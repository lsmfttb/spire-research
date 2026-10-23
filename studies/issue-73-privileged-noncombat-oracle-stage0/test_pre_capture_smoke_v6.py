"""One-shot v6 production capture-launch and pre-debit qualification."""
from __future__ import annotations

import hashlib
import json
import math
import os
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def process_tree_cpu() -> float:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return float(own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime)


START_CPU = process_tree_cpu()
STUDY = Path(__file__).resolve().parent
REPOSITORY = STUDY.parents[1]
OUT = STUDY / "receipts" / "source-qualification-v6"
REPORTS = OUT / "reports"
TEST_ROOT = REPORTS / "capture-launcher-tests-v6"
TEST_REPORT = REPORTS / "capture-launcher-test-report-v6.json"
RESERVATION_PATH = OUT / "attempts" / "builder-prelock-static-verification-v6.json"
STATIC_SMOKE_REPORT = REPORTS / "pre-capture-smoke-v6.json"
STATIC_SMOKE_FAILURE = REPORTS / "pre-capture-smoke-v6-failure.json"
LOCK_PATH = OUT / "preseed-source-lock-v6.json"
CONFIG_PATH = OUT / "qualification-config-v6.json"
PLAN_PATH = OUT / "plans" / "gate-c-replay-plan-v6.json"
SCRIPT = STUDY / "checkpoint_replay_qualification.py"
STATIC_LIMIT = 60.0
COMMIT_CPU_MARGIN = 4.0
BASE_STATIC_USED = 0.0
CONFIGURED_NATIVE_MODULE_PATH = (
    "/home/lsmft/stsrl-spikes/build-issue71-8ded-py313/"
    "slaythespire.cpython-313-x86_64-linux-gnu.so"
)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def current_cpu() -> float:
    return max(0.0, process_tree_cpu() - START_CPU)


def configure_cpu_limit(remaining: float) -> int:
    hard_cap = math.floor(remaining)
    if hard_cap < 2:
        raise RuntimeError(f"less than two CPU seconds remain in v6 static allowance: {remaining:.6f}")
    old_soft, old_hard = resource.getrlimit(resource.RLIMIT_CPU)
    hard = hard_cap if old_hard == resource.RLIM_INFINITY else min(hard_cap, int(old_hard))
    if hard < 2:
        raise RuntimeError("existing process CPU hard limit is below the remaining v6 static allowance")
    soft = max(1, hard - 1)
    if old_soft != resource.RLIM_INFINITY:
        soft = min(soft, int(old_soft))
    if soft >= hard:
        soft = hard - 1
    resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))
    return hard


def ensure_task_local_runtime_dirs(env: dict[str, str]) -> None:
    temporary = REPOSITORY / ".builder-tmp-issue73-v6"
    cache = REPOSITORY / ".builder-cache-issue73-v6"
    temporary.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "XDG_CACHE_HOME": str(cache),
        "TORCH_HOME": str(cache / "torch"),
        "TORCHINDUCTOR_CACHE_DIR": str(cache / "torchinductor"),
        "STS_LIGHTSPEED_SOURCE_DIR": "/home/lsmft/stsrl-spikes/sts_lightspeed-issue71-stage1-8ded-20261022",
        "STSRL_SOURCE_DIR": "/home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c",
    })
    for name in ("TMPDIR", "XDG_CACHE_HOME", "TORCH_HOME", "TORCHINDUCTOR_CACHE_DIR"):
        path = Path(env[name]).resolve()
        if not path.is_relative_to(REPOSITORY.resolve()):
            raise RuntimeError(f"runtime {name} escaped the task worktree: {path}")
        path.mkdir(parents=True, exist_ok=True)


