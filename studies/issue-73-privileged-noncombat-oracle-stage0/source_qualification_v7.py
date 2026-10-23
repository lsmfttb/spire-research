from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import checkpoint_replay_qualification as q
import runner


STUDY = q.STUDY
REPOSITORY = q.REPOSITORY
STUDY_RELATIVE = q.STUDY_RELATIVE
V7_OUT = STUDY / "receipts" / "source-qualification-v7"
V7_RELATIVE = V7_OUT.relative_to(REPOSITORY).as_posix()
BUILDER_RUNTIME_ROOT = REPOSITORY / ".issue73-builder-runtime-v7"
# Keep the reviewed v7 lock and receipts immutable.  The repaired checker gets a
# distinct prospective lock lineage after its source code is pushed.
V7_LOCK_OUT = V7_OUT / "autocrlf-lock-repair1"
CONFIG_PATH = V7_LOCK_OUT / "qualification-config-v7.json"
PLAN_PATH = V7_LOCK_OUT / "plans" / "gate-c-replay-plan-v7.json"
LOCK_PATH = V7_LOCK_OUT / "preseed-source-lock-v7.json"
INPUT_MANIFEST_PATH = V7_LOCK_OUT / "input-manifest-v7.json"
V7_CAPTURE_LOCK_OUT = V7_OUT / "native-capture-repair1"
V7_CAPTURE_PREFLIGHT_ROOT = V7_OUT / "reports" / "native-capture-preflight-repair1"
V7_CAPTURE_PREFLIGHT_REPORT_PATH = V7_CAPTURE_PREFLIGHT_ROOT / "capture-launch-preflight-v7.json"
V7_CAPTURE_PREFLIGHT_FAILURE_PATH = V7_CAPTURE_PREFLIGHT_ROOT / "capture-launch-preflight-v7-failure.json"
V7_CAPTURE_NATIVE_PREFLIGHT_FAILURE_PATH = V7_OUT / "reports" / "source-history-capture-preflight-failure-v7.json"
V7_CAPTURE_RUNTIME_OUT = V7_OUT / "capture-runtime-v7"
V7_CAPTURE_HISTORY_PATH = V7_OUT / "histories" / "actual-action-step-history-v7.json"
V7_CAPTURE_STAGING_PATH = V7_OUT / "reports" / "source-history-capture-staging-v7.json"
V7_CAPTURE_REPORT_PATH = V7_OUT / "reports" / "source-history-capture-report-v7.json"
V7_CAPTURE_FAILURE_PATH = V7_OUT / "reports" / "source-history-capture-failure-v7.json"
RESOURCE_LEDGER_PATH = V7_OUT / "resource-ledger.json"
RESOURCE_SNAPSHOT_PATH = V7_OUT / "ledgers" / "resource-ledger-before-v7.json"
STATIC_RESERVATION_PATH = V7_OUT / "attempts" / "builder-prelock-static-verification-v7.json"
STATIC_RESERVATION_KEY = "builder-prelock-static-verification-v7"
STATIC_RESERVATION_CPU_SECONDS = 60.0
CAPTURE_ATTEMPT_KEY = "source-history-capture-173730001-prospective-v7"
V7_CAPTURE_ATTEMPT_PATH = V7_OUT / "attempts" / f"{CAPTURE_ATTEMPT_KEY}.json"
V7_STATIC_CHARGE_PATHS = {
    "native-capture-implementation-repair1": V7_OUT / "reports" / "native-capture-implementation-cpu-charge-v7.json",
    "native-capture-lock-preparation-repair1": V7_OUT / "reports" / "native-capture-lock-preparation-cpu-charge-v7.json",
    "native-capture-preflight-repair1": V7_CAPTURE_PREFLIGHT_ROOT / "capture-preflight-cpu-charge-v7.json",
    "native-capture-inline-preflight-repair1": V7_OUT / "reports" / "native-capture-inline-preflight-cpu-charge-v7.json",
}

HISTORICAL_INPUT_BASE_COMMIT = "80729ed02e3af698a71d33538169f7aa31209457"
HISTORICAL_PREFIXES = tuple(
    f"{STUDY_RELATIVE}/receipts/{name}/"
    for name in (
        "stage0",
        "source-qualification-v1",
        "source-qualification-v4",
        "source-qualification-v5",
        "source-qualification-v6",
    )
)
OPTIONAL_RUNTIME_OUTPUT = (
    f"{STUDY_RELATIVE}/receipts/source-qualification-v6/"
    "smoke-runtime/runtime-metrics.jsonl"
)
REQUIRED_INPUT_ANCHORS = (
    f"{STUDY_RELATIVE}/receipts/stage0/preseed-source-lock-v3.json",
    f"{STUDY_RELATIVE}/receipts/stage0/resource-ledger.json",
    f"{STUDY_RELATIVE}/receipts/stage0/debug-owner-runs/173730001.json",
    f"{STUDY_RELATIVE}/receipts/stage0/attempts/debug-owner-173730001.json",
    f"{STUDY_RELATIVE}/receipts/source-qualification-v5/resource-ledger.json",
    f"{STUDY_RELATIVE}/receipts/source-qualification-v6/qualification-config-v6.json",
    f"{STUDY_RELATIVE}/receipts/source-qualification-v6/plans/gate-c-replay-plan-v6.json",
    f"{STUDY_RELATIVE}/receipts/source-qualification-v6/resource-ledger.json",
    f"{STUDY_RELATIVE}/receipts/source-qualification-v6/reports/v6-static-qualification-failure.json",
    f"{STUDY_RELATIVE}/receipts/source-qualification-v6/reports/pre-capture-smoke-v6.json",
)
SOURCE_HASH_PATHS_V7 = (
    "checkpoint_replay_qualification.py",
    "config.json",
    "config.lock.sha256",
    "gate-a-target-direction.json",
    "gate_a.py",
    "policy.py",
    "public_history_execution.py",
    "runner.py",
    "source_lock_integrity.py",
    "source_qualification_v7.py",
    "stage0.py",
    "test_autocrlf_lock_v7.py",
    "test_native_capture_v7.py",
    "test_pre_capture_smoke_v7.py",
    "test_source_manifest_v7.py",
)
STATIC_TEST_REPORT_PATH = V7_OUT / "reports" / "static-qualification-report-v7.json"
STATIC_TEST_FAILURE_PATH = V7_OUT / "reports" / "static-qualification-failure-v7.json"
STATIC_TEST_REPAIR_FAILURE_PATH = V7_OUT / "reports" / "static-qualification-repair1-failure-v7.json"
INPUT_AUDIT_REPORT_PATH = V7_OUT / "reports" / "input-manifest-prelock-v7.json"
INPUT_AUDIT_REPAIR_REPORT_PATH = V7_OUT / "reports" / "input-manifest-prelock-repair1-v7.json"
PREFLIGHT_TEST_ROOT = V7_OUT / "reports" / "capture-preflight-tests-v7"
PREFLIGHT_REPAIR_TEST_ROOT = V7_OUT / "reports" / "capture-preflight-tests-repair1-v7"
V7_NATIVE_MODULE_PATH = q.V6_CONFIGURED_NATIVE_MODULE_PATH


class QualificationV7Failure(RuntimeError):
    pass


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_checkout_text_file(path: Path) -> str:
    """Hash exact UTF-8 text content while ignoring only CRLF/LF checkout conversion."""
    return sha256_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def exact_membership(
    baseline_paths: Sequence[str],
    current_paths: Sequence[str],
    *,
    optional_paths: Sequence[str] = (OPTIONAL_RUNTIME_OUTPUT,),
) -> list[str]:
    optional = set(optional_paths)
    baseline = set(baseline_paths)
    current = set(current_paths)
    if not set(REQUIRED_INPUT_ANCHORS).issubset(baseline):
        missing = sorted(set(REQUIRED_INPUT_ANCHORS) - baseline)
        raise QualificationV7Failure(f"historical source baseline omits required anchors: {missing}")
    expected = baseline - optional
    observed = current - optional
    if observed != expected:
        raise QualificationV7Failure(
            "historical tracked input membership changed: "
            f"missing={sorted(expected - observed)}, unexpected={sorted(observed - expected)}"
        )
    return sorted(expected)


def ensure_inputs_present(study_root: Path, paths: Sequence[str]) -> None:
    root = study_root.resolve()
    missing: list[str] = []
    unsafe: list[str] = []
    for repository_path in paths:
        relative = Path(repository_path)
        if relative.is_absolute() or ".." in relative.parts:
            unsafe.append(repository_path)
            continue
        path = REPOSITORY / relative
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            missing.append(repository_path)
            continue
        if not resolved.is_relative_to(root) or not resolved.is_file():
            unsafe.append(repository_path)
    if unsafe:
        raise QualificationV7Failure(f"historical inputs resolve outside the study checkout: {sorted(unsafe)}")
    if missing:
        raise QualificationV7Failure(f"mandatory historical inputs are missing: {sorted(missing)}")


def git_tree_paths(commit: str) -> list[str]:
    result: set[str] = set()
    for prefix in HISTORICAL_PREFIXES:
        raw = q.git("ls-tree", "-r", "--name-only", commit, "--", prefix)
        result.update(line for line in raw.splitlines() if line)
    return sorted(result)


