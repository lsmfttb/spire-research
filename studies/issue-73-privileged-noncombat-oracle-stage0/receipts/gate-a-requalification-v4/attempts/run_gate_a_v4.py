#!/usr/bin/env python3
"""One-shot, source-locked Issue 73 Gate A requalification v4."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import resource
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

SCRIPT = pathlib.Path(__file__).resolve()
ATTEMPT_ROOT = SCRIPT.parents[1]
STUDY = SCRIPT.parents[3]
REPOSITORY = STUDY.parents[1]
OUTPUT = ATTEMPT_ROOT / "outputs"
RUNTIME = ATTEMPT_ROOT / "runtime"
TMP = RUNTIME / "tmp"
CACHE = RUNTIME / "cache"
PYCACHE = RUNTIME / "pycache"
for directory in (TMP, CACHE, PYCACHE, CACHE / "xdg", CACHE / "torch", CACHE / "matplotlib"):
    directory.mkdir(parents=True, exist_ok=True)
for key, value in {
    "TMPDIR": str(TMP), "TEMP": str(TMP), "TMP": str(TMP),
    "XDG_CACHE_HOME": str(CACHE / "xdg"), "TORCH_HOME": str(CACHE / "torch"),
    "HF_HOME": str(CACHE / "huggingface"), "MPLCONFIGDIR": str(CACHE / "matplotlib"),
    "PYTHONPYCACHEPREFIX": str(PYCACHE), "PYTHONDONTWRITEBYTECODE": "1",
    "CUDA_CACHE_PATH": str(CACHE / "cuda"),
}.items():
    os.environ[key] = value
sys.dont_write_bytecode = True
sys.pycache_prefix = str(PYCACHE)

START_CPU = time.process_time()
START_WALL = time.time()
PREFLIGHT_ONLY = "--preflight-only" in sys.argv
SOFT_CPU, HARD_CPU = (58, 60) if PREFLIGHT_ONLY else (118, 120)
resource.setrlimit(resource.RLIMIT_CPU, (SOFT_CPU, HARD_CPU))

class CpuReservationLimit(Exception):
    pass

def on_cpu_limit(_signum, _frame):
    raise CpuReservationLimit(f"Gate A reached its {SOFT_CPU}-CPU-second soft watchdog")

signal.signal(signal.SIGXCPU, on_cpu_limit)

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def lf_bytes(path: pathlib.Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")

def sha256_lf(path: pathlib.Path) -> str:
    return hashlib.sha256(lf_bytes(path)).hexdigest()

def write_json(path: pathlib.Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".writing")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    os.replace(temp, path)

def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))

attempt_path = ATTEMPT_ROOT / "attempts" / "gate-a-requalification-after-v9-parity-v4.json"
source_lock_path = ATTEMPT_ROOT / "source-lock.json"
claim_path = ATTEMPT_ROOT / "attempts" / "execution-claim.json"
ledger_started_path = ATTEMPT_ROOT / "resource-ledger-started.json"
ledger_final_path = ATTEMPT_ROOT / "resource-ledger-final.json"
report_path = OUTPUT / "gate-a-report.json"
corpus_path = OUTPUT / "gate-a-corpus.json"
wrapper_claim_time = utc_now()
result = None
failure = None
execution_entered = False
runtime_info = {}
try:
    sys.path.insert(0, str(STUDY))
    import numpy as np
    import torch
    import runner
    import stage0
    import gate_a

    source_lock = read_json(source_lock_path)
    if source_lock.get("attempt_key") != "gate-a-requalification-after-v9-parity-v4":
        raise RuntimeError("source lock does not identify the unique v4 Gate A attempt")
    if sha256_lf(SCRIPT) != source_lock.get("execution_wrapper_sha256"):
        raise RuntimeError("one-shot wrapper differs from its source lock")
    if runner.CONFIG_SHA256 != source_lock.get("config_sha256"):
        raise RuntimeError("configuration digest differs from the source lock")
    if runner.CONFIG_SHA256 != runner.CONFIG_LOCK_SHA256:
        raise RuntimeError("config.json does not match config.lock.sha256")
    if stage0.source_hashes() != dict(runner.CONFIG["identity"]["study_source_sha256"]):
        raise RuntimeError("study-local source hashes differ from the committed config")
    locked_text_files = source_lock.get("locked_text_files_sha256", {})
    if not isinstance(locked_text_files, dict) or not locked_text_files:
        raise RuntimeError("source lock does not enumerate the locked text files")
    for relative_path, expected_sha256 in locked_text_files.items():
        path = STUDY / str(relative_path)
        if not path.is_file() or sha256_lf(path) != expected_sha256:
            raise RuntimeError(f"locked source file differs from source lock: {relative_path}")
    preflight_root = STUDY / "receipts" / "gate-a-requalification-v3-preflight"
    preflight_lock_path = preflight_root / "source-lock-v7.json"
    preflight_report_path = preflight_root / "preflight-report-v7.json"
    parent_ledger_path = preflight_root / "resource-ledger-final-v7.json"
    if sha256_lf(preflight_lock_path) != source_lock.get("preflight_source_lock_v7_sha256"):
        raise RuntimeError("independently reviewed preflight source lock v7 changed")
    if not PREFLIGHT_ONLY:
        if sha256_lf(preflight_report_path) != source_lock.get("preflight_report_v7_sha256"):
            raise RuntimeError("preflight report v7 changed after review")
        if sha256_lf(parent_ledger_path) != source_lock.get("parent_resource_ledger_sha256"):
            raise RuntimeError("parent resource ledger changed after precharge")
    root_source = source_lock.get("root_source", {})
    producer_config_sha256 = root_source.get("producer_config_sha256")
    if producer_config_sha256 not in runner.CONFIG["identity"].get("historical_root_config_sha256s", []):
        raise RuntimeError("historical root producer config is not explicitly listed in the locked config")
    root_dir = STUDY / "receipts" / "stage0" / "roots"
    root_files = sorted(root_dir.glob("root-*.json"))
    expected_roots = root_source.get("files_sha256", {})
    if len(root_files) != int(root_source.get("file_count", -1)) or set(p.name for p in root_files) != set(expected_roots):
        raise RuntimeError("immutable root input membership differs from the source lock")
    for path in root_files:
        if sha256_lf(path) != expected_roots[path.name]:
            raise RuntimeError(f"immutable root input differs from the source lock: {path.name}")
    roots = stage0.load_roots()
    if len(roots) != int(root_source["file_count"]):
        raise RuntimeError("loaded root count differs from the immutable source lock")
    selected = gate_a.select_corpus(roots)
    root_ids = [str(row["root_id"]) for row in selected]
    if root_ids != source_lock["root_selection"]["selected_root_ids"]:
        raise RuntimeError("selected root IDs differ from the immutable Gate A v2 selection")
    if len(selected) != 24 or len({str(row["screen"]) for row in selected}) < 4:
        raise RuntimeError("fixed 24-root / four-screen Gate A contract is not met")
    native_module_names = [name for name in sys.modules if name == "slaythespire" or name.startswith("slaythespire.")]
    if native_module_names:
        raise RuntimeError(f"Gate A is non-native but simulator module was imported: {native_module_names}")
    runtime_info = {
        "python": sys.version.split()[0], "numpy": np.__version__, "torch": torch.__version__,
        "platform": sys.platform, "execution_user_uid": os.getuid(),
        "native_module_imported": False, "native_game_step_started": False,
        "formal_seed_consumed": False, "stage1_authorized": False,
    }
    if runtime_info["python"] != "3.13.13" or np.__version__ != "2.4.6" or torch.__version__ != "2.12.0+cpu":
        raise RuntimeError(f"pinned Python/NumPy/Torch runtime mismatch: {runtime_info}")
    if runtime_info["execution_user_uid"] != 0:
        raise RuntimeError("this locked WSL-mounted task worktree requires the previously qualified root execution user")
    if PREFLIGHT_ONLY:
        print(json.dumps({"status": "PRELAUNCH_PASS", "root_count": len(roots), "selected_root_count": len(selected), "config_sha256": runner.CONFIG_SHA256, "root_producer_config_sha256": producer_config_sha256, "native_module_imported": False, "native_game_step_started": False, "formal_seed_consumed": False, "stage1_authorized": False}, sort_keys=True))
        raise SystemExit(0)
    started = read_json(attempt_path)
    started_ledger = read_json(ledger_started_path)
    if started.get("status") != "STARTED" or started.get("key") != "gate-a-requalification-after-v9-parity-v4":
        raise RuntimeError("unique v4 attempt is not in its precharged STARTED state")
    if sha256_lf(source_lock_path) != started.get("source_lock_sha256"):
        raise RuntimeError("v4 source lock SHA-256 differs from the committed attempt receipt")
    if started_ledger.get("attempt", {}).get("source_lock_sha256") != started.get("source_lock_sha256"):
        raise RuntimeError("precharged resource ledger and attempt receipt do not share a source lock")
    try:
        fd = os.open(str(claim_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump({"attempt_key": "gate-a-requalification-after-v9-parity-v4", "claimed_at_utc": wrapper_claim_time, "pid": os.getpid(), "worktree": str(REPOSITORY), "source_code_commit": source_lock.get("source_code_commit")}, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise RuntimeError("refusing to rerun an already-claimed Gate A attempt")
    if OUTPUT.exists() and any(OUTPUT.iterdir()):
        raise RuntimeError("new Gate A v4 output directory is not empty; refusing to overwrite evidence")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    execution_entered = True
    result = gate_a.run_gate_a(roots, OUTPUT)
    if not isinstance(result, dict) or not result.get("status"):
        raise RuntimeError("Gate A returned no terminal status")
except SystemExit:
    raise
except BaseException as exc:
    if PREFLIGHT_ONLY:
        print(json.dumps({"status": "PRELAUNCH_FAIL", "error_type": type(exc).__name__, "error": str(exc)[:2000], "formal_seed_consumed": False, "stage1_authorized": False}, sort_keys=True), file=sys.stderr)
        raise SystemExit(2)
    failure = {
        "error_type": type(exc).__name__,
        "error": str(exc)[:2000],
        "traceback": traceback.format_exc()[-8000:],
    }
    result = {
        "schema_id": "issue73-gate-a-policy-response-v1",
        "status": "FAIL_EXCEPTION",
        "failure_type": failure["error_type"],
        "failure": failure["error"],
        "target_reselected": False,
        "formal_training_or_evaluation_seeds_consumed": False,
    }

finished_cpu = time.process_time()
children = resource.getrusage(resource.RUSAGE_CHILDREN)
child_cpu = float(children.ru_utime + children.ru_stime)
tree_cpu = float(finished_cpu + child_cpu)
wall_seconds = float(time.time() - START_WALL)
gate_status = str((result or {}).get("status", "FAIL_EXCEPTION"))
completed = bool(execution_entered and failure is None and report_path.exists() and corpus_path.exists())
attempt_status = "COMPLETE" if completed and tree_cpu <= HARD_CPU else "FAIL_OR_INCOMPLETE"
if tree_cpu > HARD_CPU:
    gate_status = "FAIL_CPU_RESERVATION_EXCEEDED"
    result["status"] = gate_status
    result["cpu_reservation_seconds"] = HARD_CPU
attempt = read_json(attempt_path)
attempt.update({
    "status": attempt_status,
    "gate_status": gate_status,
    "source_code_commit": source_lock.get("source_code_commit"),
    "execution_branch": source_lock.get("execution_branch"),
    "execution_commit": "STARTED_COMMIT_RECORDED_IN_FINAL_HANDOFF",
    "finished_at_utc": utc_now(),
    "measured_process_cpu_seconds": float(finished_cpu),
    "measured_child_process_cpu_seconds": child_cpu,
    "measured_process_tree_cpu_seconds": tree_cpu,
    "measured_wall_seconds": wall_seconds,
    "cpu_charged_seconds": 120.0,
    "measurement_status": "FIXED_120_CPU_SECOND_PRECHARGE_NO_REFUND",
    "root_count": 225,
    "selected_root_ids": source_lock.get("root_selection", {}).get("selected_root_ids", []),
    "gate_a_invoked": execution_entered,
    "formal_seed_consumed": False,
    "native_adapter_instantiated": False,
    "native_module_imported": False,
    "native_game_step_started": False,
    "stage1_authorized": False,
    "runtime": runtime_info,
    "execution_claim_sha256": sha256_lf(claim_path),
    "gate_report_sha256": sha256_lf(report_path) if report_path.is_file() else None,
    "gate_corpus_sha256": sha256_lf(corpus_path) if corpus_path.is_file() else None,
    "failure": failure,
})
write_json(attempt_path, attempt)
base = read_json(ledger_started_path)
base.update({
    "status": "TERMINAL" if attempt_status in {"COMPLETE", "FAIL_OR_INCOMPLETE"} else "INCOMPLETE",
    "finished_at_utc": attempt["finished_at_utc"],
    "attempt": attempt,
    "measured_process_cpu_seconds": float(finished_cpu),
    "measured_child_process_cpu_seconds": child_cpu,
    "measured_process_tree_cpu_seconds": tree_cpu,
    "cpu_charged_seconds": 120.0,
    "builder_charge_after_attempt_cpu_seconds": base.get("builder_charge_after_attempt_cpu_seconds"),
    "projected_builder_total_cpu_seconds": base.get("projected_builder_total_cpu_seconds"),
    "projected_native_total_cpu_seconds": base.get("projected_native_total_cpu_seconds"),
    "projected_all_in_cpu_seconds": base.get("projected_all_in_cpu_seconds"),
    "within_registered_ceilings": True,
    "formal_seeds_consumed": False,
    "stage1_authorized": False,
})
write_json(ledger_final_path, base)
if not report_path.exists():
    write_json(report_path, result)
print(json.dumps({"gate_status": gate_status, "attempt_status": attempt_status, "process_tree_cpu_seconds": tree_cpu, "charged_cpu_seconds": 120.0, "root_count": 225, "selected_root_count": 24, "formal_seed_consumed": False, "native_module_imported": False, "native_game_step_started": False, "stage1_authorized": False}, sort_keys=True))
raise SystemExit(0 if attempt_status == "COMPLETE" else 2)
