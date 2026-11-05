"""Source-locked, nonformal Issue 85 runtime/equivalence pilot.

The runner reuses the accepted Issue 84 native worker and imports its exact
source tree. All generated games, progress, updates, and receipts live under the
owner's durable issue85/artifacts directory. A pilot is never a formal result.
"""
from __future__ import annotations

import argparse
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
sys.dont_write_bytecode = True
import time
from typing import Any, Mapping, Sequence


STUDY = Path(__file__).resolve().parent
REPO = STUDY.parents[1]
LOCK_PATH = STUDY / "source-lock.json"
CONTRACT_PATH = STUDY / "contract.json"
LOCK = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
DATA_ROOT_WSL = "/mnt/d/DeadlyCatCoding/stsrl-data"
ISSUE84_ACCEPTED_COMMIT = LOCK["baseline_A"]["issue84_final_accepted_source_commit"]
PILOT_OWNERS = tuple(range(8_530_001, 8_530_017))
NEW_ARMS = ("B", "C", "D", "E", "F")
POLICIES = ("PARENT", "A", *NEW_ARMS)
PILOT_RESUME_PREDECESSOR_SHA = "0297d60382e5dfe64d8de93f6ca383e96b655f40"
PILOT_B_ACTOR_UPDATE_CARRY_FORWARD_SHA = "62382663fd5612d55f662190dc389cc0664a2f98"
PILOT_C_OUTCOME_CARRY_FORWARD_SHA = "fafbb59770f14aea14915090714b51538f8c4d85"
PILOT_COMPLETED_TRAINING_CARRY_FORWARD_SHA = "643766496a6eb9b94bcc3af4bf52afb2180fab7a"
ACTOR_STEPS = 64
ACTOR_CHECKPOINT_INTERVAL = 8
PILOT_MINIBATCH_OWNERS = len(PILOT_OWNERS)
WIDENED_FOLD_ATOL = 1e-7
_CURRENT_ROW: ContextVar[Mapping[str, Any] | None] = ContextVar("issue85_pilot_current_row", default=None)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object at {path}")
    return value


def _write_once(path: Path, value: object) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical(value) + b"\n"
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return sha256(raw)


def _write_or_verify_once(path: Path, value: object) -> str:
    raw = canonical(value) + b"\n"
    path = Path(path)
    if path.exists():
        existing = path.read_bytes()
        if existing != raw:
            raise RuntimeError(f"immutable pilot evidence already exists with different bytes: {path}")
        return sha256(existing)
    return _write_once(path, value)


def _atomic_json(path: Path, value: object) -> str:
    """Atomically replace a mutable status file without exposing partial JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical(value) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return sha256(raw)


def _path_from_wsl(value: str) -> Path:
    if os.name != "nt":
        return Path(value)
    if value.startswith("/mnt/d/"):
        return Path("D:/") / value[len("/mnt/d/"):]
    if value.startswith("/mnt/c/"):
        return Path("C:/") / value[len("/mnt/c/"):]
    raise ValueError(f"unsupported WSL data path: {value}")


DATA_ROOT = _path_from_wsl(DATA_ROOT_WSL)
PILOT_ROOT = DATA_ROOT / "issue85" / "artifacts" / "pilot"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def source_identity(*, require_clean: bool = True) -> dict[str, str]:
    commit = _git(REPO, "rev-parse", "HEAD")
    lock_sha = file_sha256(LOCK_PATH)
    if require_clean:
        dirty = _git(REPO, "status", "--porcelain", "--untracked-files=all")
        if dirty:
            raise RuntimeError("Issue 85 pilot requires a clean source worktree")
    return {"package_commit": commit, "source_lock_sha256": lock_sha}


def _recorded_completed_package(progress: Mapping[str, Any]) -> str | None:
    reuse = progress.get("pilot_completed_training_reuse")
    if reuse is None:
        return None
    if not isinstance(reuse, Mapping):
        raise RuntimeError("resumed pilot has a malformed completed-package reuse record")
    predecessor = reuse.get("predecessor_package_commit")
    if predecessor is not None and not isinstance(predecessor, str):
        raise RuntimeError("resumed pilot has an invalid completed-package source identity")
    return predecessor


def validate_issue84_source(path: Path) -> Path:
    root = Path(path).resolve(strict=True)
    actual = _git(root, "rev-parse", "HEAD")
    if actual != ISSUE84_ACCEPTED_COMMIT:
        raise RuntimeError(f"Issue 84 source is {actual}; expected accepted {ISSUE84_ACCEPTED_COMMIT}")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("Issue 84 accepted source worktree must be clean")
    return root


def derive_seed(*parts: object) -> int:
    text = "issue85-pilot-v1|" + "|".join(str(value) for value in parts)
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:4], "big") & 0x7FFFFFFF


def _effective_attempt_count(carried_attempt_count: int, package_attempt_count: int) -> int:
    counts = (carried_attempt_count, package_attempt_count)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise ValueError("pilot owner attempt counts must be nonnegative integers")
    return sum(counts)


def _next_attempt_number(carried_attempt_count: int, package_attempt_count: int) -> int:
    return _effective_attempt_count(carried_attempt_count, package_attempt_count) + 1


def _carried_c_attempt_counts(
    rows: Sequence[Mapping[str, Any]], actor_update_reuse: Mapping[str, Any],
    c_outcome_reuse: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    failures = actor_update_reuse.get("prior_C_failed_attempts")
    if not isinstance(failures, list) or len(failures) != 10:
        raise RuntimeError("Issue 85 C continuation must carry exactly ten verified prior failed attempts")
    row_ids = {str(row["unit_id"]) for row in rows}
    failed_ids = [str(item.get("unit_id", "")) for item in failures if isinstance(item, Mapping)]
    if (
        len(failed_ids) != len(failures)
        or any(not unit_id for unit_id in failed_ids)
        or len(set(failed_ids)) != len(failed_ids)
        or not set(failed_ids).issubset(row_ids)
    ):
        raise RuntimeError("carried Issue 85 C failures do not bind uniquely to the current owner rows")
    counts = {unit_id: 1 for unit_id in failed_ids}
    if c_outcome_reuse is not None:
        prior_counts = c_outcome_reuse.get("prior_C_attempt_count_by_owner")
        if not isinstance(prior_counts, dict) or set(prior_counts) != row_ids:
            raise RuntimeError("carried Issue 85 C package attempts do not bind every fixed owner")
        for unit_id, count in prior_counts.items():
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise RuntimeError("carried Issue 85 C package attempts contain an invalid count")
            counts[unit_id] = counts.get(unit_id, 0) + count
    return counts


def trailing_failure_streak(events: Sequence[tuple[str, float, str]]) -> int:
    streak = 0
    for _finished_utc, _finished_monotonic, status in sorted(events):
        if status == "FAILED":
            streak += 1
        elif status == "SUCCEEDED":
            streak = 0
        else:
            raise ValueError(f"unexpected prior attempt terminal state: {status}")
    return streak


def pilot_plan(
    *, resume_from: str | None = None, reuse_completed_package: str | None = None,
) -> dict[str, Any]:
    if resume_from not in (None, PILOT_RESUME_PREDECESSOR_SHA):
        raise ValueError("pilot plan names an unregistered predecessor source")
    if reuse_completed_package not in (None, PILOT_COMPLETED_TRAINING_CARRY_FORWARD_SHA):
        raise ValueError("pilot plan names an unregistered completed package")
    if reuse_completed_package is not None and resume_from != PILOT_RESUME_PREDECESSOR_SHA:
        raise ValueError("completed-package reuse requires the registered pilot predecessor")
    training_cohort_games = len(NEW_ARMS) * len(PILOT_OWNERS)
    evaluation_cohort_games = len(POLICIES) * len(PILOT_OWNERS)
    reused_base_receipts = len(PILOT_OWNERS) + (len(PILOT_OWNERS) // 4) if resume_from is not None else 0
    reused_completed_receipts = 28 if reuse_completed_package is not None else 0
    planned_reused_receipts = reused_base_receipts + reused_completed_receipts
    new_training_slots = training_cohort_games - planned_reused_receipts
    new_evaluation_slots = evaluation_cohort_games
    new_slots = new_training_slots + new_evaluation_slots
    return {
        "scope": "PILOT_ONLY / NONFORMAL",
        "resume_from": resume_from,
        "owner_ids": list(PILOT_OWNERS),
        "training_cohort_games": training_cohort_games,
        "evaluation_cohort_games": evaluation_cohort_games,
        "training_games": training_cohort_games,
        "evaluation_games": evaluation_cohort_games,
        "planned_reused_predecessor_training_receipts": planned_reused_receipts,
        "planned_reused_completed_package_training_receipts": reused_completed_receipts,
        "planned_reused_C_training_receipts": (
            len(PILOT_OWNERS) // 4 + (len(PILOT_OWNERS) - len(PILOT_OWNERS) // 4) if reuse_completed_package is not None
            else len(PILOT_OWNERS) // 4 if resume_from is not None else 0
        ),
        "planned_reused_D_training_receipts": len(PILOT_OWNERS) if reuse_completed_package is not None else 0,
        "reuse_completed_package": reuse_completed_package,
        "C_training_outcome_carry_forward_source_commit": (
            PILOT_C_OUTCOME_CARRY_FORWARD_SHA if resume_from is not None else None
        ),
        "planned_reused_predecessor_actor_updates": (2 if reuse_completed_package is not None else 1) if resume_from is not None else 0,
        "actor_update_carry_forward_source_commits_by_arm": {
            "B": PILOT_B_ACTOR_UPDATE_CARRY_FORWARD_SHA,
            **({"C": PILOT_COMPLETED_TRAINING_CARRY_FORWARD_SHA} if reuse_completed_package is not None else {}),
        } if resume_from is not None else {},
        "actor_update_carry_forward_source_commit": (
            PILOT_B_ACTOR_UPDATE_CARRY_FORWARD_SHA if resume_from is not None else None
        ),
        "prior_failed_C_attempts_preserved_for_accounting": (10 + len(PILOT_OWNERS) // 4) if resume_from is not None else 0,
        "prior_C_package_attempts_preserved_for_accounting": len(PILOT_OWNERS) // 2 if resume_from is not None else 0,
        "new_training_game_slots_before_retries": new_training_slots,
        "new_evaluation_game_slots_before_retries": new_evaluation_slots,
        "new_game_slots_before_retries": new_slots,
        "unique_pilot_game_outcomes": training_cohort_games + evaluation_cohort_games,
        "pilot_game_executions": new_slots,
        "formal_games": 0,
        "concurrency": 8,
        "failed_attempt_handling": "pause_current_batch_for_diagnosis; preserve attempts and resume after cause is corrected",
    }


def build_seed_rows(backend: Any) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for arm in NEW_ARMS:
        for owner in PILOT_OWNERS:
            row = backend._row(owner, phase="training", arm="TRAIN", formal=False, block=0)
            row.update({
                "unit_id": f"PILOT-TRAIN:{arm}:{owner}",
                "arm": "TRAIN",
                "phase": "training",
                "formal": False,
                "block_index": 0,
                "owner_seed": owner,
                "game_seed": owner,
                "paired_owner_key": f"pilot-train-{arm}-{owner}",
                "policy_rng_seed": derive_seed("training", arm, owner, "policy"),
                "search_invocation_seeds": [
                    derive_seed("training", arm, owner, "search", index) for index in range(4)
                ],
                "issue84_arm": arm,
                "issue84_policy_label": "pi12",
                "issue85_arm": arm,
                "issue85_phase": "pilot-training",
            })
            rows.append(row)
    for owner in PILOT_OWNERS:
        for policy in POLICIES:
            row = backend._row(owner, phase="evaluation", arm=policy, formal=False, block=None)
            row.update({
                "unit_id": f"PILOT-EVAL:{owner}:{policy}",
                "arm": policy,
                "phase": "evaluation",
                "formal": False,
                "block_index": None,
                "owner_seed": owner,
                "game_seed": owner,
                "paired_owner_key": f"pilot-eval-{owner}",
                "policy_rng_seed": derive_seed("evaluation", policy, owner, "policy"),
                "issue84_arm": policy if policy in {"A", *NEW_ARMS} else None,
                "issue84_policy_label": "pi12" if policy == "PARENT" else "pi20",
                "issue85_phase": "pilot-evaluation",
            })
            if policy in NEW_ARMS:
                row["issue85_arm"] = policy
            else:
                row.pop("issue85_arm", None)
            rows.append(row)
    rows.sort(key=lambda row: (row["phase"], int(row["owner_seed"]), str(row["arm"]), str(row.get("issue85_arm", ""))))
    return {
        "schema_id": "issue85-nonformal-pilot-seed-table-v1",
        "scope": "STUDY_ONLY / PILOT_ONLY / NOT_FORMAL",
        "owner_ids": list(PILOT_OWNERS),
        "training_games_expected": len(NEW_ARMS) * len(PILOT_OWNERS),
        "evaluation_games_expected": len(POLICIES) * len(PILOT_OWNERS),
        "same_owner_seeds_reused_across_pilot_phases": True,
        "training_and_evaluation_are_separate_fresh_game_executions": True,
        "training_minibatch_owners": PILOT_MINIBATCH_OWNERS,
        "formal_training_minibatch_owners": int(CONTRACT["training"]["minibatch_owners"]),
        "rows_sha256": sha256(canonical(rows)),
        "rows": rows,
    }


def summarize_stage(result: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    results = result.get("results", {})
    completed_ids = sorted(results)
    return {
        "status": result.get("status"),
        "expected": len(rows),
        "successful_completions": len(completed_ids),
        "incomplete": sorted(str(row["unit_id"]) for row in rows if row["unit_id"] not in results),
        "attempt_count_by_owner": dict(result.get("tries", {})),
        "carried_attempt_count_by_owner": dict(result.get("carried_attempt_count_by_owner", {})),
        "failed_attempts": list(result.get("failed_attempts", [])),
        "incomplete_attempts": list(result.get("incomplete_attempts", [])),
        "elapsed_seconds": float(result.get("elapsed_seconds", 0.0)),
        "successful_unit_ids": completed_ids,
        "row_wall_seconds": dict(result.get("row_wall_seconds", {})),
        "native_wall_seconds_by_unit": dict(result.get("native_wall_seconds_by_unit", {})),
    }


def stage_resume_wall_base(
    progress: Mapping[str, Any], *, stage: str, arm: str, current_clock_elapsed: float
) -> float:
    """Recover the last stage clock plus active time since its last durable snapshot."""
    if progress.get("active_stage") != stage or str(progress.get("active_arm")) != arm:
        return 0.0
    live = progress.get("live_stage_progress")
    if not isinstance(live, Mapping) or live.get("stage") != stage or str(live.get("arm")) != arm:
        return 0.0
    # Older progress ledgers predate stage clocks. Preserve their game receipts,
    # but do not fabricate a stage duration that was never durably recorded.
    if "cumulative_active_wall_seconds" not in live or "clock_elapsed_seconds" not in live:
        return 0.0
    stage_elapsed = float(live["cumulative_active_wall_seconds"])
    last_clock_elapsed = float(live["clock_elapsed_seconds"])
    current_clock_elapsed = float(current_clock_elapsed)
    if any(not math.isfinite(value) or value < 0 for value in (stage_elapsed, last_clock_elapsed, current_clock_elapsed)):
        raise ValueError("durable pilot stage clock contains an invalid duration")
    return stage_elapsed + max(0.0, current_clock_elapsed - last_clock_elapsed)


def stage_summary_with_cumulative_wall(
    summary: Mapping[str, Any], *, stage_base: float, stage_clock_start: float, current_clock_elapsed: float
) -> dict[str, Any]:
    result = dict(summary)
    result["invocation_elapsed_seconds"] = float(summary.get("elapsed_seconds", 0.0))
    result["elapsed_seconds"] = float(stage_base) + max(0.0, float(current_clock_elapsed) - float(stage_clock_start))
    return result


_PILOT_TASK_CLOCK_KEYS = frozenset({
    "active_clock_elapsed_seconds",
    "clock_elapsed_before",
    "clock_elapsed_seconds",
    "cumulative_active_wall_seconds",
    "predecessor_progress_snapshot_active_wall_seconds",
    "predecessor_snapshot_active_wall_seconds",
    "persisted_task_clock_high_water_seconds",
    "shared_clock_active_wall_seconds_before_resume",
})


def _pilot_task_clock_high_water(*documents: object) -> float:
    """Return the maximum durable task-clock value, rejecting malformed evidence."""
    values: list[float] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if key in _PILOT_TASK_CLOCK_KEYS and nested is not None:
                    if isinstance(nested, bool) or not isinstance(nested, (int, float)):
                        raise RuntimeError(f"durable pilot clock field {key} is not a JSON number")
                    number = float(nested)
                    if not math.isfinite(number) or number < 0:
                        raise RuntimeError(f"durable pilot clock field {key} is not finite and nonnegative")
                    values.append(number)
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    for document in documents:
        visit(document)
    return max(values, default=0.0)


def _validate_pilot_clock_reconciliation(
    receipt: object,
    *,
    identity: Mapping[str, Any],
    completed_reuse: Mapping[str, Any],
    prior_wall: float,
) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise RuntimeError("pilot clock-reconciliation receipt is not an object")
    before = receipt.get("shared_clock_active_wall_seconds_before_resume")
    persisted_high_water = receipt.get("persisted_task_clock_high_water_seconds")
    difference = receipt.get("reconciled_difference_seconds")
    for label, value in (("resume clock", before), ("persisted high-water", persisted_high_water), ("reconciled difference", difference)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise RuntimeError(f"pilot clock-reconciliation {label} is invalid")
    before = float(before)
    persisted_high_water = float(persisted_high_water)
    difference = float(difference)
    if (
        receipt.get("schema_id") != "issue85-pilot-clock-reconciliation-v1"
        or receipt.get("current_package_commit") != identity["package_commit"]
        or receipt.get("predecessor_package_commit") != completed_reuse["source_commit"]
        or receipt.get("predecessor_progress_sha256") != completed_reuse["progress_sha256"]
        or receipt.get("predecessor_snapshot_active_wall_seconds") != prior_wall
        or before < prior_wall
        or persisted_high_water < prior_wall
        or persisted_high_water > before
        or difference != before - prior_wall
        or receipt.get("authority") != "shared active-wall task clock; preserve its higher cumulative value"
    ):
        raise RuntimeError("pilot clock-reconciliation receipt does not match the locked package and predecessor")
    return receipt


def _reconcile_pilot_task_clock(
    package_root: Path,
    progress_path: Path,
    progress: dict[str, Any],
    *,
    identity: Mapping[str, Any],
    completed_reuse: Mapping[str, Any],
    before: float,
    atomic_json: Any = _atomic_json,
) -> tuple[dict[str, Any], str]:
    """Validate the shared clock and idempotently persist its predecessor receipt."""
    if isinstance(before, bool) or not isinstance(before, (int, float)) or not math.isfinite(float(before)) or before < 0:
        raise RuntimeError("shared active-wall task clock is invalid")
    before = float(before)
    predecessor_progress = completed_reuse.get("progress")
    if not isinstance(predecessor_progress, Mapping):
        raise RuntimeError("completed-package reuse has no validated predecessor progress")
    prior_value = predecessor_progress.get("cumulative_active_wall_seconds")
    if isinstance(prior_value, bool) or not isinstance(prior_value, (int, float)):
        raise RuntimeError("completed-package progress has no numeric cumulative active-wall snapshot")
    prior_wall = float(prior_value)
    if not math.isfinite(prior_wall) or prior_wall < 0:
        raise RuntimeError("completed-package progress has an invalid cumulative active-wall snapshot")

    receipt_path = Path(package_root) / "pilot-progress-clock-reconciliation.json"
    has_receipt_document = "clock_reconciliation" in progress
    has_receipt_hash = "clock_reconciliation_sha256" in progress
    if has_receipt_document != has_receipt_hash:
        raise RuntimeError("pilot progress has an incomplete clock-reconciliation reference")
    receipt_exists = receipt_path.is_file()
    disk_receipt = read_json(receipt_path) if receipt_exists else None
    high_water = _pilot_task_clock_high_water(progress, predecessor_progress, disk_receipt)
    if before < high_water:
        raise RuntimeError(
            "shared active-wall task clock is below a persisted pilot high-water "
            f"({before:.9f} < {high_water:.9f}); refusing to undercount resource use"
        )

    if has_receipt_document:
        if not receipt_exists:
            raise RuntimeError("pilot progress references a missing immutable clock-reconciliation receipt")
        receipt = disk_receipt
        if (
            receipt != progress["clock_reconciliation"]
            or progress["clock_reconciliation_sha256"] != file_sha256(receipt_path)
        ):
            raise RuntimeError("pilot clock-reconciliation receipt does not match its progress hash binding")
        receipt = _validate_pilot_clock_reconciliation(
            receipt, identity=identity, completed_reuse=completed_reuse, prior_wall=prior_wall,
        )
    elif receipt_exists:
        # A crash may have committed the immutable receipt just before the
        # mutable progress reference. Validate and adopt that exact orphan.
        receipt = _validate_pilot_clock_reconciliation(
            disk_receipt, identity=identity, completed_reuse=completed_reuse, prior_wall=prior_wall,
        )
    else:
        receipt = {
            "schema_id": "issue85-pilot-clock-reconciliation-v1",
            "current_package_commit": identity["package_commit"],
            "predecessor_package_commit": completed_reuse["source_commit"],
            "predecessor_progress_sha256": completed_reuse["progress_sha256"],
            "predecessor_snapshot_active_wall_seconds": prior_wall,
            "shared_clock_active_wall_seconds_before_resume": before,
            "persisted_task_clock_high_water_seconds": high_water,
            "reconciled_difference_seconds": before - prior_wall,
            "authority": "shared active-wall task clock; preserve its higher cumulative value",
        }
        _write_or_verify_once(receipt_path, receipt)

    receipt_sha = file_sha256(receipt_path)
    progress["clock_reconciliation"] = receipt
    progress["clock_reconciliation_sha256"] = receipt_sha
    progress["cumulative_active_wall_seconds"] = before
    progress["status"] = "READY"
    progress.pop("supervisor_pid", None)
    atomic_json(progress_path, progress)
    return receipt, receipt_sha


def stage_summary_is_complete(
    summary: Mapping[str, Any] | None,
    expected: int,
    expected_unit_ids: Sequence[str] | None = None,
) -> bool:
    complete = bool(
        summary is not None
        and summary.get("status") == "SUCCEEDED"
        and int(summary.get("expected", -1)) == expected
        and int(summary.get("successful_completions", -1)) == expected
        and not summary.get("incomplete")
    )
    return complete and (
        expected_unit_ids is None
        or sorted(str(value) for value in summary.get("successful_unit_ids", []))
        == sorted(str(value) for value in expected_unit_ids)
    )


def _receipt_path_matches(saved: Any, expected: Path) -> bool:
    return (
        isinstance(saved, str)
        and Path(saved).is_absolute()
        and Path(saved).resolve() == Path(expected).resolve()
    )


def _receipt_number(value: Any, *, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"{description} is not a numeric receipt")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise RuntimeError(f"{description} is not finite and non-negative")
    return number


def pilot_success_record(
    backend: Any, *, root: Path, row: Mapping[str, Any], package_commit: str
) -> tuple[dict[str, Any], Path] | None:
    # Return the one successful pilot only after verifying its complete receipt chain.
    root = Path(root)
    unit_id = str(row["unit_id"])
    unit_root = root / "units" / backend.unit_key(unit_id)
    attempts = sorted(path for path in unit_root.glob("attempt-*") if path.is_dir())
    successes = []
    for attempt in attempts:
        result_path = attempt / "result.json"
        if not result_path.is_file():
            continue
        result = read_json(result_path)
        if result.get("status") == "SUCCEEDED":
            successes.append((result, result_path))
    if len(successes) > 1:
        raise RuntimeError(f"pilot owner {unit_id} has multiple successful attempts")
    if not successes:
        return None

    result, result_path = successes[0]
    attempt = result_path.parent
    job_path = attempt / "worker-job.json"
    started_path = attempt / "started.json"
    finished_path = attempt / "finished.json"
    if not all(path.is_file() for path in (job_path, started_path, finished_path)):
        raise RuntimeError(f"pilot success {unit_id} lacks its complete job/start/finish/result chain")
    job = read_json(job_path)
    started = read_json(started_path)
    finished = read_json(finished_path)
    source_lock_sha256 = backend.source_lock_sha()
    run_id = f"{unit_id}-{attempt.name}"

    if (
        job.get("schema_id") != "issue79-owner-worker-job-v1"
        or not _receipt_path_matches(job.get("artifact_root"), root)
        or not _receipt_path_matches(job.get("attempt_dir"), attempt)
        or not _receipt_path_matches(job.get("result_path"), result_path)
        or not _receipt_path_matches(job.get("output_dir"), attempt / "native")
        or job.get("run_id") != run_id
        or job.get("unit_id") != unit_id
        or job.get("phase") != row.get("phase")
        or job.get("row") != dict(row)
        or job.get("pilot") is not True
        or job.get("package_commit") != package_commit
        or job.get("runner_code_commit") != package_commit
        or job.get("source_lock_sha256") != source_lock_sha256
    ):
        raise RuntimeError(f"pilot worker-job receipt differs from the frozen source/row: {unit_id}")

    started_monotonic = _receipt_number(
        started.get("started_monotonic"), description=f"pilot start time for {unit_id}",
    )
    finished_monotonic = _receipt_number(
        finished.get("finished_monotonic"), description=f"pilot finish time for {unit_id}",
    )
    if (
        started.get("schema_id") != "issue79-owner-attempt-start-v1"
        or started.get("unit_id") != unit_id
        or started.get("owner_seed") != row.get("owner_seed")
        or started.get("arm") != row.get("arm")
        or started.get("run_id") != run_id
        or started.get("package_commit") != package_commit
        or started.get("seed_row_sha256") != sha256(canonical(dict(row)))
        or not isinstance(started.get("started_utc"), str)
        or not started.get("started_utc")
        or isinstance(started.get("pid"), bool)
        or not isinstance(started.get("pid"), int)
        or started["pid"] <= 0
        or finished.get("schema_id") != "issue79-owner-attempt-finish-v1"
        or finished.get("unit_id") != unit_id
        or finished.get("run_id") != run_id
        or finished.get("status") != "SUCCEEDED"
        or not isinstance(finished.get("finished_utc"), str)
        or not finished.get("finished_utc")
        or finished_monotonic < started_monotonic
        or finished.get("result_sha256") != file_sha256(result_path)
    ):
        raise RuntimeError(f"pilot start/finish receipts disagree with the successful result: {unit_id}")

    resource = result.get("resource")
    if not isinstance(resource, dict):
        raise RuntimeError(f"pilot success {unit_id} lacks worker resource accounting")
    worker_wall = _receipt_number(
        resource.get("worker_wall_seconds"), description=f"pilot worker wall time for {unit_id}",
    )
    worker_cpu = _receipt_number(
        resource.get("worker_process_cpu_seconds"), description=f"pilot worker CPU time for {unit_id}",
    )
    terminal_floor = result.get("terminal_floor")
    if (
        result.get("schema_id") != "issue75-stage1-unit-result-v1"
        or result.get("status") != "SUCCEEDED"
        or result.get("unit_id") != unit_id
        or result.get("owner_seed") != row.get("owner_seed")
        or result.get("phase") != row.get("phase")
        or result.get("arm") != row.get("arm")
        or result.get("formal") is not False
        or result.get("package_commit") != package_commit
        or result.get("runner_code_commit") != package_commit
        or result.get("source_lock_sha256") != source_lock_sha256
        or result.get("game_seed") != row.get("game_seed")
        or result.get("paired_owner_key") != row.get("paired_owner_key")
        or resource.get("ledger_run_id") != run_id
        or isinstance(terminal_floor, bool)
        or not isinstance(terminal_floor, int)
        or not isinstance(result.get("terminal_outcome"), str)
        or not result.get("terminal_outcome")
        or worker_wall < 0
        or worker_cpu < 0
    ):
        raise RuntimeError(f"pilot result receipt differs from the frozen source/row or terminal: {unit_id}")
    return result, result_path


def stage_receipts_are_complete(
    backend: Any, *, root: Path, rows: Sequence[Mapping[str, Any]], package_commit: str,
    receipt_roots_by_owner: Mapping[str, tuple[Path, str]] | None = None,
) -> bool:
    return all(
        pilot_success_record(
            backend,
            root=(receipt_roots_by_owner or {}).get(str(row["unit_id"]), (root, package_commit))[0],
            row=row,
            package_commit=(receipt_roots_by_owner or {}).get(str(row["unit_id"]), (root, package_commit))[1],
        ) is not None
        for row in rows
    )


def _validate_predecessor_progress(
    progress: Mapping[str, Any],
    *,
    predecessor_commit: str,
    current_identity: Mapping[str, str],
    prior_seed_table: Mapping[str, Any],
    current_seed_table: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate the one known partial run that may donate completed B games."""
    if predecessor_commit != PILOT_RESUME_PREDECESSOR_SHA:
        raise RuntimeError("Issue 85 pilot continuation names an unregistered predecessor source")
    if predecessor_commit == current_identity.get("package_commit"):
        raise RuntimeError("Issue 85 pilot continuation predecessor must differ from the current source")
    if (
        progress.get("schema_id") != "issue85-pilot-progress-v1"
        or progress.get("status") != "FAILED_OR_INCOMPLETE"
        or progress.get("package_commit") != predecessor_commit
        or progress.get("source_lock_sha256") != current_identity.get("source_lock_sha256")
        or progress.get("issue84_source_commit") != ISSUE84_ACCEPTED_COMMIT
        or progress.get("active_stage") != "pilot-actor-update"
        or progress.get("active_arm") != "B"
        or progress.get("failure", {}).get("type") != "AttributeError"
        or progress.get("failure", {}).get("detail")
        != "module 'issue85_exact_issue84_driver' has no attribute '_training_examples'"
        or set(progress.get("training_runs", {})) != {"B"}
        or progress.get("evaluation_runs") != {}
        or progress.get("actor_updates") != {}
    ):
        raise RuntimeError("Issue 85 predecessor progress is not the reviewed B-game-only adapter failure")
    prior_rows = prior_seed_table.get("rows")
    current_rows = current_seed_table.get("rows")
    if (
        not isinstance(prior_rows, list)
        or prior_seed_table.get("schema_id") != "issue85-nonformal-pilot-seed-table-v1"
        or prior_seed_table.get("rows_sha256") != sha256(canonical(prior_rows))
        or prior_seed_table.get("rows_sha256") != current_seed_table.get("rows_sha256")
        or prior_rows != current_rows
        or progress.get("seed_table_rows") != len(prior_rows)
    ):
        raise RuntimeError("Issue 85 predecessor does not use the exact unchanged pilot owner/seed table")
    rows = [
        dict(row) for row in prior_rows
        if row.get("phase") == "training" and row.get("issue85_arm") == "B"
    ]
    expected_ids = sorted(str(row["unit_id"]) for row in rows)
    summary = progress.get("training_runs", {}).get("B")
    if (
        len(rows) != len(PILOT_OWNERS)
        or not stage_summary_is_complete(summary, len(rows), expected_ids)
        or summary.get("failed_attempts") != []
        or summary.get("incomplete_attempts") != []
        or summary.get("incomplete") != []
        or any(int(value) != 1 for value in summary.get("attempt_count_by_owner", {}).values())
        or sorted(summary.get("attempt_count_by_owner", {})) != expected_ids
    ):
        raise RuntimeError("Issue 85 predecessor B training summary does not prove one success for every fixed owner")
    return rows, dict(summary)


