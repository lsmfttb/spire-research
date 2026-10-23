"""Bounded, replay-first checkpoint qualification for Issue 73 Stage 0.

This program is deliberately study-local.  It never changes simulator rules,
creates formal owners, or rewrites the historical Stage 0 receipts.
"""
from __future__ import annotations

import time as _time
import resource as _resource
import signal as _signal


def process_tree_cpu() -> float:
    own = _resource.getrusage(_resource.RUSAGE_SELF)
    children = _resource.getrusage(_resource.RUSAGE_CHILDREN)
    return float(own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime)


_PROCESS_START_CPU = process_tree_cpu()

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

import gate_a
import runner
import stage0
from policy import AllNoncombatPolicy


STUDY = Path(__file__).resolve().parent
REPOSITORY = STUDY.parents[1]
STUDY_RELATIVE = STUDY.relative_to(REPOSITORY).as_posix()
HISTORICAL = STUDY / "receipts" / "stage0"
OUT = STUDY / "receipts" / "source-qualification-v1"
CONFIG_PATH = OUT / "qualification-config-v1.json"
LOCK_PATH = OUT / "preseed-source-lock-v1.json"
PLAN_PATH = OUT / "plans" / "gate-c-replay-plan.json"
BRANCH = "codex/issue-73-stage0-replay-qualification"
OLD_CONFIG = runner.CONFIG
SOURCE_QUAL_SEED = 173730001
REQUIRED_REPLAY_ROOTS = (
    "root-173730001-0000",
    "root-173730001-0001",
    "root-173730001-0002",
    "root-173730001-0003",
    "root-173730001-0028",
)
GATE_B_ROOTS = {
    "hidden_semantic": "root-173730001-0003",
    "native_rng_consequence": "root-173730001-0028",
    "post_two_third_family": "root-173730001-0002",
}
GATE_C_ROOTS = (
    "root-173730001-0000",
    "root-173730001-0001",
    "root-173730001-0002",
    "root-173730001-0003",
)
SOURCE_HASH_PATHS = (
    "checkpoint_replay_qualification.py",
    "config.json",
    "config.lock.sha256",
    "gate-a-target-direction.json",
    "gate_a.py",
    "policy.py",
    "public_history_execution.py",
    "runner.py",
    "stage0.py",
)
EXPECTED_BASE_COMMIT = "14ad9f1e5c7776b81ecb4215732511a1a47c4004"


class QualificationFailure(RuntimeError):
    pass


def _raise_cpu_limit(_signum: int, _frame: Any) -> None:
    raise QualificationFailure("registered process CPU ceiling reached; attempt is incomplete")


def install_process_cpu_ceiling(limit_seconds: float) -> None:
    cap = int(math.floor(float(limit_seconds)))
    if cap < 2:
        raise QualificationFailure("less than two process CPU seconds remain under the registered ceiling")
    old_soft, old_hard = _resource.getrlimit(_resource.RLIMIT_CPU)
    hard = cap if old_hard == _resource.RLIM_INFINITY else min(cap, int(old_hard))
    if hard < 2:
        raise QualificationFailure("existing process CPU hard limit is below the required minimum")
    soft = max(1, hard - 5)
    if old_soft != _resource.RLIM_INFINITY:
        soft = min(soft, int(old_soft))
    if soft >= hard:
        soft = hard - 1
    _resource.setrlimit(_resource.RLIMIT_CPU, (soft, hard))
    _signal.signal(_signal.SIGXCPU, _raise_cpu_limit)


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY), *args], check=False,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise QualificationFailure(f"git {' '.join(args)} failed: {result.stderr[-800:]}")
    return result.stdout.strip()


def jsonable(value: Any) -> Any:
    return json.loads(runner.canonical(value))


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def projection_payload(adapter: Any, snapshot: Any) -> dict[str, Any]:
    projection = adapter.public_projection(snapshot)
    payload = getattr(projection, "canonical_payload", None)
    value = json.loads(payload) if isinstance(payload, str) else dict(projection)
    if not isinstance(value, dict):
        raise QualificationFailure("public projection did not produce a JSON object")
    return value


def fingerprint_record(adapter: Any, snapshot: Any) -> dict[str, Any]:
    return {
        "native_state_sha256": runner.sha256_bytes(
            runner.canonical(adapter.checkpoint_fingerprint(snapshot)).encode("utf-8")
        ),
        **runner._snapshot_debug_fingerprints(adapter, snapshot),
        "full_checkpoint_fingerprint": jsonable(adapter.checkpoint_fingerprint(snapshot)),
    }


def root_path(root_id_value: str) -> Path:
    return HISTORICAL / "roots" / f"{root_id_value}.json"


def root_receipt(root_id_value: str) -> dict[str, Any]:
    path = root_path(root_id_value)
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("root_id") != root_id_value or value.get("candidate_binding_validation_passed") is not True:
        raise QualificationFailure(f"historical root is not a verified live legal-action receipt: {root_id_value}")
    return value


def compact_root(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "root_id": str(receipt["root_id"]),
        "root_index": int(receipt["owner_root_index"]),
        "run_id": str(receipt["owner_run_id"]),
        "seed": int(receipt["owner_seed"]),
        "decision_index": int(receipt["owner_decision_index"]),
        "policy_decision_index": int(receipt["policy_decision_index"]),
        "screen": str(receipt["screen"]),
        **dict(receipt["root_fingerprints"]),
        "projection_digest": str(receipt["projection_digest"]),
        "projection": receipt["raw_privileged_projection"],
        "public_projection": receipt["sanitized_public_projection"],
        "source_tag_mapping": receipt["source_tag_mapping"],
        "legal_action_identities": receipt["legal_action_occurrences"],
        "candidate_index": int(receipt["selected_owner_occurrence"]),
        "feature_matrix": receipt["semantic_feature_matrix"],
        "feature_matrix_sha256": receipt["feature_matrix_sha256"],
        "live_binding_validation_passed": bool(receipt["candidate_binding_validation_passed"]),
    }


