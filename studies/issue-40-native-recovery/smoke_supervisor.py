#!/usr/bin/env python3
"""Enforce Issue 40 smoke wall, CPU, process-RSS, and host-available-memory bounds."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STUDY = Path(__file__).resolve().parent
PYTHON = Path("/home/lsmft/stsrl-spikes/py313-torch/bin/python")
SOURCE = Path("/home/lsmft/stsrl-spikes/STSRL-issue40-95abc/src")
BUILD = Path("/home/lsmft/stsrl-spikes/sts_lightspeed-t088-20a6/build-py")
WALL_LIMIT = 1800
CPU_LIMIT = 1800
RSS_LIMIT_BYTES = 8192 * 1024 * 1024
MEM_AVAILABLE_FLOOR_BYTES = 4096 * 1024 * 1024
POLL_SECONDS = 1
GRACE_SECONDS = 8


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_process_metrics(pid: int) -> tuple[int | None, float | None]:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
        rss = None
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
                break
        tick_total = 0
        task_root = Path(f"/proc/{pid}/task")
        for task in task_root.iterdir():
            stat = (task / "stat").read_text(encoding="ascii")
            tail = stat[stat.rfind(")") + 2 :].split()
            tick_total += int(tail[11]) + int(tail[12])
        cpu = tick_total / os.sysconf("SC_CLK_TCK")
        return rss, cpu
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
        return None, None


def mem_available_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def write_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    global WALL_LIMIT, CPU_LIMIT
    budget = json.loads((STUDY / "smoke-budget.json").read_text(encoding="utf-8"))
    prior_wall = float(budget.get("spent_wall_seconds_before_current", 0.0))
    prior_cpu = float(budget.get("spent_process_cpu_seconds_before_current", 0.0))
    WALL_LIMIT = max(1, int(float(budget["max_wall_clock_seconds"]) - prior_wall))
    CPU_LIMIT = max(1, int(float(budget["max_process_cpu_seconds"]) - prior_cpu))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(SOURCE), str(BUILD), env.get("PYTHONPATH", "")])
    console_path = STUDY / "smoke-console.log"
    supervisor_path = STUDY / "smoke-supervisor.json"
    started_at = utc_now()
    started = time.monotonic()
    stop_reason = None
    max_rss = 0
    max_cpu = 0.0
    min_available = None
    with console_path.open("w", encoding="utf-8") as console:
        child = subprocess.Popen(
            [str(PYTHON), str(STUDY / "run_smoke.py")],
            cwd=str(STUDY),
            env=env,
            stdout=console,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        while child.poll() is None:
            wall = time.monotonic() - started
            rss, cpu = read_process_metrics(child.pid)
            available = mem_available_bytes()
            if rss is not None:
                max_rss = max(max_rss, rss)
            if cpu is not None:
                max_cpu = max(max_cpu, cpu)
            if available is not None:
                min_available = available if min_available is None else min(min_available, available)
            if wall >= WALL_LIMIT:
                stop_reason = "max_wall_clock_seconds"
            elif cpu is not None and cpu >= CPU_LIMIT:
                stop_reason = "max_process_cpu_seconds"
            elif rss is not None and rss >= RSS_LIMIT_BYTES:
                stop_reason = "max_peak_rss_mib"
            elif available is not None and available < MEM_AVAILABLE_FLOOR_BYTES:
                stop_reason = "minimum_mem_available_mib"
            if stop_reason:
                os.killpg(child.pid, signal.SIGTERM)
                deadline = time.monotonic() + GRACE_SECONDS
                while child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.2)
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                break
            time.sleep(POLL_SECONDS)
        returncode = child.wait()
    if stop_reason or returncode != 0:
        state_path = STUDY / "smoke-state.json"
        prior = {}
        try:
            prior = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        if prior.get("terminal") == "SMOKE_RUNNING":
            prior["terminal"] = "COST_OR_REPLAY_BLOCKED" if stop_reason else "INCOMPLETE"
            prior["stop_detail"] = stop_reason
            prior["updated_at_utc"] = utc_now()
            write_json(state_path, prior)
        result_path = STUDY / "smoke-result.json"
        if not result_path.exists():
            write_json(
                result_path,
                {
                    "schema_id": "issue-40-continuation-smoke-v1",
                    "schema_version": 1,
                    "issue": 40,
                    "terminal": "COST_OR_REPLAY_BLOCKED" if stop_reason else "INCOMPLETE",
                    "started_at_utc": started_at,
                    "finished_at_utc": utc_now(),
                    "stage1_started": False,
                    "study_disposition": "STUDY_ONLY",
                    "complete_continuations": 0,
                    "expected_continuations": 44,
                    "maximum_authorized_continuations": 64,
                    "attempts": [],
                    "stop_detail": stop_reason or f"smoke runner exited with code {returncode}",
                },
            )
    summary = {
        "schema_id": "issue-40-smoke-supervisor-v1",
        "schema_version": 1,
        "started_at_utc": started_at,
        "finished_at_utc": utc_now(),
        "wall_seconds": round(time.monotonic() - started, 3),
        "wall_limit_seconds_current_invocation": WALL_LIMIT,
        "cpu_limit_seconds_current_invocation": CPU_LIMIT,
        "total_wall_seconds_including_prior": round(prior_wall + time.monotonic() - started, 3),
        "total_cpu_seconds_including_prior": round(prior_cpu + max_cpu, 3),
        "child_returncode": returncode,
        "stop_reason": stop_reason,
        "max_observed_process_rss_bytes": max_rss,
        "max_observed_process_cpu_seconds": round(max_cpu, 3),
        "minimum_observed_mem_available_bytes": min_available,
        "poll_interval_seconds": POLL_SECONDS,
        "resource_tool": "WSL /proc per-process and per-thread sampling",
        "stage1_started": False,
        "runner_stdout": console_path.name,
    }
    write_json(supervisor_path, summary)
    progress_attempts = 0
    progress_complete = 0
    try:
        for line in (STUDY / "smoke-progress.jsonl").read_text(encoding="utf-8").splitlines():
            event = json.loads(line).get("event")
            if event == "continuation_started":
                progress_attempts += 1
            elif event == "continuation_completed":
                progress_complete += 1
    except FileNotFoundError:
        pass
    history = list(budget.get("execution_history", []))
    history.append({
        "started_at_utc": started_at,
        "finished_at_utc": summary["finished_at_utc"],
        "wall_seconds": summary["wall_seconds"],
        "process_cpu_seconds_observed": summary["max_observed_process_cpu_seconds"],
        "peak_rss_bytes_observed": summary["max_observed_process_rss_bytes"],
        "minimum_mem_available_bytes_observed": summary["minimum_observed_mem_available_bytes"],
        "child_returncode": returncode,
        "stop_reason": stop_reason,
        "continuation_attempts_started": progress_attempts,
        "continuations_logged_complete": progress_complete,
    })
    budget["execution_history"] = history
    budget["spent_wall_seconds_before_current"] = round(prior_wall + summary["wall_seconds"], 3)
    budget["spent_process_cpu_seconds_before_current"] = round(prior_cpu + max_cpu, 3)
    budget["attempted_continuations_before_current"] = int(budget.get("attempted_continuations_before_current", 0)) + progress_attempts
    budget["status"] = "CONSUMED" if returncode == 0 else "RETRY_AUTHORIZED_WITHIN_ISSUE_BUDGET"
    write_json(STUDY / "smoke-budget.json", budget)
    print(json.dumps(summary, sort_keys=True))
    return returncode if returncode != 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())