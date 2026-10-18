"""Focused control/accounting checks for the Issue 60 production entry."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import production_entry as production


class ProductionEntryTests(unittest.TestCase):
    def test_initial_checkpoint_routes_only_to_integration_milestone(self):
        self.assertEqual(
            production.execution_phase(1, {"route:builder"}),
            "integration-milestone",
        )

    def test_batch02_checkpoint_cannot_auto_continue_from_issue_comments(self):
        marker = "issue60-integration-review-v1: PASS abc123"
        builder_comment = {
            "author": {"login": "lsmfttb"},
            "body": marker,
        }
        response = subprocess.CompletedProcess(
            args=["gh", "issue", "view"], returncode=0,
            stdout=json.dumps({
                "labels": [{"name": "route:builder"}],
                "comments": [builder_comment],
            }), stderr="",
        )
        with patch.object(production, "_run_counted", return_value=response) as run:
            labels = production._issue_state()
        self.assertEqual(run.call_args.args[0][-2:], ["--json", "labels"])
        self.assertEqual(
            production.execution_phase(2, labels),
            "awaiting-review",
        )
        self.assertEqual(production.execution_phase(2, {"route:reviewer"}), "awaiting-review")
        self.assertEqual(
            production.execution_phase(2, labels, continue_reviewed=True),
            "full-continuation",
        )

    def test_default_cli_keeps_batch02_at_review_boundary(self):
        args = production.parse_production_args([])
        self.assertFalse(args.continue_reviewed)
        self.assertEqual(
            production.execution_phase(2, {"route:builder"}, continue_reviewed=args.continue_reviewed),
            "awaiting-review",
        )

    def test_explicit_continuation_requires_builder_route_and_batch02_checkpoint(self):
        with self.assertRaisesRegex(production.ProductionError, "routed to Builder"):
            production.execution_phase(2, {"route:reviewer"}, continue_reviewed=True)
        with self.assertRaisesRegex(production.ProductionError, "batch-02 checkpoint"):
            production.execution_phase(1, {"route:builder"}, continue_reviewed=True)

    def test_cli_continuation_runs_both_blocks_then_evaluation_and_analysis(self):
        args = production.parse_production_args(["--continue-reviewed"])
        self.assertTrue(args.continue_reviewed)
        phase = production.execution_phase(
            2, {"route:builder"}, continue_reviewed=args.continue_reviewed,
        )
        self.assertEqual(phase, "full-continuation")

        events = []
        class Study:
            def train_block(self, block, config, vocab, bootstrap, runtime, stage_started):
                events.append(("train", block, config, vocab, bootstrap, runtime, stage_started))
                return {"block": block}

            def config_sha(self):
                return "config-sha"

            def atomic_json(self, path, payload):
                events.append(("write-training", path, payload))

            def run_evaluation(self, config, runtime, vocab, bootstrap, stage_started):
                events.append(("evaluation", config, runtime, vocab, bootstrap, stage_started))
                return {"run_count": 640}

            def analyze(self, config):
                events.append(("analysis", config))
                return {"status": "COMPLETE"}

        config, vocab, bootstrap, runtime, stage_started = (
            {"sources": {"encoder": "pinned"}}, object(), object(), {"runtime": True}, 12.5,
        )
        evaluation, analysis = production.run_full_continuation_stages(
            Study(), config, vocab, bootstrap, runtime, stage_started,
        )

        self.assertEqual(evaluation, {"run_count": 640})
        self.assertEqual(analysis, {"status": "COMPLETE"})
        self.assertEqual([event[0] for event in events], [
            "train", "train", "write-training", "evaluation", "analysis",
        ])
        self.assertEqual([event[1] for event in events[:2]], ["block1", "block2"])
        self.assertTrue(all(event[5] is runtime for event in events[:2]))
        self.assertIs(events[3][2], runtime)
        self.assertEqual(events[4][1], config)
        self.assertEqual(events[2][2]["source_pins"], config["sources"])

    def test_production_entry_must_match_reviewed_head_blob(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            source = repo / "production_entry.py"
            source.write_text("original = True\n", encoding="utf-8")

            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(repo), *args],
                    check=True, capture_output=True, text=True,
                )

            git("init", "-q")
            git("add", "production_entry.py")
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                 "user.email=test@example.invalid", "commit", "-qm", "source"],
                check=True, capture_output=True, text=True,
            )
            with patch.object(production, "_worktree_git_environment", return_value={}):
                path, blob = production._verify_committed_source(
                    repo, source, source_name="production_entry.py",
                )
                self.assertEqual(path, "production_entry.py")
                self.assertEqual(blob, git("rev-parse", "HEAD:production_entry.py").stdout.strip())

                source.write_text("unreviewed = True\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    production.ProductionError, "bytes differ from the committed source tree",
                ):
                    production._verify_committed_source(
                        repo, source, source_name="production_entry.py",
                    )

    def test_full_continuation_passes_runtime_without_dispatching_games(self):
        config, vocab, bootstrap, runtime, stage_started = {}, object(), object(), {"runtime": True}, 12.5
        with patch.object(
            production.run_study, "train_block",
            side_effect=lambda block, *_args: {"block": block},
        ) as train_block:
            result = production.run_full_training_continuation(
                production.run_study, config, vocab, bootstrap, runtime, stage_started,
            )
        self.assertEqual(result, [{"block": "block1"}, {"block": "block2"}])
        self.assertEqual(train_block.call_args_list, [
            call("block1", config, vocab, bootstrap, runtime, stage_started),
            call("block2", config, vocab, bootstrap, runtime, stage_started),
        ])

    def test_inflight_game_and_optimizer_reserves_are_included(self):
        self.assertTrue(production.can_dispatch_with_reserves(
            actual_cpu=100.0, cpu_limit=3000.0, active_or_pending_workers=2,
            game_reserve=400.0, optimizer_reserve=1200.0, parent_reserve=60.0,
        ))
        self.assertFalse(production.can_dispatch_with_reserves(
            actual_cpu=500.0, cpu_limit=3000.0, active_or_pending_workers=3,
            game_reserve=500.0, optimizer_reserve=1200.0, parent_reserve=60.0,
        ))

    def test_integration_worker_selection_accounts_for_reserves_and_uses_small_cap(self):
        workers = production.select_milestone_worker_count(
            preferred_workers=8, actual_cpu=275.0, game_reserve=459.4,
            optimizer_reserve=1144.3, parent_reserve=60.0,
        )
        self.assertEqual(workers, 2)
        self.assertTrue(production.can_dispatch_with_reserves(
            actual_cpu=275.0, cpu_limit=3000.0, active_or_pending_workers=workers,
            game_reserve=459.4, optimizer_reserve=1144.3, parent_reserve=60.0,
        ))

    def test_interrupted_reservations_become_debits_without_changing_measured_cpu(self):
        with tempfile.TemporaryDirectory() as temporary:
            progress_path = Path(temporary) / "production-progress.json"
            with patch.object(production, "PROGRESS_PATH", progress_path):
                first = production.ProductionProgress(Path(temporary), config_sha="config")
                first.data["measured_new_cpu_seconds"] = 25.0
                first.data["historical_admin_debit_cpu_seconds"] = 1_572.370806919
                first.data["open_invocation"] = {"reservation_seconds": 60.0}
                first.data["open_optimizer_reservation"] = {"reservation_seconds": 900.0}
                first.data["open_worker_reservations"] = {
                    "[\"owner-001\",669001,\"A\"]": {
                        "reservation_seconds": 300.0, "task_key": ["owner-001", 669001, "A"],
                    }
                }
                first.persist()

                resumed = production.ProductionProgress(Path(temporary), config_sha="config")
                self.assertEqual(resumed.data["measured_new_cpu_seconds"], 25.0)
                self.assertEqual(resumed.data["administrative_debit_cpu_seconds"], 1260.0)
                self.assertEqual(resumed.data["accounted_new_cpu_seconds"], 1285.0)
                self.assertEqual(resumed.data["historical_admin_debit_cpu_seconds"], 1_572.370806919)
                self.assertIsNone(resumed.data["open_invocation"])
                self.assertIsNone(resumed.data["open_optimizer_reservation"])
                self.assertEqual(resumed.data["open_worker_reservations"], {})


if __name__ == "__main__":
    unittest.main()
