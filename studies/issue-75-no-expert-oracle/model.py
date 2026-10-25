from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import torch
from torch import nn

from algorithm import Decision

NATIVE_SOURCE_COMMIT = "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2"
DEFAULT_HASH_DIMENSION = 2048
ACTOR_FINAL_WEIGHT_STD = 0.01


def load_issue73_encoder(source_root: Path) -> Any:
    """Load the reviewed Issue 73 encoder without making public API changes."""
    path = source_root / "policy.py"
    spec = importlib.util.spec_from_file_location("issue73_locked_policy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the locked Issue 73 feature encoder")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
        raise
    if getattr(module, "EXPECTED_NATIVE", NATIVE_SOURCE_COMMIT) != NATIVE_SOURCE_COMMIT:
        raise RuntimeError("Issue 73 feature encoder native source lock changed")
    return module.SemanticHashEncoder(DEFAULT_HASH_DIMENSION)


class ActionConditionedPolicy(nn.Module):
    """One shared categorical non-Expert scorer over native semantic candidates."""

    def __init__(
        self,
        *,
        dimension: int = DEFAULT_HASH_DIMENSION,
        seed: int = 73507501,
        final_weight_std: float = ACTOR_FINAL_WEIGHT_STD,
    ) -> None:
        super().__init__()
        if dimension != DEFAULT_HASH_DIMENSION:
            raise ValueError("policy dimension differs from the locked semantic encoder")
        if final_weight_std != ACTOR_FINAL_WEIGHT_STD:
            raise ValueError("actor final initialization differs from the locked low-commitment setting")
        self.dimension = int(dimension)
        self.initialization_seed = int(seed)
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(self.initialization_seed)
            self.actor = nn.Sequential(
                nn.Linear(self.dimension, 128),
                nn.Tanh(),
                nn.Linear(128, 64),
                nn.Tanh(),
                nn.Linear(64, 1),
            )
            with torch.no_grad():
                final = self.actor[-1]
                final.weight.normal_(mean=0.0, std=ACTOR_FINAL_WEIGHT_STD)
                final.bias.zero_()

    def logits(self, candidate_features: torch.Tensor) -> torch.Tensor:
        if candidate_features.ndim != 2 or candidate_features.shape[0] < 2:
            raise ValueError("training policy accepts only a nonempty live multi-choice candidate matrix")
        if candidate_features.shape[1] != self.dimension:
            raise ValueError("candidate feature dimension differs from the locked encoder")
        if not torch.isfinite(candidate_features).all():
            raise ValueError("candidate feature matrix contains non-finite values")
        return self.actor(candidate_features).squeeze(-1)

    def distribution(self, candidate_features: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        logits = self.logits(candidate_features)
        log_probabilities = torch.log_softmax(logits, dim=0)
        probabilities = torch.softmax(logits, dim=0)
        entropy = -(probabilities * log_probabilities).sum()
        return logits, log_probabilities, entropy

    def encode_projection(
        self,
        encoder: Any,
        projection: Mapping[str, Any],
        legal_actions: Sequence[Any],
    ) -> np.ndarray:
        if projection.get("audience") != "training_oracle_only":
            raise ValueError("normal-public or unsupported information regime reached the training policy")
        if projection.get("public_api_safe") is not False:
            raise ValueError("privileged training projection unexpectedly claims public safety")
        if projection.get("native_source_commit") != NATIVE_SOURCE_COMMIT:
            raise ValueError("privileged projection came from a different native source")
        if len(legal_actions) < 2:
            raise ValueError("semantic action policy requires a live multi-choice screen")
        matrix, audit = encoder.encode(projection, len(legal_actions))
        if audit.get("opaque_bindings_used_for_model_input") is not False:
            raise ValueError("opaque native action bindings leaked into policy features")
        if matrix.shape != (len(legal_actions), self.dimension):
            raise ValueError("encoder did not produce one fixed-width row per legal native occurrence")
        return matrix

    def sample(
        self,
        *,
        features: np.ndarray,
        generator: torch.Generator,
    ) -> tuple[int, np.ndarray, np.ndarray, float, float]:
        tensor = torch.as_tensor(features, dtype=torch.float32)
        logits, log_probabilities, entropy = self.distribution(tensor)
        probabilities = torch.exp(log_probabilities)
        selected = int(torch.multinomial(probabilities, 1, generator=generator).item())
        return (
            selected,
            probabilities.detach().cpu().numpy(),
            log_probabilities.detach().cpu().numpy(),
            float(entropy.detach().cpu().item()),
            float(logits[selected].detach().cpu().item()),
        )


def model_digest(model: nn.Module) -> str:
    digest = hashlib.sha256()
    for name, tensor in sorted(model.state_dict().items()):
        value = tensor.detach().cpu().contiguous()
        digest.update(name.encode("utf-8"))
        digest.update(str(value.dtype).encode("ascii"))
        digest.update(repr(tuple(value.shape)).encode("ascii"))
        digest.update(value.numpy().tobytes())
    return digest.hexdigest()


def make_training_decision(
    *,
    policy: ActionConditionedPolicy,
    encoder: Any,
    projection: Mapping[str, Any],
    legal_actions: Sequence[Any],
    legal_identities: Sequence[Mapping[str, Any]],
    state_fingerprint: str,
    step_index: int,
    generator: torch.Generator,
    checkpoint: Any = None,
    is_root: bool = False,
) -> tuple[Decision, np.ndarray, np.ndarray, float, float]:
    if len(legal_actions) != len(legal_identities):
        raise ValueError("native actions and occurrence identities differ in length")
    features = policy.encode_projection(encoder, projection, legal_actions)
    selected, probabilities, log_probabilities, entropy, chosen_logit = policy.sample(
        features=features,
        generator=generator,
    )
    decision = Decision(
        step_index=int(step_index),
        state_fingerprint=str(state_fingerprint),
        legal_identities=tuple(dict(row) for row in legal_identities),
        chosen_index=selected,
        candidate_features=features,
        log_probabilities=log_probabilities,
        checkpoint=checkpoint,
        is_root=bool(is_root),
    )
    return decision, probabilities, log_probabilities, entropy, chosen_logit
