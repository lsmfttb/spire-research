"""Prospective, source-faithful replay qualification for Issue 73 v8.

This study-local module preserves the v1-v7 artifacts and permits no native
capture until the v8 static lock/runtime verification has passed independent
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

import numpy as np

import checkpoint_replay_qualification as q
import runner
import source_qualification_v7 as v7
import stage0
import policy


STUDY = q.STUDY
REPOSITORY = q.REPOSITORY
STUDY_RELATIVE = q.STUDY_RELATIVE
OUT = STUDY / "receipts" / "source-qualification-v8"
OUT_RELATIVE = OUT.relative_to(REPOSITORY).as_posix()
BRANCH = "codex/issue-73-v8-provenance"
SOURCE_BASE_COMMIT = "f581ed1cdd75a27a41261f83f0357b3f2f98ad3c"
SEED = 173730001
ARM = "P0"
SAMPLING_PURPOSE = "issue73-stage0-debug-owner"
ATTEMPT_KEY = "source-history-capture-173730001-prospective-v8"
ATTEMPT_PATH = OUT / "attempts" / f"{ATTEMPT_KEY}.json"
STATIC_KEY = "builder-prelock-static-verification-v8"
STATIC_PATH = OUT / "attempts" / f"{STATIC_KEY}.json"
STATIC_RESERVE = 60.0
CAPTURE_RESERVE = 60.0
LOCK_PATH = OUT / "preseed-source-lock-v8.json"
CONFIG_PATH = OUT / "qualification-config-v8.json"
PLAN_PATH = OUT / "plans" / "gate-c-replay-plan-v8.json"
MANIFEST_PATH = OUT / "input-manifest-v8.json"
LEDGER_PATH = OUT / "resource-ledger-v8.json"
LEDGER_SNAPSHOT_PATH = OUT / "ledgers" / "resource-ledger-before-v8.json"
STATIC_REPORT_PATH = OUT / "reports" / "static-qualification-report-v8.json"
STATIC_FAILURE_PATH = OUT / "reports" / "static-qualification-failure-v8.json"
CAPTURE_OUT = OUT / "capture-runtime-v8"
CAPTURE_HISTORY_PATH = OUT / "histories" / "actual-action-step-history-v8.json"
CAPTURE_REPORT_PATH = OUT / "reports" / "source-history-capture-report-v8.json"
CAPTURE_FAILURE_PATH = OUT / "reports" / "source-history-capture-failure-v8.json"
REQUIRED_ROOTS = tuple(q.REQUIRED_REPLAY_ROOTS)
PURPOSE_MODEL_DIGEST = "ed94976872e04eb3746c51a8e6305b40a673d46ca5b7ab24c7b26424723d447d"
MODEL_PATH = q.HISTORICAL / "policies" / "pi_init.pt"
NATIVE_MODULE_PATH = q.V6_CONFIGURED_NATIVE_MODULE_PATH
SOURCE_HASH_NAMES = tuple(sorted(set(v7.SOURCE_HASH_PATHS_V7) | {
    "source_qualification_v8.py",
    "test_source_history_capture_v8.py",
}))


class QualificationV8Failure(RuntimeError):
    pass


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_text_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def canonical(value: Any) -> str:
    return runner.canonical(value)


def git(*args: str) -> str:
    return q.git(*args)


def push_v8(message: str) -> str:
    git("add", "--", OUT_RELATIVE)
    staged = [x for x in git("diff", "--cached", "--name-only").splitlines() if x]
    if any(x != OUT_RELATIVE and not x.startswith(f"{OUT_RELATIVE}/") for x in staged):
        raise QualificationV8Failure("refusing to stage or commit files outside the v8 evidence directory")
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
        raise QualificationV8Failure("remote v8 branch does not contain the exact handoff commit")
    return head


def require_current_v8_branch(*, require_pushed: bool = True) -> str:
    head = git("rev-parse", "HEAD")
    branch = git("branch", "--show-current")
    if branch != BRANCH:
        raise QualificationV8Failure(f"expected v8 branch {BRANCH}, found {branch!r}")
    command, env = runner.git_invocation(REPOSITORY)
    if subprocess.run(
        [*command, "merge-base", "--is-ancestor", SOURCE_BASE_COMMIT, head],
        check=False, env=env,
    ).returncode:
        raise QualificationV8Failure("v8 worktree does not preserve the exact authorized f581 source lineage")
    if require_pushed:
        remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
        if not remote or remote[0] != head:
            raise QualificationV8Failure("v8 source/evidence must be at the exact pushed issue branch head")
    return head


def derived_policy_seed(study_id: str, seed: int, arm: str, model_digest: str, purpose: str) -> int:
    return policy.derived_seed(study_id, seed, arm, model_digest, purpose)


def original_replay_identity(owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if owner is None:
        owner, _ = q.historical_run_receipts()
    controller = owner.get("controller_provenance")
    config = controller.get("config") if isinstance(controller, Mapping) else None
    if not isinstance(config, Mapping):
        raise QualificationV8Failure("historical owner lacks controller provenance config")
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
        raise QualificationV8Failure(f"unsafe or unexpected historical root id: {root_id!r}")
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
        except (OSError, ValueError, QualificationV8Failure) as exc:
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


def compare_capture_run_v8(
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
    observed_by_id = {str(root.get("root_id", "")): root for root in roots}
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
        "schema_id": "issue73-source-history-capture-v8",
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


def _source_hashes(source_commit: str) -> dict[str, str]:
    paths = [f"{STUDY_RELATIVE}/{name}" for name in SOURCE_HASH_NAMES]
    blobs = v7.external_git_blobs(source_commit, paths)
    hashes: dict[str, str] = {}
    for name in SOURCE_HASH_NAMES:
        blob = blobs[f"{STUDY_RELATIVE}/{name}"]
        checkout = STUDY / name
        if not checkout.is_file():
            raise QualificationV8Failure(f"v8 locked source file is missing: {name}")
        q.verify_locked_source_payload(
            label=f"study/{name}", expected_sha256=sha256_bytes(blob),
            committed_blob=blob, checkout_bytes=checkout.read_bytes(),
        )
        hashes[name] = sha256_bytes(blob)
    return hashes


def _resource_projection(base_ledger: Mapping[str, Any], v8_ledger: Mapping[str, Any]) -> dict[str, Any]:
    builder_base, native_base = q.charged(base_ledger)
    attempts = list(v8_ledger.get("attempts", []))
    builder = builder_base + sum(float(x.get("cpu_charged_seconds", 0)) for x in attempts)
    native = native_base + sum(
        float(x.get("cpu_charged_seconds", 0)) for x in attempts if x.get("native_gate_bc") is True
    )
    native_remaining = 2760.0 - native
    capture_already_charged = any(x.get("key") == ATTEMPT_KEY for x in attempts)
    fixed_native_future = (0.0 if capture_already_charged else 60.0) + 80.0 + 20.0 + 64.0 * 20.0
    builder_future = fixed_native_future + 120.0
    builder_projected = builder + builder_future
    native_projected = native + fixed_native_future
    return {
        "aggregate_stage0_cpu_seconds": 6000.0,
        "builder_ceiling_cpu_seconds": 4800.0,
        "reviewer_reserve_cpu_seconds": 1200.0,
        "prior_builder_charge_cpu_seconds": builder_base,
        "builder_charge_including_v8_cpu_seconds": builder,
        "prior_native_gate_bc_charge_cpu_seconds": native_base,
        "native_gate_bc_charge_including_v8_cpu_seconds": native,
        "native_gate_bc_remaining_cpu_seconds": native_remaining,
        "v8_static_reservation_cpu_seconds": STATIC_RESERVE,
        "new_capture_reserve_cpu_seconds": 60.0,
        "fresh_process_prefix_and_transition_parity_reserve_cpu_seconds": 80.0,
        "gate_c_environment_setup_reserve_cpu_seconds": 20.0,
        "gate_c_suffix_attempt_count": 64,
        "gate_c_per_suffix_process_cpu_cap_seconds": 20.0,
        "gate_c_suffix_total_reserve_cpu_seconds": 1280.0,
        "gate_a_nonnative_process_cpu_cap_seconds": 120.0,
        "native_contingency_cpu_seconds": 0.0,
        "projected_new_builder_cpu_reserve_seconds": builder_future,
        "cumulative_builder_projection_cpu_seconds": builder_projected,
        "cumulative_native_gate_bc_projection_cpu_seconds": native_projected,
        "reviewer_reserve_untouched": True,
        "no_prior_charge_reclaimed": True,
        "stage1_authorized": False,
        "forecast_fits_registered_caps": builder_projected <= 4800.0 and native_projected <= 2760.0 and builder_projected + 1200.0 <= 6000.0,
    }


def _base_v7_ledger() -> dict[str, Any]:
    value = v7._v7_resource_ledger()
    builder, native = q.charged(value)
    if abs(builder - 2351.610134) > 1e-6 or abs(native - 1320.0) > 1e-6:
        raise QualificationV8Failure(f"v7 resource baseline changed: Builder={builder:.6f}, native={native:.6f}")
    return value


def _ledger_template(base: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_id": "issue73-v8-resource-ledger-v1",
        "baseline_ledger_path": (v7.RESOURCE_LEDGER_PATH.relative_to(STUDY)).as_posix(),
        "baseline_ledger_sha256": sha256_text_file(v7.RESOURCE_LEDGER_PATH),
        "baseline_builder_charge_cpu_seconds": q.charged(base)[0],
        "baseline_native_gate_bc_charge_cpu_seconds": q.charged(base)[1],
        "builder_ceiling_cpu_seconds": 4800.0,
        "native_gate_bc_ceiling_cpu_seconds": 2760.0,
        "reviewer_reserve_cpu_seconds": 1200.0,
        "attempts": [],
    }


def _ensure_static_reservation(*, allow_started: bool = False) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    base = _base_v7_ledger()
    if not all(path.is_file() for path in (STATIC_PATH, LEDGER_PATH, LEDGER_SNAPSHOT_PATH)):
        raise QualificationV8Failure("v8 fixed 60 CPU-s static reservation is not durably present")
    receipt = json.loads(STATIC_PATH.read_text(encoding="utf-8"))
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    snapshot = json.loads(LEDGER_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    expected_snapshot = _ledger_template(base)
    rows = [row for row in ledger.get("attempts", []) if row.get("key") == STATIC_KEY]
    if (
        snapshot != expected_snapshot
        or ledger.get("schema_id") != "issue73-v8-resource-ledger-v1"
        or ledger.get("baseline_ledger_sha256") != sha256_text_file(v7.RESOURCE_LEDGER_PATH)
        or receipt.get("key") != STATIC_KEY
        or receipt.get("reserved_cpu_seconds") != STATIC_RESERVE
        or receipt.get("cpu_charged_seconds") != STATIC_RESERVE
        or receipt.get("native_gate_bc") is not False
        or receipt.get("formal_seed_consumed") is not False
        or rows != [receipt]
    ):
        raise QualificationV8Failure("v8 static reservation, ledger snapshot, or immutable v7 baseline is inconsistent")
    if receipt.get("status") != "RESERVED_CONSERVATIVE_STATIC_DEBIT":
        raise QualificationV8Failure("v8 fixed static reservation receipt changed from its immutable reserved state")
    projection = _resource_projection(base, ledger)
    if not projection["forecast_fits_registered_caps"]:
        raise QualificationV8Failure(f"v8 continuation forecast exceeds the fixed resource contract: {projection}")
    return receipt, ledger, projection


def _reserve_static() -> dict[str, Any]:
    if STATIC_PATH.exists() or LEDGER_PATH.exists() or LEDGER_SNAPSHOT_PATH.exists():
        if all(path.is_file() for path in (STATIC_PATH, LEDGER_PATH, LEDGER_SNAPSHOT_PATH)):
            receipt, _ledger, _projection = _ensure_static_reservation()
            if receipt.get("status") == "RESERVED_CONSERVATIVE_STATIC_DEBIT" and STATIC_REPORT_PATH.is_file() and json.loads(STATIC_REPORT_PATH.read_text(encoding="utf-8")).get("status") == "PASS":
                return receipt
        raise QualificationV8Failure("v8 static reservation already exists or is partial; no second reservation/replay")
    head = require_current_v8_branch()
    base = _base_v7_ledger()
    snapshot = _ledger_template(base)
    projection_ledger = json.loads(json.dumps(snapshot))
    projection_ledger["attempts"] = [{"key": STATIC_KEY, "cpu_charged_seconds": STATIC_RESERVE, "native_gate_bc": False}]
    projection = _resource_projection(base, projection_ledger)
    if not projection["forecast_fits_registered_caps"]:
        raise QualificationV8Failure(f"v8 fixed 60 CPU-s debit plus registered continuation does not fit: {projection}")
    row = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": STATIC_KEY,
        "kind": "v8_bounded_static_provenance_lock_and_runtime_verification",
        "status": "RESERVED_CONSERVATIVE_STATIC_DEBIT",
        "native_gate_bc": False,
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
    ledger["attempts"] = [row]
    runner.atomic_json(LEDGER_SNAPSHOT_PATH, snapshot)
    runner.atomic_json(LEDGER_PATH, ledger)
    runner.atomic_json(STATIC_PATH, row)
    push_v8("Issue 73: precharge unique v8 non-native source-lock and static verification reservation")
    return row


def _manifest_digest(value: Mapping[str, Any]) -> str:
    return sha256_bytes((canonical(value) + "\n").encode("utf-8"))


def _locked_root_hashes(manifest: Mapping[str, Any]) -> dict[str, str]:
    return {str(row["path"]): str(row["git_blob_sha256"]) for row in manifest.get("included", [])}


def prepare_lock_v8(*, code_source_commit: str | None = None) -> dict[str, Any]:
    _reservation, ledger, projection = _ensure_static_reservation(allow_started=True)
    source_commit = code_source_commit or require_current_v8_branch()
    if git("rev-parse", source_commit) != source_commit:
        raise QualificationV8Failure("v8 code source commit cannot be resolved")
    current_head = git("rev-parse", "HEAD")
    if current_head != source_commit and subprocess.run(
        [*runner.git_invocation(REPOSITORY)[0], "merge-base", "--is-ancestor", source_commit, "HEAD"],
        check=False, env=runner.git_invocation(REPOSITORY)[1],
    ).returncode:
        raise QualificationV8Failure("v8 lock source commit is not an ancestor of the evidence branch")
    remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    if not remote or remote[0] != current_head:
        raise QualificationV8Failure("v8 lock preparation requires source code already pushed to the exact branch")
    expected_base, _ = v7.verify_lock_v7(require_remote_current=False, lock_out=v7.V7_CAPTURE_LOCK_OUT, lock_revision=8)
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
        raise QualificationV8Failure(f"original owner provenance is not the exact authorized v8 identity: {mismatches}")
    root_ids = [str(x) for x in owner_attempt.get("root_ids", [])]
    if not set(REQUIRED_ROOTS).issubset(root_ids) or "root-173730001-0012" in root_ids or "root-173730001-0013" in root_ids:
        raise QualificationV8Failure("owner root membership does not preserve required multi-choice roots and single-action gaps")
    input_manifest = v7.historical_input_manifest(source_commit)
    root_hashes = _locked_root_hashes(input_manifest)
    root_check = prevalidate_historical_roots(root_ids, root_hashes, source_commit=source_commit)
    if root_check["status"] != "PASS":
        raise QualificationV8Failure(f"original owner root receipts do not match the v8 locked manifest: {root_check['issues'][:5]}")

    parent_lock = json.loads(v7._lock_paths(v7.V7_CAPTURE_LOCK_OUT)[0].read_text(encoding="utf-8"))
    config = json.loads(v7._lock_paths(v7.V7_CAPTURE_LOCK_OUT)[1].read_text(encoding="utf-8"))
    plan = json.loads(v7._lock_paths(v7.V7_CAPTURE_LOCK_OUT)[2].read_text(encoding="utf-8"))
    config["schema_id"] = "issue73-checkpoint-replay-qualification-config-v8"
    config["source_qualification_version"] = "v8"
    config["source_history_attempt_key"] = ATTEMPT_KEY
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
    config["resource_caps"] = {
        **projection,
        "v8_resource_ledger_sha256": sha256_text_file(LEDGER_PATH),
        "v8_static_debit_cpu_seconds": STATIC_RESERVE,
        "no_prior_charge_reclaimed": True,
    }
    plan["schema_id"] = "issue73-checkpoint-replay-gate-c-plan-v8"
    plan["source_qualification_version"] = "v8"
    plan["qualification_config_file"] = CONFIG_PATH.relative_to(STUDY).as_posix()
    plan["root_membership_source"] = "original debug-owner attempt.root_ids; exact set comparison before root fidelity"
    plan["single_action_root_gaps"] = ["root-173730001-0012", "root-173730001-0013"]
    runner.atomic_json(MANIFEST_PATH, input_manifest)
    runner.atomic_json(CONFIG_PATH, config)
    plan["qualification_config_sha256"] = sha256_text_file(CONFIG_PATH)
    runner.atomic_json(PLAN_PATH, plan)
    source_hashes = _source_hashes(source_commit)
    lock = {
        "schema_id": "issue73-checkpoint-replay-preseed-source-lock-v8",
        "lock_revision": 9,
        "status": "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION",
        "study_only": True,
        "formal_training_seed_consumed": False,
        "formal_evaluation_seed_consumed": False,
        "stage1_authorized": False,
        "code_source_commit": source_commit,
        "remote_branch": BRANCH,
        "authorized_source_base_commit": SOURCE_BASE_COMMIT,
        "remote_must_equal_current_evidence_head_before_any_run": True,
        "parent_v7_lock_sha256": sha256_text_file(v7._lock_paths(v7.V7_CAPTURE_LOCK_OUT)[0]),
        "parent_v7_code_source_commit": expected_base["code_source_commit"],
        "historical_input_base_commit": v7.HISTORICAL_INPUT_BASE_COMMIT,
        "historical_input_manifest": MANIFEST_PATH.relative_to(STUDY).as_posix(),
        "historical_input_manifest_sha256": _manifest_digest(input_manifest),
        "historical_input_sha256": _locked_root_hashes(input_manifest),
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
        "v7_source_lock_sha256": sha256_text_file(v7._lock_paths(v7.V7_CAPTURE_LOCK_OUT)[0]),
        "v7_resource_ledger_sha256": sha256_text_file(v7.RESOURCE_LEDGER_PATH),
        "v8_resource_ledger_sha256": sha256_text_file(LEDGER_PATH),
        "static_reservation_receipt_sha256": sha256_text_file(STATIC_PATH),
        "resource_projection": projection,
        "fixed_gate_contract": parent_lock["fixed_gate_contract"],
        "source_lock_created_before_any_new_native_execution": True,
    }
    runner.atomic_json(LOCK_PATH, lock)
    push_v8("Issue 73: commit prospective v8 source, provenance, roots, and resource lock")
    return lock


def verify_lock_v8(*, require_remote_current: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    files = (LOCK_PATH, CONFIG_PATH, PLAN_PATH, MANIFEST_PATH)
    if not all(path.is_file() for path in files):
        raise QualificationV8Failure("prospective v8 lock/config/plan/manifest is missing or partial")
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if lock.get("schema_id") != "issue73-checkpoint-replay-preseed-source-lock-v8" or lock.get("lock_revision") != 9:
        raise QualificationV8Failure("only the prospective v8 lock revision 9 is accepted")
    if lock.get("status") != "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION" or lock.get("stage1_authorized") is not False:
        raise QualificationV8Failure("v8 lock status or Stage 1 prohibition changed")
    if lock.get("remote_branch") != BRANCH or lock.get("authorized_source_base_commit") != SOURCE_BASE_COMMIT:
        raise QualificationV8Failure("v8 lock is not on the authorized exact source branch/base")
    if sha256_text_file(CONFIG_PATH) != lock.get("qualification_config_sha256"):
        raise QualificationV8Failure("v8 config differs from its prospective source lock")
    if sha256_text_file(PLAN_PATH) != lock.get("gate_c_plan_sha256"):
        raise QualificationV8Failure("v8 Gate C plan differs from its prospective source lock")
    if sha256_text_file(MANIFEST_PATH) != lock.get("historical_input_manifest_sha256"):
        raise QualificationV8Failure("v8 historical input manifest differs from its prospective source lock")
    if _manifest_digest(manifest) != lock.get("historical_input_manifest_sha256"):
        raise QualificationV8Failure("v8 manifest canonical digest differs from its lock")
    if config.get("historical_input_manifest_sha256") != lock.get("historical_input_manifest_sha256"):
        raise QualificationV8Failure("v8 config and lock disagree on historical input membership")
    if plan.get("qualification_config_sha256") != lock.get("qualification_config_sha256"):
        raise QualificationV8Failure("v8 Gate C plan is not bound to the locked config")
    if config.get("replay_provenance") != lock.get("replay_provenance"):
        raise QualificationV8Failure("v8 config and lock replay provenance differ")
    if config.get("owner_root_ids") != lock.get("owner_root_ids") or plan.get("single_action_root_gaps") != ["root-173730001-0012", "root-173730001-0013"]:
        raise QualificationV8Failure("v8 exact root membership or single-action gap record changed")
    source_commit = str(lock.get("code_source_commit", ""))
    current_head = require_current_v8_branch(require_pushed=require_remote_current)
    command, env = runner.git_invocation(REPOSITORY)
    if subprocess.run(
        [*command, "merge-base", "--is-ancestor", source_commit, current_head], check=False, env=env,
    ).returncode:
        raise QualificationV8Failure("v8 branch no longer descends from its locked code source")
    expected_input = v7.historical_input_manifest(source_commit)
    if expected_input.get("included") != manifest.get("included") or expected_input.get("excluded") != manifest.get("excluded"):
        raise QualificationV8Failure("v8 historical input membership/hash map differs from the exact registered Git-tree set")
    if lock.get("study_source_sha256") != _source_hashes(source_commit):
        raise QualificationV8Failure("v8 study source hashes differ from exact committed source blobs")
    if (
        lock.get("v7_source_lock_sha256") != sha256_text_file(v7._lock_paths(v7.V7_CAPTURE_LOCK_OUT)[0])
        or lock.get("v7_resource_ledger_sha256") != sha256_text_file(v7.RESOURCE_LEDGER_PATH)
    ):
        raise QualificationV8Failure("immutable v7 source lock or resource ledger changed after v8 registration")
    _reservation, ledger, projection = _ensure_static_reservation(allow_started=True)
    if lock.get("v8_resource_ledger_sha256") != sha256_text_file(LEDGER_PATH):
        # The lock binds the pre-execution ledger state. Only the fixed capture attempt may be appended later.
        capture_rows = [x for x in ledger.get("attempts", []) if x.get("key") == ATTEMPT_KEY]
        if len(capture_rows) != 1 or capture_rows[0].get("source_lock_sha256") != sha256_text_file(LOCK_PATH):
            raise QualificationV8Failure("v8 resource ledger changed outside its uniquely keyed capture attempt")
    if not projection["forecast_fits_registered_caps"]:
        raise QualificationV8Failure("v8 resource continuation no longer fits the registered ceilings")
    historical = prevalidate_historical_roots(lock.get("owner_root_ids", []), lock.get("historical_input_sha256", {}), source_commit=source_commit)
    if historical.get("status") != "PASS":
        raise QualificationV8Failure(f"v8 named historical roots failed locked-hash prevalidation: {historical.get('issues')[:5]}")
    v7.verify_lock_v7(require_remote_current=False, lock_out=v7.V7_CAPTURE_LOCK_OUT, lock_revision=8)
    return lock, config


def _prepared_capture_runtime_v8() -> tuple[dict[str, Any], dict[str, Any], Path, str]:
    lock, config = verify_lock_v8()
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
        raise QualificationV8Failure("v8 capture preparation loaded a different native module or binary")
    provenance = lock["replay_provenance"]
    if prepared["model_digest"] != provenance["model_digest"]:
        raise QualificationV8Failure("loaded policy digest differs from the exact historical owner sampling provenance")
    if prepared["adapter_class"].__name__ != "LightSpeedAdapter":
        raise QualificationV8Failure("capture prerequisite path returned an unexpected adapter class")
    return lock, prepared, module_path, module_hash


def _run_static_tests() -> dict[str, Any]:
    loader = unittest.defaultTestLoader
    suite = loader.discover(start_dir=str(STUDY), pattern="test_source_history_capture_v8.py", top_level_dir=str(STUDY))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {
        "status": "PASS" if result.wasSuccessful() and result.testsRun > 0 else "FAIL",
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "output": stream.getvalue()[-12000:],
    }


def qualify_static_v8() -> dict[str, Any]:
    if STATIC_REPORT_PATH.is_file():
        old = json.loads(STATIC_REPORT_PATH.read_text(encoding="utf-8"))
        if old.get("status") == "PASS":
            return old
        raise QualificationV8Failure("v8 static report exists without PASS; no retry")
    if STATIC_FAILURE_PATH.exists():
        raise QualificationV8Failure("v8 static failure receipt is immutable; no retry")
    if any(path.exists() for path in (LOCK_PATH, CONFIG_PATH, PLAN_PATH, MANIFEST_PATH)):
        raise QualificationV8Failure("partial v8 source lock artifacts already exist; preserve and do not reconstruct")
    started_cpu = q.process_tree_cpu()
    started_wall = time.monotonic()
    try:
        q.install_process_cpu_ceiling(STATIC_RESERVE)
        source_commit = require_current_v8_branch()
        _reserve_static()
        lock = prepare_lock_v8(code_source_commit=source_commit)
        lock, config = verify_lock_v8()
        tests = _run_static_tests()
        if tests["status"] != "PASS":
            raise QualificationV8Failure(f"v8 provenance matcher unit tests failed: {tests['output'][-3000:]}")
        runtime_lock, prepared, module_path, module_hash = _prepared_capture_runtime_v8()
        measured = max(0.0, q.process_tree_cpu() - started_cpu)
        if measured > STATIC_RESERVE:
            raise QualificationV8Failure("v8 non-native static qualification exceeded its fixed 60 CPU-s reservation")
        report = {
            "schema_id": "issue73-v8-static-qualification-v1",
            "status": "PASS",
            "branch": BRANCH,
            "code_source_commit": lock["code_source_commit"],
            "source_lock_sha256": sha256_text_file(LOCK_PATH),
            "qualification_config_sha256": sha256_text_file(CONFIG_PATH),
            "gate_c_plan_sha256": sha256_text_file(PLAN_PATH),
            "historical_input_manifest_sha256": sha256_text_file(MANIFEST_PATH),
            "v7_source_lock_sha256": lock["v7_source_lock_sha256"],
            "v7_resource_ledger_sha256": lock["v7_resource_ledger_sha256"],
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
            raise QualificationV8Failure("capture runtime no longer matches the v8 locked source or policy provenance")
        runner.atomic_json(STATIC_REPORT_PATH, report)
        receipt, ledger, _projection = _ensure_static_reservation(allow_started=True)
        push_v8("Issue 73: preserve passing v8 static provenance and capture-runtime qualification")
        return report
    except BaseException as exc:
        measured = max(0.0, q.process_tree_cpu() - started_cpu)
        failure = {
            "schema_id": "issue73-v8-static-qualification-failure-v1",
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
            push_v8("Issue 73: preserve terminal v8 static qualification failure")
        except BaseException:
            pass
        raise


def _require_reviewed_source_sha(reviewed_source_sha: str, reviewer_pass_reference: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", str(reviewed_source_sha).lower()):
        raise QualificationV8Failure("native v8 capture requires exact 40-character Reviewer-approved source SHA")
    if not reviewer_pass_reference.startswith("https://github.com/lsmfttb/spire-research/issues/73#issuecomment-"):
        raise QualificationV8Failure("native v8 capture requires the exact independent Reviewer PASS issue-comment reference")
    head = require_current_v8_branch()
    if reviewed_source_sha.lower() != head:
        raise QualificationV8Failure("Reviewer-approved SHA differs from exact local and remote v8 branch head")
    return head


def _start_capture_attempt_v8(lock: Mapping[str, Any], reviewed_source_sha: str, reviewer_reference: str) -> dict[str, Any]:
    if ATTEMPT_PATH.exists():
        previous = json.loads(ATTEMPT_PATH.read_text(encoding="utf-8"))
        if previous.get("status") == "STARTED":
            previous.update({"status": "INTERRUPTED_NO_RERUN", "cpu_charged_seconds": CAPTURE_RESERVE, "failure": "v8 capture had no terminal receipt; fixed native attempt cannot be repeated"})
            ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
            ledger["attempts"] = [previous if x.get("key") == ATTEMPT_KEY else x for x in ledger["attempts"]]
            runner.atomic_json(ATTEMPT_PATH, previous)
            runner.atomic_json(LEDGER_PATH, ledger)
            push_v8("Issue 73: preserve interrupted v8 native capture without retry")
            raise QualificationV8Failure("v8 native capture was interrupted; no retry is permitted")
        raise QualificationV8Failure(f"v8 fixed capture already has terminal receipt {previous.get('status')}; no retry")
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    static, _ledger, projection = _ensure_static_reservation()
    if static.get("status") != "RESERVED_CONSERVATIVE_STATIC_DEBIT" or not STATIC_REPORT_PATH.is_file() or json.loads(STATIC_REPORT_PATH.read_text(encoding="utf-8")).get("status") != "PASS":
        raise QualificationV8Failure("v8 capture requires the committed static PASS receipt")
    builder, native = q.charged(v7._v7_resource_ledger())
    builder += sum(float(x.get("cpu_charged_seconds", 0)) for x in ledger.get("attempts", []))
    native += sum(float(x.get("cpu_charged_seconds", 0)) for x in ledger.get("attempts", []) if x.get("native_gate_bc") is True)
    if builder + CAPTURE_RESERVE > 4800.0 or native + CAPTURE_RESERVE > 2760.0:
        raise QualificationV8Failure("v8 fixed capture reserve exceeds the existing Builder or native CPU ceiling")
    row = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": ATTEMPT_KEY,
        "kind": "v8_historical_debug_owner_full_action_history_capture",
        "status": "STARTED",
        "native_gate_bc": True,
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
    push_v8(f"Issue 73: start uniquely authorized v8 historical replay capture {ATTEMPT_KEY}")
    return row


def capture_source_history_v8(*, reviewed_source_sha: str, reviewer_pass_reference: str) -> dict[str, Any]:
    """Perform only the one reviewed 60-second owner replay; no gates/suffixes run here."""
    current_source_sha = _require_reviewed_source_sha(reviewed_source_sha, reviewer_pass_reference)
    lock, _config = verify_lock_v8()
    if not STATIC_REPORT_PATH.is_file() or json.loads(STATIC_REPORT_PATH.read_text(encoding="utf-8")).get("status") != "PASS":
        raise QualificationV8Failure("v8 native capture requires a committed static qualification PASS")
    owner, owner_attempt = q.historical_run_receipts()
    root_preflight = prevalidate_historical_roots(lock["owner_root_ids"], lock["historical_input_sha256"], source_commit=lock["code_source_commit"])
    if root_preflight.get("status") != "PASS":
        raise QualificationV8Failure(f"all named owner roots must pass hash prevalidation before capture: {root_preflight['issues'][:5]}")
    provenance = original_replay_identity(owner)
    if replay_provenance_mismatches(provenance, lock["replay_provenance"]):
        raise QualificationV8Failure("historical owner replay provenance differs from v8 precommit")
    # All code/source/model/adapter-class prerequisites finish before the fixed attempt is started.
    _runtime_lock, prepared, _module_path, _module_hash = _prepared_capture_runtime_v8()
    if prepared["model_digest"] != provenance["model_digest"]:
        raise QualificationV8Failure("live loaded policy digest differs from the original owner")
    if CAPTURE_REPORT_PATH.exists() or CAPTURE_FAILURE_PATH.exists() or CAPTURE_HISTORY_PATH.exists():
        raise QualificationV8Failure("v8 capture output already exists without the unique attempt receipt; no retry")
    q.install_process_cpu_ceiling(int(CAPTURE_RESERVE))
    _start_capture_attempt_v8(lock, current_source_sha, reviewer_pass_reference)
    started_cpu = q.process_tree_cpu()
    adapter = None
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
            resource_label=f"SOURCE_QUAL_V8_{SEED}",
            adapter_override=adapter,
            durable=False,
            capture_trace=False,
            capture_action_history=True,
            cpu_cap_seconds=remaining,
        )
        roots = list(result.get("_volatile_roots", []))
        history = list(result.get("_actual_action_history", []))
        report = compare_capture_run_v8(
            result, roots, history, owner=owner, owner_attempt=owner_attempt,
            historical_roots=root_preflight["roots"],
        )
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
            "formal_seed_consumed": False,
        })
        runner.atomic_json(CAPTURE_HISTORY_PATH, {
            "schema_id": "issue73-actual-action-step-history-v8-v1",
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
        report["history_sha256"] = sha256_file(CAPTURE_HISTORY_PATH)
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
            "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        ledger["attempts"] = [row if x.get("key") == ATTEMPT_KEY else x for x in ledger["attempts"]]
        runner.atomic_json(ATTEMPT_PATH, row)
        runner.atomic_json(LEDGER_PATH, ledger)
        push_v8("Issue 73: preserve uniquely authorized v8 historical owner replay evidence")
        report["attempt_receipt"] = row
        return report
    except BaseException as exc:
        if adapter is not None:
            try:
                adapter.close()
            except BaseException:
                pass
        failure = {
            "schema_id": "issue73-source-history-capture-failure-v8-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1800],
            "reviewed_source_sha": current_source_sha,
            "reviewer_pass_reference": reviewer_pass_reference,
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
            push_v8("Issue 73: preserve terminal v8 historical owner replay failure")
        except BaseException:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("qualify-static", "verify-lock", "capture"))
    parser.add_argument("--reviewed-source-sha")
    parser.add_argument("--reviewer-pass-reference")
    args = parser.parse_args(argv)
    if args.command == "qualify-static":
        result = qualify_static_v8()
    elif args.command == "verify-lock":
        lock, _config = verify_lock_v8()
        result = {"status": "PASS", "source_lock_sha256": sha256_text_file(LOCK_PATH), "code_source_commit": lock["code_source_commit"]}
    else:
        if not args.reviewed_source_sha or not args.reviewer_pass_reference:
            raise QualificationV8Failure("capture requires --reviewed-source-sha and --reviewer-pass-reference")
        result = capture_source_history_v8(reviewed_source_sha=args.reviewed_source_sha, reviewer_pass_reference=args.reviewer_pass_reference)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
