"""Check B--F initial-policy identity on one retained authentic native decision.

This is an offline witness. It reuses the already-retained native CARD_SELECT
projection and starts no simulator or Issue 85 game. The historical witness
contains public-v1 rather than public-v3 context, so only the authentic legacy
matrix is used for the parity claim; zero-output residuals make the new payload
irrelevant to this initial-distribution check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import torch

from feature_firewall import assert_index_free, sanitize_executor_tree
from native_decision_witness import audit as audit_native_witness, load_legacy_encoder, load_policy
from preflight import LOCK, REPO_ROOT, git_text, load_pinned_model_module, sha256
from representations import ARMS, Issue85Arm


def _distribution(model: Any, features: torch.Tensor, payload: dict[str, Any] | None = None):
    with torch.no_grad():
        if isinstance(model, Issue85Arm):
            return model.distribution(features, payload or {})
        return model.distribution(features)


def verify(data_root: Path, git_repo: Path) -> dict[str, Any]:
    witness_report = audit_native_witness(data_root, git_repo)
    witness_lock = LOCK["native_decision_witness"]
    root_path = data_root / witness_lock["root_data_path"]
    root = json.loads(root_path.read_text(encoding="utf-8"))
    projection = root["raw_privileged_projection"]
    legacy_matrix, encoder_audit = load_legacy_encoder(
        data_root / LOCK["legacy_encoder"]["data_path"],
        LOCK["legacy_encoder"]["sha256"],
    ).encode(projection, len(root["legal_action_occurrences"]))
    wide_matrix, _wide_audit = load_legacy_encoder(
        data_root / LOCK["legacy_encoder"]["data_path"],
        LOCK["legacy_encoder"]["sha256"],
        dimension=8192,
    ).encode(projection, len(root["legal_action_occurrences"]))

    errors: list[str] = []
    saved = np.asarray(root["semantic_feature_matrix"], dtype=np.float32)
    if not np.array_equal(legacy_matrix, saved):
        errors.append("recomputed legacy matrix differs from the retained authentic native decision")
    if hashlib.sha256(legacy_matrix.astype("<f4").tobytes()).hexdigest() != root.get("feature_matrix_sha256"):
        errors.append("recomputed legacy matrix digest differs from the retained witness")
    folded = wide_matrix.reshape(len(wide_matrix), 4, 2048).sum(axis=1)
    fold_ok = bool(np.allclose(folded, legacy_matrix, rtol=0.0, atol=1e-7))
    if not fold_ok:
        errors.append("real 8192 signed-hash matrix does not fold to the authentic 2048 matrix")

    candidates = []
    for row in projection.get("candidates", ()):
        candidates.append({
            "kind": "card_select",
            "features": sanitize_executor_tree(row.get("features", {})),
        })
    parity_payload = {
        "schema_id": "issue85-authentic-legacy-matrix-parity-only-v1",
        "screen": sanitize_executor_tree(projection.get("screen_identity", {})),
        "shared_context": sanitize_executor_tree(projection.get("shared_context", {})),
        "public_context": {},
        "candidates": candidates,
    }
    try:
        assert_index_free(parity_payload)
    except ValueError as exc:
        errors.append(f"parity payload is not index-free: {exc}")
    if len(candidates) != len(legacy_matrix):
        errors.append("authentic candidate feature rows do not align with legal native occurrences")

    baseline_lock = LOCK["baseline_P"]
    checkpoint = data_root / baseline_lock["checkpoint_data_path"]
    with load_pinned_model_module(git_repo, data_root) as model_module:
        parent = load_policy(checkpoint, model_module)
        parent_state = {name: value.detach().clone() for name, value in parent.state_dict().items()}
        parent_digest = model_module.model_digest(parent)
        parent_features = torch.as_tensor(legacy_matrix, dtype=torch.float32)
        baseline_logits, baseline_logp, baseline_entropy = _distribution(parent, parent_features)
        rows: dict[str, Any] = {}
        for arm_name in sorted(ARMS):
            arm = Issue85Arm(arm_name, model_module, parent_state).eval()
            features = torch.as_tensor(
                wide_matrix if arm.widened else legacy_matrix,
                dtype=torch.float32,
            )
            logits, logp, entropy = _distribution(arm, features, parity_payload)
            logits_ok = torch.allclose(logits, baseline_logits, rtol=1e-6, atol=1e-6)
            logp_ok = torch.allclose(logp, baseline_logp, rtol=1e-6, atol=1e-6)
            entropy_ok = torch.allclose(entropy, baseline_entropy, rtol=1e-6, atol=1e-6)
            generator_arm = torch.Generator().manual_seed(8500001)
            generator_parent = torch.Generator().manual_seed(8500001)
            arm_choice = int(torch.multinomial(logp.exp(), 1, generator=generator_arm).item())
            parent_choice = int(torch.multinomial(baseline_logp.exp(), 1, generator=generator_parent).item())
            choice_ok = arm_choice == parent_choice
            if not (logits_ok and logp_ok and entropy_ok and choice_ok):
                errors.append(f"{arm_name} initial policy differs from π12 on the authentic decision")
            rows[arm_name] = {
                "logits_close": bool(logits_ok),
                "log_probabilities_close": bool(logp_ok),
                "entropy_close": bool(entropy_ok),
                "same_seed_sampled_occurrence": bool(choice_ok),
                "max_abs_logit_difference": float((logits - baseline_logits).abs().max().item()),
                "max_abs_probability_difference": float((logp.exp() - baseline_logp.exp()).abs().max().item()),
                "sampled_occurrence": arm_choice,
            }

    return {
        "schema_id": "issue85-initial-policy-authentic-decision-witness-v1",
        "verified_utc": datetime.now(timezone.utc).isoformat(),
        "verified": not errors and bool(witness_report.get("verified")),
        "issue85_games_started_by_this_check": 0,
        "source": {
            "study_source_commit": git_text(REPO_ROOT, "rev-parse", "HEAD"),
            "study_source_files": {
                name: sha256(Path(__file__).resolve().parent / name)
                for name in (
                    "integration_parity_witness.py",
                    "native_decision_witness.py",
                    "runtime_integration.py",
                    "representations.py",
                    "feature_firewall.py",
                )
            },
            "pinned_native_commit": LOCK["native"]["commit"],
            "pinned_native_binary_sha256": LOCK["native"]["compiled_module_sha256"],
            "legacy_encoder_commit": LOCK["legacy_encoder"]["commit"],
            "legacy_encoder_sha256": LOCK["legacy_encoder"]["sha256"],
            "parent_checkpoint_sha256": sha256(checkpoint),
            "parent_model_digest": parent_digest,
        },
        "native_witness": {
            "root_id": root.get("root_id"),
            "run_id": root.get("owner_run_id"),
            "owner_seed": root.get("owner_seed"),
            "decision_index": root.get("owner_decision_index"),
            "screen": root.get("screen"),
            "root_sha256": sha256(root_path),
            "source_audit_verified": bool(witness_report.get("verified")),
            "legacy_encoder_audit": encoder_audit,
            "candidate_count": len(candidates),
            "legacy_matrix_sha256": hashlib.sha256(legacy_matrix.astype("<f4").tobytes()).hexdigest(),
            "wide_matrix_folds_to_legacy": fold_ok,
        },
        "parity_scope": {
            "B_to_F_match_pi12_distribution_on_retained_authentic_legacy_matrix": rows,
            "payload_mode": "index-free candidate features from retained native projection; public-v1 context omitted; residual heads are exactly zero",
            "tolerance": {"rtol": 1e-6, "atol": 1e-6},
        },
        "limitations": [
            "This is an offline reuse of one retained authentic CARD_SELECT decision, not a new owner, game, or outcome.",
            "The retained public projection is v1. It does not verify current public-v3 context extraction or same-call live adapter binding.",
            "It proves initial actor parity on this authentic legacy feature matrix only; it is not pilot readiness or efficacy evidence.",
        ],
        "errors": errors + ([] if witness_report.get("verified") else list(witness_report.get("errors", []))),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--git-repo", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.data_root.resolve(), args.git_repo.resolve())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"verified": report["verified"], "report": str(args.report), "errors": report["errors"]}, sort_keys=True))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
