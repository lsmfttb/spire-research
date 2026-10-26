"""Preparation-package, activation gate, ledger, and watchdog tests; no simulator is run."""
from __future__ import annotations
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

STUDY = Path(__file__).resolve().parents[1]
REPO = STUDY.parents[1]
sys.path.insert(0, str(STUDY))

import model
import prepare
import stage1_budget as budget


PACKAGE_COMMIT, LOCK_DIGEST = budget._current_package_binding()
CHECKPOINT_DIGEST = "c" * 64


def decision(comment_id: int, marker: str, timestamp: str, *, commit: str = PACKAGE_COMMIT,
             lock: str = LOCK_DIGEST, login: str = budget.OWNER_LOGIN):
    return {
        "id": comment_id,
        "body": f"{marker}\npackage_commit: {commit}\nsource_lock_sha256: {lock}\nscope: STUDY_ONLY",
        "created_at": timestamp,
        "updated_at": timestamp,
        "html_url": f"https://github.com/lsmfttb/spire-research/issues/75#issuecomment-{comment_id}",
        "user": {"login": login, "type": "User"},
    }


def activation_comments(reviewer=None, planner=None):
    return [
        reviewer or decision(1, budget.REVIEW_MARKER, "2026-01-01T00:00:00Z"),
        planner or decision(2, budget.PLANNER_MARKER, "2026-01-01T00:01:00Z"),
    ]


def open_ledger(directory: Path, comments=None):
    with mock.patch.object(
        budget, "_fetch_authoritative_issue_comments",
        return_value=activation_comments() if comments is None else comments,
    ) as fetch_comments:
        ledger = budget.open_activated_stage1_ledger(directory)
        fetch_comments.assert_called_once_with()
        return ledger


def activation():
    return open_ledger(STUDY / "stage1" / "unused-test-ledger").activation


class SeedTableTests(unittest.TestCase):
    def test_exact_disjoint_seed_population_and_arm_table(self):
        table = prepare.make_seed_table()
        rows = table["rows"]
        self.assertEqual(len(rows), 1032)
        self.assertEqual(table["counts"]["training_owners"], 512)
        self.assertEqual(table["counts"]["heldout_owner_arm_rows"], 96 * 5)
        self.assertEqual(table["counts"]["preflight_owner_arm_rows"], 8 * 5)
        units = [row["unit_id"] for row in rows]
        self.assertEqual(len(set(units)), len(rows))
        owners = {
            phase: {row["owner_seed"] for row in rows if row["phase"] == phase}
            for phase in ("training", "heldout", "preflight")
        }
        self.assertEqual(owners["training"], set(range(7_500_001, 7_500_513)))
        self.assertEqual(owners["heldout"], set(range(7_510_001, 7_510_097)))
        self.assertEqual(owners["preflight"], set(range(7_520_001, 7_520_009)))
        self.assertFalse(owners["training"] & owners["heldout"])
        self.assertFalse(owners["training"] & owners["preflight"])
        self.assertFalse(owners["heldout"] & owners["preflight"])
        for phase in ("heldout", "preflight"):
            for seed in owners[phase]:
                arms = {r["arm"] for r in rows if r["phase"] == phase and r["owner_seed"] == seed}
                self.assertEqual(arms, set(prepare.HELDOUT_ARMS))
        train = [row for row in rows if row["phase"] == "training"]
        for block in range(4):
            self.assertEqual(sum(row["block_index"] == block for row in train), 128)
        for row in rows:
            if row["arm"] != "E":
                self.assertEqual(row["policy_rng_seed"],
                                 prepare.derive_seed("policy", row["owner_seed"], row["arm"]))
            opportunity_count = (
                4 if row["phase"] == "training" else
                7 if row["arm"] in {"R", "J"} else 0
            )
            expected_search_seeds = [
                prepare.derive_seed("search-invocation", row["owner_seed"],
                                    f"{row['arm']}-ROOT-{index}")
                for index in range(opportunity_count)
            ]
            self.assertEqual(row["search_invocation_seeds"], expected_search_seeds)
            for invocation_seed in expected_search_seeds:
                attempt_seeds = [
                    prepare.derive_search_attempt_seed(invocation_seed, attempt)
                    for attempt in range(16)
                ]
                self.assertEqual(len(set(attempt_seeds)), 16)
            if row["phase"] == "preflight":
                self.assertFalse(row["formal"])

    def test_seed_derivation_is_domain_separated_and_stable(self):
        seed = 7_510_001
        self.assertEqual(prepare.derive_seed("policy", seed, "P1"),
                         prepare.derive_seed("policy", seed, "P1"))
        self.assertNotEqual(prepare.derive_seed("policy", seed, "P1"),
                            prepare.derive_seed("search", seed, "P1"))
        self.assertNotEqual(prepare.derive_seed("policy", seed, "P1"),
                            prepare.derive_seed("policy", seed, "J"))
        invocation = prepare.derive_seed("search-invocation", seed, "J-ROOT-0")
        self.assertEqual(prepare.derive_search_attempt_seed(invocation, 15),
                         prepare.derive_search_attempt_seed(invocation, 15))
        with self.assertRaises(ValueError):
            prepare.derive_search_attempt_seed(invocation, 16)


