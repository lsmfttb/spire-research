"""Study-only B--F model heads for Issue 85.

The exact Issue 84 policy module remains the frozen/continued backbone. This
file adds only the registered representation treatments and never constructs
native actions; native freshness and occurrence binding stay in controller
glue. All new-path inputs are sanitized and checked before tensorization.
"""
from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from collections.abc import Mapping, Sequence
from typing import Any

import torch
from torch import nn

from feature_firewall import assert_index_free, sanitize_executor_tree


HASH_DIMENSION = 2048
WIDE_HASH_DIMENSION = 8192
TOKEN_DIMENSION = 128
FLAT_INTERACTION_DIMENSION = 512
ARMS = frozenset({"B", "C", "D", "E", "F"})


def _hash_coordinate(token: str, dimension: int) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    number = int.from_bytes(digest, "little")
    return number % dimension, 1.0 if digest[-1] & 1 else -1.0


def _typed_leaves(value: Any, path: str = "root") -> list[tuple[str, float]]:
    """Flatten a sanitized tree without encoding sequence positions.

    Field paths and value types remain explicit. Sequence multiplicity is
    preserved; order is not represented. Numeric fields contribute both an
    exact token and a bounded continuous value token.
    """
    result: list[tuple[str, float]] = []
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            child = value[key]
            result.extend(_typed_leaves(child, f"{path}.{key}"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if not value:
            result.append((f"{path}|empty-sequence", 1.0))
        else:
            for child in value:
                result.extend(_typed_leaves(child, f"{path}[]"))
    elif value is None:
        result.append((f"{path}|none", 1.0))
    elif isinstance(value, bool):
        result.append((f"{path}|bool|{str(value).lower()}", 1.0))
    elif isinstance(value, int):
        result.append((f"{path}|int|{value}", 1.0))
        result.append((f"{path}|number", math.copysign(math.log1p(abs(value)), value) / 10.0))
    elif isinstance(value, float):
        if math.isfinite(value):
            clipped = max(-100000.0, min(100000.0, value))
            result.append((f"{path}|float|{clipped:.8g}", 1.0))
            result.append((f"{path}|number", math.copysign(math.log1p(abs(clipped)), clipped) / 10.0))
        else:
            result.append((f"{path}|nonfinite", 1.0))
    else:
        result.append((f"{path}|str|{value}", 1.0))
    return result


def _vector(leaves: Sequence[tuple[str, float]], dimension: int, *, device: torch.device) -> torch.Tensor:
    output = torch.zeros(dimension, dtype=torch.float32, device=device)
    for token, amount in leaves:
        coordinate, sign = _hash_coordinate(token, dimension)
        output[coordinate] += sign * float(amount)
    return output


def fold_widened_hash_features(features: torch.Tensor) -> torch.Tensor:
    """Fold 8192 signed-hash features into their exact 2048-bucket sums."""
    if features.ndim != 2 or features.shape[1] != WIDE_HASH_DIMENSION:
        raise ValueError("widened hash matrix must have shape [actions, 8192]")
    # The 8192 hash uses the same digest modulo 8192, hence a legacy 2048
    # bucket receives entries at j, j+2048, j+4096 and j+6144.
    return features.reshape(features.shape[0], 4, HASH_DIMENSION).sum(dim=1)


def lift_first_layer(base_linear: nn.Linear) -> nn.Linear:
    """Create the output-preserving registered 2048→8192 weight lift."""
    if base_linear.in_features != HASH_DIMENSION:
        raise ValueError("legacy first layer must accept exactly 2048 features")
    widened = nn.Linear(WIDE_HASH_DIMENSION, base_linear.out_features, bias=base_linear.bias is not None)
    with torch.no_grad():
        widened.weight.copy_(base_linear.weight.repeat(1, 4))
        if base_linear.bias is not None:
            widened.bias.copy_(base_linear.bias)
    return widened


def _candidate_tree(row: Mapping[str, Any], *, include_public_action: bool) -> dict[str, Any]:
    result = {"features": row.get("features", {})}
    if include_public_action:
        result["kind"] = row.get("kind")
        result["public_choice"] = row.get("public_choice", {})
    return sanitize_executor_tree(result)


def _context_tree(payload: Mapping[str, Any], *, full: bool) -> dict[str, Any]:
    context = {"screen": payload.get("screen", {}), "shared_context": payload.get("shared_context", {})}
    if full:
        context["public_context"] = payload.get("public_context", {})
        candidates = payload.get("candidates", ())
        if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
            context["legal_candidate_set"] = [
                _candidate_tree(row, include_public_action=True)
                for row in candidates
                if isinstance(row, Mapping)
            ]
    return sanitize_executor_tree(context)


def _checked_payload(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    assert_index_free(payload)
    candidates = payload.get("candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)) or len(candidates) < 2:
        raise ValueError("Issue 85 residuals require at least two ordered legal candidates")
    if any(not isinstance(row, Mapping) for row in candidates):
        raise ValueError("candidate payload rows must be mappings")
    return list(candidates)


class FlatCandidateContextEncoder:
    """Signed-hash flat candidate×whole-context interaction encoder."""

    def __init__(self, dimension: int = FLAT_INTERACTION_DIMENSION) -> None:
        self.dimension = dimension

    def encode(self, payload: Mapping[str, Any], *, device: torch.device) -> torch.Tensor:
        candidates = _checked_payload(payload)
        context_leaves = _typed_leaves(_context_tree(payload, full=True), "context")
        if not context_leaves:
            raise ValueError("flat residual context is empty")
        rows: list[torch.Tensor] = []
        for row in candidates:
            candidate_leaves = _typed_leaves(_candidate_tree(row, include_public_action=True), "candidate")
            if not candidate_leaves:
                raise ValueError("flat residual candidate is empty")
            cross = [
                (f"cross|{candidate_token}|{context_token}", candidate_value * context_value)
                for candidate_token, candidate_value in candidate_leaves
                for context_token, context_value in context_leaves
            ]
            rows.append(_vector(cross, self.dimension, device=device))
        return torch.stack(rows)


class RelationalSetEncoder:
    """Typed candidate/context DeepSets features with candidate interactions."""

    def __init__(self, arm: str, dimension: int = TOKEN_DIMENSION) -> None:
        if arm not in {"E", "F"}:
            raise ValueError("typed set encoder is reserved for E or F")
        self.arm = arm
        self.dimension = dimension

    def encode(self, payload: Mapping[str, Any], *, device: torch.device) -> torch.Tensor:
        candidates = _checked_payload(payload)
        full = self.arm == "F"
        context = _context_tree(payload, full=full)
        context_vector = _vector(_typed_leaves(context, "context"), self.dimension, device=device)
        if full:
            context_vector = context_vector + _public_entity_set_vector(
                payload.get("public_context", {}), self.dimension, device=device
            )

        candidate_vectors: list[torch.Tensor] = []
        for row in candidates:
            candidate = _candidate_tree(row, include_public_action=full)
            candidate_vectors.append(
                _vector(_typed_leaves(candidate, "candidate"), self.dimension, device=device)
            )
        matrix = torch.stack(candidate_vectors)

        # F additionally represents the rest of the ordered legal set as an
        # unordered multiset. Excluding one row by occurrence preserves
        # duplicate multiplicity and is permutation equivariant.
        if full:
            total = matrix.sum(dim=0, keepdim=True)
            denominator = max(1, matrix.shape[0] - 1)
            others = (total - matrix) / denominator
        else:
            others = torch.zeros_like(matrix)

        context_rows = context_vector.expand_as(matrix)
        return torch.cat((matrix, context_rows, matrix * context_rows, matrix * others), dim=1)


def _public_entity_set_vector(public_context: Any, dimension: int, *, device: torch.device) -> torch.Tensor:
    """DeepSets-style sum over typed native-public entities and relations.

    Native coordinates and candidate/group pointers have already been joined
    and removed by the firewall. The remaining map nodes include their
    semantic outgoing-room relations; reward groups include their canonical
    semantic alternatives. Multiplicity is retained by summation.
    """
    if not isinstance(public_context, Mapping):
        raise ValueError("full-context public projection is not a mapping")
    entities: list[tuple[str, Any]] = []

    def extend_sequence(label: str, value: Any) -> None:
        if value is None:
            return
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError(f"public {label} entity collection is not a sequence")
        entities.extend((label, item) for item in value)

    resources = public_context.get("persistent_resources", {})
    resource_value = resources.get("value", {}) if isinstance(resources, Mapping) else {}
    if isinstance(resource_value, Mapping):
        for field in ("deck", "relics", "potion_identities", "keys"):
            collection = resource_value.get(field)
            if isinstance(collection, Sequence) and not isinstance(collection, (str, bytes)):
                extend_sequence(field, collection)
    extend_sequence("map_node", _nested_value(public_context.get("visible_map_graph")))
    extend_sequence("legal_route", _nested_value(public_context.get("immediately_legal_routes")))
    extend_sequence("screen_choice", public_context.get("screen_choices"))
    extend_sequence("reward_group", public_context.get("reward_card_groups"))

    result = torch.zeros(dimension, dtype=torch.float32, device=device)
    for label, entity in entities:
        result += _vector(_typed_leaves(entity, f"entity.{label}"), dimension, device=device)
    return result


def _nested_value(envelope: Any) -> Any:
    if not isinstance(envelope, Mapping) or envelope.get("availability") != "available":
        return None
    return envelope.get("value")


class ZeroOutputResidual(nn.Module):
    """A trainable nonlinear residual initialized to exactly zero output."""

    def __init__(self, input_dimension: int, seed: int) -> None:
        super().__init__()
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(seed)
            self.layers = nn.Sequential(
                nn.Linear(input_dimension, 64),
                nn.Tanh(),
                nn.Linear(64, 32),
                nn.Tanh(),
                nn.Linear(32, 1),
            )
        with torch.no_grad():
            self.layers[-1].weight.zero_()
            self.layers[-1].bias.zero_()

    def forward(self, features: torch.Tensor) -> torch.Tensor:
        return self.layers(features).squeeze(-1)


class Issue85Arm(nn.Module):
    """One registered new B–F policy, initialized from the exact π12 state."""

    def __init__(self, arm: str, model_module: Any, parent_state: Mapping[str, torch.Tensor], seed: int = 8500001):
        super().__init__()
        if arm not in ARMS:
            raise ValueError(f"not a new Issue 85 training arm: {arm}")
        self.arm = arm
        self.widened = arm in {"B", "D"}
        self.model_module = model_module
        runtime = model_module
        self.backbone = runtime.ActionConditionedPolicy(
            dimension=HASH_DIMENSION,
            seed=runtime.DEFAULT_MODEL_SEED if hasattr(runtime, "DEFAULT_MODEL_SEED") else 73507501,
            final_weight_std=runtime.ACTOR_FINAL_WEIGHT_STD,
        )
        self.backbone.load_state_dict(dict(parent_state), strict=True)
        if self.widened:
            self.backbone.actor[0] = lift_first_layer(self.backbone.actor[0])
            self.backbone.dimension = WIDE_HASH_DIMENSION

        if arm == "B":
            # HASH is the registered widened legacy scorer only; it has no
            # newly added sidecar and therefore no second feature tensor.
            self.encoder = None
            self.residual = None
        elif arm in {"C", "D"}:
            self.encoder = FlatCandidateContextEncoder()
            input_dimension = FLAT_INTERACTION_DIMENSION
            self.residual = ZeroOutputResidual(input_dimension, seed)
        else:
            self.encoder = RelationalSetEncoder(arm)
            input_dimension = TOKEN_DIMENSION * 4
            self.residual = ZeroOutputResidual(input_dimension, seed)

    def logits(self, legacy_features: torch.Tensor, payload: Mapping[str, Any]) -> torch.Tensor:
        expected_dim = WIDE_HASH_DIMENSION if self.widened else HASH_DIMENSION
        if legacy_features.ndim != 2 or legacy_features.shape[0] < 2 or legacy_features.shape[1] != expected_dim:
            raise ValueError("legacy backbone features do not match the arm's choice count/dimension")
        if not torch.isfinite(legacy_features).all():
            raise ValueError("legacy backbone features contain non-finite values")
        if self.arm == "B":
            return self.backbone.logits(legacy_features)
        rows = _checked_payload(payload)
        if legacy_features.shape[0] != len(rows):
            raise ValueError("legacy and new-path candidate counts disagree")
        device = legacy_features.device
        residual_features = self.encoder.encode(payload, device=device)
        if residual_features.shape != (len(rows), self.residual.layers[0].in_features):
            raise ValueError("new-path representation produced an unexpected shape")
        return self.backbone.logits(legacy_features) + self.residual(residual_features)

    def distribution(self, legacy_features: torch.Tensor, payload: Mapping[str, Any]) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        logits = self.logits(legacy_features, payload)
        log_probabilities = torch.log_softmax(logits, dim=0)
        probabilities = torch.softmax(logits, dim=0)
        entropy = -(probabilities * log_probabilities).sum()
        return logits, log_probabilities, entropy


def continue_parent_adamw(
    arm: Issue85Arm,
    optimizer_bundle: Mapping[str, Any],
    *,
    expected_parent_digest: str,
) -> tuple[torch.optim.AdamW, dict[str, Any]]:
    """Carry π12's exact step-64 AdamW state into a new B–F arm.

    Existing actor moments are matched by the locked model parameter order.
    For a widened first layer, first/second/max-second moments are repeated in
    the same four 2048-bucket blocks as the lifted weights. New residual
    parameters are appended to the same AdamW group without fabricated state;
    their per-parameter step starts at zero on the first optimizer update.
    """
    expected_steps = 64
    if optimizer_bundle.get("schema_id") != "issue75-policy-block-step-checkpoint-v1":
        raise ValueError("unsupported parent optimizer checkpoint schema")
    if int(optimizer_bundle.get("completed_steps", -1)) != expected_steps:
        raise ValueError("parent optimizer checkpoint is not at the registered step 64")
    saved_model_state = optimizer_bundle.get("model_state_dict")
    saved_optimizer_state = optimizer_bundle.get("optimizer_state_dict")
    if not isinstance(saved_model_state, Mapping) or not isinstance(saved_optimizer_state, Mapping):
        raise ValueError("parent optimizer checkpoint is missing model/optimizer state")

    model_type = arm.model_module.ActionConditionedPolicy
    source_model = model_type(dimension=HASH_DIMENSION, seed=73507501, final_weight_std=0.01)
    source_model.load_state_dict(dict(saved_model_state), strict=True)
    checkpoint_digest = arm.model_module.model_digest(source_model)
    if checkpoint_digest != expected_parent_digest:
        raise ValueError(
            "optimizer checkpoint is not the locked π12 parent: "
            f"checkpoint={checkpoint_digest} expected={expected_parent_digest}"
        )
    target_named = dict(arm.backbone.named_parameters())
    for name, source_parameter in source_model.named_parameters():
        target_parameter = target_named.get(name)
        if target_parameter is None:
            raise ValueError(f"arm backbone is missing parent parameter {name}")
        if arm.widened and name == "actor.0.weight":
            target_blocks = target_parameter.reshape(target_parameter.shape[0], 4, HASH_DIMENSION)
            if not all(torch.equal(target_blocks[:, block, :], source_parameter) for block in range(4)):
                raise ValueError("widened actor first layer is not the exact registered parent lift")
        elif target_parameter.shape != source_parameter.shape or not torch.equal(target_parameter, source_parameter):
            raise ValueError(f"arm backbone parameter {name} differs from the locked π12 parent")

    groups = saved_optimizer_state.get("param_groups")
    states = saved_optimizer_state.get("state")
    if not isinstance(groups, Sequence) or len(groups) != 1 or not isinstance(states, Mapping):
        raise ValueError("parent AdamW state must contain one parameter group")
    group = groups[0]
    parameter_ids = list(group.get("params", ()))
    source_named = list(source_model.named_parameters())
    if len(parameter_ids) != len(source_named) or set(target_named) != {name for name, _ in source_named}:
        raise ValueError("pinned optimizer parameter order differs from the locked actor")

    carried: dict[str, dict[str, Any]] = {}
    for parameter_id, (name, source_parameter) in zip(parameter_ids, source_named, strict=True):
        state = states.get(parameter_id)
        target_parameter = target_named[name]
        if not isinstance(state, Mapping) or "step" not in state:
            raise ValueError(f"parent optimizer has no state for {name}")
        step_value = state["step"]
        step_number = int(step_value.item()) if torch.is_tensor(step_value) else int(step_value)
        if step_number != expected_steps:
            raise ValueError(f"parent optimizer {name} step is {step_number}, expected 64")
        mapped: dict[str, Any] = {}
        for key, value in state.items():
            if not torch.is_tensor(value):
                mapped[key] = deepcopy(value)
                continue
            tensor = value.detach().clone().to(device=target_parameter.device)
            if key in {"exp_avg", "exp_avg_sq", "max_exp_avg_sq"} and tensor.shape != target_parameter.shape:
                if arm.widened and name == "actor.0.weight" and tensor.shape == source_parameter.shape:
                    tensor = tensor.repeat(1, 4)
                else:
                    raise ValueError(f"parent optimizer tensor {name}.{key} has incompatible shape {tuple(tensor.shape)}")
            if key in {"exp_avg", "exp_avg_sq", "max_exp_avg_sq"} and tensor.shape != target_parameter.shape:
                raise ValueError(f"mapped optimizer tensor {name}.{key} still has the wrong shape")
            mapped[key] = tensor
        carried[name] = mapped

    target_parameters = list(arm.backbone.parameters())
    residual_parameters = list(arm.residual.parameters()) if arm.residual is not None else []
    options = {key: deepcopy(value) for key, value in group.items() if key != "params"}
    optimizer = torch.optim.AdamW([{"params": target_parameters + residual_parameters, **options}])
    for name, parameter in target_named.items():
        optimizer.state[parameter] = carried[name]

    return optimizer, {
        "schema_id": "issue85-parent-adamw-continuation-v1",
        "parent_model_digest": expected_parent_digest,
        "parent_optimizer_completed_steps": expected_steps,
        "parent_source_model_digest": optimizer_bundle.get("source_model_digest"),
        "carried_parameter_names": list(carried),
        "widened_moment_mapping": "repeat first-layer exp_avg/exp_avg_sq/max_exp_avg_sq across four contiguous 2048-bucket blocks"
        if arm.widened
        else "identity by pinned actor parameter name and shape",
        "fresh_residual_parameter_count": sum(parameter.numel() for parameter in residual_parameters),
        "fresh_residual_parameter_steps": "uninitialized; starts at step 1 on first update",
        "optimizer_group": {key: value for key, value in options.items()},
    }
