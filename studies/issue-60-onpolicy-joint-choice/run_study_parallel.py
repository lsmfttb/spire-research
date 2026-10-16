"""Issue 60 bounded execution wrapper for the Planner-authorized R3 correction.

It imports the immutable study runner and R2 guard, adds fixed-key worker
scheduling/accounting around their existing calls, and makes no model or
training-setting changes.
"""
from __future__ import annotations

import argparse
import cProfile
import copy
import json
import os
from pathlib import Path
import pstats
import resource
import sys
import time
from typing import Any

from parallel_execution import (
    MAX_GAME_WORKERS, ParallelExecutionError, ParallelGameDispatcher,
    ProcessCpuTracker, can_dispatch, choose_worker_count, compare_replay, host_capacity,
    profile_summary, stage_result_status,
)
from resource_amendment_r1 import _git as study_git


HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "evidence"
VALIDATION = EVIDENCE / "validation" / "parallel-execution"
PARALLEL_RECEIPT = VALIDATION / "receipt.json"
PARALLEL_SOURCE_FILES = ("parallel_execution.py", "run_study_parallel.py")
MAX_VALIDATION_CPU_SECONDS = 500.0
VALIDATION_COMMAND_CPU_START = 0.0
VALIDATION_SETUP_CPU = 0.0
VALIDATION_PRIOR_CPU = 0.0

