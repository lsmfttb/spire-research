"""Run only Planner-authorized block1 batch-02 keys through the reviewed path.

This launcher intentionally replaces the train-stage body with fixed-key game
sampling.  It installs the ordinary R2 hooks, cached-path guard, and accepted
two-worker dispatcher, then returns before any PPO update or later stage.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Mapping

import continue_after_cost_bridge as continuation
import run_cost_bridge as bridge
import run_study_parallel as parallel

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "evidence"
PROSPECTIVE = EVIDENCE / "prospective-cost"
EXECUTIONS = PROSPECTIVE / "executions"
RECEIPT = EXECUTIONS / "batch02-sampling.json"
FORECAST_INDEX = PROSPECTIVE / "cost-index.json"
AUTHORIZATION = 6232885569
BASE_HANDOFF = "d7425ace823e2b0aee5d9640341f4cef49f00259"
CHECKPOINT_SHA = "c05ffb91ed6cb702e9ad135afe3b537c1348f2da8e21958c38141404055bfbb5"
BOUND_CONTINUATION_LAUNCHER_COMMIT = "84c4f3997d230e9a392fdef0c8ac5f9d66a5e186"
STAGE_KEY = "block1:batch-02"
TARGET_SEEDS = tuple(range(660243, 660257))
LOCAL_LIMIT = 2000.0
CODE_AND_CHECK_RESERVE = 200.0
INCREMENTAL_FINALIZATION_RESERVE = 30.0
PARALLEL_STAGE_DIR = EVIDENCE / "validation" / "parallel-execution" / "batch02-sampling"

LAUNCH_START_CPU = 0.0
SETUP_CPU_BEFORE_TRACKER = 0.0
AUTH_SPENT_BEFORE_ATTEMPT = 0.0
ATTEMPT_ID = ""
ATTEMPT: dict[str, Any] = {}
EXECUTION: dict[str, Any] = {}
HOOK_EVENTS: list[str] = []


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _batch02_seed_schedule() -> tuple[int, ...]:
    return (*range(660001, 660129), *TARGET_SEEDS)


def _r2_source_compatibility(r2: Any, head: str) -> dict[str, Any]:
    """Verify sealed R2 code while allowing its later offline test-only edit.

    The sealed R2 source commit remains the authority for all three original
    files.  Two runtime files must still match that commit byte-for-byte.  The
    later descendant edit to test_resource_amendment_r2.py is accepted only
    because it is itself committed at HEAD and is the sole R2 source-path
    difference since the seal; it changes offline assertions, not runtime code.
    """
    bridge._bind_resource_git_to_windows_worktree()
    receipt_path = HERE / r2.R2_RECEIPT
    document = r2._read_json(receipt_path)
    gate = document.get("gate", {})
    source_commit = str(gate.get("implementation_source_commit", ""))
    source_hashes = dict(gate.get("implementation_source_files", {}))
    symbol_hashes = dict(gate.get("implementation_changed_symbol_sha256", {}))
    source_paths = tuple(r2.R2_SOURCE_PATHS)
    if (gate.get("status") != "PASS" or not source_commit
            or set(source_hashes) != set(source_paths)
            or set(symbol_hashes) != set(source_paths)):
        raise bridge.CostRegimeError("sealed R2 source manifest is incomplete")
    r2._git(HERE, "merge-base", "--is-ancestor", source_commit, head)
    study_git_path = __import__("resource_amendment_r1")._study_git_path(HERE, ".")
    source_scope = r2._git(
        HERE, "diff", "--name-status", "--no-renames",
        r2.PARENT_HANDOFF, source_commit, "--", study_git_path,
    )
    source_observed = {}
    for line in source_scope.splitlines():
        status, path = line.split("\t", 1)
        source_observed[path.removeprefix(study_git_path + "/")] = status
    if source_observed != {path: "A" for path in source_paths}:
        raise bridge.CostRegimeError("sealed R2 source commit no longer has its authorized source scope")

    current_scope = r2._git(
        HERE, "diff", "--name-status", "--no-renames",
        source_commit, head, "--",
        *(f"{study_git_path}/{relative}" for relative in source_paths),
    )
    current_observed = {}
    for line in current_scope.splitlines():
        status, path = line.split("\t", 1)
        current_observed[path.removeprefix(study_git_path + "/")] = status
    offline_test = "test_resource_amendment_r2.py"
    if any(path != offline_test or status != "M" for path, status in current_observed.items()):
        raise bridge.CostRegimeError(
            f"R2 runtime source changed after its seal: {current_observed}"
        )

    source_facts: dict[str, dict[str, str]] = {}
    for relative in source_paths:
        committed = r2._git_bytes(
            HERE, "show", f"{source_commit}:{study_git_path}/{relative}",
        )
        committed_sha = hashlib.sha256(committed).hexdigest()
        if committed_sha != source_hashes[relative]:
            raise bridge.CostRegimeError(f"sealed R2 source commit hash mismatch: {relative}")
        path = HERE / relative
        current_sha = _sha256(path)
        head_blob = r2._git_bytes(HERE, "show", f"{head}:{study_git_path}/{relative}")
        head_sha = hashlib.sha256(head_blob).hexdigest()
        if current_sha != head_sha:
            raise bridge.CostRegimeError(f"R2 source worktree differs from executing HEAD: {relative}")
        if relative != offline_test:
            if (current_sha != source_hashes[relative]
                    or r2._source_symbol_hashes(path) != symbol_hashes[relative]):
                raise bridge.CostRegimeError(f"sealed R2 runtime source changed: {relative}")
        source_facts[relative] = {
            "sealed_source_sha256": source_hashes[relative],
            "executing_head_sha256": head_sha,
            "matches_sealed_source": current_sha == source_hashes[relative],
        }
    return {
        "status": "PASS",
        "sealed_source_commit": source_commit,
        "executing_head": head,
        "source_paths": source_facts,
        "post_seal_offline_test_only_change": (
            current_observed == {offline_test: "M"}
            and source_facts[offline_test]["matches_sealed_source"] is False
        ),
        "changed_paths_since_r2_seal": current_observed,
        "runtime_source_files_unchanged": all(
            source_facts[path]["matches_sealed_source"]
            for path in source_paths if path != offline_test
        ),
    }


def _verify_r2_source_scope_for_bounded_sample(
    r2: Any, root: Path, source_commit: str,
    source_hashes: Mapping[str, str], symbol_hashes: Mapping[str, Mapping[str, str]],
    expected: Mapping[str, Any],
) -> None:
    """Apply the source-seal compatibility check at the reviewed hook boundary."""
    if (Path(root).resolve() != HERE.resolve()
            or source_commit != expected.get("sealed_source_commit")
            or dict(source_hashes) != dict(
                r2._read_json(HERE / r2.R2_RECEIPT)["gate"]["implementation_source_files"]
            )
            or dict(symbol_hashes) != dict(
                r2._read_json(HERE / r2.R2_RECEIPT)["gate"]["implementation_changed_symbol_sha256"]
            )):
        raise bridge.CostRegimeError("R2 verifier input differs from the sealed source manifest")
    observed = _r2_source_compatibility(r2, str(expected["executing_head"]))
    if observed != dict(expected):
        raise bridge.CostRegimeError("R2 source compatibility witness changed after preflight")


def _no_dispatch_source_rollover(previous: Mapping[str, Any], target_state: Mapping[str, Any],
                                 *, head: str, launcher_sha256: str) -> dict[str, Any]:
    """Permit a new source revision only after fully evidenced no-dispatch failures."""
    attempts = list(previous.get("attempts", []))
    if previous.get("status") not in {"FAILED", "RESOURCE_BLOCKED"} or not attempts:
        raise bridge.CostRegimeError("changed source cannot resume a bounded receipt without failed attempts")
    if target_state.get("complete"):
        raise bridge.CostRegimeError("changed source cannot resume after any authorized game completed")
    for attempt in attempts:
        if (attempt.get("status") not in {"FAILED", "FAILED_BEFORE_DISPATCH", "RESOURCE_BLOCKED"}
                or attempt.get("completed_games") or attempt.get("complete_keys")):
            raise bridge.CostRegimeError("changed source resume requires only no-dispatch failed attempts")
        stage_path = HERE / str(attempt.get("stage_receipt_path", ""))
        stage = attempt.get("parallel_stage_receipt")
        if stage_path.is_file():
            try:
                stage_document = json.loads(stage_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise bridge.CostRegimeError("failed attempt has an unreadable stage receipt") from exc
            if stage_document.get("executions") or int(stage_document.get("execution_count") or 0) != 0:
                raise bridge.CostRegimeError("failed attempt stage receipt shows dispatched work")
        if isinstance(stage, Mapping) and (
            stage.get("execution_count") not in (None, 0)
            or stage.get("worker_process_cpu_seconds") not in (None, 0, 0.0)
        ):
            raise bridge.CostRegimeError("failed attempt summary shows dispatched worker work")
        if (attempt.get("status") == "RESOURCE_BLOCKED"
                and not stage_path.is_file() and not isinstance(stage, Mapping)):
            raise bridge.CostRegimeError("resource-blocked attempt lacks a stage receipt proving no dispatch")

    prior = attempts[-1]
    recovery = next((row.get("pre_dispatch_failure_recovery", {}) for row in attempts
                     if row.get("pre_dispatch_failure_recovery")), {})
    expected_old = {
        int(row["game_seed"]): row
        for row in recovery.get("preserved_original_incomplete_attempts", [])
    }
    if set(expected_old) != {660243, 660244}:
        raise bridge.CostRegimeError("no-dispatch resume lacks the original incomplete-attempt witness")
    for row in target_state.get("keys", []):
        seed = int(row["game_seed"])
        if seed in expected_old:
            observed = row.get("attempts", [])
            expected = expected_old[seed]
            if (len(observed) != 1 or observed[0].get("status") != "INCOMPLETE"
                    or observed[0].get("run_json_exists")
                    or observed[0].get("status_json_sha256") != expected.get("status_json_sha256")):
                raise bridge.CostRegimeError("original incomplete target attempt changed before resume")
        elif row.get("attempts"):
            raise bridge.CostRegimeError("unexpected authorized target attempt exists before resume")
    return {
        "from_executing_head": previous.get("executing_head"),
        "from_launcher_sha256": previous.get("launcher_sha256"),
        "to_executing_head": head,
        "to_launcher_sha256": launcher_sha256,
        "basis": "all prior attempts failed before dispatch; no target games completed; original incomplete attempts preserved",
        "preserved_attempt_ids": [row.get("attempt_id") for row in attempts],
        "last_failed_attempt_id": prior.get("attempt_id"),
        "new_dispatches_before_rollover": 0,
    }


def _remaining_local_budget(*, charged: float, setup: float,
                            finalization_reserve: float) -> float:
    return LOCAL_LIMIT - float(charged) - float(setup) - float(finalization_reserve)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    payload = json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _ledger_rows() -> list[dict[str, Any]]:
    path = EVIDENCE / "cpu-ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _authorization_charges() -> float:
    code_and_check_reserve = sum(
        float(row.get("process_cpu_seconds", 0.0))
        for row in _ledger_rows()
        if row.get("authorization_comment_id") == AUTHORIZATION
        and row.get("stage") == "issue60-batch02-sampling-code-check-reserve"
    )
    prior_attempts = sum(
        float(row.get("authorized_aggregate_cpu_seconds", 0.0))
        for row in EXECUTION.get("attempts", [])
        if row.get("status") != "RUNNING" or "authorized_aggregate_cpu_seconds" in row
    )
    return code_and_check_reserve + prior_attempts


def _append_ledger(row: dict[str, Any]) -> None:
    path = EVIDENCE / "cpu-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    if any(json.loads(line).get("attempt") == row.get("attempt") for line in existing if line.strip()):
        return
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _attempt_number() -> int:
    if not RECEIPT.is_file():
        return 1
    previous = json.loads(RECEIPT.read_text(encoding="utf-8"))
    if previous.get("authorization_comment_id") != AUTHORIZATION:
        raise bridge.CostRegimeError("sampling receipt belongs to another authorization")
    attempts = list(previous.get("attempts", []))
    if attempts and attempts[-1].get("status") == "RUNNING":
        # A killed process may have spent CPU that no final stage receipt can
        # recover. Do not reset the local allowance or repeat uncertain work.
        raise bridge.CostRegimeError(
            "prior bounded sampling attempt has no settled receipt; stop for Planner review"
        )
    return len(attempts) + 1


def _cost_tag_path(attempt_path: str) -> Path:
    name = hashlib.sha256(attempt_path.encode("utf-8")).hexdigest() + ".json"
    return PROSPECTIVE / "tags" / name


def _receipt_progress(attempt_path: Path) -> None:
    """Persist each newly tagged completion while the dispatcher is running."""
    relative = attempt_path.resolve().relative_to(HERE.resolve()).as_posix()
    run_path = attempt_path / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if run.get("status") != "COMPLETE" or int(run.get("game_seed", -1)) not in TARGET_SEEDS:
        return
    tag_path = _cost_tag_path(relative)
    if not tag_path.is_file():
        return
    tag = json.loads(tag_path.read_text(encoding="utf-8"))
    entry = {
        "stage_key": STAGE_KEY,
        "game_seed": int(run["game_seed"]),
        "arm": "residual",
        "attempt_path": relative,
        "run_json_sha256": _sha256(run_path),
        "tag_path": tag_path.relative_to(HERE).as_posix(),
        "tag_sha256": _sha256(tag_path),
        "process_cpu_seconds": float(tag["process_cpu_seconds"]),
    }
    progress = {int(row["game_seed"]): row for row in ATTEMPT.get("completed_games", [])}
    progress[entry["game_seed"]] = entry
    ATTEMPT["completed_games"] = [progress[key] for key in sorted(progress)]
    EXECUTION["attempts"][-1] = ATTEMPT
    _atomic_json(RECEIPT, EXECUTION)


def _install_bounded_enforcement(*, setup_cpu: float) -> None:
    from prospective_cost_regime import (
        FINALIZATION_RESERVE, enforce_actual_caps,
    )
    from resource_amendment_r2 import (
        STATIC_ALLOWANCE_SECONDS, ResourceBudgetExceeded,
    )

    def enforce(*, accounted_cpu: float, current_stage_cpu: float,
                costs: Any, remaining_games: int,
                remaining_optimizer_cpu: float) -> dict[str, Any]:
        del costs, remaining_games, remaining_optimizer_cpu
        bridge._raise_if_tagging_failed()
        tracker = getattr(parallel, "TRACKER", None)
        if tracker is None:
            raise ResourceBudgetExceeded("RESOURCE_BLOCKED: aggregate tracker is not active")
        tracked = max(float(current_stage_cpu), float(tracker.current_stage_cpu()))
        active = tracker.active_worker_count()
        with tracker.max_game_cpu.get_lock():
            max_game = float(tracker.max_game_cpu.value)
        actual = (float(STATIC_ALLOWANCE_SECONDS) + bridge._ledger_total()
                  + setup_cpu + tracked)
        local = (_authorization_charges() + setup_cpu + tracked
                 + active * max_game + FINALIZATION_RESERVE)
        if local >= LOCAL_LIMIT:
            raise ResourceBudgetExceeded(
                f"RESOURCE_BLOCKED: 2,000 CPU-s bounded sample reserve; projected {local:.3f}s"
            )
        try:
            enforce_actual_caps(
                actual_cpu=actual, in_flight_workers=active,
                max_game_cpu=max_game,
            )
        except bridge.CostRegimeError as exc:
            raise ResourceBudgetExceeded(str(exc)) from exc
        return {
            "status": "PASS",
            "scope": "Planner-authorized 14-key batch-02 sampling only",
            "actual_builder_cpu_seconds": actual,
            "local_authorized_cpu_seconds_including_reserves": local,
            "local_authorized_limit_cpu_seconds": LOCAL_LIMIT,
            "in_flight_worker_count": active,
            "in_flight_game_reserve_cpu_seconds": active * max_game,
            "finalization_reserve_cpu_seconds": FINALIZATION_RESERVE,
            "formal_grid_forecast_enforced": False,
            "no_optimizer_update_authorized": True,
        }

    bridge._dynamic_enforce = enforce


def _write_stage_status(status: str, failure: str | None = None) -> None:
    parallel.EVIDENCE.mkdir(parents=True, exist_ok=True)
    _atomic_json(parallel.EVIDENCE / "stage-status" / "train.json", {
        "schema_id": "issue60-batch02-sampling-stage-v1",
        "status": status,
        "failure": failure,
        "authorization_comment_id": AUTHORIZATION,
        "stage_key": STAGE_KEY,
        "authorized_game_seeds": list(TARGET_SEEDS),
        "optimizer_updates": 0,
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


def _sampling_stage(stage: str, *, stage_start_cpu: float) -> int:
    """Replacement train stage that dispatches only the 14 named game owners."""
    import run_study
    import run_study_r2
    import torch

    global SETUP_CPU_BEFORE_TRACKER
    status = "FAILED"
    failure = None
    if stage != "train":
        raise bridge.CostRegimeError(f"bounded sampling rejected unexpected stage {stage!r}")
    tracker = parallel.TRACKER
    if tracker is None:
        raise bridge.CostRegimeError("bounded sampling requires the reviewed two-worker tracker")
    setup_cpu = max(0.0, tracker.parent_start_cpu - LAUNCH_START_CPU)
    SETUP_CPU_BEFORE_TRACKER = setup_cpu
    ATTEMPT["setup_cpu_seconds_before_tracker"] = setup_cpu
    _install_bounded_enforcement(setup_cpu=setup_cpu)
    # Install the bounded guard before the composed R2/cache hooks capture it.
    # R2 imported its guard function by value; replace that binding so the old
    # prospective forecast gate cannot block this authorized sample.
    run_study_r2.enforce_budget = bridge._dynamic_enforce
    if run_study_r2.RUN_STUDY is None:
        run_study_r2.RUN_STUDY = run_study
        run_study_r2._install_runtime_hooks()
    if parallel.DISPATCHER is None or parallel.DISPATCHER.workers != 2:
        raise bridge.CostRegimeError("bounded sampling requires the reviewed two-worker dispatcher")
    run_study_r2.STAGE_START_CPU = stage_start_cpu
    run_study_r2.MINIBATCHES_COMPLETED = 0
    try:
        # Same order as the reviewed continuation: ordinary R2 hooks first,
        # then the cached-path guard wrapper installed by this launcher.
        run_study_r2._install_runtime_hooks()
        if HOOK_EVENTS != ["original-r2-runtime-hooks", "cached-guard-installed"]:
            raise bridge.CostRegimeError("cached continuation hook order did not match the reviewed witness")
        if not continuation._cached_guard_is_active(bridge):
            raise bridge.CostRegimeError("actual R2 stage did not install the cached-path guard")
        cache_sample = bridge._cost_sample(bridge_only=True)
        if len(cache_sample) != 59:
            raise bridge.CostRegimeError("active cached unit-cost sample is not the accepted 59-game set")
        with tracker.max_game_cpu.get_lock():
            tracker.max_game_cpu.value = max(cache_sample)
        cache_witness = EXECUTION["pre_dispatch"]["cached_execution_witness"]
        cache_witness.update({
            "guard_active_verified_during_stage": True,
            "guard_install_order_observed": list(HOOK_EVENTS),
            "active_cached_cost_sample_count": len(cache_sample),
            "active_cached_cost_sample_max_process_cpu_seconds": max(cache_sample),
            "dispatcher_in_flight_reserve_initialized_from_cached_sample": True,
        })
        _atomic_json(RECEIPT, EXECUTION)

        config = json.loads(run_study.CONFIG_PATH.read_text(encoding="utf-8"))
        run_study.validate_config(config)
        runtime = run_study.verify_runtime(config)
        vocab = run_study.Vocabulary(run_study.HERE / "vocabulary.json")
        bootstrap, _ = run_study.load_bootstrap(
            run_study.HERE / config["bootstrap"]["checkpoint"],
        )
        config_digest = run_study.config_sha()
        checkpoint_path = EVIDENCE / "training" / "block1" / "latest.pt"
        payload = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
        if (int(payload.get("completed_batches", -1)) != 1
                or payload.get("config_sha256") != config_digest):
            raise bridge.CostRegimeError("checkpoint is not unchanged post-batch-01 state")
        model = run_study.JointChoiceActorCritic(vocab)
        model.load_state_dict(payload["model_state"], strict=True)
        model_sha = run_study.model_fingerprint(model, vocab, config_digest)
        if model_sha != CHECKPOINT_SHA or model_sha != payload.get("model_sha256"):
            raise bridge.CostRegimeError("loaded model differs from the Planner-named checkpoint")

        # The dispatcher selects batch two by slicing 128:256.  This temporary
        # in-memory schedule therefore exposes exactly the 14 authorized keys.
        original_seeds = run_study.TRAINING_SEEDS["block1"]
        run_study.TRAINING_SEEDS["block1"] = _batch02_seed_schedule()
        try:
            missing = []
            for seed in TARGET_SEEDS:
                output = run_study._training_game_dir("block1", 2, seed)
                completed = run_study.successful_attempt(output, model_sha, config_digest)
                if completed is None:
                    missing.append(seed)
                    continue
                existing_attempt, existing_summary = completed
                if (existing_summary.get("stage_key") != STAGE_KEY
                        or int(existing_summary.get("game_seed", -1)) != seed
                        or existing_summary.get("arm") != "residual"
                        or existing_summary.get("model_sha256") != model_sha):
                    raise bridge.CostRegimeError(f"authorized key {seed} has conflicting completed evidence")
                tag_path = _cost_tag_path(existing_attempt.relative_to(HERE).as_posix())
                if not tag_path.is_file():
                    # A prior safe stop may have ended after run completion but
                    # before its cost tag settled; link that same attempt.
                    bridge._tag_attempt(existing_attempt)
            first_seed = TARGET_SEEDS[0]
            kwargs = {
                "seed": first_seed,
                "mode": "residual",
                "model": model,
                "block_name": "block1",
                "runtime_seed": run_study.derive_seed(
                    "training-policy", run_study.MODEL_SEEDS["block1"], first_seed, 2,
                ),
                "output_dir": run_study._training_game_dir("block1", 2, first_seed),
                "model_sha": model_sha,
                "config_digest": config_digest,
                "vocab": vocab,
                "bootstrap": bootstrap,
                "require_targets": True,
                "provenance_config": run_study._current_policy_provenance(
                    config, vocab, model_sha, "residual",
                ),
                "stage_key": STAGE_KEY,
                "arm": "residual",
            }
            results = run_study.run_game(**kwargs)
        finally:
            run_study.TRAINING_SEEDS["block1"] = original_seeds

        dispatcher = parallel.DISPATCHER
        observed = [
            int(row.get("task_key", [None, -1, None])[1])
            for row in dispatcher.evidence().get("executions", [])
        ]
        if sorted(observed) != sorted(missing):
            raise bridge.CostRegimeError(
                f"dispatcher executed unexpected new key set: {sorted(observed)}; expected {sorted(missing)}"
            )
        if run_study_r2.MINIBATCHES_COMPLETED != 0:
            raise bridge.CostRegimeError("bounded sample unexpectedly completed a PPO minibatch")
        if len(results) != 2 or results[1].get("status") != "COMPLETE":
            raise bridge.CostRegimeError("bounded dispatcher did not return a complete game")
        complete = _target_state(run_study, model_sha, config_digest)
        if [row["game_seed"] for row in complete["complete"]] != list(TARGET_SEEDS):
            raise bridge.CostRegimeError("not all 14 authorized keys completed")
        status = "COMPLETE"
        code = 0
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {exc}"
        status = "RESOURCE_BLOCKED" if "RESOURCE_BLOCKED" in failure else "FAILED"
        code = 75 if status == "RESOURCE_BLOCKED" else 2
    _write_stage_status(status, failure)
    return code


def _target_state(run_study: Any, model_sha: str, config_digest: str) -> dict[str, Any]:
    rows = []
    complete = []
    for seed in TARGET_SEEDS:
        output = run_study._training_game_dir("block1", 2, seed)
        attempts = []
        for status_path in sorted(output.glob("attempt-*/status.json")):
            try:
                row = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                row = {"status": "INVALID_STATUS"}
            attempts.append({
                "attempt_path": status_path.parent.relative_to(HERE).as_posix(),
                "status": row.get("status"),
                "failure": row.get("failure"),
                "process_cpu_seconds": row.get("process_cpu_seconds"),
                "status_json_sha256": _sha256(status_path),
                "run_json_sha256": _sha256(status_path.parent / "run.json")
                if (status_path.parent / "run.json").is_file() else None,
                "run_json_exists": (status_path.parent / "run.json").is_file(),
                "status_json_mtime_utc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(status_path.stat().st_mtime),
                ),
            })
        result = run_study.successful_attempt(output, model_sha, config_digest)
        if result is not None:
            attempt, summary = result
            complete.append({
                "game_seed": seed,
                "attempt_path": attempt.relative_to(HERE).as_posix(),
                "run_json_sha256": _sha256(attempt / "run.json"),
                "process_cpu_seconds": float(summary["process_cpu_seconds"]),
                "terminal_floor": summary.get("terminal_floor"),
                "status": summary.get("status"),
            })
        rows.append({"game_seed": seed, "attempts": attempts,
                     "status": "COMPLETE" if result is not None else (
                         attempts[-1]["status"] if attempts else "NOT_STARTED"
                     )})
    return {"complete": complete, "keys": rows}


def _recover_known_pre_dispatch_failure(previous: dict[str, Any], *, head: str,
                                        launcher_sha256: str,
                                        target_state: dict[str, Any]) -> None:
    """Settle the one observed no-dispatch failure without resetting charges."""
    attempts = list(previous.get("attempts", []))
    if (previous.get("status") != "FINALIZATION_FAILED"
            or previous.get("finalization_failure") != "NameError: name 'FORECAST_INDEX' is not defined"
            or not attempts or attempts[-1].get("status") != "RUNNING"):
        raise bridge.CostRegimeError("previous bounded attempt has no recognized safe recovery basis")
    attempt = attempts[-1]
    stage_path = HERE / str(attempt.get("stage_receipt_path", ""))
    if stage_path.exists() or attempt.get("completed_games"):
        raise bridge.CostRegimeError("failed bounded attempt may have dispatched work; stop for Planner review")
    if target_state["complete"]:
        raise bridge.CostRegimeError("unexpected completed target key after pre-dispatch failure")

    retained_incomplete = []
    for row in target_state["keys"]:
        for observed in row["attempts"]:
            retained_incomplete.append({"game_seed": row["game_seed"], **observed})
    retained_incomplete.sort(key=lambda row: (row["game_seed"], row["attempt_path"]))
    expected_old_paths = {
        "evidence/runs/training/block1/batch-02/seed-660243/attempt-0001",
        "evidence/runs/training/block1/batch-02/seed-660244/attempt-0001",
    }
    if ({row["attempt_path"] for row in retained_incomplete} != expected_old_paths
            or any(row["status"] != "INCOMPLETE" or row["run_json_exists"]
                   for row in retained_incomplete)):
        raise bridge.CostRegimeError(
            "pre-dispatch recovery did not preserve the two known INCOMPLETE attempts exactly"
        )
    started_epoch = calendar.timegm(time.strptime(previous["started_utc"], "%Y-%m-%dT%H:%M:%SZ"))
    if any((HERE / row["attempt_path"] / "status.json").stat().st_mtime > started_epoch
           for row in retained_incomplete):
        raise bridge.CostRegimeError("an original incomplete attempt changed during the failed invocation")

    code_charge = sum(
        float(row.get("process_cpu_seconds", 0.0)) for row in _ledger_rows()
        if row.get("attempt") == f"issue60-batch02-sampling-code-checks-{AUTHORIZATION}"
    )
    finalization_charge = sum(
        float(row.get("process_cpu_seconds", 0.0)) for row in _ledger_rows()
        if row.get("attempt") == f"{attempt.get('attempt_id')}-parent-and-finalization"
    )
    if code_charge < CODE_AND_CHECK_RESERVE or finalization_charge < INCREMENTAL_FINALIZATION_RESERVE:
        raise bridge.CostRegimeError("pre-dispatch failure charges are missing from the durable CPU ledger")

    old_head = str(previous.get("executing_head", ""))
    old_launcher_sha256 = str(previous.get("launcher_sha256", ""))
    attempt.update({
        "status": "FAILED_BEFORE_DISPATCH",
        "failure": "ResourceAmendmentErrorR2: R2 source hash mismatch: test_resource_amendment_r2.py",
        "result_code": 2,
        "executing_head": old_head,
        "launcher_sha256": old_launcher_sha256,
        "authorized_aggregate_cpu_seconds": finalization_charge,
        "complete_keys": [],
        "incomplete_or_unstarted_keys": list(TARGET_SEEDS),
        "pre_dispatch_failure_recovery": {
            "status": "PASS",
            "finalization_failure_repaired": previous["finalization_failure"],
            "parallel_stage_receipt_absent": True,
            "completed_games_in_attempt_receipt": 0,
            "new_complete_target_games": 0,
            "preserved_original_incomplete_attempts": retained_incomplete,
            "cpu_charges": {
                "implementation_and_check_reserve_seconds": code_charge,
                "attempt_finalization_reserve_seconds": finalization_charge,
                "pre_dispatch_parent_setup_covered_by_implementation_and_check_reserve": True,
            },
            "recovery_action": "settle this attempt as a no-dispatch failure, then continue only the same 14 named keys under the same authorization",
        },
    })
    previous["attempts"][-1] = attempt
    previous["status"] = "FAILED"
    previous["executing_head"] = head
    previous["launcher_sha256"] = launcher_sha256
    previous["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_json(RECEIPT, previous)


def _validated_continuation_receipt() -> tuple[dict[str, Any], dict[str, Any]]:
    """Honor the accepted binding across its later provenance-only launcher edit."""
    stored = bridge._read_json(continuation.BINDING)
    if (stored.get("schema_id") != "issue60-continuation-validation-binding-v1"
            or stored.get("status") != "PASS"):
        raise bridge.CostRegimeError("continuation validation binding is missing or invalid")
    relative = "studies/issue-60-onpolicy-joint-choice/continue_after_cost_bridge.py"
    bound_launcher = bridge.git_bytes(
        HERE, "show", f"{BOUND_CONTINUATION_LAUNCHER_COMMIT}:{relative}",
    )
    current_base_launcher = bridge.git_bytes(
        HERE, "show", f"{BASE_HANDOFF}:{relative}",
    )
    bound_launcher_sha = hashlib.sha256(bound_launcher).hexdigest()
    current_base_sha = hashlib.sha256(current_base_launcher).hexdigest()
    current_launcher_sha = _sha256(Path(continuation.__file__))
    if stored.get("launcher_sha256") != bound_launcher_sha:
        raise bridge.CostRegimeError("continuation binding does not match its retained launcher source")
    if current_launcher_sha != current_base_sha:
        raise bridge.CostRegimeError("continuation launcher differs from the Planner-accepted provenance correction")
    facts = continuation._facts()
    if stored.get("facts") != facts:
        raise bridge.CostRegimeError("continuation validation facts changed after the accepted cache witness")
    receipt = {
        "schema_id": "issue60-in-memory-accepted-two-worker-binding-v1",
        "status": "PASS",
        "validation": {
            "status": "PASS",
            "basis": continuation.BINDING.relative_to(HERE).as_posix(),
        },
        "source_files": stored["facts"]["execution_source_files"],
        "worker_selection": {"worker_count": 2},
        "binding_sha256": _sha256(continuation.BINDING),
    }
    compatibility = {
        "status": "PASS",
        "binding_launcher_commit": BOUND_CONTINUATION_LAUNCHER_COMMIT,
        "binding_launcher_sha256": bound_launcher_sha,
        "current_launcher_base_commit": BASE_HANDOFF,
        "current_launcher_sha256": current_launcher_sha,
        "facts_match_retained_binding": True,
        "binding_mutated": False,
        "accepted_change": "Planner-accepted historical provenance correction only",
    }
    return receipt, compatibility


def _write_bounded_receipt(preflight: dict[str, Any], *, status: str,
                           failure: str | None, stage_receipt: dict[str, Any] | None,
                           forecast: dict[str, Any] | None = None) -> dict[str, Any]:
    global EXECUTION
    import run_study

    state = _target_state(
        run_study, CHECKPOINT_SHA, run_study.config_sha(),
    )
    linked_tags = []
    for result in state["complete"]:
        tag_path = _cost_tag_path(result["attempt_path"])
        if tag_path.is_file():
            tag = json.loads(tag_path.read_text(encoding="utf-8"))
            linked_tags.append({
                "game_seed": result["game_seed"],
                "tag_path": tag_path.relative_to(HERE).as_posix(),
                "tag_sha256": _sha256(tag_path),
                "attempt_path": result["attempt_path"],
                "run_json_sha256": result["run_json_sha256"],
                "tag_producer_commit": tag.get("producer_commit"),
                "tag_producer_files": tag.get("producer_files"),
            })
    execution = {
        "schema_id": "issue60-batch02-sampling-execution-v1",
        "authorization_comment_id": AUTHORIZATION,
        "status": status,
        "failure": failure,
        "scope": "block1:batch-02 exact original game keys 660243..660256 only",
        "base_reviewed_handoff": BASE_HANDOFF,
        "attempts": EXECUTION.get("attempts", []),
        "executing_head": preflight["executing_head"],
        "launcher_path": Path(__file__).resolve().relative_to(HERE).as_posix(),
        "launcher_sha256": preflight["launcher_sha256"],
        "source_revision_rollovers": preflight["source_revision_rollovers"],
        "r2_source_compatibility": preflight["r2_source_compatibility"],
        "started_utc": EXECUTION.get("started_utc"),
        "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runtime": preflight["runtime"],
        "config_sha256": preflight["config_sha256"],
        "checkpoint": preflight["checkpoint"],
        "cached_execution_witness": preflight["cached_execution_witness"],
        "cost_index_before": preflight["cost_index_before"],
        "cost_index_after_path": FORECAST_INDEX.relative_to(HERE).as_posix(),
        "cost_index_after_sha256": _sha256(FORECAST_INDEX),
        "legacy_forecasts_preserved": preflight["legacy_forecasts_preserved"],
        "authorized_keys": list(TARGET_SEEDS),
        "complete_keys": [row["game_seed"] for row in state["complete"]],
        "incomplete_or_unstarted_keys": [
            row["game_seed"] for row in state["keys"] if row["status"] != "COMPLETE"
        ],
        "per_key_attempts": state["keys"],
        "complete_game_costs": state["complete"],
        "cost_tags_linking_observations_to_this_receipt": linked_tags,
        "parallel_stage_receipt": stage_receipt,
        "optimizer_updates": 0,
        "evaluation_runs": 0,
        "full_grid_forecast": forecast,
        "cumulative_accounted_builder_cpu_seconds": 600.0 + bridge._ledger_total(),
        "cumulative_builder_cap_cpu_seconds": 180000.0,
        "cumulative_process_cap_cpu_seconds": 200000.0,
        "additional_authorized_limit_cpu_seconds": LOCAL_LIMIT,
        "additional_authorized_charged_cpu_seconds": _authorization_charges(),
        "limitations": [
            "The 114 uncached historical batch-02 games still have unknown actual producer provenance.",
            "The updated cost index is operational evidence, not a scientific result or permission to continue.",
            "The retained legacy forecast remains historical; the new forecast is reported separately.",
        ],
        "disposition": "STUDY_ONLY / NO_SCIENTIFIC_RESULT",
    }
    EXECUTION.clear()
    EXECUTION.update(execution)
    _atomic_json(RECEIPT, execution)
    return execution


def _preflight() -> dict[str, Any]:
    import run_study
    import run_study_r2
    import resource_amendment_r1
    import resource_amendment_r2
    from prospective_cost_regime import (
        BRIDGE_SEEDS, BRIDGE_STAGE, load_cost_tags, producer_manifest,
    )
    import torch

    head = bridge.git(HERE, "rev-parse", "HEAD")
    bridge._bind_resource_git_to_windows_worktree()
    tracked_status = resource_amendment_r1._git(
        HERE, "status", "--porcelain", "--untracked-files=no",
    )
    mutable_ledger = (HERE / "evidence" / "cpu-ledger.jsonl").resolve()
    repo_root = resource_amendment_r1._repo_root(HERE).resolve()
    mutable_ledger_path = mutable_ledger.relative_to(repo_root).as_posix()
    unexpected = []
    for line in tracked_status.splitlines():
        fields = line.split(maxsplit=1)
        path = fields[-1].replace("\\", "/") if len(fields) == 2 else ""
        if path != mutable_ledger_path:
            unexpected.append(line)
    if unexpected:
        raise bridge.CostRegimeError(
            f"tracked source worktree has unexpected changes before bounded dispatch: {unexpected}"
        )
    bridge.git(HERE, "merge-base", "--is-ancestor", BASE_HANDOFF, head)
    r2_source_compatibility = _r2_source_compatibility(resource_amendment_r2, head)
    manifest = producer_manifest(HERE)
    gate = bridge._read_json(PROSPECTIVE / "bridge-gate.json")
    bridge_record = bridge._read_json(bridge.BRIDGE_LATEST)
    if (gate.get("status") != "PASS" or bridge_record.get("status") != "COMPLETE"
            or gate.get("source_files") != manifest
            or gate.get("completed_tag_count") not in (None, 59)):
        raise bridge.CostRegimeError("cached continuation gate no longer verifies")
    bridge.PRODUCER_COMMIT = str(gate["source_commit"])
    bridge.PRODUCER_HASHES = manifest
    if bridge.TAG_FAILURE.is_file():
        raise bridge.CostRegimeError("previous cost-tagging failure requires review before dispatch")
    validation, binding_compatibility = _validated_continuation_receipt()
    accepted = bridge._verify_accepted_two_worker_evidence()
    config = json.loads(run_study.CONFIG_PATH.read_text(encoding="utf-8"))
    run_study.validate_config(config)
    runtime = run_study.verify_runtime(config)
    vocab = run_study.Vocabulary(run_study.HERE / "vocabulary.json")
    payload = torch.load(
        EVIDENCE / "training" / "block1" / "latest.pt",
        map_location="cpu", weights_only=False,
    )
    model = run_study.JointChoiceActorCritic(vocab)
    model.load_state_dict(payload["model_state"], strict=True)
    model_sha = run_study.model_fingerprint(model, vocab, run_study.config_sha())
    checkpoint_path = EVIDENCE / "training" / "block1" / "latest.pt"
    if (int(payload.get("completed_batches", -1)) != 1
            or payload.get("config_sha256") != run_study.config_sha()
            or model_sha != CHECKPOINT_SHA):
        raise bridge.CostRegimeError("the unchanged post-batch-01 checkpoint did not verify")
    tags = load_cost_tags(HERE, current_manifest=manifest)
    cached_tags = [row for row in tags
                   if row.get("stage_key") == BRIDGE_STAGE
                   and int(row.get("game_seed", -1)) in BRIDGE_SEEDS]
    target_tag_seeds = sorted(
        int(row["game_seed"]) for row in tags
        if row.get("stage_key") == STAGE_KEY and int(row.get("game_seed", -1)) in TARGET_SEEDS
    )
    if len(cached_tags) != 59 or len(tags) != 59 + len(target_tag_seeds):
        raise bridge.CostRegimeError("cost index must contain only the original 59 plus authorized target keys")
    if len(target_tag_seeds) != len(set(target_tag_seeds)):
        raise bridge.CostRegimeError("authorized cost sample has duplicate target keys")
    cache_index = bridge._read_json(bridge.FORECAST_INDEX)
    old_forecast_path = bridge.FORECAST_LATEST
    checkpoint = {
        "path": checkpoint_path.relative_to(HERE).as_posix(),
        "sha256": _sha256(checkpoint_path),
        "completed_batches": int(payload["completed_batches"]),
        "model_sha256": model_sha,
    }
    if RECEIPT.is_file():
        previous = json.loads(RECEIPT.read_text(encoding="utf-8"))
        if previous.get("status") == "FINALIZATION_FAILED":
            _recover_known_pre_dispatch_failure(
                previous, head=head, launcher_sha256=_sha256(Path(__file__)),
                target_state=_target_state(run_study, model_sha, run_study.config_sha()),
            )
        prior_complete = sorted({
            int(row.get("game_seed", -1))
            for attempt in previous.get("attempts", [])
            for row in attempt.get("completed_games", [])
        })
        if not set(target_tag_seeds).issubset(set(prior_complete)):
            raise bridge.CostRegimeError("cost index has target tags not linked from a prior execution receipt")
        source_revision_rollovers = list(previous.get("source_revision_rollovers", []))
        current_launcher_sha = _sha256(Path(__file__))
        if (previous.get("executing_head") != head
                or previous.get("launcher_sha256") != current_launcher_sha):
            target_state = _target_state(run_study, model_sha, run_study.config_sha())
            rollover = _no_dispatch_source_rollover(
                previous, target_state, head=head,
                launcher_sha256=current_launcher_sha,
            )
            source_revision_rollovers.append(rollover)
            previous["source_revision_rollovers"] = source_revision_rollovers
            previous["executing_head"] = head
            previous["launcher_sha256"] = current_launcher_sha
            previous["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _atomic_json(RECEIPT, previous)
        if previous.get("status") == "COMPLETE":
            raise bridge.CostRegimeError("the bounded 14-key authorization is already complete")
        if previous.get("status") not in {"STARTED", "RESOURCE_BLOCKED", "FAILED"}:
            raise bridge.CostRegimeError("previous bounded execution receipt needs Reviewer review")
        if previous.get("status") == "STARTED":
            raise bridge.CostRegimeError("previous bounded invocation did not finalize; stop safely")
        # Failed/blocked attempts may resume only the same fixed keys and source.
    else:
        source_revision_rollovers = []
        if target_tag_seeds:
            raise bridge.CostRegimeError("target cost tags exist without this authorization receipt")
        for seed in TARGET_SEEDS:
            output = run_study._training_game_dir("block1", 2, seed)
            if run_study.successful_attempt(output, model_sha, run_study.config_sha()) is not None:
                raise bridge.CostRegimeError(
                    f"target key {seed} was complete before an authorized receipt existed"
                )
    stage_number = _attempt_number()
    forecast_copy = PROSPECTIVE / "history" / "latest-forecast-before-batch02.json"
    index_copy = PROSPECTIVE / "history" / "cost-index-before-batch02.json"
    for source, destination in ((old_forecast_path, forecast_copy),
                                (bridge.FORECAST_INDEX, index_copy)):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copy2(source, destination)
    return {
        "executing_head": head,
        "launcher_sha256": _sha256(Path(__file__)),
        "source_revision_rollovers": source_revision_rollovers,
        "r2_source_compatibility": r2_source_compatibility,
        "runtime": runtime,
        "config_sha256": run_study.config_sha(),
        "checkpoint": checkpoint,
        "cached_execution_witness": {
            "continuation_binding_path": continuation.BINDING.relative_to(HERE).as_posix(),
            "continuation_binding_sha256": _sha256(continuation.BINDING),
            "continuation_binding_compatibility": binding_compatibility,
            "accepted_two_worker_evidence_path": bridge.ACCEPTED_TWO_WORKER_EVIDENCE.relative_to(HERE).as_posix(),
            "accepted_two_worker_evidence_sha256": _sha256(bridge.ACCEPTED_TWO_WORKER_EVIDENCE),
            "accepted_replay_workers": accepted["two_worker_dispatcher_evidence"],
            "in_memory_validation": validation,
            "guard_install_order_expected": ["original-r2-runtime-hooks", "cached-guard-installed"],
            "guard_active_verified_during_stage": False,
            "guard_install_order_observed": None,
            "active_cached_cost_sample_count": None,
            "active_cached_cost_sample_max_process_cpu_seconds": None,
            "dispatcher_in_flight_reserve_initialized_from_cached_sample": False,
        },
        "cost_index_before": {
            "path": bridge.FORECAST_INDEX.relative_to(HERE).as_posix(),
            "sha256": _sha256(bridge.FORECAST_INDEX),
            "tag_count": cache_index.get("tag_count"),
            "cached_complete_game_count": len(cached_tags),
            "producer_commit": cache_index.get("producer_commit"),
            "producer_files": cache_index.get("producer_files"),
        },
        "legacy_forecasts_preserved": {
            "cost_index_snapshot": {
                "path": index_copy.relative_to(HERE).as_posix(),
                "sha256": _sha256(index_copy),
            },
            "latest_forecast_path": old_forecast_path.relative_to(HERE).as_posix(),
            "latest_forecast_sha256": _sha256(old_forecast_path),
            "forecast_events_sha256": _sha256(bridge.FORECAST_EVENTS)
            if bridge.FORECAST_EVENTS.is_file() else None,
        },
        "attempt_number": stage_number,
    }


def _run_with_reviewed_hooks() -> int:
    global SETUP_CPU_BEFORE_TRACKER, ATTEMPT_ID
    import resource_amendment_r2 as r2

    original_main = parallel.main
    original_read = parallel._read_validation_receipt
    validated_read = lambda: dict(
        EXECUTION["pre_dispatch"]["cached_execution_witness"]["in_memory_validation"]
    )
    original_tag = bridge._tag_attempt
    original_source_scope = r2._verify_source_scope
    source_compatibility = EXECUTION["pre_dispatch"]["r2_source_compatibility"]
    stage_root = PARALLEL_STAGE_DIR / f"attempt-{int(ATTEMPT['attempt_number']):04d}"
    old_parallel_evidence = parallel.EVIDENCE
    old_parallel_validation = parallel.VALIDATION

    def tag_batch02_attempt(attempt_path: Path) -> bool:
        resolved = Path(attempt_path).resolve()
        try:
            relative = resolved.relative_to((EVIDENCE / "runs").resolve()).as_posix()
            run = json.loads((resolved / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if run.get("stage_key") != STAGE_KEY or int(run.get("game_seed", -1)) not in TARGET_SEEDS:
            return False
        result = bool(original_tag(resolved))
        _receipt_progress(resolved)
        return result

    parallel._read_validation_receipt = validated_read
    bridge._tag_attempt = tag_batch02_attempt
    bridge._install_tagged_dispatcher()
    r2._verify_source_scope = lambda root, source_commit, source_hashes, symbol_hashes: (
        _verify_r2_source_scope_for_bounded_sample(
            r2, root, source_commit, source_hashes, symbol_hashes,
            source_compatibility,
        )
    )

    def bounded_parallel_main() -> int:
        global SETUP_CPU_BEFORE_TRACKER
        original_install = bridge.R2._install_runtime_hooks
        original_stage = bridge.R2._run_original_stage
        def read_and_redirect() -> dict[str, Any]:
            receipt = validated_read()
            parallel.EVIDENCE = stage_root
            parallel.VALIDATION = stage_root / "parallel-execution"
            return receipt

        parallel._read_validation_receipt = read_and_redirect
        installer = lambda: continuation._install_cached_guard(bridge, __import__("parallel_execution"))
        bridge.R2._install_runtime_hooks = continuation._wrap_runtime_hooks_with_cache(
            original_install, installer, HOOK_EVENTS,
        )
        bridge.R2._run_original_stage = _sampling_stage
        sys.argv = ["run_study_parallel.py", "--stage", "train"]
        try:
            return int(original_main())
        finally:
            bridge.R2._run_original_stage = original_stage
            bridge.R2._install_runtime_hooks = original_install
            parallel._read_validation_receipt = original_read
            parallel.EVIDENCE = old_parallel_evidence
            parallel.VALIDATION = old_parallel_validation

    parallel.main = bounded_parallel_main
    try:
        # _run_continue binds the validated cache and producer context, then
        # invokes the patched parallel entry point above. Its own stage loop is
        # never reached because the patched CLI selects only train.
        return int(bridge._run_continue())
    finally:
        parallel.main = original_main
        parallel._read_validation_receipt = original_read
        bridge._tag_attempt = original_tag
        r2._verify_source_scope = original_source_scope


def _prepare_receipt(preflight: dict[str, Any]) -> None:
    global EXECUTION, ATTEMPT, ATTEMPT_ID, AUTH_SPENT_BEFORE_ATTEMPT
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    previous = json.loads(RECEIPT.read_text(encoding="utf-8")) if RECEIPT.is_file() else None
    attempts = list(previous.get("attempts", [])) if previous else []
    number = int(preflight["attempt_number"])
    ATTEMPT_ID = f"issue60-batch02-sampling-attempt-{number:04d}"
    ATTEMPT = {
        "attempt_number": number,
        "attempt_id": ATTEMPT_ID,
        "status": "RUNNING",
        "started_utc": now,
        "executing_head": preflight["executing_head"],
        "launcher_sha256": preflight["launcher_sha256"],
        "stage_receipt_path": (
            (PARALLEL_STAGE_DIR / f"attempt-{number:04d}" / "parallel-execution" / "stages" / "train.json")
            .relative_to(HERE).as_posix()
        ),
        "completed_games": [],
    }
    attempts.append(ATTEMPT)
    EXECUTION = {
        "schema_id": "issue60-batch02-sampling-execution-v1",
        "authorization_comment_id": AUTHORIZATION,
        "status": "STARTED",
        "started_utc": previous.get("started_utc", now) if previous else now,
        "executing_head": preflight["executing_head"],
        "launcher_sha256": preflight["launcher_sha256"],
        "attempts": attempts,
        "authorized_keys": list(TARGET_SEEDS),
        "source_revision_rollovers": list(preflight["source_revision_rollovers"]),
    }
    AUTH_SPENT_BEFORE_ATTEMPT = _authorization_charges()
    # Conservative fixed charge for all implementation, focused checks and
    # launcher/preflight work performed under this bounded authorization.
    code_attempt = f"issue60-batch02-sampling-code-checks-{AUTHORIZATION}"
    _append_ledger({
        "attempt": code_attempt,
        "authorization_comment_id": AUTHORIZATION,
        "stage": "issue60-batch02-sampling-code-check-reserve",
        "status": "CHARGED",
        "process_cpu_seconds": CODE_AND_CHECK_RESERVE,
        "source_commit": preflight["executing_head"],
        "timestamp_utc": now,
        "accounting": "fixed conservative reserve for bounded implementation, focused tests and pre-dispatch verification",
    })
    AUTH_SPENT_BEFORE_ATTEMPT = _authorization_charges()
    EXECUTION["pre_dispatch"] = preflight
    EXECUTION["attempts"][-1] = ATTEMPT
    _atomic_json(RECEIPT, EXECUTION)


def _finalize(*, result: int | None, error: BaseException | None) -> dict[str, Any]:
    import run_study
    import resource_amendment_r2
    from resource_amendment_r2 import build_forecast, complete_game_costs, remaining_optimizer_projection

    attempt_number = int(ATTEMPT["attempt_number"])
    stage_path = Path(ATTEMPT["stage_receipt_path"])
    stage_receipt = None
    if (HERE / stage_path).is_file():
        stage_receipt = json.loads((HERE / stage_path).read_text(encoding="utf-8"))
    stage_status = "FAILED"
    if stage_receipt is not None:
        stage_status = str(stage_receipt.get("status", "FAILED"))
    if error is not None:
        stage_status = "RESOURCE_BLOCKED" if "RESOURCE_BLOCKED" in str(error) else "FAILED"
    elif result == 0:
        stage_status = "COMPLETE"
    target = _target_state(run_study, CHECKPOINT_SHA, run_study.config_sha())
    complete_keys = [row["game_seed"] for row in target["complete"]]
    status = "COMPLETE" if complete_keys == list(TARGET_SEEDS) and stage_status == "COMPLETE" else stage_status
    if error is not None and status not in {"RESOURCE_BLOCKED", "FAILED"}:
        status = "FAILED"

    # The parallel stage already charged wait4 worker CPU to the append-only
    # ledger. Charge parent/setup plus a conservative finalization reserve here.
    setup_cpu = max(0.0, float(ATTEMPT.get("setup_cpu_seconds_before_tracker", 0.0)))
    parent_stage_cpu = float((stage_receipt or {}).get("parent_process_cpu_seconds", 0.0))
    finalization_floor = INCREMENTAL_FINALIZATION_RESERVE
    charged_parent = setup_cpu + parent_stage_cpu + finalization_floor
    ledger_attempt = f"{ATTEMPT_ID}-parent-and-finalization"
    _append_ledger({
        "attempt": ledger_attempt,
        "authorization_comment_id": AUTHORIZATION,
        "stage": "issue60-batch02-sampling-parent-and-finalization",
        "status": status,
        "process_cpu_seconds": charged_parent,
        "source_commit": EXECUTION["executing_head"],
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "accounting": "pre-tracker parent/setup CPU + tracked parent-stage CPU + 30 CPU-s finalization reserve; worker CPU is separately charged by the parallel stage receipt",
        "parallel_stage_receipt_path": ATTEMPT["stage_receipt_path"],
    })

    # Reload tags after the dispatcher has tagged only complete authorized keys.
    bridge._COST_TAG_ROWS = None
    bridge._COST_CACHE_INDEX_STAMP = None
    bridge._COST_CACHE_VALUES = []
    tags = bridge.load_cost_tags(HERE, current_manifest=bridge.PRODUCER_HASHES)
    new_rows = [row for row in tags if row.get("stage_key") == STAGE_KEY
                and int(row.get("game_seed", -1)) in TARGET_SEEDS]
    if len(new_rows) != len(target["complete"]):
        status = "FAILED"
        error = error or bridge.CostRegimeError("completed target games and linked cost tags differ")
    if (len(new_rows) > 14 or any(int(row["game_seed"]) not in TARGET_SEEDS for row in new_rows)):
        status = "FAILED"
        error = error or bridge.CostRegimeError("cost index contains a key outside the authorized set")

    formal_complete = [row for row in complete_game_costs(HERE) if row.get("stage_key") != "preflight"]
    remaining_games = max(0, int(resource_amendment_r2.FORMAL_GAME_COUNT) - len(formal_complete))
    optimizer = remaining_optimizer_projection(HERE)
    cached_costs = bridge._cost_sample()
    forecast = build_forecast(
        costs=cached_costs,
        remaining_games=remaining_games,
        remaining_optimizer_cpu=float(optimizer["remaining_optimizer_cpu_seconds"]),
        accounted_cpu=(resource_amendment_r2.STATIC_ALLOWANCE_SECONDS
                       + resource_amendment_r2._ledger_total(HERE)),
    )
    forecast_record = {
        "schema_id": "issue60-batch02-sampling-forecast-v1",
        "authorization_comment_id": AUTHORIZATION,
        "status": "UPDATED_COST_FORECAST",
        "forecast_role": "descriptive updated cached-path projection; not a gate or permission to continue",
        "remaining_formal_games": remaining_games,
        "remaining_optimizer_projection": optimizer,
        "cached_cost_sample_count": len(cached_costs),
        "cached_cost_sample_sha256": _sha256(bridge.FORECAST_INDEX),
        "cost_index_path": bridge.FORECAST_INDEX.relative_to(HERE).as_posix(),
        "forecast": forecast,
        "decision": "STOP_AFTER_AUTHORIZED_BATCH02_SAMPLING_BEFORE_PPO_OR_EVALUATION",
        "legacy_latest_forecast_preserved": True,
        "unknown_historical_114_game_producer_preserved_as_unknown": True,
        "complete_authorized_keys": complete_keys,
        "incomplete_or_unstarted_authorized_keys": [
            row["game_seed"] for row in target["keys"] if row["status"] != "COMPLETE"
        ],
    }
    forecast_path = PROSPECTIVE / "batch02-sampling-forecast.json"
    _atomic_json(forecast_path, forecast_record)
    ATTEMPT["authorized_aggregate_cpu_seconds"] = (
        setup_cpu
        + float((stage_receipt or {}).get("aggregate_process_cpu_seconds", 0.0))
        + INCREMENTAL_FINALIZATION_RESERVE
    )
    EXECUTION["attempts"][-1] = ATTEMPT
    receipt = _write_bounded_receipt(
        EXECUTION["pre_dispatch"], status=status,
        failure=f"{type(error).__name__}: {error}" if error is not None else None,
        stage_receipt=stage_receipt,
        forecast={
            "path": forecast_path.relative_to(HERE).as_posix(),
            "sha256": _sha256(forecast_path),
            "remaining_formal_games": remaining_games,
            "optimizer_remaining_cpu_seconds": optimizer["remaining_optimizer_cpu_seconds"],
            "forecast": forecast,
        },
    )
    ATTEMPT["status"] = status
    ATTEMPT["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ATTEMPT["result_code"] = result
    ATTEMPT["failure"] = f"{type(error).__name__}: {error}" if error is not None else None
    ATTEMPT["complete_keys"] = complete_keys
    ATTEMPT["incomplete_or_unstarted_keys"] = [
        row["game_seed"] for row in target["keys"] if row["status"] != "COMPLETE"
    ]
    ATTEMPT["parallel_stage_receipt"] = {
        "path": ATTEMPT["stage_receipt_path"],
        "sha256": _sha256(HERE / stage_path) if (HERE / stage_path).is_file() else None,
        "status": stage_status,
        "aggregate_process_cpu_seconds": (stage_receipt or {}).get("aggregate_process_cpu_seconds"),
        "worker_process_cpu_seconds": (stage_receipt or {}).get("worker_process_cpu_seconds"),
        "execution_count": (stage_receipt or {}).get("execution_count"),
        "max_active_workers": (stage_receipt or {}).get("max_active_workers"),
        "max_configured_workers": (stage_receipt or {}).get("max_configured_workers"),
    }
    ATTEMPT["authorized_aggregate_cpu_seconds"] = (
        float(ATTEMPT.get("setup_cpu_seconds_before_tracker", 0.0))
        + float((stage_receipt or {}).get("aggregate_process_cpu_seconds", 0.0))
        + INCREMENTAL_FINALIZATION_RESERVE
    )
    EXECUTION["attempts"][-1] = ATTEMPT
    _atomic_json(RECEIPT, EXECUTION)
    return receipt


def main() -> int:
    global LAUNCH_START_CPU, ATTEMPT, ATTEMPT_ID, EXECUTION
    LAUNCH_START_CPU = time.process_time()
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.verify_only == args.run:
        parser.error("choose exactly one of --verify-only or --run")
    preflight = _preflight()
    if args.verify_only:
        print(json.dumps({"status": "PASS", "preflight": preflight}, sort_keys=True))
        return 0

    _prepare_receipt(preflight)
    result = None
    failure = None
    try:
        # Cap the parent process as a secondary stop; the aggregate tracker
        # applies the live two-worker reserve before every dispatch/decision.
        from resource_amendment_r2 import (
            BUILDER_BUDGET_SECONDS, STATIC_ALLOWANCE_SECONDS,
            PROCESS_CPU_CAP_SECONDS, REVIEWER_RESERVE_SECONDS,
            ResourceBudgetExceeded, _ledger_total, install_process_cpu_limit,
        )
        current_actual = STATIC_ALLOWANCE_SECONDS + _ledger_total(HERE)
        setup_before_tracker = max(0.0, time.process_time() - LAUNCH_START_CPU)
        local_remaining = _remaining_local_budget(
            charged=AUTH_SPENT_BEFORE_ATTEMPT,
            setup=setup_before_tracker,
            finalization_reserve=INCREMENTAL_FINALIZATION_RESERVE,
        )
        cap_remaining = min(
            BUILDER_BUDGET_SECONDS - current_actual,
            PROCESS_CPU_CAP_SECONDS - REVIEWER_RESERVE_SECONDS - current_actual,
        )
        remaining = min(local_remaining, cap_remaining)
        if remaining <= 0:
            raise ResourceBudgetExceeded("RESOURCE_BLOCKED before bounded dispatch: no safe CPU remains")
        # The standard R2 launcher installs the same POSIX CPU ceiling for each
        # ordinary stage; this bounded launcher applies the smaller allowance.
        from resource_amendment_r2 import _read_json
        import run_study_r2
        limit = install_process_cpu_limit(
            current_process_cpu=time.process_time(), remaining_cpu=remaining,
        )
        EXECUTION["process_cpu_limit"] = limit
        EXECUTION["attempts"][-1] = ATTEMPT
        _atomic_json(RECEIPT, EXECUTION)
        result = _run_with_reviewed_hooks()
        return int(result)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            receipt = _finalize(result=result, error=failure)
            print(json.dumps({
                "status": receipt["status"],
                "execution_receipt": RECEIPT.relative_to(HERE).as_posix(),
                "complete_keys": receipt["complete_keys"],
                "incomplete_or_unstarted_keys": receipt["incomplete_or_unstarted_keys"],
                "optimizer_updates": 0,
                "evaluation_runs": 0,
            }, sort_keys=True))
        except BaseException as finalize_error:
            # Keep the STARTED receipt and append a conservative stop marker if
            # finalization itself fails. Never erase partial attempt evidence.
            if RECEIPT.is_file():
                current = json.loads(RECEIPT.read_text(encoding="utf-8"))
                current["finalization_failure"] = f"{type(finalize_error).__name__}: {finalize_error}"
                current["status"] = "FINALIZATION_FAILED"
                _atomic_json(RECEIPT, current)
            raise


if __name__ == "__main__":
    raise SystemExit(main())
