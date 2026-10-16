"""Bounded Issue 60 guard-cache repair and retained-key replay validation.

This producer is intentionally separate from the sealed R2 source/receipt.  It
performs no training or evaluation-grid work and charges setup, tests, serial
replays, parallel workers, and finalization as new append-only ledger rows.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from typing import Any


PROCESS_CPU_START = time.process_time()
HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "evidence"
VALIDATION_ROOT = EVIDENCE / "validation" / "guard-optimization"
MAX_ADDITIONAL_CPU_SECONDS = 1000.0
FINALIZATION_RESERVE_SECONDS = 15.0
STATIC_ALLOWANCE_SECONDS = 600.0
BUILDER_BUDGET_SECONDS = 180_000.0
TOTAL_PROCESS_CAP_SECONDS = 200_000.0
REVIEWER_RESERVE_SECONDS = 20_000.0
FOCUSED_SEEDS = (660001, 660046)
PRODUCER_FILES = (
    "guard_optimization.py", "run_guard_optimization.py", "test_guard_optimization.py",
)
COMPARISON_FILES = (
    "run_study.py", "run_study_r2.py", "resource_amendment_r2.py",
    "parallel_execution.py", "run_study_parallel.py",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _next_attempt() -> Path:
    existing = [
        int(path.name.removeprefix("attempt-"))
        for path in VALIDATION_ROOT.glob("attempt-*")
        if path.name.removeprefix("attempt-").isdigit()
    ]
    path = VALIDATION_ROOT / f"attempt-{max(existing, default=0) + 1:04d}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _ledger_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            amount = float(row.get("process_cpu_seconds", 0.0))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid CPU-ledger row {number}") from exc
        if amount < 0 or not (amount < float("inf")):
            raise RuntimeError(f"invalid CPU amount in ledger row {number}")
        rows.append(row)
    return rows


def _prior_diagnostic_cpu(rows: list[dict[str, Any]]) -> float:
    return sum(
        float(row.get("process_cpu_seconds", 0.0))
        for row in rows
        if str(row.get("stage", "")).startswith("guard-optimization-validation")
    )


def _reusable_replay(attempts: list[Path], variant: str, seed: int,
                     model_sha: str, config_sha: str,
                     producer_hashes: dict[str, str]) -> tuple[Path, dict[str, Any]] | None:
    for producer in attempts:
        receipt_path = producer / "producer.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if receipt.get("producer_files") != producer_hashes:
            continue
        output = producer / "replays" / variant / f"seed-{seed}"
        for summary_path in sorted(output.glob("attempt-*/run.json")):
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (summary.get("status") == "COMPLETE"
                    and summary.get("model_sha256") == model_sha
                    and summary.get("config_sha256") == config_sha
                    and (summary_path.parent / "decisions.jsonl.gz").is_file()):
                return summary_path.parent, summary
    return None


def _unit_witness() -> dict[str, Any]:
    output = io.StringIO()
    suite = unittest.defaultTestLoader.discover(
        str(HERE), pattern="test_guard_optimization.py",
    )
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    record = {
        "status": "PASS" if result.wasSuccessful() else "FAIL",
        "tests_run": result.testsRun,
        "failures": [str(error) for _test, error in result.failures],
        "errors": [str(error) for _test, error in result.errors],
        "output": output.getvalue(),
    }
    if not result.wasSuccessful():
        raise RuntimeError("focused guard-cache tests failed: " + output.getvalue())
    return record


def _progress(attempt: Path, record: dict[str, Any]) -> None:
    _atomic_json(attempt / "producer.json", record)


def _history_hotspot_disposition() -> dict[str, Any]:
    profile_path = EVIDENCE / "validation" / "parallel-execution" / "receipt.json"
    try:
        receipt = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("retained history hotspot receipt is unavailable") from exc
    observed = []
    for profile in receipt.get("profile", {}).get("partial_profiles_by_seed", []):
        for function in profile.get("top_functions", []):
            name = str(function.get("function", ""))
            if name in {
                "sanitize_public_run_context", "forbidden_public_context_problems",
                "_append_forbidden_problems", "append_public_history_entry", "with_public_history",
            }:
                observed.append({
                    "file": function.get("file"), "function": name,
                    "inclusive_cpu_seconds": function.get("inclusive_cpu_seconds"),
                    "exclusive_cpu_seconds": function.get("exclusive_cpu_seconds"),
                })
    return {
        "status": "DEFERRED_IMMUTABILITY_NOT_ESTABLISHED",
        "profile_source": "evidence/validation/parallel-execution/receipt.json",
        "observed_functions": observed,
        "call_path": [
            "pinned STSRL public_run_context.sanitize_public_run_context",
            "pinned STSRL public_run_context.forbidden_public_context_problems",
            "study bootstrap_v47.with_public_history",
            "pinned STSRL append_public_history_entry copies/sanitizes prior history",
        ],
        "reason": "the retained profile identifies repeated walks, but immutability and exact rejection-equivalence have not been established; no caching or validation weakening was applied",
        "source_changes": [],
    }


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    attempt = _next_attempt()
    ledger_path = EVIDENCE / "cpu-ledger.jsonl"
    old_rows = _ledger_rows(ledger_path)
    prior_diagnostic_cpu = _prior_diagnostic_cpu(old_rows)
    producer_hashes = {name: _sha256(HERE / name) for name in PRODUCER_FILES}
    source_hashes = {name: _sha256(HERE / name) for name in COMPARISON_FILES}
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=HERE, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    record: dict[str, Any] = {
        "schema_id": "issue60-guard-optimization-validation-v1",
        "status": "RUNNING", "attempt": attempt.name,
        "planner_authorization_comment": 6213786268,
        "additional_cpu_bound_seconds": MAX_ADDITIONAL_CPU_SECONDS,
        "cumulative_builder_budget_seconds": BUILDER_BUDGET_SECONDS,
        "cumulative_total_process_cap_seconds": TOTAL_PROCESS_CAP_SECONDS,
        "reviewer_reserve_seconds": REVIEWER_RESERVE_SECONDS,
        "source_commit": source_commit,
        "producer_files": producer_hashes,
        "comparison_source_files": source_hashes,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "formal_sampling_ppo_evaluation": "PAUSED",
        "focused_keys": list(FOCUSED_SEEDS),
        "replays": {"baseline_serial": [], "cached_serial": [], "cached_two_worker": []},
    }
    _progress(attempt, record)

    # Imported after the durable producer marker so a setup failure is visible.
    from guard_optimization import GuardState, install_cached_guard
    import resource_amendment_r1 as r1
    import resource_amendment_r2 as r2
    import run_study
    import run_study_r2
    import parallel_execution
    import run_study_parallel

    state: GuardState | None = None
    active_tracker: Any = None
    ledger_state_wrapper_active = False
    parallel_ledger_base: float | None = None
    parallel_diagnostic_base: float | None = None
    parent_charge_cursor = PROCESS_CPU_START
    diagnostic_charged = prior_diagnostic_cpu
    append_lock = __import__("threading").RLock()
    builder_budget = BUILDER_BUDGET_SECONDS

    def append_charge(stage: str, amount: float, *, status: str = "COMPLETE",
                      details: dict[str, Any] | None = None) -> None:
        nonlocal diagnostic_charged
        if amount < 0 or not (amount < float("inf")):
            raise RuntimeError(f"invalid CPU charge for {stage}: {amount}")
        row = {
            "stage": f"guard-optimization-validation-{stage}",
            "status": status,
            "process_cpu_seconds": float(amount),
            "attempt": attempt.name,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "accounting": "new bounded Issue 60 guard-cache repair validation; append-only; separate from sealed R2 prefix",
        }
        if details:
            row.update(details)
        with append_lock:
            run_study_r2._append_ledger(row)
            diagnostic_charged += float(amount)
            if state is not None and not ledger_state_wrapper_active:
                state.add_ledger_row(row)

    def charge_parent(stage: str, *, status: str = "COMPLETE",
                      details: dict[str, Any] | None = None) -> float:
        nonlocal parent_charge_cursor
        elapsed = max(0.0, time.process_time() - parent_charge_cursor)
        amount = elapsed + 0.1  # covers ledger and the next durable progress write
        append_charge(stage, amount, status=status, details=details)
        parent_charge_cursor = time.process_time()
        return amount

    def actual_budget_guard(*, accounted_cpu: float | None = None,
                            current_stage_cpu: float | None = None,
                            **_kwargs: Any) -> dict[str, Any]:
        del current_stage_cpu
        parent_unaccounted = max(0.0, time.process_time() - parent_charge_cursor)
        tracker = active_tracker
        active = tracker.active_worker_count() if tracker is not None else 0
        if tracker is None:
            current_unaccounted = parent_unaccounted
            ledger_base = state.ledger_total(HERE) if state is not None else r2._ledger_total(HERE)
            diagnostic_base = diagnostic_charged
        else:
            # The tracker snapshot includes parent + live + wait4-settled CPU
            # since dispatch. Use the corresponding ledger snapshot from just
            # before dispatch so concurrent worker settlements are counted
            # once, even in a fork whose Python cache is now stale.
            assert parallel_ledger_base is not None and parallel_diagnostic_base is not None
            pre_dispatch_parent = max(0.0, tracker.parent_start_cpu - parent_charge_cursor)
            current_unaccounted = pre_dispatch_parent + max(0.0, tracker.current_stage_cpu())
            ledger_base = parallel_ledger_base
            diagnostic_base = parallel_diagnostic_base
        observed_costs = _kwargs.get("costs")
        max_game = (max(state.max_observed_game_cpu_seconds, 1.0) if state is not None else
                    max((float(row["process_cpu_seconds"]) for row in (observed_costs or ())), default=1.0))
        if tracker is not None:
            with tracker.max_game_cpu.get_lock():
                max_game = max(max_game, float(tracker.max_game_cpu.value))
        reserve = active * max_game
        actual_builder = STATIC_ALLOWANCE_SECONDS + ledger_base + current_unaccounted
        if tracker is None and accounted_cpu is not None:
            actual_builder = float(accounted_cpu) + current_unaccounted
        diagnostic_spent = diagnostic_base + current_unaccounted
        if diagnostic_spent + reserve + FINALIZATION_RESERVE_SECONDS >= MAX_ADDITIONAL_CPU_SECONDS:
            raise r2.ResourceBudgetExceeded(
                "RESOURCE_BLOCKED: guard-cache validation reached its 1,000 aggregate CPU-s "
                "allowance with a safe in-flight/finalization reserve"
            )
        if actual_builder + reserve + FINALIZATION_RESERVE_SECONDS >= builder_budget:
            raise r2.ResourceBudgetExceeded(
                "RESOURCE_BLOCKED: cumulative Builder CPU cap reached with a safe reserve"
            )
        if actual_builder + reserve + REVIEWER_RESERVE_SECONDS + FINALIZATION_RESERVE_SECONDS >= TOTAL_PROCESS_CAP_SECONDS:
            raise r2.ResourceBudgetExceeded(
                "RESOURCE_BLOCKED: cumulative process cap/reviewer reserve reached"
            )
        return {
            "status": "PASS", "actual_builder_cpu_seconds": actual_builder,
            "diagnostic_cpu_seconds": diagnostic_spent,
            "safe_in_flight_reserve_cpu_seconds": reserve,
            "active_or_pending_workers": active,
        }

    def tracked_reap_charge(pid: int, seconds: float, slot: int | None) -> None:
        append_charge(
            f"parallel-worker-{pid}", max(0.0, float(seconds)),
            details={"worker_pid": int(pid), "worker_slot": slot,
                     "accounting": "wait4 user+system process CPU, exactly once at worker settlement"},
        )

    try:
        chain = r1.verify_resource_chain(HERE, require_gate_seal=True)
        r2_receipt = r2.verify_r2_receipt(HERE, require_seal=True)
        gate_path = EVIDENCE / "preflight" / "resource-r2.json"
        native_path = EVIDENCE / "preflight" / "native.json"
        before_r2_receipt_sha = _sha256(gate_path)
        r2_gate = json.loads(gate_path.read_text(encoding="utf-8"))["gate"]
        unchanged_forecast = r2_gate["forecast"]
        unchanged_gate = {
            "status": r2_gate["status"], "decision": r2_gate["decision"],
            "forecast": unchanged_forecast,
            "receipt_sha256": before_r2_receipt_sha,
            "seal_sha256": r2_receipt["seal"]["preseal_receipt_sha256"],
            "retained_ledger_prefix_line_count": r2_receipt["seal"]["ledger_prefix_line_count"],
        }
        config, runtime, vocab, bootstrap, model, model_sha = run_study_parallel._load_validation_inputs()
        config_digest = run_study.config_sha()
        latest_r2_validation = json.loads(
            (EVIDENCE / "validation" / "parallel-execution" / "receipt.json").read_text(encoding="utf-8")
        )
        latest_forecast = latest_r2_validation["formal_grid_forecast_at_validation"]
        if latest_forecast.get("status") != "RESOURCE_BLOCKED":
            raise RuntimeError("the latest retained R2 forecast no longer matches the Planner-described stop")
        r2.STAGE_START_CPU = PROCESS_CPU_START
        run_study_r2.RUN_STUDY = run_study
        run_study_r2._install_runtime_hooks()

        # The formal R2 forecast remains intact and is recorded verbatim. For
        # these fixed diagnostic replays only, check actual cumulative limits
        # and safe in-flight completion reserves at each existing boundary.
        r2.enforce_budget = actual_budget_guard
        run_study.budget_guard = lambda **kwargs: actual_budget_guard(**kwargs)

        original_r2_costs = r2.complete_game_costs
        original_r2_optimizer_projection = r2.remaining_optimizer_projection
        record.update({
            "resource_chain": chain,
            "r2_receipt": unchanged_gate,
            "latest_unchanged_r2_forecast": {
                "source": "evidence/validation/parallel-execution/receipt.json",
                "receipt_sha256": _sha256(EVIDENCE / "validation" / "parallel-execution" / "receipt.json"),
                "forecast": latest_forecast,
                "status": "RESOURCE_BLOCKED; unchanged R2 arithmetic at the last formal forecast snapshot; not a new measured run cost",
            },
            "zero_update_checkpoint": {
                "model_sha256": model_sha,
                "config_sha256": config_digest,
                "completed_batches": 0,
                "checkpoint": "evidence/training/block1/latest.pt",
            },
            "runtime": runtime,
            "focused_unit_tests": _unit_witness(),
            "history_hotspot": _history_hotspot_disposition(),
            "replays": {"baseline_serial": [], "cached_serial": [], "cached_two_worker": []},
        })
        state = GuardState(HERE, r2)
        append_charge(
            "setup", max(0.0, time.process_time() - parent_charge_cursor) + 0.1,
            details={"focused_test_count": record["focused_unit_tests"]["tests_run"],
                     "startup_indexed_complete_cost_rows": len(state.complete_game_costs(HERE)),
                     "startup_completed_training_evaluation_keys": state.completed_run_count(),
                     "startup_completed_optimizer_batches": len(state._completed_batch_paths)},
        )
        parent_charge_cursor = time.process_time()

        remaining_builder = max(1.1, builder_budget - STATIC_ALLOWANCE_SECONDS - state.ledger_total(HERE) - 1.0)
        remaining_diagnostic = max(1.1, MAX_ADDITIONAL_CPU_SECONDS - diagnostic_charged - FINALIZATION_RESERVE_SECONDS)
        limit = r2.install_process_cpu_limit(
            current_process_cpu=time.process_time(),
            remaining_cpu=min(remaining_builder, remaining_diagnostic),
        )
        record["process_cpu_limit"] = limit
        _progress(attempt, record)

        previous_attempts = sorted(VALIDATION_ROOT.glob("attempt-*"))

        def validation_kwargs(seed: int, output: Path) -> dict[str, Any]:
            return {
                "seed": seed, "mode": "residual", "model": model,
                "block_name": "block1",
                "runtime_seed": run_study.derive_seed(
                    "training-policy", run_study.MODEL_SEEDS["block1"], seed, 1,
                ),
                "output_dir": output, "model_sha": model_sha,
                "config_digest": config_digest, "vocab": vocab,
                "bootstrap": bootstrap, "require_targets": True,
                "provenance_config": run_study._current_policy_provenance(
                    config, vocab, model_sha, "residual",
                ),
                "stage_key": "block1:batch-01", "arm": "residual",
            }

        def compare_one(variant: str, seed: int,
                       result: tuple[Path, dict[str, Any]]) -> dict[str, Any]:
            replay_attempt, replay_summary = result
            original = run_study.successful_attempt(
                run_study._training_game_dir("block1", 1, seed), model_sha, config_digest,
            )
            if original is None:
                raise RuntimeError(f"retained fixed-key original is missing for seed {seed}")
            original_attempt, original_summary = original
            check = parallel_execution.compare_replay(
                original_summary, replay_summary,
                run_study.read_jsonl(original_attempt / "decisions.jsonl.gz"),
                run_study.read_jsonl(replay_attempt / "decisions.jsonl.gz"),
            )
            record_row = {
                "variant": variant, "seed": seed, "status": "COMPLETE",
                "attempt_path": replay_attempt.relative_to(HERE).as_posix(),
                "comparison_to_retained_original": check,
                "run_process_cpu_seconds": float(replay_summary["process_cpu_seconds"]),
                "decision_rows": int(replay_summary.get("decision_count", 0)),
                "supported_choices": int(replay_summary.get("supported_choices", 0)),
                "executor_transitions": int(replay_summary.get("transitions", 0)),
                "terminal_summary": {
                    field: replay_summary.get(field)
                    for field in ("status", "terminal", "outcome", "terminal_floor",
                                  "terminal_public_location", "death_location", "problems")
                },
                "decision_file_sha256": _sha256(replay_attempt / "decisions.jsonl.gz"),
            }
            return record_row

        def serial_variant(variant: str) -> list[tuple[Path, dict[str, Any]]]:
            results = []
            rows = record["replays"][variant]
            for seed in FOCUSED_SEEDS:
                reusable = _reusable_replay(
                    previous_attempts, variant, seed, model_sha, config_digest,
                    producer_hashes,
                )
                if reusable is not None:
                    result = reusable
                    rows.append(compare_one(variant, seed, result))
                    rows[-1]["reused_prior_attempt"] = True
                    _progress(attempt, record)
                    results.append(result)
                    continue
                output = attempt / "replays" / variant / f"seed-{seed}"
                start_cpu = time.process_time()
                start_wall = time.monotonic()
                status = "FAILED"
                try:
                    result = run_study.run_game(**validation_kwargs(seed, output))
                    row = compare_one(variant, seed, result)
                    row.update({
                        "reused_prior_attempt": False,
                        "process_cpu_seconds_including_runner_guard": max(0.0, time.process_time() - start_cpu),
                        "wall_seconds": max(0.0, time.monotonic() - start_wall),
                        "parent_peak_rss_bytes": int(__import__("resource").getrusage(__import__("resource").RUSAGE_SELF).ru_maxrss) * 1024,
                    })
                    rows.append(row)
                    status = "COMPLETE"
                    results.append(result)
                except BaseException as exc:
                    rows.append({"seed": seed, "status": "FAILED", "failure": f"{type(exc).__name__}: {exc}"})
                    record["status"] = "RESOURCE_BLOCKED" if "RESOURCE_BLOCKED" in str(exc) else "FAILED"
                    record["failure"] = rows[-1]["failure"]
                    _progress(attempt, record)
                    raise
                finally:
                    elapsed = max(0.0, time.process_time() - start_cpu)
                    # The final row is durable before the ledger append; this
                    # charge includes the game, full decision comparison and
                    # progress serialization, with a small write margin.
                    charge_parent(f"{variant}-{seed}", status=status,
                                  details={"game_seed": seed, "variant": variant,
                                           "phase_cpu_seconds": elapsed})
                    _progress(attempt, record)
            return results

        # Baseline retains the original R2 filesystem guard. The same exact
        # wrapper and actual-only limit are used after installing the cache.
        baseline = serial_variant("baseline_serial")
        install_cached_guard(
            run_study, run_study_r2, r2, state,
            parallel_module=parallel_execution,
            on_worker_settlement=tracked_reap_charge,
        )
        ledger_state_wrapper_active = True
        cached_serial = serial_variant("cached_serial")
        for index, seed in enumerate(FOCUSED_SEEDS):
            baseline_result = baseline[index]
            cached_result = cached_serial[index]
            pair_check = parallel_execution.compare_replay(
                baseline_result[1], cached_result[1],
                run_study.read_jsonl(baseline_result[0] / "decisions.jsonl.gz"),
                run_study.read_jsonl(cached_result[0] / "decisions.jsonl.gz"),
            )
            record["replays"]["cached_serial"][index]["comparison_to_uncached_baseline"] = pair_check
        _progress(attempt, record)

        # Complete two-worker replay is attempted only if the remaining
        # diagnostic allowance covers two safe full-game reserves.
        active_tracker = parallel_execution.ProcessCpuTracker.start(time.process_time())
        with active_tracker.max_game_cpu.get_lock():
            active_tracker.max_game_cpu.value = max(1.0, state.max_observed_game_cpu_seconds)
        parallel_ledger_base = state.ledger_total(HERE)
        parallel_diagnostic_base = diagnostic_charged
        tasks = []
        for seed in FOCUSED_SEEDS:
            output = attempt / "replays" / "cached_two_worker" / f"seed-{seed}"
            tasks.append({
                "key": ("block1:batch-01", seed, "residual"),
                "kwargs": validation_kwargs(seed, output),
            })
        dispatcher = parallel_execution.ParallelGameDispatcher(run_study, active_tracker, 2)
        cached_serial_run_game = run_study.run_game
        results = []
        parallel_start = time.process_time()
        parallel_wall = time.monotonic()
        run_study.budget_guard = lambda **kwargs: actual_budget_guard(**kwargs)
        run_study.run_game = dispatcher.dispatch_run_game
        try:
            parallel_results = dispatcher.run_jobs(tasks)
            for seed in FOCUSED_SEEDS:
                result = parallel_results[("block1:batch-01", seed, "residual")]
                row = compare_one("cached_two_worker", seed, result)
                execution = next(item for item in dispatcher.executions if item["task_key"][1] == seed)
                row.update({
                    "worker_process_cpu_seconds_including_setup": execution["process_cpu_seconds_including_worker_setup"],
                    "worker_wall_seconds": execution["wall_seconds"],
                    "worker_peak_rss_bytes": execution["peak_rss_bytes"],
                    "parent_process_cpu_seconds_for_pair": max(0.0, time.process_time() - parallel_start),
                })
                record["replays"]["cached_two_worker"].append(row)
                _progress(attempt, record)
            record["two_worker_dispatcher_evidence"] = dispatcher.evidence()
            record["two_worker_wall_seconds_for_pair"] = max(0.0, time.monotonic() - parallel_wall)
            record["two_worker_parallel_comparisons_to_cached_serial"] = []
            for index, seed in enumerate(FOCUSED_SEEDS):
                optimized_pair = cached_serial[index]
                parallel_pair = parallel_results[("block1:batch-01", seed, "residual")]
                record["two_worker_parallel_comparisons_to_cached_serial"].append(
                    parallel_execution.compare_replay(
                        optimized_pair[1], parallel_pair[1],
                        run_study.read_jsonl(optimized_pair[0] / "decisions.jsonl.gz"),
                        run_study.read_jsonl(parallel_pair[0] / "decisions.jsonl.gz"),
                    )
                )
            record["status"] = "PASS"
        except BaseException as exc:
            record["status"] = "RESOURCE_BLOCKED" if "RESOURCE_BLOCKED" in str(exc) else "FAILED"
            record["failure"] = f"{type(exc).__name__}: {exc}"
            record["two_worker_dispatcher_evidence"] = dispatcher.evidence()
            record["two_worker_wall_seconds_for_pair"] = max(0.0, time.monotonic() - parallel_wall)
            raise
        finally:
            active_tracker = None
            charge_parent("two-worker-parent", status=record["status"],
                          details={"parent_cpu_seconds_for_pair": max(0.0, time.process_time() - parallel_start)})
            run_study.run_game = cached_serial_run_game
            _progress(attempt, record)

        after_r2 = r2.verify_r2_receipt(HERE, require_seal=True)
        after_receipt_sha = _sha256(gate_path)
        record["r2_unchanged_check"] = {
            "receipt_sha256_before": before_r2_receipt_sha,
            "receipt_sha256_after": after_receipt_sha,
            "receipt_bytes_unchanged": before_r2_receipt_sha == after_receipt_sha,
            "sealed_prefix_still_verifies": True,
            "seal_sha256_after": after_r2["seal"]["preseal_receipt_sha256"],
            "cached_historical_cost_rows_match_r2": state.complete_game_costs(HERE) == original_r2_costs(HERE),
            "cached_optimizer_projection_matches_r2": state.r2_optimizer_projection(HERE) == original_r2_optimizer_projection(HERE),
        }
        if not record["r2_unchanged_check"]["receipt_bytes_unchanged"]:
            raise RuntimeError("R2 receipt unexpectedly changed during validation")
        measured = [row for row in record["replays"]["cached_serial"] if row.get("status") == "COMPLETE"]
        if len(measured) == 2:
            record["measured_cached_serial_throughput"] = {
                "label": "measured two-key zero-update replay only; not a formal-grid cost forecast",
                "complete_games": 2,
                "mean_process_cpu_seconds_per_game": sum(float(row["run_process_cpu_seconds"]) for row in measured) / 2.0,
                "mean_wall_seconds_per_game": sum(float(row["wall_seconds"]) for row in measured) / 2.0,
                "mean_supported_choices": sum(int(row["supported_choices"]) for row in measured) / 2.0,
                "mean_executor_transitions": sum(int(row["executor_transitions"]) for row in measured) / 2.0,
            }
        record["aggregate_additional_cpu_seconds_this_attempt_before_finalization"] = max(
            0.0, diagnostic_charged - prior_diagnostic_cpu,
        )
        record["status"] = "PASS"
    except BaseException as exc:
        if record.get("status") == "RUNNING":
            record["status"] = "RESOURCE_BLOCKED" if "RESOURCE_BLOCKED" in str(exc) else "FAILED"
            record["failure"] = f"{type(exc).__name__}: {exc}"
        if state is not None:
            try:
                record["cache_state_at_stop"] = {
                    "completed_cost_rows": len(state.complete_game_costs(HERE)),
                    "completed_run_keys": state.completed_run_count(),
                    "optimizer_projection": state.r2_optimizer_projection(HERE),
                    "settled_worker_cpu_seconds": state.settled_worker_cpu_seconds,
                    "worker_settlements": list(state.worker_settlements),
                }
            except BaseException as secondary:
                record["cache_state_capture_failure"] = f"{type(secondary).__name__}: {secondary}"
        _progress(attempt, record)
        raise
    finally:
        try:
            charge_parent("finalization", status=record.get("status", "FAILED"),
                          details={"finalization_margin_seconds": 0.1})
        finally:
            record["additional_cpu_seconds_this_attempt_including_finalization"] = max(
                0.0, diagnostic_charged - prior_diagnostic_cpu,
            )
            record["aggregate_diagnostic_cpu_seconds_including_prior_attempts"] = diagnostic_charged
            record["completed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _progress(attempt, record)

    return 0 if record.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
