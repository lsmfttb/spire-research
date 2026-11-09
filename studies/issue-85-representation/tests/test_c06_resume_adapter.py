from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from c06_resume_adapter import (
    EXPECTED_PACKAGE_COMMIT,
    _run_c06_training_games,
    execute_c06_continuation,
)


def _fixture(
    tmp_path: Path, monkeypatch, *, successful_completions: int = 82,
    unstarted_owner_ids: list[str] | None = None,
):
    package_root = tmp_path / "formal-package"
    package_root.mkdir()
    (package_root / "formal-seed-table.json").write_text('{"seed":"current"}', encoding="utf-8")
    issue84_source = tmp_path / "issue84"
    native_source = tmp_path / "native"
    stsr_source = tmp_path / "stsr"
    for path in (issue84_source, native_source, stsr_source):
        path.mkdir()
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_text("{}", encoding="utf-8")

    identity = {"package_commit": EXPECTED_PACKAGE_COMMIT, "source_lock_sha256": "lock"}
    authorization = {
        "sha256": "authorization",
        "predecessor_carry_manifest_sha256": "carry",
        "owner_approval_comment_url": "owner-go",
        "reviewer_approval_comment_url": "reviewer-pass",
        "whole_task_active_wall_cap_seconds": 295632,
    }
    reconciliation = {
        "record_sha256": "reconciliation",
        "c06_partial": {
            "stage_id": "TRAIN-C-BLOCK-06-GAMES",
            "successful_completions": successful_completions,
            "successful_units": [{} for _ in range(successful_completions)],
            "unstarted_owner_ids": (
                unstarted_owner_ids
                if unstarted_owner_ids is not None
                else [f"TRAIN:{owner}:C" for owner in range(8400852, 8400897)]
            ),
            "started_without_terminal": {
                "unit_id": "TRAIN:8400851:C",
                "status": "STARTED_WITHOUT_TERMINAL",
                "next_attempt": "attempt-0002",
            },
        },
    }
    carry = {"e902_canonical_reconciliation": reconciliation}

    class Clock:
        elapsed_before = 100.0

        def __init__(self):
            self.closed = False

        def elapsed(self):
            return 150.0

        def expired(self):
            return False

        def set_processes(self, pids):
            assert pids == set()

        def close(self):
            self.closed = True
            return 150.0

    clock = Clock()
    backend = SimpleNamespace(ActiveClock=lambda *_args: clock)
    formal = SimpleNamespace(
        LOCK={"native": {"commit": "native"}, "stsr_source": {"commit": "stsr"}},
        pilot=SimpleNamespace(source_identity=lambda **_kwargs: identity),
        verify_dry_bundle=lambda **_kwargs: {
            "status": "VERIFIED / NO GAMES OR UPDATES STARTED",
        },
        _verify_native_source=lambda _source: {"commit": "native"},
        _verify_stsr_source=lambda _source: {"commit": "stsr"},
        file_sha256=lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        read_json=lambda path: json.loads(Path(path).read_text(encoding="utf-8")),
        DATA_ROOT=tmp_path / "data",
    )
    formal.DATA_ROOT.mkdir()
    written = {}

    def write_once(path, payload):
        written["receipt"] = dict(payload)
        Path(path).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    formal.pilot._write_or_verify_once = write_once
    runs = []
    updates = []

    def training(_formal, _backend, *, arm, block, **_kwargs):
        runs.append((arm, block))
        return {
            "status": "SUCCEEDED",
            "completed": 128,
            "receipt_sha256": f"group-{arm}-{block}",
            "failed_attempt_count": 1 if block == 6 else 0,
            "e902_canonical_reconciliation_sha256": "reconciliation",
        }

    def actor_update(_formal, _backend, _driver, *, arm, block, **_kwargs):
        updates.append((arm, block))
        return {
            "completed_steps": 64,
            "receipt_sha256": "c06-update",
            "actor_checkpoint_sha256": "c06-actor",
            "optimizer_checkpoint_sha256": "c06-optimizer",
            "training_group_receipt_sha256": "group-C-6",
            "predecessor_carry_manifest_sha256": "carry",
            "source_model_digest": "c05-model",
            "result_model_digest": "c06-model",
        }

    def atomic_json(_formal, path, payload):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    execution = SimpleNamespace(
        os=SimpleNamespace(name="posix", getpid=lambda: 123),
        PROGRESS_SCHEMA="issue85-formal-progress-v1",
        PREDECESSOR_GROUP_RECEIPT_SHA256="predecessor-group",
        _validate_readiness=lambda *_args: (
            package_root, {"package_commit": EXPECTED_PACKAGE_COMMIT}, {"rows": []},
        ),
        validate_authorization=lambda *_args, **_kwargs: authorization,
        _configure_formal_runtime=lambda *_args: (backend, "issue84-driver"),
        _validate_carry_manifest=lambda *_args, **_kwargs: (carry, "carry"),
        _e902_canonical_lineage_required=lambda *_args: True,
        _training_rows=lambda _seed, arm, block: [
            {"owner_seed": 8400001 + block * 128 + index, "unit_id": f"TRAIN:{8400001 + block * 128 + index}:{arm}"}
            for index in range(128)
        ],
        _predecessor_carry_path=lambda root: root / "predecessor-carry-manifest.json",
        _now=lambda: "2026-11-09T00:00:00Z",
        _atomic_json=atomic_json,
        _run_training_games=training,
        _e902_carried_units_from_references=lambda *_args, **_kwargs: ({}, {}),
        _read_update_receipt=lambda *_args, **_kwargs: {
            "receipt_sha256": "c05-update",
            "actor_checkpoint_sha256": "c05-actor",
            "optimizer_checkpoint_sha256": "c05-optimizer",
            "result_model_digest": "c05-model",
        },
        _train_actor_block=actor_update,
    )
    return (
        formal, execution, authorization_path, issue84_source, native_source, stsr_source,
        package_root, runs, updates, written, clock,
    )


