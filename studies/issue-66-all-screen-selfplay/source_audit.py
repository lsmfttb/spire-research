"""Identity-bound audit of native semantic legal-choice coverage for Issue 66."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


REQUIRED_SCREENS = (
    "NEOW",
    "MAP_SCREEN",
    "REWARDS",
    "SHOP_ROOM",
    "REST_ROOM",
    "EVENT_SCREEN",
    "CARD_SELECT",
    "BOSS_RELIC_REWARDS",
    "TREASURE_ROOM",
)
POLICY_SCREENS = REQUIRED_SCREENS
SUPPORTED_NONCOMBAT = frozenset({"MAP_SCREEN", "REWARDS"})


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


def _line_number(source: str, marker: str) -> int:
    position = source.find(marker)
    if position < 0:
        raise ValueError(f"native source is missing expected API marker: {marker}")
    return source.count("\n", 0, position) + 1


def _literal_reason(body: str) -> str | None:
    match = re.search(r'payload\["reason"\]\s*=\s*"([^"]+)"', body)
    return match.group(1) if match else None


def inspect_source_text(source: str) -> dict[str, Any]:
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
    for screen in REQUIRED_SCREENS:
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

    missing = [
        screen for screen in POLICY_SCREENS
        if not screen_support.get(screen, {}).get("semantic_choices", False)
    ]
    if snapshot_has_screen_choices:
        blocker = "snapshot_choice_surface_requires_manual_source_review"
    elif missing:
        blocker = (
            "pinned_native_source_does_not_expose_semantic_choice_content_for: "
            + ", ".join(missing)
        )
    else:
        blocker = None
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
        "status": "BLOCKED" if blocker else "SUPPORTED",
        "blocker": blocker,
        "policy_gate": "FAIL_CLOSED" if blocker else "SOURCE_AUDIT_ONLY_NOT_EXECUTION_AUTHORIZATION",
        "prohibited_substitutions": [
            "native action id/index as semantic entity feature",
            "Expert noncombat policy fallback",
            "Python implementation of simulator/game mechanics",
        ],
    }


def audit_native_source(source_root: str | Path, expected_commit: str) -> dict[str, Any]:
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
    result = inspect_source_text(source)
    result.update({
        "native_source_commit": actual_commit,
        "native_source_clean": True,
        "native_source_file": str(source_file),
        "native_source_file_sha256": _sha256(source_file),
    })
    return result
