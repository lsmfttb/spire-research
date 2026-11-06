from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from performance_provenance import capture_worktree_source, sha256_bytes, verify_committed_source


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "source-binding-test")
    (repo / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "code.py")
    _git(repo, "commit", "-qm", "initial")
    return repo


def test_source_capture_and_git_object_verification(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    binding = capture_worktree_source(repo, ("code.py",))

    verified = verify_committed_source(repo, binding, ("code.py",))

    assert verified == binding
    assert binding["clean_worktree_at_capture"] is True
    assert binding["files"][0]["worktree_sha256"] == sha256_bytes((repo / "code.py").read_bytes())
    assert binding["files"][0]["git_content_bytes"] == len(b"VALUE = 1\n")


def test_capture_rejects_dirty_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "code.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not clean"):
        capture_worktree_source(repo, ("code.py",))


def test_verifier_rejects_tampered_digest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    binding = capture_worktree_source(repo, ("code.py",))
    binding["files"][0]["sha256"] = "0" * 64

    with pytest.raises(ValueError, match="digest mismatch"):
        verify_committed_source(repo, binding, ("code.py",))


def test_verifier_rejects_unexpected_source_set(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    binding = capture_worktree_source(repo, ("code.py",))

    with pytest.raises(ValueError, match="source file set/order"):
        verify_committed_source(repo, binding, ("other.py",))
