"""Issue 60 execution-only fixed-key subprocess scheduling and CPU accounting.

This module deliberately leaves the frozen policy, game adapter, trainer, and
R2 resource-amendment implementation untouched.  It forks isolated workers
around the existing one-game function and returns results in registered order.
"""
from __future__ import annotations

import copy
import cProfile
from collections import deque
import json
import math
import os
from pathlib import Path
import pstats
import resource
import sys
import time
from typing import Any, Callable, Mapping, Sequence


GIB = 1024 ** 3
MAX_GAME_WORKERS = 8
MEMORY_RESERVE_FRACTION = 0.20
MIN_MEMORY_RESERVE_BYTES = 2 * GIB


class ParallelExecutionError(RuntimeError):
    """A fixed-key worker could not produce a valid terminal game record."""


def _proc_stat(pid: int) -> tuple[int, int] | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 2:].split()
    if len(fields) < 13:
        return None
    try:
        return int(fields[2]), int(fields[11]) + int(fields[12])
    except ValueError:
        return None


def _proc_ticks(pid: int) -> int:
    value = _proc_stat(pid)
    if value is None:
        return 0
    return value[1]


def aggregate_process_cpu(parent_stage_cpu: float, completed_worker_cpu: float,
                          live_worker_ticks: int, clock_ticks: int,
                          live_worker_count: int) -> float:
    if clock_ticks <= 0 or live_worker_ticks < 0 or live_worker_count < 0:
        raise ValueError("invalid process CPU accounting inputs")
    live = live_worker_ticks / clock_ticks
    return max(0.0, parent_stage_cpu) + max(0.0, completed_worker_cpu) + live + live_worker_count / clock_ticks + 0.05


def can_dispatch(*, actual_cpu: float, builder_budget: float,
                 active_or_pending_workers: int, max_game_cpu: float) -> bool:
    if active_or_pending_workers < 0 or max_game_cpu < 0:
        raise ValueError("invalid in-flight CPU reserve")
    return actual_cpu + active_or_pending_workers * max_game_cpu < builder_budget


def stage_result_status(result: int, recorded_status: str | None) -> str:
    if result == 75:
        return "RESOURCE_BLOCKED"
    return recorded_status or ("COMPLETE" if result == 0 else "FAILED")


def ordered_result_items(keys: Sequence[tuple[str, int, str]],
                         results: Mapping[tuple[str, int, str], Any]) -> list[tuple[tuple[str, int, str], Any]]:
    if len(keys) != len(set(keys)):
        raise ValueError("fixed-key task batch contains duplicate owners")
    missing = [key for key in keys if key not in results]
    if missing:
        raise ValueError(f"fixed-key task batch is incomplete: {missing[:4]}")
    return [(key, results[key]) for key in keys]


