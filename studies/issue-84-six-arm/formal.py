"""Resumable source-locked formal train, validation, test, and analysis stages."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import torch

import driver as d
from gradients import ScalarPredictor
from predictors import (
    build_predictor_cache,
    discard_predictor_cache,
    fit_cached_prior_block_predictors,
    new_predictor_optimizer,
)
from training import optimizer_parameter_steps, train_actor_block


FORMAL_BLOCKS = 16
FORMAL_OWNERS_PER_BLOCK = 128
PREDICTOR_PRESENTATIONS_PER_ARM = 15_360
BOOTSTRAP_REPLICATES = 20_000


def _hash(value: object) -> str:
    return hashlib.sha256(d.canonical(value)).hexdigest()


def _restore_predictors(state: Mapping[str, Any], arm: str, value, value_optimizer, action_value, action_optimizer) -> None:
    if state.get("arm") != arm:
        raise RuntimeError("predictor checkpoint is bound to a different arm")
    value.load_state_dict(state["value_model_state_dict"], strict=True)
    value_optimizer.load_state_dict(state["value_optimizer_state_dict"])
    if arm == "E":
        if action_value is None or action_optimizer is None:
            raise RuntimeError("E predictor checkpoint requires its registered Q model")
        action_value.load_state_dict(state["action_value_model_state_dict"], strict=True)
        action_optimizer.load_state_dict(state["action_value_optimizer_state_dict"])
    elif state.get("action_value_model_state_dict") is not None or state.get("action_value_optimizer_state_dict") is not None:
        raise RuntimeError("non-E predictor checkpoint unexpectedly contains a Q model")


def _new_predictors(arm: str):
    value = ScalarPredictor(seed=d.derive_seed("formal", arm, "V"))
    value_optimizer = new_predictor_optimizer(value)
    action_value = action_optimizer = None
    if arm == "E":
        action_value = ScalarPredictor(seed=d.derive_seed("formal", arm, "Q"))
        action_optimizer = new_predictor_optimizer(action_value)
    return value, value_optimizer, action_value, action_optimizer


def _block_rows(table: Mapping[str, Any], arm: str, block: int) -> list[dict[str, Any]]:
    start = 8_400_001 + block * FORMAL_OWNERS_PER_BLOCK
    return [
        row for row in d.seed_table_rows(
            table, phase="training", formal=True,
            owners=range(start, start + FORMAL_OWNERS_PER_BLOCK),
        )
        if row.get("issue84_arm") == arm
    ]


def _cached_game_result(rows, *, root: Path, package: str, backend) -> dict[str, Any] | None:
    results = {}
    for row in rows:
        report = backend.existing_success(root, row, package)
        if report is None:
            return None
        results[str(row["unit_id"])] = report
    return {
        "status": "SUCCEEDED", "completed": len(results), "results": results,
        "elapsed_seconds": 0.0, "native_wall_seconds_by_unit": {}, "tries": {},
    }


def _iter_predictor_history(arm: str, block: int, *, root: Path, executor, package: str):
    """Yield one native outcome at a time to keep formal fitting memory bounded."""
    table = d.ensure_seed_table()
    for prior in range(block):
        rows = _block_rows(table, arm, prior)
        if len(rows) != FORMAL_OWNERS_PER_BLOCK:
            raise RuntimeError(f"arm {arm} prior block {prior} has the wrong owner count")
        for row in sorted(rows, key=lambda item: int(item["owner_seed"])):
            _, outcomes = d.load_training_outcomes([row], root=root, executor=executor, package=package)
            if len(outcomes) != 1:
                raise RuntimeError(f"arm {arm} block {prior} owner did not produce exactly one outcome")
            yield prior, outcomes


def _predictor_state(value, value_optimizer, action_value, action_optimizer) -> dict[str, Any]:
    return {
        "value_model_state_dict": value.state_dict(),
        "value_optimizer_state_dict": value_optimizer.state_dict(),
        "action_value_model_state_dict": None if action_value is None else action_value.state_dict(),
        "action_value_optimizer_state_dict": None if action_optimizer is None else action_optimizer.state_dict(),
    }


def _prepare_predictors(
    arm: str, block: int, *, root: Path, executor, package: str,
    value, value_optimizer, action_value, action_optimizer,
) -> tuple[dict[str, Any], str]:
    predictor_dir = root / "models" / "predictors"
    predictor_path = predictor_dir / f"block-{block:02d}-before.pt"
    prior_manifests = []
    for prior in range(block):
        prior_rows = _block_rows(d.ensure_seed_table(), arm, prior)
        prior_manifests.append(d.owner_feature_manifest(prior_rows, root=root, package=package))
    binding = {
        "schema_id": "issue84-formal-predictor-checkpoint-v1",
        "arm": arm,
        "block_index": block,
        "completed_prior_blocks": list(range(block)),
        "prior_block_feature_manifest_sha256": prior_manifests,
        "source_lock_sha256": d.file_sha256(d.SOURCE_LOCK_PATH),
        "package_commit": package,
        "prior_block_indices": list(range(block)),
    }
    if predictor_path.is_file():
        state = torch.load(predictor_path, map_location="cpu", weights_only=False)
        if any(state.get(key) != val for key, val in binding.items()):
            raise RuntimeError(f"predictor checkpoint binding differs for arm {arm} block {block}")
        _restore_predictors(state, arm, value, value_optimizer, action_value, action_optimizer)
        discard_predictor_cache(root / "predictor-cache" / f"{arm}-block-{block:02d}", binding=binding)
        return state["fit_receipt"], d.file_sha256(predictor_path)

    if block:
        previous_path = predictor_dir / f"block-{block - 1:02d}-before.pt"
        if not previous_path.is_file():
            raise FileNotFoundError(f"prior frozen predictor state is missing: {previous_path}")
        previous = torch.load(previous_path, map_location="cpu", weights_only=False)
        if previous.get("block_index") != block - 1 or previous.get("arm") != arm:
            raise RuntimeError("prior predictor checkpoint is not the immediately preceding block")
        _restore_predictors(previous, arm, value, value_optimizer, action_value, action_optimizer)
        cache_path = root / "predictor-cache" / f"{arm}-block-{block:02d}"
        cache_manifest = build_predictor_cache(
            _iter_predictor_history(arm, block, root=root, executor=executor, package=package),
            cache_path=cache_path,
            binding=binding,
        )
    else:
        cache_path = root / "predictor-cache" / f"{arm}-block-{block:02d}"
        cache_manifest = build_predictor_cache((), cache_path=cache_path, binding=binding)
    fit = fit_cached_prior_block_predictors(
        arm=arm, current_block_index=block,
        cache_path=cache_path, cache_manifest=cache_manifest,
        value_model=value, value_optimizer=value_optimizer,
        action_value_model=action_value, action_value_optimizer=action_optimizer,
    )
    state = {**binding, **_predictor_state(value, value_optimizer, action_value, action_optimizer), "fit_receipt": fit}
    executor._atomic_torch_save(predictor_path, state)
    discard_predictor_cache(cache_path, binding=binding)
    return fit, d.file_sha256(predictor_path)


def _train_actor_block(arm: str, block: int, rows, *, root: Path, package: str, clock, progress, backend) -> dict[str, Any]:
    algorithm, runtime, model, learning, executor = d.load_modules(root, pilot=False)
    examples, outcomes = d.load_training_outcomes(rows, root=root, executor=executor, package=package)
    if len(examples) != FORMAL_OWNERS_PER_BLOCK:
        raise RuntimeError(f"arm {arm} block {block} does not have 128 complete native outcomes")
    expected_owners = [int(row["owner_seed"]) for row in sorted(rows, key=lambda row: int(row["owner_seed"]))]
    if [int(item.owner_seed) for item in examples] != expected_owners:
        raise RuntimeError(f"arm {arm} block {block} owner order differs from the registered seed table")

    source_label = f"pi{12 + block}"
    actor, source_digest, source_record = executor._load_model(source_label)
    value, value_optimizer, action_value, action_optimizer = _new_predictors(arm)
    fit, predictor_sha = _prepare_predictors(
        arm, block, root=root, executor=executor, package=package,
        value=value, value_optimizer=value_optimizer,
        action_value=action_value, action_optimizer=action_optimizer,
    )
    behavior_actor = copy.deepcopy(actor).eval() if arm == "F" else None
    if block == 0:
        parent_optimizer_path = d.host_path(d.PARENT_MODELS_WSL) / "block-11-step.pt"
        if d.file_sha256(parent_optimizer_path) != d.PARENT_OPTIMIZER_SHA256:
            raise RuntimeError("formal block 0 cannot inherit the exact #82 AdamW bytes")
        inherited = torch.load(parent_optimizer_path, map_location="cpu", weights_only=True)
        if inherited.get("completed_steps") != 64:
            raise RuntimeError("formal block 0 parent optimizer is not exactly step 64")
        optimizer = learning._new_optimizer(actor)
        optimizer.load_state_dict(inherited["optimizer_state_dict"])
    else:
        optimizer = learning._new_optimizer(actor)

    ordered_rows = sorted(rows, key=lambda row: int(row["owner_seed"]))
    owner_seed_sha = hashlib.sha256(",".join(str(seed) for seed in expected_owners).encode("ascii")).hexdigest()
    feature_sha = d.owner_feature_manifest(rows, root=root, package=package)
    order_seed = d.derive_seed("formal", arm, "block-order", block)
    checkpoint_path = root / "models" / "blocks" / f"block-{block:02d}-step.pt"
    update_path = root / "models" / "blocks" / f"block-{block:02d}-update.json"
    model_label = f"pi{13 + block}"
    model_path = executor._model_path(model_label)
    resume = None
    if checkpoint_path.is_file():
        resume = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
        required = {
            "schema_id": "issue84-actor-step-checkpoint-v1",
            "arm": arm,
            "block_index": block,
            "block_order_seed": order_seed,
            "source_model_digest": source_digest,
            "owner_seed_sha256": owner_seed_sha,
            "owner_training_feature_manifest_sha256": feature_sha,
            "predictor_checkpoint_sha256": predictor_sha,
            "source_lock_sha256": d.file_sha256(d.SOURCE_LOCK_PATH),
            "package_commit": package,
        }
        if any(resume.get(key) != val for key, val in required.items()):
            raise RuntimeError(f"actor-step checkpoint binding differs for arm {arm} block {block}")
        if arm == "F":
            behavior_actor.load_state_dict(resume["behavior_actor_state_dict"], strict=True)

    def save_step(step, trained, current_optimizer, rng_state, order, diagnostics):
        if clock.expired():
            raise RuntimeError("the single task-wide active-wall allowance expired during an actor update")
        executor._atomic_torch_save(checkpoint_path, {
            "schema_id": "issue84-actor-step-checkpoint-v1",
            "arm": arm,
            "block_index": block,
            "completed_steps": int(step),
            "block_order_seed": order_seed,
            "source_model_digest": source_digest,
            "owner_seed_sha256": owner_seed_sha,
            "owner_training_feature_manifest_sha256": feature_sha,
            "actor_state_dict": trained.state_dict(),
            "optimizer_state_dict": current_optimizer.state_dict(),
            "python_rng_state": rng_state,
            "owner_order": [int(index) for index in order],
            "diagnostics": list(diagnostics),
            "value_model_state_dict": value.state_dict(),
            "value_optimizer_state_dict": value_optimizer.state_dict(),
            "action_value_model_state_dict": None if action_value is None else action_value.state_dict(),
            "action_value_optimizer_state_dict": None if action_optimizer is None else action_optimizer.state_dict(),
            "behavior_actor_state_dict": None if behavior_actor is None else behavior_actor.state_dict(),
            "predictor_checkpoint_sha256": predictor_sha,
            "source_lock_sha256": d.file_sha256(d.SOURCE_LOCK_PATH),
            "package_commit": package,
        })

    if update_path.is_file():
        receipt = d.read_json(update_path)
        if receipt.get("source_model_digest") != source_digest or receipt.get("owner_seed_sha256") != owner_seed_sha:
            raise RuntimeError(f"completed actor block receipt differs for arm {arm} block {block}")
        saved, digest, _ = executor._load_model(model_label)
        if receipt.get("result_model_digest") != digest or receipt.get("checkpoint_sha256") != d.file_sha256(checkpoint_path):
            raise RuntimeError(f"completed actor block bytes differ for arm {arm} block {block}")
        return receipt

    began = time.monotonic()
    result = train_actor_block(
        actor, optimizer, outcomes, arm=arm, block_index=block,
        block_order_seed=order_seed, value_model=value,
        action_value_model=action_value, behavior_actor=behavior_actor,
        pilot=False, resume_state=resume, step_callback=save_step,
    )
    model_module = __import__("model")
    model_record = executor._save_model_once(actor, model_path, model_module.model_digest)
    actual_steps = optimizer_parameter_steps(optimizer)
    expected_steps = 128 if block == 0 else 64
    if not actual_steps or set(actual_steps) != {expected_steps}:
        raise RuntimeError("formal block optimizer steps differ from the registered 64/128 lineage")
    receipt = {
        "schema_id": "issue84-formal-actor-update-v1",
        "status": "SUCCEEDED",
        "arm": arm,
        "block_index": block,
        "owner_count": len(outcomes),
        "owner_seed_sha256": owner_seed_sha,
        "owner_training_feature_manifest_sha256": feature_sha,
        "source_model_digest": source_digest,
        "source_model_checkpoint_sha256": source_record.get("checkpoint_sha256"),
        "result_model_digest": model_module.model_digest(actor),
        "model_checkpoint": model_record,
        "model_checkpoint_sha256": d.file_sha256(model_path),
        "optimizer_checkpoint_sha256": d.file_sha256(checkpoint_path),
        "predictor_checkpoint_sha256": predictor_sha,
        "predictor_fit": fit,
        "optimizer_parameter_steps": actual_steps,
        "inherited_parent_optimizer_sha256": d.PARENT_OPTIMIZER_SHA256 if block == 0 else None,
        "completed_steps": result["completed_steps"],
        "block_order_seed": order_seed,
        "resume_completed_steps": 0 if resume is None else int(resume["completed_steps"]),
        "diagnostics": result["diagnostics"],
        "elapsed_seconds": time.monotonic() - began,
        "source_lock_sha256": d.file_sha256(d.SOURCE_LOCK_PATH),
        "package_commit": package,
    }
    backend.write_json_once(update_path, receipt)
    return receipt


def _record_game_group(group: str, rows, *, root: Path, pilot: bool, package: str, clock, backend) -> dict[str, Any]:
    result = _cached_game_result(rows, root=root, package=package, backend=backend)
    if result is None:
        result = backend.run_units(
            rows, root=root, pilot=pilot, clock=clock, package_commit=package,
        )
    summary = d.run_summary(result, rows=rows, root=root, package=package)
    expected = len(rows)
    if summary["status"] != "SUCCEEDED" or summary["completed"] != expected:
        return {
            "schema_id": "issue84-game-group-v1", "status": summary["status"],
            "group": group, "expected": expected, "completed": summary["completed"],
            "remaining": expected - summary["completed"], "run_summary": summary,
        }
    if any(item.get("expert_decisions") != 0 or item.get("terminal_floor") is None for item in summary["outcomes"]):
        raise RuntimeError(f"group {group} contains Expert use or an incomplete terminal")
    return {
        "schema_id": "issue84-game-group-v1", "status": "SUCCEEDED",
        "group": group, "expected": expected, "completed": expected,
        "run_summary": summary,
    }


def _verify_saved_game_group(record: Mapping[str, Any], rows, *, root: Path, package: str, backend) -> dict[str, Any]:
    summary = record.get("run_summary", {})
    if record.get("status") != "SUCCEEDED" or summary.get("status") != "SUCCEEDED":
        raise RuntimeError("saved game-group record is not a completed success")
    by_id = {str(item.get("unit_id")): item for item in summary.get("outcomes", [])}
    if len(by_id) != len(rows) or int(summary.get("completed", -1)) != len(rows):
        raise RuntimeError("saved game-group coverage differs from the frozen seed rows")
    for row in rows:
        unit_id = str(row["unit_id"])
        saved = by_id.get(unit_id)
        result = backend.existing_success_record(root, row, package)
        if saved is None or result is None:
            raise RuntimeError(f"saved game-group receipt is missing: {unit_id}")
        report, path = result
        if (
            saved.get("result_sha256") != d.file_sha256(path)
            or saved.get("policy_model_digest") != report.get("model_digest")
            or saved.get("source_lock_sha256") != report.get("source_lock_sha256")
        ):
            raise RuntimeError(f"saved game-group receipt changed after handoff boundary: {unit_id}")
    return dict(record)


def _write_report_once(path: Path, record: Mapping[str, Any], backend) -> dict[str, Any]:
    if not path.is_file():
        backend.write_json_once(path, dict(record))
        return dict(record)
    existing = d.read_json(path)
    stable = {key: value for key, value in record.items() if key not in {"finished_utc", "recorded_utc"}}
    old_stable = {key: value for key, value in existing.items() if key not in {"finished_utc", "recorded_utc"}}
    if stable != old_stable:
        raise FileExistsError(f"immutable Issue 84 report differs from existing evidence: {path}")
    return existing


def _save_progress(path: Path, progress: dict[str, Any], backend) -> None:
    progress["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    backend.atomic_json(path, progress)


def _resource_audit(root: Path, *, active_wall_path: Path, allowance: float, cumulative_wall: float) -> dict[str, Any]:
    attempts = []
    for directory, _subdirs, _files in os.walk(root):
        attempt = Path(directory)
        if not attempt.name.startswith("attempt-"):
            continue
        started_path = attempt / "started.json"
        finished_path = attempt / "finished.json"
        result_path = attempt / "result.json"
        started = d.read_json(started_path) if started_path.is_file() else {}
        finished = d.read_json(finished_path) if finished_path.is_file() else {}
        result = d.read_json(result_path) if result_path.is_file() else {}
        resource = result.get("resource", {})
        monotonic_wall = None
        if isinstance(started.get("started_monotonic"), (int, float)) and isinstance(finished.get("finished_monotonic"), (int, float)):
            monotonic_wall = max(0.0, float(finished["finished_monotonic"]) - float(started["started_monotonic"]))
        cpu = resource.get("worker_process_cpu_seconds")
        worker_wall = resource.get("worker_wall_seconds")
        attempts.append({
            "unit_id": finished.get("unit_id", started.get("unit_id")),
            "run_id": finished.get("run_id", started.get("run_id")),
            "status": finished.get("status", "INCOMPLETE_OR_UNKNOWN"),
            "start_receipt_present": started_path.is_file(),
            "result_receipt_present": result_path.is_file(),
            "worker_process_cpu_seconds": cpu,
            "worker_wall_seconds": worker_wall,
            "attempt_monotonic_wall_seconds": monotonic_wall,
            "result_sha256": d.file_sha256(result_path) if result_path.is_file() else None,
        })
    attempts.sort(key=lambda row: (
        str(row["unit_id"]), str(row["run_id"]), str(row["status"]), str(row["result_sha256"]),
    ))
    successes = [row for row in attempts if row["status"] == "SUCCEEDED"]
    non_successes = [row for row in attempts if row["status"] != "SUCCEEDED"]
    active_clock = d.read_json(active_wall_path) if active_wall_path.is_file() else None
    return {
        "schema_id": "issue84-resource-wall-audit-v1",
        "attempt_count": len(attempts),
        "success_attempt_count": len(successes),
        "non_success_attempt_count": len(non_successes),
        "missing_start_receipt_count": sum(not row["start_receipt_present"] for row in attempts),
        "missing_result_receipt_count": sum(not row["result_receipt_present"] for row in attempts),
        "cpu_seconds_known_attempt_count": sum(isinstance(row["worker_process_cpu_seconds"], (int, float)) for row in attempts),
        "cpu_seconds_unknown_attempt_count": sum(not isinstance(row["worker_process_cpu_seconds"], (int, float)) for row in attempts),
        "worker_wall_seconds_known_attempt_count": sum(isinstance(row["worker_wall_seconds"], (int, float)) for row in attempts),
        "worker_wall_seconds_unknown_attempt_count": sum(not isinstance(row["worker_wall_seconds"], (int, float)) for row in attempts),
        "attempt_interval_wall_seconds_known_attempt_count": sum(isinstance(row["attempt_monotonic_wall_seconds"], (int, float)) for row in attempts),
        "attempt_interval_wall_seconds_unknown_attempt_count": sum(not isinstance(row["attempt_monotonic_wall_seconds"], (int, float)) for row in attempts),
        "successful_worker_process_cpu_seconds_total": sum(float(row["worker_process_cpu_seconds"]) for row in successes if isinstance(row["worker_process_cpu_seconds"], (int, float))),
        "successful_worker_wall_seconds_total": sum(float(row["worker_wall_seconds"]) for row in successes if isinstance(row["worker_wall_seconds"], (int, float))),
        "failed_and_incomplete_attempts": non_successes,
        "task_active_wall": {
            "path": str(active_wall_path),
            "status": ("MISSING" if active_clock is None else "ACTIVE" if active_clock.get("active_session") else "CLOSED"),
            "cumulative_seconds": active_clock.get("elapsed_seconds") if active_clock else cumulative_wall,
            "allowance_seconds": active_clock.get("allowance_seconds") if active_clock else allowance,
            "known": active_clock is not None and active_clock.get("active_session") is None,
            "counting_rule": "one study coordinator/process interval plus concurrent workers counted once; idle gaps excluded",
        },
        "attempts": attempts,
        "limitations": [
            "missing per-attempt CPU or monotonic telemetry remains unknown and is not imputed as zero",
            "the task active-wall record is the authoritative whole-program wall measure",
        ],
    }


def _data_footprint(root: Path, *, source_lock_sha256: str, package: str) -> dict[str, Any]:
    by_top_level: dict[str, dict[str, int]] = {}
    total_files = total_bytes = 0
    for directory, _subdirs, filenames in os.walk(root):
        base = Path(directory)
        for filename in filenames:
            path = base / filename
            try:
                size = path.stat().st_size
            except OSError:
                continue
            relative = path.relative_to(root)
            scope = relative.parts[0] if relative.parts else "."
            summary = by_top_level.setdefault(scope, {"files": 0, "bytes": 0})
            summary["files"] += 1
            summary["bytes"] += size
            total_files += 1
            total_bytes += size
    return {
        "schema_id": "issue84-data-footprint-v1",
        "status": "COMPLETE",
        "package_commit": package,
        "source_lock_sha256": source_lock_sha256,
        "artifact_root": str(root),
        "total_files_before_this_report": total_files,
        "total_bytes_before_this_report": total_bytes,
        "by_top_level_directory": by_top_level,
        "includes": ["pilot receipts", "formal per-attempt raw results and search artifacts", "actor and predictor checkpoints", "validation/test receipts", "logs and failure evidence"],
        "snapshot_rule": "inventory captured after test analysis; this footprint and final closeout files are excluded",
    }


def _paired_outcomes(groups: Mapping[str, Mapping[str, Any]], group_names: Sequence[str], *, expected: int) -> dict[str, dict[str, Any]]:
    result = {}
    for group in group_names:
        outcomes = groups[group]["run_summary"]["outcomes"]
        by_seed = {}
        for row in outcomes:
            key = str(row.get("paired_owner_key"))
            if key in by_seed or not key or row.get("terminal_floor") is None:
                raise RuntimeError(f"group {group} has duplicate/missing paired terminal data")
            by_seed[key] = row
        if len(by_seed) != expected:
            raise RuntimeError(f"group {group} expected {expected} unique paired terminal owners")
        result[group] = by_seed
    keys = set(result[group_names[0]])
    if any(set(result[group]) != keys for group in group_names[1:]):
        raise RuntimeError("evaluation arms do not share exactly the registered paired owner keys")
    return result


def _aggregate_groups(groups: Mapping[str, Mapping[str, Any]], names: Sequence[str], *, expected: int) -> dict[str, Any]:
    paired = _paired_outcomes(groups, names, expected=expected)
    output = {}
    for group in names:
        values = list(paired[group].values())
        output[group] = {
            "owner_count": len(values),
            "mean_terminal_floor": sum(float(row["terminal_floor"]) for row in values) / len(values),
            "act2_plus_rate": sum(bool(row["act2_plus"]) for row in values) / len(values),
            "act3_plus_rate": sum(bool(row["act3_plus"]) for row in values) / len(values),
            "act4_reached_rate": sum(bool(row["act4_reached"]) for row in values) / len(values),
            "heart_win_rate": sum(bool(row["heart_win"]) for row in values) / len(values),
            "outcomes": sorted(values, key=lambda row: (int(row["owner_seed"]), str(row["unit_id"]))),
        }
    return output


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("cannot calculate a bootstrap interval without replicates")
    position = (len(ordered) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    fraction = position - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def paired_bootstrap(
    left_rows: Mapping[str, Mapping[str, Any]], right_rows: Mapping[str, Mapping[str, Any]],
    *, label: str, seed: int, replicates: int = BOOTSTRAP_REPLICATES,
) -> dict[str, Any]:
    keys = sorted(set(left_rows) & set(right_rows))
    if len(keys) != len(left_rows) or len(keys) != len(right_rows) or not keys:
        raise ValueError("paired bootstrap inputs must have the same nonempty owner keys")
    differences = []
    for key in keys:
        left, right = left_rows[key], right_rows[key]
        if left.get("owner_seed") != right.get("owner_seed"):
            raise ValueError(f"paired owner seed mismatch at {key}")
        differences.append(float(left["terminal_floor"]) - float(right["terminal_floor"]))
    mean = sum(differences) / len(differences)
    rng = random.Random(int(seed))
    estimates = [
        sum(differences[rng.randrange(len(differences))] for _ in differences) / len(differences)
        for _ in range(replicates)
    ]
    return {
        "contrast": label,
        "left_minus_right": True,
        "paired_owner_count": len(keys),
        "mean_difference_terminal_floor": mean,
        "bootstrap_replicates": replicates,
        "bootstrap_rng_seed": int(seed),
        "bootstrap_method": "owner-paired percentile; sorted sample quantile with linear interpolation",
        "ci95": [_quantile(estimates, 0.025), _quantile(estimates, 0.975)],
        "per_owner_differences": [
            {"paired_owner_key": key, "owner_seed": int(left_rows[key]["owner_seed"]), "difference": diff}
            for key, diff in zip(keys, differences)
        ],
    }


def analyze_test(test: Mapping[str, Any], *, package: str, source_lock_sha256: str) -> dict[str, Any]:
    groups = test.get("groups", {})
    names = ("PARENT", *d.ARMS)
    if set(groups) != set(names):
        raise RuntimeError("final test does not contain exactly PARENT and A-F")
    paired = _paired_outcomes(groups, names, expected=320)
    contrasts = {}
    for left, right in (("E", "D"), ("B", "A"), ("C", "B"), ("D", "C"), ("F", "D"), ("E", "A"), ("F", "A"), *((arm, "PARENT") for arm in d.ARMS)):
        label = f"{left}-{right}"
        contrasts[label] = paired_bootstrap(
            paired[left], paired[right], label=label,
            seed=d.derive_seed("test-owner-bootstrap", label, 20_000),
        )
    aggregates = _aggregate_groups(groups, names, expected=320)
    per_owner = []
    for key in sorted(paired["PARENT"]):
        rows = {group: paired[group][key] for group in names}
        per_owner.append({
            "paired_owner_key": key,
            "owner_seed": int(rows["PARENT"]["owner_seed"]),
            "terminal_floor_by_group": {group: int(rows[group]["terminal_floor"]) for group in names},
            "act2_plus_by_group": {group: rows[group]["act2_plus"] for group in names},
            "act3_plus_by_group": {group: rows[group]["act3_plus"] for group in names},
            "act4_reached_by_group": {group: rows[group]["act4_reached"] for group in names},
            "heart_win_by_group": {group: rows[group]["heart_win"] for group in names},
            "model_digest_by_group": {group: rows[group]["policy_model_digest"] for group in names},
            "policy_rng_seed_by_group": {group: rows[group]["policy_rng_seed"] for group in names},
            "result_sha256_by_group": {group: rows[group]["result_sha256"] for group in names},
        })
    return {
        "schema_id": "issue84-test-analysis-v1",
        "status": "COMPLETE",
        "package_commit": package,
        "source_lock_sha256": source_lock_sha256,
        "owner_count": len(per_owner),
        "primary": contrasts["E-D"],
        "prespecified_secondary_unadjusted": {
            key: value for key, value in contrasts.items() if key != "E-D"
        },
        "per_group_descriptive": aggregates,
        "paired_owner_outcomes": per_owner,
        "interpretation": (
            "only a positive lower endpoint for E-D establishes the registered conditional incremental "
            "full-game benefit for this one learner lineage; a CI spanning zero does not establish equivalence"
        ),
        "limitations": [
            "one trained lineage per arm does not establish independent-training stability or scaling",
            "secondary intervals are unadjusted for multiple comparisons",
            "native-surrogate outcomes do not establish live-client efficacy",
        ],
        "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def run_formal() -> dict[str, Any]:
    source = d.verify_source_lock(require_clean_worktree=True)
    backend = d.configure_backend()
    osenv = __import__("os").environ
    osenv["STS_LIGHTSPEED_SOURCE_DIR"] = d.STS_SOURCE_WSL
    osenv["STSRL_SOURCE_DIR"] = d.STSRL_SOURCE_WSL
    package = d.package_commit()
    pilot_root = backend.ARTIFACTS / "pilot" / package
    forecast_path = pilot_root / "task-wall-forecast.json"
    if not forecast_path.is_file():
        raise FileNotFoundError("the source-matched representative pilot forecast must precede formal training")
    forecast = d.read_json(forecast_path)
    if forecast.get("source_lock_sha256") != source["sha256"] or forecast.get("package_commit") != package:
        raise RuntimeError("formal wall forecast belongs to a different source lock/commit")
    allowance = float(forecast["one_task_active_wall_allowance_seconds"])
    if not math.isfinite(allowance) or allowance <= 0:
        raise RuntimeError("formal task-wide wall allowance is invalid")
    seed_table = d.ensure_seed_table()
    formal_root = backend.ARTIFACTS / "formal"
    formal_root.mkdir(parents=True, exist_ok=True)
    progress_path = formal_root / "formal-progress.json"
    progress = d.read_json(progress_path) if progress_path.is_file() else {
        "schema_id": "issue84-formal-progress-v1",
        "status": "IN_PROGRESS",
        "package_commit": package,
        "source_lock_sha256": source["sha256"],
        "seed_table_sha256": d.file_sha256(backend.SEED_TABLE),
        "forecast_sha256": d.file_sha256(forecast_path),
        "training_blocks": {arm: {} for arm in d.ARMS},
        "validation_groups": {},
        "test_groups": {},
    }
    expected_binding = {
        "schema_id": "issue84-formal-progress-v1",
        "package_commit": package,
        "source_lock_sha256": source["sha256"],
        "seed_table_sha256": d.file_sha256(backend.SEED_TABLE),
        "forecast_sha256": d.file_sha256(forecast_path),
    }
    if any(progress.get(key) != value for key, value in expected_binding.items()):
        raise RuntimeError("formal progress state belongs to another source-locked run")
    final_report_path = formal_root / "formal-run-report.json"
    if final_report_path.is_file():
        final_report = d.read_json(final_report_path)
        if final_report.get("source_lock_sha256") != source["sha256"] or final_report.get("package_commit") != package:
            raise RuntimeError("existing formal closeout report belongs to another source lock")
        if progress.get("status") != "COMPLETE" or progress.get("formal_report_sha256") != d.file_sha256(final_report_path):
            progress["status"] = "COMPLETE"
            progress["formal_report_sha256"] = d.file_sha256(final_report_path)
            _save_progress(progress_path, progress, backend)
        return final_report
    clock = backend.ActiveClock("issue84-task", allowance)
    if clock.elapsed() > allowance:
        clock.close()
        return {"status": "TASK_WALL_ALLOWANCE_ALREADY_EXCEEDED", "elapsed_seconds": clock.elapsed(), "allowance_seconds": allowance}
    clock_closed = False
    try:
        for arm in d.ARMS:
            root = formal_root / arm
            for block in range(FORMAL_BLOCKS):
                if clock.expired():
                    progress["status"] = "STOPPED_TASK_WALL_ALLOWANCE"
                    progress["stop_boundary"] = {"stage": "training", "arm": arm, "block": block}
                    progress["cumulative_active_wall_seconds"] = clock.elapsed()
                    _save_progress(progress_path, progress, backend)
                    return {"status": progress["status"], "stop_boundary": progress["stop_boundary"], "allowance_seconds": allowance}
                rows = _block_rows(seed_table, arm, block)
                if len(rows) != FORMAL_OWNERS_PER_BLOCK or any(row.get("block_index") != block for row in rows):
                    raise RuntimeError(f"formal seed table arm {arm} block {block} is incomplete/misindexed")
                progress["active_stage"] = "training-games"
                progress["active_arm"] = arm
                progress["active_block"] = block
                _save_progress(progress_path, progress, backend)
                saved_group = progress["training_blocks"].setdefault(arm, {}).get(str(block))
                if saved_group is not None:
                    game_summary = _verify_saved_game_group(
                        saved_group, rows, root=root, package=package, backend=backend,
                    )
                else:
                    game_summary = _record_game_group(
                        f"TRAIN-{arm}-BLOCK-{block:02d}", rows, root=root, pilot=False,
                        package=package, clock=clock, backend=backend,
                    )
                if game_summary["status"] != "SUCCEEDED":
                    progress["status"] = game_summary["status"]
                    progress["stop_boundary"] = {"stage": "training-games", "arm": arm, "block": block}
                    progress["incomplete_game_group"] = game_summary
                    _save_progress(progress_path, progress, backend)
                    return game_summary
                progress["training_blocks"].setdefault(arm, {})[str(block)] = game_summary
                _save_progress(progress_path, progress, backend)
                progress["active_stage"] = "actor-update"
                _save_progress(progress_path, progress, backend)
                update = _train_actor_block(
                    arm, block, rows, root=root, package=package,
                    clock=clock, progress=progress, backend=backend,
                )
                progress["training_blocks"][arm][str(block)]["actor_update"] = update
                progress["cumulative_active_wall_seconds"] = clock.elapsed()
                _save_progress(progress_path, progress, backend)

        validation_groups = progress["validation_groups"]
        validation_root = backend.ARTIFACTS / "validation"
        for group in ("PARENT", *(f"{arm}{games}" for arm in d.ARMS for games in (512, 1024, 2048))):
            if clock.expired():
                progress["status"] = "STOPPED_TASK_WALL_ALLOWANCE"
                progress["stop_boundary"] = {"stage": "validation", "group": group}
                _save_progress(progress_path, progress, backend)
                return {"status": progress["status"], "stop_boundary": progress["stop_boundary"]}
            rows = [row for row in d.seed_table_rows(
                seed_table, phase="evaluation", formal=True,
                owners=range(8_460_001, 8_460_065),
            ) if row["arm"] == group]
            if len(rows) != 64:
                raise RuntimeError(f"validation group {group} expected 64 paired owners")
            arm_root = formal_root / group if group == "PARENT" else formal_root / group[0]
            group_root = arm_root
            if group in validation_groups:
                group_result = _verify_saved_game_group(
                    validation_groups[group], rows, root=group_root, package=package, backend=backend,
                )
            else:
                group_result = _record_game_group(
                    group, rows, root=group_root, pilot=False, package=package,
                    clock=clock, backend=backend,
                )
            if group_result["status"] != "SUCCEEDED":
                progress["status"] = group_result["status"]
                progress["stop_boundary"] = {"stage": "validation", "group": group}
                _save_progress(progress_path, progress, backend)
                return group_result
            validation_groups[group] = group_result
            _save_progress(progress_path, progress, backend)
        validation_report_path = validation_root / "validation-report.json"
        validation_report = {
            "schema_id": "issue84-validation-report-v1",
            "status": "COMPLETE_DIAGNOSTIC_ONLY",
            "checkpoint_selection_or_early_stop": False,
            "source_lock_sha256": source["sha256"],
            "package_commit": package,
            "groups": validation_groups,
            "descriptive": _aggregate_groups(
                validation_groups,
                ("PARENT", *(f"{arm}{games}" for arm in d.ARMS for games in (512, 1024, 2048))),
                expected=64,
            ),
            "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        validation_report = _write_report_once(validation_report_path, validation_report, backend)
        progress["validation_report_sha256"] = d.file_sha256(validation_report_path)
        _save_progress(progress_path, progress, backend)

        test_groups = progress["test_groups"]
        for group in ("PARENT", *d.ARMS):
            if clock.expired():
                progress["status"] = "STOPPED_TASK_WALL_ALLOWANCE"
                progress["stop_boundary"] = {"stage": "test", "group": group}
                _save_progress(progress_path, progress, backend)
                return {"status": progress["status"], "stop_boundary": progress["stop_boundary"]}
            rows = [row for row in d.seed_table_rows(
                seed_table, phase="evaluation", formal=True,
                owners=range(8_470_001, 8_470_321),
            ) if row["arm"] == group]
            if len(rows) != 320:
                raise RuntimeError(f"final test group {group} expected 320 paired owners")
            group_root = formal_root / "PARENT" if group == "PARENT" else formal_root / group
            if group in test_groups:
                group_result = _verify_saved_game_group(
                    test_groups[group], rows, root=group_root, package=package, backend=backend,
                )
            else:
                group_result = _record_game_group(
                    group, rows, root=group_root, pilot=False, package=package,
                    clock=clock, backend=backend,
                )
            if group_result["status"] != "SUCCEEDED":
                progress["status"] = group_result["status"]
                progress["stop_boundary"] = {"stage": "test", "group": group}
                _save_progress(progress_path, progress, backend)
                return group_result
            test_groups[group] = group_result
            _save_progress(progress_path, progress, backend)
        test_report = {
            "schema_id": "issue84-test-report-v1",
            "status": "COMPLETE",
            "source_lock_sha256": source["sha256"],
            "package_commit": package,
            "owner_range": [8_470_001, 8_470_320],
            "independent_of_training_validation_pilot": True,
            "groups": test_groups,
            "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        test_path = backend.ARTIFACTS / "test" / "test-report.json"
        test_report = _write_report_once(test_path, test_report, backend)
        analysis = analyze_test(test_report, package=package, source_lock_sha256=source["sha256"])
        analysis_path = backend.ARTIFACTS / "test" / "analysis.json"
        analysis = _write_report_once(analysis_path, analysis, backend)
        progress["status"] = "COMPLETE_PENDING_CLOSEOUT"
        progress["test_report_sha256"] = d.file_sha256(test_path)
        progress["analysis_sha256"] = d.file_sha256(analysis_path)
        progress["cumulative_active_wall_seconds"] = clock.elapsed()
        progress["stop_boundary"] = None
        _save_progress(progress_path, progress, backend)
        clock.set_processes(set())
        measured_active_wall = clock.close()
        clock_closed = True
        post_clock_started = time.monotonic()
        active_wall_path = backend.ARTIFACTS / "active-wall-issue84-task.json"
        resource_audit = _resource_audit(
            backend.ARTIFACTS, active_wall_path=active_wall_path,
            allowance=allowance, cumulative_wall=measured_active_wall,
        )
        resource_bytes = d.canonical(resource_audit) + b"\n"
        resource_sha = d.sha256(resource_bytes)
        resource_path = formal_root / f"resource-wall-audit-{resource_sha[:16]}.json"
        backend.write_json_once(resource_path, resource_audit)
        footprint = _data_footprint(
            backend.ARTIFACTS, source_lock_sha256=source["sha256"], package=package,
        )
        footprint_bytes = d.canonical(footprint) + b"\n"
        footprint_sha = d.sha256(footprint_bytes)
        footprint_path = formal_root / f"data-footprint-{footprint_sha[:16]}.json"
        backend.write_json_once(footprint_path, footprint)
        result = {
            "status": "COMPLETE",
            "schema_id": "issue84-formal-run-report-v1",
            "source_lock_sha256": source["sha256"],
            "package_commit": package,
            "pilot_forecast_sha256": d.file_sha256(forecast_path),
            "validation_report_sha256": d.file_sha256(validation_report_path),
            "test_report_sha256": d.file_sha256(test_path),
            "analysis_sha256": d.file_sha256(analysis_path),
            "resource_wall_audit_path": resource_path.relative_to(backend.ARTIFACTS).as_posix(),
            "resource_wall_audit_sha256": d.file_sha256(resource_path),
            "data_footprint_path": footprint_path.relative_to(backend.ARTIFACTS).as_posix(),
            "data_footprint_sha256": d.file_sha256(footprint_path),
            "cumulative_active_wall_seconds": measured_active_wall,
            "post_clock_report_closeout_wall_seconds_before_final_report": None,
            "post_clock_final_write_and_return_tail_is_unmeasured": True,
            "one_task_active_wall_allowance_seconds": allowance,
            "primary": analysis["primary"],
        }
        result["post_clock_report_closeout_wall_seconds_before_final_report"] = time.monotonic() - post_clock_started
        backend.write_json_once(final_report_path, result)
        progress["status"] = "COMPLETE"
        progress["formal_report_sha256"] = d.file_sha256(final_report_path)
        _save_progress(progress_path, progress, backend)
        return result
    finally:
        if not clock_closed:
            clock.close()
