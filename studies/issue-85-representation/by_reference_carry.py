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

INDEX_SCHEMA = "issue85-by-reference-carry-index-v1"
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

# Stages whose actor a later stage consumes as its policy input. Only these need
# a worker-side producer binding; all other completed stages are
# authenticate-and-skip evidence.
POLICY_INPUT_PRODUCERS: tuple[tuple[str, int], ...] = (("C", 6), ("B", 7))

FIRST_UNFINISHED = ("C", 7)


def _formal_root(formal: Any) -> Path:
    return Path(formal.DATA_ROOT) / "issue85" / "artifacts" / "formal"


def _hash_json(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
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
        "update_receipt_sha256": update.get("receipt_sha256"),
        "predecessor_carry_manifest_sha256": update.get("predecessor_carry_manifest_sha256"),
        "actor_checkpoint_sha256": checkpoints["actor_checkpoint_sha256"],
        "optimizer_checkpoint_sha256": checkpoints["optimizer_checkpoint_sha256"],
        "source_model_digest": update.get("source_model_digest"),
        "result_model_digest": update.get("result_model_digest"),
        "owner_first": first,
        "owner_last": first + OWNERS_PER_BLOCK - 1,
    }


def build_index(formal: Any, identity: Mapping[str, str]) -> dict[str, Any]:
    """Resolve the full completed roster once, verifying every producer."""
    stages = [resolve_stage(formal, arm, block, g, u) for arm, block, g, u in STAGES]
    seen: set[tuple[str, int]] = set()
    owners: set[int] = set()
    for stage in stages:
        key = (stage["arm"], stage["block_index"])
        if key in seen:
            raise RuntimeError(f"duplicate completed stage {key}")
        seen.add(key)
        span = set(range(stage["owner_first"], stage["owner_last"] + 1))
        if owners & span:
            raise RuntimeError(f"completed stages overlap on owners at {key}")
        owners |= span
    if len(stages) != len(STAGES) or len(owners) != len(STAGES) * OWNERS_PER_BLOCK:
        raise RuntimeError("completed roster is not unique across registered owners")
    producers = sorted(
        {stage["games_package_commit"] for stage in stages}
        | {stage["update_package_commit"] for stage in stages}
    )
    unsigned = {
        "schema_id": INDEX_SCHEMA,
        "executing_package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "completed_stage_count": len(stages),
        "completed_train_outcomes": len(owners),
        "first_unfinished_arm": FIRST_UNFINISHED[0],
        "first_unfinished_block": FIRST_UNFINISHED[1],
        "producers": producers,
        "excluded_duplicate_package": EXCLUDED_DUPLICATE_PACKAGE,
        "stages": stages,
    }
    return {**unsigned, "index_sha256": _hash_json(unsigned)}


def validate_index(formal: Any, path: Path, identity: Mapping[str, str]) -> dict[str, Any]:
    """Re-verify a stored index against the live producer bytes."""
    index = formal.read_json(Path(path))
    unsigned = {key: value for key, value in index.items() if key != "index_sha256"}
    if (
        index.get("schema_id") != INDEX_SCHEMA
        or index.get("executing_package_commit") != identity["package_commit"]
        or index.get("source_lock_sha256") != identity["source_lock_sha256"]
        or index.get("index_sha256") != _hash_json(unsigned)
    ):
        raise RuntimeError(
            "by-reference carry index is malformed or bound to another executing package"
        )
    expected = build_index(formal, identity)
    if expected["index_sha256"] != index.get("index_sha256"):
        raise RuntimeError("by-reference carry index disagrees with the live producer evidence")
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
        }, sort_keys=True))
        return 0

    index = build_index(formal, identity)
    result = {
        "status": "BUILT_READ_ONLY_NO_GAMES",
        "executing_package_commit": identity["package_commit"],
        "index_sha256": index["index_sha256"],
        "completed_stage_count": index["completed_stage_count"],
        "completed_train_outcomes": index["completed_train_outcomes"],
        "first_unfinished": f"{index['first_unfinished_arm']}/{index['first_unfinished_block']:02d}",
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
