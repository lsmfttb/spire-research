"""STUDY_ONLY demand-driven public-history execution for Issue 60.

The pinned STSRL executor remains the sole run/selection/validation/step path.
This installer replaces only its public-context/history helpers for this
study's actual production consumer:

* validate each newly created history entry once at append time;
* retain those sanitized entries privately in append order;
* materialize a detached full history only for MAP/REWARDS actor inputs and
  once for the final run context;
* omit the unused growing history from Battle/OTHER and post-transition
  contexts while preserving all current public projection fields.

The resulting MAP/REWARDS input is byte-for-byte equivalent to the pinned
builder. The native adapter, controller, action resolution and PPO code remain
unchanged. Search is still run once per real Battle action by the existing
FrozenBattleSearch controller.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import fcntl
import functools
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence


SUPPORTED_HISTORY_CONSUMERS = frozenset({"MAP_SCREEN", "REWARDS"})
OPTIMIZER_ID = "issue60-demand-driven-public-history-v1"


class ValidatedPublicHistory(list):
    """Private append-only list whose entries passed the pinned sanitizer."""

    def __init__(self) -> None:
        super().__init__()
        self.missing_paths: set[str] = set()


def _counter_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for key, value in after.items():
        old = before.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            delta[key] = value - (old if isinstance(old, (int, float)) else 0)
        elif isinstance(value, dict):
            delta[key] = _counter_delta(old if isinstance(old, dict) else {}, value)
        elif isinstance(value, list):
            delta[key] = len(value) - (len(old) if isinstance(old, list) else 0)
        else:
            delta[key] = value
    return delta


class _MeteredAdapter:
    def __init__(self, adapter: Any, metrics: dict[str, Any]):
        self._adapter = adapter
        self._metrics = metrics

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    def _call(self, metric: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        cpu_start = time.process_time()
        wall_start = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            bucket = self._metrics.setdefault(metric, {"calls": 0, "cpu_seconds": 0.0, "wall_seconds": 0.0})
            bucket["calls"] += 1
            bucket["cpu_seconds"] += max(0.0, time.process_time() - cpu_start)
            bucket["wall_seconds"] += max(0.0, time.monotonic() - wall_start)

    def reset(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("adapter_reset", self._adapter.reset, *args, **kwargs)

    def legal_actions(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("adapter_legal_actions", self._adapter.legal_actions, *args, **kwargs)

    def step(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("adapter_step", self._adapter.step, *args, **kwargs)

    def public_projection(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("adapter_public_projection", self._adapter.public_projection, *args, **kwargs)

    def battle_search_v2(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("native_battle_search", self._adapter.battle_search_v2, *args, **kwargs)


class _MeteredController:
    def __init__(self, controller: Any, metrics: dict[str, Any]):
        self._controller = controller
        self._metrics = metrics

    def __getattr__(self, name: str) -> Any:
        return getattr(self._controller, name)

    def reset_for_run(self, seed: int | None) -> Any:
        reset = getattr(self._controller, "reset_for_run", None)
        return reset(seed) if callable(reset) else None

    def select_action(self, adapter: Any, snapshot: Any, actions: Sequence[Any], context: Any, step_index: int) -> Any:
        screen = str(getattr(context, "screen_state", "(none)"))
        bucket_name = (
            "public_choice_controller"
            if screen in SUPPORTED_HISTORY_CONSUMERS
            else "controller_battle_inclusive" if screen == "BATTLE"
            else "controller_other"
        )
        cpu_start = time.process_time()
        wall_start = time.monotonic()
        try:
            return self._controller.select_action(adapter, snapshot, actions, context, step_index)
        finally:
            bucket = self._metrics.setdefault(bucket_name, {"calls": 0, "cpu_seconds": 0.0, "wall_seconds": 0.0})
            bucket["calls"] += 1
            bucket["cpu_seconds"] += max(0.0, time.process_time() - cpu_start)
            bucket["wall_seconds"] += max(0.0, time.monotonic() - wall_start)


class PublicHistoryExecution:
    def __init__(self, controlled_run: Any, public_context: Any, metrics_path: Path | None):
        self.controlled_run = controlled_run
        self.public_context = public_context
        self.metrics_path = metrics_path
        self.original_build = controlled_run.build_public_run_context
        self.original_append = controlled_run.append_public_history_entry
        self.original_execute = controlled_run.execute_controlled_run
        self.stats: dict[str, Any] = {
            "context_build_calls": 0,
            "contexts_without_history": 0,
            "supported_full_history_materializations": 0,
            "supported_materialized_entries": 0,
            "terminal_full_history_materializations": 0,
            "terminal_materialized_entries": 0,
            "new_entries_sanitized": 0,
            "previous_entries_recursively_revalidated": 0,
            "untrusted_history_fallbacks": 0,
            "context_build_cpu_seconds": 0.0,
            "history_copy_cpu_seconds": 0.0,
            "new_entry_validation_cpu_seconds": 0.0,
            "append_cpu_seconds": 0.0,
            "terminal_materialization_cpu_seconds": 0.0,
        }
        self._install()

    def _materialize_history(self, context: dict[str, Any], history: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Attach detached validated history and its cached missing-field paths."""
        if not isinstance(history, ValidatedPublicHistory):
            raise TypeError("full history materialization requires validated study history")
        cpu_start = time.process_time()
        context["history"] = copy.deepcopy(list(history))
        context["missing_fields"] = sorted(set(context.get("missing_fields", [])).union(history.missing_paths))
        self.stats["history_copy_cpu_seconds"] += max(0.0, time.process_time() - cpu_start)
        return context

    def _append_validated(self, history: Sequence[Mapping[str, Any]], entry: Mapping[str, Any]) -> ValidatedPublicHistory:
        total_start = time.process_time()
        expected_index = len(history)
        observed_index = entry.get("history_index") if isinstance(entry, Mapping) else None
        if observed_index != expected_index:
            raise ValueError(
                f"public history entry index {observed_index!r} does not match expected {expected_index}"
            )
        validation_start = time.process_time()
        safe_entry = self.public_context.sanitize_public_run_context(entry)
        schema_probe = dict(safe_entry)
        schema_probe["history_index"] = 0
        problems = self.public_context.public_history_problems([schema_probe])
        if problems:
            raise ValueError("; ".join(problems))
        self.stats["new_entry_validation_cpu_seconds"] += max(0.0, time.process_time() - validation_start)
        if isinstance(history, ValidatedPublicHistory):
            retained = history
        elif not history:
            retained = ValidatedPublicHistory()
        else:
            # The actual executor starts with []. If another consumer supplies
            # a nonempty untrusted list, use its original full validator.
            self.stats["untrusted_history_fallbacks"] += 1
            rebuilt = self.original_append(history, safe_entry)
            if isinstance(rebuilt, ValidatedPublicHistory):
                retained = rebuilt
            else:
                retained = ValidatedPublicHistory()
                for index, prior in enumerate(rebuilt):
                    retained.append(prior)
                    paths = self.public_context.explicit_missing_paths({"history": [prior]})
                    retained.missing_paths.update(
                        path.replace("$.history[0]", f"$.history[{index}]", 1) for path in paths
                    )
                return retained
        retained.append(safe_entry)
        paths = self.public_context.explicit_missing_paths({"history": [safe_entry]})
        retained.missing_paths.update(
            path.replace("$.history[0]", f"$.history[{expected_index}]", 1) for path in paths
        )
        self.stats["new_entries_sanitized"] += 1
        self.stats["append_cpu_seconds"] += max(0.0, time.process_time() - total_start)
        return retained

    def _install(self) -> None:
        @functools.wraps(self.original_build)
        def build_public_run_context(
            raw_snapshot: object,
            actions: Sequence[Any],
            *,
            projection: Any,
            history: Sequence[Mapping[str, Any]] = (),
            include_candidates: bool = True,
        ) -> dict[str, Any]:
            self.stats["context_build_calls"] += 1
            cpu_start = time.process_time()
            raw = raw_snapshot if isinstance(raw_snapshot, Mapping) else {}
            screen = str(raw.get("screen_state", "(none)"))
            need_history = include_candidates and screen in SUPPORTED_HISTORY_CONSUMERS
            try:
                base = self.original_build(
                    raw_snapshot,
                    actions,
                    projection=projection,
                    history=(),
                    include_candidates=include_candidates,
                )
                if not need_history or not history:
                    self.stats["contexts_without_history"] += 1
                    return base
                if not isinstance(history, ValidatedPublicHistory):
                    self.stats["untrusted_history_fallbacks"] += 1
                    return self.original_build(
                        raw_snapshot,
                        actions,
                        projection=projection,
                        history=history,
                        include_candidates=include_candidates,
                    )
                materialized = self._materialize_history(base, history)
                self.stats["supported_full_history_materializations"] += 1
                self.stats["supported_materialized_entries"] += len(history)
                return materialized
            finally:
                self.stats["context_build_cpu_seconds"] += max(0.0, time.process_time() - cpu_start)

        @functools.wraps(self.original_append)
        def append_public_history_entry(history: Sequence[Mapping[str, Any]], entry: Mapping[str, Any]) -> list[dict[str, Any]]:
            return self._append_validated(history, entry)

        @functools.wraps(self.original_execute)
        def execute_controlled_run(
            adapter: Any,
            controller: Any,
            *,
            seed: int | None,
            max_steps: int,
            action_space: Any = None,
            before_decision_transform: Any = None,
            before_decision: Any = None,
            after_transition: Any = None,
            retain_steps: bool = True,
        ) -> Any:
            run_cpu_start = time.process_time()
            run_wall_start = time.monotonic()
            stats_before = copy.deepcopy(self.stats)
            measured_adapter = _MeteredAdapter(adapter, self.stats)
            measured_controller = _MeteredController(controller, self.stats)

            def timed_observer(name: str, callback: Any) -> Any:
                if not callable(callback):
                    return callback

                @functools.wraps(callback)
                def invoke(*args: Any, **kwargs: Any) -> Any:
                    cpu_start = time.process_time()
                    wall_start = time.monotonic()
                    try:
                        return callback(*args, **kwargs)
                    finally:
                        bucket = self.stats.setdefault(name, {"calls": 0, "cpu_seconds": 0.0, "wall_seconds": 0.0})
                        bucket["calls"] += 1
                        bucket["cpu_seconds"] += max(0.0, time.process_time() - cpu_start)
                        bucket["wall_seconds"] += max(0.0, time.monotonic() - wall_start)

                return invoke

            run = self.original_execute(
                measured_adapter,
                measured_controller,
                seed=seed,
                max_steps=max_steps,
                action_space=action_space,
                before_decision_transform=before_decision_transform,
                before_decision=timed_observer("before_observer", before_decision),
                after_transition=timed_observer("after_observer", after_transition),
                retain_steps=retain_steps,
            )
            if not run.problems and run.public_history:
                # The production consumer reads current/location/result only,
                # but preserve the exact full terminal context once rather
                # than rebuilding it for every preceding Battle transition.
                snapshot_factory = getattr(adapter, "_snapshot", None)
                if not callable(snapshot_factory):
                    raise RuntimeError("pinned adapter cannot materialize its terminal public snapshot")
                terminal_snapshot = snapshot_factory()
                projection = self.public_context.read_native_public_projection(adapter, terminal_snapshot)
                terminal_cpu_start = time.process_time()
                run = replace(
                    run,
                    public_run_context=self.original_build(
                        run.final_raw,
                        (),
                        projection=projection,
                        history=run.public_history,
                        include_candidates=False,
                    ),
                )
                self.stats["terminal_full_history_materializations"] += 1
                self.stats["terminal_materialized_entries"] += len(run.public_history)
                self.stats["terminal_materialization_cpu_seconds"] += max(0.0, time.process_time() - terminal_cpu_start)
            run_cpu = max(0.0, time.process_time() - run_cpu_start)
            run_wall = max(0.0, time.monotonic() - run_wall_start)
            record = {
                "schema_id": "issue60-public-history-execution-metric-v1",
                "optimizer_id": OPTIMIZER_ID,
                "pid": os.getpid(),
                "seed": seed,
                "requested_steps": max_steps,
                "completed_steps": len(run.public_history),
                "terminal": bool(run.terminal),
                "outcome": run.outcome,
                "problem_count": len(run.problems),
                "controlled_run_cpu_seconds": run_cpu,
                "controlled_run_wall_seconds": run_wall,
                "component_cpu_seconds": _counter_delta(stats_before, self.stats),
                "timing_note": "component timers are nested/inclusive and must not be summed; controlled_run totals are authoritative for this boundary",
            }
            self._append_metric(record)
            return run

        self.controlled_run.build_public_run_context = build_public_run_context
        self.controlled_run.append_public_history_entry = append_public_history_entry
        self.controlled_run.execute_controlled_run = execute_controlled_run
        self.controlled_run._issue60_public_history_execution = self

    def _append_metric(self, record: Mapping[str, Any]) -> None:
        if self.metrics_path is None:
            return
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.metrics_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise OSError("short write while appending public-history execution metric")
                offset += written
            os.fsync(fd)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    def installation_record(self) -> dict[str, Any]:
        return {
            "optimizer_id": OPTIMIZER_ID,
            "study_scope": "STUDY_ONLY",
            "pinned_stsr_commit": "7d172f7c24c9d29429b6946ba44f6b59760b0d96",
            "active_executor": "sts_combat_rl.sim.controlled_run.execute_controlled_run",
            "history_consumers": sorted(SUPPORTED_HISTORY_CONSUMERS),
            "new_entry_validation": "pinned sanitize_public_run_context plus schema/version/index check once before append",
            "existing_battle_history_validation": "none per Battle action; previously validated entries are privately retained",
            "full_history_materialization": "detached copy for supported MAP/REWARDS inputs and one terminal run context",
            "semantics": "action selection, candidate ordering, Native adapter, teacher/Search@400, training rows and PPO update unchanged",
            "metrics_path": str(self.metrics_path) if self.metrics_path else None,
        }


def install(*, metrics_path: Path | None = None, controlled_run_module: Any | None = None,
            public_context_module: Any | None = None) -> dict[str, Any]:
    """Install the study-local replacement into the pinned executor module."""
    if controlled_run_module is None:
        from sts_combat_rl.sim import controlled_run as controlled_run_module
    if public_context_module is None:
        from sts_combat_rl.sim import public_run_context as public_context_module
    existing = getattr(controlled_run_module, "_issue60_public_history_execution", None)
    if isinstance(existing, PublicHistoryExecution):
        if metrics_path is not None and existing.metrics_path != Path(metrics_path):
            raise RuntimeError("Issue 60 public-history optimizer already installed with another metrics path")
        return existing.installation_record()
    installed = PublicHistoryExecution(
        controlled_run_module,
        public_context_module,
        Path(metrics_path) if metrics_path is not None else None,
    )
    return installed.installation_record()


__all__ = ["OPTIMIZER_ID", "PublicHistoryExecution", "ValidatedPublicHistory", "install"]
