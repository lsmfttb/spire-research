from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import algorithm
import preflight


STUDY = Path(__file__).resolve().parent


def _decision(*, step: int, is_root: bool, chosen: int = 0, later: bool = False) -> algorithm.Decision:
    legal = ({"card": "a"}, {"card": "b"}, {"card": "c"})
    return algorithm.Decision(
        step_index=step,
        state_fingerprint=f"synthetic-test-state-{step}",
        legal_identities=legal,
        chosen_index=chosen,
        checkpoint=(object() if later else None),
        is_root=is_root,
        screen="synthetic-test-only",
    )


def _terminal_trial(
    attempt: int,
    *,
    root_index: int = 0,
    heart: bool = False,
    floor: float = 0.0,
    has_later_choice: bool = True,
    later_index: int = 0,
) -> algorithm.TrialResult:
    root = _decision(step=0, is_root=True, chosen=root_index)
    later = _decision(step=4, is_root=False, chosen=later_index, later=True) if has_later_choice else None
    return algorithm.TrialResult(
        attempt_index=attempt,
        complete=True,
        heart_win=heart,
        terminal_floor=floor,
        root_identity=dict(root.chosen_identity),
        decisions=(root,) if later is None else (root, later),
    )


class Issue75ContractTests(unittest.TestCase):
    def test_source_locked_stage0_and_search_caps(self) -> None:
        config = json.loads((STUDY / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(config["authorization"]["stage0_nonformal_authorized"])
        self.assertFalse(config["authorization"]["formal_training_enabled"])
        self.assertFalse(config["authorization"]["formal_evaluation_enabled"])
        self.assertFalse(config["authorization"]["formal_owner_seed_table_present"])
        self.assertEqual(
            config["authorization"]["reviewer_gate"]["required_identity_mode"],
            "configured_owner_account_independent_reviewer_role",
        )
        self.assertEqual(len(config["stage0"]["root_ids"]), 8)
        self.assertEqual(config["search"]["search_invocation_cpu_cap_seconds"], algorithm.SEARCH_CPU_CAP_SECONDS)
        self.assertEqual(config["search"]["search_invocation_started_attempt_cap"], algorithm.SEARCH_STARTED_ATTEMPT_CAP)
        self.assertEqual(config["search"]["r_root_trial_cap"], algorithm.R_ROOT_TRIAL_CAP)
        self.assertEqual(config["search"]["j_root_trial_cap"], algorithm.J_ROOT_TRIAL_CAP)
        self.assertEqual(config["search"]["j_later_mutation_trial_cap"], algorithm.J_LATER_MUTATION_TRIAL_CAP)
        self.assertEqual(config["search"]["j_completed_trajectory_portfolio_cap"], algorithm.J_COMPLETED_PORTFOLIO_CAP)
        resources = config["resources"]
        self.assertLessEqual(resources["stage0_root_worker_worst_case_cpu_seconds"], resources["native_root_checkpoint_terminal_cpu_hard_seconds"])
        self.assertLessEqual(
            resources["stage0_root_worker_worst_case_cpu_seconds"]
            + resources["stage0_source_preflight_conservative_cpu_reserve_seconds"],
            resources["builder_process_tree_cpu_hard_seconds"],
        )

    def test_root_scheduler_is_seed_locked_and_cyclic(self) -> None:
        args = dict(study_id="issue-75-no-expert-oracle", root_id="root-test", invocation_seed=3)
        first = algorithm.root_action_schedule(3, trial_count=16, **args)
        second = algorithm.root_action_schedule(3, trial_count=16, **args)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)
        self.assertEqual(sorted(first[:3]), [0, 1, 2])
        self.assertEqual(max(first.count(index) for index in range(3)) - min(first.count(index) for index in range(3)), 1)

    def test_joint_search_charges_all_attempts_and_bounds_portfolio(self) -> None:
        ticks = [0.0]
        started: list[tuple[int, int, algorithm.MutationTarget | None]] = []
        portfolio_sizes: list[int] = []
        mutation_rows: list[tuple[algorithm.TrialResult, algorithm.MutationTarget]] = []

        def read_cpu() -> float:
            return ticks[0]

        def run(attempt: int, root_index: int, target: algorithm.MutationTarget | None) -> algorithm.TrialResult:
            ticks[0] += 0.01
            if target is None:
                return _terminal_trial(attempt, root_index=root_index, floor=float(attempt))
            trial = _terminal_trial(
                attempt, root_index=0, floor=float(attempt), has_later_choice=True,
                later_index=target.alternative_index,
            )
            mutation_rows.append((trial, target))
            return trial

        result = algorithm.run_search(
            arm="J", study_id="issue-75-test", root_id="root-test", invocation_seed=7,
            root_legal_identities=({"card": "a"}, {"card": "b"}, {"card": "c"}),
            attempt_runner=run,
            budget=algorithm.InvocationBudget(cpu_cap_seconds=32, started_attempt_cap=16, cpu_reader=read_cpu),
            attempt_started_recorder=lambda index, root, target: started.append((index, root, target)),
            completed_portfolio_recorder=lambda rows: portfolio_sizes.append(len(rows)),
        )
        self.assertEqual(result.started_attempts, 16)
        self.assertEqual(result.mutation_attempts_started, 8)
        self.assertEqual(sum(target is not None for _, _, target in started), 8)
        self.assertTrue(portfolio_sizes)
        self.assertLessEqual(max(portfolio_sizes), algorithm.J_COMPLETED_PORTFOLIO_CAP)
        self.assertEqual(result.status, "MUTATION_ATTEMPTED")
        self.assertTrue(mutation_rows)
        self.assertTrue(all(
            row.decisions[1].chosen_index == target.alternative_index
            and row.decisions[1].chosen_index != target.parent_decision.chosen_index
            for row, target in mutation_rows
        ))

    def test_joint_search_without_reached_later_choice_is_not_renamed_as_treatment(self) -> None:
        ticks = [0.0]

        def read_cpu() -> float:
            return ticks[0]

        def run(attempt: int, root_index: int, target: algorithm.MutationTarget | None) -> algorithm.TrialResult:
            ticks[0] += 0.01
            self.assertIsNone(target)
            return _terminal_trial(attempt, root_index=root_index, has_later_choice=False)

        result = algorithm.run_search(
            arm="J", study_id="issue-75-test", root_id="root-no-later", invocation_seed=1,
            root_legal_identities=({"card": "a"}, {"card": "b"}),
            attempt_runner=run,
            budget=algorithm.InvocationBudget(cpu_cap_seconds=32, started_attempt_cap=16, cpu_reader=read_cpu),
        )
        self.assertEqual(result.started_attempts, algorithm.J_ROOT_TRIAL_CAP)
        self.assertEqual(result.mutation_attempts_started, 0)
        self.assertEqual(result.status, "TREATMENT_NOT_DELIVERED")

    def test_elite_requires_strict_lexicographic_improvement(self) -> None:
        base = _terminal_trial(1, heart=False, floor=100.0)
        tied = _terminal_trial(2, heart=False, floor=100.0)
        higher_floor = _terminal_trial(3, heart=False, floor=101.0)
        heart_win = _terminal_trial(4, heart=True, floor=-500.0)
        self.assertIsNone(algorithm.elite_candidate(base, [tied]))
        self.assertEqual(algorithm.elite_candidate(base, [higher_floor, heart_win]).attempt_index, 4)

    def test_native_negative_source_only_inspects_api_without_constructing_simulator(self) -> None:
        source = (STUDY / "native_negative.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.assertFalse(any(
            isinstance(node.func, ast.Attribute) and node.func.attr == "StepSimulator"
            for node in calls
        ))
        self.assertNotIn("PrivilegedNoncombatProjectionAccess()", source)
        self.assertIn("import_only", source)
        receipt = json.loads((STUDY / "native-negative-test.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "PINNED_NATIVE_IMPORT_API_NEGATIVE_PASS")
        self.assertEqual(receipt["python_version"], "3.13.13")
        self.assertEqual(receipt["numpy_version"], "2.4.6")
        self.assertEqual(receipt["torch_version"], "2.12.0+cpu")
        self.assertEqual(receipt["simulator_instances_created"], 0)
        self.assertEqual(receipt["games_started"], 0)
        self.assertEqual(receipt["checkpoints_loaded_or_created"], 0)
        self.assertEqual(receipt["privileged_projections_read"], 0)
        self.assertEqual(receipt["formal_seeds_consumed"], 0)
        for path in ("algorithm.py", "model.py", "learning.py", "runtime.py", "stage0.py"):
            code = (STUDY / path).read_text(encoding="utf-8")
            self.assertNotIn("ExpertNonCombatDriver", code)

    def test_root_worker_checks_reviewer_and_parent_receipts_before_native_setup(self) -> None:
        tree = ast.parse((STUDY / "stage0.py").read_text(encoding="utf-8"))
        worker = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_root_worker"
        )
        call_lines: dict[str, list[int]] = {}
        for node in ast.walk(worker):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                call_lines.setdefault(node.func.id, []).append(node.lineno)
        self.assertLess(min(call_lines["reviewer_pass_proof"]), min(call_lines["initialize_native_runtime"]))
        source = ast.get_source_segment((STUDY / "stage0.py").read_text(encoding="utf-8"), worker)
        self.assertLess(source.index("stage0-start.json"), source.index("initialize_native_runtime"))
        self.assertLess(source.index("parent-created root start receipt"), source.index("initialize_native_runtime"))
        self.assertIn('stage_start.get("reviewer_authentication")', source)
        self.assertIn('root_start.get("reviewer_authentication")', source)

    def test_stage0_receipts_preserve_reviewer_authentication_provenance(self) -> None:
        source = (STUDY / "stage0.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(
            source.count('"reviewer_authentication": proof["reviewer_authentication"]'), 4
        )

    @staticmethod
    def _reviewer_pass_comment(
        login: str, app: dict[str, object] | None = None
    ) -> dict[str, object]:
        comment: dict[str, object] = {
            "id": 812345,
            "user": {"login": login, "type": "User"},
            "body": "\n".join((
                "ISSUE75 REVIEW PASS",
                f"source_commit: {'a' * 40}",
                f"source_lock_sha256_lf: {'b' * 64}",
            )),
        }
        if app is not None:
            comment["performed_via_github_app"] = app
        return comment

    @staticmethod
    def _run_reviewer_pass(
        comment: dict[str, object], current_login: str = "lsmfttb"
    ) -> dict[str, object]:
        with (
            patch.dict(os.environ, {"GH_TOKEN": "unit-test-token"}),
            patch.object(preflight, "verify_source_lock", return_value={
                "formal_training_enabled": False,
                "formal_evaluation_enabled": False,
            }),
            patch.object(preflight, "git", return_value="a" * 40),
            patch.object(preflight, "normalized_sha256", return_value="b" * 64),
            patch.object(preflight, "_github_get_json", side_effect=(
                ({"login": current_login}, None),
                ([comment], None),
            )),
        ):
            return preflight.reviewer_pass_proof()

    def test_reviewer_gate_accepts_owner_pass_for_independent_reviewer_role(self) -> None:
        app_markers = (
            {
                "id": 1144995,
                "slug": "chatgpt-codex-connector",
                "owner": {"login": "openai"},
            },
            {"id": 1, "slug": "untrusted-app", "owner": {"login": "example"}},
            None,
        )
        for app in app_markers:
            with self.subTest(app=app):
                proof = self._run_reviewer_pass(self._reviewer_pass_comment("lsmfttb", app))
                self.assertEqual(proof["reviewer_login"], "lsmfttb")
                self.assertEqual(
                    proof["reviewer_authentication"]["identity_mode"],
                    "configured_owner_account_independent_reviewer_role",
                )

    def test_reviewer_gate_rejects_comment_from_different_github_user(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "configured project account"):
            self._run_reviewer_pass(self._reviewer_pass_comment("other-user"))

    def test_reviewer_gate_rejects_token_not_bound_to_configured_owner(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "configured project account"):
            self._run_reviewer_pass(
                self._reviewer_pass_comment("lsmfttb"),
                current_login="other-user",
            )

    def test_reviewer_gate_rejects_stale_lock_from_configured_account(self) -> None:
        comment = self._reviewer_pass_comment("lsmfttb")
        comment["body"] = str(comment["body"]).replace("b" * 64, "c" * 64)
        with self.assertRaisesRegex(RuntimeError, "no source-bound GitHub Reviewer PASS"):
            self._run_reviewer_pass(comment)

    def test_reviewer_gate_rejects_automation_accounts(self) -> None:
        comment = self._reviewer_pass_comment("review-bot")
        comment["user"] = {"login": "review-bot", "type": "Bot"}
        with self.assertRaisesRegex(RuntimeError, "configured project account"):
            self._run_reviewer_pass(comment)


if __name__ == "__main__":
    unittest.main()
