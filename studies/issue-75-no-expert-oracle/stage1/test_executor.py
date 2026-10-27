"""Pure source-bound executor checks; these tests never construct a simulator."""
from __future__ import annotations

import os
from pathlib import Path
import json
import shutil
import subprocess
import textwrap
import sys
import tempfile
import types
import unittest
from unittest import mock

STUDY = Path(__file__).resolve().parents[1]
STAGE1 = STUDY / "stage1"
sys.path.insert(0, str(STAGE1))
sys.path.insert(1, str(STUDY))

import executor


class FakeLedger:
    def __init__(self, *, unit_kind: str):
        self.binding = types.SimpleNamespace(
            package_commit="a" * 40,
            source_lock_sha256="b" * 64,
        )
        self.unit_kind = unit_kind

    def require_open_reservation(self, *, run_id: str, unit_id: str):
        return {
            "run_id": run_id,
            "unit_id": unit_id,
            "unit_kind": self.unit_kind,
            "previous_run_id": None,
            "checkpoint_sha256": None,
        }

    def read_events(self):
        return []


class FakeReportLedger:
    def __init__(self):
        self.binding = types.SimpleNamespace(
            package_commit="a" * 40,
            source_lock_sha256="b" * 64,
        )
        self.events = []

    def read_events(self):
        return list(self.events)

    def reserve(self, **event):
        event = {
            "event": "reserve", "schema_id": "issue75-stage1-budget-event-v2",
            "package_commit": self.binding.package_commit,
            "source_lock_sha256": self.binding.source_lock_sha256, **event,
        }
        self.events.append(event)
        return dict(event)

    def finish(self, *, run_id: str, measured_cpu_seconds: float, status: str, checkpoint_sha256=None, result_sha256=None):
        reservation = next(event for event in self.events if event.get("event") == "reserve" and event["run_id"] == run_id)
        self.events.append({
            "event": "finish",
            "schema_id": "issue75-stage1-budget-event-v2",
            "run_id": run_id,
            "unit_id": reservation["unit_id"],
            "measured_cpu_seconds": measured_cpu_seconds,
            "status": status,
            "checkpoint_sha256": checkpoint_sha256,
            "result_sha256": result_sha256,
            "package_commit": self.binding.package_commit,
            "source_lock_sha256": self.binding.source_lock_sha256,
        })


class FakeReceiptLedger:
    def __init__(self):
        self.binding = types.SimpleNamespace(package_commit="a" * 40, source_lock_sha256="b" * 64)
        self.events = []

    def read_events(self):
        return list(self.events)

    def set_success(self, *, run_id, unit_id, result_sha256):
        self.events = [
            {"event": "reserve", "schema_id": "issue75-stage1-budget-event-v2",
             "run_id": run_id, "unit_id": unit_id, "role": "builder",
             "unit_kind": "owner_arm", "package_commit": self.binding.package_commit,
             "source_lock_sha256": self.binding.source_lock_sha256},
            {"event": "finish", "schema_id": "issue75-stage1-budget-event-v2",
             "run_id": run_id, "unit_id": unit_id, "status": "succeeded",
             "measured_cpu_seconds": 1.0, "reserved_cpu_seconds": 2.0,
             "package_commit": self.binding.package_commit,
             "source_lock_sha256": self.binding.source_lock_sha256,
             "result_sha256": result_sha256},
        ]


class PreflightTreatmentSemanticsTests(unittest.TestCase):
    def test_root_gap_does_not_fail_episode_with_a_valid_later_mutation(self):
        searches = [
            {"root_id": "gap-root", "root_index": 2, "status": "TREATMENT_NOT_DELIVERED", "mutation_attempts": []},
            {
                "root_id": "delivered-root", "root_index": 3, "status": "MUTATION_ATTEMPTED",
                "mutation_attempts": [{
                    "complete_terminal": True,
                    "alternative_differs_from_parent_action": True,
                    "legal_occurrence_count": 3,
                    "trajectory_decision_count": 4,
                }],
            },
        ]
        delivered, root_gaps = executor._preflight_treatment_diagnostics("PREFLIGHT:1:J", "J", searches)
        self.assertTrue(delivered)
        self.assertEqual(root_gaps, [{
            "unit_id": "PREFLIGHT:1:J",
            "root_id": "gap-root",
            "root_index": 2,
            "status": "TREATMENT_NOT_DELIVERED",
        }])

    def test_episode_treatment_requires_a_valid_terminal_later_mutation(self):
        searches = [{
            "root_id": "incomplete-root",
            "root_index": 0,
            "status": "MUTATION_ATTEMPTED",
            "mutation_attempts": [{
                "complete_terminal": False,
                "alternative_differs_from_parent_action": True,
                "legal_occurrence_count": 2,
                "trajectory_decision_count": 4,
            }],
        }]
        delivered, root_gaps = executor._preflight_treatment_diagnostics("PREFLIGHT:2:J", "J", searches)
        self.assertFalse(delivered)
        self.assertEqual(root_gaps, [])


