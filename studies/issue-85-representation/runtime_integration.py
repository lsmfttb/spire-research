"""Source-preserving adapters that connect Issue 85 arms to the pinned runner.

The locked #73 controller still owns legal actions, simulator state, and native
execution.  This module adds the new feature path at its existing encode/choose
boundary and carries the exact training-time inputs through #75's retained
decision records.  It does not edit or replace any pinned upstream source.
"""

from __future__ import annotations

import base64
from contextvars import ContextVar
from dataclasses import dataclass, replace
import hashlib
import json
import zlib
from typing import Any, Callable, Mapping

import numpy as np
import torch

from feature_firewall import assert_index_free

HASH_DIMENSION = 2048
WIDE_HASH_DIMENSION = 8192
ISSUE85_ARMS = frozenset({"B", "C", "D", "E", "F"})


@dataclass(frozen=True)
class DecisionInput:
    """Inputs bound to one live native legal-action occurrence list."""

    arm: str
    legacy_sha256: str
    actor_features: np.ndarray
    payload: Mapping[str, Any] | None


_CURRENT_DECISION: ContextVar[DecisionInput | None] = ContextVar(
    "issue85_current_decision", default=None,
)


def _matrix_bytes(matrix: Any) -> tuple[np.ndarray, bytes]:
    value = np.asarray(matrix, dtype="<f4")
    if value.ndim != 2 or value.shape[0] < 2 or not np.isfinite(value).all():
        raise ValueError("Issue 85 actor input must be a finite multi-candidate matrix")
    value = np.ascontiguousarray(value, dtype="<f4")
    return value, value.tobytes(order="C")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _public_payload(adapter: Any, snapshot: Any) -> Mapping[str, Any]:
    # The locked adapter's public_projection() is the older T014 surface. The
    # registered representation study requires the current V3 projection, so
    # call the pinned native binding explicitly and validate the exact snapshot
    # before reading it. Never silently substitute the legacy projection.
    simulator = getattr(adapter, "_sim", None)
    project_v3 = getattr(simulator, "public_projection_v3", None)
    assert_current = getattr(adapter, "_assert_snapshot_is_current", None)
    if not callable(project_v3) or not callable(assert_current):
        raise RuntimeError(
            "pinned Issue 73 adapter must expose the native V3 projection and snapshot freshness check"
        )
    assert_current(snapshot)
    projection = project_v3()
    serialized = getattr(projection, "canonical_payload", None)
    if isinstance(serialized, str):
        value = json.loads(serialized)
    elif isinstance(projection, Mapping):
        value = dict(projection)
    else:
        raise TypeError("pinned native publicProjectionV3 returned a non-JSON projection")
    if not isinstance(value, Mapping):
        raise TypeError("pinned native publicProjectionV3 payload is not an object")
    if value.get("schema_id") != "native-public-projection-v3":
        raise ValueError("Issue 85 requires the locked native publicProjectionV3 schema")
    return value


class ContextualEncoder:
    """Preserve the locked legacy matrix and bind any new inputs to that call."""

    def __init__(
        self,
        legacy_encoder: Any,
        *,
        arm: str,
        adapter: Any,
        snapshot: Any,
        wide_encoder: Any,
        payload_builder: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
    ) -> None:
        self.legacy_encoder = legacy_encoder
        self.arm = str(arm).upper()
        self.adapter = adapter
        self.snapshot = snapshot
        self.wide_encoder = wide_encoder
        self.payload_builder = payload_builder

    def encode(self, privileged_projection: Mapping[str, Any], action_count: int):
        legacy_matrix, legacy_audit = self.legacy_encoder.encode(
            privileged_projection, action_count,
        )
        legacy, legacy_bytes = _matrix_bytes(legacy_matrix)
        if legacy.shape != (int(action_count), HASH_DIMENSION):
            raise ValueError("pinned Issue 73 encoder did not return the locked 2048-wide matrix")

        actor_features = legacy
        if self.arm in {"B", "D"}:
            wide_matrix, _ = self.wide_encoder.encode(privileged_projection, action_count)
            wide, _ = _matrix_bytes(wide_matrix)
            if wide.shape != (int(action_count), WIDE_HASH_DIMENSION):
                raise ValueError("8192-wide encoder returned an unexpected native choice shape")
            folded = wide.reshape(len(wide), 4, HASH_DIMENSION).sum(axis=1)
            if not np.allclose(folded, legacy, rtol=1e-6, atol=1e-6):
                raise ValueError("widened signed-hash input does not fold to the locked legacy features")
            actor_features = wide

        payload = None
        if self.arm in {"C", "D", "E", "F"}:
            payload = self.payload_builder(
                privileged_projection,
                _public_payload(self.adapter, self.snapshot),
            )
            if not isinstance(payload, Mapping):
                raise TypeError("Issue 85 new-path builder did not return a mapping")
            assert_index_free(payload)
            if len(payload.get("candidates", ())) != int(action_count):
                raise ValueError("new-path payload candidate count differs from the live legal list")

        _CURRENT_DECISION.set(DecisionInput(
            arm=self.arm,
            legacy_sha256=_sha256(legacy_bytes),
            actor_features=actor_features.copy(),
            payload=payload,
        ))
        return legacy_matrix, legacy_audit


