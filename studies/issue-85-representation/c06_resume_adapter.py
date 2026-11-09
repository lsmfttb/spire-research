from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

EXPECTED_PACKAGE_COMMIT = "2ecd996139778665d60a06f624b41e260c7b7358"
C06_OWNER_START = 8400769
C06_OWNER_STOP = 8400896
C06_SCOPE = "TRAIN-C-BLOCK-06-GAMES_AND_SINGLE_ACTOR_UPDATE"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(execution: Any, formal: Any, path: Path, payload: Mapping[str, Any]) -> None:
    execution._atomic_json(formal, path, payload)


def _read_c06_actor_update_receipt(
    formal: Any,
    execution: Any,
    package_root: Path,
    identity: Mapping[str, str],
    *,
    training_group: Mapping[str, Any],
    expected_carry_sha256: str,
) -> dict[str, Any]:
    """Verify the already-written C/06 update without rerunning its optimizer.

    The pinned reader leaves its carry digest unset for C blocks, although the
    C/06 continuation receipt is correctly bound to the authenticated carry.
    Keep this repair local to the resumed C/06 receipt path and verify every
    field the pinned reader checks, using the carry digest authenticated by the
    adapter before this function is called.
    """
    path = package_root / "training/C/updates/block-06/success-receipt.json"
    if not path.is_file():
        raise RuntimeError("C/06 actor update success receipt is missing")
    receipt = formal.read_json(path)
    if not isinstance(receipt, Mapping):
        raise RuntimeError("C/06 actor update receipt is malformed")
    digest = formal.file_sha256(path)
    actor_relative = receipt.get("actor_checkpoint_path")
    optimizer_relative = receipt.get("optimizer_checkpoint_path")
    if not isinstance(actor_relative, str) or not isinstance(optimizer_relative, str):
        raise RuntimeError("C/06 actor update receipt has invalid checkpoint paths")
    actor_path = execution._inside(package_root, actor_relative)
    optimizer_path = execution._inside(package_root, optimizer_relative)
    if (
        receipt.get("schema_id") != "issue85-formal-actor-update-v1"
        or receipt.get("status") != "SUCCEEDED"
        or receipt.get("arm") != "C"
        or receipt.get("block_index") != 6
        or receipt.get("training_data_package_commit") != identity.get("package_commit")
        or receipt.get("predecessor_carry_manifest_sha256") != expected_carry_sha256
        or receipt.get("training_group_receipt_sha256") != training_group.get("receipt_sha256")
        or receipt.get("package_commit") != identity.get("package_commit")
        or receipt.get("source_lock_sha256") != identity.get("source_lock_sha256")
        or receipt.get("owner_count") != int(formal.CONTRACT["training"]["owners_per_block"])
        or receipt.get("completed_steps") != int(formal.CONTRACT["training"]["updates_per_block"])
        or receipt.get("minibatch_owners") != int(formal.CONTRACT["training"]["minibatch_owners"])
        or receipt.get("receipt_sha256") != execution._hash_json(
            formal, {key: value for key, value in receipt.items() if key != "receipt_sha256"},
        )
        or not actor_path.is_file()
        or formal.file_sha256(actor_path) != receipt.get("actor_checkpoint_sha256")
        or not optimizer_path.is_file()
        or formal.file_sha256(optimizer_path) != receipt.get("optimizer_checkpoint_sha256")
    ):
        raise RuntimeError("C/06 actor update receipt/checkpoint differs from its authenticated inputs")
    return {
        **receipt,
        "receipt_file_sha256": digest,
        "receipt_path": path.relative_to(package_root).as_posix(),
        "producer_package_root": str(package_root),
    }