class Stage1ExecutorTests(unittest.TestCase):

    def _source_bound_child_fixture(self, root: Path) -> Path:
        repository = root / "source-bound-child-repo"
        extra_files = (
            "studies/issue-75-no-expert-oracle/stage1/source-lock.json",
            "studies/issue-75-no-expert-oracle/stage1/source-lock.sha256",
            "studies/issue-75-no-expert-oracle/stage1/artifacts/ledger/events.jsonl",
            "studies/issue-75-no-expert-oracle/stage1/artifacts/units/TRAIN-7500001/failure-TRAIN:7500001-5afd7b3277ee47a7.json",
            "studies/issue-75-no-expert-oracle/stage1/artifacts/units/TRAIN-7500001/worker-job-TRAIN:7500001-5afd7b3277ee47a7.json",
        )
        package_files = set(executor.budget.LOCKED_PACKAGE_FILES)
        repository_files = set(executor.budget.LOCKED_REPOSITORY_DEPENDENCIES)
        repository_files.update(extra_files)
        for relative in sorted(package_files):
            source = executor.STUDY / relative
            target = repository / "studies" / "issue-75-no-expert-oracle" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        for relative in sorted(repository_files):
            source = executor.ROOT / relative
            target = repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Issue 75 focused test"], cwd=repository, check=True)
        subprocess.run(
            ["git", "config", "user.email", "issue75-focused-test@example.invalid"],
            cwd=repository, check=True,
        )
        subprocess.run(["git", "add", "--all"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "isolated locked worker validation fixture"],
                       cwd=repository, check=True)
        return repository

    def test_parent_ledger_append_is_validated_by_spawned_nonformal_child(self):
        with tempfile.TemporaryDirectory(dir=STAGE1) as temp_dir:
            repository = self._source_bound_child_fixture(Path(temp_dir))
            study = repository / "studies" / "issue-75-no-expert-oracle"
            stage1 = study / "stage1"
            parent_code = textwrap.dedent(r"""
                import json, os, subprocess, sys
                from pathlib import Path
                stage1 = Path(sys.argv[1])
                study = stage1.parent
                sys.path[:0] = [str(stage1), str(study)]
                import executor
                import stage1_budget as budget
                ledger = budget.open_stage1_ledger(executor.ARTIFACT_ROOT / "ledger")
                estimates, _ = executor._forecast_estimates(ledger)
                recovered = executor._run_scheduler_reservation(ledger, "SCHEDULER:TRAIN", estimates)
                assert recovered["run_id"] == budget.SCHEDULER_TRAIN_RECOVERY_RUN_ID
                assert recovered["cap"] == budget.SCHEDULER_TRAIN_RECOVERY_CAP
                try:
                    executor._run_scheduler_reservation(ledger, "SCHEDULER:TRAIN", estimates)
                except budget.ResourceBlocked:
                    pass
                else:
                    raise AssertionError("recovered scheduler must not reserve a second allocation")
                scheduler_result = executor._finish_scheduler_reservation(
                    ledger, recovered, phase_started_cpu=executor.time.process_time() - 2.0,
                )
                assert executor._existing_scheduler_receipt(ledger, "SCHEDULER:TRAIN") == scheduler_result
                unit_id = "VALIDATION:SPAWNED-CHILD"
                run_id = unit_id + "-" + "a" * 16
                reservation = ledger.reserve(
                    run_id=run_id, unit_id=unit_id, role="builder",
                    reserved_cpu_seconds=1.0, unit_kind="validation_probe",
                )
                unit_dir = executor._unit_directory(unit_id)
                job_path = unit_dir / ("worker-job-" + run_id + ".json")
                job = {
                    "schema_id": executor.WORKER_SCHEMA,
                    "package_commit": ledger.binding.package_commit,
                    "source_lock_sha256": ledger.binding.source_lock_sha256,
                    "unit_id": unit_id, "run_id": run_id, "phase": "validation_probe",
                    "row": {"unit_id": unit_id, "formal": False, "kind": "nonformal_validation"},
                    "output_dir": str(unit_dir / "native"),
                    "result_path": str(unit_dir / "result.json"),
                    "previous_run_id": None, "checkpoint_sha256": None,
                    "retry_reason": reservation.get("retry_reason"),
                    "pre_game_start_proof": reservation.get("pre_game_start_proof"),
                    "pre_game_start_proof_sha256": reservation.get("pre_game_start_proof_sha256"),
                }
                executor._write_once(job_path, job)
                child_code = (
                    "import json,sys; from pathlib import Path; "
                    "sys.path[:0]=[sys.argv[2],str(Path(sys.argv[2]).parent)]; "
                    "import executor; "
                    "j,l,r=executor._validate_worker_job(Path(sys.argv[1]),allow_synthetic=True); "
                    "print(json.dumps({'unit_id':j['unit_id'],'run_id':j['run_id'],"
                    "'package_commit':l.binding.package_commit,'source_lock_sha256':l.binding.source_lock_sha256}))"
                )
                child = subprocess.run(
                    [sys.executable, "-c", child_code, str(job_path), str(stage1)],
                    cwd=study.parents[1], env=os.environ.copy(), text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                print(json.dumps({
                    "child_returncode": child.returncode, "child_stdout": child.stdout,
                    "child_stderr": child.stderr, "job_path": str(job_path),
                    "output_path": str(unit_dir / "native"), "result_path": str(unit_dir / "result.json"),
                    "package_commit": ledger.binding.package_commit,
                    "source_lock_sha256": ledger.binding.source_lock_sha256,
                    "scheduler_run_id": recovered["run_id"],
                    "scheduler_recovery_marker_sha256": budget.sha256_bytes(budget.canonical(recovered["recovery"])),
                    "scheduler_measured_cpu_seconds": scheduler_result["measured_cpu_seconds"],
                    "scheduler_prior_cpu_upper_bound_seconds": scheduler_result["prior_scheduler_cpu_upper_bound_seconds"],
                }))
                if child.returncode != 0:
                    raise SystemExit(child.returncode)
            """)
            env = os.environ.copy()
            env.update({
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(repository / ".python-cache"),
                "TMPDIR": str(repository / ".tmp"),
                "TEMP": str(repository / ".tmp"),
                "TMP": str(repository / ".tmp"),
                "HOME": str(repository / ".home"),
            })
            parent = subprocess.run(
                [sys.executable, "-c", parent_code, str(stage1)], cwd=repository,
                env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertEqual(parent.returncode, 0, parent.stderr + parent.stdout)
            receipt = json.loads(parent.stdout)
            child_receipt = json.loads(receipt["child_stdout"])
            self.assertEqual(receipt["child_returncode"], 0, receipt["child_stderr"])
            self.assertEqual(child_receipt["unit_id"], "VALIDATION:SPAWNED-CHILD")
            self.assertEqual(child_receipt["run_id"], "VALIDATION:SPAWNED-CHILD-" + "a" * 16)
            self.assertEqual(child_receipt["package_commit"], receipt["package_commit"])
            self.assertEqual(child_receipt["source_lock_sha256"], receipt["source_lock_sha256"])
            self.assertEqual(receipt["scheduler_run_id"], "SCHEDULER:TRAIN-3e416101548944b3")
            self.assertEqual(len(receipt["scheduler_recovery_marker_sha256"]), 64)
            self.assertGreater(receipt["scheduler_measured_cpu_seconds"], 0)
            self.assertGreater(receipt["scheduler_prior_cpu_upper_bound_seconds"], 0)
            self.assertLessEqual(receipt["scheduler_measured_cpu_seconds"], 120.0)
            self.assertFalse(Path(receipt["output_path"]).exists())
            self.assertFalse(Path(receipt["result_path"]).exists())

            child_code = (
                "import sys; from pathlib import Path; "
                "sys.path[:0]=[sys.argv[2],str(Path(sys.argv[2]).parent)]; "
                "import executor; executor._validate_worker_job(Path(sys.argv[1]),allow_synthetic=True)"
            )
            algorithm_path = repository / "studies" / "issue-75-no-expert-oracle" / "algorithm.py"
            original_algorithm = algorithm_path.read_bytes()
            algorithm_path.write_bytes(original_algorithm + b"\n")
            changed_code = subprocess.run(
                [sys.executable, "-c", child_code, receipt["job_path"], str(stage1)],
                cwd=repository, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertNotEqual(changed_code.returncode, 0)
            self.assertIn("source lock mismatch", changed_code.stderr)
            algorithm_path.write_bytes(original_algorithm)

            ledger_path = stage1 / "artifacts" / "ledger" / "events.jsonl"
            ledger_bytes = ledger_path.read_bytes()
            ledger_path.write_bytes(ledger_bytes[:-1])
            rewritten_ledger = subprocess.run(
                [sys.executable, "-c", child_code, receipt["job_path"], str(stage1)],
                cwd=repository, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertNotEqual(rewritten_ledger.returncode, 0)
            self.assertTrue(
                "ledger history was changed or truncated" in rewritten_ledger.stderr
                or "torn final ledger event" in rewritten_ledger.stderr
                or "ledger append is malformed" in rewritten_ledger.stderr
            )

    def test_symlinked_ledger_parent_is_rejected_by_parent_and_spawned_child(self):
        with tempfile.TemporaryDirectory(dir=STAGE1) as temp_dir:
            repository = self._source_bound_child_fixture(Path(temp_dir))
            study = repository / "studies" / "issue-75-no-expert-oracle"
            stage1 = study / "stage1"
            parent_code = textwrap.dedent(r"""
                import json, os, shutil, subprocess, sys
                from pathlib import Path
                stage1 = Path(sys.argv[1])
                study = stage1.parent
                repository = stage1.parents[2]
                sys.path[:0] = [str(stage1), str(study)]
                import executor
                import stage1_budget as budget
                ledger_dir = executor.ARTIFACT_ROOT / "ledger"
                ledger = budget.open_stage1_ledger(ledger_dir)
                ledger_path = ledger_dir / "events.jsonl"
                baseline = subprocess.run(
                    ["git", "show", "HEAD:studies/issue-75-no-expert-oracle/stage1/artifacts/ledger/events.jsonl"],
                    cwd=repository, check=True, stdout=subprocess.PIPE,
                ).stdout
                unit_id = "VALIDATION:SYMLINK"
                run_id = unit_id + "-" + "d" * 16
                reservation = ledger.reserve(
                    run_id=run_id, unit_id=unit_id, role="builder",
                    reserved_cpu_seconds=1.0, unit_kind="validation_probe",
                )
                if budget._current_package_binding() != (
                    ledger.binding.package_commit, ledger.binding.source_lock_sha256,
                ):
                    raise AssertionError("the legitimate in-place ledger append was rejected")
                unit_dir = executor._unit_directory(unit_id)
                job_path = unit_dir / ("worker-job-" + run_id + ".json")
                job = {
                    "schema_id": executor.WORKER_SCHEMA,
                    "package_commit": ledger.binding.package_commit,
                    "source_lock_sha256": ledger.binding.source_lock_sha256,
                    "unit_id": unit_id, "run_id": run_id, "phase": "validation_probe",
                    "row": {"unit_id": unit_id, "formal": False, "kind": "nonformal_validation"},
                    "output_dir": str(unit_dir / "native"),
                    "result_path": str(unit_dir / "result.json"),
                    "previous_run_id": None, "checkpoint_sha256": None,
                    "retry_reason": reservation.get("retry_reason"),
                    "pre_game_start_proof": reservation.get("pre_game_start_proof"),
                    "pre_game_start_proof_sha256": reservation.get("pre_game_start_proof_sha256"),
                }
                executor._write_once(job_path, job)
                child_code = (
                    "import sys; from pathlib import Path; "
                    "sys.path[:0]=[sys.argv[2],str(Path(sys.argv[2]).parent)]; "
                    "import executor; executor._validate_worker_job(Path(sys.argv[1]),allow_synthetic=True)"
                )
                copied_ledger = repository / "untracked-ledger-copy"
                copied_ledger.mkdir()
                shutil.copy2(ledger_path, copied_ledger / "events.jsonl")
                shutil.rmtree(ledger_dir)
                ledger_dir.symlink_to(copied_ledger, target_is_directory=True)

                def assert_parent_and_child_reject(label):
                    try:
                        budget.open_stage1_ledger(ledger_dir)
                    except PermissionError as exc:
                        if "symlink" not in str(exc).lower():
                            raise AssertionError(f"{label}: wrong parent rejection: {exc}")
                    else:
                        raise AssertionError(f"{label}: parent accepted a symlinked ledger directory")
                    child = subprocess.run(
                        [sys.executable, "-c", child_code, str(job_path), str(stage1)],
                        cwd=repository, env=os.environ.copy(), text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    if child.returncode == 0 or "symlink" not in child.stderr.lower():
                        raise AssertionError(f"{label}: child did not reject symlinked ledger parent: {child.stderr}")

                assert_parent_and_child_reject("append copy")
                (copied_ledger / "events.jsonl").write_bytes(baseline)
                unchanged_diff = subprocess.run(
                    ["git", "diff", "--no-ext-diff", "--no-renames", "--name-status", "HEAD", "--",
                     "studies/issue-75-no-expert-oracle/stage1/artifacts/ledger/events.jsonl"],
                    cwd=repository, check=True, text=True, stdout=subprocess.PIPE,
                ).stdout
                diff_rows = [line.split("\t", 1) for line in unchanged_diff.splitlines()]
                if len(diff_rows) != 1 or len(diff_rows[0]) != 2 or diff_rows[0][1] != (
                    "studies/issue-75-no-expert-oracle/stage1/artifacts/ledger/events.jsonl"
                ) or diff_rows[0][0] not in {"D", "M"}:
                    raise AssertionError(f"test setup expected only the ledger path to differ, got {unchanged_diff!r}")
                assert_parent_and_child_reject("copy matching committed bytes")
                print(json.dumps({"status": "SYMLINK_REJECTED", "ledger_diff_when_copy_matches_head": unchanged_diff}))
            """)
            env = os.environ.copy()
            env.update({
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(Path(temp_dir) / ".python-cache"),
                "TMPDIR": str(Path(temp_dir) / ".tmp"),
                "TEMP": str(Path(temp_dir) / ".tmp"),
                "TMP": str(Path(temp_dir) / ".tmp"),
                "HOME": str(Path(temp_dir) / ".home"),
            })
            parent = subprocess.run(
                [sys.executable, "-c", parent_code, str(stage1)], cwd=repository,
                env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertEqual(parent.returncode, 0, parent.stderr + parent.stdout)
            result = json.loads(parent.stdout)
            self.assertEqual(result["status"], "SYMLINK_REJECTED")
            diff_rows = result["ledger_diff_when_copy_matches_head"].splitlines()
            self.assertEqual(len(diff_rows), 1)
            status, changed_path = diff_rows[0].split("\t", 1)
            self.assertIn(status, {"D", "M"})
            self.assertEqual(changed_path, "studies/issue-75-no-expert-oracle/stage1/artifacts/ledger/events.jsonl")

    def test_block0_phase_replacement_is_manifest_bound_and_append_only(self):
        source_events = STAGE1 / "artifacts" / "ledger" / "events.jsonl"
        with tempfile.TemporaryDirectory(dir=STAGE1) as temp:
            root = Path(temp) / "artifacts"
            ledger_dir = root / "ledger"
            ledger_dir.mkdir(parents=True)
            shutil.copy2(source_events, ledger_dir / "events.jsonl")
            manifest = json.loads((STAGE1 / "block0-repair-lineage.json").read_text())
            for item in manifest["owners"]:
                source = STAGE1 / "artifacts" / "units" / item["unit_id"].replace(":", "-") / "result.json"
                target = root / "units" / item["unit_id"].replace(":", "-") / "result.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            binding = ("c" * 40, "d" * 64)
            with mock.patch.object(executor.budget, "_current_package_binding", return_value=binding):
                ledger = executor.budget.Stage1Ledger(ledger_dir)
                owner = manifest["owners"][0]
                info = ledger.block0_phase_repair_info(unit_id=owner["unit_id"])
                self.assertEqual(info["repair_predecessor_run_id"], owner["replacement_predecessor_run_id"])
                run_id = owner["unit_id"] + "-" + "9" * 16
                reservation = ledger.reserve(
                    run_id=run_id, unit_id=owner["unit_id"], role="builder",
                    reserved_cpu_seconds=info["reserved_cpu_seconds"], unit_kind="owner_arm",
                    repair_reason=info["repair_reason"],
                    repair_manifest_sha256=info["repair_manifest_sha256"],
                    repair_predecessor_run_id=info["repair_predecessor_run_id"],
                    repair_predecessor_receipt_sha256=info["repair_predecessor_receipt_sha256"],
                )
                self.assertEqual(reservation["repair_reason"], executor.budget.BLOCK0_PHASE_REPAIR_REASON)
                ledger.require_open_reservation(run_id=run_id, unit_id=owner["unit_id"])
                with self.assertRaises(executor.budget.ResourceBlocked):
                    ledger.block0_phase_repair_info(unit_id=owner["unit_id"])
                with self.assertRaises(executor.budget.ResourceBlocked):
                    ledger.reserve(
                        run_id=owner["unit_id"] + "-" + "8" * 16,
                        unit_id=owner["unit_id"], role="builder",
                        reserved_cpu_seconds=info["reserved_cpu_seconds"], unit_kind="owner_arm",
                        repair_reason=info["repair_reason"],
                        repair_manifest_sha256=info["repair_manifest_sha256"],
                        repair_predecessor_run_id=info["repair_predecessor_run_id"],
                        repair_predecessor_receipt_sha256=info["repair_predecessor_receipt_sha256"],
                    )

    def test_next_training_reservation_advances_past_completed_block0_replacement(self):
        rows = [row for row in executor._unit_rows("train") if row["block_index"] == 0]
        first, second = rows[:2]
        first_history_length = executor._block0_manifest_history_length(first["unit_id"])
        second_history_length = executor._block0_manifest_history_length(second["unit_id"])
        replacement_run_id = first["unit_id"] + "-" + "9" * 16
        units = {
            first["unit_id"]: [f"old-{index}" for index in range(first_history_length)] + [replacement_run_id],
            second["unit_id"]: [f"old-{index}" for index in range(second_history_length)],
        }
        runs = {replacement_run_id: {"finish": {"status": "succeeded"}}}
        ledger = mock.Mock()
        estimates = {first["unit_id"]: 1.0, second["unit_id"]: 2.0}
        with mock.patch.object(executor, "_event_state", return_value=(runs, units)):
            next_unit, next_cap = executor._next_training_reservation(ledger, estimates)
        self.assertEqual((next_unit, next_cap), (second["unit_id"], 2.0))
        ledger.validate_block0_phase_repair_state.assert_called_once_with()
        ledger.block0_phase_repair_info.assert_called_once_with(unit_id=second["unit_id"])

    def test_update0_retry_requires_completed_replacements_and_preserves_single_use(self):
        source_events = STAGE1 / "artifacts" / "ledger" / "events.jsonl"
        events = executor.budget._events(source_events)
        runs, units = executor.budget._state(events)
        with self.assertRaises(executor.budget.ResourceBlocked):
            executor.budget._validate_update0_retry_state(
                STAGE1 / "artifacts", runs, units, ("a" * 40, "b" * 64),
            )

        manifest_path = STAGE1 / "block0-repair-lineage.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        update = manifest["update_retry"]
        predecessor = update["failed_predecessor_run_id"]
        retry_unit = update["unit_id"]
        cap = float(update["replacement_reservation_cpu_seconds"])
        info = {
            "repair_reason": executor.budget.UPDATE0_RETRY_REASON,
            "repair_manifest_sha256": executor.budget.sha256_bytes(manifest_path.read_bytes()),
            "repair_predecessor_run_id": predecessor,
            "repair_predecessor_receipt_sha256": update["failure_receipt_sha256"],
            "reserved_cpu_seconds": cap,
        }
        ledger = self._test_ledger("update-retry-ledger")
        ledger.directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_events, ledger.events_path)
        self.assertEqual(executor.budget._state(ledger.read_events())[1][retry_unit], [predecessor])
        run_id = retry_unit + "-" + "a" * 16
        with mock.patch.object(ledger, "update0_retry_info", return_value=info):
            reservation = ledger.reserve(
                run_id=run_id, unit_id=retry_unit, role="builder",
                reserved_cpu_seconds=cap, unit_kind="training_update",
                previous_run_id=predecessor, checkpoint_sha256=None,
                repair_reason=info["repair_reason"],
                repair_manifest_sha256=info["repair_manifest_sha256"],
                repair_predecessor_run_id=info["repair_predecessor_run_id"],
                repair_predecessor_receipt_sha256=info["repair_predecessor_receipt_sha256"],
            )
        self.assertEqual(reservation["repair_reason"], info["repair_reason"])
        self.assertEqual(reservation["previous_run_id"], predecessor)
        self.assertEqual(reservation["checkpoint_sha256"], None)
        ledger.finish(run_id=run_id, measured_cpu_seconds=0.1, status="failed")
        with mock.patch.object(ledger, "validate_update0_retry_state", return_value=info):
            with self.assertRaises(executor.budget.ResourceBlocked):
                ledger.update0_retry_info()
    def test_pre_game_start_retry_is_single_exact_row_and_rejects_game_artifacts(self):
        tracked_ledger = executor.budget.open_stage1_ledger(STAGE1 / "artifacts" / "ledger")
        tracked_events = tracked_ledger.read_events()
        predecessor = executor.budget.PRE_GAME_START_RETRY_RUN_ID
        events = []
        for event in tracked_events:
            events.append(event)
            if event.get("event") == "finish" and event.get("run_id") == predecessor:
                break
        self.assertTrue(events)
        self.assertEqual(events[-1].get("run_id"), predecessor)

        original_receipt = (
            STAGE1 / "artifacts" / "units" / "TRAIN-7500001"
            / f"failure-{predecessor}.json"
        )
        original_job = (
            STAGE1 / "artifacts" / "units" / "TRAIN-7500001"
            / f"worker-job-{predecessor}.json"
        )
        receipt_sha = executor._file_sha256(original_receipt)
        cap = executor.budget.PRE_GAME_START_RETRY_RESERVED_CPU_SECONDS
        with tempfile.TemporaryDirectory(dir=STAGE1) as temp_dir:
            artifacts = Path(temp_dir) / "artifacts"
            ledger_dir = artifacts / "ledger"
            ledger_dir.mkdir(parents=True)
            (ledger_dir / "events.jsonl").write_bytes(
                b"".join(executor.budget.canonical(event) + b"\n" for event in events)
            )
            unit_dir = artifacts / "units" / "TRAIN-7500001"
            unit_dir.mkdir(parents=True)
            shutil.copy2(original_receipt, unit_dir / original_receipt.name)
            shutil.copy2(original_job, unit_dir / original_job.name)
            ledger = executor.budget.Stage1Ledger(ledger_dir)
            proof = ledger.pre_game_start_retry_info(
                unit_id=executor.budget.PRE_GAME_START_RETRY_UNIT_ID,
                predecessor_run_id=predecessor,
            )
            before = ledger.read_events()
            retry = ledger._reservation_event(
                before, run_id="TRAIN:7500001-" + "b" * 16,
                unit_id="TRAIN:7500001", role="builder",
                reserved_cpu_seconds=cap, unit_kind="owner_arm",
                previous_run_id=predecessor,
                retry_reason=executor.budget.PRE_GAME_START_RETRY_REASON,
                pre_game_start_proof=proof,
            )
            self.assertEqual(retry["previous_run_id"], predecessor)
            self.assertLessEqual(retry["reserved_cpu_seconds"], cap)
            with self.assertRaises(executor.budget.ResourceBlocked):
                ledger._reservation_event(
                    [*before, retry], run_id="TRAIN:7500001-" + "c" * 16,
                    unit_id="TRAIN:7500001", role="builder",
                    reserved_cpu_seconds=cap, unit_kind="owner_arm",
                    previous_run_id=retry["run_id"],
                    retry_reason=executor.budget.PRE_GAME_START_RETRY_REASON,
                    pre_game_start_proof=proof,
                )
            self.assertEqual(ledger.read_events(), before)
            self.assertEqual(tracked_ledger.read_events(), tracked_events)
            self.assertEqual(executor._file_sha256(original_receipt), receipt_sha)

            (unit_dir / "native").mkdir()
            runs, units = executor.budget._state(before)
            with self.assertRaises(executor.budget.ResourceBlocked):
                executor.budget._pre_game_start_failure_proof(
                    artifacts, runs, units,
                    predecessor_run_id=predecessor,
                    require_pristine=True,
                )

    def test_formal_forecast_checks_next_repair_reservation_and_live_caps(self):
        ledger = executor.budget.open_stage1_ledger(STAGE1 / "artifacts" / "ledger")
        before = ledger.read_events()
        estimates, _ = executor._forecast_estimates(ledger)
        result = executor._formal_forecast(ledger, phase="training")
        self.assertEqual(result["status"], "NEXT_RESERVATION_FITS")
        self.assertEqual(result["phase"], "training")
        self.assertEqual(result["next_unit_id"], "TRAIN:7500001")
        self.assertEqual(result["next_unit_reservation_cpu_seconds"], estimates["TRAIN:7500001"])
        self.assertTrue(result["full_cohort_projection_is_not_a_resource_exhaustion_finding"])
        self.assertLessEqual(result["builder_after_next_cpu_seconds"], executor.budget.BUILDER_LIMIT)
        self.assertLessEqual(result["all_in_after_next_cpu_seconds"], executor.budget.ALL_IN_LIMIT)
        self.assertEqual(ledger.read_events(), before)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=STAGE1)
        self.addCleanup(self.temporary.cleanup)
        self.original_artifact_root = executor.ARTIFACT_ROOT
        executor.ARTIFACT_ROOT = Path(self.temporary.name) / "artifacts"
        self.addCleanup(setattr, executor, "ARTIFACT_ROOT", self.original_artifact_root)

    def _test_ledger(self, name="ledger"):
        binding = executor.budget.PackageBinding(
            package_commit="a" * 40,
            source_lock_sha256="b" * 64,
        )
        with mock.patch.object(
            executor.budget, "_current_package_binding",
            return_value=(binding.package_commit, binding.source_lock_sha256),
        ):
            return executor.budget.Stage1Ledger(executor.ARTIFACT_ROOT / name)

    def _reserve_test_unit(self, ledger, *, unit_id, run_id, cap=2.0, unit_kind="owner_arm", **metadata):
        return ledger.reserve(
            run_id=run_id,
            unit_id=unit_id,
            role="builder",
            reserved_cpu_seconds=cap,
            unit_kind=unit_kind,
            **metadata,
        )

    def test_write_once_accepts_crlf_checkout_without_rewriting_or_replacing_receipt(self):
        path = executor.ARTIFACT_ROOT / "crlf-receipt.json"
        value = {"status": "retained", "text": "unchanged"}
        canonical = executor._canonical(value) + b"\n"
        checked_out = canonical.replace(b"\n", b"\r\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(checked_out)

        self.assertEqual(executor._write_once(path, value), executor._sha256(canonical))
        self.assertEqual(path.read_bytes(), checked_out)
        with self.assertRaises(FileExistsError):
            executor._write_once(path, {**value, "status": "changed"})
        self.assertEqual(path.read_bytes(), checked_out)

    def test_paired_owner_differences_bind_all_96_owner_pairs(self):
        rows = []
        for owner in range(96):
            for arm in ("J", "P1"):
                rows.append({
                    "arm": arm,
                    "paired_owner_key": f"heldout-{owner}",
                    "terminal_floor": owner + (1 if arm == "J" else 0),
                })
        rows.reverse()
        self.assertEqual(executor._paired_differences(rows, "J", "P1", "terminal_floor"), [1.0] * 96)
        with self.assertRaises(PermissionError):
            executor._paired_differences(rows[:-1], "J", "P1", "terminal_floor")

    def test_bootstrap_is_deterministic_and_reports_nearest_rank_interval(self):
        seed = "c" * 64
        first = executor._bootstrap_ci_95([2.0] * 96, seed_sha256=seed, replicates=200)
        second = executor._bootstrap_ci_95([2.0] * 96, seed_sha256=seed, replicates=200)
        self.assertEqual(first, second)
        self.assertEqual((first["lower"], first["upper"]), (2.0, 2.0))
        self.assertEqual(first["replicates"], 200)
        formal_shape = executor._bootstrap_ci_95([2.0] * 96, seed_sha256=seed)
        self.assertEqual(formal_shape["replicates"], 20_000)
        self.assertEqual((formal_shape["lower"], formal_shape["upper"]), (2.0, 2.0))

    def test_search_coverage_counts_mutation_depth_and_actual_action_changes(self):
        outcomes = [{
            "arm": "J",
            "search": [{
                "status": "MUTATION_ATTEMPTED",
                "started_attempts": 3,
                "completed_trajectories": 2,
                "cpu_seconds": 4.5,
                "root_action_changed_from_sampled_policy": True,
                "mutation_attempts": [
                    {"parent_decision_index": 1, "complete_terminal": True},
                    {"parent_decision_index": 2, "complete_terminal": False},
                ],
            }],
            "resource": {"measured_process_tree_cpu_seconds": 5.5, "status": "succeeded"},
        }]
        summary = executor._search_coverage(outcomes)["J"]
        self.assertEqual(summary["root_opportunities_searched"], 1)
        self.assertEqual(summary["mutation_attempts_started"], 2)
        self.assertEqual(summary["mutation_terminal_trajectories"], 1)
        self.assertEqual(summary["mutation_at_or_after_second_future_choice"], 1)
        self.assertEqual(summary["root_actions_changed_from_sampled_policy"], 1)
        self.assertEqual(summary["unit_process_tree_cpu_seconds"], 5.5)

    def test_report_analysis_is_owner_paired_and_resumed_from_its_receipt(self):
        outcomes = []
        for owner in range(96):
            for arm_index, arm in enumerate(executor.ARMS):
                outcomes.append({
                    "unit_id": f"HELDOUT:{owner}:{arm}",
                    "owner_seed": owner,
                    "arm": arm,
                    "paired_owner_key": f"heldout-{owner}",
                    "terminal_floor": float(owner + arm_index),
                    "heart_victory": (owner + arm_index) % 7 == 0,
                    "search": [],
                    "resource": {"measured_process_tree_cpu_seconds": 0.25, "status": "succeeded"},
                })
        ledger = FakeReportLedger()
        first = executor._run_report_analysis(ledger, outcomes)
        second = executor._run_report_analysis(ledger, outcomes)
        self.assertEqual(first, second)
        self.assertEqual(sum(event["event"] == "reserve" for event in ledger.events), 1)
        self.assertEqual(first["analysis"]["paired_terminal_floor_contrasts"]["J-P1"]["n"], 96)
        self.assertEqual(
            first["analysis"]["paired_terminal_floor_contrasts"]["J-P1"]["terminal_floor_bootstrap_ci_95"]["replicates"],
            20_000,
        )
        self.assertEqual(first["analysis"]["search_coverage_and_resource_summary"]["P0"]["owner_rows"], 96)

    def test_registered_rows_are_disjoint_and_bind_the_five_arm_policy_sources(self):
        rows = executor._rows()
        by_phase = {
            phase: [row for row in rows if row["phase"] == phase]
            for phase in ("preflight", "training", "heldout")
        }
        self.assertEqual(tuple(map(len, by_phase.values())), (40, 512, 480))
        seed_sets = {
            phase: {int(row["owner_seed"]) for row in phase_rows}
            for phase, phase_rows in by_phase.items()
        }
        self.assertTrue(seed_sets["preflight"].isdisjoint(seed_sets["training"]))
        self.assertTrue(seed_sets["preflight"].isdisjoint(seed_sets["heldout"]))
        self.assertTrue(seed_sets["training"].isdisjoint(seed_sets["heldout"]))
        preflight = {row["arm"]: row for row in by_phase["preflight"] if row["owner_seed"] == 7520001}
        self.assertEqual(set(preflight), set(executor.ARMS))
        self.assertEqual(executor._arm_policy("preflight", "P0", preflight["P0"]), ("pi0", False))
        for arm in ("P1", "R", "J"):
            self.assertEqual(executor._arm_policy("preflight", arm, preflight[arm]), ("pi-stage0-proxy", True))
        self.assertEqual(executor._arm_policy("preflight", "E", preflight["E"]), ("pi0", False))
        formal = {row["arm"]: row for row in by_phase["heldout"] if row["owner_seed"] == 7510001}
        self.assertEqual(executor._arm_policy("heldout", "P0", formal["P0"]), ("pi0", False))
        for arm in ("P1", "R", "J"):
            self.assertEqual(executor._arm_policy("heldout", arm, formal[arm]), ("pi4", False))
        for block in range(4):
            row = next(row for row in by_phase["training"] if row["block_index"] == block)
            self.assertEqual(executor._arm_policy("training", "TRAIN", row), (f"pi{block}", False))

    def test_preflight_proxy_load_checks_both_artifact_and_model_digest(self):
        try:
            model, digest, source = executor._load_model("pi-stage0-proxy", proxy=True)
        except ModuleNotFoundError as exc:
            if exc.name in {"numpy", "torch"}:
                self.skipTest(f"pinned policy runtime dependency unavailable: {exc.name}")
            raise
        self.assertIsNotNone(model)
        self.assertEqual(digest, "5d93c1c52582f9c493edda874764f14d8558d718088d3ca5886231a5c5b6688a")
        self.assertEqual(source["source"], "P1_PREFLIGHT_PROXY_ONLY")
        self.assertEqual(source["artifact_sha256"], "e4eea4fa34131dab2e697dbf59897734ce81dc4713a08c3a55cd89dec10c1416")
        self.assertFalse(source["formal_use"])
        proxy_path = STAGE1 / "preflight-proxy.json"
        original_read_json = executor._read_json

        def altered_proxy(path):
            value = original_read_json(path)
            if Path(path) == proxy_path:
                value["policy_model_digest"] = "0" * 64
            return value

        with mock.patch.object(executor, "_read_json", side_effect=altered_proxy):
            with self.assertRaises(PermissionError):
                executor._load_model("pi-stage0-proxy", proxy=True)

    def test_pi0_rejects_existing_checkpoint_with_wrong_registered_digest(self):
        try:
            import torch
            from model import ActionConditionedPolicy
        except ModuleNotFoundError as exc:
            if exc.name in {"numpy", "torch"}:
                self.skipTest(f"pinned policy runtime dependency unavailable: {exc.name}")
            raise
        config = executor._read_json(STUDY / "config.json")
        model = ActionConditionedPolicy(
            dimension=executor.POLICY_DIMENSION,
            seed=int(config["policy"]["initial_model_seed"]),
            final_weight_std=float(config["policy"]["actor_final_weight_std"]),
        ).eval()
        state = model.state_dict()
        first = next(iter(state))
        altered = state[first].clone()
        altered.reshape(-1)[0] += 0.01
        state[first] = altered
        path = executor._model_path("pi0")
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(state, path)
        with self.assertRaisesRegex(PermissionError, "registered initial-model digest"):
            executor._load_model("pi0")

    def test_reused_successful_receipts_bind_registered_row_and_original_bytes(self):
        ledger = FakeReceiptLedger()
        row = next(item for item in executor._unit_rows("preflight") if item["arm"] == "E")
        unit_id = str(row["unit_id"])
        run_id = unit_id + "-" + "f" * 16
        source = executor._read_json(executor.STAGE1 / "config.json")["source"]["registered_stage0_config"]
        digest, policy_source = executor._expected_policy_binding(ledger, "preflight", row)
        result = {
            "schema_id": executor.UNIT_SCHEMA,
            "status": "SUCCEEDED",
            "package_commit": ledger.binding.package_commit,
            "source_lock_sha256": ledger.binding.source_lock_sha256,
            "study_id": executor.STUDY_ID,
            "unit_id": unit_id,
            "phase": "preflight",
            "formal": False,
            "owner_seed": row["owner_seed"],
            "game_seed": row["game_seed"],
            "arm": "E",
            "paired_owner_key": row["paired_owner_key"],
            "terminal_outcome": "LOSS",
            "terminal_floor": 0,
            "terminal_act": 1,
            "heart_victory": False,
            "act2_plus": False,
            "act3_plus": False,
            "act4_reached": False,
            "completed_transitions": 1,
            "native_source_commit": source["sts_lightspeed_commit"],
            "stsr_source_commit": source["stsr_source_commit"],
            "native_module_sha256": source["native_module_sha256"],
            "model_digest": digest,
            "policy_source": policy_source,
            "expert_decisions": 1,
            "search": [],
            "training_features": None,
            "resource": {"ledger_run_id": run_id},
        }
        path = executor._unit_directory(unit_id) / "result.json"
        executor._write_once(path, result)
        ledger.set_success(run_id=run_id, unit_id=unit_id, result_sha256=executor._file_sha256(path))
        self.assertEqual(executor._existing_unit_receipt(ledger, unit_id), result)

        edited = dict(result)
        edited["terminal_floor"] = 999
        path.write_bytes(executor._canonical(edited) + b"\n")
        with self.assertRaisesRegex(PermissionError, "immutable ledger finish"):
            executor._existing_unit_receipt(ledger, unit_id)

        tampered = dict(result)
        tampered["owner_seed"] = int(row["owner_seed"]) + 1
        path.write_bytes(executor._canonical(tampered) + b"\n")
        ledger.set_success(run_id=run_id, unit_id=unit_id, result_sha256=executor._file_sha256(path))
        with self.assertRaisesRegex(PermissionError, "exact registered row"):
            executor._existing_unit_receipt(ledger, unit_id)

        for field, value in (("arm", "J"), ("package_commit", "c" * 40),
                             ("source_lock_sha256", "d" * 64), ("model_digest", "e" * 64)):
            with self.subTest(field=field):
                tampered = dict(result)
                tampered[field] = value
                path.write_bytes(executor._canonical(tampered) + b"\n")
                ledger.set_success(run_id=run_id, unit_id=unit_id, result_sha256=executor._file_sha256(path))
                with self.assertRaises(PermissionError):
                    executor._existing_unit_receipt(ledger, unit_id)

    def test_expert_is_constructed_only_for_the_isolated_e_arm(self):
        row = next(row for row in executor._rows() if row["phase"] == "preflight" and row["arm"] == "E")
        output_dir = executor.ARTIFACT_ROOT / "expert-output"
        config = executor._read_json(STUDY / "config.json")

        class ExpertOnlyRunner:
            def _run_one(self, **kwargs):
                self_test = kwargs
                if self_test["arm"] != "E" or self_test["model"] is not None:
                    raise AssertionError("Expert must be isolated from learned-policy actions")
                return {
                    "terminal": {
                        "outcome": "heart_win", "terminal_floor": 4, "terminal_act": 4,
                        "heart_victory": True, "act2_plus": True, "act3_plus": True, "act4_reached": True,
                    },
                    "completed_transitions": 12,
                    "expert_decisions": 1,
                    "cpu_seconds": 0.0,
                    "wall_seconds": 0.0,
                }

        fake_runtime = types.SimpleNamespace(
            initialize_native_runtime=lambda *args, **kwargs: (config, ExpertOnlyRunner(), object(), object()),
        )
        job = {"row": row, "phase": "preflight", "output_dir": str(output_dir), "run_id": row["unit_id"] + "-" + "f" * 16}
        with mock.patch.dict(sys.modules, {"runtime": fake_runtime}):
            result = executor._worker_owner(job, FakeLedger(unit_kind="owner_arm"))
        self.assertEqual(result["policy_source"]["source"], "isolated_expert_reference")
        self.assertEqual(result["expert_decisions"], 1)
        self.assertEqual(result["policy_source"]["formal_use"], True)

    def test_cli_dispatches_authorized_formal_phase_without_running_it_in_the_test(self):
        import contextlib
        import io

        report = {
            "status": "TRAINING_COMPLETE",
            "package_commit": "a" * 40,
            "source_lock_sha256": "b" * 64,
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(executor, "run_phase", return_value=report) as run_phase,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = executor.main(["--phase", "train"])
        self.assertEqual(status, 0)
        run_phase.assert_called_once_with("train", output_dir=executor.ARTIFACT_ROOT)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "TRAINING_COMPLETE")
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(executor.ARTIFACT_ROOT.exists())

    def test_preflight_resume_propagates_blocked_forecast_and_cli_exit(self):
        import contextlib
        import io

        executor.ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        report = {
            "status": "PREFLIGHT_COMPLETE_PENDING_INDEPENDENT_REVIEW",
            "package_commit": "a" * 40,
            "source_lock_sha256": "b" * 64,
        }
        forecast = {"status": "RESOURCE_BLOCKED", "reason": "full-cohort forecast exceeds remaining cap"}
        executor._write_once(executor.ARTIFACT_ROOT / "preflight-report.json", report)
        executor._write_once(executor.ARTIFACT_ROOT / "cost-forecast.json", forecast)

        resumed = executor._run_preflight(self._test_ledger())
        self.assertEqual(resumed["status"], "RESOURCE_BLOCKED")
        self.assertEqual(resumed["report"], report)
        self.assertEqual(resumed["cost_forecast"], forecast)

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(executor, "run_phase", return_value=resumed),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = executor.main(["--phase", "preflight"])
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        cli_report = json.loads(stderr.getvalue())
        self.assertEqual(cli_report["status"], "RESOURCE_BLOCKED")
        self.assertEqual(cli_report["phase"], "preflight")

    def test_phase_rejects_a_missing_source_binding_before_creating_artifacts(self):
        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "verify_stage1_phase"),
            mock.patch.object(
                executor.budget,
                "open_stage1_ledger",
                side_effect=PermissionError("package source binding unavailable"),
            ) as open_ledger,
        ):
            with self.assertRaises(PermissionError):
                executor.run_phase("preflight", output_dir=executor.ARTIFACT_ROOT)
        open_ledger.assert_called_once()
        self.assertFalse(executor.ARTIFACT_ROOT.exists())

    def test_training_runs_all_128_owner_rows_before_each_block_update(self):
        order = []
        ledger = FakeLedger(unit_kind="owner_arm")
        update_receipts = {}

        def run_owner_batch(_ledger, rows, *, phase, estimates):
            self.assertEqual(phase, "train")
            self.assertEqual(len(rows), 1)
            row = rows[0]
            order.append(("owner", int(row["block_index"]), int(row["owner_seed"])))
            return [{"status": "SUCCEEDED", "unit_id": row["unit_id"]}]

        def update(_ledger, block, *, estimates):
            order.append(("update", block, None))
            path = executor.ARTIFACT_ROOT / "units" / f"UPDATE-{block}" / "result.json"
            receipt = {
                "status": "SUCCEEDED", "unit_id": f"UPDATE:{block}",
                "model_checkpoint": {"block_index": block},
            }
            executor._write_once(path, receipt)
            update_receipts[f"UPDATE:{block}"] = receipt
            return receipt

        def existing_receipt(_ledger, unit_id):
            return update_receipts.get(unit_id)

        with (
            mock.patch.object(executor, "_formal_forecast", return_value={
                "preflight_report_sha256": "1" * 64, "cost_forecast_sha256": "2" * 64,
            }),
            mock.patch.object(executor, "_forecast_estimates", return_value=({}, {})),
            mock.patch.object(executor, "_formal_concurrency", return_value=1),
            mock.patch.object(executor, "_run_scheduler_reservation", return_value={
                "run_id": "scheduler-test", "unit_id": "SCHEDULER:TRAIN",
                "cap": executor.FORMAL_SCHEDULER_RESERVATION,
            }) as scheduler_reservation,
            mock.patch.object(executor, "_finish_scheduler_reservation") as finish_scheduler,
            mock.patch.object(executor, "_existing_unit_receipt", side_effect=existing_receipt),
            mock.patch.object(executor, "_run_owner_batch", side_effect=run_owner_batch),
            mock.patch.object(executor, "_all_units_succeeded", return_value=True),
            mock.patch.object(executor, "_run_update_unit", side_effect=update),
            mock.patch.object(executor, "_load_model", return_value=(None, "3" * 64, {"source": "pi4"})),
        ):
            report = executor._run_training(ledger)
        self.assertEqual(report["status"], "TRAINING_COMPLETE")
        self.assertEqual(report["formal_owner_count"], 512)
        self.assertEqual(len(report["training_seed_ids"]), 512)
        scheduler_reservation.assert_called_once()
        finish_scheduler.assert_called_once()
        self.assertEqual([event for event in order if event[0] == "update"], [
            ("update", 0, None), ("update", 1, None),
            ("update", 2, None), ("update", 3, None),
        ])
        for block in range(4):
            start = block * 129
            block_events = order[start:start + 129]
            self.assertEqual(len([event for event in block_events if event[0] == "owner"]), 128)
            self.assertEqual(block_events[-1], ("update", block, None))

    def test_scheduler_reservations_preserve_the_accepted_full_cohort_forecast(self):
        runs = {
            "preflight": {
                "reservation": {"unit_id": "PREFLIGHT:7520001:P0", "role": "builder"},
                "finish": {"status": "succeeded", "measured_cpu_seconds": 8077.011297},
            },
        }
        with (
            mock.patch.object(executor, "_event_state", return_value=(runs, {})),
            mock.patch.object(executor.budget, "_charge", return_value=8077.011297),
        ):
            estimates, summary = executor._forecast_estimates(None)
        self.assertEqual(len(estimates), 999)
        self.assertEqual(summary["formal_unit_count"], 996)
        self.assertEqual(summary["scheduler_unit_count"], 2)
        self.assertEqual(estimates["SCHEDULER:TRAIN"], 120.0)
        self.assertEqual(estimates["SCHEDULER:EVALUATE"], 120.0)
        self.assertAlmostEqual(sum(estimates.values()), 161896.17019694686, places=6)
        self.assertAlmostEqual(summary["projected_total_builder_cpu_seconds"], 169973.18149394685, places=6)
        self.assertEqual(summary["builder_hard_cap_cpu_seconds"], 170000)
        self.assertEqual(summary["reviewer_protected_cpu_seconds"], 10000)
        self.assertEqual(summary["aggregate_hard_cap_cpu_seconds"], 180000)

    def test_heldout_phase_requires_a_successful_training_scheduler_receipt(self):
        ledger = FakeLedger(unit_kind="owner_arm")
        with (
            mock.patch.object(
                executor, "_existing_scheduler_receipt",
                side_effect=executor.budget.ResourceBlocked("training scheduler receipt missing"),
            ) as scheduler_receipt,
            mock.patch.object(executor, "_formal_forecast") as formal_forecast,
        ):
            with self.assertRaises(executor.budget.ResourceBlocked):
                executor._run_evaluation(ledger)
        scheduler_receipt.assert_called_once_with(ledger, "SCHEDULER:TRAIN")
        formal_forecast.assert_not_called()
        self.assertFalse((executor.ARTIFACT_ROOT / "evaluation-report.json").exists())

    def test_supervisor_validation_failures_still_account_controller_cpu(self):
        import contextlib
        import io

        stdout = io.StringIO()
        job_path = executor.ARTIFACT_ROOT / "invalid-supervisor-job.json"
        with (
            mock.patch.object(executor, "_supervisor_activity_lease", return_value=contextlib.nullcontext()),
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor, "_read_json", return_value={"schema_id": "wrong"}),
            mock.patch.object(executor.time, "process_time", side_effect=[5.0, 5.25]),
            contextlib.redirect_stdout(stdout),
        ):
            status = executor._supervisor_main(job_path)
        self.assertEqual(status, 2)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "SUPERVISOR_FAILED")
        self.assertEqual(report["measured_cpu_seconds"], 0.25)
        self.assertEqual(report["boundary"], "PermissionError")

    @unittest.skipIf(sys.platform == "win32", "the Stage 1 watchdog intentionally uses Linux /proc")
    def test_watchdog_accounts_for_nested_child_cpu(self):
        grandchild = "sum(i * i for i in range(8000000))"
        child = f"import subprocess,sys; subprocess.run([sys.executable,'-B','-c',{grandchild!r}],check=True)"
        temporary_paths = [executor.ARTIFACT_ROOT / name for name in ("tmp", "cache", "pycache")]
        for path in temporary_paths:
            path.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({
            "TMPDIR": str(temporary_paths[0]), "TMP": str(temporary_paths[0]),
            "TEMP": str(temporary_paths[0]), "XDG_CACHE_HOME": str(temporary_paths[1]),
            "PYTHONPYCACHEPREFIX": str(temporary_paths[2]),
        })
        output = executor.ARTIFACT_ROOT / "nested-watchdog.log"
        run = executor.budget.ProcessTreeWatchdog(poll_seconds=0.002).run(
            [sys.executable, "-B", "-c", child],
            reserved_cpu_seconds=5.0,
            cwd=STUDY,
            env=env,
            stdout_path=output,
        )
        self.assertEqual(run.returncode, 0)
        self.assertGreater(run.cpu_seconds, 0.05)
        self.assertGreaterEqual(run.controller_cpu_seconds, 0.0)

    @unittest.skipIf(sys.platform == "win32", "the Stage 1 ledger uses Linux file locks")
    def test_real_ledger_enforces_budget_and_exact_checkpoint_resume(self):
        ledger = self._test_ledger()
        first = "TRAIN:7500001-" + "a" * 16
        digest = "c" * 64
        self._reserve_test_unit(ledger, unit_id="TRAIN:7500001", run_id=first, cap=2.0, unit_kind="training_update")
        ledger.finish(
            run_id=first, measured_cpu_seconds=0.5, status="incomplete", checkpoint_sha256=digest,
        )
        with self.assertRaises(executor.budget.CheckpointMismatch):
            self._reserve_test_unit(
                ledger, unit_id="TRAIN:7500001", run_id="TRAIN:7500001-" + "b" * 16,
                cap=2.0, unit_kind="training_update", previous_run_id="wrong", checkpoint_sha256=digest,
            )
        resumed = "TRAIN:7500001-" + "b" * 16
        self._reserve_test_unit(
            ledger, unit_id="TRAIN:7500001", run_id=resumed, cap=2.0,
            unit_kind="training_update", previous_run_id=first, checkpoint_sha256=digest,
        )
        ledger.finish(run_id=resumed, measured_cpu_seconds=0.25, status="succeeded", result_sha256="d" * 64)
        with self.assertRaises(executor.budget.DuplicateRun):
            self._reserve_test_unit(ledger, unit_id="TRAIN:7500001", run_id="TRAIN:7500001-" + "d" * 16)

        capped = self._test_ledger("capped-ledger")
        self._reserve_test_unit(
            capped, unit_id="CAP:1", run_id="CAP:1-" + "e" * 16,
            cap=executor.budget.BUILDER_LIMIT,
        )
        with self.assertRaises(executor.budget.ResourceBlocked):
            self._reserve_test_unit(capped, unit_id="CAP:2", run_id="CAP:2-" + "f" * 16, cap=0.1)

    def test_native_runtime_uses_stage1_binding_without_stage0_gate(self):
        import preflight
        import runtime

        config = executor._read_json(STUDY / "config.json")
        native_path = STUDY / "runtime.py"
        fake_module = types.ModuleType("fake_native")
        fake_module.__file__ = str(native_path)
        env = {"identity": {"native_source_commit": config["source"]["sts_lightspeed_commit"]}}
        fake_runner = type("FakeRunner", (), {
            "_setup": lambda self, output_dir, *, native_module_path: (
                env, fake_module, object(), {"runtime": "test"},
            ),
        })()
        receipt = executor.budget.PackageBinding(
            package_commit="a" * 40,
            source_lock_sha256="b" * 64,
        )
        with tempfile.TemporaryDirectory(dir=STUDY) as temp_dir:
            with (
                mock.patch.object(executor.budget, "validate_worker_package_binding") as validate_receipt,
                mock.patch.object(runtime, "verify_source_repositories"),
                mock.patch.object(runtime, "verify_native_module", return_value=native_path),
                mock.patch.object(runtime, "binary_sha256", return_value=config["source"]["native_module_sha256"]),
                mock.patch.object(runtime, "load_issue73_runner", return_value=fake_runner),
                mock.patch.object(preflight, "reviewer_pass_proof", side_effect=AssertionError("Stage-0 gate used")),
            ):
                result = runtime.initialize_native_runtime(
                    Path(temp_dir) / "runtime", sampling=True, stage1_binding=receipt,
                )
        validate_receipt.assert_called_once_with(receipt)
        self.assertEqual(result[0], config)

    def test_worker_accepts_only_the_exact_registered_owner_row(self):
        row = next(item for item in executor._rows() if item["phase"] == "preflight")
        unit_id = row["unit_id"]
        run_id = unit_id + "-" + "d" * 16
        ledger = FakeLedger(unit_kind="owner_arm")
        unit_directory = executor._unit_directory(unit_id)
        job_path = unit_directory / f"worker-job-{run_id}.json"
        job_path.parent.mkdir(parents=True)
        job = {
            "schema_id": executor.WORKER_SCHEMA,
            "package_commit": ledger.binding.package_commit,
            "source_lock_sha256": ledger.binding.source_lock_sha256,
            "run_id": run_id,
            "unit_id": unit_id,
            "phase": "preflight",
            "row": row,
            "output_dir": str(unit_directory / "native"),
            "result_path": str(unit_directory / "result.json"),
            "previous_run_id": None,
            "checkpoint_sha256": None,
        }
        job_path.write_text(json.dumps(job), encoding="utf-8")
        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "open_reserved_stage1_worker_ledger", return_value=ledger),
            mock.patch.object(executor, "_worker_owner", return_value={"status": "SUCCEEDED", "unit_id": unit_id}) as worker,
        ):
            self.assertEqual(executor._worker_main(job_path), 0)
            worker.assert_called_once()
        self.assertTrue((unit_directory / "result.json").is_file())

        job["row"] = {**row, "owner_seed": int(row["owner_seed"]) + 1}
        job_path.write_text(json.dumps(job), encoding="utf-8")
        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "open_reserved_stage1_worker_ledger", return_value=ledger),
            mock.patch.object(executor, "_worker_owner") as worker,
        ):
            with self.assertRaises(PermissionError):
                executor._worker_main(job_path)
            worker.assert_not_called()

    def test_training_batch_dispatches_canonical_phase_and_run_scoped_paths(self):
        row = next(item for item in executor._unit_rows("train") if item["block_index"] == 0)
        binding = types.SimpleNamespace(package_commit="a" * 40, source_lock_sha256="b" * 64)
        ledger = mock.Mock()
        ledger.binding = binding
        reservations = {}

        def reserve_many(specs):
            result = []
            for spec in specs:
                event = {
                    **spec,
                    "event": "reserve",
                    "schema_id": "issue75-stage1-budget-event-v2",
                    "role": "builder",
                    "unit_kind": "owner_arm",
                    "retry_reason": None,
                    "pre_game_start_proof": None,
                    "pre_game_start_proof_sha256": None,
                    "package_commit": binding.package_commit,
                    "source_lock_sha256": binding.source_lock_sha256,
                }
                reservations[event["run_id"]] = event
                result.append(event)
            return result

        ledger.reserve_many.side_effect = reserve_many
        ledger.require_open_reservation.side_effect = lambda *, run_id, unit_id: reservations[run_id]
        captured = []

        def validate_then_stop(items):
            for item in items:
                raw_job = executor._read_json(item["job_path"])
                try:
                    job, child_ledger, reservation = executor._validate_worker_job(item["job_path"])
                except Exception as exc:
                    raise AssertionError(f"job validation failed for {raw_job!r}: {exc}") from exc
                self.assertIs(child_ledger, ledger)
                self.assertEqual(reservation, reservations[item["run_id"]])
                captured.append(job)
            return {
                item["run_id"]: {
                    "status": "SUPERVISOR_FAILED", "measured_cpu_seconds": 0.1,
                    "boundary": "focused_test_stop_before_native_game",
                }
                for item in items
            }

        with (
            mock.patch.object(executor, "_existing_unit_receipt", return_value=None),
            mock.patch.object(executor, "_existing_unit", return_value=(None, None, None)),
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "open_reserved_stage1_worker_ledger", return_value=ledger),
            mock.patch.object(executor, "_finish_failed_unit"),
            mock.patch.object(executor, "_supervise_batch", side_effect=validate_then_stop),
        ):
            with self.assertRaises(executor.budget.ResourceBlocked):
                executor._run_owner_batch(
                    ledger, [row], phase="train", estimates={row["unit_id"]: 2.0},
                )
        self.assertEqual(len(captured), 1)
        job = captured[0]
        self.assertEqual(job["phase"], "training")
        self.assertEqual(job["row"], row)
        run_directory = executor._run_artifact_directory(row["unit_id"], job["run_id"])
        self.assertEqual(Path(job["output_dir"]), run_directory / "native")
        self.assertEqual(Path(job["result_path"]), run_directory / "result.json")
        self.assertFalse((run_directory / "native").exists())

    def test_formal_worker_phase_is_canonical_and_matches_registered_row(self):
        row = next(item for item in executor._unit_rows("train") if item["block_index"] == 0)
        self.assertEqual(executor._worker_phase("train", row), "training")
        unit_id = row["unit_id"]
        run_id = unit_id + "-" + "f" * 16
        ledger = FakeLedger(unit_kind="owner_arm")
        unit_directory = executor._unit_directory(unit_id)
        run_directory = executor._run_artifact_directory(unit_id, run_id)
        job_path = unit_directory / f"worker-job-{run_id}.json"
        job_path.parent.mkdir(parents=True, exist_ok=True)
        job = {
            "schema_id": executor.WORKER_SCHEMA,
            "package_commit": ledger.binding.package_commit,
            "source_lock_sha256": ledger.binding.source_lock_sha256,
            "run_id": run_id,
            "unit_id": unit_id,
            "phase": "training",
            "row": row,
            "output_dir": str(run_directory / "native"),
            "result_path": str(run_directory / "result.json"),
            "previous_run_id": None,
            "checkpoint_sha256": None,
        }
        job_path.write_text(json.dumps(job), encoding="utf-8")
        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "open_reserved_stage1_worker_ledger", return_value=ledger),
        ):
            self.assertEqual(executor._validate_worker_job(job_path)[0]["phase"], "training")
            job["phase"] = "train"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            with self.assertRaises(PermissionError):
                executor._validate_worker_job(job_path)
        with self.assertRaises(PermissionError):
            executor._worker_phase("evaluate", row)

    def test_update_worker_is_bound_to_its_exact_block_and_result_path(self):
        unit_id = "UPDATE:0"
        run_id = unit_id + "-" + "e" * 16
        ledger = FakeLedger(unit_kind="training_update")
        unit_directory = executor._unit_directory(unit_id)
        job_path = unit_directory / f"worker-job-{run_id}.json"
        job_path.parent.mkdir(parents=True)
        result_path = executor._inside_artifacts(
            executor.ARTIFACT_ROOT / "units" / "UPDATE-0" / f"result-{run_id}.json"
        )
        job = {
            "schema_id": executor.WORKER_SCHEMA,
            "package_commit": ledger.binding.package_commit,
            "source_lock_sha256": ledger.binding.source_lock_sha256,
            "run_id": run_id,
            "unit_id": unit_id,
            "phase": "update",
            "block_index": 0,
            "previous_run_id": None,
            "checkpoint_sha256": None,
            "result_path": str(result_path),
        }
        job_path.write_text(json.dumps(job), encoding="utf-8")
        def fake_worker_update(worker_job, _ledger):
            result = {"status": "SUCCEEDED", "unit_id": unit_id}
            executor._write_once(Path(worker_job["result_path"]), result)
            return result

        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "verify_stage1_phase"),
            mock.patch.object(executor.budget, "open_reserved_stage1_worker_ledger", return_value=ledger),
            mock.patch.object(executor, "_worker_update", side_effect=fake_worker_update) as worker,
        ):
            self.assertEqual(executor._worker_main(job_path), 0)
            worker.assert_called_once()
        self.assertTrue(result_path.is_file())

        job["block_index"] = 1
        job_path.write_text(json.dumps(job), encoding="utf-8")
        with (
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "verify_stage1_phase"),
            mock.patch.object(executor.budget, "open_reserved_stage1_worker_ledger", return_value=ledger),
            mock.patch.object(executor, "_worker_update") as worker,
        ):
            with self.assertRaises(PermissionError):
                executor._worker_main(job_path)
            worker.assert_not_called()


    def test_update_worker_resumes_from_its_open_checkpoint_reservation(self):
        ledger = self._test_ledger("update-ledger")
        unit_id = "UPDATE:0"
        prior_run_id = unit_id + "-" + "a" * 16
        self._reserve_test_unit(
            ledger, unit_id=unit_id, run_id=prior_run_id, cap=2.0,
            unit_kind="training_update",
        )
        checkpoint_path = executor.ARTIFACT_ROOT / "models" / "block-0-step.pt"
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_bytes(b"prior optimizer checkpoint")
        prior_checkpoint_sha256 = executor._file_sha256(checkpoint_path)
        ledger.finish(
            run_id=prior_run_id, measured_cpu_seconds=0.5, status="incomplete",
            checkpoint_sha256=prior_checkpoint_sha256,
        )

        config = executor._read_json(executor.STAGE1 / "config.json")
        block_seed = int(config["seeds"]["block_order_seeds"][0])
        source_digest = "3" * 64
        resume_state = {
            "schema_id": executor.RESUME_SCHEMA,
            "block_index": 0,
            "completed_steps": 17,
            "source_model_digest": source_digest,
        }
        torch_stub = types.ModuleType("torch")
        torch_stub.set_num_threads = lambda _count: None
        torch_stub.get_num_interop_threads = lambda: 1
        torch_stub.set_num_interop_threads = lambda _count: None
        torch_load_calls = []

        def torch_load(path, **_kwargs):
            torch_load_calls.append(Path(path))
            return dict(resume_state)

        torch_stub.load = torch_load
        torch_stub.save = lambda _value, stream: stream.write(b"mock optimizer state")

        class FakePolicy:
            def state_dict(self):
                return {"weight": 1.0}

        class FakeOptimizer:
            def state_dict(self):
                return {"step": 17}

        policy = FakePolicy()
        learning_stub = types.ModuleType("learning")
        train_calls = []

        def train_block(_frozen, examples, *, block_order_seed, step_callback, resume_state):
            train_calls.append({
                "owner_count": len(examples),
                "block_order_seed": block_order_seed,
                "resume_state": resume_state,
            })
            diagnostics = [types.SimpleNamespace(step=index) for index in range(64)]
            step_callback(64, policy, FakeOptimizer(), {"rng": "mock"}, list(range(128)), diagnostics)
            return types.SimpleNamespace(
                model=policy,
                diagnostics=diagnostics,
                block_order_seed=block_order_seed,
                owner_order_sha256="e" * 64,
                source_model_digest=source_digest,
                result_model_digest="d" * 40,
            )

        learning_stub.train_policy_iteration_block = train_block
        learning_stub.policy_probe_diagnostics = lambda *_args: {}
        model_stub = types.ModuleType("model")
        model_stub.model_digest = lambda _model: "d" * 40
        prepare_stub = types.ModuleType("prepare")
        prepare_stub.derive_seed = lambda *_args: block_seed
        owner_rows = [
            row for row in executor._rows()
            if row["phase"] == "training" and int(row["block_index"]) == 0
        ]
        examples = [types.SimpleNamespace(owner_seed=int(row["owner_seed"])) for row in owner_rows]

        def save_model_once(model, path, digest_fn):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"mock policy model")
            return {
                "path": path.relative_to(executor.ARTIFACT_ROOT).as_posix(),
                "sha256": executor._file_sha256(path),
                "model_digest": digest_fn(model),
            }

        def run_real_worker(specs):
            self.assertEqual(len(specs), 1)
            spec = specs[0]
            job_path = Path(spec["job_path"])
            job = executor._read_json(job_path)
            self.assertEqual(job["previous_run_id"], prior_run_id)
            self.assertEqual(job["checkpoint_sha256"], prior_checkpoint_sha256)
            self.assertEqual(executor._worker_main(job_path), 0)
            return {
                spec["run_id"]: {
                    "status": "SUCCEEDED",
                    "measured_cpu_seconds": 0.25,
                    "boundary": "complete",
                }
            }

        with (
            mock.patch.dict(sys.modules, {
                "torch": torch_stub, "learning": learning_stub,
                "model": model_stub, "prepare": prepare_stub,
            }),
            mock.patch.object(executor.budget, "verify_stage1_package"),
            mock.patch.object(executor.budget, "verify_stage1_phase"),
            mock.patch.object(executor.budget, "open_reserved_stage1_worker_ledger", return_value=ledger),
            mock.patch.object(executor, "_load_training_examples", return_value=examples),
            mock.patch.object(executor, "_load_model", return_value=(policy, source_digest, {})),
            mock.patch.object(executor, "_fixed_probe_features", return_value=([], "f" * 64)),
            mock.patch.object(executor, "_save_model_once", side_effect=save_model_once),
            mock.patch.object(executor, "_expected_source_model", return_value=source_digest),
            mock.patch.object(executor, "_supervise_batch", side_effect=run_real_worker),
        ):
            result = executor._run_update_unit(ledger, 0, estimates={unit_id: 2.0})
            retained = executor._existing_unit_receipt(ledger, unit_id)

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["unit_id"], unit_id)
        self.assertEqual(retained, result)
        self.assertEqual(torch_load_calls, [checkpoint_path])
        self.assertEqual(len(train_calls), 1)
        self.assertEqual(train_calls[0]["owner_count"], 128)
        self.assertEqual(train_calls[0]["block_order_seed"], block_seed)
        self.assertEqual(train_calls[0]["resume_state"], resume_state)
        runs, units = executor._event_state(ledger)
        latest_run_id = units[unit_id][-1]
        latest_run = runs[latest_run_id]
        self.assertEqual(latest_run["reservation"]["previous_run_id"], prior_run_id)
        self.assertEqual(latest_run["reservation"]["checkpoint_sha256"], prior_checkpoint_sha256)
        self.assertEqual(latest_run["finish"]["status"], "succeeded")
        self.assertTrue((executor._unit_directory(unit_id) / f"result-{latest_run_id}.json").is_file())
        self.assertFalse((executor._unit_directory(unit_id) / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
