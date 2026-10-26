"""Pure source-bound executor checks; these tests never construct a simulator."""
from __future__ import annotations

import os
from pathlib import Path
import json
import sys
import tempfile
import types
import unittest
from unittest import mock

STUDY = Path(__file__).resolve().parents[1]
STAGE1 = STUDY / "stage1"
sys.path.insert(0, str(STAGE1))
sys.path.insert(1, str(STUDY))

import executor


class FakeLedger:
    def __init__(self, *, unit_kind: str):
        self.binding = types.SimpleNamespace(
            package_commit="a" * 40,
            source_lock_sha256="b" * 64,
        )
        self.unit_kind = unit_kind

    def require_open_reservation(self, *, run_id: str, unit_id: str):
        return {
            "run_id": run_id,
            "unit_id": unit_id,
            "unit_kind": self.unit_kind,
            "previous_run_id": None,
            "checkpoint_sha256": None,
        }


class FakeReportLedger:
    def __init__(self):
        self.binding = types.SimpleNamespace(
            package_commit="a" * 40,
            source_lock_sha256="b" * 64,
        )
        self.events = []

    def read_events(self):
        return list(self.events)

    def reserve(self, **event):
        event = {
            "event": "reserve", "schema_id": "issue75-stage1-budget-event-v2",
            "package_commit": self.binding.package_commit,
            "source_lock_sha256": self.binding.source_lock_sha256, **event,
        }
        self.events.append(event)
        return dict(event)

    def finish(self, *, run_id: str, measured_cpu_seconds: float, status: str, checkpoint_sha256=None, result_sha256=None):
        reservation = next(event for event in self.events if event.get("event") == "reserve" and event["run_id"] == run_id)
        self.events.append({
            "event": "finish",
            "schema_id": "issue75-stage1-budget-event-v2",
            "run_id": run_id,
            "unit_id": reservation["unit_id"],
            "measured_cpu_seconds": measured_cpu_seconds,
            "status": status,
            "checkpoint_sha256": checkpoint_sha256,
            "result_sha256": result_sha256,
            "package_commit": self.binding.package_commit,
            "source_lock_sha256": self.binding.source_lock_sha256,
        })


class FakeReceiptLedger:
    def __init__(self):
        self.binding = types.SimpleNamespace(package_commit="a" * 40, source_lock_sha256="b" * 64)
        self.events = []

    def read_events(self):
        return list(self.events)

    def set_success(self, *, run_id, unit_id, result_sha256):
        self.events = [
            {"event": "reserve", "schema_id": "issue75-stage1-budget-event-v2",
             "run_id": run_id, "unit_id": unit_id, "role": "builder",
             "unit_kind": "owner_arm", "package_commit": self.binding.package_commit,
             "source_lock_sha256": self.binding.source_lock_sha256},
            {"event": "finish", "schema_id": "issue75-stage1-budget-event-v2",
             "run_id": run_id, "unit_id": unit_id, "status": "succeeded",
             "measured_cpu_seconds": 1.0, "reserved_cpu_seconds": 2.0,
             "package_commit": self.binding.package_commit,
             "source_lock_sha256": self.binding.source_lock_sha256,
             "result_sha256": result_sha256},
        ]


class PreflightTreatmentSemanticsTests(unittest.TestCase):
    def test_root_gap_does_not_fail_episode_with_a_valid_later_mutation(self):
        searches = [
            {"root_id": "gap-root", "root_index": 2, "status": "TREATMENT_NOT_DELIVERED", "mutation_attempts": []},
            {
                "root_id": "delivered-root", "root_index": 3, "status": "MUTATION_ATTEMPTED",
                "mutation_attempts": [{
                    "complete_terminal": True,
                    "alternative_differs_from_parent_action": True,
                    "legal_occurrence_count": 3,
                    "trajectory_decision_count": 4,
                }],
            },
        ]
        delivered, root_gaps = executor._preflight_treatment_diagnostics("PREFLIGHT:1:J", "J", searches)
        self.assertTrue(delivered)
        self.assertEqual(root_gaps, [{
            "unit_id": "PREFLIGHT:1:J",
            "root_id": "gap-root",
            "root_index": 2,
            "status": "TREATMENT_NOT_DELIVERED",
        }])

    def test_episode_treatment_requires_a_valid_terminal_later_mutation(self):
        searches = [{
            "root_id": "incomplete-root",
            "root_index": 0,
            "status": "MUTATION_ATTEMPTED",
            "mutation_attempts": [{
                "complete_terminal": False,
                "alternative_differs_from_parent_action": True,
                "legal_occurrence_count": 2,
                "trajectory_decision_count": 4,
            }],
        }]
        delivered, root_gaps = executor._preflight_treatment_diagnostics("PREFLIGHT:2:J", "J", searches)
        self.assertFalse(delivered)
        self.assertEqual(root_gaps, [])

