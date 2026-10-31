"""Narrow, source-qualified adapter for the one observed Issue 84 gap.

This does not implement game rules. It translates one exact unsupported
projection marker into the source-backed semantic label for the already-live
native action. Every other unsupported projection remains fail-closed.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import json
from typing import Any

import numpy as np


NATIVE_COMMIT = "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2"
PROJECTION_SCHEMA = "native-privileged-noncombat-choice-projection-v1"
CANDIDATE_ORDER = (
    "StepSimulator::legalActions; ordered occurrences are retained without deduplication"
)
UNSUPPORTED_REASON = "Dead Adventurer phase has no native reward entry"
ACTION_BINDING_SOURCE = (
    "ordered StepSimulator::legalActions occurrence; exact executor binding, not model semantics"
)
CHOICE_SOURCE = "GameContext::chooseEventOption DEAD_ADVENTURER reward codes"
CHOICE_DESCRIPTOR = {
    "choice_type": "search_dead_adventurer_site",
    "effect_type": "native_phase_reward_or_encounter_transition",
}


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _matrix_sha256(matrix: Any) -> str:
    array = np.asarray(matrix)
    digest = hashlib.sha256()
    digest.update(str(array.dtype).encode("ascii"))
    digest.update(json.dumps(list(array.shape), separators=(",", ":")).encode("ascii"))
    digest.update(array.tobytes(order="C"))
    return digest.hexdigest()


def repair_known_dead_adventurer_search_gap(
    projection: Mapping[str, Any], legal_action_count: int,
) -> tuple[Mapping[str, Any], dict[str, Any] | None]:
    """Map only the exact source-supported search action seen in the C witness.

    The native occurrence binding is used only to join the live action and
    decode idx1==0 against the pinned GameContext branch. The index, phase,
    transition, RNG state, and future outcomes are never added to model input.
    The existing issue73 schema already permits oracle_only choice semantics.
    """
    if not isinstance(projection, Mapping):
        return projection, None
    if projection.get("coverage_status") == "supported":
        return projection, None
    if (
        projection.get("schema_id") != PROJECTION_SCHEMA
        or projection.get("native_source_commit") != NATIVE_COMMIT
        or projection.get("audience") != "training_oracle_only"
        or projection.get("public_api_safe") is not False
        or projection.get("candidate_order_source") != CANDIDATE_ORDER
        or projection.get("coverage_status") != "NATIVE_CHOICE_SEMANTIC_NOT_READY"
        or type(legal_action_count) is not int
        or legal_action_count != 3
        or projection.get("candidate_count") != 3
        or projection.get("supported_candidate_count") != 2
    ):
        return projection, None

    screen = projection.get("screen_identity")
    if not isinstance(screen, Mapping) or (
        screen.get("availability") != "available"
        or screen.get("visibility") != "public_now"
        or screen.get("value") != "EVENT_SCREEN"
    ):
        return projection, None

    candidates = projection.get("candidates")
    unsupported = projection.get("unsupported_candidates")
    if (
        not isinstance(candidates, Sequence)
        or isinstance(candidates, (str, bytes))
        or len(candidates) != 3
        or not isinstance(unsupported, Sequence)
        or isinstance(unsupported, (str, bytes))
        or len(unsupported) != 1
    ):
        return projection, None

    candidate = candidates[0]
    unsupported_item = unsupported[0]
    if not isinstance(candidate, Mapping) or not isinstance(unsupported_item, Mapping):
        return projection, None
    features = candidate.get("features")
    binding = candidate.get("candidate_binding")
    binding_value = binding.get("value") if isinstance(binding, Mapping) else None
    action = (
        binding_value.get("candidate_action")
        if isinstance(binding_value, Mapping) else None
    )
    event = features.get("event") if isinstance(features, Mapping) else None
    choice = features.get("choice") if isinstance(features, Mapping) else None
    expected_action = {
        "scope": "game", "kind": "event", "idx1": 0, "idx2": 0, "idx3": 0,
    }
    if (
        candidate.get("coverage_status") != "unsupported"
        or candidate.get("reason") != UNSUPPORTED_REASON
        or unsupported_item.get("candidate_index") != 0
        or unsupported_item.get("kind") != "event"
        or unsupported_item.get("reason") != UNSUPPORTED_REASON
        or not isinstance(binding, Mapping)
        or binding.get("availability") != "available"
        or binding.get("visibility") != "oracle_only"
        or binding.get("source") != ACTION_BINDING_SOURCE
        or not isinstance(binding_value, Mapping)
        or binding_value.get("candidate_index") != 0
        or not isinstance(action, Mapping)
        or any(action.get(key) != value for key, value in expected_action.items())
        or not isinstance(event, Mapping)
        or event != {
            "availability": "available",
            "source": "GameContext::curEvent and constants/Events.h",
            "value": "Dead Adventurer",
            "visibility": "public_now",
        }
        or not isinstance(choice, Mapping)
        or choice.get("availability") != "unavailable"
        or choice.get("reason") != UNSUPPORTED_REASON
        or choice.get("required_for_choice_semantics") is not True
        or set(choice) != {"availability", "reason", "required_for_choice_semantics"}
    ):
        return projection, None

    # Keep every pre-existing candidate object and field byte-for-byte in the
    # supported part of the projection. Only the unsupported action's missing
    # choice descriptor and aggregate coverage counters are completed.
    repaired = deepcopy(projection)
    target = repaired["candidates"][0]
    target["features"]["choice"] = {
        "availability": "available",
        "source": CHOICE_SOURCE,
        "value": dict(CHOICE_DESCRIPTOR),
        "visibility": "oracle_only",
    }
    target["coverage_status"] = "supported"
    target.pop("reason", None)
    repaired["coverage_status"] = "supported"
    repaired["supported_candidate_count"] = 3
    repaired["unsupported_candidates"] = []
    return repaired, {
        "schema_id": "issue84-dead-adventurer-search-coverage-v1",
        "native_source_commit": NATIVE_COMMIT,
        "candidate_index_for_binding_audit_only": 0,
        "binding_is_not_model_input": True,
        "model_semantics_source": CHOICE_SOURCE,
        "model_feature": {
            "choice.choice_type": CHOICE_DESCRIPTOR["choice_type"],
            "choice.effect_type": CHOICE_DESCRIPTOR["effect_type"],
        },
        "model_feature_visibility": "oracle_only",
        "future_outcome_features_added": False,
        "current_phase_or_rng_features_added": False,
        "previously_supported_candidate_features_changed": False,
        "native_module_or_game_rules_changed": False,
    }


class CoverageRepairingEncoder:
    """Apply the one witnessed repair and audit supported-row equivalence.

    For already-supported inputs, this object delegates the original projection
    unchanged. For the exact missing-choice witness, the two existing supported
    candidate rows must hash-identically to the same encoder run on their
    supported-only projection. A mismatch fails closed before policy use.
    """

    def __init__(self, encoder: Any) -> None:
        self._encoder = encoder

    def __getattr__(self, name: str) -> Any:
        return getattr(self._encoder, name)

    def encode(
        self, projection: Mapping[str, Any], legal_action_count: int,
    ) -> tuple[Any, dict[str, Any]]:
        repaired, repair_audit = repair_known_dead_adventurer_search_gap(
            projection, legal_action_count,
        )
        if repair_audit is None:
            return self._encoder.encode(projection, legal_action_count)

        original_candidates = projection["candidates"]
        repaired_candidates = repaired["candidates"]
        original_supported = original_candidates[1:]
        repaired_supported = repaired_candidates[1:]
        if _canonical_sha256(original_supported) != _canonical_sha256(repaired_supported):
            raise RuntimeError("Issue84 coverage repair changed a previously supported candidate")
        original_bindings = [item["candidate_binding"] for item in original_candidates]
        repaired_bindings = [item["candidate_binding"] for item in repaired_candidates]
        if _canonical_sha256(original_bindings) != _canonical_sha256(repaired_bindings):
            raise RuntimeError("Issue84 coverage repair changed native legal-action bindings")

        repaired_matrix, encoder_audit = self._encoder.encode(repaired, legal_action_count)
        supported_projection = deepcopy(projection)
        supported_projection["candidates"] = deepcopy(list(original_supported))
        supported_projection["candidate_count"] = len(original_supported)
        supported_projection["supported_candidate_count"] = len(original_supported)
        supported_projection["coverage_status"] = "supported"
        supported_projection["unsupported_candidates"] = []
        supported_matrix, _ = self._encoder.encode(
            supported_projection, len(original_supported),
        )
        if not np.array_equal(np.asarray(repaired_matrix)[1:], np.asarray(supported_matrix)):
            raise RuntimeError("Issue84 coverage repair changed supported-candidate features")

        if not isinstance(encoder_audit, Mapping):
            raise RuntimeError("Issue84 coverage repair received a malformed encoder audit")
        audit = dict(encoder_audit)
        audit["coverage_repair"] = {
            **repair_audit,
            "original_projection_sha256": _canonical_sha256(projection),
            "repaired_projection_sha256": _canonical_sha256(repaired),
            "previously_supported_candidates_sha256": _canonical_sha256(original_supported),
            "candidate_bindings_before_sha256": _canonical_sha256(original_bindings),
            "candidate_bindings_after_sha256": _canonical_sha256(repaired_bindings),
            "supported_candidate_matrix_sha256": _matrix_sha256(repaired_matrix[1:]),
            "supported_only_matrix_sha256": _matrix_sha256(supported_matrix),
            "supported_candidate_features_equal": True,
            "candidate_order_and_duplicates_preserved": True,
        }
        return repaired_matrix, audit
