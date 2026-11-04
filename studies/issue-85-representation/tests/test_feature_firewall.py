from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
LEGACY_POLICY = ROOT / "upstream" / "issue73-policy.py"
sys.path.insert(0, str(ROOT))
import feature_firewall  # noqa: E402


def load_legacy_encoder():
    spec = importlib.util.spec_from_file_location("issue85_issue73_policy", LEGACY_POLICY)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load vendored locked Issue 73 policy")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def projection(target_index: int) -> dict:
    return {
        "schema_id": "native-privileged-noncombat-choice-projection-v1",
        "audience": "training_oracle_only",
        "public_api_safe": False,
        "policy_training_authorized": False,
        "native_source_commit": "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2",
        "candidate_order_source": "StepSimulator::legalActions; ordered occurrences are retained without deduplication",
        "coverage_status": "supported",
        "candidate_count": 1,
        "supported_candidate_count": 1,
        "unsupported_candidates": [],
        "screen_identity": {"availability": "available", "visibility": "public_now", "source": "GameContext::screenState", "value": "CARD_SELECT"},
        "shared_context": {"gold": {"availability": "available", "visibility": "public_now", "source": "GameContext::gold", "value": 88}},
        "candidates": [
            {
                "coverage_status": "supported",
                "candidate_binding": {"availability": "available", "visibility": "oracle_only", "value": {"candidate_index": 0, "candidate_action": {"scope": "event", "kind": "card_select", "idx1": target_index, "idx2": 0, "idx3": 0, "bits": "opaque"}}},
                "features": {
                    "target_deck_index": {"availability": "available", "visibility": "oracle_only", "source": "SelectScreenCard::deckIdx", "value": target_index},
                    "target_card": {"availability": "available", "visibility": "public_now", "source": "ScreenStateInfo::toSelectCards", "value": {"id": 42, "name": "Strike_R", "type": "ATTACK", "upgraded": False}},
                    "selection_count": {"availability": "available", "visibility": "oracle_only", "source": "ScreenStateInfo::toSelectCards", "value": 1},
                },
            }
        ],
    }


def public_projection(target_index: int) -> dict:
    return {
        "schema_id": "native-public-projection-v3",
        "screen_identity": {"availability": "available", "value": "CARD_SELECT"},
        "persistent_resources": {"availability": "available", "value": {"deck": [{"id": 42, "name": "Strike_R", "upgraded": False}]}},
        "screen_payload": {"availability": "available", "value": {"coverage_status": "supported", "choices": [{"candidate_index": 0, "candidate_action": {"scope": "event", "kind": "card_select", "idx1": target_index, "idx2": 0, "idx3": 0}, "choice_type": "card_select", "card": {"id": 42, "name": "Strike_R", "upgraded": False}}]}},
        "candidate_actions": {"availability": "available", "value": [{"scope": "event", "kind": "card_select", "idx1": target_index, "idx2": 0, "idx3": 0, "label": f"event.card_select idx1={target_index}"}]},
    }


def projection_with_legacy_indices(
    target_index: int, deck_index: int, potion_index: int, relic_index: int
) -> dict:
    value = projection(target_index)
    value["shared_context"].update(
        {
            "deck": {
                "availability": "available",
                "visibility": "public_now",
                "source": "GameContext::deck",
                "value": [
                    {
                        "id": 42,
                        "name": "Strike_R",
                        "upgraded": False,
                        "deck_index": deck_index,
                    }
                ],
            },
            "potions": {
                "availability": "available",
                "visibility": "public_now",
                "source": "GameContext::potions",
                "value": [
                    {"id": 7, "name": "Fire Potion", "potion_index": potion_index}
                ],
            },
            "relics": {
                "availability": "available",
                "visibility": "public_now",
                "source": "GameContext::relics",
                "value": [
                    {"id": 9, "name": "Burning Blood", "relic_index": relic_index}
                ],
            },
        }
    )
    return value