class Stage1ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=STAGE1)
        self.addCleanup(self.temporary.cleanup)
        self.original_artifact_root = executor.ARTIFACT_ROOT
        executor.ARTIFACT_ROOT = Path(self.temporary.name) / "artifacts"
        self.addCleanup(setattr, executor, "ARTIFACT_ROOT", self.original_artifact_root)

    def _test_ledger(self, name="ledger"):
        binding = executor.budget.PackageBinding(
            package_commit="a" * 40,
            source_lock_sha256="b" * 64,
        )
        with mock.patch.object(
            executor.budget, "_current_package_binding",
            return_value=(binding.package_commit, binding.source_lock_sha256),
        ):
            return executor.budget.Stage1Ledger(executor.ARTIFACT_ROOT / name)

    def _reserve_test_unit(self, ledger, *, unit_id, run_id, cap=2.0, unit_kind="owner_arm", **metadata):
        return ledger.reserve(
            run_id=run_id,
            unit_id=unit_id,
            role="builder",
            reserved_cpu_seconds=cap,
            unit_kind=unit_kind,
            **metadata,
        )

    def test_write_once_accepts_crlf_checkout_without_rewriting_or_replacing_receipt(self):
        path = executor.ARTIFACT_ROOT / "crlf-receipt.json"
        value = {"status": "retained", "text": "unchanged"}
        canonical = executor._canonical(value) + b"\n"
        checked_out = canonical.replace(b"\n", b"\r\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(checked_out)

        self.assertEqual(executor._write_once(path, value), executor._sha256(canonical))
        self.assertEqual(path.read_bytes(), checked_out)
        with self.assertRaises(FileExistsError):
            executor._write_once(path, {**value, "status": "changed"})
        self.assertEqual(path.read_bytes(), checked_out)

    def test_paired_owner_differences_bind_all_96_owner_pairs(self):
        rows = []
        for owner in range(96):
            for arm in ("J", "P1"):
                rows.append({
                    "arm": arm,
                    "paired_owner_key": f"heldout-{owner}",
                    "terminal_floor": owner + (1 if arm == "J" else 0),
                })
        rows.reverse()
        self.assertEqual(executor._paired_differences(rows, "J", "P1", "terminal_floor"), [1.0] * 96)
        with self.assertRaises(PermissionError):
            executor._paired_differences(rows[:-1], "J", "P1", "terminal_floor")

    def test_bootstrap_is_deterministic_and_reports_nearest_rank_interval(self):
        seed = "c" * 64
        first = executor._bootstrap_ci_95([2.0] * 96, seed_sha256=seed, replicates=200)
        second = executor._bootstrap_ci_95([2.0] * 96, seed_sha256=seed, replicates=200)
        self.assertEqual(first, second)
        self.assertEqual((first["lower"], first["upper"]), (2.0, 2.0))
        self.assertEqual(first["replicates"], 200)
        formal_shape = executor._bootstrap_ci_95([2.0] * 96, seed_sha256=seed)
        self.assertEqual(formal_shape["replicates"], 20_000)
        self.assertEqual((formal_shape["lower"], formal_shape["upper"]), (2.0, 2.0))

    def test_search_coverage_counts_mutation_depth_and_actual_action_changes(self):
        outcomes = [{
            "arm": "J",
            "search": [{
                "status": "MUTATION_ATTEMPTED",
                "started_attempts": 3,
                "completed_trajectories": 2,
                "cpu_seconds": 4.5,
                "root_action_changed_from_sampled_policy": True,
                "mutation_attempts": [
                    {"parent_decision_index": 1, "complete_terminal": True},
                    {"parent_decision_index": 2, "complete_terminal": False},
                ],
            }],
            "resource": {"measured_process_tree_cpu_seconds": 5.5, "status": "succeeded"},
        }]
        summary = executor._search_coverage(outcomes)["J"]
        self.assertEqual(summary["root_opportunities_searched"], 1)
        self.assertEqual(summary["mutation_attempts_started"], 2)
        self.assertEqual(summary["mutation_terminal_trajectories"], 1)
        self.assertEqual(summary["mutation_at_or_after_second_future_choice"], 1)
        self.assertEqual(summary["root_actions_changed_from_sampled_policy"], 1)
        self.assertEqual(summary["unit_process_tree_cpu_seconds"], 5.5)

    def test_report_analysis_is_owner_paired_and_resumed_from_its_receipt(self):
        outcomes = []
        for owner in range(96):
            for arm_index, arm in enumerate(executor.ARMS):
                outcomes.append({
                    "unit_id": f"HELDOUT:{owner}:{arm}",
                    "owner_seed": owner,
                    "arm": arm,
                    "paired_owner_key": f"heldout-{owner}",
                    "terminal_floor": float(owner + arm_index),
                    "heart_victory": (owner + arm_index) % 7 == 0,
                    "search": [],
                    "resource": {"measured_process_tree_cpu_seconds": 0.25, "status": "succeeded"},
                })
        ledger = FakeReportLedger()
        first = executor._run_report_analysis(ledger, outcomes)
        second = executor._run_report_analysis(ledger, outcomes)
        self.assertEqual(first, second)
        self.assertEqual(sum(event["event"] == "reserve" for event in ledger.events), 1)
        self.assertEqual(first["analysis"]["paired_terminal_floor_contrasts"]["J-P1"]["n"], 96)
        self.assertEqual(
            first["analysis"]["paired_terminal_floor_contrasts"]["J-P1"]["terminal_floor_bootstrap_ci_95"]["replicates"],
            20_000,
        )
        self.assertEqual(first["analysis"]["search_coverage_and_resource_summary"]["P0"]["owner_rows"], 96)

    def test_registered_rows_are_disjoint_and_bind_the_five_arm_policy_sources(self):
        rows = executor._rows()
        by_phase = {
            phase: [row for row in rows if row["phase"] == phase]
            for phase in ("preflight", "training", "heldout")
        }
        self.assertEqual(tuple(map(len, by_phase.values())), (40, 512, 480))
        seed_sets = {
            phase: {int(row["owner_seed"]) for row in phase_rows}
            for phase, phase_rows in by_phase.items()
        }
        self.assertTrue(seed_sets["preflight"].isdisjoint(seed_sets["training"]))
        self.assertTrue(seed_sets["preflight"].isdisjoint(seed_sets["heldout"]))
        self.assertTrue(seed_sets["training"].isdisjoint(seed_sets["heldout"]))
        preflight = {row["arm"]: row for row in by_phase["preflight"] if row["owner_seed"] == 7520001}
        self.assertEqual(set(preflight), set(executor.ARMS))
        self.assertEqual(executor._arm_policy("preflight", "P0", preflight["P0"]), ("pi0", False))
        for arm in ("P1", "R", "J"):
            self.assertEqual(executor._arm_policy("preflight", arm, preflight[arm]), ("pi-stage0-proxy", True))
        self.assertEqual(executor._arm_policy("preflight", "E", preflight["E"]), ("pi0", False))
        formal = {row["arm"]: row for row in by_phase["heldout"] if row["owner_seed"] == 7510001}
        self.assertEqual(executor._arm_policy("heldout", "P0", formal["P0"]), ("pi0", False))
        for arm in ("P1", "R", "J"):
            self.assertEqual(executor._arm_policy("heldout", arm, formal[arm]), ("pi4", False))
        for block in range(4):
            row = next(row for row in by_phase["training"] if row["block_index"] == block)
            self.assertEqual(executor._arm_policy("training", "TRAIN", row), (f"pi{block}", False))

    def test_preflight_proxy_load_checks_both_artifact_and_model_digest(self):
        try:
            model, digest, source = executor._load_model("pi-stage0-proxy", proxy=True)
        except ModuleNotFoundError as exc:
            if exc.name in {"numpy", "torch"}:
                self.skipTest(f"pinned policy runtime dependency unavailable: {exc.name}")
            raise
        self.assertIsNotNone(model)
        self.assertEqual(digest, "5d93c1c52582f9c493edda874764f14d8558d718088d3ca5886231a5c5b6688a")
        self.assertEqual(source["source"], "P1_PREFLIGHT_PROXY_ONLY")
        self.assertEqual(source["artifact_sha256"], "e4eea4fa34131dab2e697dbf59897734ce81dc4713a08c3a55cd89dec10c1416")
        self.assertFalse(source["formal_use"])
        proxy_path = STAGE1 / "preflight-proxy.json"
        original_read_json = executor._read_json

        def altered_proxy(path):
            value = original_read_json(path)
            if Path(path) == proxy_path:
                value["policy_model_digest"] = "0" * 64
            return value

        with mock.patch.object(executor, "_read_json", side_effect=altered_proxy):
            with self.assertRaises(PermissionError):
                executor._load_model("pi-stage0-proxy", proxy=True)

    def test_pi0_rejects_existing_checkpoint_with_wrong_registered_digest(self):
        try:
            import torch
            from model import ActionConditionedPolicy
        except ModuleNotFoundError as exc:
            if exc.name in {"numpy", "torch"}:
                self.skipTest(f"pinned policy runtime dependency unavailable: {exc.name}")
            raise
        config = executor._read_json(STUDY / "config.json")
        model = ActionConditionedPolicy(
            dimension=executor.POLICY_DIMENSION,
            seed=int(config["policy"]["initial_model_seed"]),
            final_weight_std=float(config["policy"]["actor_final_weight_std"]),
        ).eval()
        state = model.state_dict()
        first = next(iter(state))
        altered = state[first].clone()
        altered.reshape(-1)[0] += 0.01
        state[first] = altered
        path = executor._model_path("pi0")
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(state, path)
        with self.assertRaisesRegex(PermissionError, "registered initial-model digest"):
            executor._load_model("pi0")

    def test_reused_successful_receipts_bind_registered_row_and_original_bytes(self):
        ledger = FakeReceiptLedger()
        row = next(item for item in executor._unit_rows("preflight") if item["arm"] == "E")
        unit_id = str(row["unit_id"])
        run_id = unit_id + "-" + "f" * 16
        source = executor._read_json(executor.STAGE1 / "config.json")["source"]["registered_stage0_config"]
        digest, policy_source = executor._expected_policy_binding(ledger, "preflight", row)
        result = {
            "schema_id": executor.UNIT_SCHEMA,
            "status": "SUCCEEDED",
            "package_commit": ledger.binding.package_commit,
            "source_lock_sha256": ledger.binding.source_lock_sha256,
            "study_id": executor.STUDY_ID,
            "unit_id": unit_id,
            "phase": "preflight",
            "formal": False,
            "owner_seed": row["owner_seed"],
            "game_seed": row["game_seed"],
            "arm": "E",
            "paired_owner_key": row["paired_owner_key"],
            "terminal_outcome": "LOSS",
            "terminal_floor": 0,
            "terminal_act": 1,
            "heart_victory": False,
            "act2_plus": False,
            "act3_plus": False,
            "act4_reached": False,
            "completed_transitions": 1,
            "native_source_commit": source["sts_lightspeed_commit"],
            "stsr_source_commit": source["stsr_source_commit"],
            "native_module_sha256": source["native_module_sha256"],
            "model_digest": digest,
            "policy_source": policy_source,
            "expert_decisions": 1,
            "search": [],
            "training_features": None,
            "resource": {"ledger_run_id": run_id},
        }
        path = executor._unit_directory(unit_id) / "result.json"
        executor._write_once(path, result)
        ledger.set_success(run_id=run_id, unit_id=unit_id, result_sha256=executor._file_sha256(path))
        self.assertEqual(executor._existing_unit_receipt(ledger, unit_id), result)

        edited = dict(result)
        edited["terminal_floor"] = 999
        path.write_bytes(executor._canonical(edited) + b"\n")
        with self.assertRaisesRegex(PermissionError, "immutable ledger finish"):
            executor._existing_unit_receipt(ledger, unit_id)

        tampered = dict(result)
        tampered["owner_seed"] = int(row["owner_seed"]) + 1
        path.write_bytes(executor._canonical(tampered) + b"\n")
        ledger.set_success(run_id=run_id, unit_id=unit_id, result_sha256=executor._file_sha256(path))
        with self.assertRaisesRegex(PermissionError, "exact registered row"):
            executor._existing_unit_receipt(ledger, unit_id)

        for field, value in (("arm", "J"), ("package_commit", "c" * 40),
                             ("source_lock_sha256", "d" * 64), ("model_digest", "e" * 64)):
            with self.subTest(field=field):
                tampered = dict(result)
                tampered[field] = value
                path.write_bytes(executor._canonical(tampered) + b"\n")
                ledger.set_success(run_id=run_id, unit_id=unit_id, result_sha256=executor._file_sha256(path))
                with self.assertRaises(PermissionError):
                    executor._existing_unit_receipt(ledger, unit_id)

    def test_expert_is_constructed_only_for_the_isolated_e_arm(self):
        row = next(row for row in executor._rows() if row["phase"] == "preflight" and row["arm"] == "E")
        output_dir = executor.ARTIFACT_ROOT / "expert-output"
        config = executor._read_json(STUDY / "config.json")

        class ExpertOnlyRunner:
            def _run_one(self, **kwargs):
                self_test = kwargs
                if self_test["arm"] != "E" or self_test["model"] is not None:
                    raise AssertionError("Expert must be isolated from learned-policy actions")
                return {
                    "terminal": {
                        "outcome": "heart_win", "terminal_floor": 4, "terminal_act": 4,
                        "heart_victory": True, "act2_plus": True, "act3_plus": True, "act4_reached": True,
                    },
                    "completed_transitions": 12,
                    "expert_decisions": 1,
                    "cpu_seconds": 0.0,
                    "wall_seconds": 0.0,
                }

        fake_runtime = types.SimpleNamespace(
            initialize_native_runtime=lambda *args, **kwargs: (config, ExpertOnlyRunner(), object(), object()),
        )
        job = {"row": row, "phase": "preflight", "output_dir": str(output_dir), "run_id": row["unit_id"] + "-" + "f" * 16}
        with mock.patch.dict(sys.modules, {"runtime": fake_runtime}):
            result = executor._worker_owner(job, FakeLedger(unit_kind="owner_arm"))
        self.assertEqual(result["policy_source"]["source"], "isolated_expert_reference")
        self.assertEqual(result["expert_decisions"], 1)
        self.assertEqual(result["policy_source"]["formal_use"], True)

    def test_cli_dispatches_authorized_formal_phase_without_running_it_in_the_test(self):
        import contextlib
        import io

        report = {
            "status": "TRAINING_COMPLETE",
            "package_commit": "a" * 40,
            "source_lock_sha256": "b" * 64,
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(executor, "run_phase", return_value=report) as run_phase,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = executor.main(["--phase", "train"])
        self.assertEqual(status, 0)
        run_phase.assert_called_once_with("train", output_dir=executor.ARTIFACT_ROOT)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "TRAINING_COMPLETE")
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(executor.ARTIFACT_ROOT.exists())

    def test_preflight_resume_propagates_blocked_forecast_and_cli_exit(self):
        import contextlib
        import io

        executor.ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        report = {
            "status": "PREFLIGHT_COMPLETE_PENDING_INDEPENDENT_REVIEW",
            "package_commit": "a" * 40,
            "source_lock_sha256": "b" * 64,
        }
        forecast = {"status": "RESOURCE_BLOCKED", "reason": "full-cohort forecast exceeds remaining cap"}
        executor._write_once(executor.ARTIFACT_ROOT / "preflight-report.json", report)
        executor._write_once(executor.ARTIFACT_ROOT / "cost-forecast.json", forecast)

        resumed = executor._run_preflight(self._test_ledger())
        self.assertEqual(resumed["status"], "RESOURCE_BLOCKED")
        self.assertEqual(resumed["report"], report)
        self.assertEqual(resumed["cost_forecast"], forecast)

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(executor, "run_phase", return_value=resumed),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = executor.main(["--phase", "preflight"])
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        cli_report = json.loads(stderr.getvalue())
        self.assertEqual(cli_report["status"], "RESOURCE_BLOCKED")
        self.assertEqual(cli_report["phase"], "preflight")

    def test_phase_rejects_a_missing_source_binding_before_creating_artifacts(self):
        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "verify_stage1_phase"),
            mock.patch.object(
                executor.budget,
                "open_stage1_ledger",
                side_effect=PermissionError("package source binding unavailable"),
            ) as open_ledger,
        ):
            with self.assertRaises(PermissionError):
                executor.run_phase("preflight", output_dir=executor.ARTIFACT_ROOT)
        open_ledger.assert_called_once()
        self.assertFalse(executor.ARTIFACT_ROOT.exists())

    def test_training_runs_all_128_owner_rows_before_each_block_update(self):
        order = []
        ledger = FakeLedger(unit_kind="owner_arm")
        update_receipts = {}

        def run_owner_batch(_ledger, rows, *, phase, estimates):
            self.assertEqual(phase, "train")
            self.assertEqual(len(rows), 1)
            row = rows[0]
            order.append(("owner", int(row["block_index"]), int(row["owner_seed"])))
            return [{"status": "SUCCEEDED", "unit_id": row["unit_id"]}]

        def update(_ledger, block, *, estimates):
            order.append(("update", block, None))
            path = executor.ARTIFACT_ROOT / "units" / f"UPDATE-{block}" / "result.json"
            receipt = {
                "status": "SUCCEEDED", "unit_id": f"UPDATE:{block}",
                "model_checkpoint": {"block_index": block},
            }
            executor._write_once(path, receipt)
            update_receipts[f"UPDATE:{block}"] = receipt
            return receipt

        def existing_receipt(_ledger, unit_id):
            return update_receipts.get(unit_id)

        with (
            mock.patch.object(executor, "_formal_forecast", return_value={
                "preflight_report_sha256": "1" * 64, "cost_forecast_sha256": "2" * 64,
            }),
            mock.patch.object(executor, "_forecast_estimates", return_value=({}, {})),
            mock.patch.object(executor, "_formal_concurrency", return_value=1),
            mock.patch.object(executor, "_run_scheduler_reservation", return_value={
                "run_id": "scheduler-test", "unit_id": "SCHEDULER:TRAIN",
                "cap": executor.FORMAL_SCHEDULER_RESERVATION,
            }) as scheduler_reservation,
            mock.patch.object(executor, "_finish_scheduler_reservation") as finish_scheduler,
            mock.patch.object(executor, "_existing_unit_receipt", side_effect=existing_receipt),
            mock.patch.object(executor, "_run_owner_batch", side_effect=run_owner_batch),
            mock.patch.object(executor, "_all_units_succeeded", return_value=True),
            mock.patch.object(executor, "_run_update_unit", side_effect=update),
            mock.patch.object(executor, "_load_model", return_value=(None, "3" * 64, {"source": "pi4"})),
        ):
            report = executor._run_training(ledger)
        self.assertEqual(report["status"], "TRAINING_COMPLETE")
        self.assertEqual(report["formal_owner_count"], 512)
        self.assertEqual(len(report["training_seed_ids"]), 512)
        scheduler_reservation.assert_called_once()
        finish_scheduler.assert_called_once()
        self.assertEqual([event for event in order if event[0] == "update"], [
            ("update", 0, None), ("update", 1, None),
            ("update", 2, None), ("update", 3, None),
        ])
        for block in range(4):
            start = block * 129
            block_events = order[start:start + 129]
            self.assertEqual(len([event for event in block_events if event[0] == "owner"]), 128)
            self.assertEqual(block_events[-1], ("update", block, None))

    def test_scheduler_reservations_preserve_the_accepted_full_cohort_forecast(self):
        runs = {
            "preflight": {
                "reservation": {"unit_id": "PREFLIGHT:7520001:P0", "role": "builder"},
                "finish": {"status": "succeeded", "measured_cpu_seconds": 8077.011297},
            },
        }
        with (
            mock.patch.object(executor, "_event_state", return_value=(runs, {})),
            mock.patch.object(executor.budget, "_charge", return_value=8077.011297),
        ):
            estimates, summary = executor._forecast_estimates(None)
        self.assertEqual(len(estimates), 999)
        self.assertEqual(summary["formal_unit_count"], 996)
        self.assertEqual(summary["scheduler_unit_count"], 2)
        self.assertEqual(estimates["SCHEDULER:TRAIN"], 120.0)
        self.assertEqual(estimates["SCHEDULER:EVALUATE"], 120.0)
        self.assertAlmostEqual(sum(estimates.values()), 161896.17019694686, places=6)
        self.assertAlmostEqual(summary["projected_total_builder_cpu_seconds"], 169973.18149394685, places=6)
        self.assertEqual(summary["builder_hard_cap_cpu_seconds"], 170000)
        self.assertEqual(summary["reviewer_protected_cpu_seconds"], 10000)
        self.assertEqual(summary["aggregate_hard_cap_cpu_seconds"], 180000)

    def test_heldout_phase_requires_a_successful_training_scheduler_receipt(self):
        ledger = FakeLedger(unit_kind="owner_arm")
        with (
            mock.patch.object(
                executor, "_existing_scheduler_receipt",
                side_effect=executor.budget.ResourceBlocked("training scheduler receipt missing"),
            ) as scheduler_receipt,
            mock.patch.object(executor, "_formal_forecast") as formal_forecast,
        ):
            with self.assertRaises(executor.budget.ResourceBlocked):
                executor._run_evaluation(ledger)
        scheduler_receipt.assert_called_once_with(ledger, "SCHEDULER:TRAIN")
        formal_forecast.assert_not_called()
        self.assertFalse((executor.ARTIFACT_ROOT / "evaluation-report.json").exists())

    def test_supervisor_validation_failures_still_account_controller_cpu(self):
        import contextlib
        import io

        stdout = io.StringIO()
        job_path = executor.ARTIFACT_ROOT / "invalid-supervisor-job.json"
        with (
            mock.patch.object(executor, "_supervisor_activity_lease", return_value=contextlib.nullcontext()),
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor, "_read_json", return_value={"schema_id": "wrong"}),
            mock.patch.object(executor.time, "process_time", side_effect=[5.0, 5.25]),
            contextlib.redirect_stdout(stdout),
        ):
            status = executor._supervisor_main(job_path)
        self.assertEqual(status, 2)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "SUPERVISOR_FAILED")
        self.assertEqual(report["measured_cpu_seconds"], 0.25)
        self.assertEqual(report["boundary"], "PermissionError")

    @unittest.skipIf(sys.platform == "win32", "the Stage 1 watchdog intentionally uses Linux /proc")
    def test_watchdog_accounts_for_nested_child_cpu(self):
        grandchild = "sum(i * i for i in range(8000000))"
        child = f"import subprocess,sys; subprocess.run([sys.executable,'-B','-c',{grandchild!r}],check=True)"
        temporary_paths = [executor.ARTIFACT_ROOT / name for name in ("tmp", "cache", "pycache")]
        for path in temporary_paths:
            path.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({
            "TMPDIR": str(temporary_paths[0]), "TMP": str(temporary_paths[0]),
            "TEMP": str(temporary_paths[0]), "XDG_CACHE_HOME": str(temporary_paths[1]),
            "PYTHONPYCACHEPREFIX": str(temporary_paths[2]),
        })
        output = executor.ARTIFACT_ROOT / "nested-watchdog.log"
        run = executor.budget.ProcessTreeWatchdog(poll_seconds=0.002).run(
            [sys.executable, "-B", "-c", child],
            reserved_cpu_seconds=5.0,
            cwd=STUDY,
            env=env,
            stdout_path=output,
        )
        self.assertEqual(run.returncode, 0)
        self.assertGreater(run.cpu_seconds, 0.05)
        self.assertGreaterEqual(run.controller_cpu_seconds, 0.0)

    @unittest.skipIf(sys.platform == "win32", "the Stage 1 ledger uses Linux file locks")
    def test_real_ledger_enforces_budget_and_exact_checkpoint_resume(self):
        ledger = self._test_ledger()
        first = "TRAIN:7500001-" + "a" * 16
        digest = "c" * 64
        self._reserve_test_unit(ledger, unit_id="TRAIN:7500001", run_id=first, cap=2.0, unit_kind="training_update")
        ledger.finish(
            run_id=first, measured_cpu_seconds=0.5, status="incomplete", checkpoint_sha256=digest,
        )
        with self.assertRaises(executor.budget.CheckpointMismatch):
            self._reserve_test_unit(
                ledger, unit_id="TRAIN:7500001", run_id="TRAIN:7500001-" + "b" * 16,
                cap=2.0, unit_kind="training_update", previous_run_id="wrong", checkpoint_sha256=digest,
            )
        resumed = "TRAIN:7500001-" + "b" * 16
        self._reserve_test_unit(
            ledger, unit_id="TRAIN:7500001", run_id=resumed, cap=2.0,
            unit_kind="training_update", previous_run_id=first, checkpoint_sha256=digest,
        )
        ledger.finish(run_id=resumed, measured_cpu_seconds=0.25, status="succeeded", result_sha256="d" * 64)
        with self.assertRaises(executor.budget.DuplicateRun):
            self._reserve_test_unit(ledger, unit_id="TRAIN:7500001", run_id="TRAIN:7500001-" + "d" * 16)

        capped = self._test_ledger("capped-ledger")
        self._reserve_test_unit(
            capped, unit_id="CAP:1", run_id="CAP:1-" + "e" * 16,
            cap=executor.budget.BUILDER_LIMIT,
        )
        with self.assertRaises(executor.budget.ResourceBlocked):
            self._reserve_test_unit(capped, unit_id="CAP:2", run_id="CAP:2-" + "f" * 16, cap=0.1)

    def test_native_runtime_uses_stage1_binding_without_stage0_gate(self):
        import preflight
        import runtime

        config = executor._read_json(STUDY / "config.json")
        native_path = STUDY / "runtime.py"
        fake_module = types.ModuleType("fake_native")
        fake_module.__file__ = str(native_path)
        env = {"identity": {"native_source_commit": config["source"]["sts_lightspeed_commit"]}}
        fake_runner = type("FakeRunner", (), {
            "_setup": lambda self, output_dir, *, native_module_path: (
                env, fake_module, object(), {"runtime": "test"},
            ),
        })()
        receipt = executor.budget.PackageBinding(
            package_commit="a" * 40,
            source_lock_sha256="b" * 64,
        )
        with tempfile.TemporaryDirectory(dir=STUDY) as temp_dir:
            with (
                mock.patch.object(executor.budget, "validate_worker_package_binding") as validate_receipt,
                mock.patch.object(runtime, "verify_source_repositories"),
                mock.patch.object(runtime, "verify_native_module", return_value=native_path),
                mock.patch.object(runtime, "binary_sha256", return_value=config["source"]["native_module_sha256"]),
                mock.patch.object(runtime, "load_issue73_runner", return_value=fake_runner),
                mock.patch.object(preflight, "reviewer_pass_proof", side_effect=AssertionError("Stage-0 gate used")),
            ):
                result = runtime.initialize_native_runtime(
                    Path(temp_dir) / "runtime", sampling=True, stage1_binding=receipt,
                )
        validate_receipt.assert_called_once_with(receipt)
        self.assertEqual(result[0], config)

    def test_worker_accepts_only_the_exact_registered_owner_row(self):
        row = next(item for item in executor._rows() if item["phase"] == "preflight")
        unit_id = row["unit_id"]
        run_id = unit_id + "-" + "d" * 16
        ledger = FakeLedger(unit_kind="owner_arm")
        unit_directory = executor._unit_directory(unit_id)
        job_path = unit_directory / f"worker-job-{run_id}.json"
        job_path.parent.mkdir(parents=True)
        job = {
            "schema_id": executor.WORKER_SCHEMA,
            "package_commit": ledger.binding.package_commit,
            "source_lock_sha256": ledger.binding.source_lock_sha256,
            "run_id": run_id,
            "unit_id": unit_id,
            "phase": "preflight",
            "row": row,
            "output_dir": str(unit_directory / "native"),
            "result_path": str(unit_directory / "result.json"),
            "previous_run_id": None,
            "checkpoint_sha256": None,
        }
        job_path.write_text(json.dumps(job), encoding="utf-8")
        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "open_reserved_stage1_worker_ledger", return_value=ledger),
            mock.patch.object(executor, "_worker_owner", return_value={"status": "SUCCEEDED", "unit_id": unit_id}) as worker,
        ):
            self.assertEqual(executor._worker_main(job_path), 0)
            worker.assert_called_once()
        self.assertTrue((unit_directory / "result.json").is_file())

        job["row"] = {**row, "owner_seed": int(row["owner_seed"]) + 1}
        job_path.write_text(json.dumps(job), encoding="utf-8")
        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "open_reserved_stage1_worker_ledger", return_value=ledger),
            mock.patch.object(executor, "_worker_owner") as worker,
        ):
            with self.assertRaises(PermissionError):
                executor._worker_main(job_path)
            worker.assert_not_called()

    def test_update_worker_is_bound_to_its_exact_block_and_result_path(self):
        unit_id = "UPDATE:0"
        run_id = unit_id + "-" + "e" * 16
        ledger = FakeLedger(unit_kind="training_update")
        unit_directory = executor._unit_directory(unit_id)
        job_path = unit_directory / f"worker-job-{run_id}.json"
        job_path.parent.mkdir(parents=True)
        result_path = executor._inside_artifacts(
            executor.ARTIFACT_ROOT / "units" / "UPDATE-0" / f"result-{run_id}.json"
        )
        job = {
            "schema_id": executor.WORKER_SCHEMA,
            "package_commit": ledger.binding.package_commit,
            "source_lock_sha256": ledger.binding.source_lock_sha256,
            "run_id": run_id,
            "unit_id": unit_id,
            "phase": "update",
            "block_index": 0,
            "previous_run_id": None,
            "checkpoint_sha256": None,
            "result_path": str(result_path),
        }
        job_path.write_text(json.dumps(job), encoding="utf-8")
        def fake_worker_update(worker_job, _ledger):
            result = {"status": "SUCCEEDED", "unit_id": unit_id}
            executor._write_once(Path(worker_job["result_path"]), result)
            return result

        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "verify_stage1_phase"),
            mock.patch.object(executor.budget, "open_reserved_stage1_worker_ledger", return_value=ledger),
            mock.patch.object(executor, "_worker_update", side_effect=fake_worker_update) as worker,
        ):
            self.assertEqual(executor._worker_main(job_path), 0)
            worker.assert_called_once()
        self.assertTrue(result_path.is_file())

        job["block_index"] = 1
        job_path.write_text(json.dumps(job), encoding="utf-8")
        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "verify_stage1_phase"),
            mock.patch.object(executor.budget, "open_reserved_stage1_worker_ledger", return_value=ledger),
            mock.patch.object(executor, "_worker_update") as worker,
        ):
            with self.assertRaises(PermissionError):
                executor._worker_main(job_path)
            worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
