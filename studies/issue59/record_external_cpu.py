from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import time

from budget_ledger import ledger_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Append an Issue #59 process-CPU budget allocation")
    parser.add_argument("stage")
    parser.add_argument("cpu_seconds", type=float)
    parser.add_argument("--ledger", type=Path, default=None)
    parser.add_argument("--measurement", default="caller-supplied CPU allocation; not measured by this utility")
    parser.add_argument("--conservative", action="store_true")
    args = parser.parse_args()
    if not args.stage.strip() or not math.isfinite(args.cpu_seconds) or args.cpu_seconds < 0:
        parser.error("stage must be nonempty and CPU seconds finite/nonnegative")
    ledger = ledger_path(args.ledger)
    prior = 0.0
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                prior = float(json.loads(line).get("elapsed_process_cpu_s", prior))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
    total = prior + args.cpu_seconds
    row = {"stage": args.stage, "external_process_cpu_s": round(args.cpu_seconds, 6),
        "elapsed_process_cpu_s": round(total, 6), "measurement": args.measurement,
        "conservative_allocation": args.conservative, "recorded_utc_epoch": round(time.time(), 3)}
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps(row, sort_keys=True))
    return 2 if total > 10200 else 0


if __name__ == "__main__":
    raise SystemExit(main())
