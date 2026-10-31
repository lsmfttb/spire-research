"""Source-locked actor objectives for the Issue 84 six-arm study.

This module contains no simulator logic. Its inputs are the genuine ordered
legal occurrences and source-qualified candidate features captured at reached
states by the native runner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import torch
from torch import nn


T_REF = 36.6474609375
S_REF = 5.360108706240238
ENTROPY_COEFFICIENT = 0.01
KL_COEFFICIENT = 1.0
FEATURE_DIM = 2048
PREDICTOR_HIDDEN_DIM = 64


class ScalarPredictor(nn.Module):
    """Small deterministic current-time V(h) or Q(h,a) predictor."""

    def __init__(
        self,
        input_dim: int = FEATURE_DIM,
        hidden_dim: int = PREDICTOR_HIDDEN_DIM,
        *,
        seed: int = 0,
    ):
        super().__init__()
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(int(seed))
            self.network = nn.Sequential(
                nn.Linear(input_dim, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, 1),
            )
        # V starts at the contract's exact zero cold start. The hidden layer is
        # still deterministically initialized so the predictor can fit later.
        nn.init.zeros_(self.network[-1].weight)
        nn.init.zeros_(self.network[-1].bias)

    def forward(self, features: torch.Tensor) -> torch.Tensor:
        if features.ndim != 2 or features.shape[1] != FEATURE_DIM:
            raise ValueError("predictor input must be a matrix of 2048-dimensional candidate features")
        return self.network(features).squeeze(-1)


@dataclass(frozen=True)
class OwnerOutcome:
    owner_seed: int
    heart_win: bool
    terminal_floor: float
    decisions: Sequence[Any]
    complete: bool = True

    @property
    def reward(self) -> float:
        return 100.0 * float(self.heart_win) + float(self.terminal_floor)


def _feature_matrix(decision: Any) -> torch.Tensor:
    features = torch.as_tensor(decision.candidate_features, dtype=torch.float32)
    if features.ndim != 2 or features.shape[1] != FEATURE_DIM:
        raise ValueError("decision candidate features must have shape [legal occurrences, 2048]")
    if features.shape[0] != len(decision.legal_identities):
        raise ValueError("candidate feature rows must preserve every ordered legal occurrence")
    if not 1 <= len(decision.legal_identities):
        raise ValueError("a reached decision must retain at least one legal occurrence")
    if not 0 <= int(decision.chosen_index) < features.shape[0]:
        raise ValueError("chosen action index is outside the ordered legal occurrences")
    return features


def _distribution(actor: nn.Module, features: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    _logits, log_probs, entropy = actor.distribution(features)
    if log_probs.shape != (features.shape[0],) or entropy.ndim != 0:
        raise ValueError("actor distribution must return one normalized log-probability per legal occurrence")
    if not torch.isfinite(log_probs).all() or not torch.isfinite(entropy):
        raise ValueError("actor distribution contains non-finite values")
    return log_probs, entropy


def state_features(decision: Any) -> torch.Tensor:
    """Permutation-neutral current-state representation from the live legal set."""
    return _feature_matrix(decision).mean(dim=0, keepdim=True)


def leave_one_out_returns(examples: Sequence[OwnerOutcome]) -> list[float]:
    """Return each owner's mean of the other owners' terminal rewards."""
    if len(examples) < 2:
        raise ValueError("leave-one-owner-out baseline requires at least two complete owners")
    rewards = [float(row.reward) for row in examples]
    total = sum(rewards)
    denominator = len(rewards) - 1
    return [(total - reward) / denominator for reward in rewards]


def _check_block(examples: Sequence[OwnerOutcome]) -> None:
    if not examples or any(not row.complete for row in examples):
        raise ValueError("actor updates require nonempty complete natural-game owner outcomes")
    if len({int(row.owner_seed) for row in examples}) != len(examples):
        raise ValueError("owner seeds must be unique within an arm block")


def owner_loss(
    actor: nn.Module,
    example: OwnerOutcome,
    *,
    arm: str,
    block_examples: Sequence[OwnerOutcome],
    value_model: nn.Module | None = None,
    action_value_model: nn.Module | None = None,
    behavior_actor: nn.Module | None = None,
) -> tuple[torch.Tensor, dict[str, float]]:
    """Return one owner's descent loss; caller averages owner losses in minibatch.

    A/B retain the legacy clipped block-standardized terminal advantage. C uses
    a block leave-one-owner-out terminal baseline. D/E/F use a one-block-lagged
    state value; E adds the exact ordered-all-occurrence Q correction, and F
    adds KL(pi_behavior || pi_current).
    """
    arm = arm.upper()
    if arm not in {"A", "B", "C", "D", "E", "F"}:
        raise ValueError(f"unknown Issue 84 arm: {arm}")
    _check_block(block_examples)
    matched_indices = [i for i, row in enumerate(block_examples) if int(row.owner_seed) == int(example.owner_seed)]
    if not example.complete or len(matched_indices) != 1:
        raise ValueError("owner must be a complete member of its source-locked training block")
    if float(block_examples[matched_indices[0]].reward) != float(example.reward):
        raise ValueError("owner terminal outcome differs from its registered block record")
    if not example.decisions:
        zero = next(actor.parameters()).sum() * 0.0
        return zero, {"policy": 0.0, "entropy": 0.0, "kl": 0.0, "decision_count": 0.0}

    if arm in {"A", "B"}:
        rewards = torch.tensor([float(row.reward) for row in block_examples], dtype=torch.float64)
        mean = float(rewards.mean().item())
        scale = max(float(rewards.std(unbiased=False).item()), 1.0)
        advantage = max(-3.0, min(3.0, (float(example.reward) - mean) / scale))
        decision_logps: list[torch.Tensor] = []
        entropies: list[torch.Tensor] = []
        for decision in example.decisions:
            features = _feature_matrix(decision)
            log_probs, entropy = _distribution(actor, features)
            decision_logps.append(log_probs[int(decision.chosen_index)])
            entropies.append(entropy)
        score_sum = torch.stack(decision_logps).sum()
        denominator = float(len(decision_logps)) if arm == "A" else T_REF
        policy_loss = -advantage * score_sum / denominator
    elif arm == "C":
        baselines = leave_one_out_returns(block_examples)
        index = matched_indices[0]
        decision_logps = []
        entropies = []
        for decision in example.decisions:
            features = _feature_matrix(decision)
            log_probs, entropy = _distribution(actor, features)
            decision_logps.append(log_probs[int(decision.chosen_index)])
            entropies.append(entropy)
        policy_loss = -((float(example.reward) - baselines[index]) / S_REF) * torch.stack(decision_logps).sum() / T_REF
    else:
        if value_model is None:
            raise ValueError(f"arm {arm} requires its frozen prior-block V model")
        if arm == "E" and action_value_model is None:
            raise ValueError("arm E requires its frozen prior-block Q model")
        if arm == "F" and behavior_actor is None:
            raise ValueError("arm F requires the frozen policy that generated this block")
        decision_losses: list[torch.Tensor] = []
        entropies = []
        kl_terms: list[torch.Tensor] = []
        for decision in example.decisions:
            features = _feature_matrix(decision)
            log_probs, entropy = _distribution(actor, features)
            chosen = int(decision.chosen_index)
            with torch.no_grad():
                value = value_model(state_features(decision)).reshape(()).detach()
            residual = (float(example.reward) - value) * log_probs[chosen]
            if arm == "E":
                with torch.no_grad():
                    q_values = action_value_model(features).reshape(-1).detach()
                if q_values.shape != (features.shape[0],) or not torch.isfinite(q_values).all():
                    raise ValueError("Q must return one finite value per genuine ordered legal occurrence")
                q_centered = q_values - value
                # The selected-action subtraction and all-action probability
                # correction are both required; do not coalesce duplicate rows.
                correction = q_centered[chosen] * log_probs[chosen] - torch.sum(q_centered * log_probs.exp())
                residual = residual - correction
            decision_losses.append(-residual / (T_REF * S_REF))
            entropies.append(entropy)
            if arm == "F":
                with torch.no_grad():
                    behavior_log_probs, _ = _distribution(behavior_actor, features)
                    behavior_probs = behavior_log_probs.exp()
                kl_terms.append(torch.sum(behavior_probs * (behavior_log_probs - log_probs)))
        policy_loss = torch.stack(decision_losses).sum()
        if arm == "F":
            kl = torch.stack(kl_terms).mean()
            policy_loss = policy_loss + kl
        else:
            kl = policy_loss.new_zeros(())

    entropy_loss = -ENTROPY_COEFFICIENT * torch.stack(entropies).mean()
    total = policy_loss + entropy_loss
    return total, {
        "policy": float(policy_loss.detach().item()),
        "entropy": float(entropy_loss.detach().item()),
        "kl": float(kl.detach().item()) if arm == "F" else 0.0,
        "decision_count": float(len(example.decisions)),
    }


def minibatch_loss(actor: nn.Module, examples: Sequence[OwnerOutcome], **kwargs: Any) -> torch.Tensor:
    """Match #75's equal-owner averaging while preserving all within-owner decisions."""
    if not examples:
        raise ValueError("minibatch cannot be empty")
    losses: list[torch.Tensor] = []
    diagnostics: list[dict[str, float]] = []
    for row in examples:
        loss, details = owner_loss(actor, row, block_examples=kwargs["block_examples"], **{
            key: value for key, value in kwargs.items() if key != "block_examples"
        })
        losses.append(loss)
        diagnostics.append(details)
    if kwargs.get("arm", "").upper() == "F":
        # Keep the #75 equal-owner policy/entropy average, while honoring the
        # F contract's separate mean over every reached-state KL in minibatch.
        decisions = sum(item["decision_count"] for item in diagnostics)
        if decisions <= 0:
            return torch.stack(losses).mean()
        owner_kl_mean = torch.tensor(
            [item["kl"] for item in diagnostics], dtype=losses[0].dtype, device=losses[0].device,
        )
        owner_decisions = torch.tensor(
            [item["decision_count"] for item in diagnostics], dtype=losses[0].dtype, device=losses[0].device,
        )
        owner_balanced_without_kl = torch.stack(losses) - owner_kl_mean
        global_state_mean_kl = torch.sum(owner_kl_mean * owner_decisions) / decisions
        return owner_balanced_without_kl.mean() + KL_COEFFICIENT * global_state_mean_kl
    return torch.stack(losses).mean()


