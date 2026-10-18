"""Focused control/accounting checks for the Issue 60 production entry."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import production_entry as production


class ProductionEntryTests(unittest.TestCase):
    def test_initial_checkpoint_routes_only_to_integration_milestone(self):
        self.assertEqual(
            production.execution_phase(1, {"route:builder"}, [], "base"),
            "integration-milestone",
        )

    def test_batch02_checkpoint_waits_for_exact_reviewed_head(self):
        labels = {"route:builder"}
        comments = [{"author": {"login": production.REVIEWER_GITHUB_LOGIN},
                     "body": "issue60-integration-review-v1: PASS abc123"}]
        self.assertEqual(production.execution_phase(2, labels, comments, "different"), "awaiting-review")
        self.assertEqual(production.execution_phase(2, labels, comments, "abc123"), "full-continuation")
        self.assertEqual(production.execution_phase(2, {"route:reviewer"}, comments, "abc123"), "awaiting-review")

    def test_quoted_handoff_marker_does_not_authorize_continuation(self):
        marker = "issue60-integration-review-v1: PASS abc123"
        comments = [{
            "author": {"login": production.REVIEWER_GITHUB_LOGIN},
            "body": f"Builder handoff asks for review: `{marker}`",
        }]
        self.assertEqual(
            production.execution_phase(2, {"route:builder"}, comments, "abc123"),
            "awaiting-review",
        )

    def test_review_marker_requires_designated_author_and_exact_comment_body(self):
        marker = "issue60-integration-review-v1: PASS abc123"
        for comment in (
            {"author": {"login": "someone-else"}, "body": marker},
            {"author": {"login": production.REVIEWER_GITHUB_LOGIN}, "body": f"PASS\n{marker}"},
            {"body": marker},
        ):
            with self.subTest(comment=comment):
                self.assertEqual(
                    production.execution_phase(2, {"route:builder"}, [comment], "abc123"),
                    "awaiting-review",
                )

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