def test_c06_adapter_runs_only_c05_carry_and_c06_games_plus_one_update(tmp_path, monkeypatch):
    (
        formal, execution, authorization, issue84, native, stsr,
        package_root, runs, updates, written, clock,
    ) = _fixture(tmp_path, monkeypatch)

    result = execute_c06_continuation(
        formal, execution,
        issue84_source=issue84, native_source=native, stsr_source=stsr,
        authorization_receipt=authorization, adapter_commit="adapter-commit",
        adapter_sha256="adapter-hash",
    )

    assert result["status"] == "C06_CONTINUATION_SUCCEEDED"
    assert result["later_registered_stages_started"] is False
    assert runs == [("C", 5), ("C", 6)]
    assert updates == [("C", 6)]
    assert written["receipt"]["c06_completed"] == 128
    assert written["receipt"]["c06_failed_attempt_count"] == 1
    assert written["receipt"]["c05_result_model_digest"] == "c05-model"
    progress = json.loads((package_root / "formal-progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "C06_CONTINUATION_SUCCEEDED"
    assert progress["active_stage"] == "C06-CONTINUATION-COMPLETE"
    assert set(progress["game_groups"]) == {"TRAIN-C-BLOCK-05", "TRAIN-C-BLOCK-06"}
    assert set(progress["actor_updates"]) == {"C:5", "C:6"}
    assert clock.closed


def test_c06_adapter_rejects_a_changed_canonical_boundary_before_starting(tmp_path, monkeypatch):
    (
        formal, execution, authorization, issue84, native, stsr,
        _package_root, runs, updates, _written, clock,
    ) = _fixture(tmp_path, monkeypatch, successful_completions=81)

    with pytest.raises(RuntimeError, match="82/1/45 boundary"):
        execute_c06_continuation(
            formal, execution,
            issue84_source=issue84, native_source=native, stsr_source=stsr,
            authorization_receipt=authorization, adapter_commit="adapter-commit",
            adapter_sha256="adapter-hash",
        )

    assert runs == []
    assert updates == []
    assert not clock.closed


def test_c06_adapter_rejects_a_changed_unstarted_owner_list_before_starting(tmp_path, monkeypatch):
    wrong_owner_ids = [
        *[f"TRAIN:{owner}:C" for owner in range(8400852, 8400896)],
        "TRAIN:8400897:C",
    ]
    (
        formal, execution, authorization, issue84, native, stsr,
        _package_root, runs, updates, _written, clock,
    ) = _fixture(tmp_path, monkeypatch, unstarted_owner_ids=wrong_owner_ids)

    with pytest.raises(RuntimeError, match="82/1/45 boundary"):
        execute_c06_continuation(
            formal, execution,
            issue84_source=issue84, native_source=native, stsr_source=stsr,
            authorization_receipt=authorization, adapter_commit="adapter-commit",
            adapter_sha256="adapter-hash",
        )

    assert runs == []
    assert updates == []
    assert not clock.closed


def test_c06_training_game_adapter_makes_allowed_roots_mutable_and_restores_helper():
    execution = SimpleNamespace()
    original = lambda *_args, **_kwargs: ({"unit": {}}, {"package": ["c06", "c05"]})

    def training(*_args, **_kwargs):
        carried, allowed_roots = execution._e902_carried_units_from_references()
        assert carried == {"unit": {}}
        assert allowed_roots == {"package": {"c06", "c05"}}
        allowed_roots.setdefault("package", set()).add("interrupted-owner")
        return {"status": "SUCCEEDED"}

    execution._e902_carried_units_from_references = original
    execution._run_training_games = training

    result = _run_c06_training_games(execution)

    assert result == {"status": "SUCCEEDED"}
    assert execution._e902_carried_units_from_references is original


def test_c06_adapter_preserves_failed_attempt_when_resuming(tmp_path, monkeypatch):
    (
        formal, execution, authorization, issue84, native, stsr,
        package_root, runs, updates, written, clock,
    ) = _fixture(tmp_path, monkeypatch)
    progress_path = package_root / "formal-progress.json"
    prior = {
        "schema_id": execution.PROGRESS_SCHEMA,
        "status": "FAILED_OR_PAUSED",
        "package_commit": EXPECTED_PACKAGE_COMMIT,
        "source_lock_sha256": "lock",
        "seed_table_sha256": formal.file_sha256(package_root / "formal-seed-table.json"),
        "authorization_receipt_sha256": "authorization",
        "predecessor_carry_manifest_sha256": "carry",
        "game_groups": {"TRAIN-C-BLOCK-05": {"status": "SUCCEEDED", "completed": 128}},
        "actor_updates": {},
        "adapter_commit": "prior-adapter-commit",
        "adapter_sha256": "prior-adapter-hash",
        "started_utc": "2026-11-08T20:00:00Z",
        "finished_or_paused_utc": "2026-11-08T20:01:00Z",
        "active_stage": "TRAIN-C-BLOCK-06-GAMES",
        "failure": {"type": "AttributeError", "detail": "list object has no attribute add"},
        "cumulative_active_wall_seconds": 71769.5,
        "clock_start_seconds": 71749.5,
    }
    execution._atomic_json(formal, progress_path, prior)

    result = execute_c06_continuation(
        formal, execution,
        issue84_source=issue84, native_source=native, stsr_source=stsr,
        authorization_receipt=authorization, adapter_commit="new-adapter-commit",
        adapter_sha256="new-adapter-hash",
    )

    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert result["status"] == "C06_CONTINUATION_SUCCEEDED"
    assert progress["adapter_attempt_number"] == 2
    assert progress["adapter_attempt_history"] == [{
        "status": "FAILED_OR_PAUSED",
        "active_stage": "TRAIN-C-BLOCK-06-GAMES",
        "adapter_commit": "prior-adapter-commit",
        "adapter_sha256": "prior-adapter-hash",
        "started_utc": "2026-11-08T20:00:00Z",
        "finished_or_paused_utc": "2026-11-08T20:01:00Z",
        "failure": {"type": "AttributeError", "detail": "list object has no attribute add"},
        "cumulative_active_wall_seconds": 71769.5,
        "clock_start_seconds": 71749.5,
    }]
    assert "failure" not in progress
    assert runs == [("C", 5), ("C", 6)]
    assert updates == [("C", 6)]
    assert clock.closed
