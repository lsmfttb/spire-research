"""Pinned Issue 47 control composition with Issue 60 public-choice policies."""
from __future__ import annotations

import hashlib
import json
import math
import random
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import torch

from adapter_v47 import (
    ChoiceSurfaceError, PreparedPolicyInput, UnsupportedChoiceSurface,
    call_policy, prepare_policy_input, resolve_native_action,
)
from bootstrap_v47 import Scorer, with_public_history
from encoder import Vocabulary, encode
from model import JointChoiceActorCritic


def canonical_sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")).hexdigest()


def load_bootstrap(path: str | Path) -> tuple[Scorer, dict[str, Any]]:
    checkpoint = json.loads(Path(path).read_text(encoding="utf-8"))
    claimed = checkpoint.get("sha256")
    verified = canonical_sha({k: v for k, v in checkpoint.items() if k != "sha256"})
    if claimed != verified or claimed != "0ecb46dd7a39d2aa33a12d50c60be15d438a1c9cb3d9bc893183b9143d55f555":
        raise ValueError("Issue 47 bootstrap checkpoint internal SHA-256 mismatch")
    if checkpoint.get("purpose") != "STUDY_ONLY" or checkpoint.get("cell") != "A":
        raise ValueError("Issue 47 checkpoint is not the authorized STUDY_ONLY A artifact")
    if checkpoint.get("parameters") != 2081:
        raise ValueError("Issue 47 A checkpoint has an unexpected parameter count")
    model = Scorer(checkpoint["seed"])
    state = checkpoint["state"]
    model.w = state["w"]
    model.b = state["b"]
    model.v = state["v"]
    model.c = state["c"]
    return model, checkpoint


def zero_residual_model(vocabulary: Vocabulary, seed: int) -> JointChoiceActorCritic:
    torch.manual_seed(seed)
    model = JointChoiceActorCritic(vocabulary)
    if model.parameter_count() > 200_000:
        raise ValueError(f"typed encoder exceeded its 200000 parameter cap: {model.parameter_count()}")
    return model


def p0_distribution(bootstrap: Scorer, serialized: str, count: int) -> tuple[int, list[float]]:
    if count < 1:
        raise ValueError("supported choice surface has no legal candidates")
    base = bootstrap.choose(serialized, "visible")
    uniform = 1.0 / count
    result = [0.1 * uniform for _ in range(count)]
    result[base] += 0.9
    return base, result


def policy_distribution(base: Sequence[float], residual_logits: Sequence[float]) -> list[float]:
    if len(base) != len(residual_logits) or not base:
        raise ValueError("base probabilities and residual scores do not align")
    if any(not math.isfinite(value) for value in residual_logits):
        raise ValueError("residual actor emitted a non-finite score")
    if all(value == 0.0 for value in residual_logits):
        # Besides being the exact mathematical identity, this avoids a second
        # exp/log normalization introducing round-off at frozen initialization.
        return [float(value) for value in base]
    log_weights = [math.log(probability) + score for probability, score in zip(base, residual_logits, strict=True)]
    maximum = max(log_weights)
    weights = [math.exp(value - maximum) for value in log_weights]
    total = math.fsum(weights)
    return [weight / total for weight in weights]


def sample_index(probabilities: Sequence[float], rng: random.Random) -> int:
    draw = rng.random()  # consume a stream value even for a one-action state
    cumulative = 0.0
    for index, probability in enumerate(probabilities):
        cumulative += probability
        if draw < cumulative or index == len(probabilities) - 1:
            return index
    raise AssertionError("unreachable categorical sampler state")


