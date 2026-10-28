from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import driver


class SuccessorDriverTests(unittest.TestCase):
    def test_seed_table_is_fixed_and_matches_registered_cohorts(self):
        table = driver.make_seed_table()
        self.assertEqual(table, driver.make_seed_table())
        rows = table["rows"]
        self.assertEqual(len(rows), 900)
        self.assertEqual(len({row["unit_id"] for row in rows}), 900)
        self.assertEqual(sum(row["phase"] == "training" and not row["formal"] for row in rows), 16)
        self.assertEqual(sum(row["phase"] == "heldout" and not row["formal"] for row in rows), 20)
        formal_training = [row for row in rows if row["phase"] == "training" and row["formal"]]
        self.assertEqual(len(formal_training), 384)
        self.assertEqual([sum(row["block_index"] == block for row in formal_training)
                          for block in (1, 2, 3)], [128, 128, 128])
        formal_eval = [row for row in rows if row["phase"] == "heldout" and row["formal"]]
        self.assertEqual(len(formal_eval), 480)
        self.assertEqual({row["arm"] for row in formal_eval}, set(driver.ARMS))
        self.assertTrue(all(len(row["search_invocation_seeds"]) == 7
                            for row in formal_eval if row["arm"] in {"R", "J"}))
        self.assertTrue(all(not row["search_invocation_seeds"]
                            for row in formal_eval if row["arm"] not in {"R", "J"}))

    def test_complete_paired_bootstrap_and_incomplete_pairing(self):
        rows = [
            {"owner_seed": owner, "arm": arm, "terminal_floor": owner + (2 if arm == "J" else 0)}
            for owner in driver.FORMAL_EVALUATION
            for arm in driver.ARMS
        ]
        result = driver._paired_bootstrap(rows, "J", "P1", "terminal_floor")
        self.assertEqual(result["paired_owner_count"], 96)
        self.assertEqual(result["mean_difference"], 2.0)
        self.assertEqual(result["ci95"], [2.0, 2.0])
        self.assertEqual(result, driver._paired_bootstrap(rows, "J", "P1", "terminal_floor"))
        missing = [row for row in rows if not (row["owner_seed"] == driver.FORMAL_EVALUATION[0]
                                                and row["arm"] == "P1")]
        partial = driver._paired_bootstrap(missing, "J", "P1", "terminal_floor")
        self.assertEqual(partial["paired_owner_count"], 95)
        self.assertIsNone(partial["mean_difference"])
        self.assertIsNone(partial["ci95"])

    def test_search_coverage_reports_started_and_completed_work(self):
        summary = driver._search_coverage([{
            "search": [{
                "started_attempts": 16, "completed_trajectories": 13,
                "mutation_attempts_started": 3,
                "root_action_changed_from_sampled_policy": True, "cpu_seconds": 4.5,
            }],
            "resource": {"worker_process_cpu_seconds": 8.0, "worker_wall_seconds": 5.0},
        }])
        self.assertEqual(summary["root_opportunities"], 1)
        self.assertEqual(summary["started_proposals"], 16)
        self.assertEqual(summary["completed_terminal_proposals"], 13)
        self.assertEqual(summary["later_mutations_started"], 3)
        self.assertEqual(summary["first_action_switches"], 1)
        self.assertEqual(summary["total_search_process_tree_cpu_seconds"], 4.5)

    def test_completed_pilot_phase_repairs_progress_without_repeating_work(self):
        with tempfile.TemporaryDirectory(dir=driver.STUDY) as temp:
            artifacts = Path(temp)
            commit = "a" * 40
            receipt = {
                "package_commit": commit, "status": "SUCCEEDED",
                "elapsed_active_seconds": 12.5, "finished_utc": "2026-10-28T00:00:00Z",
            }
            with patch.object(driver, "ARTIFACTS", artifacts):
                driver.write_json_once(artifacts / f"pilot-phase-{commit}-training.json", receipt)
                progress_path = artifacts / f"pilot-phase-{commit}-training-progress.json"
                driver.write_json_once(progress_path, {"status": "ACTIVE", "phase": "training"})
                result = driver._pilot_phase(
                    "training", object(), lambda: self.fail("completed phase was rerun"), commit,
                )
                self.assertEqual(result, receipt)
                self.assertEqual(driver.read_json(progress_path)["status"], "COMPLETE")

    def test_recovery_can_load_success_from_its_original_execution_commit(self):
        with tempfile.TemporaryDirectory(dir=driver.STUDY) as temp:
            root = Path(temp)
            row = {
                "unit_id": "TRAIN:7620001", "owner_seed": 7620001,
                "phase": "training", "arm": "TRAIN", "formal": False,
            }
            commit = "a" * 40
            attempt = root / "units" / driver.unit_key(row["unit_id"]) / "attempt-0001"
            attempt.mkdir(parents=True)
            result = {
                **row, "status": "SUCCEEDED", "package_commit": commit,
                "source_lock_sha256": driver.source_lock_sha(),
            }
            driver.write_json_once(attempt / "result.json", result)
            with patch.object(driver, "source_commit", return_value="b" * 40):
                self.assertIsNone(driver.existing_success(root, row))
                self.assertEqual(driver.existing_success(root, row, commit), result)

    def test_search_wall_distribution_reads_pilot_search_artifact_layout(self):
        with tempfile.TemporaryDirectory(dir=driver.STUDY) as temp:
            root = Path(temp)
            for index, duration in enumerate((2.5, 7.0), start=1):
                path = root / "search" / f"root-{index}" / f"attempt-{index:02d}-result.json.zlib"
                path.parent.mkdir(parents=True, exist_ok=True)
                value = {"result": {"complete": True, "wall_seconds": duration}}
                driver.write_once(path, driver.zlib.compress(driver.canonical(value)))
            self.assertEqual(driver._attempt_wall_seconds(root), [2.5, 7.0])
            report1, policy1 = driver._pilot_artifact_paths("a" * 40, "b" * 40)
            report2, policy2 = driver._pilot_artifact_paths("a" * 40, "c" * 40)
            self.assertNotEqual(report1, report2)
            self.assertNotEqual(policy1, policy2)

    def test_worker_concurrency_uses_completed_half_open_intervals(self):
        rows = [{"unit_id": unit_id} for unit_id in ("TRAIN-A", "TRAIN-B", "TRAIN-C")]
        with tempfile.TemporaryDirectory(dir=driver.STUDY) as temp:
            root = Path(temp)
            intervals = {"TRAIN-A": (1.0, 4.0), "TRAIN-B": (2.0, 4.0), "TRAIN-C": (4.0, 6.0)}
            for unit_id, (start, finish) in intervals.items():
                attempt = root / "units" / driver.unit_key(unit_id) / "attempt-0001"
                attempt.mkdir(parents=True)
                driver.write_json_once(attempt / "started.json", {"started_monotonic": start})
                driver.write_json_once(attempt / "finished.json", {"finished_monotonic": finish})
            self.assertEqual(driver._max_concurrent_workers(root, rows), 2)

    def test_immutable_writer_is_resumable_but_never_overwrites(self):
        with tempfile.TemporaryDirectory(dir=driver.STUDY) as temp:
            path = Path(temp) / "receipt.json"
            self.assertEqual(driver.write_once(path, b"first"), driver.sha256(b"first"))
            self.assertEqual(driver.write_once(path, b"first"), driver.sha256(b"first"))
            with self.assertRaises(FileExistsError):
                driver.write_once(path, b"changed")
            self.assertEqual(path.read_bytes(), b"first")


if __name__ == "__main__":
    unittest.main()
