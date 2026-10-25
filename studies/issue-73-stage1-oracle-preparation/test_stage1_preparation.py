from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import protocol
import runner


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


class Stage1PreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = protocol.read_config()

    def test_package_is_locked_and_disabled(self) -> None:
        status = runner.verify_package()
        self.assertEqual(status["status"], "PREPARATION_PACKAGE_LOCKS_PASS")
        self.assertIs(status["stage1_authorized"], False)
        self.assertEqual(status["formal_owner_seeds_consumed"], 0)
        self.assertEqual(status["owner_count"], 96)
        boundary = self.config["information_boundary"]
        self.assertFalse(boundary["normal_public_api_safe"])
        self.assertFalse(boundary["public_deployment"])
        self.assertFalse(boundary["student_training"])
        self.assertFalse(boundary["persist_raw_privileged_projection_or_native_state"])

    def test_run_cli_refuses_before_creating_output_or_consuming_seed(self) -> None:
        output = HERE / "artifacts" / "disabled-cli-test"
        if output.exists():
            self.fail("test output already exists; refusing to overwrite workspace state")
        status = runner.main(["--run", "--output", str(output), "--native-module", "unused",
                              "--native-root", "unused", "--stsr-root", "unused"])
        self.assertEqual(status, 2)
        self.assertFalse(output.exists())

    def test_fixed_seeds_are_distinct_from_stage0_and_issue71(self) -> None:
        seeds = protocol.owner_seeds(self.config)
        self.assertEqual(len(seeds), 96)
        self.assertEqual(len(set(seeds)), 96)
        self.assertFalse(set(seeds) & set(range(173730001, 173730009)))
        self.assertFalse(set(seeds) & set(range(7410001, 7410513)))
        self.assertFalse(set(seeds) & set(range(7420001, 7420097)))
        self.assertNotEqual(protocol.continuation_seed(seeds[0], 0, 0, 0, stream="native"),
                            protocol.continuation_seed(seeds[0], 0, 0, 0, stream="suffix"))

    def test_wrong_source_commit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO) as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "locked.txt").write_text("expected source\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "locked.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Builder Test",
                            "-c", "user.email=builder-test@example.invalid", "commit", "-qm", "fixture"], check=True)
            head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
            digest = protocol.sha256_file(root / "locked.txt")
            self.assertEqual(protocol.verify_checkout(root, commit=head,
                                                       source_files={"locked.txt": digest})["commit"], head)
            with self.assertRaises(protocol.ContractError):
                protocol.verify_checkout(root, commit="0" * 40, source_files={"locked.txt": digest})

    def test_missing_arm_cannot_enter_primary_analysis(self) -> None:
        owner = {"O": {"terminal_complete": True, "terminal": {"terminal_floor": 1}},
                 "C": {"terminal_complete": True, "terminal": {"terminal_floor": 1}}}
        with self.assertRaises(protocol.ContractError):
            protocol.validate_owner_block(owner)

    def test_no_candidate_fails_closed(self) -> None:
        with self.assertRaises(protocol.ContractError):
            protocol.evaluate_candidate_grid(0, owner_seed=1, decision_index=0, fallback_index=0,
                sampler=lambda *_: {}, expected_native_commit="native", query_index=0)

    def test_occurrence_mapping_rejects_illegal_or_reordered_action(self) -> None:
        with self.assertRaises(protocol.ContractError):
            protocol.validate_live_occurrences(["a", "b"], ["b", "a"], 0)
        with self.assertRaises(protocol.ContractError):
            protocol.validate_live_occurrences(["a", "b"], ["a", "b"], 2)

    def test_two_se_abstention_uses_exact_c_fallback(self) -> None:
        choice = protocol.choose_or_abstain([[20] * 8, [20] * 8], fallback_index=1)
        self.assertTrue(choice.fallback)
        self.assertEqual(choice.chosen_index, 1)
        self.assertEqual(choice.reason, "tied_best_mean")

    def test_persisted_oracle_receipt_is_allowlisted_and_contains_no_private_payload(self) -> None:
        choice = protocol.choose_or_abstain([[20] * 8, [20] * 8], fallback_index=1)
        fingerprints = {"native_state_sha256": "a" * 64, "public_projection_sha256": "b" * 64}
        receipt = protocol.oracle_decision_receipt(
            step=1, query_index=0, candidate_count=2, fallback_index=1, selected_index=1,
            screen="MAP", choice=choice, fingerprints=fingerprints,
            legal_ids=[{"opaque_action_identity": "private fixture"}], attempt_count=16)
        serialized = json.dumps(receipt, sort_keys=True)
        self.assertNotIn("private fixture", serialized)
        self.assertEqual(set(receipt), {
            "schema_id", "step", "query_index", "screen", "candidate_count",
            "fallback_index", "selected_index", "fallback", "reason", "means",
            "sample_variances", "two_se_thresholds", "root_native_state_sha256",
            "root_public_state_sha256", "legal_occurrences_sha256", "continuation_count"})

    def test_arm_receipt_strips_native_action_and_projection_trace_payloads(self) -> None:
        result = protocol.safe_arm_receipt({
            "run_id": "owner-C", "seed": 173731001, "terminal_complete": True,
            "terminal": {"outcome": "PLAYER_LOSS", "terminal_floor": 1,
                         "terminal_act": 1, "heart_victory": False,
                         "lexicographic_fitness": [0, 1], "raw_native_state": "SECRET"},
            "completed_transitions": 4, "requested_steps": 4,
            "cpu_seconds": 2.0, "wall_seconds": 3.0,
            "native_source_commit": "pinned", "native_module_sha256": "a" * 64,
            "screen_counts": {"MAP": 1}, "action_kind_counts": {"CHOOSE": 1},
            "exposures": {"hidden_flag": True},
            "decisions": [
                {"kind": "categorical", "step": 1, "native_screen": "MAP",
                 "chosen_index": 0, "legal_action_identities": ["SECRET_ACTION"],
                 "projection_audit": {"private_projection": "SECRET"}},
                {"kind": "single_action", "step": 3, "screen": "REWARDS"}],
        }, registered_arm="C", policy_mechanics_arm="SUFFIX")
        payload = json.dumps(result, sort_keys=True)
        for secret in ("SECRET", "SECRET_ACTION", "projection_audit", "legal_action_identities", "raw_native_state"):
            self.assertNotIn(secret, payload)
        self.assertEqual([row["timing"] for row in result["decision_summary"]], ["early", "later"])

    def test_proposed_formal_budget_equals_exact_registered_worst_case_not_authorized_now(self) -> None:
        budget = self.config["resource_proposal_not_authorization"]
        branch_owner = 6 * 14 * 8 * 20
        total = 96 * (branch_owner + 120 + 120 + 120)
        self.assertEqual(branch_owner, budget["proposed_max_o_branch_cpu_seconds_per_owner"])
        self.assertEqual(total, budget["proposed_absolute_stage1_cpu_cap_seconds"])
        self.assertFalse(budget["formal_cpu_authorized"])
        self.assertFalse(budget["formal_cpu_reserved"])

    def test_decisive_candidate_requires_complete_fixed_eight_samples(self) -> None:
        choice = protocol.choose_or_abstain([[40] * 8, [5] * 8], fallback_index=1)
        self.assertFalse(choice.fallback)
        self.assertEqual(choice.chosen_index, 0)
        with self.assertRaises(protocol.ContractError):
            protocol.choose_or_abstain([[40] * 7, [5] * 8], fallback_index=1)

    def test_wrong_source_nonterminal_and_budget_exceeding_suffixes_fail(self) -> None:
        def good(candidate: int, rep: int, seed: int, cap: float) -> dict[str, object]:
            return {"terminal_complete": True, "terminal": {"terminal_floor": candidate + 2},
                    "native_source_commit": "native", "cpu_seconds": 1.0,
                    "replicate": rep, "seed": seed, "requested_cap": cap}

        result, attempts = protocol.evaluate_candidate_grid(2, owner_seed=17, decision_index=0,
            fallback_index=0, sampler=good, expected_native_commit="native", query_index=0)
        self.assertEqual(len(attempts), 16)
        self.assertFalse(result.fallback)

        for field, value in (("native_source_commit", "wrong"),
                             ("terminal_complete", False), ("cpu_seconds", 20.01)):
            def invalid(candidate: int, rep: int, seed: int, cap: float,
                        f: str = field, v: object = value) -> dict[str, object]:
                row = good(candidate, rep, seed, cap)
                row[f] = v
                return row
            with self.subTest(field=field), self.assertRaises(protocol.ContractError):
                protocol.evaluate_candidate_grid(1, owner_seed=17, decision_index=0,
                    fallback_index=0, sampler=invalid, expected_native_commit="native", query_index=0)

    def test_query_budget_abstains_and_absolute_candidate_cap_is_enforced(self) -> None:
        result, attempts = protocol.evaluate_candidate_grid(2, owner_seed=1, decision_index=1,
            fallback_index=1, sampler=lambda *_: self.fail("budget-exhausted query must not start"),
            expected_native_commit="native", query_index=6)
        self.assertTrue(result.fallback)
        self.assertEqual(result.reason, "query_budget_exhausted")
        self.assertEqual(attempts, [])
        result, attempts = protocol.evaluate_candidate_grid(15, owner_seed=1, decision_index=1,
            fallback_index=14, sampler=lambda *_: self.fail("oversized query must not start"),
            expected_native_commit="native", query_index=0)
        self.assertTrue(result.fallback)
        self.assertEqual(result.chosen_index, 14)
        self.assertEqual(attempts, [])

    def test_primary_analysis_requires_96_complete_owner_blocks(self) -> None:
        with self.assertRaises(protocol.ContractError):
            protocol.paired_differences([], arm="C")

    def test_96_owner_precision_is_reported_as_planning_only(self) -> None:
        result = protocol.paired_precision()
        self.assertAlmostEqual(result["95pct_half_width_in_paired_sd"], 0.20004, places=5)
        self.assertAlmostEqual(result["80pct_mde_in_paired_sd"], 0.286, places=3)

    def test_owner_bootstrap_is_fixed_seed_and_keeps_owner_unit(self) -> None:
        values = [float((index % 11) - 5) for index in range(96)]
        first = protocol.bootstrap_percentile(values)
        second = protocol.bootstrap_percentile(values)
        self.assertEqual(first, second)
        self.assertEqual(first["n_owners"], 96)
        self.assertEqual(first["resamples"], 20000)
        self.assertEqual(first["unit"], "owner block")

    def test_receipt_writer_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO) as temporary:
            path = Path(temporary) / "receipt.json"
            protocol.atomic_json(path, {"status": "FIRST"})
            with self.assertRaises(protocol.ContractError):
                protocol.atomic_json(path, {"status": "SECOND"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "FIRST")


if __name__ == "__main__":
    unittest.main()
