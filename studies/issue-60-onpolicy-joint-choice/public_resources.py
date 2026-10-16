"""Strict allowlist for public persistent-resource boundary evidence."""
from __future__ import annotations

import json
from typing import Any, Mapping


RESOURCE_FIELDS = (
    "current_hp", "max_hp", "gold", "potion_count", "potion_capacity",
    "deck", "relics", "potion_identities", "keys",
)
_CARD_FIELDS = (
    "id_label", "name", "type", "rarity", "upgraded", "upgrade_count", "misc",
)
_RELIC_FIELDS = ("id_label", "name", "counter")
_POTION_FIELDS = ("potion_index", "id_label", "name")
_NUMERIC_FIELDS = frozenset(("current_hp", "max_hp", "gold", "potion_count", "potion_capacity"))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _availability(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "unavailable"
    state = value.get("availability")
    if state not in {"available", "unavailable", "unsupported", "not_applicable"}:
        return "unavailable"
    return state


def _entity_list(value: Any, fields: tuple[str, ...], *, sort: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError("available public inventory collection must be a list of objects")
    result = [{name: item[name] for name in fields if name in item} for item in value]
    if sort:
        result.sort(key=_canonical)
    return result


def boundary_resources(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only declared public inventory, preserving duplicates but no order leak."""
    outer = projection.get("persistent_resources")
    if _availability(outer) != "available":
        return {name: {"availability": _availability(outer)} for name in RESOURCE_FIELDS}
    raw = outer.get("value")
    if not isinstance(raw, Mapping):
        raise ValueError("available public persistent resources have no field mapping")

    result: dict[str, Any] = {}
    for name in RESOURCE_FIELDS:
        wrapped = raw.get(name)
        state = _availability(wrapped)
        if state != "available":
            result[name] = {"availability": state}
            continue
        value = wrapped.get("value")
        if name in _NUMERIC_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"available public resource {name} must be numeric")
            result[name] = {"availability": "available", "value": value}
        elif name == "deck":
            # deck_index and list order may expose hidden draw order; multiplicity
            # and card identity/known attributes are sufficient for inventory.
            result[name] = {
                "availability": "available",
                "value": _entity_list(value, _CARD_FIELDS, sort=True),
            }
        elif name == "relics":
            result[name] = {
                "availability": "available",
                "value": _entity_list(value, _RELIC_FIELDS, sort=True),
            }
        elif name == "potion_identities":
            # Potion slots are public and intentionally retained.
            result[name] = {
                "availability": "available",
                "value": _entity_list(value, _POTION_FIELDS, sort=False),
            }
        elif name == "keys":
            if not isinstance(value, Mapping):
                raise ValueError("available public keys must be an object")
            keys = {}
            for key in ("blue_key", "green_key", "red_key"):
                if key in value and not isinstance(value[key], bool):
                    raise ValueError(f"available public key {key} must be boolean")
                if key in value:
                    keys[key] = value[key]
            result[name] = {"availability": "available", "value": keys}
    return result
