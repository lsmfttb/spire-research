from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence

from algorithm import (
    Decision, InvocationBudget, J_COMPLETED_PORTFOLIO_CAP, J_LATER_MUTATION_TRIAL_CAP,
    J_ROOT_TRIAL_CAP, MutationTarget, R_ROOT_TRIAL_CAP, SEARCH_CPU_CAP_SECONDS,
    SEARCH_STARTED_ATTEMPT_CAP, SearchBudgetExhausted,
    TrialResult, canonical, identity_key, process_tree_cpu_seconds,
    run_search, write_immutable_json,
)
from preflight import load_json, normalized_sha256, reviewer_pass_proof, verify_source_lock

STUDY = Path(__file__).resolve().parent
ROOT = STUDY.parents[1]
RECEIPTS = STUDY / "stage0" / "receipts"
NATIVE_OUTPUT = STUDY / "stage0" / "native-output"
WORKER_MARKER = "ISSUE75_ROOT_RESULT:"


def _resource_module() -> Any:
    import resource
    return resource


def _self_cpu() -> float:
    row = _resource_module().getrusage(_resource_module().RUSAGE_SELF)
    return float(row.ru_utime + row.ru_stime)


def _children_cpu() -> float:
    row = _resource_module().getrusage(_resource_module().RUSAGE_CHILDREN)
    return float(row.ru_utime + row.ru_stime)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path.name}")
    return value


def _write_once(path: Path, value: Mapping[str, Any]) -> None:
    write_immutable_json(path, value)


def _write_result_once(path: Path, value: Mapping[str, Any]) -> dict[str, Any]:
    if path.exists():
        return _read_json(path)
    _write_once(path, value)
    return dict(value)


def _load_locked_state() -> tuple[dict[str, Any], dict[str, Any], str, str]:
    config = load_json(STUDY / "config.json")
    source_lock = verify_source_lock()
    lock_sha = normalized_sha256(STUDY / "source-lock.json")
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()
    source_branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"], cwd=ROOT, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()
    if source_branch != str(source_lock.get("source_branch")):
        raise RuntimeError("Stage-0 execution must remain on the source-locked study branch")
    if source_lock.get("source_parent_commit") != config["source"]["spire_reviewed_commit"]:
        raise RuntimeError("source lock is not bound to the configured reviewed parent")
    if source_lock.get("formal_training_enabled") or source_lock.get("formal_evaluation_enabled"):
        raise RuntimeError("formal training/evaluation must remain disabled")
    if source_lock.get("formal_seed_table_present"):
        raise RuntimeError("formal seed table must remain absent")
    return config, source_lock, lock_sha, source_commit


def _selected_roots(source_lock: Mapping[str, Any]) -> list[dict[str, Any]]:
    provenance = source_lock.get("provenance")
    rows = provenance.get("selected_roots") if isinstance(provenance, Mapping) else None
    if not isinstance(rows, list):
        raise RuntimeError("source lock lacks selected debug roots")
    return [dict(row) for row in rows]


def _paths(root_id: str) -> dict[str, Path]:
    safe_id = "".join(ch for ch in root_id if ch.isalnum() or ch in "-_")
    if safe_id != root_id or not safe_id:
        raise ValueError("root ID is not a path-safe locked identity")
    return {
        "start": RECEIPTS / "roots" / f"{root_id}-start.json",
        "result": RECEIPTS / "roots" / f"{root_id}-result.json",
        "resource": RECEIPTS / "resources" / f"{root_id}.json",
    }


def _trial_receipt(trial: TrialResult) -> dict[str, Any]:
    row: dict[str, Any] = {
        "attempt_index": int(trial.attempt_index),
        "complete": bool(trial.complete),
        "cpu_seconds": None if trial.cpu_seconds is None else float(trial.cpu_seconds),
        "wall_seconds": None if trial.wall_seconds is None else float(trial.wall_seconds),
        "decision_count": len(trial.decisions),
        "later_multichoice_decisions": sum(not item.is_root for item in trial.decisions),
        "native_rng_source": trial.native_rng_source,
    }
    if trial.complete:
        row.update({
            "heart_win": bool(trial.heart_win),
            "native_terminal_floor": float(trial.terminal_floor),
            "selected_root_action_sha256": identity_key(trial.root_identity or {}),
        })
    else:
        row.update({
            "failure_boundary": str(trial.failure_boundary or "incomplete_without_terminal_outcome"),
            "terminal_outcome_recorded": False,
        })
    return row


def _safe_failure(error: BaseException, *, boundary: str) -> dict[str, Any]:
    return {
        "schema_id": "issue75-stage0-safe-failure-v1",
        "status": "INCOMPLETE",
        "failure_boundary": boundary,
        "error_type": type(error).__name__,
        "native_checkpoint_persisted": False,
        "privileged_projection_persisted": False,
        "formal_training_seed_consumed": False,
        "formal_evaluation_seed_consumed": False,
    }


def _current_root_worker_pids(root_id: str) -> list[int]:
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    result: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "ignore")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if "stage0.py" in command and "--root-worker" in command and root_id in command:
            result.append(int(entry.name))
    return sorted(result)


def _configure_task_runtime() -> None:
    temp = STUDY / "stage0" / "tmp"
    cache = STUDY / "stage0" / "cache"
    for path in (temp, cache, cache / "torch", cache / "matplotlib"):
        path.mkdir(parents=True, exist_ok=True)
    for name in ("TMPDIR", "TMP", "TEMP"):
        os.environ[name] = str(temp)
    os.environ["XDG_CACHE_HOME"] = str(cache)
    os.environ["TORCH_HOME"] = str(cache / "torch")
    os.environ["MPLCONFIGDIR"] = str(cache / "matplotlib")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def _set_root_worker_cpu_limit() -> None:
    resource = _resource_module()
    config = load_json(STUDY / "config.json")
    hard = int(config["resources"]["stage0_root_worker_process_cpu_hard_seconds"])
    if hard < 2:
        raise RuntimeError("root-worker CPU limit must be at least two seconds")
    _, current_hard = resource.getrlimit(resource.RLIMIT_CPU)
    if current_hard != resource.RLIM_INFINITY and int(current_hard) < hard:
        raise RuntimeError("inherited root-worker CPU hard limit is below the registered limit")
    resource.setrlimit(resource.RLIMIT_CPU, (hard - 1, hard))