R2: Any = None
RS: Any = None
TRACKER: ProcessCpuTracker | None = None
DISPATCHER: ParallelGameDispatcher | None = None
STAGE: str = ""
BASE_ENFORCE: Any = None
ORIGINAL_INSTALL_HOOKS: Any = None
ORIGINAL_STAGE: Any = None


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_validation_receipt() -> dict[str, Any]:
    try:
        value = json.loads(PARALLEL_RECEIPT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParallelExecutionError("parallel validation receipt is missing or invalid") from exc
    if value.get("status") != "PASS" or value.get("validation", {}).get("status") != "PASS":
        raise ParallelExecutionError("parallel validation has not passed; do not resume the study")
    if value.get("source_files") != {path: _sha256(HERE / path) for path in PARALLEL_SOURCE_FILES}:
        raise ParallelExecutionError("parallel execution source differs from the validated receipt")
    return value


def _runtime_hooks() -> None:
    global DISPATCHER, BASE_ENFORCE
    ORIGINAL_INSTALL_HOOKS()
    if TRACKER is None:
        raise ParallelExecutionError("aggregate CPU tracker was not initialized before the stage")
    if STAGE == "parallel-validation":
        worker_count = 2
        profile_dir = VALIDATION / "profiles"
    else:
        receipt = _read_validation_receipt()
        worker_count = int(receipt["worker_selection"]["worker_count"])
        profile_dir = None
    DISPATCHER = ParallelGameDispatcher(
        RS, TRACKER, worker_count, profile_directory=profile_dir,
    )
    RS.run_game = DISPATCHER.dispatch_run_game
    RS._load_final_model = DISPATCHER.wrap_model_loader()

    if BASE_ENFORCE is None:
        BASE_ENFORCE = R2.enforce_budget

    def aggregate_enforce(**kwargs: Any) -> dict[str, Any]:
        if TRACKER is None:
            return BASE_ENFORCE(**kwargs)
        current_stage_cpu = TRACKER.current_stage_cpu()
        kwargs["current_stage_cpu"] = current_stage_cpu
        accounted = float(kwargs["accounted_cpu"]) + current_stage_cpu
        active = TRACKER.active_worker_count()
        with TRACKER.max_game_cpu.get_lock():
            max_game = float(TRACKER.max_game_cpu.value)
        # Keep enough CPU inside the cap for every already-running fixed game
        # to reach a safe decision boundary and be settled before stopping.
        if not can_dispatch(
            actual_cpu=accounted,
            builder_budget=float(kwargs.get("builder_budget", 180_000.0)),
            active_or_pending_workers=active,
            max_game_cpu=max_game,
        ):
            from resource_amendment_r2 import ResourceBudgetExceeded
            raise ResourceBudgetExceeded(
                "RESOURCE_BLOCKED: stop dispatching with a full-game in-flight CPU reserve"
            )
        if STAGE == "parallel-validation" and not can_dispatch(
            actual_cpu=VALIDATION_PRIOR_CPU + VALIDATION_SETUP_CPU + current_stage_cpu,
            builder_budget=MAX_VALIDATION_CPU_SECONDS - 5.0,
            active_or_pending_workers=active,
            max_game_cpu=max_game,
        ):
            from resource_amendment_r2 import ResourceBudgetExceeded
            raise ResourceBudgetExceeded(
                "RESOURCE_BLOCKED: cannot reserve safe completion of live profile replay "
                f"within the remaining {MAX_VALIDATION_CPU_SECONDS:.0f} aggregate CPU-s"
            )
        if STAGE == "parallel-validation":
            # Planner explicitly authorized up to 500 CPU-s of read-only
            # fixed-key profiling even when the unchanged future-grid forecast
            # blocks formal training. The cumulative actual Builder cap and
            # validation-specific in-flight reserve above still apply.
            return {
                "status": "PASS",
                "scope": "bounded read-only parallel/profile validation",
                "actual_builder_cpu_seconds": accounted,
                "builder_budget_cpu_seconds": float(kwargs.get("builder_budget", 180_000.0)),
                "validation_cpu_limit_seconds": MAX_VALIDATION_CPU_SECONDS,
                "formal_grid_forecast_enforced": False,
            }
        return BASE_ENFORCE(**kwargs)

    R2.enforce_budget = aggregate_enforce


def _stage_receipt(stage: str, *, stage_started_cpu: float,
                   stage_started_wall: float, status: str) -> dict[str, Any]:
    assert TRACKER is not None
    assert DISPATCHER is not None
    parent_cpu = max(0.0, time.process_time() - stage_started_cpu)
    with TRACKER.completed_cpu.get_lock():
        worker_cpu = float(TRACKER.completed_cpu.value)
    aggregate_cpu = parent_cpu + worker_cpu
    record = {
        "schema_id": "issue60-parallel-stage-receipt-v1",
        "status": status,
        "stage": stage,
        "worker_count": DISPATCHER.workers,
        "max_active_workers": DISPATCHER.max_active_workers,
        "parent_process_cpu_seconds": parent_cpu,
        "worker_process_cpu_seconds": worker_cpu,
        "aggregate_process_cpu_seconds": aggregate_cpu,
        "wall_seconds": max(0.0, time.monotonic() - stage_started_wall),
        "host": host_capacity(),
        **DISPATCHER.evidence(),
        "source_commit": study_git(HERE, "rev-parse", "HEAD"),
        "source_files": {path: _sha256(HERE / path) for path in PARALLEL_SOURCE_FILES},
    }
    receipt_path = VALIDATION / "stages" / f"{stage}.json"
    R2._write_json(receipt_path, record)
    if worker_cpu > 0.0:
        R2._append_ledger({
            "stage": f"{stage}-parallel-workers", "status": status,
            "process_cpu_seconds": worker_cpu,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "accounting": "wait4 child user+system CPU, each reaped worker exactly once",
        })
    return record


def _validation_ledger_cpu() -> float:
    path = EVIDENCE / "cpu-ledger.jsonl"
    if not path.is_file():
        return 0.0
    total = 0.0
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            stage = row.get("stage")
            if stage in {
                "parallel-validation-setup", "parallel-validation-and-profile",
                "parallel-validation-optimizer-minibatch-profile",
                "parallel-validation-profile-finalization",
            }:
                amount = float(row.get("process_cpu_seconds", 0.0))
                if amount < 0:
                    raise ValueError("negative CPU charge")
                total += amount
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ParallelExecutionError(f"invalid validation CPU ledger row {number}") from exc
    return total


def _partial_replay_record(run_study: Any, task: dict[str, Any]) -> dict[str, Any]:
    output = Path(task["kwargs"]["output_dir"])
    attempts = sorted(output.glob("attempt-*/status.json"))
    if not attempts:
        return {"seed": int(task["kwargs"]["seed"]), "status": "NOT_STARTED"}
    status_path = attempts[-1]
    attempt = status_path.parent
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"seed": int(task["kwargs"]["seed"]), "status": "INVALID_STATUS", "failure": str(exc)}
    replay_path = attempt / "decisions.jsonl.gz"
    if not replay_path.is_file():
        replay_path = attempt / "decisions.jsonl"
    replay_rows = run_study.read_jsonl(replay_path) if replay_path.is_file() else []
    original_attempt, _original_summary = task["original"]
    original_path = original_attempt / "decisions.jsonl.gz"
    original_rows = run_study.read_jsonl(original_path)
    prefix_matches = len(replay_rows) <= len(original_rows) and replay_rows == original_rows[:len(replay_rows)]
    search_path = attempt / "battle-search.jsonl"
    search_cpu = 0.0
    if search_path.is_file():
        for line in search_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                search_cpu += float(json.loads(line).get("cpu_s", 0.0))
    return {
        "seed": int(task["kwargs"]["seed"]),
        "status": str(status.get("status", "UNKNOWN")),
        "failure": status.get("failure"),
        "attempt_path": attempt.relative_to(HERE).as_posix(),
        "process_cpu_seconds": float(status.get("process_cpu_seconds", 0.0)),
        "decision_prefix_rows": len(replay_rows),
        "retained_original_decision_rows": len(original_rows),
        "decision_prefix_matches_retained_original": prefix_matches,
        "search_calls": int(sum(int(json.loads(line).get("calls", 0)) for line in search_path.read_text(encoding="utf-8").splitlines() if line.strip())) if search_path.is_file() else 0,
        "native_search_timer_cpu_seconds": search_cpu,
        "partial_decisions_sha256": _sha256(replay_path),
        "retained_original_decisions_sha256": _sha256(original_path),
    }