def cleanup_task_local_runtime_dirs() -> None:
    for name in (".builder-tmp-issue73-v6", ".builder-cache-issue73-v6"):
        path = REPOSITORY / name
        resolved = path.resolve()
        if not resolved.is_relative_to(REPOSITORY.resolve()):
            raise RuntimeError(f"refusing to remove runtime directory outside the task worktree: {resolved}")
        if path.exists():
            if not path.is_dir():
                raise RuntimeError(f"task-local runtime path is not a directory: {path}")
            shutil.rmtree(path)


def run_phase(name: str, args: list[str], env: dict[str, str]) -> tuple[dict[str, Any], float]:
    remaining = STATIC_LIMIT - BASE_STATIC_USED - current_cpu()
    child_cap = math.floor(remaining)
    if child_cap < 2:
        raise RuntimeError(f"static test sequence has exhausted its registered CPU allowance before {name}")

    def set_child_cpu_limit() -> None:
        old_soft, old_hard = resource.getrlimit(resource.RLIMIT_CPU)
        hard = child_cap if old_hard == resource.RLIM_INFINITY else min(child_cap, int(old_hard))
        if hard < 2:
            raise RuntimeError("child process has less than two seconds under its inherited CPU limit")
        resource.setrlimit(resource.RLIMIT_CPU, (hard - 1, hard))

    before = process_tree_cpu()
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPOSITORY,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=set_child_cpu_limit,
        check=False,
    )
    measured = max(0.0, process_tree_cpu() - before)
    if completed.returncode:
        raise RuntimeError(f"{name} failed ({completed.returncode}): {completed.stderr[-1600:]}")
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{name} returned success without a JSON result: {completed.stdout[-1000:]}") from exc
    return payload, measured


def run_child(name: str, args: list[str], env: dict[str, str], *, expected_success: bool) -> tuple[dict[str, Any], float]:
    case_dir = TEST_ROOT / name
    result_path = case_dir / "launcher-result.json"
    failure_path = case_dir / "capture-launch-preflight-v6-failure.json"
    if case_dir.exists():
        if expected_success and result_path.is_file():
            return read_json(result_path), 0.0
        if not expected_success and failure_path.is_file():
            return read_json(failure_path), 0.0
        raise RuntimeError(f"partial v6 launcher test evidence exists; do not overwrite it: {case_dir}")
    case_dir.mkdir(parents=True)
    command = [sys.executable, str(SCRIPT), "capture", "--preflight-only", "--preflight-report-dir", str(case_dir), *args]
    remaining = STATIC_LIMIT - BASE_STATIC_USED - current_cpu()
    child_cap = math.floor(remaining)
    if child_cap < 2:
        raise RuntimeError(f"static test sequence has exhausted its registered CPU allowance before {name}")

    def set_child_cpu_limit() -> None:
        old_soft, old_hard = resource.getrlimit(resource.RLIMIT_CPU)
        hard = child_cap if old_hard == resource.RLIM_INFINITY else min(child_cap, int(old_hard))
        if hard < 2:
            raise RuntimeError("child process has less than two seconds under its inherited CPU limit")
        resource.setrlimit(resource.RLIMIT_CPU, (hard - 1, hard))

    before = process_tree_cpu()
    completed = subprocess.run(
        command,
        cwd=REPOSITORY,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=set_child_cpu_limit,
        check=False,
    )
    measured = max(0.0, process_tree_cpu() - before)
    if completed.returncode == 0:
        try:
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{name} returned success without one JSON result: {completed.stdout[-1000:]}") from exc
        (case_dir / "launcher-result.json").write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        if not expected_success:
            raise RuntimeError(f"negative full-entry case unexpectedly passed: {name}")
        return payload, measured
    if expected_success:
        raise RuntimeError(f"positive full-entry case failed ({completed.returncode}): {completed.stderr[-1600:]}")
    if not failure_path.is_file():
        raise RuntimeError(f"negative full-entry case did not preserve its failure receipt: {name}: {completed.stderr[-1600:]}")
    payload = read_json(failure_path)
    if payload.get("native_debit_created") is not False or payload.get("start_attempt_invoked") is not False:
        raise RuntimeError(f"negative full-entry case crossed the native debit boundary: {name}")
    return payload, measured