def external_git_blobs(source_commit: str, repository_paths: Sequence[str]) -> dict[str, bytes]:
    """Fetch exact committed blobs in one Git process to keep bounded manifest work practical."""
    if not repository_paths:
        return {}
    command, env = runner.git_invocation(REPOSITORY)
    specs = [f"{source_commit}:{relative}" for relative in repository_paths]
    result = subprocess.run(
        [*command, "cat-file", "--batch"],
        input=("\n".join(specs) + "\n").encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise QualificationV7Failure(f"git cat-file --batch failed: {detail}")
    payload = result.stdout
    cursor = 0
    blobs: dict[str, bytes] = {}
    for relative, spec in zip(repository_paths, specs, strict=True):
        header_end = payload.find(b"\n", cursor)
        if header_end < 0:
            raise QualificationV7Failure(f"git cat-file --batch omitted the header for {spec}")
        header = payload[cursor:header_end].decode("ascii", errors="replace").split()
        cursor = header_end + 1
        if len(header) != 3 or header[1] != "blob":
            raise QualificationV7Failure(f"git cat-file --batch did not return a blob for {spec}: {header}")
        try:
            size = int(header[2])
        except ValueError as exc:
            raise QualificationV7Failure(f"git cat-file --batch returned an invalid size for {spec}") from exc
        end = cursor + size
        if end >= len(payload) or payload[end:end + 1] != b"\n":
            raise QualificationV7Failure(f"git cat-file --batch returned a truncated blob for {spec}")
        blobs[relative] = payload[cursor:end]
        cursor = end + 1
    if cursor != len(payload):
        raise QualificationV7Failure("git cat-file --batch returned unexpected trailing bytes")
    return blobs


def source_file_entry(source_commit: str, repository_path: str, *, blob: bytes | None = None) -> dict[str, Any]:
    if blob is None:
        blob = external_git_blobs(source_commit, (repository_path,))[repository_path]
    checkout = REPOSITORY / repository_path
    try:
        checkout_bytes = checkout.read_bytes()
    except OSError as exc:
        raise QualificationV7Failure(f"mandatory historical input cannot be read: {repository_path}: {exc}") from exc
    binary = Path(repository_path).suffix.lower() in {".pt", ".so", ".dll", ".bin"} or b"\0" in blob
    if binary:
        if checkout_bytes != blob:
            raise QualificationV7Failure(f"{repository_path} binary checkout differs from its exact Git blob bytes")
        checkout_matches = True
        checkout_status = "PASS_EXACT_BINARY_GIT_BLOB"
    else:
        normalized = q.verify_locked_source_payload(
            label=repository_path,
            expected_sha256=sha256_bytes(blob),
            committed_blob=blob,
            checkout_bytes=checkout_bytes,
        )
        checkout_matches = normalized.get("status") == "PASS_EXACT_GIT_BLOB_WITH_EOL_ONLY_CHECKOUT_CONVERSION"
        checkout_status = str(normalized.get("status"))
    return {
        "path": repository_path,
        "file_kind": "binary_raw_bytes" if binary else "text_git_blob_with_eol_only_checkout_conversion",
        "git_blob_bytes": len(blob),
        "git_blob_sha256": sha256_bytes(blob),
        "checkout_raw_bytes": len(checkout_bytes),
        "checkout_raw_sha256": sha256_bytes(checkout_bytes),
        "checkout_matches_locked_blob": checkout_matches,
        "checkout_verification_status": checkout_status,
    }


def historical_input_manifest(source_commit: str) -> dict[str, Any]:
    baseline_paths = git_tree_paths(HISTORICAL_INPUT_BASE_COMMIT)
    current_paths = git_tree_paths(source_commit)
    included_paths = exact_membership(baseline_paths, current_paths)
    ensure_inputs_present(STUDY, included_paths)
    blobs = external_git_blobs(source_commit, included_paths)
    entries = [source_file_entry(source_commit, path, blob=blobs[path]) for path in included_paths]
    optional = REPOSITORY / OPTIONAL_RUNTIME_OUTPUT
    optional_present = optional.is_file()
    return {
        "schema_id": "issue73-v7-bounded-historical-input-manifest-v1",
        "status": "PASS",
        "baseline_commit": HISTORICAL_INPUT_BASE_COMMIT,
        "code_source_commit": source_commit,
        "historical_source_prefixes": list(HISTORICAL_PREFIXES),
        "included_file_count": len(entries),
        "included_bytes": sum(int(row["git_blob_bytes"]) for row in entries),
        "included": entries,
        "excluded": [
            {
                "path": OPTIONAL_RUNTIME_OUTPUT,
                "category": "optional prior smoke diagnostic runtime output",
                "reason": "it is not an immutable source input and absence must not block source qualification",
                "present_when_manifest_created": optional_present,
            },
            {
                "path_prefix": f"{STUDY_RELATIVE}/receipts/source-qualification-v7/",
                "category": "newly generated v7 receipts, config, plan, lock, and runtime outputs",
                "reason": "prospective v7 outputs are not historical source inputs",
            },
        ],
        "membership_rule": (
            "the included path set is exactly the Git-tree membership from the registered v6 source "
            "commit across the five named historical receipt prefixes, less only the single explicit "
            "optional metrics output; any missing, added, or renamed historical path fails closed"
        ),
    }


def _verify_source_commit_and_remote() -> str:
    source_commit = q.git("rev-parse", "HEAD")
    remote = q.git("ls-remote", "origin", f"refs/heads/{q.BRANCH}").split()
    if not remote or remote[0] != source_commit:
        raise QualificationV7Failure("v7 source-lock source must be the exact pushed issue branch head")
    command, env = runner.git_invocation(REPOSITORY)
    base_ok = subprocess.run(
        [*command, "merge-base", "--is-ancestor", q.EXPECTED_BASE_COMMIT, source_commit],
        check=False,
        env=env,
    ).returncode == 0
    history_ok = subprocess.run(
        [*command, "merge-base", "--is-ancestor", HISTORICAL_INPUT_BASE_COMMIT, source_commit],
        check=False,
        env=env,
    ).returncode == 0
    if not base_ok or not history_ok:
        raise QualificationV7Failure("v7 source does not preserve the exact registered study lineage")
    return source_commit


def _source_hashes(source_commit: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    source_paths = [f"{STUDY_RELATIVE}/{name}" for name in SOURCE_HASH_PATHS_V7]
    blobs = external_git_blobs(source_commit, source_paths)
    for name in SOURCE_HASH_PATHS_V7:
        blob = blobs[f"{STUDY_RELATIVE}/{name}"]
        checkout_path = STUDY / name
        q.verify_locked_source_payload(
            label=f"study/{name}",
            expected_sha256=sha256_bytes(blob),
            committed_blob=blob,
            checkout_bytes=checkout_path.read_bytes(),
        )
        hashes[name] = sha256_bytes(blob)
    return hashes


def _v7_resource_ledger() -> dict[str, Any]:
    if not RESOURCE_LEDGER_PATH.is_file():
        raise QualificationV7Failure("v7 cumulative resource ledger is missing")
    value = read_json(RESOURCE_LEDGER_PATH)
    if value.get("schema_id") != "issue73-checkpoint-replay-resource-ledger-v1":
        raise QualificationV7Failure("v7 cumulative resource ledger schema is invalid")
    return value


def _v7_charged(value: Mapping[str, Any]) -> tuple[float, float]:
    return q.charged(value)


def reserve_static_v7() -> dict[str, Any]:
    q.install_process_cpu_ceiling(STATIC_RESERVATION_CPU_SECONDS)
    if any(path.exists() for path in (STATIC_RESERVATION_PATH, RESOURCE_LEDGER_PATH, RESOURCE_SNAPSHOT_PATH)):
        raise QualificationV7Failure("v7 static reservation or ledger is partial or already exists; no retry is permitted")
    prior = read_json(q.OUT / "resource-ledger.json")
    builder_before, native_before = q.charged(prior)
    if abs(builder_before - 2137.891659) > 1e-6:
        raise QualificationV7Failure(f"cumulative Builder charge differs from authorized v7 baseline: {builder_before:.6f}")
    if abs(native_before - 1260.0) > 1e-6:
        raise QualificationV7Failure(f"native Gate B+C charge differs from authorized v7 baseline: {native_before:.6f}")
    projected_builder = builder_before + STATIC_RESERVATION_CPU_SECONDS + 1500.0 + 120.0
    projected_native = native_before + 1500.0
    if projected_builder > 4800.0 or projected_native > 2760.0:
        raise QualificationV7Failure("v7 authorization no longer fits the registered CPU ceilings")
    receipt = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": STATIC_RESERVATION_KEY,
        "kind": "v7_bounded_static_input_manifest_lock_and_preflight_qualification",
        "status": "RESERVED_CONSERVATIVE_STATIC_DEBIT",
        "native_gate_bc": False,
        "reserved_cpu_seconds": STATIC_RESERVATION_CPU_SECONDS,
        "cpu_charged_seconds": STATIC_RESERVATION_CPU_SECONDS,
        "measurement_status": "FIXED_RESERVATION_PRECHARGED; NO REFUND",
        "source_lock_sha256": None,
        "formal_seed_consumed": False,
        "observed_process_cpu_seconds": max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU),
        "note": (
            "One-time v7 non-native static reservation, charged in full before v7 prepare-lock, "
            "manifest, or production capture-preflight verification."
        ),
    }
    updated = json.loads(json.dumps(prior))
    updated.setdefault("attempts", []).append(receipt)
    runner.atomic_json(RESOURCE_SNAPSHOT_PATH, prior)
    runner.atomic_json(STATIC_RESERVATION_PATH, receipt)
    runner.atomic_json(RESOURCE_LEDGER_PATH, updated)
    push_v7("Issue 73: precharge one-time v7 static qualification")
    return receipt