def _record_validation_failure(run_study: Any, runtime: dict[str, Any], model_sha: str,
                               tasks: list[dict[str, Any]], profile_paths: list[Path],
                               formal_grid_forecast: dict[str, Any], exc: BaseException,
                               *, stage_started_cpu: float, stage_started_wall: float) -> None:
    """Persist partial diagnostics and charge the aggregate exactly once on stop."""
    assert TRACKER is not None and DISPATCHER is not None
    with TRACKER.completed_cpu.get_lock():
        worker_cpu = float(TRACKER.completed_cpu.value)
    replays = [_partial_replay_record(run_study, task) for task in tasks]
    usable_profiles = [path for path in profile_paths if path.is_file()]
    run_cpu = sum(float(row.get("process_cpu_seconds", 0.0)) for row in replays)
    search_cpu = sum(float(row.get("native_search_timer_cpu_seconds", 0.0)) for row in replays)
    profile = profile_summary(usable_profiles, run_cpu=run_cpu, search_cpu=search_cpu)
    blocked = "RESOURCE_BLOCKED" in str(exc)
    status = "RESOURCE_BLOCKED" if blocked else "FAILED"
    profile_summary_by_seed = []
    for path in usable_profiles:
        stats = pstats.Stats(str(path))
        top = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:20]
        profile_summary_by_seed.append({
            "seed": int(path.stem.removeprefix("seed-")),
            "path": path.relative_to(HERE).as_posix(),
            "sha256": _sha256(path),
            "function_count": len(stats.stats),
            "top_functions": [{
                "file": Path(key[0]).name, "line": int(key[1]), "function": key[2],
                "primitive_calls": int(values[0]), "calls": int(values[1]),
                "exclusive_cpu_seconds": float(values[2]),
                "inclusive_cpu_seconds": float(values[3]),
            } for key, values in top],
        })
    parent_cpu = max(0.0, time.process_time() - stage_started_cpu)
    aggregate_cpu = parent_cpu + worker_cpu
    receipt = {
        "schema_id": "issue60-parallel-execution-validation-v1",
        "status": status,
        "planner_authorization": {
            "comment_ids": [6210979819, 6211078134],
            "profile_keys": [660001, 660046],
            "additional_cpu_bound_seconds": MAX_VALIDATION_CPU_SECONDS,
        },
        "validation": {
            "status": status,
            "failure_type": type(exc).__name__,
            "failure": str(exc),
            "replays_are_partial": True,
        },
        "r2_receipt_sha256": _sha256(EVIDENCE / "preflight" / "resource-r2.json"),
        "r2_seal_sha256": run_study_r2.verify_r2_receipt(HERE, require_seal=True)["seal"]["preseal_receipt_sha256"],
        "zero_update_model_sha256": model_sha,
        "formal_grid_forecast_at_validation": formal_grid_forecast,
        "paired_replays": replays,
        "profile": {**profile, "partial_profiles_by_seed": profile_summary_by_seed},
        "optimizer_minibatch_profile": {
            "status": "NOT_RUN_BEFORE_RESOURCE_STOP",
            "decision_rows": 0, "minibatch_rows": 256,
            "source_readout": "The retained runner preprocesses and forwards each decision row separately, then backpropagates one combined scalar loss per minibatch.",
        },
        "runtime": runtime,
        "host": host_capacity(),
        "parallel_validation": DISPATCHER.evidence(),
        "worker_count": DISPATCHER.workers,
        "prior_validation_cpu_seconds": VALIDATION_PRIOR_CPU,
        "setup_validation_cpu_seconds": VALIDATION_SETUP_CPU,
        "profile_and_replay_stage_cpu_seconds": aggregate_cpu,
        "aggregate_validation_cpu_seconds_before_finalization": (
            VALIDATION_PRIOR_CPU + VALIDATION_SETUP_CPU + aggregate_cpu
        ),
        "source_files": {path: _sha256(HERE / path) for path in PARALLEL_SOURCE_FILES},
        "source_commit": study_git(HERE, "rev-parse", "HEAD"),
    }
    stage_record = {
        "schema_id": "issue60-parallel-stage-receipt-v1",
        "status": status, "stage": "parallel-validation",
        "worker_count": DISPATCHER.workers,
        "max_active_workers": DISPATCHER.max_active_workers,
        "parent_process_cpu_seconds": parent_cpu,
        "worker_process_cpu_seconds": worker_cpu,
        "aggregate_process_cpu_seconds": aggregate_cpu,
        "wall_seconds": max(0.0, time.monotonic() - stage_started_wall),
        "host": receipt["host"],
        "parallel_validation": DISPATCHER.evidence(),
        "failure": str(exc),
        "source_commit": receipt["source_commit"],
        "source_files": receipt["source_files"],
    }
    parent_cpu = max(0.0, time.process_time() - stage_started_cpu)
    aggregate_cpu = parent_cpu + worker_cpu
    stage_record["parent_process_cpu_seconds"] = parent_cpu
    stage_record["aggregate_process_cpu_seconds"] = aggregate_cpu
    receipt["profile_and_replay_stage_cpu_seconds"] = aggregate_cpu
    receipt["aggregate_validation_cpu_seconds_before_finalization"] = (
        VALIDATION_PRIOR_CPU + VALIDATION_SETUP_CPU + aggregate_cpu
    )
    finalization_start = time.process_time()
    R2._write_json(VALIDATION / "stages" / "parallel-validation.json", stage_record)
    R2._write_json(PARALLEL_RECEIPT, receipt)
    finalization_cpu = max(0.0, time.process_time() - finalization_start)
    # A small write/ledger margin makes the failed-stage charge conservative.
    charge = aggregate_cpu + finalization_cpu + 0.1
    R2._append_ledger({
        "stage": "parallel-validation-and-profile", "status": status,
        "process_cpu_seconds": charge,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "accounting": "parent process_time plus every reaped worker wait4 user/system CPU once, plus 0.1s finalization margin",
        "validation_receipt_sha256": _sha256(PARALLEL_RECEIPT),
    })
    receipt["ledger_cpu_charge_seconds"] = charge
    R2._write_json(PARALLEL_RECEIPT, receipt)


