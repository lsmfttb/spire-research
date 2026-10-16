"""Focused offline checks for the Issue 60 resource-only R1 carry-over."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import unittest

from resource_amendment_r1 import (
    ORIGINAL_CONFIG_SHA256, PARENT_HANDOFF, ResourceAmendmentError,
    _check_config_identity, _git, _git_bytes, _study_git_path,
    assert_resource_projection, verify_runner_whitelist,
)


HERE = Path(__file__).resolve().parent


class ResourceAmendmentR1Tests(unittest.TestCase):
    def test_runner_diff_contains_only_approved_resource_gates(self) -> None:
        producer = _git(HERE, "rev-parse", "HEAD")
        original = _git_bytes(HERE, "show", f"{PARENT_HANDOFF}:{_study_git_path(HERE, 'run_study.py')}")
        amended = (HERE / "run_study.py").read_bytes()
        self.assertEqual(len(producer), 40)
        verify_runner_whitelist(original, amended)

    def test_scientific_config_edit_is_rejected(self) -> None:
        path = HERE / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["training"]["batches_per_block"] += 1
        changed = (json.dumps(config, sort_keys=True, indent=2) + "\n").encode()
        self.assertNotEqual(ORIGINAL_CONFIG_SHA256, hashlib.sha256(changed).hexdigest())
        with self.assertRaises(ResourceAmendmentError):
            _check_config_identity(changed)

    def test_authorized_budget_projection_preserves_all_multipliers(self) -> None:
        old = json.loads((HERE / "evidence" / "preflight" / "native.json").read_text(encoding="utf-8"))
        self.assertEqual(old["status"], "RESOURCE_BLOCKED")
        accounted = float(old["preflight_and_implementation_accounted_cpu_seconds"]) + 400.0
        projection = assert_resource_projection(old, accounted)
        self.assertLess(projection["projected_builder_cpu_seconds"], 180000.0)
        self.assertLess(projection["all_in_projected_cpu_seconds"], 200000.0)
        self.assertEqual(projection["reviewer_reserved_cpu_seconds"], 20000.0)
        self.assertEqual(projection["future_reviewer_reserve_cpu_seconds"], 19000.0)


if __name__ == "__main__":
    unittest.main()
