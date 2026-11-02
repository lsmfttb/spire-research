from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

STUDY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDY))

from driver import (
    T_REF,
    FORMAL_PREDECESSOR_LOCK_SHA256,
    FORMAL_PREDECESSOR_PACKAGE,
    carried_formal_model_record,
    install_formal_training_model_carry_forward,
    PILOT_PREDECESSOR_RUNTIME_AST_SHA256,
    _pilot_runtime_fingerprint,
    _terminal_game_reward,
    _validate_carried_attempt,
    _issue73_snapshot_paths,
    bind_issue73_source_paths,
    canonical,
    file_sha256,
    make_task_wall_forecast,
    save_preflight_receipt,
    seed_table_path,
    _preflight_optimizer_evidence,
    call_pinned_issue73_run_one,
    install_issue73_capture_limit_compat,
    install_issue84_heldout_actor_arm_compat,
    refresh_source_lock,
    sha256,
)
import driver as driver_module
from formal import (
    _issue75_policy,
    _new_predictors,
    _new_predecessor_actor,
    _optimizer_parameter_steps_match,
    _prepare_predictors,
    _predictor_state,
    _verify_formal_training_forecast_carry_forward,
    _verify_formal_training_lock_compatibility,
    _verify_saved_game_group,
    paired_bootstrap,
)


class FormalTrainingCarryForwardBindingTests(unittest.TestCase):
    def test_predecessor_actor_dimension_comes_from_locked_model_module(self):
        class Actor:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def eval(self):
                return self

        model_module = SimpleNamespace(
            DEFAULT_HASH_DIMENSION=2048,
            ActionConditionedPolicy=Actor,
        )
        policy = {"initial_model_seed": 75, "actor_final_weight_std": 0.02}

        actor = _new_predecessor_actor(model_module, policy)

        self.assertEqual(
            actor.kwargs,
            {"dimension": 2048, "seed": 75, "final_weight_std": 0.02},
        )

    def test_predecessor_optimizer_steps_match_locked_json_list_shape(self):
        self.assertTrue(
            _optimizer_parameter_steps_match([128] * 6, expected_steps=128, parameter_count=6)
        )
        self.assertTrue(
            _optimizer_parameter_steps_match([64] * 6, expected_steps=64, parameter_count=6)
        )
        for malformed in (
            None,
            {},
            {f"p{i}": 128 for i in range(6)},
            [],
            [128] * 5,
            [128] * 7,
            [128] * 5 + [64],
            [128] * 5 + [True],
        ):
            with self.subTest(malformed=malformed):
                self.assertFalse(
                    _optimizer_parameter_steps_match(
                        malformed, expected_steps=128, parameter_count=6,
                    )
                )

    def test_training_predecessor_archive_is_active_while_saved_group_is_resolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts"
            package = "f" * 40
            current_lock = root / "source-lock.json"
            current_lock.write_bytes(b"current source lock")
            archive = artifacts / "source-lock-history" / "training-lock.json"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"archived training source lock")
            archived_sha = file_sha256(archive)
            result_path = artifacts / "formal" / package / "A" / "result.json"
            result_path.parent.mkdir(parents=True)
            result_path.write_bytes(b"saved success receipt")
            row = {"unit_id": "TRAIN:8400129:A"}
            report = {"model_digest": "actor-digest", "source_lock_sha256": archived_sha}
            result_sha = file_sha256(result_path)
            saved = {
                "status": "SUCCEEDED",
                "run_summary": {
                    "status": "SUCCEEDED",
                    "completed": 1,
                    "outcomes": [{
                        "unit_id": row["unit_id"],
                        "result_sha256": result_sha,
                        "policy_model_digest": report["model_digest"],
                        "source_lock_sha256": archived_sha,
                    }],
                },
            }

            class Backend:
                ARTIFACTS = artifacts
                SOURCE_LOCK = current_lock

                def __init__(self):
                    self.observed_lock_hashes = []

                def source_lock_sha(self):
                    return file_sha256(self.SOURCE_LOCK)

                def existing_success_record(self, _root, _row, _package):
                    active_lock_sha = self.source_lock_sha()
                    self.observed_lock_hashes.append(active_lock_sha)
                    if active_lock_sha != archived_sha:
                        return None
                    return report, result_path

            backend = Backend()
            with (
                patch.object(driver_module, "FORMAL_TRAINING_PREDECESSOR_PACKAGE", package),
                patch.object(driver_module, "FORMAL_TRAINING_PREDECESSOR_LOCK_SHA256", archived_sha),
                patch.object(driver_module, "FORMAL_TRAINING_PREDECESSOR_SOURCE_LOCK_ARCHIVE", archive.name),
                patch.object(driver_module, "configure_backend", return_value=backend),
            ):
                with driver_module._predecessor_source_lock(package):
                    self.assertEqual(backend.SOURCE_LOCK, archive)
                    verified = _verify_saved_game_group(
                        saved, [row], root=artifacts / "formal" / package / "A",
                        package=package, backend=backend,
                    )
                self.assertEqual(backend.SOURCE_LOCK, current_lock)
            self.assertEqual(verified["status"], "SUCCEEDED")
            self.assertEqual(backend.observed_lock_hashes, [archived_sha])

    def test_training_forecast_carry_forward_preserves_the_approved_global_cap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            predecessor, package = "a" * 40, "b" * 40
            predecessor_lock, current_lock = "c" * 64, "d" * 64
            approved = {
                "schema_id": "issue84-installed-task-wall-forecast-v1",
                "status": "APPROVED_SINGLE_GLOBAL_CUMULATIVE_ACTIVE_WALL_CAP",
                "issue_number": 84,
                "study_id": "issue-84-six-arm",
                "source_lock_sha256": predecessor_lock,
                "scope": "STUDY_ONLY",
                "formal_sampling_authorized": True,
                "formal_seeds_started_at_installation": False,
                "one_task_active_wall_allowance_seconds": 204171.592,
                "allowance_basis": {"planner_selected_seconds": 204171.592},
            }
            current = {
                **approved,
                "package_commit": package,
                "source_lock_sha256": current_lock,
            }
            approved["package_commit"] = predecessor
            old_path = root / "pilot" / predecessor / "task-wall-forecast.json"
            new_path = root / "pilot" / package / "task-wall-forecast.json"
            old_path.parent.mkdir(parents=True)
            new_path.parent.mkdir(parents=True)
            old_path.write_bytes(canonical(approved) + b"\n")
            new_path.write_bytes(canonical(current) + b"\n")
            old_progress = {"forecast_sha256": file_sha256(old_path)}
            prior, rebound = _verify_formal_training_forecast_carry_forward(
                predecessor=predecessor,
                predecessor_lock_sha256=predecessor_lock,
                package=package,
                source_lock_sha256=current_lock,
                old_progress=old_progress,
                artifact_root=root,
            )
            self.assertEqual(prior["one_task_active_wall_allowance_seconds"], 204171.592)
            self.assertEqual(rebound["one_task_active_wall_allowance_seconds"], 204171.592)
            current["one_task_active_wall_allowance_seconds"] = 204171.593
            new_path.write_bytes(canonical(current) + b"\n")
            with self.assertRaisesRegex(RuntimeError, "forecast binding differs"):
                _verify_formal_training_forecast_carry_forward(
                    predecessor=predecessor,
                    predecessor_lock_sha256=predecessor_lock,
                    package=package,
                    source_lock_sha256=current_lock,
                    old_progress=old_progress,
                    artifact_root=root,
                )

    def test_predecessor_lock_allows_only_formal_resume_adapter_files(self):
        predecessor = {
            "contract_sha256": "contract",
            "upstream_commit": "upstream",
            "upstream_files": {"driver.py": "a"},
            "issue73_source_snapshot": {"sha256": "issue73"},
            "external_runtime": {"native": "native"},
            "parent": {"pi12": "parent"},
            "fixed_scales": {"t_ref": 1, "s_ref": 2},
            "seed_derivation": "seed-v1",
            "generated_from_commit": "old",
            "generated_utc": "old-time",
            "local_code_files": {"driver.py": "old-driver", "formal.py": "same", "training.py": "same-training", "tests/test_formal.py": "old-tests"},
        }
        current = {
            **predecessor,
            "generated_from_commit": "new",
            "generated_utc": "new-time",
            "local_code_files": {"driver.py": "new-driver", "formal.py": "new-formal", "training.py": "same-training", "tests/test_formal.py": "new-tests"},
        }
        _verify_formal_training_lock_compatibility(predecessor, current)
        current["local_code_files"]["training.py"] = "changed-training"
        with self.assertRaisesRegex(RuntimeError, "outside the held-out/resume adapter"):
            _verify_formal_training_lock_compatibility(predecessor, current)
        current["local_code_files"]["training.py"] = "same-training"
        current["parent"] = {"pi12": "different-parent"}
        with self.assertRaisesRegex(RuntimeError, "contract or locked runtime inputs"):
            _verify_formal_training_lock_compatibility(predecessor, current)

    def test_carried_actor_model_is_bound_to_package_arm_label_and_artifact_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary)
            artifacts = data_root / "artifacts"
            package = "a" * 40
            lock_path = data_root / "source-lock.json"
            lock_path.write_text("current lock", encoding="utf-8")
            source_lock_sha = file_sha256(lock_path)
            formal_root = artifacts / "formal" / package
            active_root = formal_root / "A"
            carried_path = artifacts / "formal" / "d7e7ac5e93255787e6b55e25bd3f1197c396c8c2" / "A" / "models" / "pi13.pt"
            carried_path.parent.mkdir(parents=True)
            carried_path.write_bytes(b"frozen actor bytes")
            entry = {
                "arm": "A",
                "label": "pi13",
                "path": carried_path.relative_to(artifacts).as_posix(),
                "checkpoint_sha256": file_sha256(carried_path),
                "model_digest": "digest",
                "predecessor_package_commit": driver_module.FORMAL_TRAINING_PREDECESSOR_PACKAGE,
                "predecessor_source_lock_sha256": driver_module.FORMAL_TRAINING_PREDECESSOR_LOCK_SHA256,
            }
            bridge_body = {
                "schema_id": "issue84-formal-training-carry-forward-v1",
                "current": {"package_commit": package, "source_lock_sha256": source_lock_sha, "formal_root": f"formal/{package}"},
                "final_policy_models": {"A": {"pi13": entry}, "B": {"pi13": entry}},
            }
            bridge = {**bridge_body, "manifest_sha256": sha256(canonical(bridge_body))}
            bridge_path = formal_root / "formal-training-carry-forward.json"
            bridge_path.parent.mkdir(parents=True)
            bridge_path.write_bytes(canonical(bridge) + b"\n")
            with (
                patch.object(driver_module, "host_path", return_value=data_root),
                patch.object(driver_module, "package_commit", return_value=package),
                patch.object(driver_module, "SOURCE_LOCK_PATH", lock_path),
            ):
                record = carried_formal_model_record(active_root, "pi13", "A")
                self.assertEqual(record["resolved_path"], carried_path)
                self.assertEqual(record["checkpoint_sha256"], file_sha256(carried_path))
                self.assertEqual(record["bridge_sha256"], file_sha256(bridge_path))
                active_root.mkdir(parents=True)

                def original_load_model(label, *, proxy=False):
                    local_path = active_root / "models" / f"{label}.pt"
                    self.assertTrue(local_path.is_symlink())
                    return "actor", "digest", {"source": "issue84-pi13"}

                executor = SimpleNamespace(_load_model=original_load_model)
                token = driver_module._ISSUE84_HELDOUT_ORIGINAL_ARM.set("A")
                try:
                    install_formal_training_model_carry_forward(
                        executor, active_root=active_root, pilot=False,
                    )
                    actor, digest, source = executor._load_model("pi13")
                finally:
                    driver_module._ISSUE84_HELDOUT_ORIGINAL_ARM.reset(token)
                self.assertEqual((actor, digest), ("actor", "digest"))
                self.assertEqual(source["training_carry_forward"]["checkpoint_path"], entry["path"])
                self.assertEqual(
                    (active_root / "models" / "pi13.pt").resolve(), carried_path.resolve(),
                )
                tampered_body = {
                    **bridge_body,
                    "final_policy_models": {"A": {"pi13": {**entry, "arm": "B"}}},
                }
                tampered = {**tampered_body, "manifest_sha256": sha256(canonical(tampered_body))}
                bridge_path.write_bytes(canonical(tampered) + b"\n")
                with self.assertRaisesRegex(RuntimeError, "another registered lineage"):
                    carried_formal_model_record(active_root, "pi13", "A")


