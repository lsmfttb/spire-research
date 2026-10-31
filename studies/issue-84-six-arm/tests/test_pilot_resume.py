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
