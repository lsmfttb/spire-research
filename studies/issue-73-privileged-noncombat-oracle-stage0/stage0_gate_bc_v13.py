"""Resume Issue 73 Stage-0 Gate B/C from the reviewed v9 action-history lineage.

The module is study-local. It consumes the already captured seed-173730001
history, never reruns a formal owner, and locks the complete Gate-C population
before native Gate-B/C execution. V13 preserves the V12 prelaunch receipt and
repairs its public reward-menu parser without changing the registered study.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import unittest
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import checkpoint_replay_qualification as q
import runner
import stage0
import source_qualification_v9_engineering_5 as e5
import source_qualification_v9_engineering_6 as e6
import stage0_gate_bc_v10 as e10
import stage0_gate_bc_v11 as e11
import stage0_gate_bc_v12 as e12

STUDY = Path(__file__).resolve().parent
REPOSITORY = STUDY.parents[1]
STUDY_RELATIVE = STUDY.relative_to(REPOSITORY).as_posix()
BRANCH = "codex/issue-73-root-config-provenance-v2"
OUT = STUDY / "receipts" / "stage0-gate-bc-v13"
OUT_RELATIVE = OUT.relative_to(REPOSITORY).as_posix()
LOCK_PATH = OUT / "preseed-source-lock-v13.json"
CONFIG_PATH = OUT / "qualification-config-v13.json"
LEDGER_PATH = OUT / "resource-ledger-v13.json"
STATIC_KEY = "builder-prelock-static-verification-stage0-gate-bc-v13"
STATIC_RESERVE = 60.0
GATE_B_KEY = "gate-b-live-public-privacy-v13"
GATE_B_RESERVE = 100.0
GATE_C_SETUP_RESERVE = 20.0
GATE_C_SETUP_RECOVERY_RESERVE = 20.0
GATE_C_SUFFIX_RESERVE = 20.0
BUILDER_BASELINE = 4991.610134
NATIVE_BASELINE = 1680.0
BUILDER_CAP = 10000.0
AGGREGATE_CAP = 12000.0
NATIVE_CAP = 6000.0
REVIEWER_RESERVE = 2000.0
OWNER_SEED = 173730001
GATE_B_ROOTS = {
    "hidden_semantic": "root-173730001-0003",
    "native_rng_consequence": "root-173730001-0028",
    "post_two_third_family": "root-173730001-0002",
}
GATE_B_EXPECTED_PUBLIC_MENU_ACTION_KINDS = ("reward_relic", "reward_key", "skip")
PUBLIC_REWARD_MENU_ACTION_KINDS = frozenset(GATE_B_EXPECTED_PUBLIC_MENU_ACTION_KINDS)
GATE_C_ROOTS = (
    "root-173730001-0000",
    "root-173730001-0001",
    "root-173730001-0002",
    "root-173730001-0003",
)
GATE_C_PLAN_PATH = e5.PLAN_PATH
GATE_A_DIR = STUDY / "receipts" / "gate-a-requalification-v6"
GATE_A_REPORT_PATH = GATE_A_DIR / "outputs" / "gate-a-report.json"
GATE_A_CORPUS_PATH = GATE_A_DIR / "outputs" / "gate-a-corpus.json"
GATE_A_ATTEMPT_PATH = GATE_A_DIR / "attempts" / "gate-a-requalification-after-v9-parity-v6.json"
GATE_A_LEDGER_PATH = GATE_A_DIR / "resource-ledger-final.json"
GATE_A_EXECUTION_CLAIM_PATH = GATE_A_DIR / "attempts" / "execution-claim.json"

SOURCE_PATHS = (
    "checkpoint_replay_qualification.py",
    "config.json",
    "config.lock.sha256",
    "gate-a-target-direction.json",
    "gate_a.py",
    "policy.py",
    "public_history_execution.py",
    "runner.py",
    "source_lock_integrity.py",
    "stage0.py",
    "source_qualification_v9_engineering_5.py",
    "source_qualification_v9_engineering_6.py",
    "stage0_gate_bc_v10.py",
    "stage0_gate_bc_v11.py",
    "test_stage0_gate_bc_v10.py",
    "stage0_gate_bc_v13.py",
    "test_stage0_gate_bc_v13.py",
)

PARENT_PATHS = {
    "v10_source_lock": e10.LOCK_PATH,
    "v10_qualification_config": e10.CONFIG_PATH,
    "v10_static_report": e10.OUT / "reports" / "static-qualification-report-v10.json",
    "v10_resource_ledger": e10.LEDGER_PATH,
    "v9_engineering_5_source_lock": e5.LOCK_PATH,
    "v9_engineering_5_static_report": e5.STATIC_REPORT_PATH,
    "v9_engineering_5_capture_report": e5.CAPTURE_REPORT_PATH,
    "v9_engineering_5_capture_attempt": e5.ATTEMPT_PATH,
    "v9_engineering_5_capture_history": e5.CAPTURE_HISTORY_PATH,
    "v9_engineering_5_capture_roots": e5.CAPTURE_ROOTS_PATH,
    "v9_engineering_5_gate_c_plan": e5.PLAN_PATH,
    "v9_engineering_5_resource_ledger": e5.LEDGER_PATH,
    "v9_engineering_6_source_lock": e6.LOCK_PATH,
    "v9_engineering_6_static_report": e6.STATIC_REPORT_PATH,
    "v9_engineering_6_static_attempt": e6.STATIC_PATH,
    "v9_engineering_6_parity_report": e6.PARITY_REPORT_PATH,
    "v9_engineering_6_parity_attempt": e6.PARITY_ATTEMPT_PATH,
    "v9_engineering_6_resource_ledger": e6.LEDGER_PATH,
    "gate_a_report": GATE_A_REPORT_PATH,
    "gate_a_corpus": GATE_A_CORPUS_PATH,
    "gate_a_attempt": GATE_A_ATTEMPT_PATH,
    "gate_a_resource_ledger": GATE_A_LEDGER_PATH,
    "gate_a_execution_claim": GATE_A_EXECUTION_CLAIM_PATH,
    "v11_source_lock": e11.LOCK_PATH,
    "v11_qualification_config": e11.CONFIG_PATH,
    "v11_static_report": e11.OUT / "reports" / "static-qualification-report-v11.json",
    "v11_static_attempt": e11.OUT / "attempts" / f"{e11.STATIC_KEY}.json",
    "v11_gate_b_runtime_environment": e11.OUT / "gate-b-runtime" / "environment.json",
    "v11_gate_b_runtime_config_lock": e11.OUT / "gate-b-runtime" / "config-lock.json",
    "v11_resource_ledger": e11.LEDGER_PATH,
    "v11_gate_b_report": e11.OUT / "reports" / "gate-b-live-public-privacy-report-v11.json",
    "v11_gate_b_attempt": e11.OUT / "attempts" / f"{e11.GATE_B_KEY}.json",
    "v12_source_lock": e12.LOCK_PATH,
    "v12_qualification_config": e12.CONFIG_PATH,
    "v12_static_report": e12.OUT / "reports" / "static-qualification-report-v12.json",
    "v12_static_attempt": e12.OUT / "attempts" / f"{e12.STATIC_KEY}.json",
    "v12_resource_ledger": e12.LEDGER_PATH,
}


class GateBCV13Failure(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return runner.sha256_text_file(path)


def canonical(value: Any) -> str:
    return runner.canonical(value)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def git(*args: str) -> str:
    return q.git(*args)


def _git_commit_is_ancestor(ancestor: str, descendant: str) -> bool:
    command, env = runner.git_invocation(REPOSITORY)
    result = subprocess.run(
        [*command, "merge-base", "--is-ancestor", ancestor, descendant],
        check=False, env=env,
    )
    return result.returncode == 0


def _current_head(*, require_remote_equal: bool) -> str:
    if git("branch", "--show-current") != BRANCH:
        raise GateBCV13Failure(f"expected active study branch {BRANCH}")
    head = git("rev-parse", "HEAD")
    if require_remote_equal:
        remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
        if not remote or remote[0] != head:
            raise GateBCV13Failure("remote branch is not at the exact source/evidence head")
    return head


def _source_hashes(source_commit: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in SOURCE_PATHS:
        relative = f"{STUDY_RELATIVE}/{name}"
        blob = q.git_object_bytes(f"{source_commit}:{relative}")
        digest = sha256_bytes(blob)
        path = STUDY / name
        if not path.is_file():
            raise GateBCV13Failure(f"locked study source is missing: {name}")
        q.verify_locked_source_payload(
            label=f"study/{name}", expected_sha256=digest,
            committed_blob=blob, checkout_bytes=path.read_bytes(),
        )
        result[name] = digest
    return result


def _verify_committed_source_hashes(
    source_commit: str, expected_hashes: Mapping[str, Any], *, label: str,
) -> dict[str, str]:
    """Verify historical locks against their Git tree, not today's evolved checkout."""
    if not isinstance(expected_hashes, Mapping) or not expected_hashes:
        raise GateBCV13Failure(f"{label} source lock has no committed source-hash map")
    actual: dict[str, str] = {}
    for name, expected in expected_hashes.items():
        relative = Path(str(name))
        if relative.is_absolute() or ".." in relative.parts:
            raise GateBCV13Failure(f"{label} source lock contains an unsafe path: {name}")
        blob = q.git_object_bytes(f"{source_commit}:{STUDY_RELATIVE}/{relative.as_posix()}")
        actual[str(name)] = sha256_bytes(blob)
        if actual[str(name)] != str(expected):
            raise GateBCV13Failure(f"{label} historical Git blob differs from its source lock: {name}")
    return actual


