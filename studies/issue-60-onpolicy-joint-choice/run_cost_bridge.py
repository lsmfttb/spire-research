"""Run the Planner-authorized 59-game zero-update cost bridge, then gate continuation."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from typing import Any

from prospective_cost_regime import (
    AUTHORIZATION_COMMENT_ID, BRIDGE_CPU_LIMIT, BRIDGE_SEEDS, BRIDGE_STAGE,
    BUILDER_CAP, FINALIZATION_RESERVE, PROCESS_CAP, REGIME_ID,
    CostRegimeError, _declared_order, atomic_json, enforce_actual_caps, git,
    load_cost_tags, producer_manifest, validate_bridge_population,
    write_cost_tag,
)


HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "evidence"
PROSPECTIVE = EVIDENCE / "prospective-cost"
BRIDGE_START = PROSPECTIVE / "bridge-start.json"
BRIDGE_BUDGET_START = PROSPECTIVE / "bridge-budget-start.json"
BRIDGE_LATEST = PROSPECTIVE / "bridge-latest.json"
FORECAST_INDEX = PROSPECTIVE / "cost-index.json"
FORECAST_LATEST = PROSPECTIVE / "latest-forecast.json"
FORECAST_EVENTS = PROSPECTIVE / "forecast-events.jsonl"
STATIC_ALLOWANCE = 600.0
BRIDGE_START_CPU = 0.0
BRIDGE_BASELINE_CPU = 0.0
BRIDGE_TRACKER_SETUP_CPU = 0.0
PRODUCER_COMMIT = ""
PRODUCER_HASHES: dict[str, str] = {}
RUN_STUDY: Any = None
R2: Any = None
R2_MODULE: Any = None
PARALLEL: Any = None
TRACKER: Any = None
DISPATCHER: Any = None
CURRENT_BRIDGE: bool = True
_LAST_FORECAST_SAMPLE_N = -1
_LAST_FORECAST_STAGE = ""
_COST_CACHE_INDEX_STAMP: int | None = None
_COST_CACHE_VALUES: list[float] = []
CACHED_STATE: Any = None
_COST_TAG_ROWS: list[dict[str, Any]] | None = None
TAG_FAILURE = PROSPECTIVE / "cost-tag-failure.json"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CostRegimeError(f"expected JSON object at {path}")
    return value


def _ledger_total() -> float:
    from resource_amendment_r2 import _ledger_total as ledger_total
    return float(ledger_total(HERE))


def _append_ledger(row: dict[str, Any]) -> None:
    path = EVIDENCE / "cpu-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _bridge_prior_charge() -> float:
    path = EVIDENCE / "cpu-ledger.jsonl"
    if not path.is_file():
        return 0.0
    charges = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if str(row.get("stage", "")).startswith("issue60-cached-cost-bridge"):
            charges.append(float(row.get("process_cpu_seconds", 0.0)))
    return math.fsum(charges)


def _bridge_budget_start_record() -> dict[str, Any]:
    """Pin one cumulative bridge baseline before setup, tests or worker launch."""
    global PRODUCER_COMMIT, PRODUCER_HASHES, BRIDGE_BASELINE_CPU
    current_commit = git(HERE, "rev-parse", "HEAD")
    current_manifest = producer_manifest(HERE)
    if BRIDGE_BUDGET_START.is_file():
        record = _read_json(BRIDGE_BUDGET_START)
        source_commit = str(record.get("producer_commit", ""))
        if (record.get("authorization_comment_id") != AUTHORIZATION_COMMENT_ID
                or record.get("regime_id") != REGIME_ID
                or record.get("producer_files") != current_manifest):
            raise CostRegimeError("bridge budget baseline authorization or producer changed")
        try:
            git(HERE, "merge-base", "--is-ancestor", source_commit, current_commit)
            for name, digest in current_manifest.items():
                actual = hashlib.sha256(git(HERE, "show", f"{source_commit}:studies/issue-60-onpolicy-joint-choice/{name}")).hexdigest()
                if actual != digest:
                    raise CostRegimeError(f"bridge budget producer commit does not contain {name}")
        except subprocess.CalledProcessError as exc:
            raise CostRegimeError("bridge budget producer is not an ancestor of the current checkout") from exc
        PRODUCER_COMMIT = source_commit
        PRODUCER_HASHES = current_manifest
        BRIDGE_BASELINE_CPU = float(record["previous_actual_builder_cpu_seconds"])
        return record

    actual_before_bridge = STATIC_ALLOWANCE + _ledger_total()
    # Recover any earlier setup-failure charges from the append-only ledger so
    # retrying the launcher cannot reset the 5,000 CPU-s bridge allowance.
    baseline = actual_before_bridge - _bridge_prior_charge()
    ledger = EVIDENCE / "cpu-ledger.jsonl"
    record = {
        "schema_id": "issue60-cost-bridge-budget-start-v1",
        "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
        "regime_id": REGIME_ID,
        "producer_commit": current_commit,
        "producer_files": current_manifest,
        "previous_actual_builder_cpu_seconds": baseline,
        "previous_ledger_line_count": len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.is_file() else 0,
        "prior_bridge_cpu_seconds_recovered_from_ledger": _bridge_prior_charge(),
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    atomic_json(BRIDGE_BUDGET_START, record)
    PRODUCER_COMMIT = current_commit
    PRODUCER_HASHES = current_manifest
    BRIDGE_BASELINE_CPU = baseline
    return record


def _write_index() -> None:
    global _COST_TAG_ROWS
    if _COST_TAG_ROWS is None:
        _COST_TAG_ROWS = load_cost_tags(HERE, current_manifest=PRODUCER_HASHES)
    rows = sorted(_COST_TAG_ROWS, key=_declared_order)
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    observations = [{
        "stage_key": row["stage_key"], "game_seed": int(row["game_seed"]),
        "arm": row["arm"], "process_cpu_seconds": float(row["process_cpu_seconds"]),
    } for row in rows]
    observation_payload = json.dumps(
        observations, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    atomic_json(FORECAST_INDEX, {
        "schema_id": "issue60-prospective-cost-index-v1",
        "regime_id": REGIME_ID,
        "producer_commit": PRODUCER_COMMIT,
        "producer_files": PRODUCER_HASHES,
        "tag_count": len(rows),
        "tags_sha256": hashlib.sha256(payload).hexdigest(),
        "cost_observations": observations,
        "cost_observations_sha256": hashlib.sha256(observation_payload).hexdigest(),
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


def _record_tag_failure(exc: BaseException) -> None:
    try:
        atomic_json(TAG_FAILURE, {
            "schema_id": "issue60-cost-tag-failure-v1",
            "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
            "source_commit": PRODUCER_COMMIT,
            "failure": f"{type(exc).__name__}: {exc}",
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    except BaseException:
        # The ordinary execution and cumulative CPU guards remain active if the
        # filesystem itself prevents persisting this diagnostic.
        pass


def _raise_if_tagging_failed() -> None:
    if TAG_FAILURE.is_file():
        failure = _read_json(TAG_FAILURE).get("failure", "unknown tagging failure")
        raise CostRegimeError(f"prospective cost tagging failed; stopping safely: {failure}")


def _tag_attempt(attempt: Path) -> bool:
    """Tag one completed formal run and publish its updated prospective sample."""
    global _COST_TAG_ROWS
    relative = attempt.resolve().relative_to(HERE.resolve()).as_posix()
    run = _read_json(attempt / "run.json")
    if run.get("status") != "COMPLETE":
        return False
    if _COST_TAG_ROWS is None:
        _COST_TAG_ROWS = load_cost_tags(HERE, current_manifest=PRODUCER_HASHES)
    tag = write_cost_tag(
        HERE, relative, producer_commit=PRODUCER_COMMIT,
        manifest=PRODUCER_HASHES,
    )
    key = (tag["stage_key"], int(tag["game_seed"]), tag["arm"])
    existing = next((row for row in _COST_TAG_ROWS
                     if (row["stage_key"], int(row["game_seed"]), row["arm"]) == key), None)
    if existing is not None:
        if existing != tag:
            raise CostRegimeError(f"duplicate prospective cost identity: {key}")
        return False
    _COST_TAG_ROWS.append(tag)
    _COST_TAG_ROWS.sort(key=_declared_order)
    _write_index()
    return True


def _tag_execution_rows(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if int(row.get("exit_code", -1)) != 0 or not row.get("attempt"):
            continue
        relative = str(row["attempt"]).replace("\\", "/")
        if not relative.startswith("evidence/runs/"):
            continue
        run_path = HERE / relative / "run.json"
        try:
            run = _read_json(run_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if run.get("status") != "COMPLETE":
            continue
        _tag_attempt(run_path.parent)


def _install_tagged_dispatcher() -> None:
    import parallel_execution
    from parallel_execution import ParallelGameDispatcher
    original_latest = parallel_execution._latest_run_summary
    original = ParallelGameDispatcher.run_jobs

    def tagged_latest(rs: Any, task: Any):
        found = original_latest(rs, task)
        if found is not None:
            attempt, summary = found
            if summary.get("status") == "COMPLETE":
                try:
                    _tag_attempt(attempt)
                except BaseException as exc:
                    _record_tag_failure(exc)
        return found

    parallel_execution._latest_run_summary = tagged_latest

    def tagged_run_jobs(self: Any, tasks: Any):
        offset = len(self.executions)
        try:
            return original(self, tasks)
        finally:
            try:
                _tag_execution_rows(self.executions[offset:])
            except BaseException as exc:
                _record_tag_failure(exc)
            if CACHED_STATE is not None:
                CACHED_STATE.sync_dispatch_results(self.results)

    ParallelGameDispatcher.run_jobs = tagged_run_jobs


def _refresh_bridge_tags(run_study: Any, model_sha: str, config_sha: str,
                         start_record: dict[str, Any]) -> list[dict[str, Any]]:
    """Recover complete originally-missing bridge keys after a clean restart."""
    global _COST_TAG_ROWS
    if start_record.get("producer_commit") != PRODUCER_COMMIT or start_record.get("producer_files") != PRODUCER_HASHES:
        raise CostRegimeError("bridge producer changed; refusing to relabel untagged attempts")
    original_missing = set(int(seed) for seed in start_record.get("originally_missing_seeds", []))
    validate_bridge_population(original_missing)
    _COST_TAG_ROWS = load_cost_tags(HERE, current_manifest=PRODUCER_HASHES)
    known = {(row["stage_key"], int(row["game_seed"]), row["arm"])
             for row in _COST_TAG_ROWS}
    for seed in sorted(original_missing):
        output = run_study._training_game_dir("block1", 1, seed)
        complete = run_study.successful_attempt(output, model_sha, config_sha)
        if complete is None:
            continue
        attempt, summary = complete
        key = (BRIDGE_STAGE, seed, "residual")
        if key not in known:
            if (summary.get("status") != "COMPLETE" or summary.get("stage_key") != BRIDGE_STAGE
                    or int(summary.get("seed", summary.get("game_seed", -1))) not in {-1, seed}):
                raise CostRegimeError(f"complete bridge record has a mismatched identity for seed {seed}")
            _tag_attempt(attempt)
            known.add(key)
    _write_index()
    return load_cost_tags(HERE, current_manifest=PRODUCER_HASHES)


def _cost_sample(bridge_only: bool = False) -> list[float]:
    global _COST_CACHE_INDEX_STAMP, _COST_CACHE_VALUES
    try:
        stamp = FORECAST_INDEX.stat().st_mtime_ns
    except OSError as exc:
        raise CostRegimeError("prospective cost index is missing") from exc
    if stamp != _COST_CACHE_INDEX_STAMP:
        index = _read_json(FORECAST_INDEX)
        if (index.get("schema_id") != "issue60-prospective-cost-index-v1"
                or index.get("regime_id") != REGIME_ID
                or index.get("producer_commit") != PRODUCER_COMMIT
                or index.get("producer_files") != PRODUCER_HASHES
                or index.get("tag_count") != len(index.get("cost_observations", []))):
            raise CostRegimeError("prospective cost index identity or tag count is invalid")
        observations = index["cost_observations"]
        payload = json.dumps(
            observations, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        if hashlib.sha256(payload).hexdigest() != index.get("cost_observations_sha256"):
            raise CostRegimeError("prospective cost index sample checksum mismatch")
        keys = [(row["stage_key"], int(row["game_seed"]), row["arm"])
                for row in observations]
        if (len(set(keys)) != len(keys)
                or keys != sorted(keys, key=lambda key: _declared_order({
                "stage_key": key[0], "game_seed": key[1], "arm": key[2]}))):
            raise CostRegimeError("prospective cost index is not in declared training/evaluation order")
        values = [float(row["process_cpu_seconds"]) for row in observations]
        if not values or any(not math.isfinite(value) or value < 0 for value in values):
            raise CostRegimeError("prospective cost index sample is empty or invalid")
        _COST_CACHE_VALUES = values
        _COST_CACHE_INDEX_STAMP = stamp
    if bridge_only:
        rows = _read_json(FORECAST_INDEX)["cost_observations"]
        bridge_rows = [row for row in rows if row["stage_key"] == BRIDGE_STAGE
                       and int(row["game_seed"]) in BRIDGE_SEEDS]
        validate_bridge_population(int(row["game_seed"]) for row in bridge_rows)
        if len(bridge_rows) != len(BRIDGE_SEEDS):
            raise CostRegimeError("bridge-only index sample is not exactly the 59 authorized games")
        return [float(row["process_cpu_seconds"]) for row in bridge_rows]
    return list(_COST_CACHE_VALUES)


def _write_forecast_event(forecast: dict[str, Any], *, stage: str,
                          remaining_games: int, remaining_optimizer_cpu: float,
                          actual_cpu: float, sample_count: int) -> None:
    global _LAST_FORECAST_SAMPLE_N, _LAST_FORECAST_STAGE
    stage_changed = stage != _LAST_FORECAST_STAGE
    should_write = (sample_count != _LAST_FORECAST_SAMPLE_N or stage_changed
                    or forecast.get("status") != "PASS")
    if not should_write:
        return
    record = {
        "schema_id": "issue60-prospective-gate-event-v1",
        "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
        "regime_id": REGIME_ID,
        "source_commit": PRODUCER_COMMIT,
        "source_files": PRODUCER_HASHES,
        "stage": stage,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sample_count": sample_count,
        "remaining_games": int(remaining_games),
        "remaining_optimizer_cpu_seconds": float(remaining_optimizer_cpu),
        "actual_accounted_cpu_seconds_including_finalization_reserve": float(actual_cpu),
        "forecast": forecast,
    }
    atomic_json(FORECAST_LATEST, record)
    if sample_count != _LAST_FORECAST_SAMPLE_N or stage_changed or forecast.get("status") != "PASS":
        with FORECAST_EVENTS.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _LAST_FORECAST_SAMPLE_N = sample_count
        _LAST_FORECAST_STAGE = stage


def _dynamic_enforce(*, accounted_cpu: float, current_stage_cpu: float,
                     costs: Any, remaining_games: int,
                     remaining_optimizer_cpu: float) -> dict[str, Any]:
    del costs
    from resource_amendment_r2 import build_forecast, ResourceBudgetExceeded
    _raise_if_tagging_failed()
    tracker = getattr(PARALLEL, "TRACKER", None)
    if tracker is None:
        raise ResourceBudgetExceeded("RESOURCE_BLOCKED: aggregate process tracker is not active")
    actual = float(accounted_cpu) + max(0.0, float(current_stage_cpu))
    active = tracker.active_worker_count()
    with tracker.max_game_cpu.get_lock():
        max_game = float(tracker.max_game_cpu.value)
    try:
        enforce_actual_caps(actual_cpu=actual, in_flight_workers=active,
                            max_game_cpu=max_game)
        sample = _cost_sample()
    except CostRegimeError as exc:
        raise ResourceBudgetExceeded(str(exc)) from exc
    forecast = build_forecast(
        costs=sample, remaining_games=int(remaining_games),
        remaining_optimizer_cpu=float(remaining_optimizer_cpu),
        accounted_cpu=actual + FINALIZATION_RESERVE,
        builder_budget=BUILDER_CAP, process_cap=PROCESS_CAP,
    )
    _write_forecast_event(
        forecast, stage=str(getattr(PARALLEL, "STAGE", "unknown")),
        remaining_games=remaining_games,
        remaining_optimizer_cpu=remaining_optimizer_cpu,
        actual_cpu=actual + FINALIZATION_RESERVE, sample_count=len(sample),
    )
    if forecast["status"] != "PASS":
        raise ResourceBudgetExceeded(
            "RESOURCE_BLOCKED: prospective cached-regime forecast exceeds the unchanged caps: "
            f"Builder={forecast['projected_builder_cpu_seconds']:.3f}s, "
            f"all-in={forecast['projected_all_in_cpu_seconds']:.3f}s"
        )
    return forecast


def _bridge_enforce(*, accounted_cpu: float, current_stage_cpu: float,
                    costs: Any, remaining_games: int,
                    remaining_optimizer_cpu: float) -> dict[str, Any]:
    del costs, remaining_games, remaining_optimizer_cpu, current_stage_cpu
    from resource_amendment_r2 import ResourceBudgetExceeded
    _raise_if_tagging_failed()
    if TRACKER is None:
        raise ResourceBudgetExceeded("RESOURCE_BLOCKED: bridge CPU tracker is not active")
    tracked_stage_cpu = TRACKER.current_stage_cpu()
    if os.getpid() != TRACKER.parent_pid:
        # Worker-side /proc accounting starts when the tracker is created;
        # include earlier parent setup/tests that parent-side process_time sees.
        tracked_stage_cpu += BRIDGE_TRACKER_SETUP_CPU
    actual = float(accounted_cpu) + tracked_stage_cpu
    active = TRACKER.active_worker_count()
    with TRACKER.max_game_cpu.get_lock():
        max_game = float(TRACKER.max_game_cpu.value)
    try:
        enforce_actual_caps(
            actual_cpu=actual, in_flight_workers=active, max_game_cpu=max_game,
            bridge_baseline_cpu=BRIDGE_BASELINE_CPU,
            bridge_spent_cpu=max(0.0, actual - BRIDGE_BASELINE_CPU),
        )
    except CostRegimeError as exc:
        raise ResourceBudgetExceeded(str(exc)) from exc
    return {
        "status": "PASS", "scope": "authorized zero-update 59-game cost bridge",
        "actual_builder_cpu_seconds": actual,
        "bridge_additional_cpu_seconds": max(0.0, actual - BRIDGE_BASELINE_CPU),
        "bridge_limit_cpu_seconds": BRIDGE_CPU_LIMIT,
        "builder_cap_cpu_seconds": BUILDER_CAP,
        "reviewer_reserve_cpu_seconds": 20000.0,
        "process_cap_cpu_seconds": PROCESS_CAP,
    }


def _bridge_start_record(run_study: Any, model_sha: str, config_sha: str) -> dict[str, Any]:
    global PRODUCER_COMMIT
    manifest = producer_manifest(HERE)
    commit = git(HERE, "rev-parse", "HEAD")
    if BRIDGE_START.is_file():
        record = _read_json(BRIDGE_START)
        producer_commit = str(record.get("producer_commit", ""))
        if producer_commit != PRODUCER_COMMIT or record.get("producer_files") != manifest:
            raise CostRegimeError("bridge cannot resume with modified producer source")
        try:
            git(HERE, "merge-base", "--is-ancestor", producer_commit, commit)
            for name, digest in manifest.items():
                actual = hashlib.sha256(git(HERE, "show", f"{producer_commit}:studies/issue-60-onpolicy-joint-choice/{name}")).hexdigest()
                if actual != digest:
                    raise CostRegimeError(f"bridge producer commit does not contain {name}")
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            if isinstance(exc, CostRegimeError):
                raise
            raise CostRegimeError("bridge producer commit is not an ancestor of the current checkout") from exc
        if record.get("zero_update_model_sha256") != model_sha or record.get("config_sha256") != config_sha:
            raise CostRegimeError("bridge resume checkpoint/config differs from original zero-update state")
        PRODUCER_COMMIT = producer_commit
        return record
    batch_seeds = list(run_study.TRAINING_SEEDS["block1"][:128])
    completed = []
    for seed in batch_seeds:
        output = run_study._training_game_dir("block1", 1, int(seed))
        if run_study.successful_attempt(output, model_sha, config_sha) is not None:
            completed.append(int(seed))
    if completed != list(range(660001, 660070)):
        raise CostRegimeError(
            f"expected the existing complete 660001..660069 prefix only; found {len(completed)} keys"
        )
    missing = sorted(set(batch_seeds) - set(completed))
    validate_bridge_population(missing)
    record = {
        "schema_id": "issue60-cost-bridge-start-v1",
        "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
        "regime_id": REGIME_ID,
        "producer_commit": PRODUCER_COMMIT,
        "producer_files": manifest,
        "zero_update_model_sha256": model_sha,
        "config_sha256": config_sha,
        "previous_actual_builder_cpu_seconds": BRIDGE_BASELINE_CPU,
        "previous_ledger_line_count": _read_json(BRIDGE_BUDGET_START)["previous_ledger_line_count"],
        "originally_missing_seeds": missing,
        "existing_complete_seeds": completed,
        "stage_key": BRIDGE_STAGE,
        "policy_updates_before_bridge_completion": 0,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    atomic_json(BRIDGE_START, record)
    return record


def _run_focused_tests() -> dict[str, Any]:
    import test_prospective_cost_regime
    # The runner may need GIT_DIR/GIT_WORK_TREE to compensate for this
    # Windows-managed worktree's metadata path when running under WSL.  Tests
    # create temporary fixture repositories, so isolate those subprocesses
    # from the active checkout's Git environment.
    saved_git_env = {name: os.environ.pop(name, None)
                     for name in ("GIT_DIR", "GIT_WORK_TREE")}
    try:
        suite = unittest.defaultTestLoader.loadTestsFromModule(test_prospective_cost_regime)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        for name, value in saved_git_env.items():
            if value is not None:
                os.environ[name] = value
    if not result.wasSuccessful():
        raise CostRegimeError("focused prospective cost-regime tests failed")
    return {"status": "PASS", "test_count": result.testsRun}


def _restore_runtime(*, run_game: Any, model_loader: Any,
                     run_accounted: Any, run_budget_guard: Any,
                     controlled_executor: Any, trainer_loss: Any) -> None:
    RUN_STUDY.run_game = run_game
    RUN_STUDY._load_final_model = model_loader
    RUN_STUDY._accounted_cpu = run_accounted
    RUN_STUDY.budget_guard = run_budget_guard
    from sts_combat_rl.sim import controlled_run
    import trainer
    controlled_run.execute_controlled_run = controlled_executor
    trainer.ppo_minibatch_loss = trainer_loss
    R2.enforce_budget = _dynamic_enforce
    R2.RUN_STUDY = None


def _run_bridge(*, started: float | None = None) -> int:
    global RUN_STUDY, R2, R2_MODULE, PARALLEL, TRACKER, DISPATCHER
    global BRIDGE_START_CPU, BRIDGE_BASELINE_CPU, BRIDGE_TRACKER_SETUP_CPU
    global PRODUCER_COMMIT, PRODUCER_HASHES
    global CACHED_STATE
    if started is None:
        started = time.process_time()
    _bridge_budget_start_record()
    try:
        os.setpgid(0, 0)
    except OSError:
        if os.getpgrp() != os.getpid():
            raise
    tests = _run_focused_tests()
    import run_study
    import run_study_r2
    import run_study_parallel
    import resource_amendment_r2
    import parallel_execution
    from parallel_execution import ParallelGameDispatcher, ProcessCpuTracker
    from guard_optimization import GuardState, install_cached_guard
    from sts_combat_rl.sim import controlled_run
    import trainer

    RUN_STUDY, R2, R2_MODULE, PARALLEL = run_study, run_study_r2, resource_amendment_r2, run_study_parallel
    BRIDGE_START_CPU = started

    r1 = __import__("resource_amendment_r1").verify_resource_chain(HERE, require_gate_seal=True)
    r2_receipt = R2.verify_r2_receipt(HERE, require_seal=True)
    R2._read_json(EVIDENCE / "preflight" / "resource-r2.json")
    parallel_receipt = PARALLEL._read_validation_receipt()
    if r1.get("status") != "PASS" or r2_receipt.get("gate", {}).get("status") != "PASS":
        raise CostRegimeError("retained R1/R2 receipts do not verify")
    config, runtime, vocab, bootstrap, model, model_sha = PARALLEL._load_validation_inputs()
    del runtime
    config_sha = run_study.config_sha()
    record = _bridge_start_record(run_study, model_sha, config_sha)
    BRIDGE_BASELINE_CPU = float(record["previous_actual_builder_cpu_seconds"])
    try:
        enforce_actual_caps(
            actual_cpu=STATIC_ALLOWANCE + _ledger_total(), in_flight_workers=0,
            max_game_cpu=0.0, bridge_baseline_cpu=BRIDGE_BASELINE_CPU,
            bridge_spent_cpu=max(0.0, STATIC_ALLOWANCE + _ledger_total() - BRIDGE_BASELINE_CPU),
        )
    except CostRegimeError as exc:
        from resource_amendment_r2 import ResourceBudgetExceeded
        raise ResourceBudgetExceeded(str(exc)) from exc
    _refresh_bridge_tags(run_study, model_sha, config_sha, record)
    # The tracker begins at process entry so imports, prerequisite checks and
    # focused tests are part of the bounded 5,000 CPU-s authorization.
    BRIDGE_TRACKER_SETUP_CPU = max(0.0, time.process_time() - started)
    TRACKER = ProcessCpuTracker.start(started)
    with TRACKER.max_game_cpu.get_lock():
        TRACKER.max_game_cpu.value = run_study._max_observed_game_seconds()
    DISPATCHER = ParallelGameDispatcher(run_study, TRACKER, 2)
    prior_run_game = run_study.run_game
    prior_model_loader = run_study._load_final_model
    prior_accounted = run_study._accounted_cpu
    prior_budget = run_study.budget_guard
    prior_controlled = controlled_run.execute_controlled_run
    prior_loss = trainer.ppo_minibatch_loss
    R2.STAGE_START_CPU = started
    R2.MINIBATCHES_COMPLETED = 0
    R2.enforce_budget = _bridge_enforce
    R2._install_runtime_hooks()
    CACHED_STATE = GuardState(HERE, R2_MODULE)
    install_cached_guard(
        run_study, R2, R2_MODULE, CACHED_STATE,
        parallel_module=parallel_execution,
    )
    # _install_runtime_hooks sets the R2 hooks; the validated two-worker
    # dispatcher is installed afterward and only dispatches missing batch keys.
    run_study.run_game = DISPATCHER.dispatch_run_game
    run_study._load_final_model = DISPATCHER.wrap_model_loader()
    _install_tagged_dispatcher()
    status = "FAILED"
    failure = None
    gate: dict[str, Any] | None = None
    try:
        kwargs = {
            "seed": BRIDGE_SEEDS[0], "mode": "residual", "model": model,
            "block_name": "block1",
            "runtime_seed": run_study.derive_seed(
                "training-policy", run_study.MODEL_SEEDS["block1"], BRIDGE_SEEDS[0], 1,
            ),
            "output_dir": run_study._training_game_dir("block1", 1, BRIDGE_SEEDS[0]),
            "model_sha": model_sha, "config_digest": config_sha,
            "vocab": vocab, "bootstrap": bootstrap, "require_targets": True,
            "provenance_config": run_study._current_policy_provenance(
                config, vocab, model_sha, "residual",
            ),
            "stage_key": BRIDGE_STAGE, "arm": "residual",
        }
        run_study.run_game(**kwargs)
        _refresh_bridge_tags(run_study, model_sha, config_sha, record)
        sample_rows = [row for row in load_cost_tags(HERE, current_manifest=PRODUCER_HASHES)
                       if row["stage_key"] == BRIDGE_STAGE and int(row["game_seed"]) in BRIDGE_SEEDS]
        sample_seeds = sorted(int(row["game_seed"]) for row in sample_rows)
        validate_bridge_population(sample_seeds)
        if len(sample_rows) != len(BRIDGE_SEEDS):
            raise CostRegimeError("bridge tag sample does not contain exactly 59 unique games")
        costs = [float(row["process_cpu_seconds"]) for row in sample_rows]
        optimizer = float(run_study._remaining_optimizer_projection())
        current = STATIC_ALLOWANCE + _ledger_total() + TRACKER.current_stage_cpu()
        forecast = R2_MODULE.build_forecast(
            costs=costs, remaining_games=2560,
            remaining_optimizer_cpu=optimizer,
            accounted_cpu=current + FINALIZATION_RESERVE,
            builder_budget=BUILDER_CAP, reviewer_reserve=20000.0,
            process_cap=PROCESS_CAP,
        )
        old_forecast = parallel_receipt["formal_grid_forecast_at_validation"]
        if old_forecast.get("status") != "RESOURCE_BLOCKED":
            raise CostRegimeError("the retained historical parallel-validation forecast is not the expected blocked receipt")
        gate = {
            "schema_id": "issue60-prospective-bridge-gate-v1",
            "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
            "status": forecast["status"],
            "source_commit": PRODUCER_COMMIT,
            "source_files": PRODUCER_HASHES,
            "bridge_start_sha256": hashlib.sha256(BRIDGE_START.read_bytes()).hexdigest(),
            "parallel_receipt_sha256": hashlib.sha256((EVIDENCE / "validation" / "parallel-execution" / "receipt.json").read_bytes()).hexdigest(),
            "old_r2_forecast": {
                "status": old_forecast["status"],
                "projected_builder_cpu_seconds": old_forecast["projected_builder_cpu_seconds"],
                "projected_all_in_cpu_seconds": old_forecast["projected_all_in_cpu_seconds"],
                "receipt_sha256": hashlib.sha256((EVIDENCE / "validation" / "parallel-execution" / "receipt.json").read_bytes()).hexdigest(),
                "role": "historical/comparative only; unchanged",
            },
            "bridge": {
                "stage_key": BRIDGE_STAGE, "seeds": sample_seeds,
                "completed_games": len(sample_rows), "required_games": 59,
                "worker_count": DISPATCHER.workers,
                "execution_evidence": DISPATCHER.evidence(),
                "cost_observations": sample_rows,
                "focused_tests": tests,
                "policy_updates_before_gate": 0,
            },
            "unchanged_remaining_grid_games": 2560,
            "remaining_optimizer_projection": run_study._remaining_optimizer_projection(),
            "forecast": forecast,
            "gate_role": "prospective cached-regime unit-cost gate; operational heuristic, not guarantee",
            "decision": "continue unchanged program automatically" if forecast["status"] == "PASS" else "stop and return to Planner",
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        atomic_json(PROSPECTIVE / "bridge-gate.json", gate)
        status = "COMPLETE" if forecast["status"] == "PASS" else "RESOURCE_BLOCKED"
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {exc}"
        status = "RESOURCE_BLOCKED" if "RESOURCE_BLOCKED" in str(exc) else "FAILED"
        if status != "RESOURCE_BLOCKED":
            raise
        print(f"Bridge resource stop: {exc}", file=sys.stderr)
    finally:
        try:
            _refresh_bridge_tags(run_study, model_sha, config_sha, record)
        except BaseException as exc:
            failure = failure or f"tag finalization: {type(exc).__name__}: {exc}"
            status = "FAILED"
        stage_cpu = TRACKER.current_stage_cpu() if TRACKER is not None else max(0.0, time.process_time() - started)
        additional = max(0.0, STATIC_ALLOWANCE + _ledger_total() + stage_cpu - BRIDGE_BASELINE_CPU)
        final_state = {
            "schema_id": "issue60-cost-bridge-stage-v1",
            "status": status,
            "failure": failure,
            "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
            "producer_commit": PRODUCER_COMMIT,
            "producer_files": PRODUCER_HASHES,
            "process_cpu_seconds": stage_cpu,
            "bridge_cumulative_additional_cpu_seconds_before_finalization_charge": additional,
            "bridge_limit_cpu_seconds": BRIDGE_CPU_LIMIT,
            "finalization_reserve_seconds": FINALIZATION_RESERVE,
            "forecast_gate": gate,
            "dispatcher": DISPATCHER.evidence() if DISPATCHER is not None else None,
            "completed_bridge_tag_count": len([row for row in load_cost_tags(HERE, current_manifest=PRODUCER_HASHES)
                                                 if row["stage_key"] == BRIDGE_STAGE and int(row["game_seed"]) in BRIDGE_SEEDS]),
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        atomic_json(BRIDGE_LATEST, final_state)
        stage_cpu = TRACKER.current_stage_cpu() if TRACKER is not None else max(0.0, time.process_time() - started)
        charge = stage_cpu + 0.2
        projected_additional = max(0.0, STATIC_ALLOWANCE + _ledger_total() + charge - BRIDGE_BASELINE_CPU)
        if projected_additional > BRIDGE_CPU_LIMIT:
            status = "RESOURCE_BLOCKED"
        R2._append_ledger({
            "stage": "issue60-cached-cost-bridge", "status": status,
            "attempt": git(HERE, "rev-parse", "--short", "HEAD") + "-" + str(sum(
                1 for line in (EVIDENCE / "cpu-ledger.jsonl").read_text(encoding="utf-8").splitlines()
                if '"stage":"issue60-cached-cost-bridge"' in line
            ) + 1),
            "process_cpu_seconds": charge,
            "process_cpu_seconds_measured": stage_cpu,
            "finalization_margin_seconds": 0.2,
            "bridge_cumulative_additional_cpu_seconds_charged": projected_additional,
            "bridge_limit_cpu_seconds": BRIDGE_CPU_LIMIT,
            "builder_cap_cpu_seconds": BUILDER_CAP,
            "reviewer_reserve_cpu_seconds": 20000.0,
            "process_cap_cpu_seconds": PROCESS_CAP,
            "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
            "source_commit": PRODUCER_COMMIT,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "accounting": "parent process CPU plus each worker wait4 user/system CPU exactly once; includes setup, focused checks and failures, plus 0.2s finalization margin",
        })
        final_state["status"] = status
        final_state["process_cpu_seconds_charged"] = charge
        final_state["bridge_cumulative_additional_cpu_seconds_charged"] = projected_additional
        atomic_json(BRIDGE_LATEST, final_state)
        _restore_runtime(
            run_game=prior_run_game, model_loader=prior_model_loader,
            run_accounted=prior_accounted, run_budget_guard=prior_budget,
            controlled_executor=prior_controlled, trainer_loss=prior_loss,
        )
        TRACKER = None
        DISPATCHER = None
    if status != "COMPLETE":
        return 75 if status == "RESOURCE_BLOCKED" else 2
    print(json.dumps({"prospective_cost_bridge": gate}, sort_keys=True))
    return 0


def _run_continue() -> int:
    global RUN_STUDY, R2, R2_MODULE, PARALLEL, PRODUCER_COMMIT, PRODUCER_HASHES
    import run_study
    import run_study_r2
    import run_study_parallel
    import resource_amendment_r2
    import trainer
    from sts_combat_rl.sim import controlled_run

    RUN_STUDY, R2, R2_MODULE, PARALLEL = run_study, run_study_r2, resource_amendment_r2, run_study_parallel
    current_commit = git(HERE, "rev-parse", "HEAD")
    current_hashes = producer_manifest(HERE)
    bridge = _read_json(BRIDGE_LATEST)
    gate = _read_json(PROSPECTIVE / "bridge-gate.json")
    if bridge.get("status") != "COMPLETE" or gate.get("status") != "PASS":
        raise CostRegimeError("automatic continuation requires a complete passing 59-game bridge")
    source_commit = str(gate.get("source_commit", ""))
    if current_hashes != gate.get("source_files"):
        raise CostRegimeError("source changed after prospective bridge gate; continuation is not authorized")
    try:
        git(HERE, "merge-base", "--is-ancestor", source_commit, current_commit)
        for name, digest in current_hashes.items():
            actual = hashlib.sha256(git(HERE, "show", f"{source_commit}:studies/issue-60-onpolicy-joint-choice/{name}")).hexdigest()
            if actual != digest:
                raise CostRegimeError(f"prospective gate producer commit does not contain {name}")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, CostRegimeError):
            raise
        raise CostRegimeError("prospective gate producer is not an ancestor of the current checkout") from exc
    PRODUCER_COMMIT = source_commit
    PRODUCER_HASHES = current_hashes
    sample = [row for row in load_cost_tags(HERE, current_manifest=PRODUCER_HASHES)
              if row["stage_key"] == BRIDGE_STAGE and int(row["game_seed"]) in BRIDGE_SEEDS]
    validate_bridge_population(int(row["game_seed"]) for row in sample)
    if len(sample) != 59:
        raise CostRegimeError("prospective bridge cost sample no longer verifies")

    baseline_run_game = RUN_STUDY.run_game
    baseline_loader = RUN_STUDY._load_final_model
    baseline_accounted = RUN_STUDY._accounted_cpu
    baseline_budget = RUN_STUDY.budget_guard
    baseline_controlled = controlled_run.execute_controlled_run
    baseline_loss = trainer.ppo_minibatch_loss
    raw_stage = R2._run_original_stage

    def reset_each_stage(stage: str, *, stage_start_cpu: float) -> int:
        R2.RUN_STUDY = None
        try:
            return raw_stage(stage, stage_start_cpu=stage_start_cpu)
        finally:
            dispatcher = getattr(PARALLEL, "DISPATCHER", None)
            RUN_STUDY.run_game = dispatcher.raw_run_game if dispatcher is not None else baseline_run_game
            RUN_STUDY._load_final_model = baseline_loader
            RUN_STUDY._accounted_cpu = baseline_accounted
            RUN_STUDY.budget_guard = baseline_budget
            controlled_run.execute_controlled_run = baseline_controlled
            trainer.ppo_minibatch_loss = baseline_loss
            R2.enforce_budget = _dynamic_enforce
            R2.RUN_STUDY = None

    R2.enforce_budget = _dynamic_enforce
    R2._run_original_stage = reset_each_stage
    sys.argv = ["run_study_parallel.py", "--stage", "continue"]
    try:
        return int(PARALLEL.main())
    finally:
        R2._run_original_stage = raw_stage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("bridge", "continue"), required=True)
    stage = parser.parse_args().stage
    if stage == "bridge":
        started = time.process_time()
        ledger = EVIDENCE / "cpu-ledger.jsonl"
        before = ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else []
        try:
            result = _run_bridge(started=started)
        except BaseException as exc:
            after = ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else []
            if not any('"stage":"issue60-cached-cost-bridge"' in row for row in after[len(before):]):
                blocked = "RESOURCE_BLOCKED" in str(exc)
                charge = max(0.0, time.process_time() - started) + 0.2
                source_commit = PRODUCER_COMMIT or "unresolved-before-bridge-start"
                baseline = 0.0
                if BRIDGE_BUDGET_START.is_file():
                    budget_start = _read_json(BRIDGE_BUDGET_START)
                    baseline = float(budget_start.get("previous_actual_builder_cpu_seconds", 0.0))
                    source_commit = str(budget_start.get("producer_commit", source_commit))
                if BRIDGE_START.is_file():
                    baseline = float(_read_json(BRIDGE_START).get("previous_actual_builder_cpu_seconds", 0.0))
                prior = STATIC_ALLOWANCE + _ledger_total()
                projected = max(0.0, prior + charge - baseline) if baseline else charge
                atomic_json(BRIDGE_LATEST, {
                    "schema_id": "issue60-cost-bridge-stage-v1",
                    "status": "RESOURCE_BLOCKED" if blocked else "FAILED",
                    "failure": f"{type(exc).__name__}: {exc}",
                    "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
                    "source_commit": source_commit,
                    "process_cpu_seconds": charge,
                    "bridge_cumulative_additional_cpu_seconds_charged": projected,
                    "bridge_limit_cpu_seconds": BRIDGE_CPU_LIMIT,
                    "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                _append_ledger({
                    "stage": "issue60-cached-cost-bridge-setup-failure",
                    "status": "RESOURCE_BLOCKED" if blocked else "FAILED",
                    "process_cpu_seconds": charge,
                    "bridge_cumulative_additional_cpu_seconds_charged": projected,
                    "bridge_limit_cpu_seconds": BRIDGE_CPU_LIMIT,
                    "finalization_margin_seconds": 0.2,
                    "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
                    "source_commit": source_commit,
                    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "accounting": "parent process CPU for bridge setup/check failure before dispatch plus 0.2s finalization margin",
                })
                if blocked:
                    return 75
            raise
        if result != 0:
            return result
        # Planner explicitly authorized immediate continuation after a complete
        # bridge and passing prospective gate; no second launch/permission step.
        return _run_continue()
    return _run_continue()


if __name__ == "__main__":
    raise SystemExit(main())
