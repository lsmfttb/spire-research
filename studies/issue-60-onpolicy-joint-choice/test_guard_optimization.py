from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import resource_amendment_r2 as resource
from guard_optimization import GuardState, install_cached_guard

def _resource_view():
    return SimpleNamespace(
        _ledger_total=resource._ledger_total,
        _read_json=resource._read_json,
        ResourceAmendmentErrorR2=resource.ResourceAmendmentErrorR2,
        _declared_order=resource._declared_order,
        OPTIMIZER_COST_MULTIPLIER=resource.OPTIMIZER_COST_MULTIPLIER,
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class GuardOptimizationTests(unittest.TestCase):
    def test_startup_cache_matches_existing_r2_views(self) -> None:
        root = Path(__file__).resolve().parent
        state = GuardState(root, _resource_view())
        self.assertEqual(state.complete_game_costs(root), resource.complete_game_costs(root))
        self.assertEqual(state.ledger_total(root), resource._ledger_total(root))
        self.assertEqual(state.r2_optimizer_projection(root), resource.remaining_optimizer_projection(root))
        self.assertEqual(state.completed_run_count(), self._completed_run_count_from_files(root))

    @staticmethod
    def _completed_run_count_from_files(root: Path) -> int:
        keys = set()
        for stage in ("training", "evaluation"):
            for path in (root / "evidence" / "runs" / stage).rglob("run.json"):
                try:
                    row = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if row.get("status") == "COMPLETE":
                    keys.add((row.get("stage_key"), row.get("game_seed"), row.get("arm")))
        return len(keys)

    def test_completion_batch_and_ledger_events_update_authoritative_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_json(root / "evidence" / "preflight" / "native.json", {
                "optimizer_projection": {
                    "measured_full_256_row_minibatch_cpu_seconds": 0.25,
                },
            })
            resource_view = _resource_view()
            state = GuardState(root, resource_view)
            attempt = root / "evidence" / "runs" / "training" / "block1" / "batch-01" / "seed-660001" / "attempt-0001"
            summary = {
                "status": "COMPLETE", "stage_key": "block1:batch-01",
                "game_seed": 660001, "arm": "residual", "process_cpu_seconds": 12.5,
                "supported_choices": 5,
            }
            _write_json(attempt / "run.json", summary)
            self.assertTrue(state.register_complete(attempt, summary))
            self.assertFalse(state.register_complete(attempt, summary))
            self.assertEqual(state.completed_run_count(), 1)
            self.assertEqual(state.max_observed_game_cpu_seconds, 12.5)
            self.assertEqual(state.complete_game_costs(root), resource.complete_game_costs(root))

            batch_path = root / "evidence" / "training" / "block1" / "batch-01.json"
            _write_json(batch_path, {"status": "COMPLETE", "batch": 1})
            self.assertTrue(state.register_batch_json(batch_path))
            self.assertFalse(state.register_batch_json(batch_path))
            self.assertEqual(state.r2_optimizer_projection(root), resource.remaining_optimizer_projection(root))
            self.assertEqual(state.completed_run_count(), 1)

            def append_file(row):
                path = root / resource.CPU_LEDGER
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, sort_keys=True) + "\n")

            hooked_attempt = root / "evidence" / "runs" / "training" / "block1" / "batch-01" / "seed-660003" / "attempt-0001"
            hooked_summary = {**summary, "game_seed": 660003, "process_cpu_seconds": 7.0}

            def complete_game(**_kwargs):
                _write_json(hooked_attempt / "run.json", hooked_summary)
                return hooked_attempt, hooked_summary

            run_study = SimpleNamespace(
                HERE=root,
                atomic_json=lambda path, value: _write_json(Path(path), value),
                run_game=complete_game,
                _append_ledger=append_file,
                _completed_run_count=lambda: -1,
                _remaining_optimizer_projection=lambda: -1.0,
                _max_observed_game_seconds=lambda: -1.0,
            )
            r2 = SimpleNamespace(
                _read_json=lambda path: json.loads(Path(path).read_text(encoding="utf-8")),
                _append_ledger=append_file,
            )

            class FakeTracker:
                def reap_worker(self, pid, seconds):
                    return 3

            class FakeDispatcher:
                def __init__(self):
                    self.results = {}
                    self.observed_completed = None

                def _budget_guard(self):
                    self.observed_completed = run_study._completed_run_count()

            parallel = SimpleNamespace(
                ProcessCpuTracker=FakeTracker,
                ParallelGameDispatcher=FakeDispatcher,
            )
            install_cached_guard(run_study, r2, resource_view, state, parallel)
            second_batch = root / "evidence" / "training" / "block1" / "batch-02.json"
            run_study.atomic_json(second_batch, {"status": "COMPLETE", "batch": 2})
            self.assertEqual(len(state._completed_batch_paths), 2)
            self.assertEqual(state.r2_optimizer_projection(root), resource.remaining_optimizer_projection(root))
            run_study.run_game(seed=660003)
            self.assertEqual(state.completed_run_count(), 2)
            self.assertEqual(state.max_observed_game_cpu_seconds, 12.5)
            row = {"stage": "event-test", "process_cpu_seconds": 3.25}
            run_study._append_ledger(row)
            self.assertEqual(state.ledger_total(root), resource._ledger_total(root))
            self.assertAlmostEqual(state.ledger_total(root), 3.25)

            settled = FakeTracker().reap_worker(123, 1.75)
            self.assertEqual(settled, 3)
            self.assertAlmostEqual(state.settled_worker_cpu_seconds, 1.75)
            self.assertEqual(state.worker_settlements[-1], {"pid": 123, "cpu_seconds": 1.75, "slot": 3})

            second_attempt = root / "evidence" / "runs" / "training" / "block1" / "batch-01" / "seed-660002" / "attempt-0001"
            second_summary = {**summary, "game_seed": 660002, "process_cpu_seconds": 9.0}
            _write_json(second_attempt / "run.json", second_summary)
            dispatcher = FakeDispatcher()
            dispatcher.results[("block1:batch-01", 660002, "residual")] = (second_attempt, second_summary)
            dispatcher._budget_guard()
            self.assertEqual(dispatcher.observed_completed, 3)
            self.assertEqual(state.completed_run_count(), 3)


if __name__ == "__main__":
    unittest.main()