class ContextTrace(list):
    """Attach decision inputs to the existing volatile training trace."""

    def append(self, item: Any) -> None:
        if isinstance(item, Mapping) and item.get("kind") == "categorical":
            current = _CURRENT_DECISION.get()
            if current is not None:
                item = {
                    **item,
                    "issue85_arm": current.arm,
                    "issue85_payload": current.payload,
                    "issue85_actor_features": current.actor_features,
                    "issue85_legacy_sha256": current.legacy_sha256,
                }
        super().append(item)


class Issue85Decision:
    """Training-only view; all locked upstream Decision fields pass through."""

    __slots__ = ("_base", "issue85_payload", "wide_candidate_features", "legacy_sha256")

    def __init__(
        self,
        base: Any,
        *,
        payload: Mapping[str, Any] | None,
        wide_candidate_features: Any | None,
        legacy_sha256: str,
    ) -> None:
        self._base = base
        self.issue85_payload = payload
        self.wide_candidate_features = wide_candidate_features
        self.legacy_sha256 = str(legacy_sha256)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def _pack_matrix(matrix: Any) -> dict[str, Any]:
    value, raw = _matrix_bytes(matrix)
    if value.shape[1] not in {HASH_DIMENSION, WIDE_HASH_DIMENSION}:
        raise ValueError("Issue 85 actor matrix has an unsupported feature width")
    return {
        "shape": list(value.shape),
        "sha256": _sha256(raw),
        "zlib_base64": base64.b64encode(zlib.compress(raw, level=6)).decode("ascii"),
    }


def _unpack_matrix(value: Mapping[str, Any]) -> np.ndarray:
    shape = value.get("shape")
    if not isinstance(shape, list) or len(shape) != 2:
        raise ValueError("stored Issue 85 actor matrix has an invalid shape")
    rows, columns = map(int, shape)
    if rows < 2 or columns not in {HASH_DIMENSION, WIDE_HASH_DIMENSION}:
        raise ValueError("stored Issue 85 actor matrix has an unsupported width")
    raw = zlib.decompress(base64.b64decode(value["zlib_base64"], validate=True))
    if len(raw) != rows * columns * 4 or _sha256(raw) != value.get("sha256"):
        raise ValueError("stored Issue 85 actor matrix fails its length or digest check")
    matrix = np.frombuffer(raw, dtype="<f4").reshape((rows, columns)).copy()
    if not np.isfinite(matrix).all():
        raise ValueError("stored Issue 85 actor matrix contains non-finite values")
    return matrix


def _pack_payload(payload: Mapping[str, Any]) -> dict[str, str]:
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return {
        "sha256": _sha256(raw),
        "zlib_base64": base64.b64encode(zlib.compress(raw, level=6)).decode("ascii"),
    }


def _unpack_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = zlib.decompress(base64.b64decode(value["zlib_base64"], validate=True))
    if _sha256(raw) != value.get("sha256"):
        raise ValueError("stored Issue 85 semantic payload fails its digest check")
    result = json.loads(raw)
    if not isinstance(result, Mapping):
        raise ValueError("stored Issue 85 semantic payload is not an object")
    return result


def _is_issue85_arm(actor: Any, arm_type: type) -> bool:
    return isinstance(actor, arm_type) and str(getattr(actor, "arm", "")).upper() in ISSUE85_ARMS


def _issue85_actor_from_model(model: Any, arm_type: type) -> Any | None:
    """Accept both the pinned controller's model object and PolicyAdapter shape."""
    if _is_issue85_arm(model, arm_type):
        return model
    policy = getattr(model, "policy", None)
    if _is_issue85_arm(policy, arm_type):
        return policy
    return None


