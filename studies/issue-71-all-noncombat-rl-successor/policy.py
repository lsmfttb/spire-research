"""Issue 71's study-only semantic candidate policy.

The private native projection is consumed only inside this study controller.
Opaque candidate/action bindings are checked for coverage and positional
association, then excluded from model input. The public STSRL decision context
is not modified or given privileged projection values.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import torch
from torch import nn


PROJECTION_SCHEMA = "native-privileged-noncombat-choice-projection-v1"
PROJECTION_AUDIENCE = "training_oracle_only"
PROJECTION_ORDER = (
    "StepSimulator::legalActions; ordered occurrences are retained without deduplication"
)
MODEL_INPUT_SCHEMA = "issue71-signed-hash-semantic-candidate-v1"
DEFAULT_HASH_DIMENSION = 2048
VISIBILITIES = frozenset({"public_now", "oracle_only", "unavailable"})

_FORBIDDEN_KEYS = frozenset({
    "candidate_binding", "candidate_index", "candidate_id", "candidate_ids",
    "action_binding", "action_index", "action_id", "action_ids",
    "legal_action_index", "legal_action_id", "stable_id", "bits", "idx1",
    "idx2", "idx3", "scope", "native", "label", "source",
})


class ProjectionError(RuntimeError):
    """A native candidate association or visibility contract failed closed."""


def validate_live_native_candidate_bindings(
    projection: Mapping[str, Any], actions: Sequence[Any]
) -> None:
    """Fail closed unless each projected occurrence binds to this live action.

    The native projection's occurrence index is the exact ordered join key.
    Its public action descriptor omits opaque bits and may sanitize labels;
    when bits are present they are checked only as an association key, never
    sent to the feature encoder or treated as card/event semantics.
    """
    candidates = projection.get("candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ProjectionError("native candidate bindings are not an ordered sequence")
    if len(candidates) != len(actions):
        raise ProjectionError("native candidate binding count differs from live legal actions")
    for index, (candidate, action) in enumerate(zip(candidates, actions, strict=True)):
        if not isinstance(candidate, Mapping):
            raise ProjectionError(f"candidate occurrence {index} is not a mapping")
        binding = candidate.get("candidate_binding")
        if not isinstance(binding, Mapping) or binding.get("availability") != "available":
            raise ProjectionError(f"candidate occurrence {index} has no available native binding")
        if binding.get("visibility") != "oracle_only":
            raise ProjectionError(f"candidate occurrence {index} binding is not explicitly oracle-only")
        value = binding.get("value")
        candidate_index = value.get("candidate_index") if isinstance(value, Mapping) else None
        if isinstance(candidate_index, bool) or not isinstance(candidate_index, int) or candidate_index != index:
            raise ProjectionError(f"candidate occurrence {index} lost its native legal-action position")
        bound = value.get("candidate_action")
        raw = getattr(action, "raw", None)
        if not isinstance(bound, Mapping) or not isinstance(raw, Mapping):
            raise ProjectionError(f"candidate occurrence {index} has no comparable live native action")
        expected = {
            "scope": raw.get("scope"),
            "bits": raw.get("bits"),
            "kind": getattr(action, "kind", None),
            "idx1": raw.get("idx1"),
            "idx2": raw.get("idx2"),
            "idx3": raw.get("idx3"),
        }
        required = {"scope", "kind", "idx1", "idx2", "idx3"}
        if not required.issubset(bound) or not set(bound).issubset(required | {"bits", "label"}):
            raise ProjectionError(f"candidate occurrence {index} native descriptor has an unexpected field set")
        for key in required | ({"bits"} if "bits" in bound else set()):
            if bound[key] != expected[key]:
                raise ProjectionError(
                    f"candidate occurrence {index} native {key} binding differs from live legal action"
                )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _forbidden_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return normalized in _FORBIDDEN_KEYS or normalized.startswith("native_action_")


class SemanticHashEncoder:
    """Fixed, order-insensitive semantic feature encoder with multiplicity."""

    def __init__(self, dimension: int = DEFAULT_HASH_DIMENSION) -> None:
        if dimension < 128:
            raise ValueError("hash dimension must be at least 128")
        self.dimension = int(dimension)

    def _add(self, vector: np.ndarray, token: str, value: float = 1.0) -> None:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        number = int.from_bytes(digest, "little")
        index = number % self.dimension
        sign = 1.0 if digest[0] & 1 else -1.0
        vector[index] += np.float32(sign * value)

    def _leaf(self, vector: np.ndarray, path: str, visibility: str, value: Any) -> None:
        prefix = f"{visibility}|{path}"
        if value is None:
            self._add(vector, f"{prefix}|none")
        elif isinstance(value, bool):
            self._add(vector, f"{prefix}|bool|{str(value).lower()}")
        elif isinstance(value, int):
            # Preserve semantic enum/id values while also exposing numeric scale.
            self._add(vector, f"{prefix}|int|{value}")
            transformed = math.copysign(math.log1p(min(abs(value), 100000)), value) / 10.0
            self._add(vector, f"{prefix}|numeric", transformed)
        elif isinstance(value, float):
            if not math.isfinite(value):
                self._add(vector, f"{prefix}|nonfinite")
                return
            clipped = max(-100000.0, min(100000.0, value))
            transformed = math.copysign(math.log1p(abs(clipped)), clipped) / 10.0
            self._add(vector, f"{prefix}|numeric", transformed)
            if abs(clipped) <= 100.0 and clipped.is_integer():
                self._add(vector, f"{prefix}|int|{int(clipped)}")
        else:
            self._add(vector, f"{prefix}|str|{str(value)}")

    def _flatten(self, vector: np.ndarray, path: str, visibility: str, value: Any) -> None:
        if isinstance(value, Mapping):
            for raw_key, nested in sorted(value.items(), key=lambda item: str(item[0])):
                key = str(raw_key)
                if _forbidden_key(key):
                    continue
                self._flatten(vector, f"{path}.{key}", visibility, nested)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            # Sequence position is intentionally omitted; duplicate values add
            # multiple counts and retain multiplicity without leaking an action
            # slot/index as a semantic feature.
            for nested in value:
                self._flatten(vector, f"{path}[]", visibility, nested)
            if not value:
                self._add(vector, f"{visibility}|{path}|empty_sequence")
            return
        self._leaf(vector, path, visibility, value)

    def _feature_map(self, vector: np.ndarray, prefix: str, fields: Mapping[str, Any]) -> dict[str, int]:
        source_audit: dict[str, int] = {}
        for raw_key, item in sorted(fields.items(), key=lambda pair: str(pair[0])):
            key = str(raw_key)
            if _forbidden_key(key):
                continue
            if not isinstance(item, Mapping):
                raise ProjectionError(f"feature {prefix}.{key} lacks tagged native metadata")
            visibility = item.get("visibility")
            availability = item.get("availability")
            source = item.get("source")
            if availability not in {"available", "unavailable"}:
                raise ProjectionError(f"feature {prefix}.{key} has unsupported visibility/availability")
            path = f"{prefix}.{key}"
            if availability == "unavailable":
                # The native schema intentionally omits visibility/source for
                # optional future fields. Map that explicit absence to the
                # unavailable channel and never encode its diagnostic reason.
                if visibility not in {None, "unavailable"}:
                    raise ProjectionError(f"unavailable feature {path} has a conflicting visibility")
                if source is not None and (not isinstance(source, str) or not source):
                    raise ProjectionError(f"unavailable feature {path} has malformed source provenance")
                audit_source = source or "native_unavailable_without_source"
                source_audit[audit_source] = source_audit.get(audit_source, 0) + 1
                self._add(vector, f"unavailable|{path}|availability|unavailable")
                continue
            if visibility not in {"public_now", "oracle_only"}:
                raise ProjectionError(f"available feature {path} has unsupported visibility")
            if not isinstance(source, str) or not source:
                raise ProjectionError(f"available feature {path} has no source provenance")
            source_audit[source] = source_audit.get(source, 0) + 1
            self._add(vector, f"{visibility}|{path}|availability|{availability}")
            if "value" not in item:
                raise ProjectionError(f"available feature {path} has no value")
            self._flatten(vector, path, str(visibility), item["value"])
        return source_audit

    def encode(self, projection: Mapping[str, Any], legal_action_count: int) -> tuple[np.ndarray, dict[str, Any]]:
        if projection.get("schema_id") != PROJECTION_SCHEMA:
            raise ProjectionError("unsupported native privileged projection schema")
        if projection.get("audience") != PROJECTION_AUDIENCE:
            raise ProjectionError("native projection audience is not training_oracle_only")
        if projection.get("public_api_safe") is not False:
            raise ProjectionError("native privileged projection unexpectedly claims public safety")
        if projection.get("native_source_commit") != "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2":
            raise ProjectionError("native projection source commit does not match frozen source")
        if projection.get("candidate_order_source") != PROJECTION_ORDER:
            raise ProjectionError("native candidate order is not the accepted ordered legal-action source")
        candidates = projection.get("candidates")
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
            raise ProjectionError("native projection candidates are not an ordered sequence")
        count = len(candidates)
        if count != legal_action_count or projection.get("candidate_count") != legal_action_count:
            raise ProjectionError("native candidate occurrence count does not equal legal native actions")
        if projection.get("coverage_status") != "supported" or projection.get("supported_candidate_count") != count:
            raise ProjectionError("native projection does not support every live legal candidate")
        if projection.get("unsupported_candidates"):
            raise ProjectionError("native projection reported an unsupported live legal candidate")
        screen = projection.get("screen_identity")
        if not isinstance(screen, Mapping) or screen.get("availability") != "available" or screen.get("visibility") != "public_now":
            raise ProjectionError("native projection screen identity is not available/public_now")
        dim = self.dimension
        matrix = np.zeros((count, dim), dtype=np.float32)
        sources: dict[str, int] = {}
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, Mapping) or candidate.get("coverage_status") != "supported":
                raise ProjectionError(f"candidate occurrence {index} is not supported")
            binding = candidate.get("candidate_binding")
            if not isinstance(binding, Mapping) or binding.get("availability") != "available":
                raise ProjectionError(f"candidate occurrence {index} has no native mapping binding")
            features = candidate.get("features")
            if not isinstance(features, Mapping) or not features:
                raise ProjectionError(f"candidate occurrence {index} has no semantic feature map")
            source_counts = self._feature_map(matrix[index], "shared", projection.get("shared_context", {}))
            for source, amount in source_counts.items():
                sources[source] = sources.get(source, 0) + amount
            screen_source = self._feature_map(matrix[index], "screen", {"screen": screen})
            for source, amount in screen_source.items():
                sources[source] = sources.get(source, 0) + amount
            candidate_source = self._feature_map(matrix[index], "candidate", features)
            for source, amount in candidate_source.items():
                sources[source] = sources.get(source, 0) + amount
        return matrix, {
            "schema_id": MODEL_INPUT_SCHEMA,
            "visibility_channels": ["public_now", "oracle_only", "unavailable"],
            "projection_schema_id": PROJECTION_SCHEMA,
            "native_source_commit": projection.get("native_source_commit"),
            "screen": screen.get("value"),
            "legal_action_count": legal_action_count,
            "candidate_count": count,
            "candidate_order_source": projection.get("candidate_order_source"),
            "feature_source_counts": sources,
            "opaque_bindings_used_for_model_input": False,
        }


class AllNoncombatPolicy(nn.Module):
    """One shared categorical action-conditioned scorer across all screens."""

    def __init__(self, dimension: int = DEFAULT_HASH_DIMENSION, seed: int = 73507001) -> None:
        super().__init__()
        torch.manual_seed(seed)
        self.actor = nn.Sequential(
            nn.Linear(dimension, 128), nn.Tanh(),
            nn.Linear(128, 64), nn.Tanh(),
            nn.Linear(64, 1),
        )
        self.critic = nn.Sequential(nn.Linear(dimension, 64), nn.Tanh(), nn.Linear(64, 1))
        with torch.no_grad():
            final = self.actor[-1]
            final.weight.normal_(mean=0.0, std=0.0001)
            final.bias.zero_()

    def distribution(self, features: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        if features.ndim != 2 or features.shape[0] < 1:
            raise ValueError("candidate features must have shape [nonempty_actions, dimension]")
        logits = self.actor(features).squeeze(-1)
        log_probs = torch.log_softmax(logits, dim=0)
        probs = torch.softmax(logits, dim=0)
        value = self.critic(features.mean(dim=0)).squeeze(-1)
        return logits, log_probs, value

    @torch.no_grad()
    def choose(self, matrix: np.ndarray, generator: torch.Generator) -> tuple[int, np.ndarray, float]:
        features = torch.as_tensor(matrix, dtype=torch.float32)
        _, log_probs, value = self.distribution(features)
        probs = torch.exp(log_probs)
        chosen = int(torch.multinomial(probs, 1, generator=generator).item())
        return chosen, probs.cpu().numpy(), float(value.item())


def derived_seed(study_id: str, owner_seed: int, arm: str, model_digest: str, purpose: str) -> int:
    payload = f"{study_id}|{owner_seed}|{arm}|{model_digest}|{purpose}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & 0x7FFFFFFF
