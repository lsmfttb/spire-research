"""Focused public-input fixtures for Issue 63's map/reward context."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "issue63_renderer", Path(__file__).with_name("render_issue63.py"))
assert SPEC is not None and SPEC.loader is not None
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


class Issue63ContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        selected, records = renderer.build()
        cls.records = {case_id: record for (case_id, _), record in zip(selected, records)}

    def test_m03_m04_each_legal_route_has_its_own_public_downstream(self) -> None:
        for case_id in ("M03", "M04"):
            with self.subTest(case=case_id):
                previews = renderer.route_previews(self.records[case_id])
                self.assertEqual(len(previews), 3)
                self.assertTrue(all(len(item["levels"]) >= 3 for item in previews))
                first_downstream = [
                    tuple(row["node"] for row in item["levels"][1])
                    for item in previews
                ]
                self.assertEqual(len(set(first_downstream)), 3)
                self.assertTrue(any(
                    len(row["successors"]) > 1
                    for item in previews
                    for level in item["levels"][:-1]
                    for row in level
                ))

    def test_r03_exposes_option_categories_without_inventing_claim_groups(self) -> None:
        lines = renderer.reward_context_lines(self.records["R03"])
        text = "\n".join(lines)
        self.assertIn("未记录奖励组 ID、组成员、共同领取或互斥字段", text)
        self.assertIn("`gold`：合法选项 #1", text)
        self.assertIn("`card`：合法选项 #2, #3, #4", text)
        self.assertIn("`skip`：合法选项 #5", text)
        self.assertIn("共同领取/互斥关系：未记录/不可见", text)
        self.assertIn("仅为语义类别，不代表实际领取分组或互斥规则", text)


if __name__ == "__main__":
    unittest.main()
