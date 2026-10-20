from __future__ import annotations

import hashlib
import contextlib
import importlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import resource
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import runner
import public_history_execution as runtime


def cpu_probe_owner(seed, stage, output, config_hash, native_identity, engineering_limits):
    started = time.process_time()
    target = float(engineering_limits["cpu_target_seconds"])
    stop_path = Path(output) / engineering_limits["stop_file_name"]
    value = 0
    stopped = False
    while time.process_time() - started < target:
        if stop_path.exists():
            stopped = True
            break
        value = (value + math.isqrt(value + 113)) & 0xFFFFFFFF
    elapsed = time.process_time() - started
    _ppid, _ticks, _state, worker_start_ticks = runner.proc_cpu_details(os.getpid())
    return {
        "owner_seed": seed,
        "owner_cpu_seconds": elapsed,
        "peak_rss_kib": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        "probe_stopped": stopped,
        "probe_value": value,
        "worker_pid": os.getpid(),
        "worker_start_ticks": worker_start_ticks,
        "worker_process_cpu_seconds": time.process_time(),
    }


class Issue65RuntimeTests(unittest.TestCase):
    def test_1000_entry_history_matches_original_without_quadratic_revalidation(self):
        public = importlib.import_module("sts_combat_rl.sim.public_run_context")
        execution = object.__new__(runtime.PublicHistoryExecution)
        execution.public_context = public
        execution.original_append = public.append_public_history_entry
        execution.stats = {
            "contexts_without_history": 0,
            "supported_full_history_materializations": 0,
            "supported_materialized_entries": 0,
            "terminal_full_history_materializations": 0,
            "terminal_materialized_entries": 0,
            "new_entries_sanitized": 0,
            "previous_entries_recursively_revalidated": 0,
            "untrusted_history_fallbacks": 0,
        }
        baseline = []
        optimized = runtime.ValidatedPublicHistory()
        baseline_started = time.process_time()
        optimized_started = baseline_started
        for index in range(1000):
            entry = {
                "schema_id": "public-run-history-entry-v1",
                "schema_version": 1,
                "history_index": index,
                "step_index": index,
                "selected_action": {"index": index % 3},
            }
            baseline = execution.original_append(baseline, entry)
        baseline_cpu = time.process_time() - baseline_started
        optimized_started = time.process_time()
        for index in range(1000):
            entry = {
                "schema_id": "public-run-history-entry-v1",
                "schema_version": 1,
                "history_index": index,
                "step_index": index,
                "selected_action": {"index": index % 3},
            }
            optimized = execution._append_validated(optimized, entry)
        optimized_cpu = time.process_time() - optimized_started
        self.assertEqual(baseline, list(optimized))
        self.assertEqual(0, execution.stats["previous_entries_recursively_revalidated"])
        self.assertEqual(1000, execution.stats["new_entries_sanitized"])
        print(json.dumps({
            "history_stress_entries": 1000,
            "baseline_cpu_seconds": baseline_cpu,
            "optimized_cpu_seconds": optimized_cpu,
            "speedup": baseline_cpu / max(optimized_cpu, 1e-9),
            "baseline_validated_entries": 1000 * 1001 // 2,
            "optimized_prior_entries_recursively_revalidated": 0,
        }, sort_keys=True))

    def test_map_and_rewards_keep_full_history_and_post_context_is_cached(self):
        controlled = importlib.import_module("sts_combat_rl.sim.controlled_run")
        public = importlib.import_module("sts_combat_rl.sim.public_run_context")
        execution = runtime.install(
            optimized=True,
            controlled_run_module=controlled,
            public_context_module=public,
        )
        history = runtime.ValidatedPublicHistory()
        for index in range(2):
            history = execution._append_validated(history, {
                "schema_id": "public-run-history-entry-v1",
                "schema_version": 1,
                "history_index": index,
                "step_index": index,
                "selected_action": {"index": index},
            })
        action_type = importlib.import_module("sts_combat_rl.sim.contract").SimulatorAction
        action = action_type(action_id=1, label="route", kind="map", raw={"idx1": 1})
        state = runtime._new_state(
            labels={"test": "context"}, metrics_path=None, telemetry_enabled=True,
            optimized=True,
        )
        token = runtime._ACTIVE_RUN.set(state)
        try:
            for screen in ("MAP_SCREEN", "REWARDS"):
                raw = {"screen_state": screen}
                optimized = controlled.build_public_run_context(
                    raw, [action], projection=None, history=history, include_candidates=True
                )
                baseline = execution.original_build(
                    raw, [action], projection=None, history=list(history), include_candidates=True
                )
                self.assertEqual(baseline, optimized)
                self.assertEqual(2, len(optimized["history"]))
            raw_post = {"screen_state": "BATTLE"}
            first = controlled.build_public_run_context(
                raw_post, (), projection=None, history=history, include_candidates=False
            )
            second = controlled.build_public_run_context(
                raw_post, (), projection=None, history=history, include_candidates=False
            )
            self.assertIs(first, second)
            self.assertEqual(1, state["counts"]["post_context_cache_hits"])
        finally:
            runtime._ACTIVE_RUN.reset(token)

    def test_buffered_witness_trace_is_flushed_before_digesting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "branch-decisions.jsonl"
            writer = runner.JsonlWriter(path, buffered=True, batch_lines=128)
            rows = [
                {"branch_id": "witness-1", "screen_state": "MAP_SCREEN", "chosen_action": {"index": index}, "legal_actions": [], "controller_role": "WITNESS"}
                for index in range(3)
            ]
            try:
                for row in rows:
                    writer.write(row)
                self.assertEqual("", path.read_text(encoding="utf-8"))
                writer.flush(sync=True)
                selected = [
                    {key: row.get(key) for key in ("screen_state", "chosen_action", "legal_actions", "controller_role")}
                    for row in rows
                ]
                expected = hashlib.sha256(runner.canonical(selected).encode()).hexdigest()
                self.assertEqual(expected, runner.controller_trace_digest_path(root, "witness-1"))
            finally:
                writer.close()

    def test_failed_engineering_owner_leaves_process_cpu_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            fake_runtime = type(
                "FakeRuntime", (), {"scope": lambda self, **kwargs: contextlib.nullcontext()}
            )()
            with patch.object(runner, "ensure_runtime", return_value=fake_runtime), patch.object(
                runner, "_run_owner_impl", side_effect=RuntimeError("simulated owner failure")
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated owner failure"):
                    runner.run_owner(7, "engineering", str(output), "config", {}, {})
            receipt = json.loads(
                (output / "issue65-worker-cpu-receipts" / "engineering-seed-7.json").read_text()
            )
            self.assertEqual(os.getpid(), receipt["worker_pid"])
            self.assertIsInstance(receipt["worker_start_ticks"], int)
            self.assertGreater(receipt["worker_process_cpu_seconds"], 0)

    def test_production_launcher_accepts_only_matching_installed_sources(self):
        path = Path(__file__).with_name("run_production.py")
        spec = importlib.util.spec_from_file_location("issue65_run_production_test", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            decision = {
                "formal_production_authorized_by_preflight": True,
                "runner_sha256": module.sha256_file(module.RUNNER),
                "optimizer_module_sha256": module.sha256_file(module.ROOT / "public_history_execution.py"),
            }
            (output / "preflight-decision.json").write_text(json.dumps(decision))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), patch.object(sys, "argv", [str(path), "--output", str(output)]), patch.object(
                module.subprocess, "run", return_value=type("Result", (), {"returncode": 0})()
            ) as launch:
                self.assertEqual(0, module.main())
            command = launch.call_args.args[0]
            self.assertEqual(str(module.RUNNER), command[1])
            self.assertEqual("production", command[2])
            self.assertEqual(decision["optimizer_module_sha256"], json.loads(stdout.getvalue())["optimizer_module_sha256"])
            decision["optimizer_module_sha256"] = "0" * 64
            (output / "preflight-decision.json").write_text(json.dumps(decision))
            with patch.object(sys, "argv", [str(path), "--output", str(output)]), patch.object(
                module.subprocess, "run"
            ) as launch:
                with self.assertRaises(SystemExit):
                    module.main()
                launch.assert_not_called()

    def test_process_cpu_watchdog_counts_one_and_two_workers_and_resumes(self):
        original_run_owner = runner.run_owner
        runner.run_owner = cpu_probe_owner
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                budget_path = root / "engineering-budget-state.json"
                state = {
                    "builder_cpu_seconds": 10.0,
                    "stop_requested": False,
                    "hard_stop_requested": False,
                }
                one_output = root / "one"
                first = runner.execute_stage(
                    stage="cpu-probe-one", seeds=[1], output=one_output,
                    native_identity={}, config_hash="probe", target_workers=1,
                    budget_state=state, budget_path=budget_path,
                    engineering_limits={"cpu_target_seconds": 0.25, "stop_file_name": "probe-stop.flag"},
                    soft_stop_cpu_seconds=1000, hard_stop_cpu_seconds=2000,
                    event_log_name="probe-events.jsonl", stop_file_name="probe-stop.flag",
                )
                after_one = json.loads(budget_path.read_text())
                self.assertGreater(after_one["builder_cpu_seconds"], 10.0 + first[0]["owner_cpu_seconds"] - 0.05)
                before_two = float(after_one["builder_cpu_seconds"])
                two_output = root / "two"
                second = runner.execute_stage(
                    stage="cpu-probe-two", seeds=[2, 3], output=two_output,
                    native_identity={}, config_hash="probe", target_workers=2,
                    budget_state=after_one, budget_path=budget_path,
                    engineering_limits={"cpu_target_seconds": 0.25, "stop_file_name": "probe-stop.flag"},
                    soft_stop_cpu_seconds=1000, hard_stop_cpu_seconds=2000,
                    event_log_name="probe-events.jsonl", stop_file_name="probe-stop.flag",
                )
                after_two = json.loads(budget_path.read_text())
                worker_cpu = sum(float(row["owner_cpu_seconds"]) for row in second)
                self.assertGreater(after_two["builder_cpu_seconds"], before_two + worker_cpu - 0.05)
                self.assertGreaterEqual(after_two["builder_cpu_seconds"], before_two)
                stop_output = root / "stop"
                stopped = runner.execute_stage(
                    stage="cpu-probe-stop", seeds=[4], output=stop_output,
                    native_identity={}, config_hash="probe", target_workers=1,
                    budget_state={"builder_cpu_seconds": 0.0, "stop_requested": False},
                    budget_path=root / "stop-budget.json",
                    engineering_limits={"cpu_target_seconds": 4.0, "stop_file_name": "probe-stop.flag"},
                    soft_stop_cpu_seconds=0.2, hard_stop_cpu_seconds=10.0,
                    event_log_name="probe-events.jsonl", stop_file_name="probe-stop.flag",
                )
                stop_state = json.loads((root / "stop-budget.json").read_text())
                self.assertTrue(stop_state["stop_requested"])
                self.assertTrue(stopped[0]["probe_stopped"])
                self.assertLess(stop_state["builder_cpu_seconds"], 10.0)
                print(json.dumps({
                    "one_worker_total_cpu": after_one["builder_cpu_seconds"] - 10.0,
                    "two_worker_total_cpu": after_two["builder_cpu_seconds"] - before_two,
                    "two_worker_reported_cpu": worker_cpu,
                    "resume_cumulative_cpu": after_two["builder_cpu_seconds"],
                    "watchdog_stopped": stopped[0]["probe_stopped"],
                    "watchdog_cpu": stop_state["builder_cpu_seconds"],
                }, sort_keys=True))
        finally:
            runner.run_owner = original_run_owner


if __name__ == "__main__":
    unittest.main(verbosity=2)
