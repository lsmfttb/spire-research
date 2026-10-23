from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import checkpoint_replay_qualification as q
import runner


STUDY = q.STUDY
REPOSITORY = q.REPOSITORY
STUDY_RELATIVE = q.STUDY_RELATIVE
V7_OUT = STUDY / "receipts" / "source-qualification-v7"
V7_RELATIVE = V7_OUT.relative_to(REPOSITORY).as_posix()
CONFIG_PATH = V7_OUT / "qualification-config-v7.json"
PLAN_PATH = V7_OUT / "plans" / "gate-c-replay-plan-v7.json"
LOCK_PATH = V7_OUT / "preseed-source-lock-v7.json"
INPUT_MANIFEST_PATH = V7_OUT / "input-manifest-v7.json"
RESOURCE_LEDGER_PATH = V7_OUT / "resource-ledger.json"
RESOURCE_SNAPSHOT_PATH = V7_OUT / "ledgers" / "resource-ledger-before-v7.json"
STATIC_RESERVATION_PATH = V7_OUT / "attempts" / "builder-prelock-static-verification-v7.json"
STATIC_RESERVATION_KEY = "builder-prelock-static-verification-v7"
STATIC_RESERVATION_CPU_SECONDS = 60.0
CAPTURE_ATTEMPT_KEY = "source-history-capture-173730001-prospective-v7"

HISTORICAL_INPUT_BASE_COMMIT = "80729ed02e3af698a71d33538169f7aa31209457"
HISTORICAL_PREFIXES = tuple(
    f"{STUDY_RELATIVE}/receipts/{name}/"
    for name in (
        "stage0",
        "source-qualification-v1",
        "source-qualification-v4",
        "source-qualification-v5",
        "source-qualification-v6",
    )
)
OPTIONAL_RUNTIME_OUTPUT = (
    f"{STUDY_RELATIVE}/receipts/source-qualification-v6/"
    "smoke-runtime/runtime-metrics.jsonl"
)
REQUIRED_INPUT_ANCHORS = (
    f"{STUDY_RELATIVE}/receipts/stage0/preseed-source-lock-v3.json",
    f"{STUDY_RELATIVE}/receipts/stage0/resource-ledger.json",
    f"{STUDY_RELATIVE}/receipts/stage0/debug-owner-runs/173730001.json",
    f"{STUDY_RELATIVE}/receipts/stage0/attempts/debug-owner-173730001.json",
    f"{STUDY_RELATIVE}/receipts/source-qualification-v5/resource-ledger.json",
    f"{STUDY_RELATIVE}/receipts/source-qualification-v6/qualification-config-v6.json",
    f"{STUDY_RELATIVE}/receipts/source-qualification-v6/plans/gate-c-replay-plan-v6.json",
    f"{STUDY_RELATIVE}/receipts/source-qualification-v6/resource-ledger.json",
    f"{STUDY_RELATIVE}/receipts/source-qualification-v6/reports/v6-static-qualification-failure.json",
    f"{STUDY_RELATIVE}/receipts/source-qualification-v6/reports/pre-capture-smoke-v6.json",
)
SOURCE_HASH_PATHS_V7 = (
    "checkpoint_replay_qualification.py",
    "config.json",
    "config.lock.sha256",
    "gate-a-target-direction.json",
    "gate_a.py",
    "policy.py",
    "public_history_execution.py",
    "runner.py",
    "source_lock_integrity.py",
    "source_qualification_v7.py",
    "stage0.py",
    "test_pre_capture_smoke_v7.py",
    "test_source_manifest_v7.py",
)
STATIC_TEST_REPORT_PATH = V7_OUT / "reports" / "static-qualification-report-v7.json"
STATIC_TEST_FAILURE_PATH = V7_OUT / "reports" / "static-qualification-failure-v7.json"
INPUT_AUDIT_REPORT_PATH = V7_OUT / "reports" / "input-manifest-prelock-v7.json"
PREFLIGHT_TEST_ROOT = V7_OUT / "reports" / "capture-preflight-tests-v7"
V7_NATIVE_MODULE_PATH = q.V6_CONFIGURED_NATIVE_MODULE_PATH


class QualificationV7Failure(RuntimeError):
    pass


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def exact_membership(
    baseline_paths: Sequence[str],
    current_paths: Sequence[str],
    *,
    optional_paths: Sequence[str] = (OPTIONAL_RUNTIME_OUTPUT,),
) -> list[str]:
    optional = set(optional_paths)
    baseline = set(baseline_paths)
    current = set(current_paths)
    if not set(REQUIRED_INPUT_ANCHORS).issubset(baseline):
        missing = sorted(set(REQUIRED_INPUT_ANCHORS) - baseline)
        raise QualificationV7Failure(f"historical source baseline omits required anchors: {missing}")
    expected = baseline - optional
    observed = current - optional
    if observed != expected:
        raise QualificationV7Failure(
            "historical tracked input membership changed: "
            f"missing={sorted(expected - observed)}, unexpected={sorted(observed - expected)}"
        )
    return sorted(expected)


