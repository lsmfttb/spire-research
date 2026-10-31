from __future__ import annotations

from pathlib import Path
import tempfile
import sys
import unittest

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gradients import OwnerOutcome, ScalarPredictor  # noqa: E402
from predictors import (  # noqa: E402
    build_predictor_cache,
    fit_cached_prior_block_predictors,
    fit_prior_block_predictors,
    new_predictor_optimizer,
)


def owner(seed: int, reward: int) -> OwnerOutcome:
    matrix = torch.zeros((2, 2048), dtype=torch.float32)
    matrix[0, 0] = 0.25
    matrix[1, 0] = 1.0
    decision = type("Decision", (), {
        "candidate_features": matrix,
        "legal_identities": ({"occurrence": 0}, {"occurrence": 1}),
        "chosen_index": seed % 2,
    })()
    return OwnerOutcome(seed, False, reward, [decision])


class PredictorTests(unittest.TestCase):
    def test_zero_output_initialization_is_deterministic_and_preserves_global_rng(self):
        torch.manual_seed(17)
        before = torch.random.get_rng_state().clone()
        first = ScalarPredictor(seed=42)
        after = torch.random.get_rng_state()
        second = ScalarPredictor(seed=42)
        self.assertTrue(torch.equal(before, after))
        self.assertTrue(all(torch.equal(first.state_dict()[k], second.state_dict()[k]) for k in first.state_dict()))
        self.assertEqual(float(first(torch.ones((3, 2048))).detach().abs().max()), 0.0)

    def test_prior_block_fit_runs_for_v_and_q(self):
        rows = [owner(100, 5), owner(101, 20)]
        value = ScalarPredictor(seed=1)
        value_opt = new_predictor_optimizer(value)
        q = ScalarPredictor(seed=2)
        q_opt = new_predictor_optimizer(q)
        receipt = fit_prior_block_predictors(
            arm="E", current_block_index=1, prior_blocks=[(0, rows)],
            value_model=value, value_optimizer=value_opt,
            action_value_model=q, action_value_optimizer=q_opt,
        )
        self.assertEqual(receipt["prior_block_indices"], [0])
        self.assertEqual(receipt["value"]["examples"], 2)
        self.assertEqual(receipt["action_value"]["examples"], 2)
        self.assertEqual(receipt["value"]["steps"], 1)
        self.assertEqual(receipt["action_value"]["steps"], 1)
        self.assertEqual(value.training, False)
        self.assertEqual(q.training, False)

    def test_current_block_data_is_rejected(self):
        value = ScalarPredictor(seed=1)
        with self.assertRaisesRegex(ValueError, "current/future"):
            fit_prior_block_predictors(
                arm="D", current_block_index=1, prior_blocks=[(1, [owner(101, 20)])],
                value_model=value, value_optimizer=new_predictor_optimizer(value),
            )

    def test_non_e_arm_cannot_silently_receive_action_predictor(self):
        value = ScalarPredictor(seed=1)
        q = ScalarPredictor(seed=2)
        with self.assertRaisesRegex(ValueError, "only arm E"):
            fit_prior_block_predictors(
                arm="D", current_block_index=1, prior_blocks=[(0, [owner(100, 5)])],
                value_model=value, value_optimizer=new_predictor_optimizer(value),
                action_value_model=q, action_value_optimizer=new_predictor_optimizer(q),
            )

    def test_disk_backed_fit_matches_in_memory_updates_and_rejects_cache_changes(self):
        source = [owner(100, 5), owner(101, 20), owner(102, 12)]
        # Cross the registered minibatch boundary while preserving multiple
        # owner labels and repeated decisions from the same genuine outcome.
        source = [OwnerOutcome(item.owner_seed, item.heart_win, item.terminal_floor,
                               item.decisions * 100, item.complete) for item in source]
        value_memory = ScalarPredictor(seed=71)
        value_disk = ScalarPredictor(seed=71)
        optimizer_memory = new_predictor_optimizer(value_memory)
        optimizer_disk = new_predictor_optimizer(value_disk)
        q_memory = ScalarPredictor(seed=72)
        q_disk = ScalarPredictor(seed=72)
        q_optimizer_memory = new_predictor_optimizer(q_memory)
        q_optimizer_disk = new_predictor_optimizer(q_disk)
        memory_receipt = fit_prior_block_predictors(
            arm="E", current_block_index=1, prior_blocks=[(0, source)],
            value_model=value_memory, value_optimizer=optimizer_memory,
            action_value_model=q_memory, action_value_optimizer=q_optimizer_memory,
        )
        binding = {"arm": "E", "current_block_index": 1, "prior_block_indices": [0],
                   "source_lock_sha256": "a" * 64}
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            manifest = build_predictor_cache([(0, source)], cache_path=cache, binding=binding)
            self.assertEqual(manifest["examples"], 300)
            disk_receipt = fit_cached_prior_block_predictors(
                arm="E", current_block_index=1, cache_path=cache, cache_manifest=manifest,
                value_model=value_disk, value_optimizer=optimizer_disk,
                action_value_model=q_disk, action_value_optimizer=q_optimizer_disk,
            )
            self.assertEqual(memory_receipt["value"]["steps"], 2)
            self.assertEqual(disk_receipt["value"]["steps"], 2)
            for left, right in ((value_memory, value_disk), (q_memory, q_disk)):
                for key in left.state_dict():
                    self.assertTrue(torch.equal(left.state_dict()[key], right.state_dict()[key]), key)
            # A complete cache is reused only for the exact same source/history.
            self.assertEqual(build_predictor_cache([], cache_path=cache, binding=binding), manifest)
            with self.assertRaisesRegex(RuntimeError, "different source/history"):
                build_predictor_cache([], cache_path=cache, binding={**binding, "arm": "D"})
            state_path = cache / "states.f32"
            with state_path.open("ab") as stream:
                stream.write(b"corruption")
            with self.assertRaisesRegex(RuntimeError, "size mismatch"):
                build_predictor_cache([], cache_path=cache, binding=binding)


if __name__ == "__main__":
    unittest.main()