def ensure_static_precharge_v7() -> tuple[dict[str, Any], dict[str, Any]]:
    if not STATIC_RESERVATION_PATH.is_file() or not RESOURCE_LEDGER_PATH.is_file() or not RESOURCE_SNAPSHOT_PATH.is_file():
        raise QualificationV7Failure("the v7 one-time 60 CPU-s static reservation is not durably precharged")
    reservation = read_json(STATIC_RESERVATION_PATH)
    ledger = _v7_resource_ledger()
    snapshot = read_json(RESOURCE_SNAPSHOT_PATH)
    rows = [row for row in ledger.get("attempts", []) if row.get("key") == STATIC_RESERVATION_KEY]
    if (
        reservation.get("status") != "RESERVED_CONSERVATIVE_STATIC_DEBIT"
        or reservation.get("reserved_cpu_seconds") != STATIC_RESERVATION_CPU_SECONDS
        or reservation.get("cpu_charged_seconds") != STATIC_RESERVATION_CPU_SECONDS
        or reservation.get("native_gate_bc") is not False
        or reservation.get("formal_seed_consumed") is not False
        or rows != [reservation]
        or snapshot.get("schema_id") != "issue73-checkpoint-replay-resource-ledger-v1"
    ):
        raise QualificationV7Failure("v7 reservation, cumulative ledger, or immutable prior snapshot is inconsistent")
    expected_ledger = json.loads(json.dumps(snapshot))
    expected_ledger.setdefault("attempts", []).append(reservation)
    expected_attempts = expected_ledger["attempts"]
    observed_attempts = ledger.get("attempts", [])
    if observed_attempts[:len(expected_attempts)] != expected_attempts:
        raise QualificationV7Failure("v7 cumulative ledger no longer preserves the exact immutable baseline and static reservation")
    extra_attempts = observed_attempts[len(expected_attempts):]
    seen_extra_keys: set[str] = set()
    for expected_attempt in extra_attempts:
        key = str(expected_attempt.get("key", ""))
        if key in seen_extra_keys:
            raise QualificationV7Failure(f"v7 cumulative ledger repeats an attempt key: {key}")
        seen_extra_keys.add(key)
        if key in V7_STATIC_CHARGE_PATHS:
            receipt_path = V7_STATIC_CHARGE_PATHS[key]
            if (
                expected_attempt.get("schema_id") != "issue73-checkpoint-replay-attempt-v1"
                or expected_attempt.get("native_gate_bc") is not False
                or expected_attempt.get("status") not in {"COMPLETE", "FAIL_OR_INCOMPLETE"}
                or float(expected_attempt.get("cpu_charged_seconds", -1.0)) < 0.0
                or not receipt_path.is_file()
                or read_json(receipt_path) != expected_attempt
            ):
                raise QualificationV7Failure(f"v7 static CPU charge has no matching immutable receipt: {key}")
        elif key == CAPTURE_ATTEMPT_KEY:
            if (
                expected_attempt.get("kind") != "v7_historical_debug_owner_source_history_capture"
                or expected_attempt.get("native_gate_bc") is not True
                or expected_attempt.get("reserved_cpu_seconds") != 60.0
                or expected_attempt.get("formal_seed_consumed") is not False
                or expected_attempt.get("status") not in {"STARTED", "COMPLETE", "FAIL_OR_INCOMPLETE", "INTERRUPTED_NO_RERUN", "FAIL_CPU_RESERVATION_EXCEEDED", "FAIL_CUMULATIVE_CPU_CEILING_EXCEEDED"}
                or not V7_CAPTURE_ATTEMPT_PATH.is_file()
                or read_json(V7_CAPTURE_ATTEMPT_PATH) != expected_attempt
            ):
                raise QualificationV7Failure("v7 ledger contains an invalid or unreceipted native capture attempt")
        else:
            raise QualificationV7Failure(f"v7 ledger contains an unregistered additional attempt: {key}")
    if {key: value for key, value in ledger.items() if key != "attempts"} != {
        key: value for key, value in expected_ledger.items() if key != "attempts"
    }:
        raise QualificationV7Failure("v7 cumulative ledger changed fields outside its append-only attempts")
    baseline_builder, baseline_native = q.charged(snapshot)
    if abs(baseline_builder - 2137.891659) > 1e-6 or abs(baseline_native - 1260.0) > 1e-6:
        raise QualificationV7Failure("immutable v7 precharge snapshot does not match the authorized resource baseline")
    builder_total, native_total = _v7_charged(ledger)
    if builder_total > 4800.0 or native_total > 2760.0:
        raise QualificationV7Failure("v7 cumulative resource ledger exceeds the registered Builder or native ceiling")
    return reservation, ledger


def record_builder_cpu_charge_v7(
    *, key: str, cpu_seconds: float, status: str = "COMPLETE", details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    receipt_path = V7_STATIC_CHARGE_PATHS.get(key)
    if receipt_path is None:
        raise QualificationV7Failure(f"unregistered v7 static CPU charge key: {key}")
    if receipt_path.exists():
        raise QualificationV7Failure(f"v7 static CPU charge receipt already exists; no overwrite: {key}")
    if status not in {"COMPLETE", "FAIL_OR_INCOMPLETE"}:
        raise QualificationV7Failure("v7 static CPU charge status must describe a completed or failed bounded run")
    amount = float(cpu_seconds)
    if amount < 0.0:
        raise QualificationV7Failure("v7 static CPU charge cannot be negative")
    _reservation, ledger = ensure_static_precharge_v7()
    builder_before, native_before = _v7_charged(ledger)
    if builder_before + amount > 4800.0 or native_before > 2760.0:
        raise QualificationV7Failure("v7 static CPU charge would exceed registered resource ceilings")
    row = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": key,
        "kind": "v7_bounded_non_native_builder_cpu_charge",
        "status": status,
        "native_gate_bc": False,
        "cpu_charged_seconds": amount,
        "formal_seed_consumed": False,
        "measurement_status": "MEASURED_PROCESS_TREE_CPU_WITH_CONSERVATIVE_ORCHESTRATION_BOUND",
        **dict(details or {}),
    }
    ledger["attempts"].append(row)
    runner.atomic_json(receipt_path, row)
    runner.atomic_json(RESOURCE_LEDGER_PATH, ledger)
    push_v7(f"Issue 73: preserve v7 bounded Builder CPU charge {key}")
    return row


def verify_native_capture_implementation_v7() -> dict[str, Any]:
    key = "native-capture-implementation-repair1"
    if V7_STATIC_CHARGE_PATHS[key].exists():
        raise QualificationV7Failure("native-capture implementation verification already has an immutable charge receipt; no rerun")
    started_cpu = q.process_tree_cpu()
    started_wall = time.monotonic()
    output = ""
    return_code: int | None = None
    failure: str | None = None
    try:
        temp_root = BUILDER_RUNTIME_ROOT / "tmp" / key
        cache_root = BUILDER_RUNTIME_ROOT / "cache" / key
        temp_root.mkdir(parents=True, exist_ok=True)
        cache_root.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(BUILDER_RUNTIME_ROOT / "pycache" / key),
            "TMPDIR": str(temp_root),
            "TMP": str(temp_root),
            "TEMP": str(temp_root),
            "XDG_CACHE_HOME": str(cache_root),
            "TORCH_HOME": str(cache_root / "torch"),
            "HF_HOME": str(cache_root / "huggingface"),
        })
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_native_capture_v7.py"]
        completed = subprocess.run(command, cwd=STUDY, env=env, check=False, capture_output=True, text=True, timeout=45.0)
        return_code = int(completed.returncode)
        output = (completed.stdout + completed.stderr)[-6000:]
        if return_code:
            failure = f"focused pytest returned {return_code}"
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {str(exc)[:1000]}"
    measured = max(0.0, q.process_tree_cpu() - started_cpu)
    # Include bounded CLI/receipt/push orchestration not measured inside pytest.
    prior_autocrlf_repair_bound = 54.093588
    charged = prior_autocrlf_repair_bound + measured + 2.0
    status = "COMPLETE" if failure is None else "FAIL_OR_INCOMPLETE"
    row = record_builder_cpu_charge_v7(
        key=key,
        cpu_seconds=charged,
        status=status,
        details={
            "measured_test_process_tree_cpu_seconds": measured,
            "conservative_orchestration_bound_seconds": 2.0,
            "prior_autocrlf_lock_repair_cpu_bound_seconds": prior_autocrlf_repair_bound,
            "prior_autocrlf_repair_evidence": "https://github.com/lsmfttb/spire-research/issues/73#issuecomment-6374192622",
            "measured_wall_seconds": max(0.0, time.monotonic() - started_wall),
            "command_return_code": return_code,
            "command": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_native_capture_v7.py"],
            "test_output_tail": output,
            "failure": failure,
            "no_native_attempt_or_seed": True,
        },
    )
    if failure is not None:
        raise QualificationV7Failure(f"native-capture implementation verification failed: {failure}")
    return {"status": row["status"], "cpu_charged_seconds": row["cpu_charged_seconds"], "test_output_tail": output}


def _stage0_resource_projection(ledger: Mapping[str, Any]) -> dict[str, Any]:
    builder, native = _v7_charged(ledger)
    native_remaining = 2760.0 - native
    native_reserve = 60.0 + 80.0 + 20.0 + 64.0 * 20.0 + 60.0
    builder_reserve = native_reserve + 120.0
    projected_builder = builder + builder_reserve
    projected_native = native + native_reserve
    full_fits = projected_builder <= 4800.0 and projected_native <= 2760.0
    return {
        "aggregate_stage0_cpu_seconds": 6000.0,
        "builder_ceiling_cpu_seconds": 4800.0,
        "reviewer_reserve_cpu_seconds": 1200.0,
        "prior_builder_charge_cpu_seconds": builder,
        "builder_remaining_before_native_and_gate_a_cpu_seconds": 4800.0 - builder,
        "prior_native_gate_bc_charge_cpu_seconds": native,
        "native_gate_bc_ceiling_cpu_seconds": 2760.0,
        "native_gate_bc_remaining_cpu_seconds": native_remaining,
        "source_history_capture_reserve_cpu_seconds": 60.0,
        "fresh_process_prefix_and_transition_parity_reserve_cpu_seconds": 80.0,
        "gate_c_environment_setup_reserve_cpu_seconds": 20.0,
        "gate_c_suffix_attempt_count": 64,
        "gate_c_per_suffix_process_cpu_cap_seconds": 20.0,
        "gate_c_suffix_total_reserve_cpu_seconds": 64.0 * 20.0,
        "native_engineering_contingency_cpu_seconds": 60.0,
        "projected_new_native_cpu_reserve_seconds": native_reserve,
        "gate_a_nonnative_process_cpu_cap_seconds": 120.0,
        "projected_new_builder_cpu_reserve_seconds": builder_reserve,
        "cumulative_builder_projection_cpu_seconds": projected_builder,
        "remaining_builder_cpu_seconds_after_max_reserve": 4800.0 - projected_builder,
        "cumulative_native_gate_bc_projection_cpu_seconds": projected_native,
        "remaining_native_gate_bc_cpu_seconds_after_max_reserve": 2760.0 - projected_native,
        "full_stage0_forecast_fits_current_caps": full_fits,
        "reviewer_reserve_untouched": True,
        "no_prior_charge_reclaimed": True,
        "stage1_authorized": False,
    }


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    payload = (runner.canonical(manifest) + "\n").encode("utf-8")
    return sha256_bytes(payload)


