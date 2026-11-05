from __future__ import annotations

from contextvars import ContextVar
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pilot_runner


class FakeBackend:
    @staticmethod
    def _row(owner, *, phase, arm, formal, block):
        return {
            "unit_id": f"{phase}:{owner}:{arm}",
            "phase": phase,
            "arm": arm,
            "formal": formal,
            "block_index": block,
            "owner_seed": owner,
            "game_seed": owner,
            "policy_rng_seed": 1,
            "search_invocation_seeds": [2, 3, 4, 5] if phase == "training" else [],
        }


class PilotRunnerTests(unittest.TestCase):
    def test_actor_gradient_clip_comes_from_locked_issue84_contract_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            contract_path = Path(directory) / "contract.json"
            driver = SimpleNamespace(CONTRACT_PATH=contract_path)
            contract_path.write_text(
                json.dumps({"unchanged_actor_and_environment": {"actor_gradient_norm_clip": 1.0}}),
                encoding="utf-8",
            )
            self.assertEqual(pilot_runner._issue84_actor_gradient_norm_clip(driver), 1.0)

            invalid_contracts = (
                {},
                {"unchanged_actor_and_environment": {}},
                {"unchanged_actor_and_environment": {"actor_gradient_norm_clip": True}},
                {"unchanged_actor_and_environment": {"actor_gradient_norm_clip": "1.0"}},
                {"unchanged_actor_and_environment": {"actor_gradient_norm_clip": 0}},
                {"unchanged_actor_and_environment": {"actor_gradient_norm_clip": -1}},
                {"unchanged_actor_and_environment": {"actor_gradient_norm_clip": float("nan")}},
                {"unchanged_actor_and_environment": {"actor_gradient_norm_clip": float("inf")}},
            )
            for invalid in invalid_contracts:
                with self.subTest(contract=invalid):
                    contract_path.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        pilot_runner._issue84_actor_gradient_norm_clip(driver)

        with self.assertRaisesRegex(RuntimeError, "lacks its contract path"):
            pilot_runner._issue84_actor_gradient_norm_clip(SimpleNamespace())


    def test_issue84_training_examples_use_the_locked_public_loader(self):
        calls = {}
        examples = [object()]

        def load_training_outcomes(rows, *, root, executor, package):
            calls.update(rows=rows, root=root, executor=executor, package=package)
            return examples, [{"status": "SUCCEEDED"}]

        rows = [{"unit_id": "PILOT-TRAIN:B:8530001"}]
        executor = object()
        loaded = pilot_runner._load_pilot_training_examples(
            SimpleNamespace(load_training_outcomes=load_training_outcomes),
            rows,
            root=Path("predecessor-training"),
            executor=executor,
            package_commit=pilot_runner.PILOT_RESUME_PREDECESSOR_SHA,
        )

        self.assertIs(loaded, examples)
        self.assertEqual(calls, {
            "rows": rows,
            "root": Path("predecessor-training"),
            "executor": executor,
            "package": pilot_runner.PILOT_RESUME_PREDECESSOR_SHA,
        })

    def test_training_examples_can_read_carry_forward_receipts_per_owner(self):
        calls = []

        def load_training_outcomes(rows, *, root, executor, package):
            calls.append((rows, root, executor, package))
            return [rows[0]["unit_id"]], [{"status": "SUCCEEDED"}]

        rows = [
            {"unit_id": "PILOT-TRAIN:C:8530001"},
            {"unit_id": "PILOT-TRAIN:C:8530002"},
        ]
        old_binding = (Path("old-training"), "b" * 40)
        loaded = pilot_runner._load_pilot_training_examples(
            SimpleNamespace(load_training_outcomes=load_training_outcomes),
            rows,
            root=Path("current-training"),
            executor="executor",
            package_commit="c" * 40,
            receipt_roots_by_owner={rows[0]["unit_id"]: old_binding},
        )

        self.assertEqual(loaded, [row["unit_id"] for row in rows])
        self.assertEqual(calls, [
            ([rows[0]], Path("old-training"), "executor", "b" * 40),
            ([rows[1]], Path("current-training"), "executor", "c" * 40),
        ])

    def test_training_example_eligibility_matches_locked_issue84_wrapper(self):
        complete = SimpleNamespace(
            owner_seed=8530001,
            base=SimpleNamespace(complete=True, decisions=[object()]),
        )
        incomplete = SimpleNamespace(
            owner_seed=8530002,
            base=SimpleNamespace(complete=False, decisions=[object()]),
        )
        decision_free = SimpleNamespace(
            owner_seed=8530003,
            base=SimpleNamespace(complete=True, decisions=[]),
        )
        wrong_shape = SimpleNamespace(owner_seed=8530004, complete=True, decisions=[object()])

        self.assertTrue(pilot_runner._pilot_training_example_is_usable(complete))
        self.assertFalse(pilot_runner._pilot_training_example_is_usable(incomplete))
        self.assertFalse(pilot_runner._pilot_training_example_is_usable(decision_free))
        self.assertFalse(pilot_runner._pilot_training_example_is_usable(wrong_shape))

    def test_initial_parity_uses_locked_wrapper_and_widened_hash_helper(self):
        import torch

        class FakePolicy:
            def eval(self):
                return self

            def logits(self, features, payload=None):
                return torch.zeros(features.shape[0], dtype=torch.float32)

        legacy = torch.zeros((2, 2048), dtype=torch.float32)
        wide = torch.zeros((2, 8192), dtype=torch.float32)
        wide[0, 0] = 0.5
        legacy[0, 0] = torch.nextafter(
            torch.tensor(0.5, dtype=torch.float32),
            torch.tensor(1.0, dtype=torch.float32),
        )
        folded = wide.reshape(2, 4, 2048).sum(dim=1)
        self.assertFalse(torch.equal(folded, legacy))
        decision = SimpleNamespace(
            candidate_features=legacy,
            wide_candidate_features=wide,
            issue85_payload={},
        )
        example = SimpleNamespace(base=SimpleNamespace(decisions=[decision]))

        result = pilot_runner._initial_parity(
            FakePolicy(), FakePolicy(), [example], arm="B",
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["captured_native_training_decisions"], 1)

        legacy[0, 0] = 0.51
        with self.assertRaisesRegex(RuntimeError, "exceeds FP32 fold tolerance"):
            pilot_runner._initial_parity(FakePolicy(), FakePolicy(), [example], arm="D")

    def test_predecessor_progress_requires_exact_fixed_successful_b_cohort(self):
        rows = [
            {
                "unit_id": f"PILOT-TRAIN:B:{owner}",
                "phase": "training",
                "issue85_arm": "B",
                "owner_seed": owner,
            }
            for owner in pilot_runner.PILOT_OWNERS
        ]
        seed_table = {
            "schema_id": "issue85-nonformal-pilot-seed-table-v1",
            "rows": rows,
            "rows_sha256": pilot_runner.sha256(pilot_runner.canonical(rows)),
        }
        unit_ids = sorted(row["unit_id"] for row in rows)
        summary = {
            "status": "SUCCEEDED",
            "expected": len(rows),
            "successful_completions": len(rows),
            "successful_unit_ids": unit_ids,
            "failed_attempts": [],
            "incomplete_attempts": [],
            "incomplete": [],
            "attempt_count_by_owner": {unit_id: 1 for unit_id in unit_ids},
        }
        progress = {
            "schema_id": "issue85-pilot-progress-v1",
            "status": "FAILED_OR_INCOMPLETE",
            "package_commit": pilot_runner.PILOT_RESUME_PREDECESSOR_SHA,
            "source_lock_sha256": "lock-sha",
            "issue84_source_commit": pilot_runner.ISSUE84_ACCEPTED_COMMIT,
            "active_stage": "pilot-actor-update",
            "active_arm": "B",
            "failure": {
                "type": "AttributeError",
                "detail": "module 'issue85_exact_issue84_driver' has no attribute '_training_examples'",
            },
            "training_runs": {"B": summary},
            "evaluation_runs": {},
            "actor_updates": {},
            "seed_table_rows": len(rows),
        }
        checked_rows, checked_summary = pilot_runner._validate_predecessor_progress(
            progress,
            predecessor_commit=pilot_runner.PILOT_RESUME_PREDECESSOR_SHA,
            current_identity={"package_commit": "b" * 40, "source_lock_sha256": "lock-sha"},
            prior_seed_table=seed_table,
            current_seed_table=seed_table,
        )

        self.assertEqual(checked_rows, rows)
        self.assertEqual(checked_summary, summary)
        changed_table = {**seed_table, "rows": [*rows[:-1], {**rows[-1], "owner_seed": 999}]}
        with self.assertRaisesRegex(RuntimeError, "exact unchanged pilot owner/seed table"):
            pilot_runner._validate_predecessor_progress(
                progress,
                predecessor_commit=pilot_runner.PILOT_RESUME_PREDECESSOR_SHA,
                current_identity={"package_commit": "b" * 40, "source_lock_sha256": "lock-sha"},
                prior_seed_table=seed_table,
                current_seed_table=changed_table,
            )

    def test_seed_table_has_only_registered_nonformal_pilot_runs(self):
        table = pilot_runner.build_seed_rows(FakeBackend)
        rows = table["rows"]
        training = [row for row in rows if row["phase"] == "training"]
        evaluation = [row for row in rows if row["phase"] == "evaluation"]

        self.assertEqual(table["training_games_expected"], 80)
        self.assertEqual(table["evaluation_games_expected"], 112)
        self.assertEqual(len(rows), 192)
        self.assertTrue(all(row["formal"] is False for row in rows))
        self.assertEqual({row["issue85_arm"] for row in training}, set(pilot_runner.NEW_ARMS))
        self.assertTrue(all(row["issue84_arm"] == row["issue85_arm"] for row in training))
        self.assertEqual({row["arm"] for row in evaluation}, set(pilot_runner.POLICIES))
        self.assertEqual(
            {int(row["owner_seed"]) for row in rows},
            set(pilot_runner.PILOT_OWNERS),
        )
        self.assertEqual(
            len({row["unit_id"] for row in rows}),
            len(rows),
        )
        self.assertTrue(all(row["issue84_policy_label"] == "pi12" for row in training))
        self.assertTrue(all(row["phase"] != "formal" for row in rows))

    def test_pilot_evaluation_dispatch_uses_branch_but_retains_registered_policy_identity(self):
        parent_arm = ContextVar("test_parent_arm", default="unset")
        parent_group = ContextVar("test_parent_group", default="unset")
        calls = []

        def worker_owner(job, ledger):
            row = job["row"]
            calls.append({
                "job": job,
                "current_row": pilot_runner._CURRENT_ROW.get(),
                "parent_arm": parent_arm.get(),
                "parent_group": parent_group.get(),
            })
            return {"status": "SUCCEEDED", "unit_id": row["unit_id"], "arm": row["arm"]}

        executor = SimpleNamespace(_worker_owner=worker_owner)
        issue84_driver = SimpleNamespace(
            _ISSUE84_HELDOUT_ORIGINAL_ARM=parent_arm,
            _ISSUE84_HELDOUT_ORIGINAL_GROUP=parent_group,
        )
        pilot_runner._install_issue85_pilot_evaluation_dispatch_compat(executor, issue84_driver)
        for policy in pilot_runner.POLICIES:
            row = {
                "unit_id": f"PILOT-EVAL:8530001:{policy}",
                "phase": "evaluation",
                "arm": policy,
                "issue84_arm": None if policy == "PARENT" else policy,
                "issue84_policy_label": "pi12" if policy == "PARENT" else "pi20",
            }
            if policy in pilot_runner.NEW_ARMS:
                row["issue85_arm"] = policy
            job = {"pilot": True, "phase": "evaluation", "row": row}
            current_row_token = pilot_runner._CURRENT_ROW.set(row)
            try:
                result = executor._worker_owner(job, object())
            finally:
                pilot_runner._CURRENT_ROW.reset(current_row_token)

            call = calls[-1]
            self.assertEqual(call["job"]["row"]["arm"], "BRANCH")
            self.assertIs(call["current_row"], row)
            self.assertEqual(call["parent_arm"], row["issue84_arm"])
            self.assertEqual(call["parent_group"], policy)
            self.assertEqual(row["arm"], policy)
            self.assertEqual(job["row"]["arm"], policy)
            self.assertEqual(result["unit_id"], row["unit_id"])
            self.assertEqual(result["arm"], policy)
        self.assertEqual(parent_arm.get(), "unset")
        self.assertEqual(parent_group.get(), "unset")

        other_row = {"unit_id": "TRAIN:8530001:B", "phase": "training", "arm": "TRAIN"}
        other_job = {"pilot": True, "phase": "training", "row": other_row}
        other_result = executor._worker_owner(other_job, object())
        self.assertIs(calls[-1]["job"], other_job)
        self.assertEqual(other_result["arm"], "TRAIN")

    def test_policy_and_search_rng_are_arm_specific_and_stable(self):
        first = pilot_runner.build_seed_rows(FakeBackend)["rows"]
        second = pilot_runner.build_seed_rows(FakeBackend)["rows"]
        self.assertEqual(first, second)
        by_training_owner = {
            row["issue85_arm"]: row
            for row in first
            if row["phase"] == "training" and row["owner_seed"] == pilot_runner.PILOT_OWNERS[0]
        }
        self.assertEqual(len({row["policy_rng_seed"] for row in by_training_owner.values()}), 5)
        self.assertEqual(len({tuple(row["search_invocation_seeds"]) for row in by_training_owner.values()}), 5)

    def test_summary_counts_success_receipts_separately_from_attempts(self):
        row = {"unit_id": "PILOT-TRAIN:B:8530001"}
        summary = pilot_runner.summarize_stage({
            "status": "INCOMPLETE",
            "completed": 1,
            "results": {row["unit_id"]: {"status": "SUCCEEDED"}},
            "tries": {row["unit_id"]: 4, "other": 3},
            "failed_attempts": [{"unit_id": "other", "failure": "bad"}],
            "incomplete_attempts": [{"unit_id": "other", "attempt": "attempt-0002"}],
            "elapsed_seconds": 12.0,
        }, [row, {"unit_id": "other"}])

        self.assertEqual(summary["successful_completions"], 1)
        self.assertEqual(summary["expected"], 2)
        self.assertEqual(summary["incomplete"], ["other"])
        self.assertEqual(summary["attempt_count_by_owner"][row["unit_id"]], 4)
        self.assertEqual(len(summary["failed_attempts"]), 1)
        self.assertEqual(len(summary["incomplete_attempts"]), 1)

    def test_carried_c_failure_is_resumable_without_an_automatic_retry_loop(self):
        class Backend:
            CONCURRENCY = 1
            PINNED_PYTHON = Path("/pinned/python")
            REPO = Path("/repo")

            @staticmethod
            def unit_key(unit_id):
                return unit_id

            @staticmethod
            def source_lock_sha():
                return "source-lock"

            @staticmethod
            def write_json_once(path, value):
                with Path(path).open("x", encoding="utf-8") as handle:
                    json.dump(value, handle)

        class FailedWorker:
            pid = 12345

            @staticmethod
            def poll():
                return 1

        unit_id = "PILOT-TRAIN:C:8530001"
        rows = [{"unit_id": unit_id, "phase": "training", "arm": "C", "owner_seed": 8530001}]
        snapshots = []
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ",
            {
                "STS_LIGHTSPEED_SOURCE_DIR": "/locked/sts_lightspeed",
                "STSRL_SOURCE_DIR": "/locked/spire-research",
                "ISSUE84_SOURCE_DIR": "/locked/issue84",
                "ISSUE85_PILOT_ROOT": "/pilot",
            },
        ), mock.patch.object(pilot_runner.subprocess, "Popen", return_value=FailedWorker()) as popen:
            result = pilot_runner.run_units_bounded(
                Backend(), rows, root=Path(directory), clock=None,
                package_commit="commit", progress_callback=snapshots.append,
                initial_attempt_count_by_owner={unit_id: 3},
            )
            attempt_names = sorted(
                path.name for path in (Path(directory) / "units" / unit_id).glob("attempt-*")
            )

        self.assertEqual(attempt_names, ["attempt-0004"])
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(result["status"], "PAUSED_AFTER_FAILED_ATTEMPT")
        self.assertEqual(result["tries"][unit_id], 1)
        self.assertEqual(result["carried_attempt_count_by_owner"], {unit_id: 3})
        self.assertEqual(snapshots[-1]["carried_attempt_counts"], {unit_id: 3})

    def test_carried_c_success_is_reused_without_a_new_worker_attempt(self):
        row = self._pilot_row("PILOT-TRAIN:C:8530001", 8530001)
        row.update(phase="training", issue85_arm="C", arm="TRAIN")
        backend = self._receipt_backend()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            prior_root = base / "prior"
            current_root = base / "current"
            self._write_pilot_success_chain(prior_root, row)
            with mock.patch.dict(
                "os.environ",
                {
                    "STS_LIGHTSPEED_SOURCE_DIR": "/locked/sts_lightspeed",
                    "STSRL_SOURCE_DIR": "/locked/spire-research",
                    "ISSUE84_SOURCE_DIR": "/locked/issue84",
                    "ISSUE85_PILOT_ROOT": "/pilot",
                },
            ), mock.patch.object(pilot_runner.subprocess, "Popen") as popen:
                result = pilot_runner.run_units_bounded(
                    backend,
                    [row],
                    root=current_root,
                    clock=None,
                    package_commit="c" * 40,
                    initial_attempt_count_by_owner={row["unit_id"]: 2},
                    prior_success_receipt_roots_by_owner={
                        row["unit_id"]: (prior_root, "a" * 40),
                    },
                )

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["tries"][row["unit_id"]], 0)
        self.assertEqual(result["carried_attempt_count_by_owner"], {row["unit_id"]: 2})
        popen.assert_not_called()

    def test_ten_carried_c_failures_bind_one_prior_attempt_to_each_owner(self):
        rows = [
            row for row in pilot_runner.build_seed_rows(FakeBackend)["rows"]
            if row["phase"] == "training" and row["issue85_arm"] == "C"
        ]
        carried_ids = [str(row["unit_id"]) for row in rows[:10]]

        offsets = pilot_runner._carried_c_attempt_counts(
            rows,
            {"prior_C_failed_attempts": [{"unit_id": unit_id} for unit_id in carried_ids]},
        )

        self.assertEqual(offsets, {unit_id: 1 for unit_id in carried_ids})
        self.assertEqual(len(offsets), 10)

    def test_resumed_stage_clock_includes_active_tail_since_last_snapshot(self):
        progress = {
            "active_stage": "evaluation-games",
            "active_arm": "PARENT",
            "live_stage_progress": {
                "stage": "evaluation-games",
                "arm": "PARENT",
                "cumulative_active_wall_seconds": 12.0,
                "clock_elapsed_seconds": 100.0,
            },
        }
        base = pilot_runner.stage_resume_wall_base(
            progress, stage="evaluation-games", arm="PARENT", current_clock_elapsed=107.0,
        )
        self.assertEqual(base, 19.0)
        self.assertEqual(
            pilot_runner.stage_resume_wall_base(
                progress, stage="training-games", arm="PARENT", current_clock_elapsed=107.0,
            ),
            0.0,
        )
        summary = pilot_runner.stage_summary_with_cumulative_wall(
            {"elapsed_seconds": 4.0, "status": "SUCCEEDED"},
            stage_base=base,
            stage_clock_start=107.0,
            current_clock_elapsed=110.0,
        )
        self.assertEqual(summary["invocation_elapsed_seconds"], 4.0)
        self.assertEqual(summary["elapsed_seconds"], 22.0)

    @staticmethod
    def _clock_reconciliation_fixture(directory):
        package_root = Path(directory) / "package"
        package_root.mkdir()
        progress_path = package_root / "pilot-progress.json"
        progress = {"schema_id": "issue85-pilot-progress-v1", "status": "READY", "cumulative_active_wall_seconds": 50.0}
        pilot_runner._atomic_json(progress_path, progress)
        identity = {"package_commit": "e" * 40}
        completed_reuse = {
            "source_commit": "c" * 40,
            "progress_sha256": "d" * 64,
            "progress": {"cumulative_active_wall_seconds": 40.0},
        }
        return package_root, progress_path, progress, identity, completed_reuse

    def test_pilot_clock_rejects_rollback_below_current_package_high_water(self):
        with tempfile.TemporaryDirectory() as directory:
            package_root, progress_path, progress, identity, completed_reuse = self._clock_reconciliation_fixture(directory)
            predecessor_wall = 13_592.161178517
            saved_high_water = 13_650.42375135
            completed_reuse["progress"]["cumulative_active_wall_seconds"] = predecessor_wall
            progress["cumulative_active_wall_seconds"] = saved_high_water
            progress["clock_reconciliation"] = {
                "shared_clock_active_wall_seconds_before_resume": saved_high_water,
            }
            progress["clock_reconciliation_sha256"] = "a" * 64
            write_progress = mock.Mock()

            with self.assertRaisesRegex(RuntimeError, "below a persisted pilot high-water"):
                pilot_runner._reconcile_pilot_task_clock(
                    package_root, progress_path, progress,
                    identity=identity, completed_reuse=completed_reuse, before=13_600.0,
                    atomic_json=write_progress,
                )

            write_progress.assert_not_called()
            self.assertFalse((package_root / "pilot-progress-clock-reconciliation.json").exists())

    def test_pilot_clock_reconstructs_if_crash_precedes_receipt_write(self):
        with tempfile.TemporaryDirectory() as directory:
            package_root, progress_path, progress, identity, completed_reuse = self._clock_reconciliation_fixture(directory)
            receipt_path = package_root / "pilot-progress-clock-reconciliation.json"
            with mock.patch.object(pilot_runner, "_write_or_verify_once", side_effect=OSError("simulated crash before receipt write")):
                with self.assertRaisesRegex(OSError, "simulated crash"):
                    pilot_runner._reconcile_pilot_task_clock(
                        package_root, progress_path, progress,
                        identity=identity, completed_reuse=completed_reuse, before=100.0,
                    )
            self.assertFalse(receipt_path.exists())
            self.assertNotIn("clock_reconciliation", pilot_runner.read_json(progress_path))

            receipt, receipt_sha = pilot_runner._reconcile_pilot_task_clock(
                package_root, progress_path, pilot_runner.read_json(progress_path),
                identity=identity, completed_reuse=completed_reuse, before=101.0,
            )
            self.assertEqual(receipt["shared_clock_active_wall_seconds_before_resume"], 101.0)
            self.assertEqual(pilot_runner.read_json(progress_path)["clock_reconciliation_sha256"], receipt_sha)

    def test_pilot_clock_adopts_orphan_receipt_after_progress_write_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            package_root, progress_path, progress, identity, completed_reuse = self._clock_reconciliation_fixture(directory)
            receipt_path = package_root / "pilot-progress-clock-reconciliation.json"

            def crash_before_progress_reference(path, value):
                raise OSError("simulated crash after receipt write")

            with self.assertRaisesRegex(OSError, "after receipt write"):
                pilot_runner._reconcile_pilot_task_clock(
                    package_root, progress_path, progress,
                    identity=identity, completed_reuse=completed_reuse, before=100.0,
                    atomic_json=crash_before_progress_reference,
                )
            self.assertTrue(receipt_path.is_file())
            receipt_sha_before_recovery = pilot_runner.file_sha256(receipt_path)
            self.assertNotIn("clock_reconciliation", pilot_runner.read_json(progress_path))

            receipt, receipt_sha = pilot_runner._reconcile_pilot_task_clock(
                package_root, progress_path, pilot_runner.read_json(progress_path),
                identity=identity, completed_reuse=completed_reuse, before=101.0,
            )
            self.assertEqual(receipt_sha, receipt_sha_before_recovery)
            self.assertEqual(receipt["shared_clock_active_wall_seconds_before_resume"], 100.0)
            recovered = pilot_runner.read_json(progress_path)
            self.assertEqual(recovered["clock_reconciliation"], receipt)
            self.assertEqual(recovered["cumulative_active_wall_seconds"], 101.0)

    def test_legacy_stage_snapshot_does_not_invent_missing_elapsed_time(self):
        self.assertEqual(
            pilot_runner.stage_resume_wall_base(
                {
                    "active_stage": "training-games",
                    "active_arm": "B",
                    "live_stage_progress": {"stage": "training-games", "arm": "B"},
                },
                stage="training-games",
                arm="B",
                current_clock_elapsed=50.0,
            ),
            0.0,
        )

    def test_resumed_actor_clock_recovers_uncheckpointed_active_tail(self):
        self.assertEqual(
            pilot_runner.actor_update_resume_wall(
                {"elapsed_seconds": 9.0, "active_clock_elapsed_seconds": 100.0},
                current_clock_elapsed=108.0,
            ),
            (17.0, 8.0),
        )
        self.assertEqual(
            pilot_runner.actor_update_resume_wall(
                {"elapsed_seconds": 9.0}, current_clock_elapsed=108.0,
            ),
            (9.0, 0.0),
        )

    def test_incomplete_group_summary_is_not_treated_as_complete_on_resume(self):
        complete = {
            "status": "SUCCEEDED",
            "expected": 16,
            "successful_completions": 16,
            "incomplete": [],
            "successful_unit_ids": ["expected-owner"],
        }
        incomplete = {**complete, "successful_completions": 15, "incomplete": ["owner-16"]}
        self.assertTrue(pilot_runner.stage_summary_is_complete(complete, 16))
        self.assertTrue(pilot_runner.stage_summary_is_complete(complete, 16, ["expected-owner"]))
        self.assertFalse(pilot_runner.stage_summary_is_complete(complete, 16, ["wrong-owner"]))
        self.assertFalse(pilot_runner.stage_summary_is_complete(incomplete, 16))
        self.assertFalse(pilot_runner.stage_summary_is_complete({**complete, "status": "INCOMPLETE"}, 16))

    @staticmethod
    def _receipt_backend():
        class Backend:
            CONCURRENCY = 1

            @staticmethod
            def unit_key(unit_id):
                return str(unit_id)

            @staticmethod
            def source_lock_sha():
                return "source-lock-sha"

        return Backend

    def _write_pilot_success_chain(self, root, row, *, attempt_name="attempt-0001"):
        root = Path(root)
        attempt = root / "units" / str(row["unit_id"]) / attempt_name
        attempt.mkdir(parents=True)
        run_id = f"{row['unit_id']}-{attempt_name}"
        package_commit = "a" * 40
        result_path = attempt / "result.json"
        job = {
            "schema_id": "issue79-owner-worker-job-v1",
            "artifact_root": str(root.resolve()),
            "attempt_dir": str(attempt.resolve()),
            "result_path": str(result_path.resolve()),
            "output_dir": str((attempt / "native").resolve()),
            "run_id": run_id,
            "unit_id": row["unit_id"],
            "phase": row["phase"],
            "row": dict(row),
            "pilot": True,
            "package_commit": package_commit,
            "runner_code_commit": package_commit,
            "source_lock_sha256": "source-lock-sha",
        }
        started = {
            "schema_id": "issue79-owner-attempt-start-v1",
            "unit_id": row["unit_id"],
            "owner_seed": row["owner_seed"],
            "arm": row["arm"],
            "run_id": run_id,
            "pid": 1234,
            "started_utc": "2026-11-03T00:00:00Z",
            "started_monotonic": 1.0,
            "package_commit": package_commit,
            "seed_row_sha256": pilot_runner.sha256(pilot_runner.canonical(dict(row))),
        }
        result = {
            "schema_id": "issue75-stage1-unit-result-v1",
            "status": "SUCCEEDED",
            "package_commit": package_commit,
            "source_lock_sha256": "source-lock-sha",
            "runner_code_commit": package_commit,
            "unit_id": row["unit_id"],
            "phase": row["phase"],
            "formal": False,
            "owner_seed": row["owner_seed"],
            "game_seed": row["game_seed"],
            "arm": row["arm"],
            "paired_owner_key": row["paired_owner_key"],
            "terminal_outcome": "WIN",
            "terminal_floor": 16,
            "resource": {
                "worker_process_cpu_seconds": 0.5,
                "worker_wall_seconds": 1.0,
                "ledger_run_id": run_id,
            },
        }
        pilot_runner._write_once(attempt / "worker-job.json", job)
        pilot_runner._write_once(attempt / "started.json", started)
        pilot_runner._write_once(result_path, result)
        pilot_runner._write_once(attempt / "finished.json", {
            "schema_id": "issue79-owner-attempt-finish-v1",
            "unit_id": row["unit_id"],
            "run_id": run_id,
            "status": "SUCCEEDED",
            "finished_utc": "2026-11-03T00:00:01Z",
            "finished_monotonic": 2.0,
            "result_sha256": pilot_runner.file_sha256(result_path),
        })
        return attempt, result_path

    @staticmethod
    def _pilot_row(unit_id="owner-1", owner_seed=1):
        return {
            "unit_id": unit_id,
            "phase": "evaluation",
            "arm": "A",
            "formal": False,
            "owner_seed": owner_seed,
            "game_seed": owner_seed,
            "paired_owner_key": f"pilot-{owner_seed}",
        }

    def test_resume_skip_requires_a_valid_receipt_chain_for_every_owner(self):
        rows = [self._pilot_row("owner-1", 1), self._pilot_row("owner-2", 2)]
        backend = self._receipt_backend()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evaluation" / "A"
            self._write_pilot_success_chain(root, rows[0])
            self.assertFalse(pilot_runner.stage_receipts_are_complete(
                backend, root=root, rows=rows, package_commit="a" * 40,
            ))
            self._write_pilot_success_chain(root, rows[1])
            self.assertTrue(pilot_runner.stage_receipts_are_complete(
                backend, root=root, rows=rows, package_commit="a" * 40,
            ))

    def test_stage_receipts_accept_a_per_owner_immutable_predecessor_binding(self):
        rows = [self._pilot_row("owner-1", 1), self._pilot_row("owner-2", 2)]
        backend = self._receipt_backend()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            current_root = base / "current"
            old_root = base / "old"
            self._write_pilot_success_chain(current_root, rows[1])
            self._write_pilot_success_chain(old_root, rows[0])

            self.assertTrue(pilot_runner.stage_receipts_are_complete(
                backend,
                root=current_root,
                rows=rows,
                package_commit="a" * 40,
                receipt_roots_by_owner={rows[0]["unit_id"]: (old_root, "a" * 40)},
            ))

    def test_success_rejects_missing_job_start_or_finish_receipt(self):
        backend = self._receipt_backend()
        row = self._pilot_row()
        for missing in ("worker-job.json", "started.json", "finished.json"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "evaluation" / "A"
                attempt, _ = self._write_pilot_success_chain(root, row)
                (attempt / missing).unlink()
                with self.assertRaisesRegex(RuntimeError, "complete job/start/finish/result chain"):
                    pilot_runner.pilot_success_record(
                        backend, root=root, row=row, package_commit="a" * 40,
                    )

    def test_success_rejects_source_and_frozen_row_binding_mismatches(self):
        backend = self._receipt_backend()
        row = self._pilot_row()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evaluation" / "A"
            attempt, _ = self._write_pilot_success_chain(root, row)
            path = attempt / "worker-job.json"
            job = pilot_runner.read_json(path)
            job["source_lock_sha256"] = "wrong-source-lock"
            path.write_bytes(pilot_runner.canonical(job) + b"\n")
            with self.assertRaisesRegex(RuntimeError, "worker-job receipt"):
                pilot_runner.pilot_success_record(
                    backend, root=root, row=row, package_commit="a" * 40,
                )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evaluation" / "A"
            attempt, _ = self._write_pilot_success_chain(root, row)
            path = attempt / "worker-job.json"
            job = pilot_runner.read_json(path)
            job["row"]["owner_seed"] = 999
            path.write_bytes(pilot_runner.canonical(job) + b"\n")
            with self.assertRaisesRegex(RuntimeError, "worker-job receipt"):
                pilot_runner.pilot_success_record(
                    backend, root=root, row=row, package_commit="a" * 40,
                )

    def test_success_rejects_broken_result_hash(self):
        backend = self._receipt_backend()
        row = self._pilot_row()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evaluation" / "A"
            attempt, result_path = self._write_pilot_success_chain(root, row)
            result = pilot_runner.read_json(result_path)
            result["resource"]["worker_wall_seconds"] = 2.0
            result_path.write_bytes(pilot_runner.canonical(result) + b"\n")
            with self.assertRaisesRegex(RuntimeError, "start/finish receipts"):
                pilot_runner.pilot_success_record(
                    backend, root=root, row=row, package_commit="a" * 40,
                )

    def test_success_rejects_duplicate_success_attempts(self):
        backend = self._receipt_backend()
        row = self._pilot_row()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evaluation" / "A"
            self._write_pilot_success_chain(root, row)
            self._write_pilot_success_chain(root, row, attempt_name="attempt-0002")
            with self.assertRaisesRegex(RuntimeError, "multiple successful attempts"):
                pilot_runner.pilot_success_record(
                    backend, root=root, row=row, package_commit="a" * 40,
                )

    def test_failure_streak_is_reported_but_is_not_a_permanent_stop_rule(self):
        events = [
            ("2026-11-01T00:00:00Z", 1.0, "FAILED"),
            ("2026-11-01T00:00:01Z", 2.0, "FAILED"),
            ("2026-11-01T00:00:02Z", 3.0, "SUCCEEDED"),
            ("2026-11-01T00:00:03Z", 4.0, "FAILED"),
            ("2026-11-01T00:00:04Z", 5.0, "FAILED"),
        ]
        self.assertEqual(pilot_runner.trailing_failure_streak(events), 2)
        self.assertEqual(pilot_runner.trailing_failure_streak(events + [("2026-11-01T00:00:05Z", 6.0, "FAILED")]), 3)
        plan = pilot_runner.pilot_plan(resume_from=pilot_runner.PILOT_RESUME_PREDECESSOR_SHA)
        self.assertNotIn("retry_limit_per_owner", plan)
        self.assertNotIn("stop_after_consecutive_failed_attempts", plan)
        self.assertEqual(
            plan["failed_attempt_handling"],
            "pause_current_batch_for_diagnosis; preserve attempts and resume after cause is corrected",
        )

    def test_plan_keeps_formal_cohorts_out_of_pilot(self):
        plan = pilot_runner.pilot_plan()
        self.assertIsNone(plan["resume_from"])
        self.assertEqual(plan["training_cohort_games"], 80)
        self.assertEqual(plan["evaluation_cohort_games"], 112)
        self.assertEqual(plan["training_games"], 80)
        self.assertEqual(plan["evaluation_games"], 112)
        self.assertEqual(plan["planned_reused_predecessor_training_receipts"], 0)
        self.assertEqual(plan["planned_reused_predecessor_actor_updates"], 0)
        self.assertIsNone(plan["actor_update_carry_forward_source_commit"])
        self.assertEqual(plan["prior_failed_C_attempts_preserved_for_accounting"], 0)
        self.assertEqual(plan["prior_C_package_attempts_preserved_for_accounting"], 0)
        self.assertIsNone(plan["C_training_outcome_carry_forward_source_commit"])
        self.assertEqual(plan["new_training_game_slots_before_retries"], 80)
        self.assertEqual(plan["new_evaluation_game_slots_before_retries"], 112)
        self.assertEqual(plan["new_game_slots_before_retries"], 192)
        self.assertEqual(plan["unique_pilot_game_outcomes"], 192)
        self.assertEqual(plan["pilot_game_executions"], 192)
        self.assertEqual(plan["formal_games"], 0)
        self.assertEqual(plan["owner_ids"], list(range(8_530_001, 8_530_017)))

    def test_continuation_plan_separates_reused_receipts_new_slots_and_outcomes(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = pilot_runner.main([
                "--plan",
                "--resume-from",
                pilot_runner.PILOT_RESUME_PREDECESSOR_SHA,
            ])

        self.assertEqual(result, 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["resume_from"], pilot_runner.PILOT_RESUME_PREDECESSOR_SHA)
        self.assertEqual(plan["training_cohort_games"], 80)
        self.assertEqual(plan["evaluation_cohort_games"], 112)
        self.assertEqual(plan["training_games"], 80)
        self.assertEqual(plan["evaluation_games"], 112)
        self.assertEqual(plan["planned_reused_predecessor_training_receipts"], 20)
        self.assertEqual(plan["planned_reused_C_training_receipts"], 4)
        self.assertEqual(plan["C_training_outcome_carry_forward_source_commit"], pilot_runner.PILOT_C_OUTCOME_CARRY_FORWARD_SHA)
        self.assertEqual(plan["planned_reused_predecessor_actor_updates"], 1)
        self.assertEqual(
            plan["actor_update_carry_forward_source_commit"],
            pilot_runner.PILOT_B_ACTOR_UPDATE_CARRY_FORWARD_SHA,
        )
        self.assertEqual(plan["prior_failed_C_attempts_preserved_for_accounting"], 14)
        self.assertEqual(plan["prior_C_package_attempts_preserved_for_accounting"], 8)
        self.assertEqual(plan["new_training_game_slots_before_retries"], 60)
        self.assertEqual(plan["new_evaluation_game_slots_before_retries"], 112)
        self.assertEqual(plan["new_game_slots_before_retries"], 172)
        self.assertEqual(plan["unique_pilot_game_outcomes"], 192)
        self.assertEqual(plan["pilot_game_executions"], 172)
        self.assertEqual(plan["formal_games"], 0)

    def test_completed_package_plan_carries_forward_terminal_C_and_D_without_replaying_them(self):
        plan = pilot_runner.pilot_plan(
            resume_from=pilot_runner.PILOT_RESUME_PREDECESSOR_SHA,
            reuse_completed_package=pilot_runner.PILOT_COMPLETED_TRAINING_CARRY_FORWARD_SHA,
        )

        self.assertEqual(plan["planned_reused_predecessor_training_receipts"], 48)
        self.assertEqual(plan["planned_reused_completed_package_training_receipts"], 28)
        self.assertEqual(plan["planned_reused_C_training_receipts"], 16)
        self.assertEqual(plan["planned_reused_D_training_receipts"], 16)
        self.assertEqual(plan["planned_reused_predecessor_actor_updates"], 2)
        self.assertEqual(plan["new_training_game_slots_before_retries"], 32)
        self.assertEqual(plan["new_evaluation_game_slots_before_retries"], 112)
        self.assertEqual(plan["new_game_slots_before_retries"], 144)
        self.assertEqual(plan["pilot_game_executions"], 144)
        self.assertEqual(plan["unique_pilot_game_outcomes"], 192)
        self.assertEqual(plan["formal_games"], 0)

        with self.assertRaisesRegex(ValueError, "requires the registered pilot predecessor"):
            pilot_runner.pilot_plan(
                reuse_completed_package=pilot_runner.PILOT_COMPLETED_TRAINING_CARRY_FORWARD_SHA,
            )

    def test_plan_rejects_unregistered_predecessor(self):
        with self.assertRaisesRegex(ValueError, "unregistered predecessor source"):
            pilot_runner.pilot_plan(resume_from="a" * 40)

    def test_recorded_completed_package_handles_fresh_and_carried_progress(self):
        self.assertIsNone(pilot_runner._recorded_completed_package({}))
        self.assertIsNone(pilot_runner._recorded_completed_package({"pilot_completed_training_reuse": None}))
        self.assertEqual(
            pilot_runner._recorded_completed_package({
                "pilot_completed_training_reuse": {
                    "predecessor_package_commit": pilot_runner.PILOT_COMPLETED_TRAINING_CARRY_FORWARD_SHA,
                },
            }),
            pilot_runner.PILOT_COMPLETED_TRAINING_CARRY_FORWARD_SHA,
        )
        with self.assertRaisesRegex(RuntimeError, "malformed completed-package"):
            pilot_runner._recorded_completed_package({"pilot_completed_training_reuse": "bad"})

    def test_predecessor_reuse_allows_only_reviewed_runner_api_and_public_schema_repairs(self):
        approved_paths = (
            "studies/issue-85-representation/feature_firewall.py",
            "studies/issue-85-representation/pilot_runner.py",
            "studies/issue-85-representation/tests/test_feature_firewall.py",
            "studies/issue-85-representation/tests/test_pilot_runner.py",
            "studies/issue-85-representation/runtime_integration.py",
            "studies/issue-85-representation/tests/test_runtime_integration.py",
        )
        pilot_runner._validate_predecessor_reuse_source_diff(approved_paths)
        with self.assertRaisesRegex(RuntimeError, "reviewed runner/API repairs"):
            pilot_runner._validate_predecessor_reuse_source_diff(
                (*approved_paths, "studies/issue-85-representation/contract.json"),
            )
        with self.assertRaisesRegex(RuntimeError, "reviewed runner/API repairs"):
            pilot_runner._validate_predecessor_reuse_source_diff(approved_paths[:-1])

    def test_failed_worker_receipt_binds_the_exact_stage_root_and_attempt_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            stage = package / "training" / "C"
            attempt = stage / "units" / "PILOT-TRAIN-C-8530001" / "attempt-0001"
            result = attempt / "result.json"
            job = {
                "artifact_root": str(stage),
                "attempt_dir": str(attempt),
                "result_path": str(result),
            }
            self.assertTrue(pilot_runner._worker_job_paths_match(
                job, stage_root=stage, attempt_dir=attempt, result_path=result,
            ))
            self.assertFalse(pilot_runner._worker_job_paths_match(
                job, stage_root=package, attempt_dir=attempt, result_path=result,
            ))
            job["result_path"] = str(attempt / "other.json")
            self.assertFalse(pilot_runner._worker_job_paths_match(
                job, stage_root=stage, attempt_dir=attempt, result_path=result,
            ))

    def test_failed_result_receipt_fields_may_be_absent_but_not_conflicting(self):
        self.assertTrue(pilot_runner._optional_receipt_field_matches({}, "package_commit", "source"))
        self.assertTrue(pilot_runner._optional_receipt_field_matches(
            {"package_commit": "source"}, "package_commit", "source",
        ))
        self.assertFalse(pilot_runner._optional_receipt_field_matches(
            {"package_commit": "other"}, "package_commit", "source",
        ))

    def test_b_actor_update_carry_forward_only_allows_reviewed_paths(self):
        approved_paths = (
            "studies/issue-85-representation/feature_firewall.py",
            "studies/issue-85-representation/pilot_runner.py",
            "studies/issue-85-representation/tests/test_feature_firewall.py",
            "studies/issue-85-representation/tests/test_pilot_runner.py",
        )
        pilot_runner._validate_b_actor_update_carry_forward_source_diff(approved_paths)
        with self.assertRaisesRegex(RuntimeError, "B actor-update carry-forward"):
            pilot_runner._validate_b_actor_update_carry_forward_source_diff(
                (*approved_paths, "studies/issue-85-representation/contract.json"),
            )

    def test_completed_package_reuse_only_allows_the_reviewed_fold_runner_repair(self):
        approved_paths = (
            "studies/issue-85-representation/pilot_runner.py",
            "studies/issue-85-representation/tests/test_pilot_runner.py",
        )
        pilot_runner._validate_completed_package_reuse_source_diff(approved_paths)
        with self.assertRaisesRegex(RuntimeError, "completed Issue 85 training reuse"):
            pilot_runner._validate_completed_package_reuse_source_diff(
                (*approved_paths, "studies/issue-85-representation/contract.json"),
            )

    def test_resumed_progress_reuse_only_allows_the_pilot_dispatch_repair(self):
        approved_paths = (
            "studies/issue-85-representation/pilot_runner.py",
            "studies/issue-85-representation/tests/test_pilot_runner.py",
        )
        pilot_runner._validate_resume_progress_source_diff(approved_paths)
        with self.assertRaisesRegex(RuntimeError, "evaluation-dispatch repair"):
            pilot_runner._validate_resume_progress_source_diff(
                (*approved_paths, "studies/issue-85-representation/contract.json"),
            )

    def test_parent_resume_offsets_preserve_exactly_eight_failed_receipts(self):
        rows = [
            {"unit_id": f"PILOT-EVAL:{owner}:PARENT"}
            for owner in pilot_runner.PILOT_OWNERS
        ]
        attempted_ids = [str(row["unit_id"]) for row in rows[:8]]
        summary = {
            "status": "PAUSED_AFTER_FAILED_ATTEMPT",
            "expected": len(rows),
            "successful_completions": 0,
            "successful_unit_ids": [],
            "incomplete_attempts": [],
            "attempt_count_by_owner": {
                str(row["unit_id"]): int(row["unit_id"] in attempted_ids) for row in rows
            },
            "failed_attempts": [
                {
                    "unit_id": unit_id,
                    "attempt": f"{unit_id}-attempt-0001",
                    "failure_type": "ValueError",
                    "failure": "unknown study arm PARENT",
                    "exit_code": 1,
                }
                for unit_id in attempted_ids
            ],
        }

        offsets = pilot_runner._parent_failure_attempt_offsets(summary, rows)

        self.assertEqual(sum(offsets.values()), 8)
        self.assertEqual({unit_id for unit_id, count in offsets.items() if count}, set(attempted_ids))
        for invalid in (
            {**summary, "failed_attempts": summary["failed_attempts"][:-1]},
            {**summary, "attempt_count_by_owner": {**summary["attempt_count_by_owner"], attempted_ids[0]: 0}},
            {**summary, "status": "SUCCEEDED"},
        ):
            with self.assertRaisesRegex(RuntimeError, "retained PARENT"):
                pilot_runner._parent_failure_attempt_offsets(invalid, rows)

    def test_evaluation_resume_offsets_preserve_eight_failed_policy_attempts(self):
        rows = [
            {"unit_id": f"PILOT-EVAL:{owner}:A"}
            for owner in pilot_runner.PILOT_OWNERS
        ]
        attempted_ids = [str(row["unit_id"]) for row in rows[:8]]
        summary = {
            "status": "PAUSED_AFTER_FAILED_ATTEMPT",
            "expected": len(rows),
            "successful_completions": 0,
            "successful_unit_ids": [],
            "incomplete": sorted(str(row["unit_id"]) for row in rows),
            "incomplete_attempts": [],
            "attempt_count_by_owner": {
                str(row["unit_id"]): int(row["unit_id"] in attempted_ids) for row in rows
            },
            "failed_attempts": [
                {
                    "unit_id": unit_id,
                    "attempt": f"{unit_id}-attempt-0001",
                    "failure_type": "ValueError",
                    "failure": "unknown study arm A",
                    "exit_code": 1,
                }
                for unit_id in attempted_ids
            ],
        }

        offsets = pilot_runner._evaluation_failure_attempt_offsets(summary, rows, policy="A")

        self.assertEqual(sum(offsets.values()), 8)
        self.assertEqual({unit_id for unit_id, count in offsets.items() if count}, set(attempted_ids))
        for invalid in (
            {**summary, "failed_attempts": summary["failed_attempts"][:-1]},
            {**summary, "attempt_count_by_owner": {**summary["attempt_count_by_owner"], attempted_ids[0]: 0}},
            {**summary, "failed_attempts": [{**summary["failed_attempts"][0], "failure": "other"}, *summary["failed_attempts"][1:]]},
        ):
            with self.assertRaisesRegex(RuntimeError, "retained A evaluation"):
                pilot_runner._evaluation_failure_attempt_offsets(invalid, rows, policy="A")

    def test_reuse_manifest_names_the_progress_snapshot_clock_value(self):
        predecessor = {
            "source_commit": pilot_runner.PILOT_RESUME_PREDECESSOR_SHA,
            "root": Path("predecessor-root"),
            "progress_sha256": "progress-sha",
            "seed_table_sha256": "seed-sha",
            "seed_table": {"rows_sha256": "rows-sha"},
            "summary": {"successful_completions": 16},
            "source_diff_paths": ["pilot_runner.py"],
            "rows": [{"unit_id": "PILOT-TRAIN:B:8530001"}],
            "unit_receipts": [{"unit_id": "PILOT-TRAIN:B:8530001"}],
            "predecessor_progress_snapshot_active_wall_seconds": 94.170648168,
        }
        document = pilot_runner._predecessor_receipt_document(
            predecessor,
            current_identity={
                "package_commit": "b" * 40,
                "source_lock_sha256": "lock-sha",
            },
        )

        self.assertEqual(document["predecessor_progress_snapshot_active_wall_seconds"], 94.170648168)
        self.assertNotIn("active_wall_seconds_before_resume", document)

    def test_forecast_does_not_mislabel_partial_components_as_all_in(self):
        training = {
            arm: {
                "status": "SUCCEEDED",
                "expected": 16,
                "successful_completions": 16,
                "elapsed_seconds": 10.0,
                "failed_attempts": [],
                "attempt_count_by_owner": {str(owner): 1 for owner in pilot_runner.PILOT_OWNERS},
            }
            for arm in pilot_runner.NEW_ARMS
        }
        evaluation = {
            policy: {
                "status": "SUCCEEDED",
                "expected": 16,
                "successful_completions": 16,
                "elapsed_seconds": 20.0,
                "failed_attempts": [],
                "attempt_count_by_owner": {str(owner): 1 for owner in pilot_runner.PILOT_OWNERS},
            }
            for policy in pilot_runner.POLICIES
        }
        carried_c_ids = [f"PILOT-TRAIN:C:{owner}" for owner in pilot_runner.PILOT_OWNERS[:10]]
        training["C"]["attempt_count_by_owner"] = {
            f"PILOT-TRAIN:C:{owner}": 1 for owner in pilot_runner.PILOT_OWNERS
        }
        training["C"]["attempt_count_by_owner"][carried_c_ids[0]] = 2
        training["C"]["failed_attempts"] = [{"unit_id": carried_c_ids[0], "failure": "new source retry"}]
        training["C"]["carried_attempt_count_by_owner"] = {unit_id: 1 for unit_id in carried_c_ids}
        updates = {arm: {"elapsed_seconds": 5.0} for arm in pilot_runner.NEW_ARMS}
        forecast = pilot_runner._forecast({
            "training_runs": training,
            "evaluation_runs": evaluation,
            "actor_updates": updates,
            "cumulative_active_wall_seconds": 3.0,
            "source_lock_sha256": "lock",
            "package_commit": "commit",
            "concurrency": 8,
            "actor_update_reuse": {
                "prior_C_failed_attempts": [{"unit_id": unit_id} for unit_id in carried_c_ids],
            },
        })

        self.assertEqual(forecast["status"], "PARTIAL_COMPONENT_PROJECTION_NOT_AN_ALL_IN_ESTIMATE_OR_CAP")
        self.assertEqual(forecast["formal_games"], 7808)
        self.assertEqual(forecast["pilot_training_games"], 80)
        self.assertEqual(forecast["pilot_evaluation_games"], 112)
        self.assertEqual(forecast["projected_registered_formal_games_and_actor_updates_active_wall_seconds"], 6960.0)
        self.assertEqual(forecast["pilot_plus_projected_games_and_actor_updates_active_wall_seconds"], 6963.0)
        self.assertIsNone(forecast["all_in_study_wall_estimate_seconds"])
        self.assertIn("independent Reviewer review and Planner disposition", forecast["unmeasured_required_work"])
        self.assertEqual(forecast["carried_prior_C_attempt_count"], 10)
        self.assertEqual(forecast["pilot_failed_attempt_count"], 11)
        self.assertEqual(forecast["pilot_total_attempt_count"], 203)

    def test_forecast_counts_prior_c_package_attempts_and_failures(self):
        training = {
            arm: {
                "status": "SUCCEEDED",
                "expected": 16,
                "successful_completions": 16,
                "elapsed_seconds": 10.0,
                "failed_attempts": [],
                "attempt_count_by_owner": {f"PILOT-TRAIN:{arm}:{owner}": 1 for owner in pilot_runner.PILOT_OWNERS},
            }
            for arm in pilot_runner.NEW_ARMS
        }
        carried_failures = [f"PILOT-TRAIN:C:{owner}" for owner in pilot_runner.PILOT_OWNERS[:10]]
        c_ids = [f"PILOT-TRAIN:C:{owner}" for owner in pilot_runner.PILOT_OWNERS]
        training["C"]["attempt_count_by_owner"] = {
            unit_id: (0 if owner_index < 4 else 1)
            for owner_index, unit_id in enumerate(c_ids)
        }
        c_prior_counts = {
            unit_id: (1 if owner_index < 8 else 0)
            for owner_index, unit_id in enumerate(c_ids)
        }
        carried_counts = {
            unit_id: c_prior_counts[unit_id] + (1 if unit_id in carried_failures else 0)
            for unit_id in c_ids
        }
        training["C"]["carried_attempt_count_by_owner"] = carried_counts
        evaluation = {
            policy: {
                "status": "SUCCEEDED",
                "expected": 16,
                "successful_completions": 16,
                "elapsed_seconds": 20.0,
                "failed_attempts": [],
                "attempt_count_by_owner": {f"PILOT-EVAL:{policy}:{owner}": 1 for owner in pilot_runner.PILOT_OWNERS},
            }
            for policy in pilot_runner.POLICIES
        }
        forecast = pilot_runner._forecast({
            "training_runs": training,
            "evaluation_runs": evaluation,
            "actor_updates": {arm: {"elapsed_seconds": 5.0} for arm in pilot_runner.NEW_ARMS},
            "cumulative_active_wall_seconds": 3.0,
            "source_lock_sha256": "lock",
            "package_commit": "commit",
            "concurrency": 8,
            "actor_update_reuse": {"prior_C_failed_attempts": [{"unit_id": unit_id} for unit_id in carried_failures]},
            "pilot_C_outcome_reuse": {
                "prior_C_attempt_count_by_owner": c_prior_counts,
                "prior_C_cumulative_attempt_count_by_owner": carried_counts,
                "prior_C_successful_games": 4,
                "prior_C_failed_attempt_count": 4,
            },
        })

        self.assertEqual(forecast["carried_prior_C_attempt_count"], 18)
        self.assertEqual(forecast["carried_prior_C_failed_attempt_count"], 14)
        self.assertEqual(forecast["carried_prior_C_successful_game_count"], 4)
        self.assertEqual(forecast["pilot_failed_attempt_count"], 14)
        self.assertEqual(forecast["pilot_total_attempt_count"], 206)


if __name__ == "__main__":
    unittest.main()
