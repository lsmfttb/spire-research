"""Focused offline source-lock and terminal-branch contract checks for Issue 71."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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


NATIVE = "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2"
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


class Issue71Selfcheck(unittest.TestCase):
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


    def test_committed_config_lock_is_exact_and_stage1_remains_closed(self):
        config_bytes = runner.CONFIG_PATH.read_bytes()
        locked = runner.CONFIG_LOCK_PATH.read_text(encoding="ascii").strip().lower()
        self.assertEqual(hashlib.sha256(config_bytes).hexdigest(), locked)
        self.assertEqual(runner.CONFIG_SHA256, locked)
        self.assertIs(runner.CONFIG["stage0"]["stage1_authorized"], False)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for phase in (runner.run_preflight, runner.run_training, runner.run_evaluation):
                with self.subTest(phase=phase.__name__):
                    with self.assertRaisesRegex(runner.StudyFailure, "STAGE1_NOT_AUTHORIZED"):
                        phase(output)
            self.assertFalse((output / "config-lock.json").exists())
            self.assertFalse((output / "runs").exists())
            cli = subprocess.run(
                [sys.executable, os.fspath(runner.ROOT / "runner.py"), "train",
                 "--output", os.fspath(output / "formal")],
                cwd=runner.ROOT, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(cli.returncode, 2)
            self.assertIn("invalid choice", cli.stderr)
            self.assertFalse((output / "formal").exists())

    def test_one_byte_config_mutation_fails_at_fresh_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            for name in ("runner.py", "policy.py", "config.json", "config.lock.sha256"):
                (target / name).write_bytes((runner.ROOT / name).read_bytes())
            config_path = target / "config.json"
            payload = config_path.read_bytes()
            marker = b'"disposition": "STUDY_ONLY"'
            self.assertIn(marker, payload)
            config_path.write_bytes(payload.replace(marker, b'"disposition": "STUDY-ONLY"', 1))
            completed = subprocess.run(
                [os.fspath(Path(sys.executable)), "-c", "import runner"],
                cwd=target, capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("config.json bytes do not match", completed.stderr)

    def test_stage0_screen_normalization_uses_native_map_and_rewards_aliases(self):
        self.assertEqual(runner._stage0_screen_key("MAP_SCREEN"), "MAP")
        self.assertEqual(runner._stage0_screen_key("MAP"), "MAP")
        self.assertEqual(runner._stage0_screen_key("REWARDS"), "REWARDS")
        self.assertEqual(runner._stage0_screen_key("REWARD_SCREEN"), "REWARDS")

    def test_checkpoint_adapter_delegates_optional_projection_and_fingerprint(self):
        snapshot = object()
        projection = {"availability": "available", "value": "public"}
        fingerprint = (("visible",), {"screen_state": "MAP"})
        base = SimpleNamespace(
            public_projection=mock.Mock(return_value=projection),
            checkpoint_fingerprint=mock.Mock(return_value=fingerprint),
        )
        adapter = runner.CheckpointAdapter(base, object())
        self.assertIs(adapter.public_projection(snapshot), projection)
        self.assertIs(adapter.checkpoint_fingerprint(snapshot), fingerprint)
        base.public_projection.assert_called_once_with(snapshot)
        base.checkpoint_fingerprint.assert_called_once_with(snapshot)

    def test_child_failure_classification_preserves_distinct_boundaries(self):
        exited_nonzero = 1 << 8
        self.assertEqual(
            runner._child_failure_class(
                wait_status=exited_nonzero,
                payload={"error_type": "ValueError", "error": "invalid choice index 99"},
                raw_payload=b'{"error":"invalid choice index 99"}', cpu_limit=5, child_cpu=0.1,
            ),
            "invalid_choice",
        )
        for native_problem in (
            "BranchChooser selected action index 3 outside 3 legal actions",
            "BranchChooser selected action index 0 outside the active action space",
        ):
            self.assertTrue(runner._looks_like_invalid_choice(native_problem))
        self.assertEqual(
            runner._child_failure_class(
                wait_status=exited_nonzero,
                payload={"error_type": "StudyFailure", "error": "run incomplete",
                         "run_receipt": {"terminal_complete": False}},
                raw_payload=b'{}', cpu_limit=5, child_cpu=0.1,
            ),
            "incomplete_terminal",
        )
        self.assertEqual(
            runner._child_failure_class(
                wait_status=signal.SIGXCPU, payload=None, raw_payload=b"", cpu_limit=5, child_cpu=5.0,
            ),
            "cpu_cap",
        )
        self.assertEqual(
            runner._child_failure_class(
                wait_status=signal.SIGSEGV, payload=None, raw_payload=b"", cpu_limit=5, child_cpu=0.1,
            ),
            "worker_death",
        )
        self.assertEqual(
            runner._child_failure_class(
                wait_status=exited_nonzero, payload=None, raw_payload=b"{broken", cpu_limit=5, child_cpu=0.1,
            ),
            "malformed_pipe",
        )

    @unittest.skipUnless(hasattr(os, "fork") and hasattr(os, "wait4"), "Linux child-process evidence test")
    def test_branch_child_preserves_unabridged_exception_payload_and_wait_status(self):
        message = "raw native exception payload: exact diagnostic token / checkpoint mismatch"
        with tempfile.TemporaryDirectory() as temporary:
            root = {
                "screen": "MAP", "candidate_index": 0, "legal_action_identities": [],
                "decision_index": 7, "native_checkpoint_id": "debug-root-0",
            }
            with mock.patch.object(runner, "_run_one", side_effect=runner.StudyFailure(message)):
                result, child_cpu, parent_cpu, status = runner._branch_child(
                    out=Path(temporary), module=None, base_adapter=None, checkpoint=object(),
                    model=None, model_digest="frozen", encoder=None,
                    seed=171000071, root_index=0, root=root, candidate_index=1,
                    cpu_limit=3, label="DEBUG_171000071_ROOT_0_ALT_1",
                )
        self.assertIsInstance(result, dict)
        self.assertEqual(result["child_failure_class"], "child_exception")
        self.assertEqual(result["error"], message)
        self.assertIn(message, result["raw_child_payload"])
        self.assertEqual(result["wait_status"], status)
        self.assertEqual(result["child_exit_code"], 1)
        self.assertIsNone(result["child_signal_number"])
        self.assertGreaterEqual(child_cpu, 0.0)
        self.assertGreaterEqual(parent_cpu, 0.0)
        self.assertNotIn("branch CPU cap reached", result["raw_child_payload"])

    def test_seed_domain_is_stable_and_separates_arms(self):
        first = derived_seed(runner.STUDY_ID, 7300001, "P0", "digest", "evaluation")
        self.assertEqual(first, derived_seed(runner.STUDY_ID, 7300001, "P0", "digest", "evaluation"))
        self.assertNotEqual(first, derived_seed(runner.STUDY_ID, 7300001, "P1", "digest", "evaluation"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
