from __future__ import annotations

import json
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import prospective_cost_regime as regime
import run_cost_bridge


class ProspectiveCostRegimeTests(unittest.TestCase):
    def test_bridge_population_is_exact(self) -> None:
        regime.validate_bridge_population(range(660070, 660129))
        for wrong in (range(660069, 660129), range(660070, 660128)):
            with self.assertRaises(regime.CostRegimeError):
                regime.validate_bridge_population(wrong)

    def test_only_provenanced_formal_runs_enter_sample_and_ledger_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            study = repo / "studies" / "issue-60-onpolicy-joint-choice"
            study.mkdir(parents=True)
            for name in regime.PRODUCER_FILES:
                (study / name).write_text(f"producer file {name}\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)

            ledger = study / "evidence" / "cpu-ledger.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text('{"process_cpu_seconds":517.670394}\n', encoding="utf-8")
            before = ledger.read_bytes()
            old = study / "evidence" / "runs" / "training" / "block1" / "batch-01" / "seed-660001" / "attempt-0001"
            old.mkdir(parents=True)
            (old / "run.json").write_text(json.dumps({
                "status": "COMPLETE", "stage_key": regime.BRIDGE_STAGE,
                "game_seed": 660001, "arm": "residual", "process_cpu_seconds": 70.0,
            }), encoding="utf-8")
            diagnostic = study / "evidence" / "validation" / "replays" / "seed-660070" / "attempt-0001"
            diagnostic.mkdir(parents=True)
            (diagnostic / "run.json").write_text("{}", encoding="utf-8")

            fresh = study / "evidence" / "runs" / "training" / "block1" / "batch-01" / "seed-660070" / "attempt-0001"
            fresh.mkdir(parents=True)
            run = {
                "status": "COMPLETE", "stage_key": regime.BRIDGE_STAGE,
                "game_seed": 660070, "arm": "residual", "process_cpu_seconds": 12.5,
            }
            (fresh / "run.json").write_text(json.dumps(run), encoding="utf-8")
            later = study / "evidence" / "runs" / "training" / "block1" / "batch-01" / "seed-660071" / "attempt-0001"
            later.mkdir(parents=True)
            (later / "run.json").write_text(json.dumps({
                **run, "game_seed": 660071, "process_cpu_seconds": 10.0,
            }), encoding="utf-8")
            manifest = regime.producer_manifest(study)
            head = regime.git(study, "rev-parse", "HEAD")
            regime.write_cost_tag(study, later.relative_to(study).as_posix(),
                                  producer_commit=head, manifest=manifest)
            fresh_tag = regime.write_cost_tag(study, fresh.relative_to(study).as_posix(),
                                              producer_commit=head, manifest=manifest)
            fresh_tag_path = (study / "evidence" / "prospective-cost" / "tags"
                              / (hashlib.sha256(fresh_tag["attempt_path"].encode("utf-8")).hexdigest() + ".json"))
            fresh_tag_before = fresh_tag_path.read_bytes()
            regime.write_cost_tag(study, fresh.relative_to(study).as_posix(),
                                  producer_commit=head, manifest=manifest)
            self.assertEqual(fresh_tag_path.read_bytes(), fresh_tag_before)

            self.assertEqual(regime.prospective_game_costs(
                study, current_manifest=manifest, bridge_only=True,
            ), [12.5, 10.0])
            self.assertEqual(ledger.read_bytes(), before)

    def test_cost_index_refresh_uses_declared_order_and_exact_bridge_population(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observations = [
                {"stage_key": "block1:batch-02", "game_seed": 660130,
                 "arm": "residual", "process_cpu_seconds": 20.0},
                {"stage_key": "block1:batch-01", "game_seed": 660070,
                 "arm": "residual", "process_cpu_seconds": 10.0},
            ]
            with patch.multiple(
                run_cost_bridge,
                HERE=root,
                EVIDENCE=root / "evidence",
                PROSPECTIVE=root / "evidence" / "prospective-cost",
                FORECAST_INDEX=root / "evidence" / "prospective-cost" / "cost-index.json",
                PRODUCER_COMMIT="producer",
                PRODUCER_HASHES={},
                _COST_TAG_ROWS=[dict(row) for row in observations],
                _COST_CACHE_INDEX_STAMP=None,
                _COST_CACHE_VALUES=[],
            ):
                run_cost_bridge._write_index()
                self.assertEqual(run_cost_bridge._cost_sample(), [10.0, 20.0])
                bridge_rows = [
                    {"stage_key": regime.BRIDGE_STAGE, "game_seed": seed,
                     "arm": "residual", "process_cpu_seconds": float(seed - 660000)}
                    for seed in regime.BRIDGE_SEEDS
                ]
                run_cost_bridge._COST_TAG_ROWS = bridge_rows
                run_cost_bridge._write_index()
                sample = run_cost_bridge._cost_sample(bridge_only=True)
                self.assertEqual(len(sample), 59)
                self.assertEqual(sample[0], 70.0)
                self.assertEqual(sample[-1], 128.0)

    def test_caps_reserve_inflight_and_finalization_cpu(self) -> None:
        regime.enforce_actual_caps(
            actual_cpu=1000.0, in_flight_workers=2, max_game_cpu=20.0,
            bridge_baseline_cpu=900.0, bridge_spent_cpu=100.0,
        )
        cases = (
            {"actual_cpu": 179950.0, "in_flight_workers": 1, "max_game_cpu": 30.0},
            {"actual_cpu": 1000.0, "in_flight_workers": 1, "max_game_cpu": 20.0,
             "bridge_baseline_cpu": 0.0, "bridge_spent_cpu": 4980.0},
        )
        for kwargs in cases:
            with self.assertRaises(regime.CostRegimeError):
                regime.enforce_actual_caps(**kwargs)


if __name__ == "__main__":
    unittest.main()