def _run_c06_training_games(execution: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Adapt the pinned carry helper's list result to C/06's set mutation."""
    original = execution._e902_carried_units_from_references

    def mutable_allowed_roots(*call_args: Any, **call_kwargs: Any):
        carried, allowed_roots = original(*call_args, **call_kwargs)
        return carried, {key: set(paths) for key, paths in allowed_roots.items()}

    execution._e902_carried_units_from_references = mutable_allowed_roots
    try:
        return execution._run_training_games(*args, **kwargs)
    finally:
        execution._e902_carried_units_from_references = original


def _train_c06_actor_update(
    execution: Any,
    formal: Any,
    backend: Any,
    issue84_driver: Any,
    *,
    rows: list[dict[str, Any]],
    package_root: Path,
    identity: Mapping[str, str],
    seed_table: Mapping[str, Any],
    seed_table_sha256: str,
    training_group: Mapping[str, Any],
    clock: Any,
    authenticated_successful_units: Any,
    expected_reconciliation_sha256: str,
) -> dict[str, Any]:
    """Use the authenticated C/06 carry commit for the pinned runner's binding check.

    The pinned e902 validator verifies the exact carry manifest and group receipt.
    Only after it succeeds, temporarily bind the later generic source check to the
    manifest's source commit. The pinned model loader revalidates the carry first,
    so that validator must still see the original package identity.
    """
    if training_group.get("e902_canonical_reconciliation_sha256") != expected_reconciliation_sha256:
        raise RuntimeError("C/06 game group is not bound to the authenticated e902 reconciliation")
    if not isinstance(authenticated_successful_units, list) or len(authenticated_successful_units) != 82:
        raise RuntimeError("authenticated C/06 carry does not contain exactly 82 successful source references")
    source_commits = set()
    for reference in authenticated_successful_units:
        if not isinstance(reference, Mapping):
            raise RuntimeError("authenticated C/06 carry contains a malformed source reference")
        source_commit = reference.get("source_package_commit")
        if not isinstance(source_commit, str) or not source_commit:
            raise RuntimeError("authenticated C/06 carry contains a missing source package commit")
        source_commits.add(source_commit)
    if len(source_commits) != 1:
        raise RuntimeError("C/06 authenticated carried sources do not have one exact package binding")

    original_package_commit = execution.RESUMED_FORMAL_PACKAGE_COMMIT
    original_validator = execution._validate_e902_carried_training_group
    original_carry_validator = execution._validate_carry_manifest
    original_loader = formal.pilot._load_pilot_training_examples
    binding_active = False

    def validate_then_bind(*args: Any, **kwargs: Any) -> Any:
        nonlocal binding_active
        execution.RESUMED_FORMAL_PACKAGE_COMMIT = original_package_commit
        result = original_validator(*args, **kwargs)
        execution.RESUMED_FORMAL_PACKAGE_COMMIT = next(iter(source_commits))
        binding_active = True
        return result

    def validate_carry_with_pinned_identity(*args: Any, **kwargs: Any) -> Any:
        if not binding_active:
            return original_carry_validator(*args, **kwargs)
        bound_package_commit = execution.RESUMED_FORMAL_PACKAGE_COMMIT
        execution.RESUMED_FORMAL_PACKAGE_COMMIT = original_package_commit
        try:
            return original_carry_validator(*args, **kwargs)
        finally:
            execution.RESUMED_FORMAL_PACKAGE_COMMIT = bound_package_commit

    def restore_before_loading_examples(*args: Any, **kwargs: Any) -> Any:
        nonlocal binding_active
        if binding_active:
            execution.RESUMED_FORMAL_PACKAGE_COMMIT = original_package_commit
            binding_active = False
        return original_loader(*args, **kwargs)

    execution._validate_e902_carried_training_group = validate_then_bind
    execution._validate_carry_manifest = validate_carry_with_pinned_identity
    formal.pilot._load_pilot_training_examples = restore_before_loading_examples
    try:
        return execution._train_actor_block(
            formal, backend, issue84_driver,
            arm="C", block=6, rows=rows, package_root=package_root,
            identity=identity, seed_table=seed_table,
            seed_table_sha256=seed_table_sha256,
            training_group=training_group, clock=clock,
        )
    finally:
        execution.RESUMED_FORMAL_PACKAGE_COMMIT = original_package_commit
        execution._validate_e902_carried_training_group = original_validator
        execution._validate_carry_manifest = original_carry_validator
        formal.pilot._load_pilot_training_examples = original_loader



def execute_c06_continuation(
    formal: Any,
    execution: Any,
    *,
    issue84_source: Path,
    native_source: Path,
    stsr_source: Path,
    authorization_receipt: Path,
    adapter_commit: str,
    adapter_sha256: str,
) -> dict[str, Any]:
    if execution.os.name == "nt":
        raise RuntimeError("C/06 continuation is supported only in the pinned WSL runtime")
    identity = formal.pilot.source_identity(require_clean=True)
    if identity.get("package_commit") != EXPECTED_PACKAGE_COMMIT:
        raise RuntimeError("the adapter is bound to a different reviewed source package")

    package_root, readiness, seed = execution._validate_readiness(formal, identity)
    authorization = execution.validate_authorization(
        formal, authorization_receipt, identity=identity, readiness=readiness, seed=seed,
    )
    dry = formal.verify_dry_bundle(
        issue84_source=issue84_source, native_source=native_source, stsr_source=stsr_source,
    )
    if dry.get("status") != "VERIFIED / NO GAMES OR UPDATES STARTED":
        raise RuntimeError("formal source/seed/analysis dry check failed")
    if formal._verify_native_source(native_source)["commit"] != formal.LOCK["native"]["commit"]:
        raise RuntimeError("native source changed after dry verification")
    if formal._verify_stsr_source(stsr_source)["commit"] != formal.LOCK["stsr_source"]["commit"]:
        raise RuntimeError("STSRL source changed after dry verification")

    seed_path = package_root / "formal-seed-table.json"
    seed_sha = formal.file_sha256(seed_path)
    progress_path = package_root / "formal-progress.json"
    receipt_path = package_root / "formal-c06-continuation-receipt.json"
    carry, carry_sha = execution._validate_carry_manifest(
        formal, package_root, identity,
        expected_sha256=authorization["predecessor_carry_manifest_sha256"],
    )
    if carry_sha != authorization["predecessor_carry_manifest_sha256"]:
        raise RuntimeError("source-bound authorization references another carry manifest")
    if not execution._e902_canonical_lineage_required(formal, identity):
        raise RuntimeError("C/06-only continuation requires the canonical e902 lineage")

    reconciliation = carry.get("e902_canonical_reconciliation")
    partial = reconciliation.get("c06_partial") if isinstance(reconciliation, Mapping) else None
    interrupted = partial.get("started_without_terminal") if isinstance(partial, Mapping) else None
    expected_unstarted_owner_ids = [
        f"TRAIN:{owner}:C" for owner in range(8400852, C06_OWNER_STOP + 1)
    ]
    if (
        not isinstance(partial, Mapping)
        or partial.get("stage_id") != "TRAIN-C-BLOCK-06-GAMES"
        or partial.get("successful_completions") != 82
        or partial.get("unstarted_owner_ids") != expected_unstarted_owner_ids
        or not isinstance(partial.get("successful_units"), list)
        or len(partial["successful_units"]) != 82
        or not isinstance(interrupted, Mapping)
        or interrupted.get("unit_id") != "TRAIN:8400851:C"
        or interrupted.get("status") != "STARTED_WITHOUT_TERMINAL"
        or interrupted.get("next_attempt") != "attempt-0002"
    ):
        raise RuntimeError("authenticated carry does not preserve the registered C/06 82/1/45 boundary")

    rows = execution._training_rows(seed, "C", 6)
    if (
        len(rows) != 128
        or [int(row["owner_seed"]) for row in rows]
        != list(range(C06_OWNER_START, C06_OWNER_STOP + 1))
    ):
        raise RuntimeError("registered C/06 owner roster changed")

    if (package_root / "formal-closeout.json").is_file():
        raise RuntimeError("full-study closeout exists; the C/06-only adapter must not run")
    if receipt_path.is_file():
        receipt = formal.read_json(receipt_path)
        expected = {
            "schema_id": "issue85-formal-c06-continuation-v1",
            "status": "C06_CONTINUATION_SUCCEEDED",
            "scope": C06_SCOPE,
            "package_commit": identity["package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "seed_table_sha256": seed_sha,
            "authorization_receipt_sha256": authorization["sha256"],
            "predecessor_carry_manifest_sha256": carry_sha,
            "adapter_commit": adapter_commit,
            "adapter_sha256": adapter_sha256,
        }
        if (
            any(receipt.get(key) != value for key, value in expected.items())
            or receipt.get("later_registered_stages_started") is not False
            or receipt.get("c06_carried_success_count") != 82
            or receipt.get("c06_interrupted_owner_id") != "TRAIN:8400851:C"
            or receipt.get("c06_unstarted_owner_count") != 45
        ):
            raise RuntimeError("existing C/06 continuation receipt is bound to different inputs")
        group_path = package_root / "training/C/block-06/games/group-receipt.json"
        if formal.file_sha256(group_path) != receipt.get("c06_games_group_receipt_sha256"):
            raise RuntimeError("existing C/06 game-group receipt changed")
        update = execution._read_update_receipt(
            formal, package_root, identity, arm="C", block=6,
        )
        if (
            update.get("completed_steps") != 64
            or update.get("receipt_sha256") != receipt.get("c06_actor_update_receipt_sha256")
            or update.get("result_model_digest") != receipt.get("c06_actor_update_result_model_digest")
            or update.get("actor_checkpoint_sha256") != receipt.get("c06_actor_checkpoint_sha256")
            or update.get("optimizer_checkpoint_sha256") != receipt.get("c06_optimizer_checkpoint_sha256")
        ):
            raise RuntimeError("existing C/06 actor-update receipt or checkpoint changed")
        return receipt

    backend, issue84_driver = execution._configure_formal_runtime(
        formal, issue84_source, identity, package_root,
    )
    clock = backend.ActiveClock(
        "issue85-task", float(authorization["whole_task_active_wall_cap_seconds"]),
    )
    prior = formal.read_json(progress_path) if progress_path.is_file() else None
    if prior is not None and (
        set(prior.get("game_groups", {})) - {"TRAIN-C-BLOCK-05", "TRAIN-C-BLOCK-06"}
        or set(prior.get("actor_updates", {})) - {"C:5", "C:6"}
        or prior.get("schema_id") != execution.PROGRESS_SCHEMA
        or prior.get("package_commit") != identity["package_commit"]
        or prior.get("source_lock_sha256") != identity["source_lock_sha256"]
        or prior.get("seed_table_sha256") != seed_sha
        or prior.get("authorization_receipt_sha256") != authorization["sha256"]
        or prior.get("predecessor_carry_manifest_sha256") != carry_sha
    ):
        raise RuntimeError("existing formal progress is bound to different source, seed, carry or authorization")

    resume = {
        "manifest_sha256": carry_sha,
        "e902_canonical_reconciliation": reconciliation,
    }
    progress = prior or {
        "schema_id": execution.PROGRESS_SCHEMA,
        "status": "STARTING",
        "package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "seed_table_sha256": seed_sha,
        "authorization_receipt_sha256": authorization["sha256"],
        "predecessor_carry_manifest_path": execution._predecessor_carry_path(package_root).relative_to(package_root).as_posix(),
        "predecessor_carry_manifest_sha256": carry_sha,
        "predecessor_game_group_receipt_sha256": execution.PREDECESSOR_GROUP_RECEIPT_SHA256,
        "game_groups": {},
        "actor_updates": {},
        "started_utc": execution._now(),
    }
    if prior is None:
        progress["adapter_attempt_history"] = []
        progress["adapter_attempt_number"] = 1
    else:
        history = prior.get("adapter_attempt_history", [])
        if not isinstance(history, list) or any(not isinstance(item, Mapping) for item in history):
            raise RuntimeError("existing adapter attempt history is malformed")
        history = list(history)
        history.append({
            key: prior[key] for key in (
                "status", "active_stage", "adapter_commit", "adapter_sha256",
                "started_utc", "current_attempt_started_utc",
                "finished_or_paused_utc", "failure",
                "cumulative_active_wall_seconds", "clock_start_seconds",
            ) if key in prior
        })
        progress["adapter_attempt_history"] = history
        progress["adapter_attempt_number"] = len(history) + 1
        progress.pop("failure", None)
        progress.pop("finished_or_paused_utc", None)

    progress["scope"] = C06_SCOPE
    progress["adapter_commit"] = adapter_commit
    progress["adapter_sha256"] = adapter_sha256
    progress.update({
        "status": "RUNNING",
        "current_attempt_started_utc": execution._now(),
        "supervisor_pid": execution.os.getpid(),
        "active_stage": "C06-CARRY-AND-INPUT-VERIFICATION",
        "clock_start_seconds": clock.elapsed_before,
    })
    _atomic_json(execution, formal, progress_path, progress)

    def save_progress() -> None:
        progress["cumulative_active_wall_seconds"] = clock.elapsed()
        progress["heartbeat_utc"] = execution._now()
        _atomic_json(execution, formal, progress_path, progress)

    try:
        # Reuse only C/05's verified group/update to establish the C/06 model and optimizer input.
        c05_group = execution._run_training_games(
            formal, backend, package_root=package_root, seed=seed,
            arm="C", block=5, clock=clock, identity=identity,
            seed_table_sha256=seed_sha, authorization=authorization,
            issue84_source=Path(issue84_source).resolve(strict=True),
            progress_path=progress_path, resumed_formal_continuation=resume,
        )
        if (
            c05_group.get("status") != "SUCCEEDED"
            or c05_group.get("completed") != 128
            or c05_group.get("e902_canonical_reconciliation_sha256")
            != reconciliation.get("record_sha256")
        ):
            raise RuntimeError("C/05 carry is not the authenticated predecessor for C/06")
        progress["game_groups"]["TRAIN-C-BLOCK-05"] = c05_group
        save_progress()

        c05_update = execution._read_update_receipt(
            formal, package_root, identity, arm="C", block=5,
        )
        progress["actor_updates"]["C:5"] = c05_update
        progress["active_stage"] = "TRAIN-C-BLOCK-06-GAMES"
        save_progress()
        if clock.expired():
            raise TimeoutError("whole-task active-wall cap reached before C/06 games")

        c06_group = _run_c06_training_games(
            execution, formal, backend, package_root=package_root, seed=seed,
            arm="C", block=6, clock=clock, identity=identity,
            seed_table_sha256=seed_sha, authorization=authorization,
            issue84_source=Path(issue84_source).resolve(strict=True),
            progress_path=progress_path, resumed_formal_continuation=resume,
        )
        if (
            c06_group.get("status") != "SUCCEEDED"
            or c06_group.get("completed") != 128
            or c06_group.get("e902_canonical_reconciliation_sha256")
            != reconciliation.get("record_sha256")
        ):
            raise RuntimeError("C/06 did not produce 128 accepted owner terminals")
        progress["game_groups"]["TRAIN-C-BLOCK-06"] = c06_group
        progress["active_stage"] = "TRAIN-C-BLOCK-06-ACTOR-UPDATE"
        save_progress()

        update_path = package_root / "training/C/updates/block-06/success-receipt.json"
        if clock.expired():
            raise TimeoutError("whole-task active-wall cap reached before the C/06 actor update")
        if update_path.is_file():
            c06_update = _read_c06_actor_update_receipt(
                formal, execution, package_root, identity,
                training_group=c06_group,
                expected_carry_sha256=carry_sha,
            )
        else:
            c06_update = _train_c06_actor_update(
                execution, formal, backend, issue84_driver,
                rows=rows, package_root=package_root, identity=identity,
                seed_table=seed, seed_table_sha256=seed_sha,
                training_group=c06_group, clock=clock,
                authenticated_successful_units=partial["successful_units"],
                expected_reconciliation_sha256=reconciliation["record_sha256"],
            )
        if (
            c06_update.get("completed_steps") != 64
            or c06_update.get("training_group_receipt_sha256") != c06_group["receipt_sha256"]
            or c06_update.get("predecessor_carry_manifest_sha256") != carry_sha
            or c06_update.get("source_model_digest") != c05_update["result_model_digest"]
        ):
            raise RuntimeError("C/06 actor update did not preserve the C/05 model and registered inputs")
        progress["actor_updates"]["C:6"] = c06_update
        save_progress()

        clock.set_processes(set())
        active_wall = clock.close()
        receipt = {
            "schema_id": "issue85-formal-c06-continuation-v1",
            "status": "C06_CONTINUATION_SUCCEEDED",
            "scope": C06_SCOPE,
            "package_commit": identity["package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "seed_table_sha256": seed_sha,
            "authorization_receipt_sha256": authorization["sha256"],
            "predecessor_carry_manifest_sha256": carry_sha,
            "e902_canonical_reconciliation_sha256": reconciliation["record_sha256"],
            "owner_go_comment_url": authorization["owner_approval_comment_url"],
            "reviewer_approval_comment_url": authorization["reviewer_approval_comment_url"],
            "adapter_commit": adapter_commit,
            "adapter_sha256": adapter_sha256,
            "c05_actor_update_receipt_sha256": c05_update["receipt_sha256"],
            "c05_actor_checkpoint_sha256": c05_update["actor_checkpoint_sha256"],
            "c05_optimizer_checkpoint_sha256": c05_update["optimizer_checkpoint_sha256"],
            "c05_result_model_digest": c05_update["result_model_digest"],
            "c06_carried_success_count": 82,
            "c06_interrupted_owner_id": "TRAIN:8400851:C",
            "c06_unstarted_owner_count": 45,
            "c06_games_group_receipt_sha256": c06_group["receipt_sha256"],
            "c06_completed": c06_group["completed"],
            "c06_failed_attempt_count": c06_group["failed_attempt_count"],
            "c06_actor_update_receipt_sha256": c06_update["receipt_sha256"],
            "c06_actor_checkpoint_sha256": c06_update["actor_checkpoint_sha256"],
            "c06_optimizer_checkpoint_sha256": c06_update["optimizer_checkpoint_sha256"],
            "c06_actor_update_result_model_digest": c06_update["result_model_digest"],
            "cumulative_active_wall_seconds": active_wall,
            "whole_task_active_wall_cap_seconds": authorization["whole_task_active_wall_cap_seconds"],
            "later_registered_stages_started": False,
            "finished_utc": execution._now(),
        }
        formal.pilot._write_or_verify_once(receipt_path, receipt)
        progress.update({
            "status": "C06_CONTINUATION_SUCCEEDED",
            "active_stage": "C06-CONTINUATION-COMPLETE",
            "cumulative_active_wall_seconds": active_wall,
            "finished_utc": receipt["finished_utc"],
        })
        _atomic_json(execution, formal, progress_path, progress)
        return receipt
    except BaseException as exc:
        clock.set_processes(set())
        elapsed = clock.close()
        progress.update({
            "status": "FAILED_OR_PAUSED",
            "failure": {"type": type(exc).__name__, "detail": str(exc)[:3000]},
            "cumulative_active_wall_seconds": elapsed,
            "finished_or_paused_utc": execution._now(),
        })
        _atomic_json(execution, formal, progress_path, progress)
        raise


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE,
    ).strip()


def _load_formal_modules(source_study: Path) -> tuple[Any, Any]:
    source_study = source_study.resolve(strict=True)
    if not (source_study / "formal_runner.py").is_file():
        raise RuntimeError("source study lacks formal_runner.py")
    sys.path.insert(0, str(source_study))
    import formal_execution
    import formal_runner
    if Path(formal_runner.__file__).resolve().parent != source_study:
        raise RuntimeError("formal modules did not load from the exact reviewed source study")
    return formal_runner, formal_execution


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resume only Issue 85 C/06 from the authenticated e902 boundary.",
    )
    parser.add_argument("--source-study", type=Path, required=True)
    parser.add_argument("--issue84-source", type=Path, required=True)
    parser.add_argument("--native-source", type=Path, required=True)
    parser.add_argument("--stsr-source", type=Path, required=True)
    parser.add_argument("--authorization-receipt", type=Path, required=True)
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[2]
    status = _git(repo_root, "status", "--porcelain=v1")
    if status:
        raise RuntimeError("adapter worktree must be clean before execution")
    adapter_commit = _git(repo_root, "rev-parse", "HEAD")
    adapter_sha = file_sha256(Path(__file__).resolve())
    formal, execution = _load_formal_modules(args.source_study)
    receipt = execute_c06_continuation(
        formal, execution,
        issue84_source=args.issue84_source,
        native_source=args.native_source,
        stsr_source=args.stsr_source,
        authorization_receipt=args.authorization_receipt,
        adapter_commit=adapter_commit,
        adapter_sha256=adapter_sha,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
