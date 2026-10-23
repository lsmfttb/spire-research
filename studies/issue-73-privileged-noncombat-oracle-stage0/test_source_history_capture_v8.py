from __future__ import annotations

import copy
import json
import unittest

import checkpoint_replay_qualification as q
import source_qualification_v8 as v8
import stage0
import runner


class SourceHistoryCaptureV8Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.owner, cls.attempt = q.historical_run_receipts()
        cls.owner_root_ids = [str(x) for x in cls.attempt["root_ids"]]
        lock_path, _config, _plan, manifest_path = v8.v7._lock_paths(v8.v7.V7_CAPTURE_LOCK_OUT)
        cls.parent_lock = json.loads(lock_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        cls.locked_hashes = {
            str(row["path"]): str(row["git_blob_sha256"])
            for row in manifest["included"]
        }
        cls.historical_roots = {
            root_id: q.root_receipt(root_id)
            for root_id in cls.owner_root_ids
        }
        cls.live_roots = [q.compact_root(cls.historical_roots[root_id]) for root_id in cls.owner_root_ids]

    def make_result(self):
        return {
            "terminal": self.owner["terminal"],
            "terminal_complete": self.owner["terminal_complete"],
            "completed_transitions": self.owner["completed_transitions"],
            "run_outcome": self.owner["run_outcome"],
            "decisions": copy.deepcopy(self.owner["decisions"]),
            "screen_counts": self.owner["screen_counts"],
            "action_kind_counts": self.owner["action_kind_counts"],
        }

    def make_history(self):
        return [{"transition": {"index": i}} for i in range(int(self.owner["completed_transitions"]))]

    def compare(self, *, result=None, roots=None):
        return v8.compare_capture_run_v8(
            result or self.make_result(),
            self.live_roots if roots is None else roots,
            self.make_history(),
            owner=self.owner,
            owner_attempt=self.attempt,
            historical_roots=self.historical_roots,
        )

    def test_original_owner_provenance_reconstructs_exact_policy_stream(self):
        identity = v8.original_replay_identity(self.owner)
        self.assertEqual(identity["study_id"], "issue-73-privileged-noncombat-oracle-stage0")
        self.assertEqual(identity["seed"], 173730001)
        self.assertEqual(identity["arm"], "P0")
        self.assertEqual(identity["model_digest"], v8.PURPOSE_MODEL_DIGEST)
        self.assertEqual(identity["sampling_purpose"], "issue73-stage0-debug-owner")
        expected_seed = v8.policy.derived_seed(
            identity["study_id"], identity["seed"], identity["arm"],
            identity["model_digest"], identity["sampling_purpose"],
        )
        self.assertEqual(identity["policy_rng_seed"], expected_seed)

    def test_seed_arm_model_digest_and_purpose_mutations_are_rejected(self):
        identity = v8.original_replay_identity(self.owner)
        for key, value in (
            ("seed", identity["seed"] + 1),
            ("arm", "P1"),
            ("model_digest", "0" * 64),
            ("sampling_purpose", "issue73-source-qualification-exact-debug-owner-replay"),
            ("policy_rng_seed", identity["policy_rng_seed"] + 1),
        ):
            with self.subTest(key=key):
                changed = dict(identity)
                changed[key] = value
                self.assertIn(key, v8.replay_provenance_mismatches(changed, identity))

    def test_historical_owner_roots_prevalidate_without_single_action_gap_receipts(self):
        self.assertIn("root-173730001-0011", self.owner_root_ids)
        self.assertNotIn("root-173730001-0012", self.owner_root_ids)
        self.assertNotIn("root-173730001-0013", self.owner_root_ids)
        self.assertIn("root-173730001-0014", self.owner_root_ids)
        verified = v8.prevalidate_historical_roots(
            self.owner_root_ids, self.locked_hashes,
            source_commit=self.parent_lock["code_source_commit"],
        )
        self.assertEqual(verified["status"], "PASS", verified["issues"])
        self.assertEqual(set(verified["roots"]), set(self.owner_root_ids))

    def test_root_membership_mismatch_is_explicit_and_never_passes_on_intersection(self):
        missing_id = self.owner_root_ids[8]
        missing = [root for root in self.live_roots if stage0.root_id(root) != missing_id]
        missing_report = self.compare(roots=missing)
        self.assertEqual(missing_report["status"], "FAIL_SOURCE_HISTORY_CAPTURE_PARITY")
        self.assertEqual(missing_report["root_membership"]["missing_root_ids"], [missing_id])

        extra = [*self.live_roots, {"seed": 173730001, "decision_index": 9999}]
        extra_report = self.compare(roots=extra)
        self.assertEqual(extra_report["status"], "FAIL_SOURCE_HISTORY_CAPTURE_PARITY")
        self.assertEqual(extra_report["root_membership"]["unexpected_root_ids"], ["root-173730001-9999"])

    def test_matching_full_trace_and_exact_root_set_pass(self):
        report = self.compare()
        self.assertEqual(report["status"], "PASS")
        self.assertIsNone(report["first_trace_difference"])
        self.assertTrue(report["root_membership"]["exact_membership"])
        self.assertTrue(report["required_roots_match"])
        self.assertTrue(all(x["all_fields_match"] for x in report["roots"].values()))

    def test_runtime_volatile_roots_without_root_id_match_by_seed_and_decision(self):
        # runner.Controller's live root dictionaries have seed and
        # decision_index, but only persisted historical receipts add root_id.
        roots = copy.deepcopy(self.live_roots)
        for root in roots:
            root.pop("root_id")
            root.pop("source_tag_mapping")
            root.pop("feature_matrix_sha256")
            root.update({
                "checkpoint": object(),
                "adapter": object(),
                "native_checkpoint_id": None,
                "checkpoint_adapter_id": None,
                "native_checkpoint_metadata": {},
                "probabilities": None,
                "audit": {},
            })

        self.assertTrue(all("root_id" not in root for root in roots))
        report = self.compare(roots=roots)

        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["root_membership"]["exact_membership"])
        self.assertEqual(report["captured_root_count"], len(self.owner_root_ids))
        self.assertEqual(report["historical_root_match_count"], len(self.owner_root_ids))
        self.assertTrue(all(row["status"] == "MATCH" for row in report["roots"].values()))

    def test_durable_root_snapshots_retain_comparison_inputs_without_live_handles(self):
        roots = copy.deepcopy(self.live_roots)
        for root in roots:
            root.pop("root_id")
            root.pop("source_tag_mapping")
            root.pop("feature_matrix_sha256")
            root["checkpoint"] = object()
            root["adapter"] = object()

        record = v8.capture_root_snapshots_record_v8(roots)

        self.assertEqual(record["schema_id"], "issue73-volatile-root-snapshots-v8-v1")
        self.assertEqual(record["root_count"], len(self.owner_root_ids))
        by_id = {row["root_id"]: row for row in record["roots"]}
        self.assertEqual(set(by_id), set(self.owner_root_ids))
        for root in roots:
            root_id = stage0.root_id(root)
            snapshot = by_id[root_id]
            self.assertNotIn("checkpoint", snapshot)
            self.assertNotIn("adapter", snapshot)
            self.assertEqual(snapshot["seed"], root["seed"])
            self.assertEqual(snapshot["decision_index"], root["decision_index"])
            self.assertEqual(snapshot["projection"], root["projection"])
            self.assertEqual(snapshot["public_projection"], root["public_projection"])
            self.assertEqual(snapshot["legal_action_identities"], root["legal_action_identities"])

    def test_first_action_trace_difference_is_durable_and_indexed(self):
        result = self.make_result()
        result["decisions"][14]["chosen_index"] = 0
        report = self.compare(result=result)
        self.assertNotEqual(report["status"], "PASS")
        self.assertEqual(report["first_trace_difference"]["index"], 14)
        self.assertFalse(report["run_parity"]["full_decision_trace"])

    def test_missing_locked_root_hash_returns_diagnostic_not_file_not_found(self):
        missing_root = self.owner_root_ids[0]
        path = q.root_path(missing_root)
        repo_path = path.relative_to(q.REPOSITORY).as_posix()
        reduced = dict(self.locked_hashes)
        reduced.pop(repo_path)
        report = v8.prevalidate_historical_roots(
            self.owner_root_ids, reduced,
            source_commit=self.parent_lock["code_source_commit"],
        )
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any(row["root_id"] == missing_root and row["reason"] == "root_path_absent_from_locked_input_manifest" for row in report["issues"]))

    def test_membership_comparison_reports_duplicates(self):
        report = v8.compare_root_membership(["a", "b"], ["a", "a", "c"])
        self.assertFalse(report["exact_membership"])
        self.assertEqual(report["missing_root_ids"], ["b"])
        self.assertEqual(report["unexpected_root_ids"], ["c"])
        self.assertEqual(report["duplicate_observed_root_ids"], ["a"])

    def test_complete_trace_preserves_selected_legal_occurrence_order(self):
        result = self.make_result()
        # Same selected index but changed source-ordered legal identity is still a trace mismatch.
        result["decisions"][0]["legal_action_identities"].reverse()
        report = self.compare(result=result)
        self.assertNotEqual(report["status"], "PASS")
        self.assertEqual(report["first_trace_difference"]["index"], 0)


if __name__ == "__main__":
    unittest.main()