def public_projection_with_grandfathered_indices(target_index: int) -> dict:
    value = public_projection(target_index)
    value["persistent_resources"] = {
        "availability": "available",
        "visibility": "public_now",
        "value": {
            "deck": [{"id": 42, "name": "Strike_R", "upgraded": False, "deck_index": target_index}],
            "potion_identities": [{"id": 7, "name": "Fire Potion", "potion_index": target_index}],
            "relics": [{"id": 9, "relicIndex": target_index}],
            "potion_capacity": 3,
            "potion_count": 2,
        },
    }
    value["persistent_resources"]["value"]["relics"][0] = {
        "id": 9, "name": "Burning Blood", "relic_index": target_index,
    }
    return value


def bound_privileged_projection(screen: str, actions: list[dict], features: list[dict]) -> dict:
    return {
        "schema_id": "native-privileged-noncombat-choice-projection-v1",
        "audience": "training_oracle_only",
        "public_api_safe": False,
        "policy_training_authorized": False,
        "native_source_commit": "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2",
        "screen_identity": {"availability": "available", "visibility": "public_now", "value": screen},
        "shared_context": {"gold": {"availability": "available", "visibility": "public_now", "source": "GameContext::gold", "value": 88}},
        "candidates": [
            {
                "candidate_binding": {
                    "availability": "available",
                    "visibility": "oracle_only",
                    "value": {"candidate_index": i, "candidate_action": action.copy()},
                },
                "features": feature,
            }
            for i, (action, feature) in enumerate(zip(actions, features, strict=True))
        ],
    }


def v3_projection(screen: str, actions: list[dict], payload: dict, **fields) -> dict:
    result = {
        "schema_id": "native-public-projection-v3",
        "screen_identity": {"availability": "available", "value": screen},
        "candidate_actions": {"availability": "available", "value": [action.copy() for action in actions]},
        "screen_payload": {"availability": "available", "value": payload},
    }
    result.update(fields)
    return result