class ProcessCpuTracker:
    """Count parent stage CPU plus all live/reaped fork workers exactly once."""

    def __init__(self, *, parent_pid: int, process_group: int,
                 parent_start_cpu: float, parent_start_ticks: int,
                 clock_ticks: int, completed_cpu: Any, active_pids: Any,
                 pending_workers: Any, max_game_cpu: Any,
                 accounting_lock: Any):
        self.parent_pid = parent_pid
        self.process_group = process_group
        self.parent_start_cpu = parent_start_cpu
        self.parent_start_ticks = parent_start_ticks
        self.clock_ticks = clock_ticks
        self.completed_cpu = completed_cpu
        self.active_pids = active_pids
        self.pending_workers = pending_workers
        self.max_game_cpu = max_game_cpu
        self.accounting_lock = accounting_lock

    @classmethod
    def start(cls, stage_started_cpu: float) -> "ProcessCpuTracker":
        if not hasattr(os, "fork") or not Path("/proc/self/stat").is_file():
            raise ParallelExecutionError("parallel execution requires Linux fork and /proc CPU accounting")
        try:
            os.setpgid(0, 0)
        except OSError:
            if os.getpgrp() != os.getpid():
                raise ParallelExecutionError("could not isolate the execution process group for CPU accounting")
        import multiprocessing
        context = multiprocessing.get_context("fork")
        parent_pid = os.getpid()
        ticks = _proc_ticks(parent_pid)
        return cls(
            parent_pid=parent_pid, process_group=os.getpgrp(),
            parent_start_cpu=stage_started_cpu, parent_start_ticks=ticks,
            clock_ticks=int(os.sysconf("SC_CLK_TCK")),
            completed_cpu=context.Value("d", 0.0, lock=True),
            active_pids=context.Array("i", MAX_GAME_WORKERS, lock=True),
            pending_workers=context.Value("i", 0, lock=True),
            max_game_cpu=context.Value("d", 0.0, lock=True),
            accounting_lock=context.RLock(),
        )

    def add_reaped_cpu(self, seconds: float) -> None:
        with self.accounting_lock:
            with self.completed_cpu.get_lock():
                self.completed_cpu.value += max(0.0, float(seconds))

    def set_pending_workers(self, count: int) -> None:
        with self.accounting_lock:
            with self.pending_workers.get_lock():
                self.pending_workers.value = max(0, int(count))

    def register_worker(self, slot: int, pid: int) -> None:
        with self.accounting_lock:
            with self.active_pids.get_lock():
                self.active_pids[slot] = int(pid)

    def reap_worker(self, pid: int, seconds: float) -> int | None:
        """Atomically transfer a worker from live ticks to reaped CPU."""
        with self.accounting_lock:
            with self.active_pids.get_lock():
                slot = next((index for index, active_pid in enumerate(self.active_pids)
                             if int(active_pid) == int(pid)), None)
                if slot is not None:
                    self.active_pids[slot] = 0
            with self.completed_cpu.get_lock():
                self.completed_cpu.value += max(0.0, float(seconds))
        return slot

    def observe_game_cpu(self, seconds: float) -> None:
        with self.max_game_cpu.get_lock():
            self.max_game_cpu.value = max(self.max_game_cpu.value, max(0.0, float(seconds)))

    def active_worker_count(self) -> int:
        with self.accounting_lock:
            with self.active_pids.get_lock():
                active = sum(bool(pid) for pid in self.active_pids)
            with self.pending_workers.get_lock():
                pending = int(self.pending_workers.value)
        return active + pending

    def current_stage_cpu(self) -> float:
        own_pid = os.getpid()
        if own_pid == self.parent_pid:
            parent_cpu = max(0.0, time.process_time() - self.parent_start_cpu)
        else:
            parent_cpu = max(0, _proc_ticks(self.parent_pid) - self.parent_start_ticks) / self.clock_ticks
        with self.accounting_lock:
            with self.completed_cpu.get_lock():
                reaped = float(self.completed_cpu.value)
            with self.active_pids.get_lock():
                active = [int(pid) for pid in self.active_pids if pid]
            live_workers_ticks = sum(_proc_ticks(pid) for pid in active)
        # Round the live /proc ticks upward and cover the tiny wait4/shared-value
        # handoff window conservatively, so a checkpoint cannot undercount CPU.
        return aggregate_process_cpu(
            parent_cpu, reaped, live_workers_ticks, self.clock_ticks, len(active),
        )


def _expand_cpu_list(value: str) -> int:
    total = 0
    for part in value.strip().split(","):
        if not part:
            continue
        bounds = part.split("-", 1)
        start = int(bounds[0])
        stop = int(bounds[-1])
        total += stop - start + 1
    return total


