from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from runner import CONFIG
from source_audit import inspect_source_text
from runner import run_production


NATIVE_SOURCE_FIXTURE = r'''
pybind11::dict snapshot() {
  ret["screen_state"] = screenStateLabel(gc.screenState);
  ret["deck"] = deckSnapshot(gc);
  return ret;
}
pybind11::dict battleSearchNodeSnapshot() {}
pybind11::dict publicChoiceActionSnapshot(const LightSpeedAction &action) {
  ret["scope"] = action.scope;
  ret["kind"] = action.kind;
  ret["idx1"] = action.idx1;
  ret["idx2"] = action.idx2;
  ret["idx3"] = action.idx3;
  return ret;
}
pybind11::dict publicChoiceAssociation(const std::size_t candidateIndex, const LightSpeedAction &action) {
  ret["candidate_index"] = candidateIndex;
  ret["candidate_action"] = publicChoiceActionSnapshot(action);
}
bool isCurrentActBossSelection() {}
pybind11::dict publicMapRouteChoice() {
  route = publicChoiceAssociation(candidateIndex, action);
  route["choice_type"] = "map_route";
  route["destination"] = destination;
}
pybind11::list publicMapScreenChoices() { choices.append(publicMapRouteChoice()); }
pybind11::dict publicCardRewardSnapshot() {}
pybind11::dict publicRewardScreenPayload() {
  choice = publicChoiceAssociation(candidateIndex, action);
  choice["choice_type"] = "card";
}
pybind11::dict publicChoiceScreenPayload() {
  switch (state) {
    case ScreenState::REWARDS: return publicRewardScreenPayload(gc, actions);
    case ScreenState::MAP_SCREEN: payload["choices"] = mapChoices; return payload;
    case ScreenState::EVENT_SCREEN:
      payload["reason"] = "event option text is not source-backed by this choice surface";
      return payload;
    case ScreenState::BOSS_RELIC_REWARDS:
      payload["reason"] = "boss relic choice contents are not rendered by this capability";
      return payload;
    case ScreenState::CARD_SELECT:
      payload["reason"] = "card-select choice contents are not rendered by this capability";
      return payload;
    case ScreenState::TREASURE_ROOM:
      payload["reason"] = "treasure-room choice contents are not rendered by this capability";
      return payload;
    case ScreenState::REST_ROOM:
      payload["reason"] = "rest-room choice contents are not rendered by this capability";
      return payload;
    case ScreenState::SHOP_ROOM:
      payload["reason"] = "shop choice contents are not rendered by this capability";
      return payload;
    default: payload["reason"] = "current screen identity is not supported"; return payload;
  }
}
std::size_t knownDrawTopCount();
'''


class NativeSemanticGateTests(unittest.TestCase):
    def test_source_gap_is_a_blocker_and_indices_are_not_features(self) -> None:
        audit = inspect_source_text(NATIVE_SOURCE_FIXTURE)
        self.assertEqual(audit["status"], "BLOCKED")
        self.assertEqual(audit["policy_gate"], "FAIL_CLOSED")
        self.assertIn("EVENT_SCREEN", audit["missing_noncombat_semantics"])
        self.assertIn("NEOW", audit["missing_noncombat_semantics"])
        self.assertTrue(audit["candidate_action_surface"]["native_indices_exposed"])
        self.assertFalse(audit["candidate_action_surface"]["opaque_action_bits_exposed"])
        self.assertFalse(audit["snapshot_has_screen_choice_content"])

    def test_only_native_semantic_choice_screens_are_supported(self) -> None:
        audit = inspect_source_text(NATIVE_SOURCE_FIXTURE)
        self.assertTrue(audit["screens"]["MAP_SCREEN"]["semantic_choices"])
        self.assertTrue(audit["screens"]["REWARDS"]["semantic_choices"])
        for screen in ("EVENT_SCREEN", "CARD_SELECT", "SHOP_ROOM", "REST_ROOM", "TREASURE_ROOM", "BOSS_RELIC_REWARDS"):
            self.assertFalse(audit["screens"][screen]["semantic_choices"], screen)

    def test_config_forbids_index_fallback_and_expert(self) -> None:
        boundary = CONFIG["semantic_boundary"]
        self.assertFalse(boundary["action_ids_or_native_indices_as_semantic_features"])
        self.assertIsNone(boundary["unsupported_screen_fallback"])
        self.assertFalse(CONFIG["required_training"]["expert_action_supervision"])
        self.assertEqual(CONFIG["required_training"]["training_seed_range_inclusive"], [7100001, 7101024])
        self.assertEqual(CONFIG["required_evaluation"]["seed_range_inclusive"], [7110001, 7110096])

    def test_production_fails_closed_on_non_authorizing_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "preflight-decision.json"
            path.write_text(json.dumps({
                "schema_id": "issue66-preflight-decision-v1",
                "formal_execution_authorized_by_preflight": False,
            }), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "fail-closed"):
                run_production(Path(temporary))


if __name__ == "__main__":
    unittest.main()
