"""One bounded Stage 1 J-path witness on a locked historical debug root.

This diagnostic never opens the Stage 1 ledger and never reads or consumes a
registered Stage 1 owner seed. It replays one already-selected historical
Stage 0 prefix, then exercises the real Stage 1 joint-search continuation code.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
STUDY = ROOT / "studies" / "issue-75-no-expert-oracle"
EVIDENCE = Path(__file__).resolve().parent / "retry1"
OUT = EVIDENCE / "native"
ROOT_ID = "root-173730001-0007"
OWNER_SEED = 173730001
EXPECTED_PACKAGE = "2060cb15c072c45a7f80e1ec061c3266f8c4b6f4"
EXPECTED_LOCK = "9e6a3583a00031553927f1e09e4b61c1e1ea89d709ee8ebd82adbee01542f1ff"
STAGE0_SELECTION_LOCK = "issue74-root-selection-lock.json"

sys.path.insert(0, str(STUDY))
sys.path.insert(0, str(STUDY / "stage1"))
import executor  # noqa: E402
import preflight  # noqa: E402
import runtime  # noqa: E402
from algorithm import (  # noqa: E402
    InvocationBudget,
    derive_attempt_seed,
    run_search,
    write_immutable_json,
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_once(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write((canonical(value) + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())


def _root_record():
    lock = json.loads((STUDY / STAGE0_SELECTION_LOCK).read_text(encoding="utf-8"))
    rows = lock.get("selected_roots")
    if not isinstance(rows, list):
        raise PermissionError("locked historical Stage 0 selected-root list is missing")
    row = next((dict(item) for item in rows if item.get("root_id") == ROOT_ID), None)
    if row is None or int(row.get("owner_seed", -1)) != OWNER_SEED:
        raise PermissionError("requested historical debug root is not the exact locked owner/root pair")
    config = executor._read_json(STUDY / "stage1" / "config.json")
    registered = config["source"]["registered_stage0_config"]
    if (
        registered.get("issue74_selection_lock_sha256_lf")
        != preflight.normalized_sha256(STUDY / STAGE0_SELECTION_LOCK)
        or registered.get("issue74_selection_lock_source_path")
        != "studies/issue-74-oracle-feasibility/selection-lock.json"
    ):
        raise PermissionError("historical root selection lock differs from the registered SHA256")
    return row, config


def _worker() -> int:
    import torch

    torch.set_num_threads(1)
    out = OUT
    out.mkdir(parents=True, exist_ok=True)
    row, stage1_config = _root_record()
    package_commit = os.environ.get("ISSUE75_PACKAGE_COMMIT", "")
    source_lock = os.environ.get("ISSUE75_SOURCE_LOCK_SHA256", "")
    if package_commit != EXPECTED_PACKAGE or source_lock != EXPECTED_LOCK:
        raise PermissionError("witness was not invoked on the exact committed Stage 1 package and source lock")
    if executor.budget.verify_stage1_package().get("package_status") != "PREPARATION_ONLY_DISABLED":
        raise PermissionError("witness requires the source-locked preparation-only package")

    registered = stage1_config["source"]["registered_stage0_config"]
    native_config, runner, module, encoder = runtime.initialize_native_runtime(out / "setup", sampling=False)
    root = runtime.replay_locked_root(
        runner=runner,
        module=module,
        encoder=encoder,
        output_dir=out / "historical-prefix",
        owner_seed=OWNER_SEED,
        expected=row,
    )
    policy, policy_digest, policy_source = executor._load_model("pi-stage0-proxy", proxy=True)
    invocation_seed = int.from_bytes(hashlib.sha256(
        f"{EXPECTED_PACKAGE}|{ROOT_ID}|stage1-debug-witness".encode("ascii")
    ).digest()[:8], "big") & 0x7FFFFFFF
    invocation_budget = InvocationBudget(cpu_cap_seconds=32.0, started_attempt_cap=16)
    completed_trials = {}
    mutation_results = []

    def attempt_runner(attempt_index, root_action_index, mutation_target):
        parent_trial = None
        if mutation_target is not None:
            parent_trial = completed_trials.get(int(mutation_target.parent_attempt_index))
            if parent_trial is None:
                raise RuntimeError("debug witness mutation parent was not retained")
        remaining = 32.0 - invocation_budget.cpu_seconds
        if remaining <= 0.1:
            from algorithm import SearchBudgetExhausted
            raise SearchBudgetExhausted("debug witness invocation reached the registered 32 CPU-s cap")
        return runtime.execute_policy_trajectory(
            runner=runner,
            module=module,
            encoder=encoder,
            output_dir=out / f"trajectory-{int(attempt_index):02d}",
            policy=policy,
            owner_seed=OWNER_SEED,
            root=root,
            attempt_index=int(attempt_index),
            invocation_seed=invocation_seed,
            root_action_index=None if int(root_action_index) < 0 else int(root_action_index),
            mutation_target=mutation_target,
            parent_trial=parent_trial,
            cpu_cap_seconds=min(32.0, remaining),
            source_arm="BRANCH",
            policy_rng_seed=derive_attempt_seed(
                "issue-75-stage1-debug-witness", ROOT_ID, invocation_seed,
                int(attempt_index), "independent-policy-suffix",
            ),
        )

    def record_started(attempt_index, root_action_index, target):
        write_immutable_json(out / f"attempt-{attempt_index:02d}-started.json", {
            "schema_id": "issue75-debug-witness-attempt-start-v1",
            "historical_root_id": ROOT_ID,
            "arm": "J",
            "attempt_index": int(attempt_index),
            "root_action_index": None if root_action_index < 0 else int(root_action_index),
            "mutation_parent_attempt_index": None if target is None else int(target.parent_attempt_index),
            "mutation_parent_step_index": None if target is None else int(target.parent_decision.step_index),
            "mutation_alternative_index": None if target is None else int(target.alternative_index),
            "mutation_legal_occurrence_count": None if target is None else len(target.parent_decision.legal_identities),
            "policy_model_digest": policy_digest,
            "invocation_seed": invocation_seed,
            "registered_stage1_seed": False,
        })

    def record_result(trial, root_action_index, attempt_index, target):
        if trial.complete:
            completed_trials[int(attempt_index)] = trial
        item = {
            "schema_id": "issue75-debug-witness-attempt-result-v1",
            "historical_root_id": ROOT_ID,
            "arm": "J",
            "attempt_index": int(attempt_index),
            "complete_terminal": bool(trial.complete),
            "heart_win": bool(trial.heart_win) if trial.complete else None,
            "terminal_floor": float(trial.terminal_floor) if trial.complete else None,
            "decision_count": len(trial.decisions),
            "mutation_attempt": target is not None,
            "mutation_alternative_is_currently_legal": target is not None,
            "failure_boundary": trial.failure_boundary,
            "failure_detail": None if trial.failure_detail is None else dict(trial.failure_detail),
            "cpu_seconds": trial.cpu_seconds,
            "policy_model_digest": policy_digest,
            "native_rng_source": trial.native_rng_source,
        }
        write_immutable_json(out / f"attempt-{attempt_index:02d}-result.json", item)
        if target is not None:
            mutation_results.append(item)

    try:
        search = run_search(
            arm="J",
            study_id="issue-75-stage1-debug-witness",
            root_id=ROOT_ID,
            invocation_seed=invocation_seed,
            root_legal_identities=root.legal_identities,
            attempt_runner=attempt_runner,
            budget=invocation_budget,
            attempt_started_recorder=record_started,
            attempt_result_recorder=record_result,
        )
        mutation_completed = sum(bool(item["complete_terminal"]) for item in mutation_results)
        baseline_completed = sum(
            bool(item["complete_terminal"]) for item in completed_trials.values()
            if int(item.attempt_index) not in {int(row["attempt_index"]) for row in mutation_results}
        )
        complete_mutation = mutation_completed > 0
        status = "EXECUTION_WITNESS_COMPLETE" if baseline_completed > 0 and complete_mutation else "WITNESS_INCOMPLETE"
        witness = {
            "schema_id": "issue75-pinned-native-debug-root-witness-v1",
            "status": status,
            "purpose": "one bounded implementation witness only; no scientific outcome inference",
            "package_commit": package_commit,
            "stage1_source_lock_sha256": source_lock,
            "stage1_package_status": "PREPARATION_ONLY_DISABLED",
            "registered_stage0_root_selection_sha256": registered.get("issue74_selection_lock_sha256_lf"),
            "historical_root_selection_lock_file_sha256": executor._file_sha256(STUDY / STAGE0_SELECTION_LOCK),
            "historical_root_id": ROOT_ID,
            "historical_owner_seed": OWNER_SEED,
            "historical_owner_is_registered_stage1_seed": False,
            "stage1_ledger_opened": False,
            "registered_stage1_preflight_rows_executed": 0,
            "registered_stage1_formal_rows_executed": 0,
            "stage1_registered_seed_consumption": 0,
            "native_source_commit": native_config["source"]["sts_lightspeed_commit"],
            "stsr_source_commit": native_config["source"]["stsr_source_commit"],
            "native_module_sha256": native_config["source"]["native_module_sha256"],
            "stage1_policy_source": policy_source,
            "stage1_policy_model_digest": policy_digest,
            "root_screen": root.screen,
            "root_legal_occurrence_count": len(root.legal_identities),
            "root_feature_matrix_sha256": root.feature_matrix_sha256,
            "root_fingerprints_sha256": digest_bytes(canonical(dict(root.fingerprints)).encode("utf-8")),
            "arm": "J",
            "invocation_seed_is_registered": False,
            "search_status": search.status,
            "search_started_attempts": search.started_attempts,
            "search_completed_trajectories": search.completed_trajectories,
            "search_mutation_attempts_started": search.mutation_attempts_started,
            "search_mutation_attempts_completed": mutation_completed,
            "search_treatment_delivered": bool(search.mutation_attempts_started),
            "search_cpu_seconds": search.cpu_seconds,
            "search_attempt_cpu_seconds": list(search.attempt_cpu_seconds),
            "search_stop_reason": search.stop_reason,
            "terminal_root_trajectories_completed": baseline_completed,
            "terminal_mutation_trajectories_completed": mutation_completed,
            "selected_root_identity_sha256": None if search.selected_root_identity is None else digest_bytes(
                canonical(search.selected_root_identity).encode("utf-8")
            ),
            "search_native_rng_source": search.native_rng_source,
            "process_cpu_limit_seconds": 32.0,
            "started_attempt_limit": 16,
        }
        write_once(out / "worker-result.json", witness)
        return 0 if status == "EXECUTION_WITNESS_COMPLETE" else 2
    finally:
        root.adapter.close()


def main() -> int:
    if sys.argv[1:] == ["--worker"]:
        try:
            return _worker()
        except BaseException as exc:
            write_once(OUT / "worker-failure.json", {
                "schema_id": "issue75-debug-witness-worker-failure-v1",
                "status": "WITNESS_FAILED",
                "exception_type": type(exc).__name__,
                "stage1_ledger_opened": False,
                "registered_stage1_seed_consumption": 0,
            })
            raise

    for path in (OUT / "worker-result.json", OUT / "worker-failure.json", EVIDENCE / "receipt.json", EVIDENCE / "watchdog.stdout"):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite prior debug-witness evidence: {path.name}")
    if os.environ.get("ISSUE75_PACKAGE_COMMIT") != EXPECTED_PACKAGE:
        raise PermissionError("environment does not name the exact committed Stage 1 package")
    if os.environ.get("ISSUE75_SOURCE_LOCK_SHA256") != EXPECTED_LOCK:
        raise PermissionError("environment does not name the exact committed Stage 1 source lock")

    prior_receipt_path = Path(__file__).resolve().parent / "receipt.json"
    prior_receipt_bytes = prior_receipt_path.read_bytes()
    prior_receipt = json.loads(prior_receipt_bytes)
    prior_worker = prior_receipt.get("worker_result") or {}
    if (
        prior_receipt.get("status") != "CHILD_EXITED_NONZERO"
        or prior_worker.get("status") != "WITNESS_FAILED"
        or prior_worker.get("exception_type") != "PermissionError"
        or prior_worker.get("stage1_ledger_opened") is not False
        or prior_worker.get("registered_stage1_seed_consumption") != 0
    ):
        raise PermissionError("retry is limited to the preserved pre-native package-status check failure")

    task_env = os.environ.copy()
    task_env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(EVIDENCE / "tmp"),
        "TMP": str(EVIDENCE / "tmp"),
        "TEMP": str(EVIDENCE / "tmp"),
        "XDG_CACHE_HOME": str(EVIDENCE / "cache"),
        "TORCH_HOME": str(EVIDENCE / "cache" / "torch"),
        "PYTHONPYCACHEPREFIX": str(EVIDENCE / "pycache"),
        "STS_LIGHTSPEED_SOURCE_DIR": "/mnt/d/b75sts",
        "STSRL_SOURCE_DIR": "/mnt/d/b75stsr",
        "ISSUE75_PACKAGE_COMMIT": EXPECTED_PACKAGE,
        "ISSUE75_SOURCE_LOCK_SHA256": EXPECTED_LOCK,
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    })
    for name in ("tmp", "cache", "cache/torch", "pycache"):
        (EVIDENCE / name).mkdir(parents=True, exist_ok=True)
    watchdog = executor.budget.ProcessTreeWatchdog(poll_seconds=0.01)
    try:
        process = watchdog.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker"],
            reserved_cpu_seconds=160.0,
            wall_timeout_seconds=600.0,
            cwd=ROOT,
            env=task_env,
            stdout_path=EVIDENCE / "watchdog.stdout",
        )
        cpu_seconds = process.cpu_seconds
        return_code = process.returncode
        status = "COMPLETED" if return_code == 0 else "CHILD_EXITED_NONZERO"
    except executor.budget.ProcessTreeBudgetExceeded as exc:
        cpu_seconds = float(exc.cpu_seconds)
        return_code = exc.returncode
        status = "STAGE0_ROOT_WORKER_CPU_CAP"
    except executor.budget.ProcessTreeTimeout as exc:
        cpu_seconds = float(exc.cpu_seconds)
        return_code = None
        status = "STAGE0_ROOT_WORKER_WALL_TIMEOUT"
    except BaseException as exc:
        cpu_seconds = float(getattr(exc, "cpu_seconds", 0.0))
        return_code = getattr(exc, "returncode", None)
        status = f"WATCHDOG_{type(exc).__name__}"

    worker_result = OUT / "worker-result.json"
    worker_failure = OUT / "worker-failure.json"
    if worker_result.is_file():
        result = json.loads(worker_result.read_text(encoding="utf-8"))
    elif worker_failure.is_file():
        result = json.loads(worker_failure.read_text(encoding="utf-8"))
    else:
        result = None
    stdout_path = EVIDENCE / "watchdog.stdout"
    receipt = {
        "schema_id": "issue75-pinned-native-debug-root-witness-watchdog-v1",
        "driver_sha256": digest_bytes(Path(__file__).read_bytes()),
        "prior_setup_attempt_receipt_sha256": digest_bytes(prior_receipt_bytes),
        "prior_setup_attempt_cpu_seconds": prior_receipt.get("process_tree_cpu_charged_seconds"),
        "status": status,
        "package_commit": EXPECTED_PACKAGE,
        "stage1_source_lock_sha256": EXPECTED_LOCK,
        "resource_pool": "existing Stage 0 root-worker allowance",
        "process_tree_cpu_cap_seconds": 160.0,
        "process_tree_cpu_charged_seconds": cpu_seconds,
        "wall_timeout_seconds": 600.0,
        "worker_returncode": return_code,
        "worker_result": result,
        "stdout_sha256": digest_bytes(stdout_path.read_bytes()) if stdout_path.exists() else None,
        "stdout_bytes": stdout_path.stat().st_size if stdout_path.exists() else 0,
        "registered_stage1_preflight_rows_executed": 0,
        "registered_stage1_formal_rows_executed": 0,
        "stage1_registered_seed_consumption": 0,
    }
    write_once(EVIDENCE / "receipt.json", receipt)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if status == "COMPLETED" and result and result.get("status") == "EXECUTION_WITNESS_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