def _install_runner_hooks() -> None:
    global RS
    import run_study
    RS = run_study
    R2._install_runtime_hooks = _runtime_hooks

    def tracked_stage(stage: str, *, stage_start_cpu: float) -> int:
        global TRACKER, DISPATCHER, STAGE
        STAGE = stage
        TRACKER = ProcessCpuTracker.start(stage_start_cpu)
        with TRACKER.max_game_cpu.get_lock():
            TRACKER.max_game_cpu.value = RS._max_observed_game_seconds()
        stage_started_wall = time.monotonic()
        DISPATCHER = None
        status = "FAILED"
        try:
            result = ORIGINAL_STAGE(stage, stage_start_cpu=stage_start_cpu)
            status_path = EVIDENCE / "stage-status" / f"{stage}.json"
            recorded_status = None
            if status_path.is_file():
                try:
                    recorded_status = str(json.loads(status_path.read_text(encoding="utf-8")).get("status", "")) or None
                except (OSError, json.JSONDecodeError):
                    recorded_status = None
            status = stage_result_status(result, recorded_status)
            return result
        finally:
            if DISPATCHER is not None:
                _stage_receipt(
                    stage, stage_started_cpu=stage_start_cpu,
                    stage_started_wall=stage_started_wall, status=status,
                )
            TRACKER = None
            DISPATCHER = None

    R2._run_original_stage = tracked_stage


