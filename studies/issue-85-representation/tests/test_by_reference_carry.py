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

# Only these five stages ever went through a carry step. A fixture that gave every
# stage a digest is exactly what hid the B2 defect, so the ten others record None.
CARRY_DIGEST = "c" * 64
STAGE_CARRIES = {("B", 0), ("B", 1), ("B", 3), ("B", 4), ("C", 6)}


def _carry_for(arm: str, block: int) -> str | None:
    return CARRY_DIGEST if (arm, block) in STAGE_CARRIES else None


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
            "predecessor_carry_manifest_sha256": _carry_for(arm, block),
            "source_model_digest": f"d{index:063x}",
            "result_model_digest": f"e{index:063x}",
        }
        (update_dir / "success-receipt.json").write_text(json.dumps(update), encoding="utf-8")

    # The canonical carry manifest lives in the C/06 producer package and its file
    # digest is what C/06 really carried. The e549 failure proof and the e902
    # reconciliation are only obtainable from it, so it must be modelled here.
    import formal_execution as fe

    canonical_update = next(
        update_commit for arm, block, _games, update_commit in brc.STAGES
        if (arm, block) == brc.CANONICAL_STAGE
    )
    # The reconciliation names exactly the ten stages e902 produced (B/04-B/07,
    # C/00-C/05) and verifies itself, so the fixture must reproduce that shape.
    reconciled_groups: dict[str, dict] = {}
    reconciled_updates: dict[str, dict] = {}
    for arm, block, games_commit, update_commit in brc.STAGES:
        if update_commit != fe.E902_CANONICAL_PACKAGE_COMMIT:
            continue
        name = f"{arm}:{block}"
        group_path = (
            root / games_commit / "training" / arm / f"block-{block:02d}"
            / "games" / "group-receipt.json"
        )
        update_path = (
            root / update_commit / "training" / arm / f"updates/block-{block:02d}"
            / "success-receipt.json"
        )
        update_doc = json.loads(update_path.read_text())
        reconciled_groups[name] = {
            "completed": 128,
            "group_receipt_sha256": _sha(group_path.read_bytes()),
        }
        reconciled_updates[name] = {
            "actor_checkpoint_sha256": update_doc["actor_checkpoint_sha256"],
            "optimizer_checkpoint_sha256": update_doc["optimizer_checkpoint_sha256"],
        }

    reconciliation = {
        "schema_id": "issue85-formal-e902-canonical-reconciliation-v1",
        "status": "VERIFIED_READ_ONLY_CANONICAL_CARRY",
        "canonical_source_package_commit": fe.E902_CANONICAL_PACKAGE_COMMIT,
        "excluded_duplicate_package_commit": brc.EXCLUDED_DUPLICATE_PACKAGE,
        "groups": reconciled_groups,
        "actor_updates": reconciled_updates,
    }
    reconciliation["record_sha256"] = brc._hash_json(reconciliation)

    manifest = {
        "predecessor": {
            "package_commit": fe.PREDECESSOR_PACKAGE_COMMIT,
            "failed_update": {
                "failure_type": "AttributeError",
                "log_path": fe.PREDECESSOR_UPDATE_FAILURE_LOG,
                "log_sha256": fe.PREDECESSOR_UPDATE_FAILURE_LOG_SHA256,
                "formal_progress_sha256": fe.PREDECESSOR_PROGRESS_SHA256,
            },
        },
        "e902_canonical_reconciliation": reconciliation,
    }
    manifest_path = root / canonical_update / "predecessor-carry-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    receipt_path = (
        root / canonical_update / "training" / brc.CANONICAL_STAGE[0]
        / f"updates/block-{brc.CANONICAL_STAGE[1]:02d}" / "success-receipt.json"
    )
    receipt = json.loads(receipt_path.read_text())
    receipt["predecessor_carry_manifest_sha256"] = _sha(manifest_path.read_bytes())
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return root


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import formal_execution as fe

    # The failure log must exist in the predecessor package and hash to the locked
    # digest, so the fixture plants one and pins the constant to its real bytes.
    failure_log = (
        tmp_path / "issue85" / "artifacts" / "formal"
        / fe.PREDECESSOR_PACKAGE_COMMIT / str(fe.PREDECESSOR_UPDATE_FAILURE_LOG)
    )
    failure_log.parent.mkdir(parents=True, exist_ok=True)
    failure_log.write_bytes(b"fixture e549 failure log\n")
    monkeypatch.setattr(
        fe, "PREDECESSOR_UPDATE_FAILURE_LOG_SHA256", _sha(failure_log.read_bytes())
    )

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
    # C/06 carried the real canonical manifest, not the fixture placeholder.
    c06_carry = bindings["producers"]["C:6"]["predecessor_carry_manifest_sha256"]
    assert len(c06_carry) == 64 and c06_carry != CARRY_DIGEST
    assert bindings["producers"]["B:7"]["predecessor_carry_manifest_sha256"] is None
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
    unsigned = {k: v for k, v in index.items() if k not in brc.UNHASHED_INDEX_KEYS}
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