def ensure_inputs_present(study_root: Path, paths: Sequence[str]) -> None:
    root = study_root.resolve()
    missing: list[str] = []
    unsafe: list[str] = []
    for repository_path in paths:
        relative = Path(repository_path)
        if relative.is_absolute() or ".." in relative.parts:
            unsafe.append(repository_path)
            continue
        path = REPOSITORY / relative
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            missing.append(repository_path)
            continue
        if not resolved.is_relative_to(root) or not resolved.is_file():
            unsafe.append(repository_path)
    if unsafe:
        raise QualificationV7Failure(f"historical inputs resolve outside the study checkout: {sorted(unsafe)}")
    if missing:
        raise QualificationV7Failure(f"mandatory historical inputs are missing: {sorted(missing)}")


def git_tree_paths(commit: str) -> list[str]:
    result: set[str] = set()
    for prefix in HISTORICAL_PREFIXES:
        raw = q.git("ls-tree", "-r", "--name-only", commit, "--", prefix)
        result.update(line for line in raw.splitlines() if line)
    return sorted(result)


def source_file_entry(source_commit: str, repository_path: str) -> dict[str, Any]:
    blob = q.external_git_blob(REPOSITORY, source_commit, repository_path)
    checkout = REPOSITORY / repository_path
    try:
        checkout_bytes = checkout.read_bytes()
    except OSError as exc:
        raise QualificationV7Failure(f"mandatory historical input cannot be read: {repository_path}: {exc}") from exc
    binary = Path(repository_path).suffix.lower() in {".pt", ".so", ".dll", ".bin"} or b"\0" in blob
    if binary:
        if checkout_bytes != blob:
            raise QualificationV7Failure(f"{repository_path} binary checkout differs from its exact Git blob bytes")
        checkout_matches = True
        checkout_status = "PASS_EXACT_BINARY_GIT_BLOB"
    else:
        normalized = q.verify_locked_source_payload(
            label=repository_path,
            expected_sha256=sha256_bytes(blob),
            committed_blob=blob,
            checkout_bytes=checkout_bytes,
        )
        checkout_matches = normalized.get("status") == "PASS_EXACT_GIT_BLOB_WITH_EOL_ONLY_CHECKOUT_CONVERSION"
        checkout_status = str(normalized.get("status"))
    return {
        "path": repository_path,
        "file_kind": "binary_raw_bytes" if binary else "text_git_blob_with_eol_only_checkout_conversion",
        "git_blob_bytes": len(blob),
        "git_blob_sha256": sha256_bytes(blob),
        "checkout_raw_bytes": len(checkout_bytes),
        "checkout_raw_sha256": sha256_bytes(checkout_bytes),
        "checkout_matches_locked_blob": checkout_matches,
        "checkout_verification_status": checkout_status,
    }


def historical_input_manifest(source_commit: str) -> dict[str, Any]:
    baseline_paths = git_tree_paths(HISTORICAL_INPUT_BASE_COMMIT)
    current_paths = git_tree_paths(source_commit)
    included_paths = exact_membership(baseline_paths, current_paths)
    ensure_inputs_present(STUDY, included_paths)
    entries = [source_file_entry(source_commit, path) for path in included_paths]
    optional = REPOSITORY / OPTIONAL_RUNTIME_OUTPUT
    optional_present = optional.is_file()
    return {
        "schema_id": "issue73-v7-bounded-historical-input-manifest-v1",
        "status": "PASS",
        "baseline_commit": HISTORICAL_INPUT_BASE_COMMIT,
        "code_source_commit": source_commit,
        "historical_source_prefixes": list(HISTORICAL_PREFIXES),
        "included_file_count": len(entries),
        "included_bytes": sum(int(row["git_blob_bytes"]) for row in entries),
        "included": entries,
        "excluded": [
            {
                "path": OPTIONAL_RUNTIME_OUTPUT,
                "category": "optional prior smoke diagnostic runtime output",
                "reason": "it is not an immutable source input and absence must not block source qualification",
                "present_when_manifest_created": optional_present,
            },
            {
                "path_prefix": f"{STUDY_RELATIVE}/receipts/source-qualification-v7/",
                "category": "newly generated v7 receipts, config, plan, lock, and runtime outputs",
                "reason": "prospective v7 outputs are not historical source inputs",
            },
        ],
        "membership_rule": (
            "the included path set is exactly the Git-tree membership from the registered v6 source "
            "commit across the five named historical receipt prefixes, less only the single explicit "
            "optional metrics output; any missing, added, or renamed historical path fails closed"
        ),
    }


