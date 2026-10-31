from __future__ import annotations

from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

import torch
from torch import nn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gradients import (  # noqa: E402
    KL_COEFFICIENT,
    T_REF,
    OwnerOutcome,
    action_cv_correction,
    behavior_kl,
    leave_one_out_returns,
    minibatch_loss,
    owner_loss,
)


class ToyActor(nn.Module):
    def __init__(self, weight: float = 0.43, bias: float = -0.2):
        super().__init__()
        self.weight = nn.Parameter(torch.tensor(weight, dtype=torch.float64))
        self.bias = nn.Parameter(torch.tensor(bias, dtype=torch.float64))

    def distribution(self, candidate_features: torch.Tensor):
        x = candidate_features[:, 0].to(dtype=self.weight.dtype)
        logits = self.weight * x + self.bias
        log_probs = torch.log_softmax(logits, dim=0)
        probs = log_probs.exp()
        entropy = -(probs * log_probs).sum()
        return logits, log_probs, entropy


class ConstantValue(nn.Module):
    def __init__(self, value: float):
        super().__init__()
        self.value = nn.Parameter(torch.tensor(value, dtype=torch.float32))

    def forward(self, features: torch.Tensor):
        return self.value.expand(features.shape[0])


def decision(first_features: list[float], chosen: int = 0):
    matrix = torch.zeros((len(first_features), 2048), dtype=torch.float32)
    matrix[:, 0] = torch.tensor(first_features)
    return SimpleNamespace(
        candidate_features=matrix,
        legal_identities=tuple({"occurrence": i} for i in range(len(first_features))),
        chosen_index=chosen,
    )


def outcome(seed: int, reward: int, decisions: list[SimpleNamespace]) -> OwnerOutcome:
    heart = reward >= 100
    floor = reward - (100 if heart else 0)
    return OwnerOutcome(seed, heart, floor, decisions)


def grad_scalar(value: torch.Tensor, parameter: torch.Tensor) -> torch.Tensor:
    return torch.autograd.grad(value, parameter, retain_graph=True)[0]