def _lock_paths(lock_out: Path) -> tuple[Path, Path, Path, Path]:
    try:
        lock_out.resolve().relative_to(V7_OUT.resolve())
    except ValueError as exc:
        raise QualificationV7Failure("v7 lock outputs must remain inside the v7 receipt directory") from exc
    return (
        lock_out / "preseed-source-lock-v7.json",
        lock_out / "qualification-config-v7.json",
        lock_out / "plans" / "gate-c-replay-plan-v7.json",
        lock_out / "input-manifest-v7.json",
    )


def prepare_lock_v7(*, lock_out: Path = V7_LOCK_OUT, lock_revision: int = 7) -> dict[str, Any]:
    if (lock_out, lock_revision) not in {(V7_LOCK_OUT, 7), (V7_CAPTURE_LOCK_OUT, 8)}:
        raise QualificationV7Failure("only the reviewed v7 lock and the uniquely named native-capture repair lock are supported")
    lock_path, config_path, plan_path, input_manifest_path = _lock_paths(lock_out)
    lock_paths = (lock_path, config_path, plan_path, input_manifest_path)
    existing = [path.exists() for path in lock_paths]
    if any(existing):
        if not all(existing):
            raise QualificationV7Failure("partial v7 source/config/plan/manifest exists; it may not be reconstructed")
        lock, _config = verify_lock_v7(
            require_remote_current=False, lock_out=lock_out, lock_revision=lock_revision,
        )
        push_v7("Issue 73: preserve existing prospective v7 source lock")
        return lock

    ensure_static_precharge_v7()
    if lock_revision == 8:
        implementation_charge_path = V7_STATIC_CHARGE_PATHS["native-capture-implementation-repair1"]
        implementation_charge = read_json(implementation_charge_path) if implementation_charge_path.is_file() else None
        if not isinstance(implementation_charge, Mapping) or implementation_charge.get("status") != "COMPLETE":
            raise QualificationV7Failure("native-capture repair lock requires a passing immutable implementation verification charge")
    source_commit = _verify_source_commit_and_remote()
    baseline_paths = git_tree_paths(HISTORICAL_INPUT_BASE_COMMIT)
    current_paths = git_tree_paths(source_commit)
    input_paths = exact_membership(baseline_paths, current_paths)
    # This check deliberately precedes config/plan/manifest creation.
    ensure_inputs_present(STUDY, input_paths)
    if not q.V5_RESOURCE_LEDGER_PATH.is_file() or not q.V5_STATIC_PREFLIGHT_REPORT_PATH.is_file():
        raise QualificationV7Failure("mandatory v5 ledger or smoke receipt is missing")
    q.configure_stsr_python_root()
    external_sources = q.verify_locked_external_sources()
    code_hashes = _source_hashes(source_commit)
    manifest = historical_input_manifest(source_commit)
    config, _roots, plan = q.make_qualification_config(source_ledger=_v7_resource_ledger())
    config = json.loads(json.dumps(config))
    config["schema_id"] = "issue73-checkpoint-replay-qualification-config-v7"
    config["gate_c_plan_file"] = plan_path.relative_to(STUDY).as_posix()
    config["source_history_attempt_key"] = CAPTURE_ATTEMPT_KEY
    config["source_qualification_version"] = "v7"
    config["input_manifest"] = {
        "path": input_manifest_path.relative_to(STUDY).as_posix(),
        "sha256": _manifest_digest(manifest),
        "included_file_count": manifest["included_file_count"],
        "included_bytes": manifest["included_bytes"],
    }
    config["configured_native_module_path"] = V7_NATIVE_MODULE_PATH
    resource_projection = _stage0_resource_projection(_v7_resource_ledger())
    config["resource_caps"] = {
        **config["resource_caps"],
        **resource_projection,
        "prelock_resource_ledger_sha256": sha256_checkout_text_file(RESOURCE_SNAPSHOT_PATH),
        "prelock_static_debit_cpu_seconds": STATIC_RESERVATION_CPU_SECONDS,
        "no_prior_charge_reclaimed": True,
    }
    if not resource_projection["full_stage0_forecast_fits_current_caps"]:
        raise QualificationV7Failure("the complete fixed Stage-0 budget no longer fits current caps")
    plan = json.loads(json.dumps(plan))
    plan["schema_id"] = "issue73-checkpoint-replay-gate-c-plan-v7"
    plan["qualification_config_file"] = config_path.relative_to(STUDY).as_posix()
    plan["source_qualification_version"] = "v7"
    root_paths = [q.root_path(str(root["root_id"])).relative_to(REPOSITORY).as_posix() for root in plan.get("roots", [])]
    root_blobs = external_git_blobs(source_commit, root_paths)
    for root in plan.get("roots", []):
        root_path = q.root_path(str(root["root_id"]))
        root_relative = root_path.relative_to(REPOSITORY).as_posix()
        root["historical_root_receipt_sha256"] = sha256_bytes(root_blobs[root_relative])
    if (
        [row.get("root_id") for row in plan.get("roots", [])] != list(q.GATE_C_ROOTS)
        or len(plan.get("policy_rng_seed_streams", [])) != 64
        or plan.get("planned_suffix_attempt_count") != 64
    ):
        raise QualificationV7Failure("v7 plan differs from the fixed four-root, eight-per-action Gate C contract")
    config["historical_input_manifest_sha256"] = _manifest_digest(manifest)
    runner.atomic_json(input_manifest_path, manifest)
    runner.atomic_json(config_path, config)
    plan["qualification_config_sha256"] = sha256_checkout_text_file(config_path)
    runner.atomic_json(plan_path, plan)

    lock = {
        "schema_id": "issue73-checkpoint-replay-preseed-source-lock-v7",
        "lock_revision": lock_revision,
        "status": "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION",
        "study_only": True,
        "formal_training_seed_consumed": False,
        "formal_evaluation_seed_consumed": False,
        "stage1_authorized": False,
        "code_source_commit": source_commit,
        "remote_branch": q.BRANCH,
        "remote_must_equal_current_evidence_head_before_any_run": True,
        "historical_input_base_commit": HISTORICAL_INPUT_BASE_COMMIT,
        "historical_input_prefixes": list(HISTORICAL_PREFIXES),
        "historical_input_manifest": input_manifest_path.relative_to(STUDY).as_posix(),
        "historical_input_manifest_sha256": _manifest_digest(manifest),
        "historical_input_sha256": {
            row["path"]: row["git_blob_sha256"] for row in manifest["included"]
        },
        "optional_runtime_output_exclusions": manifest["excluded"],
        "qualification_config": config_path.relative_to(STUDY).as_posix(),
        "qualification_config_sha256": sha256_checkout_text_file(config_path),
        "gate_c_plan": plan_path.relative_to(STUDY).as_posix(),
        "gate_c_plan_sha256": sha256_checkout_text_file(plan_path),
        "study_source_sha256": code_hashes,
        "native": {
            "repository": q.OLD_CONFIG["identity"]["native_repository"],
            "commit": q.OLD_CONFIG["identity"]["native_commit"],
            "source_branch": q.OLD_CONFIG["identity"]["native_source_branch"],
            "source_file_sha256": q.OLD_CONFIG["identity"]["native_source_files_sha256"],
            "loaded_module": q.OLD_CONFIG["identity"]["native_module"],
            "loaded_module_sha256": q.OLD_CONFIG["identity"]["native_binary_sha256"],
            "module_path": V7_NATIVE_MODULE_PATH,
        },
        "stsr": {
            "repository": q.OLD_CONFIG["identity"]["stsr_repository"],
            "commit": q.OLD_CONFIG["identity"]["stsr_commit"],
            "native_integration_commit": q.OLD_CONFIG["identity"]["stsr_native_integration_commit"],
            "source_file_sha256": q.OLD_CONFIG["identity"]["stsr_source_files_sha256"],
        },
        "runtime": {
            name: q.OLD_CONFIG["identity"][name]
            for name in ("python", "torch", "numpy")
        },
        "stsr_python_package_root": q.STSRL_PYTHON_PACKAGE_ROOT,
        "external_source_verification": external_sources,
        "v7_static_reservation": {
            "path": STATIC_RESERVATION_PATH.relative_to(STUDY).as_posix(),
            "sha256": sha256_checkout_text_file(STATIC_RESERVATION_PATH),
            "prior_ledger_snapshot": RESOURCE_SNAPSHOT_PATH.relative_to(STUDY).as_posix(),
            "prior_ledger_snapshot_sha256": sha256_checkout_text_file(RESOURCE_SNAPSHOT_PATH),
        },
        "resource_caps": resource_projection,
        "fixed_gate_contract": {
            "gate_a_root_count": 24,
            "gate_b_witness_count": 3,
            "gate_c_root_count": 4,
            "gate_c_action_occurrences_per_root": 2,
            "gate_c_suffixes_per_action": 8,
            "gate_c_total_suffixes": 64,
            "suffix_policy": "0.80 expert_non_combat_v1 + 0.20 uniform among currently legal actions",
            "battle_search": "Battle Search-v2@400",
        },
        "lock_preparation_process_cpu_seconds": max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU),
        "source_lock_created_before_any_new_debug_execution": True,
    }
    runner.atomic_json(lock_path, lock)
    push_v7("Issue 73: commit prospective v7 replay source lock")
    return lock


