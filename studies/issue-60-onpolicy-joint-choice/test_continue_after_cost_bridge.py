from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import continue_after_cost_bridge as continuation
import parallel_execution


class ContinuationCostTaggingTests(unittest.TestCase):
    def test_dispatcher_cost_hook_is_installed_before_continuation(self) -> None:
        calls: list[str] = []

        def install_hook() -> None:
            calls.append("install")

        def continue_program() -> int:
            calls.append("continue")
            return 75

        with patch.object(
            continuation.bridge, "_install_tagged_dispatcher", install_hook,
        ), patch.object(
            continuation.bridge, "_run_continue", continue_program,
        ):
            result = continuation._run_continue_with_cost_tagging()

        self.assertEqual(result, 75)
        self.assertEqual(calls, ["install", "continue"])

    def test_cache_installs_after_original_runtime_hooks_and_only_once(self) -> None:
        calls: list[str] = []
        events: list[str] = []

        def original() -> str:
            calls.append("r2")
            return "r2-result"

        def install_cache() -> None:
            calls.append("cache")

        wrapped = continuation._wrap_runtime_hooks_with_cache(
            original, install_cache, events,
        )

        self.assertEqual(wrapped(), "r2-result")
        self.assertEqual(wrapped(), "r2-result")
        self.assertEqual(calls, ["r2", "cache", "r2"])
        self.assertEqual(events, ["original-r2-runtime-hooks", "cached-guard-installed"])

    def test_diagnostic_replay_paths_are_not_tagged_as_formal_costs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            study = Path(temporary)
            attempts: list[Path] = []
            formal = study / "evidence" / "runs" / "training" / "batch-02" / "attempt-0001"
            diagnostic = study / "evidence" / "validation" / "replays" / "attempt-0001"
            wrapped = continuation._tag_formal_attempts_only(
                lambda path: attempts.append(path) or True, study,
            )

            self.assertFalse(wrapped(diagnostic))
            self.assertTrue(wrapped(formal))
            self.assertEqual(attempts, [formal.resolve()])

    def test_recorded_recovery_proof_survives_later_resource_blocked_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            study = Path(temporary)
            stage = study / "evidence" / "validation" / "parallel-execution" / "stages" / "train-pre-dispatch-failure-0001.json"
            status = study / "evidence" / "stage-status" / "train-pre-dispatch-failure-0001.json"
            stage.parent.mkdir(parents=True)
            status.parent.mkdir(parents=True)
            stage.write_text("{\"execution_count\":0,\"status\":\"FAILED\",\"worker_process_cpu_seconds\":0.0}\n", encoding="utf-8")
            status.write_text("{\"status\":\"FAILED\"}\n", encoding="utf-8")
            recovery = {
                "failure": "pre-dispatch runtime checkout failure",
                "parallel_stage_status": "FAILED",
                "parallel_execution_count": 0,
                "worker_process_cpu_seconds": 0.0,
                "archived_attempt_records": [
                    {"path": stage.relative_to(study).as_posix(),
                     "sha256": continuation._sha256(stage)},
                    {"path": status.relative_to(study).as_posix(),
                     "sha256": continuation._sha256(status)},
                ],
                "no_game_or_update_dispatched": True,
            }
            rows = [
                {"state": "STARTED"},
                {"state": "FAILED", "failure": recovery["failure"]},
                {"state": "RETRY_AUTHORIZED", "recovery_basis": recovery},
                {"state": "STARTED", "attempt_number": 2},
                {"state": "EXITED", "attempt_number": 2, "exit_code": 75},
            ]
            lifecycle = study / "evidence" / "prospective-cost" / "continuation-lifecycle.jsonl"
            lifecycle.parent.mkdir(parents=True)
            lifecycle.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            with patch.multiple(continuation, HERE=study, LIFECYCLE=lifecycle):
                continuation._verify_recorded_recovery_basis(recovery)
                recovery["archived_attempt_records"][0]["sha256"] = "0" * 64
                with self.assertRaises(continuation.bridge.CostRegimeError):
                    continuation._verify_recorded_recovery_basis(recovery)

    def test_continuation_completion_refreshes_only_formal_cost_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            study = repo / "studies" / "issue-60-onpolicy-joint-choice"
            study.mkdir(parents=True)
            for name in continuation.bridge.producer_manifest(continuation.HERE):
                target = study / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(continuation.HERE / name, target)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
            commit = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            manifest = continuation.bridge.producer_manifest(study)

            formal = study / "evidence" / "runs" / "training" / "block1" / "batch-02" / "seed-660129" / "attempt-0001"
            diagnostic = study / "evidence" / "validation" / "continuation-cache-guard" / "replays" / "seed-660239" / "attempt-0001"
            formal.mkdir(parents=True)
            diagnostic.mkdir(parents=True)
            formal_run = {
                "status": "COMPLETE", "stage_key": "block1:batch-02",
                "game_seed": 660129, "arm": "residual", "process_cpu_seconds": 12.5,
            }
            diagnostic_run = {
                "status": "COMPLETE", "stage_key": "block1:batch-02",
                "game_seed": 660239, "arm": "residual", "process_cpu_seconds": 13.0,
            }
            (formal / "run.json").write_text(json.dumps(formal_run), encoding="utf-8")
            (diagnostic / "run.json").write_text(json.dumps(diagnostic_run), encoding="utf-8")
            formal_relative = formal.relative_to(study).as_posix()
            diagnostic_relative = diagnostic.relative_to(study).as_posix()

            class CacheWitness:
                def __init__(self) -> None:
                    self.synced = None

                def sync_dispatch_results(self, results):
                    self.synced = results
                    return 0

            cache = CacheWitness()
            original_latest = parallel_execution._latest_run_summary

            def run_jobs(_dispatcher, _tasks):
                _dispatcher.executions.extend([
                    {"exit_code": 0, "attempt": formal_relative,
                     "task_key": ["block1:batch-02", 660129, "residual"]},
                    {"exit_code": 0, "attempt": diagnostic_relative,
                     "task_key": ["block1:batch-02", 660239, "residual"]},
                ])
                _dispatcher.results = {
                    ("block1:batch-02", 660129, "residual"): (formal, formal_run),
                    ("block1:batch-02", 660239, "residual"): (diagnostic, diagnostic_run),
                }
                return _dispatcher.results

            index = study / "evidence" / "prospective-cost" / "cost-index.json"
            with patch.object(parallel_execution.ParallelGameDispatcher, "run_jobs", run_jobs), \
                    patch.object(parallel_execution, "_latest_run_summary", original_latest), \
                    patch.multiple(
                        continuation.bridge,
                        HERE=study,
                        EVIDENCE=study / "evidence",
                        PROSPECTIVE=study / "evidence" / "prospective-cost",
                        FORECAST_INDEX=index,
                        PRODUCER_COMMIT=commit,
                        PRODUCER_HASHES=manifest,
                        _COST_TAG_ROWS=None,
                        _COST_CACHE_INDEX_STAMP=None,
                        _COST_CACHE_VALUES=[],
                        CACHED_STATE=cache,
                        TAG_FAILURE=study / "evidence" / "prospective-cost" / "tag-failure.json",
                    ):
                continuation.bridge._install_tagged_dispatcher()
                dispatcher = object.__new__(parallel_execution.ParallelGameDispatcher)
                dispatcher.executions = []
                dispatcher.results = {}
                dispatcher.run_jobs([])
                sample = continuation.bridge._cost_sample()
                index_payload = json.loads(index.read_text(encoding="utf-8"))

            self.assertEqual(sample, [12.5])
            self.assertEqual(index_payload["tag_count"], 1)
            self.assertEqual(index_payload["cost_observations"][0]["game_seed"], 660129)
            self.assertEqual(cache.synced, dispatcher.results)
            self.assertFalse((study / "evidence" / "prospective-cost" / "tags" /
                              f"{hashlib.sha256(diagnostic_relative.encode()).hexdigest()}.json").exists())


if __name__ == "__main__":
    unittest.main()
