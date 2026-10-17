"""Resume the authorized Issue 60 program with its accepted two-worker witness.

The old R3 validation receipt records a RESOURCE_BLOCKED forecast. Issue #60's
Planner decision explicitly supersedes that forecast after the 59-game cached
regime bridge passes and authorizes use of the already-reviewed two-worker
replay. This adapter supplies run_study_parallel's in-memory receipt interface
from that accepted witness without replacing or rewriting the historical R3
receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any

import run_cost_bridge as bridge
import run_study_parallel as parallel

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "evidence"
PROSPECTIVE = EVIDENCE / "prospective-cost"
BINDING = PROSPECTIVE / "continuation-validation-binding.json"
LIFECYCLE = PROSPECTIVE / "continuation-lifecycle.jsonl"
AUTHORIZATION = bridge.AUTHORIZATION_COMMENT_ID
DIAGNOSTIC_AUTHORIZATION = 6231868984
DIAGNOSTIC_CPU_LIMIT = 1000.0
DIAGNOSTIC_CODE_AND_TEST_RESERVE = 60.0
DIAGNOSTIC_REPLAY_SEEDS = (660129, 660239)
DIAGNOSTIC_STAGE = "issue60-cached-guard-diagnostic"
DIAGNOSTIC_LEDGER_PREFIX = "issue60-continuation-cached-guard-diagnostic-"
DIAGNOSTIC_EVIDENCE = EVIDENCE / "validation" / "continuation-cache-guard"
DIAGNOSTIC_REPORT = DIAGNOSTIC_EVIDENCE / "replay-report.json"
ATTRIBUTION_REPORT = PROSPECTIVE / "continuation-cache-attribution.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _diagnostic_ledger_rows() -> list[dict[str, Any]]:
    path = EVIDENCE / "cpu-ledger.jsonl"
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return [
        row for row in rows
        if str(row.get("attempt", "")).startswith(DIAGNOSTIC_LEDGER_PREFIX)
        or str(row.get("stage", "")).startswith(DIAGNOSTIC_STAGE)
    ]


def _diagnostic_stage_receipts() -> list[dict[str, Any]]:
    root = EVIDENCE / "validation" / "parallel-execution" / "stages"
    paths = [root / f"{DIAGNOSTIC_STAGE}.json"]
    paths.extend(sorted(root.glob(f"{DIAGNOSTIC_STAGE}-attempt-*.json")))
    rows = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append({
            "path": path.relative_to(HERE).as_posix(),
            "sha256": _sha256(path),
            "stage": receipt.get("stage"),
            "status": receipt.get("status"),
            "execution_count": receipt.get("execution_count"),
            "parent_process_cpu_seconds": receipt.get("parent_process_cpu_seconds"),
            "worker_process_cpu_seconds": receipt.get("worker_process_cpu_seconds"),
            "aggregate_process_cpu_seconds": receipt.get("aggregate_process_cpu_seconds"),
            "source_commit": receipt.get("source_commit"),
        })
    return rows


def _next_diagnostic_attempt() -> int:
    numbers = []
    for row in _diagnostic_ledger_rows():
        attempt = str(row.get("attempt", ""))
        if attempt.startswith(DIAGNOSTIC_LEDGER_PREFIX):
            try:
                numbers.append(int(attempt.removeprefix(DIAGNOSTIC_LEDGER_PREFIX)))
            except ValueError as exc:
                raise bridge.CostRegimeError("diagnostic CPU ledger has a malformed attempt id") from exc
    receipt_root = EVIDENCE / "validation" / "parallel-execution" / "stages"
    for path in receipt_root.glob(f"{DIAGNOSTIC_STAGE}-attempt-*.json"):
        suffix = path.stem.removeprefix(f"{DIAGNOSTIC_STAGE}-attempt-")
        try:
            numbers.append(int(suffix))
        except ValueError as exc:
            raise bridge.CostRegimeError("diagnostic stage receipt has a malformed attempt id") from exc
    return max(numbers, default=0) + 1


def _facts() -> dict[str, Any]:
    import run_study
    latest = bridge._read_json(bridge.BRIDGE_LATEST)
    gate = bridge._read_json(PROSPECTIVE / "bridge-gate.json")
    start = bridge._read_json(bridge.BRIDGE_START)
    historical = bridge._read_historical_parallel_receipt(parallel)
    accepted = bridge._verify_accepted_two_worker_evidence()
    source_hashes = bridge.producer_manifest(HERE)
    if latest.get("status") != "COMPLETE" or latest.get("completed_bridge_tag_count") != 59:
        raise bridge.CostRegimeError("the authorized 59-game bridge is not complete")
    if (gate.get("status") != "PASS" or gate.get("source_files") != source_hashes
            or gate.get("source_commit") != start.get("producer_commit")):
        raise bridge.CostRegimeError("the passing prospective gate does not bind the current producer")
    forecast = gate.get("forecast", {})
    if (forecast.get("status") != "PASS" or forecast.get("remaining_games") != 2560
            or forecast.get("observed_complete_games") != 59):
        raise bridge.CostRegimeError("the bridge forecast does not pass for the fixed remaining grid")
    bridge_gate = gate.get("bridge", {})
    if (bridge_gate.get("completed_games") != 59 or bridge_gate.get("worker_count") != 2):
        raise bridge.CostRegimeError("bridge completion or worker evidence is not the authorized population")
    evidence_path = bridge.ACCEPTED_TWO_WORKER_EVIDENCE
    evidence_sha = _sha256(evidence_path)
    if start.get("accepted_two_worker_evidence_sha256") != evidence_sha:
        raise bridge.CostRegimeError("bridge-start record no longer binds accepted two-worker evidence")
    if (historical.get("status") != "RESOURCE_BLOCKED"
            or historical.get("formal_grid_forecast_at_validation", {}).get("status") != "RESOURCE_BLOCKED"):
        raise bridge.CostRegimeError("historical R3 receipt is not the preserved blocked record")
    if historical.get("training_progress", {}).get("ppo_updates") != 0:
        raise bridge.CostRegimeError("historical R3 receipt indicates an unexpected PPO update")
    if accepted.get("status") != "PASS":
        raise bridge.CostRegimeError("Planner-accepted two-worker evidence no longer passes")

    config = json.loads(run_study.CONFIG_PATH.read_text(encoding="utf-8"))
    run_study.validate_config(config)
    runtime = run_study.verify_runtime(config)

    bridge.PRODUCER_COMMIT = str(gate["source_commit"])
    bridge.PRODUCER_HASHES = source_hashes
    costs = bridge._cost_sample(bridge_only=True)
    if len(costs) != 59:
        raise bridge.CostRegimeError("prospective cost sample is not the exact 59-game bridge")

    runner_hashes = {
        relative: parallel._sha256(HERE / relative)
        for relative in parallel.PARALLEL_SOURCE_FILES
    }
    return {
        "authorization_comment_id": AUTHORIZATION,
        "producer_commit": bridge.PRODUCER_COMMIT,
        "producer_files": source_hashes,
        "bridge_latest_status": latest.get("status"),
        "completed_bridge_tag_count": latest.get("completed_bridge_tag_count"),
        "bridge_gate_sha256": _sha256(PROSPECTIVE / "bridge-gate.json"),
        "bridge_start_sha256": _sha256(bridge.BRIDGE_START),
        "accepted_two_worker_evidence_path": evidence_path.relative_to(HERE).as_posix(),
        "accepted_two_worker_evidence_sha256": evidence_sha,
        "accepted_two_worker_source_commit": accepted.get("source_commit"),
        "accepted_two_worker_seed_keys": sorted(
            int(row["seed"]) for row in accepted.get("replays", {}).get("cached_two_worker", [])
        ),
        "accepted_two_worker_dispatcher": {
            key: accepted["two_worker_dispatcher_evidence"].get(key)
            for key in ("execution_count", "max_active_workers", "max_configured_workers")
        },
        "accepted_two_worker_parity": [
            {key: row.get(key) for key in ("status", "decision_rows")}
            for row in accepted.get("two_worker_parallel_comparisons_to_cached_serial", [])
        ],
        "historical_r3_receipt_path": parallel.PARALLEL_RECEIPT.relative_to(HERE).as_posix(),
        "historical_r3_receipt_sha256": _sha256(parallel.PARALLEL_RECEIPT),
        "historical_r3_receipt_status": historical.get("status"),
        "historical_r3_validation_status": historical.get("validation", {}).get("status"),
        "execution_source_files": runner_hashes,
        "runtime": {
            key: runtime.get(key) for key in (
                "python", "torch", "torch_threads", "deterministic_algorithms",
                "stsr_source", "stsr_commit", "native_source", "native_commit",
                "native_binary_sha256",
            )
        },
        "selected_worker_count": 2,
        "remaining_games": 2560,
        "forecast_projected_builder_cpu_seconds": forecast.get("projected_builder_cpu_seconds"),
        "forecast_projected_all_in_cpu_seconds": forecast.get("projected_all_in_cpu_seconds"),
        "interpretation": (
            "Use the independently Planner-accepted two-worker replay witness as the existing "
            "run_study_parallel validation interface. Preserve the historical R3 RESOURCE_BLOCKED "
            "receipt byte-for-byte; its old cost forecast is superseded only by the passing "
            "prospective bridge gate. No simulation or PPO update occurs during this check."
        ),
    }


def _recovery_basis() -> dict[str, Any] | None:
    if not LIFECYCLE.is_file() or not LIFECYCLE.read_text(encoding="utf-8").strip():
        return None
    rows = [json.loads(line) for line in LIFECYCLE.read_text(encoding="utf-8").splitlines() if line]
    if (len(rows) != 2 or rows[0].get("state") != "STARTED"
            or rows[1].get("state") != "FAILED"):
        raise bridge.CostRegimeError("prior continuation state is not a single recoverable pre-dispatch failure")
    failure = str(rows[1].get("failure", ""))
    expected_failure = "CalledProcessError: Command '['git', '-C', '/home/lsmft/sts_lightspeed-issue59'"
    stage_path = EVIDENCE / "validation" / "parallel-execution" / "stages" / "train.json"
    status_path = EVIDENCE / "stage-status" / "train.json"
    stage = bridge._read_json(stage_path)
    status = bridge._read_json(status_path)
    if (not failure.startswith(expected_failure)
            or stage.get("status") != "FAILED" or stage.get("execution_count") != 0
            or stage.get("worker_process_cpu_seconds") != 0.0
            or status.get("status") != "FAILED"
            or "/home/lsmft/sts_lightspeed-issue59" not in str(status.get("failure", ""))):
        raise bridge.CostRegimeError("prior failed stage is not the verified no-dispatch runtime-path failure")
    archived = []
    for source, destination in (
        (stage_path, stage_path.with_name("train-pre-dispatch-failure-0001.json")),
        (status_path, status_path.with_name("train-pre-dispatch-failure-0001.json")),
    ):
        if destination.exists():
            if destination.read_bytes() != source.read_bytes():
                raise bridge.CostRegimeError(f"pre-dispatch failure archive already differs: {destination}")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        archived.append({
            "path": destination.relative_to(HERE).as_posix(),
            "sha256": _sha256(destination),
        })
    return {
        "failure": failure,
        "parallel_stage_status": stage.get("status"),
        "parallel_execution_count": stage.get("execution_count"),
        "worker_process_cpu_seconds": stage.get("worker_process_cpu_seconds"),
        "archived_attempt_records": archived,
        "no_game_or_update_dispatched": True,
    }


def _verify_recorded_recovery_basis(recovery: dict[str, Any] | None) -> None:
    """Verify the original zero-dispatch recovery proof after later attempts."""
    if recovery is None:
        if LIFECYCLE.is_file() and LIFECYCLE.read_text(encoding="utf-8").strip():
            raise bridge.CostRegimeError("continuation lifecycle exists without its recorded recovery proof")
        return
    expected_paths = {
        "evidence/validation/parallel-execution/stages/train-pre-dispatch-failure-0001.json",
        "evidence/stage-status/train-pre-dispatch-failure-0001.json",
    }
    archived = recovery.get("archived_attempt_records", [])
    observed_paths = {str(row.get("path")) for row in archived}
    if (
        not recovery.get("no_game_or_update_dispatched")
        or recovery.get("parallel_stage_status") != "FAILED"
        or recovery.get("parallel_execution_count") != 0
        or float(recovery.get("worker_process_cpu_seconds", -1.0)) != 0.0
        or observed_paths != expected_paths
    ):
        raise bridge.CostRegimeError("recorded continuation recovery proof is malformed")
    for row in archived:
        path = HERE / str(row["path"])
        if not path.is_file() or _sha256(path) != row.get("sha256"):
            raise bridge.CostRegimeError("archived pre-dispatch recovery evidence changed")
    if not LIFECYCLE.is_file():
        raise bridge.CostRegimeError("recorded recovery proof has no continuation lifecycle")
    rows = [json.loads(line) for line in LIFECYCLE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if (
        len(rows) < 3
        or rows[0].get("state") != "STARTED"
        or rows[1].get("state") != "FAILED"
        or rows[1].get("failure") != recovery.get("failure")
        or rows[2].get("state") != "RETRY_AUTHORIZED"
        or rows[2].get("recovery_basis") != recovery
    ):
        raise bridge.CostRegimeError("continuation lifecycle no longer binds its original recovery proof")


def _write_binding(facts: dict[str, Any], recovery: dict[str, Any] | None) -> dict[str, Any]:
    record = {
        "schema_id": "issue60-continuation-validation-binding-v1",
        "status": "PASS",
        "validated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "launcher_sha256": _sha256(Path(__file__)),
        "facts": facts,
        "recovery_basis": recovery,
    }
    bridge.atomic_json(BINDING, record)
    return record


def _verified_binding() -> dict[str, Any]:
    stored = bridge._read_json(BINDING)
    if stored.get("schema_id") != "issue60-continuation-validation-binding-v1" or stored.get("status") != "PASS":
        raise bridge.CostRegimeError("continuation validation binding is missing or invalid")
    if stored.get("launcher_sha256") != _sha256(Path(__file__)):
        raise bridge.CostRegimeError("continuation launcher changed after validation")
    if stored.get("facts") != _facts():
        raise bridge.CostRegimeError("continuation validation facts changed after bridge gate")
    return stored


def _validation_receipt() -> dict[str, Any]:
    binding = _verified_binding()
    return {
        "schema_id": "issue60-in-memory-accepted-two-worker-binding-v1",
        "status": "PASS",
        "validation": {"status": "PASS", "basis": BINDING.relative_to(HERE).as_posix()},
        "source_files": binding["facts"]["execution_source_files"],
        "worker_selection": {"worker_count": 2},
        "binding_sha256": _sha256(BINDING),
    }


def _append_lifecycle(row: dict[str, Any]) -> None:
    LIFECYCLE.parent.mkdir(parents=True, exist_ok=True)
    with LIFECYCLE.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _record_bridge_charge(*, attempt: str, stage: str, seconds: float,
                          status: str, accounting: str, source_commit: str) -> None:
    ledger = EVIDENCE / "cpu-ledger.jsonl"
    existing = ledger.read_text(encoding="utf-8").splitlines() if ledger.is_file() else []
    if any(f'"attempt":"{attempt}"' in line for line in existing):
        return
    charge = max(0.0, float(seconds))
    bridge._append_ledger({
        "attempt": attempt,
        "authorization_comment_id": AUTHORIZATION,
        "stage": stage,
        "status": status,
        "process_cpu_seconds": charge,
        "source_commit": source_commit,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "accounting": accounting,
    })
    latest = bridge._read_json(bridge.BRIDGE_LATEST)
    previous = float(latest.get("bridge_cumulative_additional_cpu_seconds_charged", 0.0))
    latest["bridge_cumulative_additional_cpu_seconds_charged"] = previous + charge
    latest.setdefault("continuation_accounting", []).append({
        "attempt": attempt, "stage": stage, "status": status,
        "process_cpu_seconds": charge,
    })
    bridge.atomic_json(bridge.BRIDGE_LATEST, latest)


def _wrap_runtime_hooks_with_cache(original: Any, install_cache: Any,
                                   events: list[str]) -> Any:
    """Install the cache once, after R2 hooks and before its dispatcher is built."""
    installed = False

    def install() -> Any:
        nonlocal installed
        result = original()
        if not installed:
            events.append("original-r2-runtime-hooks")
            install_cache()
            events.append("cached-guard-installed")
            installed = True
        return result

    return install


def _install_cached_guard(bridge_module: Any, execution_module: Any) -> None:
    from guard_optimization import GuardState, install_cached_guard

    if any(value is None for value in (
        bridge_module.RUN_STUDY, bridge_module.R2, bridge_module.R2_MODULE,
    )):
        raise bridge.CostRegimeError("continuation modules are not initialized before cache installation")
    state = bridge_module.CACHED_STATE
    if state is None:
        state = GuardState(bridge_module.HERE, bridge_module.R2_MODULE)
        bridge_module.CACHED_STATE = state
    install_cached_guard(
        bridge_module.RUN_STUDY, bridge_module.R2, bridge_module.R2_MODULE,
        state, parallel_module=execution_module,
    )


def _cached_guard_is_active(bridge_module: Any) -> bool:
    state = bridge_module.CACHED_STATE
    if state is None:
        return False
    return (
        getattr(bridge_module.RUN_STUDY._completed_run_count, "__self__", None) is state
        and getattr(bridge_module.R2.complete_game_costs, "__self__", None) is state
        and getattr(bridge_module.R2_MODULE.complete_game_costs, "__self__", None) is state
    )


def _tag_formal_attempts_only(original: Any, study_root: Path) -> Any:
    """Keep isolated diagnostic replays out of natural-game cost observations."""
    runs_root = (study_root / "evidence" / "runs").resolve()

    def tag(attempt: Path) -> bool:
        resolved = Path(attempt).resolve()
        if runs_root not in resolved.parents:
            return False
        return bool(original(resolved))

    return tag


def _diagnostic_stage(bridge_module: Any, parallel_module: Any,
                      diagnostic_start_cpu: float,
                      context: dict[str, Any]) -> int:
    """Run only the two Planner-named retained keys through the installed hooks."""
    import run_study
    import run_study_r2
    import parallel_execution
    from prospective_cost_regime import enforce_actual_caps
    from resource_amendment_r2 import (
        STATIC_ALLOWANCE_SECONDS, ResourceBudgetExceeded, _ledger_total,
    )
    from sts_combat_rl.sim import controlled_run

    tracker = parallel_module.TRACKER
    if tracker is None:
        raise bridge.CostRegimeError("continuation diagnostic has no aggregate CPU tracker")
    run_study_r2.STAGE_START_CPU = tracker.parent_start_cpu
    run_study_r2.MINIBATCHES_COMPLETED = 0

    # This is the same stage hook used by the standalone continuation. The
    # wrapper installed by _run_continue_with_cost_tagging runs the original R2
    # installer first and the cached guard immediately afterward.
    run_study_r2._install_runtime_hooks()
    state = bridge_module.CACHED_STATE
    if not _cached_guard_is_active(bridge_module):
        raise bridge.CostRegimeError("actual continuation stage did not install the cached guard")
    dispatcher = parallel_module.DISPATCHER
    if dispatcher is None or dispatcher.workers != 2:
        raise bridge.CostRegimeError("actual continuation stage did not install its reviewed two-worker dispatcher")

    import torch
    config = json.loads(run_study.CONFIG_PATH.read_text(encoding="utf-8"))
    run_study.validate_config(config)
    runtime = run_study.verify_runtime(config)
    vocab = run_study.Vocabulary(run_study.HERE / "vocabulary.json")
    bootstrap, _ = run_study.load_bootstrap(
        run_study.HERE / config["bootstrap"]["checkpoint"],
    )
    checkpoint_path = EVIDENCE / "training" / "block1" / "latest.pt"
    if not checkpoint_path.is_file():
        raise bridge.CostRegimeError("retained post-batch-01 checkpoint is missing")
    payload = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    if payload.get("completed_batches") != 1 or payload.get("config_sha256") != run_study.config_sha():
        raise bridge.CostRegimeError("retained checkpoint is not the post-batch-01 model")
    torch.manual_seed(run_study.MODEL_SEEDS["block1"])
    model = run_study.JointChoiceActorCritic(vocab)
    model.load_state_dict(payload["model_state"], strict=True)
    model_sha = run_study.model_fingerprint(model, vocab, run_study.config_sha())
    if model_sha != payload.get("model_sha256"):
        raise bridge.CostRegimeError("retained post-batch-01 checkpoint fingerprint mismatch")
    expected_model_sha = "c05ffb91ed6cb702e9ad135afe3b537c1348f2da8e21958c38141404055bfbb5"
    if model_sha != expected_model_sha:
        raise bridge.CostRegimeError("retained replay checkpoint differs from the Planner-named post-batch-01 model")
    config_digest = run_study.config_sha()
    tasks = []
    originals = {}
    for seed in DIAGNOSTIC_REPLAY_SEEDS:
        original_dir = run_study._training_game_dir("block1", 2, seed)
        original = run_study.successful_attempt(original_dir, model_sha, config_digest)
        if original is None:
            raise bridge.CostRegimeError(f"retained complete batch-02 game {seed} is missing")
        original_attempt, original_summary = original
        expected_runtime_seed = run_study.derive_seed(
            "training-policy", run_study.MODEL_SEEDS["block1"], seed, 2,
        )
        if (
            original_summary.get("stage_key") != "block1:batch-02"
            or int(original_summary.get("game_seed", -1)) != seed
            or original_summary.get("arm") != "residual"
            or original_summary.get("runtime_policy_seed") != expected_runtime_seed
            or original_summary.get("model_sha256") != expected_model_sha
        ):
            raise bridge.CostRegimeError(f"retained replay identity or RNG provenance changed for seed {seed}")
        output_dir = DIAGNOSTIC_EVIDENCE / "replays" / f"seed-{seed}"
        if (EVIDENCE / "runs").resolve() in output_dir.resolve().parents:
            raise bridge.CostRegimeError("diagnostic replays must remain outside formal evidence/runs")
        kwargs = {
            "seed": seed, "mode": "residual", "model": model,
            "block_name": "block1", "runtime_seed": int(original_summary["runtime_policy_seed"]),
            "output_dir": output_dir, "model_sha": model_sha,
            "config_digest": config_digest, "vocab": vocab,
            "bootstrap": bootstrap, "require_targets": True,
            "provenance_config": run_study._current_policy_provenance(
                config, vocab, model_sha, "residual",
            ),
            "stage_key": "block1:batch-02", "arm": "residual",
        }
        key = ("block1:batch-02", seed, "residual")
        tasks.append({"key": key, "kwargs": kwargs})
        originals[key] = (original_attempt, original_summary)

    before_counter = multiprocessing.get_context("fork").Value("i", 0, lock=True)
    base_execute = controlled_run.execute_controlled_run

    def witnessed_execute(*args: Any, **kwargs: Any) -> Any:
        callback = kwargs.get("before_decision")
        if callback is None:
            raise bridge.CostRegimeError("actual controlled-run hook omitted its before-decision callback")

        def counted_before(*callback_args: Any, **callback_kwargs: Any) -> Any:
            with before_counter.get_lock():
                before_counter.value += 1
            return callback(*callback_args, **callback_kwargs)

        kwargs["before_decision"] = counted_before
        return base_execute(*args, **kwargs)

    controlled_run.execute_controlled_run = witnessed_execute
    base_aggregate_enforce = run_study_r2.enforce_budget
    base_enforce = parallel_module.BASE_ENFORCE
    tracker_setup_cpu = max(0.0, tracker.parent_start_cpu - diagnostic_start_cpu)
    ledger_baseline = float(_ledger_total(bridge_module.HERE))
    static_allowance = float(STATIC_ALLOWANCE_SECONDS)
    code_test_reserve = DIAGNOSTIC_CODE_AND_TEST_RESERVE
    prior_diagnostic_cpu = sum(
        float(row.get("process_cpu_seconds", 0.0))
        for row in _diagnostic_ledger_rows()
    )

    def diagnostic_actual_enforce(**kwargs: Any) -> dict[str, Any]:
        current_stage_cpu = tracker.current_stage_cpu()
        active = tracker.active_worker_count()
        with tracker.max_game_cpu.get_lock():
            max_game = float(tracker.max_game_cpu.value)
        diagnostic_projected = (
            prior_diagnostic_cpu + code_test_reserve + tracker_setup_cpu + current_stage_cpu
            + active * max_game + bridge_module.FINALIZATION_RESERVE
        )
        if diagnostic_projected >= DIAGNOSTIC_CPU_LIMIT:
            raise ResourceBudgetExceeded(
                "RESOURCE_BLOCKED: retained-key diagnostic reached its 1,000 aggregate CPU-s limit"
            )
        actual_builder = (
            static_allowance + ledger_baseline + code_test_reserve
            + tracker_setup_cpu + current_stage_cpu
        )
        try:
            enforce_actual_caps(
                actual_cpu=actual_builder, in_flight_workers=active,
                max_game_cpu=max_game,
            )
        except bridge.CostRegimeError as exc:
            raise ResourceBudgetExceeded(str(exc)) from exc
        return {
            "status": "PASS", "scope": "Planner-authorized retained-key diagnostic",
            "actual_builder_cpu_seconds_including_reserved_work": (
                actual_builder + active * max_game + bridge_module.FINALIZATION_RESERVE
            ),
            "diagnostic_cpu_limit_seconds": DIAGNOSTIC_CPU_LIMIT,
            "diagnostic_cpu_projected_seconds": diagnostic_projected,
            "formal_grid_forecast_enforced": False,
            "actual_cumulative_caps_enforced": True,
        }

    # Keep the dispatcher's live aggregate accounting and in-flight reserve,
    # while applying the explicit diagnostic allowance instead of rewriting or
    # enforcing the stale full-grid forecast during two isolated validation games.
    parallel_module.BASE_ENFORCE = diagnostic_actual_enforce
    context.update({
        "status": "RUNNING",
        "diagnostic_cpu_seconds_before_attempt": prior_diagnostic_cpu,
        "replay_keys": [list(task["key"]) for task in tasks],
        "original_attempts": [
            {"key": list(key), "path": attempt.relative_to(HERE).as_posix(),
             "run_json_sha256": _sha256(attempt / "run.json")}
            for key, (attempt, _summary) in originals.items()
        ],
        "runtime": {key: runtime.get(key) for key in (
            "python", "torch", "torch_threads", "deterministic_algorithms",
            "stsr_commit", "native_commit", "native_binary_sha256",
        )},
        "checkpoint_model_sha256": model_sha,
        "config_sha256": config_digest,
        "cache_active_before_dispatch": _cached_guard_is_active(bridge_module),
        "cache_completed_formal_keys_before": state.completed_run_count(),
        "cache_cost_observations_before": len(state.complete_game_costs(bridge_module.HERE)),
        "aggregate_tracker_start_cpu": tracker.parent_start_cpu,
        "setup_cpu_seconds_before_tracker": tracker_setup_cpu,
        "code_and_test_reserve_cpu_seconds": code_test_reserve,
        "formal_grid_forecast_enforced": False,
        "cost_index_sha256_before": _sha256(bridge_module.FORECAST_INDEX),
        "tag_count_before": len(bridge.load_cost_tags(
            bridge_module.HERE, current_manifest=bridge_module.PRODUCER_HASHES,
        )),
    })
    started_wall = time.monotonic()
    try:
        results = dispatcher.run_jobs(tasks)
        comparisons = []
        for task in tasks:
            key = tuple(task["key"])
            replay_attempt, replay_summary = results[key]
            original_attempt, original_summary = originals[key]
            original_decisions = run_study.read_jsonl(original_attempt / "decisions.jsonl.gz")
            replay_decisions = run_study.read_jsonl(replay_attempt / "decisions.jsonl.gz")
            comparison = parallel_execution.compare_replay(
                original_summary, replay_summary,
                original_decisions, replay_decisions,
            )
            comparison.update({
                "seed": int(task["kwargs"]["seed"]),
                "original_run_json_sha256": _sha256(original_attempt / "run.json"),
                "replay_run_json_sha256": _sha256(replay_attempt / "run.json"),
                "original_decisions_sha256": _sha256(original_attempt / "decisions.jsonl.gz"),
                "replay_decisions_sha256": _sha256(replay_attempt / "decisions.jsonl.gz"),
                "replay_attempt_path": replay_attempt.relative_to(HERE).as_posix(),
                "process_cpu_seconds": float(replay_summary["process_cpu_seconds"]),
                "wall_seconds": next(
                    float(row["wall_seconds"]) for row in dispatcher.executions
                    if int(row["task_key"][1]) == int(task["kwargs"]["seed"])
                ),
            })
            comparisons.append(comparison)
        context["replays"] = comparisons
        context["status"] = "PASS"
    except BaseException as exc:
        context["failure"] = f"{type(exc).__name__}: {exc}"
        context["status"] = "RESOURCE_BLOCKED" if "RESOURCE_BLOCKED" in str(exc) else "FAILED"
        raise
    finally:
        with before_counter.get_lock():
            context["before_decision_check_count"] = int(before_counter.value)
        context["dispatcher"] = dispatcher.evidence()
        context["worker_settled_cpu_seconds"] = state.settled_worker_cpu_seconds
        context["worker_settlement_count"] = len(state.worker_settlements)
        context["cache_completed_formal_keys_after"] = state.completed_run_count()
        context["cache_cost_observations_after"] = len(state.complete_game_costs(bridge_module.HERE))
        context["cost_index_sha256_after"] = _sha256(bridge_module.FORECAST_INDEX)
        context["tag_count_after"] = len(bridge.load_cost_tags(
            bridge_module.HERE, current_manifest=bridge_module.PRODUCER_HASHES,
        ))
        context["wall_seconds"] = max(0.0, time.monotonic() - started_wall)
        context["aggregate_stage_cpu_seconds"] = tracker.current_stage_cpu()
        context["parent_stage_cpu_seconds"] = max(
            0.0, time.process_time() - tracker.parent_start_cpu,
        )
        context["worker_stage_cpu_seconds"] = float(tracker.completed_cpu.value)
        controlled_run.execute_controlled_run = base_execute
        run_study_r2.enforce_budget = base_aggregate_enforce
        parallel_module.BASE_ENFORCE = base_enforce
    return 0


def _capture_diagnostic_stage_accounting(context: dict[str, Any],
                                        parallel_module: Any,
                                        diagnostic_start_cpu: float) -> None:
    stage_name = str(context.get("diagnostic_stage", DIAGNOSTIC_STAGE))
    receipt_path = (
        parallel_module.VALIDATION / "stages" / f"{stage_name}.json"
    )
    if receipt_path.is_file():
        receipt = bridge._read_json(receipt_path)
        context["parent_stage_cpu_seconds"] = float(
            receipt["parent_process_cpu_seconds"]
        )
        context["worker_stage_cpu_seconds"] = float(
            receipt["worker_process_cpu_seconds"]
        )
        context["aggregate_stage_cpu_seconds"] = float(
            receipt["aggregate_process_cpu_seconds"]
        )
    elif "parent_stage_cpu_seconds" not in context:
        setup_cpu = float(context.get("setup_cpu_seconds_before_tracker", 0.0))
        context["parent_stage_cpu_seconds"] = max(
            0.0, time.process_time() - diagnostic_start_cpu - setup_cpu,
        )
        context.setdefault("worker_stage_cpu_seconds", 0.0)


def _finalize_diagnostic(context: dict[str, Any], bridge_module: Any,
                         parallel_module: Any, diagnostic_start_cpu: float,
                         error: BaseException | None) -> None:
    if error is not None and not context.get("failure"):
        context["failure"] = f"{type(error).__name__}: {error}"
        context["status"] = "RESOURCE_BLOCKED" if "RESOURCE_BLOCKED" in str(error) else "FAILED"
    worker_cpu = float(context.get("worker_stage_cpu_seconds", 0.0))
    setup_cpu = float(context.get("setup_cpu_seconds_before_tracker", 0.0))
    parent_cpu = float(context.get("parent_stage_cpu_seconds", 0.0))
    # run_study_parallel has already charged every reaped worker once in its
    # stage receipt. This row adds parent/setup CPU plus conservative reserves.
    finalization_reserve = float(bridge_module.FINALIZATION_RESERVE)
    charged_parent = (
        setup_cpu + parent_cpu + DIAGNOSTIC_CODE_AND_TEST_RESERVE
        + finalization_reserve
    )
    prior_diagnostic_cpu = float(context.get("diagnostic_cpu_seconds_before_attempt", 0.0))
    attempt_aggregate = charged_parent + worker_cpu
    aggregate = prior_diagnostic_cpu + attempt_aggregate
    if aggregate >= DIAGNOSTIC_CPU_LIMIT:
        context["status"] = "RESOURCE_BLOCKED"
        context.setdefault("failure", "diagnostic aggregate CPU-s exceeded its 1,000 CPU-s limit")
    ledger_attempt = str(context.get("diagnostic_cpu_ledger_attempt"))
    ledger_path = EVIDENCE / "cpu-ledger.jsonl"
    ledger_rows = [
        json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if ledger_path.is_file() else []
    if not any(row.get("attempt") == ledger_attempt for row in ledger_rows):
        bridge_module._append_ledger({
            "attempt": ledger_attempt,
            "authorization_comment_id": DIAGNOSTIC_AUTHORIZATION,
            "stage": str(context.get("diagnostic_stage", DIAGNOSTIC_STAGE)) + "-parent-and-preparation",
            "status": context.get("status", "FAILED"),
            "process_cpu_seconds": charged_parent,
            "source_commit": bridge_module.PRODUCER_COMMIT,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "accounting": (
                "continuation setup and parent-stage CPU plus "
                f"{DIAGNOSTIC_CODE_AND_TEST_RESERVE:g} CPU-s conservative code/focused-check "
                f"reserve and {finalization_reserve:g} CPU-s finalization reserve; worker CPU is charged once by the parallel "
                "stage receipt"
            ),
        })
        if bridge_module.CACHED_STATE is not None:
            bridge_module.CACHED_STATE.add_ledger_row({
                "process_cpu_seconds": charged_parent,
            })
    context.update({
        "authorization_comment_id": DIAGNOSTIC_AUTHORIZATION,
        "aggregate_cpu_seconds_including_reserves": aggregate,
        "current_attempt_cpu_seconds_including_reserves": attempt_aggregate,
        "diagnostic_cpu_seconds_before_attempt": prior_diagnostic_cpu,
        "parent_setup_and_stage_cpu_seconds": setup_cpu + parent_cpu,
        "worker_cpu_seconds_charged_by_stage_receipt": worker_cpu,
        "code_and_test_reserve_cpu_seconds": DIAGNOSTIC_CODE_AND_TEST_RESERVE,
        "finalization_reserve_cpu_seconds": finalization_reserve,
        "cumulative_builder_cpu_seconds_after_charge": (
            float(bridge_module.STATIC_ALLOWANCE) + bridge_module._ledger_total()
        ),
        "cumulative_process_cpu_cap_seconds": 200000.0,
        "builder_cpu_cap_seconds": 180000.0,
        "diagnostic_cpu_limit_seconds": DIAGNOSTIC_CPU_LIMIT,
        "actual_cost_index_unchanged": (
            context.get("cost_index_sha256_before") == context.get("cost_index_sha256_after")
            and context.get("tag_count_before") == context.get("tag_count_after")
        ),
        "diagnostic_cpu_ledger": _diagnostic_ledger_rows(),
        "diagnostic_stage_receipts": _diagnostic_stage_receipts(),
    })
    bridge.atomic_json(DIAGNOSTIC_REPORT, context)


def _run_continue_with_cost_tagging(*, replay_only: bool = False,
                                    diagnostic_start_cpu: float = 0.0) -> int:
    """Install cache and cost hooks in the standalone continuation call chain."""
    context: dict[str, Any] = {"schema_id": "issue60-continuation-cache-diagnostic-v1"}
    if replay_only:
        attempt_number = _next_diagnostic_attempt()
        context["diagnostic_attempt_number"] = attempt_number
        context["diagnostic_stage"] = f"{DIAGNOSTIC_STAGE}-attempt-{attempt_number:04d}"
        context["diagnostic_cpu_ledger_attempt"] = (
            f"{DIAGNOSTIC_LEDGER_PREFIX}{attempt_number:04d}"
        )
    events: list[str] = []
    original_parallel_main = parallel.main
    original_receipt_reader = parallel._read_validation_receipt
    original_tag = bridge._tag_attempt
    parallel._read_validation_receipt = _validation_receipt
    bridge._tag_attempt = _tag_formal_attempts_only(original_tag, bridge.HERE)
    bridge._install_tagged_dispatcher()

    def continuation_parallel_main() -> int:
        original_install = bridge.R2._install_runtime_hooks
        installer = lambda: _install_cached_guard(bridge, __import__("parallel_execution"))
        bridge.R2._install_runtime_hooks = _wrap_runtime_hooks_with_cache(
            original_install, installer, events,
        )
        original_r2_main = bridge.R2.main
        if replay_only:
            def replay_main() -> int:
                previous_original_stage = parallel.ORIGINAL_STAGE

                def stage(stage_name: str, *, stage_start_cpu: float) -> int:
                    return _diagnostic_stage(
                        bridge, parallel, diagnostic_start_cpu, context,
                    )

                parallel.ORIGINAL_STAGE = stage
                try:
                    return int(bridge.R2._run_original_stage(
                        context["diagnostic_stage"], stage_start_cpu=time.process_time(),
                    ))
                finally:
                    parallel.ORIGINAL_STAGE = previous_original_stage

            bridge.R2.main = replay_main
        try:
            try:
                result = int(original_parallel_main())
                if replay_only:
                    _capture_diagnostic_stage_accounting(
                        context, parallel, diagnostic_start_cpu,
                    )
                    if events != ["original-r2-runtime-hooks", "cached-guard-installed"]:
                        raise bridge.CostRegimeError("actual continuation hook composition did not install cache after R2")
                    if context.get("before_decision_check_count", 0) < 1:
                        raise bridge.CostRegimeError("actual continuation witness missed every before-decision check")
                    if context.get("worker_settlement_count", 0) < 1:
                        raise bridge.CostRegimeError("actual continuation witness missed completed-worker cache events")
                    if context.get("status") != "PASS":
                        raise bridge.CostRegimeError("retained-key continuation diagnostic did not pass")
                    context["cached_guard_install_order"] = list(events)
                    _finalize_diagnostic(context, bridge, parallel, diagnostic_start_cpu, None)
                return result
            except BaseException as exc:
                if replay_only:
                    _capture_diagnostic_stage_accounting(
                        context, parallel, diagnostic_start_cpu,
                    )
                    context["cached_guard_install_order"] = list(events)
                    _finalize_diagnostic(context, bridge, parallel, diagnostic_start_cpu, exc)
                raise
        finally:
            bridge.R2.main = original_r2_main
            bridge.R2._install_runtime_hooks = original_install

    parallel.main = continuation_parallel_main
    try:
        return int(bridge._run_continue())
    finally:
        parallel.main = original_parallel_main
        parallel._read_validation_receipt = original_receipt_reader
        bridge._tag_attempt = original_tag


def _forecast_sensitivity(attribution: dict[str, Any]) -> dict[str, Any]:
    import resource_amendment_r2

    complete = resource_amendment_r2.complete_game_costs(HERE)
    completed_formal = sum(row["stage_key"] != "preflight" for row in complete)
    remaining_games = max(0, resource_amendment_r2.FORMAL_GAME_COUNT - completed_formal)
    optimizer = resource_amendment_r2.remaining_optimizer_projection(HERE)
    sample = attribution["samples"]["mixed_cached_bridge_plus_uncached_continuation"]
    forecast = resource_amendment_r2.build_forecast(
        costs=sample["costs_seconds"], remaining_games=remaining_games,
        remaining_optimizer_cpu=float(optimizer["remaining_optimizer_cpu_seconds"]),
        accounted_cpu=(resource_amendment_r2.STATIC_ALLOWANCE_SECONDS
                       + resource_amendment_r2._ledger_total(HERE)),
        builder_budget=resource_amendment_r2.BUILDER_BUDGET_SECONDS,
        process_cap=resource_amendment_r2.PROCESS_CPU_CAP_SECONDS,
    )
    return {
        "status": "SENSITIVITY_ONLY",
        "execution_mix": "59 cached bridge observations plus 114 uncached continuation observations",
        "cost_sample_count": len(sample["costs_seconds"]),
        "remaining_formal_games": remaining_games,
        "remaining_optimizer_cpu_seconds": optimizer["remaining_optimizer_cpu_seconds"],
        "accounted_cpu_seconds_after_diagnostic_charge": (
            resource_amendment_r2.STATIC_ALLOWANCE_SECONDS
            + resource_amendment_r2._ledger_total(HERE)
        ),
        "forecast": forecast,
        "interpretation": (
            "Descriptive mixed-path sensitivity only; it is not a forecast for the restored cached path "
            "and does not authorize formal sampling, PPO or evaluation."
        ),
    }


def _write_attribution_report(replay_report: dict[str, Any]) -> dict[str, Any]:
    import resource_amendment_r2

    manifest = bridge.producer_manifest(HERE)
    tags = bridge.load_cost_tags(HERE, current_manifest=manifest)
    tag_keys = {(str(row["stage_key"]), int(row["game_seed"]), str(row["arm"])) for row in tags}
    completed = resource_amendment_r2.complete_game_costs(HERE)
    bridge_rows = []
    continuation_rows = []
    prebridge_rows = []
    incomplete = []
    for path in sorted((EVIDENCE / "runs" / "training" / "block1").rglob("run.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if row.get("status") == "COMPLETE":
            sample = {
                "stage_key": str(row["stage_key"]),
                "game_seed": int(row["game_seed"]),
                "arm": str(row["arm"]),
                "process_cpu_seconds": float(row["process_cpu_seconds"]),
                "attempt_path": path.parent.relative_to(HERE).as_posix(),
                "run_json_sha256": _sha256(path),
                "tagged": (str(row["stage_key"]), int(row["game_seed"]), str(row["arm"])) in tag_keys,
            }
            if row["stage_key"] == "block1:batch-01" and sample["tagged"]:
                bridge_rows.append(sample)
            elif row["stage_key"] == "block1:batch-01":
                prebridge_rows.append(sample)
            elif row["stage_key"] == "block1:batch-02":
                continuation_rows.append(sample)
        else:
            incomplete.append({
                "path": path.parent.relative_to(HERE).as_posix(),
                "status": row.get("status"),
                "run_json_sha256": _sha256(path),
            })

    known_incomplete = {row["path"] for row in incomplete}
    for path in sorted((EVIDENCE / "runs" / "training" / "block1").rglob("status.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        relative = path.parent.relative_to(HERE).as_posix()
        if row.get("status") not in {"COMPLETE", "FAILED"} and relative not in known_incomplete:
            incomplete.append({
                "path": relative,
                "status": row.get("status"),
                "failure": row.get("failure"),
                "status_json_sha256": _sha256(path),
            })
            known_incomplete.add(relative)

    if (len(bridge_rows), len(prebridge_rows), len(continuation_rows)) != (59, 69, 114):
        raise bridge.CostRegimeError("retained training evidence no longer matches 59/69/114 attribution")
    if len(incomplete) != 2:
        raise bridge.CostRegimeError("retained block-1 evidence no longer has its two recorded incomplete attempts")
    index_before = json.loads((PROSPECTIVE / "cost-index.json").read_text(encoding="utf-8"))
    bridge_latest = bridge._read_json(bridge.BRIDGE_LATEST)
    gate = bridge._read_json(PROSPECTIVE / "bridge-gate.json")
    historical_latest_forecast = bridge._read_json(PROSPECTIVE / "latest-forecast.json")
    historical_r3 = bridge._read_historical_parallel_receipt(__import__("run_study_parallel"))
    prior_handoff = "07c493a656bc344042f38532798e148d5fcd65cc"
    old_launcher = bridge.git_bytes(
        HERE, "show", f"{prior_handoff}:studies/issue-60-onpolicy-joint-choice/continue_after_cost_bridge.py",
    )
    current_launcher_sha = _sha256(Path(__file__))

    def cost_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        costs = [float(row["process_cpu_seconds"]) for row in rows]
        return {
            "count": len(costs), "sum_cpu_seconds": sum(costs),
            "mean_cpu_seconds": sum(costs) / len(costs) if costs else None,
            "mean_terminal_floor": sum(
                float(json.loads((HERE / row["attempt_path"] / "run.json").read_text(encoding="utf-8")).get("terminal_floor") or 0)
                for row in rows
            ) / len(rows) if rows else None,
            "costs_seconds": costs,
            "attempt_manifest_sha256": hashlib.sha256(json.dumps(
                [{"attempt_path": row["attempt_path"], "run_json_sha256": row["run_json_sha256"]}
                 for row in rows], sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
        }

    attribution = {
        "schema_id": "issue60-continuation-cache-attribution-v1",
        "authorization_comment_id": DIAGNOSTIC_AUTHORIZATION,
        "producer_commit": gate["source_commit"],
        "producer_files": gate["source_files"],
        "continuation_provenance": {
            "historical_continuation_commit": prior_handoff,
            "historical_launcher_sha256": hashlib.sha256(old_launcher).hexdigest(),
            "repaired_launcher_sha256": current_launcher_sha,
            "bridge_cached_guard_installer": "run_cost_bridge.py:_run_bridge",
            "historical_continuation_cached_guard_installer": False,
            "classification": (
                "The 59 bridge observations used GuardState/install_cached_guard; the 114 batch-02 "
                "continuation observations used the same producer manifest but lacked that installer."
            ),
        },
        "retained_training_population": {
            "block1_batch_01_complete": 128,
            "block1_batch_01_cached_bridge": cost_summary(bridge_rows),
            "block1_batch_01_pre_bridge_uncached_not_in_cost_index": cost_summary(prebridge_rows),
            "block1_batch_02_complete_uncached_continuation": cost_summary(continuation_rows),
            "incomplete_attempts": incomplete,
            "all_complete_training_games": len([row for row in completed if row["stage_key"].startswith("block")]),
            "optimizer_batches_retained": 1,
            "evaluation_runs": 0,
        },
        "samples": {
            "historical_cached_bridge_only": {
                "count": int(index_before["tag_count"]),
                "cost_index_sha256": _sha256(PROSPECTIVE / "cost-index.json"),
                "tags_sha256": index_before["tags_sha256"],
                "bridge_gate_forecast_status": gate["forecast"]["status"],
                "bridge_gate_projected_builder_cpu_seconds": gate["forecast"].get("projected_builder_cpu_seconds"),
                "bridge_gate_projected_all_in_cpu_seconds": gate["forecast"].get("projected_all_in_cpu_seconds"),
                "historical_latest_forecast_sha256": _sha256(PROSPECTIVE / "latest-forecast.json"),
                "historical_latest_forecast_status": historical_latest_forecast.get("forecast", {}).get("status"),
                "historical_latest_forecast_projected_builder_cpu_seconds": (
                    historical_latest_forecast.get("forecast", {}).get("projected_builder_cpu_seconds")
                ),
                "historical_latest_forecast_projected_all_in_cpu_seconds": (
                    historical_latest_forecast.get("forecast", {}).get("projected_all_in_cpu_seconds")
                ),
                "historical_latest_forecast": historical_latest_forecast,
                "historical_r3_receipt_sha256": _sha256(parallel.PARALLEL_RECEIPT),
                "historical_r3_status": historical_r3["status"],
                "historical_r3_forecast": historical_r3.get("formal_grid_forecast_at_validation"),
                "bridge_latest_sha256": _sha256(bridge.BRIDGE_LATEST),
                "bridge_latest_status": bridge_latest["status"],
            },
            "mixed_cached_bridge_plus_uncached_continuation": cost_summary(bridge_rows + continuation_rows),
        },
        "retained_key_replay": replay_report,
        "prospective_mixed_path_forecast": None,
    }
    attribution["prospective_mixed_path_forecast"] = _forecast_sensitivity(attribution)
    bridge.atomic_json(ATTRIBUTION_REPORT, attribution)
    return attribution


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--continue", dest="continue_program", action="store_true")
    parser.add_argument("--replay-retained-keys", action="store_true")
    parser.add_argument("--finalize-diagnostic-report", action="store_true")
    args = parser.parse_args()
    selected_modes = sum((args.verify_only, args.continue_program, args.replay_retained_keys,
                          args.finalize_diagnostic_report))
    if selected_modes != 1:
        parser.error(
            "select exactly one of --verify-only, --continue, --replay-retained-keys, "
            "or --finalize-diagnostic-report"
        )
    if args.verify_only:
        historical_before = _sha256(parallel.PARALLEL_RECEIPT)
        prior = bridge._read_json(BINDING) if BINDING.is_file() else None
        if (isinstance(prior, dict)
                and prior.get("schema_id") == "issue60-continuation-validation-binding-v1"
                and prior.get("status") == "PASS"
                and prior.get("facts") == _facts()):
            recovery = prior.get("recovery_basis")
            _verify_recorded_recovery_basis(recovery)
        else:
            recovery = _recovery_basis()
        record = _write_binding(_facts(), recovery)
        parallel._read_validation_receipt = _validation_receipt
        receipt = parallel._read_validation_receipt()
        if (receipt.get("status") != "PASS"
                or receipt.get("validation", {}).get("status") != "PASS"
                or receipt.get("worker_selection", {}).get("worker_count") != 2
                or receipt.get("source_files") != {
                    relative: parallel._sha256(HERE / relative)
                    for relative in parallel.PARALLEL_SOURCE_FILES
                }
                or _sha256(parallel.PARALLEL_RECEIPT) != historical_before):
            raise bridge.CostRegimeError("in-memory validation adapter witness failed or changed history")
        print(json.dumps({"status": record["status"], "binding": BINDING.as_posix(),
                          "sha256": _sha256(BINDING), "worker_count": 2,
                          "historical_receipt_preserved": True}, sort_keys=True))
        return 0

    if args.replay_retained_keys:
        binding = _verified_binding()
        _verify_recorded_recovery_basis(binding.get("recovery_basis"))
        diagnostic_start_cpu = time.process_time()
        _run_continue_with_cost_tagging(
            replay_only=True, diagnostic_start_cpu=diagnostic_start_cpu,
        )
        replay_report = bridge._read_json(DIAGNOSTIC_REPORT)
        attribution = _write_attribution_report(replay_report)
        replay_report["attribution_report_path"] = ATTRIBUTION_REPORT.relative_to(HERE).as_posix()
        replay_report["attribution_report_sha256"] = _sha256(ATTRIBUTION_REPORT)
        bridge.atomic_json(DIAGNOSTIC_REPORT, replay_report)
        print(json.dumps({
            "status": replay_report.get("status"),
            "diagnostic_cpu_seconds_including_reserves": replay_report.get("aggregate_cpu_seconds_including_reserves"),
            "before_decision_checks": replay_report.get("before_decision_check_count"),
            "worker_cache_settlements": replay_report.get("worker_settlement_count"),
            "attribution_report": ATTRIBUTION_REPORT.relative_to(HERE).as_posix(),
            "mixed_path_forecast_status": attribution["prospective_mixed_path_forecast"]["forecast"]["status"],
        }, sort_keys=True))
        return 0

    if args.finalize_diagnostic_report:
        report = bridge._read_json(DIAGNOSTIC_REPORT)
        if (report.get("schema_id") != "issue60-continuation-cache-diagnostic-v1"
                or report.get("authorization_comment_id") != DIAGNOSTIC_AUTHORIZATION
                or report.get("status") != "PASS"
                or [row.get("seed") for row in report.get("replays", [])]
                != list(DIAGNOSTIC_REPLAY_SEEDS)
                or any(row.get("status") != "PASS" for row in report.get("replays", []))):
            raise bridge.CostRegimeError(
                "existing diagnostic report is not a complete authorized retained-key replay"
            )
        attribution = _write_attribution_report(report)
        report["attribution_report_path"] = ATTRIBUTION_REPORT.relative_to(HERE).as_posix()
        report["attribution_report_sha256"] = _sha256(ATTRIBUTION_REPORT)
        bridge.atomic_json(DIAGNOSTIC_REPORT, report)
        print(json.dumps({
            "status": report.get("status"),
            "diagnostic_cpu_seconds_including_reserves": report.get("aggregate_cpu_seconds_including_reserves"),
            "before_decision_checks": report.get("before_decision_check_count"),
            "worker_cache_settlements": report.get("worker_settlement_count"),
            "attribution_report": ATTRIBUTION_REPORT.relative_to(HERE).as_posix(),
            "mixed_path_forecast_status": attribution["prospective_mixed_path_forecast"]["forecast"]["status"],
        }, sort_keys=True))
        return 0

    binding = _verified_binding()
    recovery = _recovery_basis()
    if binding.get("recovery_basis") != recovery:
        raise bridge.CostRegimeError("retry recovery evidence changed after continuation validation")
    attempt_number = 2 if recovery is not None else 1
    prior_receipt_sha = binding["facts"]["historical_r3_receipt_sha256"]
    started = time.process_time()
    _record_bridge_charge(
        attempt="issue60-prior-automatic-continuation-receipt-block-0001",
        stage="issue60-cached-cost-bridge-continuation-precondition-failure",
        seconds=0.2,
        status="FAILED",
        source_commit=binding["facts"]["producer_commit"],
        accounting=("conservative finalization charge for the automatic post-bridge invocation "
                    "blocked by the historical R3 receipt; no PPO update or formal game dispatched"),
    )
    _record_bridge_charge(
        attempt="issue60-continuation-validation-adapter-repair-0001",
        stage="issue60-cached-cost-bridge-continuation-repair-validation",
        seconds=30.0,
        status="PASS",
        source_commit=binding["facts"]["producer_commit"],
        accounting=("conservative implementation, focused binding verification and commit-preparation "
                    "charge for the study-local adapter; no simulation or PPO update"),
    )
    if recovery is not None:
        _append_lifecycle({
            "schema_id": "issue60-continuation-lifecycle-v1",
            "state": "RETRY_AUTHORIZED",
            "authorization_comment_id": AUTHORIZATION,
            "attempt_number": attempt_number,
            "recovery_basis": recovery,
            "authorized_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        _record_bridge_charge(
            attempt="issue60-continuation-pre-dispatch-retry-recovery-0001",
            stage="issue60-continuation-recovery-evidence-and-runtime-path-check",
            seconds=10.0,
            status="PASS",
            source_commit=binding["facts"]["producer_commit"],
            accounting=("conservative charge for archiving the verified zero-dispatch failed attempt, "
                        "correcting the runtime checkout path and revalidating pinned runtime; no simulation"),
        )
    parallel._read_validation_receipt = _validation_receipt
    launch_charge = max(0.0, time.process_time() - started) + 0.2
    _record_bridge_charge(
        attempt=f"issue60-continuation-validation-adapter-launch-{attempt_number:04d}",
        stage="issue60-cached-cost-bridge-continuation-launch",
        seconds=launch_charge,
        status="PASS",
        source_commit=binding["facts"]["producer_commit"],
        accounting="continuation binding recheck, in-memory receipt installation and finalization reserve before entering the unchanged runner",
    )
    _append_lifecycle({
        "schema_id": "issue60-continuation-lifecycle-v1",
        "state": "STARTED",
        "authorization_comment_id": AUTHORIZATION,
        "binding_sha256": _sha256(BINDING),
        "historical_r3_receipt_sha256_preserved": prior_receipt_sha,
        "worker_count": 2,
        "attempt_number": attempt_number,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    ledger = EVIDENCE / "cpu-ledger.jsonl"
    ledger_lines_before_runner = len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.is_file() else 0
    runner_started = time.process_time()
    try:
        exit_code = _run_continue_with_cost_tagging()
    except BaseException as exc:
        ledger_lines_after_runner = len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.is_file() else 0
        if ledger_lines_after_runner == ledger_lines_before_runner:
            _record_bridge_charge(
                attempt=f"issue60-continuation-runtime-failure-{attempt_number:04d}",
                stage="issue60-authorized-continuation-failure",
                seconds=max(0.0, time.process_time() - runner_started) + 0.2,
                status="FAILED",
                source_commit=binding["facts"]["producer_commit"],
                accounting="parent process CPU plus finalization reserve for continuation failure before any stage ledger charge",
            )
        _append_lifecycle({
            "schema_id": "issue60-continuation-lifecycle-v1",
            "state": "FAILED",
            "authorization_comment_id": AUTHORIZATION,
            "attempt_number": attempt_number,
            "failure": f"{type(exc).__name__}: {exc}",
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        raise
    ledger_lines_after_runner = len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.is_file() else 0
    if exit_code != 0 and ledger_lines_after_runner == ledger_lines_before_runner:
        _record_bridge_charge(
            attempt=f"issue60-continuation-runtime-stop-{attempt_number:04d}",
            stage="issue60-authorized-continuation-stop",
            seconds=max(0.0, time.process_time() - runner_started) + 0.2,
            status="RESOURCE_BLOCKED" if exit_code == 75 else "FAILED",
            source_commit=binding["facts"]["producer_commit"],
            accounting="parent process CPU plus finalization reserve for continuation stop before any stage ledger charge",
        )
    _append_lifecycle({
        "schema_id": "issue60-continuation-lifecycle-v1",
        "state": "EXITED",
        "authorization_comment_id": AUTHORIZATION,
        "attempt_number": attempt_number,
        "exit_code": exit_code,
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