class FeatureFirewallTests(unittest.TestCase):
    def test_native_public_v3_envelopes_get_explicit_public_visibility(self):
        public = public_projection(0)
        self.assertTrue(
            all(
                "visibility" not in public[field]
                for field in (
                    "screen_identity",
                    "persistent_resources",
                    "screen_payload",
                    "candidate_actions",
                )
            )
        )

        result = feature_firewall.new_path_projection(projection(0), public)
        self.assertEqual(
            result["public_context"]["persistent_resources"]["visibility"],
            "public_now",
        )
        for field in (
            "visible_act_boss", "visible_map_graph", "current_map_node",
            "immediately_legal_routes", "persistent_resources",
        ):
            self.assertEqual(result["public_context"][field]["visibility"], "public_now", field)

    def test_unavailable_privileged_envelopes_cannot_carry_values(self):
        for target in ("shared", "candidate"):
            privileged = projection(0)
            if target == "shared":
                envelope = privileged["shared_context"]["gold"]
            else:
                envelope = privileged["candidates"][0]["features"]["target_card"]
            envelope["availability"] = "unavailable"
            with self.subTest(target=target), self.assertRaisesRegex(
                ValueError, "omitted when availability is unavailable"
            ):
                feature_firewall.new_path_projection(privileged, public_projection(0))

    def test_match_and_keep_card_state_uses_native_scalar_enum(self):
        public = public_projection(0)
        action = public["candidate_actions"]["value"][0]
        public["screen_payload"]["value"].update({
            "cards": [
                {
                    "index": 0,
                    "state": "revealed",
                    "card": {"id": 42, "name": "Strike_R", "upgraded": False},
                }
            ],
            "choices": [{
                "candidate_index": 0,
                "candidate_action": action.copy(),
                "choice_type": "card_select",
                "card_index": 0,
            }],
        })
        valid = feature_firewall.new_path_projection(projection(0), public)
        self.assertIn("revealed", json.dumps(valid, sort_keys=True))

        for state in ({"deck_offset": 3}, 0, "unrecognized_state"):
            malformed = public_projection(0)
            malformed["screen_payload"]["value"]["cards"] = [{"index": 0, "state": state}]
            with self.subTest(state=state), self.assertRaisesRegex(
                ValueError, "native public card-state enum"
            ):
                feature_firewall.new_path_projection(projection(0), malformed)

    def test_new_path_is_invariant_to_executor_index_and_bindings(self):
        first = feature_firewall.new_path_projection(projection(0), public_projection(0))
        second = feature_firewall.new_path_projection(projection(17), public_projection(17))
        feature_firewall.assert_index_free(first)
        feature_firewall.assert_index_free(second)
        self.assertEqual(first, second)
        self.assertIn("Strike_R", json.dumps(first, sort_keys=True))
        self.assertIn("selection_count", json.dumps(first, sort_keys=True))

    def test_legacy_encoder_intentionally_keeps_exact_historical_field(self):
        module = load_legacy_encoder()
        self.assertNotIn("target_deck_index", module._FORBIDDEN_KEYS)
        encoder = module.SemanticHashEncoder(2048)
        left, _ = encoder.encode(projection(3), 1)
        right, _ = encoder.encode(projection(19), 1)
        self.assertEqual(left.shape, (1, 2048))
        self.assertFalse(np.array_equal(left, right))

    def test_firewall_strips_aliases_and_preserves_card_semantics(self):
        raw = {
            "target_deck_index": 7,
            "physical_target_card_position": 7,
            "candidateIndex": 0,
            "native_action_bits": "0xabc",
            "target_card": {"id": 42, "name": "Strike_R", "upgraded": False},
            "source": "private debug provenance",
        }
        clean = feature_firewall.sanitize_executor_tree(raw)
        feature_firewall.assert_index_free(clean)
        self.assertEqual(clean, {"target_card": {"id": 42, "name": "Strike_R", "upgraded": False}})

    def test_assertion_fails_on_reintroduced_executor_alias(self):
        with self.assertRaisesRegex(ValueError, "opaque executor key"):
            feature_firewall.assert_index_free({"features": {"physical_card_slot": 2}})

    def test_all_grandfathered_indices_are_removed_from_new_paths(self):
        first_privileged = projection_with_legacy_indices(3, 0, 0, 0)
        second_privileged = projection_with_legacy_indices(100003, 100001, 100002, 100004)
        first_public = public_projection_with_grandfathered_indices(3)
        second_public = public_projection_with_grandfathered_indices(100003)

        first = feature_firewall.new_path_projection(first_privileged, first_public)
        second = feature_firewall.new_path_projection(second_privileged, second_public)

        feature_firewall.assert_index_free(first)
        feature_firewall.assert_index_free(second)
        self.assertEqual(first, second)
        serialized = json.dumps(first, sort_keys=True)
        for key in (
            "target_deck_index",
            "deck_index",
            "deckIndex",
            "potion_index",
            "relic_index",
        ):
            self.assertNotIn(key, serialized)
        self.assertIn('"potion_capacity": 3', serialized)
        self.assertIn('"potion_count": 2', serialized)
        self.assertIn("Strike_R", serialized)
        self.assertIn("Fire Potion", serialized)
        self.assertIn("Burning Blood", serialized)

    def test_positional_aliases_are_blocked_but_semantic_ids_survive(self):
        for key in (
            "potion_index",
            "relicIndex",
            "deckOrdinal",
            "physicalPosition",
            "executorToken",
            "native_action_id",
            "entity_id",
        ):
            self.assertTrue(feature_firewall.is_executor_identifier_key(key), key)
        for key in ("card_id", "potion_id", "relic_id", "potion_capacity", "free_potion_slots"):
            self.assertFalse(feature_firewall.is_executor_identifier_key(key), key)

    def test_reward_groups_become_content_semantics_without_native_group_indices(self):
        actions = [
            {"scope": "game", "kind": "reward_card", "idx1": 4, "idx2": 0, "idx3": 0},
            {"scope": "game", "kind": "reward_card", "idx1": 4, "idx2": 1, "idx3": 0},
            {"scope": "game", "kind": "reward_card", "idx1": 9, "idx2": 0, "idx3": 0},
        ]
        choices = [
            {"candidate_index": 0, "candidate_action": actions[0].copy(), "choice_type": "card", "card": {"id": 1, "name": "Strike"}},
            {"candidate_index": 1, "candidate_action": actions[1].copy(), "choice_type": "card", "card": {"id": 2, "name": "Bash"}},
            {"candidate_index": 2, "candidate_action": actions[2].copy(), "choice_type": "card", "card": {"id": 3, "name": "Inflame"}},
        ]
        features = [
            {
                "choice": {
                    "availability": "available",
                    "visibility": "public_now",
                    "source": "source-backed reward candidate semantics",
                    "value": {"choice_type": "card", "card": {"id": i}},
                }
            }
            for i in (1, 2, 3)
        ]
        privileged = bound_privileged_projection("REWARDS", actions, features)
        public = v3_projection("REWARDS", actions, {"coverage_status": "supported", "choices": choices})

        first = feature_firewall.new_path_projection(privileged, public)
        shifted_actions = [{**action, "idx1": action["idx1"] + 1000} for action in actions]
        shifted_choices = [
            {**choice, "candidate_action": shifted_actions[i].copy()}
            for i, choice in enumerate(choices)
        ]
        shifted_privileged = bound_privileged_projection("REWARDS", shifted_actions, features)
        shifted_public = v3_projection("REWARDS", shifted_actions, {"coverage_status": "supported", "choices": shifted_choices})
        second = feature_firewall.new_path_projection(shifted_privileged, shifted_public)

        self.assertEqual(first, second)
        feature_firewall.assert_index_free(first)
        group_a = first["candidates"][0]["public_choice"]
        group_b = first["candidates"][1]["public_choice"]
        group_c = first["candidates"][2]["public_choice"]
        self.assertEqual(group_a["reward_group_key"], group_b["reward_group_key"])
        self.assertNotEqual(group_a["reward_group_key"], group_c["reward_group_key"])
        self.assertEqual(len(group_a["reward_group_alternatives"]), 2)
        serialized = json.dumps(first, sort_keys=True)
        self.assertNotIn('"idx1"', serialized)
        self.assertNotIn('"idx2"', serialized)

    def test_candidate_action_mismatch_fails_closed(self):
        privileged = projection(3)
        public = public_projection(3)
        public["candidate_actions"]["value"][0]["idx1"] = 99
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            feature_firewall.new_path_projection(privileged, public)

    def test_unknown_executor_aliases_fail_closed_at_every_learned_input_boundary(self):
        alias_cases = (
            ("target_deck_pos", 17),
            ("deck_offset", "17"),
            ("target_deck_hash", "sha256:17"),
            ("selected_card_number", 17),
        )
        for alias, value in alias_cases:
            with self.subTest(alias=alias):
                privileged = projection(3)
                public = public_projection(3)
                privileged["candidates"][0]["features"]["target_card"]["value"][alias] = value
                with self.assertRaisesRegex(ValueError, "unrecognized input field"):
                    feature_firewall.new_path_projection(privileged, public)

        privileged = projection(3)
        public = public_projection(3)
        privileged["shared_context"]["deck"] = {
            "availability": "available", "visibility": "public_now",
            "source": "GameContext::deck",
            "value": [{"id": 42, "name": "Strike_R", "upgraded": False, "deck_index": 3, "deck_offset": 3}],
        }
        with self.assertRaisesRegex(ValueError, "unrecognized input field"):
            feature_firewall.new_path_projection(privileged, public)

        privileged = projection(3)
        public = public_projection(3)
        public["persistent_resources"]["value"] = {
            "deck": [{"id": 42, "name": "Strike_R", "upgraded": False, "deck_index": 3, "target_deck_hash": "sha256:3"}],
        }
        with self.assertRaisesRegex(ValueError, "unrecognized input field"):
            feature_firewall.new_path_projection(privileged, public)

    def test_unknown_map_location_values_fail_closed_at_every_public_path(self):
        action = {"scope": "game", "kind": "map", "idx1": 0, "idx2": 0, "idx3": 0}
        features = [
            {
                "choice": {
                    "availability": "available",
                    "visibility": "public_now",
                    "value": {"choice_type": "map_route"},
                }
            }
        ]
        graph = [
            {"x": 0, "y": 0, "room_symbol": "R", "outgoing_edges": [{"x": 0, "y": 1}]},
            {"x": 0, "y": 1, "room_symbol": "M", "outgoing_edges": []},
        ]
        destination = {"location": "map_node", "x": 0, "y": 1, "room_symbol": "M"}
        choice = {
            "candidate_index": 0,
            "candidate_action": action.copy(),
            "choice_type": "map_route",
            "destination": destination.copy(),
        }

        def make_public():
            return v3_projection(
                "MAP_SCREEN",
                [action],
                {"coverage_status": "supported", "choices": [choice.copy()]},
                visible_map_graph={
                    "availability": "available",
                    "visibility": "public_now",
                    "value": graph,
                },
                current_map_node={
                    "availability": "available",
                    "visibility": "public_now",
                    "value": {"location": "map_node", "x": 0, "y": 0, "room_symbol": "R"},
                },
                immediately_legal_routes={
                    "availability": "available",
                    "visibility": "public_now",
                    "value": [choice.copy()],
                },
            )

        def corrupt(public, path, location):
            bad = {"location": location, "x": 0, "y": 1, "room_symbol": "M"}
            if path == "current_map_node":
                public["current_map_node"]["value"] = bad
            elif path == "screen_current_map_node":
                public["screen_payload"]["value"]["current_map_node"] = bad
            elif path == "screen_choice_destination":
                public["screen_payload"]["value"]["choices"][0]["destination"] = bad
            elif path == "screen_legal_route_destination":
                public["screen_payload"]["value"]["legal_routes"] = [choice.copy()]
                public["screen_payload"]["value"]["legal_routes"][0]["destination"] = bad
            elif path == "immediately_legal_route_destination":
                public["immediately_legal_routes"]["value"][0]["destination"] = bad
            else:
                raise AssertionError(path)

        paths = (
            "current_map_node",
            "screen_current_map_node",
            "screen_choice_destination",
            "screen_legal_route_destination",
            "immediately_legal_route_destination",
        )
        for path in paths:
            for location in ("native_target_index_17", "map_node_index_17"):
                with self.subTest(path=path, location=location):
                    public = make_public()
                    corrupt(public, path, location)
                    with self.assertRaisesRegex(ValueError, "not a native map-location enum"):
                        feature_firewall.new_path_projection(
                            bound_privileged_projection("MAP_SCREEN", [action], features),
                            public,
                        )

    def test_map_location_sentinels_are_path_specific(self):
        current_locations = ("before_first_route", "act_boss_entry")
        route_locations = ("boss_entry",)
        for location in current_locations:
            with self.subTest(path="current", location=location):
                feature_firewall._check_map_location(
                    {"location": location}, "current_map_node.value",
                    feature_firewall._CURRENT_MAP_LOCATION_VALUES,
                )
                self.assertEqual(
                    feature_firewall._map_location(
                        {"location": location}, {}, feature_firewall._CURRENT_MAP_LOCATION_VALUES
                    ),
                    {"location": location},
                )
        for location in route_locations:
            with self.subTest(path="route", location=location):
                feature_firewall._check_map_location(
                    {"location": location}, "destination",
                    feature_firewall._ROUTE_MAP_LOCATION_VALUES,
                )
                self.assertEqual(
                    feature_firewall._map_location(
                        {"location": location}, {}, feature_firewall._ROUTE_MAP_LOCATION_VALUES
                    ),
                    {"location": location},
                )
        with self.assertRaisesRegex(ValueError, "not a native map-location enum"):
            feature_firewall._check_map_location(
                {"location": "boss_entry"}, "current_map_node.value",
                feature_firewall._CURRENT_MAP_LOCATION_VALUES,
            )
        with self.assertRaisesRegex(ValueError, "not a native map-location enum"):
            feature_firewall._check_map_location(
                {"location": "before_first_route"}, "destination",
                feature_firewall._ROUTE_MAP_LOCATION_VALUES,
            )

    def test_map_coordinates_are_joined_then_removed_while_topology_remains(self):
        action = {"scope": "game", "kind": "map", "idx1": 0, "idx2": 0, "idx3": 0}
        features = [{"choice": {"availability": "available", "visibility": "public_now", "source": "source-backed map semantics", "value": {"choice_type": "map_route"}}}]
        privileged = bound_privileged_projection("MAP_SCREEN", [action], features)
        graph = [
            {"x": 0, "y": 0, "room_symbol": "R", "outgoing_edges": [{"x": 0, "y": 1}]},
            {"x": 0, "y": 1, "room_symbol": "M", "outgoing_edges": [{"x": 1, "y": 2}, {"x": 2, "y": 2}]},
            {"x": 1, "y": 2, "room_symbol": "S", "outgoing_edges": []},
            {"x": 2, "y": 2, "room_symbol": "T", "outgoing_edges": []},
        ]
        choice = {"candidate_index": 0, "candidate_action": action.copy(), "choice_type": "map_route", "destination": {"location": "map_node", "x": 0, "y": 1, "room_symbol": "M"}}
        public = v3_projection(
            "MAP_SCREEN",
            [action],
            {"coverage_status": "supported", "choices": [choice]},
            visible_map_graph={"availability": "available", "visibility": "public_now", "value": graph},
            current_map_node={"availability": "available", "visibility": "public_now", "value": {"location": "map_node", "x": 0, "y": 0, "room_symbol": "R"}},
            immediately_legal_routes={"availability": "available", "visibility": "public_now", "value": [{**choice, "destination": choice["destination"].copy()}]},
            visible_act_boss={"availability": "available", "visibility": "public_now", "value": "THE_GUARDIAN"},
            persistent_resources={"availability": "available", "visibility": "public_now", "value": {"gold": 88}},
        )
        first = feature_firewall.new_path_projection(privileged, public)

        def shift(point):
            return {**point, "x": point["x"] + 20, "y": point["y"] + 100}

        shifted_action = {**action, "idx1": action["idx1"] + 20}
        shifted_privileged = bound_privileged_projection("MAP_SCREEN", [shifted_action], features)
        shifted_graph = [
            {**shift(node), "outgoing_edges": [shift(edge) for edge in node["outgoing_edges"]]}
            for node in graph
        ]
        shifted_destination = {**choice["destination"], "x": 20, "y": 101}
        shifted_choice = {**choice, "candidate_action": shifted_action, "destination": shifted_destination}
        shifted_public = v3_projection(
            "MAP_SCREEN",
            [shifted_action],
            {"coverage_status": "supported", "choices": [shifted_choice]},
            visible_map_graph={"availability": "available", "visibility": "public_now", "value": shifted_graph},
            current_map_node={"availability": "available", "visibility": "public_now", "value": {"location": "map_node", "x": 20, "y": 100, "room_symbol": "R"}},
            immediately_legal_routes={"availability": "available", "visibility": "public_now", "value": [{**shifted_choice, "destination": shifted_destination.copy()}]},
            visible_act_boss={"availability": "available", "visibility": "public_now", "value": "THE_GUARDIAN"},
            persistent_resources={"availability": "available", "visibility": "public_now", "value": {"gold": 88}},
        )
        second = feature_firewall.new_path_projection(shifted_privileged, shifted_public)
        self.assertEqual(first, second)
        feature_firewall.assert_index_free(first)
        self.assertIn("outgoing_room_symbols", json.dumps(first, sort_keys=True))
        self.assertNotIn('"x"', json.dumps(first, sort_keys=True))
        self.assertNotIn('"y"', json.dumps(first, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
