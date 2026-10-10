from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections import Counter
from pathlib import Path

STUDY = Path(__file__).resolve().parent
REPO = STUDY.parents[1]
STAGE0 = {
    "audit_stage0.py": "553495867516621a89e4c7fb93352c4caee6022b92c8ff3b5dac3e715b5884fa",
    "source-audit.json": "57417638a8feb1055842356780d95c9fc15e2e22aa19910396ee7e8726656249",
    "run-state.json": "6a8901ef08809727473504e6ce8bd8e2adf713526e2e429b8ff28471509aae8e",
    "README.md": "c87867c1e48764875c34fe9881256a0005add0915044a11a1c67249effea89af",
}
NATIVE = "20a6c2b3a9cea817c988178b814f083ff889853f"
JSON_SUBMODULE = "0b345b20c888f7dc8888485768e4bf9a6be29de0"
PYBIND11_SUBMODULE = "d03662f0984f652b60e7ddce53d3868002275197"
STSRL = "95abc8b1afe7d84e567aa70fb7225eba09fdff25"
SPEC = "afffcdda5cebfe47a2cfa1624911d916191bab76"
CHECKPOINT = "a3a136f729b5cee69815a0b128411eec49a9b4f7695c6aae5a3796345fbd972e"
SOURCE = "94857d0e310f34cdd2780920ec81f9dc60e179c94244b9e231952a43a5f4e8b8"
TARGETS = "b8551454f764d47b43d068d507df783226f4b2651ea74fe82bec7fd4b0b8de11"
EXTENSION = "108416736aa83973e963cb06e02411d0eb389109003c62e414631053e35ef062"

checks: dict[str, object] = {}


