from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import adapter


@dataclass
class NativeAction:
    scope: str
    bits: int
    kind: str
    idx1: int
    idx2: int = 0
    idx3: int = 0
    label: str = ""


def action(index: int, kind: str, label_scope: str = "game") -> NativeAction:
    return NativeAction(
        scope=label_scope, bits=0xA500 + index, kind=kind, idx1=index,
        label=f"{label_scope}.{kind} bits={0xA500 + index} idx1={index} idx2=0 idx3=0",
    )


def projection(screen: str, options: list[dict], actions: list[NativeAction]) -> dict:
    candidates = []
    for native in actions:
        identity = adapter._public_identity(native)
        candidates.append(identity)
    if screen == "MAP_SCREEN":
        choices = [
            {
                "candidate_index": i,
                "candidate_action": candidates[i],
                "destination": {"location": "map_node", "x": i, "y": 0, "room_symbol": "M"},
            }
            for i in range(len(candidates))
        ]
        payload = {"coverage_status": "supported", "source": "test fixture", "legal_routes": choices}
        routes = {"availability": "available", "source": "test fixture", "value": choices}
    else:
        choices = [
            {
                "candidate_index": i,
                "candidate_action": candidates[i],
                "choice_type": "gold",
                "gold": options[i],
            }
            for i in range(len(candidates))
        ]
        payload = {"coverage_status": "supported", "source": "test fixture", "choices": choices}
        routes = {"availability": "unavailable", "reason": "only at MAP_SCREEN"}
    return {
        "schema_id": "native-public-projection-v3",
        "screen_identity": {"availability": "available", "source": "test", "value": screen},
        "visible_act_boss": {"availability": "available", "source": "map icon", "value": "THE_GUARDIAN"},
        "visible_map_graph": {"availability": "available", "source": "map", "value": [{"x": 0, "y": 0, "room_symbol": "M", "outgoing_edges": []}]},
        "current_map_node": {"availability": "available", "source": "position", "value": {"location": "before_first_route"}},
        "immediately_legal_routes": routes,
        "persistent_resources": {"availability": "available", "source": "native", "value": {"gold": {"availability": "available", "value": 99}}},
        "screen_payload": {"availability": "available", "source": "native", "value": payload},
        "candidate_actions": {"availability": "available", "source": "native", "value": candidates},
    }


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actions = [action(0, "reward_gold"), action(1, "reward_gold")]
        self.projection = projection("REWARDS", [17, 41], self.actions)

    def test_reward_options_are_public_ordered_and_keep_action_map_private(self) -> None:
        prepared = adapter.prepare_policy_input(self.projection, self.actions)
        parsed = json.loads(prepared.serialized_input)
        self.assertEqual(parsed["schema_id"], adapter.SCHEMA_ID)
        self.assertEqual(parsed["public_state"]["screen"], "REWARDS")
        self.assertEqual([x["candidate_index"] for x in parsed["legal_options"]], [0, 1])
        self.assertEqual([x["semantics"]["gold"] for x in parsed["legal_options"]], [17, 41])
        self.assertNotIn('"bits"', prepared.serialized_input)
        self.assertNotIn("bits=", prepared.serialized_input)
        self.assertNotIn("A500", prepared.serialized_input)

        callback_values: list[object] = []
        selected = adapter.call_policy(prepared, lambda value: callback_values.append(value) or 1)
        self.assertEqual(selected, 1)
        self.assertEqual(len(callback_values), 1)
        self.assertIsInstance(callback_values[0], str)
        resolved = adapter.resolve_native_action(prepared, selected, self.projection, self.actions)
        self.assertIs(resolved, self.actions[1])

    def test_resolution_uses_fresh_native_action_object(self) -> None:
        prepared = adapter.prepare_policy_input(self.projection, self.actions)
        refreshed = [action(0, "reward_gold"), action(1, "reward_gold")]
        refreshed[0].bits += 1000
        refreshed[1].bits += 1000
        resolved = adapter.resolve_native_action(prepared, 1, self.projection, refreshed)
        self.assertIs(resolved, refreshed[1])
        self.assertNotIsInstance(resolved, dict)

    def test_public_change_rejects_stale_mapping(self) -> None:
        prepared = adapter.prepare_policy_input(self.projection, self.actions)
        changed = projection("REWARDS", [17, 42], self.actions)
        with self.assertRaises(adapter.StaleChoiceMapping):
            adapter.resolve_native_action(prepared, 1, changed, self.actions)

    def test_candidate_order_and_action_parity_are_required(self) -> None:
        reversed_actions = list(reversed(self.actions))
        with self.assertRaises(adapter.ChoiceSurfaceError):
            adapter.prepare_policy_input(self.projection, reversed_actions)
        malformed = json.loads(json.dumps(self.projection))
        malformed["candidate_actions"]["value"][0]["bits"] = 12
        with self.assertRaises(adapter.ChoiceSurfaceError):
            adapter.prepare_policy_input(malformed, self.actions)

    def test_ambiguous_candidate_and_choice_associations_are_rejected(self) -> None:
        duplicate_actions = [action(0, "reward_gold"), action(0, "reward_gold")]
        duplicate_projection = projection("REWARDS", [17, 41], duplicate_actions)
        with self.assertRaises(adapter.ChoiceSurfaceError):
            adapter.prepare_policy_input(duplicate_projection, duplicate_actions)
        malformed = json.loads(json.dumps(self.projection))
        malformed["screen_payload"]["value"]["choices"][0]["candidate_index"] = 1
        with self.assertRaises(adapter.ChoiceSurfaceError):
            adapter.prepare_policy_input(malformed, self.actions)

    def test_unsupported_screens_and_partial_map_fail_closed(self) -> None:
        unsupported = json.loads(json.dumps(self.projection))
        unsupported["screen_identity"]["value"] = "REST_ROOM"
        with self.assertRaises(adapter.UnsupportedChoiceSurface):
            adapter.prepare_policy_input(unsupported, self.actions)

        map_actions = [action(0, "map")]
        partial = projection("MAP_SCREEN", [0], map_actions)
        partial["screen_payload"]["value"]["coverage_status"] = "partial"
        with self.assertRaises(adapter.UnsupportedChoiceSurface):
            adapter.prepare_policy_input(partial, map_actions)

    def test_map_options_keep_destination_separate(self) -> None:
        map_actions = [action(0, "map"), action(1, "map")]
        map_projection = projection("MAP_SCREEN", [0, 1], map_actions)
        prepared = adapter.prepare_policy_input(map_projection, map_actions)
        parsed = json.loads(prepared.serialized_input)
        self.assertEqual(parsed["public_state"]["screen"], "MAP_SCREEN")
        self.assertEqual(
            [option["semantics"]["destination"]["x"] for option in parsed["legal_options"]],
            [0, 1],
        )
        self.assertEqual(parsed["availability"]["visible_map_graph"], "available")


if __name__ == "__main__":
    unittest.main()