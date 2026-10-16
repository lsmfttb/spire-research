"""Resume the authorized Issue 60 program with its accepted two-worker witness.

The old R3 validation receipt records a RESOURCE_BLOCKED forecast. Issue #60's
Planner decision explicitly supersedes that forecast after the 59-game cached
regime bridge passes and authorizes use of the already-reviewed two-worker
replay. This adapter supplies run_study_parallel's in-memory receipt interface
from that accepted witness without replacing or rewriting the historical R3
receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

import run_cost_bridge as bridge
import run_study_parallel as parallel

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "evidence"
PROSPECTIVE = EVIDENCE / "prospective-cost"
BINDING = PROSPECTIVE / "continuation-validation-binding.json"
LIFECYCLE = PROSPECTIVE / "continuation-lifecycle.jsonl"
AUTHORIZATION = bridge.AUTHORIZATION_COMMENT_ID


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _facts() -> dict[str, Any]:
    latest = bridge._read_json(bridge.BRIDGE_LATEST)
    gate = bridge._read_json(PROSPECTIVE / "bridge-gate.json")
    start = bridge._read_json(bridge.BRIDGE_START)
    historical = bridge._read_historical_parallel_receipt(parallel)
    accepted = bridge._verify_accepted_two_worker_evidence()
    source_hashes = bridge.producer_manifest(HERE)
    if latest.get("status") != "COMPLETE" or latest.get("completed_bridge_tag_count") != 59:
        raise bridge.CostRegimeError("the authorized 59-game bridge is not complete")
    if (gate.get("status") != "PASS" or gate.get("source_files") != source_hashes
            or gate.get("source_commit") != start.get("producer_commit")):
        raise bridge.CostRegimeError("the passing prospective gate does not bind the current producer")
    forecast = gate.get("forecast", {})
    if (forecast.get("status") != "PASS" or forecast.get("remaining_games") != 2560
            or forecast.get("observed_complete_games") != 59):
        raise bridge.CostRegimeError("the bridge forecast does not pass for the fixed remaining grid")
    bridge_gate = gate.get("bridge", {})
    if (bridge_gate.get("completed_games") != 59 or bridge_gate.get("worker_count") != 2):
        raise bridge.CostRegimeError("bridge completion or worker evidence is not the authorized population")
    evidence_path = bridge.ACCEPTED_TWO_WORKER_EVIDENCE
    evidence_sha = _sha256(evidence_path)
    if start.get("accepted_two_worker_evidence_sha256") != evidence_sha:
        raise bridge.CostRegimeError("bridge-start record no longer binds accepted two-worker evidence")
    if (historical.get("status") != "RESOURCE_BLOCKED"
            or historical.get("formal_grid_forecast_at_validation", {}).get("status") != "RESOURCE_BLOCKED"):
        raise bridge.CostRegimeError("historical R3 receipt is not the preserved blocked record")
    if historical.get("training_progress", {}).get("ppo_updates") != 0:
        raise bridge.CostRegimeError("historical R3 receipt indicates an unexpected PPO update")
    if accepted.get("status") != "PASS":
        raise bridge.CostRegimeError("Planner-accepted two-worker evidence no longer passes")

    bridge.PRODUCER_COMMIT = str(gate["source_commit"])
    bridge.PRODUCER_HASHES = source_hashes
    costs = bridge._cost_sample(bridge_only=True)
    if len(costs) != 59:
        raise bridge.CostRegimeError("prospective cost sample is not the exact 59-game bridge")

    runner_hashes = {
        relative: parallel._sha256(HERE / relative)
        for relative in parallel.PARALLEL_SOURCE_FILES
    }
    return {
        "authorization_comment_id": AUTHORIZATION,
        "producer_commit": bridge.PRODUCER_COMMIT,
        "producer_files": source_hashes,
        "bridge_latest_status": latest.get("status"),
        "completed_bridge_tag_count": latest.get("completed_bridge_tag_count"),
        "bridge_gate_sha256": _sha256(PROSPECTIVE / "bridge-gate.json"),
        "bridge_start_sha256": _sha256(bridge.BRIDGE_START),
        "accepted_two_worker_evidence_path": evidence_path.relative_to(HERE).as_posix(),
        "accepted_two_worker_evidence_sha256": evidence_sha,
        "accepted_two_worker_source_commit": accepted.get("source_commit"),
        "accepted_two_worker_seed_keys": sorted(
            int(row["seed"]) for row in accepted.get("replays", {}).get("cached_two_worker", [])
        ),
        "accepted_two_worker_dispatcher": {
            key: accepted["two_worker_dispatcher_evidence"].get(key)
            for key in ("execution_count", "max_active_workers", "max_configured_workers")
        },
        "accepted_two_worker_parity": [
            {key: row.get(key) for key in ("status", "decision_rows")}
            for row in accepted.get("two_worker_parallel_comparisons_to_cached_serial", [])
        ],
        "historical_r3_receipt_path": parallel.PARALLEL_RECEIPT.relative_to(HERE).as_posix(),
        "historical_r3_receipt_sha256": _sha256(parallel.PARALLEL_RECEIPT),
        "historical_r3_receipt_status": historical.get("status"),
        "historical_r3_validation_status": historical.get("validation", {}).get("status"),
        "execution_source_files": runner_hashes,
        "selected_worker_count": 2,
        "remaining_games": 2560,
        "forecast_projected_builder_cpu_seconds": forecast.get("projected_builder_cpu_seconds"),
        "forecast_projected_all_in_cpu_seconds": forecast.get("projected_all_in_cpu_seconds"),
        "interpretation": (
            "Use the independently Planner-accepted two-worker replay witness as the existing "
            "run_study_parallel validation interface. Preserve the historical R3 RESOURCE_BLOCKED "
            "receipt byte-for-byte; its old cost forecast is superseded only by the passing "
            "prospective bridge gate. No simulation or PPO update occurs during this check."
        ),
    }


def _write_binding(facts: dict[str, Any]) -> dict[str, Any]:
    record = {
        "schema_id": "issue60-continuation-validation-binding-v1",
        "status": "PASS",
        "validated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "launcher_sha256": _sha256(Path(__file__)),
        "facts": facts,
    }
    bridge.atomic_json(BINDING, record)
    return record


def _verified_binding() -> dict[str, Any]:
    stored = bridge._read_json(BINDING)
    if stored.get("schema_id") != "issue60-continuation-validation-binding-v1" or stored.get("status") != "PASS":
        raise bridge.CostRegimeError("continuation validation binding is missing or invalid")
    if stored.get("launcher_sha256") != _sha256(Path(__file__)):
        raise bridge.CostRegimeError("continuation launcher changed after validation")
    if stored.get("facts") != _facts():
        raise bridge.CostRegimeError("continuation validation facts changed after bridge gate")
    return stored


def _validation_receipt() -> dict[str, Any]:
    binding = _verified_binding()
    return {
        "schema_id": "issue60-in-memory-accepted-two-worker-binding-v1",
        "status": "PASS",
        "validation": {"status": "PASS", "basis": BINDING.relative_to(HERE).as_posix()},
        "source_files": binding["facts"]["execution_source_files"],
        "worker_selection": {"worker_count": 2},
        "binding_sha256": _sha256(BINDING),
    }


def _append_lifecycle(row: dict[str, Any]) -> None:
    LIFECYCLE.parent.mkdir(parents=True, exist_ok=True)
    with LIFECYCLE.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _record_bridge_charge(*, attempt: str, stage: str, seconds: float,
                          status: str, accounting: str, source_commit: str) -> None:
    ledger = EVIDENCE / "cpu-ledger.jsonl"
    existing = ledger.read_text(encoding="utf-8").splitlines() if ledger.is_file() else []
    if any(f'"attempt":"{attempt}"' in line for line in existing):
        return
    charge = max(0.0, float(seconds))
    bridge._append_ledger({
        "attempt": attempt,
        "authorization_comment_id": AUTHORIZATION,
        "stage": stage,
        "status": status,
        "process_cpu_seconds": charge,
        "source_commit": source_commit,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "accounting": accounting,
    })
    latest = bridge._read_json(bridge.BRIDGE_LATEST)
    previous = float(latest.get("bridge_cumulative_additional_cpu_seconds_charged", 0.0))
    latest["bridge_cumulative_additional_cpu_seconds_charged"] = previous + charge
    latest.setdefault("continuation_accounting", []).append({
        "attempt": attempt, "stage": stage, "status": status,
        "process_cpu_seconds": charge,
    })
    bridge.atomic_json(bridge.BRIDGE_LATEST, latest)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--continue", dest="continue_program", action="store_true")
    args = parser.parse_args()
    if args.verify_only == args.continue_program:
        parser.error("select exactly one of --verify-only or --continue")
    if args.verify_only:
        historical_before = _sha256(parallel.PARALLEL_RECEIPT)
        record = _write_binding(_facts())
        parallel._read_validation_receipt = _validation_receipt
        receipt = parallel._read_validation_receipt()
        if (receipt.get("status") != "PASS"
                or receipt.get("validation", {}).get("status") != "PASS"
                or receipt.get("worker_selection", {}).get("worker_count") != 2
                or receipt.get("source_files") != {
                    relative: parallel._sha256(HERE / relative)
                    for relative in parallel.PARALLEL_SOURCE_FILES
                }
                or _sha256(parallel.PARALLEL_RECEIPT) != historical_before):
            raise bridge.CostRegimeError("in-memory validation adapter witness failed or changed history")
        print(json.dumps({"status": record["status"], "binding": BINDING.as_posix(),
                          "sha256": _sha256(BINDING), "worker_count": 2,
                          "historical_receipt_preserved": True}, sort_keys=True))
        return 0

    binding = _verified_binding()
    if LIFECYCLE.is_file() and LIFECYCLE.read_text(encoding="utf-8").strip():
        raise bridge.CostRegimeError("continuation was already launched; inspect lifecycle before any retry")
    prior_receipt_sha = binding["facts"]["historical_r3_receipt_sha256"]
    started = time.process_time()
    _record_bridge_charge(
        attempt="issue60-prior-automatic-continuation-receipt-block-0001",
        stage="issue60-cached-cost-bridge-continuation-precondition-failure",
        seconds=0.2,
        status="FAILED",
        source_commit=binding["facts"]["producer_commit"],
        accounting=("conservative finalization charge for the automatic post-bridge invocation "
                    "blocked by the historical R3 receipt; no PPO update or formal game dispatched"),
    )
    _record_bridge_charge(
        attempt="issue60-continuation-validation-adapter-repair-0001",
        stage="issue60-cached-cost-bridge-continuation-repair-validation",
        seconds=30.0,
        status="PASS",
        source_commit=binding["facts"]["producer_commit"],
        accounting=("conservative implementation, focused binding verification and commit-preparation "
                    "charge for the study-local adapter; no simulation or PPO update"),
    )
    parallel._read_validation_receipt = _validation_receipt
    launch_charge = max(0.0, time.process_time() - started) + 0.2
    _record_bridge_charge(
        attempt="issue60-continuation-validation-adapter-launch-0001",
        stage="issue60-cached-cost-bridge-continuation-launch",
        seconds=launch_charge,
        status="PASS",
        source_commit=binding["facts"]["producer_commit"],
        accounting="continuation binding recheck, in-memory receipt installation and finalization reserve before entering the unchanged runner",
    )
    _append_lifecycle({
        "schema_id": "issue60-continuation-lifecycle-v1",
        "state": "STARTED",
        "authorization_comment_id": AUTHORIZATION,
        "binding_sha256": _sha256(BINDING),
        "historical_r3_receipt_sha256_preserved": prior_receipt_sha,
        "worker_count": 2,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    ledger = EVIDENCE / "cpu-ledger.jsonl"
    ledger_lines_before_runner = len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.is_file() else 0
    runner_started = time.process_time()
    try:
        exit_code = int(bridge._run_continue())
    except BaseException as exc:
        ledger_lines_after_runner = len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.is_file() else 0
        if ledger_lines_after_runner == ledger_lines_before_runner:
            _record_bridge_charge(
                attempt="issue60-continuation-runtime-failure-0001",
                stage="issue60-authorized-continuation-failure",
                seconds=max(0.0, time.process_time() - runner_started) + 0.2,
                status="FAILED",
                source_commit=binding["facts"]["producer_commit"],
                accounting="parent process CPU plus finalization reserve for continuation failure before any stage ledger charge",
            )
        _append_lifecycle({
            "schema_id": "issue60-continuation-lifecycle-v1",
            "state": "FAILED",
            "authorization_comment_id": AUTHORIZATION,
            "failure": f"{type(exc).__name__}: {exc}",
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        raise
    ledger_lines_after_runner = len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.is_file() else 0
    if exit_code != 0 and ledger_lines_after_runner == ledger_lines_before_runner:
        _record_bridge_charge(
            attempt="issue60-continuation-runtime-stop-0001",
            stage="issue60-authorized-continuation-stop",
            seconds=max(0.0, time.process_time() - runner_started) + 0.2,
            status="RESOURCE_BLOCKED" if exit_code == 75 else "FAILED",
            source_commit=binding["facts"]["producer_commit"],
            accounting="parent process CPU plus finalization reserve for continuation stop before any stage ledger charge",
        )
    _append_lifecycle({
        "schema_id": "issue60-continuation-lifecycle-v1",
        "state": "EXITED",
        "authorization_comment_id": AUTHORIZATION,
        "exit_code": exit_code,
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