def _parent_input_hashes() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in PARENT_PATHS.items():
        if not path.is_file():
            raise GateBCV13Failure(f"required immutable parent evidence is missing: {name}")
        result[name] = sha256_file(path)
    return result


def _validate_parent_evidence() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    e5_lock = read_json(e5.LOCK_PATH)
    if (
        e5_lock.get("schema_id") != "issue73-checkpoint-replay-preseed-source-lock-v9"
        or e5_lock.get("status") != "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION"
        or e5_lock.get("stage1_authorized") is not False
        or e5_lock.get("code_source_commit") != "b9ecb23883aa3b10c933cfc26535670d71e5355e"
    ):
        raise GateBCV13Failure("v9 engineering-5 parent source lock is not the reviewed capture lineage")
    _verify_committed_source_hashes(
        str(e5_lock["code_source_commit"]),
        e5_lock.get("study_source_sha256", {}),
        label="v9 engineering-5",
    )
    _parent_lock, capture_report, capture_attempt, history_record, roots_record = e6.validate_parent_capture_v9()
    e6_lock = read_json(e6.LOCK_PATH)
    e6_source_commit = str(e6_lock.get("code_source_commit", ""))
    if (
        e6_lock.get("schema_id") != "issue73-v9-engineering-6-preseed-source-lock-v1"
        or e6_lock.get("status") != "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION"
        or e6_lock.get("remote_branch") != "codex/issue-73-v9-pre-capture"
        or e6_lock.get("stage1_authorized") is not False
        or not e6_source_commit
        or not _git_commit_is_ancestor(e6_source_commit, git("rev-parse", "HEAD"))
        or e6_lock.get("study_source_sha256") != e6._new_source_hashes(e6_source_commit)
    ):
        raise GateBCV13Failure("v9 engineering-6 source lock is not an unchanged ancestor of this active lineage")
    parity = read_json(e6.PARITY_REPORT_PATH)
    parity_attempt = read_json(e6.PARITY_ATTEMPT_PATH)
    if (
        parity.get("status") != "PASS"
        or parity.get("source_lock_sha256") != sha256_file(e6.LOCK_PATH)
        or parity.get("code_source_commit") != e6_source_commit
        or parity.get("required_transition_count") != 9
        or parity.get("replayed_transition_count") != 9
        or len(parity.get("witnesses", {})) != 9
        or any(row.get("status") != "PASS" for row in parity.get("witnesses", {}).values())
        or parity.get("formal_seed_consumed") is not False
        or parity_attempt.get("status") != "PASS"
        or parity_attempt.get("report_status") != "PASS"
        or parity_attempt.get("source_lock_sha256") != sha256_file(e6.LOCK_PATH)
    ):
        raise GateBCV13Failure("v9 engineering-6 fresh-process parity receipt is not a complete PASS")
    gate_a = read_json(GATE_A_REPORT_PATH)
    gate_a_attempt = read_json(GATE_A_ATTEMPT_PATH)
    if (
        gate_a.get("status") != "PASS"
        or sha256_file(GATE_A_REPORT_PATH) != "4eec38d71e4aec331a033ede3a215fb01754f30383557ddaaaf351332ec4eca2"
        or sha256_file(GATE_A_CORPUS_PATH) != "fb7b31b53dd1a078cce27f9f65cbc07bfb7db8273f9ae5d75d4bae37eb58e05e"
        or gate_a_attempt.get("status") != "COMPLETE"
        or gate_a_attempt.get("gate_status") != "PASS"
        or gate_a_attempt.get("stage1_authorized") is not False
    ):
        raise GateBCV13Failure("Gate A parent is not the independently reviewed synthetic PASS")
    v10_lock, _v10_config = e10.verify_lock()
    v10_static = read_json(e10.OUT / "reports" / "static-qualification-report-v10.json")
    v10_ledger = e10.ledger()
    v10_builder, v10_native = e10.charged(v10_ledger)
    v10_static_attempts = list(v10_ledger.get("attempts", []))
    if (
        v10_lock.get("schema_id") != "issue73-stage0-gate-bc-v10-preseed-source-lock-v1"
        or v10_static.get("status") != "PASS"
        or v10_static.get("source_lock_sha256") != sha256_file(e10.LOCK_PATH)
        or v10_builder != 4771.610134
        or v10_native != 1580.0
        or len(v10_static_attempts) != 1
        or v10_static_attempts[0].get("key") != e10.STATIC_KEY
        or v10_static_attempts[0].get("status") != "RESERVED_CONSERVATIVE_STATIC_DEBIT"
        or v10_static_attempts[0].get("cpu_charged_seconds") != e10.STATIC_RESERVE
        or v10_static_attempts[0].get("native_gate_bc") is not False
        or any(path.exists() for path in (
            e10.OUT / "reports" / "gate-b-live-public-privacy-report-v10.json",
            e10.OUT / "attempts" / f"{e10.GATE_B_KEY}.json",
        ))
    ):
        raise GateBCV13Failure("v10 parent is not an unused static PASS with its exact immutable baseline debit")
    v11_lock, _v11_config = e11.verify_lock()
    v11_static = read_json(e11.OUT / "reports" / "static-qualification-report-v11.json")
    v11_report = read_json(e11.OUT / "reports" / "gate-b-live-public-privacy-report-v11.json")
    v11_attempt = read_json(e11.OUT / "attempts" / f"{e11.GATE_B_KEY}.json")
    v11_builder, v11_native = e11.charged(e11.ledger())
    v11_chest = v11_report.get("witnesses", {}).get("native_rng_consequence", {})
    if (
        v11_lock.get("schema_id") != "issue73-stage0-gate-bc-v11-preseed-source-lock-v1"
        or v11_static.get("status") != "PASS"
        or v11_static.get("source_lock_sha256") != sha256_file(e11.LOCK_PATH)
        or v11_report.get("status") != "FAIL_OR_INCOMPLETE"
        or v11_report.get("source_lock_sha256") != sha256_file(e11.LOCK_PATH)
        or v11_chest.get("status") != "FAIL_CHEST_REWARD_NOT_PUBLICLY_REVEALED"
        or v11_chest.get("revealed_public_reward_choices") != []
        or v11_attempt.get("key") != e11.GATE_B_KEY
        or v11_attempt.get("status") != "FAIL_OR_INCOMPLETE"
        or v11_attempt.get("no_retry") is not True
        or v11_attempt.get("formal_seed_consumed") is not False
        or v11_builder != 4931.610134
        or v11_native != 1680.0
    ):
        raise GateBCV13Failure("v11 is not the exact immutable engineering-failure parent or cumulative debit")
    v12_lock = read_json(e12.LOCK_PATH)
    v12_config = read_json(e12.CONFIG_PATH)
    v12_static = read_json(e12.OUT / "reports" / "static-qualification-report-v12.json")
    v12_static_attempt = read_json(e12.OUT / "attempts" / f"{e12.STATIC_KEY}.json")
    v12_ledger = read_json(e12.LEDGER_PATH)
    v12_builder, v12_native = e12.charged(v12_ledger)
    if (
        v12_lock.get("schema_id") != "issue73-stage0-gate-bc-v12-preseed-source-lock-v1"
        or v12_lock.get("status") != "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION"
        or v12_lock.get("remote_branch") != e12.BRANCH
        or v12_lock.get("stage1_authorized") is not False
        or v12_lock.get("formal_owner_or_training_seed_consumed") is not False
        or v12_lock.get("study_source_sha256") != e12._source_hashes(str(v12_lock.get("code_source_commit", "")))
        or v12_lock.get("qualification_config_sha256") != sha256_file(e12.CONFIG_PATH)
        or v12_lock.get("parent_input_sha256") != e12._parent_input_hashes()
        or v12_config.get("code_source_commit") != v12_lock.get("code_source_commit")
        or v12_static.get("status") != "PASS"
        or v12_static.get("source_lock_sha256") != sha256_file(e12.LOCK_PATH)
        or v12_static.get("native_adapter_instantiated") is not False
        or v12_static.get("native_game_step_started") is not False
        or v12_static.get("formal_seed_consumed") is not False
        or v12_static_attempt.get("key") != e12.STATIC_KEY
        or v12_static_attempt.get("status") != "RESERVED_CONSERVATIVE_STATIC_DEBIT"
        or v12_static_attempt.get("cpu_charged_seconds") != e12.STATIC_RESERVE
        or v12_static_attempt.get("native_gate_bc") is not False
        or len(v12_ledger.get("attempts", [])) != 1
        or v12_builder != 4991.610134
        or v12_native != 1680.0
        or (e12.OUT / "reports" / "gate-b-live-public-privacy-report-v12.json").exists()
        or (e12.OUT / "attempts" / f"{e12.GATE_B_KEY}.json").exists()
    ):
        raise GateBCV13Failure("v12 is not the exact immutable static-only parent or cumulative debit")
    if GATE_B_ROOTS != q.GATE_B_ROOTS:
        raise GateBCV13Failure("Gate B witness root ids differ from the registered source-qualification plan")
    plan = read_json(e5.PLAN_PATH)
    streams = plan.get("policy_rng_seed_streams", [])
    if plan.get("status") != "PRECOMMITTED_BEFORE_ANY_NEW_SUFFIX_RESULT" or len(streams) != 64:
        raise GateBCV13Failure("Gate C must use the original precommitted 64-stream plan")
    expected = Counter(
        (str(root["root_id"]), int(action), replicate)
        for root in plan.get("roots", [])
        for action in root.get("action_indices", [])
        for replicate in range(8)
    )
    actual = Counter(
        (str(row.get("root_id")), int(row.get("candidate_index", -1)), int(row.get("replicate", -1)))
        for row in streams
    )
    if (
        expected != actual
        or set(str(row.get("root_id")) for row in plan.get("roots", [])) != set(GATE_C_ROOTS)
        or len({int(row["suffix_policy_seed"]) for row in streams}) != 64
        or any(int(row.get("owner_seed", -1)) != OWNER_SEED for row in streams)
    ):
        raise GateBCV13Failure("Gate C population, actions, owner seed, or policy RNG streams differ from registration")
    if capture_report.get("formal_seed_consumed") is not False or history_record.get("event_count") != 111 or len(roots_record.get("roots", [])) != 39:
        raise GateBCV13Failure("v9 capture parent does not retain the exact non-formal 111-event / 39-root record")
    return e5_lock, e6_lock, plan


