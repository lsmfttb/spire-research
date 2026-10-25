from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import protocol
import runner


def synthetic_roots() -> list[dict]:
    result = []
    for owner_index, owner in enumerate(protocol.OWNER_SEEDS):
        count = 29 if owner_index == 7 else 28
        for decision in range(count):
            root_id = f"root-{owner}-{decision:04d}"
            result.append({
                "root_id": root_id,
                "owner_seed": owner,
                "owner_decision_index": decision,
                "policy_decision_index": decision,
                "screen": ("EVENT_SCREEN", "REWARDS", "MAP_SCREEN", "REST_ROOM")[decision % 4],
                "candidate_binding_validation_passed": True,
                "legal_action_occurrences": [{"stable_id": f"{root_id}:{i}"} for i in range(2 + decision % 4)],
                "projection_digest": f"projection-{root_id}",
                "feature_matrix_sha256": f"features-{root_id}",
                "root_fingerprints": {"native_state_sha256": f"native-{root_id}"},
                "_source_file_sha256": f"source-{root_id}",
                "terminal_outcome_forbidden_selection": decision,
            })
    return result


class Issue74ProtocolTests(unittest.TestCase):
    def test_selection_is_fixed_top_four_per_owner_and_ignores_outcomes(self) -> None:
        records = synthetic_roots()
        first = protocol.select_roots(records)
        for record in records:
            record["terminal_outcome_forbidden_selection"] = -record["owner_decision_index"]
        second = protocol.select_roots(records)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)
        self.assertEqual({owner: sum(row["owner_seed"] == owner for row in first)
                          for owner in protocol.OWNER_SEEDS}, {owner: 4 for owner in protocol.OWNER_SEEDS})
        self.assertFalse(protocol.GATE_C_ROOT_IDS.intersection(row["root_id"] for row in first))
        self.assertTrue(all(row["selected_before_eligibility_checks"] for row in first))

    def test_selection_hash_uses_the_registered_coordinates(self) -> None:
        row = synthetic_roots()[0]
        original = protocol.selection_key(row)
        row["terminal_outcome_forbidden_selection"] = "changed"
        self.assertEqual(protocol.selection_key(row), original)
        row["screen"] = "different screen"
        self.assertNotEqual(protocol.selection_key(row), original)

    def test_rng_streams_are_stable_and_independent(self) -> None:
        common = {"owner_seed": 173730001, "root_id": "root-173730001-0004",
                  "decision_index": 4, "candidate_index": 1, "replicate": 3}
        control = protocol.stream_seed(**common, stream="control_policy")
        self.assertEqual(control, protocol.stream_seed(**common, stream="control_policy"))
        values = {control}
        for stream in ("control_expert", "oracle_policy", "oracle_expert"):
            value = protocol.stream_seed(**common, stream=stream)
            self.assertNotIn(value, values)
            values.add(value)

    def test_resource_ceiling_keeps_reviewer_reserve(self) -> None:
        charge = protocol.ResourceCharge(0, 0)
        charge = charge.add(builder_cpu_seconds=100000, native_cpu_seconds=85000)
        with self.assertRaises(protocol.ContractError):
            charge.add(builder_cpu_seconds=0.1, native_cpu_seconds=0)

    def test_selection_rejects_unqualified_or_wrong_sized_population(self) -> None:
        records = synthetic_roots()
        records[4]["candidate_binding_validation_passed"] = False
        with self.assertRaises(protocol.ContractError):
            protocol.select_roots(records)
        with self.assertRaises(protocol.ContractError):
            protocol.select_roots(records[:-1])

    def test_source_trace_match_rejects_reordered_legal_occurrences(self) -> None:
        row = synthetic_roots()[0]
        event = {"kind": "categorical", "native_screen": row["screen"],
                 "projection_digest": row["projection_digest"],
                 "legal_action_identities": row["legal_action_occurrences"],
                 "chosen_index": 0}
        protocol.verify_source_trace_event(row, event)
        event["legal_action_identities"] = list(reversed(event["legal_action_identities"]))
        with self.assertRaises(protocol.ContractError):
            protocol.verify_source_trace_event(row, event)

    def test_replayed_root_fingerprints_and_forced_occurrence_are_exact(self) -> None:
        row = synthetic_roots()[0]
        actual = {"screen": row["screen"], "projection_digest": row["projection_digest"],
                  "legal_action_identities": row["legal_action_occurrences"],
                  **row["root_fingerprints"], "checkpoint": object()}
        protocol.verify_replayed_root(row, actual)
        identities = row["legal_action_occurrences"]
        index = 1
        receipt = {"forced_root_consumed": True, "candidate_index": index,
                   "candidate_identity_sha256": protocol.sha256_bytes(protocol.canonical_payload(identities[index])),
                   "legal_action_identities_sha256": protocol.sha256_bytes(protocol.canonical_payload(identities))}
        protocol.verify_forced_root_receipt(row, index, receipt)
        receipt["candidate_index"] = 0
        with self.assertRaises(protocol.ContractError):
            protocol.verify_forced_root_receipt(row, index, receipt)


class ReviewerPreflightGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="issue74-review-gate-",
                                                dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.here = self.repo / "studies" / "issue-74-oracle-feasibility"
        self.here.mkdir(parents=True)
        self.selection_path = self.here / "selection-lock.json"
        self.preflight_path = self.here / "reviewer-preflight.json"
        self._git("init", "-q")
        self._git("config", "user.name", "Issue 74 gate test")
        self._git("config", "user.email", "issue74-gate-test@example.invalid")
        (self.repo / "baseline.txt").write_text("baseline\n", encoding="utf-8")
        self._git("add", "--", "baseline.txt")
        self._git("commit", "-q", "-m", "fixture baseline")
        self._write_json(self.selection_path, {
            "schema_id": "issue74-prospective-selection-lock-v1", "selection": "original"})
        self._commit_path(self.selection_path, "reviewed selection")
        self.reviewed_commit = self._git("rev-parse", "HEAD")
        self._write_valid_preflight()
        self._commit_path(self.preflight_path, "record reviewer preflight")

    def _git(self, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(self.repo), *args], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            self.fail(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def _write_json(self, path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8")

    def _commit_path(self, path: Path, message: str) -> None:
        relative = path.relative_to(self.repo).as_posix()
        self._git("add", "--", relative)
        self._git("commit", "-q", "-m", message)

    def _write_valid_preflight(self) -> None:
        self._write_json(self.preflight_path, {
            "status": "PASS",
            "reviewer_login": "independent-reviewer",
            "github_comment_url": "https://github.com/lsmfttb/spire-research/issues/74#issuecomment-1234567890",
            "reviewed_commit": self.reviewed_commit,
            "reviewed_selection_lock_sha256": runner._text_sha(self.selection_path),
        })

    def _verify(self) -> dict:
        with (patch.object(runner, "REPO", self.repo),
              patch.object(runner, "HERE", self.here),
              patch.object(runner, "SELECTION_LOCK_PATH", self.selection_path),
              patch.object(runner, "REVIEWER_PREFLIGHT_PATH", self.preflight_path),
              patch.object(runner, "_verify_package", return_value={})):
            return runner.verify_reviewer_preflight()

    def test_committed_preflight_for_exact_reviewed_selection_passes(self) -> None:
        result = self._verify()
        self.assertEqual(result["reviewed_commit"], self.reviewed_commit)
        self.assertEqual(result["selection_lock_sha256"], runner._text_sha(self.selection_path))

    def test_uncommitted_selection_and_matching_preflight_are_rejected(self) -> None:
        self._write_json(self.selection_path, {
            "schema_id": "issue74-prospective-selection-lock-v1", "selection": "tampered"})
        self._write_valid_preflight()
        with self.assertRaisesRegex(protocol.ContractError, "clean execution worktree"):
            self._verify()

    def test_committed_selection_tampering_after_review_is_rejected(self) -> None:
        self._write_json(self.selection_path, {
            "schema_id": "issue74-prospective-selection-lock-v1", "selection": "tampered"})
        self._commit_path(self.selection_path, "tamper reviewed selection")
        with self.assertRaisesRegex(protocol.ContractError, "selected-root lock Git blob changed"):
            self._verify()

    def test_ancestor_without_the_reviewed_selection_blob_is_rejected(self) -> None:
        preflight = json.loads(self.preflight_path.read_text(encoding="utf-8"))
        preflight["reviewed_commit"] = self._git("rev-parse", f"{self.reviewed_commit}^")
        self._write_json(self.preflight_path, preflight)
        self._commit_path(self.preflight_path, "cite ancestor without selection lock")
        with self.assertRaisesRegex(protocol.ContractError, "selected-root lock Git blob changed"):
            self._verify()

    def test_uncommitted_preflight_edit_is_rejected(self) -> None:
        self._write_json(self.preflight_path, {
            "status": "PASS", "reviewer_login": "forged-reviewer"})
        with self.assertRaisesRegex(protocol.ContractError, "clean execution worktree"):
            self._verify()

    def test_committed_preflight_tampering_after_pass_is_rejected(self) -> None:
        tampered = json.loads(self.preflight_path.read_text(encoding="utf-8"))
        tampered["reviewer_login"] = "forged-reviewer"
        self._write_json(self.preflight_path, tampered)
        self._commit_path(self.preflight_path, "tamper reviewer receipt")
        with self.assertRaisesRegex(protocol.ContractError, "only commit after the reviewed SHA"):
            self._verify()


if __name__ == "__main__":
    unittest.main()
