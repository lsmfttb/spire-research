from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from prospective_cost_regime import CostRegimeError
from bounded_batch02_sampling import (
    LOCAL_LIMIT,
    TARGET_SEEDS,
    _batch02_seed_schedule,
    _r2_source_compatibility,
    _remaining_local_budget,
)


class BoundedBatch02SamplingTests(unittest.TestCase):
    def test_dispatch_slice_is_exactly_the_authorized_original_keys(self) -> None:
        schedule = _batch02_seed_schedule()
        self.assertEqual(schedule[:128], tuple(range(660001, 660129)))
        self.assertEqual(schedule[128:256], TARGET_SEEDS)
        self.assertEqual(TARGET_SEEDS, tuple(range(660243, 660257)))
        self.assertNotIn(660239, schedule[128:256])
        self.assertNotIn(660241, schedule[128:256])

    def test_local_cpu_limit_keeps_setup_and_finalization_reserves_inside_cap(self) -> None:
        remaining = _remaining_local_budget(charged=200.0, setup=25.0, finalization_reserve=30.0)
        self.assertEqual(remaining, LOCAL_LIMIT - 255.0)
        self.assertLessEqual(200.0 + 25.0 + remaining + 30.0, LOCAL_LIMIT)
        self.assertLessEqual(_remaining_local_budget(charged=1950.0, setup=20.0, finalization_reserve=30.0), 0.0)

    def test_r2_seal_accepts_only_the_committed_offline_test_descendant(self) -> None:
        import resource_amendment_r2
        import run_cost_bridge as bridge

        head = bridge.git(Path(__file__).resolve().parent, "rev-parse", "HEAD")
        witness = _r2_source_compatibility(resource_amendment_r2, head)
        self.assertEqual(witness["status"], "PASS")
        self.assertTrue(witness["post_seal_offline_test_only_change"])
        self.assertTrue(witness["runtime_source_files_unchanged"])
        self.assertEqual(
            witness["changed_paths_since_r2_seal"],
            {"test_resource_amendment_r2.py": "M"},
        )

    def test_source_rollover_requires_only_no_dispatch_attempts(self) -> None:
        import bounded_batch02_sampling as sampling

        expected_hashes = {660243: "a" * 64, 660244: "b" * 64}
        previous = {
            "status": "FAILED",
            "executing_head": "old-head",
            "launcher_sha256": "old-launcher",
            "attempts": [{
                "attempt_id": "attempt-1",
                "status": "FAILED_BEFORE_DISPATCH",
                "stage_receipt_path": "missing-stage.json",
                "completed_games": [],
                "complete_keys": [],
                "pre_dispatch_failure_recovery": {
                    "preserved_original_incomplete_attempts": [
                        {"game_seed": seed, "status_json_sha256": digest}
                        for seed, digest in expected_hashes.items()
                    ],
                },
            }],
        }
        target_state = {"complete": [], "keys": [
            {"game_seed": seed, "attempts": [{
                "status": "INCOMPLETE", "run_json_exists": False,
                "status_json_sha256": digest,
            }]}
            for seed, digest in expected_hashes.items()
        ] + [{"game_seed": seed, "attempts": []} for seed in TARGET_SEEDS if seed not in expected_hashes]}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sampling, "HERE", Path(tmp)):
            rollover = sampling._no_dispatch_source_rollover(
                previous, target_state, head="new-head", launcher_sha256="new-launcher",
            )
        self.assertEqual(rollover["new_dispatches_before_rollover"], 0)
        self.assertEqual(rollover["preserved_attempt_ids"], ["attempt-1"])

        previous["attempts"][0]["status"] = "RESOURCE_BLOCKED"
        previous["attempts"][0]["stage_receipt_path"] = "blocked-stage.json"
        previous["attempts"][0]["parallel_stage_receipt"] = {
            "execution_count": 0, "worker_process_cpu_seconds": 0.0,
        }
        previous["attempts"][0].pop("pre_dispatch_failure_recovery", None)
        previous["attempts"][0]["pre_dispatch_failure_recovery"] = {
            "preserved_original_incomplete_attempts": [
                {"game_seed": seed, "status_json_sha256": digest}
                for seed, digest in expected_hashes.items()
            ],
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sampling, "HERE", Path(tmp)):
            (Path(tmp) / "blocked-stage.json").write_text(
                '{"execution_count":0,"executions":[]}', encoding="utf-8",
            )
            rollover = sampling._no_dispatch_source_rollover(
                previous, target_state, head="new-head", launcher_sha256="new-launcher",
            )
        self.assertEqual(rollover["new_dispatches_before_rollover"], 0)

        previous["attempts"][0]["parallel_stage_receipt"]["worker_process_cpu_seconds"] = 0.5
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sampling, "HERE", Path(tmp)):
            (Path(tmp) / "blocked-stage.json").write_text(
                '{"execution_count":0,"executions":[]}', encoding="utf-8",
            )
            with self.assertRaises(CostRegimeError):
                sampling._no_dispatch_source_rollover(
                    previous, target_state, head="new-head", launcher_sha256="new-launcher",
                )


if __name__ == "__main__":
    unittest.main()
