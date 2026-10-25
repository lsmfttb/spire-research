from __future__ import annotations

import ast
import hashlib
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from types import ModuleType
from unittest.mock import patch

import algorithm
import preflight
import runtime
import stage0


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
    def test_immutable_json_receipt_accepts_crlf_but_never_overwrites_content(self) -> None:
        with tempfile.TemporaryDirectory(dir=STUDY) as temp_dir:
            path = Path(temp_dir) / "receipt.json"
            original = b'{\r\n  "status": "PASS"\r\n}\r\n'
            path.write_bytes(original)
            preflight.write_locked_file(path, {"status": "PASS"})
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(FileExistsError):
                preflight.write_locked_file(path, {"status": "DIFFERENT"})
            self.assertEqual(path.read_bytes(), original)

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

    def test_stage0_attempt_directories_are_source_scoped_and_contained(self) -> None:
        attempt = stage0._attempt_dir_for_id("source-5624600b-a20e8012")
        self.assertEqual(
            attempt,
            (STUDY / "stage0" / "attempts" / "source-5624600b-a20e8012").resolve(),
        )
        for invalid in ("", "../outside", "nested/name", "attempt.with.dot"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                stage0._attempt_dir_for_id(invalid)
        with patch.dict(os.environ, {stage0.STAGE0_ATTEMPT_ENV: str(attempt / "nested")}):
            with self.assertRaises(RuntimeError):
                stage0._stage0_dir_from_environment()

    def test_run_debug_defaults_to_a_source_and_lock_scoped_attempt(self) -> None:
        prior_state = (stage0.STAGE0_DIR, stage0.RECEIPTS, stage0.NATIVE_OUTPUT)
        prior_env = os.environ.get(stage0.STAGE0_ATTEMPT_ENV)
        source_commit = "123456789abcdef0123456789abcdef012345678"
        lock_sha = "a" * 64
        try:
            with (
                patch.object(stage0, "_load_locked_state", return_value=({}, {}, lock_sha, source_commit)),
                patch.object(stage0, "run_stage0", return_value={"status": "STAGE0_NONFORMAL_REVIEWABLE_COMPLETE"}),
                patch.object(sys, "argv", ["stage0.py", "--run-debug"]),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(stage0.main(), 0)
            self.assertEqual(
                stage0.STAGE0_DIR,
                (STUDY / "stage0" / "attempts" / "source-12345678-aaaaaaaa").resolve(),
            )
        finally:
            stage0.STAGE0_DIR, stage0.RECEIPTS, stage0.NATIVE_OUTPUT = prior_state
            if prior_env is None:
                os.environ.pop(stage0.STAGE0_ATTEMPT_ENV, None)
            else:
                os.environ[stage0.STAGE0_ATTEMPT_ENV] = prior_env

    def test_incomplete_receipts_keep_bounded_static_exception_trace_only(self) -> None:
        try:
            raise RuntimeError("private detail must not enter a receipt")
        except RuntimeError as exc:
            trace = algorithm.safe_exception_trace(exc)
        self.assertTrue(trace)
        self.assertLessEqual(len(trace), 8)
        self.assertEqual(trace[-1][0], Path(__file__).name)
        self.assertEqual(trace[-1][1], "test_incomplete_receipts_keep_bounded_static_exception_trace_only")
        trial = algorithm.TrialResult(
            attempt_index=1, complete=False, failure_boundary="native_continuation_error:RuntimeError",
            failure_trace=trace,
        )
        expected = [
            {"file": filename, "function": function, "line": line}
            for filename, function, line in trace
        ]
        for receipt in (runtime._safe_trial_payload(trial), stage0._trial_receipt(trial)):
            self.assertEqual(receipt["failure_trace"], expected)
            self.assertNotIn("private detail", json.dumps(receipt))
            self.assertNotIn(str(STUDY.parent), json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "frame limit"):
            algorithm.safe_exception_trace(RuntimeError(), max_frames=0)

    def test_worker_reuses_only_matching_parent_reviewer_receipt(self) -> None:
        config = json.loads((STUDY / "config.json").read_text(encoding="utf-8"))
        gate = config["authorization"]["reviewer_gate"]
        source_commit = "a" * 40
        lock_sha = "b" * 64
        login = str(gate["owner_login"])
        receipt = {
            "source_commit": source_commit,
            "source_lock_sha256_lf": lock_sha,
            "reviewer_comment_id": 6397788284,
            "reviewer_login": login,
            "reviewer_authentication": {
                "identity_mode": gate["required_identity_mode"],
                "reviewer_login": login,
            },
            "status": "AUTHORIZED_STAGE0_NONFORMAL_ONLY",
            "formal_training_seed_consumed": False,
            "formal_evaluation_seed_consumed": False,
        }
        proof = stage0._reviewer_proof_from_parent_receipt(
            receipt, source_commit=source_commit, lock_sha=lock_sha, config=config,
        )
        self.assertEqual(proof["comment_id"], receipt["reviewer_comment_id"])
        self.assertEqual(proof["reviewer_authentication"], receipt["reviewer_authentication"])
        for key, invalid in (
            ("source_commit", "c" * 40),
            ("source_lock_sha256_lf", "d" * 64),
            ("reviewer_comment_id", 0),
            ("reviewer_login", "unconfigured-user"),
            ("status", "PENDING"),
            ("formal_training_seed_consumed", True),
            ("reviewer_authentication", {"identity_mode": "other"}),
        ):
            changed = dict(receipt)
            changed[key] = invalid
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                stage0._reviewer_proof_from_parent_receipt(
                    changed, source_commit=source_commit, lock_sha=lock_sha, config=config,
                )

    def test_worker_auth_gate_error_parser_keeps_only_known_class_marker(self) -> None:
        self.assertEqual(
            stage0._worker_authorization_gate_error(
                b"ISSUE75_ROOT_AUTHORIZATION_GATE_REJECTED:URLError\nsecret detail\n",
            ),
            "URLError",
        )
        self.assertEqual(
            stage0._worker_authorization_gate_error(
                b"x" * 8192 + b"\nISSUE75_ROOT_AUTHORIZATION_GATE_REJECTED:RuntimeError\n",
            ),
            "RuntimeError",
        )
        self.assertIsNone(stage0._worker_authorization_gate_error(b"secret detail\n"))
        self.assertIsNone(stage0._worker_authorization_gate_error(
            b"ISSUE75_ROOT_AUTHORIZATION_GATE_REJECTED:URLError:secret\n",
        ))

    def test_root_worker_does_not_repeat_github_reviewer_lookup(self) -> None:
        tree = ast.parse((STUDY / "stage0.py").read_text(encoding="utf-8"))
        worker = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_root_worker"
        )
        calls = [
            node.func.id if isinstance(node.func, ast.Name)
            else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            for node in ast.walk(worker) if isinstance(node, ast.Call)
        ]
        self.assertNotIn("reviewer_pass_proof", calls)
        runtime_call = next(
            node for node in ast.walk(worker)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "initialize_native_runtime"
        )
        reviewer_proof = next(
            keyword.value for keyword in runtime_call.keywords
            if keyword.arg == "reviewer_proof"
        )
        self.assertIsInstance(reviewer_proof, ast.Name)
        self.assertEqual(reviewer_proof.id, "proof")

    def test_root_runtime_reuses_exact_parent_proof_without_github_lookup(self) -> None:
        config = preflight.load_json(STUDY / "config.json")
        native_path = STUDY / "runtime.py"
        fake_module = ModuleType("fake_native")
        fake_module.__file__ = str(native_path)
        env = {"identity": {"native_source_commit": config["source"]["sts_lightspeed_commit"]}}
        fake_runner = type("FakeRunner", (), {
            "_setup": lambda self, output_dir, *, native_module_path: (
                env, fake_module, object(), {"runtime": "test"},
            ),
        })()
        proof = {"proof": "already-validated-by-root-worker"}
        with tempfile.TemporaryDirectory(dir=STUDY) as temp_dir:
            with (
                patch.object(runtime, "_validate_parent_sampling_proof") as validate_proof,
                patch.object(runtime, "verify_source_repositories"),
                patch.object(runtime, "verify_native_module", return_value=native_path),
                patch.object(runtime, "binary_sha256", return_value=config["source"]["native_module_sha256"]),
                patch.object(runtime, "load_issue73_runner", return_value=fake_runner),
                patch.object(preflight, "reviewer_pass_proof", side_effect=AssertionError("network lookup repeated")),
            ):
                result = runtime.initialize_native_runtime(
                    Path(temp_dir) / "runtime", sampling=True, reviewer_proof=proof,
                )
        validate_proof.assert_called_once_with(proof)
        self.assertEqual(result[0], config)

    def test_parent_sampling_proof_validation_keeps_source_lock_binding(self) -> None:
        config = preflight.load_json(STUDY / "config.json")
        source_commit = "a" * 40
        lock_sha = "b" * 64
        login = config["authorization"]["reviewer_gate"]["owner_login"]
        proof = {
            "comment_id": 12345,
            "reviewer_login": login,
            "reviewer_authentication": {
                "identity_mode": config["authorization"]["reviewer_gate"]["required_identity_mode"],
                "reviewer_login": login,
            },
            "source_commit": source_commit,
            "source_lock_sha256_lf": lock_sha,
            "formal_training_enabled": False,
            "formal_evaluation_enabled": False,
        }
        with (
            patch.object(preflight, "verify_source_lock", return_value={
                "formal_training_enabled": False, "formal_evaluation_enabled": False,
            }),
            patch.object(preflight, "git", return_value=source_commit),
            patch.object(preflight, "normalized_sha256", return_value=lock_sha),
            patch.object(preflight, "reviewer_pass_proof", side_effect=AssertionError("network lookup repeated")),
        ):
            runtime._validate_parent_sampling_proof(proof)
        with (
            patch.object(preflight, "verify_source_lock", return_value={
                "formal_training_enabled": False, "formal_evaluation_enabled": False,
            }),
            patch.object(preflight, "normalized_sha256", return_value=lock_sha),
            patch.object(preflight, "git", return_value="c" * 40),
        ):
            with self.assertRaisesRegex(RuntimeError, "does not match the locked Stage-0 source"):
                runtime._validate_parent_sampling_proof(proof)

    def test_native_checkpoint_owner_adapter_preserves_adapter_checkpoint_contract(self) -> None:
        class FakeSimulator:
            def capture_checkpoint(self) -> tuple[str]:
                return ("native-checkpoint",)

            def restore_checkpoint(self, checkpoint: object) -> tuple[str, object]:
                return ("native-restored", checkpoint)

        simulator = FakeSimulator()
        class FakeAdapter:
            _sim = simulator

            def capture_checkpoint(self, snapshot: object) -> tuple[str, object, object]:
                return ("adapter-checkpoint", snapshot, self._sim.capture_checkpoint())

            def restore_checkpoint(self, checkpoint: tuple[str, object, object]) -> tuple[str, object, object]:
                return ("adapter-restored", checkpoint, self._sim.restore_checkpoint(checkpoint[2]))

            def close(self) -> str:
                return "closed"

        class FakeRunner:
            @staticmethod
            def native_simulator(adapter: object) -> object:
                return getattr(adapter, "_sim")

        adapter = runtime.NativeCheckpointOwnerAdapter(FakeRunner(), FakeAdapter())
        snapshot = object()
        checkpoint = adapter.capture_checkpoint(snapshot)
        self.assertEqual(checkpoint, ("adapter-checkpoint", snapshot, ("native-checkpoint",)))
        self.assertEqual(
            adapter.restore_checkpoint(checkpoint),
            ("adapter-restored", checkpoint, ("native-restored", ("native-checkpoint",))),
        )
        self.assertEqual(adapter.close(), "closed")

    def test_incomplete_trial_receipt_keeps_only_safe_missing_attribute_detail(self) -> None:
        error = AttributeError(
            "do not persist this private message", name="capture_checkpoint", obj=object(),
        )
        detail = algorithm.safe_exception_detail(error)
        trial = algorithm.TrialResult(
            attempt_index=1, complete=False,
            failure_boundary="native_continuation_error:AttributeError",
            failure_detail=detail,
        )
        payload = runtime._safe_trial_payload(trial)
        receipt = stage0._trial_receipt(trial)
        expected = {
            "kind": "missing_attribute",
            "exception_type": "AttributeError",
            "missing_attribute": "capture_checkpoint",
        }
        self.assertEqual(payload["failure_detail"], expected)
        self.assertEqual(receipt["failure_detail"], expected)
        self.assertNotIn("private message", json.dumps(payload))
        self.assertNotIn("private message", json.dumps(receipt))
        self.assertEqual(stage0._decode_trial(payload).failure_detail, detail)

    def test_diagnostic_unavailable_is_not_classified_as_uninformative(self) -> None:
        self.assertEqual(
            stage0._diagnostic_classification("UNAVAILABLE_VOLATILE_TRAINING_PAYLOAD_NOT_RETAINED"),
            "DIAGNOSTIC_SIGNAL_UNAVAILABLE",
        )
        self.assertEqual(
            stage0._diagnostic_classification("DIAGNOSTIC_SIGNAL_UNAVAILABLE_BASE_TERMINALS_INCOMPLETE"),
            "DIAGNOSTIC_SIGNAL_UNAVAILABLE",
        )
        self.assertEqual(
            stage0._diagnostic_classification("REAL_RETURN_SIGNAL_UNINFORMATIVE_NO_ELITES"),
            "REAL_RETURN_SIGNAL_UNINFORMATIVE",
        )
        self.assertIsNone(stage0._diagnostic_classification("DIAGNOSTIC_TRAINING_COMPLETE"))

    def test_checkpoint_adapter_stays_volatile_through_decision_capture(self) -> None:
        import numpy as np

        adapter = object()
        checkpoint = object()
        root_row = {
            "adapter": adapter, "checkpoint": checkpoint, "decision_index": 0,
            "policy_decision_index": 0, "screen": "synthetic-test-only",
            "legal_action_identities": ({"card": "a"}, {"card": "b"}),
            "native_state_sha256": "state", "public_projection_sha256": "projection",
            "public_resource_state_sha256": "resource", "projection_digest": "projection-digest",
            "feature_matrix": np.zeros((2, 2), dtype=np.float32),
        }
        captured = runtime._safe_root_capture(root_row)
        self.assertIs(captured["adapter"], adapter)

        trace = [{
            "kind": "categorical", "step": 0, "chosen_index": 0,
            "legal_action_identities": root_row["legal_action_identities"],
            "probabilities": [0.5, 0.5],
        }]
        decisions = runtime._decision_rows(
            runner=object(),
            result={"_volatile_roots": [dict(root_row)], "decisions": trace},
            is_root_run=True,
        )
        self.assertIs(decisions[0].adapter, adapter)
        packed = runtime._hash_only_decisions(decisions)
        self.assertNotIn("adapter", packed[0])
        self.assertNotIn("checkpoint", packed[0])
        self.assertEqual(set(packed[0]), {
            "step_index", "state_fingerprint", "legal_identity_sha256", "chosen_index",
            "candidate_features_f32le_zlib_base64", "candidate_features_sha256",
            "candidate_features_shape", "is_root",
        })

    def test_root_replay_keeps_the_adapter_that_created_its_checkpoint(self) -> None:
        import numpy as np

        config = json.loads((STUDY / "config.json").read_text(encoding="utf-8"))
        identities = ({"card": "a"}, {"card": "b"})
        matrix = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype="<f4")
        adapter_rows = []

        class FakeSimulator:
            def capture_checkpoint(self):
                return ("checkpoint",)

            def restore_checkpoint(self, checkpoint):
                return ("restored", checkpoint)

        class FakeAdapter:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.closed = False
                self.simulator = FakeSimulator()
                adapter_rows.append(self)

            def capture_checkpoint(self, snapshot):
                return ("adapter-checkpoint", snapshot)

            def restore_checkpoint(self, checkpoint):
                return ("adapter-restored", checkpoint)

            def close(self):
                self.closed = True

        native_package = ModuleType("sts_combat_rl")
        native_package.__path__ = []
        sim_package = ModuleType("sts_combat_rl.sim")
        sim_package.__path__ = []
        lightspeed_module = ModuleType("sts_combat_rl.sim.lightspeed")
        lightspeed_module.LightSpeedAdapter = FakeAdapter
        modules = {
            "sts_combat_rl": native_package,
            "sts_combat_rl.sim": sim_package,
            "sts_combat_rl.sim.lightspeed": lightspeed_module,
        }
        expected = {
            "root_id": "root-test", "owner_decision_index": 0, "policy_decision_index": 0,
            "screen": "synthetic-test-only", "legal_action_identities_sha256": hashlib.sha256(
                runtime.canonical(list(identities)).encode("utf-8")
            ).hexdigest(),
            "feature_matrix_sha256": runtime.feature_matrix_sha256(matrix),
            "projection_digest": "projection-digest",
            "root_fingerprints": {
                "native_state_sha256": "state", "public_projection_sha256": "projection",
                "public_resource_state_sha256": "resource",
            },
        }
        checkpoint = object()
        row = {
            "checkpoint": checkpoint, "decision_index": 0, "policy_decision_index": 0,
            "screen": "synthetic-test-only", "legal_action_identities": identities,
            "native_state_sha256": "state", "public_projection_sha256": "projection",
            "public_resource_state_sha256": "resource", "projection_digest": "projection-digest",
            "feature_matrix": matrix,
        }
        captured = {}

        class FakeRunner:
            def native_simulator(self, adapter):
                return adapter.simulator

            def _load_policy(self, *args, **kwargs):
                return object(), "policy-digest"

            def _run_one(self, **kwargs):
                captured.update(kwargs)
                row["adapter"] = kwargs["adapter_override"]
                kwargs["root_callback"](row)

        with patch.object(runtime, "binary_sha256", return_value=config["source"]["issue73_pi_init_sha256"]):
            with patch.dict(sys.modules, modules):
                root = runtime.replay_locked_root(
                    runner=FakeRunner(), module=object(), encoder=object(), output_dir=STUDY,
                    owner_seed=17, expected=expected,
                )

        self.assertEqual(len(adapter_rows), 1)
        self.assertIs(captured["adapter_override"]._adapter, adapter_rows[0])
        self.assertIs(root.adapter, captured["adapter_override"])
        self.assertIs(root.checkpoint, checkpoint)
        self.assertEqual(root.adapter.capture_checkpoint("snapshot"), ("adapter-checkpoint", "snapshot"))
        self.assertFalse(adapter_rows[0].closed)

    def test_native_continuations_reuse_the_adapter_that_owns_each_checkpoint(self) -> None:
        class FakeAdapter:
            def restore_checkpoint(self, checkpoint: object) -> tuple[str, object]:
                return ("restored", checkpoint)

            def capture_checkpoint(self, snapshot: object) -> tuple[str, object]:
                return ("captured", snapshot)

        root_adapter = FakeAdapter()
        root_checkpoint = object()
        root = runtime.LockedRoot(
            owner_seed=17, root_id="root-test", decision_index=0, policy_decision_index=0,
            screen="synthetic-test-only", checkpoint=root_checkpoint, adapter=root_adapter,
            legal_identities=({"card": "a"}, {"card": "b"}), fingerprints={},
            projection_digest="projection", feature_matrix_sha256="features",
        )

        def run_and_capture(*, root_value, mutation_target=None, parent_trial=None):
            captured = {}

            class FakeRunner:
                def _run_one(self, **kwargs):
                    captured.update(kwargs)
                    raise ValueError("checkpoint belongs to a different simulator adapter")

            with patch.object(runtime, "model_digest", return_value="test-policy-digest"):
                trial = runtime.execute_policy_trajectory(
                    runner=FakeRunner(), module=object(), encoder=object(), output_dir=STUDY,
                    policy=object(), owner_seed=17, root=root_value, attempt_index=1,
                    invocation_seed=0, root_action_index=0 if mutation_target is None else None,
                    mutation_target=mutation_target, parent_trial=parent_trial,
                )
            return captured, trial

        captured, trial = run_and_capture(root_value=root)
        start_adapter = captured["adapter_override"]
        self.assertIsInstance(start_adapter, runtime.CheckpointStartAdapter)
        self.assertIs(start_adapter._adapter, root_adapter)
        self.assertIsNone(captured["checkpoint"])
        self.assertEqual(start_adapter.reset(seed=17), ("restored", root_checkpoint))
        self.assertEqual(start_adapter.capture_checkpoint("next-snapshot"), ("captured", "next-snapshot"))
        self.assertEqual(start_adapter.restore_checkpoint("other-checkpoint"), ("restored", "other-checkpoint"))
        self.assertEqual(trial.failure_boundary, "native_continuation_error:ValueError")

        later_adapter = FakeAdapter()
        later_checkpoint = object()
        root_decision = algorithm.Decision(
            step_index=0, state_fingerprint="state-root", legal_identities=root.legal_identities,
            chosen_index=0, is_root=True,
        )
        later_decision = algorithm.Decision(
            step_index=3, state_fingerprint="state-later",
            legal_identities=({"card": "x"}, {"card": "y"}), chosen_index=0,
            checkpoint=later_checkpoint, adapter=later_adapter, is_root=False,
            screen="synthetic-test-only", root_fingerprints={"native_state_sha256": "state-later"},
        )
        parent = algorithm.TrialResult(
            attempt_index=9, complete=True, heart_win=False, terminal_floor=1.0,
            root_identity={"card": "a"}, decisions=(root_decision, later_decision),
        )
        target = algorithm.MutationTarget(
            parent_attempt_index=9, parent_decision=later_decision, alternative_index=1,
        )
        captured, trial = run_and_capture(root_value=root, mutation_target=target, parent_trial=parent)
        start_adapter = captured["adapter_override"]
        self.assertIsInstance(start_adapter, runtime.CheckpointStartAdapter)
        self.assertIs(start_adapter._adapter, later_adapter)
        self.assertIsNone(captured["checkpoint"])
        self.assertEqual(start_adapter.reset(seed=17), ("restored", later_checkpoint))
        self.assertEqual(trial.failure_boundary, "native_continuation_error:ValueError")

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
        self.assertLess(
            min(call_lines["_reviewer_proof_from_parent_receipt"]),
            min(call_lines["initialize_native_runtime"]),
        )
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

    def test_runtime_uses_pinned_stsrl_src_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir=STUDY) as temp_dir:
            source_root = Path(temp_dir)
            package_dir = source_root / "src" / "sts_combat_rl"
            package_dir.mkdir(parents=True)
            previous_path = sys.path[:]
            try:
                resolved = runtime.configure_stsrl_import_path(source_root)
                self.assertEqual(resolved, package_dir.parent.resolve())
                self.assertEqual(sys.path[0], str(package_dir.parent.resolve()))
            finally:
                sys.path[:] = previous_path

    def test_runtime_rejects_unpinned_stsrl_package_layout(self) -> None:
        with tempfile.TemporaryDirectory(dir=STUDY) as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "pinned STSRL source lacks"):
                runtime.configure_stsrl_import_path(Path(temp_dir))

if __name__ == "__main__":
    unittest.main()
