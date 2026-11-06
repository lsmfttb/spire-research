from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import formal_analysis
import formal_runner


class StubBackend:
    @staticmethod
    def _row(owner: int, *, phase: str, arm: str, formal: bool, block: int | None):
        roots = 4 if phase == "training" else 0
        return {
            "unit_id": f"TRAIN:{owner}" if phase == "training" else f"EVAL:{owner}:{arm}",
            "phase": phase,
            "arm": arm,
            "formal": formal,
            "block_index": block,
            "owner_seed": owner,
            "game_seed": owner,
            "paired_owner_key": f"{phase}-{owner}",
            "policy_rng_seed": None,
            "search_invocation_seeds": [owner + index for index in range(roots)],
        }


def test_formal_plan_uses_only_registered_disjoint_cohorts() -> None:
    plan = formal_runner.formal_plan()

    assert plan["formal_games"] == 7_808
    assert plan["training_games"] == 5_120
    assert plan["validation_games"] == 448
    assert plan["test_games"] == 2_240
    assert plan["training_blocks_per_arm"] == 8
    assert plan["formal_games_started"] == 0
    assert plan["formal_execution_command_available"] is False
    assert len(plan["training_stage_schedule"]) == 80
    assert all(
        plan["training_stage_schedule"][index]["stage"] == "training_games"
        and plan["training_stage_schedule"][index + 1]["stage"] == "actor_update"
        and plan["training_stage_schedule"][index + 1]["depends_on"]
        == plan["training_stage_schedule"][index]["stage_id"]
        for index in range(0, 80, 2)
    )
    assert plan["owner_ranges"] == {
        "training": [8_400_001, 8_401_024],
        "validation": [8_510_001, 8_510_064],
        "test": [8_520_001, 8_520_320],
    }
    training_ids = set(range(8_400_001, 8_401_025))
    validation_ids = set(range(8_510_001, 8_510_065))
    test_ids = set(range(8_520_001, 8_520_321))
    pilot_ids = set(range(8_530_001, 8_530_017))
    assert not training_ids & validation_ids
    assert not training_ids & test_ids
    assert not validation_ids & test_ids
    assert not test_ids & pilot_ids


def test_formal_seed_table_is_exact_arm_specific_and_reproducible() -> None:
    identity = {"package_commit": "a" * 40, "source_lock_sha256": "b" * 64}
    table = formal_runner.build_formal_seed_table(StubBackend(), identity)
    formal_runner.validate_formal_seed_table(table, identity=identity)

    assert table["row_count"] == 7_808
    assert table["expected_games_by_phase"] == {
        "training": 5_120,
        "validation": 448,
        "test": 2_240,
    }
    rows = table["rows"]
    assert all(row["formal"] is True for row in rows)
    assert all(row["issue85_phase"] != "pilot" for row in rows)
    training = [row for row in rows if row["issue85_phase"] == "training"]
    assert {row["owner_seed"] for row in training} == set(range(8_400_001, 8_401_025))
    by_owner = {}
    for row in training:
        by_owner.setdefault(row["owner_seed"], []).append(row)
    assert all({row["issue85_arm"] for row in arm_rows} == set("BCDEF") for arm_rows in by_owner.values())
    assert all(len({row["policy_rng_seed"] for row in arm_rows}) == 5 for arm_rows in by_owner.values())
    assert all(len(row["search_invocation_seeds"]) == 4 for row in training)
    assert all(len(set(row["search_invocation_seeds"])) == 4 for row in training)
    assert table == formal_runner.build_formal_seed_table(StubBackend(), identity)


def test_formal_seed_table_rejects_relabelled_pilot_seed_or_mutated_row() -> None:
    identity = {"package_commit": "c" * 40, "source_lock_sha256": "d" * 64}
    table = formal_runner.build_formal_seed_table(StubBackend(), identity)
    table["rows"][0]["policy_rng_seed"] += 1
    table["rows_sha256"] = formal_runner.sha256_bytes(formal_runner.canonical(table["rows"]))

    with pytest.raises(ValueError, match="not reproducible"):
        formal_runner.validate_formal_seed_table(table, identity=identity)