def test_clock_advance_cannot_change_the_evidence_digest(store):
    """The wall advances during the run; the evidence digest must not follow it."""
    first = brc.build_index(formal, IDENTITY)
    assert first["observed_active_wall_seconds"] == pytest.approx(75785.56550202602)

    (store / "issue85/artifacts/active-wall-issue85-task.json").write_text(
        json.dumps({"elapsed_seconds": 80000.0, "allowance_seconds": 295632}), encoding="utf-8"
    )
    later = brc.build_index(formal, IDENTITY)
    assert later["observed_active_wall_seconds"] == pytest.approx(80000.0)
    # Only the annotation moves; producer evidence and its digest are identical.
    assert later["stages"] == first["stages"]
    assert later["index_sha256"] == first["index_sha256"]


def test_stored_index_still_validates_after_the_run_clock_advances(store):
    """Workers are admitted only after the wall has moved; that must not fail closed."""
    index = brc.build_index(formal, IDENTITY)
    root = store / "issue85" / "artifacts" / "formal" / EXECUTING_COMMIT
    root.mkdir(parents=True, exist_ok=True)
    path = root / "by-reference-carry-index.json"
    path.write_text(json.dumps(index), encoding="utf-8")

    # The run starts and the shared ledger advances well past the written value.
    (store / "issue85/artifacts/active-wall-issue85-task.json").write_text(
        json.dumps({"elapsed_seconds": 90000.0, "allowance_seconds": 295632}), encoding="utf-8"
    )
    assert brc.validate_index(formal, path, IDENTITY)["index_sha256"] == index["index_sha256"]


def test_index_rejects_a_wall_annotation_ahead_of_the_ledger(store):
    """The annotation is unhashed, so it needs its own monotone guard."""
    index = brc.build_index(formal, IDENTITY)
    index["observed_active_wall_seconds"] = 999999.0
    root = store / "issue85" / "artifacts" / "formal" / EXECUTING_COMMIT
    root.mkdir(parents=True, exist_ok=True)
    path = root / "by-reference-carry-index.json"
    path.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(RuntimeError, match="more active wall than the ledger"):
        brc.validate_index(formal, path, IDENTITY)


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


def _manifest_path(store: Path) -> Path:
    canonical_update = next(
        update_commit for arm, block, _g, update_commit in brc.STAGES
        if (arm, block) == brc.CANONICAL_STAGE
    )
    return (
        store / "issue85" / "artifacts" / "formal" / canonical_update
        / "predecessor-carry-manifest.json"
    )


def _repin_c06(store: Path) -> None:
    """Re-point C/06's carry digest at the current manifest, as a forger would."""
    canonical_update = next(
        update_commit for arm, block, _g, update_commit in brc.STAGES
        if (arm, block) == brc.CANONICAL_STAGE
    )
    receipt_path = (
        store / "issue85" / "artifacts" / "formal" / canonical_update
        / "training" / brc.CANONICAL_STAGE[0]
        / f"updates/block-{brc.CANONICAL_STAGE[1]:02d}" / "success-receipt.json"
    )
    receipt = json.loads(receipt_path.read_text())
    receipt["predecessor_carry_manifest_sha256"] = _sha(
        _manifest_path(store).read_bytes()
    )
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


