#!/usr/bin/env python3
"""Run the exact matched Stage 1 after the artifact-only plan gate."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import platform
import resource
import signal
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
I40 = REPO / "studies/issue-40-native-recovery"
sys.path.insert(0, str(I40))
import run_smoke as h  # exact Issue 40 source/replay/controller helpers

spec = importlib.util.spec_from_file_location("issue41_plan_builder", HERE / "build_stage1_plan.py")
assert spec and spec.loader
plan_builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan_builder)

PLAN_PATH = HERE / "stage1-plan.json"
PREFLIGHT_PATH = HERE / "stage1-preflight.json"
PROGRESS_PATH = HERE / "stage1-progress.jsonl"
STATE_PATH = HERE / "stage1-state.json"
RESULT_PATH = HERE / "stage1-result.json"
BUDGET_PATH = HERE / "stage1-budget.json"
SEEDS = (892201, 892202, 892203, 892204)
FAMILIES = ("MAP_SCREEN", "REST_ROOM", "REWARDS", "TREASURE_ROOM")
MAX_WALL = 12000
MAX_CPU = 12000
MAX_ATTEMPTS = 512
MAX_STEPS = 500
MAX_RSS_MIB = 8192
MIN_MEM_MIB = 4096
STARTED = time.monotonic()


class GateError(RuntimeError):
    def __init__(self, terminal: str, message: str):
        super().__init__(message)
        self.terminal = terminal


class BudgetSignal(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write(path: Path, value: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def append(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def sample() -> dict[str, Any]:
    u = resource.getrusage(resource.RUSAGE_SELF)
    mem = None
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            mem = int(line.split()[1]) / 1024
            break
    return {"wall_seconds": round(time.monotonic() - STARTED, 3), "process_cpu_seconds": round(u.ru_utime + u.ru_stime, 3), "peak_rss_mib": round(u.ru_maxrss / 1024, 2), "mem_available_mib": None if mem is None else round(mem, 2)}


def budget_reason() -> str | None:
    budget = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    prior_wall = sum(float(x.get("wall_seconds", 0)) for x in budget.get("execution_history", []))
    prior_cpu = sum(float(x.get("process_cpu_seconds_observed", 0)) for x in budget.get("execution_history", []))
    s = sample()
    if prior_wall + s["wall_seconds"] >= MAX_WALL:
        return "max_wall_clock_seconds"
    if prior_cpu + s["process_cpu_seconds"] >= MAX_CPU:
        return "max_process_cpu_seconds"
    if s["peak_rss_mib"] >= MAX_RSS_MIB:
        return "max_peak_rss_mib"
    if s["mem_available_mib"] is not None and s["mem_available_mib"] < MIN_MEM_MIB:
        return "minimum_mem_available_mib"
    return None


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, text=True, capture_output=True).stdout.strip()


def verify_runtime(plan: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
    audit = json.loads((I40 / "source-audit.json").read_text(encoding="utf-8"))
    if audit.get("terminal") != "STAGE0_READY_FOR_SMOKE":
        raise GateError("SOURCE_OR_TARGET_UNAVAILABLE", "Issue 40 Stage-0 audit is not ready")
    checked = plan_builder.verify_roles(audit)
    if checked["retained_role_count"] != 14 or checked["retention_manifests"] != plan["input_identity"]["retention_manifests"]:
        raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", "retained role/manifest identity changed after plan")
    proc = subprocess.run([sys.executable, str(I40 / "verify_issue40.py")], cwd=str(REPO), text=True, capture_output=True)
    if proc.returncode or "PASS Issue 40 verification: 66 checks" not in proc.stdout:
        raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", f"Issue 40 verification failed: {proc.stderr[-1000:]}")
    if git(h.SOURCE_ROOT, "rev-parse", "HEAD") != plan["model"]["code_commit"] or git(h.SOURCE_ROOT, "cat-file", "-t", plan["baseline"]["approved_spec_commit"]) != "commit":
        raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", "accepted STSRL code/spec identity mismatch")
    if git(h.NATIVE_ROOT, "rev-parse", "HEAD") != plan["native"]["commit"]:
        raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", "exact native source commit mismatch")
    sub = git(h.NATIVE_ROOT, "submodule", "status", "--", "json", "pybind11").splitlines()
    subheads = {line.lstrip(" +- ").split()[1]: line.lstrip(" +- ").split()[0] for line in sub if line.strip()}
    if subheads != {"json": plan["native"]["json_commit"], "pybind11": plan["native"]["pybind11_commit"]} or any(line.startswith(("-", "+", "U")) for line in sub):
        raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", f"native submodule identities mismatch: {sub}")
    import slaythespire
    ext = Path(slaythespire.__file__).resolve()
    if ext.parent != h.BUILD_ROOT.resolve() or sha(ext) != plan["native"]["extension_sha256"]:
        raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", "loaded native extension path/hash differs from exact Issue 40 build")
    if sha(h.STATES_PATH) != plan["source"]["artifact_sha256"] or sha(h.TARGETS_PATH) != plan["baseline"]["target_table_sha256"] or sha(h.CHECKPOINT_PATH) != plan["baseline"]["checkpoint_sha256"]:
        raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", "frozen source/target/checkpoint changed after plan")
    model = h.load_t089_checkpoint(h.CHECKPOINT_PATH)
    if model.model_seed != 893002 or model.checkpoint_artifact_id != plan["model"]["checkpoint_sha256"]:
        raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", "loaded model differs from selected frozen T089 checkpoint")
    battle = h.t089_battle_provenance()
    if battle.get("search_budget", {}).get("simulations") != 400 or battle.get("root_selection") != "highest_mean" or battle.get("version") != "search-v2":
        raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", "frozen Battle controller provenance changed")
    host = {"os": platform.platform(), "python": platform.python_version(), "torch": __import__("torch").__version__, "machine": platform.machine(), "native_commit": git(h.NATIVE_ROOT, "rev-parse", "HEAD"), "json_submodule": subheads["json"], "pybind11_submodule": subheads["pybind11"], "extension_path": str(ext), "extension_sha256": sha(ext), "STSRL_commit": git(h.SOURCE_ROOT, "rev-parse", "HEAD"), "checkpoint_sha256": sha(h.CHECKPOINT_PATH), "battle_controller": battle}
    return model, host


def read_progress() -> list[dict[str, Any]]:
    if not PROGRESS_PATH.is_file():
        return []
    return [json.loads(line) for line in PROGRESS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_target_rows(plan: Mapping[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
    wanted = {int(s["index"]): {int(s["model"]["index"]), int(s["expert"]["index"])} for s in plan["selection"]["heldout_states"]}
    rows = {}
    for row in h.iter_top_level_array(h.TARGETS_PATH, "targets"):
        idx, action = row.get("selected_state_index"), row.get("legal_action_index")
        if idx in wanted and action in wanted[idx]:
            key = (int(idx), int(action))
            if key in rows:
                raise GateError("PAIRED_TARGET_MISMATCH", f"duplicate selected frozen target row {key}")
            rows[key] = row
    expected = sum(len(actions) for actions in wanted.values())
    if len(rows) != expected:
        raise GateError("PAIRED_TARGET_MISMATCH", f"selected frozen target rows incomplete: {len(rows)}/{expected}")
    return rows


def bootstrap(states: list[dict[str, Any]]) -> dict[str, Any]:
    import numpy as np
    rng = np.random.default_rng(391089)
    families = {f: [s for s in states if s["family"] == f] for f in FAMILIES}
    if any(len(v) != 16 for v in families.values()):
        raise GateError("SOURCE_OR_TARGET_UNAVAILABLE", "bootstrap family counts are not 16 each")
    b_dm = np.empty(10000, dtype=np.float64)
    b_delta = np.empty(10000, dtype=np.float64)
    for b in range(10000):
        selected = []
        for family in FAMILIES:
            rows = families[family]
            selected.extend(rows[int(i)] for i in rng.integers(0, 16, size=16))
        b_dm[b] = statistics.mean(x["d_m"] for x in selected)
        b_delta[b] = statistics.mean(x["delta"] for x in selected)
    return {"mean_d_m_ci95": [float(x) for x in np.quantile(b_dm, [0.025, 0.975], method="linear")], "mean_delta_ci95": [float(x) for x in np.quantile(b_delta, [0.025, 0.975], method="linear")], "method": "family-stratified paired source-state bootstrap", "replicates": 10000, "seed": 391089, "quantile_method": "linear", "rng": f"numpy {np.__version__} default_rng"}


def analyze(plan: Mapping[str, Any], rows: list[dict[str, Any]], budget: Mapping[str, Any]) -> dict[str, Any]:
    import numpy as np
    target_rows = load_target_rows(plan)
    by_key = {(int(r["state_index"]), int(r["root_action_index"]), int(r["continuation_seed"])): r for r in rows}
    states = []
    for info in plan["selection"]["heldout_states"]:
        idx = int(info["index"])
        mi, ei = int(info["model"]["index"]), int(info["expert"]["index"])
        paired = []
        for seed in SEEDS:
            mr, er = by_key[(idx, mi, seed)], by_key[(idx, ei, seed)]
            offset = SEEDS.index(seed)
            tm, te = target_rows[(idx, mi)], target_rows[(idx, ei)]
            ge_m = max(0.0, float(tm["terminal_floors"][offset]) - float(info["source_floor"]))
            ge_e = max(0.0, float(te["terminal_floors"][offset]) - float(info["source_floor"]))
            dm = float(mr["g_m_floor"]) - float(er["g_m_floor"])
            paired.append({"seed": seed, "g_e_model_root": ge_m, "g_e_expert_root": ge_e, "g_m_model_root": float(mr["g_m_floor"]), "g_m_expert_root": float(er["g_m_floor"]), "d_e_seed": ge_m - ge_e, "d_m_seed": dm, "delta_seed": dm - (ge_m - ge_e)})
        de, dm = statistics.mean(x["d_e_seed"] for x in paired), statistics.mean(x["d_m_seed"] for x in paired)
        states.append({"state_index": idx, "family": info["family"], "source_run_id": info["source_run_id"], "coincident_root_action": info["coincident_root_action"], "per_seed": paired, "d_e": de, "d_m": dm, "delta": dm - de})
    cis = bootstrap(states)
    mean_dm = statistics.mean(s["d_m"] for s in states)
    mean_delta = statistics.mean(s["delta"] for s in states)
    if cis["mean_delta_ci95"][1] < 0 and mean_dm <= 0:
        terminal = "CONTINUATION_POLICY_MISMATCH_SUPPORTED"
    elif cis["mean_d_m_ci95"][0] > 0:
        terminal = "CONTINUATION_POLICY_SIGNAL_SURVIVES"
    else:
        terminal = "MECHANISM_INCONCLUSIVE"
    noncoincident = [s for s in states if not s["coincident_root_action"]]
    strict_switches = [s["state_index"] for s in noncoincident if (s["d_e"] > 0 > s["d_m"]) or (s["d_e"] < 0 < s["d_m"])]
    zero_crossings = [s["state_index"] for s in noncoincident if (s["d_e"] == 0) != (s["d_m"] == 0)]
    family = {f: {"state_count": 16, "mean_d_e": statistics.mean(s["d_e"] for s in states if s["family"] == f), "mean_d_m": statistics.mean(s["d_m"] for s in states if s["family"] == f), "mean_delta": statistics.mean(s["delta"] for s in states if s["family"] == f)} for f in FAMILIES}
    outcomes = Counter(str(r["terminal_outcome"]) for r in rows)
    policy_events = Counter(e["status"] for r in rows for e in r.get("noncombat_policy_events", []))
    step_counts = [int(r["step_count_including_forced_root"]) for r in rows]
    histories = budget.get("execution_history", [])
    resources = {"stage1_wall_seconds": round(sum(float(x.get("wall_seconds", 0)) for x in histories), 3), "stage1_process_cpu_seconds": round(sum(float(x.get("process_cpu_seconds_observed", 0)) for x in histories), 3), "peak_process_tree_rss_mib": round(max((int(x.get("peak_process_tree_rss_bytes_observed", 0)) for x in histories), default=0) / (1024 * 1024), 2), "peak_single_process_rss_mib": round(max((int(x.get("peak_single_process_rss_bytes_observed", 0)) for x in histories), default=0) / (1024 * 1024), 2), "minimum_available_memory_mib": round(min((int(x["minimum_mem_available_bytes_observed"]) for x in histories if x.get("minimum_mem_available_bytes_observed") is not None), default=0) / (1024 * 1024), 2), "stage1_attempts_started": int(budget.get("continuation_attempts_started", 0)), "total_attempts_including_issue40": 45 + int(budget.get("continuation_attempts_started", 0)), "max_total_attempts": MAX_ATTEMPTS, "max_steps": MAX_STEPS}
    return {"schema_id": "issue-41-t089-matched-continuation-result-v1", "schema_version": 1, "issue": 41, "terminal": terminal, "study_disposition": "STUDY_ONLY", "stage1_started": True, "started_at_utc": budget.get("stage1_started_at_utc"), "finished_at_utc": utc_now(), "primary_analysis": {"mean_d_e": statistics.mean(s["d_e"] for s in states), "mean_d_m": mean_dm, "mean_delta": mean_delta, **cis, "states": states, "family_breakdown": family}, "secondary_sensitivity": {"distinct_source_run_count": 43, "source_run_clustered_ci_computed": False, "note": "The primary prespecified CI is over 64 source-state units; 43 distinct source runs limit independence."}, "sign_switches": {"noncoincident_state_count": len(noncoincident), "strict_sign_switch_count": len(strict_switches), "strict_sign_switch_state_indices": strict_switches, "zero_crossing_count": len(zero_crossings), "zero_crossing_state_indices": zero_crossings, "coincident_state_count": 64 - len(noncoincident), "coincident_fraction": (64 - len(noncoincident)) / 64}, "terminal_outcomes": dict(sorted(outcomes.items())), "later_noncombat_policy_events": dict(sorted(policy_events.items())), "continuation_rows_by_family": dict(sorted(Counter(r["family"] for r in rows).items())), "step_count": {"minimum": min(step_counts), "maximum": max(step_counts), "mean": statistics.mean(step_counts)}, "runtime": resources, "row_count": len(rows), "reused_issue40_rows": 44, "new_issue41_rows": len(rows) - 44, "rows": rows}


def run_stage1() -> int:
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    if plan.get("schema_id") != "issue-41-t089-matched-continuation-plan-v1" or not plan.get("resource_projection", {}).get("projection_gate_passed"):
        raise GateError("COST_OR_REPLAY_BLOCKED", "fixed plan or resource projection gate is missing")
    before_plan_sha = sha(PLAN_PATH)
    if plan["execution"]["historical_attempts"] != 45 or plan["execution"]["total_attempts_if_complete_no_retry"] > MAX_ATTEMPTS or plan["execution"]["new_unique_continuations"] != 336:
        raise GateError("COST_OR_REPLAY_BLOCKED", "fixed plan attempt ceiling changed")
    if plan["resource_projection"]["upper_empirical_projection"]["wall_seconds"] > MAX_WALL or plan["resource_projection"]["upper_empirical_projection"]["process_cpu_seconds"] > MAX_CPU:
        raise GateError("COST_OR_REPLAY_BLOCKED", "empirical resource projection exceeds cap")
    model, runtime = verify_runtime(plan)
    if sha(PLAN_PATH) != before_plan_sha:
        raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", "fixed plan changed during runtime verification")
    states = h.read_source_states(h.STATES_PATH)
    if len(states) != 320:
        raise GateError("SOURCE_OR_TARGET_UNAVAILABLE", "T075 reader did not load exactly 320 states")
    state_by_index = {s.selected_state_index: s for s in states}
    gate = json.loads(plan_builder.GATE.read_text(encoding="utf-8"))
    gate_by_index = {int(r["selected_state_index"]): r for r in gate["model_results"]["893002"]}
    runtime_by_index = {}
    adapter = h.LightSpeedAdapter(seed=1, ascension=20, player_class="IRONCLAD")
    if not adapter.supports_checkpoint_restore:
        raise GateError("COST_OR_REPLAY_BLOCKED", "native checkpoint restore unavailable")
    for info in plan["selection"]["heldout_states"]:
        if budget_reason():
            raise GateError("COST_OR_REPLAY_BLOCKED", budget_reason() or "resource budget reached during root preflight")
        idx = int(info["index"])
        state = state_by_index[idx]
        if state.split != "heldout" or state.family != info["family"] or state.source_run_id != info["source_run_id"] or state.source_floor != info["source_floor"] or state.public_state_identity != info["public_state_identity"]:
            raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", f"source row identity changed at {idx}")
        try:
            snapshot, actions, context, checkpoint = h.replay_source_state(adapter, state)
        except Exception as exc:
            raise GateError("COST_OR_REPLAY_BLOCKED", f"source replay failed at {idx}: {type(exc).__name__}: {exc}") from exc
        identities = tuple(dict(x) for x in h.action_identity_dicts_for_actions(actions))
        if identities != tuple(dict(x) for x in state.legal_action_identities):
            raise GateError("PUBLIC_INPUT_INVALID", f"ordered legal identities changed at state {idx}")
        encoded = h.encode_non_combat_decision_context(context)
        if tuple(encoded.state_features) != tuple(state.state_features) or tuple(encoded.eligible_action_indices) != tuple(state.eligible_action_indices):
            raise GateError("PUBLIC_INPUT_INVALID", f"normal-public model input/legal mask changed at state {idx}")
        learned = h.T089LearnedNonCombatPolicy(model)
        model_index = int(learned.select_action(context).legal_action_index)
        expert_index = int(h._expert_comparison_action_index(context, state.simulator_seed, seed=895001))
        expected = gate_by_index[idx]
        if model_index != int(expected["model_action_index"]) or expert_index != int(expected["expert_action_index"]) or model_index != int(info["model"]["index"]) or expert_index != int(info["expert"]["index"]):
            raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", f"replayed frozen root choices differ from original gate at {idx}")
        runtime_by_index[idx] = {"state": state, "snapshot": snapshot, "actions": actions, "context": context, "checkpoint": checkpoint, "identities": identities}

    if len(runtime_by_index) != 64 or Counter(x["state"].family for x in runtime_by_index.values()) != Counter({f: 16 for f in FAMILIES}):
        raise GateError("SOURCE_OR_TARGET_UNAVAILABLE", "runtime root preflight did not cover the exact balanced 64 states")
    targets = load_target_rows(plan)
    old = json.loads((I40 / "smoke-result.json").read_text(encoding="utf-8"))
    if len(old.get("attempts", [])) != 44:
        raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", "Issue 40 must contribute exactly 44 completed rows")
    branch_by_key = {(int(b["state_index"]), int(b["root_action_index"])): b for b in plan["execution"]["physical_branches"]}
    progress = read_progress()
    old_normalized = {}
    for row in old["attempts"]:
        key = (int(row["state_index"]), int(row["root_action_index"]), int(row["continuation_seed"]))
        branch = branch_by_key.get(key[:2])
        if key in old_normalized or key[2] not in SEEDS or branch is None:
            raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", f"Issue 40 row is duplicate or outside the frozen Stage-1 keys: {key}")
        if row.get("terminal") is not True or int(row.get("step_count_including_forced_root", MAX_STEPS + 1)) > MAX_STEPS:
            raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", f"Issue 40 row is not a valid terminal continuation: {key}")
        if row.get("root_action_identity") != branch["root_action_identity"] or row.get("root_roles") != branch["root_roles"]:
            raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", f"Issue 40 root/action roles differ from the fixed plan: {key}")
        state_info = next(x for x in plan["selection"]["heldout_states"] if int(x["index"]) == key[0])
        target = targets[key[:2]]
        offset = SEEDS.index(key[2])
        old_normalized[key] = {"state_index": key[0], "family": row["family"], "source_run_id": state_info["source_run_id"], "root_action_index": key[1], "root_action_identity": row["root_action_identity"], "root_roles": row["root_roles"], "continuation_seed": key[2], "simulator_seed": row["simulator_seed"], "terminal": True, "terminal_floor": float(row["terminal_floor"]), "terminal_act": float(row["terminal_act"]), "source_floor": float(state_info["source_floor"]), "g_m_floor": max(0.0, float(row["terminal_floor"]) - float(state_info["source_floor"])), "g_e_floor": max(0.0, float(target["terminal_floors"][offset]) - float(state_info["source_floor"])), "terminal_outcome": row["terminal_outcome"], "step_count_including_forced_root": int(row["step_count_including_forced_root"]), "native_search_cost": row["native_search_cost"], "noncombat_policy_events": row["noncombat_policy_events"], "attempt_wall_seconds": row["attempt_wall_seconds"], "evidence_origin": "issue40_reused", "issue40_attempt": row["attempt"]}
    complete = dict(old_normalized)
    for row in progress:
        if row.get("event") != "continuation_completed":
            continue
        key = (int(row["state_index"]), int(row["root_action_index"]), int(row["continuation_seed"]))
        if key in complete:
            if complete[key].get("evidence_origin") == "issue40_reused":
                raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", f"progress repeats a reused Issue 40 key {key}")
            raise GateError("HISTORICAL_IDENTITY_UNAVAILABLE", f"duplicate complete Stage-1 key {key}")
        info = next(x for x in plan["selection"]["heldout_states"] if int(x["index"]) == key[0])
        target = targets[key[:2]]
        off = SEEDS.index(key[2])
        complete[key] = {"state_index": key[0], "family": row["family"], "source_run_id": info["source_run_id"], "root_action_index": key[1], "root_action_identity": row["root_action_identity"], "root_roles": row["root_roles"], "continuation_seed": key[2], "simulator_seed": row["simulator_seed"], "terminal": True, "terminal_floor": float(row["terminal_floor"]), "terminal_act": float(row["terminal_act"]), "source_floor": float(info["source_floor"]), "g_m_floor": max(0.0, float(row["terminal_floor"]) - float(info["source_floor"])), "g_e_floor": max(0.0, float(target["terminal_floors"][off]) - float(info["source_floor"])), "terminal_outcome": row["terminal_outcome"], "step_count_including_forced_root": int(row["step_count_including_forced_root"]), "native_search_cost": row["native_search_cost"], "noncombat_policy_events": row["noncombat_policy_events"], "attempt_wall_seconds": row["attempt_wall_seconds"], "evidence_origin": "issue41_executed"}
    all_keys = {(int(b["state_index"]), int(b["root_action_index"]), int(seed)) for b in plan["execution"]["physical_branches"] for seed in SEEDS}
    starts_so_far = sum(1 for e in progress if e.get("event") == "continuation_started")
    if 45 + starts_so_far > MAX_ATTEMPTS:
        raise GateError("COST_OR_REPLAY_BLOCKED", "combined Issue 40/41 continuation starts exceed 512")
    # All preflight checks, including 64 runtime replays and exact policy decisions,
    # have passed. Mark Stage 1 only now, immediately before first planned branch.
    state_doc = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    started_at = state_doc.get("stage1_started_at_utc") or utc_now()
    write(PREFLIGHT_PATH, {"schema_id": "issue-41-stage1-preflight-v1", "schema_version": 1, "issue": 41, "terminal": "PREFLIGHT_PASS_STAGE1_READY", "stage1_started": False, "plan_sha256": before_plan_sha, "runtime_identity": runtime, "root_decisions_verified": len(runtime_by_index), "exact_issue40_rows_reused": 44, "new_unique_keys": len(all_keys) - 44, "source_states": 64, "source_runs": 43, "target_rows": 221, "retained_roles": 14, "resources": sample()})
    if os.environ.get("SPIRE_STAGE1_PREFLIGHT_ONLY") == "1":
        print(json.dumps({"terminal": "PREFLIGHT_PASS_STAGE1_READY", "stage1_started": False, "root_decisions_verified": len(runtime_by_index), "resources": sample()}, sort_keys=True))
        return 0
    write(STATE_PATH, {"schema_id": "issue-41-stage1-state-v1", "schema_version": 1, "issue": 41, "terminal": "STAGE1_RUNNING", "stage1_started": True, "stage1_started_at_utc": started_at, "updated_at_utc": utc_now(), "complete_unique_keys": len(complete), "expected_unique_keys": len(all_keys), "attempts_used_including_issue40": 45 + starts_so_far})
    terminal, stop_detail = "STAGE1_COMPLETE_REVIEWER_PENDING", None
    branches = plan["execution"]["physical_branches"]
    for branch in branches:
        idx, action_index = int(branch["state_index"]), int(branch["root_action_index"])
        rt = runtime_by_index[idx]
        state, target = rt["state"], targets[(idx, action_index)]
        for seed_offset, seed in enumerate(SEEDS):
            key = (idx, action_index, seed)
            if key in complete:
                continue
            attempts_used = 45 + sum(1 for e in read_progress() if e.get("event") == "continuation_started")
            if attempts_used >= MAX_ATTEMPTS:
                terminal, stop_detail = "COST_OR_REPLAY_BLOCKED", "512 total continuation starts reached"
                break
            reason = budget_reason()
            if reason:
                terminal, stop_detail = "COST_OR_REPLAY_BLOCKED", reason
                break
            attempt_number = attempts_used - 44
            attempt_key = f"{idx}:{action_index}:{seed}"
            append(PROGRESS_PATH, {"event": "continuation_started", "attempt": attempt_key, "attempt_number": attempt_number, "state_index": idx, "family": state.family, "root_action_index": action_index, "root_action_identity": dict(branch["root_action_identity"]), "root_roles": list(branch["root_roles"]), "continuation_seed": seed, "simulator_seed": state.simulator_seed, "started_at_utc": utc_now(), "sample_before": sample()})
            attempt_started = time.monotonic()
            try:
                restored = adapter.restore_checkpoint(rt["checkpoint"])
                restored_actions = tuple(adapter.legal_actions(restored))
                restored_ids = tuple(dict(x) for x in h.action_identity_dicts_for_actions(restored_actions))
                if restored_ids != tuple(dict(x) for x in rt["identities"]):
                    raise GateError("PUBLIC_INPUT_INVALID", "legal action identities changed after restore")
                if tuple(h.encode_lightspeed_battle_snapshot(restored.raw)) != tuple(rt["context"].snapshot_features):
                    raise GateError("PUBLIC_INPUT_INVALID", "public snapshot features changed after restore")
                history = rt["context"].public_run_context.get("history", [])
                if not isinstance(history, (list, tuple)):
                    raise GateError("PUBLIC_INPUT_INVALID", "public history is malformed")
                restored_context = h.build_public_run_context(restored.raw, restored_actions, projection=h.read_native_public_projection(adapter, restored), history=[x for x in history if isinstance(x, Mapping)])
                if restored_context != rt["context"].public_run_context:
                    raise GateError("PUBLIC_INPUT_INVALID", "public context changed after restore")
                forced = adapter.step(restored_actions[action_index])
                cost = {"native_search_simulator_steps": 0.0, "native_search_wall_clock_seconds": 0.0, "native_search_decisions": 0.0}
                if forced.terminal:
                    raw, steps, events = dict(forced.snapshot.raw), 1, []
                else:
                    policy = h.T089LearnedNonCombatPolicy(model)
                    policy.fallback = h.ExpertNonCombatDriver(seed=seed)
                    controller = h.RoutedRunController(battle=h.build_frozen_battle_controller(), non_combat=h.PolicyController(policy))
                    controlled = h.execute_controlled_run(h._ContinuationAdapter(adapter, forced.snapshot), controller, seed=state.simulator_seed, max_steps=MAX_STEPS, action_space=h.frozen_action_space())
                    if controlled.problems:
                        raise GateError("INCOMPLETE", "controlled continuation problems: " + "; ".join(controlled.problems))
                    if not controlled.terminal:
                        raise GateError("COST_OR_REPLAY_BLOCKED", "500-step truncation before native terminal")
                    raw, steps = dict(controlled.final_raw), len(controlled.steps) + 1
                    events = [dict(x) for x in policy.decision_events]
                    h._add_search_cost(cost, controlled)
                floor = h._raw_number(raw, "floor_num", "floor")
                act = h._raw_number(raw, "act")
                outcome = raw.get("outcome")
                if floor is None or act is None or not isinstance(outcome, str) or not outcome:
                    raise GateError("INCOMPLETE", "native terminal lacks floor/Act/outcome")
                source_floor = float(state.source_floor)
                historical_floor = float(target["terminal_floors"][seed_offset])
                row = {"event": "continuation_completed", "attempt": attempt_key, "attempt_number": attempt_number, "state_index": idx, "family": state.family, "source_run_id": state.source_run_id, "root_action_index": action_index, "root_action_identity": dict(branch["root_action_identity"]), "root_roles": list(branch["root_roles"]), "continuation_seed": seed, "simulator_seed": state.simulator_seed, "terminal": True, "terminal_floor": float(floor), "terminal_act": float(act), "terminal_outcome": outcome, "source_floor": source_floor, "g_m_floor": max(0.0, float(floor) - source_floor), "g_e_floor": max(0.0, historical_floor - source_floor), "historical_expert_terminal_floor": historical_floor, "step_count_including_forced_root": steps, "native_search_cost": cost, "noncombat_policy_events": events, "attempt_wall_seconds": round(time.monotonic() - attempt_started, 3), "resource_sample": sample(), "completed_at_utc": utc_now()}
                append(PROGRESS_PATH, row)
                complete[key] = {k: row[k] for k in ("state_index", "family", "source_run_id", "root_action_index", "root_action_identity", "root_roles", "continuation_seed", "simulator_seed", "terminal", "terminal_floor", "terminal_act", "source_floor", "g_m_floor", "g_e_floor", "terminal_outcome", "step_count_including_forced_root", "native_search_cost", "noncombat_policy_events", "attempt_wall_seconds")}
                complete[key]["evidence_origin"] = "issue41_executed"
                write(STATE_PATH, {"schema_id": "issue-41-stage1-state-v1", "schema_version": 1, "issue": 41, "terminal": "STAGE1_RUNNING", "stage1_started": True, "stage1_started_at_utc": started_at, "updated_at_utc": utc_now(), "complete_unique_keys": len(complete), "expected_unique_keys": len(all_keys), "attempts_used_including_issue40": 45 + attempt_number})
            except GateError as exc:
                append(PROGRESS_PATH, {"event": "continuation_failed", "attempt": attempt_key, "attempt_number": attempt_number, "state_index": idx, "family": state.family, "root_action_index": action_index, "root_action_identity": dict(branch["root_action_identity"]), "root_roles": list(branch["root_roles"]), "continuation_seed": seed, "failure_terminal": exc.terminal, "failure": str(exc), "sample_after": sample(), "failed_at_utc": utc_now()})
                terminal, stop_detail = exc.terminal, f"{attempt_key}: {exc}"
                break
            except Exception as exc:
                append(PROGRESS_PATH, {"event": "continuation_failed", "attempt": attempt_key, "attempt_number": attempt_number, "state_index": idx, "family": state.family, "root_action_index": action_index, "root_action_identity": dict(branch["root_action_identity"]), "root_roles": list(branch["root_roles"]), "continuation_seed": seed, "failure_terminal": "INCOMPLETE", "failure": f"{type(exc).__name__}: {exc}", "sample_after": sample(), "failed_at_utc": utc_now()})
                terminal, stop_detail = "INCOMPLETE", f"{attempt_key}: {type(exc).__name__}: {exc}"
                break
            reason = budget_reason()
            if reason:
                terminal, stop_detail = "COST_OR_REPLAY_BLOCKED", reason
                break
        if terminal != "STAGE1_COMPLETE_REVIEWER_PENDING":
            break
    if set(complete) != all_keys and terminal == "STAGE1_COMPLETE_REVIEWER_PENDING":
        terminal, stop_detail = "INCOMPLETE", f"valid fixed cohort incomplete: {len(complete)}/{len(all_keys)}"
    rows = []
    old_by_key = {(int(r["state_index"]), int(r["root_action_index"]), int(r["continuation_seed"])): r for r in old["attempts"]}
    for key in sorted(complete):
        row = dict(complete[key])
        row["attempt"] = f"{key[0]}:{key[1]}:{key[2]}"
        if row.get("evidence_origin") == "issue40_reused":
            row["terminal_outcome"] = old_by_key[key]["terminal_outcome"]
        rows.append(row)
    if terminal == "STAGE1_COMPLETE_REVIEWER_PENDING":
        result = analyze(plan, rows, json.loads(BUDGET_PATH.read_text(encoding="utf-8")))
        result["execution_terminal"] = terminal
        result["study_disposition"] = "STUDY_ONLY"
        result["proposed_code_disposition"] = "STUDY_ONLY_REVIEWER_PENDING"
        result["stop_detail"] = None
    else:
        result = {"schema_id": "issue-41-t089-matched-continuation-result-v1", "schema_version": 1, "issue": 41, "terminal": terminal, "study_disposition": "STUDY_ONLY", "stage1_started": True, "started_at_utc": started_at, "finished_at_utc": utc_now(), "complete_unique_keys": len(complete), "expected_unique_keys": len(all_keys), "stop_detail": stop_detail, "partial_rows": rows, "hypothesis_result_reported": False}
    write(RESULT_PATH, result)
    write(STATE_PATH, {"schema_id": "issue-41-stage1-state-v1", "schema_version": 1, "issue": 41, "terminal": terminal, "stage1_started": True, "stage1_started_at_utc": started_at, "updated_at_utc": utc_now(), "complete_unique_keys": len(complete), "expected_unique_keys": len(all_keys), "attempts_used_including_issue40": 45 + sum(1 for e in read_progress() if e.get("event") == "continuation_started"), "result_sha256": sha(RESULT_PATH), "stop_detail": stop_detail})
    print(json.dumps({"terminal": terminal, "complete_unique_keys": len(complete), "expected_unique_keys": len(all_keys), "resources": sample()}, sort_keys=True))
    return 0 if terminal in {"STAGE1_COMPLETE_REVIEWER_PENDING", "CONTINUATION_POLICY_MISMATCH_SUPPORTED", "CONTINUATION_POLICY_SIGNAL_SURVIVES", "MECHANISM_INCONCLUSIVE"} else 2


def main() -> int:
    try:
        return run_stage1()
    except (GateError, BudgetSignal) as exc:
        previous = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.is_file() else {}
        started = bool(previous.get("stage1_started", False))
        terminal = exc.terminal if isinstance(exc, GateError) else ("COST_OR_REPLAY_BLOCKED" if started else "INCOMPLETE")
        result = {"schema_id": "issue-41-t089-matched-continuation-result-v1", "schema_version": 1, "issue": 41, "terminal": terminal, "study_disposition": "STUDY_ONLY", "stage1_started": started, "finished_at_utc": utc_now(), "stop_detail": str(exc), "hypothesis_result_reported": False}
        write(RESULT_PATH, result)
        write(STATE_PATH, {**previous, "terminal": terminal, "stage1_started": started, "updated_at_utc": utc_now(), "stop_detail": str(exc), "result_sha256": sha(RESULT_PATH)})
        if not started:
            write(PREFLIGHT_PATH, {"schema_id": "issue-41-stage1-preflight-v1", "schema_version": 1, "issue": 41, "terminal": terminal, "stage1_started": False, "stop_detail": str(exc)})
        print(json.dumps({"terminal": terminal, "stage1_started": started, "stop_detail": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _s, _f: (_ for _ in ()).throw(BudgetSignal("external supervisor termination")))
    signal.signal(signal.SIGINT, lambda _s, _f: (_ for _ in ()).throw(BudgetSignal("interrupt")))
    raise SystemExit(main())
