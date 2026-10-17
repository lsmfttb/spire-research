from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from prospective_cost_regime import CostRegimeError
from bounded_batch02_sampling import (
    LOCAL_LIMIT,
    TARGET_SEEDS,
    _batch02_seed_schedule,
    _ensure_sampling_runtime_hooks,
    _authorization_charges,
    _interruption_budget_hold,
    _invalidate_batch02_forecast,
    _attempt_number,
    _r2_source_compatibility,
    _remaining_local_budget,
    _settled_source_rollover,
)


class BoundedBatch02SamplingTests(unittest.TestCase):
    def test_existing_dispatcher_is_not_wrapped_again(self) -> None:
        calls = []
        already_bound = SimpleNamespace(
            RUN_STUDY=object(), _install_runtime_hooks=lambda: calls.append("install"),
        )
        _ensure_sampling_runtime_hooks(already_bound, object(), dispatcher_exists=True)
        self.assertEqual(calls, [])

        unbound = SimpleNamespace(
            RUN_STUDY=None, _install_runtime_hooks=lambda: calls.append("install"),
        )
        run_study = object()
        _ensure_sampling_runtime_hooks(unbound, run_study, dispatcher_exists=False)
        self.assertIs(unbound.RUN_STUDY, run_study)
        self.assertEqual(calls, ["install"])
        _ensure_sampling_runtime_hooks(unbound, run_study, dispatcher_exists=True)
        self.assertEqual(calls, ["install"])

        inconsistent = SimpleNamespace(
            RUN_STUDY=None, _install_runtime_hooks=lambda: calls.append("unexpected"),
        )
        with self.assertRaisesRegex(CostRegimeError, "dispatcher exists without"):
            _ensure_sampling_runtime_hooks(inconsistent, run_study, dispatcher_exists=True)
        self.assertEqual(calls, ["install"])

    def test_dispatch_slice_is_exactly_the_authorized_original_keys(self) -> None:
        schedule = _batch02_seed_schedule()
        self.assertEqual(schedule[:128], tuple(range(660001, 660129)))
        self.assertEqual(schedule[128:256], TARGET_SEEDS)
        self.assertEqual(TARGET_SEEDS, tuple(range(660243, 660257)))
        self.assertNotIn(660239, schedule[128:256])
        self.assertNotIn(660241, schedule[128:256])

    def test_local_cpu_limit_keeps_setup_and_finalization_reserves_inside_cap(self) -> None:
        remaining = _remaining_local_budget(charged=200.0, setup=25.0, finalization_reserve=30.0)
        self.assertEqual(remaining, LOCAL_LIMIT - 255.0)
        self.assertLessEqual(200.0 + 25.0 + remaining + 30.0, LOCAL_LIMIT)
        self.assertLessEqual(_remaining_local_budget(charged=1950.0, setup=20.0, finalization_reserve=30.0), 0.0)

    def test_interrupted_attempt_reserves_all_remaining_local_authority(self) -> None:
        import bounded_batch02_sampling as sampling

        attempts = [
            {"status": "FAILED", "authorized_aggregate_cpu_seconds": 30.0},
            {"status": "INTERRUPTED", "attempt_id": "attempt-0006",
             "authorized_aggregate_cpu_seconds": None},
        ]
        hold = _interruption_budget_hold(
            code_and_check_reserve=200.0, attempt_rows=attempts, explicit_hold=0.0,
        )
        self.assertEqual(hold, LOCAL_LIMIT - 230.0)

        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "receipt.json"
            receipt.write_text(json.dumps({
                "authorization_comment_id": sampling.AUTHORIZATION,
                "status": "INTERRUPTED",
                "attempts": attempts,
            }), encoding="utf-8")
            with mock.patch.object(sampling, "RECEIPT", receipt):
                with self.assertRaisesRegex(CostRegimeError, "interrupted"):
                    _attempt_number()

    def test_authorization_charges_include_explicit_unmeasured_budget_hold(self) -> None:
        import bounded_batch02_sampling as sampling

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cpu-ledger.jsonl").write_text("\n".join([
                json.dumps({
                    "authorization_comment_id": sampling.AUTHORIZATION,
                    "stage": "issue60-batch02-sampling-code-check-reserve",
                    "process_cpu_seconds": 200.0,
                }),
                json.dumps({
                    "authorization_comment_id": sampling.AUTHORIZATION,
                    "stage": sampling.INTERRUPTION_RESERVE_STAGE,
                    "process_cpu_seconds": LOCAL_LIMIT - 230.0,
                    "measurement": "CONSERVATIVE_RESERVATION_NOT_MEASUREMENT",
                }),
            ]), encoding="utf-8")
            with (mock.patch.object(sampling, "EVIDENCE", root),
                  mock.patch.object(sampling, "ATTEMPT_ID", ""),
                  mock.patch.object(sampling, "EXECUTION", {"attempts": [
                      {"status": "FAILED", "authorized_aggregate_cpu_seconds": 30.0},
                      {"status": "INTERRUPTED", "attempt_id": "attempt-0006",
                       "authorized_aggregate_cpu_seconds": None},
                  ]})):
                self.assertEqual(_authorization_charges(), LOCAL_LIMIT)

    def test_forecast_invalidation_preserves_stale_record_and_binds_current_index(self) -> None:
        import bounded_batch02_sampling as sampling

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prospective = root / "prospective-cost"
            prospective.mkdir()
            index = prospective / "cost-index.json"
            index.write_text(json.dumps({"tag_count": 65}), encoding="utf-8")
            forecast_path = prospective / "batch02-sampling-forecast.json"
            stale = json.dumps({"status": "UPDATED_COST_FORECAST", "forecast": {"status": "PASS"}})
            forecast_path.write_text(stale, encoding="utf-8")
            with (mock.patch.object(sampling, "HERE", root),
                  mock.patch.object(sampling, "PROSPECTIVE", prospective),
                  mock.patch.object(sampling, "FORECAST_INDEX", index),
                  mock.patch.object(sampling, "FORECAST_PATH", forecast_path)):
                record = _invalidate_batch02_forecast(
                    status="INVALIDATED_UNRECONCILED_CPU",
                    reason="parallel stage receipt is unavailable",
                    attempt_id="attempt-0006",
                )
            preserved = prospective / "history" / "batch02-sampling-forecast-before-unsettled-attempt.json"
            current = json.loads(forecast_path.read_text(encoding="utf-8"))
            self.assertEqual(preserved.read_text(encoding="utf-8"), stale)
            self.assertEqual(current["status"], "INVALIDATED_UNRECONCILED_CPU")
            self.assertIsNone(current["forecast"])
            self.assertEqual(current["cached_cost_sample_count"], 65)
            self.assertEqual(current["cached_cost_sample_sha256"], sampling._sha256(index))
            self.assertEqual(current["source_forecast_sha256"], sampling._sha256(preserved))
            self.assertEqual(record["attempt_id"], "attempt-0006")

    def test_r2_seal_accepts_only_the_committed_offline_test_descendant(self) -> None:
        import resource_amendment_r2
        import run_cost_bridge as bridge

        head = bridge.git(Path(__file__).resolve().parent, "rev-parse", "HEAD")
        witness = _r2_source_compatibility(resource_amendment_r2, head)
        self.assertEqual(witness["status"], "PASS")
        self.assertTrue(witness["post_seal_offline_test_only_change"])
        self.assertTrue(witness["runtime_source_files_unchanged"])
        self.assertEqual(
            witness["changed_paths_since_r2_seal"],
            {"test_resource_amendment_r2.py": "M"},
        )

    def test_source_rollover_requires_only_no_dispatch_attempts(self) -> None:
        import bounded_batch02_sampling as sampling

        expected_hashes = {660243: "a" * 64, 660244: "b" * 64}
        previous = {
            "status": "FAILED",
            "executing_head": "old-head",
            "launcher_sha256": "old-launcher",
            "attempts": [{
                "attempt_id": "attempt-1",
                "status": "FAILED_BEFORE_DISPATCH",
                "stage_receipt_path": "missing-stage.json",
                "completed_games": [],
                "complete_keys": [],
                "pre_dispatch_failure_recovery": {
                    "preserved_original_incomplete_attempts": [
                        {"game_seed": seed, "status_json_sha256": digest}
                        for seed, digest in expected_hashes.items()
                    ],
                },
            }],
        }
        target_state = {"complete": [], "keys": [
            {"game_seed": seed, "attempts": [{
                "status": "INCOMPLETE", "run_json_exists": False,
                "status_json_sha256": digest,
            }]}
            for seed, digest in expected_hashes.items()
        ] + [{"game_seed": seed, "attempts": []} for seed in TARGET_SEEDS if seed not in expected_hashes]}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sampling, "HERE", Path(tmp)):
            rollover = sampling._no_dispatch_source_rollover(
                previous, target_state, head="new-head", launcher_sha256="new-launcher",
            )
        self.assertEqual(rollover["new_dispatches_before_rollover"], 0)
        self.assertEqual(rollover["preserved_attempt_ids"], ["attempt-1"])

        previous["attempts"][0]["status"] = "RESOURCE_BLOCKED"
        previous["status"] = "RESOURCE_BLOCKED"
        previous["attempts"][0]["stage_receipt_path"] = "blocked-stage.json"
        previous["attempts"][0]["parallel_stage_receipt"] = {
            "execution_count": 0, "worker_process_cpu_seconds": 0.0,
        }
        previous["attempts"][0].pop("pre_dispatch_failure_recovery", None)
        previous["attempts"][0]["pre_dispatch_failure_recovery"] = {
            "preserved_original_incomplete_attempts": [
                {"game_seed": seed, "status_json_sha256": digest}
                for seed, digest in expected_hashes.items()
            ],
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sampling, "HERE", Path(tmp)):
            (Path(tmp) / "blocked-stage.json").write_text(
                '{"execution_count":0,"executions":[]}', encoding="utf-8",
            )
            rollover = sampling._no_dispatch_source_rollover(
                previous, target_state, head="new-head", launcher_sha256="new-launcher",
            )
        self.assertEqual(rollover["new_dispatches_before_rollover"], 0)

        previous["attempts"][0]["parallel_stage_receipt"]["worker_process_cpu_seconds"] = 0.5
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sampling, "HERE", Path(tmp)):
            (Path(tmp) / "blocked-stage.json").write_text(
                '{"execution_count":0,"executions":[]}', encoding="utf-8",
            )
            with self.assertRaises(CostRegimeError):
                sampling._no_dispatch_source_rollover(
                    previous, target_state, head="new-head", launcher_sha256="new-launcher",
                )

    def test_settled_partial_source_rollover_reconciles_only_exact_completed_artifacts(self) -> None:
        import json
        import bounded_batch02_sampling as sampling

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "evidence/runs/training/block1/batch-02"
            original_status_hashes = {}
            target_rows = []
            for seed in (660243, 660244):
                original = runs / f"seed-{seed}" / "attempt-0001"
                original.mkdir(parents=True)
                status_path = original / "status.json"
                status_path.write_text(json.dumps({"game_seed": seed, "status": "INCOMPLETE"}))
                digest = sampling._sha256(status_path)
                original_status_hashes[seed] = digest
                target_rows.append({
                    "game_seed": seed,
                    "attempts": [{
                        "attempt_path": status_path.parent.relative_to(root).as_posix(),
                        "status": "INCOMPLETE", "run_json_exists": False,
                        "status_json_sha256": digest,
                    }],
                })

            completed = runs / "seed-660243" / "attempt-0002"
            completed.mkdir(parents=True)
            run = {
                "status": "COMPLETE", "game_seed": 660243,
                "stage_key": "block1:batch-02", "arm": "residual",
                "model_sha256": sampling.CHECKPOINT_SHA, "config_sha256": "d" * 64,
                "process_cpu_seconds": 12.5,
            }
            (completed / "run.json").write_text(json.dumps(run))
            (completed / "status.json").write_text(json.dumps({
                "game_seed": 660243, "status": "COMPLETE",
            }))
            (completed / "decisions.jsonl.gz").write_bytes(b"test")
            target_rows[0]["attempts"].append({
                "attempt_path": completed.relative_to(root).as_posix(),
                "status": "COMPLETE", "run_json_exists": True,
                "status_json_sha256": sampling._sha256(completed / "status.json"),
            })

            stage_path = root / "stage.json"
            stage = {
                "status": "FAILED", "execution_count": 1,
                "executions": [{"task_key": ["block1:batch-02", 660243, "residual"]}],
                "source_commit": "old-head",
            }
            stage_path.write_text(json.dumps(stage))
            previous = {
                "status": "FAILED", "executing_head": "old-head",
                "launcher_sha256": "old-launcher",
                "attempts": [{
                    "attempt_id": "no-dispatch-1", "status": "FAILED_BEFORE_DISPATCH",
                    "complete_keys": [], "completed_games": [],
                    "stage_receipt_path": "missing-stage.json",
                    "pre_dispatch_failure_recovery": {
                        "preserved_original_incomplete_attempts": [
                            {
                                "game_seed": seed,
                                "attempt_path": f"evidence/runs/training/block1/batch-02/seed-{seed}/attempt-0001",
                                "status_json_sha256": digest,
                            }
                            for seed, digest in original_status_hashes.items()
                        ],
                    },
                }, {
                    "attempt_id": "partial-2", "status": "FAILED",
                    "executing_head": "old-head", "complete_keys": [660243],
                    "completed_games": [], "stage_receipt_path": "stage.json",
                    "parallel_stage_receipt": {
                        "status": "FAILED", "execution_count": 1,
                        "sha256": sampling._sha256(stage_path),
                    },
                }],
            }
            target_state = {
                "complete": [{
                    "game_seed": 660243,
                    "attempt_path": completed.relative_to(root).as_posix(),
                }],
                "keys": target_rows + [
                    {"game_seed": seed, "attempts": []}
                    for seed in TARGET_SEEDS if seed not in {660243, 660244}
                ],
            }
            with (mock.patch.object(sampling, "HERE", root),
                  mock.patch.object(sampling, "PROSPECTIVE", root / "evidence/prospective-cost")):
                rollover = _settled_source_rollover(
                    previous, target_state, head="new-head", launcher_sha256="new-launcher",
                    config_sha256="d" * 64,
                )

        self.assertEqual(rollover["prior_dispatched_execution_count"], 1)
        self.assertEqual(rollover["reconciled_completed_game_artifacts"][0]["game_seed"], 660243)
        self.assertEqual(
            rollover["reconciled_completed_game_artifacts"][0]["source_attempt_id"], "partial-2",
        )


if __name__ == "__main__":
    unittest.main()
