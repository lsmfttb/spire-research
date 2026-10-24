"""Focused, no-native tests for the fixed Stage-0 Gate B/C continuation."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import checkpoint_replay_qualification as q
import stage0_gate_bc_v12 as v12


class GateBCV12StaticTests(unittest.TestCase):
    def test_parent_gate_a_v9_and_v10_static_evidence_is_complete(self) -> None:
        _capture_lock, _parity_lock, plan = v12._validate_parent_evidence()
        self.assertEqual(len(plan["policy_rng_seed_streams"]), 64)
        self.assertEqual(v12.e10.read_json(v12.e10.OUT / "reports" / "static-qualification-report-v10.json")["status"], "PASS")

    def test_historical_parent_lock_uses_its_committed_blobs(self) -> None:
        parent_lock = v12.read_json(v12.e5.LOCK_PATH)
        committed = v12._verify_committed_source_hashes(
            parent_lock["code_source_commit"],
            parent_lock["study_source_sha256"],
            label="v9 engineering-5 test fixture",
        )
        self.assertEqual(committed, parent_lock["study_source_sha256"])
        current_config = v12.sha256_bytes((v12.STUDY / "config.json").read_bytes())
        self.assertNotEqual(current_config, committed["config.json"])

    def test_pinned_native_module_digest_uses_raw_binary_bytes(self) -> None:
        native = v12.read_json(v12.e5.LOCK_PATH)["native"]
        module_path = v12.Path(native["module_path"])
        self.assertEqual(v12.sha256_bytes(module_path.read_bytes()), native["loaded_module_sha256"])

    def test_fixed_population_and_resource_projection(self) -> None:
        self.assertEqual(v12.GATE_B_ROOTS["hidden_semantic"], "root-173730001-0003")
        self.assertEqual(v12.GATE_B_ROOTS, q.GATE_B_ROOTS)
        self.assertEqual(v12.GATE_C_ROOTS, tuple(f"root-173730001-{i:04d}" for i in range(4)))
        self.assertIn("test_stage0_gate_bc_v12.py", v12.SOURCE_PATHS)
        roots, actions = v12.gate_b_root_contract()
        self.assertEqual(set(roots), set(v12.GATE_B_ROOTS))
        self.assertEqual(actions, {key: 0 for key in v12.GATE_B_ROOTS})
        keys = v12._planned_suffix_keys()
        self.assertEqual(len(keys), 64)
        self.assertEqual(len(set(keys)), 64)

        projection = v12.resource_projection()
        self.assertTrue(projection["forecast_fits_registered_caps"])
        self.assertAlmostEqual(projection["charged_builder_cpu_seconds"], 4991.610134)
        self.assertAlmostEqual(projection["projected_builder_cpu_seconds"], 6411.610134)
        self.assertAlmostEqual(projection["projected_native_gate_bc_cpu_seconds"], 3100.0)
        self.assertAlmostEqual(projection["projected_aggregate_cpu_seconds_including_reviewer_reserve"], 8411.610134)

    def test_reward_menu_parser_matches_locked_chest_public_projection(self) -> None:
        parity = v12.read_json(v12.e6.PARITY_REPORT_PATH)
        witness = parity["witnesses"]["root-173730001-0028-a0"]
        transition = witness["transition"]
        self.assertTrue(witness["cross_process_full_transition_equal"])
        projection = transition["post_public_projection"]
        pre_choices = v12.public_reward_menu_actions_v12(transition["pre_public_projection"])
        choices = v12.public_reward_menu_actions_v12(projection)
        self.assertEqual(pre_choices, [])
        self.assertEqual([row["kind"] for row in choices], ["reward_relic", "reward_key"])

        prior = v12.read_json(v12.e11.OUT / "reports" / "gate-b-live-public-privacy-report-v11.json")
        chest_transition = prior["witnesses"]["native_rng_consequence"]["transition"]
        projection_sha = v12.sha256_bytes(v12.canonical(projection).encode("utf-8"))
        self.assertEqual(projection_sha, chest_transition["post_public_projection_sha256"])
        self.assertEqual(chest_transition["root_id"], v12.GATE_B_ROOTS["native_rng_consequence"])
        self.assertEqual(chest_transition["action_index"], 0)

    def test_reward_menu_parser_requires_live_public_legal_action_source(self) -> None:
        value = {
            "candidate_actions": {
                "availability": "available",
                "source": "unverified test source",
                "value": [{"kind": "reward_relic"}],
            }
        }
        self.assertEqual(v12.public_reward_menu_actions_v12(value), [])

    def test_capture_root_is_normalized_for_replay_adapter(self) -> None:
        root = {
            "root_id": "root-173730001-0003",
            "root_index": 3,
            "run_id": "seed-173730001",
            "seed": 173730001,
            "decision_index": 3,
            "policy_decision_index": 3,
            "screen": "MAP_SCREEN",
            "native_state_sha256": "state-hash",
            "public_projection_sha256": "public-hash",
            "public_resource_state_sha256": "resource-hash",
            "projection_digest": "projection-hash",
            "projection": {"candidates": []},
            "public_projection": {"screen": "MAP_SCREEN"},
            "legal_action_identities": [{"action": "left"}, {"action": "right"}],
            "candidate_index": 0,
            "feature_matrix": [[0.0], [1.0]],
            "feature_matrix_sha256": "feature-hash",
            "live_binding_validation_passed": True,
        }
        with patch.object(v12, "read_json", return_value={"roots": [root]}), patch.object(
            v12.stage0, "source_tags", return_value=[]
        ):
            normalized = v12.locked_root(root["root_id"])

        compact = q.compact_root(normalized)
        expected_hash = v12.runner.sha256_bytes(v12.np.asarray(root["feature_matrix"], dtype="<f4").tobytes())
        self.assertEqual(compact["feature_matrix_sha256"], expected_hash)
        self.assertEqual(compact["root_id"], root["root_id"])
        self.assertTrue(compact["live_binding_validation_passed"])

    def test_sanitized_projection_negative_detects_private_name_or_value(self) -> None:
        raw = {
            "candidates": [{
                "features": {
                    "oracle_map_secret_marker": {
                        "visibility": "oracle_only",
                        "availability": "available",
                        "source": "test-source",
                        "reason": "test fixture",
                        "value": "private-sentinel-90173",
                    },
                    "unavailable_rng_sentinel": {
                        "visibility": "public",
                        "availability": "unavailable",
                        "source": "test-source",
                        "reason": "test fixture",
                        "value": None,
                    },
                }
            }]
        }
        clean = v12.audit_sanitized_projection(raw, {"screen": "MAP_SCREEN", "visible": "safe"})
        self.assertEqual(clean["private_feature_count"], 2)
        self.assertTrue(clean["all_private_names_and_values_absent"])

        leaked = v12.audit_sanitized_projection(raw, {"oracle_map_secret_marker": "private-sentinel-90173"})
        self.assertFalse(leaked["all_private_names_and_values_absent"])

    def test_scope_stays_study_only(self) -> None:
        self.assertEqual(v12.BUILDER_CAP, 10000.0)
        self.assertEqual(v12.AGGREGATE_CAP, 12000.0)
        self.assertEqual(v12.NATIVE_CAP, 6000.0)
        self.assertEqual(v12.REVIEWER_RESERVE, 2000.0)
        config = v12._config()
        self.assertFalse(config["stage1_authorized"])
        self.assertFalse(config["formal_owner_or_training_seed_consumed"])
        self.assertEqual(config["disposition"], "STUDY_ONLY")


if __name__ == "__main__":
    unittest.main()
