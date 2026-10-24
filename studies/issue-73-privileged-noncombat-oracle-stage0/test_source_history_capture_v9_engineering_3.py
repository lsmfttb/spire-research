from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import source_qualification_v9_engineering_3 as v9


class SourceHistoryCaptureV9Engineering3Tests(unittest.TestCase):
    def test_autocrlf_checkout_matches_committed_text_hash(self):
        v9.RUNTIME_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="issue73-autocrlf-", dir=v9.RUNTIME_TEMP_ROOT) as temp:
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
            self.assertEqual(v9.sha256_text_file(sample), v9.sha256_bytes(committed))
            self.assertNotEqual(v9.sha256_file(sample), v9.sha256_bytes(committed))

    def test_text_hash_rejects_substantive_json_change(self):
        v9.RUNTIME_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="issue73-content-change-", dir=v9.RUNTIME_TEMP_ROOT) as temp:
            path = Path(temp) / "receipt.json"
            committed = b'{"value":1}\n'
            path.write_bytes(committed.replace(b"\n", b"\r\n"))
            expected = v9.sha256_bytes(committed)
            self.assertEqual(v9.sha256_text_file(path), expected)
            path.write_bytes(b'{"value":2}\r\n')
            self.assertNotEqual(v9.sha256_text_file(path), expected)

    def test_text_hash_does_not_erase_bare_carriage_returns(self):
        v9.RUNTIME_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="issue73-bare-cr-", dir=v9.RUNTIME_TEMP_ROOT) as temp:
            path = Path(temp) / "receipt.json"
            committed = b'{"value":1}\n'
            path.write_bytes(b'{"value":1}\r\n\r')
            self.assertNotEqual(v9.sha256_text_file(path), v9.sha256_bytes(committed))

    def test_parent_capture_receipt_hashes_verify_on_crlf_checkout(self):
        report = json.loads(v9.ENGINEERING_1_CAPTURE_REPORT_PATH.read_text(encoding="utf-8"))
        self.assertNotEqual(v9.sha256_file(v9.ENGINEERING_1_CAPTURE_HISTORY_PATH), report["history_sha256"])
        self.assertNotEqual(v9.sha256_file(v9.ENGINEERING_1_CAPTURE_HISTORY_PATH), v9.sha256_text_file(v9.ENGINEERING_1_CAPTURE_HISTORY_PATH))
        self.assertEqual(v9.sha256_text_file(v9.ENGINEERING_1_CAPTURE_HISTORY_PATH), report["history_sha256"])
        self.assertNotEqual(v9.sha256_file(v9.ENGINEERING_1_CAPTURE_ROOTS_PATH), report["root_snapshots_sha256"])
        self.assertEqual(v9.sha256_text_file(v9.ENGINEERING_1_CAPTURE_ROOTS_PATH), report["root_snapshots_sha256"])
        self.assertEqual(v9._base_prior_v9_ledger()["schema_id"], "issue73-v9-resource-ledger-v1")


if __name__ == "__main__":
    unittest.main()
