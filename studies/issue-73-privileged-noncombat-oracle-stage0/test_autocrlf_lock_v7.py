"""Fresh-checkout EOL stability and tamper rejection for the v7 source lock."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import source_qualification_v7 as v7


CLI = v7.STUDY / "checkpoint_replay_qualification.py"


def test_checkout_text_digest_normalizes_only_crlf_pairs(tmp_path: Path) -> None:
    path = tmp_path / "locked.json"
    lf = b'{"schema_id":"test"}\n'
    path.write_bytes(lf)
    expected = v7.sha256_checkout_text_file(path)

    crlf = lf.replace(b"\n", b"\r\n")
    path.write_bytes(crlf)
    assert v7.sha256_checkout_text_file(path) == expected
    assert v7.sha256_file(path) != expected

    edited = crlf.replace(b'"test"', b'"edit"')
    path.write_bytes(edited)
    assert v7.sha256_checkout_text_file(path) != expected


def _verify_lock_cli() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), "--qualification-version", "v7", "verify-lock"],
        cwd=v7.REPOSITORY,
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
        timeout=45,
    )


def _change_schema_id(payload: bytes) -> bytes:
    marker = b'"schema_id":"'
    start = payload.index(marker) + len(marker)
    end = payload.index(b'"', start)
    return payload[:start] + b"tampered" + payload[end:]


def test_production_verify_lock_accepts_checkout_eol_and_rejects_content_edits() -> None:
    locked = (
        (v7.CONFIG_PATH, "v7 qualification config changed after source lock"),
        (v7.PLAN_PATH, "v7 Gate C roots/actions/streams plan changed after source lock"),
        (v7.INPUT_MANIFEST_PATH, "v7 bounded historical input manifest changed after source lock"),
    )
    for path, _failure in locked:
        assert path.is_file(), f"new repair lock artifact is missing: {path}"
    assert not (v7.V7_OUT / "attempts" / f"{v7.CAPTURE_ATTEMPT_KEY}.json").exists()

    positive = _verify_lock_cli()
    assert positive.returncode == 0, positive.stderr[-2000:]

    for path, expected_failure in locked:
        original = path.read_bytes()
        try:
            edited = _change_schema_id(original)
            assert edited != original
            path.write_bytes(edited)
            negative = _verify_lock_cli()
            output = negative.stdout + negative.stderr
            assert negative.returncode != 0, f"verify-lock accepted a changed {path.name}"
            assert expected_failure in output, output[-2000:]
        finally:
            path.write_bytes(original)
        assert path.read_bytes() == original, f"negative check did not restore {path.name} exactly"

    restored = _verify_lock_cli()
    assert restored.returncode == 0, restored.stderr[-2000:]