def gate_c_plan(root_values: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    candidate_root_ids = sorted(
        root_values,
        key=lambda root_id_value: (
            int(root_values[root_id_value]["owner_seed"]),
            int(root_values[root_id_value]["owner_decision_index"]),
            str(root_id_value),
        ),
    )
    eligible = [
        compact_root(root_values[root_id_value])
        for root_id_value in candidate_root_ids
        if len(root_values[root_id_value].get("legal_action_occurrences", [])) >= 2
        and root_values[root_id_value].get("candidate_binding_validation_passed") is True
    ]
    eligible.sort(key=lambda row: (int(row["seed"]), int(row["decision_index"])))
    selected: list[dict[str, Any]] = []
    families: set[str] = set()
    for root in eligible:
        family = str(root["screen"])
        if family not in families:
            selected.append(root)
            families.add(family)
            if len(families) >= int(OLD_CONFIG["stage0"]["gate_c"]["screen_family_min"]):
                break
    post_two = next(
        (root for root in eligible if int(root["policy_decision_index"]) >= int(OLD_CONFIG["stage0"]["gate_c"]["require_policy_decision_index_min"])),
        None,
    )
    if post_two is not None and all(post_two["root_id"] != row["root_id"] for row in selected):
        selected.append(post_two)
    for root in eligible:
        if len(selected) >= int(OLD_CONFIG["stage0"]["gate_c"]["root_count"]):
            break
        if all(root["root_id"] != row["root_id"] for row in selected):
            selected.append(root)
    if [row["root_id"] for row in selected] != list(GATE_C_ROOTS):
        raise QualificationFailure("fixed Gate C roots do not match the original deterministic eligible-root rule")
    roots: list[dict[str, Any]] = []
    streams: list[dict[str, Any]] = []
    for root_id_value in GATE_C_ROOTS:
        receipt = root_values[root_id_value]
        root = compact_root(receipt)
        action_indices = [0, len(root["legal_action_identities"]) - 1]
        if len(root["legal_action_identities"]) < 2 or action_indices[0] == action_indices[1]:
            raise QualificationFailure(f"precommitted Gate C root is not a multichoice root: {root_id_value}")
        roots.append({
            "root_id": root_id_value,
            "owner_seed": root["seed"],
            "run_id": root["run_id"],
            "root_index": root["root_index"],
            "decision_index": root["decision_index"],
            "policy_decision_index": root["policy_decision_index"],
            "screen": root["screen"],
            "historical_root_receipt_sha256": runner.sha256_text_file(root_path(root_id_value)),
            "action_indices": action_indices,
            "action_identities": [root["legal_action_identities"][i] for i in action_indices],
        })
        for action_index in action_indices:
            for replicate in range(int(OLD_CONFIG["stage0"]["gate_c"]["repetitions_per_action"])):
                streams.append({
                    "root_id": root_id_value,
                    "root_index": root["root_index"],
                    "owner_seed": root["seed"],
                    "run_id": root["run_id"],
                    "candidate_index": action_index,
                    "replicate": replicate,
                    "suffix_policy_seed": stage0.derived_gate_c_seed("suffix", root, action_index, replicate),
                    "expert_seed": stage0.derived_gate_c_seed("expert", root, action_index, replicate),
                })
    all_stream_seeds = [
        seed for row in streams for seed in (row["suffix_policy_seed"], row["expert_seed"])
    ]
    if len(roots) != 4 or len(streams) != 64 or len(set(all_stream_seeds)) != len(all_stream_seeds):
        raise QualificationFailure("fixed Gate C plan does not have 4 roots, 64 attempts, and unique RNG streams")
    if len({row["screen"] for row in roots}) < 2 or not any(row["policy_decision_index"] >= 2 for row in roots):
        raise QualificationFailure("fixed Gate C roots miss screen-family or post-two coverage")
    return {
        "schema_id": "issue73-checkpoint-replay-gate-c-plan-v1",
        "status": "PRECOMMITTED_BEFORE_ANY_NEW_SUFFIX_RESULT",
        "selection_rule": "original Stage 0 rule over every immutable pre-existing debug-owner root receipt, in fixed seed/decision order; reserve earliest two distinct families; include the earliest policy decision index >=2; fill to four; selected roots are fixed before new replay and never replaced after parity failure",
        "candidate_root_ids": candidate_root_ids,
        "candidate_root_count": len(candidate_root_ids),
        "roots": roots,
        "action_rule": "first and last source-order live legal occurrences",
        "policy_rng_seed_streams": streams,
        "planned_suffix_attempt_count": len(streams),
        "independent_streams_no_reuse": True,
        "native_rng_resampling": "none; native RNG is reconstituted by exact seed plus complete legal action/step prefix; suffix variation resamples only the fixed pi_suffix policy RNG streams",
        "full_native_state_q_estimand": "Q(s_full,a;pi_suffix) conditional on the replay-qualified full state and native RNG state",
        "public_information_set_q_estimand": "Q(I_public,a) not estimated; no legal hidden-state sampling conditioned on public observations",
        "interrupted_or_failed_suffix_rule": "immutable failure or incomplete receipt; no replacement or result-subset claim",
    }


def make_qualification_config() -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    all_roots = stage0.load_roots()
    expected_candidate_seeds = set(range(SOURCE_QUAL_SEED, SOURCE_QUAL_SEED + 8))
    observed_candidate_seeds = {int(row["seed"]) for row in all_roots}
    if len(all_roots) != 225 or observed_candidate_seeds != expected_candidate_seeds:
        raise QualificationFailure("locked Gate C candidate population must be the exact 225 prior roots from the eight fixed debug seeds")
    selected = gate_a.select_corpus(all_roots)
    if len(selected) != 24 or len({str(row["screen"]) for row in selected}) < 4:
        raise QualificationFailure("historical immutable Gate A root corpus does not satisfy its fixed 24-root contract")
    root_ids = [str(row["root_id"]) for row in selected]
    candidate_root_ids = [str(row["root_id"]) for row in all_roots]
    root_receipts = {rid: root_receipt(rid) for rid in candidate_root_ids}
    plan = gate_c_plan(root_receipts)
    chest = root_receipts[GATE_B_ROOTS["native_rng_consequence"]]
    open_chest_indices = [
        i for i, candidate in enumerate(chest.get("raw_privileged_projection", {}).get("candidates", []))
        if ((candidate.get("features", {}).get("choice_type") or {}).get("value") == "open_chest")
    ]
    if open_chest_indices != [0]:
        raise QualificationFailure("precommitted chest root no longer maps open_chest to exact native legal occurrence zero")
    gate_b_actions = {
        "hidden_semantic": 0,
        "native_rng_consequence": open_chest_indices[0],
        "post_two_third_family": 0,
    }
    transition_pairs: dict[tuple[str, int], set[str]] = {}
    for row in plan["roots"]:
        for index in row["action_indices"]:
            transition_pairs.setdefault((row["root_id"], int(index)), set()).add("gate_c")
    for witness, rid in GATE_B_ROOTS.items():
        index = int(gate_b_actions[witness])
        transition_pairs.setdefault((rid, index), set()).add(f"gate_b:{witness}")
    transitions = []
    for (rid, index), purposes in sorted(transition_pairs.items()):
        identities = root_receipts[rid]["legal_action_occurrences"]
        if not 0 <= index < len(identities):
            raise QualificationFailure(f"precommitted transition action is outside root legal occurrences: {rid}/{index}")
        transitions.append({
            "root_id": rid,
            "candidate_index": index,
            "action_identity": identities[index],
            "purposes": sorted(purposes),
        })
    if not any("gate_b:native_rng_consequence" in row["purposes"] for row in transitions):
        raise QualificationFailure("fixed Gate B chest RNG action is absent from replay witness plan")
    old_run = HISTORICAL / "debug-owner-runs" / f"{SOURCE_QUAL_SEED}.json"
    old_attempt = HISTORICAL / "attempts" / f"debug-owner-{SOURCE_QUAL_SEED}.json"
    old_failure_files = [
        "preseed-source-lock-v3.json", "stage0-report.json", "gate-a-report.json",
        "gate-b-report.json", "gate-c-report.json", "resource-ledger.json",
    ]
    config = {
        "schema_id": "issue73-checkpoint-replay-qualification-config-v1",
        "study_id": runner.STUDY_ID,
        "source_qualification_only": True,
        "formal_training_seed_consumed": False,
        "formal_evaluation_seed_consumed": False,
        "stage1_authorized": False,
        "original_preseed_blocked_receipts_are_immutable": True,
        "method": "replay-first; complete action/step history, reset-to-seed prefix replay, full observation/resource/public state parity, exact root projection and legal occurrence parity, and repeated forced-transition witnesses including a native treasureRng chest consequence",
        "source_qualification_seed": SOURCE_QUAL_SEED,
        "historical_debug_seed_role": "exact provenance and parity witness only; never a new independent owner",
        "historical_debug_run_receipt": old_run.relative_to(STUDY).as_posix(),
        "historical_debug_attempt_receipt": old_attempt.relative_to(STUDY).as_posix(),
        "gate_a_root_ids": root_ids,
        "gate_b_witness_root_ids": GATE_B_ROOTS,
        "gate_b_witness_candidate_indices": gate_b_actions,
        "gate_c_plan_file": PLAN_PATH.relative_to(STUDY).as_posix(),
        "gate_c_root_ids": list(GATE_C_ROOTS),
        "gate_c_candidate_root_ids": candidate_root_ids,
        "gate_c_transition_pairs": transitions,
        "native_rng_limit": "opaque native RNG bytes/counters are not exposed by the pinned API; parity evidence is exact prefix transition history plus repeated forced-action post-state/info parity across process replays, including treasureRng-dependent chest generation; no byte-level RNG-state claim",
        "setup_resume_policy": "a crashed environment-only setup may be rerun only under a new immutable setup attempt key and a fresh CPU reservation; completed/failed Gate C suffix attempts are never rerun or replaced",
        "suffixed_policy": "0.80 expert_non_combat_v1 + 0.20 uniform among all currently legal actions at each multichoice; exact frozen existing Stage 0 rule",
        "old_debug_seeds_replay_scope": [SOURCE_QUAL_SEED],
        "old_debug_seeds_not_replayed": [173730002, 173730003, 173730004, 173730005, 173730006, 173730007, 173730008],
        "gates": {
            "A": "run corrected exact original fixed 24-root target/encoder test; no target reselection; <=128 gradient updates; TV>=0.05 with targeted action preference rising on >=80% roots",
            "B": "source-qualify fixed hidden MAP, native-RNG chest, and post-two CARD_SELECT witnesses; cross-process repeat exact projection/legal/root/action/post-step state; public-only API denial and hidden-field nonleak",
            "C": "four preselected seed-173730001 roots; first+last legal occurrence per root; exactly 8 suffixes per action; full terminal under unchanged pi_suffix; no replacements; fixed native RNG is not resampled",
        },
        "resource_caps": {
            "aggregate_stage0_cpu_seconds": 6000.0,
            "builder_ceiling_cpu_seconds": 4800.0,
            "reviewer_reserve_cpu_seconds": 1200.0,
            "prior_builder_charge_cpu_seconds": 1501.060651,
            "builder_remaining_before_new_phase_cpu_seconds": 3298.939349,
            "prior_native_gate_bc_charge_cpu_seconds": 960.0,
            "native_gate_bc_ceiling_cpu_seconds": 2400.0,
            "native_gate_bc_remaining_cpu_seconds": 1440.0,
            "source_history_capture_reserve_cpu_seconds": 60.0,
            "fresh_process_prefix_and_transition_parity_reserve_cpu_seconds": 80.0,
            "gate_c_environment_setup_reserve_cpu_seconds": 20.0,
            "gate_c_suffix_attempt_count": 64,
            "gate_c_per_suffix_process_cpu_cap_seconds": 20.0,
            "gate_c_suffix_total_reserve_cpu_seconds": 1280.0,
            "gate_a_nonnative_process_cpu_cap_seconds": 120.0,
            "projected_new_native_cpu_reserve_seconds": 1440.0,
            "projected_new_builder_cpu_reserve_seconds": 1560.0,
            "cumulative_builder_projection_cpu_seconds": 3061.060651,
            "remaining_builder_cpu_seconds_after_max_reserve": 1738.939349,
            "process_topology": "separate source-history capture and fresh-process prefix replay; one Gate C process with per-suffix 20 CPU-second watchdog; process CPU charged once; no build or child simulator processes",
            "no_reclaim_of_prior_charges": True,
            "failure_or_interruption": "charge at least the precommitted attempt reserve, preserve immutable receipt, stop without retry or replacement",
        },
    }
    return config, root_receipts, plan


def prepare_lock() -> dict[str, Any]:
    lock_paths = (LOCK_PATH, CONFIG_PATH, PLAN_PATH)
    existing = [path.exists() for path in lock_paths]
    if any(existing):
        if not all(existing):
            raise QualificationFailure("prospective source lock/config/plan is partial; it may not be reconstructed")
        lock, _ = verify_lock(require_remote_current=False)
        # Recover a crash after writing the immutable lock files but before the
        # scoped Git commit/push. This only commits the already-hashed bytes.
        push_scoped_commit("Issue 73: commit prospective replay source lock")
        return lock
    if any(OUT.rglob("*.json")) or any(OUT.rglob("*.jsonl")):
        raise QualificationFailure("source qualification output already contains receipts; lock cannot be regenerated")
    config, roots, plan = make_qualification_config()
    runner.atomic_json(CONFIG_PATH, config)
    plan = {**plan, "qualification_config_sha256": runner.sha256_text_file(CONFIG_PATH)}
    runner.atomic_json(PLAN_PATH, plan)
    old_input_paths = {
        HISTORICAL / "debug-owner-runs" / f"{SOURCE_QUAL_SEED}.json",
        HISTORICAL / "attempts" / f"debug-owner-{SOURCE_QUAL_SEED}.json",
        *(root_path(rid) for rid in roots),
        *(HISTORICAL / name for name in (
            "preseed-source-lock-v3.json", "stage0-report.json", "gate-a-report.json",
            "gate-b-report.json", "gate-c-report.json", "resource-ledger.json",
        )),
    }
    code_hashes = {name: runner.sha256_text_file(STUDY / name) for name in SOURCE_HASH_PATHS}
    current_commit = git("rev-parse", "HEAD")
    if git("symbolic-ref", "--short", "HEAD") != BRANCH:
        raise QualificationFailure("prospective source lock must be prepared on its registered remote branch")
    if subprocess.run(
        ["git", "-C", str(REPOSITORY), "merge-base", "--is-ancestor", EXPECTED_BASE_COMMIT, current_commit],
        check=False,
    ).returncode:
        raise QualificationFailure(f"source code branch does not descend from the registered base: {current_commit}")
    remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    if not remote or remote[0] != current_commit:
        raise QualificationFailure("prospective source code commit must be pushed before creating the source/config lock")
    lock = {
        "schema_id": "issue73-checkpoint-replay-preseed-source-lock-v1",
        "lock_revision": 1,
        "status": "PRESEED_LOCKED_BEFORE_NEW_DEBUG_EXECUTION",
        "study_only": True,
        "formal_training_seed_consumed": False,
        "formal_evaluation_seed_consumed": False,
        "stage1_authorized": False,
        "code_source_commit": current_commit,
        "remote_branch": BRANCH,
        "remote_must_equal_lock_commit_before_any_run": True,
        "qualification_config": CONFIG_PATH.relative_to(STUDY).as_posix(),
        "qualification_config_sha256": runner.sha256_text_file(CONFIG_PATH),
        "gate_c_plan": PLAN_PATH.relative_to(STUDY).as_posix(),
        "gate_c_plan_sha256": runner.sha256_text_file(PLAN_PATH),
        "study_source_sha256": code_hashes,
        "native": {
            "repository": OLD_CONFIG["identity"]["native_repository"],
            "commit": OLD_CONFIG["identity"]["native_commit"],
            "source_branch": OLD_CONFIG["identity"]["native_source_branch"],
            "source_file_sha256": OLD_CONFIG["identity"]["native_source_files_sha256"],
            "loaded_module": OLD_CONFIG["identity"]["native_module"],
            "loaded_module_sha256": OLD_CONFIG["identity"]["native_binary_sha256"],
        },
        "stsr": {
            "repository": OLD_CONFIG["identity"]["stsr_repository"],
            "commit": OLD_CONFIG["identity"]["stsr_commit"],
            "native_integration_commit": OLD_CONFIG["identity"]["stsr_native_integration_commit"],
            "source_file_sha256": OLD_CONFIG["identity"]["stsr_source_files_sha256"],
        },
        "runtime": {name: OLD_CONFIG["identity"][name] for name in ("python", "torch", "numpy")},
        "historical_input_sha256": {
            path.relative_to(STUDY).as_posix(): runner.sha256_text_file(path)
            for path in sorted(old_input_paths)
        },
        "durability_method": "reset same native owner seed and replay the complete actual legal action/step history in a fresh process; validate every recorded legal occurrence order, selected occurrence, full pinned adapter state fingerprint, sanitized public projection, exact native privileged root projection, deck/resource/map/choice fields and repeated forced-action transitions; native RNG bytes/counters remain opaque, so RNG equivalence is qualified by exact transition/post-step parity including the treasureRng-dependent chest reveal",
        "gate_a_b_c_scope_locked": True,
        "all_gate_c_stream_ids_locked_before_suffix_results": True,
        "resource_caps": config["resource_caps"],
        "source_lock_created_before_any_new_debug_execution": True,
    }
    runner.atomic_json(LOCK_PATH, lock)
    push_scoped_commit("Issue 73: commit prospective replay source lock")
    return lock


def verify_lock(*, require_remote_current: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    if not LOCK_PATH.exists() or not CONFIG_PATH.exists() or not PLAN_PATH.exists():
        raise QualificationFailure("prospective source/config/Gate C plan lock is missing")
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    if lock.get("status") != "PRESEED_LOCKED_BEFORE_NEW_DEBUG_EXECUTION":
        raise QualificationFailure("prospective source lock has an invalid status")
    if runner.sha256_text_file(CONFIG_PATH) != lock.get("qualification_config_sha256"):
        raise QualificationFailure("qualification config changed after source lock")
    if runner.sha256_text_file(PLAN_PATH) != lock.get("gate_c_plan_sha256"):
        raise QualificationFailure("Gate C roots/actions/streams plan changed after source lock")
    if plan.get("qualification_config_sha256") != lock.get("qualification_config_sha256"):
        raise QualificationFailure("Gate C plan is for a different qualification config")
    for name, expected in lock.get("study_source_sha256", {}).items():
        path = STUDY / name
        if not path.is_file() or runner.sha256_text_file(path) != expected:
            raise QualificationFailure(f"source-locked study file changed: {name}")
    for name, expected in lock.get("historical_input_sha256", {}).items():
        path = STUDY / name
        if not path.is_file() or runner.sha256_text_file(path) != expected:
            raise QualificationFailure(f"immutable historical input changed: {name}")
    ancestor = subprocess.run(
        ["git", "-C", str(REPOSITORY), "merge-base", "--is-ancestor", lock["code_source_commit"], "HEAD"],
        check=False,
    )
    if ancestor.returncode:
        raise QualificationFailure("current worktree does not descend from the locked source commit")
    if require_remote_current:
        remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
        head = git("rev-parse", "HEAD")
        if not remote:
            raise QualificationFailure("registered source branch is missing from GitHub")
        if remote[0] != head:
            remote_is_ancestor = subprocess.run(
                ["git", "-C", str(REPOSITORY), "merge-base", "--is-ancestor", remote[0], head],
                check=False,
            ).returncode == 0
            if not remote_is_ancestor:
                raise QualificationFailure("remote source branch diverged from the current evidence lineage")
            # Recover a local commit whose previous push was interrupted. No
            # experiment may proceed until its exact bytes are durable.
            git("push", "origin", f"HEAD:refs/heads/{BRANCH}")
            remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
            if not remote or remote[0] != head:
                raise QualificationFailure("remote source branch is not at the exact committed lock/evidence SHA")
    return lock, config


def push_scoped_commit(message: str) -> str:
    git("add", "--", f"{STUDY_RELATIVE}/receipts/source-qualification-v1")
    staged = [item for item in git("diff", "--cached", "--name-only").splitlines() if item]
    if any(item != f"{STUDY_RELATIVE}/receipts/source-qualification-v1" and not item.startswith(f"{STUDY_RELATIVE}/receipts/source-qualification-v1/") for item in staged):
        raise QualificationFailure("refusing to commit evidence outside the new source-qualification receipt folder")
    changed = subprocess.run(["git", "-C", str(REPOSITORY), "diff", "--cached", "--quiet"], check=False).returncode != 0
    if changed:
        git("commit", "-m", message)
    commit = git("rev-parse", "HEAD")
    git("push", "origin", f"HEAD:refs/heads/{BRANCH}")
    remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    if not remote or remote[0] != commit:
        raise QualificationFailure("GitHub does not contain the exact evidence commit")
    return commit


def ledger() -> dict[str, Any]:
    path = OUT / "resource-ledger.json"
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_id") != "issue73-checkpoint-replay-resource-ledger-v1":
            raise QualificationFailure("new source qualification ledger schema changed")
    else:
        value = {
            "schema_id": "issue73-checkpoint-replay-resource-ledger-v1",
            "baseline_builder_charge_cpu_seconds": 1501.060651,
            "baseline_native_gate_bc_charge_cpu_seconds": 960.0,
            "builder_ceiling_cpu_seconds": 4800.0,
            "native_gate_bc_ceiling_cpu_seconds": 2400.0,
            "reviewer_reserve_cpu_seconds": 1200.0,
            "attempts": [],
            "setup_process_cpu_seconds": 0.0,
            "reviewer_reserve_released_cpu_seconds": 0.0,
            "no_prior_charge_reclaimed": True,
        }
    by_key = {str(row["key"]): i for i, row in enumerate(value.get("attempts", []))}
    changed = False
    for receipt_path in sorted((OUT / "attempts").glob("*.json")):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("schema_id") != "issue73-checkpoint-replay-attempt-v1":
            continue
        key = str(receipt.get("key", ""))
        if not key or receipt_path.stem != key:
            raise QualificationFailure(f"attempt receipt key/path mismatch: {receipt_path.name}")
        if key not in by_key:
            by_key[key] = len(value["attempts"])
            value["attempts"].append(receipt)
            changed = True
            continue
        index = by_key[key]
        prior = value["attempts"][index]
        if prior.get("status") == "STARTED" and receipt.get("status") != "STARTED":
            # The attempt receipt is written before the aggregate ledger. If a
            # crash split those writes, recover its durable terminal receipt.
            value["attempts"][index] = receipt
            changed = True
        elif prior.get("status") != receipt.get("status"):
            raise QualificationFailure(f"resource ledger/attempt receipt disagree for {key}; stop for audit")
    if changed:
        runner.atomic_json(path, value)
        push_scoped_commit("Issue 73: reconcile durable source qualification attempt ledger")
    return value


def charged(value: Mapping[str, Any]) -> tuple[float, float]:
    total = float(value["baseline_builder_charge_cpu_seconds"]) + float(value.get("setup_process_cpu_seconds", 0.0))
    native = float(value["baseline_native_gate_bc_charge_cpu_seconds"])
    for row in value.get("attempts", []):
        amount = float(row.get("cpu_charged_seconds", row.get("reserved_cpu_seconds", 0.0)))
        total += amount
        if row.get("native_gate_bc"):
            native += amount
    return total, native


def start_attempt(key: str, *, kind: str, reserve: float, native_gate_bc: bool) -> dict[str, Any]:
    attempt_path = OUT / "attempts" / f"{key}.json"
    if attempt_path.exists():
        old = json.loads(attempt_path.read_text(encoding="utf-8"))
        if old.get("status") == "STARTED":
            old.update({
                "status": "INTERRUPTED_NO_RERUN",
                "cpu_charged_seconds": float(old["reserved_cpu_seconds"]),
                "failure": "started receipt has no terminal evidence; this fixed attempt may not be rerun",
            })
            value = ledger()
            value["attempts"] = [old if item.get("key") == key else item for item in value["attempts"]]
            runner.atomic_json(attempt_path, old)
            runner.atomic_json(OUT / "resource-ledger.json", value)
            push_scoped_commit(f"Issue 73: preserve interrupted source qualification attempt {key}")
            raise QualificationFailure(f"attempt {key} was interrupted; no retry permitted")
        raise QualificationFailure(f"fixed attempt {key} already has a terminal receipt: {old.get('status')}")
    value = ledger()
    total, native = charged(value)
    projected_total = total + reserve
    projected_native = native + (reserve if native_gate_bc else 0.0)
    if projected_total > 4800.0 or projected_native > 2400.0:
        raise QualificationFailure(f"registered remaining CPU cap blocks {key}: builder={projected_total:.6f}, native={projected_native:.6f}")
    row = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": key,
        "kind": kind,
        "status": "STARTED",
        "native_gate_bc": bool(native_gate_bc),
        "reserved_cpu_seconds": float(reserve),
        "source_lock_sha256": runner.sha256_text_file(LOCK_PATH),
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    value["attempts"].append(row)
    runner.atomic_json(attempt_path, row)
    runner.atomic_json(OUT / "resource-ledger.json", value)
    push_scoped_commit(f"Issue 73: start source qualification attempt {key}")
    return row


def finish_attempt(key: str, *, started_cpu: float, status: str, details: Mapping[str, Any]) -> dict[str, Any]:
    path = OUT / "attempts" / f"{key}.json"
    previous = json.loads(path.read_text(encoding="utf-8"))
    if previous.get("status") != "STARTED":
        raise QualificationFailure(f"attempt {key} is not in its immutable STARTED state")
    measured = max(0.0, process_tree_cpu() - started_cpu)
    reserve = float(previous["reserved_cpu_seconds"])
    exceeded = measured > reserve
    success = status in {"COMPLETE", "PASS", "READY"}
    row = {
        **previous,
        **dict(details),
        "status": "FAIL_CPU_RESERVATION_EXCEEDED" if exceeded else status,
        "measured_process_cpu_seconds": measured,
        "cpu_charged_seconds": measured if success and not exceeded else max(reserve, measured),
        "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    value = ledger()
    value["attempts"] = [row if item.get("key") == key else item for item in value["attempts"]]
    total, native = charged(value)
    if total > 4800.0 or native > 2400.0:
        row["status"] = "FAIL_CUMULATIVE_CPU_CEILING_EXCEEDED"
        value["attempts"] = [row if item.get("key") == key else item for item in value["attempts"]]
    runner.atomic_json(path, row)
    runner.atomic_json(OUT / "resource-ledger.json", value)
    push_scoped_commit(f"Issue 73: finish source qualification attempt {key}")
    return row


def verify_locked_external_sources() -> None:
    for env_name, key in (("STS_LIGHTSPEED_SOURCE_DIR", "native"), ("STSRL_SOURCE_DIR", "stsr")):
        root_value = os.environ.get(env_name)
        if not root_value:
            raise QualificationFailure(f"{env_name} is required by the precommitted source lock")
        root = Path(root_value)
        source_lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))[key]
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True).strip()
        if commit != source_lock["commit"] or dirty:
            raise QualificationFailure(f"{env_name} differs from the exact clean source lock")
        for name, expected in source_lock["source_file_sha256"].items():
            if runner.sha256_file(root / name) != expected:
                raise QualificationFailure(f"{env_name}/{name} differs from the exact source lock")


