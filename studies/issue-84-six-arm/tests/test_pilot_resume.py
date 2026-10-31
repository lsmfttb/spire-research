from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

STUDY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDY))

import driver  # noqa: E402
import gradients  # noqa: E402
import predictors  # noqa: E402


class PilotResumeRepairTests(unittest.TestCase):
    def test_parent_prestart_failures_are_bound_without_reading_terminal_outcomes(self):
        source_package = driver.PILOT_EVALUATION_PRESTART_FAILURE_CARRY_FORWARD["package"]
        lock_sha256 = driver.PILOT_EVALUATION_PRESTART_FAILURE_CARRY_FORWARD["source_lock_sha256"]
        expected_progress_sha256 = "ce745ba41b934360e6d6a78d7d4faee597ebed236c64a12f215eea0db9c62310"
        owners = range(8_490_001, 8_490_009)
        rows = [
            {
                "unit_id": f"EVAL:{owner}:PARENT",
                "owner_seed": owner,
                "phase": "evaluation",
                "arm": "PARENT",
                "formal": False,
            }
            for owner in owners
        ]
        rows_sha256 = driver.sha256(driver.canonical(rows))
        seed_table = {"rows": rows, "rows_sha256": rows_sha256}

        def write_json(path, value):
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(driver.canonical(value) + b"\n")
            return path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts"
            pilot_root = artifacts / "pilot" / ("a" * 40)
            prior_root = artifacts / "pilot" / source_package
            lock_path = artifacts / "source-lock-history" / f"{source_package}-{lock_sha256}.json"
            progress_path = prior_root / "pilot-progress.json"
            table_path = root / "seed-table.json"
            write_json(lock_path, {"schema_id": "fixture"})
            write_json(progress_path, {
                "package_commit": source_package,
                "source_lock_sha256": lock_sha256,
                "evaluation_runs": {},
            })
            write_json(table_path, {
                "source_lock_sha256": lock_sha256,
                "rows_sha256": rows_sha256,
                "rows": rows,
            })
            for row in rows:
                unit_id = row["unit_id"]
                attempt = prior_root / "PARENT" / "units" / unit_id.replace(":", "-") / "attempt-0001"
                started = {
                    "unit_id": unit_id,
                    "owner_seed": row["owner_seed"],
                    "arm": "PARENT",
                    "package_commit": source_package,
                    "seed_row_sha256": driver.sha256(driver.canonical(row)),
                    "run_id": f"{unit_id}-attempt-0001",
                }
                result = {
                    "status": "FAILED",
                    "unit_id": unit_id,
                    "owner_seed": row["owner_seed"],
                    "arm": "PARENT",
                    "failure_boundary": "ValueError",
                    "failure_detail": "unknown study arm PARENT",
                    "run_id": started["run_id"],
                    "traceback": "ValueError: unknown study arm PARENT\n  line 727, in __init__",
                }
                result_path = write_json(attempt / "result.json", result)
                write_json(attempt / "started.json", started)
                write_json(attempt / "finished.json", {
                    "status": "FAILED",
                    "unit_id": unit_id,
                    "run_id": started["run_id"],
                    "result_sha256": driver.file_sha256(result_path),
                })
                write_json(attempt / "failure-classification.json", {
                    "schema_id": "issue84-attempt-failure-classification-v1",
                    "category": "unresolved_failure",
                    "failure_boundary": "ValueError",
                    "failure_detail": "unknown study arm PARENT",
                    "failure_signature_sha256": "cfc57887ac2bee8628d099bf6f8f36246ea1e11ebcff4feec666a4e228702980",
                })

            backend = types.SimpleNamespace(
                ARTIFACTS=artifacts,
                unit_key=lambda unit_id: "".join(
                    character if character.isalnum() or character in "-_" else "-"
                    for character in unit_id
                ),
            )
            actual_file_sha256 = driver.file_sha256

            def fixture_hash(path):
                path = Path(path)
                if path == lock_path:
                    return lock_sha256
                if path == progress_path:
                    return expected_progress_sha256
                if path.name == "failure-classification.json":
                    return driver.PILOT_EVALUATION_PRESTART_FAILURE_CARRY_FORWARD["failure_classification_sha256"]
                return actual_file_sha256(path)

            with (
                patch.object(driver, "configure_backend", return_value=backend),
                patch.object(driver, "seed_table_path", return_value=table_path),
                patch.object(driver, "file_sha256", side_effect=fixture_hash),
                patch.object(driver, "_verify_predecessor_lock_compatibility"),
            ):
                receipt = driver._pilot_evaluation_prestart_failure_resume(
                    package="a" * 40,
                    pilot_root=pilot_root,
                    rows=rows,
                    seed_table=seed_table,
                    current_lock={"schema_id": "fixture"},
                )

            self.assertEqual(len(receipt["units"]), 8)
            self.assertTrue(all(unit["game_created"] is False for unit in receipt["units"]))
            self.assertTrue(all(unit["unit_id"].startswith("EVAL:") for unit in receipt["units"]))
            self.assertTrue((pilot_root / "lineage" / "previous-parent-prestart-failures.json").is_file())

    def test_predictor_fits_can_be_carried_by_hash_bound_pointer(self):
        for arm in ("D", "E", "F"):
            spec = driver.PILOT_PREDICTOR_CARRY_FORWARD[arm]
            with self.subTest(arm=arm), tempfile.TemporaryDirectory() as temporary:
                artifacts = Path(temporary) / "artifacts"
                source_root = artifacts / "pilot" / spec["package"] / arm
                current_package = "a" * 40
                pilot_root = artifacts / "pilot" / current_package
                source_lock = artifacts / "source-lock-history" / (
                    f"{spec['package']}-{spec['source_lock_sha256']}.json"
                )
                progress_path = source_root.parent / "pilot-progress.json"
                receipt_path = source_root / "diagnostics" / "pilot-predictor-fit.json"
                checkpoint_path = source_root / "models" / "predictors" / "pilot-after-block-00.pt"
                source_lock.parent.mkdir(parents=True)
                source_lock.write_text('{"schema_id":"fixture"}', encoding="utf-8")
                checkpoint_path.parent.mkdir(parents=True)
                checkpoint_path.write_bytes(b"predictor checkpoint fixture")
                fit = {"value": {"examples": 32}}
                training_origin = {"package_commit": spec["package"], "source_lock_sha256": spec["source_lock_sha256"]}
                update_origin = {"package_commit": spec["package"], "source_lock_sha256": spec["source_lock_sha256"]}
                progress = {
                    "package_commit": spec["package"],
                    "source_lock_sha256": spec["source_lock_sha256"],
                    "predictor_fits": {arm: {
                        "status": "SUCCEEDED",
                        "fit": fit,
                        "checkpoint_sha256": spec["checkpoint_sha256"],
                    }},
                    "training_runs": {arm: {"status": "SUCCEEDED", "completed": 32, "evidence_origin": training_origin}},
                    "actor_updates": {arm: {"status": "SUCCEEDED", "evidence_origin": update_origin}},
                }
                receipt = {
                    "status": "SUCCEEDED",
                    "arm": arm,
                    "package_commit": spec["package"],
                    "source_lock_sha256": spec["source_lock_sha256"],
                    "checkpoint_sha256": spec["checkpoint_sha256"],
                    "fit": fit,
                }
                for path, value in ((progress_path, progress), (receipt_path, receipt)):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(driver.canonical(value))

                backend = types.SimpleNamespace(ARTIFACTS=artifacts)
                actual_file_sha256 = driver.file_sha256
                hashes = {
                    source_lock: spec["source_lock_sha256"],
                    progress_path: spec["progress_sha256"],
                    receipt_path: spec["receipt_sha256"],
                    checkpoint_path: spec["checkpoint_sha256"],
                }

                def fixture_hash(path):
                    path = Path(path)
                    if path in hashes:
                        return hashes[path]
                    return actual_file_sha256(path)

                current_source = Path(driver.__file__).read_text(encoding="utf-8")
                with (
                    patch.object(driver, "configure_backend", return_value=backend),
                    patch.object(driver, "file_sha256", side_effect=fixture_hash),
                    patch.object(driver, "_verify_predecessor_lock_compatibility"),
                    patch.object(driver, "_show_source_commit_file", return_value=current_source),
                ):
                    carried, manifest = driver._pilot_predictor_fit_resume(
                        arm=arm, package=current_package, pilot_root=pilot_root,
                        current_lock={"schema_id": "fixture"},
                    )

                self.assertEqual(carried["status"], "SUCCEEDED")
                self.assertEqual(carried["checkpoint_sha256"], spec["checkpoint_sha256"])
                self.assertEqual(manifest["source_package_commit"], spec["package"])
                self.assertTrue((pilot_root / arm / "diagnostics" / "pilot-predictor-fit-carry-forward.json").is_file())

    def test_resume_runtime_fingerprint_is_source_qualified_and_distinct(self):
        self.assertEqual(
            driver.PILOT_CARRY_FORWARD["C"]["runtime_ast_sha256"],
            driver.PILOT_RESUME_RUNTIME_AST_SHA256,
        )
        self.assertEqual(
            driver._historical_pilot_runtime_fingerprint(driver.PILOT_RESUME_PACKAGE),
            driver.PILOT_RESUME_RUNTIME_AST_SHA256,
        )
        self.assertEqual(
            driver._pilot_runtime_fingerprint(),
            driver.PILOT_PREDECESSOR_RUNTIME_AST_SHA256,
        )
        for arm in ("E", "F"):
            self.assertEqual(
                driver.PILOT_CARRY_FORWARD[arm]["runtime_ast_sha256"],
                driver.PILOT_RESUME_RUNTIME_AST_SHA256,
            )

    def test_pilot_closes_clock_if_preflight_fails(self):
        events = []
        callbacks = []

        class Clock:
            elapsed_before = 12.0

            def close(self):
                events.append("clock-close")

        def verify_source_lock(**_kwargs):
            events.append("source-lock")
            raise RuntimeError("expected source-lock diagnostic failure")

        backend = types.SimpleNamespace(
            ActiveClock=lambda mode, allowance: (events.append("clock-start") or Clock()),
        )
        with (
            patch.object(driver, "configure_backend", return_value=backend),
            patch.object(driver, "verify_source_lock", side_effect=verify_source_lock),
            patch.object(driver.atexit, "register", side_effect=lambda callback: callbacks.append(callback)),
            patch.object(driver.atexit, "unregister"),
        ):
            with self.assertRaisesRegex(RuntimeError, "expected source-lock diagnostic failure"):
                driver.run_pilot()

        self.assertEqual(events, ["clock-start", "source-lock"])
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(events, ["clock-start", "source-lock", "clock-close"])

    def test_actor_update_fingerprint_reads_exact_predecessor_commit(self):
        driver._verify_pilot_update_algorithm_unchanged(driver.PILOT_RESUME_PACKAGE)

    def test_c_and_d_completed_training_are_carried_without_replay(self):
        predecessor = driver.PILOT_RESUME_PACKAGE
        lock_sha = driver.PILOT_RESUME_SOURCE_LOCK_SHA256
        verified_summary = {
            "status": "SUCCEEDED", "completed": 32,
            "elapsed_seconds": 12.5, "outcomes": ["fixture-only"],
        }
        prior_summary = {
            **verified_summary, "retry_count": 0,
            "native_worker_wall_seconds": 10.0,
        }

        for arm in ("C", "D"):
            with self.subTest(arm=arm), tempfile.TemporaryDirectory() as temporary:
                artifacts = Path(temporary) / "artifacts"
                current_root = artifacts / "pilot" / "new-package" / arm
                predecessor_root = artifacts / "pilot" / predecessor / arm
                predecessor_root.mkdir(parents=True)
                current_root.parent.mkdir(parents=True)
                lock_path = artifacts / "source-lock-history" / f"{predecessor}-{lock_sha}.json"
                lock_path.parent.mkdir(parents=True)
                lock_path.write_bytes(b"source-lock-placeholder")
                seed_path = Path(temporary) / "seed-table.json"
                seed_path.write_text("{}", encoding="utf-8")
                progress_path = predecessor_root.parent / "pilot-progress.json"
                progress_path.write_text("{}", encoding="utf-8")
                rows = [
                    {"unit_id": f"TRAIN:{8_480_001 + index}:{arm}", "owner_seed": 8_480_001 + index}
                    for index in range(32)
                ]
                seed_table = {
                    "source_lock_sha256": lock_sha,
                    "rows": rows,
                    "rows_sha256": driver.sha256(driver.canonical(rows)),
                }
                progress = {
                    "package_commit": predecessor,
                    "source_lock_sha256": lock_sha,
                    "training_runs": {arm: prior_summary},
                }

                def read_fixture(path):
                    path = Path(path)
                    if path == seed_path:
                        return seed_table
                    if path == lock_path:
                        return {"schema_id": "fixture"}
                    if path == progress_path:
                        return progress
                    raise AssertionError(f"unexpected evidence read: {path}")

                backend = types.SimpleNamespace(
                    ARTIFACTS=artifacts,
                    existing_success_record=lambda root, row, package: (
                        {"resource": {"worker_wall_seconds": 1.0}},
                        Path(root) / row["unit_id"] / "result.json",
                    ),
                )
                with (
                    patch.object(driver, "configure_backend", return_value=backend),
                    patch.object(driver, "seed_table_path", return_value=seed_path),
                    patch.object(driver, "read_json", side_effect=read_fixture),
                    patch.object(driver, "file_sha256", return_value=lock_sha),
                    patch.object(driver, "_verify_predecessor_lock_compatibility"),
                    patch.object(driver, "_predecessor_source_lock", return_value=nullcontext()),
                    patch.object(
                        driver, "_validate_carried_attempt",
                        side_effect=lambda row, *_args, **_kwargs: {"unit_id": row["unit_id"]},
                    ),
                    patch.object(driver, "run_summary", return_value=verified_summary),
                ):
                    result = driver._pilot_training_resume(
                        arm=arm, package="new-package", pilot_root=current_root.parent,
                        rows=rows, seed_table={"rows": rows}, current_lock={"schema_id": "fixture"},
                    )

                carried_root, carried_package, summary, manifest = result
                self.assertEqual(carried_root, predecessor_root)
                self.assertEqual(carried_package, predecessor)
                self.assertEqual(summary["completed"], 32)
                self.assertEqual(summary["evidence_origin"]["package_commit"], predecessor)
                self.assertEqual(summary["evidence_origin"]["source_lock_sha256"], lock_sha)
                self.assertEqual(len(manifest["units"]), 32)

    def test_predictor_cache_reads_the_resolved_training_source(self):
        outcome = object()
        row = {"owner_seed": 8_480_001, "unit_id": "TRAIN:8480001:D"}
        training_root = Path("/frozen/pilot/9be126a/D")
        training_package = driver.PILOT_RESUME_PACKAGE
        current_package = "new-package"
        executor = types.SimpleNamespace()
        manifest_lookups = []
        history_lookups = []
        predecessor_lock_sha = driver.PILOT_RESUME_SOURCE_LOCK_SHA256
        current_lock_sha = "a" * 64
        prior_receipt = {
            "training_features": {"sha256": "c" * 64},
            "heart_victory": False,
            "terminal_floor": 3.0,
            "source_lock_sha256": predecessor_lock_sha,
        }

        def load_current_lock(path):
            return predecessor_lock_sha if Path(path) == predecessor_lock else current_lock_sha

        def prior_package_only_success(root, _row, package):
            active_lock_sha = backend.source_lock_sha()
            manifest_lookups.append((Path(root), package, active_lock_sha))
            if (
                Path(root) == training_root
                and package == training_package
                and active_lock_sha == prior_receipt["source_lock_sha256"]
            ):
                return prior_receipt
            return None

        def load_outcomes(rows, *, root, executor, package):
            active_lock_sha = backend.source_lock_sha()
            history_lookups.append((Path(root), package, active_lock_sha))
            self.assertEqual(active_lock_sha, predecessor_lock_sha)
            self.assertEqual(Path(root), training_root)
            self.assertEqual(package, training_package)
            self.assertEqual(rows, [row])
            self.assertIsNotNone(executor)
            return [], [outcome]

        expected_manifest = driver.sha256(driver.canonical([{
            "owner_seed": row["owner_seed"],
            "feature_sha256": "c" * 64,
            "reward": 3.0,
        }]))

        def save_checkpoint(path, _state):
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"checkpoint")

        executor._atomic_torch_save = save_checkpoint

        class FakePredictor:
            def __init__(self, *, seed):
                self.seed = seed

            def state_dict(self):
                return {"seed": self.seed}

        def build_cache(history, *, binding, **_kwargs):
            self.assertEqual(list(history), [(0, [outcome])])
            self.assertEqual(binding["prior_block_feature_manifest_sha256"], [expected_manifest])
            return {"examples": 1}

        with tempfile.TemporaryDirectory() as temporary:
            current_root = Path(temporary) / current_package
            archive_root = Path(temporary) / "artifacts"
            predecessor_lock = (
                archive_root / "source-lock-history"
                / f"{training_package}-{predecessor_lock_sha}.json"
            )
            predecessor_lock.parent.mkdir(parents=True)
            predecessor_lock.write_text("predecessor lock fixture", encoding="utf-8")
            backend = types.SimpleNamespace(
                ARTIFACTS=archive_root,
                SOURCE_LOCK=driver.SOURCE_LOCK_PATH,
                existing_success=prior_package_only_success,
            )
            backend.source_lock_sha = lambda: load_current_lock(backend.SOURCE_LOCK)

            self.assertNotEqual(training_package, current_package)
            self.assertFalse(current_root.exists(), "current package must not contain carried training receipts")
            with (
                patch.object(driver, "BACKEND", backend),
                patch.object(driver, "configure_backend", return_value=backend),
                patch.object(driver, "file_sha256", side_effect=load_current_lock),
                patch.object(driver, "load_training_outcomes", side_effect=load_outcomes) as load,
                patch.object(driver, "write_json_once"),
                patch.object(gradients, "ScalarPredictor", FakePredictor),
                patch.object(predictors, "new_predictor_optimizer", return_value=types.SimpleNamespace(state_dict=lambda: {})),
                patch.object(predictors, "build_predictor_cache", side_effect=build_cache),
                patch.object(
                    predictors, "fit_cached_prior_block_predictors",
                    return_value={"value": {"examples": 1}},
                ),
                patch.object(predictors, "discard_predictor_cache"),
            ):
                self.assertIsNone(backend.existing_success(training_root, row, training_package))
                with self.assertRaisesRegex(RuntimeError, "owner lacks an immutable training-feature receipt"):
                    driver.owner_feature_manifest([row], root=training_root, package=training_package)
                manifest_lookups.clear()

                receipt = driver.fit_pilot_predictors(
                    "D", [row], root=current_root, executor=executor,
                    package=current_package, training_root=training_root,
                    training_package=training_package,
                )

        load.assert_called_once_with([row], root=training_root, executor=executor, package=training_package)
        self.assertEqual(manifest_lookups, [(training_root, training_package, predecessor_lock_sha)])
        self.assertEqual(history_lookups, [(training_root, training_package, predecessor_lock_sha)])
        self.assertEqual(backend.SOURCE_LOCK, driver.SOURCE_LOCK_PATH)
        self.assertEqual(receipt["status"], "SUCCEEDED")

    def test_source_lock_refresh_archives_the_explicit_predecessor_package(self):
        previous_package = "9be126a0a0739676953a55367d47fdef5826e5e1"
        current_package = "a" * 40
        old_lock = b'{"schema_id":"previous-lock"}\n'
        new_lock = {"schema_id": "new-lock", "generated_from_commit": current_package, "local_code_files": {}}
        old_sha = driver.sha256(old_lock)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = root / "study" / "source-lock.json"
            lock_path.parent.mkdir()
            lock_path.write_bytes(old_lock)
            with patch.object(driver, "SOURCE_LOCK_PATH", lock_path):
                result = driver.refresh_source_lock(
                    lock=new_lock, data_root=root / "data",
                    previous_package_commit=previous_package,
                )

            archive = root / "data" / "artifacts" / "source-lock-history" / f"{previous_package}-{old_sha}.json"
            self.assertTrue(archive.is_file())
            self.assertEqual(archive.read_bytes(), old_lock)
            self.assertEqual(result["archived_previous_lock_sha256"], old_sha)
            self.assertFalse(
                (root / "data" / "artifacts" / "source-lock-history" / f"{current_package}-{old_sha}.json").exists()
            )

    def test_c_and_d_actor_updates_are_bound_to_the_same_frozen_package(self):
        for arm in ("C", "D"):
            with self.subTest(arm=arm):
                spec = driver.PILOT_ACTOR_UPDATE_CARRY_FORWARD[arm]
                self.assertEqual(spec["package"], driver.PILOT_RESUME_PACKAGE)
                self.assertEqual(spec["source_lock_sha256"], driver.PILOT_RESUME_SOURCE_LOCK_SHA256)
                self.assertEqual(spec["training_package"], driver.PILOT_RESUME_PACKAGE)
                self.assertEqual(spec["training_source_lock_sha256"], driver.PILOT_RESUME_SOURCE_LOCK_SHA256)
                self.assertEqual(len(spec["progress_sha256"]), 64)
                self.assertEqual(len(spec["receipt_sha256"]), 64)
                self.assertEqual(len(spec["model_sha256"]), 64)
                self.assertEqual(len(spec["checkpoint_sha256"]), 64)