class ActivationTests(unittest.TestCase):
    def test_ordered_exact_source_gates_accept_same_owner_account_roles(self):
        receipt = activation()
        self.assertEqual(receipt.package_commit, PACKAGE_COMMIT)
        self.assertEqual(receipt.source_lock_sha256, LOCK_DIGEST)
        self.assertEqual(receipt.reviewer_comment_id, 1)
        self.assertEqual(receipt.planner_comment_id, 2)

    def assert_activation_rejected(self, comments):
        with tempfile.TemporaryDirectory(dir=REPO / ".task-tmp") as temp:
            ledger_path = Path(temp) / "ledger"
            with mock.patch.object(budget, "_fetch_authoritative_issue_comments", return_value=comments):
                with self.assertRaises(PermissionError):
                    budget.open_activated_stage1_ledger(ledger_path)
            self.assertFalse(ledger_path.exists())

    def test_wrong_order_owner_or_issue_url_rejected(self):
        reviewer = decision(1, budget.REVIEW_MARKER, "2026-01-01T00:01:00Z")
        planner = decision(2, budget.PLANNER_MARKER, "2026-01-01T00:00:00Z")
        self.assert_activation_rejected([reviewer, planner])

        planner = decision(3, budget.PLANNER_MARKER, "2026-01-01T00:02:00Z", login="unrelated")
        self.assert_activation_rejected([reviewer, planner])

        planner = decision(4, budget.PLANNER_MARKER, "2026-01-01T00:02:00Z")
        planner["html_url"] = "https://github.com/lsmfttb/spire-research/issues/74#issuecomment-4"
        self.assert_activation_rejected([reviewer, planner])

    def test_comment_url_and_id_are_required_and_must_match(self):
        reviewer = decision(1, budget.REVIEW_MARKER, "2026-01-01T00:00:00Z")
        planner = decision(2, budget.PLANNER_MARKER, "2026-01-01T00:01:00Z")
        planner.pop("html_url")
        self.assert_activation_rejected([reviewer, planner])

        planner = decision(3, budget.PLANNER_MARKER, "2026-01-01T00:01:00Z")
        planner["html_url"] = "https://github.com/lsmfttb/spire-research/issues/75#issuecomment-999"
        self.assert_activation_rejected([reviewer, planner])

        planner = decision(4, budget.PLANNER_MARKER, "2026-01-01T00:01:00Z")
        planner.pop("id")
        self.assert_activation_rejected([reviewer, planner])

    def test_comments_must_bind_package_sha_and_lock_of_current_checkout(self):
        reviewer = decision(1, budget.REVIEW_MARKER, "2026-01-01T00:00:00Z")
        wrong_package = decision(2, budget.PLANNER_MARKER, "2026-01-01T00:01:00Z", commit="d" * 40)
        self.assert_activation_rejected([reviewer, wrong_package])

        wrong_lock = decision(3, budget.PLANNER_MARKER, "2026-01-01T00:01:00Z", lock="e" * 64)
        self.assert_activation_rejected([reviewer, wrong_lock])

    def test_directly_forged_receipt_cannot_create_ledger_or_write_events(self):
        forged = budget.ActivationReceipt("0" * 40, "0" * 64, 1, 2, "2026-01-01T00:01:00+00:00")
        with tempfile.TemporaryDirectory(dir=REPO / ".task-tmp") as temp:
            ledger_path = Path(temp) / "forged-ledger"
            with self.assertRaises(PermissionError):
                budget.Stage1Ledger(ledger_path, forged)
            self.assertFalse(ledger_path.exists())

    def test_empty_authoritative_issue_response_cannot_open_ledger(self):
        self.assert_activation_rejected([])

    def test_disabled_preparation_config_cannot_start_stage1(self):
        config = prepare.preflight.load_json(STUDY / "stage1" / "config.json")
        self.assertFalse(config["execution"]["formal_training_enabled"])
        self.assertFalse(config["execution"]["formal_evaluation_enabled"])
        self.assertFalse(config["execution"]["nonformal_preflight_enabled"])
        self.assertFalse(config["execution"]["native_sampling_enabled"])
        self.assertTrue(config["execution"]["planner_activation_required"])
        self.assertEqual(config["activation_gate"]["owner_login"], budget.OWNER_LOGIN)
        self.assertFalse(config["activation_gate"]["separate_github_account_required"])
        self.assertEqual(config["population"]["preflight_arm_schedule"], list(prepare.HELDOUT_ARMS))
        lock = prepare.verify_stage1_package()
        self.assertFalse(lock["execution_authorized"])
        with mock.patch.object(budget, "_fetch_authoritative_issue_comments", return_value=[]):
            with self.assertRaises(PermissionError):
                budget.Stage1Ledger(STUDY / "stage1" / "ledger", None)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=REPO / ".task-tmp")
        self.addCleanup(self.temp.cleanup)
        self.ledger = open_ledger(Path(self.temp.name))

    def reserve(self, run_id, unit, cap=32.0, *, role="builder", kind="search_invocation", attempts=16, **kwargs):
        return self.ledger.reserve(run_id=run_id, unit_id=unit, role=role,
                                   reserved_cpu_seconds=cap, unit_kind=kind,
                                   attempt_cap=attempts if kind == "search_invocation" else None, **kwargs)

    def test_resume_requires_checkpoint_and_preserves_usage(self):
        self.reserve("run-1", "TRAIN:7500001", cap=32.0)
        self.ledger.finish(run_id="run-1", measured_cpu_seconds=9.5, status="incomplete",
                           checkpoint_sha256=CHECKPOINT_DIGEST, started_attempts=4)
        with self.assertRaises(budget.CheckpointMismatch):
            self.reserve("run-2", "TRAIN:7500001", previous_run_id="run-1",
                         checkpoint_sha256="d" * 64)
        self.reserve("run-2", "TRAIN:7500001", cap=20.0, previous_run_id="run-1",
                     checkpoint_sha256=CHECKPOINT_DIGEST)
        self.ledger.finish(run_id="run-2", measured_cpu_seconds=7.0, status="succeeded",
                           started_attempts=3, result_sha256="e" * 64)
        with self.assertRaises(budget.DuplicateRun):
            self.reserve("run-3", "TRAIN:7500001")
        row = self.ledger.read_events()
        self.assertEqual(len(row), 4)
        self.assertEqual(sum(e.get("measured_cpu_seconds", 0) for e in row), 16.5)

    def test_unresolved_reservation_is_charged_full_cap_and_not_resumable(self):
        self.reserve("lost-run", "TRAIN:7500001", cap=32)
        with self.assertRaises(budget.ResourceBlocked):
            self.reserve("retry", "TRAIN:7500001", previous_run_id="lost-run",
                         checkpoint_sha256=CHECKPOINT_DIGEST)
        estimate = {"TRAIN:7500002": 170_000.0}
        with self.assertRaises(budget.ResourceBlocked):
            self.ledger.forecast(expected_unit_ids=list(estimate), future_builder_cpu_seconds=estimate)

    def test_search_caps_and_protected_reviewer_reserve(self):
        with self.assertRaises(ValueError):
            self.reserve("too-large", "TRAIN:1", cap=32.01)
        self.ledger.reserve(run_id="review", unit_id="review", role="reviewer",
                            reserved_cpu_seconds=10_000, unit_kind="review")
        with self.assertRaises(budget.ResourceBlocked):
            self.reserve("builder-over", "TRAIN:1", cap=170_001, kind="training_owner",
                         attempts=None)

    def test_incomplete_and_failed_units_are_not_silently_omitted_from_forecast(self):
        self.reserve("partial", "TRAIN:7500001", cap=20, kind="training_owner", attempts=None)
        self.ledger.finish(run_id="partial", measured_cpu_seconds=12, status="incomplete",
                           checkpoint_sha256=CHECKPOINT_DIGEST)
        estimates = {"TRAIN:7500001": 100, "TRAIN:7500002": 200}
        forecast = self.ledger.forecast(expected_unit_ids=list(estimates),
                                        future_builder_cpu_seconds=estimates)
        self.assertEqual(forecast["future_builder_cpu_forecast"], 300)
        self.reserve("bad", "TRAIN:7500003", cap=20, kind="training_owner", attempts=None)
        self.ledger.finish(run_id="bad", measured_cpu_seconds=19, status="failed")
        with self.assertRaises(budget.ResourceBlocked):
            self.ledger.forecast(expected_unit_ids=list(estimates),
                                 future_builder_cpu_seconds=estimates)
        with self.assertRaises(ValueError):
            self.ledger.forecast(expected_unit_ids=list(estimates),
                                 future_builder_cpu_seconds={"TRAIN:7500001": 100})

    def test_overrun_is_durably_charged_before_exception(self):
        self.reserve("overrun", "SEARCH:1", cap=1.0, attempts=1)
        with self.assertRaises(budget.BudgetOverrun):
            self.ledger.finish(run_id="overrun", measured_cpu_seconds=1.2, status="failed",
                               started_attempts=2)
        events = self.ledger.read_events()
        self.assertEqual(events[-1]["measured_cpu_seconds"], 1.2)