class FormalPredictorLifecycleTests(unittest.TestCase):
    def test_legacy_arms_skip_auxiliary_predictor_creation_and_fitting(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "formal.fit_cached_prior_block_predictors",
        ) as fit_predictors:
            root = Path(temporary)
            for arm in "ABC":
                predictors = _new_predictors(arm)
                self.assertEqual(predictors, (None, None, None, None))
                fit, checkpoint_sha = _prepare_predictors(
                    arm, 0, root=root, executor=None, package="test-package",
                    value=predictors[0], value_optimizer=predictors[1],
                    action_value=predictors[2], action_optimizer=predictors[3],
                )
                self.assertEqual(fit["schema_id"], "issue84-no-auxiliary-predictor-v1")
                self.assertEqual(fit["status"], "NOT_APPLICABLE")
                self.assertEqual(fit["arm"], arm)
                self.assertIsNone(checkpoint_sha)
                self.assertEqual(_predictor_state(*predictors), {
                    "value_model_state_dict": None,
                    "value_optimizer_state_dict": None,
                    "action_value_model_state_dict": None,
                    "action_value_optimizer_state_dict": None,
                })
            fit_predictors.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    def test_auxiliary_arms_keep_their_registered_predictor_models(self):
        for arm in "DEF":
            value, value_optimizer, action_value, action_optimizer = _new_predictors(arm)
            self.assertIsNotNone(value)
            self.assertIsNotNone(value_optimizer)
            self.assertEqual(action_value is not None, arm == "E")
            self.assertEqual(action_optimizer is not None, arm == "E")
            self.assertIsNotNone(_predictor_state(
                value, value_optimizer, action_value, action_optimizer,
            )["value_model_state_dict"])


