from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

import torch
import numpy as np

PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parents[1]
sys.path.insert(0, str(PACKAGE))
sys.path.insert(0, str(REPO))
import preflight  # noqa: E402
import representations  # noqa: E402
from feature_firewall import new_path_projection  # noqa: E402
from test_feature_firewall import load_legacy_encoder, projection  # noqa: E402


def sample_payload(extra_offer: str = "Bash") -> dict:
    action_rows = [
        {"scope": "game", "kind": "reward_card", "idx1": 0, "idx2": 0, "idx3": 0},
        {"scope": "game", "kind": "reward_card", "idx1": 0, "idx2": 1, "idx3": 0},
        {"scope": "game", "kind": "skip", "idx1": 0, "idx2": 0, "idx3": 0},
    ]
    card_ids = ["Strike_R", extra_offer, None]
    candidates = []
    choices = []
    for index, (action, card_id) in enumerate(zip(action_rows, card_ids, strict=True)):
        kind = action["kind"]
        feature_value = {"choice_type": "card", "card": {"id": index + 1, "name": card_id, "upgraded": False}} if card_id is not None else {"choice_type": "skip"}
        features = {"choice": {"availability": "available", "visibility": "public_now", "source": "source-backed reward candidate semantics", "value": feature_value}}
        candidates.append(
            {
                "coverage_status": "supported",
                "features": features,
                "candidate_binding": {
                    "availability": "available",
                    "visibility": "oracle_only",
                    "value": {"candidate_index": index, "candidate_action": action},
                },
            }
        )
        choice = {"candidate_index": index, "candidate_action": action.copy()}
        if card_id is None:
            choice.update({"choice_type": "skip", "description": "Skip the remaining reward screen choices"})
        else:
            choice.update({"choice_type": "card", "card": {"id": index + 1, "name": card_id, "upgraded": False}})
        choices.append(choice)
    privileged = {
        "schema_id": "native-privileged-noncombat-choice-projection-v1",
        "audience": "training_oracle_only",
        "public_api_safe": False,
        "policy_training_authorized": False,
        "native_source_commit": "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2",
        "candidates": candidates,
        "screen_identity": {"availability": "available", "visibility": "public_now", "value": "CARD_REWARD"},
        "shared_context": {"gold": {"availability": "available", "visibility": "public_now", "value": 88}},
        "candidate_count": len(candidates),
        "supported_candidate_count": len(candidates),
        "unsupported_candidates": [],
        "coverage_status": "supported",
    }
    public = {
        "schema_id": "native-public-projection-v3",
        "screen_identity": {"availability": "available", "visibility": "public_now", "value": "CARD_REWARD"},
        "persistent_resources": {"availability": "available", "visibility": "public_now", "value": {"gold": {"availability": "available", "source": "GameContext::gold", "value": 88}}},
        "screen_payload": {"availability": "available", "visibility": "public_now", "value": {"coverage_status": "supported", "choices": choices}},
        "candidate_actions": {"availability": "available", "visibility": "public_now", "value": [
            *action_rows,
        ]},
    }
    return new_path_projection(privileged, public)


class RepresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp.name)
        self.module_context = preflight.load_pinned_model_module(REPO, self.data_root)
        self.model_module = self.module_context.__enter__()
        parent = self.model_module.ActionConditionedPolicy(
            dimension=2048, seed=73507501, final_weight_std=0.01
        )
        self.parent = parent.eval()
        self.parent_state = {key: value.detach().clone() for key, value in parent.state_dict().items()}

    def tearDown(self):
        self.module_context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_widened_bucket_folding_and_first_layer_lift_preserve_scores(self):
        torch.manual_seed(10)
        wide_features = torch.randn(7, 8192)
        folded = representations.fold_widened_hash_features(wide_features)
        expected = self.parent.logits(folded)
        for arm_name in ("B", "D"):
            arm = representations.Issue85Arm(arm_name, self.model_module, self.parent_state).eval()
            torch.testing.assert_close(arm.backbone.logits(wide_features), expected, rtol=1e-6, atol=1e-6)

    def test_real_signed_hash_tokens_fold_to_the_locked_2048_encoder(self):
        legacy_module = load_legacy_encoder()
        old, _ = legacy_module.SemanticHashEncoder(2048).encode(projection(13), 1)
        wide, _ = legacy_module.SemanticHashEncoder(8192).encode(projection(13), 1)
        folded = wide.reshape(1, 4, 2048).sum(axis=1)
        np.testing.assert_allclose(folded, old, rtol=0, atol=1e-7)

    def test_all_new_arms_start_at_parent_distribution(self):
        payload = sample_payload()
        legacy = torch.randn(3, 2048)
        for arm_name in ("B", "C", "D", "E", "F"):
            arm = representations.Issue85Arm(arm_name, self.model_module, self.parent_state).eval()
            features = legacy
            if arm.widened:
                # Construct a valid 8192 representation whose bucket-fold is
                # the chosen historical 2048 matrix.
                wide = torch.zeros(3, 8192)
                wide[:, :2048] = legacy
                features = wide
            actual_logits, actual_logp, actual_entropy = arm.distribution(features, payload)
            expected_logits, expected_logp, expected_entropy = self.parent.distribution(legacy)
            torch.testing.assert_close(actual_logits, expected_logits, rtol=1e-6, atol=1e-6)
            torch.testing.assert_close(actual_logp, expected_logp, rtol=1e-6, atol=1e-6)
            torch.testing.assert_close(actual_entropy, expected_entropy, rtol=1e-6, atol=1e-6)

    def test_c_and_d_flat_residual_respond_to_other_visible_offer(self):
        encoder = representations.FlatCandidateContextEncoder()
        left = encoder.encode(sample_payload("Bash"), device=torch.device("cpu"))
        right = encoder.encode(sample_payload("Inflame"), device=torch.device("cpu"))
        self.assertFalse(torch.equal(left[0], right[0]))

    def test_e_uses_only_a_context_while_f_sees_full_choice_context(self):
        first = sample_payload("Bash")
        second = sample_payload("Inflame")
        e = representations.RelationalSetEncoder("E")
        f = representations.RelationalSetEncoder("F")
        e_first = e.encode(first, device=torch.device("cpu"))
        e_second = e.encode(second, device=torch.device("cpu"))
        f_first = f.encode(first, device=torch.device("cpu"))
        f_second = f.encode(second, device=torch.device("cpu"))
        # The candidate field itself changes for the offered card, but the
        # first row's legacy-A information is invariant to a different offer.
        torch.testing.assert_close(e_first[0], e_second[0])
        self.assertFalse(torch.equal(f_first[0], f_second[0]))

    def test_candidate_permutation_equivariance_and_duplicate_multiplicity(self):
        payload = sample_payload()
        permutation = [2, 0, 1]
        permuted = dict(payload)
        permuted["candidates"] = [payload["candidates"][i] for i in permutation]
        # Full context set order is deliberately unchanged: it describes the
        # same visible screen and is itself encoded as an unordered set.
        for encoder in (
            representations.FlatCandidateContextEncoder(),
            representations.RelationalSetEncoder("E"),
            representations.RelationalSetEncoder("F"),
        ):
            original = encoder.encode(payload, device=torch.device("cpu"))
            shuffled = encoder.encode(permuted, device=torch.device("cpu"))
            # Symmetric reductions can differ by a few float32 ULPs when the
            # candidate order changes; demand numerical, not bitwise, equality.
            torch.testing.assert_close(shuffled, original[permutation], rtol=1e-6, atol=1e-7)

    def test_residual_heads_are_zero_but_trainable(self):
        arm = representations.Issue85Arm("C", self.model_module, self.parent_state)
        payload = sample_payload()
        features = arm.encoder.encode(payload, device=torch.device("cpu"))
        self.assertGreater(float(arm.residual.layers[0].weight.detach().abs().sum()), 0.0)
        self.assertEqual(float(arm.residual.layers[-1].weight.detach().abs().sum()), 0.0)
        output = arm.residual(features)
        self.assertTrue(torch.equal(output, torch.zeros_like(output)))
        loss = arm.residual.layers[-1].weight.sum() + arm.residual.layers[-1].bias.sum()
        loss.backward()
        self.assertIsNotNone(arm.residual.layers[-1].weight.grad)

    def test_locked_pi12_checkpoint_and_optimizer_transfer(self):
        roots = [Path("/mnt/d/DeadlyCatCoding/stsrl-data"), Path(r"D:\DeadlyCatCoding\stsrl-data")]
        data_root = next((root for root in roots if root.is_dir()), None)
        if data_root is None:
            self.skipTest("owner-managed Issue 82 checkpoints are not mounted in this environment")
        lock = preflight.LOCK
        parent_lock = lock["baseline_P"]
        parent_path = data_root / parent_lock["checkpoint_data_path"]
        optimizer_path = data_root / parent_lock["optimizer_data_path"]
        self.assertEqual(hashlib.sha256(parent_path.read_bytes()).hexdigest(), parent_lock["checkpoint_sha256"])
        self.assertEqual(hashlib.sha256(optimizer_path.read_bytes()).hexdigest(), parent_lock["optimizer_sha256"])
        parent_state = torch.load(parent_path, map_location="cpu", weights_only=True)
        optimizer_bundle = torch.load(optimizer_path, map_location="cpu", weights_only=True)
        parent_model = self.model_module.ActionConditionedPolicy(dimension=2048, seed=73507501, final_weight_std=0.01)
        parent_model.load_state_dict(parent_state, strict=True)
        self.assertEqual(self.model_module.model_digest(parent_model), parent_lock["model_digest"])

        for arm_name in ("B", "C", "D", "E", "F"):
            arm = representations.Issue85Arm(arm_name, self.model_module, parent_state)
            optimizer, metadata = representations.continue_parent_adamw(
                arm, optimizer_bundle, expected_parent_digest=parent_lock["model_digest"]
            )
            self.assertEqual(metadata["parent_optimizer_completed_steps"], 64)
            for parameter in arm.backbone.parameters():
                self.assertEqual(int(optimizer.state[parameter]["step"].item()), 64)
            residual_parameters = list(arm.residual.parameters()) if arm.residual is not None else []
            self.assertTrue(all(parameter not in optimizer.state for parameter in residual_parameters))
            if arm.widened:
                first_layer = arm.backbone.actor[0]
                torch.testing.assert_close(
                    optimizer.state[first_layer.weight]["exp_avg"],
                    optimizer.state[first_layer.weight]["exp_avg"][:, :2048].repeat(1, 4),
                    rtol=0,
                    atol=0,
                )
                torch.testing.assert_close(
                    optimizer.state[first_layer.weight]["exp_avg_sq"],
                    optimizer.state[first_layer.weight]["exp_avg_sq"][:, :2048].repeat(1, 4),
                    rtol=0,
                    atol=0,
                )


if __name__ == "__main__":
    unittest.main()
