from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

from budget_ledger import ledger_path, sha256_jsonl


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one Issue #59 stage and durably account process CPU")
    parser.add_argument("stage")
    parser.add_argument("--ledger", type=Path, default=None)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not args.stage.strip() or not command:
        parser.error("provide a nonempty stage and command")

    ledger = ledger_path(args.ledger)

    prior = 0.0
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                prior = float(json.loads(line).get("elapsed_process_cpu_s", prior))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
    if prior >= 10199.0:
        row = {"stage": args.stage, "command": command, "exit_code": 2,
            "child_process_cpu_s": 0.0, "wrapper_process_cpu_s": 0.0,
            "accounting_margin_cpu_s": 0.0, "external_process_cpu_s": 0.0,
            "elapsed_process_cpu_s": round(prior, 6),
            "measurement": "skipped before launch: budget guard preserves reviewer reserve",
            "recorded_epoch": round(time.time(), 3), "budget_guard_skipped": True}
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if args.summary is not None and args.summary.is_file():
            summary = json.loads(args.summary.read_text(encoding="utf-8"))
            summary["builder_all_in_process_cpu_s"] = round(prior, 3)
            summary["budget_events_sha256"] = sha256_jsonl(ledger)
            temporary = args.summary.with_suffix(args.summary.suffix + ".tmp")
            temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, args.summary)
        print(json.dumps(row, sort_keys=True))
        return 2

    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    parent_started = time.process_time()
    launch_error = None
    try:
        child_env = os.environ.copy()
        child_env["ISSUE59_BUDGET_LEDGER"] = str(ledger)
        result = subprocess.run(command, check=False, env=child_env)
        exit_code = result.returncode
    except OSError as exc:
        launch_error = f"{type(exc).__name__}: {exc}"
        exit_code = 127
    parent_cpu = time.process_time() - parent_started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    margin = 1.0
    stage_cpu = max(0.0, child_cpu) + parent_cpu + margin

    total = prior + stage_cpu
    row = {"stage": args.stage, "command": command, "exit_code": exit_code,
        "child_process_cpu_s": round(child_cpu, 6), "wrapper_process_cpu_s": round(parent_cpu, 6),
        "accounting_margin_cpu_s": margin, "external_process_cpu_s": round(stage_cpu, 6),
        "elapsed_process_cpu_s": round(total, 6),
        "measurement": "resource.getrusage(RUSAGE_CHILDREN)+wrapper process_time+1s margin",
        "recorded_epoch": round(time.time(), 3)}
    if launch_error:
        row["launch_error"] = launch_error
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    if args.summary is not None and args.summary.is_file():
        summary = json.loads(args.summary.read_text(encoding="utf-8"))
        summary["builder_all_in_process_cpu_s"] = round(total, 3)
        summary["budget_events_sha256"] = sha256_jsonl(ledger)
        temporary = args.summary.with_suffix(args.summary.suffix + ".tmp")
        temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, args.summary)

    print(json.dumps(row, sort_keys=True))
    return exit_code if exit_code else (2 if total > 10200 else 0)


if __name__ == "__main__":
    raise SystemExit(main())