def resource_projection(ledger_value: Mapping[str, Any] | None = None) -> dict[str, float | bool]:
    current = dict(ledger_value) if ledger_value is not None else {
        "attempts": [{"key": STATIC_KEY, "cpu_charged_seconds": STATIC_RESERVE, "native_gate_bc": False}]
    }
    attempts = list(current.get("attempts", []))
    builder_charged = BUILDER_BASELINE + sum(float(row.get("cpu_charged_seconds", 0.0)) for row in attempts)
    native_charged = NATIVE_BASELINE + sum(
        float(row.get("cpu_charged_seconds", 0.0)) for row in attempts
        if row.get("native_gate_bc") is True
    )
    gate_b_attempt = next((row for row in attempts if row.get("key") == GATE_B_KEY), None)
    gate_b_reserve = 0.0 if gate_b_attempt else GATE_B_RESERVE
    setup_rows = [row for row in attempts if str(row.get("key", "")).startswith("gate-c-environment-and-policy-setup-")]
    setup_reserve = max(0, 2 - len(setup_rows)) * GATE_C_SETUP_RESERVE
    suffix_attempts = {str(row.get("key")): row for row in attempts if str(row.get("key", "")).startswith("gate-c-root-")}
    plan = read_json(e5.PLAN_PATH)
    suffix_reserve = sum(
        GATE_C_SUFFIX_RESERVE
        for stream in plan["policy_rng_seed_streams"]
        if f"gate-c-{stream['root_id']}-a{int(stream['candidate_index'])}-r{int(stream['replicate']):02d}" not in suffix_attempts
    )
    remaining_builder_reserve = gate_b_reserve + setup_reserve + suffix_reserve
    remaining_native_reserve = remaining_builder_reserve
    projected_builder = builder_charged + remaining_builder_reserve
    projected_native = native_charged + remaining_native_reserve
    projected_aggregate = projected_builder + REVIEWER_RESERVE
    return {
        "charged_builder_cpu_seconds": builder_charged,
        "projected_builder_cpu_seconds": projected_builder,
        "builder_ceiling_cpu_seconds": BUILDER_CAP,
        "charged_native_gate_bc_cpu_seconds": native_charged,
        "projected_native_gate_bc_cpu_seconds": projected_native,
        "native_gate_bc_ceiling_cpu_seconds": NATIVE_CAP,
        "reviewer_reserve_cpu_seconds": REVIEWER_RESERVE,
        "projected_aggregate_cpu_seconds_including_reviewer_reserve": projected_aggregate,
        "aggregate_ceiling_cpu_seconds": AGGREGATE_CAP,
        "forecast_fits_registered_caps": projected_builder <= BUILDER_CAP and projected_native <= NATIVE_CAP and projected_aggregate <= AGGREGATE_CAP,
    }


def _config() -> dict[str, Any]:
    plan = read_json(e5.PLAN_PATH)
    return {
        "schema_id": "issue73-stage0-gate-bc-v13-qualification-config-v1",
        "study_id": "issue-73-privileged-noncombat-oracle-stage0",
        "stage1_authorized": False,
        "formal_owner_or_training_seed_consumed": False,
        "historical_debug_owner_seed": OWNER_SEED,
        "gate_b_witness_root_ids": GATE_B_ROOTS,
        "gate_b_expected_public_menu_action_kinds": list(GATE_B_EXPECTED_PUBLIC_MENU_ACTION_KINDS),
        "gate_c_root_ids": list(GATE_C_ROOTS),
        "gate_c_plan_path": e5.PLAN_PATH.relative_to(STUDY).as_posix(),
        "gate_c_plan_sha256": sha256_file(e5.PLAN_PATH),
        "gate_c_suffix_attempt_count": len(plan["policy_rng_seed_streams"]),
        "gate_c_suffix_cpu_cap_seconds": GATE_C_SUFFIX_RESERVE,
        "gate_b_live_privacy_cpu_reserve_seconds": GATE_B_RESERVE,
        "gate_c_setup_cpu_reserve_seconds": GATE_C_SETUP_RESERVE,
        "gate_c_setup_recovery_reserve_seconds": GATE_C_SETUP_RECOVERY_RESERVE,
        "resource_projection": resource_projection(),
        "disposition": "STUDY_ONLY",
    }


def _required_environment() -> None:
    q.verify_locked_external_sources(read_json(e5.LOCK_PATH))
    native = read_json(e5.LOCK_PATH)["native"]
    module_path = Path(str(native["module_path"]))
    if not module_path.is_file() or sha256_bytes(module_path.read_bytes()) != native.get("loaded_module_sha256"):
        raise GateBCV13Failure("pinned native module path/hash differs from the reviewed v9 lock")


