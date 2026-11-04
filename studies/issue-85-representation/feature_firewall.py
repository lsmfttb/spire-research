"""Index-free projections for newly learned Issue 85 representation paths.

The exact locked Issue 73 encoder/backbone is grandfathered with the four
historical positional fields listed in the Issue 85 source lock. This module
creates a separate sanitized copy for every newly introduced sidecar or
residual. Native action occurrence bindings remain outside learned tensors.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_EXACT_EXECUTOR_KEYS = frozenset(
    {
        "action_binding",
        "action_id",
        "action_ids",
        "action_index",
        "action_ordinal",
        "bits",
        "candidate_action",
        "candidate_binding",
        "candidate_id",
        "candidate_ids",
        "candidate_index",
        "candidate_ordinal",
        "deck_index",
        "deck_slot",
        "entity_id",
        "entity_ids",
        "executor_index",
        "executor_id",
        "group_id",
        "group_ids",
        "idx1",
        "idx2",
        "idx3",
        "legal_action_id",
        "legal_action_index",
        "native_action_id",
        "native_action_index",
        "native_action_bits",
        "native_id",
        "opaque_id",
        "opaque_key",
        "occurrence_index",
        "potion_index",
        "relic_index",
        "physical_card_position",
        "physical_card_slot",
        "physical_deck_index",
        "position_index",
        "scope",
        "stable_id",
        "target_deck_index",
        "target_id",
        "target_index",
        "target_position",
    }
)

_NON_FEATURE_METADATA_KEYS = frozenset({"label", "reason", "source"})


def _normalized_key(key: Any) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def is_executor_identifier_key(key: Any) -> bool:
    """Recognize opaque IDs and native positional/ordinal input fields.

    Any index/idx/ordinal field is excluded, including shared-context deck,
    potion and relic positions. Semantic IDs such as ``card_id`` and
    ``potion_id`` remain allowed.
    """
    name = _normalized_key(key)
    if name in _EXACT_EXECUTOR_KEYS or name.startswith("native_action_"):
        return True
    if name in {"candidate", "action", "legal_action", "native_action", "executor", "occurrence", "binding"}:
        return True
    words = set(name.split("_"))
    if words & {"index", "idx", "ordinal", "position", "bits"}:
        return True
    if words & {"binding", "executor", "native", "occurrence"} and words & {"id", "ids", "key", "slot", "token"}:
        return True
    if "occurrence" in words and not words & {"count", "total"}:
        return True
    if words & {"action", "candidate", "legal", "target"} and words & {"id", "ids", "key", "ordinal", "slot", "token"}:
        return True
    if "slot" in words:
        # Capacity/occupancy are semantic current-state quantities, unlike a
        # physical slot identity.
        return not bool(words & {"available", "capacity", "count", "free", "occupied", "total"})
    return False


def sanitize_executor_tree(value: Any) -> Any:
    """Return a deep semantic copy with executor IDs and provenance text removed.

    Lists retain multiplicity and order solely so the caller can align outputs
    to the live legal-action occurrences.  The learned module must apply a
    shared candidate scorer and symmetric set aggregation; list ordinals are
    never emitted as features by this helper.
    """
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = _normalized_key(key)
            if is_executor_identifier_key(key) or normalized in _NON_FEATURE_METADATA_KEYS:
                continue
            output[key] = sanitize_executor_tree(child)
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize_executor_tree(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_executor_tree(item) for item in value]
    return value


def new_path_projection(
    privileged_projection: Mapping[str, Any],
    public_projection: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the sanitized inputs consumed by newly learned Issue 85 modules.

    The caller still owns native freshness/legal-action binding.  Candidate
    arrays are preserved in correspondence order, while the model consumes
    them with a permutation-equivariant per-candidate scorer and symmetric
    context pooling.
    """
    candidates = privileged_projection.get("candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("privileged projection has no ordered candidate sequence")
    if public_projection.get("schema_id") != "native-public-projection-v3":
        raise ValueError("unsupported public projection schema")
    if not isinstance(public_projection.get("candidate_actions"), Mapping):
        raise ValueError("public projection has no candidate action association")
    action_envelope = public_projection["candidate_actions"]
    if action_envelope.get("availability") != "available":
        raise ValueError("public candidate actions are unavailable")
    actions = action_envelope.get("value")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        raise ValueError("public candidate action value is not a sequence")
    if len(candidates) != len(actions):
        raise ValueError("privileged and public candidate counts disagree")

    candidate_rows: list[dict[str, Any]] = []
    for candidate, action in zip(candidates, actions, strict=True):
        if not isinstance(candidate, Mapping) or not isinstance(action, Mapping):
            raise ValueError("candidate/action row is not a mapping")
        features = candidate.get("features")
        if not isinstance(features, Mapping):
            raise ValueError("candidate has no semantic feature mapping")
        candidate_rows.append(
            {
                "kind": action.get("kind"),
                "features": sanitize_executor_tree(features),
                "public_action": sanitize_executor_tree(action),
            }
        )

    result = {
        "schema_id": "issue85-index-free-new-paths-v1",
        "screen": sanitize_executor_tree(privileged_projection.get("screen_identity", {})),
        "shared_context": sanitize_executor_tree(privileged_projection.get("shared_context", {})),
        "public_context": sanitize_executor_tree(public_projection),
        "candidates": candidate_rows,
    }
    assert_index_free(result)
    return result


def assert_index_free(value: Any, path: str = "root") -> None:
    """Fail closed if an opaque executor key remains in a new-path payload."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if is_executor_identifier_key(key):
                raise ValueError(f"opaque executor key remains at {path}.{key}")
            assert_index_free(child, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            assert_index_free(child, f"{path}[{index}]")