def action_cv_correction(
    actor: nn.Module,
    features: torch.Tensor,
    q_values: torch.Tensor,
    chosen_index: int,
    value: torch.Tensor | float,
) -> torch.Tensor:
    """Loss-sign form of the E-arm Q correction over every legal occurrence."""
    log_probs, _ = _distribution(actor, features)
    q_values = torch.as_tensor(q_values, dtype=log_probs.dtype, device=log_probs.device).detach()
    if q_values.shape != log_probs.shape:
        raise ValueError("Q values must match the ordered legal occurrence vector")
    center = torch.as_tensor(value, dtype=log_probs.dtype, device=log_probs.device).detach()
    centered = q_values - center
    if not 0 <= int(chosen_index) < len(centered):
        raise ValueError("chosen occurrence is outside legal list")
    # Negative actor objective: subtraction in the gradient, then add back the
    # exact expected correction; gradient descent is the negative of the issue's
    # stated actor-gradient ascent convention.
    return centered[int(chosen_index)] * log_probs[int(chosen_index)] - torch.sum(centered * log_probs.exp())


def behavior_kl(actor: nn.Module, behavior_actor: nn.Module, features: torch.Tensor) -> torch.Tensor:
    """One reached-state KL in the registered pi_behavior || pi_current direction."""
    current_log_probs, _ = _distribution(actor, features)
    with torch.no_grad():
        behavior_log_probs, _ = _distribution(behavior_actor, features)
        behavior_probs = behavior_log_probs.exp()
    return torch.sum(behavior_probs * (behavior_log_probs - current_log_probs))
