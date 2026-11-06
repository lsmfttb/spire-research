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
        DATA_ROOT_WSL="/mnt/d/DeadlyCatCoding/stsrl-data",
        LOCK=lock,
        NEW_ARMS=("B", "C", "D", "E", "F"),
        POLICIES=("PARENT", "A", "B", "C", "D", "E", "F"),
    )
    representations = ModuleType("representations")
    representations.Issue85Arm = type("Issue85Arm", (), {})
    monkeypatch.setitem(sys.modules, "representations", representations)

    configured, _ = formal_execution._configure_formal_runtime(
        formal, tmp_path / "issue84", {"package_commit": "c" * 40}, tmp_path / "package",
    )
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
        formal_plan=lambda: {"training_blocks_per_arm": 1},
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
        update_receipt = package_root / "training" / arm / "updates" / "block-00" / "success-receipt.json"
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
    monkeypatch.setattr(formal_execution, "_atomic_json", lambda *_args: None)
    monkeypatch.setattr(
        formal_execution, "_run_training_games",
        lambda *_args, **_kwargs: {"status": "SUCCEEDED", "completed": 128},
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
    assert not (package_root / "test").exists()