def _decode_trial(value: Mapping[str, Any]) -> TrialResult:
    if not value.get("complete"):
        return TrialResult(
            attempt_index=int(value["attempt_index"]), complete=False,
            failure_boundary=str(value.get("failure_boundary", "incomplete")),
            cpu_seconds=value.get("cpu_seconds"), wall_seconds=value.get("wall_seconds"),
        )
    import numpy as np
    import zlib

    decisions: list[Decision] = []
    for row in value.get("decisions", []):
        packed = base64.b64decode(str(row["candidate_features_f32le_zlib_base64"]), validate=True)
        matrix_bytes = zlib.decompress(packed)
        shape = tuple(int(v) for v in row["candidate_features_shape"])
        if len(shape) != 2 or any(v <= 0 for v in shape):
            raise ValueError("training feature payload has an invalid matrix shape")
        if hashlib.sha256(matrix_bytes).hexdigest() != row["candidate_features_sha256"]:
            raise ValueError("training feature payload hash mismatch")
        matrix = np.frombuffer(matrix_bytes, dtype="<f4").reshape(shape).copy()
        hashes = [str(item) for item in row["legal_identity_sha256"]]
        if len(hashes) != shape[0]:
            raise ValueError("hashed legal occurrence count differs from feature rows")
        chosen = int(row["chosen_index"])
        if not 0 <= chosen < len(hashes):
            raise ValueError("selected action does not map to a live legal occurrence")
        decisions.append(Decision(
            step_index=int(row["step_index"]),
            state_fingerprint=str(row["state_fingerprint"]),
            legal_identities=tuple({"sha256": item} for item in hashes),
            chosen_index=chosen,
            candidate_features=matrix,
            is_root=bool(row["is_root"]),
        ))
    return TrialResult(
        attempt_index=int(value["attempt_index"]), complete=True,
        heart_win=bool(value["heart_win"]),
        terminal_floor=float(value["terminal_floor"]),
        root_identity={"sha256": str(value["root_identity_sha256"])},
        decisions=tuple(decisions),
        cpu_seconds=value.get("cpu_seconds"),
        wall_seconds=value.get("wall_seconds"),
        native_rng_source=str(value.get("native_rng_source", "restored_checkpoint_native_rng_same_future_stream")),
    )


