"""Synthetic witnesses required before any Issue 60 simulator preflight."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any

import torch
from torch import nn

from adapter_v47 import StaleChoiceMapping
from bootstrap_v47 import Scorer
from controllers import (
    load_bootstrap, policy_distribution, sample_index, zero_residual_model,
)
from encoder import Vocabulary, encode
from model import JointChoiceActorCritic
from normalizer_v49 import mapped_action, normalize
from public_resources import RESOURCE_FIELDS, boundary_resources
from trainer import (
    DecisionRow, IncompleteRun, ppo_minibatch_loss, terminal_target,
)


def _available(value: Any) -> dict[str, Any]:
    return {"availability": "available", "value": value}


def _action(scope: str, kind: str, idx1: int, idx2: int = 0,
            idx3: int = 0) -> dict[str, Any]:
    return {
        "scope": scope, "kind": kind, "idx1": idx1, "idx2": idx2,
        "idx3": idx3, "label": f"{scope}.{kind} candidate",
    }


def public_fixture() -> dict[str, Any]:
    card_a = {
        "id_label": "STRIKE_RED", "type": "ATTACK", "rarity": "BASIC",
        "upgraded": False, "cost": 1, "damage": 6, "block": 0,
    }
    card_b = {
        "id_label": "DEFEND_RED", "type": "SKILL", "rarity": "BASIC",
        "upgraded": False, "cost": 1, "damage": 0, "block": 5,
    }
    actions = [
        {
            "action": _action("reward", "reward_card", 0, 0),
            "semantics": {"choice_type": "card", "card": copy.deepcopy(card_a)},
        },
        {
            "action": _action("reward", "reward_card", 0, 1),
            "semantics": {"choice_type": "card", "card": copy.deepcopy(card_b)},
        },
        {
            "action": _action("reward", "reward_card", 1, 0),
            "semantics": {"choice_type": "singing_bowl_max_hp", "max_hp_gain": 2},
        },
        {
            "action": _action("game", "game_potion_use", 0),
            "semantics": {
                "choice_type": "potion_use",
                "potion": {
                    "id_label": "FIRE_POTION", "name": "Fire Potion",
                    "potion_index": 0,
                },
            },
        },
    ]
    return {
        "schema_id": "issue47-normal-public-input-v3",
        "availability": {
            name: ("unavailable" if name == "screen_payload" else "available") for name in (
                "screen_identity", "visible_act_boss", "visible_map_graph",
                "current_map_node", "immediately_legal_routes",
                "persistent_resources", "screen_payload", "candidate_actions",
            )
        },
        "public_state": {
            "screen": "REWARDS",
            "visible_act_boss": "THE_HEART",
            "map_graph": [
                {"x": 1, "y": 2, "room_symbol": "E", "outgoing_edges": [{"x": 2, "y": 3}]},
                {"x": 2, "y": 3, "room_symbol": "M", "outgoing_edges": []},
            ],
            "current_map_node": {"location": "map_node", "x": 1, "y": 2},
            "screen_context": {"coverage_status": "supported"},
            "ordinary_location": {"act": _available(1), "floor": _available(8)},
            "persistent_resources": {
                "current_hp": _available(63), "max_hp": _available(70),
                "gold": _available(88), "potion_count": _available(1),
                "potion_capacity": _available(3),
                "deck": _available([copy.deepcopy(card_a), copy.deepcopy(card_a)]),
                "relics": _available([{"id_label": "BURNING_BLOOD", "counter": 0}]),
                "potion_identities": _available([{
                    "id_label": "FIRE_POTION", "potion_index": 0,
                }]),
                "keys": _available({"blue_key": False, "green_key": True, "red_key": False}),
            },
            "history": [],
        },
        "legal_options": actions,
    }


def map_fixture() -> dict[str, Any]:
    public = public_fixture()
    public["public_state"]["screen"] = "MAP_SCREEN"
    map_action = _action("game", "map", 2)
    public["legal_options"] = [{
        "action": map_action,
        "semantics": {
            "choice_type": "map_route",
            "destination": {"location": "map_node", "x": 2, "y": 3, "room_symbol": "M"},
        },
    }]
    return public


def _load_vocab(directory: Path) -> Vocabulary:
    return Vocabulary(directory / "vocabulary.json")


def witness_public_context(directory: Path) -> dict[str, Any]:
    vocab = _load_vocab(directory)
    public = public_fixture()
    observation = encode(public, vocab)
    assert len(observation.candidates) == 4
    assert len(observation.reward_groups) == 2
    assert len(observation.reward_groups[0]) == 2
    assert any(
        candidate.children and candidate.children[0].categories[0] == vocab.id("special", "singing_bowl_max_hp")
        for candidate in observation.candidates
    ), "Singing Bowl must remain a typed max-HP conflict, never a fake card"
    assert len(observation.potions) == 1 and observation.potions[0].categories
    assert len(observation.deck) == 2 and observation.deck[0] == observation.deck[1]
    assert len(observation.state_numeric) == 14 and len(observation.state_masks) == 14

    # A visible offer/entity-attribute change must alter the residual input.
    changed_offer = copy.deepcopy(public)
    changed_offer["legal_options"][0]["semantics"]["card"]["damage"] = 19
    changed_observation = encode(changed_offer, vocab)
    assert changed_observation != observation

    # Private/unknown fields within any projection branch are removed too.
    hidden_only = copy.deepcopy(public)
    hidden_only["private_future_rewards"] = ["not public"]
    hidden_only["public_state"]["future_draw_order"] = [7, 8, 9]
    hidden_only["public_state"]["persistent_resources"]["value_from_hidden_rng"] = _available(999)
    hidden_only["public_state"]["persistent_resources"]["deck"]["value"][0]["future_card"] = "SECRET"
    hidden_only["legal_options"][0]["semantics"]["card"]["hidden_score"] = 1e9
    assert encode(hidden_only, vocab) == observation

    # Availability is field-specific in the typed context. Equal counts of
    # available fields must not collapse distinct public information regimes.
    availability_variant = copy.deepcopy(public)
    availability_variant["availability"]["visible_map_graph"] = "unavailable"
    availability_variant["availability"]["screen_payload"] = "available"
    assert sum(value == "available" for value in availability_variant["availability"].values()) == sum(
        value == "available" for value in public["availability"].values()
    )
    assert encode(availability_variant, vocab) != observation

    # Entity collection multiplicity remains represented.
    duplicated = copy.deepcopy(public)
    duplicated["public_state"]["persistent_resources"]["deck"]["value"].append(
        copy.deepcopy(duplicated["public_state"]["persistent_resources"]["deck"]["value"][0])
    )
    assert len(encode(duplicated, vocab).deck) == 3

    # Both candidate and set order may change without changing scores by meaning.
    torch.manual_seed(670001)
    model = JointChoiceActorCritic(vocab)
    with torch.no_grad():
        model.actor[-1].weight.normal_(mean=0.0, std=0.01)
        model.actor[-1].bias.normal_(mean=0.0, std=0.01)
    with torch.no_grad():
        logits, _ = model(observation)
        reordered = copy.deepcopy(public)
        reordered["legal_options"] = list(reversed(reordered["legal_options"]))
        reordered["public_state"]["map_graph"] = list(reversed(reordered["public_state"]["map_graph"]))
        deck = reordered["public_state"]["persistent_resources"]["deck"]["value"]
        deck.reverse()
        other_logits, _ = model(encode(reordered, vocab))
    by_key = {c.semantic_key: float(v) for c, v in zip(observation.candidates, logits, strict=True)}
    other_by_key = {
        c.semantic_key: float(v)
        for c, v in zip(encode(reordered, vocab).candidates, other_logits, strict=True)
    }
    assert by_key.keys() == other_by_key.keys()
    assert all(math.isclose(by_key[key], other_by_key[key], rel_tol=1e-6, abs_tol=1e-7) for key in by_key)

    # The v49 mapping guard rejects a reused reward-group/potion slot when its
    # fresh public meaning has changed, despite an unchanged integer action.
    actions = [row["action"] for row in public["legal_options"]]
    assert mapped_action(public, 0, actions, current_public=public) == actions[0]
    reused = copy.deepcopy(public)
    reused["legal_options"][0]["semantics"]["card"]["id_label"] = "BASH"
    try:
        mapped_action(public, 0, actions, current_public=reused)
    except ValueError:
        pass
    else:
        raise AssertionError("stale reward-group mapping was accepted")
    reused_potion = copy.deepcopy(public)
    reused_potion["legal_options"][3]["semantics"]["potion"] = {
        "id_label": "BLOCK_POTION", "potion_index": 0,
    }
    try:
        mapped_action(public, 3, actions, current_public=reused_potion)
    except ValueError:
        pass
    else:
        raise AssertionError("stale potion-slot mapping was accepted")

    # MAP_SCREEN destination encodings include the directed visible map.
    map_obs = encode(map_fixture(), vocab)
    assert map_obs.candidates[0].destination == (2, 3)
    assert map_obs.map_nodes[0].outgoing

    return {
        "passed": True,
        "candidate_count": len(observation.candidates),
        "group_sizes": [len(group) for group in observation.reward_groups],
        "map_node_count": len(map_obs.map_nodes),
        "numeric_feature_count": len(observation.state_numeric),
        "hidden_future_noninterference": True,
        "candidate_and_set_permutation_equivariance": True,
        "duplicate_multiplicity_preserved": True,
        "stale_group_and_potion_mapping_rejected": True,
    }


def witness_on_policy_and_terminal(directory: Path) -> dict[str, Any]:
    vocab = _load_vocab(directory)
    public = public_fixture()
    serialized = json.dumps(public, sort_keys=True, separators=(",", ":"))
    bootstrap, _ = load_bootstrap(directory / "bootstrap" / "A.json")
    base_index = bootstrap.choose(serialized, "visible")
    count = len(public["legal_options"])
    base = [0.1 / count] * count
    base[base_index] += 0.9
    probs = policy_distribution(base, [0.0] * count)
    assert probs == base, "zero residual must reproduce p0 exactly"
    assert math.isclose(sum(probs), 1.0, rel_tol=0.0, abs_tol=1e-15)
    picked1 = sample_index(probs, random.Random(670010))
    picked2 = sample_index(probs, random.Random(670010))
    assert picked1 == picked2

    death_return = terminal_target(
        terminal=True, problems=(), outcome="PLAYER_LOSS",
        final_floor=11, current_floor=5,
    )
    assert death_return == 0.1
    for kwargs in (
        {"terminal": False, "problems": (), "outcome": "UNDECIDED", "final_floor": 11, "current_floor": 5},
        {"terminal": True, "problems": ("executor cap",), "outcome": "PLAYER_LOSS", "final_floor": 11, "current_floor": 5},
        {"terminal": True, "problems": (), "outcome": "UNDECIDED", "final_floor": 11, "current_floor": 5},
    ):
        try:
            terminal_target(**kwargs)
        except IncompleteRun:
            pass
        else:
            raise AssertionError("cap/error/nonterminal case incorrectly received a target")

    model = zero_residual_model(vocab, 660101)
    obs = encode(public, vocab)
    with torch.no_grad():
        initial_logits, initial_value = model(obs)
    policy = policy_distribution(base, initial_logits.tolist())
    assert policy == base
    clone = JointChoiceActorCritic(vocab)
    clone.load_state_dict(copy.deepcopy(model.state_dict()))
    with torch.no_grad():
        replay_logits, replay_value = clone(obs)
    assert torch.equal(initial_logits, replay_logits)
    assert torch.equal(initial_value, replay_value)
    assert sample_index(policy, random.Random(670011)) == sample_index(
        policy, random.Random(670011)
    )

    # Compare one frozen minibatch's PPO objective with an independent scalar
    # calculation, including the fixed sum-per-game normalization.
    class DirectLogitModel(nn.Module):
        def forward(self, observation):
            values = [0.4, -0.2, 0.1, -0.1][:len(observation.candidates)]
            return torch.tensor(values, requires_grad=True), torch.tensor(0.3, requires_grad=True)

    old_probability = 0.55
    row = DecisionRow(
        serialized_public=serialized, p0=tuple(base), action=base_index,
        old_log_probability=math.log(old_probability), old_value=0.0,
        value_target=0.4, current_floor=5.0, actor_update_mask=1.0,
        game_seed=668001,
    )
    dummy = DirectLogitModel()
    loss, metrics = ppo_minibatch_loss(dummy, [row], [0.7], vocab)
    direct_probs = policy_distribution(base, [0.4, -0.2, 0.1, -0.1])
    ratio = direct_probs[base_index] / old_probability
    clipped_ratio = min(max(ratio, 0.8), 1.2)
    direct_policy = -min(ratio * 0.7, clipped_ratio * 0.7)
    direct_value = (0.3 - 0.4) ** 2
    direct_loss = (direct_policy + 0.5 * direct_value) / 128.0
    assert math.isclose(float(loss.detach()), direct_loss, rel_tol=1e-6, abs_tol=1e-9)
    assert math.isclose(metrics["value_mse_sum"], direct_value, rel_tol=1e-6)

    # One-action states retain critic targets but explicitly mask actor updates.
    one_action_public = copy.deepcopy(public_fixture())
    one_action_public["legal_options"] = one_action_public["legal_options"][:1]
    one_action = DecisionRow(
        serialized_public=json.dumps(one_action_public, sort_keys=True, separators=(",", ":")),
        p0=(1.0,), action=0,
        old_log_probability=0.0, old_value=0.0, value_target=0.2,
        current_floor=5.0, actor_update_mask=0.0, game_seed=668001,
    )
    _, one_metrics = ppo_minibatch_loss(dummy, [one_action], [0.0], vocab)
    assert one_metrics["actor_rows"] == 0.0
    assert one_metrics["value_mse_sum"] > 0.0

    checkpoint = directory / "bootstrap" / "A.json"
    checkpoint_hash = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    return {
        "passed": True,
        "p0_exact_at_zero_residual": True,
        "death_return": death_return,
        "cap_and_error_targets_rejected": True,
        "ppo_reference_loss": direct_loss,
        "no_change_checkpoint_replay": True,
        "single_action_critic_only": True,
        "bootstrap_file_sha256": checkpoint_hash,
    }


def witness_boundary_inventory() -> dict[str, Any]:
    """Prove boundary snapshots retain public inventory, not hidden ordering."""
    resources = {
        "current_hp": _available(51), "max_hp": _available(70),
        "gold": _available(42), "potion_count": _available(2),
        "potion_capacity": _available(3),
        "deck": _available([
            {"id_label": "STRIKE_RED", "name": "Strike", "type": "ATTACK",
             "rarity": "BASIC", "upgraded": False, "upgrade_count": 0,
             "misc": 0, "deck_index": 2, "draw_order": "PRIVATE"},
            {"id_label": "STRIKE_RED", "name": "Strike", "type": "ATTACK",
             "rarity": "BASIC", "upgraded": False, "upgrade_count": 0,
             "misc": 0, "deck_index": 1, "future_rng": 1},
        ]),
        "relics": _available([
            {"id_label": "BURNING_BLOOD", "name": "Burning Blood", "counter": 0,
             "relic_index": 0, "private_payload": 1},
        ]),
        "potion_identities": _available([
            {"potion_index": 1, "id_label": "FIRE_POTION", "name": "Fire Potion",
             "private_payload": 1},
        ]),
        "keys": _available({"blue_key": False, "green_key": True, "red_key": False,
                             "hidden_flag": True}),
        "unknown_hidden_field": _available("PRIVATE"),
    }
    projection = {
        "schema_id": "native-public-projection-v3",
        "persistent_resources": _available(resources),
        "unexposed_future": [1, 2, 3],
    }
    result = boundary_resources(projection)
    assert tuple(result) == RESOURCE_FIELDS
    assert len(result["deck"]["value"]) == 2
    assert result["deck"]["value"][0] == result["deck"]["value"][1]
    assert all("deck_index" not in row and "draw_order" not in row for row in result["deck"]["value"])
    assert result["potion_identities"]["value"][0]["potion_index"] == 1
    assert "hidden_flag" not in result["keys"]["value"]
    assert "unknown_hidden_field" not in result

    reordered = copy.deepcopy(projection)
    reordered["persistent_resources"]["value"]["deck"]["value"].reverse()
    assert boundary_resources(reordered) == result
    unavailable = boundary_resources({"persistent_resources": {"availability": "unavailable"}})
    assert all(row["availability"] == "unavailable" for row in unavailable.values())
    return {
        "passed": True, "resource_field_count": len(RESOURCE_FIELDS),
        "exact_public_inventory_retained": True,
        "deck_order_not_retained": True,
        "duplicate_card_multiplicity_retained": True,
        "potion_slots_retained": True,
        "unknown_and_hidden_fields_dropped": True,
    }


def run_witnesses(directory: Path, output: Path | None = None) -> dict[str, Any]:
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    config_bytes = (directory / "config.json").read_bytes()
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    config = json.loads(config_bytes)
    if config["input_schema"]["schema_id"] != "issue60-typed-joint-choice-v1":
        raise ValueError("unexpected frozen schema")
    if config["architecture"]["trainable_parameter_cap"] != 200000:
        raise ValueError("unexpected parameter cap")
    public = witness_public_context(directory)
    inventory = witness_boundary_inventory()
    on_policy = witness_on_policy_and_terminal(directory)
    result = {
        "schema_id": "issue60-synthetic-preflight-witness-v1",
        "config_sha256": config_sha,
        "vocabulary_sha256": _load_vocab(directory).sha256,
        "parameter_count": zero_residual_model(_load_vocab(directory), 660101).parameter_count(),
        "public_context_and_execution": public,
        "boundary_inventory": inventory,
        "on_policy_and_terminal": on_policy,
        "passed": bool(public["passed"] and inventory["passed"] and on_policy["passed"]),
    }
    if result["parameter_count"] > 200000:
        raise ValueError("parameter cap exceeded")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    print(json.dumps(run_witnesses(here, here / "evidence" / "preflight" / "synthetic.json"), indent=2))
