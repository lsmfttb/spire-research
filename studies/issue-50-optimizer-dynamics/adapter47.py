"""Study-only bridge from native public choices to serialized policy input.

This module does not simulate rules, choose actions, train a policy, or expose
native action bits to the callback.  The executor must resolve the returned
candidate index against a fresh native projection and legal-action list.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
from typing import Any


SCHEMA_ID = "noncombat-visible-choice-input-v1"
NATIVE_SCHEMA_ID = "native-public-projection-v3"
SUPPORTED_SCREENS = frozenset({"MAP_SCREEN", "REWARDS"})
_ACTION_FIELDS = frozenset({"scope", "kind", "idx1", "idx2", "idx3", "label"})
_PROJECTION_AVAILABILITY_FIELDS = (
    "screen_identity",
    "visible_act_boss",
    "visible_map_graph",
    "current_map_node",
    "immediately_legal_routes",
    "persistent_resources",
    "screen_payload",
    "candidate_actions",
)


class ChoiceSurfaceError(ValueError):
    """The native public surface cannot safely provide a policy choice."""


class UnsupportedChoiceSurface(ChoiceSurfaceError):
    """The current screen or a subcase is outside this adapter's coverage."""


class StaleChoiceMapping(ChoiceSurfaceError):
    """The public state changed after the callback input was prepared."""


@dataclass(frozen=True, repr=False)
class PreparedPolicyInput:
    """Serialized callback input and executor-only action mapping.

    The native actions are deliberately private fields.  Pass only
    ``serialized_input`` to policy code.
    """

    serialized_input: str
    _native_actions: tuple[Any, ...] = field(repr=False)
    _candidate_fingerprints: tuple[str, ...] = field(repr=False)


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ChoiceSurfaceError(f"public projection is not finite JSON: {exc}") from exc


def _required_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChoiceSurfaceError(f"{label} must be an object")
    return value


def _availability(projection: Mapping[str, Any], name: str) -> tuple[str, Any | None]:
    wrapper = _required_mapping(projection.get(name), name)
    state = wrapper.get("availability")
    if state not in {"available", "unavailable", "unsupported", "not_applicable"}:
        raise ChoiceSurfaceError(f"{name} has an unknown availability value")
    if state == "available":
        if "value" not in wrapper:
            raise ChoiceSurfaceError(f"{name} is available but has no value")
        return state, wrapper["value"]
    if "value" in wrapper:
        raise ChoiceSurfaceError(f"{name} has a value while marked {state}")
    return state, None


def _native_attr(action: Any, name: str) -> Any:
    if isinstance(action, Mapping):
        if name not in action:
            raise ChoiceSurfaceError(f"native action is missing {name}")
        return action[name]
    try:
        return getattr(action, name)
    except AttributeError as exc:
        raise ChoiceSurfaceError(f"native action is missing {name}") from exc


def _public_identity(action: Any) -> dict[str, Any]:
    scope = _native_attr(action, "scope")
    kind = _native_attr(action, "kind")
    indexes = tuple(_native_attr(action, name) for name in ("idx1", "idx2", "idx3"))
    label = _native_attr(action, "label")
    if not isinstance(scope, str) or not isinstance(kind, str) or not isinstance(label, str):
        raise ChoiceSurfaceError("native action identity fields must be strings")
    if any(type(index) is not int for index in indexes):
        raise ChoiceSurfaceError("native action indexes must be integers")
    if "bits=" in label:
        label = (
            f"{scope}.{kind} idx1={indexes[0]} idx2={indexes[1]} "
            f"idx3={indexes[2]}"
        )
    if "bits=" in label:
        raise ChoiceSurfaceError("opaque native action bits appear in the public label")
    return {
        "scope": scope,
        "kind": kind,
        "idx1": indexes[0],
        "idx2": indexes[1],
        "idx3": indexes[2],
        "label": label,
    }


def _validate_candidate(value: Any, index: int) -> dict[str, Any]:
    candidate = dict(_required_mapping(value, f"candidate_actions[{index}]"))
    if frozenset(candidate) != _ACTION_FIELDS:
        raise ChoiceSurfaceError(
            f"candidate_actions[{index}] must contain only the six public action fields"
        )
    if any(type(candidate[name]) is not int for name in ("idx1", "idx2", "idx3")):
        raise ChoiceSurfaceError(f"candidate_actions[{index}] indexes must be integers")
    if any(not isinstance(candidate[name], str) for name in ("scope", "kind", "label")):
        raise ChoiceSurfaceError(f"candidate_actions[{index}] text fields must be strings")
    if "bits=" in candidate["label"]:
        raise ChoiceSurfaceError("opaque native action bits appear in a public candidate")
    return candidate


