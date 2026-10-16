"""R2 budget-gated entry point; run_study.py remains byte-exact."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from resource_amendment_r1 import verify_resource_chain
from resource_amendment_r2 import (
    BUILDER_BUDGET_SECONDS, CPU_LEDGER, ResourceBudgetExceeded,
    ResourceAmendmentErrorR2, _ledger_total, _read_json, _write_json,
    complete_game_costs, enforce_budget, install_process_cpu_limit,
    remaining_optimizer_projection, seal_resource_gate,
    verify_r2_receipt, wrap_before_decision, wrap_minibatch_loss,
    write_budget_stop, write_resource_gate,
)


HERE = Path(__file__).resolve().parent
RUN_STUDY: Any = None
STAGE_START_CPU = 0.0
MINIBATCHES_COMPLETED = 0


def _append_ledger(row: dict[str, Any]) -> None:
    path = HERE / CPU_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _r2_accounted_cpu(current_stage_cpu: float = 0.0) -> float:
    return 600.0 + _ledger_total(HERE) + max(
        time.process_time() - STAGE_START_CPU, current_stage_cpu
    )


def _install_runtime_hooks() -> None:
    global MINIBATCHES_COMPLETED
    import run_study
    import trainer
    from sts_combat_rl.sim import controlled_run

    def guard(remaining_games: int | None = None,
              remaining_optimizer_cpu: float | None = None) -> dict[str, Any]:
        games = (
            max(0, 2688 - run_study._completed_run_count())
            if remaining_games is None else int(remaining_games)
        )
        optimizer = (
            run_study._remaining_optimizer_projection()
            if remaining_optimizer_cpu is None else float(remaining_optimizer_cpu)
        )
        measured = float(_read_json(HERE / "evidence" / "preflight" / "native.json")
                         ["optimizer_projection"]["measured_full_256_row_minibatch_cpu_seconds"])
        optimizer = max(0.0, optimizer - MINIBATCHES_COMPLETED * measured * 1.5)
        return enforce_budget(
            accounted_cpu=600.0 + _ledger_total(HERE),
            current_stage_cpu=max(0.0, time.process_time() - STAGE_START_CPU),
            costs=[float(row["process_cpu_seconds"]) for row in complete_game_costs(HERE)],
            remaining_games=games, remaining_optimizer_cpu=optimizer,
        )

    def budget_guard(*, remaining_games: int, remaining_optimizer_cpu: float,
                     current_stage_cpu: float) -> None:
        del current_stage_cpu
        guard(remaining_games, remaining_optimizer_cpu)

    run_study._accounted_cpu = _r2_accounted_cpu
    run_study.budget_guard = budget_guard

    original_execute = controlled_run.execute_controlled_run

    def checked_execute(*args: Any, **kwargs: Any) -> Any:
        callback = kwargs.get("before_decision")
        if callback is None:
            raise ResourceAmendmentErrorR2("controlled-run API lacks before_decision")
        kwargs["before_decision"] = wrap_before_decision(callback, lambda: guard())
        return original_execute(*args, **kwargs)

    controlled_run.execute_controlled_run = checked_execute

    original_loss = trainer.ppo_minibatch_loss

    def minibatch_complete() -> None:
        global MINIBATCHES_COMPLETED
        MINIBATCHES_COMPLETED += 1

    trainer.ppo_minibatch_loss = wrap_minibatch_loss(
        original_loss, lambda: guard(), minibatch_complete
    )


def _run_original_stage(stage: str, *, stage_start_cpu: float) -> int:
    global RUN_STUDY, STAGE_START_CPU, MINIBATCHES_COMPLETED
    STAGE_START_CPU = stage_start_cpu
    MINIBATCHES_COMPLETED = 0
    remaining = BUILDER_BUDGET_SECONDS - 600.0 - _ledger_total(HERE) - max(
        0.0, time.process_time() - stage_start_cpu
    )
    process_limit = install_process_cpu_limit(
        current_process_cpu=time.process_time(), remaining_cpu=remaining,
    )
    stage_status = HERE / "evidence" / "stage-status" / f"{stage}.json"
    run_started = time.process_time()
    status = "FAILED"
    try:
        if RUN_STUDY is None:
            import run_study
            RUN_STUDY = run_study
            _install_runtime_hooks()
        sys.argv = ["run_study.py", "--stage", stage]
        print(json.dumps({"r2_resource_guard": {
            "stage": stage, "process_cpu_limit": process_limit,
            "accounted_before_stage": 600.0 + _ledger_total(HERE),
            "remaining_allowance_at_launch": remaining,
        }}, sort_keys=True))
        result = RUN_STUDY.main()
        status = "COMPLETE" if result == 0 else "FAILED"
        return result
    except ResourceBudgetExceeded as exc:
        write_budget_stop(
            HERE, stage=stage, message=str(exc),
            process_cpu_seconds=max(0.0, time.process_time() - stage_start_cpu),
        )
        print(f"R2 resource stop: {exc}", file=sys.stderr)
        status = "RESOURCE_BLOCKED"
        return 75
    except BaseException as exc:
        if not stage_status.exists():
            _write_json(stage_status, {
                "schema_id": "issue60-stage-status-r2-v1", "stage": stage,
                "status": "FAILED", "failure": f"{type(exc).__name__}: {exc}",
                "process_cpu_seconds": max(0.0, time.process_time() - stage_start_cpu),
            })
        raise
    finally:
        runner_cpu = 0.0
        if stage_status.is_file():
            try:
                runner_cpu = float(_read_json(stage_status).get("process_cpu_seconds", 0.0))
            except (OSError, ValueError, TypeError):
                pass
        total_stage_cpu = max(0.0, time.process_time() - stage_start_cpu)
        wrapper_cpu = max(0.0, total_stage_cpu - runner_cpu)
        _append_ledger({
            "stage": f"{stage}-r2-launcher-overhead", "status": status,
            "process_cpu_seconds": wrapper_cpu,
            "process_cpu_limit": process_limit,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })


def _run_r2_receipt_stage(stage: str, *, stage_start_cpu: float) -> int:
    remaining = BUILDER_BUDGET_SECONDS - 600.0 - _ledger_total(HERE) - max(
        0.0, time.process_time() - stage_start_cpu
    )
    process_limit = install_process_cpu_limit(
        current_process_cpu=time.process_time(), remaining_cpu=remaining,
    )
    status = "FAILED"
    error = None
    try:
        if stage == "resource-r2":
            result = write_resource_gate(HERE, stage_started_cpu=stage_start_cpu)
            _write_json(HERE / "evidence" / "stage-status" / "resource-r2.json", {
                "schema_id": "issue60-stage-status-r2-v1", "stage": stage,
                "status": result["status"],
                "process_cpu_seconds": time.process_time() - stage_start_cpu,
                "decision": result["decision"],
            })
            status = result["status"]
            print(json.dumps(result, indent=2))
            return 0 if status == "PASS" else 2
        result = seal_resource_gate(HERE, stage_started_cpu=stage_start_cpu)
        _write_json(HERE / "evidence" / "stage-status" / "seal-resource-r2.json", {
            "schema_id": "issue60-stage-status-r2-v1", "stage": stage,
            "status": "COMPLETE",
            "process_cpu_seconds": time.process_time() - stage_start_cpu,
            "preseal_receipt_sha256": result["preseal_receipt_sha256"],
        })
        status = "COMPLETE"
        print(json.dumps(result, indent=2))
        return 0
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        elapsed = time.process_time() - stage_start_cpu
        if status == "FAILED":
            _write_json(HERE / "evidence" / "stage-status" / f"{stage}.json", {
                "schema_id": "issue60-stage-status-r2-v1", "stage": stage,
                "status": status, "failure": error,
                "process_cpu_seconds": elapsed,
            })
        _append_ledger({
            "stage": stage, "status": status,
            "process_cpu_seconds": elapsed,
            "process_cpu_limit": process_limit,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=(
        "resource-r2", "seal-resource-r2", "train", "evaluate", "analyze", "continue",
    ), required=True)
    stage = parser.parse_args().stage
    stage_start = time.process_time()
    verify_resource_chain(HERE, require_gate_seal=True)
    if stage == "resource-r2":
        return _run_r2_receipt_stage(stage, stage_start_cpu=stage_start)
    if stage == "seal-resource-r2":
        verify_r2_receipt(HERE, require_seal=False)
        return _run_r2_receipt_stage(stage, stage_start_cpu=stage_start)
    verify_r2_receipt(HERE, require_seal=True)
    if stage == "continue":
        for current in ("train", "evaluate", "analyze"):
            code = _run_original_stage(current, stage_start_cpu=time.process_time())
            if code != 0:
                return code
        return 0
    return _run_original_stage(stage, stage_start_cpu=stage_start)


if __name__ == "__main__":
    raise SystemExit(main())
