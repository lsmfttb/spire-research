"""Pure source-bound executor checks; these tests never construct a simulator."""
from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import types
import unittest
from unittest import mock

STUDY = Path(__file__).resolve().parents[1]
STAGE1 = STUDY / "stage1"
sys.path.insert(0, str(STAGE1))
sys.path.insert(1, str(STUDY))

import executor


class FakeLedger:
    def __init__(self, *, unit_kind: str):
        self.activation = types.SimpleNamespace(
            package_commit="a" * 40,
            source_lock_sha256="b" * 64,
        )
        self.unit_kind = unit_kind

    def require_open_reservation(self, *, run_id: str, unit_id: str):
        return {
            "run_id": run_id,
            "unit_id": unit_id,
            "unit_kind": self.unit_kind,
            "previous_run_id": None,
            "checkpoint_sha256": None,
        }


class FakeReportLedger:
    def __init__(self):
        self.activation = types.SimpleNamespace(
            package_commit="a" * 40,
            source_lock_sha256="b" * 64,
        )
        self.events = []

    def read_events(self):
        return list(self.events)

    def reserve(self, **event):
        self.events.append({"event": "reserve", **event})

    def finish(self, *, run_id: str, measured_cpu_seconds: float, status: str, checkpoint_sha256=None):
        self.events.append({
            "event": "finish",
            "run_id": run_id,
            "measured_cpu_seconds": measured_cpu_seconds,
            "status": status,
            "checkpoint_sha256": checkpoint_sha256,
        })


