"""Width-64 typed entity/group/map actor-critic for Issue 60."""
from __future__ import annotations

from typing import Sequence

import torch
from torch import Tensor, nn

from encoder import Candidate, Entity, Observation, Vocabulary


WIDTH = 64
EMBED = 16


def _sum(vectors: Sequence[Tensor], *, like: Tensor) -> Tensor:
    if not vectors:
        return like.new_zeros((WIDTH,))
    return torch.stack(tuple(vectors), dim=0).sum(dim=0)


class JointChoiceActorCritic(nn.Module):
    """Candidate-conditioned residual actor and public-context value head.

    Card, relic, potion, and map entities use the same categorical embedding
    table and numeric-field encoder.  Group pooling and map messages preserve
    within-entity binding and multiplicity without native indices or ordinals.
    """

    def __init__(self, vocabulary: Vocabulary):
        super().__init__()
        self.vocabulary_sha256 = vocabulary.sha256
        self._vocab_indices = vocabulary.indices
        self.category = nn.Embedding(len(vocabulary.entries), EMBED)
        self.entity = nn.Sequential(
            nn.Linear(EMBED + 16, WIDTH), nn.Tanh(),
            nn.Linear(WIDTH, WIDTH), nn.Tanh(),
        )
        self.map_node = nn.Sequential(nn.Linear(WIDTH + 2, WIDTH), nn.Tanh())
        self.map_message = nn.ModuleList(
            [nn.Linear(WIDTH * 2, WIDTH), nn.Linear(WIDTH * 2, WIDTH)]
        )
        self.choice_combine = nn.Sequential(nn.Linear(WIDTH * 2, WIDTH), nn.Tanh())
        self.global_context = nn.Sequential(
            nn.Linear(WIDTH * 7 + EMBED + 28, WIDTH), nn.Tanh(),
            nn.Linear(WIDTH, WIDTH), nn.Tanh(),
        )
        self.actor = nn.Sequential(
            nn.Linear(WIDTH * 5, WIDTH), nn.Tanh(), nn.Linear(WIDTH, 1)
        )
        self.value = nn.Linear(WIDTH, 1)
        # Exact p0 behavior at initialization is part of the protocol.
        nn.init.zeros_(self.actor[-1].weight)
        nn.init.zeros_(self.actor[-1].bias)

    def parameter_count(self) -> int:
        return sum(parameter.numel() for parameter in self.parameters() if parameter.requires_grad)

    def _category_sum(self, ids: Sequence[int], *, like: Tensor) -> Tensor:
        if not ids:
            return like.new_zeros((EMBED,))
        index = torch.tensor(tuple(ids), dtype=torch.long, device=like.device)
        return self.category(index).sum(dim=0)

    def _entity_vector(self, entity: Entity, *, like: Tensor) -> Tensor:
        categorical = self._category_sum(entity.categories, like=like)
        numeric = torch.tensor(entity.numeric + entity.masks, dtype=like.dtype, device=like.device)
        return self.entity(torch.cat((categorical, numeric), dim=0))

    def _entity_pool(self, entities: Sequence[Entity], *, like: Tensor) -> Tensor:
        return _sum([self._entity_vector(value, like=like) for value in entities], like=like)

    def _map_vectors(self, observation: Observation, *, like: Tensor) -> tuple[Tensor, Tensor, dict[tuple[int, int], Tensor]]:
        nodes = observation.map_nodes
        if not nodes:
            zero = like.new_zeros((WIDTH,))
            return zero, zero, {}
        coordinates = {node.coordinate: index for index, node in enumerate(nodes)}
        initial: list[Tensor] = []
        for node in nodes:
            entity = self._entity_vector(node.entity, like=like)
            coordinate = like.new_tensor([node.x, node.y])
            initial.append(self.map_node(torch.cat((entity, coordinate), dim=0)))
        hidden = torch.stack(initial, dim=0)
        for layer in self.map_message:
            aggregate = torch.zeros_like(hidden)
            for source, node in enumerate(nodes):
                for target_coord in node.outgoing:
                    target = coordinates.get(target_coord)
                    if target is not None:
                        aggregate[source] = aggregate[source] + hidden[target]
            hidden = torch.tanh(layer(torch.cat((hidden, aggregate), dim=1)))
        by_coordinate = {node.coordinate: hidden[index] for index, node in enumerate(nodes)}
        pooled = hidden.sum(dim=0)
        current = by_coordinate.get(observation.current_coordinate, like.new_zeros((WIDTH,)))
        return pooled, current, by_coordinate

    def _candidate_vectors(self, candidates: Sequence[Candidate], *, like: Tensor) -> list[Tensor]:
        vectors: list[Tensor] = []
        for candidate in candidates:
            main = self._entity_vector(candidate.entity, like=like)
            children = self._entity_pool(candidate.children, like=like)
            vectors.append(self.choice_combine(torch.cat((main, children), dim=0)))
        return vectors

    def forward(self, observation: Observation) -> tuple[Tensor, Tensor]:
        """Return residual logits in legal-option order and public value V(I)."""
        like = self.category.weight
        deck = self._entity_pool(observation.deck, like=like)
        relics = self._entity_pool(observation.relics, like=like)
        potions = self._entity_pool(observation.potions, like=like)
        groups = [self._entity_pool(group, like=like) for group in observation.reward_groups]
        group_by_key = {
            key: groups[index] for index, key in enumerate(observation.group_keys)
            if index < len(groups)
        }
        # Rebuild content-hash lookup from candidate alternatives to avoid any
        # dependence on native reward-group or card-slot ordinals.
        from normalizer_v49 import key as content_key
        group_by_candidate_key: dict[str, Tensor] = {}
        for candidate in observation.candidates:
            if candidate.group_key is None or candidate.group_key in group_by_candidate_key:
                continue
            alternatives = candidate.semantic_key
            # The encoder's canonical facts include the matched group members;
            # group order in v49 is canonical.  Find the group via its digest.
            for raw_group, group_vector in zip(
                observation.public_facts.get("card_reward_groups", ()), groups, strict=True
            ):
                if content_key("card-group", raw_group) == candidate.group_key:
                    group_by_candidate_key[candidate.group_key] = group_vector
                    break
        candidate_vectors = self._candidate_vectors(observation.candidates, like=like)
        choice_pool = _sum(candidate_vectors, like=like)
        map_pool, current_node, map_by_coordinate = self._map_vectors(observation, like=like)
        state_categories = self._category_sum(observation.state_categories, like=like)
        state_numeric = torch.tensor(
            observation.state_numeric + observation.state_masks,
            dtype=like.dtype,
            device=like.device,
        )
        global_vector = self.global_context(torch.cat((
            deck, relics, potions, _sum(groups, like=like), map_pool, current_node,
            choice_pool, state_categories, state_numeric,
        ), dim=0))
        scores: list[Tensor] = []
        for index, candidate in enumerate(observation.candidates):
            group = group_by_candidate_key.get(candidate.group_key, like.new_zeros((WIDTH,)))
            other_choices = choice_pool - candidate_vectors[index]
            if candidate.destination is not None:
                destination = map_by_coordinate.get(candidate.destination, like.new_zeros((WIDTH,)))
            elif candidate.boss_entry:
                destination = self._category_sum(
                    (self.vocabulary_index("boss", observation.public_facts["public_state"].get("visible_act_boss")),),
                    like=like,
                )
                # Category features have width 16; lift through a zero-padded
                # vector to keep the fixed candidate tensor contract.
                destination = torch.nn.functional.pad(destination, (0, WIDTH - EMBED))
            else:
                destination = like.new_zeros((WIDTH,))
            score = self.actor(torch.cat((
                global_vector, candidate_vectors[index], group, other_choices, destination,
            ), dim=0)).reshape(())
            scores.append(score)
        return torch.stack(scores), self.value(global_vector).reshape(())

    def vocabulary_index(self, namespace: str, value: object) -> int:
        # Kept only for the explicit boss-entry feature; regular observation
        # category IDs are precomputed before policy invocation.
        return self._vocab_indices.get(f"{namespace}:{value}", 0)

    def bind_vocabulary(self, vocabulary: Vocabulary) -> None:
        self._vocab_indices = vocabulary.indices
