"""Production-shaped tests for the by-reference carry index.

The index is the only thing standing between "the study continues" and "the
study silently replays fifteen completed blocks", so every property asserted
here matters: producers are named by real commits, content is re-hashed from
bytes, an excluded duplicate is never referenced, and any tampering fails
closed.

No game, optimizer step, or artifact write occurs here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

STUDY = Path(__file__).resolve().parents[1]
if str(STUDY) not in sys.path:
    sys.path.insert(0, str(STUDY))

import by_reference_carry as brc  # noqa: E402
import formal_runner as formal  # noqa: E402

EXECUTING_COMMIT = "a" * 40
LOCK = "b" * 64
IDENTITY = {"package_commit": EXECUTING_COMMIT, "source_lock_sha256": LOCK}


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _stage_packages(tmp_path: Path) -> Path:
    """Create synthetic producer packages for every audited stage."""
    root = tmp_path / "issue85" / "artifacts" / "formal"
    for index, (arm, block, games_commit, update_commit) in enumerate(brc.STAGES):
        games_dir = root / games_commit / "training" / arm / f"block-{block:02d}" / "games"
        games_dir.mkdir(parents=True, exist_ok=True)
        group = {
            "status": "SUCCEEDED",
            "completed": 128,
            "expected": 128,
            "successful_unit_ids": [f"TRAIN:{8400001 + block * 128 + i}:{arm}" for i in range(128)],
        }
        group_path = games_dir / "group-receipt.json"
        group_path.write_text(json.dumps(group), encoding="utf-8")

        update_dir = root / update_commit / "training" / arm / "updates" / f"block-{block:02d}"
        update_dir.mkdir(parents=True, exist_ok=True)
        actor = f"actor-{arm}-{block}".encode()
        optimizer = f"optimizer-{arm}-{block}".encode()
        (update_dir / "actor.pt").write_bytes(actor)
        (update_dir / "optimizer.pt").write_bytes(optimizer)
        update = {
            "status": "SUCCEEDED",
            "arm": arm,
            "block_index": block,
            "package_commit": update_commit,
            "owner_count": 128,
            "training_group_receipt_sha256": _sha(group_path.read_bytes()),
            # Real receipts record these package-root-relative, not update-dir-relative.
            "actor_checkpoint_path": f"training/{arm}/updates/block-{block:02d}/actor.pt",
            "actor_checkpoint_sha256": _sha(actor),
            "optimizer_checkpoint_path": f"training/{arm}/updates/block-{block:02d}/optimizer.pt",
            "optimizer_checkpoint_sha256": _sha(optimizer),
            "receipt_sha256": f"{index:064x}",
            "predecessor_carry_manifest_sha256": "c" * 64,
            "source_model_digest": f"d{index:063x}",
            "result_model_digest": f"e{index:063x}",
        }
        (update_dir / "success-receipt.json").write_text(json.dumps(update), encoding="utf-8")
    return root


@pytest.fixture()
def store(tmp_path, monkeypatch):
    _stage_packages(tmp_path)
    artifacts = tmp_path / "issue85" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "active-wall-issue85-task.json").write_text(
        json.dumps({"elapsed_seconds": 75785.56550202602, "allowance_seconds": 295632}),
        encoding="utf-8",
    )
    monkeypatch.setattr(formal, "DATA_ROOT", str(tmp_path))
    return tmp_path


def test_index_covers_the_fifteen_completed_stages(store):
    index = brc.build_index(formal, IDENTITY)
    assert index["completed_stage_count"] == 15
    assert index["completed_train_outcomes"] == 1920
    assert (index["first_unfinished_arm"], index["first_unfinished_block"]) == ("C", 7)
    assert index["executing_package_commit"] == EXECUTING_COMMIT
    assert index["excluded_duplicate_package"] == brc.EXCLUDED_DUPLICATE_PACKAGE
    assert brc.EXCLUDED_DUPLICATE_PACKAGE not in index["producers"]


def test_c06_and_b07_are_named_as_policy_input_producers(store):
    index = brc.build_index(formal, IDENTITY)
    bindings = brc.policy_producers(formal, index, IDENTITY)
    assert set(bindings["producers"]) == {"C:6", "B:7"}
    assert bindings["producers"]["C:6"]["package_commit"] == "2ecd996139778665d60a06f624b41e260c7b7358"
    assert bindings["producers"]["C:6"]["predecessor_carry_manifest_sha256"] == "c" * 64
    assert bindings["executing_package_commit"] == EXECUTING_COMMIT


def test_stage_rejects_incomplete_game_group(store):
    _, _, games_commit, _ = brc.STAGES[0]
    path = (store / "issue85/artifacts/formal" / games_commit
            / "training/B/block-00/games/group-receipt.json")
    group = json.loads(path.read_text())
    group["completed"] = 127
    path.write_text(json.dumps(group), encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a complete"):
        brc.build_index(formal, IDENTITY)


def test_stage_rejects_mutated_actor_bytes(store):
    _, _, _, update_commit = brc.STAGES[0]
    (store / "issue85/artifacts/formal" / update_commit
     / "training/B/updates/block-00/actor.pt").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="bytes differ"):
        brc.build_index(formal, IDENTITY)


def test_stage_rejects_group_digest_mismatch(store):
    _, _, _, update_commit = brc.STAGES[-1]
    path = (store / "issue85/artifacts/formal" / update_commit
            / "training/C/updates/block-06/success-receipt.json")
    update = json.loads(path.read_text())
    update["training_group_receipt_sha256"] = "f" * 64
    path.write_text(json.dumps(update), encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not name the verified"):
        brc.build_index(formal, IDENTITY)


def test_stage_rejects_foreign_package_commit(store):
    _, _, _, update_commit = brc.STAGES[0]
    path = (store / "issue85/artifacts/formal" / update_commit
            / "training/B/updates/block-00/success-receipt.json")
    update = json.loads(path.read_text())
    update["package_commit"] = brc.EXCLUDED_DUPLICATE_PACKAGE
    path.write_text(json.dumps(update), encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a complete"):
        brc.build_index(formal, IDENTITY)


def test_excluded_duplicate_package_cannot_carry_a_stage(store, monkeypatch):
    patched = tuple(
        (arm, block, g, brc.EXCLUDED_DUPLICATE_PACKAGE)
        for arm, block, g, _ in brc.STAGES
    )
    monkeypatch.setattr(brc, "STAGES", patched)
    _stage_packages(store)
    with pytest.raises((RuntimeError, FileNotFoundError)):
        brc.build_index(formal, IDENTITY)


def test_validate_index_round_trip_and_stale_identity(store):
    index = brc.build_index(formal, IDENTITY)
    package_root = store / "issue85/artifacts/formal" / EXECUTING_COMMIT
    package_root.mkdir(parents=True, exist_ok=True)
    path = package_root / "by-reference-carry-index.json"
    path.write_text(json.dumps(index), encoding="utf-8")

    assert brc.validate_index(formal, path, IDENTITY)["index_sha256"] == index["index_sha256"]

    other = {"package_commit": "9" * 40, "source_lock_sha256": LOCK}
    with pytest.raises(RuntimeError, match="bound to another executing package"):
        brc.validate_index(formal, path, other)


def test_validate_index_rejects_resigned_tampering(store):
    """Recomputing the self-hash must not rescue a mutated stage."""
    index = brc.build_index(formal, IDENTITY)
    index["stages"][0]["owner_first"] = 1
    unsigned = {k: v for k, v in index.items() if k != "index_sha256"}
    index["index_sha256"] = brc._hash_json(unsigned)
    package_root = store / "issue85/artifacts/formal" / EXECUTING_COMMIT
    package_root.mkdir(parents=True, exist_ok=True)
    path = package_root / "by-reference-carry-index.json"
    path.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(RuntimeError, match="disagrees with the live producer evidence"):
        brc.validate_index(formal, path, IDENTITY)


def test_index_binds_only_the_executing_commit(store):
    first = brc.build_index(formal, IDENTITY)
    other_identity = {"package_commit": "f" * 40, "source_lock_sha256": LOCK}
    second = brc.build_index(formal, other_identity)
    assert first["index_sha256"] != second["index_sha256"]
    assert second["executing_package_commit"] == "f" * 40


def test_stage_bindings_name_games_and_update_producers_separately(store):
    index = brc.build_index(formal, IDENTITY)
    bindings = brc.stage_bindings(index, IDENTITY)
    assert len(bindings) == 15
    assert set(bindings) == {f"{a}:{b}" for a, b, _, _ in brc.STAGES}
    # B/00's games were staged in e549105e but its update was applied in fde456f5.
    assert bindings["B:0"]["games_package_commit"] == "e549105ead79c9524bee706fc600164f8c7f9f1a"
    assert bindings["B:0"]["package_commit"] == "fde456f52aaa388b7abc92a2b148ba91d3720e1e"
    assert bindings["B:0"]["games_package_commit"] != bindings["B:0"]["package_commit"]
    # B/01 has the same asymmetry.
    assert bindings["B:1"]["games_package_commit"] == "318bdc800c5f986d67b445c38caeb13cebb154fb"
    assert bindings["B:1"]["package_commit"] == "b8b3fc043671498628f96d378d9cecd7ac54106f"


def test_policy_input_bindings_carry_the_games_producer_too(store):
    index = brc.build_index(formal, IDENTITY)
    producers = brc.policy_producers(formal, index, IDENTITY)["producers"]
    assert producers["C:6"]["games_package_commit"] == "2ecd996139778665d60a06f624b41e260c7b7358"


def test_index_binds_the_observed_wall_and_never_moves_it_backwards(store):
    first = brc.build_index(formal, IDENTITY)
    assert first["observed_active_wall_seconds"] == pytest.approx(75785.56550202602)

    clock = store / "issue85/artifacts/active-wall-issue85-task.json"
    clock.write_text(
        json.dumps({"elapsed_seconds": 80000.0, "allowance_seconds": 295632}), encoding="utf-8"
    )
    later = brc.build_index(formal, IDENTITY)
    assert later["observed_active_wall_seconds"] == pytest.approx(80000.0)
    # The wall is part of the index, so a refreshed index re-binds the higher reading
    # instead of letting a stale snapshot travel with the evidence.
    assert later["index_sha256"] != first["index_sha256"]


def test_index_refuses_a_missing_clock(store):
    (store / "issue85/artifacts/active-wall-issue85-task.json").unlink()
    with pytest.raises(RuntimeError, match="ledger is missing"):
        brc.build_index(formal, IDENTITY)


def _write_index(store: Path) -> dict:
    index = brc.build_index(formal, IDENTITY)
    root = store / "issue85" / "artifacts" / "formal" / EXECUTING_COMMIT
    root.mkdir(parents=True, exist_ok=True)
    (root / "by-reference-carry-index.json").write_text(json.dumps(index), encoding="utf-8")
    return index


def test_gate_helper_accepts_the_matching_index(store):
    import formal_execution as fe

    index = _write_index(store)
    root = store / "issue85" / "artifacts" / "formal" / EXECUTING_COMMIT
    got = fe._validate_by_reference_index(formal, root, IDENTITY, index["index_sha256"])
    assert got["index_sha256"] == index["index_sha256"]
    assert got["completed_stage_count"] == 15


def test_gate_helper_rejects_a_different_index_digest(store):
    import formal_execution as fe

    _write_index(store)
    root = store / "issue85" / "artifacts" / "formal" / EXECUTING_COMMIT
    with pytest.raises(RuntimeError, match="different by-reference carry index"):
        fe._validate_by_reference_index(formal, root, IDENTITY, "0" * 64)


def test_gate_helper_rejects_a_missing_index(store):
    import formal_execution as fe

    root = store / "issue85" / "artifacts" / "formal" / EXECUTING_COMMIT
    root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(RuntimeError, match="missing from the executing package"):
        fe._validate_by_reference_index(formal, root, IDENTITY, "0" * 64)


def test_successor_skips_b00_to_c06_and_dispatches_first_at_c07(store):
    """An empty successor package must not restart the study at B/00."""
    import formal_execution as fe

    index = brc.build_index(formal, IDENTITY)
    groups = fe._by_reference_carried_groups(index, 128)

    assert len(groups) == 15
    assert set(groups) == {(a, b) for a, b, _, _ in brc.STAGES}
    assert ("C", 6) in groups
    assert ("C", 7) not in groups
    assert all(record["status"] == "SUCCEEDED" for record in groups.values())
    # Carried records describe provenance, not results: no model digest is restated.
    assert all("result_model_digest" not in record for record in groups.values())

    # The executor walks arms in order and blocks 0..7; what remains after the
    # carried set is exactly the work the successor must actually produce.
    remaining = [
        (arm, block)
        for arm in ("B", "C", "D", "E", "F")
        for block in range(8)
        if (arm, block) not in groups
    ]
    assert remaining[0] == ("C", 7)
    assert len(remaining) == 25          # 1 remaining C block + 8 each for D, E, F
    assert ("B", 0) not in remaining

    # That block's registered owners are 8400897..8401024.
    first_owner = brc.TRAIN_FIRST_OWNER + 7 * brc.OWNERS_PER_BLOCK
    assert first_owner == 8400897
    assert first_owner + brc.OWNERS_PER_BLOCK - 1 == 8401024


def test_building_the_index_twice_yields_the_same_identity(store):
    """A restart must re-derive the identical index, not a drifting one."""
    first = brc.build_index(formal, IDENTITY)
    second = brc.build_index(formal, IDENTITY)
    assert first["index_sha256"] == second["index_sha256"]
    assert first["stages"] == second["stages"]


REAL_DATA_ROOT = Path("/mnt/d/DeadlycatCoding/stsrl-data")


@pytest.mark.skipif(
    not (REAL_DATA_ROOT / "issue85" / "artifacts" / "formal").is_dir(),
    reason="durable Issue 85 store is not mounted",
)
def test_real_evidence_keeps_c06_failed_attempt_alongside_carried_success(monkeypatch):
    """The carried stage succeeded *after* a retained failure; both must survive.

    C/06 records a single attempt that was rejected by the reader, so the stage is
    SUCCEEDED while `failed_attempt_count` stays 1. Carrying the stage by reference
    must not launder that history into a clean first-try execution.
    """
    monkeypatch.setattr(formal, "DATA_ROOT", str(REAL_DATA_ROOT))
    index = brc.build_index(formal, IDENTITY)
    stages = {(s["arm"], s["block_index"]): s for s in index["stages"]}
    c06 = stages[("C", 6)]
    assert c06["update_package_commit"].startswith("2ecd9961")

    receipt = formal.read_json(
        REAL_DATA_ROOT / "issue85" / "artifacts" / "formal"
        / c06["update_package_commit"]
        / "training" / "C" / "updates" / "block-06" / "success-receipt.json"
    )
    assert receipt["failed_attempt_count"] == 1
    assert formal.file_sha256(
        REAL_DATA_ROOT / "issue85" / "artifacts" / "formal"
        / c06["update_package_commit"]
        / "training" / "C" / "updates" / "block-06" / "success-receipt.json"
    ) == c06["update_receipt_sha256"]
    # The index cites provenance only; it must not restate or clear attempt history.
    assert "failed_attempt_count" not in c06
