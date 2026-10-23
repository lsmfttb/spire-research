"""Bounded retrospective diagnostics for integrated Issue 72.

All model inputs and native receipts are read from the immutable Issue 71
source tree. Historical training updates are replayed in memory only. This
script never launches the game, consumes a study seed, writes into stage1, or
changes a registered checkpoint.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
from typing import Any, Iterable, Mapping

import numpy as np
import torch


DIAGNOSTIC = Path(__file__).resolve().parent
STUDY = DIAGNOSTIC.parent
ART = STUDY / "stage1-artifacts"
CONFIG_PATH = STUDY / "config.json"
EXPECTED_SPIRE_SHA = "8d1b04527777c8602b4b5238b29d4a776723ae63"
EXPECTED_NATIVE_SHA = "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2"
EXPECTED_STSRL_SHA = "7d172f7c24c9d29429b6946ba44f6b59760b0d96"
EXPECTED_NATIVE_INTEGRATION_SHA = "d61c7c2a120aa90730fd8530718a7cd7cd856b46"
EXPECTED_MODULE_SHA = "bb22332a4a7108e8f9691482df2624b3b132d75ca07e1aa7b394b932193e5cad"
NATIVE_ROOT = Path("/home/lsmft/sts-lightspeed-issue71-native")
STSRL_ROOT = Path("/home/lsmft/stsrl-issue71-source")
NATIVE_MODULE_DIR = Path("/home/lsmft/stsrl-spikes/build-issue71-8ded-py313")
PYTHON = Path("/home/lsmft/stsrl-spikes/py313-torch/bin/python3.13")
sys.path.insert(0, str(STUDY))
sys.path.insert(0, str(STSRL_ROOT / "src"))
import runner  # noqa: E402 - exact study-local implementation under audit


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_new(name: str, value: Mapping[str, Any]) -> Path:
    target = DIAGNOSTIC / f"{name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
    return target


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def file_inventory_and_manifest() -> dict[str, Any]:
    manifest_path = ART / "artifact-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    expected = {item["path"]: item for item in manifest["files"]}
    actual_paths = {
        path.relative_to(ART).as_posix()
        for path in ART.rglob("*")
        if path.is_file() and path != manifest_path
    }
    mismatches: list[dict[str, Any]] = []
    bytes_checked = 0
    for rel, item in expected.items():
        path = ART / Path(rel)
        if not path.is_file():
            mismatches.append({"path": rel, "issue": "missing"})
            continue
        actual_size = path.stat().st_size
        actual_sha = sha256_file(path)
        bytes_checked += actual_size
        if actual_size != item["bytes"] or actual_sha != item["sha256"]:
            mismatches.append({
                "path": rel,
                "expected_bytes": item["bytes"],
                "actual_bytes": actual_size,
                "expected_sha256": item["sha256"],
                "actual_sha256": actual_sha,
            })
    unexpected = sorted(actual_paths - set(expected))
    missing_entries = sorted(set(expected) - actual_paths)
    return {
        "schema_id": manifest.get("schema_id"),
        "study_id": manifest.get("study_id"),
        "manifest_path": "stage1-artifacts/artifact-manifest.json",
        "manifest_bytes": len(manifest_bytes),
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "manifest_entry_count": len(expected),
        "actual_file_count_excluding_manifest": len(actual_paths),
        "manifested_bytes_checked": bytes_checked,
        "file_set_matches": not unexpected and not missing_entries,
        "unexpected_path_count": len(unexpected),
        "missing_path_count": len(missing_entries),
        "content_mismatch_count": len(mismatches),
        "first_content_mismatches": mismatches[:10],
        "first_unexpected_paths": unexpected[:10],
        "first_missing_paths": missing_entries[:10],
        "passed": not unexpected and not missing_entries and not mismatches,
    }


def validate_decision_run(run_path: Path, *, require_native: bool) -> dict[str, Any]:
    run = load_json(run_path)
    run_id = str(run["run_id"])
    npz_path = ART / "decisions" / f"{run_id}.npz"
    with np.load(npz_path, allow_pickle=False) as data:
        features = data["features"]
        offsets = data["offsets"]
        choices = data["choices"]
        errors: list[str] = []
        if features.ndim != 2 or features.shape[1] != 2048 or features.dtype != np.float32:
            errors.append("features shape/dtype mismatch")
        if offsets.ndim != 1 or choices.ndim != 1 or len(offsets) != len(choices) + 1:
            errors.append("offset/choice dimensions mismatch")
        if len(offsets) and (int(offsets[0]) != 0 or int(offsets[-1]) != len(features)):
            errors.append("offset boundary mismatch")
        if len(offsets) > 1 and np.any(np.diff(offsets.astype(np.int64)) <= 0):
            errors.append("nonpositive candidate row count")
        categorical = [d for d in run.get("decisions", []) if d.get("kind") == "categorical"]
        if len(categorical) != len(choices):
            errors.append("JSON categorical rows do not join to NPZ choices")
        max_actions = 0
        identity_rows = 0
        for index, decision in enumerate(categorical[:len(choices)]):
            left, right = int(offsets[index]), int(offsets[index + 1])
            count = right - left
            identities = decision.get("legal_action_identities", [])
            audit_count = decision.get("projection_audit", {}).get("candidate_count")
            chosen = int(choices[index])
            max_actions = max(max_actions, count)
            identity_rows += 1
            if count < 2:
                errors.append(f"categorical row {index} is not multi-action")
            if len(identities) != count or audit_count != count:
                errors.append(f"row {index} legal identity/projection count mismatch")
            if not 0 <= chosen < count or int(decision.get("chosen_index", -2)) != chosen:
                errors.append(f"row {index} chosen index mismatch")
            stable_ids = [x.get("stable_id") for x in identities]
            if len(stable_ids) != len(set(stable_ids)):
                errors.append(f"row {index} contains duplicate legal-action stable IDs")
            if require_native and decision.get("projection_audit", {}).get("native_source_commit") != EXPECTED_NATIVE_SHA:
                errors.append(f"row {index} native projection source mismatch")
        stored_sha = run.get("decision_artifact_sha256")
        stored_size = run.get("decision_artifact_bytes")
        actual_sha = sha256_file(npz_path)
        actual_size = npz_path.stat().st_size
        if stored_sha != actual_sha or stored_size != actual_size:
            errors.append("run receipt decision-artifact digest/size mismatch")
        if require_native and (
            run.get("native_source_commit") != EXPECTED_NATIVE_SHA
            or run.get("native_module_sha256") != EXPECTED_MODULE_SHA
        ):
            errors.append("run receipt native source/module identity mismatch")
        return {
            "run_id": run_id,
            "arm": run.get("arm"),
            "seed": run.get("seed"),
            "terminal_complete": run.get("terminal_complete"),
            "npz_bytes": actual_size,
            "npz_sha256": actual_sha,
            "candidate_rows": len(choices),
            "candidate_matrix_shape": list(features.shape),
            "identity_rows_joined": identity_rows,
            "max_legal_action_count": max_actions,
            "errors": errors,
        }


def checkpoint_identity_gate() -> dict[str, Any]:
    report = load_json(ART / "stage1-train-report.json")
    training_status = load_json(ART / "training-status.json")
    initial_path = ART / "policies" / "pi_init.pt"
    final_path = ART / "policies" / "pi_final.pt"
    initial, initial_digest = runner._load_policy(initial_path)
    final, final_digest = runner._load_policy(final_path)
    config_digest = sha256_file(CONFIG_PATH)
    blocks: list[dict[str, Any]] = []
    previous_digest = initial_digest
    previous_state: dict[str, Any] | None = None
    for block in range(1, 5):
        path = ART / "checkpoints" / f"block_{block:02d}.pt"
        state = torch.load(path, map_location="cpu", weights_only=True)
        update = load_json(ART / "updates" / f"block_{block:02d}.json")
        model = runner.AllNoncombatPolicy(
            dimension=runner.CONFIG["model"]["hash_dimension"],
            seed=runner.CONFIG["model"]["torch_seed"],
        )
        model.load_state_dict(state["model_state"])
        model_digest = runner._model_digest(model)
        model_deltas = []
        if previous_state is not None:
            for key, tensor in state["model_state"].items():
                delta = tensor.detach().cpu() - previous_state["model_state"][key].detach().cpu()
                model_deltas.append(float(torch.max(torch.abs(delta)).item()) if delta.numel() else 0.0)
        else:
            for key, tensor in state["model_state"].items():
                delta = tensor.detach().cpu() - initial.state_dict()[key].detach().cpu()
                model_deltas.append(float(torch.max(torch.abs(delta)).item()) if delta.numel() else 0.0)
        update_metrics_match = all(
            state.get("metrics", {}).get(key) == value
            for key, value in update.items()
            if key != "checkpoint_sha256"
        )
        blocks.append({
            "block": block,
            "file_bytes": path.stat().st_size,
            "file_sha256": sha256_file(path),
            "recorded_checkpoint_sha256": update.get("checkpoint_sha256"),
            "checkpoint_file_sha_matches": sha256_file(path) == update.get("checkpoint_sha256"),
            "config_sha256_matches": state.get("config_sha256") == config_digest,
            "study_id_matches": state.get("study_id") == runner.STUDY_ID,
            "one_optimizer_step_per_block": state.get("one_optimizer_step_per_block"),
            "block_state_matches": state.get("block") == block,
            "previous_model_digest": previous_digest,
            "block_start_digest_matches": update.get("block_start_policy_digest") == previous_digest,
            "model_digest": model_digest,
            "recorded_model_digest": state.get("model_digest"),
            "update_end_digest": update.get("block_end_policy_digest"),
            "model_digest_matches": model_digest == state.get("model_digest") == update.get("block_end_policy_digest"),
            "checkpoint_metrics_match_update": update_metrics_match,
            "model_max_abs_delta_from_previous": max(model_deltas, default=0.0),
            "optimizer_state_parameter_count": len(state.get("optimizer_state", {}).get("state", {})),
        })
        previous_digest = model_digest
        previous_state = state
    initial_sha = sha256_file(initial_path)
    final_sha = sha256_file(final_path)
    return {
        "config_sha256": config_digest,
        "initial_file_sha256": initial_sha,
        "initial_report_sha_matches": initial_sha == report.get("pi_init_sha256") == training_status.get("pi_init_sha256"),
        "initial_model_digest": initial_digest,
        "initial_report_digest_matches": initial_digest == report.get("pi_init_model_digest") == training_status.get("pi_init_model_digest"),
        "final_file_sha256": final_sha,
        "final_report_sha_matches": final_sha == report.get("pi_final_sha256") == training_status.get("pi_final_sha256"),
        "final_model_digest": final_digest,
        "final_report_digest_matches": final_digest == report.get("pi_final_model_digest") == training_status.get("pi_final_model_digest"),
        "four_blocks": blocks,
        "final_equals_block04": final_digest == blocks[-1]["model_digest"],
        "passed": (
            initial_sha == report.get("pi_init_sha256") == training_status.get("pi_init_sha256")
            and initial_digest == report.get("pi_init_model_digest") == training_status.get("pi_init_model_digest")
            and final_sha == report.get("pi_final_sha256") == training_status.get("pi_final_sha256")
            and final_digest == report.get("pi_final_model_digest") == training_status.get("pi_final_model_digest")
            and final_digest == blocks[-1]["model_digest"]
            and all(
                b["checkpoint_file_sha_matches"] and b["config_sha256_matches"]
                and b["study_id_matches"] and b["one_optimizer_step_per_block"] is True
                and b["block_state_matches"] and b["block_start_digest_matches"]
                and b["model_digest_matches"] and b["checkpoint_metrics_match_update"]
                for b in blocks
            )
        ),
    }


def identity_phase() -> dict[str, Any]:
    config_bytes = CONFIG_PATH.read_bytes()
    config_sha = sha256_bytes(config_bytes)
    config = json.loads(config_bytes)
    lock_text = (STUDY / "config.lock.sha256").read_text(encoding="ascii").strip().lower()
    env = load_json(ART / "environment.json")
    sys.path.insert(0, str(NATIVE_MODULE_DIR))
    module = importlib.import_module("slaythespire")
    module_path = Path(module.__file__).resolve()
    module_sha = sha256_file(module_path)
    source_module = importlib.import_module("sts_combat_rl.sim.lightspeed_source")
    current_source_identity = source_module.lightspeed_source_identity_dict()
    native_head = git(NATIVE_ROOT, "rev-parse", "HEAD")
    native_tree = git(NATIVE_ROOT, "rev-parse", "HEAD^{tree}")
    native_origin_branch = git(NATIVE_ROOT, "rev-parse", "origin/stsrl/main")
    native_clean = git(NATIVE_ROOT, "status", "--porcelain") == ""
    stsr_head = git(STSRL_ROOT, "rev-parse", "HEAD")
    stsr_tree = git(STSRL_ROOT, "rev-parse", "HEAD^{tree}")
    stsr_origin_main = git(STSRL_ROOT, "rev-parse", "origin/main")
    stsr_clean = git(STSRL_ROOT, "status", "--porcelain") == ""
    source_manifest = load_json(STSRL_ROOT / "docs" / "sts_lightspeed_source_manifest.json")
    fixture = load_json(STUDY / "native-fixture-evidence.json")
    reproduction = load_json(STUDY / "native-fixture-reproduction.json")
    manifest_check = file_inventory_and_manifest()

    expected_train = {
        f"TRAIN_{seed}"
        for seed in range(int(config["training"]["seed_first"]), int(config["training"]["seed_last"]) + 1)
    }
    eval_seeds = range(int(config["evaluation"]["seed_first"]), int(config["evaluation"]["seed_last"]) + 1)
    expected_eval = {f"EVAL_{seed}_{arm}" for seed in eval_seeds for arm in ("P0", "P1", "E")}
    run_paths = sorted((ART / "runs").glob("*.json"))
    run_ids = {p.stem for p in run_paths}
    missing_required_runs = sorted((expected_train | expected_eval) - run_ids)
    training_rows = [validate_decision_run(ART / "runs" / f"{rid}.json", require_native=True) for rid in sorted(expected_train & run_ids)]
    eval_model_rows = [
        validate_decision_run(ART / "runs" / f"EVAL_{seed}_{arm}.json", require_native=True)
        for seed in eval_seeds for arm in ("P0", "P1")
        if f"EVAL_{seed}_{arm}" in run_ids
    ]
    eval_expert_rows = [
        validate_decision_run(ART / "runs" / f"EVAL_{seed}_E.json", require_native=True)
        for seed in eval_seeds if f"EVAL_{seed}_E" in run_ids
    ]
    decision_errors = [row for row in training_rows + eval_model_rows + eval_expert_rows if row["errors"]]
    checkpoint_check = checkpoint_identity_gate()
    phase_failure_reasons = []
    checks = {
        "config_lock_matches": lock_text == config_sha,
        "study_id_matches": config.get("study_id") == "issue-71-all-noncombat-rl-successor",
        "spire_source_sha_is_issue71_pin": EXPECTED_SPIRE_SHA == "8d1b04527777c8602b4b5238b29d4a776723ae63",
        "python_exact": sys.version_info[:3] == (3, 13, 13) and PYTHON.exists(),
        "torch_exact_cpu": torch.__version__ == "2.12.0+cpu" and not torch.cuda.is_available(),
        "numpy_exact": np.__version__ == "2.4.6",
        "native_module_path_matches_receipt": str(module_path) == env.get("native_module_path"),
        "native_module_sha_matches_receipt_and_config": module_sha == env.get("native_module_sha256") == EXPECTED_MODULE_SHA == config.get("identity", {}).get("native_binary_sha256"),
        "native_head_exact": native_head == EXPECTED_NATIVE_SHA,
        "native_origin_stsrl_main_exact": native_origin_branch == EXPECTED_NATIVE_SHA,
        "native_source_worktree_clean": native_clean,
        "stsr_source_head_exact": stsr_head == EXPECTED_STSRL_SHA,
        "stsr_origin_main_exact": stsr_origin_main == EXPECTED_STSRL_SHA,
        "stsr_source_worktree_clean": stsr_clean,
        "source_manifest_integration_commit_exact": source_manifest.get("integration", {}).get("commit") == EXPECTED_NATIVE_INTEGRATION_SHA,
        "runtime_identity_matches_environment": current_source_identity == env.get("lightspeed_source_identity"),
        "config_native_and_stsr_locks_match": config.get("identity", {}).get("native_commit") == EXPECTED_NATIVE_SHA and config.get("identity", {}).get("stsr_commit") == EXPECTED_STSRL_SHA and config.get("identity", {}).get("stsr_native_integration_commit") == EXPECTED_NATIVE_INTEGRATION_SHA,
        "native_fixture_passed_exact_source": fixture.get("passed") is True and fixture.get("native_source_commit") == EXPECTED_NATIVE_SHA and fixture.get("native_module_sha256") == EXPECTED_MODULE_SHA,
        "native_fixture_source_checks_passed": fixture.get("checks", {}).get("native_source_and_submodule_identity", {}).get("native_commit_exact") is True and fixture.get("checks", {}).get("native_source_and_submodule_identity", {}).get("native_tree_exact") is True,
        "formal_training_or_evaluation_not_run_by_fixture": fixture.get("training_or_evaluation_ran") is False,
        "native_reproduction_does_not_launch_training_or_evaluation": reproduction.get("training_or_evaluation_launched") is False and reproduction.get("formal_preflight_launched") is False,
        "stage1_artifact_manifest_all_files_match": manifest_check["passed"],
        "required_train_and_heldout_runs_present": not missing_required_runs,
        "train_count_exact": len(training_rows) == 512,
        "heldout_P0_P1_count_exact": len(eval_model_rows) == 192,
        "heldout_E_count_exact": len(eval_expert_rows) == 96,
        "decision_npz_identity_joins_pass": not decision_errors,
        "checkpoint_identity_chain_passes": checkpoint_check["passed"],
    }
    for name, passed in checks.items():
        if not passed:
            phase_failure_reasons.append(name)
    return {
        "phase": "identity",
        "status": "PASS" if not phase_failure_reasons else "SOURCE_INVALID",
        "failed_checks": phase_failure_reasons,
        "checks": checks,
        "source": {
            "spire_commit": EXPECTED_SPIRE_SHA,
            "spire_remote_branch": "origin/codex/issue-71-heldout-20261022",
            "native_commit": native_head,
            "native_tree": native_tree,
            "native_origin_stsrl_main": native_origin_branch,
            "native_module_path": str(module_path),
            "native_module_sha256": module_sha,
            "stsr_source_commit": stsr_head,
            "stsr_tree": stsr_tree,
            "stsr_origin_main": stsr_origin_main,
            "stsr_native_integration_commit": source_manifest.get("integration", {}).get("commit"),
            "source_manifest_schema": source_manifest.get("schema_id"),
            "runtime": {"python": sys.version, "torch": torch.__version__, "numpy": np.__version__, "cuda": torch.cuda.is_available()},
            "recorded_runtime": env,
        },
        "study_identity": {
            "config_sha256": config_sha,
            "config_lock": lock_text,
            "artifact_manifest": manifest_check,
            "checkpoint_chain": checkpoint_check,
        },
        "receipt_population": {
            "stage1_run_json_count": len(run_paths),
            "stage1_decision_npz_count": len(list((ART / "decisions").glob("*.npz"))),
            "required_training_runs": len(expected_train),
            "present_training_runs": len(training_rows),
            "required_heldout_P0_P1_runs": 192,
            "present_heldout_P0_P1_runs": len(eval_model_rows),
            "required_heldout_E_runs": 96,
            "present_heldout_E_runs": len(eval_expert_rows),
            "missing_required_run_ids": missing_required_runs[:30],
            "decision_identity_join_error_count": sum(len(row["errors"]) for row in decision_errors),
            "decision_join_error_samples": decision_errors[:5],
        },
        "preexisting_native_fixture_limitations": fixture.get("limitations", []),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def quantile_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p90": None, "max": None}
    a = np.asarray(values, dtype=np.float64)
    return {
        "n": int(a.size),
        "mean": float(a.mean()),
        "median": float(np.median(a)),
        "p90": float(np.quantile(a, 0.9)),
        "max": float(a.max()),
    }


def grouped_summary(rows: list[dict[str, Any]], metrics: list[str]) -> dict[str, Any]:
    output: dict[str, Any] = {"n_decisions": len(rows), "n_owners": len({r["owner"] for r in rows})}
    for metric in metrics:
        values = [float(r[metric]) for r in rows]
        by_owner: dict[int, list[float]] = defaultdict(list)
        for row in rows:
            by_owner[int(row["owner"])].append(float(row[metric]))
        owner_means = [float(np.mean(v)) for v in by_owner.values()]
        row_summary = quantile_summary(values)
        owner_summary = quantile_summary(owner_means)
        owner_summary["sd"] = float(np.std(owner_means, ddof=1)) if len(owner_means) > 1 else 0.0
        output[metric] = {"decision_level": row_summary, "owner_mean_level": owner_summary}
    return output


def count_stratum(count: int) -> str:
    if count == 2:
        return "2"
    if count == 3:
        return "3"
    if count <= 5:
        return "4-5"
    if count <= 9:
        return "6-9"
    return "10+"


def distribution(model: torch.nn.Module, x: torch.Tensor) -> tuple[np.ndarray, np.ndarray]:
    with torch.inference_mode():
        logits = model.actor(x).squeeze(-1)
        probs = torch.softmax(logits, dim=0)
    return logits.detach().cpu().numpy().astype(np.float64), probs.detach().cpu().numpy().astype(np.float64)


def policy_phase() -> dict[str, Any]:
    torch.set_num_threads(1)
    initial, initial_digest = runner._load_policy(ART / "policies" / "pi_init.pt")
    final, final_digest = runner._load_policy(ART / "policies" / "pi_final.pt")
    model_specs: list[tuple[str, runner.AllNoncombatPolicy]] = [("P0", initial)]
    for block in range(1, 5):
        state = torch.load(ART / "checkpoints" / f"block_{block:02d}.pt", map_location="cpu", weights_only=True)
        model = runner.AllNoncombatPolicy(
            dimension=runner.CONFIG["model"]["hash_dimension"],
            seed=runner.CONFIG["model"]["torch_seed"],
        )
        model.load_state_dict(state["model_state"])
        model.eval()
        model_specs.append((f"block_{block:02d}", model))
    model_specs.append(("P1", final))
    model_map = dict(model_specs)
    if initial_digest != runner._load_policy(ART / "policies" / "pi_init.pt")[1] or final_digest != runner._load_policy(ART / "policies" / "pi_final.pt")[1]:
        raise RuntimeError("model reload digest instability")

    pair_metrics = ["tv_vs_P0", "kl_P0_to_model", "max_abs_delta_probability", "max_abs_centered_logit_change", "margin_change", "argmax_flip", "all_equal_vectors", "pair_collision_fraction"]
    model_metrics = ["entropy", "normalized_entropy", "tv_from_uniform", "margin"]
    actual_metrics = ["chosen_probability", "argmax_matches_logged_choice", "entropy"]
    grouped_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    grouped_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    grouped_actual: dict[str, list[dict[str, Any]]] = defaultdict(list)
    owner_trajectory: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    unique_row_checks = 0
    stored_probability_error_max = 0.0
    collision_examples: list[dict[str, Any]] = []

    heldout = range(int(runner.CONFIG["evaluation"]["seed_first"]), int(runner.CONFIG["evaluation"]["seed_last"]) + 1)
    for seed in heldout:
        for arm, own_model_label in (("P0", "P0"), ("P1", "P1")):
            run = load_json(ART / "runs" / f"EVAL_{seed}_{arm}.json")
            npz_path = ART / "decisions" / f"EVAL_{seed}_{arm}.npz"
            with np.load(npz_path, allow_pickle=False) as data:
                features, offsets, choices = data["features"], data["offsets"], data["choices"]
                categorical = [d for d in run.get("decisions", []) if d.get("kind") == "categorical"]
                for row_index, decision in enumerate(categorical):
                    left, right = int(offsets[row_index]), int(offsets[row_index + 1])
                    matrix = features[left:right]
                    if len(matrix) < 2:
                        raise RuntimeError(f"non-multiaction heldout row: {run['run_id']} {row_index}")
                    x = torch.as_tensor(matrix, dtype=torch.float32)
                    vectors: dict[str, tuple[np.ndarray, np.ndarray]] = {}
                    for label, model in model_specs:
                        vectors[label] = distribution(model, x)
                    base_logits, p0 = vectors["P0"]
                    final_logits, p1 = vectors["P1"]
                    recorded = np.asarray(decision.get("probabilities", []), dtype=np.float64)
                    own_p = vectors[own_model_label][1]
                    if len(recorded) != len(own_p):
                        raise RuntimeError(f"stored probability length mismatch: {run['run_id']} row {row_index}")
                    prob_error = float(np.max(np.abs(recorded - own_p)))
                    stored_probability_error_max = max(stored_probability_error_max, prob_error)
                    unique_row_checks += 1

                    identities = decision.get("legal_action_identities", [])
                    kinds = sorted({str(item.get("kind", "unknown")) for item in identities})
                    action_type = "+".join(kinds) if kinds else "unknown"
                    screen = str(decision.get("native_screen") or decision.get("screen") or "UNKNOWN")
                    strata = count_stratum(len(matrix))
                    key = "|".join((arm, screen, action_type, strata))
                    equal_pairs = 0
                    total_pairs = len(matrix) * (len(matrix) - 1) // 2
                    duplicate_pairs: list[tuple[int, int]] = []
                    for i in range(len(matrix)):
                        for j in range(i + 1, len(matrix)):
                            if np.array_equal(matrix[i], matrix[j]):
                                equal_pairs += 1
                                duplicate_pairs.append((i, j))
                    all_equal = float(equal_pairs == total_pairs)
                    collision_fraction = float(equal_pairs / total_pairs) if total_pairs else 0.0
                    if duplicate_pairs and len(collision_examples) < 25:
                        for i, j in duplicate_pairs:
                            if len(collision_examples) >= 25:
                                break
                            collision_examples.append({
                                "owner": seed, "input_arm": arm, "screen": screen,
                                "action_type": action_type, "row": row_index,
                                "candidate_i": identities[i] if i < len(identities) else None,
                                "candidate_j": identities[j] if j < len(identities) else None,
                                "exact_feature_vector_collision": True,
                            })

                    for label, (logits, probs) in vectors.items():
                        entropy = float(-(probs * np.log(np.maximum(probs, 1e-300))).sum())
                        centered = logits - logits.mean()
                        sorted_logits = np.sort(logits)
                        margin = float(sorted_logits[-1] - sorted_logits[-2])
                        uniform_tv = float(0.5 * np.abs(probs - 1.0 / len(probs)).sum())
                        grouped_model[f"{key}|{label}"].append({
                            "owner": seed, "entropy": entropy,
                            "normalized_entropy": entropy / math.log(len(probs)),
                            "tv_from_uniform": uniform_tv,
                            "margin": margin,
                        })
                        base_centered = base_logits - base_logits.mean()
                        centered_delta = centered - base_centered
                        kl = float(np.sum(p0 * (np.log(np.maximum(p0, 1e-300)) - np.log(np.maximum(probs, 1e-300)))))
                        grouped_pair[f"{key}|{label}"].append({
                            "owner": seed,
                            "tv_vs_P0": float(0.5 * np.abs(p0 - probs).sum()),
                            "kl_P0_to_model": kl,
                            "max_abs_delta_probability": float(np.max(np.abs(p0 - probs))),
                            "max_abs_centered_logit_change": float(np.max(np.abs(centered_delta))),
                            "margin_change": margin - float(np.sort(base_logits)[-1] - np.sort(base_logits)[-2]),
                            "argmax_flip": float(int(np.argmax(p0) != int(np.argmax(probs)))),
                            "all_equal_vectors": all_equal,
                            "pair_collision_fraction": collision_fraction,
                        })

                    chosen = int(choices[row_index])
                    own_logits, own_probs = vectors[own_model_label]
                    own_entropy = float(-(own_probs * np.log(np.maximum(own_probs, 1e-300))).sum())
                    chosen_probability = float(own_probs[chosen])
                    argmax_match = float(int(np.argmax(own_probs)) == chosen)
                    grouped_actual[f"{arm}|{screen}|{action_type}|{strata}"].append({
                        "owner": seed, "chosen_probability": chosen_probability,
                        "argmax_matches_logged_choice": argmax_match,
                        "entropy": own_entropy,
                    })
                    owner_trajectory[seed][f"{arm}_chosen_probability"].append(chosen_probability)
                    owner_trajectory[seed][f"{arm}_entropy"].append(own_entropy)
                    owner_trajectory[seed][f"{arm}_uniform_tv"].append(float(0.5 * np.abs(own_probs - 1.0 / len(own_probs)).sum()))

    def summarize_map(source: dict[str, list[dict[str, Any]]], metrics: list[str]) -> dict[str, Any]:
        return {key: grouped_summary(rows, metrics) for key, rows in sorted(source.items())}

    state_deltas: dict[str, Any] = {}
    state_a = initial.state_dict()
    for label, model in model_specs[1:]:
        layer_values = []
        actor_l2 = critic_l2 = actor_start_l2 = critic_start_l2 = 0.0
        for name, tensor in model.state_dict().items():
            delta = tensor.detach().cpu().to(torch.float64) - state_a[name].detach().cpu().to(torch.float64)
            start = state_a[name].detach().cpu().to(torch.float64)
            entry = {
                "parameter": name,
                "initial_l2": float(torch.linalg.vector_norm(start).item()),
                "delta_l2": float(torch.linalg.vector_norm(delta).item()),
                "relative_l2": float((torch.linalg.vector_norm(delta) / torch.linalg.vector_norm(start).clamp_min(1e-30)).item()),
                "max_abs_delta": float(torch.max(torch.abs(delta)).item()),
            }
            layer_values.append(entry)
            if name.startswith("actor."):
                actor_l2 += entry["delta_l2"] ** 2
                actor_start_l2 += entry["initial_l2"] ** 2
            elif name.startswith("critic."):
                critic_l2 += entry["delta_l2"] ** 2
                critic_start_l2 += entry["initial_l2"] ** 2
        state_deltas[label] = {
            "layers": layer_values,
            "actor_total_delta_l2": math.sqrt(actor_l2),
            "actor_relative_l2": math.sqrt(actor_l2) / max(math.sqrt(actor_start_l2), 1e-30),
            "critic_total_delta_l2": math.sqrt(critic_l2),
            "critic_relative_l2": math.sqrt(critic_l2) / max(math.sqrt(critic_start_l2), 1e-30),
        }
    init_head = initial.actor[-1].weight.detach().cpu().numpy().astype(np.float64)
    final_head = final.actor[-1].weight.detach().cpu().numpy().astype(np.float64)
    final_state = final.state_dict()
    block4 = model_map["block_04"].state_dict()
    max_block4_final_difference = max(
        float(torch.max(torch.abs(final_state[k].cpu() - block4[k].cpu())).item())
        for k in final_state
    )

    # SemanticHashEncoder._add uses low digest bits for both bucket and sign.
    dimension = int(runner.DEFAULT_HASH_DIMENSION)
    parity_sign_counts = Counter(1.0 if (bucket & 1) else -1.0 for bucket in range(dimension))
    return {
        "phase": "policy",
        "status": "PASS" if stored_probability_error_max <= 1e-6 and max_block4_final_difference == 0.0 else "MISMATCH",
        "model_states_evaluated_on_every_P0_and_P1_heldout_matrix": [name for name, _ in model_specs],
        "model_aliases": {"P0_equals_pi_init": initial_digest, "P1_equals_block_04": final_digest},
        "block04_final_max_abs_difference": max_block4_final_difference,
        "heldout_owner_count": 96,
        "heldout_input_arms": ["P0", "P1"],
        "heldout_decision_matrix_count": unique_row_checks,
        "stored_run_probability_max_abs_reproduction_error": stored_probability_error_max,
        "state_parameter_motion": state_deltas,
        "actor_output_head": {
            "initial_mean": float(init_head.mean()),
            "initial_empirical_std": float(init_head.std()),
            "initial_expected_init_std_from_policy_py": 0.0001,
            "final_empirical_std": float(final_head.std()),
            "max_abs_weight_change": float(np.max(np.abs(final_head - init_head))),
            "input_output_scale_ratio_final_to_initial_std": float(final_head.std() / max(init_head.std(), 1e-30)),
        },
        "pair_metrics_by_input_trajectory_screen_action_type_and_action_count": summarize_map(grouped_pair, pair_metrics),
        "distribution_metrics_by_input_trajectory_screen_action_type_and_action_count": summarize_map(grouped_model, model_metrics),
        "actual_trajectory_logged_choice_metrics_by_arm_screen_type_action_count": summarize_map(grouped_actual, actual_metrics),
        "owner_trajectory_summary": {
            key: quantile_summary([float(np.mean(value[key])) for value in owner_trajectory.values() if value.get(key)])
            for key in sorted({metric for row in owner_trajectory.values() for metric in row})
        },
        "encoded_feature_collision": {
            "rows_all_equal_fraction_by_group_and_owner": "included in pair_metrics_by_input_trajectory_screen_action_type_and_action_count",
            "exact_duplicate_pair_examples_first_25": collision_examples,
            "raw_projection_token_payload_available_for_rehash": False,
            "interpretation_limit": "The accepted NPZ retains encoded candidate vectors, while run JSON retains projection digests and legal-action identities but not raw per-candidate projection payloads. Exact encoded-vector aliases are observable; source-token collision pairs cannot be reconstructed for every historical row.",
        },
        "semantic_hash_audit": {
            "dimension": dimension,
            "dimension_is_power_of_two": dimension > 0 and dimension & (dimension - 1) == 0,
            "index_source": "int.from_bytes(blake2b(token, digest_size=8), little) modulo dimension",
            "sign_source": "digest[0] bit 0",
            "low_bits_used_by_bucket_at_dimension_2048": 11,
            "sign_is_perfectly_determined_by_bucket_parity": dimension == 2048,
            "bucket_sign_counts": {str(k): v for k, v in parity_sign_counts.items()},
            "consequence": "Because dimension 2048 uses the low 11 bits of the little-endian integer for the bucket, digest byte 0 bit 0 is also the bucket parity bit. Every even bucket is forced negative and every odd bucket forced positive; sign is not independent of bucket as intended by ordinary signed feature hashing.",
            "source_file": "studies/issue-71-all-noncombat-rl-successor/policy.py",
        },
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def collision_phase() -> dict[str, Any]:
    heldout = range(int(runner.CONFIG["evaluation"]["seed_first"]), int(runner.CONFIG["evaluation"]["seed_last"]) + 1)
    grouped: dict[str, dict[str, Any]] = {}
    total_rows = total_vectors = total_candidate_pairs = duplicate_pairs = 0
    rows_with_alias = all_equal_rows = 0
    examples: list[dict[str, Any]] = []
    owners_by_arm: dict[str, set[int]] = defaultdict(set)
    for seed in heldout:
        for arm in ("P0", "P1"):
            run = load_json(ART / "runs" / f"EVAL_{seed}_{arm}.json")
            with np.load(ART / "decisions" / f"EVAL_{seed}_{arm}.npz", allow_pickle=False) as data:
                features, offsets = data["features"], data["offsets"]
                categorical = [d for d in run.get("decisions", []) if d.get("kind") == "categorical"]
                if len(categorical) + 1 != len(offsets):
                    raise RuntimeError(f"categorical decision/feature row mismatch: {run['run_id']}")
                owners_by_arm[arm].add(seed)
                for row_index, decision in enumerate(categorical):
                    left, right = int(offsets[row_index]), int(offsets[row_index + 1])
                    matrix = features[left:right]
                    identities = decision.get("legal_action_identities", [])
                    if len(identities) != len(matrix) or len(matrix) < 2:
                        raise RuntimeError(f"invalid alias audit input: {run['run_id']} row {row_index}")
                    kinds = sorted({str(item.get("kind", "unknown")) for item in identities})
                    screen = str(decision.get("native_screen") or decision.get("screen") or "UNKNOWN")
                    action_type = "+".join(kinds) if kinds else "unknown"
                    count_stratum_key = count_stratum(len(matrix))
                    key = "|".join((arm, screen, action_type, count_stratum_key))
                    group = grouped.setdefault(key, {
                        "input_arm": arm, "screen": screen, "action_type": action_type,
                        "action_count_stratum": count_stratum_key, "n_rows": 0,
                        "owner_ids": set(), "candidate_vectors": 0,
                        "candidate_pairs": 0, "duplicate_vector_pairs": 0,
                        "rows_with_duplicate_vectors": 0, "all_equal_rows": 0,
                    })
                    group["n_rows"] += 1
                    group["owner_ids"].add(seed)
                    group["candidate_vectors"] += len(matrix)
                    group_pairs = len(matrix) * (len(matrix) - 1) // 2
                    group["candidate_pairs"] += group_pairs
                    equal_pairs_here: list[tuple[int, int]] = []
                    for left_index in range(len(matrix)):
                        for right_index in range(left_index + 1, len(matrix)):
                            if np.array_equal(matrix[left_index], matrix[right_index]):
                                equal_pairs_here.append((left_index, right_index))
                    equal_count = len(equal_pairs_here)
                    group["duplicate_vector_pairs"] += equal_count
                    group["rows_with_duplicate_vectors"] += int(equal_count > 0)
                    all_equal = equal_count == group_pairs
                    group["all_equal_rows"] += int(all_equal)
                    total_rows += 1
                    total_vectors += len(matrix)
                    total_candidate_pairs += group_pairs
                    duplicate_pairs += equal_count
                    rows_with_alias += int(equal_count > 0)
                    all_equal_rows += int(all_equal)
                    for candidate_i, candidate_j in equal_pairs_here:
                        if len(examples) >= 50:
                            break
                        examples.append({
                            "owner": seed, "input_arm": arm, "screen": screen,
                            "row": row_index, "action_type": action_type,
                            "candidate_i": identities[candidate_i],
                            "candidate_j": identities[candidate_j],
                            "feature_vector_sha256": sha256_bytes(np.ascontiguousarray(matrix[candidate_i]).tobytes()),
                            "exact_encoded_vector_alias": True,
                        })
    grouped_output = {}
    for key, group in sorted(grouped.items()):
        group["n_owners"] = len(group.pop("owner_ids"))
        group["duplicate_vector_pair_fraction"] = group["duplicate_vector_pairs"] / group["candidate_pairs"] if group["candidate_pairs"] else None
        group["all_equal_row_fraction"] = group["all_equal_rows"] / group["n_rows"] if group["n_rows"] else None
        group["rows_with_duplicate_vector_fraction"] = group["rows_with_duplicate_vectors"] / group["n_rows"] if group["n_rows"] else None
        grouped_output[key] = group
    return {
        "phase": "heldout_encoded_action_aliases",
        "status": "PASS" if total_rows == 4181 and all_equal_rows == 0 else "MISMATCH",
        "input_population": {"owners_per_arm": {arm: len(ids) for arm, ids in sorted(owners_by_arm.items())}, "arms": ["P0", "P1"]},
        "totals": {
            "decision_matrices": total_rows,
            "candidate_vectors": total_vectors,
            "within_matrix_candidate_pairs": total_candidate_pairs,
            "exact_duplicate_vector_pairs": duplicate_pairs,
            "decision_matrices_with_at_least_one_exact_duplicate_pair": rows_with_alias,
            "all_candidates_equal_vector_matrices": all_equal_rows,
            "all_equal_vector_matrix_fraction": all_equal_rows / total_rows if total_rows else None,
            "duplicate_pair_fraction_of_all_within_matrix_pairs": duplicate_pairs / total_candidate_pairs if total_candidate_pairs else None,
            "rows_with_duplicate_pair_fraction": rows_with_alias / total_rows if total_rows else None,
        },
        "by_input_arm_screen_action_type_and_action_count": grouped_output,
        "first_50_exact_alias_examples": examples,
        "identity_limit": "Stage1 retained encoded float32 candidate matrices and legal-action identities, but not raw per-candidate projection payloads. The exact encoded aliases are confirmed; whether a pair had genuinely identical source semantics or collided after semantic hashing is nonidentifiable from the retained artifacts.",
        "actor_consequence": "Identical candidate feature vectors passed to the deterministic shared actor receive identical scores; no candidate ID or legal-action index is encoded as model semantics.",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def scalar_stats(values: Iterable[float]) -> dict[str, Any]:
    a = np.asarray(list(values), dtype=np.float64)
    if a.size == 0:
        return {"n": 0, "mean": None, "sd": None, "min": None, "max": None, "p10": None, "p50": None, "p90": None}
    return {
        "n": int(a.size), "mean": float(a.mean()),
        "sd": float(a.std(ddof=1)) if a.size > 1 else 0.0,
        "min": float(a.min()), "max": float(a.max()),
        "p10": float(np.quantile(a, 0.1)), "p50": float(np.median(a)), "p90": float(np.quantile(a, 0.9)),
    }


def safe_corr(x: Iterable[float], y: Iterable[float]) -> float | None:
    xa = np.asarray(list(x), dtype=np.float64)
    ya = np.asarray(list(y), dtype=np.float64)
    if xa.size < 2 or ya.size != xa.size or np.std(xa) == 0 or np.std(ya) == 0:
        return None
    return float(np.corrcoef(xa, ya)[0, 1])


def tensor_list_norm(values: tuple[torch.Tensor | None, ...]) -> float:
    parts = [v.detach().reshape(-1).to(torch.float64) for v in values if v is not None]
    if not parts:
        return 0.0
    return float(torch.linalg.vector_norm(torch.cat(parts)).item())


def tensor_tuple_zeros(values: tuple[torch.Tensor | None, ...], params: list[torch.nn.Parameter]) -> tuple[torch.Tensor, ...]:
    return tuple(torch.zeros_like(p) if value is None else value for value, p in zip(values, params, strict=True))


def dot_cosine(a: tuple[torch.Tensor, ...], b: tuple[torch.Tensor, ...]) -> float | None:
    af = torch.cat([x.detach().reshape(-1).to(torch.float64) for x in a])
    bf = torch.cat([x.detach().reshape(-1).to(torch.float64) for x in b])
    denom = torch.linalg.vector_norm(af) * torch.linalg.vector_norm(bf)
    if float(denom.item()) == 0.0:
        return None
    return float(torch.dot(af, bf).item() / denom.item())


def gradient_layer_norms(gradients: tuple[torch.Tensor, ...], named_params: list[tuple[str, torch.nn.Parameter]]) -> dict[str, float]:
    return {name: float(torch.linalg.vector_norm(grad.detach().to(torch.float64)).item()) for (name, _), grad in zip(named_params, gradients, strict=True)}


def compare_optimizer_states(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    max_abs = 0.0
    max_rel = 0.0
    tensor_count = 0
    scalar_mismatch = 0
    if actual.get("param_groups") != expected.get("param_groups"):
        errors.append("parameter groups differ")
    astate, estate = actual.get("state", {}), expected.get("state", {})
    if set(astate) != set(estate):
        errors.append("optimizer parameter-state IDs differ")
    for pid in set(astate) & set(estate):
        if set(astate[pid]) != set(estate[pid]):
            errors.append(f"optimizer state keys differ for parameter {pid}")
            continue
        for key in astate[pid]:
            a, e = astate[pid][key], estate[pid][key]
            if torch.is_tensor(a) and torch.is_tensor(e):
                tensor_count += 1
                ad, ed = a.detach().cpu().to(torch.float64), e.detach().cpu().to(torch.float64)
                if ad.shape != ed.shape:
                    errors.append(f"optimizer tensor shape differs for {pid}/{key}")
                    continue
                diff = torch.abs(ad - ed)
                max_abs = max(max_abs, float(diff.max().item()) if diff.numel() else 0.0)
                rel = diff / torch.abs(ed).clamp_min(1e-30)
                max_rel = max(max_rel, float(rel.max().item()) if rel.numel() else 0.0)
            elif a != e:
                scalar_mismatch += 1
                errors.append(f"optimizer scalar differs for {pid}/{key}")
    return {
        "state_parameter_count": len(astate),
        "state_tensor_count": tensor_count,
        "max_abs_tensor_difference": max_abs,
        "max_relative_tensor_difference": max_rel,
        "scalar_mismatch_count": scalar_mismatch,
        "errors": errors,
        "passed_exact": not errors and max_abs == 0.0 and max_rel == 0.0,
    }


def loss_gradient_breakdown(model: runner.AllNoncombatPolicy, seeds: list[int], optimizer: torch.optim.Optimizer) -> dict[str, Any]:
    config = runner.CONFIG["training"]
    reward_by_owner: dict[int, float] = {}
    episodes: dict[int, dict[str, np.ndarray]] = {}
    branch_examples: list[tuple[int, int, int, float]] = []
    for seed in seeds:
        run = load_json(ART / "runs" / f"TRAIN_{seed}.json")
        reward_by_owner[seed] = float(run["terminal"]["reward"])
        with np.load(ART / "decisions" / f"TRAIN_{seed}.npz", allow_pickle=False) as data:
            episodes[seed] = {key: data[key].copy() for key in data.files}
        for branch_path in sorted((ART / "branches").glob(f"TRAIN_{seed}_ROOT_*_ALT_*.json")):
            branch = load_json(branch_path)
            if branch.get("superior_to_original_lexicographically") and branch.get("terminal_complete"):
                delta = max(0.0, float(branch["terminal"]["reward"]) - reward_by_owner[seed])
                weight = min(delta, float(config["branch_advantage_weight_cap"]))
                if weight > 0.0:
                    branch_examples.append((seed, int(branch["root_decision_index"]), int(branch["candidate_index"]), weight))
    baseline = float(np.mean(list(reward_by_owner.values())))
    advantages = {seed: reward - baseline for seed, reward in reward_by_owner.items()}
    optimizer.zero_grad(set_to_none=True)
    policy_terms: list[torch.Tensor] = []
    value_terms: list[torch.Tensor] = []
    entropy_terms: list[torch.Tensor] = []
    loss_magnitude_by_owner: dict[int, float] = {}
    lengths: dict[int, int] = {}
    for seed in seeds:
        data = episodes[seed]
        features, offsets, choices = data["features"], data["offsets"].astype(int), data["choices"].astype(int)
        lengths[seed] = len(choices)
        trajectory_log_probs: list[torch.Tensor] = []
        for decision_index, chosen in enumerate(choices):
            matrix = torch.as_tensor(features[offsets[decision_index]:offsets[decision_index + 1]], dtype=torch.float32)
            _, log_probs, value = model.distribution(matrix)
            trajectory_log_probs.append(log_probs[int(chosen)])
            value_terms.append((value - reward_by_owner[seed]).pow(2))
            probs = torch.softmax(model.actor(matrix).squeeze(-1), dim=0)
            entropy_terms.append(-(probs * torch.log(probs.clamp_min(1e-12))).sum())
        if trajectory_log_probs:
            episode_logprob = torch.stack(trajectory_log_probs).sum()
            policy_terms.append(-advantages[seed] * episode_logprob)
            loss_magnitude_by_owner[seed] = float(abs((-advantages[seed] * episode_logprob).detach().item()))
        else:
            loss_magnitude_by_owner[seed] = 0.0
    pg_loss = torch.stack(policy_terms).mean() if policy_terms else torch.zeros((), requires_grad=True)
    value_loss = torch.stack(value_terms).mean() if value_terms else torch.zeros((), requires_grad=True)
    entropy = torch.stack(entropy_terms).mean() if entropy_terms else torch.zeros((), requires_grad=True)
    branch_terms: list[torch.Tensor] = []
    branch_weights: list[float] = []
    for seed, decision_index, candidate_index, weight in branch_examples:
        data = episodes[seed]
        offsets = data["offsets"].astype(int)
        matrix = torch.as_tensor(data["features"][offsets[decision_index]:offsets[decision_index + 1]], dtype=torch.float32)
        _, log_probs, _ = model.distribution(matrix)
        branch_terms.append(-float(weight) * log_probs[candidate_index])
        branch_weights.append(weight)
    branch_loss = torch.stack(branch_terms).mean() if branch_terms else torch.zeros((), requires_grad=True)
    entropy_component = -float(config["entropy_coefficient"]) * entropy
    value_component = float(config["value_loss_coefficient"]) * value_loss
    total_loss = pg_loss + value_component + entropy_component + branch_loss

    actor_named = list(model.actor.named_parameters())
    actor_params = [p for _, p in actor_named]
    critic_named = list(model.critic.named_parameters())
    critic_params = [p for _, p in critic_named]
    g_pg_raw = torch.autograd.grad(pg_loss, actor_params, retain_graph=True, allow_unused=True)
    g_branch_raw = torch.autograd.grad(branch_loss, actor_params, retain_graph=True, allow_unused=True)
    g_entropy_raw = torch.autograd.grad(entropy_component, actor_params, retain_graph=True, allow_unused=True)
    g_value_actor_raw = torch.autograd.grad(value_component, actor_params, retain_graph=True, allow_unused=True)
    g_value_critic_raw = torch.autograd.grad(value_component, critic_params, retain_graph=True, allow_unused=True)
    g_total_actor_raw = torch.autograd.grad(total_loss, actor_params, retain_graph=True, allow_unused=True)
    g_policy_branch_critic_raw = torch.autograd.grad(
        pg_loss + branch_loss + entropy_component,
        critic_params,
        retain_graph=True,
        allow_unused=True,
    )
    g_total_critic_raw = torch.autograd.grad(total_loss, critic_params, allow_unused=True)
    g_pg = tensor_tuple_zeros(g_pg_raw, actor_params)
    g_branch = tensor_tuple_zeros(g_branch_raw, actor_params)
    g_entropy = tensor_tuple_zeros(g_entropy_raw, actor_params)
    g_value_actor = tensor_tuple_zeros(g_value_actor_raw, actor_params)
    g_value_critic = tensor_tuple_zeros(g_value_critic_raw, critic_params)
    g_total_actor = tensor_tuple_zeros(g_total_actor_raw, actor_params)
    g_total_critic = tensor_tuple_zeros(g_total_critic_raw, critic_params)
    actor_components = [g_pg, g_branch, g_entropy]
    sum_components = tuple(sum((component[i] for component in actor_components), torch.zeros_like(actor_params[i])) for i in range(len(actor_params)))
    sum_component_norm = sum(tensor_list_norm(component) for component in actor_components)
    total_actor_norm = tensor_list_norm(g_total_actor)
    actor_clip_factor = min(1.0, float(config["gradient_clip_norm"]) / max(total_actor_norm, 1e-300))
    critic_norm = tensor_list_norm(g_total_critic)
    critic_clip_factor = min(1.0, float(config["gradient_clip_norm"]) / max(critic_norm, 1e-300))
    component_names = ["policy_gradient", "branch_loss", "entropy_component"]
    component_grads = [g_pg, g_branch, g_entropy]
    cosines = {}
    for i, left in enumerate(component_names):
        for j in range(i + 1, len(component_names)):
            right = component_names[j]
            cosines[f"{left}_vs_{right}"] = dot_cosine(component_grads[i], component_grads[j])
    layers = {
        name: {
            "policy_gradient": gradient_layer_norms(g_pg, actor_named)[name],
            "branch_loss": gradient_layer_norms(g_branch, actor_named)[name],
            "entropy_component": gradient_layer_norms(g_entropy, actor_named)[name],
            "combined_actor": gradient_layer_norms(g_total_actor, actor_named)[name],
        }
        for name, _ in actor_named
    }
    adv = np.asarray([advantages[s] for s in seeds], dtype=np.float64)
    episode_length = np.asarray([lengths[s] for s in seeds], dtype=np.float64)
    abs_adv = np.abs(adv)
    loss_magnitudes = np.asarray([loss_magnitude_by_owner[s] for s in seeds], dtype=np.float64)
    return {
        "baseline": baseline,
        "owner_terminal_reward": scalar_stats(reward_by_owner.values()),
        "owner_advantage": scalar_stats(advantages.values()),
        "episode_decision_count": scalar_stats(lengths.values()),
        "episode_length_vs_absolute_advantage_pearson": safe_corr(episode_length, abs_adv),
        "episode_length_vs_absolute_episode_policy_term_pearson": safe_corr(episode_length, loss_magnitudes),
        "episode_policy_term_magnitude": scalar_stats(loss_magnitudes),
        "loss_terms": {
            "policy_gradient": float(pg_loss.detach().item()),
            "branch_loss": float(branch_loss.detach().item()),
            "value_loss_unscaled": float(value_loss.detach().item()),
            "value_component": float(value_component.detach().item()),
            "entropy_unscaled": float(entropy.detach().item()),
            "entropy_component": float(entropy_component.detach().item()),
            "total": float(total_loss.detach().item()),
            "branch_example_count": len(branch_examples),
            "branch_positive_weight_stats": scalar_stats(branch_weights),
            "policy_reduction": "mean over 128 owner terms; each owner term sums its chosen log-probabilities over all categorical decisions",
            "value_and_entropy_reduction": "mean over every categorical decision across the 128 owners",
            "branch_reduction": "mean over completed superior branch alternatives with positive clipped reward delta",
        },
        "actor_gradient_components": {
            "norms_before_clipping": {
                "policy_gradient": tensor_list_norm(g_pg),
                "branch_loss": tensor_list_norm(g_branch),
                "entropy_component": tensor_list_norm(g_entropy),
                "value_component_actor": tensor_list_norm(g_value_actor),
                "combined_actor": total_actor_norm,
            },
            "component_pair_cosines": cosines,
            "sum_of_component_norms": sum_component_norm,
            "norm_of_component_sum": tensor_list_norm(sum_components),
            "cancellation_ratio_norm_sum_over_sum_norms": total_actor_norm / max(sum_component_norm, 1e-300),
            "component_norm_share": {
                name: tensor_list_norm(grad) / max(sum_component_norm, 1e-300)
                for name, grad in zip(component_names, component_grads, strict=True)
            },
            "layerwise_actor_gradient_norms": layers,
        },
        "critic_gradient": {
            "value_component_gradient_norm_before_clipping": tensor_list_norm(g_value_critic),
            "combined_critic_gradient_norm_before_clipping": critic_norm,
            "critic_parameter_gradient_norms": gradient_layer_norms(g_total_critic, critic_named),
            "actor_value_gradient_norm": tensor_list_norm(g_value_actor),
            "actor_value_all_grads_unused": all(g is None for g in g_value_actor_raw),
            "policy_and_branch_critic_all_grads_unused": all(g is None for g in g_policy_branch_critic_raw),
        },
        "gradient_clipping": {
            "clip_norm": float(config["gradient_clip_norm"]),
            "actor_preclip_norm": total_actor_norm,
            "actor_scale_applied": actor_clip_factor,
            "actor_clipped": actor_clip_factor < 1.0,
            "critic_preclip_norm": critic_norm,
            "critic_scale_applied": critic_clip_factor,
            "critic_clipped": critic_clip_factor < 1.0,
            "critic_postclip_norm": critic_norm * critic_clip_factor,
        },
    }


def replay_phase() -> dict[str, Any]:
    torch.set_num_threads(1)
    training = runner.CONFIG["training"]
    model, initial_digest = runner._load_policy(ART / "policies" / "pi_init.pt")
    optimizer = torch.optim.Adam(
        model.parameters(), lr=float(training["learning_rate"]),
        betas=(0.9, 0.999), eps=1e-8, weight_decay=0.0, amsgrad=False,
    )
    blocks: list[dict[str, Any]] = []
    all_passed = True
    per_block = int(training["owners_per_block"])
    for block in range(1, int(training["block_count"]) + 1):
        if block > 1:
            runner._load_model_checkpoint(model, optimizer, ART / "checkpoints" / f"block_{block - 1:02d}.pt")
        first = int(training["seed_first"]) + (block - 1) * per_block
        seeds = list(range(first, first + per_block))
        start_digest = runner._model_digest(model)
        expected_metrics = load_json(ART / "updates" / f"block_{block:02d}.json")
        expected_checkpoint = torch.load(ART / "checkpoints" / f"block_{block:02d}.pt", map_location="cpu", weights_only=True)
        breakdown = loss_gradient_breakdown(model, seeds, optimizer)
        replayed_metrics = runner._training_update(
            out=ART, block=block, seeds=seeds, model=model,
            optimizer=optimizer, block_start_digest=start_digest,
        )
        checkpoint_model = expected_checkpoint["model_state"]
        model_diff_max = max(
            float(torch.max(torch.abs(model.state_dict()[k].cpu() - checkpoint_model[k].cpu())).item())
            for k in checkpoint_model
        )
        replay_model_digest = runner._model_digest(model)
        optimizer_check = compare_optimizer_states(optimizer.state_dict(), expected_checkpoint["optimizer_state"])
        numeric_keys = [
            "terminal_reward_baseline", "branch_loss_term", "policy_gradient_term",
            "value_loss_term", "entropy_mean", "total_loss",
            "actor_gradient_norm_before_clip", "critic_gradient_norm_before_clip",
        ]
        metric_deltas = {
            key: float(replayed_metrics[key]) - float(expected_metrics[key])
            for key in numeric_keys
        }
        metric_non_numeric_mismatches = [
            key for key in replayed_metrics
            if key in expected_metrics and not isinstance(replayed_metrics[key], (float, int))
            and replayed_metrics[key] != expected_metrics[key]
        ]
        metric_numeric_pass = all(abs(v) <= 1e-7 for v in metric_deltas.values())
        passed = (
            start_digest == expected_metrics["block_start_policy_digest"]
            and replay_model_digest == expected_metrics["block_end_policy_digest"] == expected_checkpoint["model_digest"]
            and model_diff_max == 0.0
            and optimizer_check["passed_exact"]
            and metric_numeric_pass
            and not metric_non_numeric_mismatches
        )
        all_passed = all_passed and passed
        blocks.append({
            "block": block,
            "seed_first": first,
            "seed_last": first + per_block - 1,
            "owner_count": len(seeds),
            "historical_optimizer_state_source": "empty Adam state before block 1; exact saved block_{n-1} optimizer_state for subsequent blocks",
            "block_start_digest": start_digest,
            "recorded_block_start_digest": expected_metrics["block_start_policy_digest"],
            "recorded_loss_metric_deltas": metric_deltas,
            "non_numeric_metric_mismatches": metric_non_numeric_mismatches,
            "replayed_model_digest": replay_model_digest,
            "recorded_model_digest": expected_metrics["block_end_policy_digest"],
            "replayed_vs_checkpoint_model_max_abs_difference": model_diff_max,
            "optimizer_state_reconstruction": optimizer_check,
            "gradient_and_loss_breakdown": breakdown,
            "passed": passed,
        })
    return {
        "phase": "historical_update_replay",
        "status": "PASS" if all_passed else "REPLAY_MISMATCH",
        "replay_count": len(blocks),
        "replay_scope": "exactly the four historical authorized Adam steps; one in-memory step for each original 128-owner block; no new formal data or checkpoint writes",
        "initial_digest": initial_digest,
        "blocks": blocks,
        "passed": all_passed,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def branch_phase() -> dict[str, Any]:
    config = runner.CONFIG
    continuation = config["continuations"]
    branch_paths = sorted((ART / "branches").glob("TRAIN_*.json"))
    status_counts: Counter[str] = Counter()
    by_root_count: Counter[tuple[int, int]] = Counter()
    by_owner_root_count: Counter[int] = Counter()
    root_cpu: dict[tuple[int, int], float] = defaultdict(float)
    by_block: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_screen: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_decision_depth: dict[int, list[dict[str, Any]]] = defaultdict(list)
    errors: list[str] = []
    seen_targets: set[tuple[int, int, int]] = set()
    seen_attempt_ids: set[str] = set()
    attempted_count = 0
    not_attempted_count = 0
    attempts_without_forced_root: Counter[str] = Counter()
    forced_root_consumed_count = 0
    actual_terminal_cpu = 0.0
    for path in branch_paths:
        row = load_json(path)
        seed = int(row.get("seed", -1))
        block = (seed - int(config["training"]["seed_first"])) // int(config["training"]["owners_per_block"]) + 1
        status = str(row.get("status"))
        status_counts[status] += 1
        root_index = int(row.get("root_index", -1))
        decision_index_value = row.get("root_decision_index")
        decision_index = None if decision_index_value is None else int(decision_index_value)
        candidate = int(row.get("candidate_index", -1))
        attempt_id = row.get("attempt_id")
        is_not_attempted = status.startswith("NOT_ATTEMPTED")
        child_value = row.get("child_result")
        child = child_value if isinstance(child_value, Mapping) else {}
        forced_value = row.get("forced_root_receipt")
        forced = forced_value if isinstance(forced_value, Mapping) else {}
        cpu = max(0.0, float(row.get("cpu_seconds_total", 0.0)))
        target = (seed, root_index, candidate)
        if target in seen_targets:
            errors.append(f"duplicate branch target {target}")
        seen_targets.add(target)
        by_root_count[(seed, root_index)] += 1
        by_owner_root_count[seed] = max(by_owner_root_count[seed], root_index + 1)
        root_cpu[(seed, root_index)] += cpu
        if is_not_attempted:
            not_attempted_count += 1
            if attempt_id is not None or cpu > 0.0 or child_value is not None or forced_value is not None:
                errors.append(f"not-attempted row has execution payload for {target}")
            if row.get("terminal_complete") or row.get("terminal") is not None:
                errors.append(f"not-attempted row has a terminal payload for {target}")
        else:
            attempted_count += 1
            if not attempt_id:
                errors.append(f"attempted branch row has no attempt_id for {target}")
            elif str(attempt_id) in seen_attempt_ids:
                errors.append(f"duplicate branch attempt_id {attempt_id}")
            else:
                seen_attempt_ids.add(str(attempt_id))
            if not isinstance(child_value, Mapping):
                errors.append(f"attempted branch row has no child receipt for {target}")
            if cpu <= 0.0:
                errors.append(f"attempted branch row has no positive process CPU receipt for {target}")
        run = load_json(ART / "runs" / f"TRAIN_{seed}.json")
        categorical = [d for d in run.get("decisions", []) if d.get("kind") == "categorical"]
        if not 0 <= root_index < min(2, len(categorical)):
            errors.append(f"root {seed}/{root_index} is not among its earliest two categorical decisions")
            decision = {}
        else:
            decision = categorical[root_index]
        if decision_index is not None and decision_index != root_index:
            errors.append(f"root decision/index mismatch for {target}: {decision_index} vs {root_index}")
        if not is_not_attempted and decision_index != root_index:
            errors.append(f"attempted root has no exact source decision index for {target}")
        identities = decision.get("legal_action_identities", [])
        chosen = int(decision.get("chosen_index", -1)) if decision else -1
        if row.get("original_root_candidate_index") is not None and int(row["original_root_candidate_index"]) != chosen:
            errors.append(f"original action identity does not join to TRAIN receipt for {target}")
        if not is_not_attempted and row.get("original_root_candidate_index") is None:
            errors.append(f"attempted root has no retained original action index for {target}")
        if not 0 <= candidate < len(identities) or candidate == chosen:
            errors.append(f"alternative candidate does not bind to a different legal action for {target}")
        probabilities = decision.get("probabilities", []) if decision else []
        if len(probabilities) != len(identities):
            errors.append(f"source legal-action/probability count mismatch for {target}")
        elif candidate not in runner._root_alternatives({"chosen_index": chosen, "probabilities": probabilities}):
            errors.append(f"alternative was not among the registered top-probability nonchosen actions for {target}")
        if not is_not_attempted:
            if row.get("native_source_commit") != EXPECTED_NATIVE_SHA or row.get("native_search_api") != config["battle_search"]["native_api"]:
                errors.append(f"native source/search identity mismatch for {target}")
            if row.get("policy_used_for_all_suffix_nonbattle_choices") is not True:
                errors.append(f"suffix did not use frozen policy for {target}")
            if float(row.get("root_aggregate_cpu_cap_seconds", -1)) != float(continuation["aggregate_process_cpu_seconds_per_root_max"]):
                errors.append(f"root cap receipt mismatch for {target}")
            if child.get("native_source_commit") != EXPECTED_NATIVE_SHA:
                errors.append(f"child native source identity mismatch for {target}")
            if child.get("candidate_index") is not None and int(child["candidate_index"]) != candidate:
                errors.append(f"child candidate index mismatch for {target}")
            if forced:
                forced_root_consumed_count += int(forced.get("forced_root_consumed") is True)
                expected_action_sha = sha256_bytes(runner.canonical(identities[candidate]).encode("utf-8")) if 0 <= candidate < len(identities) else None
                expected_legal_sha = sha256_bytes(runner.canonical(identities).encode("utf-8"))
                if (
                    forced.get("forced_root_consumed") is not True
                    or int(forced.get("candidate_index", -1)) != candidate
                    or forced.get("candidate_identity_sha256") != expected_action_sha
                    or forced.get("legal_action_identities_sha256") != expected_legal_sha
                    or child.get("forced_root_receipt") != forced_value
                ):
                    errors.append(f"forced action identity receipt mismatch for {target}")
            else:
                attempts_without_forced_root[status] += 1
                if status == "COMPLETED_NATIVE_TERMINAL":
                    errors.append(f"completed suffix lacks a consumed forced-root receipt for {target}")
        if row.get("terminal_complete"):
            if is_not_attempted:
                errors.append(f"not-attempted row marked terminal complete for {target}")
            actual_terminal_cpu += cpu
            original_reward = float(run["terminal"]["reward"])
            original_lex = tuple(run["terminal"]["lexicographic_fitness"])
            alt_reward = float(row["terminal"]["reward"])
            alt_lex = tuple(row["terminal"]["lexicographic_fitness"])
            superior = alt_lex > original_lex
            delta = alt_reward - original_reward
            expected_weight = min(max(delta, 0.0), float(config["training"]["branch_advantage_weight_cap"])) if superior else 0.0
            if bool(row.get("superior_to_original_lexicographically")) != superior:
                errors.append(f"superiority label mismatch for {target}")
            if abs(float(row.get("completed_superior_example_weight", 0.0)) - expected_weight) > 1e-9:
                errors.append(f"clipped positive label mismatch for {target}")
            if not child.get("terminal_complete") or child.get("terminal") != row.get("terminal"):
                errors.append(f"no complete terminal suffix result in child receipt for {target}")
            probabilities = decision.get("probabilities", []) if decision else []
            record = {
                "seed": seed, "block": block, "root_index": root_index,
                "decision_index": decision_index, "screen": str(row.get("root_screen")),
                "action_type": str(identities[candidate].get("kind", "unknown")) if 0 <= candidate < len(identities) else "unknown",
                "selected_candidate_probability": float(probabilities[candidate]) if 0 <= candidate < len(probabilities) else None,
                "reward_delta": delta, "positive_label_weight": expected_weight,
                "is_lexicographically_superior": superior,
                "cpu_seconds": cpu,
                "attempt_id": row.get("attempt_id"),
            }
            by_block[block].append(record)
            by_screen[record["screen"]].append(record)
            by_decision_depth[decision_index].append(record)
        elif row.get("terminal") is not None:
            errors.append(f"incomplete branch has terminal payload for {target}")
        if not is_not_attempted and child.get("terminal_complete") != bool(row.get("terminal_complete")):
            errors.append(f"row/child terminal status mismatch for {target}")
    root_max = max(root_cpu.values(), default=0.0)
    root_exceeds = [
        {"seed": seed, "root_index": root, "cpu_seconds": cpu}
        for (seed, root), cpu in root_cpu.items()
        if cpu > float(continuation["aggregate_process_cpu_seconds_per_root_max"]) + 1e-6
    ]
    if root_exceeds:
        errors.append("one or more root CPU totals exceed the registered cap")
    if any(count > int(continuation["alternatives_per_root_max"]) for count in by_root_count.values()):
        errors.append("more alternatives than the registered per-root maximum")
    if any(count > int(continuation["roots_per_owner_max"]) for count in by_owner_root_count.values()):
        errors.append("more roots than the registered per-owner maximum")

    block_summaries = {}
    for block in range(1, 5):
        rows = by_block.get(block, [])
        deltas = [float(r["reward_delta"]) for r in rows]
        count_signs = {
            "negative": sum(x < 0 for x in deltas),
            "zero": sum(x == 0 for x in deltas),
            "positive": sum(x > 0 for x in deltas),
        }
        update = load_json(ART / "updates" / f"block_{block:02d}.json")
        block_summaries[str(block)] = {
            "seed_first": int(config["training"]["seed_first"]) + (block - 1) * int(config["training"]["owners_per_block"]),
            "seed_last": int(config["training"]["seed_first"]) + block * int(config["training"]["owners_per_block"]) - 1,
            "terminal_suffix_count": len(rows),
            "signed_reward_delta_counts": count_signs,
            "signed_reward_delta_shares_of_completed_suffixes": {k: (v / len(rows) if rows else None) for k, v in count_signs.items()},
            "signed_reward_delta_stats": scalar_stats(deltas),
            "positive_weight_label_count": sum(float(r["positive_label_weight"]) > 0 for r in rows),
            "positive_weight_sum": float(sum(float(r["positive_label_weight"]) for r in rows)),
            "training_update_branch_examples_completed_superior": update.get("branch_examples_completed_superior"),
            "loss_reconstruction_label_count_matches": sum(float(r["positive_label_weight"]) > 0 for r in rows) == update.get("branch_examples_completed_superior"),
            "branch_cpu_seconds": float(sum(float(r["cpu_seconds"]) for r in rows)),
            "by_screen": {
                screen: {
                    "n": len(group),
                    "signed_delta_counts": {
                        "negative": sum(float(r["reward_delta"]) < 0 for r in group),
                        "zero": sum(float(r["reward_delta"]) == 0 for r in group),
                        "positive": sum(float(r["reward_delta"]) > 0 for r in group),
                    },
                    "signed_reward_delta_stats_pooled_across_distinct_roots_and_actions": scalar_stats(float(r["reward_delta"]) for r in group),
                    "selected_candidate_probability_stats": scalar_stats(float(r["selected_candidate_probability"]) for r in group if r["selected_candidate_probability"] is not None),
                    "positive_weight_labels": sum(float(r["positive_label_weight"]) > 0 for r in group),
                    "decision_depth_stats": scalar_stats(float(r["decision_index"]) for r in group),
                }
                for screen in sorted({str(r["screen"]) for r in rows})
                for group in [[r for r in rows if str(r["screen"]) == screen]]
            },
        }
    all_completed_rows = [row for rows in by_block.values() for row in rows]
    depth_summaries = {}
    for depth, rows in sorted(by_decision_depth.items()):
        deltas = [float(row["reward_delta"]) for row in rows]
        depth_summaries[str(depth)] = {
            "n_completed_distinct_root_alternatives": len(rows),
            "signed_reward_delta_counts": {
                "negative": sum(value < 0 for value in deltas),
                "zero": sum(value == 0 for value in deltas),
                "positive": sum(value > 0 for value in deltas),
            },
            "signed_reward_delta_shares": {
                "negative": sum(value < 0 for value in deltas) / len(deltas),
                "zero": sum(value == 0 for value in deltas) / len(deltas),
                "positive": sum(value > 0 for value in deltas) / len(deltas),
            },
            "signed_reward_delta_stats_pooled_across_distinct_roots_and_actions": scalar_stats(deltas),
            "selected_candidate_probability_stats": scalar_stats(float(row["selected_candidate_probability"]) for row in rows if row["selected_candidate_probability"] is not None),
            "screens": dict(Counter(str(row["screen"]) for row in rows)),
        }
    block_counts = Counter()
    for seed in range(int(config["training"]["seed_first"]), int(config["training"]["seed_last"]) + 1):
        block_counts[(seed - int(config["training"]["seed_first"])) // int(config["training"]["owners_per_block"]) + 1] += 1
    return {
        "phase": "branch_receipts",
        "status": "PASS" if not errors else "RECEIPT_MISMATCH",
        "config_contract": {
            "owners": int(config["training"]["owner_count"]),
            "owners_per_block": int(config["training"]["owners_per_block"]),
            "roots_per_owner_max": int(continuation["roots_per_owner_max"]),
            "alternatives_per_root_max": int(continuation["alternatives_per_root_max"]),
            "aggregate_process_cpu_seconds_per_root_max": float(continuation["aggregate_process_cpu_seconds_per_root_max"]),
            "suffix_policy": continuation["continuation_policy"],
            "battle_search_api": config["battle_search"]["native_api"],
            "battle_search_simulations": int(config["battle_search"]["simulations"]),
            "registered_terminal_arm_populations_unchanged": True,
        },
        "branch_receipts": {
            "file_count": len(branch_paths),
            "status_counts": dict(status_counts),
            "unique_seed_root_alt_targets": len(seen_targets),
            "attempted_alternative_count": attempted_count,
            "not_attempted_alternative_count": not_attempted_count,
            "attempted_rows_without_forced_root_receipt_by_status": dict(attempts_without_forced_root),
            "forced_root_consumed_receipt_count": forced_root_consumed_count,
            "distinct_attempt_id_count": len(seen_attempt_ids),
            "attempted_rows_equal_distinct_attempt_ids": attempted_count == len(seen_attempt_ids),
            "owners_with_any_branch_root": len(by_owner_root_count),
            "root_status_count": len(root_cpu),
            "max_roots_per_owner_observed": max(by_owner_root_count.values(), default=0),
            "max_alternatives_per_root_observed": max(by_root_count.values(), default=0),
            "max_aggregate_root_cpu_seconds": root_max,
            "roots_over_cap": root_exceeds,
            "completed_terminal_suffix_cpu_seconds": actual_terminal_cpu,
            "all_receipt_attempt_cpu_seconds": float(sum(max(0.0, float(load_json(p).get("cpu_seconds_total", 0.0))) for p in branch_paths)),
            "one_terminal_payload_per_completed_alternative": all(
                int(status_counts.get("COMPLETED_NATIVE_TERMINAL", 0)) == sum(len(x) for x in by_block.values())
                for _ in [0]
            ),
            "no_debug_or_formal_suffix_launched_by_issue72": True,
        },
        "block_reconciliation": block_summaries,
        "depth_reconciliation": depth_summaries,
        "selection_dependence_descriptive_only": {
            "branch_selection_rule": "up to two frozen-policy highest-probability nonchosen legal actions at each of up to two earliest categorical roots per owner",
            "selected_candidate_probability_stats": scalar_stats(float(row["selected_candidate_probability"]) for row in all_completed_rows if row["selected_candidate_probability"] is not None),
            "selected_candidate_probability_vs_signed_reward_delta_pearson": safe_corr(
                (float(row["selected_candidate_probability"]) for row in all_completed_rows if row["selected_candidate_probability"] is not None),
                (float(row["reward_delta"]) for row in all_completed_rows if row["selected_candidate_probability"] is not None),
            ),
            "warning": "These pooled descriptive relationships mix distinct owners, states, screens, action identities, and episode depths; they are not causal or inferential estimates and do not identify conditional terminal variance.",
        },
        "diagnostic_cpu_seconds": 0.0,
        "native_debug_probe_cpu_seconds": 0.0,
        "errors": errors,
        "interpretation": {
            "one suffix per completed alternative": "Each unique seed/root/alt target has one retained branch receipt/attempt_id and, when complete, one terminal outcome. Interrupted and not-attempted receipts remain outcomes; no retry was launched.",
            "selection": "The branch alternatives are selected from the frozen policy's highest-probability nonchosen actions, so labels are action-selected and screen/depth selected rather than an unselected random sample.",
            "variance": "These signed deltas describe realized paired root/alternative suffixes only. One realized suffix per action cannot estimate E[R|s,a], Var(R|s,a), or the action-conditional expected advantage.",
            "CRN": "The nominal owner seed does not establish common random numbers across alternatives: actions can consume different native RNG streams and transition into different states before subsequent random draws.",
        },
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


PHASES = {
    "identity": identity_phase,
    "policy": policy_phase,
    "collisions": collision_phase,
    "replay": replay_phase,
    "branches": branch_phase,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["identity", "policy", "collisions", "replay", "branches", "all"], required=True)
    args = parser.parse_args()
    selected = list(PHASES) if args.phase == "all" else [args.phase]
    for name in selected:
        target = DIAGNOSTIC / f"{name}.json"
        if target.exists():
            print(f"SKIP_EXISTING {target}")
            continue
        try:
            result = PHASES[name]()
            save_new(name, result)
            print(json.dumps({"phase": name, "status": result.get("status"), "file": str(target)}))
            if name == "identity" and result.get("status") != "PASS":
                return 2
            if name in {"replay", "branches"} and result.get("status") != "PASS":
                return 3
        except BaseException as exc:
            failure = {
                "phase": name,
                "status": "FAILED",
                "exception_type": type(exc).__name__,
                "exception": str(exc),
                "traceback": traceback.format_exc(),
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            failure_path = DIAGNOSTIC / f"{name}.failure.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
            with failure_path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(failure, handle, sort_keys=True, indent=2)
                handle.write("\n")
            print(json.dumps({"phase": name, "status": "FAILED", "file": str(failure_path), "error": str(exc)}))
            return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