class WatchdogTests(unittest.TestCase):
    def watchdog(self):
        return budget.ProcessTreeWatchdog(poll_seconds=0.005, terminate_grace=0.02,
                                          safety_margin=0.10)

    def test_counts_reaped_grandchild_cpu_and_keeps_task_output_inside_worktree(self):
        child = "import time; x=time.process_time(); exec('while time.process_time()-x < 0.12: pass')"
        parent = "import subprocess,sys; subprocess.run([sys.executable,'-c',sys.argv[1]],check=True)"
        output = REPO / ".task-tmp" / f"watchdog-{os.getpid()}.log"
        output.unlink(missing_ok=True)
        result = self.watchdog().run([sys.executable, "-c", parent, child],
                                     reserved_cpu_seconds=2, cwd=STUDY, stdout_path=output)
        self.assertEqual(result.returncode, 0)
        self.assertGreaterEqual(result.cpu_seconds, 0.08)
        self.assertTrue(output.is_file())
        with self.assertRaises(ValueError):
            self.watchdog().run([sys.executable, "-c", "pass"], reserved_cpu_seconds=2,
                                cwd=STUDY, stdout_path=Path("/outside-issue75-watchdog-must-not-create.log"))

    def test_watchdog_accounts_for_a_worker_adopted_after_its_parent_exits(self):
        code = "import os,time; pid=os.fork(); (os._exit(0) if pid else None); x=time.process_time(); exec('while time.process_time()-x < 0.12: pass'); os._exit(0)"
        result = self.watchdog().run([sys.executable, "-c", code],
                                     reserved_cpu_seconds=2, cwd=STUDY)
        self.assertEqual(result.returncode, 0)
        self.assertGreaterEqual(result.cpu_seconds, 0.08)

    def test_watchdog_stops_busy_process_tree_before_reserved_cpu_cap(self):
        code = "import time; x=time.process_time(); exec('while time.process_time()-x < 5: pass')"
        with self.assertRaises(budget.ProcessTreeBudgetExceeded) as caught:
            self.watchdog().run([sys.executable, "-c", code], reserved_cpu_seconds=0.50, cwd=STUDY)
        self.assertLess(caught.exception.cpu_seconds, caught.exception.cap_seconds)
        self.assertLess(caught.exception.cpu_seconds, 0.50)


