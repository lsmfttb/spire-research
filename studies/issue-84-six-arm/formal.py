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


def _optimizer_parameter_steps_match(
    value: Any, *, expected_steps: int, parameter_count: int,
) -> bool:
    """Validate the JSON list emitted by the locked optimizer-step recorder."""
    return (
        isinstance(value, list)
        and len(value) == parameter_count
        and all(type(step) is int and step == expected_steps for step in value)
    )


def _new_predecessor_actor(model_module, policy: Mapping[str, Any]):
    """Build the carry-forward validator with the dimension from its locked model."""
    return model_module.ActionConditionedPolicy(
        dimension=model_module.DEFAULT_HASH_DIMENSION,
        seed=int(policy["initial_model_seed"]),
        final_weight_std=float(policy["actor_final_weight_std"]),
    ).eval()


def _issue75_policy() -> Mapping[str, Any]:
    """Read the model policy from the vendored, source-locked Issue 75 config."""
    policy = d.read_json(d.UPSTREAM75 / "config.json").get("policy")
    if not isinstance(policy, Mapping):
        raise RuntimeError("locked Issue 75 config lacks its model policy mapping")
    return policy


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
    arm = arm.upper()
    if arm not in {"A", "B", "C", "D", "E", "F"}:
        raise ValueError(f"unknown Issue 84 arm: {arm}")
    if arm in {"A", "B", "C"}:
        return None, None, None, None
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
        "value_model_state_dict": None if value is None else value.state_dict(),
        "value_optimizer_state_dict": None if value_optimizer is None else value_optimizer.state_dict(),
        "action_value_model_state_dict": None if action_value is None else action_value.state_dict(),
        "action_value_optimizer_state_dict": None if action_optimizer is None else action_optimizer.state_dict(),
    }


def _prepare_predictors(
    arm: str, block: int, *, root: Path, executor, package: str,
    value, value_optimizer, action_value, action_optimizer,
) -> tuple[dict[str, Any], str | None]:
    arm = arm.upper()
    if arm in {"A", "B", "C"}:
        if any(item is not None for item in (value, value_optimizer, action_value, action_optimizer)):
            raise RuntimeError(f"arm {arm} must not create auxiliary predictors")
        return {
            "schema_id": "issue84-no-auxiliary-predictor-v1",
            "status": "NOT_APPLICABLE",
            "arm": arm,
            "current_block_index": block,
            "prior_block_indices": list(range(block)),
            "source_lock_sha256": d.file_sha256(d.SOURCE_LOCK_PATH),
            "package_commit": package,
        }, None

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