def host_capacity() -> dict[str, Any]:
    """Read CPU and memory limits from the actual Linux execution host."""
    model = "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    cpus_allowed = ""
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("Cpus_allowed_list:"):
                cpus_allowed = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    cpuset_count = _expand_cpu_list(cpus_allowed) if cpus_allowed else (os.cpu_count() or 1)
    quota_count: float | None = None
    for path in (Path("/sys/fs/cgroup/cpu.max"),):
        try:
            fields = path.read_text(encoding="ascii").split()
            if len(fields) == 2 and fields[0] != "max":
                quota_count = int(fields[0]) / int(fields[1])
        except (OSError, ValueError, ZeroDivisionError):
            pass
    if quota_count is None:
        try:
            quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
            period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
            if quota > 0 and period > 0:
                quota_count = quota / period
        except (OSError, ValueError, ZeroDivisionError):
            pass
    effective_cpus = max(1, min(cpuset_count, math.floor(quota_count) if quota_count else cpuset_count))
    memory_total = 0
    memory_available = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, raw = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                amount = int(raw.strip().split()[0]) * 1024
                if key == "MemTotal":
                    memory_total = amount
                else:
                    memory_available = amount
    except (OSError, ValueError):
        pass
    memory_limit: int | None = None
    memory_current = 0
    try:
        raw_limit = Path("/sys/fs/cgroup/memory.max").read_text(encoding="ascii").strip()
        if raw_limit != "max":
            memory_limit = int(raw_limit)
            memory_current = int(Path("/sys/fs/cgroup/memory.current").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        try:
            raw_limit = int(Path("/sys/fs/cgroup/memory/memory.limit_in_bytes").read_text())
            if raw_limit < (1 << 60):
                memory_limit = raw_limit
                memory_current = int(Path("/sys/fs/cgroup/memory/memory.usage_in_bytes").read_text())
        except (OSError, ValueError):
            pass
    effective_available = memory_available
    if memory_limit is not None:
        effective_available = min(effective_available, max(0, memory_limit - memory_current))
    return {
        "cpu_model": model,
        "logical_cpu_count": os.cpu_count(),
        "cpus_allowed_list": cpus_allowed,
        "effective_cpu_count": effective_cpus,
        "cpu_quota_count": quota_count,
        "memory_total_bytes": memory_total,
        "memory_available_bytes": memory_available,
        "memory_limit_bytes": memory_limit,
        "memory_current_bytes": memory_current,
        "effective_memory_available_bytes": effective_available,
    }


def choose_worker_count(*, effective_cpus: int, available_bytes: int,
                        worker_peak_rss_samples: Sequence[int],
                        cap: int = MAX_GAME_WORKERS) -> dict[str, Any]:
    if effective_cpus < 1 or available_bytes < 0 or not worker_peak_rss_samples:
        raise ValueError("worker capacity needs positive CPU and measured worker RSS")
    observed_peak = max(int(value) for value in worker_peak_rss_samples)
    if observed_peak <= 0:
        raise ValueError("worker RSS samples must be positive")
    reserve = max(MIN_MEMORY_RESERVE_BYTES, int(available_bytes * MEMORY_RESERVE_FRACTION))
    capacity = max(1, (available_bytes - reserve) // observed_peak)
    workers = max(1, min(int(cap), int(effective_cpus), int(capacity)))
    return {
        "worker_count": workers,
        "configured_cap": int(cap),
        "effective_cpu_count": int(effective_cpus),
        "measured_worker_peak_rss_bytes": observed_peak,
        "memory_reserve_bytes": reserve,
        "memory_based_worker_capacity": int(capacity),
        "worker_peak_rss_samples_bytes": [int(value) for value in worker_peak_rss_samples],
    }


def _latest_run_summary(rs: Any, task: Mapping[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    kwargs = task["kwargs"]
    found = rs.successful_attempt(
        Path(kwargs["output_dir"]), str(kwargs["model_sha"]), str(kwargs["config_digest"])
    )
    if found is not None:
        return found
    runs = []
    for path in Path(kwargs["output_dir"]).glob("attempt-*/run.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        runs.append((path, row))
    if runs:
        return sorted(runs, key=lambda item: item[0].parent.name)[-1]
    return None


class ParallelGameDispatcher:
    """Run one frozen batch concurrently, then expose successful results in key order."""

    def __init__(self, run_study: Any, tracker: ProcessCpuTracker, workers: int,
                 *, profile_directory: Path | None = None):
        self.rs = run_study
        self.tracker = tracker
        self.workers = max(1, min(MAX_GAME_WORKERS, int(workers)))
        self.profile_directory = profile_directory
        self.raw_run_game = run_study.run_game
        self.results: dict[tuple[str, int, str], tuple[Path, dict[str, Any]]] = {}
        self.final_models: dict[str, tuple[Any, str]] = {}
        self.executions: list[dict[str, Any]] = []
        self.max_worker_rss_bytes = 0
        self.max_active_workers = 0
        self._completed_groups: set[str] = set()

    def _prepare_worker_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        worker = dict(kwargs)
        model = worker.get("model")
        if model is not None:
            worker["model"] = copy.deepcopy(model)
        worker["vocab"] = self.rs.Vocabulary(self.rs.HERE / "vocabulary.json")
        worker["bootstrap"] = copy.deepcopy(worker["bootstrap"])
        return worker

    def _child_run(self, task: Mapping[str, Any]) -> None:
        kwargs = self._prepare_worker_kwargs(task["kwargs"])
        profiler = None
        profile_path = task.get("profile_path")
        if profile_path is not None:
            profiler = cProfile.Profile(time.process_time)
            profiler.enable()
        try:
            _, summary = self.raw_run_game(**kwargs)
        finally:
            if profiler is not None:
                profiler.disable()
                Path(profile_path).parent.mkdir(parents=True, exist_ok=True)
                profiler.dump_stats(str(profile_path))
        if summary.get("status") not in {"COMPLETE", "INCOMPLETE"}:
            raise ParallelExecutionError("worker returned an invalid terminal game status")

    def _write_worker_error(self, task: Mapping[str, Any], exc: BaseException) -> None:
        output_dir = Path(task["kwargs"]["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"parallel-worker-error-{os.getpid()}.json"
        payload = {
            "schema_id": "issue60-parallel-worker-error-v1",
            "task_key": list(task["key"]), "pid": os.getpid(),
            "failure": f"{type(exc).__name__}: {exc}",
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)

    def _fork_task(self, task: Mapping[str, Any], slot: int) -> int:
        pid = os.fork()
        if pid == 0:
            try:
                self.tracker.register_worker(slot, os.getpid())
                self._child_run(task)
                os._exit(0)
            except BaseException as exc:
                try:
                    self._write_worker_error(task, exc)
                except BaseException:
                    pass
                os._exit(70)
        # Register in the parent too, before clearing the pending dispatch
        # reservation. This closes the interval where a child has been forked
        # but an already-running worker could observe neither pending nor live.
        self.tracker.register_worker(slot, pid)
        return pid

    def _budget_guard(self) -> None:
        self.rs.budget_guard(
            remaining_games=max(0, 2688 - self.rs._completed_run_count()),
            remaining_optimizer_cpu=self.rs._remaining_optimizer_projection(),
            current_stage_cpu=self.tracker.current_stage_cpu(),
        )

    def _mark_abnormal_attempt_incomplete(self, task: Mapping[str, Any],
                                          failure: str, cpu_seconds: float) -> None:
        output_dir = Path(task["kwargs"]["output_dir"])
        for status_path in sorted(output_dir.glob("attempt-*/status.json")):
            try:
                record = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if record.get("status") != "RUNNING":
                continue
            record["status"] = "INCOMPLETE"
            record["failure"] = failure
            record["process_cpu_seconds"] = max(0.0, cpu_seconds)
            record["completed_cpu_seconds"] = time.process_time()
            temp = status_path.with_name(status_path.name + f".tmp-{os.getpid()}")
            temp.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, status_path)

    def run_jobs(self, tasks: Sequence[Mapping[str, Any]]) -> dict[tuple[str, int, str], tuple[Path, dict[str, Any]]]:
        pending = deque()
        for task in tasks:
            key = tuple(task["key"])
            kwargs = task["kwargs"]
            existing = self.rs.successful_attempt(
                Path(kwargs["output_dir"]), str(kwargs["model_sha"]), str(kwargs["config_digest"])
            )
            if existing is not None:
                self.results[key] = existing
            else:
                pending.append(dict(task))
        active: dict[int, tuple[dict[str, Any], float]] = {}
        free_slots = deque(range(self.workers))
        stop_error: BaseException | None = None
        while pending or active:
            while pending and len(active) < self.workers and stop_error is None:
                try:
                    self.tracker.set_pending_workers(1)
                    self._budget_guard()
                except BaseException as exc:
                    stop_error = exc
                    self.tracker.set_pending_workers(0)
                    break
                task = pending.popleft()
                start = time.monotonic()
                try:
                    slot = free_slots.popleft()
                    pid = self._fork_task(task, slot)
                except OSError as exc:
                    self.tracker.set_pending_workers(0)
                    stop_error = ParallelExecutionError(f"could not start fixed-key worker: {exc}")
                    pending.appendleft(task)
                    break
                self.tracker.set_pending_workers(0)
                active[pid] = (task, start)
                self.max_active_workers = max(self.max_active_workers, len(active))
            if not active:
                break
            try:
                pid, wait_status, usage = os.wait4(-1, 0)
            except InterruptedError:
                continue
            item = active.pop(pid, None)
            if item is None:
                self.tracker.add_reaped_cpu(float(usage.ru_utime + usage.ru_stime))
                stop_error = stop_error or ParallelExecutionError(f"reaped an unregistered child process {pid}")
                continue
            task, started_wall = item
            child_cpu = float(usage.ru_utime + usage.ru_stime)
            slot = self.tracker.reap_worker(pid, child_cpu)
            if slot is not None:
                free_slots.append(slot)
            self.max_worker_rss_bytes = max(self.max_worker_rss_bytes, int(usage.ru_maxrss) * 1024)
            task_key = tuple(task["key"])
            summary_pair = _latest_run_summary(self.rs, task)
            exit_code = os.waitstatus_to_exitcode(wait_status)
            execution = {
                "task_key": list(task_key), "pid": pid, "exit_code": exit_code,
                "process_cpu_seconds_including_worker_setup": child_cpu,
                "run_game_cpu_seconds": None,
                "peak_rss_bytes": int(usage.ru_maxrss) * 1024,
                "wall_seconds": max(0.0, time.monotonic() - started_wall),
            }
            if summary_pair is not None:
                attempt, summary = summary_pair
                execution["attempt"] = str(attempt.relative_to(self.rs.HERE))
                execution["run_game_cpu_seconds"] = float(summary.get("process_cpu_seconds", 0.0))
                self.tracker.observe_game_cpu(execution["run_game_cpu_seconds"])
                if exit_code == 0 and summary.get("status") == "COMPLETE":
                    self.results[task_key] = (attempt, summary)
                else:
                    failure = str(summary.get("failure") or summary.get("problems") or f"worker exit {exit_code}")
                    self._mark_abnormal_attempt_incomplete(task, failure, child_cpu)
                    if "ResourceBudgetExceeded" in failure or "RESOURCE_BLOCKED" in failure:
                        stop_error = stop_error or self._resource_budget_exception(failure)
                    else:
                        stop_error = stop_error or ParallelExecutionError(
                            f"fixed game {task_key} is INCOMPLETE: {failure}"
                        )
            else:
                failure = "worker exited without a terminal run record"
                error_path = Path(task["kwargs"]["output_dir"]) / f"parallel-worker-error-{pid}.json"
                if error_path.is_file():
                    try:
                        failure = json.loads(error_path.read_text(encoding="utf-8")).get("failure", failure)
                    except (OSError, json.JSONDecodeError):
                        pass
                self._mark_abnormal_attempt_incomplete(task, failure, child_cpu)
                if "ResourceBudgetExceeded" in failure or "RESOURCE_BLOCKED" in failure:
                    stop_error = stop_error or self._resource_budget_exception(failure)
                else:
                    stop_error = stop_error or ParallelExecutionError(f"fixed game {task_key} failed: {failure}")
            self.executions.append(execution)
            if stop_error is not None:
                # Do not start more games; active workers encounter the same
                # aggregate guard at their next decision and are settled below.
                continue
        if stop_error is not None:
            raise stop_error
        missing = [tuple(task["key"]) for task in tasks if tuple(task["key"]) not in self.results]
        if missing:
            raise ParallelExecutionError(f"fixed-key batch did not settle: {missing[:4]}")
        ordered = ordered_result_items(
            [tuple(task["key"]) for task in tasks], self.results,
        )
        return dict(ordered)

    @staticmethod
    def _resource_budget_exception(message: str) -> BaseException:
        try:
            from resource_amendment_r2 import ResourceBudgetExceeded
            return ResourceBudgetExceeded(message)
        except ImportError:
            return ParallelExecutionError(message)

    def _training_tasks(self, template: Mapping[str, Any]) -> list[dict[str, Any]]:
        rs = self.rs
        kwargs = dict(template)
        stage_key = str(kwargs["stage_key"])
        block = str(kwargs["block_name"])
        batch_index = int(stage_key.rsplit("-", 1)[1])
        seeds = list(rs.TRAINING_SEEDS[block])[(batch_index - 1) * 128:batch_index * 128]
        frozen_sha = str(kwargs["model_sha"])
        digest = str(kwargs["config_digest"])
        tasks = []
        for seed in seeds:
            output = rs._training_game_dir(block, batch_index, seed)
            if rs.successful_attempt(output, frozen_sha, digest) is not None:
                self.results[(stage_key, seed, "residual")] = rs.successful_attempt(output, frozen_sha, digest)
                continue
            task_kwargs = dict(kwargs)
            task_kwargs.update({
                "seed": seed,
                "runtime_seed": rs.derive_seed("training-policy", rs.MODEL_SEEDS[block], seed, batch_index),
                "output_dir": output,
            })
            tasks.append({"key": (stage_key, seed, "residual"), "kwargs": task_kwargs})
        return tasks

    def _evaluation_tasks(self, template: Mapping[str, Any]) -> list[dict[str, Any]]:
        rs = self.rs
        kwargs = dict(template)
        stage_key = str(kwargs["stage_key"])
        owner = int(stage_key.split("-", 1)[1])
        seed = int(kwargs["seed"])
        config = json.loads(rs.CONFIG_PATH.read_text(encoding="utf-8"))
        digest = str(kwargs["config_digest"])
        vocab = kwargs["vocab"]
        bootstrap = kwargs["bootstrap"]
        runtime_seed = int(kwargs["runtime_seed"])
        tasks = []
        modes = {
            "final_block1": ("residual", self.final_models["block1"][0], "block1", self.final_models["block1"][1]),
            "final_block2": ("residual", self.final_models["block2"][0], "block2", self.final_models["block2"][1]),
            "p0": ("p0", None, "evaluation", "p0"),
            "A": ("A", None, "evaluation", "A"),
            "Expert": ("Expert", None, "evaluation", "Expert"),
        }
        for arm in rs.EVAL_ARMS:
            mode, model, block, model_sha = modes[arm]
            output = rs.EVIDENCE / "runs" / "evaluation" / f"owner-{owner:03d}" / arm
            if rs.successful_attempt(output, model_sha, digest) is not None:
                self.results[(stage_key, seed, arm)] = rs.successful_attempt(output, model_sha, digest)
                continue
            task_kwargs = dict(kwargs)
            task_kwargs.update({
                "mode": mode, "model": model, "block_name": block,
                "model_sha": model_sha, "runtime_seed": runtime_seed,
                "output_dir": output,
                "provenance_config": rs._current_policy_provenance(config, vocab, model_sha, mode),
                "require_targets": False, "stage_key": stage_key, "arm": arm,
            })
            tasks.append({"key": (stage_key, seed, arm), "kwargs": task_kwargs})
        return tasks

    def dispatch_run_game(self, **kwargs: Any) -> tuple[Path, dict[str, Any]]:
        stage_key = str(kwargs.get("stage_key", ""))
        seed = int(kwargs["seed"])
        arm = str(kwargs["arm"])
        key = (stage_key, seed, arm)
        if key in self.results:
            return self.results[key]
        if stage_key.startswith(("block1:batch-", "block2:batch-")):
            tasks = self._training_tasks(kwargs)
        elif stage_key.startswith("owner-"):
            tasks = self._evaluation_tasks(kwargs)
        else:
            tasks = [{"key": key, "kwargs": dict(kwargs)}]
        if tasks:
            self.run_jobs(tasks)
        if key not in self.results:
            raise ParallelExecutionError(f"dispatcher has no settled result for {key}")
        return self.results[key]

    def wrap_model_loader(self) -> Callable[..., Any]:
        original = self.rs._load_final_model

        def record(block: str, vocab: Any, digest: str):
            loaded = original(block, vocab, digest)
            self.final_models[block] = loaded
            return loaded

        return record

    def evidence(self) -> dict[str, Any]:
        parent_rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
        parent_worker_rss_bound = parent_rss + self.max_worker_rss_bytes * self.max_active_workers
        return {
            "max_configured_workers": self.workers,
            "max_active_workers": self.max_active_workers,
            "parent_peak_rss_bytes": parent_rss,
            "worker_peak_rss_bytes": self.max_worker_rss_bytes,
            "parent_plus_worker_peak_rss_upper_bound_bytes": parent_worker_rss_bound,
            "execution_count": len(self.executions),
            "executions": list(self.executions),
        }


def profile_summary(profile_paths: Sequence[Path], *, run_cpu: float,
                    search_cpu: float) -> dict[str, Any]:
    """Summarize exclusive profile time by requested implementation boundary."""
    categories: dict[str, float] = {}
    inclusive: dict[str, float] = {}
    for profile_path in profile_paths:
        stats = pstats.Stats(str(profile_path))
        for (filename, _line, function), (_primitive, _calls, own, cumulative, _callers) in stats.stats.items():
            base = Path(filename).name
            if base in {"controlled_run.py", "lightspeed.py", "online_controller.py"}:
                category = "native reset/step/snapshot/public projection and executor"
            elif base in {"bootstrap_v47.py"}:
                category = "A bootstrap tokenization/hash/scoring"
            elif base in {"normalizer_v49.py", "adapter_v47.py", "public_run_context.py"}:
                category = "public context/history and legacy feature construction"
            elif base in {"encoder.py", "model.py"}:
                category = "residual normalization/encoding/forward"
            elif base in {"run_study.py"} and function in {"compress_jsonl", "atomic_json", "__call__", "read_jsonl", "decision_rows"}:
                category = "JSON/history/log serialization/compression"
            elif "json" in filename.lower() or base in {"gzip.py", "zlib.py"}:
                category = "JSON/history/log serialization/compression"
            elif base in {"controllers.py"}:
                category = "search report/action mapping and policy dispatch"
            else:
                category = "other Python/executor remainder"
            categories[category] = categories.get(category, 0.0) + float(own)
            inclusive[category] = inclusive.get(category, 0.0) + float(cumulative)
    return {
        "profiling_method": "cProfile; category exclusive seconds sum function self times, inclusive seconds sum cumulative times and may overlap",
        "category_exclusive_cpu_seconds": categories,
        "category_inclusive_cpu_seconds_overlapping": inclusive,
        "native_search_cpu_seconds_from_existing_timer": float(search_cpu),
        "run_game_process_cpu_seconds": float(run_cpu),
        "cprofile_exclusive_sum_seconds": sum(categories.values()),
        "unaccounted_run_game_cpu_seconds": max(0.0, float(run_cpu) - sum(categories.values())),
    }


def compare_replay(original: Mapping[str, Any], replay: Mapping[str, Any],
                   original_decisions: Sequence[Mapping[str, Any]],
                   replay_decisions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Require matching policy/context/target and terminal evidence, not timing metadata."""
    summary_fields = (
        "status", "game_seed", "mode", "block", "stage_key", "arm",
        "runtime_policy_seed", "model_sha256", "config_sha256", "terminal",
        "outcome", "terminal_floor", "terminal_public_location", "death_location",
        "supported_choices", "bootstrap_shifts", "visible_bosses_seen",
        "heart_entry_selected", "screen_counts", "transitions", "boundaries", "problems",
    )
    summary_equal = all(original.get(field) == replay.get(field) for field in summary_fields)
    decision_equal = list(original_decisions) == list(replay_decisions)
    if not summary_equal or not decision_equal:
        raise ParallelExecutionError("parallel replay changed public decisions, behavior probabilities, targets, or terminal outcomes")
    return {
        "status": "PASS", "summary_fields_compared": list(summary_fields),
        "decision_rows": len(original_decisions),
        "decision_fields": "complete serialized decision rows (including public input, chosen action, behavior/base probabilities, and terminal targets)",
        "timing_path_pid_metadata_excluded": True,
    }

