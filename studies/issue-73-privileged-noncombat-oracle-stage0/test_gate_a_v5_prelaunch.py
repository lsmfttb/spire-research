"""Issue 73 Gate A v4 launcher boundary checks for startup preflight v5."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
LAUNCHER_PATH = ROOT / "receipts" / "gate-a-requalification-v4" / "attempts" / "run_gate_a_v5.py"
SPEC = importlib.util.spec_from_file_location("gate_a_v5_launcher_under_test", LAUNCHER_PATH)
assert SPEC is not None and SPEC.loader is not None
LAUNCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAUNCHER)


class GateAV4LauncherBoundaryTests(unittest.TestCase):
    def test_execute_fails_closed_without_precharged_start_receipt(self):
        with (
            mock.patch.object(LAUNCHER, "verify_source_lock", return_value=({}, "lock-sha", "launcher-commit")),
            mock.patch.object(LAUNCHER, "verify_gate_a_inputs", return_value={"root_count": 225}),
            mock.patch.object(LAUNCHER, "EXECUTION_START_PATH", ROOT / "missing-execution-start.json"),
            mock.patch.object(LAUNCHER, "EXECUTION_CLAIM_PATH", ROOT / ".v5-test-claim-do-not-create.json"),
        ):
            with self.assertRaisesRegex(RuntimeError, "not precharged"):
                LAUNCHER.execute_gate_a()
        self.assertFalse((ROOT / ".v5-test-claim-do-not-create.json").exists())

    def test_execute_fails_closed_without_precharged_resource_ledger(self):
        lock = {
            "attempt_key": "gate-a-requalification-after-v9-parity-v4",
            "execution_branch": "codex/issue-73-gatea-v4-readiness",
            "launcher_source_commit": "1" * 40,
        }
        started = {
            "status": "STARTED",
            "attempt_key": lock["attempt_key"],
            "source_lock_sha256": "lock-sha",
            "execution_branch": lock["execution_branch"],
            "launcher_source_commit": lock["launcher_source_commit"],
            "reviewer_preflight_pass_comment": "https://github.com/lsmfttb/spire-research/issues/73#issuecomment-1",
            "reviewed_commit_sha": "2" * 40,
            "reserved_cpu_seconds": 120.0,
        }
        start_path = mock.Mock()
        start_path.is_file.return_value = True
        ledger_path = mock.Mock()
        ledger_path.is_file.return_value = False
        with (
            mock.patch.object(LAUNCHER, "verify_source_lock", return_value=(lock, "lock-sha", "launcher-commit")),
            mock.patch.object(LAUNCHER, "EXECUTION_START_PATH", start_path),
            mock.patch.object(LAUNCHER, "EXECUTION_LEDGER_STARTED_PATH", ledger_path),
            mock.patch.object(LAUNCHER, "read_json", return_value=started),
            mock.patch.object(LAUNCHER, "verify_gate_a_inputs") as verify_inputs,
            mock.patch.object(LAUNCHER, "EXECUTION_CLAIM_PATH", ROOT / ".v5-test-ledger-claim-do-not-create.json"),
        ):
            with self.assertRaisesRegex(RuntimeError, "lacks its precharged resource ledger"):
                LAUNCHER.execute_gate_a()
        verify_inputs.assert_not_called()
        self.assertFalse((ROOT / ".v5-test-ledger-claim-do-not-create.json").exists())

    def test_stale_source_map_fails_before_root_files_are_read(self):
        import os
        import sys

        import numpy as np
        import runner
        import stage0
        import torch

        lock = {
            "config_sha256": runner.CONFIG_SHA256,
            "runtime": {
                "python_version": sys.version.split()[0],
                "numpy_version": np.__version__,
                "torch_version": torch.__version__,
                "python_executable": str(Path(sys.executable).resolve()),
                "execution_uid": os.getuid(),
            },
            "study_source_sha256": {"runner.py": "deliberately-stale"},
        }
        with (
            mock.patch.object(stage0, "source_hashes", return_value={"runner.py": "current"}),
            mock.patch.object(stage0, "load_roots", side_effect=AssertionError("roots must not be loaded")) as load_roots,
        ):
            with self.assertRaisesRegex(RuntimeError, "study source map differs"):
                LAUNCHER.verify_gate_a_inputs(lock)
        load_roots.assert_not_called()

    def test_preflight_mode_is_separate_from_gate_a_execution(self):
        import ast

        tree = ast.parse(LAUNCHER_PATH.read_text(encoding="utf-8"))
        preflight = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_preflight")
        calls = {
            node.func.attr if isinstance(node.func, ast.Call) and isinstance(node.func, ast.Attribute)
            else node.func.id if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            else None
            for node in ast.walk(preflight)
        }
        self.assertNotIn("run_gate_a", calls)
        self.assertNotIn("run_gate_a", {node.func.attr for node in ast.walk(preflight) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)})


if __name__ == "__main__":
    unittest.main()
