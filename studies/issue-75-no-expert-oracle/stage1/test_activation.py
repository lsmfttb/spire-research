"""Stage 1 package, source binding, ledger, and watchdog tests; no simulator is run."""
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
import executor


PACKAGE_COMMIT, LOCK_DIGEST = chr(97) * 40, chr(98) * 64
CHECKPOINT_DIGEST = "c" * 64


def package_binding():
    return budget.PackageBinding(PACKAGE_COMMIT, LOCK_DIGEST)


def open_ledger(directory: Path):
    with mock.patch.object(
        budget, "_current_package_binding",
        return_value=(PACKAGE_COMMIT, LOCK_DIGEST),
    ):
        return budget.open_stage1_ledger(directory)


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


class PackageBindingTests(unittest.TestCase):
    def test_accepted_prospective_package_enables_only_registered_phases(self):
        config = prepare.preflight.load_json(STUDY / "stage1" / "config.json")
        execution = config["execution"]
        self.assertTrue(execution["stage1_execution_authorized"])
        self.assertTrue(execution["nonformal_preflight_enabled"])
        self.assertTrue(execution["native_sampling_enabled"])
        self.assertTrue(execution["formal_training_enabled"])
        self.assertTrue(execution["formal_evaluation_enabled"])
        self.assertEqual(execution["authorized_phases"], ["preflight", "train", "evaluate"])
        self.assertEqual(execution["max_concurrent_owner_process_trees"], 1)
        self.assertEqual(execution["torch_intraop_threads"], 1)
        self.assertEqual(execution["torch_interop_threads"], 1)
        self.assertNotIn("activation_gate", config)
        lock = prepare.verify_stage1_package()
        self.assertEqual(lock["package_status"], "FORMAL_EXECUTION_ENABLED")
        self.assertEqual(lock["final_package_commit_binding"], "all execution receipts bind the exact package commit and source-lock digest")
        for phase in ("preflight", "train", "evaluate", "update"):
            budget.verify_stage1_phase(phase)
        self.assertEqual(executor._formal_concurrency(), 1)

    def test_worker_ledger_uses_current_package_binding_without_comment_receipt(self):
        with tempfile.TemporaryDirectory(dir=STUDY) as temp:
            ledger = open_ledger(Path(temp) / "ledger")
            binding = ledger.binding
            unit_id = "PREFLIGHT:7520001:P0"
            run_id = unit_id + "-" + "a" * 16
            ledger.reserve(
                run_id=run_id, unit_id=unit_id, role="builder",
                reserved_cpu_seconds=1000.0, unit_kind="owner_arm",
            )
            with mock.patch.object(
                budget, "_current_package_binding",
                return_value=(binding.package_commit, binding.source_lock_sha256),
            ):
                worker = budget.open_reserved_stage1_worker_ledger(
                    ledger.directory, run_id=run_id, unit_id=unit_id,
                )
            self.assertEqual(worker.binding, binding)
            reservation = worker.require_open_reservation(run_id=run_id, unit_id=unit_id)
            self.assertEqual(reservation["package_commit"], binding.package_commit)
            self.assertEqual(reservation["source_lock_sha256"], binding.source_lock_sha256)
            self.assertNotIn("activation_receipt", reservation)

    def test_reserved_worker_rejects_a_different_package_binding(self):
        with tempfile.TemporaryDirectory(dir=STUDY) as temp:
            ledger = open_ledger(Path(temp) / "ledger")
            unit_id = "PREFLIGHT:7520001:P0"
            run_id = unit_id + "-" + "b" * 16
            ledger.reserve(
                run_id=run_id, unit_id=unit_id, role="builder",
                reserved_cpu_seconds=1000.0, unit_kind="owner_arm",
            )
            with mock.patch.object(
                budget, "_current_package_binding",
                return_value=("0" * 40, "1" * 64),
            ):
                with self.assertRaises(PermissionError):
                    budget.open_reserved_stage1_worker_ledger(
                        ledger.directory, run_id=run_id, unit_id=unit_id,
                    )

    def test_dirty_tracked_checkout_cannot_open_the_ledger(self):
        dirty = budget.subprocess.CompletedProcess(args=["git"], returncode=1)
        with (
            mock.patch.object(budget.subprocess, "run", return_value=dirty),
            mock.patch.object(budget, "verify_stage1_package"),
        ):
            with tempfile.TemporaryDirectory(dir=STUDY) as temp:
                ledger_path = Path(temp) / "ledger"
                with self.assertRaises(PermissionError):
                    budget.Stage1Ledger(ledger_path)
                self.assertFalse(ledger_path.exists())


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=STUDY)
        self.addCleanup(self.temp.cleanup)
        self.ledger = open_ledger(Path(self.temp.name))

    def reserve(self, run_id, unit, cap=32.0, *, role="builder", kind="search_invocation", attempts=16, **kwargs):
        return self.ledger.reserve(run_id=run_id, unit_id=unit, role=role,
                                   reserved_cpu_seconds=cap, unit_kind=kind,
                                   attempt_cap=attempts if kind == "search_invocation" else None, **kwargs)

    def test_worker_ledger_reuses_source_binding_for_an_open_reservation(self):
        unit_id = "PREFLIGHT:7520001:P0"
        run_id = unit_id + "-" + "a" * 16
        binding = self.ledger.binding
        self.ledger.reserve(
            run_id=run_id, unit_id=unit_id, role="builder",
            reserved_cpu_seconds=1000.0, unit_kind="owner_arm",
        )
        with mock.patch.object(
            budget, "_current_package_binding",
            return_value=(binding.package_commit, binding.source_lock_sha256),
        ):
            worker = budget.open_reserved_stage1_worker_ledger(
                self.ledger.directory, run_id=run_id, unit_id=unit_id,
            )
        self.assertEqual(worker.binding, binding)
        self.assertEqual(
            worker.require_open_reservation(run_id=run_id, unit_id=unit_id)["package_commit"],
            binding.package_commit,
        )

    def test_unknown_ledger_schema_is_rejected(self):
        with self.assertRaises(budget.LedgerCorrupt):
            budget._state([{"event": "reserve", "schema_id": "issue75-stage1-budget-event-v1"}])

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
            self.reserve("builder-over", "TRAIN:1", cap=budget.BUILDER_LIMIT + 1, kind="training_owner",
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

    def test_batch_reservations_validate_all_rows_before_appending_any(self):
        first = {
            "run_id": "batch-first", "unit_id": "TRAIN:7500001", "role": "builder",
            "reserved_cpu_seconds": 5.0, "unit_kind": "training_owner",
        }
        with self.assertRaises(budget.DuplicateRun):
            self.ledger.reserve_many([first, {**first, "unit_id": "TRAIN:7500002"}])
        self.assertEqual(self.ledger.read_events(), [])

        second = {**first, "run_id": "batch-second", "unit_id": "TRAIN:7500002"}
        reserved = self.ledger.reserve_many([first, second])
        self.assertEqual([event["run_id"] for event in reserved], ["batch-first", "batch-second"])
        self.assertEqual(
            [event["run_id"] for event in self.ledger.read_events() if event.get("event") == "reserve"],
            ["batch-first", "batch-second"],
        )

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
        with tempfile.TemporaryDirectory(dir=STUDY) as temporary:
            output = Path(temporary) / "watchdog.log"
            result = self.watchdog().run([sys.executable, "-c", parent, child],
                                         reserved_cpu_seconds=2, cwd=STUDY, stdout_path=output)
            self.assertEqual(result.returncode, 0)
            self.assertGreaterEqual(result.cpu_seconds, 0.08)
            self.assertGreaterEqual(result.controller_cpu_seconds, 0.0)
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
    def test_historical_stage0_lock_passes_from_stage1_descendant(self):
        config = prepare.preflight.load_json(STUDY / "config.json")
        stage0 = prepare._verify_stage0_lock_and_evidence(config)
        self.assertEqual(stage0["source_commit"], prepare.STAGE0_SOURCE_COMMIT)
        self.assertEqual(stage0["source_lock_sha256_lf"], prepare.STAGE0_SOURCE_LOCK_SHA256)
        historical_lock = prepare.preflight.load_json(STUDY / "source-lock.json")
        self.assertEqual(prepare._stage0_historical_file_hashes(), historical_lock["files_sha256_lf"])
        lock = prepare.verify_stage1_package()
        self.assertEqual(lock["stage0"]["source_lock_sha256_lf"], prepare.STAGE0_SOURCE_LOCK_SHA256)
        self.assertIn("stage1_budget.py", lock["locked_files_sha256_lf"])
        self.assertIn("stage1/test_activation.py", lock["locked_files_sha256_lf"])
        self.assertEqual(lock["formal_seeds_or_games_consumed"], 0)

    def test_changed_blob_at_pinned_stage0_source_is_rejected(self):
        config = prepare.preflight.load_json(STUDY / "config.json")
        read_blob = prepare._stage0_source_blob

        def changed_blob(source_commit, relative_path):
            blob = read_blob(source_commit, relative_path)
            if relative_path == "algorithm.py":
                return blob + b"\n# changed historical blob\n"
            return blob

        with mock.patch.object(prepare, "_stage0_source_blob", side_effect=changed_blob):
            with self.assertRaisesRegex(ValueError, "Stage 0 lock content differs"):
                prepare._verify_stage0_lock_and_evidence(config)

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