def prepare_native_capture_lock_v7() -> dict[str, Any]:
    key = "native-capture-lock-preparation-repair1"
    if V7_STATIC_CHARGE_PATHS[key].exists():
        raise QualificationV7Failure("native-capture lock preparation already has an immutable CPU receipt; no rerun")
    started_cpu = q.process_tree_cpu()
    started_wall = time.monotonic()
    lock = prepare_lock_v7(lock_out=V7_CAPTURE_LOCK_OUT, lock_revision=8)
    measured = max(0.0, q.process_tree_cpu() - started_cpu)
    row = record_builder_cpu_charge_v7(
        key=key,
        cpu_seconds=measured + 2.0,
        details={
            "measured_process_tree_cpu_seconds": measured,
            "conservative_orchestration_bound_seconds": 2.0,
            "measured_wall_seconds": max(0.0, time.monotonic() - started_wall),
            "source_lock_sha256": sha256_checkout_text_file(_lock_paths(V7_CAPTURE_LOCK_OUT)[0]),
            "lock_revision": 8,
            "no_native_attempt_or_seed": True,
        },
    )
    return {"status": lock["status"], "lock_revision": lock["lock_revision"], "cpu_charged_seconds": row["cpu_charged_seconds"], "historical_input_count": len(lock["historical_input_sha256"])}


def verify_lock_v7(
    *, require_remote_current: bool = True, lock_out: Path = V7_LOCK_OUT, lock_revision: int = 7,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if (lock_out, lock_revision) not in {(V7_LOCK_OUT, 7), (V7_CAPTURE_LOCK_OUT, 8)}:
        raise QualificationV7Failure("only the reviewed v7 lock and the uniquely named native-capture repair lock are supported")
    lock_path, config_path, plan_path, input_manifest_path = _lock_paths(lock_out)
    if not all(path.is_file() for path in (lock_path, config_path, plan_path, input_manifest_path)):
        raise QualificationV7Failure("prospective v7 source/config/plan/input manifest is missing")
    ensure_static_precharge_v7()
    lock = read_json(lock_path)
    config = read_json(config_path)
    plan = read_json(plan_path)
    manifest = read_json(input_manifest_path)
    if lock.get("schema_id") != "issue73-checkpoint-replay-preseed-source-lock-v7" or lock.get("lock_revision") != lock_revision:
        raise QualificationV7Failure("only the prospective v7 source lock is valid on this path")
    if lock.get("status") != "PRESEED_LOCKED_BEFORE_ANY_NEW_NATIVE_EXECUTION":
        raise QualificationV7Failure("prospective v7 source lock has an invalid status")
    if lock_revision == 8:
        implementation_charge_path = V7_STATIC_CHARGE_PATHS["native-capture-implementation-repair1"]
        implementation_charge = read_json(implementation_charge_path) if implementation_charge_path.is_file() else None
        if not isinstance(implementation_charge, Mapping) or implementation_charge.get("status") != "COMPLETE":
            raise QualificationV7Failure("native-capture repair lock has no passing bounded implementation verification receipt")
    if lock.get("code_source_commit") != manifest.get("code_source_commit"):
        raise QualificationV7Failure("v7 lock and input manifest name different source commits")
    if sha256_checkout_text_file(config_path) != lock.get("qualification_config_sha256"):
        raise QualificationV7Failure("v7 qualification config changed after source lock")
    if sha256_checkout_text_file(plan_path) != lock.get("gate_c_plan_sha256"):
        raise QualificationV7Failure("v7 Gate C roots/actions/streams plan changed after source lock")
    if sha256_checkout_text_file(input_manifest_path) != lock.get("historical_input_manifest_sha256"):
        raise QualificationV7Failure("v7 bounded historical input manifest changed after source lock")
    if config.get("historical_input_manifest_sha256") != lock.get("historical_input_manifest_sha256"):
        raise QualificationV7Failure("v7 config and source lock disagree on historical input manifest")
    if plan.get("qualification_config_sha256") != lock.get("qualification_config_sha256"):
        raise QualificationV7Failure("v7 Gate C plan is for another locked config")
    if lock.get("native", {}).get("module_path") != config.get("configured_native_module_path"):
        raise QualificationV7Failure("v7 config and source lock disagree on native module path")
    if lock.get("stsr_python_package_root") != q.STSRL_PYTHON_PACKAGE_ROOT:
        raise QualificationV7Failure("v7 lock does not name the pinned STSRL Python package root")

    source_commit = str(lock["code_source_commit"])
    baseline_paths = git_tree_paths(HISTORICAL_INPUT_BASE_COMMIT)
    current_paths = git_tree_paths(source_commit)
    included_paths = exact_membership(baseline_paths, current_paths)
    expected_manifest_paths = [str(row["path"]) for row in manifest.get("included", [])]
    if expected_manifest_paths != included_paths:
        raise QualificationV7Failure("v7 manifest membership differs from the exact registered historical Git-tree set")
    if set(lock.get("historical_input_sha256", {})) != set(included_paths):
        raise QualificationV7Failure("v7 historical source hash map differs from the explicit manifest membership")
    # Reject source and optimizer mutations before reopening every historical Git blob.
    code_names = list(lock.get("study_source_sha256", {}))
    code_paths = [f"{STUDY_RELATIVE}/{name}" for name in code_names]
    code_blobs = external_git_blobs(source_commit, code_paths)
    for name, expected in lock.get("study_source_sha256", {}).items():
        path = STUDY / name
        if not path.is_file():
            raise QualificationV7Failure(f"v7 locked study source is missing: {name}")
        blob = code_blobs[f"{STUDY_RELATIVE}/{name}"]
        q.verify_locked_source_payload(
            label=f"study/{name}",
            expected_sha256=str(expected),
            committed_blob=blob,
            checkout_bytes=path.read_bytes(),
        )
    ensure_inputs_present(STUDY, included_paths)
    historical_blobs = external_git_blobs(source_commit, included_paths)
    for repository_path in included_paths:
        expected = str(lock["historical_input_sha256"][repository_path])
        entry = next(row for row in manifest["included"] if row.get("path") == repository_path)
        if entry.get("git_blob_sha256") != expected:
            raise QualificationV7Failure(f"v7 manifest hash map differs for {repository_path}")
        source_file_entry(source_commit, repository_path, blob=historical_blobs[repository_path])
    fixed_roots = [row.get("root_id") for row in plan.get("roots", [])]
    if (
        fixed_roots != list(q.GATE_C_ROOTS)
        or plan.get("planned_suffix_attempt_count") != 64
        or len(plan.get("policy_rng_seed_streams", [])) != 64
        or len(config.get("gate_a_root_ids", [])) != 24
        or len(config.get("gate_b_witness_root_ids", {})) != 3
    ):
        raise QualificationV7Failure("v7 Gate A/B/C roots, actions, or stream population changed")
    q.configure_stsr_python_root()
    q.verify_locked_external_sources(lock)

    command, env = runner.git_invocation(REPOSITORY)
    if subprocess.run(
        [*command, "merge-base", "--is-ancestor", source_commit, "HEAD"],
        check=False,
        env=env,
    ).returncode:
        raise QualificationV7Failure("current source worktree does not descend from the locked v7 code commit")
    if require_remote_current:
        remote = q.git("ls-remote", "origin", f"refs/heads/{q.BRANCH}").split()
        if not remote or remote[0] != q.git("rev-parse", "HEAD"):
            raise QualificationV7Failure("v7 evidence worktree is not at the exact current pushed issue-branch head")
    return lock, config


def capture_preflight_v7(
    *,
    preflight_report_dir: Path,
    native_module_path_override: str | None = None,
    lock_out: Path = V7_LOCK_OUT,
    lock_revision: int = 7,
) -> dict[str, Any]:
    try:
        out = preflight_report_dir.resolve()
        out.relative_to(V7_OUT.resolve())
    except ValueError as exc:
        raise QualificationV7Failure("v7 capture preflight output must remain inside the v7 receipt directory") from exc
    report_path = out / "capture-launch-preflight-v7.json"
    failure_path = out / "capture-launch-preflight-v7-failure.json"
    if report_path.exists() or failure_path.exists():
        raise QualificationV7Failure("this immutable v7 preflight case already has a receipt; no rerun or overwrite")
    if lock_revision == 8 and V7_STATIC_CHARGE_PATHS["native-capture-preflight-repair1"].exists():
        raise QualificationV7Failure("native-capture preflight already has an immutable CPU charge; no rerun")
    reservation, _ledger = ensure_static_precharge_v7()
    q.install_process_cpu_ceiling(STATIC_RESERVATION_CPU_SECONDS)
    started_cpu = q.process_tree_cpu()
    started_wall = time.monotonic()
    try:
        lock_path, _config_path, _plan_path, _manifest_path = _lock_paths(lock_out)
        lock, _config, prepared, module_path_value, native_hash = _prepare_v7_capture_runtime(
            lock_out=lock_out,
            lock_revision=lock_revision,
            out=out,
            native_module_path_override=native_module_path_override,
        )
        module_path = Path(module_path_value)
    except BaseException as exc:
        failure = {
            "schema_id": "issue73-capture-launch-preflight-v7-failure-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1600],
            "command_argv": list(sys.argv),
            "python_executable": sys.executable,
            "cwd": os.getcwd(),
            "fixed_static_reservation_cpu_seconds": reservation["reserved_cpu_seconds"],
            "additional_reservation_cpu_seconds": 0.0,
            "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - started_cpu),
            "measured_wall_seconds": max(0.0, time.monotonic() - started_wall),
            "start_attempt_invoked": False,
            "native_adapter_instantiated": False,
            "native_game_step_started": False,
            "formal_seed_consumed": False,
            "native_debit_created": False,
            "no_retry_or_overwrite": True,
        }
        runner.atomic_json(failure_path, failure)
        if lock_revision == 8:
            record_builder_cpu_charge_v7(
                key="native-capture-preflight-repair1",
                cpu_seconds=max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU) + 2.0,
                status="FAIL_OR_INCOMPLETE",
                details={
                    "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU),
                    "conservative_orchestration_bound_seconds": 2.0,
                    "failure_receipt": failure_path.relative_to(STUDY).as_posix(),
                    "no_native_attempt_or_seed": True,
                },
            )
        else:
            push_v7("Issue 73: preserve v7 capture preflight failure")
        raise

    result = {
        "schema_id": "issue73-capture-launch-preflight-v7-v1",
        "status": "PASS",
        "source_lock_sha256": sha256_checkout_text_file(lock_path),
        "code_source_commit": lock["code_source_commit"],
        "lock_revision": lock_revision,
        "remote_branch": lock["remote_branch"],
        "command_argv": list(sys.argv),
        "python_executable": sys.executable,
        "python": sys.version.split()[0],
        "cwd": os.getcwd(),
        "pythonpath_entries": [entry for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry],
        "sys_path": list(sys.path),
        "native_module_path": str(Path(prepared["module"].__file__).resolve()),
        "native_module_sha256": native_hash,
        "native_module_search": prepared["environment"]["identity"]["native_module_search"],
        "external_git_sources": prepared["external_sources"],
        "optimizer_id": prepared["runtime"].get("optimizer_id"),
        "optimizer_source_sha256": prepared["optimizer_source_sha256"],
        "optimizer_expected_sha256": q.OLD_CONFIG["runtime_optimizer"]["sha256"],
        "adapter_class": {
            "module": prepared["adapter_class"].__module__,
            "qualname": prepared["adapter_class"].__qualname__,
        },
        "loaded_policy_sha256": prepared["model_digest"],
        "runtime_installation": prepared["runtime"],
        "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - started_cpu),
        "capture_entry_path": {
            "shared_preparation_function": "prepare_capture_prerequisites",
            "production_capture_uses_same_function_in_same_process": True,
            "preparation_completed_in_this_capture_process": True,
            "start_attempt_invoked": False,
            "native_adapter_instantiated": False,
            "native_game_step_started": False,
            "debug_owner_started": False,
            "formal_seed_consumed": False,
            "native_debit_created": False,
        },
    }
    runner.atomic_json(report_path, result)
    if lock_revision == 8:
        record_builder_cpu_charge_v7(
            key="native-capture-preflight-repair1",
            cpu_seconds=max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU) + 2.0,
            details={
                "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU),
                "conservative_orchestration_bound_seconds": 2.0,
                "preflight_report": report_path.relative_to(STUDY).as_posix(),
                "source_lock_sha256": result["source_lock_sha256"],
                "no_native_attempt_or_seed": True,
            },
        )
    else:
        push_v7("Issue 73: preserve v7 production capture preflight")
    return result


