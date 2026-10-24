#!/usr/bin/env python3
"""One-shot, source-locked Issue 73 Gate A v4 launcher with a non-executing readiness mode."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import resource
import signal
import subprocess
import sys
import time
import traceback
import unittest
from datetime import datetime, timezone

SCRIPT = pathlib.Path(__file__).resolve()
ATTEMPT_ROOT = SCRIPT.parents[1]
STUDY = SCRIPT.parents[3]
REPOSITORY = STUDY.parents[1]
LOCK_PATH = ATTEMPT_ROOT / "source-lock-v2.json"
LOCK_SHA_PATH = ATTEMPT_ROOT / "source-lock-v2.sha256"
PREFLIGHT_REPORT_PATH = ATTEMPT_ROOT / "preflight-report-v2.json"
PREFLIGHT_START_PATH = ATTEMPT_ROOT / "preflight-started-v2.json"
PREFLIGHT_LEDGER_STARTED_PATH = ATTEMPT_ROOT / "preflight-resource-ledger-started-v2.json"
PREFLIGHT_LEDGER_FINAL_PATH = ATTEMPT_ROOT / "preflight-resource-ledger-final-v2.json"
EXECUTION_START_PATH = ATTEMPT_ROOT / "execution-started-v2.json"
EXECUTION_CLAIM_PATH = ATTEMPT_ROOT / "attempts" / "execution-claim-v2.json"
EXECUTION_TERMINAL_PATH = ATTEMPT_ROOT / "attempts" / "gate-a-terminal-v2.json"
EXECUTION_LEDGER_FINAL_PATH = ATTEMPT_ROOT / "resource-ledger-final-v2.json"
OUTPUT = ATTEMPT_ROOT / "outputs"
RUNTIME = ATTEMPT_ROOT / "runtime"
TMP = RUNTIME / "tmp"
CACHE = RUNTIME / "cache"
PYCACHE = RUNTIME / "pycache"
if str(STUDY) not in sys.path:
    sys.path.insert(0, str(STUDY))

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def canonical_text(payload: bytes, label: str) -> bytes:
    if b"\r" in payload.replace(b"\r\n", b""):
        raise RuntimeError(f"{label} contains bare CR")
    return payload.replace(b"\r\n", b"\n")

def sha256_lf(path: pathlib.Path) -> str:
    return hashlib.sha256(canonical_text(path.read_bytes(), str(path))).hexdigest()

def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))

def write_json_exclusive(path: pathlib.Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())

def git_text(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY), *args],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return result.stdout.strip()

def git_bytes(*args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY), *args],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout

def configure_task_local_runtime() -> None:
    for directory in (TMP, CACHE / "xdg", CACHE / "torch", CACHE / "matplotlib", PYCACHE):
        directory.mkdir(parents=True, exist_ok=True)
    for key, value in {
        "TMPDIR": str(TMP), "TMP": str(TMP), "TEMP": str(TMP),
        "XDG_CACHE_HOME": str(CACHE / "xdg"), "TORCH_HOME": str(CACHE / "torch"),
        "HF_HOME": str(CACHE / "huggingface"), "MPLCONFIGDIR": str(CACHE / "matplotlib"),
        "PYTHONPYCACHEPREFIX": str(PYCACHE), "PYTHONDONTWRITEBYTECODE": "1",
        "CUDA_CACHE_PATH": str(CACHE / "cuda"),
    }.items():
        os.environ[key] = value
    sys.dont_write_bytecode = True
    sys.pycache_prefix = str(PYCACHE)

def verify_source_lock():
    import source_lock_integrity

    lock_raw = canonical_text(LOCK_PATH.read_bytes(), "Gate A v4 source lock")
    expected_lock_sha = LOCK_SHA_PATH.read_text(encoding="ascii").strip().lower()
    observed_lock_sha = hashlib.sha256(lock_raw).hexdigest()
    if observed_lock_sha != expected_lock_sha:
        raise RuntimeError("source-lock-v2.sha256 does not match the canonical lock bytes")
    lock = json.loads(lock_raw)
    if lock.get("attempt_key") != "gate-a-requalification-after-v9-parity-v4":
        raise RuntimeError("unexpected Gate A v4 attempt key")
    head = git_text("rev-parse", "HEAD")
    git_text("merge-base", "--is-ancestor", lock["study_source_commit"], "HEAD")
    git_text("merge-base", "--is-ancestor", lock["launcher_source_commit"], "HEAD")
    branch = git_text("branch", "--show-current")
    if branch and branch != lock["execution_branch"]:
        raise RuntimeError(f"unexpected execution branch: {branch}")
    for entry in lock["locked_source_files"] + lock["locked_launcher_files"]:
        commit = entry["source_commit"]
        path = entry["path"]
        observed_blob = git_text("rev-parse", f"{commit}:{path}")
        if observed_blob != entry["git_blob_sha1"]:
            raise RuntimeError(f"locked Git blob changed: {path}")
        committed = git_bytes("show", f"{commit}:{path}")
        checkout = (REPOSITORY / pathlib.PurePosixPath(path)).read_bytes()
        source_lock_integrity.verify_locked_source_payload(
            label=path,
            expected_sha256=entry["committed_blob_sha256"],
            committed_blob=committed,
            checkout_bytes=checkout,
        )
    verify_parent_evidence(lock)
    return lock, observed_lock_sha, head

def verify_parent_evidence(lock) -> None:
    for item in lock.get("parent_evidence", {}).values():
        path = STUDY / pathlib.PurePosixPath(item["path"])
        if not path.is_file() or sha256_lf(path) != item["sha256"]:
            raise RuntimeError(f"immutable parent evidence changed: {item['path']}")

def verify_preflight_charge(lock, lock_sha: str) -> tuple[dict[str, object], dict[str, object]]:
    started = read_json(PREFLIGHT_START_PATH)
    ledger = read_json(PREFLIGHT_LEDGER_STARTED_PATH)
    if started.get("status") != "STARTED" or started.get("attempt_key") != lock["preflight"]["attempt_key"]:
        raise RuntimeError("Gate A readiness preflight lacks its unique STARTED receipt")
    if ledger.get("status") != "STARTED" or ledger.get("attempt_key") != lock["preflight"]["attempt_key"]:
        raise RuntimeError("Gate A readiness preflight lacks its precharged ledger")
    if started.get("source_lock_sha256") != lock_sha or ledger.get("source_lock_sha256") != lock_sha:
        raise RuntimeError("preflight start receipt/ledger does not bind the candidate source lock")
    if started.get("source_lock_commit") != ledger.get("source_lock_commit"):
        raise RuntimeError("preflight receipt and ledger disagree on the lock commit")
    lock_commit = str(started["source_lock_commit"])
    git_text("merge-base", "--is-ancestor", lock_commit, "HEAD")
    locked_path = str(LOCK_PATH.relative_to(REPOSITORY)).replace("\\", "/")
    if git_text("rev-parse", f"{lock_commit}:{locked_path}") != started.get("source_lock_git_blob_sha1"):
        raise RuntimeError("preflight lock Git blob differs from the precharged receipt")
    if float(started.get("reserved_cpu_seconds", 0)) != 60.0 or float(ledger.get("reservation_cpu_seconds", 0)) != 60.0:
        raise RuntimeError("Gate A readiness preflight requires its fixed 60 CPU-s reservation")
    if float(started.get("builder_charge_after_preflight_cpu_seconds", -1)) != float(started["parent_builder_charge_cpu_seconds"]) + 60.0:
        raise RuntimeError("preflight cumulative charge arithmetic is inconsistent")
    return started, ledger

def verify_gate_a_inputs(lock):
    sys.path.insert(0, str(STUDY))
    import gate_a
    import runner
    import stage0
    import numpy as np
    import torch

    if sys.version.split()[0] != lock["runtime"]["python_version"]:
        raise RuntimeError(f"Python version mismatch: {sys.version.split()[0]}")
    if np.__version__ != lock["runtime"]["numpy_version"] or torch.__version__ != lock["runtime"]["torch_version"]:
        raise RuntimeError("pinned NumPy/Torch runtime mismatch")
    expected_python = pathlib.Path(lock["runtime"]["python_executable"])
    if pathlib.Path(sys.executable).resolve() != expected_python.resolve():
        raise RuntimeError(f"unexpected interpreter: {sys.executable}")
    if os.getuid() != int(lock["runtime"]["execution_uid"]):
        raise RuntimeError("WSL execution UID differs from the reviewed environment")
    if runner.CONFIG_SHA256 != lock["config_sha256"] or runner.CONFIG_LOCK_SHA256 != lock["config_sha256"]:
        raise RuntimeError("config digest or committed config lock mismatch")
    if stage0.source_hashes() != lock["study_source_sha256"]:
        raise RuntimeError("study source map differs from normalized committed source")
    root_path = STUDY / "receipts" / "stage0" / "roots"
    if len(list(root_path.glob("root-*.json"))) != int(lock["root_input_tree"]["file_count"]):
        raise RuntimeError("root input count differs from source lock")
    observed_tree = git_text("rev-parse", f'{lock["study_source_commit"]}:{lock["root_input_tree"]["path"]}')
    if observed_tree != lock["root_input_tree"]["git_tree_sha1"]:
        raise RuntimeError("root input tree differs from source lock")
    roots = stage0.load_roots()
    selected = gate_a.select_corpus(roots)
    selected_ids = [str(row["root_id"]) for row in selected]
    if len(roots) != int(lock["root_input_tree"]["file_count"]):
        raise RuntimeError("loaded root count differs from source lock")
    if selected_ids != lock["root_selection"]["selected_root_ids"]:
        raise RuntimeError("fixed Gate A selected root identities differ from source lock")
    if len(selected) != 24 or len({str(row["screen"]) for row in selected}) < 4:
        raise RuntimeError("Gate A root/family minimum is not met")
    _, target_sha = gate_a._target_direction()
    if target_sha != lock["target"]["canonical_sha256"]:
        raise RuntimeError("frozen target identity differs from source lock")
    native_loaded = [name for name in sys.modules if name == "slaythespire" or name.startswith("slaythespire.")]
    if native_loaded:
        raise RuntimeError(f"non-native Gate A imported simulator modules: {native_loaded}")
    if OUTPUT.exists() and any(OUTPUT.iterdir()):
        raise RuntimeError("Gate A output path already contains data")
    return {
        "python": sys.version.split()[0], "numpy": np.__version__, "torch": torch.__version__,
        "python_executable": str(pathlib.Path(sys.executable).resolve()), "execution_uid": os.getuid(),
        "source_code_commit": lock["study_source_commit"], "launcher_source_commit": lock["launcher_source_commit"],
        "source_lock_sha256": sha256_lf(LOCK_PATH), "root_count": len(roots),
        "selected_root_ids": selected_ids, "selected_root_count": len(selected),
        "selected_screen_family_count": len({str(row["screen"]) for row in selected}),
        "target_sha256": target_sha, "native_module_imported": False,
    }

def run_preflight(record_report: bool) -> int:
    started_cpu = resource.getrusage(resource.RUSAGE_SELF)
    started_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    started_wall = time.time()
    report = {
        "schema_id": "issue73-gate-a-v4-startup-preflight-v2",
        "attempt_key": "builder-gate-a-requalification-v4-startup-preflight-v2",
        "status": "FAIL_EXCEPTION",
        "finished_at_utc": None,
        "formal_seed_consumed": False, "gate_a_invoked": False,
        "optimizer_updated": False, "native_module_imported": False,
        "native_game_step_started": False, "stage1_authorized": False,
        "failure": None,
    }
    try:
        lock, lock_sha, head = verify_source_lock()
        verify_preflight_charge(lock, lock_sha)
        inputs = verify_gate_a_inputs(lock)
        suite = unittest.TestSuite()
        for test_module in ("test_gate_a_target_direction", "test_gate_a_v4_prelaunch"):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(test_module))
        test_result = unittest.TextTestRunner(verbosity=2).run(suite)
        report.update({
            "status": "PASS" if test_result.wasSuccessful() else "FAIL_TESTS",
            "test_count": test_result.testsRun,
            "tests_failed": len(test_result.failures) + len(test_result.errors),
            "test_failure_names": [
                test.id() for test, _ in (test_result.failures + test_result.errors)
            ],
            "inputs": inputs,
            "current_head": head,
            "source_lock_sha256": lock_sha,
            "preflight_reviewer_gate_comment": lock["preflight"]["reviewer_runtime_pass_comment"],
        })
    except BaseException as exc:
        report["failure"] = {
            "error_type": type(exc).__name__, "error": str(exc)[:2000],
            "traceback": traceback.format_exc()[-8000:],
        }
    ended_cpu = resource.getrusage(resource.RUSAGE_SELF)
    ended_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    report["measured_cpu"] = {
        "self_user_seconds": float(ended_cpu.ru_utime - started_cpu.ru_utime),
        "self_system_seconds": float(ended_cpu.ru_stime - started_cpu.ru_stime),
        "children_user_seconds": float(ended_children.ru_utime - started_children.ru_utime),
        "children_system_seconds": float(ended_children.ru_stime - started_children.ru_stime),
    }
    report["measured_cpu"]["process_tree_cpu_seconds"] = sum(report["measured_cpu"].values())
    report["measured_wall_seconds"] = float(time.time() - started_wall)
    report["finished_at_utc"] = utc_now()
    print(json.dumps(report, sort_keys=True))
    if record_report:
        write_json_exclusive(PREFLIGHT_REPORT_PATH, report)
        started = read_json(PREFLIGHT_START_PATH)
        ledger = read_json(PREFLIGHT_LEDGER_STARTED_PATH)
        if started["source_lock_sha256"] != report.get("source_lock_sha256"):
            raise RuntimeError("preflight start receipt does not match the source lock")
        if ledger["source_lock_sha256"] != report.get("source_lock_sha256"):
            raise RuntimeError("preflight ledger does not match the source lock")
        terminal = {
            "schema_id": "issue73-gate-a-v4-startup-preflight-ledger-v1",
            "status": report["status"], "attempt_key": started["attempt_key"],
            "source_lock_path": str(LOCK_PATH.relative_to(STUDY)).replace("\\", "/"),
            "source_lock_sha256": report.get("source_lock_sha256"),
            "preflight_report_path": str(PREFLIGHT_REPORT_PATH.relative_to(STUDY)).replace("\\", "/"),
            "preflight_report_sha256": sha256_lf(PREFLIGHT_REPORT_PATH),
            "parent_builder_charge_cpu_seconds": ledger["parent_builder_charge_cpu_seconds"],
            "reservation_cpu_seconds": ledger["reservation_cpu_seconds"],
            "measured_process_tree_cpu_seconds": report["measured_cpu"]["process_tree_cpu_seconds"],
            "builder_charge_after_preflight_cpu_seconds": ledger["builder_charge_after_preflight_cpu_seconds"],
            "native_gate_bc_charge_cpu_seconds": ledger["native_gate_bc_charge_cpu_seconds"],
            "remaining_fixed_native_work_cpu_seconds": ledger["remaining_fixed_native_work_cpu_seconds"],
            "remaining_native_contingency_cpu_seconds": ledger["remaining_native_contingency_cpu_seconds"],
            "next_gate_a_reservation_cpu_seconds": ledger["next_gate_a_reservation_cpu_seconds"],
            "projected_builder_total_cpu_seconds": ledger["projected_builder_total_cpu_seconds"],
            "projected_all_in_including_reviewer_reserve_cpu_seconds": ledger["projected_all_in_including_reviewer_reserve_cpu_seconds"],
            "within_registered_ceilings": ledger["within_registered_ceilings"],
            "gate_a_invoked": False, "optimizer_updated": False,
            "native_module_imported": False, "native_game_step_started": False,
            "formal_seed_consumed": False, "stage1_authorized": False,
            "finished_at_utc": report["finished_at_utc"],
        }
        write_json_exclusive(PREFLIGHT_LEDGER_FINAL_PATH, terminal)
    return 0 if report["status"] == "PASS" else 2

def execute_gate_a() -> int:
    lock, lock_sha, head = verify_source_lock()
    if not EXECUTION_START_PATH.is_file():
        raise RuntimeError("Gate A execution is not precharged; refusing to start")
    started = read_json(EXECUTION_START_PATH)
    if started.get("status") != "STARTED" or started.get("attempt_key") != lock["attempt_key"]:
        raise RuntimeError("unique Gate A execution is not in its precharged STARTED state")
    if started.get("source_lock_sha256") != lock_sha:
        raise RuntimeError("Gate A precharge source lock differs from the reviewed lock")
    if not started.get("reviewer_preflight_pass_comment"):
        raise RuntimeError("independent Reviewer preflight PASS is required before Gate A")
    if float(started.get("reserved_cpu_seconds", 0)) != 120.0:
        raise RuntimeError("Gate A requires the fixed 120 CPU-s reservation")
    if not PREFLIGHT_REPORT_PATH.is_file():
        raise RuntimeError("passing recorded Gate A readiness preflight is absent")
    report = read_json(PREFLIGHT_REPORT_PATH)
    if started.get("preflight_report_sha256") != sha256_lf(PREFLIGHT_REPORT_PATH) or report.get("status") != "PASS":
        raise RuntimeError("Gate A precharge does not reference the passing preflight report")
    inputs = verify_gate_a_inputs(lock)
    if EXECUTION_CLAIM_PATH.exists():
        raise RuntimeError("Gate A attempt was already claimed; refusing to rerun")
    if EXECUTION_TERMINAL_PATH.exists() or EXECUTION_LEDGER_FINAL_PATH.exists():
        raise RuntimeError("Gate A terminal receipt already exists; refusing to overwrite or rerun")
    if OUTPUT.exists() and any(OUTPUT.iterdir()):
        raise RuntimeError("Gate A outputs exist; refusing to overwrite or rerun")
    fd = os.open(str(EXECUTION_CLAIM_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        json.dump({
            "attempt_key": lock["attempt_key"], "claimed_at_utc": utc_now(),
            "pid": os.getpid(), "source_lock_sha256": lock_sha, "started_head": head,
        }, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())

    import gate_a
    import stage0

    start_cpu = time.process_time()
    start_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    start_wall = time.time()
    result = None
    failure = None
    invoked = False
    try:
        OUTPUT.mkdir(parents=True, exist_ok=False)
        invoked = True
        result = gate_a.run_gate_a(stage0.load_roots(), OUTPUT)
        if not isinstance(result, dict) or not result.get("status"):
            raise RuntimeError("Gate A returned no terminal status")
    except BaseException as exc:
        failure = {
            "error_type": type(exc).__name__, "error": str(exc)[:2000],
            "traceback": traceback.format_exc()[-8000:],
        }
        result = {"status": "FAIL_EXCEPTION", "failure": failure["error"]}
    self_cpu = time.process_time() - start_cpu
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = float((child.ru_utime - start_children.ru_utime) + (child.ru_stime - start_children.ru_stime))
    tree_cpu = float(self_cpu + child_cpu)
    status = str((result or {}).get("status", "FAIL_EXCEPTION"))
    terminal = {
        "schema_id": "issue73-gate-a-attempt-v1", "attempt_key": lock["attempt_key"],
        "status": "COMPLETE" if invoked and failure is None and tree_cpu <= 120.0 else "FAIL_OR_INCOMPLETE",
        "gate_status": status, "source_lock_sha256": lock_sha,
        "source_code_commit": lock["study_source_commit"], "started_head": head,
        "finished_at_utc": utc_now(), "reserved_cpu_seconds": 120.0,
        "charged_cpu_seconds": 120.0, "measured_process_tree_cpu_seconds": tree_cpu,
        "measured_wall_seconds": float(time.time() - start_wall),
        "root_count": inputs["root_count"], "selected_root_ids": inputs["selected_root_ids"],
        "gate_a_invoked": invoked, "formal_seed_consumed": False,
        "native_module_imported": inputs["native_module_imported"],
        "native_game_step_started": False, "stage1_authorized": False,
        "runtime": inputs, "report_sha256": sha256_lf(OUTPUT / "gate-a-report.json") if (OUTPUT / "gate-a-report.json").is_file() else None,
        "corpus_sha256": sha256_lf(OUTPUT / "gate-a-corpus.json") if (OUTPUT / "gate-a-corpus.json").is_file() else None,
        "result": result, "failure": failure,
    }
    write_json_exclusive(EXECUTION_TERMINAL_PATH, terminal)
    ledger_started = read_json(ATTEMPT_ROOT / "resource-ledger-started-v1.json")
    ledger_started.update({
        "status": "TERMINAL", "finished_at_utc": terminal["finished_at_utc"],
        "attempt_terminal_sha256": hashlib.sha256(json.dumps(terminal, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "measured_process_tree_cpu_seconds": tree_cpu, "charged_cpu_seconds": 120.0,
        "builder_charge_after_gate_a_cpu_seconds": ledger_started["parent_builder_charge_cpu_seconds"] + 120.0,
        "gate_a_status": status, "gate_a_invoked": invoked, "formal_seed_consumed": False,
        "native_module_imported": False, "native_game_step_started": False,
        "stage1_authorized": False,
    })
    write_json_exclusive(EXECUTION_LEDGER_FINAL_PATH, ledger_started)
    print(json.dumps({
        "attempt_key": lock["attempt_key"], "status": terminal["status"], "gate_status": status,
        "measured_process_tree_cpu_seconds": tree_cpu, "charged_cpu_seconds": 120.0,
        "formal_seed_consumed": False, "native_module_imported": False,
        "native_game_step_started": False, "stage1_authorized": False,
    }, sort_keys=True))
    return 0 if terminal["status"] == "COMPLETE" else 2

def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--record-preflight", action="store_true")
    args = parser.parse_args()
    soft, hard = (58, 60) if args.preflight_only else (118, 120)
    resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))
    signal.signal(signal.SIGXCPU, lambda _sig, _frame: (_ for _ in ()).throw(TimeoutError(f"CPU reservation reached {soft}s")))
    configure_task_local_runtime()
    if args.preflight_only:
        if args.record_preflight:
            if PREFLIGHT_REPORT_PATH.exists() or PREFLIGHT_LEDGER_FINAL_PATH.exists():
                raise RuntimeError("preflight terminal receipt exists; refusing to overwrite it")
            if not PREFLIGHT_START_PATH.is_file() or not PREFLIGHT_LEDGER_STARTED_PATH.is_file():
                raise RuntimeError("preflight is not precharged; refusing to record execution")
        return run_preflight(args.record_preflight)
    if args.record_preflight:
        raise RuntimeError("--record-preflight is valid only with --preflight-only")
    return execute_gate_a()

if __name__ == "__main__":
    raise SystemExit(main())