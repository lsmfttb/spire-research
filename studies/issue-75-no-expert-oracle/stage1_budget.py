"""Stage 1 source binding, CPU ledger, and Linux process-tree watchdog."""
from __future__ import annotations
from dataclasses import dataclass
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import signal
import subprocess
import threading
import time
from typing import Any, Mapping, Sequence

BUILDER_LIMIT = 170_000
REVIEWER_LIMIT = 10_000
ALL_IN_LIMIT = 180_000
SEARCH_CPU_CAP_SECONDS = 32.0
SEARCH_ATTEMPT_CAP = 16
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
STUDY = Path(__file__).resolve().parent
STAGE1 = STUDY / "stage1"
LOCKED_PACKAGE_FILES = (
    "config.json",
    "algorithm.py",
    "learning.py",
    "model.py",
    "native_negative.py",
    "preflight.py",
    "runtime.py",
    "stage0.py",
    "test_issue75_contract.py",
    "planning/build_stage1_cost_model.py",
    "planning/stage1-cost-model.json",
    "planning/stage1-cost-model.md",
    "stage1_budget.py",
    "stage1/config.json",
    "stage1/preflight-proxy.json",
    "stage1/seed-table.json",
    "stage1/budget.json",
    "stage1/prepare.py",
    "stage1/executor.py",
    "stage1/test_activation.py",
    "stage1/test_executor.py",
    "stage1/block0-repair-lineage.json",
)
LOCKED_REPOSITORY_DEPENDENCIES = (
    "studies/issue-73-privileged-noncombat-oracle-stage0/runner.py",
    "studies/issue-73-privileged-noncombat-oracle-stage0/policy.py",
)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def normalized_file_sha256(path: Path) -> str:
    return _normalized_file_sha256(path)


def verify_stage1_package() -> dict[str, Any]:
    """Verify the registered source package and its explicit executable phase set."""
    lock_path = STAGE1 / "source-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if (
        lock.get("schema_id") != "issue75-stage1-source-lock-v1"
        or lock.get("study_id") != "issue-75-no-expert-oracle"
        or lock.get("scope") != "STUDY_ONLY"
        or lock.get("execution_authorized") is not True
        or lock.get("package_status") not in {
            "PREFLIGHT_ONLY_ENABLED", "FORMAL_TRAINING_ENABLED", "FORMAL_EXECUTION_ENABLED",
        }
        or lock.get("formal_seeds_or_games_consumed") != 0
        or lock.get("native_sampling_performed") is not False
        or lock.get("model_training_performed") is not False
    ):
        raise PermissionError("Stage 1 source lock is not a valid registered executable package")
    files = lock.get("locked_files_sha256_lf")
    if not isinstance(files, dict) or set(files) != set(LOCKED_PACKAGE_FILES):
        raise PermissionError("Stage 1 lock does not cover the exact registered package file set")
    for relative, expected in files.items():
        path = (STUDY / relative).resolve()
        if not path.is_relative_to(STUDY.resolve()) or not path.is_file():
            raise PermissionError(f"locked Stage 1 package file is unavailable: {relative}")
        if not isinstance(expected, str) or _normalized_file_sha256(path) != expected:
            raise PermissionError(f"Stage 1 package source lock mismatch: {relative}")

    repository_dependencies = lock.get("locked_repository_dependencies_sha256_lf")
    if not isinstance(repository_dependencies, dict) or set(repository_dependencies) != set(LOCKED_REPOSITORY_DEPENDENCIES):
        raise PermissionError("Stage 1 lock does not cover the exact Issue 73 runtime dependencies")
    repository_root = STUDY.parents[1].resolve()
    for relative, expected in repository_dependencies.items():
        path = (repository_root / relative).resolve()
        if not path.is_relative_to(repository_root) or not path.is_file():
            raise PermissionError(f"Stage 1 repository dependency is unavailable: {relative}")
        if not isinstance(expected, str) or _normalized_file_sha256(path) != expected:
            raise PermissionError(f"Stage 1 repository dependency source-lock mismatch: {relative}")

    config = json.loads((STAGE1 / "config.json").read_text(encoding="utf-8"))
    execution = config.get("execution")
    if not isinstance(execution, dict):
        raise PermissionError("Stage 1 package has no execution phase declaration")
    stage1_enabled = execution.get("stage1_execution_authorized")
    preflight_enabled = execution.get("nonformal_preflight_enabled")
    native_enabled = execution.get("native_sampling_enabled")
    training_enabled = execution.get("formal_training_enabled")
    evaluation_enabled = execution.get("formal_evaluation_enabled")
    if any(type(value) is not bool for value in (
        stage1_enabled, preflight_enabled, native_enabled, training_enabled, evaluation_enabled,
    )):
        raise PermissionError("Stage 1 execution flags must be explicit booleans")
    expected_phases = []
    if stage1_enabled and preflight_enabled and native_enabled:
        expected_phases.append("preflight")
    if stage1_enabled and training_enabled:
        expected_phases.append("train")
    if stage1_enabled and evaluation_enabled:
        expected_phases.append("evaluate")
    if not expected_phases or expected_phases[0] != "preflight" or (
        evaluation_enabled and not training_enabled
    ) or execution.get("authorized_phases") != expected_phases:
        raise PermissionError("Stage 1 package execution phase list is invalid")
    expected_status = (
        "PREFLIGHT_ONLY_ENABLED" if expected_phases == ["preflight"] else
        "FORMAL_TRAINING_ENABLED" if expected_phases == ["preflight", "train"] else
        "FORMAL_EXECUTION_ENABLED" if expected_phases == ["preflight", "train", "evaluate"] else None
    )
    if expected_status is None or execution.get("package_status") != expected_status or lock.get("package_status") != expected_status:
        raise PermissionError("Stage 1 package status does not match its enabled execution phases")

    for relative, field in (
        ("stage1/config.json", "stage1_config_sha256"),
        ("stage1/seed-table.json", "seed_table_sha256"),
        ("stage1/budget.json", "budget_config_sha256"),
    ):
        if lock.get(field) != _normalized_file_sha256(STUDY / relative):
            raise PermissionError(f"Stage 1 lock does not bind its registered input: {relative}")
    table = json.loads((STAGE1 / "seed-table.json").read_text(encoding="utf-8"))
    rows = table.get("rows", [])
    units = [row.get("unit_id") for row in rows]
    if (
        len(rows) != 1032 or len(set(units)) != 1032 or any(not unit for unit in units)
        or table.get("counts") != {
            "training_owners": 512, "heldout_owner_arm_rows": 480,
            "preflight_owner_arm_rows": 40, "preflight_arm_schedule": ["P0", "P1", "R", "J", "E"],
            "preflight_is_nonformal_and_never_enters_efficacy": True,
        }
    ):
        raise PermissionError("Stage 1 package has an incomplete or changed owner/arm schedule")
    budget = json.loads((STAGE1 / "budget.json").read_text(encoding="utf-8"))
    if (
        budget.get("stage1_execution_authorized") is not True
        or budget.get("ledger_status") != "PREFLIGHT_COMPLETE_ACCEPTED_FORMAL_PROSPECTIVE"
        or budget.get("limits_cpu_seconds") != {
            "builder": BUILDER_LIMIT, "reviewer_protected": REVIEWER_LIMIT, "all_in": ALL_IN_LIMIT,
        }
        or budget.get("reviewer_reserve_transferable_to_builder") is not False
        or budget.get("search_call_cpu_cap_seconds") != SEARCH_CPU_CAP_SECONDS
        or budget.get("search_call_started_attempt_cap") != SEARCH_ATTEMPT_CAP
        or type(budget.get("concurrent_stage1_process_trees")) is not int
        or budget.get("concurrent_stage1_process_trees") != 1
        or execution.get("max_concurrent_owner_process_trees") != 1
        or execution.get("torch_intraop_threads") != 1
        or execution.get("torch_interop_threads") != 1
    ):
        raise PermissionError("Stage 1 budget does not match the registered hard limits")
    sidecar = (STAGE1 / "source-lock.sha256").read_text(encoding="ascii").split()
    lock_digest = _normalized_file_sha256(lock_path)
    if not sidecar or sidecar[0] != lock_digest:
        raise PermissionError("Stage 1 source-lock sidecar mismatch")
    return lock


def verify_stage1_phase(phase: str) -> None:
    """Refuse any phase not explicitly enabled by the locked Stage 1 package.

    Owner jobs use canonical seed-table phases internally. CLI phase names remain
    the registered short aliases so launcher behavior and the scientific roster
    are unchanged.
    """
    phase = {"training": "train", "heldout": "evaluate"}.get(phase, phase)
    if phase not in {"preflight", "train", "evaluate", "update"}:
        raise ValueError(f"unknown Stage 1 phase: {phase}")
    config = json.loads((STAGE1 / "config.json").read_text(encoding="utf-8"))
    execution = config["execution"]
    target = "train" if phase == "update" else phase
    if target not in execution["authorized_phases"]:
        raise PermissionError(f"Stage 1 phase is not enabled by the locked package: {phase}")


@dataclass(frozen=True)
class PackageBinding:
    package_commit: str
    source_lock_sha256: str