def prepare_lock() -> dict[str, Any]:
    if OUT.exists():
        raise GateBCV13Failure("v13 output already exists; never reconstruct or overwrite a source lock")
    _current_head(require_remote_equal=True)
    e5_lock, e6_lock, plan = _validate_parent_evidence()
    _required_environment()
    head = git("rev-parse", "HEAD")
    config = _config()
    if not config["resource_projection"]["forecast_fits_registered_caps"]:
        raise GateBCV13Failure("fixed Gate B/C work exceeds the current registered ceilings")
    OUT.mkdir(parents=True)
    (OUT / "attempts").mkdir()
    (OUT / "reports").mkdir()
    config["code_source_commit"] = head
    runner.atomic_json(CONFIG_PATH, config)
    lock = {
        "schema_id": "issue73-stage0-gate-bc-v13-preseed-source-lock-v1",
        "status": "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION",
        "study_id": config["study_id"],
        "study_only": True,
        "stage1_authorized": False,
        "formal_owner_or_training_seed_consumed": False,
        "remote_branch": BRANCH,
        "code_source_commit": head,
        "study_source_sha256": _source_hashes(head),
        "qualification_config_sha256": sha256_file(CONFIG_PATH),
        "parent_input_sha256": _parent_input_hashes(),
        "parent_capture_source_commit": e5_lock["code_source_commit"],
        "parent_parity_source_commit": e6_lock["code_source_commit"],
        "parent_gate_a_status": read_json(GATE_A_REPORT_PATH)["status"],
        "gate_b_witness_root_ids": GATE_B_ROOTS,
        "gate_b_expected_public_menu_action_kinds": list(GATE_B_EXPECTED_PUBLIC_MENU_ACTION_KINDS),
        "gate_c_root_ids": list(GATE_C_ROOTS),
        "gate_c_plan_path": e5.PLAN_PATH.relative_to(STUDY).as_posix(),
        "gate_c_plan_sha256": sha256_file(e5.PLAN_PATH),
        "gate_c_suffix_attempt_count": len(plan["policy_rng_seed_streams"]),
        "owner_seed": OWNER_SEED,
        "native_rng_resampled": False,
        "registered_caps_cpu_seconds": {
            "builder": BUILDER_CAP,
            "aggregate_builder_reviewer": AGGREGATE_CAP,
            "native_gate_b_c": NATIVE_CAP,
            "reviewer_protected_reserve": REVIEWER_RESERVE,
        },
        "baseline_charges_cpu_seconds": {
            "builder": BUILDER_BASELINE,
            "native_gate_b_c": NATIVE_BASELINE,
        "source": "V12 static PASS ledger plus all immutable parent charges",
        },
        "resource_projection": config["resource_projection"],
        "external_native_lock_sha256": sha256_file(e5.LOCK_PATH),
        "external_native_source_commit": e5_lock["native"]["commit"],
        "external_stsrl_source_commit": e5_lock["stsr"]["commit"],
        "native_module_path": e5_lock["native"]["module_path"],
        "native_module_sha256": e5_lock["native"]["loaded_module_sha256"],
        "no_population_action_endpoint_or_rng_changes": True,
        "no_stage1_or_training_authority": True,
    }
    runner.atomic_json(LOCK_PATH, lock)
    static = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": STATIC_KEY,
        "kind": "stage0_gate_bc_v13_bounded_static_source_lock_and_contract_verification",
        "status": "RESERVED_CONSERVATIVE_STATIC_DEBIT",
        "reserved_cpu_seconds": STATIC_RESERVE,
        "cpu_charged_seconds": STATIC_RESERVE,
        "native_gate_bc": False,
        "formal_seed_consumed": False,
        "no_native_adapter_or_step": True,
        "source_lock_sha256": sha256_file(LOCK_PATH),
    }
    runner.atomic_json(OUT / "attempts" / f"{STATIC_KEY}.json", static)
    runner.atomic_json(LEDGER_PATH, {
        "schema_id": "issue73-stage0-gate-bc-v13-resource-ledger-v1",
        "baseline_builder_charge_cpu_seconds": BUILDER_BASELINE,
        "baseline_native_gate_bc_charge_cpu_seconds": NATIVE_BASELINE,
        "builder_ceiling_cpu_seconds": BUILDER_CAP,
        "aggregate_ceiling_cpu_seconds": AGGREGATE_CAP,
        "native_gate_bc_ceiling_cpu_seconds": NATIVE_CAP,
        "reviewer_reserve_cpu_seconds": REVIEWER_RESERVE,
        "parent_input_sha256": lock["parent_input_sha256"],
        "attempts": [static],
    })
    push_evidence("Issue 73: prelock Stage-0 Gate B/C v13 source and fixed population")
    return lock


def ledger() -> dict[str, Any]:
    if not LEDGER_PATH.is_file():
        raise GateBCV13Failure("v13 cumulative ledger is missing")
    value = read_json(LEDGER_PATH)
    if value.get("schema_id") != "issue73-stage0-gate-bc-v13-resource-ledger-v1":
        raise GateBCV13Failure("v13 resource ledger schema changed")
    by_key = {str(row.get("key")): row for row in value.get("attempts", [])}
    changed = False
    attempt_dir = OUT / "attempts"
    if attempt_dir.is_dir():
        for path in sorted(attempt_dir.glob("*.json")):
            receipt = read_json(path)
            key = str(receipt.get("key", ""))
            if not key or path.stem != key or receipt.get("schema_id") != "issue73-checkpoint-replay-attempt-v1":
                continue
            prior = by_key.get(key)
            if prior is None:
                value["attempts"].append(receipt)
                by_key[key] = receipt
                changed = True
            elif prior.get("status") == "STARTED" and receipt.get("status") not in {"STARTED"}:
                value["attempts"] = [receipt if str(row.get("key")) == key else row for row in value["attempts"]]
                by_key[key] = receipt
                changed = True
            elif prior.get("status") != receipt.get("status"):
                raise GateBCV13Failure(f"ledger/receipt terminal disagreement for {key}; stop for audit")
    if changed:
        runner.atomic_json(LEDGER_PATH, value)
    return value


def charged(value: Mapping[str, Any] | None = None) -> tuple[float, float]:
    current = value or ledger()
    total = float(current["baseline_builder_charge_cpu_seconds"])
    native = float(current["baseline_native_gate_bc_charge_cpu_seconds"])
    for row in current.get("attempts", []):
        amount = float(row.get("cpu_charged_seconds", row.get("reserved_cpu_seconds", 0.0)))
        total += amount
        if row.get("native_gate_bc") is True:
            native += amount
    return total, native


def _planned_suffix_keys() -> list[str]:
    plan = read_json(e5.PLAN_PATH)
    return [
        f"gate-c-{row['root_id']}-a{int(row['candidate_index'])}-r{int(row['replicate']):02d}"
        for row in plan["policy_rng_seed_streams"]
    ]


def _future_reserve(value: Mapping[str, Any]) -> tuple[float, float]:
    attempts = {str(row.get("key")): row for row in value.get("attempts", [])}
    gate_b = 0.0 if GATE_B_KEY in attempts else GATE_B_RESERVE
    setup_rows = [key for key in attempts if key.startswith("gate-c-environment-and-policy-setup-")]
    setups = max(0, 2 - len(setup_rows)) * GATE_C_SETUP_RESERVE
    suffixes = sum(GATE_C_SUFFIX_RESERVE for key in _planned_suffix_keys() if key not in attempts)
    native = gate_b + setups + suffixes
    return native, native


def _check_resource_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    total, native = charged(value)
    future_builder, future_native = _future_reserve(value)
    projected_builder = total + future_builder
    projected_native = native + future_native
    projected_aggregate = projected_builder + REVIEWER_RESERVE
    result = {
        "charged_builder_cpu_seconds": total,
        "projected_builder_cpu_seconds": projected_builder,
        "builder_ceiling_cpu_seconds": BUILDER_CAP,
        "charged_native_gate_bc_cpu_seconds": native,
        "projected_native_gate_bc_cpu_seconds": projected_native,
        "native_gate_bc_ceiling_cpu_seconds": NATIVE_CAP,
        "reviewer_reserve_cpu_seconds": REVIEWER_RESERVE,
        "projected_aggregate_cpu_seconds_including_reviewer_reserve": projected_aggregate,
        "aggregate_ceiling_cpu_seconds": AGGREGATE_CAP,
        "forecast_fits_registered_caps": projected_builder <= BUILDER_CAP and projected_native <= NATIVE_CAP and projected_aggregate <= AGGREGATE_CAP,
    }
    return result


