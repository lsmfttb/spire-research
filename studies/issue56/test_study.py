import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import study
from study import (
    DecisionContext,
    EXPECTED_STSRL_COMMIT,
    NATIVE_MODULE_SHA256,
    RunTrace,
    branch_primary,
    eligible_after_exact_potion_mask,
    finalize,
    paired_statistics,
    qualifying_preflight_seeds,
    sha256_file,
    verify_preflight_gate,
)


class StudyContractTests(unittest.TestCase):
    def setUp(self):
        self.source = {"potion_index": 0, "id": 7, "id_label": "FIRE_POTION", "name": "Fire Potion"}
        self.other = {"potion_index": 1, "id": 8, "id_label": "STRENGTH_POTION", "name": "Strength Potion"}
        self.actions = [
            SimpleNamespace(kind="potion", raw={"idx1": 0, "idx2": 0}),
            SimpleNamespace(kind="potion_discard", raw={"idx1": 0, "idx2": 6}),
            SimpleNamespace(kind="potion", raw={"idx1": 1, "idx2": 0}),
            SimpleNamespace(kind="card", raw={"idx1": 0}),
        ]
        self.context = DecisionContext(
            screen_state="BATTLE", snapshot_features=[], legal_action_features=[[], [], [], []],
            legal_action_kinds=[x.kind for x in self.actions], eligible_action_indices=[0, 1, 2, 3])

    def test_mask_blocks_only_exact_source_slot_use_and_discard(self):
        current = {"battle_potions": [self.source, self.other]}
        self.assertEqual(
            eligible_after_exact_potion_mask(self.actions, self.context, self.source, current),
            [2, 3],
        )

    def test_mask_fails_closed_if_source_instance_identity_changes(self):
        current = {"battle_potions": [{"potion_index": 0, "id": 9, "id_label": "FAIRY_POTION", "name": "Fairy Potion"}, self.other]}
        with self.assertRaises(RuntimeError):
            eligible_after_exact_potion_mask(self.actions, self.context, self.source, current)

    def test_b2_escape_is_primary_survival_and_remains_separately_labeled(self):
        trace = RunTrace()
        trace.b2_resolved = True
        trace.b2_outcome = "PLAYER_ESCAPE"
        result = branch_primary(trace)
        self.assertEqual(result["primary"], 1)
        self.assertTrue(result["b2_escape"])
        self.assertEqual(result["b2_outcome"], "PLAYER_ESCAPE")

        trace.b2_outcome = "UNKNOWN"
        result = branch_primary(trace)
        self.assertIsNone(result["primary"])
        self.assertEqual(result["status"], "unclassified_b2_outcome")

    def test_only_heart_terminal_victory_is_early_terminal_success(self):
        trace = RunTrace()
        trace.terminal = True
        trace.outcome = "PLAYER_VICTORY"
        trace.terminal_act = 4
        result = branch_primary(trace)
        self.assertEqual(result["primary"], 1)
        self.assertEqual(result["status"], "terminal_heart_victory_before_b2")

        trace.terminal_act = 3
        result = branch_primary(trace)
        self.assertIsNone(result["primary"])
        self.assertEqual(result["status"], "unclassified_terminal_before_b2")

        trace.outcome = "PLAYER_LOSS"
        result = branch_primary(trace)
        self.assertEqual(result["primary"], 0)
        self.assertEqual(result["status"], "true_death_before_b2")

    def test_incomplete_pair_uses_fixed_denominator_bounds(self):
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
        self.assertEqual(result["complete_case_paired_difference_consume_minus_reserve"], 1.0)
        self.assertEqual(result["primary_fixed_denominator_difference_bounds"], [0.0, 0.5])
        self.assertEqual(result["primary_estimator"],
            "fixed-denominator worst-case bounds with owner-resampled paired bootstrap envelope")

    def test_final_report_handles_sources_without_an_eligible_root(self):
        report = finalize([{"seed": 558001, "source_disposition": "source_terminal_no_qualifying_use"}],
            SimpleNamespace(prior_cpu_s=0.0, cpu_started=0.0))
        self.assertEqual(report["eligible_owner_denominator"], 0)
        self.assertEqual(report["terminal_disposition"], "RESOURCE_BLOCKED_INCOMPLETE")

    def test_preflight_requires_two_owners_with_paired_b2_and_b1_long_divergence(self):
        def owner(seed, consume_b2, reserve_b2, consume_states, reserve_states):
            def branch(has_b2, states, masked=0):
                return {
                    "endpoint": {"status": "b2_resolved" if has_b2 else "terminal_before_b2"},
                    "trace": {
                        "battle_exits": [{}, {}] if has_b2 else [{}],
                        "between_b1_b2_noncombat_decisions": 1,
                        "b1_raw_state_fingerprint_sha256": states,
                        "b1_masked_decisions": masked,
                    },
                }
            return {
                "seed": seed,
                "source_disposition": "eligible_root",
                "preflight_integrity_pass": True,
                "consume": branch(consume_b2, consume_states),
                "reserve": branch(reserve_b2, reserve_states, masked=2),
            }

        split = [
            owner(558001, True, False, ["root", "a"], ["root", "b"]),
            owner(558002, False, True, ["root", "c"], ["root", "d"]),
        ]
        self.assertEqual(qualifying_preflight_seeds(split), [])
        valid = [
            owner(558001, True, True, ["root", "a"], ["root", "b"]),
            owner(558002, True, True, ["root", "c"], ["root", "d"]),
        ]
        self.assertEqual(qualifying_preflight_seeds(valid), [558001, 558002])

    def test_formal_gate_checks_preflight_hashes_and_source_identity(self):
        producer = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            manifest_path = path / "manifest.json"
            owners_path = path / "owners.jsonl"
            sources_path = path / "source_attempts.jsonl"
            summary_path = path / "summary.json"
            manifest_path.write_text(json.dumps({
                "study_code_sha256": sha256_file(Path(study.__file__)),
                "producer_checkout": producer,
                "simulator": {"module_sha256": NATIVE_MODULE_SHA256},
                "executor_commit": EXPECTED_STSRL_COMMIT,
            }), encoding="utf-8")
            owners = []
            for seed in (558001, 558002):
                owners.append({
                    "seed": seed,
                    "source_disposition": "eligible_root",
                    "preflight_integrity_pass": True,
                    "consume": {"endpoint": {"status": "b2_resolved"},
                        "trace": {"battle_exits": [{}, {}],
                            "between_b1_b2_noncombat_decisions": 1,
                            "b1_raw_state_fingerprint_sha256": ["root", f"a-{seed}"]}},
                    "reserve": {"endpoint": {"status": "b2_resolved"},
                        "trace": {"battle_exits": [{}, {}],
                            "between_b1_b2_noncombat_decisions": 1,
                            "b1_masked_decisions": 2,
                            "b1_raw_state_fingerprint_sha256": ["root", f"b-{seed}"]}},
                })
            owners_path.write_text("".join(json.dumps(row) + "\n" for row in owners), encoding="utf-8")
            sources_path.write_text(json.dumps({"seed": 558001}) + "\n", encoding="utf-8")
            manifest_sha = sha256_file(manifest_path)
            summary_path.write_text(json.dumps({
                "preflight_integrity_pass": True,
                "preflight_qualified_owner_seeds": [558001, 558002],
                "producer_manifest_sha256": manifest_sha,
                "owners_jsonl_sha256": sha256_file(owners_path),
                "source_attempts_sha256": sha256_file(sources_path),
            }), encoding="utf-8")
            gate = verify_preflight_gate(path, producer)
            self.assertEqual(gate["qualified_owner_seeds"], [558001, 558002])
            owners_path.write_text(owners_path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "owner ledger"):
                verify_preflight_gate(path, producer)

    def test_checked_in_preflight_passes_checked_in_formal_gate(self):
        artifact_root = Path(__file__).resolve().parent
        preflight_path = artifact_root / "preflight"
        preflight_manifest = json.loads((preflight_path / "manifest.json").read_text(encoding="utf-8"))
        formal_manifest = json.loads((artifact_root / "results" / "manifest.json").read_text(encoding="utf-8"))
        producer = formal_manifest["producer_checkout"]
        current_code_sha = sha256_file(Path(study.__file__).resolve())

        self.assertEqual(preflight_manifest["producer_checkout"], producer)
        self.assertEqual(preflight_manifest["study_code_sha256"], current_code_sha)
        self.assertEqual(formal_manifest["study_code_sha256"], current_code_sha)
        gate = verify_preflight_gate(preflight_path, producer)
        self.assertEqual(gate["qualified_owner_seeds"], formal_manifest["preflight_gate"]["qualified_owner_seeds"])

        owners = [json.loads(line) for line in (artifact_root / "results" / "owners.jsonl").read_text(encoding="utf-8").splitlines()]
        later_root_owners = [row for row in owners
            if row.get("source_disposition") == "eligible_root"
            and row.get("source_root_battle_number", 1) > 1]
        self.assertTrue(later_root_owners, "fixed natural-run cohort has no real later-battle source root regression case")
        for owner in later_root_owners:
            root_battle = owner["source_root_battle_number"]
            exits = owner["source_battle_exits"]
            self.assertEqual(root_battle, owner["source_root"]["source_battle_number"])
            self.assertEqual(owner["source_battles_before_root"], root_battle - 1)
            self.assertEqual([row["battle_number"] for row in exits], list(range(1, root_battle)))
            self.assertTrue(all(row["qualifying_source_use_not_yet_selected"] for row in exits))
            self.assertFalse(any(row["terminal"] for row in exits))

        for owner in owners:
            if owner.get("source_disposition") == "source_terminal_no_qualifying_use":
                self.assertTrue(owner["source_terminal"])
            if owner.get("source_disposition") == "source_truncated_before_qualifying_use":
                self.assertFalse(owner["source_terminal"])
            if str(owner.get("source_disposition", "")).startswith("not_run"):
                self.assertNotIn("no_qualifying_use", owner["source_disposition"])


if __name__ == "__main__":
    unittest.main()
