"""Index-free projections for newly learned Issue 85 representation paths.

The exact locked Issue 73 encoder/backbone is grandfathered with the four
historical positional fields listed in the Issue 85 source lock. This module
creates a separate sanitized copy for every newly introduced sidecar or
residual. Native action occurrence bindings remain outside learned tensors.
"""
from __future__ import annotations

import hashlib
import json
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

# These are the complete input field sets emitted by the exact locked native
# choice projections.  They are deliberately allowlists: sanitizing unknown
# fields after the fact is not a security boundary because an executor alias
# can use any previously unseen name.
_PRIVILEGED_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_id", "audience", "public_api_safe", "policy_training_authorized",
        "screen_identity", "native_source_commit", "candidate_order_source",
        "shared_context", "candidates", "candidate_count",
        "supported_candidate_count", "unsupported_candidates", "coverage_status",
        "public_information_note",
    }
)
_PRIVILEGED_CANDIDATE_KEYS = frozenset(
    {"coverage_status", "reason", "candidate_binding", "features"}
)
_FEATURE_ROOT_KEYS = frozenset(
    {
        "potion", "choice_type", "effect_type", "sacred_bark", "effect_amount_hp",
        "effect_amount_max_hp", "free_slots", "result_potions", "event", "choice",
        "revealed_card_identity", "first_card_identity", "second_card_identity",
        "cards_match", "random_relic", "result_card", "result_potion", "result_relic", "target_card",
        "target_deck_index", "target_relic", "oracle_destination_encounter",
        "oracle_destination_room", "public_destination", "offered_relic",
        "selection_purpose", "selection_count", "chest_size", "relic_tier",
        "unrevealed_loot", "heal_amount", "target_family", "key",
        "current_strength", "gold_available", "item", "price",
    }
)
_FEATURE_ENVELOPE_KEYS = frozenset(
    {"availability", "visibility", "source", "required_for_choice_semantics", "reason", "value"}
)
_ACTION_KEYS = frozenset({"scope", "kind", "idx1", "idx2", "idx3", "bits", "label"})
_CARD_KEYS = frozenset(
    {"id", "id_label", "name", "type", "rarity", "upgraded", "upgrade_count", "misc", "deck_index"}
)
_POTION_KEYS = frozenset({"id", "id_label", "name", "potion_index"})
_RELIC_KEYS = frozenset({"id", "id_label", "name", "counter", "relic_index"})
_SHARED_CONTEXT_KEYS = frozenset(
    {"current_hp", "max_hp", "gold", "floor", "act", "potions", "deck", "relics"}
)
_CHOICE_VALUE_KEYS = frozenset(
    {
        "apparition_count", "attempts_remaining", "bonus_effect_type", "bonus_id",
        "bonus_name", "card_index", "card_rarity", "card", "cards_match",
        "choice_type", "colorless_card_reward_count", "current_phase",
        "current_tome_phase", "description", "drawback_effect_type", "drawback_id",
        "drawback_name", "effect_type", "first_card_index", "gold_amount_max",
        "gold_amount_min", "gold_amount", "gold_cost", "gold", "heal_amount_hp",
        "hp_cost", "key", "madness_count", "max_hp_amount", "max_hp_cost",
        "max_hp_gain", "next_hp_cost", "normality_count", "phase_reward_kind",
        "phase", "potion_id", "potion_index", "potion_reward_count", "potion",
        "relic_removed_with_key", "relic", "sapphire_key_is_lost", "second_card_index",
        "selection_count", "selection_purpose", "source_fallthrough_to_option_4",
        "source_fallthrough_to_option_5", "target_family",
        # Exact source-backed snapshots nested in choices.
        "id", "id_label", "name", "type", "rarity", "upgraded", "upgrade_count",
        "misc", "deck_index", "relic_index", "counter",
    }
)
_PUBLIC_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_id", "screen_identity", "visible_act_boss", "visible_map_graph",
        "current_map_node", "immediately_legal_routes", "persistent_resources",
        "screen_payload", "candidate_actions",
    }
)
_LOCKED_NATIVE_SOURCE_COMMIT = "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2"
_PUBLIC_ENVELOPE_KEYS = frozenset({"availability", "visibility", "source", "reason", "value"})
_PUBLIC_RESOURCE_KEYS = frozenset(
    {
        "current_hp", "max_hp", "gold", "potion_count", "potion_capacity", "deck",
        "relics", "potion_identities", "keys",
    }
)
_PUBLIC_KEY_FLAGS = frozenset({"blue_key", "green_key", "red_key"})
_MAP_NODE_KEYS = frozenset({"x", "y", "room_symbol", "outgoing_edges"})
_MAP_LOCATION_KEYS = frozenset({"location", "x", "y", "room_symbol"})
_CURRENT_MAP_LOCATION_VALUES = frozenset({"before_first_route", "act_boss_entry", "map_node"})
_ROUTE_MAP_LOCATION_VALUES = frozenset({"boss_entry", "map_node"})
_SCREEN_PAYLOAD_KEYS = frozenset(
    {
        "coverage_status", "reason", "source", "choices", "map_graph",
        "current_map_node", "legal_routes", "missing_facts", "cards",
        "hidden_card_note", "event", "attempts_remaining", "phase", "first_card_index",
    }
)
_PUBLIC_CHOICE_KEYS = frozenset(
    {
        "candidate_index", "candidate_action", "choice_type", "destination", "card",
        "gold", "relic", "potion", "key", "relic_removed_with_key",
        "sapphire_key_is_lost", "max_hp_gain", "description", "card_index",
        "first_card_index",
    }
)
_CARD_SLOT_KEYS = frozenset({"index", "state", "card"})
_CARD_SLOT_STATES = frozenset(
    {"matched_hidden", "revealed", "first_pick_revealed", "face_down"}
)
_PUBLIC_ENVELOPE_FIELDS = (
    "screen_identity",
    "visible_act_boss",
    "visible_map_graph",
    "current_map_node",
    "immediately_legal_routes",
    "persistent_resources",
    "screen_payload",
    "candidate_actions",
)


