"""Issue 73 Stage 0 Gate A: hash parity and a fixed synthetic response witness."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import torch
from torch import nn

import runner
import source_lock_integrity
from policy import AllNoncombatPolicy, PROJECTION_ORDER, SemanticHashEncoder


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _legacy_matrix(projection: Mapping[str, Any], action_count: int, dimension: int) -> np.ndarray:
    class LegacyEncoder(SemanticHashEncoder):
        def _add(self, vector: np.ndarray, token: str, value: float = 1.0) -> None:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            number = int.from_bytes(digest, "little")
            index = number % self.dimension
            sign = 1.0 if digest[0] & 1 else -1.0
            vector[index] += np.float32(sign * value)

    return LegacyEncoder(dimension).encode(projection, action_count)[0]


def check_sign_parity_corpus(dimension: int) -> dict[str, Any]:
    fixed_cells: set[tuple[int, int]] = set()
    legacy_cells: set[tuple[int, int]] = set()
    encoder = SemanticHashEncoder(dimension)
    corpus_count = 16384
    for index in range(corpus_count):
        token = f"issue73-gate-a-sign-parity-corpus-v1:{index:05d}"
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest, "little") % dimension
        parity = bucket & 1
        old_sign = 1 if digest[0] & 1 else -1
        new_sign = 1 if digest[-1] & 1 else -1
        vector = np.zeros(dimension, dtype=np.float32)
        encoder._add(vector, token)
        observed_sign = 1 if vector[bucket] > 0 else -1
        if observed_sign != new_sign:
            raise AssertionError("SemanticHashEncoder._add disagrees with its independent digest sign bit")
        legacy_cells.add((parity, old_sign))
        fixed_cells.add((parity, new_sign))
    required = {(0, -1), (0, 1), (1, -1), (1, 1)}
    if fixed_cells != required:
        raise AssertionError(f"fixed digest corpus did not witness all bucket/sign combinations: {fixed_cells}")
    if legacy_cells != {(0, -1), (1, 1)}:
        raise AssertionError(f"legacy parity defect was not reproduced exactly: {legacy_cells}")
    return {
        "schema_id": "issue73-hash-sign-bucket-parity-v1",
        "token_corpus_count": corpus_count,
        "dimension": dimension,
        "fixed_bucket_parity_and_sign_cells": [list(x) for x in sorted(fixed_cells)],
        "legacy_bucket_parity_and_sign_cells": [list(x) for x in sorted(legacy_cells)],
        "fixed_sign_uses_digest_byte_index": -1,
        "legacy_sign_uses_digest_byte_index": 0,
        "passed": True,
    }


def _canonical_target_payload(raw: bytes, *, label: str) -> bytes:
    """Accept one uniform checkout EOL style without weakening target identity."""
    try:
        canonical = source_lock_integrity.normalize_locked_source_eol(raw, label=label)
    except source_lock_integrity.SourceLockValidationError as exc:
        raise RuntimeError(f"fixed synthetic target direction has invalid line endings: {label}") from exc
    if b"\r\n" in raw and b"\n" in raw.replace(b"\r\n", b""):
        raise RuntimeError(f"fixed synthetic target direction has mixed line endings: {label}")
    return canonical


def _target_direction() -> tuple[np.ndarray, str]:
    path = runner.ROOT / runner.CONFIG["stage0"]["synthetic_target"]["direction_file"]
    raw = path.read_bytes()
    canonical = _canonical_target_payload(raw, label=str(path))
    digest = _sha_bytes(canonical)
    expected = runner.CONFIG["stage0"]["synthetic_target"]["direction_sha256"]
    if digest != expected:
        raise RuntimeError(
            "fixed synthetic target direction canonical SHA-256 differs from the committed configuration: "
            f"{digest} != {expected}"
        )
    values = np.asarray(json.loads(canonical.decode("utf-8"))["direction"], dtype=np.float32)
    if values.shape != (int(runner.CONFIG["model"]["hash_dimension"]),) or not np.all(np.isfinite(values)):
        raise RuntimeError("synthetic target direction has invalid shape or values")
    norm = float(np.linalg.norm(values))
    if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-6):
        raise RuntimeError("synthetic target direction is not unit-normalized")
    return values, digest


def select_corpus(roots: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Predeclared order: first four distinct screen families, then first roots to 24."""
    ordered = sorted(roots, key=lambda r: (int(r["seed"]), int(r["decision_index"])))
    selected: list[Mapping[str, Any]] = []
    selected_ids: set[str] = set()
    families: set[str] = set()
    for root in ordered:
        family = str(root["screen"])
        if family not in families:
            families.add(family)
            selected.append(root)
            selected_ids.add(str(root["root_id"]))
        if len(families) >= int(runner.CONFIG["stage0"]["required_screen_family_count"]):
            break
    if len(families) < int(runner.CONFIG["stage0"]["required_screen_family_count"]):
        return []
    for root in ordered:
        root_key = str(root["root_id"])
        if root_key not in selected_ids:
            selected.append(root)
            selected_ids.add(root_key)
        if len(selected) == int(runner.CONFIG["stage0"]["required_multichoice_roots"]):
            break
    if len(selected) != int(runner.CONFIG["stage0"]["required_multichoice_roots"]):
        return []
    if any(len(root["legal_action_identities"]) < 2 for root in selected):
        raise AssertionError("selected Gate A root is not a genuine legal multichoice")
    return selected