def _decision_with_issue85_inputs(base: Any, row: Mapping[str, Any]) -> Any:
    """Bind runtime-created #75 decisions to their exact categorical trace row."""
    arm = str(row.get("issue85_arm", "")).upper()
    if arm not in ISSUE85_ARMS:
        raise ValueError("Issue 85 training trace has no registered arm identity")
    legacy, legacy_bytes = _matrix_bytes(base.candidate_features)
    if legacy.shape[1] != HASH_DIMENSION:
        raise ValueError("runtime decision does not contain the locked 2048-wide legacy matrix")
    legacy_digest = _sha256(legacy_bytes)
    if row.get("issue85_legacy_sha256") != legacy_digest:
        raise ValueError("training trace Issue 85 context does not bind to its candidate matrix")

    actor_value = row.get("issue85_actor_features")
    if actor_value is None:
        raise ValueError("Issue 85 training trace lost its actor feature matrix")
    actor_features, _ = _matrix_bytes(actor_value)
    if actor_features.shape[0] != legacy.shape[0]:
        raise ValueError("Issue 85 training matrix candidate count differs from the legacy matrix")
    expected_width = WIDE_HASH_DIMENSION if arm in {"B", "D"} else HASH_DIMENSION
    if actor_features.shape[1] != expected_width:
        raise ValueError(f"Issue 85 {arm} trace has the wrong actor feature width")
    wide = None
    if expected_width == WIDE_HASH_DIMENSION:
        folded = actor_features.reshape(len(actor_features), 4, HASH_DIMENSION).sum(axis=1)
        if not np.allclose(folded, legacy, rtol=1e-6, atol=1e-6):
            raise ValueError("Issue 85 widened training matrix does not fold to legacy features")
        wide = actor_features
    elif not np.array_equal(actor_features, legacy):
        raise ValueError("Issue 85 legacy-width actor features differ from the pinned matrix")

    payload = row.get("issue85_payload")
    if arm in {"C", "D", "E", "F"}:
        if not isinstance(payload, Mapping):
            raise ValueError(f"Issue 85 {arm} training trace lost its semantic payload")
        assert_index_free(payload)
        if len(payload.get("candidates", ())) != len(legacy):
            raise ValueError("Issue 85 training payload candidate count differs from the native matrix")
    elif payload is not None:
        raise ValueError("Issue 85 B HASH trace unexpectedly carries a contextual payload")

    return Issue85Decision(
        base,
        payload=payload,
        wide_candidate_features=wide,
        legacy_sha256=legacy_digest,
    )