def _load_validation_inputs() -> tuple[dict[str, Any], dict[str, Any], Any, Any, Any, str]:
    import torch
    import run_study
    config = json.loads(run_study.CONFIG_PATH.read_text(encoding="utf-8"))
    run_study.validate_config(config)
    runtime = run_study.verify_runtime(config)
    vocab = run_study.Vocabulary(run_study.HERE / "vocabulary.json")
    bootstrap, _ = run_study.load_bootstrap(run_study.HERE / config["bootstrap"]["checkpoint"])
    checkpoint_path = EVIDENCE / "training" / "block1" / "latest.pt"
    if not checkpoint_path.is_file():
        raise ParallelExecutionError("the retained zero-update block1 checkpoint is missing")
    payload = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    if payload.get("completed_batches") != 0 or payload.get("config_sha256") != run_study.config_sha():
        raise ParallelExecutionError("profile replay must use the retained zero-update block1 checkpoint")
    torch.manual_seed(run_study.MODEL_SEEDS["block1"])
    model = run_study.JointChoiceActorCritic(vocab)
    model.load_state_dict(payload["model_state"], strict=True)
    model_sha = run_study.model_fingerprint(model, vocab, run_study.config_sha())
    if model_sha != payload.get("model_sha256"):
        raise ParallelExecutionError("retained zero-update checkpoint fingerprint mismatch")
    return config, runtime, vocab, bootstrap, model, model_sha


def _preflight_rows(run_study: Any, vocab: Any, bootstrap: Any) -> list[Any]:
    from controllers import load_bootstrap
    rows = []
    config = json.loads(run_study.CONFIG_PATH.read_text(encoding="utf-8"))
    for index, seed in enumerate(run_study.PRE_FLIGHT_SEEDS):
        block = "block1" if index % 2 == 0 else "block2"
        model = run_study._preflight_model(vocab, run_study.MODEL_SEEDS[block])
        digest = run_study.config_sha()
        model_sha = run_study.model_fingerprint(model, vocab, digest)
        directory = EVIDENCE / "runs" / "preflight" / block / f"seed-{seed}"
        completed = run_study.successful_attempt(directory, model_sha, digest)
        if completed is None:
            raise ParallelExecutionError(f"frozen optimizer minibatch source is incomplete: preflight seed {seed}")
        attempt, summary = completed
        rows.extend(run_study.decision_rows(attempt, summary))
    return rows


def _profile_fixed_optimizer_minibatch(run_study: Any, vocab: Any,
                                       rows: list[Any], profile_path: Path) -> tuple[float, float, cProfile.Profile]:
    """Repeat the registered 256-row PPO benchmark on a disposable copy."""
    import torch
    from trainer import centered_advantages, ppo_minibatch_loss

    rng_state = torch.get_rng_state().clone()
    try:
        torch.manual_seed(run_study.MODEL_SEEDS["block1"])
        model = run_study.JointChoiceActorCritic(vocab)
        optimizer = torch.optim.Adam(model.parameters(), lr=3e-4)
        batch = [rows[index % len(rows)] for index in range(256)]
        advantages = centered_advantages(batch)
        profiler = cProfile.Profile(time.process_time)
        wall_start = time.monotonic()
        cpu_start = time.process_time()
        profiler.enable()
        optimizer.zero_grad(set_to_none=True)
        loss, _ = ppo_minibatch_loss(model, batch, advantages, vocab)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 0.5)
        optimizer.step()
        profiler.disable()
        wall_seconds = max(0.0, time.monotonic() - wall_start)
        cpu_seconds = max(0.0, time.process_time() - cpu_start)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profiler.dump_stats(str(profile_path))
        return cpu_seconds, wall_seconds, profiler
    finally:
        torch.set_rng_state(rng_state)


