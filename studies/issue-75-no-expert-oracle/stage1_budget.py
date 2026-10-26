"""Disabled Stage 1 activation gate, CPU ledger, and Linux process-tree watchdog.

This module is preparation-only; it never changes the checked-in disabled flag.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
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
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

OWNER_LOGIN = "lsmfttb"
REPOSITORY = "lsmfttb/spire-research"
ISSUE_NUMBER = 75
BUILDER_LIMIT = 170_000
REVIEWER_LIMIT = 10_000
ALL_IN_LIMIT = 180_000
SEARCH_CPU_CAP_SECONDS = 32.0
SEARCH_ATTEMPT_CAP = 16
REVIEW_MARKER = "STAGE1_REVIEW_PASS"
PLANNER_MARKER = "STAGE1_PLANNER_ACTIVATE"
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
STUDY = Path(__file__).resolve().parent
STAGE1 = STUDY / "stage1"
LOCKED_PACKAGE_FILES = (
    "config.json",
    "source-lock.json",
    "source-lock.sha256",
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
    "stage1/seed-table.json",
    "stage1/budget.json",
    "stage1/prepare.py",
    "stage1/test_activation.py",
    "stage1/test_activation_gate.py",
)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def verify_stage1_package() -> dict[str, Any]:
    """Verify the disabled package and every exact source-lock entry using only stdlib code."""
    lock_path = STAGE1 / "source-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if (
        lock.get("schema_id") != "issue75-stage1-source-lock-v1"
        or lock.get("study_id") != "issue-75-no-expert-oracle"
        or lock.get("scope") != "STUDY_ONLY"
        or lock.get("execution_authorized") is not False
        or lock.get("package_status") != "PREPARATION_ONLY_DISABLED"
        or lock.get("formal_seeds_or_games_consumed") != 0
        or lock.get("native_sampling_performed") is not False
        or lock.get("model_training_performed") is not False
    ):
        raise PermissionError("Stage 1 package is not the registered disabled preparation package")
    files = lock.get("locked_files_sha256_lf")
    if not isinstance(files, dict) or set(files) != set(LOCKED_PACKAGE_FILES):
        raise PermissionError("Stage 1 lock does not cover the exact registered package file set")
    for relative, expected in files.items():
        path = (STUDY / relative).resolve()
        if not path.is_relative_to(STUDY.resolve()) or not path.is_file():
            raise PermissionError(f"locked Stage 1 package file is unavailable: {relative}")
        if not isinstance(expected, str) or _normalized_file_sha256(path) != expected:
            raise PermissionError(f"Stage 1 package source lock mismatch: {relative}")

    config = json.loads((STAGE1 / "config.json").read_text(encoding="utf-8"))
    disabled = (
        "stage1_execution_authorized", "formal_training_enabled", "formal_evaluation_enabled",
        "nonformal_preflight_enabled", "native_sampling_enabled",
    )
    if any(config.get("execution", {}).get(key) is not False for key in disabled):
        raise PermissionError("a Stage 1 execution flag is not explicitly disabled")
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
    if budget.get("stage1_execution_authorized") is not False or budget.get("ledger_status") != "NOT_ACTIVATED_ZERO_CHARGES":
        raise PermissionError("Stage 1 budget must remain disabled and uncharged")
    sidecar = (STAGE1 / "source-lock.sha256").read_text(encoding="ascii").split()
    lock_digest = _normalized_file_sha256(lock_path)
    if not sidecar or sidecar[0] != lock_digest:
        raise PermissionError("Stage 1 source-lock sidecar mismatch")
    return lock


@dataclass(frozen=True)
class ActivationReceipt:
    package_commit: str
    source_lock_sha256: str
    reviewer_comment_id: int
    planner_comment_id: int
    authorized_at: str


def _fetch_authoritative_issue_comments() -> list[Mapping[str, Any]]:
    """Fetch all top-level Issue comments from GitHub; activation records are never caller-supplied."""
    base = f"https://api.github.com/repos/{REPOSITORY}/issues/{ISSUE_NUMBER}/comments"
    comments: list[Mapping[str, Any]] = []
    page = 1
    while True:
        request = Request(
            f"{base}?per_page=100&page={page}",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "spire-research-issue75-activation-gate",
            },
        )
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
        if not isinstance(payload, list) or any(not isinstance(row, Mapping) for row in payload):
            raise PermissionError("GitHub returned an invalid Issue comment response")
        comments.extend(payload)
        if len(payload) < 100:
            break
        page += 1
    return comments


def _verify_activation_comments(
    comments: Sequence[Mapping[str, Any]], *,
    package_commit: str, source_lock_sha256: str,
    repository: str = REPOSITORY, issue_number: int = ISSUE_NUMBER,
) -> ActivationReceipt:
    """Select ordered exact-source decisions from authoritative Issue comment records."""
    if repository != REPOSITORY or issue_number != ISSUE_NUMBER:
        raise PermissionError("activation is bound to lsmfttb/spire-research#75")
    if not isinstance(package_commit, str) or len(package_commit) != 40 or any(c not in "0123456789abcdef" for c in package_commit):
        raise ValueError("invalid package commit SHA")
    if not isinstance(source_lock_sha256, str) or len(source_lock_sha256) != 64 or any(c not in "0123456789abcdef" for c in source_lock_sha256):
        raise ValueError("invalid source-lock SHA256")
    if not isinstance(comments, Sequence) or isinstance(comments, (str, bytes)):
        raise PermissionError("GitHub Issue comments must be a sequence of records")

    def check(comment: Mapping[str, Any], marker: str) -> tuple[int, datetime]:
        comment_id = comment.get("id")
        if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id <= 0:
            raise PermissionError("decision comment has no valid GitHub comment ID")
        url = comment.get("html_url")
        if not isinstance(url, str):
            raise PermissionError("decision comment has no GitHub URL")
        parsed_url = urlsplit(url)
        expected_path = f"/{REPOSITORY}/issues/{ISSUE_NUMBER}"
        if (
            parsed_url.scheme != "https" or parsed_url.netloc != "github.com"
            or parsed_url.path != expected_path or parsed_url.query
            or parsed_url.fragment != f"issuecomment-{comment_id}"
        ):
            raise PermissionError("decision comment URL does not identify its exact Issue 75 comment ID")
        user, body, created = comment.get("user"), comment.get("body"), comment.get("created_at")
        if not isinstance(user, Mapping) or user.get("login") != OWNER_LOGIN or user.get("type") != "User":
            raise PermissionError("comment is not from the configured project owner")
        if not isinstance(body, str) or not {
            marker, f"package_commit: {package_commit}",
            f"source_lock_sha256: {source_lock_sha256}", "scope: STUDY_ONLY",
        }.issubset({line.strip() for line in body.splitlines()}):
            raise PermissionError(f"comment does not bind {marker}, exact source, lock, and STUDY_ONLY scope")
        if not isinstance(created, str):
            raise PermissionError("decision comment has no GitHub timestamp")
        try:
            timestamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PermissionError("decision comment has an invalid GitHub timestamp") from exc
        if timestamp.tzinfo is None:
            raise PermissionError("decision comment timestamp lacks timezone")
        updated = comment.get("updated_at")
        if not isinstance(updated, str):
            raise PermissionError("decision comment has no GitHub edit timestamp")
        try:
            updated_timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PermissionError("decision comment has an invalid GitHub edit timestamp") from exc
        if updated_timestamp.tzinfo is None or updated_timestamp < timestamp:
            raise PermissionError("decision comment edit timestamp is invalid")
        return comment_id, timestamp, updated_timestamp

    def marked(marker: str) -> list[Mapping[str, Any]]:
        return [
            row for row in comments
            if isinstance(row, Mapping) and isinstance(row.get("body"), str)
            and marker in {line.strip() for line in row["body"].splitlines()}
        ]

    reviewers: list[tuple[int, datetime, datetime]] = []
    planners: list[tuple[int, datetime, datetime]] = []
    for row in marked(REVIEW_MARKER):
        try:
            reviewers.append(check(row, REVIEW_MARKER))
        except (PermissionError, ValueError, TypeError):
            continue
    for row in marked(PLANNER_MARKER):
        try:
            planners.append(check(row, PLANNER_MARKER))
        except (PermissionError, ValueError, TypeError):
            continue

    valid_pairs = [
        (reviewer_id, planner_id, activated_at)
        for reviewer_id, reviewed_at, reviewer_updated_at in reviewers
        for planner_id, activated_at, _planner_updated_at in planners
        if reviewer_id != planner_id and activated_at > reviewed_at
        and reviewer_updated_at <= activated_at
    ]
    if not valid_pairs:
        raise PermissionError(
            "no authoritative ordered Reviewer PASS and later Planner GO comments bind the current package, lock, and STUDY_ONLY scope"
        )
    reviewer_id, planner_id, activated_at = max(valid_pairs, key=lambda pair: pair[2])
    return ActivationReceipt(
        package_commit, source_lock_sha256, reviewer_id, planner_id, activated_at.isoformat(),
    )


def _current_package_binding() -> tuple[str, str]:
    """Verify the locked package and bind activation to this checkout's commit and lock digest."""
    verify_stage1_package()
    working_tree = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", "."], cwd=STUDY,
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if working_tree.returncode != 0:
        raise PermissionError("Stage 1 activation requires a clean tracked study checkout")
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


