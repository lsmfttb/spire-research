"""Source-locked Stage 1 owner executor for Issue 75.

This module is intentionally study-local. The CLI accepts only phases enabled
by the source-locked package. Formal owner work uses bounded independent
supervisors and write-ahead batch CPU reservations. Artifacts and worker files stay below
``stage1/artifacts``.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from typing import Any, Mapping, Sequence

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
STAGE1 = STUDY / "stage1"
ARTIFACT_ROOT = STAGE1 / "artifacts"
sys.path.insert(0, str(STUDY))

import stage1_budget as budget  # noqa: E402

STUDY_ID = "issue-75-no-expert-oracle"
ARMS = ("P0", "P1", "R", "J", "E")
PROXY_LABEL = "P1_PREFLIGHT_PROXY_ONLY"
WORKER_SCHEMA = "issue75-stage1-worker-job-v1"
UNIT_SCHEMA = "issue75-stage1-unit-result-v1"
FEATURE_SCHEMA = "issue75-stage1-training-features-v1"
RESUME_SCHEMA = "issue75-policy-block-step-checkpoint-v1"
POLICY_DIMENSION = 2048
PREFLIGHT_ROW_RESERVATION = 1_000.0
MAX_FEATURE_ROWS = 512
MAX_TRIAL_DECISIONS = 20_000
BOOTSTRAP_REPLICATES = 20_000
ANALYSIS_REPORT_RESERVATION = 120.0
FORMAL_SCHEDULER_RESERVATION = 120.0
TRAINING_SCHEDULER_CONTINUATION_RESERVATION = 20.0
TRAINING_SCHEDULER_CONTINUATION_PREFIX = "SCHEDULER:TRAIN:CONTINUATION:"
SUPERVISOR_SCHEMA = "issue75-stage1-supervisor-v1"
ACCEPTED_PREFLIGHT_REPORT_SHA256 = "30453842d951a0fbbcd49935ae22a47fce413aafcbd017e61c34a2fa3417e81f"
ACCEPTED_COST_FORECAST_SHA256 = "8933d20699955f5a128eba4ea373df1d0b89f27eb0b4613902c483245ed46c36"
ACCEPTED_PREFLIGHT_DECISION_ID = 6423390920


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_once(path: Path, value: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical(value) + b"\n"
    if path.exists():
        # Git's core.autocrlf can translate checked-out JSON newlines.
        # Normalize only CRLF pairs for comparison; never rewrite evidence.
        if path.read_bytes().replace(b"\r\n", b"\n") != payload:
            raise FileExistsError(f"refusing to replace immutable Stage 1 receipt {path.name}")
        return _sha256(payload)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return _sha256(payload)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _inside_artifacts(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(ARTIFACT_ROOT.resolve(strict=False))
    except ValueError as exc:
        raise ValueError("Stage 1 output must remain under the registered task-worktree artifact root") from exc
    return resolved


def _rows() -> list[dict[str, Any]]:
    value = json.loads((STAGE1 / "seed-table.json").read_text(encoding="utf-8"))
    rows = value.get("rows")
    if not isinstance(rows, list) or len(rows) != 1032:
        raise PermissionError("the locked Stage 1 seed table is incomplete")
    units: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PermissionError("the locked Stage 1 seed table contains a non-object row")
        unit = str(row.get("unit_id", ""))
        if not unit or unit in units:
            raise PermissionError("the locked Stage 1 seed table contains a duplicate/empty unit")
        units.add(unit)
        if row.get("phase") == "preflight":
            if row.get("formal") is not False or row.get("arm") not in ARMS:
                raise PermissionError("a preflight row changed its nonformal/five-arm registration")
        elif row.get("phase") == "training":
            if row.get("formal") is not True or row.get("arm") != "TRAIN":
                raise PermissionError("a training row changed its registered formal status")
        elif row.get("phase") == "heldout":
            if row.get("formal") is not True or row.get("arm") not in ARMS:
                raise PermissionError("a heldout row changed its registered formal status/arm")
        else:
            raise PermissionError("the locked Stage 1 seed table contains an unknown phase")
    return rows


def _unit_rows(phase: str) -> list[dict[str, Any]]:
    expected_phase = {"preflight": "preflight", "train": "training", "evaluate": "heldout"}[phase]
    return [row for row in _rows() if row["phase"] == expected_phase]


def _worker_phase(phase: str, row: Mapping[str, Any]) -> str:
    """Translate CLI aliases once to the canonical registered seed-table phase."""
    expected_phase = {"preflight": "preflight", "train": "training", "evaluate": "heldout"}.get(phase)
    if expected_phase is None or row.get("phase") != expected_phase:
        raise PermissionError("worker phase alias differs from its registered seed-table phase")
    return expected_phase


def _unit_key(unit_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in unit_id)


def _unit_directory(unit_id: str) -> Path:
    return _inside_artifacts(ARTIFACT_ROOT / "units" / _unit_key(unit_id))


def _run_artifact_directory(unit_id: str, run_id: str) -> Path:
    if not run_id.startswith(unit_id + "-"):
        raise PermissionError("owner artifact directory is not scoped to its exact ledger run")
    return _inside_artifacts(_unit_directory(unit_id) / "runs" / run_id)


def _owner_result_path(unit_id: str, run_id: str, phase: str, *, current_package: bool) -> Path:
    if current_package and phase in {"training", "heldout"}:
        return _run_artifact_directory(unit_id, run_id) / "result.json"
    return _unit_directory(unit_id) / "result.json"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path.name}")
    return value


def _locked_models() -> tuple[Any, Any, Any]:
    import torch
    from model import ActionConditionedPolicy, model_digest

    config = _read_json(STUDY / "config.json")
    policy_config = config["policy"]
    initial = ActionConditionedPolicy(
        dimension=POLICY_DIMENSION,
        seed=int(policy_config["initial_model_seed"]),
        final_weight_std=float(policy_config["actor_final_weight_std"]),
    ).eval()
    return torch, ActionConditionedPolicy, (initial, model_digest)


def _model_path(label: str) -> Path:
    if label not in {"pi0", "pi1", "pi2", "pi3", "pi4", "pi-stage0-proxy"}:
        raise ValueError("unknown registered Issue 75 policy checkpoint")
    return _inside_artifacts(ARTIFACT_ROOT / "models" / f"{label}.pt")


def _save_model_once(model: Any, path: Path, digest_fn: Any) -> dict[str, str]:
    import torch

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        state = torch.load(path, map_location="cpu", weights_only=True)
        check = type(model)(dimension=POLICY_DIMENSION, seed=73507501).eval()
        check.load_state_dict(state, strict=True)
        if digest_fn(check) != digest_fn(model):
            raise FileExistsError(f"immutable model checkpoint differs: {path.name}")
    else:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as stream:
                torch.save(model.state_dict(), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"path": path.relative_to(ARTIFACT_ROOT).as_posix(), "sha256": _file_sha256(path), "model_digest": digest_fn(model)}


def _load_model(label: str, *, proxy: bool = False) -> tuple[Any, str, dict[str, Any]]:
    import torch
    from model import ActionConditionedPolicy, model_digest

    config = _read_json(STUDY / "config.json")
    if label == "pi0":
        model = ActionConditionedPolicy(
            dimension=POLICY_DIMENSION,
            seed=int(config["policy"]["initial_model_seed"]),
            final_weight_std=float(config["policy"]["actor_final_weight_std"]),
        ).eval()
        path = _model_path("pi0")
        if path.exists():
            model.load_state_dict(torch.load(path, map_location="cpu", weights_only=True), strict=True)
        else:
            _save_model_once(model, path, model_digest)
        digest = model_digest(model)
        expected_digest = _read_json(STAGE1 / "config.json")["source"]["initial_model_digest"]
        if digest != expected_digest:
            raise PermissionError("existing pi0 checkpoint does not match the registered initial-model digest")
        return model, digest, {"source": "registered_initial_pi0", "checkpoint_sha256": _file_sha256(path)}
    if proxy:
        proxy_record = _read_json(STAGE1 / "preflight-proxy.json")
        relative = str(proxy_record["artifact_path"])
        artifact = (STUDY / relative).resolve(strict=True)
        if not artifact.is_relative_to(STUDY.resolve()) or _file_sha256(artifact) != proxy_record["artifact_sha256"]:
            raise PermissionError("Stage 0 preflight proxy artifact bytes do not match their frozen digest")
        model = ActionConditionedPolicy(
            dimension=POLICY_DIMENSION,
            seed=int(config["policy"]["initial_model_seed"]),
            final_weight_std=float(config["policy"]["actor_final_weight_std"]),
        ).eval()
        state = torch.load(artifact, map_location="cpu", weights_only=True)
        model.load_state_dict(state, strict=True)
        digest = model_digest(model)
        if digest != proxy_record["policy_model_digest"] or proxy_record.get("label") != PROXY_LABEL:
            raise PermissionError("Stage 0 diagnostic model architecture/state digest differs from the locked P1 proxy")
        return model, digest, {
            "source": PROXY_LABEL,
            "artifact_path": relative,
            "artifact_sha256": proxy_record["artifact_sha256"],
            "initial_model_digest": proxy_record["initial_model_digest"],
            "policy_model_digest": digest,
            "formal_use": False,
        }
    path = _model_path(label)
    if not path.is_file():
        raise FileNotFoundError(f"registered policy checkpoint has not been produced: {label}")
    model = ActionConditionedPolicy(
        dimension=POLICY_DIMENSION,
        seed=int(config["policy"]["initial_model_seed"]),
        final_weight_std=float(config["policy"]["actor_final_weight_std"]),
    ).eval()
    model.load_state_dict(torch.load(path, map_location="cpu", weights_only=True), strict=True)
    return model, model_digest(model), {"source": label, "checkpoint_sha256": _file_sha256(path), "formal_use": True}


def _arm_policy(phase: str, arm: str, row: Mapping[str, Any]) -> tuple[str, bool]:
    if arm == "E":
        return "pi0", False
    if phase == "preflight" and arm in {"P1", "R", "J"}:
        return "pi-stage0-proxy", True
    if phase == "preflight" or arm in {"P0", "R", "J"}:
        return ("pi0" if phase == "preflight" or arm == "P0" else "pi4"), False
    if arm == "P1":
        return "pi4", False
    if row.get("phase") == "training":
        return f"pi{int(row['block_index'])}", False
    raise ValueError("no registered policy source exists for this Stage 1 row")


def _pack_trial(trial: Any, safe_trial: Any) -> dict[str, Any]:
    return safe_trial(trial)


def _save_training_features(directory: Path, value: Mapping[str, Any]) -> dict[str, str]:
    path = directory / "training-features.json"
    return {
        "path": path.resolve().relative_to(ARTIFACT_ROOT.resolve()).as_posix(),
        "sha256": _write_once(path, value),
    }


def _decision_from_trace(row: Mapping[str, Any], index: int, *, is_root: bool) -> Any:
    from algorithm import Decision

    features = row.get("candidate_features")
    if features is None:
        raise ValueError("a genuine training decision is missing its candidate feature matrix")
    import numpy as np

    matrix = np.asarray(features, dtype="<f4")
    identities = tuple(dict(item) for item in row.get("legal_action_identities", ()))
    chosen = int(row.get("chosen_index", -1))
    if matrix.ndim != 2 or matrix.shape != (len(identities), POLICY_DIMENSION) or not 0 <= chosen < len(identities):
        raise ValueError("training matrix/action list does not match the actual chosen legal occurrence")
    return Decision(
        step_index=int(row.get("step", index)),
        state_fingerprint=str(row.get("projection_digest", "")),
        legal_identities=identities,
        chosen_index=chosen,
        candidate_features=matrix.copy(),
        is_root=bool(is_root),
        screen=str(row.get("native_screen", row.get("screen", ""))),
    )


def _base_trial(result: Mapping[str, Any], owner_seed: int) -> Any:
    from algorithm import TrialResult

    decision_rows = result.get("_volatile_training_decisions", ())
    decisions = tuple(_decision_from_trace(row, index, is_root=index == 0) for index, row in enumerate(decision_rows))
    terminal = result["terminal"]
    return TrialResult(
        attempt_index=1,
        complete=True,
        heart_win=bool(terminal["heart_victory"]),
        terminal_floor=float(terminal["terminal_floor"]),
        root_identity={"natural_owner_seed": int(owner_seed)},
        decisions=decisions,
        cpu_seconds=float(result.get("cpu_seconds", 0.0)),
        wall_seconds=float(result.get("wall_seconds", 0.0)),
        natural_owner=True,
    )


def _serialize_trial(trial: Any, runtime: Any) -> dict[str, Any]:
    return runtime._safe_trial_payload(trial)


def _decode_trial(payload: Mapping[str, Any]) -> Any:
    import base64
    import numpy as np
    import zlib
    from algorithm import Decision, TrialResult

    complete = payload.get("complete") is True
    common = {
        "attempt_index": int(payload["attempt_index"]),
        "complete": complete,
        "cpu_seconds": payload.get("cpu_seconds"),
        "wall_seconds": payload.get("wall_seconds"),
    }
    if not complete:
        trace = tuple(
            (str(row["file"]), str(row["function"]), int(row["line"]))
            for row in payload.get("failure_trace", ())
        )
        return TrialResult(
            **common,
            failure_boundary=payload.get("failure_boundary"),
            failure_trace=trace,
            failure_detail=payload.get("failure_detail"),
        )
    decision_rows = payload.get("decisions", ())
    if not isinstance(decision_rows, list) or len(decision_rows) > MAX_TRIAL_DECISIONS:
        raise ValueError("stored trajectory has an invalid decision count")
    decisions = []
    for index, row in enumerate(decision_rows):
        shape = row.get("candidate_features_shape")
        if not isinstance(shape, list) or len(shape) != 2 or not 1 <= int(shape[0]) <= MAX_FEATURE_ROWS or int(shape[1]) != POLICY_DIMENSION:
            raise ValueError("stored training feature shape differs from the locked model input")
        compressed = base64.b64decode(row["candidate_features_f32le_zlib_base64"], validate=True)
        matrix_bytes = zlib.decompress(compressed)
        expected_size = int(shape[0]) * POLICY_DIMENSION * 4
        if len(matrix_bytes) != expected_size or _sha256(matrix_bytes) != row.get("candidate_features_sha256"):
            raise ValueError("stored compressed training features fail their size/hash check")
        matrix = np.frombuffer(matrix_bytes, dtype="<f4").reshape(tuple(map(int, shape))).copy()
        if not np.isfinite(matrix).all():
            raise ValueError("stored training feature matrix contains non-finite values")
        hashes = row.get("legal_identity_sha256", ())
        if len(hashes) != int(shape[0]) or any(not isinstance(item, str) or len(item) != 64 for item in hashes):
            raise ValueError("stored legal occurrence hashes do not match feature rows")
        chosen = int(row["chosen_index"])
        if not 0 <= chosen < len(hashes):
            raise ValueError("stored chosen index is not a current legal occurrence")
        decisions.append(Decision(
            step_index=int(row["step_index"]),
            state_fingerprint=str(row["state_fingerprint"]),
            legal_identities=tuple({"identity_sha256": item} for item in hashes),
            chosen_index=chosen,
            candidate_features=matrix,
            is_root=bool(row.get("is_root", index == 0)),
        ))
    root_hash = str(payload.get("root_identity_sha256", ""))
    if len(root_hash) != 64:
        raise ValueError("stored complete trajectory lacks a root identity digest")
    return TrialResult(
        **common,
        heart_win=bool(payload["heart_win"]),
        terminal_floor=float(payload["terminal_floor"]),
        root_identity={"identity_sha256": root_hash},
        decisions=tuple(decisions),
        native_rng_source=str(payload.get("native_rng_source", "restored_checkpoint_native_rng_same_future_stream")),
        natural_owner=bool(payload.get("natural_owner", False)),
        search_root_id=None if payload.get("search_root_id") is None else str(payload["search_root_id"]),
    )


def _training_example_from_payload(value: Mapping[str, Any]) -> Any:
    from learning import OwnerTrainingExample

    base = _decode_trial(value["base"])
    joint = tuple(_decode_trial(item) for item in value.get("joint_trajectories", ()))
    return OwnerTrainingExample(owner_seed=int(value["owner_seed"]), base=base, joint_trajectories=joint)


def _lock_context(path: Path):
    @contextmanager
    def held():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        except BlockingIOError as exc:
            raise RuntimeError("another Stage 1 process tree currently owns the study lock") from exc
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    return held()


def _event_state(ledger: Any) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    return budget._state(ledger.read_events())


def _existing_unit(ledger: Any, unit_id: str) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    runs, units = _event_state(ledger)
    history = units.get(unit_id, [])
    if not history:
        return None, None, None
    run_id = history[-1]
    run = runs[run_id]
    return run_id, run["reservation"], run["finish"]


def _current_update_reservation(
    ledger: Any, *, unit_id: str, run_id: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Bind an update worker to its live reservation and the exact resumable predecessor."""
    reservation = ledger.require_open_reservation(run_id=run_id, unit_id=unit_id)
    if (
        reservation.get("run_id") != run_id
        or reservation.get("unit_kind") != "training_update"
        or reservation.get("role") != "builder"
    ):
        raise PermissionError("update worker does not own the exact live Builder update reservation")
    runs, units = _event_state(ledger)
    history = units.get(unit_id, [])
    if not history or history[-1] != run_id:
        raise PermissionError("update worker reservation is not the latest unit ledger event")

    previous_run_id = reservation.get("previous_run_id")
    if (
        unit_id == "UPDATE:0"
        and reservation.get("repair_reason") == budget.UPDATE0_RETRY_REASON
        and reservation.get("checkpoint_sha256") is None
    ):
        info = ledger.validate_update0_retry_state()
        if (
            previous_run_id != info.get("retry_previous_run_id")
            or any(reservation.get(key) != info.get(key) for key in (
                "repair_reason", "repair_manifest_sha256", "repair_predecessor_run_id",
                "repair_predecessor_receipt_sha256",
            ))
        ):
            raise PermissionError("UPDATE:0 retry does not match the exact failed predecessor chain")
        return None, None
    if previous_run_id is None:
        if len(history) != 1 or reservation.get("checkpoint_sha256") is not None:
            raise PermissionError("new policy update reservation has unexpected resume metadata")
        return None, None

    if len(history) < 2 or history[-2] != previous_run_id:
        raise PermissionError("update resume does not name the immediately preceding unit run")
    previous = runs.get(previous_run_id)
    if (
        previous is None
        or previous["reservation"].get("unit_id") != unit_id
        or previous["reservation"].get("unit_kind") != "training_update"
        or previous["reservation"].get("role") != "builder"
    ):
        raise PermissionError("update resume predecessor is not the same registered Builder unit")
    previous_finish = previous["finish"]
    if previous_finish is None or previous_finish.get("status") != "incomplete":
        raise budget.ResourceBlocked("policy update predecessor is not an incomplete checkpoint run")
    checkpoint_sha256 = previous_finish.get("checkpoint_sha256")
    if (
        not _valid_digest(checkpoint_sha256, 64)
        or reservation.get("checkpoint_sha256") != checkpoint_sha256
    ):
        raise budget.CheckpointMismatch("current update reservation differs from its predecessor checkpoint")
    return previous_run_id, previous_finish


def _unit_accounting(ledger: Any, unit_id: str) -> dict[str, Any]:
    run_id, reservation, finish = _existing_unit(ledger, unit_id)
    if run_id is None or reservation is None:
        return {"unit_id": unit_id, "status": "not_started", "charged_cpu_seconds": 0.0}
    return {
        "unit_id": unit_id,
        "run_id": run_id,
        "status": "reservation_open" if finish is None else str(finish["status"]),
        "reserved_cpu_seconds": float(reservation["reserved_cpu_seconds"]),
        "measured_process_tree_cpu_seconds": None if finish is None else float(finish["measured_cpu_seconds"]),
        "charged_cpu_seconds": budget._charge({"reservation": reservation, "finish": finish}),
        "started_attempts": 0 if finish is None else int(finish.get("started_attempts", 0)),
        "checkpoint_sha256": None if finish is None else finish.get("checkpoint_sha256"),
        "result_sha256": None if finish is None else finish.get("result_sha256"),
    }