class SourceLockTests(unittest.TestCase):
    def test_stage0_and_stage1_source_locks_bind_reviewed_receipts_and_runtime(self):
        config = prepare.preflight.load_json(STUDY / "config.json")
        stage0 = prepare._verify_stage0_lock_and_evidence(config)
        self.assertEqual(stage0["source_commit"], prepare.STAGE0_SOURCE_COMMIT)
        lock = prepare.verify_stage1_package()
        self.assertEqual(lock["stage0"]["source_lock_sha256_lf"], prepare.STAGE0_SOURCE_LOCK_SHA256)
        self.assertIn("stage1_budget.py", lock["locked_files_sha256_lf"])
        self.assertIn("stage1/test_activation.py", lock["locked_files_sha256_lf"])
        self.assertEqual(lock["formal_seeds_or_games_consumed"], 0)

    def test_initial_model_and_empty_optimizer_are_reproducible(self):
        config = prepare.preflight.load_json(STUDY / "config.json")
        policy = config["policy"]
        first = model.ActionConditionedPolicy(
            seed=policy["initial_model_seed"], final_weight_std=policy["actor_final_weight_std"])
        second = model.ActionConditionedPolicy(
            seed=policy["initial_model_seed"], final_weight_std=policy["actor_final_weight_std"])
        self.assertEqual(model.model_digest(first), model.model_digest(second))
        descriptor = prepare._optimizer_descriptor(first)
        self.assertTrue(descriptor["state_is_empty_before_first_step"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
