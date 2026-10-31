from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import sys
from types import SimpleNamespace
import unittest

STUDY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDY))

from driver import (
    T_REF,
    bind_issue73_source_paths,
    make_task_wall_forecast,
    save_preflight_receipt,
    seed_table_path,
)
from formal import paired_bootstrap


class FormalAnalysisTests(unittest.TestCase):
    def test_owner_paired_bootstrap_is_deterministic_and_interpolates(self):
        left = {
            "seed-1": {"owner_seed": 1, "terminal_floor": 5},
            "seed-2": {"owner_seed": 2, "terminal_floor": 9},
            "seed-3": {"owner_seed": 3, "terminal_floor": 3},
        }
        right = {
            "seed-1": {"owner_seed": 1, "terminal_floor": 2},
            "seed-2": {"owner_seed": 2, "terminal_floor": 8},
            "seed-3": {"owner_seed": 3, "terminal_floor": 1},
        }
        first = paired_bootstrap(left, right, label="E-D", seed=184, replicates=500)
        second = paired_bootstrap(left, right, label="E-D", seed=184, replicates=500)
        self.assertEqual(first, second)
        self.assertEqual(first["mean_difference_terminal_floor"], 2.0)
        self.assertEqual(first["paired_owner_count"], 3)
        self.assertEqual(first["bootstrap_replicates"], 500)
        self.assertLessEqual(first["ci95"][0], first["ci95"][1])
        self.assertEqual([row["difference"] for row in first["per_owner_differences"]], [3.0, 1.0, 2.0])

    def test_owner_paired_bootstrap_rejects_unmatched_sets_and_seed_mismatch(self):
        left = {"owner": {"owner_seed": 1, "terminal_floor": 5}}
        with self.assertRaises(ValueError):
            paired_bootstrap(left, {}, label="E-D", seed=1, replicates=10)
        right = {"owner": {"owner_seed": 2, "terminal_floor": 4}}
        with self.assertRaises(ValueError):
            paired_bootstrap(left, right, label="E-D", seed=1, replicates=10)

    def test_full_task_forecast_includes_all_pilot_stages_and_one_global_margin(self):
        progress = {
            "training_runs": {arm: {"elapsed_seconds": 2.0} for arm in "ABCDEF"},
            "evaluation_runs": {group: {"elapsed_seconds": 1.0} for group in ("PARENT", *"ABCDEF")},
            "actor_updates": {arm: {"elapsed_seconds": 0.5} for arm in "ABCDEF"},
            "predictor_fits": {
                "D": {"fit": {
                    "feature_cache_build_elapsed_seconds": 0.1,
                    "value": {"examples": 100, "elapsed_seconds": 0.2},
                }},
                "E": {"fit": {"value": {"examples": 100, "elapsed_seconds": 0.2}, "action_value": {"examples": 100, "elapsed_seconds": 0.2}}},
                "F": {"fit": {"value": {"examples": 100, "elapsed_seconds": 0.2}}},
            },
            "clock_elapsed_before": 0.0,
        }
        forecast = make_task_wall_forecast(
            progress, task_wall=20.0, source_lock_sha256="a" * 64, package="b" * 40,
        )
        expected_predictor_rows = round(T_REF * 15_360 * 4)
        self.assertEqual(forecast["formal_predictor_training_rows_estimated_from_t_ref"], expected_predictor_rows)
        self.assertEqual(forecast["pilot_predictor_cache_build_wall_seconds"], 0.1)
        self.assertAlmostEqual(forecast["pilot_predictor_fit_wall_seconds"], 0.9)
        self.assertGreater(forecast["estimated_all_in_active_wall_seconds"], 20.0)
        self.assertEqual(
            forecast["one_task_active_wall_allowance_seconds"],
            forecast["estimated_all_in_active_wall_seconds"] * 1.75,
        )

    def test_preflight_receipt_is_immutable_and_idempotent(self):
        receipt = {
            "schema_id": "issue84-native-preflight-v1",
            "status": "SUCCEEDED_NO_GAMES_STARTED",
            "source_commit": "a" * 40,
            "source_lock_sha256": "b" * 64,
            "native_module_sha256": "c" * 64,
        }
        with tempfile.TemporaryDirectory() as temporary:
            first = save_preflight_receipt(receipt, data_root=Path(temporary))
            self.assertEqual(save_preflight_receipt(receipt, data_root=Path(temporary)), first)
            with self.assertRaisesRegex(FileExistsError, "differs"):
                save_preflight_receipt({**receipt, "native_module_sha256": "d" * 64}, data_root=Path(temporary))
            self.assertEqual(first["status"], "SUCCEEDED_NO_GAMES_STARTED")

    def test_clean_worker_import_path_contains_locked_preflight_dependency(self):
        upstream75 = STUDY / "upstream" / "issue-75-no-expert-oracle"
        relative = "issue-75-no-expert-oracle/preflight.py"
        manifest = json.loads(
            (STUDY / "upstream" / "upstream-manifest.json").read_text(encoding="utf-8")
        )
        record = manifest["files"][relative]
        module_path = upstream75 / "preflight.py"
        self.assertEqual(
            hashlib.sha256(module_path.read_bytes()).hexdigest(), record["sha256"],
        )

        # Worker subprocesses start with a clean interpreter and reconstruct the
        # #75 import roots in load_modules; exercise that exact module lookup.
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "root = Path(sys.argv[1])\n"
            "sys.path[:0] = [str(root), str(root / 'stage1')]\n"
            "import preflight\n"
            "assert Path(preflight.__file__).resolve() == (root / 'preflight.py').resolve()\n"
            "assert preflight.load_json(preflight.CONFIG_PATH)\n"
        )
        subprocess.run(
            [sys.executable, "-c", code, str(upstream75)],
            cwd=STUDY,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_issue75_lineage_inputs_are_vendored_and_locked(self):
        upstream = STUDY / "upstream"
        manifest = json.loads(
            (upstream / "upstream-manifest.json").read_text(encoding="utf-8")
        )
        for name in ("issue74-root-selection-lock.json", "root-provenance.json"):
            relative = f"issue-75-no-expert-oracle/{name}"
            path = upstream / relative
            self.assertTrue(path.is_file(), relative)
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                manifest["files"][relative]["sha256"],
            )

    def test_issue73_source_binding_rebases_the_vendored_runtime_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary)
            runtime = SimpleNamespace()
            preflight = SimpleNamespace()
            bind_issue73_source_paths(runtime, preflight, snapshot)
            issue73 = snapshot / "studies" / "issue-73-privileged-noncombat-oracle-stage0"
            self.assertEqual(runtime.ISSUE73_DIR, issue73)
            self.assertEqual(runtime.ISSUE73_RUNNER_PATH, issue73 / "runner.py")
            self.assertEqual(runtime.ISSUE73_POLICY_PATH, issue73 / "policy.py")
            self.assertEqual(runtime.PI_INIT_PATH, issue73 / "receipts/stage0/policies/pi_init.pt")
            self.assertEqual(preflight.ROOTS, issue73 / "receipts/stage0/roots")
            self.assertEqual(preflight.OWNER_RUNS, issue73 / "receipts/stage0/debug-owner-runs")
            self.assertEqual(
                preflight.SELECTION_PATH,
                STUDY / "upstream" / "issue-75-no-expert-oracle" / "issue74-root-selection-lock.json",
            )

    def test_seed_table_path_is_versioned_by_source_package(self):
        first = seed_table_path("6c30f045f3d308731561d22b3706d99520b7d0a2")
        repaired = seed_table_path("a0d6a6a4e26688db12a40f8d261ec55a88bb5631")
        self.assertNotEqual(first, repaired)
        self.assertEqual(repaired.name, "seed-table.json")
        self.assertIn("a0d6a6a4e26688db12a40f8d261ec55a88bb5631", repaired.parts)


if __name__ == "__main__":
    unittest.main()