def _current_package_binding() -> tuple[str, str]:
    """Lock executable inputs while permitting only append-only tracked ledger events."""
    verify_stage1_package()
    ledger_relative = "studies/issue-75-no-expert-oracle/stage1/artifacts/ledger/events.jsonl"
    repository_root = STUDY.parents[1].resolve(strict=True)
    ledger_path = repository_root
    parts = Path(ledger_relative).parts
    for index, part in enumerate(parts):
        ledger_path = ledger_path / part
        if ledger_path.is_symlink():
            raise PermissionError("tracked Stage 1 ledger path contains a symlink")
        if index < len(parts) - 1 and not ledger_path.is_dir():
            raise PermissionError("tracked Stage 1 ledger parent directory is missing or replaced")
    if not ledger_path.is_file():
        raise PermissionError("tracked Stage 1 ledger file is missing or replaced")
    try:
        resolved_ledger_path = ledger_path.resolve(strict=True)
    except OSError as exc:
        raise PermissionError("tracked Stage 1 ledger file cannot be resolved") from exc
    if resolved_ledger_path != ledger_path or not resolved_ledger_path.is_relative_to(repository_root):
        raise PermissionError("tracked Stage 1 ledger resolves outside its exact repository path")

    tracked_index = subprocess.run(
        ["git", "ls-files", "--stage", "--", ledger_relative],
        cwd=repository_root, check=False, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    index_rows = [line for line in tracked_index.stdout.splitlines() if line]
    if tracked_index.returncode != 0 or len(index_rows) != 1:
        raise PermissionError("Stage 1 ledger is not one exact tracked repository file")
    index_metadata, separator, index_path = index_rows[0].partition("\t")
    index_fields = index_metadata.split()
    if (
        not separator or index_path.replace("\\", "/") != ledger_relative
        or len(index_fields) != 3 or index_fields[0] != "100644" or index_fields[2] != "0"
    ):
        raise PermissionError("Stage 1 ledger is missing, replaced, or not a regular tracked file")

    working_tree = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--no-renames", "--name-status", "HEAD", "--", "."],
        cwd=repository_root, check=False, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    if working_tree.returncode != 0:
        raise PermissionError("cannot verify Stage 1 source immutability")
    changes = []
    for line in working_tree.stdout.splitlines():
        status, separator, path = line.partition("\t")
        if not separator:
            raise PermissionError("cannot parse tracked Stage 1 working-tree changes")
        changes.append((status, path.replace("\\", "/")))
    if changes:
        if changes != [("M", ledger_relative)]:
            raise PermissionError("Stage 1 tracked changes include immutable code/config or non-ledger artifacts")
        baseline = subprocess.run(
            ["git", "show", f"HEAD:{ledger_relative}"], cwd=repository_root,
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if baseline.returncode != 0:
            raise PermissionError("cannot read the committed Stage 1 ledger baseline")
        current_ledger = ledger_path.read_bytes().replace(b"\r\n", b"\n")
        baseline_ledger = baseline.stdout.replace(b"\r\n", b"\n")
        if not current_ledger.startswith(baseline_ledger):
            raise PermissionError("tracked Stage 1 ledger history was changed or truncated")
        try:
            _events(ledger_path)
        except (LedgerCorrupt, OSError) as exc:
            raise PermissionError("tracked Stage 1 ledger append is malformed") from exc
    current = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=STUDY, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()
    if len(current) != 40 or any(char not in "0123456789abcdef" for char in current):
        raise PermissionError("current package checkout has no valid Git commit SHA")
    lock_path = STAGE1 / "source-lock.json"
    lock_digest = _normalized_file_sha256(lock_path)
    sidecar = (STAGE1 / "source-lock.sha256").read_text(encoding="ascii").split()
    if not sidecar or sidecar[0] != lock_digest:
        raise PermissionError("current Stage 1 source-lock digest does not match its checked-in sidecar")
    return current, lock_digest


def validate_worker_package_binding(binding: PackageBinding) -> PackageBinding:
    """Validate a child source identity against the current locked checkout."""
    if not isinstance(binding, PackageBinding):
        raise PermissionError("Stage 1 worker must use the source binding from its reserved ledger")
    current = PackageBinding(*_current_package_binding())
    if binding != current:
        raise PermissionError("worker package binding does not match the current source-locked checkout")
    return current


class ResourceBlocked(RuntimeError): pass
class LedgerCorrupt(RuntimeError): pass
class DuplicateRun(RuntimeError): pass
class CheckpointMismatch(RuntimeError): pass


class BudgetOverrun(RuntimeError):
    def __init__(self, message: str, charged_cpu_seconds: float):
        super().__init__(message)
        self.charged_cpu_seconds = charged_cpu_seconds


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise LedgerCorrupt("torn final ledger event; stop and preserve the full reservation")
        return [json.loads(line) for line in raw.splitlines()]
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerCorrupt("cannot parse append-only budget ledger") from exc


def _state(events: Sequence[Mapping[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    runs: dict[str, dict[str, Any]] = {}
    units: dict[str, list[str]] = {}
    for e in events:
        if e.get("schema_id") != "issue75-stage1-budget-event-v2":
            raise LedgerCorrupt("unsupported budget ledger event schema")
        if e.get("event") == "reserve":
            rid, uid = e.get("run_id"), e.get("unit_id")
            if not isinstance(rid, str) or not rid or rid in runs or not isinstance(uid, str) or not uid:
                raise LedgerCorrupt("duplicate/empty run id or unit id")
            runs[rid] = {"reservation": dict(e), "finish": None, "scheduler_recoveries": []}
            units.setdefault(uid, []).append(rid)
        elif e.get("event") == "finish":
            rid = e.get("run_id")
            if rid not in runs or runs[rid]["finish"] is not None:
                raise LedgerCorrupt("orphan or duplicate finish event")
            runs[rid]["finish"] = dict(e)
        elif e.get("event") == "scheduler_recovery":
            rid = e.get("run_id")
            run = runs.get(rid)
            upper_bound = e.get("prior_cpu_upper_bound_seconds")
            if (
                run is None or run["finish"] is not None
                or run["reservation"].get("unit_id") != "SCHEDULER:TRAIN"
                or rid != SCHEDULER_TRAIN_RECOVERY_RUN_ID
                or e.get("unit_id") != "SCHEDULER:TRAIN"
                or run.setdefault("scheduler_recoveries", [])
                or not isinstance(upper_bound, (int, float))
                or not math.isfinite(float(upper_bound))
                or not 0 < float(upper_bound) < SCHEDULER_TRAIN_RECOVERY_CAP
                or e.get("accounting_basis") != "reservation retained at full cap; prior CPU bounded by source-bound event timestamps"
                or not isinstance(e.get("package_commit"), str)
                or len(e["package_commit"]) != 40
                or not isinstance(e.get("source_lock_sha256"), str)
                or len(e["source_lock_sha256"]) != 64
                or not isinstance(e.get("created_at_ns"), int)
            ):
                raise LedgerCorrupt("orphan or duplicate Stage 1 scheduler recovery marker")
            run["scheduler_recoveries"].append(dict(e))
        else:
            raise LedgerCorrupt(f"unknown event {e.get('event')!r}")
    return runs, units


PRE_GAME_START_RETRY_UNIT_ID = "TRAIN:7500001"
PRE_GAME_START_RETRY_RUN_ID = "TRAIN:7500001-5afd7b3277ee47a7"
PRE_GAME_START_RETRY_PACKAGE_COMMIT = "e0161453ba1dd96e18326fdac2fb9c75a234049a"
PRE_GAME_START_RETRY_SOURCE_LOCK_SHA256 = "932a9ecf39794e5b6f284914ded91eb153ac59cd19bb490a64af2d9744abe30d"
PRE_GAME_START_FAILURE_RECEIPT_SHA256 = "1c60d8c4e5e4fa64802089d5c7c337186ff53194ec43b4b7a75f64d79dd0acea"
PRE_GAME_START_WORKER_JOB_SHA256 = "098d19dd2149bea16e66fb7274bfdf38fdae760186f86d003dc17d27e19d8db3"
PRE_GAME_START_RETRY_REASON = "PRE_GAME_START_INFRA_FAILURE"
PRE_GAME_START_RETRY_RESERVED_CPU_SECONDS = 165.9459124477378
PRE_GAME_START_RETRY_MEASURED_CPU_SECONDS = 0.03347004399999998
PRE_GAME_START_RETRY_WALL_SECONDS = 0.6596911649999999
PRE_GAME_START_RETRY_RESERVED_AT_NS = 1793049822031952961
PRE_GAME_START_RETRY_FINISHED_AT_NS = 1793049822696986726
PRE_GAME_START_PROOF_SCHEMA = "issue75-pre-game-start-infra-proof-v1"
SCHEDULER_TRAIN_RECOVERY_RUN_ID = "SCHEDULER:TRAIN-3e416101548944b3"
SCHEDULER_TRAIN_RECOVERY_RESERVED_AT_NS = 1793049822021316152
SCHEDULER_TRAIN_RECOVERY_CAP = 120.0
SCHEDULER_RECOVERY_EXIT_MARGIN_SECONDS = 1.0
BLOCK0_PHASE_REPAIR_REASON = "BLOCK0_PHASE_MISMATCH_INTERVENTION_NOT_DELIVERED"
UPDATE0_RETRY_REASON = "UPDATE0_FAILED_BEFORE_OPTIMIZER"
BLOCK0_ORIGINAL_PACKAGE_COMMIT = "482c5b3b87e38066c8738d6f102906f965b9d0ae"
BLOCK0_ORIGINAL_SOURCE_LOCK_SHA256 = "ed54abb587d28661968544cbca31eabfb4dc8fad77696bcf726911b06fc01735"
BLOCK0_REPAIR_OWNER_RANGE = (7_500_001, 7_500_128)


def _load_block0_repair_manifest(artifacts_root: Path) -> tuple[dict[str, Any], str, dict[str, dict[str, Any]]]:
    path = STAGE1 / "block0-repair-lineage.json"
    raw = path.read_bytes()
    manifest = json.loads(raw)
    owners = manifest.get("owners")
    expected_seeds = list(range(*BLOCK0_REPAIR_OWNER_RANGE[:1], BLOCK0_REPAIR_OWNER_RANGE[1] + 1))
    if (
        manifest.get("schema_id") != "issue75-block0-intervention-repair-lineage-v1"
        or manifest.get("study_id") != "issue-75-no-expert-oracle"
        or manifest.get("scope") != "STUDY_ONLY"
        or manifest.get("block_index") != 0
        or manifest.get("registered_training_phase") != "training"
        or manifest.get("original_dispatched_worker_phase") != "train"
        or manifest.get("cause") != "UNIFORM_PHASE_MISMATCH_INTERVENTION_NOT_DELIVERED"
        or manifest.get("planner_disposition_comment_id") != 6431275271
        or manifest.get("owner_seed_range") != list(BLOCK0_REPAIR_OWNER_RANGE)
        or manifest.get("registered_owner_count") != 128
        or manifest.get("replace_every_registered_owner_once") is not True
        or manifest.get("required_treatment") != "J search with captured training features; old results remain immutable and excluded from training"
        or not isinstance(owners, list) or len(owners) != 128
        or [item.get("owner_seed") for item in owners] != expected_seeds
    ):
        raise ResourceBlocked("the locked block-0 lineage manifest does not match the single authorized repair")
    by_unit = {}
    for item in owners:
        seed = int(item["owner_seed"])
        unit_id = f"TRAIN:{seed}"
        history = item.get("history")
        if (
            item.get("unit_id") != unit_id
            or not isinstance(history, list) or not history
            or item.get("replacement_predecessor_run_id") != history[-1].get("run_id")
            or item.get("replacement_predecessor_result_sha256") != history[-1].get("result_sha256")
            or not isinstance(item.get("replacement_reservation_cpu_seconds"), (int, float))
            or not 0 < float(item["replacement_reservation_cpu_seconds"]) <= BUILDER_LIMIT
        ):
            raise ResourceBlocked("the block-0 lineage row is malformed")
        by_unit[unit_id] = item
    return manifest, sha256_bytes(raw), by_unit


def _validate_block0_originals(
    artifacts_root: Path, runs: Mapping[str, Mapping[str, Any]],
    units: Mapping[str, Sequence[str]],
) -> tuple[dict[str, Any], str, dict[str, dict[str, Any]]]:
    manifest, manifest_sha, by_unit = _load_block0_repair_manifest(artifacts_root)
    common_model_digest = None
    for unit_id, item in by_unit.items():
        expected_history = item["history"]
        actual_history = list(units.get(unit_id, ()))
        expected_ids = [record["run_id"] for record in expected_history]
        if actual_history[:len(expected_ids)] != expected_ids or len(actual_history) not in {
            len(expected_ids), len(expected_ids) + 1,
        }:
            raise ResourceBlocked(f"original block-0 lineage changed or has more than one replacement: {unit_id}")
        for expected in expected_history:
            run = runs.get(expected["run_id"])
            if not isinstance(run, Mapping):
                raise ResourceBlocked(f"original block-0 owner reservation is missing: {unit_id}")
            reservation, finish = run.get("reservation"), run.get("finish")
            if (
                not isinstance(reservation, Mapping)
                or reservation.get("unit_id") != unit_id
                or reservation.get("unit_kind") != "owner_arm"
                or reservation.get("role") != "builder"
                or reservation.get("package_commit") != expected.get("package_commit")
                or reservation.get("source_lock_sha256") != expected.get("source_lock_sha256")
                or not isinstance(finish, Mapping)
                or finish.get("status") != expected.get("status")
                or finish.get("result_sha256") != expected.get("result_sha256")
            ):
                raise ResourceBlocked(f"original block-0 owner receipt changed: {unit_id}")
        final = expected_history[-1]
        if (
            final.get("status") != "succeeded"
            or final.get("package_commit") != BLOCK0_ORIGINAL_PACKAGE_COMMIT
            or final.get("source_lock_sha256") != BLOCK0_ORIGINAL_SOURCE_LOCK_SHA256
            or not isinstance(final.get("result_sha256"), str)
        ):
            raise ResourceBlocked(f"original block-0 owner lacks the reviewed successful source binding: {unit_id}")
        result_path = Path(artifacts_root) / "units" / unit_id.replace(":", "-") / "result.json"
        try:
            result_bytes = result_path.read_bytes()
            result = json.loads(result_bytes)
        except (OSError, ValueError) as exc:
            raise ResourceBlocked(f"original immutable block-0 result is unavailable: {unit_id}") from exc
        if (
            sha256_bytes(result_bytes) != final["result_sha256"]
            or result.get("status") != "SUCCEEDED"
            or result.get("unit_id") != unit_id
            or result.get("phase") != "train"
            or result.get("formal") is not True
            or result.get("arm") != "TRAIN"
            or result.get("owner_seed") != item["owner_seed"]
            or result.get("package_commit") != BLOCK0_ORIGINAL_PACKAGE_COMMIT
            or result.get("source_lock_sha256") != BLOCK0_ORIGINAL_SOURCE_LOCK_SHA256
            or result.get("search") != []
            or result.get("training_features") is not None
            or result.get("expert_decisions") != 0
        ):
            raise ResourceBlocked(f"original block-0 row does not show the uniform missing intervention: {unit_id}")
        model_digest = result.get("model_digest")
        if not isinstance(model_digest, str) or (common_model_digest is not None and model_digest != common_model_digest):
            raise ResourceBlocked("block-0 original receipts do not share the same initial model digest")
        common_model_digest = model_digest
        added = actual_history[len(expected_ids):]
        if added:
            run_id = added[0]
            run = runs.get(run_id)
            reservation = None if not isinstance(run, Mapping) else run.get("reservation")
            if (
                not isinstance(reservation, Mapping)
                or reservation.get("repair_reason") != BLOCK0_PHASE_REPAIR_REASON
                or reservation.get("repair_manifest_sha256") != manifest_sha
                or reservation.get("repair_predecessor_run_id") != expected_ids[-1]
                or reservation.get("repair_predecessor_receipt_sha256") != final["result_sha256"]
                or reservation.get("package_commit") is None
                or reservation.get("source_lock_sha256") is None
            ):
                raise ResourceBlocked(f"block-0 replacement is not the single manifest-bound attempt: {unit_id}")
    update = manifest.get("update_retry")
    if (
        not isinstance(update, Mapping)
        or update.get("unit_id") != "UPDATE:0"
        or update.get("failed_predecessor_run_id") != "UPDATE:0-8c7ee939e4924cc6"
        or update.get("retry_once_after_all_replacements") is not True
        or update.get("failed_before_optimizer") is not True
        or update.get("no_model_checkpoint") is not True
    ):
        raise ResourceBlocked("the block-0 optimizer retry record is malformed")
    return manifest, manifest_sha, by_unit


def _validate_update0_retry_state(
    artifacts_root: Path, runs: Mapping[str, Mapping[str, Any]],
    units: Mapping[str, Sequence[str]], current_binding: tuple[str, str],
) -> tuple[dict[str, Any], str]:
    manifest, manifest_sha, by_unit = _validate_block0_originals(artifacts_root, runs, units)
    update = manifest["update_retry"]
    for unit_id, owner in by_unit.items():
        expected = [entry["run_id"] for entry in owner["history"]]
        history = list(units.get(unit_id, ()))
        if len(history) != len(expected) + 1 or history[:len(expected)] != expected:
            raise ResourceBlocked(f"all original block-0 owners must have exactly one replacement before UPDATE:0: {unit_id}")
        replacement = runs[history[-1]]
        reservation, finish = replacement["reservation"], replacement["finish"]
        if (
            reservation.get("repair_reason") != BLOCK0_PHASE_REPAIR_REASON
            or reservation.get("repair_manifest_sha256") != manifest_sha
            or finish is None or finish.get("status") != "succeeded"
        ):
            raise ResourceBlocked(f"UPDATE:0 retry requires completed block-0 replacements: {unit_id}")

    unit_id = str(update["unit_id"])
    predecessor = str(update["failed_predecessor_run_id"])
    history = list(units.get(unit_id, ()))
    if not history or history[0] != predecessor:
        raise ResourceBlocked("UPDATE:0 no longer has its exact reviewed failed predecessor")
    original = runs[predecessor]
    reservation, finish = original["reservation"], original["finish"]
    expected_reserved = float(update["failed_predecessor_reservation_cpu_seconds"])
    expected_measured = float(update["failed_predecessor_measured_cpu_seconds"])
    if (
        reservation.get("unit_id") != unit_id
        or reservation.get("unit_kind") != "training_update"
        or reservation.get("role") != "builder"
        or reservation.get("package_commit") != BLOCK0_ORIGINAL_PACKAGE_COMMIT
        or reservation.get("source_lock_sha256") != BLOCK0_ORIGINAL_SOURCE_LOCK_SHA256
        or reservation.get("reserved_cpu_seconds") != expected_reserved
        or finish is None or finish.get("status") != "failed"
        or finish.get("measured_cpu_seconds") != expected_measured
        or finish.get("checkpoint_sha256") is not None or finish.get("result_sha256") is not None
    ):
        raise ResourceBlocked("original UPDATE:0 failure differs from the immutable pre-optimizer record")
    for relative, digest_key in (
        (update["failure_receipt_path"], "failure_receipt_sha256"),
        (update["worker_job_path"], "worker_job_sha256"),
        (update["worker_log_path"], "worker_log_sha256"),
    ):
        try:
            digest = sha256_bytes((Path(artifacts_root) / relative).read_bytes())
        except OSError as exc:
            raise ResourceBlocked("original UPDATE:0 failure evidence is unavailable") from exc
        if digest != update[digest_key]:
            raise ResourceBlocked("original UPDATE:0 failure evidence changed after registration")
    failure = json.loads((Path(artifacts_root) / update["failure_receipt_path"]).read_text())
    job = json.loads((Path(artifacts_root) / update["worker_job_path"]).read_text())
    if (
        failure.get("unit_id") != unit_id or failure.get("run_id") != predecessor
        or failure.get("status") != "FAILED" or failure.get("checkpoint_sha256") is not None
        or failure.get("measured_cpu_seconds") != expected_measured
        or failure.get("reserved_cpu_seconds") != expected_reserved
        or job.get("unit_id") != unit_id or job.get("run_id") != predecessor
        or job.get("phase") != "update"
        or job.get("package_commit") != BLOCK0_ORIGINAL_PACKAGE_COMMIT
        or job.get("source_lock_sha256") != BLOCK0_ORIGINAL_SOURCE_LOCK_SHA256
        or job.get("previous_run_id") is not None or job.get("checkpoint_sha256") is not None
    ):
        raise ResourceBlocked("original UPDATE:0 artifacts do not prove a pre-optimizer failure")
    for name in ("pi1.pt", "block-0-step.pt"):
        if (Path(artifacts_root) / "models" / name).exists():
            raise ResourceBlocked("UPDATE:0 now has a checkpoint and is not eligible for the single retry")
    info = {
        "repair_reason": UPDATE0_RETRY_REASON,
        "repair_manifest_sha256": manifest_sha,
        "repair_predecessor_run_id": predecessor,
        "repair_predecessor_receipt_sha256": str(update["failure_receipt_sha256"]),
        "reserved_cpu_seconds": float(update["replacement_reservation_cpu_seconds"]),
    }
    previous = predecessor
    for index, run_id in enumerate(history[1:]):
        run = runs[run_id]
        retry, terminal = run["reservation"], run["finish"]
        first = index == 0
        if (
            retry.get("repair_reason") != info["repair_reason"]
            or retry.get("repair_manifest_sha256") != manifest_sha
            or retry.get("repair_predecessor_run_id") != predecessor
            or retry.get("repair_predecessor_receipt_sha256") != info["repair_predecessor_receipt_sha256"]
            or retry.get("package_commit") != current_binding[0]
            or retry.get("source_lock_sha256") != current_binding[1]
            or retry.get("unit_kind") != "training_update" or retry.get("role") != "builder"
            or float(retry.get("reserved_cpu_seconds", info["reserved_cpu_seconds"] + 1)) > info["reserved_cpu_seconds"]
            or retry.get("previous_run_id") != previous
            or (first and retry.get("checkpoint_sha256") is not None)
            or (not first and (
                runs[previous]["finish"] is None
                or runs[previous]["finish"].get("status") != "incomplete"
                or retry.get("checkpoint_sha256") != runs[previous]["finish"].get("checkpoint_sha256")
            ))
            or (terminal is not None and terminal.get("status") not in {"succeeded", "incomplete", "failed"})
        ):
            raise ResourceBlocked("UPDATE:0 retry/resume chain differs from its exact repair lineage")
        previous = run_id
    return info, manifest_sha


def _pre_game_start_failure_proof(
    artifacts_root: Path, runs: Mapping[str, Mapping[str, Any]], units: Mapping[str, Sequence[str]],
    *, predecessor_run_id: str, require_pristine: bool,
) -> dict[str, Any]:
    """Prove the one reviewed failed row stopped before native owner execution."""
    unit_id = PRE_GAME_START_RETRY_UNIT_ID
    history = list(units.get(unit_id, ()))
    if (predecessor_run_id != PRE_GAME_START_RETRY_RUN_ID or not history
            or history[0] != predecessor_run_id or len(history) > 2):
        raise ResourceBlocked("the one-row pre-game-start exception does not match its immutable predecessor")
    run = runs.get(predecessor_run_id)
    if not isinstance(run, Mapping):
        raise ResourceBlocked("the reviewed pre-game-start predecessor is absent")
    reservation, finish = run.get("reservation"), run.get("finish")
    expected_reservation = {
        "event": "reserve", "schema_id": "issue75-stage1-budget-event-v2",
        "run_id": PRE_GAME_START_RETRY_RUN_ID, "unit_id": unit_id,
        "role": "builder", "unit_kind": "owner_arm", "attempt_cap": None,
        "previous_run_id": None, "checkpoint_sha256": None,
        "reserved_cpu_seconds": PRE_GAME_START_RETRY_RESERVED_CPU_SECONDS,
        "package_commit": PRE_GAME_START_RETRY_PACKAGE_COMMIT,
        "source_lock_sha256": PRE_GAME_START_RETRY_SOURCE_LOCK_SHA256,
        "created_at_ns": PRE_GAME_START_RETRY_RESERVED_AT_NS,
    }
    expected_finish = {
        "event": "finish", "schema_id": "issue75-stage1-budget-event-v2",
        "run_id": PRE_GAME_START_RETRY_RUN_ID, "unit_id": unit_id,
        "status": "failed", "measured_cpu_seconds": PRE_GAME_START_RETRY_MEASURED_CPU_SECONDS,
        "reserved_cpu_seconds": PRE_GAME_START_RETRY_RESERVED_CPU_SECONDS,
        "started_attempts": 0, "checkpoint_sha256": None, "result_sha256": None,
        "package_commit": PRE_GAME_START_RETRY_PACKAGE_COMMIT,
        "source_lock_sha256": PRE_GAME_START_RETRY_SOURCE_LOCK_SHA256,
        "finished_at_ns": PRE_GAME_START_RETRY_FINISHED_AT_NS,
    }
    if not isinstance(reservation, Mapping) or any(reservation.get(k) != v for k, v in expected_reservation.items()):
        raise ResourceBlocked("the original registered row reservation differs from the reviewed failure")
    if not isinstance(finish, Mapping) or any(finish.get(k) != v for k, v in expected_finish.items()):
        raise ResourceBlocked("the original failure is not the exact reviewed zero-start ledger finish")

    units_root = Path(artifacts_root) / "units" / "TRAIN-7500001"
    failure_path = units_root / f"failure-{PRE_GAME_START_RETRY_RUN_ID}.json"
    job_path = units_root / f"worker-job-{PRE_GAME_START_RETRY_RUN_ID}.json"
    try:
        failure_bytes, job_bytes = failure_path.read_bytes(), job_path.read_bytes()
    except OSError as exc:
        raise ResourceBlocked("the immutable pre-game-start failure evidence is missing") from exc
    if sha256_bytes(failure_bytes) != PRE_GAME_START_FAILURE_RECEIPT_SHA256:
        raise ResourceBlocked("the original failed-row receipt bytes changed")
    if sha256_bytes(job_bytes) != PRE_GAME_START_WORKER_JOB_SHA256:
        raise ResourceBlocked("the original failed-row worker job bytes changed")
    try:
        failure, job = json.loads(failure_bytes), json.loads(job_bytes)
    except json.JSONDecodeError as exc:
        raise ResourceBlocked("the reviewed failure evidence is not valid JSON") from exc
    if (
        failure.get("schema_id") != "issue75-stage1-unit-result-v1"
        or failure.get("status") != "FAILED" or failure.get("boundary") != "PermissionError"
        or failure.get("unit_id") != unit_id or failure.get("run_id") != predecessor_run_id
        or failure.get("measured_cpu_seconds") != PRE_GAME_START_RETRY_MEASURED_CPU_SECONDS
        or failure.get("wall_seconds") != PRE_GAME_START_RETRY_WALL_SECONDS
        or failure.get("reserved_cpu_seconds") != PRE_GAME_START_RETRY_RESERVED_CPU_SECONDS
        or failure.get("checkpoint_sha256") is not None
        or job.get("schema_id") != "issue75-stage1-worker-job-v1"
        or job.get("run_id") != predecessor_run_id or job.get("unit_id") != unit_id
        or job.get("phase") != "train" or job.get("package_commit") != PRE_GAME_START_RETRY_PACKAGE_COMMIT
        or job.get("source_lock_sha256") != PRE_GAME_START_RETRY_SOURCE_LOCK_SHA256
        or job.get("row", {}).get("formal") is not True
        or job.get("row", {}).get("unit_id") != unit_id
        or job.get("row", {}).get("owner_seed") != 7_500_001
        or job.get("row", {}).get("game_seed") != 7_500_001
        or job.get("row", {}).get("arm") != "TRAIN"
    ):
        raise ResourceBlocked("the original failure/job no longer prove the reviewed pre-game-start boundary")

    run_id = PRE_GAME_START_RETRY_RUN_ID
    absence = {
        "worker_result_absent": not (units_root / "result.json").exists(),
        "worker_log_absent": not (units_root / f"worker-log-{run_id}.txt").exists(),
        "native_output_absent": not (units_root / "native").exists(),
        "runtime_root_absent": not (units_root / "runtime" / run_id).exists(),
        "search_output_absent": not (Path(artifacts_root) / "search" / "TRAIN-7500001").exists(),
    }
    if require_pristine and not all(absence.values()):
        raise ResourceBlocked("the original owner worker may have entered native/game execution")
    proof = {
        "schema_id": PRE_GAME_START_PROOF_SCHEMA,
        "unit_id": unit_id,
        "predecessor_run_id": predecessor_run_id,
        "package_commit": PRE_GAME_START_RETRY_PACKAGE_COMMIT,
        "source_lock_sha256": PRE_GAME_START_RETRY_SOURCE_LOCK_SHA256,
        "failure_boundary": "PermissionError",
        "started_attempts": 0,
        "worker_wall_seconds": PRE_GAME_START_RETRY_WALL_SECONDS,
        "failure_receipt_sha256": PRE_GAME_START_FAILURE_RECEIPT_SHA256,
        "worker_job_sha256": PRE_GAME_START_WORKER_JOB_SHA256,
        **absence,
    }
    return proof


def _validate_pre_game_start_retry_reservation(
    reservation: Mapping[str, Any], runs: Mapping[str, Mapping[str, Any]],
    units: Mapping[str, Sequence[str]], artifacts_root: Path, *, require_pristine: bool,
) -> dict[str, Any]:
    unit_id = str(reservation.get("unit_id", ""))
    predecessor = str(reservation.get("previous_run_id", ""))
    history = units.get(unit_id, ())
    if (
        unit_id != PRE_GAME_START_RETRY_UNIT_ID or len(history) != 2
        or list(history) != [PRE_GAME_START_RETRY_RUN_ID, reservation.get("run_id")]
        or predecessor != PRE_GAME_START_RETRY_RUN_ID
        or reservation.get("role") != "builder" or reservation.get("unit_kind") != "owner_arm"
        or reservation.get("attempt_cap") is not None or reservation.get("checkpoint_sha256") is not None
        or reservation.get("retry_reason") != PRE_GAME_START_RETRY_REASON
        or not isinstance(reservation.get("reserved_cpu_seconds"), (int, float))
        or not 0 < float(reservation["reserved_cpu_seconds"]) <= PRE_GAME_START_RETRY_RESERVED_CPU_SECONDS
    ):
        raise ResourceBlocked("the new row attempt is not the single authorized pre-game-start retry")
    proof = _pre_game_start_failure_proof(
        artifacts_root, runs, units, predecessor_run_id=predecessor,
        require_pristine=require_pristine,
    )
    if reservation.get("pre_game_start_proof") != proof or reservation.get("pre_game_start_proof_sha256") != sha256_bytes(canonical(proof)):
        raise ResourceBlocked("the retry reservation is not bound to its verified pre-game-start evidence")
    return proof


def _scheduler_train_recovery_cpu_upper_bound(
    runs: Mapping[str, Mapping[str, Any]], units: Mapping[str, Sequence[str]], artifacts_root: Path,
) -> float:
    """Bound pre-repair scheduler CPU from recorded wall timestamps and keep a shutdown margin."""
    scheduler_history = list(units.get("SCHEDULER:TRAIN", ()))
    if scheduler_history != [SCHEDULER_TRAIN_RECOVERY_RUN_ID]:
        raise ResourceBlocked("open training scheduler is not the exact original reservation")
    scheduler_run = runs[SCHEDULER_TRAIN_RECOVERY_RUN_ID]
    reservation = scheduler_run["reservation"]
    if (
        scheduler_run["finish"] is not None
        or reservation.get("unit_kind") != "scheduler" or reservation.get("role") != "builder"
        or reservation.get("reserved_cpu_seconds") != SCHEDULER_TRAIN_RECOVERY_CAP
        or reservation.get("package_commit") != PRE_GAME_START_RETRY_PACKAGE_COMMIT
        or reservation.get("source_lock_sha256") != PRE_GAME_START_RETRY_SOURCE_LOCK_SHA256
        or reservation.get("created_at_ns") != SCHEDULER_TRAIN_RECOVERY_RESERVED_AT_NS
    ):
        raise ResourceBlocked("open scheduler reservation differs from its immutable original receipt")
    proof = _pre_game_start_failure_proof(
        artifacts_root, runs, units, predecessor_run_id=PRE_GAME_START_RETRY_RUN_ID,
        require_pristine=True,
    )
    if proof.get("started_attempts") != 0:
        raise ResourceBlocked("scheduler recovery requires the exact zero-start predecessor")
    failure = runs[PRE_GAME_START_RETRY_RUN_ID]["finish"]
    elapsed = (int(failure["finished_at_ns"]) - int(reservation["created_at_ns"])) / 1_000_000_000
    if elapsed < float(proof["worker_wall_seconds"]):
        raise ResourceBlocked("scheduler recovery timestamps do not cover the worker's measured monotonic wall interval")
    upper_bound = elapsed + SCHEDULER_RECOVERY_EXIT_MARGIN_SECONDS
    if not math.isfinite(upper_bound) or elapsed < 0 or upper_bound >= SCHEDULER_TRAIN_RECOVERY_CAP:
        raise ResourceBlocked("the original scheduler reserve cannot safely fund recovery")
    return upper_bound


def _charge(run: Mapping[str, Any]) -> float:
    done = run["finish"]
    return float(run["reservation"]["reserved_cpu_seconds"] if done is None else done["measured_cpu_seconds"])


class Stage1Ledger:
    """Append-only accounting; every reservation is bound to the locked package and CPU limits."""
    def __init__(self, directory: Path):
        self._initialize(directory, PackageBinding(*_current_package_binding()))

    def _initialize(self, directory: Path, binding: PackageBinding) -> None:
        self.binding = binding
        self.directory = Path(directory)
        self.events_path = self.directory / "events.jsonl"
        self.lock_path = self.directory / "events.lock"
        self.thread_lock = threading.RLock()

    @classmethod
    def open_reserved_worker(
        cls, directory: Path, *, run_id: str, unit_id: str,
    ) -> "Stage1Ledger":
        """Open only a live parent reservation bound to the current locked package."""
        current_binding = _current_package_binding()
        ledger = cls.__new__(cls)
        ledger._initialize(directory, PackageBinding(*current_binding))
        runs, _ = _state(_events(ledger.events_path))
        run = runs.get(str(run_id))
        if run is None or run["finish"] is not None:
            raise PermissionError("worker has no open write-ahead ledger reservation")
        reservation = run["reservation"]
        if reservation.get("unit_id") != str(unit_id):
            raise PermissionError("worker reservation does not match its registered unit")
        if (
            reservation.get("package_commit") != current_binding[0]
            or reservation.get("source_lock_sha256") != current_binding[1]
        ):
            raise PermissionError("worker reservation belongs to a different package or source lock")
        ledger.require_open_reservation(run_id=run_id, unit_id=unit_id)
        return ledger

    def _tx(self, operation):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with self.thread_lock:
                fcntl.flock(fd, fcntl.LOCK_EX)
                rows = _events(self.events_path)
                result, event = operation(rows)
                if event is not None:
                    events = event if isinstance(event, list) else [event]
                    data = b"".join(canonical(item) + b"\n" for item in events)
                    out = os.open(self.events_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                    try:
                        if os.write(out, data) != len(data):
                            raise OSError("short append to CPU ledger")
                        os.fsync(out)
                    finally:
                        os.close(out)
                return result
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def require_open_reservation(self, *, run_id: str, unit_id: str, role: str = "builder") -> dict[str, Any]:
        """Confirm a worker is the exact source-bound child of a live ledger reservation."""
        runs, units = _state(_events(self.events_path))
        run = runs.get(str(run_id))
        if run is None or run["finish"] is not None:
            raise PermissionError("worker has no open write-ahead ledger reservation")
        reservation = run["reservation"]
        if (
            reservation.get("unit_id") != str(unit_id)
            or reservation.get("role") != str(role)
            or reservation.get("package_commit") != self.binding.package_commit
            or reservation.get("source_lock_sha256") != self.binding.source_lock_sha256
        ):
            raise PermissionError("worker reservation does not match its unit and active package binding")
        if reservation.get("retry_reason") == PRE_GAME_START_RETRY_REASON:
            _validate_pre_game_start_retry_reservation(
                reservation, runs, units, self.directory.parent, require_pristine=True,
            )
        elif reservation.get("repair_reason") == BLOCK0_PHASE_REPAIR_REASON:
            manifest, manifest_sha, by_unit = _validate_block0_originals(
                self.directory.parent, runs, units,
            )
            owner = by_unit.get(str(unit_id))
            original_ids = [] if owner is None else [entry["run_id"] for entry in owner["history"]]
            if (
                owner is None or list(units.get(str(unit_id), ())) != original_ids + [str(run_id)]
                or reservation.get("repair_manifest_sha256") != manifest_sha
                or reservation.get("repair_predecessor_run_id") != original_ids[-1]
                or reservation.get("repair_predecessor_receipt_sha256") != owner["replacement_predecessor_result_sha256"]
                or reservation.get("package_commit") != self.binding.package_commit
                or reservation.get("source_lock_sha256") != self.binding.source_lock_sha256
                or reservation.get("unit_kind") != "owner_arm" or reservation.get("role") != "builder"
                or reservation.get("previous_run_id") is not None
                or reservation.get("checkpoint_sha256") is not None
            ):
                raise PermissionError("owner worker reservation differs from its authorized block-0 replacement")
        elif reservation.get("repair_reason") == UPDATE0_RETRY_REASON:
            info = self.validate_update0_retry_state()
            if any(reservation.get(key) != info.get(key) for key in (
                "repair_reason", "repair_manifest_sha256", "repair_predecessor_run_id",
                "repair_predecessor_receipt_sha256",
            )) or reservation.get("package_commit") != self.binding.package_commit or reservation.get("source_lock_sha256") != self.binding.source_lock_sha256:
                raise PermissionError("UPDATE:0 worker reservation differs from the one-time failure-repair lineage")
        elif any(reservation.get(key) is not None for key in (
            "retry_reason", "pre_game_start_proof", "pre_game_start_proof_sha256",
            "repair_reason", "repair_manifest_sha256", "repair_predecessor_run_id",
            "repair_predecessor_receipt_sha256",
        )):
            raise PermissionError("worker reservation contains an unrecognized retry claim")
        return dict(reservation)

    def pre_game_start_retry_info(self, *, unit_id: str, predecessor_run_id: str) -> dict[str, Any]:
        """Return proof only for the one fixed immutable pre-game-start predecessor."""
        if unit_id != PRE_GAME_START_RETRY_UNIT_ID or predecessor_run_id != PRE_GAME_START_RETRY_RUN_ID:
            raise ResourceBlocked("no pre-game-start retry exception is registered for this unit")
        runs, units = _state(_events(self.events_path))
        if units.get(unit_id) != [predecessor_run_id] or runs[predecessor_run_id]["finish"] is None:
            raise ResourceBlocked("pre-game-start retry predecessor has changed")
        return _pre_game_start_failure_proof(
            self.directory.parent, runs, units, predecessor_run_id=predecessor_run_id,
            require_pristine=True,
        )

    def validate_block0_phase_repair_state(self) -> tuple[dict[str, Any], str, dict[str, dict[str, Any]]]:
        """Validate all original and any single append-only replacement receipts."""
        runs, units = _state(_events(self.events_path))
        return _validate_block0_originals(self.directory.parent, runs, units)

    def block0_phase_repair_info(self, *, unit_id: str) -> dict[str, Any]:
        """Return the exact one-time replacement lineage for a missing block-0 intervention."""
        runs, units = _state(_events(self.events_path))
        manifest, manifest_sha, by_unit = _validate_block0_originals(
            self.directory.parent, runs, units,
        )
        owner = by_unit.get(unit_id)
        if owner is None:
            raise ResourceBlocked("no block-0 intervention replacement is registered for this owner")
        original_ids = [entry["run_id"] for entry in owner["history"]]
        if list(units.get(unit_id, ())) != original_ids:
            raise ResourceBlocked("this registered block-0 owner already has its one replacement reservation")
        return {
            "repair_reason": BLOCK0_PHASE_REPAIR_REASON,
            "repair_manifest_sha256": manifest_sha,
            "repair_predecessor_run_id": original_ids[-1],
            "repair_predecessor_receipt_sha256": owner["replacement_predecessor_result_sha256"],
            "reserved_cpu_seconds": float(owner["replacement_reservation_cpu_seconds"]),
            "scope": manifest["scope"],
        }

    def validate_update0_retry_state(self) -> dict[str, Any]:
        runs, units = _state(_events(self.events_path))
        info, manifest_sha = _validate_update0_retry_state(
            self.directory.parent, runs, units,
            (self.binding.package_commit, self.binding.source_lock_sha256),
        )
        return {**info, "repair_manifest_sha256": manifest_sha}

    def update0_retry_info(self) -> dict[str, Any]:
        info = self.validate_update0_retry_state()
        runs, units = _state(_events(self.events_path))
        if list(units.get("UPDATE:0", ())) != [info["repair_predecessor_run_id"]]:
            raise ResourceBlocked("the single UPDATE:0 retry has already been reserved")
        return info

    def recover_open_training_scheduler(self) -> dict[str, Any]:
        """Mark and reuse the exact original 120 CPU-s allocation once, without a second reserve."""
        def operation(rows):
            runs, units = _state(rows)
            run = runs.get(SCHEDULER_TRAIN_RECOVERY_RUN_ID)
            if run is None or run["finish"] is not None or run.get("scheduler_recoveries"):
                raise ResourceBlocked("the original training scheduler reservation is already recovered or unavailable")
            upper_bound = _scheduler_train_recovery_cpu_upper_bound(runs, units, self.directory.parent)
            event = {
                "event": "scheduler_recovery", "schema_id": "issue75-stage1-budget-event-v2",
                "run_id": SCHEDULER_TRAIN_RECOVERY_RUN_ID, "unit_id": "SCHEDULER:TRAIN",
                "prior_cpu_upper_bound_seconds": upper_bound,
                "accounting_basis": "reservation retained at full cap; prior CPU bounded by source-bound event timestamps",
                "package_commit": self.binding.package_commit,
                "source_lock_sha256": self.binding.source_lock_sha256,
                "created_at_ns": time.time_ns(),
            }
            return event, event
        return self._tx(operation)

    @staticmethod
    def _charges(runs):
        result = {"builder": 0.0, "reviewer": 0.0}
        for run in runs.values():
            role = run["reservation"].get("role")
            if role not in result:
                raise LedgerCorrupt("unknown ledger role")
            result[role] += _charge(run)
        return result

    def _reservation_event(self, rows, *, run_id: str, unit_id: str, role: str,
                           reserved_cpu_seconds: float, unit_kind: str,
                           attempt_cap: int | None = None,
                           previous_run_id: str | None = None,
                           checkpoint_sha256: str | None = None,
                           retry_reason: str | None = None,
                           pre_game_start_proof: Mapping[str, Any] | None = None,
                           repair_reason: str | None = None,
                           repair_manifest_sha256: str | None = None,
                           repair_predecessor_run_id: str | None = None,
                           repair_predecessor_receipt_sha256: str | None = None):
        cap = float(reserved_cpu_seconds)
        if role not in {"builder", "reviewer"} or not run_id or not unit_id or not math.isfinite(cap) or cap <= 0:
            raise ValueError("invalid run, unit, role, or CPU reservation")
        if unit_kind == "search_invocation":
            if cap > SEARCH_CPU_CAP_SECONDS or attempt_cap is None or not 1 <= attempt_cap <= SEARCH_ATTEMPT_CAP:
                raise ValueError("search reservation exceeds 32 CPU-s / 16 started-attempt lock")
        elif attempt_cap is not None:
            raise ValueError("attempt_cap is only valid for a search invocation")
        if checkpoint_sha256 is not None and (
            len(checkpoint_sha256) != 64 or any(c not in "0123456789abcdef" for c in checkpoint_sha256)
        ):
            raise ValueError("invalid checkpoint digest")
        repair_fields = {
            "repair_reason": repair_reason,
            "repair_manifest_sha256": repair_manifest_sha256,
            "repair_predecessor_run_id": repair_predecessor_run_id,
            "repair_predecessor_receipt_sha256": repair_predecessor_receipt_sha256,
        }
        runs, units = _state(rows)
        if run_id in runs:
            raise DuplicateRun(run_id)
        history = units.get(unit_id, [])
        if history:
            last = runs[history[-1]]
            done = last["finish"]
            if done is None:
                raise ResourceBlocked(f"unresolved reservation remains charged at full cap: {unit_id}")
            if done.get("status") == "incomplete":
                if retry_reason is not None or pre_game_start_proof is not None:
                    raise CheckpointMismatch("checkpoint resume cannot claim the pre-game-start exception")
                if last["reservation"].get("repair_reason") == UPDATE0_RETRY_REASON:
                    info = self.validate_update0_retry_state()
                    if any(
                        last["reservation"].get(key) != info.get(key)
                        for key in (
                            "repair_reason", "repair_manifest_sha256",
                            "repair_predecessor_run_id", "repair_predecessor_receipt_sha256",
                        )
                    ) or any(
                        repair_fields.get(key) != info.get(key)
                        for key in (
                            "repair_reason", "repair_manifest_sha256",
                            "repair_predecessor_run_id", "repair_predecessor_receipt_sha256",
                        )
                    ):
                        raise CheckpointMismatch("UPDATE:0 checkpoint resume changed its original failure lineage")
                elif any(value is not None for value in (
                    repair_reason, repair_manifest_sha256, repair_predecessor_run_id,
                    repair_predecessor_receipt_sha256,
                )):
                    raise CheckpointMismatch("checkpoint resume carries unregistered repair metadata")
                if float(done.get("measured_cpu_seconds", 0.0)) > float(done.get("reserved_cpu_seconds", 0.0)):
                    raise ResourceBlocked("an incomplete unit exceeded its immutable reservation and cannot be resumed")
                if (previous_run_id != history[-1] or not done.get("checkpoint_sha256")
                        or checkpoint_sha256 != done["checkpoint_sha256"]
                        or role != last["reservation"]["role"]):
                    raise CheckpointMismatch("resume needs the exact prior incomplete run and checkpoint digest")
            elif done.get("status") == "failed":
                if unit_id == "UPDATE:0" and repair_reason == UPDATE0_RETRY_REASON:
                    info = self.update0_retry_info()
                    if (
                        previous_run_id != info["repair_predecessor_run_id"]
                        or checkpoint_sha256 is not None
                        or repair_manifest_sha256 != info["repair_manifest_sha256"]
                        or repair_predecessor_run_id != info["repair_predecessor_run_id"]
                        or repair_predecessor_receipt_sha256 != info["repair_predecessor_receipt_sha256"]
                        or retry_reason is not None or pre_game_start_proof is not None
                        or unit_kind != "training_update" or role != "builder"
                        or cap > info["reserved_cpu_seconds"]
                    ):
                        raise ResourceBlocked("UPDATE:0 retry differs from its exact registered repair cap/lineage")
                elif history == [PRE_GAME_START_RETRY_RUN_ID] and unit_id == PRE_GAME_START_RETRY_UNIT_ID:
                    if previous_run_id != PRE_GAME_START_RETRY_RUN_ID or checkpoint_sha256 is not None:
                        raise ResourceBlocked("the one-row exception must link its exact failed predecessor")
                    if retry_reason != PRE_GAME_START_RETRY_REASON:
                        raise ResourceBlocked("failed owner row lacks the explicit pre-game-start failure reason")
                    proof = _pre_game_start_failure_proof(
                        self.directory.parent, runs, units, predecessor_run_id=previous_run_id,
                        require_pristine=True,
                    )
                    if pre_game_start_proof != proof:
                        raise ResourceBlocked("retry proof differs from the verified original failure evidence")
                    if unit_kind != "owner_arm" or role != "builder" or cap > PRE_GAME_START_RETRY_RESERVED_CPU_SECONDS:
                        raise ResourceBlocked("pre-game-start retry changed the registered owner row or CPU ceiling")
                else:
                    raise ResourceBlocked(f"failed unit is immutable and cannot be replayed: {unit_id}")
            elif done.get("status") == "succeeded":
                if not unit_id.startswith("TRAIN:") or repair_reason != BLOCK0_PHASE_REPAIR_REASON:
                    raise DuplicateRun(f"terminal unit cannot be replayed: {unit_id}")
                manifest, manifest_sha, by_unit = _validate_block0_originals(
                    self.directory.parent, runs, units,
                )
                owner = by_unit.get(unit_id)
                if owner is None:
                    raise ResourceBlocked("replacement is outside the one registered block-0 owner cohort")
                original_ids = [entry["run_id"] for entry in owner["history"]]
                if (
                    history != original_ids
                    or previous_run_id is not None or checkpoint_sha256 is not None
                    or retry_reason is not None or pre_game_start_proof is not None
                    or repair_manifest_sha256 != manifest_sha
                    or repair_predecessor_run_id != original_ids[-1]
                    or repair_predecessor_receipt_sha256 != owner["replacement_predecessor_result_sha256"]
                    or unit_kind != "owner_arm" or role != "builder"
                    or cap > float(owner["replacement_reservation_cpu_seconds"])
                ):
                    raise ResourceBlocked("block-0 replacement differs from its exact immutable lineage and cap")
            else:
                raise DuplicateRun(f"terminal unit cannot be replayed: {unit_id}")
        elif (previous_run_id is not None or checkpoint_sha256 is not None
              or retry_reason is not None or pre_game_start_proof is not None
              or repair_reason is not None or repair_manifest_sha256 is not None
              or repair_predecessor_run_id is not None or repair_predecessor_receipt_sha256 is not None):
            raise CheckpointMismatch("resume/retry metadata supplied for a new unit")
        charges = self._charges(runs)
        if role == "builder":
            if charges["builder"] + cap > BUILDER_LIMIT:
                raise ResourceBlocked("Builder CPU cap would be exceeded")
            if charges["builder"] + cap + max(REVIEWER_LIMIT, charges["reviewer"]) > ALL_IN_LIMIT:
                raise ResourceBlocked("all-in cap or protected Reviewer reserve would be breached")
        else:
            if charges["reviewer"] + cap > REVIEWER_LIMIT:
                raise ResourceBlocked("protected Reviewer CPU reserve would be exceeded")
            if charges["builder"] + charges["reviewer"] + cap > ALL_IN_LIMIT:
                raise ResourceBlocked("all-in CPU cap would be exceeded")
        return {
            "event": "reserve", "schema_id": "issue75-stage1-budget-event-v2",
            "run_id": run_id, "unit_id": unit_id, "role": role, "unit_kind": unit_kind,
            "reserved_cpu_seconds": cap, "attempt_cap": attempt_cap,
            "previous_run_id": previous_run_id, "checkpoint_sha256": checkpoint_sha256,
            "retry_reason": retry_reason,
            "repair_reason": repair_reason,
            "repair_manifest_sha256": repair_manifest_sha256,
            "repair_predecessor_run_id": repair_predecessor_run_id,
            "repair_predecessor_receipt_sha256": repair_predecessor_receipt_sha256,
            "pre_game_start_proof": None if pre_game_start_proof is None else dict(pre_game_start_proof),
            "pre_game_start_proof_sha256": None if pre_game_start_proof is None else sha256_bytes(canonical(pre_game_start_proof)),
            "package_commit": self.binding.package_commit,
            "source_lock_sha256": self.binding.source_lock_sha256,
            "created_at_ns": time.time_ns(),
        }

    def reserve_many(self, reservations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Append a batch of write-ahead reservations under one ledger lock."""
        specs = [dict(spec) for spec in reservations]
        if not specs:
            raise ValueError("reserve_many requires at least one reservation")

        def operation(rows):
            projected = list(rows)
            events = []
            for spec in specs:
                event = self._reservation_event(projected, **spec)
                events.append(event)
                projected.append(event)
            return events, events

        return self._tx(operation)

    def reserve(self, *, run_id: str, unit_id: str, role: str,
                reserved_cpu_seconds: float, unit_kind: str,
                attempt_cap: int | None = None,
                previous_run_id: str | None = None,
                checkpoint_sha256: str | None = None,
                retry_reason: str | None = None,
                pre_game_start_proof: Mapping[str, Any] | None = None,
                repair_reason: str | None = None,
                repair_manifest_sha256: str | None = None,
                repair_predecessor_run_id: str | None = None,
                repair_predecessor_receipt_sha256: str | None = None) -> dict[str, Any]:
        return self.reserve_many([{
            "run_id": run_id, "unit_id": unit_id, "role": role,
            "reserved_cpu_seconds": reserved_cpu_seconds, "unit_kind": unit_kind,
            "attempt_cap": attempt_cap, "previous_run_id": previous_run_id,
            "checkpoint_sha256": checkpoint_sha256, "retry_reason": retry_reason,
            "pre_game_start_proof": pre_game_start_proof,
            "repair_reason": repair_reason,
            "repair_manifest_sha256": repair_manifest_sha256,
            "repair_predecessor_run_id": repair_predecessor_run_id,
            "repair_predecessor_receipt_sha256": repair_predecessor_receipt_sha256,
        }])[0]

    def finish(
        self, *, run_id: str, measured_cpu_seconds: float, status: str,
        checkpoint_sha256: str | None = None, started_attempts: int = 0,
        result_sha256: str | None = None,
    ) -> dict[str, Any]:
        measured = float(measured_cpu_seconds)
        if not math.isfinite(measured) or measured < 0 or status not in {"succeeded", "failed", "incomplete"}:
            raise ValueError("invalid measured CPU or attempt status")
        if not isinstance(started_attempts, int) or started_attempts < 0:
            raise ValueError("started_attempts must be a nonnegative integer")
        if status == "incomplete":
            if not checkpoint_sha256 or len(checkpoint_sha256) != 64 or any(c not in "0123456789abcdef" for c in checkpoint_sha256):
                raise ValueError("incomplete work requires a durable checkpoint SHA256")
        elif checkpoint_sha256 is not None:
            raise ValueError("only incomplete work may carry a resume checkpoint")
        if status == "succeeded":
            if not isinstance(result_sha256, str) or len(result_sha256) != 64 or any(
                c not in "0123456789abcdef" for c in result_sha256
            ):
                raise ValueError("successful work requires its immutable result SHA256")
        elif result_sha256 is not None:
            raise ValueError("only succeeded work may carry a result SHA256")

        def operation(rows):
            runs, _ = _state(rows)
            run = runs.get(run_id)
            if run is None or run["finish"] is not None:
                raise DuplicateRun(f"no open reservation: {run_id}")
            reservation = run["reservation"]
            event = {
                "event": "finish", "schema_id": "issue75-stage1-budget-event-v2",
                "run_id": run_id, "unit_id": reservation["unit_id"], "status": status,
                "measured_cpu_seconds": measured, "reserved_cpu_seconds": reservation["reserved_cpu_seconds"],
                "started_attempts": started_attempts, "checkpoint_sha256": checkpoint_sha256,
                "result_sha256": result_sha256,
                "finished_at_ns": time.time_ns(), "package_commit": self.binding.package_commit,
                "source_lock_sha256": self.binding.source_lock_sha256,
            }
            return (event, reservation), event
        event, reservation = self._tx(operation)
        if measured > float(reservation["reserved_cpu_seconds"]) or (
            reservation["unit_kind"] == "search_invocation" and
            started_attempts > int(reservation["attempt_cap"])
        ):
            raise BudgetOverrun(f"actual work exceeded its recorded reservation: {run_id}", measured)
        return event

    def forecast(
        self, *, expected_unit_ids: Sequence[str], future_builder_cpu_seconds: Mapping[str, float],
        allowed_completed_unit_ids: Sequence[str] = (),
        allowed_open_reservation_unit_ids: Sequence[str] = (),
    ):
        expected = list(expected_unit_ids)
        allowed = set(allowed_completed_unit_ids)
        allowed_open = set(allowed_open_reservation_unit_ids)
        if (not expected or len(set(expected)) != len(expected)
                or set(expected) != set(future_builder_cpu_seconds)
                or allowed & set(expected) or not allowed_open <= set(expected)
                or not allowed_open <= {"SCHEDULER:TRAIN"}):
            raise ValueError("forecast must include each exact registered unit once")
        estimates = {unit: float(future_builder_cpu_seconds[unit]) for unit in expected}
        if any(not math.isfinite(value) or value < 0 for value in estimates.values()):
            raise ValueError("forecast costs must be finite and nonnegative")

        def operation(rows):
            runs, units = _state(rows)
            charges = self._charges(runs)
            future = 0.0
            blocked = []
            for unit, history in units.items():
                if unit not in set(expected) and (
                    unit not in allowed or runs[history[-1]]["finish"] is None
                    or runs[history[-1]]["finish"]["status"] != "succeeded"
                ):
                    blocked.append(unit)
            carried_open = []
            retry_units = []
            for unit in expected:
                history = units.get(unit, [])
                if not history:
                    future += estimates[unit]
                    continue
                last = runs[history[-1]]
                done = last["finish"]
                if done is None:
                    if unit in allowed_open and unit == "SCHEDULER:TRAIN":
                        if last.get("scheduler_recoveries"):
                            blocked.append(unit)
                            continue
                        try:
                            _scheduler_train_recovery_cpu_upper_bound(runs, units, self.directory.parent)
                        except ResourceBlocked:
                            blocked.append(unit)
                            continue
                        if float(last["reservation"].get("reserved_cpu_seconds", -1)) != estimates[unit]:
                            blocked.append(unit)
                            continue
                        carried_open.append(unit)
                    else:
                        blocked.append(unit)
                elif done.get("status") == "failed":
                    if unit == PRE_GAME_START_RETRY_UNIT_ID and history == [PRE_GAME_START_RETRY_RUN_ID]:
                        try:
                            _pre_game_start_failure_proof(
                                self.directory.parent, runs, units,
                                predecessor_run_id=PRE_GAME_START_RETRY_RUN_ID, require_pristine=True,
                            )
                        except ResourceBlocked:
                            blocked.append(unit)
                            continue
                        future += estimates[unit]
                        retry_units.append(unit)
                    else:
                        blocked.append(unit)
                elif done.get("status") == "incomplete":
                    future += estimates[unit]
            if blocked:
                raise ResourceBlocked("forecast stops on unresolved/failed units: " + ", ".join(blocked[:8]))
            builder = charges["builder"] + future
            all_in = builder + max(REVIEWER_LIMIT, charges["reviewer"])
            result = {
                "status": "FITS" if builder <= BUILDER_LIMIT and all_in <= ALL_IN_LIMIT else "RESOURCE_BLOCKED",
                "registered_units": len(expected),
                "completed_units": sum(
                    bool(units.get(unit)) and runs[units[unit][-1]]["finish"] is not None
                    and runs[units[unit][-1]]["finish"]["status"] == "succeeded" for unit in expected
                ),
                "builder_cpu_charged": charges["builder"], "reviewer_cpu_charged": charges["reviewer"],
                "future_builder_cpu_forecast": future, "projected_builder_cpu": builder,
                "projected_all_in_with_reviewer_reserve": all_in,
                "carried_open_reservation_unit_ids": carried_open,
                "authorized_pre_game_start_retry_unit_ids": retry_units,
                "builder_cpu_remaining": BUILDER_LIMIT - builder, "all_in_cpu_remaining": ALL_IN_LIMIT - all_in,
                "package_commit": self.binding.package_commit,
                "source_lock_sha256": self.binding.source_lock_sha256,
            }
            if result["status"] != "FITS":
                raise ResourceBlocked(canonical(result).decode())
            return result, None
        return self._tx(operation)

    def read_events(self):
        return self._tx(lambda rows: (_state(rows) and rows, None))


def open_stage1_ledger(directory: Path) -> Stage1Ledger:
    """Open the cumulative ledger after validating the locked package and source binding."""
    return Stage1Ledger(directory)


def open_reserved_stage1_worker_ledger(
    directory: Path, *, run_id: str, unit_id: str,
) -> Stage1Ledger:
    """Validate a durable parent reservation without refetching GitHub in every worker."""
    return Stage1Ledger.open_reserved_worker(directory, run_id=run_id, unit_id=unit_id)


class ProcessTreeSafetyError(RuntimeError): pass


class ProcessTreeTimeout(TimeoutError):
    def __init__(self, cpu_seconds: float, timeout_seconds: float):
        super().__init__(f"worker wall timeout after {cpu_seconds:.6f} process-tree CPU seconds")
        self.cpu_seconds, self.timeout_seconds = cpu_seconds, timeout_seconds


class ProcessTreeBudgetExceeded(RuntimeError):
    def __init__(self, cpu_seconds: float, cap_seconds: float, returncode: int | None,
                 controller_cpu_seconds: float = 0.0):
        super().__init__(f"process tree stopped at {cpu_seconds:.6f}s under {cap_seconds:.6f}s cap")
        self.cpu_seconds, self.cap_seconds, self.returncode = cpu_seconds, cap_seconds, returncode
        self.controller_cpu_seconds = controller_cpu_seconds


@dataclass(frozen=True)
class ProcessRun:
    argv: tuple[str, ...]
    returncode: int
    cpu_seconds: float
    wall_seconds: float
    stdout_path: str | None
    controller_cpu_seconds: float = 0.0


@dataclass(frozen=True)
class _Proc:
    pid: int
    ppid: int
    start: int
    ticks: int
    state: str


def _parse_stat(pid: int, raw: str) -> _Proc:
    close = raw.rfind(")")
    fields = raw[close + 1:].split()
    if close < 0 or len(fields) < 20 or int(raw[:raw.find(" ")]) != pid:
        raise ValueError("invalid /proc stat")
    utime, stime = int(fields[11]), int(fields[12])
    cutime, cstime = int(fields[13]), int(fields[14])
    return _Proc(pid, int(fields[1]), int(fields[19]),
                 sum(max(0, value) for value in (utime, stime, cutime, cstime)), fields[0])


def _table() -> dict[int, _Proc]:
    result = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as exc:
        raise ProcessTreeSafetyError("cannot enumerate Linux /proc") from exc
    for entry in entries:
        if entry.name.isdigit():
            try:
                result[int(entry.name)] = _parse_stat(int(entry.name), (entry / "stat").read_text())
            except FileNotFoundError:
                continue
            except PermissionError as exc:
                raise ProcessTreeSafetyError(f"cannot inspect process {entry.name}") from exc
            except (ValueError, OSError):
                continue
    return result


def _snapshot(root: int, root_start: int | None, controller: int, baseline: set[tuple[int, int]]):
    table = _table()
    root_proc = table.get(root)
    if root_proc and root_start is not None and root_proc.start != root_start:
        raise ProcessTreeSafetyError("worker PID was reused")
    selected: dict[int, _Proc] = {}
    pending = []
    if root_proc and (root_start is None or root_proc.start == root_start):
        selected[root] = root_proc
        pending.append(root)
    for process in table.values():
        if process.ppid == controller and (process.pid, process.start) not in baseline and process.pid != root:
            selected[process.pid] = process
            pending.append(process.pid)
    while pending:
        parent = pending.pop()
        for process in table.values():
            if process.ppid == parent and process.pid not in selected:
                selected[process.pid] = process
                pending.append(process.pid)
    identities = {(proc.pid, proc.start) for proc in selected.values()}
    cpu = sum(proc.ticks for proc in selected.values()) / os.sysconf("SC_CLK_TCK")
    live = sum(proc.state not in {"Z", "X"} for proc in selected.values())
    return cpu, identities, live


def _set_subreaper(enabled: bool) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    prior = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(prior), 0, 0, 0) != 0:
        raise ProcessTreeSafetyError("cannot query child-subreaper state")
    if libc.prctl(36, 1 if enabled else 0, 0, 0, 0) != 0:
        raise ProcessTreeSafetyError("cannot enable child-subreaper CPU accounting")
    return prior.value


def _reap(identities: set[tuple[int, int]], root: int):
    for pid, _ in identities:
        if pid != root:
            try:
                os.wait4(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            except OSError as exc:
                if exc.errno not in {errno.ECHILD, errno.EINTR}:
                    raise ProcessTreeSafetyError(f"cannot reap adopted process {pid}") from exc


def _signal(identities: set[tuple[int, int]], root: int, hard: bool):
    signum = signal.SIGKILL if hard else signal.SIGTERM
    try:
        os.killpg(root, signum)
    except ProcessLookupError:
        pass
    table = _table()
    for pid, start in identities:
        current = table.get(pid)
        if current and current.start == start:
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise ProcessTreeSafetyError(f"cannot signal process {pid}") from exc


def _self_cpu():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _children_cpu():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


class ProcessTreeWatchdog:
    """One serialized subprocess tree, sampled and killed before its reserved CPU cap."""
    def __init__(self, *, poll_seconds: float = 0.005, terminate_grace: float = 0.05, safety_margin: float = 0.10):
        if not 0.001 <= poll_seconds <= 0.1 or not 0 <= terminate_grace <= 1 or not 0 < safety_margin <= 5:
            raise ValueError("invalid watchdog polling, termination, or safety-margin setting")
        self.poll_seconds, self.terminate_grace, self.safety_margin = poll_seconds, terminate_grace, safety_margin

    @staticmethod
    def _within_worktree(path: Path):
        task_root = Path(__file__).resolve().parents[2]
        result = Path(path).resolve(strict=False)
        try:
            result.relative_to(task_root)
        except ValueError as exc:
            raise ValueError("watchdog cwd/output must stay inside the Issue 75 task worktree") from exc
        return result

    def _stop(self, proc, root_start, controller, baseline, identities):
        deadline = time.monotonic() + self.terminate_grace
        hard = False
        while True:
            _, current, live = _snapshot(proc.pid, root_start, controller, baseline)
            identities.update(current)
            _signal(identities, proc.pid, hard)
            _reap(identities, proc.pid)
            code = proc.poll()
            if code is not None and live == 0:
                return
            if not hard and time.monotonic() >= deadline:
                hard = True
                deadline = time.monotonic() + max(0.2, self.terminate_grace)
            elif hard and time.monotonic() >= deadline:
                raise ProcessTreeSafetyError("a worker descendant survived SIGKILL")
            time.sleep(min(self.poll_seconds, 0.01))

    def run(
        self, argv: Sequence[str], *, reserved_cpu_seconds: float, cwd: Path,
        env: Mapping[str, str] | None = None, stdout_path: Path | None = None,
        wall_timeout_seconds: float | None = None,
    ) -> ProcessRun:
        cap = float(reserved_cpu_seconds)
        if not argv or not math.isfinite(cap) or cap <= 0.05:
            raise ValueError("argv and a finite CPU cap above 0.05 seconds are required")
        if wall_timeout_seconds is not None and (not math.isfinite(wall_timeout_seconds) or wall_timeout_seconds <= 0):
            raise ValueError("wall timeout must be finite and positive")
        workdir = self._within_worktree(cwd)
        output = self._within_worktree(stdout_path) if stdout_path is not None else None
        controller = os.getpid()
        before_table = _table()
        baseline = {(p.pid, p.start) for p in before_table.values() if p.ppid == controller}
        prior_subreaper = _set_subreaper(True)
        self_before, children_before = _self_cpu(), _children_cpu()
        started = time.monotonic()
        stream = None
        proc = None
        root_start = None
        maximum_cpu = 0.0
        identities: set[tuple[int, int]] = set()
        prior_wall, prior_cpu, peak_rate = started, 0.0, 0.0
        try:
            if output is not None:
                output.parent.mkdir(parents=True, exist_ok=True)
                stream = os.fdopen(os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb")
            child_env = os.environ.copy()
            if env:
                child_env.update({str(k): str(v) for k, v in env.items()})
            proc = subprocess.Popen(
                [str(arg) for arg in argv], cwd=workdir, env=child_env,
                stdin=subprocess.DEVNULL, stdout=stream or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if stream else subprocess.DEVNULL,
                close_fds=True, start_new_session=True,
            )
            while root_start is None:
                root = _table().get(proc.pid)
                if root:
                    root_start = root.start
                elif proc.poll() is not None:
                    break
                else:
                    time.sleep(self.poll_seconds)

            while True:
                now = time.monotonic()
                sample, seen, live = _snapshot(proc.pid, root_start, controller, baseline)
                maximum_cpu = max(maximum_cpu, sample)
                identities.update(seen)
                dt = now - prior_wall
                if dt > 0:
                    peak_rate = max(peak_rate, max(0.0, sample - prior_cpu) / dt)
                self_delta = max(0.0, _self_cpu() - self_before)
                child_delta = max(0.0, _children_cpu() - children_before)
                measured = max(maximum_cpu, child_delta) + self_delta
                response = self.poll_seconds + self.terminate_grace
                margin = min(cap * 0.25, max(self.safety_margin, peak_rate * response))
                if measured + peak_rate * response >= cap - margin:
                    self._stop(proc, root_start, controller, baseline, identities)
                    actual = max(maximum_cpu, max(0.0, _children_cpu() - children_before))
                    actual += max(0.0, _self_cpu() - self_before)
                    raise ProcessTreeBudgetExceeded(actual, cap, proc.returncode, max(0.0, _self_cpu() - self_before))
                if wall_timeout_seconds is not None and now - started >= wall_timeout_seconds:
                    self._stop(proc, root_start, controller, baseline, identities)
                    actual = max(maximum_cpu, max(0.0, _children_cpu() - children_before))
                    actual += max(0.0, _self_cpu() - self_before)
                    raise ProcessTreeTimeout(actual, wall_timeout_seconds)
                code = proc.poll()
                _reap(identities, proc.pid)
                if code is not None:
                    # Reap of the root may just have caused its orphaned worker
                    # to be adopted by this subreaper. Take one post-wait scan
                    # before concluding the process tree is empty.
                    after, adopted, after_live = _snapshot(
                        proc.pid, root_start, controller, baseline,
                    )
                    maximum_cpu = max(maximum_cpu, after)
                    identities.update(adopted)
                    _reap(identities, proc.pid)
                    if after_live == 0:
                        break
                prior_wall, prior_cpu = now, sample
                time.sleep(self.poll_seconds)

            actual = max(maximum_cpu, max(0.0, _children_cpu() - children_before))
            actual += max(0.0, _self_cpu() - self_before)
            if actual > cap:
                raise ProcessTreeBudgetExceeded(actual, cap, proc.returncode, max(0.0, _self_cpu() - self_before))
            return ProcessRun(tuple(map(str, argv)), int(proc.returncode or 0), actual,
                              time.monotonic() - started, str(output) if output else None,
                              max(0.0, _self_cpu() - self_before))
        finally:
            try:
                if proc is not None and proc.poll() is None:
                    try:
                        self._stop(proc, root_start, controller, baseline, identities)
                    except BaseException as stop_error:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        try:
                            proc.wait(timeout=1.0)
                        except BaseException:
                            pass
                        raise ProcessTreeSafetyError("watchdog could not prove the process tree stopped") from stop_error
            finally:
                try:
                    if stream is not None:
                        stream.close()
                finally:
                    _set_subreaper(bool(prior_subreaper))