def _profile_validation() -> dict[str, Any]:
    global R2, RS, TRACKER, DISPATCHER, STAGE, BASE_ENFORCE, ORIGINAL_INSTALL_HOOKS
    global VALIDATION_SETUP_CPU, VALIDATION_PRIOR_CPU
    import torch
    import run_study
    import run_study_r2
    config, runtime, vocab, bootstrap, model, model_sha = _load_validation_inputs()
    RS = run_study
    R2 = run_study_r2
    STAGE = "parallel-validation"
    VALIDATION_PRIOR_CPU = _validation_ledger_cpu()
    VALIDATION_SETUP_CPU = max(0.0, time.process_time() - VALIDATION_COMMAND_CPU_START)
    R2._append_ledger({
        "stage": "parallel-validation-setup", "status": "STARTED",
        "process_cpu_seconds": VALIDATION_SETUP_CPU + 0.1,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "accounting": "resource/runtime/checkpoint setup plus a 0.1s ledger-write margin",
    })
    started_cpu = time.process_time()
    started_wall = time.monotonic()
    TRACKER = ProcessCpuTracker.start(started_cpu)
    with TRACKER.max_game_cpu.get_lock():
        TRACKER.max_game_cpu.value = RS._max_observed_game_seconds()
    R2.STAGE_START_CPU = started_cpu
    ORIGINAL_INSTALL_HOOKS = R2._install_runtime_hooks
    BASE_ENFORCE = R2.enforce_budget
    _runtime_hooks()
    assert DISPATCHER is not None
    from resource_amendment_r2 import (
        BUILDER_BUDGET_SECONDS, FORMAL_GAME_COUNT, STATIC_ALLOWANCE_SECONDS,
        _ledger_total, build_forecast, complete_game_costs,
    )
    formal_observations = complete_game_costs(HERE)
    completed_formal_games = sum(row["stage_key"] != "preflight" for row in formal_observations)
    formal_optimizer = run_study._remaining_optimizer_projection()
    formal_grid_forecast = build_forecast(
        costs=[float(row["process_cpu_seconds"]) for row in formal_observations],
        remaining_games=max(0, FORMAL_GAME_COUNT - completed_formal_games),
        remaining_optimizer_cpu=float(formal_optimizer),
        accounted_cpu=(STATIC_ALLOWANCE_SECONDS + _ledger_total(HERE)
                       + TRACKER.current_stage_cpu()),
        builder_budget=BUILDER_BUDGET_SECONDS,
    )
    config_digest = run_study.config_sha()
    tasks = []
    for seed in (660001, 660046):
        original_dir = run_study._training_game_dir("block1", 1, seed)
        original = run_study.successful_attempt(original_dir, model_sha, config_digest)
        if original is None:
            raise ParallelExecutionError(f"paired unprofiled training run {seed} is missing")
        runtime_seed = run_study.derive_seed("training-policy", run_study.MODEL_SEEDS["block1"], seed, 1)
        output_dir = VALIDATION / "replays" / f"seed-{seed}"
        kwargs = {
            "seed": seed, "mode": "residual", "model": model,
            "block_name": "block1", "runtime_seed": runtime_seed,
            "output_dir": output_dir, "model_sha": model_sha,
            "config_digest": config_digest, "vocab": vocab,
            "bootstrap": bootstrap, "require_targets": True,
            "provenance_config": run_study._current_policy_provenance(config, vocab, model_sha, "residual"),
            "stage_key": "block1:batch-01", "arm": "residual",
        }
        tasks.append({
            "key": ("block1:batch-01", seed, "residual"),
            "kwargs": kwargs,
            "profile_path": VALIDATION / "profiles" / f"seed-{seed}.pstats",
            "original": original,
        })
    profile_paths = [Path(task["profile_path"]) for task in tasks]
    try:
        validation_results = DISPATCHER.run_jobs(tasks)
    except BaseException as exc:
        _record_validation_failure(
            run_study, runtime, model_sha, tasks, profile_paths,
            formal_grid_forecast, exc,
            stage_started_cpu=started_cpu, stage_started_wall=started_wall,
        )
        raise
    replay_checks = []
    replay_summaries = []
    for task in tasks:
        key = tuple(task["key"])
        replay_attempt, replay_summary = validation_results[key]
        original_attempt, original_summary = task["original"]
        original_decisions = run_study.read_jsonl(original_attempt / "decisions.jsonl.gz")
        replay_decisions = run_study.read_jsonl(replay_attempt / "decisions.jsonl.gz")
        check = compare_replay(
            original_summary, replay_summary,
            original_decisions, replay_decisions,
        )
        check.update({
            "seed": int(task["kwargs"]["seed"]),
            "original_run_json_sha256": run_study.sha256_bytes((original_attempt / "run.json").read_bytes()),
            "replay_run_json_sha256": run_study.sha256_bytes((replay_attempt / "run.json").read_bytes()),
            "original_decisions_sha256": run_study.sha256_bytes((original_attempt / "decisions.jsonl.gz").read_bytes()),
            "replay_decisions_sha256": run_study.sha256_bytes((replay_attempt / "decisions.jsonl.gz").read_bytes()),
        })
        replay_checks.append(check)
        replay_summaries.append(replay_summary)
    native_search_cpu = sum(float(row.get("search_cpu_seconds", 0.0)) for row in replay_summaries)
    run_cpu = sum(float(row.get("process_cpu_seconds", 0.0)) for row in replay_summaries)
    profiles = profile_summary(profile_paths, run_cpu=run_cpu, search_cpu=native_search_cpu)

    preflight_rows = _preflight_rows(run_study, vocab, bootstrap)
    optimizer_profile_path = VALIDATION / "profiles" / "optimizer-minibatch.pstats"
    optimizer_cpu, optimizer_wall, optimizer_profiler = _profile_fixed_optimizer_minibatch(
        run_study, vocab, preflight_rows, optimizer_profile_path,
    )
    optimizer_stats = pstats.Stats(str(optimizer_profile_path))
    optimizer_top = []
    for (filename, line, function), (primitive, calls, own, cumulative, _callers) in sorted(
        optimizer_stats.stats.items(), key=lambda item: item[1][3], reverse=True
    )[:40]:
        optimizer_top.append({
            "file": Path(filename).name, "line": int(line), "function": function,
            "primitive_calls": int(primitive), "calls": int(calls),
            "exclusive_cpu_seconds": float(own), "inclusive_cpu_seconds": float(cumulative),
        })
    runtime_host = host_capacity()
    worker_selection = choose_worker_count(
        effective_cpus=int(runtime_host["effective_cpu_count"]),
        available_bytes=int(runtime_host["effective_memory_available_bytes"]),
        worker_peak_rss_samples=[int(row["peak_rss_bytes"]) for row in DISPATCHER.executions],
        cap=MAX_GAME_WORKERS,
    )
    stage_cpu = TRACKER.current_stage_cpu()
    total_validation_cpu = VALIDATION_PRIOR_CPU + VALIDATION_SETUP_CPU + stage_cpu
    receipt_write_cpu_start = time.process_time()
    if total_validation_cpu > MAX_VALIDATION_CPU_SECONDS - 5.0:
        raise ParallelExecutionError(
            f"parallel/profile validation used {total_validation_cpu:.3f} CPU-s, over the {MAX_VALIDATION_CPU_SECONDS:.0f}s allowance"
        )
    receipt = {
        "schema_id": "issue60-parallel-execution-validation-v1",
        "status": "PASS" if all(row["status"] == "PASS" for row in replay_checks) else "FAILED",
        "planner_authorization": {
            "comment_ids": [6210979819, 6211078134],
            "execution_correction": "same frozen128-game training batch; parallel owner arms after final checkpoints",
            "profile_keys": [660001, 660046],
            "additional_cpu_bound_seconds": MAX_VALIDATION_CPU_SECONDS,
        },
        "r2_receipt_sha256": _sha256(EVIDENCE / "preflight" / "resource-r2.json"),
        "r2_seal_sha256": run_study_r2.verify_r2_receipt(HERE, require_seal=True)["seal"]["preseal_receipt_sha256"],
        "zero_update_model_sha256": model_sha,
        "paired_replays": replay_checks,
        "profile": profiles,
        "optimizer_minibatch_profile": {
            "fixed_source": "the exact eight preflight games/decision rows used by the frozen native.json benchmark",
            "decision_rows": len(preflight_rows),
            "minibatch_rows": 256,
            "wall_seconds": optimizer_wall,
            "process_cpu_seconds": optimizer_cpu,
            "timed_boundary": "zero_grad through loss forward, backward, gradient clipping, and optimizer.step; model/optimizer construction and row repetition excluded, matching run_study._benchmark_minibatch",
            "disposable_model_optimizer_only": True,
            "torch_rng_state_restored": True,
            "top_functions": optimizer_top,
            "source_readout": "preprocessing and model forward are per decision row in ppo_minibatch_loss; one combined scalar loss is backpropagated, then one optimizer step runs per minibatch",
        },
        "runtime": runtime,
        "host": runtime_host,
        "parallel_validation": DISPATCHER.evidence(),
        "worker_selection": worker_selection,
        "formal_grid_forecast_at_validation": formal_grid_forecast,
        "formal_grid_forecast_policy": (
            "reported but not enforced for this explicitly authorized bounded diagnostic; "
            "formal training/evaluation stages still enforce the complete-grid forecast"
        ),
        "setup_validation_cpu_seconds": VALIDATION_SETUP_CPU,
        "profile_and_replay_stage_cpu_seconds": stage_cpu,
        "prior_validation_cpu_seconds": VALIDATION_PRIOR_CPU,
        "aggregate_validation_cpu_seconds": total_validation_cpu,
        "validation_wall_seconds": max(0.0, time.monotonic() - started_wall),
        "source_files": {path: _sha256(HERE / path) for path in PARALLEL_SOURCE_FILES},
        "source_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=HERE.parents[1],
            check=True, capture_output=True, text=True,
        ).stdout.strip(),
    }
    if receipt["status"] != "PASS":
        raise ParallelExecutionError("same-key two-worker validation did not pass")
    R2._write_json(PARALLEL_RECEIPT, receipt)
    receipt_write_cpu = max(0.0, time.process_time() - receipt_write_cpu_start)
    R2._append_ledger({
        "stage": "parallel-validation-and-profile", "status": "PASS",
        "process_cpu_seconds": stage_cpu + receipt_write_cpu + 0.1,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "accounting": "parent process-time plus all two replay worker wait4 user/system CPU exactly once",
        "validation_receipt_sha256": _sha256(PARALLEL_RECEIPT),
    })
    return receipt


def main() -> int:
    global R2, RS, ORIGINAL_INSTALL_HOOKS, ORIGINAL_STAGE
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("validate-parallel", "continue", "train", "evaluate", "analyze"), required=True)
    args = parser.parse_args()
    global VALIDATION_COMMAND_CPU_START
    VALIDATION_COMMAND_CPU_START = time.process_time()
    try:
        os.setpgid(0, 0)
    except OSError:
        if os.getpgrp() != os.getpid():
            raise
    import run_study
    import run_study_r2
    R2 = run_study_r2
    RS = run_study
    if args.stage == "validate-parallel":
        from resource_amendment_r1 import verify_resource_chain
        verify_resource_chain(HERE, require_gate_seal=True)
        R2.verify_r2_receipt(HERE, require_seal=True)
        return 0 if _profile_validation()["status"] == "PASS" else 2
    _read_validation_receipt()
    ORIGINAL_INSTALL_HOOKS = R2._install_runtime_hooks
    ORIGINAL_STAGE = R2._run_original_stage
    _install_runner_hooks()
    sys.argv = ["run_study_r2.py", "--stage", args.stage]
    return R2.main()


if __name__ == "__main__":
    raise SystemExit(main())