def test_holm_stepdown_adjusts_four_confirmatory_p_values() -> None:
    adjusted = formal_analysis.holm_adjust({
        "B-A": 0.01,
        "C-A": 0.04,
        "E-A": 0.03,
        "F-A": 0.50,
    })

    assert adjusted["B-A"]["holm_adjusted_p"] == pytest.approx(0.04)
    assert adjusted["E-A"]["holm_adjusted_p"] == pytest.approx(0.09)
    assert adjusted["C-A"]["holm_adjusted_p"] == pytest.approx(0.09)
    assert adjusted["F-A"]["holm_adjusted_p"] == pytest.approx(0.50)
    assert adjusted["B-A"]["reject_at_alpha"] is True
    assert adjusted["C-A"]["reject_at_alpha"] is False


def test_paired_bootstrap_is_deterministic_and_null_centered() -> None:
    values = [1.0, 1.0, 2.0, 2.0, 3.0]
    first = formal_analysis.bootstrap_mean_contrast(values, seed=85, replicates=500)
    second = formal_analysis.bootstrap_mean_contrast(values, seed=85, replicates=500)

    assert first == second
    assert first["mean_difference"] == pytest.approx(1.8)
    assert first["owner_count"] == 5
    assert first["bootstrap_replicates"] == 500
    assert 0.0 <= first["two_sided_bootstrap_p"] <= 1.0
    assert first["ci95"][0] <= first["mean_difference"] <= first["ci95"][1]


def test_paired_bootstrap_rejects_empty_or_nonfinite_input() -> None:
    with pytest.raises(ValueError, match="nonempty"):
        formal_analysis.bootstrap_mean_contrast([], seed=1, replicates=10)
    with pytest.raises(ValueError, match="valid range"):
        formal_analysis.bootstrap_mean_contrast([float("nan")], seed=1, replicates=10)


def test_cache_evidence_requires_the_exact_reviewed_report_digest() -> None:
    with pytest.raises(RuntimeError, match="report digest differs"):
        formal_runner.validate_cache_equivalence_report({}, report_sha256="0" * 64)


def test_issue84_runtime_clean_attestation_follows_exact_source_validation(monkeypatch) -> None:
    accepted_root = Path("/accepted/issue84")
    monkeypatch.setenv("ISSUE84_WORKTREE_CLEAN", "untrusted inherited value")
    monkeypatch.setattr(
        formal_runner.pilot, "validate_issue84_source", lambda path: accepted_root,
    )

    assert formal_runner._validate_issue84_source_for_runtime(Path("input")) == accepted_root
    assert formal_runner.os.environ["ISSUE84_WORKTREE_CLEAN"] == "1"

    def reject_dirty_source(path):
        raise RuntimeError("source worktree is dirty")

    monkeypatch.setenv("ISSUE84_WORKTREE_CLEAN", "untrusted inherited value")
    monkeypatch.setattr(formal_runner.pilot, "validate_issue84_source", reject_dirty_source)
    with pytest.raises(RuntimeError, match="source worktree is dirty"):
        formal_runner._validate_issue84_source_for_runtime(Path("input"))
    assert "ISSUE84_WORKTREE_CLEAN" not in formal_runner.os.environ


def test_whole_task_estimate_separates_conditional_core_and_assumed_reserve() -> None:
    estimate = formal_runner.whole_task_active_wall_estimate({
        "conditional_cost_projection": {
            "conditional_formal_core_projection_hours": 19.993465346981917,
            "conditional_pilot_plus_formal_core_hours": 29.06117108279053,
            "conditional_actor_update_projection_hours": 3.696762003157477,
            "unchanged_formal_game_group_projection_hours": 16.29670334382444,
        },
    })

    assert estimate["cumulative_active_wall_hours_approx"] == 40
    assert estimate["cumulative_components_hours"]["pilot_already_spent_and_included_once"] == pytest.approx(9.067705735808613)
    assert estimate["formal_core_breakdown_hours"]["conditional_actor_update_component"] == pytest.approx(3.696762003157477)
    assert estimate["cumulative_components_hours"]["explicit_assumed_program_active_reserve"] == 10
    assert estimate["human_review_or_wait_hours"] == 0
    assert "not a measured upper bound or time cap" in estimate["limitations"][1]