def _distribution(model: AllNoncombatPolicy, matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    with torch.no_grad():
        logits, log_probs, _ = model.distribution(torch.as_tensor(matrix, dtype=torch.float32))
    return logits.cpu().numpy(), log_probs.exp().cpu().numpy()


def _log_odds(logits: np.ndarray, target: int) -> float:
    others = np.delete(np.asarray(logits, dtype=np.float64), target)
    if not len(others):
        return float("inf")
    maximum = float(np.max(others))
    logsumexp_others = maximum + math.log(float(np.exp(others - maximum).sum()))
    return float(logits[target]) - logsumexp_others


def _train_encoding(
    roots: Sequence[Mapping[str, Any]], matrices: Sequence[np.ndarray], targets: Sequence[int], *, updates: int, lr: float, model_seed: int,
) -> tuple[AllNoncombatPolicy, dict[str, Any]]:
    model = AllNoncombatPolicy(
        dimension=int(runner.CONFIG["model"]["hash_dimension"]), seed=model_seed
    )
    initial_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
    initial_metrics: list[dict[str, Any]] = []
    for root, matrix, target in zip(roots, matrices, targets, strict=True):
        logits, probs = _distribution(model, matrix)
        initial_metrics.append({"target_probability": float(probs[target]), "target_log_odds": _log_odds(logits, target), "probabilities": probs.tolist()})
    optimizer = torch.optim.Adam(model.actor.parameters(), lr=lr)
    gradient_norms: list[float] = []
    nonzero_gradient_parameters: list[int] = []
    for _ in range(updates):
        optimizer.zero_grad(set_to_none=True)
        losses: list[torch.Tensor] = []
        for matrix, target in zip(matrices, targets, strict=True):
            logits, _, _ = model.distribution(torch.as_tensor(matrix, dtype=torch.float32))
            losses.append(nn.functional.cross_entropy(logits.unsqueeze(0), torch.tensor([target])))
        loss = torch.stack(losses).mean()
        loss.backward()
        gradients = [p.grad for p in model.actor.parameters() if p.grad is not None]
        gradient_norms.append(float(torch.sqrt(sum(torch.sum(g.detach().float() ** 2) for g in gradients)).item()))
        nonzero_gradient_parameters.append(sum(int(torch.count_nonzero(g).item()) for g in gradients))
        optimizer.step()
    results: list[dict[str, Any]] = []
    tv: list[float] = []
    rises: list[bool] = []
    joint_response: list[bool] = []
    for root, matrix, target, initial in zip(roots, matrices, targets, initial_metrics, strict=True):
        logits, probs = _distribution(model, matrix)
        variation = 0.5 * float(np.abs(probs - np.asarray(initial["probabilities"], dtype=np.float64)).sum())
        rise = float(probs[target]) > float(initial["target_probability"])
        odds = _log_odds(logits, target)
        tv.append(variation)
        rises.append(rise)
        joint_response.append(rise and variation >= 0.05)
        results.append({
            "seed": int(root["seed"]), "run_id": root["run_id"],
            "decision_index": int(root["decision_index"]), "screen_family": str(root["screen"]),
            "legal_action_count": len(root["legal_action_identities"]),
            "target_action_occurrence": int(target),
            "target_identity_sha256": _sha_json(root["legal_action_identities"][target]),
            "initial_target_probability": float(initial["target_probability"]),
            "final_target_probability": float(probs[target]),
            "target_probability_rise": rise,
            "initial_signed_log_odds": float(initial["target_log_odds"]),
            "final_signed_log_odds": float(odds),
            "signed_log_odds_change": float(odds - float(initial["target_log_odds"])),
            "total_variation_from_own_initialization": variation,
            "joint_target_rise_and_tv_at_least_005": rise and variation >= 0.05,
        })
    layers = []
    for index, module in enumerate(model.actor):
        if isinstance(module, nn.Linear):
            layers.append({"index": index, "in_features": module.in_features, "out_features": module.out_features, "parameters": sum(p.numel() for p in module.parameters())})
    return model, {
        "gradient_updates": updates,
        "gradient_norm_min": min(gradient_norms), "gradient_norm_max": max(gradient_norms),
        "nonzero_gradient_parameter_count_first_last": [nonzero_gradient_parameters[0], nonzero_gradient_parameters[-1]],
        "actor_layers": layers,
        "actor_parameter_count": sum(p.numel() for p in model.actor.parameters()),
        "critic_parameters_frozen": True,
        "target_probability_rise_count": sum(rises),
        "tv_at_least_005_count": sum(value >= 0.05 for value in tv),
        "tv_at_least_005_fraction": sum(value >= 0.05 for value in tv) / len(tv),
        "joint_target_rise_and_tv_at_least_005_count": sum(joint_response),
        "joint_target_rise_and_tv_at_least_005_fraction": sum(joint_response) / len(joint_response),
        "root_results": results,
    }


def _sha_json(value: Any) -> str:
    return _sha_bytes(runner.canonical(value).encode("utf-8"))


def run_gate_a(roots: Sequence[Mapping[str, Any]], out: Path) -> dict[str, Any]:
    dimension = int(runner.CONFIG["model"]["hash_dimension"])
    parity = check_sign_parity_corpus(dimension)
    selected = select_corpus(roots)
    if not selected:
        return {"status": "FAIL_INSUFFICIENT_ROOTS_OR_FAMILIES", "required_roots": 24, "observed_roots": len(roots), "minimum_screen_families": 4, "sign_parity_corpus": parity}
    fixed = SemanticHashEncoder(dimension)
    target_vector, target_sha = _target_direction()
    fixed_matrices: list[np.ndarray] = []
    old_matrices: list[np.ndarray] = []
    target_scores: list[np.ndarray] = []
    targets: list[int] = []
    for root in selected:
        projection = root["projection"]
        actions = root["legal_action_identities"]
        if root.get("live_binding_validation_passed") is not True:
            raise AssertionError(f"native candidate-to-legal-action binding was not validated for {root.get('root_id')}")
        if len(actions) != len(projection.get("candidates", [])):
            raise AssertionError("candidate identity count changed between projection and live legal occurrence order")
        matrix, audit = fixed.encode(projection, len(actions))
        legacy = _legacy_matrix(projection, len(actions), dimension)
        if not np.all(np.isfinite(matrix)) or not np.all(np.isfinite(legacy)):
            raise AssertionError("hash encoding produced a non-finite candidate feature")
        scores = matrix.astype(np.float64) @ target_vector.astype(np.float64)
        target = int(np.argmax(scores))
        fixed_matrices.append(matrix)
        old_matrices.append(legacy)
        target_scores.append(scores)
        targets.append(target)
        if audit.get("candidate_order_source") != PROJECTION_ORDER:
            raise AssertionError("candidate occurrence mapping source changed")
    steps_each = int(runner.CONFIG["stage0"]["synthetic_target"]["updates_per_encoding"])
    if steps_each * 2 > int(runner.CONFIG["stage0"]["synthetic_target"]["total_gradient_updates_max"]):
        raise RuntimeError("fixed and legacy candidate actors exceed the committed total gradient-update ceiling")
    model_seed = int(runner.CONFIG["stage0"]["synthetic_target"]["actor_seed"])
    fixed_model, fixed_metrics = _train_encoding(
        selected, fixed_matrices, targets, updates=steps_each,
        lr=float(runner.CONFIG["stage0"]["synthetic_target"]["learning_rate"]), model_seed=model_seed,
    )
    legacy_model, legacy_metrics = _train_encoding(
        selected, old_matrices, targets, updates=steps_each,
        lr=float(runner.CONFIG["stage0"]["synthetic_target"]["learning_rate"]), model_seed=model_seed,
    )
    before = {k: v.detach().cpu().numpy() for k, v in AllNoncombatPolicy(dimension=dimension, seed=model_seed).state_dict().items()}
    for key, value in before.items():
        if not np.array_equal(value, fixed_model.state_dict()[key].detach().cpu().numpy() if key in fixed_model.state_dict() else value):
            # Actor is expected to move; all unchanged critic tensors must match initialization.
            if key.startswith("critic."):
                raise AssertionError("synthetic target update changed critic parameters")
    required_fraction = float(runner.CONFIG["stage0"]["synthetic_target"]["minimum_tv_fraction"])
    fixed_pass = fixed_metrics["joint_target_rise_and_tv_at_least_005_fraction"] >= required_fraction
    legacy_pass = legacy_metrics["joint_target_rise_and_tv_at_least_005_fraction"] >= required_fraction
    gate = {
        "schema_id": "issue73-gate-a-policy-response-v1",
        "status": "PASS" if fixed_pass else "FAIL_FIXED_POLICY_RESPONSE_THRESHOLD",
        "synthetic_target": {
            "target_direction_file": runner.CONFIG["stage0"]["synthetic_target"]["direction_file"],
            "target_direction_sha256": target_sha,
            "target_formula": "argmax_c (fixed_semantic_hash_features[c] dot precommitted unit direction)",
            "target_direction_seed": runner.CONFIG["stage0"]["synthetic_target"]["direction_seed"],
            "target_reselected": False,
            "root_target_scores_sha256": _sha_json([x.tolist() for x in target_scores]),
            "actor_seed": model_seed,
            "learning_rate": float(runner.CONFIG["stage0"]["synthetic_target"]["learning_rate"]),
            "updates_per_encoding": steps_each,
            "total_gradient_updates_across_fixed_and_legacy_actors": steps_each * 2,
            "loss": "mean cross entropy over fixed target occurrences across fixed root corpus",
        },
        "sign_parity_corpus": parity,
        "root_count": len(selected),
        "screen_family_count": len({str(root["screen"]) for root in selected}),
        "root_source": [
            {"seed": int(root["seed"]), "run_id": root["run_id"], "decision_index": int(root["decision_index"]), "policy_decision_index": int(root["policy_decision_index"]), "screen": root["screen"], "legal_action_count": len(root["legal_action_identities"]), "root_projection_sha256": root["projection_digest"]}
            for root in selected
        ],
        "fixed_encoder": fixed_metrics,
        "legacy_encoder_comparison": {"status": "PASS_RESPONSE_THRESHOLD" if legacy_pass else "FAIL_RESPONSE_THRESHOLD", **legacy_metrics},
        "fixed_encoder_passed_threshold": fixed_pass,
        "candidate_identity_and_legal_binding_preserved": all(bool(root.get("live_binding_validation_passed")) for root in selected),
        "model_input_schema": "issue73-signed-hash-semantic-candidate-v1",
        "normal_public_api_receives_oracle_only_values": False,
        "interpretation": "Synthetic optimization responsiveness only; no return-quality, generalization, or Stage 1 claim.",
    }
    runner.atomic_json(out / "gate-a-report.json", gate)
    runner.atomic_json(out / "gate-a-corpus.json", {
        "schema_id": "issue73-gate-a-fixed-root-corpus-v1",
        "root_ids": [{"seed": int(r["seed"]), "run_id": r["run_id"], "decision_index": int(r["decision_index"]), "screen": r["screen"]} for r in selected],
        "candidate_target_occurrences": targets,
        "target_direction_sha256": target_sha,
        "target_scores": [x.tolist() for x in target_scores],
        "fixed_feature_matrices_sha256": [_sha_bytes(np.asarray(x, dtype="<f4").tobytes()) for x in fixed_matrices],
        "legacy_feature_matrices_sha256": [_sha_bytes(np.asarray(x, dtype="<f4").tobytes()) for x in old_matrices],
    })
    return gate
