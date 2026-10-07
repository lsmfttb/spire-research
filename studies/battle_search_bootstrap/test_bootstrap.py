from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from run import INFORMATION_REGIME, SIMULATOR_COMMIT, run_bootstrap


class FakeAction:
    scope = "battle"
    bits = 7
    kind = "card"
    idx1 = 0
    idx2 = 0
    idx3 = 0
    label = "fixture action"


class RaisingSearchSimulator:
    def snapshot(self):
        return {"battle_active": True, "screen_state": "BATTLE"}

    def legal_actions(self):
        return [FakeAction()]

    def battle_search_v2(self, simulations, include_potions):
        raise RuntimeError("controlled search failure")


class SearchFailureMarkerTest(unittest.TestCase):
    def test_search_exception_leaves_entered_boundary_and_failure_marker(self):
        config = {
            "character": "IRONCLAD",
            "ascension": 0,
            "seed": 1,
            "simulator_commit": SIMULATOR_COMMIT,
            "information_regime": INFORMATION_REGIME,
            "battle_entry": {"selector": "first_legal_action", "max_steps": 32},
            "search": {
                "api": "battle_search_v2",
                "simulations": 400,
                "include_potions": False,
                "policy_prior_callback": None,
                "leaf_value_callback": None,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            output_dir = root / "run"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "controlled search failure"):
                run_bootstrap(
                    config_path=config_path,
                    output_dir=output_dir,
                    simulator_factory=lambda _: RaisingSearchSimulator(),
                    code_commit="test-commit",
                )

            events = [
                json.loads(line)
                for line in (output_dir / "progress.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            phases = [event["phase"] for event in events]
            self.assertEqual(
                phases,
                ["prepared", "simulator_ready", "input_qualified", "search_entered", "failed"],
            )
            self.assertEqual(events[-1]["last_completed_phase"], "search_entered")
            self.assertFalse((output_dir / "result.json").exists())


if __name__ == "__main__":
    unittest.main()