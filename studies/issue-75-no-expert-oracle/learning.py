from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import math
import random
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import torch
from torch import nn

from algorithm import Decision, TrialResult, elite_candidate
from model import ActionConditionedPolicy, model_digest


LEARNING_RATE = 3e-4
WEIGHT_DECAY = 0.0
BETAS = (0.9, 0.999)
EPSILON = 1e-8
ENTROPY_COEFFICIENT = 0.01
MAX_GRAD_NORM = 1.0
FORMAL_BLOCK_OWNERS = 128
FORMAL_MINIBATCH_OWNERS = 32
FORMAL_STEPS_PER_BLOCK = 64


class IncompleteOwnerBlock(RuntimeError):
    pass


@dataclass(frozen=True)
class OwnerTrainingExample:
    owner_seed: int
    base: TrialResult
    joint_trajectories: tuple[TrialResult, ...] = ()


@dataclass(frozen=True)
class UpdateDiagnostic:
    step: int
    loss_pg: float
    loss_elite: float
    entropy: float
    total_loss: float
    grad_norm_pg: float
    grad_norm_elite: float
    grad_norm_entropy: float
    grad_norm_before_clip: float
    grad_norm_after_clip: float
    elite_owner_count: int
    elite_root_count: int


@dataclass(frozen=True)
class BlockTrainingResult:
    model: ActionConditionedPolicy
    source_model_digest: str
    result_model_digest: str
    block_order_seed: int
    owner_order_sha256: str
    diagnostics: tuple[UpdateDiagnostic, ...]
    status: str


def _utility(example: OwnerTrainingExample) -> float:
    return 100.0 * float(example.base.heart_win) + float(example.base.terminal_floor)


def _validate_examples(examples: Sequence[OwnerTrainingExample], expected_count: int) -> None:
    if len(examples) != expected_count:
        raise IncompleteOwnerBlock(f"expected exactly {expected_count} predeclared owners, got {len(examples)}")
    if len({int(row.owner_seed) for row in examples}) != len(examples):
        raise ValueError("owner seeds must be unique within a training block")
    if any(not row.base.complete for row in examples):
        raise IncompleteOwnerBlock("incomplete base games have no terminal utility and cannot enter REINFORCE")


def _decision_terms(
    model: ActionConditionedPolicy,
    decisions: Sequence[Decision],
) -> tuple[torch.Tensor, torch.Tensor]:
    if not decisions:
        zero = next(model.parameters()).sum() * 0.0
        return zero, zero
    log_probabilities: list[torch.Tensor] = []
    entropies: list[torch.Tensor] = []
    for decision in decisions:
        if decision.candidate_features is None:
            raise ValueError("training decision lacks its true reached semantic feature matrix")
        if not 0 <= decision.chosen_index < len(decision.legal_identities):
            raise ValueError("training action is not a current live legal occurrence")
        features = torch.as_tensor(decision.candidate_features, dtype=torch.float32)
        logits, log_probs, entropy = model.distribution(features)
        if logits.shape[0] != len(decision.legal_identities):
            raise ValueError("training feature rows differ from actual legal occurrence count")
        log_probabilities.append(log_probs[decision.chosen_index])
        entropies.append(entropy)
    return torch.stack(log_probabilities).mean(), torch.stack(entropies).mean()


def _owner_losses(
    model: ActionConditionedPolicy,
    batch: Sequence[OwnerTrainingExample],
    block_mean: float,
    block_sd: float,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, int, int]:
    pg_terms: list[torch.Tensor] = []
    entropies: list[torch.Tensor] = []
    elite_terms: list[torch.Tensor] = []
    elite_owner_count = 0
    elite_root_count = 0
    zero = next(model.parameters()).sum() * 0.0
    scale = max(float(block_sd), 1.0)
    for example in batch:
        log_prob_mean, entropy_mean = _decision_terms(model, example.base.decisions)
        advantage = max(-3.0, min(3.0, (_utility(example) - block_mean) / scale))
        pg_terms.append(-float(advantage) * log_prob_mean)
        entropies.append(entropy_mean)
        by_root: dict[str, list[TrialResult]] = {}
        for trial in example.joint_trajectories:
            if not trial.complete or not trial.decisions:
                continue
            root_id = trial.search_root_id or trial.decisions[0].state_fingerprint
            by_root.setdefault(str(root_id), []).append(trial)
        root_terms: list[torch.Tensor] = []
        for root_id in sorted(by_root):
            target = elite_candidate(example.base, by_root[root_id])
            if target is not None:
                target_log_prob_mean, _ = _decision_terms(model, target.decisions)
                root_terms.append(-target_log_prob_mean)
                elite_root_count += 1
        if root_terms:
            elite_terms.append(torch.stack(root_terms).mean())
            elite_owner_count += 1
        else:
            elite_terms.append(zero)
    loss_pg = torch.stack(pg_terms).mean() if pg_terms else zero
    entropy = torch.stack(entropies).mean() if entropies else zero
    loss_elite = torch.stack(elite_terms).mean() if elite_terms else zero
    return loss_pg, loss_elite, entropy, elite_owner_count, elite_root_count


