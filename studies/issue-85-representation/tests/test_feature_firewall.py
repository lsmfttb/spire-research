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
                "candidate_binding": {"availability": "available", "visibility": "oracle_only", "value": {"candidate_index": 0, "candidate_action": {"scope": "event", "kind": "card_select", "idx1": 1, "idx2": 2, "idx3": 0, "bits": "opaque"}}},
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
        "screen_identity": {"availability": "available", "visibility": "public_now", "value": "CARD_SELECT"},
        "persistent_resources": {"availability": "available", "visibility": "public_now", "value": {"deck": [{"id": 42, "name": "Strike_R", "upgraded": False}]}},
        "screen_payload": {"availability": "available", "visibility": "public_now", "value": {"coverage_status": "supported", "choices": [{"candidate_index": 0, "candidate_action": {"scope": "event", "kind": "card_select", "idx1": target_index, "idx2": 0, "idx3": 0}, "card": {"id": 42, "name": "Strike_R", "upgraded": False}}]}},
        "candidate_actions": {"availability": "available", "visibility": "public_now", "value": [{"scope": "event", "kind": "card_select", "idx1": target_index, "idx2": 0, "idx3": 0, "label": f"event.card_select idx1={target_index}"}]},
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


def public_projection_with_executor_aliases(target_index: int) -> dict:
    value = public_projection(target_index)
    value["persistent_resources"] = {
        "availability": "available",
        "visibility": "public_now",
        "value": {
            "deck": [{"id": 42, "deckIndex": target_index, "position_hash": str(target_index)}],
            "potions": [{"id": 7, "potion_index": target_index}],
            "relics": [{"id": 9, "relicIndex": target_index}],
            "potion_capacity": 3,
            "free_potion_slots": 1,
        },
    }
    value["screen_payload"]["value"]["choices"][0]["executor_token"] = f"native-{target_index}"
    return value


class FeatureFirewallTests(unittest.TestCase):
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
        first_public = public_projection_with_executor_aliases(3)
        second_public = public_projection_with_executor_aliases(100003)

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
            "position_hash",
            "executor_token",
        ):
            self.assertNotIn(key, serialized)
        self.assertIn('"potion_capacity": 3', serialized)
        self.assertIn('"free_potion_slots": 1', serialized)
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


if __name__ == "__main__":
    unittest.main()
