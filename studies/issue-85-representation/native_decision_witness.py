"""Audit an existing authentic native CARD_SELECT witness for Issue 85.

This reads only the historical Issue 73 source snapshot and receipts, the
source-locked P/A checkpoints, and the current Issue 85 firewall. It does not
launch the simulator or create any game trajectory.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import torch

from feature_firewall import assert_index_free, sanitize_executor_tree
from preflight import LOCK, load_pinned_model_module, sha256


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_legacy_encoder(path: Path, expected_sha256: str, *, dimension: int | None = None) -> Any:
    actual = sha256(path)
    if actual != expected_sha256:
        raise RuntimeError(f"legacy encoder SHA mismatch: expected {expected_sha256}, got {actual}")
    directory = str(path.parent)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location("issue85_locked_issue73_policy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import source-locked Issue 73 encoder at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    selected_dimension = int(
        LOCK["model_runtime"]["architecture_dimension"] if dimension is None else dimension
    )
    return module.SemanticHashEncoder(selected_dimension)


def load_policy(checkpoint: Path, model_module: Any) -> Any:
    runtime = LOCK["model_runtime"]
    model = model_module.ActionConditionedPolicy(
        dimension=runtime["architecture_dimension"],
        seed=runtime["initial_model_seed"],
        final_weight_std=runtime["final_weight_std"],
    ).eval()
    state = torch.load(checkpoint, map_location="cpu", weights_only=True)
    model.load_state_dict(state, strict=True)
    return model


def distributions(matrix: np.ndarray, model: Any) -> dict[str, list[float]]:
    with torch.no_grad():
        logits, log_probs, _ = model.distribution(torch.as_tensor(matrix, dtype=torch.float32))
    return {
        "logits": [float(value) for value in logits.tolist()],
        "probabilities": [float(value) for value in torch.exp(log_probs).tolist()],
    }


def audit(data_root: Path, git_repo: Path) -> dict[str, Any]:
    errors: list[str] = []
    witness_lock = LOCK["native_decision_witness"]
    witness_path = data_root / witness_lock["root_data_path"]
    snapshot_path = data_root / witness_lock["source_snapshot_data_path"]
    receipt_path = data_root / witness_lock["run_receipt_data_path"]
    root_sha = sha256(witness_path) if witness_path.is_file() else None
    snapshot_sha = sha256(snapshot_path) if snapshot_path.is_file() else None
    receipt_sha = sha256(receipt_path) if receipt_path.is_file() else None

    def require(condition: bool, label: str, detail: Any = None) -> None:
        if not condition:
            errors.append(f"{label}: {detail if detail is not None else 'mismatch'}")

    require(witness_path.is_file(), "native_witness_exists", str(witness_path))
    require(snapshot_path.is_file(), "native_source_snapshot_exists", str(snapshot_path))
    require(receipt_path.is_file(), "native_run_receipt_exists", str(receipt_path))
    if errors:
        return {"verified": False, "errors": errors}

    require(root_sha == witness_lock["root_sha256"], "native_witness_sha256", root_sha)
    require(snapshot_sha == witness_lock["source_snapshot_sha256"], "native_source_snapshot_sha256", snapshot_sha)
    require(receipt_sha == witness_lock["run_receipt_sha256"], "native_run_receipt_sha256", receipt_sha)
    root = read_json(witness_path)
    snapshot = read_json(snapshot_path)
    receipt = read_json(receipt_path)
    relative_root = witness_lock["root_data_path"].split(
        "issue-73-privileged-noncombat-oracle-stage0/", 1
    )[1]
    manifest_entry = snapshot.get("files", {}).get(relative_root, {})
    require(manifest_entry.get("sha256") == root_sha, "source_snapshot_root_sha256", manifest_entry)
    require(snapshot.get("issue73_source_commit") == witness_lock["source_commit"], "witness_producer_source", snapshot.get("issue73_source_commit"))
    require(root.get("schema_id") == "issue73-native-debug-root-v1", "witness_schema", root.get("schema_id"))
    require(root.get("root_id") == witness_lock["root_id"], "witness_root_id", root.get("root_id"))
    require(root.get("owner_run_id") == witness_lock["run_id"], "witness_run_id", root.get("owner_run_id"))
    require(root.get("owner_seed") == witness_lock["owner_seed"], "witness_owner_seed", root.get("owner_seed"))
    require(root.get("owner_decision_index") == witness_lock["owner_decision_index"], "witness_decision_index", root.get("owner_decision_index"))
    require(root.get("screen") == "CARD_SELECT", "witness_screen", root.get("screen"))
    require(root.get("native_source_commit") == LOCK["native"]["commit"], "witness_native_commit", root.get("native_source_commit"))
    require(root.get("native_binary_sha256") == LOCK["native"]["compiled_module_sha256"], "witness_native_binary", root.get("native_binary_sha256"))
    require(root.get("candidate_binding_validation_passed") is True, "native_candidate_binding_validation", root.get("candidate_binding_validation_passed"))
    require(receipt.get("completed_native_terminal") is True and receipt.get("terminal_complete") is True, "source_run_terminal", {"completed_native_terminal": receipt.get("completed_native_terminal"), "terminal_complete": receipt.get("terminal_complete")})
    require(receipt.get("seed") == witness_lock["owner_seed"], "source_run_seed", receipt.get("seed"))
    require(receipt.get("native_source_commit") == LOCK["native"]["commit"], "source_run_native_commit", receipt.get("native_source_commit"))
    require(receipt.get("native_module_sha256") == LOCK["native"]["compiled_module_sha256"], "source_run_native_binary", receipt.get("native_module_sha256"))

    projection = root.get("raw_privileged_projection", {})
    candidates = projection.get("candidates", [])
    legal = root.get("legal_action_occurrences", [])
    require(len(candidates) == len(legal) == projection.get("candidate_count"), "candidate_legal_count", {"candidates": len(candidates), "legal": len(legal), "projection": projection.get("candidate_count")})
    require(bool(candidates), "candidate_rows_nonempty")
    indices: list[int] = []
    card_descriptors: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        features = candidate.get("features", {})
        field = features.get("target_deck_index", {})
        require(field.get("availability") == "available", f"target_index_{index}_available", field)
        require(field.get("visibility") == "oracle_only", f"target_index_{index}_visibility", field)
        require(field.get("source") == witness_lock["target_deck_index_source"], f"target_index_{index}_source", field.get("source"))
        require(isinstance(field.get("value"), int) and not isinstance(field.get("value"), bool), f"target_index_{index}_type", field.get("value"))
        if isinstance(field.get("value"), int) and not isinstance(field.get("value"), bool):
            indices.append(field["value"])
        target = features.get("target_card", {})
        target_value = target.get("value") if isinstance(target, dict) else None
        card_descriptors.append({
            "availability": target.get("availability") if isinstance(target, dict) else None,
            "visibility": target.get("visibility") if isinstance(target, dict) else None,
            "value": target_value,
        })
        if index < len(legal):
            require(legal[index].get("kind") == "card_select", f"legal_action_{index}_kind", legal[index].get("kind"))

    decision = next((row for row in receipt.get("decisions", []) if row.get("step") == witness_lock["owner_decision_index"]), None)
    require(decision is not None, "run_contains_witness_decision")
    if decision is not None:
        require(decision.get("screen") == "CARD_SELECT", "run_decision_screen", decision.get("screen"))
        require(decision.get("chosen_index") == root.get("selected_owner_occurrence"), "native_chosen_occurrence", {"run": decision.get("chosen_index"), "root": root.get("selected_owner_occurrence")})
        require(decision.get("legal_action_identities") == legal, "native_legal_action_identity_binding")

    data_encoder = data_root / LOCK["legacy_encoder"]["data_path"]
    encoder = load_legacy_encoder(data_encoder, LOCK["legacy_encoder"]["sha256"])
    matrix, encoder_audit = encoder.encode(projection, len(legal))
    saved_matrix = np.asarray(root.get("semantic_feature_matrix"), dtype=np.float32)
    require(matrix.shape == saved_matrix.shape, "legacy_matrix_shape", {"recomputed": list(matrix.shape), "saved": list(saved_matrix.shape)})
    require(np.array_equal(matrix, saved_matrix), "legacy_matrix_recomputation", {"recomputed_sha256": hashlib.sha256(matrix.astype("<f4").tobytes()).hexdigest(), "saved_sha256": root.get("feature_matrix_sha256")})
    require(hashlib.sha256(matrix.astype("<f4").tobytes()).hexdigest() == root.get("feature_matrix_sha256"), "legacy_matrix_sha256")

    changed_projection = copy.deepcopy(projection)
    changed_to = int(changed_projection["candidates"][0]["features"]["target_deck_index"]["value"]) + 100_000
    changed_projection["candidates"][0]["features"]["target_deck_index"]["value"] = changed_to
    changed_matrix, _ = encoder.encode(changed_projection, len(legal))
    legacy_index_reaches_backbone = not np.array_equal(matrix, changed_matrix)
    require(legacy_index_reaches_backbone, "legacy_index_changes_historical_encoding")

    before_new_features = [sanitize_executor_tree(row["features"]) for row in candidates]
    after_new_features = [sanitize_executor_tree(row["features"]) for row in changed_projection["candidates"]]
    for index, value in enumerate(before_new_features):
        assert_index_free(value, f"new_candidate_{index}")
    require(before_new_features == after_new_features, "new_path_invariant_to_index_only_change")
    target_cards_preserved = all(
        before_new_features[index].get("target_card", {}).get("value")
        == candidate.get("features", {}).get("target_card", {}).get("value")
        for index, candidate in enumerate(candidates)
    )
    require(all(descriptor.get("value") is not None for descriptor in card_descriptors), "target_card_semantics_available")
    require(target_cards_preserved, "target_card_semantics_preserved")

    p_lock, a_lock = LOCK["baseline_P"], LOCK["baseline_A"]
    p_checkpoint = data_root / p_lock["checkpoint_data_path"]
    a_checkpoint = data_root / a_lock["checkpoint_data_path"]
    require(sha256(p_checkpoint) == p_lock["checkpoint_sha256"], "P_checkpoint_sha256")
    require(sha256(a_checkpoint) == a_lock["checkpoint_sha256"], "A_checkpoint_sha256")
    with load_pinned_model_module(git_repo, data_root) as model_module:
        p_model = load_policy(p_checkpoint, model_module)
        a_model = load_policy(a_checkpoint, model_module)
        p_digest = model_module.model_digest(p_model)
        a_digest = model_module.model_digest(a_model)
        p_outputs = distributions(matrix, p_model)
        a_outputs = distributions(matrix, a_model)
    require(p_digest == p_lock["model_digest"], "P_model_digest", p_digest)
    require(a_digest == a_lock["model_digest"], "A_model_digest", a_digest)

    summary = {
        "schema_id": "issue85-native-card-select-witness-v1",
        "verified_utc": datetime.now(timezone.utc).isoformat(),
        "verified": not errors,
        "issue85_games_started_by_this_check": 0,
        "source_witness": {
            "issue73_source_commit": witness_lock["source_commit"],
            "source_snapshot_sha256": snapshot_sha,
            "root_path": str(witness_path),
            "root_sha256": root_sha,
            "run_receipt_path": str(receipt_path),
            "run_receipt_sha256": receipt_sha,
            "owner_run_id": root.get("owner_run_id"),
            "owner_seed": root.get("owner_seed"),
            "decision_index": root.get("owner_decision_index"),
            "screen": root.get("screen"),
            "root_capture_status": root.get("owner_attempt_status_at_capture"),
            "completed_terminal": receipt.get("completed_native_terminal"),
            "terminal_outcome": receipt.get("run_outcome"),
            "terminal_floor": receipt.get("terminal", {}).get("terminal_floor"),
            "native_source_commit": root.get("native_source_commit"),
            "native_binary_sha256": root.get("native_binary_sha256"),
            "legal_candidates": len(legal),
            "candidate_binding_validation_passed": root.get("candidate_binding_validation_passed"),
            "selected_native_occurrence": root.get("selected_owner_occurrence"),
            "target_deck_indices": indices,
            "target_card_attributes": card_descriptors,
            "public_projection_schema_in_historical_receipt": root.get("sanitized_public_projection", {}).get("schema_id"),
        },
        "path_witnesses": {
            "legacy_encoder_file_sha256": LOCK["legacy_encoder"]["sha256"],
            "legacy_encoder_audit": encoder_audit,
            "legacy_matrix_recomputed_from_raw_projection": bool(np.array_equal(matrix, saved_matrix)),
            "changing_only_first_target_deck_index_to": changed_to,
            "legacy_matrix_changes_when_index_changes": legacy_index_reaches_backbone,
            "new_candidate_features_opaque_key_free": True,
            "new_candidate_features_invariant_when_only_index_changes": before_new_features == after_new_features,
            "target_card_attributes_preserved_in_new_feature_tree": target_cards_preserved,
        },
        "locked_policy_outputs_on_authentic_decision": {
            "P_model_digest": p_digest,
            "A_model_digest": a_digest,
            "P": p_outputs,
            "A_B1024": a_outputs,
            "B_F_initial_policy_parity": "NOT_YET_IMPLEMENTED_OR_TESTED",
        },
        "errors": errors,
        "limitations": [
            "The authentic witness is a historical Issue 73 debug-run CARD_SELECT, not a new Issue 85 owner or outcome.",
            "The captured native checkpoint could not be serialized; exact raw projection, legal actions, binding validation, selected occurrence, and the complete native terminal receipt are retained.",
            "The historical public projection in this Issue 73 receipt is v1. This witnesses the inherited target-index path only; Issue 85 public-v3 context paths remain to be checked with their final implementation.",
            "B-F arms are not yet implemented, so their initial-policy equality remains unverified.",
        ],
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--git-repo", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.data_root.resolve(), args.git_repo.resolve())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"verified": report["verified"], "report": str(args.report), "errors": report["errors"]}, sort_keys=True))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