def _current_floor(context: Any) -> int | None:
    public = getattr(context, "public_run_context", {})
    try:
        value = public["current"]["location"]["floor"]
    except (KeyError, TypeError):
        return None
    if isinstance(value, Mapping):
        if value.get("availability") != "available":
            return None
        value = value.get("value")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class PublicChoicePolicy:
    """Normal-public MAP/REWARDS policy; executor mapping is private and fresh."""

    name = "issue60_joint_public_choice"

    def __init__(self, *, vocabulary: Vocabulary, bootstrap: Scorer,
                 model: JointChoiceActorCritic | None, mode: str,
                 rng_seed: int, block_seed: int | None,
                 fallback: Any, decision_sink: Callable[[Mapping[str, Any]], None] | None = None,
                 require_targets: bool = False, provenance_config: Mapping[str, Any] | None = None):
        if mode not in {"residual", "p0", "A"}:
            raise ValueError(f"unsupported public choice mode: {mode}")
        self.vocabulary = vocabulary
        self.bootstrap = bootstrap
        self.model = model
        self.mode = mode
        self.rng_seed = rng_seed
        self.block_seed = block_seed
        self.rng = random.Random(rng_seed)
        self.fallback = fallback
        self.decision_sink = decision_sink
        self.require_targets = require_targets
        self.counts: Counter[str] = Counter()
        self.provenance_config = dict(provenance_config or {})
        self.provenance_config.update({
            "mode": mode,
            "supported_screens": ["MAP_SCREEN", "REWARDS"],
            "unsupported_screen_fallback": "expert_non_combat_v1",
            "bootstrap_internal_sha256": "0ecb46dd7a39d2aa33a12d50c60be15d438a1c9cb3d9bc893183b9143d55f555",
            "residual_model_seed": block_seed,
            "runtime_policy_rng_seed_sha256": f"{rng_seed:064x}"[-64:],
        })
        from sts_combat_rl.sim.controller_contract import ControllerProvenance
        self.provenance = ControllerProvenance(
            kind="decision_policy", name=self.name, config=self.provenance_config
        )

    def reset_for_run(self, seed: int | None) -> None:
        # The actor stream is derived from the declared policy/game identity;
        # the simulator seed is passed only to the explicit expert fallback.
        self.rng = random.Random(self.rng_seed)
        reset = getattr(self.fallback, "reset_for_run", None)
        if callable(reset):
            reset(seed)
        self.counts.clear()

    @staticmethod
    def _native_identity(action: Any) -> tuple[Any, ...]:
        return tuple(getattr(action, name) for name in ("scope", "kind", "idx1", "idx2", "idx3"))

    def select_action(self, adapter: Any, snapshot: Any, actions: Sequence[Any],
                      context: Any, step_index: int) -> Any:
        screen = getattr(context, "screen_state", None)
        if screen not in {"MAP_SCREEN", "REWARDS"}:
            self.counts["unsupported_expert_fallback"] += 1
            decision = self.fallback.select_action(adapter, snapshot, actions, context, step_index)
            return decision

        # The callback receives only this public projection-derived string.
        projection = dict(adapter._sim.public_projection_v3())
        native_actions = list(adapter._sim.legal_actions())
        prepared: PreparedPolicyInput = prepare_policy_input(projection, native_actions)
        history = getattr(context, "public_run_context", {}).get("history", [])
        enriched = with_public_history(
            prepared.serialized_input,
            history,
            getattr(context, "public_run_context", None),
        )
        choice_count = len(prepared._native_actions)
        # The callable receives only the serialized public JSON. Native
        # handles, indices, labels, bit fields, and simulator state remain in
        # this mapping layer and never enter the actor/critic code path.
        public_prepared = PreparedPolicyInput(
            enriched, prepared._native_actions, prepared._candidate_fingerprints
        )
        decision = call_policy(
            public_prepared,
            lambda serialized: self._select_from_public(serialized, choice_count),
        )
        bootstrap_choice = decision["bootstrap_index"]
        p0 = decision["base_probabilities"]
        probabilities = decision["behavior_probabilities"]
        residual_scores = decision["residual_scores"]
        value_prediction = decision["value_prediction"]
        selected = decision["selected_index"]
        behavior = decision["behavior"]

        # Re-read the same native boundary immediately before the action returns.
        # This rejects reused card/potion slots even when their integer indices
        # happen to be the same after a different public offer.
        fresh_projection = dict(adapter._sim.public_projection_v3())
        fresh_actions = list(adapter._sim.legal_actions())
        native_selected = resolve_native_action(prepared, selected, fresh_projection, fresh_actions)
        wrapped_selected = actions[selected].raw.get("native")
        if wrapped_selected is None or self._native_identity(native_selected) != self._native_identity(wrapped_selected):
            raise ChoiceSurfaceError("fresh public candidate mapping does not resolve to the executor action")

        current_floor = _current_floor(context)
        if self.require_targets and current_floor is None:
            raise ChoiceSurfaceError("supported training decision has no available public current floor")
        selected_probability = probabilities[selected]
        meta = {
            "issue60_supported_choice": True,
            "issue60_mode": self.mode,
            "issue60_behavior": behavior,
            "issue60_public_input": enriched,
            "issue60_public_input_sha256": hashlib.sha256(enriched.encode("utf-8")).hexdigest(),
            "issue60_selected_index": selected,
            "issue60_bootstrap_index": bootstrap_choice,
            "issue60_bootstrap_shift": selected != bootstrap_choice,
            "issue60_choice_count": choice_count,
            "issue60_base_probabilities": p0,
            "issue60_behavior_probabilities": probabilities,
            "issue60_residual_scores": residual_scores,
            "issue60_old_log_probability": math.log(selected_probability),
            "issue60_value_prediction": value_prediction,
            "issue60_current_floor": current_floor,
            "issue60_actor_update_mask": 0 if choice_count == 1 else 1,
            "issue60_native_mapping_fresh": True,
        }
        self.counts["supported_choices"] += 1
        self.counts["bootstrap_shifts"] += int(selected != bootstrap_choice)
        if self.decision_sink is not None:
            self.decision_sink(meta)
        from sts_combat_rl.sim.controller_contract import ControllerDecision
        return ControllerDecision(
            selected_index=selected,
            provenance=self.provenance,
            reason=f"issue60_{self.mode}",
            score=selected_probability,
            metadata=meta,
        )

    def _select_from_public(self, serialized_public: str, choice_count: int) -> dict[str, Any]:
        """Choose from one serialized public observation, with no native API."""
        bootstrap_choice, p0 = p0_distribution(
            self.bootstrap, serialized_public, choice_count
        )
        value_prediction: float | None = None
        if self.mode == "A":
            selected = bootstrap_choice
            probabilities = [
                1.0 if index == selected else 0.0
                for index in range(choice_count)
            ]
            residual_scores = [0.0] * choice_count
            behavior = "deterministic_A"
        else:
            if self.model is None:
                residual_scores = [0.0] * choice_count
            else:
                observation = encode(serialized_public, self.vocabulary)
                with torch.no_grad():
                    logits, value = self.model(observation)
                    residual_scores = [float(x) for x in logits.detach().cpu().tolist()]
                    value_prediction = float(value.detach().cpu())
                if len(observation.candidates) != choice_count:
                    raise ValueError(
                        "public encoder candidate count disagrees with native mapping"
                    )
            probabilities = policy_distribution(p0, residual_scores)
            selected = sample_index(probabilities, self.rng)
            behavior = "sampled_policy"
        return {
            "selected_index": selected,
            "bootstrap_index": bootstrap_choice,
            "base_probabilities": p0,
            "behavior_probabilities": probabilities,
            "residual_scores": residual_scores,
            "value_prediction": value_prediction,
            "behavior": behavior,
        }


