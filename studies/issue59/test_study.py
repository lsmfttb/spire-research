import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import study
from budget_ledger import ledger_path, sha256_jsonl
from study import (
    CONSTRAINED_SEARCH_API,
    CONSTRAINED_SEARCH_PATCH,
    EXPECTED_STSRL_COMMIT,
    NATIVE_MODULE_SHA256,
    Root,
    RunTrace,
    branch_primary,
    call_constrained_b1_search,
    finalize,
    is_ledger_prefix_sha256,
    paired_statistics,
    preflight_qualification,
    recorded_source_starts,
    sha256_file,
    verify_preflight_gate,
    verify_protected_root_occurrences,
)
from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions


class StudyContractTests(unittest.TestCase):
    def test_preflight_cli_reports_preflight_summary_schema(self):
        report = {"terminal_disposition": "SEMANTIC_PREFLIGHT_PASS", "owners_scanned": 1}
        with patch("study.run", return_value=report), patch.object(sys, "argv", ["study.py", "--preflight"]):
            self.assertEqual(study.main(), 0)

    def test_recovery_verifier_accepts_ledger_from_a_different_checkout_path(self):
        from recovery import verify_recovery

        original_recovery = verify_recovery.RECOVERY_DIR
        original_summary = original_recovery / "results" / "summary.json"
        original_summary_bytes = original_summary.read_bytes()
        with tempfile.TemporaryDirectory(prefix="issue59-fresh-checkout-") as temp_dir:
            checkout_root = Path(temp_dir) / "checkout-at-a-different-absolute-path"
            copied_recovery = checkout_root / "studies" / "issue59" / "recovery"
            shutil.copytree(original_recovery, copied_recovery)
            with patch.object(verify_recovery, "RECOVERY_DIR", copied_recovery), redirect_stdout(io.StringIO()):
                self.assertEqual(verify_recovery.verify(), 0)

            report = json.loads((copied_recovery / "source_parity.json").read_text(encoding="utf-8"))
            summary = json.loads((copied_recovery / "results" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["study_terminal_disposition"], "TWO_BATTLE_VALUE_UNRESOLVED")
            self.assertEqual(summary["terminal_disposition"], "TWO_BATTLE_VALUE_UNRESOLVED")

        self.assertEqual(original_summary.read_bytes(), original_summary_bytes)

    def test_pinned_executor_source_commit_and_hashes(self):
        self.assertEqual(study.git(study.STSRL, "rev-parse", "HEAD"), EXPECTED_STSRL_COMMIT)
        for name, expected in study.EXPECTED_STSRL_HASHES.items():
            self.assertEqual(sha256_file(study.STSRL / name), expected, name)

    def setUp(self):
        self.potion = {"potion_index": 0, "id": 7, "id_label": "FIRE_POTION", "name": "Fire Potion"}
        self.actions = [
            SimpleNamespace(kind="potion", label="Use Fire Potion", action_id=90,
                raw={"scope": "battle", "bits": 1, "idx1": 0, "idx2": 0}),
            SimpleNamespace(kind="potion_discard", label="Discard Fire Potion", action_id=91,
                raw={"scope": "battle", "bits": 2, "idx1": 0, "idx2": 6}),
            SimpleNamespace(kind="potion", label="Use Strength Potion", action_id=92,
                raw={"scope": "battle", "bits": 3, "idx1": 1, "idx2": 0}),
            SimpleNamespace(kind="card", label="Play Strike", action_id=1,
                raw={"scope": "battle", "bits": 4, "idx1": 0}),
        ]
        self.identities = action_identity_dicts_for_actions(self.actions)
        self.root = SimpleNamespace(selected_identity=self.identities[0], potion=self.potion)

    def native_rows(self):
        rows = []
        for index, action in enumerate(self.actions[:3]):
            protected = index in (0, 1)
            rows.append({
                "scope": action.raw["scope"], "bits": action.raw["bits"],
                "kind": action.kind, "idx1": action.raw["idx1"],
                "search_exclusion_reason": "forbidden_potion_slot" if protected else None,
                "search_tree_present": not protected,
                "visits": 0 if protected else 3,
                "evaluation_sum": None if protected else 1.2,
                "mean_value": None if protected else 0.4,
            })
        return {
            "native_api": CONSTRAINED_SEARCH_API,
            "patch_identity": CONSTRAINED_SEARCH_PATCH,
            "forbidden_potion_slot": 0,
            "search_constraint": "potion_source_slot_omitted_from_tree_and_random_rollouts",
            "root_rows": rows,
            "tree_forbidden_potion_slot_exclusion_count": 2,
            "rollout_forbidden_potion_slot_exclusion_count": 7,
        }

    def witness_owner(self):
        source_root = {"source_battle_number": 2, "potion": self.potion}
        reserve_trace = {
            "b1_constrained_decisions": 1,
            "b1_exact_source_potion_uses": 0,
            "b1_exact_source_potion_discards": 0,
            "b1_constraint_closed": True,
            "b2_unmasked_decisions": 0,
            "b2_decisions": 0,
            "battle_exits": [{"battle_number": 1}],
            "constrained_root_witness": {
                "native_api": CONSTRAINED_SEARCH_API,
                "patch_identity": CONSTRAINED_SEARCH_PATCH,
                "forbidden_potion_slot": 0,
                "search_constraint": "potion_source_slot_omitted_from_tree_and_random_rollouts",
                "protected_rows": [
                    {"search_exclusion_reason": "forbidden_potion_slot", "search_tree_present": False,
                     "visits": 0, "evaluation_sum": None, "mean_value": None},
                ],
                "visited_allowed_action_count": 1,
            },
        }
        return {
            "seed": 559001,
            "source_disposition": "eligible_root",
            "source_root": source_root,
            "source_battle_exits": [{"qualifying_source_use_not_yet_selected": True, "terminal": False}],
            "preflight_integrity_pass": True,
            "consume": {"root_restore_equal": True,
                "endpoint": {"primary": None, "status": "terminal_heart_victory_before_b2"}},
            "reserve": {"root_restore_equal": True, "trace": reserve_trace,
                "endpoint": {"primary": None, "status": "terminal_heart_victory_before_b2"}},
        }

    def test_native_rows_prove_use_and_discard_are_excluded_but_other_slot_is_searchable(self):
        evidence = verify_protected_root_occurrences(self.native_rows(), self.actions, 0, self.root)
        self.assertEqual(evidence["protected_legal_occurrence_count"], 2)
        self.assertEqual(evidence["source_selected_legal_index"], 0)
        self.assertGreater(evidence["tree_forbidden_potion_slot_exclusion_count"], 0)
        self.assertGreater(evidence["rollout_forbidden_potion_slot_exclusion_count"], 0)

    def test_later_constrained_decision_does_not_require_source_action_to_remain_legal(self):
        later_actions = [self.actions[1], self.actions[2], self.actions[3]]
        raw = self.native_rows()
        raw["root_rows"] = raw["root_rows"][1:]

        with self.assertRaisesRegex(ValueError, "action identity matched 0 legal actions"):
            verify_protected_root_occurrences(raw, later_actions, 0, self.root)

        evidence = verify_protected_root_occurrences(
            raw, later_actions, 0, self.root, initial_restored_root=False)

        self.assertEqual(evidence["protected_legal_occurrence_count"], 1)
        self.assertIsNone(evidence["source_selected_legal_index"])
        self.assertFalse(evidence["source_selected_identity_verified"])

    def test_committed_budget_hashes_identify_the_canonical_ledger_and_preflight_prefix(self):
        study_dir = Path(__file__).resolve().parent
        ledger = study_dir / "budget_events.jsonl"
        preflight = json.loads((study_dir / "preflight" / "summary.json").read_text(encoding="utf-8"))
        results = json.loads((study_dir / "results" / "summary.json").read_text(encoding="utf-8"))
        results_manifest_path = study_dir / "results" / "manifest.json"
        results_manifest = json.loads(results_manifest_path.read_text(encoding="utf-8"))

        self.assertTrue(is_ledger_prefix_sha256(ledger, preflight["budget_events_sha256"]))
        self.assertEqual(sha256_jsonl(ledger), results["budget_events_sha256"])
        self.assertEqual(results["terminal_disposition"], "VALIDITY_FAILURE")
        self.assertEqual(results_manifest["preflight_gate"]["budget_events_sha256"],
            preflight["budget_events_sha256"])
        self.assertEqual(results_manifest["preflight_gate"]["summary_sha256"],
            sha256_file(study_dir / "preflight" / "summary.json"))
        self.assertEqual(results["producer_manifest_sha256"], sha256_file(results_manifest_path))

    def test_measured_runner_shares_ledger_path_with_child_and_hashes_appended_evidence(self):
        if os.name != "posix":
            self.skipTest("measured CPU runner uses POSIX child-process accounting")
        runner = Path(__file__).resolve().with_name("run_measured_stage.py")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "nested" / "events.jsonl"
            summary = root / "summary.json"
            summary.write_text(json.dumps({"terminal_disposition": "TEST_ONLY"}), encoding="utf-8")
            child = (
                "import os; from pathlib import Path; "
                "p=Path(os.environ['ISSUE59_BUDGET_LEDGER']); p.parent.mkdir(parents=True, exist_ok=True); "
                "p.open('a', encoding='utf-8').write('{\\\"stage\\\":\\\"child\\\"}\\n')"
            )
            env = os.environ.copy()
            env["ISSUE59_BUDGET_LEDGER"] = str(ledger)
            completed = subprocess.run(
                [sys.executable, str(runner), "--summary", str(summary), "ledger-path-test", "--",
                 sys.executable, "-c", child],
                cwd=root, env=env, check=False, capture_output=True, text=True)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["stage"] for row in rows], ["child", "ledger-path-test"])
            self.assertEqual(json.loads(summary.read_text(encoding="utf-8"))["budget_events_sha256"],
                sha256_jsonl(ledger))

    def test_external_cpu_recorder_honors_the_shared_ledger_override(self):
        recorder = Path(__file__).resolve().with_name("record_external_cpu.py")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "nested" / "events.jsonl"
            env = os.environ.copy()
            env["ISSUE59_BUDGET_LEDGER"] = str(ledger)
            completed = subprocess.run(
                [sys.executable, str(recorder), "metadata-repair", "0.25", "--conservative"],
                cwd=root, env=env, check=False, capture_output=True, text=True)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["stage"], "metadata-repair")
            self.assertEqual(rows[0]["elapsed_process_cpu_s"], 0.25)
            self.assertFalse((root / "studies" / "issue59" / "budget_events.jsonl").exists())

    def test_jsonl_hash_normalizes_git_checkout_newlines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lf = root / "lf.jsonl"
            crlf = root / "crlf.jsonl"
            lf.write_bytes(b'{"stage":"one"}\n{"stage":"two"}\n')
            crlf.write_bytes(b'{"stage":"one"}\r\n{"stage":"two"}\r\n')
            self.assertEqual(sha256_jsonl(lf), sha256_jsonl(crlf))
            self.assertTrue(is_ledger_prefix_sha256(crlf, sha256_jsonl(lf)))

    def test_sha256_file_normalizes_git_text_newlines_but_preserves_binary_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lf = root / "artifact.json"
            crlf = root / "artifact-crlf.json"
            binary_lf = root / "artifact.bin"
            binary_crlf = root / "artifact-crlf.bin"
            lf.write_bytes(b'{"value":1}\n')
            crlf.write_bytes(b'{"value":1}\r\n')
            binary_lf.write_bytes(b"\x00\x01\n\x02")
            binary_crlf.write_bytes(b"\x00\x01\r\n\x02")

            self.assertEqual(sha256_file(lf), sha256_file(crlf))
            self.assertNotEqual(sha256_file(binary_lf), sha256_file(binary_crlf))

    def test_relative_ledger_override_is_anchored_to_study_directory(self):
        expected = (Path(__file__).resolve().parent / "custom" / "budget.jsonl").resolve()
        with patch.dict(os.environ, {"ISSUE59_BUDGET_LEDGER": "custom/budget.jsonl"}):
            self.assertEqual(ledger_path(), expected)

    def test_native_report_must_keep_zero_visit_null_value_rows(self):
        raw = self.native_rows()
        raw["root_rows"][0]["visits"] = 1
        with self.assertRaisesRegex(RuntimeError, "zero-visit/null-mean"):
            verify_protected_root_occurrences(raw, self.actions, 0, self.root)

    def test_constrained_forwarder_checks_snapshot_and_passes_exact_native_parameters(self):
        seen = {}
        class Adapter:
            _sim = SimpleNamespace()
            def _assert_snapshot_is_current(self, snapshot):
                seen["snapshot"] = snapshot
            def native_search(self, simulations, include_potions, prior, leaf, **kwargs):
                seen.update({"simulations": simulations, "include_potions": include_potions,
                    "prior": prior, "leaf": leaf, **kwargs})
                return {"native_api": CONSTRAINED_SEARCH_API}
        adapter = Adapter()
        adapter._sim.battle_search_v2 = adapter.native_search
        snapshot = object()
        result = call_constrained_b1_search(adapter, snapshot, 0)
        self.assertEqual(result["native_api"], CONSTRAINED_SEARCH_API)
        self.assertIs(seen["snapshot"], snapshot)
        self.assertEqual(seen["simulations"], 400)
        self.assertIs(seen["include_potions"], True)
        self.assertIsNone(seen["prior"])
        self.assertIsNone(seen["leaf"])
        self.assertEqual(seen["forbidden_potion_slot"], 0)

    def test_later_natural_battle_is_sufficient_without_b2_or_divergence(self):
        owner = self.witness_owner()
        checks = preflight_qualification(owner)
        self.assertTrue(checks["source_root_is_first_qualifying_use_in_later_battle"])
        self.assertTrue(checks["constrained_slot_matches_source_potion_identity"])
        self.assertTrue(checks["qualifies"])

    def test_changing_the_reserved_slot_fails_preflight(self):
        owner = self.witness_owner()
        owner["reserve"]["trace"]["constrained_root_witness"]["forbidden_potion_slot"] = 1
        self.assertFalse(preflight_qualification(owner)["qualifies"])

    def test_primary_outcome_requires_b2_resolution_or_terminal_heart_win(self):
        trace = RunTrace()
        self.assertIsNone(branch_primary(trace)["primary"])
        self.assertEqual(branch_primary(trace)["status"], "incomplete_no_b2_resolution")

        trace.terminal = True
        trace.outcome = "PLAYER_VICTORY"
        trace.terminal_act = 4
        self.assertEqual(branch_primary(trace)["primary"], 1)
        self.assertEqual(branch_primary(trace)["status"], "terminal_heart_victory_before_b2")

        trace.terminal_act = 3
        self.assertIsNone(branch_primary(trace)["primary"])
        self.assertEqual(branch_primary(trace)["status"], "unclassified_terminal_before_b2")
        trace.outcome = "PLAYER_LOSS"
        self.assertEqual(branch_primary(trace)["primary"], 0)
        self.assertEqual(branch_primary(trace)["status"], "true_death_before_b2")

        trace.terminal = False
        trace.b2_resolved = True
        trace.b2_outcome = "PLAYER_ESCAPE"
        self.assertEqual(branch_primary(trace)["primary"], 1)
        self.assertTrue(branch_primary(trace)["b2_escape"])

        trace.b2_outcome = "PLAYER_LOSS"
        self.assertEqual(branch_primary(trace)["primary"], 0)
        trace.b2_outcome = "UNKNOWN"
        self.assertIsNone(branch_primary(trace)["primary"])

    def test_owner_bootstrap_keeps_incomplete_pairs_in_fixed_denominator_bounds(self):
        owners = [
            {"source_disposition": "eligible_root",
             "consume": {"endpoint": {"primary": 1}},
             "reserve": {"endpoint": {"primary": 0}}},
            {"source_disposition": "eligible_root",
             "consume": {"endpoint": {"primary": None}},
             "reserve": {"endpoint": {"primary": 1}}},
        ]
        result = paired_statistics(owners)
        self.assertEqual(result["eligible_owner_denominator"], 2)
        self.assertEqual(result["complete_paired_owners"], 1)
        self.assertEqual(result["primary_fixed_denominator_difference_bounds"], [0.0, 0.5])
        self.assertEqual(result["bootstrap_seed"], 559099)
        self.assertEqual(result["bootstrap_replicates"], 10000)
        self.assertEqual(result["discordant_pairs"]["consume_only_success"], 1)

    def test_complete_cohort_without_natural_exposure_is_labeled_insufficient(self):
        rows = [{"seed": seed, "source_disposition": "source_terminal_no_qualifying_use"}
                for seed in study.SEEDS]
        report = finalize(rows, SimpleNamespace(prior_cpu_s=0.0, cpu_started=0.0))
        self.assertEqual(report["terminal_disposition"], "INSUFFICIENT_NATURAL_EXPOSURE")
        self.assertEqual(report["owners_processed"], 64)
        self.assertEqual(report["eligible_owner_denominator"], 0)

    def test_formal_gate_checks_preflight_source_hashes_and_later_battle_witness_in_crlf_checkout(self):
        producer = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            manifest_path = path / "manifest.json"
            owners_path = path / "owners.jsonl"
            source_path = path / "source_attempts.jsonl"
            summary_path = path / "summary.json"
            budget_path = path / "budget_events.jsonl"
            owner = self.witness_owner()
            owner["preflight_role"] = "later_battle_semantic_witness"
            manifest_path.write_text(json.dumps({
                "study_code_sha256": sha256_file(Path(study.__file__)),
                "producer_checkout": producer,
                "simulator": {"module_sha256": NATIVE_MODULE_SHA256},
                "executor_commit": EXPECTED_STSRL_COMMIT,
            }, indent=2) + "\n", encoding="utf-8")
            owners_path.write_text(json.dumps(owner) + "\n", encoding="utf-8")
            source_path.write_text(json.dumps({"seed": 559001}) + "\n", encoding="utf-8")
            budget_path.write_text(json.dumps({"stage": "preflight", "elapsed_process_cpu_s": 5.0}) + "\n",
                encoding="utf-8")
            summary_path.write_text(json.dumps({
                "preflight_semantic_pass": True,
                "terminal_disposition": "SEMANTIC_PREFLIGHT_PASS",
                "producer_manifest_sha256": sha256_file(manifest_path),
                "owners_jsonl_sha256": sha256_file(owners_path),
                "source_attempts_sha256": sha256_file(source_path),
                "budget_events_sha256": sha256_file(budget_path),
            }, indent=2) + "\n", encoding="utf-8")
            text_artifacts = (manifest_path, owners_path, source_path, summary_path, budget_path)
            for artifact in text_artifacts:
                artifact.write_bytes(artifact.read_bytes().replace(b"\r\n", b"\n"))
            lf_hashes = {artifact: sha256_file(artifact) for artifact in text_artifacts}
            for artifact in text_artifacts:
                artifact.write_bytes(artifact.read_bytes().replace(b"\n", b"\r\n"))
                self.assertIn(b"\r\n", artifact.read_bytes(), artifact.name)
            with patch.dict(os.environ, {"ISSUE59_BUDGET_LEDGER": str(budget_path)}):
                gate = verify_preflight_gate(path, producer)
                self.assertEqual(gate["budget_events_sha256"], lf_hashes[budget_path])
            self.assertEqual(gate["summary_sha256"], lf_hashes[summary_path])
            self.assertEqual(gate["manifest_sha256"], lf_hashes[manifest_path])
            self.assertEqual(gate["owners_jsonl_sha256"], lf_hashes[owners_path])
            self.assertEqual(gate["source_attempts_sha256"], lf_hashes[source_path])
            self.assertEqual(gate["witness_owner_seed"], 559001)
            self.assertTrue(gate["semantic_witness"]["qualifies"])
            source_path.write_text(source_path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
            with patch.dict(os.environ, {"ISSUE59_BUDGET_LEDGER": str(budget_path)}):
                with self.assertRaisesRegex(RuntimeError, "source-attempt ledger"):
                    verify_preflight_gate(path, producer)

    def test_source_start_boundary_identifies_interrupted_owner_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "preflight"
            other = root / "formal"
            ledger = root / "budget.jsonl"
            ledger.write_text("\n".join([
                json.dumps({"run_directory": str(run.resolve()), "stage": "source_started", "owner_seed": 559001}),
                json.dumps({"run_directory": str(other.resolve()), "stage": "source_started", "owner_seed": 559001}),
            ]) + "\n", encoding="utf-8")
            self.assertEqual(recorded_source_starts(ledger, run), {559001})

    def test_preflight_budget_hash_remains_valid_only_as_an_append_only_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "budget.jsonl"
            ledger.write_text('{"stage":"preflight"}\n', encoding="utf-8")
            prefix = sha256_file(ledger)
            with ledger.open("a", encoding="utf-8") as handle:
                handle.write('{"stage":"formal"}\n')
            self.assertTrue(is_ledger_prefix_sha256(ledger, prefix))
            self.assertFalse(is_ledger_prefix_sha256(ledger, "0" * 64))

    def test_measured_runner_refuses_to_launch_when_reviewer_reserve_is_at_risk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "budget.jsonl"
            marker = root / "must-not-exist"
            ledger.write_text(json.dumps({"elapsed_process_cpu_s": 10199.0}) + "\n", encoding="utf-8")
            runner = Path(__file__).with_name("run_measured_stage.py")
            command = [sys.executable, str(runner), "--ledger", str(ledger), "guard-test", "--",
                sys.executable, "-c", "import sys; from pathlib import Path; Path(sys.argv[1]).touch()", str(marker)]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertFalse(marker.exists())
            rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(rows[-1]["budget_guard_skipped"])


if __name__ == "__main__":
    unittest.main()