def test_index_derives_the_skip_set_instead_of_asserting_it(store):
    """A reconciliation naming a stage e902 did not produce must be refused.

    Without this the index could cite the reconciliation digest while still taking
    its stages from the hardcoded table, and drift between the two would go unseen.
    """
    manifest = json.loads(_manifest_path(store).read_text())
    reconciliation = manifest["e902_canonical_reconciliation"]
    # Move a reconciled stage to B/02, which the index carries but e902 never made.
    reconciliation["groups"]["B:2"] = reconciliation["groups"].pop("B:4")
    reconciliation["actor_updates"]["B:2"] = reconciliation["actor_updates"].pop("B:4")
    body = {k: v for k, v in reconciliation.items() if k != "record_sha256"}
    reconciliation["record_sha256"] = brc._hash_json(body)
    _manifest_path(store).write_text(json.dumps(manifest), encoding="utf-8")
    _repin_c06(store)

    with pytest.raises(RuntimeError, match="not produced by the canonical e902 package"):
        brc.build_index(formal, IDENTITY)


def test_index_rejects_a_reconciliation_that_does_not_verify(store):
    manifest = json.loads(_manifest_path(store).read_text())
    manifest["e902_canonical_reconciliation"]["record_sha256"] = "0" * 64
    _manifest_path(store).write_text(json.dumps(manifest), encoding="utf-8")
    _repin_c06(store)

    with pytest.raises(RuntimeError, match="self-digest does not verify"):
        brc.build_index(formal, IDENTITY)


def test_index_records_the_canonical_carry_proofs(store):
    """Canonicality must be evidence in the index, not a hardcoded table."""
    index = brc.build_index(formal, IDENTITY)
    assert len(index["carry_manifest_sha256"]) == 64
    assert len(index["e549_failure_proof_sha256"]) == 64
    assert len(index["e902_reconciliation_sha256"]) == 64
    assert index["e902_excluded_duplicate_package_commit"] == brc.EXCLUDED_DUPLICATE_PACKAGE


