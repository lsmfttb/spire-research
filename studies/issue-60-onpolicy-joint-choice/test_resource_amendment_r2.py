"""Focused offline checks for the Issue 60 resource-only R2 amendment."""
from __future__ import annotations

from pathlib import Path
import unittest
import tempfile
from unittest import mock

from resource_amendment_r2 import (
    BUILDER_BUDGET_SECONDS, ResourceBudgetExceeded, build_forecast,
    complete_game_costs, cpu_limit_values, enforce_budget,
    remaining_optimizer_projection, upper_mean_plus_three_se,
    wrap_before_decision, wrap_minibatch_loss,
)


HERE = Path(__file__).resolve().parent


class ResourceAmendmentR2Tests(unittest.TestCase):
    def test_resource_gate_stage_persists_gate_before_reporting_pass(self) -> None:
        import run_study_r2

        result = {"status": "PASS", "decision": "continue the unchanged fixed grid"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.object(run_study_r2, "HERE", root),
                mock.patch.object(run_study_r2, "install_process_cpu_limit", return_value={}),
                mock.patch.object(run_study_r2, "write_resource_gate", return_value=result) as write_gate,
                mock.patch.object(run_study_r2, "_write_json"),
                mock.patch.object(run_study_r2, "_append_ledger"),
            ):
                status = run_study_r2._run_r2_receipt_stage("resource-r2", stage_start_cpu=0.0)
        self.assertEqual(status, 0)
        write_gate.assert_called_once_with(root, stage_started_cpu=0.0)

    def test_reference_54_game_history_matches_planner_arithmetic(self) -> None:
        rows = complete_game_costs(HERE)
        self.assertEqual(len(rows), 54)
        costs = [float(row["process_cpu_seconds"]) for row in rows]
        stats = upper_mean_plus_three_se(costs)
        self.assertAlmostEqual(stats["mean"], 11.808997913759262, places=9)
        self.assertAlmostEqual(stats["sample_sd"], 11.54912410040899, places=9)
        self.assertAlmostEqual(stats["upper"], 16.523908084106232, places=9)
        self.assertEqual(upper_mean_plus_three_se(costs[-128:]), stats)

    def test_recent_window_responds_to_cost_growth(self) -> None:
        costs = [10.0] * 200 + [100.0] * 128
        all_history = upper_mean_plus_three_se(costs)
        recent = upper_mean_plus_three_se(costs[-128:])
        self.assertLess(all_history["upper"], recent["upper"])
        result = build_forecast(
            costs=costs, remaining_games=100, remaining_optimizer_cpu=0,
            accounted_cpu=0,
        )
        self.assertEqual(result["unit_cost_cpu_seconds"], 100.0)
        self.assertEqual(result["status"], "PASS")

    def test_optimizer_forecast_retains_the_existing_dynamic_max_choice_rule(self) -> None:
        projection = remaining_optimizer_projection(HERE)
        self.assertEqual(projection["max_supported_choices"], 69)
        self.assertEqual(projection["remaining_optimizer_minibatches"], 3328)
        self.assertAlmostEqual(
            projection["remaining_optimizer_cpu_seconds"],
            3328 * 5.375514805000009 * 1.5, places=8,
        )

    def test_actual_budget_exhaustion_stops_before_work(self) -> None:
        with self.assertRaises(ResourceBudgetExceeded):
            enforce_budget(
                accounted_cpu=BUILDER_BUDGET_SECONDS, current_stage_cpu=0.1,
                costs=[1.0], remaining_games=0, remaining_optimizer_cpu=0,
            )

    def test_process_cpu_limit_accounts_for_current_process_usage(self) -> None:
        soft, hard = cpu_limit_values(8.25, 100.0)
        self.assertEqual((soft, hard), (78, 108))
        with self.assertRaises(ResourceBudgetExceeded):
            cpu_limit_values(20.0, 1.0)

    def test_budget_hooks_preserve_returns_and_do_not_call_blocked_work(self) -> None:
        checks = []
        decision = lambda *args, **kwargs: ("decision", args, kwargs)
        guarded_decision = wrap_before_decision(
            decision, lambda: checks.append("before-decision")
        )
        self.assertEqual(guarded_decision(1, flag=True), decision(1, flag=True))
        self.assertEqual(checks, ["before-decision"])

        updates = []
        loss = lambda *args, **kwargs: (sum(args), kwargs.get("scale", 1))
        guarded_loss = wrap_minibatch_loss(
            loss, lambda: checks.append("before-minibatch"),
            lambda: updates.append("complete"),
        )
        self.assertEqual(guarded_loss(2, 3, scale=4), loss(2, 3, scale=4))
        self.assertEqual(updates, ["complete"])

        def blocked() -> None:
            raise ResourceBudgetExceeded("resource stop")

        with self.assertRaises(ResourceBudgetExceeded):
            wrap_minibatch_loss(loss, blocked, lambda: updates.append("invalid"))(2, 3)
        self.assertEqual(updates, ["complete"])

    def test_resource_stop_is_not_misrecorded_as_a_game_failure(self) -> None:
        try:
            raise ResourceBudgetExceeded("stop")
        except Exception:
            self.fail("resource stop must bypass run_game's ordinary exception handler")
        except ResourceBudgetExceeded:
            pass


if __name__ == "__main__":
    unittest.main()