def _root_worker(root_id: str) -> int:
    """Execute one preselected root once; private native data stays in memory."""
    _set_root_worker_cpu_limit()
    start_cpu = process_tree_cpu_seconds()
    start_wall = time.monotonic()
    result_path = _paths(root_id)["result"]
    if result_path.exists():
        raise RuntimeError("root already has an immutable result receipt; replay is forbidden")
    config: dict[str, Any] = {}
    proof: dict[str, Any] = {}
    sampling_authorized = False
    try:
        config, source_lock, lock_sha, source_commit = _load_locked_state()
        _safe_config_preflight(config)
        proof = reviewer_pass_proof()
        stage_start_path = RECEIPTS / "stage0-start.json"
        if not stage_start_path.is_file():
            raise RuntimeError("root worker requires the parent Stage-0 authorization receipt")
        stage_start = _read_json(stage_start_path)
        if (
            stage_start.get("source_commit") != source_commit
            or stage_start.get("source_lock_sha256_lf") != lock_sha
            or int(stage_start.get("reviewer_comment_id", -1)) != int(proof["comment_id"])
            or str(stage_start.get("reviewer_login", "")).casefold() != str(proof["reviewer_login"]).casefold()
            or stage_start.get("status") != "AUTHORIZED_STAGE0_NONFORMAL_ONLY"
            or stage_start.get("formal_training_seed_consumed") is not False
            or stage_start.get("formal_evaluation_seed_consumed") is not False
            or int(stage_start.get("native_subset_cpu_hard_seconds", -1))
                != int(config["resources"]["native_root_checkpoint_terminal_cpu_hard_seconds"])
            or int(stage_start.get("builder_cpu_hard_seconds", -1))
                != int(config["resources"]["builder_process_tree_cpu_hard_seconds"])
            or int(stage_start.get("reviewer_cpu_protected_seconds", -1))
                != int(config["resources"]["reviewer_protected_cpu_seconds"])
        ):
            raise RuntimeError("parent Stage-0 authorization receipt does not match the locked source/reviewer gate")
        root_start_path = _paths(root_id)["start"]
        if not root_start_path.is_file():
            raise RuntimeError("root worker requires a parent-created root start receipt")
        root_start = _read_json(root_start_path)
        if (
            root_start.get("root_id") != root_id
            or root_start.get("source_commit") != source_commit
            or root_start.get("source_lock_sha256_lf") != lock_sha
            or int(root_start.get("owner_seed", -1)) != int(next(
                row["owner_seed"] for row in _selected_roots(source_lock) if row["root_id"] == root_id
            ))
            or int(root_start.get("reviewer_comment_id", -1)) != int(proof["comment_id"])
            or str(root_start.get("reviewer_login", "")).casefold() != str(proof["reviewer_login"]).casefold()
            or root_start.get("formal_training_seed_consumed") is not False
            or root_start.get("formal_evaluation_seed_consumed") is not False
            or int(root_start.get("worker_process_cpu_hard_seconds", -1))
                != int(config["resources"]["stage0_root_worker_process_cpu_hard_seconds"])
            or root_start.get("native_root_invocation_caps")
                != {"historical_prefix": 32, "pi0_base": 32, "R": 32, "J": 32}
            or int(root_start.get("root_proposal_limit", -1)) != 8
        ):
            raise RuntimeError("parent root start receipt does not match this locked root/reviewer gate")
        sampling_authorized = True
        selected = next((row for row in _selected_roots(source_lock) if row["root_id"] == root_id), None)
        if selected is None:
            raise RuntimeError("root ID is not in the source-locked selection")
        _configure_task_runtime()
        from runtime import (
            execute_policy_trajectory, initialize_native_runtime,
            process_cpu_alarm, replay_locked_root, _safe_trial_payload,
        )
        from model import ActionConditionedPolicy, model_digest

        owner_seed = int(selected["owner_seed"])
        native_out = NATIVE_OUTPUT / root_id
        runtime_config, runner, module, encoder = initialize_native_runtime(native_out / "setup", sampling=True)
        if runtime_config != config:
            raise RuntimeError("runtime configuration differs from the locked Stage-0 source")
        policy = ActionConditionedPolicy(
            dimension=int(config["policy"]["actor_layers"][0]),
            seed=int(config["policy"]["initial_model_seed"]),
            final_weight_std=float(config["policy"]["actor_final_weight_std"]),
        ).eval()
        policy_sha = model_digest(policy)

        prefix_start_cpu = _self_cpu()
        _write_once(RECEIPTS / "roots" / f"{root_id}-prefix-start.json", {
            "schema_id": "issue75-stage0-prefix-start-v1",
            "owner_seed": owner_seed, "root_id": root_id,
            "purpose": "issue73-stage0-debug-owner",
            "source_commit": source_commit, "source_lock_sha256_lf": lock_sha,
            "native_sampling_started": True, "formal_seed_consumed": False,
        })
        try:
            with process_cpu_alarm(32.0):
                root = replay_locked_root(
                    runner=runner, module=module, encoder=encoder,
                    output_dir=native_out / "prefix", owner_seed=owner_seed,
                    expected=selected,
                )
        except Exception as exc:
            from algorithm import SearchBudgetExhausted
            if isinstance(exc, SearchBudgetExhausted):
                prefix_status = "RESOURCE_BLOCKED"
            elif type(exc).__name__ == "SourceRootMismatch":
                prefix_status = "SOURCE_INVALID"
            else:
                prefix_status = "TECHNICAL_CHAIN_INCOMPLETE"
            _write_result_once(RECEIPTS / "roots" / f"{root_id}-prefix-result.json", {
                "schema_id": "issue75-stage0-prefix-result-v1",
                "status": prefix_status, "error_type": type(exc).__name__,
                "cpu_seconds": max(0.0, _self_cpu() - prefix_start_cpu),
                "checkpoint_persisted": False, "privileged_projection_persisted": False,
            })
            raise
        _write_once(RECEIPTS / "roots" / f"{root_id}-prefix-result.json", {
            "schema_id": "issue75-stage0-prefix-result-v1",
            "status": "SOURCE_ROOT_MATCHED", "owner_seed": owner_seed, "root_id": root_id,
            "native_state_fingerprints_sha256": hashlib.sha256(canonical(dict(root.fingerprints)).encode()).hexdigest(),
            "legal_action_identities_sha256": hashlib.sha256(canonical(list(root.legal_identities)).encode()).hexdigest(),
            "feature_matrix_sha256": root.feature_matrix_sha256,
            "legal_occurrence_count": len(root.legal_identities),
            "cpu_seconds": max(0.0, _self_cpu() - prefix_start_cpu),
            "checkpoint_persisted": False, "privileged_projection_persisted": False,
        })

        base_start_cpu = _self_cpu()
        _write_once(RECEIPTS / "roots" / f"{root_id}-base-start.json", {
            "schema_id": "issue75-stage0-base-start-v1",
            "owner_seed": owner_seed, "root_id": root_id, "arm": "P0",
            "policy_sha256": policy_sha, "source_commit": source_commit,
            "source_lock_sha256_lf": lock_sha, "formal_seed_consumed": False,
        })
        base = execute_policy_trajectory(
            runner=runner, module=module, encoder=encoder,
            output_dir=native_out / "pi0-base", policy=policy,
            owner_seed=owner_seed, root=root, attempt_index=1,
            invocation_seed=-1, root_action_index=None,
            mutation_target=None, cpu_cap_seconds=32.0, source_arm="P0",
        )
        _write_once(RECEIPTS / "roots" / f"{root_id}-base-result.json", {
            "schema_id": "issue75-stage0-base-result-v1",
            "status": "TERMINAL_COMPLETE" if base.complete else "INCOMPLETE",
            "source_commit": source_commit, "policy_sha256": policy_sha,
            **_trial_receipt(base),
            "native_checkpoint_persisted": False, "privileged_projection_persisted": False,
        })

        search_summaries: dict[str, Any] = {}
        joint_portfolio: list[TrialResult] = []
        for arm in ("R", "J"):
            _write_once(RECEIPTS / "roots" / f"{root_id}-{arm}-search-start.json", {
                "schema_id": "issue75-stage0-search-start-v1",
                "owner_seed": owner_seed, "root_id": root_id, "arm": arm,
                "policy_sha256": policy_sha, "cpu_cap_seconds": 32,
                "started_attempt_cap": 16, "source_commit": source_commit,
                "source_lock_sha256_lf": lock_sha, "formal_seed_consumed": False,
            })
            budget = InvocationBudget(cpu_cap_seconds=32.0, started_attempt_cap=16)
            completed: dict[int, TrialResult] = {}
            search_cpu_start = process_tree_cpu_seconds()
            search_wall_start = time.monotonic()

            def start_attempt(attempt_index: int, root_index: int, target: MutationTarget | None) -> None:
                common: dict[str, Any] = {
                    "schema_id": "issue75-stage0-attempt-start-v1",
                    "owner_seed": owner_seed, "root_id": root_id, "arm": arm,
                    "attempt_index": int(attempt_index), "source_commit": source_commit,
                    "policy_sha256": policy_sha,
                    "root_action_index": None if target is not None else int(root_index),
                    "formal_seed_consumed": False,
                }
                if target is None:
                    common["root_action_identity_sha256"] = identity_key(root.legal_identities[root_index])
                else:
                    common["mutation"] = {
                        "parent_attempt_index": int(target.parent_attempt_index),
                        "target_step_index": int(target.parent_decision.step_index),
                        "target_state_fingerprint": str(target.parent_decision.state_fingerprint),
                        "target_screen": str(target.parent_decision.screen),
                        "alternative_identity_sha256": identity_key(target.parent_decision.legal_identities[target.alternative_index]),
                        "alternative_index": int(target.alternative_index),
                    }
                _write_once(RECEIPTS / "attempts" / f"{root_id}-{arm}-{attempt_index:02d}-start.json", common)

            def attempt_runner(attempt_index: int, root_index: int, target: MutationTarget | None) -> TrialResult:
                parent = completed.get(int(target.parent_attempt_index)) if target is not None else None
                if target is not None and parent is None:
                    return TrialResult(
                        attempt_index=attempt_index, complete=False,
                        failure_boundary="mutation_parent_missing_from_live_completed_portfolio",
                        cpu_seconds=0.0, wall_seconds=0.0,
                    )
                remaining = max(0.05, budget.cpu_cap_seconds - budget.cpu_seconds - 0.05)
                try:
                    with process_cpu_alarm(remaining):
                        return execute_policy_trajectory(
                            runner=runner, module=module, encoder=encoder,
                            output_dir=native_out / f"{arm.lower()}-{attempt_index:02d}",
                            policy=policy, owner_seed=owner_seed, root=root,
                            attempt_index=attempt_index, invocation_seed=(0 if arm == "R" else 1),
                            root_action_index=None if target is not None else root_index,
                            mutation_target=target, parent_trial=parent,
                            cpu_cap_seconds=remaining,
                        )
                except SearchBudgetExhausted:
                    return TrialResult(
                        attempt_index=attempt_index, complete=False,
                        failure_boundary="per_search_process_cpu_watchdog",
                        cpu_seconds=max(0.0, process_tree_cpu_seconds() - search_cpu_start),
                        wall_seconds=max(0.0, time.monotonic() - search_wall_start),
                    )

            def record_attempt_result(trial: TrialResult, root_index: int, attempt_index: int, target: MutationTarget | None) -> None:
                if trial.complete:
                    completed[int(attempt_index)] = trial
                _write_once(RECEIPTS / "attempts" / f"{root_id}-{arm}-{attempt_index:02d}-result.json", {
                    "schema_id": "issue75-stage0-attempt-result-v1",
                    "owner_seed": owner_seed, "root_id": root_id, "arm": arm,
                    "root_action_index": None if target is not None else int(root_index),
                    **_trial_receipt(trial),
                    "mutation_parent_attempt_index": None if target is None else int(target.parent_attempt_index),
                    "mutation_target_step_index": None if target is None else int(target.parent_decision.step_index),
                    "mutation_delivered": target is not None,
                })

            def record_portfolio(rows: Sequence[TrialResult]) -> None:
                retained = {int(row.attempt_index): row for row in rows}
                completed.clear()
                completed.update(retained)
                if arm == "J":
                    joint_portfolio[:] = list(rows)

            try:
                with process_cpu_alarm(32.0):
                    outcome = run_search(
                        arm=arm, study_id=str(config["study_id"]),
                        root_id=root.root_id, invocation_seed=0,
                        root_legal_identities=root.legal_identities,
                        attempt_runner=attempt_runner, budget=budget,
                        attempt_started_recorder=start_attempt,
                        attempt_result_recorder=record_attempt_result,
                        completed_portfolio_recorder=record_portfolio,
                    )
            except SearchBudgetExhausted as exc:
                raise RuntimeError("search watchdog expired between durably recorded native attempts") from exc
            summary = {
                "arm": arm, "status": outcome.status,
                "started_attempts": outcome.started_attempts,
                "completed_trajectories": outcome.completed_trajectories,
                "cpu_seconds": outcome.cpu_seconds,
                "attempt_cpu_seconds": list(outcome.attempt_cpu_seconds),
                "stop_reason": outcome.stop_reason,
                "mutation_attempts_started": outcome.mutation_attempts_started,
                "selected_root_action_sha256": None if outcome.selected_root_identity is None else identity_key(outcome.selected_root_identity),
                "best_completed_attempt_index": outcome.best_completed_attempt_index,
                "wall_seconds": max(0.0, time.monotonic() - search_wall_start),
            }
            _write_once(RECEIPTS / "roots" / f"{root_id}-{arm}-search-result.json", {
                "schema_id": "issue75-stage0-search-result-v1",
                "owner_seed": owner_seed, "root_id": root_id,
                "source_commit": source_commit, "policy_sha256": policy_sha,
                **summary,
                "native_checkpoint_persisted": False, "privileged_projection_persisted": False,
            })
            search_summaries[arm] = summary

        training_payload = {
            "schema_id": "issue75-volatile-stage0-training-data-v1",
            "owner_seed": owner_seed,
            "base_training_data": _safe_trial_payload(base),
            "joint_training_data": [
                _safe_trial_payload(row) for row in sorted(joint_portfolio, key=lambda item: item.attempt_index)
            ],
        }
        payload_sha = hashlib.sha256(canonical(training_payload).encode()).hexdigest()
        root_result = {
            "schema_id": "issue75-stage0-root-result-v1",
            "status": "ROOT_TERMINAL_DATA_CAPTURED" if base.complete else "ROOT_BASE_INCOMPLETE",
            "owner_seed": owner_seed, "root_id": root_id,
            "source_commit": source_commit, "source_lock_sha256_lf": lock_sha,
            "reviewer_comment_id": int(proof["comment_id"]),
            "reviewer_login": str(proof["reviewer_login"]),
            "policy_sha256": policy_sha, "source_prefix_status": "SOURCE_ROOT_MATCHED",
            "base_terminal_complete": bool(base.complete),
            "base_heart_win": bool(base.heart_win) if base.complete else None,
            "base_terminal_floor": float(base.terminal_floor) if base.complete else None,
            "R": search_summaries["R"], "J": search_summaries["J"],
            "training_payload_sha256": payload_sha,
            "training_payload_persisted": False,
            "native_checkpoint_persisted": False, "privileged_projection_persisted": False,
            "formal_training_seed_consumed": False, "formal_evaluation_seed_consumed": False,
            "worker_process_cpu_seconds": max(0.0, process_tree_cpu_seconds() - start_cpu),
            "worker_wall_seconds": max(0.0, time.monotonic() - start_wall),
        }
        _write_once(result_path, root_result)
        sys.stdout.write(WORKER_MARKER + json.dumps(
            {"root_result": root_result, "training_payload": training_payload},
            separators=(",", ":"), allow_nan=False,
        ) + "\n")
        sys.stdout.flush()
        return 0
    except Exception as exc:
        if not sampling_authorized:
            sys.stderr.write(f"ISSUE75_ROOT_AUTHORIZATION_GATE_REJECTED:{type(exc).__name__}\n")
            return 2
        failed = _safe_failure(exc, boundary="root_worker_exception")
        from algorithm import SearchBudgetExhausted
        if isinstance(exc, SearchBudgetExhausted):
            failed["status"] = "RESOURCE_BLOCKED"
        elif type(exc).__name__ == "SourceRootMismatch":
            failed["status"] = "SOURCE_INVALID"
        else:
            failed["status"] = "TECHNICAL_CHAIN_INCOMPLETE"
        failed.update({
            "root_id": root_id,
            "reviewer_comment_id": proof.get("comment_id"),
            "reviewer_login": proof.get("reviewer_login"),
            "source_commit": proof.get("source_commit"),
            "source_lock_sha256_lf": proof.get("source_lock_sha256_lf"),
            "worker_process_cpu_seconds": max(0.0, process_tree_cpu_seconds() - start_cpu),
            "worker_wall_seconds": max(0.0, time.monotonic() - start_wall),
        })
        _write_result_once(result_path, failed)
        sys.stdout.write(WORKER_MARKER + json.dumps(
            {"root_result": _read_json(result_path), "training_payload": None},
            separators=(",", ":"), allow_nan=False,
        ) + "\n")
        sys.stdout.flush()
        return 2


