"""Focused offline contract checks for the Issue 70 study controller."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch

import runner
from policy import (
    AllNoncombatPolicy,
    ProjectionError,
    SemanticHashEncoder,
    derived_seed,
    validate_live_native_candidate_bindings,
)


NATIVE = "abf6eadbd2eb1ddf1aa8af4ede21922f9eaf0e16"
ACTION_ORDER = "StepSimulator::legalActions; ordered occurrences are retained without deduplication"


def _tag(value, *, visibility="oracle_only", source="native fixture"):
    return {"availability": "available", "visibility": visibility, "source": source, "value": value}


def projection_fixture():
    identities = [
        {"scope": "game", "bits": 101, "kind": "card_select", "label": "Strike", "idx1": 0, "idx2": 0, "idx3": 0},
        {"scope": "game", "bits": 202, "kind": "card_select", "label": "Defend", "idx1": 1, "idx2": 0, "idx3": 0},
    ]
    candidates = []
    for index, identity in enumerate(identities):
        candidates.append({
            "coverage_status": "supported",
            "candidate_binding": {"availability": "available", "visibility": "oracle_only", "source": "legalActions", "value": {"candidate_index": index, "candidate_action": dict(identity)}},
            "features": {
                "choice_type": _tag("CARD", source="ChoiceRenderer::type"),
                "card": _tag({"name": "Strike" if index == 0 else "Defend", "cost": 1}, source="CardDefinition"),
            },
        })
    return {
        "schema_id": "native-privileged-noncombat-choice-projection-v1",
        "audience": "training_oracle_only",
        "public_api_safe": False,
        "native_source_commit": NATIVE,
        "candidate_order_source": ACTION_ORDER,
        "candidate_count": 2,
        "supported_candidate_count": 2,
        "coverage_status": "supported",
        "unsupported_candidates": [],
        "screen_identity": _tag("CARD_SELECT", visibility="public_now", source="GameContext::screenState"),
        "shared_context": {"current_hp": _tag(70, visibility="public_now", source="GameContext::curHp")},
        "candidates": candidates,
    }, identities


class Issue70Selfcheck(unittest.TestCase):
    def setUp(self):
        self.encoder = SemanticHashEncoder(256)

    def test_live_ordered_candidate_bindings_are_exact_and_fail_closed(self):
        projection, identities = projection_fixture()
        actions = [SimpleNamespace(kind=item["kind"], label=item["label"], raw={key: item[key] for key in ("scope", "bits", "idx1", "idx2", "idx3")}) for item in identities]
        validate_live_native_candidate_bindings(projection, actions)
        projection["candidates"][1]["candidate_binding"]["value"]["candidate_index"] = 0
        with self.assertRaises(ProjectionError):
            validate_live_native_candidate_bindings(projection, actions)

    def test_native_ordered_binding_allows_sanitized_label_and_omitted_opaque_bits(self):
        projection, identities = projection_fixture()
        actions = [SimpleNamespace(kind=item["kind"], label=f"opaque live label {index}", raw={key: item[key] for key in ("scope", "bits", "idx1", "idx2", "idx3")}) for index, item in enumerate(identities)]
        for index, candidate in enumerate(projection["candidates"]):
            descriptor = candidate["candidate_binding"]["value"]["candidate_action"]
            descriptor.pop("bits")
            descriptor["label"] = f"sanitized native descriptor {index}"
        validate_live_native_candidate_bindings(projection, actions)
        projection["candidates"][1]["candidate_binding"]["value"]["candidate_action"]["idx1"] = 0
        with self.assertRaises(ProjectionError):
            validate_live_native_candidate_bindings(projection, actions)

    def test_opaque_bindings_action_labels_and_provenance_never_enter_features(self):
        projection, _ = projection_fixture()
        first, _ = self.encoder.encode(projection, 2)
        changed = copy.deepcopy(projection)
        for candidate in changed["candidates"]:
            binding = candidate["candidate_binding"]["value"]["candidate_action"]
            binding.update({"bits": -9999, "label": "opaque renamed native action"})
            candidate["candidate_binding"]["source"] = "changed source metadata"
            for feature in candidate["features"].values():
                feature["source"] = "changed source provenance"
        second, _ = self.encoder.encode(changed, 2)
        np.testing.assert_array_equal(first, second)

    def test_candidate_reordering_reorders_rows_without_encoding_slot(self):
        projection, _ = projection_fixture()
        first, _ = self.encoder.encode(projection, 2)
        changed = copy.deepcopy(projection)
        changed["candidates"].reverse()
        for index, candidate in enumerate(changed["candidates"]):
            candidate["candidate_binding"]["value"]["candidate_index"] = index
        second, _ = self.encoder.encode(changed, 2)
        np.testing.assert_array_equal(second, first[::-1])

    def test_unavailable_values_are_not_observed_and_visibility_is_encoded(self):
        projection, _ = projection_fixture()
        candidate = projection["candidates"][0]
        candidate["features"]["future"] = {"availability": "unavailable", "visibility": "unavailable", "source": "future oracle", "value": "secret A"}
        first, _ = self.encoder.encode(projection, 2)
        candidate["features"]["future"]["value"] = "secret B"
        second, _ = self.encoder.encode(projection, 2)
        np.testing.assert_array_equal(first, second)
        public = copy.deepcopy(projection)
        public["candidates"][0]["features"]["choice_type"]["visibility"] = "public_now"
        third, _ = self.encoder.encode(public, 2)
        self.assertFalse(np.array_equal(first[0], third[0]))

    def test_native_optional_unavailable_marker_maps_only_to_unavailable_channel(self):
        projection, _ = projection_fixture()
        feature = projection["candidates"][0]["features"]
        feature["future_optional"] = {
            "availability": "unavailable",
            "required_for_choice_semantics": False,
            "reason": "future MiscRng result is not available to the live choice",
        }
        first, _ = self.encoder.encode(projection, 2)
        feature["future_optional"]["reason"] = "changed diagnostic text must not be encoded"
        second, _ = self.encoder.encode(projection, 2)
        np.testing.assert_array_equal(first, second)

    def test_projection_contract_rejects_public_safe_and_count_mismatch(self):
        projection, _ = projection_fixture()
        projection["public_api_safe"] = True
        with self.assertRaises(ProjectionError):
            self.encoder.encode(projection, 2)
        projection, _ = projection_fixture()
        with self.assertRaises(ProjectionError):
            self.encoder.encode(projection, 3)

    def test_policy_is_normalized_legal_and_trainable(self):
        projection, _ = projection_fixture()
        matrix, _ = self.encoder.encode(projection, 2)
        model = AllNoncombatPolicy(256, seed=73507001)
        generator = torch.Generator(device="cpu").manual_seed(901)
        action, probabilities, _ = model.choose(matrix, generator)
        self.assertIn(action, (0, 1))
        self.assertAlmostEqual(float(probabilities.sum()), 1.0, places=6)
        optimizer = torch.optim.Adam(model.parameters(), lr=3e-4)
        before = [value.detach().clone() for value in model.parameters()]
        features = torch.as_tensor(matrix, dtype=torch.float32)
        _, log_probs, value = model.distribution(features)
        loss = -log_probs[action] + 0.5 * value.square() - 0.01 * (-(torch.exp(log_probs) * log_probs).sum())
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        self.assertTrue(any(parameter.grad is not None and torch.count_nonzero(parameter.grad).item() for parameter in model.actor.parameters()))
        self.assertTrue(any(parameter.grad is not None and torch.count_nonzero(parameter.grad).item() for parameter in model.critic.parameters()))
        torch.nn.utils.clip_grad_norm_(model.actor.parameters(), 1.0)
        torch.nn.utils.clip_grad_norm_(model.critic.parameters(), 1.0)
        optimizer.step()
        self.assertTrue(any(not torch.equal(old, new) for old, new in zip(before, model.parameters(), strict=True)))

    def test_exposure_strata_keep_offered_acquired_and_bottled_cases(self):
        projection, _ = projection_fixture()
        projection["candidates"][0]["features"].update({
            "event": _tag("FALLING", source="event choice"),
            "card": _tag({"name": "Strike", "bottled": True}, source="bottled card"),
            "relic": _tag("PRISMATIC_SHARD", source="relic offer"),
        })
        flags = runner.exposure_flags(projection, {"prismatic_shard_present": True})
        self.assertTrue(flags["prismatic_shard_offered"])
        self.assertTrue(flags["prismatic_shard_acquired"])
        self.assertTrue(flags["falling_bottled_card_case"])

    def test_interrupted_branch_attempt_is_charged_after_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            runner.append_jsonl(out / "coarse-state.jsonl", {
                "study_id": runner.STUDY_ID, "boundary": "branch_attempt_started",
                "attempt_id": "attempt-1", "run_id": "TRAIN_1_ROOT_0_ALT_1",
                "seed": 1, "root_index": 0, "candidate_index": 1, "cpu_limit_seconds": 4,
            })
            _, branches = runner._existing_cpu_charge(out)
            self.assertEqual(branches, 5.0)
            runner.append_jsonl(out / "coarse-state.jsonl", {
                "study_id": runner.STUDY_ID, "boundary": "branch_attempt_committed",
                "attempt_id": "attempt-1", "run_id": "TRAIN_1_ROOT_0_ALT_1",
                "seed": 1, "root_index": 0, "cpu_seconds": 3.1,
            })
            _, branches = runner._existing_cpu_charge(out)
            self.assertEqual(branches, 0.0)

    def test_unclosed_phase_charges_full_persisted_process_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            runner.append_jsonl(out / "coarse-state.jsonl", {
                "study_id": runner.STUDY_ID, "boundary": "phase_started",
                "phase_id": "preflight:1:10", "phase": "preflight",
                "rlimit_cpu_seconds": 37,
            })
            runs, branches = runner._existing_cpu_charge(out)
            self.assertEqual(runs, 37.0)
            self.assertEqual(branches, 0.0)

    def test_closed_phase_carries_unattributed_process_cpu_forward(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            runner.append_jsonl(out / "coarse-state.jsonl", {
                "study_id": runner.STUDY_ID, "boundary": "phase_started",
                "phase_id": "train:1:10", "phase": "train",
                "rlimit_cpu_seconds": 37,
            })
            runner.append_jsonl(out / "coarse-state.jsonl", {
                "study_id": runner.STUDY_ID, "boundary": "phase_finished",
                "phase_id": "train:1:10", "unattributed_process_cpu_seconds": 4.5,
            })
            runs, branches = runner._existing_cpu_charge(out)
            self.assertEqual(runs, 4.5)
            self.assertEqual(branches, 0.0)

    def test_zero_transition_optimizer_startup_receipt_is_archived_and_charged(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            run_path = out / "runs" / "TRAIN_7300001.json"
            runner.atomic_json(run_path, {
                "run_id": "TRAIN_7300001", "seed": 7300001, "arm": "TRAIN",
                "terminal_complete": False, "completed_transitions": 0,
                "error_type": "StudyFailure",
                "error": "Issue 65 public-history execution optimization is not installed",
                "screen_counts": {}, "action_kind_counts": {}, "decisions": [],
                "exposures": {"prismatic_shard_offered": False}, "cpu_seconds": 0.125,
            })
            archived = runner._archive_predecision_optimizer_startup_failure(
                out, run_path=run_path, run_id="TRAIN_7300001", seed=7300001, arm="TRAIN",
            )
            self.assertIsNotNone(archived)
            self.assertFalse(run_path.exists())
            self.assertEqual(runner._run_and_replay_cpu_total(out), 0.125)

    def test_seed_domain_is_stable_and_separates_arms(self):
        first = derived_seed(runner.STUDY_ID, 7300001, "P0", "digest", "evaluation")
        self.assertEqual(first, derived_seed(runner.STUDY_ID, 7300001, "P0", "digest", "evaluation"))
        self.assertNotEqual(first, derived_seed(runner.STUDY_ID, 7300001, "P1", "digest", "evaluation"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
