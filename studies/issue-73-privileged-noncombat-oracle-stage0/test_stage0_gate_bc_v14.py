"""Focused no-native tests for the V14 Stage-0 Gate C operational repair."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import checkpoint_replay_qualification as q
import stage0_gate_bc_v13 as v13
import stage0_gate_bc_v14 as v14


class GateBCV14StaticTests(unittest.TestCase):
    def test_exact_registered_suffix_policy_uses_pi_suffix_field(self) -> None:
        stage0_config = q.OLD_CONFIG["stage0"]
        self.assertNotIn("suffix_policy", stage0_config)
        self.assertEqual(
            stage0_config["pi_suffix"],
            "0.80 expert_non_combat_v1 + 0.20 uniform over all currently legal actions at each multichoice",
        )
        self.assertEqual(v14.CONFIG_PATH.name, "qualification-config-v14.json")

    def test_failed_setup_01_selects_fixed_recovery_slot_without_mutation(self) -> None:
        first = v13.read_json(v14.PARENT_PATHS["v13_setup_01_attempt"])
        self.assertEqual(first["status"], "FAIL_OR_INCOMPLETE")
        before = dict(first)
        key, action = v14.select_setup_action([first])
        self.assertEqual((key, action), (v14.SETUP_02_KEY, "START"))
        self.assertEqual(first, before)

    def test_setup_02_complete_is_resumable_but_terminal_failure_is_not(self) -> None:
        first = v13.read_json(v14.PARENT_PATHS["v13_setup_01_attempt"])
        complete = {"key": v14.SETUP_02_KEY, "status": "COMPLETE"}
        self.assertEqual(v14.select_setup_action([first, complete]), (v14.SETUP_02_KEY, "RESUME"))
        failed = {"key": v14.SETUP_02_KEY, "status": "FAIL_OR_INCOMPLETE"}
        with self.assertRaisesRegex(v14.GateBCV14Failure, "no retry"):
            v14.select_setup_action([first, failed])

    def test_setup_01_started_is_not_retried_or_bypassed(self) -> None:
        first = {"key": v14.SETUP_01_KEY, "status": "STARTED"}
        with self.assertRaisesRegex(v14.GateBCV14Failure, "still STARTED"):
            v14.select_setup_action([first])

    def test_v13_parent_receipts_are_exact_reviewed_gate_b_and_failed_setup(self) -> None:
        _lock, _config, plan = v14._validate_parent_evidence()
        failure = v14.read_json(v14.SETUP_01_FAILURE)
        self.assertEqual(failure["status"], "FAIL_OR_INCOMPLETE_NO_RETRY")
        self.assertEqual(failure["failure"], "'suffix_policy'")
        self.assertEqual(len(plan["policy_rng_seed_streams"]), 64)
        self.assertEqual(tuple(row["root_id"] for row in plan["roots"]), v13.GATE_C_ROOTS)

    def test_combined_resource_forecast_includes_v14_static_debit_and_all_suffixes(self) -> None:
        ledger = v13.ledger()
        projection = v14._validate_live_v13_ledger({"v13_ledger_snapshot": ledger})
        self.assertAlmostEqual(projection["charged_v13_builder_cpu_seconds"], 5073.191423)
        self.assertAlmostEqual(projection["charged_v13_native_gate_bc_cpu_seconds"], 1701.581289)
        self.assertEqual(projection["remaining_v13_native_reserve_cpu_seconds"], 1300.0)
        self.assertAlmostEqual(projection["projected_builder_cpu_seconds"], 6433.191423)
        self.assertAlmostEqual(projection["projected_native_gate_bc_cpu_seconds"], 3001.581289)
        self.assertAlmostEqual(projection["projected_aggregate_including_reviewer_cpu_seconds"], 8433.191423)
        self.assertTrue(projection["forecast_fits_registered_caps"])

    def test_stage_1_and_formal_seeds_remain_blocked(self) -> None:
        config = v13._config()
        self.assertFalse(config["stage1_authorized"])
        self.assertFalse(config["formal_owner_or_training_seed_consumed"])

    def test_unregistered_v13_attempt_is_rejected(self) -> None:
        ledger = v13.ledger()
        snapshot = {"v13_ledger_snapshot": ledger}
        changed = dict(ledger)
        changed["attempts"] = [*ledger["attempts"], {
            "key": "gate-c-unregistered-attempt",
            "kind": "gate_c_terminal_suffix_via_prefix_replay",
            "status": "COMPLETE",
            "native_gate_bc": True,
            "reserved_cpu_seconds": 20.0,
            "cpu_charged_seconds": 20.0,
            "formal_seed_consumed": False,
        }]
        with patch.object(v13, "ledger", return_value=changed):
            with self.assertRaisesRegex(v14.GateBCV14Failure, "unregistered V13 Gate C attempt"):
                v14._validate_live_v13_ledger(snapshot)


if __name__ == "__main__":
    unittest.main()