def _verify_source_commit_and_remote() -> str:
    source_commit = q.git("rev-parse", "HEAD")
    remote = q.git("ls-remote", "origin", f"refs/heads/{q.BRANCH}").split()
    if not remote or remote[0] != source_commit:
        raise QualificationV7Failure("v7 source-lock source must be the exact pushed issue branch head")
    command, env = runner.git_invocation(REPOSITORY)
    base_ok = subprocess.run(
        [*command, "merge-base", "--is-ancestor", q.EXPECTED_BASE_COMMIT, source_commit],
        check=False,
        env=env,
    ).returncode == 0
    history_ok = subprocess.run(
        [*command, "merge-base", "--is-ancestor", HISTORICAL_INPUT_BASE_COMMIT, source_commit],
        check=False,
        env=env,
    ).returncode == 0
    if not base_ok or not history_ok:
        raise QualificationV7Failure("v7 source does not preserve the exact registered study lineage")
    return source_commit


def _source_hashes(source_commit: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name in SOURCE_HASH_PATHS_V7:
        blob = q.external_git_blob(REPOSITORY, source_commit, f"{STUDY_RELATIVE}/{name}")
        checkout_path = STUDY / name
        q.verify_locked_source_payload(
            label=f"study/{name}",
            expected_sha256=sha256_bytes(blob),
            committed_blob=blob,
            checkout_bytes=checkout_path.read_bytes(),
        )
        hashes[name] = sha256_bytes(blob)
    return hashes


def _v7_resource_ledger() -> dict[str, Any]:
    if not RESOURCE_LEDGER_PATH.is_file():
        raise QualificationV7Failure("v7 cumulative resource ledger is missing")
    value = read_json(RESOURCE_LEDGER_PATH)
    if value.get("schema_id") != "issue73-checkpoint-replay-resource-ledger-v1":
        raise QualificationV7Failure("v7 cumulative resource ledger schema is invalid")
    return value


def _v7_charged(value: Mapping[str, Any]) -> tuple[float, float]:
    return q.charged(value)


def reserve_static_v7() -> dict[str, Any]:
    if any(path.exists() for path in (STATIC_RESERVATION_PATH, RESOURCE_LEDGER_PATH, RESOURCE_SNAPSHOT_PATH)):
        raise QualificationV7Failure("v7 static reservation or ledger is partial or already exists; no retry is permitted")
    prior = read_json(q.OUT / "resource-ledger.json")
    builder_before, native_before = q.charged(prior)
    if abs(builder_before - 2137.891659) > 1e-6:
        raise QualificationV7Failure(f"cumulative Builder charge differs from authorized v7 baseline: {builder_before:.6f}")
    if abs(native_before - 1260.0) > 1e-6:
        raise QualificationV7Failure(f"native Gate B+C charge differs from authorized v7 baseline: {native_before:.6f}")
    projected_builder = builder_before + STATIC_RESERVATION_CPU_SECONDS + 1500.0 + 120.0
    projected_native = native_before + 1500.0
    if projected_builder > 4800.0 or projected_native > 2760.0:
        raise QualificationV7Failure("v7 authorization no longer fits the registered CPU ceilings")
    receipt = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",        "key": STATIC_RESERVATION_KEY,
        "kind": "v7_bounded_static_input_manifest_lock_and_preflight_qualification",
        "status": "RESERVED_CONSERVATIVE_STATIC_DEBIT",
        "native_gate_bc": False,
        "reserved_cpu_seconds": STATIC_RESERVATION_CPU_SECONDS,
        "cpu_charged_seconds": STATIC_RESERVATION_CPU_SECONDS,
        "measurement_status": "FIXED_RESERVATION_PRECHARGED; NO REFUND",
        "source_lock_sha256": None,
        "formal_seed_consumed": False,
        "observed_process_cpu_seconds": max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU),
        "note": (
            "One-time v7 non-native static reservation, charged in full before v7 prepare-lock, "
            "manifest, or production capture-preflight verification."
        ),
    }
    updated = json.loads(json.dumps(prior))
    updated.setdefault("attempts", []).append(receipt)
    runner.atomic_json(RESOURCE_SNAPSHOT_PATH, prior)
    runner.atomic_json(STATIC_RESERVATION_PATH, receipt)
    runner.atomic_json(RESOURCE_LEDGER_PATH, updated)
    push_v7("Issue 73: precharge one-time v7 static qualification")
    return receipt