def historical_run_receipts() -> tuple[dict[str, Any], dict[str, Any]]:
    owner = json.loads((HISTORICAL / "debug-owner-runs" / f"{SOURCE_QUAL_SEED}.json").read_text(encoding="utf-8"))
    attempt = json.loads((HISTORICAL / "attempts" / f"debug-owner-{SOURCE_QUAL_SEED}.json").read_text(encoding="utf-8"))
    return owner, attempt


def compare_capture_run(result: Mapping[str, Any], roots: Sequence[Mapping[str, Any]], history: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    old, old_attempt = historical_run_receipts()
    checks = {
        "terminal": result.get("terminal") == old.get("terminal"),
        "terminal_complete": result.get("terminal_complete") is True and old.get("terminal_complete") is True,
        "completed_transitions": int(result.get("completed_transitions", -1)) == int(old.get("completed_transitions", -2)) == len(history),
        "run_outcome": result.get("run_outcome") == old.get("run_outcome"),
        "decisions": result.get("decisions") == old.get("decisions"),
        "screen_counts": result.get("screen_counts") == old.get("screen_counts"),
        "action_kind_counts": result.get("action_kind_counts") == old.get("action_kind_counts"),
        "debug_owner_attempt_terminal": old_attempt.get("terminal") == old.get("terminal"),
    }
    current = {stage0.root_id(root): root for root in roots}
    root_checks: dict[str, Any] = {}
    for root_id_value in sorted(current):
        historical = root_receipt(root_id_value)
        root = current[root_id_value]
        matrix_sha = runner.sha256_bytes(np.asarray(root["feature_matrix"], dtype="<f4").tobytes())
        fields = {
            "owner_root_index": int(root["root_index"]) == int(historical["owner_root_index"]),
            "owner_decision_index": int(root["decision_index"]) == int(historical["owner_decision_index"]),
            "policy_decision_index": int(root["policy_decision_index"]) == int(historical["policy_decision_index"]),
            "screen": str(root["screen"]) == str(historical["screen"]),
            "native_state_sha256": root["native_state_sha256"] == historical["root_fingerprints"]["native_state_sha256"],
            "public_projection_sha256": root["public_projection_sha256"] == historical["root_fingerprints"]["public_projection_sha256"],
            "public_resource_state_sha256": root["public_resource_state_sha256"] == historical["root_fingerprints"]["public_resource_state_sha256"],
            "legal_action_occurrence_order": runner.canonical(root["legal_action_identities"]) == runner.canonical(historical["legal_action_occurrences"]),
            "raw_privileged_projection": runner.canonical(root["projection"]) == runner.canonical(historical["raw_privileged_projection"]),
            "sanitized_public_projection": runner.canonical(root["public_projection"]) == runner.canonical(historical["sanitized_public_projection"]),
            "projection_digest": root["projection_digest"] == historical["projection_digest"],
            "semantic_feature_matrix_sha256": matrix_sha == historical["feature_matrix_sha256"],
            "candidate_binding_validation": bool(root.get("live_binding_validation_passed")) == bool(historical["candidate_binding_validation_passed"]),
        }
        root_checks[root_id_value] = fields
    target_roots = all(root_checks.get(rid) and all(root_checks[rid].values()) for rid in REQUIRED_REPLAY_ROOTS)
    report = {
        "schema_id": "issue73-source-history-capture-v1",
        "status": "PASS" if all(checks.values()) and target_roots else "FAIL_SOURCE_HISTORY_CAPTURE_PARITY",
        "replayed_seed": SOURCE_QUAL_SEED,
        "historical_owner_treatment": "exact provenance/parity replay only; not a new owner",
        "run_parity": checks,
        "history_event_count": len(history),
        "history_transition_complete": all(isinstance(row.get("transition"), Mapping) for row in history),
        "captured_root_count": len(roots),
        "historical_root_match_count": sum(all(fields.values()) for fields in root_checks.values()),
        "required_replay_root_ids": list(REQUIRED_REPLAY_ROOTS),
        "required_roots_match": target_roots,
        "roots": root_checks,
        "state_coverage": ["full LightSpeedAdapter checkpoint_fingerprint values except declared transition-only annotations", "full sanitized public projection", "source-ordered legal action occurrences", "raw privileged projection at captured roots", "deck/resources/map/choice fields from the root projections"],
        "native_rng_state_bytes_or_counters_available": False,
        "no_formal_seed_consumed": True,
    }
    return report


def capture_transition_witness(base: Any, module: Any, root: Mapping[str, Any], candidate_index: int) -> dict[str, Any]:
    checkpoint = root["checkpoint"]
    seed = int(root["seed"])
    def once() -> dict[str, Any]:
        wrapper = runner.CheckpointAdapter(base, checkpoint)
        snapshot = wrapper.reset(seed)
        identities_source = __import__("sts_combat_rl.sim.decision_record", fromlist=["action_identity_dicts_for_actions"])
        identities = identities_source.action_identity_dicts_for_actions(base.legal_actions(snapshot))
        before_projection = module.PrivilegedNoncombatProjectionAccess(runner.native_simulator(base), "training_oracle_only").project_v1()
        before_public = projection_payload(base, snapshot)
        before_state = fingerprint_record(base, snapshot)
        if not 0 <= int(candidate_index) < len(identities):
            raise QualificationFailure("precommitted forced action is not a current legal occurrence")
        actions = base.legal_actions(snapshot)
        transition = wrapper.step(actions[int(candidate_index)])
        post_snapshot = transition.snapshot
        post_public = projection_payload(base, post_snapshot)
        post_state = fingerprint_record(base, post_snapshot)
        post_actions = [] if transition.terminal else identities_source.action_identity_dicts_for_actions(base.legal_actions(post_snapshot))
        return {
            "pre_state_fingerprints": before_state,
            "pre_public_projection": before_public,
            "pre_privileged_projection": jsonable(before_projection),
            "legal_action_occurrences": identities,
            "candidate_index": int(candidate_index),
            "candidate_identity": identities[int(candidate_index)],
            "transition_terminal": bool(transition.terminal),
            "transition_info": dict(transition.info),
            "post_state_fingerprints": post_state,
            "post_public_projection": post_public,
            "post_legal_action_occurrences": post_actions,
        }
    first, second = once(), once()
    if runner.canonical(first) != runner.canonical(second):
        raise QualificationFailure(f"same-process repeated root/action transition differed for {root['root_id']}/{candidate_index}")
    return {"status": "PASS", "repeated_same_owner_restore_step_equal": True, "transition": first}


def load_history() -> list[dict[str, Any]]:
    path = OUT / "histories" / "actual-action-step-history.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_id") != "issue73-actual-action-step-history-v1":
        raise QualificationFailure("complete action/step history schema mismatch")
    events = value.get("events")
    if not isinstance(events, list) or not events:
        raise QualificationFailure("complete actual action/step history is empty")
    return events