def verify_lock(*, require_remote_equal: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    if not all(path.is_file() for path in (LOCK_PATH, CONFIG_PATH, LEDGER_PATH)):
        raise GateBCV13Failure("v13 source lock/config/ledger is missing")
    lock = read_json(LOCK_PATH)
    config = read_json(CONFIG_PATH)
    if (
        lock.get("schema_id") != "issue73-stage0-gate-bc-v13-preseed-source-lock-v1"
        or lock.get("status") != "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION"
        or lock.get("stage1_authorized") is not False
        or lock.get("formal_owner_or_training_seed_consumed") is not False
        or lock.get("remote_branch") != BRANCH
        or lock.get("no_population_action_endpoint_or_rng_changes") is not True
        or lock.get("no_stage1_or_training_authority") is not True
        or lock.get("gate_b_expected_public_menu_action_kinds") != list(GATE_B_EXPECTED_PUBLIC_MENU_ACTION_KINDS)
        or config.get("gate_b_expected_public_menu_action_kinds") != list(GATE_B_EXPECTED_PUBLIC_MENU_ACTION_KINDS)
    ):
        raise GateBCV13Failure("v13 lock scope or Stage-1 prohibition changed")
    head = _current_head(require_remote_equal=require_remote_equal)
    if not _git_commit_is_ancestor(str(lock.get("code_source_commit", "")), head):
        raise GateBCV13Failure("current evidence head does not descend from the locked Gate B/C code")
    if lock.get("study_source_sha256") != _source_hashes(str(lock["code_source_commit"])):
        raise GateBCV13Failure("current Gate B/C source differs from its exact locked Git blobs")
    if lock.get("qualification_config_sha256") != sha256_file(CONFIG_PATH) or config.get("code_source_commit") != lock.get("code_source_commit"):
        raise GateBCV13Failure("qualification config differs from the preseed lock")
    if lock.get("parent_input_sha256") != _parent_input_hashes():
        raise GateBCV13Failure("a locked parent capture, parity, Gate A, or plan artifact changed")
    _validate_parent_evidence()
    _required_environment()
    current_ledger = ledger()
    if (
        current_ledger.get("parent_input_sha256") != lock.get("parent_input_sha256")
        or current_ledger.get("baseline_builder_charge_cpu_seconds") != BUILDER_BASELINE
        or current_ledger.get("baseline_native_gate_bc_charge_cpu_seconds") != NATIVE_BASELINE
    ):
        raise GateBCV13Failure("v13 resource baseline no longer matches the reviewed Gate A handoff")
    projection = _check_resource_projection(current_ledger)
    if not projection.get("forecast_fits_registered_caps"):
        raise GateBCV13Failure(f"fixed Stage-0 Gate B/C work exceeds the registered caps: {projection}")
    if lock.get("resource_projection") != config.get("resource_projection"):
        raise GateBCV13Failure("locked resource forecast differs from the registered contract")
    return lock, config


def push_evidence(message: str) -> str:
    git("add", "--", OUT_RELATIVE)
    staged = [item for item in git("diff", "--cached", "--name-only").splitlines() if item]
    if any(item != OUT_RELATIVE and not item.startswith(f"{OUT_RELATIVE}/") for item in staged):
        raise GateBCV13Failure("refusing to commit files outside the v13 evidence directory")
    command, env = runner.git_invocation(REPOSITORY)
    if subprocess.run([*command, "diff", "--cached", "--quiet"], check=False, env=env).returncode:
        git("commit", "-m", message)
    head = git("rev-parse", "HEAD")
    git("push", "origin", f"HEAD:refs/heads/{BRANCH}")
    remote = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    if not remote or remote[0] != head:
        raise GateBCV13Failure("GitHub does not contain the exact v13 evidence commit")
    return head


def start_attempt(key: str, *, kind: str, reserve: float, native_gate_bc: bool) -> dict[str, Any]:
    verify_lock()
    attempt_path = OUT / "attempts" / f"{key}.json"
    if attempt_path.exists():
        previous = read_json(attempt_path)
        if previous.get("status") == "STARTED":
            previous.update({
                "status": "INTERRUPTED_NO_RERUN",
                "cpu_charged_seconds": float(previous["reserved_cpu_seconds"]),
                "failure": "attempt has no terminal receipt; preserve its fixed debit and do not rerun",
            })
            current = ledger()
            current["attempts"] = [previous if row.get("key") == key else row for row in current["attempts"]]
            runner.atomic_json(attempt_path, previous)
            runner.atomic_json(LEDGER_PATH, current)
            push_evidence(f"Issue 73: preserve interrupted Gate B/C v13 attempt {key}")
            raise GateBCV13Failure(f"fixed attempt {key} was interrupted; no retry")
        raise GateBCV13Failure(f"fixed attempt {key} already has terminal status {previous.get('status')}")
    if key != GATE_B_KEY and key not in {"gate-c-environment-and-policy-setup-01", "gate-c-environment-and-policy-setup-02"} and key not in _planned_suffix_keys():
        raise GateBCV13Failure(f"attempt key is outside the fixed Gate B/C population: {key}")
    current = ledger()
    if not _check_resource_projection(current).get("forecast_fits_registered_caps"):
        raise GateBCV13Failure("registered cumulative CPU ceiling blocks this attempt")
    row = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": key,
        "kind": kind,
        "status": "STARTED",
        "native_gate_bc": bool(native_gate_bc),
        "reserved_cpu_seconds": float(reserve),
        "cpu_charged_seconds": float(reserve),
        "source_lock_sha256": sha256_file(LOCK_PATH),
        "code_source_commit": read_json(LOCK_PATH)["code_source_commit"],
        "formal_seed_consumed": False,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    current["attempts"].append(row)
    runner.atomic_json(attempt_path, row)
    runner.atomic_json(LEDGER_PATH, current)
    push_evidence(f"Issue 73: start fixed Stage-0 Gate B/C v13 attempt {key}")
    return row


def finish_attempt(key: str, *, started_cpu: float, status: str, details: Mapping[str, Any]) -> dict[str, Any]:
    path = OUT / "attempts" / f"{key}.json"
    previous = read_json(path)
    if previous.get("status") != "STARTED":
        raise GateBCV13Failure(f"attempt {key} is not in its immutable STARTED state")
    measured = max(0.0, q.process_tree_cpu() - started_cpu)
    reserve = float(previous["reserved_cpu_seconds"])
    succeeded = status in {"COMPLETE", "PASS", "READY"}
    exceeded = measured > reserve
    row = {
        **previous,
        **dict(details),
        "status": "FAIL_CPU_RESERVATION_EXCEEDED" if exceeded else status,
        "measured_process_cpu_seconds": measured,
        "cpu_charged_seconds": measured if succeeded and not exceeded else max(reserve, measured),
        "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    current = ledger()
    current["attempts"] = [row if old.get("key") == key else old for old in current["attempts"]]
    total, native = charged(current)
    if total > BUILDER_CAP or native > NATIVE_CAP or total + REVIEWER_RESERVE > AGGREGATE_CAP:
        row["status"] = "FAIL_CUMULATIVE_CPU_CEILING_EXCEEDED"
        current["attempts"] = [row if old.get("key") == key else old for old in current["attempts"]]
    runner.atomic_json(path, row)
    runner.atomic_json(LEDGER_PATH, current)
    push_evidence(f"Issue 73: finish fixed Stage-0 Gate B/C v13 attempt {key}")
    return row


def budget_summary() -> dict[str, Any]:
    current = ledger()
    result = _check_resource_projection(current)
    result["measured_new_attempt_process_cpu_seconds"] = sum(
        float(row.get("measured_process_cpu_seconds", 0.0)) for row in current.get("attempts", [])
    )
    result["remaining_builder_cpu_seconds"] = max(0.0, BUILDER_CAP - result["charged_builder_cpu_seconds"])
    result["remaining_native_gate_bc_cpu_seconds"] = max(0.0, NATIVE_CAP - result["charged_native_gate_bc_cpu_seconds"])
    result["no_prior_charge_reclaimed"] = True
    return result


def locked_root(root_id: str) -> dict[str, Any]:
    record = read_json(e5.CAPTURE_ROOTS_PATH)
    matches = [row for row in record["roots"] if str(row.get("root_id")) == str(root_id)]
    if len(matches) != 1:
        raise GateBCV13Failure(f"locked v9 capture does not uniquely contain root {root_id}")
    value = matches[0]
    projection = value["projection"]
    return {
        "root_id": str(value["root_id"]),
        "owner_root_index": int(value["root_index"]),
        "owner_run_id": str(value["run_id"]),
        "owner_seed": int(value["seed"]),
        "owner_decision_index": int(value["decision_index"]),
        "policy_decision_index": int(value["policy_decision_index"]),
        "screen": str(value["screen"]),
        "root_fingerprints": {
            "native_state_sha256": str(value["native_state_sha256"]),
            "public_projection_sha256": str(value["public_projection_sha256"]),
            "public_resource_state_sha256": str(value["public_resource_state_sha256"]),
        },
        "projection_digest": str(value["projection_digest"]),
        "raw_privileged_projection": projection,
        "sanitized_public_projection": value["public_projection"],
        "source_tag_mapping": stage0.source_tags(projection),
        "legal_action_occurrences": value["legal_action_identities"],
        "selected_owner_occurrence": int(value["candidate_index"]),
        "semantic_feature_matrix": value["feature_matrix"],
        "feature_matrix_sha256": runner.sha256_bytes(np.asarray(value["feature_matrix"], dtype="<f4").tobytes()),
        "candidate_binding_validation_passed": bool(value["live_binding_validation_passed"]),
    }


def load_history() -> list[dict[str, Any]]:
    value = read_json(e5.CAPTURE_HISTORY_PATH)
    if (
        value.get("schema_id") != "issue73-actual-action-step-history-v9-v1"
        or value.get("source_lock_sha256") != sha256_file(e5.LOCK_PATH)
        or value.get("event_count") != 111
        or len(value.get("events", [])) != 111
    ):
        raise GateBCV13Failure("locked v9 action/step history is not the reviewed complete 111-event record")
    return value["events"]


def gate_b_root_contract() -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    roots = {name: locked_root(root_id) for name, root_id in GATE_B_ROOTS.items()}
    if set(roots) != {"hidden_semantic", "native_rng_consequence", "post_two_third_family"}:
        raise GateBCV13Failure("Gate B root categories differ from the registered three-witness contract")
    for name, root in roots.items():
        if root["owner_seed"] != OWNER_SEED or root["candidate_binding_validation_passed"] is not True:
            raise GateBCV13Failure(f"Gate B {name} root is not a live-bound root from the locked owner history")
    hidden = q.compact_root(roots["hidden_semantic"])
    chest = q.compact_root(roots["native_rng_consequence"])
    post = q.compact_root(roots["post_two_third_family"])
    if not stage0.root_has_hidden_semantic(hidden):
        raise GateBCV13Failure("Gate B hidden witness is not the registered hidden MAP semantic")
    hidden_indices = [
        index for index in range(len(hidden["legal_action_identities"]))
        if isinstance(stage0.candidate_feature(hidden, index, "oracle_destination_room"), Mapping)
    ]
    if not hidden_indices or hidden_indices[0] != 0:
        raise GateBCV13Failure("Gate B hidden-semantic transition is not the registered legal occurrence zero")
    if not stage0.root_has_rng_consequence(chest):
        raise GateBCV13Failure("Gate B chest witness lacks the locked unavailable-loot source tag")
    open_chest_indices = []
    for index in range(len(chest["legal_action_identities"])):
        feature = stage0.candidate_feature(chest, index, "choice_type")
        value = feature.get("value") if isinstance(feature, Mapping) else feature
        if value == "open_chest":
            open_chest_indices.append(index)
    if open_chest_indices != [0]:
        raise GateBCV13Failure("Gate B chest open action differs from its precommitted legal occurrence")
    if not stage0.root_after_two_third_family(post):
        raise GateBCV13Failure("Gate B third witness is not the registered post-two noncombat family")
    families = {str(root["screen"]) for root in roots.values()}
    if len(families) < int(q.OLD_CONFIG["stage0"]["gate_b"]["minimum_distinct_screen_families"]):
        raise GateBCV13Failure("Gate B witness roots do not cover the required distinct screen families")
    return roots, {"hidden_semantic": 0, "native_rng_consequence": 0, "post_two_third_family": 0}


def public_reward_menu_actions_v13(value: Any) -> list[dict[str, Any]]:
    """Return only the complete, provenance-validated public reward menu kinds."""
    if not isinstance(value, Mapping):
        return []
    candidate_actions = value.get("candidate_actions")
    if not isinstance(candidate_actions, Mapping):
        return []
    if (
        candidate_actions.get("availability") != "available"
        or candidate_actions.get("source") != "StepSimulator::legalActions"
    ):
        return []
    actions = candidate_actions.get("value")
    if not isinstance(actions, list) or not actions:
        return []
    kinds = [action.get("kind") if isinstance(action, Mapping) else None for action in actions]
    if any(not isinstance(kind, str) or kind not in PUBLIC_REWARD_MENU_ACTION_KINDS for kind in kinds):
        return []
    # Preserve order and occurrence count while omitting every non-kind field.
    return [{"kind": kind} for kind in kinds]

def _public_transition_once(module: Any, history: Sequence[Mapping[str, Any]], root: Mapping[str, Any], action_index: int) -> dict[str, Any]:
    adapter = runner.native_adapter(module, OWNER_SEED)
    try:
        snapshot, actions, history_index = q.replay_prefix_to_root(adapter, history, root, module)
        if not 0 <= action_index < len(actions):
            raise GateBCV13Failure(f"registered Gate B occurrence is not legal at {root['root_id']}")
        pre_state, pre_public = q.event_state(adapter, snapshot)
        transition = adapter.step(actions[action_index])
        post_state, post_public = q.event_state(adapter, transition.snapshot)
        before_keys = ("native_state_sha256", "public_projection_sha256", "public_resource_state_sha256")
        witness = {
            "root_id": root["root_id"],
            "screen": root["screen"],
            "history_event_index": history_index,
            "action_index": action_index,
            "action_identity_sha256": sha256_bytes(canonical(root["legal_action_occurrences"][action_index]).encode("utf-8")),
            "pre_state_fingerprints": {key: pre_state[key] for key in before_keys},
            "pre_public_projection_sha256": sha256_bytes(canonical(pre_public).encode("utf-8")),
            "transition_terminal": bool(transition.terminal),
            "post_state_fingerprints": {key: post_state[key] for key in before_keys},
            "post_public_projection_sha256": sha256_bytes(canonical(post_public).encode("utf-8")),
            "public_reward_menu_actions": public_reward_menu_actions_v13(post_public),
        }
        return {"witness": witness, "pre_public": pre_public, "post_public": post_public}
    finally:
        adapter.close()


def _live_public_only_rejection(module: Any, history: Sequence[Mapping[str, Any]], root: Mapping[str, Any]) -> dict[str, Any]:
    adapter = runner.native_adapter(module, OWNER_SEED)
    try:
        snapshot, _actions, history_index = q.replay_prefix_to_root(adapter, history, root, module)
        rejected = False
        error = None
        try:
            module.PrivilegedNoncombatProjectionAccess(runner.native_simulator(adapter), "public_only").project_v1()
        except Exception as exc:
            rejected = True
            error = {"type": type(exc).__name__, "message": str(exc)[:400]}
        live_public = runner._public_projection_payload(adapter, snapshot)
        return {
            "status": "PASS" if rejected else "FAIL_PUBLIC_ONLY_AUDIENCE_ACCEPTED",
            "public_only_audience_rejected": rejected,
            "rejection": error,
            "history_event_index": history_index,
            "live_public_projection_sha256": sha256_bytes(canonical(live_public).encode("utf-8")),
            "live_public_projection": live_public,
        }
    finally:
        adapter.close()


def audit_sanitized_projection(raw_projection: Mapping[str, Any], public_projection: Mapping[str, Any]) -> dict[str, Any]:
    public_json = canonical(public_projection).lower()
    findings: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(raw_projection.get("candidates", [])):
        features = candidate.get("features", {}) if isinstance(candidate, Mapping) else {}
        if not isinstance(features, Mapping):
            continue
        for name, item in features.items():
            if not isinstance(item, Mapping) or (item.get("visibility") != "oracle_only" and item.get("availability") != "unavailable"):
                continue
            private_value = item.get("value")
            value_present = private_value is not None and canonical(private_value).lower() in public_json
            name_present = str(name).lower() in public_json
            findings.append({
                "candidate_index": candidate_index,
                "field": str(name),
                "visibility": item.get("visibility"),
                "availability": item.get("availability"),
                "source": item.get("source"),
                "reason": item.get("reason"),
                "value_sha256": sha256_bytes(canonical(private_value).encode("utf-8")) if private_value is not None else None,
                "field_name_present_in_sanitized_projection": name_present,
                "value_present_in_sanitized_projection": value_present,
            })
    return {
        "private_feature_count": len(findings),
        "all_private_names_and_values_absent": bool(findings) and all(
            not row["field_name_present_in_sanitized_projection"] and not row["value_present_in_sanitized_projection"]
            for row in findings
        ),
        "private_features": findings,
    }


def run_gate_b_live_privacy() -> dict[str, Any]:
    lock, config = verify_lock()
    report_path = OUT / "reports" / "gate-b-live-public-privacy-report-v13.json"
    attempt_path = OUT / "attempts" / f"{GATE_B_KEY}.json"
    if report_path.exists() or attempt_path.exists():
        existing = read_json(report_path) if report_path.exists() else {}
        if existing.get("status") == "PASS" and read_json(attempt_path).get("status") == "COMPLETE":
            return existing
        raise GateBCV13Failure("Gate B fixed live-privacy attempt already has a receipt; no retry or overwrite")
    gate_a = read_json(GATE_A_REPORT_PATH)
    parity = read_json(e6.PARITY_REPORT_PATH)
    if gate_a.get("status") != "PASS" or parity.get("status") != "PASS":
        raise GateBCV13Failure("Gate B requires the independently reviewed Gate A and v9 fresh-process parity PASS receipts")
    start_attempt(GATE_B_KEY, kind="three_fixed_gate_b_root_transitions_plus_live_public_only_rejection_and_sanitized_projection_negative", reserve=GATE_B_RESERVE, native_gate_bc=True)
    started_cpu = q.process_tree_cpu()
    witnesses: dict[str, Any] = {}
    try:
        q.install_process_cpu_ceiling(GATE_B_RESERVE)
        e6.OUT = OUT
        parent_lock = read_json(e5.LOCK_PATH)
        prepared = q.prepare_capture_prerequisites(
            out=OUT / "gate-b-runtime",
            source_lock=parent_lock,
            native_module_path_override=e5.NATIVE_MODULE_PATH,
        )
        module = prepared["module"]
        history = load_history()
        roots, action_indices = gate_b_root_contract()
        for name, root in roots.items():
            first = _public_transition_once(module, history, root, action_indices[name])
            second = _public_transition_once(module, history, root, action_indices[name])
            equal = canonical(first["witness"]) == canonical(second["witness"])
            witnesses[name] = {
                "status": "PASS" if equal else "FAIL_REPLAYED_TRANSITION_MISMATCH",
                "root_id": root["root_id"],
                "screen": root["screen"],
                "action_index": action_indices[name],
                "repeat_restore_and_transition_equal": equal,
                "transition": first["witness"],
            }
        hidden_root = roots["hidden_semantic"]
        hidden_live = _live_public_only_rejection(module, history, hidden_root)
        live_public = hidden_live.pop("live_public_projection")
        hidden_public_matches_locked = canonical(live_public) == canonical(hidden_root["sanitized_public_projection"])
        privacy = audit_sanitized_projection(hidden_root["raw_privileged_projection"], live_public)
        hidden_source_evidence = [
            {key: row.get(key) for key in ("field", "source", "visibility", "availability", "reason")}
            for row in hidden_root["source_tag_mapping"]
            if row.get("field") == "oracle_destination_room"
        ]
        chest = roots["native_rng_consequence"]
        chest_choices = witnesses["native_rng_consequence"]["transition"]["public_reward_menu_actions"]
        chest_changed = (
            witnesses["native_rng_consequence"]["transition"]["pre_public_projection_sha256"]
            != witnesses["native_rng_consequence"]["transition"]["post_public_projection_sha256"]
        )
        chest_menu_kinds = [row["kind"] for row in chest_choices]
        expected_chest_menu_kinds = list(config["gate_b_expected_public_menu_action_kinds"])
        witnesses["native_rng_consequence"]["status"] = (
            "PASS"
            if chest_menu_kinds == expected_chest_menu_kinds and chest_changed
            else "FAIL_CHEST_PUBLIC_MENU_INCOMPLETE_OR_CHANGED"
        )
        witnesses["native_rng_consequence"]["public_reward_menu_actions"] = chest_choices
        witnesses["native_rng_consequence"]["expected_public_reward_menu_action_kinds"] = expected_chest_menu_kinds
        witnesses["native_rng_consequence"]["native_rng_source"] = stage0.native_chest_rng_source_evidence()
        families = sorted({str(root["screen"]) for root in roots.values()})
        all_witnesses_pass = all(row.get("status") == "PASS" for row in witnesses.values())
        hidden_public_checks_pass = (
            hidden_live["status"] == "PASS"
            and hidden_public_matches_locked
            and privacy["all_private_names_and_values_absent"]
            and bool(hidden_source_evidence)
            and any(stage0.is_mapnode_room_source(row.get("source")) and row.get("visibility") == "oracle_only" for row in hidden_source_evidence)
        )
        gate_status = "PASS" if all_witnesses_pass and hidden_public_checks_pass and len(families) >= int(q.OLD_CONFIG["stage0"]["gate_b"]["minimum_distinct_screen_families"]) else "FAIL_OR_BLOCKED"
        report = {
            "schema_id": "issue73-gate-b-privileged-transition-v13-v1",
            "status": gate_status,
            "source_lock_sha256": sha256_file(LOCK_PATH),
            "code_source_commit": lock["code_source_commit"],
            "gate_a_parent_report_sha256": sha256_file(GATE_A_REPORT_PATH),
            "v9_parity_report_sha256": sha256_file(e6.PARITY_REPORT_PATH),
            "capture_history_sha256": sha256_file(e5.CAPTURE_HISTORY_PATH),
            "witness_root_ids": {name: root["root_id"] for name, root in roots.items()},
            "expected_public_reward_menu_action_kinds": expected_chest_menu_kinds,
            "distinct_witness_screen_families": families,
            "minimum_distinct_screen_families": int(q.OLD_CONFIG["stage0"]["gate_b"]["minimum_distinct_screen_families"]),
            "owner_seed": OWNER_SEED,
            "hidden_semantic_source_tags": hidden_source_evidence,
            "live_public_only_negative_test": hidden_live,
            "live_sanitized_public_projection_matches_locked_root": hidden_public_matches_locked,
            "live_sanitized_public_projection_sha256": hidden_live["live_public_projection_sha256"],
            "sanitized_projection_negative_check": privacy,
            "witnesses": witnesses,
            "native_rng_resampled": False,
            "formal_seed_consumed": False,
            "stage1_authorized": False,
            "source_interpretation": {
                "hidden_semantic": "MapNode::room is oracle-only at the exact replayed MAP root; it is not a future realization.",
                "rng_dependent_consequence": "one legal open-chest transition exposes public reward-menu action kinds; this does not identify a particular relic, amount, or future distribution.",
                "post_two_third_family": "one later owner-history noncombat screen verifies the registered multi-decision witness.",
                "conditional_value_or_distribution": "not identified by Gate B",
            },
            "scope": "all three registered Gate B root/action witnesses plus live public-only rejection and sanitized-projection negative; chest evidence is the public reward menu only, with no item/value/distribution claim",
        }
    except BaseException as exc:
        failure = {
            "schema_id": "issue73-gate-b-privileged-transition-failure-v13-v1",
            "status": "FAIL_OR_INCOMPLETE_NO_RETRY",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1200],
            "partial_witnesses": witnesses,
            "no_retry": True,
            "formal_seed_consumed": False,
            "stage1_authorized": False,
        }
        runner.atomic_json(OUT / "reports" / "gate-b-privileged-transition-failure-v13.json", failure)
        finish_attempt(GATE_B_KEY, started_cpu=started_cpu, status="FAIL_OR_INCOMPLETE", details=failure)
        raise
    finished = finish_attempt(GATE_B_KEY, started_cpu=started_cpu, status="COMPLETE" if report["status"] == "PASS" else "FAIL_OR_INCOMPLETE", details={
        "gate_status": report["status"],
        "formal_seed_consumed": False,
        "no_retry": True,
    })
    if finished.get("status") != "COMPLETE":
        report["status"] = finished["status"]
    report["attempt_receipt"] = finished
    runner.atomic_json(report_path, report)
    push_evidence("Issue 73: preserve live Gate B public privacy evidence")
    return report


def _prepare_v13_runtime(out: Path) -> dict[str, Any]:
    parent_lock = read_json(e5.LOCK_PATH)
    prepared = q.prepare_capture_prerequisites(
        out=out,
        source_lock=parent_lock,
        native_module_path_override=e5.NATIVE_MODULE_PATH,
    )
    module_path = Path(prepared["module"].__file__).resolve(strict=True)
    module_hash = runner.validate_native_module_identity(
        module_path,
        expected_name=q.OLD_CONFIG["identity"]["native_module"],
        expected_sha256=q.OLD_CONFIG["identity"]["native_binary_sha256"],
    )
    if str(module_path) != parent_lock["native"]["module_path"] or module_hash != parent_lock["native"]["loaded_module_sha256"]:
        raise GateBCV13Failure("Gate C runtime loaded a different native module than the reviewed v9 lock")
    if prepared["model_digest"] != parent_lock["replay_provenance"]["model_digest"]:
        raise GateBCV13Failure("Gate C runtime loaded a different historical policy")
    return prepared


def setup_gate_c_v13() -> tuple[dict[str, Any], dict[str, Any], Any, Any, Any, str, list[dict[str, Any]]]:
    lock, config = verify_lock()
    gate_b_path = OUT / "reports" / "gate-b-live-public-privacy-report-v13.json"
    if not gate_b_path.is_file() or read_json(gate_b_path).get("status") != "PASS":
        raise GateBCV13Failure("Gate C requires the live Gate B privacy PASS")
    if read_json(e6.PARITY_REPORT_PATH).get("status") != "PASS" or read_json(GATE_A_REPORT_PATH).get("status") != "PASS":
        raise GateBCV13Failure("Gate C requires the already reviewed Gate A and fresh-process parity PASS receipts")
    existing = []
    for index in (1, 2):
        key = f"gate-c-environment-and-policy-setup-{index:02d}"
        path = OUT / "attempts" / f"{key}.json"
        if path.is_file():
            row = read_json(path)
            if row.get("status") != "COMPLETE":
                start_attempt(key, kind="gate_c_environment_and_policy_setup", reserve=GATE_C_SETUP_RESERVE, native_gate_bc=True)
                raise GateBCV13Failure("Gate C setup attempt is terminal/incomplete; no retry")
            existing.append(key)
        else:
            selected_key = key
            break
    else:
        raise GateBCV13Failure("both fixed Gate C environment setup slots are exhausted")
    start_attempt(selected_key, kind="gate_c_environment_and_policy_setup", reserve=GATE_C_SETUP_RESERVE, native_gate_bc=True)
    started_cpu = q.process_tree_cpu()
    runtime_out = OUT / "capture-preflight-runtime" / selected_key
    try:
        prepared = _prepare_v13_runtime(runtime_out)
        setup_report = {
            "schema_id": "issue73-gate-c-replay-setup-v13-v1",
            "status": "READY",
            "setup_attempt_key": selected_key,
            "source_lock_sha256": sha256_file(LOCK_PATH),
            "parent_capture_source_lock_sha256": sha256_file(e5.LOCK_PATH),
            "native_module_path": str(Path(prepared["module"].__file__).resolve()),
            "native_module_sha256": prepared["environment"]["identity"]["native_module_sha256"],
            "source_history_sha256": sha256_file(e5.CAPTURE_HISTORY_PATH),
            "gate_c_plan_sha256": sha256_file(e5.PLAN_PATH),
            "policy_artifact_sha256": runner.sha256_file(prepared["model_path"]),
            "policy_tensor_digest": prepared["model_digest"],
            "suffix_policy": config["suffixed_policy"] if "suffixed_policy" in config else q.OLD_CONFIG["stage0"]["suffix_policy"],
            "native_rng_resampling": "none",
            "formal_seed_consumed": False,
            "stage1_authorized": False,
        }
    except BaseException as exc:
        failure = {
            "schema_id": "issue73-gate-c-replay-setup-failure-v13-v1",
            "status": "FAIL_OR_INCOMPLETE_NO_RETRY",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1200],
            "no_retry": True,
            "formal_seed_consumed": False,
        }
        runner.atomic_json(OUT / "reports" / f"{selected_key}-failure.json", failure)
        finish_attempt(selected_key, started_cpu=started_cpu, status="FAIL_OR_INCOMPLETE", details=failure)
        raise
    finished = finish_attempt(selected_key, started_cpu=started_cpu, status="COMPLETE", details={"setup_status": "READY", "formal_seed_consumed": False})
    setup_report["attempt_receipt"] = finished
    if finished.get("status") != "COMPLETE":
        setup_report["status"] = "FAIL_CPU_RESERVATION_EXCEEDED"
        runner.atomic_json(OUT / "reports" / f"{selected_key}.json", setup_report)
        raise GateBCV13Failure("Gate C setup exceeded its fixed CPU reserve")
    runner.atomic_json(OUT / "reports" / f"{selected_key}.json", setup_report)
    push_evidence("Issue 73: preserve replay-qualified Gate C setup receipt")
    return lock, config, prepared["module"], prepared["encoder"], prepared["model"], prepared["model_digest"], load_history()