def ensure_static_precharge_v7() -> tuple[dict[str, Any], dict[str, Any]]:
    if not STATIC_RESERVATION_PATH.is_file() or not RESOURCE_LEDGER_PATH.is_file() or not RESOURCE_SNAPSHOT_PATH.is_file():
        raise QualificationV7Failure("the v7 one-time 60 CPU-s static reservation is not durably precharged")
    reservation = read_json(STATIC_RESERVATION_PATH)
    ledger = _v7_resource_ledger()
    snapshot = read_json(RESOURCE_SNAPSHOT_PATH)
    rows = [row for row in ledger.get("attempts", []) if row.get("key") == STATIC_RESERVATION_KEY]
    if (
        reservation.get("status") != "RESERVED_CONSERVATIVE_STATIC_DEBIT"
        or reservation.get("reserved_cpu_seconds") != STATIC_RESERVATION_CPU_SECONDS
        or reservation.get("cpu_charged_seconds") != STATIC_RESERVATION_CPU_SECONDS
        or reservation.get("native_gate_bc") is not False
        or reservation.get("formal_seed_consumed") is not False
        or rows != [reservation]
        or snapshot.get("schema_id") != "issue73-checkpoint-replay-resource-ledger-v1"
    ):
        raise QualificationV7Failure("v7 reservation, cumulative ledger, or immutable prior snapshot is inconsistent")
    expected_ledger = json.loads(json.dumps(snapshot))
    expected_ledger.setdefault("attempts", []).append(reservation)
    if ledger != expected_ledger:
        raise QualificationV7Failure("v7 cumulative ledger differs from the exact immutable baseline plus one fixed reservation")
    baseline_builder, baseline_native = q.charged(snapshot)
    if abs(baseline_builder - 2137.891659) > 1e-6 or abs(baseline_native - 1260.0) > 1e-6:
        raise QualificationV7Failure("immutable v7 precharge snapshot does not match the authorized resource baseline")
    return reservation, ledger


def _stage0_resource_projection(ledger: Mapping[str, Any]) -> dict[str, Any]:
    builder, native = _v7_charged(ledger)
    native_remaining = 2760.0 - native
    native_reserve = 60.0 + 80.0 + 20.0 + 64.0 * 20.0 + 60.0
    builder_reserve = native_reserve + 120.0
    projected_builder = builder + builder_reserve
    projected_native = native + native_reserve
    full_fits = projected_builder <= 4800.0 and projected_native <= 2760.0
    return {
        "aggregate_stage0_cpu_seconds": 6000.0,
        "builder_ceiling_cpu_seconds": 4800.0,
        "reviewer_reserve_cpu_seconds": 1200.0,
        "prior_builder_charge_cpu_seconds": builder,
        "builder_remaining_before_native_and_gate_a_cpu_seconds": 4800.0 - builder,
        "prior_native_gate_bc_charge_cpu_seconds": native,
        "native_gate_bc_ceiling_cpu_seconds": 2760.0,
        "native_gate_bc_remaining_cpu_seconds": native_remaining,
        "source_history_capture_reserve_cpu_seconds": 60.0,
        "fresh_process_prefix_and_transition_parity_reserve_cpu_seconds": 80.0,
        "gate_c_environment_setup_reserve_cpu_seconds": 20.0,
        "gate_c_suffix_attempt_count": 64,        "gate_c_per_suffix_process_cpu_cap_seconds": 20.0,
        "gate_c_suffix_total_reserve_cpu_seconds": 64.0 * 20.0,
        "native_engineering_contingency_cpu_seconds": 60.0,
        "projected_new_native_cpu_reserve_seconds": native_reserve,
        "gate_a_nonnative_process_cpu_cap_seconds": 120.0,
        "projected_new_builder_cpu_reserve_seconds": builder_reserve,
        "cumulative_builder_projection_cpu_seconds": projected_builder,
        "remaining_builder_cpu_seconds_after_max_reserve": 4800.0 - projected_builder,
        "cumulative_native_gate_bc_projection_cpu_seconds": projected_native,
        "remaining_native_gate_bc_cpu_seconds_after_max_reserve": 2760.0 - projected_native,
        "full_stage0_forecast_fits_current_caps": full_fits,
        "reviewer_reserve_untouched": True,
        "no_prior_charge_reclaimed": True,
        "stage1_authorized": False,
    }


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    payload = (runner.canonical(manifest) + "\n").encode("utf-8")
    return sha256_bytes(payload)