def event_state(adapter: Any, snapshot: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return fingerprint_record(adapter, snapshot), projection_payload(adapter, snapshot)


def validate_event_before(adapter: Any, snapshot: Any, actions: Sequence[Any], event: Mapping[str, Any]) -> None:
    from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions

    state, public = event_state(adapter, snapshot)
    expected_fingerprints = event.get("state_fingerprints")
    fingerprint_keys = ("native_state_sha256", "public_projection_sha256", "public_resource_state_sha256")
    actual_fingerprints = {key: state[key] for key in fingerprint_keys}
    if not isinstance(expected_fingerprints, Mapping) or runner.canonical(actual_fingerprints) != runner.canonical(expected_fingerprints):
        raise QualificationFailure(f"replay before-state differs at action-history event {event.get('event_index')}")
    if runner.canonical(public) != runner.canonical(event["public_projection"]):
        raise QualificationFailure(f"replay public/deck/resource/map/choice projection differs at event {event.get('event_index')}")
    actual = action_identity_dicts_for_actions(actions)
    if runner.canonical(actual) != runner.canonical(event["legal_action_occurrences"]):
        raise QualificationFailure(f"ordered native legal action occurrences differ at event {event.get('event_index')}")


def validate_event_transition(adapter: Any, transition: Any, event: Mapping[str, Any]) -> None:
    after_state, after_public = event_state(adapter, transition.snapshot)
    expected = event["transition"]
    if bool(transition.terminal) != bool(expected["terminal"]):
        raise QualificationFailure(f"replay terminal marker differs at event {event.get('event_index')}")
    if runner.canonical(dict(transition.info)) != runner.canonical(expected["info"]):
        raise QualificationFailure(f"replay transition annotations differ at event {event.get('event_index')}")
    fingerprint_keys = ("native_state_sha256", "public_projection_sha256", "public_resource_state_sha256")
    actual_fingerprints = {key: after_state[key] for key in fingerprint_keys}
    if runner.canonical(actual_fingerprints) != runner.canonical(expected["post_state_fingerprints"]):
        raise QualificationFailure(f"replay post-step native state differs at event {event.get('event_index')}")
    if runner.canonical(after_public) != runner.canonical(expected["post_public_projection"]):
        raise QualificationFailure(f"replay post-step public projection differs at event {event.get('event_index')}")


def find_history_index(history: Sequence[Mapping[str, Any]], root: Mapping[str, Any]) -> int:
    matches = [
        int(event["event_index"])
        for event in history
        if int(event.get("controller_trace_index", -1)) == int(root["owner_decision_index"])
        and str(event.get("screen_state", "")) == str(root["screen"])
        and runner.canonical(event.get("legal_action_occurrences")) == runner.canonical(root["legal_action_occurrences"])
    ]
    if len(matches) != 1:
        raise QualificationFailure(f"full action history does not uniquely identify the locked root {root['root_id']}: {matches}")
    return matches[0]


def replay_prefix_to_root(adapter: Any, history: Sequence[Mapping[str, Any]], root: Mapping[str, Any], module: Any) -> tuple[Any, list[Any], int]:
    from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions

    target_index = find_history_index(history, root)
    snapshot = adapter.reset(int(root["owner_seed"]))
    for event in history[:target_index]:
        actions = adapter.legal_actions(snapshot)
        validate_event_before(adapter, snapshot, actions, event)
        selected = int(event["selected_occurrence"])
        if not 0 <= selected < len(actions):
            raise QualificationFailure(f"history selected action occurrence is invalid at {event.get('event_index')}")
        if runner.canonical(event["selected_identity"]) != runner.canonical(event["legal_action_occurrences"][selected]):
            raise QualificationFailure(f"stored selected action identity mismatch at {event.get('event_index')}")
        transition = adapter.step(actions[selected])
        validate_event_transition(adapter, transition, event)
        if transition.terminal:
            raise QualificationFailure(f"historical action history terminated before locked root {root['root_id']}")
        snapshot = transition.snapshot

    actions = adapter.legal_actions(snapshot)
    validate_event_before(adapter, snapshot, actions, history[target_index])
    identities = action_identity_dicts_for_actions(actions)
    if runner.canonical(identities) != runner.canonical(root["legal_action_occurrences"]):
        raise QualificationFailure(f"fresh-process root legal occurrences differ for {root['root_id']}")
    state, public = event_state(adapter, snapshot)
    if state["native_state_sha256"] != root["root_fingerprints"]["native_state_sha256"]:
        raise QualificationFailure(f"fresh-process root native state fingerprint differs for {root['root_id']}")
    if state["public_projection_sha256"] != root["root_fingerprints"]["public_projection_sha256"]:
        raise QualificationFailure(f"fresh-process root public projection differs for {root['root_id']}")
    if state["public_resource_state_sha256"] != root["root_fingerprints"]["public_resource_state_sha256"]:
        raise QualificationFailure(f"fresh-process root resources differ for {root['root_id']}")
    if runner.canonical(public) != runner.canonical(root["sanitized_public_projection"]):
        raise QualificationFailure(f"fresh-process public root payload differs for {root['root_id']}")
    privileged = module.PrivilegedNoncombatProjectionAccess(runner.native_simulator(adapter), "training_oracle_only").project_v1()
    if runner.canonical(privileged) != runner.canonical(root["raw_privileged_projection"]):
        raise QualificationFailure(f"fresh-process privileged choice/deck/map projection differs for {root['root_id']}")
    tags = stage0.source_tags(privileged)
    if runner.canonical(tags) != runner.canonical(root["source_tag_mapping"]):
        raise QualificationFailure(f"fresh-process source-tag mapping differs for {root['root_id']}")
    return snapshot, actions, target_index


def replay_forced_transition(adapter: Any, history: Sequence[Mapping[str, Any]], root: Mapping[str, Any], candidate_index: int, module: Any) -> dict[str, Any]:
    snapshot, actions, _ = replay_prefix_to_root(adapter, history, root, module)
    state, public = event_state(adapter, snapshot)
    from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions
    identities = action_identity_dicts_for_actions(actions)
    projection = module.PrivilegedNoncombatProjectionAccess(runner.native_simulator(adapter), "training_oracle_only").project_v1()
    if not 0 <= int(candidate_index) < len(actions):
        raise QualificationFailure(f"forced transition candidate {candidate_index} is not legal at {root['root_id']}")
    transition = adapter.step(actions[int(candidate_index)])
    post_snapshot = transition.snapshot
    post_state, post_public = event_state(adapter, post_snapshot)
    post_actions = [] if transition.terminal else action_identity_dicts_for_actions(adapter.legal_actions(post_snapshot))
    return {
        "pre_state_fingerprints": state,
        "pre_public_projection": public,
        "pre_privileged_projection": jsonable(projection),
        "legal_action_occurrences": identities,
        "candidate_index": int(candidate_index),
        "candidate_identity": identities[int(candidate_index)],
        "transition_terminal": bool(transition.terminal),
        "transition_info": dict(transition.info),
        "post_state_fingerprints": post_state,
        "post_public_projection": post_public,
        "post_legal_action_occurrences": post_actions,
    }


def capture_source_history() -> dict[str, Any]:
    lock, config = verify_lock()
    attempt_key = "source-history-capture-173730001"
    attempt_path = OUT / "attempts" / f"{attempt_key}.json"
    if attempt_path.exists():
        prior = json.loads(attempt_path.read_text(encoding="utf-8"))
        if prior.get("status") == "COMPLETE":
            saved = read_json(OUT / "reports" / "source-history-capture-report.json")
            if saved is None:
                raise QualificationFailure("completed source-history attempt has no durable report")
            return saved
    prior = start_attempt(attempt_key, kind="historical_debug_seed_complete_action_step_history_capture", reserve=60.0, native_gate_bc=True)
    install_process_cpu_ceiling(60.0)
    phase_start = _PROCESS_START_CPU
    try:
        verify_locked_external_sources()
        env, module, encoder, runtime = runner._setup(OUT)
        model_path = HISTORICAL / "policies" / "pi_init.pt"
        model, model_digest = runner._load_policy(model_path, initial=False)
        adapter = runner.native_adapter(module, SOURCE_QUAL_SEED)
        try:
            result = runner._run_one(
                out=OUT, module=module, model=model, model_digest=model_digest,
                seed=SOURCE_QUAL_SEED, arm="P0", encoder=encoder,
                capture_checkpoints=True, capture_all_roots=True,
                purpose="issue73-source-qualification-exact-debug-owner-replay",
                resource_label=f"SOURCE_QUAL_{SOURCE_QUAL_SEED}",
                adapter_override=adapter, durable=False, capture_trace=False,
                capture_action_history=True,
            )
            roots = list(result.get("_volatile_roots", []))
            history = list(result.get("_actual_action_history", []))
            if len(history) != int(result.get("completed_transitions", -1)):
                raise QualificationFailure("history does not contain one actual legal action/step receipt per native transition")
            report = compare_capture_run(result, roots, history)
            history_path = OUT / "histories" / "actual-action-step-history.json"
            runner.atomic_json(history_path, {
                "schema_id": "issue73-actual-action-step-history-v1",
                "source_lock_sha256": runner.sha256_text_file(LOCK_PATH),
                "owner_seed": SOURCE_QUAL_SEED,
                "role": "historical debug owner provenance/prefix replay only; not a new owner",
                "source_identity": env["identity"],
                "runtime_identity": runtime,
                "event_count": len(history),
                "events": history,
            })
            root_by_id = {stage0.root_id(root): root for root in roots}
            transition_receipts: dict[str, Any] = {}
            for row in config["gate_c_transition_pairs"]:
                rid, index = row["root_id"], int(row["candidate_index"])
                root = root_by_id.get(rid)
                if root is None:
                    raise QualificationFailure(f"locked live root was absent from deterministic owner replay: {rid}")
                transition_receipts[f"{rid}-a{index}"] = capture_transition_witness(adapter, module, root, index)
            map_root = root_by_id[GATE_B_ROOTS["hidden_semantic"]]
            chest_root = root_by_id[GATE_B_ROOTS["native_rng_consequence"]]
            post_root = root_by_id[GATE_B_ROOTS["post_two_third_family"]]
            hidden_negative = stage0.hidden_public_negative(module, map_root)
            chest_source = stage0.native_chest_rng_source_evidence()
            gate_b = {
                "schema_id": "issue73-replay-qualified-gate-b-v1",
                "witness_root_ids": dict(GATE_B_ROOTS),
                "distinct_screen_families": sorted({str(map_root["screen"]), str(chest_root["screen"]), str(post_root["screen"])}),
                "hidden_semantic": {
                    "status": "PASS" if stage0.root_has_hidden_semantic(compact_root(root_receipt(GATE_B_ROOTS["hidden_semantic"]))) and hidden_negative.get("status") == "PASS" else "FAIL",
                    "source_tags": root_receipt(GATE_B_ROOTS["hidden_semantic"])["source_tag_mapping"],
                    "public_negative_test": hidden_negative,
                    "transition_witnesses": {key: value for key, value in transition_receipts.items() if key.startswith(GATE_B_ROOTS["hidden_semantic"])},
                },
                "native_rng_consequence": {
                    "status": "PASS" if stage0.root_has_rng_consequence(compact_root(root_receipt(GATE_B_ROOTS["native_rng_consequence"]))) and chest_source.get("status") == "PASS" else "FAIL",
                    "source_evidence": chest_source,
                    "unavailable_loot_tags": root_receipt(GATE_B_ROOTS["native_rng_consequence"])["source_tag_mapping"],
                    "forced_action_index": int(config["gate_b_witness_candidate_indices"]["native_rng_consequence"]),
                    "transition_witnesses": {key: value for key, value in transition_receipts.items() if key.startswith(GATE_B_ROOTS["native_rng_consequence"])},
                    "revealed_selected_consequence": {
                        "transition_info": transition_receipts[f"{GATE_B_ROOTS['native_rng_consequence']}-a{config['gate_b_witness_candidate_indices']['native_rng_consequence']}"]["transition"]["transition_info"],
                        "post_public_projection": transition_receipts[f"{GATE_B_ROOTS['native_rng_consequence']}-a{config['gate_b_witness_candidate_indices']['native_rng_consequence']}"]["transition"]["post_public_projection"],
                        "post_legal_action_occurrences": transition_receipts[f"{GATE_B_ROOTS['native_rng_consequence']}-a{config['gate_b_witness_candidate_indices']['native_rng_consequence']}"]["transition"]["post_legal_action_occurrences"],
                    },
                    "claim_limit": "one exact treasureRng consequence replayed across processes; no distribution or conditional-return claim",
                },
                "post_two_third_family": {
                    "status": "PASS" if stage0.root_after_two_third_family(compact_root(root_receipt(GATE_B_ROOTS["post_two_third_family"]))) else "FAIL",
                    "transition_witnesses": {key: value for key, value in transition_receipts.items() if key.startswith(GATE_B_ROOTS["post_two_third_family"])},
                },
                "native_rng_byte_state_exposed": False,
                "native_rng_parity_method": "exact reset+prefix replay plus identical forced transition and post-step observation/public output; includes pinned treasureRng chest generation",
                "normal_public_api_receives_oracle_only_values": hidden_negative.get("status") == "PASS",
                "formal_seed_consumed": False,
            }
            report["transition_witnesses"] = transition_receipts
            report["gate_b"] = gate_b
            report["source_identity"] = env["identity"]
            report["runtime_identity"] = runtime
            report["history_sha256"] = runner.sha256_text_file(history_path)
            report["history_bytes"] = history_path.stat().st_size
            report["all_locked_transition_witnesses_repeated_equal"] = all(row.get("status") == "PASS" for row in transition_receipts.values())
            report["status"] = "PASS" if report["status"] == "PASS" and report["all_locked_transition_witnesses_repeated_equal"] and all(gate_b[name]["status"] == "PASS" for name in GATE_B_ROOTS) else "FAIL_SOURCE_HISTORY_OR_GATE_B_CAPTURE"
            runner.atomic_json(OUT / "reports" / "source-history-capture-report.json", report)
        finally:
            adapter.close()
    except BaseException as exc:
        failure = {
            "schema_id": "issue73-source-history-capture-failure-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1200],
            "formal_seed_consumed": False,
            "no_retry": True,
        }
        runner.atomic_json(OUT / "reports" / "source-history-capture-failure.json", failure)
        finish_attempt(attempt_key, started_cpu=phase_start, status="FAIL_OR_INCOMPLETE", details=failure)
        raise
    finished = finish_attempt(attempt_key, started_cpu=phase_start, status="COMPLETE" if report["status"] == "PASS" else "FAIL_OR_INCOMPLETE", details={"report_status": report["status"], "history_event_count": len(history), "formal_seed_consumed": False})
    if finished.get("status") != "COMPLETE":
        report["status"] = "FAIL_CPU_RESERVATION_EXCEEDED"
    report["attempt_receipt"] = finished
    runner.atomic_json(OUT / "reports" / "source-history-capture-report.json", report)
    push_scoped_commit("Issue 73: preserve source-history capture report and resource receipt")
    return report


def fresh_process_prefix_parity() -> dict[str, Any]:
    lock, config = verify_lock()
    capture_report_path = OUT / "reports" / "source-history-capture-report.json"
    if not capture_report_path.exists():
        raise QualificationFailure("source history capture report is missing")
    capture_report = json.loads(capture_report_path.read_text(encoding="utf-8"))
    if capture_report.get("status") != "PASS":
        raise QualificationFailure("source history capture failed; fresh-process parity must not run")
    attempt_key = "fresh-process-prefix-and-transition-parity"
    attempt_path = OUT / "attempts" / f"{attempt_key}.json"
    if attempt_path.exists():
        prior = json.loads(attempt_path.read_text(encoding="utf-8"))
        if prior.get("status") == "PASS":
            saved = read_json(OUT / "reports" / "fresh-process-prefix-parity-report.json")
            if saved is None:
                raise QualificationFailure("completed prefix-parity attempt has no durable report")
            return saved
    start_attempt(attempt_key, kind="fresh_process_complete_prefix_replay_and_gate_b_parity", reserve=80.0, native_gate_bc=True)
    install_process_cpu_ceiling(80.0)
    phase_start = _PROCESS_START_CPU
    try:
        verify_locked_external_sources()
        env, module, encoder, runtime = runner._setup(OUT)
        history = load_history()
        root_values = {rid: root_receipt(rid) for rid in REQUIRED_REPLAY_ROOTS}
        first_process_witnesses = capture_report["transition_witnesses"]
        adapter = runner.native_adapter(module, SOURCE_QUAL_SEED)
        results: dict[str, Any] = {}
        try:
            for row in config["gate_c_transition_pairs"]:
                rid, index = row["root_id"], int(row["candidate_index"])
                root = root_values[rid]
                key = f"{rid}-a{index}"
                original = first_process_witnesses.get(key)
                if not original or original.get("status") != "PASS":
                    raise QualificationFailure(f"same-owner checkpoint transition witness is missing: {key}")
                first, second = replay_forced_transition(adapter, history, root, index, module), replay_forced_transition(adapter, history, root, index, module)
                if runner.canonical(first) != runner.canonical(second):
                    raise QualificationFailure(f"fresh-process repeated prefix/action transition differs: {key}")
                if runner.canonical(first) != runner.canonical(original["transition"]):
                    raise QualificationFailure(f"fresh-process prefix/action/post-step differs from original live root: {key}")
                results[key] = {
                    "status": "PASS",
                    "root_id": rid,
                    "candidate_index": index,
                    "action_identity": root["legal_action_occurrences"][index],
                    "purpose": row["purposes"],
                    "same_process_repeat_equal": True,
                    "cross_process_full_transition_equal": True,
                    "transition": first,
                }
            gate_b = capture_report["gate_b"]
            if len(gate_b.get("distinct_screen_families", [])) < 3:
                raise QualificationFailure("Gate B fixed three witness roots do not span three screen families")
            status = "PASS" if len(results) == len(config["gate_c_transition_pairs"]) else "FAIL_TRANSITION_SET_INCOMPLETE"
            report = {
                "schema_id": "issue73-fresh-process-prefix-parity-v1",
                "status": status,
                "fresh_process": True,
                "source_identity": env["identity"],
                "runtime_identity": runtime,
                "history_sha256": runner.sha256_text_file(OUT / "histories" / "actual-action-step-history.json"),
                "replayed_transition_count": len(results),
                "witnesses": results,
                "gate_b_source_capture": gate_b,
                "native_rng_byte_state_exposed": False,
                "rng_claim": "The pinned API exposes no native RNG bytes/counters. Evidence is every-step ordered-action/full-state/public-transition replay parity, repeated same-process forced transitions, and exact cross-process post-step equality including treasureRng-dependent chest generation; no hidden-game-future distribution claim.",
                "formal_seed_consumed": False,
            }
            runner.atomic_json(OUT / "reports" / "fresh-process-prefix-parity-report.json", report)
        finally:
            adapter.close()
    except BaseException as exc:
        failure = {"schema_id": "issue73-fresh-process-prefix-parity-failure-v1", "status": "FAIL_OR_INCOMPLETE", "failure_type": type(exc).__name__, "failure": str(exc)[:1200], "no_retry": True, "formal_seed_consumed": False}
        runner.atomic_json(OUT / "reports" / "fresh-process-prefix-parity-failure.json", failure)
        finish_attempt(attempt_key, started_cpu=phase_start, status="FAIL_OR_INCOMPLETE", details=failure)
        raise
    finished = finish_attempt(attempt_key, started_cpu=phase_start, status=status, details={"report_status": report["status"], "replayed_transition_count": len(results), "formal_seed_consumed": False})
    if finished.get("status") != "PASS":
        report["status"] = "FAIL_CPU_RESERVATION_EXCEEDED"
    report["attempt_receipt"] = finished
    runner.atomic_json(OUT / "reports" / "fresh-process-prefix-parity-report.json", report)
    push_scoped_commit("Issue 73: preserve fresh-process parity report and resource receipt")
    return report


def load_fixed_gate_a_roots() -> list[dict[str, Any]]:
    _, config = verify_lock()
    root_values = [root_receipt(rid) for rid in config["gate_a_root_ids"]]
    rows = [compact_root(value) for value in root_values]
    if [row["root_id"] for row in rows] != config["gate_a_root_ids"]:
        raise QualificationFailure("fixed Gate A corpus order changed")
    return rows


def run_gate_a_locked() -> dict[str, Any]:
    lock, config = verify_lock()
    parity_path = OUT / "reports" / "fresh-process-prefix-parity-report.json"
    if not parity_path.exists() or json.loads(parity_path.read_text(encoding="utf-8")).get("status") != "PASS":
        raise QualificationFailure("Gate A/B/C execution requires complete fresh-process checkpoint parity PASS")
    attempt_key = "gate-a-corrected-fixed-24-root-policy-response"
    attempt_path = OUT / "attempts" / f"{attempt_key}.json"
    if attempt_path.exists():
        prior = json.loads(attempt_path.read_text(encoding="utf-8"))
        if prior.get("status") == "COMPLETE":
            saved = read_json(OUT / "reports" / "gate-a-source-qualification-report.json")
            if saved is None:
                raise QualificationFailure("completed Gate A attempt has no durable report")
            return saved
    start_attempt(attempt_key, kind="gate_a_fixed_source_policy_response", reserve=120.0, native_gate_bc=False)
    install_process_cpu_ceiling(120.0)
    phase_start = _PROCESS_START_CPU
    try:
        roots = load_fixed_gate_a_roots()
        observed_selected = gate_a.select_corpus(roots)
        if [str(row["root_id"]) for row in observed_selected] != config["gate_a_root_ids"]:
            raise QualificationFailure("Gate A corrected selection differs from the precommitted 24-root corpus")
        report = gate_a.run_gate_a(roots, OUT)
        expected_min = math.ceil(len(config["gate_a_root_ids"]) * 0.8)
        report["precommitted_root_ids"] = config["gate_a_root_ids"]
        report["required_joint_target_rise_and_tv_count"] = expected_min
        report["source_qualification_lock_sha256"] = runner.sha256_text_file(LOCK_PATH)
        report["status"] = report.get("status", "FAIL_MISSING_STATUS")
        if report.get("root_count") != 24 or report.get("screen_family_count", 0) < 4 or report.get("fixed_encoder", {}).get("joint_target_rise_and_tv_at_least_005_count", 0) < expected_min:
            report["status"] = "FAIL_FIXED_POLICY_RESPONSE_THRESHOLD"
        report["formal_seed_consumed"] = False
        runner.atomic_json(OUT / "reports" / "gate-a-source-qualification-report.json", report)
    except BaseException as exc:
        failure = {"schema_id": "issue73-gate-a-source-qualification-failure-v1", "status": "FAIL_OR_INCOMPLETE", "failure_type": type(exc).__name__, "failure": str(exc)[:1200], "no_retry": True, "formal_seed_consumed": False}
        runner.atomic_json(OUT / "reports" / "gate-a-source-qualification-failure.json", failure)
        finish_attempt(attempt_key, started_cpu=phase_start, status="FAIL_OR_INCOMPLETE", details=failure)
        raise
    finished = finish_attempt(attempt_key, started_cpu=phase_start, status="COMPLETE" if report["status"] == "PASS" else "FAIL_OR_INCOMPLETE", details={"gate_status": report["status"], "formal_seed_consumed": False})
    if finished.get("status") != "COMPLETE":
        report["status"] = "FAIL_CPU_RESERVATION_EXCEEDED"
    report["attempt_receipt"] = finished
    runner.atomic_json(OUT / "reports" / "gate-a-source-qualification-report.json", report)
    push_scoped_commit("Issue 73: preserve Gate A report and resource receipt")
    return report


class ReplayPrefixAdapter:
    """Recreate an old native root by replaying the source-locked real prefix."""

    def __init__(self, base: Any, history: Sequence[Mapping[str, Any]], root: Mapping[str, Any], module: Any) -> None:
        self.base = base
        self.history = history
        self.root = root
        self.module = module

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)

    def reset(self, seed: int | None = None) -> Any:
        if seed is not None and int(seed) != int(self.root["owner_seed"]):
            raise QualificationFailure("suffix runner requested a seed outside the fixed replay owner")
        snapshot, _, _ = replay_prefix_to_root(self.base, self.history, self.root, self.module)
        return snapshot