def _validate_predecessor_reuse_source_diff(changed_paths: Sequence[str]) -> None:
    """Accept only reviewed runner/API fixes and the reviewed public-schema repair."""
    expected_paths = sorted((
        "studies/issue-85-representation/feature_firewall.py",
        "studies/issue-85-representation/pilot_runner.py",
        "studies/issue-85-representation/tests/test_feature_firewall.py",
        "studies/issue-85-representation/tests/test_pilot_runner.py",
        "studies/issue-85-representation/runtime_integration.py",
        "studies/issue-85-representation/tests/test_runtime_integration.py",
    ))
    observed_paths = sorted(filter(None, (str(path) for path in changed_paths)))
    if observed_paths != expected_paths:
        raise RuntimeError(
            "Issue 85 predecessor reuse requires only reviewed runner/API repairs, "
            "the public-schema repair and their tests"
        )


def _validate_b_actor_update_carry_forward_source_diff(changed_paths: Sequence[str]) -> None:
    """Bind B actor reuse to the reviewed public-schema and continuation changes."""
    expected_paths = sorted((
        "studies/issue-85-representation/feature_firewall.py",
        "studies/issue-85-representation/pilot_runner.py",
        "studies/issue-85-representation/tests/test_feature_firewall.py",
        "studies/issue-85-representation/tests/test_pilot_runner.py",
    ))
    observed_paths = sorted(filter(None, (str(path) for path in changed_paths)))
    if observed_paths != expected_paths:
        raise RuntimeError(
            "B actor-update carry-forward requires only the reviewed public-schema "
            "repair and this runner's carry-forward validation"
        )


def _optional_receipt_field_matches(receipt: Mapping[str, Any], field: str, expected: str) -> bool:
    """Allow absent wrapper fields on failures, but reject conflicting bindings."""
    return field not in receipt or receipt.get(field) == expected


def _worker_job_paths_match(
    job: Mapping[str, Any], *, stage_root: Path, attempt_dir: Path, result_path: Path,
) -> bool:
    """Bind a worker receipt to its stage root and exact per-owner attempt files."""
    return (
        Path(str(job.get("artifact_root", ""))).resolve() == Path(stage_root).resolve()
        and Path(str(job.get("attempt_dir", ""))).resolve() == Path(attempt_dir).resolve()
        and Path(str(job.get("result_path", ""))).resolve() == Path(result_path).resolve()
    )