class Stage1ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=STAGE1)
        self.addCleanup(self.temporary.cleanup)
        self.original_artifact_root = executor.ARTIFACT_ROOT
        executor.ARTIFACT_ROOT = Path(self.temporary.name) / "artifacts"
        self.addCleanup(setattr, executor, "ARTIFACT_ROOT", self.original_artifact_root)

    def test_paired_owner_differences_bind_all_96_owner_pairs(self):
        rows = []
        for owner in range(96):
            for arm in ("J", "P1"):
                rows.append({
                    "arm": arm,
                    "paired_owner_key": f"heldout-{owner}",
                    "terminal_floor": owner + (1 if arm == "J" else 0),
                })
        rows.reverse()
        self.assertEqual(executor._paired_differences(rows, "J", "P1", "terminal_floor"), [1.0] * 96)
        with self.assertRaises(PermissionError):
            executor._paired_differences(rows[:-1], "J", "P1", "terminal_floor")

    def test_bootstrap_is_deterministic_and_reports_nearest_rank_interval(self):
        seed = "c" * 64
        first = executor._bootstrap_ci_95([2.0] * 96, seed_sha256=seed, replicates=200)
        second = executor._bootstrap_ci_95([2.0] * 96, seed_sha256=seed, replicates=200)
        self.assertEqual(first, second)
        self.assertEqual((first["lower"], first["upper"]), (2.0, 2.0))
        self.assertEqual(first["replicates"], 200)
        formal_shape = executor._bootstrap_ci_95([2.0] * 96, seed_sha256=seed)
        self.assertEqual(formal_shape["replicates"], 20_000)
        self.assertEqual((formal_shape["lower"], formal_shape["upper"]), (2.0, 2.0))

    def test_search_coverage_counts_mutation_depth_and_actual_action_changes(self):
        outcomes = [{
            "arm": "J",
            "search": [{
                "status": "MUTATION_ATTEMPTED",
                "started_attempts": 3,
                "completed_trajectories": 2,
                "cpu_seconds": 4.5,
                "root_action_changed_from_sampled_policy": True,
                "mutation_attempts": [
                    {"parent_decision_index": 1, "complete_terminal": True},
                    {"parent_decision_index": 2, "complete_terminal": False},
                ],
            }],
            "resource": {"measured_process_tree_cpu_seconds": 5.5, "status": "succeeded"},
        }]
        summary = executor._search_coverage(outcomes)["J"]
        self.assertEqual(summary["root_opportunities_searched"], 1)
        self.assertEqual(summary["mutation_attempts_started"], 2)
        self.assertEqual(summary["mutation_terminal_trajectories"], 1)
        self.assertEqual(summary["mutation_at_or_after_second_future_choice"], 1)
        self.assertEqual(summary["root_actions_changed_from_sampled_policy"], 1)
        self.assertEqual(summary["unit_process_tree_cpu_seconds"], 5.5)

    def test_report_analysis_is_owner_paired_and_resumed_from_its_receipt(self):
        outcomes = []
        for owner in range(96):
            for arm_index, arm in enumerate(executor.ARMS):
                outcomes.append({
                    "unit_id": f"HELDOUT:{owner}:{arm}",
                    "owner_seed": owner,
                    "arm": arm,
                    "paired_owner_key": f"heldout-{owner}",
                    "terminal_floor": float(owner + arm_index),
                    "heart_victory": (owner + arm_index) % 7 == 0,
                    "search": [],
                    "resource": {"measured_process_tree_cpu_seconds": 0.25, "status": "succeeded"},
                })
        ledger = FakeReportLedger()
        first = executor._run_report_analysis(ledger, outcomes)
        second = executor._run_report_analysis(ledger, outcomes)
        self.assertEqual(first, second)
        self.assertEqual(sum(event["event"] == "reserve" for event in ledger.events), 1)
        self.assertEqual(first["analysis"]["paired_terminal_floor_contrasts"]["J-P1"]["n"], 96)
        self.assertEqual(
            first["analysis"]["paired_terminal_floor_contrasts"]["J-P1"]["terminal_floor_bootstrap_ci_95"]["replicates"],
            20_000,
        )
        self.assertEqual(first["analysis"]["search_coverage_and_resource_summary"]["P0"]["owner_rows"], 96)

    def test_worker_accepts_only_the_exact_registered_owner_row(self):
        row = next(item for item in executor._rows() if item["phase"] == "preflight")
        unit_id = row["unit_id"]
        run_id = unit_id + "-" + "d" * 16
        ledger = FakeLedger(unit_kind="owner_arm")
        unit_directory = executor._unit_directory(unit_id)
        job_path = unit_directory / f"worker-job-{run_id}.json"
        job_path.parent.mkdir(parents=True)
        job = {
            "schema_id": executor.WORKER_SCHEMA,
            "package_commit": ledger.activation.package_commit,
            "source_lock_sha256": ledger.activation.source_lock_sha256,
            "run_id": run_id,
            "unit_id": unit_id,
            "phase": "preflight",
            "row": row,
            "output_dir": str(unit_directory / "native"),
            "result_path": str(unit_directory / "result.json"),
            "previous_run_id": None,
            "checkpoint_sha256": None,
        }
        job_path.write_text(json.dumps(job), encoding="utf-8")
        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "open_activated_stage1_ledger", return_value=ledger),
            mock.patch.object(executor, "_worker_owner", return_value={"status": "SUCCEEDED", "unit_id": unit_id}) as worker,
        ):
            self.assertEqual(executor._worker_main(job_path), 0)
            worker.assert_called_once()
        self.assertTrue((unit_directory / "result.json").is_file())

        job["row"] = {**row, "owner_seed": int(row["owner_seed"]) + 1}
        job_path.write_text(json.dumps(job), encoding="utf-8")
        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "open_activated_stage1_ledger", return_value=ledger),
            mock.patch.object(executor, "_worker_owner") as worker,
        ):
            with self.assertRaises(PermissionError):
                executor._worker_main(job_path)
            worker.assert_not_called()

    def test_update_worker_is_bound_to_its_exact_block_and_result_path(self):
        unit_id = "UPDATE:0"
        run_id = unit_id + "-" + "e" * 16
        ledger = FakeLedger(unit_kind="training_update")
        unit_directory = executor._unit_directory(unit_id)
        job_path = unit_directory / f"worker-job-{run_id}.json"
        job_path.parent.mkdir(parents=True)
        result_path = executor._inside_artifacts(
            executor.ARTIFACT_ROOT / "units" / "UPDATE-0" / f"result-{run_id}.json"
        )
        job = {
            "schema_id": executor.WORKER_SCHEMA,
            "package_commit": ledger.activation.package_commit,
            "source_lock_sha256": ledger.activation.source_lock_sha256,
            "run_id": run_id,
            "unit_id": unit_id,
            "phase": "update",
            "block_index": 0,
            "previous_run_id": None,
            "checkpoint_sha256": None,
            "result_path": str(result_path),
        }
        job_path.write_text(json.dumps(job), encoding="utf-8")
        def fake_worker_update(worker_job, _ledger):
            result = {"status": "SUCCEEDED", "unit_id": unit_id}
            executor._write_once(Path(worker_job["result_path"]), result)
            return result

        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "open_activated_stage1_ledger", return_value=ledger),
            mock.patch.object(executor, "_worker_update", side_effect=fake_worker_update) as worker,
        ):
            self.assertEqual(executor._worker_main(job_path), 0)
            worker.assert_called_once()
        self.assertTrue(result_path.is_file())

        job["block_index"] = 1
        job_path.write_text(json.dumps(job), encoding="utf-8")
        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "open_activated_stage1_ledger", return_value=ledger),
            mock.patch.object(executor, "_worker_update") as worker,
        ):
            with self.assertRaises(PermissionError):
                executor._worker_main(job_path)
            worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
