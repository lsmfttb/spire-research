"""Issue 73 Gate A source-identity preflight; deliberately never trains or steps native state."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gate_a
import runner
import source_lock_integrity


ROOT = Path(__file__).resolve().parent
TARGET_FILE = runner.CONFIG["stage0"]["synthetic_target"]["direction_file"]
EXPECTED_TARGET_SHA256 = runner.CONFIG["stage0"]["synthetic_target"]["direction_sha256"]
PREVIOUS_GATE_A_LEDGER = ROOT / "receipts" / "gate-a-requalification-v2" / "resource-ledger-final.json"
STAGE0_ROOTS = ROOT / "receipts" / "stage0" / "roots"


def load_locked_root_inputs() -> list[dict[str, object]]:
    roots: list[dict[str, object]] = []
    for path in sorted(STAGE0_ROOTS.glob("root-*.json")):
        source = json.loads(path.read_text(encoding="utf-8"))
        roots.append({
            "root_id": source["root_id"],
            "seed": source["owner_seed"],
            "run_id": source["owner_run_id"],
            "decision_index": source["owner_decision_index"],
            "policy_decision_index": source["policy_decision_index"],
            "screen": source["screen"],
            "projection": source["raw_privileged_projection"],
            "legal_action_identities": source["legal_action_occurrences"],
            "live_binding_validation_passed": source["candidate_binding_validation_passed"],
            "projection_digest": source["projection_digest"],
        })
    return roots


class GateATargetDirectionPreflightTests(unittest.TestCase):
    def _temporary_root(self):
        temp_parent = ROOT / ".tmp"
        temp_parent.mkdir(exist_ok=True)
        return tempfile.TemporaryDirectory(prefix="gate-a-target-preflight-", dir=temp_parent)

    def test_canonical_target_hash_accepts_lf_and_uniform_crlf(self):
        committed = (ROOT / TARGET_FILE).read_bytes()
        canonical_committed = source_lock_integrity.normalize_locked_source_eol(
            committed, label="committed target fixture",
        )
        self.assertEqual(gate_a._sha_bytes(canonical_committed), EXPECTED_TARGET_SHA256)
        for payload in (canonical_committed, canonical_committed.replace(b"\n", b"\r\n")):
            with self.subTest(crlf=b"\r\n" in payload):
                with self._temporary_root() as temp:
                    root = Path(temp)
                    (root / TARGET_FILE).write_bytes(payload)
                    with mock.patch.object(runner, "ROOT", root):
                        vector, digest = gate_a._target_direction()
                self.assertEqual(digest, EXPECTED_TARGET_SHA256)
                self.assertEqual(vector.shape, (int(runner.CONFIG["model"]["hash_dimension"]),))

    def test_substantive_target_change_fails_before_optimizer_or_output(self):
        roots = load_locked_root_inputs()
        self.assertEqual(len(roots), 225)
        selected = gate_a.select_corpus(roots)
        ledger = json.loads(PREVIOUS_GATE_A_LEDGER.read_text(encoding="utf-8"))
        expected_ids = ledger["terminal_attempt"]["selected_root_ids"]
        self.assertEqual([root["root_id"] for root in selected], expected_ids)

        committed = source_lock_integrity.normalize_locked_source_eol(
            (ROOT / TARGET_FILE).read_bytes(), label="committed target fixture",
        )
        changed = committed + b" "
        with self._temporary_root() as temp:
            root = Path(temp)
            (root / TARGET_FILE).write_bytes(changed)
            config = copy.deepcopy(runner.CONFIG)
            out = root / "must-not-be-created"
            with mock.patch.object(runner, "ROOT", root), mock.patch.object(runner, "CONFIG", config):
                with mock.patch.object(gate_a, "_train_encoding", side_effect=AssertionError("optimizer boundary crossed")) as train:
                    with mock.patch.object(gate_a, "AllNoncombatPolicy") as policy_constructor:
                        with self.assertRaisesRegex(RuntimeError, "canonical SHA-256"):
                            gate_a.run_gate_a(roots, out)
            train.assert_not_called()
            policy_constructor.assert_not_called()
            self.assertFalse(out.exists())

    def test_target_normalizer_rejects_bare_cr_and_mixed_line_endings(self):
        for payload in (b'{"x":1}\r', b'{"x":1}\r\n{"y":2}\n'):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RuntimeError, "invalid line endings|mixed line endings"):
                    gate_a._canonical_target_payload(payload, label="negative fixture")


if __name__ == "__main__":
    unittest.main()