def _finish_failed_unit(
    ledger: Any, *, run_id: str, unit_id: str, directory: Path, measured: float,
    cap: float, boundary: str, started: float, status: str = "failed",
    checkpoint_sha256: str | None = None,
) -> None:
    if status not in {"failed", "incomplete"}:
        raise ValueError("failed-unit finalization requires a failed or incomplete state")
    overrun = measured > cap
    _write_once(directory / f"failure-{run_id}.json", {
        "schema_id": UNIT_SCHEMA,
        "status": "FAILED" if overrun or status == "failed" else "INCOMPLETE",
        "unit_id": unit_id,
        "run_id": run_id,
        "boundary": boundary,
        "measured_cpu_seconds": measured,
        "reserved_cpu_seconds": cap,
        "checkpoint_sha256": checkpoint_sha256,
        "wall_seconds": max(0.0, time.monotonic() - started),
    })
    try:
        ledger.finish(
            run_id=run_id, measured_cpu_seconds=measured, status=status,
            checkpoint_sha256=checkpoint_sha256 if status == "incomplete" else None,
        )
    except budget.BudgetOverrun as exc:
        raise budget.ResourceBlocked(
            f"unit {unit_id} exceeded its immutable process-tree CPU reservation; preserve and stop"
        ) from exc


def _preflight_complete(ledger: Any) -> bool:
    runs, units = _event_state(ledger)
    expected = [row["unit_id"] for row in _unit_rows("preflight")]
    return all(
        unit in units
        and runs[units[unit][-1]]["finish"] is not None
        and runs[units[unit][-1]]["finish"].get("status") == "succeeded"
        for unit in expected
    )


def _formal_unit_ids() -> list[str]:
    return (
        [row["unit_id"] for row in _rows() if row["phase"] in {"training", "heldout"}]
        + [f"UPDATE:{index}" for index in range(4)]
        + ["SCHEDULER:TRAIN", "SCHEDULER:EVALUATE", "REPORT:HELDOUT"]
    )


def _forecast_estimates(ledger: Any) -> tuple[dict[str, float], dict[str, Any]]:
    cost = _read_json(STUDY / "planning" / "stage1-cost-model.json")
    projection = cost["stage1_builder_projection_cpu_seconds"]
    registered = cost["registered_workload"]
    if (
        projection.get("proposed_builder_hard_cap") != budget.BUILDER_LIMIT
        or projection.get("protected_reviewer_reserve") != budget.REVIEWER_LIMIT
        or registered.get("maximum_search_cpu_seconds") != 108544.0
    ):
        raise PermissionError("the frozen Stage 1 cost model no longer matches the budget code")
    runs, _ = _event_state(ledger)
    preflight_charge = sum(
        budget._charge(run) for run in runs.values()
        if run["reservation"].get("unit_id", "").startswith("PREFLIGHT:")
        and run["reservation"].get("role") == "builder"
    )
    engineering = float(projection["engineering_allowance"]) - preflight_charge
    if engineering < 0:
        raise budget.ResourceBlocked("preflight actual charge exceeded the registered engineering allowance")
    search_per_train = 4 * float(registered["search_cpu_cap_per_invocation"])
    search_per_heldout = 7 * float(registered["search_cpu_cap_per_invocation"])
    owner_count = int(registered["training_owners"]) + int(registered["heldout_scenarios"])
    nonsearch = float(projection["nonsearch_root_bootstrap_p95"])
    train_guard = float(projection["controller_training_allocation_guard"])
    owner_nonsearch = (nonsearch - train_guard) / owner_count
    unit_ids = _formal_unit_ids()
    allowance = engineering + float(projection["rounding_remainder"]) - ANALYSIS_REPORT_RESERVATION
    owner_and_update_count = len(unit_ids) - 3
    legacy_owner_allowance = allowance * owner_and_update_count / (owner_and_update_count + 1)
    scheduler_reservations = 2.0 * FORMAL_SCHEDULER_RESERVATION
    owner_update_allowance = legacy_owner_allowance - scheduler_reservations
    if owner_update_allowance < 0:
        raise budget.ResourceBlocked("registered engineering allowance cannot fund scheduler accounting")
    per_unit_allowance = owner_update_allowance / owner_and_update_count
    estimates: dict[str, float] = {}
    for row in _rows():
        if row["phase"] == "training":
            estimates[row["unit_id"]] = search_per_train + owner_nonsearch + per_unit_allowance
        elif row["phase"] == "heldout":
            searches = search_per_heldout if row["arm"] in {"R", "J"} else 0.0
            estimates[row["unit_id"]] = searches + owner_nonsearch + per_unit_allowance
    for block in range(4):
        estimates[f"UPDATE:{block}"] = train_guard / 4.0 + per_unit_allowance
    estimates["SCHEDULER:TRAIN"] = FORMAL_SCHEDULER_RESERVATION
    estimates["SCHEDULER:EVALUATE"] = FORMAL_SCHEDULER_RESERVATION
    estimates["REPORT:HELDOUT"] = ANALYSIS_REPORT_RESERVATION
    if set(estimates) != set(unit_ids):
        raise AssertionError("cost forecast unit IDs differ from the locked formal workload")
    summary = {
        "schema_id": "issue75-stage1-cost-forecast-v1",
        "status": "FITS_IF_LEDGER_ACCEPTS",
        "source": "locked Stage 1 cost-model caps and current exact-source ledger charges",
        "maximum_search_cpu_seconds": float(projection["search_hard_cap"]),
        "operational_search_stop_margin_cpu_seconds": 2.0,
        "nonsearch_root_bootstrap_p95_cpu_seconds": nonsearch,
        "training_update_guard_cpu_seconds": train_guard,
        "engineering_allowance_initial_cpu_seconds": float(projection["engineering_allowance"]),
        "preflight_actual_cpu_seconds": preflight_charge,
        "remaining_engineering_allowance_cpu_seconds": engineering,
        "rounding_remainder_cpu_seconds": float(projection["rounding_remainder"]),
        "formal_unit_count": owner_count + 4,
        "forecast_unit_count": len(unit_ids),
        "scheduler_unit_count": 2,
        "scheduler_reservation_per_phase_cpu_seconds": FORMAL_SCHEDULER_RESERVATION,
        "heldout_report_analysis_reservation_cpu_seconds": ANALYSIS_REPORT_RESERVATION,
        "forecast_builder_cpu_seconds": sum(estimates.values()),
        "projected_total_builder_cpu_seconds": preflight_charge + sum(estimates.values()),
        "builder_hard_cap_cpu_seconds": budget.BUILDER_LIMIT,
        "reviewer_protected_cpu_seconds": budget.REVIEWER_LIMIT,
        "aggregate_hard_cap_cpu_seconds": budget.ALL_IN_LIMIT,
        "cost_model_sha256_lf": budget.normalized_file_sha256(STUDY / "planning" / "stage1-cost-model.json"),
        "limitations": list(cost.get("limitations", ())),
    }
    return estimates, summary


def _accepted_preflight_evidence() -> tuple[dict[str, Any], dict[str, Any], str, str]:
    config = _read_json(STAGE1 / "config.json")
    acceptance = config.get("preflight_protocol", {}).get("accepted_evidence")
    expected = {
        "planner_decision_comment_id": ACCEPTED_PREFLIGHT_DECISION_ID,
        "scope": "nonformal-feasibility-only-with-prospective-hard-caps",
        "preflight_report_path": "stage1/artifacts/preflight-report-review-repair.json",
        "preflight_report_sha256_lf": ACCEPTED_PREFLIGHT_REPORT_SHA256,
        "cost_forecast_path": "stage1/artifacts/cost-forecast-review-repair.json",
        "cost_forecast_sha256_lf": ACCEPTED_COST_FORECAST_SHA256,
        "reported_over_cap_search_invocations": 82,
        "reported_search_invocation_count": 112,
        "reported_aggregate_search_cpu_excess_seconds": 11.5538,
        "reported_maximum_search_cpu_seconds": 32.503407,
    }
    if acceptance != expected:
        raise PermissionError("formal protocol is not bound to the accepted immutable preflight evidence")
    report_path = STUDY / expected["preflight_report_path"]
    forecast_path = STUDY / expected["cost_forecast_path"]
    if not report_path.is_file() or not forecast_path.is_file():
        raise PermissionError("accepted immutable preflight and affordability reports are absent")
    report_sha = budget.normalized_file_sha256(report_path)
    forecast_sha = budget.normalized_file_sha256(forecast_path)
    if report_sha != ACCEPTED_PREFLIGHT_REPORT_SHA256 or forecast_sha != ACCEPTED_COST_FORECAST_SHA256:
        raise PermissionError("accepted preflight artifacts changed after Planner review")
    report = _read_json(report_path)
    forecast = _read_json(forecast_path)
    if (
        report.get("status") != "PREFLIGHT_COMPLETE_PENDING_INDEPENDENT_REVIEW"
        or report.get("scope") != "STUDY_ONLY"
        or report.get("registered_rows") != 40
        or report.get("completed_rows") != 40
        or report.get("terminal_rows") != 40
        or report.get("formal_seeds_consumed") != 0
        or report.get("incomplete_units") != []
        or len(report.get("episode_treatment_delivered_units", [])) != 8
        or len(report.get("root_treatment_not_delivered", [])) != 7
        or forecast.get("status") != "PENDING_INDEPENDENT_AFFORDABILITY_REVIEW"
        or forecast.get("affordability_acceptance") != "PENDING_INDEPENDENT_REVIEW"
        or forecast.get("formal_execution_enabled") is not False
        or forecast.get("source_lock_sha256") != "c195b4fcbc1ee1754a5023bc6bfcde8e9a74cd4837135fcb0bb6d922186ee219"
        or forecast.get("forecast_builder_cpu_seconds") != 161896.17019694686
        or forecast.get("preflight_actual_cpu_seconds") != 8077.011297
        or forecast.get("projected_total_builder_cpu_seconds") != 169973.18149394685
        or forecast.get("builder_hard_cap_cpu_seconds") != budget.BUILDER_LIMIT
        or forecast.get("reviewer_protected_cpu_seconds") != budget.REVIEWER_LIMIT
        or forecast.get("aggregate_hard_cap_cpu_seconds") != budget.ALL_IN_LIMIT
    ):
        raise budget.ResourceBlocked("accepted preflight reports do not match the reviewed bounded-risk decision")
    return report, forecast, report_sha, forecast_sha


def _next_training_reservation(
    ledger: Any, estimates: Mapping[str, float],
) -> tuple[str, float]:
    runs, units = _event_state(ledger)
    ledger.validate_block0_phase_repair_state()
    training_rows = _unit_rows("train")
    for block in range(4):
        for row in training_rows:
            if int(row["block_index"]) != block:
                continue
            unit_id = str(row["unit_id"])
            if block == 0:
                history = units.get(unit_id, [])
                expected_history_length = _block0_manifest_history_length(unit_id)
                if len(history) == expected_history_length:
                    ledger.block0_phase_repair_info(unit_id=unit_id)
                    return unit_id, float(estimates[unit_id])
                if len(history) != expected_history_length + 1:
                    raise budget.ResourceBlocked(f"block-0 repair lineage is malformed: {unit_id}")
                last = runs[history[-1]]
                if last["finish"] is None or last["finish"].get("status") != "succeeded":
                    raise budget.ResourceBlocked(f"block-0 replacement is unresolved or failed: {unit_id}")
                continue
            history = units.get(unit_id, [])
            if not history or runs[history[-1]]["finish"] is None or runs[history[-1]]["finish"].get("status") != "succeeded":
                return unit_id, float(estimates[unit_id])
        unit_id = f"UPDATE:{block}"
        history = units.get(unit_id, [])
        if not history:
            return unit_id, float(estimates[unit_id])
        finish = runs[history[-1]]["finish"]
        if finish is None or finish.get("status") == "incomplete":
            return unit_id, float(estimates[unit_id])
        if finish.get("status") == "failed":
            if block == 0:
                return unit_id, float(estimates[unit_id])
            raise budget.ResourceBlocked(f"failed optimizer update is immutable: {unit_id}")
    for row in _unit_rows("evaluate"):
        unit_id = str(row["unit_id"])
        history = units.get(unit_id, [])
        if not history or runs[history[-1]]["finish"] is None or runs[history[-1]]["finish"].get("status") != "succeeded":
            return unit_id, float(estimates[unit_id])
    for unit_id in ("SCHEDULER:EVALUATE", "REPORT:HELDOUT"):
        history = units.get(unit_id, [])
        if not history or runs[history[-1]]["finish"] is None or runs[history[-1]]["finish"].get("status") != "succeeded":
            return unit_id, float(estimates[unit_id])
    raise budget.ResourceBlocked("no unfinished registered work remains for another reservation")


def _block0_manifest_history_length(unit_id: str) -> int:
    value = _read_json(STAGE1 / "block0-repair-lineage.json")
    owner = next((item for item in value.get("owners", ()) if item.get("unit_id") == unit_id), None)
    if owner is None:
        raise PermissionError("block-0 owner is absent from the locked correction lineage")
    return len(owner["history"])


def _formal_forecast(ledger: Any, *, phase: str) -> dict[str, Any]:
    if not _preflight_complete(ledger):
        raise PermissionError("formal execution requires all 40 preflight owner-arm rows to be durably successful")
    report, forecast, report_sha, forecast_sha = _accepted_preflight_evidence()
    estimates, dynamic = _forecast_estimates(ledger)
    next_unit, next_cap = _next_training_reservation(ledger, estimates)
    scheduler_plan = _training_scheduler_plan(ledger, estimates) if phase == "training" else None
    scheduler_new_cap = (
        float(scheduler_plan["cap"])
        if scheduler_plan is not None and scheduler_plan["action"] == "reserve" else 0.0
    )
    runs, _ = _event_state(ledger)
    charges = ledger._charges(runs)
    builder_after_next = float(charges["builder"]) + next_cap + scheduler_new_cap
    all_in_after_next = builder_after_next + max(budget.REVIEWER_LIMIT, float(charges["reviewer"]))
    if builder_after_next > budget.BUILDER_LIMIT or all_in_after_next > budget.ALL_IN_LIMIT:
        raise budget.ResourceBlocked("the next bounded operation cannot fit within the unchanged live hard caps")
    return {
        "schema_id": "issue75-formal-continuation-forecast-v1",
        "status": "NEXT_RESERVATION_FITS",
        "package_commit": ledger.binding.package_commit,
        "source_lock_sha256": ledger.binding.source_lock_sha256,
        "scope": "STUDY_ONLY",
        "phase": phase,
        "registered_training_owners": 512,
        "registered_heldout_owner_arm_rows": 480,
        "registered_update_units": 4,
        "registered_heldout_report_units": 1,
        "preflight_report_sha256": report_sha,
        "cost_forecast_sha256": forecast_sha,
        "accepted_planner_decision_comment_id": ACCEPTED_PREFLIGHT_DECISION_ID,
        "accepted_static_forecast_status": forecast["status"],
        "static_projection_cpu_seconds_advisory_only": dynamic["projected_total_builder_cpu_seconds"],
        "static_projection_with_scheduler_continuation_cpu_seconds_advisory_only": (
            float(dynamic["projected_total_builder_cpu_seconds"]) + scheduler_new_cap
        ),
        "next_unit_id": next_unit,
        "next_unit_reservation_cpu_seconds": next_cap,
        "training_scheduler_plan": scheduler_plan,
        "training_scheduler_new_reservation_cpu_seconds": scheduler_new_cap,
        "builder_cpu_charged_including_open_reservations": float(charges["builder"]),
        "reviewer_cpu_charged_including_open_reservations": float(charges["reviewer"]),
        "builder_after_next_cpu_seconds": builder_after_next,
        "all_in_after_next_cpu_seconds": all_in_after_next,
        "builder_hard_cap_cpu_seconds": budget.BUILDER_LIMIT,
        "reviewer_protected_cpu_seconds": budget.REVIEWER_LIMIT,
        "aggregate_hard_cap_cpu_seconds": budget.ALL_IN_LIMIT,
        "full_cohort_projection_is_not_a_resource_exhaustion_finding": True,
    }


def _preflight_treatment_diagnostics(
    unit_id: str, arm: str, searches: Sequence[Mapping[str, Any]],
) -> tuple[bool | None, list[dict[str, Any]]]:
    root_gaps = [
        {
            "unit_id": unit_id,
            "root_id": item.get("root_id"),
            "root_index": item.get("root_index"),
            "status": "TREATMENT_NOT_DELIVERED",
        }
        for item in searches
        if item.get("status") == "TREATMENT_NOT_DELIVERED"
    ]
    if arm != "J":
        return None, root_gaps
    delivered = any(
        mutation.get("complete_terminal") is True
        and mutation.get("alternative_differs_from_parent_action") is True
        and type(mutation.get("legal_occurrence_count")) is int
        and mutation["legal_occurrence_count"] >= 2
        and type(mutation.get("trajectory_decision_count")) is int
        and mutation["trajectory_decision_count"] > 0
        for search in searches
        for mutation in search.get("mutation_attempts", ())
    )
    return delivered, root_gaps


def _preflight_reports(ledger: Any) -> dict[str, Any]:
    reports = []
    treatment_not_delivered = []
    root_treatment_not_delivered = []
    incomplete = []
    runs, units = _event_state(ledger)
    for row in _unit_rows("preflight"):
        unit = row["unit_id"]
        history = units.get(unit, [])
        accounting = _unit_accounting(ledger, unit)
        if accounting["status"] != "succeeded":
            incomplete.append(unit)
        result_path = _unit_directory(unit) / "result.json"
        if not history or not result_path.is_file():
            continue
        run = runs[history[-1]]
        if accounting["status"] == "succeeded":
            result = _existing_unit_receipt(ledger, unit)
            if result is None:
                raise PermissionError(f"successful preflight row lacks its bound result receipt: {unit}")
        else:
            result = _read_json(result_path)
        episode_treatment_delivered, root_gaps = _preflight_treatment_diagnostics(
            unit, str(row["arm"]), result.get("search", []),
        )
        root_treatment_not_delivered.extend(root_gaps)
        reports.append({
            "unit_id": unit,
            "ledger_status": None if run["finish"] is None else run["finish"]["status"],
            "result_sha256": _file_sha256(result_path),
            "arm": row["arm"],
            "owner_seed": row["owner_seed"],
            "terminal_floor": result.get("terminal_floor") if accounting["status"] == "succeeded" else None,
            "heart_victory": result.get("heart_victory") if accounting["status"] == "succeeded" else None,
            "search": result.get("search", []),
            "episode_treatment_delivered": episode_treatment_delivered,
            "policy_source": result.get("policy_source"),
            "model_digest": result.get("model_digest"),
            "native_source_commit": result.get("native_source_commit"),
            "stsr_source_commit": result.get("stsr_source_commit"),
            "native_module_sha256": result.get("native_module_sha256"),
            "accounting": accounting,
        })
        if (
            accounting["status"] == "succeeded"
            and row["arm"] == "J"
            and episode_treatment_delivered is False
        ):
            treatment_not_delivered.append(unit)
    return {
        "rows": reports,
        "incomplete": incomplete,
        "treatment_not_delivered": treatment_not_delivered,
        "root_treatment_not_delivered": root_treatment_not_delivered,
    }


