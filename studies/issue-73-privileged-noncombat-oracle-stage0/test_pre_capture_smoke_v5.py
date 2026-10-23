"""Focused source/binary identity checks for the v5 pre-capture path."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import runner
import source_lock_integrity
import checkpoint_replay_qualification as qualification


class PreCaptureIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        qualification.PRE_CAPTURE_SMOKE_OUT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=qualification.PRE_CAPTURE_SMOKE_OUT)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_optimizer_accepts_lf_and_crlf_only(self) -> None:
        canonical = b"alpha=1\nbeta=2\n"
        expected = hashlib.sha256(canonical).hexdigest()
        for name, content in (("lf.py", canonical), ("crlf.py", canonical.replace(b"\n", b"\r\n"))):
            path = self.root / name
            path.write_bytes(content)
            self.assertEqual(runner.validate_locked_optimizer_source(path, expected_sha256=expected), expected)

    def test_optimizer_rejects_semantic_change_and_bare_cr(self) -> None:
        expected = hashlib.sha256(b"alpha=1\nbeta=2\n").hexdigest()
        for name, content in (("changed.py", b"alpha=1\nbeta=3\n"), ("bare-cr.py", b"alpha=1\rbeta=2\n")):
            path = self.root / name
            path.write_bytes(content)
            with self.subTest(name=name), self.assertRaises(runner.StudyFailure):
                runner.validate_locked_optimizer_source(path, expected_sha256=expected)

    def test_source_blob_allows_only_checkout_eol_conversion(self) -> None:
        committed = b"alpha=1\nbeta=2\n"
        expected = hashlib.sha256(committed).hexdigest()
        converted = committed.replace(b"\n", b"\r\n")
        result = qualification.verify_locked_source_payload(
            label="fixture", expected_sha256=expected,
            committed_blob=committed, checkout_bytes=converted,
        )
        self.assertEqual(result["status"], "PASS_EXACT_GIT_BLOB_WITH_EOL_ONLY_CHECKOUT_CONVERSION")
        with self.assertRaises(qualification.QualificationFailure):
            qualification.verify_locked_source_payload(
                label="fixture", expected_sha256=expected,
                committed_blob=committed, checkout_bytes=b"alpha=1\nbeta=3\n",
            )

    def test_native_binary_digest_uses_raw_bytes(self) -> None:
        payload = b"\x00native\r\nblob"
        path = self.root / "module.pyd"
        path.write_bytes(payload)
        expected = hashlib.sha256(payload).hexdigest()
        self.assertEqual(runner.validate_native_module_identity(
            path, expected_name="module.pyd", expected_sha256=expected,
        ), expected)
        normalized = hashlib.sha256(payload.replace(b"\r\n", b"\n")).hexdigest()
        with self.assertRaises(runner.StudyFailure):
            runner.validate_native_module_identity(
                path, expected_name="module.pyd", expected_sha256=normalized,
            )

    def test_wrong_adapter_is_rejected(self) -> None:
        class WrongAdapter:
            pass

        with self.assertRaises(runner.StudyFailure):
            runner.validate_light_speed_adapter_class(WrongAdapter)


if __name__ == "__main__":
    unittest.main()
