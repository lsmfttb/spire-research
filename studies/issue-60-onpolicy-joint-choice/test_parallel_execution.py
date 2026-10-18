"""Focused tests for Issue 60 execution queue, CPU, and memory guards."""
from __future__ import annotations

import unittest
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

from parallel_execution import (
    aggregate_process_cpu, can_dispatch, choose_worker_count, BudgetAdmissionDeferred,
    ordered_result_items, ParallelGameDispatcher, ProcessCpuTracker,
    stage_result_status,
)


def _fake_run_game(**kwargs):
    time.sleep(0.05)
    output = Path(kwargs["output_dir"])
    attempt = output / "attempt-0001"
    attempt.mkdir(parents=True, exist_ok=True)
    summary = {
        "status": "COMPLETE", "model_sha256": kwargs["model_sha"],
        "config_sha256": kwargs["config_digest"], "game_seed": kwargs["seed"],
        "process_cpu_seconds": 0.01,
    }
    (attempt / "run.json").write_text(json.dumps(summary), encoding="utf-8")
    (attempt / "decisions.jsonl.gz").write_bytes(b"fixture")
    return attempt, summary


class ParallelExecutionTests(unittest.TestCase):
    def test_aggregate_cpu_counts_parent_live_and_reaped_once(self):
        value = aggregate_process_cpu(2.5, 3.25, 1250, 100, 2)
        self.assertAlmostEqual(value, 18.32)

    def test_reap_transition_is_conservative_and_does_not_lose_worker_cpu(self):
        active = aggregate_process_cpu(2.5, 3.25, 1000, 100, 1)
        reaped = aggregate_process_cpu(2.5, 13.25, 0, 100, 0)
        self.assertGreaterEqual(active, reaped)
        self.assertLessEqual(active - reaped, 0.02)

    def test_reap_transfers_worker_cpu_from_live_to_reaped_atomically(self):
        tracker = ProcessCpuTracker.start(time.process_time())
        tracker.register_worker(0, 999_999_999)
        self.assertEqual(tracker.active_worker_count(), 1)
        self.assertEqual(tracker.reap_worker(999_999_999, 2.5), 0)
        self.assertEqual(tracker.active_worker_count(), 0)
        with tracker.active_pids.get_lock():
            self.assertFalse(any(tracker.active_pids))
        with tracker.completed_cpu.get_lock():
            self.assertAlmostEqual(tracker.completed_cpu.value, 2.5)

    def test_dispatch_reserves_cpu_for_every_active_or_pending_game(self):
        self.assertTrue(can_dispatch(
            actual_cpu=1000, builder_budget=180_000,
            active_or_pending_workers=2, max_game_cpu=100,
        ))
        self.assertFalse(can_dispatch(
            actual_cpu=179_850, builder_budget=180_000,
            active_or_pending_workers=2, max_game_cpu=100,
        ))

    def test_resource_stop_status_is_not_overwritten_by_fallback(self):
        self.assertEqual(stage_result_status(75, "COMPLETE"), "RESOURCE_BLOCKED")
        self.assertEqual(stage_result_status(0, "FAILED"), "FAILED")
        self.assertEqual(stage_result_status(0, None), "COMPLETE")
        self.assertEqual(stage_result_status(2, None), "FAILED")

    def test_worker_capacity_respects_cpu_memory_reserve_and_eight_worker_cap(self):
        selected = choose_worker_count(
            effective_cpus=16, available_bytes=16 * 1024**3,
            worker_peak_rss_samples=[2 * 1024**3, 1_900_000_000], cap=8,
        )
        self.assertEqual(selected["worker_count"], 6)
        constrained = choose_worker_count(
            effective_cpus=2, available_bytes=3 * 1024**3,
            worker_peak_rss_samples=[2 * 1024**3], cap=8,
        )
        self.assertEqual(constrained["worker_count"], 1)
        capped = choose_worker_count(
            effective_cpus=64, available_bytes=128 * 1024**3,
            worker_peak_rss_samples=[256 * 1024**2], cap=8,
        )
        self.assertEqual(capped["worker_count"], 8)

    def test_results_follow_registered_order_not_completion_order(self):
        first = ("block1:batch-01", 660001, "residual")
        second = ("block1:batch-01", 660002, "residual")
        values = {second: "second", first: "first"}
        self.assertEqual(
            ordered_result_items([first, second], values),
            [(first, "first"), (second, "second")],
        )
        with self.assertRaises(ValueError):
            ordered_result_items([first, first], values)
        with self.assertRaises(ValueError):
            ordered_result_items([first, second], {first: "first"})

    @unittest.skipUnless(hasattr(os, "fork"), "dispatcher integration requires Linux fork")
    def test_two_worker_queue_settles_jobs_and_returns_registered_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def successful_attempt(directory, model_sha, config_digest):
                for path in sorted(directory.glob("attempt-*/run.json")):
                    row = json.loads(path.read_text(encoding="utf-8"))
                    if (row.get("status") == "COMPLETE"
                            and row.get("model_sha256") == model_sha
                            and row.get("config_sha256") == config_digest
                            and (path.parent / "decisions.jsonl.gz").is_file()):
                        return path.parent, row
                return None

            fake = SimpleNamespace(
                run_game=_fake_run_game, HERE=root,
                Vocabulary=lambda _path: None,
                successful_attempt=successful_attempt,
                budget_guard=lambda **_kwargs: None,
                _completed_run_count=lambda: 0,
                _remaining_optimizer_projection=lambda: 0.0,
            )
            tracker = ProcessCpuTracker.start(time.process_time())
            dispatcher = ParallelGameDispatcher(fake, tracker, 2)
            tasks = []
            for seed in (660001, 660002):
                key = ("validation", seed, "residual")
                tasks.append({
                    "key": key,
                    "kwargs": {
                        "seed": seed, "model": None, "vocab": None, "bootstrap": None,
                        "model_sha": "model", "config_digest": "config",
                        "output_dir": root / f"seed-{seed}",
                    },
                })
            results = dispatcher.run_jobs(tasks)
            self.assertEqual(dispatcher.max_active_workers, 2)
            self.assertEqual(list(results), [tuple(task["key"]) for task in tasks])
            self.assertEqual([row[1]["game_seed"] for row in results.values()], [660001, 660002])
            self.assertGreater(tracker.current_stage_cpu(), 0.0)

    @unittest.skipUnless(hasattr(os, "fork"), "dispatcher integration requires Linux fork")
    def test_budget_admission_defers_and_retries_after_active_worker_settles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def successful_attempt(directory, model_sha, config_digest):
                for path in sorted(directory.glob("attempt-*/run.json")):
                    row = json.loads(path.read_text(encoding="utf-8"))
                    if (row.get("status") == "COMPLETE"
                            and row.get("model_sha256") == model_sha
                            and row.get("config_sha256") == config_digest
                            and (path.parent / "decisions.jsonl.gz").is_file()):
                        return path.parent, row
                return None

            tracker = ProcessCpuTracker.start(time.process_time())

            def reserve_at_most_two(**_kwargs):
                if tracker.active_worker_count() >= 3:
                    raise BudgetAdmissionDeferred("two active workers fit; three do not")

            fake = SimpleNamespace(
                run_game=_fake_run_game, HERE=root,
                Vocabulary=lambda _path: None,
                successful_attempt=successful_attempt,
                budget_guard=reserve_at_most_two,
                _completed_run_count=lambda: 0,
                _remaining_optimizer_projection=lambda: 0.0,
            )
            dispatcher = ParallelGameDispatcher(fake, tracker, 3)
            tasks = []
            for seed in (660001, 660002, 660003, 660004, 660005):
                key = ("validation", seed, "residual")
                tasks.append({
                    "key": key,
                    "kwargs": {
                        "seed": seed, "model": None, "vocab": None, "bootstrap": None,
                        "model_sha": "model", "config_digest": "config",
                        "output_dir": root / f"seed-{seed}",
                    },
                })

            results = dispatcher.run_jobs(tasks)

            self.assertEqual(len(results), len(tasks))
            self.assertEqual(dispatcher.max_active_workers, 2)
            self.assertGreaterEqual(len(dispatcher.budget_admission_deferrals), 1)
            self.assertEqual(
                [row[1]["game_seed"] for row in results.values()],
                [660001, 660002, 660003, 660004, 660005],
            )


if __name__ == "__main__":
    unittest.main()
