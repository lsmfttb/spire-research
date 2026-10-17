from __future__ import annotations

import unittest
from unittest.mock import patch

import continue_after_cost_bridge as continuation


class ContinuationCostTaggingTests(unittest.TestCase):
    def test_dispatcher_cost_hook_is_installed_before_continuation(self) -> None:
        calls: list[str] = []

        def install_hook() -> None:
            calls.append("install")

        def continue_program() -> int:
            calls.append("continue")
            return 75

        with patch.object(
            continuation.bridge, "_install_tagged_dispatcher", install_hook,
        ), patch.object(
            continuation.bridge, "_run_continue", continue_program,
        ):
            result = continuation._run_continue_with_cost_tagging()

        self.assertEqual(result, 75)
        self.assertEqual(calls, ["install", "continue"])


if __name__ == "__main__":
    unittest.main()
