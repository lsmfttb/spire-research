from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import numpy as np
import torch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))

from runtime_integration import (
    Issue85Decision,
    _pack_matrix,
    _pack_payload,
    _unpack_matrix,
    _unpack_payload,
    install_issue85_runtime,
)


class FakeEncoder:
    def __init__(self, dimension: int = 2048):
        self.dimension = dimension

    def encode(self, _projection, action_count):
        matrix = np.zeros((action_count, self.dimension), dtype=np.float32)
        for row in range(action_count):
            matrix[row, 0] = float(row + 1)
        if self.dimension == 8192:
            matrix[:, 2048:] = 0.0
        return matrix, {"opaque_bindings_used_for_model_input": False}


class BrokenWideEncoder(FakeEncoder):
    def encode(self, projection, action_count):
        matrix, audit = super().encode(projection, action_count)
        if self.dimension == 8192:
            matrix[0, 0] += 1.0
        return matrix, audit


class FakeIssue85Policy(torch.nn.Module):
    def __init__(self, arm="C"):
        super().__init__()
        self.arm = arm
        self.widened = arm in {"B", "D"}
        self.weight = torch.nn.Parameter(torch.tensor(0.25))

    def distribution(self, features, _payload):
        logits = features[:, 0] * self.weight
        logp = torch.log_softmax(logits, dim=0)
        probs = logp.exp()
        entropy = -(probs * logp).sum()
        return logits, logp, entropy


class LegacyPolicy:
    def choose(self, matrix, _generator):
        return 0, np.full(len(matrix), 1.0 / len(matrix)), 0.0


@dataclass(frozen=True)
class BaseDecision:
    candidate_features: np.ndarray
    chosen_index: int


@dataclass(frozen=True)
class Trial:
    complete: bool
    decisions: tuple


