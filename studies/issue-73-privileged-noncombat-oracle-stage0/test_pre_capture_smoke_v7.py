"""Production-launcher positive and fail-closed v7 preflight qualification."""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import checkpoint_replay_qualification as q
import runner
import source_qualification_v7 as v7


CLI = v7.STUDY / "checkpoint_replay_qualification.py"
ATTEMPT_PATH = v7.V7_OUT / "attempts" / f"{v7.CAPTURE_ATTEMPT_KEY}.json"


def _cpu_now() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return float(usage.ru_utime + usage.ru_stime)


def _preexec_cpu_cap(remaining_seconds: float) -> Any:
    hard = max(2, min(60, int(remaining_seconds)))
    soft = max(1, hard - 2)

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))

    return apply


def _write_json_tamper(path: Path, mutate: Callable[[dict[str, Any]], None]) -> bytes:
    original = path.read_bytes()
    value = json.loads(original.decode("utf-8"))
    mutate(value)
    runner.atomic_json(path, value)
    return original


def _case(
    name: str,
    *,
    expected_success: bool,
    expected_failure_fragment: str | None = None,
    extra_args: tuple[str, ...] = (),
    mutation: tuple[Path, Callable[[dict[str, Any]], None]] | None = None,
    mutation_bytes: tuple[Path, Callable[[bytes], bytes]] | None = None,
    missing_file: Path | None = None,
    remaining_cpu_seconds: float,
) -> dict[str, Any]:
    case_dir = v7.PREFLIGHT_REPAIR_TEST_ROOT / name
    report_path = case_dir / "capture-launch-preflight-v7.json"
    failure_path = case_dir / "capture-launch-preflight-v7-failure.json"
    if report_path.exists() or failure_path.exists():
        raise RuntimeError(f"immutable preflight case {name!r} already has a receipt; no retry/overwrite")
    if case_dir.exists() and any(case_dir.iterdir()):
        raise RuntimeError(f"preflight case {name!r} has partial outputs; preserve and stop")
    if ATTEMPT_PATH.exists():
        raise RuntimeError("a v7 source-history capture attempt exists before preflight review; stop")

    original_bytes: bytes | None = None
    if mutation is not None:
        target, change = mutation
        original_bytes = _write_json_tamper(target, change)
    elif mutation_bytes is not None:
        target, change_bytes = mutation_bytes
        original_bytes = target.read_bytes()
        target.write_bytes(change_bytes(original_bytes))
    elif missing_file is not None:
        original_bytes = missing_file.read_bytes()
        missing_file.unlink()

    started_wall = time.monotonic()
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--qualification-version",
                "v7",
                "capture",
                "--preflight-only",
                "--preflight-report-dir",
                str(case_dir),
                *extra_args,
            ],
            cwd=v7.REPOSITORY,
            env=os.environ.copy(),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
            preexec_fn=_preexec_cpu_cap(remaining_cpu_seconds),
        )
    finally:
        if original_bytes is not None:
            if mutation is not None:
                mutation[0].write_bytes(original_bytes)
            elif mutation_bytes is not None:
                mutation_bytes[0].write_bytes(original_bytes)
            elif missing_file is not None:
                missing_file.write_bytes(original_bytes)
    wall = max(0.0, time.monotonic() - started_wall)
    if ATTEMPT_PATH.exists():
        raise AssertionError(f"preflight case {name!r} registered a native capture attempt")

    if expected_success:
        if proc.returncode != 0 or not report_path.is_file() or failure_path.exists():
            raise AssertionError(
                f"preflight case {name!r} should pass; exit={proc.returncode}, stderr={proc.stderr[-1200:]}"
            )
        receipt = json.loads(report_path.read_text(encoding="utf-8"))
        if receipt.get("status") != "PASS":
            raise AssertionError(f"positive preflight case {name!r} has no PASS receipt")
        boundary = receipt.get("capture_entry_path", {})
        forbidden = (
            "start_attempt_invoked",
            "native_adapter_instantiated",
            "native_game_step_started",
            "debug_owner_started",
            "formal_seed_consumed",
            "native_debit_created",
        )
        if any(boundary.get(key) is not False for key in forbidden):
            raise AssertionError(f"positive preflight case {name!r} crossed the no-native-attempt boundary")
    else:
        if proc.returncode == 0 or report_path.exists() or not failure_path.is_file():
            raise AssertionError(
                f"preflight case {name!r} should fail closed; exit={proc.returncode}, stderr={proc.stderr[-1200:]}"
            )
        receipt = json.loads(failure_path.read_text(encoding="utf-8"))
        if expected_failure_fragment and expected_failure_fragment not in str(receipt.get("failure", "")):
            raise AssertionError(
                f"preflight case {name!r} failed for an unexpected reason: {receipt.get('failure')}"
            )
        forbidden = (
            "start_attempt_invoked",
            "native_adapter_instantiated",
            "native_game_step_started",
            "formal_seed_consumed",
            "native_debit_created",
        )
        if any(receipt.get(key) is not False for key in forbidden):
            raise AssertionError(f"negative preflight case {name!r} crossed the no-native-attempt boundary")
    if mutation is not None and mutation[0].read_bytes() != original_bytes:
        raise AssertionError(f"negative case {name!r} did not restore its source/config/plan bytes")
    if mutation_bytes is not None and mutation_bytes[0].read_bytes() != original_bytes:
        raise AssertionError(f"negative case {name!r} did not restore its source/optimizer bytes")
    if missing_file is not None and missing_file.read_bytes() != original_bytes:
        raise AssertionError(f"negative case {name!r} did not restore its historical input bytes")
    return {
        "case": name,
        "expected": "PASS" if expected_success else "FAIL_CLOSED",
        "observed": receipt.get("status", "FAIL_OR_INCOMPLETE"),
        "launcher_exit_code": proc.returncode,
        "failure": receipt.get("failure"),
        "measured_process_tree_cpu_seconds": float(receipt.get("measured_process_tree_cpu_seconds", 0.0)),
        "measured_wall_seconds": wall,
        "native_attempt_registered": False,
        "adapter_or_step_started": False,
        "formal_seed_consumed": False,
    }