def _parse_worker_stdout(stdout: bytes) -> dict[str, Any] | None:
    for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
        if line.startswith(WORKER_MARKER):
            try:
                value = json.loads(line[len(WORKER_MARKER):])
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None
    return None


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    temp = STUDY / "stage0" / "tmp"
    cache = STUDY / "stage0" / "cache"
    temp.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    for name in ("TMPDIR", "TMP", "TEMP"):
        env[name] = str(temp)
    env["XDG_CACHE_HOME"] = str(cache)
    env["TORCH_HOME"] = str(cache / "torch")
    env["MPLCONFIGDIR"] = str(cache / "matplotlib")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _process_group_cpu_seconds(group_id: int) -> float:
    ticks_per_second = float(os.sysconf("SC_CLK_TCK"))
    total_ticks = 0
    proc = Path("/proc")
    if not proc.is_dir():
        raise RuntimeError("Linux /proc process-tree CPU accounting is required")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="ascii")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        end_comm = stat.rfind(")")
        if end_comm < 0:
            continue
        fields = stat[end_comm + 2:].split()
        # fields start at Linux stat field 3; pgrp is field 5, utime/stime are 14/15.
        if len(fields) < 13:
            continue
        try:
            pgrp = int(fields[2])
            if pgrp == int(group_id):
                total_ticks += int(fields[11]) + int(fields[12])
        except (ValueError, IndexError):
            continue
    return total_ticks / ticks_per_second


