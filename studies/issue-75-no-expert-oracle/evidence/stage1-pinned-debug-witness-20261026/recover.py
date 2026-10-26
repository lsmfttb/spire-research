"""Reconstruct the completed debug witness from immutable attempt receipts.

This only validates and summarizes artifacts already written by one native run;
it never imports the simulator or repeats a trajectory.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os

BASE = Path(__file__).resolve().parent
RUN = BASE / "retry1"
NATIVE = RUN / "native"
EXPECTED_PACKAGE = "2060cb15c072c45a7f80e1ec061c3266f8c4b6f4"
EXPECTED_LOCK = "9e6a3583a00031553927f1e09e4b61c1e1ea89d709ee8ebd82adbee01542f1ff"
EXPECTED_ROOT = "root-173730001-0007"
EXPECTED_OWNER_SEED = 173730001
EXPECTED_NATIVE = "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2"
EXPECTED_STSR = "7d172f7c24c9d29429b6946ba44f6b59760b0d96"
EXPECTED_MODULE_SHA = "bb22332a4a7108e8f9691482df2624b3b132d75ca07e1aa7b394b932193e5cad"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_once(path: Path, value) -> None:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    recovery_path = BASE / "recovery.json"
    if recovery_path.exists():
        raise FileExistsError("refusing to replace an existing recovery receipt")

    prior = load(BASE / "receipt.json")
    run_receipt_path = RUN / "receipt.json"
    run_receipt_bytes = run_receipt_path.read_bytes()
    run_receipt = json.loads(run_receipt_bytes)
    worker_failure = load(NATIVE / "worker-failure.json")
    prior_stdout = (BASE / "watchdog.stdout").read_text(encoding="utf-8")
    run_stdout_path = RUN / "watchdog.stdout"
    run_stdout = run_stdout_path.read_text(encoding="utf-8")
    if (
        prior.get("status") != "CHILD_EXITED_NONZERO"
        or prior.get("worker_result", {}).get("status") != "WITNESS_FAILED"
        or prior.get("worker_result", {}).get("stage1_ledger_opened") is not False
        or prior.get("registered_stage1_preflight_rows_executed") != 0
        or prior.get("registered_stage1_formal_rows_executed") != 0
        or prior.get("stage1_registered_seed_consumption") != 0
        or "witness requires the source-locked preparation-only package" not in prior_stdout
    ):
        raise ValueError("initial pre-native setup failure receipt does not match expected evidence")
    if (
        run_receipt.get("status") != "CHILD_EXITED_NONZERO"
        or run_receipt.get("package_commit") != EXPECTED_PACKAGE
        or run_receipt.get("stage1_source_lock_sha256") != EXPECTED_LOCK
        or run_receipt.get("registered_stage1_preflight_rows_executed") != 0
        or run_receipt.get("registered_stage1_formal_rows_executed") != 0
        or run_receipt.get("stage1_registered_seed_consumption") != 0
        or worker_failure.get("status") != "WITNESS_FAILED"
        or worker_failure.get("exception_type") != "TypeError"
        or worker_failure.get("stage1_ledger_opened") is not False
        or worker_failure.get("registered_stage1_seed_consumption") != 0
        or run_receipt.get("stdout_sha256") != sha256(run_stdout_path)
        or run_receipt.get("stdout_bytes") != run_stdout_path.stat().st_size
        or "baseline_completed = sum(" not in run_stdout
        or "TypeError: 'TrialResult' object is not subscriptable" not in run_stdout
    ):
        raise ValueError("preserved native-run failure receipt does not match expected runner-summary defect")
    if run_receipt.get("prior_setup_attempt_receipt_sha256") != sha256(BASE / "receipt.json"):
        raise ValueError("native-run receipt does not bind the earlier setup-attempt receipt")
    if run_receipt.get("driver_sha256") != sha256(BASE / "driver-retry1.py"):
        raise ValueError("executed retry driver differs from the digest bound by its receipt")

    environment = load(NATIVE / "setup" / "environment.json")
    if (
        environment.get("native_source_commit") != EXPECTED_NATIVE
        or environment.get("stsr_source_commit") != EXPECTED_STSR
        or environment.get("native_module_sha256") != EXPECTED_MODULE_SHA
        or environment.get("cpu_only") is not True
        or environment.get("python") != "3.13.13"
        or environment.get("torch") != "2.12.0+cpu"
        or environment.get("numpy") != "2.4.6"
    ):
        raise ValueError("native setup receipt is not bound to the registered pinned runtime")

    expected_indices = set(range(1, 17))
    starts = {}
    results = {}
    metric_files = {}
    for index in sorted(expected_indices):
        start_path = NATIVE / f"attempt-{index:02d}-started.json"
        result_path = NATIVE / f"attempt-{index:02d}-result.json"
        metric_path = NATIVE / f"trajectory-{index:02d}" / "runtime-metrics.jsonl"
        start = load(start_path)
        result = load(result_path)
        metric_rows = [json.loads(line) for line in metric_path.read_text(encoding="utf-8").splitlines() if line]
        controlled_runs = [row for row in metric_rows if row.get("record_kind") == "controlled_run"]
        if (
            start.get("schema_id") != "issue75-debug-witness-attempt-start-v1"
            or result.get("schema_id") != "issue75-debug-witness-attempt-result-v1"
            or start.get("attempt_index") != index
            or result.get("attempt_index") != index
            or start.get("historical_root_id") != EXPECTED_ROOT
            or result.get("historical_root_id") != EXPECTED_ROOT
            or start.get("arm") != "J"
            or result.get("arm") != "J"
            or start.get("registered_stage1_seed") is not False
            or result.get("complete_terminal") is not True
            or result.get("failure_boundary") is not None
            or result.get("native_rng_source") != "restored_checkpoint_native_rng_same_future_stream"
            or result.get("policy_model_digest") != start.get("policy_model_digest")
            or len(controlled_runs) != 1
            or controlled_runs[0].get("status") != "completed"
            or controlled_runs[0].get("terminal") is not True
            or controlled_runs[0].get("seed") != EXPECTED_OWNER_SEED
            or controlled_runs[0].get("native_source_commit") != EXPECTED_NATIVE
        ):
            raise ValueError(f"attempt {index} failed immutable native-run receipt validation")

        is_mutation = index > 8
        if bool(start.get("mutation_parent_attempt_index") is not None) != is_mutation:
            raise ValueError(f"attempt {index} does not match the locked J root/mutation phase boundary")
        if bool(result.get("mutation_attempt")) != is_mutation:
            raise ValueError(f"attempt {index} result does not match its recorded J phase")
        if is_mutation and result.get("mutation_alternative_is_currently_legal") is not True:
            raise ValueError(f"mutation attempt {index} lacks a live-legal alternative receipt")

        starts[index] = start
        results[index] = result
        metric_files[index] = metric_path

    root_count = sum(not row["mutation_attempt"] for row in results.values())
    mutation_count = sum(bool(row["mutation_attempt"]) for row in results.values())
    if root_count != 8 or mutation_count != 8:
        raise ValueError("the durable attempt receipts do not show the full 8-root / 8-mutation J schedule")

    artifact_paths = sorted(path for path in NATIVE.rglob("*") if path.is_file())
    artifact_hashes = {
        path.relative_to(NATIVE).as_posix(): sha256(path)
        for path in artifact_paths
    }
    artifact_bytes = sum(path.stat().st_size for path in artifact_paths)
    prior_cpu = float(prior["process_tree_cpu_charged_seconds"])
    run_cpu = float(run_receipt["process_tree_cpu_charged_seconds"])
    summary = {
        "schema_id": "issue75-pinned-native-debug-root-witness-recovered-v1",
        "status": "EXECUTION_WITNESS_RECOVERED_FROM_IMMUTABLE_ATTEMPT_RECEIPTS",
        "interpretation": "implementation/runtime witness only; no efficacy or scientific outcome inference",
        "recovery_performed_without_simulator_import_or_trajectory_replay": True,
        "runner_summary_failure": "TypeError after run_search returned; TrialResult was indexed as a mapping",
        "initial_pre_native_failure_receipt_sha256": sha256(BASE / "receipt.json"),
        "initial_pre_native_process_tree_cpu_seconds": prior_cpu,
        "native_run_failure_receipt_sha256": sha256(run_receipt_path),
        "native_run_stdout_sha256": sha256(run_stdout_path),
        "native_run_driver_sha256": run_receipt["driver_sha256"],
        "native_run_process_tree_cpu_seconds": run_cpu,
        "cumulative_witness_process_tree_cpu_seconds": prior_cpu + run_cpu,
        "process_tree_cpu_cap_seconds": run_receipt["process_tree_cpu_cap_seconds"],
        "per_search_cpu_cap_seconds": 32.0,
        "per_search_started_attempt_cap": 16,
        "package_commit": EXPECTED_PACKAGE,
        "stage1_source_lock_sha256": EXPECTED_LOCK,
        "native_source_commit": EXPECTED_NATIVE,
        "stsr_source_commit": EXPECTED_STSR,
        "native_module_sha256": EXPECTED_MODULE_SHA,
        "python": environment["python"],
        "torch": environment["torch"],
        "numpy": environment["numpy"],
        "historical_root_id": EXPECTED_ROOT,
        "historical_owner_seed": EXPECTED_OWNER_SEED,
        "arm": "J",
        "diagnostic_invocation_seed": starts[1]["invocation_seed"],
        "diagnostic_invocation_seed_is_registered": False,
        "started_attempts": len(starts),
        "completed_native_trajectories": sum(bool(row["complete_terminal"]) for row in results.values()),
        "completed_root_proposals": root_count,
        "completed_later_mutations": mutation_count,
        "attempt_reported_cpu_seconds_sum": sum(float(row["cpu_seconds"]) for row in results.values()),
        "registered_stage1_ledger_opened": False,
        "registered_stage1_preflight_rows_executed": 0,
        "registered_stage1_formal_rows_executed": 0,
        "registered_stage1_seed_consumption": 0,
        "native_setup_environment_sha256": sha256(NATIVE / "setup" / "environment.json"),
        "attempt_artifact_count": len(artifact_paths),
        "attempt_artifact_bytes": artifact_bytes,
        "attempt_artifact_sha256": artifact_hashes,
    }
    write_once(recovery_path, summary)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