def setup_gate_c() -> tuple[dict[str, Any], dict[str, Any], Any, Any, Any, str, list[dict[str, Any]]]:
    lock, config = verify_lock()
    parity_path = OUT / "reports" / "fresh-process-prefix-parity-report.json"
    gate_a_path = OUT / "reports" / "gate-a-source-qualification-report.json"
    if not parity_path.exists() or json.loads(parity_path.read_text(encoding="utf-8")).get("status") != "PASS":
        raise QualificationFailure("Gate C blocked before source replay parity PASS")
    if not gate_a_path.exists() or json.loads(gate_a_path.read_text(encoding="utf-8")).get("status") != "PASS":
        raise QualificationFailure("Gate C blocked before corrected Gate A PASS")
    prior_setups = [row for row in ledger()["attempts"] if row.get("kind") == "gate_c_environment_setup"]
    for previous in prior_setups:
        if previous.get("status") == "STARTED":
            # Setup has no owner/seed outcome. Preserve the interrupted
            # reservation, then create a separately charged setup attempt.
            try:
                start_attempt(str(previous["key"]), kind="gate_c_environment_setup", reserve=float(previous["reserved_cpu_seconds"]), native_gate_bc=True)
            except QualificationFailure as exc:
                if "was interrupted" not in str(exc):
                    raise
        elif previous.get("status") not in {"COMPLETE", "INTERRUPTED_NO_RERUN"}:
            raise QualificationFailure(f"prior Gate C environment setup is terminal: {previous.get('status')}")
    setup_key = f"gate-c-environment-and-policy-setup-{len(prior_setups) + 1:02d}"
    start_attempt(setup_key, kind="gate_c_environment_setup", reserve=20.0, native_gate_bc=True)
    setup_start = _PROCESS_START_CPU
    setup_report_path = OUT / "reports" / f"{setup_key}.json"
    setup_failure_path = OUT / "reports" / f"{setup_key}-failure.json"
    try:
        verify_locked_external_sources()
        env, module, encoder, runtime = runner._setup(OUT)
        model_path = HISTORICAL / "policies" / "pi_init.pt"
        model, model_digest = runner._load_policy(model_path, initial=False)
        history = load_history()
        setup_report = {
            "schema_id": "issue73-gate-c-replay-setup-v1",
            "status": "READY",
            "source_identity": env["identity"],
            "runtime_identity": runtime,
            "policy_artifact_sha256": runner.sha256_file(model_path),
            "policy_tensor_digest": model_digest,
            "source_history_sha256": runner.sha256_text_file(OUT / "histories" / "actual-action-step-history.json"),
            "gate_c_plan_sha256": runner.sha256_text_file(PLAN_PATH),
            "suffix_policy": config["suffixed_policy"],
            "native_rng_resampling": "none",
            "formal_seed_consumed": False,
        }
        runner.atomic_json(setup_report_path, setup_report)
    except BaseException as exc:
        failure = {"schema_id": "issue73-gate-c-setup-failure-v1", "status": "FAIL_OR_INCOMPLETE", "failure_type": type(exc).__name__, "failure": str(exc)[:1200], "no_retry": True}
        runner.atomic_json(setup_failure_path, failure)
        finish_attempt(setup_key, started_cpu=setup_start, status="FAIL_OR_INCOMPLETE", details=failure)
        raise
    finished = finish_attempt(setup_key, started_cpu=setup_start, status="COMPLETE", details={"setup_status": "READY", "formal_seed_consumed": False})
    setup_report["attempt_receipt"] = finished
    if finished.get("status") != "COMPLETE":
        setup_report["status"] = "FAIL_CPU_RESERVATION_EXCEEDED"
        runner.atomic_json(setup_report_path, setup_report)
        push_scoped_commit("Issue 73: preserve Gate C setup resource-cap failure")
        raise QualificationFailure("Gate C setup exceeded its precommitted process CPU reservation")
    runner.atomic_json(setup_report_path, setup_report)
    push_scoped_commit("Issue 73: preserve Gate C setup receipt")
    return lock, config, module, encoder, model, model_digest, history


