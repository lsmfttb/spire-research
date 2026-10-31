from __future__ import annotations

from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

import torch
from torch import nn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gradients import OwnerOutcome  # noqa: E402
from training import train_actor_block  # noqa: E402


class ToyActor(nn.Module):
    def __init__(self):
        super().__init__()
        self.weight = nn.Parameter(torch.tensor(0.4, dtype=torch.float64))
        self.bias = nn.Parameter(torch.tensor(-0.1, dtype=torch.float64))

    def distribution(self, features):
        logits = self.weight * features[:, 0].double() + self.bias
        logp = torch.log_softmax(logits, dim=0)
        p = logp.exp()
        return logits, logp, -(p * logp).sum()


def pilot_owner(seed: int) -> OwnerOutcome:
    features = torch.zeros((2, 2048), dtype=torch.float32)
    features[0, 0] = seed % 7 / 7.0
    features[1, 0] = 1.0
    row = SimpleNamespace(
        candidate_features=features,
        legal_identities=({"occurrence": 0}, {"occurrence": 1}),
        chosen_index=seed % 2,
    )
    return OwnerOutcome(seed, bool(seed % 13 == 0), float(seed % 20), [row])


class TrainingTests(unittest.TestCase):
    def test_pilot_uses_64_updates_and_advances_inherited_adamw_64_to_128(self):
        actor = ToyActor()
        optimizer = torch.optim.AdamW(actor.parameters(), lr=3e-4, weight_decay=0.0)
        for _ in range(64):
            optimizer.zero_grad(set_to_none=True)
            (actor.weight.square() + actor.bias.square()).backward()
            optimizer.step()
        callbacks = []
        result = train_actor_block(
            actor, optimizer, [pilot_owner(seed) for seed in range(8480001, 8480033)],
            arm="A", block_index=0, block_order_seed=77, pilot=True,
            step_callback=lambda step, *_: callbacks.append(step),
        )
        self.assertEqual(result["completed_steps"], 64)
        self.assertEqual(result["optimizer_parameter_steps"], [128, 128])
        self.assertEqual(callbacks, list(range(1, 65)))
        self.assertTrue(all(math_isfinite(row["total_loss"]) for row in result["diagnostics"]))


def math_isfinite(value: float) -> bool:
    return bool(torch.isfinite(torch.tensor(value)))


if __name__ == "__main__":
    unittest.main()
