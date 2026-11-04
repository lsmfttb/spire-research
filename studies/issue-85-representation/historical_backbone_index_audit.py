"""Source-verify non-grandfathered indices reaching the frozen #73 backbone.

This is a read-only audit of an already archived native CARD_SELECT receipt.
It does not launch the simulator or create any new game outcome.
"""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import torch

from native_decision_witness import distributions, load_legacy_encoder, load_policy, read_json
from preflight import LOCK, load_pinned_model_module, sha256


INDEX_PATHS: tuple[tuple[str, tuple[Any, ...]], ...] = (
    ("shared_context.deck.value[0].deck_index", ("shared_context", "deck", "value", 0, "deck_index")),
    ("shared_context.potions.value[0].potion_index", ("shared_context", "potions", "value", 0, "potion_index")),
    ("shared_context.relics.value[0].relic_index", ("shared_context", "relics", "value", 0, "relic_index")),
)


def lookup(root: dict[str, Any], path: tuple[Any, ...]) -> Any:
    value: Any = root
    for part in path:
        value = value[part]
    return value


def assign(root: dict[str, Any], path: tuple[Any, ...], value: Any) -> None:
    parent: Any = root
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value


def audit(data_root: Path, git_repo: Path) -> dict[str, Any]:
    witness = LOCK["native_decision_witness"]
    root_path = data_root / witness["root_data_path"]
    run_path = data_root / witness["run_receipt_data_path"]
    root_sha = sha256(root_path)
    run_sha = sha256(run_path)
    if root_sha != witness["root_sha256"] or run_sha != witness["run_receipt_sha256"]:
        raise RuntimeError("locked historical native witness or run receipt changed")
    root = read_json(root_path)
    run = read_json(run_path)
    projection = root["raw_privileged_projection"]
    if root.get("screen") != "CARD_SELECT" or not root.get("candidate_binding_validation_passed"):
        raise RuntimeError("locked witness is not a validated native CARD_SELECT decision")
    if not run.get("completed_native_terminal") or not run.get("terminal_complete"):
        raise RuntimeError("locked native owner run lacks a complete terminal receipt")

    encoder_path = data_root / LOCK["legacy_encoder"]["data_path"]
    encoder = load_legacy_encoder(encoder_path, LOCK["legacy_encoder"]["sha256"])
    matrix, _encoder_audit = encoder.encode(projection, len(root["legal_action_occurrences"]))
    saved = np.asarray(root["semantic_feature_matrix"], dtype=np.float32)
    if not np.array_equal(matrix, saved):
        raise RuntimeError("historical encoder did not reproduce the locked authentic witness matrix")

    with load_pinned_model_module(git_repo, data_root) as model_module:
        # The model classes/functions retain their verified module globals;
        # the temporary source checkout is removed when this context exits.
        pass
    a_lock = LOCK["baseline_A"]
    a_path = data_root / a_lock["checkpoint_data_path"]
    if sha256(a_path) != a_lock["checkpoint_sha256"]:
        raise RuntimeError("frozen A/B1024 checkpoint hash changed")
    a_model = load_policy(a_path, model_module)
    if model_module.model_digest(a_model) != a_lock["model_digest"]:
        raise RuntimeError("frozen A/B1024 model digest changed")
    base_distribution = distributions(matrix, a_model)

    cases: list[dict[str, Any]] = []
    for label, path in INDEX_PATHS:
        original_value = lookup(projection, path)
        if isinstance(original_value, bool) or not isinstance(original_value, int):
            raise RuntimeError(f"unexpected non-integer source index at {label}: {original_value!r}")
        mutated = copy.deepcopy(projection)
        changed_value = original_value + 100_000
        assign(mutated, path, changed_value)
        changed_matrix, _ = encoder.encode(mutated, len(root["legal_action_occurrences"]))
        changed_distribution = distributions(changed_matrix, a_model)
        matrix_changed = not np.array_equal(matrix, changed_matrix)
        max_logit_delta = max(
            abs(a - b) for a, b in zip(base_distribution["logits"], changed_distribution["logits"], strict=True)
        )
        max_probability_delta = max(
            abs(a - b) for a, b in zip(base_distribution["probabilities"], changed_distribution["probabilities"], strict=True)
        )
        envelope = projection["shared_context"][label.split(".")[1]]
        cases.append({
            "path": label,
            "source": envelope.get("source"),
            "visibility": envelope.get("visibility"),
            "availability": envelope.get("availability"),
            "original_value": original_value,
            "controlled_changed_value": changed_value,
            "historical_encoder_matrix_changed": matrix_changed,
            "A_B1024_max_logit_delta": max_logit_delta,
            "A_B1024_max_probability_delta": max_probability_delta,
            "transmitted_to_A_actor_output": max_logit_delta > 0.0,
        })

    return {
        "schema_id": "issue85-historical-backbone-index-audit-v1",
        "audited_utc": datetime.now(timezone.utc).isoformat(),
        "source_audit_verified": True,
        "issue85_clearance": "PRE_PILOT_LEGACY_INDEX_AMENDMENT_APPROVED",
        "planner_decision_comment_id": LOCK["planner_decision_comment_id"],
        "issue85_games_started_by_this_check": 0,
        "historical_witness": {
            "issue73_source_commit": witness["source_commit"],
            "native_source_commit": LOCK["native"]["commit"],
            "native_binary_sha256": LOCK["native"]["compiled_module_sha256"],
            "root_id": root.get("root_id"),
            "owner_seed": root.get("owner_seed"),
            "screen": root.get("screen"),
            "root_sha256": root_sha,
            "run_receipt_sha256": run_sha,
            "complete_native_terminal": run.get("terminal_complete"),
            "issue73_terminal_outcome": run.get("run_outcome"),
            "issue73_terminal_floor": run.get("terminal", {}).get("terminal_floor"),
            "legal_candidates": len(root["legal_action_occurrences"]),
        },
        "frozen_base": {
            "arm": "A / #84 B1024",
            "checkpoint_sha256": a_lock["checkpoint_sha256"],
            "model_digest": model_module.model_digest(a_model),
            "legacy_encoder_sha256": LOCK["legacy_encoder"]["sha256"],
            "recomputed_feature_matrix_matches_locked_witness": True,
        },
        "additional_historical_index_paths": cases,
        "interpretation": [
            "The additional deck_index, potion_index, and relic_index paths supplement target_deck_index in the immutable historical backbone under the pre-pilot Planner amendment.",
            "The 0-to-100000 single-field mutations are out-of-domain structural transmission witnesses, not natural gameplay effect sizes or causal estimates.",
            "No Issue 85 pilot or formal outcomes were produced by this audit.",
        ],
    }


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
    print(json.dumps({"issue85_clearance": report["issue85_clearance"], "report": str(args.report), "cases": report["additional_historical_index_paths"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
