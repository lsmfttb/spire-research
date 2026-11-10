"""Production-shaped C/06 -> C/07 policy-load regression.

The successor package executes C/07 while the previous block's (C/06) actor,
optimizer and update receipt stay in the package that really produced them.
These tests read the *actual* durable C/06 receipt through the explicit
producer binding, and require every mutated binding to fail closed.

No C/07 game, optimizer step, or artifact write may occur here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

STUDY = Path(__file__).resolve().parents[1]
if str(STUDY) not in sys.path:
    sys.path.insert(0, str(STUDY))

import formal_execution as execution  # noqa: E402
import formal_runner as formal  # noqa: E402

PRODUCER_COMMIT = "2ecd996139778665d60a06f624b41e260c7b7358"
PRODUCER_CARRY_SHA256 = "741e5892880c58b081b621b2fde9087fae07e690e6891c3d04bbe0137b7703f7"
PRODUCER_GROUP_SHA256 = "833f72c4f9ee21086cec25b399f7c37c8dbba0dae4f2c45dde55732faceaebb7"
PRODUCER_LOCK_SHA256 = "a25c25cd4144cbe362c7c241460e9d71f4554627c9bb129407ef69ec8366404a"
C06_ACTOR_SHA256 = "26741730950c8daa9ed5aded9574906883053a602acd9bb17982fd30d0d46a31"
C06_OPTIMIZER_SHA256 = "14acbb8abe51efb8d920893e3eabc3d1cf853c7292f0515c9440909e08fd099e"
C06_SOURCE_MODEL_DIGEST = "a4124f493d487a4606fb58425493f10153de69be9ba931b83a7d4e0b7edff989"
C06_RESULT_MODEL_DIGEST = "a67e4bf6a316cf8e898cb3c6ded3c914a2c501ecd5fa631dab847a066f532dcb"
C07_FIRST_OWNER = 8400897
C07_LAST_OWNER = 8401024

EXECUTING_IDENTITY = {
    "package_commit": "0" * 40,
    "source_lock_sha256": PRODUCER_LOCK_SHA256,
}


def _producer(**overrides):
    record = {
        "package_commit": PRODUCER_COMMIT,
        "source_lock_sha256": PRODUCER_LOCK_SHA256,
        "predecessor_carry_manifest_sha256": PRODUCER_CARRY_SHA256,
        "group_receipt_sha256": PRODUCER_GROUP_SHA256,
    }
    record.update(overrides)
    return record


def _producer_package_root() -> Path:
    return (
        Path(formal.DATA_ROOT) / "issue85" / "artifacts" / "formal" / PRODUCER_COMMIT
    )


def _require_durable_c06() -> None:
    root = _producer_package_root()
    receipt = root / "training/C/updates/block-06/success-receipt.json"
    if not receipt.is_file():
        pytest.skip("durable C/06 update receipt is not present in this environment")


def test_c07_policy_load_reads_authenticated_c06_producer_without_writes(tmp_path):
    """C/07 resolves its previous block from the authenticated producer package."""
    _require_durable_c06()
    executing_root = tmp_path / "executing-package"
    executing_root.mkdir()

    receipt = execution._read_update_receipt(
        formal, executing_root, EXECUTING_IDENTITY,
        arm="C", block=6, producer=_producer(),
    )

    assert receipt["schema_id"] == execution.UPDATE_SCHEMA
    assert receipt["status"] == "SUCCEEDED"
    assert receipt["arm"] == "C" and receipt["block_index"] == 6
    assert receipt["package_commit"] == PRODUCER_COMMIT
    assert receipt["predecessor_carry_manifest_sha256"] == PRODUCER_CARRY_SHA256
    assert receipt["training_group_receipt_sha256"] == PRODUCER_GROUP_SHA256
    assert receipt["actor_checkpoint_sha256"] == C06_ACTOR_SHA256
    assert receipt["optimizer_checkpoint_sha256"] == C06_OPTIMIZER_SHA256
    assert receipt["completed_steps"] == 64
    # C/06 consumed C/05's result and produced the model C/07 must load.
    assert receipt["source_model_digest"] == C06_SOURCE_MODEL_DIGEST
    assert receipt["result_model_digest"] == C06_RESULT_MODEL_DIGEST
    # The next block's loader uses these two fields to find the real checkpoint.
    assert Path(receipt["producer_package_root"]) == _producer_package_root().resolve()
    assert receipt["carried_from_package_commit"] == PRODUCER_COMMIT
    assert receipt["carried_by_manifest_sha256"] == PRODUCER_CARRY_SHA256
    # Reading a policy input must never write into the executing package.
    assert list(executing_root.iterdir()) == []


def test_c07_policy_load_without_producer_fails_closed():
    """The unbound read stays rejected: the C/06 carry is not silently dropped."""
    _require_durable_c06()
    with pytest.raises(RuntimeError):
        execution._read_update_receipt(
            formal, Path(formal.DATA_ROOT), EXECUTING_IDENTITY, arm="C", block=6,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"predecessor_carry_manifest_sha256": "b" * 64},
        {"group_receipt_sha256": "c" * 64},
        {"package_commit": "9" * 40},
        {"source_lock_sha256": "d" * 64},
    ],
)
def test_c07_policy_load_rejects_wrong_producer_bindings(tmp_path, overrides):
    """Wrong producer identity, carry or group digest must fail closed."""
    _require_durable_c06()
    executing_root = tmp_path / "executing-package"
    executing_root.mkdir()
    with pytest.raises(RuntimeError):
        execution._read_update_receipt(
            formal, executing_root, EXECUTING_IDENTITY,
            arm="C", block=6, producer=_producer(**overrides),
        )
    assert list(executing_root.iterdir()) == []


def test_policy_producer_document_round_trip(tmp_path):
    """A well-formed bindings document is parsed; the executing package must match."""
    package_root = tmp_path / "package"
    package_root.mkdir()
    document = {
        "schema_id": execution.POLICY_PRODUCER_SCHEMA,
        "executing_package_commit": EXECUTING_IDENTITY["package_commit"],
        "source_lock_sha256": PRODUCER_LOCK_SHA256,
        "producers": {"C:6": _producer()},
    }
    (package_root / "policy-producers.json").write_text(json.dumps(document), encoding="utf-8")
    producers = execution._load_policy_producers(formal, package_root, EXECUTING_IDENTITY)
    assert set(producers) == {"C:6"}
    assert producers["C:6"]["package_commit"] == PRODUCER_COMMIT


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(executing_package_commit="f" * 40),
        lambda d: d.update(schema_id="other-schema"),
        lambda d: d["producers"].update({"C08": d["producers"]["C:6"]}),
        lambda d: d["producers"].update({"C:9": d["producers"]["C:6"]}),
        lambda d: d["producers"]["C:6"].update(group_receipt_sha256="short"),
    ],
)
def test_policy_producer_document_rejects_malformed_bindings(tmp_path, mutate):
    package_root = tmp_path / "package"
    package_root.mkdir()
    document = {
        "schema_id": execution.POLICY_PRODUCER_SCHEMA,
        "executing_package_commit": EXECUTING_IDENTITY["package_commit"],
        "source_lock_sha256": PRODUCER_LOCK_SHA256,
        "producers": {"C:6": _producer()},
    }
    mutate(document)
    (package_root / "policy-producers.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RuntimeError):
        execution._load_policy_producers(formal, package_root, EXECUTING_IDENTITY)


def test_missing_producer_document_means_self_contained(tmp_path):
    package_root = tmp_path / "package"
    package_root.mkdir()
    assert execution._load_policy_producers(formal, package_root, EXECUTING_IDENTITY) == {}


def test_c07_registered_roster_is_frozen_128_owners():
    """C/07 is the last C block: 128 contiguous owners 8400897..8401024."""
    _require_durable_c06()
    seed_path = _producer_package_root() / "formal-seed-table.json"
    if not seed_path.is_file():
        pytest.skip("durable seed table unavailable")
    seed = formal.read_json(seed_path)
    rows = execution._training_rows(seed, "C", 7)
    assert len(rows) == 128
    owners = sorted(int(row["owner_seed"]) for row in rows)
    assert owners == list(range(C07_FIRST_OWNER, C07_LAST_OWNER + 1))
    assert all(row["issue85_arm"] == "C" and row["block_index"] == 7 for row in rows)
