from __future__ import annotations

from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import formal_execution
import formal_runner


def test_formal_runner_exports_the_locked_wsl_data_root() -> None:
    assert formal_runner.DATA_ROOT_WSL == formal_runner.pilot.DATA_ROOT_WSL


@pytest.mark.parametrize("block", range(8))
def test_training_worker_accepts_issue84_block_dispatch_for_locked_pi12_lineage(block: int) -> None:
    row = {
        "issue85_arm": "B",
        "block_index": block,
        "issue84_policy_label": "pi12",
    }

    assert formal_execution._formal_training_dispatch(
        formal_runner, row, f"pi{12 + block}",
    ) == ("B", block)


@pytest.mark.parametrize(
    ("row", "loader_label"),
    [
        ({"issue85_arm": "B", "block_index": 1, "issue84_policy_label": "pi12"}, "pi1"),
        ({"issue85_arm": "B", "block_index": 1, "issue84_policy_label": "pi12"}, "pi12"),
        ({"issue85_arm": "B", "block_index": 1, "issue84_policy_label": "pi20"}, "pi13"),
        ({"issue85_arm": "A", "block_index": 1, "issue84_policy_label": "pi12"}, "pi13"),
        ({"issue85_arm": "B", "block_index": True, "issue84_policy_label": "pi12"}, "pi13"),
        ({"issue85_arm": "B", "block_index": 8, "issue84_policy_label": "pi12"}, "pi20"),
    ],
)
def test_training_worker_rejects_unregistered_block_dispatch(row: dict, loader_label: str) -> None:
    with pytest.raises(RuntimeError, match="invalid arm/block/model label"):
        formal_execution._formal_training_dispatch(formal_runner, row, loader_label)


def test_b01_actor_update_uses_pinned_issue84_dispatch_token(tmp_path: Path, monkeypatch) -> None:
    package_commit = "b" * 40
    carry_sha = "e" * 64
    identity = {
        "package_commit": package_commit,
        "source_lock_sha256": formal_execution.PREDECESSOR_SOURCE_LOCK_SHA256,
    }
    manifest = {"resumed_training_group_carries": [dict(formal_execution.RESUMED_B01_GROUP_CARRY)]}
    monkeypatch.setattr(
        formal_execution, "_validate_carry_manifest",
        lambda *_args, **_kwargs: (manifest, carry_sha),
    )
    loader_labels: list[str] = []

    class StopAfterModelLoad(Exception):
        pass

    class Executor:
        def _load_model(self, label: str, *, proxy: bool):
            assert proxy is False
            loader_labels.append(label)
            raise StopAfterModelLoad

    executor = Executor()
    backend = SimpleNamespace(load_modules=lambda *_args, **_kwargs: (None, None, None, None, executor))
    formal = SimpleNamespace(
        CONTRACT={"training": {"owners_per_block": 128, "updates_per_block": 64, "minibatch_owners": 32}},
        pilot=SimpleNamespace(_CURRENT_ROW=ContextVar("issue85-current-row", default=None)),
    )
    rows = [{"owner_seed": owner} for owner in range(8400129, 8400257)]
    seed_table = {
        "actor_update_schedule": [{"arm": "B", "block_index": 1, "seed": 12345}],
        "actor_update_schedule_sha256": "a" * 64,
    }
    training_group = {
        "receipt_sha256": formal_execution.RESUMED_B01_GROUP_RECEIPT_SHA256,
        "training_data_package_commit": formal_execution.RESUMED_GAMES_PACKAGE_COMMIT,
        "predecessor_carry_manifest_sha256": carry_sha,
    }
    with pytest.raises(StopAfterModelLoad):
        formal_execution._train_actor_block(
            formal, backend, object(), arm="B", block=1, rows=rows,
            package_root=tmp_path / package_commit, identity=identity,
            seed_table=seed_table, seed_table_sha256="d" * 64,
            training_group=training_group, clock=object(),
        )
    assert loader_labels == ["pi13"]
    assert formal.pilot._CURRENT_ROW.get() is None


@pytest.mark.parametrize(
    ("steps", "parameter_count", "allowed", "valid"),
    [
        ([128] * 6, 6, {64, 128}, True),
        ([64, 128, 128, 128, 128, 128], 6, {64, 128}, True),
        ([64] * 6, 6, {64}, True),
        ([129] * 6, 6, {64, 128}, False),
        ([128] * 6, 6, {64}, False),
        ([128] * 5, 6, {64, 128}, False),
    ],
)
def test_formal_adamw_lineage_checks_each_parameter_step(
    steps: list[int], parameter_count: int, allowed: set[int], valid: bool,
) -> None:
    if valid:
        formal_execution._validate_formal_adamw_lineage(
            steps, parameter_count=parameter_count, allowed_steps=allowed,
            arm="B", block=0, context="formal",
        )
    else:
        with pytest.raises(RuntimeError, match="AdamW"):
            formal_execution._validate_formal_adamw_lineage(
                steps, parameter_count=parameter_count, allowed_steps=allowed,
                arm="B", block=0, context="formal",
            )


def _active_clock(*, elapsed: float, active_session: str | None, process_ids: list[int]) -> dict:
    return {
        "schema_id": "issue79-active-wall-clock-v1",
        "mode": "issue85-task",
        "elapsed_seconds": elapsed,
        "active_session": active_session,
        "process_ids": process_ids,
    }


