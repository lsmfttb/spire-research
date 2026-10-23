"""Static, cross-platform source-lock qualification for Issue 73 v4.

The test reads exact Git blobs and source checkouts only. It does not import or
step the native simulator and does not start any Stage-0 gate.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

import source_lock_integrity as lock_integrity


STUDY = Path(__file__).resolve().parent
REPOSITORY = STUDY.parents[1]
V4_REPORT = STUDY / "receipts" / "source-qualification-v4" / "reports" / "source-lock-cross-platform-preflight-v4.json"
V4_DEBIT = STUDY / "receipts" / "source-qualification-v4" / "attempts" / "builder-prelock-static-verification-v4.json"
IDENTITY = json.loads((STUDY / "config.json").read_text(encoding="utf-8"))["identity"]
ENVIRONMENT = json.loads((STUDY / "receipts" / "source-qualification-v1" / "environment.json").read_text(encoding="utf-8"))


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def canonical_remote(value: str) -> str:
    return value.rstrip("/").removesuffix(".git").lower()


def required_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise AssertionError(f"{name} must point to the task-scoped pinned source worktree")
    path = Path(value).resolve()
    if not path.is_dir():
        raise AssertionError(f"{name} is not a directory: {path}")
    return path


def inspect_source(root: Path, *, kind: str, checkout_label: str) -> dict[str, object]:
    commit_key = "native_commit" if kind == "native" else "stsr_commit"
    repository_key = f"{kind}_repository"
    file_key = f"{kind}_source_files_sha256"
    branch = str(IDENTITY["native_source_branch"]) if kind == "native" else "main"
    expected_commit = str(IDENTITY[commit_key])
    commit = git(root, "rev-parse", "HEAD")
    if commit != expected_commit:
        raise AssertionError(f"{checkout_label} commit differs from the frozen pin: {commit}")
    if git(root, "status", "--porcelain"):
        raise AssertionError(f"{checkout_label} source worktree is not clean")
    origin = git(root, "remote", "get-url", "origin")
    expected_repository = str(IDENTITY[repository_key])
    if canonical_remote(origin) != canonical_remote(expected_repository):
        raise AssertionError(f"{checkout_label} origin differs from the frozen repository")
    remote_ref = f"refs/remotes/origin/{branch}"
    if git(root, "rev-parse", "--verify", remote_ref) != expected_commit:
        raise AssertionError(f"{checkout_label} fetched source ref {remote_ref} does not equal the frozen commit")

    files: dict[str, object] = {}
    for relative, expected_sha in IDENTITY[file_key].items():
        checkout = (root / relative).resolve()
        if not checkout.is_relative_to(root) or not checkout.is_file():
            raise AssertionError(f"{checkout_label}/{relative} is outside or missing from the source checkout")
        blob = subprocess.check_output(["git", "-C", str(root), "show", f"{commit}:{relative}"])
        files[relative] = lock_integrity.verify_locked_source_payload(
            label=f"{checkout_label}/{relative}",
            expected_sha256=str(expected_sha),
            committed_blob=blob,
            checkout_bytes=checkout.read_bytes(),
        )
    return {
        "environment_variable": "STS_LIGHTSPEED_SOURCE_DIR" if kind == "native" else "STSRL_SOURCE_DIR",
        "repository": expected_repository,
        "origin": origin,
        "source_ref": remote_ref,
        "commit": commit,
        "clean_worktree": True,
        "files": files,
    }


def native_binary_sha256() -> str:
    module_path = str(ENVIRONMENT["native_module_path"])
    expected = str(IDENTITY["native_binary_sha256"])
    path = Path(module_path)
    if path.is_file():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    elif os.name == "nt" and module_path.startswith("/"):
        output = subprocess.check_output(["wsl.exe", "-e", "sha256sum", module_path], text=True)
        digest = output.split()[0].lower()
    else:
        raise AssertionError(f"pinned native module is unavailable for static digest verification: {module_path}")
    if digest != expected:
        raise AssertionError(f"native module digest differs from the frozen config: {digest} != {expected}")
    return digest


def adapter_wrapper_source_check() -> dict[str, object]:
    runner_tree = ast.parse((STUDY / "runner.py").read_text(encoding="utf-8"))
    history_controller = next(
        node for node in runner_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ActionHistoryController"
    )
    select_action = next(
        node for node in history_controller.body
        if isinstance(node, ast.FunctionDef) and node.name == "select_action"
    )
    uses_checked_adapter = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "adapter_matches_history_target"
        for node in ast.walk(select_action)
    )
    if not uses_checked_adapter:
        raise AssertionError("action-history controller does not validate the adapter wrapper identity")

    runtime_tree = ast.parse((STUDY / "public_history_execution.py").read_text(encoding="utf-8"))
    metered_adapter = next(
        node for node in runtime_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_MeteredAdapter"
    )
    initializer = next(
        node for node in metered_adapter.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    stores_wrapped_adapter = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr == "_adapter"
            for target in node.targets
        )
        and isinstance(node.value, ast.Name)
        and node.value.id == "adapter"
        for node in initializer.body
    )
    if not stores_wrapped_adapter:
        raise AssertionError("pinned metering wrapper no longer exposes its inner adapter through _adapter")

    target = object()
    meter = type("StaticMeterWrapper", (), {"__init__": lambda self, value: setattr(self, "_adapter", value)})(target)
    if not lock_integrity.adapter_matches_history_target(meter, target):
        raise AssertionError("identity helper rejected the exact single metering-wrapper adapter")
    if lock_integrity.adapter_matches_history_target(object(), target):
        raise AssertionError("identity helper accepted an unrelated adapter")
    return {
        "status": "PASS",
        "runner_action_history_uses_identity_guard": True,
        "pinned_metering_wrapper_exposes_exact_inner_adapter": True,
        "single_wrapper_identity_case": "PASS",
        "unrelated_adapter_rejection": "PASS",
    }


def package_path_check(source_root: Path) -> dict[str, object]:
    package_root = source_root / "src"
    prior_path = list(sys.path)
    prior_module = sys.modules.pop("sts_combat_rl", None)
    try:
        sys.path.insert(0, str(package_root))
        resolved = lock_integrity.resolve_exact_python_package_root(
            source_root, "sts_combat_rl", sys.path,
        )
        if resolved != package_root.resolve():
            raise AssertionError("STSRL package root resolved outside the exact pinned src directory")
        try:
            lock_integrity.resolve_exact_python_package_root(source_root, "sts_combat_rl", [])
        except lock_integrity.SourceLockValidationError:
            wrong_path_rejected = True
        else:
            wrong_path_rejected = False
        if not wrong_path_rejected:
            raise AssertionError("STSRL package resolution passed without its pinned src directory on sys.path")
    finally:
        sys.path[:] = prior_path
        sys.modules.pop("sts_combat_rl", None)
        if prior_module is not None:
            sys.modules["sts_combat_rl"] = prior_module
    return {
        "status": "PASS",
        "logical_root": "STSRL_SOURCE_DIR/src",
        "resolved_under_pinned_source": True,
        "missing_sys_path_rejected": True,
    }


def build_report() -> dict[str, object]:
    started = time.process_time()
    native_crlf = required_path("STS_LIGHTSPEED_SOURCE_DIR")
    stsr_crlf = required_path("STSRL_SOURCE_DIR")
    native_lf = required_path("ISSUE73_NATIVE_LF_SOURCE_DIR")
    stsr_lf = required_path("ISSUE73_STSRL_LF_SOURCE_DIR")
    native_sources = inspect_source(native_crlf, kind="native", checkout_label="native-crlf")
    stsr_sources = inspect_source(stsr_crlf, kind="stsr", checkout_label="stsr-crlf")
    variants = {
        "native_lf": inspect_source(native_lf, kind="native", checkout_label="native-lf"),
        "stsr_lf": inspect_source(stsr_lf, kind="stsr", checkout_label="stsr-lf"),
    }
    return {
        "schema_id": "issue73-source-lock-cross-platform-preflight-v4",
        "status": "PASS",
        "tested_study_source_commit": git(REPOSITORY, "rev-parse", "HEAD"),
        "tested_at_utc": datetime.now(timezone.utc).isoformat(),
        "external_git_sources": {
            "schema_id": "issue73-external-source-git-blob-check-v4",
            "status": "PASS",
            "sources": [native_sources, stsr_sources],
        },
        "cross_platform_eol_variants": variants,
        "stsr_python_import_path": package_path_check(stsr_crlf),
        "adapter_and_metering_wrapper": adapter_wrapper_source_check(),
        "native_loaded_module_path": str(ENVIRONMENT["native_module_path"]),
        "native_loaded_module_sha256": native_binary_sha256(),
        "native_module_digest_source": "SHA256 of the exact module path recorded by the earlier successful pinned runtime environment receipt; verified read-only in this static pass",
        "simulator_imported_or_stepped": False,
        "stage0_gate_started": False,
        "formal_seed_consumed": False,
        "static_preflight_reservation_cpu_seconds": 120.0,
        "measured_current_python_process_cpu_seconds": max(0.0, time.process_time() - started),
        "resource_accounting": "fixed 120 CPU-second non-native preflight reservation; no native Gate B/C debit",
    }


class SourceLockPortabilityTests(unittest.TestCase):
    def test_crlf_and_lf_are_the_only_checkout_difference(self) -> None:
        report = build_report()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["external_git_sources"]["sources"][0]["commit"], IDENTITY["native_commit"])
        self.assertEqual(report["external_git_sources"]["sources"][1]["commit"], IDENTITY["stsr_commit"])
        self.assertTrue(report["stsr_python_import_path"]["missing_sys_path_rejected"])
        self.assertEqual(report["native_loaded_module_sha256"], IDENTITY["native_binary_sha256"])

    def test_payload_verifier_rejects_semantic_changes_and_bare_cr(self) -> None:
        source = b"line one\nline two\n"
        expected = hashlib.sha256(source).hexdigest()
        good = lock_integrity.verify_locked_source_payload(
            label="fixture", expected_sha256=expected, committed_blob=source,
            checkout_bytes=b"line one\r\nline two\r\n",
        )
        self.assertEqual(good["status"], "PASS_EXACT_GIT_BLOB_WITH_EOL_ONLY_CHECKOUT_CONVERSION")
        for changed in (b"line one\nchanged\n", b"line one\rline two\n"):
            with self.assertRaises(lock_integrity.SourceLockValidationError):
                lock_integrity.verify_locked_source_payload(
                    label="fixture", expected_sha256=expected, committed_blob=source,
                    checkout_bytes=changed,
                )

    def test_adapter_identity_and_metering_wrapper_are_checked(self) -> None:
        self.assertEqual(adapter_wrapper_source_check()["status"], "PASS")


def main() -> int:
    debit = {
        "schema_id": "issue73-checkpoint-replay-attempt-v1",
        "key": "builder-prelock-static-verification-v4",
        "kind": "prelock_static_source_config_and_manifest_verification",
        "status": "COMPLETE_CONSERVATIVE_STATIC_DEBIT",
        "native_gate_bc": False,
        "reserved_cpu_seconds": 120.0,
        "cpu_charged_seconds": 120.0,
        "measurement_status": "CONSERVATIVE_FIXED_RESERVATION; UNUSED_CPU_NOT_RECLAIMED",
        "source_lock_sha256": None,
        "formal_seed_consumed": False,
        "note": "Covers the bounded v4 prelock source/static verification pass; charged before verification and remains charged regardless of measured use.",
    }
    V4_DEBIT.parent.mkdir(parents=True, exist_ok=True)
    if V4_DEBIT.exists():
        if json.loads(V4_DEBIT.read_text(encoding="utf-8")) != debit:
            raise RuntimeError("v4 static preflight debit receipt exists with different bytes; refusing to overwrite")
    else:
        V4_DEBIT.write_text(json.dumps(debit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if "--precharge-only" in sys.argv:
        return 0
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SourceLockPortabilityTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    report = build_report()
    V4_REPORT.parent.mkdir(parents=True, exist_ok=True)
    if V4_REPORT.exists():
        raise RuntimeError("v4 static preflight report already exists; refusing to overwrite evidence")
    V4_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"report": V4_REPORT.relative_to(STUDY).as_posix(), "sha256": hashlib.sha256(V4_REPORT.read_bytes()).hexdigest(), "status": report["status"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
