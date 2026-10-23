"""Issue 65 study-local runtime optimization and phase telemetry.

The pinned STSRL controlled-run executor remains the only simulation path.
This installer changes only study-local runtime plumbing: validated public
history is retained append-only, unused growing history is omitted from
intermediate contexts, and the final full context is materialized once.
Optional phase timers wrap the same pinned path and never change action
selection or simulator state.
"""
from __future__ import annotations

import contextvars
import copy
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import functools
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterator, Mapping, Sequence


SUPPORTED_HISTORY_CONSUMERS = frozenset({"MAP_SCREEN", "REWARDS"})
OPTIMIZER_ID = "issue65-demand-driven-public-history-v1"
METRICS_SCHEMA = "issue65-runtime-metrics-v1"
_ACTIVE_RUN: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "issue65_active_run_metrics", default=None
)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _bucket(phases: dict[str, Any], name: str) -> dict[str, Any]:
    return phases.setdefault(name, {
        "calls": 0,
        "inclusive_cpu_seconds": 0.0,
        "exclusive_cpu_seconds": 0.0,
        "inclusive_wall_seconds": 0.0,
        "exclusive_wall_seconds": 0.0,
    })


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Collect nested CPU/wall timers with non-overlapping exclusive time."""
    state = _ACTIVE_RUN.get()
    if state is None or not state["telemetry_enabled"]:
        yield
        return
    frame = {
        "name": name,
        "cpu_start": time.process_time(),
        "wall_start": time.monotonic(),
        "child_cpu_seconds": 0.0,
        "child_wall_seconds": 0.0,
    }
    stack = state["stack"]
    stack.append(frame)
    try:
        yield
    finally:
        cpu = max(0.0, time.process_time() - frame["cpu_start"])
        wall = max(0.0, time.monotonic() - frame["wall_start"])
        stack.pop()
        item = _bucket(state["phases"], name)
        item["calls"] += 1
        item["inclusive_cpu_seconds"] += cpu
        item["exclusive_cpu_seconds"] += max(0.0, cpu - frame["child_cpu_seconds"])
        item["inclusive_wall_seconds"] += wall
        item["exclusive_wall_seconds"] += max(0.0, wall - frame["child_wall_seconds"])
        if stack:
            stack[-1]["child_cpu_seconds"] += cpu
            stack[-1]["child_wall_seconds"] += wall


def count(name: str, amount: int = 1) -> None:
    state = _ACTIVE_RUN.get()
    if state is not None and state["telemetry_enabled"]:
        state["counts"][name] = state["counts"].get(name, 0) + amount


def _new_state(
    *,
    labels: Mapping[str, Any],
    metrics_path: Path | None,
    telemetry_enabled: bool,
    optimized: bool,
    projection_cache_enabled: bool = False,
) -> dict[str, Any]:
    return {
        "labels": dict(labels),
        "metrics_path": metrics_path,
        "telemetry_enabled": telemetry_enabled,
        "optimized": optimized,
        "projection_cache_enabled": projection_cache_enabled,
        "projection_cache": None,
        "post_context_cache": None,
        "last_projection": None,
        "current_battle_steps": 0,
        "longest_battle_steps": 0,
        "phases": {},
        "counts": {},
        "stack": [],
        "sequence_digest": hashlib.sha256(),
        "sequence_steps": 0,
    }


def _append_jsonl_durable(path: Path, record: Mapping[str, Any]) -> None:
    payload = (canonical(record) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("short write while appending Issue 65 runtime metric")
            offset += written
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _append_child_time(parent: dict[str, Any] | None, cpu: float, wall: float) -> None:
    if parent is None or not parent["telemetry_enabled"] or not parent["stack"]:
        return
    parent["stack"][-1]["child_cpu_seconds"] += cpu
    parent["stack"][-1]["child_wall_seconds"] += wall


class ValidatedPublicHistory(list):
    """Private append-only list whose entries passed the pinned sanitizer."""

    def __init__(self) -> None:
        super().__init__()
        self.missing_paths: set[str] = set()


class _MeteredAdapter:
    _TIMED = {
        "reset": "adapter_reset",
        "legal_actions": "adapter_legal_actions",
        "step": "adapter_step",
        "battle_search_v2": "native_battle_search",
        "capture_checkpoint": "checkpoint_capture",
        "restore_checkpoint": "checkpoint_restore",
        "checkpoint_fingerprint": "checkpoint_fingerprint",
        "public_projection": "adapter_public_projection",
    }

    def __init__(self, adapter: Any):
        self._adapter = adapter

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._adapter, name)
        metric = self._TIMED.get(name)
        if metric is None or not callable(value):
            return value

        @functools.wraps(value)
        def measured(*args: Any, **kwargs: Any) -> Any:
            with phase(metric):
                return value(*args, **kwargs)

        return measured


class _MeteredController:
    def __init__(self, controller: Any):
        self._controller = controller

    def __getattr__(self, name: str) -> Any:
        return getattr(self._controller, name)

    def reset_for_run(self, seed: int | None) -> Any:
        reset = getattr(self._controller, "reset_for_run", None)
        if not callable(reset):
            return None
        with phase("controller_reset"):
            return reset(seed)

    def select_action(self, adapter: Any, snapshot: Any, actions: Sequence[Any], context: Any, step_index: int) -> Any:
        screen = str(getattr(context, "screen_state", "(none)")).upper()
        count(f"decisions:{screen}")
        with phase(f"controller_select:{screen}"):
            return self._controller.select_action(adapter, snapshot, actions, context, step_index)


class PublicHistoryExecution:
    def __init__(
        self,
        controlled_run: Any,
        public_context: Any,
        *,
        optimized: bool,
        metrics_path: Path | None,
    ):
        self.controlled_run = controlled_run
        self.public_context = public_context
        self.optimized = optimized
        self.metrics_path = metrics_path
        self.original_build = controlled_run.build_public_run_context
        self.original_append = controlled_run.append_public_history_entry
        self.original_execute = controlled_run.execute_controlled_run
        self.original_build_decision_context = controlled_run.build_decision_context
        self.original_projection_reader = controlled_run._read_public_projection_for_context
        self.stats: dict[str, Any] = {
            "contexts_without_history": 0,
            "supported_full_history_materializations": 0,
            "supported_materialized_entries": 0,
            "terminal_full_history_materializations": 0,
            "terminal_materialized_entries": 0,
            "new_entries_sanitized": 0,
            "previous_entries_recursively_revalidated": 0,
            "untrusted_history_fallbacks": 0,
        }
        self._install()

    @contextmanager
    def scope(
        self,
        *,
        metrics_path: Path,
        labels: Mapping[str, Any],
        telemetry_enabled: bool = True,
    ) -> Iterator[dict[str, Any]]:
        state = _new_state(
            labels=labels,
            metrics_path=metrics_path,
            telemetry_enabled=telemetry_enabled,
            optimized=self.optimized,
        )
        cpu_start = time.process_time()
        wall_start = time.monotonic()
        token = _ACTIVE_RUN.set(state)
        try:
            yield state
        finally:
            cpu = max(0.0, time.process_time() - cpu_start)
            wall = max(0.0, time.monotonic() - wall_start)
            _ACTIVE_RUN.reset(token)
            if telemetry_enabled:
                self._append_metric({
                    "schema_id": METRICS_SCHEMA,
                    "record_kind": "arm_scope",
                    "optimizer_id": OPTIMIZER_ID if self.optimized else "baseline-unoptimized",
                    **dict(labels),
                    "run_cpu_seconds": cpu,
                    "run_wall_seconds": wall,
                    "phase_metrics": state["phases"],
                    "counts": state["counts"],
                    "unallocated_cpu_seconds": max(
                        0.0,
                        cpu - sum(float(item["exclusive_cpu_seconds"]) for item in state["phases"].values()),
                    ),
                    "unallocated_wall_seconds": max(
                        0.0,
                        wall - sum(float(item["exclusive_wall_seconds"]) for item in state["phases"].values()),
                    ),
                }, metrics_path=metrics_path)

    def _materialize_history(self, context: dict[str, Any], history: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if not isinstance(history, ValidatedPublicHistory):
            raise TypeError("full history materialization requires validated study history")
        with phase("public_history_materialize"):
            context["history"] = copy.deepcopy(list(history))
            context["missing_fields"] = sorted(
                set(context.get("missing_fields", [])).union(history.missing_paths)
            )
        return context

    def _append_validated(self, history: Sequence[Mapping[str, Any]], entry: Mapping[str, Any]) -> ValidatedPublicHistory:
        expected_index = len(history)
        observed_index = entry.get("history_index") if isinstance(entry, Mapping) else None
        if observed_index != expected_index:
            raise ValueError(
                f"public history entry index {observed_index!r} does not match expected {expected_index}"
            )
        with phase("public_history_new_entry_validation"):
            safe_entry = self.public_context.sanitize_public_run_context(entry)
            schema_probe = dict(safe_entry)
            schema_probe["history_index"] = 0
            problems = self.public_context.public_history_problems([schema_probe])
            if problems:
                raise ValueError("; ".join(problems))
        if isinstance(history, ValidatedPublicHistory):
            retained = history
        elif not history:
            retained = ValidatedPublicHistory()
        else:
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
        with phase("public_history_append"):
            retained.append(safe_entry)
            paths = self.public_context.explicit_missing_paths({"history": [safe_entry]})
            retained.missing_paths.update(
                path.replace("$.history[0]", f"$.history[{expected_index}]", 1) for path in paths
            )
        self.stats["new_entries_sanitized"] += 1
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
            with phase("public_context_build"):
                raw = raw_snapshot if isinstance(raw_snapshot, Mapping) else {}
                screen = str(raw.get("screen_state", "(none)")).upper()
                state = _ACTIVE_RUN.get()
                if state is not None and not include_candidates:
                    state["last_projection"] = projection
                if not self.optimized:
                    return self.original_build(
                        raw_snapshot, actions, projection=projection, history=history,
                        include_candidates=include_candidates,
                    )
                if state is not None and not include_candidates:
                    cached = state.get("post_context_cache")
                    if (
                        cached is not None
                        and cached["raw_snapshot"] is raw_snapshot
                        and cached["projection"] is projection
                    ):
                        if state["telemetry_enabled"]:
                            state["counts"]["post_context_cache_hits"] = (
                                state["counts"].get("post_context_cache_hits", 0) + 1
                            )
                        return cached["value"]
                need_history = include_candidates and screen in SUPPORTED_HISTORY_CONSUMERS
                base = self.original_build(
                    raw_snapshot, actions, projection=projection, history=(),
                    include_candidates=include_candidates,
                )
                if not need_history or not history:
                    self.stats["contexts_without_history"] += 1
                    if state is not None and not include_candidates:
                        state["post_context_cache"] = {
                            "raw_snapshot": raw_snapshot,
                            "projection": projection,
                            "value": base,
                        }
                    return base
                if not isinstance(history, ValidatedPublicHistory):
                    self.stats["untrusted_history_fallbacks"] += 1
                    return self.original_build(
                        raw_snapshot, actions, projection=projection, history=history,
                        include_candidates=include_candidates,
                    )
                self.stats["supported_full_history_materializations"] += 1
                self.stats["supported_materialized_entries"] += len(history)
                return self._materialize_history(base, history)

        @functools.wraps(self.original_append)
        def append_public_history_entry(
            history: Sequence[Mapping[str, Any]], entry: Mapping[str, Any]
        ) -> list[dict[str, Any]]:
            if not self.optimized:
                with phase("public_history_append_and_full_validation"):
                    result = self.original_append(history, entry)
                state = _ACTIVE_RUN.get()
                if state is not None and state["telemetry_enabled"]:
                    state["counts"]["previous_entries_recursively_revalidated"] = (
                        state["counts"].get("previous_entries_recursively_revalidated", 0) + len(history)
                    )
                    state["counts"]["history_entries_appended"] = state["counts"].get("history_entries_appended", 0) + 1
                return result
            previous_count = len(history)
            result = self._append_validated(history, entry)
            state = _ACTIVE_RUN.get()
            if state is not None and state["telemetry_enabled"]:
                state["counts"]["previous_entries_recursively_revalidated"] = state["counts"].get(
                    "previous_entries_recursively_revalidated", 0
                )
                state["counts"]["history_entries_appended"] = state["counts"].get("history_entries_appended", 0) + 1
                state["counts"]["history_entries_before_append"] = (
                    state["counts"].get("history_entries_before_append", 0) + previous_count
                )
            return result

        @functools.wraps(self.original_build_decision_context)
        def build_decision_context(*args: Any, **kwargs: Any) -> Any:
            with phase("decision_feature_context_build"):
                return self.original_build_decision_context(*args, **kwargs)

        @functools.wraps(self.original_projection_reader)
        def read_public_projection(adapter: Any, snapshot: Any) -> Any:
            state = _ACTIVE_RUN.get()
            if state is not None and state["optimized"] and state["projection_cache_enabled"]:
                cached = state.get("projection_cache")
                if cached is not None and cached["adapter"] is adapter and cached["snapshot"] is snapshot:
                    if state["telemetry_enabled"]:
                        state["counts"]["projection_cache_hits"] = state["counts"].get("projection_cache_hits", 0) + 1
                    return cached["value"]
            with phase("public_projection_read"):
                result = self.original_projection_reader(adapter, snapshot)
            if state is not None and state["optimized"] and state["projection_cache_enabled"]:
                state["projection_cache"] = {
                    "adapter": adapter,
                    "snapshot": snapshot,
                    "value": result,
                }
            return result

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
            parent = _ACTIVE_RUN.get()
            labels = dict(parent["labels"]) if parent is not None else {}
            for key in ("arm", "owner_seed", "call_index", "candidate_index", "branch_id"):
                value = getattr(controller, key, None)
                if value is not None:
                    labels[key] = value
            labels.update({
                "record_kind": "controlled_run",
                "seed": seed,
                "candidate_index": labels.get("candidate_index"),
                "run_type": "continuation" if hasattr(controller, "candidate_index") else "arm_actual",
            })
            if hasattr(controller, "candidate_index"):
                labels["arm"] = getattr(controller, "arm", labels.get("arm"))
            telemetry_enabled = parent["telemetry_enabled"] if parent is not None else True
            child = _new_state(
                labels=labels,
                metrics_path=parent["metrics_path"] if parent is not None else self.metrics_path,
                telemetry_enabled=telemetry_enabled,
                optimized=self.optimized,
                projection_cache_enabled=(self.optimized and before_decision_transform is None),
            )
            cpu_start = time.process_time()
            wall_start = time.monotonic()
            token = _ACTIVE_RUN.set(child)
            run = None
            try:
                measured_adapter = _MeteredAdapter(adapter) if telemetry_enabled else adapter
                measured_controller = _MeteredController(controller) if telemetry_enabled else controller

                def timed_observer(name: str, callback: Any) -> Any:
                    if not callable(callback):
                        return callback

                    @functools.wraps(callback)
                    def invoke(*args: Any, **kwargs: Any) -> Any:
                        if name == "after_transition" and telemetry_enabled:
                            step = args[0]
                            sequence_item = {
                                "step_index": step.step_index,
                                "screen_state": step.screen_state,
                                "legal_action_identities": step.legal_action_identities,
                                "chosen_action_identity": step.chosen_action_identity,
                            }
                            child["sequence_digest"].update((canonical(sequence_item) + "\n").encode("utf-8"))
                            child["sequence_steps"] += 1
                            if bool(step.battle_active):
                                child["current_battle_steps"] += 1
                                child["longest_battle_steps"] = max(
                                    child["longest_battle_steps"], child["current_battle_steps"]
                                )
                            else:
                                child["current_battle_steps"] = 0
                        with phase(name):
                            return callback(*args, **kwargs)

                    return invoke

                with phase("controlled_run_executor"):
                    run = self.original_execute(
                        measured_adapter,
                        measured_controller,
                        seed=seed,
                        max_steps=max_steps,
                        action_space=action_space,
                        before_decision_transform=before_decision_transform,
                        before_decision=timed_observer("before_observer", before_decision),
                        after_transition=timed_observer("after_transition", after_transition),
                        retain_steps=retain_steps,
                    )
                if self.optimized and run.public_history:
                    with phase("terminal_history_materialization"):
                        terminal_context = self.original_build(
                            run.final_raw,
                            (),
                            projection=child.get("last_projection"),
                            history=(),
                            include_candidates=False,
                        )
                        if isinstance(run.public_history, ValidatedPublicHistory):
                            terminal_context = self._materialize_history(terminal_context, run.public_history)
                        else:
                            terminal_context = self.original_build(
                                run.final_raw, (), projection=child.get("last_projection"),
                                history=run.public_history, include_candidates=False,
                            )
                        run = replace(run, public_run_context=terminal_context)
                        self.stats["terminal_full_history_materializations"] += 1
                        self.stats["terminal_materialized_entries"] += len(run.public_history)
                if telemetry_enabled:
                    status = "completed" if run.terminal and not run.problems else "incomplete"
                    if run.problems and labels.get("arm") == "B2" and all(
                        "b2 candidate rejected:" in str(problem).lower() for problem in run.problems
                    ):
                        status = "rejected"
                    elif run.problems:
                        status = "failed"
                    counts = getattr(controller, "counts", None)
                    if not isinstance(counts, Mapping):
                        counts = getattr(controller, "actual_counts", {})
                    child["counts"].update({f"decisions_by_role:{key}": int(value) for key, value in counts.items()})
                    child_cpu = max(0.0, time.process_time() - cpu_start)
                    child_wall = max(0.0, time.monotonic() - wall_start)
                    child["counts"]["step_count"] = len(run.public_history)
                    child["counts"]["terminal_replay_depth"] = len(run.public_history) if run.terminal else 0
                    self._append_metric({
                        "schema_id": METRICS_SCHEMA,
                        "record_kind": "controlled_run",
                        "optimizer_id": OPTIMIZER_ID if self.optimized else "baseline-unoptimized",
                        **labels,
                        "status": status,
                        "terminal": bool(run.terminal),
                        "outcome": run.outcome,
                        "problem_count": len(run.problems),
                        "problems": [str(problem) for problem in run.problems],
                        "run_cpu_seconds": child_cpu,
                        "run_wall_seconds": child_wall,
                        "process_cpu_seconds_at_finish": time.process_time(),
                        "step_count": len(run.public_history),
                        "longest_battle_steps": child["longest_battle_steps"],
                        "terminal_replay_depth": len(run.public_history) if run.terminal else 0,
                        "action_sequence_sha256": child["sequence_digest"].hexdigest(),
                        "action_sequence_steps": child["sequence_steps"],
                        "candidate_ordinal": labels.get("candidate_index"),
                        "b2_rejected_candidate_cpu_seconds": child_cpu if status == "rejected" else None,
                        "public_history_sha256": hashlib.sha256(canonical(run.public_history).encode("utf-8")).hexdigest(),
                        "final_public_context_sha256": hashlib.sha256(canonical(run.public_run_context).encode("utf-8")).hexdigest(),
                        "final_raw_sha256": hashlib.sha256(canonical(dict(run.final_raw)).encode("utf-8")).hexdigest(),
                        "phase_metrics": child["phases"],
                        "counts": child["counts"],
                        "unallocated_cpu_seconds": max(
                            0.0,
                            child_cpu - sum(float(item["exclusive_cpu_seconds"]) for item in child["phases"].values()),
                        ),
                        "unallocated_wall_seconds": max(
                            0.0,
                            child_wall - sum(float(item["exclusive_wall_seconds"]) for item in child["phases"].values()),
                        ),
                        "timing_note": "phase_metrics report inclusive and exclusive CPU/wall; exclusive values are non-overlapping within this executor call",
                    })
                return run
            finally:
                child_cpu = max(0.0, time.process_time() - cpu_start)
                child_wall = max(0.0, time.monotonic() - wall_start)
                _ACTIVE_RUN.reset(token)
                _append_child_time(parent, child_cpu, child_wall)

        self.controlled_run.build_public_run_context = build_public_run_context
        self.controlled_run.append_public_history_entry = append_public_history_entry
        self.controlled_run.build_decision_context = build_decision_context
        self.controlled_run._read_public_projection_for_context = read_public_projection
        self.controlled_run.execute_controlled_run = execute_controlled_run
        if hasattr(self.controlled_run, "action_identity_dicts_for_actions"):
            self._patch_module_function(
                self.controlled_run, "action_identity_dicts_for_actions", "legal_action_identity_build"
            )
        self.controlled_run._issue65_public_history_execution = self

    def _append_metric(self, record: Mapping[str, Any], *, metrics_path: Path | None = None) -> None:
        state = _ACTIVE_RUN.get()
        target = metrics_path or (state.get("metrics_path") if state is not None else self.metrics_path)
        if target is not None:
            _append_jsonl_durable(Path(target), record)

    def instrument_adapter_and_policy(
        self,
        *,
        adapter_class: type | None = None,
        expert_driver_class: type | None = None,
        runner_module: Any | None = None,
    ) -> None:
        if adapter_class is not None:
            for method, metric in (
                ("_snapshot", "adapter_snapshot"),
                ("step", "adapter_step_method"),
                ("legal_actions", "adapter_legal_action_materialization"),
                ("public_projection", "adapter_public_projection_method"),
                ("battle_search_v2", "native_battle_search_method"),
                ("capture_checkpoint", "checkpoint_capture_method"),
                ("restore_checkpoint", "checkpoint_restore_method"),
                ("checkpoint_fingerprint", "checkpoint_fingerprint_method"),
            ):
                self._patch_class_method(adapter_class, method, metric)
        if expert_driver_class is not None:
            for method, metric in (
                ("__init__", "expert_reconstruction"),
                ("reset_for_run", "expert_reset"),
                ("select_action", "expert_action_choice"),
            ):
                self._patch_class_method(expert_driver_class, method, metric)
        if runner_module is not None:
            runner_module.execute_controlled_run = self.controlled_run.execute_controlled_run
            if hasattr(self.controlled_run, "action_identity_dicts_for_actions"):
                runner_module.action_identity_dicts_for_actions = (
                    self.controlled_run.action_identity_dicts_for_actions
                )
            for attr, metric in (
                ("action_identity_dicts_for_actions", "legal_action_identity_build"),
                ("action_identity", "action_identity_projection"),
                ("same_action", "legal_action_identity_compare"),
            ):
                if hasattr(runner_module, attr):
                    self._patch_module_function(runner_module, attr, metric)

    @staticmethod
    def _patch_class_method(cls: type, name: str, metric: str) -> None:
        original = getattr(cls, name, None)
        if not callable(original) or getattr(original, "__issue65_timed__", False):
            return

        @functools.wraps(original)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            with phase(metric):
                return original(*args, **kwargs)

        wrapped.__issue65_timed__ = True
        setattr(cls, name, wrapped)

    @staticmethod
    def _patch_module_function(module: Any, name: str, metric: str) -> None:
        original = getattr(module, name)
        if not callable(original) or getattr(original, "__issue65_timed__", False):
            return

        @functools.wraps(original)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            with phase(metric):
                return original(*args, **kwargs)

        wrapped.__issue65_timed__ = True
        setattr(module, name, wrapped)

    def installation_record(self) -> dict[str, Any]:
        return {
            "optimizer_id": OPTIMIZER_ID if self.optimized else "baseline-unoptimized",
            "study_scope": "STUDY_ONLY",
            "pinned_stsr_commit": "7d172f7c24c9d29429b6946ba44f6b59760b0d96",
            "active_executor": "sts_combat_rl.sim.controlled_run.execute_controlled_run",
            "history_consumers": sorted(SUPPORTED_HISTORY_CONSUMERS),
            "history_consumer_audit": "ExpertNonCombatDriver reads current location, visible boss and persistent resources; it does not read history; MAP/REWARDS keep exact history conservatively",
            "new_entry_validation": "pinned sanitizer and schema/version/index check once before append",
            "existing_history_validation": "no recursive prior-entry revalidation per transition",
            "intermediate_history": "full detached history only on MAP/REWARDS decision contexts; empty intermediate/post contexts otherwise",
            "terminal_history": "full detached history materialized once after each controlled run",
            "projection_cache": "one consecutive post-transition to next-decision reuse keyed by exact adapter and snapshot object identity; disabled when a before-decision transform is installed",
            "semantics": "native action selection, candidate order, Search-v2@400, Expert seeds, checkpoint restores and terminal utility unchanged",
            "metrics_path": str(self.metrics_path) if self.metrics_path else None,
        }


def install(
    *,
    optimized: bool = True,
    metrics_path: Path | None = None,
    controlled_run_module: Any | None = None,
    public_context_module: Any | None = None,
    adapter_class: type | None = None,
    expert_driver_class: type | None = None,
    runner_module: Any | None = None,
) -> PublicHistoryExecution:
    """Install history handling and timers into the pinned study executor."""
    if controlled_run_module is None:
        from sts_combat_rl.sim import controlled_run as controlled_run_module
    if public_context_module is None:
        from sts_combat_rl.sim import public_run_context as public_context_module
    existing = getattr(controlled_run_module, "_issue65_public_history_execution", None)
    if isinstance(existing, PublicHistoryExecution):
        if existing.optimized != optimized:
            raise RuntimeError("Issue 65 runtime already installed with another history mode")
        if adapter_class is not None or expert_driver_class is not None or runner_module is not None:
            existing.instrument_adapter_and_policy(
                adapter_class=adapter_class,
                expert_driver_class=expert_driver_class,
                runner_module=runner_module,
            )
        return existing
    execution = PublicHistoryExecution(
        controlled_run_module,
        public_context_module,
        optimized=optimized,
        metrics_path=Path(metrics_path) if metrics_path is not None else None,
    )
    execution.instrument_adapter_and_policy(
        adapter_class=adapter_class,
        expert_driver_class=expert_driver_class,
        runner_module=runner_module,
    )
    return execution


__all__ = [
    "METRICS_SCHEMA",
    "OPTIMIZER_ID",
    "PublicHistoryExecution",
    "ValidatedPublicHistory",
    "count",
    "install",
    "phase",
]