def test_stale_active_wall_session_is_resumable_without_resetting_elapsed(monkeypatch) -> None:
    monkeypatch.setattr(formal_execution.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    assert formal_execution._validate_active_wall_clock(
        _active_clock(elapsed=32_643.7, active_session="interrupted", process_ids=[987_654_321]),
        100_000,
        require_idle=True,
    ) == pytest.approx(32_643.7)

    monkeypatch.setattr(formal_execution.os, "kill", lambda pid, sig: None)
    with pytest.raises(RuntimeError, match="remains active"):
        formal_execution._validate_active_wall_clock(
            _active_clock(elapsed=12.0, active_session="live", process_ids=[123]),
            100_000,
            require_idle=True,
        )


@pytest.mark.parametrize(
    ("current_elapsed", "must_reject"),
    [
        (34_996.251722737004 - 0.25, True),
        (34_996.251722737004, False),
        (34_996.251722737004 + 0.25, False),
    ],
)
def test_fresh_carry_cannot_lower_pinned_predecessor_progress_time(
    current_elapsed: float, must_reject: bool,
) -> None:
    predecessor_elapsed = 34_996.251722737004
    if must_reject:
        with pytest.raises(RuntimeError, match="below pinned predecessor high-water"):
            formal_execution._require_active_wall_high_water(
                current_elapsed, predecessor_elapsed, context="test predecessor progress",
            )
    else:
        assert formal_execution._require_active_wall_high_water(
            current_elapsed, predecessor_elapsed, context="test predecessor progress",
        ) == pytest.approx(predecessor_elapsed)


@pytest.mark.parametrize(
    ("current_elapsed", "must_reject"),
    [
        (formal_execution.PRESERVED_ACTIVE_WALL_CARRY_SECONDS - 0.25, True),
        (formal_execution.PRESERVED_ACTIVE_WALL_CARRY_SECONDS, False),
        (formal_execution.PRESERVED_ACTIVE_WALL_CARRY_SECONDS + 0.25, False),
    ],
)
def test_fresh_carry_preserves_existing_active_wall_floor(
    current_elapsed: float, must_reject: bool,
) -> None:
    floor = formal_execution.PRESERVED_ACTIVE_WALL_CARRY_SECONDS
    if must_reject:
        with pytest.raises(RuntimeError, match="below pinned predecessor high-water"):
            formal_execution._require_active_wall_high_water(
                current_elapsed, floor, context="test preserved carry",
            )
    else:
        assert formal_execution._require_active_wall_high_water(
            current_elapsed, floor, context="test preserved carry",
        ) == pytest.approx(floor)


@pytest.mark.parametrize(
    "predecessor_elapsed",
    [True, float("nan"), float("inf"), -1.0, formal_execution.FORMAL_ACTIVE_WALL_CAP_SECONDS + 1, None],
)
def test_carry_rejects_malformed_pinned_predecessor_active_wall(
    predecessor_elapsed: object,
) -> None:
    with pytest.raises(RuntimeError, match="pinned predecessor progress"):
        formal_execution._require_active_wall_high_water(
            40_000.0, predecessor_elapsed, context="test predecessor progress",
        )


def test_prior_carry_active_wall_floor_is_source_and_snapshot_verified(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path
    package_root = (
        data_root / "issue85" / "artifacts" / "formal"
        / formal_execution.PRESERVED_ACTIVE_WALL_CARRY_PACKAGE_COMMIT
    )
    package_root.mkdir(parents=True)
    formal = SimpleNamespace(
        DATA_ROOT=data_root,
        read_json=formal_runner.read_json,
        file_sha256=formal_runner.file_sha256,
        canonical=formal_runner.canonical,
        sha256_bytes=formal_runner.sha256_bytes,
    )
    snapshot_body = {
        "elapsed_seconds": formal_execution.PRESERVED_ACTIVE_WALL_CARRY_SECONDS,
        "active_session": None,
        "process_ids": [],
        "receipt_file_sha256": "f" * 64,
    }
    snapshot = {
        **snapshot_body,
        "snapshot_sha256": formal_execution._hash_json(formal, snapshot_body),
    }
    manifest = {
        "schema_id": formal_execution.PREDECESSOR_CARRY_SCHEMA,
        "status": "VERIFIED_READ_ONLY_CONTINUATION",
        "successor": {
            "package_commit": formal_execution.PRESERVED_ACTIVE_WALL_CARRY_PACKAGE_COMMIT,
            "source_lock_sha256": formal_execution.PREDECESSOR_SOURCE_LOCK_SHA256,
        },
        "actor_update_carries": [{"active_wall_clock_snapshot": snapshot}],
    }
    manifest["manifest_sha256"] = formal_execution._hash_json(formal, manifest)
    manifest_path = formal_execution._predecessor_carry_path(package_root)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        formal_execution,
        "PRESERVED_ACTIVE_WALL_CARRY_MANIFEST_SHA256",
        formal_runner.file_sha256(manifest_path),
    )

    assert formal_execution._preserved_active_wall_high_water(formal) == pytest.approx(
        formal_execution.PRESERVED_ACTIVE_WALL_CARRY_SECONDS,
    )


@pytest.mark.parametrize(
    ("current_elapsed", "must_reject"),
    [
        (33_893.47028780101 - 0.25, True),
        (33_893.47028780101, False),
        (33_893.47028780101 + 10.0, False),
    ],
)
def test_validate_authorization_enforces_carried_active_wall_high_water(
    monkeypatch, tmp_path: Path, current_elapsed: float, must_reject: bool,
) -> None:
    package_root = tmp_path / "formal-package"
    package_root.mkdir()
    data_root = tmp_path / "data"
    contract_path = tmp_path / "contract.json"
    contract_path.write_text("{}", encoding="utf-8")
    study = tmp_path / "study"
    study.mkdir()
    analysis_path = study / "formal_analysis.py"
    analysis_path.write_text("# frozen test analysis\n", encoding="utf-8")

    identity = {"package_commit": "a" * 40, "source_lock_sha256": "b" * 64}
    seed_sha = "c" * 64
    schedule_sha = "d" * 64
    carry_sha = "e" * 64
    readiness = {"formal_seed_table_sha256": seed_sha}
    seed = {"actor_update_schedule_sha256": schedule_sha}
    carried_elapsed = 33_893.47028780101
    snapshot_body = {
        "elapsed_seconds": carried_elapsed,
        "active_session": None,
        "process_ids": [],
        "receipt_file_sha256": "f" * 64,
    }
    snapshot = {
        **snapshot_body,
        "snapshot_sha256": formal_runner.sha256_bytes(formal_runner.canonical(snapshot_body)),
    }
    carry_manifest = {"actor_update_carries": [{"active_wall_clock_snapshot": snapshot}]}
    monkeypatch.setattr(
        formal_execution, "_formal_paths",
        lambda *_args: (package_root, tmp_path / "readiness.json", tmp_path / "seed.json"),
    )
    monkeypatch.setattr(
        formal_execution, "_validate_carry_manifest",
        lambda *_args, **_kwargs: (carry_manifest, carry_sha),
    )

    formal = SimpleNamespace(
        DATA_ROOT=data_root,
        CONTRACT_PATH=contract_path,
        STUDY=study,
        read_json=formal_runner.read_json,
        file_sha256=formal_runner.file_sha256,
    )
    pilot_report_path = (
        data_root / "issue85" / "artifacts" / "pilot"
        / formal_execution.ACCEPTED_PILOT_PACKAGE_COMMIT / "pilot-report.json"
    )
    pilot_report_path.parent.mkdir(parents=True)
    pilot_report_path.write_text(json.dumps({
        "schema_id": "issue85-pilot-report-v1",
        "status": "SUCCEEDED",
        "package_commit": formal_execution.ACCEPTED_PILOT_PACKAGE_COMMIT,
        "source_lock_sha256": identity["source_lock_sha256"],
        "formal_games_started": 0,
        "formal_cohorts_changed": False,
    }), encoding="utf-8")
    monkeypatch.setattr(
        formal_execution, "ACCEPTED_PILOT_REPORT_SHA256",
        formal_runner.file_sha256(pilot_report_path),
    )

    active_wall = data_root / "issue85" / "artifacts" / "active-wall-issue85-task.json"
    active_wall.parent.mkdir(parents=True, exist_ok=True)
    active_wall.write_text(json.dumps(_active_clock(
        elapsed=current_elapsed, active_session=None, process_ids=[],
    )), encoding="utf-8")
    assert formal_runner.file_sha256(active_wall) != snapshot_body["receipt_file_sha256"]

    authorization = {
        "schema_id": formal_execution.AUTH_SCHEMA,
        "issue": 85,
        "formal_go": True,
        "status": "APPROVED",
        "package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "contract_sha256": formal_runner.file_sha256(contract_path),
        "seed_table_sha256": seed_sha,
        "actor_update_schedule_sha256": schedule_sha,
        "analysis_source_sha256": formal_runner.file_sha256(analysis_path),
        "whole_task_active_wall_cap_seconds": formal_execution.FORMAL_ACTIVE_WALL_CAP_SECONDS,
        "predecessor_carry_manifest_sha256": carry_sha,
        "pilot_report_sha256": formal_runner.file_sha256(pilot_report_path),
        "owner_approval": {
            "status": "APPROVED",
            "decision": "FORMAL_GO",
            "comment_url": formal_execution.FORMAL_GO_COMMENT_URL,
        },
        "reviewer_approval": {
            "status": "APPROVED",
            "package_commit": identity["package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "seed_table_sha256": seed_sha,
            "actor_update_schedule_sha256": schedule_sha,
            "analysis_source_sha256": formal_runner.file_sha256(analysis_path),
            "decision": "PASS",
            "comment_url": "https://github.com/lsmfttb/spire-research/issues/85#issuecomment-123",
        },
    }
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_text(json.dumps(authorization), encoding="utf-8")

    if must_reject:
        with pytest.raises(RuntimeError, match="below the immutable carried active-wall high-water"):
            formal_execution.validate_authorization(
                formal, authorization_path, identity=identity, readiness=readiness, seed=seed,
            )
    else:
        result = formal_execution.validate_authorization(
            formal, authorization_path, identity=identity, readiness=readiness, seed=seed,
        )
        assert result["prior_active_wall_seconds"] == pytest.approx(current_elapsed)


def test_formal_runtime_dispatches_locked_a_without_using_issue84_final_pi20(monkeypatch, tmp_path: Path) -> None:
    current_row = ContextVar("test_formal_current_row", default=None)
    parent_actor = object()
    baseline_actor = object()
    model = SimpleNamespace(
        model_digest=lambda actor: "parent-digest" if actor is parent_actor else "baseline-digest",
    )
    original_calls: list[str] = []
    checkpoint_calls: list[tuple[Path, str, str]] = []

    def original_load_model(label: str, *, proxy: bool = False):
        original_calls.append(label)
        if label == "pi12":
            return parent_actor, "parent-digest", {"checkpoint_sha256": "parent-checkpoint-sha"}
        raise AssertionError(f"unexpected original model load: {label}")

    executor = SimpleNamespace(_load_model=original_load_model, _worker_owner=lambda job, ledger: {})

    def root_loader(active_root: Path, *, pilot: bool):
        assert pilot is True  # the Issue 84 runtime loader's carry-forward compatibility mode
        return "algorithm", "runtime", model, "learning", executor

    backend = SimpleNamespace(load_modules=root_loader)
    issue84_driver = SimpleNamespace(
        _ISSUE84_HELDOUT_ORIGINAL_ARM=ContextVar("test_issue84_arm", default=None),
        _ISSUE84_HELDOUT_ORIGINAL_GROUP=ContextVar("test_issue84_group", default=None),
    )

    def load_checkpoint(model_arg, checkpoint: Path, expected_sha: str, expected_digest: str):
        assert model_arg is model
        checkpoint_calls.append((checkpoint, expected_sha, expected_digest))
        return baseline_actor

    pilot_module = SimpleNamespace(
        _CURRENT_ROW=current_row,
        _configure_runtime=lambda *args: (backend, issue84_driver),
        _load_checkpoint=load_checkpoint,
        _path_from_wsl=lambda value: tmp_path,
        derive_seed=lambda *args: 1,
    )
    lock = {
        "model_runtime": {
            "python_data_path": "runtime/python3.13",
            "python_sha256": hashlib.sha256(b"source-locked-python").hexdigest(),
        },
        "baseline_P": {"model_digest": "parent-digest", "checkpoint_sha256": "parent-checkpoint-sha"},
        "baseline_A": {
            "model_digest": "baseline-digest",
            "checkpoint_sha256": "baseline-checkpoint-sha",
            "checkpoint_data_path": "issue84/artifacts/formal/B/models/pi20.pt",
            "issue84_training_package_commit": "d" * 40,
        },
    }
    formal = SimpleNamespace(
        pilot=pilot_module,
        __file__=str(tmp_path / "formal_runner.py"),
        DATA_ROOT=tmp_path,
        DATA_ROOT_WSL="/mnt/d/DeadlyCatCoding/stsrl-data",
        LOCK=lock,
        file_sha256=formal_runner.file_sha256,
        NEW_ARMS=("B", "C", "D", "E", "F"),
        POLICIES=("PARENT", "A", "B", "C", "D", "E", "F"),
    )
    representations = ModuleType("representations")
    representations.Issue85Arm = type("Issue85Arm", (), {})
    monkeypatch.setitem(sys.modules, "representations", representations)

    pinned_python = tmp_path / "runtime/python3.13"
    pinned_python.parent.mkdir(parents=True)
    pinned_python.write_bytes(b"source-locked-python")
    configured, _ = formal_execution._configure_formal_runtime(
        formal, tmp_path / "issue84", {"package_commit": "c" * 40}, tmp_path / "package",
    )
    assert configured.PINNED_PYTHON == pinned_python
    _algorithm, _runtime, _model, _learning, configured_executor = configured.load_modules(tmp_path, pilot=False)

    a_token = current_row.set({
        "formal": True,
        "unit_id": "EVAL:8510001:A",
        "phase": "evaluation",
        "arm": "A",
        "issue85_phase": "validation",
    })
    try:
        actor, digest, record = configured_executor._load_model("pi20", proxy=False)
    finally:
        current_row.reset(a_token)
    assert actor is baseline_actor
    assert digest == "baseline-digest"
    assert record["source"] == "exact-locked-issue84-B1024-baseline"
    assert checkpoint_calls == [(
        tmp_path / "issue84/artifacts/formal/B/models/pi20.pt",
        "baseline-checkpoint-sha",
        "baseline-digest",
    )]
    assert original_calls == []

    parent_token = current_row.set({
        "formal": True,
        "unit_id": "EVAL:8510001:PARENT",
        "phase": "evaluation",
        "arm": "PARENT",
        "issue85_phase": "validation",
    })
    try:
        actor, digest, record = configured_executor._load_model("pi12", proxy=False)
    finally:
        current_row.reset(parent_token)
    assert actor is parent_actor
    assert digest == "parent-digest"
    assert record["checkpoint_sha256"] == "parent-checkpoint-sha"
    assert original_calls == ["pi12"]


def test_predecessor_group_reference_uses_original_receipt_without_forging_one(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    group_root = data_root / formal_execution.PREDECESSOR_GROUP_ROOT
    group_root.mkdir(parents=True)
    original_receipt = group_root / "group-receipt.json"
    original_receipt.write_bytes(b'{"retained":"original"}\\n')
    receipt_sha = formal_runner.file_sha256(original_receipt)
    monkeypatch.setattr(formal_execution, "PREDECESSOR_GROUP_RECEIPT_SHA256", receipt_sha)

    package_commit = formal_execution.OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT
    source_lock_sha = formal_execution.PREDECESSOR_SOURCE_LOCK_SHA256
    package_root = data_root / "issue85" / "artifacts" / "formal" / package_commit
    package_root.mkdir(parents=True)
    manifest = {
        "schema_id": formal_execution.PREDECESSOR_CARRY_SCHEMA,
        "status": "VERIFIED_READ_ONLY_CONTINUATION",
        "successor": {"package_commit": package_commit, "source_lock_sha256": source_lock_sha},
        "predecessor": {
            "package_commit": formal_execution.PREDECESSOR_PACKAGE_COMMIT,
            "source_lock_sha256": source_lock_sha,
            "formal_seed_table_sha256": formal_execution.PREDECESSOR_SEED_TABLE_SHA256,
            "group_receipt_sha256": receipt_sha,
            "formal_progress_sha256": formal_execution.PREDECESSOR_PROGRESS_SHA256,
            "authorization_receipt_sha256": formal_execution.PREDECESSOR_AUTHORIZATION_SHA256,
            "group_root": formal_execution.PREDECESSOR_GROUP_ROOT,
            "failed_update": {
                "failure_type": "AttributeError",
                "failure_detail": formal_execution.PREDECESSOR_UPDATE_FAILURE_DETAIL,
                "log_path": formal_execution.PREDECESSOR_UPDATE_FAILURE_LOG,
                "log_sha256": formal_execution.PREDECESSOR_UPDATE_FAILURE_LOG_SHA256,
                "formal_progress_sha256": formal_execution.PREDECESSOR_PROGRESS_SHA256,
            },
        },
        "owner_go_comment_url": formal_execution.FORMAL_GO_COMMENT_URL,
        "validated_units": 128,
        "formal_games_carried": 128,
        "actor_updates_carried": 0,
        "units_sha256": "a" * 64,
    }
    manifest["manifest_sha256"] = formal_runner.sha256_bytes(formal_runner.canonical(manifest))
    carry_path = formal_execution._predecessor_carry_path(package_root)
    carry_path.write_text(json.dumps(manifest), encoding="utf-8")
    formal = SimpleNamespace(
        DATA_ROOT=data_root,
        read_json=formal_runner.read_json,
        file_sha256=formal_runner.file_sha256,
        canonical=formal_runner.canonical,
        sha256_bytes=formal_runner.sha256_bytes,
    )

    group = formal_execution._predecessor_training_group(
        formal, package_root, {"package_commit": package_commit, "source_lock_sha256": source_lock_sha},
    )

    assert group["package_commit"] == formal_execution.PREDECESSOR_PACKAGE_COMMIT
    assert group["training_data_package_commit"] == formal_execution.PREDECESSOR_PACKAGE_COMMIT
    assert group["receipt_sha256"] == receipt_sha
    assert group["group_root"] == str(group_root)
    assert group["schema_id"] == formal_execution.CARRIED_GROUP_REFERENCE_SCHEMA
    assert "no successor game receipt" in group["artifact_kind"]
    assert not (package_root / "training/B/block-00/games/group-receipt.json").exists()


def test_new_successor_carry_requires_an_actor_update_reference(tmp_path: Path, monkeypatch) -> None:
    package_commit = "b" * 40
    source_lock_sha = formal_execution.PREDECESSOR_SOURCE_LOCK_SHA256
    package_root = tmp_path / "issue85" / "artifacts" / "formal" / package_commit
    package_root.mkdir(parents=True)
    checked: list[dict] = []
    monkeypatch.setattr(
        formal_execution, "_validate_operational_actor_update_carry",
        lambda _formal, carry: checked.append(dict(carry)) or dict(carry),
    )
    formal = SimpleNamespace(
        read_json=formal_runner.read_json,
        file_sha256=formal_runner.file_sha256,
        canonical=formal_runner.canonical,
        sha256_bytes=formal_runner.sha256_bytes,
    )
    manifest = {
        "schema_id": formal_execution.PREDECESSOR_CARRY_SCHEMA,
        "status": "VERIFIED_READ_ONLY_CONTINUATION",
        "successor": {"package_commit": package_commit, "source_lock_sha256": source_lock_sha},
        "predecessor": {
            "package_commit": formal_execution.PREDECESSOR_PACKAGE_COMMIT,
            "source_lock_sha256": formal_execution.PREDECESSOR_SOURCE_LOCK_SHA256,
            "formal_seed_table_sha256": formal_execution.PREDECESSOR_SEED_TABLE_SHA256,
            "group_receipt_sha256": formal_execution.PREDECESSOR_GROUP_RECEIPT_SHA256,
            "formal_progress_sha256": formal_execution.PREDECESSOR_PROGRESS_SHA256,
            "authorization_receipt_sha256": formal_execution.PREDECESSOR_AUTHORIZATION_SHA256,
            "group_root": formal_execution.PREDECESSOR_GROUP_ROOT,
            "failed_update": {
                "failure_type": "AttributeError",
                "failure_detail": formal_execution.PREDECESSOR_UPDATE_FAILURE_DETAIL,
                "log_path": formal_execution.PREDECESSOR_UPDATE_FAILURE_LOG,
                "log_sha256": formal_execution.PREDECESSOR_UPDATE_FAILURE_LOG_SHA256,
                "formal_progress_sha256": formal_execution.PREDECESSOR_PROGRESS_SHA256,
            },
        },
        "owner_go_comment_url": formal_execution.FORMAL_GO_COMMENT_URL,
        "validated_units": 128,
        "formal_games_carried": 128,
        "actor_updates_carried": 1,
        "actor_update_carries": [{"arm": "B", "block_index": 0}],
        "resumed_training_group_carries": [dict(formal_execution.RESUMED_B01_GROUP_CARRY)],
        "resumed_training_games_carried": 128,
    }
    manifest["manifest_sha256"] = formal_runner.sha256_bytes(formal_runner.canonical(manifest))
    formal_execution._predecessor_carry_path(package_root).write_text(json.dumps(manifest), encoding="utf-8")

    loaded, _digest = formal_execution._validate_carry_manifest(
        formal, package_root, {"package_commit": package_commit, "source_lock_sha256": source_lock_sha},
    )
    assert loaded["actor_updates_carried"] == 1
    assert loaded["resumed_training_games_carried"] == 128
    assert loaded["resumed_training_group_carries"] == [formal_execution.RESUMED_B01_GROUP_CARRY]
    assert checked == [{"arm": "B", "block_index": 0}]

    manifest["resumed_training_group_carries"][0]["group_receipt_sha256"] = "0" * 64
    manifest["manifest_sha256"] = formal_runner.sha256_bytes(formal_runner.canonical(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    ))
    formal_execution._predecessor_carry_path(package_root).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="malformed or bound"):
        formal_execution._validate_carry_manifest(
            formal, package_root, {"package_commit": package_commit, "source_lock_sha256": source_lock_sha},
        )

    manifest["resumed_training_group_carries"] = [dict(formal_execution.RESUMED_B01_GROUP_CARRY)]
    manifest["actor_updates_carried"] = 0
    manifest["actor_update_carries"] = []
    manifest["manifest_sha256"] = formal_runner.sha256_bytes(formal_runner.canonical(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    ))
    formal_execution._predecessor_carry_path(package_root).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="malformed or bound"):
        formal_execution._validate_carry_manifest(
            formal, package_root, {"package_commit": package_commit, "source_lock_sha256": source_lock_sha},
        )


def test_b00_update_receipt_is_read_from_its_verified_source_package(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    source_root = data_root / formal_execution.OPERATIONAL_PREDECESSOR_ROOT
    package_root = data_root / "issue85/artifacts/formal" / ("b" * 40)
    source_root.mkdir(parents=True)
    package_root.mkdir(parents=True)
    source_lock_sha = formal_execution.OPERATIONAL_PREDECESSOR_LOCK_SHA256
    fde_carry_sha = "f" * 64
    current_carry_sha = "e" * 64
    actor_path = source_root / "training/B/updates/block-00/attempt-0001/actor-state.pt"
    optimizer_path = source_root / "training/B/updates/block-00/attempt-0001/optimizer-state.pt"
    actor_path.parent.mkdir(parents=True)
    actor_path.write_bytes(b"carried actor")
    optimizer_path.write_bytes(b"carried optimizer")
    receipt_path = source_root / "training/B/updates/block-00/success-receipt.json"
    receipt = {
        "schema_id": formal_execution.UPDATE_SCHEMA,
        "status": "SUCCEEDED",
        "arm": "B",
        "block_index": 0,
        "package_commit": formal_execution.OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT,
        "source_lock_sha256": source_lock_sha,
        "training_data_package_commit": formal_execution.PREDECESSOR_PACKAGE_COMMIT,
        "predecessor_carry_manifest_sha256": fde_carry_sha,
        "training_group_receipt_sha256": formal_execution.PREDECESSOR_GROUP_RECEIPT_SHA256,
        "owner_count": 128,
        "completed_steps": 64,
        "minibatch_owners": 32,
        "seed_table_sha256": "a" * 64,
        "actor_update_seed": 96940393,
        "actor_update_schedule_sha256": "1" * 64,
        "owner_seed_sha256": "2" * 64,
        "result_model_digest": "model-digest",
        "actor_checkpoint_path": actor_path.relative_to(source_root).as_posix(),
        "actor_checkpoint_sha256": formal_runner.file_sha256(actor_path),
        "optimizer_checkpoint_path": optimizer_path.relative_to(source_root).as_posix(),
        "optimizer_checkpoint_sha256": formal_runner.file_sha256(optimizer_path),
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = formal_runner.sha256_bytes(formal_runner.canonical(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    ))
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt_file_sha = formal_runner.file_sha256(receipt_path)
    carry = {
        "arm": "B",
        "block_index": 0,
        "source_package_commit": formal_execution.OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT,
        "source_package_root": formal_execution.OPERATIONAL_PREDECESSOR_ROOT,
        "source_lock_sha256": source_lock_sha,
        "source_seed_table_sha256": receipt["seed_table_sha256"],
        "source_formal_progress_path": "formal-progress.json",
        "source_formal_progress_sha256": "b" * 64,
        "source_authorization_path": "formal-execution-authorization.json",
        "source_authorization_sha256": "c" * 64,
        "source_predecessor_carry_manifest_sha256": fde_carry_sha,
        "source_update_receipt_path": receipt_path.relative_to(source_root).as_posix(),
        "source_update_receipt_file_sha256": receipt_file_sha,
        "source_update_attempt_progress_path": "training/B/updates/block-00/attempt-0001/progress.json",
        "source_update_attempt_progress_sha256": "d" * 64,
        "actor_checkpoint_path": receipt["actor_checkpoint_path"],
        "actor_checkpoint_sha256": receipt["actor_checkpoint_sha256"],
        "optimizer_checkpoint_path": receipt["optimizer_checkpoint_path"],
        "optimizer_checkpoint_sha256": receipt["optimizer_checkpoint_sha256"],
        "receipt_sha256": receipt["receipt_sha256"],
        "actor_update_seed": 96940393,
        "actor_update_schedule_sha256": "1" * 64,
        "owner_seed_sha256": "2" * 64,
        "completed_steps": 64,
        "result_model_digest": "model-digest",
        "active_wall_clock_snapshot": {"snapshot_sha256": "3" * 64},
    }
    manifest = {"actor_update_carries": [carry]}

    def validate_carry(_formal, _root, identity):
        if identity["package_commit"] == formal_execution.OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT:
            return {"actor_update_carries": []}, fde_carry_sha
        return manifest, current_carry_sha

    monkeypatch.setattr(formal_execution, "_validate_carry_manifest", validate_carry)
    formal = SimpleNamespace(
        DATA_ROOT=data_root,
        CONTRACT={"training": {"owners_per_block": 128, "updates_per_block": 64, "minibatch_owners": 32}},
        read_json=formal_runner.read_json,
        file_sha256=formal_runner.file_sha256,
        canonical=formal_runner.canonical,
        sha256_bytes=formal_runner.sha256_bytes,
    )

    loaded = formal_execution._read_update_receipt(
        formal, package_root,
        {"package_commit": "b" * 40, "source_lock_sha256": source_lock_sha},
        arm="B", block=0,
    )

    assert loaded["package_commit"] == formal_execution.OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT
    assert loaded["producer_package_root"] == str(source_root)
    assert loaded["carried_from_package_commit"] == formal_execution.OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT
    assert loaded["carried_by_manifest_sha256"] == current_carry_sha
    assert not (package_root / "training/B/updates/block-00/success-receipt.json").exists()

    local_attempt = package_root / "training/B/updates/block-00/attempt-0001"
    local_attempt.mkdir(parents=True)
    (local_attempt / "progress.json").write_text('{"status":"FAILED"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="local update attempts"):
        formal_execution._read_update_receipt(
            formal, package_root,
            {"package_commit": "b" * 40, "source_lock_sha256": source_lock_sha},
            arm="B", block=0,
        )


def test_verify_predecessor_carry_cli_never_dispatches_formal_execution(monkeypatch, tmp_path: Path, capsys) -> None:
    import formal_execution as execution

    package_commit = "d" * 40
    identity = {"package_commit": package_commit, "source_lock_sha256": "e" * 64}
    package_root = tmp_path / "formal-package"
    readiness = {"package_commit": package_commit, "source_lock_sha256": "e" * 64}
    seed = {"package_commit": package_commit, "source_lock_sha256": "e" * 64}
    monkeypatch.setattr(
        formal_runner, "verify_dry_bundle",
        lambda **_kwargs: {
            "status": "VERIFIED / NO GAMES OR UPDATES STARTED",
            "issue84_source_root": str(tmp_path / "issue84"),
        },
    )
    monkeypatch.setattr(formal_runner.pilot, "source_identity", lambda **_kwargs: identity)
    monkeypatch.setattr(execution, "_validate_readiness", lambda *_args: (package_root, readiness, seed))

    def verify(*_args, **_kwargs):
        return {
            "status": "VERIFIED / 128 PREDECESSOR TERMINALS AND FEATURES / NO NEW GAMES OR UPDATES",
            "formal_games_carried": 128,
            "actor_updates_carried": 0,
            "writes_performed": True,
            "manifest": {"units": ["details omitted from console output"]},
        }

    monkeypatch.setattr(execution, "verify_predecessor_carry", verify)
    monkeypatch.setattr(
        execution, "execute_formal",
        lambda *_args, **_kwargs: pytest.fail("carry-verification mode dispatched formal execution"),
    )

    assert formal_runner.main([
        "--verify-predecessor-carry",
        "--issue84-source", str(tmp_path / "issue84"),
        "--native-source", str(tmp_path / "native"),
        "--stsr-source", str(tmp_path / "stsr"),
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["formal_games_carried"] == 128
    assert report["actor_updates_carried"] == 0
    assert report["writes_performed"] is True
    assert "manifest" not in report


def test_execute_cli_requires_authorization_before_calling_executor(monkeypatch) -> None:
    monkeypatch.setattr(
        formal_execution,
        "execute_formal",
        lambda *args, **kwargs: pytest.fail("formal executor was reached without an authorization receipt"),
    )

    with pytest.raises(SystemExit) as error:
        formal_runner.main([
            "--execute",
            "--issue84-source", "unused-issue84-source",
            "--native-source", "unused-native-source",
            "--stsr-source", "unused-stsr-source",
        ])
    assert error.value.code == 2


def test_wsl_preflight_uses_linux_git_for_native_object_paths(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "worktree"
    repo.mkdir()
    pointer = repo / ".git"
    pointer.write_text(
        "gitdir: /mnt/d/DeadlyCatCoding/spire-research/.git/worktrees/formal-runner-implementation\n",
        encoding="utf-8",
    )
    original_pointer = pointer.read_text(encoding="utf-8")
    preflight = formal_runner.baseline_preflight
    monkeypatch.setattr(preflight, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    calls = []

    def capture(command, **kwargs):
        calls.append((command, kwargs))
        return b"locked-commit\n"

    monkeypatch.setattr(preflight.subprocess, "check_output", capture)
    assert preflight.git_bytes(repo, "rev-parse", "HEAD") == b"locked-commit\n"
    command, kwargs = calls[0]
    assert command == ["/usr/bin/git", "-C", str(repo), "rev-parse", "HEAD"]
    assert kwargs == {}
    assert pointer.read_text(encoding="utf-8") == original_pointer


@pytest.mark.parametrize("field", ["seed_table_sha256", "authorization_receipt_sha256"])
def test_formal_attempt_rejects_job_from_another_frozen_receipt(tmp_path: Path, field: str) -> None:
    root = tmp_path / "validation" / "A"
    row = {"unit_id": "VALIDATION:8510001:A", "phase": "evaluation", "owner_seed": 8510001, "arm": "A"}
    attempt = root / "units" / "unit-8510001" / "attempt-0001"
    attempt.mkdir(parents=True)
    expected = {
        "seed_table_sha256": "s" * 64,
        "authorization_receipt_sha256": "a" * 64,
    }
    job = {
        "schema_id": "issue79-owner-worker-job-v1",
        "pilot": False,
        "formal": True,
        "unit_id": row["unit_id"],
        "run_id": f"{row['unit_id']}-attempt-0001",
        "row": row,
        "phase": "evaluation",
        "package_commit": "c" * 40,
        "runner_code_commit": "c" * 40,
        "source_lock_sha256": "l" * 64,
        **expected,
        "artifact_root": str(root),
        "attempt_dir": str(attempt),
        "result_path": str(attempt / "result.json"),
        "output_dir": str(attempt / "native"),
    }
    job[field] = "b" * 64
    (attempt / "worker-job.json").write_text(json.dumps(job), encoding="utf-8")
    backend = SimpleNamespace(unit_key=lambda _unit_id: "unit-8510001")
    formal = SimpleNamespace(read_json=lambda path: json.loads(Path(path).read_text(encoding="utf-8")))

    with pytest.raises(RuntimeError, match="misbound to a source, row, or output path"):
        formal_execution._unit_attempts(
            formal, backend, root=root, row=row, package_commit="c" * 40,
            lock_sha="l" * 64, seed_table_sha256=expected["seed_table_sha256"],
            authorization_receipt_sha256=expected["authorization_receipt_sha256"],
        )


@pytest.mark.parametrize(
    "field",
    [
        "contract_sha256",
        "actor_update_schedule_sha256",
        "test_owner_range",
        "validation_receipt_sha256",
        "validation_completion_count",
        "owner_approval_comment_url",
        "reviewer_approval_comment_url",
        "status",
        "test_games_started_before_open_receipt",
        "boolean_zero_start_marker",
        "test_policy_models",
        "missing_contract_digest",
    ],
)
def test_resumed_test_open_receipt_revalidates_every_gate_before_any_test_work(
    monkeypatch, tmp_path: Path, field: str,
) -> None:
    policies = ("PARENT", "A", "B", "C", "D", "E", "F")
    new_arms = ("B", "C", "D", "E", "F")
    package_commit = "c" * 40
    source_lock_sha = "d" * 64
    authorization_sha = "e" * 64
    analysis_sha = "f" * 64
    schedule_sha = "1" * 64
    seed_path = tmp_path / "package" / "formal-seed-table.json"
    seed_path.parent.mkdir(parents=True)
    seed_path.write_text("{}", encoding="utf-8")
    package_root = seed_path.parent
    seed_sha = hashlib.sha256(seed_path.read_bytes()).hexdigest()
    contract_path = tmp_path / "contract.json"
    contract_path.write_text("{}", encoding="utf-8")
    study = tmp_path / "study"
    study.mkdir()
    analysis_path = study / "formal_analysis.py"
    analysis_path.write_text("# frozen analysis fixture\n", encoding="utf-8")
    issue84_source = tmp_path / "issue84"
    issue84_source.mkdir()

    model_digests = {
        policy: hashlib.sha256(policy.encode("ascii")).hexdigest()
        for policy in policies
    }
    authorization = {
        "sha256": authorization_sha,
        "predecessor_carry_manifest_sha256": "9" * 64,
        "owner_approval_comment_url": "https://github.com/lsmfttb/spire-research/issues/85#issuecomment-100",
        "reviewer_approval_comment_url": "https://github.com/lsmfttb/spire-research/issues/85#issuecomment-101",
        "whole_task_active_wall_cap_seconds": 1_000_000,
        "path": str(tmp_path / "authorization.json"),
    }
    identity = {"package_commit": package_commit, "source_lock_sha256": source_lock_sha}
    readiness = {"formal_seed_table_sha256": seed_sha}
    seed = {"actor_update_schedule_sha256": schedule_sha}

    def file_sha256(path: Path) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def read_json(path: Path) -> dict:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    formal = SimpleNamespace(
        pilot=SimpleNamespace(
            source_identity=lambda *, require_clean: identity,
            _write_or_verify_once=formal_runner._write_or_verify_once,
        ),
        __file__=str(tmp_path / "formal_runner.py"),
        DATA_ROOT=tmp_path / "data",
        LOCK={
            "baseline_P": {"model_digest": model_digests["PARENT"]},
            "baseline_A": {"model_digest": model_digests["A"]},
            "native": {"commit": "native-commit"},
            "stsr_source": {"commit": "stsr-commit"},
        },
        NEW_ARMS=new_arms,
        POLICIES=policies,
        CONTRACT={
            "training": {"owners_per_block": 128},
            "registered_design_unchanged": {
                "test": {"owners": [8520001, 8520320]},
            },
        },
        CONTRACT_PATH=contract_path,
        STUDY=study,
        _registered_ranges=lambda: {"validation": range(64), "test": range(320)},
        formal_plan=lambda: {"training_blocks_per_arm": 2},
        file_sha256=file_sha256,
        read_json=read_json,
        verify_dry_bundle=lambda **_kwargs: {
            "status": "VERIFIED / NO GAMES OR UPDATES STARTED",
        },
        _verify_native_source=lambda _path: {"commit": "native-commit"},
        _verify_stsr_source=lambda _path: {"commit": "stsr-commit"},
    )
    validation_groups = {}
    for policy in policies:
        group_root = package_root / "validation" / policy
        group_root.mkdir(parents=True)
        receipt = {
            "schema_id": formal_execution.GROUP_SCHEMA,
            "status": "SUCCEEDED",
            "stage_id": f"VALIDATION-{policy}",
            "package_commit": package_commit,
            "source_lock_sha256": source_lock_sha,
            "seed_table_sha256": seed_sha,
            "authorization_receipt_sha256": authorization_sha,
            "row_count": 64,
            "completed": 64,
            "units": [
                {
                    "unit_id": f"EVAL:{8510001 + index}:{policy}",
                    "owner_seed": 8510001 + index,
                    "attempt_path": f"validation/{policy}/attempt-{index:04d}",
                    "result_sha256": "2" * 64,
                    "model_digest": model_digests[policy],
                    "attempt_count": 1,
                }
                for index in range(64)
            ],
            "model_selection_or_tuning": False,
            "created_utc": "2026-11-06T00:00:00+00:00",
        }
        receipt_path = group_root / "group-receipt.json"
        receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
        validation_groups[policy] = {
            **receipt,
            "receipt_sha256": file_sha256(receipt_path),
            "group_root": str(group_root),
        }

    open_path, initial_open_sha = formal_execution._test_open_receipt(
        formal, package_root=package_root, identity=identity, readiness=readiness,
        seed=seed, authorization=authorization,
        validation_groups=validation_groups, model_digests=model_digests,
    )
    resumed_path, resumed_open_sha = formal_execution._test_open_receipt(
        formal, package_root=package_root, identity=identity, readiness=readiness,
        seed=seed, authorization=authorization,
        validation_groups=validation_groups, model_digests=model_digests,
    )
    assert resumed_path == open_path
    assert resumed_open_sha == initial_open_sha
    saved = read_json(open_path)
    if field == "validation_receipt_sha256":
        saved["validation_group_receipts"]["A"]["receipt_sha256"] = "0" * 64
    elif field == "validation_completion_count":
        saved["validation_group_receipts"]["A"]["completed"] = 63
    elif field == "test_policy_models":
        saved["test_policy_models"]["A"] = "0" * 64
    elif field == "missing_contract_digest":
        del saved["contract_sha256"]
    elif field == "test_owner_range":
        saved[field] = [8520002, 8520320]
    elif field == "test_games_started_before_open_receipt":
        saved[field] = 1
    elif field == "boolean_zero_start_marker":
        saved["test_games_started_before_open_receipt"] = False
    elif field == "status":
        saved[field] = "TEST_OPEN / stale"
    elif field == "owner_approval_comment_url":
        saved[field] = "https://example.invalid/owner"
    elif field == "reviewer_approval_comment_url":
        saved[field] = "https://example.invalid/reviewer"
    else:
        saved[field] = "0" * 64
    open_path.write_text(json.dumps(saved, sort_keys=True), encoding="utf-8")

    for arm in new_arms:
        for block in range(2):
            update_receipt = package_root / "training" / arm / "updates" / f"block-{block:02d}" / "success-receipt.json"
            update_receipt.parent.mkdir(parents=True, exist_ok=True)
            update_receipt.write_text("{}", encoding="utf-8")

    class Clock:
        elapsed_before = 0.0

        def elapsed(self) -> float:
            return 0.0

        def expired(self) -> bool:
            return False

        def set_processes(self, _pids: set[int]) -> None:
            pass

        def close(self) -> float:
            return 0.0

    backend = SimpleNamespace(ActiveClock=lambda *_args: Clock())
    monkeypatch.setattr(formal_execution, "os", SimpleNamespace(name="posix", getpid=lambda: 123))
    monkeypatch.setattr(formal_execution, "_validate_readiness", lambda *_args: (package_root, readiness, seed))
    monkeypatch.setattr(formal_execution, "validate_authorization", lambda *_args, **_kwargs: authorization)
    monkeypatch.setattr(formal_execution, "_configure_formal_runtime", lambda *_args: (backend, object()))
    monkeypatch.setattr(
        formal_execution, "verify_predecessor_carry",
        lambda *_args, **_kwargs: {"manifest_sha256": authorization["predecessor_carry_manifest_sha256"]},
    )
    monkeypatch.setattr(
        formal_execution, "_predecessor_training_group",
        lambda *_args, **_kwargs: {
            "status": "SUCCEEDED", "completed": 128,
            "receipt_sha256": formal_execution.PREDECESSOR_GROUP_RECEIPT_SHA256,
            "training_data_package_commit": formal_execution.PREDECESSOR_PACKAGE_COMMIT,
            "predecessor_carry_manifest_sha256": authorization["predecessor_carry_manifest_sha256"],
        },
    )
    monkeypatch.setattr(
        formal_execution, "_resumed_b01_training_group",
        lambda *_args, **_kwargs: {
            "status": "SUCCEEDED", "completed": 128,
            "receipt_sha256": formal_execution.RESUMED_B01_GROUP_RECEIPT_SHA256,
            "training_data_package_commit": formal_execution.RESUMED_GAMES_PACKAGE_COMMIT,
            "predecessor_carry_manifest_sha256": authorization["predecessor_carry_manifest_sha256"],
        },
    )
    monkeypatch.setattr(
        formal_execution, "_training_rows",
        lambda _seed, arm, block: [
            {"owner_seed": 8400001 + block * 128 + index, "unit_id": f"TRAIN:{8400001 + block * 128 + index}:{arm}"}
            for index in range(128)
        ],
    )
    monkeypatch.setattr(formal_execution, "_atomic_json", lambda *_args: None)
    training_game_calls: list[tuple[str, int]] = []

    def run_training_games(_formal, _backend, *, arm, block, **_kwargs):
        training_game_calls.append((arm, block))
        return {"status": "SUCCEEDED", "completed": 128}

    monkeypatch.setattr(
        formal_execution, "_run_training_games", run_training_games,
    )
    monkeypatch.setattr(
        formal_execution, "_read_update_receipt",
        lambda _formal, _root, _identity, *, arm, block: {
            "result_model_digest": model_digests[arm],
        },
    )
    test_worker_calls = []

    def run_stage(_formal, _backend, *, phase, group, **_kwargs):
        if phase == "test":
            test_worker_calls.append(group)
            raise AssertionError("TEST worker was reached with a damaged TEST-open receipt")
        return validation_groups[group]

    monkeypatch.setattr(formal_execution, "_run_stage", run_stage)

    with pytest.raises(RuntimeError, match="existing TEST-open receipt differs"):
        formal_execution.execute_formal(
            formal, issue84_source=issue84_source, native_source=issue84_source,
            stsr_source=issue84_source, authorization_receipt=tmp_path / "authorization.json",
        )

    assert test_worker_calls == []
    assert ("B", 0) not in training_game_calls
    assert ("B", 1) not in training_game_calls
    assert not (package_root / "test").exists()

def _formal_attempt_fixture(tmp_path: Path, *, terminal: bool = True):
    root = tmp_path / "validation" / "A"
    row = {
        "unit_id": "VALIDATION:8510001:A",
        "phase": "evaluation",
        "owner_seed": 8510001,
        "arm": "A",
        "game_seed": 9510001,
        "paired_owner_key": "owner-8510001",
    }
    package_commit = "c" * 40
    lock_sha = "l" * 64
    seed_sha = "s" * 64
    authorization_sha = "a" * 64
    attempt = root / "units" / "unit-8510001" / "attempt-0001"
    attempt.mkdir(parents=True)
    run_id = f"{row['unit_id']}-attempt-0001"
    result_path = attempt / "result.json"
    job = {
        "schema_id": "issue79-owner-worker-job-v1",
        "pilot": False,
        "formal": True,
        "unit_id": row["unit_id"],
        "run_id": run_id,
        "row": row,
        "phase": row["phase"],
        "package_commit": package_commit,
        "runner_code_commit": package_commit,
        "source_lock_sha256": lock_sha,
        "seed_table_sha256": seed_sha,
        "authorization_receipt_sha256": authorization_sha,
        "artifact_root": str(root),
        "attempt_dir": str(attempt),
        "result_path": str(result_path),
        "output_dir": str(attempt / "native"),
    }
    (attempt / "worker-job.json").write_text(json.dumps(job), encoding="utf-8")
    row_sha = "r" * 64
    started = {
        "schema_id": "issue79-owner-attempt-start-v1",
        "unit_id": row["unit_id"],
        "run_id": run_id,
        "owner_seed": row["owner_seed"],
        "arm": row["arm"],
        "package_commit": package_commit,
        "seed_row_sha256": row_sha,
        "pid": 639,
    }
    (attempt / "started.json").write_text(json.dumps(started), encoding="utf-8")
    result = {
        "schema_id": "issue75-stage1-unit-result-v1",
        "status": "SUCCEEDED",
        "formal": True,
        "unit_id": row["unit_id"],
        "phase": row["phase"],
        "arm": row["arm"],
        "owner_seed": row["owner_seed"],
        "game_seed": row["game_seed"],
        "paired_owner_key": row["paired_owner_key"],
        "package_commit": package_commit,
        "runner_code_commit": package_commit,
        "source_lock_sha256": lock_sha,
        "native_source_commit": formal_runner.LOCK["native"]["commit"],
        "stsr_source_commit": formal_runner.LOCK["stsr_source"]["commit"],
        "native_module_sha256": formal_runner.LOCK["native"]["compiled_module_sha256"],
        "resource": {"ledger_run_id": run_id},
    }
    if terminal:
        result_path.write_text(json.dumps(result), encoding="utf-8")
        finished = {
            "schema_id": "issue79-owner-attempt-finish-v1",
            "unit_id": row["unit_id"],
            "run_id": run_id,
            "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            "status": "SUCCEEDED",
        }
        (attempt / "finished.json").write_text(json.dumps(finished), encoding="utf-8")
    formal = SimpleNamespace(
        read_json=lambda path: json.loads(Path(path).read_text(encoding="utf-8")),
        file_sha256=lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        LOCK=formal_runner.LOCK,
    )
    backend = SimpleNamespace(unit_key=lambda _unit_id: "unit-8510001")
    return formal, backend, root, row, package_commit, lock_sha, seed_sha, authorization_sha, result, row_sha


def test_formal_attempt_does_not_probe_reused_pid_after_terminal_success(tmp_path: Path, monkeypatch) -> None:
    fixture = _formal_attempt_fixture(tmp_path)
    formal, backend, root, row, package, lock, seed, authorization, result, row_sha = fixture
    monkeypatch.setattr(formal_execution, "_hash_json", lambda _formal, _row: row_sha)

    def reused_pid_is_not_owned(_pid: int, _signal: int) -> None:
        raise PermissionError("PID has been reused by another process")

    monkeypatch.setattr(formal_execution.os, "kill", reused_pid_is_not_owned)
    success, historical = formal_execution._unit_attempts(
        formal, backend, root=root, row=row, package_commit=package, lock_sha=lock,
        seed_table_sha256=seed, authorization_receipt_sha256=authorization,
    )

    assert success == result
    assert historical == []


def test_formal_attempt_still_refuses_a_started_worker_without_terminal_receipt(
    tmp_path: Path, monkeypatch,
) -> None:
    fixture = _formal_attempt_fixture(tmp_path, terminal=False)
    formal, backend, root, row, package, lock, seed, authorization, _result, row_sha = fixture
    monkeypatch.setattr(formal_execution, "_hash_json", lambda _formal, _row: row_sha)
    live_pids: list[tuple[int, int]] = []

    def worker_is_live(pid: int, signal: int) -> None:
        live_pids.append((pid, signal))

    monkeypatch.setattr(formal_execution.os, "kill", worker_is_live)
    with pytest.raises(RuntimeError, match="formal worker 639 is still alive"):
        formal_execution._unit_attempts(
            formal, backend, root=root, row=row, package_commit=package, lock_sha=lock,
            seed_table_sha256=seed, authorization_receipt_sha256=authorization,
        )
    assert live_pids == [(639, 0)]