def gate_c_suffixes() -> dict[str, Any]:
    pre_lock, pre_config = verify_lock()
    pre_plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    all_fixed_attempts_complete = True
    for stream in pre_plan["policy_rng_seed_streams"]:
        key = f"gate-c-{stream['root_id']}-a{stream['candidate_index']}-r{int(stream['replicate']):02d}"
        prior_path = OUT / "attempts" / f"{key}.json"
        if not prior_path.exists():
            all_fixed_attempts_complete = False
            continue
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
        if prior.get("status") == "STARTED":
            start_attempt(key, kind="gate_c_terminal_suffix_via_prefix_replay", reserve=20.0, native_gate_bc=True)
        if prior.get("status") not in {"COMPLETE"}:
            raise QualificationFailure(f"fixed Gate C attempt {key} is terminal/incomplete; no retry allowed")
        if not (OUT / "runs" / f"{key}.json").is_file():
            raise QualificationFailure(f"completed fixed Gate C attempt {key} has no terminal run receipt")
        existing_run = json.loads((OUT / "runs" / f"{key}.json").read_text(encoding="utf-8"))
        if existing_run.get("terminal_complete") is not True:
            raise QualificationFailure(f"completed fixed Gate C attempt {key} lacks terminal evidence")
    if all_fixed_attempts_complete and len(pre_plan["policy_rng_seed_streams"]) != 64:
        raise QualificationFailure("fixed Gate C completion count is not the registered 64 suffixes")
    if all_fixed_attempts_complete:
        lock, config, module, encoder, model, model_digest, history = pre_lock, pre_config, None, None, None, "", []
    else:
        current_total, current_native = charged(ledger())
        remaining = min(4800.0 - current_total, 2400.0 - current_native)
        install_process_cpu_ceiling(remaining)
        lock, config, module, encoder, model, model_digest, history = setup_gate_c()
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    roots = {rid: root_receipt(rid) for rid in GATE_C_ROOTS}
    attempts = OUT / "attempts"
    runs_dir = OUT / "runs"
    observations: list[dict[str, Any]] = []
    groups: dict[str, dict[int, list[float]]] = {}
    for stream in plan["policy_rng_seed_streams"]:
        key = f"gate-c-{stream['root_id']}-a{stream['candidate_index']}-r{int(stream['replicate']):02d}"
        old_path = attempts / f"{key}.json"
        if old_path.exists():
            old = json.loads(old_path.read_text(encoding="utf-8"))
            if old.get("status") == "COMPLETE":
                run_path = runs_dir / f"{key}.json"
                if not run_path.exists():
                    raise QualificationFailure(f"completed suffix {key} has no terminal run receipt")
                run = json.loads(run_path.read_text(encoding="utf-8"))
                if run.get("terminal_complete") is not True:
                    raise QualificationFailure(f"completed suffix {key} lacks genuine terminal evidence")
                observation = run["observation"]
                observations.append(observation)
                groups.setdefault(stream["root_id"], {}).setdefault(int(stream["candidate_index"]), []).append(float(observation["terminal_floor"]))
                continue
            raise QualificationFailure(f"fixed Gate C attempt {key} is terminal/incomplete; no retry allowed")
        attempt_start = process_tree_cpu()
        start_attempt(key, kind="gate_c_terminal_suffix_via_prefix_replay", reserve=20.0, native_gate_bc=True)
        try:
            root = roots[str(stream["root_id"])]
            fresh = runner.native_adapter(module, int(root["owner_seed"]))
            wrapped = ReplayPrefixAdapter(fresh, history, root, module)
            forced = {
                "root_fingerprints": dict(root["root_fingerprints"]),
                "screen": str(root["screen"]),
                "legal_action_identities": root["legal_action_occurrences"],
                "candidate_index": int(stream["candidate_index"]),
            }
            try:
                result = runner._run_one(
                    out=OUT, module=module, model=model, model_digest=model_digest,
                    seed=int(root["owner_seed"]), arm="SUFFIX", encoder=encoder,
                    purpose="issue73-gatec-replay-qualified-independent-policy-suffix",
                    resource_label=key, adapter_override=wrapped, forced_root=forced,
                    capture_trace=False, durable=False,
                    suffix_random_seed=int(stream["suffix_policy_seed"]),
                    expert_random_seed=int(stream["expert_seed"]),
                    cpu_cap_seconds=float(OLD_CONFIG["stage0"]["gate_c"]["per_suffix_cpu_cap_seconds"]),
                )
            finally:
                fresh.close()
            if result.get("terminal_complete") is not True or result.get("completed_native_terminal") is not True:
                raise QualificationFailure("prefix-replayed suffix did not reach a genuine native terminal")
            forced_root_receipt = result.get("forced_root_receipt") or {}
            if forced_root_receipt.get("forced_root_consumed") is not True or forced_root_receipt.get("candidate_index") != int(stream["candidate_index"]):
                raise QualificationFailure("suffix did not force the exact precommitted legal root occurrence")
            observation = {
                "run_id": key,
                "root_id": str(stream["root_id"]),
                "candidate_index": int(stream["candidate_index"]),
                "candidate_identity": root["legal_action_occurrences"][int(stream["candidate_index"])],
                "replicate": int(stream["replicate"]),
                "suffix_policy_seed": int(stream["suffix_policy_seed"]),
                "expert_seed": int(stream["expert_seed"]),
                "terminal": result["terminal"],
                "terminal_floor": float(result["terminal"]["terminal_floor"]),
                "completed_transitions": int(result["completed_transitions"]),
                "cpu_seconds": float(result["cpu_seconds"]),
                "wall_seconds": float(result["wall_seconds"]),
                "max_rss_kib_sampled_for_run": int(result.get("max_rss_kib_sampled_for_run", 0)),
                "source_history_prefix_root_id": str(stream["root_id"]),
                "native_game_rng_resampled": False,
            }
            run_record = {
                "schema_id": "issue73-gate-c-replay-terminal-suffix-v1",
                "status": "COMPLETE",
                "terminal_complete": True,
                "observation": observation,
                "forced_root_receipt": forced_root_receipt,
                "controller_provenance": result.get("controller_provenance"),
                "screen_counts": result.get("screen_counts"),
                "action_kind_counts": result.get("action_kind_counts"),
                "exposures": result.get("exposures"),
                "terminal": result.get("terminal"),
            }
            runner.atomic_json(runs_dir / f"{key}.json", run_record)
            observations.append(observation)
            groups.setdefault(stream["root_id"], {}).setdefault(int(stream["candidate_index"]), []).append(observation["terminal_floor"])
        except BaseException as exc:
            failure = {
                "schema_id": "issue73-gate-c-suffix-failure-v1",
                "status": "FAIL_OR_INCOMPLETE_NO_REPLACEMENT",
                "failure_type": type(exc).__name__,
                "failure": str(exc)[:1000],
                "root_id": str(stream["root_id"]),
                "candidate_index": int(stream["candidate_index"]),
                "replicate": int(stream["replicate"]),
                "suffix_policy_seed": int(stream["suffix_policy_seed"]),
                "expert_seed": int(stream["expert_seed"]),
                "no_retry": True,
            }
            runner.atomic_json(runs_dir / f"{key}.json", failure)
            finish_attempt(key, started_cpu=attempt_start, status="FAIL_OR_INCOMPLETE", details=failure)
            runner.atomic_json(OUT / "reports" / "gate-c-incomplete-report.json", {
                "schema_id": "issue73-gate-c-incomplete-v1",
                "status": "FAIL_OR_PARTIAL_NO_REPLACEMENTS",
                "planned_suffix_attempt_count": 64,
                "completed_suffix_attempt_count": len(observations),
                "failed_attempt": key,
                "partial_observations": observations,
                "resource_accounting": budget_summary(),
            })
            push_scoped_commit(f"Issue 73: preserve incomplete Gate C suffix {key}")
            raise QualificationFailure(f"Gate C stopped on immutable failure {key}; no replacement") from exc
        finished = finish_attempt(key, started_cpu=attempt_start, status="COMPLETE", details={
            "root_id": str(stream["root_id"]),
            "candidate_index": int(stream["candidate_index"]),
            "replicate": int(stream["replicate"]),
            "terminal_floor": float(observation["terminal_floor"]),
            "formal_seed_consumed": False,
        })
        if finished.get("status") != "COMPLETE":
            runner.atomic_json(OUT / "reports" / "gate-c-incomplete-report.json", {
                "schema_id": "issue73-gate-c-incomplete-v1",
                "status": "FAIL_CPU_RESERVATION_EXCEEDED_NO_REPLACEMENT",
                "planned_suffix_attempt_count": 64,
                "completed_suffix_attempt_count": len(observations),
                "failed_attempt": key,
                "partial_observations": observations,
                "resource_accounting": budget_summary(),
            })
            push_scoped_commit(f"Issue 73: preserve Gate C CPU-cap failure {key}")
            raise QualificationFailure(f"Gate C stopped on fixed suffix CPU cap {key}; no replacement")
    if len(observations) != 64:
        raise QualificationFailure(f"Gate C completed {len(observations)}/64 fixed attempts")
    summaries: dict[str, Any] = {}
    for root_id_value, action_groups in sorted(groups.items()):
        summaries[root_id_value] = stage0.gate_c_uncertainty(action_groups)
    report = {
        "schema_id": "issue73-gate-c-replay-suffix-uncertainty-v1",
        "status": "PASS" if len(summaries) == 4 and all(value.get("status") == "COMPLETE" for value in summaries.values()) else "FAIL_INCOMPLETE_FIXED_GROUPS",
        "planned_suffix_attempt_count": 64,
        "completed_suffix_attempt_count": len(observations),
        "failed_suffix_attempt_count": 0,
        "replay_substitutions": 0,
        "per_root_action_uncertainty": summaries,
        "attempts": observations,
        "native_rng_resampling": "none; deterministic owner seed and full legal step prefix restore exact native RNG state; only independent fixed pi_suffix policy RNG streams resampled",
        "full_native_state_q_estimand": "Q(s_full,a;pi_suffix) conditional on the exact replay-qualified state/RNG root; not public information-set Q",
        "public_information_set_q_estimand": "Q(I_public,a) not estimated without legal hidden-state sampling conditioned on public observations",
        "process_cpu_accounting": budget_summary(),
        "formal_seed_consumed": False,
    }
    runner.atomic_json(OUT / "reports" / "gate-c-replay-uncertainty-report.json", report)
    push_scoped_commit("Issue 73: finalize replay-qualified Gate C uncertainty evidence")
    return report


