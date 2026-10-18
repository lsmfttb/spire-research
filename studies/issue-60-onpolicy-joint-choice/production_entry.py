"""Single STUDY_ONLY production entry for the registered Issue 60 trial.

The frozen research source continues to own controller and PPO semantics. This
entry installs the existing in-memory accounting hooks and fixed-key worker
dispatcher, then resumes the registered training/evaluation stages. The
default mode stops at the batch-02 integration-review boundary; the explicit
``--continue-reviewed`` option resumes the authorized fixed program there.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


ENTRY_PROCESS_CPU_STARTED = time.process_time()

import torch

import controllers
import encoder
import guard_optimization
import parallel_execution
import resource_amendment_r2
import run_study
import run_study_r2
import trainer


HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "evidence"
PROGRESS_PATH = EVIDENCE / "execution" / "production-progress.json"
EVENTS_PATH = EVIDENCE / "execution" / "production-events.jsonl"
MILESTONE_PATH = EVIDENCE / "execution" / "integration-milestone.json"
ISSUE_NUMBER = 60
REPOSITORY = "lsmfttb/spire-research"
HISTORICAL_ADMIN_CPU_SECONDS = 25_644.469
ACCEPTED_INTERRUPTION_DEBIT_SECONDS = 1_572.370806919
NEW_BUILDER_CPU_LIMIT_SECONDS = 150_000.0
INTEGRATION_CPU_LIMIT_SECONDS = 3_000.0
INTEGRATION_MAX_WORKERS = 2
PARENT_INTERRUPTION_RESERVE_SECONDS = 60.0
GAME_RESERVE_MULTIPLIER = 1.25
OPTIMIZER_RESERVE_MULTIPLIER = 2.0
INITIAL_OPTIMIZER_RESERVE_SECONDS = 900.0
EXPECTED_BATCH01_SHA = "c05ffb91ed6cb702e9ad135afe3b537c1348f2da8e21958c38141404055bfbb5"
MISSING_BATCH02_SEEDS = (660248, 660250, 660251, 660252, 660253, 660254, 660255, 660256)


class ProductionError(RuntimeError):
    """A correctness, routing, or runtime failure in the production path."""


class ProspectiveBudgetStop(ProductionError):
    """The actual budget cannot safely reserve the next bounded operation."""


class IntegrationMilestoneReached(Exception):
    """Batch 02 completed; stop for the required independent integration review."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    try:
        directory = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory)
    except OSError:
        pass
    finally:
        os.close(directory)


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def verify_active_sources_once(rs: Any) -> dict[str, Any]:
    """Validate registered config and committed current study source at startup.

    The historical frozen-manifest hash for run_study.py is stale on the
    accepted study branch. Keep all other manifest checks and verify the
    current orchestration source against the exact Git tree object instead.
    """
    manifest_path = rs.HERE / "frozen-manifest.json"
    if not manifest_path.is_file():
        raise ProductionError("registered study manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionError("registered study manifest is unreadable") from exc
    if manifest.get("schema_id") != "issue60-frozen-manifest-v1":
        raise ProductionError("registered study manifest schema changed")
    if manifest.get("config_sha256") != rs.config_sha():
        raise ProductionError("registered config differs from the frozen manifest")
    config = json.loads(rs.CONFIG_PATH.read_text(encoding="utf-8"))
    if (manifest.get("source_pins") != config.get("sources")
            or manifest.get("base_commit") != config.get("base", {}).get("commit")):
        raise ProductionError("manifest source/base pins disagree with the registered config")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ProductionError("registered manifest has no file identity list")
    paths = [entry.get("path") for entry in entries if isinstance(entry, Mapping)]
    if len(paths) != len(entries) or set(paths) != set(rs.FROZEN_FILES) or len(paths) != len(set(paths)):
        raise ProductionError("registered manifest file set differs from the frozen study source list")
    stale_manifest_run_study_sha: str | None = None
    for entry in entries:
        relative = str(entry["path"])
        source = rs.HERE / relative
        if not source.is_file():
            raise ProductionError(f"registered study source is missing: {relative}")
        current_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        if relative == "run_study.py":
            stale_manifest_run_study_sha = str(entry["sha256"])
            continue
        if current_sha != entry.get("sha256"):
            raise ProductionError(f"registered frozen scientific input changed: {relative}")

    root_result = _run_counted(["git", "rev-parse", "--show-toplevel"], cwd=HERE)
    head_result = _run_counted(["git", "rev-parse", "HEAD"], cwd=HERE)
    if root_result.returncode != 0 or head_result.returncode != 0:
        raise ProductionError("could not resolve the current source tree identity")
    repo_root = Path(root_result.stdout.strip()).resolve()
    _, committed_run_study = _verify_committed_source(
        repo_root, (rs.HERE / "run_study.py").resolve(), source_name="run_study.py",
    )
    relative_entry, committed_entry = _verify_committed_source(
        repo_root, Path(__file__).resolve(), source_name="production_entry.py",
    )
    optimizer_path = HERE / "public_history_execution.py"
    relative_optimizer, committed_optimizer = _verify_committed_source(
        repo_root, optimizer_path, source_name="public_history_execution.py",
    )
    return {
        "manifest_schema_id": manifest["schema_id"],
        "config_sha256": rs.config_sha(),
        "base_commit": config["base"]["commit"],
        "source_pins": dict(config["sources"]),
        "current_run_study_git_blob": committed_run_study,
        "current_run_study_sha256": hashlib.sha256((rs.HERE / "run_study.py").read_bytes()).hexdigest(),
        "current_production_entry_git_blob": committed_entry,
        "current_production_entry_sha256": _sha256(Path(__file__).resolve()),
        "current_production_entry_path": relative_entry,
        "current_public_history_optimizer_git_blob": committed_optimizer,
        "current_public_history_optimizer_sha256": _sha256(optimizer_path),
        "current_public_history_optimizer_path": relative_optimizer,
        "historical_manifest_run_study_sha256": stale_manifest_run_study_sha,
        "historical_manifest_run_study_sha_stale": (
            hashlib.sha256((rs.HERE / "run_study.py").read_bytes()).hexdigest()
            != stale_manifest_run_study_sha
        ),
        "other_frozen_files_verified": len(entries) - 1,
        "verification": "current HEAD tree identity for run_study.py, production_entry.py, and the study-local public-history optimizer plus registered config and unchanged remaining manifest inputs",
    }


def _verify_committed_source(repo_root: Path, source: Path, *, source_name: str) -> tuple[str, str]:
    """Fail closed unless a launched Python source file matches its HEAD blob."""
    try:
        relative = Path(source).resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError as exc:
        raise ProductionError(f"{source_name} is outside the reviewed repository tree") from exc
    committed = _run_counted(["git", "rev-parse", f"HEAD:{relative}"], cwd=repo_root)
    working = _run_counted(
        ["git", "hash-object", "--path", relative, str(Path(source).resolve())], cwd=repo_root,
    )
    if committed.returncode != 0 or working.returncode != 0:
        raise ProductionError(f"could not verify {source_name} against the committed source tree")
    committed_blob = committed.stdout.strip()
    if not committed_blob or committed_blob != working.stdout.strip():
        raise ProductionError(f"current {source_name} bytes differ from the committed source tree")
    return relative, committed_blob


def _run_counted(argv: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    environment = None
    if argv and argv[0] == "git":
        environment = os.environ.copy()
        environment.update(_worktree_git_environment())
    result = subprocess.run(argv, cwd=cwd, env=environment, check=False, capture_output=True, text=True)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    _EXTERNAL_CHILD_CPU[0] += max(0.0, (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime))
    return result


def _run_counted_external(argv: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Meter a subprocess outside the Windows-backed production worktree."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = subprocess.run(argv, cwd=cwd, check=False, capture_output=True, text=True)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    _EXTERNAL_CHILD_CPU[0] += max(0.0, (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime))
    return result


_EXTERNAL_CHILD_CPU = [0.0]


def _worktree_git_environment() -> dict[str, str]:
    """Translate a Windows Git worktree pointer when this entry runs under WSL."""
    repository_root = HERE.parents[1].resolve()
    dot_git = repository_root / ".git"
    if dot_git.is_dir():
        return {"GIT_WORK_TREE": str(repository_root)}
    if not dot_git.is_file():
        raise ProductionError("repository .git metadata is missing")
    first, separator, target = dot_git.read_text(encoding="utf-8").strip().partition(":")
    if not separator or first.strip().lower() != "gitdir":
        raise ProductionError("repository .git worktree pointer is invalid")
    target = target.strip()
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", target)
    if match:
        worktree_git_dir = Path(f"/mnt/{match.group(1).lower()}/{match.group(2).replace(chr(92), '/')}")
    else:
        worktree_git_dir = Path(target)
        if not worktree_git_dir.is_absolute():
            worktree_git_dir = (dot_git.parent / worktree_git_dir).resolve()
    if not worktree_git_dir.is_dir():
        raise ProductionError(f"worktree Git metadata path is unavailable: {worktree_git_dir}")
    return {
        "GIT_DIR": str(worktree_git_dir),
        "GIT_WORK_TREE": str(repository_root),
    }


def _gh_executable() -> str:
    linux = shutil.which("gh")
    if linux:
        return linux
    wsl_path = Path("/mnt/c/Program Files/GitHub CLI/gh.exe")
    if wsl_path.is_file():
        return str(wsl_path)
    raise ProductionError("GitHub CLI is unavailable; issue routing must be checked before execution")


def _issue_state() -> set[str]:
    result = _run_counted([
        _gh_executable(), "issue", "view", str(ISSUE_NUMBER), "--repo", REPOSITORY,
        "--json", "labels",
    ])
    if result.returncode != 0:
        raise ProductionError(f"could not read Issue #{ISSUE_NUMBER} routing: {result.stderr.strip()}")
    try:
        raw = json.loads(result.stdout)
        labels = {str(row["name"]) for row in raw.get("labels", [])}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProductionError("GitHub CLI returned an invalid Issue routing response") from exc
    return labels


def parse_production_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the registered Issue 60 study.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--continue-reviewed", action="store_true",
        help=(
            "continue the already authorized study from a valid batch-02 checkpoint; "
            "Issue comments are not consulted"
        ),
    )
    mode.add_argument(
        "--single-key-witness", action="store_true",
        help=(
            "execute only the next missing fixed training key through the production runtime, "
            "then stop before PPO or evaluation"
        ),
    )
    return parser.parse_args(argv)


def execution_phase(completed_batch: int, labels: set[str], *, continue_reviewed: bool = False,
                    single_key_witness: bool = False) -> str:
    """Select the registered run phase; comment text never grants execution authority."""
    if single_key_witness:
        if "route:builder" not in labels:
            raise ProductionError("--single-key-witness requires Issue #60 to be routed to Builder")
        if completed_batch < 2:
            raise ProductionError("--single-key-witness requires the accepted training continuation boundary")
        return "single-key-witness"
    if continue_reviewed:
        if "route:builder" not in labels:
            raise ProductionError("--continue-reviewed requires Issue #60 to be routed to Builder")
        if completed_batch < 2:
            raise ProductionError("--continue-reviewed requires a valid completed batch-02 checkpoint")
        return "full-continuation"
    if "route:builder" not in labels:
        return "awaiting-review"
    if completed_batch == 1:
        return "integration-milestone"
    if completed_batch >= 2:
        return "awaiting-review"
    raise ProductionError(f"unexpected block1 checkpoint boundary: completed_batches={completed_batch}")


def run_full_training_continuation(rs: Any, config: Mapping[str, Any], vocab: Any,
                                   bootstrap: Any, runtime: Mapping[str, Any],
                                   stage_started: float) -> list[dict[str, Any]]:
    """Run both registered training blocks with the installed runtime hooks."""
    return [
        rs.train_block(block, config, vocab, bootstrap, runtime, stage_started)
        for block in ("block1", "block2")
    ]


def run_full_continuation_stages(rs: Any, config: Mapping[str, Any], vocab: Any,
                                 bootstrap: Any, runtime: Mapping[str, Any],
                                 stage_started: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the registered full continuation through training, evaluation, and analysis."""
    blocks = run_full_training_continuation(
        rs, config, vocab, bootstrap, runtime, stage_started,
    )
    training_result = {
        "schema_id": "issue60-training-result-v1", "status": "COMPLETE",
        "config_sha256": rs.config_sha(), "blocks": blocks,
        "source_pins": dict(config["sources"]),
    }
    rs.atomic_json(EVIDENCE / "training" / "result.json", training_result)
    evaluation = rs.run_evaluation(config, runtime, vocab, bootstrap, stage_started)
    analysis = rs.analyze(config)
    return evaluation, analysis


def can_dispatch_with_reserves(*, actual_cpu: float, cpu_limit: float,
                               active_or_pending_workers: int, game_reserve: float,
                               optimizer_reserve: float, parent_reserve: float) -> bool:
    values = (actual_cpu, cpu_limit, game_reserve, optimizer_reserve, parent_reserve)
    if any(not math.isfinite(value) or value < 0 for value in values) or active_or_pending_workers < 0:
        raise ValueError("CPU accounting reserves must be finite and nonnegative")
    return (actual_cpu + active_or_pending_workers * game_reserve
            + optimizer_reserve + parent_reserve) < cpu_limit


def select_milestone_worker_count(*, preferred_workers: int, actual_cpu: float,
                                  game_reserve: float, optimizer_reserve: float,
                                  parent_reserve: float) -> int:
    """Bound batch-02 integration parallelism before the dispatcher starts."""
    if preferred_workers < 1:
        raise ValueError("preferred worker count must be positive")
    limit = min(int(preferred_workers), INTEGRATION_MAX_WORKERS)
    for count in range(limit, 0, -1):
        if can_dispatch_with_reserves(
            actual_cpu=actual_cpu, cpu_limit=INTEGRATION_CPU_LIMIT_SECONDS,
            active_or_pending_workers=count, game_reserve=game_reserve,
            optimizer_reserve=optimizer_reserve, parent_reserve=parent_reserve,
        ):
            return count
    raise ProspectiveBudgetStop(
        "batch-02 integration cannot safely reserve one worker and the pending PPO update "
        "inside the 3000s CPU cap"
    )


class ProductionProgress:
    """Durable coarse state; detailed completed runs remain in their run.json files."""

    def __init__(self, base: Path, *, config_sha: str):
        self.base = Path(base)
        self.config_sha = config_sha
        self.data: dict[str, Any] = {
            "schema_id": "issue60-production-progress-v1",
            "purpose": "STUDY_ONLY",
            "config_sha256": config_sha,
            "measured_new_cpu_seconds": 0.0,
            "administrative_debit_cpu_seconds": 0.0,
            "administrative_debits": [],
            "historical_admin_debit_cpu_seconds": 0.0,
            "historical_admin_debits": [],
            "open_invocation": None,
            "open_optimizer_reservation": None,
            "open_worker_reservations": {},
            "phase": "created",
            "events_written": 0,
        }
        if PROGRESS_PATH.is_file():
            try:
                saved = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProductionError(f"production progress file is unreadable: {PROGRESS_PATH}") from exc
            if saved.get("schema_id") != self.data["schema_id"] or saved.get("config_sha256") != config_sha:
                raise ProductionError("production progress does not match the registered study config")
            self.data.update(saved)
        self._recover_interrupted_reservations()

    def _recover_interrupted_reservations(self) -> None:
        rows: list[dict[str, Any]] = []
        invocation = self.data.get("open_invocation")
        if isinstance(invocation, Mapping):
            rows.append({
                "kind": "interrupted-parent-invocation",
                "seconds": float(invocation.get("reservation_seconds", PARENT_INTERRUPTION_RESERVE_SECONDS)),
                "reason": "hard interruption left unmeasured parent/process interval",
            })
        optimizer = self.data.get("open_optimizer_reservation")
        if isinstance(optimizer, Mapping):
            rows.append({
                "kind": "interrupted-optimizer-reservation",
                "seconds": float(optimizer.get("reservation_seconds", 0.0)),
                "block": optimizer.get("block"), "batch_index": optimizer.get("batch_index"),
                "reason": "hard interruption left optimizer work interval unmeasured",
            })
        workers = self.data.get("open_worker_reservations", {})
        if isinstance(workers, Mapping):
            for reservation in workers.values():
                if not isinstance(reservation, Mapping):
                    continue
                rows.append({
                    "kind": "interrupted-worker-reservation",
                    "seconds": float(reservation.get("reservation_seconds", 0.0)),
                    "task_key": reservation.get("task_key"),
                    "pid": reservation.get("pid"),
                    "reason": "hard interruption left worker CPU interval unmeasured",
                })
        if rows:
            self.add_administrative_debits(rows)
            self.data["open_invocation"] = None
            self.data["open_optimizer_reservation"] = None
            self.data["open_worker_reservations"] = {}
            self.data["phase"] = "resumed-after-interruption"
            self.persist()

    def add_administrative_debits(self, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            amount = float(row.get("seconds", 0.0))
            if not math.isfinite(amount) or amount < 0:
                raise ProductionError("interruption reservation debit is invalid")
            self.data["administrative_debit_cpu_seconds"] = (
                float(self.data["administrative_debit_cpu_seconds"]) + amount
            )
            self.data["administrative_debits"].append(dict(row))

    def begin_invocation(self, *, head: str, launcher_sha: str,
                         runtime: Mapping[str, Any], parent_reserve: float) -> None:
        if self.data.get("open_invocation") is not None:
            raise ProductionError("another production invocation is already marked open")
        self.data["open_invocation"] = {
            "started_utc": _utc_now(), "head": head, "launcher_sha256": launcher_sha,
            "reservation_seconds": parent_reserve,
        }
        self.data["runtime"] = dict(runtime)
        self.data["phase"] = "startup-validated"
        self.persist()

    def event(self, kind: str, *, actual_cpu: float, **details: Any) -> None:
        row = {
            "schema_id": "issue60-production-event-v1", "event": kind,
            "timestamp_utc": _utc_now(), "measured_new_cpu_seconds": actual_cpu,
            "administrative_debit_cpu_seconds": float(self.data["administrative_debit_cpu_seconds"]),
            "historical_admin_debit_cpu_seconds": float(self.data["historical_admin_debit_cpu_seconds"]),
            **details,
        }
        _append_jsonl(EVENTS_PATH, row)
        self.data["events_written"] = int(self.data.get("events_written", 0)) + 1
        self.persist(measured_cpu_seconds=actual_cpu)

    def persist(self, *, measured_cpu_seconds: float | None = None) -> None:
        if measured_cpu_seconds is not None:
            if not math.isfinite(measured_cpu_seconds) or measured_cpu_seconds < 0:
                raise ProductionError("measured production CPU is invalid")
            self.data["measured_new_cpu_seconds"] = measured_cpu_seconds
        self.data["accounted_new_cpu_seconds"] = (
            float(self.data["measured_new_cpu_seconds"])
            + float(self.data["administrative_debit_cpu_seconds"])
        )
        _atomic_json(PROGRESS_PATH, self.data)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Meter:
    def __init__(self, *, progress: ProductionProgress,
                 tracker: parallel_execution.ProcessCpuTracker,
                 setup_parent_cpu: float, setup_external_cpu: float):
        self.progress = progress
        self.tracker = tracker
        self.setup_parent_cpu = max(0.0, setup_parent_cpu)
        self.setup_external_cpu = max(0.0, setup_external_cpu)
        self.base_measured_cpu = float(progress.data["measured_new_cpu_seconds"])

    def actual_cpu(self) -> float:
        return (self.base_measured_cpu + self.setup_parent_cpu + self.setup_external_cpu
                + self.tracker.current_stage_cpu())

    def accounted_cpu(self) -> float:
        return self.actual_cpu() + float(self.progress.data["administrative_debit_cpu_seconds"])


class DurableDispatcher(parallel_execution.ParallelGameDispatcher):
    def __init__(self, *args: Any, owner: "ProductionRuntime", **kwargs: Any):
        self.owner = owner
        super().__init__(*args, **kwargs)

    def _training_tasks(self, template: Mapping[str, Any]) -> list[dict[str, Any]]:
        tasks = super()._training_tasks(template)
        seed = self.owner.single_key_witness_seed
        if seed is None:
            return tasks
        stage_key = str(template.get("stage_key", ""))
        if stage_key != self.owner.single_key_witness_stage:
            raise ProductionError("single-key witness attempted to dispatch outside its exact training stage")
        selected = [task for task in tasks if int(task["kwargs"]["seed"]) == seed]
        if len(selected) != 1:
            raise ProductionError(
                f"single-key witness expected exactly one missing task for seed {seed}, got {len(selected)}"
            )
        return selected

    def _fork_task(self, task: Mapping[str, Any], slot: int) -> int:
        key = tuple(task["key"])
        kwargs = task["kwargs"]
        token = json.dumps(list(key), separators=(",", ":"))
        reserve = self.owner.game_reserve_seconds()
        reservation = {
            "task_key": list(key), "pid": None,
            "output_dir": str(Path(kwargs["output_dir"]).resolve()),
            "model_sha256": str(kwargs["model_sha"]),
            "config_sha256": str(kwargs["config_digest"]),
            "reservation_seconds": reserve,
            "reserved_utc": _utc_now(),
        }
        self.owner.progress.data["open_worker_reservations"][token] = reservation
        self.owner.progress.event(
            "worker-reservation", actual_cpu=self.owner.meter.actual_cpu(),
            task_key=list(key), reservation_seconds=reserve,
        )
        try:
            pid = super()._fork_task(task, slot)
        except BaseException:
            self.owner.progress.data["open_worker_reservations"].pop(token, None)
            self.owner.progress.persist(measured_cpu_seconds=self.owner.meter.actual_cpu())
            raise
        reservation["pid"] = int(pid)
        self.owner._worker_tokens[str(pid)] = token
        self.owner.progress.persist(measured_cpu_seconds=self.owner.meter.actual_cpu())
        return pid


class ProductionRuntime:
    def __init__(self, *, progress: ProductionProgress, state: guard_optimization.GuardState,
                 tracker: parallel_execution.ProcessCpuTracker, meter: Meter,
                 worker_selection: Mapping[str, Any], milestone_mode: bool):
        self.progress = progress
        self.state = state
        self.tracker = tracker
        self.meter = meter
        self.worker_selection = dict(worker_selection)
        self.milestone_mode = milestone_mode
        self.optimizer_reserve_seconds = 0.0
        self.single_key_witness_seed: int | None = None
        self.single_key_witness_stage: str | None = None
        self.dispatcher: DurableDispatcher | None = None
        self._worker_tokens: dict[str, str] = {}
        self.batch_validation: dict[str, Any] | None = None
        self.milestone_checkpoint_sha: str | None = None

    def game_reserve_seconds(self) -> float:
        return max(60.0, float(self.state.max_observed_game_cpu_seconds) * GAME_RESERVE_MULTIPLIER)

    def cpu_limit(self) -> float:
        if self.milestone_mode:
            return min(NEW_BUILDER_CPU_LIMIT_SECONDS, INTEGRATION_CPU_LIMIT_SECONDS)
        return NEW_BUILDER_CPU_LIMIT_SECONDS

    def check_budget(self, *, workers: int | None = None,
                     optimizer_reserve: float | None = None) -> None:
        actual = self.meter.accounted_cpu()
        active = self.tracker.active_worker_count() if workers is None else workers
        optimizer = self.optimizer_reserve_seconds if optimizer_reserve is None else optimizer_reserve
        parent = float(self.progress.data.get("open_invocation", {}).get(
            "reservation_seconds", PARENT_INTERRUPTION_RESERVE_SECONDS,
        ))
        if not can_dispatch_with_reserves(
            actual_cpu=actual, cpu_limit=self.cpu_limit(),
            active_or_pending_workers=active, game_reserve=self.game_reserve_seconds(),
            optimizer_reserve=optimizer, parent_reserve=parent,
        ):
            raise ProspectiveBudgetStop(
                f"actual prospective CPU {actual:.3f}s cannot safely reserve the next operation "
                f"inside the {self.cpu_limit():.0f}s cap (workers={active}, "
                f"game_reserve={self.game_reserve_seconds():.3f}s each, "
                f"optimizer_reserve={optimizer:.3f}s, parent_reserve={parent:.3f}s)"
            )

    def persist_event(self, kind: str, **details: Any) -> None:
        self.progress.event(kind, actual_cpu=self.meter.actual_cpu(), **details)

    def worker_settled(self, pid: int, seconds: float, slot: int | None) -> None:
        token = self._worker_tokens.pop(str(pid), None)
        reservation = None
        if token is not None:
            reservation = self.progress.data["open_worker_reservations"].pop(token, None)
        if reservation is not None:
            output = Path(str(reservation["output_dir"]))
            completed = run_study.successful_attempt(
                output, str(reservation["model_sha256"]), str(reservation["config_sha256"]),
            )
            if completed is not None:
                self.state.register_complete(*completed)
            self.persist_event(
                "worker-settled", pid=pid, slot=slot, task_key=reservation["task_key"],
                settled_worker_cpu_seconds=seconds,
                completed_keys=self.state.completed_run_count(),
            )
        else:
            self.persist_event(
                "worker-settled-unmatched", pid=pid, slot=slot,
                settled_worker_cpu_seconds=seconds,
            )

    def install_cache(self, *, rs: Any, resource_module: Any,
                      parallel_module: Any) -> None:
        guard_optimization.install_cached_guard(
            rs, run_study_r2, resource_module, self.state,
            parallel_module=parallel_module,
            on_worker_settlement=self.worker_settled,
        )
        self._cached_raw_game = rs.run_game
        self._cached_atomic_json = rs.atomic_json
        self._cached_count = rs._completed_run_count
        self._cached_projection = rs._remaining_optimizer_projection
        self._cached_r2_complete = resource_module.complete_game_costs
        self._cached_r2_ledger = resource_module._ledger_total

        progress_atomic = rs.atomic_json

        def tracked_atomic(path: Path, value: Mapping[str, Any]) -> Any:
            result = progress_atomic(path, value)
            candidate = Path(path)
            if candidate.parent.resolve() == (EVIDENCE / "training" / "block1").resolve() \
                    and candidate.name.startswith("batch-") and candidate.suffix == ".json":
                self.state.register_batch_json(candidate)
                self.persist_event(
                    "optimizer-checkpoint-summary", block="block1",
                    batch_summary=str(candidate.relative_to(HERE)),
                    checkpoint_sha256=_sha256(EVIDENCE / "training" / "block1" / "latest.pt"),
                    completed_batches=self.state.completed_run_count(),
                )
            return result

        rs.atomic_json = tracked_atomic
        original_save = rs.atomic_torch_save

        def tracked_checkpoint_save(path: Path, value: Mapping[str, Any]) -> Any:
            result = original_save(path, value)
            candidate = Path(path)
            if candidate.name == "latest.pt" and candidate.parent.name in {"block1", "block2"}:
                payload = torch.load(candidate, map_location="cpu", weights_only=False)
                if payload.get("optimizer_state") is None or payload.get("model_state") is None:
                    raise ProductionError("atomic PPO checkpoint lacks model or optimizer state")
                self.milestone_checkpoint_sha = str(payload.get("model_sha256"))
                self.progress.data["live_checkpoint"] = {
                    "path": str(candidate.relative_to(HERE)),
                    "file_sha256": _sha256(candidate),
                    "model_sha256": payload.get("model_sha256"),
                    "completed_batches": int(payload.get("completed_batches", 0)),
                    "block": payload.get("block"),
                }
                self.persist_event(
                    "atomic-checkpoint-written", checkpoint=self.progress.data["live_checkpoint"],
                )
            return result

        rs.atomic_torch_save = tracked_checkpoint_save

        original_update = rs.update_batch

        def tracked_update(model: Any, optimizer: Any, rows: Sequence[Any], vocab: Any, **kwargs: Any):
            block = "block1" if int(kwargs.get("block_seed", -1)) == rs.MODEL_SEEDS["block1"] else "block2"
            batch_index = int(kwargs.get("batch_index", -1))
            if block == "block1" and batch_index == 2:
                self.verify_cache_installed("before-block1-batch02-update")
                self.batch_validation = validate_batch02_input(
                    rs=rs, model=model, vocab=vocab, bootstrap=BOOTSTRAP[0], rows=rows,
                )
                self.progress.data["batch02_validation"] = self.batch_validation
                self.progress.data["open_optimizer_reservation"] = {
                    "block": block, "batch_index": batch_index,
                    "reservation_seconds": self.optimizer_reserve_seconds,
                    "started_utc": _utc_now(),
                }
                self.check_budget(workers=0)
                self.persist_event(
                    "optimizer-started", block=block, batch_index=batch_index,
                    reservation_seconds=self.optimizer_reserve_seconds,
                    decision_count=len(rows), owner_count=128,
                )
            else:
                self.progress.data["open_optimizer_reservation"] = {
                    "block": block, "batch_index": batch_index,
                    "reservation_seconds": self.optimizer_reserve_seconds,
                    "started_utc": _utc_now(),
                }
                self.check_budget(workers=0)
                self.progress.persist(measured_cpu_seconds=self.meter.actual_cpu())
            started = time.process_time()
            result = original_update(model, optimizer, rows, vocab, **kwargs)
            update_cpu = max(0.0, time.process_time() - started)
            self.progress.data["open_optimizer_reservation"] = None
            self.progress.data["last_optimizer_update"] = {
                "block": block, "batch_index": batch_index,
                "measured_parent_cpu_seconds": update_cpu,
                "decision_count": len(rows), "minibatch_count": len(result),
            }
            self.persist_event(
                "optimizer-completed", block=block, batch_index=batch_index,
                measured_parent_cpu_seconds=update_cpu, decision_count=len(rows),
                minibatch_count=len(result),
            )
            return result

        rs.update_batch = tracked_update

        def actual_budget_guard(**_ignored: Any) -> None:
            if self.milestone_mode and any(
                path.endswith("block1/batch-02.json") for path in self.state._completed_batch_paths
            ):
                raise IntegrationMilestoneReached()
            self.check_budget()

        rs.budget_guard = actual_budget_guard
        rs._accounted_cpu = lambda _cpu=0.0: self.meter.accounted_cpu()
        rs.BUILDER_BUDGET = NEW_BUILDER_CPU_LIMIT_SECONDS

        original_train_block = rs.train_block

        def tracked_train_block(block: str, config: Mapping[str, Any], vocab: Any,
                                bootstrap: Any, runtime: Mapping[str, Any], stage_started: float):
            self.verify_cache_installed(f"before-training-{block}")
            self.optimizer_reserve_seconds = self.optimizer_reserve(rs)
            self.progress.data["phase"] = f"training-{block}"
            self.persist_event(
                "stage-started", stage=f"training-{block}",
                worker_selection=self.worker_selection,
                completed_run_keys=self.state.completed_run_count(),
            )
            try:
                result = original_train_block(block, config, vocab, bootstrap, runtime, stage_started)
                self.persist_event(
                    "stage-completed", stage=f"training-{block}",
                    completed_batches=result.get("completed_batches"),
                    checkpoint=result.get("final_checkpoint"),
                )
                return result
            except IntegrationMilestoneReached:
                raise
            finally:
                self.optimizer_reserve_seconds = 0.0
                self.verify_cache_installed(f"after-training-{block}")

        rs.train_block = tracked_train_block
        original_evaluation = rs.run_evaluation

        def tracked_evaluation(config: Mapping[str, Any], runtime: Mapping[str, Any],
                               vocab: Any, bootstrap: Any, stage_started: float):
            self.verify_cache_installed("before-evaluation")
            self.optimizer_reserve_seconds = 0.0
            self.progress.data["phase"] = "evaluation"
            self.persist_event(
                "stage-started", stage="evaluation",
                worker_selection=self.worker_selection,
                completed_run_keys=self.state.completed_run_count(),
            )
            try:
                result = original_evaluation(config, runtime, vocab, bootstrap, stage_started)
                self.persist_event(
                    "stage-completed", stage="evaluation", run_count=result.get("run_count"),
                )
                return result
            finally:
                self.verify_cache_installed("after-evaluation")

        rs.run_evaluation = tracked_evaluation
        self.dispatcher = DurableDispatcher(
            rs, self.tracker, int(self.worker_selection["worker_count"]), owner=self,
        )
        rs.run_game = self.dispatcher.dispatch_run_game
        self._dispatcher_loader = self.dispatcher.wrap_model_loader()
        rs._load_final_model = self._dispatcher_loader
        self._production_atomic = rs.atomic_json

    def optimizer_reserve(self, rs: Any) -> float:
        samples = []
        for block in ("block1", "block2"):
            for path in sorted((EVIDENCE / "training" / block).glob("batch-*.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    amount = float(value.get("optimizer_cpu_seconds", 0.0))
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    continue
                if math.isfinite(amount) and amount > 0:
                    samples.append(amount)
        return max(INITIAL_OPTIMIZER_RESERVE_SECONDS,
                   (max(samples) * OPTIMIZER_RESERVE_MULTIPLIER) if samples else 0.0)

    def verify_cache_installed(self, stage: str) -> None:
        rs = run_study
        if self.dispatcher is None:
            # During first hook setup, only the cached stage hooks are ready.
            if rs.atomic_json is not self._cached_atomic_json:
                raise ProductionError(f"cached atomic accounting hook was lost before {stage}")
            if rs._completed_run_count() != self.state.completed_run_count():
                raise ProductionError(f"cached completed-key index is stale before {stage}")
            return
        if rs.atomic_json is not self._production_atomic:
            raise ProductionError(f"production/cached atomic writer was replaced before {stage}")
        if getattr(rs._completed_run_count, "__self__", None) is not self.state:
            raise ProductionError(f"cached completed-key index is not installed at {stage}")
        if rs._completed_run_count() != self.state.completed_run_count():
            raise ProductionError(f"cached completed-key count drifted at {stage}")
        if rs._remaining_optimizer_projection is not self._cached_projection:
            raise ProductionError(f"cached optimizer accounting hook was replaced at {stage}")
        if getattr(resource_amendment_r2.complete_game_costs, "__self__", None) is not self.state:
            raise ProductionError(f"cached game-cost index is not installed at {stage}")
        if getattr(resource_amendment_r2._ledger_total, "__self__", None) is not self.state:
            raise ProductionError(f"cached ledger index is not installed at {stage}")
        if getattr(rs.run_game, "__self__", None) is not self.dispatcher:
            raise ProductionError(f"parallel dispatcher was replaced at {stage}")
        if self.dispatcher.raw_run_game is not self._cached_raw_game:
            raise ProductionError(f"dispatcher no longer uses the cached run-completion hook at {stage}")
        if rs._load_final_model is not self._dispatcher_loader:
            raise ProductionError(f"parallel final-model loader was replaced at {stage}")
        if torch.get_num_threads() != 1:
            raise ProductionError(f"worker Torch thread limit changed at {stage}")
        self.progress.data["last_cache_verification"] = {
            "stage": stage, "timestamp_utc": _utc_now(),
            "completed_run_keys": self.state.completed_run_count(),
            "dispatcher_workers": self.dispatcher.workers,
        }


BOOTSTRAP: list[Any] = []


def validate_batch02_input(*, rs: Any, model: Any, vocab: Any,
                           bootstrap: Any, rows: Sequence[Any]) -> dict[str, Any]:
    """Validate all retained and newly completed owners before the batch-02 PPO update."""
    digest = rs.config_sha()
    checkpoint_path = EVIDENCE / "training" / "block1" / "latest.pt"
    payload = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    if (payload.get("completed_batches") != 1 or payload.get("model_sha256") != EXPECTED_BATCH01_SHA
            or payload.get("config_sha256") != digest or payload.get("vocabulary_sha256") != vocab.sha256):
        raise ProductionError("batch-02 update does not start from the retained post-batch-01 checkpoint/config")
    if rs.model_fingerprint(model, vocab, digest) != EXPECTED_BATCH01_SHA:
        raise ProductionError("batch-02 frozen model changed before owner assembly")

    expected_seeds = list(rs.TRAINING_SEEDS["block1"])[128:256]
    if len(expected_seeds) != 128 or len(set(expected_seeds)) != 128:
        raise ProductionError("registered batch-02 owner grid is not exactly 128 unique keys")
    reconstructed_rows = []
    per_owner: list[dict[str, Any]] = []
    max_probability_error = 0.0
    max_value_error = 0.0
    decisions_checked = 0
    was_training = bool(model.training)
    model.eval()
    try:
        with torch.inference_mode():
            for seed in expected_seeds:
                directory = rs._training_game_dir("block1", 2, seed)
                completed = rs.successful_attempt(directory, EXPECTED_BATCH01_SHA, digest)
                if completed is None:
                    raise ProductionError(f"batch-02 owner {seed} lacks a complete frozen-policy attempt")
                attempt, summary = completed
                if (summary.get("game_seed") != seed or summary.get("stage_key") != "block1:batch-02"
                        or summary.get("arm") not in {None, "residual"}
                        or summary.get("model_sha256") != EXPECTED_BATCH01_SHA
                        or summary.get("config_sha256") != digest
                        or summary.get("status") != "COMPLETE"
                        or summary.get("terminal") is not True
                        or summary.get("problems") not in ([], None)
                        or summary.get("outcome") not in {"PLAYER_LOSS", "PLAYER_VICTORY"}
                        or summary.get("terminal_floor") is None):
                    raise ProductionError(f"batch-02 owner {seed} summary identity/terminal evidence is invalid")
                events = list(rs.read_jsonl(attempt / "decisions.jsonl.gz"))
                if len(events) != int(summary.get("decision_count", -1)):
                    raise ProductionError(f"batch-02 owner {seed} decision count does not match its log")
                if not events:
                    raise ProductionError(f"batch-02 owner {seed} has no decisions")
                owner_rows = rs.decision_rows(attempt, summary)
                if len(owner_rows) != len(events):
                    raise ProductionError(f"batch-02 owner {seed} did not reconstruct every training row")
                for event, row in zip(events, owner_rows, strict=True):
                    serialized = event.get("issue60_public_input")
                    if not isinstance(serialized, str):
                        raise ProductionError(f"batch-02 owner {seed} has no serialized public input")
                    if hashlib.sha256(serialized.encode("utf-8")).hexdigest() != event.get("issue60_public_input_sha256"):
                        raise ProductionError(f"batch-02 owner {seed} public input hash is inconsistent")
                    public = json.loads(serialized)
                    options = public.get("legal_options")
                    selected = event.get("issue60_selected_index")
                    count = event.get("issue60_choice_count")
                    if (not isinstance(options, list) or len(options) < 1 or count != len(options)
                            or not isinstance(selected, int) or isinstance(selected, bool)
                            or selected < 0 or selected >= len(options)
                            or event.get("issue60_native_mapping_fresh") is not True
                            or event.get("issue60_supported_choice") is not True):
                        raise ProductionError(f"batch-02 owner {seed} has an invalid legal action index/mapping witness")
                    base_stored = [float(value) for value in event.get("issue60_base_probabilities", [])]
                    behavior_stored = [float(value) for value in event.get("issue60_behavior_probabilities", [])]
                    if len(base_stored) != len(options) or len(behavior_stored) != len(options):
                        raise ProductionError(f"batch-02 owner {seed} probability vectors do not match legal choices")
                    if (any(not math.isfinite(value) or value < 0.0 for value in base_stored + behavior_stored)
                            or not math.isclose(math.fsum(base_stored), 1.0, rel_tol=0.0, abs_tol=1e-10)
                            or not math.isclose(math.fsum(behavior_stored), 1.0, rel_tol=0.0, abs_tol=1e-10)):
                        raise ProductionError(f"batch-02 owner {seed} has an invalid stored probability distribution")
                    _bootstrap_index, expected_base = controllers.p0_distribution(bootstrap, serialized, len(options))
                    base_error = max(abs(a - b) for a, b in zip(base_stored, expected_base, strict=True))
                    if base_error > 1e-10:
                        raise ProductionError(f"batch-02 owner {seed} stored base distribution differs from frozen A")
                    observation = encoder.encode(serialized, vocab)
                    logits, value = model(observation)
                    expected_behavior = controllers.policy_distribution(
                        base_stored, [float(item) for item in logits.detach().cpu().tolist()],
                    )
                    probability_error = max(
                        abs(a - b) for a, b in zip(behavior_stored, expected_behavior, strict=True)
                    )
                    max_probability_error = max(max_probability_error, probability_error)
                    value_error = abs(float(event["issue60_value_prediction"]) - float(value.detach().cpu()))
                    max_value_error = max(max_value_error, value_error)
                    if probability_error > 1e-6 or value_error > 1e-5:
                        raise ProductionError(
                            f"batch-02 owner {seed} stored policy/value disagrees with frozen checkpoint "
                            f"(probability_error={probability_error:.3g}, value_error={value_error:.3g})"
                        )
                    target = (float(summary["terminal_floor"]) - float(event["issue60_current_floor"])) / 60.0
                    if not math.isclose(row.value_target, target, rel_tol=0.0, abs_tol=1e-12):
                        raise ProductionError(f"batch-02 owner {seed} terminal target arithmetic is inconsistent")
                    reconstructed_rows.append(row)
                    decisions_checked += 1
                per_owner.append({
                    "seed": seed, "attempt": str(attempt.relative_to(HERE)),
                    "decision_count": len(events),
                    "decision_log_sha256": _sha256(attempt / "decisions.jsonl.gz"),
                    "run_json_sha256": _sha256(attempt / "run.json"),
                    "process_cpu_seconds": float(summary.get("process_cpu_seconds", 0.0)),
                })
    finally:
        model.train(was_training)

    if list(rows) != reconstructed_rows:
        raise ProductionError("batch-02 optimizer rows differ from the declared owner/run/decision order")
    rows_path = EVIDENCE / "training" / "block1" / "batch-02-training-rows.jsonl.gz"
    if not rows_path.is_file():
        raise ProductionError("batch-02 optimizer input artifact was not written before update")
    with gzip.open(rows_path, "rt", encoding="utf-8") as stream:
        stored_rows = [json.loads(line) for line in stream if line.strip()]
    expected_artifact = [row.to_json() for row in reconstructed_rows]
    if stored_rows != expected_artifact:
        raise ProductionError("batch-02 training rows artifact differs from the validated 128-owner assembly")
    return {
        "schema_id": "issue60-batch02-input-validation-v1", "status": "PASS",
        "config_sha256": digest, "starting_checkpoint_sha256": _sha256(checkpoint_path),
        "starting_model_sha256": EXPECTED_BATCH01_SHA,
        "owner_count": len(per_owner), "ordered_seeds": expected_seeds,
        "missing_seed_keys_completed": list(MISSING_BATCH02_SEEDS),
        "decision_count": decisions_checked,
        "stored_probability_vectors_reconstructed": decisions_checked,
        "stored_value_predictions_reconstructed": decisions_checked,
        "base_probabilities_reconstructed_from_frozen_bootstrap": decisions_checked,
        "legal_option_mapping_checks": decisions_checked,
        "terminal_target_arithmetic_checks": decisions_checked,
        "max_behavior_probability_absolute_error": max_probability_error,
        "max_value_prediction_absolute_error": max_value_error,
        "training_rows_sha256": _sha256(rows_path),
        "owners": per_owner,
        "provenance_limitation": (
            "114 historically retained games have an unknown exact producer; their provisional use "
            "is explicitly authorized for this exploratory pilot."
        ),
    }


def _worker_samples() -> list[int]:
    candidates = (
        EVIDENCE / "validation" / "parallel-execution" / "stages" / "train.json",
        EVIDENCE / "validation" / "parallel-execution" / "batch02-sampling" / "attempt-0005" / "parallel-execution" / "stages" / "train.json",
        EVIDENCE / "validation" / "parallel-execution" / "batch02-sampling" / "attempt-0004" / "parallel-execution" / "stages" / "train.json",
    )
    samples: list[int] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in raw.get("executions", []):
            if not isinstance(row, Mapping):
                continue
            value = row.get("peak_rss_bytes")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                samples.append(int(value))
    if not samples:
        raise ProductionError("fixed parallel runner has no measured worker RSS sample for CPU/RAM selection")
    return samples


def _checkpoint_completed_batches(rs: Any, vocab: Any, config: Mapping[str, Any]) -> int:
    path = EVIDENCE / "training" / "block1" / "latest.pt"
    if not path.is_file():
        raise ProductionError("block1 latest checkpoint is missing")
    model = rs.JointChoiceActorCritic(vocab)
    optimizer = torch.optim.Adam(model.parameters(), lr=float(config["training"]["learning_rate"]))
    cpu_state = torch.random.get_rng_state()
    try:
        payload = rs.load_checkpoint(path, model, optimizer, vocab, rs.config_sha())
    finally:
        torch.random.set_rng_state(cpu_state)
    completed = int(payload.get("completed_batches", -1))
    if completed == 1 and payload.get("model_sha256") != EXPECTED_BATCH01_SHA:
        raise ProductionError("block1 batch-01 checkpoint SHA differs from the authorized milestone parent")
    return completed


def _startup_identity(rs: Any, runtime: Mapping[str, Any], head: str) -> dict[str, Any]:
    checkpoint = EVIDENCE / "training" / "block1" / "latest.pt"
    return {
        "head": head, "launcher": str(Path(__file__).resolve().relative_to(HERE)),
        "launcher_sha256": _sha256(Path(__file__).resolve()),
        "config_sha256": rs.config_sha(), "runtime": dict(runtime),
        "checkpoint_path": str(checkpoint.relative_to(HERE)),
        "checkpoint_file_sha256": _sha256(checkpoint),
    }


def run_single_key_witness(rs: Any, config: Mapping[str, Any], vocab: Any,
                           bootstrap: Any, runtime: Mapping[str, Any],
                           stage_started: float, runtime_context: ProductionRuntime,
                           *, head: str, source_validation: Mapping[str, Any]) -> dict[str, Any]:
    """Run exactly the first missing original block-2 key through production dispatch."""
    block = "block2"
    checkpoint_path = EVIDENCE / "training" / block / "latest.pt"
    if not checkpoint_path.is_file():
        raise ProductionError("single-key witness block2 checkpoint is missing")
    digest = rs.config_sha()
    training_config = config["training"]
    torch.manual_seed(rs.MODEL_SEEDS[block])
    model = rs.JointChoiceActorCritic(vocab)
    optimizer = torch.optim.Adam(model.parameters(), lr=float(training_config["learning_rate"]))
    checkpoint = rs.load_checkpoint(checkpoint_path, model, optimizer, vocab, digest)
    if checkpoint.get("block") != block:
        raise ProductionError("single-key witness checkpoint is not the block2 model")
    completed_batches = int(checkpoint.get("completed_batches", -1))
    if not 1 <= completed_batches < 8:
        raise ProductionError(f"single-key witness checkpoint boundary is invalid: {completed_batches}")
    batch_index = completed_batches + 1
    frozen_sha = rs.model_fingerprint(model, vocab, digest)
    if frozen_sha != checkpoint.get("model_sha256"):
        raise ProductionError("single-key witness loaded model fingerprint differs from its checkpoint")

    batch_seeds = list(rs.TRAINING_SEEDS[block])[(batch_index - 1) * 128:batch_index * 128]
    if not batch_seeds:
        raise ProductionError(f"single-key witness has no registered seeds for batch {batch_index}")
    selected_seed = None
    output_dir = None
    for seed in batch_seeds:
        candidate_dir = rs._training_game_dir(block, batch_index, seed)
        if rs.successful_attempt(candidate_dir, frozen_sha, digest) is None:
            selected_seed, output_dir = int(seed), candidate_dir
            break
    if selected_seed is None or output_dir is None:
        raise ProductionError(
            f"block2 batch {batch_index} has no missing original key for checkpoint {frozen_sha}"
        )

    stage_key = f"{block}:batch-{batch_index:02d}"
    runtime_context.single_key_witness_seed = selected_seed
    runtime_context.single_key_witness_stage = stage_key
    runtime_context.optimizer_reserve_seconds = runtime_context.optimizer_reserve(rs)
    provenance = rs._current_policy_provenance(config, vocab, frozen_sha, "residual")
    started_wall = time.monotonic()
    attempt, summary = rs.run_game(
        seed=selected_seed, mode="residual", model=model, block_name=block,
        runtime_seed=rs.derive_seed("training-policy", rs.MODEL_SEEDS[block], selected_seed, batch_index),
        output_dir=output_dir, model_sha=frozen_sha, config_digest=digest,
        vocab=vocab, bootstrap=bootstrap, require_targets=True,
        provenance_config=provenance, stage_key=stage_key, arm="residual",
    )
    if summary.get("status") != "COMPLETE":
        raise ProductionError(f"single-key witness did not complete: {summary.get('problems')}")
    completed = rs.successful_attempt(output_dir, frozen_sha, digest)
    if completed is None or completed[0].resolve() != attempt.resolve():
        raise ProductionError("single-key witness result is not a durable matching successful attempt")
    if int(summary.get("game_seed", -1)) != selected_seed:
        raise ProductionError("single-key witness result seed differs from the registered original key")

    pid = int(summary.get("pid", -1))
    metrics_path = EVIDENCE / "execution" / "public-history-execution.jsonl"
    history_metrics = []
    if metrics_path.is_file():
        for line in metrics_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("pid") == pid and record.get("seed") == selected_seed:
                history_metrics.append(record)
    if len(history_metrics) != 1:
        raise ProductionError(
            f"single-key witness expected one public-history metric for pid={pid}, seed={selected_seed}, "
            f"got {len(history_metrics)}"
        )
    execution_rows = [
        row for row in runtime_context.dispatcher.executions
        if tuple(row.get("task_key", ())) == (stage_key, selected_seed, "residual")
    ] if runtime_context.dispatcher is not None else []
    if len(execution_rows) != 1 or int(execution_rows[0].get("exit_code", -1)) != 0:
        raise ProductionError("single-key witness lacks exactly one successful production worker record")
    if runtime_context.dispatcher.max_active_workers != 1:
        raise ProductionError("single-key witness production path did not execute exactly one worker")

    summary_path = attempt / "run.json"
    decisions_path = attempt / "decisions.jsonl.gz"
    receipt = {
        "schema_id": "issue60-single-key-witness-v1",
        "purpose": "STUDY_ONLY",
        "status": "COMPLETE",
        "issue": ISSUE_NUMBER,
        "execution_head": head,
        "launcher": "production_entry.py",
        "launcher_sha256": _sha256(Path(__file__).resolve()),
        "config_sha256": digest,
        "runtime": dict(runtime),
        "source_validation": dict(source_validation),
        "checkpoint": {
            "path": str(checkpoint_path.relative_to(HERE)),
            "file_sha256": _sha256(checkpoint_path),
            "block": block,
            "completed_batches_before_witness": completed_batches,
            "model_sha256": frozen_sha,
            "optimizer_state_loaded": checkpoint.get("optimizer_state") is not None,
        },
        "training_key": {
            "block": block,
            "batch_index": batch_index,
            "stage_key": stage_key,
            "seed": selected_seed,
            "arm": "residual",
            "runtime_policy_seed": rs.derive_seed(
                "training-policy", rs.MODEL_SEEDS[block], selected_seed, batch_index,
            ),
            "output_directory": str(output_dir.relative_to(HERE)),
        },
        "attempt": {
            "path": str(attempt.relative_to(HERE)),
            "run_json_sha256": _sha256(summary_path),
            "decisions_sha256": _sha256(decisions_path),
            "status": summary["status"],
            "supported_choices": summary.get("supported_choices"),
            "search_calls": summary.get("search_calls"),
            "process_cpu_seconds": summary.get("process_cpu_seconds"),
            "pid": pid,
        },
        "public_history_execution_metric": history_metrics[0],
        "production_dispatcher_execution": execution_rows[0],
        "production_dispatcher": runtime_context.dispatcher.evidence(),
        "measured_new_cpu_seconds_at_receipt": runtime_context.meter.actual_cpu(),
        "accounted_new_cpu_seconds_at_receipt": runtime_context.meter.accounted_cpu(),
        "stage_wall_seconds": max(0.0, time.monotonic() - started_wall),
        "ppo_updated": False,
        "evaluation_started": False,
        "continuation": (
            "The standard training continuation reuses this exact completed seed/model/config key "
            "when it resumes; no new seed or duplicate successful run is introduced."
        ),
    }
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_production_args(argv)
    if not hasattr(os, "fork") or not Path("/proc/self/stat").is_file():
        raise ProductionError("the production entry requires the registered Linux /proc/fork CPU tracker")
    setup_parent_cpu = max(0.0, time.process_time() - ENTRY_PROCESS_CPU_STARTED)
    tracker = parallel_execution.ProcessCpuTracker.start(time.process_time())
    resource_module = resource_amendment_r2

    config = json.loads(run_study.CONFIG_PATH.read_text(encoding="utf-8"))
    source_validation = verify_active_sources_once(run_study)
    run_study.validate_config(config)

    def metered_repo_head(path: str) -> str:
        result = _run_counted_external(["git", "-C", str(path), "rev-parse", "HEAD"])
        if result.returncode != 0:
            raise ProductionError(f"could not read pinned source HEAD at {path}")
        return result.stdout.strip()

    run_study._repo_head = metered_repo_head
    runtime = run_study.verify_runtime(config)
    vocab = encoder.Vocabulary(HERE / "vocabulary.json")
    if vocab.sha256 != config["sources"]["vocabulary_sha256"]:
        raise ProductionError("frozen vocabulary SHA differs from the registered config")
    bootstrap, _bootstrap_payload = controllers.load_bootstrap(HERE / config["bootstrap"]["checkpoint"])
    BOOTSTRAP[:] = [bootstrap]
    head_result = _run_counted(["git", "rev-parse", "HEAD"], cwd=HERE)
    if head_result.returncode != 0:
        raise ProductionError("could not record the actual repository HEAD")
    head = head_result.stdout.strip()
    completed_batches = _checkpoint_completed_batches(run_study, vocab, config)
    labels = _issue_state()
    phase = execution_phase(
        completed_batches, labels, continue_reviewed=args.continue_reviewed,
        single_key_witness=args.single_key_witness,
    )

    progress = ProductionProgress(PROGRESS_PATH.parent, config_sha=run_study.config_sha())
    # The first administrative debit is recorded separately from any measured run CPU.
    if not progress.data.get("administrative_protocol_initialized"):
        progress.data["historical_admin_debit_cpu_seconds"] = ACCEPTED_INTERRUPTION_DEBIT_SECONDS
        progress.data["historical_admin_debits"] = [{
            "kind": "accepted-historical-interruption",
            "seconds": ACCEPTED_INTERRUPTION_DEBIT_SECONDS,
            "measured": False,
            "reason": "Planner accepted this as an administrative debit, not a measured upper bound.",
        }]
        progress.data["administrative_protocol_initialized"] = True
        progress.data["historical_admin_total_cpu_seconds"] = HISTORICAL_ADMIN_CPU_SECONDS
        progress.persist()

    if phase == "awaiting-review":
        continuation_disabled = completed_batches >= 2
        progress.data["phase"] = (
            "continuation-disabled" if continuation_disabled else "not-routed-to-builder"
        )
        progress.data["observed_head"] = head
        progress.persist()
        print(json.dumps({
            "status": "CONTINUATION_DISABLED" if continuation_disabled else "AWAITING_REVIEW",
            "checkpoint_completed_batches": completed_batches,
            "issue_labels": sorted(labels), "head": head,
            "automatic_continuation_authorization": "disabled" if continuation_disabled else "not-applicable",
        }, indent=2))
        return 0
    if phase not in {"integration-milestone", "full-continuation", "single-key-witness"}:
        raise ProductionError(f"Issue #{ISSUE_NUMBER} is not routed to Builder")

    from public_history_execution import install as install_public_history_execution

    history_metrics_path = EVIDENCE / "execution" / "public-history-execution.jsonl"
    history_execution = install_public_history_execution(metrics_path=history_metrics_path)
    progress.data["public_history_execution"] = history_execution
    progress.persist()

    milestone_mode = phase == "integration-milestone"
    progress.begin_invocation(
        head=head, launcher_sha=_sha256(Path(__file__).resolve()), runtime=runtime,
        parent_reserve=PARENT_INTERRUPTION_RESERVE_SECONDS,
    )
    selection = parallel_execution.choose_worker_count(
        effective_cpus=int(parallel_execution.host_capacity()["effective_cpu_count"]),
        available_bytes=int(parallel_execution.host_capacity()["effective_memory_available_bytes"]),
        worker_peak_rss_samples=_worker_samples(), cap=parallel_execution.MAX_GAME_WORKERS,
    )
    if phase == "single-key-witness":
        selection["host_selected_worker_count"] = int(selection["worker_count"])
        selection["worker_count"] = 1
        selection["single_key_witness_worker_ceiling"] = 1
    meter = Meter(
        progress=progress, tracker=tracker, setup_parent_cpu=setup_parent_cpu,
        setup_external_cpu=_EXTERNAL_CHILD_CPU[0],
    )
    state = guard_optimization.GuardState(HERE, resource_module)
    tracker.max_game_cpu.value = float(state.max_observed_game_cpu_seconds)
    runtime_context = ProductionRuntime(
        progress=progress, state=state, tracker=tracker, meter=meter,
        worker_selection=selection, milestone_mode=milestone_mode,
    )
    if milestone_mode:
        runtime_context.optimizer_reserve_seconds = runtime_context.optimizer_reserve(run_study)
        selection["host_selected_worker_count"] = int(selection["worker_count"])
        selection["worker_count"] = select_milestone_worker_count(
            preferred_workers=int(selection["worker_count"]),
            actual_cpu=meter.accounted_cpu(),
            game_reserve=runtime_context.game_reserve_seconds(),
            optimizer_reserve=runtime_context.optimizer_reserve_seconds,
            parent_reserve=PARENT_INTERRUPTION_RESERVE_SECONDS,
        )
        selection["integration_budget_worker_ceiling"] = INTEGRATION_MAX_WORKERS
        runtime_context.worker_selection = dict(selection)
    runtime_context.install_cache(
        rs=run_study, resource_module=resource_module,
        parallel_module=parallel_execution,
    )
    runtime_context.verify_cache_installed("startup")
    progress.data["startup_identity"] = _startup_identity(run_study, runtime, head)
    progress.data["source_validation"] = source_validation
    progress.data["historical_accounting"] = {
        "historical_admin_total_cpu_seconds": HISTORICAL_ADMIN_CPU_SECONDS,
        "accepted_interruption_debit_cpu_seconds": ACCEPTED_INTERRUPTION_DEBIT_SECONDS,
        "historical_physical_cpu_certified": False,
        "prospective_builder_limit_cpu_seconds": NEW_BUILDER_CPU_LIMIT_SECONDS,
        "reviewer_reserve_cpu_seconds": 20_000.0,
        "integration_milestone_limit_cpu_seconds": INTEGRATION_CPU_LIMIT_SECONDS,
    }
    progress.data["worker_selection"] = selection
    progress.data["phase"] = phase
    progress.persist(measured_cpu_seconds=meter.actual_cpu())
    runtime_context.persist_event(
        "production-started", phase=phase, head=head,
        launcher_sha256=_sha256(Path(__file__).resolve()),
        checkpoint=progress.data["startup_identity"]["checkpoint_file_sha256"],
        worker_selection=selection,
        cached_completed_run_keys=state.completed_run_count(),
    )
    config = json.loads(run_study.CONFIG_PATH.read_text(encoding="utf-8"))
    stage_started = time.process_time()

    try:
        if milestone_mode:
            run_study.train_block("block1", config, vocab, bootstrap, runtime, stage_started)
            raise ProductionError("batch-02 completed without reaching its integration-review stop boundary")

        if phase == "single-key-witness":
            receipt = run_single_key_witness(
                run_study, config, vocab, bootstrap, runtime, stage_started, runtime_context,
                head=head, source_validation=source_validation,
            )
            receipt_path = EVIDENCE / "execution" / "single-key-witness.json"
            _atomic_json(receipt_path, receipt)
            progress.data["phase"] = "single-key-witness-complete"
            progress.data["single_key_witness"] = str(receipt_path.relative_to(HERE))
            progress.data["open_invocation"] = None
            progress.data["open_optimizer_reservation"] = None
            runtime_context.persist_event(
                "single-key-witness-completed", receipt=str(receipt_path.relative_to(HERE)),
                seed=receipt["training_key"]["seed"], stage_key=receipt["training_key"]["stage_key"],
                attempt=receipt["attempt"]["path"], ppo_updated=False, evaluation_started=False,
            )
            progress.persist(measured_cpu_seconds=meter.actual_cpu())
            print(json.dumps(receipt, indent=2))
            return 0

        evaluation, analysis = run_full_continuation_stages(
            run_study, config, vocab, bootstrap, runtime, stage_started,
        )
        runtime_context.verify_cache_installed("before-analysis-complete")
        progress.data["final_analysis"] = {
            "status": analysis.get("status"),
            "path": "evidence/analysis/result.json",
        }
        runtime_context.persist_event(
            "production-completed", evaluation_run_count=evaluation.get("run_count"),
            analysis_status=analysis.get("status"),
            cumulative_completed_run_keys=state.completed_run_count(),
        )
        progress.data["phase"] = "completed"
        progress.data["open_invocation"] = None
        progress.persist(measured_cpu_seconds=meter.actual_cpu())
        return 0
    except IntegrationMilestoneReached:
        checkpoint = torch.load(
            EVIDENCE / "training" / "block1" / "latest.pt", map_location="cpu", weights_only=False,
        )
        if checkpoint.get("completed_batches") != 2 or checkpoint.get("model_sha256") != runtime_context.milestone_checkpoint_sha:
            raise ProductionError("batch-02 integration checkpoint did not atomically retain the PPO state")
        receipt = {
            "schema_id": "issue60-integration-milestone-v1", "purpose": "STUDY_ONLY",
            "status": "INTEGRATION_REVIEW_REQUIRED", "issue": ISSUE_NUMBER,
            "execution_head": head,
            "launcher": "production_entry.py",
            "launcher_sha256": _sha256(Path(__file__).resolve()),
            "config_sha256": run_study.config_sha(), "runtime": runtime,
            "source_validation": progress.data["source_validation"],
            "checkpoint": {
                "path": "evidence/training/block1/latest.pt",
                "file_sha256": _sha256(EVIDENCE / "training" / "block1" / "latest.pt"),
                "model_sha256": checkpoint["model_sha256"],
                "completed_batches": checkpoint["completed_batches"],
                "optimizer_state_present": checkpoint.get("optimizer_state") is not None,
            },
            "batch02_validation": runtime_context.batch_validation,
            "worker_selection": selection,
            "max_active_workers": runtime_context.dispatcher.max_active_workers if runtime_context.dispatcher else 0,
            "completed_batch02_seeds": [
                row["seed"] for row in (runtime_context.batch_validation or {}).get("owners", [])
            ],
            "measured_new_cpu_seconds": meter.actual_cpu(),
            "administrative_debit_cpu_seconds": progress.data["administrative_debit_cpu_seconds"],
            "accounted_new_cpu_seconds": meter.accounted_cpu(),
            "historical_admin_total_cpu_seconds": HISTORICAL_ADMIN_CPU_SECONDS,
            "historical_physical_cpu_certified": False,
            "stage_wall_seconds": max(0.0, time.monotonic() - _INVOCATION_WALL_STARTED[0]),
            "provenance_limitation": (
                "114 retained historical training games have an unknown exact producer and are used provisionally "
                "under the Planner authorization; no full-provenance claim is made."
            ),
            "review_protocol": (
                "After push, Reviewer must post issue60-integration-review-v1: PASS <exact pushed HEAD SHA> "
                "only after reviewing that exact handoff commit."
            ),
        }
        _atomic_json(MILESTONE_PATH, receipt)
        progress.data["phase"] = "integration-review-required"
        progress.data["milestone"] = str(MILESTONE_PATH.relative_to(HERE))
        progress.data["milestone_checkpoint_sha256"] = receipt["checkpoint"]["file_sha256"]
        progress.data["open_invocation"] = None
        runtime_context.persist_event(
            "integration-milestone-reached", milestone=str(MILESTONE_PATH.relative_to(HERE)),
            checkpoint=receipt["checkpoint"], completed_batch02_owners=len(receipt["completed_batch02_seeds"]),
            worker_count=selection["worker_count"], max_active_workers=receipt["max_active_workers"],
        )
        progress.persist(measured_cpu_seconds=meter.actual_cpu())
        print(json.dumps(receipt, indent=2))
        return 0
    except ProspectiveBudgetStop as exc:
        progress.data["phase"] = "budget-stop"
        progress.data["stop_reason"] = str(exc)
        progress.data["open_optimizer_reservation"] = None
        progress.data["open_invocation"] = None
        progress.persist(measured_cpu_seconds=meter.actual_cpu())
        runtime_context.persist_event(
            "prospective-budget-stop", reason=str(exc),
            cumulative_completed_run_keys=state.completed_run_count(),
        )
        print(json.dumps({
            "status": "BUDGET_STOP", "reason": str(exc),
            "measured_new_cpu_seconds": progress.data["measured_new_cpu_seconds"],
            "administrative_debit_cpu_seconds": progress.data["administrative_debit_cpu_seconds"],
            "completed_run_keys": state.completed_run_count(),
        }, indent=2))
        return 2
    except BaseException as exc:
        progress.data["phase"] = "correctness-or-runtime-stop"
        progress.data["stop_reason"] = f"{type(exc).__name__}: {exc}"
        progress.data["open_optimizer_reservation"] = None
        progress.data["open_invocation"] = None
        progress.persist(measured_cpu_seconds=meter.actual_cpu())
        runtime_context.persist_event(
            "correctness-or-runtime-stop", error=progress.data["stop_reason"],
            cumulative_completed_run_keys=state.completed_run_count(),
        )
        raise


_INVOCATION_WALL_STARTED = [time.monotonic()]


if __name__ == "__main__":
    raise SystemExit(main())
