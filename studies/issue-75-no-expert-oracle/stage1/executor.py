"""Source-locked, serialized Stage 1 owner executor for Issue 75.

This module is intentionally study-local.  The CLI remains disabled by the
live exact-source Reviewer/Planner gate in ``stage1_budget``; all artifacts,
workers, temporary files, and caches stay below ``stage1/artifacts``.
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
import sys
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
        if path.read_bytes() != payload:
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


def _unit_key(unit_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in unit_id)


def _unit_directory(unit_id: str) -> Path:
    return _inside_artifacts(ARTIFACT_ROOT / "units" / _unit_key(unit_id))


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
        return model, model_digest(model), {"source": "registered_initial_pi0", "checkpoint_sha256": _file_sha256(path)}
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
        raw = base64.b64decode(row["candidate_features_f32le_zlib_base64"], validate=True)
        expected_size = int(shape[0]) * POLICY_DIMENSION * 4
        if len(raw) != expected_size or _sha256(raw) != row.get("candidate_features_sha256"):
            raise ValueError("stored compressed training features fail their size/hash check")
        matrix = np.frombuffer(zlib.decompress(raw), dtype="<f4").reshape(tuple(map(int, shape))).copy()
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
        + ["REPORT:HELDOUT"]
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
    if allowance < 0:
        raise budget.ResourceBlocked("registered engineering allowance cannot fund heldout report analysis")
    per_unit_allowance = allowance / len(unit_ids)
    estimates: dict[str, float] = {}
    for row in _rows():
        if row["phase"] == "training":
            estimates[row["unit_id"]] = search_per_train + owner_nonsearch + per_unit_allowance
        elif row["phase"] == "heldout":
            searches = search_per_heldout if row["arm"] in {"R", "J"} else 0.0
            estimates[row["unit_id"]] = searches + owner_nonsearch + per_unit_allowance
    for block in range(4):
        estimates[f"UPDATE:{block}"] = train_guard / 4.0 + per_unit_allowance
    estimates["REPORT:HELDOUT"] = ANALYSIS_REPORT_RESERVATION
    if list(estimates) and set(estimates) != set(unit_ids):
        raise AssertionError("cost forecast unit IDs differ from the locked formal workload")
    summary = {
        "schema_id": "issue75-stage1-cost-forecast-v1",
        "status": "FITS_IF_LEDGER_ACCEPTS",
        "source": "locked Stage 1 cost-model caps and current exact-source ledger charges",
        "maximum_search_cpu_seconds": float(projection["search_hard_cap"]),
        "nonsearch_root_bootstrap_p95_cpu_seconds": nonsearch,
        "training_update_guard_cpu_seconds": train_guard,
        "engineering_allowance_initial_cpu_seconds": float(projection["engineering_allowance"]),
        "preflight_actual_cpu_seconds": preflight_charge,
        "remaining_engineering_allowance_cpu_seconds": engineering,
        "rounding_remainder_cpu_seconds": float(projection["rounding_remainder"]),
        "formal_unit_count": len(unit_ids) - 1,
        "forecast_unit_count": len(unit_ids),
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


def _formal_forecast(ledger: Any) -> dict[str, Any]:
    if not _preflight_complete(ledger):
        raise PermissionError("formal execution requires all 40 preflight owner-arm rows to be durably successful")
    report_path = ARTIFACT_ROOT / "preflight-report.json"
    forecast_path = ARTIFACT_ROOT / "cost-forecast.json"
    if not report_path.is_file() or not forecast_path.is_file():
        raise PermissionError("formal execution requires immutable preflight and cost-forecast reports")
    report = _read_json(report_path)
    forecast = _read_json(forecast_path)
    if report.get("status") != "PREFLIGHT_COMPLETE_PENDING_INDEPENDENT_REVIEW" or forecast.get("status") != "FITS_IF_LEDGER_ACCEPTS":
        raise budget.ResourceBlocked("preflight/forecast status does not permit formal rows")
    report_sha = _file_sha256(report_path)
    forecast_sha = _file_sha256(forecast_path)
    review = budget.verify_preflight_review_pass(
        preflight_report_sha256=report_sha,
        cost_forecast_sha256=forecast_sha,
        not_before=str(report["completed_at"]),
    )
    estimates, _ = _forecast_estimates(ledger)
    live = ledger.forecast(
        expected_unit_ids=_formal_unit_ids(),
        future_builder_cpu_seconds=estimates,
        allowed_completed_unit_ids=[row["unit_id"] for row in _unit_rows("preflight")],
    )
    if live.get("status") != "FITS":
        raise budget.ResourceBlocked("live Stage 1 ledger no longer fits the exact remaining cohort")
    return {"preflight_report_sha256": report_sha, "cost_forecast_sha256": forecast_sha, "review": review, "ledger_forecast": live}


def _preflight_reports(ledger: Any) -> dict[str, Any]:
    reports = []
    treatment_not_delivered = []
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
        result = _read_json(result_path)
        reports.append({
            "unit_id": unit,
            "ledger_status": None if run["finish"] is None else run["finish"]["status"],
            "result_sha256": _file_sha256(result_path),
            "arm": row["arm"],
            "owner_seed": row["owner_seed"],
            "terminal_floor": result.get("terminal_floor") if accounting["status"] == "succeeded" else None,
            "heart_victory": result.get("heart_victory") if accounting["status"] == "succeeded" else None,
            "search": result.get("search", []),
            "policy_source": result.get("policy_source"),
            "model_digest": result.get("model_digest"),
            "native_source_commit": result.get("native_source_commit"),
            "stsr_source_commit": result.get("stsr_source_commit"),
            "native_module_sha256": result.get("native_module_sha256"),
            "accounting": accounting,
        })
        if accounting["status"] == "succeeded" and any(item.get("status") == "TREATMENT_NOT_DELIVERED" for item in result.get("search", [])):
            treatment_not_delivered.append(unit)
    return {"rows": reports, "incomplete": incomplete, "treatment_not_delivered": treatment_not_delivered}


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
        "package_commit": ledger.activation.package_commit,
        "source_lock_sha256": ledger.activation.source_lock_sha256,
        "scope": "STUDY_ONLY",
        "registered_rows": 40,
        "completed_rows": len(summary["rows"]),
        "terminal_rows": sum(item["terminal_floor"] is not None for item in summary["rows"]),
        "formal_seeds_consumed": 0,
        "preflight_rows_are_not_efficacy_evidence": True,
        "p1_policy_label": PROXY_LABEL,
        "treatment_not_delivered_units": summary["treatment_not_delivered"],
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


def _load_training_examples(block: int, *, ledger: Any) -> list[Any]:
    rows = [row for row in _rows() if row["phase"] == "training" and int(row["block_index"]) == block]
    expected_seeds = {int(row["owner_seed"]) for row in rows}
    examples = []
    for row in rows:
        unit_id = str(row["unit_id"])
        receipt = _existing_unit_receipt(ledger, unit_id)
        if receipt is None:
            raise PermissionError("training block has an owner without a successful ledger receipt")
        if (
            receipt.get("phase") != "train" or receipt.get("formal") is not True
            or receipt.get("arm") != "TRAIN" or receipt.get("owner_seed") != int(row["owner_seed"])
            or receipt.get("package_commit") != ledger.activation.package_commit
            or receipt.get("source_lock_sha256") != ledger.activation.source_lock_sha256
        ):
            raise PermissionError("training owner receipt differs from its locked row or active package")
        directory = _unit_directory(row["unit_id"])
        result = receipt
        if result.get("status") != "SUCCEEDED" or result.get("training_features") is None:
            raise PermissionError("training block lacks a complete natural owner and auxiliary J evidence")
        pointer = result["training_features"]
        path = _inside_artifacts(ARTIFACT_ROOT / pointer["path"])
        expected_path = _inside_artifacts(directory / "training-features.json")
        if path != expected_path:
            raise PermissionError("training feature pointer does not name its own immutable owner artifact")
        if _file_sha256(path) != pointer["sha256"]:
            raise PermissionError("training features differ from their immutable unit receipt")
        features = _read_json(path)
        if (
            features.get("schema_id") != FEATURE_SCHEMA
            or int(features.get("owner_seed", -1)) != int(row["owner_seed"])
            or features.get("unit_id") != unit_id
            or features.get("package_commit") != ledger.activation.package_commit
            or features.get("source_lock_sha256") != ledger.activation.source_lock_sha256
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
            "package_commit": ledger.activation.package_commit,
            "source_lock_sha256": ledger.activation.source_lock_sha256,
            "selection": "first actual multi-choice decision per block-zero owner in ascending registered seed order",
            "outcome_conditioned": False,
            "owners": owners,
        })
    payload = _read_json(path)
    if (
        payload.get("schema_id") != "issue75-stage1-fixed-policy-probes-v1"
        or payload.get("package_commit") != ledger.activation.package_commit
        or payload.get("source_lock_sha256") != ledger.activation.source_lock_sha256
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


def _existing_unit_receipt(ledger: Any, unit_id: str) -> dict[str, Any] | None:
    run_id, _, finish = _existing_unit(ledger, unit_id)
    if run_id is None:
        return None
    if finish is None:
        raise budget.ResourceBlocked(f"unresolved reservation is preserved at full cap: {unit_id}")
    if finish.get("status") != "succeeded":
        raise budget.ResourceBlocked(f"failed unit is immutable and cannot be replayed: {unit_id}")
    result_path = _unit_directory(unit_id) / "result.json"
    if not result_path.is_file():
        raise PermissionError(f"successful ledger unit lacks its durable result: {unit_id}")
    result = _read_json(result_path)
    if result.get("status") != "SUCCEEDED" or result.get("unit_id") != unit_id:
        raise PermissionError(f"unit receipt failed identity/status verification: {unit_id}")
    return result


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
    ledger.reserve(
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
    job_path = _inside_artifacts(directory / f"worker-job-{run_id}.json")
    stdout_path = _inside_artifacts(directory / f"worker-log-{run_id}.txt")
    job = {
        "schema_id": WORKER_SCHEMA,
        "package_commit": ledger.activation.package_commit,
        "source_lock_sha256": ledger.activation.source_lock_sha256,
        "run_id": run_id,
        "unit_id": unit_id,
        "phase": phase,
        "row": dict(row),
        "output_dir": str(directory / "native"),
        "result_path": str(directory / "result.json"),
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
        result = _read_json(directory / "result.json")
        if result.get("status") != "SUCCEEDED" or result.get("unit_id") != unit_id:
            raise RuntimeError("Stage 1 worker result did not pass unit identity/status checks")
        if measured > cap:
            _finish_failed_unit(
                ledger, run_id=run_id, unit_id=unit_id, directory=directory,
                measured=measured, cap=cap, boundary="measured_process_tree_cpu_over_reservation",
                started=started,
            )
            raise budget.ResourceBlocked(f"unit exceeded its process-tree CPU reservation: {unit_id}")
        ledger.finish(run_id=run_id, measured_cpu_seconds=measured, status="succeeded")
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


def _update_block(ledger: Any, block: int, *, estimates: Mapping[str, float]) -> dict[str, Any]:
    import torch
    import learning
    from model import model_digest

    unit_id = f"UPDATE:{block}"
    existing = _existing_unit_receipt(ledger, unit_id)
    if existing is not None:
        return existing
    examples = _load_training_examples(block, ledger=ledger)
    frozen, source_digest, _ = _load_model(f"pi{block}")
    checkpoint_path = _inside_artifacts(ARTIFACT_ROOT / "models" / f"block-{block}-step.pt")
    previous_run_id, _, previous_finish = _existing_unit(ledger, unit_id)
    resume_state = None
    if previous_run_id is not None:
        if previous_finish is None or previous_finish.get("status") != "incomplete":
            raise budget.ResourceBlocked(f"policy update cannot be resumed: {unit_id}")
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

    first_owner = min(int(row["owner_seed"]) for row in _rows() if row["phase"] == "training" and int(row["block_index"]) == block)
    import prepare
    stage1_config = _read_json(STAGE1 / "config.json")
    registered_seeds = stage1_config.get("seeds", {}).get("block_order_seeds", ())
    if len(registered_seeds) != 4:
        raise PermissionError("the locked four-block owner-order seed schedule is incomplete")
    block_seed = int(registered_seeds[block])
    if block_seed != prepare.derive_seed("block-order", first_owner, f"TRAIN-BLOCK-{block}"):
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
        "source_commit": ledger.activation.package_commit,
        "source_lock_sha256": ledger.activation.source_lock_sha256,
    }
    output = _inside_artifacts(ARTIFACT_ROOT / "units" / f"UPDATE-{block}" / "result.json")
    _write_once(output, result_receipt)
    return result_receipt


def _run_update_unit(ledger: Any, block: int, *, estimates: Mapping[str, float]) -> dict[str, Any]:
    unit_id = f"UPDATE:{block}"
    existing = _existing_unit_receipt(ledger, unit_id)
    if existing is not None:
        return existing
    previous_run_id, _, previous_finish = _existing_unit(ledger, unit_id)
    resume_digest = None
    if previous_run_id is not None:
        if previous_finish is None or previous_finish.get("status") != "incomplete":
            raise budget.ResourceBlocked(f"optimizer update has an unresolved/nonresumable ledger state: {unit_id}")
        resume_digest = previous_finish.get("checkpoint_sha256")
        checkpoint = _inside_artifacts(ARTIFACT_ROOT / "models" / f"block-{block}-step.pt")
        if not checkpoint.is_file() or _file_sha256(checkpoint) != resume_digest:
            raise budget.CheckpointMismatch("incomplete optimizer update checkpoint does not match its ledger receipt")
    run_id = f"{unit_id}-{uuid.uuid4().hex[:16]}"
    cap = float(estimates[unit_id])
    ledger.reserve(
        run_id=run_id, unit_id=unit_id, role="builder",
        reserved_cpu_seconds=cap, unit_kind="training_update",
        previous_run_id=previous_run_id, checkpoint_sha256=resume_digest,
    )
    worker_dir = _inside_artifacts(ARTIFACT_ROOT / "units" / f"UPDATE-{block}")
    worker_dir.mkdir(parents=True, exist_ok=True)
    job_path = _inside_artifacts(worker_dir / f"worker-job-{run_id}.json")
    stdout_path = _inside_artifacts(worker_dir / f"worker-log-{run_id}.txt")
    job = {
        "schema_id": WORKER_SCHEMA,
        "package_commit": ledger.activation.package_commit,
        "source_lock_sha256": ledger.activation.source_lock_sha256,
        "run_id": run_id,
        "unit_id": unit_id,
        "phase": "update",
        "block_index": block,
        "previous_run_id": previous_run_id,
        "checkpoint_sha256": resume_digest,
        "result_path": str(worker_dir / f"result-{run_id}.json"),
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
    measured = 0.0
    try:
        process = watchdog.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker-job", str(job_path)],
            reserved_cpu_seconds=cap, cwd=ROOT, env=env, stdout_path=stdout_path,
        )
        measured = float(process.cpu_seconds)
        if process.returncode != 0:
            raise RuntimeError(f"Stage 1 update worker exited with status {process.returncode}")
        result = _read_json(worker_dir / f"result-{run_id}.json")
        if result.get("status") != "SUCCEEDED" or result.get("unit_id") != unit_id:
            raise RuntimeError("Stage 1 update result failed identity/status checks")
        if measured > cap:
            _finish_failed_unit(
                ledger, run_id=run_id, unit_id=unit_id, directory=worker_dir,
                measured=measured, cap=cap, boundary="measured_process_tree_cpu_over_reservation",
                started=time.monotonic(),
            )
            raise budget.ResourceBlocked(f"policy update exceeded its process-tree CPU reservation: {unit_id}")
        ledger.finish(run_id=run_id, measured_cpu_seconds=measured, status="succeeded")
        return result
    except budget.ProcessTreeBudgetExceeded as exc:
        measured = float(exc.cpu_seconds)
        checkpoint = _inside_artifacts(ARTIFACT_ROOT / "models" / f"block-{block}-step.pt")
        if checkpoint.is_file():
            digest = _file_sha256(checkpoint)
            _finish_failed_unit(
                ledger, run_id=run_id, unit_id=unit_id, directory=worker_dir,
                measured=measured, cap=cap, boundary="process_tree_cpu_cap",
                started=started, status="incomplete", checkpoint_sha256=digest,
            )
            raise budget.ResourceBlocked(f"policy update stopped at a durable step checkpoint: {unit_id}") from exc
        _finish_failed_unit(
            ledger, run_id=run_id, unit_id=unit_id, directory=worker_dir,
            measured=measured, cap=cap, boundary="process_tree_cpu_cap",
            started=started,
        )
        raise budget.ResourceBlocked(f"policy update exceeded its cap before a durable checkpoint: {unit_id}") from exc
    except BaseException as exc:
        measured = max(measured, float(getattr(exc, "cpu_seconds", 0.0)))
        if "process" not in locals():
            measured = max(measured, cap)
        current, _, finish = _existing_unit(ledger, unit_id)
        if current == run_id and finish is None:
            _finish_failed_unit(
                ledger, run_id=run_id, unit_id=unit_id, directory=worker_dir,
                measured=measured, cap=cap, boundary=type(exc).__name__,
                started=started,
            )
        raise


def _worker_owner(job: Mapping[str, Any], ledger: Any) -> dict[str, Any]:
    from algorithm import InvocationBudget, run_search, derive_attempt_seed, write_immutable_json
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
    config, runner, module, encoder = runtime.initialize_native_runtime(output, sampling=True)
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
            root_id=f"{row['unit_id']}-ROOT-{index}",
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
        attempt_dir = _inside_artifacts(ARTIFACT_ROOT / "search" / _unit_key(str(row["unit_id"])) / f"root-{index}")
        attempt_dir.mkdir(parents=True, exist_ok=True)
        invocation_start_cpu = runtime.process_tree_cpu_seconds()
        completed: dict[int, Any] = {}
        mutation_attempts: dict[int, dict[str, Any]] = {}

        def attempt_runner(attempt_index: int, root_action_index: int, mutation_target: Any | None):
            parent = None
            if mutation_target is not None:
                parent = completed.get(int(mutation_target.parent_attempt_index))
                if parent is None:
                    raise RuntimeError("joint search mutation parent was not retained")
            remaining = 32.0 - (runtime.process_tree_cpu_seconds() - invocation_start_cpu)
            if remaining <= 0.1:
                from algorithm import SearchBudgetExhausted
                raise SearchBudgetExhausted("search invocation CPU cap reached before another continuation")
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
                cpu_cap_seconds=min(32.0, remaining),
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
            outcome = run_search(
                arm=search_arm,
                study_id=STUDY_ID,
                root_id=root.root_id,
                invocation_seed=invocation_seed,
                root_legal_identities=root.legal_identities,
                attempt_runner=attempt_runner,
                budget=InvocationBudget(cpu_cap_seconds=32.0, started_attempt_cap=16),
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
                "package_commit": ledger.activation.package_commit,
                "source_lock_sha256": ledger.activation.source_lock_sha256,
                "base": _serialize_trial(base, runtime),
                "joint_trajectories": [_serialize_trial(trial, runtime) for trial in joint],
                "search_receipts": search_receipts,
            }
            training_pointer = _save_training_features(_unit_directory(str(row["unit_id"])), owner_features)
        report = {
            "schema_id": UNIT_SCHEMA,
            "status": "SUCCEEDED",
            "package_commit": ledger.activation.package_commit,
            "source_lock_sha256": ledger.activation.source_lock_sha256,
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
    result = _update_block(ledger, int(job["block_index"]), estimates={str(job["unit_id"]): 1.0})
    result_path = _inside_artifacts(Path(str(job["result_path"])))
    _write_once(result_path, result)
    return result


def _worker_main(job_path: Path) -> int:
    budget.verify_stage1_package()
    job = _read_json(job_path)
    if job.get("schema_id") != WORKER_SCHEMA:
        raise PermissionError("worker job schema differs from the locked executor")
    ledger = budget.open_activated_stage1_ledger(ARTIFACT_ROOT / "ledger")
    if (
        job.get("package_commit") != ledger.activation.package_commit
        or job.get("source_lock_sha256") != ledger.activation.source_lock_sha256
    ):
        raise PermissionError("worker job is not bound to the live activation package")
    unit_id = str(job.get("unit_id", ""))
    run_id = str(job.get("run_id", ""))
    if len(run_id) != len(unit_id) + 17 or not run_id.startswith(unit_id + "-"):
        raise PermissionError("worker run ID is not a unique unit-scoped reservation ID")
    suffix = run_id[len(unit_id) + 1:]
    if len(suffix) != 16 or any(char not in "0123456789abcdef" for char in suffix):
        raise PermissionError("worker run ID suffix is malformed")
    reservation = ledger.require_open_reservation(run_id=run_id, unit_id=unit_id)
    if (
        job.get("previous_run_id") != reservation.get("previous_run_id")
        or job.get("checkpoint_sha256") != reservation.get("checkpoint_sha256")
    ):
        raise PermissionError("worker resume metadata differs from the durable ledger reservation")
    expected_job_path = _inside_artifacts(_unit_directory(unit_id) / f"worker-job-{run_id}.json")
    if job_path.resolve() != expected_job_path:
        raise PermissionError("worker job path differs from its reserved unit")
    if job.get("phase") == "update":
        block = int(job.get("block_index", -1))
        expected_dir = _inside_artifacts(ARTIFACT_ROOT / "units" / f"UPDATE-{block}")
        if unit_id != f"UPDATE:{block}" or reservation.get("unit_kind") != "training_update":
            raise PermissionError("optimizer worker unit differs from its registered update block")
        if Path(str(job.get("result_path"))).resolve() != expected_dir / f"result-{run_id}.json":
            raise PermissionError("optimizer worker result path differs from its reserved update")
        result = _worker_update(job, ledger)
    else:
        phase = str(job.get("phase", ""))
        phase_name = {"preflight": "preflight", "train": "training", "evaluate": "heldout"}.get(phase)
        row = job.get("row")
        if phase_name is None or not isinstance(row, dict) or row.get("unit_id") != unit_id:
            raise PermissionError("owner worker is not bound to a registered unit phase")
        registered = next((item for item in _rows() if item["unit_id"] == unit_id), None)
        if registered is None or row != registered or registered.get("phase") != phase_name:
            raise PermissionError("owner worker row differs from the exact locked seed-table record")
        if reservation.get("unit_kind") != "owner_arm":
            raise PermissionError("owner worker does not hold an owner-arm reservation")
        unit_dir = _unit_directory(unit_id)
        if Path(str(job.get("output_dir"))).resolve() != unit_dir / "native":
            raise PermissionError("owner worker native output path differs from its registered unit")
        if Path(str(job.get("result_path"))).resolve() != unit_dir / "result.json":
            raise PermissionError("owner worker result path differs from its registered unit")
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


def _run_report_analysis(ledger: Any, outcomes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    unit_id = "REPORT:HELDOUT"
    existing = _existing_unit_receipt(ledger, unit_id)
    if existing is not None:
        return existing
    run_id = f"{unit_id}-{uuid.uuid4().hex[:16]}"
    cap = ANALYSIS_REPORT_RESERVATION
    ledger.reserve(
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
            "package_commit": ledger.activation.package_commit,
            "source_lock_sha256": ledger.activation.source_lock_sha256,
            "analysis": analysis,
            "analysis_cpu_seconds_before_ledger_finalize": measured,
        }
        _write_once(result_path, result)
        measured = max(0.0, time.process_time() - started)
        if measured > cap:
            try:
                ledger.finish(run_id=run_id, measured_cpu_seconds=measured, status="failed")
            except budget.BudgetOverrun:
                pass
            raise budget.ResourceBlocked("paired-owner report analysis exceeded its 120 CPU-s reservation")
        ledger.finish(run_id=run_id, measured_cpu_seconds=measured, status="succeeded")
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
        return {"status": report["status"], "report": report, "cost_forecast": _read_json(forecast_path)}
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
    gate = _formal_forecast(ledger)
    estimates, _ = _forecast_estimates(ledger)
    for block in range(4):
        rows = [row for row in _rows() if row["phase"] == "training" and int(row["block_index"]) == block]
        for row in rows:
            if _existing_unit_receipt(ledger, str(row["unit_id"])) is None:
                _run_unit(ledger, row, phase="train", estimates=estimates)
        if not _all_units_succeeded(ledger, rows):
            raise budget.ResourceBlocked(f"registered 128-owner block {block} is not complete")
        _run_update_unit(ledger, block, estimates=estimates)
    checkpoints = []
    for block in range(4):
        receipt = _read_json(_inside_artifacts(ARTIFACT_ROOT / "units" / f"UPDATE-{block}" / "result.json"))
        checkpoints.append(receipt["model_checkpoint"])
    final_model, final_digest, final_source = _load_model("pi4")
    report = {
        "schema_id": "issue75-stage1-training-report-v1",
        "status": "TRAINING_COMPLETE",
        "package_commit": ledger.activation.package_commit,
        "source_lock_sha256": ledger.activation.source_lock_sha256,
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
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if len(report["training_seed_ids"]) != 512:
        raise AssertionError("formal training report omitted registered owners")
    _write_once(ARTIFACT_ROOT / "training-report.json", report)
    return report


def _run_evaluation(ledger: Any) -> dict[str, Any]:
    report_path = _inside_artifacts(ARTIFACT_ROOT / "evaluation-report.json")
    if report_path.is_file():
        report = _read_json(report_path)
        if (
            report.get("schema_id") != "issue75-stage1-heldout-report-v1"
            or report.get("package_commit") != ledger.activation.package_commit
            or report.get("source_lock_sha256") != ledger.activation.source_lock_sha256
            or report.get("status") != "EVALUATION_COMPLETE_PENDING_INDEPENDENT_REVIEW"
        ):
            raise PermissionError("existing heldout report is not a successful receipt for this exact package")
        return report
    gate = _formal_forecast(ledger)
    training_path = ARTIFACT_ROOT / "training-report.json"
    if not training_path.is_file():
        raise PermissionError("heldout evaluation requires a completed four-block π4 training report")
    training = _read_json(training_path)
    if training.get("status") != "TRAINING_COMPLETE" or training.get("p1_formal_model_source") != "pi4":
        raise PermissionError("heldout evaluation requires the actual 512-owner π4 checkpoint")
    if not _model_path("pi4").is_file():
        raise FileNotFoundError("formal π4 checkpoint is absent")
    estimates, _ = _forecast_estimates(ledger)
    for row in _unit_rows("evaluate"):
        if _existing_unit_receipt(ledger, str(row["unit_id"])) is None:
            _run_unit(ledger, row, phase="evaluate", estimates=estimates)
    rows = _unit_rows("evaluate")
    if not _all_units_succeeded(ledger, rows):
        raise budget.ResourceBlocked("the registered 96-owner × five-arm evaluation is incomplete")
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
            "result_sha256": _file_sha256(_unit_directory(str(row["unit_id"])) / "result.json"),
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
    analysis_unit = _run_report_analysis(ledger, outcomes)
    analysis = analysis_unit["analysis"]
    report = {
        "schema_id": "issue75-stage1-heldout-report-v1",
        "status": "EVALUATION_COMPLETE_PENDING_INDEPENDENT_REVIEW",
        "package_commit": ledger.activation.package_commit,
        "source_lock_sha256": ledger.activation.source_lock_sha256,
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
    return report


def run_phase(phase: str, *, output_dir: Path) -> dict[str, Any]:
    """Run one registered phase after its exact live authorization and write-ahead gates."""
    if phase not in {"preflight", "train", "evaluate"}:
        raise ValueError("Stage 1 executor accepts only preflight, train, or evaluate")
    budget.verify_stage1_package()
    if _inside_artifacts(output_dir) != ARTIFACT_ROOT.resolve(strict=False):
        raise ValueError("Stage 1 uses one registered artifact root and cumulative ledger")
    # This network/source/timestamp gate is the first operation that may authorize
    # artifact creation. No config edit or caller-provided receipt can bypass it.
    ledger = budget.open_activated_stage1_ledger(ARTIFACT_ROOT / "ledger")
    with _lock_context(ARTIFACT_ROOT / "executor.lock"):
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        if phase == "preflight":
            return _run_preflight(ledger)
        if phase == "train":
            return _run_training(ledger)
        return _run_evaluation(ledger)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-job", type=Path)
    args = parser.parse_args(argv)
    if args.worker_job is None:
        parser.error("executor worker mode requires --worker-job")
    try:
        return _worker_main(_inside_artifacts(args.worker_job))
    except BaseException as exc:
        # No exception message, native payload, private state, or path is emitted.
        print(json.dumps({"status": "STAGE1_WORKER_FAILED", "error_type": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