def test_index_rejects_a_failure_proof_from_another_lineage(store):
    """A manifest that no longer proves the e549 failure must be refused."""
    import formal_execution as fe

    canonical_update = next(
        update_commit for arm, block, _g, update_commit in brc.STAGES
        if (arm, block) == brc.CANONICAL_STAGE
    )
    manifest_path = (
        store / "issue85" / "artifacts" / "formal" / canonical_update
        / "predecessor-carry-manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    manifest["predecessor"]["failed_update"]["log_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    receipt_path = (
        store / "issue85" / "artifacts" / "formal" / canonical_update
        / "training" / brc.CANONICAL_STAGE[0]
        / f"updates/block-{brc.CANONICAL_STAGE[1]:02d}" / "success-receipt.json"
    )
    receipt = json.loads(receipt_path.read_text())
    receipt["predecessor_carry_manifest_sha256"] = _sha(manifest_path.read_bytes())
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(RuntimeError, match="does not match the locked e549 evidence"):
        brc.build_index(formal, IDENTITY)


def test_building_the_index_twice_yields_the_same_identity(store):
    """A restart must re-derive the identical index, not a drifting one."""
    first = brc.build_index(formal, IDENTITY)
    second = brc.build_index(formal, IDENTITY)
    assert first["index_sha256"] == second["index_sha256"]
    assert first["stages"] == second["stages"]


# Derived from the study's own data root rather than a hardcoded WSL path: the
# literal here was also mis-cased ("DeadlycatCoding"), which drvfs tolerates and a
# native Windows host does not. Deriving it keeps the real-evidence tests from
# silently skipping, which is what happened on the reviewer's run.
REAL_DATA_ROOT = Path(formal.DATA_ROOT)


@pytest.mark.skipif(
    not (REAL_DATA_ROOT / "issue85" / "artifacts" / "formal").is_dir(),
    reason="durable Issue 85 store is not mounted",
)
def test_every_carried_stage_reads_through_its_producer_binding(monkeypatch):
    """All fifteen stages must be readable, not just the five that carried anything.

    Only B/00, B/01, B/03, B/04 and C/06 recorded a carry digest; the other ten
    hold None. A binding demanding a 64-hex digest for all of them rejected ten
    stages, so the successor died at B/02 -- before any game, long before C/07.
    The synthetic fixture hid this by hardcoding a digest for every stage.
    """
    import formal_execution as fe

    monkeypatch.setattr(formal, "DATA_ROOT", str(REAL_DATA_ROOT))
    # Take the lock from real evidence rather than a module constant.
    probe = formal.read_json(
        REAL_DATA_ROOT / "issue85" / "artifacts" / "formal"
        / "2ecd996139778665d60a06f624b41e260c7b7358"
        / "training" / "C" / "updates" / "block-06" / "success-receipt.json"
    )
    identity = {
        "package_commit": "0" * 40,
        "source_lock_sha256": str(probe["source_lock_sha256"]),
    }
    index = brc.build_index(formal, identity)
    bindings = brc.stage_bindings(index, identity)

    null_carry = [
        key for key, value in bindings.items()
        if value["predecessor_carry_manifest_sha256"] is None
    ]
    assert len(null_carry) == 10, null_carry

    parent = (
        REAL_DATA_ROOT / "issue85" / "artifacts" / "formal"
        / "2ecd996139778665d60a06f624b41e260c7b7358"
    )
    for stage in index["stages"]:
        key = f"{stage['arm']}:{stage['block_index']}"
        binding = bindings[key]
        receipt = fe._read_update_receipt(
            formal, parent, identity,
            arm=stage["arm"], block=int(stage["block_index"]),
            producer=binding,
        )
        assert receipt.get("status") == "SUCCEEDED", key
        assert receipt.get("package_commit") == binding["package_commit"], key


@pytest.mark.skipif(
    not (REAL_DATA_ROOT / "issue85" / "artifacts" / "formal").is_dir(),
    reason="durable Issue 85 store is not mounted",
)
def test_real_evidence_keeps_c06_attempt_layout_intact(monkeypatch):
    """Carrying C/06 by reference must not re-attempt it in its producer package.

    C/06 already holds a completed 64-step update under a single attempt directory.
    A successor that replayed it -- or that wrote a second attempt beside the
    original evidence -- would corrupt the producer package it is supposed to cite.
    """
    monkeypatch.setattr(formal, "DATA_ROOT", str(REAL_DATA_ROOT))
    index = brc.build_index(formal, IDENTITY)
    stages = {(s["arm"], s["block_index"]): s for s in index["stages"]}
    c06 = stages[("C", 6)]
    assert c06["update_package_commit"].startswith("2ecd9961")

    update_root = (
        REAL_DATA_ROOT / "issue85" / "artifacts" / "formal"
        / c06["update_package_commit"] / "training" / "C" / "updates" / "block-06"
    )
    receipt_path = update_root / "success-receipt.json"
    receipt = formal.read_json(receipt_path)
    # The index binds the receipt's internal self-hash (what the reader verifies),
    # not the file digest -- the two differ, and conflating them would hide a swap.
    assert receipt["receipt_sha256"] == c06["update_receipt_self_sha256"]
    assert formal.file_sha256(receipt_path) == c06["update_receipt_file_sha256"]
    assert c06["update_receipt_file_sha256"] != c06["update_receipt_self_sha256"]
    assert receipt["status"] == "SUCCEEDED"
    assert receipt["completed_steps"] == 64
    assert receipt["owner_count"] == 128
    # Exactly the one attempt that was really run; no replay was appended.
    assert [p.name for p in update_root.iterdir() if p.is_dir()] == ["attempt-0001"]
    # The index cites provenance only; it must not restate attempt history.
    assert "failed_attempt_count" not in c06