def _mutate_first(items: list[Any]) -> None:
    if not items:
        raise AssertionError("locked list is unexpectedly empty")
    items[0] = "__v7_test_mutation__"


def _run() -> dict[str, Any]:
    if v7.STATIC_TEST_REPORT_PATH.exists() or v7.STATIC_TEST_REPAIR_FAILURE_PATH.exists():
        raise RuntimeError("v7 production preflight suite has an immutable receipt; no retry or overwrite")
    reservation, ledger = v7.ensure_static_precharge_v7()
    if reservation["reserved_cpu_seconds"] != 60.0:
        raise AssertionError("v7 static reservation differs from the one-time 60 CPU-s authorization")
    prelock_path = v7.INPUT_AUDIT_REPAIR_REPORT_PATH
    if not prelock_path.is_file():
        raise RuntimeError("v7 pre-lock manifest qualification report is missing")
    prelock = v7.read_json(prelock_path)
    if prelock.get("status") != "PASS":
        raise RuntimeError("v7 pre-lock manifest qualification did not pass")
    lock, config = v7.verify_lock_v7()
    if ATTEMPT_PATH.exists():
        raise RuntimeError("native source-history capture attempt already exists; preflight cannot continue")
    if v7.OPTIONAL_RUNTIME_OUTPUT and (v7.REPOSITORY / v7.OPTIONAL_RUNTIME_OUTPUT).exists():
        raise AssertionError("optional historical runtime output was fabricated during qualification")

    started_cpu = _cpu_now()
    prior_cpu = float(prelock["resource_accounting"]["conservative_sum_cpu_seconds"])
    cases: list[dict[str, Any]] = []

    def add_case(*args: Any, **kwargs: Any) -> None:
        next_cpu = prior_cpu + (_cpu_now() - started_cpu) + sum(
            float(row["measured_process_tree_cpu_seconds"]) for row in cases
        )
        if next_cpu >= v7.STATIC_RESERVATION_CPU_SECONDS - 2.0:
            raise RuntimeError(f"60 CPU-s static reservation is nearly exhausted before {args[0]!r}: {next_cpu:.3f}")
        cases.append(_case(*args, remaining_cpu_seconds=max(2.0, v7.STATIC_RESERVATION_CPU_SECONDS - next_cpu - 2.0), **kwargs))
        measured = prior_cpu + (_cpu_now() - started_cpu) + sum(
            float(row["measured_process_tree_cpu_seconds"]) for row in cases
        )
        if measured >= v7.STATIC_RESERVATION_CPU_SECONDS:
            raise RuntimeError(f"60 CPU-s static reservation was exceeded: {measured:.3f}")

    add_case("production-positive", expected_success=True)
    add_case(
        "missing-mandatory-input",
        expected_success=False,
        expected_failure_fragment="mandatory historical inputs are missing",
        missing_file=v7.REPOSITORY / v7.REQUIRED_INPUT_ANCHORS[-2],
    )
    add_case(
        "mutated-source",
        expected_success=False,
        expected_failure_fragment="checkout differs from the exact committed Git blob",
        mutation_bytes=(v7.STUDY / "policy.py", lambda payload: payload + b"\n# temporary source-lock negative test\n"),
    )
    add_case(
        "mutated-optimizer",
        expected_success=False,
        expected_failure_fragment="checkout differs from the exact committed Git blob",
        mutation_bytes=(
            v7.STUDY / "public_history_execution.py",
            lambda payload: payload + b"\n# temporary optimizer-lock negative test\n",
        ),
    )

    add_case(
        "wrong-native-module",
        expected_success=False,
        expected_failure_fragment="differs from the exact prospective v7 lock",
        extra_args=("--native-module-path", str(v7.STUDY / "runner.py")),
    )
    add_case(
        "missing-native-module",
        expected_success=False,
        expected_failure_fragment="native module path override is unavailable",
        extra_args=("--native-module-path", str(v7.V7_OUT / "missing-native-module.so")),
    )
    config_path = v7.CONFIG_PATH
    add_case(
        "mutated-gate-a-roots",
        expected_success=False,
        expected_failure_fragment="qualification config changed after source lock",
        mutation=(config_path, lambda value: _mutate_first(value["gate_a_root_ids"])),
    )
    add_case(
        "mutated-gate-b-witness-roots",
        expected_success=False,
        expected_failure_fragment="qualification config changed after source lock",
        mutation=(config_path, lambda value: value["gate_b_witness_root_ids"].update({"hidden_semantic": "__mutated__"})),
    )
    plan_path = v7.PLAN_PATH
    add_case(
        "mutated-gate-c-roots",
        expected_success=False,
        expected_failure_fragment="Gate C roots/actions/streams plan changed after source lock",
        mutation=(plan_path, lambda value: value["roots"][0].update({"root_id": "__mutated__"})),
    )
    add_case(
        "mutated-gate-c-actions",
        expected_success=False,
        expected_failure_fragment="Gate C roots/actions/streams plan changed after source lock",
        mutation=(plan_path, lambda value: value["roots"][0]["action_indices"].__setitem__(0, 999)),
    )
    add_case(
        "mutated-gate-c-streams",
        expected_success=False,
        expected_failure_fragment="Gate C roots/actions/streams plan changed after source lock",
        mutation=(plan_path, lambda value: _mutate_first(value["policy_rng_seed_streams"])),
    )

    if ATTEMPT_PATH.exists():
        raise AssertionError("preflight suite unexpectedly registered a native capture attempt")
    measured_total = prior_cpu + (_cpu_now() - started_cpu) + sum(
        float(row["measured_process_tree_cpu_seconds"]) for row in cases
    )
    if measured_total >= v7.STATIC_RESERVATION_CPU_SECONDS:
        raise AssertionError(f"v7 static qualification exceeded its 60 CPU-s reserve: {measured_total:.3f}")
    report = {
        "schema_id": "issue73-v7-static-input-manifest-and-capture-preflight-v1",
        "status": "PASS" if all(row["observed"] == ("PASS" if row["expected"] == "PASS" else "FAIL_OR_INCOMPLETE") for row in cases) else "FAIL",
        "source_lock_sha256": v7.sha256_checkout_text_file(v7.LOCK_PATH),
        "prior_static_failure": {
            "path": v7.STATIC_TEST_FAILURE_PATH.relative_to(v7.STUDY).as_posix(),
            "sha256": v7.sha256_file(v7.STATIC_TEST_FAILURE_PATH),
            "status": v7.read_json(v7.STATIC_TEST_FAILURE_PATH).get("status"),
            "measured_process_tree_cpu_seconds": v7.read_json(v7.STATIC_TEST_FAILURE_PATH).get("measured_process_tree_cpu_seconds"),
            "preserved_unchanged": True,
        },
        "source_commit": lock["code_source_commit"],
        "remote_branch": lock["remote_branch"],
        "historical_input_count": len(lock["historical_input_sha256"]),
        "historical_input_manifest_sha256": lock["historical_input_manifest_sha256"],
        "production_capture_preflight_in_separate_process": True,
        "cases": cases,
        "native_capture_attempt_registered": False,
        "native_adapter_instantiated": False,
        "native_game_step_started": False,
        "debug_owner_started": False,
        "formal_seed_consumed": False,
        "resource_accounting": {
            "prior_builder_charge_cpu_seconds": 2137.891659,
            "one_time_static_reservation_cpu_seconds": 60.0,
            "measured_prelock_phase_cpu_seconds": prior_cpu,
            "measured_preflight_phase_cpu_seconds": measured_total - prior_cpu,
            "measured_total_static_qualification_cpu_seconds": measured_total,
            "static_reservation_fully_charged_without_refund": True,
            "projected_builder_cpu_seconds_after_max_remaining_stage0_reserve": 3817.891659,
            "projected_native_gate_bc_cpu_seconds_after_max_reserve": 2760.0,
            "reviewer_reserve_cpu_seconds": 1200.0,
            "aggregate_stage0_cpu_ceiling_seconds": 6000.0,
        },
        "limitations": [
            "No native source-history capture was registered or run.",
            "No parity replay, Gate A/B/C execution, formal owner seed, or Stage 1 training was run.",
            "Independent Reviewer PASS is required before the one separately authorized 60 CPU-s native capture attempt.",
        ],
    }
    if report["status"] != "PASS":
        raise AssertionError("one or more v7 production preflight cases failed their registered expectation")
    return report


def main() -> int:
    q.install_process_cpu_ceiling(v7.STATIC_RESERVATION_CPU_SECONDS)
    if "STS_LIGHTSPEED_SOURCE_DIR" not in os.environ or "STSRL_SOURCE_DIR" not in os.environ:
        raise RuntimeError("exact pinned external source worktree environment is required")
    if v7.STATIC_TEST_REPAIR_FAILURE_PATH.exists():
        raise RuntimeError("immutable v7 static repair qualification failure already exists; no rerun")
    try:
        report = _run()
    except BaseException as exc:
        failure = {
            "schema_id": "issue73-v7-static-input-manifest-and-capture-preflight-failure-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:2000],
            "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU),
            "fixed_static_reservation_cpu_seconds": v7.STATIC_RESERVATION_CPU_SECONDS,
            "no_refund": True,
            "no_retry_or_overwrite": True,
        }
        runner.atomic_json(v7.STATIC_TEST_REPAIR_FAILURE_PATH, failure)
        v7.push_v7("Issue 73: preserve v7 static repair qualification failure")
        raise
    runner.atomic_json(v7.STATIC_TEST_REPORT_PATH, report)
    v7.push_v7("Issue 73: preserve v7 static input manifest and launcher preflight")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
