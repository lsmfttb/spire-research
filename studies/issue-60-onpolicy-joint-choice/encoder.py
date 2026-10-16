"""Typed normal-public features for the disposable Issue 60 policy.

Only normalized Issue 47 adapter JSON reaches this module.  It never accepts a
native object, simulator snapshot, seed, or action bit field.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from normalizer_v49 import canonical, normalize


NUMERIC_FIELDS = ("cost", "damage", "block", "upgrade_count", "misc",
                  "counter", "max_hp_gain", "gold")
RESOURCE_FIELDS = ("current_hp", "max_hp", "gold", "potion_count", "potion_capacity")
PUBLIC_AVAILABILITY_FIELDS = (
    "screen_identity", "visible_act_boss", "visible_map_graph", "current_map_node",
    "immediately_legal_routes", "persistent_resources", "screen_payload", "candidate_actions",
)
_MISSING = object()


def numeric(value: Any) -> tuple[float, float]:
    """Fixed signed log normalization plus a separate availability bit."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0, 0.0
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("public numeric feature is not finite")
    return math.copysign(math.log1p(abs(value)) / 10.0, value), 1.0


class Vocabulary:
    def __init__(self, path: str | Path):
        raw = Path(path).read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw)
        if data.get("schema_id") != "issue60-fixed-category-vocab-v1":
            raise ValueError("unexpected category vocabulary schema")
        self.entries = tuple(data["entries"])
        if not self.entries or self.entries[0] != "<UNK>" or len(set(self.entries)) != len(self.entries):
            raise ValueError("vocabulary must have unique entries and reserve 0 for UNK")
        self.indices = {value: index for index, value in enumerate(self.entries)}

    def id(self, namespace: str, value: Any) -> int:
        if value is None:
            return 0
        return self.indices.get(f"{namespace}:{value}", 0)


@dataclass(frozen=True)
class Entity:
    categories: tuple[int, ...]
    numeric: tuple[float, ...]
    masks: tuple[float, ...]


@dataclass(frozen=True)
class MapNode:
    entity: Entity
    x: float
    y: float
    coordinate: tuple[int, int]
    outgoing: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Candidate:
    entity: Entity
    children: tuple[Entity, ...]
    group_key: str | None
    destination: tuple[int, int] | None
    boss_entry: bool
    semantic_key: str


@dataclass(frozen=True)
class Observation:
    schema_id: str
    public_facts: Mapping[str, Any]
    state_categories: tuple[int, ...]
    state_numeric: tuple[float, ...]
    state_masks: tuple[float, ...]
    deck: tuple[Entity, ...]
    relics: tuple[Entity, ...]
    potions: tuple[Entity, ...]
    reward_groups: tuple[tuple[Entity, ...], ...]
    group_keys: tuple[str, ...]
    map_nodes: tuple[MapNode, ...]
    current_coordinate: tuple[int, int] | None
    candidates: tuple[Candidate, ...]


def _field(value: Any) -> tuple[Any, float]:
    if isinstance(value, Mapping) and "availability" in value:
        if value.get("availability") == "available" and "value" in value:
            return value["value"], 1.0
        return _MISSING, 0.0
    if value is _MISSING or value is None:
        return _MISSING, 0.0
    return value, 1.0


def _entity(vocab: Vocabulary, kind: str, raw: Mapping[str, Any], *,
            extra_categories: tuple[tuple[str, Any], ...] = ()) -> Entity:
    categories: list[int] = []
    if kind in {"card", "relic", "potion"}:
        categories.append(vocab.id(kind, raw.get("id_label")))
    elif kind == "map_node":
        categories.append(vocab.id("room_symbol", raw.get("room_symbol")))
    elif kind == "max_hp_conflict":
        categories.append(vocab.id("special", "singing_bowl_max_hp"))
    else:
        categories.append(vocab.id("special", kind))
    for namespace, value in extra_categories:
        categories.append(vocab.id(namespace, value))
    for namespace, field in (("card_type", "type"), ("card_rarity", "rarity")):
        if field in raw:
            categories.append(vocab.id(namespace, raw[field]))
    if "upgraded" in raw:
        categories.append(vocab.id("boolean", str(bool(raw["upgraded"])).lower()))
    unavailable = set(raw.get("unavailable_attributes", ()))
    values: list[float] = []
    masks: list[float] = []
    for field in NUMERIC_FIELDS:
        value, present = _field(raw.get(field, _MISSING))
        if field in unavailable:
            value, present = _MISSING, 0.0
        encoded, mask = numeric(value) if present else (0.0, 0.0)
        values.append(encoded)
        masks.append(mask)
    return Entity(tuple(categories), tuple(values), tuple(masks))


