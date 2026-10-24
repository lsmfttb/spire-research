"""Prospective v9 engineering-6 lock and parity repair for Issue 73.

The completed engineering-5 capture is consumed only as an immutable parent
receipt. This version does not replay or replace that capture.
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence

import checkpoint_replay_qualification as q
import runner
import source_qualification_v9_engineering_5 as parent

STUDY = parent.STUDY
REPOSITORY = parent.REPOSITORY
STUDY_RELATIVE = parent.STUDY_RELATIVE
BRANCH = parent.BRANCH
SOURCE_BASE_COMMIT = parent.SOURCE_BASE_COMMIT
OUT = STUDY / "receipts" / "source-qualification-v9-engineering-6"
OUT_RELATIVE = OUT.relative_to(REPOSITORY).as_posix()
RUNTIME_TEMP_ROOT = REPOSITORY / ".builder-runtime" / "engineering-6"
LOCK_PATH = OUT / "preseed-source-lock-v9-engineering-6.json"
STATIC_KEY = "builder-prelock-static-verification-v9-engineering-6"
STATIC_PATH = OUT / "attempts" / f"{STATIC_KEY}.json"
STATIC_REPORT_PATH = OUT / "reports" / "static-qualification-report-v9-engineering-6.json"
STATIC_FAILURE_PATH = OUT / "reports" / "static-qualification-failure-v9-engineering-6.json"
LEDGER_PATH = OUT / "resource-ledger-v9-engineering-6.json"
LEDGER_SNAPSHOT_PATH = OUT / "ledgers" / "resource-ledger-before-v9-engineering-6.json"
# The original 80 CPU-s parity was never started. Preserve its registered key.
PARITY_ATTEMPT_KEY = parent.PARITY_ATTEMPT_KEY
PARITY_ATTEMPT_PATH = OUT / "attempts" / f"{PARITY_ATTEMPT_KEY}.json"
PARITY_REPORT_PATH = OUT / "reports" / "fresh-process-prefix-parity-report-v9-engineering-6.json"
PARITY_FAILURE_PATH = OUT / "reports" / "fresh-process-prefix-parity-failure-v9-engineering-6.json"
STATIC_RESERVE = 60.0
PARITY_RESERVE = 80.0
NEW_NONNATIVE_POOL = parent.NEW_NONNATIVE_POOL
CAPTURE_REPORT_PATH = parent.CAPTURE_REPORT_PATH
CAPTURE_ATTEMPT_PATH = parent.ATTEMPT_PATH
CAPTURE_HISTORY_PATH = parent.CAPTURE_HISTORY_PATH
CAPTURE_ROOTS_PATH = parent.CAPTURE_ROOTS_PATH
NEW_SOURCE_NAMES = (
    "source_qualification_v9_engineering_6.py",
    "test_source_history_capture_v9_engineering_6.py",
)
_PARENT_LOCK_VERIFIED = False


class QualificationV9Engineering6Failure(RuntimeError):
    pass


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text_file(path: Path) -> str:
    return parent.sha256_text_file(path)


def canonical(value: Any) -> str:
    return runner.canonical(value)


def git(*args: str) -> str:
    return q.git(*args)


def push_v9(message: str) -> str:
    git("add", "--", OUT_RELATIVE)
    staged = [x for x in git("diff", "--cached", "--name-only").splitlines() if x]
    if any(x != OUT_RELATIVE and not x.startswith(f"{OUT_RELATIVE}/") for x in staged):
        raise QualificationV9Engineering6Failure("refusing to stage files outside the engineering-6 evidence directory")
    command, env = runner.git_invocation(REPOSITORY)
    if subprocess.run([*command, "diff", "--cached", "--quiet"], check=False, env=env).returncode:
        git("commit", "-m", message)
    head = git("rev-parse", "HEAD")
    git("push", "origin", f"HEAD:refs/heads/{BRANCH}")
    remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    if not remote or remote[0] != head:
        raise QualificationV9Engineering6Failure("remote does not contain the exact engineering-6 evidence commit")
    return head


def require_current_branch(*, require_pushed: bool = True) -> str:
    head = git("rev-parse", "HEAD")
    if git("branch", "--show-current") != BRANCH:
        raise QualificationV9Engineering6Failure(f"expected branch {BRANCH}")
    command, env = runner.git_invocation(REPOSITORY)
    if subprocess.run(
        [*command, "merge-base", "--is-ancestor", SOURCE_BASE_COMMIT, head],
        check=False, env=env,
    ).returncode:
        raise QualificationV9Engineering6Failure("engineering-6 no longer descends from the authorized source base")
    if require_pushed:
        remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
        if not remote or remote[0] != head:
            raise QualificationV9Engineering6Failure("engineering-6 source/evidence is not at the exact pushed branch head")
    return head


def capture_receipt_mismatches_v9(
    capture_report: Mapping[str, Any],
    capture_attempt: Mapping[str, Any],
    history_record: Mapping[str, Any],
    roots_record: Mapping[str, Any],
    *,
    parent_lock: Mapping[str, Any],
    parent_lock_sha256: str,
    roots_path: str,
    roots_sha256: str,
    history_sha256: str,
) -> list[str]:
    history = history_record.get("events", [])
    roots = roots_record.get("roots", [])
    checks = {
        "capture_report_status": capture_report.get("status") == "PASS",
        "capture_transition_witnesses": capture_report.get("transition_witness_integrity", {}).get("complete_pass") is True,
        "capture_report_lock": capture_report.get("source_lock_sha256") == parent_lock_sha256,
        "history_schema": history_record.get("schema_id") == "issue73-actual-action-step-history-v9-v1",
        "history_lock": history_record.get("source_lock_sha256") == parent_lock_sha256,
        "history_source_commit": history_record.get("code_source_commit") == parent_lock.get("code_source_commit"),
        "history_event_count": history_record.get("event_count") == len(history) == capture_report.get("history_event_count") == capture_attempt.get("history_event_count"),
        "history_digest": history_sha256 == capture_report.get("history_sha256"),
        "capture_roots_path": capture_report.get("root_snapshots_path") == roots_path,
        "capture_roots_digest": capture_report.get("root_snapshots_sha256") == roots_sha256 == capture_attempt.get("root_snapshots_sha256"),
        "capture_attempt_terminal": capture_attempt.get("status") == "COMPLETE" and capture_attempt.get("report_status") == "PASS",
        "capture_attempt_lock": capture_attempt.get("source_lock_sha256") == parent_lock_sha256,
        "capture_root_count": roots_record.get("schema_id") == "issue73-volatile-root-snapshots-v9-v1" and roots_record.get("root_count") == len(roots) == 39 and capture_report.get("captured_root_count") == 39,
    }
    mismatches = [name for name, valid in checks.items() if not valid]
    expected_roots = [str(x) for x in parent_lock.get("owner_root_ids", [])]
    observed_roots = [str(row.get("root_id", "")) for row in roots]
    if len(expected_roots) != 39 or len(set(expected_roots)) != 39 or set(observed_roots) != set(expected_roots) or len(observed_roots) != len(set(observed_roots)):
        mismatches.append("capture_root_membership")
    return mismatches


def _parent_capture_values() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    parent_lock = json.loads(parent.LOCK_PATH.read_text(encoding="utf-8"))
    capture_report = json.loads(CAPTURE_REPORT_PATH.read_text(encoding="utf-8"))
    capture_attempt = json.loads(CAPTURE_ATTEMPT_PATH.read_text(encoding="utf-8"))
    history_record = json.loads(CAPTURE_HISTORY_PATH.read_text(encoding="utf-8"))
    roots_record = json.loads(CAPTURE_ROOTS_PATH.read_text(encoding="utf-8"))
    return parent_lock, capture_report, capture_attempt, history_record, roots_record


def validate_parent_capture_v9() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    paths = (parent.LOCK_PATH, parent.STATIC_REPORT_PATH, CAPTURE_REPORT_PATH, CAPTURE_ATTEMPT_PATH, CAPTURE_HISTORY_PATH, CAPTURE_ROOTS_PATH)
    if not all(path.is_file() for path in paths):
        raise QualificationV9Engineering6Failure("completed engineering-5 source lock/capture receipts are missing")
    parent_lock, capture_report, capture_attempt, history_record, roots_record = _parent_capture_values()
    parent_lock_sha = sha256_text_file(parent.LOCK_PATH)
    roots_path = CAPTURE_ROOTS_PATH.relative_to(STUDY).as_posix()
    roots_sha = sha256_text_file(CAPTURE_ROOTS_PATH)
    mismatches = capture_receipt_mismatches_v9(
        capture_report, capture_attempt, history_record, roots_record,
        parent_lock=parent_lock,
        parent_lock_sha256=parent_lock_sha,
        roots_path=roots_path,
        roots_sha256=roots_sha,
        history_sha256=sha256_text_file(CAPTURE_HISTORY_PATH),
    )
    if mismatches:
        raise QualificationV9Engineering6Failure("engineering-5 capture receipts do not match their own lock: " + ", ".join(mismatches))
    config = json.loads(parent.CONFIG_PATH.read_text(encoding="utf-8"))
    integrity = parent.transition_witness_integrity_v9(
        capture_report.get("transition_witnesses", {}),
        config["gate_c_transition_pairs"],
    )
    if not integrity.get("complete_pass"):
        raise QualificationV9Engineering6Failure("engineering-5 transition witnesses are incomplete")
    if json.loads(parent.STATIC_REPORT_PATH.read_text(encoding="utf-8")).get("status") != "PASS":
        raise QualificationV9Engineering6Failure("engineering-5 static source-lock report is not PASS")
    if any(path.exists() for path in (parent.PARITY_ATTEMPT_PATH, parent.PARITY_REPORT_PATH, parent.PARITY_FAILURE_PATH)):
        raise QualificationV9Engineering6Failure("the registered parity already has a receipt; do not duplicate or rerun")
    return parent_lock, capture_report, capture_attempt, history_record, roots_record


def _base_prior_ledger() -> dict[str, Any]:
    global _PARENT_LOCK_VERIFIED
    if not _PARENT_LOCK_VERIFIED:
        parent.verify_lock_v9(require_remote_current=False)
        _PARENT_LOCK_VERIFIED = True
    receipt, ledger, _projection = parent._ensure_static_reservation(allow_started=True)
    if receipt.get("status") != "RESERVED_CONSERVATIVE_STATIC_DEBIT":
        raise QualificationV9Engineering6Failure("engineering-5 static reservation is not immutable")
    if ledger.get("schema_id") != "issue73-v9-engineering-5-resource-ledger-v1":
        raise QualificationV9Engineering6Failure("engineering-5 cumulative resource ledger schema changed")
    attempt = json.loads(CAPTURE_ATTEMPT_PATH.read_text(encoding="utf-8"))
    if attempt.get("status") != "COMPLETE" or attempt.get("report_status") != "PASS":
        raise QualificationV9Engineering6Failure("engineering-5 capture is not the completed PASS receipt")
    validate_parent_capture_v9()
    return parent._base_prior_v9_ledger()


def _ledger_template(base: Mapping[str, Any]) -> dict[str, Any]:
    parent_ledger = json.loads(parent.LEDGER_PATH.read_text(encoding="utf-8"))
    return {
        "schema_id": "issue73-v9-engineering-6-resource-ledger-v1",
        "baseline_ledger_path": parent.LEDGER_PATH.relative_to(STUDY).as_posix(),
        "baseline_ledger_sha256": sha256_text_file(parent.LEDGER_PATH),
        "baseline_builder_charge_cpu_seconds": q.charged(base)[0],
        "baseline_native_gate_bc_charge_cpu_seconds": q.charged(base)[1],
        "builder_ceiling_cpu_seconds": 4800.0,
        "native_gate_bc_ceiling_cpu_seconds": 3060.0,
        "reviewer_reserve_cpu_seconds": 1200.0,
        "aggregate_stage0_cpu_seconds": 6000.0,
        "parent_engineering_5_source_lock_sha256": sha256_text_file(parent.LOCK_PATH),
        "parent_engineering_5_static_report_sha256": sha256_text_file(parent.STATIC_REPORT_PATH),
        "parent_engineering_5_capture_report_sha256": sha256_text_file(parent.CAPTURE_REPORT_PATH),
        "parent_engineering_5_capture_attempt_sha256": sha256_text_file(parent.ATTEMPT_PATH),
        "attempts": json.loads(json.dumps(parent_ledger.get("attempts", []))),
    }


def _resource_projection(base_ledger: Mapping[str, Any], ledger: Mapping[str, Any]) -> dict[str, Any]:
    return parent._resource_projection(base_ledger, ledger)


def _ensure_static_reservation() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    base = _base_prior_ledger()
    if not all(path.is_file() for path in (STATIC_PATH, LEDGER_PATH, LEDGER_SNAPSHOT_PATH)):
        raise QualificationV9Engineering6Failure("engineering-6 fixed 60 CPU-s static debit is missing")
    receipt = json.loads(STATIC_PATH.read_text(encoding="utf-8"))
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    snapshot = json.loads(LEDGER_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    expected_snapshot = _ledger_template(base)
    static_rows = [row for row in ledger.get("attempts", []) if row.get("key") == STATIC_KEY]
    parity_rows = [row for row in ledger.get("attempts", []) if row.get("key") == PARITY_ATTEMPT_KEY]
    if (
        snapshot != expected_snapshot
        or ledger.get("schema_id") != "issue73-v9-engineering-6-resource-ledger-v1"
        or ledger.get("baseline_ledger_sha256") != sha256_text_file(parent.LEDGER_PATH)
        or receipt.get("key") != STATIC_KEY
        or receipt.get("reserved_cpu_seconds") != STATIC_RESERVE
        or receipt.get("cpu_charged_seconds") != STATIC_RESERVE
        or receipt.get("native_gate_bc") is not False
        or receipt.get("contingency_pool") != NEW_NONNATIVE_POOL
        or receipt.get("formal_seed_consumed") is not False
        or static_rows != [receipt]
        or ledger.get("attempts") != expected_snapshot["attempts"] + [receipt] + parity_rows
    ):
        raise QualificationV9Engineering6Failure("engineering-6 static receipt, snapshot, or cumulative parent ledger is inconsistent")
    if receipt.get("status") != "RESERVED_CONSERVATIVE_STATIC_DEBIT":
        raise QualificationV9Engineering6Failure("engineering-6 fixed static receipt changed from its immutable precharge")
    projection = _resource_projection(base, ledger)
    if not projection.get("forecast_fits_registered_caps"):
        raise QualificationV9Engineering6Failure(f"engineering-6 projection exceeds registered caps: {projection}")
    return receipt, ledger, projection


def _verify_static_report(lock: Mapping[str, Any]) -> None:
    if not STATIC_REPORT_PATH.is_file():
        raise QualificationV9Engineering6Failure("engineering-6 static qualification report is missing")
    report = json.loads(STATIC_REPORT_PATH.read_text(encoding="utf-8"))
    if (
        report.get("schema_id") != "issue73-v9-engineering-6-static-qualification-report-v1"
        or report.get("engineering_revision") != 6
        or report.get("status") != "PASS"
        or report.get("source_lock_sha256") != sha256_text_file(LOCK_PATH)
        or report.get("code_source_commit") != lock.get("code_source_commit")
        or report.get("fixed_static_reservation_cpu_seconds") != STATIC_RESERVE
        or report.get("stage1_authorized") is not False
        or report.get("no_capture_replay_or_formal_seed") is not True
        or report.get("runtime", {}).get("adapter_instantiated") is not False
        or report.get("runtime", {}).get("game_step_started") is not False
        or report.get("runtime", {}).get("formal_seed_consumed") is not False
    ):
        raise QualificationV9Engineering6Failure("engineering-6 static PASS receipt differs from its lock or safe-execution contract")


def _reserve_static() -> dict[str, Any]:
    if any(path.exists() for path in (STATIC_PATH, LEDGER_PATH, LEDGER_SNAPSHOT_PATH, LOCK_PATH, STATIC_REPORT_PATH, STATIC_FAILURE_PATH)):
        raise QualificationV9Engineering6Failure("engineering-6 static attempt exists or is partial; no retry/reconstruction")
    head = require_current_branch()
    base = _base_prior_ledger()
    snapshot = _ledger_template(base)
    candidate = json.loads(json.dumps(snapshot))
    candidate["attempts"].append({
        "key": STATIC_KEY,
        "cpu_charged_seconds": STATIC_RESERVE,
        "native_gate_bc": False,
        "contingency_pool": NEW_NONNATIVE_POOL,
    })
    projection = _resource_projection(base, candidate)
    if not projection.get("forecast_fits_registered_caps"):
        raise QualificationV9Engineering6Failure(f"engineering-6 fixed debit exceeds registered caps: {projection}")
    row = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": STATIC_KEY,
        "kind": "v9_engineering_6_parent_capture_lock_and_runtime_verification",
        "status": "RESERVED_CONSERVATIVE_STATIC_DEBIT",
        "native_gate_bc": False,
        "contingency_pool": NEW_NONNATIVE_POOL,
        "reserved_cpu_seconds": STATIC_RESERVE,
        "cpu_charged_seconds": STATIC_RESERVE,
        "measurement_status": "FIXED_RESERVATION_PRECHARGED; NO REFUND",
        "source_commit": head,
        "source_lock_sha256": None,
        "formal_seed_consumed": False,
        "no_native_attempt_or_seed": True,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    snapshot["attempts"].append(row)
    runner.atomic_json(LEDGER_SNAPSHOT_PATH, _ledger_template(base))
    runner.atomic_json(LEDGER_PATH, snapshot)
    runner.atomic_json(STATIC_PATH, row)
    push_v9("Issue 73: precharge v9 engineering-6 static source verification")
    return row


def _new_source_hashes(source_commit: str) -> dict[str, str]:
    paths = [f"{STUDY_RELATIVE}/{name}" for name in NEW_SOURCE_NAMES]
    blobs = parent.v7.external_git_blobs(source_commit, paths)
    hashes: dict[str, str] = {}
    for name in NEW_SOURCE_NAMES:
        blob = blobs[f"{STUDY_RELATIVE}/{name}"]
        checkout = STUDY / name
        if not checkout.is_file():
            raise QualificationV9Engineering6Failure(f"engineering-6 source missing from checkout: {name}")
        q.verify_locked_source_payload(
            label=f"study/{name}", expected_sha256=sha256_bytes(blob),
            committed_blob=blob, checkout_bytes=checkout.read_bytes(),
        )
        hashes[name] = sha256_bytes(blob)
    return hashes


def prepare_lock_v9(*, code_source_commit: str | None = None) -> dict[str, Any]:
    base = _base_prior_ledger()
    _static, _ledger, projection = _ensure_static_reservation()
    source_commit = code_source_commit or require_current_branch()
    head = require_current_branch()
    command, env = runner.git_invocation(REPOSITORY)
    if source_commit != head and subprocess.run(
        [*command, "merge-base", "--is-ancestor", source_commit, head], check=False, env=env,
    ).returncode:
        raise QualificationV9Engineering6Failure("engineering-6 lock source is not an ancestor of the pushed evidence head")
    remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    if not remote or remote[0] != head:
        raise QualificationV9Engineering6Failure("engineering-6 lock must be prepared at the exact remote head")
    parent_lock, capture_report, capture_attempt, history_record, roots_record = validate_parent_capture_v9()
    lock = {
        "schema_id": "issue73-v9-engineering-6-preseed-source-lock-v1",
        "lock_revision": 15,
        "status": "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION",
        "study_only": True,
        "formal_training_seed_consumed": False,
        "formal_evaluation_seed_consumed": False,
        "stage1_authorized": False,
        "remote_branch": BRANCH,
        "authorized_source_base_commit": SOURCE_BASE_COMMIT,
        "remote_must_equal_current_evidence_head_before_any_run": True,
        "code_source_commit": source_commit,
        "study_source_sha256": _new_source_hashes(source_commit),
        "parent_engineering_5_source_lock_sha256": sha256_text_file(parent.LOCK_PATH),
        "parent_engineering_5_code_source_commit": parent_lock["code_source_commit"],
        "parent_engineering_5_static_report_sha256": sha256_text_file(parent.STATIC_REPORT_PATH),
        "parent_engineering_5_resource_ledger_sha256": sha256_text_file(parent.LEDGER_PATH),
        "parent_engineering_5_capture_attempt_sha256": sha256_text_file(CAPTURE_ATTEMPT_PATH),
        "parent_engineering_5_capture_report_sha256": sha256_text_file(CAPTURE_REPORT_PATH),
        "parent_engineering_5_capture_history_sha256": sha256_text_file(CAPTURE_HISTORY_PATH),
        "parent_engineering_5_capture_roots_sha256": sha256_text_file(CAPTURE_ROOTS_PATH),
        "parent_engineering_5_capture_source_lock_sha256": capture_report["source_lock_sha256"],
        "capture_history_event_count": history_record["event_count"],
        "capture_root_count": roots_record["root_count"],
        "capture_owner_root_ids": [str(x) for x in parent_lock["owner_root_ids"]],
        "historical_input_manifest_sha256": parent_lock["historical_input_manifest_sha256"],
        "native": parent_lock["native"],
        "stsr": parent_lock["stsr"],
        "fixed_gate_contract": parent_lock["fixed_gate_contract"],
        "stage1_prohibition_inherited": True,
        "static_reservation_receipt_sha256": sha256_text_file(STATIC_PATH),
        "resource_ledger_sha256": sha256_text_file(LEDGER_PATH),
        "parent_resource_ledger_sha256": sha256_text_file(parent.LEDGER_PATH),
        "resource_projection": projection,
        "source_lock_created_before_parity_attempt": True,
        "no_capture_replay_or_formal_seed_authorized": True,
    }
    runner.atomic_json(LOCK_PATH, lock)
    push_v9("Issue 73: lock v9 engineering-6 capture-parent verification")
    return lock


def verify_lock_v9(*, require_remote_current: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    global _PARENT_LOCK_VERIFIED
    if not all(path.is_file() for path in (LOCK_PATH, STATIC_PATH, LEDGER_PATH, LEDGER_SNAPSHOT_PATH)):
        raise QualificationV9Engineering6Failure("engineering-6 source lock or static debit is missing")
    parent.verify_lock_v9(require_remote_current=require_remote_current)
    _PARENT_LOCK_VERIFIED = True
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if (
        lock.get("schema_id") != "issue73-v9-engineering-6-preseed-source-lock-v1"
        or lock.get("lock_revision") != 15
        or lock.get("status") != "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION"
        or lock.get("stage1_authorized") is not False
        or lock.get("formal_training_seed_consumed") is not False
        or lock.get("formal_evaluation_seed_consumed") is not False
        or lock.get("remote_branch") != BRANCH
        or lock.get("authorized_source_base_commit") != SOURCE_BASE_COMMIT
        or lock.get("source_lock_created_before_parity_attempt") is not True
        or lock.get("no_capture_replay_or_formal_seed_authorized") is not True
    ):
        raise QualificationV9Engineering6Failure("engineering-6 source lock scope/revision or Stage 1 prohibition changed")
    head = require_current_branch(require_pushed=require_remote_current)
    source_commit = str(lock.get("code_source_commit", ""))
    command, env = runner.git_invocation(REPOSITORY)
    if subprocess.run([*command, "merge-base", "--is-ancestor", source_commit, head], check=False, env=env).returncode:
        raise QualificationV9Engineering6Failure("engineering-6 source commit is not an ancestor of the evidence branch")
    if lock.get("study_source_sha256") != _new_source_hashes(source_commit):
        raise QualificationV9Engineering6Failure("engineering-6 source differs from its exact committed source blobs")
    parent_lock = json.loads(parent.LOCK_PATH.read_text(encoding="utf-8"))
    capture_report = json.loads(CAPTURE_REPORT_PATH.read_text(encoding="utf-8"))
    if (
        lock.get("parent_engineering_5_source_lock_sha256") != sha256_text_file(parent.LOCK_PATH)
        or lock.get("parent_engineering_5_code_source_commit") != parent_lock.get("code_source_commit")
        or lock.get("parent_engineering_5_static_report_sha256") != sha256_text_file(parent.STATIC_REPORT_PATH)
        or lock.get("parent_engineering_5_resource_ledger_sha256") != sha256_text_file(parent.LEDGER_PATH)
        or lock.get("parent_engineering_5_capture_attempt_sha256") != sha256_text_file(CAPTURE_ATTEMPT_PATH)
        or lock.get("parent_engineering_5_capture_report_sha256") != sha256_text_file(CAPTURE_REPORT_PATH)
        or lock.get("parent_engineering_5_capture_history_sha256") != sha256_text_file(CAPTURE_HISTORY_PATH)
        or lock.get("parent_engineering_5_capture_roots_sha256") != sha256_text_file(CAPTURE_ROOTS_PATH)
        or lock.get("parent_engineering_5_capture_source_lock_sha256") != capture_report.get("source_lock_sha256")
        or lock.get("capture_history_event_count") != capture_report.get("history_event_count")
        or lock.get("capture_root_count") != capture_report.get("captured_root_count")
        or lock.get("capture_owner_root_ids") != parent_lock.get("owner_root_ids")
        or lock.get("native") != parent_lock.get("native")
        or lock.get("stsr") != parent_lock.get("stsr")
        or lock.get("fixed_gate_contract") != parent_lock.get("fixed_gate_contract")
        or lock.get("stage1_prohibition_inherited") is not True
    ):
        raise QualificationV9Engineering6Failure("immutable engineering-5 capture/source-lock parent changed")
    validate_parent_capture_v9()
    static, ledger, projection = _ensure_static_reservation()
    if lock.get("static_reservation_receipt_sha256") != sha256_text_file(STATIC_PATH):
        raise QualificationV9Engineering6Failure("engineering-6 fixed static debit differs from its lock")
    if lock.get("parent_resource_ledger_sha256") != sha256_text_file(parent.LEDGER_PATH):
        raise QualificationV9Engineering6Failure("engineering-5 cumulative ledger differs from the engineering-6 lock")
    if lock.get("resource_ledger_sha256") != sha256_text_file(LEDGER_PATH):
        parity_rows = [row for row in ledger.get("attempts", []) if row.get("key") == PARITY_ATTEMPT_KEY]
        if (
            len(parity_rows) != 1
            or parity_rows[0].get("source_lock_sha256") != sha256_text_file(LOCK_PATH)
            or parity_rows[0].get("reserved_cpu_seconds") != PARITY_RESERVE
            or parity_rows[0].get("cpu_charged_seconds") != PARITY_RESERVE
            or parity_rows[0].get("native_gate_bc") is not True
            or parity_rows[0].get("contingency_pool") != "native"
            or parity_rows[0].get("formal_seed_consumed") is not False
            or parity_rows[0].get("status") not in {"STARTED", "PASS", "FAIL_OR_INCOMPLETE", "FAIL_CPU_RESERVATION_EXCEEDED", "INTERRUPTED_NO_RERUN"}
        ):
            raise QualificationV9Engineering6Failure("engineering-6 ledger changed outside its one locked parity attempt")
    base_ledger = _base_prior_ledger()
    baseline = _ledger_template(base_ledger)
    baseline["attempts"].append(static)
    if lock.get("resource_projection") != _resource_projection(base_ledger, baseline):
        raise QualificationV9Engineering6Failure("engineering-6 source-lock resource projection changed")
    if STATIC_REPORT_PATH.exists():
        _verify_static_report(lock)
    if not projection.get("forecast_fits_registered_caps"):
        raise QualificationV9Engineering6Failure("engineering-6 projection exceeds a registered Builder/native/aggregate cap")
    return lock, json.loads(parent.CONFIG_PATH.read_text(encoding="utf-8"))


def _prepared_capture_runtime_v9() -> tuple[dict[str, Any], dict[str, Any], Path, str]:
    parent_lock = json.loads(parent.LOCK_PATH.read_text(encoding="utf-8"))
    prepared = q.prepare_capture_prerequisites(
        out=OUT / "capture-preflight-runtime",
        source_lock=parent_lock,
        native_module_path_override=parent.NATIVE_MODULE_PATH,
    )
    module_path = Path(prepared["module"].__file__).resolve(strict=True)
    module_hash = runner.validate_native_module_identity(
        module_path,
        expected_name=q.OLD_CONFIG["identity"]["native_module"],
        expected_sha256=q.OLD_CONFIG["identity"]["native_binary_sha256"],
    )
    if str(module_path) != parent_lock["native"]["module_path"] or module_hash != parent_lock["native"]["loaded_module_sha256"]:
        raise QualificationV9Engineering6Failure("engineering-6 runtime loaded a different locked native module")
    if prepared["model_digest"] != parent_lock["replay_provenance"]["model_digest"]:
        raise QualificationV9Engineering6Failure("engineering-6 runtime loaded a different historical policy")
    if prepared["adapter_class"].__name__ != "LightSpeedAdapter":
        raise QualificationV9Engineering6Failure("engineering-6 returned an unexpected adapter class")
    return parent_lock, prepared, module_path, module_hash


def _run_static_tests() -> dict[str, Any]:
    suite = unittest.TestSuite()
    for pattern in (
        "test_source_history_capture_v9.py",
        "test_source_history_capture_v9_engineering_2.py",
        "test_source_history_capture_v9_engineering_4.py",
        "test_source_history_capture_v9_engineering_5.py",
        "test_source_history_capture_v9_engineering_6.py",
    ):
        suite.addTests(unittest.defaultTestLoader.discover(start_dir=str(STUDY), pattern=pattern, top_level_dir=str(STUDY)))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {
        "status": "PASS" if result.wasSuccessful() and result.testsRun > 0 else "FAIL",
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "output": stream.getvalue()[-12000:],
    }


def qualify_static_v9() -> dict[str, Any]:
    global _PARENT_LOCK_VERIFIED
    if STATIC_REPORT_PATH.is_file():
        report = json.loads(STATIC_REPORT_PATH.read_text(encoding="utf-8"))
        if report.get("status") == "PASS":
            return report
        raise QualificationV9Engineering6Failure("engineering-6 static report exists without PASS; no retry")
    if STATIC_FAILURE_PATH.exists():
        raise QualificationV9Engineering6Failure("engineering-6 static failure receipt is immutable; no retry")
    if any(path.exists() for path in (LOCK_PATH, STATIC_PATH, LEDGER_PATH, LEDGER_SNAPSHOT_PATH)):
        raise QualificationV9Engineering6Failure("partial engineering-6 static/lock artifacts exist; no retry")
    started_cpu = q.process_tree_cpu()
    started_wall = time.monotonic()
    try:
        q.install_process_cpu_ceiling(STATIC_RESERVE)
        parent.verify_lock_v9()
        _PARENT_LOCK_VERIFIED = True
        validate_parent_capture_v9()
        source_commit = require_current_branch()
        _reserve_static()
        lock = prepare_lock_v9(code_source_commit=source_commit)
        lock, _config = verify_lock_v9()
        tests = _run_static_tests()
        if tests["status"] != "PASS":
            raise QualificationV9Engineering6Failure(f"engineering-6 focused tests failed: {tests['output'][-3000:]}")
        runtime_lock, prepared, module_path, module_hash = _prepared_capture_runtime_v9()
        measured = max(0.0, q.process_tree_cpu() - started_cpu)
        if measured > STATIC_RESERVE:
            raise QualificationV9Engineering6Failure("engineering-6 static qualification exceeded its fixed 60 CPU-s reservation")
        parent_lock = json.loads(parent.LOCK_PATH.read_text(encoding="utf-8"))
        report = {
            "schema_id": "issue73-v9-engineering-6-static-qualification-report-v1",
            "engineering_revision": 6,
            "status": "PASS",
            "branch": BRANCH,
            "code_source_commit": lock["code_source_commit"],
            "source_lock_sha256": sha256_text_file(LOCK_PATH),
            "parent_engineering_5_source_lock_sha256": sha256_text_file(parent.LOCK_PATH),
            "parent_engineering_5_capture_report_sha256": sha256_text_file(CAPTURE_REPORT_PATH),
            "parent_engineering_5_capture_attempt_sha256": sha256_text_file(CAPTURE_ATTEMPT_PATH),
            "parent_engineering_5_capture_history_sha256": sha256_text_file(CAPTURE_HISTORY_PATH),
            "parent_engineering_5_capture_roots_sha256": sha256_text_file(CAPTURE_ROOTS_PATH),
            "parent_capture_receipt_validation": {"status": "PASS"},
            "parent_lock_status": parent_lock.get("status"),
            "parent_lock_revision": parent_lock.get("lock_revision"),
            "owner_root_ids": lock["capture_owner_root_ids"],
            "capture_history_event_count": lock["capture_history_event_count"],
            "capture_root_count": lock["capture_root_count"],
            "focused_tests": tests,
            "runtime": {
                "python_executable": sys.executable,
                "python_version": sys.version.split()[0],
                "native_module_path": str(module_path),
                "native_module_sha256": module_hash,
                "model_digest": prepared["model_digest"],
                "adapter_class": {"module": prepared["adapter_class"].__module__, "qualname": prepared["adapter_class"].__qualname__},
                "external_sources": prepared["external_sources"],
                "runtime_identity": prepared["runtime"],
                "adapter_instantiated": False,
                "game_step_started": False,
                "formal_seed_consumed": False,
                "parent_lock_source_commit": runtime_lock["code_source_commit"],
            },
            "resource_projection": lock["resource_projection"],
            "fixed_static_reservation_cpu_seconds": STATIC_RESERVE,
            "measured_process_tree_cpu_seconds": measured,
            "measured_wall_seconds": max(0.0, time.monotonic() - started_wall),
            "stage1_authorized": False,
            "no_capture_replay_or_formal_seed": True,
        }
        runner.atomic_json(STATIC_REPORT_PATH, report)
        _receipt, _ledger, _projection = _ensure_static_reservation()
        push_v9("Issue 73: preserve v9 engineering-6 static qualification")
        return report
    except BaseException as exc:
        measured = max(0.0, q.process_tree_cpu() - started_cpu)
        failure = {
            "schema_id": "issue73-v9-engineering-6-static-qualification-failure-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1800],
            "fixed_static_reservation_cpu_seconds": STATIC_RESERVE,
            "measured_process_tree_cpu_seconds": measured,
            "measured_wall_seconds": max(0.0, time.monotonic() - started_wall),
            "formal_seed_consumed": False,
            "native_adapter_instantiated": False,
            "native_game_step_started": False,
            "no_retry_or_overwrite": True,
        }
        if not STATIC_FAILURE_PATH.exists():
            runner.atomic_json(STATIC_FAILURE_PATH, failure)
        try:
            push_v9("Issue 73: preserve v9 engineering-6 static qualification failure")
        except BaseException:
            pass
        raise


def _start_parity_attempt_v9(lock: Mapping[str, Any]) -> dict[str, Any]:
    if PARITY_ATTEMPT_PATH.exists():
        previous = json.loads(PARITY_ATTEMPT_PATH.read_text(encoding="utf-8"))
        if previous.get("status") == "STARTED":
            previous.update({
                "status": "INTERRUPTED_NO_RERUN",
                "cpu_charged_seconds": PARITY_RESERVE,
                "failure": "parity had no terminal receipt; keep its fixed debit and do not rerun",
            })
            ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
            ledger["attempts"] = [previous if row.get("key") == PARITY_ATTEMPT_KEY else row for row in ledger["attempts"]]
            runner.atomic_json(PARITY_ATTEMPT_PATH, previous)
            runner.atomic_json(LEDGER_PATH, ledger)
            push_v9("Issue 73: preserve interrupted engineering-6 parity without rerun")
            raise QualificationV9Engineering6Failure("engineering-6 parity was interrupted; no retry")
        raise QualificationV9Engineering6Failure(f"engineering-6 parity already terminal: {previous.get('status')}")
    _receipt, ledger, _projection = _ensure_static_reservation()
    if json.loads(STATIC_REPORT_PATH.read_text(encoding="utf-8")).get("status") != "PASS":
        raise QualificationV9Engineering6Failure("fresh-process parity requires the engineering-6 static PASS")
    validate_parent_capture_v9()
    base = _base_prior_ledger()
    builder = q.charged(base)[0] + sum(float(row.get("cpu_charged_seconds", 0)) for row in ledger["attempts"])
    native = q.charged(base)[1] + sum(float(row.get("cpu_charged_seconds", 0)) for row in ledger["attempts"] if row.get("native_gate_bc") is True)
    if builder + PARITY_RESERVE > 4800.0 or native + PARITY_RESERVE > 3060.0:
        raise QualificationV9Engineering6Failure("authorized parity debit exceeds registered Builder/native ceilings")
    row = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": PARITY_ATTEMPT_KEY,
        "kind": "v9_fresh_process_complete_prefix_and_forced_action_parity",
        "status": "STARTED",
        "native_gate_bc": True,
        "contingency_pool": "native",
        "reserved_cpu_seconds": PARITY_RESERVE,
        "cpu_charged_seconds": PARITY_RESERVE,
        "source_lock_sha256": sha256_text_file(LOCK_PATH),
        "code_source_commit": lock["code_source_commit"],
        "formal_seed_consumed": False,
        "resource_projection": _resource_projection(base, ledger),
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ledger["attempts"].append(row)
    runner.atomic_json(PARITY_ATTEMPT_PATH, row)
    runner.atomic_json(LEDGER_PATH, ledger)
    push_v9("Issue 73: start the authorized v9 fresh-process parity")
    return row


def fresh_process_prefix_parity_v9() -> dict[str, Any]:
    lock, config = verify_lock_v9()
    if PARITY_ATTEMPT_PATH.is_file():
        previous = json.loads(PARITY_ATTEMPT_PATH.read_text(encoding="utf-8"))
        if previous.get("status") == "PASS" and PARITY_REPORT_PATH.is_file():
            return json.loads(PARITY_REPORT_PATH.read_text(encoding="utf-8"))
        raise QualificationV9Engineering6Failure(f"parity receipt already exists ({previous.get('status')}); no retry")
    parent_lock, capture_report, capture_attempt, history_record, roots_record = validate_parent_capture_v9()
    mismatches = capture_receipt_mismatches_v9(
        capture_report, capture_attempt, history_record, roots_record,
        parent_lock=parent_lock,
        parent_lock_sha256=sha256_text_file(parent.LOCK_PATH),
        roots_path=CAPTURE_ROOTS_PATH.relative_to(STUDY).as_posix(),
        roots_sha256=sha256_text_file(CAPTURE_ROOTS_PATH),
        history_sha256=sha256_text_file(CAPTURE_HISTORY_PATH),
    )
    if mismatches:
        raise QualificationV9Engineering6Failure("capture parent preflight mismatch: " + ", ".join(mismatches))
    roots = roots_record.get("roots", [])
    root_ids = [str(row.get("root_id", "")) for row in roots]
    if len(root_ids) != 39 or set(root_ids) != set(lock.get("capture_owner_root_ids", [])) or len(root_ids) != len(set(root_ids)):
        raise QualificationV9Engineering6Failure("parity requires the exact 39 unique locked capture roots")
    integrity = parent.transition_witness_integrity_v9(
        capture_report.get("transition_witnesses", {}),
        config["gate_c_transition_pairs"],
    )
    if not integrity.get("complete_pass"):
        raise QualificationV9Engineering6Failure(f"same-process capture witness set is incomplete: {integrity}")

    q.install_process_cpu_ceiling(PARITY_RESERVE)
    _start_parity_attempt_v9(lock)
    phase_start = q.process_tree_cpu()
    adapter = None
    try:
        _runtime_lock, prepared, module_path, module_hash = _prepared_capture_runtime_v9()
        history = history_record["events"]
        roots_by_id = {str(root_id): q.root_receipt(str(root_id)) for root_id in lock["capture_owner_root_ids"]}
        adapter = runner.native_adapter(prepared["module"], parent.SEED)
        witnesses: dict[str, Any] = {}
        for pair in config["gate_c_transition_pairs"]:
            root_id = str(pair["root_id"])
            candidate_index = int(pair["candidate_index"])
            key = f"{root_id}-a{candidate_index}"
            try:
                root = roots_by_id[root_id]
                original = capture_report["transition_witnesses"][key]
                first = q.replay_forced_transition(adapter, history, root, candidate_index, prepared["module"])
                second = q.replay_forced_transition(adapter, history, root, candidate_index, prepared["module"])
                same_process_equal = canonical(first) == canonical(second)
                original_equal = canonical(first) == canonical(original["transition"])
                witnesses[key] = {
                    "status": "PASS" if same_process_equal and original_equal else "FAIL",
                    "root_id": root_id,
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
                    "root_id": root_id,
                    "candidate_index": candidate_index,
                    "failure_type": type(exc).__name__,
                    "failure": str(exc)[:800],
                }
        gate_b_screens = sorted({str(roots_by_id[root_id]["screen"]) for root_id in q.GATE_B_ROOTS.values()})
        all_pass = len(witnesses) == len(config["gate_c_transition_pairs"]) and all(row.get("status") == "PASS" for row in witnesses.values())
        report = {
            "schema_id": "issue73-fresh-process-prefix-parity-v9-engineering-6-v1",
            "status": "PASS" if all_pass and len(gate_b_screens) >= 3 else "FAIL_FRESH_PROCESS_PREFIX_OR_TRANSITION_PARITY",
            "fresh_process": True,
            "source_lock_sha256": sha256_text_file(LOCK_PATH),
            "capture_source_lock_sha256": sha256_text_file(parent.LOCK_PATH),
            "code_source_commit": lock["code_source_commit"],
            "capture_code_source_commit": parent_lock["code_source_commit"],
            "native_module_path": str(module_path),
            "native_module_sha256": module_hash,
            "history_sha256": sha256_text_file(CAPTURE_HISTORY_PATH),
            "replayed_transition_count": len(witnesses),
            "required_transition_count": len(config["gate_c_transition_pairs"]),
            "witnesses": witnesses,
            "gate_b_root_screen_families": gate_b_screens,
            "gate_b_witness_root_ids": dict(q.GATE_B_ROOTS),
            "native_rng_byte_state_exposed": False,
            "rng_claim": "The pinned API exposes no native RNG bytes/counters. Evidence is full action-history prefix replay and exact transition parity; no hidden-game-future distribution claim.",
            "formal_seed_consumed": False,
        }
        runner.atomic_json(PARITY_REPORT_PATH, report)
    except BaseException as exc:
        failure = {
            "schema_id": "issue73-fresh-process-prefix-parity-failure-v9-engineering-6-v1",
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
            "cpu_charged_seconds": PARITY_RESERVE,
            "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        ledger["attempts"] = [attempt if row.get("key") == PARITY_ATTEMPT_KEY else row for row in ledger["attempts"]]
        runner.atomic_json(PARITY_ATTEMPT_PATH, attempt)
        runner.atomic_json(LEDGER_PATH, ledger)
        push_v9("Issue 73: preserve fresh-process parity failure without retry")
        raise
    finally:
        if adapter is not None:
            adapter.close()
    attempt = json.loads(PARITY_ATTEMPT_PATH.read_text(encoding="utf-8"))
    measured = max(0.0, q.process_tree_cpu() - phase_start)
    if measured > PARITY_RESERVE and report["status"] == "PASS":
        report["status"] = "FAIL_CPU_RESERVATION_EXCEEDED"
    success = report["status"] == "PASS" and measured <= PARITY_RESERVE
    attempt.update({
        "status": "PASS" if success else ("FAIL_CPU_RESERVATION_EXCEEDED" if measured > PARITY_RESERVE else "FAIL_OR_INCOMPLETE"),
        "report_status": report["status"],
        "measured_process_cpu_seconds": measured,
        "cpu_charged_seconds": PARITY_RESERVE,
        "formal_seed_consumed": False,
        "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    ledger["attempts"] = [attempt if row.get("key") == PARITY_ATTEMPT_KEY else row for row in ledger["attempts"]]
    runner.atomic_json(PARITY_ATTEMPT_PATH, attempt)
    runner.atomic_json(LEDGER_PATH, ledger)
    report["attempt_receipt"] = attempt
    runner.atomic_json(PARITY_REPORT_PATH, report)
    push_v9("Issue 73: preserve fresh-process parity result")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("qualify-static", "verify-lock", "fresh-process-parity"))
    args = parser.parse_args(argv)
    if args.command == "qualify-static":
        result = qualify_static_v9()
    elif args.command == "verify-lock":
        lock, _config = verify_lock_v9()
        result = {"status": "PASS", "source_lock_sha256": sha256_text_file(LOCK_PATH), "code_source_commit": lock["code_source_commit"]}
    else:
        result = fresh_process_prefix_parity_v9()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
