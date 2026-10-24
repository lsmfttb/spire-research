"""Prospective, source-faithful replay qualification for Issue 73 v9.

This study-local module preserves the v1-v8 artifacts and permits no native
capture until the v9 static lock/runtime verification has passed independent
review at the exact current branch SHA.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest.mock import patch

import numpy as np

import checkpoint_replay_qualification as q
import runner
import source_qualification_v7 as v7
import source_qualification_v8 as v8
import source_qualification_v9 as v9_engineering_1
import source_qualification_v9_engineering_2 as v9_engineering_2
import stage0
import policy


STUDY = q.STUDY
REPOSITORY = q.REPOSITORY
STUDY_RELATIVE = q.STUDY_RELATIVE
V9_PRIOR_OUT = STUDY / "receipts" / "source-qualification-v9"
V9_PRIOR_LOCK_PATH = V9_PRIOR_OUT / "preseed-source-lock-v9.json"
V9_PRIOR_CONFIG_PATH = V9_PRIOR_OUT / "qualification-config-v9.json"
V9_PRIOR_PLAN_PATH = V9_PRIOR_OUT / "plans" / "gate-c-replay-plan-v9.json"
V9_PRIOR_MANIFEST_PATH = V9_PRIOR_OUT / "input-manifest-v9.json"
V9_PRIOR_LEDGER_PATH = V9_PRIOR_OUT / "resource-ledger-v9.json"
V9_PRIOR_LEDGER_SNAPSHOT_PATH = V9_PRIOR_OUT / "ledgers" / "resource-ledger-before-v9.json"
V9_PRIOR_STATIC_PATH = V9_PRIOR_OUT / "attempts" / "builder-prelock-static-verification-v9.json"
V9_PRIOR_STATIC_REPORT_PATH = V9_PRIOR_OUT / "reports" / "static-qualification-report-v9.json"
V9_PRIOR_STATIC_FAILURE_PATH = V9_PRIOR_OUT / "reports" / "static-qualification-failure-v9.json"
V9_PRIOR_CAPTURE_ATTEMPT_PATH = V9_PRIOR_OUT / "attempts" / "source-history-capture-173730001-prospective-v9.json"
V9_PRIOR_CAPTURE_REPORT_PATH = V9_PRIOR_OUT / "reports" / "source-history-capture-report-v9.json"
V9_PRIOR_CAPTURE_FAILURE_PATH = V9_PRIOR_OUT / "reports" / "source-history-capture-failure-v9.json"
V9_PRIOR_CAPTURE_HISTORY_PATH = V9_PRIOR_OUT / "histories" / "actual-action-step-history-v9.json"
V9_PRIOR_CAPTURE_ROOTS_PATH = V9_PRIOR_OUT / "histories" / "captured-root-snapshots-v9.json"
ENGINEERING_1_OUT = v9_engineering_1.OUT
ENGINEERING_1_CAPTURE_REPORT_PATH = v9_engineering_1.CAPTURE_REPORT_PATH
ENGINEERING_1_CAPTURE_HISTORY_PATH = v9_engineering_1.CAPTURE_HISTORY_PATH
ENGINEERING_1_CAPTURE_ROOTS_PATH = v9_engineering_1.CAPTURE_ROOTS_PATH
ENGINEERING_1_LEDGER_PATH = v9_engineering_1.LEDGER_PATH
ENGINEERING_2_LEDGER_PATH = v9_engineering_2.LEDGER_PATH
ENGINEERING_2_LOCK_PATH = v9_engineering_2.LOCK_PATH
ENGINEERING_2_STATIC_PATH = v9_engineering_2.STATIC_PATH
ENGINEERING_2_STATIC_REPORT_PATH = v9_engineering_2.STATIC_REPORT_PATH
RUNTIME_TEMP_ROOT = REPOSITORY / ".builder-runtime" / "tmp"
OUT = STUDY / "receipts" / "source-qualification-v9-engineering-3"
OUT_RELATIVE = OUT.relative_to(REPOSITORY).as_posix()
BRANCH = "codex/issue-73-v9-pre-capture"
SOURCE_BASE_COMMIT = "1cd3d886a6d59fffd700567425df2ec471f2253f"
SEED = 173730001
ARM = "P0"
SAMPLING_PURPOSE = "issue73-stage0-debug-owner"
ATTEMPT_KEY = "source-history-capture-173730001-prospective-v9-engineering-3"
PARENT_CAPTURE_ATTEMPT_KEY = v9_engineering_1.ATTEMPT_KEY
ATTEMPT_PATH = OUT / "attempts" / f"{ATTEMPT_KEY}.json"
PARITY_ATTEMPT_KEY = "fresh-process-prefix-and-transition-parity-v9-engineering-3"
PARITY_ATTEMPT_PATH = OUT / "attempts" / f"{PARITY_ATTEMPT_KEY}.json"
PARITY_REPORT_PATH = OUT / "reports" / "fresh-process-prefix-parity-report-v9.json"
PARITY_FAILURE_PATH = OUT / "reports" / "fresh-process-prefix-parity-failure-v9.json"
STATIC_KEY = "builder-prelock-static-verification-v9-engineering-3"
STATIC_PATH = OUT / "attempts" / f"{STATIC_KEY}.json"
STATIC_RESERVE = 60.0
CAPTURE_RESERVE = 60.0
LOCK_PATH = OUT / "preseed-source-lock-v9.json"
CONFIG_PATH = OUT / "qualification-config-v9.json"
PLAN_PATH = OUT / "plans" / "gate-c-replay-plan-v9.json"
MANIFEST_PATH = OUT / "input-manifest-v9.json"
LEDGER_PATH = OUT / "resource-ledger-v9.json"
LEDGER_SNAPSHOT_PATH = OUT / "ledgers" / "resource-ledger-before-v9.json"
STATIC_REPORT_PATH = OUT / "reports" / "static-qualification-report-v9.json"
STATIC_FAILURE_PATH = OUT / "reports" / "static-qualification-failure-v9.json"
CAPTURE_OUT = OUT / "capture-runtime-v9"
CAPTURE_HISTORY_PATH = OUT / "histories" / "actual-action-step-history-v9.json"
CAPTURE_ROOTS_PATH = OUT / "histories" / "captured-root-snapshots-v9.json"
CAPTURE_REPORT_PATH = OUT / "reports" / "source-history-capture-report-v9.json"
CAPTURE_FAILURE_PATH = OUT / "reports" / "source-history-capture-failure-v9.json"
REQUIRED_ROOTS = tuple(q.REQUIRED_REPLAY_ROOTS)
PURPOSE_MODEL_DIGEST = "ed94976872e04eb3746c51a8e6305b40a673d46ca5b7ab24c7b26424723d447d"
MODEL_PATH = q.HISTORICAL / "policies" / "pi_init.pt"
NATIVE_MODULE_PATH = q.V6_CONFIGURED_NATIVE_MODULE_PATH
SOURCE_HASH_NAMES = tuple(sorted(set(v7.SOURCE_HASH_PATHS_V7) | {
    "source_qualification_v8.py",
    "test_source_history_capture_v8.py",
    "source_qualification_v9.py",
    "test_source_history_capture_v9.py",
    "source_qualification_v9_engineering_2.py",
    "test_source_history_capture_v9_engineering_2.py",
    "source_qualification_v9_engineering_3.py",
    "test_source_history_capture_v9_engineering_3.py",
}))


class QualificationV9Failure(RuntimeError):
    pass


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_text_file(path: Path) -> str:
    """Hash Git text content after CRLF-to-LF checkout conversion only."""
    return sha256_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def canonical(value: Any) -> str:
    return runner.canonical(value)


def git(*args: str) -> str:
    return q.git(*args)


def push_v9(message: str) -> str:
    git("add", "--", OUT_RELATIVE)
    staged = [x for x in git("diff", "--cached", "--name-only").splitlines() if x]
    if any(x != OUT_RELATIVE and not x.startswith(f"{OUT_RELATIVE}/") for x in staged):
        raise QualificationV9Failure("refusing to stage or commit files outside the v9 evidence directory")
    command, env = runner.git_invocation(REPOSITORY)
    changed = subprocess.run(
        [*command, "diff", "--cached", "--quiet"], check=False, env=env,
    ).returncode != 0
    if changed:
        git("commit", "-m", message)
    head = git("rev-parse", "HEAD")
    git("push", "origin", f"HEAD:refs/heads/{BRANCH}")
    remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    if not remote or remote[0] != head:
        raise QualificationV9Failure("remote v9 branch does not contain the exact handoff commit")
    return head


def require_current_v9_branch(*, require_pushed: bool = True) -> str:
    head = git("rev-parse", "HEAD")
    branch = git("branch", "--show-current")
    if branch != BRANCH:
        raise QualificationV9Failure(f"expected v9 branch {BRANCH}, found {branch!r}")
    command, env = runner.git_invocation(REPOSITORY)
    if subprocess.run(
        [*command, "merge-base", "--is-ancestor", SOURCE_BASE_COMMIT, head],
        check=False, env=env,
    ).returncode:
        raise QualificationV9Failure("v9 worktree does not preserve the exact authorized 1cd3d886 source lineage")
    if require_pushed:
        remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
        if not remote or remote[0] != head:
            raise QualificationV9Failure("v9 source/evidence must be at the exact pushed issue branch head")
    return head


def derived_policy_seed(study_id: str, seed: int, arm: str, model_digest: str, purpose: str) -> int:
    return policy.derived_seed(study_id, seed, arm, model_digest, purpose)


def original_replay_identity(owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if owner is None:
        owner, _ = q.historical_run_receipts()
    controller = owner.get("controller_provenance")
    config = controller.get("config") if isinstance(controller, Mapping) else None
    if not isinstance(config, Mapping):
        raise QualificationV9Failure("historical owner lacks controller provenance config")
    study_id = str(config.get("study_id", owner.get("study_id", "")))
    seed = int(owner.get("seed", -1))
    arm = str(config.get("arm", owner.get("arm", "")))
    digest = str(config.get("policy_sha256", ""))
    purpose = str(config.get("sampling_purpose", ""))
    return {
        "study_id": study_id,
        "seed": seed,
        "arm": arm,
        "model_digest": digest,
        "sampling_purpose": purpose,
        "policy_rng_seed": derived_policy_seed(study_id, seed, arm, digest, purpose),
    }


def replay_provenance_mismatches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    return [
        key for key in ("study_id", "seed", "arm", "model_digest", "sampling_purpose", "policy_rng_seed")
        if actual.get(key) != expected.get(key)
    ]


def first_trace_difference(expected: Sequence[Any], observed: Sequence[Any]) -> dict[str, Any] | None:
    shared = min(len(expected), len(observed))
    for index in range(shared):
        if canonical(expected[index]) != canonical(observed[index]):
            return {
                "index": index,
                "expected": expected[index],
                "observed": observed[index],
                "reason": "decision payload differs, including selected legal occurrence and source order",
            }
    if len(expected) != len(observed):
        index = shared
        return {
            "index": index,
            "expected": expected[index] if index < len(expected) else None,
            "observed": observed[index] if index < len(observed) else None,
            "reason": "decision trace lengths differ",
        }
    return None


def compare_root_membership(expected_ids: Sequence[str], observed_ids: Sequence[str]) -> dict[str, Any]:
    expected_list = [str(x) for x in expected_ids]
    observed_list = [str(x) for x in observed_ids]
    expected_set = set(expected_list)
    observed_set = set(observed_list)
    duplicate_expected = sorted({x for x in expected_list if expected_list.count(x) > 1})
    duplicate_observed = sorted({x for x in observed_list if observed_list.count(x) > 1})
    return {
        "exact_membership": expected_set == observed_set and not duplicate_expected and not duplicate_observed,
        "expected_root_ids": expected_list,
        "observed_root_ids": observed_list,
        "missing_root_ids": sorted(expected_set - observed_set),
        "unexpected_root_ids": sorted(observed_set - expected_set),
        "duplicate_expected_root_ids": duplicate_expected,
        "duplicate_observed_root_ids": duplicate_observed,
    }


def historical_root_path(root_id: str) -> Path:
    if not re.fullmatch(r"root-173730001-\d{4}", root_id):
        raise QualificationV9Failure(f"unsafe or unexpected historical root id: {root_id!r}")
    return q.root_path(root_id)


def prevalidate_historical_roots(
    expected_ids: Sequence[str],
    locked_sha256: Mapping[str, str],
    *,
    source_commit: str,
    path_resolver: Any = historical_root_path,
) -> dict[str, Any]:
    """Load and hash every named historical multichoice root without throwing on absence."""
    ids = [str(x) for x in expected_ids]
    duplicate_ids = sorted({x for x in ids if ids.count(x) > 1})
    roots: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, str]] = []
    resolved_paths: dict[str, tuple[Path, str]] = {}
    for root_id in ids:
        try:
            path = path_resolver(root_id)
            repository_path = path.resolve().relative_to(REPOSITORY.resolve()).as_posix()
        except (OSError, ValueError, QualificationV9Failure) as exc:
            issues.append({"root_id": root_id, "reason": f"unsafe_root_path:{type(exc).__name__}:{exc}"})
            continue
        expected_hash = locked_sha256.get(repository_path)
        if not expected_hash:
            issues.append({"root_id": root_id, "reason": "root_path_absent_from_locked_input_manifest"})
            continue
        resolved_paths[root_id] = (path, repository_path)
    try:
        blobs = v7.external_git_blobs(source_commit, [relative for _path, relative in resolved_paths.values()])
    except BaseException as exc:
        blobs = {}
        issues.extend({"root_id": root_id, "reason": f"locked_git_blob_unavailable:{type(exc).__name__}:{str(exc)[:300]}"} for root_id in resolved_paths)
    for root_id, (path, repository_path) in resolved_paths.items():
        expected_hash = locked_sha256[repository_path]
        blob = blobs.get(repository_path)
        if blob is None:
            continue
        if sha256_bytes(blob) != expected_hash:
            issues.append({"root_id": root_id, "reason": "root_git_blob_sha256_differs_from_locked_manifest"})
            continue
        try:
            payload = path.read_bytes()
        except OSError as exc:
            issues.append({"root_id": root_id, "reason": f"historical_root_unreadable:{type(exc).__name__}:{exc}"})
            continue
        try:
            normalized = q.verify_locked_source_payload(
                label=f"historical root {root_id}", expected_sha256=expected_hash,
                committed_blob=blob, checkout_bytes=payload,
            )
        except BaseException as exc:
            issues.append({"root_id": root_id, "reason": f"root_checkout_differs_from_locked_blob:{type(exc).__name__}:{str(exc)[:300]}"})
            continue
        try:
            root = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            issues.append({"root_id": root_id, "reason": f"historical_root_json_invalid:{type(exc).__name__}"})
            continue
        if root.get("root_id") != root_id:
            issues.append({"root_id": root_id, "reason": "historical_root_content_id_mismatch"})
            continue
        if root.get("candidate_binding_validation_passed") is not True:
            issues.append({"root_id": root_id, "reason": "historical_root_candidate_binding_not_validated"})
            continue
        roots[root_id] = root
    return {
        "status": "PASS" if not issues and not duplicate_ids and set(roots) == set(ids) else "FAIL",
        "expected_root_ids": ids,
        "loaded_root_ids": sorted(roots),
        "duplicate_root_ids": duplicate_ids,
        "issues": issues,
        "roots": roots,
    }


def compare_capture_run_v9(
    result: Mapping[str, Any],
    roots: Sequence[Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
    *,
    owner: Mapping[str, Any] | None = None,
    owner_attempt: Mapping[str, Any] | None = None,
    historical_roots: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if owner is None or owner_attempt is None:
        default_owner, default_attempt = q.historical_run_receipts()
        owner = owner or default_owner
        owner_attempt = owner_attempt or default_attempt
    old_trace = list(owner.get("decisions", []))
    new_trace = list(result.get("decisions", []))
    trace_difference = first_trace_difference(old_trace, new_trace)
    observed_ids = [stage0.root_id(root) for root in roots]
    membership = compare_root_membership(owner_attempt.get("root_ids", []), observed_ids)
    history_complete = len(history) == int(result.get("completed_transitions", -1)) and all(
        isinstance(row.get("transition"), Mapping) for row in history
    )
    run_parity = {
        "terminal": result.get("terminal") == owner.get("terminal"),
        "terminal_complete": result.get("terminal_complete") is True and owner.get("terminal_complete") is True,
        "completed_transitions": int(result.get("completed_transitions", -1)) == int(owner.get("completed_transitions", -2)) == len(history),
        "run_outcome": result.get("run_outcome") == owner.get("run_outcome"),
        "full_decision_trace": trace_difference is None,
        "screen_counts": result.get("screen_counts") == owner.get("screen_counts"),
        "action_kind_counts": result.get("action_kind_counts") == owner.get("action_kind_counts"),
        "debug_owner_attempt_terminal": owner_attempt.get("terminal") == owner.get("terminal"),
        "debug_owner_attempt_complete": owner_attempt.get("terminal_complete") is True,
        "attempt_transition_count": int(owner_attempt.get("completed_transitions", -1)) == int(owner.get("completed_transitions", -2)),
    }
    expected_roots = set(str(x) for x in owner_attempt.get("root_ids", []))
    # Volatile roots captured by runner use (seed, decision_index) as their
    # identity and do not carry the persisted receipt's root_id field.
    observed_by_id = {stage0.root_id(root): root for root in roots}
    historical = dict(historical_roots or {})
    root_checks: dict[str, Any] = {}
    for root_id in sorted(expected_roots | set(observed_by_id)):
        root = observed_by_id.get(root_id)
        old = historical.get(root_id)
        if root is None:
            root_checks[root_id] = {"status": "MISSING_FROM_CAPTURE", "all_fields_match": False}
            continue
        if old is None:
            root_checks[root_id] = {"status": "UNEXPECTED_CAPTURE_ROOT_NO_HISTORICAL_LOOKUP", "all_fields_match": False}
            continue
        try:
            matrix_sha = runner.sha256_bytes(np.asarray(root["feature_matrix"], dtype="<f4").tobytes())
            fields = {
                "owner_root_index": int(root["root_index"]) == int(old["owner_root_index"]),
                "owner_decision_index": int(root["decision_index"]) == int(old["owner_decision_index"]),
                "policy_decision_index": int(root["policy_decision_index"]) == int(old["policy_decision_index"]),
                "screen": str(root["screen"]) == str(old["screen"]),
                "native_state_sha256": root["native_state_sha256"] == old["root_fingerprints"]["native_state_sha256"],
                "public_projection_sha256": root["public_projection_sha256"] == old["root_fingerprints"]["public_projection_sha256"],
                "public_resource_state_sha256": root["public_resource_state_sha256"] == old["root_fingerprints"]["public_resource_state_sha256"],
                "selected_legal_occurrence_order": canonical(root["legal_action_identities"]) == canonical(old["legal_action_occurrences"]),
                "raw_privileged_projection": canonical(root["projection"]) == canonical(old["raw_privileged_projection"]),
                "sanitized_public_projection": canonical(root["public_projection"]) == canonical(old["sanitized_public_projection"]),
                "projection_digest": root["projection_digest"] == old["projection_digest"],
                "semantic_feature_matrix_sha256": matrix_sha == old["feature_matrix_sha256"],
                "candidate_binding_validation": bool(root.get("live_binding_validation_passed")) == bool(old["candidate_binding_validation_passed"]),
            }
            root_checks[root_id] = {"status": "MATCH" if all(fields.values()) else "MISMATCH", "all_fields_match": all(fields.values()), "fields": fields}
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            root_checks[root_id] = {"status": "MALFORMED_CAPTURE_ROOT", "all_fields_match": False, "failure": f"{type(exc).__name__}: {str(exc)[:300]}"}
    root_fields_match = membership["exact_membership"] and all(
        root_checks.get(root_id, {}).get("all_fields_match") is True for root_id in expected_roots
    ) and all(root_id in expected_roots for root_id in observed_by_id)
    provenance = original_replay_identity(owner)
    report = {
        "schema_id": "issue73-source-history-capture-v9",
        "status": "PASS" if all(run_parity.values()) and membership["exact_membership"] and history_complete and root_fields_match else "FAIL_SOURCE_HISTORY_CAPTURE_PARITY",
        "replayed_seed": SEED,
        "historical_owner_treatment": "exact source-provenance and trajectory replay only; not a new owner",
        "replay_provenance": provenance,
        "run_parity": run_parity,
        "first_trace_difference": trace_difference,
        "root_membership": membership,
        "history_event_count": len(history),
        "history_transition_complete": history_complete,
        "captured_root_count": len(roots),
        "historical_root_match_count": sum(row.get("all_fields_match") is True for row in root_checks.values()),
        "required_replay_root_ids": list(REQUIRED_ROOTS),
        "required_roots_match": all(root_checks.get(root_id, {}).get("all_fields_match") is True for root_id in REQUIRED_ROOTS),
        "roots": root_checks,
        "single_action_decision_gap": {
            "decision_12": "SHOP_ROOM / single_action; no root receipt expected",
            "decision_13": "MAP_SCREEN / single_action; no root receipt expected",
            "root_sequence": "0011 -> 0014 is valid; root-0012/root-0013 must not be synthesized",
        },
        "native_rng_state_bytes_or_counters_available": False,
        "no_formal_seed_consumed": True,
    }
    if not report["required_roots_match"]:
        report["status"] = "FAIL_REQUIRED_REPLAY_ROOT_MISMATCH"
    return report


def transition_witness_integrity_v9(
    witnesses: Mapping[str, Any], pairs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    expected = {
        f"{row['root_id']}-a{int(row['candidate_index'])}"
        for row in pairs
    }
    observed = {str(key) for key in witnesses}
    invalid = sorted(
        key for key in expected & observed
        if not isinstance(witnesses.get(key), Mapping)
        or witnesses[key].get("status") != "PASS"
        or not isinstance(witnesses[key].get("transition"), Mapping)
    )
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected)
    complete = bool(expected) and not missing and not unexpected and not invalid
    return {
        "schema_id": "issue73-transition-witness-integrity-v1",
        "expected_count": len(expected),
        "observed_count": len(observed),
        "missing_keys": missing,
        "unexpected_keys": unexpected,
        "invalid_keys": invalid,
        "complete_pass": complete,
    }


def apply_transition_witness_gate_v9(
    report: Mapping[str, Any], witnesses: Mapping[str, Any], pairs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result = dict(report)
    integrity = transition_witness_integrity_v9(witnesses, pairs)
    result["transition_witnesses"] = dict(witnesses)
    result["transition_witness_integrity"] = integrity
    if result.get("status") == "PASS" and not integrity["complete_pass"]:
        result["status"] = "FAIL_SOURCE_HISTORY_OR_TRANSITION_WITNESS_CAPTURE"
    return result


def capture_root_snapshots_record_v9(roots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Keep JSON-safe live roots so a later comparator failure is diagnosable."""
    snapshots: list[dict[str, Any]] = []
    for index, root in enumerate(roots):
        # Checkpoints and adapters are live native handles, not comparator
        # inputs. Preserve the volatile root's remaining fields verbatim.
        snapshot = {key: value for key, value in root.items() if key not in {"checkpoint", "adapter"}}
        try:
            snapshot["root_id"] = stage0.root_id(root)
        except (KeyError, TypeError, ValueError) as exc:
            snapshot["root_id"] = None
            snapshot["root_identity_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        snapshot["capture_root_index"] = index
        snapshots.append(q.jsonable(snapshot))
    return {
        "schema_id": "issue73-volatile-root-snapshots-v9-v1",
        "identity": "root_id derived from seed and decision_index; live checkpoint/adapter handles omitted",
        "root_count": len(snapshots),
        "roots": snapshots,
    }


def _source_hashes(source_commit: str) -> dict[str, str]:
    paths = [f"{STUDY_RELATIVE}/{name}" for name in SOURCE_HASH_NAMES]
    blobs = v7.external_git_blobs(source_commit, paths)
    hashes: dict[str, str] = {}
    for name in SOURCE_HASH_NAMES:
        blob = blobs[f"{STUDY_RELATIVE}/{name}"]
        checkout = STUDY / name
        if not checkout.is_file():
            raise QualificationV9Failure(f"v9 locked source file is missing: {name}")
        q.verify_locked_source_payload(
            label=f"study/{name}", expected_sha256=sha256_bytes(blob),
            committed_blob=blob, checkout_bytes=checkout.read_bytes(),
        )
        hashes[name] = sha256_bytes(blob)
    return hashes

def compare_capture_with_durable_roots_v9(
    result: Mapping[str, Any],
    roots: Sequence[Mapping[str, Any]],
    history: Sequence[Any],
    **kwargs: Any,
) -> tuple[dict[str, Any], str]:
    if CAPTURE_ROOTS_PATH.exists():
        raise QualificationV9Failure("v9 volatile-root snapshot path already exists; refuse overwrite")
    runner.atomic_json(CAPTURE_ROOTS_PATH, capture_root_snapshots_record_v9(roots))
    snapshot_sha256 = sha256_text_file(CAPTURE_ROOTS_PATH)
    report = compare_capture_run_v9(
        result, roots, history, **kwargs,
    )
    return report, snapshot_sha256


def capture_root_snapshot_sha256_v9() -> str | None:
    """Return the durable snapshot digest even when comparison raises."""
    try:
        return sha256_text_file(CAPTURE_ROOTS_PATH) if CAPTURE_ROOTS_PATH.is_file() else None
    except OSError:
        return None


def _resource_projection(base_ledger: Mapping[str, Any], v9_ledger: Mapping[str, Any]) -> dict[str, Any]:
    builder_base, native_base = q.charged(base_ledger)
    attempts = list(v9_ledger.get("attempts", []))
    builder = builder_base + sum(float(x.get("cpu_charged_seconds", 0)) for x in attempts)
    native = native_base + sum(
        float(x.get("cpu_charged_seconds", 0)) for x in attempts if x.get("native_gate_bc") is True
    )
    native_contingency_used = sum(
        float(x.get("cpu_charged_seconds", 0)) for x in attempts
        if x.get("contingency_pool") == "native"
    )
    nonnative_contingency_used = sum(
        float(x.get("cpu_charged_seconds", 0)) for x in attempts
        if x.get("contingency_pool") == "nonnative"
    )
    native_contingency_remaining = 240.0 - native_contingency_used
    nonnative_contingency_remaining = 180.0 - nonnative_contingency_used
    if native_contingency_remaining < 0 or nonnative_contingency_remaining < 0:
        raise QualificationV9Failure("v9 engineering contingency was overcharged")
    native_remaining = 3060.0 - native
    capture_already_charged = any(x.get("key") in {PARENT_CAPTURE_ATTEMPT_KEY, ATTEMPT_KEY} for x in attempts)
    parity_already_charged = any(x.get("key") == PARITY_ATTEMPT_KEY for x in attempts)
    fixed_native_future = (0.0 if capture_already_charged else CAPTURE_RESERVE) + (0.0 if parity_already_charged else 80.0) + 20.0 + 64.0 * 20.0
    builder_future = (
        fixed_native_future + 120.0
        + nonnative_contingency_remaining + native_contingency_remaining
    )
    builder_projected = builder + builder_future
    native_projected = native + fixed_native_future + native_contingency_remaining
    return {
        "aggregate_stage0_cpu_seconds": 6000.0,
        "builder_ceiling_cpu_seconds": 4800.0,
        "reviewer_reserve_cpu_seconds": 1200.0,
        "prior_builder_charge_cpu_seconds": builder_base,
        "builder_charge_including_v9_cpu_seconds": builder,
        "prior_native_gate_bc_charge_cpu_seconds": native_base,
        "native_gate_bc_charge_including_v9_cpu_seconds": native,
        "native_gate_bc_remaining_cpu_seconds": native_remaining,
        "v9_static_reservation_cpu_seconds": STATIC_RESERVE,
        "new_capture_reserve_cpu_seconds": CAPTURE_RESERVE,
        "fresh_process_prefix_and_transition_parity_reserve_cpu_seconds": 80.0,
        "gate_c_environment_setup_reserve_cpu_seconds": 20.0,
        "gate_c_suffix_attempt_count": 64,
        "gate_c_per_suffix_process_cpu_cap_seconds": 20.0,
        "gate_c_suffix_total_reserve_cpu_seconds": 1280.0,
        "gate_a_nonnative_process_cpu_cap_seconds": 120.0,
        "native_contingency_authorized_cpu_seconds": 240.0,
        "native_contingency_used_cpu_seconds": native_contingency_used,
        "native_contingency_remaining_cpu_seconds": native_contingency_remaining,
        "nonnative_contingency_authorized_cpu_seconds": 180.0,
        "nonnative_contingency_used_cpu_seconds": nonnative_contingency_used,
        "nonnative_contingency_remaining_cpu_seconds": nonnative_contingency_remaining,
        "projected_new_builder_cpu_reserve_seconds": builder_future,
        "cumulative_builder_projection_cpu_seconds": builder_projected,
        "cumulative_native_gate_bc_projection_cpu_seconds": native_projected,
        "reviewer_reserve_untouched": True,
        "no_prior_charge_reclaimed": True,
        "stage1_authorized": False,
        "forecast_fits_registered_caps": (
            builder_projected <= 4800.0
            and native_projected <= 3060.0
            and builder_projected + 1200.0 <= 6000.0
        ),
    }


def _base_v8_ledger() -> dict[str, Any]:
    required = (
        v8.LOCK_PATH, v8.CONFIG_PATH, v8.PLAN_PATH, v8.MANIFEST_PATH,
        v8.LEDGER_PATH, v8.STATIC_PATH, v8.STATIC_REPORT_PATH,
        v8.ATTEMPT_PATH, v8.CAPTURE_REPORT_PATH,
    )
    if not all(path.is_file() for path in required):
        raise QualificationV9Failure("immutable v8 lock, ledger, reports, or attempt receipt is missing")
    lock = json.loads(v8.LOCK_PATH.read_text(encoding="utf-8"))
    config = json.loads(v8.CONFIG_PATH.read_text(encoding="utf-8"))
    plan = json.loads(v8.PLAN_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(v8.MANIFEST_PATH.read_text(encoding="utf-8"))
    ledger = json.loads(v8.LEDGER_PATH.read_text(encoding="utf-8"))
    static = json.loads(v8.STATIC_PATH.read_text(encoding="utf-8"))
    static_report = json.loads(v8.STATIC_REPORT_PATH.read_text(encoding="utf-8"))
    attempt = json.loads(v8.ATTEMPT_PATH.read_text(encoding="utf-8"))
    capture_report = json.loads(v8.CAPTURE_REPORT_PATH.read_text(encoding="utf-8"))
    if (
        lock.get("schema_id") != "issue73-checkpoint-replay-preseed-source-lock-v8"
        or lock.get("stage1_authorized") is not False
        or lock.get("formal_training_seed_consumed") is not False
        or lock.get("formal_evaluation_seed_consumed") is not False
        or lock.get("qualification_config_sha256") != sha256_text_file(v8.CONFIG_PATH)
        or lock.get("gate_c_plan_sha256") != sha256_text_file(v8.PLAN_PATH)
        or lock.get("historical_input_manifest_sha256") != _manifest_digest(manifest)
        or config.get("historical_input_manifest_sha256") != lock.get("historical_input_manifest_sha256")
        or plan.get("qualification_config_sha256") != lock.get("qualification_config_sha256")
    ):
        raise QualificationV9Failure("immutable v8 source/config/plan/manifest lock no longer verifies")
    if (
        manifest.get("included_file_count") != 345
        or lock.get("native", {}).get("commit") != "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2"
        or lock.get("native", {}).get("loaded_module_sha256") != "bb22332a4a7108e8f9691482df2624b3b132d75ca07e1aa7b394b932193e5cad"
        or lock.get("stsr", {}).get("commit") != "7d172f7c24c9d29429b6946ba44f6b59760b0d96"
    ):
        raise QualificationV9Failure("v8 historical population or external source pins changed")
    if static_report.get("status") != "PASS" or static.get("cpu_charged_seconds") != 60.0:
        raise QualificationV9Failure("v8 static debit/report is not the immutable passing 60 CPU-s receipt")
    if (
        attempt.get("key") != "source-history-capture-173730001-prospective-v8"
        or attempt.get("status") != "FAIL_OR_INCOMPLETE"
        or attempt.get("report_status") != "FAIL_SOURCE_HISTORY_CAPTURE_PARITY"
        or attempt.get("reserved_cpu_seconds") != 60.0
        or attempt.get("cpu_charged_seconds") != 60.0
        or attempt.get("native_gate_bc") is not True
        or attempt.get("formal_seed_consumed") is not False
        or capture_report.get("status") != "FAIL_SOURCE_HISTORY_CAPTURE_PARITY"
        or attempt.get("source_lock_sha256") != sha256_text_file(v8.LOCK_PATH)
    ):
        raise QualificationV9Failure("v8 terminal native failure or its fixed debit changed")
    rows = ledger.get("attempts", [])
    if (
        ledger.get("schema_id") != "issue73-v8-resource-ledger-v1"
        or len(rows) != 2
        or {row.get("key") for row in rows} != {
            "builder-prelock-static-verification-v8",
            "source-history-capture-173730001-prospective-v8",
        }
        or abs(q.charged(ledger)[0] - 2471.610134) > 1e-6
        or abs(q.charged(ledger)[1] - 1380.0) > 1e-6
    ):
        raise QualificationV9Failure("v8 terminal resource ledger does not match its immutable Builder/native debit")
    return ledger


def _engineering_1_ledger_rows() -> list[dict[str, Any]]:
    value = json.loads(ENGINEERING_1_LEDGER_PATH.read_text(encoding="utf-8"))
    return json.loads(json.dumps(value.get("attempts", [])))


def _base_prior_v9_ledger() -> dict[str, Any]:
    """Verify the immutable engineering-2 parent with CRLF/LF-safe JSON identity."""
    # Engineering-2 is an immutable reviewed parent. Its raw hash helper predates
    # the checkout fix; scope normalization to the parent verifier without
    # rewriting its source, lock, receipts, or resource ledger.
    parent_raw_hash = v9_engineering_2.sha256_file
    v9_engineering_2.sha256_file = v9_engineering_2.sha256_text_file
    try:
        parent_lock, _parent_config = v9_engineering_2.verify_lock_v9(require_remote_current=False)
        base = v9_engineering_2._base_prior_v9_ledger()
    finally:
        v9_engineering_2.sha256_file = parent_raw_hash

    parent_ledger = json.loads(ENGINEERING_2_LEDGER_PATH.read_text(encoding="utf-8"))
    parent_static = json.loads(ENGINEERING_2_STATIC_PATH.read_text(encoding="utf-8"))
    parent_report = json.loads(ENGINEERING_2_STATIC_REPORT_PATH.read_text(encoding="utf-8"))
    expected_keys = {
        v9_engineering_1.STATIC_KEY,
        v9_engineering_1.ATTEMPT_KEY,
        v9_engineering_2.STATIC_KEY,
    }
    rows = parent_ledger.get("attempts", [])
    builder_charge, native_charge = q.charged(parent_ledger)
    parent_projection = parent_report.get("resource_projection", {})
    if (
        parent_lock.get("lock_revision") != 12
        or parent_report.get("status") != "PASS"
        or parent_report.get("source_lock_sha256") != sha256_text_file(ENGINEERING_2_LOCK_PATH)
        or parent_static.get("key") != v9_engineering_2.STATIC_KEY
        or parent_static.get("cpu_charged_seconds") != 60.0
        or parent_static.get("status") != "RESERVED_CONSERVATIVE_STATIC_DEBIT"
        or parent_ledger.get("schema_id") != "issue73-v9-engineering-2-resource-ledger-v1"
        or {row.get("key") for row in rows} != expected_keys
        or len(rows) != len(expected_keys)
        or parent_lock.get("v9_resource_ledger_sha256") != sha256_text_file(ENGINEERING_2_LEDGER_PATH)
        or abs(builder_charge - 2711.610134) > 1e-6
        or abs(native_charge - 1440.0) > 1e-6
        or abs(float(parent_projection.get("builder_charge_including_v9_cpu_seconds", -1)) - builder_charge) > 1e-6
        or abs(float(parent_projection.get("native_gate_bc_charge_including_v9_cpu_seconds", -1)) - native_charge) > 1e-6
        or parent_projection.get("nonnative_contingency_remaining_cpu_seconds") != 60.0
        or parent_projection.get("native_contingency_remaining_cpu_seconds") != 240.0
    ):
        raise QualificationV9Failure("immutable v9 engineering-3 PASS, normalized parent hashes, or charged resource ledger is inconsistent")
    return base


def _engineering_2_ledger_rows() -> list[dict[str, Any]]:
    value = json.loads(ENGINEERING_2_LEDGER_PATH.read_text(encoding="utf-8"))
    return json.loads(json.dumps(value.get("attempts", [])))


def _ledger_template(base: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_id": "issue73-v9-engineering-3-resource-ledger-v1",
        "baseline_ledger_path": V9_PRIOR_LEDGER_PATH.relative_to(STUDY).as_posix(),
        "baseline_ledger_sha256": sha256_text_file(V9_PRIOR_LEDGER_PATH),
        "baseline_builder_charge_cpu_seconds": q.charged(base)[0],
        "baseline_native_gate_bc_charge_cpu_seconds": q.charged(base)[1],
        "builder_ceiling_cpu_seconds": 4800.0,
        "native_gate_bc_ceiling_cpu_seconds": 3060.0,
        "reviewer_reserve_cpu_seconds": 1200.0,
        "aggregate_stage0_cpu_seconds": 6000.0,
        "nonnative_engineering_contingency_cpu_seconds": 180.0,
        "native_engineering_contingency_cpu_seconds": 240.0,
        "parent_v9_engineering_1_ledger_sha256": sha256_text_file(ENGINEERING_1_LEDGER_PATH),
        "parent_v9_engineering_2_ledger_sha256": sha256_text_file(ENGINEERING_2_LEDGER_PATH),
        "attempts": _engineering_2_ledger_rows(),
    }

def _ensure_static_reservation(*, allow_started: bool = False) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    base = _base_prior_v9_ledger()
    if not all(path.is_file() for path in (STATIC_PATH, LEDGER_PATH, LEDGER_SNAPSHOT_PATH)):
        raise QualificationV9Failure("v9 fixed 60 CPU-s static reservation is not durably present")
    receipt = json.loads(STATIC_PATH.read_text(encoding="utf-8"))
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    snapshot = json.loads(LEDGER_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    expected_snapshot = _ledger_template(base)
    rows = [row for row in ledger.get("attempts", []) if row.get("key") == STATIC_KEY]
    if (
        snapshot != expected_snapshot
        or ledger.get("schema_id") != "issue73-v9-engineering-3-resource-ledger-v1"
        or ledger.get("baseline_ledger_sha256") != sha256_text_file(V9_PRIOR_LEDGER_PATH)
        or receipt.get("key") != STATIC_KEY
        or receipt.get("reserved_cpu_seconds") != STATIC_RESERVE
        or receipt.get("cpu_charged_seconds") != STATIC_RESERVE
        or receipt.get("native_gate_bc") is not False
        or receipt.get("formal_seed_consumed") is not False
        or rows != [receipt]
    ):
        raise QualificationV9Failure("v9 static reservation, ledger snapshot, or immutable v8 baseline is inconsistent")
    if receipt.get("status") != "RESERVED_CONSERVATIVE_STATIC_DEBIT":
        raise QualificationV9Failure("v9 fixed static reservation receipt changed from its immutable reserved state")
    projection = _resource_projection(base, ledger)
    if not projection["forecast_fits_registered_caps"]:
        raise QualificationV9Failure(f"v9 continuation forecast exceeds the fixed resource contract: {projection}")
    return receipt, ledger, projection


def _reserve_static() -> dict[str, Any]:
    if STATIC_PATH.exists() or LEDGER_PATH.exists() or LEDGER_SNAPSHOT_PATH.exists():
        if all(path.is_file() for path in (STATIC_PATH, LEDGER_PATH, LEDGER_SNAPSHOT_PATH)):
            receipt, _ledger, _projection = _ensure_static_reservation()
            if receipt.get("status") == "RESERVED_CONSERVATIVE_STATIC_DEBIT" and STATIC_REPORT_PATH.is_file() and json.loads(STATIC_REPORT_PATH.read_text(encoding="utf-8")).get("status") == "PASS":
                return receipt
        raise QualificationV9Failure("v9 static reservation already exists or is partial; no second reservation/replay")
    head = require_current_v9_branch()
    base = _base_prior_v9_ledger()
    snapshot = _ledger_template(base)
    projection_ledger = json.loads(json.dumps(snapshot))
    projection_ledger["attempts"].append({"key": STATIC_KEY, "cpu_charged_seconds": STATIC_RESERVE, "native_gate_bc": False, "contingency_pool": "nonnative"})
    projection = _resource_projection(base, projection_ledger)
    if not projection["forecast_fits_registered_caps"]:
        raise QualificationV9Failure(f"v9 fixed 60 CPU-s debit plus registered continuation does not fit: {projection}")
    row = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": STATIC_KEY,
        "kind": "v9_engineering_3_bounded_static_provenance_lock_and_runtime_verification",
        "status": "RESERVED_CONSERVATIVE_STATIC_DEBIT",
        "native_gate_bc": False,
        "contingency_pool": "nonnative",
        "reserved_cpu_seconds": STATIC_RESERVE,
        "cpu_charged_seconds": STATIC_RESERVE,
        "measurement_status": "FIXED_RESERVATION_PRECHARGED; NO REFUND",
        "source_commit": head,
        "source_lock_sha256": None,
        "formal_seed_consumed": False,
        "no_native_attempt_or_seed": True,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ledger = json.loads(json.dumps(snapshot))
    ledger["attempts"].append(row)
    runner.atomic_json(LEDGER_SNAPSHOT_PATH, snapshot)
    runner.atomic_json(LEDGER_PATH, ledger)
    runner.atomic_json(STATIC_PATH, row)
    push_v9("Issue 73: precharge v9 engineering-3 static verification reservation")
    return row


def _manifest_digest(value: Mapping[str, Any]) -> str:
    return sha256_bytes((canonical(value) + "\n").encode("utf-8"))


def _locked_root_hashes(manifest: Mapping[str, Any]) -> dict[str, str]:
    return {str(row["path"]): str(row["git_blob_sha256"]) for row in manifest.get("included", [])}


def prepare_lock_v9(*, code_source_commit: str | None = None) -> dict[str, Any]:
    _base_prior_v9_ledger()
    _reservation, ledger, projection = _ensure_static_reservation(allow_started=True)
    source_commit = code_source_commit or require_current_v9_branch()
    if git("rev-parse", source_commit) != source_commit:
        raise QualificationV9Failure("v9 code source commit cannot be resolved")
    current_head = git("rev-parse", "HEAD")
    if current_head != source_commit and subprocess.run(
        [*runner.git_invocation(REPOSITORY)[0], "merge-base", "--is-ancestor", source_commit, "HEAD"],
        check=False, env=runner.git_invocation(REPOSITORY)[1],
    ).returncode:
        raise QualificationV9Failure("v9 lock source commit is not an ancestor of the evidence branch")
    remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    if not remote or remote[0] != current_head:
        raise QualificationV9Failure("v9 lock preparation requires source code already pushed to the exact branch")

    parent_lock = json.loads(v8.LOCK_PATH.read_text(encoding="utf-8"))
    input_manifest = json.loads(v8.MANIFEST_PATH.read_text(encoding="utf-8"))
    if (
        input_manifest.get("included_file_count") != 345
        or _manifest_digest(input_manifest) != parent_lock.get("historical_input_manifest_sha256")
        or sha256_text_file(v8.MANIFEST_PATH) != parent_lock.get("historical_input_manifest_sha256")
    ):
        raise QualificationV9Failure("original 345-file v8 historical manifest is not byte/content locked")
    owner, owner_attempt = q.historical_run_receipts()
    identity = original_replay_identity(owner)
    expected_identity = {
        "study_id": runner.STUDY_ID,
        "seed": SEED,
        "arm": ARM,
        "model_digest": PURPOSE_MODEL_DIGEST,
        "sampling_purpose": SAMPLING_PURPOSE,
        "policy_rng_seed": derived_policy_seed(runner.STUDY_ID, SEED, ARM, PURPOSE_MODEL_DIGEST, SAMPLING_PURPOSE),
    }
    mismatches = replay_provenance_mismatches(identity, expected_identity)
    if mismatches:
        raise QualificationV9Failure(f"original owner provenance is not the exact authorized v9 identity: {mismatches}")
    root_ids = [str(x) for x in owner_attempt.get("root_ids", [])]
    if (
        len(root_ids) != 39 or len(set(root_ids)) != 39
        or not set(REQUIRED_ROOTS).issubset(root_ids)
        or "root-173730001-0012" in root_ids
        or "root-173730001-0013" in root_ids
    ):
        raise QualificationV9Failure("owner root membership is not the exact 39-root set with the registered single-action gaps")
    root_hashes = _locked_root_hashes(input_manifest)
    root_check = prevalidate_historical_roots(root_ids, root_hashes, source_commit=source_commit)
    if root_check["status"] != "PASS":
        raise QualificationV9Failure(f"original owner root receipts do not match the immutable 345-file manifest: {root_check['issues'][:5]}")

    config = json.loads(v8.CONFIG_PATH.read_text(encoding="utf-8"))
    plan = json.loads(v8.PLAN_PATH.read_text(encoding="utf-8"))
    config["schema_id"] = "issue73-checkpoint-replay-qualification-config-v9-engineering-3"
    config["source_qualification_version"] = "v9-engineering-3"
    config["source_history_attempt_key"] = ATTEMPT_KEY
    config["parent_v8_source_lock_sha256"] = sha256_text_file(v8.LOCK_PATH)
    config["input_manifest"] = {
        "path": MANIFEST_PATH.relative_to(STUDY).as_posix(),
        "sha256": _manifest_digest(input_manifest),
        "included_file_count": input_manifest["included_file_count"],
        "included_bytes": input_manifest["included_bytes"],
    }
    config["historical_input_manifest_sha256"] = _manifest_digest(input_manifest)
    config["replay_provenance"] = identity
    config["owner_root_ids"] = root_ids
    config["single_action_gaps"] = [
        {"decision_index": 12, "screen": "SHOP_ROOM", "kind": "single_action"},
        {"decision_index": 13, "screen": "MAP_SCREEN", "kind": "single_action"},
    ]
    config["configured_native_module_path"] = parent_lock["native"]["module_path"]
    config["resource_caps"] = {
        **projection,
        "parent_v8_lock_sha256": sha256_text_file(v8.LOCK_PATH),
        "parent_v8_resource_ledger_sha256": sha256_text_file(v8.LEDGER_PATH),
        "parent_v9_failed_static_lock_sha256": sha256_text_file(V9_PRIOR_LOCK_PATH),
        "parent_v9_failed_static_failure_sha256": sha256_text_file(V9_PRIOR_STATIC_FAILURE_PATH),
        "parent_v9_failed_static_ledger_sha256": sha256_text_file(V9_PRIOR_LEDGER_PATH),
        "v9_resource_ledger_sha256": sha256_text_file(LEDGER_PATH),
        "v9_static_debit_cpu_seconds": STATIC_RESERVE,
        "no_prior_charge_reclaimed": True,
    }
    plan["schema_id"] = "issue73-checkpoint-replay-gate-c-plan-v9-engineering-3"
    plan["source_qualification_version"] = "v9-engineering-3"
    plan["qualification_config_file"] = CONFIG_PATH.relative_to(STUDY).as_posix()
    plan["qualification_config_sha256"] = None
    plan["parent_v8_source_lock_sha256"] = sha256_text_file(v8.LOCK_PATH)
    plan["root_membership_source"] = "immutable v8 345-file manifest plus original debug-owner attempt.root_ids; exact 39-ID set before root fidelity"
    plan["single_action_root_gaps"] = ["root-173730001-0012", "root-173730001-0013"]
    runner.atomic_json(MANIFEST_PATH, input_manifest)
    if sha256_text_file(MANIFEST_PATH) != sha256_text_file(v8.MANIFEST_PATH):
        raise QualificationV9Failure("v9 copy does not preserve the original v8 345-file manifest bytes")
    runner.atomic_json(CONFIG_PATH, config)
    plan["qualification_config_sha256"] = sha256_text_file(CONFIG_PATH)
    runner.atomic_json(PLAN_PATH, plan)
    source_hashes = _source_hashes(source_commit)
    lock = {
        "schema_id": "issue73-checkpoint-replay-preseed-source-lock-v9",
        "lock_revision": 13,
        "status": "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION",
        "study_only": True,
        "formal_training_seed_consumed": False,
        "formal_evaluation_seed_consumed": False,
        "stage1_authorized": False,
        "code_source_commit": source_commit,
        "remote_branch": BRANCH,
        "authorized_source_base_commit": SOURCE_BASE_COMMIT,
        "remote_must_equal_current_evidence_head_before_any_run": True,
        "parent_v8_source_lock_sha256": sha256_text_file(v8.LOCK_PATH),
        "parent_v8_code_source_commit": parent_lock["code_source_commit"],
        "parent_v8_resource_ledger_sha256": sha256_text_file(v8.LEDGER_PATH),
        "parent_v8_static_receipt_sha256": sha256_text_file(v8.STATIC_PATH),
        "parent_v8_static_report_sha256": sha256_text_file(v8.STATIC_REPORT_PATH),
        "parent_v8_capture_attempt_sha256": sha256_text_file(v8.ATTEMPT_PATH),
        "parent_v8_capture_report_sha256": sha256_text_file(v8.CAPTURE_REPORT_PATH),
        "parent_v9_source_lock_sha256": sha256_text_file(V9_PRIOR_LOCK_PATH),
        "parent_v9_code_source_commit": json.loads(V9_PRIOR_LOCK_PATH.read_text(encoding="utf-8"))["code_source_commit"],
        "parent_v9_static_attempt_sha256": sha256_text_file(V9_PRIOR_STATIC_PATH),
        "parent_v9_static_failure_sha256": sha256_text_file(V9_PRIOR_STATIC_FAILURE_PATH),
        "parent_v9_resource_ledger_sha256": sha256_text_file(V9_PRIOR_LEDGER_PATH),
        "parent_v9_engineering_1_source_lock_sha256": sha256_text_file(v9_engineering_1.LOCK_PATH),
        "parent_v9_engineering_1_ledger_sha256": sha256_text_file(ENGINEERING_1_LEDGER_PATH),
        "parent_v9_engineering_1_capture_report_sha256": sha256_text_file(ENGINEERING_1_CAPTURE_REPORT_PATH),
        "parent_v9_engineering_1_capture_attempt_sha256": sha256_text_file(v9_engineering_1.ATTEMPT_PATH),
        "parent_v9_engineering_2_source_lock_sha256": sha256_text_file(ENGINEERING_2_LOCK_PATH),
        "parent_v9_engineering_2_static_receipt_sha256": sha256_text_file(ENGINEERING_2_STATIC_PATH),
        "parent_v9_engineering_2_static_report_sha256": sha256_text_file(ENGINEERING_2_STATIC_REPORT_PATH),
        "parent_v9_engineering_2_ledger_sha256": sha256_text_file(ENGINEERING_2_LEDGER_PATH),
        "parent_v7_lock_sha256": parent_lock["v7_source_lock_sha256"],
        "parent_v7_code_source_commit": parent_lock["parent_v7_code_source_commit"],
        "historical_input_base_commit": v7.HISTORICAL_INPUT_BASE_COMMIT,
        "historical_input_manifest": MANIFEST_PATH.relative_to(STUDY).as_posix(),
        "historical_input_manifest_sha256": sha256_text_file(MANIFEST_PATH),
        "historical_input_manifest_canonical_sha256": _manifest_digest(input_manifest),
        "historical_input_sha256": root_hashes,
        "optional_runtime_output_exclusions": input_manifest["excluded"],
        "qualification_config": CONFIG_PATH.relative_to(STUDY).as_posix(),
        "qualification_config_sha256": sha256_text_file(CONFIG_PATH),
        "gate_c_plan": PLAN_PATH.relative_to(STUDY).as_posix(),
        "gate_c_plan_sha256": sha256_text_file(PLAN_PATH),
        "study_source_sha256": source_hashes,
        "replay_provenance": identity,
        "owner_root_ids": root_ids,
        "required_replay_root_ids": list(REQUIRED_ROOTS),
        "historical_root_sha256": {
            str(root_id): root_hashes[q.root_path(str(root_id)).relative_to(REPOSITORY).as_posix()]
            for root_id in root_ids
        },
        "single_action_gaps": [12, 13],
        "native": parent_lock["native"],
        "stsr": parent_lock["stsr"],
        "runtime": parent_lock["runtime"],
        "stsr_python_package_root": parent_lock["stsr_python_package_root"],
        "external_source_verification": q.verify_locked_external_sources(parent_lock),
        "v7_source_lock_sha256": parent_lock["v7_source_lock_sha256"],
        "v7_resource_ledger_sha256": parent_lock["v7_resource_ledger_sha256"],
        "v9_resource_ledger_sha256": sha256_text_file(LEDGER_PATH),
        "v9_retry_resource_baseline_sha256": sha256_text_file(V9_PRIOR_LEDGER_PATH),
        "static_reservation_receipt_sha256": sha256_text_file(STATIC_PATH),
        "resource_projection": projection,
        "fixed_gate_contract": parent_lock["fixed_gate_contract"],
        "source_lock_created_before_any_new_native_execution": True,
    }
    runner.atomic_json(LOCK_PATH, lock)
    push_v9("Issue 73: lock v9 engineering-3 EOL-safe source qualification")
    return lock

def verify_lock_v9(*, require_remote_current: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    files = (LOCK_PATH, CONFIG_PATH, PLAN_PATH, MANIFEST_PATH)
    if not all(path.is_file() for path in files):
        raise QualificationV9Failure("prospective v9 lock/config/plan/manifest is missing or partial")
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if lock.get("schema_id") != "issue73-checkpoint-replay-preseed-source-lock-v9" or lock.get("lock_revision") != 13:
        raise QualificationV9Failure("only the prospective v9 engineering-3 lock revision 13 is accepted")
    if lock.get("status") != "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION" or lock.get("stage1_authorized") is not False:
        raise QualificationV9Failure("v9 lock status or Stage 1 prohibition changed")
    if lock.get("remote_branch") != BRANCH or lock.get("authorized_source_base_commit") != SOURCE_BASE_COMMIT:
        raise QualificationV9Failure("v9 lock is not on the authorized exact source branch/base")
    if sha256_text_file(CONFIG_PATH) != lock.get("qualification_config_sha256"):
        raise QualificationV9Failure("v9 config differs from its prospective source lock")
    if sha256_text_file(PLAN_PATH) != lock.get("gate_c_plan_sha256"):
        raise QualificationV9Failure("v9 Gate C plan differs from its prospective source lock")
    if (
        sha256_text_file(MANIFEST_PATH) != lock.get("historical_input_manifest_sha256")
        or _manifest_digest(manifest) != lock.get("historical_input_manifest_canonical_sha256")
        or lock.get("historical_input_manifest_sha256") != lock.get("historical_input_manifest_canonical_sha256")
    ):
        raise QualificationV9Failure("v9 historical input manifest differs from its raw/canonical source lock")
    parent_manifest = json.loads(v8.MANIFEST_PATH.read_text(encoding="utf-8"))
    if (
        manifest != parent_manifest
        or sha256_text_file(MANIFEST_PATH) != sha256_text_file(v8.MANIFEST_PATH)
        or manifest.get("included_file_count") != 345
    ):
        raise QualificationV9Failure("v9 no longer preserves the exact immutable original 345-file v8 manifest")
    if (
        config.get("historical_input_manifest_sha256") != lock.get("historical_input_manifest_canonical_sha256")
        or plan.get("qualification_config_sha256") != lock.get("qualification_config_sha256")
        or config.get("replay_provenance") != lock.get("replay_provenance")
        or config.get("owner_root_ids") != lock.get("owner_root_ids")
        or plan.get("single_action_root_gaps") != ["root-173730001-0012", "root-173730001-0013"]
    ):
        raise QualificationV9Failure("v9 config, plan, owner provenance or root membership differs from its source lock")
    if len(lock.get("owner_root_ids", [])) != 39 or len(set(lock.get("owner_root_ids", []))) != 39:
        raise QualificationV9Failure("v9 source lock does not contain 39 unique original owner roots")
    source_commit = str(lock.get("code_source_commit", ""))
    current_head = require_current_v9_branch(require_pushed=require_remote_current)
    command, env = runner.git_invocation(REPOSITORY)
    if subprocess.run(
        [*command, "merge-base", "--is-ancestor", source_commit, current_head],
        check=False, env=env,
    ).returncode:
        raise QualificationV9Failure("v9 branch no longer descends from its locked code source")
    if lock.get("study_source_sha256") != _source_hashes(source_commit):
        raise QualificationV9Failure("v9 study source hashes differ from exact committed source blobs")
    base = _base_prior_v9_ledger()
    parent_lock = json.loads(v8.LOCK_PATH.read_text(encoding="utf-8"))
    if (
        lock.get("parent_v8_source_lock_sha256") != sha256_text_file(v8.LOCK_PATH)
        or lock.get("parent_v8_code_source_commit") != parent_lock.get("code_source_commit")
        or lock.get("parent_v8_resource_ledger_sha256") != sha256_text_file(v8.LEDGER_PATH)
        or lock.get("parent_v8_static_receipt_sha256") != sha256_text_file(v8.STATIC_PATH)
        or lock.get("parent_v8_static_report_sha256") != sha256_text_file(v8.STATIC_REPORT_PATH)
        or lock.get("parent_v8_capture_attempt_sha256") != sha256_text_file(v8.ATTEMPT_PATH)
        or lock.get("parent_v8_capture_report_sha256") != sha256_text_file(v8.CAPTURE_REPORT_PATH)
        or lock.get("parent_v9_source_lock_sha256") != sha256_text_file(V9_PRIOR_LOCK_PATH)
        or lock.get("parent_v9_code_source_commit") != json.loads(V9_PRIOR_LOCK_PATH.read_text(encoding="utf-8")).get("code_source_commit")
        or lock.get("parent_v9_static_attempt_sha256") != sha256_text_file(V9_PRIOR_STATIC_PATH)
        or lock.get("parent_v9_static_failure_sha256") != sha256_text_file(V9_PRIOR_STATIC_FAILURE_PATH)
        or lock.get("parent_v9_resource_ledger_sha256") != sha256_text_file(V9_PRIOR_LEDGER_PATH)
        or lock.get("parent_v9_engineering_1_source_lock_sha256") != sha256_text_file(v9_engineering_1.LOCK_PATH)
        or lock.get("parent_v9_engineering_1_ledger_sha256") != sha256_text_file(ENGINEERING_1_LEDGER_PATH)
        or lock.get("parent_v9_engineering_1_capture_report_sha256") != sha256_text_file(ENGINEERING_1_CAPTURE_REPORT_PATH)
        or lock.get("parent_v9_engineering_1_capture_attempt_sha256") != sha256_text_file(v9_engineering_1.ATTEMPT_PATH)
        or lock.get("parent_v9_engineering_2_source_lock_sha256") != sha256_text_file(ENGINEERING_2_LOCK_PATH)
        or lock.get("parent_v9_engineering_2_static_receipt_sha256") != sha256_text_file(ENGINEERING_2_STATIC_PATH)
        or lock.get("parent_v9_engineering_2_static_report_sha256") != sha256_text_file(ENGINEERING_2_STATIC_REPORT_PATH)
        or lock.get("parent_v9_engineering_2_ledger_sha256") != sha256_text_file(ENGINEERING_2_LEDGER_PATH)
        or lock.get("v7_source_lock_sha256") != parent_lock.get("v7_source_lock_sha256")
        or lock.get("v7_resource_ledger_sha256") != parent_lock.get("v7_resource_ledger_sha256")
    ):
        raise QualificationV9Failure("immutable v8/v7 source locks, reports or charged receipts changed after v9 registration")
    if lock.get("native") != parent_lock.get("native") or lock.get("stsr") != parent_lock.get("stsr"):
        raise QualificationV9Failure("v9 native/STSR source, module, or binary pins differ from the v8 source lock")
    _reservation, ledger, projection = _ensure_static_reservation(allow_started=True)
    if lock.get("v9_resource_ledger_sha256") != sha256_text_file(LEDGER_PATH):
        allowed_keys = {ATTEMPT_KEY, PARITY_ATTEMPT_KEY}
        run_rows = [x for x in ledger.get("attempts", []) if x.get("key") in allowed_keys]
        if not run_rows or any(x.get("source_lock_sha256") != sha256_text_file(LOCK_PATH) for x in run_rows):
            raise QualificationV9Failure("v9 ledger changed outside its uniquely keyed locked capture/parity attempts")
    if not projection["forecast_fits_registered_caps"]:
        raise QualificationV9Failure("v9 registered plan/contingency no longer fits the fixed Builder/native/aggregate ceilings")
    historical = prevalidate_historical_roots(
        lock.get("owner_root_ids", []), lock.get("historical_input_sha256", {}),
        source_commit=source_commit,
    )
    if historical.get("status") != "PASS":
        raise QualificationV9Failure(f"v9 named historical roots failed locked-hash prevalidation: {historical.get('issues')[:5]}")
    v7.verify_lock_v7(require_remote_current=False, lock_out=v7.V7_CAPTURE_LOCK_OUT, lock_revision=8)
    return lock, config

def _prepared_capture_runtime_v9() -> tuple[dict[str, Any], dict[str, Any], Path, str]:
    lock, config = verify_lock_v9()
    prepared = q.prepare_capture_prerequisites(
        out=OUT / "capture-preflight-runtime",
        source_lock=lock,
        native_module_path_override=NATIVE_MODULE_PATH,
    )
    module_path = Path(prepared["module"].__file__).resolve(strict=True)
    module_hash = runner.validate_native_module_identity(
        module_path,
        expected_name=q.OLD_CONFIG["identity"]["native_module"],
        expected_sha256=q.OLD_CONFIG["identity"]["native_binary_sha256"],
    )
    if str(module_path) != lock["native"]["module_path"] or module_hash != lock["native"]["loaded_module_sha256"]:
        raise QualificationV9Failure("v9 capture preparation loaded a different native module or binary")
    provenance = lock["replay_provenance"]
    if prepared["model_digest"] != provenance["model_digest"]:
        raise QualificationV9Failure("loaded policy digest differs from the exact historical owner sampling provenance")
    if prepared["adapter_class"].__name__ != "LightSpeedAdapter":
        raise QualificationV9Failure("capture prerequisite path returned an unexpected adapter class")
    return lock, prepared, module_path, module_hash


def _run_static_tests() -> dict[str, Any]:
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for pattern in (
        "test_source_history_capture_v9.py",
        "test_source_history_capture_v9_engineering_2.py",
        "test_source_history_capture_v9_engineering_3.py",
    ):
        suite.addTests(loader.discover(start_dir=str(STUDY), pattern=pattern, top_level_dir=str(STUDY)))
    stream = io.StringIO()
    # E2 remains immutable; use E3's authorized checkout-normalized helper while
    # rerunning the original complete E1/E2 suite in this new verifier.
    with patch.object(v9_engineering_2, "sha256_file", v9_engineering_2.sha256_text_file):
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {
        "status": "PASS" if result.wasSuccessful() and result.testsRun > 0 else "FAIL",
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "output": stream.getvalue()[-12000:],
    }


def qualify_static_v9() -> dict[str, Any]:
    if STATIC_REPORT_PATH.is_file():
        old = json.loads(STATIC_REPORT_PATH.read_text(encoding="utf-8"))
        if old.get("status") == "PASS":
            return old
        raise QualificationV9Failure("v9 static report exists without PASS; no retry")
    if STATIC_FAILURE_PATH.exists():
        raise QualificationV9Failure("v9 static failure receipt is immutable; no retry")
    if any(path.exists() for path in (LOCK_PATH, CONFIG_PATH, PLAN_PATH, MANIFEST_PATH)):
        raise QualificationV9Failure("partial v9 source lock artifacts already exist; preserve and do not reconstruct")
    started_cpu = q.process_tree_cpu()
    started_wall = time.monotonic()
    try:
        q.install_process_cpu_ceiling(STATIC_RESERVE)
        source_commit = require_current_v9_branch()
        _reserve_static()
        lock = prepare_lock_v9(code_source_commit=source_commit)
        lock, config = verify_lock_v9()
        tests = _run_static_tests()
        if tests["status"] != "PASS":
            raise QualificationV9Failure(f"v9 provenance matcher unit tests failed: {tests['output'][-3000:]}")
        runtime_lock, prepared, module_path, module_hash = _prepared_capture_runtime_v9()
        measured = max(0.0, q.process_tree_cpu() - started_cpu)
        if measured > STATIC_RESERVE:
            raise QualificationV9Failure("v9 non-native static qualification exceeded its fixed 60 CPU-s reservation")
        report = {
            "schema_id": "issue73-v9-static-qualification-v1",
            "engineering_revision": 3,
            "status": "PASS",
            "branch": BRANCH,
            "code_source_commit": lock["code_source_commit"],
            "source_lock_sha256": sha256_text_file(LOCK_PATH),
            "parent_engineering_2_source_lock_sha256": sha256_text_file(ENGINEERING_2_LOCK_PATH),
            "parent_engineering_2_static_report_sha256": sha256_text_file(ENGINEERING_2_STATIC_REPORT_PATH),
            "qualification_config_sha256": sha256_text_file(CONFIG_PATH),
            "gate_c_plan_sha256": sha256_text_file(PLAN_PATH),
            "historical_input_manifest_sha256": sha256_text_file(MANIFEST_PATH),
            "v7_source_lock_sha256": lock["v7_source_lock_sha256"],
            "v7_resource_ledger_sha256": lock["v7_resource_ledger_sha256"],
            "parent_v8_source_lock_sha256": lock["parent_v8_source_lock_sha256"],
            "parent_v8_resource_ledger_sha256": lock["parent_v8_resource_ledger_sha256"],
            "parent_v8_static_receipt_sha256": lock["parent_v8_static_receipt_sha256"],
            "parent_v8_capture_attempt_sha256": lock["parent_v8_capture_attempt_sha256"],
            "replay_provenance": lock["replay_provenance"],
            "owner_root_ids": lock["owner_root_ids"],
            "root_prevalidation": {k: v for k, v in prevalidate_historical_roots(lock["owner_root_ids"], lock["historical_input_sha256"], source_commit=lock["code_source_commit"]).items() if k != "roots"},
            "focused_tests": tests,
            "capture_runtime": {
                "python_executable": sys.executable,
                "python_version": sys.version.split()[0],
                "native_module_path": str(module_path),
                "native_module_sha256": module_hash,
                "model_digest": prepared["model_digest"],
                "adapter_class": {"module": prepared["adapter_class"].__module__, "qualname": prepared["adapter_class"].__qualname__},
                "external_sources": prepared["external_sources"],
                "runtime_identity": prepared["runtime"],
                "start_attempt_invoked": False,
                "native_adapter_instantiated": False,
                "native_game_step_started": False,
                "debug_owner_started": False,
                "formal_seed_consumed": False,
            },
            "resource_projection": lock["resource_projection"],
            "fixed_static_reservation_cpu_seconds": STATIC_RESERVE,
            "measured_process_tree_cpu_seconds": measured,
            "measured_wall_seconds": max(0.0, time.monotonic() - started_wall),
            "no_native_attempt_or_seed": True,
        }
        if runtime_lock["code_source_commit"] != lock["code_source_commit"] or prepared["model_digest"] != lock["replay_provenance"]["model_digest"]:
            raise QualificationV9Failure("capture runtime no longer matches the v9 locked source or policy provenance")
        runner.atomic_json(STATIC_REPORT_PATH, report)
        receipt, ledger, _projection = _ensure_static_reservation(allow_started=True)
        push_v9("Issue 73: preserve v9 engineering-3 static qualification")
        return report
    except BaseException as exc:
        measured = max(0.0, q.process_tree_cpu() - started_cpu)
        failure = {
            "schema_id": "issue73-v9-engineering-3-static-qualification-failure-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1800],
            "fixed_static_reservation_cpu_seconds": STATIC_RESERVE,
            "measured_process_tree_cpu_seconds": measured,
            "measured_wall_seconds": max(0.0, time.monotonic() - started_wall),
            "formal_seed_consumed": False,
            "start_attempt_invoked": False,
            "native_adapter_instantiated": False,
            "native_game_step_started": False,
            "no_retry_or_overwrite": True,
        }
        if not STATIC_FAILURE_PATH.exists():
            runner.atomic_json(STATIC_FAILURE_PATH, failure)
        try:
            if STATIC_PATH.is_file() and LEDGER_PATH.is_file():
                receipt = json.loads(STATIC_PATH.read_text(encoding="utf-8"))
                ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
            push_v9("Issue 73: preserve v9 engineering-3 static qualification failure")
        except BaseException:
            pass
        raise


def _require_reviewed_source_sha(reviewed_source_sha: str, reviewer_pass_reference: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", str(reviewed_source_sha).lower()):
        raise QualificationV9Failure("native v9 capture requires exact 40-character Reviewer-approved source SHA")
    if not reviewer_pass_reference.startswith("https://github.com/lsmfttb/spire-research/issues/73#issuecomment-"):
        raise QualificationV9Failure("native v9 capture requires the exact independent Reviewer PASS issue-comment reference")
    head = require_current_v9_branch()
    if reviewed_source_sha.lower() != head:
        raise QualificationV9Failure("Reviewer-approved SHA differs from exact local and remote v9 branch head")
    return head


def _start_capture_attempt_v9(lock: Mapping[str, Any], reviewed_source_sha: str, reviewer_reference: str) -> dict[str, Any]:
    if ATTEMPT_PATH.exists():
        previous = json.loads(ATTEMPT_PATH.read_text(encoding="utf-8"))
        if previous.get("status") == "STARTED":
            previous.update({"status": "INTERRUPTED_NO_RERUN", "cpu_charged_seconds": CAPTURE_RESERVE, "failure": "v9 capture had no terminal receipt; fixed native attempt cannot be repeated"})
            ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
            ledger["attempts"] = [previous if x.get("key") == ATTEMPT_KEY else x for x in ledger["attempts"]]
            runner.atomic_json(ATTEMPT_PATH, previous)
            runner.atomic_json(LEDGER_PATH, ledger)
            push_v9("Issue 73: preserve interrupted v9 native capture without retry")
            raise QualificationV9Failure("v9 native capture was interrupted; no retry is permitted")
        raise QualificationV9Failure(f"v9 fixed capture already has terminal receipt {previous.get('status')}; no retry")
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    static, _ledger, projection = _ensure_static_reservation()
    if static.get("status") != "RESERVED_CONSERVATIVE_STATIC_DEBIT" or not STATIC_REPORT_PATH.is_file() or json.loads(STATIC_REPORT_PATH.read_text(encoding="utf-8")).get("status") != "PASS":
        raise QualificationV9Failure("v9 capture requires the committed static PASS receipt")
    builder, native = q.charged(_base_prior_v9_ledger())
    builder += sum(float(x.get("cpu_charged_seconds", 0)) for x in ledger.get("attempts", []))
    native += sum(float(x.get("cpu_charged_seconds", 0)) for x in ledger.get("attempts", []) if x.get("native_gate_bc") is True)
    if builder + CAPTURE_RESERVE > 4800.0 or native + CAPTURE_RESERVE > 3060.0:
        raise QualificationV9Failure("v9 fixed capture reserve exceeds the existing Builder or native CPU ceiling")
    row = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": ATTEMPT_KEY,
        "kind": "v9_engineering_3_historical_debug_owner_full_action_history_capture",
        "status": "STARTED",
        "native_gate_bc": True,
        "contingency_pool": "native",
        "reserved_cpu_seconds": CAPTURE_RESERVE,
        "cpu_charged_seconds": CAPTURE_RESERVE,
        "source_lock_sha256": sha256_text_file(LOCK_PATH),
        "reviewed_source_sha": reviewed_source_sha,
        "reviewer_pass_reference": reviewer_reference,
        "formal_seed_consumed": False,
        "resource_projection": projection,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ledger["attempts"].append(row)
    runner.atomic_json(ATTEMPT_PATH, row)
    runner.atomic_json(LEDGER_PATH, ledger)
    push_v9(f"Issue 73: start uniquely authorized v9 historical replay capture {ATTEMPT_KEY}")
    return row


def capture_source_history_v9(*, reviewed_source_sha: str, reviewer_pass_reference: str) -> dict[str, Any]:
    """Repeat the fixed owner replay with all nine source-locked transition witnesses."""
    current_source_sha = _require_reviewed_source_sha(reviewed_source_sha, reviewer_pass_reference)
    lock, config = verify_lock_v9()
    if not STATIC_REPORT_PATH.is_file() or json.loads(STATIC_REPORT_PATH.read_text(encoding="utf-8")).get("status") != "PASS":
        raise QualificationV9Failure("v9 native capture requires a committed static qualification PASS")
    owner, owner_attempt = q.historical_run_receipts()
    root_preflight = prevalidate_historical_roots(lock["owner_root_ids"], lock["historical_input_sha256"], source_commit=lock["code_source_commit"])
    if root_preflight.get("status") != "PASS":
        raise QualificationV9Failure(f"all named owner roots must pass hash prevalidation before capture: {root_preflight['issues'][:5]}")
    provenance = original_replay_identity(owner)
    if replay_provenance_mismatches(provenance, lock["replay_provenance"]):
        raise QualificationV9Failure("historical owner replay provenance differs from v9 precommit")
    # All code/source/model/adapter-class prerequisites finish before the fixed attempt is started.
    _runtime_lock, prepared, _module_path, _module_hash = _prepared_capture_runtime_v9()
    if prepared["model_digest"] != provenance["model_digest"]:
        raise QualificationV9Failure("live loaded policy digest differs from the original owner")
    if CAPTURE_REPORT_PATH.exists() or CAPTURE_FAILURE_PATH.exists() or CAPTURE_HISTORY_PATH.exists() or CAPTURE_ROOTS_PATH.exists():
        raise QualificationV9Failure("v9 capture output already exists without the unique attempt receipt; no retry")
    q.install_process_cpu_ceiling(int(CAPTURE_RESERVE))
    _start_capture_attempt_v9(lock, current_source_sha, reviewer_pass_reference)
    started_cpu = q.process_tree_cpu()
    adapter = None
    root_snapshots_sha256 = None
    try:
        module = prepared["module"]
        adapter = runner.native_adapter(module, SEED)
        remaining = max(1.0, CAPTURE_RESERVE - max(0.0, q.process_tree_cpu() - started_cpu))
        result = runner._run_one(
            out=CAPTURE_OUT,
            module=module,
            model=prepared["model"],
            model_digest=prepared["model_digest"],
            seed=SEED,
            arm=ARM,
            encoder=prepared["encoder"],
            capture_checkpoints=True,
            capture_all_roots=True,
            purpose=SAMPLING_PURPOSE,
            resource_label=f"SOURCE_QUAL_V9_{SEED}",
            adapter_override=adapter,
            durable=False,
            capture_trace=False,
            capture_action_history=True,
            cpu_cap_seconds=remaining,
        )
        roots = list(result.get("_volatile_roots", []))
        history = list(result.get("_actual_action_history", []))
        report, root_snapshots_sha256 = compare_capture_with_durable_roots_v9(
            result, roots, history, owner=owner, owner_attempt=owner_attempt,
            historical_roots=root_preflight["roots"],
        )
        roots_by_id = {stage0.root_id(root): root for root in roots}
        transition_witnesses: dict[str, Any] = {}
        for pair in config["gate_c_transition_pairs"]:
            root_id_value = str(pair["root_id"])
            candidate_index = int(pair["candidate_index"])
            key = f"{root_id_value}-a{candidate_index}"
            root = roots_by_id.get(root_id_value)
            if root is None:
                transition_witnesses[key] = {"status": "MISSING_ROOT", "root_id": root_id_value}
                continue
            try:
                transition_witnesses[key] = q.capture_transition_witness(
                    adapter, prepared["module"], {**root, "root_id": root_id_value}, candidate_index,
                )
            except Exception as exc:
                transition_witnesses[key] = {
                    "status": "FAIL",
                    "root_id": root_id_value,
                    "candidate_index": candidate_index,
                    "failure_type": type(exc).__name__,
                    "failure": str(exc)[:800],
                }
        report = apply_transition_witness_gate_v9(report, transition_witnesses, config["gate_c_transition_pairs"])
        report.update({
            "source_lock_sha256": sha256_text_file(LOCK_PATH),
            "reviewed_source_sha": current_source_sha,
            "reviewer_pass_reference": reviewer_pass_reference,
            "runtime_identity": prepared["runtime"],
            "native_module_path": str(_module_path),
            "native_module_sha256": _module_hash,
            "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - started_cpu),
            "history_sha256": None,
            "history_event_count": len(history),
            "root_snapshots_path": CAPTURE_ROOTS_PATH.relative_to(STUDY).as_posix(),
            "root_snapshots_sha256": root_snapshots_sha256,
            "formal_seed_consumed": False,
        })
        runner.atomic_json(CAPTURE_HISTORY_PATH, {
            "schema_id": "issue73-actual-action-step-history-v9-v1",
            "source_lock_sha256": sha256_text_file(LOCK_PATH),
            "code_source_commit": lock["code_source_commit"],
            "reviewed_source_sha": current_source_sha,
            "reviewer_pass_reference": reviewer_pass_reference,
            "owner_seed": SEED,
            "arm": ARM,
            "sampling_purpose": SAMPLING_PURPOSE,
            "policy_rng_seed": provenance["policy_rng_seed"],
            "role": "historical debug owner provenance/prefix replay only; not a new owner",
            "source_identity": prepared["environment"]["identity"],
            "runtime_identity": prepared["runtime"],
            "event_count": len(history),
            "events": history,
        })
        report["history_sha256"] = sha256_text_file(CAPTURE_HISTORY_PATH)
        report["status"] = report["status"] if report.get("status") == "PASS" else "FAIL_SOURCE_HISTORY_CAPTURE_PARITY"
        if adapter is not None:
            adapter.close()
            adapter = None
        runner.atomic_json(CAPTURE_REPORT_PATH, report)
        row = json.loads(ATTEMPT_PATH.read_text(encoding="utf-8"))
        row.update({
            "status": "COMPLETE" if report["status"] == "PASS" else "FAIL_OR_INCOMPLETE",
            "measured_process_cpu_seconds": max(0.0, q.process_tree_cpu() - started_cpu),
            "cpu_charged_seconds": CAPTURE_RESERVE,
            "report_status": report["status"],
            "history_event_count": len(history),
            "root_snapshots_sha256": root_snapshots_sha256,
            "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        ledger["attempts"] = [row if x.get("key") == ATTEMPT_KEY else x for x in ledger["attempts"]]
        runner.atomic_json(ATTEMPT_PATH, row)
        runner.atomic_json(LEDGER_PATH, ledger)
        push_v9("Issue 73: preserve uniquely authorized v9 historical owner replay evidence")
        report["attempt_receipt"] = row
        return report
    except BaseException as exc:
        root_snapshots_sha256 = capture_root_snapshot_sha256_v9()
        if adapter is not None:
            try:
                adapter.close()
            except BaseException:
                pass
        failure = {
            "schema_id": "issue73-source-history-capture-failure-v9-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1800],
            "reviewed_source_sha": current_source_sha,
            "reviewer_pass_reference": reviewer_pass_reference,
            "root_snapshots_path": CAPTURE_ROOTS_PATH.relative_to(STUDY).as_posix() if root_snapshots_sha256 else None,
            "root_snapshots_sha256": root_snapshots_sha256,
            "formal_seed_consumed": False,
            "no_retry_or_overwrite": True,
        }
        if not CAPTURE_FAILURE_PATH.exists():
            runner.atomic_json(CAPTURE_FAILURE_PATH, failure)
        try:
            row = json.loads(ATTEMPT_PATH.read_text(encoding="utf-8"))
            if row.get("status") == "STARTED":
                row.update({"status": "FAIL_OR_INCOMPLETE", "failure_type": failure["failure_type"], "failure": failure["failure"], "cpu_charged_seconds": CAPTURE_RESERVE, "measured_process_cpu_seconds": max(0.0, q.process_tree_cpu() - started_cpu), "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
                ledger["attempts"] = [row if x.get("key") == ATTEMPT_KEY else x for x in ledger["attempts"]]
                runner.atomic_json(ATTEMPT_PATH, row)
                runner.atomic_json(LEDGER_PATH, ledger)
            push_v9("Issue 73: preserve terminal v9 historical owner replay failure")
        except BaseException:
            pass
        raise


def _start_parity_attempt_v9(lock: Mapping[str, Any]) -> dict[str, Any]:
    if PARITY_ATTEMPT_PATH.exists():
        previous = json.loads(PARITY_ATTEMPT_PATH.read_text(encoding="utf-8"))
        if previous.get("status") == "STARTED":
            previous.update({
                "status": "INTERRUPTED_NO_RERUN",
                "cpu_charged_seconds": 80.0,
                "failure": "fresh-process parity has no terminal receipt; preserve its fixed reservation and do not rerun",
            })
            ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
            ledger["attempts"] = [previous if x.get("key") == PARITY_ATTEMPT_KEY else x for x in ledger["attempts"]]
            runner.atomic_json(PARITY_ATTEMPT_PATH, previous)
            runner.atomic_json(LEDGER_PATH, ledger)
            push_v9("Issue 73: preserve interrupted v9 fresh-process parity without rerun")
            raise QualificationV9Failure("v9 fresh-process parity was interrupted; its attempt is terminal")
        raise QualificationV9Failure(f"v9 fresh-process parity already has terminal receipt {previous.get('status')}; no retry")

    capture_report = json.loads(CAPTURE_REPORT_PATH.read_text(encoding="utf-8"))
    if capture_report.get("status") != "PASS" or not capture_report.get("transition_witness_integrity", {}).get("complete_pass"):
        raise QualificationV9Failure("fresh-process parity requires all nine same-process transition witnesses to pass")
    static, ledger, projection = _ensure_static_reservation()
    if static.get("status") != "RESERVED_CONSERVATIVE_STATIC_DEBIT" or json.loads(STATIC_REPORT_PATH.read_text(encoding="utf-8")).get("status") != "PASS":
        raise QualificationV9Failure("fresh-process parity requires the locked static PASS")
    builder, native = q.charged(_base_prior_v9_ledger())
    builder += sum(float(x.get("cpu_charged_seconds", 0)) for x in ledger.get("attempts", []))
    native += sum(float(x.get("cpu_charged_seconds", 0)) for x in ledger.get("attempts", []) if x.get("native_gate_bc") is True)
    if builder + 80.0 > 4800.0 or native + 80.0 > 3060.0:
        raise QualificationV9Failure("v9 fresh-process parity reserve exceeds the existing Builder or native CPU ceiling")
    row = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": PARITY_ATTEMPT_KEY,
        "kind": "v9_engineering_3_fresh_process_complete_prefix_and_forced_action_parity",
        "status": "STARTED",
        "native_gate_bc": True,
        "reserved_cpu_seconds": 80.0,
        "cpu_charged_seconds": 80.0,
        "source_lock_sha256": sha256_text_file(LOCK_PATH),
        "code_source_commit": lock["code_source_commit"],
        "formal_seed_consumed": False,
        "resource_projection": projection,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ledger["attempts"].append(row)
    runner.atomic_json(PARITY_ATTEMPT_PATH, row)
    runner.atomic_json(LEDGER_PATH, ledger)
    push_v9("Issue 73: start uniquely locked v9 fresh-process prefix/transition parity")
    return row


def fresh_process_prefix_parity_v9() -> dict[str, Any]:
    """Compare nine new-process forced transitions to the captured same-process witnesses."""
    lock, config = verify_lock_v9()
    if PARITY_ATTEMPT_PATH.is_file():
        previous = json.loads(PARITY_ATTEMPT_PATH.read_text(encoding="utf-8"))
        if previous.get("status") == "PASS" and PARITY_REPORT_PATH.is_file():
            return json.loads(PARITY_REPORT_PATH.read_text(encoding="utf-8"))
        raise QualificationV9Failure(f"v9 fresh-process parity attempt is already terminal: {previous.get('status')}")
    capture_report = json.loads(CAPTURE_REPORT_PATH.read_text(encoding="utf-8"))
    capture_attempt = json.loads(v9_engineering_1.ATTEMPT_PATH.read_text(encoding="utf-8"))
    if capture_report.get("status") != "PASS" or not capture_report.get("transition_witness_integrity", {}).get("complete_pass"):
        raise QualificationV9Failure("v9 fresh-process parity is blocked until the capture's complete witness set passes")
    history_record = json.loads(CAPTURE_HISTORY_PATH.read_text(encoding="utf-8"))
    if (
        history_record.get("schema_id") != "issue73-actual-action-step-history-v9-v1"
        or history_record.get("source_lock_sha256") != sha256_text_file(LOCK_PATH)
        or history_record.get("code_source_commit") != lock.get("code_source_commit")
        or history_record.get("event_count") != len(history_record.get("events", []))
        or len(history_record.get("events", [])) != capture_report.get("history_event_count")
        or sha256_text_file(CAPTURE_HISTORY_PATH) != capture_report.get("history_sha256")
        or capture_report.get("source_lock_sha256") != sha256_text_file(v9_engineering_1.LOCK_PATH)
        or capture_report.get("root_snapshots_path") != ENGINEERING_1_CAPTURE_ROOTS_PATH.relative_to(STUDY).as_posix()
        or capture_report.get("root_snapshots_sha256") != sha256_text_file(ENGINEERING_1_CAPTURE_ROOTS_PATH)
        or capture_attempt.get("status") != "COMPLETE"
        or capture_attempt.get("report_status") != "PASS"
        or capture_attempt.get("source_lock_sha256") != sha256_text_file(v9_engineering_1.LOCK_PATH)
        or capture_attempt.get("history_event_count") != capture_report.get("history_event_count")
        or capture_attempt.get("root_snapshots_sha256") != capture_report.get("root_snapshots_sha256")
    ):
        raise QualificationV9Failure("v9 action history differs from its locked complete capture receipt")
    roots_record = json.loads(CAPTURE_ROOTS_PATH.read_text(encoding="utf-8"))
    if (
        roots_record.get("root_count") != 39
        or roots_record.get("schema_id") != "issue73-volatile-root-snapshots-v9-v1"
        or len(roots_record.get("roots", [])) != 39
        or capture_report.get("captured_root_count") != 39
    ):
        raise QualificationV9Failure("fresh-process parity requires the complete 39-root v9 snapshot receipt")
    snapshot_ids = [str(row.get("root_id", "")) for row in roots_record.get("roots", [])]
    if set(snapshot_ids) != set(lock.get("owner_root_ids", [])) or len(snapshot_ids) != len(set(snapshot_ids)):
        raise QualificationV9Failure("v9 root snapshot membership is not the exact unique 39-root owner set")
    integrity = transition_witness_integrity_v9(capture_report.get("transition_witnesses", {}), config["gate_c_transition_pairs"])
    if not integrity.get("complete_pass"):
        raise QualificationV9Failure(f"v9 same-process transition witness set is incomplete: {integrity}")

    q.install_process_cpu_ceiling(80.0)
    _start_parity_attempt_v9(lock)
    phase_start = q.process_tree_cpu()
    adapter = None
    try:
        _runtime_lock, prepared, module_path, module_hash = _prepared_capture_runtime_v9()
        history = history_record["events"]
        roots = {str(root_id): q.root_receipt(str(root_id)) for root_id in lock["owner_root_ids"]}
        adapter = runner.native_adapter(prepared["module"], SEED)
        witnesses: dict[str, Any] = {}
        for pair in config["gate_c_transition_pairs"]:
            root_id_value = str(pair["root_id"])
            candidate_index = int(pair["candidate_index"])
            key = f"{root_id_value}-a{candidate_index}"
            try:
                root = roots[root_id_value]
                original = capture_report["transition_witnesses"][key]
                first = q.replay_forced_transition(adapter, history, root, candidate_index, prepared["module"])
                second = q.replay_forced_transition(adapter, history, root, candidate_index, prepared["module"])
                same_process_equal = runner.canonical(first) == runner.canonical(second)
                original_equal = runner.canonical(first) == runner.canonical(original["transition"])
                witnesses[key] = {
                    "status": "PASS" if same_process_equal and original_equal else "FAIL",
                    "root_id": root_id_value,
                    "candidate_index": candidate_index,
                    "action_identity": root["legal_action_occurrences"][candidate_index],
                    "purpose": pair.get("purposes", []),
                    "fresh_process_repeat_equal": same_process_equal,
                    "cross_process_full_transition_equal": original_equal,
                    "transition": first,
                }
            except Exception as exc:
                witnesses[key] = {
                    "status": "FAIL",
                    "root_id": root_id_value,
                    "candidate_index": candidate_index,
                    "failure_type": type(exc).__name__,
                    "failure": str(exc)[:800],
                }
        gate_b_screens = sorted({str(roots[root_id]["screen"]) for root_id in q.GATE_B_ROOTS.values()})
        all_witnesses_pass = len(witnesses) == len(config["gate_c_transition_pairs"]) and all(x.get("status") == "PASS" for x in witnesses.values())
        report = {
            "schema_id": "issue73-fresh-process-prefix-parity-v9-v1",
            "status": "PASS" if all_witnesses_pass and len(gate_b_screens) >= 3 else "FAIL_FRESH_PROCESS_PREFIX_OR_TRANSITION_PARITY",
            "fresh_process": True,
            "source_lock_sha256": sha256_text_file(LOCK_PATH),
            "code_source_commit": lock["code_source_commit"],
            "source_identity": prepared["environment"]["identity"],
            "runtime_identity": prepared["runtime"],
            "native_module_path": str(module_path),
            "native_module_sha256": module_hash,
            "history_sha256": sha256_text_file(CAPTURE_HISTORY_PATH),
            "replayed_transition_count": len(witnesses),
            "required_transition_count": len(config["gate_c_transition_pairs"]),
            "witnesses": witnesses,
            "gate_b_root_screen_families": gate_b_screens,
            "gate_b_witness_root_ids": dict(q.GATE_B_ROOTS),
            "native_rng_byte_state_exposed": False,
            "rng_claim": "The pinned API exposes no native RNG bytes/counters. Evidence is full action-history prefix replay and exact same-process witness parity for each of the nine locked forced transitions; no hidden-game-future distribution claim.",
            "formal_seed_consumed": False,
        }
        runner.atomic_json(PARITY_REPORT_PATH, report)
    except BaseException as exc:
        failure = {
            "schema_id": "issue73-fresh-process-prefix-parity-failure-v9-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1200],
            "no_retry": True,
            "formal_seed_consumed": False,
        }
        runner.atomic_json(PARITY_FAILURE_PATH, failure)
        attempt = json.loads(PARITY_ATTEMPT_PATH.read_text(encoding="utf-8"))
        attempt.update({
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": failure["failure_type"],
            "failure": failure["failure"],
            "measured_process_cpu_seconds": max(0.0, q.process_tree_cpu() - phase_start),
            "cpu_charged_seconds": 80.0,
            "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        ledger["attempts"] = [attempt if x.get("key") == PARITY_ATTEMPT_KEY else x for x in ledger["attempts"]]
        runner.atomic_json(PARITY_ATTEMPT_PATH, attempt)
        runner.atomic_json(LEDGER_PATH, ledger)
        push_v9("Issue 73: preserve v9 fresh-process parity failure receipt")
        raise
    finally:
        if adapter is not None:
            adapter.close()
    attempt = json.loads(PARITY_ATTEMPT_PATH.read_text(encoding="utf-8"))
    measured = max(0.0, q.process_tree_cpu() - phase_start)
    if measured > 80.0 and report["status"] == "PASS":
        report["status"] = "FAIL_CPU_RESERVATION_EXCEEDED"
    report["measured_process_cpu_seconds"] = measured
    success = report["status"] == "PASS" and measured <= 80.0
    attempt.update({
        "status": "PASS" if success else ("FAIL_CPU_RESERVATION_EXCEEDED" if measured > 80.0 else "FAIL_OR_INCOMPLETE"),
        "report_status": report["status"],
        "measured_process_cpu_seconds": measured,
        "cpu_charged_seconds": 80.0,
        "formal_seed_consumed": False,
        "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    ledger["attempts"] = [attempt if x.get("key") == PARITY_ATTEMPT_KEY else x for x in ledger["attempts"]]
    runner.atomic_json(PARITY_ATTEMPT_PATH, attempt)
    runner.atomic_json(LEDGER_PATH, ledger)
    report["attempt_receipt"] = attempt
    runner.atomic_json(PARITY_REPORT_PATH, report)
    push_v9("Issue 73: preserve v9 fresh-process prefix/transition parity")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("qualify-static", "verify-lock", "capture", "fresh-process-parity"))
    parser.add_argument("--reviewed-source-sha")
    parser.add_argument("--reviewer-pass-reference")
    args = parser.parse_args(argv)
    if args.command == "qualify-static":
        result = qualify_static_v9()
    elif args.command == "verify-lock":
        lock, _config = verify_lock_v9()
        result = {"status": "PASS", "source_lock_sha256": sha256_text_file(LOCK_PATH), "code_source_commit": lock["code_source_commit"]}
    elif args.command == "capture":
        if not args.reviewed_source_sha or not args.reviewer_pass_reference:
            raise QualificationV9Failure("capture requires --reviewed-source-sha and --reviewer-pass-reference")
        result = capture_source_history_v9(reviewed_source_sha=args.reviewed_source_sha, reviewer_pass_reference=args.reviewer_pass_reference)
    else:
        result = fresh_process_prefix_parity_v9()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