def _install_gate_c_bridge() -> None:
    q.OUT = OUT
    q.PLAN_PATH = e5.PLAN_PATH
    q.LOCK_PATH = LOCK_PATH
    q.RESOURCE_LEDGER_PATH = LEDGER_PATH
    q.GATE_C_ROOTS = GATE_C_ROOTS
    q.NATIVE_GATE_BC_CAP = NATIVE_CAP
    q.verify_lock = lambda **_kwargs: verify_lock()
    q.ledger = ledger
    q.charged = lambda value=None: (
        sum(float(row.get("cpu_charged_seconds", row.get("reserved_cpu_seconds", 0.0))) for row in (value or ledger()).get("attempts", [])),
        sum(float(row.get("cpu_charged_seconds", row.get("reserved_cpu_seconds", 0.0))) for row in (value or ledger()).get("attempts", []) if row.get("native_gate_bc") is True),
    )
    q.start_attempt = start_attempt
    q.finish_attempt = finish_attempt
    q.push_scoped_commit = push_evidence
    q.root_receipt = locked_root
    q.load_history = load_history
    q.setup_gate_c = setup_gate_c_v13
    q.budget_summary = budget_summary


def run_gate_c() -> dict[str, Any]:
    verify_lock()
    gate_b_path = OUT / "reports" / "gate-b-live-public-privacy-report-v13.json"
    if not gate_b_path.is_file() or read_json(gate_b_path).get("status") != "PASS":
        raise GateBCV13Failure("Gate C requires the live Gate B privacy PASS")
    _install_gate_c_bridge()
    report = q.gate_c_suffixes()
    if report.get("status") != "PASS" or report.get("planned_suffix_attempt_count") != 64 or report.get("completed_suffix_attempt_count") != 64:
        raise GateBCV13Failure("fixed Gate C replay did not complete all registered suffixes")
    summary = finalize_stage0_gate_bc()
    runner.atomic_json(OUT / "reports" / "stage0-gate-bc-report-v13.json", summary)
    push_evidence("Issue 73: finalize replay-qualified Stage-0 Gate B/C report")
    return report


