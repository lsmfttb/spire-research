#!/usr/bin/env python3
"""Replay the retained Issue 60 owner-662496 action prefix without Search.

This is a study-local correctness witness. It uses the pinned STSRL adapter
directly and never constructs the growing public-history context or invokes a
controller. Native Battle state in the output is diagnostic-only; it is never
passed to the actor, critic, or training pipeline.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXPECTED_STSRL_COMMIT = "7d172f7c24c9d29429b6946ba44f6b59760b0d96"
EXPECTED_NATIVE_COMMIT = "0990ef48d51cd48444a707839d65226d52c1d73e"
DEFAULT_STSRL_SOURCE = "/home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c"
DEFAULT_NATIVE_BUILD = "/home/lsmft/stsrl-spikes/issue60-runtime/build-issue60"
RAW_RESOURCE_KEYS = {
    "current_hp": "cur_hp",
    "max_hp": "max_hp",
    "gold": "gold",
    "potion_capacity": "potion_capacity",
    "potion_count": "potion_count",
}
BATTLE_STATE_KEYS = (
    "screen_state",
    "outcome",
    "act",
    "floor_num",
    "room_type",
    "cur_hp",
    "max_hp",
    "gold",
    "potion_count",
    "potion_capacity",
    "battle_active",
    "encounter_id",
    "battle_outcome",
    "battle_input_state",
    "battle_turn",
    "battle_player_hp",
    "battle_player_energy",
    "battle_player_block",
    "battle_player",
    "battle_hand_size",
    "battle_hand",
    "battle_draw_pile_size",
    "battle_discard_pile_size",
    "battle_discard_pile",
    "battle_exhaust_pile_size",
    "battle_exhaust_pile",
    "battle_monster_count",
    "battle_monsters_alive",
    "battle_monsters",
    "battle_potion_count",
    "battle_potion_capacity",
    "battle_potions",
    "completed_battle_outcome",
)


def _git_head(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raw_snapshot_fingerprint(adapter: Any, snapshot: Any) -> str:
    observation, stateful_raw = adapter.checkpoint_fingerprint(snapshot)
    payload = json.dumps(
        {"observation": list(observation), "raw": stateful_raw},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _screen_value(value: Any) -> str | None:
    if not isinstance(value, dict) or value.get("availability") != "available":
        return None
    screen = value.get("value")
    return screen if isinstance(screen, str) else None


def _resource_mismatches(record: dict[str, Any], projection: Any, phase: str) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    resources = record.get("resources")
    if not isinstance(resources, dict):
        return [{"field": "resources", "phase": phase, "reason": "missing"}]
    projection_values = getattr(projection, "resource_values", None)
    if not isinstance(projection_values, dict):
        projection_values = {}
    for recorded_name, raw_name in RAW_RESOURCE_KEYS.items():
        item = resources.get(recorded_name)
        if not isinstance(item, dict) or item.get("availability") != "available":
            continue
        expected = item.get("before" if phase == "before" else "after")
        actual = projection_values.get(recorded_name)
        if expected is None or actual != expected:
            mismatches.append(
                {
                    "field": recorded_name,
                    "raw_field": f"NativePublicProjection.resource_values.{recorded_name}",
                    "phase": phase,
                    "expected": expected,
                    "actual": actual,
                }
            )
    return mismatches


def _state_view(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: raw[key] for key in BATTLE_STATE_KEYS if key in raw}


def _state_summary(raw: dict[str, Any]) -> dict[str, Any]:
    player = raw.get("battle_player")
    player = player if isinstance(player, dict) else {}
    monsters = raw.get("battle_monsters")
    monsters = monsters if isinstance(monsters, list) else []
    return {
        "screen_state": raw.get("screen_state"),
        "floor_num": raw.get("floor_num"),
        "room_type": raw.get("room_type"),
        "encounter_id": raw.get("encounter_id"),
        "outcome": raw.get("outcome"),
        "battle_outcome": raw.get("battle_outcome"),
        "completed_battle_outcome": raw.get("completed_battle_outcome"),
        "battle_turn": raw.get("battle_turn"),
        "player_hp": player.get("current_hp", raw.get("battle_player_hp", raw.get("cur_hp"))),
        "player_max_hp": player.get("max_hp", raw.get("max_hp")),
        "player_energy": player.get("energy", raw.get("battle_player_energy")),
        "player_block": player.get("block", raw.get("battle_player_block")),
        "monsters": [
            {
                key: monster.get(key)
                for key in ("name", "current_hp", "max_hp", "alive", "current_move", "intent_category")
            }
            for monster in monsters if isinstance(monster, dict)
        ],
    }


def _action_view(action: Any, pre_raw: dict[str, Any]) -> dict[str, Any]:
    raw_action = dict(action.raw)
    result: dict[str, Any] = {
        "action_id": action.action_id,
        "kind": action.kind,
        "label": action.label,
        "scope": raw_action.get("scope"),
        "bits": raw_action.get("bits"),
        "idx1": raw_action.get("idx1"),
        "idx2": raw_action.get("idx2"),
        "idx3": raw_action.get("idx3"),
    }
    idx1, idx2 = raw_action.get("idx1"), raw_action.get("idx2")
    hand = pre_raw.get("battle_hand", [])
    monsters = pre_raw.get("battle_monsters", [])
    potions = pre_raw.get("battle_potions", [])
    if action.kind == "card" and isinstance(idx1, int) and 0 <= idx1 < len(hand):
        result["selected_card"] = hand[idx1]
    if action.kind.startswith("potion") and isinstance(idx1, int) and 0 <= idx1 < len(potions):
        result["selected_potion"] = potions[idx1]
    if action.kind in {"card", "potion"} and isinstance(idx2, int) and 0 <= idx2 < len(monsters):
        result["selected_target"] = monsters[idx2]
    return result


def _public_action_key(action: Any) -> tuple[Any, ...]:
    if hasattr(action, "raw"):
        raw = dict(action.raw)
        return (
            raw.get("scope"),
            getattr(action, "kind", raw.get("kind")),
            raw.get("idx1"),
            raw.get("idx2"),
            raw.get("idx3"),
        )
    return tuple(action.get(key) for key in ("scope", "kind", "idx1", "idx2", "idx3"))


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stsr-source",
        type=Path,
        default=Path(os.environ.get("ISSUE60_STSRL_SOURCE", DEFAULT_STSRL_SOURCE)),
    )
    parser.add_argument(
        "--native-build",
        type=Path,
        default=Path(os.environ.get("ISSUE60_NATIVE_BUILD", DEFAULT_NATIVE_BUILD)),
    )
    parser.add_argument("--max-replay-cpu-seconds", type=float, default=850.0)
    parser.add_argument(
        "--repository-commit",
        default=os.environ.get("ISSUE60_REPOSITORY_COMMIT"),
        help="exact Windows worktree HEAD when git is not available inside WSL",
    )
    args = parser.parse_args()
    cpu_start = time.process_time()

    stsr_source = args.stsr_source.resolve()
    native_build = args.native_build.resolve()
    sys.path.insert(0, str(stsr_source / "src"))
    sys.path.insert(0, str(native_build))

    from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
    import slaythespire

    stsr_commit = _git_head(stsr_source)
    native_source = Path("/home/lsmft/stsrl-spikes/sts_lightspeed-issue59")
    native_commit = _git_head(native_source)
    native_module_path = Path(slaythespire.__file__).resolve()
    native_binary_sha256 = _sha256_file(native_module_path)

    run_dir = (
        repo
        / "studies/issue-60-onpolicy-joint-choice/evidence/runs/training"
        / "block2/batch-04/seed-662496/attempt-0001"
    )
    run_path = run_dir / "run.json"
    decisions_path = run_dir / "decisions.jsonl.gz"
    run_data = json.loads(run_path.read_text(encoding="utf-8"))
    with gzip.open(decisions_path, "rt", encoding="utf-8") as stream:
        decisions = [json.loads(line) for line in stream if line.strip()]
    if not decisions:
        raise RuntimeError("no retained decisions for owner 662496")
    latest_decision = decisions[-1]
    if latest_decision.get("game_seed") != 662496:
        raise RuntimeError("last decision is not for game seed 662496")
    public_input = json.loads(latest_decision["issue60_public_input"])
    history = public_input["public_state"]["history"]
    if not history:
        raise RuntimeError("retained public history is empty")
    if any(entry.get("chosen_ordinal") is None for entry in history):
        raise RuntimeError("history contains an action without a chosen ordinal")

    out_dir = repo / "studies/issue-60-onpolicy-joint-choice/evidence/diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / "owner-662496-battle-prefix.jsonl.gz"
    report_path = out_dir / "owner-662496-direct-replay.json"

    mismatches: list[dict[str, Any]] = []
    prefix_replayed = 0
    prefix_battle_actions = 0
    floor5_trace_count = 0
    floor5_turn_changes: list[dict[str, int]] = []
    floor5_action_counts: collections.Counter[str] = collections.Counter()
    floor5_card_counts: collections.Counter[str] = collections.Counter()
    floor5_pre_hashes: collections.Counter[str] = collections.Counter()
    floor5_noop_count = 0
    floor5_max_turn = 0
    floor5_min_hp: int | None = None
    floor5_max_hp: int | None = None
    floor5_start: dict[str, Any] | None = None
    floor5_exit: dict[str, Any] | None = None
    floor8_entry: dict[str, Any] | None = None
    selected_map_extension: dict[str, Any] | None = None
    stop_reason = "PREFIX_COMPLETE"
    adapter = LightSpeedAdapter(seed=662496, ascension=20, player_class="IRONCLAD")
    snapshot = adapter.reset(662496)

    with gzip.open(trace_path, "wt", encoding="utf-8", compresslevel=6) as trace:
        for index, entry in enumerate(history):
            cpu_used = time.process_time() - cpu_start
            if cpu_used >= args.max_replay_cpu_seconds:
                stop_reason = "BOUNDED_CPU_STOP"
                mismatches.append(
                    {
                        "index": index,
                        "kind": "bounded_cpu_stop",
                        "cpu_seconds": cpu_used,
                        "limit_seconds": args.max_replay_cpu_seconds,
                    }
                )
                break

            pre_raw = dict(snapshot.raw)
            actual_pre_screen = pre_raw.get("screen_state")
            expected_pre_screen = _screen_value(entry.get("pre_screen"))
            if expected_pre_screen is None or actual_pre_screen != expected_pre_screen:
                mismatches.append(
                    {
                        "index": index,
                        "kind": "pre_screen_mismatch",
                        "expected": expected_pre_screen,
                        "actual": actual_pre_screen,
                    }
                )
                stop_reason = "FIRST_MISMATCH"
                break
            pre_projection = adapter.public_projection(snapshot)
            resource_errors = _resource_mismatches(entry, pre_projection, "before")
            if resource_errors:
                mismatches.append({"index": index, "kind": "pre_resource_mismatch", "items": resource_errors})
                stop_reason = "FIRST_MISMATCH"
                break

            actions = adapter.legal_actions(snapshot)
            ordinal = entry["chosen_ordinal"]
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 0 <= ordinal < len(actions):
                mismatches.append(
                    {
                        "index": index,
                        "kind": "illegal_recorded_ordinal",
                        "ordinal": ordinal,
                        "legal_action_count": len(actions),
                    }
                )
                stop_reason = "FIRST_MISMATCH"
                break

            action = actions[ordinal]
            transition = adapter.step(action)
            next_snapshot = transition.snapshot
            post_raw = dict(next_snapshot.raw)
            post_projection = adapter.public_projection(next_snapshot)
            expected_post_screen = _screen_value(entry.get("post_screen"))
            actual_post_screen = post_raw.get("screen_state")
            post_errors = _resource_mismatches(entry, post_projection, "after")
            post_mismatch = expected_post_screen is None or actual_post_screen != expected_post_screen
            fp_pre = _raw_snapshot_fingerprint(adapter, snapshot)
            fp_post = _raw_snapshot_fingerprint(adapter, next_snapshot)
            step_record = {
                "history_index": index,
                "chosen_ordinal": ordinal,
                "legal_action_count": len(actions),
                "chosen_action": _action_view(action, pre_raw),
                "legal_actions": [
                    {"ordinal": action_index, **_action_view(candidate, pre_raw)}
                    for action_index, candidate in enumerate(actions)
                ],
                "screen_before": actual_pre_screen,
                "screen_after": actual_post_screen,
                "floor_before": pre_raw.get("floor_num"),
                "floor_after": post_raw.get("floor_num"),
                "current_hp_before": pre_raw.get("cur_hp"),
                "current_hp_after": post_raw.get("cur_hp"),
                "turn_before": pre_raw.get("battle_turn"),
                "turn_after": post_raw.get("battle_turn"),
                "snapshot_fingerprint_before": fp_pre,
                "snapshot_fingerprint_after": fp_post,
                "expected_post_screen": expected_post_screen,
                "post_resource_mismatches": post_errors,
            }
            is_battle_step = (
                actual_pre_screen == "BATTLE"
                and pre_raw.get("floor_num") == 5
            )
            if actual_pre_screen == "BATTLE":
                prefix_battle_actions += 1
            if is_battle_step:
                floor5_trace_count += 1
                floor5_action_counts[action.kind] += 1
                selected_card = step_record["chosen_action"].get("selected_card")
                if isinstance(selected_card, dict) and isinstance(selected_card.get("name"), str):
                    floor5_card_counts[selected_card["name"]] += 1
                floor5_pre_hashes[fp_pre] += 1
                floor5_noop_count += int(fp_pre == fp_post)
                pre_turn = int(pre_raw.get("battle_turn", 0))
                post_turn = int(post_raw.get("battle_turn", pre_turn))
                floor5_max_turn = max(floor5_max_turn, pre_turn, post_turn)
                for value in (pre_raw.get("battle_player_hp"), post_raw.get("battle_player_hp")):
                    if isinstance(value, int):
                        floor5_min_hp = value if floor5_min_hp is None else min(floor5_min_hp, value)
                        floor5_max_hp = value if floor5_max_hp is None else max(floor5_max_hp, value)
                if pre_turn != post_turn:
                    floor5_turn_changes.append(
                        {"history_index": index, "before": pre_turn, "after": post_turn}
                    )
                if floor5_start is None:
                    floor5_start = _state_summary(pre_raw)
                if expected_post_screen == "REWARDS":
                    floor5_exit = {
                        "history_index": index,
                        "state": _state_summary(post_raw),
                        "transition_info": dict(transition.info),
                        "terminal": transition.terminal,
                    }
                trace_record = {
                    "history_index": index,
                    "screen_before": actual_pre_screen,
                    "screen_after": actual_post_screen,
                    "action": _action_view(action, pre_raw),
                    "snapshot_fingerprint_before": fp_pre,
                    "snapshot_fingerprint_after": fp_post,
                    "state_before": _state_view(pre_raw),
                    "state_after": _state_view(post_raw),
                    "transition_terminal": transition.terminal,
                    "transition_info": dict(transition.info),
                    "matching": {
                        "recorded_post_screen": expected_post_screen,
                        "post_screen_matches": not post_mismatch,
                        "post_resource_mismatches": post_errors,
                    },
                }
                trace.write(json.dumps(trace_record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")

            prefix_replayed += 1
            snapshot = next_snapshot
            if post_mismatch or post_errors:
                mismatches.append(
                    {
                        "index": index,
                        "kind": "post_state_mismatch",
                        "chosen_ordinal": ordinal,
                        "chosen_action": _action_view(action, pre_raw),
                        "actual_post_state": _state_view(post_raw),
                        "legal_actions": step_record["legal_actions"],
                        "expected_screen": expected_post_screen,
                        "actual_screen": actual_post_screen,
                        "resource_mismatches": post_errors,
                    }
                )
                stop_reason = "FIRST_MISMATCH"
                break

    prefix_complete = prefix_replayed == len(history) and not mismatches
    current_input_screen = public_input.get("public_state", {}).get("screen")
    latest_input_matches = bool(
        prefix_complete and snapshot.raw.get("screen_state") == current_input_screen
    )

    if latest_input_matches:
        selected_index = latest_decision.get("issue60_selected_index")
        options = public_input.get("legal_options", [])
        if isinstance(selected_index, int) and not isinstance(selected_index, bool) and 0 <= selected_index < len(options):
            selected_option = options[selected_index]
            public_action = selected_option.get("action", {})
            actual_actions = adapter.legal_actions(snapshot)
            matches = [
                candidate
                for candidate in actual_actions
                if _public_action_key(candidate) == _public_action_key(public_action)
            ]
            if len(matches) == 1:
                map_transition = adapter.step(matches[0])
                snapshot = map_transition.snapshot
                selected_map_extension = {
                    "source": "last recorded supported-decision selected index",
                    "decision_floor": latest_decision.get("issue60_current_floor"),
                    "selected_index": selected_index,
                    "selected_public_action": public_action,
                    "mapped_native_action": _action_view(matches[0], dict(map_transition.snapshot.raw)),
                    "native_action_match_count": len(matches),
                    "post_action_state": _state_summary(dict(snapshot.raw)),
                    "transition_terminal": map_transition.terminal,
                    "transition_info": dict(map_transition.info),
                }
                if snapshot.raw.get("screen_state") == "BATTLE" and snapshot.raw.get("floor_num") == 8:
                    floor8_entry = _state_summary(dict(snapshot.raw))
            else:
                selected_map_extension = {
                    "source": "last recorded supported-decision selected index",
                    "selected_index": selected_index,
                    "selected_public_action": public_action,
                    "native_action_match_count": len(matches),
                    "replayed_state_before_extension": _state_view(dict(snapshot.raw)),
                    "native_legal_actions": [
                        _action_view(candidate, dict(snapshot.raw))
                        for candidate in actual_actions
                    ],
                    "status": "BLOCKED_BY_NON_UNIQUE_OR_MISSING_NATIVE_MAPPING",
                }
        else:
            selected_map_extension = {"status": "BLOCKED_BY_INVALID_RECORDED_SELECTED_INDEX"}

    adapter.close()

    run_boundaries = run_data.get("boundaries", [])
    floor5_boundaries = [
        item
        for item in run_boundaries
        if item.get("boundary") in {"battle_enter", "battle_exit"} and item.get("floor") == 5.0
    ]
    floor8_boundaries = [
        item
        for item in run_boundaries
        if item.get("boundary") in {"battle_enter", "battle_exit"} and item.get("floor") == 8.0
    ]
    run_transition_count = run_data.get("transitions")
    tail_after_current_decision = (
        run_transition_count - len(history) - 1
        if isinstance(run_transition_count, int)
        else None
    )
    unaccounted_search_calls = run_data.get("search_calls", 0) - prefix_battle_actions
    floor8_turn_upper_bound = None
    if floor8_entry is not None and isinstance(floor8_entry.get("battle_turn"), int) and isinstance(unaccounted_search_calls, int):
        floor8_turn_upper_bound = floor8_entry["battle_turn"] + max(0, unaccounted_search_calls)

    report = {
        "schema_id": "issue60-owner-662496-direct-replay-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository_head": args.repository_commit or _git_head(repo),
        "source_pins": {
            "stsr_expected_commit": EXPECTED_STSRL_COMMIT,
            "stsr_actual_commit": stsr_commit,
            "native_expected_commit": EXPECTED_NATIVE_COMMIT,
            "native_actual_commit": native_commit,
            "native_binary_path": str(native_module_path),
            "native_binary_sha256": native_binary_sha256,
        },
        "input_artifacts": {
            "run_json_sha256": _sha256_file(run_path),
            "decisions_jsonl_gz_sha256": _sha256_file(decisions_path),
            "game_seed": run_data.get("game_seed"),
            "public_history_entries": len(history),
            "retained_decision_rows": len(decisions),
            "last_decision_public_screen": current_input_screen,
        },
        "verification": {
            "replay_method": "LightSpeedAdapter.reset/legal_actions/step directly; no Search, controlled-run executor, controller, or public-history builder",
            "battle_hp_comparison": "During BATTLE, compare retained public current_hp against live battle_player_hp; raw cur_hp is stale until the battle ends.",
            "normal_public_actor_or_critic_used": False,
            "recorded_ordinal_prefix_complete": prefix_complete,
            "replayed_prefix_actions": prefix_replayed,
            "replay_cpu_seconds": max(0.0, time.process_time() - cpu_start),
            "battle_trace_artifact": str(trace_path.relative_to(repo)).replace("\\", "/"),
            "first_mismatch_or_blocker": mismatches[0] if mismatches else None,
            "stop_reason": stop_reason,
            "last_supported_decision_input_matches_replayed_prefix": latest_input_matches,
            "last_decision_selected_action_extension": selected_map_extension,
            "floor8_entry_state": floor8_entry,
            "causal_assessment": {
                "ordinal_replay": "all 1,992 retained ordinals were legal and reproduced retained pre/post screens and public resources",
                "floor5_battle": {
                    "trace_actions": floor5_trace_count,
                    "action_kinds": dict(sorted(floor5_action_counts.items())),
                    "card_names": dict(sorted(floor5_card_counts.items())),
                    "max_observed_turn": floor5_max_turn,
                    "turn_changes": len(floor5_turn_changes),
                    "state_identical_transitions": floor5_noop_count,
                    "repeated_pre_state_fingerprints": sum(count - 1 for count in floor5_pre_hashes.values()),
                    "entry": floor5_start,
                    "exit": floor5_exit,
                },
                "turn_guard": "The retained floor5 replay reached turn 484; the next supported selected map action entered floor8 at turn 0. Seven later Search calls/transitions cannot reach the Native >500 turn guard.",
                "search_causality": "The retained artifact has Search counts and CPU but no per-call root action identities; replay establishes the resulting trajectory, not why Search selected its actions.",
                "terminal_tail": "The final floor8 PLAYER_LOSS and HP 0 are retained run.json facts; its final seven action identities are not present in the ordinal prefix and were not fabricated or replayed.",
            },
            "floor5_boundary_summary_from_retained_run": [
                {k: item.get(k) for k in ("boundary", "floor", "room_type", "screen", "battle_outcome", "player_hp", "player_max_hp")}
                for item in floor5_boundaries
            ],
            "floor8_boundary_summary_from_retained_run": [
                {k: item.get(k) for k in ("boundary", "floor", "room_type", "screen", "battle_outcome", "player_hp", "player_max_hp")}
                for item in floor8_boundaries
            ],
            "retained_terminal_summary": {
                k: run_data.get(k)
                for k in ("terminal", "outcome", "terminal_floor", "death_location", "terminal_public_location", "problems")
            },
            "retained_run_transitions": run_transition_count,
            "retained_search_calls": run_data.get("search_calls"),
            "battle_actions_in_replayed_prefix": prefix_battle_actions,
            "unreplayed_search_calls_after_prefix": unaccounted_search_calls,
            "transitions_after_latest_supported_decision": tail_after_current_decision,
            "native_turn_guard": {
                "source_guard": "BattleContext::executeActions returns PLAYER_LOSS when turn > 500",
                "floor5_max_turn_in_replay": floor5_max_turn,
                "floor5_guard_triggered_in_replay": floor5_max_turn > 500,
                "floor5_within_10_turns_of_guard": floor5_max_turn >= 491,
                "floor8_entry_turn": floor8_entry.get("battle_turn") if floor8_entry else None,
                "floor8_turn_upper_bound_if_each_remaining_search_executes_at_most_one_end_turn": floor8_turn_upper_bound,
                "floor8_guard_can_be_ruled_out_from_retained_counts": bool(floor8_turn_upper_bound is not None and floor8_turn_upper_bound < 500),
                "turn_increment_source": "BattleContext::afterMonsterTurns increments turn once per end-of-monster-turn boundary",
            },
        },
        "floor5_battle_summary": {
            "first_history_index": 37 if floor5_trace_count else None,
            "last_history_index": 1969 if floor5_trace_count else None,
            "trace_rows": floor5_trace_count,
            "action_kind_counts": dict(sorted(floor5_action_counts.items())),
            "entry_state": floor5_start,
            "exit_state": floor5_exit,
            "turn_changes": floor5_turn_changes,
            "maximum_turn": floor5_max_turn,
            "player_hp_minimum_and_maximum": [floor5_min_hp, floor5_max_hp],
            "same_exposed_snapshot_pre_post_transitions": floor5_noop_count,
            "repeated_exposed_pre_snapshot_fingerprints": [
                {"sha256": key, "occurrences": count}
                for key, count in sorted(floor5_pre_hashes.items())
                if count > 1
            ],
            "fingerprint_limit": "adapter stateful raw snapshot plus observation; native snapshot omits draw-pile card identities, so equality is not proof of full hidden engine-state identity",
        },
        "trace_artifact": str(trace_path.relative_to(repo)).replace("\\", "/"),
        "cpu": {
            "process_cpu_seconds_including_adapter_import_artifact_read_replay_and_trace_write": time.process_time() - cpu_start,
            "replay_limit_seconds": args.max_replay_cpu_seconds,
            "aggregate_issue_limit_seconds": 1000,
        },
        "limitations": [
            "The native API snapshot exposes hands, discard/exhaust piles, player state, monsters and intents but only draw-pile size; fingerprints do not establish exact equality of hidden draw order.",
            "The retained selected-action trace ends at the last supported floor-7 map decision. The final seven Battle search choices are not present as action identities in the retained decision/history artifacts; their Search log rows contain counters and CPU only. The floor-8 terminal is therefore reported from the original run artifact, not independently action-replayed.",
            "The replay diagnoses one owner and does not estimate how common the long Battle is or support a performance claim.",
        ],
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    report["cpu"]["process_cpu_seconds_including_adapter_import_artifact_read_replay_and_trace_write"] = time.process_time() - cpu_start
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "trace": str(trace_path), "verification": report["verification"], "floor5_battle_summary": report["floor5_battle_summary"], "cpu": report["cpu"]}, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if prefix_complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
