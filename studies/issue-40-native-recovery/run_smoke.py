#!/usr/bin/env python3
"""One-shot, bounded Issue 40 continuation smoke on the exact recovered T089 stack."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

STUDY = Path(__file__).resolve().parent
SOURCE_ROOT = Path("/home/lsmft/stsrl-spikes/STSRL-issue40-95abc")
NATIVE_ROOT = Path("/home/lsmft/stsrl-spikes/sts_lightspeed-t088-20a6")
BUILD_ROOT = NATIVE_ROOT / "build-py"
ARTIFACT_ROOT = Path("/mnt/d/DeadlycatCoding/STSRL/artifacts")
STATES_PATH = ARTIFACT_ROOT / "t075-leakage-safe-non-combat-cohort-repair/selected-states.jsonl"
TARGETS_PATH = ARTIFACT_ROOT / "t089-frozen-search-self-generated-non-combat-policy/target-table-formal-attempt-2.json"
CHECKPOINT_PATH = ARTIFACT_ROOT / "t089-frozen-search-self-generated-non-combat-policy/training-attempt-4/checkpoints/model-893002.pt"
EXPECTED_TARGET_SHA256 = "b8551454f764d47b43d068d507df783226f4b2651ea74fe82bec7fd4b0b8de11"
EXPECTED_CHECKPOINT_SHA256 = "a3a136f729b5cee69815a0b128411eec49a9b4f7695c6aae5a3796345fbd972e"
EXPECTED_NATIVE_COMMIT = "20a6c2b3a9cea817c988178b814f083ff889853f"
EXPECTED_JSON_COMMIT = "0b345b20c888f7dc8888485768e4bf9a6be29de0"
EXPECTED_PYBIND11_COMMIT = "d03662f0984f652b60e7ddce53d3868002275197"
EXPECTED_EXTENSION_SHA256 = "108416736aa83973e963cb06e02411d0eb389109003c62e414631053e35ef062"
EXPECTED_SOURCE_SELECTION_SHA256 = "94857d0e310f34cdd2780920ec81f9dc60e179c94244b9e231952a43a5f4e8b8"
SEEDS = (892201, 892202, 892203, 892204)
MAX_WALL_SECONDS = 1800
MAX_CPU_SECONDS = 1800
MAX_RSS_MIB = 8192
MIN_MEM_AVAILABLE_MIB = 4096
MAX_CONTINUATIONS = 64
MAX_STEPS = 500
PROGRESS_PATH = STUDY / "smoke-progress.jsonl"
RESULT_PATH = STUDY / "smoke-result.json"
STATE_PATH = STUDY / "smoke-state.json"

sys.path.insert(0, str(BUILD_ROOT))
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
from sts_combat_rl.sim.non_combat_learning import (
    _ContinuationAdapter,
    _add_search_cost,
    _expert_comparison_action_index,
    _raw_number,
    action_identity_dicts_for_actions,
    build_frozen_battle_controller,
    build_public_run_context,
    encode_lightspeed_battle_snapshot,
    encode_non_combat_decision_context,
    frozen_action_space,
    read_native_public_projection,
    read_source_states,
    execute_controlled_run,
    replay_source_state,
)
from sts_combat_rl.sim.non_combat_policy import ExpertNonCombatDriver
from sts_combat_rl.sim.online_controller import PolicyController, RoutedRunController
from sts_combat_rl.sim.t089_non_combat_policy import (
    T089_HELDOUT_EXPERT_COMPARATOR_SEED,
    T089LearnedNonCombatPolicy,
    load_t089_checkpoint,
    t089_battle_provenance,
)

PLAN_EXPECTATIONS: dict[int, tuple[str, int, str, int, str]] = {
    64: ("MAP_SCREEN", 1, "game:6", 0, "game:5"),
    65: ("MAP_SCREEN", 0, "game:1", 0, "game:1"),
    144: ("REST_ROOM", 4, "game:3221225473", 2, "game:2"),
    145: ("REST_ROOM", 0, "game:0", 0, "game:0"),
    224: ("REWARDS", 0, "game:134217728", 0, "game:134217728"),
    225: ("REWARDS", 0, "game:134217728", 2, "game:402653184"),
    304: ("TREASURE_ROOM", 0, "game:0", 0, "game:0"),
    305: ("TREASURE_ROOM", 0, "game:0", 0, "game:0"),
}


class BudgetSignal(Exception):
    pass


def on_term(_signum: int, _frame: Any) -> None:
    raise BudgetSignal("external wall/CPU budget stop requested")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def resource_sample(started: float) -> dict[str, Any]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    mem_available_kib = None
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            mem_available_kib = int(line.split()[1])
            break
    return {
        "wall_seconds": round(time.monotonic() - started, 3),
        "process_cpu_seconds": round(usage.ru_utime + usage.ru_stime, 3),
        "peak_rss_mib": round(usage.ru_maxrss / 1024, 2),
        "mem_available_mib": None if mem_available_kib is None else round(mem_available_kib / 1024, 2),
    }


def check_budget(started: float) -> str | None:
    sample = resource_sample(started)
    if sample["wall_seconds"] >= MAX_WALL_SECONDS:
        return "max_wall_clock_seconds"
    if sample["process_cpu_seconds"] >= MAX_CPU_SECONDS:
        return "max_process_cpu_seconds"
    if sample["peak_rss_mib"] >= MAX_RSS_MIB:
        return "max_peak_rss_mib"
    if sample["mem_available_mib"] is not None and sample["mem_available_mib"] < MIN_MEM_AVAILABLE_MIB:
        return "minimum_mem_available_mib"
    return None


def iter_top_level_array(path: Path, key: str) -> Iterable[dict[str, Any]]:
    marker = f'  "{key}": ['
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.startswith(marker):
                break
        else:
            raise ValueError(f"target table lacks top-level {key!r}")
        buffer = stream.read(64 * 1024)
        while True:
            buffer = buffer.lstrip()
            if buffer.startswith("]"):
                return
            if buffer.startswith(","):
                buffer = buffer[1:]
                continue
            if not buffer:
                buffer = stream.read(64 * 1024)
                if not buffer:
                    raise ValueError(f"target table {key!r} ended unexpectedly")
                continue
            try:
                row, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    raise ValueError(f"target table {key!r} contains invalid JSON")
                buffer += chunk
                continue
            if not isinstance(row, dict):
                raise ValueError(f"target table {key!r} contains a non-object row")
            yield row
            buffer = buffer[end:]


def append_state(terminal: str, started_at: str, started: float, details: Mapping[str, Any]) -> None:
    write_json(
        STATE_PATH,
        {
            "schema_id": "issue-40-smoke-state-v1",
            "schema_version": 1,
            "issue": 40,
            "terminal": terminal,
            "started_at_utc": started_at,
            "updated_at_utc": utc_now(),
            "resources": resource_sample(started),
            "stage1_started": False,
            **dict(details),
        },
    )


def main() -> int:
    global MAX_WALL_SECONDS, MAX_CPU_SECONDS, MAX_CONTINUATIONS
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    if hasattr(signal, "SIGXCPU"):
        signal.signal(signal.SIGXCPU, on_term)
    started = time.monotonic()
    started_at = utc_now()
    plan = json.loads((STUDY / "smoke-plan.json").read_text(encoding="utf-8-sig"))
    budget = json.loads((STUDY / "smoke-budget.json").read_text(encoding="utf-8-sig"))
    prior_wall = float(budget.get("spent_wall_seconds_before_current", 0.0))
    prior_cpu = float(budget.get("spent_process_cpu_seconds_before_current", 0.0))
    prior_attempts = int(budget.get("attempted_continuations_before_current", 0))
    MAX_WALL_SECONDS = max(0.0, float(budget["max_wall_clock_seconds"]) - prior_wall)
    MAX_CPU_SECONDS = max(0.0, float(budget["max_process_cpu_seconds"]) - prior_cpu)
    MAX_CONTINUATIONS = max(0, int(budget["max_complete_continuation_attempts"]) - prior_attempts)
    if MAX_WALL_SECONDS <= 0 or MAX_CPU_SECONDS <= 0 or MAX_CONTINUATIONS <= 0:
        raise BudgetSignal("no reserved smoke budget remains for continuation execution")
    resource.setrlimit(resource.RLIMIT_CPU, (max(1, int(MAX_CPU_SECONDS)), max(1, int(MAX_CPU_SECONDS))))
    audit = json.loads((STUDY / "source-audit.json").read_text(encoding="utf-8"))
    if audit.get("terminal") != "STAGE0_READY_FOR_SMOKE":
        raise ValueError(f"Stage-0 gate was not ready: {audit.get('terminal')!r}")
    if budget.get("status") not in {"RESERVED_BEFORE_SIMULATOR", "RETRY_AUTHORIZED_WITHIN_ISSUE_BUDGET"}:
        raise ValueError("finite smoke budget was not reserved before simulator execution")
    if plan.get("selection", {}).get("continuation_seeds") != list(SEEDS):
        raise ValueError("continuation seeds do not match the predeclared plan")
    if sha256_file(STATES_PATH) != EXPECTED_SOURCE_SELECTION_SHA256:
        raise ValueError("retained T075 source-state identity mismatch")
    if sha256_file(TARGETS_PATH) != EXPECTED_TARGET_SHA256:
        raise ValueError("retained T089 target-table identity mismatch")
    if sha256_file(CHECKPOINT_PATH) != EXPECTED_CHECKPOINT_SHA256:
        raise ValueError("retained T089 checkpoint identity mismatch")

    source_head = subprocess.check_output(["git", "-C", str(SOURCE_ROOT), "rev-parse", "HEAD"], text=True).strip()
    if source_head != "95abc8b1afe7d84e567aa70fb7225eba09fdff25":
        raise ValueError(f"STSRL source HEAD identity mismatch: {source_head}")
    native_head = subprocess.check_output(["git", "-C", str(NATIVE_ROOT), "rev-parse", "HEAD"], text=True).strip()
    if native_head != EXPECTED_NATIVE_COMMIT:
        raise ValueError(f"native HEAD identity mismatch: {native_head}")
    submodule_status = subprocess.check_output(["git", "-C", str(NATIVE_ROOT), "submodule", "status", "--", "json", "pybind11"], text=True)
    sub_lines = [line.strip() for line in submodule_status.splitlines() if line.strip()]
    if len(sub_lines) != 2 or any(line.startswith(("-", "+", "U")) for line in sub_lines):
        raise ValueError(f"exact native submodules are not initialized: {sub_lines!r}")
    if not any(line.split()[0] == EXPECTED_JSON_COMMIT and line.split()[1] == "json" for line in sub_lines):
        raise ValueError(f"json submodule identity mismatch: {sub_lines!r}")
    if not any(line.split()[0] == EXPECTED_PYBIND11_COMMIT and line.split()[1] == "pybind11" for line in sub_lines):
        raise ValueError(f"pybind11 submodule identity mismatch: {sub_lines!r}")

    import slaythespire  # type: ignore[import-not-found]

    extension_path = Path(slaythespire.__file__).resolve()
    if extension_path.parent != BUILD_ROOT.resolve() or sha256_file(extension_path) != EXPECTED_EXTENSION_SHA256:
        raise ValueError("loaded native extension path/binary hash does not match recovered exact build")
    if not hasattr(slaythespire, "StepSimulator"):
        raise ValueError("loaded native module has no Simulator API")

    states = read_source_states(STATES_PATH)
    if len(states) != 320 or tuple(s.selected_state_index for s in states) != tuple(range(320)):
        raise ValueError("retained 320-state source cohort identity/order mismatch")
    state_by_index = {s.selected_state_index: s for s in states}
    selected = plan["selection"]["heldout_states"]
    for spec in selected:
        state = state_by_index[int(spec["index"])]
        if state.split != "heldout" or state.family != spec["family"]:
            raise ValueError(f"predeclared held-out family/index mismatch for {spec['index']}")
    wanted_actions = {
        int(spec["index"]): {
            int(spec["model"]["index"]),
            int(spec["expert"]["index"]),
        }
        for spec in selected
    }
    targets: dict[tuple[int, int], dict[str, Any]] = {}
    for row in iter_top_level_array(TARGETS_PATH, "targets"):
        idx = row.get("selected_state_index")
        action_index = row.get("legal_action_index")
        if isinstance(idx, int) and idx in wanted_actions and isinstance(action_index, int) and action_index in wanted_actions[idx]:
            key = (idx, action_index)
            if key in targets:
                raise ValueError(f"duplicate frozen target row for {key}")
            if row.get("target_status") != "complete" or row.get("problems"):
                raise ValueError(f"frozen target row is incomplete for {key}")
            if row.get("continuation_seeds") != list(SEEDS):
                raise ValueError(f"frozen target row seeds differ for {key}")
            floors = row.get("terminal_floors")
            if not isinstance(floors, list) or len(floors) != len(SEEDS):
                raise ValueError(f"frozen terminal Q_E references incomplete for {key}")
            if row.get("legal_action_identity") != dict(state_by_index[idx].legal_action_identities[action_index]):
                raise ValueError(f"frozen target legal identity mismatch for {key}")
            if state_by_index[idx].source_floor is None:
                raise ValueError(f"target/source pair lacks source floor for {key}")
            expected_q = sum(max(0.0, float(value) - float(state_by_index[idx].source_floor)) for value in floors) / len(floors)
            if abs(float(row.get("q_floor", float("nan"))) - expected_q) > 1e-9:
                raise ValueError(f"historical target q_floor does not pair with terminal floors for {key}")
            targets[key] = row
    expected_pairs = {(idx, action) for idx, actions in wanted_actions.items() for action in actions}
    if set(targets) != expected_pairs or len(targets) != 11:
        raise ValueError(f"paired target rows do not match the fixed 11 root/action pairs: got {sorted(targets)}")

    model_run = load_t089_checkpoint(CHECKPOINT_PATH)
    if model_run.model_seed != 893002 or model_run.checkpoint_artifact_id != EXPECTED_CHECKPOINT_SHA256:
        raise ValueError("loaded model is not the exact T089 seed-893002 checkpoint")
    battle_provenance = t089_battle_provenance()
    if battle_provenance.get("search_budget", {}).get("simulations") != 400 or battle_provenance.get("root_selection") != "highest_mean":
        raise ValueError(f"frozen battle Search-v2 provenance mismatch: {battle_provenance!r}")

    runtime: dict[int, dict[str, Any]] = {}
    for spec in selected:
        if check_budget(started):
            raise BudgetSignal(f"resource budget exceeded before root replay: {check_budget(started)}")
        index = int(spec["index"])
        state = state_by_index[index]
        adapter = LightSpeedAdapter(seed=1, ascension=20, player_class="IRONCLAD")
        if not adapter.supports_checkpoint_restore:
            raise ValueError("exact native adapter checkpoint/restore is unavailable")
        snapshot, actions, context, checkpoint = replay_source_state(adapter, state)
        identities = tuple(dict(item) for item in action_identity_dicts_for_actions(actions))
        encoded = encode_non_combat_decision_context(context)
        if tuple(encoded.state_features) != tuple(state.state_features):
            raise ValueError(f"replayed public model-state features differ at source index {index}")
        if tuple(encoded.eligible_action_indices) != tuple(state.eligible_action_indices):
            raise ValueError(f"replayed eligible action indices differ at source index {index}")
        learned = T089LearnedNonCombatPolicy(model_run)
        learned_decision = learned.select_action(context)
        model_index = int(learned_decision.legal_action_index)
        expert_index = int(
            _expert_comparison_action_index(
                context,
                state.simulator_seed,
                seed=T089_HELDOUT_EXPERT_COMPARATOR_SEED,
            )
        )
        expected = PLAN_EXPECTATIONS[index]
        if state.family != expected[0] or model_index != expected[1] or expert_index != expected[3]:
            raise ValueError(
                f"PUBLIC_INPUT_INVALID at index {index}: "
                f"family/model/expert={(state.family, model_index, expert_index)!r}, "
                f"expected={(expected[0], expected[1], expected[3])!r}"
            )
        if identities[model_index].get("action_id") != expected[2] or identities[expert_index].get("action_id") != expected[4]:
            raise ValueError(f"PUBLIC_INPUT_INVALID: frozen root action identity changed at index {index}")
        runtime[index] = {
            "adapter": adapter,
            "state": state,
            "snapshot": snapshot,
            "actions": actions,
            "context": context,
            "checkpoint": checkpoint,
            "identities": identities,
            "model_index": model_index,
            "expert_index": expert_index,
            "family": state.family,
            "source_floor": state.source_floor,
            "public_context_status": state.public_context_status,
        }

    branch_specs: list[dict[str, Any]] = []
    for spec in selected:
        index = int(spec["index"])
        info = runtime[index]
        unique_order = [("model_root", info["model_index"]), ("expert_root", info["expert_index"])]
        seen: set[int] = set()
        for role, action_index in unique_order:
            if action_index in seen:
                branch_specs[-1]["root_roles"].append(role)
                continue
            seen.add(action_index)
            branch_specs.append(
                {
                    "state_index": index,
                    "family": info["family"],
                    "root_action_index": action_index,
                    "root_action_identity": dict(info["identities"][action_index]),
                    "root_roles": [role],
                    "adapter_index": index,
                }
            )
    if len(branch_specs) != 11 or len(branch_specs) * len(SEEDS) != 44:
        raise ValueError(f"paired smoke plan does not resolve to 44 continuations: {len(branch_specs)} unique action pairs")
    if len(branch_specs) * len(SEEDS) > MAX_CONTINUATIONS:
        raise ValueError("predeclared smoke exceeds the authorized continuation cap")

    runtime_evidence = {
        "native_head": native_head,
        "STSRL_source_head": source_head,
        "prior_execution_history": list(budget.get("execution_history", [])),
        "native_submodule_status": sub_lines,
        "loaded_extension_path": str(extension_path),
        "loaded_extension_sha256": sha256_file(extension_path),
        "loaded_extension_size_bytes": extension_path.stat().st_size,
        "source_selection_sha256": sha256_file(STATES_PATH),
        "target_table_sha256": sha256_file(TARGETS_PATH),
        "checkpoint_sha256": sha256_file(CHECKPOINT_PATH),
        "checkpoint_model_seed": model_run.model_seed,
        "battle_controller_provenance": dict(battle_provenance),
        "root_decisions": [
            {
                "state_index": index,
                "family": row["family"],
                "model_root_action_index": row["model_index"],
                "model_root_action_identity": dict(row["identities"][row["model_index"]]),
                "expert_root_action_index": row["expert_index"],
                "expert_root_action_identity": dict(row["identities"][row["expert_index"]]),
                "same_root_action": row["model_index"] == row["expert_index"],
                "public_context_status": row["public_context_status"],
            }
            for index, row in runtime.items()
        ],
        "unique_root_action_pairs": len(branch_specs),
        "planned_complete_continuations": len(branch_specs) * len(SEEDS),
    }
    append_state("SMOKE_RUNNING", started_at, started, {"runtime_evidence": runtime_evidence})
    if PROGRESS_PATH.exists():
        PROGRESS_PATH.unlink()

    completed = 0
    attempts: list[dict[str, Any]] = []
    terminal = "SMOKE_PASS_STAGE1_AWAITING_PLANNER"
    stop_detail: str | None = None
    for branch in branch_specs:
        index = int(branch["state_index"])
        info = runtime[index]
        state = info["state"]
        adapter = info["adapter"]
        checkpoint = info["checkpoint"]
        action_index = int(branch["root_action_index"])
        source_floor = float(state.source_floor) if state.source_floor is not None else None
        if source_floor is None:
            terminal, stop_detail = "PAIRED_TARGET_MISMATCH", f"missing source floor for {index}"
            break
        target = targets[(index, action_index)]
        for seed_offset, continuation_seed in enumerate(SEEDS):
            budget_reason = check_budget(started)
            if budget_reason:
                terminal, stop_detail = "COST_OR_REPLAY_BLOCKED", budget_reason
                break
            if completed >= MAX_CONTINUATIONS:
                terminal, stop_detail = "COST_OR_REPLAY_BLOCKED", "continuation cap reached"
                break
            attempt_started = utc_now()
            sample_before = resource_sample(started)
            attempt_key = f"{index}:{action_index}:{continuation_seed}"
            append_jsonl(
                PROGRESS_PATH,
                {
                    "event": "continuation_started",
                    "attempt": attempt_key,
                    "state_index": index,
                    "family": state.family,
                    "root_action_index": action_index,
                    "root_action_identity": dict(branch["root_action_identity"]),
                    "root_roles": list(branch["root_roles"]),
                    "continuation_seed": continuation_seed,
                    "simulator_seed": state.simulator_seed,
                    "sample_before": sample_before,
                    "started_at_utc": attempt_started,
                },
            )
            attempt_wall = time.monotonic()
            try:
                restored = adapter.restore_checkpoint(checkpoint)
                restored_actions = tuple(adapter.legal_actions(restored))
                restored_identities = tuple(
                    dict(item) for item in action_identity_dicts_for_actions(restored_actions)
                )
                if restored_identities != tuple(dict(item) for item in info["identities"]):
                    raise RuntimeError("replay legal-action identities changed on restore")
                restored_features = tuple(encode_lightspeed_battle_snapshot(restored.raw))
                if restored_features != tuple(info["context"].snapshot_features):
                    raise RuntimeError("replay public snapshot features changed on restore")
                history = info["context"].public_run_context.get("history", [])
                if not isinstance(history, (list, tuple)):
                    raise RuntimeError("replay public history is malformed")
                restored_context = build_public_run_context(
                    restored.raw,
                    restored_actions,
                    projection=read_native_public_projection(adapter, restored),
                    history=[item for item in history if isinstance(item, Mapping)],
                )
                if restored_context != info["context"].public_run_context:
                    raise RuntimeError("replay public context changed on restore")
                forced = adapter.step(restored_actions[action_index])
                cost: dict[str, float] = {"native_search_simulator_steps": 0.0, "native_search_wall_clock_seconds": 0.0, "native_search_decisions": 0.0}
                if forced.terminal:
                    final_raw = dict(forced.snapshot.raw)
                    terminal_steps = 1
                    policy_events: list[dict[str, Any]] = []
                else:
                    learned_policy = T089LearnedNonCombatPolicy(model_run)
                    learned_policy.fallback = ExpertNonCombatDriver(seed=continuation_seed)
                    controller = RoutedRunController(
                        battle=build_frozen_battle_controller(),
                        non_combat=PolicyController(learned_policy),
                    )
                    controlled = execute_controlled_run(
                        _ContinuationAdapter(adapter, forced.snapshot),
                        controller,
                        seed=state.simulator_seed,
                        max_steps=MAX_STEPS,
                        action_space=frozen_action_space(),
                    )
                    if controlled.problems:
                        raise RuntimeError("controlled continuation problems: " + "; ".join(controlled.problems))
                    if not controlled.terminal:
                        terminal, stop_detail = "COST_OR_REPLAY_BLOCKED", "500-step truncation before native terminal"
                        raise RuntimeError(stop_detail)
                    final_raw = dict(controlled.final_raw)
                    terminal_steps = len(controlled.steps) + 1
                    policy_events = [dict(event) for event in learned_policy.decision_events]
                    _add_search_cost(cost, controlled)
                floor = _raw_number(final_raw, "floor_num", "floor")
                act = _raw_number(final_raw, "act")
                outcome = final_raw.get("outcome")
                if floor is None or act is None or not isinstance(outcome, str) or not outcome:
                    raise RuntimeError("native terminal is missing floor/Act/outcome")
                historical_floor = float(target["terminal_floors"][seed_offset])
                historical_q = max(0.0, historical_floor - source_floor)
                smoke_q = max(0.0, floor - source_floor)
                entry = {
                    "event": "continuation_completed",
                    "attempt": attempt_key,
                    "state_index": index,
                    "family": state.family,
                    "root_action_index": action_index,
                    "root_action_identity": dict(branch["root_action_identity"]),
                    "root_roles": list(branch["root_roles"]),
                    "continuation_seed": continuation_seed,
                    "simulator_seed": state.simulator_seed,
                    "terminal": True,
                    "terminal_floor": floor,
                    "terminal_act": act,
                    "terminal_outcome": outcome,
                    "source_floor": source_floor,
                    "smoke_q_floor": smoke_q,
                    "historical_expert_terminal_floor": historical_floor,
                    "historical_expert_q_floor": historical_q,
                    "q_floor_delta_vs_historical_expert": smoke_q - historical_q,
                    "step_count_including_forced_root": terminal_steps,
                    "native_search_cost": cost,
                    "noncombat_policy_events": policy_events,
                    "attempt_wall_seconds": round(time.monotonic() - attempt_wall, 3),
                    "resource_sample": resource_sample(started),
                    "completed_at_utc": utc_now(),
                }
                append_jsonl(PROGRESS_PATH, entry)
                attempts.append(entry)
                completed += 1
            except BudgetSignal:
                raise
            except Exception as exc:
                append_jsonl(
                    PROGRESS_PATH,
                    {
                        "event": "continuation_failed",
                        "attempt": attempt_key,
                        "state_index": index,
                        "family": state.family,
                        "root_action_index": action_index,
                        "root_action_identity": dict(branch["root_action_identity"]),
                        "root_roles": list(branch["root_roles"]),
                        "continuation_seed": continuation_seed,
                        "simulator_seed": state.simulator_seed,
                        "failure": f"{type(exc).__name__}: {exc}",
                        "sample_after": resource_sample(started),
                        "failed_at_utc": utc_now(),
                    },
                )
                if terminal != "COST_OR_REPLAY_BLOCKED":
                    message = str(exc).lower()
                    terminal = "PAIRED_TARGET_MISMATCH" if "target" in message or "historical" in message else "PUBLIC_INPUT_INVALID" if "public" in message or "legal-action" in message else "COST_OR_REPLAY_BLOCKED" if "replay" in message or "restore" in message else "INCOMPLETE"
                stop_detail = f"{attempt_key}: {type(exc).__name__}: {exc}"
                break
            budget_reason = check_budget(started)
            if budget_reason and completed < len(branch_specs) * len(SEEDS):
                terminal, stop_detail = "COST_OR_REPLAY_BLOCKED", budget_reason
                break
        if terminal != "SMOKE_PASS_STAGE1_AWAITING_PLANNER":
            break

    final = {
        "schema_id": "issue-40-continuation-smoke-v1",
        "schema_version": 1,
        "issue": 40,
        "terminal": terminal,
        "started_at_utc": started_at,
        "finished_at_utc": utc_now(),
        "stage1_started": False,
        "authorized_stage1_followup": "Planner decision required after independent Reviewer PASS; no Stage-1 work performed.",
        "study_disposition": "STUDY_ONLY",
        "runtime_evidence": runtime_evidence,
        "complete_continuations": completed,
        "expected_continuations": 44,
        "prior_attempts_counted_against_budget": prior_attempts,
        "total_attempts_counted_against_budget": prior_attempts + completed,
        "maximum_authorized_continuations": int(budget["max_complete_continuation_attempts"]),
        "attempts": attempts,
        "stop_detail": stop_detail,
        "resources": resource_sample(started),
    }
    write_json(RESULT_PATH, final)
    append_state(
        terminal,
        started_at,
        started,
        {
            "complete_continuations": completed,
            "expected_continuations": 44,
            "stop_detail": stop_detail,
            "result_sha256": sha256_file(RESULT_PATH),
        },
    )
    append_jsonl(PROGRESS_PATH, {"event": "smoke_terminal", "terminal": terminal, "complete_continuations": completed, "finished_at_utc": utc_now(), "result_sha256": sha256_file(RESULT_PATH)})
    print(json.dumps({"terminal": terminal, "complete_continuations": completed, "stop_detail": stop_detail, "resources": resource_sample(started)}, sort_keys=True))
    return 0 if terminal == "SMOKE_PASS_STAGE1_AWAITING_PLANNER" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BudgetSignal as exc:
        terminal = "COST_OR_REPLAY_BLOCKED"
        stop_detail = str(exc)
    except Exception as exc:
        message = str(exc).lower()
        stop_detail = f"{type(exc).__name__}: {exc}"
        terminal = (
            "PAIRED_TARGET_MISMATCH"
            if "target" in message or "historical" in message
            else "PUBLIC_INPUT_INVALID"
            if "public" in message or "legal-action" in message
            else "COST_OR_REPLAY_BLOCKED"
            if "replay" in message or "restore" in message
            else "INCOMPLETE"
        )
    else:
        raise SystemExit(0)
    current = {}
    try:
        current = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    except Exception:
        pass
    write_json(
        STATE_PATH,
        {
            "schema_id": "issue-40-smoke-state-v1",
            "schema_version": 1,
            "issue": 40,
            "terminal": terminal,
            "updated_at_utc": utc_now(),
            "stage1_started": False,
            "stop_detail": stop_detail,
            "prior_state": current,
        },
    )
    if not RESULT_PATH.exists():
        write_json(
            RESULT_PATH,
            {
                "schema_id": "issue-40-continuation-smoke-v1",
                "schema_version": 1,
                "issue": 40,
                "terminal": terminal,
                "started_at_utc": None,
                "finished_at_utc": utc_now(),
                "stage1_started": False,
                "study_disposition": "STUDY_ONLY",
                "complete_continuations": 0,
                "expected_continuations": 44,
                "maximum_authorized_continuations": 64,
                "attempts": [],
                "stop_detail": stop_detail,
            },
        )
    print(json.dumps({"terminal": terminal, "stop_detail": stop_detail}))
    raise SystemExit(2)
