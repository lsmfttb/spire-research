"""Build and verify the source-locked Stage 1 package for the registered preflight.

Run only in the pinned Linux Python 3.13 environment. The builder/verifier
commands do not create a simulator, game, checkpoint, projection, attempt, or seed receipt.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping


STUDY = Path(__file__).resolve().parents[1]
STAGE1 = STUDY / "stage1"
sys.path.insert(0, str(STUDY))

import preflight  # noqa: E402
from stage1_budget import (  # noqa: E402
    ALL_IN_LIMIT,
    BUILDER_LIMIT,
    LOCKED_PACKAGE_FILES,
    LOCKED_REPOSITORY_DEPENDENCIES,
    REVIEWER_LIMIT,
    RESOURCE_BUDGET_AMENDMENT,
    PRIOR_USE_CPU_ACCOUNTING,
    PRIOR_USE_CPU_ACCOUNTING_AMENDMENT,
    canonical,
    sha256_bytes,
    verify_stage1_package as verify_package_lock,
)


PREPARATION_BASE = "a8410ff48ecf168f904726c79da302d10f921984"
STAGE0_EVIDENCE_COMMIT = "7f7538a706710626740cdb635d3711e69eac900f"
STAGE0_SOURCE_COMMIT = "c77d64243320639bf156330760d511d2a13eee85"
STAGE0_SOURCE_LOCK_SHA256 = "b3ce3d33c27ec392f6d88151bce3c86a8fdce8c9dc0152d417069441f7140d38"
STAGE0_REVIEW_COMMENT_ID = 6403333026
COST_REVIEW_COMMENT_ID = 6403856784
PLANNER_BUDGET_COMMENT_ID = 6403907358
HELDOUT_ARMS = ("P0", "P1", "R", "J", "E")
SOURCE_FILES = LOCKED_PACKAGE_FILES


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_resource_budget_amendment(lock: Mapping[str, Any]) -> None:
    amendment = RESOURCE_BUDGET_AMENDMENT
    base_commit = amendment["base_package_commit"]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=STUDY,
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()
    lock_path = STAGE1 / "source-lock.json"
    recorded_amendment = lock.get("resource_budget_amendment")
    if head != base_commit or recorded_amendment not in (None, amendment):
        raise ValueError("resource-cap amendment is allowed only from the exact reviewed base package")
    if recorded_amendment is None and preflight.normalized_sha256(lock_path) != amendment["base_source_lock_sha256"]:
        raise ValueError("resource-cap amendment requires the exact original Stage 1 source lock")

    def historical_file(relative_path: str) -> bytes:
        repository_path = f"{STUDY.relative_to(STUDY.parents[1]).as_posix()}/{relative_path}"
        return subprocess.run(
            ["git", "show", f"{base_commit}:{repository_path}"], cwd=STUDY,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout

    locked_hashes = lock.get("locked_files_sha256_lf", {})
    config_bytes = historical_file("stage1/config.json")
    budget_bytes = historical_file("stage1/budget.json")
    config_hash = hashlib.sha256(config_bytes).hexdigest()
    budget_hash = hashlib.sha256(budget_bytes).hexdigest()
    if recorded_amendment is None and (
        lock.get("stage1_config_sha256") != config_hash
        or lock.get("budget_config_sha256") != budget_hash
        or locked_hashes.get("stage1/config.json") != config_hash
        or locked_hashes.get("stage1/budget.json") != budget_hash
    ):
        raise ValueError("resource-cap amendment base files do not match the original source lock")

    expected_config = json.loads(config_bytes)
    expected_config["resources"]["builder_process_tree_cpu_hard_seconds"] = BUILDER_LIMIT
    expected_config["resources"]["reviewer_cpu_protected_seconds"] = REVIEWER_LIMIT
    expected_config["resources"]["all_in_cpu_hard_seconds"] = ALL_IN_LIMIT
    if preflight.load_json(STAGE1 / "config.json") != expected_config:
        raise ValueError("resource-cap amendment changed Stage 1 config beyond the approved ceilings")

    expected_budget = json.loads(budget_bytes)
    expected_budget["limits_cpu_seconds"] = {
        "builder": BUILDER_LIMIT,
        "reviewer_protected": REVIEWER_LIMIT,
        "all_in": ALL_IN_LIMIT,
    }
    if preflight.load_json(STAGE1 / "budget.json") != expected_budget:
        raise ValueError("resource-cap amendment changed Stage 1 budget beyond the approved ceilings")


def _verify_prior_use_cpu_accounting_amendment(lock: Mapping[str, Any]) -> None:
    amendment = PRIOR_USE_CPU_ACCOUNTING_AMENDMENT
    base_commit = amendment["base_package_commit"]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=STUDY,
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()
    lock_path = STAGE1 / "source-lock.json"
    if (
        head != base_commit
        or lock.get("prior_use_cpu_accounting_amendment") is not None
        or preflight.normalized_sha256(lock_path) != amendment["base_source_lock_sha256"]
    ):
        raise ValueError("prior-use accounting is allowed only from its exact reviewed source lock")

    def historical_file(relative_path: str) -> bytes:
        repository_path = f"{STUDY.relative_to(STUDY.parents[1]).as_posix()}/{relative_path}"
        return subprocess.run(
            ["git", "show", f"{base_commit}:{repository_path}"], cwd=STUDY,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout

    config_bytes = historical_file("stage1/config.json")
    budget_bytes = historical_file("stage1/budget.json")
    locked = lock.get("locked_files_sha256_lf", {})
    config_hash = hashlib.sha256(config_bytes.replace(b"\r\n", b"\n")).hexdigest()
    budget_hash = hashlib.sha256(budget_bytes.replace(b"\r\n", b"\n")).hexdigest()
    if (
        lock.get("stage1_config_sha256") != config_hash
        or lock.get("budget_config_sha256") != budget_hash
        or locked.get("stage1/config.json") != config_hash
        or locked.get("stage1/budget.json") != budget_hash
        or preflight.load_json(STAGE1 / "config.json") != json.loads(config_bytes)
    ):
        raise ValueError("prior-use accounting amendment changed locked config or lost its base")
    expected_budget = json.loads(budget_bytes)
    expected_budget["prior_unledgered_builder_cpu_use"] = PRIOR_USE_CPU_ACCOUNTING
    if preflight.load_json(STAGE1 / "budget.json") != expected_budget:
        raise ValueError("budget changed beyond the reviewed prior-use CPU allowance")


def _stage0_source_blob(source_commit: str, relative_path: str) -> bytes:
    study_prefix = preflight.STUDY.relative_to(preflight.ROOT).as_posix()
    repository_path = f"{study_prefix}/{relative_path}"
    return subprocess.run(
        ["git", "show", f"{source_commit}:{repository_path}"], cwd=STUDY,
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def _stage0_historical_file_hashes() -> dict[str, str]:
    hashes = {}
    for relative_path in preflight.LOCKED_FILES:
        blob = _stage0_source_blob(STAGE0_SOURCE_COMMIT, relative_path)
        hashes[relative_path] = hashlib.sha256(blob.replace(b"\r\n", b"\n")).hexdigest()
    return hashes


def write_immutable(path: Path, value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError(f"refusing to replace locked Stage 1 file: {path.relative_to(STUDY)}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    return sha256_bytes(payload)


def derive_seed(domain: str, owner_seed: int, arm: str) -> int:
    payload = f"issue75-stage1-seed-table-v1|{domain}|{owner_seed}|{arm}".encode("ascii")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & 0x7FFFFFFF


BLOCK_ORDER_SEED_OWNER = 75_000_000


def derive_block_order_seed(block: int) -> int:
    if not isinstance(block, int) or isinstance(block, bool) or not 0 <= block < 4:
        raise ValueError("training block index must be in the registered 0..3 range")
    return derive_seed("block-order", BLOCK_ORDER_SEED_OWNER, f"BLOCK-{block}")


def derive_search_attempt_seed(invocation_seed: int, attempt_index: int) -> int:
    if not 0 <= attempt_index < 16:
        raise ValueError("search attempt index must be inside the registered 0..15 cap")
    return derive_seed("search-attempt", invocation_seed, f"ATTEMPT-{attempt_index}")


def make_seed_table() -> dict[str, Any]:
    training: list[dict[str, Any]] = []
    for owner_seed in range(7_500_001, 7_500_513):
        block = (owner_seed - 7_500_001) // 128
        training.append({
            "phase": "training",
            "formal": True,
            "owner_seed": owner_seed,
            "game_seed": owner_seed,
            "arm": "TRAIN",
            "unit_id": f"TRAIN:{owner_seed}",
            "block_index": block,
            "paired_owner_key": f"train-{owner_seed}",
            "policy_rng_seed": derive_seed("policy", owner_seed, "TRAIN"),
            "search_invocation_seeds": [
                derive_seed("search-invocation", owner_seed, f"TRAIN-ROOT-{index}") for index in range(4)
            ],
        })

    heldout: list[dict[str, Any]] = []
    for owner_seed in range(7_510_001, 7_510_097):
        for arm in HELDOUT_ARMS:
            heldout.append({
                "phase": "heldout",
                "formal": True,
                "owner_seed": owner_seed,
                "game_seed": owner_seed,
                "arm": arm,
                "unit_id": f"HELDOUT:{owner_seed}:{arm}",
                "block_index": None,
                "paired_owner_key": f"heldout-{owner_seed}",
                "policy_rng_seed": None if arm == "E" else derive_seed("policy", owner_seed, arm),
                "search_invocation_seeds": [
                    derive_seed("search-invocation", owner_seed, f"{arm}-ROOT-{index}") for index in range(7)
                ] if arm in {"R", "J"} else [],
            })

    preflight: list[dict[str, Any]] = []
    for owner_seed in range(7_520_001, 7_520_009):
        for arm in HELDOUT_ARMS:
            preflight.append({
                "phase": "preflight",
                "formal": False,
                "owner_seed": owner_seed,
                "game_seed": owner_seed,
                "arm": arm,
                "unit_id": f"PREFLIGHT:{owner_seed}:{arm}",
                "block_index": None,
                "paired_owner_key": f"preflight-{owner_seed}",
                "policy_rng_seed": None if arm == "E" else derive_seed("policy", owner_seed, arm),
                "search_invocation_seeds": [
                    derive_seed("search-invocation", owner_seed, f"{arm}-ROOT-{index}") for index in range(7)
                ] if arm in {"R", "J"} else [],
            })

    return {
        "schema_id": "issue75-stage1-owner-arm-seed-table-v1",
        "study_id": "issue-75-no-expert-oracle",
        "seed_semantics": {
            "owner_seed_is_game_seed": True,
            "same_heldout_seed_across_arms": "blocking identifier only; not equal future RNG consumption after divergence",
            "policy_and_search_streams": "domain-separated SHA-256 derivation; never the native game RNG stream",
            "formal_owner_ids_are_disjoint_from_issue71_73_74": True,
        },
        "rows": training + heldout + preflight,
        "counts": {
            "training_owners": len(training),
            "heldout_owner_arm_rows": len(heldout),
            "preflight_owner_arm_rows": len(preflight),
            "preflight_arm_schedule": list(HELDOUT_ARMS),
            "preflight_is_nonformal_and_never_enters_efficacy": True,
        },
    }


def _verify_stage0_lock_and_evidence(config: Mapping[str, Any]) -> dict[str, Any]:
    provenance = preflight.verify_lineage(config)
    preflight.verify_provenance_file(config, provenance)
    expected = preflight.make_source_lock(config, provenance)
    # Stage 0's file digests bind the exact reviewed source commit. This check
    # runs from a later Stage 1 descendant, whose implementation files may have
    # changed while retaining the historical Stage 0 report and provenance.
    expected["files_sha256_lf"] = _stage0_historical_file_hashes()
    actual = preflight.load_json(STUDY / "source-lock.json")
    # The Stage 0 source lock names its original active branch. We prepare from
    # a descendant cost-model branch, so compare every lock field except the
    # current worktree branch name; the pinned remote branch and evidence SHA
    # below are checked independently.
    if {k: v for k, v in expected.items() if k != "source_branch"} != {
        k: v for k, v in actual.items() if k != "source_branch"
    }:
        raise ValueError("Stage 0 lock content differs from its reviewed source/config/provenance")
    branch = str(actual["source_branch"])
    if branch != "codex/issue-75-stage0-repair-20261026":
        raise ValueError("Stage 0 evidence branch is not the registered source lineage")
    remote_source = subprocess.run(
        ["git", "rev-parse", f"origin/{branch}"], cwd=STUDY, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()
    if remote_source != STAGE0_EVIDENCE_COMMIT:
        raise ValueError("registered Stage 0 evidence commit differs from the pinned remote")
    lock_digest = preflight.normalized_sha256(STUDY / "source-lock.json")
    sidecar = (STUDY / "source-lock.sha256").read_text(encoding="utf-8").split()[0]
    report_path = STUDY / "stage0/attempts/source-c77d6424-b3ce3d33-pass1/receipts/stage0-report.json"
    report = preflight.load_json(report_path)
    if lock_digest != STAGE0_SOURCE_LOCK_SHA256 or sidecar != lock_digest:
        raise ValueError("Stage 0 source-lock digest differs from the reviewed exact source")
    if report.get("source_commit") != STAGE0_SOURCE_COMMIT or report.get("source_lock_sha256_lf") != lock_digest:
        raise ValueError("Stage 0 report is not bound to the reviewed source and lock")
    if report.get("status") != "STAGE0_NONFORMAL_REVIEWABLE_COMPLETE":
        raise ValueError("Stage 0 report is not the reviewed completed nonformal attempt")
    if any(report.get(key) is not False for key in (
        "formal_training_seed_consumed", "formal_evaluation_seed_consumed",
        "formal_seed_table_present", "native_checkpoint_persisted",
        "privileged_projection_persisted",
    )):
        raise ValueError("Stage 0 report violates its completed nonformal contract")
    return {
        "source_commit": STAGE0_SOURCE_COMMIT,
        "source_lock_sha256_lf": lock_digest,
        "evidence_commit": STAGE0_EVIDENCE_COMMIT,
        "evidence_report_sha256": file_sha256(report_path),
        "review_comment_id": STAGE0_REVIEW_COMMENT_ID,
        "cumulative_builder_cpu_seconds": report["resource_accounting"]["builder_cpu_charged_seconds"],
        "cumulative_native_cpu_seconds": report["resource_accounting"]["native_worker_cpu_charged_seconds"],
    }


def _optimizer_descriptor(policy: model.ActionConditionedPolicy) -> dict[str, Any]:
    import learning

    optimizer = learning._new_optimizer(policy)
    state = optimizer.state_dict()
    groups = []
    for group in state["param_groups"]:
        groups.append({
            "parameter_count": len(group["params"]),
            "options": {key: value for key, value in group.items() if key != "params"},
        })
    return {
        "class": f"{type(optimizer).__module__}.{type(optimizer).__qualname__}",
        "param_groups": groups,
        "state_is_empty_before_first_step": not bool(state["state"]),
    }


def _runtime_lock(config: Mapping[str, Any]) -> dict[str, Any]:
    import model
    import native_negative
    import runtime

    runtime.verify_source_repositories(config)
    native_path = runtime.verify_native_module(config)
    negative = native_negative.run_negative_check()
    if negative["status"] != "PINNED_NATIVE_IMPORT_API_NEGATIVE_PASS":
        raise ValueError("pinned native import-only API gate failed")
    if negative["games_started"] or negative["simulator_instances_created"] or negative["formal_seeds_consumed"]:
        raise ValueError("native API gate unexpectedly constructed or sampled a game")
    import numpy
    import torch
    if sys.version.split()[0] != str(config["source"]["python_version"]):
        raise ValueError("Python runtime version differs from the registered source")
    if str(numpy.__version__) != str(config["source"]["numpy_version"]):
        raise ValueError("NumPy runtime version differs from the registered source")
    if str(torch.__version__) != str(config["source"]["torch_version"]):
        raise ValueError("PyTorch runtime version differs from the registered source")
    policy_config = config["policy"]
    pi0 = model.ActionConditionedPolicy(
        seed=int(policy_config["initial_model_seed"]),
        final_weight_std=float(policy_config["actor_final_weight_std"]),
    )
    optimizer = _optimizer_descriptor(pi0)
    return {
        "python_version": sys.version.split()[0],
        "numpy_version": str(numpy.__version__),
        "torch_version": str(torch.__version__),
        "sts_lightspeed_commit": config["source"]["sts_lightspeed_commit"],
        "stsr_source_commit": config["source"]["stsr_source_commit"],
        "native_module_path": str(native_path),
        "native_module_sha256": negative["native_module_sha256"],
        "native_import_only_negative_test": negative,
        "pi0_model_seed": int(policy_config["initial_model_seed"]),
        "pi0_model_digest": model.model_digest(pi0),
        "optimizer_initial_state": optimizer,
        "optimizer_initial_state_sha256": sha256_bytes(canonical(optimizer)),
    }


def _make_stage1_config(
    config: Mapping[str, Any], seed_table_sha256: str,
    stage0: Mapping[str, Any], runtime_lock: Mapping[str, Any],
) -> dict[str, Any]:
    learning_config = copy.deepcopy(config["learning"])
    training_block_order_seeds = [derive_block_order_seed(block) for block in range(4)]
    return {
        "schema_id": "issue75-stage1-preparation-config-v1",
        "study_id": "issue-75-no-expert-oracle",
        "scope": "STUDY_ONLY",
        "preparation_base_commit": PREPARATION_BASE,
        "execution": {
            "package_status": "PREFLIGHT_ONLY_ENABLED",
            "stage1_execution_authorized": True,
            "formal_training_enabled": False,
            "formal_evaluation_enabled": False,
            "nonformal_preflight_enabled": True,
            "native_sampling_enabled": True,
            "authorized_phases": ["preflight"],
        },
        "population": {
            "training_owners": {"first_owner_seed": 7_500_001, "last_owner_seed": 7_500_512, "count": 512},
            "training_blocks": 4,
            "training_owners_per_block": 128,
            "heldout_owners": {"first_owner_seed": 7_510_001, "last_owner_seed": 7_510_096, "count": 96},
            "heldout_arms": list(HELDOUT_ARMS),
            "preflight_owners": {"first_owner_seed": 7_520_001, "last_owner_seed": 7_520_008, "count": 8},
            "preflight_arm_schedule": list(HELDOUT_ARMS),
            "preflight_arm_schedule_basis": "all five registered arms are included for operational qualification only; schedule requires independent package review and is not efficacy evidence",
            "preflight_results_are_not_formal_or_efficacy_evidence": True,
        },
        "seeds": {
            "table_path": "seed-table.json",
            "table_sha256": seed_table_sha256,
            "block_order_seeds": training_block_order_seeds,
            "block_order_seed_domain": "issue75-stage1-seed-table-v1|block-order",
            "policy_rng_seed_derivation": "domain-separated SHA-256 owner/arm values in seed-table.json",
            "search_invocation_seed_derivation": "domain-separated SHA-256 seed per locked arm and root opportunity in seed-table.json",
            "search_attempt_seed_derivation": "derive_search_attempt_seed(invocation_seed, attempt_index), fixed attempt index 0..15",
            "game_seed_is_never_reused_from_issue71_73_74": True,
        },
        "source": {
            "registered_stage0_config": copy.deepcopy(config["source"]),
            "algorithm_sha256_lf": preflight.normalized_sha256(STUDY / "algorithm.py"),
            "learning_sha256_lf": preflight.normalized_sha256(STUDY / "learning.py"),
            "model_sha256_lf": preflight.normalized_sha256(STUDY / "model.py"),
            "runtime_sha256_lf": preflight.normalized_sha256(STUDY / "runtime.py"),
            "initial_model_digest": runtime_lock["pi0_model_digest"],
            "initial_optimizer_state_sha256": runtime_lock["optimizer_initial_state_sha256"],
            "source_and_binary_lock_is_required_before_any_registered_phase": True,
        },
        "algorithm": {
            "policy": copy.deepcopy(config["policy"]),
            "search": copy.deepcopy(config["search"]),
            "learning": learning_config,
        },
        "resources": {
            "ledger_name": "one cumulative Stage 1 ledger; starts at zero before the registered preflight",
            "builder_process_tree_cpu_hard_seconds": int(BUILDER_LIMIT),
            "reviewer_cpu_protected_seconds": int(REVIEWER_LIMIT),
            "all_in_cpu_hard_seconds": int(ALL_IN_LIMIT),
            "reviewer_reserve_is_not_transferable": True,
            "stage0_builder_hard_seconds": int(config["resources"]["builder_process_tree_cpu_hard_seconds"]),
            "stage0_native_hard_seconds": int(config["resources"]["native_root_checkpoint_terminal_cpu_hard_seconds"]),
            "stage0_all_in_hard_seconds": int(config["resources"]["all_in_cpu_hard_seconds"]),
            "stage0_cumulative_builder_at_completed_attempt_seconds": stage0["cumulative_builder_cpu_seconds"],
            "stage0_cumulative_native_at_completed_attempt_seconds": stage0["cumulative_native_cpu_seconds"],
            "stage0_and_stage1_ledgers_do_not_carry_over": True,
            "search_invocation_cpu_limit_seconds": config["search"]["search_invocation_cpu_cap_seconds"],
            "search_invocation_started_attempt_limit": config["search"]["search_invocation_started_attempt_cap"],
        },
        "preflight_protocol": {
            "runs_after_one_focused_independent_review_of_the_revised_executable_path": True,
            "owner_seeds_are_the_eight_rows_marked_preflight": True,
            "arms_are_all_five_registered_heldout_arms": True,
            "recompute_full_remaining_cohort_forecast_before_formal_owner_seed": True,
            "no_outcome_conditioned_replacement_or_subset": True,
        },
        "provenance": {
            "stage0": dict(stage0),
            "cost_model_commit": PREPARATION_BASE,
            "cost_model_json_sha256": file_sha256(STUDY / "planning/stage1-cost-model.json"),
            "cost_model_review_comment_id": COST_REVIEW_COMMENT_ID,
            "planner_budget_decision_comment_id": PLANNER_BUDGET_COMMENT_ID,
            "runtime": dict(runtime_lock),
        },
    }


def _make_budget_config() -> dict[str, Any]:
    return {
        "schema_id": "issue75-stage1-budget-config-v1",
        "study_id": "issue-75-no-expert-oracle",
        "ledger_status": "PREFLIGHT_AUTHORIZED_ZERO_CHARGES",
        "stage1_execution_authorized": True,
        "stage0_charges_or_caps_included": False,
        "limits_cpu_seconds": {
            "builder": int(BUILDER_LIMIT),
            "reviewer_protected": int(REVIEWER_LIMIT),
            "all_in": int(ALL_IN_LIMIT),
        },
        "reviewer_reserve_transferable_to_builder": False,
        "concurrent_stage1_process_trees": 1,
        "reservation_policy": "write-ahead unique run ID; unresolved reservation is charged at its full cap; finished receipt charges measured process-tree CPU",
        "resume_policy": "completed unit IDs cannot run again; incomplete units require exact previous checkpoint digest and new process-run ID; lost unreceipted processes remain fully reserved",
        "future_forecast_required_before_formal_owner_seed": True,
        "search_call_cpu_cap_seconds": 32,
        "search_call_started_attempt_cap": 16,
        "accounting_method": "Linux /proc process tree own+reaped-descendant CPU ticks; serialized supervised worker tree; fail closed if root/accounting disappears",
    }


def _locked_file_hashes() -> dict[str, str]:
    values: dict[str, str] = {}
    for relative in SOURCE_FILES:
        path = STUDY / relative
        if not path.is_file():
            raise FileNotFoundError(f"required Stage 1 lock input missing: {relative}")
        values[relative] = preflight.normalized_sha256(path)
    return values


def _locked_repository_dependency_hashes() -> dict[str, str]:
    repository_root = STUDY.parents[1].resolve()
    values: dict[str, str] = {}
    for relative in LOCKED_REPOSITORY_DEPENDENCIES:
        path = (repository_root / relative).resolve()
        if not path.is_relative_to(repository_root) or not path.is_file():
            raise FileNotFoundError(f"required repository source lock input missing: {relative}")
        values[relative] = preflight.normalized_sha256(path)
    return values


def prepare_stage1_package() -> dict[str, Any]:
    current = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=STUDY, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()
    if current != PREPARATION_BASE:
        raise RuntimeError(f"prepare package from the exact reviewed cost-model base, got {current}")
    source_config = preflight.load_json(STUDY / "config.json")
    stage0 = _verify_stage0_lock_and_evidence(source_config)
    runtime_lock = _runtime_lock(source_config)
    table = make_seed_table()
    table_sha256 = write_immutable(STAGE1 / "seed-table.json", table)
    stage1_config = _make_stage1_config(source_config, table_sha256, stage0, runtime_lock)
    config_sha256 = write_immutable(STAGE1 / "config.json", stage1_config)
    budget = _make_budget_config()
    budget_sha256 = write_immutable(STAGE1 / "budget.json", budget)
    locked_files = _locked_file_hashes()
    lock = {
        "schema_id": "issue75-stage1-source-lock-v1",
        "study_id": "issue-75-no-expert-oracle",
        "scope": "STUDY_ONLY",
        "package_status": "PREFLIGHT_ONLY_ENABLED",
        "execution_authorized": True,
        "prepared_from_commit": PREPARATION_BASE,
        "final_package_commit_binding": "all execution receipts bind the exact package commit and source-lock digest",
        "resource_budget_amendment": RESOURCE_BUDGET_AMENDMENT,
        "stage1_config_sha256": config_sha256,
        "seed_table_sha256": table_sha256,
        "budget_config_sha256": budget_sha256,
        "locked_files_sha256_lf": locked_files,
        "locked_repository_dependencies_sha256_lf": _locked_repository_dependency_hashes(),
        "stage0": stage0,
        "runtime": runtime_lock,
        "formal_seeds_or_games_consumed": 0,
        "native_sampling_performed": False,
        "model_training_performed": False,
    }
    lock_sha256 = write_immutable(STAGE1 / "source-lock.json", lock)
    sidecar = f"{lock_sha256}  source-lock.json\n".encode("ascii")
    sidecar_path = STAGE1 / "source-lock.sha256"
    if sidecar_path.exists():
        if sidecar_path.read_bytes() != sidecar:
            raise FileExistsError("refusing to replace Stage 1 source-lock digest")
    else:
        with sidecar_path.open("xb") as stream:
            stream.write(sidecar)
            stream.flush()
            os.fsync(stream.fileno())
    return {
        "status": lock["package_status"],
        "stage1_config_sha256": config_sha256,
        "seed_table_sha256": table_sha256,
        "source_lock_sha256": lock_sha256,
        "seed_row_count": len(table["rows"]),
        "training_rows": table["counts"]["training_owners"],
        "heldout_rows": table["counts"]["heldout_owner_arm_rows"],
        "preflight_rows": table["counts"]["preflight_owner_arm_rows"],
        "native_sampling_performed": False,
        "formal_seeds_or_games_consumed": 0,
    }


def _atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for index in range(100):
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{index}.tmp")
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"cannot allocate an in-directory temporary file for {path.name}")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _preflight_only_inputs(config: Mapping[str, Any], budget: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retire comment authorization while preserving every registered study input."""
    updated_config = copy.deepcopy(dict(config))
    execution = updated_config.get("execution")
    gate = updated_config.get("activation_gate")
    if not isinstance(execution, dict) or not isinstance(gate, dict):
        raise ValueError("existing Stage 1 package is not the disabled comment-gated package")
    if (
        execution.get("package_status") != "PREPARATION_ONLY_REQUIRES_EXACT_REVIEWER_PASS_AND_LATER_PLANNER_GO"
        or execution.get("stage1_execution_authorized") is not False
        or execution.get("formal_training_enabled") is not False
        or execution.get("formal_evaluation_enabled") is not False
        or execution.get("nonformal_preflight_enabled") is not False
        or execution.get("native_sampling_enabled") is not False
        or execution.get("planner_activation_required") is not True
        or execution.get("reviewer_pass_must_precede_planner_activation") is not True
        or gate.get("separate_github_account_required") is not False
    ):
        raise ValueError("existing Stage 1 execution contract is not the expected retired gate")
    del updated_config["activation_gate"]
    execution.clear()
    execution.update({
        "package_status": "PREFLIGHT_ONLY_ENABLED",
        "stage1_execution_authorized": True,
        "formal_training_enabled": False,
        "formal_evaluation_enabled": False,
        "nonformal_preflight_enabled": True,
        "native_sampling_enabled": True,
        "authorized_phases": ["preflight"],
    })
    resources = updated_config["resources"]
    resources["ledger_name"] = "one cumulative Stage 1 ledger; starts at zero before the registered preflight"
    source = updated_config["source"]
    if source.pop("source_and_binary_lock_is_required_before_any_activation", None) is not True:
        raise ValueError("registered source and binary lock precondition is missing")
    source["source_and_binary_lock_is_required_before_any_registered_phase"] = True
    protocol = updated_config["preflight_protocol"]
    if protocol.pop("runs_only_after_exact_package_reviewer_pass_and_planner_activation", None) is not True:
        raise ValueError("old preflight comment gate is missing")
    protocol["runs_after_one_focused_independent_review_of_the_revised_executable_path"] = True

    updated_budget = copy.deepcopy(dict(budget))
    if (
        updated_budget.get("stage1_execution_authorized") is not False
        or updated_budget.get("ledger_status") != "NOT_ACTIVATED_ZERO_CHARGES"
    ):
        raise ValueError("existing Stage 1 budget is not in the expected uncharged disabled state")
    updated_budget["ledger_status"] = "PREFLIGHT_AUTHORIZED_ZERO_CHARGES"
    updated_budget["stage1_execution_authorized"] = True
    return updated_config, updated_budget


