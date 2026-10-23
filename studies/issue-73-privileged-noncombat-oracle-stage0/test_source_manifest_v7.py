"""One-time, bounded v7 manifest and pre-lock ordering qualification."""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
from pathlib import Path
from typing import Any

MODULE_START_OWN_CPU = resource.getrusage(resource.RUSAGE_SELF)

import checkpoint_replay_qualification as q
import runner
import source_qualification_v7 as v7


REPORT_PATH = v7.INPUT_AUDIT_REPAIR_REPORT_PATH


def _preexec_cpu_cap(remaining_seconds: float) -> Any:
    hard = max(2, min(60, int(remaining_seconds)))
    soft = max(1, hard - 2)

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))

    return apply


def _run_cli(*args: str, remaining_seconds: float) -> dict[str, Any]:
    script = v7.STUDY / "checkpoint_replay_qualification.py"
    result = subprocess.run(
        [sys.executable, str(script), "--qualification-version", "v7", *args],
        cwd=v7.REPOSITORY,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=45,
        preexec_fn=_preexec_cpu_cap(remaining_seconds),
    )
    if result.returncode:
        raise AssertionError(
            f"production CLI {' '.join(args)} failed ({result.returncode}): {result.stderr[-1600:]}"
        )
    try:
        value = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise AssertionError(f"production CLI {' '.join(args)} did not return a JSON receipt") from exc
    if not isinstance(value, dict):
        raise AssertionError(f"production CLI {' '.join(args)} returned a non-object")
    return value