@dataclass(frozen=True)
class Owner:
    owner_seed: int
    reward: float
    complete: bool
    decisions: tuple


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.policy_type = FakeIssue85Policy

        class PolicyAdapter:
            def __init__(adapter, policy):
                adapter.policy = policy

            def choose(adapter, matrix, generator):
                return adapter.policy.choose(matrix, generator)

        class Controller:
            def __init__(controller, model):
                controller.model = model
                controller.encoder = FakeEncoder()
                controller.trace = []
                controller.rng = torch.Generator().manual_seed(7)

            def select_action(controller, adapter, snapshot, actions, context, step_index):
                matrix, _audit = controller.encoder.encode(context, len(actions))
                selected, probabilities, value = controller.model.choose(matrix, controller.rng)
                controller.trace.append({
                    "kind": "categorical",
                    "step": step_index,
                    "chosen_index": selected,
                    "probabilities": probabilities.tolist(),
                    "matrix": matrix,
                })
                return selected, probabilities, value

        class Runner:
            StudyController = Controller

        class Executor:
            @staticmethod
            def decision_from_trace(row, _index, *, is_root):
                return BaseDecision(
                    candidate_features=np.asarray(row["candidate_features"], dtype=np.float32).copy(),
                    chosen_index=int(row["chosen_index"]),
                )

            @staticmethod
            def decode_trial(payload):
                rows = payload["decisions"]
                decisions = tuple(
                    BaseDecision(
                        candidate_features=np.asarray(row["candidate_features"], dtype=np.float32),
                        chosen_index=int(row["chosen_index"]),
                    )
                    for row in rows
                )
                return Trial(complete=payload["complete"], decisions=decisions)

        class Gradients:
            T_REF = 36.6474609375
            ENTROPY_COEFFICIENT = 0.01

            @staticmethod
            def _check_block(_block):
                return None

            @staticmethod
            def _feature_matrix(decision):
                return torch.as_tensor(decision.candidate_features, dtype=torch.float32)

            @staticmethod
            def owner_loss(*_args, **_kwargs):
                raise AssertionError("Issue 85 actor should use the registered B objective adapter")

        self.runtime = SimpleNamespace(
            PolicyAdapter=PolicyAdapter,
            load_issue73_runner=lambda _config: Runner(),
            _hash_only_decisions=lambda decisions: [
                {
                    "candidate_features": np.asarray(row.candidate_features, dtype=np.float32).tolist(),
                    "chosen_index": int(row.chosen_index),
                }
                for row in decisions
            ],
        )
        self.executor = SimpleNamespace(
            _decision_from_trace=Executor.decision_from_trace,
            _decode_trial=Executor.decode_trial,
        )
        self.gradients = Gradients()
        self.report = install_issue85_runtime(
            self.runtime,
            self.executor,
            self.gradients,
            arm_type=self.policy_type,
            payload_builder=lambda _privileged, _public: {
                "candidates": [{"kind": "choice", "features": {"row": index}} for index in range(3)]
            },
        )

    def test_native_choice_context_is_carried_through_training_record_round_trip(self):
        actor = FakeIssue85Policy("C")
        adapter = self.runtime.PolicyAdapter(actor)
        runner = self.runtime.load_issue73_runner({})
        controller = runner.StudyController(adapter)
        public_adapter = SimpleNamespace(public_projection=lambda _snapshot: {"schema_id": "native-public-projection-v3"})
        privileged = {"candidates": [{}, {}, {}]}
        controller.select_action(
            public_adapter, object(), ["a", "b", "c"], privileged, 12,
        )
        self.assertIsInstance(controller.trace, list)
        trace_row = controller.trace[0]
        self.assertIn("issue85_payload", trace_row)
        self.assertEqual(len(trace_row["issue85_payload"]["candidates"]), 3)

        training_row = {key: value for key, value in trace_row.items() if key != "matrix"}
        training_row["candidate_features"] = trace_row["matrix"]
        decision = self.executor._decision_from_trace(training_row, 0, is_root=True)
        self.assertIsInstance(decision, Issue85Decision)
        self.assertEqual(decision.issue85_payload, trace_row["issue85_payload"])

        packed = self.runtime._hash_only_decisions([decision])
        restored = self.executor._decode_trial({"complete": True, "decisions": packed})
        self.assertIsInstance(restored, Trial)
        self.assertIsInstance(restored.decisions[0], Issue85Decision)
        self.assertEqual(restored.decisions[0].issue85_payload, decision.issue85_payload)
        np.testing.assert_array_equal(restored.decisions[0].candidate_features, decision.candidate_features)

    def test_widened_hash_requires_real_fold_identity_and_keeps_native_sampling(self):
        encoder = FakeEncoder(2048)
        wide = FakeEncoder(8192)
        legacy, _ = encoder.encode({}, 3)
        widened, _ = wide.encode({}, 3)
        widened[:, :2048] = legacy
        # The exact sampling wrapper consumes the native generator, without a
        # second sampling draw or candidate reordering.
        actor = FakeIssue85Policy("B")
        adapter = self.runtime.PolicyAdapter(actor)
        runner = self.runtime.load_issue73_runner({})
        controller = runner.StudyController(adapter)
        controller.encoder = encoder
        original_encoder_type = type(controller.encoder)
        selected = controller.select_action(
            SimpleNamespace(public_projection=lambda _snapshot: {"schema_id": "native-public-projection-v3"}),
            object(), ["a", "b", "c"], {}, 0,
        )
        expected_features, _audit = FakeEncoder(8192).encode({}, 3)
        expected_actor_features = torch.as_tensor(expected_features, dtype=torch.float32)
        _logits, expected_logp, _entropy = actor.distribution(expected_actor_features, {})
        expected_rng = torch.Generator().manual_seed(7)
        expected_index = int(torch.multinomial(expected_logp.exp(), 1, generator=expected_rng).item())
        self.assertEqual(selected[0], expected_index)
        np.testing.assert_allclose(selected[1], expected_logp.exp().detach().numpy(), rtol=0, atol=0)
        self.assertIs(type(controller.encoder), original_encoder_type)

    def test_widened_hash_rejects_a_matrix_without_the_locked_fold(self):
        actor = FakeIssue85Policy("B")
        runner = self.runtime.load_issue73_runner({})
        controller = runner.StudyController(self.runtime.PolicyAdapter(actor))
        controller.encoder = BrokenWideEncoder()
        adapter = SimpleNamespace(public_projection=lambda _snapshot: {"schema_id": "native-public-projection-v3"})
        with self.assertRaisesRegex(ValueError, "does not fold"):
            controller.select_action(adapter, object(), ["a", "b", "c"], {}, 0)

    def test_context_trace_only_enriches_categorical_decisions(self):
        actor = FakeIssue85Policy("C")
        controller = self.runtime.load_issue73_runner({}).StudyController(self.runtime.PolicyAdapter(actor))
        adapter = SimpleNamespace(public_projection=lambda _snapshot: {"schema_id": "native-public-projection-v3"})
        controller.select_action(adapter, object(), ["a", "b", "c"], {}, 3)
        controller.trace.append({"kind": "single_action", "step": 4})
        self.assertNotIn("issue85_payload", controller.trace[-1])

    def test_payload_and_matrix_storage_detect_corruption(self):
        matrix = np.zeros((2, 2048), dtype=np.float32)
        matrix[:, :2] = [[1.0, 2.0], [3.0, 4.0]]
        self.assertTrue(np.array_equal(_unpack_matrix(_pack_matrix(matrix)), matrix))
        payload = {"candidates": [{"kind": "x"}, {"kind": "y"}]}
        self.assertEqual(_unpack_payload(_pack_payload(payload)), payload)
        packed = _pack_payload(payload)
        packed["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "fails its digest"):
            _unpack_payload(packed)
        with self.assertRaisesRegex(ValueError, "unsupported feature width"):
            _pack_matrix(np.ones((2, 2), dtype=np.float32))

    def test_owner_loss_uses_exact_registered_length_only_formula(self):
        actor = FakeIssue85Policy("B")
        legacy = np.zeros((3, 2048), dtype=np.float32)
        legacy[:, 0] = [1.0, 2.0, 3.0]
        wide = np.zeros((3, 8192), dtype=np.float32)
        wide[:, 0] = legacy[:, 0]
        decision = Issue85Decision(
            BaseDecision(legacy, 2),
            payload=None,
            wide_candidate_features=wide,
            legacy_sha256="fixture-legacy-digest",
        )
        owner = Owner(1, 2.0, True, (decision,))
        block = [Owner(1, 2.0, True, (decision,)), Owner(2, 0.0, True, (decision,))]
        loss, diagnostics = self.gradients.owner_loss(
            actor, owner, arm="B", block_examples=block,
        )
        _logits, logp, entropy = actor.distribution(torch.as_tensor(wide), {})
        expected = -logp[2] / self.gradients.T_REF - self.gradients.ENTROPY_COEFFICIENT * entropy
        torch.testing.assert_close(loss, expected)
        self.assertEqual(diagnostics["decision_count"], 1.0)


if __name__ == "__main__":
    unittest.main()
