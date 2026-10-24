from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runner
import stage0


STUDY = Path(__file__).resolve().parent
ROOT_CONFIG_SHA256 = "3393c326e62b3f2ef15883de95ddc6ca9379c4ca7008fd35251fc57292e1e76c"


def root_record(config_sha256: str) -> dict[str, object]:
    return {
        "schema_id": "issue73-native-debug-root-v1",
        "root_id": "root-test-0000",
        "config_sha256": config_sha256,
        "owner_run_id": "run-test",
        "owner_root_index": 0,
        "owner_seed": 1,
        "owner_decision_index": 0,
        "policy_decision_index": 0,
        "screen": "EVENT_SCREEN",
        "root_fingerprints": {
            "native_state_sha256": "1" * 64,
            "public_projection_sha256": "2" * 64,
            "public_resource_state_sha256": "3" * 64,
        },
        "projection_digest": "4" * 64,
        "raw_privileged_projection": {},
        "sanitized_public_projection": {},
        "source_tag_mapping": {},
        "legal_action_occurrences": [],
        "selected_owner_occurrence": 0,
        "semantic_feature_matrix": [],
        "feature_matrix_sha256": "5" * 64,
        "candidate_binding_validation_passed": False,
        "native_checkpoint": {"status": "NOT_SERIALIZED"},
    }


class Stage0RootConfigProvenanceTests(unittest.TestCase):
    def load_fixture(self, config_sha256: str) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(dir=STUDY, prefix=".root-config-test-") as temp:
            root_dir = Path(temp) / "roots"
            root_dir.mkdir()
            (root_dir / "root-test-0000.json").write_text(
                json.dumps(root_record(config_sha256)), encoding="utf-8"
            )
            with mock.patch.object(stage0, "OUT", Path(temp)):
                return stage0.load_roots()

    def test_historical_root_producer_config_digest_is_explicitly_locked(self) -> None:
        self.assertEqual(
            runner.CONFIG["identity"]["historical_root_config_sha256s"],
            [ROOT_CONFIG_SHA256],
        )

    def test_accepts_root_with_the_locked_historical_producer_digest(self) -> None:
        rows = self.load_fixture(ROOT_CONFIG_SHA256)
        self.assertEqual([row["root_id"] for row in rows], ["root-test-0000"])

    def test_accepts_root_created_under_the_current_config(self) -> None:
        rows = self.load_fixture(runner.CONFIG_SHA256)
        self.assertEqual([row["root_id"] for row in rows], ["root-test-0000"])

    def test_rejects_unregistered_root_config_digest(self) -> None:
        with self.assertRaisesRegex(stage0.Stage0Failure, "config lock mismatch"):
            self.load_fixture("f" * 64)

    def test_current_followup_metadata_matches_gate_a_lock_and_wrapper(self) -> None:
        gate_root = STUDY / "receipts" / "gate-a-requalification-v6"
        preflight_root = STUDY / "receipts" / "gate-a-requalification-v3-preflight"
        active_gate_a_lock_path = gate_root / "source-lock.json"
        active_gate_a_lock = json.loads(active_gate_a_lock_path.read_text(encoding="utf-8"))
        preflight_version = active_gate_a_lock["preflight_version"]
        preflight_lock_path = preflight_root / f"source-lock-v{preflight_version}.json"
        gate_a_snapshot_path = gate_root / f"source-lock-v{preflight_version}.json"
        preflight_lock = json.loads(preflight_lock_path.read_text(encoding="utf-8"))
        gate_a_snapshot = json.loads(gate_a_snapshot_path.read_text(encoding="utf-8"))
        snapshot_path = f"receipts/gate-a-requalification-v6/source-lock-v{preflight_version}.json"

        self.assertEqual(preflight_lock["preflight_version"], preflight_version)
        self.assertEqual(preflight_lock["gate_a_source_lock_snapshot_path"], snapshot_path)
        self.assertEqual(
            gate_a_snapshot["preflight_source_lock_path"],
            f"receipts/gate-a-requalification-v3-preflight/source-lock-v{preflight_version}.json",
        )
        lock_sha256 = hashlib.sha256(
            preflight_lock_path.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        self.assertEqual(gate_a_snapshot["preflight_source_lock_sha256"], lock_sha256)
        self.assertEqual(
            active_gate_a_lock["preflight_source_lock_path"],
            gate_a_snapshot["preflight_source_lock_path"],
        )
        self.assertEqual(active_gate_a_lock["preflight_source_lock_sha256"], lock_sha256)
        self.assertEqual(active_gate_a_lock["attempt_key"], gate_a_snapshot["attempt_key"])
        self.assertEqual(active_gate_a_lock["resource_reservation"], gate_a_snapshot["resource_reservation"])
        self.assertEqual(active_gate_a_lock["source_code_commit"], gate_a_snapshot["source_code_commit"])

        reservation = gate_a_snapshot["resource_reservation"]
        expected_followup = {
            "attempt_key": gate_a_snapshot["attempt_key"],
            "fixed_cpu_reservation_seconds": reservation["fixed_cpu_seconds"],
            "status": reservation["status"],
            "start_allowed_only_after_independent_review_pass": True,
            "gate_a_native": False,
            "formal_seed_consumed": False,
            "stage1_authorized": False,
        }
        self.assertEqual(preflight_lock["gate_a_followup"], expected_followup)
        self.assertEqual(
            preflight_lock["attempt_key"],
            gate_a_snapshot["preflight_attempt_key"],
        )

        wrapper = STUDY / gate_a_snapshot["execution_wrapper_path"]
        wrapper_text = wrapper.read_text(encoding="utf-8")
        attempt_path_match = re.search(
            r'(?m)^attempt_path = ATTEMPT_ROOT / "attempts" / "([^"]+)"$',
            wrapper_text,
        )
        self.assertIsNotNone(attempt_path_match)
        self.assertEqual(
            attempt_path_match.group(1),
            Path(gate_a_snapshot["attempt_path"]).name,
        )
        self.assertIn(
            f'source_lock.get("attempt_key") != "{gate_a_snapshot["attempt_key"]}"',
            wrapper_text,
        )


if __name__ == "__main__":
    unittest.main()
