"""V14 repair adapter for Issue 73's already-locked Stage-0 Gate C.

V14 leaves the reviewed V13 lock and all V13 receipts untouched. It fixes the
setup metadata field lookup and consumes the fixed recovery setup slot after
the immutable V13 setup-01 failure. The 64 suffix plan, roots, actions, RNG
streams, and outcome rules remain owned by V13.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence

import checkpoint_replay_qualification as q
import runner
import stage0_gate_bc_v13 as v13

STUDY = Path(__file__).resolve().parent
REPOSITORY = STUDY.parents[1]
STUDY_RELATIVE = STUDY.relative_to(REPOSITORY).as_posix()
BRANCH = v13.BRANCH
OUT = STUDY / "receipts" / "stage0-gate-bc-v14"
OUT_RELATIVE = OUT.relative_to(REPOSITORY).as_posix()
LOCK_PATH = OUT / "preseed-source-lock-v14.json"
CONFIG_PATH = OUT / "qualification-config-v14.json"
LEDGER_PATH = OUT / "resource-ledger-v14.json"
STATIC_KEY = "builder-prelock-static-verification-stage0-gate-bc-v14"
STATIC_RESERVE = 60.0
SETUP_01_KEY = "gate-c-environment-and-policy-setup-01"
SETUP_02_KEY = "gate-c-environment-and-policy-setup-02"
SETUP_01_FAILURE = v13.OUT / "reports" / f"{SETUP_01_KEY}-failure.json"
SETUP_01_RUNTIME = v13.OUT / "capture-preflight-runtime" / SETUP_01_KEY

SOURCE_PATHS = ("stage0_gate_bc_v14.py", "test_stage0_gate_bc_v14.py")
PARENT_PATHS = {
    "v13_source_lock": v13.LOCK_PATH,
    "v13_qualification_config": v13.CONFIG_PATH,
    "v13_static_report": v13.OUT / "reports" / "static-qualification-report-v13.json",
    "v13_resource_ledger": v13.LEDGER_PATH,
    "v13_gate_b_attempt": v13.OUT / "attempts" / f"{v13.GATE_B_KEY}.json",
    "v13_gate_b_report": v13.OUT / "reports" / "gate-b-live-public-privacy-report-v13.json",
    "v13_setup_01_attempt": v13.OUT / "attempts" / f"{SETUP_01_KEY}.json",
    "v13_setup_01_failure": SETUP_01_FAILURE,
    "v13_setup_01_runtime_environment": SETUP_01_RUNTIME / "environment.json",
    "v13_setup_01_runtime_config_lock": SETUP_01_RUNTIME / "config-lock.json",
    "gate_c_plan": v13.GATE_C_PLAN_PATH,
}


class GateBCV14Failure(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return runner.sha256_text_file(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def git(*args: str) -> str:
    return v13.git(*args)


def push_evidence(message: str) -> str:
    git("add", "--", OUT_RELATIVE)
    staged = [item for item in git("diff", "--cached", "--name-only").splitlines() if item]
    if any(item != OUT_RELATIVE and not item.startswith(f"{OUT_RELATIVE}/") for item in staged):
        raise GateBCV14Failure("refusing to commit files outside the V14 evidence directory")
    command, env = runner.git_invocation(REPOSITORY)
    if subprocess.run([*command, "diff", "--cached", "--quiet"], check=False, env=env).returncode:
        git("commit", "-m", message)
    head = git("rev-parse", "HEAD")
    git("push", "origin", f"HEAD:refs/heads/{BRANCH}")
    remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    if not remote or remote[0] != head:
        raise GateBCV14Failure("GitHub does not contain the exact V14 evidence commit")
    return head


def _current_head(*, require_remote_equal: bool) -> str:
    if git("branch", "--show-current") != BRANCH:
        raise GateBCV14Failure(f"expected active study branch {BRANCH}")
    head = git("rev-parse", "HEAD")
    if require_remote_equal:
        remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
        if not remote or remote[0] != head:
            raise GateBCV14Failure("remote branch is not at the exact V14 source/evidence head")
    return head


def _source_hashes(source_commit: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in SOURCE_PATHS:
        relative = f"{STUDY_RELATIVE}/{name}"
        blob = q.git_object_bytes(f"{source_commit}:{relative}")
        path = STUDY / name
        if not path.is_file():
            raise GateBCV14Failure(f"locked V14 source is missing: {name}")
        digest = sha256_bytes(blob)
        q.verify_locked_source_payload(
            label=f"study/{name}", expected_sha256=digest,
            committed_blob=blob, checkout_bytes=path.read_bytes(),
        )
        result[name] = digest
    return result


def _parent_input_hashes() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in PARENT_PATHS.items():
        if not path.is_file():
            raise GateBCV14Failure(f"required V13 parent evidence is missing: {name}")
        result[name] = sha256_file(path)
    return result


def select_setup_action(attempts: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """Select the registered setup slot without retrying any terminal attempt."""
    by_key = {str(row.get("key")): row for row in attempts}
    first = by_key.get(SETUP_01_KEY)
    if first is None:
        raise GateBCV14Failure("the immutable V13 setup-01 receipt is missing")
    if first.get("status") == "STARTED":
        raise GateBCV14Failure("V13 setup-01 is still STARTED; resolve its interrupted receipt before continuing")
    if first.get("status") == "COMPLETE":
        return SETUP_01_KEY, "REUSE"
    second = by_key.get(SETUP_02_KEY)
    if second is None:
        return SETUP_02_KEY, "START"
    if second.get("status") == "COMPLETE":
        return SETUP_02_KEY, "RESUME"
    raise GateBCV14Failure(f"fixed V13 recovery setup slot is terminal ({second.get('status')}); no retry")


def _validate_live_v13_ledger(lock: Mapping[str, Any]) -> dict[str, Any]:
    ledger = v13.ledger()
    snapshot = lock.get("v13_ledger_snapshot")
    if not isinstance(snapshot, Mapping):
        raise GateBCV14Failure("V14 lock has no immutable V13 ledger baseline")
    for field in (
        "schema_id", "baseline_builder_charge_cpu_seconds",
        "baseline_native_gate_bc_charge_cpu_seconds", "builder_ceiling_cpu_seconds",
        "aggregate_ceiling_cpu_seconds", "native_gate_bc_ceiling_cpu_seconds",
        "reviewer_reserve_cpu_seconds",
    ):
        if ledger.get(field) != snapshot.get(field):
            raise GateBCV14Failure(f"V13 ledger contract changed after V14 lock: {field}")
    current_rows = {str(row.get("key")): row for row in ledger.get("attempts", [])}
    original_rows = {str(row.get("key")): row for row in snapshot.get("attempts", [])}
    if len(current_rows) != len(ledger.get("attempts", [])):
        raise GateBCV14Failure("V13 ledger contains duplicate attempt keys")
    for key, original in original_rows.items():
        if current_rows.get(key) != original:
            raise GateBCV14Failure(f"immutable V13 attempt changed after V14 lock: {key}")
    allowed_new = {SETUP_02_KEY, *v13._planned_suffix_keys()}
    for key, row in current_rows.items():
        if key in original_rows:
            continue
        if key not in allowed_new:
            raise GateBCV14Failure(f"unregistered V13 Gate C attempt appeared after V14 lock: {key}")
        if (
            row.get("native_gate_bc") is not True
            or row.get("formal_seed_consumed") is not False
            or float(row.get("reserved_cpu_seconds", -1)) != v13.GATE_C_SETUP_RESERVE
                and key == SETUP_02_KEY
            or float(row.get("reserved_cpu_seconds", -1)) != v13.GATE_C_SUFFIX_RESERVE
                and key != SETUP_02_KEY
        ):
            raise GateBCV14Failure(f"V13 Gate C attempt violates its fixed resource contract: {key}")
    total, native = v13.charged(ledger)
    future_builder, future_native = v13._future_reserve(ledger)
    builder_projection = total + future_builder + STATIC_RESERVE
    native_projection = native + future_native
    aggregate_projection = builder_projection + v13.REVIEWER_RESERVE
    if (
        builder_projection > v13.BUILDER_CAP
        or native_projection > v13.NATIVE_CAP
        or aggregate_projection > v13.AGGREGATE_CAP
    ):
        raise GateBCV14Failure("combined V13/V14 resource projection exceeds the registered ceilings")
    return {
        "charged_v13_builder_cpu_seconds": total,
        "charged_v13_native_gate_bc_cpu_seconds": native,
        "remaining_v13_builder_reserve_cpu_seconds": future_builder,
        "remaining_v13_native_reserve_cpu_seconds": future_native,
        "v14_static_debit_cpu_seconds": STATIC_RESERVE,
        "projected_builder_cpu_seconds": builder_projection,
        "projected_native_gate_bc_cpu_seconds": native_projection,
        "projected_aggregate_including_reviewer_cpu_seconds": aggregate_projection,
        "builder_ceiling_cpu_seconds": v13.BUILDER_CAP,
        "native_gate_bc_ceiling_cpu_seconds": v13.NATIVE_CAP,
        "aggregate_ceiling_cpu_seconds": v13.AGGREGATE_CAP,
        "reviewer_protected_reserve_cpu_seconds": v13.REVIEWER_RESERVE,
        "forecast_fits_registered_caps": True,
    }


def _validate_parent_evidence(
    *, locked_hashes: Mapping[str, Any] | None = None,
    locked_ledger_snapshot: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    v13_lock, v13_config = v13.verify_lock()
    if (
        sha256_file(v13.LOCK_PATH) != "cc3ed0d6d50414316018b5216875307008384ad1e613cbece942207fb08f86b1"
        or v13_lock.get("code_source_commit") != "8aa33d4422002a636b61c2daf70d8dc8f61814c3"
        or v13_lock.get("stage1_authorized") is not False
        or v13_config.get("stage1_authorized") is not False
    ):
        raise GateBCV14Failure("V14 requires the independently reviewed, immutable V13 source lock")
    static_report = read_json(PARENT_PATHS["v13_static_report"])
    gate_b = read_json(PARENT_PATHS["v13_gate_b_report"])
    gate_b_attempt = read_json(PARENT_PATHS["v13_gate_b_attempt"])
    setup_01 = read_json(PARENT_PATHS["v13_setup_01_attempt"])
    setup_failure = read_json(SETUP_01_FAILURE)
    if (
        static_report.get("status") != "PASS"
        or static_report.get("source_lock_sha256") != sha256_file(v13.LOCK_PATH)
        or gate_b.get("status") != "PASS"
        or gate_b.get("source_lock_sha256") != sha256_file(v13.LOCK_PATH)
        or gate_b_attempt.get("key") != v13.GATE_B_KEY
        or gate_b_attempt.get("status") != "COMPLETE"
        or gate_b_attempt.get("formal_seed_consumed") is not False
        or setup_01.get("key") != SETUP_01_KEY
        or setup_01.get("status") != "FAIL_OR_INCOMPLETE"
        or setup_01.get("failure_type") != "KeyError"
        or setup_01.get("failure") != "'suffix_policy'"
        or setup_01.get("no_retry") is not True
        or setup_01.get("formal_seed_consumed") is not False
        or setup_failure.get("status") != "FAIL_OR_INCOMPLETE_NO_RETRY"
        or setup_failure.get("failure") != "'suffix_policy'"
        or setup_failure.get("no_retry") is not True
    ):
        raise GateBCV14Failure("V13 parent receipts are not the exact reviewed Gate B PASS / setup-01 failure lineage")
    if locked_ledger_snapshot is None:
        if (v13.OUT / "attempts" / f"{SETUP_02_KEY}.json").exists():
            raise GateBCV14Failure("V13 recovery setup slot already exists before the V14 lock")
        if any((v13.OUT / "attempts" / f"{key}.json").exists() for key in v13._planned_suffix_keys()):
            raise GateBCV14Failure("V13 suffix attempts already exist before the V14 lock")
    config = read_json(v13.CONFIG_PATH)
    suffix_policy = q.OLD_CONFIG["stage0"].get("pi_suffix")
    if not isinstance(suffix_policy, str) or not suffix_policy.strip():
        raise GateBCV14Failure("locked study config has no registered stage0.pi_suffix")
    if "suffix_policy" in q.OLD_CONFIG["stage0"]:
        raise GateBCV14Failure("V14 source contract unexpectedly contains the stale suffix_policy key")
    if config.get("code_source_commit") != v13_lock.get("code_source_commit"):
        raise GateBCV14Failure("V13 qualification config does not match its preseed code lock")
    plan = v13.read_json(v13.GATE_C_PLAN_PATH)
    if (
        plan.get("status") != "PRECOMMITTED_BEFORE_ANY_NEW_SUFFIX_RESULT"
        or len(plan.get("policy_rng_seed_streams", [])) != 64
        or tuple(row.get("root_id") for row in plan.get("roots", [])) != v13.GATE_C_ROOTS
    ):
        raise GateBCV14Failure("V14 must preserve the exact registered 64-stream Gate C plan")
    _validate_live_v13_ledger({"v13_ledger_snapshot": locked_ledger_snapshot or v13.ledger()})
    if locked_hashes is not None:
        for name, expected in locked_hashes.items():
            if name == "v13_resource_ledger":
                continue
            path = PARENT_PATHS[name]
            if sha256_file(path) != expected:
                raise GateBCV14Failure(f"immutable V13 parent input changed after V14 lock: {name}")
    return v13_lock, v13_config, plan


def prepare_lock() -> dict[str, Any]:
    if OUT.exists():
        raise GateBCV14Failure("V14 output already exists; never reconstruct or overwrite a source lock")
    head = _current_head(require_remote_equal=True)
    v13_lock, _v13_config, plan = _validate_parent_evidence()
    v13._required_environment()
    ledger_snapshot = v13.ledger()
    projection = _validate_live_v13_ledger({"v13_ledger_snapshot": ledger_snapshot})
    parent_hashes = _parent_input_hashes()
    out_value = {
        "schema_id": "issue73-stage0-gate-bc-v14-qualification-config-v1",
        "study_id": "issue-73-privileged-noncombat-oracle-stage0",
        "disposition": "STUDY_ONLY",
        "stage1_authorized": False,
        "formal_owner_or_training_seed_consumed": False,
        "code_source_commit": head,
        "v13_code_source_commit": v13_lock["code_source_commit"],
        "v13_source_lock_sha256": sha256_file(v13.LOCK_PATH),
        "parent_input_sha256": parent_hashes,
        "v13_ledger_snapshot": ledger_snapshot,
        "setup_failure_preserved": SETUP_01_KEY,
        "recovery_setup_key": SETUP_02_KEY,
        "suffix_policy_config_path": "config.json#/stage0/pi_suffix",
        "suffix_policy": q.OLD_CONFIG["stage0"]["pi_suffix"],
        "registered_gate_c_root_ids": list(v13.GATE_C_ROOTS),
        "registered_gate_c_plan_path": v13.GATE_C_PLAN_PATH.relative_to(STUDY).as_posix(),
        "registered_gate_c_plan_sha256": sha256_file(v13.GATE_C_PLAN_PATH),
        "registered_gate_c_suffix_attempt_count": len(plan["policy_rng_seed_streams"]),
        "resource_projection": projection,
        "native_rng_resampled": False,
        "no_population_action_endpoint_or_rng_changes": True,
        "no_stage1_or_training_authority": True,
    }
    OUT.mkdir(parents=True)
    (OUT / "attempts").mkdir()
    (OUT / "reports").mkdir()
    runner.atomic_json(CONFIG_PATH, out_value)
    lock = {
        "schema_id": "issue73-stage0-gate-bc-v14-preseed-source-lock-v1",
        "status": "PRESEED_LOCKED_BEFORE_GATE_C_RECOVERY_SETUP",
        "remote_branch": BRANCH,
        "code_source_commit": head,
        "study_source_sha256": _source_hashes(head),
        "qualification_config_sha256": sha256_file(CONFIG_PATH),
        "parent_input_sha256": parent_hashes,
        "v13_source_lock_sha256": sha256_file(v13.LOCK_PATH),
        "v13_code_source_commit": v13_lock["code_source_commit"],
        "v13_ledger_snapshot": ledger_snapshot,
        "v13_ledger_snapshot_sha256": parent_hashes["v13_resource_ledger"],
        "gate_c_plan_sha256": sha256_file(v13.GATE_C_PLAN_PATH),
        "gate_c_root_ids": list(v13.GATE_C_ROOTS),
        "gate_c_suffix_attempt_count": len(plan["policy_rng_seed_streams"]),
        "suffix_policy": q.OLD_CONFIG["stage0"]["pi_suffix"],
        "registered_caps_cpu_seconds": {
            "builder": v13.BUILDER_CAP,
            "aggregate_builder_reviewer": v13.AGGREGATE_CAP,
            "native_gate_b_c": v13.NATIVE_CAP,
            "reviewer_protected_reserve": v13.REVIEWER_RESERVE,
        },
        "fixed_static_debit_cpu_seconds": STATIC_RESERVE,
        "resource_projection": projection,
        "no_population_action_endpoint_or_rng_changes": True,
        "no_stage1_or_training_authority": True,
    }
    runner.atomic_json(LOCK_PATH, lock)
    attempt = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": STATIC_KEY,
        "kind": "stage0_gate_bc_v14_bounded_static_source_and_recovery_contract_qualification",
        "status": "RESERVED_CONSERVATIVE_STATIC_DEBIT",
        "reserved_cpu_seconds": STATIC_RESERVE,
        "cpu_charged_seconds": STATIC_RESERVE,
        "native_gate_bc": False,
        "formal_seed_consumed": False,
        "no_native_adapter_or_step": True,
        "source_lock_sha256": sha256_file(LOCK_PATH),
    }
    runner.atomic_json(OUT / "attempts" / f"{STATIC_KEY}.json", attempt)
    runner.atomic_json(LEDGER_PATH, {
        "schema_id": "issue73-stage0-gate-bc-v14-resource-ledger-v1",
        "baseline_v13_builder_charge_cpu_seconds": projection["charged_v13_builder_cpu_seconds"],
        "baseline_v13_native_charge_cpu_seconds": projection["charged_v13_native_gate_bc_cpu_seconds"],
        "fixed_v14_static_debit_cpu_seconds": STATIC_RESERVE,
        "registered_caps_cpu_seconds": lock["registered_caps_cpu_seconds"],
        "parent_input_sha256": parent_hashes,
        "attempts": [attempt],
    })
    push_evidence("Issue 73: prelock V14 Gate C recovery setup and exact policy provenance")
    return lock


def verify_lock(*, require_remote_equal: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    if not all(path.is_file() for path in (LOCK_PATH, CONFIG_PATH, LEDGER_PATH)):
        raise GateBCV14Failure("V14 source lock/config/ledger is missing")
    lock = read_json(LOCK_PATH)
    config = read_json(CONFIG_PATH)
    head = _current_head(require_remote_equal=require_remote_equal)
    command, env = runner.git_invocation(REPOSITORY)
    if subprocess.run([*command, "merge-base", "--is-ancestor", str(lock.get("code_source_commit", "")), head], check=False, env=env).returncode:
        raise GateBCV14Failure("current evidence head does not descend from the locked V14 source")
    if (
        lock.get("schema_id") != "issue73-stage0-gate-bc-v14-preseed-source-lock-v1"
        or lock.get("status") != "PRESEED_LOCKED_BEFORE_GATE_C_RECOVERY_SETUP"
        or lock.get("remote_branch") != BRANCH
        or lock.get("stage1_authorized") is not None
        or lock.get("no_stage1_or_training_authority") is not True
        or lock.get("study_source_sha256") != _source_hashes(str(lock["code_source_commit"]))
        or lock.get("qualification_config_sha256") != sha256_file(CONFIG_PATH)
        or config.get("code_source_commit") != lock.get("code_source_commit")
        or config.get("stage1_authorized") is not False
        or config.get("formal_owner_or_training_seed_consumed") is not False
        or config.get("suffix_policy") != q.OLD_CONFIG["stage0"].get("pi_suffix")
        or lock.get("suffix_policy") != config.get("suffix_policy")
        or lock.get("v13_source_lock_sha256") != sha256_file(v13.LOCK_PATH)
        or lock.get("v13_ledger_snapshot_sha256") != lock.get("parent_input_sha256", {}).get("v13_resource_ledger")
        or lock.get("parent_input_sha256") != config.get("parent_input_sha256")
        or lock.get("gate_c_plan_sha256") != sha256_file(v13.GATE_C_PLAN_PATH)
        or config.get("registered_gate_c_plan_sha256") != sha256_file(v13.GATE_C_PLAN_PATH)
    ):
        raise GateBCV14Failure("V14 lock, source, config, or registered policy/plan differs")
    _validate_parent_evidence(
        locked_hashes=lock["parent_input_sha256"],
        locked_ledger_snapshot=lock["v13_ledger_snapshot"],
    )
    ledger = read_json(LEDGER_PATH)
    attempts = ledger.get("attempts", [])
    if (
        ledger.get("schema_id") != "issue73-stage0-gate-bc-v14-resource-ledger-v1"
        or ledger.get("fixed_v14_static_debit_cpu_seconds") != STATIC_RESERVE
        or len(attempts) != 1
        or attempts[0].get("key") != STATIC_KEY
        or attempts[0].get("source_lock_sha256") != sha256_file(LOCK_PATH)
        or attempts[0].get("cpu_charged_seconds") != STATIC_RESERVE
        or attempts[0].get("native_gate_bc") is not False
    ):
        raise GateBCV14Failure("V14 fixed static debit ledger differs from its source lock")
    return lock, config


def _run_static_tests() -> dict[str, Any]:
    import test_stage0_gate_bc_v14

    suite = unittest.defaultTestLoader.loadTestsFromModule(test_stage0_gate_bc_v14)
    stream = __import__("io").StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {
        "status": "PASS" if result.wasSuccessful() and result.testsRun > 0 else "FAIL",
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "output": stream.getvalue()[-12000:],
    }


def run_static_verification() -> dict[str, Any]:
    lock, _config = verify_lock()
    report_path = OUT / "reports" / "static-qualification-report-v14.json"
    failure_path = OUT / "reports" / "static-qualification-failure-v14.json"
    if report_path.exists() or failure_path.exists():
        existing = read_json(report_path if report_path.exists() else failure_path)
        if existing.get("status") == "PASS":
            return existing
        raise GateBCV14Failure("V14 static verification has a terminal receipt; do not retry or overwrite")
    if v13.read_json(PARENT_PATHS["v13_gate_b_report"]).get("status") != "PASS":
        raise GateBCV14Failure("V14 static qualification requires the retained Gate B PASS")
    v13.q.install_process_cpu_ceiling(STATIC_RESERVE)
    started = v13.q.process_tree_cpu()
    try:
        tests = _run_static_tests()
        report = {
            "schema_id": "issue73-stage0-gate-bc-v14-static-qualification-report-v1",
            "status": tests["status"],
            "source_lock_sha256": sha256_file(LOCK_PATH),
            "code_source_commit": lock["code_source_commit"],
            "tests": tests,
            "measured_process_tree_cpu_seconds": max(0.0, v13.q.process_tree_cpu() - started),
            "fixed_static_reservation_cpu_seconds": STATIC_RESERVE,
            "v13_setup_01_failure_preserved": True,
            "native_adapter_instantiated": False,
            "native_game_step_started": False,
            "formal_seed_consumed": False,
            "stage1_authorized": False,
            "resource_projection": _validate_live_v13_ledger(lock),
        }
        runner.atomic_json(report_path, report)
        push_evidence("Issue 73: preserve Stage-0 Gate B/C v14 static recovery qualification")
        if report["status"] != "PASS":
            raise GateBCV14Failure("V14 focused static tests failed; no Gate C native phase started")
        return report
    except BaseException as exc:
        if report_path.exists():
            raise
        failure = {
            "schema_id": "issue73-stage0-gate-bc-v14-static-qualification-failure-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1200],
            "fixed_static_reservation_cpu_seconds": STATIC_RESERVE,
            "measured_process_tree_cpu_seconds": max(0.0, v13.q.process_tree_cpu() - started),
            "native_adapter_instantiated": False,
            "native_game_step_started": False,
            "formal_seed_consumed": False,
            "no_retry_or_overwrite": True,
        }
        runner.atomic_json(failure_path, failure)
        push_evidence("Issue 73: preserve Stage-0 Gate B/C v14 static qualification failure")
        raise


def _runtime_setup_v14() -> tuple[dict[str, Any], dict[str, Any], Any, Any, Any, str, list[dict[str, Any]]]:
    lock, _config = verify_lock()
    ledger = v13.ledger()
    if any(
        row.get("key") == SETUP_02_KEY and row.get("status") == "STARTED"
        for row in ledger.get("attempts", [])
    ):
        v13.start_attempt(SETUP_02_KEY, kind="gate_c_environment_and_policy_setup", reserve=v13.GATE_C_SETUP_RESERVE, native_gate_bc=True)
    key, action = select_setup_action(ledger.get("attempts", []))
    if action == "START":
        v13.start_attempt(key, kind="gate_c_environment_and_policy_setup", reserve=v13.GATE_C_SETUP_RESERVE, native_gate_bc=True)
        started_cpu = v13.q.process_tree_cpu()
        runtime_out = v13.OUT / "capture-preflight-runtime" / key
        try:
            prepared = v13._prepare_v13_runtime(runtime_out)
            setup_report = {
                "schema_id": "issue73-gate-c-replay-setup-v14-v1",
                "status": "READY",
                "setup_attempt_key": key,
                "source_lock_sha256": sha256_file(LOCK_PATH),
                "v13_source_lock_sha256": sha256_file(v13.LOCK_PATH),
                "native_module_path": str(Path(prepared["module"].__file__).resolve()),
                "native_module_sha256": prepared["environment"]["identity"]["native_module_sha256"],
                "source_history_sha256": sha256_file(v13.e5.CAPTURE_HISTORY_PATH),
                "gate_c_plan_sha256": sha256_file(v13.GATE_C_PLAN_PATH),
                "policy_artifact_sha256": runner.sha256_file(prepared["model_path"]),
                "policy_tensor_digest": prepared["model_digest"],
                "suffix_policy": q.OLD_CONFIG["stage0"]["pi_suffix"],
                "setup_metadata_field": "config.json#/stage0/pi_suffix",
                "native_rng_resampling": "none",
                "formal_seed_consumed": False,
                "stage1_authorized": False,
            }
        except BaseException as exc:
            failure = {
                "schema_id": "issue73-gate-c-replay-setup-failure-v14-v1",
                "status": "FAIL_OR_INCOMPLETE_NO_RETRY",
                "failure_type": type(exc).__name__,
                "failure": str(exc)[:1200],
                "no_retry": True,
                "formal_seed_consumed": False,
            }
            runner.atomic_json(v13.OUT / "reports" / f"{key}-failure-v14.json", failure)
            v13.finish_attempt(key, started_cpu=started_cpu, status="FAIL_OR_INCOMPLETE", details=failure)
            raise
        finished = v13.finish_attempt(key, started_cpu=started_cpu, status="COMPLETE", details={"setup_status": "READY", "formal_seed_consumed": False})
        setup_report["attempt_receipt"] = finished
        if finished.get("status") != "COMPLETE":
            setup_report["status"] = finished.get("status")
            runner.atomic_json(v13.OUT / "reports" / f"{key}-report-v14.json", setup_report)
            raise GateBCV14Failure("fixed V13 recovery setup did not complete within its 20 CPU-s reserve")
        runner.atomic_json(v13.OUT / "reports" / f"{key}-report-v14.json", setup_report)
        v13.push_evidence("Issue 73: preserve V14 repair of the fixed Gate C setup recovery slot")
        return lock, v13.read_json(v13.CONFIG_PATH), prepared["module"], prepared["encoder"], prepared["model"], prepared["model_digest"], v13.load_history()

    if action != "RESUME" or key != SETUP_02_KEY:
        raise GateBCV14Failure("the V13 setup-01 failure cannot be retried; V14 requires its fixed recovery slot")
    runtime_out = v13.OUT / "capture-preflight-runtime" / f"{key}-resume-{int(time.time())}"
    start_cpu = v13.q.process_tree_cpu()
    prepared = v13._prepare_v13_runtime(runtime_out)
    preparation_cpu = max(0.0, v13.q.process_tree_cpu() - start_cpu)
    report_path = v13.OUT / "reports" / f"{key}-resume-{int(time.time())}.json"
    runner.atomic_json(report_path, {
        "schema_id": "issue73-gate-c-replay-runtime-resume-v14-v1",
        "status": "READY",
        "setup_attempt_key": key,
        "source_lock_sha256": sha256_file(LOCK_PATH),
        "runtime_output": runtime_out.relative_to(v13.STUDY).as_posix(),
        "runtime_preparation_cpu_seconds_to_charge_to_next_suffix": preparation_cpu,
        "suffix_policy": q.OLD_CONFIG["stage0"]["pi_suffix"],
        "formal_seed_consumed": False,
        "stage1_authorized": False,
    })
    original_cpu = v13.q.process_tree_cpu
    first_call = {"pending": True}

    def cpu_with_resume_preparation() -> float:
        current = original_cpu()
        if first_call["pending"]:
            first_call["pending"] = False
            return max(0.0, current - preparation_cpu)
        return current

    original_finish = v13.finish_attempt
    finish_pending = {"pending": True}

    def finish_with_resume_detail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if finish_pending["pending"]:
            details = dict(kwargs.get("details", {}))
            details["v14_resume_runtime_preparation_cpu_seconds"] = preparation_cpu
            kwargs["details"] = details
            finish_pending["pending"] = False
        return original_finish(*args, **kwargs)

    v13.q.process_tree_cpu = cpu_with_resume_preparation
    v13.finish_attempt = finish_with_resume_detail
    _RESUME_PATCHES.append((original_cpu, original_finish))
    return lock, v13.read_json(v13.CONFIG_PATH), prepared["module"], prepared["encoder"], prepared["model"], prepared["model_digest"], v13.load_history()


_RESUME_PATCHES: list[tuple[Any, Any]] = []


def run_gate_c() -> dict[str, Any]:
    lock, _config = verify_lock()
    static_path = OUT / "reports" / "static-qualification-report-v14.json"
    if not static_path.is_file() or read_json(static_path).get("status") != "PASS":
        raise GateBCV14Failure("Gate C recovery requires the retained V14 static qualification PASS")
    if not read_json(PARENT_PATHS["v13_gate_b_report"]).get("status") == "PASS":
        raise GateBCV14Failure("Gate C recovery requires the retained V13 live Gate B PASS")
    original_setup = v13.setup_gate_c_v13
    _RESUME_PATCHES.clear()
    v13.setup_gate_c_v13 = _runtime_setup_v14
    try:
        report = v13.run_gate_c()
        runner.atomic_json(OUT / "reports" / "gate-c-v14-handoff.json", {
            "schema_id": "issue73-stage0-gate-bc-v14-gate-c-handoff-v1",
            "status": report.get("status"),
            "v14_source_lock_sha256": sha256_file(LOCK_PATH),
            "v13_source_lock_sha256": sha256_file(v13.LOCK_PATH),
            "v13_gate_c_report_sha256": sha256_file(v13.OUT / "reports" / "gate-c-replay-uncertainty-report.json"),
            "registered_suffix_count": 64,
            "resource_projection": _validate_live_v13_ledger(lock),
            "formal_seed_consumed": False,
            "stage1_authorized": False,
            "code_disposition": "STUDY_ONLY",
        })
        push_evidence("Issue 73: preserve V14 Gate C repair handoff")
        return report
    finally:
        v13.setup_gate_c_v13 = original_setup
        if _RESUME_PATCHES:
            original_cpu, original_finish = _RESUME_PATCHES[-1]
            v13.q.process_tree_cpu = original_cpu
            v13.finish_attempt = original_finish
            _RESUME_PATCHES.clear()


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare-lock", "verify-lock", "static", "gate-c"))
    args = parser.parse_args(argv)
    if args.command == "prepare-lock":
        result = prepare_lock()
    elif args.command == "verify-lock":
        lock, _config = verify_lock()
        result = {"status": "PASS", "source_lock_sha256": sha256_file(LOCK_PATH), "code_source_commit": lock["code_source_commit"]}
    elif args.command == "static":
        result = run_static_verification()
    else:
        result = run_gate_c()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"PASS", "READY", "PRESEED_LOCKED_BEFORE_GATE_C_RECOVERY_SETUP"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
