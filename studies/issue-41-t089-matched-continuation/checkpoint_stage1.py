#!/usr/bin/env python3
"""Periodically push durable append-only continuation progress during execution."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PROGRESS = HERE / "stage1-progress.jsonl"
STATE = HERE / "stage1-state.json"
BRANCH = "codex/issue-41-t089-stage1"
CHECKPOINT_EVERY = 8
POLL_SECONDS = 10
WINDOWS_REPO = subprocess.run(["wslpath", "-w", str(REPO)], check=True, text=True, capture_output=True).stdout.strip()


def git(*args: str) -> str:
    # Git's worktree metadata points to a Windows .git directory; use Windows
    # Git from WSL so it resolves the shared checkout's .git file correctly.
    result = subprocess.run(["git.exe", "-C", WINDOWS_REPO, *args], check=True, text=True, capture_output=True)
    return result.stdout.strip()


def process_running(pid: int) -> bool:
    cmdline = Path(f"/proc/{pid}/cmdline")
    if not cmdline.exists():
        return False
    try:
        args = cmdline.read_bytes().decode("utf-8", errors="replace").split("\0")
    except FileNotFoundError:
        return False
    return "stage1_supervisor.py" in " ".join(args) and "execute" in args


def stable_progress() -> tuple[bytes, list[dict[str, Any]]] | None:
    if not PROGRESS.exists():
        return None
    data = PROGRESS.read_bytes()
    if data and not data.endswith(b"\n"):
        return None
    try:
        rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data, rows


def checkpoint(expected_count: int) -> int:
    snapshot = stable_progress()
    if snapshot is None:
        return expected_count
    data, events = snapshot
    completions = sum(1 for row in events if row.get("event") == "continuation_completed")
    if completions < expected_count + CHECKPOINT_EVERY and completions != 336:
        return expected_count
    paths = [PROGRESS.relative_to(REPO).as_posix(), STATE.relative_to(REPO).as_posix()]
    before_state = STATE.read_bytes() if STATE.exists() else b""
    before_progress_sha = hashlib.sha256(data).hexdigest()
    git("add", "--", *paths)
    after_progress = PROGRESS.read_bytes() if PROGRESS.exists() else b""
    after_state = STATE.read_bytes() if STATE.exists() else b""
    if hashlib.sha256(after_progress).hexdigest() != before_progress_sha or after_state != before_state:
        git("reset", "-q", "HEAD", "--", *paths)
        return expected_count
    staged = set(git("diff", "--cached", "--name-only").splitlines())
    if not staged.issubset(set(paths)):
        raise RuntimeError(f"refusing checkpoint with unrelated staged paths: {sorted(staged)}")
    if staged:
        git("commit", "-m", f"Study 41: checkpoint {completions} completed continuations")
        git("push", "origin", f"HEAD:refs/heads/{BRANCH}")
    return completions


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("usage: checkpoint_stage1.py supervisor-pid", file=sys.stderr)
        return 2
    pid = int(sys.argv[1])
    if git("branch", "--show-current") != BRANCH:
        print("checkpoint branch does not match the fixed Issue 41 branch", file=sys.stderr)
        return 2
    if git("status", "--porcelain"):
        print("worktree must be clean before starting the progress checkpoint", file=sys.stderr)
        return 2
    checkpointed = 0
    while process_running(pid):
        checkpointed = checkpoint(checkpointed)
        time.sleep(POLL_SECONDS)
    checkpointed = checkpoint(0)
    print(json.dumps({"terminal": "CHECKPOINT_MONITOR_COMPLETE", "last_completed_key_count": checkpointed}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
