"""Versioned by-reference carry index for the Issue 85 formal successor.

The authoritative evidence for an already-completed training stage is the
immutable artifact inside the package that really produced it. An executing
successor therefore keeps a small authenticated *index* that names those
producers by exact commit and pins their content hashes, instead of copying
result trees or restating historical facts as if they had happened under the
successor root.

Three identities stay distinct everywhere in this module:

* the **producer** package that wrote a historical stage,
* the **executing** successor commit that owns the index, and
* the registered **arm/block/owner** roster entry.

A stage's games and its actor update did not always land in the same package, so
both producers are named explicitly.

Nothing here starts a game or an optimizer step; it only reads and verifies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

INDEX_SCHEMA = "issue85-by-reference-carry-index-v2"
POLICY_PRODUCER_SCHEMA = "issue85-policy-producers-v1"
OWNERS_PER_BLOCK = 128
TRAIN_FIRST_OWNER = 8400001

# Audited producers of every completed training block. B/00's update was applied
# after its original AttributeError failure, and B/01's games were staged before
# the package that applied its update, so the two columns differ there.
#
# `b61c6674` also holds B/04..B/07 update receipts; those are the excluded
# duplicate executions and are deliberately never referenced.
STAGES: tuple[tuple[str, int, str, str], ...] = (
    ("B", 0, "e549105ead79c9524bee706fc600164f8c7f9f1a", "fde456f52aaa388b7abc92a2b148ba91d3720e1e"),
    ("B", 1, "318bdc800c5f986d67b445c38caeb13cebb154fb", "b8b3fc043671498628f96d378d9cecd7ac54106f"),
    ("B", 2, "b8b3fc043671498628f96d378d9cecd7ac54106f", "b8b3fc043671498628f96d378d9cecd7ac54106f"),
    ("B", 3, "8ac6fa868d4a81625304c7e9024db6e538b5a5b6", "8ac6fa868d4a81625304c7e9024db6e538b5a5b6"),
    ("B", 4, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a", "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("B", 5, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a", "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("B", 6, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a", "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("B", 7, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a", "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 0, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a", "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 1, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a", "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 2, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a", "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 3, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a", "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 4, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a", "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 5, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a", "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 6, "2ecd996139778665d60a06f624b41e260c7b7358", "2ecd996139778665d60a06f624b41e260c7b7358"),
)

EXCLUDED_DUPLICATE_PACKAGE = "b61c6674e104739dc86a243486b0ed08335c6a76"

# The successor package that really executed C/07 and D/E/F after the by-reference
# route opened. Its evidence is carried by exact reference: it is a legitimate
# producer because legitimacy rests on the executing SHA, the registered owners,
# the successful receipts and byte-level checkpoint continuity -- not on whether a
# package is an ancestor, a failure-recovery package, or a same-lineage successor.
#
# Nothing here re-runs those stages. Each is read and verified in the package that
# wrote it, and its outcomes are never restated as if the new package ran them.
SUCCESSOR_PACKAGE_COMMIT = "57ed39411c8024283a930ed30e80339597b9006e"

# C/07 is the stage the previous index declared unfinished; D/E/F ran after it.
SUCCESSOR_STAGES: tuple[tuple[str, int], ...] = (
    ("C", 7),
    *((arm, block) for arm in ("D", "E", "F") for block in range(8)),
)

# Training roster the study registered: the five trained arms over eight blocks.
# Deriving from this roster rather than from a hardcoded "first unfinished" pair is
# what keeps the cursor honest once stages from another package are carried.
TRAINED_ARMS: tuple[str, ...] = ("B", "C", "D", "E", "F")

VALIDATION_POLICIES: tuple[str, ...] = ("PARENT", "A", "B", "C", "D", "E", "F")

# VALIDATION groups completed in the successor package. PARENT and A were run and
# kept before the evaluation loader defect stopped the run at B; they never entered
# the defective carried-arm branch, so their evidence stands on its own.
SUCCESSOR_VALIDATION: tuple[str, ...] = ("PARENT", "A")

# Stages whose actor is consumed as a model input, so a worker needs a producer
# binding rather than a local read.
#
# Every arm's block-07 actor is the evaluation model for that arm's VALIDATION and
# TEST groups, so once C/07..F/07 are carried (not executed here) each arm's final
# actor must be named explicitly. C/06 stays because the historical C lineage
# consumed it and the index must still describe that link.
POLICY_INPUT_PRODUCERS: tuple[tuple[str, int], ...] = (
    ("C", 6),
    *((arm, 7) for arm in TRAINED_ARMS),
)


def _formal_root(formal: Any) -> Path:
    return Path(formal.DATA_ROOT) / "issue85" / "artifacts" / "formal"


def _hash_json(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _inside(root: Path, relative: str) -> Path:
    path = (Path(root) / relative).resolve(strict=True)
    try:
        path.relative_to(Path(root).resolve(strict=True))
    except ValueError as exc:  # pragma: no cover - defensive
        raise RuntimeError("by-reference evidence escapes its producer package") from exc
    return path


def resolve_stage(
    formal: Any, arm: str, block: int, games_commit: str, update_commit: str,
) -> dict[str, Any]:
    """Verify one completed stage in its real producer packages and describe it.

    Every returned field is recomputed from bytes under those roots; nothing is
    trusted because a receipt merely asserts it.
    """
    games_root = _formal_root(formal) / games_commit
    if not games_root.is_dir():
        raise RuntimeError(f"games producer is absent for {arm}/{block:02d}: {games_commit}")
    update_root = _formal_root(formal) / update_commit
    if not update_root.is_dir():
        raise RuntimeError(f"update producer is absent for {arm}/{block:02d}: {update_commit}")
    if update_commit == EXCLUDED_DUPLICATE_PACKAGE:
        raise RuntimeError(f"excluded duplicate package may not carry {arm}/{block:02d}")

    group_rel = f"training/{arm}/block-{block:02d}/games/group-receipt.json"
    group_path = _inside(games_root, group_rel)
    group = formal.read_json(group_path)
    group_sha = formal.file_sha256(group_path)
    if (
        group.get("status") != "SUCCEEDED"
        or group.get("completed") != OWNERS_PER_BLOCK
        or group.get("expected") != OWNERS_PER_BLOCK
        or len(group.get("successful_unit_ids", [])) != OWNERS_PER_BLOCK
    ):
        raise RuntimeError(f"producer game group is not a complete {arm}/{block:02d} stage")

    update_rel = f"training/{arm}/updates/block-{block:02d}/success-receipt.json"
    update_path = _inside(update_root, update_rel)
    update = formal.read_json(update_path)
    if (
        update.get("status") != "SUCCEEDED"
        or update.get("arm") != arm
        or update.get("block_index") != block
        or update.get("package_commit") != update_commit
        or update.get("owner_count") != OWNERS_PER_BLOCK
    ):
        raise RuntimeError(f"producer actor update is not a complete {arm}/{block:02d} stage")
    if update.get("training_group_receipt_sha256") != group_sha:
        raise RuntimeError(
            f"producer update does not name the verified {arm}/{block:02d} game group"
        )

    # The optimizer/actor bytes themselves must still be present and unmodified
    # in the package that wrote them.
    checkpoints: dict[str, str] = {}
    for field, relative_field in (
        ("actor_checkpoint_sha256", "actor_checkpoint_path"),
        ("optimizer_checkpoint_sha256", "optimizer_checkpoint_path"),
    ):
        recorded = update.get(field)
        relative = update.get(relative_field)
        if not isinstance(recorded, str) or len(recorded) != 64 or not isinstance(relative, str):
            raise RuntimeError(f"producer update lacks {field} for {arm}/{block:02d}")
        actual = formal.file_sha256(_inside(update_root, relative))
        if actual != recorded:
            raise RuntimeError(f"producer {field} bytes differ for {arm}/{block:02d}")
        checkpoints[field] = recorded

    first = TRAIN_FIRST_OWNER + block * OWNERS_PER_BLOCK
    return {
        "arm": arm,
        "block_index": block,
        "games_package_commit": games_commit,
        "update_package_commit": update_commit,
        "group_receipt_path": group_rel,
        "group_receipt_sha256": group_sha,
        "update_receipt_path": update_rel,
        "update_receipt_file_sha256": formal.file_sha256(update_path),
        "update_receipt_self_sha256": update.get("receipt_sha256"),
        "predecessor_carry_manifest_sha256": update.get("predecessor_carry_manifest_sha256"),
        "actor_checkpoint_sha256": checkpoints["actor_checkpoint_sha256"],
        "optimizer_checkpoint_sha256": checkpoints["optimizer_checkpoint_sha256"],
        "source_model_digest": update.get("source_model_digest"),
        "result_model_digest": update.get("result_model_digest"),
        "owner_first": first,
        "owner_last": first + OWNERS_PER_BLOCK - 1,
    }


CANONICAL_STAGE = ("C", 6)

# Every completed training stage the index carries: the predecessor's audited
# fifteen plus the successor's twenty-five.
ALL_STAGES: tuple[tuple[str, int, str, str], ...] = STAGES + tuple(
    (arm, block, SUCCESSOR_PACKAGE_COMMIT, SUCCESSOR_PACKAGE_COMMIT)
    for arm, block in SUCCESSOR_STAGES
)

VALIDATION_OWNERS = 64
GROUP_SCHEMA = "issue85-formal-game-group-v1"


def resolve_validation_group(
    formal: Any, policy: str, producer_commit: str, source_lock_sha256: str,
) -> dict[str, Any]:
    """Verify one completed VALIDATION group in the package that really ran it.

    Returns only facts recomputed from the producer's own bytes. Nothing about the
    group is restated as though the executing successor had produced it.
    """
    import formal_execution as fe

    group_root = _formal_root(formal) / producer_commit / "validation" / policy
    receipt_path = _inside(group_root, "group-receipt.json")
    group = formal.read_json(receipt_path)
    receipt_sha = formal.file_sha256(receipt_path)
    units = group.get("units")
    if (
        group.get("schema_id") != GROUP_SCHEMA
        or group.get("stage_id") != f"VALIDATION-{policy}"
        or group.get("status") != "SUCCEEDED"
        or group.get("completed") != VALIDATION_OWNERS
        or group.get("expected") != VALIDATION_OWNERS
        or group.get("package_commit") != producer_commit
        or group.get("source_lock_sha256") != source_lock_sha256
        or group.get("model_selection_or_tuning") is not False
        or not isinstance(units, list)
        or len(units) != VALIDATION_OWNERS
        or len(group.get("successful_unit_ids", [])) != VALIDATION_OWNERS
    ):
        raise RuntimeError(
            f"producer VALIDATION group {policy} is not a complete frozen 64-owner stage"
        )
    digests = set()
    for unit in units:
        digest = unit.get("model_digest") if isinstance(unit, Mapping) else None
        if not isinstance(digest, str) or len(digest) != 64:
            raise RuntimeError(f"producer VALIDATION group {policy} lacks a frozen model digest")
        digests.add(digest)
    if len(digests) != 1:
        raise RuntimeError(
            f"producer VALIDATION group {policy} evaluated more than one model; "
            "validation must describe a single frozen policy"
        )
    # The evaluated model must be the arm's registered evaluation model, which for
    # PARENT/A is the locked baseline and for B..F is that arm's block-07 actor.
    expected_digest = (
        str(formal.LOCK["baseline_P"]["model_digest"]) if policy == "PARENT"
        else str(formal.LOCK["baseline_A"]["model_digest"]) if policy == "A"
        else None
    )
    if expected_digest is not None and digests.pop() != expected_digest:
        raise RuntimeError(
            f"producer VALIDATION group {policy} did not evaluate its locked model"
        )
    return {
        "policy": policy,
        "producer_package_commit": producer_commit,
        "group_receipt_path": f"validation/{policy}/group-receipt.json",
        "group_receipt_sha256": receipt_sha,
        "completed": VALIDATION_OWNERS,
        "expected": VALIDATION_OWNERS,
        "model_digest": next(iter(digests), expected_digest),
        "seed_table_sha256": group.get("seed_table_sha256"),
        "authorization_receipt_sha256": group.get("authorization_receipt_sha256"),
        "active_wall_seconds_at_stage_close": group.get("active_wall_seconds_at_stage_close"),
    }


def derive_next_stage(
    completed_training: set[tuple[str, int]], carried_validation: set[str],
) -> dict[str, Any]:
    """Derive the next unfinished stage from the registered roster.

    Previously this was the constant ``("C", 7)``. That was wrong the moment work
    beyond C/07 was carried: a fresh package would have declared a completed stage
    unfinished and re-run it. The cursor is a function of what the roster registers
    and what has verifiably completed, never a literal.
    """
    for arm in TRAINED_ARMS:
        for block in range(8):
            if (arm, block) not in completed_training:
                return {"phase": "TRAIN", "arm": arm, "block": block,
                        "stage_id": f"TRAIN-{arm}-BLOCK-{block:02d}"}
    for policy in VALIDATION_POLICIES:
        if policy not in carried_validation:
            return {"phase": "VALIDATION", "arm": None, "block": None,
                    "stage_id": f"VALIDATION-{policy}"}
    return {"phase": "TEST", "arm": None, "block": None, "stage_id": "TEST"}


def _canonical_carry_proofs(formal: Any, stages: list[dict[str, Any]]) -> dict[str, Any]:
    """Authenticate the e549 failure proof and the e902 reconciliation by reference.

    In by-reference mode `_validate_carry_manifest` is never called, so nothing
    would otherwise establish that the predecessor's failed update really failed
    this way, or that the canonical e902 reconciliation governs the carried
    blocks. Absent this, canonicality rests on the hardcoded STAGES table -- code,
    not evidence. Both proofs are read from the producer package that holds them
    and recorded here as digests, never recomputed under the successor root.
    """
    import formal_execution as fe

    by_key = {(s["arm"], s["block_index"]): s for s in stages}
    stage = by_key[CANONICAL_STAGE]
    manifest_path = (
        _formal_root(formal) / stage["update_package_commit"]
        / "predecessor-carry-manifest.json"
    )
    if not manifest_path.is_file():
        raise RuntimeError("canonical carry manifest is missing from its producer package")
    manifest = formal.read_json(manifest_path)
    digest = formal.file_sha256(manifest_path)
    if digest != stage["predecessor_carry_manifest_sha256"]:
        raise RuntimeError("canonical carry manifest is not the one C/06 carried")
    predecessor = manifest.get("predecessor")
    failed = predecessor.get("failed_update") if isinstance(predecessor, dict) else None
    if (
        not isinstance(predecessor, dict)
        or predecessor.get("package_commit") != fe.PREDECESSOR_PACKAGE_COMMIT
        or not isinstance(failed, dict)
        or failed.get("failure_type") != "AttributeError"
        or failed.get("failure_detail") != fe.PREDECESSOR_UPDATE_FAILURE_DETAIL
        or failed.get("log_path") != fe.PREDECESSOR_UPDATE_FAILURE_LOG
        or failed.get("log_sha256") != fe.PREDECESSOR_UPDATE_FAILURE_LOG_SHA256
        or failed.get("formal_progress_sha256") != fe.PREDECESSOR_PROGRESS_SHA256
    ):
        raise RuntimeError("predecessor failure proof does not match the locked e549 evidence")
    # Anchoring to the constant alone proves only that the manifest agrees with the
    # constant. The log itself still exists in e549's own package, so hash it there
    # and make the proof point at its producer rather than at a compile-time value.
    failure_log = _inside(
        _formal_root(formal) / fe.PREDECESSOR_PACKAGE_COMMIT,
        str(failed["log_path"]),
    )
    failure_log_sha256 = formal.file_sha256(failure_log)
    if failure_log_sha256 != fe.PREDECESSOR_UPDATE_FAILURE_LOG_SHA256:
        raise RuntimeError("the e549 failure log does not hash to the locked digest")
    reconciliation = manifest.get("e902_canonical_reconciliation")
    if not isinstance(reconciliation, dict):
        raise RuntimeError("canonical carry manifest lacks the e902 reconciliation")
    if (
        reconciliation.get("excluded_duplicate_package_commit") != EXCLUDED_DUPLICATE_PACKAGE
        or reconciliation.get("canonical_source_package_commit")
        != fe.E902_CANONICAL_PACKAGE_COMMIT
    ):
        raise RuntimeError("e902 reconciliation does not exclude the duplicate evidence it must")
    # The reconciliation is self-verifying under the study canonical serializer, so
    # trusting it is not a matter of trusting the document's own say-so.
    body = {k: v for k, v in reconciliation.items() if k != "record_sha256"}
    if reconciliation.get("record_sha256") != _hash_json(body):
        raise RuntimeError("e902 reconciliation self-digest does not verify")

    # Derive the skip set from the reconciliation instead of asserting it from the
    # STAGES table: if the table drifted, nothing else would notice. The record
    # names only the ten stages it reconciled (B/04-B/07, C/00-C/05); the other
    # five were produced elsewhere and are not derivable from it.
    named_groups = reconciliation.get("groups")
    named_updates = reconciliation.get("actor_updates")
    if not isinstance(named_groups, dict) or not isinstance(named_updates, dict):
        raise RuntimeError("e902 reconciliation does not name its reconciled stages")
    if set(named_groups) != set(named_updates):
        raise RuntimeError("e902 reconciliation names unequal group and update stage sets")
    by_key = {(s["arm"], s["block_index"]): s for s in stages}
    canonical_source = str(reconciliation.get("canonical_source_package_commit"))
    for name, group in named_groups.items():
        arm, _, block_text = str(name).partition(":")
        stage = by_key.get((arm, int(block_text)))
        if stage is None:
            raise RuntimeError(f"e902 reconciliation names a stage the index does not carry: {name}")
        if stage["update_package_commit"] != canonical_source:
            raise RuntimeError(f"{name} was not produced by the canonical e902 package")
        if stage["group_receipt_sha256"] != group.get("group_receipt_sha256"):
            raise RuntimeError(f"{name} game group differs from the canonical reconciliation")
        if int(group.get("completed", -1)) != OWNERS_PER_BLOCK:
            raise RuntimeError(f"{name} reconciled game group is incomplete")
        update = named_updates[name]
        if (
            stage["actor_checkpoint_sha256"] != update.get("actor_checkpoint_sha256")
            or stage["optimizer_checkpoint_sha256"] != update.get("optimizer_checkpoint_sha256")
        ):
            raise RuntimeError(f"{name} checkpoints differ from the canonical reconciliation")

    return {
        "carry_manifest_sha256": digest,
        "e549_failure_proof_sha256": _hash_json(failed),
        "e549_failure_log_sha256": failure_log_sha256,
        "e902_reconciliation_sha256": _hash_json(reconciliation),
        "e902_reconciled_stages": sorted(str(name) for name in named_groups),
        "e902_excluded_duplicate_package_commit": reconciliation.get(
            "excluded_duplicate_package_commit"
        ),
        "e902_canonical_source_package_commit": reconciliation.get(
            "canonical_source_package_commit"
        ),
    }


def build_index(
    formal: Any, identity: Mapping[str, str], *, require_active_wall: bool = True,
) -> dict[str, Any]:
    """Resolve the full completed roster once, verifying every producer."""
    stages = [resolve_stage(formal, arm, block, g, u) for arm, block, g, u in ALL_STAGES]
    seen: set[tuple[str, int]] = set()
    # Each arm trains its own lineage over the same 1,024 registered owners, so a
    # registered outcome is identified by (arm, owner) -- the unit id
    # TRAIN:<owner>:<arm> -- not by owner alone.
    units: set[tuple[str, int]] = set()
    for stage in stages:
        key = (stage["arm"], stage["block_index"])
        if key in seen:
            raise RuntimeError(f"duplicate completed stage {key}")
        seen.add(key)
        span = {(stage["arm"], owner) for owner in range(stage["owner_first"], stage["owner_last"] + 1)}
        if units & span:
            raise RuntimeError(f"completed stages overlap on registered outcomes at {key}")
        units |= span
    if len(stages) != len(ALL_STAGES) or len(units) != len(ALL_STAGES) * OWNERS_PER_BLOCK:
        raise RuntimeError("completed roster is not unique across registered outcomes")

    validation_stages = [
        resolve_validation_group(formal, policy, producer, identity["source_lock_sha256"])
        for policy, producer in ((p, SUCCESSOR_PACKAGE_COMMIT) for p in SUCCESSOR_VALIDATION)
    ]
    carried_validation = {group["policy"] for group in validation_stages}
    collisions = carried_validation - set(VALIDATION_POLICIES)
    if collisions:
        raise RuntimeError(f"carried VALIDATION names unregistered policies: {sorted(collisions)}")

    next_stage = derive_next_stage(seen, carried_validation)

    producers = sorted(
        {stage["games_package_commit"] for stage in stages}
        | {stage["update_package_commit"] for stage in stages}
        | {group["producer_package_commit"] for group in validation_stages}
    )
    proofs = _canonical_carry_proofs(formal, stages)
    # The active wall is live ledger state, not producer evidence. Reading it is
    # meaningful only when minting the annotation; a re-derivation that exists to
    # check producer bytes must not depend on a file another process rewrites
    # continuously. Requiring it there made one worker die when the ledger was
    # mid-replace, which paused the whole stage for a file the check did not need.
    observed_wall: float | None = None
    if require_active_wall:
        clock_path = (
            Path(formal.DATA_ROOT) / "issue85" / "artifacts" / "active-wall-issue85-task.json"
        )
        if not clock_path.is_file():
            raise RuntimeError("the shared Issue 85 active-wall ledger is missing")
        observed_wall = float(formal.read_json(clock_path).get("elapsed_seconds", -1.0))
        if observed_wall < 0:
            raise RuntimeError("the shared active-wall ledger has no elapsed reading")
    unsigned = {
        "schema_id": INDEX_SCHEMA,
        "executing_package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "completed_stage_count": len(stages),
        "completed_train_outcomes": len(units),
        "validation_stage_count": len(validation_stages),
        "validation_outcomes": sum(group["completed"] for group in validation_stages),
        "next_stage_phase": next_stage["phase"],
        "next_stage_id": next_stage["stage_id"],
        "producers": producers,
        "excluded_duplicate_package": EXCLUDED_DUPLICATE_PACKAGE,
        "stages": stages,
        "validation_stages": validation_stages,
        **proofs,
    }
    # The active wall is mutable ledger state, not producer evidence. It advances
    # while the run is in progress -- including before the very first worker is
    # admitted -- so folding it into index_sha256 would make the digest disagree
    # with itself the moment the run starts. It is recorded beside the digest and
    # checked monotonically against the ledger, never hashed into it.
    return {
        **unsigned,
        "index_sha256": _hash_json(unsigned),
        "observed_active_wall_seconds": observed_wall,
    }


# Recorded beside the digest, never inside it: both change or are re-derived
# independently of the producer evidence the digest is meant to pin.
UNHASHED_INDEX_KEYS = ("index_sha256", "observed_active_wall_seconds")


def _optional_active_wall(formal: Any) -> float | None:
    """The live cumulative wall, or None when the ledger is mid-replace.

    Read separately from the index rebuild so the annotation can still be checked
    whenever the ledger is legible, without making producer-evidence verification
    fail when it is not.
    """
    path = (
        Path(formal.DATA_ROOT) / "issue85" / "artifacts" / "active-wall-issue85-task.json"
    )
    try:
        value = formal.read_json(path).get("elapsed_seconds")
    except (OSError, ValueError):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) < 0:
        return None
    return float(value)


def validate_index(formal: Any, path: Path, identity: Mapping[str, str]) -> dict[str, Any]:
    """Re-verify a stored index against the live producer bytes.

    The stored digest covers producer evidence only. The active-wall annotation is
    checked monotonically against the ledger so a stale or inflated reading cannot
    be smuggled in, while the run's own clock advance cannot invalidate the index.
    """
    index = formal.read_json(Path(path))
    unsigned = {key: value for key, value in index.items() if key not in UNHASHED_INDEX_KEYS}
    if (
        index.get("schema_id") != INDEX_SCHEMA
        or index.get("executing_package_commit") != identity["package_commit"]
        or index.get("source_lock_sha256") != identity["source_lock_sha256"]
        or index.get("index_sha256") != _hash_json(unsigned)
    ):
        raise RuntimeError(
            "by-reference carry index is malformed or bound to another executing package"
        )
    expected = build_index(formal, identity, require_active_wall=False)
    if expected["index_sha256"] != index.get("index_sha256"):
        raise RuntimeError("by-reference carry index disagrees with the live producer evidence")
    # The annotation is compared only where it can be read. It is not part of the
    # digest; the ledger read is best-effort here so that a mid-replace file cannot
    # fail a check about producer bytes. The floor that protects the study clock is
    # enforced separately, strictly, against the ledger in validate_authorization.
    stored_wall = index.get("observed_active_wall_seconds")
    if not isinstance(stored_wall, (int, float)):
        raise RuntimeError("by-reference carry index lacks its active-wall annotation")
    live_wall = _optional_active_wall(formal)
    if live_wall is not None and float(stored_wall) > live_wall + 1.0:
        raise RuntimeError("by-reference carry index claims more active wall than the ledger")
    return index


def policy_producers(
    formal: Any, index: Mapping[str, Any], identity: Mapping[str, str],
) -> dict[str, Any]:
    """Worker bindings for stages whose actor is consumed as a policy input."""
    by_key = {(s["arm"], s["block_index"]): s for s in index["stages"]}
    producers: dict[str, Any] = {}
    for arm, block in POLICY_INPUT_PRODUCERS:
        stage = by_key.get((arm, block))
        if stage is None:
            raise RuntimeError(f"policy input producer {arm}/{block:02d} is not a completed stage")
        producers[f"{arm}:{block}"] = {
            "package_commit": stage["update_package_commit"],
            "games_package_commit": stage["games_package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "predecessor_carry_manifest_sha256": stage["predecessor_carry_manifest_sha256"],
            "group_receipt_sha256": stage["group_receipt_sha256"],
        }
    return {
        "schema_id": POLICY_PRODUCER_SCHEMA,
        "executing_package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "producers": producers,
    }


def stage_bindings(
    index: Mapping[str, Any], identity: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """Producer binding for every completed stage, keyed ``arm:block``.

    A carried stage's update receipt is resolved from the package that really
    wrote it, while its game group may live in a different package -- B/00's games
    are in `e549105e` but its update in `fde456f5`. Both are named so verification
    never has to assume they coincide.
    """
    bindings: dict[str, dict[str, Any]] = {}
    for stage in index["stages"]:
        bindings[f"{stage['arm']}:{stage['block_index']}"] = {
            "package_commit": stage["update_package_commit"],
            "games_package_commit": stage["games_package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "predecessor_carry_manifest_sha256": stage["predecessor_carry_manifest_sha256"],
            "group_receipt_sha256": stage["group_receipt_sha256"],
        }
    return bindings


def carried_validation_groups(index: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Completed VALIDATION groups a successor adopts instead of re-running.

    Each record names the package that really ran the group so the caller can
    verify it there and record an honest provenance, rather than presenting the
    group as local work.
    """
    return {
        str(group["policy"]): {
            "policy": str(group["policy"]),
            "producer_package_commit": str(group["producer_package_commit"]),
            "group_receipt_sha256": str(group["group_receipt_sha256"]),
            "completed": int(group["completed"]),
            "expected": int(group["expected"]),
            "model_digest": str(group["model_digest"]),
            # Needed so TEST-open can verify the carried receipt against the
            # authorization and seed table that really produced it, instead of
            # demanding this package's own identity where it does not apply.
            "authorization_receipt_sha256": str(group["authorization_receipt_sha256"]),
            "seed_table_sha256": str(group["seed_table_sha256"]),
            "carried_by_reference": True,
        }
        for group in index.get("validation_stages", [])
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build/verify the Issue 85 by-reference carry index. Never starts games."
    )
    parser.add_argument("--write", action="store_true", help="write index and policy bindings into the executing package")
    parser.add_argument("--verify", action="store_true", help="re-verify the stored index instead of building")
    args = parser.parse_args(argv)

    import formal_runner as formal

    identity = formal.pilot.source_identity(require_clean=True)
    package_root = _formal_root(formal) / identity["package_commit"]

    if args.verify:
        index = validate_index(formal, package_root / "by-reference-carry-index.json", identity)
        print(json.dumps({
            "status": "VERIFIED",
            "index_sha256": index["index_sha256"],
            "completed_stage_count": index["completed_stage_count"],
            "completed_train_outcomes": index["completed_train_outcomes"],
            "validation_stage_count": index["validation_stage_count"],
            "validation_outcomes": index["validation_outcomes"],
            "next_stage": index["next_stage_id"],
        }, sort_keys=True))
        return 0

    index = build_index(formal, identity)
    result = {
        "status": "BUILT_READ_ONLY_NO_GAMES",
        "executing_package_commit": identity["package_commit"],
        "index_sha256": index["index_sha256"],
        "completed_stage_count": index["completed_stage_count"],
        "completed_train_outcomes": index["completed_train_outcomes"],
        "validation_stage_count": index["validation_stage_count"],
        "validation_outcomes": index["validation_outcomes"],
        "next_stage": index["next_stage_id"],
        "producers": index["producers"],
    }
    if args.write:
        if not package_root.is_dir():
            raise RuntimeError("executing package does not exist; run --preflight first")
        (package_root / "by-reference-carry-index.json").write_text(
            json.dumps(index, sort_keys=True, indent=1), encoding="utf-8"
        )
        (package_root / "policy-producers.json").write_text(
            json.dumps(policy_producers(formal, index, identity), sort_keys=True, indent=1),
            encoding="utf-8",
        )
        result["written"] = ["by-reference-carry-index.json", "policy-producers.json"]
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