def _require_reviewed_source_sha(reviewed_source_sha: str | None) -> str:
    if not reviewed_source_sha or len(reviewed_source_sha) != 40:
        raise QualificationV7Failure("native v7 capture requires the exact independently reviewed 40-character source SHA")
    current = q.git("rev-parse", "HEAD")
    if reviewed_source_sha.lower() != current:
        raise QualificationV7Failure("reviewed source SHA does not equal the current local evidence head")
    remote = q.git("ls-remote", "origin", f"refs/heads/{q.BRANCH}").split()
    if not remote or remote[0] != current:
        raise QualificationV7Failure("native v7 capture requires the exact reviewed source SHA at the remote issue-branch head")
    return current


def _start_capture_attempt_v7(lock_path: Path) -> dict[str, Any]:
    ensure_static_precharge_v7()
    if V7_CAPTURE_ATTEMPT_PATH.exists():
        old = read_json(V7_CAPTURE_ATTEMPT_PATH)
        if old.get("status") == "STARTED":
            old.update({
                "status": "INTERRUPTED_NO_RERUN",
                "cpu_charged_seconds": float(old["reserved_cpu_seconds"]),
                "failure": "started v7 native capture has no terminal evidence; the fixed attempt may not be rerun",
            })
            ledger = _v7_resource_ledger()
            ledger["attempts"] = [old if row.get("key") == CAPTURE_ATTEMPT_KEY else row for row in ledger["attempts"]]
            runner.atomic_json(V7_CAPTURE_ATTEMPT_PATH, old)
            runner.atomic_json(RESOURCE_LEDGER_PATH, ledger)
            push_v7("Issue 73: preserve interrupted v7 source-history capture")
            raise QualificationV7Failure("the fixed v7 native capture attempt was interrupted; no retry is permitted")
        raise QualificationV7Failure(f"the fixed v7 native capture attempt already has terminal receipt {old.get('status')}; no retry is permitted")

    ledger = _v7_resource_ledger()
    builder_before, native_before = _v7_charged(ledger)
    if builder_before + 60.0 > 4800.0 or native_before + 60.0 > 2760.0:
        raise QualificationV7Failure(
            f"the registered 60 CPU-s capture does not fit remaining caps: "
            f"builder={builder_before + 60.0:.6f}, native={native_before + 60.0:.6f}"
        )
    if V7_CAPTURE_REPORT_PATH.exists() or V7_CAPTURE_STAGING_PATH.exists() or V7_CAPTURE_FAILURE_PATH.exists() or V7_CAPTURE_HISTORY_PATH.exists():
        raise QualificationV7Failure("a v7 source-history output already exists without its fixed attempt receipt; no retry")
    row = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": CAPTURE_ATTEMPT_KEY,
        "kind": "v7_historical_debug_owner_source_history_capture",
        "status": "STARTED",
        "native_gate_bc": True,
        "reserved_cpu_seconds": 60.0,
        "source_lock_sha256": sha256_checkout_text_file(lock_path),
        "formal_seed_consumed": False,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ledger["attempts"].append(row)
    runner.atomic_json(V7_CAPTURE_ATTEMPT_PATH, row)
    runner.atomic_json(RESOURCE_LEDGER_PATH, ledger)
    push_v7(f"Issue 73: start uniquely authorized v7 source-history capture {CAPTURE_ATTEMPT_KEY}")
    return row