def _check_keys(value: Any, allowed: frozenset[str], path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a mapping")
    unknown = sorted(str(key) for key in value if not isinstance(key, str) or key not in allowed)
    if unknown:
        raise ValueError(f"unrecognized input field at {path}: {unknown[0]}")
    return value


def _check_snapshot(value: Any, allowed: frozenset[str], path: str) -> None:
    row = _check_keys(value, allowed, path)
    for key, child in row.items():
        if not (child is None or isinstance(child, (str, int, float, bool))):
            raise ValueError(f"{path}.{key} must be a scalar snapshot field")


def _check_feature_envelope(envelope: Any, path: str) -> Any:
    row = _check_keys(envelope, _FEATURE_ENVELOPE_KEYS, path)
    availability = row.get("availability")
    if availability not in {"available", "unavailable"}:
        raise ValueError(f"{path}.availability is not source-schema valid")
    if row.get("visibility") not in {None, "public_now", "oracle_only"}:
        raise ValueError(f"{path}.visibility is not source-schema valid")
    if "source" in row and not isinstance(row["source"], str):
        raise ValueError(f"{path}.source must be source provenance text")
    if "reason" in row and not isinstance(row["reason"], str):
        raise ValueError(f"{path}.reason must be text")
    if "required_for_choice_semantics" in row and not isinstance(row["required_for_choice_semantics"], bool):
        raise ValueError(f"{path}.required_for_choice_semantics must be boolean")
    if availability == "unavailable":
        if "value" in row:
            raise ValueError(f"{path}.value must be omitted when availability is unavailable")
        return None
    if "value" not in row:
        return None
    return row["value"]


def _explicit_public_visibility(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the pinned public-v3 envelope schema to explicit visibility.

    The locked native publicProjectionV3 API has no visibility member because
    these fields are public observations by construction. The learned payload
    keeps that contract explicit instead of leaking a missing/None tag.
    """
    normalized = dict(value)
    for field in _PUBLIC_ENVELOPE_FIELDS:
        envelope = normalized.get(field)
        if not isinstance(envelope, Mapping):
            continue
        tagged = dict(envelope)
        tagged["visibility"] = "public_now"
        normalized[field] = tagged
    return normalized


def _check_action(value: Any, path: str) -> None:
    row = _check_keys(value, _ACTION_KEYS, path)
    _native_action_identity(row, path)


def _check_privileged_projection(value: Any) -> None:
    root = _check_keys(value, _PRIVILEGED_TOP_LEVEL_KEYS, "privileged_projection")
    if root.get("schema_id") != "native-privileged-noncombat-choice-projection-v1":
        raise ValueError("unsupported privileged projection schema")
    if root.get("native_source_commit") != _LOCKED_NATIVE_SOURCE_COMMIT:
        raise ValueError("privileged projection does not match the locked native source commit")
    if (
        root.get("audience") != "training_oracle_only"
        or root.get("public_api_safe") is not False
        or root.get("policy_training_authorized") is not False
    ):
        raise ValueError("privileged projection audience/safety metadata is invalid")
    _check_feature_envelope(root.get("screen_identity"), "screen_identity")
    shared = _check_keys(root.get("shared_context"), _SHARED_CONTEXT_KEYS, "shared_context")
    for name, envelope in shared.items():
        payload = _check_feature_envelope(envelope, f"shared_context.{name}")
        if payload is None:
            continue
        if name == "deck":
            if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
                raise ValueError("shared_context.deck.value must be a sequence")
            for i, item in enumerate(payload):
                _check_snapshot(item, _CARD_KEYS, f"shared_context.deck.value[{i}]")
        elif name == "potions":
            if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
                raise ValueError("shared_context.potions.value must be a sequence")
            for i, item in enumerate(payload):
                _check_snapshot(item, _POTION_KEYS, f"shared_context.potions.value[{i}]")
        elif name == "relics":
            if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
                raise ValueError("shared_context.relics.value must be a sequence")
            for i, item in enumerate(payload):
                _check_snapshot(item, _RELIC_KEYS, f"shared_context.relics.value[{i}]")
        elif isinstance(payload, (Mapping, Sequence)) and not isinstance(payload, (str, bytes)):
            raise ValueError(f"shared_context.{name}.value must be a scalar")

    candidates = root.get("candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("privileged projection candidates must be an ordered sequence")
    for index, candidate in enumerate(candidates):
        row = _check_keys(candidate, _PRIVILEGED_CANDIDATE_KEYS, f"candidates[{index}]")
        binding = _check_keys(row.get("candidate_binding"), _FEATURE_ENVELOPE_KEYS, f"candidates[{index}].candidate_binding")
        _check_feature_envelope(binding, f"candidates[{index}].candidate_binding")
        if binding.get("availability") != "available" or binding.get("visibility") != "oracle_only":
            raise ValueError(f"candidates[{index}] has invalid native binding provenance")
        binding_value = _check_keys(
            binding.get("value"), frozenset({"candidate_index", "candidate_action"}),
            f"candidates[{index}].candidate_binding.value",
        )
        _check_action(binding_value.get("candidate_action"), f"candidates[{index}].candidate_binding.value.candidate_action")
        features = _check_keys(row.get("features"), _FEATURE_ROOT_KEYS, f"candidates[{index}].features")
        for name, envelope in features.items():
            payload = _check_feature_envelope(envelope, f"candidates[{index}].features.{name}")
            if payload is None:
                continue
            if name == "choice":
                _check_known_semantic_tree(payload, _CHOICE_VALUE_KEYS, f"candidates[{index}].features.choice.value")
            elif name in {"target_card", "first_card_identity", "second_card_identity", "revealed_card_identity"}:
                _check_snapshot(payload, _CARD_KEYS, f"candidates[{index}].features.{name}.value")
            elif name in {"target_relic", "offered_relic"}:
                _check_snapshot(payload, _RELIC_KEYS, f"candidates[{index}].features.{name}.value")
            elif name == "potion":
                _check_snapshot(payload, _POTION_KEYS, f"candidates[{index}].features.potion.value")
            elif name == "item":
                _check_known_semantic_tree(payload, frozenset(_CARD_KEYS | _POTION_KEYS | _RELIC_KEYS), f"candidates[{index}].features.item.value")
            elif isinstance(payload, (Mapping, Sequence)) and not isinstance(payload, (str, bytes)):
                raise ValueError(f"candidates[{index}].features.{name}.value must be a scalar")


def _check_known_semantic_tree(value: Any, allowed: frozenset[str], path: str) -> None:
    if isinstance(value, Mapping):
        row = _check_keys(value, allowed, path)
        for key, child in row.items():
            if isinstance(child, Mapping):
                snapshot = {"card": _CARD_KEYS, "relic": _RELIC_KEYS, "potion": _POTION_KEYS, "relic_removed_with_key": _RELIC_KEYS}.get(key)
                if snapshot is None:
                    raise ValueError(f"{path}.{key} must be a scalar source feature")
                _check_snapshot(child, snapshot, f"{path}.{key}")
            elif isinstance(child, Sequence) and not isinstance(child, (str, bytes, bytearray)):
                raise ValueError(f"{path}.{key} must be a scalar source feature")
            elif child is not None and not isinstance(child, (str, int, float, bool)):
                raise ValueError(f"{path}.{key} has a non-JSON source value")


def _check_public_projection(value: Any) -> None:
    root = _check_keys(value, _PUBLIC_TOP_LEVEL_KEYS, "public_projection")
    if root.get("schema_id") != "native-public-projection-v3":
        raise ValueError("unsupported public projection schema")
    for name in ("screen_identity", "visible_act_boss"):
        if name in root:
            envelope = _check_keys(root[name], _PUBLIC_ENVELOPE_KEYS, name)
            _check_public_envelope(envelope, name)
            if isinstance(envelope.get("value"), (Mapping, Sequence)) and not isinstance(envelope.get("value"), (str, bytes)):
                raise ValueError(f"{name}.value must be a scalar")
    if "candidate_actions" in root:
        envelope = _check_keys(root["candidate_actions"], _PUBLIC_ENVELOPE_KEYS, "candidate_actions")
        _check_public_envelope(envelope, "candidate_actions")
        actions = envelope.get("value", ())
        if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
            raise ValueError("candidate_actions.value must be an ordered sequence")
        for i, action in enumerate(actions):
            _check_action(action, f"candidate_actions.value[{i}]")
    if "persistent_resources" in root:
        envelope = _check_keys(root["persistent_resources"], _PUBLIC_ENVELOPE_KEYS, "persistent_resources")
        _check_public_envelope(envelope, "persistent_resources")
        if envelope.get("availability") == "available":
            resources = _check_keys(envelope.get("value"), _PUBLIC_RESOURCE_KEYS, "persistent_resources.value")
            for field in {"current_hp", "max_hp", "gold", "potion_count", "potion_capacity"}:
                if field not in resources:
                    continue
                available, field_value = _public_resource_field(
                    resources[field], f"persistent_resources.value.{field}"
                )
                if available and (isinstance(field_value, bool) or not isinstance(field_value, int)):
                    raise ValueError(f"persistent_resources.value.{field}.value must be an integer")
            for field, allowed in (("deck", _CARD_KEYS), ("relics", _RELIC_KEYS), ("potion_identities", _POTION_KEYS)):
                if field not in resources:
                    continue
                available, entries = _public_resource_field(
                    resources[field], f"persistent_resources.value.{field}"
                )
                if not available:
                    continue
                if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
                    raise ValueError(f"persistent_resources.value.{field}.value must be a sequence")
                for i, item in enumerate(entries):
                    _check_snapshot(item, allowed, f"persistent_resources.value.{field}.value[{i}]")
            if "keys" in resources:
                available, key_value = _public_resource_field(
                    resources["keys"], "persistent_resources.value.keys"
                )
                if available:
                    flags = _check_keys(key_value, _PUBLIC_KEY_FLAGS, "persistent_resources.value.keys.value")
                    if any(not isinstance(flag, bool) for flag in flags.values()):
                        raise ValueError("persistent_resources.value.keys.value flags must be booleans")
        elif "value" in envelope:
            raise ValueError("persistent_resources.value must be omitted when availability is unavailable")
    if "visible_map_graph" in root:
        envelope = _check_keys(root["visible_map_graph"], _PUBLIC_ENVELOPE_KEYS, "visible_map_graph")
        _check_public_envelope(envelope, "visible_map_graph")
        if "value" in envelope:
            _check_map_graph(envelope["value"], "visible_map_graph.value")
    if "current_map_node" in root:
        envelope = _check_keys(root["current_map_node"], _PUBLIC_ENVELOPE_KEYS, "current_map_node")
        _check_public_envelope(envelope, "current_map_node")
        if envelope.get("availability") == "available":
            _check_map_location(
                envelope.get("value"), "current_map_node.value", _CURRENT_MAP_LOCATION_VALUES
            )
    if "immediately_legal_routes" in root:
        _check_public_route_list(root["immediately_legal_routes"], "immediately_legal_routes")
    if "screen_payload" in root:
        envelope = _check_keys(root["screen_payload"], _PUBLIC_ENVELOPE_KEYS, "screen_payload")
        _check_public_envelope(envelope, "screen_payload")
        payload = envelope.get("value")
        if isinstance(payload, Mapping):
            screen = _check_keys(payload, _SCREEN_PAYLOAD_KEYS, "screen_payload.value")
            for field in {"coverage_status", "reason", "source", "hidden_card_note", "event", "phase"}:
                if field in screen and not isinstance(screen[field], str):
                    raise ValueError(f"screen_payload.value.{field} must be text")
            for field in {"attempts_remaining", "first_card_index"}:
                if field in screen and (isinstance(screen[field], bool) or not isinstance(screen[field], int)):
                    raise ValueError(f"screen_payload.value.{field} must be an integer")
            if "missing_facts" in screen and (
                not isinstance(screen["missing_facts"], Sequence)
                or isinstance(screen["missing_facts"], (str, bytes))
                or any(not isinstance(fact, str) for fact in screen["missing_facts"])
            ):
                raise ValueError("screen_payload.value.missing_facts must be a sequence of text facts")
            if "choices" in screen:
                _check_public_route_list({"availability": "available", "value": screen["choices"]}, "screen_payload.value.choices")
            if "cards" in screen:
                slots = screen["cards"]
                if not isinstance(slots, Sequence) or isinstance(slots, (str, bytes)):
                    raise ValueError("screen_payload.value.cards must be a sequence")
                for i, slot in enumerate(slots):
                    row = _check_keys(slot, _CARD_SLOT_KEYS, f"screen_payload.value.cards[{i}]")
                    state = row.get("state")
                    if "state" in row and (
                        not isinstance(state, str) or state not in _CARD_SLOT_STATES
                    ):
                        raise ValueError(
                            f"screen_payload.value.cards[{i}].state is not a native public card-state enum"
                        )
                    if isinstance(row.get("card"), Mapping):
                        _check_snapshot(row["card"], _CARD_KEYS, f"screen_payload.value.cards[{i}].card")
            if "map_graph" in screen:
                _check_map_graph(screen["map_graph"], "screen_payload.value.map_graph")
            if "current_map_node" in screen:
                _check_map_location(
                    screen["current_map_node"],
                    "screen_payload.value.current_map_node",
                    _CURRENT_MAP_LOCATION_VALUES,
                )
            if "legal_routes" in screen:
                _check_public_route_list(
                    {"availability": "available", "value": screen["legal_routes"]},
                    "screen_payload.value.legal_routes",
                )


def _check_public_route_list(envelope: Any, path: str) -> None:
    row = _check_keys(envelope, _PUBLIC_ENVELOPE_KEYS, path)
    _check_public_envelope(row, path)
    routes = row.get("value", ())
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
        raise ValueError(f"{path}.value must be a sequence")
    for i, route in enumerate(routes):
        item = _check_keys(route, _PUBLIC_CHOICE_KEYS, f"{path}.value[{i}]")
        for field in {"choice_type", "description", "key"}:
            if field in item and not isinstance(item[field], str):
                raise ValueError(f"{path}.value[{i}].{field} must be text")
        for field in {"candidate_index", "gold", "max_hp_gain", "card_index", "first_card_index"}:
            if field in item and (isinstance(item[field], bool) or not isinstance(item[field], int)):
                raise ValueError(f"{path}.value[{i}].{field} must be an integer")
        if "sapphire_key_is_lost" in item and not isinstance(item["sapphire_key_is_lost"], bool):
            raise ValueError(f"{path}.value[{i}].sapphire_key_is_lost must be boolean")
        if "candidate_action" in item:
            _check_action(item["candidate_action"], f"{path}.value[{i}].candidate_action")
        if "destination" in item:
            _check_map_location(
                item["destination"],
                f"{path}.value[{i}].destination",
                _ROUTE_MAP_LOCATION_VALUES,
            )
        for key, allowed in (("card", _CARD_KEYS), ("relic", _RELIC_KEYS), ("relic_removed_with_key", _RELIC_KEYS), ("potion", _POTION_KEYS)):
            if isinstance(item.get(key), Mapping):
                _check_snapshot(item[key], allowed, f"{path}.value[{i}].{key}")


def _check_map_graph(graph: Any, path: str) -> None:
    if not isinstance(graph, Sequence) or isinstance(graph, (str, bytes)):
        raise ValueError(f"{path} must be a sequence")
    for i, node in enumerate(graph):
        row = _check_keys(node, _MAP_NODE_KEYS, f"{path}[{i}]")
        if any(isinstance(row.get(key), bool) or not isinstance(row.get(key), int) for key in ("x", "y")):
            raise ValueError(f"{path}[{i}] map coordinates must be integers")
        if not isinstance(row.get("room_symbol"), str):
            raise ValueError(f"{path}[{i}].room_symbol must be text")
        edges = row.get("outgoing_edges", ())
        if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes)):
            raise ValueError(f"{path}[{i}].outgoing_edges must be a sequence")
        for j, edge in enumerate(edges):
            _check_keys(edge, frozenset({"x", "y"}), f"{path}[{i}].outgoing_edges[{j}]")
            if any(isinstance(edge.get(key), bool) or not isinstance(edge.get(key), int) for key in ("x", "y")):
                raise ValueError(f"{path}[{i}].outgoing_edges[{j}] coordinates must be integers")


def _check_map_location(value: Any, path: str, allowed_locations: frozenset[str]) -> None:
    row = _check_keys(value, _MAP_LOCATION_KEYS, path)
    location = row.get("location")
    if not isinstance(location, str) or location not in allowed_locations:
        raise ValueError(f"{path}.location is not a native map-location enum")
    if "room_symbol" in row and not isinstance(row["room_symbol"], str):
        raise ValueError(f"{path}.room_symbol must be text")
    for key in ("x", "y"):
        if key in row and (isinstance(row[key], bool) or not isinstance(row[key], int)):
            raise ValueError(f"{path}.{key} must be an integer")


def _check_public_envelope(envelope: Mapping[str, Any], path: str) -> None:
    availability = envelope.get("availability")
    if availability not in {"available", "unavailable"}:
        raise ValueError(f"{path}.availability is not source-schema valid")
    if envelope.get("visibility") not in {None, "public_now"}:
        raise ValueError(f"{path}.visibility must be public_now")
    for field in ("source", "reason"):
        if field in envelope and not isinstance(envelope[field], str):
            raise ValueError(f"{path}.{field} must be text")


def _public_resource_field(value: Any, path: str) -> tuple[bool, Any]:
    """Validate and unwrap one nested resource envelope from native V3."""
    envelope = _check_keys(value, _PUBLIC_ENVELOPE_KEYS, path)
    _check_public_envelope(envelope, path)
    if envelope.get("availability") == "available":
        if "value" not in envelope:
            raise ValueError(f"{path} is available but has no value")
        return True, envelope["value"]
    if "value" in envelope:
        raise ValueError(f"{path}.value must be omitted when availability is unavailable")
    return False, None


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
    context pooling.  Both source envelopes are validated against explicit
    field schemas before any sanitizer runs; an unknown key is a hard error.
    """
    _check_privileged_projection(privileged_projection)
    _check_public_projection(public_projection)
    public_projection = _explicit_public_visibility(public_projection)
    candidates = privileged_projection.get("candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("privileged projection has no ordered candidate sequence")
    if public_projection.get("schema_id") != "native-public-projection-v3":
        raise ValueError("unsupported public projection schema")
    privileged_screen = privileged_projection.get("screen_identity")
    public_screen = public_projection.get("screen_identity")
    if (
        not isinstance(privileged_screen, Mapping)
        or privileged_screen.get("availability") != "available"
        or privileged_screen.get("visibility") != "public_now"
        or not isinstance(public_screen, Mapping)
        or public_screen.get("availability") != "available"
        or public_screen.get("visibility") != "public_now"
        or privileged_screen.get("value") != public_screen.get("value")
    ):
        raise ValueError("privileged/public projections do not bind the same available current screen")
    if not isinstance(public_projection.get("candidate_actions"), Mapping):
        raise ValueError("public projection has no candidate action association")
    action_envelope = public_projection["candidate_actions"]
    if action_envelope.get("availability") != "available" or action_envelope.get("visibility") != "public_now":
        raise ValueError("public candidate actions are unavailable")
    actions = action_envelope.get("value")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        raise ValueError("public candidate action value is not a sequence")
    if len(candidates) != len(actions):
        raise ValueError("privileged and public candidate counts disagree")

    candidate_rows: list[dict[str, Any]] = []
    action_identities: list[dict[str, Any]] = []
    for index, (candidate, action) in enumerate(zip(candidates, actions, strict=True)):
        if not isinstance(candidate, Mapping) or not isinstance(action, Mapping):
            raise ValueError("candidate/action row is not a mapping")
        identity = _native_action_identity(action, f"candidate_actions[{index}]")
        binding = candidate.get("candidate_binding")
        if (
            not isinstance(binding, Mapping)
            or binding.get("availability") != "available"
            or binding.get("visibility") != "oracle_only"
        ):
            raise ValueError(f"privileged candidate {index} has no available native occurrence binding")
        binding_value = binding.get("value")
        if not isinstance(binding_value, Mapping):
            raise ValueError(f"privileged candidate {index} binding value is not a mapping")
        bound_index = binding_value.get("candidate_index")
        if isinstance(bound_index, bool) or not isinstance(bound_index, int) or bound_index != index:
            raise ValueError(f"privileged candidate {index} is bound to a different legal occurrence")
        bound_action = binding_value.get("candidate_action")
        if _native_action_identity(bound_action, f"candidate_binding[{index}]") != identity:
            raise ValueError(f"privileged/public native action identity mismatch at candidate {index}")
        features = candidate.get("features")
        if not isinstance(features, Mapping):
            raise ValueError("candidate has no semantic feature mapping")
        candidate_rows.append({"kind": identity["kind"], "features": sanitize_executor_tree(features)})
        action_identities.append(identity)

    public_context, choice_by_candidate = _normalize_public_context(public_projection, action_identities)
    for index, candidate_row in enumerate(candidate_rows):
        if index not in choice_by_candidate:
            continue
        candidate_row["public_choice"] = {
            "kind": action_identities[index]["kind"],
            **choice_by_candidate[index],
        }

    result = {
        "schema_id": "issue85-index-free-new-paths-v1",
        "screen": sanitize_executor_tree(privileged_projection.get("screen_identity", {})),
        "shared_context": sanitize_executor_tree(privileged_projection.get("shared_context", {})),
        "public_context": public_context,
        "candidates": candidate_rows,
    }
    assert_index_free(result)
    return result


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _native_action_identity(action: Any, path: str) -> dict[str, Any]:
    if not isinstance(action, Mapping):
        raise ValueError(f"{path} native action descriptor is not a mapping")
    required = ("scope", "kind", "idx1", "idx2", "idx3")
    if any(key not in action for key in required):
        raise ValueError(f"{path} native action descriptor lacks its source-locked identity fields")
    if not isinstance(action["scope"], str) or not isinstance(action["kind"], str):
        raise ValueError(f"{path} scope/kind must be strings")
    for key in ("idx1", "idx2", "idx3"):
        if isinstance(action[key], bool) or not isinstance(action[key], int):
            raise ValueError(f"{path}.{key} must be an integer native binding")
    return {key: action[key] for key in required}


def _available_value(envelope: Any) -> tuple[bool, Any]:
    if not isinstance(envelope, Mapping):
        return False, None
    if envelope.get("availability") != "available" or "value" not in envelope:
        return False, None
    return True, envelope["value"]


def _map_graph_semantics(graph: Any) -> tuple[list[dict[str, Any]], dict[tuple[int, int], dict[str, Any]]]:
    if not isinstance(graph, Sequence) or isinstance(graph, (str, bytes)):
        return [], {}
    by_coordinate: dict[tuple[int, int], Mapping[str, Any]] = {}
    for node in graph:
        if not isinstance(node, Mapping):
            raise ValueError("visible map graph contains a non-mapping node")
        x, y, symbol = node.get("x"), node.get("y"), node.get("room_symbol")
        if isinstance(x, bool) or not isinstance(x, int) or isinstance(y, bool) or not isinstance(y, int):
            raise ValueError("visible map node lacks source-backed x/y association coordinates")
        if not isinstance(symbol, str):
            raise ValueError("visible map node lacks a public room symbol")
        coordinate = (x, y)
        if coordinate in by_coordinate:
            raise ValueError("visible map graph repeats a node association coordinate")
        by_coordinate[coordinate] = node

    semantic_by_coordinate: dict[tuple[int, int], dict[str, Any]] = {}
    for coordinate, node in by_coordinate.items():
        children: list[str] = []
        edges = node.get("outgoing_edges", ())
        if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes)):
            raise ValueError("visible map node outgoing_edges is not a sequence")
        for edge in edges:
            if not isinstance(edge, Mapping):
                raise ValueError("visible map edge is not a mapping")
            child_x, child_y = edge.get("x"), edge.get("y")
            if (
                isinstance(child_x, bool)
                or not isinstance(child_x, int)
                or isinstance(child_y, bool)
                or not isinstance(child_y, int)
            ):
                raise ValueError("visible map edge lacks source-backed x/y association coordinates")
            target = by_coordinate.get((child_x, child_y))
            if target is None:
                raise ValueError("visible map edge does not resolve to a visible node")
            children.append(str(target["room_symbol"]))
        semantic_by_coordinate[coordinate] = {
            "room_symbol": str(node["room_symbol"]),
            "outgoing_room_symbols": sorted(children),
        }
    nodes = sorted(semantic_by_coordinate.values(), key=_canonical_json)
    return nodes, semantic_by_coordinate


def _map_location(
    value: Any,
    semantic_by_coordinate: Mapping[tuple[int, int], Mapping[str, Any]],
    allowed_locations: frozenset[str],
) -> Any:
    if not isinstance(value, Mapping):
        raise ValueError("map location must be a source-defined mapping")
    location = value.get("location")
    if not isinstance(location, str) or location not in allowed_locations:
        raise ValueError("map location is not a native map-location enum")
    if location == "map_node":
        coordinate = (value.get("x"), value.get("y"))
        node = semantic_by_coordinate.get(coordinate)
        if node is None:
            raise ValueError("current/target map coordinate does not resolve to visible topology")
        return {"location": "map_node", **dict(node)}
    return {"location": location}


def _normalize_public_context(
    public: Mapping[str, Any], action_identities: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    graph_available, graph_value = _available_value(public.get("visible_map_graph"))
    map_nodes, semantic_by_coordinate = _map_graph_semantics(graph_value) if graph_available else ([], {})
    choice_by_candidate: dict[int, dict[str, Any]] = {}
    groups: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    group_candidates: dict[int, list[int]] = {}

    screen_available, screen_value = _available_value(public.get("screen_payload"))
    raw_choices = screen_value.get("choices", ()) if screen_available and isinstance(screen_value, Mapping) else ()
    coverage = screen_value.get("coverage_status") if isinstance(screen_value, Mapping) else None
    board_slots: dict[int, dict[str, Any]] = {}
    if screen_available and isinstance(screen_value, Mapping):
        cards = screen_value.get("cards", ())
        if isinstance(cards, Sequence) and not isinstance(cards, (str, bytes)):
            for slot in cards:
                if not isinstance(slot, Mapping):
                    raise ValueError("public Match and Keep card slot is not a mapping")
                slot_index = slot.get("index")
                if isinstance(slot_index, bool) or not isinstance(slot_index, int) or slot_index in board_slots:
                    raise ValueError("public Match and Keep board has a missing or duplicate slot association")
                semantic_slot = {"state": slot.get("state")}
                if "card" in slot:
                    semantic_slot["card"] = sanitize_executor_tree(slot["card"])
                board_slots[slot_index] = semantic_slot
    if raw_choices and (not isinstance(raw_choices, Sequence) or isinstance(raw_choices, (str, bytes))):
        raise ValueError("public screen choices are not an ordered sequence")
    if coverage == "supported" and len(raw_choices) != len(action_identities):
        raise ValueError("supported public screen payload does not cover every legal candidate")
    for choice in raw_choices or ():
        if not isinstance(choice, Mapping):
            raise ValueError("public screen choice is not a mapping")
        candidate_index = choice.get("candidate_index")
        if isinstance(candidate_index, bool) or not isinstance(candidate_index, int):
            raise ValueError("public screen choice lacks a valid transient candidate association")
        if not 0 <= candidate_index < len(action_identities) or candidate_index in choice_by_candidate:
            raise ValueError("public screen choice has an out-of-range or duplicate candidate association")
        bound_action = choice.get("candidate_action")
        if bound_action is not None and _native_action_identity(
            bound_action, f"screen_payload.choices[{candidate_index}]"
        ) != dict(action_identities[candidate_index]):
            raise ValueError("screen choice native action association disagrees with top-level legal action")

        semantic = dict(choice)
        semantic.pop("candidate_index", None)
        semantic.pop("candidate_action", None)
        card_index = semantic.pop("card_index", None)
        first_card_index = semantic.pop("first_card_index", None)
        if card_index is not None:
            if isinstance(card_index, bool) or not isinstance(card_index, int) or card_index not in board_slots:
                raise ValueError("public choice card_index does not resolve to a visible board slot")
            semantic["selected_card_state"] = board_slots[card_index]
        if first_card_index is not None:
            if (
                isinstance(first_card_index, bool)
                or not isinstance(first_card_index, int)
                or first_card_index not in board_slots
            ):
                raise ValueError("public choice first_card_index does not resolve to a visible board slot")
            semantic["first_card_state"] = board_slots[first_card_index]
        identity = action_identities[candidate_index]
        if semantic.get("choice_type") == "map_route":
            destination = semantic.get("destination")
            if isinstance(destination, Mapping) and destination.get("location") == "map_node":
                destination_x = destination.get("x")
                if isinstance(destination_x, bool) or not isinstance(destination_x, int) or identity["kind"] != "map" or identity["idx1"] != destination_x:
                    raise ValueError("map-route action index does not bind to its visible destination")
        if isinstance(semantic.get("destination"), Mapping):
            semantic["destination"] = _map_location(
                semantic["destination"], semantic_by_coordinate, _ROUTE_MAP_LOCATION_VALUES
            )
        choice_by_candidate[candidate_index] = sanitize_executor_tree(semantic)

        identity = action_identities[candidate_index]
        if identity["kind"] == "reward_card":
            group_index, option_index = identity["idx1"], identity["idx2"]
            if group_index < 0 or option_index < 0:
                raise ValueError("reward-card group/option binding must be nonnegative")
            groups.setdefault(group_index, []).append((option_index, choice_by_candidate[candidate_index]))
            group_candidates.setdefault(group_index, []).append(candidate_index)

    group_rows: list[dict[str, Any]] = []
    for group_index, members in groups.items():
        option_indices = [option for option, _ in members]
        if len(option_indices) != len(set(option_indices)):
            raise ValueError("duplicate public option pointer within a reward-card group")
        alternatives = sorted((choice for _, choice in members), key=_canonical_json)
        digest = hashlib.sha256(_canonical_json(alternatives).encode("utf-8")).hexdigest()
        group = {"group_key": f"card-group:{digest}", "alternatives": alternatives}
        group_rows.append(group)
        for candidate_index in group_candidates[group_index]:
            choice_by_candidate[candidate_index] = {
                **choice_by_candidate[candidate_index],
                "reward_group_key": group["group_key"],
                "reward_group_alternatives": alternatives,
            }

    def public_field(name: str) -> Any:
        envelope = public.get(name)
        available, value = _available_value(envelope)
        if not available:
            return {"availability": "unavailable", "visibility": "public_now"}
        if name in {"visible_map_graph"}:
            semantic_value = map_nodes
        elif name in {"current_map_node"}:
            semantic_value = _map_location(
                value, semantic_by_coordinate, _CURRENT_MAP_LOCATION_VALUES
            )
        elif name == "immediately_legal_routes":
            semantic_value = []
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                semantic_value = []
                for route in value:
                    clean = dict(route) if isinstance(route, Mapping) else route
                    if isinstance(clean, dict):
                        candidate_index = clean.get("candidate_index")
                        if isinstance(candidate_index, bool) or not isinstance(candidate_index, int) or not 0 <= candidate_index < len(action_identities):
                            raise ValueError("immediately legal route has no valid candidate association")
                        bound_action = clean.get("candidate_action")
                        if _native_action_identity(bound_action, f"immediately_legal_routes[{candidate_index}]") != dict(action_identities[candidate_index]):
                            raise ValueError("immediately legal route action identity does not bind to its candidate")
                        if clean.get("choice_type") == "map_route":
                            destination = clean.get("destination")
                            if isinstance(destination, Mapping) and destination.get("location") == "map_node":
                                destination_x = destination.get("x")
                                if isinstance(destination_x, bool) or not isinstance(destination_x, int) or action_identities[candidate_index]["kind"] != "map" or action_identities[candidate_index]["idx1"] != destination_x:
                                    raise ValueError("immediately legal map route does not bind to its visible destination")
                        if isinstance(clean.get("destination"), Mapping):
                            clean["destination"] = _map_location(
                                clean["destination"],
                                semantic_by_coordinate,
                                _ROUTE_MAP_LOCATION_VALUES,
                            )
                        clean.pop("candidate_index", None)
                        clean.pop("candidate_action", None)
                    semantic_value.append(sanitize_executor_tree(clean))
        elif name == "persistent_resources":
            semantic_value = {}
            if not isinstance(value, Mapping):
                raise ValueError("public persistent resource value is not a mapping")
            for field_name, raw_field in value.items():
                field_available, field_value = _public_resource_field(
                    raw_field, f"persistent_resources.value.{field_name}"
                )
                if field_available:
                    semantic_value[field_name] = {
                        "availability": "available",
                        "value": sanitize_executor_tree(field_value),
                    }
                else:
                    semantic_value[field_name] = {"availability": "unavailable"}
        else:
            semantic_value = sanitize_executor_tree(value)
        return {"availability": "available", "visibility": "public_now", "value": semantic_value}

    normalized = {
        "visible_act_boss": public_field("visible_act_boss"),
        "visible_map_graph": public_field("visible_map_graph"),
        "current_map_node": public_field("current_map_node"),
        "immediately_legal_routes": public_field("immediately_legal_routes"),
        "persistent_resources": public_field("persistent_resources"),
        "screen_payload_availability": "available" if screen_available else "unavailable",
        "screen_choices": sorted(choice_by_candidate.values(), key=_canonical_json),
        "reward_card_groups": sorted(group_rows, key=_canonical_json),
    }
    if screen_available and isinstance(screen_value, Mapping):
        extras: dict[str, Any] = {}
        consumed = {
            "choices",
            "coverage_status",
            "reason",
            "source",
            "missing_facts",
            "map_graph",
            "current_map_node",
            "legal_routes",
            "cards",
            "hidden_card_note",
        }
        for key, value in screen_value.items():
            if key not in consumed:
                extras[key] = sanitize_executor_tree(value)
        if board_slots:
            extras["visible_card_slots"] = sorted(board_slots.values(), key=_canonical_json)
        if extras:
            normalized["screen_payload_semantics"] = extras
    return sanitize_executor_tree(normalized), choice_by_candidate


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
