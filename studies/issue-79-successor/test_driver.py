from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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