def _grad_norm(loss: torch.Tensor, parameters: Sequence[torch.Tensor], *, retain_graph: bool) -> float:
    grads = torch.autograd.grad(
        loss, parameters, retain_graph=retain_graph, allow_unused=True,
    )
    squared = torch.zeros((), dtype=torch.float64)
    for grad in grads:
        if grad is not None:
            squared += grad.detach().double().pow(2).sum()
    return float(torch.sqrt(squared).item())


def _update(
    model: ActionConditionedPolicy,
    batch: Sequence[OwnerTrainingExample],
    *,
    block_mean: float,
    block_sd: float,
    step: int,
    optimizer: torch.optim.Optimizer,
) -> UpdateDiagnostic:
    parameters = tuple(model.parameters())
    loss_pg, loss_elite, entropy, elite_count, elite_root_count = _owner_losses(model, batch, block_mean, block_sd)
    grad_pg = _grad_norm(loss_pg, parameters, retain_graph=True)
    grad_elite = _grad_norm(loss_elite, parameters, retain_graph=True)
    grad_entropy = _grad_norm(entropy, parameters, retain_graph=True)
    total = loss_pg + loss_elite - ENTROPY_COEFFICIENT * entropy
    optimizer.zero_grad(set_to_none=True)
    total.backward()
    before = float(torch.nn.utils.clip_grad_norm_(parameters, MAX_GRAD_NORM).item())
    after = math.sqrt(sum(
        float(parameter.grad.detach().double().pow(2).sum().item())
        for parameter in parameters if parameter.grad is not None
    ))
    optimizer.step()
    return UpdateDiagnostic(
        step=int(step),
        loss_pg=float(loss_pg.detach().item()),
        loss_elite=float(loss_elite.detach().item()),
        entropy=float(entropy.detach().item()),
        total_loss=float(total.detach().item()),
        grad_norm_pg=grad_pg,
        grad_norm_elite=grad_elite,
        grad_norm_entropy=grad_entropy,
        grad_norm_before_clip=before,
        grad_norm_after_clip=after,
        elite_owner_count=elite_count,
        elite_root_count=elite_root_count,
    )


def _new_optimizer(model: ActionConditionedPolicy) -> torch.optim.AdamW:
    return torch.optim.AdamW(
        model.parameters(),
        lr=LEARNING_RATE,
        weight_decay=WEIGHT_DECAY,
        betas=BETAS,
        eps=EPSILON,
    )


def train_stage0_diagnostic(
    frozen_pi0: ActionConditionedPolicy,
    examples: Sequence[OwnerTrainingExample],
) -> BlockTrainingResult:
    """Exactly one conditional-on-fixed-root minibatch; never a standard-start owner estimate."""
    _validate_examples(examples, expected_count=8)
    model = deepcopy(frozen_pi0)
    source_digest = model_digest(frozen_pi0)
    utilities = torch.tensor([_utility(row) for row in examples], dtype=torch.float64)
    block_mean = float(utilities.mean().item())
    block_sd = float(utilities.std(unbiased=False).item())
    optimizer = _new_optimizer(model)
    diagnostic = _update(
        model, examples, block_mean=block_mean, block_sd=block_sd,
        step=1, optimizer=optimizer,
    )
    order_digest = hashlib.sha256(
        ",".join(str(int(row.owner_seed)) for row in examples).encode("ascii")
    ).hexdigest()
    return BlockTrainingResult(
        model=model,
        source_model_digest=source_digest,
        result_model_digest=model_digest(model),
        block_order_seed=0,
        owner_order_sha256=order_digest,
        diagnostics=(diagnostic,),
        status="CONDITIONAL_FIXED_ROOT_DIAGNOSTIC_ONE_UPDATE",
    )