def _collection(resources: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    value, available = _field(resources.get(name))
    if not available:
        return []
    if not isinstance(value, list) or any(not isinstance(x, Mapping) for x in value):
        raise ValueError(f"available public {name} must be a list of objects")
    return value


def _available_number(resources: Mapping[str, Any], name: str) -> tuple[float, float]:
    value, present = _field(resources.get(name))
    return numeric(value) if present else (0.0, 0.0)


def _safe_choice_entity(vocab: Vocabulary, raw: Mapping[str, Any], field: str) -> Entity:
    item = raw.get(field)
    if not isinstance(item, Mapping):
        raise ValueError(f"public choice {field} must be an object")
    kind = "relic" if field in {"relic", "relic_removed_with_key"} else field
    slot: tuple[tuple[str, Any], ...] = ()
    if kind == "potion" and "potion_index" in item:
        slot = (("potion_slot", item["potion_index"]),)
    return _entity(vocab, kind, item, extra_categories=slot)


def encode(serialized_public: str | Mapping[str, Any], vocab: Vocabulary) -> Observation:
    """Normalize v49 public choices, then create typed entity/group/map inputs."""
    norm = normalize(serialized_public)
    facts = norm["facts"]
    state = facts["public_state"]
    availability = facts["availability"]
    screen = state.get("screen")
    if screen not in ("MAP_SCREEN", "REWARDS"):
        raise ValueError("residual encoder only accepts its declared supported screens")

    cats = [vocab.id("screen", screen)]
    for field_name in PUBLIC_AVAILABILITY_FIELDS:
        status = availability.get(field_name, "unavailable")
        # Preserve which public projection branch is available. A shared
        # availability token summed across slots would encode only the count
        # of available branches, conflating (for example) a missing map with
        # missing persistent resources.
        cats.append(vocab.id("availability_field", f"{field_name}:{status}"))
    boss, boss_available = _field(state.get("visible_act_boss"))
    if boss_available:
        cats.append(vocab.id("boss", boss))

    resources = state.get("persistent_resources")
    if not isinstance(resources, Mapping):
        resources = {}
    resource_values: list[float] = []
    resource_masks: list[float] = []
    for name in RESOURCE_FIELDS:
        value, mask = _available_number(resources, name)
        resource_values.append(value)
        resource_masks.append(mask)
    hp, hp_mask = _available_number(resources, "current_hp")
    max_hp, max_mask = _available_number(resources, "max_hp")
    ratio = (hp / max_hp) if hp_mask and max_mask and max_hp > 0 else 0.0
    ratio_value, ratio_mask = numeric(ratio) if hp_mask and max_mask and max_hp > 0 else (0.0, 0.0)

    deck_raw = _collection(resources, "deck")
    relic_raw = _collection(resources, "relics")
    potion_raw = _collection(resources, "potion_identities")
    deck = tuple(_entity(vocab, "card", x) for x in deck_raw)
    relics = tuple(_entity(vocab, "relic", x) for x in relic_raw)
    potions = tuple(
        _entity(vocab, "potion", x,
                extra_categories=(("potion_slot", x.get("potion_index")),))
        for x in potion_raw
    )
    state_numeric = list(resource_values) + [ratio_value]
    state_masks = list(resource_masks) + [ratio_mask]

    location = state.get("ordinary_location", {})
    if not isinstance(location, Mapping):
        location = {}
    for name, scale in (("act", 4.0), ("floor", 60.0)):
        value, present = _field(location.get(name))
        if present and isinstance(value, (int, float)) and not isinstance(value, bool):
            encoded, mask = numeric(float(value) / scale)
        else:
            encoded, mask = 0.0, 0.0
        state_numeric.append(encoded)
        state_masks.append(mask)
    keys, keys_available = _field(resources.get("keys"))
    for name in ("blue_key", "green_key", "red_key"):
        value = keys.get(name) if keys_available and isinstance(keys, Mapping) else _MISSING
        if isinstance(value, bool):
            state_numeric.append(float(value))
            state_masks.append(1.0)
        else:
            state_numeric.append(0.0)
            state_masks.append(0.0)
    for collection in (deck_raw, relic_raw, potion_raw):
        encoded, mask = numeric(len(collection))
        state_numeric.append(encoded)
        state_masks.append(mask)
    groups_raw = facts.get("card_reward_groups", [])
    if not isinstance(groups_raw, list) or any(not isinstance(g, list) for g in groups_raw):
        raise ValueError("normalized reward groups must be lists")
    group_keys = sorted({
        candidate.get("group_key") for candidate in norm["candidates"]
        if isinstance(candidate, Mapping) and isinstance(candidate.get("group_key"), str)
    })
    # v49 emits groups in canonical content order; candidate references use a
    # content digest, so the mapping remains stable under native group-index reuse.
    reward_groups: list[tuple[Entity, ...]] = []
    for group in groups_raw:
        entities: list[Entity] = []
        for item in group:
            if not isinstance(item, Mapping):
                raise ValueError("reward group member must be a public object")
            if item.get("kind") == "max_hp_conflict":
                entities.append(_entity(vocab, "max_hp_conflict", item))
            else:
                entities.append(_entity(vocab, "card", item))
        reward_groups.append(tuple(entities))
    # Derive content keys in exactly the same way as v49 rather than rely on
    # native reward group ordinals.
    from normalizer_v49 import key as content_key
    group_key_by_content = {
        content_key("card-group", group): tuple(entities)
        for group, entities in zip(groups_raw, reward_groups, strict=True)
    }

    graph = state.get("map_graph")
    map_nodes: list[MapNode] = []
    if isinstance(graph, list):
        for raw_node in graph:
            if not isinstance(raw_node, Mapping):
                raise ValueError("public map node must be an object")
            x, y = raw_node.get("x"), raw_node.get("y")
            if any(isinstance(v, bool) or not isinstance(v, int) for v in (x, y)):
                raise ValueError("public map node coordinates must be integers")
            edges = raw_node.get("outgoing_edges", [])
            if not isinstance(edges, list):
                raise ValueError("public map edges must be a list")
            outgoing = []
            for edge in edges:
                if not isinstance(edge, Mapping):
                    raise ValueError("public map edge must be an object")
                ex, ey = edge.get("x"), edge.get("y")
                if any(isinstance(v, bool) or not isinstance(v, int) for v in (ex, ey)):
                    raise ValueError("public map edge coordinates must be integers")
                outgoing.append((ex, ey))
            outgoing.sort()
            map_nodes.append(MapNode(
                entity=_entity(vocab, "map_node", raw_node),
                x=float(x) / 6.0, y=float(y) / 15.0,
                coordinate=(x, y), outgoing=tuple(outgoing),
            ))
    map_nodes.sort(key=lambda node: node.coordinate)

    current = state.get("current_map_node")
    current_coordinate = None
    if isinstance(current, Mapping) and current.get("location") == "map_node":
        x, y = current.get("x"), current.get("y")
        if isinstance(x, int) and not isinstance(x, bool) and isinstance(y, int) and not isinstance(y, bool):
            current_coordinate = (x, y)
    cats.append(vocab.id("map_location", current.get("location") if isinstance(current, Mapping) else None))

    candidates: list[Candidate] = []
    options = norm.get("candidates")
    if not isinstance(options, list) or not options:
        raise ValueError("normalized policy input has no legal candidates")
    for candidate in options:
        action_kind = candidate.get("kind")
        semantics = candidate.get("semantics")
        if not isinstance(action_kind, str) or not isinstance(semantics, Mapping):
            raise ValueError("normalized candidate is malformed")
        choice_type = semantics.get("choice_type")
        extra_categories: list[tuple[str, Any]] = [("action_kind", action_kind)]
        if choice_type is not None:
            extra_categories.append(("choice_type", choice_type))
        if "key" in semantics:
            extra_categories.append(("key", semantics["key"]))
        children: list[Entity] = []
        for field in ("card", "relic", "potion", "relic_removed_with_key"):
            if field in semantics:
                children.append(_safe_choice_entity(vocab, semantics, field))
        if choice_type == "singing_bowl_max_hp":
            special={"choice_type":"singing_bowl_max_hp",
                     "max_hp_gain":semantics.get("max_hp_gain",2),
                     "unavailable_attributes":["card","misc","upgrade_count"]}
            children.append(_entity(vocab,"max_hp_conflict",special))
        numbers: list[float] = []
        masks: list[float] = []
        for field in ("gold", "max_hp_gain"):
            value, present = _field(semantics.get(field, _MISSING))
            encoded, mask = numeric(value) if present else (0.0, 0.0)
            numbers.append(encoded); masks.append(mask)
        lost = semantics.get("sapphire_key_is_lost")
        numbers.append(float(bool(lost)) if isinstance(lost, bool) else 0.0)
        masks.append(1.0 if isinstance(lost, bool) else 0.0)
        group_key = candidate.get("group_key")
        group = group_key_by_content.get(group_key, ()) if isinstance(group_key, str) else ()
        numbers.append(numeric(len(group))[0]); masks.append(1.0 if group else 0.0)
        destination = semantics.get("destination")
        target = None
        is_boss = False
        if isinstance(destination, Mapping):
            if destination.get("location") == "map_node":
                x, y = destination.get("x"), destination.get("y")
                if isinstance(x, int) and not isinstance(x, bool) and isinstance(y, int) and not isinstance(y, bool):
                    target = (x, y)
                    extra_categories.append(("room_symbol", destination.get("room_symbol")))
                    numbers.extend([float(x) / 6.0, float(y) / 15.0, 0.0])
                    masks.extend([1.0, 1.0, 1.0])
            elif destination.get("location") == "boss_entry":
                is_boss = True
                numbers.extend([0.0, 0.0, 1.0]); masks.extend([0.0, 0.0, 1.0])
        if "potion" in semantics and isinstance(semantics["potion"], Mapping):
            slot = semantics["potion"].get("potion_index")
            if isinstance(slot, int) and not isinstance(slot, bool):
                extra_categories.append(("potion_slot", slot))
        # Pad optional candidate numeric features to the frozen length 8.
        numbers = numbers[:8] + [0.0] * max(0, 8 - len(numbers))
        masks = masks[:8] + [0.0] * max(0, 8 - len(masks))
        candidate_entity = _entity(vocab, "choice", {}, extra_categories=tuple(extra_categories))
        candidate_entity = Entity(candidate_entity.categories, tuple(numbers), tuple(masks))
        candidates.append(Candidate(
            entity=candidate_entity, children=tuple(children),
            group_key=group_key if isinstance(group_key, str) else None,
            destination=target, boss_entry=is_boss,
            semantic_key=canonical({"kind": action_kind, "semantics": semantics,
                                    "group_key": group_key,
                                    "group_alternatives": candidate.get("group_alternatives")}),
        ))

    return Observation(
        schema_id="issue60-typed-joint-choice-v1",
        public_facts=facts,
        state_categories=tuple(cats),
        state_numeric=tuple(state_numeric), state_masks=tuple(state_masks),
        deck=deck, relics=relics, potions=potions,
        reward_groups=tuple(reward_groups), group_keys=tuple(group_key_by_content),
        map_nodes=tuple(map_nodes), current_coordinate=current_coordinate,
        candidates=tuple(candidates),
    )