class Issue73EvaluationArmCompatibilityTests(unittest.TestCase):
    def test_training_and_existing_issue73_labels_are_unchanged(self):
        class Controller:
            def __init__(self, *, arm):
                self.arm = arm
                self.rng = SimpleNamespace(
                    seed=None,
                    manual_seed=lambda seed: setattr(self.rng, "seed", seed),
                )

        runner = SimpleNamespace(StudyController=Controller)

        def run_one(**kwargs):
            controller = runner.StudyController(arm=kwargs["arm"])
            return {
                "controller_arm": controller.arm,
                "policy_rng_seed": controller.rng.seed,
                "purpose": kwargs.get("purpose"),
            }

        for arm in ("TRAIN", "BRANCH", "P0", "P1", "E", "SUFFIX"):
            with self.subTest(arm=arm):
                result = call_pinned_issue73_run_one(
                    runner,
                    run_one,
                    arm=arm,
                    policy_rng_seed=987,
                )
                self.assertEqual(result["controller_arm"], arm)
                self.assertEqual(result["policy_rng_seed"], 987)

    def test_issue75_worker_dispatch_preserves_issue84_actor_and_row_identity(self):
        class Controller:
            def __init__(self, *, arm):
                self.arm = arm
                self.rng = SimpleNamespace(
                    seed=None,
                    manual_seed=lambda seed: setattr(self.rng, "seed", seed),
                )

        runner = SimpleNamespace(StudyController=Controller)
        observed_rows = []
        executor = SimpleNamespace()
        executor._arm_policy = lambda _phase, _arm, row: (row["issue84_policy_label"], False)

        def worker(job, _ledger):
            row = job["row"]
            observed_rows.append(dict(row))
            arm = row["arm"]
            # Mirror #75's legacy branch: E selects Expert; every other label
            # loads the adapter-provided registered actor checkpoint.
            if arm == "E":
                policy_source = "legacy-expert"
                checkpoint = None
            else:
                checkpoint, _proxy = executor._arm_policy("heldout", arm, row)
                policy_source = "issue84-actor"

            def run_one(**kwargs):
                controller = runner.StudyController(arm=kwargs["arm"])
                return {
                    "controller_arm": controller.arm,
                    "policy_rng_seed": controller.rng.seed,
                    "purpose": kwargs["purpose"],
                }

            controller_result = call_pinned_issue73_run_one(
                runner,
                run_one,
                arm=arm,
                policy_rng_seed=row["policy_rng_seed"],
                purpose=f"stage1-heldout-{arm}-{row['owner_seed']}",
            )
            return {
                "status": "SUCCEEDED",
                "unit_id": row["unit_id"],
                "arm": arm,
                "policy_source": policy_source,
                "checkpoint": checkpoint,
                "policy_rng_seed": row["policy_rng_seed"],
                "controller": controller_result,
            }

        executor._worker_owner = worker
        install_issue84_heldout_actor_arm_compat(executor)
        for study_arm in ("PARENT", *driver_module.ARMS):
            with self.subTest(study_arm=study_arm):
                label = "pi12" if study_arm == "PARENT" else "pi28"
                row = {
                    "arm": study_arm,
                    "unit_id": f"EVAL-8460001-{study_arm}",
                    "owner_seed": 8_460_001,
                    "issue84_arm": None if study_arm == "PARENT" else study_arm,
                    "issue84_policy_label": label,
                    "policy_rng_seed": 84_600_000 + len(observed_rows),
                    "paired_owner_key": f"eval-{8_460_001}",
                }
                result = executor._worker_owner({"pilot": False, "phase": "heldout", "row": row}, None)
                dispatched = observed_rows[-1]
                self.assertEqual(dispatched["arm"], "BRANCH")
                self.assertEqual(dispatched["issue84_arm"], row["issue84_arm"])
                self.assertEqual(dispatched["issue84_policy_label"], label)
                self.assertEqual(dispatched["policy_rng_seed"], row["policy_rng_seed"])
                self.assertEqual(result["arm"], study_arm)
                self.assertEqual(result["unit_id"], row["unit_id"])
                self.assertEqual(result["policy_source"], "issue84-actor")
                self.assertEqual(result["checkpoint"], label)
                self.assertEqual(result["policy_rng_seed"], row["policy_rng_seed"])
                self.assertEqual(result["controller"]["controller_arm"], "BRANCH")
                self.assertEqual(result["controller"]["policy_rng_seed"], row["policy_rng_seed"])
                self.assertEqual(
                    result["controller"]["purpose"],
                    f"stage1-heldout-{study_arm}-{row['owner_seed']}",
                )

    def test_issue84_worker_compat_does_not_rewrite_training_rows(self):
        seen = []
        executor = SimpleNamespace(
            _worker_owner=lambda job, _ledger: seen.append(dict(job["row"])) or {
                "status": "SUCCEEDED", "unit_id": job["row"]["unit_id"],
                "arm": job["row"]["arm"],
            },
        )
        install_issue84_heldout_actor_arm_compat(executor)
        row = {"arm": "TRAIN", "unit_id": "TRAIN:8400001:E", "issue84_arm": "E"}
        result = executor._worker_owner({"pilot": False, "phase": "training", "row": row}, None)
        self.assertEqual(seen, [row])
        self.assertEqual(result["arm"], "TRAIN")
        pilot_row = {"arm": "E", "unit_id": "EVAL:8490001:E"}
        pilot_result = executor._worker_owner(
            {"pilot": True, "phase": "heldout", "row": pilot_row}, None,
        )
        self.assertEqual(seen[-1], pilot_row)
        self.assertEqual(pilot_result["arm"], "E")

    def test_pinned_issue75_worker_loads_issue84_e_actor_not_legacy_expert(self):
        stage1 = (
            STUDY / "upstream" / "issue-75-no-expert-oracle" / "stage1"
        )
        executor_path = stage1 / "executor.py"
        spec = importlib.util.spec_from_file_location("issue84_test_issue75_executor", executor_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        executor = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = executor
        sys.path.insert(0, str(stage1))
        try:
            spec.loader.exec_module(executor)
        finally:
            sys.path.remove(str(stage1))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = root / "units" / "EVAL-8460001-E" / "attempt-0001" / "native"
            executor.ARTIFACT_ROOT = root
            executor.STUDY_ID = "issue-84-six-arm"
            executor._arm_policy = lambda _phase, _arm, row: (row["issue84_policy_label"], False)
            actor_policy = object()
            loaded_labels = []

            def load_model(label, *, proxy=False):
                loaded_labels.append((label, proxy))
                return actor_policy, "a" * 64, {
                    "source": "issue84-actor",
                    "checkpoint_sha256": "b" * 64,
                    "formal_use": True,
                }

            executor._load_model = load_model
            install_issue84_heldout_actor_arm_compat(executor)

            algorithm = ModuleType("algorithm")
            algorithm.InvocationBudget = object
            algorithm.SEARCH_CPU_CAP_SECONDS = 32.0
            algorithm.SEARCH_CPU_STOP_MARGIN_SECONDS = 0.0
            algorithm.run_search = lambda **_kwargs: None
            algorithm.derive_attempt_seed = lambda *_parts: 1
            algorithm.write_immutable_json = lambda *_args, **_kwargs: None
            runtime = ModuleType("runtime")
            runner_seen = {}

            class Controller:
                def __init__(self, *, arm):
                    self.arm = arm
                    self.rng = SimpleNamespace(
                        seed=None,
                        manual_seed=lambda seed: setattr(self.rng, "seed", seed),
                    )

            runner = SimpleNamespace(StudyController=Controller)

            def run_one(**kwargs):
                controller = runner.StudyController(arm=kwargs["arm"])
                runner_seen.update({
                    "arm": controller.arm,
                    "model": kwargs["model"],
                    "purpose": kwargs["purpose"],
                    "policy_rng_seed": controller.rng.seed,
                })
                return {
                    "terminal": {
                        "outcome": "PLAYER_LOSS", "terminal_floor": 1,
                        "terminal_act": 3, "heart_victory": False,
                        "act2_plus": True, "act3_plus": False,
                        "act4_reached": False,
                    },
                    "completed_transitions": 40,
                }

            runner._run_one = run_one
            runtime.initialize_native_runtime = lambda *_args, **_kwargs: (
                {"source": {
                    "sts_lightspeed_commit": "c" * 40,
                    "stsr_source_commit": "d" * 40,
                    "native_module_sha256": "e" * 64,
                }}, runner, None, None,
            )
            runtime.PolicyAdapter = lambda model: model
            install_issue73_capture_limit_compat(runtime)
            row = {
                "unit_id": "EVAL-8460001-E",
                "owner_seed": 8_460_001,
                "game_seed": 8_460_001,
                "arm": "E",
                "formal": True,
                "paired_owner_key": "eval-8460001",
                "issue84_arm": "E",
                "issue84_policy_label": "pi28",
                "policy_rng_seed": 84_600_001,
                "search_invocation_seeds": [],
            }
            job = {
                "pilot": False,
                "phase": "heldout",
                "row": row,
                "output_dir": str(output),
                "run_id": "EVAL-8460001-E-attempt-0001",
            }
            with patch.dict(sys.modules, {"algorithm": algorithm, "runtime": runtime}):
                result = executor._worker_owner(job, SimpleNamespace(
                    binding=SimpleNamespace(
                        package_commit="f" * 40,
                        source_lock_sha256="1" * 64,
                    ),
                ))

            self.assertEqual(loaded_labels, [("pi28", False)])
            self.assertIs(runner_seen["model"], actor_policy)
            self.assertEqual(runner_seen["arm"], "BRANCH")
            self.assertEqual(runner_seen["policy_rng_seed"], row["policy_rng_seed"])
            self.assertEqual(runner_seen["purpose"], "stage1-heldout-E-8460001")
            self.assertEqual(result["arm"], "E")
            self.assertEqual(result["policy_source"]["source"], "issue84-actor")
            self.assertEqual(result["policy_source"]["checkpoint_sha256"], "b" * 64)


class FormalAnalysisTests(unittest.TestCase):
    def _continuation_bridge_fixture(
        self, artifacts_root: Path,
        data_root_wsl: str = "/mnt/d/DeadlycatCoding/stsrl-data/issue84",
    ):
        predecessor_root = Path("formal/A")
        units = []
        for seed in range(8_400_001, 8_400_129):
            unit_id = f"TRAIN:{seed}:A"
            unit_key = f"TRAIN-{seed}-A"
            attempt_rel = predecessor_root / "units" / unit_key / "attempt-0001"
            attempt = artifacts_root / attempt_rel
            attempt.mkdir(parents=True)
            uncompressed_features = b'{"schema_id":"issue84-test-features-v1"}'
            feature_bytes = zlib.compress(uncompressed_features)
            feature_path = attempt / "training-features.json.zlib"
            feature_path.write_bytes(feature_bytes)
            feature_rel = (Path("units") / unit_key / "attempt-0001" / feature_path.name).as_posix()
            row = {
                "unit_id": unit_id, "owner_seed": seed, "phase": "training",
                "arm": "TRAIN", "formal": True, "block_index": 0,
                "game_seed": seed, "paired_owner_key": f"train-{seed}",
                "issue84_arm": "A", "issue84_policy_label": None,
                "policy_rng_seed": seed,
                "search_invocation_seeds": [seed + index for index in range(4)],
            }
            row_sha = hashlib.sha256(canonical(row)).hexdigest()
            run_id = f"{unit_id}-attempt-0001"
            result = {
                "status": "SUCCEEDED", "unit_id": unit_id,
                "package_commit": driver_module.FORMAL_PREDECESSOR_PACKAGE,
                "runner_code_commit": driver_module.FORMAL_PREDECESSOR_PACKAGE,
                "source_lock_sha256": driver_module.FORMAL_PREDECESSOR_LOCK_SHA256,
                "formal": True, "phase": "training", "arm": "TRAIN",
                "owner_seed": seed, "game_seed": seed,
                "paired_owner_key": f"train-{seed}",
                "policy_source": {
                    "checkpoint_sha256": "6f72fdd60d684e4667f2357ba578f84f6e087a18d4d3944c65bf1e0aabd0cf19",
                    "formal_use": True,
                    "source": "issue82-parent-pi12",
                },
                "model_digest": "7c530520b5e107702575f9c8e612bff70e345b5c73715c33aa16eb75e00653ee",
                "native_source_commit": "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2",
                "stsr_source_commit": "7d172f7c24c9d29429b6946ba44f6b59760b0d96",
                "native_module_sha256": "bb22332a4a7108e8f9691482df2624b3b132d75ca07e1aa7b394b932193e5cad",
                "expert_decisions": 0, "terminal_outcome": "PLAYER_LOSS",
                "resource": {"worker_wall_seconds": 1.0},
                "training_features": {
                    "compression": "zlib",
                    "path": feature_rel,
                    "sha256": hashlib.sha256(feature_bytes).hexdigest(),
                    "uncompressed_sha256": hashlib.sha256(uncompressed_features).hexdigest(),
                },
            }
            result_path = attempt / "result.json"
            result_path.write_bytes(canonical(result))
            result_sha = hashlib.sha256(result_path.read_bytes()).hexdigest()
            started = {
                "unit_id": unit_id, "owner_seed": seed,
                "package_commit": driver_module.FORMAL_PREDECESSOR_PACKAGE,
                "seed_row_sha256": row_sha, "run_id": run_id,
                "started_utc": f"2026-11-01T02:{22 + (seed - 8_400_001) // 60:02d}:{(seed - 8_400_001) % 60:02d}Z",
            }
            (attempt / "started.json").write_bytes(canonical(started))
            finished = {
                "unit_id": unit_id, "status": "SUCCEEDED",
                "result_sha256": result_sha, "run_id": run_id,
            }
            (attempt / "finished.json").write_bytes(canonical(finished))
            job = {
                "schema_id": "issue79-owner-worker-job-v1",
                "unit_id": unit_id, "package_commit": driver_module.FORMAL_PREDECESSOR_PACKAGE,
                "runner_code_commit": driver_module.FORMAL_PREDECESSOR_PACKAGE,
                "source_lock_sha256": driver_module.FORMAL_PREDECESSOR_LOCK_SHA256,
                "run_id": run_id, "row": row, "phase": "training",
                "pilot": False,
                "artifact_root": f"{data_root_wsl}/artifacts/formal/A",
                "attempt_dir": f"{data_root_wsl}/artifacts/{attempt_rel.as_posix()}",
                "result_path": f"{data_root_wsl}/artifacts/{(attempt_rel / 'result.json').as_posix()}",
            }
            (attempt / "worker-job.json").write_bytes(canonical(job))
            units.append({
                "unit_id": unit_id,
                "owner_seed": seed,
                "seed_row_sha256": row_sha,
                "result_path": (attempt_rel / "result.json").as_posix(),
                "training_features_path": (predecessor_root / feature_rel).as_posix(),
                "receipt_sha256": {
                    "result_sha256": file_sha256(result_path),
                    "started_sha256": file_sha256(attempt / "started.json"),
                    "finished_sha256": file_sha256(attempt / "finished.json"),
                    "worker_job_sha256": file_sha256(attempt / "worker-job.json"),
                    "training_features_sha256": file_sha256(feature_path),
                },
            })
        return units

    def test_formal_continuation_is_append_only_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "data"
            artifacts_root = data_root / "artifacts"
            artifacts_root.mkdir(parents=True)
            old_package, new_package = "f" * 40, "a" * 40
            current_lock_sha = file_sha256(driver_module.SOURCE_LOCK_PATH)
            compatible_lock = {
                "contract_sha256": "contract",
                "upstream_commit": "upstream",
                "upstream_files": [],
                "issue73_source_snapshot": {},
                "external_runtime": {},
                "parent": {},
                "fixed_scales": {"t_ref": 1.0, "s_ref": 2.0},
                "seed_derivation": "issue84-v1",
            }
            old_lock_path = artifacts_root / "source-lock-history" / f"{old_package}-placeholder.json"
            old_lock_path.parent.mkdir(parents=True)
            old_lock_path.write_bytes(canonical(compatible_lock) + b"\n")
            old_lock_sha = file_sha256(old_lock_path)
            old_lock_path.rename(old_lock_path.with_name(f"{old_package}-{old_lock_sha}.json"))

            with (
                patch.object(driver_module, "DATA_ROOT_WSL", "/mnt/d/issue84-fixture"),
                patch.object(driver_module, "host_path", return_value=data_root),
                patch.object(driver_module, "FORMAL_PREDECESSOR_PACKAGE", old_package),
                patch.object(driver_module, "FORMAL_PREDECESSOR_LOCK_SHA256", old_lock_sha),
                patch.object(driver_module, "_predecessor_source_lock", side_effect=lambda _package: nullcontext()),
            ):
                bridge_seed_table_path = seed_table_path(new_package)
                current_rows = []
                preview_units = self._continuation_bridge_fixture(
                    artifacts_root, driver_module.DATA_ROOT_WSL,
                )
                for entry in preview_units:
                    result_path = artifacts_root / entry["result_path"]
                    job = json.loads((result_path.parent / "worker-job.json").read_text(encoding="utf-8"))
                    current_rows.append(job["row"])
                current_rows.sort(key=lambda row: int(row["owner_seed"]))
                rows_sha = hashlib.sha256(canonical(current_rows)).hexdigest()
                old_seed_path = seed_table_path(old_package)
                old_seed_path.parent.mkdir(parents=True, exist_ok=True)
                current_seed_table = {
                    "schema_id": "issue84-seed-table-v1",
                    "source_lock_sha256": current_lock_sha,
                    "rows_sha256": rows_sha,
                    "rows": current_rows,
                }
                old_seed_table = {
                    **current_seed_table,
                    "source_lock_sha256": old_lock_sha,
                }
                old_seed_path.write_bytes(canonical(old_seed_table) + b"\n")
                old_seed_sha256 = driver_module.file_sha256(old_seed_path)
                bridge_seed_table_path.parent.mkdir(parents=True, exist_ok=True)
                bridge_seed_table_path.write_bytes(canonical(current_seed_table) + b"\n")

                old_forecast_path = artifacts_root / "pilot" / old_package / "task-wall-forecast.json"
                old_forecast_path.parent.mkdir(parents=True)
                feasibility_comment = "https://github.com/lsmfttb/spire-research/issues/84#issuecomment-6546533491"
                formal_authorization = "https://github.com/lsmfttb/spire-research/issues/84#issuecomment-6552055284"
                prior_clock_sha = "b" * 64
                prior_clock_seconds = 5624.912292028998
                unknown_tail_record = {
                    "status": "UNKNOWN",
                    "treatment": "not zeroed and no duration invented",
                }
                forecast = {
                    "schema_id": "issue84-installed-task-wall-forecast-v1",
                    "package_commit": old_package,
                    "source_lock_sha256": old_lock_sha,
                    "issue_number": 84,
                    "study_id": "issue-84-six-arm",
                    "scope": "STUDY_ONLY",
                    "status": "APPROVED_SINGLE_GLOBAL_CUMULATIVE_ACTIVE_WALL_CAP",
                    "formal_sampling_authorized": True,
                    "formal_seeds_started_at_installation": False,
                    "one_task_active_wall_allowance_seconds": 204171.592,
                    "seed_table_sha256": old_seed_sha256,
                    "allowance_basis": {
                        "planner_selected_seconds": 204171.592,
                        "planner_feasibility_decision": feasibility_comment,
                        "planner_formal_execution_authorization": formal_authorization,
                    },
                    "pilot_wall_carry_forward": {
                        "add_again_at_launch": False,
                        "already_in_global_clock": True,
                        "clock_path": "active-wall-issue84-task.json",
                        "clock_sha256_before_formal_start": prior_clock_sha,
                        "known_cumulative_active_seconds": prior_clock_seconds,
                        "unknown_tail": unknown_tail_record,
                    },
                }
                old_forecast_path.write_bytes(canonical(forecast) + b"\n")
                old_forecast_bytes = old_forecast_path.read_bytes()
                cap_installation_path = old_forecast_path.with_name("formal-wall-cap-installation-v1.json")
                cap_installation = {
                    "schema_id": "issue84-formal-global-wall-cap-installation-v1",
                    "issue_number": 84,
                    "study_id": "issue-84-six-arm",
                    "status": "INSTALLED_BEFORE_FORMAL_SAMPLING",
                    "installed_utc": "2026-11-01T02:20:56Z",
                    "package_commit": old_package,
                    "source_lock_sha256": old_lock_sha,
                    "forecast_path": old_forecast_path.relative_to(artifacts_root).as_posix(),
                    "forecast_sha256": driver_module.file_sha256(old_forecast_path),
                    "seed_table_sha256": old_seed_sha256,
                    "selected_global_allowance_seconds": 204171.592,
                    "planner_feasibility_comment": feasibility_comment,
                    "planner_formal_execution_authorization": formal_authorization,
                    "prior_active_wall_clock_sha256": prior_clock_sha,
                    "prior_cumulative_active_wall_seconds": prior_clock_seconds,
                    "prior_wall_carried_forward_once": True,
                    "unknown_pilot_tail_preserved": True,
                    "unknown_tail_record": unknown_tail_record,
                    "formal_seed_count_at_installation": 0,
                    "formal_root_was_absent_at_installation": True,
                    "no_pilot_checkpoints_used_or_deleted": True,
                }
                cap_installation_path.write_bytes(canonical(cap_installation) + b"\n")
                cap_installation_bytes = cap_installation_path.read_bytes()
                unknown_attempt = (
                    artifacts_root / "pilot" / old_package / "C" / "units"
                    / "TRAIN-8480022-C" / "attempt-0019"
                )
                unknown_attempt.mkdir(parents=True)
                (unknown_attempt / "started.json").write_bytes(b'{"attempt":19}\n')
                (unknown_attempt / "worker-job.json").write_bytes(b'{"owner":"TRAIN-8480022-C"}\n')
                outcomes = [{"unit_id": row["unit_id"]} for row in current_rows]
                old_group = {
                    "schema_id": "issue84-game-group-v1", "status": "SUCCEEDED",
                    "group": "TRAIN-A-BLOCK-00", "expected": 128, "completed": 128,
                    "run_summary": {
                        "status": "SUCCEEDED", "completed": 128, "elapsed_seconds": 12.0,
                        "native_worker_wall_seconds": 128.0, "retry_count": 0,
                        "outcomes": outcomes,
                    },
                }
                old_progress_path = artifacts_root / "formal" / "formal-progress.json"
                old_progress_path.parent.mkdir(parents=True, exist_ok=True)
                old_progress = {
                    "schema_id": "issue84-formal-progress-v1", "status": "IN_PROGRESS",
                    "package_commit": old_package, "source_lock_sha256": old_lock_sha,
                    "seed_table_sha256": file_sha256(old_seed_path),
                    "forecast_sha256": file_sha256(old_forecast_path),
                    "active_stage": "actor-update", "active_arm": "A", "active_block": 0,
                    "training_blocks": {"A": {"0": old_group}},
                }
                old_progress_path.write_bytes(canonical(old_progress) + b"\n")
                old_progress_bytes = old_progress_path.read_bytes()
                clock_path = artifacts_root / "active-wall-issue84-task.json"
                clock_path.write_bytes(canonical({
                    "schema_id": "issue79-active-wall-clock-v1", "mode": "issue84-task",
                    "allowance_seconds": 204171.592, "elapsed_seconds": 6323.316307722998,
                    "active_session": None, "process_ids": [],
                }) + b"\n")

                class Backend:
                    ARTIFACTS = artifacts_root
                    SEED_TABLE = bridge_seed_table_path
                    SOURCE_LOCK = old_lock_path.with_name(f"{old_package}-{old_lock_sha}.json")

                    @staticmethod
                    def unit_key(unit_id):
                        return str(unit_id).replace(":", "-")

                    @staticmethod
                    def _pid_alive(pid):
                        return False

                    @staticmethod
                    def existing_success_record(root, row, package):
                        result_path = Path(root) / "units" / Backend.unit_key(row["unit_id"]) / "attempt-0001" / "result.json"
                        if package != old_package or not result_path.is_file():
                            return None
                        return json.loads(result_path.read_text(encoding="utf-8")), result_path

                    @staticmethod
                    def write_json_once(path, value):
                        return driver_module.write_json_once(path, value)

                backend = Backend()
                with (
                    patch.object(driver_module, "configure_backend", return_value=backend),
                    patch.object(driver_module, "run_summary", return_value=old_group["run_summary"]),
                ):
                    first = driver_module.formal_continuation(
                        package=new_package, source_lock_sha256=current_lock_sha,
                        current_lock=compatible_lock, current_seed_table=current_seed_table, backend=backend,
                    )
                    second = driver_module.formal_continuation(
                        package=new_package, source_lock_sha256=current_lock_sha,
                        current_lock=compatible_lock, current_seed_table=current_seed_table, backend=backend,
                    )
                    self.assertEqual(first["bridge_sha256"], second["bridge_sha256"])
                    clock_bytes = clock_path.read_bytes()
                    interrupted_clock = json.loads(clock_bytes)
                    interrupted_clock["elapsed_seconds"] += 2.0
                    interrupted_clock["active_session"] = "interrupted-session"
                    interrupted_clock["process_ids"] = [999_999]
                    clock_path.write_bytes(canonical(interrupted_clock) + b"\n")
                    recovered = driver_module.formal_continuation(
                        package=new_package, source_lock_sha256=current_lock_sha,
                        current_lock=compatible_lock, current_seed_table=current_seed_table, backend=backend,
                    )
                    self.assertEqual(recovered["bridge_sha256"], first["bridge_sha256"])
                    self.assertEqual(
                        recovered["bridge"]["predecessor_active_wall_clock"]["record"]["elapsed_seconds"],
                        6323.316307722998,
                    )
                    clock_path.write_bytes(clock_bytes)
                    tampered_installation = json.loads(cap_installation_path.read_text(encoding="utf-8"))
                    tampered_installation["selected_global_allowance_seconds"] += 1.0
                    cap_installation_path.write_bytes(canonical(tampered_installation) + b"\n")
                    with self.assertRaisesRegex(
                        RuntimeError, "predecessor forecast and installed global wall-cap receipt are incompatible",
                    ):
                        driver_module.formal_continuation(
                            package=new_package, source_lock_sha256=current_lock_sha,
                            current_lock=compatible_lock, current_seed_table=current_seed_table, backend=backend,
                        )
                    cap_installation_path.write_bytes(cap_installation_bytes)
                    late_installation = json.loads(cap_installation_path.read_text(encoding="utf-8"))
                    late_installation["installed_utc"] = "2026-11-01T02:22:01Z"
                    cap_installation_path.write_bytes(canonical(late_installation) + b"\n")
                    with self.assertRaisesRegex(
                        RuntimeError, "carried A0 start receipt predates or coincides with wall-cap installation",
                    ):
                        driver_module.formal_continuation(
                            package=new_package, source_lock_sha256=current_lock_sha,
                            current_lock=compatible_lock, current_seed_table=current_seed_table, backend=backend,
                        )
                    cap_installation_path.write_bytes(cap_installation_bytes)

                    wrong_receipt_seed_sha = json.loads(cap_installation_path.read_text(encoding="utf-8"))
                    wrong_receipt_seed_sha["seed_table_sha256"] = "c" * 64
                    cap_installation_path.write_bytes(canonical(wrong_receipt_seed_sha) + b"\n")
                    with self.assertRaisesRegex(
                        RuntimeError, "predecessor forecast and installed global wall-cap receipt are incompatible",
                    ):
                        driver_module.formal_continuation(
                            package=new_package, source_lock_sha256=current_lock_sha,
                            current_lock=compatible_lock, current_seed_table=current_seed_table, backend=backend,
                        )
                    cap_installation_path.write_bytes(cap_installation_bytes)

                    wrong_forecast_seed_sha = json.loads(old_forecast_path.read_text(encoding="utf-8"))
                    wrong_forecast_seed_sha["seed_table_sha256"] = "d" * 64
                    old_forecast_path.write_bytes(canonical(wrong_forecast_seed_sha) + b"\n")
                    wrong_forecast_hash = driver_module.file_sha256(old_forecast_path)
                    rebound_cap_receipt = json.loads(cap_installation_path.read_text(encoding="utf-8"))
                    rebound_cap_receipt["forecast_sha256"] = wrong_forecast_hash
                    cap_installation_path.write_bytes(canonical(rebound_cap_receipt) + b"\n")
                    rebound_progress = json.loads(old_progress_path.read_text(encoding="utf-8"))
                    rebound_progress["forecast_sha256"] = wrong_forecast_hash
                    old_progress_path.write_bytes(canonical(rebound_progress) + b"\n")
                    with self.assertRaisesRegex(
                        RuntimeError, "predecessor forecast and installed global wall-cap receipt are incompatible",
                    ):
                        driver_module.formal_continuation(
                            package=new_package, source_lock_sha256=current_lock_sha,
                            current_lock=compatible_lock, current_seed_table=current_seed_table, backend=backend,
                        )
                    old_forecast_path.write_bytes(old_forecast_bytes)
                    cap_installation_path.write_bytes(cap_installation_bytes)
                    old_progress_path.write_bytes(old_progress_bytes)

                    reset_clock = json.loads(clock_bytes)
                    reset_clock["elapsed_seconds"] = 0.0
                    clock_path.write_bytes(canonical(reset_clock) + b"\n")
                    with self.assertRaisesRegex(RuntimeError, "clock lineage is invalid or was reset"):
                        driver_module.formal_continuation(
                            package=new_package, source_lock_sha256=current_lock_sha,
                            current_lock=compatible_lock, current_seed_table=current_seed_table, backend=backend,
                        )
                    clock_path.write_bytes(clock_bytes)
                    first_unit = first["bridge"]["units"][0]
                    result_path = artifacts_root / first_unit["result_path"]
                    job_path = result_path.parent / "worker-job.json"
                    job = json.loads(job_path.read_text(encoding="utf-8"))
                    job["row"]["owner_seed"] += 1
                    job_path.write_bytes(canonical(job))
                    with self.assertRaisesRegex(RuntimeError, "worker-job receipt binding differs"):
                        driver_module.formal_continuation(
                            package=new_package, source_lock_sha256=current_lock_sha,
                            current_lock=compatible_lock, current_seed_table=current_seed_table, backend=backend,
                        )
                    self.assertEqual(old_progress_path.read_bytes(), old_progress_bytes)

                self.assertEqual(first["bridge_sha256"], second["bridge_sha256"])
                self.assertEqual(first["predecessor_group"], old_group)
                self.assertEqual(old_progress_path.read_bytes(), old_progress_bytes)
                self.assertEqual(old_forecast_path.read_bytes(), old_forecast_bytes)
                self.assertEqual(cap_installation_path.read_bytes(), cap_installation_bytes)
                rebound = json.loads(first["forecast_path"].read_text(encoding="utf-8"))
                self.assertEqual(rebound["one_task_active_wall_allowance_seconds"], 204171.592)
                self.assertEqual(
                    rebound["continuation_origin"]["continuation_bridge_sha256"],
                    first["bridge_sha256"],
                )
                self.assertEqual(len(first["bridge"]["units"]), 128)
                self.assertEqual(
                    first["bridge"]["predecessor"]["wall_cap_installation_receipt_path"],
                    cap_installation_path.relative_to(artifacts_root).as_posix(),
                )
                self.assertEqual(
                    first["bridge"]["predecessor"]["wall_cap_installation_receipt_sha256"],
                    driver_module.file_sha256(cap_installation_path),
                )
                self.assertEqual(
                    first["bridge"]["predecessor"]["wall_cap_installation_utc"],
                    cap_installation["installed_utc"],
                )
                self.assertEqual(first["bridge"]["units"][0]["receipt_sha256"].keys(), {
                    "result_sha256", "started_sha256", "finished_sha256",
                    "training_features_sha256", "worker_job_sha256",
                })
                unknown_tail = first["bridge"]["historical_unknown_pilot_tails"][0]
                self.assertEqual(unknown_tail["time_status"], "UNKNOWN_NOT_RECONSTRUCTED")
                self.assertEqual(
                    set(unknown_tail["present_receipt_sha256"]), {"started.json", "worker-job.json"},
                )

    def test_preflight_optimizer_record_keeps_runtime_source_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime_path = Path(temporary) / "issue73" / "public_history_execution.py"
            parent_optimizer_path = Path(temporary) / "parent" / "block-11-step.pt"
            runtime_path.parent.mkdir()
            parent_optimizer_path.parent.mkdir()
            runtime_path.write_bytes(b"locked runtime optimizer")
            parent_optimizer_path.write_bytes(b"parent optimizer checkpoint")
            record = _preflight_optimizer_evidence(
                optimizer_id="issue65-demand-driven-public-history-v1",
                source_sha256="a" * 64,
                runtime_optimizer_path=runtime_path,
                parent_optimizer_path=parent_optimizer_path,
            )
            self.assertEqual(
                record["issue73_runtime_optimizer"]["loaded_path"],
                runtime_path.as_posix(),
            )
            self.assertNotEqual(
                record["issue73_runtime_optimizer"]["loaded_path"],
                parent_optimizer_path.as_posix(),
            )
            self.assertEqual(
                record["parent_optimizer_checkpoint_sha256"],
                sha256(parent_optimizer_path.read_bytes()),
            )

    def test_pilot_reward_manifest_uses_executor_terminal_fields(self):
        self.assertEqual(
            _terminal_game_reward({"heart_victory": True, "terminal_floor": 17}),
            117.0,
        )
        self.assertEqual(
            _terminal_game_reward({"heart_victory": False, "terminal_floor": 9}),
            9.0,
        )

    def test_pilot_carry_forward_is_bound_to_reviewed_episode_adapter(self):
        self.assertEqual(_pilot_runtime_fingerprint(), PILOT_PREDECESSOR_RUNTIME_AST_SHA256)

    def test_carried_pilot_attempt_checks_seed_start_finish_and_feature_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row = {
                "unit_id": "TRAIN-8480001-A", "owner_seed": 8_480_001,
                "game_seed": 8_480_001, "paired_owner_key": "train-8480001",
                "policy_source": {"source": "issue82-parent-pi12", "formal_use": True},
            }
            attempt = root / "units" / row["unit_id"] / "attempt-0001"
            attempt.mkdir(parents=True)
            feature_bytes = b'{"schema_id":"test-features"}'
            import zlib
            compressed = zlib.compress(feature_bytes)
            (attempt / "training-features.json.zlib").write_bytes(compressed)
            report = {
                "status": "SUCCEEDED", "unit_id": row["unit_id"],
                "owner_seed": row["owner_seed"], "phase": "training", "arm": "TRAIN",
                "formal": False, "package_commit": "fbbcae6d55a505ce4c4a12968c7a09abb2f06f3d",
                "runner_code_commit": "fbbcae6d55a505ce4c4a12968c7a09abb2f06f3d",
                "source_lock_sha256": "d23bda9d60572814fbeef8d245246d1cccda74cf4ba565a87d0f1e98f7c49533",
                "game_seed": row["game_seed"], "paired_owner_key": row["paired_owner_key"],
                "policy_source": {
                    "checkpoint_sha256": "6f72fdd60d684e4667f2357ba578f84f6e087a18d4d3944c65bf1e0aabd0cf19",
                    "formal_use": True, "source": "issue82-parent-pi12",
                },
                "model_digest": "7c530520b5e107702575f9c8e612bff70e345b5c73715c33aa16eb75e00653ee",
                "native_source_commit": "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2",
                "stsr_source_commit": "7d172f7c24c9d29429b6946ba44f6b59760b0d96",
                "native_module_sha256": "bb22332a4a7108e8f9691482df2624b3b132d75ca07e1aa7b394b932193e5cad",
                "expert_decisions": 0, "terminal_outcome": "PLAYER_LOSS",
                "training_features": {
                    "compression": "zlib", "path": f"units/{row['unit_id']}/attempt-0001/training-features.json.zlib",
                    "sha256": hashlib.sha256(compressed).hexdigest(),
                    "uncompressed_sha256": hashlib.sha256(feature_bytes).hexdigest(),
                },
            }
            result_path = attempt / "result.json"
            result_path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
            result_sha = hashlib.sha256(result_path.read_bytes()).hexdigest()
            (attempt / "started.json").write_text(json.dumps({
                "unit_id": row["unit_id"], "owner_seed": row["owner_seed"],
                "package_commit": report["package_commit"], "run_id": "attempt-1",
                "seed_row_sha256": hashlib.sha256(json.dumps(
                    row, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")).hexdigest(),
            }, sort_keys=True), encoding="utf-8")
            (attempt / "finished.json").write_text(json.dumps({
                "unit_id": row["unit_id"], "run_id": "attempt-1",
                "status": "SUCCEEDED", "result_sha256": result_sha,
            }, sort_keys=True), encoding="utf-8")
            artifact = _validate_carried_attempt(
                row, root, report, result_path,
                package=report["package_commit"], source_lock_sha256=report["source_lock_sha256"],
            )
            self.assertEqual(artifact["result_sha256"], result_sha)
            started_path = attempt / "started.json"
            started = json.loads(started_path.read_text(encoding="utf-8"))
            started["seed_row_sha256"] = "0" * 64
            started_path.write_text(json.dumps(started, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "start/finish receipts"):
                _validate_carried_attempt(
                    row, root, report, result_path,
                    package=report["package_commit"], source_lock_sha256=report["source_lock_sha256"],
                )

    def test_pinned_issue73_adapter_preserves_issue75_training_inputs_and_nested_runs(self):
        class Controller:
            def __init__(self, *, capture_checkpoints=False, capture_all_roots=False):
                self.capture_checkpoints = capture_checkpoints
                self.capture_all_roots = bool(capture_all_roots)
                self.roots = []
                self.trace = []
                self.rng = SimpleNamespace(manual_seed=lambda value: setattr(self, "policy_seed", value))

        class Runner:
            StudyController = Controller

        runner = Runner()

        def initialize_native_runtime(*args, **kwargs):
            def run_one(**run_kwargs):
                controller = runner.StudyController(
                    capture_checkpoints=run_kwargs["capture_checkpoints"],
                    capture_all_roots=run_kwargs.get("capture_all_roots", False),
                )
                nested_result = None
                if not run_kwargs.get("_nested", False):
                    nested_result = runner._run_one(
                        _nested=True,
                        capture_checkpoints=True,
                        capture_all_roots=True,
                        policy_rng_seed=73,
                    )
                    controller.trace.extend((
                        {
                            "kind": "categorical", "step": 4, "chosen_index": 0,
                            "legal_action_identities": [{"id": "a"}],
                            "projection_digest": "state-a", "matrix": [[1.0, 2.0]],
                        },
                        {"kind": "single_action", "step": 5},
                        {
                            "kind": "categorical", "step": 6, "chosen_index": 1,
                            "legal_action_identities": [{"id": "b"}, {"id": "c"}],
                            "projection_digest": "state-b", "matrix": [[3.0], [4.0]],
                        },
                    ))
                captured = []
                for decision_index in range(10):
                    capture = controller.capture_all_roots or len(controller.roots) < 2
                    if capture:
                        controller.roots.append(decision_index)
                        captured.append(decision_index)
                        callback = run_kwargs.get("root_callback")
                        if callable(callback):
                            callback({"decision_index": decision_index})
                return {
                    "captured": captured,
                    "forwarded": run_kwargs,
                    "policy_seed": getattr(controller, "policy_seed", None),
                    "nested_captured": None if nested_result is None else nested_result["captured"],
                    "nested_policy_seed": None if nested_result is None else nested_result["policy_seed"],
                }

            runner._run_one = run_one
            return {}, runner, object(), object()

        runtime = SimpleNamespace(initialize_native_runtime=initialize_native_runtime)
        install_issue73_capture_limit_compat(runtime)
        _, initialized_runner, _, _ = runtime.initialize_native_runtime()
        original_init = Controller.__init__
        result = initialized_runner._run_one(
            capture_checkpoints=True,
            capture_all_roots=False,
            checkpoint_screens=None,
            root_callback=lambda record: None,
            capture_root_limit=4,
            capture_training_features=True,
            capture_trace=True,
            policy_rng_seed=842,
        )
        self.assertEqual(result["captured"], [0, 1, 2, 3])
        self.assertNotIn("capture_root_limit", result["forwarded"])
        self.assertNotIn("capture_training_features", result["forwarded"])
        self.assertNotIn("policy_rng_seed", result["forwarded"])
        self.assertEqual(result["policy_seed"], 842)
        self.assertEqual(result["nested_captured"], list(range(10)))
        self.assertEqual(result["nested_policy_seed"], 73)
        decisions = result["_volatile_training_decisions"]
        self.assertEqual([row["step"] for row in decisions], [4, 6])
        self.assertEqual(decisions[0]["candidate_features"], [[1.0, 2.0]])
        self.assertEqual(decisions[1]["candidate_features"], [[3.0], [4.0]])
        self.assertNotIn("matrix", decisions[0])
        self.assertIs(Controller.__init__, original_init)

    def test_owner_paired_bootstrap_is_deterministic_and_interpolates(self):
        left = {
            "seed-1": {"owner_seed": 1, "terminal_floor": 5},
            "seed-2": {"owner_seed": 2, "terminal_floor": 9},
            "seed-3": {"owner_seed": 3, "terminal_floor": 3},
        }
        right = {
            "seed-1": {"owner_seed": 1, "terminal_floor": 2},
            "seed-2": {"owner_seed": 2, "terminal_floor": 8},
            "seed-3": {"owner_seed": 3, "terminal_floor": 1},
        }
        first = paired_bootstrap(left, right, label="E-D", seed=184, replicates=500)
        second = paired_bootstrap(left, right, label="E-D", seed=184, replicates=500)
        self.assertEqual(first, second)
        self.assertEqual(first["mean_difference_terminal_floor"], 2.0)
        self.assertEqual(first["paired_owner_count"], 3)
        self.assertEqual(first["bootstrap_replicates"], 500)
        self.assertLessEqual(first["ci95"][0], first["ci95"][1])
        self.assertEqual([row["difference"] for row in first["per_owner_differences"]], [3.0, 1.0, 2.0])

    def test_owner_paired_bootstrap_rejects_unmatched_sets_and_seed_mismatch(self):
        left = {"owner": {"owner_seed": 1, "terminal_floor": 5}}
        with self.assertRaises(ValueError):
            paired_bootstrap(left, {}, label="E-D", seed=1, replicates=10)
        right = {"owner": {"owner_seed": 2, "terminal_floor": 4}}
        with self.assertRaises(ValueError):
            paired_bootstrap(left, right, label="E-D", seed=1, replicates=10)

    def test_full_task_forecast_includes_all_pilot_stages_and_one_global_margin(self):
        progress = {
            "training_runs": {arm: {"elapsed_seconds": 2.0} for arm in "ABCDEF"},
            "evaluation_runs": {group: {"elapsed_seconds": 1.0} for group in ("PARENT", *"ABCDEF")},
            "actor_updates": {arm: {"elapsed_seconds": 0.5} for arm in "ABCDEF"},
            "predictor_fits": {
                "D": {"fit": {
                    "feature_cache_build_elapsed_seconds": 0.1,
                    "value": {"examples": 100, "elapsed_seconds": 0.2},
                }},
                "E": {"fit": {"value": {"examples": 100, "elapsed_seconds": 0.2}, "action_value": {"examples": 100, "elapsed_seconds": 0.2}}},
                "F": {"fit": {"value": {"examples": 100, "elapsed_seconds": 0.2}}},
            },
            "clock_elapsed_before": 0.0,
        }
        forecast = make_task_wall_forecast(
            progress, task_wall=20.0, source_lock_sha256="a" * 64, package="b" * 40,
        )
        expected_predictor_rows = round(T_REF * 15_360 * 4)
        self.assertEqual(forecast["formal_predictor_training_rows_estimated_from_t_ref"], expected_predictor_rows)
        self.assertEqual(forecast["pilot_predictor_cache_build_wall_seconds"], 0.1)
        self.assertAlmostEqual(forecast["pilot_predictor_fit_wall_seconds"], 0.9)
        self.assertGreater(forecast["estimated_all_in_active_wall_seconds"], 20.0)
        self.assertEqual(
            forecast["one_task_active_wall_allowance_seconds"],
            forecast["estimated_all_in_active_wall_seconds"] * 1.75,
        )

    def test_preflight_receipt_is_immutable_and_idempotent(self):
        receipt = {
            "schema_id": "issue84-native-preflight-v1",
            "status": "SUCCEEDED_NO_GAMES_STARTED",
            "source_commit": "a" * 40,
            "source_lock_sha256": "b" * 64,
            "native_module_sha256": "c" * 64,
        }
        with tempfile.TemporaryDirectory() as temporary:
            first = save_preflight_receipt(receipt, data_root=Path(temporary))
            self.assertEqual(save_preflight_receipt(receipt, data_root=Path(temporary)), first)
            with self.assertRaisesRegex(FileExistsError, "differs"):
                save_preflight_receipt({**receipt, "native_module_sha256": "d" * 64}, data_root=Path(temporary))
            self.assertEqual(first["status"], "SUCCEEDED_NO_GAMES_STARTED")

    def test_source_files_excludes_generated_cache_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
            (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / ".pytest_cache").mkdir()
            (root / ".pytest_cache" / ".gitignore").write_text("*\n", encoding="utf-8")
            (root / ".pytest_cache" / "lastfailed.json").write_text("{}\n", encoding="utf-8")
            (root / "nested" / "__pycache__").mkdir(parents=True)
            (root / "nested" / "__pycache__" / "generated.py").write_text("CACHE = True\n", encoding="utf-8")

            with patch.object(driver_module, "STUDY", root):
                files = driver_module._source_files()

        self.assertEqual(set(files), {".gitignore", "source.py"})

    def test_source_lock_refresh_archives_previous_bytes_before_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = root / "study" / "source-lock.json"
            lock_path.parent.mkdir()
            previous = b'{"source_lock":"previous"}\n'
            lock_path.write_bytes(previous)
            replacement = {
                "schema_id": "issue84-source-lock-v1", "revision": 2,
                "local_code_files": {"driver.py": "a" * 64},
            }
            with patch("driver.SOURCE_LOCK_PATH", lock_path), patch.dict(
                "os.environ", {"ISSUE84_PACKAGE_COMMIT": "4e25bf1bec80dbdc119c490f31155ee2996726ea"},
            ):
                receipt = refresh_source_lock(lock=replacement, data_root=root / "data")
                archive = Path(receipt["archived_previous_lock"])
                self.assertEqual(archive.read_bytes(), previous)
                self.assertEqual(receipt["archived_previous_lock_sha256"], sha256(previous))
                self.assertEqual(
                    lock_path.read_bytes(),
                    json.dumps(
                        replacement, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False,
                    ).encode("utf-8") + b"\n",
                )
                retry = refresh_source_lock(lock=replacement, data_root=root / "data")
                self.assertIsNone(retry["archived_previous_lock"])

    def test_clean_worker_import_path_contains_locked_preflight_dependency(self):
        upstream75 = STUDY / "upstream" / "issue-75-no-expert-oracle"
        relative = "issue-75-no-expert-oracle/preflight.py"
        manifest = json.loads(
            (STUDY / "upstream" / "upstream-manifest.json").read_text(encoding="utf-8")
        )
        record = manifest["files"][relative]
        module_path = upstream75 / "preflight.py"
        self.assertEqual(
            hashlib.sha256(module_path.read_bytes()).hexdigest(), record["sha256"],
        )

        # Worker subprocesses start with a clean interpreter and reconstruct the
        # #75 import roots in load_modules; exercise that exact module lookup.
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "root = Path(sys.argv[1])\n"
            "sys.path[:0] = [str(root), str(root / 'stage1')]\n"
            "import preflight\n"
            "assert Path(preflight.__file__).resolve() == (root / 'preflight.py').resolve()\n"
            "assert preflight.load_json(preflight.CONFIG_PATH)\n"
        )
        subprocess.run(
            [sys.executable, "-c", code, str(upstream75)],
            cwd=STUDY,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_clean_worker_loads_locked_issue75_model_through_driver(self):
        upstream75 = STUDY / "upstream" / "issue-75-no-expert-oracle"
        relative = "issue-75-no-expert-oracle/model.py"
        manifest = json.loads((
            STUDY / "upstream" / "upstream-manifest.json"
        ).read_text(encoding="utf-8"))
        model_path = upstream75 / "model.py"
        self.assertEqual(
            hashlib.sha256(model_path.read_bytes()).hexdigest(),
            manifest["files"][relative]["sha256"],
        )

        # Reproduce a worker interpreter with no generic #75 import roots or
        # cached model module; the pinned driver must load the locked model.
        code = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "import driver\n"
            "os.environ[\"ISSUE84_PACKAGE_COMMIT\"] = \"0000000000000000000000000000000000000000\"\n"
            "assert \"model\" not in sys.modules\n"
            "assert not any(\"issue-75-no-expert-oracle\" in p for p in sys.path)\n"
            "os.environ[\"STS_LIGHTSPEED_SOURCE_DIR\"] = driver.STS_SOURCE_WSL\n"
            "os.environ[\"STSRL_SOURCE_DIR\"] = driver.STSRL_SOURCE_WSL\n"
            "root = Path(sys.argv[1])\n"
            "root.mkdir(parents=True, exist_ok=True)\n"
            "modules = driver.load_modules(root, pilot=False)\n"
            "model = modules[2]\n"
            "assert Path(model.__file__).resolve() == (driver.UPSTREAM75 / \"model.py\").resolve()\n"
            "assert callable(model.ActionConditionedPolicy) and callable(model.model_digest)\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, "-c", code, str(Path(temporary) / "active")],
                cwd=STUDY,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_formal_policy_comes_from_locked_issue75_root_config(self):
        upstream75 = STUDY / "upstream" / "issue-75-no-expert-oracle"
        config_path = upstream75 / "config.json"
        relative = "issue-75-no-expert-oracle/config.json"
        manifest = json.loads((
            STUDY / "upstream" / "upstream-manifest.json"
        ).read_text(encoding="utf-8"))
        self.assertFalse((STUDY / "config.json").exists())
        self.assertEqual(
            hashlib.sha256(config_path.read_bytes()).hexdigest(),
            manifest["files"][relative]["sha256"],
        )
        config = json.loads(config_path.read_text(encoding="utf-8"))
        policy = _issue75_policy()
        self.assertEqual(policy, config["policy"])
        self.assertEqual(policy["initial_model_seed"], 73507501)
        self.assertEqual(policy["actor_final_weight_std"], 0.01)

    def test_issue75_lineage_inputs_are_vendored_and_locked(self):
        upstream = STUDY / "upstream"
        manifest = json.loads(
            (upstream / "upstream-manifest.json").read_text(encoding="utf-8")
        )
        for name in ("issue74-root-selection-lock.json", "root-provenance.json"):
            relative = f"issue-75-no-expert-oracle/{name}"
            path = upstream / relative
            self.assertTrue(path.is_file(), relative)
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                manifest["files"][relative]["sha256"],
            )

    def test_issue73_source_binding_rebases_the_vendored_runtime_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary)
            runtime = SimpleNamespace()
            preflight = SimpleNamespace()
            bind_issue73_source_paths(runtime, preflight, snapshot)
            issue73 = snapshot / "studies" / "issue-73-privileged-noncombat-oracle-stage0"
            self.assertEqual(runtime.ISSUE73_DIR, issue73)
            self.assertEqual(runtime.ISSUE73_RUNNER_PATH, issue73 / "runner.py")
            self.assertEqual(
                runtime.ISSUE73_RUNTIME_OPTIMIZER_PATH,
                issue73 / "public_history_execution.py",
            )
            self.assertEqual(runtime.ISSUE73_POLICY_PATH, issue73 / "policy.py")
            self.assertEqual(runtime.PI_INIT_PATH, issue73 / "receipts/stage0/policies/pi_init.pt")
            self.assertEqual(preflight.ROOTS, issue73 / "receipts/stage0/roots")
            self.assertEqual(preflight.OWNER_RUNS, issue73 / "receipts/stage0/debug-owner-runs")
            self.assertEqual(
                preflight.SELECTION_PATH,
                STUDY / "upstream" / "issue-75-no-expert-oracle" / "issue74-root-selection-lock.json",
            )

    def test_issue73_snapshot_includes_the_local_locked_runtime_optimizer(self):
        config = json.loads(
            (STUDY / "upstream" / "issue-75-no-expert-oracle" / "config.json").read_text()
        )
        self.assertIn("public_history_execution.py", _issue73_snapshot_paths(config))

    def test_seed_table_path_is_versioned_by_source_package(self):
        first = seed_table_path("6c30f045f3d308731561d22b3706d99520b7d0a2")
        repaired = seed_table_path("a0d6a6a4e26688db12a40f8d261ec55a88bb5631")
        self.assertNotEqual(first, repaired)
        self.assertEqual(repaired.name, "seed-table.json")
        self.assertIn("a0d6a6a4e26688db12a40f8d261ec55a88bb5631", repaired.parts)


if __name__ == "__main__":
    unittest.main()