def finalize_stage0_gate_bc() -> dict[str, Any]:
    gate_b = read_json(OUT / "reports" / "gate-b-live-public-privacy-report-v13.json")
    gate_c_path = OUT / "reports" / "gate-c-replay-uncertainty-report.json"
    gate_c = read_json(gate_c_path) if gate_c_path.is_file() else {}
    gate_a = read_json(GATE_A_REPORT_PATH)
    parity = read_json(e6.PARITY_REPORT_PATH)
    passed = all(row.get("status") == "PASS" for row in (gate_a, parity, gate_b, gate_c))
    return {
        "schema_id": "issue73-stage0-gate-bc-aggregate-report-v13-v1",
        "status": "PASS" if passed else "INCOMPLETE_OR_FAIL",
        "code_disposition": "STUDY_ONLY",
        "source_lock_sha256": sha256_file(LOCK_PATH),
        "code_source_commit": read_json(LOCK_PATH)["code_source_commit"],
        "parent_gate_a_report_sha256": sha256_file(GATE_A_REPORT_PATH),
        "parent_v9_parity_report_sha256": sha256_file(e6.PARITY_REPORT_PATH),
        "gate_b_report_sha256": sha256_file(OUT / "reports" / "gate-b-live-public-privacy-report-v13.json"),
        "gate_c_report_sha256": sha256_file(gate_c_path) if gate_c_path.is_file() else None,
        "gate_statuses": {"A": gate_a.get("status"), "B": gate_b.get("status"), "C": gate_c.get("status"), "v9_prefix_parity": parity.get("status")},
        "resource_accounting": budget_summary(),
        "formal_seed_consumed": False,
        "stage1_authorized": False,
        "limits": [
            "Gate A is synthetic optimizer responsiveness only; the legacy comparison also passed.",
            "Gate B privacy is witnessed only on the exact replay-qualified MAP root and does not establish general noninterference.",
            "Gate C estimates full native-state return uncertainty conditional on the exact owner roots and pi_suffix; it is not public information-set Q.",
            "No Stage 1, training, additional formal owner, or game RNG resampling is authorized or performed.",
        ],
    }


