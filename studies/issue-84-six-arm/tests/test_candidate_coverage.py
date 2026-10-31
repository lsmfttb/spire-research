from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

STUDY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDY))

from candidate_coverage import (  # noqa: E402
    ACTION_BINDING_SOURCE,
    CHOICE_DESCRIPTOR,
    CHOICE_SOURCE,
    PROJECTION_SCHEMA,
    CoverageRepairingEncoder,
    repair_known_dead_adventurer_search_gap,
)
from driver import (  # noqa: E402
    ISSUE73_SOURCE_STUDY,
    SOURCE_LOCK_PATH,
    file_sha256,
    host_path,
    read_json,
)
import driver as issue84_driver  # noqa: E402


NATIVE_COMMIT = "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2"
UNSUPPORTED_REASON = "Dead Adventurer phase has no native reward entry"


def observed_projection() -> dict:
    def binding(index: int, kind: str, idx1: int, label: str) -> dict:
        return {
            "availability": "available",
            "source": ACTION_BINDING_SOURCE,
            "value": {
                "candidate_action": {
                    "scope": "game", "kind": kind, "idx1": idx1,
                    "idx2": 0, "idx3": 0, "label": label,
                },
                "candidate_index": index,
            },
            "visibility": "oracle_only",
        }

    event = {
        "availability": "available",
        "source": "GameContext::curEvent and constants/Events.h",
        "value": "Dead Adventurer",
        "visibility": "public_now",
    }
    return {
        "schema_id": PROJECTION_SCHEMA,
        "native_source_commit": NATIVE_COMMIT,
        "audience": "training_oracle_only",
        "public_api_safe": False,
        "candidate_order_source": (
            "StepSimulator::legalActions; ordered occurrences are retained without deduplication"
        ),
        "coverage_status": "NATIVE_CHOICE_SEMANTIC_NOT_READY",
        "candidate_count": 3,
        "supported_candidate_count": 2,
        "unsupported_candidates": [{
            "candidate_index": 0,
            "kind": "event",
            "reason": UNSUPPORTED_REASON,
        }],
        "screen_identity": {
            "availability": "available", "source": "GameContext::screenState",
            "value": "EVENT_SCREEN", "visibility": "public_now",
        },
        "shared_context": {
            "act": {"availability": "available", "source": "GameContext::act", "value": 1, "visibility": "public_now"},
            "floor": {"availability": "available", "source": "GameContext::floorNum", "value": 7, "visibility": "public_now"},
        },
        "candidates": [
            {
                "native_legal_occurrence_index": 0,
                "coverage_status": "unsupported",
                "reason": UNSUPPORTED_REASON,
                "candidate_binding": binding(0, "event", 0, "game.event idx1=0 idx2=0 idx3=0"),
                "features": {
                    "event": deepcopy(event),
                    "choice": {
                        "availability": "unavailable",
                        "reason": UNSUPPORTED_REASON,
                        "required_for_choice_semantics": True,
                    },
                },
            },
            {
                "native_legal_occurrence_index": 1,
                "coverage_status": "supported",
                "candidate_binding": binding(1, "event", 1, "game.event idx1=1 idx2=0 idx3=0"),
                "features": {
                    "event": deepcopy(event),
                    "choice": {
                        "availability": "available", "source": CHOICE_SOURCE,
                        "value": {
                            "choice_type": "leave_dead_adventurer",
                            "effect_type": "return_to_map",
                        },
                        "visibility": "oracle_only",
                    },
                },
            },
            {
                "native_legal_occurrence_index": 2,
                "coverage_status": "supported",
                "candidate_binding": binding(2, "game_potion_discard", 0, "potion discard"),
                "features": {
                    "choice_type": {
                        "availability": "available", "source": "GameContext::discardPotionAtIdx",
                        "value": "discard_potion", "visibility": "public_now",
                    },
                    "effect_type": {
                        "availability": "available", "source": "GameContext::discardPotionAtIdx",
                        "value": "remove_potion_from_associated_slot", "visibility": "public_now",
                    },
                    "potion": {
                        "availability": "available", "source": "GameContext::potions and GameContext::drinkPotionAtIdx",
                        "value": {"id": 5, "name": "Blessing Of The Forge", "potion_index": 0},
                        "visibility": "public_now",
                    },
                },
            },
        ],
    }


