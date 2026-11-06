from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence


MEASUREMENT_SOURCE_SCHEMA = "issue85-performance-measurement-source-v1"
MEASUREMENT_SOURCE_FILES = (
    "studies/issue-85-representation/compare_performance.py",
    "studies/issue-85-representation/performance_harness.py",
    "studies/issue-85-representation/performance_provenance.py",
    "studies/issue-85-representation/pilot_runner.py",
    "studies/issue-85-representation/representations.py",
    "studies/issue-85-representation/runtime_integration.py",
    "studies/issue-85-representation/source-lock.json",
)


def _git_environment(repo_root: Path) -> dict[str, str]:
    """Return Git's worktree environment, translating Windows worktree pointers for WSL."""
    if os.name == "nt":
        return os.environ.copy()
    marker = repo_root / ".git"
    if marker.is_dir():
        git_dir = marker.resolve()
    elif marker.is_file():
        text = marker.read_text(encoding="utf-8").strip()
        if not text.lower().startswith("gitdir:"):
            raise ValueError(f"unsupported .git pointer in {repo_root}")
        raw = text.split(":", 1)[1].strip()
        match = re.fullmatch(r"([A-Za-z]):[\\/](.*)", raw)
        if match:
            git_dir = Path(f"/mnt/{match.group(1).lower()}/{match.group(2).replace(chr(92), '/')}")
        else:
            git_dir = (repo_root / raw).resolve()
    else:
        raise ValueError(f"missing Git metadata for {repo_root}")
    env = os.environ.copy()
    env["GIT_DIR"] = str(git_dir)
    env["GIT_WORK_TREE"] = str(repo_root.resolve())
    return env


def _windows_object_directory_from_wsl_alternate(
    repo_root: Path,
    *,
    platform_name: str = os.name,
) -> str | None:
    """Map one WSL-mounted alternate object directory for Windows Git reads.

    Some durable runtime snapshots were created by WSL Git and retain an
    ``objects/info/alternates`` entry such as ``/mnt/d/...``. Windows Git
    cannot resolve that entry, although the same object database is available
    at ``D:/...``. The caller uses this only for read-only identity/status
    checks, leaving the snapshot's refs, index, and worktree as the objects
    being verified.
    """
    if platform_name != "nt":
        return None
    marker = repo_root / ".git"
    if not marker.is_dir():
        return None
    alternates = marker / "objects" / "info" / "alternates"
    if not alternates.is_file():
        return None
    entries = [line.strip() for line in alternates.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(entries) != 1:
        return None
    match = re.fullmatch(r"/mnt/([A-Za-z])/(.+)", entries[0])
    if not match:
        return None
    return f"{match.group(1).upper()}:/{match.group(2)}"


def _git(repo_root: Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        env=_git_environment(repo_root),
    )
    return result.stdout if binary else result.stdout.decode("utf-8").strip()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_source(value: bytes) -> bytes:
    return value.replace(b"\r\n", b"\n")


def capture_worktree_source(
    repo_root: Path,
    paths: Sequence[str] = MEASUREMENT_SOURCE_FILES,
) -> dict[str, Any]:
    """Capture exact committed source bytes, requiring a clean worktree at capture time."""
    repo_root = repo_root.resolve(strict=True)
    status = str(_git(repo_root, "status", "--porcelain", "--untracked-files=all"))
    if status:
        raise ValueError(f"measurement worktree is not clean: {status[:500]}")
    commit = str(_git(repo_root, "rev-parse", "HEAD"))
    tree = str(_git(repo_root, "rev-parse", "HEAD^{tree}"))
    records: list[dict[str, Any]] = []
    normalized = tuple(sorted(set(paths)))
    if len(normalized) != len(paths):
        raise ValueError("measurement source paths must be unique")
    for relative in normalized:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"source path escapes repository: {relative}")
        source = repo_root / relative_path
        content = source.read_bytes()
        committed = bytes(_git(repo_root, "show", f"{commit}:{relative}", binary=True))
        if _normalized_source(content) != _normalized_source(committed):
            raise ValueError(f"working source differs from committed bytes: {relative}")
        blob = str(_git(repo_root, "rev-parse", f"{commit}:{relative}"))
        records.append({
            "path": relative,
            "git_blob_sha1": blob,
            "worktree_sha256": sha256_bytes(content),
            "git_content_sha256": sha256_bytes(committed),
            "bytes": len(content),
            "git_content_bytes": len(committed),
        })
    return {
        "schema_id": MEASUREMENT_SOURCE_SCHEMA,
        "commit": commit,
        "tree": tree,
        "clean_worktree_at_capture": True,
        "files": records,
    }


def verify_committed_source(
    repo_root: Path,
    binding: dict[str, Any],
    required_paths: Sequence[str] = MEASUREMENT_SOURCE_FILES,
) -> dict[str, Any]:
    """Verify a saved source binding against Git objects, independent of current checkout state."""
    if not isinstance(binding, dict) or binding.get("schema_id") != MEASUREMENT_SOURCE_SCHEMA:
        raise ValueError("unsupported or missing measurement source binding")
    if binding.get("clean_worktree_at_capture") is not True:
        raise ValueError("measurement source was not captured from a clean worktree")
    commit = binding.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("measurement source commit is not a full Git SHA")
    tree = str(_git(repo_root, "rev-parse", f"{commit}^{{tree}}"))
    if binding.get("tree") != tree:
        raise ValueError("measurement source tree does not match the committed tree")
    given_files = binding.get("files")
    if not isinstance(given_files, list):
        raise ValueError("measurement source file list is missing")
    paths = [row.get("path") if isinstance(row, dict) else None for row in given_files]
    if paths != sorted(set(required_paths)):
        raise ValueError("measurement source file set/order is incomplete or unexpected")
    verified_files: list[dict[str, Any]] = []
    for row in given_files:
        relative = row["path"]
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"source path escapes repository: {relative}")
        content = bytes(_git(repo_root, "show", f"{commit}:{relative}", binary=True))
        blob = str(_git(repo_root, "rev-parse", f"{commit}:{relative}"))
        worktree_path = repo_root / relative_path
        worktree_content = worktree_path.read_bytes() if worktree_path.is_file() else None
        if worktree_content is None or _normalized_source(worktree_content) != _normalized_source(content):
            raise ValueError(f"checked-out source does not match the bound commit: {relative}")
        expected = {
            "path": relative,
            "git_blob_sha1": blob,
            "worktree_sha256": sha256_bytes(worktree_content),
            "git_content_sha256": sha256_bytes(content),
            "bytes": len(worktree_content),
            "git_content_bytes": len(content),
        }
        if row != expected:
            raise ValueError(f"measurement source file digest mismatch: {relative}")
        verified_files.append(expected)
    return {
        "schema_id": MEASUREMENT_SOURCE_SCHEMA,
        "commit": commit,
        "tree": tree,
        "clean_worktree_at_capture": True,
        "files": verified_files,
    }
