"""Focused, no-native tests for the fixed Stage-0 Gate B/C continuation."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import checkpoint_replay_qualification as q
import stage0_gate_bc_v10 as v10


class GateBCV10StaticTests(unittest.TestCase):
    def test_historical_parent_lock_uses_its_committed_blobs(self) -> None:
        parent_lock = v10.read_json(v10.e5.LOCK_PATH)
        committed = v10._verify_committed_source_hashes(
            parent_lock["code_source_commit"],
            parent_lock["study_source_sha256"],
            label="v9 engineering-5 test fixture",
        )
        self.assertEqual(committed, parent_lock["study_source_sha256"])
        current_config = v10.sha256_bytes((v10.STUDY / "config.json").read_bytes())
        self.assertNotEqual(current_config, committed["config.json"])

    def test_fixed_population_and_resource_projection(self) -> None:
        self.assertEqual(v10.GATE_B_ROOTS["hidden_semantic"], "root-173730001-0003")
        self.assertEqual(v10.GATE_C_ROOTS, tuple(f"root-173730001-{i:04d}" for i in range(4)))
        self.assertIn("test_stage0_gate_bc_v10.py", v10.SOURCE_PATHS)
        keys = v10._planned_suffix_keys()
        self.assertEqual(len(keys), 64)
        self.assertEqual(len(set(keys)), 64)

        projection = v10.resource_projection()
        self.assertTrue(projection["forecast_fits_registered_caps"])
        self.assertAlmostEqual(projection["charged_builder_cpu_seconds"], 4771.610134)
        self.assertAlmostEqual(projection["projected_builder_cpu_seconds"], 6171.610134)
        self.assertAlmostEqual(projection["projected_native_gate_bc_cpu_seconds"], 2980.0)
        self.assertAlmostEqual(projection["projected_aggregate_cpu_seconds_including_reviewer_reserve"], 8171.610134)

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
        with patch.object(v10, "read_json", return_value={"roots": [root]}), patch.object(
            v10.stage0, "source_tags", return_value=[]
        ):
            normalized = v10.locked_root(root["root_id"])

        compact = q.compact_root(normalized)
        expected_hash = v10.runner.sha256_bytes(v10.np.asarray(root["feature_matrix"], dtype="<f4").tobytes())
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
        clean = v10.audit_sanitized_projection(raw, {"screen": "MAP_SCREEN", "visible": "safe"})
        self.assertEqual(clean["private_feature_count"], 2)
        self.assertTrue(clean["all_private_names_and_values_absent"])

        leaked = v10.audit_sanitized_projection(raw, {"oracle_map_secret_marker": "private-sentinel-90173"})
        self.assertFalse(leaked["all_private_names_and_values_absent"])

    def test_scope_stays_study_only(self) -> None:
        self.assertEqual(v10.BUILDER_CAP, 10000.0)
        self.assertEqual(v10.AGGREGATE_CAP, 12000.0)
        self.assertEqual(v10.NATIVE_CAP, 6000.0)
        self.assertEqual(v10.REVIEWER_RESERVE, 2000.0)
        config = v10._config()
        self.assertFalse(config["stage1_authorized"])
        self.assertFalse(config["formal_owner_or_training_seed_consumed"])
        self.assertEqual(config["disposition"], "STUDY_ONLY")


if __name__ == "__main__":
    unittest.main()