def _finish_capture_attempt_v7(*, started_cpu: float, status: str, details: Mapping[str, Any]) -> dict[str, Any]:
    if not V7_CAPTURE_ATTEMPT_PATH.is_file():
        raise QualificationV7Failure("v7 capture attempt receipt disappeared before terminal accounting")
    previous = read_json(V7_CAPTURE_ATTEMPT_PATH)
    if previous.get("status") != "STARTED":
        raise QualificationV7Failure("v7 capture attempt is not in its immutable STARTED state")
    measured = max(0.0, q.process_tree_cpu() - started_cpu)
    reserve = float(previous["reserved_cpu_seconds"])
    exceeded = measured > reserve
    success = status in {"COMPLETE", "PASS", "READY"}
    row = {
        **previous,
        **dict(details),
        "status": "FAIL_CPU_RESERVATION_EXCEEDED" if exceeded else status,
        "measured_process_cpu_seconds": measured,
        "cpu_charged_seconds": measured if success and not exceeded else max(reserve, measured),
        "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ledger = _v7_resource_ledger()
    ledger["attempts"] = [row if item.get("key") == CAPTURE_ATTEMPT_KEY else item for item in ledger["attempts"]]
    total, native = _v7_charged(ledger)
    if total > 4800.0 or native > 2760.0:
        row["status"] = "FAIL_CUMULATIVE_CPU_CEILING_EXCEEDED"
        ledger["attempts"] = [row if item.get("key") == CAPTURE_ATTEMPT_KEY else item for item in ledger["attempts"]]
    runner.atomic_json(V7_CAPTURE_ATTEMPT_PATH, row)
    runner.atomic_json(RESOURCE_LEDGER_PATH, ledger)
    push_v7(f"Issue 73: finish uniquely authorized v7 source-history capture {CAPTURE_ATTEMPT_KEY}")
    return row


def _prepare_v7_capture_runtime(
    *, lock_out: Path, lock_revision: int, out: Path, native_module_path_override: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str]:
    lock_path, _config_path, _plan_path, _manifest_path = _lock_paths(lock_out)
    lock, config = verify_lock_v7(lock_out=lock_out, lock_revision=lock_revision)
    prepared = q.prepare_capture_prerequisites(
        out=out,
        source_lock=lock,
        native_module_path_override=native_module_path_override,
    )
    module_path = Path(prepared["module"].__file__).resolve(strict=True)
    native_hash = runner.validate_native_module_identity(
        module_path,
        expected_name=q.OLD_CONFIG["identity"]["native_module"],
        expected_sha256=q.OLD_CONFIG["identity"]["native_binary_sha256"],
    )
    if str(module_path) != lock["native"]["module_path"] or native_hash != lock["native"]["loaded_module_sha256"]:
        raise QualificationV7Failure("production v7 capture did not load the exact locked native module")
    return lock, config, prepared, str(module_path), native_hash


def capture_source_history_v7(
    *, reviewed_source_sha: str,
    native_module_path_override: str | None = None,
) -> dict[str, Any]:
    if V7_CAPTURE_ATTEMPT_PATH.exists():
        previous = read_json(V7_CAPTURE_ATTEMPT_PATH)
        if previous.get("status") == "COMPLETE" and V7_CAPTURE_REPORT_PATH.is_file():
            return read_json(V7_CAPTURE_REPORT_PATH)
        if previous.get("status") == "COMPLETE" and V7_CAPTURE_STAGING_PATH.is_file():
            staged = read_json(V7_CAPTURE_STAGING_PATH)
            if (
                staged.get("schema_id") != "issue73-source-history-capture-staging-v7-v1"
                or staged.get("source_lock_sha256") != previous.get("source_lock_sha256")
                or not isinstance(staged.get("report"), Mapping)
            ):
                raise QualificationV7Failure("completed v7 attempt has an invalid staged report; preserve evidence for audit")
            report = dict(staged["report"])
            report["attempt_receipt"] = previous
            if previous.get("status") != "COMPLETE":
                report["status"] = str(previous.get("status"))
            runner.atomic_json(V7_CAPTURE_REPORT_PATH, report)
            push_v7("Issue 73: recover final v7 source-history report from durable staged result")
            return report
        if previous.get("status") == "STARTED":
            _start_capture_attempt_v7(V7_CAPTURE_LOCK_OUT)
        raise QualificationV7Failure(f"the fixed v7 source-history attempt is already receipted as {previous.get('status')}; no retry")
    if V7_CAPTURE_NATIVE_PREFLIGHT_FAILURE_PATH.exists():
        raise QualificationV7Failure("v7 native capture has an immutable pre-attempt failure receipt; no retry or overwrite")
    if V7_STATIC_CHARGE_PATHS["native-capture-inline-preflight-repair1"].exists():
        raise QualificationV7Failure("v7 native capture inline preflight already has an immutable CPU charge; no retry")
    if V7_CAPTURE_REPORT_PATH.exists() or V7_CAPTURE_STAGING_PATH.exists() or V7_CAPTURE_FAILURE_PATH.exists() or V7_CAPTURE_HISTORY_PATH.exists():
        raise QualificationV7Failure("v7 native capture has immutable output without an attempt receipt; no retry")
    current_source_sha = _require_reviewed_source_sha(reviewed_source_sha)

    try:
        lock, config, prepared, module_path, native_hash = _prepare_v7_capture_runtime(
            lock_out=V7_CAPTURE_LOCK_OUT,
            lock_revision=8,
            out=V7_CAPTURE_PREFLIGHT_ROOT / "capture-runtime",
            native_module_path_override=native_module_path_override,
        )
    except BaseException as exc:
        failure = {
            "schema_id": "issue73-source-history-capture-preflight-v7-failure-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1600],
            "command_argv": list(sys.argv),
            "python_executable": sys.executable,
            "cwd": os.getcwd(),
            "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU),
            "start_attempt_invoked": False,
            "native_adapter_instantiated": False,
            "native_game_step_started": False,
            "debug_owner_started": False,
            "formal_seed_consumed": False,
            "native_debit_created": False,
            "no_retry_or_overwrite": True,
        }
        if not V7_CAPTURE_NATIVE_PREFLIGHT_FAILURE_PATH.exists():
            runner.atomic_json(V7_CAPTURE_NATIVE_PREFLIGHT_FAILURE_PATH, failure)
            measured = max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU)
            record_builder_cpu_charge_v7(
                key="native-capture-inline-preflight-repair1",
                cpu_seconds=measured + 2.0,
                status="FAIL_OR_INCOMPLETE",
                details={
                    "measured_process_tree_cpu_seconds": measured,
                    "conservative_orchestration_bound_seconds": 2.0,
                    "failure_receipt": V7_CAPTURE_NATIVE_PREFLIGHT_FAILURE_PATH.relative_to(STUDY).as_posix(),
                    "start_attempt_invoked": False,
                    "native_adapter_instantiated": False,
                    "native_game_step_started": False,
                    "no_native_debit_or_seed": True,
                },
            )
        raise

    reviewed_preflight = read_json(V7_CAPTURE_PREFLIGHT_REPORT_PATH) if V7_CAPTURE_PREFLIGHT_REPORT_PATH.is_file() else None
    lock_path, _config_path, _plan_path, _manifest_path = _lock_paths(V7_CAPTURE_LOCK_OUT)
    if (
        not isinstance(reviewed_preflight, Mapping)
        or reviewed_preflight.get("status") != "PASS"
        or reviewed_preflight.get("lock_revision") != 8
        or reviewed_preflight.get("source_lock_sha256") != sha256_checkout_text_file(lock_path)
        or reviewed_preflight.get("code_source_commit") != lock.get("code_source_commit")
    ):
        if V7_CAPTURE_NATIVE_PREFLIGHT_FAILURE_PATH.exists():
            raise QualificationV7Failure("v7 capture preflight mismatch already has an immutable pre-attempt failure receipt")
        runner.atomic_json(V7_CAPTURE_NATIVE_PREFLIGHT_FAILURE_PATH, {
            "schema_id": "issue73-source-history-capture-preflight-v7-failure-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure": "the new v7 lock has no matching production capture-preflight PASS receipt",
            "source_lock_sha256": sha256_checkout_text_file(lock_path),
            "formal_seed_consumed": False,
            "start_attempt_invoked": False,
            "native_adapter_instantiated": False,
            "native_game_step_started": False,
            "native_debit_created": False,
            "no_retry_or_overwrite": True,
        })
        measured = max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU)
        record_builder_cpu_charge_v7(
            key="native-capture-inline-preflight-repair1",
            cpu_seconds=measured + 2.0,
            status="FAIL_OR_INCOMPLETE",
            details={
                "measured_process_tree_cpu_seconds": measured,
                "conservative_orchestration_bound_seconds": 2.0,
                "failure_receipt": V7_CAPTURE_NATIVE_PREFLIGHT_FAILURE_PATH.relative_to(STUDY).as_posix(),
                "start_attempt_invoked": False,
                "native_adapter_instantiated": False,
                "native_game_step_started": False,
                "no_native_debit_or_seed": True,
            },
        )
        raise QualificationV7Failure("the new v7 lock has no matching production capture-preflight PASS receipt")

    attempt = _start_capture_attempt_v7(lock_path)
    del attempt
    q.install_process_cpu_ceiling(60.0)
    phase_start = q._PROCESS_START_CPU
    adapter = None

    def enforce_capture_cpu_budget() -> float:
        used = max(0.0, q.process_tree_cpu() - phase_start)
        if used >= 60.0:
            raise QualificationV7Failure("v7 capture reached its fixed 60 process-tree CPU-s reservation")
        return 60.0 - used

    try:
        env = prepared["environment"]
        module = prepared["module"]
        encoder = prepared["encoder"]
        runtime = prepared["runtime"]
        model = prepared["model"]
        model_digest = prepared["model_digest"]
        adapter = runner.native_adapter(module, q.SOURCE_QUAL_SEED)
        remaining_cpu = enforce_capture_cpu_budget()
        result = runner._run_one(
            out=V7_CAPTURE_RUNTIME_OUT,
            module=module,
            model=model,
            model_digest=model_digest,
            seed=q.SOURCE_QUAL_SEED,
            arm="P0",
            encoder=encoder,
            capture_checkpoints=True,
            capture_all_roots=True,
            purpose="issue73-source-qualification-exact-debug-owner-replay-v7",
            resource_label=f"SOURCE_QUAL_V7_{q.SOURCE_QUAL_SEED}",
            adapter_override=adapter,
            durable=False,
            capture_trace=False,
            capture_action_history=True,
            cpu_cap_seconds=remaining_cpu,
        )
        enforce_capture_cpu_budget()
        roots = list(result.get("_volatile_roots", []))
        history = list(result.get("_actual_action_history", []))
        if len(history) != int(result.get("completed_transitions", -1)):
            raise QualificationV7Failure("v7 history lacks one actual legal action/step receipt per native transition")
        report = q.compare_capture_run(result, roots, history)
        runner.atomic_json(V7_CAPTURE_HISTORY_PATH, {
            "schema_id": "issue73-actual-action-step-history-v7-v1",
            "source_lock_sha256": sha256_checkout_text_file(lock_path),
            "code_source_commit": lock["code_source_commit"],
            "owner_seed": q.SOURCE_QUAL_SEED,
            "role": "historical debug owner provenance/prefix replay only; not a new owner",
            "source_identity": env["identity"],
            "runtime_identity": runtime,
            "event_count": len(history),
            "events": history,
        })
        root_by_id = {q.stage0.root_id(root): root for root in roots}
        transition_receipts: dict[str, Any] = {}
        for row in config["gate_c_transition_pairs"]:
            enforce_capture_cpu_budget()
            root_id_value, index = row["root_id"], int(row["candidate_index"])
            root = root_by_id.get(root_id_value)
            if root is None:
                raise QualificationV7Failure(f"locked live root was absent from v7 owner replay: {root_id_value}")
            transition_receipts[f"{root_id_value}-a{index}"] = q.capture_transition_witness(adapter, module, root, index)
        enforce_capture_cpu_budget()

        map_root = root_by_id[q.GATE_B_ROOTS["hidden_semantic"]]
        chest_root = root_by_id[q.GATE_B_ROOTS["native_rng_consequence"]]
        post_root = root_by_id[q.GATE_B_ROOTS["post_two_third_family"]]
        hidden_negative = q.stage0.hidden_public_negative(module, map_root)
        chest_source = q.stage0.native_chest_rng_source_evidence()
        gate_b = {
            "schema_id": "issue73-replay-qualified-gate-b-v1",
            "witness_root_ids": dict(q.GATE_B_ROOTS),
            "distinct_screen_families": sorted({str(map_root["screen"]), str(chest_root["screen"]), str(post_root["screen"])}),
            "hidden_semantic": {
                "status": "PASS" if q.stage0.root_has_hidden_semantic(q.compact_root(q.root_receipt(q.GATE_B_ROOTS["hidden_semantic"]))) and hidden_negative.get("status") == "PASS" else "FAIL",
                "source_tags": q.root_receipt(q.GATE_B_ROOTS["hidden_semantic"])["source_tag_mapping"],
                "public_negative_test": hidden_negative,
                "transition_witnesses": {key: value for key, value in transition_receipts.items() if key.startswith(q.GATE_B_ROOTS["hidden_semantic"])},
            },
            "native_rng_consequence": {
                "status": "PASS" if q.stage0.root_has_rng_consequence(q.compact_root(q.root_receipt(q.GATE_B_ROOTS["native_rng_consequence"]))) and chest_source.get("status") == "PASS" else "FAIL",
                "source_evidence": chest_source,
                "unavailable_loot_tags": q.root_receipt(q.GATE_B_ROOTS["native_rng_consequence"])["source_tag_mapping"],
                "forced_action_index": int(config["gate_b_witness_candidate_indices"]["native_rng_consequence"]),
                "transition_witnesses": {key: value for key, value in transition_receipts.items() if key.startswith(q.GATE_B_ROOTS["native_rng_consequence"])},
                "revealed_selected_consequence": {
                    "transition_info": transition_receipts[f"{q.GATE_B_ROOTS['native_rng_consequence']}-a{config['gate_b_witness_candidate_indices']['native_rng_consequence']}"]["transition"]["transition_info"],
                    "post_public_projection": transition_receipts[f"{q.GATE_B_ROOTS['native_rng_consequence']}-a{config['gate_b_witness_candidate_indices']['native_rng_consequence']}"]["transition"]["post_public_projection"],
                    "post_legal_action_occurrences": transition_receipts[f"{q.GATE_B_ROOTS['native_rng_consequence']}-a{config['gate_b_witness_candidate_indices']['native_rng_consequence']}"]["transition"]["post_legal_action_occurrences"],
                },
                "claim_limit": "one exact treasureRng consequence replayed across processes; no distribution or conditional-return claim",
            },
            "post_two_third_family": {
                "status": "PASS" if q.stage0.root_after_two_third_family(q.compact_root(q.root_receipt(q.GATE_B_ROOTS["post_two_third_family"]))) else "FAIL",
                "transition_witnesses": {key: value for key, value in transition_receipts.items() if key.startswith(q.GATE_B_ROOTS["post_two_third_family"])},
            },
            "native_rng_byte_state_exposed": False,
            "native_rng_parity_method": "exact reset+prefix replay plus identical forced transition and post-step observation/public output; includes pinned treasureRng chest generation",
            "normal_public_api_receives_oracle_only_values": hidden_negative.get("status") == "PASS",
            "formal_seed_consumed": False,
        }
        report["transition_witnesses"] = transition_receipts
        report["gate_b"] = gate_b
        report["source_identity"] = env["identity"]
        report["runtime_identity"] = runtime
        report["native_module_path"] = module_path
        report["native_module_sha256"] = native_hash
        report["source_lock_sha256"] = sha256_checkout_text_file(lock_path)
        report["reviewed_source_sha"] = current_source_sha
        report["history_sha256"] = sha256_checkout_text_file(V7_CAPTURE_HISTORY_PATH)
        report["history_bytes"] = V7_CAPTURE_HISTORY_PATH.stat().st_size
        report["all_locked_transition_witnesses_repeated_equal"] = all(row.get("status") == "PASS" for row in transition_receipts.values())
        report["status"] = "PASS" if report["status"] == "PASS" and report["all_locked_transition_witnesses_repeated_equal"] and all(gate_b[name]["status"] == "PASS" for name in q.GATE_B_ROOTS) else "FAIL_SOURCE_HISTORY_OR_GATE_B_CAPTURE"
        enforce_capture_cpu_budget()
        if adapter is not None:
            closing_adapter = adapter
            adapter = None
            closing_adapter.close()
        runner.atomic_json(V7_CAPTURE_STAGING_PATH, {
            "schema_id": "issue73-source-history-capture-staging-v7-v1",
            "source_lock_sha256": sha256_checkout_text_file(lock_path),
            "reviewed_source_sha": current_source_sha,
            "report": report,
        })
        push_v7("Issue 73: checkpoint v7 source-history results before final attempt accounting")
    except BaseException as exc:
        failure = {
            "schema_id": "issue73-source-history-capture-failure-v7-v1",
            "status": "FAIL_OR_INCOMPLETE",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:1600],
            "reviewed_source_sha": current_source_sha,
            "formal_seed_consumed": False,
            "no_retry_or_overwrite": True,
        }
        if adapter is not None:
            try:
                adapter.close()
            except BaseException as close_exc:
                failure["adapter_close_failure"] = f"{type(close_exc).__name__}: {str(close_exc)[:400]}"
        runner.atomic_json(V7_CAPTURE_FAILURE_PATH, failure)
        _finish_capture_attempt_v7(
            started_cpu=phase_start,
            status="FAIL_OR_INCOMPLETE",
            details={"formal_seed_consumed": False, "failure_type": failure["failure_type"]},
        )
        raise

    attempt_receipt = _finish_capture_attempt_v7(
        started_cpu=phase_start,
        status="COMPLETE" if report["status"] == "PASS" else "FAIL_OR_INCOMPLETE",
        details={
            "formal_seed_consumed": False,
            "report_status": report["status"],
            "history_event_count": len(history),
            "source_lock_sha256": sha256_checkout_text_file(lock_path),
        },
    )
    if attempt_receipt.get("status") != "COMPLETE":
        report["status"] = attempt_receipt["status"]
    report["attempt_receipt"] = attempt_receipt
    runner.atomic_json(V7_CAPTURE_REPORT_PATH, report)
    push_v7("Issue 73: preserve uniquely authorized v7 source-history capture evidence")
    return report


