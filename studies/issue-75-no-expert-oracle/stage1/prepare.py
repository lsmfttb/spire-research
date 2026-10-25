"""Build the immutable, still-disabled Stage 1 package from Issue 75 locks.

Run only in the pinned Linux Python 3.13 environment. This command imports the
native extension for its API-negative check; it creates no simulator, game,
checkpoint, projection, Stage 1 attempt, or seed receipt.
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

import learning  # noqa: E402
import model  # noqa: E402
import native_negative  # noqa: E402
import preflight  # noqa: E402
import runtime  # noqa: E402
from stage1_budget import (  # noqa: E402
    ALL_IN_LIMIT,
    BUILDER_LIMIT,
    OWNER_LOGIN,
    REVIEWER_LIMIT,
    canonical,
    sha256_bytes,
)


PREPARATION_BASE = "a8410ff48ecf168f904726c79da302d10f921984"
STAGE0_EVIDENCE_COMMIT = "7f7538a706710626740cdb635d3711e69eac900f"
STAGE0_SOURCE_COMMIT = "c77d64243320639bf156330760d511d2a13eee85"
STAGE0_SOURCE_LOCK_SHA256 = "b3ce3d33c27ec392f6d88151bce3c86a8fdce8c9dc0152d417069441f7140d38"
STAGE0_REVIEW_COMMENT_ID = 6403333026
COST_REVIEW_COMMENT_ID = 6403856784
PLANNER_BUDGET_COMMENT_ID = 6403907358
HELDOUT_ARMS = ("P0", "P1", "R", "J", "E")
SOURCE_FILES = (
    "config.json",
    "source-lock.json",
    "source-lock.sha256",
    "algorithm.py",
    "learning.py",
    "model.py",
    "native_negative.py",
    "preflight.py",
    "runtime.py",
    "stage0.py",
    "test_issue75_contract.py",
    "planning/build_stage1_cost_model.py",
    "planning/stage1-cost-model.json",
    "planning/stage1-cost-model.md",
    "stage1_budget.py",
    "stage1/config.json",
    "stage1/seed-table.json",
    "stage1/budget.json",
    "stage1/prepare.py",
    "stage1/test_activation.py",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


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
    training_block_order_seeds = [
        derive_seed("block-order", 75_000_000, f"BLOCK-{block}") for block in range(4)
    ]
    return {
        "schema_id": "issue75-stage1-preparation-config-v1",
        "study_id": "issue-75-no-expert-oracle",
        "scope": "STUDY_ONLY",
        "preparation_base_commit": PREPARATION_BASE,
        "execution": {
            "package_status": "PREPARATION_ONLY_REQUIRES_EXACT_REVIEWER_PASS_AND_LATER_PLANNER_GO",
            "stage1_execution_authorized": False,
            "formal_training_enabled": False,
            "formal_evaluation_enabled": False,
            "nonformal_preflight_enabled": False,
            "native_sampling_enabled": False,
            "planner_activation_required": True,
            "reviewer_pass_must_precede_planner_activation": True,
            "exact_package_commit_and_source_lock_must_be_cited": True,
        },
        "activation_gate": {
            "owner_login": OWNER_LOGIN,
            "github_user_type": "User",
            "reviewer_pass_marker": "STAGE1_REVIEW_PASS",
            "planner_activation_marker": "STAGE1_PLANNER_ACTIVATE",
            "reviewer_and_planner_comments_must_be_distinct_and_ordered": True,
            "both_comments_bind_exact_package_commit_and_source_lock_sha256": True,
            "separate_github_account_required": False,
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
            "source_and_binary_lock_is_required_before_any_activation": True,
        },
        "algorithm": {
            "policy": copy.deepcopy(config["policy"]),
            "search": copy.deepcopy(config["search"]),
            "learning": learning_config,
        },
        "resources": {
            "ledger_name": "separate prospective Stage 1 ledger; starts at zero only after explicit Planner activation",
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
            "runs_only_after_exact_package_reviewer_pass_and_planner_activation": True,
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
        "ledger_status": "NOT_ACTIVATED_ZERO_CHARGES",
        "stage1_execution_authorized": False,
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
        "package_status": "PREPARATION_ONLY_DISABLED",
        "execution_authorized": False,
        "prepared_from_commit": PREPARATION_BASE,
        "final_package_commit_binding": "must be named by exact SHA in Reviewer PASS and subsequent Planner activation comments",
        "stage1_config_sha256": config_sha256,
        "seed_table_sha256": table_sha256,
        "budget_config_sha256": budget_sha256,
        "locked_files_sha256_lf": locked_files,
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


def verify_stage1_package() -> dict[str, Any]:
    lock_path = STAGE1 / "source-lock.json"
    lock = preflight.load_json(lock_path)
    if lock.get("execution_authorized") is not False or lock.get("package_status") != "PREPARATION_ONLY_DISABLED":
        raise ValueError("Stage 1 package no longer fails closed")
    execution = preflight.load_json(STAGE1 / "config.json").get("execution", {})
    disabled = (
        "stage1_execution_authorized", "formal_training_enabled", "formal_evaluation_enabled",
        "nonformal_preflight_enabled", "native_sampling_enabled",
    )
    if any(execution.get(key) is not False for key in disabled):
        raise ValueError("a Stage 1 execution flag is not explicitly disabled")
    if set(lock.get("locked_files_sha256_lf", {})) != set(SOURCE_FILES):
        raise ValueError("Stage 1 source lock does not cover the exact prepared package")
    for relative, expected in lock["locked_files_sha256_lf"].items():
        actual = preflight.normalized_sha256(STUDY / relative)
        if actual != expected:
            raise ValueError(f"Stage 1 package source lock mismatch: {relative}")
    table = preflight.load_json(STAGE1 / "seed-table.json")
    rows = table.get("rows", [])
    ids = [row.get("unit_id") for row in rows]
    if len(rows) != 1032 or len(set(ids)) != 1032 or any(not value for value in ids):
        raise ValueError("Stage 1 seed-table owner/arm rows or unit identifiers are incomplete")
    if table.get("counts") != {
        "training_owners": 512,
        "heldout_owner_arm_rows": 480,
        "preflight_owner_arm_rows": 40,
        "preflight_arm_schedule": list(HELDOUT_ARMS),
        "preflight_is_nonformal_and_never_enters_efficacy": True,
    }:
        raise ValueError("Stage 1 seed-table population differs from the registered schedule")
    budget = preflight.load_json(STAGE1 / "budget.json")
    if budget.get("stage1_execution_authorized") is not False or budget.get("ledger_status") != "NOT_ACTIVATED_ZERO_CHARGES":
        raise ValueError("Stage 1 budget ledger must remain disabled and uncharged")
    sidecar = (STAGE1 / "source-lock.sha256").read_text(encoding="ascii").split()[0]
    if preflight.normalized_sha256(lock_path) != sidecar:
        raise ValueError("Stage 1 source lock sidecar mismatch")
    if lock.get("formal_seeds_or_games_consumed") != 0 or lock.get("native_sampling_performed") is not False:
        raise ValueError("preparation package records unauthorized sampling or formal execution")
    return lock


def main() -> int:
    print(json.dumps(prepare_stage1_package(), sort_keys=True))
    verify_stage1_package()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
