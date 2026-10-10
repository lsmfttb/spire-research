#!/usr/bin/env python3
"""External Linux process-tree watchdog for the bounded Stage-1 run."""
from __future__ import annotations

import json
import hashlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RUNNER = HERE / "run_stage1.py"
PLAN_BUILDER = HERE / "build_stage1_plan.py"
BUDGET = HERE / "stage1-budget.json"
PROGRESS = HERE / "stage1-progress.jsonl"
STATE = HERE / "stage1-state.json"
RESULT = HERE / "stage1-result.json"
LOG = HERE / "stage1-supervisor.jsonl"
RESOURCE_REVIEW = HERE / "stage1-resource-review.json"
MAX_WALL = 12000.0
MAX_CPU = 12000.0
MAX_RSS = 8192 * 1024 * 1024
MIN_AVAILABLE = 4096 * 1024 * 1024
MAX_ATTEMPTS = 512
HISTORICAL_STARTS = 45
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write(path: Path, value: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def append(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def budget_doc() -> dict[str, Any]:
    if not BUDGET.exists():
        return {
            "schema_id": "issue-41-stage1-budget-v1",
            "issue": 41,
            "stage1_max_wall_seconds": MAX_WALL,
            "stage1_max_process_cpu_seconds": MAX_CPU,
            "max_total_attempts_including_issue40": MAX_ATTEMPTS,
            "issue40_prior_attempt_starts": HISTORICAL_STARTS,
            "issue40_prior_wall_seconds": 776.518,
            "issue40_prior_process_cpu_seconds": 718.170,
            "issue40_prior_peak_rss_mib": 1206.3,
            "issue40_prior_min_available_memory_mib": 19804.3,
            "execution_history": [],
            "continuation_attempts_started": 0,
        }
    return json.loads(BUDGET.read_text(encoding="utf-8"))


def progress_start_count() -> int:
    if not PROGRESS.exists():
        return 0
    return sum(1 for line in PROGRESS.read_text(encoding="utf-8").splitlines() if line and json.loads(line).get("event") == "continuation_started")


def resource_summary(doc: dict[str, Any]) -> dict[str, Any]:
    history = doc.get("execution_history", [])
    return {
        "stage1_wall_seconds": round(sum(float(x.get("wall_seconds", 0)) for x in history), 3),
        "stage1_process_cpu_seconds": round(sum(float(x.get("process_cpu_seconds_observed", 0)) for x in history), 3),
        "peak_process_tree_rss_mib": round(max((int(x.get("peak_process_tree_rss_bytes_observed", 0)) for x in history), default=0) / (1024 * 1024), 2),
        "peak_single_process_rss_mib": round(max((int(x.get("peak_single_process_rss_bytes_observed", 0)) for x in history), default=0) / (1024 * 1024), 2),
        "minimum_available_memory_mib": round(min((int(x["minimum_mem_available_bytes_observed"]) for x in history if x.get("minimum_mem_available_bytes_observed") is not None), default=0) / (1024 * 1024), 2),
        "stage1_attempts_started": progress_start_count(),
        "total_attempts_including_issue40": HISTORICAL_STARTS + progress_start_count(),
        "max_total_attempts": MAX_ATTEMPTS,
        "max_steps": 500,
    }


def process_group_sample(pgid: int) -> dict[str, Any]:
    cpu_ticks = 0
    rss_bytes = 0
    max_process_rss = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="ascii")
            tail = stat[stat.rfind(")") + 2 :].split()
            if int(tail[2]) != pgid:
                continue
            cpu_ticks += int(tail[11]) + int(tail[12])
            rss = max(0, int(tail[21])) * PAGE_SIZE
            rss_bytes += rss
            max_process_rss = max(max_process_rss, rss)
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
            continue
    available = None
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            available = int(line.split()[1]) * 1024
            break
    return {"process_group_cpu_seconds": cpu_ticks / CLK_TCK, "process_group_rss_bytes": rss_bytes, "max_single_process_rss_bytes": max_process_rss, "mem_available_bytes": available}


def terminate_group(proc: subprocess.Popen[Any], grace_seconds: float = 20.0) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def run_child(mode: str, command: list[str], environment: dict[str, str], doc: dict[str, Any]) -> dict[str, Any]:
    history = doc["execution_history"]
    prior_wall = sum(float(item.get("wall_seconds", 0)) for item in history)
    prior_cpu = sum(float(item.get("process_cpu_seconds_observed", 0)) for item in history)
    if prior_wall >= MAX_WALL or prior_cpu >= MAX_CPU:
        raise RuntimeError("stage-wide resource budget is already exhausted")
    before = process_group_sample(0)
    if before["mem_available_bytes"] is not None and before["mem_available_bytes"] < MIN_AVAILABLE:
        raise RuntimeError("minimum available memory gate failed before launch")
    log_path = HERE / f"stage1-{mode}.log"
    started = time.monotonic()
    max_group_rss = 0
    max_single_rss = 0
    max_cpu = 0.0
    min_mem = before["mem_available_bytes"]
    stop_reason = None
    started_utc = now()
    with log_path.open("ab") as output:
        output.write((f"\n[{now()}] START {mode}: {command!r}\n").encode())
        output.flush()
        proc = subprocess.Popen(command, cwd=REPO, env=environment, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        while proc.poll() is None:
            current = process_group_sample(proc.pid)
            elapsed = time.monotonic() - started
            max_cpu = max(max_cpu, current["process_group_cpu_seconds"])
            max_group_rss = max(max_group_rss, current["process_group_rss_bytes"])
            max_single_rss = max(max_single_rss, current["max_single_process_rss_bytes"])
            if current["mem_available_bytes"] is not None:
                min_mem = current["mem_available_bytes"] if min_mem is None else min(min_mem, current["mem_available_bytes"])
            if prior_wall + elapsed >= MAX_WALL:
                stop_reason = "max_wall_clock_seconds"
            elif prior_cpu + max_cpu >= MAX_CPU:
                stop_reason = "max_process_cpu_seconds"
            elif max_group_rss >= MAX_RSS:
                stop_reason = "max_process_tree_rss_bytes"
            elif current["mem_available_bytes"] is not None and current["mem_available_bytes"] < MIN_AVAILABLE:
                stop_reason = "minimum_mem_available_bytes"
            if stop_reason:
                terminate_group(proc)
                break
            time.sleep(0.5)
        return_code = proc.wait()
        elapsed = time.monotonic() - started
        tail = process_group_sample(proc.pid)
        max_cpu = max(max_cpu, tail["process_group_cpu_seconds"])
        max_group_rss = max(max_group_rss, tail["process_group_rss_bytes"])
        max_single_rss = max(max_single_rss, tail["max_single_process_rss_bytes"])
        if tail["mem_available_bytes"] is not None:
            min_mem = tail["mem_available_bytes"] if min_mem is None else min(min_mem, tail["mem_available_bytes"])
        output.write((f"\n[{now()}] END {mode} return_code={return_code} stop_reason={stop_reason}\n").encode())
        output.flush()
        os.fsync(output.fileno())
    return {
        "mode": mode,
        "command": command,
        "started_at_utc": started_utc,
        "wall_seconds": round(elapsed, 3),
        "process_cpu_seconds_observed": round(max_cpu, 3),
        "peak_process_tree_rss_bytes_observed": max_group_rss,
        "peak_single_process_rss_bytes_observed": max_single_rss,
        "minimum_mem_available_bytes_observed": min_mem,
        "return_code": return_code,
        "stop_reason": stop_reason,
        "log": log_path.name,
    }


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def main() -> int:
    if os.name != "posix" or not Path("/proc").is_dir():
        print("COST_OR_REPLAY_BLOCKED: external supervisor requires Linux /proc", file=sys.stderr)
        return 2
    if len(sys.argv) != 2 or sys.argv[1] not in {"preflight", "review", "execute"}:
        print("usage: stage1_supervisor.py preflight|review|execute", file=sys.stderr)
        return 2
    mode = sys.argv[1]
    doc = budget_doc()
    if doc.get("schema_id") != "issue-41-stage1-budget-v1" or doc.get("max_total_attempts_including_issue40") != MAX_ATTEMPTS:
        print("COST_OR_REPLAY_BLOCKED: budget ledger identity mismatch", file=sys.stderr)
        return 2
    if mode == "preflight" and read_json(STATE).get("stage1_started") is True:
        print("COST_OR_REPLAY_BLOCKED: cannot overwrite a started Stage 1 preflight", file=sys.stderr)
        return 2
    if mode in {"review", "execute"}:
        pf = read_json(HERE / "stage1-preflight.json")
        if pf.get("terminal") != "PREFLIGHT_PASS_STAGE1_READY" or pf.get("stage1_started") is not False:
            print("COST_OR_REPLAY_BLOCKED: runtime preflight has not passed", file=sys.stderr)
            return 2
        if read_json(STATE).get("stage1_started") is True:
            print("COST_OR_REPLAY_BLOCKED: observed-resource review cannot restart a started Stage 1", file=sys.stderr)
            return 2
    if mode == "execute":
        review = read_json(RESOURCE_REVIEW)
        if review.get("terminal") != "RESOURCE_PROJECTION_PASS" or review.get("plan_sha256") != hashlib.sha256((HERE / "stage1-plan.json").read_bytes()).hexdigest():
            print("COST_OR_REPLAY_BLOCKED: observed-resource projection review has not passed for this plan", file=sys.stderr)
            return 2
        if read_json(RESULT).get("terminal") in {"STAGE1_COMPLETE_REVIEWER_PENDING", "CONTINUATION_POLICY_MISMATCH_SUPPORTED", "CONTINUATION_POLICY_SIGNAL_SURVIVES", "MECHANISM_INCONCLUSIVE"}:
            print("COST_OR_REPLAY_BLOCKED: completed Stage 1 cannot be rerun", file=sys.stderr)
            return 2
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    children = []
    if mode == "preflight":
        children.append(("plan", [sys.executable, str(PLAN_BUILDER)], {}))
        children.append(("preflight", [sys.executable, str(RUNNER)], {"SPIRE_STAGE1_PREFLIGHT_ONLY": "1"}))
    elif mode == "execute":
        children.append(("execute", [sys.executable, str(RUNNER)], {}))
    for child_mode, command, extra_env in children:
        environment.update(extra_env)
        try:
            record = run_child(child_mode, command, environment, doc)
        except Exception as exc:
            record = {"mode": child_mode, "command": command, "started_at_utc": now(), "wall_seconds": 0.0, "process_cpu_seconds_observed": 0.0, "peak_process_tree_rss_bytes_observed": 0, "peak_single_process_rss_bytes_observed": 0, "minimum_mem_available_bytes_observed": None, "return_code": 2, "stop_reason": f"{type(exc).__name__}: {exc}", "log": None}
            doc["execution_history"].append(record)
            doc["continuation_attempts_started"] = progress_start_count()
            doc["updated_at_utc"] = now()
            atomic_write(BUDGET, doc)
            append(LOG, record)
            previous = read_json(STATE)
            started = bool(previous.get("stage1_started", False))
            terminal = "COST_OR_REPLAY_BLOCKED"
            atomic_write(STATE, {**previous, "schema_id": "issue-41-stage1-state-v1", "schema_version": 1, "issue": 41, "terminal": terminal, "stage1_started": started, "updated_at_utc": now(), "stop_detail": record["stop_reason"], "attempts_used_including_issue40": HISTORICAL_STARTS + progress_start_count()})
            if mode == "preflight" and not started:
                atomic_write(HERE / "stage1-preflight.json", {"schema_id": "issue-41-stage1-preflight-v1", "schema_version": 1, "issue": 41, "terminal": terminal, "stage1_started": False, "stop_detail": record["stop_reason"]})
            elif started:
                atomic_write(RESULT, {"schema_id": "issue-41-t089-matched-continuation-result-v1", "schema_version": 1, "issue": 41, "terminal": terminal, "study_disposition": "STUDY_ONLY", "stage1_started": True, "finished_at_utc": now(), "stop_detail": record["stop_reason"], "hypothesis_result_reported": False})
            print(f"COST_OR_REPLAY_BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        doc["execution_history"].append(record)
        doc["continuation_attempts_started"] = progress_start_count()
        doc["updated_at_utc"] = now()
        atomic_write(BUDGET, doc)
        append(LOG, record)
        print(json.dumps(record, sort_keys=True))
        if record["stop_reason"]:
            prev = read_json(STATE)
            started = bool(prev.get("stage1_started", False))
            terminal = "COST_OR_REPLAY_BLOCKED"
            atomic_write(STATE, {**prev, "schema_id": "issue-41-stage1-state-v1", "schema_version": 1, "issue": 41, "terminal": terminal, "stage1_started": started, "updated_at_utc": now(), "stop_detail": record["stop_reason"], "attempts_used_including_issue40": HISTORICAL_STARTS + progress_start_count()})
            if started:
                existing = read_json(RESULT)
                if not existing or existing.get("terminal") not in {"INCOMPLETE", "COST_OR_REPLAY_BLOCKED"}:
                    atomic_write(RESULT, {"schema_id": "issue-41-t089-matched-continuation-result-v1", "schema_version": 1, "issue": 41, "terminal": terminal, "study_disposition": "STUDY_ONLY", "stage1_started": True, "finished_at_utc": now(), "stop_detail": record["stop_reason"], "hypothesis_result_reported": False})
                current_result = read_json(RESULT)
                current_result["runtime"] = resource_summary(doc)
                atomic_write(RESULT, current_result)
                state_doc = read_json(STATE)
                state_doc["attempts_used_including_issue40"] = HISTORICAL_STARTS + progress_start_count()
                state_doc["result_sha256"] = hashlib.sha256(RESULT.read_bytes()).hexdigest()
                atomic_write(STATE, state_doc)
            return 2
        if child_mode == "execute" and RESULT.exists():
            current_result = read_json(RESULT)
            current_result["runtime"] = resource_summary(doc)
            atomic_write(RESULT, current_result)
            state_doc = read_json(STATE)
            state_doc["attempts_used_including_issue40"] = HISTORICAL_STARTS + progress_start_count()
            state_doc["result_sha256"] = hashlib.sha256(RESULT.read_bytes()).hexdigest()
            atomic_write(STATE, state_doc)
        if record["return_code"] != 0:
            return record["return_code"]
        if child_mode == "preflight":
            pf = read_json(HERE / "stage1-preflight.json")
            if pf.get("terminal") != "PREFLIGHT_PASS_STAGE1_READY" or pf.get("root_decisions_verified") != 64:
                return 2
    if mode in {"preflight", "review"}:
        plan_doc = read_json(HERE / "stage1-plan.json")
        successful = [x for x in doc["execution_history"] if x.get("mode") == "preflight" and x.get("return_code") == 0 and not x.get("stop_reason")]
        if not successful:
            print("COST_OR_REPLAY_BLOCKED: successful complete-root preflight is absent", file=sys.stderr)
            return 2
        root_preflight = successful[-1]
        completed_keys = int(plan_doc["execution"]["new_unique_continuations"])
        old_cost = plan_doc["resource_projection"]["smoke_observed"]
        actual_wall = sum(float(x.get("wall_seconds", 0)) for x in doc["execution_history"])
        actual_cpu = sum(float(x.get("process_cpu_seconds_observed", 0)) for x in doc["execution_history"])
        upper_remaining_wall = float(root_preflight["wall_seconds"]) + completed_keys * float(old_cost["wall_seconds_cumulative"]) / 44
        upper_remaining_cpu = float(root_preflight["process_cpu_seconds_observed"]) + completed_keys * float(old_cost["process_cpu_seconds_cumulative"]) / 44
        typical_remaining_wall = float(root_preflight["wall_seconds"]) + completed_keys * float(old_cost["mean_completed_row_wall_seconds"])
        typical_remaining_cpu = float(root_preflight["process_cpu_seconds_observed"]) + completed_keys * float(old_cost["mean_completed_row_cpu_seconds"])
        peak_rss = max((int(x.get("peak_process_tree_rss_bytes_observed", 0)) for x in doc["execution_history"]), default=0)
        min_mem = min((int(x["minimum_mem_available_bytes_observed"]) for x in doc["execution_history"] if x.get("minimum_mem_available_bytes_observed") is not None), default=0)
        resource_review = {
            "schema_id": "issue-41-observed-resource-review-v1",
            "issue": 41,
            "terminal": "RESOURCE_PROJECTION_PASS" if actual_wall + upper_remaining_wall <= MAX_WALL and actual_cpu + upper_remaining_cpu <= MAX_CPU and HISTORICAL_STARTS + progress_start_count() + completed_keys <= MAX_ATTEMPTS and peak_rss <= 8192 * 1024 * 1024 and min_mem >= MIN_AVAILABLE else "COST_OR_REPLAY_BLOCKED",
            "stage1_started": False,
            "plan_sha256": hashlib.sha256((HERE / "stage1-plan.json").read_bytes()).hexdigest(),
            "additional_caps_seconds": {"wall": MAX_WALL, "process_cpu": MAX_CPU},
            "actual_plan_and_preflight_history": {"wall_seconds": round(actual_wall, 3), "process_cpu_seconds": round(actual_cpu, 3)},
            "observed_memory": {"peak_process_tree_rss_bytes": peak_rss, "minimum_available_memory_bytes": min_mem, "max_process_tree_rss_bytes": 8192 * 1024 * 1024, "min_available_memory_bytes": MIN_AVAILABLE},
            "remaining_unique_continuations": completed_keys,
            "total_attempts_if_no_retry_including_issue40": HISTORICAL_STARTS + progress_start_count() + completed_keys,
            "attempt_cap": MAX_ATTEMPTS,
            "issue40_observed_cost_basis": {"wall_seconds_cumulative": old_cost["wall_seconds_cumulative"], "process_cpu_seconds_cumulative": old_cost["process_cpu_seconds_cumulative"], "completed_rows": 44, "mean_successful_row_wall_seconds": old_cost["mean_completed_row_wall_seconds"], "mean_successful_row_cpu_seconds": old_cost["mean_completed_row_cpu_seconds"]},
            "latest_64_root_preflight": {"wall_seconds": root_preflight["wall_seconds"], "process_cpu_seconds": root_preflight["process_cpu_seconds_observed"]},
            "upper_projection_from_start": {"wall_seconds": round(actual_wall + upper_remaining_wall, 3), "process_cpu_seconds": round(actual_cpu + upper_remaining_cpu, 3), "method": "actual completed plan/preflight invocations plus one observed 64-root preflight and all 336 new keys charged the full Issue 40 cumulative cost per completed row"},
            "typical_projection_from_start": {"wall_seconds": round(actual_wall + typical_remaining_wall, 3), "process_cpu_seconds": round(actual_cpu + typical_remaining_cpu, 3), "method": "actual completed plan/preflight invocations plus one observed 64-root preflight and all 336 new keys charged the Issue 40 successful-row mean"},
        }
        atomic_write(RESOURCE_REVIEW, resource_review)
        atomic_write(STATE, {"schema_id": "issue-41-stage1-state-v1", "schema_version": 1, "issue": 41, "terminal": "PREFLIGHT_PASS_STAGE1_READY", "stage1_started": False, "plan_sha256": resource_review["plan_sha256"], "updated_at_utc": now(), "attempts_used_including_issue40": HISTORICAL_STARTS + progress_start_count()})
        atomic_write(RESULT, {"schema_id": "issue-41-t089-matched-continuation-result-v1", "schema_version": 1, "issue": 41, "terminal": "PREFLIGHT_PASS_STAGE1_READY", "study_disposition": "STUDY_ONLY", "stage1_started": False, "finished_at_utc": now(), "stop_detail": None, "hypothesis_result_reported": False})
        print(json.dumps(resource_review, sort_keys=True))
        if resource_review["terminal"] != "RESOURCE_PROJECTION_PASS":
            return 2
    if mode == "execute":
        result = read_json(RESULT)
        if not result:
            print("INCOMPLETE: execution ended without a durable result", file=sys.stderr)
            return 2
        print(json.dumps({"terminal": result.get("terminal"), "stage1_started": result.get("stage1_started"), "result": RESULT.name}, sort_keys=True))
        if result.get("terminal") == "STAGE1_COMPLETE_REVIEWER_PENDING" or result.get("terminal") in {"CONTINUATION_POLICY_MISMATCH_SUPPORTED", "CONTINUATION_POLICY_SIGNAL_SURVIVES", "MECHANISM_INCONCLUSIVE"}:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
