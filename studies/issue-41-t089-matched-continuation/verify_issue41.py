#!/usr/bin/env python3
"""Independent integrity and preregistered-analysis verifier for Issue 41."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
I40 = REPO / "studies/issue-40-native-recovery"
RUNNER = HERE / "run_stage1.py"
RUNNER_REL = "studies/issue-41-t089-matched-continuation/run_stage1.py"
VERIFIER = HERE / "verify_issue41.py"
VERIFIER_REL = "studies/issue-41-t089-matched-continuation/verify_issue41.py"
MANIFEST = HERE / "final-manifest.json"
MANIFEST_REL = "studies/issue-41-t089-matched-continuation/final-manifest.json"
# Pin the runner to the exact reviewed head. Its blob is unchanged from the
# source used during execution; later evidence-only commits must preserve it.
RUNNER_REFERENCE_COMMIT = "e48146c1b28f060e12e4d6c0c8e098db332bda7c"
ART = Path("/mnt/d/DeadlyCatCoding/STSRL/artifacts")
T075 = ART / "t075-leakage-safe-non-combat-cohort-repair"
T089 = ART / "t089-frozen-search-self-generated-non-combat-policy"
SOURCE = T075 / "selected-states.jsonl"
OWNERSHIP = T075 / "ownership-audit.json"
TARGETS = T089 / "target-table-formal-attempt-2.json"
AUDIT = I40 / "source-audit.json"
FAMILIES = ("MAP_SCREEN", "REST_ROOM", "REWARDS", "TREASURE_ROOM")
SEEDS = (892201, 892202, 892203, 892204)
EXPECTED = {
    "source": "94857d0e310f34cdd2780920ec81f9dc60e179c94244b9e231952a43a5f4e8b8",
    "target": "b8551454f764d47b43d068d507df783226f4b2651ea74fe82bec7fd4b0b8de11",
}
MAX_WALL = 12000.0
MAX_CPU = 12000.0
MAX_ATTEMPTS = 512
HISTORICAL_STARTS = 45
CHECKS = 0


class VerifyError(RuntimeError):
    pass


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise VerifyError(message)


def load(path: Path) -> Any:
    check(path.is_file(), f"missing required evidence file: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    if path.resolve().is_relative_to(REPO.resolve()):
        # Git's core.autocrlf clean conversion canonicalizes CRLF text to LF.
        # Match committed evidence bytes so verification is checkout-independent.
        return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_prefix() -> list[str]:
    git_marker = REPO / ".git"
    if not git_marker.is_file() or os.name == "nt":
        return ["-C", str(REPO)]
    line = git_marker.read_text(encoding="utf-8").strip()
    if not line.startswith("gitdir:"):
        raise VerifyError("worktree .git file has an unexpected format")
    git_dir = line.split(":", 1)[1].strip()
    if len(git_dir) >= 3 and git_dir[1] == ":" and git_dir[2] in {"/", "\\"}:
        git_dir = f"/mnt/{git_dir[0].lower()}/{git_dir[3:].replace(chr(92), '/') }"
    elif not Path(git_dir).is_absolute():
        git_dir = str((REPO / git_dir).resolve())
    return ["--git-dir", git_dir, "--work-tree", str(REPO)]


def git_bytes(*args: str) -> bytes:
    return subprocess.run(
        ["git", *git_prefix(), *args],
        cwd=REPO,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def git_text(*args: str) -> str:
    return git_bytes(*args).decode("ascii").strip()


def git_oid_for_bytes(data: bytes) -> str:
    return subprocess.run(
        ["git", *git_prefix(), "hash-object", "-w", "--stdin"],
        cwd=REPO,
        input=data,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.decode("ascii").strip()


def git_blob_for_worktree(path: Path) -> tuple[str, bytes]:
    """Return canonical LF Git bytes independent of Windows checkout EOLs."""
    raw = path.read_bytes()
    canonical = raw.replace(b"\r\n", b"\n")
    oid = git_oid_for_bytes(canonical)
    blob = git_bytes("cat-file", "blob", oid)
    if blob != canonical:
        raise VerifyError(f"Git blob bytes differ from canonical contents for {path.name}")
    return oid, blob


def git_blob_for_commit(commit: str, relative: str) -> tuple[str, bytes]:
    oid = git_text("rev-parse", f"{commit}:{relative}")
    return oid, git_bytes("cat-file", "blob", oid)


def verify_runner_source() -> dict[str, Any]:
    expected_oid, expected_bytes = git_blob_for_commit(RUNNER_REFERENCE_COMMIT, RUNNER_REL)
    head_oid = git_text("rev-parse", f"HEAD:{RUNNER_REL}")
    worktree_oid, canonical_bytes = git_blob_for_worktree(RUNNER)
    check(head_oid == expected_oid, "reviewed HEAD runner Git blob differs from the executed Stage-1 runner")
    check(worktree_oid == expected_oid, "runner worktree does not clean to the executed Stage-1 Git blob")
    check(canonical_bytes == expected_bytes, "runner canonical Git blob bytes differ from the executed source")
    return {
        "path": RUNNER_REL,
        "reviewed_commit": RUNNER_REFERENCE_COMMIT,
        "git_blob_oid": expected_oid,
        "sha256": hashlib.sha256(expected_bytes).hexdigest(),
        "size_bytes": len(expected_bytes),
    }


def verify_executed_verifier() -> dict[str, Any]:
    """Fail closed unless the verifier on disk is the exact committed source."""
    expected_oid, expected_bytes = git_blob_for_commit("HEAD", VERIFIER_REL)
    worktree_oid, worktree_bytes = git_blob_for_worktree(VERIFIER)
    check(worktree_oid == expected_oid, "executed verifier worktree differs from HEAD Git blob")
    check(worktree_bytes == expected_bytes, "executed verifier bytes differ from HEAD Git blob")
    return {
        "path": VERIFIER_REL,
        "git_blob_oid": expected_oid,
        "sha256": hashlib.sha256(expected_bytes).hexdigest(),
        "size_bytes": len(expected_bytes),
    }


def verify_previous_manifest() -> bool:
    """On reruns, compare the previous v2 manifest with committed blob bytes."""
    if not MANIFEST.is_file():
        return False
    manifest_head_oid, manifest_head_bytes = git_blob_for_commit("HEAD", MANIFEST_REL)
    manifest_worktree_oid, manifest_worktree_bytes = git_blob_for_worktree(MANIFEST)
    check(manifest_worktree_oid == manifest_head_oid, "manifest worktree differs from HEAD Git blob")
    check(manifest_worktree_bytes == manifest_head_bytes, "manifest bytes differ from HEAD Git blob")
    previous = json.loads(manifest_worktree_bytes.decode("utf-8"))
    if previous.get("schema_id") != "issue-41-final-manifest-v2":
        return False
    source = previous.get("code_source", {})
    check(source.get("reviewed_commit") == RUNNER_REFERENCE_COMMIT, "manifest reviewed-source commit mismatch")
    runner_oid, runner_bytes = git_blob_for_commit(RUNNER_REFERENCE_COMMIT, RUNNER_REL)
    runner_entry = next((item for item in previous.get("files", []) if item.get("path") == "run_stage1.py"), {})
    check(source.get("runner_git_blob_oid") == runner_oid, "manifest runner Git blob OID mismatch")
    check(runner_entry.get("git_blob_oid") == runner_oid, "manifest runner file Git blob OID mismatch")
    check(runner_entry.get("size_bytes") == len(runner_bytes), "manifest runner canonical size mismatch")
    check(runner_entry.get("sha256") == hashlib.sha256(runner_bytes).hexdigest(), "manifest runner canonical SHA-256 mismatch")
    for item in previous.get("files", []):
        relative = str(item.get("path", ""))
        check(relative and Path(relative).name == relative, "manifest path is not a study-local filename")
        path = HERE / relative
        check(path.is_file(), f"manifest-listed file missing: {relative}")
        expected_oid = str(item.get("git_blob_oid", ""))
        committed_path = f"{RUNNER_REL.rsplit('/', 1)[0]}/{relative}"
        committed_oid, committed_bytes = git_blob_for_commit("HEAD", committed_path)
        worktree_oid, worktree_bytes = git_blob_for_worktree(path)
        check(worktree_oid == committed_oid, f"worktree differs from committed Git blob for {relative}")
        check(worktree_bytes == committed_bytes, f"worktree bytes differ from committed Git blob for {relative}")
        recorded_bytes = committed_bytes
        if expected_oid != committed_oid and relative == Path(VERIFIER_REL).name:
            # This one-commit bridge lets a committed verifier repair refresh its
            # own prior manifest entry; the verifier worktree is independently
            # required above to match HEAD exactly.
            previous_source_oid, recorded_bytes = git_blob_for_commit("HEAD^", committed_path)
            check(expected_oid == previous_source_oid, "prior manifest verifier blob is not the parent-commit source")
        else:
            check(expected_oid == committed_oid, f"Git blob OID mismatch for {relative}")
        check(len(recorded_bytes) == item.get("size_bytes"), f"canonical Git blob size mismatch for {relative}")
        check(hashlib.sha256(recorded_bytes).hexdigest() == item.get("sha256"), f"canonical Git blob SHA-256 mismatch for {relative}")
    return True


def iter_array(path: Path, key: str):
    marker = f'  "{key}": ['
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.startswith(marker):
                break
        else:
            raise VerifyError(f"JSON document lacks array {key!r}")
        buffer = stream.read(65536)
        while True:
            buffer = buffer.lstrip()
            if buffer.startswith("]"):
                return
            if buffer.startswith(","):
                buffer = buffer[1:]
                continue
            if not buffer:
                buffer = stream.read(65536)
                if not buffer:
                    raise VerifyError("JSON array ended unexpectedly")
                continue
            try:
                row, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                chunk = stream.read(65536)
                if not chunk:
                    raise VerifyError("JSON array contains malformed input")
                buffer += chunk
                continue
            if not isinstance(row, dict):
                raise VerifyError("target array contains a non-object row")
            yield row
            buffer = buffer[end:]


def target_rows() -> dict[tuple[int, int], dict[str, Any]]:
    check(sha(TARGETS) == EXPECTED["target"], "frozen T089 target-table hash changed")
    selected = load(HERE / "stage1-plan.json")["selection"]["heldout_states"]
    source_floor = {int(s["index"]): float(s["source_floor"]) for s in selected}
    wanted = {(int(s["index"]), int(i)) for s in selected for i in s["eligible_action_indices"]}
    rows = {}
    for row in iter_array(TARGETS, "targets"):
        key = (row.get("selected_state_index"), row.get("legal_action_index"))
        if key not in wanted:
            continue
        check(key not in rows, f"duplicate frozen target row: {key}")
        check(row.get("split") == "heldout" and row.get("target_status") == "complete" and not row.get("problems"), f"invalid target status at {key}")
        check(row.get("continuation_seeds") == list(SEEDS), f"target seed order changed at {key}")
        floors = row.get("terminal_floors")
        check(isinstance(floors, list) and len(floors) == 4, f"per-seed terminal target missing at {key}")
        expected_q = max(0.0, statistics.mean(map(float, floors)) - source_floor[int(key[0])])
        check(abs(float(row["q_floor"]) - expected_q) <= 1e-9, f"frozen Q arithmetic mismatch at {key}")
        rows[(int(key[0]), int(key[1]))] = row
    check(len(rows) == 221, f"expected exact 221 selected target rows, got {len(rows)}")
    return rows


def rows_from_progress(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not path.exists():
        return [], []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return ([x for x in events if x.get("event") == "continuation_started"],
            [x for x in events if x.get("event") in {"continuation_completed", "continuation_failed"}])


def bootstrap(states: list[dict[str, Any]]) -> dict[str, Any]:
    import numpy as np
    grouped = {family: [row for row in states if row["family"] == family] for family in FAMILIES}
    check(all(len(grouped[f]) == 16 for f in FAMILIES), "bootstrap input is not 16 states per family")
    rng = np.random.default_rng(391089)
    dm = np.empty(10000, dtype=np.float64)
    delta = np.empty(10000, dtype=np.float64)
    for draw in range(10000):
        chosen = []
        for family in FAMILIES:
            rows = grouped[family]
            chosen.extend(rows[int(i)] for i in rng.integers(0, 16, size=16))
        dm[draw] = statistics.mean(float(x["d_m"]) for x in chosen)
        delta[draw] = statistics.mean(float(x["delta"]) for x in chosen)
    return {
        "mean_d_m_ci95": [float(x) for x in np.quantile(dm, [0.025, 0.975], method="linear")],
        "mean_delta_ci95": [float(x) for x in np.quantile(delta, [0.025, 0.975], method="linear")],
    }


def near(a: Any, b: Any) -> bool:
    return math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-12)


def validate() -> dict[str, Any]:
    global CHECKS
    CHECKS = 0
    verifier_source = verify_executed_verifier()
    runner_source = verify_runner_source()
    previous_manifest_git_checked = verify_previous_manifest()
    plan = load(HERE / "stage1-plan.json")
    check(plan.get("schema_id") == "issue-41-t089-matched-continuation-plan-v1", "unexpected plan schema")
    check(plan.get("study_disposition") == "STUDY_ONLY", "study disposition changed")
    check(plan["resource_projection"]["projection_gate_passed"] is True, "prelaunch resource projection did not pass")
    check(sha(SOURCE) == EXPECTED["source"], "frozen T075 selected-state hash changed")
    audit = load(AUDIT)
    ownership = load(OWNERSHIP)
    check(sha(OWNERSHIP) == plan["source_ownership"]["ownership_audit_sha256"], "source ownership audit changed")
    check(audit.get("identity", {}).get("T075_retention_manifest_sha256") == plan["input_identity"]["retention_manifests"][0]["sha256"], "T075 retention manifest identity mismatch")
    check(audit.get("identity", {}).get("T089_retention_manifest_sha256") == plan["input_identity"]["retention_manifests"][1]["sha256"], "T089 retention manifest identity mismatch")
    check(plan["source_ownership"]["verified_state_count"] == 320 and plan["source_ownership"]["heldout_source_run_count"] == 43, "source owner gate counts changed")
    check(len(ownership.get("groups", [])) > 0, "source ownership groups unavailable")
    verify40 = subprocess.run([sys.executable, str(I40 / "verify_issue40.py")], cwd=REPO, text=True, capture_output=True)
    check(verify40.returncode == 0 and "PASS Issue 40 verification: 66 checks" in verify40.stdout, "Issue 40's independent verifier failed")
    plan_spec = importlib.util.spec_from_file_location("issue41_plan_builder_for_verify", HERE / "build_stage1_plan.py")
    check(plan_spec is not None and plan_spec.loader is not None, "plan verifier module unavailable")
    plan_module = importlib.util.module_from_spec(plan_spec)
    plan_spec.loader.exec_module(plan_module)
    roles = plan_module.verify_roles(audit)
    check(roles["retained_role_count"] == 14, f"retained role count changed: {roles['retained_role_count']}")

    selection = plan["selection"]["heldout_states"]
    check(len(selection) == 64, "heldout selection is not exactly 64 roots")
    check(Counter(s["family"] for s in selection) == Counter({f: 16 for f in FAMILIES}), "heldout families are not balanced")
    check(len({s["source_run_id"] for s in selection}) == 43, "heldout source-run count changed")
    check(plan["source"]["heldout_state_count"] == 64 and plan["baseline"]["target_rows_heldout"] == 221, "plan cohort/target counts changed")
    branches = plan["execution"]["physical_branches"]
    branch_by_key = {(int(b["state_index"]), int(b["root_action_index"])): b for b in branches}
    check(len(branch_by_key) == 95, "physical root-action pair count changed")
    expected_keys = {(int(b["state_index"]), int(b["root_action_index"]), seed) for b in branches for seed in SEEDS}
    check(len(expected_keys) == 380, "unique physical continuation key count changed")
    check(plan["execution"]["reused_issue40_complete_rows"] == 44 and plan["execution"]["new_unique_continuations"] == 336, "Issue 40 reuse mapping changed")
    check(plan["execution"]["total_attempts_if_complete_no_retry"] == 381 and 381 <= MAX_ATTEMPTS, "attempt projection changed")
    targets = target_rows()

    preflight = load(HERE / "stage1-preflight.json")
    check(preflight.get("plan_sha256") == sha(HERE / "stage1-plan.json"), "preflight plan hash mismatch")
    if preflight.get("terminal") == "PREFLIGHT_PASS_STAGE1_READY":
        check(preflight.get("stage1_started") is False and preflight.get("root_decisions_verified") == 64, "runtime preflight did not verify all roots")
        check(preflight.get("exact_issue40_rows_reused") == 44 and preflight.get("new_unique_keys") == 336, "runtime preflight reuse counts changed")
        runtime = preflight.get("runtime_identity", {})
        check(runtime.get("native_commit") == plan["native"]["commit"], "runtime native source identity mismatch")
        check(runtime.get("json_submodule") == plan["native"]["json_commit"] and runtime.get("pybind11_submodule") == plan["native"]["pybind11_commit"], "runtime submodule identity mismatch")
        check(runtime.get("extension_sha256") == plan["native"]["extension_sha256"], "loaded extension hash mismatch")
        check(runtime.get("STSRL_commit") == plan["model"]["code_commit"] and runtime.get("checkpoint_sha256") == plan["model"]["checkpoint_sha256"], "runtime model identity mismatch")
        review = load(HERE / "stage1-resource-review.json")
        check(review.get("terminal") == "RESOURCE_PROJECTION_PASS" and review.get("plan_sha256") == sha(HERE / "stage1-plan.json"), "observed-resource projection gate did not pass for this plan")
        check(review.get("remaining_unique_continuations") == 336 and review.get("total_attempts_if_no_retry_including_issue40") == 381, "observed resource review used different continuation counts")
        check(review["upper_projection_from_start"]["wall_seconds"] <= MAX_WALL and review["upper_projection_from_start"]["process_cpu_seconds"] <= MAX_CPU, "actualized upper resource projection exceeds cap")
        check(review["observed_memory"]["peak_process_tree_rss_bytes"] <= 8192 * 1024 * 1024 and review["observed_memory"]["minimum_available_memory_bytes"] >= 4096 * 1024 * 1024, "preflight memory gate failed")
    else:
        check(preflight.get("terminal") in {"HISTORICAL_IDENTITY_UNAVAILABLE", "SOURCE_OR_TARGET_UNAVAILABLE", "PUBLIC_INPUT_INVALID", "PAIRED_TARGET_MISMATCH", "COST_OR_REPLAY_BLOCKED", "INCOMPLETE"}, "unknown preflight terminal")

    old = load(I40 / "smoke-result.json")
    old_rows = {}
    for row in old.get("attempts", []):
        key = (int(row["state_index"]), int(row["root_action_index"]), int(row["continuation_seed"]))
        check(key not in old_rows, f"duplicate Issue 40 completion {key}")
        old_rows[key] = row
    check(len(old_rows) == 44, "Issue 40 must contribute exactly 44 unique complete rows")
    for key, row in old_rows.items():
        branch = branch_by_key.get(key[:2])
        check(branch is not None and key[2] in SEEDS, f"Issue 40 row is outside the plan: {key}")
        check(row.get("root_action_identity") == branch["root_action_identity"] and set(row.get("root_roles", [])) == set(branch["root_roles"]), f"Issue 40 root role/identity mismatch: {key}")

    starts, events = rows_from_progress(HERE / "stage1-progress.jsonl")
    check(len(starts) <= MAX_ATTEMPTS - HISTORICAL_STARTS, "continuation start ledger exceeded total attempt ceiling")
    start_keys = [(int(x["state_index"]), int(x["root_action_index"]), int(x["continuation_seed"])) for x in starts]
    check(all(k in expected_keys for k in start_keys), "attempt ledger includes a key outside the fixed plan")
    completed_events = [x for x in events if x.get("event") == "continuation_completed"]
    failed_events = [x for x in events if x.get("event") == "continuation_failed"]
    check(len(completed_events) == len({(int(x["state_index"]), int(x["root_action_index"]), int(x["continuation_seed"])) for x in completed_events}), "duplicate Stage 1 completed key")
    check(all((int(x["state_index"]), int(x["root_action_index"]), int(x["continuation_seed"])) in start_keys for x in events), "completion/failure lacks a durable start row")
    check(all(int(x["step_count_including_forced_root"]) <= 500 and x.get("terminal") is True for x in completed_events), "truncated or nonterminal continuation was counted complete")
    check(not any(int(x.get("step_count_including_forced_root", 0)) > 500 for x in failed_events), "failure ledger records a truncation as successful")

    result = load(HERE / "stage1-result.json")
    check(result.get("schema_id") == "issue-41-t089-matched-continuation-result-v1" and result.get("issue") == 41, "unexpected result schema or issue")
    check(result.get("study_disposition") == "STUDY_ONLY", "result must remain study-only")
    state = load(HERE / "stage1-state.json")
    budget = load(HERE / "stage1-budget.json")
    history = budget.get("execution_history", [])
    check(budget.get("schema_id") == "issue-41-stage1-budget-v1", "resource budget ledger schema changed")
    check(budget.get("stage1_max_wall_seconds") == MAX_WALL and budget.get("stage1_max_process_cpu_seconds") == MAX_CPU, "resource cap identity changed")
    check(state.get("terminal") in {"PREFLIGHT_PASS_STAGE1_READY", "STAGE1_COMPLETE_REVIEWER_PENDING", "INCOMPLETE", "COST_OR_REPLAY_BLOCKED", "HISTORICAL_IDENTITY_UNAVAILABLE", "SOURCE_OR_TARGET_UNAVAILABLE", "PUBLIC_INPUT_INVALID", "PAIRED_TARGET_MISMATCH"}, "unknown execution state terminal")
    total_wall = sum(float(x.get("wall_seconds", 0)) for x in history)
    total_cpu = sum(float(x.get("process_cpu_seconds_observed", 0)) for x in history)
    peak_rss = max((int(x.get("peak_process_tree_rss_bytes_observed", 0)) for x in history), default=0)
    min_mem = min((int(x["minimum_mem_available_bytes_observed"]) for x in history if x.get("minimum_mem_available_bytes_observed") is not None), default=0)
    check(total_wall <= MAX_WALL and total_cpu <= MAX_CPU, "stage-wide wall/CPU budget exceeded")
    check(peak_rss <= 8192 * 1024 * 1024, "process-tree RSS cap exceeded")
    check(min_mem >= 4096 * 1024 * 1024, "minimum available-memory floor violated")
    check(45 + len(starts) <= MAX_ATTEMPTS, "combined Issue 40/41 continuation starts exceeded 512")
    check(int(budget.get("continuation_attempts_started", -1)) == len(starts), "budget ledger and durable attempt starts disagree")

    reused = []
    for row in result.get("rows", []):
        if row.get("evidence_origin") == "issue40_reused":
            reused.append(row)
    complete_result = result.get("terminal") in {"CONTINUATION_POLICY_MISMATCH_SUPPORTED", "CONTINUATION_POLICY_SIGNAL_SURVIVES", "MECHANISM_INCONCLUSIVE"}
    if complete_result:
        check(result.get("study_disposition") == "STUDY_ONLY" and result.get("proposed_code_disposition") == "STUDY_ONLY_REVIEWER_PENDING", "study-only disposition changed")
        check(state.get("terminal") == "STAGE1_COMPLETE_REVIEWER_PENDING" and result.get("stage1_started") is True, "completed analysis did not reach reviewer-pending state")
        rows = result.get("rows", [])
        row_by_key = {(int(r["state_index"]), int(r["root_action_index"]), int(r["continuation_seed"])): r for r in rows}
        check(len(rows) == 380 and set(row_by_key) == expected_keys, "final result lacks exact 380-key coverage")
        check(len(reused) == 44 and {tuple((int(r["state_index"]), int(r["root_action_index"]), int(r["continuation_seed"]))) for r in reused} == set(old_rows), "Issue 40 rows were not reused exactly once")
        for key, row in row_by_key.items():
            branch = branch_by_key[key[:2]]
            state_info = next(s for s in selection if int(s["index"]) == key[0])
            target = targets[key[:2]]
            offset = SEEDS.index(key[2])
            check(row.get("root_action_identity") == branch["root_action_identity"] and set(row.get("root_roles", [])) == set(branch["root_roles"]), f"root identity/roles mismatch at {key}")
            check(row.get("source_run_id") == state_info["source_run_id"] and row.get("family") == state_info["family"] and int(row["simulator_seed"]) == int(state_info["simulator_seed"]), f"source identity mismatch at {key}")
            check(row.get("terminal") is True and int(row["step_count_including_forced_root"]) <= 500, f"invalid terminal at {key}")
            gm = max(0.0, float(row["terminal_floor"]) - float(state_info["source_floor"]))
            ge = max(0.0, float(target["terminal_floors"][offset]) - float(state_info["source_floor"]))
            check(near(row["g_m_floor"], gm) and near(row["g_e_floor"], ge), f"per-seed floor arithmetic mismatch at {key}")
            if key in old_rows:
                old_row = old_rows[key]
                check(row.get("evidence_origin") == "issue40_reused" and near(row["terminal_floor"], old_row["terminal_floor"]) and near(row["terminal_act"], old_row["terminal_act"]) and row.get("terminal_outcome") == old_row["terminal_outcome"], f"reused Issue 40 outcome/floor/Act changed at {key}")
        stats = result["primary_analysis"]
        report_by_idx = {int(s["state_index"]): s for s in stats["states"]}
        check(len(report_by_idx) == 64 and set(report_by_idx) == {int(s["index"]) for s in selection}, "per-state analysis does not cover all 64 roots")
        derived = []
        for state_info in selection:
            idx = int(state_info["index"])
            per_seed = []
            reported = report_by_idx[idx]
            check(reported.get("family") == state_info["family"] and reported.get("source_run_id") == state_info["source_run_id"], f"per-state source identity mismatch at {idx}")
            reported_seeds = {int(x["seed"]): x for x in reported["per_seed"]}
            check(set(reported_seeds) == set(SEEDS), f"per-state seed coverage mismatch at {idx}")
            for seed in SEEDS:
                mi, ei = int(state_info["model"]["index"]), int(state_info["expert"]["index"])
                mr, er = row_by_key[(idx, mi, seed)], row_by_key[(idx, ei, seed)]
                de = float(mr["g_e_floor"]) - float(er["g_e_floor"])
                dm = float(mr["g_m_floor"]) - float(er["g_m_floor"])
                per_seed.append((de, dm))
                summary = reported_seeds[seed]
                check(near(summary["g_e_model_root"], mr["g_e_floor"]) and near(summary["g_e_expert_root"], er["g_e_floor"]), f"per-seed historical Q mapping mismatch at {idx}/{seed}")
                check(near(summary["g_m_model_root"], mr["g_m_floor"]) and near(summary["g_m_expert_root"], er["g_m_floor"]), f"per-seed continuation mapping mismatch at {idx}/{seed}")
                check(near(summary["d_e_seed"], de) and near(summary["d_m_seed"], dm) and near(summary["delta_seed"], dm - de), f"per-seed paired contrast mismatch at {idx}/{seed}")
            de = statistics.mean(x[0] for x in per_seed)
            dm = statistics.mean(x[1] for x in per_seed)
            check(near(reported["d_e"], de) and near(reported["d_m"], dm) and near(reported["delta"], dm - de), f"per-state paired contrast mismatch at {idx}")
            derived.append({"state_index": idx, "family": state_info["family"], "d_e": de, "d_m": dm, "delta": dm - de})
        check(near(stats["mean_d_e"], statistics.mean(x["d_e"] for x in derived)), "overall mean d_E mismatch")
        check(near(stats["mean_d_m"], statistics.mean(x["d_m"] for x in derived)), "overall mean d_M mismatch")
        check(near(stats["mean_delta"], statistics.mean(x["delta"] for x in derived)), "overall mean delta mismatch")
        ci = bootstrap(derived)
        check(all(near(a, b) for a, b in zip(stats["mean_d_m_ci95"], ci["mean_d_m_ci95"])), "predeclared paired-bootstrap d_M CI mismatch")
        check(all(near(a, b) for a, b in zip(stats["mean_delta_ci95"], ci["mean_delta_ci95"])), "predeclared paired-bootstrap delta CI mismatch")
        if stats["mean_delta_ci95"][1] < 0 and stats["mean_d_m"] <= 0:
            expected_terminal = "CONTINUATION_POLICY_MISMATCH_SUPPORTED"
        elif stats["mean_d_m_ci95"][0] > 0:
            expected_terminal = "CONTINUATION_POLICY_SIGNAL_SURVIVES"
        else:
            expected_terminal = "MECHANISM_INCONCLUSIVE"
        check(result["terminal"] == expected_terminal, "predeclared terminal rule was not applied exactly")
        check(result.get("secondary_sensitivity", {}).get("distinct_source_run_count") == 43, "43-run dependence limitation missing")
        check(result.get("row_count") == 380 and result.get("reused_issue40_rows") == 44 and result.get("new_issue41_rows") == 336, "result row provenance counts mismatch")
        check(result.get("runtime", {}).get("total_attempts_including_issue40", MAX_ATTEMPTS + 1) <= MAX_ATTEMPTS, "result reports attempt-cap violation")
        check(result["runtime"]["stage1_attempts_started"] == len(starts) and result["runtime"]["total_attempts_including_issue40"] == HISTORICAL_STARTS + len(starts), "result and durable attempt ledger counts disagree")
        check(near(result["runtime"]["stage1_wall_seconds"], total_wall) and near(result["runtime"]["stage1_process_cpu_seconds"], total_cpu), "result and external resource ledger totals disagree")
        check(near(result["runtime"]["peak_process_tree_rss_mib"], round(peak_rss / (1024 * 1024), 2)) and near(result["runtime"]["minimum_available_memory_mib"], round(min_mem / (1024 * 1024), 2)), "result and external memory ledger disagree")
        noncoincident = [s for s in derived if not next(x for x in selection if int(x["index"]) == s["state_index"])["coincident_root_action"]]
        switches = [s["state_index"] for s in noncoincident if (s["d_e"] > 0 > s["d_m"]) or (s["d_e"] < 0 < s["d_m"])]
        zero_crossings = [s["state_index"] for s in noncoincident if (s["d_e"] == 0) != (s["d_m"] == 0)]
        sign = result["sign_switches"]
        check(sign["noncoincident_state_count"] == len(noncoincident) and sign["strict_sign_switch_state_indices"] == switches and sign["strict_sign_switch_count"] == len(switches), "strict sign-switch summary mismatch")
        check(sign["zero_crossing_state_indices"] == zero_crossings and sign["zero_crossing_count"] == len(zero_crossings), "zero-crossing summary mismatch")
        coincident = sum(1 for s in selection if s["coincident_root_action"])
        check(sign["coincident_state_count"] == coincident and near(sign["coincident_fraction"], coincident / 64), "coincident-root summary mismatch")
        for family in FAMILIES:
            rows_family = [s for s in derived if s["family"] == family]
            summary = stats["family_breakdown"][family]
            check(summary["state_count"] == len(rows_family) == 16, f"family state count mismatch: {family}")
            check(near(summary["mean_d_e"], statistics.mean(x["d_e"] for x in rows_family)) and near(summary["mean_d_m"], statistics.mean(x["d_m"] for x in rows_family)) and near(summary["mean_delta"], statistics.mean(x["delta"] for x in rows_family)), f"family paired summary mismatch: {family}")
        check(result.get("terminal_outcomes") == dict(sorted(Counter(r["terminal_outcome"] for r in rows).items())), "terminal outcome summary mismatch")
        event_counts = Counter(e["status"] for r in rows for e in r.get("noncombat_policy_events", []))
        check(result.get("later_noncombat_policy_events") == dict(sorted(event_counts.items())), "learned/fallback event summary mismatch")
        row_families = dict(sorted(Counter(r["family"] for r in rows).items()))
        expected_family_rows = dict(sorted((family, count * len(SEEDS)) for family, count in Counter(b["family"] for b in branches).items()))
        check(result.get("continuation_rows_by_family") == row_families and row_families == expected_family_rows, "continuation family coverage mismatch")
        steps = [int(r["step_count_including_forced_root"]) for r in rows]
        check(result["step_count"]["minimum"] == min(steps) and result["step_count"]["maximum"] == max(steps) and near(result["step_count"]["mean"], statistics.mean(steps)), "step-count summary mismatch")
    else:
        check(result.get("hypothesis_result_reported") is not True, "partial data must not produce a hypothesis result")
        check(not result.get("primary_analysis"), "partial data must not publish a full-cohort analysis")
    return {
        "schema_id": "issue-41-verification-v2",
        "terminal": "PASS_COMPLETE" if complete_result else "PASS_FAIL_CLOSED",
        "checks": CHECKS,
        "verifier_source": verifier_source,
        "runner_source": runner_source,
        "previous_manifest_git_checked": previous_manifest_git_checked,
        "plan_sha256": sha(HERE / "stage1-plan.json"),
        "preflight_terminal": preflight.get("terminal"),
        "result_terminal": result.get("terminal"),
        "complete_result_rows": len(result.get("rows", [])),
        "old_rows_reused": len(reused),
        "new_attempt_starts": len(starts),
        "failed_attempts": len(failed_events),
        "stage1_wall_seconds": round(total_wall, 3),
        "stage1_process_cpu_seconds": round(total_cpu, 3),
        "peak_process_tree_rss_mib": round(peak_rss / (1024 * 1024), 2),
        "minimum_available_memory_mib": round(min_mem / (1024 * 1024), 2),
    }


def main() -> int:
    try:
        report = validate()
        (HERE / "verification.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_files = []
        for path in sorted(HERE.iterdir()):
            if path.is_file() and path.name != "final-manifest.json":
                git_blob_oid, canonical_bytes = git_blob_for_worktree(path)
                manifest_files.append({
                    "path": path.name,
                    "size_bytes": len(canonical_bytes),
                    "sha256": hashlib.sha256(canonical_bytes).hexdigest(),
                    "git_blob_oid": git_blob_oid,
                })
        runner_source = report["runner_source"]
        manifest = {
            "schema_id": "issue-41-final-manifest-v2",
            "schema_version": 2,
            "issue": 41,
            "study_disposition": "STUDY_ONLY",
            "self_hash_excluded": True,
            "code_source": {
                "verifier_path": report["verifier_source"]["path"],
                "verifier_git_blob_oid": report["verifier_source"]["git_blob_oid"],
                "verifier_sha256": report["verifier_source"]["sha256"],
                "verifier_size_bytes": report["verifier_source"]["size_bytes"],
                "reviewed_commit": runner_source["reviewed_commit"],
                "runner_path": runner_source["path"],
                "runner_git_blob_oid": runner_source["git_blob_oid"],
                "runner_sha256": runner_source["sha256"],
                "runner_size_bytes": runner_source["size_bytes"],
            },
            "files": manifest_files,
            "verification": report,
        }
        MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"PASS Issue 41 verification: {report['checks']} checks; {report['terminal']}; result={report['result_terminal']}; rows={report['complete_result_rows']}; manifest_files={len(manifest_files)}")
        return 0
    except Exception as exc:
        print(f"FAIL Issue 41 verification: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