def _run_static_tests() -> dict[str, Any]:
    import test_stage0_gate_bc_v13

    suite = unittest.defaultTestLoader.loadTestsFromModule(test_stage0_gate_bc_v13)
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
    report_path = OUT / "reports" / "static-qualification-report-v13.json"
    failure_path = OUT / "reports" / "static-qualification-failure-v13.json"
    if report_path.exists() or failure_path.exists():
        existing = read_json(report_path if report_path.exists() else failure_path)
        if existing.get("status") == "PASS":
            return existing
        raise GateBCV13Failure("static verification has a terminal receipt; do not retry or overwrite")
    if any(path.exists() for path in (OUT / "reports" / "gate-b-live-public-privacy-report-v13.json", OUT / "attempts" / f"{GATE_B_KEY}.json")):
        raise GateBCV13Failure("static prelaunch verification must precede any new Gate B attempt")
    q.install_process_cpu_ceiling(STATIC_RESERVE)
    started = q.process_tree_cpu()
    try:
        tests = _run_static_tests()
        report = {
            "schema_id": "issue73-stage0-gate-bc-v13-static-qualification-report-v1",
            "status": tests["status"],
            "source_lock_sha256": sha256_file(LOCK_PATH),
            "code_source_commit": lock["code_source_commit"],
            "tests": tests,
            "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - started),
            "fixed_static_reservation_cpu_seconds": STATIC_RESERVE,
            "native_adapter_instantiated": False,
            "native_game_step_started": False,
            "formal_seed_consumed": False,
            "stage1_authorized": False,
        }
        runner.atomic_json(report_path, report)
        push_evidence("Issue 73: preserve Stage-0 Gate B/C v13 static verification")
        if report["status"] != "PASS":
            raise GateBCV13Failure("focused Stage-0 Gate B/C v13 static tests failed; no native phase started")
        return report
    except BaseException as exc:
        if report_path.exists():
            raise
        failure = {
            "schema_id": "issue73-stage0-gate-bc-v13-static-qualification-failure-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1200],
            "fixed_static_reservation_cpu_seconds": STATIC_RESERVE,
            "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - started),
            "native_adapter_instantiated": False,
            "native_game_step_started": False,
            "formal_seed_consumed": False,
            "no_retry_or_overwrite": True,
        }
        runner.atomic_json(failure_path, failure)
        push_evidence("Issue 73: preserve Stage-0 Gate B/C v13 static verification failure")
        raise


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare-lock", "verify-lock", "static", "gate-b", "gate-c", "finalize"))
    args = parser.parse_args(argv)
    if args.command == "prepare-lock":
        result = prepare_lock()
    elif args.command == "verify-lock":
        lock, _config = verify_lock()
        result = {"status": "PASS", "source_lock_sha256": sha256_file(LOCK_PATH), "code_source_commit": lock["code_source_commit"]}
    elif args.command == "static":
        result = run_static_verification()
    elif args.command == "gate-b":
        result = run_gate_b_live_privacy()
    elif args.command == "gate-c":
        result = run_gate_c()
    else:
        result = finalize_stage0_gate_bc()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"PASS", "READY", "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
