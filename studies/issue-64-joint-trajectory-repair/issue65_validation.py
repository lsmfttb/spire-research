"""Bounded Issue 65 baseline, stress, and production-launcher validation helpers."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import runner


HERE = Path(__file__).resolve().parent
BASELINE_COMMIT = "e13f97402a0716c4e2618a6d4800d58602a3058d"
LIMITS = {
    "planning_calls_max": 1,
    "complete_terminal_continuations_per_call_max": 1,
    "retry_attempts_per_call_max": 8,
    "checkpoint_replays": 2,
}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    os.replace(temporary, path)


def _baseline_worker_impl(args: argparse.Namespace) -> int:
    baseline_root = Path(args.baseline_worktree).resolve()
    source_commit = subprocess.check_output(
        ["git", "-C", str(baseline_root), "rev-parse", "HEAD"], text=True
    ).strip()
    source_dirty = subprocess.check_output(
        ["git", "-C", str(baseline_root), "status", "--porcelain"], text=True
    ).strip()
    if source_commit != BASELINE_COMMIT or source_dirty:
        raise RuntimeError(f"baseline source is not frozen clean {BASELINE_COMMIT}: {source_commit} {source_dirty}")
    baseline_file = baseline_root / "studies/issue-64-joint-trajectory-repair/runner.py"
    spec = importlib.util.spec_from_file_location("issue65_baseline_runner", baseline_file)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the frozen Issue 64 baseline runner")
    import types

    module = types.ModuleType(spec.name)
    module.__file__ = str(baseline_file)
    module.__package__ = None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.CONFIG["non_combat"]["planning_calls_max"] = LIMITS["planning_calls_max"]
    module.TRAJECTORY["complete_terminal_continuations_per_call_max"] = LIMITS[
        "complete_terminal_continuations_per_call_max"
    ]
    module.TRAJECTORY["retry_attempts_per_call_max"] = LIMITS["retry_attempts_per_call_max"]
    native_identity = module.validate_environment(args.native_source, args.stsrl_source)
    config_hash = hashlib.sha256(module.canonical(module.CONFIG).encode()).hexdigest()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    runtime_module = importlib.import_module("public_history_execution")
    controlled = importlib.import_module("sts_combat_rl.sim.controlled_run")
    public_context = importlib.import_module("sts_combat_rl.sim.public_run_context")
    execution = runtime_module.install(
        optimized=False,
        controlled_run_module=controlled,
        public_context_module=public_context,
        adapter_class=module.LightSpeedAdapter,
        expert_driver_class=module.ExpertNonCombatDriver,
        runner_module=module,
    )
    module.execute_controlled_run = controlled.execute_controlled_run
    module.action_identity_dicts_for_actions = controlled.action_identity_dicts_for_actions
    original_write = module.JsonlWriter.write

    def timed_baseline_write(writer: Any, payload: Any, *, sync: bool = False) -> None:
        with runtime_module.phase("baseline_trace_serialize_write_flush"):
            original_write(writer, payload, sync=sync)

    module.JsonlWriter.write = timed_baseline_write
    metrics_path = output / "issue65-baseline-runtime-metrics.jsonl"
    with execution.scope(
        metrics_path=metrics_path,
        labels={"stage": "baseline", "owner_seed": int(args.seed)},
        telemetry_enabled=not bool(args.telemetry_disabled),
    ):
        result = module.run_owner(
            int(args.seed), "preflight", str(output), config_hash, native_identity
        )
    _write_json(output / "issue65-baseline-worker-result.json", {
        "schema_id": "issue65-baseline-worker-v1",
        "source_commit": source_commit,
        "runner_sha256": module.sha256_file(baseline_file),
        "owner_seed": int(args.seed),
        "config_sha256": config_hash,
        "native_identity": native_identity,
        "limits": LIMITS,
        "telemetry_enabled": not bool(args.telemetry_disabled),
        "owner_result": result,
        "worker_pid": os.getpid(),
        "worker_start_ticks": runner.proc_cpu_details(os.getpid())[3],
        "worker_process_cpu_seconds": time.process_time(),
    })
    return 0


def baseline_worker(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    receipt_path = output / "issue65-baseline-worker-result.json"
    failure: BaseException | None = None
    try:
        return _baseline_worker_impl(args)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        payload: dict[str, Any] = {}
        if receipt_path.exists():
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        else:
            output.mkdir(parents=True, exist_ok=True)
            payload = {"schema_id": "issue65-baseline-worker-v1"}
        payload.update({
            "status": "failed" if failure is not None else "completed",
            "worker_pid": os.getpid(),
            "worker_start_ticks": runner.proc_cpu_details(os.getpid())[3],
            "worker_process_cpu_seconds": time.process_time(),
        })
        if failure is not None:
            payload["error_type"] = type(failure).__name__
            payload["error"] = str(failure)
        _write_json(receipt_path, payload)


def supervise_baseline(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    measurement_mode = "uninstrumented" if args.telemetry_disabled else "instrumented"
    baseline_output = output / f"baseline-{measurement_mode}"
    optimized_output = output / "optimized"
    baseline_output.mkdir(parents=True, exist_ok=True)
    optimized_output.mkdir(parents=True, exist_ok=True)
    identity = runner.optimized_source_identity()
    budget_path = optimized_output / "engineering-budget-state.json"
    state = runner.read_engineering_budget(budget_path, identity["runner_sha256"])
    config_hash = hashlib.sha256(runner.canonical({
        "config": runner.CONFIG,
        "limits": LIMITS,
        "seeds": [int(args.seed)],
        "source_identity": identity,
        "optimizer_id": "issue65-demand-driven-public-history-v1",
    }).encode()).hexdigest()
    if state.get("engineering_config_sha256") not in (None, config_hash):
        raise RuntimeError("baseline receipts do not match the active engineering profile")
    consumed_before = float(state["builder_cpu_seconds"])
    cpu_sampler = runner.ProcessTreeCPUSampler(os.getpid())
    started_cpu = cpu_sampler.sample()
    started_wall = time.monotonic()
    state.update({
        "engineering_config_sha256": config_hash,
        "selected_fixed_seeds": [int(args.seed)],
        "engineering_limits": LIMITS,
        "baseline_source_commit": BASELINE_COMMIT,
        "baseline_runner_sha256": runner.sha256_file(
            Path(args.baseline_worktree) / "studies/issue-64-joint-trajectory-repair/runner.py"
        ),
        "optimizer_module_sha256": identity["optimizer_module_sha256"],
        "workers": 1,
    })
    runner.write_budget_state(budget_path, state)
    worker_command = [
        sys.executable, str(Path(__file__).resolve()), "baseline-worker",
        "--baseline-worktree", args.baseline_worktree,
        "--output", str(baseline_output),
        "--seed", str(args.seed),
        "--native-source", args.native_source,
        "--stsrl-source", args.stsrl_source,
    ]
    if args.telemetry_disabled:
        worker_command.append("--telemetry-disabled")
    worker = subprocess.Popen(worker_command)
    while worker.poll() is None:
        time.sleep(0.5)
        current = cpu_sampler.sample()
        delta = max(0.0, current - started_cpu)
        consumed = consumed_before + delta
        state.update({
            "builder_cpu_seconds": consumed,
            "last_process_cpu_checkpoint": delta,
            "stop_requested": consumed >= runner.ENGINEERING_BUILDER_SOFT_STOP_CPU,
            "hard_stop_requested": consumed >= runner.ENGINEERING_BUILDER_HARD_CPU,
            "last_updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        runner.write_budget_state(budget_path, state)
        if state["stop_requested"]:
            (baseline_output / "budget-stop.flag").touch()
            (optimized_output / "issue65-budget-stop.flag").touch()
        if state["hard_stop_requested"]:
            worker.terminate()
            worker.wait(timeout=10)
            _write_json(output / "issue65-budget-stop-receipt.json", {
                "phase": "baseline_timing_witness",
                "builder_cpu_seconds": consumed,
                "hard_stop_cpu_seconds": runner.ENGINEERING_BUILDER_HARD_CPU,
                "worker_returncode": worker.returncode,
                "disposition": "EFFICIENCY_NOT_ESTABLISHED",
            })
            return 2
    receipt_path = baseline_output / "issue65-baseline-worker-result.json"
    if receipt_path.exists():
        cpu_sampler.record_worker_result(json.loads(receipt_path.read_text(encoding="utf-8")))
    final_cpu = cpu_sampler.sample()
    delta = max(0.0, final_cpu - started_cpu)
    consumed = consumed_before + delta
    state.update({
        "builder_cpu_seconds": consumed,
        "last_process_cpu_checkpoint": delta,
        "stop_requested": consumed >= runner.ENGINEERING_BUILDER_SOFT_STOP_CPU,
        "hard_stop_requested": consumed >= runner.ENGINEERING_BUILDER_HARD_CPU,
        f"baseline_{measurement_mode}_timing_witness_cpu_seconds": delta,
        f"baseline_{measurement_mode}_timing_witness_wall_seconds": time.monotonic() - started_wall,
        "last_updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    runner.write_budget_state(budget_path, state)
    print(json.dumps({
        "baseline_returncode": worker.returncode,
        "baseline_cpu_seconds": delta,
        "baseline_wall_seconds": time.monotonic() - started_wall,
        "measurement_mode": measurement_mode,
        "aggregate_builder_cpu_seconds": consumed,
        "budget_path": str(budget_path),
        "baseline_output": str(baseline_output),
    }, sort_keys=True))
    return int(worker.returncode or 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    baseline = sub.add_parser("baseline-worker")
    baseline.add_argument("--baseline-worktree", required=True)
    baseline.add_argument("--output", required=True)
    baseline.add_argument("--seed", type=int, required=True)
    baseline.add_argument("--native-source", default="/home/lsmft/stsrl-spikes/sts_lightspeed-issue59")
    baseline.add_argument("--stsrl-source", default="/home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c")
    baseline.add_argument("--telemetry-disabled", action="store_true")
    supervisor = sub.add_parser("supervise-baseline")
    supervisor.add_argument("--baseline-worktree", required=True)
    supervisor.add_argument("--output", required=True)
    supervisor.add_argument("--seed", type=int, choices=runner.ENGINEERING_SEEDS, required=True)
    supervisor.add_argument("--native-source", default="/home/lsmft/stsrl-spikes/sts_lightspeed-issue59")
    supervisor.add_argument("--stsrl-source", default="/home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c")
    supervisor.add_argument("--telemetry-disabled", action="store_true")
    args = parser.parse_args()
    if args.command == "baseline-worker":
        return baseline_worker(args)
    if args.command == "supervise-baseline":
        return supervise_baseline(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