def _live_activation_receipt() -> ActivationReceipt:
    package_commit, lock_digest = _current_package_binding()
    comments = _fetch_authoritative_issue_comments()
    if _current_package_binding() != (package_commit, lock_digest):
        raise PermissionError("Stage 1 package changed while authoritative activation comments were fetched")
    return _verify_activation_comments(
        comments, package_commit=package_commit, source_lock_sha256=lock_digest,
    )


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
    """Append-only write-ahead accounting; construction always fetches and verifies live activation."""
    def __init__(self, directory: Path, activation: ActivationReceipt | None = None):
        if activation is not None:
            raise PermissionError("caller-supplied activation receipts cannot authorize ledger construction")
        self.activation = _live_activation_receipt()
        self.directory = Path(directory)
        self.events_path = self.directory / "events.jsonl"
        self.lock_path = self.directory / "events.lock"
        self.thread_lock = threading.RLock()

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
                "event": "reserve", "schema_id": "issue75-stage1-budget-event-v1",
                "run_id": run_id, "unit_id": unit_id, "role": role, "unit_kind": unit_kind,
                "reserved_cpu_seconds": cap, "attempt_cap": attempt_cap,
                "previous_run_id": previous_run_id, "checkpoint_sha256": checkpoint_sha256,
                "package_commit": self.activation.package_commit,
                "source_lock_sha256": self.activation.source_lock_sha256, "created_at_ns": time.time_ns(),
            }
            return event, event
        return self._tx(operation)

    def finish(
        self, *, run_id: str, measured_cpu_seconds: float, status: str,
        checkpoint_sha256: str | None = None, started_attempts: int = 0,
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

        def operation(rows):
            runs, _ = _state(rows)
            run = runs.get(run_id)
            if run is None or run["finish"] is not None:
                raise DuplicateRun(f"no open reservation: {run_id}")
            reservation = run["reservation"]
            event = {
                "event": "finish", "schema_id": "issue75-stage1-budget-event-v1",
                "run_id": run_id, "unit_id": reservation["unit_id"], "status": status,
                "measured_cpu_seconds": measured, "reserved_cpu_seconds": reservation["reserved_cpu_seconds"],
                "started_attempts": started_attempts, "checkpoint_sha256": checkpoint_sha256,
                "finished_at_ns": time.time_ns(), "package_commit": self.activation.package_commit,
                "source_lock_sha256": self.activation.source_lock_sha256,
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
                "package_commit": self.activation.package_commit,
                "source_lock_sha256": self.activation.source_lock_sha256,
            }
            if result["status"] != "FITS":
                raise ResourceBlocked(canonical(result).decode())
            return result, None
        return self._tx(operation)

    def read_events(self):
        return self._tx(lambda rows: (_state(rows) and rows, None))


def open_activated_stage1_ledger(directory: Path) -> Stage1Ledger:
    """Open the sole Stage 1 ledger gate after live GitHub and checkout binding checks."""
    return Stage1Ledger(directory)


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
