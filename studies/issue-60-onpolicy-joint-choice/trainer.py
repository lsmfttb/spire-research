"""Fixed PPO return calculation and update for the Issue 60 study."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import torch
from torch import Tensor
from torch.nn import functional as F

from encoder import Observation, Vocabulary, encode
from model import JointChoiceActorCritic
from controllers import policy_distribution


class IncompleteRun(RuntimeError):
    """A capped, failed, or nonterminal game has no training target."""


def derive_seed(domain: str, *parts: object) -> int:
    payload = json.dumps([domain, *parts], sort_keys=True, separators=(",", ":"))
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big")


def terminal_target(*, terminal: bool, problems: Sequence[str], outcome: str,
                    final_floor: float | None, current_floor: float) -> float:
    """Return normalized terminal floor progress only for a real terminal run."""
    if not terminal or problems:
        raise IncompleteRun("caps and executor errors do not produce a target")
    if outcome not in {"PLAYER_LOSS", "PLAYER_VICTORY"}:
        raise IncompleteRun(f"unexpected terminal game outcome: {outcome}")
    if final_floor is None or not math.isfinite(float(final_floor)):
        raise IncompleteRun("terminal public floor is unavailable")
    if not math.isfinite(float(current_floor)):
        raise IncompleteRun("decision floor is not finite")
    return (float(final_floor) - float(current_floor)) / 60.0


@dataclass(frozen=True)
class DecisionRow:
    serialized_public: str
    p0: tuple[float, ...]
    action: int
    old_log_probability: float
    old_value: float
    value_target: float
    current_floor: float
    actor_update_mask: float
    game_seed: int

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "DecisionRow":
        if data.get("issue60_supported_choice") is not True:
            raise ValueError("training row is not a supported Issue 60 choice")
        value_target = terminal_target(
            terminal=data["terminal"], problems=data["run_problems"],
            outcome=data["run_outcome"], final_floor=data["terminal_floor"],
            current_floor=data["issue60_current_floor"],
        )
        p0 = tuple(float(x) for x in data["issue60_base_probabilities"])
        behavior = tuple(float(x) for x in data["issue60_behavior_probabilities"])
        action = int(data["issue60_selected_index"])
        if len(p0) != len(behavior) or not 0 <= action < len(p0):
            raise ValueError("stored behavior distribution does not match its candidate")
        if not math.isclose(
            math.log(behavior[action]),
            float(data["issue60_old_log_probability"]),
            rel_tol=1e-10, abs_tol=1e-10,
        ):
            raise ValueError("stored selected-action log probability is inconsistent")
        return cls(
            serialized_public=data["issue60_public_input"], p0=p0,
            action=action,
            old_log_probability=float(data["issue60_old_log_probability"]),
            old_value=float(data["issue60_value_prediction"]),
            value_target=value_target,
            current_floor=float(data["issue60_current_floor"]),
            actor_update_mask=float(data["issue60_actor_update_mask"]),
            game_seed=int(data["game_seed"]),
        )

    def to_json(self) -> dict[str, Any]:
        advantage = self.value_target - self.old_value
        return {
            "schema_id": "issue60-training-row-v1",
            "serialized_public": self.serialized_public,
            "p0": list(self.p0), "action": self.action,
            "old_log_probability": self.old_log_probability,
            "old_value": self.old_value, "value_target": self.value_target,
            "advantage": advantage, "current_floor": self.current_floor,
            "actor_update_mask": self.actor_update_mask,
            "game_seed": self.game_seed,
        }


def centered_advantages(rows: Sequence[DecisionRow]) -> list[float]:
    if not rows:
        raise ValueError("cannot normalize an empty decision batch")
    raw = torch.tensor(
        [row.value_target - row.old_value for row in rows],
        dtype=torch.float64,
    )
    mean = float(raw.mean())
    std = float(raw.std(unbiased=False))
    denominator = max(std, 1e-8)
    return [float(x) for x in ((raw - mean) / denominator).tolist()]


def ppo_minibatch_loss(model: JointChoiceActorCritic,
                       rows: Sequence[DecisionRow], advantages: Sequence[float],
                       vocabulary: Vocabulary, *, denominator_games: int = 128,
                       clip_ratio: float = 0.2, value_coefficient: float = 0.5,
                       entropy_coefficient: float = 0.0
                       ) -> tuple[Tensor, dict[str, float]]:
    """Sum per-decision losses then divide by the fixed original game count."""
    if len(rows) != len(advantages) or not rows:
        raise ValueError("PPO minibatch rows and advantages must be nonempty and aligned")
    if denominator_games != 128:
        raise ValueError("Issue 60 fixes the per-game loss denominator at 128")
    losses: list[Tensor] = []
    policy_losses: list[Tensor] = []
    value_losses: list[Tensor] = []
    entropies: list[Tensor] = []
    for row, advantage in zip(rows, advantages, strict=True):
        observation: Observation = encode(row.serialized_public, vocabulary)
        logits, value = model(observation)
        if len(logits) != len(row.p0):
            raise ValueError("encoded candidate count changed since rollout collection")
        p0 = torch.tensor(row.p0, dtype=logits.dtype, device=logits.device)
        log_weights = torch.log(p0) + logits
        log_probs = F.log_softmax(log_weights, dim=0)
        probs = log_probs.exp()
        new_log_probability = log_probs[row.action]
        ratio = torch.exp(new_log_probability - row.old_log_probability)
        adv = logits.new_tensor(float(advantage))
        clipped = torch.clamp(ratio, 1.0 - clip_ratio, 1.0 + clip_ratio)
        policy = -torch.minimum(ratio * adv, clipped * adv)
        policy = policy * row.actor_update_mask
        target = logits.new_tensor(row.value_target)
        value_loss = (value - target).square()
        entropy = -(probs * log_probs).sum()
        losses.append(policy + value_coefficient * value_loss - entropy_coefficient * entropy)
        policy_losses.append(policy)
        value_losses.append(value_loss)
        entropies.append(entropy)
    loss = torch.stack(losses).sum() / denominator_games
    return loss, {
        "loss": float(loss.detach()),
        "policy_loss_sum": float(torch.stack(policy_losses).sum().detach()),
        "value_mse_sum": float(torch.stack(value_losses).sum().detach()),
        "entropy_sum": float(torch.stack(entropies).sum().detach()),
        "decision_rows": float(len(rows)),
        "actor_rows": float(sum(row.actor_update_mask > 0 for row in rows)),
    }


def update_batch(model: JointChoiceActorCritic, optimizer: torch.optim.Optimizer,
                 rows: Sequence[DecisionRow], vocabulary: Vocabulary, *,
                 block_seed: int, batch_index: int, epochs: int = 4,
                 minibatch_size: int = 256, gradient_norm: float = 0.5,
                 clip_ratio: float = 0.2, value_coefficient: float = 0.5,
                 entropy_coefficient: float = 0.0,
                 ) -> list[dict[str, float]]:
    if not rows:
        raise ValueError("a fixed training batch produced no supported decisions")
    advantages = centered_advantages(rows)
    metrics: list[dict[str, float]] = []
    for epoch in range(epochs):
        generator = torch.Generator(device="cpu")
        generator.manual_seed(derive_seed("optimizer-shuffle", block_seed, batch_index, epoch))
        permutation = torch.randperm(len(rows), generator=generator).tolist()
        for offset in range(0, len(rows), minibatch_size):
            indices = permutation[offset:offset + minibatch_size]
            minibatch = [rows[index] for index in indices]
            normalized = [advantages[index] for index in indices]
            optimizer.zero_grad(set_to_none=True)
            loss, values = ppo_minibatch_loss(
                model, minibatch, normalized, vocabulary,
                clip_ratio=clip_ratio, value_coefficient=value_coefficient,
                entropy_coefficient=entropy_coefficient,
            )
            loss.backward()
            norm = torch.nn.utils.clip_grad_norm_(model.parameters(), gradient_norm)
            optimizer.step()
            values["gradient_norm_before_clip"] = float(norm)
            values["epoch"] = float(epoch + 1)
            values["minibatch_index"] = float(offset // minibatch_size)
            metrics.append(values)
    return metrics


def policy_probs_for_witness(p0: Sequence[float], residual: Sequence[float]) -> list[float]:
    """Named alias keeps the preflight comparison independent of model code."""
    return policy_distribution(p0, residual)
