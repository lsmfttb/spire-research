"""Identity-bound audit of native semantic legal-choice coverage for Issue 66."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


LEGAL_POTION_INVENTORY_ACTIONS = "legal_potion_and_inventory_actions"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _region(source: str, start: str, end: str) -> str:
    begin = source.find(start)
    if begin < 0:
        raise ValueError(f"native source is missing expected API marker: {start}")
    finish = source.find(end, begin + len(start))
    if finish < 0:
        raise ValueError(f"native source is missing expected API boundary: {end}")
    return source[begin:finish]


def _optional_region(source: str, start: str, end: str) -> str:
    begin = source.find(start)
    if begin < 0:
        return ""
    finish = source.find(end, begin + len(start))
    return "" if finish < 0 else source[begin:finish]


def _line_number(source: str, marker: str) -> int:
    position = source.find(marker)
    if position < 0:
        raise ValueError(f"native source is missing expected API marker: {marker}")
    return source.count("\n", 0, position) + 1


def _literal_reason(body: str) -> str | None:
    match = re.search(r'payload\["reason"\]\s*=\s*"([^"]+)"', body)
    return match.group(1) if match else None


def _assess_registered_requirements(
    required_screens: tuple[str, ...] | list[str],
    required_action_capabilities: tuple[str, ...] | list[str],
    screen_support: dict[str, dict[str, Any]],
    action_capability_support: dict[str, dict[str, Any]],
    snapshot_has_screen_choices: bool,
) -> tuple[list[str], list[str], str | None]:
    missing_screens = [
        screen for screen in required_screens
        if not screen_support.get(screen, {}).get("semantic_choices", False)
    ]
    missing_capabilities = [
        capability for capability in required_action_capabilities
        if not action_capability_support.get(capability, {}).get("semantic_actions", False)
    ]
    blockers = []
    if snapshot_has_screen_choices:
        blockers.append("snapshot_choice_surface_requires_manual_source_review")
    if missing_screens:
        blockers.append(
            "pinned_native_source_does_not_expose_semantic_choice_content_for: "
            + ", ".join(missing_screens)
        )
    if missing_capabilities:
        blockers.append(
            "pinned_native_source_does_not_expose_required_action_semantics_for: "
            + ", ".join(missing_capabilities)
        )
    return missing_screens, missing_capabilities, "; ".join(blockers) or None


def inspect_source_text(
    source: str,
    required_screens: tuple[str, ...] | list[str],
    required_action_capabilities: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """Return semantic support, never inferring it from opaque action indices."""
    snapshot = _region(
        source,
        "pybind11::dict snapshot()",
        "pybind11::dict battleSearchNodeSnapshot(",
    )
    action = _region(
        source,
        "pybind11::dict publicChoiceActionSnapshot(",
        "pybind11::dict publicChoiceAssociation(",
    )
    association = _region(
        source,
        "pybind11::dict publicChoiceAssociation(",
        "bool isCurrentActBossSelection(",
    )
    map_choices = _region(
        source,
        "pybind11::list publicMapScreenChoices(",
        "pybind11::dict publicCardRewardSnapshot(",
    )
    map_route_choice = _region(
        source,
        "pybind11::dict publicMapRouteChoice(",
        "pybind11::list publicMapScreenChoices(",
    )
    reward_choices = _region(
        source,
        "pybind11::dict publicRewardScreenPayload(",
        "pybind11::dict publicChoiceScreenPayload(",
    )
    choices = _region(
        source,
        "pybind11::dict publicChoiceScreenPayload(",
        "std::size_t knownDrawTopCount(",
    )
    game_action_kind = _optional_region(
        source,
        "std::string gameActionKind(",
        "LightSpeedAction makeGameAction(",
    )
    game_potion_snapshot = _optional_region(
        source,
        "pybind11::dict gamePotionSnapshot(",
        "pybind11::dict cardIdentitySnapshot(",
    )

    snapshot_has_screen_choices = bool(
        re.search(r'ret\["(?:choices|screen_payload|card_rewards|event_options)"\]', snapshot)
    )
    candidate_semantics = {
        "kind": 'ret["kind"]' in action,
        "scope": 'ret["scope"]' in action,
        "opaque_action_bits_exposed": 'ret["bits"]' in action,
        "native_indices_exposed": all(f'ret["{name}"]' in action for name in ("idx1", "idx2", "idx3")),
        "screen_entity_associations": (
            'ret["candidate_index"]' in association
            and 'ret["candidate_action"]' in association
            and "publicMapRouteChoice" in map_choices
            and "publicChoiceAssociation" in map_route_choice
            and "publicChoiceAssociation" in reward_choices
            and "choice_type" in reward_choices
        ),
    }

    screen_support: dict[str, dict[str, Any]] = {}
    for screen in required_screens:
        marker = f"case ScreenState::{screen}:"
        pos = choices.find(marker)
        if pos < 0:
            screen_support[screen] = {"semantic_choices": False, "reason": "no native screen case"}
            continue
        next_case = choices.find("case ScreenState::", pos + len(marker))
        next_default = choices.find("default:", pos + len(marker))
        boundaries = [value for value in (next_case, next_default) if value >= 0]
        finish = min(boundaries) if boundaries else len(choices)
        body = choices[pos:finish]
        supported = (
            screen == "MAP_SCREEN" and "choices" in body and "mapChoices" in body
            and "publicMapRouteChoice" in map_choices
            and "destination" in map_route_choice
            and "publicChoiceAssociation" in map_route_choice
        )
        supported = supported or (
            screen == "REWARDS" and "publicRewardScreenPayload" in body
            and "publicChoiceAssociation" in reward_choices
            and "choice_type" in reward_choices
        )
        reason = _literal_reason(body)
        if screen == "MAP_SCREEN" and supported:
            reason = "native map route choices include destination semantics and candidate binding"
        elif screen == "REWARDS" and supported:
            reason = "native reward choices include card/relic/potion/key/gold content and candidate binding"
        screen_support[screen] = {
            "semantic_choices": supported,
            "reason": reason or ("native case exposes no semantic choice payload" if not supported else None),
        }

    potion_action_checks = {
        "candidate_association_binds_semantic_action": candidate_semantics["screen_entity_associations"],
        "action_kind_maps_use_and_discard": (
            "action.isPotionAction()" in game_action_kind
            and "action.isPotionDiscard()" in game_action_kind
            and '"game_potion_use"' in game_action_kind
            and '"game_potion_discard"' in game_action_kind
        ),
        "candidate_action_exposes_semantic_kind": 'ret["kind"]' in action,
        "potion_identity_has_label_and_name": (
            'ret["id_label"]' in game_potion_snapshot
            and 'ret["name"]' in game_potion_snapshot
        ),
        "map_potion_candidates_bind_visible_potion": all(
            marker in map_choices for marker in (
                '"game_potion_use"', '"game_potion_discard"',
                "publicChoiceAssociation", "gamePotionSnapshot(gc, action.idx1)",
            )
        ),
        "reward_potion_candidates_bind_visible_potion": all(
            marker in reward_choices for marker in (
                '"game_potion_use"', '"game_potion_discard"', '"reward_potion"',
                "publicChoiceAssociation", "gamePotionSnapshot(gc, action.idx1)",
                "potionIdLabel", "potionLabel",
            )
        ),
    }
    action_capability_support: dict[str, dict[str, Any]] = {}
    for capability in required_action_capabilities:
        if capability == LEGAL_POTION_INVENTORY_ACTIONS:
            supported = all(potion_action_checks.values())
            reason = (
                "legal potion use/discard and reward potion candidates bind to native potion identity"
                if supported else
                "native source does not semantically bind every required potion/inventory candidate path"
            )
        else:
            supported = False
            reason = "unknown required noncombat action capability"
        action_capability_support[capability] = {
            "semantic_actions": supported,
            "reason": reason,
            "checks": potion_action_checks if capability == LEGAL_POTION_INVENTORY_ACTIONS else {},
            "covered_choice_surfaces": ["MAP_SCREEN", "REWARDS"] if supported else [],
        }
    missing, missing_capabilities, blocker = _assess_registered_requirements(
        required_screens,
        required_action_capabilities,
        screen_support,
        action_capability_support,
        snapshot_has_screen_choices,
    )
    return {
        "native_source_locations": {
            "snapshot": _line_number(source, "pybind11::dict snapshot()"),
            "public_choice_action_snapshot": _line_number(source, "pybind11::dict publicChoiceActionSnapshot("),
            "public_choice_screen_payload": _line_number(source, "pybind11::dict publicChoiceScreenPayload("),
        },
        "snapshot_has_screen_choice_content": snapshot_has_screen_choices,
        "candidate_action_surface": candidate_semantics,
        "screens": screen_support,
        "missing_noncombat_semantics": missing,
        "required_action_capabilities": list(required_action_capabilities),
        "action_capabilities": action_capability_support,
        "missing_action_capabilities": missing_capabilities,
        "status": "BLOCKED" if blocker else "SUPPORTED",
        "blocker": blocker,
        "policy_gate": "FAIL_CLOSED" if blocker else "SOURCE_AUDIT_ONLY_NOT_EXECUTION_AUTHORIZATION",
        "prohibited_substitutions": [
            "native action id/index as semantic entity feature",
            "Expert noncombat policy fallback",
            "Python implementation of simulator/game mechanics",
        ],
    }


def audit_native_source(
    source_root: str | Path,
    expected_commit: str,
    required_screens: tuple[str, ...] | list[str],
    required_action_capabilities: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    root = Path(source_root).expanduser().resolve()
    import subprocess

    actual_commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain"], text=True
    ).strip()
    if actual_commit != expected_commit:
        raise ValueError(f"native source commit mismatch: {actual_commit}")
    if dirty:
        raise ValueError("pinned native source checkout is dirty")
    source_file = root / "bindings" / "slaythespire.cpp"
    source = source_file.read_text(encoding="utf-8")
    result = inspect_source_text(source, required_screens, required_action_capabilities)
    result.update({
        "native_source_commit": actual_commit,
        "native_source_clean": True,
        "native_source_file": str(source_file),
        "native_source_file_sha256": _sha256(source_file),
    })
    return result
