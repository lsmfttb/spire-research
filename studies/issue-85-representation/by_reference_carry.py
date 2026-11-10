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

Nothing here starts a game or an optimizer step; it only reads and verifies.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping

INDEX_SCHEMA = "issue85-by-reference-carry-index-v1"
POLICY_PRODUCER_SCHEMA = "issue85-policy-producers-v1"
OWNERS_PER_BLOCK = 128
TRAIN_FIRST_OWNER = 8400001

# Audited producer of every completed training block, in the order the study ran
# them. This is the fixed historical roster, not a per-successor exception list:
# it is resolved once, and every entry is re-verified against real bytes below.
PRODUCERS: tuple[tuple[str, int, str], ...] = (
    ("B", 0, "e549105ead79c9524bee706fc600164f8c7f9f1a"),
    ("B", 1, "318bdc800c5f986d67b445c38caeb13cebb154fb"),
    ("B", 2, "b8b3fc043671498628f96d378d9cecd7ac54106f"),
    ("B", 3, "8ac6fa868d4a81625304c7e9024db6e538b5a5b6"),
    ("B", 4, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("B", 5, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("B", 6, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("B", 7, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 0, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 1, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 2, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 3, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 4, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 5, "e902a953d55dfa5a2adbfbdaf0329370e9f2217a"),
    ("C", 6, "2ecd996139778665d60a06f624b41e260c7b7358"),
)

# The stage whose actor a later stage consumes as its policy input. Only these
# need a producer binding on the worker side; every other completed stage is
# authenticate-and-skip evidence.
POLICY_INPUT_PRODUCERS: tuple[tuple[str, int], ...] = (("C", 6), ("B", 7))

FIRST_UNFINISHED = ("C", 7)


def _formal_root(formal: Any) -> Path:
    return Path(formal.DATA_ROOT) / "issue85" / "artifacts" / "formal"


def _inside(root: Path, relative: str) -> Path:
    path = (Path(root) / relative).resolve(strict=True)
    try:
        path.relative_to(Path(root).resolve(strict=True))
    except ValueError as exc:  # pragma: no cover - defensive
        raise RuntimeError("by-reference evidence escapes its producer package") from exc
    return path


def _paid(receipt: Mapping[str, Any]) -> bool:
    return receipt.get("status") == "SUCCEEDED"


def resolve_stage(formal: Any, arm: str, block: int, producer_commit: str) -> dict[str, Any]:
    """Verify one completed stage in its real producer package and describe it.

    Every field returned is recomputed from bytes that exist under the producer
    root; nothing is trusted because a receipt merely asserts it.
    """
    root = _formal_root(formal) / producer_commit
    if not root.is_dir():
        raise RuntimeError(f"producer package is absent for {arm}/{block:02d}: {producer_commit}")

    group_rel = f"training/{arm}/block-{block:02d}/games/group-receipt.json"
    group_path = _inside(root, group_rel)
    group = formal.read_json(group_path)
    if (
        not _paid(group)
        or group.get("completed") != OWNERS_PER_BLOCK
        or group.get("expected") != OWNERS_PER_BLOCK
        or len(group.get("successful_unit_ids", [])) != OWNERS_PER_BLOCK
    ):
        raise RuntimeError(f"producer game group is not a complete {arm}/{block:02d} stage")

    update_rel = f"training/{arm}/updates/block-{block:02d}/success-receipt.json"
    update_path = _inside(root, update_rel)
    update = formal.read_json(update_path)
    if (
        not _paid(update)
        or update.get("arm") != arm
        or update.get("block_index") != block
        or update.get("package_commit") != producer_commit
        or update.get("owner_count") != OWNERS_PER_BLOCK
    ):
        raise RuntimeError(f"producer actor update is not a complete {arm}/{block:02d} stage")
    if update.get("training_group_receipt_sha256") != formal.file_sha256(group_path):
        raise RuntimeError(f"producer group digest disagrees for {arm}/{block:02d}")
    for field in ("actor_checkpoint_sha256", "optimizer_checkpoint_sha256"):
        value = update.get(field)
        if not isinstance(value, str) or len(value) != 64:
            raise RuntimeError(f"producer update lacks a valid {field} for {arm}/{block:02d}")

    first = TRAIN_FIRST_OWNER + block * OWNERS_PER_BLOCK
    return {
        "arm": arm,
        "block_index": block,
        "producer_package_commit": producer_commit,
        "group_receipt_path": group_rel,
        "group_receipt_sha256": formal.file_sha256(group_path),
        "update_receipt_path": update_rel,
        "update_receipt_file_sha256": formal.file_sha256(update_path),
        "update_receipt_sha256": update.get("receipt_sha256"),
        "predecessor_carry_manifest_sha256": update.get("predecessor_carry_manifest_sha256"),
        "actor_checkpoint_sha256": update["actor_checkpoint_sha256"],
        "optimizer_checkpoint_sha256": update["optimizer_checkpoint_sha256"],
        "source_model_digest": update.get("source_model_digest"),
        "result_model_digest": update.get("result_model_digest"),
        "owner_first": first,
        "owner_last": first + OWNERS_PER_BLOCK - 1,
    }


def build_index(formal: Any, identity: Mapping[str, str]) -> dict[str, Any]:
    """Resolve the full completed roster once, verifying every producer."""
    stages = [resolve_stage(formal, arm, block, commit) for arm, block, commit in PRODUCERS]
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
    if len(stages) != len(PRODUCERS) or len(owners) != len(PRODUCERS) * OWNERS_PER_BLOCK:
        raise RuntimeError("completed roster is not unique across registered owners")
    unsigned = {
        "schema_id": INDEX_SCHEMA,
        "executing_package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "completed_stage_count": len(stages),
        "completed_train_outcomes": len(owners),
        "first_unfinished_arm": FIRST_UNFINISHED[0],
        "first_unfinished_block": FIRST_UNFINISHED[1],
        "producers": sorted({stage["producer_package_commit"] for stage in stages}),
        "stages": stages,
    }
    return {**unsigned, "index_sha256": _hash_json(formal, unsigned)}


def _hash_json(formal: Any, value: Mapping[str, Any]) -> str:
    import hashlib
    import json

    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_index(formal: Any, path: Path, identity: Mapping[str, str]) -> dict[str, Any]:
    """Re-verify a stored index against the live producer bytes."""
    index = formal.read_json(Path(path))
    unsigned = {key: value for key, value in index.items() if key != "index_sha256"}
    if (
        index.get("schema_id") != INDEX_SCHEMA
        or index.get("executing_package_commit") != identity["package_commit"]
        or index.get("source_lock_sha256") != identity["source_lock_sha256"]
        or index.get("index_sha256") != _hash_json(formal, unsigned)
    ):
        raise RuntimeError("by-reference carry index is malformed or bound to another executing package")
    expected = build_index(formal, identity)
    if expected["index_sha256"] != index.get("index_sha256"):
        raise RuntimeError("by-reference carry index disagrees with the live producer evidence")
    return index


def policy_producers(formal: Any, index: Mapping[str, Any], identity: Mapping[str, str]) -> dict[str, Any]:
    """Worker-side bindings for the stages whose actor is consumed as a policy input."""
    by_key = {(s["arm"], s["block_index"]): s for s in index["stages"]}
    producers: dict[str, Any] = {}
    for arm, block in POLICY_INPUT_PRODUCERS:
        stage = by_key.get((arm, block))
        if stage is None:
            raise RuntimeError(f"policy input producer {arm}/{block:02d} is not a completed stage")
        producers[f"{arm}:{block}"] = {
            "package_commit": stage["producer_package_commit"],
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
    parser = argparse.ArgumentParser(description="Build/verify the Issue 85 by-reference carry index")
    parser.add_argument("--write", action="store_true", help="write the index and policy bindings into the executing package")
    parser.add_argument("--verify", action="store_true", help="re-verify an existing index instead of writing")
    args = parser.parse_args(argv)

    import formal_runner as formal

    identity = formal.pilot.source_identity(require_clean=True)
    if args.verify:
        path = _formal_root(formal) / identity["package_commit"] / "by-reference-carry-index.json"
        index = validate_index(formal, path, identity)
        print(formal.canonical_json({"status": "VERIFIED", "index_sha256": index["index_sha256"],
                                     "completed_stage_count": index["completed_stage_count"]}))
        return 0

    index = build_index(formal, identity)
    output = {
        "status": "BUILT_READ_ONLY_NO_GAMES",
        "executing_package_commit": identity["package_commit"],
        "index_sha256": index["index_sha256"],
        "completed_stage_count": index["completed_stage_count"],
        "completed_train_outcomes": index["completed_train_outcomes"],
        "first_unfinished": f"{index['first_unfinished_arm']}/{index['first_unfinished_block']:02d}",
        "producers": index["producers"],
    }
    if args.write:
        package_root = _formal_root(formal) / identity["package_commit"]
        if not package_root.is_dir():
            raise RuntimeError("executing package does not exist; run --preflight first")
        (package_root / "by-reference-carry-index.json").write_text(
            formal.canonical_json(index), encoding="utf-8"
        )
        (package_root / "policy-producers.json").write_text(
            formal.canonical_json(policy_producers(formal, index, identity)), encoding="utf-8"
        )
        output["written"] = [
            "by-reference-carry-index.json",
            "policy-producers.json",
        ]
    print(formal.canonical_json(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