def budget_summary() -> dict[str, Any]:
    value = ledger()
    total, native = charged(value)
    measured = sum(float(row.get("measured_process_cpu_seconds", 0.0)) for row in value["attempts"])
    return {
        "charged_builder_cpu_seconds_including_previous": total,
        "measured_new_attempt_process_cpu_seconds": measured,
        "builder_ceiling_cpu_seconds": 4800.0,
        "remaining_builder_cpu_seconds": max(0.0, 4800.0 - total),
        "charged_native_gate_bc_cpu_seconds_including_previous": native,
        "native_gate_bc_ceiling_cpu_seconds": 2400.0,
        "remaining_native_gate_bc_cpu_seconds": max(0.0, 2400.0 - native),
        "reviewer_reserve_cpu_seconds": 1200.0,
        "reviewer_reserve_released_cpu_seconds": float(value.get("reviewer_reserve_released_cpu_seconds", 0.0)),
        "resource_ledger_sha256": runner.sha256_text_file(OUT / "resource-ledger.json") if (OUT / "resource-ledger.json").exists() else None,
    }


def finalize_report() -> dict[str, Any]:
    _, config = verify_lock()
    parity = read_json(OUT / "reports" / "fresh-process-prefix-parity-report.json", {})
    gate_a_report = read_json(OUT / "reports" / "gate-a-source-qualification-report.json", {})
    gate_c_report = read_json(OUT / "reports" / "gate-c-replay-uncertainty-report.json", {})
    gate_b = read_json(OUT / "reports" / "source-history-capture-report.json", {}).get("gate_b", {})
    all_pass = parity.get("status") == "PASS" and gate_a_report.get("status") == "PASS" and gate_c_report.get("status") == "PASS" and all(gate_b.get(name, {}).get("status") == "PASS" for name in GATE_B_ROOTS)
    report = {
        "schema_id": "issue73-checkpoint-replay-source-qualification-final-v1",
        "status": "STAGE0_SOURCE_QUALIFICATION_PASS_ALL_GATES" if all_pass else "STAGE0_SOURCE_QUALIFICATION_BLOCKED_OR_FAILED",
        "durability_method": "deterministic complete action/step prefix replay in a fresh process; no native API change",
        "source_history_capture_status": read_json(OUT / "reports" / "source-history-capture-report.json", {}).get("status"),
        "fresh_process_parity_status": parity.get("status"),
        "gate_a_status": gate_a_report.get("status"),
        "gate_b_statuses": {name: gate_b.get(name, {}).get("status") for name in GATE_B_ROOTS},
        "gate_c_status": gate_c_report.get("status"),
        "source_lock_sha256": runner.sha256_text_file(LOCK_PATH),
        "qualification_config_sha256": runner.sha256_text_file(CONFIG_PATH),
        "gate_c_plan_sha256": runner.sha256_text_file(PLAN_PATH),
        "resource_accounting": budget_summary(),
        "formal_training_seed_consumed": False,
        "formal_evaluation_seed_consumed": False,
        "stage1_authorized": False,
        "scientific_claim_limit": "native-surrogate privileged state/RNG replay feasibility only; no public-information-set Q, future distribution, or performance claim",
        "proposed_code_disposition": "study-only; no core or simulator merge",
    }
    report["evidence_manifest"] = write_evidence_manifest()
    runner.atomic_json(OUT / "reports" / "checkpoint-replay-source-qualification-final.json", report)
    push_scoped_commit("Issue 73: preserve checkpoint replay source qualification result")
    return report