def supported_projection() -> dict:
    projection = observed_projection()
    first = projection["candidates"][0]
    first["coverage_status"] = "supported"
    first.pop("reason")
    first["features"]["choice"] = {
        "availability": "available", "source": CHOICE_SOURCE,
        "value": dict(CHOICE_DESCRIPTOR), "visibility": "oracle_only",
    }
    projection["coverage_status"] = "supported"
    projection["supported_candidate_count"] = 3
    projection["unsupported_candidates"] = []
    return projection


def locked_semantic_encoder():
    lock = read_json(SOURCE_LOCK_PATH)
    snapshot = lock["issue73_source_snapshot"]
    policy_path = (
        host_path(snapshot["path"]) / ISSUE73_SOURCE_STUDY / "policy.py"
    )
    if file_sha256(policy_path) != snapshot["files_sha256"]["policy.py"]:
        raise AssertionError("locked Issue 73 policy bytes differ from the Issue 84 source lock")
    spec = importlib.util.spec_from_file_location("issue84_locked_issue73_policy", policy_path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load the locked Issue 73 policy encoder")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.SemanticHashEncoder()


class CandidateCoverageTests(unittest.TestCase):
    def test_exact_source_qualified_candidate_maps_to_registered_descriptor(self):
        original = observed_projection()
        before = deepcopy(original)
        repaired, audit = repair_known_dead_adventurer_search_gap(original, 3)
        self.assertEqual(original, before)
        self.assertEqual(repaired["coverage_status"], "supported")
        self.assertEqual(repaired["supported_candidate_count"], 3)
        self.assertEqual(repaired["unsupported_candidates"], [])
        self.assertEqual(repaired["candidates"][0]["features"]["choice"], {
            "availability": "available", "source": CHOICE_SOURCE,
            "value": CHOICE_DESCRIPTOR, "visibility": "oracle_only",
        })
        self.assertEqual(audit["native_source_commit"], NATIVE_COMMIT)
        self.assertEqual(audit["model_feature_visibility"], "oracle_only")
        self.assertFalse(audit["future_outcome_features_added"])
        self.assertFalse(audit["current_phase_or_rng_features_added"])

    def test_locked_issue73_encoder_proves_supported_rows_and_bindings_unchanged(self):
        projection = observed_projection()
        candidates_before = deepcopy(projection["candidates"][1:])
        bindings_before = [deepcopy(row["candidate_binding"]) for row in projection["candidates"]]
        encoder = CoverageRepairingEncoder(locked_semantic_encoder())
        matrix, audit = encoder.encode(projection, 3)
        witness = audit["coverage_repair"]
        self.assertEqual(matrix.shape, (3, 2048))
        self.assertEqual(projection["candidates"][1:], candidates_before)
        self.assertEqual(
            [row["candidate_binding"] for row in projection["candidates"]],
            bindings_before,
        )
        self.assertTrue(witness["supported_candidate_features_equal"])
        self.assertEqual(
            witness["supported_candidate_matrix_sha256"],
            witness["supported_only_matrix_sha256"],
        )
        self.assertEqual(
            witness["candidate_bindings_before_sha256"],
            witness["candidate_bindings_after_sha256"],
        )
        self.assertTrue(witness["candidate_order_and_duplicates_preserved"])

    def test_already_supported_projection_delegates_without_mutation(self):
        projection = supported_projection()
        before = deepcopy(projection)
        raw = locked_semantic_encoder()
        expected_matrix, expected_audit = raw.encode(projection, 3)
        observed_matrix, observed_audit = CoverageRepairingEncoder(raw).encode(projection, 3)
        self.assertEqual(projection, before)
        self.assertTrue((observed_matrix == expected_matrix).all())
        self.assertEqual(observed_audit, expected_audit)
        self.assertNotIn("coverage_repair", observed_audit)

    def test_near_miss_projections_are_not_repaired(self):
        mutations = (
            lambda item: item.update(native_source_commit="f" * 40),
            lambda item: item["candidates"][0]["candidate_binding"]["value"]["candidate_action"].update(idx1=2),
            lambda item: item["candidates"][0]["features"]["choice"].update(reason="different source condition"),
            lambda item: item["screen_identity"].update(value="MAP_SCREEN"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                projection = observed_projection()
                mutate(projection)
                before = deepcopy(projection)
                class RejectingEncoder:
                    def encode(self, received, count):
                        self.received = received
                        self.count = count
                        raise ValueError("original fail-closed encoder")
                rejecting = RejectingEncoder()
                with self.assertRaisesRegex(ValueError, "original fail-closed"):
                    CoverageRepairingEncoder(rejecting).encode(projection, 3)
                self.assertIs(rejecting.received, projection)
                self.assertEqual(projection, before)

    def test_runtime_returns_repairing_encoder_to_runner_execution_path(self):
        raw_encoder = locked_semantic_encoder()
        projection = observed_projection()

        class FakeRunner:
            def _run_one(self, *, encoder, received_projection):
                return encoder.encode(received_projection, 3)

        runner = FakeRunner()
        runtime = SimpleNamespace(
            initialize_native_runtime=lambda *args, **kwargs: ({}, runner, object(), raw_encoder),
        )
        issue84_driver.install_issue73_capture_limit_compat(runtime)

        _, observed_runner, _, encoder = runtime.initialize_native_runtime()
        self.assertIsInstance(encoder, CoverageRepairingEncoder)
        matrix, audit = observed_runner._run_one(
            encoder=encoder, received_projection=projection,
        )
        self.assertEqual(matrix.shape, (3, 2048))
        self.assertTrue(audit["coverage_repair"]["supported_candidate_features_equal"])


class CarryForwardTests(unittest.TestCase):
    def test_source_lock_carry_forward_allows_only_registered_scheduler_overlay(self):
        driver = issue84_driver
        package = "a" * 40
        overlay_path = "issue-79-successor/driver.py"
        base_digest = "b" * 64
        overlay_digest = "c" * 64
        manifest = {
            "files": {overlay_path: {"sha256": base_digest}},
            "overlays": {overlay_path: {
                "issue": 84, "base_commit": "d" * 40,
                "base_sha256": base_digest, "sha256": overlay_digest,
                "bytes": 10, "reason": "bounded Issue 84 retry repair",
            }},
        }
        compatible = {
            "contract_sha256": "contract", "upstream_commit": "d" * 40,
            "issue73_source_snapshot": {"sha256": "issue73"},
            "external_runtime": {"sha256": "runtime"},
            "parent": {"sha256": "parent"},
            "fixed_scales": {"t_ref": 1.0}, "seed_derivation": "fixed",
        }
        predecessor = {
            **compatible,
            "upstream_files": {overlay_path: base_digest, "other.py": "e" * 64},
        }
        current = {
            **compatible,
            "upstream_files": {overlay_path: overlay_digest, "other.py": "e" * 64},
        }
        with tempfile.TemporaryDirectory(dir=STUDY) as temporary:
            manifest_path = Path(temporary) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with (
                patch.object(driver, "UPSTREAM_MANIFEST_PATH", manifest_path),
                patch.object(driver, "UPSTREAM_COMMIT", "d" * 40),
                patch.object(driver, "PILOT_CANDIDATE_COVERAGE_OVERLAY_PATH", overlay_path),
                patch.object(driver, "PILOT_CANDIDATE_COVERAGE_BASE_SHA256", base_digest),
                patch.object(driver, "PILOT_CARRY_FORWARD", {"A": {
                    "package": package, "runtime_ast_sha256": "f" * 64,
                }}),
                patch.object(driver, "_historical_pilot_runtime_fingerprint", return_value="f" * 64),
                patch.object(driver, "_pilot_runtime_fingerprint", return_value="1" * 64),
                patch.object(driver, "PILOT_PREDECESSOR_RUNTIME_AST_SHA256", "1" * 64),
            ):
                driver._verify_predecessor_lock_compatibility(predecessor, current, package=package)
                changed = deepcopy(current)
                changed["upstream_files"]["other.py"] = "2" * 64
                with self.assertRaisesRegex(RuntimeError, "differs beyond"):
                    driver._verify_predecessor_lock_compatibility(predecessor, changed, package=package)

    def test_registered_c_owner_attempts_are_carried_and_resume_numbered_twenty(self):
        driver = issue84_driver
        source_package = driver.PILOT_C_INTERRUPTED_ATTEMPT_PACKAGE
        prior_table = read_json(driver.seed_table_path(source_package))
        rows = [
            row for row in prior_table["rows"]
            if row.get("phase") == "training" and row.get("formal") is False
            and row.get("issue84_arm") == "C"
            and 8_480_001 <= int(row.get("owner_seed", 0)) <= 8_480_032
        ]
        artifact_root = host_path(driver.DATA_ROOT_WSL) / "artifacts"
        package = "a" * 40
        with tempfile.TemporaryDirectory(dir=STUDY) as temporary:
            pilot_root = Path(temporary) / "pilot" / package
            backend_stub = SimpleNamespace(ARTIFACTS=artifact_root)
            with (
                patch.object(driver, "configure_backend", return_value=backend_stub),
                patch.object(driver, "_verify_predecessor_lock_compatibility"),
            ):
                manifest = driver._pilot_c_attempt_lineage(
                    package=package, pilot_root=pilot_root, rows=rows,
                    seed_table=prior_table, current_lock=read_json(SOURCE_LOCK_PATH),
                )
            self.assertEqual(manifest["prior_attempt_count"], 19)
            self.assertEqual(manifest["attempt_19_active_wall_tail"], "UNKNOWN")
            self.assertEqual(
                [item["status"] for item in manifest["attempts"][:18]],
                ["FAILED_DETERMINISTIC_COVERAGE"] * 18,
            )
            self.assertEqual(manifest["attempts"][18]["status"], "INCOMPLETE_UNKNOWN_TAIL")

            backend_path = STUDY / "upstream" / "issue-79-successor" / "driver.py"
            spec = importlib.util.spec_from_file_location("issue84_lineage_backend", backend_path)
            backend = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = backend
            spec.loader.exec_module(backend)
            backend.ARTIFACTS = artifact_root
            target_row = next(row for row in rows if row["unit_id"] == "TRAIN:8480022:C")
            with patch.object(backend, "source_lock_sha", return_value=file_sha256(SOURCE_LOCK_PATH)):
                offset = backend._prior_attempt_offset(
                    pilot_root / "C", target_row, package,
                )
            self.assertEqual(offset, 19)

    def test_actor_update_pointer_resolves_retained_checkpoint_without_copy(self):
        driver = issue84_driver
        package = "a" * 40
        source_package = "b" * 40
        arm = "A"
        with tempfile.TemporaryDirectory(dir=STUDY) as temporary:
            artifacts = Path(temporary) / "artifacts"
            source_root = artifacts / "pilot" / source_package / arm
            source_root.mkdir(parents=True)
            source_progress = source_root.parent / "pilot-progress.json"
            source_progress.write_bytes(b"opaque progress bytes")
            receipt_path = source_root / "diagnostics" / "pilot-update.json"
            receipt_path.parent.mkdir(parents=True)
            receipt_path.write_bytes(b"opaque actor update receipt")
            model_path = source_root / "models" / "pi13.pt"
            model_path.parent.mkdir(parents=True)
            model_path.write_bytes(b"retained actor bytes")
            checkpoint_path = source_root / "models" / "blocks" / "pilot-block-00-step.pt"
            checkpoint_path.parent.mkdir(parents=True)
            checkpoint_path.write_bytes(b"retained optimizer bytes")
            lock_bytes = b"source lock bytes"
            source_lock_sha = driver.sha256(lock_bytes)
            lock_path = artifacts / "source-lock-history" / f"{source_package}-{source_lock_sha}.json"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_bytes(lock_bytes)
            current_lock_path = Path(temporary) / "current-source-lock.json"
            current_lock_path.write_bytes(b"current source lock bytes")
            current_lock_sha = driver.file_sha256(current_lock_path)
            spec = {
                "package": source_package,
                "source_lock_sha256": source_lock_sha,
                "progress_sha256": driver.file_sha256(source_progress),
                "receipt_sha256": driver.file_sha256(receipt_path),
                "model_sha256": driver.file_sha256(model_path),
                "checkpoint_sha256": driver.file_sha256(checkpoint_path),
                "model_digest": "d" * 64,
                "owner_training_feature_manifest_sha256": "e" * 64,
            }
            current_root = artifacts / "pilot" / package / arm
            pointer = {
                "schema_id": "issue84-pilot-actor-update-carry-forward-v1",
                "current_package_commit": package,
                "current_source_lock_sha256": current_lock_sha,
                "arm": arm,
                "source_package_commit": source_package,
                "source_lock_sha256": source_lock_sha,
                "source_progress_sha256": spec["progress_sha256"],
                "source_receipt_sha256": spec["receipt_sha256"],
                "source_artifact_root": f"pilot/{source_package}/{arm}",
                "training_carry_forward_manifest_sha256": "f" * 64,
                "model_sha256": spec["model_sha256"],
                "optimizer_checkpoint_sha256": spec["checkpoint_sha256"],
                "model_digest": spec["model_digest"],
                "owner_training_feature_manifest_sha256": spec["owner_training_feature_manifest_sha256"],
            }
            pointer["manifest_sha256"] = driver.sha256(driver.canonical(pointer))
            pointer_path = current_root / "diagnostics" / "pilot-update-carry-forward.json"
            driver.write_json_once(pointer_path, pointer)
            backend = SimpleNamespace(ARTIFACTS=artifacts)
            patches = (
                patch.object(driver, "PILOT_ACTOR_UPDATE_CARRY_FORWARD", {arm: spec}),
                patch.object(driver, "SOURCE_LOCK_PATH", current_lock_path),
                patch.object(driver, "package_commit", return_value=package),
                patch.object(driver, "configure_backend", return_value=backend),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                resolved = driver._pilot_actor_update_model_path(current_root, package)
            self.assertEqual(resolved, model_path)
            self.assertFalse((current_root / "models" / "pi13.pt").exists())
            model_path.write_bytes(b"changed retained actor bytes")
            with patches[0], patches[1], patches[2], patches[3]:
                with self.assertRaisesRegex(RuntimeError, "evidence changed"):
                    driver._pilot_actor_update_model_path(current_root, package)


class RetryPauseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend_path = STUDY / "upstream" / "issue-79-successor" / "driver.py"
        spec = importlib.util.spec_from_file_location("issue84_pinned_backend", backend_path)
        if spec is None or spec.loader is None:
            raise AssertionError("could not load pinned #79 backend")
        cls.backend = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.backend
        spec.loader.exec_module(cls.backend)

    def _run(self, outcomes):
        backend = self.backend
        row = {
            "unit_id": "TRAIN-8480001-C", "owner_seed": 8_480_001,
            "phase": "training", "arm": "TRAIN", "formal": False,
            "game_seed": 8_480_001, "policy_rng_seed": 12345,
        }
        started_jobs = []

        class FakeProcess:
            def __init__(self, job, outcome):
                self.pid = 99_000 + len(started_jobs)
                self.returncode = 0
                attempt = Path(job["attempt_dir"])
                attempt.mkdir(parents=True, exist_ok=True)
                started = {
                    "schema_id": "issue79-owner-attempt-start-v1",
                    "unit_id": row["unit_id"], "owner_seed": row["owner_seed"],
                    "run_id": job["run_id"], "started_monotonic": 1.0,
                    "package_commit": job["package_commit"],
                    "seed_row_sha256": backend.sha256(backend.canonical(row)),
                }
                backend.write_json_once(attempt / "started.json", started)
                result = {"schema_id": "issue79-owner-attempt-result-v1", **outcome}
                result.update({"unit_id": row["unit_id"], "owner_seed": row["owner_seed"],
                              "run_id": job["run_id"], "phase": "training", "arm": "TRAIN"})
                result_path = Path(job["result_path"])
                backend.write_json_once(result_path, result)
                backend.write_json_once(attempt / "finished.json", {
                    "schema_id": "issue79-owner-attempt-finish-v1",
                    "unit_id": row["unit_id"], "run_id": job["run_id"],
                    "status": result["status"], "finished_monotonic": 2.0,
                    "result_sha256": backend.file_sha256(result_path),
                })

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        def fake_popen(argv, **kwargs):
            job = backend.read_json(Path(argv[-1]))
            started_jobs.append(deepcopy(job))
            if not outcomes:
                raise AssertionError("scheduler started an unexpected extra attempt")
            return FakeProcess(job, outcomes.pop(0))

        temporary_context = tempfile.TemporaryDirectory(dir=STUDY)
        try:
            root = Path(temporary_context.name) / "pilot" / "C"
            root.mkdir(parents=True)
            patches = (
                patch.object(backend, "source_commit", return_value="a" * 40),
                patch.object(backend, "source_lock_sha", return_value="b" * 64),
                patch.object(backend, "existing_success", return_value=None),
                patch.object(backend, "PINNED_PYTHON", Path(sys.executable)),
                patch.object(backend, "CONCURRENCY", 1),
                patch.object(backend.subprocess, "Popen", side_effect=fake_popen),
                patch.dict(os.environ, {
                    "STS_LIGHTSPEED_SOURCE_DIR": "/locked/native",
                    "STSRL_SOURCE_DIR": "/locked/stsrl",
                }),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                result = backend.run_units(
                    [row], root=root, pilot=True, clock=None,
                    package_commit="c" * 40,
                )
            attempts = sorted((root / "units" / backend.unit_key(row["unit_id"])).glob("attempt-*"))
            self.addCleanup(temporary_context.cleanup)
            return result, started_jobs, attempts
        except BaseException:
            temporary_context.cleanup()
            raise

    def test_deterministic_coverage_failure_pauses_without_retry(self):
        result, jobs, attempts = self._run([{
            "status": "FAILED", "failure_boundary": "ProjectionError",
            "failure_detail": "native projection does not support every live legal candidate",
        }])
        self.assertEqual(result["status"], "PILOT_PAUSED_DETERMINISTIC_FAILURE")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(attempts), 1)
        self.assertTrue((attempts[0] / "failure-classification.json").is_file())
        self.assertTrue(Path(result["pause_record"]).is_file())

    def test_one_identified_transport_fault_retries_same_frozen_row(self):
        result, jobs, attempts = self._run([
            {"status": "FAILED", "failure_boundary": "ConnectionResetError", "failure_detail": "peer reset"},
            {"status": "SUCCEEDED", "resource": {"worker_wall_seconds": 0.1}},
        ])
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["row"], jobs[1]["row"])
        self.assertEqual(jobs[0]["run_id"], "TRAIN-8480001-C-attempt-0001")
        self.assertEqual(jobs[1]["run_id"], "TRAIN-8480001-C-attempt-0002")
        self.assertTrue((attempts[0] / "failure-classification.json").is_file())
        self.assertTrue((attempts[1] / "result.json").is_file())

    def test_repeated_transport_fault_pauses_before_third_attempt(self):
        result, jobs, attempts = self._run([
            {"status": "FAILED", "failure_boundary": "ConnectionResetError", "failure_detail": "peer reset"},
            {"status": "FAILED", "failure_boundary": "ConnectionResetError", "failure_detail": "peer reset"},
        ])
        self.assertEqual(result["status"], "PILOT_PAUSED_REPEATED_TRANSPORT_FAILURE")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(Path(result["pause_record"]).is_file())

    def test_unknown_failure_pauses_without_retry(self):
        result, jobs, attempts = self._run([{
            "status": "FAILED", "failure_boundary": "ValueError",
            "failure_detail": "unclassified failure",
        }])
        self.assertEqual(result["status"], "PILOT_PAUSED_UNRESOLVED_FAILURE")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(attempts), 1)
        self.assertTrue(Path(result["pause_record"]).is_file())

    def test_unaccepted_success_shaped_attempt_pauses_instead_of_duplicate_game(self):
        backend = self.backend
        row = {
            "unit_id": "TRAIN-8480001-C", "owner_seed": 8_480_001,
            "phase": "training", "arm": "TRAIN", "formal": False,
            "game_seed": 8_480_001, "policy_rng_seed": 12345,
        }
        temporary_context = tempfile.TemporaryDirectory(dir=STUDY)
        self.addCleanup(temporary_context.cleanup)
        try:
            root = Path(temporary_context.name) / "pilot" / "C"
            base = root / "units" / backend.unit_key(row["unit_id"])
            attempt = base / "attempt-0001"
            attempt.mkdir(parents=True)
            started = {
                "schema_id": "issue79-owner-attempt-start-v1",
                "unit_id": row["unit_id"], "owner_seed": row["owner_seed"],
                "run_id": f"{row['unit_id']}-attempt-0001", "started_monotonic": 1.0,
                "package_commit": "c" * 40,
                "seed_row_sha256": backend.sha256(backend.canonical(row)),
            }
            backend.write_json_once(attempt / "started.json", started)
            result_path = attempt / "result.json"
            backend.write_json_once(result_path, {
                "schema_id": "issue79-owner-attempt-result-v1",
                "unit_id": row["unit_id"], "owner_seed": row["owner_seed"],
                "run_id": started["run_id"], "phase": "training", "arm": "TRAIN",
                "status": "SUCCEEDED",
            })
            backend.write_json_once(attempt / "finished.json", {
                "schema_id": "issue79-owner-attempt-finish-v1",
                "unit_id": row["unit_id"], "run_id": started["run_id"],
                "status": "SUCCEEDED", "finished_monotonic": 2.0,
                "result_sha256": backend.file_sha256(result_path),
            })
            with (
                patch.object(backend, "source_commit", return_value="a" * 40),
                patch.object(backend, "source_lock_sha", return_value="b" * 64),
                patch.object(backend, "existing_success", return_value=None),
                patch.object(backend, "PINNED_PYTHON", Path(sys.executable)),
                patch.object(backend, "CONCURRENCY", 1),
                patch.object(backend.subprocess, "Popen", side_effect=AssertionError("duplicate run")),
                patch.dict(os.environ, {
                    "STS_LIGHTSPEED_SOURCE_DIR": "/locked/native",
                    "STSRL_SOURCE_DIR": "/locked/stsrl",
                }),
            ):
                outcome = backend.run_units(
                    [row], root=root, pilot=True, clock=None, package_commit="c" * 40,
                )
        except BaseException:
            temporary_context.cleanup()
            raise
        self.assertEqual(outcome["status"], "PILOT_PAUSED_UNRESOLVED_FAILURE")
        self.assertEqual(outcome["completed"], 0)
        self.assertTrue(Path(outcome["pause_record"]).is_file())


if __name__ == "__main__":
    unittest.main()
