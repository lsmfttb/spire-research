from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import checkpoint_replay_qualification as q
import source_qualification_v9 as v9_engineering_1
import source_qualification_v9_engineering_2 as v9

RUNTIME_TEMP_ROOT = v9.REPOSITORY / ".builder-runtime" / "tmp"


class SourceHistoryCaptureV9Engineering2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pairs = json.loads(v9_engineering_1.CONFIG_PATH.read_text(encoding="utf-8"))["gate_c_transition_pairs"]

    def passing_witnesses(self):
        return {
            f"{row['root_id']}-a{int(row['candidate_index'])}": {
                "status": "PASS",
                "transition": {"candidate_index": int(row["candidate_index"]), "terminal": False},
            }
            for row in self.pairs
        }

    def test_passed_parent_capture_is_missing_all_locked_transition_witnesses(self):
        report = json.loads(v9_engineering_1.CAPTURE_REPORT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(len(self.pairs), 9)
        integrity = v9.transition_witness_integrity_v9(report.get("transition_witnesses", {}), self.pairs)
        self.assertFalse(integrity["complete_pass"])
        self.assertEqual(integrity["expected_count"], 9)
        self.assertEqual(integrity["missing_keys"], sorted(
            f"{row['root_id']}-a{int(row['candidate_index'])}" for row in self.pairs
        ))

    def test_exact_nine_repeated_same_process_witnesses_pass(self):
        integrity = v9.transition_witness_integrity_v9(self.passing_witnesses(), self.pairs)
        self.assertTrue(integrity["complete_pass"])
        self.assertEqual(integrity["observed_count"], 9)
        self.assertEqual(integrity["missing_keys"], [])
        self.assertEqual(integrity["unexpected_keys"], [])
        self.assertEqual(integrity["invalid_keys"], [])

    def test_missing_unexpected_failed_and_malformed_witnesses_are_rejected(self):
        witnesses = self.passing_witnesses()
        missing_key = next(iter(witnesses))
        witnesses.pop(missing_key)
        witnesses["unexpected-a99"] = {"status": "PASS", "transition": {}}
        bad_key = next(iter(witnesses))
        witnesses[bad_key] = {"status": "FAIL", "transition": {}}
        malformed_key = next(iter(set(witnesses) - {"unexpected-a99", bad_key}))
        witnesses[malformed_key] = {"status": "PASS", "transition": None}
        integrity = v9.transition_witness_integrity_v9(witnesses, self.pairs)
        self.assertFalse(integrity["complete_pass"])
        self.assertIn(missing_key, integrity["missing_keys"])
        self.assertEqual(integrity["unexpected_keys"], ["unexpected-a99"])
        self.assertEqual(set(integrity["invalid_keys"]), {bad_key, malformed_key})

    def test_capture_cannot_remain_pass_without_complete_witness_set(self):
        report = {"status": "PASS", "captured_root_count": 39}
        gated = v9.apply_transition_witness_gate_v9(report, {}, self.pairs)
        self.assertEqual(gated["status"], "FAIL_SOURCE_HISTORY_OR_TRANSITION_WITNESS_CAPTURE")
        self.assertFalse(gated["transition_witness_integrity"]["complete_pass"])

    def test_repair_ledger_preserves_parent_charges_and_remaining_stage0_reserves(self):
        base = v9._base_prior_v9_ledger()
        ledger = v9._ledger_template(base)
        ledger["attempts"].append({
            "key": v9.STATIC_KEY,
            "cpu_charged_seconds": 60.0,
            "native_gate_bc": False,
            "contingency_pool": "nonnative",
        })
        projection = v9._resource_projection(base, ledger)
        self.assertAlmostEqual(projection["builder_charge_including_v9_cpu_seconds"], 2711.610134)
        self.assertAlmostEqual(projection["cumulative_builder_projection_cpu_seconds"], 4511.610134)
        self.assertEqual(projection["native_gate_bc_charge_including_v9_cpu_seconds"], 1440.0)
        self.assertEqual(projection["cumulative_native_gate_bc_projection_cpu_seconds"], 3060.0)
        self.assertEqual(projection["nonnative_contingency_remaining_cpu_seconds"], 60.0)
        self.assertEqual(projection["native_contingency_remaining_cpu_seconds"], 240.0)
        self.assertEqual(projection["gate_c_suffix_attempt_count"], 64)
        self.assertEqual(projection["gate_c_suffix_total_reserve_cpu_seconds"], 1280.0)

    def test_repair_capture_spends_only_existing_native_contingency(self):
        base = v9._base_prior_v9_ledger()
        ledger = v9._ledger_template(base)
        ledger["attempts"].extend([
            {"key": v9.STATIC_KEY, "cpu_charged_seconds": 60.0, "native_gate_bc": False, "contingency_pool": "nonnative"},
            {"key": v9.ATTEMPT_KEY, "cpu_charged_seconds": 60.0, "native_gate_bc": True, "contingency_pool": "native"},
        ])
        projection = v9._resource_projection(base, ledger)
        self.assertAlmostEqual(projection["builder_charge_including_v9_cpu_seconds"], 2771.610134)
        self.assertAlmostEqual(projection["cumulative_builder_projection_cpu_seconds"], 4511.610134)
        self.assertEqual(projection["native_gate_bc_charge_including_v9_cpu_seconds"], 1500.0)
        self.assertEqual(projection["native_contingency_remaining_cpu_seconds"], 180.0)
        self.assertEqual(projection["nonnative_contingency_remaining_cpu_seconds"], 60.0)


    def test_autocrlf_checkout_matches_committed_text_hash(self):
        RUNTIME_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="issue73-eng4-autocrlf-", dir=RUNTIME_TEMP_ROOT) as temp:
            repo = Path(temp)
            sample = repo / "receipt.json"
            sample.write_bytes(b'{"value":1}\n')
            subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "true"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Issue 73 test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "issue73-test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "receipt.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "fixture"], check=True)
            committed = subprocess.check_output(["git", "-C", str(repo), "show", "HEAD:receipt.json"])

            sample.unlink()
            subprocess.run(["git", "-C", str(repo), "checkout", "--", "receipt.json"], check=True)
            checkout = sample.read_bytes()

            self.assertIn(b"\r\n", checkout)
            self.assertNotIn(b"\n", checkout.replace(b"\r\n", b""))
            self.assertEqual(v9.sha256_text_file(sample), v9.sha256_bytes(committed))
            self.assertNotEqual(v9.sha256_file(sample), v9.sha256_bytes(committed))

    def test_text_hash_rejects_mixed_line_endings(self):
        RUNTIME_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="issue73-eng4-mixed-eol-", dir=RUNTIME_TEMP_ROOT) as temp:
            path = Path(temp) / "receipt.json"
            path.write_bytes(b'{"value":1}\r\n{"value":2}\n')
            with self.assertRaisesRegex(ValueError, "mixed line endings"):
                v9.sha256_text_file(path)


if __name__ == "__main__":
    unittest.main()