def _write_receipt(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(formal_runner.canonical(value) + b"\n")


def test_test_analysis_accepts_only_source_bound_success_receipt_chain(tmp_path: Path) -> None:
    package = tmp_path / "formal-package"
    attempt = package / "test" / "PARENT" / "units" / "unit-8520001" / "attempt-0001"
    attempt.mkdir(parents=True)
    unit = "TEST:8520001:PARENT"
    commit = "c" * 40
    lock_sha = "s" * 64
    model_digest = "m" * 64
    row = {
        "unit_id": unit,
        "owner_seed": 8520001,
        "game_seed": 8520001,
        "arm": "PARENT",
        "phase": "evaluation",
        "formal": True,
        "paired_owner_key": "issue85-formal-test-8520001",
        "policy_rng_seed": 123,
        "search_invocation_seeds": [11, 12, 13, 14],
    }
    run_id = f"{unit}-attempt-0001"
    result = {
        "schema_id": "issue75-stage1-unit-result-v1",
        "status": "SUCCEEDED",
        "formal": True,
        "unit_id": unit,
        "phase": "evaluation",
        "arm": "PARENT",
        "owner_seed": 8520001,
        "game_seed": 8520001,
        "paired_owner_key": row["paired_owner_key"],
        "package_commit": commit,
        "runner_code_commit": commit,
        "source_lock_sha256": lock_sha,
        "model_digest": model_digest,
        "native_source_commit": formal_runner.LOCK["native"]["commit"],
        "stsr_source_commit": formal_runner.LOCK["stsr_source"]["commit"],
        "native_module_sha256": formal_runner.LOCK["native"]["compiled_module_sha256"],
        "terminal_floor": 16,
        "terminal_outcome": "WIN",
        "act2_plus": True,
        "act3_plus": True,
        "act4_reached": True,
        "heart_victory": True,
        "resource": {
            "ledger_run_id": run_id,
            "worker_wall_seconds": 6.0,
            "worker_process_cpu_seconds": 5.0,
        },
    }
    result_path = attempt / "result.json"
    _write_receipt(result_path, result)
    _write_receipt(attempt / "worker-job.json", {
        "schema_id": "issue79-owner-worker-job-v1",
        "pilot": False,
        "package_commit": commit,
        "runner_code_commit": commit,
        "source_lock_sha256": lock_sha,
        "unit_id": unit,
        "row": row,
        "phase": "evaluation",
        "artifact_root": str(package / "test" / "PARENT"),
        "attempt_dir": str(attempt),
        "result_path": str(result_path),
        "output_dir": str(attempt / "native"),
        "run_id": run_id,
    })
    _write_receipt(attempt / "started.json", {
        "schema_id": "issue79-owner-attempt-start-v1",
        "unit_id": unit,
        "owner_seed": 8520001,
        "arm": "PARENT",
        "run_id": run_id,
        "package_commit": commit,
        "seed_row_sha256": formal_runner.sha256_bytes(formal_runner.canonical(row)),
        "pid": 123,
        "started_monotonic": 1.0,
    })
    _write_receipt(attempt / "finished.json", {
        "schema_id": "issue79-owner-attempt-finish-v1",
        "unit_id": unit,
        "run_id": run_id,
        "status": "SUCCEEDED",
        "result_sha256": formal_runner.file_sha256(result_path),
        "finished_monotonic": 2.0,
    })

    outcome = formal_analysis._read_attempt(
        package_root=package,
        policy="PARENT",
        seed_row=row,
        attempt_relative="test/PARENT/units/unit-8520001/attempt-0001",
        package_commit=commit,
        source_lock_sha256=lock_sha,
        expected_model_digest=model_digest,
    )
    assert outcome["terminal_floor"] == 16
    assert outcome["heart_win"] is True
    assert outcome["worker_wall_seconds"] == 6.0

    result.pop("heart_victory")
    _write_receipt(result_path, result)
    finished = formal_runner.read_json(attempt / "finished.json")
    finished["result_sha256"] = formal_runner.file_sha256(result_path)
    _write_receipt(attempt / "finished.json", finished)
    with pytest.raises(ValueError, match="Heart-victory"):
        formal_analysis._read_attempt(
            package_root=package,
            policy="PARENT",
            seed_row=row,
            attempt_relative="test/PARENT/units/unit-8520001/attempt-0001",
            package_commit=commit,
            source_lock_sha256=lock_sha,
            expected_model_digest=model_digest,
        )