def run() -> dict[str, Any]:
    global BASE_STATIC_USED
    if TEST_REPORT.exists():
        raise RuntimeError("v6 launcher-test report is immutable and already exists; no rerun permitted")
    if not RESERVATION_PATH.is_file():
        raise RuntimeError("v6 static CPU reservation is missing; tests must not run before precharge")
    if STATIC_SMOKE_FAILURE.exists():
        raise RuntimeError("immutable v6 static smoke failure exists; no launcher qualification can continue")
    reservation = read_json(RESERVATION_PATH)
    if (
        reservation.get("status") != "RESERVED_CONSERVATIVE_STATIC_DEBIT"
        or reservation.get("reserved_cpu_seconds") != STATIC_LIMIT
        or reservation.get("cpu_charged_seconds") != STATIC_LIMIT
        or reservation.get("native_gate_bc") is not False
    ):
        raise RuntimeError("v6 launcher tests are not covered by the required full 60-second non-native precharge")

    preexisting_smoke_cpu = 0.0
    if STATIC_SMOKE_REPORT.is_file():
        preexisting_smoke_cpu = float(read_json(STATIC_SMOKE_REPORT).get("resource_accounting", {}).get("measured_process_tree_cpu_seconds", 0.0)) + COMMIT_CPU_MARGIN
    lock_files = (LOCK_PATH, CONFIG_PATH, PLAN_PATH)
    if any(path.exists() for path in lock_files) and not all(path.is_file() for path in lock_files):
        raise RuntimeError("partial v6 source/config/plan lock exists; do not reconstruct it")
    preexisting_lock_cpu = 0.0
    if LOCK_PATH.is_file():
        lock_record = read_json(LOCK_PATH)
        if "lock_preparation_process_cpu_seconds" not in lock_record:
            raise RuntimeError("v6 lock lacks its durable preparation CPU measurement; stop rather than infer")
        preexisting_lock_cpu = float(lock_record["lock_preparation_process_cpu_seconds"]) + COMMIT_CPU_MARGIN
    preexisting_test_cpu = 0.0
    for case_name in ("positive", "negative-missing", "negative-wrong-module"):
        case_dir = TEST_ROOT / case_name
        result_path = case_dir / "launcher-result.json"
        failure_path = case_dir / "capture-launch-preflight-v6-failure.json"
        if not case_dir.exists():
            continue
        if result_path.is_file():
            case_receipt = read_json(result_path)
        elif failure_path.is_file():
            case_receipt = read_json(failure_path)
        else:
            raise RuntimeError(f"partial v6 test case receipt exists; do not overwrite: {case_dir}")
        preexisting_test_cpu += float(case_receipt.get("measured_process_tree_cpu_seconds", 0.0)) + COMMIT_CPU_MARGIN
    observed_precharge_cpu = float(reservation.get("observed_process_cpu_seconds", 0.0))
    BASE_STATIC_USED = observed_precharge_cpu + COMMIT_CPU_MARGIN + preexisting_smoke_cpu + preexisting_lock_cpu + preexisting_test_cpu
    parent_cap = configure_cpu_limit(STATIC_LIMIT - BASE_STATIC_USED)
    env = os.environ.copy()
    ensure_task_local_runtime_dirs(env)
    import checkpoint_replay_qualification as qualification

    if not env.get("STS_LIGHTSPEED_SOURCE_DIR") or not env.get("STSRL_SOURCE_DIR"):
        raise RuntimeError("pinned external source environment is not configured")
    configured_module = CONFIGURED_NATIVE_MODULE_PATH
    if CONFIG_PATH.is_file() and read_json(CONFIG_PATH).get("configured_native_module_path") != configured_module:
        raise RuntimeError("v6 configured native module path differs from the source-locked qualification test")

    started = time.monotonic()
    phases: dict[str, Any] = {}
    if STATIC_SMOKE_REPORT.is_file():
        prior = read_json(STATIC_SMOKE_REPORT)
        phases["pre_capture_smoke"] = {"status": "RESUMED_FROM_DURABLE_RECEIPT", "measured_process_tree_cpu_seconds": prior.get("resource_accounting", {}).get("measured_process_tree_cpu_seconds"), "receipt": prior}
    else:
        smoke, cpu = run_phase("pre-capture-smoke", ["pre-capture-smoke"], env)
        # The smoke command's receipt is separately pushed by its own entrypoint.
        if not STATIC_SMOKE_REPORT.is_file() or smoke.get("status") != "PASS":
            raise RuntimeError("v6 smoke process returned without its immutable PASS receipt")
        phases["pre_capture_smoke"] = {"status": "PASS", "measured_child_cpu_seconds": cpu, "receipt": smoke}
    if LOCK_PATH.is_file() and CONFIG_PATH.is_file() and PLAN_PATH.is_file():
        prior_lock = read_json(LOCK_PATH)
        phases["prepare_lock"] = {
            "status": "RESUMED_FROM_DURABLE_LOCK",
            "measured_process_cpu_seconds": prior_lock.get("lock_preparation_process_cpu_seconds"),
            "lock_sha256": hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest(),
        }
    elif any(path.exists() for path in (LOCK_PATH, CONFIG_PATH, PLAN_PATH)):
        raise RuntimeError("partial v6 source/config/plan lock exists; do not reconstruct it")
    else:
        lock_result, lock_cpu = run_phase("prepare-lock", ["prepare-lock"], env)
        if not LOCK_PATH.is_file() or lock_result.get("status") != "PRESEED_LOCKED_BEFORE_NEW_DEBUG_EXECUTION":
            raise RuntimeError("prepare-lock did not preserve the complete immutable v6 lock")
        phases["prepare_lock"] = {"status": "PASS", "measured_child_cpu_seconds": lock_cpu, "result": lock_result}

    ledger_path = OUT / "resource-ledger.json"
    capture_attempt_path = OUT / "attempts" / "source-history-capture-173730001-prospective-v6.json"
    global_capture_failure_path = REPORTS / "capture-launch-preflight-v6-failure.json"
    if not ledger_path.is_file():
        raise RuntimeError("v6 resource ledger is missing after source-lock preparation")
    ledger_before = ledger_path.read_bytes()
    if capture_attempt_path.exists():
        raise RuntimeError("v6 capture attempt receipt already exists; do not rerun or overwrite it")
    if global_capture_failure_path.exists():
        raise RuntimeError("immutable production capture-preflight failure receipt exists; native capture is blocked")

    positive, positive_cpu = run_child("positive", [], env, expected_success=True)
    expected_digest = str(read_json(STUDY / "config.json")["identity"]["native_binary_sha256"])
    expected_module_name = str(read_json(STUDY / "config.json")["identity"]["native_module"])
    external_sources = positive.get("external_git_sources", {}).get("sources", [])
    external_by_env = {row.get("environment_variable"): row for row in external_sources}
    checks = {
        "production_capture_entry_completed_in_same_process": positive.get("capture_entry_path", {}).get("preparation_completed_in_this_capture_process") is True,
        "stopped_immediately_before_start_attempt": positive.get("capture_entry_path", {}).get("start_attempt_invoked") is False,
        "native_adapter_not_instantiated": positive.get("capture_entry_path", {}).get("native_adapter_instantiated") is False,
        "native_game_step_not_started": positive.get("capture_entry_path", {}).get("native_game_step_started") is False,
        "formal_seed_not_consumed": positive.get("capture_entry_path", {}).get("formal_seed_consumed") is False,
        "locked_native_module_path": positive.get("native_module_path") == configured_module,
        "locked_native_module_sha256": positive.get("native_module_sha256") == expected_digest,
        "runtime_optimizer_accepted": positive.get("optimizer_id") == "issue65-demand-driven-public-history-v1",
        "runtime_optimizer_source_sha256": positive.get("optimizer_source_sha256") == read_json(STUDY / "config.json")["runtime_optimizer"]["sha256"],
        "python_is_pinned_runtime": positive.get("python") == str(read_json(STUDY / "config.json")["identity"]["python"]),
        "adapter_class_checked": bool(positive.get("adapter_class", {}).get("qualname")),
        "native_source_commit_clean": external_by_env.get("STS_LIGHTSPEED_SOURCE_DIR", {}).get("commit") == read_json(STUDY / "config.json")["identity"]["native_commit"] and external_by_env.get("STS_LIGHTSPEED_SOURCE_DIR", {}).get("clean_worktree") is True,
        "stsr_source_commit_clean": external_by_env.get("STSRL_SOURCE_DIR", {}).get("commit") == read_json(STUDY / "config.json")["identity"]["stsr_commit"] and external_by_env.get("STSRL_SOURCE_DIR", {}).get("clean_worktree") is True,
    }
    if not all(checks.values()):
        raise RuntimeError(f"production capture preflight positive checks failed: {checks}")

    missing_name = "negative-missing"
    missing_module = str(TEST_ROOT / missing_name / "missing-native-module.so")
    missing, missing_cpu = run_child(missing_name, ["--native-module-path", missing_module], env, expected_success=False)
    if "unavailable" not in str(missing.get("failure", "")):
        raise RuntimeError("missing native module negative did not fail closed at configured-path resolution")

    wrong_name = "negative-wrong-module"
    wrong_fixture_dir = TEST_ROOT / "wrong-module-fixture"
    if not wrong_fixture_dir.exists():
        wrong_fixture_dir.mkdir(parents=True)
    wrong_module = wrong_fixture_dir / expected_module_name
    wrong_bytes = b"not the frozen native extension; issue73 v6 negative fixture\n"
    if wrong_module.exists():
        if wrong_module.read_bytes() != wrong_bytes:
            raise RuntimeError("immutable wrong-module fixture differs from the registered bytes")
    else:
        wrong_module.write_bytes(wrong_bytes)
    wrong, wrong_cpu = run_child(wrong_name, ["--native-module-path", str(wrong_module)], env, expected_success=False)
    if "differs from the exact prospective v6 lock" not in str(wrong.get("failure", "")):
        raise RuntimeError("wrong native module path negative did not fail closed against the exact lock")

    raw_wrong_digest_rejected = False
    try:
        qualification.runner.validate_native_module_identity(
            wrong_module,
            expected_name=expected_module_name,
            expected_sha256=expected_digest,
        )
    except qualification.runner.StudyFailure:
        raw_wrong_digest_rejected = True
    if not raw_wrong_digest_rejected:
        raise RuntimeError("raw wrong-binary digest negative was not rejected")

    if ledger_path.read_bytes() != ledger_before:
        raise RuntimeError("capture launcher preflight changed the cumulative resource ledger before start_attempt")
    if capture_attempt_path.exists():
        raise RuntimeError("capture launcher preflight created a native attempt receipt before start_attempt")
    if global_capture_failure_path.exists():
        raise RuntimeError("diagnostic negative cases wrote the immutable production failure receipt")

    static_cpu_used = BASE_STATIC_USED + current_cpu() + COMMIT_CPU_MARGIN
    if static_cpu_used > STATIC_LIMIT:
        raise RuntimeError(f"v6 static qualification plus its commit margin exceeds the precharged 60 CPU-s: {static_cpu_used:.6f}")
    if STATIC_LIMIT - static_cpu_used < 2.0:
        raise RuntimeError("v6 static qualification leaves less than two CPU seconds after the final commit margin")
    report = {
        "schema_id": "issue73-v6-capture-launcher-test-report-v1",
        "status": "PASS",
        "study_only": True,
        "formal_seed_consumed": False,
        "native_capture_started": False,
        "branch": "codex/issue-73-stage0-replay-qualification",
        "source_lock_sha256": hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest(),
        "source_lock_code_commit": read_json(LOCK_PATH).get("code_source_commit"),
        "resource_ledger_sha256_before_and_after_capture_preflights": hashlib.sha256(ledger_before).hexdigest(),
        "capture_attempt_receipt_absent_before_start_attempt": True,
        "configured_native_module_path": configured_module,
        "configured_native_module_name": expected_module_name,
        "configured_native_module_sha256": expected_digest,
        "parent_hard_cpu_limit_seconds": parent_cap,
        "checks": checks,
        "negative_cases": {
            "missing_configured_module": {"status": missing.get("status"), "failure": missing.get("failure"), "measured_process_tree_cpu_seconds": missing_cpu, "native_debit_created": missing.get("native_debit_created")},
            "wrong_module_path": {"status": wrong.get("status"), "failure": wrong.get("failure"), "measured_process_tree_cpu_seconds": wrong_cpu, "native_debit_created": wrong.get("native_debit_created")},
            "wrong_raw_binary_digest_rejected": raw_wrong_digest_rejected,
        },
        "positive_case": {
            "status": positive.get("status"),
            "command_argv": positive.get("command_argv"),
            "python_executable": positive.get("python_executable"),
            "python": positive.get("python"),
            "cwd": positive.get("cwd"),
            "pythonpath_entries": positive.get("pythonpath_entries"),
            "sys_path": positive.get("sys_path"),
            "native_module_path": positive.get("native_module_path"),
            "native_module_sha256": positive.get("native_module_sha256"),
            "native_module_search": positive.get("native_module_search"),
            "external_git_sources": positive.get("external_git_sources"),
            "optimizer_id": positive.get("optimizer_id"),
            "adapter_class": positive.get("adapter_class"),
            "loaded_policy_sha256": positive.get("loaded_policy_sha256"),
            "measured_process_tree_cpu_seconds": positive_cpu,
            "capture_entry_path": positive.get("capture_entry_path"),
        },
        "source_lock": {
            "schema_id": read_json(LOCK_PATH).get("schema_id"),
            "revision": read_json(LOCK_PATH).get("lock_revision"),
            "remote_branch": read_json(LOCK_PATH).get("remote_branch"),
        },
        "phases": phases,
        "resource_accounting": {
            "precharged_builder_cpu_seconds": STATIC_LIMIT,
            "precharge_process_observed_cpu_seconds": observed_precharge_cpu,
            "preexisting_static_phase_cpu_seconds_with_commit_margins": BASE_STATIC_USED - observed_precharge_cpu - COMMIT_CPU_MARGIN,
            "post_precharge_process_tree_cpu_seconds": current_cpu(),
            "planned_final_receipt_commit_cpu_margin_seconds": COMMIT_CPU_MARGIN,
            "cumulative_observed_builder_cpu_seconds": static_cpu_used,
            "remaining_under_static_reservation_cpu_seconds": STATIC_LIMIT - static_cpu_used,
            "native_debit_created": False,
            "reviewer_reserve_untouched": True,
            "measured_wall_seconds": max(0.0, time.monotonic() - started),
        },
        "durability": "all case output and failure receipts remain inside the v6 study-local receipt directory; the test runner commits the exact receipt set after completion",
    }
    cleanup_task_local_runtime_dirs()
    REPORTS.mkdir(parents=True, exist_ok=True)
    if TEST_REPORT.exists():
        raise RuntimeError("v6 launcher-test report appeared during this run; refusing overwrite")
    TEST_REPORT.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    configure_cpu_limit(STATIC_LIMIT - BASE_STATIC_USED - current_cpu())
    qualification.push_scoped_commit("Issue 73: preserve v6 production capture launcher qualification")
    return report


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