class FrozenBattleSearch:
    """Issue 47 unguided full-state Search-v2@400 controller."""

    def __init__(self, event_sink: Callable[[Mapping[str, Any]], None] | None = None):
        from sts_combat_rl.sim.controller_contract import ControllerProvenance
        self.cpu_seconds = 0.0
        self.calls = 0
        self.event_sink = event_sink
        self.provenance = ControllerProvenance(
            kind="native_search_v2", name="issue60_frozen_search_v2_400", config={
                "native_api": "StepSimulator.battle_search_v2.v1",
                "patch_identity": "sts_lightspeed_battle_search_v2_tree_internal_v1",
                "simulations": 400, "include_potions": True,
                "root_selection_rule": "highest_mean",
                "policy_prior_callback": None, "leaf_value_callback": None,
                "information_regime": "full_simulator_state_oracle_like",
            },
        )

    def select_action(self, adapter: Any, snapshot: Any, actions: Sequence[Any],
                      context: Any, step_index: int) -> Any:
        from sts_combat_rl.sim.controller_contract import ControllerDecision
        from sts_combat_rl.sim.oracle_search import (
            build_oracle_search_report, select_oracle_root_action,
            oracle_search_controller_metadata,
        )
        del step_index
        start = time.process_time()
        raw = adapter.battle_search_v2(
            snapshot,
            simulations=400,
            include_potions=True,
            policy_prior_callback=None,
            leaf_value_callback=None,
        )
        cpu = time.process_time() - start
        self.cpu_seconds += cpu
        self.calls += 1
        if self.event_sink is not None:
            self.event_sink({"type": "battle_search", "cpu_s": cpu, "calls": self.calls})
        report = build_oracle_search_report(
            raw, actions, context,
            expected_native_api="StepSimulator.battle_search_v2.v1",
            expected_patch_identity="sts_lightspeed_battle_search_v2_tree_internal_v1",
        )
        if not report.search_ok or not report.include_potions:
            raise ValueError("NATIVE_SEARCH_INVALID: " + repr(report.problems))
        target = select_oracle_root_action(report, selection_rule="highest_mean")
        metadata = oracle_search_controller_metadata(report, target)
        metadata["issue60_search_cpu_s"] = cpu
        return ControllerDecision(
            selected_index=target.legal_action_index,
            provenance=self.provenance,
            reason="highest_mean",
            score=target.score,
            metadata=metadata,
        )


def routed_controller(*, noncombat: Any, event_sink: Callable[[Mapping[str, Any]], None] | None = None) -> tuple[Any, FrozenBattleSearch]:
    from sts_combat_rl.sim.online_controller import RoutedRunController
    battle = FrozenBattleSearch(event_sink)
    return RoutedRunController(battle=battle, non_combat=noncombat), battle
