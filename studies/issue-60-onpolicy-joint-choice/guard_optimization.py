"""Study-local cached view of the Issue 60 execution guard inputs.

The frozen R2 receipt/source remains untouched.  This module is an opt-in
execution overlay for the Planner-bounded retained-key replay only.  It builds
the same guard inputs once, then accepts updates at durable game, batch, ledger,
and worker-settlement events.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
from pathlib import Path
from typing import Any, Mapping


class GuardState:
    """In-memory index matching the existing R2 filesystem calculations."""

    def __init__(self, study_root: Path, resource_module: Any) -> None:
        self.study_root = Path(study_root).resolve()
        self.resource_module = resource_module
        self.evidence = self.study_root / "evidence"
        self.runs_root = self.evidence / "runs"
        self._lock = threading.RLock()
        self._cost_rows_by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
        self._completed_run_keys: set[tuple[str, int, str]] = set()
        self._seen_attempt_paths: set[str] = set()
        self._completed_batch_paths: set[str] = set()
        self.max_observed_game_cpu_seconds = 0.0
        self.max_supported_choices = 0
        self.settled_worker_cpu_seconds = 0.0
        self.worker_settlements: list[dict[str, Any]] = []
        self._scan_run_history_once()
        self._scan_optimizer_history_once()
        self._ledger_cpu_seconds = float(resource_module._ledger_total(self.study_root))
        native_path = self.evidence / "preflight" / "native.json"
        if native_path.is_file():
            self._native_gate = resource_module._read_json(native_path)
            self._measured_minibatch_cpu = float(
                self._native_gate["optimizer_projection"]
                ["measured_full_256_row_minibatch_cpu_seconds"]
            )
        else:
            self._native_gate = None
            self._measured_minibatch_cpu = 0.0

    def _error(self, message: str) -> BaseException:
        error_type = getattr(self.resource_module, "ResourceAmendmentErrorR2", ValueError)
        return error_type(message)

    @staticmethod
    def _key(row: Mapping[str, Any], path: Path) -> tuple[str, int, str]:
        try:
            return str(row["stage_key"]), int(row["game_seed"]), str(row["arm"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"complete run lacks cost identity: {path}") from exc

    def _row_order(self, row: Mapping[str, Any]) -> tuple[int, ...]:
        return self.resource_module._declared_order(row)

    def _scan_run_history_once(self) -> None:
        candidates: dict[tuple[str, int, str], dict[str, Any]] = {}
        if self.runs_root.exists():
            paths = sorted(self.runs_root.rglob("run.json"))
        else:
            paths = []
        for path in paths:
            try:
                raw = path.read_bytes()
                row = json.loads(raw)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(row, Mapping) or row.get("status") != "COMPLETE":
                continue
            key = self._key(row, path)
            try:
                cost = float(row["process_cpu_seconds"])
            except (KeyError, TypeError, ValueError) as exc:
                raise self._error(f"complete run has invalid process CPU: {path}") from exc
            if not math.isfinite(cost) or cost < 0:
                raise self._error(f"complete run has invalid process CPU: {path}")
            self._row_order(row)
            relative = path.parent.relative_to(self.study_root).as_posix()
            self._seen_attempt_paths.add(relative)
            self.max_observed_game_cpu_seconds = max(
                self.max_observed_game_cpu_seconds, cost,
            )
            stage = str(row.get("stage_key"))
            if stage == "preflight" or stage.startswith(("block1:batch-", "block2:batch-")):
                try:
                    choices = int(row.get("supported_choices", 0))
                except (TypeError, ValueError) as exc:
                    raise self._error(f"complete run has invalid supported-choice count: {path}") from exc
                self.max_supported_choices = max(self.max_supported_choices, choices)
            if stage.startswith(("block1:batch-", "block2:batch-", "owner-")):
                self._completed_run_keys.add(key)
            candidates.setdefault(key, {
                "stage_key": key[0],
                "game_seed": key[1],
                "arm": key[2],
                "process_cpu_seconds": cost,
                "attempt_path": relative,
                "run_json_sha256": hashlib.sha256(raw).hexdigest(),
            })
        self._cost_rows_by_key = candidates

    def _scan_optimizer_history_once(self) -> None:
        root = self.evidence / "training"
        if not root.exists():
            return
        for path in sorted(root.glob("block*/batch-*.json")):
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            self._completed_batch_paths.add(path.relative_to(self.study_root).as_posix())

    def _assert_root(self, study_root: Path) -> None:
        if Path(study_root).resolve() != self.study_root:
            raise ValueError("cached guard state cannot serve a different study root")

    def complete_game_costs(self, study_root: Path) -> list[dict[str, Any]]:
        self._assert_root(study_root)
        with self._lock:
            rows = sorted(self._cost_rows_by_key.values(), key=self._row_order)
            return [dict(row) for row in rows]

    def completed_run_count(self) -> int:
        with self._lock:
            return len(self._completed_run_keys)

    def ledger_total(self, study_root: Path) -> float:
        self._assert_root(study_root)
        with self._lock:
            return self._ledger_cpu_seconds

    def remaining_optimizer_cpu(self) -> float:
        if self._native_gate is None:
            return 0.0
        rows_per_batch = math.ceil(self.max_supported_choices * 1.5) * 128
        minibatches_per_epoch = math.ceil(rows_per_batch / 256)
        remaining_batches = max(0, 16 - len(self._completed_batch_paths))
        return remaining_batches * 4 * minibatches_per_epoch * self._measured_minibatch_cpu * 1.5

    def r2_optimizer_projection(self, study_root: Path) -> dict[str, Any]:
        self._assert_root(study_root)
        rows_per_batch = math.ceil(self.max_supported_choices * 1.5) * 128
        minibatches_per_epoch = math.ceil(rows_per_batch / 256)
        completed_batches = len(self._completed_batch_paths)
        remaining_batches = max(0, 16 - completed_batches)
        minibatches = remaining_batches * 4 * minibatches_per_epoch
        multiplier = float(self.resource_module.OPTIMIZER_COST_MULTIPLIER)
        return {
            "max_supported_choices": self.max_supported_choices,
            "projected_decisions_per_batch": rows_per_batch,
            "projected_minibatches_per_epoch": minibatches_per_epoch,
            "completed_batches": completed_batches,
            "remaining_batches": remaining_batches,
            "remaining_optimizer_minibatches": minibatches,
            "measured_full_256_row_minibatch_cpu_seconds": self._measured_minibatch_cpu,
            "optimizer_cost_multiplier": multiplier,
            "remaining_optimizer_cpu_seconds": minibatches * self._measured_minibatch_cpu * multiplier,
        }

    def register_complete(self, attempt_dir: Path, summary: Mapping[str, Any]) -> bool:
        """Index a newly finalized formal run; ignore isolated replay outputs."""
        attempt = Path(attempt_dir).resolve()
        try:
            attempt.relative_to(self.runs_root.resolve())
        except ValueError:
            return False
        relative = attempt.relative_to(self.study_root).as_posix()
        with self._lock:
            if relative in self._seen_attempt_paths:
                return False
        run_path = attempt / "run.json"
        try:
            raw = run_path.read_bytes()
            stored = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise self._error(f"completed run event has no readable authoritative summary: {run_path}") from exc
        if not isinstance(stored, Mapping) or stored.get("status") != "COMPLETE":
            return False
        if summary.get("status") != "COMPLETE":
            return False
        key = self._key(stored, run_path)
        try:
            cost = float(stored["process_cpu_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise self._error(f"complete run has invalid process CPU: {run_path}") from exc
        if not math.isfinite(cost) or cost < 0:
            raise self._error(f"complete run has invalid process CPU: {run_path}")
        self._row_order(stored)
        with self._lock:
            if relative in self._seen_attempt_paths:
                return False
            self._seen_attempt_paths.add(relative)
            self.max_observed_game_cpu_seconds = max(self.max_observed_game_cpu_seconds, cost)
            stage = key[0]
            if stage == "preflight" or stage.startswith(("block1:batch-", "block2:batch-")):
                choices = int(stored.get("supported_choices", 0))
                self.max_supported_choices = max(self.max_supported_choices, choices)
            if stage.startswith(("block1:batch-", "block2:batch-", "owner-")):
                self._completed_run_keys.add(key)
            self._cost_rows_by_key.setdefault(key, {
                "stage_key": key[0],
                "game_seed": key[1],
                "arm": key[2],
                "process_cpu_seconds": cost,
                "attempt_path": relative,
                "run_json_sha256": hashlib.sha256(raw).hexdigest(),
            })
            return True

    def register_batch_json(self, path: Path) -> bool:
        candidate = Path(path).resolve()
        root = (self.evidence / "training").resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            return False
        if not (relative.startswith("block1/") or relative.startswith("block2/")):
            return False
        if not relative.startswith(("block1/batch-", "block2/batch-")) or not relative.endswith(".json"):
            return False
        try:
            json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return False
        with self._lock:
            before = len(self._completed_batch_paths)
            self._completed_batch_paths.add(candidate.relative_to(self.study_root).as_posix())
            return len(self._completed_batch_paths) != before

    def add_ledger_row(self, row: Mapping[str, Any]) -> None:
        try:
            amount = float(row.get("process_cpu_seconds", 0.0))
        except (TypeError, ValueError) as exc:
            raise self._error("new CPU ledger row has invalid process CPU") from exc
        if not math.isfinite(amount) or amount < 0:
            raise self._error("new CPU ledger row has invalid process CPU")
        with self._lock:
            self._ledger_cpu_seconds += amount

    def settle_worker(self, pid: int, cpu_seconds: float, slot: int | None) -> None:
        amount = float(cpu_seconds)
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("settled worker CPU must be finite and nonnegative")
        with self._lock:
            self.settled_worker_cpu_seconds += amount
            self.worker_settlements.append({
                "pid": int(pid), "cpu_seconds": amount,
                "slot": None if slot is None else int(slot),
            })

    def sync_dispatch_results(self, results: Mapping[Any, Any]) -> int:
        """Ingest settled completions before the dispatcher checks next reserve."""
        updated = 0
        for attempt_dir, summary in results.values():
            if isinstance(summary, Mapping) and self.register_complete(attempt_dir, summary):
                updated += 1
        return updated


def install_cached_guard(run_study: Any, run_study_r2: Any,
                         resource_module: Any, state: GuardState,
                         parallel_module: Any | None = None,
                         on_worker_settlement: Any | None = None) -> None:
    """Install cache hooks after the unchanged R2 wrapper has been attached."""
    native_path = (state.evidence / "preflight" / "native.json").resolve()
    original_read_json = run_study_r2._read_json

    def cached_read_json(path: Path) -> Any:
        if Path(path).resolve() == native_path and state._native_gate is not None:
            return copy.deepcopy(state._native_gate)
        return original_read_json(path)

    resource_module.complete_game_costs = state.complete_game_costs
    resource_module._ledger_total = state.ledger_total
    resource_module.remaining_optimizer_projection = state.r2_optimizer_projection
    run_study_r2.complete_game_costs = state.complete_game_costs
    run_study_r2._ledger_total = state.ledger_total
    run_study_r2._read_json = cached_read_json

    run_study._completed_run_count = state.completed_run_count
    run_study._remaining_optimizer_projection = lambda: state.remaining_optimizer_cpu()
    run_study._max_observed_game_seconds = lambda: state.max_observed_game_cpu_seconds

    original_atomic_json = run_study.atomic_json

    def atomic_json_with_batch_update(path: Path, value: Mapping[str, Any]) -> Any:
        result = original_atomic_json(path, value)
        state.register_batch_json(path)
        return result

    run_study.atomic_json = atomic_json_with_batch_update

    original_game = run_study.run_game

    def run_game_with_completion_update(**kwargs: Any) -> Any:
        result = original_game(**kwargs)
        if isinstance(result, tuple) and len(result) == 2:
            state.register_complete(Path(result[0]), result[1])
        return result

    run_study.run_game = run_game_with_completion_update

    original_rs_ledger = run_study._append_ledger

    def append_rs_ledger(row: Mapping[str, Any]) -> Any:
        result = original_rs_ledger(row)
        state.add_ledger_row(row)
        return result

    run_study._append_ledger = append_rs_ledger

    original_r2_ledger = run_study_r2._append_ledger

    def append_r2_ledger(row: dict[str, Any]) -> Any:
        result = original_r2_ledger(row)
        state.add_ledger_row(row)
        return result

    run_study_r2._append_ledger = append_r2_ledger

    if parallel_module is not None:
        original_reap = parallel_module.ProcessCpuTracker.reap_worker

        def reap_worker_with_state(tracker: Any, pid: int, seconds: float) -> Any:
            slot = original_reap(tracker, pid, seconds)
            state.settle_worker(pid, seconds, slot)
            if on_worker_settlement is not None:
                on_worker_settlement(pid, seconds, slot)
            return slot

        parallel_module.ProcessCpuTracker.reap_worker = reap_worker_with_state

        original_dispatch_guard = parallel_module.ParallelGameDispatcher._budget_guard

        def dispatch_guard_with_state(dispatcher: Any) -> Any:
            state.sync_dispatch_results(dispatcher.results)
            return original_dispatch_guard(dispatcher)

        parallel_module.ParallelGameDispatcher._budget_guard = dispatch_guard_with_state