def install_issue85_runtime(
    runtime: Any,
    executor: Any,
    gradients: Any,
    *,
    arm_type: type,
    payload_builder: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Install adapters around the exact #75/#73 APIs after source verification.

    Call this after the accepted #84 ``load_modules`` has returned its modules.
    Repeated installation into the same imported modules is safe.
    """
    existing = getattr(runtime, "_issue85_runtime_install", None)
    if existing is not None:
        return dict(existing)

    original_policy_adapter = runtime.PolicyAdapter
    original_loader = runtime.load_issue73_runner
    original_decision_from_trace = executor._decision_from_trace
    original_decision_rows = runtime._decision_rows
    original_hash_only_decisions = runtime._hash_only_decisions
    original_decode_trial = executor._decode_trial
    original_owner_loss = gradients.owner_loss

    class Issue85PolicyAdapter(original_policy_adapter):
        def choose(self, matrix: Any, generator: Any) -> tuple[int, Any, float]:
            if not _is_issue85_arm(self.policy, arm_type):
                return super().choose(matrix, generator)
            import torch

            current = _CURRENT_DECISION.get()
            if current is None:
                raise RuntimeError("Issue 85 policy was called without a same-decision native context")
            legacy, raw = _matrix_bytes(matrix)
            if _sha256(raw) != current.legacy_sha256:
                raise RuntimeError("Issue 85 context belongs to a different candidate matrix")
            features = torch.as_tensor(current.actor_features, dtype=torch.float32)
            payload = current.payload or {}
            with torch.no_grad():
                _logits, log_probabilities, _entropy = self.policy.distribution(features, payload)
                probabilities = torch.exp(log_probabilities)
                selected = int(torch.multinomial(probabilities, 1, generator=generator).item())
            return selected, probabilities.detach().cpu().numpy(), 0.0

    runtime.PolicyAdapter = Issue85PolicyAdapter

    def load_bound_runner(config: Mapping[str, Any]) -> Any:
        runner = original_loader(config)
        controller_type = getattr(runner, "StudyController", None)
        if controller_type is None:
            raise RuntimeError("locked Issue 73 runner has no StudyController")
        if getattr(controller_type, "_issue85_context_installed", False):
            return runner
        original_select_action = controller_type.select_action

        def select_action(controller: Any, adapter: Any, snapshot: Any, actions: Any, context: Any, step_index: int):
            original_model = controller.model
            actor = _issue85_actor_from_model(original_model, arm_type)
            if actor is None:
                return original_select_action(controller, adapter, snapshot, actions, context, step_index)
            arm = str(actor.arm).upper()
            if not isinstance(controller.trace, ContextTrace):
                controller.trace = ContextTrace(controller.trace)
            legacy_encoder = controller.encoder
            wide_encoder = getattr(controller, "_issue85_wide_encoder", None)
            if arm in {"B", "D"} and wide_encoder is None:
                wide_encoder = type(legacy_encoder)(WIDE_HASH_DIMENSION)
                controller._issue85_wide_encoder = wide_encoder
            proxy = ContextualEncoder(
                legacy_encoder,
                arm=arm,
                adapter=adapter,
                snapshot=snapshot,
                wide_encoder=wide_encoder,
                payload_builder=payload_builder,
            )
            token = _CURRENT_DECISION.set(None)
            controller.encoder = proxy
            # The locked StudyController calls ``self.model.choose`` directly.
            # Its model may be an adapter or the arm object itself, so normalize
            # both shapes only for this selection and then restore the caller's object.
            if not isinstance(original_model, Issue85PolicyAdapter) or original_model.policy is not actor:
                controller.model = Issue85PolicyAdapter(actor)
            try:
                return original_select_action(controller, adapter, snapshot, actions, context, step_index)
            finally:
                controller.encoder = legacy_encoder
                controller.model = original_model
                _CURRENT_DECISION.reset(token)

        controller_type.select_action = select_action
        controller_type._issue85_context_installed = True
        return runner

    runtime.load_issue73_runner = load_bound_runner

    def decision_from_trace(row: Mapping[str, Any], index: int, *, is_root: bool):
        base = original_decision_from_trace(row, index, is_root=is_root)
        if "issue85_arm" not in row:
            return base
        return _decision_with_issue85_inputs(base, row)

    executor._decision_from_trace = decision_from_trace

    def decision_rows(runner: Any, result: dict[str, Any], *, is_root_run: bool):
        # The pinned #75 runtime builds Decision rows directly here; it does not
        # call executor._decision_from_trace. Snapshot only the trace-row
        # references before the original function pops/clears its volatile roots.
        trace = result.get("decisions", ())
        roots = result.get("_volatile_roots")
        ordered_trace_rows: list[Mapping[str, Any]] = []
        if roots is not None:
            indices = [int(root["decision_index"]) for root in roots]
            if len(indices) != len(set(indices)):
                raise ValueError("runtime roots contain duplicate Issue 85 trace indices")
            for index in sorted(indices):
                if not 0 <= index < len(trace):
                    raise ValueError("runtime root does not map to an Issue 85 trace row")
                ordered_trace_rows.append(trace[index])

        decisions = original_decision_rows(runner, result, is_root_run=is_root_run)
        if not any("issue85_arm" in row for row in trace):
            return decisions
        if len(decisions) != len(ordered_trace_rows):
            raise ValueError("Issue 85 runtime decisions do not align with captured native roots")
        updated = []
        for decision, row in zip(decisions, ordered_trace_rows, strict=True):
            if "issue85_arm" not in row:
                raise ValueError("Issue 85 runtime root maps to an unenriched categorical trace row")
            if int(getattr(decision, "chosen_index")) != int(row["chosen_index"]):
                raise ValueError("Issue 85 training decision choice differs from its native trace row")
            updated.append(_decision_with_issue85_inputs(decision, row))
        return tuple(updated)

    runtime._decision_rows = decision_rows

    def hash_only_decisions(decisions: Any) -> list[dict[str, Any]]:
        packed = original_hash_only_decisions(decisions)
        for output, decision in zip(packed, decisions, strict=True):
            if not isinstance(decision, Issue85Decision):
                continue
            if decision.issue85_payload is not None:
                output["issue85_payload"] = _pack_payload(decision.issue85_payload)
            wide = decision.wide_candidate_features
            if wide is not None:
                output["issue85_wide_features"] = _pack_matrix(wide)
            output["issue85_legacy_sha256"] = decision.legacy_sha256
        return packed

    runtime._hash_only_decisions = hash_only_decisions

    def decode_trial(payload: Mapping[str, Any]):
        trial = original_decode_trial(payload)
        if not trial.complete:
            return trial
        source_rows = payload.get("decisions", ())
        if len(source_rows) != len(trial.decisions):
            raise ValueError("stored Issue 85 decisions do not match the source trace count")
        updated = []
        changed = False
        for source, decision in zip(source_rows, trial.decisions, strict=True):
            packed_payload = source.get("issue85_payload")
            packed_wide = source.get("issue85_wide_features")
            if packed_payload is None and packed_wide is None:
                updated.append(decision)
                continue
            semantic_payload = None if packed_payload is None else _unpack_payload(packed_payload)
            wide = None if packed_wide is None else _unpack_matrix(packed_wide)
            legacy, raw = _matrix_bytes(decision.candidate_features)
            digest = _sha256(raw)
            if source.get("issue85_legacy_sha256") != digest:
                raise ValueError("stored Issue 85 context no longer matches the locked legacy matrix")
            if wide is not None:
                folded = wide.reshape(len(wide), 4, HASH_DIMENSION).sum(axis=1)
                if wide.shape[0] != legacy.shape[0] or not np.allclose(folded, legacy, rtol=1e-6, atol=1e-6):
                    raise ValueError("stored Issue 85 widened matrix does not fold to legacy features")
            updated.append(Issue85Decision(
                decision,
                payload=semantic_payload,
                wide_candidate_features=wide,
                legacy_sha256=digest,
            ))
            changed = True
        return replace(trial, decisions=tuple(updated)) if changed else trial

    executor._decode_trial = decode_trial

    def owner_loss(actor: Any, example: Any, *, arm: str, block_examples: Any, **kwargs: Any):
        if not _is_issue85_arm(actor, arm_type):
            return original_owner_loss(actor, example, arm=arm, block_examples=block_examples, **kwargs)
        import torch

        if str(arm).upper() != "B":
            raise ValueError("Issue 85 B–F must use the frozen #84 B LENGTH_ONLY objective")
        gradients._check_block(block_examples)
        matched = [i for i, row in enumerate(block_examples) if int(row.owner_seed) == int(example.owner_seed)]
        if not example.complete or len(matched) != 1 or float(block_examples[matched[0]].reward) != float(example.reward):
            raise ValueError("Issue 85 owner outcome is not a complete matching member of its frozen block")
        if not example.decisions:
            zero = next(actor.parameters()).sum() * 0.0
            return zero, {"policy": 0.0, "entropy": 0.0, "kl": 0.0, "decision_count": 0.0}

        rewards = torch.tensor([float(row.reward) for row in block_examples], dtype=torch.float64)
        mean = float(rewards.mean().item())
        scale = max(float(rewards.std(unbiased=False).item()), 1.0)
        advantage = max(-3.0, min(3.0, (float(example.reward) - mean) / scale))
        logps = []
        entropies = []
        for decision in example.decisions:
            legacy = gradients._feature_matrix(decision)
            if actor.widened:
                if not isinstance(decision, Issue85Decision) or decision.wide_candidate_features is None:
                    raise ValueError("widened Issue 85 training decision is missing its source-bound 8192 matrix")
                features = torch.as_tensor(decision.wide_candidate_features, dtype=torch.float32)
            else:
                features = legacy
            if not isinstance(decision, Issue85Decision):
                raise ValueError("Issue 85 training decision lost its native semantic payload")
            _logits, probabilities, entropy = actor.distribution(features, decision.issue85_payload or {})
            logps.append(probabilities[int(decision.chosen_index)])
            entropies.append(entropy)
        score_sum = torch.stack(logps).sum()
        policy_loss = -advantage * score_sum / float(gradients.T_REF)
        entropy_loss = -float(gradients.ENTROPY_COEFFICIENT) * torch.stack(entropies).mean()
        total = policy_loss + entropy_loss
        return total, {
            "policy": float(policy_loss.detach().item()),
            "entropy": float(entropy_loss.detach().item()),
            "kl": 0.0,
            "decision_count": float(len(logps)),
        }

    gradients.owner_loss = owner_loss
    report = {
        "schema_id": "issue85-runtime-integration-v1",
        "pinned_upstream_source_modified": False,
        "legacy_input_dimension": HASH_DIMENSION,
        "widened_input_dimension": WIDE_HASH_DIMENSION,
        "training_objective": "issue84-B-LENGTH_ONLY",
        "sampling": "locked-policy-generator-torch-multinomial",
    }
    runtime._issue85_runtime_install = dict(report)
    return report