def _load_predecessor_b_actor_update(
    backend: Any,
    *,
    identity: Mapping[str, str],
    current_seed_table: Mapping[str, Any],
    b_game_predecessor: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and reference the completed B update from the failed C pilot."""
    predecessor_commit = PILOT_B_ACTOR_UPDATE_CARRY_FORWARD_SHA
    if predecessor_commit == identity.get("package_commit"):
        raise RuntimeError("B actor-update predecessor must differ from the current source")
    is_ancestor = subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", predecessor_commit, identity["package_commit"]],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if is_ancestor.returncode != 0:
        raise RuntimeError("B actor-update carry-forward source is not an ancestor of the current source")
    changed_paths = sorted(filter(None, _git(REPO, "diff", "--name-only", predecessor_commit, identity["package_commit"]).splitlines()))
    _validate_b_actor_update_carry_forward_source_diff(changed_paths)

    prior_root = PILOT_ROOT / predecessor_commit
    progress_path = prior_root / "pilot-progress.json"
    seed_path = prior_root / "pilot-seed-table.json"
    if not progress_path.is_file() or not seed_path.is_file() or (prior_root / "pilot-report.json").exists():
        raise RuntimeError("B actor-update predecessor lacks failed-run progress/seed evidence or has a final report")
    progress = read_json(progress_path)
    prior_seed_table = read_json(seed_path)
    if (
        file_sha256(seed_path) != progress.get("seed_table_sha256")
        or prior_seed_table != current_seed_table
        or progress.get("schema_id") != "issue85-pilot-progress-v1"
        or progress.get("status") != "FAILED_OR_INCOMPLETE"
        or progress.get("package_commit") != predecessor_commit
        or progress.get("source_lock_sha256") != identity.get("source_lock_sha256")
        or progress.get("issue84_source_commit") != ISSUE84_ACCEPTED_COMMIT
        or progress.get("active_stage") != "training-games"
        or progress.get("active_arm") != "C"
        or progress.get("evaluation_runs") != {}
        or set(progress.get("training_runs", {})) != {"B", "C"}
        or set(progress.get("actor_updates", {})) != {"B"}
    ):
        raise RuntimeError("B actor-update predecessor is not the reviewed failed C-game continuation")

    predecessor_manifest_path = prior_root / "pilot-predecessor-training-reuse.json"
    predecessor_manifest = read_json(predecessor_manifest_path) if predecessor_manifest_path.is_file() else None
    if (
        not isinstance(predecessor_manifest, dict)
        or predecessor_manifest != progress.get("predecessor_training_reuse")
        or file_sha256(predecessor_manifest_path) != progress.get("predecessor_training_reuse_sha256")
        or predecessor_manifest.get("current_package_commit") != predecessor_commit
        or predecessor_manifest.get("predecessor_package_commit") != PILOT_RESUME_PREDECESSOR_SHA
        or predecessor_manifest.get("source_lock_sha256") != identity.get("source_lock_sha256")
        or predecessor_manifest.get("rows_sha256") != prior_seed_table.get("rows_sha256")
        or predecessor_manifest.get("successful_game_count") != len(PILOT_OWNERS)
        or progress.get("training_runs", {}).get("B") != b_game_predecessor.get("summary")
    ):
        raise RuntimeError("B actor-update predecessor does not preserve the validated 16-owner B game reuse")

    c_rows = [
        dict(row) for row in prior_seed_table.get("rows", [])
        if row.get("phase") == "training" and row.get("issue85_arm") == "C"
    ]
    expected_c_ids = {str(row["unit_id"]): row for row in c_rows}
    c_summary = progress.get("training_runs", {}).get("C")
    failed = c_summary.get("failed_attempts") if isinstance(c_summary, dict) else None
    if (
        len(c_rows) != len(PILOT_OWNERS)
        or not isinstance(failed, list)
        or c_summary.get("status") != "REPEATED_FAILED_ATTEMPTS"
        or c_summary.get("expected") != len(PILOT_OWNERS)
        or c_summary.get("successful_completions") != 0
        or c_summary.get("successful_unit_ids") != []
        or c_summary.get("incomplete_attempts") != []
        or len(failed) != 10
        or set(c_summary.get("attempt_count_by_owner", {})) != set(expected_c_ids)
        or sum(int(value) for value in c_summary.get("attempt_count_by_owner", {}).values()) != len(failed)
        or sum(int(value) == 0 for value in c_summary.get("attempt_count_by_owner", {}).values()) != 6
    ):
        raise RuntimeError("B actor-update predecessor C ledger is not 10 failed, 0 successful and 6 unstarted owners")
    failure_rows: dict[str, Mapping[str, Any]] = {}
    for failure in failed:
        unit_id = str(failure.get("unit_id", ""))
        if unit_id not in expected_c_ids or unit_id in failure_rows:
            raise RuntimeError("B actor-update predecessor contains duplicate/unregistered C failures")
        if failure.get("attempt") != f"{unit_id}-attempt-0001":
            raise RuntimeError("B actor-update predecessor C failure is not the retained first attempt")
        failure_rows[unit_id] = failure
    failure_receipts = []
    for unit_id, row in expected_c_ids.items():
        owner_root = prior_root / "training" / "C" / "units" / str(backend.unit_key(unit_id))
        attempt = owner_root / "attempt-0001"
        count = int(c_summary["attempt_count_by_owner"][unit_id])
        if unit_id in failure_rows:
            if count != 1 or not attempt.is_dir():
                raise RuntimeError(f"B actor-update predecessor C failed owner receipt is missing: {unit_id}")
            job_path = attempt / "worker-job.json"
            started_path = attempt / "started.json"
            finished_path = attempt / "finished.json"
            result_path = attempt / "result.json"
            if not all(path.is_file() for path in (job_path, started_path, finished_path, result_path)):
                raise RuntimeError(f"B actor-update predecessor C attempt chain is incomplete: {unit_id}")
            job, started = read_json(job_path), read_json(started_path)
            finished, result = read_json(finished_path), read_json(result_path)
            run_id = f"{unit_id}-attempt-0001"
            detail = (
                f"TRAIN_{row['owner_seed']} is incomplete or invalid: "
                "['candidates[0].features.public_destination.value must be a scalar']"
            )
            if (
                job.get("schema_id") != "issue79-owner-worker-job-v1"
                or job.get("unit_id") != unit_id
                or job.get("row") != row
                or job.get("run_id") != run_id
                or job.get("package_commit") != predecessor_commit
                or job.get("runner_code_commit") != predecessor_commit
                or job.get("source_lock_sha256") != identity.get("source_lock_sha256")
                or not _worker_job_paths_match(
                    job, stage_root=prior_root / "training" / "C",
                    attempt_dir=attempt, result_path=result_path,
                )
                or started.get("schema_id") != "issue79-owner-attempt-start-v1"
                or started.get("unit_id") != unit_id
                or started.get("run_id") != run_id
                or started.get("package_commit") != predecessor_commit
                or started.get("seed_row_sha256") != sha256(canonical(row))
                or finished.get("schema_id") != "issue79-owner-attempt-finish-v1"
                or finished.get("unit_id") != unit_id
                or finished.get("run_id") != run_id
                or finished.get("status") != "FAILED"
                or finished.get("result_sha256") != file_sha256(result_path)
                or result.get("schema_id") != "issue79-owner-attempt-result-v1"
                or result.get("unit_id") != unit_id
                or not _optional_receipt_field_matches(result, "package_commit", predecessor_commit)
                or not _optional_receipt_field_matches(result, "runner_code_commit", predecessor_commit)
                or not _optional_receipt_field_matches(
                    result, "source_lock_sha256", str(identity.get("source_lock_sha256")),
                )
                or result.get("status") != "FAILED"
                or result.get("terminal_outcome") is not None
                or result.get("failure_detail") != detail
                or failure_rows[unit_id].get("failure") != detail
            or failure_rows[unit_id].get("failure_type") != "StudyFailure"
            or failure_rows[unit_id].get("exit_code") != 1
            ):
                raise RuntimeError(f"B actor-update predecessor C failed receipt binding differs: {unit_id}")
            failure_receipts.append({
                "unit_id": unit_id,
                "attempt": "attempt-0001",
                "worker_job_sha256": file_sha256(job_path),
                "started_sha256": file_sha256(started_path),
                "finished_sha256": file_sha256(finished_path),
                "result_sha256": file_sha256(result_path),
                "failure_detail": detail,
            })
        elif count != 0 or owner_root.exists():
            raise RuntimeError(f"B actor-update predecessor claims unstarted C owner has attempt evidence: {unit_id}")
    live = progress.get("live_stage_progress", {})
    if (
        live.get("stage") != "training-games"
        or live.get("arm") != "C"
        or live.get("expected") != len(PILOT_OWNERS)
        or live.get("successful_completions") != 0
        or live.get("failed_attempt_count") != len(failed)
        or progress.get("failure", {}).get("type") != "RuntimeError"
        or progress.get("failure", {}).get("detail")
        != "Issue 85 pilot training games for C stopped with REPEATED_FAILED_ATTEMPTS: 0/16"
    ):
        raise RuntimeError("B actor-update predecessor does not retain the expected C repeated-failure stop evidence")

    update_root = prior_root / "training" / "B" / "pilot-updates" / "B"
    attempts = sorted(path for path in update_root.glob("attempt-*") if path.is_dir())
    if [path.name for path in attempts] != ["attempt-0001"]:
        raise RuntimeError("B actor-update predecessor does not contain exactly one retained B update attempt")
    update_attempt = attempts[0]
    receipt_path = update_attempt / "receipt.json"
    update_progress_path = update_attempt / "progress.json"
    if not receipt_path.is_file() or not update_progress_path.is_file():
        raise RuntimeError("B actor-update predecessor lacks its immutable receipt or progress")
    receipt = read_json(receipt_path)
    update_progress = read_json(update_progress_path)
    if progress.get("actor_updates", {}).get("B") != receipt:
        raise RuntimeError("B actor-update receipt differs from the prior run's durable ledger")
    owner_sha = sha256(",".join(str(owner) for owner in PILOT_OWNERS).encode("ascii"))
    if (
        receipt.get("schema_id") != "issue85-pilot-actor-update-v1"
        or receipt.get("status") != "SUCCEEDED"
        or receipt.get("scope") != "PILOT_ONLY / NONFORMAL"
        or receipt.get("arm") != "B"
        or receipt.get("package_commit") != predecessor_commit
        or receipt.get("source_lock_sha256") != identity.get("source_lock_sha256")
        or receipt.get("owner_seed_sha256") != owner_sha
        or receipt.get("owner_count") != len(PILOT_OWNERS)
        or receipt.get("minibatch_owner_count") != len(PILOT_OWNERS)
        or receipt.get("objective") != "issue84-B-LENGTH_ONLY"
        or receipt.get("completed_steps") != ACTOR_STEPS
        or receipt.get("optimizer_parameter_step_values") != [128]
        or receipt.get("loss_summary", {}).get("finite_all_steps") is not True
        or receipt.get("initial_probability_parity", {}).get("status") != "PASS"
        or receipt.get("initial_probability_parity", {}).get("uses_outcomes_for_efficacy") is not False
        or receipt.get("model_digest") is None
        or not math.isfinite(float(receipt.get("elapsed_seconds", -1)))
        or float(receipt.get("elapsed_seconds", -1)) < 0
        or update_progress.get("schema_id") != "issue85-pilot-actor-update-progress-v1"
        or update_progress.get("status") != "SUCCEEDED"
        or update_progress.get("arm") != "B"
        or update_progress.get("package_commit") != predecessor_commit
        or update_progress.get("source_lock_sha256") != identity.get("source_lock_sha256")
        or update_progress.get("owner_seed_sha256") != owner_sha
        or update_progress.get("completed_steps") != ACTOR_STEPS
        or update_progress.get("model_digest") != receipt.get("model_digest")
        or update_progress.get("checkpoint_sha256") != receipt.get("checkpoint_sha256")
        or update_progress.get("optimizer_sha256") != receipt.get("optimizer_sha256")
    ):
        raise RuntimeError("B actor-update predecessor receipt/progress is not a completed, bound 64-step update")
    checkpoint_path = (prior_root / str(receipt.get("checkpoint_path", ""))).resolve()
    optimizer_path = (prior_root / str(receipt.get("optimizer_path", ""))).resolve()
    if (
        prior_root.resolve() not in checkpoint_path.parents
        or prior_root.resolve() not in optimizer_path.parents
        or not checkpoint_path.is_file()
        or not optimizer_path.is_file()
        or file_sha256(checkpoint_path) != receipt.get("checkpoint_sha256")
        or file_sha256(optimizer_path) != receipt.get("optimizer_sha256")
    ):
        raise RuntimeError("B actor-update predecessor checkpoint/optimizer is missing or hash-invalid")

    document = {
        "schema_id": "issue85-pilot-actor-update-reuse-v1",
        "scope": "PILOT_ONLY / NONFORMAL",
        "current_package_commit": identity["package_commit"],
        "predecessor_package_commit": predecessor_commit,
        "source_lock_sha256": identity["source_lock_sha256"],
        "issue84_source_commit": ISSUE84_ACCEPTED_COMMIT,
        "source_diff_paths": changed_paths,
        "progress_sha256": file_sha256(progress_path),
        "seed_table_sha256": file_sha256(seed_path),
        "b_training_game_reuse_sha256": file_sha256(predecessor_manifest_path),
        "actor_update_attempt": update_attempt.name,
        "actor_update_progress_sha256": file_sha256(update_progress_path),
        "actor_update_receipt_sha256": file_sha256(receipt_path),
        "actor_update_receipt": receipt,
        "checkpoint_sha256": receipt["checkpoint_sha256"],
        "optimizer_sha256": receipt["optimizer_sha256"],
        "prior_C_failed_attempts": failure_receipts,
        "prior_C_successful_games": 0,
        "prior_C_unstarted_owners": 6,
        "reuse_rule": "reference the exact successful B update checkpoint and optimizer; do not repeat the completed B actor update; preserve prior failed C attempts and retry only under the reviewed current source",
    }
    return {"source_commit": predecessor_commit, "root": prior_root, "receipt": receipt, "document": document}


def _load_predecessor_b_training(
    backend: Any,
    *,
    identity: Mapping[str, str],
    current_seed_table: Mapping[str, Any],
    predecessor_commit: str,
) -> dict[str, Any]:
    if predecessor_commit != PILOT_RESUME_PREDECESSOR_SHA:
        raise RuntimeError("Issue 85 pilot continuation names an unregistered predecessor source")
    if identity.get("package_commit") == predecessor_commit:
        raise RuntimeError("Issue 85 pilot continuation predecessor must differ from the current source")
    is_ancestor = subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", predecessor_commit, identity["package_commit"]],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if is_ancestor.returncode != 0:
        raise RuntimeError("Issue 85 pilot continuation source is not descended from its exact predecessor")
    changed_paths = sorted(filter(None, _git(REPO, "diff", "--name-only", predecessor_commit, identity["package_commit"]).splitlines()))
    _validate_predecessor_reuse_source_diff(changed_paths)

    prior_root = PILOT_ROOT / predecessor_commit
    progress_path = prior_root / "pilot-progress.json"
    seed_path = prior_root / "pilot-seed-table.json"
    if not progress_path.is_file() or not seed_path.is_file() or (prior_root / "pilot-report.json").exists():
        raise RuntimeError("Issue 85 predecessor lacks its failed progress/seed evidence or unexpectedly has a final report")
    progress = read_json(progress_path)
    prior_seed_table = read_json(seed_path)
    if file_sha256(seed_path) != progress.get("seed_table_sha256"):
        raise RuntimeError("Issue 85 predecessor seed table hash differs from its progress ledger")
    rows, summary = _validate_predecessor_progress(
        progress,
        predecessor_commit=predecessor_commit,
        current_identity=identity,
        prior_seed_table=prior_seed_table,
        current_seed_table=current_seed_table,
    )
    update_root = prior_root / "training" / "B" / "pilot-updates" / "B"
    if update_root.exists() and any(update_root.iterdir()):
        raise RuntimeError("Issue 85 predecessor unexpectedly contains a B actor-update attempt; inspect it before reuse")
    receipt_root = prior_root / "training" / "B"
    expected_units = {str(backend.unit_key(row["unit_id"])) for row in rows}
    units_root = receipt_root / "units"
    actual_units = {
        path.name for path in units_root.iterdir() if path.is_dir()
    } if units_root.is_dir() else set()
    if actual_units != expected_units:
        raise RuntimeError("Issue 85 predecessor B evidence contains missing or unregistered owner directories")
    for row in rows:
        attempts = sorted(
            path.name for path in (units_root / str(backend.unit_key(row["unit_id"]))).glob("attempt-*")
            if path.is_dir()
        )
        if attempts != ["attempt-0001"]:
            raise RuntimeError(
                f"Issue 85 predecessor B owner {row['unit_id']} does not have exactly one first attempt"
            )
    if not stage_receipts_are_complete(
        backend, root=receipt_root, rows=rows, package_commit=predecessor_commit,
    ):
        raise RuntimeError("Issue 85 predecessor B training receipts fail strict source/row/hash validation")
    current_b_units = PILOT_ROOT / identity["package_commit"] / "training" / "B" / "units"
    if current_b_units.is_dir() and any(path.is_dir() for path in current_b_units.iterdir()):
        raise RuntimeError("continuation package already contains B training attempts; inspect them before resuming")
    unit_receipts = []
    for row in rows:
        result, result_path = pilot_success_record(
            backend, root=receipt_root, row=row, package_commit=predecessor_commit,
        ) or (None, None)
        if result is None or result_path is None:
            raise RuntimeError(f"Issue 85 predecessor lost a validated B game receipt: {row['unit_id']}")
        attempt = result_path.parent
        unit_receipts.append({
            "unit_id": str(row["unit_id"]),
            "attempt": attempt.name,
            "worker_job_sha256": file_sha256(attempt / "worker-job.json"),
            "started_sha256": file_sha256(attempt / "started.json"),
            "finished_sha256": file_sha256(attempt / "finished.json"),
            "result_sha256": file_sha256(result_path),
        })
    return {
        "source_commit": predecessor_commit,
        "source_lock_sha256": identity["source_lock_sha256"],
        "issue84_source_commit": ISSUE84_ACCEPTED_COMMIT,
        "root": prior_root,
        "progress": progress,
        "progress_sha256": file_sha256(progress_path),
        "seed_table": prior_seed_table,
        "seed_table_sha256": file_sha256(seed_path),
        "rows": rows,
        "summary": summary,
        "receipt_root": receipt_root,
        "unit_receipts": unit_receipts,
        "predecessor_progress_snapshot_active_wall_seconds": float(progress["cumulative_active_wall_seconds"]),
        "source_diff_paths": changed_paths,
    }


def _predecessor_receipt_document(predecessor: Mapping[str, Any], *, current_identity: Mapping[str, str]) -> dict[str, Any]:
    return {
        "schema_id": "issue85-pilot-predecessor-training-reuse-v1",
        "scope": "PILOT_ONLY / NONFORMAL",
        "current_package_commit": current_identity["package_commit"],
        "predecessor_package_commit": predecessor["source_commit"],
        "source_lock_sha256": current_identity["source_lock_sha256"],
        "issue84_source_commit": ISSUE84_ACCEPTED_COMMIT,
        "stage": "training/B",
        "evidence_root": str(predecessor["root"].resolve()),
        "progress_sha256": predecessor["progress_sha256"],
        "seed_table_sha256": predecessor["seed_table_sha256"],
        "rows_sha256": predecessor["seed_table"]["rows_sha256"],
        "summary_sha256": sha256(canonical(predecessor["summary"])),
        "source_diff_paths": predecessor["source_diff_paths"],
        "successful_unit_ids": sorted(str(row["unit_id"]) for row in predecessor["rows"]),
        "validated_unit_receipts": predecessor["unit_receipts"],
        "successful_game_count": len(predecessor["rows"]),
        "predecessor_progress_snapshot_active_wall_seconds": predecessor[
            "predecessor_progress_snapshot_active_wall_seconds"
        ],
        "reuse_rule": "reference the immutable predecessor receipt chains; do not copy, relabel, or rerun those games",
    }


def _load_predecessor_c_training_outcomes(
    backend: Any,
    *,
    identity: Mapping[str, str],
    current_seed_table: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate fafbb's four completed C games and carry them by immutable reference."""
    predecessor_commit = PILOT_C_OUTCOME_CARRY_FORWARD_SHA
    is_ancestor = subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", predecessor_commit, identity["package_commit"]],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if is_ancestor.returncode != 0:
        raise RuntimeError("Issue 85 C outcome source is not an ancestor of the current source")
    changed_paths = sorted(filter(None, _git(REPO, "diff", "--name-only", predecessor_commit, identity["package_commit"]).splitlines()))
    expected_paths = sorted((
        "studies/issue-85-representation/feature_firewall.py",
        "studies/issue-85-representation/pilot_runner.py",
        "studies/issue-85-representation/tests/test_feature_firewall.py",
        "studies/issue-85-representation/tests/test_pilot_runner.py",
    ))
    if changed_paths != expected_paths:
        raise RuntimeError("Issue 85 C outcome reuse is limited to the reviewed schema/runner repair and tests")

    prior_root = PILOT_ROOT / predecessor_commit
    progress_path = prior_root / "pilot-progress.json"
    seed_path = prior_root / "pilot-seed-table.json"
    actor_reuse_path = prior_root / "pilot-actor-update-reuse.json"
    if not all(path.is_file() for path in (progress_path, seed_path, actor_reuse_path)):
        raise RuntimeError("Issue 85 C outcome predecessor is missing immutable progress, seed, or actor-reuse evidence")
    progress = read_json(progress_path)
    prior_seed_table = read_json(seed_path)
    actor_reuse = read_json(actor_reuse_path)
    if (
        prior_seed_table != current_seed_table
        or file_sha256(seed_path) != progress.get("seed_table_sha256")
        or progress.get("schema_id") != "issue85-pilot-progress-v1"
        or progress.get("status") != "FAILED_OR_INCOMPLETE"
        or progress.get("package_commit") != predecessor_commit
        or progress.get("source_lock_sha256") != identity.get("source_lock_sha256")
        or progress.get("issue84_source_commit") != ISSUE84_ACCEPTED_COMMIT
        or progress.get("active_stage") != "training-games"
        or progress.get("active_arm") != "C"
        or progress.get("failure") != {
            "type": "RuntimeError",
            "detail": "Issue 85 pilot training games for C stopped with OWNER_RETRY_LIMIT_REACHED: 4/16",
        }
        or progress.get("evaluation_runs") != {}
        or set(progress.get("training_runs", {})) != set(("B", "C"))
        or set(progress.get("actor_updates", {})) != {"B"}
        or actor_reuse != progress.get("actor_update_reuse")
        or file_sha256(actor_reuse_path) != progress.get("actor_update_reuse_sha256")
        or actor_reuse.get("current_package_commit") != predecessor_commit
        or actor_reuse.get("prior_C_successful_games") != 0
        or actor_reuse.get("prior_C_unstarted_owners") != 6
        or len(actor_reuse.get("prior_C_failed_attempts", [])) != 10
    ):
        raise RuntimeError("Issue 85 C outcome predecessor does not match the immutable four-success/eight-attempt pilot")

    rows = [
        dict(row) for row in prior_seed_table.get("rows", [])
        if row.get("phase") == "training" and row.get("issue85_arm") == "C"
    ]
    row_by_id = {str(row["unit_id"]): row for row in rows}
    summary = progress.get("training_runs", {}).get("C")
    original_failure_ids = {
        str(item.get("unit_id", "")) for item in actor_reuse["prior_C_failed_attempts"]
    }
    if (
        not isinstance(summary, dict)
        or len(rows) != len(PILOT_OWNERS)
        or len(row_by_id) != len(rows)
        or summary.get("status") != "OWNER_RETRY_LIMIT_REACHED"
        or summary.get("expected") != len(PILOT_OWNERS)
        or summary.get("successful_completions") != len(PILOT_OWNERS) // 4
        or len(summary.get("successful_unit_ids", [])) != len(PILOT_OWNERS) // 4
        or len(summary.get("failed_attempts", [])) != len(PILOT_OWNERS) // 4
        or summary.get("incomplete_attempts") != []
        or set(summary.get("attempt_count_by_owner", {})) != set(row_by_id)
        or set(summary.get("carried_attempt_count_by_owner", {})) != {
            str(row["unit_id"]) for row in rows[:10]
        }
        or len(original_failure_ids) != 10
        or "" in original_failure_ids
        or summary.get("carried_attempt_count_by_owner") != {unit_id: 1 for unit_id in original_failure_ids}
    ):
        raise RuntimeError("Issue 85 C outcome predecessor summary has inconsistent owner or attempt counts")

    successful_ids = [str(value) for value in summary["successful_unit_ids"]]
    failed_rows = {str(item.get("unit_id", "")): item for item in summary["failed_attempts"]}
    first_attempted_owner_ids = {str(row["unit_id"]) for row in rows[:8]}
    successful_id_set = set(successful_ids)
    failed_id_set = set(failed_rows)
    expected_incomplete_ids = failed_id_set | {str(row["unit_id"]) for row in rows[8:]}
    if (
        len(set(successful_ids)) != len(successful_ids)
        or successful_id_set.intersection(failed_id_set)
        or successful_id_set.union(failed_id_set) != first_attempted_owner_ids
        or set(summary.get("incomplete", [])) != expected_incomplete_ids
        or len(failed_rows) != len(summary["failed_attempts"])
    ):
        raise RuntimeError("Issue 85 C outcome predecessor success/failure owner ledger is inconsistent")

    success_roots: dict[str, tuple[Path, str]] = {}
    success_receipts: list[dict[str, str]] = []
    failed_receipts: list[dict[str, str]] = []
    prior_local_counts: dict[str, int] = {}
    for row in rows:
        unit_id = str(row["unit_id"])
        count = summary["attempt_count_by_owner"][unit_id]
        expected_local_count = 1 if PILOT_OWNERS.index(int(row["owner_seed"])) < 8 else 0
        if isinstance(count, bool) or not isinstance(count, int) or count != expected_local_count:
            raise RuntimeError(f"Issue 85 C predecessor attempt count differs for {unit_id}")
        prior_local_counts[unit_id] = count
        unit_root = prior_root / "training" / "C" / "units" / str(backend.unit_key(unit_id))
        attempts = sorted(path.name for path in unit_root.glob("attempt-*") if path.is_dir())
        expected_attempts = ["attempt-0002"] if count else []
        if attempts != expected_attempts:
            raise RuntimeError(f"Issue 85 C predecessor attempt directories differ for {unit_id}")
        if unit_id in successful_ids:
            if count != 1 or unit_id in failed_rows:
                raise RuntimeError(f"Issue 85 completed C predecessor owner has no unique attempt: {unit_id}")
            record = pilot_success_record(
                backend, root=prior_root / "training" / "C", row=row,
                package_commit=predecessor_commit,
            )
            if record is None:
                raise RuntimeError(f"Issue 85 C predecessor success receipt failed validation: {unit_id}")
            result, result_path = record
            attempt = result_path.parent
            success_roots[unit_id] = (prior_root / "training" / "C", predecessor_commit)
            success_receipts.append({
                "unit_id": unit_id,
                "attempt": attempt.name,
                "worker_job_sha256": file_sha256(attempt / "worker-job.json"),
                "started_sha256": file_sha256(attempt / "started.json"),
                "finished_sha256": file_sha256(attempt / "finished.json"),
                "result_sha256": file_sha256(result_path),
            })
            continue
        if unit_id not in failed_rows:
            if count != 0:
                raise RuntimeError(f"Issue 85 C predecessor has an unclassified attempt for {unit_id}")
            continue
        if count != 1:
            raise RuntimeError(f"Issue 85 C predecessor has an unexplained incomplete owner: {unit_id}")
        attempt = unit_root / "attempt-0002"
        job_path = attempt / "worker-job.json"
        started_path = attempt / "started.json"
        finished_path = attempt / "finished.json"
        result_path = attempt / "result.json"
        if not all(path.is_file() for path in (job_path, started_path, finished_path, result_path)):
            raise RuntimeError(f"Issue 85 C predecessor failure chain is incomplete: {unit_id}")
        job, started = read_json(job_path), read_json(started_path)
        finished, result = read_json(finished_path), read_json(result_path)
        run_id = f"{unit_id}-attempt-0002"
        failure = failed_rows[unit_id]
        if (
            job.get("schema_id") != "issue79-owner-worker-job-v1"
            or job.get("unit_id") != unit_id
            or job.get("row") != row
            or job.get("run_id") != run_id
            or job.get("package_commit") != predecessor_commit
            or job.get("runner_code_commit") != predecessor_commit
            or job.get("source_lock_sha256") != identity.get("source_lock_sha256")
            or not _worker_job_paths_match(
                job, stage_root=prior_root / "training" / "C", attempt_dir=attempt,
                result_path=result_path,
            )
            or started.get("schema_id") != "issue79-owner-attempt-start-v1"
            or started.get("unit_id") != unit_id
            or started.get("run_id") != run_id
            or started.get("package_commit") != predecessor_commit
            or started.get("seed_row_sha256") != sha256(canonical(row))
            or finished.get("schema_id") != "issue79-owner-attempt-finish-v1"
            or finished.get("unit_id") != unit_id
            or finished.get("run_id") != run_id
            or finished.get("status") != "FAILED"
            or finished.get("result_sha256") != file_sha256(result_path)
            or result.get("schema_id") != "issue79-owner-attempt-result-v1"
            or result.get("unit_id") != unit_id
            or not _optional_receipt_field_matches(result, "package_commit", predecessor_commit)
            or not _optional_receipt_field_matches(result, "runner_code_commit", predecessor_commit)
            or not _optional_receipt_field_matches(
                result, "source_lock_sha256", str(identity.get("source_lock_sha256")),
            )
            or result.get("status") != "FAILED"
            or result.get("terminal_outcome") is not None
            or result.get("failure_detail") != failure.get("failure")
            or failure.get("attempt") != run_id
            or failure.get("failure_type") != "StudyFailure"
            or failure.get("exit_code") != 1
        ):
            raise RuntimeError(f"Issue 85 C predecessor failed receipt binding differs: {unit_id}")
        failed_receipts.append({
            "unit_id": unit_id,
            "attempt": attempt.name,
            "worker_job_sha256": file_sha256(job_path),
            "started_sha256": file_sha256(started_path),
            "finished_sha256": file_sha256(finished_path),
            "result_sha256": file_sha256(result_path),
            "failure_detail": str(failure.get("failure", "")),
        })

    prior_c_attempt_counts = {
        unit_id: prior_local_counts[unit_id] + (1 if unit_id in original_failure_ids else 0)
        for unit_id in row_by_id
    }
    document = {
        "schema_id": "issue85-pilot-C-outcome-reuse-v1",
        "scope": "PILOT_ONLY / NONFORMAL",
        "current_package_commit": identity["package_commit"],
        "predecessor_package_commit": predecessor_commit,
        "source_lock_sha256": identity["source_lock_sha256"],
        "issue84_source_commit": ISSUE84_ACCEPTED_COMMIT,
        "source_diff_paths": changed_paths,
        "progress_sha256": file_sha256(progress_path),
        "seed_table_sha256": file_sha256(seed_path),
        "prior_actor_update_reuse_sha256": file_sha256(actor_reuse_path),
        "prior_C_summary_sha256": sha256(canonical(summary)),
        "prior_C_successful_unit_ids": sorted(successful_ids),
        "prior_C_successful_games": len(successful_ids),
        "prior_C_failed_attempt_receipts": sorted(failed_receipts, key=lambda item: item["unit_id"]),
        "prior_C_failed_attempt_count": len(failed_receipts),
        "prior_C_attempt_count_by_owner": prior_local_counts,
        "prior_C_attempt_receipts": sorted(success_receipts + failed_receipts, key=lambda item: item["unit_id"]),
        "prior_C_cumulative_attempt_count_by_owner": prior_c_attempt_counts,
        "prior_inherited_failed_attempt_count": len(original_failure_ids),
        "prior_C_inherited_failed_attempt_receipts": actor_reuse["prior_C_failed_attempts"],
        "reuse_rule": "reference the exact four validated C success chains; preserve all eight later package attempts and the ten earlier failures; resume only incomplete owners with cumulative attempt numbers",
    }
    return {
        "source_commit": predecessor_commit,
        "root": prior_root,
        "document": document,
        "summary": summary,
        "success_receipt_roots_by_owner": success_roots,
    }


def _validate_completed_package_reuse_source_diff(changed_paths: Sequence[str]) -> None:
    expected_paths = sorted((
        "studies/issue-85-representation/pilot_runner.py",
        "studies/issue-85-representation/tests/test_pilot_runner.py",
    ))
    observed_paths = sorted(filter(None, (str(path) for path in changed_paths)))
    if observed_paths != expected_paths:
        raise RuntimeError(
            "completed Issue 85 training reuse only allows the reviewed fold-tolerance runner repair and its test"
        )


def _load_completed_package_training_reuse(
    backend: Any,
    *,
    identity: Mapping[str, str],
    current_seed_table: Mapping[str, Any],
    c_outcome_reuse: Mapping[str, Any],
) -> dict[str, Any]:
    """Reference C/D outcomes from the failed 643 package without copying or replaying them."""
    predecessor_commit = PILOT_COMPLETED_TRAINING_CARRY_FORWARD_SHA
    if identity.get("package_commit") == predecessor_commit:
        raise RuntimeError("completed training package must differ from the current runner source")
    is_ancestor = subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", predecessor_commit, identity["package_commit"]],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if is_ancestor.returncode != 0:
        raise RuntimeError("completed training package is not an ancestor of the current runner source")
    changed_paths = sorted(filter(None, _git(REPO, "diff", "--name-only", predecessor_commit, identity["package_commit"]).splitlines()))
    _validate_completed_package_reuse_source_diff(changed_paths)

    prior_root = PILOT_ROOT / predecessor_commit
    progress_path = prior_root / "pilot-progress.json"
    seed_path = prior_root / "pilot-seed-table.json"
    if not progress_path.is_file() or not seed_path.is_file() or (prior_root / "pilot-report.json").exists():
        raise RuntimeError("completed training package lacks its failed progress/seed evidence or has a final report")
    progress = read_json(progress_path)
    prior_seed_table = read_json(seed_path)
    if (
        progress.get("schema_id") != "issue85-pilot-progress-v1"
        or progress.get("status") != "FAILED_OR_INCOMPLETE"
        or progress.get("package_commit") != predecessor_commit
        or progress.get("source_lock_sha256") != identity.get("source_lock_sha256")
        or progress.get("issue84_source_commit") != ISSUE84_ACCEPTED_COMMIT
        or progress.get("active_stage") != "pilot-actor-update"
        or progress.get("active_arm") != "D"
        or progress.get("failure") != {
            "type": "RuntimeError",
            "detail": "pilot widened matrix for D fails exact fold identity",
        }
        or set(progress.get("training_runs", {})) != {"B", "C", "D"}
        or progress.get("evaluation_runs") != {}
        or set(progress.get("actor_updates", {})) != {"B", "C"}
        or file_sha256(seed_path) != progress.get("seed_table_sha256")
        or prior_seed_table != current_seed_table
        or prior_seed_table.get("rows_sha256") != current_seed_table.get("rows_sha256")
    ):
        raise RuntimeError("completed training package is not the exact failed D-update attempt with the unchanged seed table")

    roots_by_arm: dict[str, dict[str, tuple[Path, str]]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    summary_hashes: dict[str, str] = {}
    receipt_records: list[dict[str, str]] = []
    prior_c_success_roots = dict(c_outcome_reuse.get("success_receipt_roots_by_owner", {}))
    for arm in ("C", "D"):
        rows = [
            dict(row) for row in current_seed_table["rows"]
            if row.get("phase") == "training" and row.get("issue85_arm") == arm
        ]
        expected_ids = sorted(str(row["unit_id"]) for row in rows)
        summary = progress.get("training_runs", {}).get(arm)
        if (
            len(rows) != len(PILOT_OWNERS)
            or len(set(expected_ids)) != len(rows)
            or not stage_summary_is_complete(summary, len(rows), expected_ids)
            or summary.get("failed_attempts") != []
            or summary.get("incomplete_attempts") != []
            or summary.get("incomplete") != []
            or set(summary.get("attempt_count_by_owner", {})) != set(expected_ids)
        ):
            raise RuntimeError(f"completed package {arm} training summary is not a complete fixed-owner cohort")
        if arm == "C" and summary.get("carried_attempt_count_by_owner") != c_outcome_reuse.get(
            "document", {},
        ).get("prior_C_cumulative_attempt_count_by_owner"):
            raise RuntimeError("completed package C summary changed the immutable prior-attempt accounting")
        if arm == "D" and (
            summary.get("carried_attempt_count_by_owner") != {}
            or any(int(count) != 1 for count in summary.get("attempt_count_by_owner", {}).values())
        ):
            raise RuntimeError("completed package D cohort contains an unaccounted attempt history")

        arm_roots: dict[str, tuple[Path, str]] = {}
        for row in rows:
            unit_id = str(row["unit_id"])
            local_count = summary["attempt_count_by_owner"][unit_id]
            binding = prior_c_success_roots.get(unit_id) if arm == "C" else None
            carried_count = summary.get("carried_attempt_count_by_owner", {}).get(unit_id, 0)
            if isinstance(carried_count, bool) or not isinstance(carried_count, int) or carried_count < 0:
                raise RuntimeError(f"completed package {arm} owner has an invalid carried attempt count: {unit_id}")
            if binding is None:
                if isinstance(local_count, bool) or not isinstance(local_count, int) or local_count != 1:
                    raise RuntimeError(f"completed package {arm} owner lacks exactly one local terminal attempt: {unit_id}")
                root = prior_root / "training" / arm
                package_commit = predecessor_commit
                unit_root = root / "units" / str(backend.unit_key(unit_id))
                attempt_names = sorted(path.name for path in unit_root.glob("attempt-*") if path.is_dir())
                expected_attempt_name = f"attempt-{carried_count + local_count:04d}"
                if attempt_names != [expected_attempt_name]:
                    raise RuntimeError(f"completed package {arm} owner has missing or unregistered attempt directories: {unit_id}")
            else:
                root, package_commit = Path(binding[0]), str(binding[1])
                if isinstance(local_count, bool) or not isinstance(local_count, int) or local_count != 0:
                    raise RuntimeError(f"completed package C owner has both inherited and local attempts: {unit_id}")
                local_unit_root = prior_root / "training" / "C" / "units" / str(backend.unit_key(unit_id))
                if any(path.is_dir() for path in local_unit_root.glob("attempt-*")):
                    raise RuntimeError(f"completed package C inherited owner has unaccounted local attempt directories: {unit_id}")
            record = pilot_success_record(backend, root=root, row=row, package_commit=package_commit)
            if record is None:
                raise RuntimeError(f"completed package {arm} terminal receipt failed strict validation: {unit_id}")
            _result, result_path = record
            attempt = result_path.parent
            arm_roots[unit_id] = (root, package_commit)
            receipt_records.append({
                "arm": arm,
                "unit_id": unit_id,
                "evidence_root": str(root.resolve()),
                "package_commit": package_commit,
                "attempt": attempt.name,
                "worker_job_sha256": file_sha256(attempt / "worker-job.json"),
                "started_sha256": file_sha256(attempt / "started.json"),
                "finished_sha256": file_sha256(attempt / "finished.json"),
                "result_sha256": file_sha256(result_path),
            })
        if set(arm_roots) != set(expected_ids):
            raise RuntimeError(f"completed package {arm} receipt roots do not cover every fixed owner")
        roots_by_arm[arm] = arm_roots
        summaries[arm] = dict(summary)
        summary_hashes[arm] = sha256(canonical(summary))

    actor_receipt = progress["actor_updates"]["C"]
    actor_attempt = prior_root / "training" / "C" / "pilot-updates" / "C" / "attempt-0001"
    actor_receipt_path = actor_attempt / "receipt.json"
    actor_progress_path = actor_attempt / "progress.json"
    actor_progress = read_json(actor_progress_path) if actor_progress_path.is_file() else {}
    checkpoint = (prior_root / str(actor_receipt.get("checkpoint_path", ""))).resolve()
    optimizer = (prior_root / str(actor_receipt.get("optimizer_path", ""))).resolve()
    if (
        not actor_receipt_path.is_file()
        or not actor_progress_path.is_file()
        or read_json(actor_receipt_path) != actor_receipt
        or actor_receipt.get("schema_id") != "issue85-pilot-actor-update-v1"
        or actor_receipt.get("status") != "SUCCEEDED"
        or actor_receipt.get("arm") != "C"
        or actor_receipt.get("package_commit") != predecessor_commit
        or actor_receipt.get("source_lock_sha256") != identity.get("source_lock_sha256")
        or actor_receipt.get("owner_seed_sha256") != sha256(",".join(str(owner) for owner in PILOT_OWNERS).encode("ascii"))
        or actor_receipt.get("owner_count") != len(PILOT_OWNERS)
        or actor_receipt.get("minibatch_owner_count") != len(PILOT_OWNERS)
        or actor_receipt.get("completed_steps") != ACTOR_STEPS
        or actor_receipt.get("loss_summary", {}).get("finite_all_steps") is not True
        or actor_progress.get("schema_id") != "issue85-pilot-actor-update-progress-v1"
        or actor_progress.get("status") != "SUCCEEDED"
        or actor_progress.get("arm") != "C"
        or actor_progress.get("package_commit") != predecessor_commit
        or actor_progress.get("source_lock_sha256") != identity.get("source_lock_sha256")
        or actor_progress.get("completed_steps") != ACTOR_STEPS
        or actor_progress.get("model_digest") != actor_receipt.get("model_digest")
        or actor_progress.get("checkpoint_sha256") != actor_receipt.get("checkpoint_sha256")
        or actor_progress.get("optimizer_sha256") != actor_receipt.get("optimizer_sha256")
        or actor_attempt.resolve() not in checkpoint.parents
        or actor_attempt.resolve() not in optimizer.parents
        or not checkpoint.is_file()
        or not optimizer.is_file()
        or file_sha256(checkpoint) != actor_receipt.get("checkpoint_sha256")
        or file_sha256(optimizer) != actor_receipt.get("optimizer_sha256")
    ):
        raise RuntimeError("completed package C actor update receipt/progress/checkpoint chain failed strict validation")
    actor_reuse_document = {
        "schema_id": "issue85-pilot-actor-update-reuse-v1",
        "scope": "PILOT_ONLY / NONFORMAL",
        "current_package_commit": identity["package_commit"],
        "predecessor_package_commit": predecessor_commit,
        "source_lock_sha256": identity["source_lock_sha256"],
        "issue84_source_commit": ISSUE84_ACCEPTED_COMMIT,
        "source_diff_paths": changed_paths,
        "predecessor_progress_sha256": file_sha256(progress_path),
        "predecessor_seed_table_sha256": file_sha256(seed_path),
        "actor_update_attempt": actor_attempt.name,
        "actor_update_progress_sha256": file_sha256(actor_progress_path),
        "actor_update_receipt_sha256": file_sha256(actor_receipt_path),
        "actor_update_receipt": actor_receipt,
        "checkpoint_sha256": actor_receipt["checkpoint_sha256"],
        "optimizer_sha256": actor_receipt["optimizer_sha256"],
        "reuse_rule": "reference the completed C actor update and immutable checkpoint; do not repeat or relabel it",
    }
    completed_package_receipts = [
        record for record in receipt_records if record["package_commit"] == predecessor_commit
    ]
    completed_package_c_receipts = [
        record for record in completed_package_receipts if record["arm"] == "C"
    ]
    completed_package_d_receipts = [
        record for record in completed_package_receipts if record["arm"] == "D"
    ]
    if (
        len(receipt_records) != 32
        or len(completed_package_receipts) != 28
        or len(completed_package_c_receipts) != 12
        or len(completed_package_d_receipts) != 16
    ):
        raise RuntimeError("completed package must contribute exactly 12 new C and 16 D terminal receipts")
    document = {
        "schema_id": "issue85-pilot-completed-training-reuse-v1",
        "scope": "PILOT_ONLY / NONFORMAL",
        "current_package_commit": identity["package_commit"],
        "predecessor_package_commit": predecessor_commit,
        "source_lock_sha256": identity["source_lock_sha256"],
        "issue84_source_commit": ISSUE84_ACCEPTED_COMMIT,
        "source_diff_paths": changed_paths,
        "progress_sha256": file_sha256(progress_path),
        "seed_table_sha256": file_sha256(seed_path),
        "rows_sha256": current_seed_table["rows_sha256"],
        "training_arms": ["C", "D"],
        "training_summaries": summaries,
        "training_summary_sha256": summary_hashes,
        "validated_unit_receipts": sorted(receipt_records, key=lambda item: (item["arm"], item["unit_id"])),
        "validated_training_cohort_receipt_count": len(receipt_records),
        "successful_game_count": len(completed_package_receipts),
        "C_successful_game_count": len(completed_package_c_receipts),
        "D_successful_game_count": len(completed_package_d_receipts),
        "actor_update_reuse": actor_reuse_document,
        "actor_update_reuse_sha256": sha256(canonical(actor_reuse_document)),
        "reuse_rule": "reference 12 current-package C terminals, 16 current-package D terminals, and the four older inherited C terminals in their original source packages; do not copy, relabel, or replay them",
    }
    return {
        "source_commit": predecessor_commit,
        "root": prior_root,
        "progress": progress,
        "progress_sha256": file_sha256(progress_path),
        "seed_table_sha256": file_sha256(seed_path),
        "document": document,
        "actor_update_reuse_document": actor_reuse_document,
        "actor_update_receipt": actor_receipt,
        "receipt_roots_by_arm": roots_by_arm,
        "summaries": summaries,
    }


def actor_update_resume_wall(progress: Mapping[str, Any], current_clock_elapsed: float | None) -> tuple[float, float]:
    elapsed = float(progress.get("elapsed_seconds", 0.0))
    checkpoint_clock_elapsed = progress.get("active_clock_elapsed_seconds")
    if checkpoint_clock_elapsed is None or current_clock_elapsed is None:
        return elapsed, 0.0
    checkpoint_clock_elapsed = float(checkpoint_clock_elapsed)
    current_clock_elapsed = float(current_clock_elapsed)
    if any(not math.isfinite(value) or value < 0 for value in (elapsed, checkpoint_clock_elapsed, current_clock_elapsed)):
        raise ValueError("durable pilot actor clock contains an invalid duration")
    recovered_tail = max(0.0, current_clock_elapsed - checkpoint_clock_elapsed)
    return elapsed + recovered_tail, recovered_tail


def run_units_bounded(
    backend: Any,
    rows: list[dict[str, Any]],
    *,
    root: Path,
    clock: Any,
    package_commit: str,
    progress_callback: Any | None = None,
    initial_attempt_count_by_owner: Mapping[str, int] | None = None,
    prior_success_receipt_roots_by_owner: Mapping[str, tuple[Path, str]] | None = None,
) -> dict[str, Any]:
    """Run the fixed owner rows without an implementation-defined retry cap.

    Successful immutable receipts are reused and failed receipts remain in
    place. A failure pauses new scheduling for this invocation so the cause
    can be diagnosed; a later explicit resume can continue the same owner
    after repair, with cumulative attempt numbers preserved.
    """
    started = time.monotonic()
    pending = list(rows)
    row_ids = [str(row["unit_id"]) for row in rows]
    if len(set(row_ids)) != len(row_ids):
        raise ValueError("pilot stage rows contain duplicate unit IDs")
    carried_attempt_counts = {
        str(unit_id): count
        for unit_id, count in (initial_attempt_count_by_owner or {}).items()
    }
    if not set(carried_attempt_counts).issubset(set(row_ids)) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in carried_attempt_counts.values()
    ):
        raise ValueError("carried pilot attempt counts must be nonnegative integers for current owner rows")
    carried_successes = {
        str(unit_id): (Path(binding[0]), str(binding[1]))
        for unit_id, binding in (prior_success_receipt_roots_by_owner or {}).items()
    }
    if not set(carried_successes).issubset(set(row_ids)):
        raise ValueError("carried pilot success receipts must bind current owner rows")
    active: dict[str, tuple[Any, Any, dict[str, Any], float]] = {}
    complete: dict[str, dict[str, Any]] = {}
    failed_attempts: list[dict[str, Any]] = []
    incomplete_attempts: list[dict[str, Any]] = []
    prior_events: list[tuple[str, float, str]] = []

    def event_time(finished: Mapping[str, Any] | None, result: Mapping[str, Any] | None, started_receipt: Mapping[str, Any] | None) -> tuple[str, float]:
        monotonic = 0.0
        for source in (finished, result, started_receipt):
            if source is not None and source.get("finished_monotonic") is not None:
                monotonic = float(source["finished_monotonic"])
                break
        else:
            if started_receipt is not None and started_receipt.get("started_monotonic") is not None:
                monotonic = float(started_receipt["started_monotonic"])
        utc = ""
        for source in (finished, result, started_receipt):
            if source is not None:
                value = source.get("finished_utc", source.get("started_utc"))
                if value is not None:
                    utc = str(value)
                    break
        return utc, monotonic

    for row in rows:
        unit_id = str(row["unit_id"])
        base = Path(root) / "units" / backend.unit_key(unit_id)
        for prior_attempt in sorted(base.glob("attempt-*") if base.exists() else ()):
            if not prior_attempt.is_dir():
                continue
            result_path = prior_attempt / "result.json"
            finished_path = prior_attempt / "finished.json"
            started_path = prior_attempt / "started.json"
            job_path = prior_attempt / "worker-job.json"
            result = read_json(result_path) if result_path.is_file() else None
            finished = read_json(finished_path) if finished_path.is_file() else None
            started_receipt = read_json(started_path) if started_path.is_file() else None
            job = read_json(job_path) if job_path.is_file() else None
            if job is None and any(value is not None for value in (result, finished, started_receipt)):
                raise RuntimeError(f"prior pilot attempt has receipts but no worker-job binding: {prior_attempt}")
            if job is not None and (
                job.get("package_commit") != package_commit
                or job.get("source_lock_sha256") != backend.source_lock_sha()
                or job.get("unit_id") != unit_id
                or job.get("row") != dict(row)
                or job.get("pilot") is not True
            ):
                raise RuntimeError(f"prior pilot worker job has a different source/owner binding: {job_path}")
            if started_receipt is not None and (
                started_receipt.get("unit_id") != unit_id
                or started_receipt.get("owner_seed") != row.get("owner_seed")
                or started_receipt.get("run_id") != job.get("run_id")
            ):
                raise RuntimeError(f"prior pilot start receipt has a different owner binding: {started_path}")
            if finished is not None and (
                finished.get("unit_id") != unit_id
                or finished.get("run_id") != job.get("run_id")
            ):
                raise RuntimeError(f"prior pilot finish receipt has a different owner binding: {finished_path}")
            if result is not None and (
                result.get("unit_id") != unit_id
                or result.get("owner_seed") != row.get("owner_seed")
                or result.get("arm") != row.get("arm")
            ):
                raise RuntimeError(f"prior pilot result receipt has a different owner binding: {result_path}")
            if finished is not None and result is not None and finished.get("result_sha256") != file_sha256(result_path):
                raise RuntimeError(f"prior pilot result hash differs from its finish receipt: {result_path}")
            if finished is not None and result is not None and finished.get("status") != result.get("status"):
                raise RuntimeError(f"prior pilot result and finish states disagree: {result_path}")
            status = finished.get("status") if finished is not None else (result.get("status") if result is not None else None)
            if status == "SUCCEEDED":
                if result is None or result.get("status") != "SUCCEEDED":
                    raise RuntimeError(f"prior pilot attempt claims success without its successful result receipt: {prior_attempt}")
                if result.get("package_commit") != package_commit or result.get("source_lock_sha256") != backend.source_lock_sha():
                    raise RuntimeError(f"prior pilot success receipt has a different source binding: {result_path}")
                utc, monotonic = event_time(finished, result, started_receipt)
                prior_events.append((utc, monotonic, "SUCCEEDED"))
            elif status == "FAILED":
                failure = {
                    "unit_id": unit_id,
                    "attempt": prior_attempt.name,
                    "failure_type": (result or {}).get("failure_boundary"),
                    "failure": str((result or {}).get("failure_detail", "failed attempt receipt"))[:1000],
                    "prior_attempt": True,
                }
                failed_attempts.append(failure)
                utc, monotonic = event_time(finished, result, started_receipt)
                prior_events.append((utc, monotonic, "FAILED"))
            else:
                incomplete_attempts.append({
                    "unit_id": unit_id,
                    "attempt": prior_attempt.name,
                    "state": "INCOMPLETE_OR_UNVERIFIED",
                    "started_pid": None if started_receipt is None else started_receipt.get("pid"),
                    "result_present": result is not None,
                    "finish_present": finished is not None,
                })
                if result is not None or finished is not None:
                    raise RuntimeError(f"prior pilot attempt has an unrecognized/inconsistent terminal receipt: {prior_attempt}")
    row_by_id = {str(row["unit_id"]): row for row in rows}
    for unit_id, (receipt_root, receipt_commit) in carried_successes.items():
        local_attempts = list((root / "units" / backend.unit_key(unit_id)).glob("attempt-*"))
        if local_attempts:
            raise RuntimeError(f"carried-success owner already has current-source attempts: {unit_id}")
        record = pilot_success_record(
            backend, root=receipt_root, row=row_by_id[unit_id], package_commit=receipt_commit,
        )
        if record is None:
            raise RuntimeError(f"carried-success receipt chain failed validation: {unit_id}")
        complete[unit_id] = record[0]
        finished = read_json(record[1].parent / "finished.json")
        utc, monotonic = event_time(finished, record[0], read_json(record[1].parent / "started.json"))
        prior_events.append((utc, monotonic, "SUCCEEDED"))
    consecutive_failures = trailing_failure_streak(prior_events)
    stop_reason: str | None = None
    last_heartbeat = time.monotonic()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in ("STS_LIGHTSPEED_SOURCE_DIR", "STSRL_SOURCE_DIR", "ISSUE84_SOURCE_DIR", "ISSUE85_PILOT_ROOT"):
        if not env.get(name):
            raise RuntimeError(f"{name} must be source-locked and present before pilot execution")

    def snapshot() -> None:
        if progress_callback is None:
            return
        progress_callback({
            "expected": len(rows),
            "successful_completions": len(complete),
            "successful_unit_ids": sorted(complete),
            "active_unit_ids": sorted(value[2]["unit_id"] for value in active.values()),
            "pending_unit_ids": [str(row["unit_id"]) for row in pending],
            "attempt_counts": {
                str(row["unit_id"]): len(list((root / "units" / backend.unit_key(row["unit_id"])).glob("attempt-*")))
                for row in rows
            },
            "carried_attempt_counts": dict(carried_attempt_counts),
            "failed_attempt_count": len(failed_attempts),
            "recent_failures": failed_attempts[-5:],
            "incomplete_attempt_count": len(incomplete_attempts),
            "incomplete_attempts": incomplete_attempts[-10:],
            "consecutive_failed_attempts": consecutive_failures,
            "stop_reason": stop_reason,
            "updated_utc": datetime.now(timezone.utc).isoformat(),
        })

    while pending or active:
        if stop_reason is not None and not active:
            break
        if clock is not None and clock.expired():
            stop_reason = "GLOBAL_ACTIVE_WALL_LIMIT"
            for process, _log, job, _began in active.values():
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            for process, log, job, _began in active.values():
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                log.close()
                backend.write_json_once(Path(job["attempt_dir"]) / "global-wall-stop.json", {
                    "schema_id": "issue85-pilot-global-wall-stop-v1",
                    "unit_id": job["unit_id"], "run_id": job["run_id"],
                    "cumulative_active_seconds": clock.elapsed(),
                })
            active.clear()
            if clock is not None:
                clock.set_processes(set())
            break

        while pending and len(active) < backend.CONCURRENCY and stop_reason is None:
            row = pending.pop(0)
            base = root / "units" / backend.unit_key(row["unit_id"])
            base.mkdir(parents=True, exist_ok=True)
            prior_attempts = sorted(base.glob("attempt-*"))
            for prior_attempt in prior_attempts:
                started_path = prior_attempt / "started.json"
                if not started_path.is_file() or (prior_attempt / "finished.json").is_file():
                    continue
                started_receipt = read_json(started_path)
                prior_pid = int(started_receipt.get("pid", 0))
                if prior_pid <= 0:
                    raise RuntimeError(f"unfinished prior attempt has no valid worker PID: {prior_attempt}")
                try:
                    os.kill(prior_pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError as exc:
                    raise RuntimeError(f"cannot verify unfinished prior worker PID {prior_pid}; refusing a duplicate owner run") from exc
                else:
                    raise RuntimeError(f"unfinished prior worker PID {prior_pid} is alive; refusing a duplicate owner run")
            if str(row["unit_id"]) in carried_successes:
                continue
            exact_successes = []
            for prior_attempt in prior_attempts:
                result_path = prior_attempt / "result.json"
                if not result_path.is_file():
                    continue
                result_receipt = read_json(result_path)
                if result_receipt.get("status") == "SUCCEEDED":
                    if result_receipt.get("package_commit") != package_commit or result_receipt.get("source_lock_sha256") != backend.source_lock_sha():
                        raise RuntimeError(f"prior pilot success has a different source binding: {result_path}")
                    exact_successes.append(result_path)
            if len(exact_successes) > 1:
                raise RuntimeError(f"multiple success receipts exist for pilot owner {row['unit_id']}; refusing to choose one")
            prior_record = pilot_success_record(
                backend, root=root, row=row, package_commit=package_commit,
            )
            if exact_successes and prior_record is None:
                raise RuntimeError(f"prior pilot success receipt failed validation: {exact_successes[0]}")
            if prior_record is not None:
                complete[row["unit_id"]] = prior_record[0]
                continue
            attempt_number = _next_attempt_number(
                carried_attempt_counts.get(str(row["unit_id"]), 0), len(prior_attempts),
            )
            attempt = base / f"attempt-{attempt_number:04d}"
            attempt.mkdir(parents=True, exist_ok=False)
            run_id = f"{row['unit_id']}-attempt-{attempt_number:04d}"
            job = {
                "schema_id": "issue79-owner-worker-job-v1",
                "artifact_root": str(root),
                "attempt_dir": str(attempt),
                "result_path": str(attempt / "result.json"),
                "output_dir": str(attempt / "native"),
                "run_id": run_id,
                "unit_id": row["unit_id"],
                "phase": row["phase"],
                "row": row,
                "pilot": True,
                "package_commit": package_commit,
                "runner_code_commit": package_commit,
                "source_lock_sha256": backend.source_lock_sha(),
            }
            job_path = attempt / "worker-job.json"
            backend.write_json_once(job_path, job)
            log = (attempt / "worker.log").open("ab")
            process = subprocess.Popen(
                [str(backend.PINNED_PYTHON), str(Path(__file__).resolve()), "--worker", str(job_path)],
                cwd=backend.REPO, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            active[run_id] = (process, log, job, time.monotonic())
            if clock is not None:
                clock.set_processes({item[0].pid for item in active.values()})
            snapshot()

        exit_records: list[tuple[float, str, Any, Any, dict[str, Any], float, int]] = []
        for run_id, (process, log, job, began) in list(active.items()):
            exit_code = process.poll()
            if exit_code is None:
                continue
            finished_path = Path(job["attempt_dir"]) / "finished.json"
            finished = read_json(finished_path) if finished_path.is_file() else None
            finished_monotonic = float(finished.get("finished_monotonic", time.monotonic())) if finished is not None else time.monotonic()
            exit_records.append((finished_monotonic, run_id, process, log, job, began, exit_code))
        observed_exit = bool(exit_records)
        for _finished_monotonic, run_id, process, log, job, began, exit_code in sorted(exit_records, key=lambda item: (item[0], item[1])):
            log.close()
            row = job["row"]
            result_path = Path(job["result_path"])
            result = read_json(result_path) if result_path.is_file() else None
            del active[run_id]
            if exit_code == 0 and result and result.get("status") == "SUCCEEDED":
                verified = pilot_success_record(
                    backend, root=root, row=row, package_commit=package_commit,
                )
                if verified is None:
                    failed_attempts.append({"unit_id": row["unit_id"], "attempt": job["run_id"], "failure": "success_receipt_failed_validation"})
                    consecutive_failures += 1
                    stop_reason = "INVALID_SUCCESS_RECEIPT"
                else:
                    complete[row["unit_id"]] = verified[0]
                    consecutive_failures = 0
            else:
                failed_attempts.append({
                    "unit_id": row["unit_id"],
                    "attempt": job["run_id"],
                    "exit_code": exit_code,
                    "failure_type": None if result is None else result.get("failure_boundary"),
                    "failure": "worker_result_missing" if result is None else str(result.get("failure_detail", result.get("status")))[:1000],
                })
                consecutive_failures += 1
                base = root / "units" / backend.unit_key(row["unit_id"])
                attempt_number = _effective_attempt_count(
                    carried_attempt_counts.get(str(row["unit_id"]), 0),
                    len(list(base.glob("attempt-*"))),
                )
                stop_reason = "PAUSED_AFTER_FAILED_ATTEMPT"
            if clock is not None:
                clock.set_processes({item[0].pid for item in active.values()})
            snapshot()
        if not observed_exit:
            time.sleep(0.25)
            now = time.monotonic()
            if now - last_heartbeat >= 5.0:
                snapshot()
                last_heartbeat = now

    row_wall: dict[str, float] = {}
    tries: dict[str, int] = {}
    native_wall: dict[str, float] = {}
    for row in rows:
        base = root / "units" / backend.unit_key(row["unit_id"])
        durations: list[float] = []
        for finish_path in base.glob("attempt-*/finished.json") if base.exists() else ():
            finish = read_json(finish_path)
            start_path = finish_path.parent / "started.json"
            if start_path.is_file() and finish.get("finished_monotonic") is not None:
                began = float(read_json(start_path)["started_monotonic"])
                durations.append(max(0.0, float(finish["finished_monotonic"]) - began))
        row_wall[row["unit_id"]] = sum(durations)
        tries[row["unit_id"]] = len(list(base.glob("attempt-*"))) if base.exists() else 0
        report = complete.get(row["unit_id"])
        if report is not None:
            native_wall[row["unit_id"]] = float(report.get("resource", {}).get("worker_wall_seconds", 0.0))
    completed = len(complete)
    remaining = sorted(str(row["unit_id"]) for row in rows if row["unit_id"] not in complete)
    status = "SUCCEEDED" if completed == len(rows) else (stop_reason or "INCOMPLETE")
    return {
        "status": status,
        "elapsed_seconds": time.monotonic() - started,
        "completed": completed,
        "remaining": remaining,
        "results": complete,
        "failed_attempts": failed_attempts,
        "incomplete_attempts": incomplete_attempts,
        "row_wall_seconds": row_wall,
        "native_wall_seconds_by_unit": native_wall,
        "tries": tries,
        "carried_attempt_count_by_owner": dict(carried_attempt_counts),
    }


def _load_issue84_driver(root: Path) -> Any:
    driver_path = root / "studies" / "issue-84-six-arm" / "driver.py"
    spec = importlib.util.spec_from_file_location("issue85_exact_issue84_driver", driver_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load accepted Issue 84 driver from {driver_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_checkpoint(model_module: Any, checkpoint: Path, expected_sha: str, expected_digest: str) -> Any:
    import torch

    if file_sha256(checkpoint) != expected_sha:
        raise RuntimeError(f"locked checkpoint bytes differ: {checkpoint}")
    runtime = LOCK["model_runtime"]
    actor = model_module.ActionConditionedPolicy(
        dimension=int(runtime["architecture_dimension"]),
        seed=int(runtime["initial_model_seed"]),
        final_weight_std=float(runtime["final_weight_std"]),
    ).eval()
    actor.load_state_dict(torch.load(checkpoint, map_location="cpu", weights_only=True), strict=True)
    digest = model_module.model_digest(actor)
    if digest != expected_digest:
        raise RuntimeError(f"locked checkpoint model digest differs: expected {expected_digest}, got {digest}")
    return actor


def _install_issue85_pilot_parent_evaluation_compat(executor, issue84_driver) -> None:
    """Dispatch pilot PARENT through #73's supported BRANCH arm without relabeling it.

    PARENT is the frozen pi12 policy, not a #73 study arm. Keep the original
    row in the pilot context for exact pi12 model loading and change only the
    transient executor dispatch label required by the pinned #73 controller.
    """
    if getattr(executor, "_issue85_pilot_parent_evaluation_compat_installed", False):
        return
    original_worker_owner = executor._worker_owner
    original_arm_context = getattr(issue84_driver, "_ISSUE84_HELDOUT_ORIGINAL_ARM", None)
    original_group_context = getattr(issue84_driver, "_ISSUE84_HELDOUT_ORIGINAL_GROUP", None)
    if not all(
        hasattr(context, method)
        for context in (original_arm_context, original_group_context)
        for method in ("set", "reset")
    ):
        raise RuntimeError("pinned Issue 84 driver lacks its reviewed held-out dispatch context")

    def worker_owner_with_parent_pilot_compat(job, ledger):
        if not isinstance(job, Mapping):
            return original_worker_owner(job, ledger)
        row = job.get("row")
        if (
            job.get("pilot") is not True
            or job.get("phase") != "evaluation"
            or not isinstance(row, Mapping)
            or row.get("phase") != "evaluation"
            or row.get("arm") != "PARENT"
        ):
            return original_worker_owner(job, ledger)
        if (
            row.get("issue84_arm") is not None
            or row.get("issue84_policy_label") != "pi12"
            or row.get("issue85_arm") is not None
        ):
            raise RuntimeError("Issue 85 pilot PARENT evaluation row is not bound to the frozen pi12 policy")

        dispatch_job = dict(job)
        dispatch_row = dict(row)
        dispatch_row["arm"] = "BRANCH"
        dispatch_job["row"] = dispatch_row
        arm_token = original_arm_context.set(None)
        group_token = original_group_context.set("PARENT")
        row_token = _CURRENT_ROW.set(row)
        try:
            result = original_worker_owner(dispatch_job, ledger)
        finally:
            _CURRENT_ROW.reset(row_token)
            original_group_context.reset(group_token)
            original_arm_context.reset(arm_token)

        if (
            not isinstance(result, dict)
            or result.get("status") != "SUCCEEDED"
            or result.get("unit_id") != row.get("unit_id")
            or result.get("arm") != "BRANCH"
        ):
            raise RuntimeError("Issue 85 pilot PARENT compatibility changed the dispatched unit identity")
        result = dict(result)
        result["arm"] = "PARENT"
        return result

    executor._worker_owner = worker_owner_with_parent_pilot_compat
    executor._issue85_pilot_parent_evaluation_compat_installed = True


def _configure_runtime(issue84_root: Path, pilot_root: Path, identity: Mapping[str, str]) -> tuple[Any, Any]:
    issue84_driver = _load_issue84_driver(issue84_root)
    issue84_identity = issue84_driver.verify_source_lock(require_clean_worktree=True)
    expected_issue84_lock = LOCK["baseline_A"]["issue84_final_accepted_source_lock_sha256"]
    if issue84_identity.get("sha256") != expected_issue84_lock:
        raise RuntimeError(
            "accepted Issue 84 source-lock bytes differ from the Issue 85 contract: "
            f"expected {expected_issue84_lock}, got {issue84_identity.get('sha256')}"
        )
    os.environ["ISSUE84_PACKAGE_COMMIT"] = ISSUE84_ACCEPTED_COMMIT
    os.environ["ISSUE85_PACKAGE_COMMIT"] = identity["package_commit"]
    os.environ["ISSUE85_SOURCE_LOCK_SHA256"] = identity["source_lock_sha256"]
    os.environ["ISSUE85_PILOT_ROOT"] = str(pilot_root)
    os.environ["ISSUE84_SOURCE_DIR"] = str(issue84_root)
    os.environ["STS_LIGHTSPEED_SOURCE_DIR"] = issue84_driver.STS_SOURCE_WSL
    os.environ["STSRL_SOURCE_DIR"] = issue84_driver.STSRL_SOURCE_WSL

    backend = issue84_driver.configure_backend()
    backend.REPO = issue84_root
    backend.STUDY = issue84_root / "studies" / "issue-84-six-arm"
    backend.STUDY75 = backend.STUDY / "upstream" / "issue-75-no-expert-oracle"
    backend.STAGE1 = backend.STUDY75 / "stage1"
    backend.ARTIFACTS = DATA_ROOT / "issue85" / "artifacts"
    backend.SEED_TABLE = pilot_root / "pilot-seed-table.json"
    backend.SOURCE_LOCK = LOCK_PATH
    backend.PINNED_PYTHON = _path_from_wsl(issue84_driver.PYTHON_WSL)
    backend.__file__ = str(Path(__file__).resolve())
    backend.source_commit = lambda: identity["package_commit"]
    backend.source_lock_sha = lambda: identity["source_lock_sha256"]

    def issue85_load_modules(active_root: Path, *, pilot: bool):
        loaded = issue84_driver.load_modules(Path(active_root), pilot=pilot)
        algorithm, runtime, model, learning, executor = loaded
        gradients = importlib.import_module("gradients")
        from feature_firewall import new_path_projection
        from representations import Issue85Arm
        from runtime_integration import install_issue85_runtime

        install_issue85_runtime(
            runtime, executor, gradients, arm_type=Issue85Arm,
            payload_builder=new_path_projection,
        )
        _install_issue85_pilot_parent_evaluation_compat(executor, issue84_driver)
        original_load_model = executor._load_model
        parent_lock = LOCK["baseline_P"]
        baseline_lock = LOCK["baseline_A"]
        parent_path = _path_from_wsl(DATA_ROOT_WSL) / parent_lock["checkpoint_data_path"]
        base_path = _path_from_wsl(DATA_ROOT_WSL) / baseline_lock["checkpoint_data_path"]
        progress_path = pilot_root / "pilot-progress.json"

        def load_parent():
            actor, digest, source = original_load_model("pi12", proxy=False)
            if digest != parent_lock["model_digest"] or file_sha256(parent_path) != parent_lock["checkpoint_sha256"]:
                raise RuntimeError("the loaded parent differs from the locked Issue 82 pi12 checkpoint")
            return actor, digest, source

        def make_new_arm(arm: str):
            import torch
            from representations import Issue85Arm

            parent, parent_digest, parent_source = load_parent()
            actor = Issue85Arm(
                arm, model, parent.state_dict(),
                seed=derive_seed("residual-init", arm),
            ).eval()
            return actor, parent_digest, parent_source

        def load_model(label: str, *, proxy: bool = False):
            row = _CURRENT_ROW.get()
            if row is None:
                return original_load_model(label, proxy=proxy)
            phase = str(row.get("phase", ""))
            group = str(row.get("arm", ""))
            if phase == "training":
                arm = str(row.get("issue85_arm", ""))
                if arm not in NEW_ARMS or label != "pi12":
                    raise RuntimeError("Issue 85 training worker was not bound to its registered arm/pi12 start")
                actor, parent_digest, _source = make_new_arm(arm)
                return actor, model.model_digest(actor), {
                    "source": "issue82-pi12-plus-registered-issue85-initialization",
                    "parent_model_digest": parent_digest,
                    "pilot_only": True,
                    "formal_use": False,
                }
            if phase != "evaluation":
                return original_load_model(label, proxy=proxy)
            if group == "PARENT":
                return load_parent()
            if group == "A":
                actor = _load_checkpoint(model, base_path, baseline_lock["checkpoint_sha256"], baseline_lock["model_digest"])
                return actor, model.model_digest(actor), {
                    "source": "issue84-exact-B1024-baseline",
                    "checkpoint_sha256": baseline_lock["checkpoint_sha256"],
                    "pilot_only": True,
                    "formal_use": False,
                }
            if group not in NEW_ARMS or str(row.get("issue85_arm", "")) != group:
                raise RuntimeError("Issue 85 evaluation worker policy and arm bindings disagree")
            if label != "pi20":
                raise RuntimeError("new pilot policy dispatch label differs from the frozen evaluation row")
            progress = read_json(progress_path)
            receipt = progress.get("actor_updates", {}).get(group)
            if not isinstance(receipt, dict) or receipt.get("status") != "SUCCEEDED":
                raise RuntimeError(f"pilot actor update is incomplete for {group}")
            checkpoint_root = pilot_root
            if receipt.get("package_commit") != identity["package_commit"]:
                if group == "B":
                    reuse_path = pilot_root / "pilot-actor-update-reuse.json"
                    reuse = read_json(reuse_path) if reuse_path.is_file() else None
                    if (
                        not isinstance(reuse, dict)
                        or reuse != progress.get("actor_update_reuse")
                        or progress.get("actor_update_reuse_sha256") != file_sha256(reuse_path)
                        or reuse.get("current_package_commit") != identity["package_commit"]
                        or reuse.get("predecessor_package_commit") != PILOT_B_ACTOR_UPDATE_CARRY_FORWARD_SHA
                        or reuse.get("actor_update_receipt") != receipt
                    ):
                        raise RuntimeError("carried B actor update lacks its exact source-bound reuse manifest")
                    checkpoint_root = PILOT_ROOT / str(reuse["predecessor_package_commit"])
                else:
                    reuse_path = pilot_root / f"pilot-{group}-actor-update-reuse.json"
                    reuse = read_json(reuse_path) if reuse_path.is_file() else None
                    expected_reuse = progress.get("actor_update_reuse_by_arm", {}).get(group)
                    if (
                        not isinstance(reuse, dict)
                        or reuse != expected_reuse
                        or reuse.get("current_package_commit") != identity["package_commit"]
                        or reuse.get("predecessor_package_commit") != receipt.get("package_commit")
                        or reuse.get("source_lock_sha256") != identity["source_lock_sha256"]
                        or reuse.get("actor_update_receipt") != receipt
                        or reuse.get("actor_update_receipt_sha256") != file_sha256(
                            PILOT_ROOT / str(reuse["predecessor_package_commit"]) / "training" / group
                            / "pilot-updates" / group / str(reuse["actor_update_attempt"]) / "receipt.json"
                        )
                    ):
                        raise RuntimeError(f"carried {group} actor update lacks its exact source-bound reuse manifest")
                    checkpoint_root = PILOT_ROOT / str(reuse["predecessor_package_commit"])
            checkpoint = (checkpoint_root / receipt["checkpoint_path"]).resolve()
            if checkpoint_root.resolve() not in checkpoint.parents or not checkpoint.is_file():
                raise RuntimeError(f"pilot actor checkpoint path is outside its bound source root for {group}")
            if file_sha256(checkpoint) != receipt.get("checkpoint_sha256"):
                raise RuntimeError(f"pilot actor checkpoint hash differs for {group}")
            actor, parent_digest, _source = make_new_arm(group)
            import torch
            actor.load_state_dict(torch.load(checkpoint, map_location="cpu", weights_only=True), strict=True)
            digest = model.model_digest(actor)
            if digest != receipt.get("model_digest"):
                raise RuntimeError(f"pilot actor model digest differs for {group}")
            return actor.eval(), digest, {
                "source": f"issue85-nonformal-pilot-{group}",
                "checkpoint_sha256": receipt["checkpoint_sha256"],
                "parent_model_digest": parent_digest,
                "pilot_only": True,
                "formal_use": False,
            }

        executor._load_model = load_model
        return algorithm, runtime, model, learning, executor

    backend.load_modules = issue85_load_modules
    return backend, issue84_driver


def _verify_baselines(backend: Any, issue84_driver: Any, pilot_root: Path) -> dict[str, Any]:
    scratch = pilot_root / "baseline-verification"
    scratch.mkdir(parents=True, exist_ok=True)
    loaded = backend.load_modules(scratch, pilot=True)
    _algorithm, _runtime, model, _learning, executor = loaded
    parent, parent_digest, parent_source = executor._load_model("pi12", proxy=False)
    baseline = LOCK["baseline_P"]
    if (
        model.model_digest(parent) != baseline["model_digest"]
        or parent_digest != baseline["model_digest"]
        or parent_source.get("checkpoint_sha256") != baseline["checkpoint_sha256"]
    ):
        raise RuntimeError("loaded pi12 model digest differs from the Issue 85 lock")
    checkpoint_path = _path_from_wsl(DATA_ROOT_WSL) / LOCK["baseline_A"]["checkpoint_data_path"]
    actor = _load_checkpoint(
        model, checkpoint_path,
        LOCK["baseline_A"]["checkpoint_sha256"],
        LOCK["baseline_A"]["model_digest"],
    )
    optimizer_path = _path_from_wsl(DATA_ROOT_WSL) / baseline["optimizer_data_path"]
    if file_sha256(optimizer_path) != baseline["optimizer_sha256"]:
        raise RuntimeError("locked pi12 optimizer bytes differ")
    prior = LOCK["prior_preflight_evidence"][0]
    prior_path = _path_from_wsl(DATA_ROOT_WSL) / prior["report_data_path"]
    if file_sha256(prior_path) != prior["sha256"]:
        raise RuntimeError("the retained no-game P/A ancestry report does not match its source lock")
    report = read_json(prior_path)
    if report.get("verified") is not True:
        raise RuntimeError("retained no-game P/A ancestry report is not marked verified")
    checks = report.get("checks", {})
    ancestry = checks.get("A_training_ancestry", {})
    if ancestry.get("selected_owner_folders") != 1024 or len(ancestry.get("blocks", [])) != 8:
        raise RuntimeError("retained B1024 baseline ancestry is incomplete")
    if actor is None:
        raise AssertionError("baseline load unexpectedly returned no actor")
    return {
        "status": "VERIFIED_NO_NEW_GAMES",
        "parent_checkpoint_sha256": baseline["checkpoint_sha256"],
        "parent_model_digest": baseline["model_digest"],
        "parent_optimizer_sha256": baseline["optimizer_sha256"],
        "base_checkpoint_sha256": LOCK["baseline_A"]["checkpoint_sha256"],
        "base_model_digest": LOCK["baseline_A"]["model_digest"],
        "ancestry_report_sha256": prior["sha256"],
        "baseline_validation_game_count": 0,
    }


def _initial_parity(actor: Any, parent: Any, examples: Sequence[Any], *, arm: str) -> dict[str, Any]:
    import torch
    from representations import fold_widened_hash_features

    max_logit_error = 0.0
    max_probability_error = 0.0
    decisions = 0
    actor.eval()
    parent.eval()
    with torch.no_grad():
        for owner in examples:
            base = getattr(owner, "base", None)
            if base is None:
                raise RuntimeError("captured Issue 84 training example has no base wrapper")
            owner_decisions = getattr(base, "decisions", None)
            if owner_decisions is None:
                raise RuntimeError("captured Issue 84 training example base has no decisions")
            for decision in owner_decisions:
                legacy = torch.as_tensor(decision.candidate_features, dtype=torch.float32)
                if arm in {"B", "D"}:
                    wide = torch.as_tensor(decision.wide_candidate_features, dtype=torch.float32)
                    folded = fold_widened_hash_features(wide)
                    # The legacy and widened encoders independently accumulate
                    # float32 values, so folding can differ by rounding order.
                    # Keep this tighter than the policy-level parity tolerance.
                    if not torch.allclose(folded, legacy, rtol=0.0, atol=WIDENED_FOLD_ATOL):
                        max_fold_error = float(torch.max(torch.abs(folded - legacy)).item())
                        raise RuntimeError(
                            f"pilot widened matrix for {arm} exceeds FP32 fold tolerance "
                            f"(max_abs_error={max_fold_error:.9g}, atol={WIDENED_FOLD_ATOL:.9g})"
                        )
                    candidate = wide
                else:
                    candidate = legacy
                payload = decision.issue85_payload or {}
                new_logits = actor.logits(candidate, payload)
                parent_logits = parent.logits(legacy)
                logits_error = float(torch.max(torch.abs(new_logits - parent_logits)).item())
                new_probs = torch.softmax(new_logits, dim=0)
                parent_probs = torch.softmax(parent_logits, dim=0)
                probability_error = float(torch.max(torch.abs(new_probs - parent_probs)).item())
                max_logit_error = max(max_logit_error, logits_error)
                max_probability_error = max(max_probability_error, probability_error)
                if not torch.allclose(new_probs, parent_probs, rtol=0.0, atol=1e-6):
                    raise RuntimeError(f"Issue 85 {arm} initial policy differs from pi12 on captured native rows")
                decisions += 1
    if decisions <= 0:
        raise RuntimeError(f"pilot arm {arm} produced no actual native training decisions for parity")
    return {
        "status": "PASS",
        "arm": arm,
        "captured_native_training_decisions": decisions,
        "maximum_absolute_logit_difference": max_logit_error,
        "maximum_absolute_probability_difference": max_probability_error,
        "probability_atol": 1e-6,
        "uses_outcomes_for_efficacy": False,
    }


def _atomic_torch_save(path: Path, value: object) -> str:
    import torch

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("xb") as stream:
            torch.save(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return file_sha256(path)


def _load_pilot_training_examples(
    issue84_driver: Any,
    rows: list[dict[str, Any]],
    *,
    root: Path,
    executor: Any,
    package_commit: str,
    receipt_roots_by_owner: Mapping[str, tuple[Path, str]] | None = None,
) -> list[Any]:
    loader = getattr(issue84_driver, "load_training_outcomes", None)
    if not callable(loader):
        raise RuntimeError("locked Issue 84 driver lacks its training-outcome loader")
    bindings = receipt_roots_by_owner or {}
    if not bindings:
        examples, _outcomes = loader(
            rows, root=root, executor=executor, package=package_commit,
        )
        return examples
    examples: list[Any] = []
    for row in rows:
        selected_root, selected_package = bindings.get(
            str(row["unit_id"]), (root, package_commit),
        )
        owner_examples, _outcomes = loader(
            [row], root=Path(selected_root), executor=executor, package=selected_package,
        )
        if len(owner_examples) != 1:
            raise RuntimeError(f"training-outcome loader returned no unique example for {row['unit_id']}")
        examples.extend(owner_examples)
    return examples


def _issue84_actor_gradient_norm_clip(issue84_driver: Any) -> float:
    """Read the actor clip threshold from the source-locked Issue 84 contract."""
    contract_path = getattr(issue84_driver, "CONTRACT_PATH", None)
    if contract_path is None:
        raise RuntimeError("locked Issue 84 driver lacks its contract path")
    contract = read_json(Path(contract_path))
    unchanged = contract.get("unchanged_actor_and_environment")
    if not isinstance(unchanged, dict):
        raise ValueError("locked Issue 84 contract lacks unchanged_actor_and_environment")
    value = unchanged.get("actor_gradient_norm_clip")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("locked Issue 84 contract has no numeric actor_gradient_norm_clip")
    clip = float(value)
    if not math.isfinite(clip) or clip <= 0:
        raise ValueError("locked Issue 84 actor_gradient_norm_clip must be finite and positive")
    return clip


def _pilot_training_example_is_usable(example: Any) -> bool:
    """Check completeness through the locked #84 OwnerTrainingExample.base API."""
    base = getattr(example, "base", None)
    return (
        base is not None
        and bool(getattr(base, "complete", False))
        and bool(getattr(base, "decisions", ()))
    )


def _train_pilot_arm(
    backend: Any,
    issue84_driver: Any,
    *,
    arm: str,
    rows: list[dict[str, Any]],
    root: Path,
    pilot_root: Path,
    identity: Mapping[str, str],
    examples_root: Path | None = None,
    examples_package_commit: str | None = None,
    example_receipt_roots_by_owner: Mapping[str, tuple[Path, str]] | None = None,
    active_clock: Any | None = None,
    resume_clock_elapsed: float | None = None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    import torch
    from representations import Issue85Arm, continue_parent_adamw

    actor_gradient_norm_clip = _issue84_actor_gradient_norm_clip(issue84_driver)
    loaded = backend.load_modules(root, pilot=True)
    _algorithm, _runtime, model, learning, executor = loaded
    examples = _load_pilot_training_examples(
        issue84_driver,
        rows,
        root=Path(examples_root) if examples_root is not None else root,
        executor=executor,
        package_commit=examples_package_commit or identity["package_commit"],
        receipt_roots_by_owner=example_receipt_roots_by_owner,
    )
    if len(examples) != len(PILOT_OWNERS) or len({int(row.owner_seed) for row in examples}) != len(PILOT_OWNERS):
        raise RuntimeError(f"pilot actor update for {arm} needs 16 unique complete owner outcomes")
    if any(not _pilot_training_example_is_usable(row) for row in examples):
        raise RuntimeError(f"pilot actor update for {arm} contains an incomplete/decision-free game")
    owner_sha = sha256(",".join(str(int(row.owner_seed)) for row in examples).encode("ascii"))
    update_root = root / "pilot-updates" / arm
    update_root.mkdir(parents=True, exist_ok=True)
    attempt_dirs = sorted(path for path in update_root.glob("attempt-*") if path.is_dir())
    successful: list[tuple[dict[str, Any], Path]] = []
    resumable: tuple[Path, dict[str, Any]] | None = None
    for attempt_dir in attempt_dirs:
        progress_path = attempt_dir / "progress.json"
        progress = read_json(progress_path) if progress_path.is_file() else None
        receipt_path = attempt_dir / "receipt.json"
        receipt = read_json(receipt_path) if receipt_path.is_file() else None
        binding = progress if progress is not None else receipt
        if binding is not None and (
            binding.get("arm") != arm
            or binding.get("package_commit") != identity["package_commit"]
            or binding.get("source_lock_sha256") != identity["source_lock_sha256"]
            or binding.get("owner_seed_sha256") != owner_sha
        ):
            raise RuntimeError(f"pilot actor attempt {attempt_dir.name} has a different arm/source/owner binding")
        if receipt is not None:
            if receipt.get("status") == "FAILED":
                raise RuntimeError(
                    f"pilot actor update for {arm} has a retained failed attempt "
                    f"({receipt.get('failure', {}).get('detail', 'see receipt')}); preserving it and refusing an automatic repeat"
                )
            if receipt.get("status") != "SUCCEEDED" or receipt.get("schema_id") != "issue85-pilot-actor-update-v1":
                raise RuntimeError(f"pilot actor update receipt is invalid for {arm}: {receipt_path}")
            state_path = (pilot_root / receipt.get("checkpoint_path", "")).resolve()
            optimizer_path_out = (pilot_root / receipt.get("optimizer_path", "")).resolve()
            if (
                attempt_dir.resolve() not in state_path.parents
                or attempt_dir.resolve() not in optimizer_path_out.parents
                or not state_path.is_file()
                or not optimizer_path_out.is_file()
                or file_sha256(state_path) != receipt.get("checkpoint_sha256")
                or file_sha256(optimizer_path_out) != receipt.get("optimizer_sha256")
                or progress is None
                or progress.get("status") == "FAILED"
                or int(progress.get("completed_steps", -1)) != ACTOR_STEPS
                or progress.get("model_digest") not in {None, receipt.get("model_digest")}
            ):
                raise RuntimeError(f"completed pilot actor update for {arm} has invalid checkpoint/progress evidence")
            if progress.get("status") != "SUCCEEDED":
                progress.update({
                    "status": "SUCCEEDED",
                    "recovered_from_immutable_receipt": True,
                    "model_digest": receipt["model_digest"],
                    "checkpoint_sha256": receipt["checkpoint_sha256"],
                    "optimizer_sha256": receipt["optimizer_sha256"],
                    "finished_utc": datetime.now(timezone.utc).isoformat(),
                })
                _atomic_json(progress_path, progress)
            successful.append((receipt, state_path))
        elif progress is not None:
            if progress.get("status") == "FAILED":
                raise RuntimeError(f"pilot actor update for {arm} has a retained failed attempt; preserving it")
            if progress.get("status") == "SUCCEEDED":
                raise RuntimeError(f"pilot actor update for {arm} says SUCCEEDED but has no immutable receipt")
            if progress.get("status") not in {"RUNNING", "INTERRUPTED"}:
                raise RuntimeError(f"unrecognized pilot actor progress state for {arm}: {progress.get('status')}")
            if resumable is not None:
                raise RuntimeError(f"multiple incomplete pilot actor updates exist for {arm}; refusing ambiguous resume")
            resumable = (attempt_dir, progress)
        else:
            if resumable is not None:
                raise RuntimeError(f"multiple incomplete pilot actor update attempts exist for {arm}")
            resumable = (attempt_dir, {
                "schema_id": "issue85-pilot-actor-update-progress-v1",
                "status": "INTERRUPTED",
                "arm": arm,
                "package_commit": identity["package_commit"],
                "source_lock_sha256": identity["source_lock_sha256"],
                "owner_seed_sha256": owner_sha,
                "completed_steps": 0,
                "elapsed_seconds": 0.0,
                "resume_history": [],
                "diagnostics": [],
            })
    if successful:
        if len(successful) != 1 or len(attempt_dirs) != 1:
            raise RuntimeError(f"multiple pilot actor attempts exist for {arm}; preserving all and refusing to select one")
        receipt, state_path = successful[0]
        return {**receipt, "checkpoint_path": state_path.relative_to(pilot_root).as_posix()}
    if resumable is not None and resumable[0] != attempt_dirs[-1]:
        raise RuntimeError(f"an incomplete pilot actor update is not the latest attempt for {arm}")

    training_row = rows[0]
    token = _CURRENT_ROW.set(training_row)
    try:
        actor, _initial_digest, _source = executor._load_model("pi12", proxy=False)
    finally:
        _CURRENT_ROW.reset(token)
    if not isinstance(actor, Issue85Arm) or actor.arm != arm:
        raise RuntimeError(f"training actor loader returned the wrong arm for {arm}")
    parent = executor._load_model("pi12", proxy=False)[0]
    if isinstance(parent, Issue85Arm):
        parent = parent.backbone
    parity = _initial_parity(actor, parent, examples, arm=arm)

    parent_optimizer = LOCK["baseline_P"]
    optimizer_path = _path_from_wsl(DATA_ROOT_WSL) / parent_optimizer["optimizer_data_path"]
    if file_sha256(optimizer_path) != parent_optimizer["optimizer_sha256"]:
        raise RuntimeError("pi12 AdamW source checkpoint differs before the pilot actor update")
    optimizer_bundle = torch.load(optimizer_path, map_location="cpu", weights_only=True)
    optimizer, optimizer_mapping = continue_parent_adamw(
        actor, optimizer_bundle, expected_parent_digest=parent_optimizer["model_digest"],
    )
    gradients = importlib.import_module("gradients")
    if resumable is None:
        attempt_number = 1 + len(attempt_dirs)
        attempt = update_root / f"attempt-{attempt_number:04d}"
        attempt.mkdir(parents=True, exist_ok=False)
        update_progress = {
            "schema_id": "issue85-pilot-actor-update-progress-v1",
            "status": "RUNNING",
            "arm": arm,
            "package_commit": identity["package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "owner_seed_sha256": owner_sha,
            "parent_model_digest": parent_optimizer["model_digest"],
            "parent_optimizer_sha256": parent_optimizer["optimizer_sha256"],
            "completed_steps": 0,
            "last_checkpoint": None,
            "elapsed_seconds": 0.0,
            "resume_history": [],
            "diagnostics": [],
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "heartbeat_utc": datetime.now(timezone.utc).isoformat(),
        }
    else:
        attempt, update_progress = resumable
        if (
            update_progress.get("parent_model_digest") not in {None, parent_optimizer["model_digest"]}
            or update_progress.get("parent_optimizer_sha256") not in {None, parent_optimizer["optimizer_sha256"]}
        ):
            raise RuntimeError(f"incomplete pilot actor update for {arm} is bound to a different parent optimizer")
        resume_history = list(update_progress.get("resume_history", []))
        resume_history.append({"observed_utc": datetime.now(timezone.utc).isoformat(), "status_before_resume": update_progress.get("status")})
        update_progress["resume_history"] = resume_history
        update_progress["status"] = "RUNNING"
        update_progress["resumed_utc"] = datetime.now(timezone.utc).isoformat()
        update_progress["parent_model_digest"] = parent_optimizer["model_digest"]
        update_progress["parent_optimizer_sha256"] = parent_optimizer["optimizer_sha256"]

    attempt_progress_path = attempt / "progress.json"

    def publish_update_progress() -> None:
        if progress_callback is not None:
            progress_callback({
                "status": update_progress.get("status"),
                "attempt": attempt.name,
                "completed_steps": int(update_progress.get("completed_steps", 0)),
                "expected_steps": ACTOR_STEPS,
                "elapsed_seconds": float(update_progress.get("elapsed_seconds", 0.0)),
                "active_clock_elapsed_seconds": update_progress.get("active_clock_elapsed_seconds"),
                "last_checkpoint": update_progress.get("last_checkpoint"),
                "failure": update_progress.get("failure"),
                "heartbeat_utc": update_progress.get("heartbeat_utc"),
            })

    completed_steps = int(update_progress.get("completed_steps", 0))
    if completed_steps < 0 or completed_steps > ACTOR_STEPS or completed_steps % ACTOR_CHECKPOINT_INTERVAL:
        raise RuntimeError(f"pilot actor checkpoint step is invalid for {arm}: {completed_steps}")
    checkpoint_root = attempt / "checkpoints"
    if checkpoint_root.exists():
        disk_checkpoints: list[tuple[int, Path, dict[str, Any]]] = []
        for checkpoint_dir in sorted(path for path in checkpoint_root.glob("step-*") if path.is_dir()):
            metadata_path = checkpoint_dir / "checkpoint.json"
            if not metadata_path.is_file():
                raise RuntimeError(f"pilot actor checkpoint directory lacks its immutable manifest: {checkpoint_dir}")
            metadata = read_json(metadata_path)
            step = int(metadata.get("completed_steps", -1))
            state_path = checkpoint_dir / "actor-state.pt"
            optimizer_checkpoint_path = checkpoint_dir / "optimizer-state.pt"
            if (
                metadata.get("schema_id") != "issue85-pilot-actor-update-checkpoint-v1"
                or metadata.get("arm") != arm
                or metadata.get("package_commit") != identity["package_commit"]
                or metadata.get("source_lock_sha256") != identity["source_lock_sha256"]
                or metadata.get("owner_seed_sha256") != owner_sha
                or step <= 0
                or step > ACTOR_STEPS
                or step % ACTOR_CHECKPOINT_INTERVAL
                or not state_path.is_file()
                or not optimizer_checkpoint_path.is_file()
                or file_sha256(state_path) != metadata.get("checkpoint_sha256")
                or file_sha256(optimizer_checkpoint_path) != metadata.get("optimizer_sha256")
                or len(metadata.get("diagnostics", [])) != step
            ):
                raise RuntimeError(f"pilot actor checkpoint failed source/owner/hash validation: {checkpoint_dir}")
            disk_checkpoints.append((step, checkpoint_dir, metadata))
        if disk_checkpoints:
            disk_step, disk_dir, disk_metadata = max(disk_checkpoints, key=lambda item: item[0])
            if completed_steps > disk_step:
                raise RuntimeError(f"pilot actor progress is ahead of its durable checkpoint for {arm}")
            update_progress["completed_steps"] = disk_step
            update_progress["last_checkpoint"] = {
                "completed_steps": disk_step,
                "path": disk_dir.relative_to(attempt).as_posix(),
                "checkpoint_sha256": disk_metadata["checkpoint_sha256"],
                "optimizer_sha256": disk_metadata["optimizer_sha256"],
            }
            update_progress["diagnostics"] = disk_metadata["diagnostics"]
            update_progress["elapsed_seconds"] = max(
                float(update_progress.get("elapsed_seconds", 0.0)),
                float(disk_metadata.get("elapsed_seconds", 0.0)),
            )
            disk_clock_elapsed = disk_metadata.get("active_clock_elapsed_seconds")
            progress_clock_elapsed = update_progress.get("active_clock_elapsed_seconds")
            known_clock_values = [
                float(value) for value in (disk_clock_elapsed, progress_clock_elapsed) if value is not None
            ]
            if any(not math.isfinite(value) or value < 0 for value in known_clock_values):
                raise RuntimeError(f"pilot actor checkpoint clock is invalid for {arm}")
            if known_clock_values:
                update_progress["active_clock_elapsed_seconds"] = max(known_clock_values)
            completed_steps = disk_step
    if completed_steps:
        last_checkpoint = update_progress.get("last_checkpoint")
        if not isinstance(last_checkpoint, dict) or int(last_checkpoint.get("completed_steps", -1)) != completed_steps:
            raise RuntimeError(f"pilot actor progress has no matching checkpoint manifest for {arm}")
        checkpoint_dir = attempt / str(last_checkpoint["path"])
        checkpoint_meta_path = checkpoint_dir / "checkpoint.json"
        checkpoint_meta = read_json(checkpoint_meta_path)
        state_path = checkpoint_dir / "actor-state.pt"
        optimizer_checkpoint_path = checkpoint_dir / "optimizer-state.pt"
        if (
            checkpoint_meta.get("completed_steps") != completed_steps
            or checkpoint_meta.get("arm") != arm
            or checkpoint_meta.get("package_commit") != identity["package_commit"]
            or checkpoint_meta.get("source_lock_sha256") != identity["source_lock_sha256"]
            or checkpoint_meta.get("owner_seed_sha256") != owner_sha
            or len(checkpoint_meta.get("diagnostics", [])) != completed_steps
            or not state_path.is_file()
            or not optimizer_checkpoint_path.is_file()
            or file_sha256(state_path) != checkpoint_meta.get("checkpoint_sha256")
            or file_sha256(optimizer_checkpoint_path) != checkpoint_meta.get("optimizer_sha256")
        ):
            raise RuntimeError(f"pilot actor checkpoint for {arm} failed source/owner/hash validation")
        actor.load_state_dict(torch.load(state_path, map_location="cpu", weights_only=True), strict=True)
        optimizer.load_state_dict(torch.load(optimizer_checkpoint_path, map_location="cpu", weights_only=True))
        current_steps = sorted(
            int(value["step"].item()) for value in optimizer.state.values()
            if isinstance(value, dict) and "step" in value
        )
        expected_partial_steps = {64 + completed_steps} if arm == "B" else {completed_steps, 64 + completed_steps}
        if set(current_steps) != expected_partial_steps:
            raise RuntimeError(f"resumed optimizer step state for {arm} is unexpected: {current_steps}")
    diagnostics = list(update_progress.get("diagnostics", []))
    if len(diagnostics) != completed_steps or [int(item.get("step", -1)) for item in diagnostics] != list(range(1, completed_steps + 1)):
        raise RuntimeError(f"pilot actor progress diagnostics do not match its saved step for {arm}")
    _atomic_json(attempt_progress_path, update_progress)
    publish_update_progress()

    try:
        elapsed_before_invocation, recovered_clock_tail = actor_update_resume_wall(update_progress, resume_clock_elapsed)
    except ValueError as exc:
        raise RuntimeError(f"pilot actor update clock binding is invalid for {arm}") from exc

    def active_elapsed_now() -> float | None:
        if active_clock is None:
            return None
        value = float(active_clock.elapsed())
        if not math.isfinite(value) or value < 0:
            raise RuntimeError("active pilot task clock returned an invalid elapsed duration")
        return value

    def save_progress_checkpoint(step: int) -> None:
        checkpoint_dir = attempt / "checkpoints" / f"step-{step:03d}"
        checkpoint_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary_dir = checkpoint_dir.with_name(f".{checkpoint_dir.name}.tmp-{os.getpid()}-{time.time_ns()}")
        temporary_dir.mkdir(parents=False, exist_ok=False)
        state_path = temporary_dir / "actor-state.pt"
        state_sha = _atomic_torch_save(state_path, actor.state_dict())
        optimizer_path_out = temporary_dir / "optimizer-state.pt"
        optimizer_sha = _atomic_torch_save(optimizer_path_out, optimizer.state_dict())
        checkpoint_meta = {
            "schema_id": "issue85-pilot-actor-update-checkpoint-v1",
            "arm": arm,
            "package_commit": identity["package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "owner_seed_sha256": owner_sha,
            "completed_steps": step,
            "elapsed_seconds": elapsed_before_invocation + (time.monotonic() - started),
            "active_clock_elapsed_seconds": active_elapsed_now(),
            "checkpoint_sha256": state_sha,
            "optimizer_sha256": optimizer_sha,
            "diagnostics": list(diagnostics),
        }
        _write_once(temporary_dir / "checkpoint.json", checkpoint_meta)
        os.replace(temporary_dir, checkpoint_dir)
        update_progress["completed_steps"] = step
        update_progress["last_checkpoint"] = {
            "completed_steps": step,
            "path": checkpoint_dir.relative_to(attempt).as_posix(),
            "checkpoint_sha256": state_sha,
            "optimizer_sha256": optimizer_sha,
        }
        update_progress["elapsed_seconds"] = float(checkpoint_meta["elapsed_seconds"])
        if checkpoint_meta.get("active_clock_elapsed_seconds") is not None:
            update_progress["active_clock_elapsed_seconds"] = checkpoint_meta["active_clock_elapsed_seconds"]
        update_progress["diagnostics"] = diagnostics
        update_progress["heartbeat_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_json(attempt_progress_path, update_progress)
        publish_update_progress()

    actor.train()
    started = time.monotonic()

    def record_update_failure(exc: Exception) -> None:
        failure = {"type": type(exc).__name__, "detail": str(exc)[:2000]}
        update_progress.update({
            "status": "FAILED",
            "failed_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": elapsed_before_invocation + (time.monotonic() - started),
            "active_clock_elapsed_seconds": active_elapsed_now(),
            "failure": failure,
            "heartbeat_utc": datetime.now(timezone.utc).isoformat(),
        })
        _atomic_json(attempt_progress_path, update_progress)
        publish_update_progress()
        _write_once(attempt / "receipt.json", {
            "schema_id": "issue85-pilot-actor-update-v1",
            "status": "FAILED",
            "arm": arm,
            "package_commit": identity["package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "owner_seed_sha256": owner_sha,
            "completed_steps": int(update_progress.get("completed_steps", 0)),
            "elapsed_seconds": float(update_progress["elapsed_seconds"]),
            "active_clock_elapsed_seconds": update_progress.get("active_clock_elapsed_seconds"),
            "failure": failure,
        })

    try:
        for step in range(completed_steps, ACTOR_STEPS):
            loss = gradients.minibatch_loss(actor, examples, arm=arm, block_examples=examples)
            loss_value = float(loss.detach().item())
            if not math.isfinite(loss_value):
                raise RuntimeError(f"pilot actor loss became non-finite at step {step + 1}")
            parameters = tuple(parameter for parameter in actor.parameters() if parameter.requires_grad)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            before = float(torch.nn.utils.clip_grad_norm_(parameters, actor_gradient_norm_clip).item())
            if not math.isfinite(before):
                raise RuntimeError(f"pilot actor gradient norm became non-finite at step {step + 1}")
            optimizer.step()
            diagnostics.append({"step": step + 1, "loss": loss_value, "grad_norm_before_clip": before})
            next_step = step + 1
            if next_step % ACTOR_CHECKPOINT_INTERVAL == 0 or next_step == ACTOR_STEPS:
                for name, parameter in actor.named_parameters():
                    if not bool(torch.isfinite(parameter).all().item()):
                        raise RuntimeError(f"pilot actor parameter {name} became non-finite at step {next_step}")
                for state in optimizer.state.values():
                    for value in state.values():
                        if torch.is_tensor(value) and value.is_floating_point() and not bool(torch.isfinite(value).all().item()):
                            raise RuntimeError(f"pilot optimizer state became non-finite at step {next_step}")
                save_progress_checkpoint(next_step)
    except Exception as exc:
        record_update_failure(exc)
        raise
    elapsed_total = elapsed_before_invocation + (time.monotonic() - started)
    actor.eval()
    model_digest = model.model_digest(actor)
    step_values = sorted(
        int(value["step"].item()) for value in optimizer.state.values()
        if isinstance(value, dict) and "step" in value
    )
    expected_step_values = {128} if arm == "B" else {64, 128}
    if len(step_values) != len(tuple(actor.parameters())) or set(step_values) != expected_step_values:
        error = RuntimeError(f"pilot optimizer steps for {arm} differ from parent64 + 64 updates: {step_values}")
        record_update_failure(error)
        raise error

    state_path = attempt / "actor-state.pt"
    state_sha = _atomic_torch_save(state_path, actor.state_dict())
    optimizer_path_out = attempt / "optimizer-state.pt"
    optimizer_sha = _atomic_torch_save(optimizer_path_out, optimizer.state_dict())
    receipt = {
        "schema_id": "issue85-pilot-actor-update-v1",
        "status": "SUCCEEDED",
        "scope": "PILOT_ONLY / NONFORMAL",
        "arm": arm,
        "package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "parent_model_digest": parent_optimizer["model_digest"],
        "parent_optimizer_sha256": parent_optimizer["optimizer_sha256"],
        "optimizer_mapping": optimizer_mapping,
        "owner_count": len(examples),
        "owner_seed_sha256": owner_sha,
        "minibatch_owner_count": len(examples),
        "formal_minibatch_owner_count": int(CONTRACT["training"]["minibatch_owners"]),
        "minibatch_deviation": "pilot uses all 16 unique pilot owners per update; the registered formal minibatch remains 32",
        "objective": "issue84-B-LENGTH_ONLY",
        "completed_steps": ACTOR_STEPS,
        "optimizer_parameter_step_values": sorted(set(step_values)),
        "elapsed_seconds": elapsed_total,
        "active_clock_elapsed_seconds": active_elapsed_now(),
        "recovered_clock_tail_seconds": recovered_clock_tail,
        "model_digest": model_digest,
        "checkpoint_path": state_path.relative_to(pilot_root).as_posix(),
        "checkpoint_sha256": state_sha,
        "optimizer_path": optimizer_path_out.relative_to(pilot_root).as_posix(),
        "optimizer_sha256": optimizer_sha,
        "initial_probability_parity": parity,
        "loss_summary": {
            "first": diagnostics[0], "last": diagnostics[-1],
            "finite_all_steps": len(diagnostics) == ACTOR_STEPS and all(math.isfinite(item["loss"]) for item in diagnostics),
        },
    }
    if receipt["loss_summary"]["finite_all_steps"] is not True:
        error = RuntimeError(f"pilot actor diagnostics are incomplete or non-finite for {arm}")
        record_update_failure(error)
        raise error
    _write_once(attempt / "receipt.json", receipt)
    update_progress.update({
        "status": "SUCCEEDED",
        "completed_steps": ACTOR_STEPS,
        "elapsed_seconds": elapsed_total,
        "active_clock_elapsed_seconds": receipt["active_clock_elapsed_seconds"],
        "model_digest": model_digest,
        "checkpoint_sha256": state_sha,
        "optimizer_sha256": optimizer_sha,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "heartbeat_utc": datetime.now(timezone.utc).isoformat(),
    })
    _atomic_json(attempt_progress_path, update_progress)
    publish_update_progress()
    return receipt


def _forecast(progress: Mapping[str, Any]) -> dict[str, Any]:
    training = progress["training_runs"]
    evaluation = progress["evaluation_runs"]
    updates = progress["actor_updates"]
    if set(training) != set(NEW_ARMS) or set(evaluation) != set(POLICIES) or set(updates) != set(NEW_ARMS):
        raise RuntimeError("cannot project formal time from an incomplete or failed pilot")
    if any(item.get("status") != "SUCCEEDED" or item.get("successful_completions") != item.get("expected") for group in (training, evaluation) for item in group.values()):
        raise RuntimeError("cannot project formal time from any incomplete pilot game group")
    train_estimate = sum(float(training[arm]["elapsed_seconds"]) * (1024 / len(PILOT_OWNERS)) for arm in NEW_ARMS)
    eval_estimate = sum(float(evaluation[policy]["elapsed_seconds"]) * (384 / len(PILOT_OWNERS)) for policy in POLICIES)
    # Formal uses 8 blocks/arm, 64 updates/block and 32-owner batches. Pilot
    # measures 64 updates/arm over 16 owners; owner-loss work is scaled linearly.
    actor_estimate = sum(float(updates[arm]["elapsed_seconds"]) * 8 * (32 / len(PILOT_OWNERS)) for arm in NEW_ARMS)
    game_projection = train_estimate + eval_estimate
    formal_component_projection = game_projection + actor_estimate
    actor_update_reuse = progress.get("actor_update_reuse", {})
    carried_c_failures = actor_update_reuse.get("prior_C_failed_attempts", [])
    if not isinstance(carried_c_failures, list):
        raise RuntimeError("pilot forecast cannot count malformed carried C failure records")
    carried_c_failure_ids = [
        str(item.get("unit_id", "")) for item in carried_c_failures if isinstance(item, Mapping)
    ]
    if (
        len(carried_c_failure_ids) != len(carried_c_failures)
        or any(not unit_id for unit_id in carried_c_failure_ids)
        or len(set(carried_c_failure_ids)) != len(carried_c_failure_ids)
    ):
        raise RuntimeError("pilot forecast cannot count unbound or duplicate carried C failures")
    c_run = training["C"]
    c_attempt_counts = c_run.get("attempt_count_by_owner", {})
    if not set(carried_c_failure_ids).issubset(set(c_attempt_counts)):
        raise RuntimeError("pilot forecast carried C failures do not match the C training owner ledger")
    c_carried_counts = c_run.get("carried_attempt_count_by_owner", {})
    c_outcome_reuse = progress.get("pilot_C_outcome_reuse")
    if isinstance(c_outcome_reuse, Mapping):
        c_prior_counts = c_outcome_reuse.get("prior_C_attempt_count_by_owner")
        original_failure_ids = set(carried_c_failure_ids)
        if not isinstance(c_prior_counts, dict) or set(c_prior_counts) != set(c_attempt_counts):
            raise RuntimeError("pilot forecast cannot verify all historical C attempt offsets")
        expected_carried_counts = {
            unit_id: int(c_prior_counts[unit_id]) + (1 if unit_id in original_failure_ids else 0)
            for unit_id in c_attempt_counts
        }
        if (
            c_carried_counts != expected_carried_counts
            or c_outcome_reuse.get("prior_C_cumulative_attempt_count_by_owner") != expected_carried_counts
            or c_outcome_reuse.get("prior_C_successful_games") != len(PILOT_OWNERS) // 4
            or c_outcome_reuse.get("prior_C_failed_attempt_count") != len(PILOT_OWNERS) // 4
        ):
            raise RuntimeError("pilot forecast C attempt offsets differ from the immutable carry-forward manifests")
        carried_attempt_count = sum(expected_carried_counts.values())
        carried_failure_count = len(carried_c_failure_ids) + int(c_outcome_reuse["prior_C_failed_attempt_count"])
    else:
        expected_carried_counts = {unit_id: 1 for unit_id in carried_c_failure_ids}
        if c_carried_counts != expected_carried_counts:
            raise RuntimeError("pilot forecast C attempt offsets differ from the immutable carry-forward manifest")
        carried_attempt_count = len(carried_c_failure_ids)
        carried_failure_count = carried_attempt_count
    return {
        "schema_id": "issue85-pilot-formal-wall-forecast-v1",
        "status": "PARTIAL_COMPONENT_PROJECTION_NOT_AN_ALL_IN_ESTIMATE_OR_CAP",
        "source_lock_sha256": progress["source_lock_sha256"],
        "package_commit": progress["package_commit"],
        "concurrency": progress["concurrency"],
        "pilot_training_games": sum(item["successful_completions"] for item in training.values()),
        "pilot_evaluation_games": sum(item["successful_completions"] for item in evaluation.values()),
        "formal_games": 7808,
        "training_active_wall_projection_seconds": train_estimate,
        "evaluation_active_wall_projection_seconds": eval_estimate,
        "actor_update_active_wall_projection_seconds": actor_estimate,
        "measured_pilot_cumulative_active_wall_seconds": progress["cumulative_active_wall_seconds"],
        "projected_registered_formal_games_and_actor_updates_active_wall_seconds": formal_component_projection,
        "pilot_plus_projected_games_and_actor_updates_active_wall_seconds": float(progress["cumulative_active_wall_seconds"]) + formal_component_projection,
        "unmeasured_required_work": [
            "formal validation/test execution and frozen analysis/bootstraps",
            "independent Reviewer review and Planner disposition",
            "formal retry/restart and long-tail variance beyond the pilot attempts",
            "source build/restore and queue overhead not included in measured stage times",
        ],
        "all_in_study_wall_estimate_seconds": None,
        "all_in_estimate_status": "UNKNOWN_UNTIL_UNMEASURED_REQUIRED_WORK_IS_ESTIMATED",
        "carried_prior_C_attempt_count": carried_attempt_count,
        "pilot_failed_attempt_count": carried_failure_count + sum(len(item.get("failed_attempts", [])) for group in (training, evaluation) for item in group.values()),
        "pilot_total_attempt_count": carried_attempt_count + sum(sum(int(value) for value in item.get("attempt_count_by_owner", {}).values()) for group in (training, evaluation) for item in group.values()),
        "carried_prior_C_failed_attempt_count": carried_failure_count,
        "carried_prior_C_successful_game_count": 0 if not isinstance(c_outcome_reuse, Mapping) else int(c_outcome_reuse["prior_C_successful_games"]),
        "projection_components": ["formal training game groups", "formal evaluation game groups", "formal actor-update work"],
        "pilot_batch_size_limitation": "training estimate scales the 16-owner pilot update to the registered 32-owner minibatch; this is an explicit linear cost assumption, not an equivalence result",
        "pilot_concurrency_limitation": "projection assumes the registered formal run uses the same measured concurrency and resource availability as this pilot",
        "owner_policy_comparison": "report forecast for Planner/owner comparison; no new numeric cap or changed formal cohort is inferred",
    }


def _validate_completed_pilot(
    report: Mapping[str, Any],
    progress: Mapping[str, Any],
    *,
    package_root: Path,
    identity: Mapping[str, str],
    backend: Any,
    seed_table: Mapping[str, Any],
    seed_table_sha256: str,
) -> None:
    seed_rows = seed_table.get("rows")
    if not isinstance(seed_rows, list):
        raise RuntimeError("successful pilot seed table does not contain its fixed rows")
    predecessor_commit = progress.get("predecessor_training_reuse", {}).get("predecessor_package_commit")
    predecessor = _load_predecessor_b_training(
        backend,
        identity=identity,
        current_seed_table=seed_table,
        predecessor_commit=str(predecessor_commit or ""),
    )
    predecessor_document = _predecessor_receipt_document(predecessor, current_identity=identity)
    predecessor_manifest_path = package_root / "pilot-predecessor-training-reuse.json"
    if (
        not predecessor_manifest_path.is_file()
        or read_json(predecessor_manifest_path) != predecessor_document
        or progress.get("predecessor_training_reuse") != predecessor_document
        or report.get("predecessor_training_reuse") != predecessor_document
        or progress.get("predecessor_training_reuse_sha256") != file_sha256(predecessor_manifest_path)
    ):
        raise RuntimeError("successful pilot report lacks its validated immutable predecessor-game reference")
    b_actor_update = _load_predecessor_b_actor_update(
        backend,
        identity=identity,
        current_seed_table=seed_table,
        b_game_predecessor=predecessor,
    )
    actor_reuse_document = b_actor_update["document"]
    actor_reuse_path = package_root / "pilot-actor-update-reuse.json"
    if (
        not actor_reuse_path.is_file()
        or read_json(actor_reuse_path) != actor_reuse_document
        or progress.get("actor_update_reuse") != actor_reuse_document
        or report.get("actor_update_reuse") != actor_reuse_document
        or progress.get("actor_update_reuse_sha256") != file_sha256(actor_reuse_path)
        or report.get("actor_update_reuse_sha256") != file_sha256(actor_reuse_path)
        or progress.get("actor_updates", {}).get("B") != b_actor_update["receipt"]
    ):
        raise RuntimeError("successful pilot report lacks the validated immutable B actor-update reuse reference")
    c_outcome_reuse = _load_predecessor_c_training_outcomes(
        backend, identity=identity, current_seed_table=seed_table,
    )
    c_outcome_document = c_outcome_reuse["document"]
    c_outcome_path = package_root / "pilot-C-training-outcome-reuse.json"
    if c_outcome_document.get("prior_C_inherited_failed_attempt_receipts") != actor_reuse_document.get("prior_C_failed_attempts"):
        raise RuntimeError("C outcome reuse does not preserve the independently validated original failure receipts")
    if (
        not c_outcome_path.is_file()
        or read_json(c_outcome_path) != c_outcome_document
        or progress.get("pilot_C_outcome_reuse") != c_outcome_document
        or report.get("C_outcome_reuse") != c_outcome_document
        or report.get("pilot_C_outcome_reuse") != c_outcome_document
        or progress.get("pilot_C_outcome_reuse_sha256") != file_sha256(c_outcome_path)
        or report.get("C_outcome_reuse_sha256") != file_sha256(c_outcome_path)
        or report.get("pilot_C_outcome_reuse_sha256") != file_sha256(c_outcome_path)
    ):
        raise RuntimeError("successful pilot report lacks the validated immutable C outcome receipt reference")
    completed_reuse = None
    completed_reuse_roots_by_arm: dict[str, dict[str, tuple[Path, str]]] = {}
    completed_reuse_doc = progress.get("pilot_completed_training_reuse")
    if completed_reuse_doc is not None:
        completed_reuse = _load_completed_package_training_reuse(
            backend, identity=identity, current_seed_table=seed_table,
            c_outcome_reuse=c_outcome_reuse,
        )
        completed_reuse_path = package_root / "pilot-completed-training-reuse.json"
        actor_c_reuse_path = package_root / "pilot-C-actor-update-reuse.json"
        if (
            not completed_reuse_path.is_file()
            or read_json(completed_reuse_path) != completed_reuse["document"]
            or file_sha256(completed_reuse_path) != progress.get("pilot_completed_training_reuse_sha256")
            or completed_reuse_doc != completed_reuse["document"]
            or not actor_c_reuse_path.is_file()
            or read_json(actor_c_reuse_path) != completed_reuse["actor_update_reuse_document"]
            or progress.get("actor_update_reuse_by_arm", {}).get("C") != completed_reuse["actor_update_reuse_document"]
            or file_sha256(actor_c_reuse_path) != progress.get("actor_update_reuse_by_arm_sha256", {}).get("C")
            or progress.get("actor_updates", {}).get("C") != completed_reuse["actor_update_receipt"]
            or progress.get("training_runs", {}).get("C") != completed_reuse["summaries"].get("C")
            or progress.get("training_runs", {}).get("D") != completed_reuse["summaries"].get("D")
        ):
            raise RuntimeError("successful pilot lacks the immutable C/D completed-package receipt references")
        completed_reuse_roots_by_arm = completed_reuse["receipt_roots_by_arm"]
    elif progress.get("actor_update_reuse_by_arm"):
        raise RuntimeError("pilot has carried actor updates without a completed-package training reuse record")
    clock_reconciliation = progress.get("clock_reconciliation")
    clock_reconciliation_sha256 = progress.get("clock_reconciliation_sha256")
    if completed_reuse is not None:
        clock_reconciliation_path = package_root / "pilot-progress-clock-reconciliation.json"
        if (
            not isinstance(clock_reconciliation, dict)
            or not clock_reconciliation_path.is_file()
            or read_json(clock_reconciliation_path) != clock_reconciliation
            or file_sha256(clock_reconciliation_path) != clock_reconciliation_sha256
            or clock_reconciliation.get("predecessor_progress_sha256") != completed_reuse["progress_sha256"]
            or clock_reconciliation.get("predecessor_package_commit") != completed_reuse["source_commit"]
            or float(progress.get("cumulative_active_wall_seconds", -1.0))
            < float(clock_reconciliation.get("shared_clock_active_wall_seconds_before_resume", float("inf")))
        ):
            raise RuntimeError("successful pilot progress lacks the immutable shared-clock reconciliation")
        predecessor_wall = completed_reuse["progress"]["cumulative_active_wall_seconds"]
        _validate_pilot_clock_reconciliation(
            clock_reconciliation, identity=identity, completed_reuse=completed_reuse,
            prior_wall=float(predecessor_wall),
        )
        if float(progress.get("cumulative_active_wall_seconds", -1.0)) < _pilot_task_clock_high_water(
            progress, completed_reuse["progress"],
        ):
            raise RuntimeError("successful pilot cumulative clock is below a persisted task-clock high-water")
    elif clock_reconciliation is not None or clock_reconciliation_sha256 is not None:
        raise RuntimeError("pilot contains an unrelated clock-reconciliation receipt")
    expected_training = set(NEW_ARMS)
    expected_evaluation = set(POLICIES)
    expected_updates = set(NEW_ARMS)
    training = report.get("training_runs")
    evaluation = report.get("evaluation_runs")
    updates = report.get("actor_updates")
    if not all(isinstance(value, dict) for value in (training, evaluation, updates)):
        raise RuntimeError("successful pilot report lacks its training/evaluation/update ledgers")
    if set(training) != expected_training or set(evaluation) != expected_evaluation or set(updates) != expected_updates:
        raise RuntimeError("successful pilot report does not contain every registered pilot group")
    for group in training.values():
        if group.get("status") != "SUCCEEDED" or group.get("expected") != len(PILOT_OWNERS) or group.get("successful_completions") != len(PILOT_OWNERS):
            raise RuntimeError("successful pilot report contains an incomplete training group")
    for group in evaluation.values():
        if group.get("status") != "SUCCEEDED" or group.get("expected") != len(PILOT_OWNERS) or group.get("successful_completions") != len(PILOT_OWNERS):
            raise RuntimeError("successful pilot report contains an incomplete evaluation group")
    for update in updates.values():
        if update.get("status") != "SUCCEEDED" or update.get("completed_steps") != ACTOR_STEPS or update.get("owner_count") != len(PILOT_OWNERS) or update.get("loss_summary", {}).get("finite_all_steps") is not True:
            raise RuntimeError("successful pilot report contains an incomplete/non-finite actor update")
    for groups in (progress.get("training_runs", {}), progress.get("evaluation_runs", {})):
        for group in groups.values():
            if group.get("status") != "SUCCEEDED" or group.get("expected") != len(PILOT_OWNERS) or group.get("successful_completions") != len(PILOT_OWNERS):
                raise RuntimeError("durable progress ledger contains an incomplete pilot game group")
    c_rows = [row for row in seed_rows if row.get("phase") == "training" and row.get("issue85_arm") == "C"]
    c_summary = progress["training_runs"]["C"]
    if completed_reuse is None:
        current_c_attempt_counts = {
            str(row["unit_id"]): len(list((package_root / "training" / "C" / "units" / backend.unit_key(row["unit_id"])).glob("attempt-*")))
            for row in c_rows
        }
    else:
        current_c_attempt_counts = dict(completed_reuse["summaries"]["C"]["attempt_count_by_owner"])
    if (
        c_summary.get("carried_attempt_count_by_owner")
        != c_outcome_document.get("prior_C_cumulative_attempt_count_by_owner")
        or c_summary.get("attempt_count_by_owner") != current_c_attempt_counts
        or c_summary.get("successful_completions") != len(c_rows)
        or c_summary.get("successful_unit_ids") != sorted(str(row["unit_id"]) for row in c_rows)
    ):
        raise RuntimeError("successful pilot report C attempt counts differ from the immutable receipt references")
    for update in progress.get("actor_updates", {}).values():
        if update.get("status") != "SUCCEEDED" or update.get("completed_steps") != ACTOR_STEPS:
            raise RuntimeError("durable progress ledger contains an incomplete actor update")
    if (
        report.get("schema_id") != "issue85-pilot-report-v1"
        or report.get("status") != "SUCCEEDED"
        or report.get("package_commit") != identity["package_commit"]
        or report.get("source_lock_sha256") != identity["source_lock_sha256"]
        or report.get("pilot_owner_ids") != list(PILOT_OWNERS)
        or report.get("training_game_count") != len(NEW_ARMS) * len(PILOT_OWNERS)
        or report.get("new_training_game_count") != (
            len(NEW_ARMS) * len(PILOT_OWNERS)
            - len(PILOT_OWNERS)
            - int(c_outcome_document["prior_C_successful_games"])
            - (0 if completed_reuse is None else int(completed_reuse["document"]["successful_game_count"]))
        )
        or report.get("reused_predecessor_training_game_count") != (
            len(PILOT_OWNERS) + int(c_outcome_document["prior_C_successful_games"])
            + (0 if completed_reuse is None else int(completed_reuse["document"]["successful_game_count"]))
        )
        or report.get("reused_predecessor_C_training_game_count") != (
            int(c_outcome_document["prior_C_successful_games"])
            + (0 if completed_reuse is None else int(completed_reuse["document"]["C_successful_game_count"]))
        )
        or report.get("new_evaluation_game_count") != len(POLICIES) * len(PILOT_OWNERS)
        or report.get("unique_pilot_game_count") != len(NEW_ARMS) * len(PILOT_OWNERS) + len(POLICIES) * len(PILOT_OWNERS)
        or report.get("evaluation_game_count") != len(POLICIES) * len(PILOT_OWNERS)
        or report.get("formal_games_started") != 0
        or report.get("formal_cohorts_changed") is not False
        or report.get("pilot_weights_used_formally") is not False
        or report.get("baseline_verification", {}).get("status") != "VERIFIED_NO_NEW_GAMES"
        or report.get("completed_training_reuse") != completed_reuse_doc
        or report.get("completed_training_reuse_sha256") != (
            None if completed_reuse is None else progress.get("pilot_completed_training_reuse_sha256")
        )
        or report.get("clock_reconciliation") != clock_reconciliation
        or report.get("clock_reconciliation_sha256") != clock_reconciliation_sha256
    ):
        raise RuntimeError("successful pilot report identity/count/scope validation failed")
    if (
        progress.get("status") != "SUCCEEDED"
        or progress.get("package_commit") != identity["package_commit"]
        or progress.get("source_lock_sha256") != identity["source_lock_sha256"]
        or progress.get("seed_table_sha256") != seed_table_sha256
        or set(progress.get("training_runs", {})) != expected_training
        or set(progress.get("evaluation_runs", {})) != expected_evaluation
        or set(progress.get("actor_updates", {})) != expected_updates
    ):
        raise RuntimeError("successful pilot report has no matching completed durable progress ledger")
    successful_ids: dict[str, list[str]] = {arm: [] for arm in NEW_ARMS}
    successful_eval_ids: dict[str, list[str]] = {policy: [] for policy in POLICIES}
    for row in seed_rows:
        stage = "training" if row["phase"] == "training" else "evaluation"
        group = str(row["issue85_arm"] if stage == "training" else row["arm"])
        if stage == "training" and group == "B":
            root = predecessor["receipt_root"]
            receipt_commit = predecessor["source_commit"]
        elif stage == "training" and group == "C" and str(row["unit_id"]) in c_outcome_reuse["success_receipt_roots_by_owner"]:
            root, receipt_commit = c_outcome_reuse["success_receipt_roots_by_owner"][str(row["unit_id"])]
        elif stage == "training" and group in completed_reuse_roots_by_arm and str(row["unit_id"]) in completed_reuse_roots_by_arm[group]:
            root, receipt_commit = completed_reuse_roots_by_arm[group][str(row["unit_id"])]
        else:
            root = package_root / stage / group
            receipt_commit = identity["package_commit"]
        if pilot_success_record(
            backend, root=root, row=row, package_commit=receipt_commit,
        ) is None:
            raise RuntimeError(f"pilot report owner {row['unit_id']} lacks a fully validated success receipt")
        (successful_ids if stage == "training" else successful_eval_ids)[group].append(str(row["unit_id"]))
    for arm, summary in training.items():
        if sorted(successful_ids[arm]) != summary.get("successful_unit_ids"):
            raise RuntimeError(f"pilot report training owner list differs from retained receipts for {arm}")
    for policy, summary in evaluation.items():
        if sorted(successful_eval_ids[policy]) != summary.get("successful_unit_ids"):
            raise RuntimeError(f"pilot report evaluation owner list differs from retained receipts for {policy}")
    forecast_path = (package_root / str(report.get("forecast_path", ""))).resolve()
    if (
        package_root.resolve() not in forecast_path.parents
        or not forecast_path.is_file()
        or file_sha256(forecast_path) != report.get("forecast_sha256")
    ):
        raise RuntimeError("successful pilot report has a missing/mismatched forecast artifact")
    forecast = read_json(forecast_path)
    if (
        forecast.get("package_commit") != identity["package_commit"]
        or forecast.get("source_lock_sha256") != identity["source_lock_sha256"]
        or forecast.get("status") != "PARTIAL_COMPONENT_PROJECTION_NOT_AN_ALL_IN_ESTIMATE_OR_CAP"
        or forecast.get("all_in_study_wall_estimate_seconds") is not None
        or forecast.get("formal_games") != 7808
    ):
        raise RuntimeError("successful pilot report's forecast is not the bounded partial projection")


def run_pilot(
    issue84_source: Path, *, resume_from: str | None = None,
    reuse_completed_package: str | None = None,
) -> dict[str, Any]:
    identity = source_identity(require_clean=True)
    issue84_root = validate_issue84_source(issue84_source)
    python_path = _path_from_wsl("/mnt/d/DeadlyCatCoding/stsrl-data/stsrl-spikes/py313-torch/bin/python3.13")
    expected_python_sha = LOCK["model_runtime"]["python_sha256"]
    if not python_path.is_file() or file_sha256(python_path) != expected_python_sha:
        raise RuntimeError("pinned Python 3.13 runtime is missing or its bytes differ from the source lock")
    package_root = PILOT_ROOT / identity["package_commit"]
    package_root.mkdir(parents=True, exist_ok=True)
    backend, issue84_driver = _configure_runtime(issue84_root, package_root, identity)
    baseline = _verify_baselines(backend, issue84_driver, package_root)
    rows_document = build_seed_rows(backend)
    seed_path = package_root / "pilot-seed-table.json"
    seed_hash = _write_or_verify_once(seed_path, rows_document)
    report_path = package_root / "pilot-report.json"
    progress_path = package_root / "pilot-progress.json"
    if report_path.is_file():
        report = read_json(report_path)
        if not progress_path.is_file():
            raise RuntimeError("successful pilot report exists without its durable progress ledger")
        progress = read_json(progress_path)
        _validate_completed_pilot(
            report, progress, package_root=package_root, identity=identity,
            backend=backend, seed_table=rows_document, seed_table_sha256=seed_hash,
        )
        return report

    progress_exists = progress_path.is_file()
    if progress_exists:
        progress = read_json(progress_path)
        if progress.get("package_commit") != identity["package_commit"] or progress.get("source_lock_sha256") != identity["source_lock_sha256"] or progress.get("seed_table_sha256") != seed_hash:
            raise RuntimeError("pilot progress is bound to another source, lock or fixed seed table")
        recorded_predecessor = progress.get("predecessor_training_reuse", {}).get("predecessor_package_commit")
        if not recorded_predecessor or (resume_from is not None and resume_from != recorded_predecessor):
            raise RuntimeError("resumed pilot must preserve its already-recorded predecessor training evidence")
        predecessor_commit = str(recorded_predecessor)
    else:
        if resume_from != PILOT_RESUME_PREDECESSOR_SHA:
            raise RuntimeError(
                "this Issue 85 pilot continuation requires --resume-from "
                f"{PILOT_RESUME_PREDECESSOR_SHA}; refusing to replay completed B owners"
            )
        predecessor_commit = resume_from
    if predecessor_commit != PILOT_RESUME_PREDECESSOR_SHA:
        raise RuntimeError("Issue 85 pilot continuation changed its immutable B-game predecessor")
    predecessor = _load_predecessor_b_training(
        backend,
        identity=identity,
        current_seed_table=rows_document,
        predecessor_commit=predecessor_commit,
    )
    predecessor_document = _predecessor_receipt_document(predecessor, current_identity=identity)
    predecessor_manifest_path = package_root / "pilot-predecessor-training-reuse.json"
    actor_update_reuse = _load_predecessor_b_actor_update(
        backend,
        identity=identity,
        current_seed_table=rows_document,
        b_game_predecessor=predecessor,
    )
    actor_reuse_document = actor_update_reuse["document"]
    actor_reuse_path = package_root / "pilot-actor-update-reuse.json"
    c_outcome_reuse = _load_predecessor_c_training_outcomes(
        backend, identity=identity, current_seed_table=rows_document,
    )
    c_outcome_document = c_outcome_reuse["document"]
    c_outcome_path = package_root / "pilot-C-training-outcome-reuse.json"
    if c_outcome_document.get("prior_C_inherited_failed_attempt_receipts") != actor_reuse_document.get("prior_C_failed_attempts"):
        raise RuntimeError("C outcome reuse does not preserve the independently validated original failure receipts")
    recorded_completed_package = None
    if progress_exists:
        recorded_completed_package = _recorded_completed_package(progress)
        if reuse_completed_package is not None and reuse_completed_package != recorded_completed_package:
            raise RuntimeError("resumed pilot must preserve its already-recorded completed-package evidence")
    completed_package_commit = recorded_completed_package or reuse_completed_package
    if completed_package_commit is not None and completed_package_commit != PILOT_COMPLETED_TRAINING_CARRY_FORWARD_SHA:
        raise RuntimeError("Issue 85 continuation names an unregistered completed training package")
    completed_reuse = None
    completed_reuse_path = package_root / "pilot-completed-training-reuse.json"
    actor_c_reuse_path = package_root / "pilot-C-actor-update-reuse.json"
    if completed_package_commit is not None:
        completed_reuse = _load_completed_package_training_reuse(
            backend, identity=identity, current_seed_table=rows_document,
            c_outcome_reuse=c_outcome_reuse,
        )
    completed_reuse_document = None if completed_reuse is None else completed_reuse["document"]
    actor_c_reuse_document = None if completed_reuse is None else completed_reuse["actor_update_reuse_document"]
    if progress_exists:
        if (
            not predecessor_manifest_path.is_file()
            or read_json(predecessor_manifest_path) != predecessor_document
            or progress.get("predecessor_training_reuse") != predecessor_document
            or progress.get("predecessor_training_reuse_sha256") != file_sha256(predecessor_manifest_path)
            or not actor_reuse_path.is_file()
            or read_json(actor_reuse_path) != actor_reuse_document
            or progress.get("actor_update_reuse") != actor_reuse_document
            or progress.get("actor_update_reuse_sha256") != file_sha256(actor_reuse_path)
            or progress.get("actor_updates", {}).get("B") != actor_update_reuse["receipt"]
            or not c_outcome_path.is_file()
            or read_json(c_outcome_path) != c_outcome_document
            or progress.get("pilot_C_outcome_reuse") != c_outcome_document
            or progress.get("pilot_C_outcome_reuse_sha256") != file_sha256(c_outcome_path)
            or (
                completed_reuse is not None
                and (
                    not completed_reuse_path.is_file()
                    or read_json(completed_reuse_path) != completed_reuse_document
                    or progress.get("pilot_completed_training_reuse") != completed_reuse_document
                    or progress.get("pilot_completed_training_reuse_sha256") != file_sha256(completed_reuse_path)
                    or not actor_c_reuse_path.is_file()
                    or read_json(actor_c_reuse_path) != actor_c_reuse_document
                    or progress.get("actor_update_reuse_by_arm", {}).get("C") != actor_c_reuse_document
                    or progress.get("actor_update_reuse_by_arm_sha256", {}).get("C") != file_sha256(actor_c_reuse_path)
                    or progress.get("actor_updates", {}).get("C") != completed_reuse["actor_update_receipt"]
                    or progress.get("training_runs", {}).get("C") != completed_reuse["summaries"].get("C")
                    or progress.get("training_runs", {}).get("D") != completed_reuse["summaries"].get("D")
                )
            )
            or (completed_reuse is None and progress.get("pilot_completed_training_reuse") is not None)
        ):
            raise RuntimeError("pilot continuation predecessor game/update/C-outcome references are missing or changed")
    else:
        predecessor_manifest_sha = _write_or_verify_once(predecessor_manifest_path, predecessor_document)
        actor_reuse_sha = _write_or_verify_once(actor_reuse_path, actor_reuse_document)
        c_outcome_sha = _write_or_verify_once(c_outcome_path, c_outcome_document)
        completed_reuse_sha = None
        actor_c_reuse_sha = None
        if completed_reuse is not None:
            completed_reuse_sha = _write_or_verify_once(completed_reuse_path, completed_reuse_document)
            actor_c_reuse_sha = _write_or_verify_once(actor_c_reuse_path, actor_c_reuse_document)
        training_runs = {"B": predecessor["summary"]}
        actor_updates = {"B": actor_update_reuse["receipt"]}
        actor_update_reuse_by_arm: dict[str, Any] = {}
        actor_update_reuse_by_arm_sha256: dict[str, str] = {}
        if completed_reuse is not None:
            training_runs.update(completed_reuse["summaries"])
            actor_updates["C"] = completed_reuse["actor_update_receipt"]
            actor_update_reuse_by_arm["C"] = actor_c_reuse_document
            actor_update_reuse_by_arm_sha256["C"] = str(actor_c_reuse_sha)
        progress = {
            "schema_id": "issue85-pilot-progress-v1",
            "status": "READY",
            "package_commit": identity["package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "issue84_source_commit": ISSUE84_ACCEPTED_COMMIT,
            "issue84_source_root": str(issue84_root),
            "seed_table_sha256": seed_hash,
            "seed_table_rows": len(rows_document["rows"]),
            "baseline_verification": baseline,
            "training_runs": training_runs,
            "predecessor_training_reuse": predecessor_document,
            "predecessor_training_reuse_sha256": predecessor_manifest_sha,
            "actor_updates": actor_updates,
            "actor_update_reuse": actor_reuse_document,
            "actor_update_reuse_sha256": actor_reuse_sha,
            "pilot_C_outcome_reuse": c_outcome_document,
            "pilot_C_outcome_reuse_sha256": c_outcome_sha,
            "pilot_completed_training_reuse": completed_reuse_document,
            "pilot_completed_training_reuse_sha256": completed_reuse_sha,
            "actor_update_reuse_by_arm": actor_update_reuse_by_arm,
            "actor_update_reuse_by_arm_sha256": actor_update_reuse_by_arm_sha256,
            "evaluation_runs": {},
            "concurrency": int(backend.CONCURRENCY),
            "run_history": [],
        }
        backend.atomic_json(progress_path, progress)
    if progress.get("predecessor_training_reuse") != predecessor_document:
        raise RuntimeError("pilot progress has no immutable B-game predecessor reference")
    old_status = progress.get("status")
    if old_status == "RUNNING":
        prior_pid = int(progress.get("supervisor_pid", -1))
        if prior_pid > 0:
            try:
                os.kill(prior_pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise RuntimeError(f"cannot verify prior pilot supervisor PID {prior_pid}; refusing a duplicate run") from exc
            else:
                raise RuntimeError(f"pilot supervisor PID {prior_pid} is still alive; refusing a duplicate run")
        progress.setdefault("run_history", []).append({
            "status": "INTERRUPTED",
            "active_stage": progress.get("active_stage"),
            "supervisor_pid": prior_pid,
            "observed_utc": datetime.now(timezone.utc).isoformat(),
        })
    clock = backend.ActiveClock("issue85-task", None)
    before = clock.elapsed_before
    if completed_reuse is not None:
        try:
            _reconcile_pilot_task_clock(
                package_root, progress_path, progress,
                identity=identity, completed_reuse=completed_reuse, before=before,
                atomic_json=backend.atomic_json,
            )
        except BaseException:
            clock.close()
            raise
    progress.update({
        "status": "RUNNING",
        "supervisor_pid": os.getpid(),
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "clock_elapsed_before": before,
    })
    backend.atomic_json(progress_path, progress)
    try:
        os.environ["ISSUE85_PILOT_ROOT"] = str(package_root)
        os.environ["ISSUE84_SOURCE_DIR"] = str(issue84_root)

        def start_game_stage(stage: str, arm: str, rows: Sequence[Mapping[str, Any]]) -> tuple[float, float, Any]:
            stage_clock_start = clock.elapsed()
            stage_base = stage_resume_wall_base(
                progress, stage=stage, arm=arm, current_clock_elapsed=stage_clock_start,
            )
            progress["active_stage"] = stage
            progress["active_arm"] = arm
            progress["live_stage_progress"] = {
                "stage": stage,
                "arm": arm,
                "expected": len(rows),
                "successful_completions": 0,
                "cumulative_active_wall_seconds": stage_base,
                "clock_elapsed_seconds": stage_clock_start,
                "updated_utc": datetime.now(timezone.utc).isoformat(),
            }
            backend.atomic_json(progress_path, progress)

            def record_live_stage(snapshot: Mapping[str, Any]) -> None:
                now = clock.elapsed()
                progress["live_stage_progress"] = {
                    **dict(snapshot),
                    "stage": stage,
                    "arm": arm,
                    "cumulative_active_wall_seconds": stage_base + max(0.0, now - stage_clock_start),
                    "clock_elapsed_seconds": now,
                    "updated_utc": datetime.now(timezone.utc).isoformat(),
                }
                backend.atomic_json(progress_path, progress)

            return stage_clock_start, stage_base, record_live_stage

        def preserve_stage_summary(stage: str, arm: str, summary: Mapping[str, Any] | None) -> None:
            if summary is not None:
                progress.setdefault("stage_summary_history", []).append({
                    "stage": stage,
                    "arm": arm,
                    "observed_utc": datetime.now(timezone.utc).isoformat(),
                    "summary": dict(summary),
                })

        for arm in NEW_ARMS:
            rows = [row for row in rows_document["rows"] if row["phase"] == "training" and row["issue85_arm"] == arm]
            root = package_root / "training" / arm
            prior_training = progress["training_runs"].get(arm)
            reused_receipt_roots = dict(c_outcome_reuse["success_receipt_roots_by_owner"]) if arm == "C" else {}
            reused_receipt_roots.update(completed_reuse["receipt_roots_by_arm"].get(arm, {}) if completed_reuse is not None else {})
            if arm == "B":
                training_complete = stage_summary_is_complete(
                    prior_training, len(rows), [str(row["unit_id"]) for row in rows],
                ) and stage_receipts_are_complete(
                    backend, root=predecessor["receipt_root"], rows=rows,
                    package_commit=predecessor["source_commit"],
                )
            else:
                training_complete = stage_summary_is_complete(
                    prior_training, len(rows), [str(row["unit_id"]) for row in rows],
                ) and stage_receipts_are_complete(
                    backend, root=root, rows=rows, package_commit=identity["package_commit"],
                    receipt_roots_by_owner=reused_receipt_roots or None,
                )
            if not training_complete:
                if arm in (completed_reuse["receipt_roots_by_arm"] if completed_reuse is not None else {}):
                    raise RuntimeError(f"immutable completed-package {arm} training reuse failed validation; refusing to rerun owners")
                preserve_stage_summary("training-games", arm, prior_training)
                stage_clock_start, stage_base, record_live_stage = start_game_stage("training-games", arm, rows)
                attempt_offsets = (
                    _carried_c_attempt_counts(rows, actor_reuse_document, c_outcome_document)
                    if arm == "C" else {}
                )
                result = run_units_bounded(
                    backend, rows, root=root, clock=clock,
                    package_commit=identity["package_commit"], progress_callback=record_live_stage,
                    initial_attempt_count_by_owner=attempt_offsets,
                    prior_success_receipt_roots_by_owner=(
                        c_outcome_reuse["success_receipt_roots_by_owner"] if arm == "C" else None
                    ),
                )
                summary = summarize_stage(result, rows)
                summary = stage_summary_with_cumulative_wall(
                    summary, stage_base=stage_base, stage_clock_start=stage_clock_start,
                    current_clock_elapsed=clock.elapsed(),
                )
                progress["training_runs"][arm] = summary
                backend.atomic_json(progress_path, progress)
                if not stage_summary_is_complete(summary, len(rows), [str(row["unit_id"]) for row in rows]):
                    raise RuntimeError(f"Issue 85 pilot training games for {arm} stopped with {summary['status']}: {summary['successful_completions']}/{summary['expected']}")

            if arm == "B":
                if (
                    actor_update_reuse["receipt"] != progress.get("actor_updates", {}).get("B")
                    or progress.get("actor_update_reuse") != actor_reuse_document
                ):
                    raise RuntimeError("completed B actor update is not bound to the immutable carry-forward record")
                continue
            if arm in progress.get("actor_update_reuse_by_arm", {}):
                expected_receipt = (completed_reuse or {}).get("actor_update_receipt")
                expected_manifest = (completed_reuse or {}).get("actor_update_reuse_document")
                if (
                    expected_receipt != progress.get("actor_updates", {}).get(arm)
                    or expected_manifest != progress.get("actor_update_reuse_by_arm", {}).get(arm)
                    or progress.get("actor_update_reuse_by_arm_sha256", {}).get(arm)
                    != file_sha256(package_root / f"pilot-{arm}-actor-update-reuse.json")
                ):
                    raise RuntimeError(f"completed {arm} actor update is not bound to its immutable carry-forward record")
                continue

            actor_resume_clock = clock.elapsed()
            progress["active_stage"] = "pilot-actor-update"
            progress["active_arm"] = arm
            progress["live_stage_progress"] = {
                "stage": "pilot-actor-update",
                "arm": arm,
                "clock_elapsed_seconds": actor_resume_clock,
                "updated_utc": datetime.now(timezone.utc).isoformat(),
            }
            backend.atomic_json(progress_path, progress)

            def record_actor_update(snapshot: Mapping[str, Any], *, selected_arm: str = arm) -> None:
                progress["live_stage_progress"] = {
                    **dict(snapshot),
                    "stage": "pilot-actor-update",
                    "arm": selected_arm,
                    "clock_elapsed_seconds": clock.elapsed(),
                    "updated_utc": datetime.now(timezone.utc).isoformat(),
                }
                backend.atomic_json(progress_path, progress)

            receipt = _train_pilot_arm(
                backend, issue84_driver, arm=arm, rows=rows, root=root,
                pilot_root=package_root, identity=identity,
                examples_root=predecessor["receipt_root"] if arm == "B" else None,
                examples_package_commit=predecessor["source_commit"] if arm == "B" else None,
                example_receipt_roots_by_owner=reused_receipt_roots or None,
                active_clock=clock, resume_clock_elapsed=actor_resume_clock,
                progress_callback=record_actor_update,
            )
            if receipt.get("status") != "SUCCEEDED" or receipt.get("completed_steps") != ACTOR_STEPS:
                raise RuntimeError(f"Issue 85 actor update for {arm} did not complete all {ACTOR_STEPS} steps")
            progress["actor_updates"][arm] = receipt
            backend.atomic_json(progress_path, progress)

        for policy in POLICIES:
            rows = [row for row in rows_document["rows"] if row["phase"] == "evaluation" and row["arm"] == policy]
            root = package_root / "evaluation" / policy
            prior_evaluation = progress["evaluation_runs"].get(policy)
            if stage_summary_is_complete(
                prior_evaluation, len(rows), [str(row["unit_id"]) for row in rows],
            ) and stage_receipts_are_complete(
                backend, root=root, rows=rows, package_commit=identity["package_commit"],
            ):
                continue
            preserve_stage_summary("evaluation-games", policy, prior_evaluation)
            stage_clock_start, stage_base, record_live_stage = start_game_stage("evaluation-games", policy, rows)
            result = run_units_bounded(
                backend, rows, root=root, clock=clock,
                package_commit=identity["package_commit"], progress_callback=record_live_stage,
            )
            summary = summarize_stage(result, rows)
            summary = stage_summary_with_cumulative_wall(
                summary, stage_base=stage_base, stage_clock_start=stage_clock_start,
                current_clock_elapsed=clock.elapsed(),
            )
            progress["evaluation_runs"][policy] = summary
            backend.atomic_json(progress_path, progress)
            if not stage_summary_is_complete(summary, len(rows)):
                raise RuntimeError(f"Issue 85 pilot evaluation for {policy} stopped with {summary['status']}: {summary['successful_completions']}/{summary['expected']}")
        clock.set_processes(set())
        task_wall = clock.elapsed()
    except BaseException as exc:
        clock.set_processes(set())
        progress["status"] = "FAILED_OR_INCOMPLETE"
        progress["failure"] = {"type": type(exc).__name__, "detail": str(exc)[:2000]}
        progress["cumulative_active_wall_seconds"] = clock.elapsed()
        progress["finished_or_stopped_utc"] = datetime.now(timezone.utc).isoformat()
        backend.atomic_json(progress_path, progress)
        raise
    finally:
        clock.close()

    progress["status"] = "SUCCEEDED"
    progress["cumulative_active_wall_seconds"] = task_wall
    progress["incremental_active_wall_seconds"] = max(0.0, task_wall - before)
    progress["finished_or_stopped_utc"] = datetime.now(timezone.utc).isoformat()
    forecast = _forecast(progress)
    forecast_path = package_root / "pilot-formal-wall-forecast.json"
    _write_once(forecast_path, forecast)
    report = {
        "schema_id": "issue85-pilot-report-v1",
        "status": "SUCCEEDED",
        "claim": "nonformal runtime/equivalence pilot only; no efficacy evidence or registered model selection",
        "package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "issue84_source_commit": ISSUE84_ACCEPTED_COMMIT,
        "issue84_source_root": str(issue84_root),
        "native_source_commit": LOCK["native"]["commit"],
        "stsr_source_commit": LOCK["stsr_source"]["commit"],
        "pilot_owner_ids": list(PILOT_OWNERS),
        "training_game_count": sum(item["successful_completions"] for item in progress["training_runs"].values()),
        "new_training_game_count": sum(
            item["successful_completions"]
            for arm, item in progress["training_runs"].items()
            if arm not in ("B", "C")
        ) - (0 if completed_reuse is None else int(completed_reuse["document"]["D_successful_game_count"])) + max(
            0,
            int(progress["training_runs"]["C"]["successful_completions"])
            - int(c_outcome_document["prior_C_successful_games"])
            - (0 if completed_reuse is None else int(completed_reuse["document"]["C_successful_game_count"])),
        ),
        "reused_predecessor_training_game_count": len(predecessor["rows"])
        + int(c_outcome_document["prior_C_successful_games"])
        + (0 if completed_reuse is None else int(completed_reuse["document"]["successful_game_count"])),
        "reused_predecessor_C_training_game_count": int(c_outcome_document["prior_C_successful_games"])
        + (0 if completed_reuse is None else int(completed_reuse["document"]["C_successful_game_count"])),
        "evaluation_game_count": sum(item["successful_completions"] for item in progress["evaluation_runs"].values()),
        "new_evaluation_game_count": sum(
            item["successful_completions"]
            for item in progress["evaluation_runs"].values()
        ),
        "unique_pilot_game_count": sum(
            item["successful_completions"] for item in progress["training_runs"].values()
        ) + sum(item["successful_completions"] for item in progress["evaluation_runs"].values()),
        "training_runs": progress["training_runs"],
        "predecessor_training_reuse": progress["predecessor_training_reuse"],
        "actor_update_reuse": progress["actor_update_reuse"],
        "actor_update_reuse_sha256": progress["actor_update_reuse_sha256"],
        "C_outcome_reuse": c_outcome_document,
        "C_outcome_reuse_sha256": file_sha256(c_outcome_path),
        "pilot_C_outcome_reuse": progress["pilot_C_outcome_reuse"],
        "pilot_C_outcome_reuse_sha256": progress["pilot_C_outcome_reuse_sha256"],
        "completed_training_reuse": progress.get("pilot_completed_training_reuse"),
        "completed_training_reuse_sha256": progress.get("pilot_completed_training_reuse_sha256"),
        "clock_reconciliation": progress.get("clock_reconciliation"),
        "clock_reconciliation_sha256": progress.get("clock_reconciliation_sha256"),
        "actor_updates": progress["actor_updates"],
        "evaluation_runs": progress["evaluation_runs"],
        "baseline_verification": progress["baseline_verification"],
        "formal_games_started": 0,
        "formal_cohorts_changed": False,
        "pilot_weights_used_formally": False,
        "pilot_weights_retained_under": "issue85/artifacts/pilot/" + identity["package_commit"],
        "active_physical_wall_seconds": task_wall,
        "forecast_path": forecast_path.relative_to(package_root).as_posix(),
        "forecast_sha256": file_sha256(forecast_path),
        "forecast": forecast,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
    }
    backend.atomic_json(progress_path, progress)
    _write_once(report_path, report)
    _validate_completed_pilot(
        report, progress, package_root=package_root, identity=identity,
        backend=backend, seed_table=rows_document, seed_table_sha256=seed_hash,
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan", action="store_true", help="print the fixed pilot counts without running games")
    group.add_argument("--pilot", action="store_true", help="run the authorized nonformal pilot")
    group.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--issue84-source", type=Path, default=os.environ.get("ISSUE84_SOURCE_DIR"))
    parser.add_argument("--resume-from", help="reuse strict completed pilot receipts from the exact registered predecessor source")
    parser.add_argument(
        "--reuse-completed-package",
        help="reference the exact registered failed package's completed C/D pilot terminals without replaying them",
    )
    args = parser.parse_args(argv)
    if args.plan:
        print(json.dumps(
            pilot_plan(resume_from=args.resume_from, reuse_completed_package=args.reuse_completed_package),
            sort_keys=True,
        ))
        return 0

    if args.issue84_source is None:
        parser.error("--issue84-source or ISSUE84_SOURCE_DIR is required")
    if args.worker is not None:
        identity = source_identity(require_clean=True)
        job = read_json(args.worker)
        if job.get("package_commit") != identity["package_commit"] or job.get("source_lock_sha256") != identity["source_lock_sha256"]:
            raise RuntimeError("worker job does not match the exact Issue 85 source/lock")
        issue84_root = validate_issue84_source(Path(args.issue84_source))
        pilot_root = Path(os.environ["ISSUE85_PILOT_ROOT"]).resolve(strict=True)
        backend, _driver = _configure_runtime(issue84_root, pilot_root, identity)
        token = _CURRENT_ROW.set(job["row"])
        try:
            return int(backend.worker_main(args.worker))
        finally:
            _CURRENT_ROW.reset(token)

    identity = source_identity(require_clean=True)
    result = run_pilot(
        Path(args.issue84_source), resume_from=args.resume_from,
        reuse_completed_package=args.reuse_completed_package,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
