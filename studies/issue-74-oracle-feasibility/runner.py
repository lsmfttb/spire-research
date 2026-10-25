"""Issue 74 feasibility package. Source locking is static; sampling is review-gated."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

import protocol


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CONFIG_PATH = HERE / "config.json"
CONFIG_LOCK_PATH = HERE / "config.lock.sha256"
PACKAGE_LOCK_PATH = HERE / "source-lock.json"
SELECTION_LOCK_PATH = HERE / "selection-lock.json"
REVIEWER_PREFLIGHT_PATH = HERE / "reviewer-preflight.json"
REQUIRED_PACKAGE_FILES = (
    "config.json", "config.lock.sha256", "protocol.py", "runner.py", "test_protocol.py",
)


def _text_sha(path: Path) -> str:
    raw = path.read_bytes()
    if b"\r" in raw.replace(b"\r\n", b""):
        raise protocol.ContractError(f"bare CR in locked text file: {path}")
    return protocol.sha256_bytes(raw.replace(b"\r\n", b"\n"))


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise protocol.ContractError(f"missing or malformed JSON: {path}") from exc


def _config() -> dict[str, Any]:
    value = _json(CONFIG_PATH)
    if not isinstance(value, dict) or value.get("schema_id") != "issue74-oracle-feasibility-config-v1":
        raise protocol.ContractError("Issue 74 config schema mismatch")
    if _text_sha(CONFIG_PATH) != CONFIG_LOCK_PATH.read_text(encoding="utf-8").strip():
        raise protocol.ContractError("Issue 74 config differs from config.lock.sha256")
    if value["intervention"]["formal_stage1_authorized"] is not False or value["intervention"]["stage1_formal_execution_authorized"] is not False:
        raise protocol.ContractError("Issue 74 must not authorize Stage 1 formal execution")
    return value


def _git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if check and result.returncode:
        raise protocol.ContractError(f"git {' '.join(args)} failed in {root}: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_blob_oid(root: Path, revision: str, relative_path: str) -> str:
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify",
                             f"{revision}:{relative_path}"], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        return ""
    return result.stdout.strip()


def _git_blob_text_sha(root: Path, revision: str, relative_path: str) -> str:
    result = subprocess.run(["git", "-C", str(root), "show", f"{revision}:{relative_path}"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise protocol.ContractError(f"committed file is missing: {revision}:{relative_path}")
    raw = result.stdout
    if b"\r" in raw.replace(b"\r\n", b""):
        raise protocol.ContractError(f"bare CR in committed text file: {revision}:{relative_path}")
    return protocol.sha256_bytes(raw.replace(b"\r\n", b"\n"))


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor",
                           older, newer], check=False).returncode == 0


def _verify_package(*, require_committed: bool) -> dict[str, Any]:
    config = _config()
    manifest = _json(PACKAGE_LOCK_PATH)
    if manifest.get("schema_id") != "issue74-package-source-lock-v1":
        raise protocol.ContractError("Issue 74 package source-lock schema mismatch")
    files = manifest.get("files")
    if not isinstance(files, Mapping) or set(files) != set(REQUIRED_PACKAGE_FILES):
        raise protocol.ContractError("Issue 74 package source-lock membership mismatch")
    for rel, expected in sorted(files.items()):
        path = HERE / rel
        if not path.is_file() or _text_sha(path) != expected:
            raise protocol.ContractError(f"Issue 74 package source hash mismatch: {rel}")
    if require_committed:
        head = _git(REPO, "rev-parse", "HEAD")
        package_rel = HERE.relative_to(REPO).as_posix()
        for rel, expected in sorted(files.items()):
            blob = subprocess.run(["git", "-C", str(REPO), "show", f"{head}:{package_rel}/{rel}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if blob.returncode or protocol.sha256_bytes(blob.stdout.replace(b"\r\n", b"\n")) != expected:
                raise protocol.ContractError(f"committed package Git blob differs from source lock: {rel}")
        manifest_blob = subprocess.run(["git", "-C", str(REPO), "show",
                                        f"{head}:{package_rel}/source-lock.json"],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if manifest_blob.returncode or protocol.sha256_bytes(manifest_blob.stdout.replace(b"\r\n", b"\n")) != _text_sha(PACKAGE_LOCK_PATH):
            raise protocol.ContractError("Issue 74 package source lock is not the committed manifest")
    return {"head": _git(REPO, "rev-parse", "HEAD"),
            "package_source_lock_sha256": protocol.sha256_file(PACKAGE_LOCK_PATH),
            "config_sha256": _text_sha(CONFIG_PATH), "files": dict(files),
            "formal_stage1_authorized": config["intervention"]["formal_stage1_authorized"]}


def lock_package() -> dict[str, Any]:
    _config()
    files = {rel: _text_sha(HERE / rel) for rel in REQUIRED_PACKAGE_FILES}
    manifest = {"schema_id": "issue74-package-source-lock-v1", "files": files}
    digest = protocol.write_once(PACKAGE_LOCK_PATH, manifest)
    return {"status": "PACKAGE_LOCK_WRITTEN", "source_lock_sha256": digest, "files": files}


def _verify_stage1_package(config: Mapping[str, Any]) -> dict[str, Any]:
    stage1 = REPO / str(config["source_base"]["stage1_prep_path"])
    source_lock = _json(stage1 / "source-lock.json")
    files = source_lock.get("files")
    if (source_lock.get("schema_id") != "issue73-stage1-preparation-source-lock-v1"
            or not isinstance(files, Mapping)):
        raise protocol.ContractError("Stage 1 preparation package source-lock is invalid")
    for rel, expected in sorted(files.items()):
        path = stage1 / rel
        if not path.is_file() or _text_sha(path) != expected:
            raise protocol.ContractError(f"pinned Stage 1 preparation file differs: {rel}")
    stage1_config = _json(stage1 / "config.json")
    lock_digest = (stage1 / "config.lock.sha256").read_text(encoding="utf-8").strip()
    if _text_sha(stage1 / "config.json") != lock_digest:
        raise protocol.ContractError("pinned Stage 1 prep config lock does not match")
    if (stage1_config.get("stage1_authorized") is not False
            or stage1_config.get("stage1_formal_execution_authorized") is not False):
        raise protocol.ContractError("pinned Stage 1 preparation package is not disabled")
    for seed in stage1_config.get("population", {}).get("seeds", []):
        if seed in protocol.OWNER_SEEDS:
            raise protocol.ContractError("Issue 73 formal population overlaps the Issue 74 debug owners")
    return {"commit": config["source_base"]["stage1_prep_source_commit"],
            "source_lock_sha256": _text_sha(stage1 / "source-lock.json"),
            "config_sha256": _text_sha(stage1 / "config.json"),
            "stage1_authorized": False, "stage1_formal_execution_authorized": False}


def _verify_sources(*, native_root: Path, stsr_root: Path, native_module: Path) -> dict[str, Any]:
    config = _config()
    source = config["source_base"]
    base = source["execution_base_commit"]
    head = _git(REPO, "rev-parse", "HEAD")
    if not _is_ancestor(REPO, base, head):
        raise protocol.ContractError("execution branch no longer descends from the registered Stage 1 prep base")
    branch = _git(REPO, "branch", "--show-current")
    if branch != source["execution_branch"]:
        raise protocol.ContractError(f"wrong Issue 74 execution branch: {branch}")
    dirty = _git(REPO, "status", "--porcelain")
    if dirty:
        raise protocol.ContractError("execution repository must be clean for source preparation")
    remote_head = _git(REPO, "rev-parse", f"origin/{branch}", check=False)
    if remote_head != head:
        raise protocol.ContractError("source lock must be prepared from the exact pushed execution commit")
    native_commit = _git(native_root, "rev-parse", "HEAD")
    stsr_commit = _git(stsr_root, "rev-parse", "HEAD")
    if native_commit != source["native_commit"] or _git(native_root, "status", "--porcelain"):
        raise protocol.ContractError("native simulator worktree is not the exact clean registered commit")
    if stsr_commit != source["stsr_commit"] or _git(stsr_root, "status", "--porcelain"):
        raise protocol.ContractError("STSRL worktree is not the exact clean registered commit")
    integration = subprocess.run(["git", "-C", str(native_root), "merge-base", "--is-ancestor",
                                  source["native_integration_commit"], native_commit], check=False)
    if integration.returncode:
        raise protocol.ContractError("registered STSRL native integration commit is not in the native source lineage")
    module_sha = protocol.sha256_file(native_module)
    if module_sha != source["native_module_sha256"]:
        raise protocol.ContractError("native module binary hash differs from its pinned Issue 73 identity")
    stage1 = _verify_stage1_package(config)
    root_lock_path = REPO / str(source["stage0_root_source_lock_path"])
    gate_lock_path = REPO / str(source["stage0_gate_c_source_lock_path"])
    gate_handoff_path = REPO / str(source["stage0_gate_c_handoff_path"])
    for path, expected in ((root_lock_path, source["stage0_root_source_lock_sha256"]),
                           (gate_lock_path, source["stage0_gate_c_source_lock_sha256"]),
                           (gate_handoff_path, source["stage0_gate_c_handoff_sha256"])):
        if _text_sha(path) != expected:
            raise protocol.ContractError(f"registered source receipt hash differs: {path.name}")
    root_lock = _json(root_lock_path)
    root_source = root_lock.get("root_source", {})
    root_tree = root_lock.get("root_input_tree", {})
    root_dir = REPO / str(source["stage0_root_path"])
    if (root_lock.get("schema_id") != "issue73-gate-a-requalification-source-lock-v1"
            or root_source.get("producer_commit") != source["stage0_root_producer_commit"]
            or root_source.get("producer_config_sha256") != "3393c326e62b3f2ef15883de95ddc6ca9379c4ca7008fd35251fc57292e1e76c"
            or root_tree.get("git_tree_sha1") != source["stage0_root_tree_sha1"]
            or root_tree.get("file_count") != source["stage0_root_count"]):
        raise protocol.ContractError("Stage 0 source lock does not match the registered 225-root source")
    root_tree_path = f"{source['stage0_root_path']}"
    for commit in (source["stage0_root_producer_commit"], source["accepted_stage0_commit"], head):
        tree = _git(REPO, "rev-parse", f"{commit}:{root_tree_path}")
        if tree != source["stage0_root_tree_sha1"]:
            raise protocol.ContractError(f"225-root Git tree changed at {commit}")
    hashes = root_source.get("files_sha256")
    if not isinstance(hashes, Mapping) or len(hashes) != 225:
        raise protocol.ContractError("Stage 0 root source lock does not enumerate exactly 225 files")
    actual_files = {path.name for path in root_dir.glob("*.json")}
    if actual_files != set(hashes):
        raise protocol.ContractError("Stage 0 root directory membership differs from its lock")
    root_records: list[dict[str, Any]] = []
    for name, expected in sorted(hashes.items()):
        path = root_dir / name
        if _text_sha(path) != expected:
            raise protocol.ContractError(f"Stage 0 root file differs from its source hash: {name}")
        row = _json(path)
        if (row.get("root_id") != Path(name).stem
                or row.get("native_source_commit") != source["native_commit"]
                or row.get("native_binary_sha256") != source["native_module_sha256"]
                or row.get("config_sha256") != root_source.get("producer_config_sha256")
                or row.get("candidate_binding_validation_passed") is not True):
            raise protocol.ContractError(f"Stage 0 root source/provenance mismatch: {name}")
        row["_source_file_sha256"] = expected
        root_records.append(row)
    gate_lock = _json(gate_lock_path)
    gate = _json(gate_handoff_path)
    if (gate_lock.get("schema_id") != "issue73-stage0-gate-bc-v14-preseed-source-lock-v1"
            or gate_lock.get("no_stage1_or_training_authority") is not True
            or set(gate_lock.get("gate_c_root_ids", [])) != set(protocol.GATE_C_ROOT_IDS)
            or gate.get("status") != "PASS"
            or gate.get("stage1_authorized") is not False):
        raise protocol.ContractError("Gate C is not PASS/disabled or its excluded-root set changed")
    debug_runs: dict[str, str] = {}
    run_rows: dict[int, Mapping[str, Any]] = {}
    run_dir = REPO / str(source["stage0_root_path"]).replace("roots", "debug-owner-runs")
    for owner in protocol.OWNER_SEEDS:
        run_path = run_dir / f"{owner}.json"
        run = _json(run_path)
        if (run.get("seed") != owner or run.get("native_source_commit") != source["native_commit"]
                or run.get("native_module_sha256") != source["native_module_sha256"]
                or run.get("terminal_complete") is not True or not isinstance(run.get("decisions"), list)):
            raise protocol.ContractError(f"fixed debug-owner prefix receipt is incomplete or wrong-source: {owner}")
        run_rows[owner] = run
        debug_runs[str(owner)] = _text_sha(run_path)
    selected = protocol.select_roots(root_records)
    gate_ids = sorted(protocol.GATE_C_ROOT_IDS)
    if set(gate_lock.get("gate_c_root_ids", [])) != set(gate_ids):
        raise protocol.ContractError("Gate C exclusion set differs from the preregistered root IDs")
    by_id = {str(row["root_id"]): row for row in root_records}
    for selected_row in selected:
        source_root = by_id[selected_row["root_id"]]
        event_index = selected_row["owner_decision_index"]
        run = run_rows[selected_row["owner_seed"]]
        if not 0 <= event_index < len(run["decisions"]):
            raise protocol.ContractError(f"selected root has no owner decision prefix: {selected_row['root_id']}")
        event = run["decisions"][event_index]
        protocol.verify_source_trace_event(source_root, event)
    return {"native_commit": native_commit, "stsr_commit": stsr_commit,
            "native_module_sha256": module_sha, "execution_head": head,
            "root_lock_sha256": _text_sha(root_lock_path), "gate_c_lock_sha256": _text_sha(gate_lock_path),
            "gate_c_handoff_sha256": _text_sha(gate_handoff_path),
            "stage1_package": stage1, "root_file_hashes": dict(hashes),
            "debug_owner_run_hashes": debug_runs, "root_records": root_records,
            "selected_roots": selected}


def prepare_selection_lock(*, native_root: Path, stsr_root: Path, native_module: Path) -> dict[str, Any]:
    package = _verify_package(require_committed=True)
    source = _verify_sources(native_root=native_root, stsr_root=stsr_root, native_module=native_module)
    roots = source["selected_roots"]
    menu_strata: dict[str, int] = {}
    families: dict[str, int] = {}
    early = late = 0
    attempt_plan: list[dict[str, Any]] = []
    for row in roots:
        families[row["screen"]] = families.get(row["screen"], 0) + 1
        is_early = row["policy_decision_index"] < 2
        early += int(is_early)
        late += int(not is_early)
        count = row["legal_occurrence_count"]
        menu_strata[str(count)] = menu_strata.get(str(count), 0) + 1
        c_policy = protocol.stream_seed(owner_seed=row["owner_seed"], root_id=row["root_id"],
                                        decision_index=row["policy_decision_index"], stream="control_policy")
        c_expert = protocol.stream_seed(owner_seed=row["owner_seed"], root_id=row["root_id"],
                                        decision_index=row["policy_decision_index"], stream="control_expert")
        oracle_attempts = []
        if count <= 14:
            for candidate in range(count):
                for replicate in range(8):
                    oracle_attempts.append({
                        "candidate_index": candidate, "replicate": replicate,
                        "policy_seed": protocol.stream_seed(
                            owner_seed=row["owner_seed"], root_id=row["root_id"],
                            decision_index=row["policy_decision_index"], stream="oracle_policy",
                            candidate_index=candidate, replicate=replicate),
                        "expert_seed": protocol.stream_seed(
                            owner_seed=row["owner_seed"], root_id=row["root_id"],
                            decision_index=row["policy_decision_index"], stream="oracle_expert",
                            candidate_index=candidate, replicate=replicate),
                    })
        attempt_plan.append({"root_id": row["root_id"], "control_policy_seed": c_policy,
                             "control_expert_seed": c_expert, "oracle_suffixes": oracle_attempts,
                             "oracle_preabstention_reason": "candidate_count_over_cap" if count > 14 else None})
    max_suffix_cpu = sum(len(row["oracle_suffixes"]) * 20 for row in attempt_plan)
    max_replay_control_cpu = 32 * 240
    runtime_preflight_cpu = 600
    registered_suffix_ceiling = int(_config()["resource_budget"]["worst_case_suffix_reservation_cpu_seconds"])
    max_native = max_suffix_cpu + max_replay_control_cpu + runtime_preflight_cpu
    if max_suffix_cpu > registered_suffix_ceiling or max_native > 85000:
        raise protocol.ContractError("locked workload cannot fit the registered Issue 74 native budget")
    selection_fields = {"owner_seed", "screen", "policy_decision_index", "root_id"}
    manifest = {
        "schema_id": "issue74-prospective-selection-lock-v1",
        "status": "PRESELECTION_LOCKED_PENDING_INDEPENDENT_REVIEW",
        "issue": "https://github.com/lsmfttb/spire-research/issues/74",
        "source_code_commit": package["head"],
        "package_source_lock_sha256": package["package_source_lock_sha256"],
        "config_sha256": package["config_sha256"],
        "registered_population": {"source_count": 225, "selected_count": 32,
                                  "owner_seeds": list(protocol.OWNER_SEEDS),
                                  "per_owner": 4, "excluded_gate_c_root_ids": sorted(protocol.GATE_C_ROOT_IDS),
                                  "selection_domain": protocol.SELECTION_DOMAIN,
                                  "selection_hash_fields": sorted(selection_fields),
                                  "outcome_independent": True, "without_replacement": True,
                                  "eligibility_filter_before_selection": False,
                                  "denominator_includes_ineligible": True},
        "source_receipts": {key: value for key, value in source.items()
                            if key not in {"root_records", "selected_roots"}},
        "selected_roots": roots,
        "observed_strata": {"screen_family_counts": dict(sorted(families.items())),
                            "early_policy_decision_index_lt_2": early,
                            "after_first_two_policy_decisions": late,
                            "menu_size_counts": dict(sorted(menu_strata.items(), key=lambda item: int(item[0])))},
        "coverage_targets": {"screen_families_at_least": 4, "early_roots_at_least": 1,
                             "after_first_two_roots_at_least": 1,
                             "targets_met": len(families) >= 4 and early >= 1 and late >= 1,
                             "no_replacement_if_unmet": True},
        "intervention_and_control": _config()["intervention"],
        "decision_criteria": _config()["decision_criteria"],
        "registered_endpoints": _config()["registered_endpoints"],
        "source_replay_contract": _config()["source_replay"],
        "rng_streams": _config()["rng_streams"],
        "attempt_plan": attempt_plan,
        "resource_reservation": {"selected_roots_suffix_reservation_cpu_seconds": max_suffix_cpu,
                                 "registered_worst_case_suffix_ceiling_cpu_seconds": registered_suffix_ceiling,
                                 "root_replay_plus_control_cpu_reservation_seconds": max_replay_control_cpu,
                                 "same_execution_runtime_preflight_cpu_seconds": runtime_preflight_cpu,
                                 "native_reserved_cpu_seconds": max_native,
                                 "native_ceiling_cpu_seconds": 85000,
                                 "builder_ceiling_cpu_seconds": 100000,
                                 "aggregate_ceiling_cpu_seconds": 120000,
                                 "reviewer_protected_cpu_seconds": 20000,
                                 "no_budget_recycling_or_attempt_replacement": True},
        "review_gate": {"native_root_sampling_permitted": False,
                        "requires_reviewer_pass_on_exact_commit": True,
                        "requires_same_execution_runtime_and_negative_checks": True},
        "formal_stage1_authorized": False,
        "stage1_formal_execution_authorized": False,
    }
    digest = protocol.write_once(SELECTION_LOCK_PATH, manifest)
    return {"status": manifest["status"], "selection_lock_sha256": digest,
            "root_count": len(roots), "source_code_commit": package["head"],
            "coverage_targets": manifest["coverage_targets"],
            "resource_reservation": manifest["resource_reservation"]}


def verify_reviewer_preflight() -> dict[str, Any]:
    dirty = _git(REPO, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise protocol.ContractError("Reviewer preflight requires a clean execution worktree")
    _verify_package(require_committed=True)
    selection = _json(SELECTION_LOCK_PATH)
    preflight = _json(REVIEWER_PREFLIGHT_PATH)
    if selection.get("schema_id") != "issue74-prospective-selection-lock-v1":
        raise protocol.ContractError("prospective selection lock is missing or malformed")
    if (preflight.get("status") != "PASS" or not preflight.get("reviewer_login")
            or not str(preflight.get("github_comment_url", "")).startswith(
                "https://github.com/lsmfttb/spire-research/issues/74#issuecomment-")):
        raise protocol.ContractError("native sampling requires a durable independent Reviewer PASS comment")
    reviewed_commit = str(preflight.get("reviewed_commit", ""))
    if (len(reviewed_commit) != 40
            or any(character not in "0123456789abcdef" for character in reviewed_commit)):
        raise protocol.ContractError("Reviewer PASS must cite a full lowercase commit SHA")
    head = _git(REPO, "rev-parse", "HEAD")
    resolved_reviewed = _git(REPO, "rev-parse", "--verify", f"{reviewed_commit}^{{commit}}",
                             check=False)
    if resolved_reviewed != reviewed_commit or not _is_ancestor(REPO, reviewed_commit, head):
        raise protocol.ContractError("Reviewer PASS must cite an exact commit in this execution branch")

    selection_rel = SELECTION_LOCK_PATH.relative_to(REPO).as_posix()
    preflight_rel = REVIEWER_PREFLIGHT_PATH.relative_to(REPO).as_posix()
    reviewed_selection_blob = _git_blob_oid(REPO, reviewed_commit, selection_rel)
    current_selection_blob = _git_blob_oid(REPO, head, selection_rel)
    if not reviewed_selection_blob or reviewed_selection_blob != current_selection_blob:
        raise protocol.ContractError("selected-root lock Git blob changed after Reviewer PASS")
    selection_sha = _text_sha(SELECTION_LOCK_PATH)
    if (preflight.get("reviewed_selection_lock_sha256") != selection_sha
            or _git_blob_text_sha(REPO, reviewed_commit, selection_rel) != selection_sha
            or _git_blob_text_sha(REPO, head, selection_rel) != selection_sha):
        raise protocol.ContractError("Reviewer PASS does not match the committed selected-root lock")

    if _git_blob_oid(REPO, reviewed_commit, preflight_rel):
        raise protocol.ContractError("reviewer-preflight receipt must be added after the reviewed commit")
    current_preflight_blob = _git_blob_oid(REPO, head, preflight_rel)
    if (not current_preflight_blob
            or _git_blob_text_sha(REPO, head, preflight_rel) != _text_sha(REVIEWER_PREFLIGHT_PATH)):
        raise protocol.ContractError("Reviewer preflight receipt must match a committed Git blob")
    parent = _git(REPO, "rev-parse", "--verify", f"{head}^", check=False)
    if parent != reviewed_commit:
        raise protocol.ContractError("Reviewer preflight must be the only commit after the reviewed SHA")
    changed = set(_git(REPO, "diff", "--name-only", f"{reviewed_commit}..{head}").splitlines())
    if changed != {preflight_rel}:
        raise protocol.ContractError("only the committed Reviewer preflight receipt may follow the reviewed SHA")
    return {"reviewed_commit": reviewed_commit, "reviewer": preflight["reviewer_login"],
            "comment_url": preflight["github_comment_url"], "selection_lock_sha256": selection_sha}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("lock-package", help="write the package source lock before code commit")
    prepare = commands.add_parser("prepare-lock", help="source-check and prospectively select without native rollout")
    prepare.add_argument("--native-root", type=Path, required=True)
    prepare.add_argument("--stsr-root", type=Path, required=True)
    prepare.add_argument("--native-module", type=Path, required=True)
    commands.add_parser("verify-reviewer-pass", help="validate the exact independent review gate")
    args = parser.parse_args(argv)
    try:
        if args.command == "lock-package":
            result = lock_package()
        elif args.command == "prepare-lock":
            result = prepare_selection_lock(native_root=args.native_root,
                                            stsr_root=args.stsr_root,
                                            native_module=args.native_module)
        else:
            result = {"status": "REVIEWER_PASS_VALIDATED", **verify_reviewer_preflight()}
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (protocol.ContractError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "INCOMPLETE", "error_type": type(exc).__name__,
                          "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
