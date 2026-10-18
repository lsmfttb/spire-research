"""Focused control/accounting checks for the Issue 60 production entry."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import production_entry as production


class ProductionEntryTests(unittest.TestCase):
    def test_initial_checkpoint_routes_only_to_integration_milestone(self):
        self.assertEqual(
            production.execution_phase(1, {"route:builder"}, [], "base"),
            "integration-milestone",
        )

    def test_batch02_checkpoint_waits_for_exact_reviewed_head(self):
        labels = {"route:builder"}
        comments = [{"body": "issue60-integration-review-v1: PASS abc123"}]
        self.assertEqual(production.execution_phase(2, labels, comments, "different"), "awaiting-review")
        self.assertEqual(production.execution_phase(2, labels, comments, "abc123"), "full-continuation")
        self.assertEqual(production.execution_phase(2, {"route:reviewer"}, comments, "abc123"), "awaiting-review")

    def test_inflight_game_and_optimizer_reserves_are_included(self):
        self.assertTrue(production.can_dispatch_with_reserves(
            actual_cpu=100.0, cpu_limit=3000.0, active_or_pending_workers=2,
            game_reserve=400.0, optimizer_reserve=1200.0, parent_reserve=60.0,
        ))
        self.assertFalse(production.can_dispatch_with_reserves(
            actual_cpu=500.0, cpu_limit=3000.0, active_or_pending_workers=3,
            game_reserve=500.0, optimizer_reserve=1200.0, parent_reserve=60.0,
        ))

    def test_integration_worker_selection_accounts_for_reserves_and_uses_small_cap(self):
        workers = production.select_milestone_worker_count(
            preferred_workers=8, actual_cpu=275.0, game_reserve=459.4,
            optimizer_reserve=1144.3, parent_reserve=60.0,
        )
        self.assertEqual(workers, 2)
        self.assertTrue(production.can_dispatch_with_reserves(
            actual_cpu=275.0, cpu_limit=3000.0, active_or_pending_workers=workers,
            game_reserve=459.4, optimizer_reserve=1144.3, parent_reserve=60.0,
        ))

    def test_interrupted_reservations_become_debits_without_changing_measured_cpu(self):
        with tempfile.TemporaryDirectory() as temporary:
            progress_path = Path(temporary) / "production-progress.json"
            with patch.object(production, "PROGRESS_PATH", progress_path):
                first = production.ProductionProgress(Path(temporary), config_sha="config")
                first.data["measured_new_cpu_seconds"] = 25.0
                first.data["historical_admin_debit_cpu_seconds"] = 1_572.370806919
                first.data["open_invocation"] = {"reservation_seconds": 60.0}
                first.data["open_optimizer_reservation"] = {"reservation_seconds": 900.0}
                first.data["open_worker_reservations"] = {
                    "[\"owner-001\",669001,\"A\"]": {
                        "reservation_seconds": 300.0, "task_key": ["owner-001", 669001, "A"],
                    }
                }
                first.persist()

                resumed = production.ProductionProgress(Path(temporary), config_sha="config")
                self.assertEqual(resumed.data["measured_new_cpu_seconds"], 25.0)
                self.assertEqual(resumed.data["administrative_debit_cpu_seconds"], 1260.0)
                self.assertEqual(resumed.data["accounted_new_cpu_seconds"], 1285.0)
                self.assertEqual(resumed.data["historical_admin_debit_cpu_seconds"], 1_572.370806919)
                self.assertIsNone(resumed.data["open_invocation"])
                self.assertIsNone(resumed.data["open_optimizer_reservation"])
                self.assertEqual(resumed.data["open_worker_reservations"], {})


if __name__ == "__main__":
    unittest.main()