class Issue84GradientTests(unittest.TestCase):
    def test_action_cv_expected_correction_is_zero_with_duplicate_occurrences(self):
        actor = ToyActor()
        # Occurrences 0 and 1 intentionally have identical features. They stay
        # separate categorical outcomes and receive separate Q values.
        features = decision([0.2, 0.2, 1.4]).candidate_features
        q_values = torch.tensor([2.0, -1.0, 5.0], dtype=torch.float64)
        value = 1.5
        logp = actor.distribution(features)[1]
        probabilities = logp.exp().detach()
        correction_gradients = []
        for chosen in range(3):
            correction = action_cv_correction(actor, features, q_values, chosen, value)
            correction_gradients.append(grad_scalar(correction, actor.weight))
        expected = sum(probabilities[i] * correction_gradients[i] for i in range(3))
        self.assertEqual(len(logp), 3)
        self.assertAlmostEqual(float(expected), 0.0, places=12)

    def test_action_cv_full_gradient_matches_score_function_expectation(self):
        actor = ToyActor()
        features = decision([0.2, 0.2, 1.4]).candidate_features
        q_values = torch.tensor([2.0, -1.0, 5.0], dtype=torch.float64)
        conditional_returns = [3.0, 7.0, 11.0]
        value = 1.5
        logp = actor.distribution(features)[1]
        probs = logp.exp().detach()
        all_probabilities = logp.exp()
        q_center = q_values - value
        baseline_grads = []
        corrected_grads = []
        for i, reward in enumerate(conditional_returns):
            base_objective = (reward - value) * logp[i]
            corrected_objective = (
                base_objective
                - q_center[i] * logp[i]
                + torch.sum(q_center * all_probabilities)
            )
            baseline_grads.append(grad_scalar(base_objective, actor.weight))
            corrected_grads.append(grad_scalar(corrected_objective, actor.weight))
        baseline = sum(probs[i] * baseline_grads[i] for i in range(3))
        corrected = sum(probs[i] * corrected_grads[i] for i in range(3))
        self.assertAlmostEqual(float(corrected), float(baseline), places=12)

    def test_b_changes_only_the_trajectory_length_divisor(self):
        actor = ToyActor()
        row = outcome(1, 15, [decision([0.1, 0.9], 0), decision([0.3, 1.2], 1)])
        a, a_info = owner_loss(actor, row, arm="A", block_examples=[row, outcome(2, 5, [decision([1.0, 0.0])])])
        b, b_info = owner_loss(actor, row, arm="B", block_examples=[row, outcome(2, 5, [decision([1.0, 0.0])])])
        self.assertAlmostEqual(b_info["policy"] / a_info["policy"], 2.0 / T_REF, places=12)
        # Entropy remains identical across A and B; B differs only in score divisor.
        self.assertAlmostEqual(a_info["entropy"], b_info["entropy"], places=12)
        self.assertNotEqual(float(a.detach()), float(b.detach()))

    def test_leave_one_out_uses_other_owner_returns(self):
        rows = [outcome(1, 1, []), outcome(2, 3, []), outcome(3, 8, [])]
        self.assertEqual(leave_one_out_returns(rows), [5.5, 4.5, 2.0])

    def test_value_predictor_is_detached_from_actor_gradient(self):
        actor = ToyActor()
        row = outcome(11, 17, [decision([0.1, 0.9], 1)])
        value = ConstantValue(4.0)
        loss, _ = owner_loss(actor, row, arm="D", block_examples=[row], value_model=value)
        loss.backward()
        self.assertIsNotNone(actor.weight.grad)
        self.assertIsNone(value.value.grad)

    def test_action_predictor_is_detached_from_actor_gradient(self):
        actor = ToyActor()
        row = outcome(12, 17, [decision([0.1, 0.9], 1)])
        value = ConstantValue(4.0)
        action_value = ConstantValue(3.0)
        loss, _ = owner_loss(
            actor, row, arm="E", block_examples=[row], value_model=value,
            action_value_model=action_value,
        )
        loss.backward()
        self.assertIsNotNone(actor.weight.grad)
        self.assertIsNone(value.value.grad)
        self.assertIsNone(action_value.value.grad)

    def test_behavior_kl_direction_and_detachment(self):
        current = ToyActor(weight=0.8, bias=0.0)
        behavior = ToyActor(weight=-0.3, bias=0.1)
        features = decision([0.2, 1.3, 0.4]).candidate_features
        current_logp = current.distribution(features)[1]
        with torch.no_grad():
            behavior_logp = behavior.distribution(features)[1]
            expected = (behavior_logp.exp() * (behavior_logp - current_logp)).sum()
        actual = behavior_kl(current, behavior, features)
        self.assertAlmostEqual(float(actual.detach()), float(expected.detach()), places=12)
        actual.backward()
        self.assertIsNotNone(current.weight.grad)
        self.assertIsNone(behavior.weight.grad)

    def test_f_kl_is_mean_over_reached_states_not_equal_owner_means(self):
        actor = ToyActor(weight=0.6)
        behavior = ToyActor(weight=-0.2)
        row1 = outcome(21, 10, [decision([0.2, 1.0])])
        row2 = outcome(22, 12, [decision([0.3, 1.1]), decision([0.5, 1.4]), decision([0.7, 1.8])])
        block = [row1, row2]
        individual = [owner_loss(actor, row, arm="F", block_examples=block, value_model=ConstantValue(1), behavior_actor=behavior) for row in block]
        loss1, info1 = individual[0]
        loss2, info2 = individual[1]
        expected = ((loss1 - info1["kl"]) + (loss2 - info2["kl"])) / 2
        expected = expected + KL_COEFFICIENT * (info1["kl"] + 3 * info2["kl"]) / 4
        actual = minibatch_loss(actor, block, arm="F", block_examples=block, value_model=ConstantValue(1), behavior_actor=behavior)
        self.assertAlmostEqual(float(actual.detach()), float(expected.detach()), places=12)


if __name__ == "__main__":
    unittest.main()