def prepare_lock_v7() -> dict[str, Any]:
    lock_paths = (LOCK_PATH, CONFIG_PATH, PLAN_PATH, INPUT_MANIFEST_PATH, INPUT_AUDIT_REPORT_PATH)
    existing = [path.exists() for path in lock_paths]
    if any(existing):
        if not all(existing):
            raise QualificationV7Failure("partial v7 source/config/plan/manifest exists; it may not be reconstructed")
        lock, _config = verify_lock_v7(require_remote_current=False)
        push_v7("Issue 73: preserve existing prospective v7 source lock")
        return lock

    ensure_static_precharge_v7()
    source_commit = _verify_source_commit_and_remote()
    baseline_paths = git_tree_paths(HISTORICAL_INPUT_BASE_COMMIT)
    current_paths = git_tree_paths(source_commit)
    input_paths = exact_membership(baseline_paths, current_paths)
    # This check deliberately precedes config/plan/manifest creation.
    ensure_inputs_present(STUDY, input_paths)
    if STATIC_TEST_FAILURE_PATH.exists():
        raise QualificationV7Failure("an immutable v7 static qualification failure exists")
    if not q.V5_RESOURCE_LEDGER_PATH.is_file() or not q.V5_STATIC_PREFLIGHT_REPORT_PATH.is_file():
        raise QualificationV7Failure("mandatory v5 ledger or smoke receipt is missing")
    q.configure_stsr_python_root()
    external_sources = q.verify_locked_external_sources()
    code_hashes = _source_hashes(source_commit)
    manifest = historical_input_manifest(source_commit)
    config, _roots, plan = q.make_qualification_config(source_ledger=_v7_resource_ledger())
    config = json.loads(json.dumps(config))
    config["schema_id"] = "issue73-checkpoint-replay-qualification-config-v7"
    config["gate_c_plan_file"] = PLAN_PATH.relative_to(STUDY).as_posix()
    config["source_history_attempt_key"] = CAPTURE_ATTEMPT_KEY
    config["source_qualification_version"] = "v7"
    config["input_manifest"] = {
        "path": INPUT_MANIFEST_PATH.relative_to(STUDY).as_posix(),
        "sha256": _manifest_digest(manifest),
        "included_file_count": manifest["included_file_count"],
        "included_bytes": manifest["included_bytes"],
    }
    config["configured_native_module_path"] = V7_NATIVE_MODULE_PATH
    resource_projection = _stage0_resource_projection(_v7_resource_ledger())
    config["resource_caps"] = {
        **config["resource_caps"],
        **resource_projection,
        "prelock_resource_ledger_sha256": sha256_file(RESOURCE_SNAPSHOT_PATH),
        "prelock_static_debit_cpu_seconds": STATIC_RESERVATION_CPU_SECONDS,
        "no_prior_charge_reclaimed": True,
    }
    if not resource_projection["full_stage0_forecast_fits_current_caps"]:
        raise QualificationV7Failure("the complete fixed Stage-0 budget no longer fits current caps")
    plan = json.loads(json.dumps(plan))
    plan["schema_id"] = "issue73-checkpoint-replay-gate-c-plan-v7"
    plan["qualification_config_file"] = CONFIG_PATH.relative_to(STUDY).as_posix()
    plan["source_qualification_version"] = "v7"
    for root in plan.get("roots", []):
        root_path = q.root_path(str(root["root_id"]))
        root_relative = root_path.relative_to(REPOSITORY).as_posix()
        root["historical_root_receipt_sha256"] = sha256_bytes(
            q.external_git_blob(REPOSITORY, source_commit, root_relative)
        )
    if (
        [row.get("root_id") for row in plan.get("roots", [])] != list(q.GATE_C_ROOTS)
        or len(plan.get("policy_rng_seed_streams", [])) != 64
        or plan.get("planned_suffix_attempt_count") != 64
    ):
        raise QualificationV7Failure("v7 plan differs from the fixed four-root, eight-per-action Gate C contract")
    config["historical_input_manifest_sha256"] = _manifest_digest(manifest)
    runner.atomic_json(INPUT_MANIFEST_PATH, manifest)
    runner.atomic_json(CONFIG_PATH, config)
    plan["qualification_config_sha256"] = sha256_file(CONFIG_PATH)
    runner.atomic_json(PLAN_PATH, plan)

    lock = {
        "schema_id": "issue73-checkpoint-replay-preseed-source-lock-v7",
        "lock_revision": 7,
        "status": "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION",
        "study_only": True,
        "formal_training_seed_consumed": False,
        "formal_evaluation_seed_consumed": False,
        "stage1_authorized": False,
        "code_source_commit": source_commit,
        "remote_branch": q.BRANCH,
        "remote_must_equal_current_evidence_head_before_any_run": True,
        "historical_input_base_commit": HISTORICAL_INPUT_BASE_COMMIT,
        "historical_input_prefixes": list(HISTORICAL_PREFIXES),
        "historical_input_manifest": INPUT_MANIFEST_PATH.relative_to(STUDY).as_posix(),
        "historical_input_manifest_sha256": _manifest_digest(manifest),
        "historical_input_sha256": {
            row["path"]: row["git_blob_sha256"] for row in manifest["included"]
        },
        "optional_runtime_output_exclusions": manifest["excluded"],
        "qualification_config": CONFIG_PATH.relative_to(STUDY).as_posix(),
        "qualification_config_sha256": sha256_file(CONFIG_PATH),
        "gate_c_plan": PLAN_PATH.relative_to(STUDY).as_posix(),
        "gate_c_plan_sha256": sha256_file(PLAN_PATH),
        "study_source_sha256": code_hashes,
        "native": {
            "repository": q.OLD_CONFIG["identity"]["native_repository"],
            "commit": q.OLD_CONFIG["identity"]["native_commit"],
            "source_branch": q.OLD_CONFIG["identity"]["native_source_branch"],
            "source_file_sha256": q.OLD_CONFIG["identity"]["native_source_files_sha256"],
            "loaded_module": q.OLD_CONFIG["identity"]["native_module"],
            "loaded_module_sha256": q.OLD_CONFIG["identity"]["native_binary_sha256"],
            "module_path": V7_NATIVE_MODULE_PATH,
        },
        "stsr": {
            "repository": q.OLD_CONFIG["identity"]["stsr_repository"],
            "commit": q.OLD_CONFIG["identity"]["stsr_commit"],
            "native_integration_commit": q.OLD_CONFIG["identity"]["stsr_native_integration_commit"],
            "source_file_sha256": q.OLD_CONFIG["identity"]["stsr_source_files_sha256"],
        },
        "runtime": {
            name: q.OLD_CONFIG["identity"][name]
            for name in ("python", "torch", "numpy")
        },
        "stsr_python_package_root": q.STSRL_PYTHON_PACKAGE_ROOT,
        "external_source_verification": external_sources,
        "v7_static_reservation": {
            "path": STATIC_RESERVATION_PATH.relative_to(STUDY).as_posix(),
            "sha256": sha256_file(STATIC_RESERVATION_PATH),
            "prior_ledger_snapshot": RESOURCE_SNAPSHOT_PATH.relative_to(STUDY).as_posix(),
            "prior_ledger_snapshot_sha256": sha256_file(RESOURCE_SNAPSHOT_PATH),
        },
        "resource_caps": resource_projection,        "fixed_gate_contract": {
            "gate_a_root_count": 24,
            "gate_b_witness_count": 3,
            "gate_c_root_count": 4,
            "gate_c_action_occurrences_per_root": 2,
            "gate_c_suffixes_per_action": 8,
            "gate_c_total_suffixes": 64,
            "suffix_policy": "0.80 expert_non_combat_v1 + 0.20 uniform among currently legal actions",
            "battle_search": "Battle Search-v2@400",
        },
        "lock_preparation_process_cpu_seconds": max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU),
        "source_lock_created_before_any_new_debug_execution": True,
    }
    runner.atomic_json(LOCK_PATH, lock)
    push_v7("Issue 73: commit prospective v7 replay source lock")
    return lock