def _train_actor_block(
    arm: str, block: int, rows, *, root: Path, package: str, clock, progress, backend,
    training_root: Path | None = None, training_package: str | None = None,
    training_origin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    algorithm, runtime, model, learning, executor = d.load_modules(root, pilot=False)
    training_root = root if training_root is None else training_root
    training_package = package if training_package is None else training_package
    training_origin = None if training_origin is None else dict(training_origin)
    if training_package != package:
        if (
            training_origin is None
            or training_origin.get("schema_id") != "issue84-formal-training-continuation-v1"
            or training_origin.get("predecessor_package_commit") != training_package
            or training_origin.get("predecessor_source_lock_sha256")
            != d.FORMAL_PREDECESSOR_LOCK_SHA256
            or not isinstance(training_origin.get("bridge_sha256"), str)
            or not isinstance(training_origin.get("bridge_manifest_sha256"), str)
        ):
            raise RuntimeError("formal actor update lacks a valid predecessor evidence bridge")
        artifact_root = backend.ARTIFACTS.resolve()
        expected_root = (artifact_root / str(training_origin.get("predecessor_root", ""))).resolve()
        bridge_path = (artifact_root / str(training_origin.get("bridge_path", ""))).resolve()
        try:
            expected_root.relative_to(artifact_root)
            bridge_path.relative_to(artifact_root)
        except ValueError as exc:
            raise RuntimeError("formal actor update predecessor provenance escapes its artifact root") from exc
        if (
            Path(training_root).resolve() != expected_root.resolve()
            or not expected_root.is_dir()
            or not bridge_path.is_file()
            or d.file_sha256(bridge_path) != training_origin.get("bridge_sha256")
        ):
            raise RuntimeError("formal actor update predecessor evidence path/hash differs from its bridge")
        bridge = d.read_json(bridge_path)
        bridge_manifest = {key: value for key, value in bridge.items() if key != "manifest_sha256"}
        if (
            bridge.get("schema_id") != "issue84-formal-continuation-bridge-v1"
            or bridge.get("manifest_sha256") != d.sha256(d.canonical(bridge_manifest))
            or bridge.get("manifest_sha256") != training_origin.get("bridge_manifest_sha256")
            or not isinstance(bridge.get("current"), Mapping)
            or not isinstance(bridge.get("predecessor"), Mapping)
            or not isinstance(bridge.get("units"), list)
            or bridge.get("current", {}).get("package_commit") != package
            or bridge.get("current", {}).get("source_lock_sha256") != d.file_sha256(d.SOURCE_LOCK_PATH)
            or bridge.get("predecessor", {}).get("package_commit") != training_package
            or bridge.get("predecessor", {}).get("source_lock_sha256")
            != training_origin.get("predecessor_source_lock_sha256")
            or bridge.get("predecessor", {}).get("formal_arm_a_root")
            != training_origin.get("predecessor_root")
            or training_origin.get("receipts_sha256") != d.sha256(d.canonical(bridge.get("units")))
        ):
            raise RuntimeError("formal actor update bridge manifest hash differs from progress lineage")
    elif training_origin is not None:
        raise RuntimeError("current-package actor update unexpectedly received predecessor provenance")

    if training_package == package:
        examples, outcomes = d.load_training_outcomes(
            rows, root=training_root, executor=executor, package=training_package,
        )
        feature_sha = d.owner_feature_manifest(
            rows, root=training_root, package=training_package,
        )
    else:
        with d._predecessor_source_lock(training_package):
            examples, outcomes = d.load_training_outcomes(
                rows, root=training_root, executor=executor, package=training_package,
            )
            feature_sha = d.owner_feature_manifest(
                rows, root=training_root, package=training_package,
            )
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
            "training_evidence_origin": training_origin,
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
            "training_evidence_origin": training_origin,
            "actor_state_dict": trained.state_dict(),
            "optimizer_state_dict": current_optimizer.state_dict(),
            "python_rng_state": rng_state,
            "owner_order": [int(index) for index in order],
            "diagnostics": list(diagnostics),
            **_predictor_state(value, value_optimizer, action_value, action_optimizer),
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
        "training_evidence_origin": training_origin,
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



def _validation_summary_row_matches(saved: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    fields = (
        "unit_id", "owner_seed", "phase", "arm", "issue84_arm",
        "issue84_policy_label", "paired_owner_key", "game_seed",
        "policy_rng_seed", "search_invocation_seeds",
    )
    return all(saved.get(key) == row.get(key) for key in fields)


def _validation_success_reference(
    report: Mapping[str, Any], result_path: Path, row: Mapping[str, Any], *,
    group: str, package: str, source_lock_sha256: str, expected_model_digest: str,
    expected_policy_source: Mapping[str, Any], root: Path, backend,
) -> dict[str, Any]:
    """Verify immutable result, worker-job, start, finish, source, and seed bindings."""
    result_path = Path(result_path)
    result_sha256 = d.file_sha256(result_path)
    unit_root = Path(root) / "units" / backend.unit_key(str(row["unit_id"]))
    successes = []
    for candidate in sorted(unit_root.glob("attempt-*/result.json")):
        candidate_report = d.read_json(candidate)
        if (
            candidate_report.get("status") == "SUCCEEDED"
            and candidate_report.get("package_commit") == package
            and candidate_report.get("source_lock_sha256") == source_lock_sha256
        ):
            successes.append(candidate)
    if len(successes) != 1 or successes[0] != result_path:
        raise RuntimeError(f"validation unit does not have exactly one matching successful attempt: {row['unit_id']}")

    attempt = result_path.parent
    job_path = attempt / "worker-job.json"
    started_path = attempt / "started.json"
    finished_path = attempt / "finished.json"
    if not all(path.is_file() for path in (job_path, started_path, finished_path)):
        raise RuntimeError(f"validation unit lacks its immutable job/start/finish receipts: {row['unit_id']}")
    job = d.read_json(job_path)
    started = d.read_json(started_path)
    finished = d.read_json(finished_path)
    if (
        report.get("schema_id") != "issue75-stage1-unit-result-v1"
        or report.get("status") != "SUCCEEDED"
        or report.get("unit_id") != row.get("unit_id")
        or report.get("owner_seed") != row.get("owner_seed")
        or report.get("phase") != "evaluation"
        or report.get("arm") != group
        or report.get("formal") is not True
        or report.get("package_commit") != package
        or report.get("runner_code_commit") != package
        or report.get("source_lock_sha256") != source_lock_sha256
        or report.get("game_seed") != row.get("game_seed")
        or report.get("paired_owner_key") != row.get("paired_owner_key")
        or report.get("policy_source") != dict(expected_policy_source)
        or report.get("model_digest") != expected_model_digest
        or report.get("native_source_commit") != d.NATIVE_COMMIT
        or report.get("stsr_source_commit") != d.STSRL_COMMIT
        or report.get("native_module_sha256") != d.NATIVE_SHA256
        or report.get("expert_decisions") != 0
        or report.get("terminal_floor") is None
        or report.get("terminal_outcome") is None
        or job.get("schema_id") != "issue79-owner-worker-job-v1"
        or job.get("artifact_root") != str(Path(root))
        or job.get("attempt_dir") != str(attempt)
        or job.get("unit_id") != row.get("unit_id")
        or job.get("row") != dict(row)
        or job.get("package_commit") != package
        or job.get("runner_code_commit") != package
        or job.get("source_lock_sha256") != source_lock_sha256
        or started.get("schema_id") != "issue79-owner-attempt-start-v1"
        or started.get("unit_id") != row.get("unit_id")
        or started.get("owner_seed") != row.get("owner_seed")
        or started.get("arm") != group
        or started.get("package_commit") != package
        or started.get("run_id") != job.get("run_id")
        or started.get("seed_row_sha256") != _hash(dict(row))
        or finished.get("schema_id") != "issue79-owner-attempt-finish-v1"
        or finished.get("unit_id") != row.get("unit_id")
        or finished.get("run_id") != started.get("run_id")
        or finished.get("status") != "SUCCEEDED"
        or finished.get("result_sha256") != result_sha256
    ):
        raise RuntimeError(f"validation result provenance differs from its registered seed row: {row['unit_id']}")
    return {
        "unit_id": str(row["unit_id"]),
        "result_path": result_path.relative_to(Path(root)).as_posix(),
        "result_sha256": result_sha256,
        "worker_job_sha256": d.file_sha256(job_path),
        "started_sha256": d.file_sha256(started_path),
        "finished_sha256": d.file_sha256(finished_path),
        "package_commit": package,
        "source_lock_sha256": source_lock_sha256,
    }


def _validation_attempt_inventory(root: Path, rows, *, backend) -> list[dict[str, Any]]:
    inventory = []
    for row in rows:
        unit_root = Path(root) / "units" / backend.unit_key(str(row["unit_id"]))
        attempts = []
        successes = 0
        for attempt in sorted(unit_root.glob("attempt-*")):
            item = {"attempt": attempt.name}
            for name in ("worker-job.json", "started.json", "finished.json", "result.json"):
                path = attempt / name
                if path.is_file():
                    item[f"{name.removesuffix('.json')}_sha256"] = d.file_sha256(path)
                    if name == "result.json":
                        report = d.read_json(path)
                        item["result_status"] = report.get("status")
                        if report.get("status") == "SUCCEEDED":
                            successes += 1
            attempts.append(item)
        if successes > 1:
            raise RuntimeError(f"validation owner has multiple successful attempts: {row['unit_id']}")
        if successes:
            status = "SUCCEEDED"
        elif any(item.get("result_status") == "FAILED" for item in attempts):
            status = "FAILED"
        elif attempts:
            status = "NO_RESULT"
        else:
            status = "NOT_STARTED"
        inventory.append({
            "unit_id": str(row["unit_id"]),
            "owner_seed": int(row["owner_seed"]),
            "status": status,
            "attempt_count": len(attempts),
            "attempts": attempts,
        })
    return inventory


def _formal_validation_carry_forward(
    *, package: str, source_lock_sha256: str, current_lock: Mapping[str, Any],
    current_seed_table: Mapping[str, Any], formal_root: Path, backend,
) -> dict[str, Any] | None:
    """Reference verified predecessor validation outcomes; never copy or replay them."""
    predecessor = d.FORMAL_VALIDATION_PREDECESSOR_PACKAGE
    predecessor_lock_sha = d.FORMAL_VALIDATION_PREDECESSOR_LOCK_SHA256
    predecessor_root = backend.ARTIFACTS / "formal" / predecessor
    progress_path = predecessor_root / "formal-progress.json"
    seed_path = d.seed_table_path(predecessor)
    lock_path = backend.ARTIFACTS / "source-lock-history" / f"{predecessor}-{predecessor_lock_sha}.json"
    evidence_paths = (progress_path, seed_path, lock_path)
    if not any(path.exists() for path in evidence_paths):
        return None
    if not all(path.is_file() for path in evidence_paths):
        raise RuntimeError("formal validation predecessor evidence is incomplete; refusing to repeat completed games")

    old_progress = d.read_json(progress_path)
    old_seed_table = d.read_json(seed_path)
    if (
        package == predecessor
        or old_progress.get("schema_id") != "issue84-formal-progress-v2"
        or old_progress.get("status") != "IN_PROGRESS"
        or old_progress.get("package_commit") != predecessor
        or old_progress.get("source_lock_sha256") != predecessor_lock_sha
        or old_progress.get("seed_table_sha256") != d.file_sha256(seed_path)
        or old_seed_table.get("schema_id") != "issue84-seed-table-v1"
        or old_seed_table.get("source_lock_sha256") != predecessor_lock_sha
        or old_seed_table.get("rows") != current_seed_table.get("rows")
        or old_seed_table.get("rows_sha256") != current_seed_table.get("rows_sha256")
        or old_seed_table.get("rows_sha256") != d.sha256(d.canonical(old_seed_table.get("rows")))
        or set(old_progress.get("validation_groups", {})) != {"PARENT"}
        or old_progress.get("test_groups")
    ):
        raise RuntimeError("formal validation predecessor ledger or seed schedule is incompatible")

    old_training = old_progress.get("training_blocks")
    if not isinstance(old_training, Mapping) or set(old_training) != set(d.ARMS):
        raise RuntimeError("formal validation predecessor lacks the registered six-arm training ledger")
    for arm in d.ARMS:
        blocks = old_training.get(arm)
        if not isinstance(blocks, Mapping) or set(blocks) != {str(i) for i in range(FORMAL_BLOCKS)}:
            raise RuntimeError(f"formal validation predecessor training coverage differs for {arm}")
        if any(
            blocks[str(i)].get("status") != "SUCCEEDED"
            or blocks[str(i)].get("completed") != FORMAL_OWNERS_PER_BLOCK
            for i in range(FORMAL_BLOCKS)
        ):
            raise RuntimeError(f"formal validation predecessor training is incomplete for {arm}")

    old_train_origin = old_progress.get("execution_carry_forward")
    if not isinstance(old_train_origin, Mapping):
        raise RuntimeError("formal validation predecessor lacks its actor lineage manifest")
    train_manifest_path = (backend.ARTIFACTS / str(old_train_origin.get("path", ""))).resolve(strict=True)
    try:
        train_manifest_path.relative_to(backend.ARTIFACTS.resolve())
    except ValueError as exc:
        raise RuntimeError("formal validation actor lineage escaped managed research storage") from exc
    train_manifest = d.read_json(train_manifest_path)
    if (
        d.file_sha256(train_manifest_path) != old_train_origin.get("file_sha256")
        or train_manifest.get("schema_id") != "issue84-formal-training-carry-forward-v1"
        or train_manifest.get("manifest_sha256") != old_train_origin.get("manifest_sha256")
        or train_manifest.get("current", {}).get("package_commit") != predecessor
        or train_manifest.get("current", {}).get("source_lock_sha256") != predecessor_lock_sha
    ):
        raise RuntimeError("formal validation predecessor actor lineage manifest changed")

    old_parent_rows = [row for row in d.seed_table_rows(
        old_seed_table, phase="evaluation", formal=True,
        owners=range(8_460_001, 8_460_065),
    ) if row["arm"] == "PARENT"]
    current_parent_rows = [row for row in d.seed_table_rows(
        current_seed_table, phase="evaluation", formal=True,
        owners=range(8_460_001, 8_460_065),
    ) if row["arm"] == "PARENT"]
    old_a512_rows = [row for row in d.seed_table_rows(
        old_seed_table, phase="evaluation", formal=True,
        owners=range(8_460_001, 8_460_065),
    ) if row["arm"] == "A512"]
    current_a512_rows = [row for row in d.seed_table_rows(
        current_seed_table, phase="evaluation", formal=True,
        owners=range(8_460_001, 8_460_065),
    ) if row["arm"] == "A512"]
    if (
        len(old_parent_rows) != 64 or old_parent_rows != current_parent_rows
        or len(old_a512_rows) != 64 or old_a512_rows != current_a512_rows
    ):
        raise RuntimeError("formal validation predecessor rows differ from the current frozen seed table")

    parent_group = old_progress["validation_groups"]["PARENT"]
    if (
        parent_group.get("status") != "SUCCEEDED"
        or parent_group.get("completed") != 64
        or parent_group.get("expected") != 64
    ):
        raise RuntimeError("formal validation predecessor PARENT group is not complete")

    with d._predecessor_source_lock(predecessor):
        old_lock = d.read_json(backend.SOURCE_LOCK)
        _verify_formal_training_lock_compatibility(old_lock, current_lock)
        verified_parent = _verify_saved_game_group(
            parent_group, old_parent_rows, root=predecessor_root / "PARENT",
            package=predecessor, backend=backend,
        )
        parent_policy = {
            "checkpoint_sha256": d.PARENT_MODEL_SHA256,
            "formal_use": True,
            "source": "issue82-parent-pi12",
        }
        parent_refs = []
        parent_by_id = {
            str(item.get("unit_id")): item
            for item in verified_parent["run_summary"]["outcomes"]
        }
        for row in old_parent_rows:
            saved = parent_by_id.get(str(row["unit_id"]))
            if saved is None or not _validation_summary_row_matches(saved, row):
                raise RuntimeError(f"predecessor PARENT summary changed its frozen row: {row['unit_id']}")
            result_record = backend.existing_success_record(predecessor_root / "PARENT", row, predecessor)
            if result_record is None:
                raise RuntimeError(f"predecessor PARENT success disappeared: {row['unit_id']}")
            report, result_path = result_record
            ref = _validation_success_reference(
                report, result_path, row, group="PARENT", package=predecessor,
                source_lock_sha256=predecessor_lock_sha,
                expected_model_digest=d.PARENT_MODEL_DIGEST,
                expected_policy_source=parent_policy,
                root=predecessor_root / "PARENT", backend=backend,
            )
            if (
                saved.get("result_sha256") != ref["result_sha256"]
                or saved.get("policy_model_digest") != report.get("model_digest")
                or saved.get("source_lock_sha256") != predecessor_lock_sha
            ):
                raise RuntimeError(f"predecessor PARENT summary receipt changed: {row['unit_id']}")
            parent_refs.append(ref)

        inventory = _validation_attempt_inventory(predecessor_root / "A", old_a512_rows, backend=backend)
        successful_reports = {}
        successful_paths = {}
        successful_refs = {}
        for row, item in zip(old_a512_rows, inventory, strict=True):
            if item["status"] != "SUCCEEDED":
                continue
            result_record = backend.existing_success_record(predecessor_root / "A", row, predecessor)
            if result_record is None:
                raise RuntimeError(f"predecessor A512 success cannot be resolved: {row['unit_id']}")
            report, result_path = result_record
            label = str(row.get("issue84_policy_label"))
            arm = str(row.get("issue84_arm"))
            model_entry = train_manifest.get("final_policy_models", {}).get(arm, {}).get(label)
            if not isinstance(model_entry, Mapping):
                raise RuntimeError(f"predecessor A512 model lineage is missing {arm}/{label}")
            model_path = (backend.ARTIFACTS / str(model_entry.get("path", ""))).resolve(strict=True)
            try:
                model_path.relative_to(backend.ARTIFACTS.resolve())
            except ValueError as exc:
                raise RuntimeError("predecessor A512 checkpoint escaped managed research storage") from exc
            if (
                d.file_sha256(model_path) != model_entry.get("checkpoint_sha256")
                or model_entry.get("arm") != arm
                or model_entry.get("label") != label
                or report.get("model_digest") != model_entry.get("model_digest")
            ):
                raise RuntimeError(f"predecessor A512 model identity differs for {row['unit_id']}")
            expected_source = {
                "checkpoint_sha256": model_entry["checkpoint_sha256"],
                "formal_use": True,
                "source": f"issue84-{label}",
                "training_carry_forward": {
                    "bridge_sha256": old_train_origin["file_sha256"],
                    "predecessor_package_commit": model_entry["predecessor_package_commit"],
                    "predecessor_source_lock_sha256": model_entry["predecessor_source_lock_sha256"],
                    "checkpoint_path": model_entry["path"],
                },
            }
            ref = _validation_success_reference(
                report, result_path, row, group="A512", package=predecessor,
                source_lock_sha256=predecessor_lock_sha,
                expected_model_digest=str(model_entry["model_digest"]),
                expected_policy_source=expected_source,
                root=predecessor_root / "A", backend=backend,
            )
            successful_reports[str(row["unit_id"])] = report
            successful_paths[str(row["unit_id"])] = result_path
            successful_refs[str(row["unit_id"])] = ref

        if len(successful_reports) != sum(item["status"] == "SUCCEEDED" for item in inventory):
            raise RuntimeError("predecessor A512 successful-owner inventory is inconsistent")
        a512_result = {
            "status": "SUCCEEDED",
            "completed": len(successful_reports),
            "results": successful_reports,
            "elapsed_seconds": 0.0,
            "native_wall_seconds_by_unit": {},
            "tries": {},
        }
        a512_summary = d.run_summary(
            a512_result, rows=[row for row in old_a512_rows if str(row["unit_id"]) in successful_reports],
            root=predecessor_root / "A", package=predecessor,
        )
        a512_by_id = {str(item["unit_id"]): item for item in a512_summary["outcomes"]}
        for row in old_a512_rows:
            unit_id = str(row["unit_id"])
            if unit_id not in successful_reports:
                continue
            saved = a512_by_id.get(unit_id)
            if saved is None or not _validation_summary_row_matches(saved, row):
                raise RuntimeError(f"predecessor A512 summary changed its frozen row: {unit_id}")
            ref = successful_refs[unit_id]
            if (
                saved.get("result_sha256") != ref["result_sha256"]
                or saved.get("policy_model_digest") != successful_reports[unit_id].get("model_digest")
                or saved.get("source_lock_sha256") != predecessor_lock_sha
            ):
                raise RuntimeError(f"predecessor A512 summary receipt changed: {unit_id}")

    current_lock_path = backend.ARTIFACTS / "source-lock-history" / f"{package}-{source_lock_sha256}.json"
    backend.write_json_once(current_lock_path, dict(current_lock))
    if d.file_sha256(current_lock_path) != source_lock_sha256:
        raise RuntimeError("current formal source-lock archive is missing or changed")
    manifest = {
        "schema_id": "issue84-formal-validation-carry-forward-v1",
        "current": {
            "package_commit": package,
            "source_lock_sha256": source_lock_sha256,
            "seed_table_sha256": d.file_sha256(d.seed_table_path(package)),
            "formal_root": formal_root.relative_to(backend.ARTIFACTS).as_posix(),
        },
        "predecessor": {
            "package_commit": predecessor,
            "source_lock_sha256": predecessor_lock_sha,
            "source_lock_archive_path": lock_path.relative_to(backend.ARTIFACTS).as_posix(),
            "formal_progress_path": progress_path.relative_to(backend.ARTIFACTS).as_posix(),
            "formal_progress_sha256": d.file_sha256(progress_path),
            "seed_table_path": seed_path.relative_to(backend.ARTIFACTS).as_posix(),
            "seed_table_sha256": d.file_sha256(seed_path),
            "completed_validation_groups": ["PARENT"],
            "completed_validation_game_outcomes": 64 + len(successful_reports),
        },
        "groups": {
            "PARENT": {
                "status": "SUCCEEDED",
                "expected": 64,
                "completed": 64,
                "group_record_sha256": _hash(verified_parent),
                "outcomes": parent_refs,
            },
            "A512": {
                "status": "PARTIAL",
                "expected": 64,
                "completed": len(successful_reports),
                "outcomes": [successful_refs[key] for key in sorted(successful_refs)],
                "attempt_inventory": inventory,
            },
        },
    }
    manifest["manifest_sha256"] = _hash(manifest)
    manifest_path = formal_root / "formal-validation-carry-forward.json"
    backend.write_json_once(manifest_path, manifest)
    origin = {
        "schema_id": "issue84-formal-validation-carry-forward-origin-v1",
        "path": manifest_path.relative_to(backend.ARTIFACTS).as_posix(),
        "manifest_sha256": manifest["manifest_sha256"],
        "file_sha256": d.file_sha256(manifest_path),
        "predecessor_package_commit": predecessor,
        "predecessor_source_lock_sha256": predecessor_lock_sha,
    }
    return {
        "origin": origin,
        "groups": {
            "PARENT": {
                "record": verified_parent,
                "outcomes": verified_parent["run_summary"]["outcomes"],
                "references": parent_refs,
                "completed": 64,
                "prior_attempts": 0,
            },
            "A512": {
                "outcomes": a512_summary["outcomes"],
                "references": [successful_refs[key] for key in sorted(successful_refs)],
                "completed": len(successful_reports),
                "prior_attempts": sum(
                    item["attempt_count"] for item in inventory
                    if item["status"] != "SUCCEEDED"
                ),
            },
        },
    }


def _merge_validation_game_group(
    group: str, rows, *, carried: Mapping[str, Any], fresh: Mapping[str, Any],
    origin: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine immutable predecessor results with this package's new owners."""
    carried_outcomes = list(carried.get("outcomes", []))
    fresh_summary = dict(fresh.get("run_summary", {}))
    fresh_outcomes = list(fresh_summary.get("outcomes", []))
    by_id = {}
    for outcome in (*carried_outcomes, *fresh_outcomes):
        unit_id = str(outcome.get("unit_id"))
        if unit_id in by_id:
            raise RuntimeError(f"validation carry-forward duplicated an owner: {unit_id}")
        by_id[unit_id] = outcome
    expected = {str(row["unit_id"]): row for row in rows}
    if not set(by_id) <= set(expected):
        raise RuntimeError(f"validation carry-forward contains unregistered {group} owners")
    for unit_id, outcome in by_id.items():
        if not _validation_summary_row_matches(outcome, expected[unit_id]):
            raise RuntimeError(f"validation carry-forward changed the registered row: {unit_id}")
    completed = len(by_id)
    fresh_status = str(fresh.get("status", fresh_summary.get("status", "FAILED")))
    status = "SUCCEEDED" if completed == len(rows) and fresh_status == "SUCCEEDED" else fresh_status
    prior_attempts = int(carried.get("prior_attempts", 0))
    summary = {
        **fresh_summary,
        "status": status,
        "completed": completed,
        "retry_count": int(fresh_summary.get("retry_count", 0)) + prior_attempts,
        "native_worker_wall_seconds": float(fresh_summary.get("native_worker_wall_seconds", 0.0)),
        "carried_forward_reported_worker_wall_seconds": sum(
            float(item.get("worker_wall_seconds") or 0.0) for item in carried_outcomes
        ),
        "carried_forward_completed": len(carried_outcomes),
        "predecessor_retries": prior_attempts,
        "outcomes": sorted(
            by_id.values(),
            key=lambda item: (str(item.get("arm")), int(item.get("owner_seed", -1))),
        ),
    }
    record = {
        "schema_id": "issue84-game-group-v1",
        "status": status,
        "group": group,
        "expected": len(rows),
        "completed": completed,
        "remaining": len(rows) - completed,
        "run_summary": summary,
        "validation_carry_forward_origin": dict(origin),
    }
    if status == "SUCCEEDED" and any(
        item.get("expert_decisions") != 0 or item.get("terminal_floor") is None
        for item in summary["outcomes"]
    ):
        raise RuntimeError(f"group {group} contains Expert use or an incomplete terminal")
    return record


def _verify_saved_validation_game_group(
    record: Mapping[str, Any], rows, *, root: Path, package: str, carry_forward: Mapping[str, Any],
    group: str, backend,
) -> dict[str, Any]:
    if record.get("validation_carry_forward_origin") != carry_forward.get("origin"):
        raise RuntimeError("saved validation group has a different carry-forward manifest")
    summary = record.get("run_summary", {})
    if (
        record.get("schema_id") != "issue84-game-group-v1"
        or record.get("status") != "SUCCEEDED"
        or record.get("group") != group
        or summary.get("status") != "SUCCEEDED"
    ):
        raise RuntimeError("saved carried validation group is not a completed success")
    by_id = {str(item.get("unit_id")): item for item in summary.get("outcomes", [])}
    if (
        len(by_id) != len(rows) or int(summary.get("completed", -1)) != len(rows)
        or record.get("expected") != len(rows) or record.get("completed") != len(rows)
    ):
        raise RuntimeError("saved carried validation group coverage differs from frozen seed rows")
    carried_group = carry_forward.get("groups", {}).get(group)
    if not isinstance(carried_group, Mapping):
        raise RuntimeError(f"validation carry-forward manifest lacks {group}")
    carried_by_id = {
        str(item.get("unit_id")): item for item in carried_group.get("outcomes", [])
    }
    carried_refs = {
        str(item.get("unit_id")): item for item in carried_group.get("references", [])
    }
    predecessor = d.FORMAL_VALIDATION_PREDECESSOR_PACKAGE
    predecessor_root = backend.ARTIFACTS / "formal" / predecessor
    for row in rows:
        unit_id = str(row["unit_id"])
        saved = by_id.get(unit_id)
        if saved is None or not _validation_summary_row_matches(saved, row):
            raise RuntimeError(f"saved carried validation outcome differs from its seed row: {unit_id}")
        if unit_id in carried_by_id:
            expected_ref = carried_refs.get(unit_id)
            if expected_ref is None:
                raise RuntimeError(f"validation carry-forward lacks a receipt reference: {unit_id}")
            source_package = predecessor
            source_lock = d.FORMAL_VALIDATION_PREDECESSOR_LOCK_SHA256
            source_root = predecessor_root / ("PARENT" if group == "PARENT" else group[0])
            with d._predecessor_source_lock(predecessor):
                result = backend.existing_success_record(source_root, row, predecessor)
        else:
            expected_ref = None
            source_package = package
            source_lock = d.file_sha256(d.SOURCE_LOCK_PATH)
            source_root = Path(root)
            result = backend.existing_success_record(source_root, row, package)
        if result is None:
            raise RuntimeError(f"saved carried validation result is missing: {unit_id}")
        report, result_path = result
        if (
            saved.get("package_commit") != source_package
            or saved.get("source_lock_sha256") != source_lock
            or saved.get("result_sha256") != d.file_sha256(result_path)
            or saved.get("policy_model_digest") != report.get("model_digest")
        ):
            raise RuntimeError(f"saved carried validation receipt changed: {unit_id}")
        if expected_ref is not None and (
            expected_ref.get("result_path") != result_path.relative_to(source_root).as_posix()
            or expected_ref.get("result_sha256") != d.file_sha256(result_path)
            or expected_ref.get("worker_job_sha256") != d.file_sha256(result_path.parent / "worker-job.json")
            or expected_ref.get("started_sha256") != d.file_sha256(result_path.parent / "started.json")
            or expected_ref.get("finished_sha256") != d.file_sha256(result_path.parent / "finished.json")
            or saved != carried_by_id[unit_id]
        ):
            raise RuntimeError(f"saved validation result differs from its immutable carry-forward reference: {unit_id}")
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


def _verify_formal_training_lock_compatibility(
    predecessor_lock: Mapping[str, Any], current_lock: Mapping[str, Any],
) -> None:
    """Permit only the reviewed formal execution adapters to differ from completed training."""
    ignored = {"generated_from_commit", "generated_utc", "local_code_files"}
    if {key for key in predecessor_lock if key not in ignored} != {
        key for key in current_lock if key not in ignored
    } or any(
        predecessor_lock.get(key) != current_lock.get(key)
        for key in predecessor_lock if key not in ignored
    ):
        raise RuntimeError("formal training predecessor changed the contract or locked runtime inputs")
    old_files = predecessor_lock.get("local_code_files")
    new_files = current_lock.get("local_code_files")
    if not isinstance(old_files, Mapping) or not isinstance(new_files, Mapping) or set(old_files) != set(new_files):
        raise RuntimeError("formal training predecessor source inventory differs")
    changed = {name for name in old_files if old_files[name] != new_files[name]}
    if not changed or not changed <= {"driver.py", "formal.py", "tests/test_formal.py"}:
        raise RuntimeError("formal training predecessor contains changes outside the held-out/resume adapter")


def _training_update_stub(update: Mapping[str, Any], *, update_path: Path, backend) -> dict[str, Any]:
    return {
        "schema_id": "issue84-formal-actor-update-carry-forward-v1",
        "status": update["status"],
        "arm": update["arm"],
        "block_index": update["block_index"],
        "completed_steps": update["completed_steps"],
        "result_model_digest": update["result_model_digest"],
        "checkpoint_sha256": update["model_checkpoint_sha256"],
        "optimizer_checkpoint_sha256": update["optimizer_checkpoint_sha256"],
        "predictor_checkpoint_sha256": update.get("predictor_checkpoint_sha256"),
        "source_lock_sha256": update["source_lock_sha256"],
        "package_commit": update["package_commit"],
        "receipt_path": update_path.relative_to(backend.ARTIFACTS).as_posix(),
        "receipt_sha256": d.file_sha256(update_path),
    }


def _verify_formal_training_forecast_carry_forward(
    *, predecessor: str, predecessor_lock_sha256: str, package: str,
    source_lock_sha256: str, old_progress: Mapping[str, Any], artifact_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    predecessor_path = artifact_root / "pilot" / predecessor / "task-wall-forecast.json"
    current_path = artifact_root / "pilot" / package / "task-wall-forecast.json"
    if not predecessor_path.is_file() or not current_path.is_file():
        raise RuntimeError("formal training carry-forward is missing its source-matched wall forecast")
    predecessor_forecast = d.read_json(predecessor_path)
    current_forecast = d.read_json(current_path)
    allowance_basis = predecessor_forecast.get("allowance_basis")
    if (
        old_progress.get("forecast_sha256") != d.file_sha256(predecessor_path)
        or predecessor_forecast.get("schema_id") != "issue84-installed-task-wall-forecast-v1"
        or predecessor_forecast.get("status") != "APPROVED_SINGLE_GLOBAL_CUMULATIVE_ACTIVE_WALL_CAP"
        or predecessor_forecast.get("issue_number") != 84
        or predecessor_forecast.get("study_id") != "issue-84-six-arm"
        or predecessor_forecast.get("package_commit") != predecessor
        or predecessor_forecast.get("source_lock_sha256") != predecessor_lock_sha256
        or predecessor_forecast.get("scope") != "STUDY_ONLY"
        or predecessor_forecast.get("formal_sampling_authorized") is not True
        or predecessor_forecast.get("formal_seeds_started_at_installation") is not False
        or not isinstance(allowance_basis, Mapping)
        or allowance_basis.get("planner_selected_seconds")
        != predecessor_forecast.get("one_task_active_wall_allowance_seconds")
        or current_forecast.get("schema_id") != "issue84-installed-task-wall-forecast-v1"
        or current_forecast.get("status") != "APPROVED_SINGLE_GLOBAL_CUMULATIVE_ACTIVE_WALL_CAP"
        or current_forecast.get("issue_number") != 84
        or current_forecast.get("study_id") != "issue-84-six-arm"
        or current_forecast.get("package_commit") != package
        or current_forecast.get("source_lock_sha256") != source_lock_sha256
        or current_forecast.get("scope") != "STUDY_ONLY"
        or current_forecast.get("formal_sampling_authorized") is not True
        or current_forecast.get("formal_seeds_started_at_installation") is not False
        or predecessor_forecast.get("one_task_active_wall_allowance_seconds")
        != current_forecast.get("one_task_active_wall_allowance_seconds")
    ):
        raise RuntimeError("formal training carry-forward predecessor/current forecast binding differs")
    return predecessor_forecast, current_forecast


def _formal_training_carry_forward(
    *, package: str, source_lock_sha256: str, current_lock: Mapping[str, Any],
    current_seed_table: Mapping[str, Any], formal_root: Path, progress: dict[str, Any],
    continuation: Mapping[str, Any] | None, backend,
) -> dict[str, Any] | None:
    """Validate and reference completed training without replaying any training game or update."""
    predecessor = d.FORMAL_TRAINING_PREDECESSOR_PACKAGE
    predecessor_lock_sha = d.FORMAL_TRAINING_PREDECESSOR_LOCK_SHA256
    predecessor_root = backend.ARTIFACTS / "formal" / predecessor
    predecessor_progress_path = predecessor_root / "formal-progress.json"
    predecessor_seed_path = d.seed_table_path(predecessor)
    lock_path = backend.ARTIFACTS / "source-lock-history" / d.FORMAL_TRAINING_PREDECESSOR_SOURCE_LOCK_ARCHIVE
    evidence_paths = (predecessor_progress_path, predecessor_seed_path, lock_path)
    if not any(path.exists() for path in evidence_paths):
        return None
    if not all(path.is_file() for path in evidence_paths):
        raise RuntimeError("formal training predecessor evidence is incomplete; refusing to repeat training")
    if d.file_sha256(lock_path) != predecessor_lock_sha:
        raise RuntimeError("formal training predecessor source-lock archive changed")
    old_lock = d.read_json(lock_path)
    _verify_formal_training_lock_compatibility(old_lock, current_lock)
    old_progress = d.read_json(predecessor_progress_path)
    old_seed_table = d.read_json(predecessor_seed_path)
    predecessor_progress_sha = d.file_sha256(predecessor_progress_path)
    predecessor_seed_sha = d.file_sha256(predecessor_seed_path)
    _verify_formal_training_forecast_carry_forward(
        predecessor=predecessor,
        predecessor_lock_sha256=predecessor_lock_sha,
        package=package,
        source_lock_sha256=source_lock_sha256,
        old_progress=old_progress,
        artifact_root=backend.ARTIFACTS,
    )
    if (
        package == predecessor
        or old_progress.get("schema_id") != "issue84-formal-progress-v2"
        or old_progress.get("status") != "IN_PROGRESS"
        or old_progress.get("package_commit") != predecessor
        or old_progress.get("source_lock_sha256") != predecessor_lock_sha
        or old_progress.get("active_stage") != "actor-update"
        or old_progress.get("active_arm") != "F"
        or old_progress.get("active_block") != FORMAL_BLOCKS - 1
        or old_progress.get("seed_table_sha256") != predecessor_seed_sha
        or old_seed_table.get("schema_id") != "issue84-seed-table-v1"
        or old_seed_table.get("source_lock_sha256") != predecessor_lock_sha
        or old_seed_table.get("rows") != current_seed_table.get("rows")
        or old_seed_table.get("rows_sha256") != current_seed_table.get("rows_sha256")
        or old_seed_table.get("rows_sha256") != d.sha256(d.canonical(old_seed_table.get("rows")))
        or old_progress.get("validation_groups")
        or old_progress.get("test_groups")
        or continuation is None
        or continuation.get("predecessor_package") != d.FORMAL_PREDECESSOR_PACKAGE
    ):
        raise RuntimeError("formal training predecessor ledger or seed schedule is incompatible")

    group_refs: dict[str, dict[str, Any]] = {}
    policy_models: dict[str, dict[str, Any]] = {}
    old_blocks = old_progress.get("training_blocks")
    if not isinstance(old_blocks, Mapping) or set(old_blocks) != set(d.ARMS):
        raise RuntimeError("formal training predecessor is missing one or more registered arms")

    # Resolve the model through the same clean, source-locked loader used for training.
    # The driver deliberately removes generic #75 imports from sys.path after loading.
    _, _, model_module, _, _ = d.load_modules(formal_root, pilot=False)
    locked_model_path = (d.UPSTREAM75 / "model.py").resolve(strict=True)
    if Path(model_module.__file__).resolve(strict=True) != locked_model_path:
        raise RuntimeError("formal carry-forward loaded a model outside the locked Issue 75 source")

    policy = _issue75_policy()
    for arm in d.ARMS:
        blocks = old_blocks.get(arm)
        if not isinstance(blocks, Mapping) or set(blocks) != {str(block) for block in range(FORMAL_BLOCKS)}:
            raise RuntimeError(f"formal training predecessor block coverage differs for arm {arm}")
        group_refs[arm] = {}
        policy_models[arm] = {}
        previous_model_digest = None
        for block in range(FORMAL_BLOCKS):
            old_group = blocks[str(block)]
            rows = _block_rows(old_seed_table, arm, block)
            if len(rows) != FORMAL_OWNERS_PER_BLOCK:
                raise RuntimeError(f"formal predecessor seed table has incomplete {arm}/{block} training rows")
            expected_group = f"TRAIN-{arm}-BLOCK-{block:02d}"
            if (
                old_group.get("group") != expected_group
                or old_group.get("expected") != FORMAL_OWNERS_PER_BLOCK
                or old_group.get("completed") != FORMAL_OWNERS_PER_BLOCK
                or old_group.get("status") != "SUCCEEDED"
            ):
                raise RuntimeError(f"formal predecessor training group {expected_group} is incomplete")
            if arm == "A" and block == 0:
                old_base = {key: value for key, value in old_group.items() if key not in {"actor_update", "continuation_origin"}}
                old_origin = old_group.get("continuation_origin")
                if not isinstance(old_origin, Mapping):
                    raise RuntimeError("predecessor A0 training group lacks its immutable continuation origin")
                old_bridge_path = (backend.ARTIFACTS / str(old_origin.get("bridge_path", ""))).resolve(strict=True)
                try:
                    old_bridge_path.relative_to(backend.ARTIFACTS.resolve())
                except ValueError as exc:
                    raise RuntimeError("predecessor A0 continuation bridge escaped managed research storage") from exc
                old_bridge = d.read_json(old_bridge_path)
                old_bridge_body = {key: value for key, value in old_bridge.items() if key != "manifest_sha256"}
                current_origin = continuation["training_origin"]
                if (
                    old_origin.get("schema_id") != "issue84-formal-training-continuation-v1"
                    or old_bridge.get("schema_id") != "issue84-formal-continuation-bridge-v1"
                    or d.file_sha256(old_bridge_path) != old_origin.get("bridge_sha256")
                    or old_bridge.get("manifest_sha256") != d.sha256(d.canonical(old_bridge_body))
                    or old_bridge.get("manifest_sha256") != old_origin.get("bridge_manifest_sha256")
                    or old_origin.get("receipts_sha256") != d.sha256(d.canonical(old_bridge.get("units")))
                    or old_origin.get("predecessor_package_commit") != current_origin.get("predecessor_package_commit")
                    or old_origin.get("predecessor_source_lock_sha256") != current_origin.get("predecessor_source_lock_sha256")
                    or old_origin.get("predecessor_root") != current_origin.get("predecessor_root")
                    or old_bridge.get("current", {}).get("package_commit") != predecessor
                    or old_bridge.get("current", {}).get("source_lock_sha256") != predecessor_lock_sha
                    or old_bridge.get("predecessor", {}).get("package_commit") != continuation.get("predecessor_package")
                    or old_bridge.get("predecessor", {}).get("source_lock_sha256") != continuation.get("predecessor_lock_sha256")
                    or old_bridge.get("predecessor", {}).get("formal_arm_a_root")
                    != continuation["predecessor_root"].relative_to(backend.ARTIFACTS).as_posix()
                    or old_bridge.get("units") != continuation.get("bridge", {}).get("units")
                    or old_base != continuation["predecessor_group"]
                ):
                    raise RuntimeError("carried A0 training group differs from the validated immutable continuation")
                game_root = continuation["predecessor_root"]
                game_package = continuation["predecessor_package"]
            else:
                game_root = predecessor_root / arm
                game_package = predecessor
            with d._predecessor_source_lock(game_package):
                verified_group = _verify_saved_game_group(
                    old_base if arm == "A" and block == 0 else old_group,
                    rows, root=game_root, package=game_package, backend=backend,
                )
                feature_manifest_sha = d.owner_feature_manifest(
                    rows, root=game_root, package=game_package,
                )
            group_sha = _hash(verified_group)
            outcomes = verified_group["run_summary"]["outcomes"]
            source_digests = {item.get("policy_model_digest") for item in outcomes}
            if len(source_digests) != 1 or None in source_digests:
                raise RuntimeError(f"formal predecessor group {expected_group} mixed policy source models")
            source_digest = next(iter(source_digests))

            update = old_group.get("actor_update")
            update_path = predecessor_root / arm / "models" / "blocks" / f"block-{block:02d}-update.json"
            step_path = predecessor_root / arm / "models" / "blocks" / f"block-{block:02d}-step.pt"
            if not isinstance(update, Mapping) or not update_path.is_file() or d.read_json(update_path) != update:
                raise RuntimeError(f"formal predecessor actor-update receipt is missing or changed for {arm}/{block}")
            expected_steps = 128 if block == 0 else 64
            expected_label = f"pi{13 + block}"
            model_rel = str(update.get("model_checkpoint", {}).get("path", ""))
            model_path = (predecessor_root / arm / model_rel).resolve(strict=True)
            artifact_root = backend.ARTIFACTS.resolve()
            try:
                model_path.relative_to(artifact_root)
            except ValueError as exc:
                raise RuntimeError("formal predecessor model checkpoint escaped managed research storage") from exc
            model_sha = d.file_sha256(model_path)
            expected_owner_seed_sha = hashlib.sha256(
                ",".join(str(int(row["owner_seed"])) for row in sorted(rows, key=lambda item: int(item["owner_seed"]))).encode("ascii")
            ).hexdigest()
            expected_source_checkpoint_sha = (
                d.PARENT_MODEL_SHA256
                if block == 0
                else blocks[str(block - 1)]["actor_update"].get("model_checkpoint_sha256")
            )
            actor = _new_predecessor_actor(model_module, policy)
            if (
                update.get("schema_id") != "issue84-formal-actor-update-v1"
                or update.get("status") != "SUCCEEDED"
                or update.get("arm") != arm
                or update.get("block_index") != block
                or update.get("owner_count") != FORMAL_OWNERS_PER_BLOCK
                or update.get("owner_seed_sha256") != expected_owner_seed_sha
                or update.get("owner_training_feature_manifest_sha256") != feature_manifest_sha
                or update.get("completed_steps") != 64
                or update.get("package_commit") != predecessor
                or update.get("source_lock_sha256") != predecessor_lock_sha
                or update.get("source_model_digest") != source_digest
                or (previous_model_digest is not None and source_digest != previous_model_digest)
                or update.get("source_model_checkpoint_sha256") != expected_source_checkpoint_sha
                or update.get("model_checkpoint", {}).get("path") != f"models/{expected_label}.pt"
                or update.get("model_checkpoint_sha256") != model_sha
                or update.get("model_checkpoint", {}).get("sha256") != model_sha
                or update.get("model_checkpoint", {}).get("model_digest") != update.get("result_model_digest")
                or update.get("optimizer_checkpoint_sha256") != d.file_sha256(step_path)
                or update.get("completed_steps") != expected_steps // (2 if block == 0 else 1)
                or not _optimizer_parameter_steps_match(
                    update.get("optimizer_parameter_steps"),
                    expected_steps=expected_steps,
                    parameter_count=len(tuple(actor.parameters())),
                )
            ):
                raise RuntimeError(f"formal predecessor actor update binding differs for {arm}/{block}")
            if block == 0:
                expected_origin = old_group.get("continuation_origin") if arm == "A" else None
                if update.get("training_evidence_origin") != expected_origin:
                    raise RuntimeError(f"formal predecessor block-zero evidence origin differs for arm {arm}")
            elif update.get("training_evidence_origin") is not None:
                raise RuntimeError(f"formal predecessor actor update has unexpected evidence origin for {arm}/{block}")

            actor.load_state_dict(torch.load(model_path, map_location="cpu", weights_only=True), strict=True)
            observed_digest = model_module.model_digest(actor)
            if observed_digest != update["result_model_digest"]:
                raise RuntimeError(f"formal predecessor model digest differs for {arm}/{block}")
            previous_model_digest = observed_digest

            predictor_sha = update.get("predictor_checkpoint_sha256")
            predictor_path = predecessor_root / arm / "models" / "predictors" / f"block-{block:02d}-before.pt"
            if arm in {"D", "E", "F"}:
                if not predictor_path.is_file() or d.file_sha256(predictor_path) != predictor_sha:
                    raise RuntimeError(f"formal predecessor predictor checkpoint differs for {arm}/{block}")
            elif predictor_sha is not None:
                raise RuntimeError(f"formal predecessor arm {arm} unexpectedly has a predictor checkpoint")

            update_stub = _training_update_stub(update, update_path=update_path, backend=backend)
            group_refs[arm][str(block)] = {
                "schema_id": "issue84-formal-training-group-carry-forward-v1",
                "status": "SUCCEEDED",
                "group": expected_group,
                "expected": FORMAL_OWNERS_PER_BLOCK,
                "completed": FORMAL_OWNERS_PER_BLOCK,
                "group_summary_sha256": group_sha,
                "run_summary_package_commit": game_package,
                "run_summary_source_lock_sha256": (
                    d.FORMAL_PREDECESSOR_LOCK_SHA256 if game_package == d.FORMAL_PREDECESSOR_PACKAGE
                    else predecessor_lock_sha
                ),
                "actor_update": update_stub,
            }
            policy_models[arm][expected_label] = {
                "arm": arm,
                "label": expected_label,
                "path": model_path.relative_to(artifact_root).as_posix(),
                "checkpoint_sha256": model_sha,
                "model_digest": observed_digest,
                "predecessor_package_commit": predecessor,
                "predecessor_source_lock_sha256": predecessor_lock_sha,
                "actor_update_receipt_path": update_path.relative_to(artifact_root).as_posix(),
                "actor_update_receipt_sha256": d.file_sha256(update_path),
                "optimizer_checkpoint_sha256": update["optimizer_checkpoint_sha256"],
                "predictor_checkpoint_sha256": predictor_sha,
            }

    manifest = {
        "schema_id": "issue84-formal-training-carry-forward-v1",
        "current": {
            "package_commit": package,
            "source_lock_sha256": source_lock_sha256,
            "formal_root": formal_root.relative_to(backend.ARTIFACTS).as_posix(),
            "seed_table_sha256": d.file_sha256(backend.SEED_TABLE),
        },
        "predecessor": {
            "package_commit": predecessor,
            "source_lock_sha256": predecessor_lock_sha,
            "source_lock_archive_path": lock_path.relative_to(backend.ARTIFACTS).as_posix(),
            "progress_path": predecessor_progress_path.relative_to(backend.ARTIFACTS).as_posix(),
            "progress_sha256": predecessor_progress_sha,
            "seed_table_path": predecessor_seed_path.relative_to(backend.ARTIFACTS).as_posix(),
            "seed_table_sha256": predecessor_seed_sha,
            "completed_training_groups": len(d.ARMS) * FORMAL_BLOCKS,
            "completed_training_game_outcomes": len(d.ARMS) * FORMAL_BLOCKS * FORMAL_OWNERS_PER_BLOCK,
            "completed_actor_updates": len(d.ARMS) * FORMAL_BLOCKS,
        },
        "training_groups": group_refs,
        "final_policy_models": policy_models,
    }
    manifest["manifest_sha256"] = _hash(manifest)
    manifest_path = formal_root / "formal-training-carry-forward.json"
    backend.write_json_once(manifest_path, manifest)
    manifest_sha = d.file_sha256(manifest_path)
    ref = {
        "schema_id": "issue84-formal-training-carry-forward-origin-v1",
        "path": manifest_path.relative_to(backend.ARTIFACTS).as_posix(),
        "manifest_sha256": manifest["manifest_sha256"],
        "file_sha256": manifest_sha,
        "predecessor_package_commit": predecessor,
        "predecessor_source_lock_sha256": predecessor_lock_sha,
    }
    expected_groups: dict[str, dict[str, Any]] = {}
    for arm in d.ARMS:
        expected_groups[arm] = {}
        for block in range(FORMAL_BLOCKS):
            group_ref = dict(group_refs[arm][str(block)])
            group_ref["carry_forward_origin"] = ref
            if arm == "A" and block == 0:
                group_ref = {
                    **continuation["predecessor_group"],
                    "continuation_origin": continuation["training_origin"],
                    "execution_carry_forward_origin": ref,
                    "run_summary_origin": group_ref,
                    "actor_update": group_ref["actor_update"],
                }
            else:
                group_ref["execution_carry_forward_origin"] = group_ref.pop("carry_forward_origin")
            expected_groups[arm][str(block)] = group_ref

    if progress.get("execution_carry_forward") not in (None, ref):
        raise RuntimeError("current formal progress is bound to a different training carry-forward")
    current_blocks = progress.get("training_blocks", {})
    empty_blocks = {arm: {} for arm in d.ARMS}
    initial_blocks = {arm: {} for arm in d.ARMS}
    initial_blocks["A"]["0"] = {
        **continuation["predecessor_group"],
        "continuation_origin": continuation["training_origin"],
    }
    if current_blocks not in (empty_blocks, initial_blocks):
        if current_blocks != expected_groups:
            raise RuntimeError("current formal training groups differ from the verified carry-forward manifest")
    else:
        progress["training_blocks"] = expected_groups
    progress["execution_carry_forward"] = ref
    return {"manifest": manifest, "manifest_path": manifest_path, "manifest_sha256": manifest_sha}


def run_formal() -> dict[str, Any]:
    source = d.verify_source_lock(require_clean_worktree=True)
    backend = d.configure_backend()
    osenv = __import__("os").environ
    osenv["STS_LIGHTSPEED_SOURCE_DIR"] = d.STS_SOURCE_WSL
    osenv["STSRL_SOURCE_DIR"] = d.STSRL_SOURCE_WSL
    package = d.package_commit()
    seed_table = d.ensure_seed_table()
    continuation = d.formal_continuation(
        package=package,
        source_lock_sha256=source["sha256"],
        current_lock=source["lock"],
        current_seed_table=seed_table,
        backend=backend,
    )
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
    if continuation is not None and continuation["forecast_path"] != forecast_path:
        raise RuntimeError("formal continuation forecast is not in the current package namespace")
    formal_root = backend.ARTIFACTS / "formal" / package
    formal_root.mkdir(parents=True, exist_ok=True)
    progress_path = formal_root / "formal-progress.json"
    progress = d.read_json(progress_path) if progress_path.is_file() else {
        "schema_id": "issue84-formal-progress-v2",
        "status": "IN_PROGRESS",
        "package_commit": package,
        "source_lock_sha256": source["sha256"],
        "seed_table_sha256": d.file_sha256(backend.SEED_TABLE),
        "forecast_sha256": d.file_sha256(forecast_path),
        "training_blocks": {arm: {} for arm in d.ARMS},
        "validation_groups": {},
        "test_groups": {},
    }
    if continuation is not None:
        origin = continuation["training_origin"]
        if not progress_path.is_file():
            progress["continuation_bridge_sha256"] = continuation["bridge_sha256"]
            progress["training_blocks"]["A"]["0"] = {
                **continuation["predecessor_group"],
                "continuation_origin": origin,
            }
        elif progress.get("continuation_bridge_sha256") != continuation["bridge_sha256"]:
            raise RuntimeError("current formal progress is not bound to the validated continuation bridge")
        saved_a0 = progress.get("training_blocks", {}).get("A", {}).get("0")
        if not isinstance(saved_a0, dict):
            raise RuntimeError("current formal progress lost its carried A0 evidence")
        if saved_a0.get("continuation_origin") != origin:
            raise RuntimeError("current formal A0 progress changed its continuation provenance")
        saved_base = {
            key: value for key, value in saved_a0.items()
            if key not in {
                "continuation_origin", "actor_update", "execution_carry_forward_origin", "run_summary_origin",
            }
        }
        if saved_base != continuation["predecessor_group"]:
            raise RuntimeError("current formal A0 progress differs from the immutable predecessor group")
    expected_binding = {
        "schema_id": "issue84-formal-progress-v2",
        "package_commit": package,
        "source_lock_sha256": source["sha256"],
        "seed_table_sha256": d.file_sha256(backend.SEED_TABLE),
        "forecast_sha256": d.file_sha256(forecast_path),
    }
    if continuation is not None:
        expected_binding["continuation_bridge_sha256"] = continuation["bridge_sha256"]
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
        training_carry_forward = _formal_training_carry_forward(
            package=package,
            source_lock_sha256=source["sha256"],
            current_lock=source["lock"],
            current_seed_table=seed_table,
            formal_root=formal_root,
            progress=progress,
            continuation=continuation,
            backend=backend,
        )
        if training_carry_forward is not None:
            progress["cumulative_active_wall_seconds"] = clock.elapsed()
            _save_progress(progress_path, progress, backend)
        for arm in (() if training_carry_forward is not None else d.ARMS):
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
                if continuation is not None and arm == "A" and block == 0:
                    if saved_group is None:
                        raise RuntimeError("current formal progress is missing the bridged A0 training group")
                    if saved_group.get("continuation_origin") != continuation["training_origin"]:
                        raise RuntimeError("current formal A0 group is not bound to the validated continuation bridge")
                    saved_base = {
                        key: value for key, value in saved_group.items()
                        if key not in {"continuation_origin", "actor_update"}
                    }
                    if saved_base != continuation["predecessor_group"]:
                        raise RuntimeError("current formal A0 group differs from its immutable predecessor")
                    game_summary = {
                        **continuation["predecessor_group"],
                        "continuation_origin": continuation["training_origin"],
                    }
                elif saved_group is not None:
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
                training_root = root
                training_package = package
                training_origin = None
                if continuation is not None and arm == "A" and block == 0:
                    training_root = continuation["predecessor_root"]
                    training_package = continuation["predecessor_package"]
                    training_origin = continuation["training_origin"]
                update = _train_actor_block(
                    arm, block, rows, root=root, package=package,
                    clock=clock, progress=progress, backend=backend,
                    training_root=training_root, training_package=training_package,
                    training_origin=training_origin,
                )
                progress["training_blocks"][arm][str(block)]["actor_update"] = update
                progress["cumulative_active_wall_seconds"] = clock.elapsed()
                _save_progress(progress_path, progress, backend)

        validation_carry_forward = _formal_validation_carry_forward(
            package=package,
            source_lock_sha256=source["sha256"],
            current_lock=source["lock"],
            current_seed_table=seed_table,
            formal_root=formal_root,
            backend=backend,
        )
        if validation_carry_forward is not None:
            existing_origin = progress.get("validation_carry_forward")
            if existing_origin not in (None, validation_carry_forward["origin"]):
                raise RuntimeError("formal progress is bound to a different validation carry-forward manifest")
            progress["validation_carry_forward"] = validation_carry_forward["origin"]
            _save_progress(progress_path, progress, backend)
        elif progress.get("validation_carry_forward") is not None:
            raise RuntimeError("formal progress references missing validation carry-forward evidence")

        validation_groups = progress["validation_groups"]
        validation_root = backend.ARTIFACTS / "validation" / package
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
            carried = (
                validation_carry_forward["groups"].get(group)
                if validation_carry_forward is not None else None
            )
            progress["active_stage"] = "validation"
            progress["active_validation_group"] = {
                "group": group,
                "expected": len(rows),
                "carried_forward": 0 if carried is None else int(carried["completed"]),
                "source_package": package,
            }
            progress["cumulative_active_wall_seconds"] = clock.elapsed()
            _save_progress(progress_path, progress, backend)
            if group in validation_groups:
                if carried is not None:
                    group_result = _verify_saved_validation_game_group(
                        validation_groups[group], rows, root=group_root, package=package,
                        carry_forward=validation_carry_forward, group=group, backend=backend,
                    )
                else:
                    group_result = _verify_saved_game_group(
                        validation_groups[group], rows, root=group_root, package=package, backend=backend,
                    )
            elif carried is not None and group == "PARENT":
                group_result = {
                    **carried["record"],
                    "validation_carry_forward_origin": validation_carry_forward["origin"],
                }
                group_result = _verify_saved_validation_game_group(
                    group_result, rows, root=group_root, package=package,
                    carry_forward=validation_carry_forward, group=group, backend=backend,
                )
            elif carried is not None:
                carried_ids = {
                    str(item["unit_id"]) for item in carried.get("outcomes", [])
                }
                fresh_rows = [row for row in rows if str(row["unit_id"]) not in carried_ids]
                if fresh_rows:
                    fresh_result = _record_game_group(
                        group, fresh_rows, root=group_root, pilot=False, package=package,
                        clock=clock, backend=backend,
                    )
                else:
                    fresh_result = {
                        "status": "SUCCEEDED",
                        "run_summary": {
                            "status": "SUCCEEDED", "completed": 0, "elapsed_seconds": 0.0,
                            "native_worker_wall_seconds": 0.0, "retry_count": 0, "outcomes": [],
                        },
                    }
                group_result = _merge_validation_game_group(
                    group, rows, carried=carried, fresh=fresh_result,
                    origin=validation_carry_forward["origin"],
                )
                if group_result["status"] == "SUCCEEDED":
                    group_result = _verify_saved_validation_game_group(
                        group_result, rows, root=group_root, package=package,
                        carry_forward=validation_carry_forward, group=group, backend=backend,
                    )
                else:
                    progress.setdefault("validation_partials", {})[group] = group_result
                    progress["status"] = group_result["status"]
                    progress["stop_boundary"] = {"stage": "validation", "group": group}
                    progress["active_validation_group"]["completed"] = group_result["completed"]
                    progress["cumulative_active_wall_seconds"] = clock.elapsed()
                    _save_progress(progress_path, progress, backend)
                    return group_result
            else:
                group_result = _record_game_group(
                    group, rows, root=group_root, pilot=False, package=package,
                    clock=clock, backend=backend,
                )
            if group_result["status"] != "SUCCEEDED":
                progress.setdefault("validation_partials", {})[group] = group_result
                progress["status"] = group_result["status"]
                progress["stop_boundary"] = {"stage": "validation", "group": group}
                progress["active_validation_group"]["completed"] = group_result.get("completed", 0)
                progress["cumulative_active_wall_seconds"] = clock.elapsed()
                _save_progress(progress_path, progress, backend)
                return group_result
            progress.setdefault("validation_partials", {}).pop(group, None)
            progress.pop("active_validation_group", None)
            validation_groups[group] = group_result
            progress["cumulative_active_wall_seconds"] = clock.elapsed()
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