def enable_registered_preflight() -> dict[str, Any]:
    """Enable only the already registered 40-row preflight after source-lock checks."""
    lock_path = STAGE1 / "source-lock.json"
    sidecar_path = STAGE1 / "source-lock.sha256"
    lock = preflight.load_json(lock_path)
    digest = preflight.normalized_sha256(lock_path)
    sidecar = sidecar_path.read_text(encoding="ascii").split()
    if (
        not sidecar or sidecar[0] != digest
        or lock.get("schema_id") != "issue75-stage1-source-lock-v1"
        or lock.get("study_id") != "issue-75-no-expert-oracle"
        or lock.get("scope") != "STUDY_ONLY"
        or lock.get("execution_authorized") is not False
        or lock.get("package_status") != "PREPARATION_ONLY_DISABLED"
        or lock.get("formal_seeds_or_games_consumed") != 0
        or lock.get("native_sampling_performed") is not False
        or lock.get("model_training_performed") is not False
    ):
        raise ValueError("existing source lock is not the registered disabled Stage 1 package")
    config_path = STAGE1 / "config.json"
    budget_path = STAGE1 / "budget.json"
    if (
        preflight.normalized_sha256(config_path) != lock.get("stage1_config_sha256")
        or preflight.normalized_sha256(budget_path) != lock.get("budget_config_sha256")
    ):
        raise ValueError("refusing preflight enablement from changed locked inputs")
    config, budget = _preflight_only_inputs(
        preflight.load_json(config_path), preflight.load_json(budget_path),
    )
    _atomic_replace(
        config_path,
        json.dumps(config, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n",
    )
    _atomic_replace(
        budget_path,
        json.dumps(budget, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n",
    )
    return refresh_stage1_source_lock(preflight_transition=True)


def refresh_stage1_source_lock(*, preflight_transition: bool = False) -> dict[str, Any]:
    """Relock reviewed executor repairs; a single transition may enable approved preflight."""
    lock_path = STAGE1 / "source-lock.json"
    sidecar_path = STAGE1 / "source-lock.sha256"
    lock = preflight.load_json(lock_path)
    old_digest = preflight.normalized_sha256(lock_path)
    sidecar = sidecar_path.read_text(encoding="ascii").split()
    if not sidecar or sidecar[0] != old_digest:
        raise ValueError("refusing to refresh a source lock whose existing sidecar is invalid")
    old_disabled = (
        lock.get("execution_authorized") is False
        and lock.get("package_status") == "PREPARATION_ONLY_DISABLED"
    )
    new_preflight = (
        lock.get("execution_authorized") is True
        and lock.get("package_status") == "PREFLIGHT_ONLY_ENABLED"
    )
    formal_enabled = (
        lock.get("execution_authorized") is True
        and lock.get("package_status") in {"FORMAL_TRAINING_ENABLED", "FORMAL_EXECUTION_ENABLED"}
    )
    if (
        lock.get("schema_id") != "issue75-stage1-source-lock-v1"
        or lock.get("study_id") != "issue-75-no-expert-oracle"
        or lock.get("scope") != "STUDY_ONLY"
        or lock.get("formal_seeds_or_games_consumed") != 0
        or lock.get("native_sampling_performed") is not False
        or lock.get("model_training_performed") is not False
        or (preflight_transition and not old_disabled)
        or (not preflight_transition and not (new_preflight or formal_enabled))
    ):
        raise ValueError("refusing to refresh a source lock outside the registered Stage 1 package")

    added_repair_files = {
        "stage1/executor.py", "stage1/test_executor.py", "stage1/preflight-proxy.json",
        "stage1/block0-repair-lineage.json", "stage1/block0-decoder-compatibility.json",
        "stage1/update0-seed-failure-continuation.json",
    }
    legacy_self_lock_files = {"source-lock.json", "source-lock.sha256"}
    existing_file_set = frozenset(lock.get("locked_files_sha256_lf", {}))
    accepted_file_sets = {
        frozenset(SOURCE_FILES),
        frozenset(set(SOURCE_FILES) - {"stage1/block0-repair-lineage.json"}),
        frozenset(set(SOURCE_FILES) - {"stage1/block0-decoder-compatibility.json"}),
        frozenset(set(SOURCE_FILES) - {"stage1/update0-seed-failure-continuation.json"}),
        frozenset(set(SOURCE_FILES) | {"stage1/test_activation_gate.py"}),
        frozenset((set(SOURCE_FILES) - added_repair_files) | legacy_self_lock_files),
    }
    if existing_file_set not in accepted_file_sets:
        raise ValueError("existing source lock does not contain the expected registered package file set")

    if preflight_transition:
        config = preflight.load_json(STAGE1 / "config.json")
        budget = preflight.load_json(STAGE1 / "budget.json")
        _preflight_only_inputs_from_enabled(config, budget)
        lock["execution_authorized"] = True
        lock["package_status"] = "PREFLIGHT_ONLY_ENABLED"
        lock["final_package_commit_binding"] = (
            "all execution receipts bind the exact package commit and source-lock digest"
        )

    immutable_files = {
        "stage1/config.json": "stage1_config_sha256",
        "stage1/seed-table.json": "seed_table_sha256",
        "stage1/budget.json": "budget_config_sha256",
    }
    approved_budget_input_amendment = False
    resource_cap_amendment = False
    prior_use_cpu_amendment = False
    if not preflight_transition:
        changed_budget_inputs = {
            relative for relative, field in immutable_files.items()
            if relative in {"stage1/config.json", "stage1/budget.json"}
            and preflight.normalized_sha256(STUDY / relative) != lock.get(field)
        }
        if changed_budget_inputs == {"stage1/config.json", "stage1/budget.json"}:
            _verify_resource_budget_amendment(lock)
            resource_cap_amendment = True
            approved_budget_input_amendment = True
        elif changed_budget_inputs == {"stage1/budget.json"}:
            _verify_prior_use_cpu_accounting_amendment(lock)
            prior_use_cpu_amendment = True
            approved_budget_input_amendment = True
        elif changed_budget_inputs:
            raise ValueError("locked resource inputs differ outside the exact approved amendment")
        else:
            for relative, field in immutable_files.items():
                if preflight.normalized_sha256(STUDY / relative) != lock.get(field):
                    raise ValueError(f"refusing to change registered Stage 1 input: {relative}")

    refreshed = _locked_file_hashes()
    repairable = {
        "stage1_budget.py", "stage1/prepare.py", "stage1/executor.py",
        "stage1/test_activation.py", "stage1/test_executor.py", "stage1/preflight-proxy.json",
        "stage1/block0-repair-lineage.json", "stage1/block0-decoder-compatibility.json",
        "stage1/update0-seed-failure-continuation.json",
        "algorithm.py", "learning.py", "runtime.py",
    }
    if preflight_transition or approved_budget_input_amendment:
        repairable |= {"stage1/config.json", "stage1/budget.json"}
    changed = {
        relative for relative, digest in refreshed.items()
        if lock["locked_files_sha256_lf"].get(relative) != digest
    }
    removed = set(lock["locked_files_sha256_lf"]) - set(refreshed)
    if not changed <= repairable or not removed <= {"stage1/test_activation_gate.py"}:
        raise ValueError("source-lock refresh found changes outside the approved repair and preflight scope")
    repository_dependencies = _locked_repository_dependency_hashes()
    old_repository_dependencies = lock.get("locked_repository_dependencies_sha256_lf", {})
    if not isinstance(old_repository_dependencies, dict):
        raise ValueError("existing repository dependency lock is malformed")
    if frozenset(old_repository_dependencies) not in {frozenset(), frozenset(repository_dependencies)}:
        raise ValueError("existing repository dependency lock has an unexpected file set")
    changed_dependencies = {
        relative for relative, digest in repository_dependencies.items()
        if old_repository_dependencies.get(relative) not in {None, digest}
    }
    if not changed_dependencies <= {"studies/issue-73-privileged-noncombat-oracle-stage0/runner.py"}:
        raise ValueError("source-lock refresh found changes outside the authorized Issue 73 runner binding")
    lock["locked_files_sha256_lf"] = refreshed
    lock["locked_repository_dependencies_sha256_lf"] = repository_dependencies
    if resource_cap_amendment:
        lock["resource_budget_amendment"] = RESOURCE_BUDGET_AMENDMENT
    if prior_use_cpu_amendment:
        lock["prior_use_cpu_accounting_amendment"] = PRIOR_USE_CPU_ACCOUNTING_AMENDMENT
    lock["final_package_commit_binding"] = (
        "all execution receipts bind the exact package commit and source-lock digest"
    )
    lock["stage1_config_sha256"] = preflight.normalized_sha256(STAGE1 / "config.json")
    lock["seed_table_sha256"] = preflight.normalized_sha256(STAGE1 / "seed-table.json")
    lock["budget_config_sha256"] = preflight.normalized_sha256(STAGE1 / "budget.json")
    payload = json.dumps(lock, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    _atomic_replace(lock_path, payload)
    digest = preflight.normalized_sha256(lock_path)
    _atomic_replace(sidecar_path, f"{digest}  source-lock.json\n".encode("ascii"))
    verify_stage1_package()
    return {"status": lock["package_status"], "source_lock_sha256": digest, "relocked_files": sorted(changed)}


def _preflight_only_inputs_from_enabled(
    config: Mapping[str, Any], budget: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_config = copy.deepcopy(dict(config))
    execution = expected_config.get("execution")
    expected_execution = {
        "package_status": "PREFLIGHT_ONLY_ENABLED",
        "stage1_execution_authorized": True,
        "formal_training_enabled": False,
        "formal_evaluation_enabled": False,
        "nonformal_preflight_enabled": True,
        "native_sampling_enabled": True,
        "authorized_phases": ["preflight"],
    }
    if not isinstance(execution, dict) or execution != expected_execution:
        raise ValueError("Stage 1 config is not the registered preflight-only phase set")
    if "activation_gate" in expected_config:
        raise ValueError("retired comment-gate fields remain in Stage 1 config")
    if (
        expected_config.get("resources", {}).get("ledger_name")
        != "one cumulative Stage 1 ledger; starts at zero before the registered preflight"
        or expected_config.get("source", {}).get("source_and_binary_lock_is_required_before_any_registered_phase") is not True
        or expected_config.get("preflight_protocol", {}).get(
            "runs_after_one_focused_independent_review_of_the_revised_executable_path",
        ) is not True
        or "runs_only_after_exact_package_reviewer_pass_and_planner_activation"
        in expected_config.get("preflight_protocol", {})
    ):
        raise ValueError("Stage 1 config contains unexpected activation or preflight policy")
    expected_budget = copy.deepcopy(dict(budget))
    if (
        expected_budget.get("stage1_execution_authorized") is not True
        or expected_budget.get("ledger_status") != "PREFLIGHT_AUTHORIZED_ZERO_CHARGES"
    ):
        raise ValueError("Stage 1 budget is not the authorized zero-charge preflight state")
    return expected_config, expected_budget


def verify_stage1_package() -> dict[str, Any]:
    return verify_package_lock()


def main() -> int:
    if sys.argv[1:] == ["--enable-registered-preflight"]:
        result = enable_registered_preflight()
    elif sys.argv[1:] == ["--refresh-source-lock"]:
        result = refresh_stage1_source_lock()
    elif not sys.argv[1:]:
        result = prepare_stage1_package()
    else:
        raise SystemExit("usage: prepare.py [--enable-registered-preflight|--refresh-source-lock]")
    print(json.dumps(result, sort_keys=True))
    verify_stage1_package()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