def verify_lock_v7(*, require_remote_current: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    if not all(path.is_file() for path in (LOCK_PATH, CONFIG_PATH, PLAN_PATH, INPUT_MANIFEST_PATH)):
        raise QualificationV7Failure("prospective v7 source/config/plan/input manifest is missing")
    ensure_static_precharge_v7()
    lock = read_json(LOCK_PATH)
    config = read_json(CONFIG_PATH)
    plan = read_json(PLAN_PATH)
    manifest = read_json(INPUT_MANIFEST_PATH)
    if lock.get("schema_id") != "issue73-checkpoint-replay-preseed-source-lock-v7" or lock.get("lock_revision") != 7:
        raise QualificationV7Failure("only the prospective v7 source lock is valid on this path")
    if lock.get("status") != "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION":
        raise QualificationV7Failure("prospective v7 source lock has an invalid status")
    if lock.get("code_source_commit") != manifest.get("code_source_commit"):
        raise QualificationV7Failure("v7 lock and input manifest name different source commits")
    if sha256_file(CONFIG_PATH) != lock.get("qualification_config_sha256"):
        raise QualificationV7Failure("v7 qualification config changed after source lock")
    if sha256_file(PLAN_PATH) != lock.get("gate_c_plan_sha256"):
        raise QualificationV7Failure("v7 Gate C roots/actions/streams plan changed after source lock")
    if sha256_file(INPUT_MANIFEST_PATH) != lock.get("historical_input_manifest_sha256"):
        raise QualificationV7Failure("v7 bounded historical input manifest changed after source lock")
    if config.get("historical_input_manifest_sha256") != lock.get("historical_input_manifest_sha256"):
        raise QualificationV7Failure("v7 config and source lock disagree on historical input manifest")
    if plan.get("qualification_config_sha256") != lock.get("qualification_config_sha256"):
        raise QualificationV7Failure("v7 Gate C plan is for another locked config")
    if lock.get("native", {}).get("module_path") != config.get("configured_native_module_path"):
        raise QualificationV7Failure("v7 config and source lock disagree on native module path")
    if lock.get("stsr_python_package_root") != q.STSRL_PYTHON_PACKAGE_ROOT:
        raise QualificationV7Failure("v7 lock does not name the pinned STSRL Python package root")

    source_commit = str(lock["code_source_commit"])
    baseline_paths = git_tree_paths(HISTORICAL_INPUT_BASE_COMMIT)
    current_paths = git_tree_paths(source_commit)
    included_paths = exact_membership(baseline_paths, current_paths)
    expected_manifest_paths = [str(row["path"]) for row in manifest.get("included", [])]
    if expected_manifest_paths != included_paths:
        raise QualificationV7Failure("v7 manifest membership differs from the exact registered historical Git-tree set")
    if set(lock.get("historical_input_sha256", {})) != set(included_paths):
        raise QualificationV7Failure("v7 historical source hash map differs from the explicit manifest membership")
    # Reject source and optimizer mutations before reopening every historical Git blob.
    for name, expected in lock.get("study_source_sha256", {}).items():
        path = STUDY / name
        if not path.is_file():
            raise QualificationV7Failure(f"v7 locked study source is missing: {name}")
        blob = q.external_git_blob(REPOSITORY, source_commit, f"{STUDY_RELATIVE}/{name}")
        q.verify_locked_source_payload(
            label=f"study/{name}",
            expected_sha256=str(expected),
            committed_blob=blob,
            checkout_bytes=path.read_bytes(),
        )
    ensure_inputs_present(STUDY, included_paths)
    for repository_path in included_paths:
        expected = str(lock["historical_input_sha256"][repository_path])
        entry = next(row for row in manifest["included"] if row.get("path") == repository_path)
        if entry.get("git_blob_sha256") != expected:
            raise QualificationV7Failure(f"v7 manifest hash map differs for {repository_path}")
        source_file_entry(source_commit, repository_path)
    fixed_roots = [row.get("root_id") for row in plan.get("roots", [])]
    if (
        fixed_roots != list(q.GATE_C_ROOTS)
        or plan.get("planned_suffix_attempt_count") != 64
        or len(plan.get("policy_rng_seed_streams", [])) != 64
        or len(config.get("gate_a_root_ids", [])) != 24
        or len(config.get("gate_b_witness_root_ids", {})) != 3
    ):
        raise QualificationV7Failure("v7 Gate A/B/C roots, actions, or stream population changed")
    q.configure_stsr_python_root()
    q.verify_locked_external_sources(lock)

    command, env = runner.git_invocation(REPOSITORY)
    if subprocess.run(
        [*command, "merge-base", "--is-ancestor", source_commit, "HEAD"],
        check=False,
        env=env,
    ).returncode:
        raise QualificationV7Failure("current source worktree does not descend from the locked v7 code commit")
    if require_remote_current:
        remote = q.git("ls-remote", "origin", f"refs/heads/{q.BRANCH}").split()
        if not remote or remote[0] != q.git("rev-parse", "HEAD"):
            raise QualificationV7Failure("v7 evidence worktree is not at the exact current pushed issue-branch head")
    return lock, config


def capture_preflight_v7(
    *,
    preflight_report_dir: Path,
    native_module_path_override: str | None = None,
) -> dict[str, Any]:
    try:
        out = preflight_report_dir.resolve()
        out.relative_to(V7_OUT.resolve())
    except ValueError as exc:
        raise QualificationV7Failure("v7 capture preflight output must remain inside the v7 receipt directory") from exc
    report_path = out / "capture-launch-preflight-v7.json"
    failure_path = out / "capture-launch-preflight-v7-failure.json"
    if report_path.exists() or failure_path.exists():
        raise QualificationV7Failure("this immutable v7 preflight case already has a receipt; no rerun or overwrite")
    reservation, _ledger = ensure_static_precharge_v7()
    q.install_process_cpu_ceiling(STATIC_RESERVATION_CPU_SECONDS)
    started_cpu = q.process_tree_cpu()
    started_wall = time.monotonic()
    try:
        locked_module = read_json(LOCK_PATH)["native"]["module_path"]
        if native_module_path_override is not None:
            try:
                override_path = Path(native_module_path_override).expanduser().resolve(strict=True)
                locked_path = Path(locked_module).expanduser().resolve(strict=True)
            except OSError as exc:
                raise QualificationV7Failure(f"native module path override is unavailable: {exc}") from exc
            if override_path != locked_path:
                raise QualificationV7Failure("native module path override differs from the exact prospective v7 lock")
        lock, _config = verify_lock_v7()
        prepared = q.prepare_capture_prerequisites(
            out=out,
            source_lock=lock,
            native_module_path_override=native_module_path_override,
        )
        module_path = Path(prepared["module"].__file__).resolve(strict=True)
        native_hash = runner.validate_native_module_identity(
            module_path,
            expected_name=q.OLD_CONFIG["identity"]["native_module"],
            expected_sha256=q.OLD_CONFIG["identity"]["native_binary_sha256"],
        )
        if str(module_path) != lock["native"]["module_path"] or native_hash != lock["native"]["loaded_module_sha256"]:
            raise QualificationV7Failure("production entry path did not load the exact v7 locked native module")
    except BaseException as exc:
        failure = {
            "schema_id": "issue73-capture-launch-preflight-v7-failure-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1600],
            "command_argv": list(sys.argv),
            "python_executable": sys.executable,
            "cwd": os.getcwd(),
            "fixed_static_reservation_cpu_seconds": reservation["reserved_cpu_seconds"],
            "additional_reservation_cpu_seconds": 0.0,
            "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - started_cpu),
            "measured_wall_seconds": max(0.0, time.monotonic() - started_wall),
            "start_attempt_invoked": False,
            "native_adapter_instantiated": False,
            "native_game_step_started": False,
            "formal_seed_consumed": False,
            "native_debit_created": False,
            "no_retry_or_overwrite": True,
        }
        runner.atomic_json(failure_path, failure)
        raise

    result = {
        "schema_id": "issue73-capture-launch-preflight-v7-v1",
        "status": "PASS",
        "source_lock_sha256": sha256_file(LOCK_PATH),
        "code_source_commit": lock["code_source_commit"],
        "remote_branch": lock["remote_branch"],
        "command_argv": list(sys.argv),
        "python_executable": sys.executable,
        "python": sys.version.split()[0],
        "cwd": os.getcwd(),
        "pythonpath_entries": [entry for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry],
        "sys_path": list(sys.path),
        "native_module_path": str(Path(prepared["module"].__file__).resolve()),
        "native_module_sha256": native_hash,
        "native_module_search": prepared["environment"]["identity"]["native_module_search"],
        "external_git_sources": prepared["external_sources"],
        "optimizer_id": prepared["runtime"].get("optimizer_id"),
        "optimizer_source_sha256": prepared["optimizer_source_sha256"],
        "optimizer_expected_sha256": q.OLD_CONFIG["runtime_optimizer"]["sha256"],
        "adapter_class": {
            "module": prepared["adapter_class"].__module__,
            "qualname": prepared["adapter_class"].__qualname__,
        },
        "loaded_policy_sha256": prepared["model_digest"],
        "runtime_installation": prepared["runtime"],
        "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - started_cpu),
        "capture_entry_path": {
            "shared_preparation_function": "prepare_capture_prerequisites",
            "production_capture_uses_same_function_in_same_process": True,
            "preparation_completed_in_this_capture_process": True,
            "start_attempt_invoked": False,
            "native_adapter_instantiated": False,
            "native_game_step_started": False,
            "debug_owner_started": False,
            "formal_seed_consumed": False,
            "native_debit_created": False,
        },
    }
    runner.atomic_json(report_path, result)
    return result


def push_v7(message: str) -> str:
    q.git("add", "--", V7_RELATIVE)
    staged = [
        row for row in q.git("diff", "--cached", "--name-only").splitlines() if row
    ]
    if any(row != V7_RELATIVE and not row.startswith(f"{V7_RELATIVE}/") for row in staged):
        raise QualificationV7Failure("refusing to commit evidence outside the v7 receipt directory")
    command, env = runner.git_invocation(REPOSITORY)
    changed = subprocess.run(
        [*command, "diff", "--cached", "--quiet"],
        check=False,
        env=env,
    ).returncode != 0
    if changed:        q.git("commit", "-m", message)
    commit = q.git("rev-parse", "HEAD")
    q.git("push", "origin", f"HEAD:refs/heads/{q.BRANCH}")
    remote = q.git("ls-remote", "origin", f"refs/heads/{q.BRANCH}").split()
    if not remote or remote[0] != commit:
        raise QualificationV7Failure("GitHub does not contain the exact v7 evidence commit")
    return commit


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("reserve-static", "prepare-lock", "verify-lock", "capture"))
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--native-module-path")
    parser.add_argument("--preflight-report-dir")
    args = parser.parse_args(argv)
    if args.command == "reserve-static":
        result = reserve_static_v7()
    elif args.command == "prepare-lock":
        lock = prepare_lock_v7()
        result = {
            "status": lock["status"],
            "source_commit": lock["code_source_commit"],
            "lock_revision": lock["lock_revision"],
            "historical_input_count": len(lock["historical_input_sha256"]),
            "historical_input_manifest_sha256": lock["historical_input_manifest_sha256"],
            "lock_preparation_process_cpu_seconds": lock["lock_preparation_process_cpu_seconds"],
        }
    elif args.command == "verify-lock":
        lock, _config = verify_lock_v7()
        result = {
            "status": lock["status"],
            "source_commit": lock["code_source_commit"],
            "lock_revision": lock["lock_revision"],
            "historical_input_count": len(lock["historical_input_sha256"]),
            "historical_input_manifest_sha256": lock["historical_input_manifest_sha256"],
            "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU),
        }
    else:
        if not args.preflight_only:
            parser.error("prospective v7 capture is restricted to --preflight-only until independent review passes")
        result = capture_preflight_v7(
            preflight_report_dir=Path(args.preflight_report_dir) if args.preflight_report_dir else PREFLIGHT_TEST_ROOT / "production-default",
            native_module_path_override=args.native_module_path,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