def _choices_for_screen(
    screen: str,
    payload: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> Sequence[Any]:
    if screen == "REWARDS":
        choices = payload.get("choices")
    else:
        choices = payload.get("choices")
        route_state, route_value = _availability(projection, "immediately_legal_routes")
        if route_state != "available" or not isinstance(route_value, list):
            raise UnsupportedChoiceSurface("MAP_SCREEN legal routes are not available")
        payload_routes = payload.get("legal_routes")
        if not isinstance(payload_routes, list):
            raise ChoiceSurfaceError("MAP_SCREEN is missing its route-only candidate list")
        if _json(payload_routes) != _json(route_value):
            raise ChoiceSurfaceError("MAP_SCREEN route lists disagree across native fields")
        if not isinstance(choices, list):
            raise UnsupportedChoiceSurface("MAP_SCREEN does not provide ordered legal choices")
        choice_routes = [
            choice for choice in choices
            if isinstance(choice, Mapping) and choice.get("choice_type") == "map_route"
        ]
        if _json(choice_routes) != _json(payload_routes):
            raise ChoiceSurfaceError("MAP_SCREEN choices disagree with the route-only list")
    if not isinstance(choices, list):
        raise UnsupportedChoiceSurface(f"{screen} does not provide a choice list")
    return choices


def _prepare_parts(
    native_projection: Mapping[str, Any],
    native_actions: Sequence[Any],
) -> tuple[dict[str, Any], tuple[Any, ...], tuple[str, ...]]:
    projection = _required_mapping(native_projection, "native projection")
    if projection.get("schema_id") != NATIVE_SCHEMA_ID:
        raise ChoiceSurfaceError(
            f"expected native schema {NATIVE_SCHEMA_ID!r}; received {projection.get('schema_id')!r}"
        )
    screen_state, screen_value = _availability(projection, "screen_identity")
    if screen_state != "available" or not isinstance(screen_value, str):
        raise UnsupportedChoiceSurface("native screen identity is unavailable")
    screen = screen_value
    if screen not in SUPPORTED_SCREENS:
        raise UnsupportedChoiceSurface(f"{screen} is outside the study adapter coverage")

    candidate_state, candidate_value = _availability(projection, "candidate_actions")
    if candidate_state != "available" or not isinstance(candidate_value, list):
        raise UnsupportedChoiceSurface("ordered native candidate actions are unavailable")
    if not isinstance(native_actions, Sequence) or isinstance(native_actions, (str, bytes)):
        raise ChoiceSurfaceError("trusted native actions must be an ordered sequence")
    if len(candidate_value) != len(native_actions) or not native_actions:
        raise ChoiceSurfaceError("candidate list and trusted native action list do not align")

    candidates = tuple(_validate_candidate(value, i) for i, value in enumerate(candidate_value))
    candidate_fingerprints = tuple(_json(item) for item in candidates)
    if len(set(candidate_fingerprints)) != len(candidate_fingerprints):
        raise ChoiceSurfaceError("public candidate identity is ambiguous")
    for index, (candidate, action) in enumerate(zip(candidates, native_actions)):
        if candidate != _public_identity(action):
            raise ChoiceSurfaceError(f"candidate {index} does not identify the same native action")

    payload_state, payload_value = _availability(projection, "screen_payload")
    if payload_state != "available":
        raise UnsupportedChoiceSurface("native screen payload is unavailable")
    payload = _required_mapping(payload_value, "screen payload")
    coverage = payload.get("coverage_status")
    if coverage != "supported":
        reason = payload.get("reason", f"screen payload coverage is {coverage!r}")
        raise UnsupportedChoiceSurface(str(reason))

    if screen == "MAP_SCREEN":
        for required in ("visible_act_boss", "visible_map_graph", "current_map_node"):
            state, _ = _availability(projection, required)
            if state != "available":
                raise UnsupportedChoiceSurface(f"MAP_SCREEN requires available {required}")
    choices = _choices_for_screen(screen, payload, projection)
    options: list[dict[str, Any]] = []
    seen: set[int] = set()
    association_order: list[int] = []
    for choice_index, value in enumerate(choices):
        choice = dict(_required_mapping(value, f"choices[{choice_index}]"))
        candidate_index = choice.get("candidate_index")
        if type(candidate_index) is not int or not 0 <= candidate_index < len(candidates):
            raise ChoiceSurfaceError(f"choice {choice_index} has an invalid candidate index")
        if candidate_index in seen:
            raise ChoiceSurfaceError("multiple visible choices map to one candidate")
        seen.add(candidate_index)
        association_order.append(candidate_index)
        if choice.get("candidate_action") != candidates[candidate_index]:
            raise ChoiceSurfaceError(
                f"choice {choice_index} candidate association is malformed or stale"
            )
        if screen == "MAP_SCREEN":
            choice_type = choice.get("choice_type")
            if choice_type == "map_route":
                candidate = candidates[candidate_index]
                destination = _required_mapping(
                    choice.get("destination"), f"choices[{choice_index}].destination"
                )
                if candidate["scope"] != "game" or candidate["kind"] != "map":
                    raise ChoiceSurfaceError(
                        f"choice {choice_index} route does not identify a native map action"
                    )
                location = destination.get("location")
                if location == "map_node":
                    if (
                        type(destination.get("x")) is not int
                        or type(destination.get("y")) is not int
                        or not isinstance(destination.get("room_symbol"), str)
                    ):
                        raise ChoiceSurfaceError(
                            f"choice {choice_index} has an incomplete map destination"
                        )
                elif location != "boss_entry":
                    raise ChoiceSurfaceError(
                        f"choice {choice_index} has an unknown map destination"
                    )
            elif choice_type in {"potion_use", "potion_discard"}:
                candidate = candidates[candidate_index]
                expected_kind = {
                    "potion_use": "game_potion_use",
                    "potion_discard": "game_potion_discard",
                }[choice_type]
                potion = _required_mapping(
                    choice.get("potion"), f"choices[{choice_index}].potion"
                )
                if (
                    candidate["scope"] != "game"
                    or candidate["kind"] != expected_kind
                    or type(potion.get("potion_index")) is not int
                    or potion["potion_index"] != candidate["idx1"]
                    or type(potion.get("id")) is not int
                    or not isinstance(potion.get("id_label"), str)
                    or not isinstance(potion.get("name"), str)
                ):
                    raise ChoiceSurfaceError(
                        f"choice {choice_index} does not describe its native potion action"
                    )
            else:
                raise UnsupportedChoiceSurface(
                    f"MAP_SCREEN choice {choice_index} has unsupported semantics"
                )
        semantics = {
            key: value for key, value in choice.items()
            if key not in {"candidate_index", "candidate_action"}
        }
        options.append({
            "candidate_index": candidate_index,
            "action": candidates[candidate_index],
            "semantics": semantics,
        })
    if association_order != list(range(len(candidates))):
        raise ChoiceSurfaceError("choice list is incomplete or not in native candidate order")

    availability = {
        name: _availability(projection, name)[0]
        for name in _PROJECTION_AVAILABILITY_FIELDS
    }
    resources_state, resources_value = _availability(projection, "persistent_resources")
    public_state: dict[str, Any] = {
        "screen": screen,
        "persistent_resources": resources_value if resources_state == "available" else None,
    }
    for name, output_name in (
        ("visible_act_boss", "visible_act_boss"),
        ("visible_map_graph", "map_graph"),
        ("current_map_node", "current_map_node"),
    ):
        state, value = _availability(projection, name)
        public_state[output_name] = value if state == "available" else None
    context = {
        key: value for key, value in payload.items()
        if key not in {"choices", "legal_routes"}
    }
    public_state["screen_context"] = context
    policy_input = {
        "schema_id": SCHEMA_ID,
        "availability": availability,
        "public_state": public_state,
        "legal_options": options,
    }
    serialized = _json(policy_input)
    actions = tuple(native_actions)
    return policy_input, actions, candidate_fingerprints


def prepare_policy_input(
    native_projection: Mapping[str, Any],
    native_actions: Sequence[Any],
) -> PreparedPolicyInput:
    """Build callback JSON and keep the corresponding native actions private."""
    policy_input, actions, fingerprints = _prepare_parts(native_projection, native_actions)
    return PreparedPolicyInput(_json(policy_input), actions, fingerprints)


def call_policy(
    prepared: PreparedPolicyInput,
    callback: Callable[[str], Any],
) -> Any:
    """Invoke policy code with serialized public values only."""
    if not isinstance(prepared, PreparedPolicyInput):
        raise TypeError("prepared must be a PreparedPolicyInput")
    return callback(prepared.serialized_input)


def resolve_native_action(
    prepared: PreparedPolicyInput,
    candidate_index: Any,
    current_projection: Mapping[str, Any],
    current_native_actions: Sequence[Any],
) -> Any:
    """Resolve a model index against a fresh native state and action list.

    Rebuilding the mapping rejects stale public input and returns the current
    action object, so a delayed callback cannot execute a retained stale action.
    """
    if type(candidate_index) is not int:
        raise ChoiceSurfaceError("policy output must be an integer candidate index")
    if not 0 <= candidate_index < len(prepared._native_actions):
        raise ChoiceSurfaceError("policy output index is outside the legal option list")
    _, current_actions, current_fingerprints = _prepare_parts(
        current_projection, current_native_actions
    )
    if current_fingerprints != prepared._candidate_fingerprints:
        raise StaleChoiceMapping("native candidate mapping changed after policy input creation")
    current_input = _json(_prepare_parts(current_projection, current_native_actions)[0])
    if current_input != prepared.serialized_input:
        raise StaleChoiceMapping("public state changed after policy input creation")
    return current_actions[candidate_index]