def _write_preflight_reports(ledger: Any) -> dict[str, Any]:
    summary = _preflight_reports(ledger)
    status = "PREFLIGHT_COMPLETE_PENDING_INDEPENDENT_REVIEW"
    if summary["incomplete"]:
        status = "PREFLIGHT_INCOMPLETE_NO_FORMAL"
    elif summary["treatment_not_delivered"]:
        status = "PREFLIGHT_TREATMENT_NOT_DELIVERED_NO_FORMAL"
    report = {
        "schema_id": "issue75-stage1-40-row-preflight-report-v1",
        "study_id": STUDY_ID,
        "status": status,
        "package_commit": ledger.binding.package_commit,
        "source_lock_sha256": ledger.binding.source_lock_sha256,
        "scope": "STUDY_ONLY",
        "registered_rows": 40,
        "completed_rows": len(summary["rows"]),
        "terminal_rows": sum(item["terminal_floor"] is not None for item in summary["rows"]),
        "formal_seeds_consumed": 0,
        "preflight_rows_are_not_efficacy_evidence": True,
        "p1_policy_label": PROXY_LABEL,
        "treatment_not_delivered_units": summary["treatment_not_delivered"],
        "root_treatment_not_delivered": summary["root_treatment_not_delivered"],
        "incomplete_units": summary["incomplete"],
        "units": summary["rows"],
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_once(ARTIFACT_ROOT / "preflight-report.json", report)
    if status != "PREFLIGHT_COMPLETE_PENDING_INDEPENDENT_REVIEW":
        raise budget.ResourceBlocked(status)
    estimates, forecast = _forecast_estimates(ledger)
    try:
        ledger_result = ledger.forecast(
            expected_unit_ids=_formal_unit_ids(),
            future_builder_cpu_seconds=estimates,
            allowed_completed_unit_ids=[row["unit_id"] for row in _unit_rows("preflight")],
        )
        forecast["ledger"] = ledger_result
        forecast["status"] = "FITS_IF_LEDGER_ACCEPTS"
    except budget.ResourceBlocked as exc:
        forecast["status"] = "RESOURCE_BLOCKED"
        forecast["reason"] = str(exc)[:2048]
    _write_once(ARTIFACT_ROOT / "cost-forecast.json", forecast)
    if forecast["status"] != "FITS_IF_LEDGER_ACCEPTS":
        raise budget.ResourceBlocked("full registered cohort forecast is resource blocked")
    report["report_sha256"] = _file_sha256(ARTIFACT_ROOT / "preflight-report.json")
    forecast["forecast_sha256"] = _file_sha256(ARTIFACT_ROOT / "cost-forecast.json")
    return {"status": status, "report": report, "cost_forecast": forecast}


class _ReadOnlyHistoricalLedger:
    """Read existing source-bound events without rebinding or mutating the ledger."""

    def __init__(self, binding: Any, events: Sequence[Mapping[str, Any]]):
        self.binding = binding
        self._events = [dict(event) for event in events]

    def read_events(self):
        return [dict(event) for event in self._events]

    def _tx(self, operation):
        result, append = operation(self.read_events())
        if append is not None:
            raise PermissionError("receipt reconciliation cannot append to the historical ledger")
        return result

    _charges = staticmethod(budget.Stage1Ledger._charges)
    forecast = budget.Stage1Ledger.forecast


def _reconcile_preflight_receipts() -> dict[str, Any]:
    """Derive corrected summaries from the completed 40-row receipts; launch no games."""
    global ARTIFACT_ROOT
    budget.verify_stage1_package()
    source_report_path = ARTIFACT_ROOT / "preflight-report.json"
    events_path = ARTIFACT_ROOT / "ledger" / "events.jsonl"
    if not source_report_path.is_file() or not events_path.is_file():
        raise PermissionError("receipt reconciliation requires the retained preflight report and ledger")
    repair_commit = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=ROOT,
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()

    def source_blob(path: Path) -> bytes:
        relative = path.relative_to(ROOT).as_posix()
        return subprocess.run(
            ["git", "show", f"{repair_commit}:{relative}"], cwd=ROOT,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout

    source_report_bytes = source_blob(source_report_path)
    if source_report_bytes != source_report_path.read_bytes().replace(b"\r\n", b"\n"):
        raise PermissionError("retained preflight report differs from its committed receipt bytes")
    source_report = json.loads(source_report_bytes)
    if source_report.get("status") != "PREFLIGHT_TREATMENT_NOT_DELIVERED_NO_FORMAL":
        raise PermissionError("receipt reconciliation only accepts the reviewed immutable TND preflight")
    source_report_sha256 = hashlib.sha256(source_report_bytes).hexdigest()
    events_bytes = source_blob(events_path)
    if events_bytes != events_path.read_bytes().replace(b"\r\n", b"\n"):
        raise PermissionError("historical ledger differs from its committed receipt bytes")
    events = [
        json.loads(line)
        for line in events_bytes.decode("utf-8").splitlines()
        if line.strip()
    ]
    runs, units = budget._state(events)
    binding = budget.PackageBinding(
        str(source_report.get("package_commit", "")),
        str(source_report.get("source_lock_sha256", "")),
    )
    if not binding.package_commit or not binding.source_lock_sha256:
        raise PermissionError("source report lacks its original package and lock binding")
    if any(
        event.get("package_commit") != binding.package_commit
        or event.get("source_lock_sha256") != binding.source_lock_sha256
        for event in events
    ):
        raise PermissionError("historical ledger events do not match the retained report binding")
    expected_units = [str(row["unit_id"]) for row in _unit_rows("preflight")]
    if len(expected_units) != 40 or set(units) != set(expected_units) or len(runs) != 40:
        raise PermissionError("historical ledger does not contain exactly the registered 40 unique rows")
    for unit in expected_units:
        history = units.get(unit, [])
        if len(history) != 1 or runs[history[0]]["finish"] is None:
            raise PermissionError(f"refusing reconciliation of an unpaired or repeated unit: {unit}")
        if runs[history[0]]["finish"].get("status") != "succeeded":
            raise PermissionError(f"refusing reconciliation of a non-successful unit: {unit}")
    ledger = _ReadOnlyHistoricalLedger(binding, events)
    original_artifact_root = ARTIFACT_ROOT
    with tempfile.TemporaryDirectory(
        prefix="issue75-preflight-receipt-read-", dir=STAGE1,
    ) as scratch:
        snapshot_root = Path(scratch)
        for unit in expected_units:
            result_path = original_artifact_root / "units" / _unit_key(unit) / "result.json"
            result_blob = source_blob(result_path)
            if hashlib.sha256(result_blob).hexdigest() != runs[units[unit][0]]["finish"].get("result_sha256"):
                raise PermissionError(f"committed result bytes differ from their immutable ledger finish: {unit}")
            snapshot_path = snapshot_root / "units" / _unit_key(unit) / "result.json"
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_bytes(result_blob)
        initial_model = original_artifact_root / "models" / "pi0.pt"
        if initial_model.is_file():
            snapshot_model = snapshot_root / "models" / "pi0.pt"
            snapshot_model.parent.mkdir(parents=True, exist_ok=True)
            snapshot_model.write_bytes(source_blob(initial_model))
        ARTIFACT_ROOT = snapshot_root
        try:
            summary = _preflight_reports(ledger)
        finally:
            ARTIFACT_ROOT = original_artifact_root
    source_rows = {
        str(item.get("unit_id")): item for item in source_report.get("units", [])
        if isinstance(item, Mapping)
    }
    if set(source_rows) != set(expected_units):
        raise PermissionError("immutable source report does not contain the exact registered row set")
    for row in summary["rows"]:
        source_row = source_rows[row["unit_id"]]
        if source_row.get("result_sha256") != row["result_sha256"]:
            raise PermissionError(f"retained result digest changed during reconciliation: {row['unit_id']}")
        if source_row.get("ledger_status") != row["ledger_status"]:
            raise PermissionError(f"retained ledger status changed during reconciliation: {row['unit_id']}")
    if len(summary["rows"]) != 40 or summary["incomplete"]:
        raise PermissionError("reconciliation found incomplete rows; preserve and stop")
    status = (
        "PREFLIGHT_INCOMPLETE_NO_FORMAL" if summary["incomplete"]
        else "PREFLIGHT_TREATMENT_NOT_DELIVERED_NO_FORMAL"
        if summary["treatment_not_delivered"]
        else "PREFLIGHT_COMPLETE_PENDING_INDEPENDENT_REVIEW"
    )
    report = {
        "schema_id": "issue75-stage1-preflight-receipt-reconciliation-v1",
        "status": status,
        "study_id": STUDY_ID,
        "scope": "STUDY_ONLY",
        "package_commit": binding.package_commit,
        "source_lock_sha256": binding.source_lock_sha256,
        "source_preflight_report_sha256": source_report_sha256,
        "reconciliation_source_commit": repair_commit,
        "reconciliation_source_lock_sha256": budget.normalized_file_sha256(STAGE1 / "source-lock.json"),
        "registered_rows": 40,
        "completed_rows": len(summary["rows"]),
        "terminal_rows": sum(item["terminal_floor"] is not None for item in summary["rows"]),
        "formal_seeds_consumed": 0,
        "preflight_rows_are_not_efficacy_evidence": True,
        "formal_execution_enabled": False,
        "p1_policy_label": PROXY_LABEL,
        "treatment_not_delivered_units": summary["treatment_not_delivered"],
        "episode_treatment_delivered_units": [
            item["unit_id"] for item in summary["rows"]
            if item["arm"] == "J" and item["episode_treatment_delivered"] is True
        ],
        "root_treatment_not_delivered": summary["root_treatment_not_delivered"],
        "incomplete_units": summary["incomplete"],
        "units": summary["rows"],
        "completed_at": source_report.get("completed_at"),
    }
    report_path = ARTIFACT_ROOT / "preflight-report-review-repair.json"
    _write_once(report_path, report)
    report_sha256 = _file_sha256(report_path)

    estimates, forecast = _forecast_estimates(ledger)
    forecast["schema_id"] = "issue75-stage1-cost-forecast-reconciliation-v1"
    forecast["cost_model_status"] = forecast["status"]
    forecast["source_preflight_report_sha256"] = source_report_sha256
    forecast["reconciliation_report_sha256"] = report_sha256
    forecast["reconciliation_source_commit"] = repair_commit
    forecast["source_package_commit"] = binding.package_commit
    forecast["source_lock_sha256"] = binding.source_lock_sha256
    try:
        forecast["ledger"] = ledger.forecast(
            expected_unit_ids=_formal_unit_ids(),
            future_builder_cpu_seconds=estimates,
            allowed_completed_unit_ids=expected_units,
        )
        forecast["status"] = "PENDING_INDEPENDENT_AFFORDABILITY_REVIEW"
    except budget.ResourceBlocked as exc:
        forecast["status"] = "RESOURCE_BLOCKED"
        forecast["reason"] = str(exc)[:2048]
    forecast["formal_execution_enabled"] = False
    forecast["affordability_acceptance"] = "PENDING_INDEPENDENT_REVIEW"
    forecast_path = ARTIFACT_ROOT / "cost-forecast-review-repair.json"
    _write_once(forecast_path, forecast)
    return {
        "report": report,
        "report_sha256": report_sha256,
        "cost_forecast": forecast,
        "cost_forecast_sha256": _file_sha256(forecast_path),
    }


def _load_training_examples(block: int, *, ledger: Any) -> list[Any]:
    rows = [row for row in _rows() if row["phase"] == "training" and int(row["block_index"]) == block]
    expected_seeds = {int(row["owner_seed"]) for row in rows}
    compatibility_records = (
        ledger.block0_decoder_compatibility_records() if block == 0 else {}
    )
    examples = []
    for row in rows:
        unit_id = str(row["unit_id"])
        receipt = _existing_unit_receipt(
            ledger, unit_id, decoder_compatibility=compatibility_records,
        )
        if receipt is None:
            raise PermissionError("training block has an owner without a successful ledger receipt")
        run_id = str(receipt["resource"]["ledger_run_id"])
        compatibility = compatibility_records.get(unit_id)
        expected_binding = (
            ledger.binding.package_commit, ledger.binding.source_lock_sha256,
        )
        if compatibility is not None:
            if compatibility.get("run_id") != run_id:
                raise PermissionError("training owner receipt differs from its exact decoder compatibility run")
            expected_binding = (
                str(compatibility["package_commit"]),
                str(compatibility["source_lock_sha256"]),
            )
        if (
            receipt.get("phase") != "training" or receipt.get("formal") is not True
            or receipt.get("arm") != "TRAIN" or receipt.get("owner_seed") != int(row["owner_seed"])
            or receipt.get("package_commit") != expected_binding[0]
            or receipt.get("source_lock_sha256") != expected_binding[1]
        ):
            raise PermissionError("training owner receipt differs from its locked row or active compatibility binding")
        directory = _unit_directory(row["unit_id"])
        result = receipt
        if result.get("status") != "SUCCEEDED" or result.get("training_features") is None:
            raise PermissionError("training block lacks a complete natural owner and auxiliary J evidence")
        pointer = result["training_features"]
        path = _inside_artifacts(ARTIFACT_ROOT / pointer["path"])
        expected_path = _inside_artifacts(
            _run_artifact_directory(unit_id, run_id) / "training-features.json"
        )
        if path != expected_path:
            raise PermissionError("training feature pointer does not name its own immutable owner artifact")
        if _file_sha256(path) != pointer["sha256"]:
            raise PermissionError("training features differ from their immutable unit receipt")
        features = _read_json(path)
        if (
            features.get("schema_id") != FEATURE_SCHEMA
            or int(features.get("owner_seed", -1)) != int(row["owner_seed"])
            or features.get("unit_id") != unit_id
            or features.get("package_commit") != expected_binding[0]
            or features.get("source_lock_sha256") != expected_binding[1]
        ):
            raise PermissionError("training feature receipt is not for the registered owner")
        example = _training_example_from_payload(features)
        if example.owner_seed != int(row["owner_seed"]):
            raise PermissionError("training example owner differs from its locked seed-table row")
        examples.append(example)
    if len(examples) != 128 or {row.owner_seed for row in examples} != expected_seeds:
        raise PermissionError("training block does not contain exactly 128 unique registered owners")
    return examples


def _fixed_probe_features(ledger: Any, examples: Sequence[Any] | None) -> tuple[list[Any], str]:
    """Use the first actual block-zero decision per owner as the same frozen actor probe."""
    import numpy as np

    path = _inside_artifacts(ARTIFACT_ROOT / "policy-probe-features.json")
    if not path.exists():
        if examples is None:
            raise PermissionError("block-zero probe features are missing before a later policy update")
        owners = []
        for example in sorted(examples, key=lambda item: int(item.owner_seed)):
            if not example.base.decisions:
                continue
            decision = example.base.decisions[0]
            matrix = np.asarray(decision.candidate_features, dtype="<f4")
            if matrix.ndim != 2 or not 1 <= matrix.shape[0] <= MAX_FEATURE_ROWS or matrix.shape[1] != POLICY_DIMENSION:
                raise ValueError("fixed policy probe does not match the source-locked model input")
            raw = matrix.tobytes()
            owners.append({
                "owner_seed": int(example.owner_seed),
                "step_index": int(decision.step_index),
                "state_fingerprint": str(decision.state_fingerprint),
                "shape": list(matrix.shape),
                "matrix_sha256": _sha256(raw),
                "matrix_f32le_zlib_base64": base64.b64encode(zlib.compress(raw, level=6)).decode("ascii"),
            })
        _write_once(path, {
            "schema_id": "issue75-stage1-fixed-policy-probes-v1",
            "package_commit": ledger.binding.package_commit,
            "source_lock_sha256": ledger.binding.source_lock_sha256,
            "selection": "first actual multi-choice decision per block-zero owner in ascending registered seed order",
            "outcome_conditioned": False,
            "owners": owners,
        })
    payload = _read_json(path)
    if (
        payload.get("schema_id") != "issue75-stage1-fixed-policy-probes-v1"
        or payload.get("package_commit") != ledger.binding.package_commit
        or payload.get("source_lock_sha256") != ledger.binding.source_lock_sha256
        or payload.get("outcome_conditioned") is not False
    ):
        raise PermissionError("fixed policy probes are not bound to this package and ledger")
    features = []
    for row in payload.get("owners", ()):
        shape = row.get("shape")
        if not isinstance(shape, list) or len(shape) != 2 or not 1 <= int(shape[0]) <= MAX_FEATURE_ROWS or int(shape[1]) != POLICY_DIMENSION:
            raise ValueError("fixed policy probe feature shape is invalid")
        raw = zlib.decompress(base64.b64decode(row["matrix_f32le_zlib_base64"], validate=True))
        if len(raw) != int(shape[0]) * POLICY_DIMENSION * 4 or _sha256(raw) != row.get("matrix_sha256"):
            raise ValueError("fixed policy probe feature bytes failed their size/hash check")
        matrix = np.frombuffer(raw, dtype="<f4").reshape(tuple(map(int, shape))).copy()
        if not np.isfinite(matrix).all():
            raise ValueError("fixed policy probe contains non-finite values")
        features.append(matrix)
    return features, _file_sha256(path)


def _valid_digest(value: Any, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(char in "0123456789abcdef" for char in value)


def _expected_source_model(ledger: Any, block: int) -> str:
    if block == 0:
        return str(_read_json(STAGE1 / "config.json")["source"]["initial_model_digest"])
    previous = _existing_unit_receipt(ledger, f"UPDATE:{block - 1}")
    if previous is None:
        raise PermissionError("registered policy block lacks the preceding completed update receipt")
    return str(previous["result_model_digest"])


def _expected_policy_binding(
    ledger: Any, phase: str, row: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    config = _read_json(STAGE1 / "config.json")
    arm = str(row["arm"])
    if arm == "E":
        source_commit = str(config["source"]["registered_stage0_config"]["stsr_source_commit"])
        digest = _sha256(_canonical({"expert": "expert_non_combat_v1", "source": source_commit}))
        return digest, {
            "source": "isolated_expert_reference",
            "source_commit": source_commit,
            "policy_digest": digest,
            "formal_use": True,
        }

    model_label, proxy = _arm_policy(phase, arm, row)
    if proxy:
        record = _read_json(STAGE1 / "preflight-proxy.json")
        artifact = (STUDY / str(record["artifact_path"])).resolve(strict=True)
        if not artifact.is_relative_to(STUDY.resolve()) or _file_sha256(artifact) != record.get("artifact_sha256"):
            raise PermissionError("registered P1 preflight proxy artifact changed after its source lock")
        digest = str(record["policy_model_digest"])
        source = {
            "source": PROXY_LABEL,
            "artifact_path": str(record["artifact_path"]),
            "artifact_sha256": str(record["artifact_sha256"]),
            "initial_model_digest": str(record["initial_model_digest"]),
            "policy_model_digest": digest,
            "formal_use": False,
        }
        return digest, source

    if model_label == "pi0":
        path = _model_path("pi0")
        if not path.is_file():
            raise PermissionError("successful registered pi0 row lacks its initial-model checkpoint")
        return str(config["source"]["initial_model_digest"]), {
            "source": "registered_initial_pi0", "checkpoint_sha256": _file_sha256(path),
        }

    try:
        block = int(model_label.removeprefix("pi")) - 1
    except ValueError as exc:
        raise PermissionError("registered owner row names an unknown policy checkpoint") from exc
    if not 0 <= block < 4:
        raise PermissionError("registered owner row names a policy checkpoint outside pi1..pi4")
    update = _existing_unit_receipt(ledger, f"UPDATE:{block}")
    if update is None:
        raise PermissionError("registered owner row lacks its completed policy checkpoint receipt")
    checkpoint = update.get("model_checkpoint")
    if not isinstance(checkpoint, Mapping):
        raise PermissionError("policy update receipt lacks its saved model identity")
    path = _model_path(model_label)
    expected_relative = path.relative_to(ARTIFACT_ROOT).as_posix()
    checkpoint_sha = str(checkpoint.get("sha256", ""))
    digest = str(checkpoint.get("model_digest", ""))
    if (
        checkpoint.get("path") != expected_relative
        or not _valid_digest(checkpoint_sha, 64)
        or not _valid_digest(digest, 40)
        or update.get("result_model_digest") != digest
        or not path.is_file()
        or _file_sha256(path) != checkpoint_sha
    ):
        raise PermissionError("registered policy checkpoint no longer matches its completed update receipt")
    if model_label == "pi4":
        report_path = ARTIFACT_ROOT / "training-report.json"
        if not report_path.is_file():
            raise PermissionError("formal pi4 row lacks the completed four-block training report")
        report = _read_json(report_path)
        if (
            report.get("package_commit") != ledger.binding.package_commit
            or report.get("source_lock_sha256") != ledger.binding.source_lock_sha256
            or report.get("status") != "TRAINING_COMPLETE"
            or report.get("formal_owner_count") != 512
            or report.get("p1_formal_model_source") != "pi4"
            or report.get("p1_formal_model_digest") != digest
            or report.get("p1_formal_checkpoint", {}).get("sha256") != checkpoint_sha
        ):
            raise PermissionError("formal pi4 checkpoint differs from the complete registered training report")
    return digest, {"source": model_label, "checkpoint_sha256": checkpoint_sha, "formal_use": True}


def _validate_success_result(
    ledger: Any, *, unit_id: str, run_id: str, reservation: Mapping[str, Any],
    result: Mapping[str, Any], result_path: Path, finish: Mapping[str, Any] | None = None,
    source_binding: tuple[str, str] | None = None,
) -> dict[str, Any]:
    package_commit, source_lock_sha256 = source_binding or (
        ledger.binding.package_commit, ledger.binding.source_lock_sha256,
    )
    if (
        reservation.get("unit_id") != unit_id
        or reservation.get("role") != "builder"
        or reservation.get("package_commit") != package_commit
        or reservation.get("source_lock_sha256") != source_lock_sha256
        or result.get("schema_id") != UNIT_SCHEMA
        or result.get("status") != "SUCCEEDED"
        or result.get("unit_id") != unit_id
        or result.get("package_commit") != package_commit
        or result.get("source_lock_sha256") != source_lock_sha256
    ):
        raise PermissionError(f"successful unit receipt is not bound to its reservation and package binding: {unit_id}")
    if finish is not None:
        actual_digest = _file_sha256(result_path)
        if (
            finish.get("status") != "succeeded"
            or finish.get("unit_id") != unit_id
            or finish.get("package_commit") != package_commit
            or finish.get("source_lock_sha256") != source_lock_sha256
            or finish.get("result_sha256") != actual_digest
        ):
            raise PermissionError(f"successful unit result bytes differ from their immutable ledger finish: {unit_id}")

    registered = next((row for row in _rows() if row["unit_id"] == unit_id), None)
    if registered is not None:
        expected_phase = registered["phase"]
        if reservation.get("unit_kind") != "owner_arm":
            raise PermissionError(f"registered owner row has a non-owner reservation: {unit_id}")
        expected_fields = {
            "study_id": STUDY_ID,
            "phase": expected_phase,
            "formal": registered["formal"],
            "owner_seed": registered["owner_seed"],
            "game_seed": registered["game_seed"],
            "arm": registered["arm"],
            "paired_owner_key": registered["paired_owner_key"],
        }
        if any(result.get(key) != value for key, value in expected_fields.items()):
            raise PermissionError(f"successful owner result differs from its exact registered row: {unit_id}")
        native = _read_json(STAGE1 / "config.json")["source"]["registered_stage0_config"]
        if (
            result.get("native_source_commit") != native["sts_lightspeed_commit"]
            or result.get("stsr_source_commit") != native["stsr_source_commit"]
            or result.get("native_module_sha256") != native["native_module_sha256"]
        ):
            raise PermissionError(f"successful owner result differs from the registered native source: {unit_id}")
        expected_model_digest, expected_policy_source = _expected_policy_binding(
            ledger, expected_phase, registered,
        )
        if (
            result.get("model_digest") != expected_model_digest
            or result.get("policy_source") != expected_policy_source
        ):
            raise PermissionError(f"successful owner result differs from its registered policy source: {unit_id}")
        if registered["arm"] != "E" and result.get("expert_decisions") != 0:
            raise PermissionError("Expert actions appeared outside the isolated E reference arm")
        resource = result.get("resource")
        if not isinstance(resource, Mapping) or resource.get("ledger_run_id") != run_id:
            raise PermissionError(f"successful owner result does not name its exact ledger reservation: {unit_id}")
    elif unit_id.startswith("UPDATE:"):
        if reservation.get("unit_kind") != "training_update":
            raise PermissionError(f"policy update has a non-update reservation: {unit_id}")
        try:
            block = int(unit_id.split(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise PermissionError("policy update unit identifier is malformed") from exc
        checkpoint = result.get("model_checkpoint")
        path = _model_path(f"pi{block + 1}")
        if (
            result.get("ledger_run_id") != run_id
            or result.get("source_commit") != package_commit
            or result.get("block_index") != block
            or result.get("owner_count") != 128
            or result.get("completed_steps") != 64
            or result.get("expert_decisions") != 0
            or result.get("source_model_digest") != _expected_source_model(ledger, block)
            or not isinstance(checkpoint, Mapping)
            or checkpoint.get("path") != path.relative_to(ARTIFACT_ROOT).as_posix()
            or checkpoint.get("model_digest") != result.get("result_model_digest")
            or not _valid_digest(checkpoint.get("model_digest"), 40)
            or not _valid_digest(checkpoint.get("sha256"), 64)
            or not path.is_file()
            or _file_sha256(path) != checkpoint.get("sha256")
        ):
            raise PermissionError(f"successful update receipt differs from its registered block/model: {unit_id}")
    elif unit_id == "REPORT:HELDOUT":
        resource = result.get("resource")
        if not isinstance(resource, Mapping) or resource.get("ledger_run_id") != run_id or not isinstance(result.get("analysis"), Mapping):
            raise PermissionError("heldout analysis receipt does not match its exact ledger reservation")
    else:
        raise PermissionError(f"successful ledger receipt names an unregistered unit: {unit_id}")
    return dict(result)


def _existing_unit_receipt(
    ledger: Any, unit_id: str, *, allow_pre_game_start_retry: bool = False,
    decoder_compatibility: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    run_id, reservation, finish = _existing_unit(ledger, unit_id)
    if run_id is None:
        return None
    if finish is None:
        raise budget.ResourceBlocked(f"unresolved reservation is preserved at full cap: {unit_id}")
    if finish.get("status") != "succeeded":
        if (
            allow_pre_game_start_retry
            and unit_id == budget.PRE_GAME_START_RETRY_UNIT_ID
            and finish.get("status") == "failed"
        ):
            ledger.pre_game_start_retry_info(
                unit_id=unit_id, predecessor_run_id=run_id,
            )
            return None
        raise budget.ResourceBlocked(f"failed unit is immutable and cannot be replayed: {unit_id}")
    registered = next((row for row in _rows() if row["unit_id"] == unit_id), None)
    source_binding = (ledger.binding.package_commit, ledger.binding.source_lock_sha256)
    compatibility = None
    if (
        registered is not None and registered["phase"] == "training"
        and int(registered.get("block_index", -1)) == 0
        and reservation.get("package_commit") != ledger.binding.package_commit
    ):
        if decoder_compatibility is None:
            compatibility = ledger.block0_decoder_compatibility_record(unit_id, run_id)
        else:
            compatibility = decoder_compatibility.get(unit_id)
        if compatibility is None:
            # The original run remains charged and immutable. The narrow whole-block
            # correction uses a new run ID/artifact directory under the same owner.
            ledger.block0_phase_repair_info(unit_id=unit_id)
            return None
        if compatibility.get("run_id") != run_id:
            raise PermissionError("block-0 receipt is not the exact source-locked decoder compatibility run")
        source_binding = (
            str(compatibility["package_commit"]),
            str(compatibility["source_lock_sha256"]),
        )
    if compatibility is not None:
        result_path = _inside_artifacts(ARTIFACT_ROOT / str(compatibility["result_path"]))
    elif unit_id.startswith("UPDATE:"):
        result_path = _unit_directory(unit_id) / f"result-{run_id}.json"
    else:
        registered = next((row for row in _rows() if row["unit_id"] == unit_id), None)
        result_path = _owner_result_path(
            unit_id, run_id, "" if registered is None else str(registered["phase"]),
            current_package=reservation.get("package_commit") == ledger.binding.package_commit,
        )
    if not result_path.is_file():
        raise PermissionError(f"successful ledger unit lacks its durable result: {unit_id}")
    result = _read_json(result_path)
    return _validate_success_result(
        ledger, unit_id=unit_id, run_id=run_id, reservation=reservation or {},
        result=result, result_path=result_path, finish=finish,
        source_binding=source_binding,
    )


def _unit_cap(phase: str, row: Mapping[str, Any], estimates: Mapping[str, float] | None) -> float:
    if phase == "preflight":
        return PREFLIGHT_ROW_RESERVATION
    if estimates is None:
        raise ValueError("formal row reservation requires a current full-cohort cost forecast")
    return float(estimates[str(row["unit_id"])])


def _run_unit(ledger: Any, row: Mapping[str, Any], *, phase: str, estimates: Mapping[str, float] | None) -> dict[str, Any]:
    unit_id = str(row["unit_id"])
    existing = _existing_unit_receipt(ledger, unit_id)
    if existing is not None:
        return existing
    previous_run_id, _, previous_finish = _existing_unit(ledger, unit_id)
    resume_digest = None
    if previous_run_id is not None:
        if phase != "train" or row.get("phase") != "training":
            raise budget.ResourceBlocked(f"owner/arm rows do not support replay or checkpoint resume: {unit_id}")
        if previous_finish is None or previous_finish.get("status") != "incomplete":
            raise budget.ResourceBlocked(f"unit is not an explicitly resumable update: {unit_id}")
        resume_digest = previous_finish.get("checkpoint_sha256")
        if not resume_digest:
            raise budget.CheckpointMismatch("incomplete policy update has no durable step checkpoint")
    run_id = f"{unit_id}-{uuid.uuid4().hex[:16]}"
    cap = _unit_cap(phase, row, estimates)
    unit_kind = "training_update" if unit_id.startswith("UPDATE:") else "owner_arm"
    reservation = ledger.reserve(
        run_id=run_id,
        unit_id=unit_id,
        role="builder",
        reserved_cpu_seconds=cap,
        unit_kind=unit_kind,
        previous_run_id=previous_run_id,
        checkpoint_sha256=resume_digest,
    )
    directory = _unit_directory(unit_id)
    directory.mkdir(parents=True, exist_ok=True)
    run_directory = _run_artifact_directory(unit_id, run_id) if row.get("phase") in {"training", "heldout"} else directory
    run_directory.mkdir(parents=True, exist_ok=True)
    owner_result_path = run_directory / "result.json"
    job_path = _inside_artifacts(directory / f"worker-job-{run_id}.json")
    stdout_path = _inside_artifacts(directory / f"worker-log-{run_id}.txt")
    job = {
        "schema_id": WORKER_SCHEMA,
        "package_commit": ledger.binding.package_commit,
        "source_lock_sha256": ledger.binding.source_lock_sha256,
        "run_id": run_id,
        "unit_id": unit_id,
        "phase": _worker_phase(phase, row),
        "row": dict(row),
        "output_dir": str(run_directory / "native"),
        "result_path": str(owner_result_path),
        "previous_run_id": previous_run_id,
        "checkpoint_sha256": resume_digest,
    }
    _write_once(job_path, job)
    env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(_inside_artifacts(ARTIFACT_ROOT / "tmp")),
        "TMP": str(_inside_artifacts(ARTIFACT_ROOT / "tmp")),
        "TEMP": str(_inside_artifacts(ARTIFACT_ROOT / "tmp")),
        "XDG_CACHE_HOME": str(_inside_artifacts(ARTIFACT_ROOT / "cache")),
        "TORCH_HOME": str(_inside_artifacts(ARTIFACT_ROOT / "cache" / "torch")),
        "PYTHONPYCACHEPREFIX": str(_inside_artifacts(ARTIFACT_ROOT / "cache" / "pycache")),
    }
    for key in ("TMPDIR", "XDG_CACHE_HOME", "TORCH_HOME", "PYTHONPYCACHEPREFIX"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    watchdog = budget.ProcessTreeWatchdog()
    started = time.monotonic()
    status = "failed"
    measured = 0.0
    checkpoint_sha = None
    try:
        process = watchdog.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker-job", str(job_path)],
            reserved_cpu_seconds=cap,
            cwd=ROOT,
            env=env,
            stdout_path=stdout_path,
        )
        measured = float(process.cpu_seconds)
        if process.returncode != 0:
            raise RuntimeError(f"Stage 1 worker exited with status {process.returncode}")
        result = _read_json(owner_result_path)
        result_path = owner_result_path
        _validate_success_result(
            ledger, unit_id=unit_id, run_id=run_id, reservation=reservation,
            result=result, result_path=result_path,
        )
        if measured > cap:
            _finish_failed_unit(
                ledger, run_id=run_id, unit_id=unit_id, directory=directory,
                measured=measured, cap=cap, boundary="measured_process_tree_cpu_over_reservation",
                started=started,
            )
            raise budget.ResourceBlocked(f"unit exceeded its process-tree CPU reservation: {unit_id}")
        ledger.finish(
            run_id=run_id, measured_cpu_seconds=measured, status="succeeded",
            result_sha256=_file_sha256(result_path),
        )
        status = "succeeded"
        return result
    except budget.ProcessTreeBudgetExceeded as exc:
        measured = float(exc.cpu_seconds)
        if unit_kind == "training_update":
            checkpoint_path = _inside_artifacts(ARTIFACT_ROOT / "models" / f"block-{int(row['block_index'])}-step.pt")
            if checkpoint_path.is_file():
                checkpoint_sha = _file_sha256(checkpoint_path)
                status = "incomplete"
        _finish_failed_unit(
            ledger, run_id=run_id, unit_id=unit_id, directory=directory,
            measured=measured, cap=cap, boundary="process_tree_cpu_cap",
            started=started, status=status, checkpoint_sha256=checkpoint_sha,
        )
        if status != "incomplete":
            raise budget.ResourceBlocked(f"unit failed its process-tree CPU cap and is not replayable: {unit_id}") from exc
        raise budget.ResourceBlocked(f"training update stopped at a durable optimizer checkpoint: {unit_id}") from exc
    except BaseException as exc:
        measured = max(measured, float(getattr(exc, "cpu_seconds", 0.0)))
        if "process" not in locals():
            measured = max(measured, cap)
        current, _, finish = _existing_unit(ledger, unit_id)
        if current == run_id and finish is None:
            _finish_failed_unit(
                ledger, run_id=run_id, unit_id=unit_id, directory=directory,
                measured=measured, cap=cap, boundary=type(exc).__name__,
                started=started,
            )
        raise


def _supervisor_lock_path() -> Path:
    return _inside_artifacts(ARTIFACT_ROOT / "supervisors.active.lock")


@contextmanager
def _supervisor_activity_lease():
    path = _supervisor_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _assert_no_active_supervisors() -> None:
    path = _supervisor_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            raise budget.ResourceBlocked(
                "a prior Stage 1 supervisor is still active; preserve its reservations and wait"
            ) from exc
    finally:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _formal_concurrency() -> int:
    config = _read_json(STAGE1 / "config.json")
    limits = _read_json(STAGE1 / "budget.json")
    configured = config.get("execution", {}).get("max_concurrent_owner_process_trees")
    if (
        type(configured) is not int or configured != 1
        or limits.get("concurrent_stage1_process_trees") != 1
    ):
        raise PermissionError("formal process-tree concurrency differs from its locked resource contract")
    return configured


def _worker_environment(runtime_root: Path) -> dict[str, str]:
    runtime_root = _inside_artifacts(runtime_root)
    paths = {
        "TMPDIR": runtime_root / "tmp",
        "TMP": runtime_root / "tmp",
        "TEMP": runtime_root / "tmp",
        "XDG_CACHE_HOME": runtime_root / "cache",
        "TORCH_HOME": runtime_root / "cache" / "torch",
        "PYTHONPYCACHEPREFIX": runtime_root / "cache" / "pycache",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({key: str(value) for key, value in paths.items()})
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS"):
        env[key] = "1"
    env["MKL_DYNAMIC"] = "FALSE"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _supervise_batch(items: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    if not items or len(items) > _formal_concurrency():
        raise ValueError("supervisor batch exceeds the locked Stage 1 process-tree limit")
    active: list[tuple[Mapping[str, Any], subprocess.Popen[str]]] = []
    results: dict[str, dict[str, Any]] = {}
    for item in items:
        run_id = str(item["run_id"])
        try:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()),
                 "--supervisor-job", str(item["job_path"])],
                cwd=ROOT, env=dict(item["env"]), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                close_fds=True, start_new_session=True,
            )
            active.append((item, proc))
        except OSError:
            results[run_id] = {
                "schema_id": SUPERVISOR_SCHEMA, "unit_id": str(item["unit_id"]),
                "run_id": run_id, "status": "SUPERVISOR_START_FAILED",
                "measured_cpu_seconds": float(item["reserved_cpu_seconds"]),
                "boundary": "supervisor_start_failed",
            }
    while any(proc.poll() is None for _, proc in active):
        time.sleep(0.01)
    for item, proc in active:
        output, _ = proc.communicate()
        run_id = str(item["run_id"])
        try:
            lines = [line for line in output.splitlines() if line.strip()]
            payload = json.loads(lines[-1]) if lines else {}
            if (
                not isinstance(payload, dict)
                or payload.get("schema_id") != SUPERVISOR_SCHEMA
                or payload.get("unit_id") != item["unit_id"]
                or payload.get("run_id") != run_id
                or not isinstance(payload.get("measured_cpu_seconds"), (int, float))
                or not math.isfinite(float(payload["measured_cpu_seconds"]))
                or float(payload["measured_cpu_seconds"]) < 0
                or (proc.returncode == 0 and payload.get("status") != "SUCCEEDED")
                or (proc.returncode != 0 and payload.get("status") == "SUCCEEDED")
            ):
                raise ValueError("invalid supervisor receipt")
            results[run_id] = payload
        except (ValueError, TypeError, json.JSONDecodeError):
            results[run_id] = {
                "schema_id": SUPERVISOR_SCHEMA, "unit_id": str(item["unit_id"]),
                "run_id": run_id, "status": "SUPERVISOR_UNRECEIPTED",
                "measured_cpu_seconds": float(item["reserved_cpu_seconds"]),
                "boundary": "missing_or_invalid_supervisor_receipt",
            }
    return results


def _run_owner_batch(
    ledger: Any, rows: Sequence[Mapping[str, Any]], *,
    phase: str, estimates: Mapping[str, float],
) -> list[dict[str, Any]]:
    pending = []
    completed = {}
    for row in rows:
        unit_id = str(row["unit_id"])
        existing = _existing_unit_receipt(
            ledger, unit_id,
            allow_pre_game_start_retry=(phase == "train" and unit_id == budget.PRE_GAME_START_RETRY_UNIT_ID),
        )
        if existing is not None:
            completed[unit_id] = existing
            continue
        previous_run_id, _, previous_finish = _existing_unit(ledger, unit_id)
        resume_digest = None
        retry_proof = None
        retry_reason = None
        repair_info = None
        if previous_run_id is not None:
            if phase != "train" or row.get("phase") != "training":
                raise budget.ResourceBlocked(f"owner/arm rows do not support replay or checkpoint resume: {unit_id}")
            if previous_finish is None:
                raise budget.ResourceBlocked(f"unit has an unresolved reservation: {unit_id}")
            if previous_finish.get("status") == "incomplete":
                resume_digest = previous_finish.get("checkpoint_sha256")
                if not resume_digest:
                    raise budget.CheckpointMismatch("incomplete policy update has no durable step checkpoint")
            elif (
                previous_finish.get("status") == "succeeded"
                and phase == "train" and row.get("phase") == "training"
                and int(row.get("block_index", -1)) == 0
            ):
                repair_info = ledger.block0_phase_repair_info(unit_id=unit_id)
            elif (
                previous_finish.get("status") == "failed"
                and unit_id == budget.PRE_GAME_START_RETRY_UNIT_ID
                and previous_run_id == budget.PRE_GAME_START_RETRY_RUN_ID
            ):
                retry_reason = budget.PRE_GAME_START_RETRY_REASON
                retry_proof = ledger.pre_game_start_retry_info(
                    unit_id=unit_id, predecessor_run_id=previous_run_id,
                )
            else:
                raise budget.ResourceBlocked(f"unit is not eligible for retry or checkpoint resume: {unit_id}")
        run_id = f"{unit_id}-{uuid.uuid4().hex[:16]}"
        cap = float(estimates[unit_id])
        if retry_proof is not None and cap > budget.PRE_GAME_START_RETRY_RESERVED_CPU_SECONDS:
            raise budget.ResourceBlocked("the one-row retry estimate exceeds its immutable original CPU ceiling")
        reservation_previous_run_id = None if repair_info is not None else previous_run_id
        pending.append({
            "row": dict(row), "unit_id": unit_id, "run_id": run_id, "cap": cap,
            "previous_run_id": reservation_previous_run_id, "checkpoint_sha256": resume_digest,
            "retry_reason": retry_reason, "pre_game_start_proof": retry_proof,
            "repair_info": repair_info,
        })
    if not pending:
        return [completed[str(row["unit_id"])] for row in rows]
    reservations = ledger.reserve_many([{
        "run_id": item["run_id"], "unit_id": item["unit_id"], "role": "builder",
        "reserved_cpu_seconds": item["cap"], "unit_kind": "owner_arm",
        "previous_run_id": item["previous_run_id"],
        "checkpoint_sha256": item["checkpoint_sha256"],
        "retry_reason": item["retry_reason"],
        "pre_game_start_proof": item["pre_game_start_proof"],
        "repair_reason": None if item["repair_info"] is None else item["repair_info"]["repair_reason"],
        "repair_manifest_sha256": None if item["repair_info"] is None else item["repair_info"]["repair_manifest_sha256"],
        "repair_predecessor_run_id": None if item["repair_info"] is None else item["repair_info"]["repair_predecessor_run_id"],
        "repair_predecessor_receipt_sha256": None if item["repair_info"] is None else item["repair_info"]["repair_predecessor_receipt_sha256"],
    } for item in pending])
    supervisor_items = []
    started = time.monotonic()
    for item, reservation in zip(pending, reservations, strict=True):
        directory = _unit_directory(item["unit_id"])
        directory.mkdir(parents=True, exist_ok=True)
        job_path = _inside_artifacts(directory / f"worker-job-{item['run_id']}.json")
        log_path = _inside_artifacts(directory / f"worker-log-{item['run_id']}.txt")
        runtime_root = _inside_artifacts(directory / "runtime" / item["run_id"])
        run_directory = _run_artifact_directory(item["unit_id"], item["run_id"])
        run_directory.mkdir(parents=True, exist_ok=True)
        job = {
            "schema_id": WORKER_SCHEMA,
            "package_commit": ledger.binding.package_commit,
            "source_lock_sha256": ledger.binding.source_lock_sha256,
            "run_id": item["run_id"], "unit_id": item["unit_id"],
            "phase": _worker_phase(phase, item["row"]),
            "row": item["row"],
            "output_dir": str(run_directory / "native"),
            "result_path": str(run_directory / "result.json"),
            "worker_log_path": str(log_path), "runtime_root": str(runtime_root),
            "previous_run_id": item["previous_run_id"],
            "checkpoint_sha256": item["checkpoint_sha256"],
            "retry_reason": reservation.get("retry_reason"),
            "pre_game_start_proof": reservation.get("pre_game_start_proof"),
            "pre_game_start_proof_sha256": reservation.get("pre_game_start_proof_sha256"),
            "repair_reason": reservation.get("repair_reason"),
            "repair_manifest_sha256": reservation.get("repair_manifest_sha256"),
            "repair_predecessor_run_id": reservation.get("repair_predecessor_run_id"),
            "repair_predecessor_receipt_sha256": reservation.get("repair_predecessor_receipt_sha256"),
        }
        _write_once(job_path, job)
        supervisor_items.append({
            "unit_id": item["unit_id"], "run_id": item["run_id"],
            "job_path": job_path, "env": _worker_environment(runtime_root),
            "reserved_cpu_seconds": item["cap"], "reservation": reservation,
            "directory": directory, "started": started,
        })
    supervisor_results = _supervise_batch(supervisor_items)
    failures = []
    for item in supervisor_items:
        run_id, unit_id = str(item["run_id"]), str(item["unit_id"])
        envelope = supervisor_results[run_id]
        measured = float(envelope["measured_cpu_seconds"])
        cap = float(item["reserved_cpu_seconds"])
        result_path = _run_artifact_directory(unit_id, run_id) / "result.json"
        try:
            if envelope["status"] != "SUCCEEDED" or measured > cap or not result_path.is_file():
                boundary = str(envelope.get("boundary", envelope.get("status", "worker_failed")))
                _finish_failed_unit(
                    ledger, run_id=run_id, unit_id=unit_id, directory=item["directory"],
                    measured=measured, cap=cap, boundary=boundary, started=started,
                )
                failures.append(unit_id)
                continue
            result = _read_json(result_path)
            _validate_success_result(
                ledger, unit_id=unit_id, run_id=run_id,
                reservation=item["reservation"], result=result, result_path=result_path,
            )
            ledger.finish(
                run_id=run_id, measured_cpu_seconds=measured, status="succeeded",
                result_sha256=_file_sha256(result_path),
            )
            completed[unit_id] = result
        except budget.ResourceBlocked:
            failures.append(unit_id)
        except BaseException as exc:
            current, _, finish = _existing_unit(ledger, unit_id)
            if current == run_id and finish is None:
                try:
                    _finish_failed_unit(
                        ledger, run_id=run_id, unit_id=unit_id, directory=item["directory"],
                        measured=measured, cap=cap, boundary=type(exc).__name__, started=started,
                    )
                except budget.ResourceBlocked:
                    pass
            failures.append(unit_id)
    if failures:
        raise budget.ResourceBlocked("formal owner batch failed; preserve receipts and stop: " + ", ".join(failures))
    return [completed[str(row["unit_id"])] for row in rows]


def _run_scheduler_reservation(ledger: Any, unit_id: str, estimates: Mapping[str, float]) -> dict[str, Any]:
    previous, reservation, finish = _existing_unit(ledger, unit_id)
    if previous is not None:
        if unit_id != "SCHEDULER:TRAIN" or reservation is None or finish is not None:
            raise budget.ResourceBlocked(f"scheduler reservation already exists and cannot be repeated: {unit_id}")
        runs, _ = _event_state(ledger)
        recovery_events = runs.get(previous, {}).get("scheduler_recoveries", [])
        if recovery_events:
            raise budget.ResourceBlocked("training scheduler recovery was interrupted; preserve and stop without rebooking")
        cap = float(estimates[unit_id])
        if float(reservation.get("reserved_cpu_seconds", -1)) != cap:
            raise budget.ResourceBlocked("training scheduler recovery changed its immutable CPU allocation")
        recovery = ledger.recover_open_training_scheduler()
        return {
            "run_id": previous, "unit_id": unit_id, "cap": cap,
            "reservation": reservation, "recovery": recovery,
        }
    run_id = f"{unit_id}-{uuid.uuid4().hex[:16]}"
    cap = (
        TRAINING_SCHEDULER_CONTINUATION_RESERVATION
        if unit_id.startswith(TRAINING_SCHEDULER_CONTINUATION_PREFIX)
        else float(estimates[unit_id])
    )
    reservation = ledger.reserve(
        run_id=run_id, unit_id=unit_id, role="builder",
        reserved_cpu_seconds=cap, unit_kind="scheduler",
    )
    return {
        "run_id": run_id, "unit_id": unit_id, "cap": cap,
        "reservation": reservation, "recovery": None,
    }


def _require_scheduler_cpu_headroom(reservation: Mapping[str, Any], phase_started_cpu: float) -> None:
    cap = float(reservation["cap"])
    margin = min(1.0, cap * 0.05)
    used = max(0.0, time.process_time() - phase_started_cpu)
    if used >= cap - margin:
        raise budget.ResourceBlocked(
            "formal scheduler reached its controller CPU stop margin; preserve receipts and stop"
        )


def _finish_scheduler_reservation(
    ledger: Any, reservation: Mapping[str, Any], *,
    phase_started_cpu: float, separately_accounted_cpu_seconds: float = 0.0,
) -> dict[str, Any]:
    unit_id, run_id = str(reservation["unit_id"]), str(reservation["run_id"])
    cap = float(reservation["cap"])
    current_process_cpu = max(0.0, time.process_time() - phase_started_cpu - separately_accounted_cpu_seconds)
    recovery = reservation.get("recovery")
    prior_cpu_upper_bound = 0.0 if recovery is None else float(recovery["prior_cpu_upper_bound_seconds"])
    measured = prior_cpu_upper_bound + current_process_cpu
    success = measured <= cap
    result = {
        "schema_id": UNIT_SCHEMA, "status": "SUCCEEDED" if success else "FAILED",
        "unit_id": unit_id, "run_id": run_id,
        "package_commit": ledger.binding.package_commit,
        "source_lock_sha256": ledger.binding.source_lock_sha256,
        "measured_cpu_seconds": measured, "reserved_cpu_seconds": cap,
        "current_process_cpu_seconds": current_process_cpu,
        "prior_scheduler_cpu_upper_bound_seconds": prior_cpu_upper_bound,
        "scheduler_recovery_marker_sha256": (
            None if recovery is None else budget.sha256_bytes(budget.canonical(recovery))
        ),
        "scheduler_process_tree_limit": _formal_concurrency(),
    }
    result_path = _unit_directory(unit_id) / "result.json"
    _write_once(result_path, result)
    receipt_sha256 = _file_sha256(result_path) if success else None
    try:
        ledger.finish(
            run_id=run_id, measured_cpu_seconds=measured,
            status="succeeded" if success else "failed",
            result_sha256=receipt_sha256,
        )
    except budget.BudgetOverrun as exc:
        raise budget.ResourceBlocked("formal scheduler exceeded its reserved CPU allowance; preserve and stop") from exc
    if not success:
        raise budget.ResourceBlocked("formal scheduler exceeded its reserved CPU allowance; preserve and stop")
    return {**result, "receipt_sha256": receipt_sha256}


def _training_scheduler_plan(ledger: Any, estimates: Mapping[str, float]) -> dict[str, Any]:
    """Select a fresh, ledger-bounded controller reservation without reusing uncertain CPU."""
    runs, units = _event_state(ledger)
    base_history = units.get("SCHEDULER:TRAIN", [])
    if len(base_history) > 1:
        raise budget.ResourceBlocked("training scheduler has duplicate base reservations")
    base_run = None if not base_history else runs[base_history[-1]]
    continuation_units: list[tuple[int, str, dict[str, Any]]] = []
    for candidate, history in units.items():
        if not candidate.startswith(TRAINING_SCHEDULER_CONTINUATION_PREFIX):
            continue
        suffix = candidate[len(TRAINING_SCHEDULER_CONTINUATION_PREFIX):]
        if not suffix.isdecimal() or str(int(suffix)) != suffix or len(history) != 1:
            raise budget.ResourceBlocked("training scheduler continuation lineage is malformed")
        run = runs[history[0]]
        reservation = run["reservation"]
        if (
            reservation.get("role") != "builder"
            or reservation.get("unit_kind") != "scheduler"
            or reservation.get("reserved_cpu_seconds") != TRAINING_SCHEDULER_CONTINUATION_RESERVATION
        ):
            raise budget.ResourceBlocked("training scheduler continuation changed its fixed CPU allocation")
        continuation_units.append((int(suffix), candidate, run))
    continuation_units.sort()
    if [item[0] for item in continuation_units] != list(range(1, len(continuation_units) + 1)):
        raise budget.ResourceBlocked("training scheduler continuation sequence has a gap")
    if base_run is None and continuation_units:
        raise budget.ResourceBlocked("training scheduler continuation has no original reservation lineage")
    scheduler_cap = float(estimates.get("SCHEDULER:TRAIN", FORMAL_SCHEDULER_RESERVATION))
    if base_run is not None:
        base_reservation = base_run["reservation"]
        if (
            base_reservation.get("role") != "builder"
            or base_reservation.get("unit_kind") != "scheduler"
            or base_reservation.get("reserved_cpu_seconds") != scheduler_cap
        ):
            raise budget.ResourceBlocked("original training scheduler reservation differs from its immutable cap")
    if base_run is not None and base_run["finish"] is not None and base_run["finish"].get("status") == "succeeded":
        if continuation_units:
            raise budget.ResourceBlocked("completed base scheduler has an unexpected continuation lineage")
        receipt = _existing_scheduler_receipt(ledger, "SCHEDULER:TRAIN")
        return {"action": "reuse_receipt", "unit_id": "SCHEDULER:TRAIN", "cap": 0.0, "receipt": receipt}
    if continuation_units:
        _, latest_unit, latest = continuation_units[-1]
        finish = latest["finish"]
        if finish is not None and finish.get("status") == "succeeded":
            receipt = _existing_scheduler_receipt(ledger, latest_unit)
            return {"action": "reuse_receipt", "unit_id": latest_unit, "cap": 0.0, "receipt": receipt}
    elif base_run is None:
        return {
            "action": "reserve", "unit_id": "SCHEDULER:TRAIN",
            "cap": scheduler_cap, "receipt": None,
        }
    elif base_run["finish"] is None and not base_run.get("scheduler_recoveries"):
        try:
            budget._scheduler_train_recovery_cpu_upper_bound(runs, units, ledger.directory.parent)
            return {
                "action": "recover", "unit_id": "SCHEDULER:TRAIN",
                "cap": scheduler_cap, "receipt": None,
            }
        except budget.ResourceBlocked:
            pass
    next_index = 1 if not continuation_units else continuation_units[-1][0] + 1
    unit_id = f"{TRAINING_SCHEDULER_CONTINUATION_PREFIX}{next_index}"
    return {
        "action": "reserve", "unit_id": unit_id,
        "cap": TRAINING_SCHEDULER_CONTINUATION_RESERVATION,
        "receipt": None,
    }


def _existing_scheduler_receipt(ledger: Any, unit_id: str) -> dict[str, Any]:
    run_id, reservation, finish = _existing_unit(ledger, unit_id)
    if run_id is None or reservation is None or finish is None or finish.get("status") != "succeeded":
        raise budget.ResourceBlocked(f"formal scheduler has no durable successful receipt: {unit_id}")
    path = _unit_directory(unit_id) / "result.json"
    if not path.is_file():
        raise PermissionError(f"formal scheduler receipt is missing: {unit_id}")
    result = _read_json(path)
    measured = result.get("measured_cpu_seconds")
    reserved = reservation.get("reserved_cpu_seconds")
    runs, _ = _event_state(ledger)
    recovery_events = runs.get(run_id, {}).get("scheduler_recoveries", [])
    if len(recovery_events) > 1:
        raise PermissionError(f"formal scheduler has duplicate recovery markers: {unit_id}")
    recovery = recovery_events[0] if recovery_events else None
    current_process_cpu = result.get("current_process_cpu_seconds")
    prior_cpu_upper_bound = result.get("prior_scheduler_cpu_upper_bound_seconds")
    expected_prior = 0.0 if recovery is None else float(recovery["prior_cpu_upper_bound_seconds"])
    expected_marker_sha = None if recovery is None else budget.sha256_bytes(budget.canonical(recovery))
    if (
        result.get("schema_id") != UNIT_SCHEMA or result.get("status") != "SUCCEEDED"
        or result.get("unit_id") != unit_id or result.get("run_id") != run_id
        or result.get("package_commit") != ledger.binding.package_commit
        or result.get("source_lock_sha256") != ledger.binding.source_lock_sha256
        or not isinstance(measured, (int, float)) or not math.isfinite(float(measured))
        or not isinstance(reserved, (int, float)) or float(measured) < 0
        or float(measured) > float(reserved)
        or result.get("reserved_cpu_seconds") != reserved
        or not isinstance(current_process_cpu, (int, float)) or not math.isfinite(float(current_process_cpu))
        or float(current_process_cpu) < 0
        or prior_cpu_upper_bound != expected_prior
        or measured != float(current_process_cpu) + expected_prior
        or result.get("scheduler_recovery_marker_sha256") != expected_marker_sha
        or result.get("scheduler_process_tree_limit") != _formal_concurrency()
        or finish.get("status") != "succeeded"
        or finish.get("measured_cpu_seconds") != measured
        or finish.get("reserved_cpu_seconds") != reserved
        or _file_sha256(path) != finish.get("result_sha256")
    ):
        raise PermissionError(f"formal scheduler receipt differs from its locked ledger: {unit_id}")
    return {**result, "receipt_sha256": finish.get("result_sha256")}


def _load_or_make_initial_model() -> tuple[Any, str]:
    model, digest, _ = _load_model("pi0")
    return model, digest


def _atomic_torch_save(path: Path, value: Mapping[str, Any]) -> None:
    import torch

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            torch.save(dict(value), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _update_block(
    ledger: Any, block: int, *, estimates: Mapping[str, float], ledger_run_id: str,
) -> dict[str, Any]:
    import torch
    import learning
    from model import model_digest

    unit_id = f"UPDATE:{block}"
    previous_run_id, previous_finish = _current_update_reservation(
        ledger, unit_id=unit_id, run_id=ledger_run_id,
    )
    examples = _load_training_examples(block, ledger=ledger)
    frozen, source_digest, _ = _load_model(f"pi{block}")
    checkpoint_path = _inside_artifacts(ARTIFACT_ROOT / "models" / f"block-{block}-step.pt")
    resume_state = None
    if previous_run_id is not None:
        expected = previous_finish.get("checkpoint_sha256")
        if not checkpoint_path.is_file() or _file_sha256(checkpoint_path) != expected:
            raise budget.CheckpointMismatch("optimizer state does not match the incomplete ledger checkpoint")
        resume_state = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
        if (
            resume_state.get("schema_id") != RESUME_SCHEMA
            or resume_state.get("block_index") != block
            or resume_state.get("source_model_digest") != source_digest
        ):
            raise budget.CheckpointMismatch("optimizer checkpoint belongs to a different frozen model block")

    def checkpoint_step(step, model, optimizer, rng_state, order, diagnostics):
        value = {
            "schema_id": RESUME_SCHEMA,
            "block_index": int(block),
            "completed_steps": int(step),
            "block_order_seed": int(block_seed),
            "source_model_digest": source_digest,
            "owner_seed_sha256": owner_seed_sha,
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "python_rng_state": rng_state,
            "owner_order": [int(item) for item in order],
            "diagnostics": [vars(item) for item in diagnostics],
        }
        _atomic_torch_save(checkpoint_path, value)

    import prepare
    stage1_config = _read_json(STAGE1 / "config.json")
    registered_seeds = stage1_config.get("seeds", {}).get("block_order_seeds", ())
    if len(registered_seeds) != 4:
        raise PermissionError("the locked four-block owner-order seed schedule is incomplete")
    block_seed = int(registered_seeds[block])
    if block_seed != prepare.derive_block_order_seed(block):
        raise PermissionError("derived block-order seed differs from the registered Stage 1 seed schedule")
    owner_seed_sha = hashlib.sha256(",".join(str(item.owner_seed) for item in examples).encode("ascii")).hexdigest()
    result = learning.train_policy_iteration_block(
        frozen,
        examples,
        block_order_seed=block_seed,
        step_callback=checkpoint_step,
        resume_state=resume_state,
    )
    probes, probes_sha256 = _fixed_probe_features(ledger, examples if block == 0 else None)
    probe_diagnostics = learning.policy_probe_diagnostics(frozen, result.model, probes) if probes else None
    target = _model_path(f"pi{block + 1}")
    model_record = _save_model_once(result.model, target, model_digest)
    result_receipt = {
        "schema_id": UNIT_SCHEMA,
        "status": "SUCCEEDED",
        "unit_id": unit_id,
        "block_index": block,
        "owner_count": len(examples),
        "completed_steps": len(result.diagnostics),
        "block_order_seed": result.block_order_seed,
        "owner_order_sha256": result.owner_order_sha256,
        "owner_seed_sha256": owner_seed_sha,
        "source_model_digest": result.source_model_digest,
        "result_model_digest": result.result_model_digest,
        "model_checkpoint": model_record,
        "diagnostics": [vars(item) for item in result.diagnostics],
        "probe_features_sha256": probes_sha256,
        "probe_state_count": len(probes),
        "probe_diagnostics": probe_diagnostics,
        "expert_decisions": 0,
        "ledger_run_id": ledger_run_id,
        "package_commit": ledger.binding.package_commit,
        "source_commit": ledger.binding.package_commit,
        "source_lock_sha256": ledger.binding.source_lock_sha256,
    }
    return result_receipt


def _run_update_unit(ledger: Any, block: int, *, estimates: Mapping[str, float]) -> dict[str, Any]:
    unit_id = f"UPDATE:{block}"
    previous_run_id, previous_reservation, previous_finish = _existing_unit(ledger, unit_id)
    resume_digest = None
    repair_info = None
    if previous_run_id is not None:
        if previous_finish is None:
            raise budget.ResourceBlocked(f"optimizer update reservation remains unresolved: {unit_id}")
        if previous_finish.get("status") == "succeeded":
            existing = _existing_unit_receipt(ledger, unit_id)
            if existing is None:
                raise PermissionError(f"succeeded update has no immutable receipt: {unit_id}")
            return existing
        if previous_finish.get("status") == "failed":
            if unit_id != "UPDATE:0":
                raise budget.ResourceBlocked(f"failed optimizer update is immutable and cannot be replayed: {unit_id}")
            repair_info = ledger.update0_retry_info()
            if previous_run_id != repair_info["retry_previous_run_id"]:
                raise budget.ResourceBlocked("UPDATE:0 retry no longer names its exact failed predecessor")
        elif previous_finish.get("status") == "incomplete":
            if (
                previous_reservation is None
                or previous_reservation.get("unit_kind") != "training_update"
                or previous_reservation.get("role") != "builder"
            ):
                raise PermissionError(f"incomplete predecessor is not the registered Builder update: {unit_id}")
            resume_digest = previous_finish.get("checkpoint_sha256")
            if not _valid_digest(resume_digest, 64):
                raise budget.CheckpointMismatch("incomplete optimizer update lacks a valid checkpoint digest")
            checkpoint = _inside_artifacts(ARTIFACT_ROOT / "models" / f"block-{block}-step.pt")
            if not checkpoint.is_file() or _file_sha256(checkpoint) != resume_digest:
                raise budget.CheckpointMismatch("incomplete optimizer update checkpoint does not match its ledger receipt")
            if previous_reservation.get("repair_reason") == budget.UPDATE0_RETRY_REASON:
                repair_info = ledger.validate_update0_retry_state()
        else:
            raise budget.ResourceBlocked(f"failed optimizer update is immutable and cannot be replayed: {unit_id}")
    run_id = f"{unit_id}-{uuid.uuid4().hex[:16]}"
    cap = float(estimates[unit_id])
    reservation = ledger.reserve(
        run_id=run_id, unit_id=unit_id, role="builder",
        reserved_cpu_seconds=cap, unit_kind="training_update",
        previous_run_id=previous_run_id, checkpoint_sha256=resume_digest,
        repair_reason=None if repair_info is None else repair_info["repair_reason"],
        repair_manifest_sha256=None if repair_info is None else repair_info["repair_manifest_sha256"],
        repair_predecessor_run_id=None if repair_info is None else repair_info["repair_predecessor_run_id"],
        repair_predecessor_receipt_sha256=None if repair_info is None else repair_info["repair_predecessor_receipt_sha256"],
    )
    worker_dir = _inside_artifacts(ARTIFACT_ROOT / "units" / f"UPDATE-{block}")
    worker_dir.mkdir(parents=True, exist_ok=True)
    job_path = _inside_artifacts(worker_dir / f"worker-job-{run_id}.json")
    stdout_path = _inside_artifacts(worker_dir / f"worker-log-{run_id}.txt")
    runtime_root = _inside_artifacts(worker_dir / "runtime" / run_id)
    result_path = _inside_artifacts(worker_dir / f"result-{run_id}.json")
    job = {
        "schema_id": WORKER_SCHEMA,
        "package_commit": ledger.binding.package_commit,
        "source_lock_sha256": ledger.binding.source_lock_sha256,
        "run_id": run_id, "unit_id": unit_id, "phase": "update",
        "block_index": block, "previous_run_id": previous_run_id,
        "checkpoint_sha256": resume_digest, "result_path": str(result_path),
        "worker_log_path": str(stdout_path), "runtime_root": str(runtime_root),
        "repair_reason": reservation.get("repair_reason"),
        "repair_manifest_sha256": reservation.get("repair_manifest_sha256"),
        "repair_predecessor_run_id": reservation.get("repair_predecessor_run_id"),
        "repair_predecessor_receipt_sha256": reservation.get("repair_predecessor_receipt_sha256"),
    }
    _write_once(job_path, job)
    env = _worker_environment(runtime_root)
    started = time.monotonic()
    envelope = _supervise_batch([{
        "unit_id": unit_id, "run_id": run_id, "job_path": job_path,
        "env": env, "reserved_cpu_seconds": cap,
    }])[run_id]
    measured = float(envelope["measured_cpu_seconds"])
    if envelope["status"] != "SUCCEEDED" or measured > cap or not result_path.is_file():
        checkpoint_path = _inside_artifacts(ARTIFACT_ROOT / "models" / f"block-{block}-step.pt")
        checkpoint_sha = _file_sha256(checkpoint_path) if checkpoint_path.is_file() else None
        resumable = checkpoint_sha is not None and measured <= cap
        _finish_failed_unit(
            ledger, run_id=run_id, unit_id=unit_id, directory=worker_dir,
            measured=measured, cap=cap,
            boundary=str(envelope.get("boundary", envelope["status"])),
            started=started, status="incomplete" if resumable else "failed",
            checkpoint_sha256=checkpoint_sha if resumable else None,
        )
        raise budget.ResourceBlocked(
            f"training update stopped at its reserved supervisor boundary: {unit_id}"
        )
    result = _read_json(result_path)
    _validate_success_result(
        ledger, unit_id=unit_id, run_id=run_id, reservation=reservation,
        result=result, result_path=result_path,
    )
    ledger.finish(
        run_id=run_id, measured_cpu_seconds=measured, status="succeeded",
        result_sha256=_file_sha256(result_path),
    )
    return result


def _worker_owner(job: Mapping[str, Any], ledger: Any) -> dict[str, Any]:
    from algorithm import (
        InvocationBudget, SEARCH_CPU_CAP_SECONDS, SEARCH_CPU_STOP_MARGIN_SECONDS,
        run_search, derive_attempt_seed, write_immutable_json,
    )
    import runtime

    row = dict(job["row"])
    phase = str(job["phase"])
    arm = str(row["arm"])
    seed = int(row["game_seed"])
    policy_rng_seed = row.get("policy_rng_seed")
    output = _inside_artifacts(Path(str(job["output_dir"])))
    output.mkdir(parents=True, exist_ok=True)
    model_label, proxy = _arm_policy(phase, arm, row)
    if arm == "E":
        stage1_source = _read_json(STAGE1 / "config.json")["source"]
        registered_source = stage1_source.get("registered_stage0_config")
        if not isinstance(registered_source, Mapping):
            raise PermissionError("isolated Expert reference lacks its locked Stage 0 source provenance")
        source_commit = registered_source.get("stsr_source_commit")
        if not isinstance(source_commit, str) or len(source_commit) != 40 or any(
            char not in "0123456789abcdef" for char in source_commit
        ):
            raise PermissionError("isolated Expert reference has an invalid locked STSRL source commit")
        digest = _sha256(_canonical({"expert": "expert_non_combat_v1", "source": source_commit}))
        policy = None
        policy_source = {
            "source": "isolated_expert_reference",
            "source_commit": source_commit,
            "policy_digest": digest,
            "formal_use": True,
        }
    else:
        policy, digest, policy_source = _load_model(model_label, proxy=proxy)
    config, runner, module, encoder = runtime.initialize_native_runtime(
        output, sampling=True, stage1_binding=ledger.binding,
    )
    search_receipts: list[dict[str, Any]] = []
    all_joint_trials: list[Any] = []
    search_seeds = [int(item) for item in row.get("search_invocation_seeds", ())]
    root_cap = 4 if phase == "training" else (7 if arm in {"R", "J"} else 0)
    root_counter = 0
    search_arm = "J" if phase == "training" else arm

    def run_root_search(root_record: Mapping[str, Any], *, select_action: bool) -> int | None:
        nonlocal root_counter
        index = root_counter
        root_counter += 1
        if index >= root_cap:
            return None
        if index >= len(search_seeds):
            raise PermissionError("root opportunity has no pre-registered invocation seed")
        natural_root_index = int(root_record["candidate_index"])
        locked = runtime._safe_root_capture(root_record)
        if not 0 <= natural_root_index < len(locked["legal_action_identities"]):
            raise PermissionError("sampled natural policy action is not a live legal root occurrence")
        root = runtime.LockedRoot(
            owner_seed=seed,
            root_id=f"{job['run_id']}-ROOT-{index}",
            decision_index=locked["decision_index"],
            policy_decision_index=locked["policy_decision_index"],
            screen=locked["screen"],
            checkpoint=locked["checkpoint"],
            adapter=locked["adapter"],
            legal_identities=locked["legal_action_identities"],
            fingerprints=locked["fingerprints"],
            projection_digest=locked["projection_digest"],
            feature_matrix_sha256=locked["feature_matrix_sha256"],
        )
        invocation_seed = search_seeds[index]
        attempt_dir = _inside_artifacts(
            ARTIFACT_ROOT / "search" / _unit_key(str(row["unit_id"])) / str(job["run_id"]) / f"root-{index}"
        )
        attempt_dir.mkdir(parents=True, exist_ok=True)
        invocation_start_cpu = runtime.process_tree_cpu_seconds()
        invocation_budget = InvocationBudget(
            cpu_cap_seconds=SEARCH_CPU_CAP_SECONDS,
            started_attempt_cap=16,
            cpu_reader=runtime.process_tree_cpu_seconds,
        )
        completed: dict[int, Any] = {}
        mutation_attempts: dict[int, dict[str, Any]] = {}

        def attempt_runner(attempt_index: int, root_action_index: int, mutation_target: Any | None):
            parent = None
            if mutation_target is not None:
                parent = completed.get(int(mutation_target.parent_attempt_index))
                if parent is None:
                    raise RuntimeError("joint search mutation parent was not retained")
            elapsed = runtime.process_tree_cpu_seconds() - invocation_start_cpu
            remaining = SEARCH_CPU_CAP_SECONDS - elapsed
            if remaining <= SEARCH_CPU_STOP_MARGIN_SECONDS + 0.1:
                from algorithm import SearchBudgetExhausted
                raise SearchBudgetExhausted("search invocation CPU safety margin reached before another continuation")
            return runtime.execute_policy_trajectory(
                runner=runner,
                module=module,
                encoder=encoder,
                output_dir=attempt_dir,
                policy=policy,
                owner_seed=seed,
                root=root,
                attempt_index=attempt_index,
                invocation_seed=invocation_seed,
                root_action_index=root_action_index if root_action_index >= 0 else None,
                mutation_target=mutation_target,
                parent_trial=parent,
                cpu_cap_seconds=min(SEARCH_CPU_CAP_SECONDS, remaining),
                transition_guard=lambda: invocation_budget.guard_transition(
                    safety_margin_seconds=SEARCH_CPU_STOP_MARGIN_SECONDS,
                ),
                policy_rng_seed=derive_attempt_seed(STUDY_ID, root.root_id, invocation_seed, attempt_index, "policy-suffix"),
            )

        def attempt_started(attempt_index: int, root_action_index: int, target: Any | None) -> None:
            if target is not None:
                parent = completed.get(int(target.parent_attempt_index))
                if parent is None:
                    raise RuntimeError("joint search mutation parent disappeared before its attempt")
                try:
                    future_index = next(
                        index for index, decision in enumerate(parent.decisions)
                        if decision is target.parent_decision
                    )
                except StopIteration as exc:
                    raise RuntimeError("joint mutation target is not in its completed parent") from exc
                mutation_attempts[attempt_index] = {
                    "attempt_index": int(attempt_index),
                    "parent_attempt_index": int(target.parent_attempt_index),
                    "parent_decision_index": int(future_index),
                    "parent_decision_step_index": int(target.parent_decision.step_index),
                    "legal_occurrence_count": len(target.parent_decision.legal_identities),
                    "alternative_differs_from_parent_action": True,
                }
            write_immutable_json(attempt_dir / f"attempt-{attempt_index:02d}-started.json", {
                "schema_id": "issue75-stage1-search-attempt-start-v1",
                "unit_id": row["unit_id"],
                "root_id": root.root_id,
                "arm": search_arm,
                "attempt_index": attempt_index,
                "root_action_index": root_action_index,
                "mutation_parent_attempt_index": None if target is None else target.parent_attempt_index,
                "mutation_step_index": None if target is None else target.parent_decision.step_index,
                "mutation_alternative_index": None if target is None else target.alternative_index,
                "invocation_seed": invocation_seed,
                "model_digest": digest,
            })

        def attempt_result(trial: Any, root_action_index: int, attempt_index: int, target: Any | None) -> None:
            if trial.complete:
                completed[attempt_index] = trial
                all_joint_trials.append(trial)
            write_immutable_json(attempt_dir / f"attempt-{attempt_index:02d}-result.json", {
                "schema_id": "issue75-stage1-search-attempt-result-v1",
                "unit_id": row["unit_id"],
                "root_id": root.root_id,
                "arm": search_arm,
                "attempt_index": attempt_index,
                "root_action_index": root_action_index,
                "mutation_parent_attempt_index": None if target is None else target.parent_attempt_index,
                "mutation_step_index": None if target is None else target.parent_decision.step_index,
                "result": runtime._safe_trial_payload(trial),
                "model_digest": digest,
            })
            if target is not None:
                receipt = mutation_attempts[int(attempt_index)]
                receipt["complete_terminal"] = bool(trial.complete)
                receipt["trajectory_decision_count"] = len(trial.decisions)

        try:
            hard_remaining = SEARCH_CPU_CAP_SECONDS - (
                runtime.process_tree_cpu_seconds() - invocation_start_cpu
            )
            alarm_seconds = hard_remaining - SEARCH_CPU_STOP_MARGIN_SECONDS
            if alarm_seconds <= 0.1:
                from algorithm import SearchBudgetExhausted
                raise SearchBudgetExhausted("search invocation CPU safety margin reached before native search")
            with runtime.process_cpu_alarm(alarm_seconds):
                outcome = run_search(
                    arm=search_arm,
                    study_id=STUDY_ID,
                    root_id=root.root_id,
                    invocation_seed=invocation_seed,
                    root_legal_identities=root.legal_identities,
                    attempt_runner=attempt_runner,
                    budget=invocation_budget,
                    attempt_started_recorder=attempt_started,
                    attempt_result_recorder=attempt_result,
                )
        finally:
            root.adapter.restore_checkpoint(root.checkpoint)
            root_record.clear()
        search_selected = None
        if outcome.selected_root_identity is not None:
            target = _canonical(outcome.selected_root_identity)
            for candidate_index, identity in enumerate(root.legal_identities):
                if _canonical(identity) == target:
                    search_selected = candidate_index
                    break
            if search_selected is None:
                raise RuntimeError("selected trajectory root identity is not a current legal occurrence")
        selected = search_selected if select_action else None
        action_change = None
        if search_selected is not None:
            action_change = search_selected != natural_root_index
        search_receipts.append({
            "root_index": index,
            "root_id": root.root_id,
            "arm": search_arm,
            "status": outcome.status,
            "started_attempts": outcome.started_attempts,
            "completed_trajectories": outcome.completed_trajectories,
            "mutation_attempts_started": outcome.mutation_attempts_started,
            "cpu_seconds": outcome.cpu_seconds,
            "attempt_cpu_seconds": list(outcome.attempt_cpu_seconds),
            "stop_reason": outcome.stop_reason,
            "selected_root_identity_sha256": None if outcome.selected_root_identity is None else _sha256(_canonical(outcome.selected_root_identity)),
            "natural_policy_root_identity_sha256": _sha256(_canonical(root.legal_identities[natural_root_index])),
            "root_action_changed_from_sampled_policy": action_change,
            "mutation_attempts": [mutation_attempts[index] for index in sorted(mutation_attempts)],
            "registered_cpu_cap_seconds": SEARCH_CPU_CAP_SECONDS,
            "operational_stop_margin_cpu_seconds": SEARCH_CPU_STOP_MARGIN_SECONDS,
            "native_rng_source": outcome.native_rng_source,
            "model_digest": digest,
        })
        return selected

    def root_callback(root_record: dict[str, Any]) -> int | None:
        if root_counter >= root_cap:
            root_record.clear()
            return None
        try:
            return run_root_search(root_record, select_action=phase != "training")
        finally:
            # Search restores this live native game before releasing the private checkpoint.
            root_record.clear()

    capture_checkpoints = root_cap > 0
    capture_training = phase == "training"
    try:
        result = runner._run_one(
            out=output,
            module=module,
            model=None if policy is None else runtime.PolicyAdapter(policy),
            model_digest=digest,
            seed=seed,
            arm="TRAIN" if phase == "training" else arm,
            encoder=encoder,
            durable=False,
            capture_trace=capture_training,
            capture_checkpoints=capture_checkpoints,
            capture_root_limit=root_cap if root_cap else None,
            capture_training_features=capture_training,
            root_callback=root_callback if root_cap else None,
            policy_rng_seed=None if policy_rng_seed is None else int(policy_rng_seed),
            purpose=f"stage1-{phase}-{arm}-{seed}",
        )
        terminal = result["terminal"]
        training_pointer = None
        if capture_training:
            base = _base_trial(result, seed)
            joint = [trial for trial in all_joint_trials if trial.complete]
            owner_features = {
                "schema_id": FEATURE_SCHEMA,
                "unit_id": str(row["unit_id"]),
                "owner_seed": seed,
                "package_commit": ledger.binding.package_commit,
                "source_lock_sha256": ledger.binding.source_lock_sha256,
                "base": _serialize_trial(base, runtime),
                "joint_trajectories": [_serialize_trial(trial, runtime) for trial in joint],
                "search_receipts": search_receipts,
            }
            training_pointer = _save_training_features(output.parent, owner_features)
        report = {
            "schema_id": UNIT_SCHEMA,
            "status": "SUCCEEDED",
            "package_commit": ledger.binding.package_commit,
            "source_lock_sha256": ledger.binding.source_lock_sha256,
            "study_id": STUDY_ID,
            "unit_id": row["unit_id"],
            "phase": phase,
            "formal": bool(row["formal"]),
            "owner_seed": seed,
            "game_seed": int(row["game_seed"]),
            "arm": arm,
            "paired_owner_key": row["paired_owner_key"],
            "terminal_outcome": str(terminal["outcome"]),
            "terminal_floor": int(terminal["terminal_floor"]),
            "terminal_act": int(terminal["terminal_act"]),
            "heart_victory": bool(terminal["heart_victory"]),
            "act2_plus": bool(terminal["act2_plus"]),
            "act3_plus": bool(terminal["act3_plus"]),
            "act4_reached": bool(terminal["act4_reached"]),
            "completed_transitions": int(result["completed_transitions"]),
            "native_source_commit": config["source"]["sts_lightspeed_commit"],
            "stsr_source_commit": config["source"]["stsr_source_commit"],
            "native_module_sha256": config["source"]["native_module_sha256"],
            "model_digest": digest,
            "policy_source": policy_source,
            "expert_decisions": int(result.get("expert_decisions", 0)),
            "search": search_receipts,
            "training_features": training_pointer,
            "resource": {
                "worker_process_cpu_seconds": float(result.get("cpu_seconds", 0.0)),
                "worker_wall_seconds": float(result.get("wall_seconds", 0.0)),
                "ledger_run_id": str(job["run_id"]),
            },
        }
        return report
    finally:
        for root_record in getattr(result if "result" in locals() else {}, "_volatile_roots", ()):
            root_record.clear()

def _worker_update(job: Mapping[str, Any], ledger: Any) -> dict[str, Any]:
    if str(job["unit_id"]) != f"UPDATE:{int(job['block_index'])}":
        raise PermissionError("update worker unit/block identifiers disagree")
    result = _update_block(
        ledger, int(job["block_index"]), estimates={str(job["unit_id"]): 1.0},
        ledger_run_id=str(job["run_id"]),
    )
    result_path = _inside_artifacts(Path(str(job["result_path"])))
    _write_once(result_path, result)
    return result


def _supervisor_main(job_path: Path) -> int:
    started_cpu = time.process_time()
    unit_id, run_id = "", ""
    cap = 0.0
    status, measured, boundary = "SUPERVISOR_FAILED", 0.0, "supervisor_validation"
    try:
        with _supervisor_activity_lease():
            budget.verify_stage1_package()
            job_path = _inside_artifacts(job_path)
            job = _read_json(job_path)
            if job.get("schema_id") != WORKER_SCHEMA:
                raise PermissionError("supervisor job schema differs from the locked executor")
            unit_id, run_id = str(job.get("unit_id", "")), str(job.get("run_id", ""))
            if job.get("phase") not in {"training", "heldout", "update"}:
                raise PermissionError("only formal owner/update jobs may use a supervisor process")
            budget.verify_stage1_phase(str(job["phase"]))
            if len(run_id) != len(unit_id) + 17 or not run_id.startswith(unit_id + "-"):
                raise PermissionError("supervisor run ID is not unit scoped")
            unit_dir = _unit_directory(unit_id)
            if job_path != _inside_artifacts(unit_dir / f"worker-job-{run_id}.json"):
                raise PermissionError("supervisor job path differs from its reserved unit")
            ledger = budget.open_reserved_stage1_worker_ledger(
                ARTIFACT_ROOT / "ledger", run_id=run_id, unit_id=unit_id,
            )
            reservation = ledger.require_open_reservation(run_id=run_id, unit_id=unit_id)
            if (
                job.get("package_commit") != ledger.binding.package_commit
                or job.get("source_lock_sha256") != ledger.binding.source_lock_sha256
                or reservation.get("unit_kind") not in {"owner_arm", "training_update"}
                or job.get("previous_run_id") != reservation.get("previous_run_id")
                or job.get("checkpoint_sha256") != reservation.get("checkpoint_sha256")
                or job.get("repair_reason") != reservation.get("repair_reason")
                or job.get("repair_manifest_sha256") != reservation.get("repair_manifest_sha256")
                or job.get("repair_predecessor_run_id") != reservation.get("repair_predecessor_run_id")
                or job.get("repair_predecessor_receipt_sha256") != reservation.get("repair_predecessor_receipt_sha256")
            ):
                raise PermissionError("supervisor job differs from its write-ahead reservation")
            cap = float(reservation["reserved_cpu_seconds"])
            if cap <= 0.5:
                raise PermissionError("supervised unit reservation cannot fund supervisor startup")
            runtime_root = _inside_artifacts(unit_dir / "runtime" / run_id)
            if Path(str(job.get("runtime_root"))).resolve() != runtime_root:
                raise PermissionError("supervisor runtime directory differs from its reserved unit")
            worker_log = _inside_artifacts(unit_dir / f"worker-log-{run_id}.txt")
            if Path(str(job.get("worker_log_path"))).resolve() != worker_log:
                raise PermissionError("supervisor worker log path differs from its reserved unit")
            if job["phase"] == "update":
                block = int(job.get("block_index", -1))
                expected_dir = _inside_artifacts(ARTIFACT_ROOT / "units" / f"UPDATE-{block}")
                if unit_id != f"UPDATE:{block}" or reservation["unit_kind"] != "training_update":
                    raise PermissionError("supervisor update differs from the registered block reservation")
                expected_result = expected_dir / f"result-{run_id}.json"
            else:
                phase_name = {"training": "training", "heldout": "heldout"}[str(job["phase"])]
                row = job.get("row")
                registered = next((item for item in _rows() if item["unit_id"] == unit_id), None)
                if (
                    not isinstance(row, dict) or registered is None or row != registered
                    or registered["phase"] != phase_name or registered.get("formal") is not True
                    or reservation["unit_kind"] != "owner_arm"
                ):
                    raise PermissionError("supervisor owner row differs from the formal seed table")
                run_dir = _run_artifact_directory(unit_id, run_id)
                expected_result = run_dir / "result.json"
                if Path(str(job.get("output_dir"))).resolve() != run_dir / "native":
                    raise PermissionError("supervisor native output path differs from its registered unit")
            if Path(str(job.get("result_path"))).resolve() != expected_result:
                raise PermissionError("supervisor result path differs from its reserved unit")
            child_env = os.environ.copy()
            child_env.update({
                "TMPDIR": str(runtime_root / "tmp"), "TMP": str(runtime_root / "tmp"),
                "TEMP": str(runtime_root / "tmp"),
                "XDG_CACHE_HOME": str(runtime_root / "cache"),
                "TORCH_HOME": str(runtime_root / "cache" / "torch"),
                "PYTHONPYCACHEPREFIX": str(runtime_root / "cache" / "pycache"),
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            before_run_cpu = time.process_time()
            try:
                process = budget.ProcessTreeWatchdog().run(
                    [sys.executable, str(Path(__file__).resolve()), "--worker-job", str(job_path)],
                    reserved_cpu_seconds=cap - 0.5, cwd=ROOT, env=child_env,
                    stdout_path=worker_log,
                )
                measured = max(
                    0.0,
                    process.cpu_seconds - process.controller_cpu_seconds
                    + time.process_time() - started_cpu,
                )
                status = "SUCCEEDED" if process.returncode == 0 else "WORKER_FAILED"
                boundary = "worker_returned_nonzero" if process.returncode != 0 else "complete"
            except budget.ProcessTreeBudgetExceeded as exc:
                measured = max(
                    0.0,
                    exc.cpu_seconds - exc.controller_cpu_seconds
                    + time.process_time() - started_cpu,
                )
                status, boundary = "PROCESS_TREE_CPU_LIMIT", "process_tree_cpu_cap"
            except BaseException as exc:
                measured = cap
                status, boundary = "SUPERVISOR_FAILED", type(exc).__name__
    except BaseException as exc:
        if cap:
            measured = max(measured, cap)
        status, boundary = "SUPERVISOR_FAILED", type(exc).__name__
    measured = max(measured, max(0.0, time.process_time() - started_cpu))
    payload = {
        "schema_id": SUPERVISOR_SCHEMA,
        "unit_id": unit_id, "run_id": run_id, "status": status,
        "measured_cpu_seconds": measured, "reserved_cpu_seconds": cap,
        "boundary": boundary,
    }
    print(_canonical(payload).decode("utf-8"))
    return 0 if status == "SUCCEEDED" and measured <= cap else 2


def _validate_worker_job(
    job_path: Path, *, allow_synthetic: bool = False,
) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    """Validate a parent-written job and its open source-bound ledger reservation."""
    budget.verify_stage1_package()
    job = _read_json(job_path)
    if job.get("schema_id") != WORKER_SCHEMA:
        raise PermissionError("worker job schema differs from the locked executor")
    unit_id = str(job.get("unit_id", ""))
    run_id = str(job.get("run_id", ""))
    phase = str(job.get("phase", ""))
    synthetic = phase == "validation_probe"
    if synthetic:
        if not allow_synthetic:
            raise PermissionError("synthetic worker validation is test-only")
    else:
        budget.verify_stage1_phase(phase)
    if len(run_id) != len(unit_id) + 17 or not run_id.startswith(unit_id + "-"):
        raise PermissionError("worker run ID is not a unique unit-scoped reservation ID")
    suffix = run_id[len(unit_id) + 1:]
    if len(suffix) != 16 or any(char not in "0123456789abcdef" for char in suffix):
        raise PermissionError("worker run ID suffix is malformed")
    ledger = budget.open_reserved_stage1_worker_ledger(
        ARTIFACT_ROOT / "ledger", run_id=run_id, unit_id=unit_id,
    )
    if (
        job.get("package_commit") != ledger.binding.package_commit
        or job.get("source_lock_sha256") != ledger.binding.source_lock_sha256
    ):
        raise PermissionError("worker job is not bound to the active ledger reservation")
    reservation = ledger.require_open_reservation(run_id=run_id, unit_id=unit_id)
    if (
        job.get("previous_run_id") != reservation.get("previous_run_id")
        or job.get("checkpoint_sha256") != reservation.get("checkpoint_sha256")
        or job.get("retry_reason") != reservation.get("retry_reason")
        or job.get("pre_game_start_proof") != reservation.get("pre_game_start_proof")
        or job.get("pre_game_start_proof_sha256") != reservation.get("pre_game_start_proof_sha256")
        or job.get("repair_reason") != reservation.get("repair_reason")
        or job.get("repair_manifest_sha256") != reservation.get("repair_manifest_sha256")
        or job.get("repair_predecessor_run_id") != reservation.get("repair_predecessor_run_id")
        or job.get("repair_predecessor_receipt_sha256") != reservation.get("repair_predecessor_receipt_sha256")
    ):
        raise PermissionError("worker resume/retry metadata differs from the durable ledger reservation")
    expected_job_path = _inside_artifacts(_unit_directory(unit_id) / f"worker-job-{run_id}.json")
    if job_path.resolve() != expected_job_path:
        raise PermissionError("worker job path differs from its reserved unit")

    unit_dir = _unit_directory(unit_id)
    result_path = _inside_artifacts(Path(str(job.get("result_path", ""))))
    if synthetic:
        expected_row = {
            "unit_id": unit_id, "formal": False, "kind": "nonformal_validation",
        }
        output_path = _inside_artifacts(Path(str(job.get("output_dir", ""))))
        if (
            not unit_id.startswith("VALIDATION:")
            or job.get("row") != expected_row
            or reservation.get("unit_kind") != "validation_probe"
            or output_path != unit_dir / "native"
            or result_path != unit_dir / "result.json"
            or output_path.exists() or result_path.exists()
        ):
            raise PermissionError("synthetic worker probe must be nonformal and must not enter game execution")
        return job, ledger, dict(reservation)

    if phase == "update":
        block = int(job.get("block_index", -1))
        expected_dir = _inside_artifacts(ARTIFACT_ROOT / "units" / f"UPDATE-{block}")
        if unit_id != f"UPDATE:{block}" or reservation.get("unit_kind") != "training_update":
            raise PermissionError("optimizer worker unit differs from its registered update block")
        if result_path != expected_dir / f"result-{run_id}.json":
            raise PermissionError("optimizer worker result path differs from its reserved update")
        return job, ledger, dict(reservation)

    phase_name = {"preflight": "preflight", "training": "training", "heldout": "heldout"}.get(phase)
    row = job.get("row")
    if phase_name is None or not isinstance(row, dict) or row.get("unit_id") != unit_id:
        raise PermissionError("owner worker is not bound to a registered unit phase")
    registered = next((item for item in _rows() if item["unit_id"] == unit_id), None)
    if registered is None or row != registered or registered.get("phase") != phase_name:
        raise PermissionError("owner worker row differs from the exact locked seed-table record")
    if reservation.get("unit_kind") != "owner_arm":
        raise PermissionError("owner worker does not hold an owner-arm reservation")
    output_path = _inside_artifacts(Path(str(job.get("output_dir", ""))))
    if phase_name == "preflight":
        expected_owner_dir = unit_dir
    else:
        expected_owner_dir = _run_artifact_directory(unit_id, run_id)
    if output_path != expected_owner_dir / "native":
        raise PermissionError("owner worker native output path differs from its registered unit/run")
    if result_path != expected_owner_dir / "result.json":
        raise PermissionError("owner worker result path differs from its registered unit/run")
    return job, ledger, dict(reservation)


def _worker_main(job_path: Path) -> int:
    job, ledger, reservation = _validate_worker_job(job_path)
    import torch
    execution = _read_json(STAGE1 / "config.json")["execution"]
    intraop = int(execution.get("torch_intraop_threads", 0))
    interop = int(execution.get("torch_interop_threads", 0))
    if intraop != 1 or interop != 1:
        raise PermissionError("worker Torch thread counts differ from the locked bounded CPU profile")
    torch.set_num_threads(intraop)
    if torch.get_num_interop_threads() != interop:
        torch.set_num_interop_threads(interop)
    unit_id = str(job["unit_id"])
    run_id = str(job["run_id"])
    if job.get("phase") == "update":
        result = _worker_update(job, ledger)
    else:
        result = _worker_owner(job, ledger)
        _write_once(_inside_artifacts(Path(str(job["result_path"]))), result)
    return 0 if result.get("status") == "SUCCEEDED" else 2


def _all_units_succeeded(ledger: Any, rows: Sequence[Mapping[str, Any]]) -> bool:
    runs, units = _event_state(ledger)
    return all(
        row["unit_id"] in units
        and runs[units[row["unit_id"]][-1]]["finish"] is not None
        and runs[units[row["unit_id"]][-1]]["finish"].get("status") == "succeeded"
        for row in rows
    )


def _paired_differences(
    outcomes: Sequence[Mapping[str, Any]], left: str, right: str, field: str,
) -> list[float]:
    left_rows = [row for row in outcomes if row.get("arm") == left]
    right_rows = [row for row in outcomes if row.get("arm") == right]
    left_map = {str(row["paired_owner_key"]): row for row in left_rows}
    right_map = {str(row["paired_owner_key"]): row for row in right_rows}
    if len(left_map) != 96 or len(right_map) != 96 or set(left_map) != set(right_map):
        raise PermissionError("paired analysis requires the same exact 96 registered owners in both arms")
    return [
        float(left_map[key][field]) - float(right_map[key][field])
        for key in sorted(left_map)
    ]


def _bootstrap_ci_95(
    differences: Sequence[float], *, seed_sha256: str, replicates: int = BOOTSTRAP_REPLICATES,
) -> dict[str, Any]:
    if len(differences) != 96 or replicates < 1 or len(seed_sha256) != 64:
        raise ValueError("paired-owner bootstrap input does not match the frozen 96-owner plan")
    values = [float(value) for value in differences]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("paired-owner bootstrap cannot accept non-finite outcome differences")
    rng = random.Random(int(seed_sha256[:16], 16))
    count = len(values)
    means = [
        sum(values[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(replicates)
    ]
    means.sort()

    def nearest_rank(probability: float) -> float:
        index = max(0, min(len(means) - 1, math.ceil(probability * len(means)) - 1))
        return means[index]

    return {
        "confidence_level": 0.95,
        "method": "paired-owner bootstrap; resample 96 owner pairs with replacement",
        "replicates": int(replicates),
        "quantile_method": "nearest-rank",
        "lower": nearest_rank(0.025),
        "upper": nearest_rank(0.975),
        "seed_sha256": seed_sha256,
    }


def _paired_bootstrap_seed(contrast: str, metric: str) -> str:
    seed_table_sha = budget.normalized_file_sha256(STAGE1 / "seed-table.json")
    material = f"issue75-paired-owner-bootstrap-v1|{seed_table_sha}|{contrast}|{metric}".encode("ascii")
    return _sha256(material)


def _search_coverage(outcomes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for arm in ARMS:
        rows = [row for row in outcomes if row.get("arm") == arm]
        receipts = [search for row in rows for search in row.get("search", ())]
        mutation_attempts = [
            attempt for search in receipts for attempt in search.get("mutation_attempts", ())
        ]
        action_changes = [
            search["root_action_changed_from_sampled_policy"] for search in receipts
            if search.get("root_action_changed_from_sampled_policy") is not None
        ]
        statuses: dict[str, int] = {}
        for search in receipts:
            status = str(search.get("status", "UNKNOWN"))
            statuses[status] = statuses.get(status, 0) + 1
        result[arm] = {
            "owner_rows": len(rows),
            "root_opportunities_searched": len(receipts),
            "search_status_counts": statuses,
            "started_attempts": sum(int(search.get("started_attempts", 0)) for search in receipts),
            "completed_terminal_trajectories": sum(int(search.get("completed_trajectories", 0)) for search in receipts),
            "search_attempt_cpu_seconds": sum(
                float(search.get("cpu_seconds", 0.0)) for search in receipts
            ),
            "mutation_attempts_started": len(mutation_attempts),
            "mutation_terminal_trajectories": sum(bool(item.get("complete_terminal")) for item in mutation_attempts),
            "mutation_at_or_after_second_future_choice": sum(
                int(item.get("parent_decision_index", -1)) >= 2 for item in mutation_attempts
            ),
            "root_actions_changed_from_sampled_policy": sum(bool(value) for value in action_changes),
            "root_action_change_observations": len(action_changes),
            "root_action_change_exposure_rate": (
                sum(bool(value) for value in action_changes) / len(action_changes)
                if action_changes else None
            ),
            "unit_process_tree_cpu_seconds": sum(
                float(row.get("resource", {}).get("measured_process_tree_cpu_seconds") or 0.0)
                for row in rows
            ),
            "unit_resource_status_counts": {
                status: sum(row.get("resource", {}).get("status") == status for row in rows)
                for status in sorted({str(row.get("resource", {}).get("status", "unknown")) for row in rows})
            },
        }
    return result


def _build_evaluation_analysis(outcomes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    paired = {}
    for left, right in (("J", "P1"), ("J", "E"), ("P1", "P0"), ("J", "R")):
        contrast = f"{left}-{right}"
        floor_diffs = _paired_differences(outcomes, left, right, "terminal_floor")
        heart_diffs = _paired_differences(outcomes, left, right, "heart_victory")
        paired[contrast] = {
            "n": len(floor_diffs),
            "mean_terminal_floor_difference": sum(floor_diffs) / len(floor_diffs),
            "heart_win_difference": int(sum(heart_diffs)),
            "mean_heart_win_difference": sum(heart_diffs) / len(heart_diffs),
            "paired_differences": floor_diffs,
            "terminal_floor_bootstrap_ci_95": _bootstrap_ci_95(
                floor_diffs, seed_sha256=_paired_bootstrap_seed(contrast, "terminal-floor"),
            ),
            "heart_win_bootstrap_ci_95": _bootstrap_ci_95(
                heart_diffs, seed_sha256=_paired_bootstrap_seed(contrast, "heart-win"),
            ),
        }
    return {
        "paired_terminal_floor_contrasts": paired,
        "search_coverage_and_resource_summary": _search_coverage(outcomes),
        "bootstrap_contract": {
            "replicates_per_contrast_and_metric": BOOTSTRAP_REPLICATES,
            "owner_pair_count": 96,
            "seed_source": "SHA256 of locked seed-table digest plus contrast/metric analysis domain",
            "outcome_metrics": ["terminal_floor", "heart_win"],
            "confidence_level": 0.95,
        },
    }


def _run_report_analysis(
    ledger: Any, outcomes: Sequence[Mapping[str, Any]], *,
    accounting_sink: dict[str, float] | None = None,
) -> dict[str, Any]:
    unit_id = "REPORT:HELDOUT"
    existing = _existing_unit_receipt(ledger, unit_id)
    if existing is not None:
        return existing
    run_id = f"{unit_id}-{uuid.uuid4().hex[:16]}"
    cap = ANALYSIS_REPORT_RESERVATION
    reservation = ledger.reserve(
        run_id=run_id, unit_id=unit_id, role="builder",
        reserved_cpu_seconds=cap, unit_kind="analysis_report",
    )
    result_path = _unit_directory(unit_id) / "result.json"
    started = time.process_time()
    try:
        analysis = _build_evaluation_analysis(outcomes)
        measured = max(0.0, time.process_time() - started)
        result = {
            "schema_id": UNIT_SCHEMA,
            "status": "SUCCEEDED",
            "unit_id": unit_id,
            "package_commit": ledger.binding.package_commit,
            "source_lock_sha256": ledger.binding.source_lock_sha256,
            "resource": {"ledger_run_id": run_id},
            "analysis": analysis,
            "analysis_cpu_seconds_before_ledger_finalize": measured,
        }
        _write_once(result_path, result)
        measured = max(0.0, time.process_time() - started)
        if accounting_sink is not None:
            accounting_sink["cpu_seconds"] = measured
        _validate_success_result(
            ledger, unit_id=unit_id, run_id=run_id, reservation=reservation,
            result=result, result_path=result_path,
        )
        if measured > cap:
            try:
                ledger.finish(run_id=run_id, measured_cpu_seconds=measured, status="failed")
            except budget.BudgetOverrun:
                pass
            raise budget.ResourceBlocked("paired-owner report analysis exceeded its 120 CPU-s reservation")
        ledger.finish(
            run_id=run_id, measured_cpu_seconds=measured, status="succeeded",
            result_sha256=_file_sha256(result_path),
        )
        return result
    except BaseException as exc:
        current, _, finish = _existing_unit(ledger, unit_id)
        if current == run_id and finish is None:
            measured = max(0.0, time.process_time() - started)
            failure = {
                "schema_id": UNIT_SCHEMA,
                "status": "FAILED",
                "unit_id": unit_id,
                "run_id": run_id,
                "boundary": type(exc).__name__,
                "measured_cpu_seconds": measured,
                "reserved_cpu_seconds": cap,
            }
            _write_once(_unit_directory(unit_id) / f"failure-{run_id}.json", failure)
            try:
                ledger.finish(run_id=run_id, measured_cpu_seconds=measured, status="failed")
            except budget.BudgetOverrun:
                pass
        raise


def _run_preflight(ledger: Any) -> dict[str, Any]:
    previous_report = ARTIFACT_ROOT / "preflight-report.json"
    if previous_report.is_file():
        report = _read_json(previous_report)
        if report.get("status") != "PREFLIGHT_COMPLETE_PENDING_INDEPENDENT_REVIEW":
            raise budget.ResourceBlocked("an immutable failed/incomplete preflight report already exists")
        forecast_path = ARTIFACT_ROOT / "cost-forecast.json"
        forecast = _read_json(forecast_path)
        forecast_status = forecast.get("status")
        if forecast_status == "RESOURCE_BLOCKED":
            return {"status": forecast_status, "report": report, "cost_forecast": forecast}
        if forecast_status != "FITS_IF_LEDGER_ACCEPTS":
            raise budget.ResourceBlocked("retained full-cohort forecast does not permit preflight success")
        return {"status": report["status"], "report": report, "cost_forecast": forecast}
    _, units = _event_state(ledger)
    if any(
        unit in units and (
            (finish := _existing_unit(ledger, unit)[2]) is None or finish.get("status") != "succeeded"
        )
        for unit in (row["unit_id"] for row in _unit_rows("preflight"))
    ):
        _write_preflight_reports(ledger)
    for row in _unit_rows("preflight"):
        if _existing_unit_receipt(ledger, str(row["unit_id"])) is None:
            _run_unit(ledger, row, phase="preflight", estimates=None)
    return _write_preflight_reports(ledger)


def _run_training(ledger: Any) -> dict[str, Any]:
    phase_started_cpu = time.process_time()
    report_path = ARTIFACT_ROOT / "training-report.json"
    if report_path.is_file():
        report = _read_json(report_path)
        if (
            report.get("status") != "TRAINING_COMPLETE"
            or report.get("package_commit") != ledger.binding.package_commit
            or report.get("source_lock_sha256") != ledger.binding.source_lock_sha256
            or report.get("training_seed_ids") != [row["owner_seed"] for row in _unit_rows("train")]
        ):
            raise PermissionError("existing training report is not a complete receipt for this package")
        scheduler_reference = report.get("training_scheduler_receipt", {})
        scheduler_unit = scheduler_reference.get("unit_id", "SCHEDULER:TRAIN")
        scheduler_receipt = _existing_scheduler_receipt(ledger, scheduler_unit)
        if scheduler_reference and (
            scheduler_reference.get("run_id") != scheduler_receipt.get("run_id")
            or scheduler_reference.get("receipt_sha256") != scheduler_receipt.get("receipt_sha256")
        ):
            raise PermissionError("training report differs from its source-bound scheduler receipt")
        return report
    gate = _formal_forecast(ledger, phase="training")
    event_state_sha = _sha256(_canonical(ledger.read_events()))
    forecast_path = _inside_artifacts(
        ARTIFACT_ROOT
        / f"training-continuation-forecast-{ledger.binding.package_commit}-{event_state_sha[:16]}.json"
    )
    _write_once(forecast_path, gate)
    estimates, _ = _forecast_estimates(ledger)
    scheduler_plan = gate.get("training_scheduler_plan") or _training_scheduler_plan(ledger, estimates)
    scheduler_reservation = None
    scheduler_receipt = scheduler_plan.get("receipt")
    if scheduler_plan["action"] in {"reserve", "recover"}:
        scheduler_reservation = _run_scheduler_reservation(
            ledger, str(scheduler_plan["unit_id"]), estimates,
        )
    if scheduler_receipt is None:
        concurrency = _formal_concurrency()
        for block in range(4):
            rows = [
                row for row in _rows()
                if row["phase"] == "training" and int(row["block_index"]) == block
            ]
            for offset in range(0, len(rows), concurrency):
                if scheduler_reservation is not None:
                    _require_scheduler_cpu_headroom(scheduler_reservation, phase_started_cpu)
                _run_owner_batch(
                    ledger, rows[offset:offset + concurrency], phase="train", estimates=estimates,
                )
            if not _all_units_succeeded(ledger, rows):
                raise budget.ResourceBlocked(f"registered 128-owner block {block} is not complete")
            if scheduler_reservation is not None:
                _require_scheduler_cpu_headroom(scheduler_reservation, phase_started_cpu)
            _run_update_unit(ledger, block, estimates=estimates)
        if scheduler_reservation is not None:
            scheduler_receipt = _finish_scheduler_reservation(
                ledger, scheduler_reservation, phase_started_cpu=phase_started_cpu,
            )
        if scheduler_receipt is None:
            scheduler_receipt = _existing_scheduler_receipt(ledger, str(scheduler_plan["unit_id"]))
    else:
        for block in range(4):
            rows = [
                row for row in _rows()
                if row["phase"] == "training" and int(row["block_index"]) == block
            ]
            if not _all_units_succeeded(ledger, rows):
                raise budget.ResourceBlocked("successful scheduler receipt exists before all registered owner rows completed")
            if _existing_unit_receipt(ledger, f"UPDATE:{block}") is None:
                raise budget.ResourceBlocked("successful scheduler receipt exists before its optimizer update completed")
    checkpoints = []
    for block in range(4):
        receipt = _existing_unit_receipt(ledger, f"UPDATE:{block}")
        if receipt is None:
            raise PermissionError(f"completed training block lacks its bound update receipt: {block}")
        checkpoints.append(receipt["model_checkpoint"])
    final_model, final_digest, final_source = _load_model("pi4")
    report = {
        "schema_id": "issue75-stage1-training-report-v1",
        "status": "TRAINING_COMPLETE",
        "package_commit": ledger.binding.package_commit,
        "source_lock_sha256": ledger.binding.source_lock_sha256,
        "scope": "STUDY_ONLY",
        "formal_owner_count": 512,
        "completed_blocks": 4,
        "updates_per_block": 64,
        "p1_formal_model_source": "pi4",
        "p1_formal_model_digest": final_digest,
        "p1_formal_checkpoint": final_source,
        "block_checkpoints": checkpoints,
        "preflight_report_sha256": gate["preflight_report_sha256"],
        "cost_forecast_sha256": gate["cost_forecast_sha256"],
        "expert_decisions": 0,
        "training_seed_ids": [row["owner_seed"] for row in _unit_rows("train")],
        "training_scheduler_receipt": {
            "unit_id": scheduler_receipt.get("unit_id"),
            "run_id": scheduler_receipt.get("run_id"),
            "receipt_sha256": scheduler_receipt.get("receipt_sha256"),
        },
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if len(report["training_seed_ids"]) != 512:
        raise AssertionError("formal training report omitted registered owners")
    _write_once(report_path, report)
    return report


def _run_evaluation(ledger: Any) -> dict[str, Any]:
    phase_started_cpu = time.process_time()
    training_report_path = _inside_artifacts(ARTIFACT_ROOT / "training-report.json")
    if training_report_path.is_file():
        training_report = _read_json(training_report_path)
        if (
            training_report.get("status") != "TRAINING_COMPLETE"
            or training_report.get("package_commit") != ledger.binding.package_commit
            or training_report.get("source_lock_sha256") != ledger.binding.source_lock_sha256
            or training_report.get("training_seed_ids") != [row["owner_seed"] for row in _unit_rows("train")]
        ):
            raise PermissionError("held-out phase requires the complete source-bound training report")
        training_scheduler = training_report.get("training_scheduler_receipt", {})
        training_scheduler_unit = training_scheduler.get("unit_id", "SCHEDULER:TRAIN")
        training_scheduler_receipt = _existing_scheduler_receipt(ledger, training_scheduler_unit)
        if training_scheduler and (
            training_scheduler.get("run_id") != training_scheduler_receipt.get("run_id")
            or training_scheduler.get("receipt_sha256") != training_scheduler_receipt.get("receipt_sha256")
        ):
            raise PermissionError("held-out phase is not bound to the completed training scheduler receipt")
    else:
        _existing_scheduler_receipt(ledger, "SCHEDULER:TRAIN")
    report_path = _inside_artifacts(ARTIFACT_ROOT / "evaluation-report.json")
    if report_path.is_file():
        report = _read_json(report_path)
        if (
            report.get("schema_id") != "issue75-stage1-heldout-report-v1"
            or report.get("package_commit") != ledger.binding.package_commit
            or report.get("source_lock_sha256") != ledger.binding.source_lock_sha256
            or report.get("status") != "EVALUATION_COMPLETE_PENDING_INDEPENDENT_REVIEW"
        ):
            raise PermissionError("existing heldout report is not a successful receipt for this exact package")
        _existing_scheduler_receipt(ledger, "SCHEDULER:EVALUATE")
        return report
    gate = _formal_forecast(ledger, phase="heldout")
    training_path = ARTIFACT_ROOT / "training-report.json"
    if not training_path.is_file():
        raise PermissionError("heldout evaluation requires a completed four-block pi4 training report")
    training = _read_json(training_path)
    if (
        training.get("status") != "TRAINING_COMPLETE"
        or training.get("p1_formal_model_source") != "pi4"
        or training.get("package_commit") != ledger.binding.package_commit
        or training.get("source_lock_sha256") != ledger.binding.source_lock_sha256
        or training.get("training_seed_ids") != [row["owner_seed"] for row in _unit_rows("train")]
    ):
        raise PermissionError("heldout evaluation requires the actual 512-owner pi4 checkpoint")
    if not _model_path("pi4").is_file():
        raise FileNotFoundError("formal pi4 checkpoint is absent")
    estimates, _ = _forecast_estimates(ledger)
    scheduler_reservation = _run_scheduler_reservation(ledger, "SCHEDULER:EVALUATE", estimates)
    concurrency = _formal_concurrency()
    rows = _unit_rows("evaluate")
    for offset in range(0, len(rows), concurrency):
        _run_owner_batch(
            ledger, rows[offset:offset + concurrency], phase="evaluate", estimates=estimates,
        )
    if not _all_units_succeeded(ledger, rows):
        raise budget.ResourceBlocked("the registered 96-owner by five-arm evaluation is incomplete")
    run_events, units = _event_state(ledger)
    outcomes = []
    for row in rows:
        result = _existing_unit_receipt(ledger, str(row["unit_id"]))
        outcomes.append({
            "unit_id": row["unit_id"], "owner_seed": row["owner_seed"], "arm": row["arm"],
            "paired_owner_key": row["paired_owner_key"],
            "terminal_floor": result["terminal_floor"], "heart_victory": result["heart_victory"],
            "terminal_act": result["terminal_act"], "act2_plus": result["act2_plus"],
            "act3_plus": result["act3_plus"], "act4_reached": result["act4_reached"],
            "search": result["search"],
            "result_sha256": _file_sha256(
                _owner_result_path(
                    str(row["unit_id"]), str(result["resource"]["ledger_run_id"]),
                    str(row["phase"]), current_package=True,
                )
            ),
            "resource": _unit_accounting(ledger, str(row["unit_id"])),
            "model_digest": result.get("model_digest"),
            "policy_source": result.get("policy_source"),
            "native_source_commit": result.get("native_source_commit"),
            "stsr_source_commit": result.get("stsr_source_commit"),
            "native_module_sha256": result.get("native_module_sha256"),
        })
    by_arm: dict[str, list[dict[str, Any]]] = {arm: [] for arm in ARMS}
    for row in outcomes:
        by_arm[row["arm"]].append(row)
    if any(len(by_arm[arm]) != 96 for arm in ARMS):
        raise PermissionError("heldout report does not contain all 96 rows in every registered arm")
    means = {
        arm: {
            "n": 96,
            "mean_terminal_floor": sum(float(row["terminal_floor"]) for row in by_arm[arm]) / 96,
            "heart_wins": sum(bool(row["heart_victory"]) for row in by_arm[arm]),
            "act2_plus": sum(bool(row["act2_plus"]) for row in by_arm[arm]),
            "act3_plus": sum(bool(row["act3_plus"]) for row in by_arm[arm]),
            "act4_reached": sum(bool(row["act4_reached"]) for row in by_arm[arm]),
        }
        for arm in ARMS
    }
    analysis_accounting: dict[str, float] = {}
    analysis_unit = _run_report_analysis(ledger, outcomes, accounting_sink=analysis_accounting)
    analysis = analysis_unit["analysis"]
    report = {
        "schema_id": "issue75-stage1-heldout-report-v1",
        "status": "EVALUATION_COMPLETE_PENDING_INDEPENDENT_REVIEW",
        "package_commit": ledger.binding.package_commit,
        "source_lock_sha256": ledger.binding.source_lock_sha256,
        "scope": "STUDY_ONLY",
        "owner_count": 96,
        "arm_count": 5,
        "outcomes": outcomes,
        "arm_summaries": means,
        "paired_terminal_floor_contrasts": analysis["paired_terminal_floor_contrasts"],
        "search_coverage_and_resource_summary": analysis["search_coverage_and_resource_summary"],
        "bootstrap_contract": analysis["bootstrap_contract"],
        "analysis_resource_accounting": _unit_accounting(ledger, "REPORT:HELDOUT"),
        "p1_formal_model_digest": training["p1_formal_model_digest"],
        "preflight_report_sha256": gate["preflight_report_sha256"],
        "cost_forecast_sha256": gate["cost_forecast_sha256"],
        "complete_owner_arm_count": len(outcomes),
        "formal_training_seed_count": 512,
        "formal_evaluation_seed_count": 480,
        "no_missing_arm_fabrication": True,
        "expert_decisions_outside_E": 0,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_once(report_path, report)
    _finish_scheduler_reservation(
        ledger, scheduler_reservation, phase_started_cpu=phase_started_cpu,
        separately_accounted_cpu_seconds=analysis_accounting.get("cpu_seconds", 0.0),
    )
    return report


def run_phase(phase: str, *, output_dir: Path) -> dict[str, Any]:
    """Run one enabled, source-locked phase under write-ahead CPU accounting."""
    if phase not in {"preflight", "train", "evaluate"}:
        raise ValueError("Stage 1 executor accepts only preflight, train, or evaluate")
    budget.verify_stage1_package()
    budget.verify_stage1_phase(phase)
    if _inside_artifacts(output_dir) != ARTIFACT_ROOT.resolve(strict=False):
        raise ValueError("Stage 1 uses one registered artifact root and cumulative ledger")
    # The lock and phase allowlist are the first operation that may authorize artifact creation.
    ledger = budget.open_stage1_ledger(ARTIFACT_ROOT / "ledger")
    with _lock_context(ARTIFACT_ROOT / "executor.lock"):
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        _assert_no_active_supervisors()
        if phase == "preflight":
            return _run_preflight(ledger)
        if phase == "train":
            return _run_training(ledger)
        return _run_evaluation(ledger)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--phase", choices=("preflight", "train", "evaluate"))
    mode.add_argument("--worker-job", type=Path)
    mode.add_argument("--supervisor-job", type=Path)
    mode.add_argument("--reconcile-preflight", action="store_true")
    args = parser.parse_args(argv)
    if args.reconcile_preflight:
        try:
            reconciled = _reconcile_preflight_receipts()
        except BaseException as exc:
            print(json.dumps({
                "status": "STAGE1_PREFLIGHT_RECONCILIATION_FAILED",
                "error_type": type(exc).__name__,
            }, sort_keys=True), file=sys.stderr)
            return 2
        result = {
            "status": reconciled["report"]["status"],
            "report_sha256": reconciled["report_sha256"],
            "cost_forecast_status": reconciled["cost_forecast"]["status"],
            "cost_forecast_sha256": reconciled["cost_forecast_sha256"],
            "formal_execution_enabled": False,
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.supervisor_job is not None:
        return _supervisor_main(_inside_artifacts(args.supervisor_job))
    if args.worker_job is not None:
        try:
            return _worker_main(_inside_artifacts(args.worker_job))
        except BaseException as exc:
            # No exception message, native payload, private state, or path is emitted.
            print(json.dumps({
                "status": "STAGE1_WORKER_FAILED", "error_type": type(exc).__name__,
            }, sort_keys=True), file=sys.stderr)
            return 2
    try:
        report = run_phase(args.phase, output_dir=ARTIFACT_ROOT)
        result = {
            "status": report.get("status"),
            "phase": args.phase,
            "package_commit": report.get("package_commit"),
            "source_lock_sha256": report.get("source_lock_sha256"),
        }
        expected_status = {
            "preflight": "PREFLIGHT_COMPLETE_PENDING_INDEPENDENT_REVIEW",
            "train": "TRAINING_COMPLETE",
            "evaluate": "EVALUATION_COMPLETE_PENDING_INDEPENDENT_REVIEW",
        }[args.phase]
        if report.get("status") != expected_status:
            print(json.dumps(result, sort_keys=True), file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    except BaseException as exc:
        # The registered parent interface emits only a coarse failure boundary.
        print(json.dumps({
            "status": "STAGE1_EXECUTOR_FAILED",
            "phase": args.phase,
            "error_type": type(exc).__name__,
        }, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