def run() -> dict[str, Any]:
    if REPORT_PATH.exists() or v7.STATIC_TEST_REPAIR_FAILURE_PATH.exists():
        raise RuntimeError("v7 pre-lock test already has an immutable report; no retry or overwrite")
    if any(path.exists() for path in (v7.LOCK_PATH, v7.CONFIG_PATH, v7.PLAN_PATH, v7.INPUT_MANIFEST_PATH)):
        raise RuntimeError("v7 pre-lock test requires all prospective lock artifacts to be absent")
    v7.ensure_static_precharge_v7()
    prior_failure = v7.read_json(v7.STATIC_TEST_FAILURE_PATH) if v7.STATIC_TEST_FAILURE_PATH.is_file() else None
    prior_failure_cpu = float((prior_failure or {}).get("measured_process_tree_cpu_seconds", 0.0))
    if prior_failure is not None and prior_failure.get("no_refund") is not True:
        raise RuntimeError("prior v7 static failure does not preserve the required no-refund accounting")
    if (v7.REPOSITORY / v7.OPTIONAL_RUNTIME_OUTPUT).exists():
        raise AssertionError("the known optional v6 runtime-metrics output is unexpectedly present")

    missing_input_path = v7.REPOSITORY / v7.REQUIRED_INPUT_ANCHORS[-2]
    original_bytes = missing_input_path.read_bytes()
    child_start = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        missing_input_path.unlink()
        try:
            v7.prepare_lock_v7()
        except v7.QualificationV7Failure as exc:
            if "mandatory historical inputs are missing" not in str(exc):
                raise AssertionError(f"missing mandatory source failed for the wrong reason: {exc}") from exc
            missing_rejected = True
            missing_failure = str(exc)
        else:
            raise AssertionError("missing mandatory historical input was accepted")
        if any(path.exists() for path in (v7.LOCK_PATH, v7.CONFIG_PATH, v7.PLAN_PATH, v7.INPUT_MANIFEST_PATH)):
            raise AssertionError("pre-lock rejection wrote a prospective v7 config, plan, manifest, or lock")
    finally:
        missing_input_path.write_bytes(original_bytes)
    if missing_input_path.read_bytes() != original_bytes:
        raise AssertionError("temporarily removed historical input was not restored byte-for-byte")
    own_after_missing = resource.getrusage(resource.RUSAGE_SELF)
    child_after_missing = resource.getrusage(resource.RUSAGE_CHILDREN)
    prelock_probe_cpu = max(
        0.0,
        own_after_missing.ru_utime + own_after_missing.ru_stime
        - MODULE_START_OWN_CPU.ru_utime - MODULE_START_OWN_CPU.ru_stime,
    ) + max(
        0.0,
        child_after_missing.ru_utime + child_after_missing.ru_stime
        - child_start.ru_utime - child_start.ru_stime,
    )
    prepared = _run_cli(
        "prepare-lock",
        remaining_seconds=v7.STATIC_RESERVATION_CPU_SECONDS - prior_failure_cpu - prelock_probe_cpu - 2.0,
    )
    prepare_cpu = float(prepared.get("lock_preparation_process_cpu_seconds", 0.0))
    verified = _run_cli(
        "verify-lock",
        remaining_seconds=v7.STATIC_RESERVATION_CPU_SECONDS - prior_failure_cpu - prelock_probe_cpu - prepare_cpu - 2.0,
    )
    lock = v7.read_json(v7.LOCK_PATH)
    manifest = v7.read_json(v7.INPUT_MANIFEST_PATH)
    if not all(path.is_file() for path in (v7.LOCK_PATH, v7.CONFIG_PATH, v7.PLAN_PATH, v7.INPUT_MANIFEST_PATH)):
        raise AssertionError("production prepare-lock did not write the complete prospective v7 lock set")
    if prepared.get("status") != "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION":
        raise AssertionError("production prepare-lock did not report its locked state")
    if verified.get("status") != "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION":
        raise AssertionError("production verify-lock did not succeed")
    if manifest.get("excluded", [])[0].get("path") != v7.OPTIONAL_RUNTIME_OUTPUT:
        raise AssertionError("manifest does not explicitly exclude the optional v6 runtime metrics output")
    if (v7.REPOSITORY / v7.OPTIONAL_RUNTIME_OUTPUT).exists():
        raise AssertionError("v7 qualification fabricated the optional v6 runtime metrics output")
    included_paths = [str(row["path"]) for row in manifest.get("included", [])]
    baseline = v7.git_tree_paths(v7.HISTORICAL_INPUT_BASE_COMMIT)
    current = v7.git_tree_paths(str(lock["code_source_commit"]))
    expected_paths = v7.exact_membership(baseline, current)
    if included_paths != expected_paths:
        raise AssertionError("manifest membership is not the exact bounded historical Git-tree set")
    if not set(v7.REQUIRED_INPUT_ANCHORS).issubset(included_paths):
        raise AssertionError("mandatory immutable input anchors are omitted from the v7 manifest")
    if any(not row.get("checkout_matches_locked_blob") for row in manifest.get("included", [])):
        raise AssertionError("a historical text or binary source did not match its exact Git blob")
    if len(lock.get("historical_input_sha256", {})) != len(included_paths):
        raise AssertionError("v7 source-lock hash map does not cover each included historical input")

    cli_cpu = float(prepared.get("lock_preparation_process_cpu_seconds", 0.0)) + float(
        verified.get("measured_process_tree_cpu_seconds", 0.0)
    )
    report = {
        "schema_id": "issue73-v7-input-manifest-prelock-qualification-v1",
        "status": "PASS",
        "qualification_run": "repair1_after_immutable_prepare_lock_timeout",
        "prior_static_failure": {
            "path": v7.STATIC_TEST_FAILURE_PATH.relative_to(v7.STUDY).as_posix() if prior_failure else None,
            "sha256": v7.sha256_file(v7.STATIC_TEST_FAILURE_PATH) if prior_failure else None,
            "preserved_unchanged": prior_failure is not None,
            "measured_process_tree_cpu_seconds": prior_failure_cpu,
        },
        "source_commit": lock["code_source_commit"],
        "missing_mandatory_input": {
            "path": missing_input_path.relative_to(v7.REPOSITORY).as_posix(),
            "rejected_before_any_new_v7_config_plan_manifest_or_lock": missing_rejected,
            "failure": missing_failure,
            "restored_exact_bytes": True,
        },
        "optional_runtime_output": {
            "path": v7.OPTIONAL_RUNTIME_OUTPUT,
            "exists": False,
            "explicitly_excluded": True,
            "fabricated": False,
        },
        "historical_membership": {
            "baseline_commit": v7.HISTORICAL_INPUT_BASE_COMMIT,
            "included_count": len(included_paths),
            "included_bytes": manifest["included_bytes"],
            "all_required_anchors_included": True,
            "all_exact_git_blob_checks_passed": True,
            "manifest_sha256": v7.sha256_file(v7.INPUT_MANIFEST_PATH),
        },
        "production_prepare_lock": prepared,
        "production_verify_lock": verified,
        "resource_accounting": {
            "measured_test_process_cpu_seconds": prelock_probe_cpu,
            "prepare_lock_reported_process_tree_cpu_seconds": float(prepared.get("lock_preparation_process_cpu_seconds", 0.0)),
            "verify_lock_reported_process_tree_cpu_seconds": float(verified.get("measured_process_tree_cpu_seconds", 0.0)),
            "conservative_sum_cpu_seconds": prior_failure_cpu + prelock_probe_cpu + cli_cpu,
            "fixed_reservation_cpu_seconds": v7.STATIC_RESERVATION_CPU_SECONDS,
            "no_refund": True,
        },
    }
    runner.atomic_json(REPORT_PATH, report)
    v7.push_v7("Issue 73: preserve v7 input manifest and pre-lock qualification")
    return report


def main() -> int:
    q.install_process_cpu_ceiling(v7.STATIC_RESERVATION_CPU_SECONDS)
    if "STS_LIGHTSPEED_SOURCE_DIR" not in os.environ or "STSRL_SOURCE_DIR" not in os.environ:
        raise RuntimeError("exact pinned external source worktree environment is required")
    try:
        report = run()
    except BaseException as exc:
        if not v7.STATIC_TEST_REPAIR_FAILURE_PATH.exists():
            runner.atomic_json(v7.STATIC_TEST_REPAIR_FAILURE_PATH, {
                "schema_id": "issue73-v7-input-manifest-prelock-qualification-failure-v1",
                "status": "FAIL_OR_INCOMPLETE",
                "failure_type": type(exc).__name__,
                "failure": str(exc)[:2000],
                "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU),
                "fixed_static_reservation_cpu_seconds": v7.STATIC_RESERVATION_CPU_SECONDS,
                "no_refund": True,
                "no_retry_or_overwrite": True,
            })
            v7.push_v7("Issue 73: preserve v7 input manifest repair qualification failure")
        raise
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