def train_policy_iteration_block(
    frozen_pi_k: ActionConditionedPolicy,
    examples: Sequence[OwnerTrainingExample],
    *,
    block_order_seed: int,
    step_callback: Callable[[int, ActionConditionedPolicy, torch.optim.Optimizer, tuple[Any, ...], list[int], tuple[UpdateDiagnostic, ...]], None] | None = None,
    resume_state: Mapping[str, Any] | None = None,
) -> BlockTrainingResult:
    """Implement the locked 128-owner/64-step block; caller must hold Planner activation."""
    _validate_examples(examples, expected_count=FORMAL_BLOCK_OWNERS)
    model = deepcopy(frozen_pi_k)
    source_digest = model_digest(frozen_pi_k)
    utilities = torch.tensor([_utility(row) for row in examples], dtype=torch.float64)
    block_mean = float(utilities.mean().item())
    block_sd = float(utilities.std(unbiased=False).item())
    optimizer = _new_optimizer(model)
    rng = random.Random(int(block_order_seed))
    order = list(range(len(examples)))
    diagnostics: list[UpdateDiagnostic] = []
    owner_seed_sha256 = hashlib.sha256(
        ",".join(str(int(row.owner_seed)) for row in examples).encode("ascii")
    ).hexdigest()
    first_step = 0
    if resume_state is not None:
        if (
            resume_state.get("schema_id") != "issue75-policy-block-step-checkpoint-v1"
            or resume_state.get("block_order_seed") != int(block_order_seed)
            or resume_state.get("source_model_digest") != source_digest
            or resume_state.get("owner_seed_sha256") != owner_seed_sha256
        ):
            raise ValueError("optimizer checkpoint does not belong to this frozen policy block")
        first_step = int(resume_state.get("completed_steps", 0))
        if not 1 <= first_step <= FORMAL_STEPS_PER_BLOCK:
            raise ValueError("optimizer checkpoint has an invalid completed step count")
        saved_order = [int(index) for index in resume_state.get("owner_order", ())]
        if sorted(saved_order) != list(range(len(examples))):
            raise ValueError("optimizer checkpoint has an invalid owner permutation")
        model.load_state_dict(resume_state["model_state_dict"], strict=True)
        optimizer.load_state_dict(resume_state["optimizer_state_dict"])
        rng.setstate(resume_state["python_rng_state"])
        order = saved_order
        rows = resume_state.get("diagnostics", ())
        diagnostics = [UpdateDiagnostic(**dict(row)) for row in rows]
        if len(diagnostics) != first_step or [row.step for row in diagnostics] != list(range(1, first_step + 1)):
            raise ValueError("optimizer checkpoint diagnostics do not match the completed update prefix")
    for step in range(first_step, FORMAL_STEPS_PER_BLOCK):
        if step % (FORMAL_BLOCK_OWNERS // FORMAL_MINIBATCH_OWNERS) == 0:
            rng.shuffle(order)
        offset = (step % (FORMAL_BLOCK_OWNERS // FORMAL_MINIBATCH_OWNERS)) * FORMAL_MINIBATCH_OWNERS
        batch = [examples[index] for index in order[offset:offset + FORMAL_MINIBATCH_OWNERS]]
        if len(batch) != FORMAL_MINIBATCH_OWNERS:
            raise AssertionError("fixed block minibatch construction failed")
        diagnostics.append(_update(
            model, batch, block_mean=block_mean, block_sd=block_sd,
            step=step + 1, optimizer=optimizer,
        ))
        if step_callback is not None:
            step_callback(
                step + 1, model, optimizer, rng.getstate(), list(order), tuple(diagnostics),
            )
    order_digest = hashlib.sha256(
        ",".join(str(int(examples[index].owner_seed)) for index in order).encode("ascii")
    ).hexdigest()
    return BlockTrainingResult(
        model=model,
        source_model_digest=source_digest,
        result_model_digest=model_digest(model),
        block_order_seed=int(block_order_seed),
        owner_order_sha256=order_digest,
        diagnostics=tuple(diagnostics),
        status="FIXED_64_STEP_BLOCK_COMPLETE",
    )


def policy_probe_diagnostics(
    before: ActionConditionedPolicy,
    after: ActionConditionedPolicy,
    probe_features: Sequence[np.ndarray],
) -> dict[str, Any]:
    if not probe_features:
        raise ValueError("same held probe states are required for actor response diagnostics")
    tv_values: list[float] = []
    margins_before: list[float] = []
    margins_after: list[float] = []
    rank_changes = 0
    with torch.no_grad():
        for matrix in probe_features:
            features = torch.as_tensor(matrix, dtype=torch.float32)
            before_logits, before_logp, _ = before.distribution(features)
            after_logits, after_logp, _ = after.distribution(features)
            before_probs = torch.exp(before_logp)
            after_probs = torch.exp(after_logp)
            tv_values.append(float((0.5 * torch.abs(before_probs - after_probs).sum()).item()))
            sorted_before = torch.sort(before_logits, descending=True).values
            sorted_after = torch.sort(after_logits, descending=True).values
            margins_before.append(float((sorted_before[0] - sorted_before[1]).item()))
            margins_after.append(float((sorted_after[0] - sorted_after[1]).item()))
            rank_changes += int(torch.argmax(before_logits).item() != torch.argmax(after_logits).item())
    return {
        "probe_state_count": len(probe_features),
        "pre_post_distribution_total_variation_mean": float(np.mean(tv_values)),
        "pre_post_distribution_total_variation_max": float(np.max(tv_values)),
        "pre_action_top_two_logit_margin_mean": float(np.mean(margins_before)),
        "post_action_top_two_logit_margin_mean": float(np.mean(margins_after)),
        "argmax_rank_changes": rank_changes,
        "synthetic_probe_used": False,
    }