def push_v7(message: str) -> str:
    q.git("add", "--", V7_RELATIVE)
    staged = [
        row for row in q.git("diff", "--cached", "--name-only").splitlines() if row
    ]
    if any(row != V7_RELATIVE and not row.startswith(f"{V7_RELATIVE}/") for row in staged):
        raise QualificationV7Failure("refusing to commit evidence outside the v7 receipt directory")
    command, env = runner.git_invocation(REPOSITORY)
    changed = subprocess.run(
        [*command, "diff", "--cached", "--quiet"],
        check=False,
        env=env,
    ).returncode != 0
    if changed:
        q.git("commit", "-m", message)
    commit = q.git("rev-parse", "HEAD")
    q.git("push", "origin", f"HEAD:refs/heads/{q.BRANCH}")
    remote = q.git("ls-remote", "origin", f"refs/heads/{q.BRANCH}").split()
    if not remote or remote[0] != commit:
        raise QualificationV7Failure("GitHub does not contain the exact v7 evidence commit")
    return commit


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("reserve-static", "prepare-lock", "verify-lock", "verify-capture-implementation", "prepare-capture-lock", "verify-capture-lock", "capture"))
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--native-module-path")
    parser.add_argument("--preflight-report-dir")
    parser.add_argument("--capture-profile", choices=("reviewed-v7", "native-capture-repair1"), default="native-capture-repair1")
    parser.add_argument("--reviewed-source-sha")
    args = parser.parse_args(argv)
    if args.command == "reserve-static":
        result = reserve_static_v7()
    elif args.command == "prepare-lock":
        lock = prepare_lock_v7()
        result = {
            "status": lock["status"],
            "source_commit": lock["code_source_commit"],
            "lock_revision": lock["lock_revision"],
            "historical_input_count": len(lock["historical_input_sha256"]),
            "historical_input_manifest_sha256": lock["historical_input_manifest_sha256"],
            "lock_preparation_process_cpu_seconds": lock["lock_preparation_process_cpu_seconds"],
        }
    elif args.command == "verify-lock":
        lock, _config = verify_lock_v7()
        result = {
            "status": lock["status"],
            "source_commit": lock["code_source_commit"],
            "lock_revision": lock["lock_revision"],
            "historical_input_count": len(lock["historical_input_sha256"]),
            "historical_input_manifest_sha256": lock["historical_input_manifest_sha256"],
            "measured_process_tree_cpu_seconds": max(0.0, q.process_tree_cpu() - q._PROCESS_START_CPU),
        }
    elif args.command == "verify-capture-implementation":
        result = verify_native_capture_implementation_v7()
    elif args.command == "prepare-capture-lock":
        result = prepare_native_capture_lock_v7()
    elif args.command == "verify-capture-lock":
        lock, _config = verify_lock_v7(lock_out=V7_CAPTURE_LOCK_OUT, lock_revision=8)
        result = {
            "status": lock["status"],
            "source_commit": lock["code_source_commit"],
            "lock_revision": lock["lock_revision"],
            "historical_input_count": len(lock["historical_input_sha256"]),
            "historical_input_manifest_sha256": lock["historical_input_manifest_sha256"],
        }
    else:
        if args.capture_profile == "native-capture-repair1":
            lock_out, lock_revision = V7_CAPTURE_LOCK_OUT, 8
            default_report_dir = V7_CAPTURE_PREFLIGHT_ROOT
        else:
            lock_out, lock_revision = V7_LOCK_OUT, 7
            default_report_dir = PREFLIGHT_TEST_ROOT / "production-default"
        if args.preflight_only:
            result = capture_preflight_v7(
                preflight_report_dir=Path(args.preflight_report_dir) if args.preflight_report_dir else default_report_dir,
                native_module_path_override=args.native_module_path,
                lock_out=lock_out,
                lock_revision=lock_revision,
            )
        else:
            if args.capture_profile != "native-capture-repair1":
                parser.error("native capture requires the uniquely source-locked native-capture-repair1 profile")
            if not args.reviewed_source_sha:
                parser.error("native capture requires --reviewed-source-sha from the exact independent Reviewer PASS")
            result = capture_source_history_v7(
                reviewed_source_sha=args.reviewed_source_sha,
                native_module_path_override=args.native_module_path,
            )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
