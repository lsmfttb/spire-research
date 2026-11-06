from __future__ import annotations

from contextvars import ContextVar
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