def write_evidence_manifest() -> dict[str, Any]:
    excluded = {
        "evidence-manifest.json",
        "reports/checkpoint-replay-source-qualification-final.json",
    }
    rows = []
    for path in sorted(p for p in OUT.rglob("*") if p.is_file()):
        relative = path.relative_to(OUT).as_posix()
        if relative in excluded:
            continue
        payload = path.read_bytes()
        if path.suffix.lower() in {".json", ".jsonl"}:
            payload = runner.normalize_text_bytes(payload)
        rows.append({"path": relative, "bytes": len(payload), "sha256": runner.sha256_bytes(payload)})
    manifest = {
        "schema_id": "issue73-checkpoint-replay-evidence-manifest-v1",
        "files": rows,
        "excluded_for_nonrecursive_manifest_reference": sorted(excluded),
        "total_included_bytes": sum(row["bytes"] for row in rows),
    }
    path = OUT / "evidence-manifest.json"
    runner.atomic_json(path, manifest)
    return {
        "path": path.relative_to(STUDY).as_posix(),
        "sha256": runner.sha256_text_file(path),
        "bytes": len(runner.normalize_text_bytes(path.read_bytes())),
        "included_file_count": len(rows),
        "included_bytes": manifest["total_included_bytes"],
        "excluded": sorted(excluded),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare-lock", "capture", "parity", "gate-a", "gate-c", "finalize"))
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if args.command == "prepare-lock":
        lock = prepare_lock()
        print(json.dumps({"status": lock["status"], "config_sha256": lock["qualification_config_sha256"], "gate_c_plan_sha256": lock["gate_c_plan_sha256"], "source_commit": lock["code_source_commit"], "stream_count": 64}, sort_keys=True))
    elif args.command == "capture":
        print(json.dumps(capture_source_history(), sort_keys=True))
    elif args.command == "parity":
        print(json.dumps(fresh_process_prefix_parity(), sort_keys=True))
    elif args.command == "gate-a":
        print(json.dumps(run_gate_a_locked(), sort_keys=True))
    elif args.command == "gate-c":
        print(json.dumps(gate_c_suffixes(), sort_keys=True))
    else:
        print(json.dumps(finalize_report(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