def require(name: str, condition: bool, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{name} failed: {detail!r}")
    checks[name] = {"passed": True, "evidence": detail}


def load(name: str) -> dict:
    return json.loads((STUDY / name).read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_text(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, text=True, capture_output=True
    ).stdout.strip()


def git_at(path: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", path, *args], check=True, text=True, capture_output=True
    ).stdout.strip()


def finite_nonnegative(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


manifest = load("output-manifest.json")
require("stage0_manifest_identity", manifest["issue"] == 39 and manifest["schema_id"] == "issue-39-output-manifest-v1")
manifest_entries = {item["path"]: item for item in manifest["artifacts"]}
require("stage0_manifest_files", set(manifest_entries) == set(STAGE0), sorted(manifest_entries))
for rel, expected_hash in STAGE0.items():
    path = STUDY / rel
    entry = manifest_entries[rel]
    actual_hash = sha256_file(path)
    require(f"stage0_immutable_{rel}", actual_hash == expected_hash == entry["sha256"] and path.stat().st_size == entry["size_bytes"], {"sha256": actual_hash, "size_bytes": path.stat().st_size})

audit = load("source-audit.json")
run_state = load("run-state.json")
require("stage0_ready", audit["terminal"] == "STAGE0_READY_FOR_SMOKE" and run_state["terminal"] == "STAGE0_READY_FOR_SMOKE")
cohort = audit["cohort"]
require("stage0_cohort_ownership", cohort["T075_selected_state_count"] == 320 and cohort["heldout_state_count"] == 64 and cohort["heldout_target_rows_expected"] == 221 and cohort["heldout_target_rows_per_eligible_action"] == 221)
require("stage0_family_balance", cohort["heldout_family_counts"] == {"MAP_SCREEN": 16, "REST_ROOM": 16, "REWARDS": 16, "TREASURE_ROOM": 16})
require("stage0_no_smoke_or_stage1_mutation", audit["execution"]["eight_state_smoke_executed"] is False and audit["execution"]["stage1_attempts"] == 0 and run_state["stage1_started"] is False and run_state["stage1_completed"] is False)

plan = load("smoke-plan.json")
native = load("native-recovery.json")
require("plan_disposition_and_terminal", plan["study_disposition"] == "STUDY_ONLY" and plan["authorized_terminal_after_pass"] == "SMOKE_PASS_STAGE1_AWAITING_PLANNER" and plan["stage_0_terminal"] == "STAGE0_READY_FOR_SMOKE")
require("planned_exact_source_identities", plan["native"]["commit"] == NATIVE and plan["native"]["json_commit"] == JSON_SUBMODULE and plan["native"]["pybind11_commit"] == PYBIND11_SUBMODULE and plan["model"]["code_commit"] == STSRL and plan["model"]["checkpoint_sha256"] == CHECKPOINT and plan["selection"]["source_selection_sha256"] == SOURCE)
notes = (STUDY / "ISSUE40-NOTES.md").read_text(encoding="utf-8")
require("human_handoff_notes_match_exact_sources", all(value in notes for value in (NATIVE, JSON_SUBMODULE, PYBIND11_SUBMODULE, STSRL, SPEC, CHECKPOINT, SOURCE, TARGETS, EXTENSION)))
require("stage0_plan_links", plan["stage_0_snapshot"]["source_audit_sha256"] == STAGE0["source-audit.json"] and plan["stage_0_snapshot"]["audit_script_sha256"] == STAGE0["audit_stage0.py"])
require("native_build_provenance", native["exact_native_source"]["head"] == NATIVE and native["exact_native_source"]["status"] == "detached" and native["exact_native_source"]["submodules"]["json"]["commit"] == JSON_SUBMODULE and native["exact_native_source"]["submodules"]["json"]["initialized"] is True and native["exact_native_source"]["submodules"]["pybind11"]["commit"] == PYBIND11_SUBMODULE and native["exact_native_source"]["submodules"]["pybind11"]["initialized"] is True)
extension = native["loaded_extension"]
ext_path = Path(extension["path"])
require("native_extension_loaded_identity", extension["loaded_successfully"] is True and extension["sha256"] == EXTENSION and ext_path.is_file() and sha256_file(ext_path) == EXTENSION and ext_path.stat().st_size == extension["size_bytes"])
build_log = (REPO / native["build"]["log"]).read_text(encoding="utf-8", errors="replace")
require("native_build_succeeded", "Built target slaythespire" in build_log and native["build"]["result"] == "Built target slaythespire")

source_roles = audit["retained_artifacts"]["T075_required_roles"]
target_roles = audit["retained_artifacts"]["T089_required_roles"]
source_role = source_roles["selected_states"]
target_role = target_roles["target_table"]
checkpoint_role = target_roles["checkpoint_893002"]
require("retained_input_audit", source_role["verified"] is True and source_role["sha256"] == SOURCE and target_role["verified"] is True and target_role["sha256"] == TARGETS and checkpoint_role["verified"] is True and checkpoint_role["sha256"] == CHECKPOINT)
require("retained_role_inventory", all(item.get("verified") is True for item in list(source_roles.values()) + list(target_roles.values())))
for name, item, expected in (("T075 selected states", source_role, SOURCE), ("T089 target table", target_role, TARGETS), ("T089 checkpoint", checkpoint_role, CHECKPOINT)):
    artifact_path = Path(item["path"])
    require(f"external_hash_{name}", artifact_path.is_file() and sha256_file(artifact_path) == expected and artifact_path.stat().st_size == item["size_bytes"], {"path": str(artifact_path), "sha256": expected, "size_bytes": item["size_bytes"]})

stage0_contract = audit["identity"]
require("accepted_code_and_spec_identity", stage0_contract["T089_accepted_code_head"] == STSRL and stage0_contract["T089_approved_spec_commit"] == SPEC)
require("stage0_target_identity", target_role["sha256"] == TARGETS and audit["retained_artifacts"]["T089_target_record_count_from_training_provenance"] == 1111)
native_path = native["exact_native_source"]["path"]
native_head = git_at(native_path, "rev-parse", "HEAD")
require("native_source_git_head", native_head == NATIVE)
submodule_lines = git_at(native_path, "submodule", "status", "--", "json", "pybind11").splitlines()
submodule_heads = {line.lstrip(" +-").split()[1]: line.lstrip(" +-").split()[0] for line in submodule_lines}
require("native_git_submodule_heads", submodule_heads == {"json": JSON_SUBMODULE, "pybind11": PYBIND11_SUBMODULE}, submodule_heads)
stsr_path = native["other_required_source_identity"]["STSRL_code"]["worktree"]
stsr_head = git_at(stsr_path, "rev-parse", "HEAD")
require("accepted_stsrl_git_head", stsr_head == STSRL)
require("approved_spec_object_present", git_at(stsr_path, "cat-file", "-t", SPEC) == "commit")

result = load("smoke-result.json")
state = load("smoke-state.json")
supervisor = load("smoke-supervisor.json")
budget = load("smoke-budget.json")
require("smoke_pass_terminal", result["issue"] == 40 and result["terminal"] == "SMOKE_PASS_STAGE1_AWAITING_PLANNER" and state["terminal"] == result["terminal"])
require("stage1_not_started", result["stage1_started"] is False and state["stage1_started"] is False and supervisor["stage1_started"] is False and budget["no_stage1"] is True)
require("smoke_result_state_hash", state["result_sha256"] == sha256_file(STUDY / "smoke-result.json"))
require("smoke_result_counts", result["complete_continuations"] == 44 and result["expected_continuations"] == 44 and result["maximum_authorized_continuations"] == 64 and state["complete_continuations"] == 44 and state["expected_continuations"] == 44)

expected = {}
for state_plan in plan["selection"]["heldout_states"]:
    state_index = int(state_plan["index"])
    for role in ("model", "expert"):
        selected = state_plan[role]
        pair = (int(selected["index"]), str(selected["action_id"]))
        pair_key = (state_index, pair[0])
        entry = expected.setdefault(pair_key, {"action_id": pair[1], "roles": set(), "family": state_plan["family"]})
        require("planned_action_index_identity_consistent", entry["action_id"] == pair[1] and entry["family"] == state_plan["family"], pair_key)
        entry["roles"].add(role + "_root")
require("planned_unique_root_action_pairs", len(expected) == 11 and plan["execution"]["unique_root_state_action_pairs"] == 11)
expected_attempts = {
    (state_index, action_index, int(seed)): details
    for (state_index, action_index), details in expected.items()
    for seed in plan["selection"]["continuation_seeds"]
}
require("planned_continuation_count", len(expected_attempts) == plan["execution"]["expected_complete_continuations"] == 44)

decisions = result["runtime_evidence"]["root_decisions"]
require("all_preselected_root_decisions_present", len(decisions) == 8)
decision_by_state = {int(item["state_index"]): item for item in decisions}
require("root_decision_states_unique_and_exact", len(decision_by_state) == 8 and set(decision_by_state) == {int(x["index"]) for x in plan["selection"]["heldout_states"]})
for state_plan in plan["selection"]["heldout_states"]:
    decision = decision_by_state[int(state_plan["index"])]
    require(f"root_decision_{state_plan['index']}", decision["family"] == state_plan["family"] and decision["public_context_status"] == "available" and decision["model_root_action_index"] == state_plan["model"]["index"] and decision["model_root_action_identity"]["action_id"] == state_plan["model"]["action_id"] and decision["expert_root_action_index"] == state_plan["expert"]["index"] and decision["expert_root_action_identity"]["action_id"] == state_plan["expert"]["action_id"])

rows = result["attempts"]
require("smoke_rows_count", len(rows) == 44)
rows_by_key = {}
for row in rows:
    key = (int(row["state_index"]), int(row["root_action_index"]), int(row["continuation_seed"]))
    require("no_duplicate_continuation_row", key not in rows_by_key, key)
    rows_by_key[key] = row
require("exact_planned_continuation_set", set(rows_by_key) == set(expected_attempts), {"missing": sorted(set(expected_attempts) - set(rows_by_key)), "unexpected": sorted(set(rows_by_key) - set(expected_attempts))})
for key, row in rows_by_key.items():
    state_index, action_index, seed = key
    expected_row = expected_attempts[key]
    require("continuation_id_matches_fixed_plan", row["attempt"] == f"{state_index}:{action_index}:{seed}")
    require("continuation_identity_matches_plan", row["family"] == expected_row["family"] and row["root_action_identity"]["action_id"] == expected_row["action_id"] and set(row["root_roles"]) == expected_row["roles"])
    require("native_terminal_not_truncation", row["event"] == "continuation_completed" and row["terminal"] is True and 0 < row["step_count_including_forced_root"] <= plan["execution"]["max_steps"])
    require("continuation_scored_against_frozen_reference", finite_nonnegative(row["smoke_q_floor"]) and finite_nonnegative(row["historical_expert_q_floor"]) and finite_nonnegative(row["historical_expert_terminal_floor"]) and math.isfinite(row["q_floor_delta_vs_historical_expert"]))
    cost = row["native_search_cost"]
    require("native_search_cost_recorded", all(name in cost and finite_nonnegative(cost[name]) for name in ("native_search_decisions", "native_search_simulator_steps", "native_search_wall_clock_seconds")))

supported = {"MAP_SCREEN", "REST_ROOM", "REWARDS", "TREASURE_ROOM"}
unsupported = {"EVENT_SCREEN", "SHOP_ROOM", "CARD_SELECT"}
policy_counts = Counter()
for row in rows:
    for event in row["noncombat_policy_events"]:
        family = event["screen_family"]
        status = event["status"]
        require("known_noncombat_policy_family", family in supported | unsupported, family)
        require("correct_learned_or_fallback_routing", (status == "learned_success" if family in supported else status == "unsupported_fallback"), {"family": family, "status": status})
        policy_counts[status] += 1
require("learned_and_explicit_fallback_observed", policy_counts["learned_success"] == 575 and policy_counts["unsupported_fallback"] == 122, dict(policy_counts))

progress = [json.loads(line) for line in (STUDY / "smoke-progress.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
progress_counts = Counter(item["event"] for item in progress)
started = {item["attempt"] for item in progress if item["event"] == "continuation_started"}
completed = {item["attempt"] for item in progress if item["event"] == "continuation_completed"}
require("progress_log_complete_and_failure_free", progress_counts == {"continuation_started": 44, "continuation_completed": 44, "smoke_terminal": 1} and started == completed and completed == {row["attempt"] for row in rows})
require("terminal_console_recorded", result["terminal"] in (STUDY / "smoke-console.log").read_text(encoding="utf-8"))

history = budget["execution_history"]
history_wall = sum(float(item["wall_seconds"]) for item in history)
history_cpu = sum(float(item["process_cpu_seconds_observed"]) for item in history)
history_attempts = sum(int(item["continuation_attempts_started"]) for item in history)
history_complete = sum(int(item["continuations_logged_complete"]) for item in history)
require("budget_ledger_consistency", budget["status"] == "CONSUMED" and history_attempts == budget["attempted_continuations_before_current"] == result["total_attempts_counted_against_budget"] == 45 and history_complete == 44 and math.isclose(history_wall, budget["spent_wall_seconds_before_current"], abs_tol=0.001) and math.isclose(history_cpu, budget["spent_process_cpu_seconds_before_current"], abs_tol=0.001))
require("attempt_limit_respected", history_attempts <= budget["max_complete_continuation_attempts"] == 64)
require("aggregate_wall_and_cpu_budgets_respected", history_wall <= budget["max_wall_clock_seconds"] == 1800 and history_cpu <= budget["max_process_cpu_seconds"] == 1800)
peak_rss = max(int(item["peak_rss_bytes_observed"]) for item in history)
minimum_available = min(int(item["minimum_mem_available_bytes_observed"]) for item in history)
require("aggregate_memory_budget_respected", peak_rss <= budget["max_peak_rss_mib"] * 1024 * 1024 and minimum_available >= budget["minimum_mem_available_mib"] * 1024 * 1024)
require("successful_supervised_execution", supervisor["child_returncode"] == 0 and supervisor["stop_reason"] is None and supervisor["wall_seconds"] < budget["max_wall_clock_seconds"] and supervisor["max_observed_process_cpu_seconds"] < budget["max_process_cpu_seconds"])
require("supervisor_memory_limits_respected", supervisor["max_observed_process_rss_bytes"] <= budget["max_peak_rss_mib"] * 1024 * 1024 and supervisor["minimum_observed_mem_available_bytes"] >= budget["minimum_mem_available_mib"] * 1024 * 1024)
require("fixed_failure_and_retry_accounted", len(history) == 3 and history[0]["continuation_attempts_started"] == 1 and history[0]["continuations_logged_complete"] == 0 and history[1]["continuation_attempts_started"] == 0 and history[1]["continuations_logged_complete"] == 0 and history[2]["continuation_attempts_started"] == 44 and history[2]["continuations_logged_complete"] == 44)
require("conservative_resource_totals_match_supervisor", math.isclose(history_wall, supervisor["total_wall_seconds_including_prior"], abs_tol=0.001) and math.isclose(history_cpu, supervisor["total_cpu_seconds_including_prior"], abs_tol=0.001))

outcomes = Counter(row["terminal_outcome"] for row in rows)
verification = {
    "schema_id": "issue-40-verification-v1",
    "schema_version": 1,
    "issue": 40,
    "passed": True,
    "terminal": result["terminal"],
    "proposed_disposition": plan["study_disposition"],
    "stage1_started": False,
    "checks": checks,
    "evidence": {
        "stage0_manifest_file_count": len(manifest_entries),
        "stage0_cohort": {"source_states": cohort["T075_selected_state_count"], "heldout_states": cohort["heldout_state_count"], "heldout_target_rows": cohort["heldout_target_rows_expected"]},
        "native_commit": NATIVE,
        "native_extension_sha256": EXTENSION,
        "stsr_code_commit": STSRL,
        "accepted_spec_commit": SPEC,
        "target_table_sha256": TARGETS,
        "checkpoint_sha256": CHECKPOINT,
        "smoke_rows": len(rows),
        "unique_state_action_pairs": len(expected),
        "continuation_seeds": plan["selection"]["continuation_seeds"],
        "step_limit": plan["execution"]["max_steps"],
        "max_observed_steps": max(row["step_count_including_forced_root"] for row in rows),
        "terminal_outcomes": dict(sorted(outcomes.items())),
        "noncombat_policy_events": dict(sorted(policy_counts.items())),
        "continuation_attempts_counted": history_attempts,
        "wall_seconds_cumulative": round(history_wall, 3),
        "process_cpu_seconds_cumulative": round(history_cpu, 3),
        "peak_rss_bytes_cumulative_max": peak_rss,
        "minimum_available_memory_bytes": minimum_available,
        "result_sha256": sha256_file(STUDY / "smoke-result.json"),
        "state_sha256": sha256_file(STUDY / "smoke-state.json"),
    },
}
verification_path = STUDY / "verification.json"
verification_bytes = (json.dumps(verification, indent=2, sort_keys=True) + "\n").encode("utf-8")
verification_path.write_bytes(verification_bytes)

final_manifest_path = STUDY / "final-manifest.json"
if final_manifest_path.is_file():
    final_manifest = json.loads(final_manifest_path.read_text(encoding="utf-8"))
    current = []
    for path in sorted((p for p in STUDY.rglob("*") if p.is_file() and p != final_manifest_path), key=lambda p: p.relative_to(STUDY).as_posix()):
        current.append({"path": path.relative_to(STUDY).as_posix(), "sha256": sha256_file(path), "size_bytes": path.stat().st_size})
    require_manifest = final_manifest.get("schema_id") == "issue-40-final-manifest-v1" and final_manifest.get("artifacts") == current
    if not require_manifest:
        raise AssertionError("final-manifest.json does not match current study artifacts")
    print(f"PASS final manifest: {len(current)} files")
else:
    print("NOTE final-manifest.json not generated yet")

print(f"PASS Issue 40 verification: {len(checks)} checks")
print(json.dumps(verification["evidence"], indent=2, sort_keys=True))