def _communicate_with_tree_cpu_cap(
    process: subprocess.Popen[bytes], *, cap: float,
) -> tuple[bytes, bytes, int, float, bool]:
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("root worker pipes are required for bounded execution")
    group_id = int(process.pid)
    captured_out = b""
    captured_err = b""
    peak_tree_cpu = 0.0
    watchdog_triggered = False
    while True:
        try:
            captured_out, captured_err = process.communicate(timeout=0.05)
            break
        except subprocess.TimeoutExpired:
            tree_cpu = _process_group_cpu_seconds(group_id)
            peak_tree_cpu = max(peak_tree_cpu, tree_cpu)
            if tree_cpu >= cap:
                watchdog_triggered = True
                try:
                    os.killpg(group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                captured_out, captured_err = process.communicate()
                break
    peak_tree_cpu = max(peak_tree_cpu, _process_group_cpu_seconds(group_id))
    return captured_out, captured_err, int(process.returncode), peak_tree_cpu, watchdog_triggered


def _record_resource(
    root_id: str,
    *,
    measured_worker_cpu: float | None,
    controller_cpu: float,
    completed: bool,
    watchdog_triggered: bool,
    config: Mapping[str, Any],
    exit_code: int | None,
) -> dict[str, Any]:
    path = _paths(root_id)["resource"]
    if path.exists():
        return _read_json(path)
    cap = float(config["resources"]["stage0_root_worker_process_cpu_hard_seconds"])
    worker_charge = (
        max(0.0, float(measured_worker_cpu))
        if completed and measured_worker_cpu is not None else cap
    )
    record = {
        "schema_id": "issue75-stage0-root-resource-v1",
        "root_id": root_id,
        "worker_cpu_seconds_measured": measured_worker_cpu,
        "worker_cpu_seconds_charged": worker_charge,
        "worker_cpu_seconds_hard_reserve": cap,
        "worker_cpu_hard_cap_exceeded": worker_charge > cap,
        "worker_cpu_watchdog_triggered": bool(watchdog_triggered),
        "controller_process_cpu_seconds": max(0.0, float(controller_cpu)),
        "process_exit_code": exit_code,
        "worker_terminal_result_present": bool(completed),
        "resource_charge_conservative_when_result_missing": not bool(completed),
    }
    _write_once(path, record)
    return record


def _safe_config_preflight(config: Mapping[str, Any]) -> None:
    authorization = config["authorization"]
    if authorization["stage0_nonformal_authorized"] is not True:
        raise RuntimeError("the registered Stage-0 nonformal authorization is absent")
    if authorization["formal_training_enabled"] or authorization["formal_evaluation_enabled"]:
        raise RuntimeError("formal execution is not authorized")
    if authorization["formal_owner_seed_table_present"]:
        raise RuntimeError("formal owner seeds must not be present")
    roots = config["stage0"]["root_ids"]
    if len(roots) != 8 or int(config["stage0"]["root_attempt_limit"]) != 8:
        raise RuntimeError("Stage-0 root population differs from the registered eight-root ceiling")
    resources = config["resources"]
    if int(resources["stage0_root_worker_process_cpu_hard_seconds"]) * len(roots) != int(resources["stage0_root_worker_worst_case_cpu_seconds"]):
        raise RuntimeError("per-root CPU reserves do not match the aggregate Stage-0 worst case")
    if int(resources["stage0_root_and_search_proposal_max_cpu_seconds"]) != 2 * 32 * len(roots):
        raise RuntimeError("R/J proposal CPU cap does not match two 32-second invocations per root")
    search = config["search"]
    if (
        int(search["search_invocation_cpu_cap_seconds"]) != int(SEARCH_CPU_CAP_SECONDS)
        or int(search["search_invocation_started_attempt_cap"]) != int(SEARCH_STARTED_ATTEMPT_CAP)
        or int(search["r_root_trial_cap"]) != int(R_ROOT_TRIAL_CAP)
        or int(search["j_root_trial_cap"]) != int(J_ROOT_TRIAL_CAP)
        or int(search["j_later_mutation_trial_cap"]) != int(J_LATER_MUTATION_TRIAL_CAP)
        or int(search["j_completed_trajectory_portfolio_cap"]) != int(J_COMPLETED_PORTFOLIO_CAP)
    ):
        raise RuntimeError("implemented R/J proposal controls differ from the source-locked config")


def run_stage0() -> dict[str, Any]:
    config, source_lock, lock_sha, source_commit = _load_locked_state()
    _safe_config_preflight(config)
    proof = reviewer_pass_proof()
    from runtime import verify_native_module, verify_source_repositories
    verify_source_repositories(config)
    verify_native_module(config)
    roots = _selected_roots(source_lock)
    expected_ids = list(config["stage0"]["root_ids"])
    if [row["root_id"] for row in roots] != expected_ids:
        raise RuntimeError("source-locked Stage-0 root order differs from the preregistered order")

    stage_start_path = RECEIPTS / "stage0-start.json"
    stage_start = {
        "schema_id": "issue75-stage0-start-v1",
        "source_commit": source_commit,
        "source_lock_sha256_lf": lock_sha,
        "reviewer_comment_id": proof["comment_id"],
        "reviewer_login": proof["reviewer_login"],
        "status": "AUTHORIZED_STAGE0_NONFORMAL_ONLY",
        "debug_root_count": len(roots),
        "formal_training_seed_consumed": False,
        "formal_evaluation_seed_consumed": False,
        "builder_cpu_hard_seconds": int(config["resources"]["builder_process_tree_cpu_hard_seconds"]),
        "reviewer_cpu_protected_seconds": int(config["resources"]["reviewer_protected_cpu_seconds"]),
        "all_in_cpu_hard_seconds": int(config["resources"]["all_in_cpu_hard_seconds"]),
        "native_subset_cpu_hard_seconds": int(config["resources"]["native_root_checkpoint_terminal_cpu_hard_seconds"]),
        "source_preflight_conservative_cpu_reserve_seconds": int(config["resources"]["stage0_source_preflight_conservative_cpu_reserve_seconds"]),
    }
    if stage_start_path.exists():
        previous = _read_json(stage_start_path)
        if any(previous.get(key) != value for key, value in stage_start.items()):
            raise RuntimeError("Stage-0 start receipt belongs to a different source or authorization")
    else:
        _write_once(stage_start_path, stage_start)

    final_report = RECEIPTS / "stage0-report.json"
    if final_report.exists():
        return _read_json(final_report)
    resource_stop_path = RECEIPTS / "stage0-resource-stop.json"
    if resource_stop_path.exists():
        return _read_json(resource_stop_path)

    def record_resource_stop(reason: str, **details: Any) -> dict[str, Any]:
        return _write_result_once(resource_stop_path, {
            "schema_id": "issue75-stage0-resource-stop-v1",
            "status": "RESOURCE_BLOCKED", "reason": reason,
            **details,
            "formal_training_seed_consumed": False,
            "formal_evaluation_seed_consumed": False,
        })

    payloads: dict[int, dict[str, Any]] = {}
    root_summaries: list[dict[str, Any]] = []
    resource_records: list[dict[str, Any]] = []
    worker_script = str(Path(__file__).resolve())
    worker_command = [sys.executable, worker_script, "--root-worker"]
    for expected in roots:
        root_id = str(expected["root_id"])
        owner = int(expected["owner_seed"])
        paths = _paths(root_id)
        if paths["result"].exists():
            live = _current_root_worker_pids(root_id)
            if live:
                return {
                    "schema_id": "issue75-stage0-active-v1", "status": "ACTIVE_ROOT_WORKER",
                    "active_root_id": root_id, "active_pids": live,
                    "root_results_completed": len(root_summaries),
                    "formal_training_seed_consumed": False, "formal_evaluation_seed_consumed": False,
                }
            root_summaries.append(_read_json(paths["result"]))
            if paths["resource"].exists():
                resource_record = _read_json(paths["resource"])
            elif not _current_root_worker_pids(root_id):
                resource_record = _record_resource(
                    root_id, measured_worker_cpu=None, controller_cpu=0.0,
                    completed=False, watchdog_triggered=False,
                    config=config, exit_code=None,
                )
            else:
                resource_record = None
            if resource_record is not None:
                resource_records.append(resource_record)
            if root_summaries[-1].get("status") == "RESOURCE_BLOCKED":
                return record_resource_stop(
                    "resumed_root_result_reports_resource_blocked",
                    active_root_id=root_id, root_resource_records=resource_records,
                )
            # Feature matrices are volatile and never reconstructed by replaying a completed root.
            continue

        if paths["start"].exists():
            live = _current_root_worker_pids(root_id)
            if live:
                return {
                    "schema_id": "issue75-stage0-active-v1", "status": "ACTIVE_ROOT_WORKER",
                    "active_root_id": root_id, "active_pids": live,
                    "root_results_completed": len(root_summaries),
                    "formal_training_seed_consumed": False, "formal_evaluation_seed_consumed": False,
                }
            root_summaries.append(_write_result_once(paths["result"], {
                "schema_id": "issue75-stage0-root-result-v1",
                "status": "INTERRUPTED_WITHOUT_TERMINAL_RECEIPT",
                "owner_seed": owner, "root_id": root_id,
                "source_commit": source_commit, "source_lock_sha256_lf": lock_sha,
                "failure_boundary": "root_worker_exit_without_durable_terminal_result",
                "native_checkpoint_persisted": False, "privileged_projection_persisted": False,
                "formal_training_seed_consumed": False, "formal_evaluation_seed_consumed": False,
            }))
            resource_records.append(_record_resource(
                root_id, measured_worker_cpu=None, controller_cpu=0.0,
                completed=False, watchdog_triggered=False,
                config=config, exit_code=None,
            ))
            continue

        preflight_reserve = int(config["resources"]["stage0_source_preflight_conservative_cpu_reserve_seconds"])
        worker_cap = int(config["resources"]["stage0_root_worker_process_cpu_hard_seconds"])
        projected = preflight_reserve + sum(
            float(row["worker_cpu_seconds_charged"]) + float(row["controller_process_cpu_seconds"])
            for row in resource_records
        ) + worker_cap
        if projected > int(config["resources"]["builder_process_tree_cpu_hard_seconds"]):
            return record_resource_stop(
                "builder_cpu_reservation_would_exceed_hard_cap",
                projected_builder_cpu_seconds=projected,
            )
        native_projected = sum(float(row["worker_cpu_seconds_charged"]) for row in resource_records) + worker_cap
        if native_projected > int(config["resources"]["native_root_checkpoint_terminal_cpu_hard_seconds"]):
            return record_resource_stop(
                "native_subset_reservation_would_exceed_hard_cap",
                projected_native_cpu_seconds=native_projected,
            )

        _write_once(paths["start"], {
            "schema_id": "issue75-stage0-root-start-v1",
            "owner_seed": owner, "root_id": root_id,
            "source_commit": source_commit, "source_lock_sha256_lf": lock_sha,
            "reviewer_comment_id": proof["comment_id"], "reviewer_login": proof["reviewer_login"],
            "worker_process_cpu_hard_seconds": worker_cap,
            "native_root_invocation_caps": {"historical_prefix": 32, "pi0_base": 32, "R": 32, "J": 32},
            "root_proposal_limit": 8,
            "formal_training_seed_consumed": False, "formal_evaluation_seed_consumed": False,
        })
        child_cpu_before = _children_cpu()
        parent_cpu_before = _self_cpu()
        process: subprocess.Popen[bytes] | None = None
        stdout = b""
        exit_code: int | None = None
        process_tree_cpu = 0.0
        worker_cpu_watchdog_triggered = False
        try:
            process = subprocess.Popen(
                [*worker_command, root_id], cwd=ROOT, env=_worker_env(),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
            _write_once(RECEIPTS / "roots" / f"{root_id}-process.json", {
                "schema_id": "issue75-stage0-worker-process-v1",
                "root_id": root_id, "pid": int(process.pid),
                "process_group_id": int(process.pid),
                "worker_command_sha256": hashlib.sha256(canonical(worker_command + [root_id]).encode()).hexdigest(),
            })
            stdout, _stderr, exit_code, process_tree_cpu, worker_cpu_watchdog_triggered = _communicate_with_tree_cpu_cap(
                process, cap=float(worker_cap) - 1.0,
            )
        except BaseException:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
            raise
        child_cpu_after = _children_cpu()
        parent_cpu_after = _self_cpu()
        measured_child_cpu = max(0.0, child_cpu_after - child_cpu_before)
        controller_cpu = max(0.0, parent_cpu_after - parent_cpu_before)
        marker = _parse_worker_stdout(stdout)
        if paths["result"].exists():
            summary = _read_json(paths["result"])
        elif marker and isinstance(marker.get("root_result"), dict):
            summary = _write_result_once(paths["result"], marker["root_result"])
        else:
            summary = _write_result_once(paths["result"], {
                "schema_id": "issue75-stage0-root-result-v1",
                "status": "RESOURCE_BLOCKED" if worker_cpu_watchdog_triggered else "WORKER_FAILED_WITHOUT_TERMINAL_RECEIPT",
                "owner_seed": owner, "root_id": root_id,
                "source_commit": source_commit, "source_lock_sha256_lf": lock_sha,
                "process_exit_code": exit_code,
                "failure_boundary": "worker_missing_safe_result_marker",
                "native_checkpoint_persisted": False, "privileged_projection_persisted": False,
                "formal_training_seed_consumed": False, "formal_evaluation_seed_consumed": False,
            })
        reported_worker_cpu = summary.get("worker_process_cpu_seconds")
        measured_worker_cpu = max(
            measured_child_cpu,
            max(0.0, process_tree_cpu),
            max(0.0, float(reported_worker_cpu)) if reported_worker_cpu is not None else 0.0,
        )
        completed = bool(
            exit_code == 0
            and summary.get("status") in {"ROOT_TERMINAL_DATA_CAPTURED", "ROOT_BASE_INCOMPLETE"}
        )
        resource_record = _record_resource(
            root_id, measured_worker_cpu=measured_worker_cpu,
            controller_cpu=controller_cpu, completed=completed,
            watchdog_triggered=worker_cpu_watchdog_triggered,
            config=config, exit_code=exit_code,
        )
        resource_records.append(resource_record)
        root_summaries.append(summary)
        if (
            worker_cpu_watchdog_triggered
            or resource_record.get("worker_cpu_hard_cap_exceeded")
            or summary.get("status") == "RESOURCE_BLOCKED"
        ):
            return record_resource_stop(
                "root_worker_resource_limit_reached",
                active_root_id=root_id,
                root_resource_records=resource_records,
                root_result=summary,
            )
        if marker and isinstance(marker.get("training_payload"), dict) and completed:
            payload = marker["training_payload"]
            payload_sha = hashlib.sha256(canonical(payload).encode()).hexdigest()
            if payload_sha != summary.get("training_payload_sha256"):
                raise RuntimeError("volatile training payload differs from its durable hash-only root receipt")
            payloads[owner] = payload

    if any(
        row.get("worker_cpu_hard_cap_exceeded") or row.get("worker_cpu_watchdog_triggered")
        for row in resource_records
    ):
        return record_resource_stop(
            "root_worker_process_tree_cpu_cap_stop_triggered",
            root_resource_records=resource_records,
        )
    native_cpu = sum(float(row["worker_cpu_seconds_charged"]) for row in resource_records)
    parent_cpu = sum(float(row["controller_process_cpu_seconds"]) for row in resource_records)
    preflight_reserve = float(config["resources"]["stage0_source_preflight_conservative_cpu_reserve_seconds"])
    builder_charged = preflight_reserve + native_cpu + parent_cpu
    if native_cpu > float(config["resources"]["native_root_checkpoint_terminal_cpu_hard_seconds"]):
        return record_resource_stop(
            "measured_native_subset_cpu_exceeded_hard_cap",
            measured_native_cpu_seconds=native_cpu,
            native_root_checkpoint_terminal_cpu_hard_seconds=int(
                config["resources"]["native_root_checkpoint_terminal_cpu_hard_seconds"]
            ),
            root_resource_records=resource_records,
        )
    if builder_charged > float(config["resources"]["builder_process_tree_cpu_hard_seconds"]):
        return record_resource_stop(
            "measured_builder_process_tree_cpu_exceeded_hard_cap",
            measured_builder_cpu_seconds=builder_charged,
            builder_process_tree_cpu_hard_seconds=int(config["resources"]["builder_process_tree_cpu_hard_seconds"]),
            root_resource_records=resource_records,
        )
    if builder_charged + float(config["resources"]["reviewer_protected_cpu_seconds"]) > float(config["resources"]["all_in_cpu_hard_seconds"]):
        return record_resource_stop(
            "measured_all_in_process_tree_cpu_exceeded_hard_cap",
            measured_builder_cpu_seconds=builder_charged,
            reviewer_cpu_protected_seconds=int(config["resources"]["reviewer_protected_cpu_seconds"]),
            all_in_cpu_hard_seconds=int(config["resources"]["all_in_cpu_hard_seconds"]),
            root_resource_records=resource_records,
        )

    diagnostic: dict[str, Any]
    if len(payloads) != 8 or len({int(value["owner_seed"]) for value in payloads.values()}) != 8:
        diagnostic = {
            "status": "UNAVAILABLE_VOLATILE_TRAINING_PAYLOAD_NOT_RETAINED",
            "available_owner_count": len(payloads), "required_owner_count": 8,
            "diagnostic_optimizer_steps": 0, "synthetic_replay_performed": False,
        }
    else:
        import numpy as np
        import torch
        from learning import (
            OwnerTrainingExample, policy_probe_diagnostics, train_stage0_diagnostic,
        )
        from model import ActionConditionedPolicy, model_digest

        frozen_pi0 = ActionConditionedPolicy(
            dimension=int(config["policy"]["actor_layers"][0]),
            seed=int(config["policy"]["initial_model_seed"]),
            final_weight_std=float(config["policy"]["actor_final_weight_std"]),
        ).eval()
        examples: list[OwnerTrainingExample] = []
        probes: list[np.ndarray] = []
        for owner in sorted(payloads):
            payload = payloads[owner]
            base_raw = payload.get("base_training_data")
            if not isinstance(base_raw, Mapping) or not base_raw.get("complete"):
                continue
            base = _decode_trial(base_raw)
            joint = tuple(
                _decode_trial(row) for row in payload.get("joint_training_data", [])
                if isinstance(row, Mapping) and row.get("complete")
            )
            examples.append(OwnerTrainingExample(
                owner_seed=int(owner), base=base, joint_trajectories=joint,
            ))
            probes.extend(np.asarray(row.candidate_features, dtype="<f4") for row in base.decisions)
        if len(examples) != 8:
            diagnostic = {
                "status": "REAL_RETURN_SIGNAL_UNINFORMATIVE_BASE_TERMINALS_INCOMPLETE",
                "available_complete_base_owner_count": len(examples),
                "required_owner_count": 8,
                "diagnostic_optimizer_steps": 0,
                "synthetic_replay_performed": False,
            }
        else:
            before_sha = model_digest(frozen_pi0)
            trained = train_stage0_diagnostic(frozen_pi0, examples)
            movement = policy_probe_diagnostics(frozen_pi0, trained.model, probes)
            if trained.result_model_digest != model_digest(trained.model):
                raise RuntimeError("diagnostic model digest changed after training")
            diagnostic_dir = STUDY / "stage0" / "diagnostics"
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
            artifact = diagnostic_dir / "pi0-stage0-diagnostic.pt"
            if artifact.exists():
                raise FileExistsError("diagnostic policy artifact already exists and is immutable")
            temporary = artifact.with_name(artifact.name + f".{os.getpid()}.tmp")
            with temporary.open("xb") as stream:
                torch.save(trained.model.state_dict(), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, artifact)
            diagnostic = {
                "status": "CONDITIONAL_FIXED_ROOT_DIAGNOSTIC_ONE_UPDATE",
                "source_policy_sha256": before_sha,
                "updated_policy_sha256": trained.result_model_digest,
                "block_order_seed": trained.block_order_seed,
                "owner_order_sha256": trained.owner_order_sha256,
                "optimizer_steps": len(trained.diagnostics),
                "update": trained.diagnostics[0].__dict__,
                "probe_diagnostics": movement,
                "diagnostic_policy_artifact": artifact.relative_to(STUDY).as_posix(),
                "diagnostic_policy_artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "probe_states_from_native_pi0_trajectories": True,
                "formal_training_seed_consumed": False,
                "formal_evaluation_seed_consumed": False,
            }

    j_mutations = sum(int(row.get("J", {}).get("mutation_attempts_started", 0)) for row in root_summaries)
    j_mutation_complete = 0
    for row in root_summaries:
        root_id = str(row.get("root_id", ""))
        for attempt in range(1, 17):
            result_path = RECEIPTS / "attempts" / f"{root_id}-J-{attempt:02d}-result.json"
            if result_path.is_file() and _read_json(result_path).get("complete"):
                j_mutation_complete += int(bool(_read_json(result_path).get("mutation_delivered")))

    statuses: list[str] = []
    if any(
        (RECEIPTS / "roots" / f"{row.get('root_id')}-prefix-result.json").is_file()
        and _read_json(RECEIPTS / "roots" / f"{row.get('root_id')}-prefix-result.json").get("status") == "SOURCE_INVALID"
        for row in root_summaries
    ):
        statuses.append("SOURCE_INVALID")
    if any(
        row.get("status") == "RESOURCE_BLOCKED"
        or (
            (RECEIPTS / "roots" / f"{row.get('root_id')}-prefix-result.json").is_file()
            and _read_json(RECEIPTS / "roots" / f"{row.get('root_id')}-prefix-result.json").get("status") == "RESOURCE_BLOCKED"
        )
        for row in root_summaries
    ):
        statuses.append("RESOURCE_BLOCKED")
    if any(row.get("status") in {
        "ROOT_BASE_INCOMPLETE", "INCOMPLETE", "WORKER_FAILED_WITHOUT_TERMINAL_RECEIPT",
        "INTERRUPTED_WITHOUT_TERMINAL_RECEIPT",
    } for row in root_summaries):
        statuses.append("TECHNICAL_CHAIN_INCOMPLETE")
    if j_mutations == 0:
        statuses.append("TREATMENT_NOT_DELIVERED")
    if str(diagnostic.get("status", "")).startswith(("UNAVAILABLE", "REAL_RETURN_SIGNAL_UNINFORMATIVE")):
        statuses.append("REAL_RETURN_SIGNAL_UNINFORMATIVE")
    if not statuses:
        statuses.append("TECHNICAL_CHAIN_PROVEN")

    report = {
        "schema_id": "issue75-stage0-report-v1",
        "status": "STAGE0_NONFORMAL_REVIEWABLE_COMPLETE",
        "diagnostic_classifications": statuses,
        "source_commit": source_commit,
        "source_lock_sha256_lf": lock_sha,
        "reviewer_comment_id": proof["comment_id"],
        "reviewer_login": proof["reviewer_login"],
        "locked_root_count": len(roots),
        "root_results": [
            {key: value for key, value in row.items()
             if key not in {"native_checkpoint", "projection", "feature_matrix"}}
            for row in root_summaries
        ],
        "completed_later_mutation_trajectories": j_mutation_complete,
        "diagnostic_training": diagnostic,
        "resource_accounting": {
            "preflight_conservative_reserve_cpu_seconds": preflight_reserve,
            "native_worker_cpu_charged_seconds": native_cpu,
            "controller_cpu_seconds": parent_cpu,
            "builder_cpu_charged_seconds": builder_charged,
            "builder_cpu_hard_cap_seconds": int(config["resources"]["builder_process_tree_cpu_hard_seconds"]),
            "reviewer_cpu_protected_seconds": int(config["resources"]["reviewer_protected_cpu_seconds"]),
            "all_in_cpu_hard_cap_seconds": int(config["resources"]["all_in_cpu_hard_seconds"]),
            "native_subset_hard_cap_seconds": int(config["resources"]["native_root_checkpoint_terminal_cpu_hard_seconds"]),
            "formal_stage1_cpu_reserved_seconds": 0,
        },
        "limitations": [
            "conditional on fixed historical Issue 73 source-policy debug checkpoints; not standard-start on-policy owner evidence",
            "same restored native future RNG stream; only policy sampling stream varies",
            "selected single-sample trajectories are not estimates of public Q or optimal policy value",
            "Stage 0 does not activate formal training or evaluation",
        ],
        "formal_training_seed_consumed": False,
        "formal_evaluation_seed_consumed": False,
        "formal_seed_table_present": False,
        "native_checkpoint_persisted": False,
        "privileged_projection_persisted": False,
    }
    _write_once(RECEIPTS / "stage0-report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-worker", action="store_true")
    parser.add_argument("root_id", nargs="?")
    parser.add_argument("--run-debug", action="store_true")
    args = parser.parse_args()
    if args.root_worker:
        if not args.root_id:
            parser.error("--root-worker requires a locked root ID")
        return _root_worker(str(args.root_id))
    if args.root_id:
        parser.error("root ID is valid only with --root-worker")
    if args.run_debug:
        report = run_stage0()
        print(json.dumps(report, sort_keys=True))
        return 0 if report.get("status") == "STAGE0_NONFORMAL_REVIEWABLE_COMPLETE" else 2
    _, lock, lock_sha, source_commit = _load_locked_state()
    print(json.dumps({
        "status": lock["status"], "source_commit": source_commit,
        "source_lock_sha256_lf": lock_sha,
        "selected_root_count": len(_selected_roots(lock)),
        "formal_training_enabled": lock["formal_training_enabled"],
        "formal_evaluation_enabled": lock["formal_evaluation_enabled"],
        "native_sampling_started": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
