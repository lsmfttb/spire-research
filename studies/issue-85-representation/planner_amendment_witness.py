"""Apply Planner's pre-pilot legacy-index amendment to retained evidence.

This is a source/evidence audit only. It does not load a simulator, start a
game, or rerun the historical backbone sensitivity experiment. It verifies
the existing immutable evidence and checks that the current new-path firewall
removes all four grandfathered positional fields from the authentic archived
projection.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from feature_firewall import assert_index_free, sanitize_executor_tree


HERE = Path(__file__).resolve().parent
LOCK = json.loads((HERE / "source-lock.json").read_text(encoding="utf-8"))
EXPECTED_PATHS = (
    "candidates[*].features.target_deck_index",
    "shared_context.deck.value[*].deck_index",
    "shared_context.potions.value[*].potion_index",
    "shared_context.relics.value[*].relic_index",
)
SHARED_FIELDS = (
    ("deck", "deck_index", "GameContext::deck"),
    ("potions", "potion_index", "GameContext::potions"),
    ("relics", "relic_index", "GameContext::relics"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def audit(data_root: Path) -> dict[str, Any]:
    errors: list[str] = []

    def require(condition: bool, name: str, detail: Any = None) -> None:
        if not condition:
            errors.append(f"{name}: {detail if detail is not None else 'mismatch'}")

    amendment = LOCK["legacy_backbone_index_amendment"]
    paths = amendment["historical_legacy_index_paths"]
    producer_sources = {
        relative: sha256(HERE / relative)
        for relative in (
            "contract.json",
            "feature_firewall.py",
            "planner_amendment_witness.py",
            "source-lock.json",
        )
    }
    require(LOCK["schema_id"] == "issue85-source-lock-v2", "source_lock_schema")
    require(LOCK["planner_decision_comment_id"] == 6621197800, "planner_decision_comment_id")
    require(
        tuple(row["path"] for row in paths) == EXPECTED_PATHS,
        "planner_path_list",
        [row.get("path") for row in paths],
    )

    # Reuse and hash-check completed evidence; do not rerun historical model
    # inference or the prior three shared-index sensitivity mutations.
    evidence_checks: list[dict[str, Any]] = []
    evidence_specs = [
        *LOCK["prior_preflight_evidence"],
        {
            "report_data_path": amendment["audit_report_data_path"],
            "sha256": amendment["audit_report_sha256"],
            "purpose": "Previously completed shared-context backbone transmission audit.",
        },
    ]
    evidence_by_path: dict[str, Any] = {}
    for item in evidence_specs:
        path = data_root / item["report_data_path"]
        if not path.is_file():
            errors.append(f"prior_evidence_missing: {path}")
            continue
        actual = sha256(path)
        require(actual == item["sha256"], f"prior_evidence_sha256:{path.name}", actual)
        payload = read_json(path)
        evidence_by_path[item["report_data_path"]] = payload
        evidence_checks.append(
            {
                "path": str(path),
                "sha256": actual,
                "verified": payload.get("verified", payload.get("source_audit_verified")),
            }
        )

    old_native = evidence_by_path.get(LOCK["prior_preflight_evidence"][1]["report_data_path"], {})
    old_outputs = old_native.get("locked_policy_outputs_on_authentic_decision", {})
    require(old_native.get("verified") is True, "retained_native_witness_verified")
    require(
        old_outputs.get("P_model_digest") == LOCK["baseline_P"]["model_digest"],
        "retained_parent_model_digest",
        old_outputs.get("P_model_digest"),
    )
    require(
        old_outputs.get("A_model_digest") == LOCK["baseline_A"]["model_digest"],
        "retained_baseline_model_digest",
        old_outputs.get("A_model_digest"),
    )

    historical_audit = evidence_by_path.get(amendment["audit_report_data_path"], {})
    require(historical_audit.get("source_audit_verified") is True, "retained_index_audit_verified")
    require(historical_audit.get("issue85_games_started_by_this_check") == 0, "retained_audit_game_count")
    additional = historical_audit.get("additional_historical_index_paths", [])
    require(
        {str(row.get("path", "")).replace("[0]", "[*]") for row in additional}
        == set(EXPECTED_PATHS[1:]),
        "retained_shared_index_audit_paths",
        [row.get("path") for row in additional],
    )
    for row in additional:
        require(row.get("historical_encoder_matrix_changed") is True, f"legacy_matrix_transmission:{row.get('path')}")
        require(row.get("transmitted_to_A_actor_output") is True, f"legacy_actor_transmission:{row.get('path')}")

    witness_lock = LOCK["native_decision_witness"]
    root_path = data_root / witness_lock["root_data_path"]
    require(root_path.is_file(), "authentic_root_exists", str(root_path))
    root: dict[str, Any] = read_json(root_path) if root_path.is_file() else {}
    actual_root_sha = sha256(root_path) if root_path.is_file() else None
    require(actual_root_sha == witness_lock["root_sha256"], "authentic_root_sha256", actual_root_sha)
    projection = root.get("raw_privileged_projection", {})
    candidates = projection.get("candidates", [])
    shared = projection.get("shared_context", {})
    legal = root.get("legal_action_occurrences", [])

    target_values: list[int] = []
    target_sources: set[str] = set()
    for i, candidate in enumerate(candidates):
        field = candidate.get("features", {}).get("target_deck_index", {})
        require(field.get("availability") == "available", f"target_available_{i}")
        require(field.get("visibility") == "oracle_only", f"target_visibility_{i}", field.get("visibility"))
        target_sources.add(str(field.get("source")))
        value = field.get("value")
        require(isinstance(value, int) and not isinstance(value, bool), f"target_index_type_{i}", value)
        if isinstance(value, int) and not isinstance(value, bool):
            target_values.append(value)
    require(bool(candidates) and len(candidates) == len(legal), "candidate_occurrence_alignment")
    require(target_sources == {"SelectScreenCard::deckIdx; physical target occurrence for execution"}, "target_native_source", sorted(target_sources))

    shared_value_counts: dict[str, int] = {}
    for collection, field_name, source in SHARED_FIELDS:
        envelope = shared.get(collection, {})
        require(envelope.get("availability") == "available", f"{collection}_available")
        require(envelope.get("visibility") == "public_now", f"{collection}_visibility", envelope.get("visibility"))
        require(envelope.get("source") == source, f"{collection}_source", envelope.get("source"))
        values = envelope.get("value", [])
        require(isinstance(values, list) and bool(values), f"{collection}_nonempty")
        good = [
            item.get(field_name)
            for item in values
            if isinstance(item, dict)
            and isinstance(item.get(field_name), int)
            and not isinstance(item.get(field_name), bool)
        ]
        require(len(good) == len(values), f"{collection}_index_values")
        shared_value_counts[collection] = len(good)

    # Exercise the exact recursive sanitization used by new representations
    # on the archived authentic native projection, changing one raw index at a
    # time. This verifies invariance, not an in-domain behavioral effect.
    cleaned = sanitize_executor_tree(projection)
    opaque_keys_absent = True
    try:
        assert_index_free(cleaned)
    except ValueError as exc:
        opaque_keys_absent = False
        errors.append(f"authentic_new_path_contains_executor_key: {exc}")

    mutation_witnesses: list[dict[str, Any]] = []
    locations = [
        (EXPECTED_PATHS[0], [("candidate", i, "features", "target_deck_index") for i in range(len(candidates))]),
        *[
            (
                EXPECTED_PATHS[index + 1],
                [("shared", collection, "value", i, field_name) for i in range(shared_value_counts[collection])],
            )
            for index, (collection, field_name, _) in enumerate(SHARED_FIELDS)
        ],
    ]
    for path, locs in locations:
        for occurrence, location in enumerate(locs):
            changed = copy.deepcopy(projection)
            if location[0] == "candidate":
                _, i, group, key = location
                envelope = changed["candidates"][i][group][key]
                old_value = envelope["value"]
                envelope["value"] = old_value + 100_000 + occurrence
            else:
                _, collection, group, i, key = location
                row = changed["shared_context"][collection][group][i]
                old_value = row[key]
                row[key] = old_value + 100_000 + occurrence
            sanitized_changed = sanitize_executor_tree(changed)
            require(
                sanitized_changed == cleaned,
                f"new_path_index_invariance:{path}:{occurrence}",
            )
            mutation_witnesses.append({"path": path, "occurrence": occurrence, "invariant": sanitized_changed == cleaned})

    original_deck = shared.get("deck", {}).get("value", [])
    cleaned_deck = cleaned.get("shared_context", {}).get("deck", {}).get("value", [])
    deck_semantics = Counter(json.dumps({k: v for k, v in row.items() if k != "deck_index"}, sort_keys=True) for row in original_deck)
    cleaned_deck_semantics = Counter(json.dumps(row, sort_keys=True) for row in cleaned_deck)
    require(deck_semantics == cleaned_deck_semantics, "deck_semantic_attributes_and_duplicate_multiplicity_preserved")

    return {
        "schema_id": "issue85-planner-index-amendment-witness-v1",
        "verified_utc": datetime.now(timezone.utc).isoformat(),
        "source_lock_schema": LOCK["schema_id"],
        "producer_source_sha256": producer_sources,
        "planner_decision_comment_id": LOCK["planner_decision_comment_id"],
        "verified": not errors,
        "pilot_ready": False,
        "issue85_games_started_by_this_check": 0,
        "authentic_fixture": {
            "root_data_path": str(root_path),
            "root_sha256": actual_root_sha,
            "screen": root.get("screen"),
            "historical_issue73_terminal_complete": historical_audit.get("historical_witness", {}).get("complete_native_terminal"),
            "candidate_occurrences": len(candidates),
            "target_deck_index_values": target_values,
            "shared_index_value_counts": shared_value_counts,
        },
        "retained_prior_evidence": evidence_checks,
        "new_path_firewall": {
            "authentic_projection_opaque_keys_absent": opaque_keys_absent,
            "single_field_mutations_tested": len(mutation_witnesses),
            "all_new_path_index_mutations_invariant": all(item["invariant"] for item in mutation_witnesses),
            "mutation_witnesses": mutation_witnesses,
            "deck_semantics_and_duplicate_occurrences_preserved": deck_semantics == cleaned_deck_semantics,
            "candidate_occurrence_count_preserved": len(candidates) == len(legal),
        },
        "b_to_f_initial_policy_parity": "NOT_IMPLEMENTED_OR_VERIFIED",
        "limitations": [
            "The authentic public projection retained with this Issue 73 root is schema v1; this check applies the sanitizer to the archived raw privileged projection, while the v3 projection integration is covered by focused synthetic unit tests.",
            "B-F architectures are not implemented yet, so their exact pi12 initial-policy equivalence on this native decision is unverified and pilot remains blocked.",
            "The existing 0-to-100000 historical-backbone mutations are out-of-domain structural sensitivity witnesses, not natural gameplay effect sizes.",
        ],
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.data_root.resolve())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"verified": report["verified"], "pilot_ready": report["pilot_ready"], "report": str(args.report), "errors": report["errors"]}, sort_keys=True))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
