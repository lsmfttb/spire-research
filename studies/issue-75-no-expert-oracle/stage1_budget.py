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
        or budget.get("ledger_status") != "PREFLIGHT_AUTHORIZED_ZERO_CHARGES"
        or budget.get("limits_cpu_seconds") != {
            "builder": BUILDER_LIMIT, "reviewer_protected": REVIEWER_LIMIT, "all_in": ALL_IN_LIMIT,
        }
        or budget.get("reviewer_reserve_transferable_to_builder") is not False
        or budget.get("search_call_cpu_cap_seconds") != SEARCH_CPU_CAP_SECONDS
        or budget.get("search_call_started_attempt_cap") != SEARCH_ATTEMPT_CAP
    ):
        raise PermissionError("Stage 1 budget does not match the registered hard limits")
    sidecar = (STAGE1 / "source-lock.sha256").read_text(encoding="ascii").split()
    lock_digest = _normalized_file_sha256(lock_path)
    if not sidecar or sidecar[0] != lock_digest:
        raise PermissionError("Stage 1 source-lock sidecar mismatch")
    return lock


def verify_stage1_phase(phase: str) -> None:
    """Refuse any phase not explicitly enabled by the locked Stage 1 package."""
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
    """Verify the package and bind ledger receipts to the current commit and lock."""
    verify_stage1_package()
    working_tree = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", "."], cwd=STUDY,
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if working_tree.returncode != 0:
        raise PermissionError("Stage 1 receipts require a clean tracked study checkout")
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
            runs[rid] = {"reservation": dict(e), "finish": None}
            units.setdefault(uid, []).append(rid)
        elif e.get("event") == "finish":
            rid = e.get("run_id")
            if rid not in runs or runs[rid]["finish"] is not None:
                raise LedgerCorrupt("orphan or duplicate finish event")
            runs[rid]["finish"] = dict(e)
        else:
            raise LedgerCorrupt(f"unknown event {e.get('event')!r}")
    return runs, units


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
                    data = canonical(event) + b"\n"
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
        runs, _ = _state(_events(self.events_path))
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
        return dict(reservation)

    @staticmethod
    def _charges(runs):
        result = {"builder": 0.0, "reviewer": 0.0}
        for run in runs.values():
            role = run["reservation"].get("role")
            if role not in result:
                raise LedgerCorrupt("unknown ledger role")
            result[role] += _charge(run)
        return result

    def reserve(
        self, *, run_id: str, unit_id: str, role: str, reserved_cpu_seconds: float,
        unit_kind: str, attempt_cap: int | None = None, previous_run_id: str | None = None,
        checkpoint_sha256: str | None = None,
    ) -> dict[str, Any]:
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

        def operation(rows):
            runs, units = _state(rows)
            if run_id in runs:
                raise DuplicateRun(run_id)
            history = units.get(unit_id, [])
            if history:
                last = runs[history[-1]]
                done = last["finish"]
                if done is None:
                    raise ResourceBlocked(f"unresolved reservation remains charged at full cap: {unit_id}")
                if done.get("status") != "incomplete":
                    raise DuplicateRun(f"terminal unit cannot be replayed: {unit_id}")
                if float(done.get("measured_cpu_seconds", 0.0)) > float(done.get("reserved_cpu_seconds", 0.0)):
                    raise ResourceBlocked("an incomplete unit exceeded its immutable reservation and cannot be resumed")
                if (
                    previous_run_id != history[-1] or
                    not done.get("checkpoint_sha256") or checkpoint_sha256 != done["checkpoint_sha256"] or
                    role != last["reservation"]["role"]
                ):
                    raise CheckpointMismatch("resume needs the exact prior incomplete run and checkpoint digest")
            elif previous_run_id is not None or checkpoint_sha256 is not None:
                raise CheckpointMismatch("resume metadata supplied for a new unit")

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
            event = {
                "event": "reserve", "schema_id": "issue75-stage1-budget-event-v2",
                "run_id": run_id, "unit_id": unit_id, "role": role, "unit_kind": unit_kind,
                "reserved_cpu_seconds": cap, "attempt_cap": attempt_cap,
                "previous_run_id": previous_run_id, "checkpoint_sha256": checkpoint_sha256,
                "package_commit": self.binding.package_commit,
                "source_lock_sha256": self.binding.source_lock_sha256,
                "created_at_ns": time.time_ns(),
            }
            return event, event
        return self._tx(operation)

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
    ):
        expected = list(expected_unit_ids)
        allowed = set(allowed_completed_unit_ids)
        if (not expected or len(set(expected)) != len(expected)
                or set(expected) != set(future_builder_cpu_seconds)
                or allowed & set(expected)):
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
            for unit in expected:
                history = units.get(unit, [])
                if not history:
                    future += estimates[unit]
                    continue
                last = runs[history[-1]]
                done = last["finish"]
                if done is None or done["status"] == "failed":
                    blocked.append(unit)
                elif done["status"] == "incomplete":
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
    def __init__(self, cpu_seconds: float, cap_seconds: float, returncode: int | None):
        super().__init__(f"process tree stopped at {cpu_seconds:.6f}s under {cap_seconds:.6f}s cap")
        self.cpu_seconds, self.cap_seconds, self.returncode = cpu_seconds, cap_seconds, returncode


@dataclass(frozen=True)
class ProcessRun:
    argv: tuple[str, ...]
    returncode: int
    cpu_seconds: float
    wall_seconds: float
    stdout_path: str | None


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
                    raise ProcessTreeBudgetExceeded(actual, cap, proc.returncode)
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
                raise ProcessTreeBudgetExceeded(actual, cap, proc.returncode)
            return ProcessRun(tuple(map(str, argv)), int(proc.returncode or 0), actual,
                              time.monotonic() - started, str(output) if output else None)
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
