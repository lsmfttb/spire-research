from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
