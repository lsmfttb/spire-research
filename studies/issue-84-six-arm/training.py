"""Deterministic 64-step owner-block updates for the six registered arms."""
from __future__ import annotations

import hashlib
import random
from typing import Any, Callable, Sequence

import torch
from torch import nn

from gradients import OwnerOutcome, minibatch_loss


ACTOR_STEPS_PER_BLOCK = 64
FORMAL_OWNERS_PER_BLOCK = 128
PILOT_OWNERS_PER_BLOCK = 32
MINIBATCH_OWNERS = 32
ACTOR_GRAD_CLIP = 1.0


def optimizer_parameter_steps(optimizer: torch.optim.Optimizer) -> list[int]:
    return sorted(
        int(state["step"].item())
        for state in optimizer.state.values()
        if isinstance(state, dict) and "step" in state
    )


def train_actor_block(
    actor: nn.Module,
    optimizer: torch.optim.Optimizer,
    examples: Sequence[OwnerOutcome],
    *,
    arm: str,
    block_index: int,
    block_order_seed: int,
    value_model: nn.Module | None = None,
    action_value_model: nn.Module | None = None,
    behavior_actor: nn.Module | None = None,
    pilot: bool = False,
    resume_state: dict[str, Any] | None = None,
    step_callback: Callable[[int, nn.Module, torch.optim.Optimizer, object, list[int], list[dict[str, float]]], None] | None = None,
) -> dict[str, Any]:
    """Apply exactly 64 AdamW updates with #75's deterministic owner minibatches.

    Pilot blocks use their 32 registered nonformal owners as the whole minibatch
    on each of 64 steps; formal blocks use 4 minibatches of 32 owners, reshuffled
    each epoch. Both use the same optimizer API and arm objective.
    """
    arm = arm.upper()
    if arm not in {"A", "B", "C", "D", "E", "F"}:
        raise ValueError(f"unknown Issue 84 arm: {arm}")
    expected_count = PILOT_OWNERS_PER_BLOCK if pilot else FORMAL_OWNERS_PER_BLOCK
    if len(examples) != expected_count:
        raise ValueError(f"block {block_index} expected {expected_count} owners, got {len(examples)}")
    if len({int(row.owner_seed) for row in examples}) != expected_count or any(not row.complete for row in examples):
        raise ValueError("actor update needs unique complete owner outcomes")
    if expected_count % MINIBATCH_OWNERS:
        raise AssertionError("registered owner count must divide into 32-owner minibatches")
    if arm in {"D", "E", "F"} and value_model is None:
        raise ValueError(f"arm {arm} needs its frozen state-value predictor")
    if arm == "E" and action_value_model is None:
        raise ValueError("arm E needs its frozen action-value predictor")
    if arm == "F" and behavior_actor is None:
        raise ValueError("arm F needs the frozen policy that generated its block")

    actor.train()
    frozen_models = [model for model in (value_model, action_value_model, behavior_actor) if model is not None]
    for model in frozen_models:
        model.eval()
    if behavior_actor is actor:
        raise ValueError("F must retain a separate frozen block-generating policy snapshot")

    rng = random.Random(int(block_order_seed))
    order = list(range(expected_count))
    batches_per_epoch = expected_count // MINIBATCH_OWNERS
    diagnostics: list[dict[str, float]] = []
    first_step = 0
    if resume_state is not None:
        expected_owner_hash = hashlib.sha256(
            ",".join(str(int(row.owner_seed)) for row in examples).encode("ascii")
        ).hexdigest()
        if (
            resume_state.get("schema_id") != "issue84-actor-step-checkpoint-v1"
            or resume_state.get("arm") != arm
            or int(resume_state.get("block_index", -1)) != int(block_index)
            or int(resume_state.get("block_order_seed", -1)) != int(block_order_seed)
            or resume_state.get("owner_seed_sha256") != expected_owner_hash
        ):
            raise ValueError("actor resume checkpoint does not match this frozen block")
        first_step = int(resume_state.get("completed_steps", -1))
        if not 0 <= first_step <= ACTOR_STEPS_PER_BLOCK:
            raise ValueError("actor resume checkpoint has an invalid completed-step count")
        if first_step:
            actor.load_state_dict(resume_state["actor_state_dict"], strict=True)
            optimizer.load_state_dict(resume_state["optimizer_state_dict"])
            rng.setstate(resume_state["python_rng_state"])
            order = [int(value) for value in resume_state["owner_order"]]
            if sorted(order) != list(range(expected_count)):
                raise ValueError("actor resume checkpoint owner permutation is invalid")
            diagnostics = list(resume_state["diagnostics"])
            if len(diagnostics) != first_step:
                raise ValueError("actor resume checkpoint diagnostics do not cover completed steps")
    for step in range(first_step, ACTOR_STEPS_PER_BLOCK):
        if step % batches_per_epoch == 0:
            rng.shuffle(order)
        offset = (step % batches_per_epoch) * MINIBATCH_OWNERS
        minibatch = [examples[index] for index in order[offset:offset + MINIBATCH_OWNERS]]
        if len(minibatch) != MINIBATCH_OWNERS:
            raise AssertionError("registered minibatch construction failed")
        loss = minibatch_loss(
            actor,
            minibatch,
            arm=arm,
            block_examples=examples,
            value_model=value_model,
            action_value_model=action_value_model,
            behavior_actor=behavior_actor,
        )
        parameters = tuple(parameter for parameter in actor.parameters() if parameter.requires_grad)
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        before = float(torch.nn.utils.clip_grad_norm_(parameters, ACTOR_GRAD_CLIP).item())
        after = math_sqrt_grad(parameters)
        optimizer.step()
        diagnostic = {
            "step": float(step + 1),
            "total_loss": float(loss.detach().item()),
            "grad_norm_before_clip": before,
            "grad_norm_after_clip": after,
        }
        diagnostics.append(diagnostic)
        if step_callback is not None:
            step_callback(step + 1, actor, optimizer, rng.getstate(), order, diagnostics)

    expected_steps = 128 if block_index == 0 else 64
    actual_steps = optimizer_parameter_steps(optimizer)
    if len(actual_steps) != len(tuple(actor.parameters())) or set(actual_steps) != {expected_steps}:
        raise RuntimeError(
            f"AdamW step state mismatch after block {block_index}: expected {expected_steps}, got {actual_steps}"
        )
    owner_manifest = "\n".join(str(int(row.owner_seed)) for row in examples).encode("ascii")
    return {
        "schema_id": "issue84-actor-block-update-v1",
        "arm": arm,
        "block_index": int(block_index),
        "pilot_nonformal": bool(pilot),
        "owner_count": len(examples),
        "owner_seed_order_sha256": hashlib.sha256(owner_manifest).hexdigest(),
        "block_order_seed": int(block_order_seed),
        "completed_steps": len(diagnostics),
        "optimizer_parameter_steps": actual_steps,
        "diagnostics": diagnostics,
    }


def math_sqrt_grad(parameters: Sequence[torch.Tensor]) -> float:
    squared = torch.zeros((), dtype=torch.float64)
    for parameter in parameters:
        if parameter.grad is not None:
            squared += parameter.grad.detach().double().pow(2).sum()
    return float(torch.sqrt(squared).item())
