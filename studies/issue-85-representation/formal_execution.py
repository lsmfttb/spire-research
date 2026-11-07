"""Fail-closed formal executor for the registered Issue 85 cohorts.

This module is intentionally inert when imported. Game/update subprocesses can
only be started through formal_runner --execute after exact source/seed,
independent-review and owner GO/cap receipts have been checked.
"""
from __future__ import annotations

from contextvars import Token
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys
import tempfile
import time
import zlib
from typing import Any, Mapping, Sequence


AUTH_SCHEMA = "issue85-formal-execution-authorization-v1"
GROUP_SCHEMA = "issue85-formal-game-group-v1"
UPDATE_SCHEMA = "issue85-formal-actor-update-v1"
TEST_OPEN_SCHEMA = "issue85-formal-test-open-v1"
PROGRESS_SCHEMA = "issue85-formal-progress-v1"
PREDECESSOR_CARRY_SCHEMA = "issue85-formal-predecessor-carry-v1"
CARRIED_GROUP_REFERENCE_SCHEMA = "issue85-formal-carried-training-group-reference-v1"
PREDECESSOR_PACKAGE_COMMIT = "e549105ead79c9524bee706fc600164f8c7f9f1a"
PREDECESSOR_SOURCE_LOCK_SHA256 = "a25c25cd4144cbe362c7c241460e9d71f4554627c9bb129407ef69ec8366404a"
PREDECESSOR_SEED_TABLE_SHA256 = "6e304435485663e5fc8e95fadeeb771eea78cd01e5e1ff63caa2aba43c10a2de"
PREDECESSOR_GROUP_RECEIPT_SHA256 = "55a5466091c38b197fbe15c274d88dcf7a665390c70d155e02594ac8d448ae4a"
PREDECESSOR_PROGRESS_SHA256 = "499055e2a1382efc010653d88b8b4e2eed23388f9fdbda3467110055bf57262d"
PREDECESSOR_AUTHORIZATION_SHA256 = "96b202b8d5c86aa4fc81cc1077cf4e5d1f867ca28b05743fba716c3337ed89e0"
PREDECESSOR_UPDATE_FAILURE_LOG = "logs/formal-execution-20261106-resume01.log"
PREDECESSOR_UPDATE_FAILURE_LOG_SHA256 = "62c6b3554c536518f724a737749ec7c4af41ab6474fa88f9e6727d6922cb26c0"
PREDECESSOR_UPDATE_FAILURE_DETAIL = "module '__main__' has no attribute 'DATA_ROOT_WSL'"
PREDECESSOR_GROUP_ROOT = (
    "issue85/artifacts/formal/e549105ead79c9524bee706fc600164f8c7f9f1a/"
    "training/B/block-00/games"
)
RESUMED_GAMES_PACKAGE_COMMIT = "318bdc800c5f986d67b445c38caeb13cebb154fb"
RESUMED_GAMES_SOURCE_LOCK_SHA256 = PREDECESSOR_SOURCE_LOCK_SHA256
RESUMED_GAMES_SEED_TABLE_SHA256 = "eabbfa135330e2729d1c4e854c387afed1f61ad1f5b24b9f5c1d90a061e9c957"
RESUMED_GAMES_PROGRESS_SHA256 = "8ac79ed8bc8509c296f2a07ab08b52c4e0a1a2e9e6c6ed5333d0bf86b8c92d5e"
RESUMED_GAMES_AUTHORIZATION_SHA256 = "a739437e17e81f92318fc1a6bcf1218f064b0d6916e60e304f43358b5fa7507e"
RESUMED_GAMES_CARRY_SHA256 = "6c83e39421014ca2193668253ef3b204eb1f1551c120c6920da592c0f036d0ef"
RESUMED_B01_GROUP_RECEIPT_SHA256 = "480078361cd6bec04eabf07a7a875dbc08810dcf7999b3876d0fcfb4d069856c"
RESUMED_B01_GROUP_ROOT = (
    "issue85/artifacts/formal/318bdc800c5f986d67b445c38caeb13cebb154fb/"
    "training/B/block-01/games"
)
RESUMED_B01_UPDATE_RECEIPT_SHA256 = "301a74e4cca79e813eebdaa843c948acbdd864a24cab81c647a75d4dc6c32358"
RESUMED_B01_MODEL_DIGEST = "a272f206458ac60644805a9773a5eac66f7c7b2bcbf7be502bdc03a5939d6165"
RESUMED_B01_FORMAL_FAILURE = "formal training worker has an invalid arm/block/model label"
PRESERVED_ACTIVE_WALL_CARRY_PACKAGE_COMMIT = "fe33f0ccde3098b8b907d371321d2d0533112e86"
PRESERVED_ACTIVE_WALL_CARRY_MANIFEST_SHA256 = "e3066a4390c6d2b235723e26449f3ddc12a0a9e022930fe16c0f3f1d30c7a972"
PRESERVED_ACTIVE_WALL_CARRY_SECONDS = 34996.25197497001
RESUMED_B01_GROUP_CARRY = {
    "arm": "B",
    "block_index": 1,
    "stage_id": "TRAIN-B-BLOCK-01-GAMES",
    "package_commit": RESUMED_GAMES_PACKAGE_COMMIT,
    "source_lock_sha256": RESUMED_GAMES_SOURCE_LOCK_SHA256,
    "seed_table_sha256": RESUMED_GAMES_SEED_TABLE_SHA256,
    "formal_progress_sha256": RESUMED_GAMES_PROGRESS_SHA256,
    "authorization_receipt_sha256": RESUMED_GAMES_AUTHORIZATION_SHA256,
    "predecessor_carry_manifest_sha256": RESUMED_GAMES_CARRY_SHA256,
    "group_receipt_sha256": RESUMED_B01_GROUP_RECEIPT_SHA256,
    "group_root": RESUMED_B01_GROUP_ROOT,
    "expected": 128,
    "completed": 128,
    "failed_attempt_count": 0,
    "model_digest": RESUMED_B01_MODEL_DIGEST,
    "previous_update_receipt_sha256": RESUMED_B01_UPDATE_RECEIPT_SHA256,
}
RESUMED_FORMAL_PACKAGE_COMMIT = "b8b3fc043671498628f96d378d9cecd7ac54106f"
RESUMED_FORMAL_PACKAGE_ROOT = (
    "issue85/artifacts/formal/" + RESUMED_FORMAL_PACKAGE_COMMIT
)
RESUMED_FORMAL_SOURCE_LOCK_SHA256 = PREDECESSOR_SOURCE_LOCK_SHA256
RESUMED_FORMAL_SEED_TABLE_SHA256 = "6fd3f9b022c93d91de0f4c36a9b26b3e651a8a6c57f9a90e26f11e2d6efffc46"
RESUMED_FORMAL_PROGRESS_SHA256 = "d2d37be3f8421d4b10fbcb773f648b1ab6eb27425df505d4d11b360bcc3b0f14"
RESUMED_FORMAL_AUTHORIZATION_SHA256 = "ab532c36b5d8dd00ca05cc3b338e16b026d204a36998a2ab9245886e6ffd44c1"
RESUMED_FORMAL_CARRY_SHA256 = "83ff045730e9be827b9bb2762e16ddf75d0e304a59dee950486c83034ab0db43"
RESUMED_B02_GROUP_ROOT = RESUMED_FORMAL_PACKAGE_ROOT + "/training/B/block-02/games"
RESUMED_B02_GROUP_RECEIPT_SHA256 = "db787976261eb556b3e5ca0071a4b5e73ce85083a9545c8f6d9d79f2500786c2"
RESUMED_B01_UPDATE_RECEIPT_FILE_SHA256 = "4d8bee2d845beedbd710d0a8badf01412a7e26bc91871f8ec7d028b34509d21a"
RESUMED_B02_UPDATE_RECEIPT_FILE_SHA256 = "218d9e7ca5a38f97ea7fc2e661f87e1b4501f1900542fcf79ced27777bf5872e"
RESUMED_B03_GROUP_ROOT = RESUMED_FORMAL_PACKAGE_ROOT + "/training/B/block-03/games"
RESUMED_B03_LIVE_PROGRESS_PATH = RESUMED_FORMAL_PACKAGE_ROOT + "/live-training-B-03.json"
RESUMED_B03_LIVE_PROGRESS_SHA256 = "ca0579f756c1ca3d136b5b0ae59b65471b18ae9b681ffd5ad52b9e17fec56ee5"
RESUMED_FORMAL_CONTINUATION_SCHEMA = "issue85-formal-resumed-package-continuation-v2"
RESUMED_B03_COMPLETED = 33
RESUMED_B03_FAILED = 1
RESUMED_B03_PENDING = 94
RESUMED_B02_FAILED = 1
FORMAL_GO_COMMENT_URL = "https://github.com/lsmfttb/spire-research/issues/85#issuecomment-6686508621"
FORMAL_ACTIVE_WALL_CAP_SECONDS = 295632
PREDECESSOR_INITIAL_MODEL_DIGEST = "2891e60f6f9467c7f319a250e5789f4a0bfd89dcda17a60e8d416bcbdbd18908"
ACCEPTED_PILOT_PACKAGE_COMMIT = "5cd70c00d03f1104728efdc4b66c764592e5acf8"
ACCEPTED_PILOT_REPORT_SHA256 = "ea0c1025b6a4d87df66c7c55e629a8f2008b0d09f68b455b5e2017a376f6a606"
OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT = "fde456f52aaa388b7abc92a2b148ba91d3720e1e"
OPERATIONAL_PREDECESSOR_ROOT = (
    "issue85/artifacts/formal/fde456f52aaa388b7abc92a2b148ba91d3720e1e"
)
OPERATIONAL_PREDECESSOR_LOCK_SHA256 = PREDECESSOR_SOURCE_LOCK_SHA256
OPERATIONAL_PREDECESSOR_SEED_TABLE_SHA256 = "3784c3503a16ecc24da905f543486890c7d9afb8dc006dda8c2cb1320a5e6592"
OPERATIONAL_PREDECESSOR_PROGRESS_SHA256 = "d8827751b8bcbbe2c47ecc6bd4788691be8e303936e3679cdf1a58753e4eeab4"
OPERATIONAL_PREDECESSOR_AUTHORIZATION_SHA256 = "74cc91c8aee173af1554a3f5a4ec7df5e92d082474c6e072996b22152edd64e8"
OPERATIONAL_PREDECESSOR_CARRY_SHA256 = "0b418eb8f83396aa64cff43c1e36efc9c96725170ada9d61ba84c1d361eb9a70"
OPERATIONAL_PREDECESSOR_UPDATE_RECEIPT_FILE_SHA256 = "341f7937eb41ddcc999003493f1b47d84336257d18663c1271c35c70a86a46ae"
OPERATIONAL_PREDECESSOR_UPDATE_RECEIPT_SHA256 = "301a74e4cca79e813eebdaa843c948acbdd864a24cab81c647a75d4dc6c32358"
OPERATIONAL_PREDECESSOR_UPDATE_PROGRESS_SHA256 = "399a286cd1499cf0b8a3a3afd98d61de5f691436b16f3e920aed55b9d61fd8f4"
OPERATIONAL_PREDECESSOR_LIVE_B1_SHA256 = "49ca20d48e09b8012d56e58095d946a6b478e2dd41a3795c5ea0d169df8254e9"
OPERATIONAL_PREDECESSOR_UPDATE_FAILURE_DETAIL = (
    "formal execution must be launched inside the source-locked pinned WSL Python runtime"
)
OPERATIONAL_PREDECESSOR_UPDATE_SEED = 96940393
OPERATIONAL_PREDECESSOR_UPDATE_STEPS = 64
OPERATIONAL_PREDECESSOR_UPDATE_SCHEDULE_SHA256 = "22679fe29f5b7c518c5c5b7e7c22a99bd30c1dd5064540b4cc91162677492774"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(formal: Any, value: object) -> bytes:
    return formal.canonical(value)


def _hash_json(formal: Any, value: object) -> str:
    return formal.sha256_bytes(_canonical(formal, value))


def _atomic_json(formal: Any, path: Path, value: object) -> str:
    return formal.pilot._atomic_json(Path(path), value)


def _issue_comment_url(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"https://github\.com/lsmfttb/spire-research/issues/85#issuecomment-[1-9][0-9]*",
        value,
    ) is not None


def _resolves_to(value: Any, expected: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return Path(value).resolve(strict=True) == Path(expected).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False


def _validate_active_wall_clock(
    prior_clock: Mapping[str, Any], cap_seconds: int, *, require_idle: bool,
) -> float:
    elapsed = prior_clock.get("elapsed_seconds")
    process_ids = prior_clock.get("process_ids")
    if (
        prior_clock.get("schema_id") != "issue79-active-wall-clock-v1"
        or prior_clock.get("mode") != "issue85-task"
        or isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
        or not math.isfinite(float(elapsed)) or float(elapsed) < 0
        or float(elapsed) > cap_seconds
        or not isinstance(process_ids, list)
    ):
        raise RuntimeError("the cumulative Issue 85 active-wall receipt is malformed or exceeds the approved cap")
    live_process_ids: list[int] = []
    for pid in process_ids:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise RuntimeError("the cumulative Issue 85 active-wall process list contains an invalid PID")
        if not require_idle:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise RuntimeError(f"cannot verify prior Issue 85 process {pid}; refusing a duplicate run") from exc
        else:
            live_process_ids.append(pid)
    if require_idle and live_process_ids:
        raise RuntimeError(f"prior Issue 85 program remains active: pids={live_process_ids}")
    if require_idle and prior_clock.get("active_session") is None and process_ids:
        raise RuntimeError("the closed Issue 85 active-wall receipt still lists processes")
    return float(elapsed)


def _validated_active_wall_seconds(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"{label} is malformed or exceeds the approved active-wall cap")
    try:
        seconds = float(value)
    except (OverflowError, ValueError) as exc:
        raise RuntimeError(f"{label} is malformed or exceeds the approved active-wall cap") from exc
    if not math.isfinite(seconds) or seconds < 0 or seconds > FORMAL_ACTIVE_WALL_CAP_SECONDS:
        raise RuntimeError(f"{label} is malformed or exceeds the approved active-wall cap")
    return seconds


def _require_active_wall_high_water(
    current_seconds: Any, predecessor_seconds: Any, *, context: str,
) -> float:
    current = _validated_active_wall_seconds(current_seconds, label=f"{context} current clock")
    predecessor = _validated_active_wall_seconds(
        predecessor_seconds, label=f"{context} pinned predecessor progress",
    )
    if current < predecessor:
        raise RuntimeError(
            f"{context} active wall is below pinned predecessor high-water "
            f"({current} < {predecessor}); refusing to lower the cumulative budget floor"
        )
    return predecessor


def _preserved_active_wall_high_water(formal: Any) -> float:
    data_root = Path(formal.DATA_ROOT).resolve(strict=True)
    manifest_path = _inside(
        data_root,
        f"issue85/artifacts/formal/{PRESERVED_ACTIVE_WALL_CARRY_PACKAGE_COMMIT}/predecessor-carry-manifest.json",
    )
    if (
        not manifest_path.is_file()
        or formal.file_sha256(manifest_path) != PRESERVED_ACTIVE_WALL_CARRY_MANIFEST_SHA256
    ):
        raise RuntimeError("the pinned prior active-wall carry is missing or changed")
    manifest = formal.read_json(manifest_path)
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    successor = manifest.get("successor")
    carries = manifest.get("actor_update_carries")
    if (
        manifest.get("schema_id") != PREDECESSOR_CARRY_SCHEMA
        or manifest.get("status") != "VERIFIED_READ_ONLY_CONTINUATION"
        or not isinstance(successor, Mapping)
        or successor.get("package_commit") != PRESERVED_ACTIVE_WALL_CARRY_PACKAGE_COMMIT
        or successor.get("source_lock_sha256") != PREDECESSOR_SOURCE_LOCK_SHA256
        or manifest.get("manifest_sha256") != _hash_json(formal, unsigned)
        or not isinstance(carries, list)
        or len(carries) != 1
        or not isinstance(carries[0], Mapping)
    ):
        raise RuntimeError("the pinned prior active-wall carry is malformed or has changed lineage")
    snapshot = carries[0].get("active_wall_clock_snapshot")
    if not isinstance(snapshot, Mapping):
        raise RuntimeError("the pinned prior active-wall carry has no clock snapshot")
    snapshot_body = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    elapsed = _validated_active_wall_seconds(
        snapshot.get("elapsed_seconds"), label="pinned prior carry active-wall floor",
    )
    if (
        snapshot.get("snapshot_sha256") != _hash_json(formal, snapshot_body)
        or snapshot.get("active_session") is not None
        or snapshot.get("process_ids") != []
        or elapsed != PRESERVED_ACTIVE_WALL_CARRY_SECONDS
    ):
        raise RuntimeError("the pinned prior active-wall floor is malformed or differs from retained evidence")
    return elapsed


def _carried_active_wall_high_water(
    carry_manifest: Mapping[str, Any], cap_seconds: int,
) -> float | None:
    """Return the immutable predecessor clock floor for a carried Actor update."""
    carries = carry_manifest.get("actor_update_carries", [])
    if not isinstance(carries, list):
        raise RuntimeError("carried active-wall high-water record is malformed")
    if not carries:
        return None
    if len(carries) != 1 or not isinstance(carries[0], Mapping):
        raise RuntimeError("carried active-wall high-water record is malformed")
    snapshots = [carries[0].get("active_wall_clock_snapshot")]
    continuation = carry_manifest.get("resumed_formal_continuation")
    if isinstance(continuation, Mapping):
        snapshots.append(continuation.get("active_wall_clock_snapshot"))
    elapsed_values = []
    for snapshot in snapshots:
        elapsed = snapshot.get("elapsed_seconds") if isinstance(snapshot, Mapping) else None
        if (
            isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
            or not math.isfinite(float(elapsed))
            or float(elapsed) < 0
            or float(elapsed) > cap_seconds
        ):
            raise RuntimeError("immutable carried active-wall high-water is malformed or exceeds the approved cap")
        elapsed_values.append(float(elapsed))
    return max(elapsed_values)


def _predecessor_carry_path(package_root: Path) -> Path:
    return Path(package_root) / "predecessor-carry-manifest.json"


def _resumed_formal_continuation_required(package_commit: str) -> bool:
    return package_commit not in {
        OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT,
        RESUMED_GAMES_PACKAGE_COMMIT,
        RESUMED_FORMAL_PACKAGE_COMMIT,
    }


def _validate_resumed_formal_continuation_shape(
    formal: Any, continuation: Any,
) -> bool:
    if not isinstance(continuation, Mapping):
        return False
    b03_units = continuation.get("b03_successful_units")
    expected_successes = [
        *(f"TRAIN:{owner}:B" for owner in range(8400385, 8400417)),
        "TRAIN:8400418:B",
    ]
    failed = continuation.get("b03_failed_attempt")
    expected_failed_attempt_path = (
        Path(RESUMED_B03_GROUP_ROOT)
        .relative_to(Path(RESUMED_FORMAL_PACKAGE_ROOT))
        / "units/TRAIN-8400417-B/attempt-0001"
    ).as_posix()
    b01_update = continuation.get("b01_actor_update")
    b02_update = continuation.get("b02_actor_update")
    snapshot = continuation.get("active_wall_clock_snapshot")
    elapsed = snapshot.get("elapsed_seconds") if isinstance(snapshot, Mapping) else None

    def valid_update_reference(
        value: Any, *, block: int, receipt_file_sha256: str,
        training_group_receipt_sha256: str, training_data_package_commit: str,
    ) -> bool:
        return bool(
            isinstance(value, Mapping)
            and value.get("arm") == "B"
            and value.get("block_index") == block
            and value.get("source_package_commit") == RESUMED_FORMAL_PACKAGE_COMMIT
            and value.get("source_package_root") == RESUMED_FORMAL_PACKAGE_ROOT
            and value.get("source_lock_sha256") == RESUMED_FORMAL_SOURCE_LOCK_SHA256
            and value.get("source_update_receipt_file_sha256") == receipt_file_sha256
            and value.get("training_group_receipt_sha256") == training_group_receipt_sha256
            and value.get("training_data_package_commit") == training_data_package_commit
            and isinstance(value.get("source_update_receipt_path"), str)
            and isinstance(value.get("receipt_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["receipt_sha256"]) is not None
            and isinstance(value.get("actor_checkpoint_path"), str)
            and isinstance(value.get("actor_checkpoint_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["actor_checkpoint_sha256"]) is not None
            and isinstance(value.get("optimizer_checkpoint_path"), str)
            and isinstance(value.get("optimizer_checkpoint_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["optimizer_checkpoint_sha256"]) is not None
            and isinstance(value.get("result_model_digest"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["result_model_digest"]) is not None
        )

    return bool(
        continuation.get("schema_id") == RESUMED_FORMAL_CONTINUATION_SCHEMA
        and continuation.get("source_package_commit") == RESUMED_FORMAL_PACKAGE_COMMIT
        and continuation.get("source_package_root") == RESUMED_FORMAL_PACKAGE_ROOT
        and continuation.get("source_lock_sha256") == RESUMED_FORMAL_SOURCE_LOCK_SHA256
        and continuation.get("formal_seed_table_sha256") == RESUMED_FORMAL_SEED_TABLE_SHA256
        and continuation.get("formal_progress_sha256") == RESUMED_FORMAL_PROGRESS_SHA256
        and continuation.get("authorization_receipt_sha256") == RESUMED_FORMAL_AUTHORIZATION_SHA256
        and continuation.get("predecessor_carry_manifest_sha256") == RESUMED_FORMAL_CARRY_SHA256
        and continuation.get("b02_group_root") == RESUMED_B02_GROUP_ROOT
        and continuation.get("b02_group_receipt_sha256") == RESUMED_B02_GROUP_RECEIPT_SHA256
        and continuation.get("b01_update_receipt_file_sha256") == RESUMED_B01_UPDATE_RECEIPT_FILE_SHA256
        and continuation.get("b02_update_receipt_file_sha256") == RESUMED_B02_UPDATE_RECEIPT_FILE_SHA256
        and continuation.get("b03_live_progress_path") == RESUMED_B03_LIVE_PROGRESS_PATH
        and continuation.get("b03_live_progress_sha256") == RESUMED_B03_LIVE_PROGRESS_SHA256
        and continuation.get("b02_expected") == 128
        and continuation.get("b02_completed") == 128
        and continuation.get("b02_failed_attempt_count") == RESUMED_B02_FAILED
        and continuation.get("b03_expected") == 128
        and continuation.get("b03_completed") == RESUMED_B03_COMPLETED
        and continuation.get("b03_failed_attempt_count") == RESUMED_B03_FAILED
        and continuation.get("b03_pending") == RESUMED_B03_PENDING
        and isinstance(b03_units, list)
        and len(b03_units) == RESUMED_B03_COMPLETED
        and [item.get("unit_id") for item in b03_units if isinstance(item, Mapping)] == expected_successes
        and continuation.get("b03_units_sha256") == _hash_json(formal, b03_units)
        and valid_update_reference(
            b01_update, block=1,
            receipt_file_sha256=RESUMED_B01_UPDATE_RECEIPT_FILE_SHA256,
            training_group_receipt_sha256=RESUMED_B01_GROUP_RECEIPT_SHA256,
            training_data_package_commit=RESUMED_GAMES_PACKAGE_COMMIT,
        )
        and valid_update_reference(
            b02_update, block=2,
            receipt_file_sha256=RESUMED_B02_UPDATE_RECEIPT_FILE_SHA256,
            training_group_receipt_sha256=RESUMED_B02_GROUP_RECEIPT_SHA256,
            training_data_package_commit=RESUMED_FORMAL_PACKAGE_COMMIT,
        )
        and continuation.get("b02_update_receipt_sha256") == b02_update.get("receipt_sha256")
        and isinstance(failed, Mapping)
        and failed.get("unit_id") == "TRAIN:8400417:B"
        and failed.get("attempt") == "attempt-0001"
        and failed.get("attempt_path") == expected_failed_attempt_path
        and failed.get("status") == "PRE_START_AUTHORIZATION_FAILURE"
        and failed.get("failure_boundary") == "before_started_receipt"
        and type(failed.get("attempt_count")) is int
        and failed.get("attempt_count") == 1
        and failed.get("next_attempt") == "attempt-0002"
        and isinstance(failed.get("worker_job_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", failed["worker_job_sha256"]) is not None
        and isinstance(failed.get("worker_log_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", failed["worker_log_sha256"]) is not None
        and failed.get("started_receipt_absent") is True
        and failed.get("result_receipt_absent") is True
        and failed.get("finished_receipt_absent") is True
        and isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
        and math.isfinite(float(elapsed)) and 0 <= float(elapsed) <= FORMAL_ACTIVE_WALL_CAP_SECONDS
        and snapshot.get("active_session") is None
        and snapshot.get("process_ids") == []
        and isinstance(snapshot.get("source_active_wall_file_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", snapshot["source_active_wall_file_sha256"]) is not None
    )


def _validate_carry_manifest(
    formal: Any, package_root: Path, identity: Mapping[str, str], *,
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    path = _predecessor_carry_path(package_root)
    if not path.is_file():
        raise RuntimeError("verified predecessor carry manifest is missing; no formal work may start")
    manifest = formal.read_json(path)
    digest = formal.file_sha256(path)
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    predecessor = manifest.get("predecessor")
    successor = manifest.get("successor")
    failed_update = predecessor.get("failed_update") if isinstance(predecessor, Mapping) else None
    actor_update_carries = manifest.get("actor_update_carries", [])
    resumed_game_carries = manifest.get("resumed_training_group_carries", [])
    expected_actor_updates_carried = (
        0 if successor.get("package_commit") == OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT else 1
    ) if isinstance(successor, Mapping) else -1
    expected_resumed_game_carries = (
        [] if successor.get("package_commit") == OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT
        else [RESUMED_B01_GROUP_CARRY]
    ) if isinstance(successor, Mapping) else None
    needs_formal_continuation = (
        _resumed_formal_continuation_required(str(successor.get("package_commit", "")))
        if isinstance(successor, Mapping) else False
    )
    resumed_formal_continuation = manifest.get("resumed_formal_continuation")
    if (
        manifest.get("schema_id") != PREDECESSOR_CARRY_SCHEMA
        or manifest.get("status") != "VERIFIED_READ_ONLY_CONTINUATION"
        or manifest.get("manifest_sha256") != _hash_json(formal, unsigned)
        or (expected_sha256 is not None and digest != expected_sha256)
        or not isinstance(predecessor, Mapping)
        or not isinstance(successor, Mapping)
        or successor.get("package_commit") != identity.get("package_commit")
        or successor.get("source_lock_sha256") != identity.get("source_lock_sha256")
        or predecessor.get("package_commit") != PREDECESSOR_PACKAGE_COMMIT
        or predecessor.get("source_lock_sha256") != PREDECESSOR_SOURCE_LOCK_SHA256
        or predecessor.get("formal_seed_table_sha256") != PREDECESSOR_SEED_TABLE_SHA256
        or predecessor.get("group_receipt_sha256") != PREDECESSOR_GROUP_RECEIPT_SHA256
        or predecessor.get("formal_progress_sha256") != PREDECESSOR_PROGRESS_SHA256
        or predecessor.get("authorization_receipt_sha256") != PREDECESSOR_AUTHORIZATION_SHA256
        or predecessor.get("group_root") != PREDECESSOR_GROUP_ROOT
        or not isinstance(failed_update, Mapping)
        or failed_update.get("failure_type") != "AttributeError"
        or failed_update.get("failure_detail") != PREDECESSOR_UPDATE_FAILURE_DETAIL
        or failed_update.get("log_path") != PREDECESSOR_UPDATE_FAILURE_LOG
        or failed_update.get("log_sha256") != PREDECESSOR_UPDATE_FAILURE_LOG_SHA256
        or failed_update.get("formal_progress_sha256") != PREDECESSOR_PROGRESS_SHA256
        or manifest.get("owner_go_comment_url") != FORMAL_GO_COMMENT_URL
        or manifest.get("formal_games_carried") != int(manifest.get("validated_units", 0))
        or manifest.get("validated_units") != 128
        or expected_actor_updates_carried not in (0, 1)
        or manifest.get("actor_updates_carried") != expected_actor_updates_carried
        or not isinstance(actor_update_carries, list)
        or len(actor_update_carries) != expected_actor_updates_carried
        or resumed_game_carries != expected_resumed_game_carries
        or manifest.get("resumed_training_games_carried", 0) != (
            0 if expected_resumed_game_carries == [] else 128
        )
        or (resumed_formal_continuation is not None) != needs_formal_continuation
        or (needs_formal_continuation and not _validate_resumed_formal_continuation_shape(
            formal, resumed_formal_continuation,
        ))
        or manifest.get("resumed_formal_games_carried", 0) != (
            0 if not needs_formal_continuation else 128 + RESUMED_B03_COMPLETED
        )
        or manifest.get("resumed_formal_actor_updates_carried", 0) != (
            0 if not needs_formal_continuation else 2
        )
    ):
        raise RuntimeError("predecessor carry manifest is malformed or bound to another source/evidence")
    if actor_update_carries:
        _validate_operational_actor_update_carry(formal, actor_update_carries[0])
    return manifest, digest


def _predecessor_units_from_carry_manifest(
    formal: Any, manifest: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], backend: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reuse the pinned B8 unit evidence, checking its owner and consumption paths."""
    units = manifest.get("units")
    if (
        len(rows) != 128 or not isinstance(units, list) or len(units) != len(rows)
        or manifest.get("validated_units") != len(rows)
        or manifest.get("formal_games_carried") != len(rows)
        or manifest.get("features_loaded_by_pinned_issue84_parser") != len(rows)
        or manifest.get("initial_model_digest") != PREDECESSOR_INITIAL_MODEL_DIGEST
        or manifest.get("units_sha256") != _hash_json(formal, units)
    ):
        raise RuntimeError("pinned predecessor carry manifest has an incomplete unit or feature summary")

    outcomes: list[dict[str, Any]] = []
    for row, unit in zip(rows, units, strict=True):
        unit_id = str(row["unit_id"])
        owner_seed = int(row["owner_seed"])
        unit_key = backend.unit_key(unit_id)
        attempt_path = (
            Path("training/B/block-00/games/units") / unit_key / "attempt-0001"
        ).as_posix()
        feature_path = (Path("units") / unit_key / "attempt-0001" / "training-features.json.zlib").as_posix()
        digests = (
            unit.get("result_sha256"), unit.get("training_features_sha256"),
            unit.get("training_features_uncompressed_sha256"),
        )
        terminal_floor = unit.get("terminal_floor")
        heart_victory = unit.get("heart_victory")
        if (
            unit.get("unit_id") != unit_id or unit.get("owner_seed") != owner_seed
            or unit.get("attempt_path") != attempt_path
            or unit.get("training_features_path") != feature_path
            or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in digests)
            or unit.get("model_digest") != PREDECESSOR_INITIAL_MODEL_DIGEST
            or unit.get("complete") is not True or unit.get("expert_decisions") != 0
            or isinstance(terminal_floor, bool) or not isinstance(terminal_floor, (int, float))
            or not math.isfinite(float(terminal_floor)) or not isinstance(heart_victory, bool)
        ):
            raise RuntimeError(f"pinned predecessor carry unit identity or outcome differs: {unit_id}")
        outcomes.append({
            "unit_id": unit_id, "owner_seed": owner_seed,
            "terminal_floor": float(terminal_floor), "heart_victory": heart_victory,
        })
    if manifest.get("outcome_manifest_sha256") != _hash_json(formal, outcomes):
        raise RuntimeError("pinned predecessor carry outcome summary hash differs")
    return [dict(unit) for unit in units], outcomes


def _predecessor_training_group(
    formal: Any, package_root: Path, identity: Mapping[str, str], *,
    expected_carry_sha256: str | None = None,
) -> dict[str, Any]:
    manifest, carry_sha256 = _validate_carry_manifest(
        formal, package_root, identity, expected_sha256=expected_carry_sha256,
    )
    old_root = _inside(Path(formal.DATA_ROOT), PREDECESSOR_GROUP_ROOT)
    receipt_path = old_root / "group-receipt.json"
    if formal.file_sha256(receipt_path) != PREDECESSOR_GROUP_RECEIPT_SHA256:
        raise RuntimeError("immutable predecessor group receipt changed after carry verification")
    return {
        "schema_id": CARRIED_GROUP_REFERENCE_SCHEMA,
        "artifact_kind": "read-only predecessor receipt reference; no successor game receipt",
        "status": "SUCCEEDED",
        "stage_id": "TRAIN-B-BLOCK-00-GAMES",
        "package_commit": PREDECESSOR_PACKAGE_COMMIT,
        "training_data_package_commit": PREDECESSOR_PACKAGE_COMMIT,
        "source_lock_sha256": PREDECESSOR_SOURCE_LOCK_SHA256,
        "seed_table_sha256": PREDECESSOR_SEED_TABLE_SHA256,
        "authorization_receipt_sha256": PREDECESSOR_AUTHORIZATION_SHA256,
        "expected": 128,
        "completed": 128,
        "failed_attempt_count": 0,
        "receipt_path": (Path(PREDECESSOR_GROUP_ROOT) / "group-receipt.json").as_posix(),
        "receipt_sha256": PREDECESSOR_GROUP_RECEIPT_SHA256,
        "group_root": str(old_root),
        "predecessor_carry_manifest_path": _predecessor_carry_path(package_root).relative_to(package_root).as_posix(),
        "predecessor_carry_manifest_sha256": carry_sha256,
        "units_manifest_sha256": manifest["units_sha256"],
    }


def _load_pinned_predecessor_training_outcomes(
    formal: Any, backend: Any, issue84_driver: Any, rows: Sequence[Mapping[str, Any]], *,
    training_group: Mapping[str, Any], carry_manifest: Mapping[str, Any],
    carry_manifest_sha256: str, executor: Any, package: str,
) -> tuple[list[Any], list[Any]]:
    """Bind the pinned B8 unit hashes to the exact bytes read by the #84 loader."""
    group_root = _inside(Path(formal.DATA_ROOT), PREDECESSOR_GROUP_ROOT)
    try:
        recorded_group_root = Path(str(training_group.get("group_root", ""))).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("B/block-00 predecessor training root is missing or malformed") from exc
    if (
        training_group.get("schema_id") != CARRIED_GROUP_REFERENCE_SCHEMA
        or training_group.get("status") != "SUCCEEDED"
        or training_group.get("training_data_package_commit") != PREDECESSOR_PACKAGE_COMMIT
        or training_group.get("receipt_sha256") != PREDECESSOR_GROUP_RECEIPT_SHA256
        or training_group.get("receipt_path") != (
            Path(PREDECESSOR_GROUP_ROOT) / "group-receipt.json"
        ).as_posix()
        or training_group.get("predecessor_carry_manifest_sha256") != carry_manifest_sha256
        or training_group.get("units_manifest_sha256") != carry_manifest.get("units_sha256")
        or recorded_group_root != group_root
        or package != PREDECESSOR_PACKAGE_COMMIT
    ):
        raise RuntimeError("B/block-00 training inputs differ from the pinned predecessor carry")

    units, _outcomes = _predecessor_units_from_carry_manifest(formal, carry_manifest, rows, backend)
    result_bindings: dict[str, Mapping[str, Any]] = {}
    feature_bindings: dict[str, tuple[str, str]] = {}

    def absolute_key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    for unit in units:
        unit_id = unit.get("unit_id")
        feature_path = Path(str(unit.get("training_features_path", "")))
        result_path = group_root / feature_path.parent / "result.json"
        feature_file = group_root / feature_path
        result_key = absolute_key(result_path)
        feature_key = absolute_key(feature_file)
        if result_key in result_bindings or feature_key in feature_bindings:
            raise RuntimeError(f"pinned predecessor B/00 unit paths are duplicated: {unit_id}")
        result_bindings[result_key] = unit
        feature_bindings[feature_key] = (unit_id, str(unit["training_features_sha256"]))

    group_root_resolved = group_root.resolve(strict=True)
    group_root_absolute = Path(os.path.abspath(os.fspath(group_root)))

    def is_unit_artifact(path: Path) -> bool:
        try:
            relative = Path(os.path.abspath(os.fspath(path))).relative_to(group_root_absolute)
        except ValueError:
            return False
        return (
            len(relative.parts) == 4
            and relative.parts[0] == "units"
            and relative.parts[-2].startswith("attempt-")
            and relative.name in {"result.json", "training-features.json.zlib"}
        )

    class PinnedPredecessorPath(type(group_root)):
        __slots__ = ()

        def _assert_inside_group(self) -> None:
            try:
                Path(self).resolve(strict=True).relative_to(group_root_resolved)
            except (OSError, RuntimeError, ValueError) as exc:
                raise RuntimeError("B/block-00 training artifact escaped the pinned predecessor group") from exc

        def _verify_result(self, content: bytes, unit: Mapping[str, Any]) -> None:
            unit_id = str(unit["unit_id"])
            if formal.sha256_bytes(content) != unit["result_sha256"]:
                raise RuntimeError(f"pinned predecessor B/00 result bytes differ: {unit_id}")
            try:
                result = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"pinned predecessor B/00 result is malformed: {unit_id}") from exc
            pointer = result.get("training_features") if isinstance(result, Mapping) else None
            if (
                not isinstance(pointer, Mapping)
                or result.get("unit_id") != unit_id
                or result.get("owner_seed") != unit["owner_seed"]
                or result.get("status") != "SUCCEEDED"
                or result.get("package_commit") != PREDECESSOR_PACKAGE_COMMIT
                or result.get("source_lock_sha256") != PREDECESSOR_SOURCE_LOCK_SHA256
                or result.get("phase") != "training" or result.get("arm") != "TRAIN"
                or result.get("formal") is not True
                or pointer.get("compression") != "zlib"
                or pointer.get("path") != unit.get("training_features_path")
                or pointer.get("sha256") != unit.get("training_features_sha256")
                or pointer.get("uncompressed_sha256") != unit.get("training_features_uncompressed_sha256")
            ):
                raise RuntimeError(f"pinned predecessor B/00 result pointer differs: {unit_id}")

        def read_text(self, encoding: str | None = None, errors: str | None = None) -> str:
            key = absolute_key(self)
            unit = result_bindings.get(key)
            if unit is None:
                if is_unit_artifact(self) and self.name == "result.json":
                    raise RuntimeError("unexpected B/00 result file outside the pinned predecessor carry")
                return super().read_text(encoding=encoding, errors=errors)
            self._assert_inside_group()
            content = super().read_bytes()
            self._verify_result(content, unit)
            try:
                text = content.decode(encoding or "utf-8", errors or "strict")
                return text.replace("\r\n", "\n").replace("\r", "\n")
            except (LookupError, UnicodeDecodeError) as exc:
                raise RuntimeError(f"pinned predecessor B/00 result cannot be decoded: {unit['unit_id']}") from exc

        def read_bytes(self) -> bytes:
            self._assert_inside_group()
            content = super().read_bytes()
            key = absolute_key(self)
            unit = result_bindings.get(key)
            if unit is not None:
                self._verify_result(content, unit)
                return content
            feature = feature_bindings.get(key)
            if feature is not None:
                if formal.sha256_bytes(content) != feature[1]:
                    raise RuntimeError(f"pinned predecessor B/00 feature bytes differ: {feature[0]}")
                return content
            if is_unit_artifact(self) and self.name == "training-features.json.zlib":
                raise RuntimeError(f"unexpected B/00 training feature outside the pinned predecessor carry: {self}")
            return content

    return issue84_driver.load_training_outcomes(
        rows, root=PinnedPredecessorPath(group_root), executor=executor, package=package,
    )


def _resumed_b01_training_group(
    formal: Any, package_root: Path, identity: Mapping[str, str], *,
    expected_carry_sha256: str | None = None,
) -> dict[str, Any]:
    manifest, carry_sha256 = _validate_carry_manifest(
        formal, package_root, identity, expected_sha256=expected_carry_sha256,
    )
    carries = manifest.get("resumed_training_group_carries", [])
    if carries != [RESUMED_B01_GROUP_CARRY]:
        raise RuntimeError("B/block-01 games are not bound to the verified read-only continuation")
    old_root = _inside(Path(formal.DATA_ROOT), RESUMED_B01_GROUP_ROOT)
    receipt_path = old_root / "group-receipt.json"
    if formal.file_sha256(receipt_path) != RESUMED_B01_GROUP_RECEIPT_SHA256:
        raise RuntimeError("immutable resumed B/block-01 group receipt changed after carry verification")
    receipt = formal.read_json(receipt_path)
    if (
        receipt.get("schema_id") != GROUP_SCHEMA
        or receipt.get("status") != "SUCCEEDED"
        or receipt.get("stage_id") != "TRAIN-B-BLOCK-01-GAMES"
        or receipt.get("package_commit") != RESUMED_GAMES_PACKAGE_COMMIT
        or receipt.get("source_lock_sha256") != RESUMED_GAMES_SOURCE_LOCK_SHA256
        or receipt.get("seed_table_sha256") != RESUMED_GAMES_SEED_TABLE_SHA256
        or receipt.get("authorization_receipt_sha256") != RESUMED_GAMES_AUTHORIZATION_SHA256
        or receipt.get("expected") != 128
        or receipt.get("completed") != 128
        or receipt.get("failed_attempt_count") != 0
        or len(receipt.get("units", [])) != 128
    ):
        raise RuntimeError("resumed B/block-01 group receipt is incomplete or bound to other evidence")
    return {
        "schema_id": CARRIED_GROUP_REFERENCE_SCHEMA,
        "artifact_kind": "read-only resumed group reference; no successor game receipt",
        "status": "SUCCEEDED",
        "stage_id": "TRAIN-B-BLOCK-01-GAMES",
        "package_commit": RESUMED_GAMES_PACKAGE_COMMIT,
        "training_data_package_commit": RESUMED_GAMES_PACKAGE_COMMIT,
        "source_lock_sha256": RESUMED_GAMES_SOURCE_LOCK_SHA256,
        "seed_table_sha256": RESUMED_GAMES_SEED_TABLE_SHA256,
        "authorization_receipt_sha256": RESUMED_GAMES_AUTHORIZATION_SHA256,
        "expected": 128,
        "completed": 128,
        "failed_attempt_count": 0,
        "receipt_path": (Path(RESUMED_B01_GROUP_ROOT) / "group-receipt.json").as_posix(),
        "receipt_sha256": RESUMED_B01_GROUP_RECEIPT_SHA256,
        "group_root": str(old_root),
        "predecessor_carry_manifest_path": _predecessor_carry_path(package_root).relative_to(package_root).as_posix(),
        "predecessor_carry_manifest_sha256": carry_sha256,
        "units_manifest_sha256": _hash_json(formal, receipt["units"]),
    }


def _operational_predecessor_actor_update(
    formal: Any, *, active_wall_clock_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify the successful B/00 update in fde before carrying it to a source successor."""
    data_root = Path(formal.DATA_ROOT).resolve(strict=True)
    source_root = _inside(data_root, OPERATIONAL_PREDECESSOR_ROOT)
    progress_path = source_root / "formal-progress.json"
    authorization_path = source_root / "formal-execution-authorization.json"
    seed_path = source_root / "formal-seed-table.json"
    carry_path = _predecessor_carry_path(source_root)
    receipt_path = source_root / "training/B/updates/block-00/success-receipt.json"
    attempt_progress_path = source_root / "training/B/updates/block-00/attempt-0001/progress.json"
    live_b1_path = source_root / "live-training-B-01.json"
    pinned_hashes = (
        (progress_path, OPERATIONAL_PREDECESSOR_PROGRESS_SHA256, "formal progress"),
        (authorization_path, OPERATIONAL_PREDECESSOR_AUTHORIZATION_SHA256, "authorization"),
        (seed_path, OPERATIONAL_PREDECESSOR_SEED_TABLE_SHA256, "seed table"),
        (carry_path, OPERATIONAL_PREDECESSOR_CARRY_SHA256, "predecessor carry manifest"),
        (receipt_path, OPERATIONAL_PREDECESSOR_UPDATE_RECEIPT_FILE_SHA256, "B/00 update receipt"),
        (attempt_progress_path, OPERATIONAL_PREDECESSOR_UPDATE_PROGRESS_SHA256, "B/00 attempt progress"),
        (live_b1_path, OPERATIONAL_PREDECESSOR_LIVE_B1_SHA256, "B/01 live-game progress"),
    )
    for path, expected, label in pinned_hashes:
        if not path.is_file() or formal.file_sha256(path) != expected:
            raise RuntimeError(f"verified operational predecessor {label} changed: {path}")

    progress = formal.read_json(progress_path)
    live_b1 = formal.read_json(live_b1_path)
    if (
        progress.get("schema_id") != PROGRESS_SCHEMA
        or progress.get("status") != "FAILED_OR_PAUSED"
        or progress.get("package_commit") != OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT
        or progress.get("source_lock_sha256") != OPERATIONAL_PREDECESSOR_LOCK_SHA256
        or progress.get("seed_table_sha256") != OPERATIONAL_PREDECESSOR_SEED_TABLE_SHA256
        or progress.get("authorization_receipt_sha256") != OPERATIONAL_PREDECESSOR_AUTHORIZATION_SHA256
        or progress.get("active_stage") != "TRAIN-B-BLOCK-01-GAMES"
        or progress.get("failure", {}).get("type") != "RuntimeError"
        or progress.get("failure", {}).get("detail") != OPERATIONAL_PREDECESSOR_UPDATE_FAILURE_DETAIL
        or set(progress.get("game_groups", {})) != {"TRAIN-B-BLOCK-00"}
        or set(progress.get("actor_updates", {})) != {"B:0"}
        or progress["game_groups"]["TRAIN-B-BLOCK-00"].get("status") != "SUCCEEDED"
        or progress["game_groups"]["TRAIN-B-BLOCK-00"].get("completed") != 128
        or live_b1.get("status") != "RUNNING"
        or live_b1.get("expected") != 128
        or live_b1.get("successful_completions") != 0
        or live_b1.get("active_unit_ids") != []
        or len(live_b1.get("pending_unit_ids", [])) != 128
        or live_b1.get("recent_failures") != []
        or not isinstance(live_b1.get("attempt_counts"), Mapping)
        or len(live_b1["attempt_counts"]) != 128
        or any(value != 0 for value in live_b1["attempt_counts"].values())
    ):
        raise RuntimeError("operational predecessor is not paused immediately before B/01 games")

    b1_root = source_root / "training/B/block-01/games"
    if b1_root.exists() and any(b1_root.iterdir()):
        raise RuntimeError("operational predecessor has B/01 game attempts; refusing a duplicate continuation")

    source_identity = {
        "package_commit": OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT,
        "source_lock_sha256": OPERATIONAL_PREDECESSOR_LOCK_SHA256,
    }
    receipt = _read_update_receipt(formal, source_root, source_identity, arm="B", block=0)
    attempt_progress = formal.read_json(attempt_progress_path)
    optimizer_path = _inside(source_root, str(receipt.get("optimizer_checkpoint_path", "")))
    seed_table = formal.read_json(seed_path)
    training_rows = [
        row for row in seed_table.get("rows", [])
        if row.get("issue85_phase") == "training"
        and row.get("issue85_arm") == "B"
        and row.get("block_index") == 0
    ]
    prior_update = progress["actor_updates"]["B:0"]
    if (
        receipt.get("receipt_sha256") != OPERATIONAL_PREDECESSOR_UPDATE_RECEIPT_SHA256
        or receipt.get("status") != "SUCCEEDED"
        or receipt.get("completed_steps") != OPERATIONAL_PREDECESSOR_UPDATE_STEPS
        or receipt.get("actor_update_seed") != OPERATIONAL_PREDECESSOR_UPDATE_SEED
        or receipt.get("actor_update_schedule_sha256") != OPERATIONAL_PREDECESSOR_UPDATE_SCHEDULE_SHA256
        or receipt.get("seed_table_sha256") != OPERATIONAL_PREDECESSOR_SEED_TABLE_SHA256
        or receipt.get("source_lock_sha256") != OPERATIONAL_PREDECESSOR_LOCK_SHA256
        or receipt.get("training_data_package_commit") != PREDECESSOR_PACKAGE_COMMIT
        or receipt.get("predecessor_carry_manifest_sha256") != OPERATIONAL_PREDECESSOR_CARRY_SHA256
        or len(training_rows) != 128
        or _owner_hash(training_rows) != receipt.get("owner_seed_sha256")
        or formal.file_sha256(optimizer_path) != receipt.get("optimizer_checkpoint_sha256")
        or attempt_progress.get("status") != "SUCCEEDED"
        or attempt_progress.get("completed_steps") != OPERATIONAL_PREDECESSOR_UPDATE_STEPS
        or attempt_progress.get("actor_update_seed") != OPERATIONAL_PREDECESSOR_UPDATE_SEED
        or prior_update.get("status") != "SUCCEEDED"
        or prior_update.get("completed_steps") != OPERATIONAL_PREDECESSOR_UPDATE_STEPS
        or prior_update.get("receipt_sha256") != receipt.get("receipt_sha256")
        or prior_update.get("receipt_file_sha256") != receipt.get("receipt_file_sha256")
        or prior_update.get("actor_checkpoint_sha256") != receipt.get("actor_checkpoint_sha256")
        or prior_update.get("optimizer_checkpoint_sha256") != receipt.get("optimizer_checkpoint_sha256")
    ):
        raise RuntimeError("operational predecessor B/00 actor update receipt or checkpoint chain differs")

    if active_wall_clock_snapshot is None:
        clock_path = data_root / "issue85/artifacts/active-wall-issue85-task.json"
        clock = formal.read_json(clock_path)
        elapsed = _validate_active_wall_clock(
            clock, FORMAL_ACTIVE_WALL_CAP_SECONDS, require_idle=True,
        )
        snapshot_body = {
            "elapsed_seconds": elapsed,
            "active_session": clock.get("active_session"),
            "process_ids": clock.get("process_ids"),
            "receipt_file_sha256": formal.file_sha256(clock_path),
        }
        active_wall_clock_snapshot = {
            **snapshot_body,
            "snapshot_sha256": _hash_json(formal, snapshot_body),
        }
    snapshot = dict(active_wall_clock_snapshot)
    snapshot_hash = snapshot.pop("snapshot_sha256", None)
    snapshot_elapsed = _validated_active_wall_seconds(
        snapshot.get("elapsed_seconds"), label="carried active-wall pause snapshot",
    )
    operational_predecessor_elapsed = _validated_active_wall_seconds(
        progress.get("cumulative_active_wall_seconds"),
        label="pinned operational predecessor cumulative active wall",
    )
    if (
        snapshot_hash != _hash_json(formal, snapshot)
        or snapshot.get("active_session") is not None
        or snapshot.get("process_ids") != []
    ):
        raise RuntimeError("carried active-wall pause snapshot is malformed or exceeds the approved cap")
    _require_active_wall_high_water(
        snapshot_elapsed, operational_predecessor_elapsed,
        context="operational predecessor carry",
    )
    _require_active_wall_high_water(
        snapshot_elapsed, _preserved_active_wall_high_water(formal),
        context="preserved prior continuation carry",
    )

    return {
        "arm": "B",
        "block_index": 0,
        "source_package_commit": OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT,
        "source_package_root": OPERATIONAL_PREDECESSOR_ROOT,
        "source_lock_sha256": OPERATIONAL_PREDECESSOR_LOCK_SHA256,
        "source_seed_table_sha256": OPERATIONAL_PREDECESSOR_SEED_TABLE_SHA256,
        "source_formal_progress_path": "formal-progress.json",
        "source_formal_progress_sha256": OPERATIONAL_PREDECESSOR_PROGRESS_SHA256,
        "source_authorization_path": "formal-execution-authorization.json",
        "source_authorization_sha256": OPERATIONAL_PREDECESSOR_AUTHORIZATION_SHA256,
        "source_predecessor_carry_manifest_sha256": OPERATIONAL_PREDECESSOR_CARRY_SHA256,
        "source_update_receipt_path": "training/B/updates/block-00/success-receipt.json",
        "source_update_receipt_file_sha256": OPERATIONAL_PREDECESSOR_UPDATE_RECEIPT_FILE_SHA256,
        "source_update_attempt_progress_path": "training/B/updates/block-00/attempt-0001/progress.json",
        "source_update_attempt_progress_sha256": OPERATIONAL_PREDECESSOR_UPDATE_PROGRESS_SHA256,
        "actor_checkpoint_path": receipt["actor_checkpoint_path"],
        "actor_checkpoint_sha256": receipt["actor_checkpoint_sha256"],
        "optimizer_checkpoint_path": receipt["optimizer_checkpoint_path"],
        "optimizer_checkpoint_sha256": receipt["optimizer_checkpoint_sha256"],
        "receipt_sha256": receipt["receipt_sha256"],
        "actor_update_seed": receipt["actor_update_seed"],
        "actor_update_schedule_sha256": receipt["actor_update_schedule_sha256"],
        "owner_seed_sha256": receipt["owner_seed_sha256"],
        "completed_steps": receipt["completed_steps"],
        "result_model_digest": receipt["result_model_digest"],
        "active_wall_clock_snapshot": dict(active_wall_clock_snapshot),
    }


def _validate_operational_actor_update_carry(formal: Any, carry: Mapping[str, Any]) -> dict[str, Any]:
    expected = _operational_predecessor_actor_update(
        formal, active_wall_clock_snapshot=carry.get("active_wall_clock_snapshot"),
    )
    if dict(carry) != expected:
        raise RuntimeError("carried B/00 actor update differs from its immutable operational predecessor")
    return expected


def _verify_resumed_b01_training_group(
    formal: Any, backend: Any, issue84_driver: Any, *, seed: Mapping[str, Any],
    active_wall_clock_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the successful 318bdc B/01 games for read-only source continuation."""
    data_root = Path(formal.DATA_ROOT).resolve(strict=True)
    source_root = _inside(data_root, f"issue85/artifacts/formal/{RESUMED_GAMES_PACKAGE_COMMIT}")
    progress_path = source_root / "formal-progress.json"
    authorization_path = source_root / "formal-execution-authorization.json"
    seed_path = source_root / "formal-seed-table.json"
    carry_path = _predecessor_carry_path(source_root)
    group_root = _inside(data_root, RESUMED_B01_GROUP_ROOT)
    group_path = group_root / "group-receipt.json"
    for path, expected, label in (
        (progress_path, RESUMED_GAMES_PROGRESS_SHA256, "formal progress"),
        (authorization_path, RESUMED_GAMES_AUTHORIZATION_SHA256, "authorization"),
        (seed_path, RESUMED_GAMES_SEED_TABLE_SHA256, "seed table"),
        (carry_path, RESUMED_GAMES_CARRY_SHA256, "predecessor carry"),
        (group_path, RESUMED_B01_GROUP_RECEIPT_SHA256, "B/01 group receipt"),
    ):
        if not path.is_file() or formal.file_sha256(path) != expected:
            raise RuntimeError(f"read-only B/01 continuation {label} is missing or changed: {path}")

    old_seed = formal.read_json(seed_path)
    old_progress = formal.read_json(progress_path)
    authorization = formal.read_json(authorization_path)
    owner = authorization.get("owner_approval")
    reviewer = authorization.get("reviewer_approval")
    if (
        old_seed.get("schema_id") != "issue85-formal-seed-table-v1"
        or old_seed.get("package_commit") != RESUMED_GAMES_PACKAGE_COMMIT
        or old_seed.get("source_lock_sha256") != RESUMED_GAMES_SOURCE_LOCK_SHA256
        or old_seed.get("row_count") != 7808
        or old_seed.get("rows") != seed.get("rows")
        or old_seed.get("rows_sha256") != _hash_json(formal, old_seed.get("rows"))
        or old_seed.get("actor_update_schedule") != seed.get("actor_update_schedule")
        or old_seed.get("actor_update_schedule_sha256") != seed.get("actor_update_schedule_sha256")
        or old_progress.get("schema_id") != PROGRESS_SCHEMA
        or old_progress.get("status") != "FAILED_OR_PAUSED"
        or old_progress.get("package_commit") != RESUMED_GAMES_PACKAGE_COMMIT
        or old_progress.get("source_lock_sha256") != RESUMED_GAMES_SOURCE_LOCK_SHA256
        or old_progress.get("seed_table_sha256") != RESUMED_GAMES_SEED_TABLE_SHA256
        or old_progress.get("authorization_receipt_sha256") != RESUMED_GAMES_AUTHORIZATION_SHA256
        or old_progress.get("active_stage") != "TRAIN-B-BLOCK-01-ACTOR-UPDATE"
        or old_progress.get("failure") != {"type": "RuntimeError", "detail": RESUMED_B01_FORMAL_FAILURE}
        or authorization.get("schema_id") != AUTH_SCHEMA
        or authorization.get("status") != "APPROVED"
        or authorization.get("issue") != 85
        or authorization.get("formal_go") is not True
        or authorization.get("package_commit") != RESUMED_GAMES_PACKAGE_COMMIT
        or authorization.get("source_lock_sha256") != RESUMED_GAMES_SOURCE_LOCK_SHA256
        or authorization.get("seed_table_sha256") != RESUMED_GAMES_SEED_TABLE_SHA256
        or authorization.get("predecessor_carry_manifest_sha256") != RESUMED_GAMES_CARRY_SHA256
        or authorization.get("whole_task_active_wall_cap_seconds") != FORMAL_ACTIVE_WALL_CAP_SECONDS
        or not isinstance(owner, Mapping)
        or owner.get("status") != "APPROVED"
        or owner.get("decision") != "FORMAL_GO"
        or owner.get("comment_url") != FORMAL_GO_COMMENT_URL
        or not isinstance(reviewer, Mapping)
        or reviewer.get("status") != "APPROVED"
        or reviewer.get("decision") != "PASS"
        or reviewer.get("package_commit") != RESUMED_GAMES_PACKAGE_COMMIT
        or reviewer.get("source_lock_sha256") != RESUMED_GAMES_SOURCE_LOCK_SHA256
        or reviewer.get("seed_table_sha256") != RESUMED_GAMES_SEED_TABLE_SHA256
    ):
        raise RuntimeError("read-only B/01 continuation changed its authorized source, cohort, or stop boundary")

    if not isinstance(active_wall_clock_snapshot, Mapping):
        raise RuntimeError("read-only B/01 continuation is missing the verified active-wall carry snapshot")
    snapshot_elapsed = _validated_active_wall_seconds(
        active_wall_clock_snapshot.get("elapsed_seconds"),
        label="B/01 continuation active-wall carry snapshot",
    )
    _require_active_wall_high_water(
        snapshot_elapsed, old_progress.get("cumulative_active_wall_seconds"),
        context="pinned 318bdc B/01 progress carry",
    )
    _require_active_wall_high_water(
        snapshot_elapsed, _preserved_active_wall_high_water(formal),
        context="preserved prior continuation carry",
    )

    rows = _training_rows(seed, "B", 1)
    owners = list(range(8400129, 8400257))
    unit_ids = [f"TRAIN:{owner}:B" for owner in owners]
    prior_group = old_progress.get("game_groups", {}).get("TRAIN-B-BLOCK-01")
    prior_update = old_progress.get("actor_updates", {}).get("B:0")
    receipt = formal.read_json(group_path)
    if (
        len(rows) != 128
        or [int(row["owner_seed"]) for row in rows] != owners
        or [str(row["unit_id"]) for row in rows] != unit_ids
        or old_progress.get("actor_updates", {}).keys() != {"B:0"}
        or not isinstance(prior_update, Mapping)
        or prior_update.get("status") != "SUCCEEDED"
        or prior_update.get("completed_steps") != 64
        or prior_update.get("result_model_digest") != RESUMED_B01_MODEL_DIGEST
        or prior_update.get("receipt_sha256") != RESUMED_B01_UPDATE_RECEIPT_SHA256
        or not isinstance(prior_group, Mapping)
        or prior_group.get("status") != "SUCCEEDED"
        or prior_group.get("stage_id") != "TRAIN-B-BLOCK-01-GAMES"
        or prior_group.get("package_commit") != RESUMED_GAMES_PACKAGE_COMMIT
        or prior_group.get("source_lock_sha256") != RESUMED_GAMES_SOURCE_LOCK_SHA256
        or prior_group.get("seed_table_sha256") != RESUMED_GAMES_SEED_TABLE_SHA256
        or prior_group.get("authorization_receipt_sha256") != RESUMED_GAMES_AUTHORIZATION_SHA256
        or prior_group.get("expected") != 128 or prior_group.get("completed") != 128
        or prior_group.get("failed_attempt_count") != 0
        or prior_group.get("receipt_sha256") != RESUMED_B01_GROUP_RECEIPT_SHA256
        or prior_group.get("successful_unit_ids") != unit_ids
        or not isinstance(receipt.get("units"), list) or len(receipt["units"]) != 128
        or receipt.get("schema_id") != GROUP_SCHEMA
        or receipt.get("status") != "SUCCEEDED"
        or receipt.get("stage_id") != "TRAIN-B-BLOCK-01-GAMES"
        or receipt.get("package_commit") != RESUMED_GAMES_PACKAGE_COMMIT
        or receipt.get("source_lock_sha256") != RESUMED_GAMES_SOURCE_LOCK_SHA256
        or receipt.get("seed_table_sha256") != RESUMED_GAMES_SEED_TABLE_SHA256
        or receipt.get("authorization_receipt_sha256") != RESUMED_GAMES_AUTHORIZATION_SHA256
        or receipt.get("expected") != 128 or receipt.get("completed") != 128
        or receipt.get("failed_attempt_count") != 0
        or receipt.get("successful_unit_ids") != unit_ids
    ):
        raise RuntimeError("read-only B/01 progress and complete-game receipt are inconsistent")
    update_root = source_root / "training" / "B" / "updates" / "block-01"
    if update_root.exists() and any(update_root.iterdir()):
        raise RuntimeError("B/01 actor-update artifacts exist; refusing to skip or relabel the update")
    if {path.name for path in (group_root / "units").iterdir() if path.is_dir()} != {
        backend.unit_key(unit_id) for unit_id in unit_ids
    }:
        raise RuntimeError("resumed B/01 unit directories differ from its exact 128 registered owners")

    decoded: list[dict[str, Any]] = []
    for row, unit_receipt in zip(rows, receipt["units"], strict=True):
        unit_id = str(row["unit_id"])
        expected_attempt = Path("training/B/block-01/games/units") / backend.unit_key(unit_id) / "attempt-0001"
        if (
            unit_receipt.get("unit_id") != unit_id
            or unit_receipt.get("owner_seed") != int(row["owner_seed"])
            or unit_receipt.get("attempt_count") != 1
            or Path(str(unit_receipt.get("attempt_path", ""))) != expected_attempt
        ):
            raise RuntimeError(f"resumed B/01 unit identity/attempt count differs: {unit_id}")
        result, history = _unit_attempts(
            formal, backend, root=group_root, row=row,
            package_commit=RESUMED_GAMES_PACKAGE_COMMIT,
            lock_sha=RESUMED_GAMES_SOURCE_LOCK_SHA256,
            seed_table_sha256=RESUMED_GAMES_SEED_TABLE_SHA256,
            authorization_receipt_sha256=RESUMED_GAMES_AUTHORIZATION_SHA256,
            check_live_workers=False,
        )
        if result is None or history:
            raise RuntimeError(f"resumed B/01 owner is not a single complete success: {unit_id}")
        result_path = _inside(group_root, str(unit_receipt["attempt_path"]).removeprefix("training/B/block-01/games/")) / "result.json"
        policy_source = result.get("policy_source")
        pointer = result.get("training_features")
        if (
            formal.file_sha256(result_path) != unit_receipt.get("result_sha256")
            or unit_receipt.get("model_digest") != RESUMED_B01_MODEL_DIGEST
            or result.get("model_digest") != RESUMED_B01_MODEL_DIGEST
            or not isinstance(policy_source, Mapping)
            or policy_source.get("source") != "previous-registered-formal-block"
            or policy_source.get("previous_block") != 0
            or policy_source.get("parent_model_digest") != formal.LOCK["baseline_P"]["model_digest"]
            or policy_source.get("previous_update_receipt_sha256") != RESUMED_B01_UPDATE_RECEIPT_SHA256
            or result.get("expert_decisions") != 0
            or result.get("terminal_outcome") is None
            or isinstance(result.get("terminal_floor"), bool)
            or not isinstance(result.get("terminal_floor"), (int, float))
            or not math.isfinite(float(result["terminal_floor"]))
            or not isinstance(result.get("heart_victory"), bool)
            or result.get("native_source_commit") != formal.LOCK["native"]["commit"]
            or result.get("stsr_source_commit") != formal.LOCK["stsr_source"]["commit"]
            or result.get("native_module_sha256") != formal.LOCK["native"]["compiled_module_sha256"]
            or not isinstance(pointer, Mapping) or pointer.get("compression") != "zlib"
        ):
            raise RuntimeError(f"resumed B/01 terminal/model/source lineage differs: {unit_id}")
        relative_feature = Path(str(pointer.get("path", "")))
        expected_feature = expected_attempt.relative_to(Path("training/B/block-01/games")) / "training-features.json.zlib"
        if relative_feature.is_absolute() or ".." in relative_feature.parts or relative_feature != expected_feature:
            raise RuntimeError(f"resumed B/01 feature path differs from its attempt: {unit_id}")
        compressed = _inside(group_root, relative_feature.as_posix()).read_bytes()
        raw = zlib.decompress(compressed)
        if (
            formal.sha256_bytes(compressed) != pointer.get("sha256")
            or len(compressed) != pointer.get("stored_bytes")
            or formal.sha256_bytes(raw) != pointer.get("uncompressed_sha256")
            or len(raw) != pointer.get("uncompressed_bytes")
        ):
            raise RuntimeError(f"resumed B/01 feature bytes/hash/length differ: {unit_id}")
        payload = json.loads(raw.decode("utf-8"))
        base = payload.get("base") if isinstance(payload, Mapping) else None
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_id") != "issue75-stage1-training-features-v1"
            or payload.get("unit_id") != unit_id
            or payload.get("owner_seed") != int(row["owner_seed"])
            or payload.get("package_commit") != RESUMED_GAMES_PACKAGE_COMMIT
            or payload.get("source_lock_sha256") != RESUMED_GAMES_SOURCE_LOCK_SHA256
            or not isinstance(base, Mapping) or base.get("complete") is not True
            or base.get("terminal_floor") != result.get("terminal_floor")
            or base.get("heart_win") is not result.get("heart_victory")
        ):
            raise RuntimeError(f"resumed B/01 training-feature lineage differs: {unit_id}")
        decoded.append({
            "unit_id": unit_id, "owner_seed": int(row["owner_seed"]),
            "terminal_floor": float(result["terminal_floor"]),
            "heart_victory": bool(result["heart_victory"]),
        })

    with tempfile.TemporaryDirectory(prefix="issue85-resumed-b01-carry-") as temporary_root:
        loaded = backend.load_modules(Path(temporary_root), pilot=True)
        examples, outcomes = issue84_driver.load_training_outcomes(
            rows, root=group_root, executor=loaded[-1], package=RESUMED_GAMES_PACKAGE_COMMIT,
        )
    if len(examples) != 128 or len(outcomes) != 128:
        raise RuntimeError("pinned #84 loader did not read all 128 resumed B/01 feature records")
    for expected, example, outcome in zip(decoded, examples, outcomes, strict=True):
        base = getattr(example, "base", None)
        if (
            int(getattr(example, "owner_seed", -1)) != expected["owner_seed"]
            or not bool(getattr(base, "complete", False))
            or float(getattr(base, "terminal_floor", math.nan)) != expected["terminal_floor"]
            or bool(getattr(base, "heart_win", False)) != expected["heart_victory"]
            or int(getattr(outcome, "owner_seed", -1)) != expected["owner_seed"]
            or not bool(getattr(outcome, "complete", False))
            or float(getattr(outcome, "terminal_floor", math.nan)) != expected["terminal_floor"]
            or bool(getattr(outcome, "heart_win", False)) != expected["heart_victory"]
        ):
            raise RuntimeError(f"pinned #84 loader changed resumed B/01 outcome: {expected['unit_id']}")
    return dict(RESUMED_B01_GROUP_CARRY)


def verify_predecessor_carry(
    formal: Any, *, issue84_source: Path, identity: Mapping[str, str],
    readiness: Mapping[str, Any], seed: Mapping[str, Any], package_root: Path,
    write_manifest: bool, backend: Any | None = None,
    issue84_driver: Any | None = None,
) -> dict[str, Any]:
    """Hash-verify carried e549 games, the fde B/00 update, and the pinned feature loader."""
    if (
        identity.get("source_lock_sha256") != PREDECESSOR_SOURCE_LOCK_SHA256
        or formal.file_sha256(formal.LOCK_PATH) != PREDECESSOR_SOURCE_LOCK_SHA256
        or readiness.get("package_commit") != identity.get("package_commit")
        or readiness.get("source_lock_sha256") != PREDECESSOR_SOURCE_LOCK_SHA256
        or seed.get("package_commit") != identity.get("package_commit")
        or seed.get("source_lock_sha256") != PREDECESSOR_SOURCE_LOCK_SHA256
        or seed.get("actor_update_schedule_sha256") != "22679fe29f5b7c518c5c5b7e7c22a99bd30c1dd5064540b4cc91162677492774"
    ):
        raise RuntimeError("successor source/readiness/seed does not preserve the reviewed Issue 85 contract")
    new_rows = seed.get("rows")
    if not isinstance(new_rows, list) or len(new_rows) != 7808:
        raise RuntimeError("successor seed table has an unexpected registered row count")

    data_root = Path(formal.DATA_ROOT).resolve(strict=True)
    old_package_root = _inside(data_root, f"issue85/artifacts/formal/{PREDECESSOR_PACKAGE_COMMIT}")
    old_seed_path = old_package_root / "formal-seed-table.json"
    old_progress_path = old_package_root / "formal-progress.json"
    old_auth_path = old_package_root / "formal-execution-authorization-corrected.json"
    old_failure_log_path = old_package_root / PREDECESSOR_UPDATE_FAILURE_LOG
    old_group_root = _inside(data_root, PREDECESSOR_GROUP_ROOT)
    old_group_path = old_group_root / "group-receipt.json"
    expected_files = (
        (old_seed_path, PREDECESSOR_SEED_TABLE_SHA256, "predecessor seed table"),
        (old_progress_path, PREDECESSOR_PROGRESS_SHA256, "predecessor formal progress"),
        (old_auth_path, PREDECESSOR_AUTHORIZATION_SHA256, "predecessor source-bound authorization"),
        (old_failure_log_path, PREDECESSOR_UPDATE_FAILURE_LOG_SHA256, "predecessor failed-update log"),
        (old_group_path, PREDECESSOR_GROUP_RECEIPT_SHA256, "predecessor game group receipt"),
    )
    for path, digest, label in expected_files:
        if not path.is_file() or formal.file_sha256(path) != digest:
            raise RuntimeError(f"{label} is missing or changed; refusing to rerun predecessor owners")

    old_seed = formal.read_json(old_seed_path)
    if (
        old_seed.get("schema_id") != "issue85-formal-seed-table-v1"
        or old_seed.get("package_commit") != PREDECESSOR_PACKAGE_COMMIT
        or old_seed.get("source_lock_sha256") != PREDECESSOR_SOURCE_LOCK_SHA256
        or old_seed.get("contract_sha256") != formal.file_sha256(formal.CONTRACT_PATH)
        or old_seed.get("row_count") != 7808
        or old_seed.get("rows") != new_rows
        or old_seed.get("rows_sha256") != _hash_json(formal, old_seed.get("rows"))
        or old_seed.get("actor_update_schedule") != seed.get("actor_update_schedule")
        or old_seed.get("actor_update_schedule_sha256") != seed.get("actor_update_schedule_sha256")
    ):
        raise RuntimeError("predecessor and successor seed rows/schedule differ")
    rows = _training_rows(seed, "B", 0)
    owners = [int(row["owner_seed"]) for row in rows]
    unit_ids = [str(row["unit_id"]) for row in rows]
    if (
        len(rows) != 128 or owners != list(range(8400001, 8400129))
        or len(set(owners)) != 128 or len(set(unit_ids)) != 128
        or any(
            row.get("formal") is not True or row.get("phase") != "training"
            or row.get("issue85_phase") != "training" or row.get("issue85_arm") != "B"
            or row.get("issue84_arm") != "B" or row.get("issue84_policy_label") != "pi12"
            or row.get("unit_id") != f"TRAIN:{row.get('owner_seed')}:B"
            for row in rows
        )
    ):
        raise RuntimeError("registered successor B/block-00 owner/unit identities are not exact and unique")

    old_auth = formal.read_json(old_auth_path)
    old_owner = old_auth.get("owner_approval")
    old_reviewer = old_auth.get("reviewer_approval")
    if (
        old_auth.get("schema_id") != AUTH_SCHEMA or old_auth.get("issue") != 85
        or old_auth.get("formal_go") is not True or old_auth.get("status") != "APPROVED"
        or old_auth.get("package_commit") != PREDECESSOR_PACKAGE_COMMIT
        or old_auth.get("source_lock_sha256") != PREDECESSOR_SOURCE_LOCK_SHA256
        or old_auth.get("seed_table_sha256") != PREDECESSOR_SEED_TABLE_SHA256
        or old_auth.get("actor_update_schedule_sha256") != seed.get("actor_update_schedule_sha256")
        or old_auth.get("whole_task_active_wall_cap_seconds") != FORMAL_ACTIVE_WALL_CAP_SECONDS
        or not isinstance(old_owner, Mapping) or old_owner.get("status") != "APPROVED"
        or old_owner.get("decision") != "FORMAL_GO"
        or old_owner.get("comment_url") != FORMAL_GO_COMMENT_URL
        or not isinstance(old_reviewer, Mapping) or old_reviewer.get("status") != "APPROVED"
        or old_reviewer.get("decision") != "PASS"
        or old_reviewer.get("package_commit") != PREDECESSOR_PACKAGE_COMMIT
        or old_reviewer.get("source_lock_sha256") != PREDECESSOR_SOURCE_LOCK_SHA256
    ):
        raise RuntimeError("predecessor authorization does not trace to the same owner FORMAL_GO and approved source")
    active_wall_path = data_root / "issue85" / "artifacts" / "active-wall-issue85-task.json"
    if not active_wall_path.is_file():
        raise RuntimeError("the authoritative Issue 85 clock is missing; predecessor carry cannot be verified")
    _validate_active_wall_clock(
        formal.read_json(active_wall_path), FORMAL_ACTIVE_WALL_CAP_SECONDS, require_idle=True,
    )
    prior_progress = formal.read_json(old_progress_path)
    prior_group = prior_progress.get("game_groups", {}).get("TRAIN-B-BLOCK-00")
    group_receipt = formal.read_json(old_group_path)
    if (
        prior_progress.get("schema_id") != PROGRESS_SCHEMA
        or prior_progress.get("status") != "FAILED_OR_PAUSED"
        or prior_progress.get("package_commit") != PREDECESSOR_PACKAGE_COMMIT
        or prior_progress.get("source_lock_sha256") != PREDECESSOR_SOURCE_LOCK_SHA256
        or prior_progress.get("seed_table_sha256") != PREDECESSOR_SEED_TABLE_SHA256
        or prior_progress.get("authorization_receipt_sha256") != PREDECESSOR_AUTHORIZATION_SHA256
        or prior_progress.get("active_stage") != "TRAIN-B-BLOCK-00-ACTOR-UPDATE"
        or prior_progress.get("failure", {}).get("type") != "AttributeError"
        or prior_progress.get("failure", {}).get("detail") != PREDECESSOR_UPDATE_FAILURE_DETAIL
        or PREDECESSOR_UPDATE_FAILURE_DETAIL not in old_failure_log_path.read_text(encoding="utf-8", errors="replace")
        or prior_progress.get("actor_updates") != {}
        or not isinstance(prior_group, Mapping)
        or prior_group.get("schema_id") != GROUP_SCHEMA or prior_group.get("status") != "SUCCEEDED"
        or prior_group.get("stage_id") != "TRAIN-B-BLOCK-00-GAMES"
        or prior_group.get("package_commit") != PREDECESSOR_PACKAGE_COMMIT
        or prior_group.get("source_lock_sha256") != PREDECESSOR_SOURCE_LOCK_SHA256
        or prior_group.get("seed_table_sha256") != PREDECESSOR_SEED_TABLE_SHA256
        or prior_group.get("authorization_receipt_sha256") != PREDECESSOR_AUTHORIZATION_SHA256
        or prior_group.get("expected") != 128 or prior_group.get("completed") != 128
        or prior_group.get("failed_attempt_count") != 0
        or prior_group.get("group_root") != str(old_group_root)
        or prior_group.get("receipt_sha256") != PREDECESSOR_GROUP_RECEIPT_SHA256
        or not isinstance(group_receipt.get("units"), list)
        or len(group_receipt["units"]) != 128
        or group_receipt.get("schema_id") != GROUP_SCHEMA
        or group_receipt.get("status") != "SUCCEEDED"
        or group_receipt.get("stage_id") != "TRAIN-B-BLOCK-00-GAMES"
        or group_receipt.get("package_commit") != PREDECESSOR_PACKAGE_COMMIT
        or group_receipt.get("source_lock_sha256") != PREDECESSOR_SOURCE_LOCK_SHA256
        or group_receipt.get("seed_table_sha256") != PREDECESSOR_SEED_TABLE_SHA256
        or group_receipt.get("authorization_receipt_sha256") != PREDECESSOR_AUTHORIZATION_SHA256
        or group_receipt.get("completed") != 128
        or prior_group.get("successful_unit_ids") != unit_ids
        or not _resolves_to(prior_group.get("receipt_path"), old_group_path)
        or prior_group.get("receipt_sha256") != formal.file_sha256(old_group_path)
    ):
        raise RuntimeError("predecessor progress/group receipt is incomplete, inconsistent, or already updated")
    update_root = old_package_root / "training" / "B" / "updates" / "block-00"
    if update_root.exists() and any(update_root.iterdir()):
        raise RuntimeError("predecessor Actor-update artifacts exist; carry would skip or relabel a real update")
    current_root = Path(package_root).resolve(strict=True)
    current_progress = current_root / "formal-progress.json"
    if not _predecessor_carry_path(current_root).is_file() and current_progress.exists():
        raise RuntimeError("successor execution began before the predecessor carry manifest was fixed")
    successor_games_root = current_root / "training" / "B" / "block-00" / "games"
    if successor_games_root.exists() and any(successor_games_root.iterdir()):
        raise RuntimeError("successor B/block-00 game attempts already exist; refusing duplicate registered owners")

    if backend is None or issue84_driver is None:
        backend, issue84_driver = _configure_formal_runtime(
            formal, Path(issue84_source).resolve(strict=True), identity, current_root,
        )
    resumed_source_root = _inside(data_root, RESUMED_FORMAL_PACKAGE_ROOT)
    inherited_carry, _inherited_carry_sha256 = _validate_carry_manifest(
        formal, resumed_source_root,
        {
            "package_commit": RESUMED_FORMAL_PACKAGE_COMMIT,
            "source_lock_sha256": RESUMED_FORMAL_SOURCE_LOCK_SHA256,
        },
        expected_sha256=RESUMED_FORMAL_CARRY_SHA256,
    )
    if (
        inherited_carry.get("pinned_issue84_source_commit")
        != formal.LOCK["baseline_A"]["issue84_final_accepted_source_commit"]
        or inherited_carry.get("parent_model_digest") != formal.LOCK["baseline_P"]["model_digest"]
        or inherited_carry.get("parent_checkpoint_sha256") != formal.LOCK["baseline_P"]["checkpoint_sha256"]
        or inherited_carry.get("native_source_commit") != formal.LOCK["native"]["commit"]
        or inherited_carry.get("stsr_source_commit") != formal.LOCK["stsr_source"]["commit"]
    ):
        raise RuntimeError("pinned predecessor carry manifest differs from the locked model or source lineage")
    # B8 already validated these immutable B/00 results and features with the pinned #84 parser.
    # The actual B/00 update below consumes the referenced features and verifies their hashes there.
    unit_manifest, decoded_features = _predecessor_units_from_carry_manifest(
        formal, inherited_carry, rows, backend,
    )

    manifest_path = _predecessor_carry_path(current_root)
    stored_actor_update_snapshot = None
    if manifest_path.is_file():
        stored_manifest = formal.read_json(manifest_path)
        stored_carries = stored_manifest.get("actor_update_carries", [])
        if isinstance(stored_carries, list) and len(stored_carries) == 1 and isinstance(stored_carries[0], Mapping):
            stored_actor_update_snapshot = stored_carries[0].get("active_wall_clock_snapshot")
    actor_update_carries = (
        [] if identity["package_commit"] == OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT
        else [_operational_predecessor_actor_update(
            formal, active_wall_clock_snapshot=stored_actor_update_snapshot,
        )]
    )
    if identity["package_commit"] == OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT:
        resumed_training_group_carries = []
    else:
        if len(actor_update_carries) != 1:
            raise RuntimeError("resumed B/01 games require the same validated active-wall carry snapshot")
        resumed_training_group_carries = [_verify_resumed_b01_training_group(
            formal, backend, issue84_driver, seed=seed,
            active_wall_clock_snapshot=actor_update_carries[0].get("active_wall_clock_snapshot"),
        )]
    resumed_formal_continuation = None
    if _resumed_formal_continuation_required(str(identity["package_commit"])):
        stored_continuation = None
        if manifest_path.is_file():
            old_manifest = formal.read_json(manifest_path)
            stored_continuation = old_manifest.get("resumed_formal_continuation")
        snapshot = (
            stored_continuation.get("active_wall_clock_snapshot")
            if isinstance(stored_continuation, Mapping) else None
        )
        resumed_formal_continuation = _build_resumed_formal_continuation(
            formal, issue84_source=issue84_source, seed=seed,
            backend=backend, issue84_driver=issue84_driver,
            active_wall_clock_snapshot=snapshot,
        )
    manifest: dict[str, Any] = {
        "schema_id": PREDECESSOR_CARRY_SCHEMA,
        "status": "VERIFIED_READ_ONLY_CONTINUATION",
        "successor": {
            "package_commit": identity["package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "formal_seed_table_sha256": formal.file_sha256(package_root / "formal-seed-table.json"),
            "actor_update_schedule_sha256": seed["actor_update_schedule_sha256"],
        },
        "predecessor": {
            "package_commit": PREDECESSOR_PACKAGE_COMMIT,
            "source_lock_sha256": PREDECESSOR_SOURCE_LOCK_SHA256,
            "formal_seed_table_sha256": PREDECESSOR_SEED_TABLE_SHA256,
            "formal_progress_sha256": PREDECESSOR_PROGRESS_SHA256,
            "authorization_receipt_sha256": PREDECESSOR_AUTHORIZATION_SHA256,
            "group_receipt_sha256": PREDECESSOR_GROUP_RECEIPT_SHA256,
            "group_root": PREDECESSOR_GROUP_ROOT,
            "failed_update": {
                "failure_type": "AttributeError",
                "failure_detail": PREDECESSOR_UPDATE_FAILURE_DETAIL,
                "log_path": PREDECESSOR_UPDATE_FAILURE_LOG,
                "log_sha256": PREDECESSOR_UPDATE_FAILURE_LOG_SHA256,
                "formal_progress_sha256": PREDECESSOR_PROGRESS_SHA256,
            },
            "stage_id": "TRAIN-B-BLOCK-00-GAMES",
            "expected": 128,
            "completed": 128,
            "failed_attempt_count": 0,
            "actor_update_count": 0,
        },
        "owner_go_comment_url": FORMAL_GO_COMMENT_URL,
        "pinned_issue84_source_commit": formal.LOCK["baseline_A"]["issue84_final_accepted_source_commit"],
        "initial_model_digest": PREDECESSOR_INITIAL_MODEL_DIGEST,
        "parent_model_digest": formal.LOCK["baseline_P"]["model_digest"],
        "parent_checkpoint_sha256": formal.LOCK["baseline_P"]["checkpoint_sha256"],
        "native_source_commit": formal.LOCK["native"]["commit"],
        "stsr_source_commit": formal.LOCK["stsr_source"]["commit"],
        "validated_units": len(unit_manifest),
        "formal_games_carried": len(unit_manifest),
        "actor_updates_carried": len(actor_update_carries),
        "features_loaded_by_pinned_issue84_parser": inherited_carry["features_loaded_by_pinned_issue84_parser"],
        "units_sha256": _hash_json(formal, unit_manifest),
        "units": unit_manifest,
        "outcome_manifest_sha256": _hash_json(formal, decoded_features),
    }
    if actor_update_carries:
        manifest["actor_update_carries"] = actor_update_carries
        manifest["no_new_successor_game_or_actor_update_receipts_created"] = True
    else:
        # Preserve the exact immutable fde package manifest schema and bytes.
        manifest["no_successor_game_or_update_receipts_created"] = True
    if resumed_training_group_carries:
        manifest["resumed_training_group_carries"] = resumed_training_group_carries
        manifest["resumed_training_games_carried"] = 128
    if resumed_formal_continuation is not None:
        manifest["resumed_formal_continuation"] = resumed_formal_continuation
        manifest["resumed_formal_games_carried"] = 128 + RESUMED_B03_COMPLETED
        manifest["resumed_formal_actor_updates_carried"] = 2
    manifest["manifest_sha256"] = _hash_json(formal, manifest)
    created_manifest = False
    if manifest_path.exists():
        if formal.read_json(manifest_path) != manifest:
            raise RuntimeError("immutable successor predecessor-carry manifest differs from verified evidence")
    elif write_manifest:
        if (current_root / "formal-progress.json").exists():
            raise RuntimeError("successor formal progress exists before the immutable carry manifest")
        update_root = current_root / "training" / "B" / "updates" / "block-00"
        if update_root.exists() and any(update_root.iterdir()):
            raise RuntimeError("successor Actor-update attempts exist before predecessor carry verification")
        formal.pilot._write_or_verify_once(manifest_path, manifest)
        created_manifest = True
    else:
        raise RuntimeError("successor predecessor-carry manifest has not been established")
    manifest_sha = formal.file_sha256(manifest_path)
    return {
        "schema_id": "issue85-formal-predecessor-carry-verification-report-v1",
        "status": (
            "VERIFIED / 128 PREDECESSOR + 128 RESUMED B/01 TERMINALS AND FEATURES / B/00 UPDATE CARRIED BY REFERENCE"
            if actor_update_carries else
            "VERIFIED / 128 PREDECESSOR TERMINALS AND FEATURES / NO NEW GAMES OR UPDATES"
        ),
        "package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "predecessor_package_commit": PREDECESSOR_PACKAGE_COMMIT,
        "predecessor_group_receipt_sha256": PREDECESSOR_GROUP_RECEIPT_SHA256,
        "formal_games_carried": len(unit_manifest),
        "actor_updates_carried": len(actor_update_carries),
        "resumed_training_games_carried": 128 * len(resumed_training_group_carries),
        "total_formal_games_carried": (
            len(unit_manifest) + 128 * len(resumed_training_group_carries)
            + (0 if resumed_formal_continuation is None else 128 + RESUMED_B03_COMPLETED)
        ),
        "resumed_formal_games_carried": (
            0 if resumed_formal_continuation is None else 128 + RESUMED_B03_COMPLETED
        ),
        "resumed_formal_actor_updates_carried": (
            0 if resumed_formal_continuation is None else 2
        ),
        "features_loaded_by_pinned_issue84_parser": int(
            inherited_carry["features_loaded_by_pinned_issue84_parser"]
        ) + (0 if resumed_formal_continuation is None else RESUMED_B03_COMPLETED),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "manifest": manifest,
        "writes_performed": created_manifest,
    }


def _resumed_formal_continuation_files(formal: Any) -> tuple[Path, dict[str, Path]]:
    data_root = Path(formal.DATA_ROOT).resolve(strict=True)
    source_root = _inside(data_root, RESUMED_FORMAL_PACKAGE_ROOT)
    files = {
        "seed": source_root / "formal-seed-table.json",
        "progress": source_root / "formal-progress.json",
        "authorization": source_root / "formal-execution-authorization.json",
        "carry": source_root / "predecessor-carry-manifest.json",
        "b02_group": source_root / "training/B/block-02/games/group-receipt.json",
        "b01_update": source_root / "training/B/updates/block-01/success-receipt.json",
        "b02_update": source_root / "training/B/updates/block-02/success-receipt.json",
        "b03_live": source_root / "live-training-B-03.json",
    }
    expected = {
        "seed": RESUMED_FORMAL_SEED_TABLE_SHA256,
        "progress": RESUMED_FORMAL_PROGRESS_SHA256,
        "authorization": RESUMED_FORMAL_AUTHORIZATION_SHA256,
        "carry": RESUMED_FORMAL_CARRY_SHA256,
        "b02_group": RESUMED_B02_GROUP_RECEIPT_SHA256,
        "b01_update": RESUMED_B01_UPDATE_RECEIPT_FILE_SHA256,
        "b02_update": RESUMED_B02_UPDATE_RECEIPT_FILE_SHA256,
        "b03_live": RESUMED_B03_LIVE_PROGRESS_SHA256,
    }
    for key, path in files.items():
        if not path.is_file() or formal.file_sha256(path) != expected[key]:
            raise RuntimeError(f"retained b8 continuation input is missing or changed: {key}")
    return source_root, files


def _b8_update_carry_reference(
    formal: Any, source_root: Path, receipt: Mapping[str, Any], *, arm: str, block: int,
) -> dict[str, Any]:
    receipt_path = source_root / "training" / arm / "updates" / f"block-{block:02d}" / "success-receipt.json"
    return {
        "arm": arm,
        "block_index": block,
        "source_package_commit": RESUMED_FORMAL_PACKAGE_COMMIT,
        "source_package_root": RESUMED_FORMAL_PACKAGE_ROOT,
        "source_lock_sha256": RESUMED_FORMAL_SOURCE_LOCK_SHA256,
        "source_update_receipt_path": receipt_path.relative_to(source_root).as_posix(),
        "source_update_receipt_file_sha256": formal.file_sha256(receipt_path),
        "receipt_sha256": receipt.get("receipt_sha256"),
        "training_group_receipt_sha256": receipt.get("training_group_receipt_sha256"),
        "training_data_package_commit": receipt.get("training_data_package_commit"),
        "source_predecessor_carry_manifest_sha256": receipt.get("predecessor_carry_manifest_sha256"),
        "actor_checkpoint_path": receipt.get("actor_checkpoint_path"),
        "actor_checkpoint_sha256": receipt.get("actor_checkpoint_sha256"),
        "optimizer_checkpoint_path": receipt.get("optimizer_checkpoint_path"),
        "optimizer_checkpoint_sha256": receipt.get("optimizer_checkpoint_sha256"),
        "result_model_digest": receipt.get("result_model_digest"),
        "completed_steps": receipt.get("completed_steps"),
        "actor_update_seed": receipt.get("actor_update_seed"),
        "actor_update_schedule_sha256": receipt.get("actor_update_schedule_sha256"),
        "owner_seed_sha256": receipt.get("owner_seed_sha256"),
    }


def _receipt_attempt_path(package_root: Path, unit: Mapping[str, Any]) -> Path:
    relative = unit.get("attempt_path")
    if not isinstance(relative, str) or not relative:
        raise RuntimeError("group unit receipt has no package-relative attempt path")
    return _inside(package_root, relative)


def _verify_b03_prestart_failure_reference(
    formal: Any, backend: Any, *, source_root: Path, b03_root: Path,
    row: Mapping[str, Any], authorization_receipt_sha256: str,
    reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind the retained B/03 attempt that failed before its start receipt."""
    failure_id = "TRAIN:8400417:B"
    attempt_name = "attempt-0001"
    if str(row.get("unit_id")) != failure_id:
        raise RuntimeError("retained B/03 pre-start failure row differs from its registered owner")
    unit_root = b03_root / "units" / backend.unit_key(failure_id)
    failed_attempt = unit_root / attempt_name
    attempt_dirs = sorted(path for path in unit_root.glob("attempt-*") if path.is_dir())
    if attempt_dirs != [failed_attempt]:
        raise RuntimeError("retained B/03 pre-start failure attempt inventory changed")

    job_path = failed_attempt / "worker-job.json"
    log_path = failed_attempt / "worker.log"
    started_path = failed_attempt / "started.json"
    result_path = failed_attempt / "result.json"
    finished_path = failed_attempt / "finished.json"
    if (
        not job_path.is_file() or not log_path.is_file()
        or started_path.exists() or result_path.exists() or finished_path.exists()
    ):
        raise RuntimeError("retained B/03 pre-start failure receipts or files differ")

    result, history = _unit_attempts(
        formal, backend, root=b03_root, row=row,
        package_commit=RESUMED_FORMAL_PACKAGE_COMMIT,
        lock_sha=RESUMED_FORMAL_SOURCE_LOCK_SHA256,
        seed_table_sha256=RESUMED_FORMAL_SEED_TABLE_SHA256,
        authorization_receipt_sha256=authorization_receipt_sha256,
        check_live_workers=False,
    )
    if result is not None or history != [{
        "attempt": attempt_name, "status": "STARTED_WITHOUT_TERMINAL",
    }]:
        raise RuntimeError("retained B/03 pre-start failure is not one unfinished attempt")

    log_text = log_path.read_text(encoding="utf-8")
    if not all(marker in log_text for marker in (
        "_validate_worker_authorization", "FileNotFoundError",
        "active-wall-issue85-task.json",
    )):
        raise RuntimeError("retained B/03 worker log does not show the reviewed pre-start authorization failure")
    observed = {
        "unit_id": failure_id,
        "attempt": attempt_name,
        "attempt_path": failed_attempt.relative_to(source_root).as_posix(),
        "status": "PRE_START_AUTHORIZATION_FAILURE",
        "failure_boundary": "before_started_receipt",
        "attempt_count": 1,
        "next_attempt": "attempt-0002",
        "worker_job_sha256": formal.file_sha256(job_path),
        "worker_log_sha256": formal.file_sha256(log_path),
        "started_receipt_absent": True,
        "result_receipt_absent": True,
        "finished_receipt_absent": True,
    }
    if reference is not None and dict(reference) != observed:
        raise RuntimeError("retained B/03 pre-start failure reference or evidence hash changed")
    return observed


def _validate_prestart_carried_attempt(carried: Mapping[str, Any]) -> None:
    reference = carried.get("reference")
    if not isinstance(reference, Mapping) or reference.get("status") != "PRE_START_AUTHORIZATION_FAILURE":
        return
    attempt_count = carried.get("attempt_count")
    if (
        type(attempt_count) is not int
        or attempt_count != reference.get("attempt_count")
        or carried.get("next_attempt") != f"attempt-{attempt_count + 1:04d}"
        or reference.get("next_attempt") != carried.get("next_attempt")
    ):
        raise RuntimeError("B/03 pre-start failure must resume at exactly its next attempt number")


def _resumed_b02_group_receipt_matches(
    group: Mapping[str, Any], *, expected_unit_ids: Sequence[str],
) -> bool:
    """Validate the retained game-group schema using its actual count fields."""
    units = group.get("units")
    return (
        group.get("schema_id") == GROUP_SCHEMA
        and group.get("status") == "SUCCEEDED"
        and group.get("stage_id") == "TRAIN-B-BLOCK-02-GAMES"
        and group.get("package_commit") == RESUMED_FORMAL_PACKAGE_COMMIT
        and group.get("source_lock_sha256") == RESUMED_FORMAL_SOURCE_LOCK_SHA256
        and group.get("seed_table_sha256") == RESUMED_FORMAL_SEED_TABLE_SHA256
        and group.get("authorization_receipt_sha256") == RESUMED_FORMAL_AUTHORIZATION_SHA256
        and group.get("expected") == 128
        and group.get("completed") == 128
        and group.get("failed_attempt_count") == RESUMED_B02_FAILED
        and isinstance(units, list)
        and [item.get("unit_id") for item in units] == list(expected_unit_ids)
    )


def _resumed_b03_feature_path(
    b03_root: Path, backend: Any, unit_id: str, path_value: Any,
) -> Path:
    relative = Path(str(path_value))
    expected = (
        Path("units") / backend.unit_key(unit_id) / "attempt-0001" / "training-features.json.zlib"
    )
    if relative.is_absolute() or ".." in relative.parts or relative != expected:
        raise RuntimeError(f"retained B/03 feature path escapes its group or differs from its attempt: {unit_id}")
    return _inside(b03_root, relative.as_posix())


def _resumed_b03_historical_attempts_match(
    history: Any, unit_ids: Sequence[str], failure_unit_id: str,
) -> bool:
    if failure_unit_id not in unit_ids or len(set(unit_ids)) != len(unit_ids):
        return False
    expected: dict[str, list[dict[str, str]]] = {unit_id: [] for unit_id in unit_ids}
    expected[failure_unit_id] = [{
        "attempt": "attempt-0001", "status": "STARTED_WITHOUT_TERMINAL",
    }]
    return history == expected


def _build_resumed_formal_continuation(
    formal: Any, *, issue84_source: Path, seed: Mapping[str, Any],
    backend: Any, issue84_driver: Any, active_wall_clock_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source_root, files = _resumed_formal_continuation_files(formal)
    source_seed = formal.read_json(files["seed"])
    current_clock_path = Path(formal.DATA_ROOT) / "issue85" / "artifacts" / "active-wall-issue85-task.json"
    if not current_clock_path.is_file():
        raise RuntimeError("the authoritative Issue 85 active-wall clock is missing")
    current_clock = formal.read_json(current_clock_path)
    elapsed = _validate_active_wall_clock(
        current_clock, FORMAL_ACTIVE_WALL_CAP_SECONDS, require_idle=True,
    )
    if active_wall_clock_snapshot is None:
        clock_snapshot = {
            "elapsed_seconds": float(elapsed),
            "active_session": None,
            "process_ids": [],
            "source_active_wall_file_sha256": formal.file_sha256(current_clock_path),
        }
    else:
        clock_snapshot = dict(active_wall_clock_snapshot)
        prior_elapsed = clock_snapshot.get("elapsed_seconds")
        if (
            isinstance(prior_elapsed, bool) or not isinstance(prior_elapsed, (int, float))
            or float(elapsed) < float(prior_elapsed)
        ):
            raise RuntimeError("the cumulative Issue 85 clock fell below the carried b8 high-water")
    if (
        source_seed.get("schema_id") != "issue85-formal-seed-table-v1"
        or source_seed.get("package_commit") != RESUMED_FORMAL_PACKAGE_COMMIT
        or source_seed.get("source_lock_sha256") != RESUMED_FORMAL_SOURCE_LOCK_SHA256
        or source_seed.get("contract_sha256") != formal.file_sha256(formal.CONTRACT_PATH)
        or source_seed.get("row_count") != 7808
        or source_seed.get("rows_sha256") != _hash_json(formal, source_seed.get("rows"))
        or source_seed.get("rows") != seed.get("rows")
        or source_seed.get("actor_update_schedule") != seed.get("actor_update_schedule")
        or source_seed.get("actor_update_schedule_sha256") != seed.get("actor_update_schedule_sha256")
    ):
        raise RuntimeError("b8 and successor seed tables do not bind the exact same registered rows and update schedule")
    old_progress = formal.read_json(files["progress"])
    old_auth = formal.read_json(files["authorization"])
    reviewer = old_auth.get("reviewer_approval")
    owner = old_auth.get("owner_approval")
    old_b02_group = formal.read_json(files["b02_group"])
    old_b03_live = formal.read_json(files["b03_live"])
    source_identity = {
        "package_commit": RESUMED_FORMAL_PACKAGE_COMMIT,
        "source_lock_sha256": RESUMED_FORMAL_SOURCE_LOCK_SHA256,
    }
    if (
        old_auth.get("schema_id") != AUTH_SCHEMA
        or old_auth.get("issue") != 85 or old_auth.get("formal_go") is not True
        or old_auth.get("status") != "APPROVED"
        or old_auth.get("package_commit") != RESUMED_FORMAL_PACKAGE_COMMIT
        or old_auth.get("source_lock_sha256") != RESUMED_FORMAL_SOURCE_LOCK_SHA256
        or old_auth.get("seed_table_sha256") != RESUMED_FORMAL_SEED_TABLE_SHA256
        or old_auth.get("whole_task_active_wall_cap_seconds") != FORMAL_ACTIVE_WALL_CAP_SECONDS
        or not isinstance(owner, Mapping) or owner.get("decision") != "FORMAL_GO"
        or owner.get("comment_url") != FORMAL_GO_COMMENT_URL
        or not isinstance(reviewer, Mapping) or reviewer.get("decision") != "PASS"
        or reviewer.get("package_commit") != RESUMED_FORMAL_PACKAGE_COMMIT
        or reviewer.get("source_lock_sha256") != RESUMED_FORMAL_SOURCE_LOCK_SHA256
        or old_progress.get("schema_id") != PROGRESS_SCHEMA
        or old_progress.get("package_commit") != RESUMED_FORMAL_PACKAGE_COMMIT
        or old_progress.get("source_lock_sha256") != RESUMED_FORMAL_SOURCE_LOCK_SHA256
        or old_progress.get("seed_table_sha256") != RESUMED_FORMAL_SEED_TABLE_SHA256
        or old_progress.get("authorization_receipt_sha256") != RESUMED_FORMAL_AUTHORIZATION_SHA256
        or old_progress.get("actor_updates", {}).get("B:1", {}).get("status") != "SUCCEEDED"
        or old_progress.get("actor_updates", {}).get("B:2", {}).get("status") != "SUCCEEDED"
    ):
        raise RuntimeError("b8 formal authorization/progress does not preserve the approved same-study continuation")
    b02_rows = _training_rows(seed, "B", 2)
    b03_rows = _training_rows(seed, "B", 3)
    if (
        len(b02_rows) != 128 or [int(row["owner_seed"]) for row in b02_rows] != list(range(8400257, 8400385))
        or len(b03_rows) != 128 or [int(row["owner_seed"]) for row in b03_rows] != list(range(8400385, 8400513))
    ):
        raise RuntimeError("registered B/02 or B/03 owners differ from the frozen formal seed table")
    group_root = _inside(Path(formal.DATA_ROOT), RESUMED_B02_GROUP_ROOT)
    expected_b02_ids = [str(row["unit_id"]) for row in b02_rows]
    progress_b02 = old_progress.get("game_groups", {}).get("TRAIN-B-BLOCK-02")
    update_b01 = _read_update_receipt(
        formal, source_root, source_identity, arm="B", block=1,
    )
    update_b02 = _read_update_receipt(
        formal, source_root, source_identity, arm="B", block=2,
    )
    if (
        not _resumed_b02_group_receipt_matches(
            old_b02_group, expected_unit_ids=expected_b02_ids,
        )
        or not isinstance(progress_b02, Mapping) or progress_b02.get("status") != "SUCCEEDED"
        or progress_b02.get("receipt_sha256") != RESUMED_B02_GROUP_RECEIPT_SHA256
        or update_b01.get("receipt_file_sha256") != RESUMED_B01_UPDATE_RECEIPT_FILE_SHA256
        or update_b02.get("receipt_file_sha256") != RESUMED_B02_UPDATE_RECEIPT_FILE_SHA256
        or update_b02.get("training_group_receipt_sha256") != RESUMED_B02_GROUP_RECEIPT_SHA256
        or update_b02.get("result_model_digest") != "10a3749c923d8ab7d9fdac6283bc5ddb8ae17ab793633cb78aca374e81cc4c75"
    ):
        raise RuntimeError("b8 B/02 group or B/01-B/02 actor-update lineage differs from retained receipts")
    # The exact B/02 receipt SHA and B/01-B/02 update receipt/checkpoint hashes were
    # validated above. Do not reopen every historical B/02 attempt on each resume;
    # those immutable receipts retain the outcomes and failed-attempt history.
    b03_root = _inside(Path(formal.DATA_ROOT), RESUMED_B03_GROUP_ROOT)
    expected_success_ids = [
        *(f"TRAIN:{owner}:B" for owner in range(8400385, 8400417)),
        "TRAIN:8400418:B",
    ]
    expected_pending_ids = [f"TRAIN:{owner}:B" for owner in range(8400419, 8400513)]
    failure_id = "TRAIN:8400417:B"
    attempt_counts = old_b03_live.get("attempt_counts")
    historical_attempts = old_b03_live.get("historical_attempts")
    all_b03_ids = [str(row["unit_id"]) for row in b03_rows]
    pending_with_attempts = [
        unit_id for unit_id in expected_pending_ids
        if (
            (b03_root / "units" / backend.unit_key(unit_id)).is_dir()
            and any((b03_root / "units" / backend.unit_key(unit_id)).iterdir())
        )
    ]
    if pending_with_attempts:
        raise RuntimeError(
            "retained B/03 pending owner has unrecorded attempts; refusing to repeat or overwrite evidence: "
            + ", ".join(pending_with_attempts[:5])
        )
    if (
        old_b03_live.get("schema_id") != "issue85-formal-live-stage-v1"
        or old_b03_live.get("status") != "RUNNING"
        or old_b03_live.get("stage_id") != "TRAIN-B-BLOCK-03-GAMES"
        or old_b03_live.get("package_commit") != RESUMED_FORMAL_PACKAGE_COMMIT
        or old_b03_live.get("source_lock_sha256") != RESUMED_FORMAL_SOURCE_LOCK_SHA256
        or old_b03_live.get("seed_table_sha256") != RESUMED_FORMAL_SEED_TABLE_SHA256
        or old_b03_live.get("authorization_receipt_sha256") != RESUMED_FORMAL_AUTHORIZATION_SHA256
        or old_b03_live.get("expected") != 128
        or old_b03_live.get("successful_completions") != RESUMED_B03_COMPLETED
        or old_b03_live.get("successful_unit_ids") != expected_success_ids
        or old_b03_live.get("pending_unit_ids") != expected_pending_ids
        or old_b03_live.get("active_unit_ids") != []
        or old_b03_live.get("recent_failures") != [{
            "unit_id": failure_id, "attempt": "attempt-0001", "exit_code": 1,
            "failure_detail": "worker failed without a result",
        }]
        or old_b03_live.get("historical_attempts", {}).get(failure_id) != [{
            "attempt": "attempt-0001", "status": "STARTED_WITHOUT_TERMINAL",
        }]
        or not isinstance(attempt_counts, Mapping)
        or set(attempt_counts) != set(all_b03_ids)
        or any(attempt_counts.get(unit_id) != 1 for unit_id in expected_success_ids)
        or any(attempt_counts.get(unit_id) != 0 for unit_id in expected_pending_ids)
        or attempt_counts.get(failure_id) != 1
        or not _resumed_b03_historical_attempts_match(
            historical_attempts, all_b03_ids, failure_id,
        )
    ):
        raise RuntimeError("b8 B/03 partial progress is inconsistent or contains active/changed attempts")
    successful_units = []
    decoded = []
    old_auth_sha = RESUMED_FORMAL_AUTHORIZATION_SHA256
    for row in b03_rows:
        unit_id = str(row["unit_id"])
        if unit_id not in expected_success_ids:
            continue
        result, history = _unit_attempts(
            formal, backend, root=b03_root, row=row,
            package_commit=RESUMED_FORMAL_PACKAGE_COMMIT,
            lock_sha=RESUMED_FORMAL_SOURCE_LOCK_SHA256,
            seed_table_sha256=RESUMED_FORMAL_SEED_TABLE_SHA256,
            authorization_receipt_sha256=old_auth_sha,
            check_live_workers=False,
        )
        if result is None or history:
            raise RuntimeError(f"retained B/03 success is not one terminal source-bound result: {unit_id}")
        unit_dir = b03_root / "units" / backend.unit_key(unit_id)
        attempt_dirs = sorted(path for path in unit_dir.glob("attempt-*") if path.is_dir())
        if len(attempt_dirs) != 1 or attempt_dirs[0].name != "attempt-0001":
            raise RuntimeError(f"retained B/03 success attempt history is not unique: {unit_id}")
        attempt = attempt_dirs[0]
        result_path = attempt / "result.json"
        finish_path = attempt / "finished.json"
        pointer = result.get("training_features")
        model_source = result.get("policy_source")
        if (
            not isinstance(pointer, Mapping)
            or pointer.get("compression") != "zlib"
            or result.get("model_digest") != update_b02.get("result_model_digest")
            or not isinstance(model_source, Mapping)
            or model_source.get("previous_update_receipt_sha256") != update_b02.get("receipt_sha256")
            or result.get("expert_decisions") != 0
        ):
            raise RuntimeError(f"retained B/03 result is not bound to the carried B/02 actor: {unit_id}")
        feature_relative = Path(str(pointer.get("path", "")))
        feature_path = _resumed_b03_feature_path(
            b03_root, backend, unit_id, feature_relative,
        )
        compressed = feature_path.read_bytes()
        raw = zlib.decompress(compressed)
        if (
            formal.sha256_bytes(compressed) != pointer.get("sha256")
            or len(compressed) != pointer.get("stored_bytes")
            or formal.sha256_bytes(raw) != pointer.get("uncompressed_sha256")
            or len(raw) != pointer.get("uncompressed_bytes")
        ):
            raise RuntimeError(f"retained B/03 feature bytes/hash/length differ: {unit_id}")
        payload = json.loads(raw.decode("utf-8"))
        base = payload.get("base") if isinstance(payload, Mapping) else None
        if (
            not isinstance(payload, Mapping) or payload.get("unit_id") != unit_id
            or payload.get("owner_seed") != int(row["owner_seed"])
            or payload.get("package_commit") != RESUMED_FORMAL_PACKAGE_COMMIT
            or payload.get("source_lock_sha256") != RESUMED_FORMAL_SOURCE_LOCK_SHA256
            or not isinstance(base, Mapping) or base.get("complete") is not True
            or base.get("terminal_floor") != result.get("terminal_floor")
            or base.get("heart_win") is not result.get("heart_victory")
        ):
            raise RuntimeError(f"retained B/03 feature terminal/owner binding differs: {unit_id}")
        successful_units.append({
            "unit_id": unit_id,
            "owner_seed": int(row["owner_seed"]),
            "attempt_path": attempt.relative_to(source_root).as_posix(),
            "attempt_count": 1,
            "result_sha256": formal.file_sha256(result_path),
            "finished_receipt_sha256": formal.file_sha256(finish_path),
            "training_features_path": feature_relative.as_posix(),
            "training_features_sha256": str(pointer["sha256"]),
            "training_features_uncompressed_sha256": str(pointer["uncompressed_sha256"]),
            "model_digest": str(result["model_digest"]),
        })
        decoded.append({
            "unit_id": unit_id, "owner_seed": int(row["owner_seed"]),
            "terminal_floor": float(result["terminal_floor"]),
            "heart_victory": bool(result["heart_victory"]),
        })
    if len(successful_units) != RESUMED_B03_COMPLETED:
        raise RuntimeError("retained B/03 success count differs from the immutable live-stage receipt")
    failed_row = next(row for row in b03_rows if str(row["unit_id"]) == failure_id)
    failure_reference = _verify_b03_prestart_failure_reference(
        formal, backend, source_root=source_root, b03_root=b03_root,
        row=failed_row, authorization_receipt_sha256=old_auth_sha,
    )
    with tempfile.TemporaryDirectory(prefix="issue85-resumed-b03-carry-") as temporary_root:
        loaded = backend.load_modules(Path(temporary_root), pilot=True)
        examples, outcomes = issue84_driver.load_training_outcomes(
            [dict(row) for row in b03_rows if str(row["unit_id"]) in expected_success_ids],
            root=b03_root, executor=loaded[-1], package=RESUMED_FORMAL_PACKAGE_COMMIT,
        )
    if len(examples) != RESUMED_B03_COMPLETED or len(outcomes) != RESUMED_B03_COMPLETED:
        raise RuntimeError("pinned #84 loader did not recover all 33 retained B/03 outcomes")
    for expected, example, outcome in zip(decoded, examples, outcomes, strict=True):
        base = getattr(example, "base", None)
        if (
            int(getattr(example, "owner_seed", -1)) != expected["owner_seed"]
            or not bool(getattr(base, "complete", False))
            or float(getattr(base, "terminal_floor", math.nan)) != expected["terminal_floor"]
            or bool(getattr(base, "heart_win", False)) != expected["heart_victory"]
            or int(getattr(outcome, "owner_seed", -1)) != expected["owner_seed"]
            or not bool(getattr(outcome, "complete", False))
            or float(getattr(outcome, "terminal_floor", math.nan)) != expected["terminal_floor"]
            or bool(getattr(outcome, "heart_win", False)) != expected["heart_victory"]
        ):
            raise RuntimeError(f"pinned #84 loader changed retained B/03 outcome: {expected['unit_id']}")
    return {
        "schema_id": RESUMED_FORMAL_CONTINUATION_SCHEMA,
        "source_package_commit": RESUMED_FORMAL_PACKAGE_COMMIT,
        "source_package_root": RESUMED_FORMAL_PACKAGE_ROOT,
        "source_lock_sha256": RESUMED_FORMAL_SOURCE_LOCK_SHA256,
        "formal_seed_table_sha256": RESUMED_FORMAL_SEED_TABLE_SHA256,
        "formal_progress_sha256": RESUMED_FORMAL_PROGRESS_SHA256,
        "authorization_receipt_sha256": RESUMED_FORMAL_AUTHORIZATION_SHA256,
        "predecessor_carry_manifest_sha256": RESUMED_FORMAL_CARRY_SHA256,
        "b02_group_root": RESUMED_B02_GROUP_ROOT,
        "b02_group_receipt_sha256": RESUMED_B02_GROUP_RECEIPT_SHA256,
        "b01_update_receipt_file_sha256": update_b01["receipt_file_sha256"],
        "b02_update_receipt_file_sha256": update_b02["receipt_file_sha256"],
        "b02_update_receipt_sha256": update_b02["receipt_sha256"],
        "b02_expected": 128,
        "b02_completed": 128,
        "b02_failed_attempt_count": RESUMED_B02_FAILED,
        "b03_live_progress_path": RESUMED_B03_LIVE_PROGRESS_PATH,
        "b03_live_progress_sha256": RESUMED_B03_LIVE_PROGRESS_SHA256,
        "b03_expected": 128,
        "b03_completed": len(successful_units),
        "b03_failed_attempt_count": RESUMED_B03_FAILED,
        "b03_pending": RESUMED_B03_PENDING,
        "b01_actor_update": _b8_update_carry_reference(
            formal, source_root, update_b01, arm="B", block=1,
        ),
        "b02_actor_update": _b8_update_carry_reference(
            formal, source_root, update_b02, arm="B", block=2,
        ),
        "b03_successful_units": successful_units,
        "b03_units_sha256": _hash_json(formal, successful_units),
        "b03_failed_attempt": failure_reference,
        "active_wall_clock_snapshot": clock_snapshot,
    }


def _validate_resumed_formal_continuation(
    formal: Any, package_root: Path, identity: Mapping[str, str], backend: Any,
) -> dict[str, Any] | None:
    if not _resumed_formal_continuation_required(str(identity.get("package_commit", ""))):
        return None
    manifest, carry_sha256 = _validate_carry_manifest(formal, package_root, identity)
    continuation = manifest.get("resumed_formal_continuation")
    if not _validate_resumed_formal_continuation_shape(formal, continuation):
        raise RuntimeError("b8 formal continuation reference is malformed")
    source_root, files = _resumed_formal_continuation_files(formal)
    if (
        formal.file_sha256(files["seed"]) != continuation["formal_seed_table_sha256"]
        or formal.file_sha256(files["progress"]) != continuation["formal_progress_sha256"]
        or formal.file_sha256(files["authorization"]) != continuation["authorization_receipt_sha256"]
        or formal.file_sha256(files["carry"]) != continuation["predecessor_carry_manifest_sha256"]
        or formal.file_sha256(files["b02_group"]) != continuation["b02_group_receipt_sha256"]
        or formal.file_sha256(files["b03_live"]) != continuation["b03_live_progress_sha256"]
    ):
        raise RuntimeError("retained b8 package source receipts changed after continuation review")
    source_identity = {
        "package_commit": RESUMED_FORMAL_PACKAGE_COMMIT,
        "source_lock_sha256": RESUMED_FORMAL_SOURCE_LOCK_SHA256,
    }
    b01_update = _read_update_receipt(formal, source_root, source_identity, arm="B", block=1)
    b02_update = _read_update_receipt(formal, source_root, source_identity, arm="B", block=2)
    references = {
        "B:1": (continuation["b01_actor_update"], b01_update),
        "B:2": (continuation["b02_actor_update"], b02_update),
    }
    for label, (reference, receipt) in references.items():
        if (
            reference.get("source_update_receipt_file_sha256") != receipt.get("receipt_file_sha256")
            or reference.get("receipt_sha256") != receipt.get("receipt_sha256")
            or reference.get("actor_checkpoint_sha256") != receipt.get("actor_checkpoint_sha256")
            or reference.get("optimizer_checkpoint_sha256") != receipt.get("optimizer_checkpoint_sha256")
            or reference.get("result_model_digest") != receipt.get("result_model_digest")
        ):
            raise RuntimeError(f"retained b8 carried actor update differs for {label}")
    b03_root = _inside(Path(formal.DATA_ROOT), RESUMED_B03_GROUP_ROOT)
    source_seed = formal.read_json(files["seed"])
    b03_rows = _training_rows(source_seed, "B", 3)
    rows_by_id = {str(row["unit_id"]): row for row in b03_rows}
    carried_units: dict[str, dict[str, Any]] = {}
    for reference in continuation["b03_successful_units"]:
        unit_id = str(reference["unit_id"])
        row = rows_by_id.get(unit_id)
        if (
            row is None
            or reference.get("owner_seed") != int(row["owner_seed"])
            or reference.get("attempt_count") != 1
        ):
            raise RuntimeError(f"retained b8 B/03 owner reference differs from the seed table: {unit_id}")
        expected_attempt = b03_root / "units" / backend.unit_key(unit_id) / "attempt-0001"
        attempt = _inside(source_root, str(reference["attempt_path"]))
        if attempt != expected_attempt:
            raise RuntimeError(f"retained b8 B/03 attempt path differs from its owner: {unit_id}")
        result_path = attempt / "result.json"
        finish_path = attempt / "finished.json"
        feature_path = _resumed_b03_feature_path(
            b03_root, backend, unit_id, reference["training_features_path"],
        )
        if (
            formal.file_sha256(result_path) != reference["result_sha256"]
            or formal.file_sha256(finish_path) != reference["finished_receipt_sha256"]
            or formal.file_sha256(feature_path) != reference["training_features_sha256"]
        ):
            raise RuntimeError(f"retained b8 B/03 success changed after continuation review: {unit_id}")
        result = formal.read_json(result_path)
        pointer = result.get("training_features")
        model_source = result.get("policy_source")
        if (
            result.get("status") != "SUCCEEDED"
            or result.get("unit_id") != unit_id
            or result.get("owner_seed") != int(row["owner_seed"])
            or result.get("package_commit") != RESUMED_FORMAL_PACKAGE_COMMIT
            or result.get("source_lock_sha256") != RESUMED_FORMAL_SOURCE_LOCK_SHA256
            or result.get("model_digest") != continuation["b02_actor_update"]["result_model_digest"]
            or not isinstance(pointer, Mapping)
            or pointer.get("path") != reference.get("training_features_path")
            or pointer.get("sha256") != reference.get("training_features_sha256")
            or pointer.get("uncompressed_sha256") != reference.get("training_features_uncompressed_sha256")
            or not isinstance(model_source, Mapping)
            or model_source.get("previous_update_receipt_sha256") != continuation["b02_actor_update"].get("receipt_sha256")
            or result.get("expert_decisions") != 0
        ):
            raise RuntimeError(f"retained b8 B/03 terminal result is not bound to the reviewed B/02 model: {unit_id}")
        carried_units[unit_id] = {
            "result": result,
            "history": [],
            "attempt_count": int(reference["attempt_count"]),
            "reference": dict(reference),
            "group_root": str(b03_root),
            "package_commit": RESUMED_FORMAL_PACKAGE_COMMIT,
        }
    failed = continuation["b03_failed_attempt"]
    failed_row = rows_by_id.get(str(failed["unit_id"]))
    if failed_row is None:
        raise RuntimeError("retained b8 B/03 failed owner is absent from the frozen seed table")
    _verify_b03_prestart_failure_reference(
        formal, backend, source_root=source_root, b03_root=b03_root,
        row=failed_row,
        authorization_receipt_sha256=RESUMED_FORMAL_AUTHORIZATION_SHA256,
        reference=failed,
    )
    carried_units[str(failed["unit_id"])] = {
        "result": None,
        "history": [{
            "attempt": str(failed["attempt"]),
            "status": str(failed["status"]),
            "failure_boundary": str(failed["failure_boundary"]),
            "worker_job_sha256": str(failed["worker_job_sha256"]),
            "worker_log_sha256": str(failed["worker_log_sha256"]),
            "started_receipt_absent": True,
            "result_receipt_absent": True,
            "finished_receipt_absent": True,
            "source_package_commit": RESUMED_FORMAL_PACKAGE_COMMIT,
            "next_attempt": str(failed["next_attempt"]),
        }],
        "attempt_count": int(failed["attempt_count"]),
        "reference": dict(failed),
        "next_attempt": str(failed["next_attempt"]),
        "group_root": str(b03_root),
        "package_commit": RESUMED_FORMAL_PACKAGE_COMMIT,
    }
    pending_ids = [
        str(row["unit_id"]) for row in b03_rows
        if str(row["unit_id"]) not in carried_units
    ]
    pending_with_attempts = [
        unit_id for unit_id in pending_ids
        if (
            (b03_root / "units" / backend.unit_key(unit_id)).is_dir()
            and any((b03_root / "units" / backend.unit_key(unit_id)).iterdir())
        )
    ]
    if pending_with_attempts:
        raise RuntimeError(
            "retained b8 B/03 pending owner now has unrecorded attempts; refusing to repeat evidence: "
            + ", ".join(pending_with_attempts[:5])
        )
    return {
        "manifest_sha256": carry_sha256,
        "source_package_root": str(source_root),
        "b02_group_receipt": formal.read_json(files["b02_group"]),
        "b02_group_root": str(_inside(Path(formal.DATA_ROOT), RESUMED_B02_GROUP_ROOT)),
        "b01_actor_update": b01_update,
        "b02_actor_update": b02_update,
        "b03_carried_units": carried_units,
        "b03_failed_attempt": dict(failed),
    }


def _resumed_b02_training_group(
    formal: Any, package_root: Path, identity: Mapping[str, str],
    continuation: Mapping[str, Any],
) -> dict[str, Any]:
    _manifest, carry_sha256 = _validate_carry_manifest(formal, package_root, identity)
    receipt = continuation.get("b02_group_receipt")
    if (
        not isinstance(receipt, Mapping)
        or formal.file_sha256(_inside(Path(formal.DATA_ROOT), RESUMED_B02_GROUP_ROOT) / "group-receipt.json")
        != RESUMED_B02_GROUP_RECEIPT_SHA256
    ):
        raise RuntimeError("verified read-only B/02 group receipt is unavailable")
    return {
        "schema_id": CARRIED_GROUP_REFERENCE_SCHEMA,
        "artifact_kind": "read-only b8 B/02 receipt reference; no successor game receipt",
        "status": "SUCCEEDED",
        "stage_id": "TRAIN-B-BLOCK-02-GAMES",
        "package_commit": RESUMED_FORMAL_PACKAGE_COMMIT,
        "training_data_package_commit": RESUMED_FORMAL_PACKAGE_COMMIT,
        "source_lock_sha256": RESUMED_FORMAL_SOURCE_LOCK_SHA256,
        "seed_table_sha256": RESUMED_FORMAL_SEED_TABLE_SHA256,
        "authorization_receipt_sha256": RESUMED_FORMAL_AUTHORIZATION_SHA256,
        "expected": 128,
        "completed": 128,
        "failed_attempt_count": RESUMED_B02_FAILED,
        "receipt_path": (Path(RESUMED_B02_GROUP_ROOT) / "group-receipt.json").as_posix(),
        "receipt_sha256": RESUMED_B02_GROUP_RECEIPT_SHA256,
        "group_root": str(continuation["b02_group_root"]),
        "predecessor_carry_manifest_sha256": carry_sha256,
        "carried_actor_update": "B/02 is also referenced from the reviewed b8 package",
    }


def _formal_paths(formal: Any, identity: Mapping[str, str]) -> tuple[Path, Path, Path]:
    data_root = Path(formal.DATA_ROOT)
    package = str(identity["package_commit"])
    package_root = data_root / "issue85" / "artifacts" / "formal" / package
    readiness = data_root / "issue85" / "artifacts" / "formal-readiness" / package / "formal-readiness.json"
    seed_table = package_root / "formal-seed-table.json"
    return package_root, readiness, seed_table


def _validate_readiness(formal: Any, identity: Mapping[str, str]) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    package_root, readiness_path, seed_path = _formal_paths(formal, identity)
    preflight_path = package_root / "source-preflight.json"
    if not all(path.is_file() for path in (readiness_path, seed_path, preflight_path)):
        raise RuntimeError("formal execution requires completed, immutable source and seed preflight receipts")
    readiness = formal.read_json(readiness_path)
    seed = formal.read_json(seed_path)
    source_preflight = formal.read_json(preflight_path)
    formal.validate_formal_seed_table(seed, identity=identity)
    estimate = readiness.get("whole_task_active_wall_estimate")
    if (
        readiness.get("schema_id") != "issue85-formal-readiness-v1"
        or readiness.get("package_commit") != identity["package_commit"]
        or readiness.get("source_lock_sha256") != identity["source_lock_sha256"]
        or readiness.get("contract_sha256") != formal.file_sha256(formal.CONTRACT_PATH)
        or readiness.get("analysis_source_sha256") != formal.file_sha256(formal.STUDY / "formal_analysis.py")
        or readiness.get("formal_seed_table_sha256") != formal.file_sha256(seed_path)
        or readiness.get("formal_games_started") != 0
        or readiness.get("formal_cohorts_changed") is not False
        or not isinstance(estimate, Mapping)
        or "NO_GO" not in str(estimate.get("status", ""))
        or source_preflight.get("verified") is not True
        or readiness.get("baseline_preflight_sha256") != formal.file_sha256(preflight_path)
    ):
        raise RuntimeError("formal readiness is stale, mismatched, or does not preserve the no-GO state")
    return package_root, readiness, seed


def validate_authorization(
    formal: Any, authorization_path: Path, *, identity: Mapping[str, str],
    readiness: Mapping[str, Any], seed: Mapping[str, Any], require_idle_clock: bool = True,
) -> dict[str, Any]:
    """Require human decisions bound to the exact executable package and all cohorts."""
    authorization_path = Path(authorization_path).resolve(strict=True)
    receipt = formal.read_json(authorization_path)
    owner = receipt.get("owner_approval")
    reviewer = receipt.get("reviewer_approval")
    cap = receipt.get("whole_task_active_wall_cap_seconds")
    package_root, _readiness_path, _seed_path = _formal_paths(formal, identity)
    carry_sha256 = receipt.get("predecessor_carry_manifest_sha256")
    carry_manifest = None
    if isinstance(carry_sha256, str):
        carry_manifest, _validated_carry_sha256 = _validate_carry_manifest(
            formal, package_root, identity, expected_sha256=carry_sha256,
        )
    if (
        receipt.get("schema_id") != AUTH_SCHEMA
        or receipt.get("issue") != 85
        or receipt.get("formal_go") is not True
        or receipt.get("status") != "APPROVED"
        or receipt.get("package_commit") != identity.get("package_commit")
        or receipt.get("source_lock_sha256") != identity.get("source_lock_sha256")
        or receipt.get("contract_sha256") != formal.file_sha256(formal.CONTRACT_PATH)
        or receipt.get("seed_table_sha256") != readiness.get("formal_seed_table_sha256")
        or receipt.get("actor_update_schedule_sha256") != seed.get("actor_update_schedule_sha256")
        or receipt.get("analysis_source_sha256") != formal.file_sha256(formal.STUDY / "formal_analysis.py")
        or isinstance(cap, bool) or not isinstance(cap, int) or cap != FORMAL_ACTIVE_WALL_CAP_SECONDS
        or not isinstance(carry_sha256, str) or len(carry_sha256) != 64
        or receipt.get("pilot_report_sha256") != ACCEPTED_PILOT_REPORT_SHA256
        or not isinstance(owner, Mapping)
        or owner.get("status") != "APPROVED"
        or owner.get("decision") != "FORMAL_GO"
        or owner.get("comment_url") != FORMAL_GO_COMMENT_URL
        or not isinstance(reviewer, Mapping)
        or reviewer.get("status") != "APPROVED"
        or reviewer.get("package_commit") != identity.get("package_commit")
        or reviewer.get("source_lock_sha256") != identity.get("source_lock_sha256")
        or reviewer.get("seed_table_sha256") != readiness.get("formal_seed_table_sha256")
        or reviewer.get("actor_update_schedule_sha256") != seed.get("actor_update_schedule_sha256")
        or reviewer.get("analysis_source_sha256") != formal.file_sha256(formal.STUDY / "formal_analysis.py")
        or reviewer.get("decision") != "PASS"
        or not _issue_comment_url(reviewer.get("comment_url"))
    ):
        raise RuntimeError(
            "formal execution is blocked: exact owner FORMAL_GO, whole-task wall cap, and independent review receipts are required"
        )
    active_wall = Path(formal.DATA_ROOT) / "issue85" / "artifacts" / "active-wall-issue85-task.json"
    if not active_wall.is_file():
        raise RuntimeError("the cumulative Issue 85 active-wall clock is missing; refusing to reset study time")
    prior_clock = formal.read_json(active_wall)
    elapsed = _validate_active_wall_clock(prior_clock, cap, require_idle=require_idle_clock)
    if carry_manifest is not None:
        high_water = _carried_active_wall_high_water(carry_manifest, cap)
        if high_water is not None and elapsed < high_water:
            raise RuntimeError(
                "the cumulative Issue 85 active-wall clock is below the immutable carried active-wall high-water "
                f"({elapsed} < {high_water}); refusing to reset study time"
            )
    pilot_report_path = (
        Path(formal.DATA_ROOT) / "issue85" / "artifacts" / "pilot"
        / ACCEPTED_PILOT_PACKAGE_COMMIT / "pilot-report.json"
    )
    if not pilot_report_path.is_file():
        raise RuntimeError("the accepted completed Issue 85 Pilot receipt is required before formal execution")
    pilot_report = formal.read_json(pilot_report_path)
    if (
        formal.file_sha256(pilot_report_path) != ACCEPTED_PILOT_REPORT_SHA256
        or pilot_report.get("schema_id") != "issue85-pilot-report-v1"
        or pilot_report.get("status") != "SUCCEEDED"
        or pilot_report.get("package_commit") != ACCEPTED_PILOT_PACKAGE_COMMIT
        or pilot_report.get("source_lock_sha256") != identity["source_lock_sha256"]
        or pilot_report.get("formal_games_started") != 0
        or pilot_report.get("formal_cohorts_changed") is not False
    ):
        raise RuntimeError("the retained Pilot is not the accepted nonformal predecessor or reports formal work")
    return {
        "path": str(authorization_path),
        "sha256": formal.file_sha256(authorization_path),
        "whole_task_active_wall_cap_seconds": cap,
        "owner_approval_comment_url": owner["comment_url"],
        "reviewer_approval_comment_url": reviewer["comment_url"],
        "pilot_report_sha256": formal.file_sha256(pilot_report_path),
        "predecessor_carry_manifest_sha256": carry_sha256,
        "prior_active_wall_seconds": float(elapsed),
    }


def _validate_worker_authorization(
    formal: Any, job: Mapping[str, Any], *, identity: Mapping[str, str],
    readiness: Mapping[str, Any], seed: Mapping[str, Any],
) -> dict[str, Any]:
    path = Path(str(job.get("authorization_receipt_path", ""))).resolve(strict=True)
    receipt = validate_authorization(
        formal, path, identity=identity, readiness=readiness, seed=seed,
        require_idle_clock=False,
    )
    if (
        job.get("authorization_receipt_sha256") != receipt["sha256"]
        or job.get("package_commit") != identity["package_commit"]
        or job.get("source_lock_sha256") != identity["source_lock_sha256"]
        or job.get("seed_table_sha256") != readiness.get("formal_seed_table_sha256")
    ):
        raise RuntimeError("worker job authorization/source/seed binding differs")
    wall_path = Path(formal.DATA_ROOT) / "issue85" / "artifacts" / "active-wall-issue85-task.json"
    wall = formal.read_json(wall_path)
    supervisor = job.get("supervisor_pid")
    if (
        isinstance(supervisor, bool) or not isinstance(supervisor, int) or supervisor <= 0
        or os.getppid() != supervisor
        or wall.get("active_session") is None
        or supervisor not in [int(pid) for pid in wall.get("process_ids", [])]
        or float(wall.get("elapsed_seconds", 0.0)) > receipt["whole_task_active_wall_cap_seconds"]
    ):
        raise RuntimeError("worker was not launched by the live, cap-bound formal supervisor")
    return receipt


def _formal_training_dispatch(
    formal: Any, row: Mapping[str, Any], loader_label: str,
) -> tuple[str, int]:
    """Validate #84's block dispatch token, then select the formal actor lineage.

    The pinned #84 driver calls `arm_policy` with the zero-based training block
    and therefore asks its loader for `pi{12 + block}`. For Issue 85, that label is
    only a dispatch token: the source-bound row still records pi12 as the locked
    initialization, while `load_training_arm` selects either that initialization
    or the preceding Issue 85 block checkpoint.
    """
    arm = str(row.get("issue85_arm", ""))
    block = row.get("block_index")
    if (
        arm not in formal.NEW_ARMS
        or row.get("issue84_policy_label") != "pi12"
        or isinstance(block, bool)
        or not isinstance(block, int)
        or not 0 <= block < 8
        or loader_label != f"pi{12 + block}"
    ):
        raise RuntimeError("formal training worker has an invalid arm/block/model label")
    return arm, block


def _configure_formal_runtime(
    formal: Any, issue84_source: Path, identity: Mapping[str, str], package_root: Path,
) -> tuple[Any, Any]:
    pilot_module = formal.pilot
    backend, issue84_driver = pilot_module._configure_runtime(issue84_source, package_root, identity)
    backend.PINNED_PYTHON = _source_locked_formal_python(formal)
    pilot_root_loader = backend.load_modules
    backend.__file__ = str(Path(formal.__file__).resolve())

    def load_modules(active_root: Path, *, pilot: bool):
        # The pinned #84 loader/runtime is reused. Its pilot-specific Issue 85
        # model selector is replaced below with the source-locked block lineage.
        loaded = pilot_root_loader(Path(active_root), pilot=True)
        _algorithm, _runtime, model, _learning, executor = loaded
        original_load_model = executor._load_model
        from representations import Issue85Arm

        def load_locked_pi12() -> tuple[Any, str, dict[str, Any]]:
            token = pilot_module._CURRENT_ROW.set(None)
            try:
                actor, digest, record = original_load_model("pi12", proxy=False)
            finally:
                pilot_module._CURRENT_ROW.reset(token)
            locked = formal.LOCK["baseline_P"]
            if digest != locked["model_digest"] or record.get("checkpoint_sha256") != locked["checkpoint_sha256"]:
                raise RuntimeError("Issue 85 formal actor initialization differs from the exact locked pi12 bytes")
            return actor, digest, dict(record)

        def fresh_arm(arm: str) -> tuple[Any, str, dict[str, Any]]:
            parent, parent_digest, parent_record = load_locked_pi12()
            actor = Issue85Arm(
                arm, model, parent.state_dict(), seed=pilot_module.derive_seed("residual-init", arm),
            ).eval()
            return actor, model.model_digest(actor), {
                "source": "locked-issue82-pi12-plus-registered-issue85-initialization",
                "parent_model_digest": parent_digest,
                "parent_checkpoint_sha256": parent_record.get("checkpoint_sha256"),
                "formal": True,
            }

        def load_training_arm(row: Mapping[str, Any], arm: str, block: int) -> tuple[Any, str, dict[str, Any]]:
            actor, _initial_digest, source = fresh_arm(arm)
            if block == 0:
                return actor, model.model_digest(actor), source
            previous = _read_update_receipt(formal, package_root, identity, arm=arm, block=block - 1)
            previous_root = Path(str(previous.get("producer_package_root", package_root)))
            checkpoint = _inside(previous_root, str(previous["actor_checkpoint_path"]))
            if formal.file_sha256(checkpoint) != previous.get("actor_checkpoint_sha256"):
                raise RuntimeError("previous formal actor checkpoint hash differs")
            import torch
            actor.load_state_dict(torch.load(checkpoint, map_location="cpu", weights_only=True), strict=True)
            digest = model.model_digest(actor)
            if digest != previous.get("result_model_digest"):
                raise RuntimeError("previous formal actor digest differs from its immutable update receipt")
            return actor.eval(), digest, {
                "source": "previous-registered-formal-block",
                "previous_block": block - 1,
                "previous_update_receipt_sha256": previous["receipt_sha256"],
                "parent_model_digest": source["parent_model_digest"],
                "formal": True,
            }

        def load_evaluation_arm(row: Mapping[str, Any], arm: str) -> tuple[Any, str, dict[str, Any]]:
            if arm == "PARENT":
                return load_locked_pi12()
            if arm == "A":
                locked = formal.LOCK["baseline_A"]
                checkpoint = formal.pilot._path_from_wsl(formal.DATA_ROOT_WSL) / locked["checkpoint_data_path"]
                actor = formal.pilot._load_checkpoint(
                    model, checkpoint, locked["checkpoint_sha256"], locked["model_digest"],
                )
                return actor, model.model_digest(actor), {
                    "source": "exact-locked-issue84-B1024-baseline",
                    "checkpoint_sha256": locked["checkpoint_sha256"],
                    "training_package_commit": locked["issue84_training_package_commit"],
                    "formal": True,
                }
            receipt = _read_update_receipt(formal, package_root, identity, arm=arm, block=7)
            checkpoint = _inside(package_root, str(receipt["actor_checkpoint_path"]))
            if formal.file_sha256(checkpoint) != receipt.get("actor_checkpoint_sha256"):
                raise RuntimeError(f"final Issue 85 {arm} model checkpoint hash differs")
            actor, _digest, source = fresh_arm(arm)
            import torch
            actor.load_state_dict(torch.load(checkpoint, map_location="cpu", weights_only=True), strict=True)
            digest = model.model_digest(actor)
            if digest != receipt.get("result_model_digest"):
                raise RuntimeError(f"final Issue 85 {arm} model digest differs")
            return actor.eval(), digest, {
                "source": "formal-training-block-7",
                "checkpoint_sha256": receipt["actor_checkpoint_sha256"],
                "update_receipt_sha256": receipt["receipt_sha256"],
                "parent_model_digest": source["parent_model_digest"],
                "formal": True,
            }

        def load_model(label: str, *, proxy: bool = False):
            row = pilot_module._CURRENT_ROW.get()
            if row is None:
                return original_load_model(label, proxy=proxy)
            if row.get("formal") is not True or not row.get("unit_id"):
                raise RuntimeError("Issue 85 formal worker did not receive its frozen seed-table row")
            phase = str(row.get("phase", ""))
            if phase == "training":
                arm, block = _formal_training_dispatch(formal, row, label)
                return load_training_arm(row, arm, block)
            if phase == "evaluation":
                policy = str(row.get("arm", ""))
                if policy not in formal.POLICIES:
                    raise RuntimeError("formal evaluation worker has an unregistered policy")
                if row.get("issue85_phase") not in {"validation", "test"}:
                    raise RuntimeError("formal evaluation row is not registered validation or TEST")
                expected_label = "pi12" if policy == "PARENT" else "pi20"
                if label != expected_label:
                    raise RuntimeError("formal evaluation requested a model label outside its registered policy")
                return load_evaluation_arm(row, policy)
            raise RuntimeError(f"formal worker has an unsupported phase: {phase}")

        executor._load_model = load_model
        _install_formal_evaluation_dispatch(formal, executor, issue84_driver)
        return loaded

    backend.load_modules = load_modules
    return backend, issue84_driver


def _source_locked_formal_python(formal: Any) -> Path:
    runtime = formal.LOCK["model_runtime"]
    path = Path(formal.DATA_ROOT) / str(runtime["python_data_path"])
    if not path.is_file() or formal.file_sha256(path) != runtime["python_sha256"]:
        raise RuntimeError("formal runtime Python differs from the source-locked Issue 85 binary")
    return path


def _inside(root: Path, relative: str) -> Path:
    path = (Path(root) / relative).resolve(strict=True)
    try:
        path.relative_to(Path(root).resolve(strict=True))
    except ValueError as exc:
        raise RuntimeError("formal artifact path escapes its source-bound package") from exc
    return path


def _install_formal_evaluation_dispatch(formal: Any, executor: Any, issue84_driver: Any) -> None:
    if getattr(executor, "_issue85_formal_evaluation_dispatch_installed", False):
        return
    original_worker_owner = executor._worker_owner
    arm_context = getattr(issue84_driver, "_ISSUE84_HELDOUT_ORIGINAL_ARM", None)
    group_context = getattr(issue84_driver, "_ISSUE84_HELDOUT_ORIGINAL_GROUP", None)
    if not all(hasattr(context, "set") and hasattr(context, "reset") for context in (arm_context, group_context)):
        raise RuntimeError("pinned #84 held-out dispatch contexts are missing")

    def worker_owner(job: Mapping[str, Any], ledger: Any):
        row = job.get("row") if isinstance(job, Mapping) else None
        if (
            not isinstance(row, Mapping) or job.get("pilot") is not False
            or job.get("phase") != "evaluation" or row.get("formal") is not True
            or row.get("issue85_phase") not in {"validation", "test"}
            or row.get("arm") not in formal.POLICIES
        ):
            return original_worker_owner(job, ledger)
        policy = str(row["arm"])
        issue84_arm = None if policy == "PARENT" else policy
        expected_issue85 = policy if policy in formal.NEW_ARMS else None
        if (
            row.get("issue84_arm") != issue84_arm
            or row.get("issue84_policy_label") != ("pi12" if policy == "PARENT" else "pi20")
            or row.get("issue85_arm") != expected_issue85
        ):
            raise RuntimeError("formal evaluation row differs from its frozen #84/#85 policy binding")
        dispatch_job = dict(job)
        dispatch_row = dict(row)
        dispatch_row["arm"] = "BRANCH"
        dispatch_job["row"] = dispatch_row
        arm_token: Token[Any] = arm_context.set(issue84_arm)
        group_token: Token[Any] = group_context.set(policy)
        row_token = formal.pilot._CURRENT_ROW.set(row)
        try:
            result = original_worker_owner(dispatch_job, ledger)
        finally:
            formal.pilot._CURRENT_ROW.reset(row_token)
            group_context.reset(group_token)
            arm_context.reset(arm_token)
        if not isinstance(result, dict) or result.get("status") != "SUCCEEDED" or result.get("unit_id") != row.get("unit_id"):
            raise RuntimeError("formal held-out policy dispatch changed the registered owner identity")
        result = dict(result)
        result["arm"] = policy
        return result

    executor._worker_owner = worker_owner
    executor._issue85_formal_evaluation_dispatch_installed = True


def _worker_row(formal: Any, job: Mapping[str, Any], seed: Mapping[str, Any], identity: Mapping[str, str]) -> dict[str, Any]:
    if (
        job.get("schema_id") != "issue79-owner-worker-job-v1"
        or job.get("pilot") is not False
        or job.get("formal") is not True
        or job.get("package_commit") != identity["package_commit"]
        or job.get("runner_code_commit") != identity["package_commit"]
        or job.get("source_lock_sha256") != identity["source_lock_sha256"]
    ):
        raise RuntimeError("formal worker job is not bound to the exact formal package/source")
    unit_id = str(job.get("unit_id", ""))
    rows = seed.get("rows")
    row = next((dict(value) for value in rows if value.get("unit_id") == unit_id), None)
    if row is None or job.get("row") != row or job.get("phase") != row.get("phase"):
        raise RuntimeError("formal worker job row is not an exact member of the frozen seed table")
    attempt = Path(str(job.get("attempt_dir", ""))).resolve(strict=True)
    root = Path(str(job.get("artifact_root", ""))).resolve(strict=True)
    try:
        attempt.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("formal worker attempt escapes its group root") from exc
    if (
        Path(str(job.get("result_path", ""))).resolve() != (attempt / "result.json").resolve()
        or Path(str(job.get("output_dir", ""))).resolve() != (attempt / "native").resolve()
        or job.get("run_id") != f"{unit_id}-{attempt.name}"
        or not re.fullmatch(r"attempt-[0-9]{4,}", attempt.name)
    ):
        raise RuntimeError("formal worker attempt paths or run ID differ")
    return row


def worker_main(formal: Any, job_path: Path) -> int:
    identity = formal.pilot.source_identity(require_clean=True)
    package_root, readiness, seed = _validate_readiness(formal, identity)
    job_path = Path(job_path).resolve(strict=True)
    job = formal.read_json(job_path)
    authorization = _validate_worker_authorization(
        formal, job, identity=identity, readiness=readiness, seed=seed,
    )
    row = _worker_row(formal, job, seed, identity)
    if row.get("formal") is not True:
        raise RuntimeError("formal worker row unexpectedly has nonformal status")
    issue84_path = Path(str(job.get("issue84_source_root", ""))).resolve(strict=True)
    issue84_root = formal._validate_issue84_source_for_runtime(issue84_path)
    backend, _driver = _configure_formal_runtime(formal, issue84_root, identity, package_root)
    formal.pilot._CURRENT_ROW.set(row)
    job["formal_execution_authorization_sha256"] = authorization["sha256"]
    return int(backend.worker_main(job_path))


def _unit_attempts(
    formal: Any, backend: Any, *, root: Path, row: Mapping[str, Any], package_commit: str,
    lock_sha: str, seed_table_sha256: str, authorization_receipt_sha256: str,
    check_live_workers: bool = True,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    unit_id = str(row["unit_id"])
    unit_root = Path(root) / "units" / backend.unit_key(unit_id)
    attempts = sorted(path for path in unit_root.glob("attempt-*") if path.is_dir()) if unit_root.is_dir() else []
    success_records: list[tuple[dict[str, Any], Path]] = []
    historical: list[dict[str, Any]] = []
    expected_root = Path(root).resolve()
    for attempt in attempts:
        if not re.fullmatch(r"attempt-[0-9]{4,}", attempt.name):
            raise RuntimeError(f"formal attempt directory has an invalid name: {attempt}")
        job_path = attempt / "worker-job.json"
        if not job_path.is_file():
            if any(attempt.iterdir()):
                raise RuntimeError(f"formal attempt has evidence but no immutable worker-job binding: {attempt}")
            historical.append({
                "attempt": attempt.name,
                "status": "ORPHAN_EMPTY_ATTEMPT_DIRECTORY",
                "failure_boundary": "before_worker_job_receipt",
                "failure_detail": "empty attempt directory retained after an interrupted launch setup",
            })
            continue
        job = formal.read_json(job_path)
        run_id = f"{unit_id}-{attempt.name}"
        if (
            job.get("schema_id") != "issue79-owner-worker-job-v1"
            or job.get("pilot") is not False or job.get("formal") is not True
            or job.get("unit_id") != unit_id or job.get("run_id") != run_id
            or job.get("row") != dict(row) or job.get("phase") != row.get("phase")
            or job.get("package_commit") != package_commit
            or job.get("runner_code_commit") != package_commit
            or job.get("source_lock_sha256") != lock_sha
            or job.get("seed_table_sha256") != seed_table_sha256
            or job.get("authorization_receipt_sha256") != authorization_receipt_sha256
            or Path(str(job.get("artifact_root", ""))).resolve() != expected_root
            or Path(str(job.get("attempt_dir", ""))).resolve() != attempt.resolve()
            or Path(str(job.get("result_path", ""))).resolve() != (attempt / "result.json").resolve()
            or Path(str(job.get("output_dir", ""))).resolve() != (attempt / "native").resolve()
        ):
            raise RuntimeError(f"formal attempt {attempt} is misbound to a source, row, or output path")
        started_path, finish_path, result_path = attempt / "started.json", attempt / "finished.json", attempt / "result.json"
        started = formal.read_json(started_path) if started_path.is_file() else None
        finished = formal.read_json(finish_path) if finish_path.is_file() else None
        result = formal.read_json(result_path) if result_path.is_file() else None
        if started is not None:
            pid = started.get("pid")
            if (
                started.get("schema_id") != "issue79-owner-attempt-start-v1"
                or started.get("unit_id") != unit_id or started.get("run_id") != run_id
                or started.get("owner_seed") != row.get("owner_seed")
                or started.get("arm") != row.get("arm")
                or started.get("package_commit") != package_commit
                or started.get("seed_row_sha256") != _hash_json(formal, dict(row))
                or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            ):
                raise RuntimeError(f"formal attempt start receipt differs: {started_path}")
            # Terminal attempts can outlive their PID; it may have been reused.
            if check_live_workers and finished is None:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError as exc:
                    raise RuntimeError(f"cannot verify formal worker {pid}; refusing a duplicate attempt") from exc
                else:
                    raise RuntimeError(f"formal worker {pid} is still alive; refusing a duplicate attempt")
        if finished is not None:
            if (
                finished.get("schema_id") != "issue79-owner-attempt-finish-v1"
                or finished.get("unit_id") != unit_id or finished.get("run_id") != run_id
                or result is None or finished.get("result_sha256") != formal.file_sha256(result_path)
                or finished.get("status") != result.get("status")
            ):
                raise RuntimeError(f"formal attempt terminal receipt is inconsistent: {finish_path}")
        if result is not None and result.get("status") == "SUCCEEDED":
            if (
                finished is None or finished.get("status") != "SUCCEEDED"
                or result.get("schema_id") != "issue75-stage1-unit-result-v1"
                or result.get("formal") is not True
                or result.get("unit_id") != unit_id
                or result.get("phase") != row.get("phase")
                or result.get("arm") != row.get("arm")
                or result.get("owner_seed") != row.get("owner_seed")
                or result.get("game_seed") != row.get("game_seed")
                or result.get("paired_owner_key") != row.get("paired_owner_key")
                or result.get("package_commit") != package_commit
                or result.get("runner_code_commit") != package_commit
                or result.get("source_lock_sha256") != lock_sha
                or result.get("native_source_commit") != formal.LOCK["native"]["commit"]
                or result.get("stsr_source_commit") != formal.LOCK["stsr_source"]["commit"]
                or result.get("native_module_sha256") != formal.LOCK["native"]["compiled_module_sha256"]
                or result.get("resource", {}).get("ledger_run_id") != run_id
            ):
                raise RuntimeError(f"formal successful result is not source/owner-bound: {result_path}")
            success_records.append((result, result_path))
        elif finished is not None or result is not None:
            status = "INCOMPLETE_OR_UNVERIFIED" if finished is None else str(finished.get("status"))
            historical.append({
                "attempt": attempt.name,
                "status": status,
                "failure_boundary": None if result is None else result.get("failure_boundary"),
                "failure_detail": "missing worker result" if result is None else str(result.get("failure_detail", ""))[:1000],
                "result_sha256": None if not result_path.is_file() else formal.file_sha256(result_path),
            })
        else:
            historical.append({"attempt": attempt.name, "status": "STARTED_WITHOUT_TERMINAL"})
    if len(success_records) > 1:
        raise RuntimeError(f"formal unit has multiple successful attempts; refusing to select one: {unit_id}")
    if success_records:
        return success_records[0][0], historical
    return None, historical


def _snapshot_stage(formal: Any, *, progress_path: Path, group: Mapping[str, Any]) -> None:
    _atomic_json(formal, progress_path, {**dict(group), "updated_utc": _now()})


def _run_formal_units(
    formal: Any, backend: Any, *, rows: list[dict[str, Any]], root: Path,
    stage_id: str, package_commit: str, source_lock_sha256: str,
    seed_table_sha256: str, authorization: Mapping[str, Any],
    issue84_source: Path, clock: Any, progress_path: Path,
    carried_unit_results: Mapping[str, Mapping[str, Any]] | None = None,
    predecessor_carry_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    if not rows or len({str(row["unit_id"]) for row in rows}) != len(rows):
        raise ValueError(f"{stage_id} must contain unique registered rows")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    active: dict[str, tuple[subprocess.Popen, Any, dict[str, Any], float]] = {}
    complete: dict[str, dict[str, Any]] = {}
    attempts_by_unit: dict[str, int] = {}
    histories: dict[str, list[dict[str, Any]]] = {}
    carried_histories: dict[str, list[dict[str, Any]]] = {}
    carried_results = {str(key): dict(value) for key, value in (carried_unit_results or {}).items()}
    pending: list[dict[str, Any]] = []
    row_ids = {str(row["unit_id"]) for row in rows}
    if not set(carried_results).issubset(row_ids):
        raise RuntimeError(f"{stage_id} continuation contains an unregistered carried owner")
    for row in rows:
        unit_id = str(row["unit_id"])
        carried = carried_results.get(unit_id)
        unit_dir = root / "units" / backend.unit_key(unit_id)
        local_attempts = sorted(path for path in unit_dir.glob("attempt-*") if path.is_dir()) if unit_dir.is_dir() else []
        local_result, local_history = _unit_attempts(
            formal, backend, root=root, row=row, package_commit=package_commit,
            lock_sha=source_lock_sha256, seed_table_sha256=seed_table_sha256,
            authorization_receipt_sha256=str(authorization["sha256"]),
        )
        if carried is not None:
            _validate_prestart_carried_attempt(carried)
            if (
                int(carried.get("attempt_count", -1)) < 0
                or str(carried.get("package_commit", "")) != RESUMED_FORMAL_PACKAGE_COMMIT
                or not isinstance(carried.get("history"), list)
                or (carried.get("result") is not None and local_attempts)
                or (carried.get("result") is not None and local_result is not None)
            ):
                raise RuntimeError(f"{stage_id} carried and successor attempts overlap or are malformed: {unit_id}")
            if carried.get("result") is None and any(
                int(path.name.removeprefix("attempt-")) <= int(carried["attempt_count"])
                for path in local_attempts
            ):
                raise RuntimeError(f"{stage_id} successor attempt numbers overlap the carried history: {unit_id}")
            carried_histories[unit_id] = [dict(item) for item in carried["history"]]
            attempts_by_unit[unit_id] = int(carried["attempt_count"]) + len(local_attempts)
            histories[unit_id] = carried_histories[unit_id] + local_history
            if carried.get("result") is not None:
                result = carried["result"]
                if (
                    result.get("unit_id") != unit_id
                    or result.get("owner_seed") != int(row["owner_seed"])
                    or result.get("status") != "SUCCEEDED"
                ):
                    raise RuntimeError(f"{stage_id} carried result differs from its registered row: {unit_id}")
                complete[unit_id] = result
                continue
        else:
            attempts_by_unit[unit_id] = len(local_attempts)
            histories[unit_id] = local_history
        if local_result is None:
            pending.append(row)
        else:
            complete[unit_id] = local_result
    group: dict[str, Any] = {
        "schema_id": "issue85-formal-live-stage-v1",
        "stage_id": stage_id,
        "status": "RUNNING",
        "package_commit": package_commit,
        "source_lock_sha256": source_lock_sha256,
        "seed_table_sha256": seed_table_sha256,
        "authorization_receipt_sha256": authorization["sha256"],
        "expected": len(rows),
        "successful_completions": len(complete),
        "successful_unit_ids": sorted(complete),
        "pending_unit_ids": [str(row["unit_id"]) for row in pending],
        "active_unit_ids": [],
        "attempt_counts": dict(attempts_by_unit),
        "historical_attempts": histories,
        "recent_failures": [],
    }
    _snapshot_stage(formal, progress_path=progress_path, group=group)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ISSUE84_SOURCE_DIR"] = str(issue84_source)
    env["ISSUE85_PILOT_ROOT"] = str(Path(formal.DATA_ROOT) / "issue85" / "artifacts" / "formal" / package_commit)
    for name in ("STS_LIGHTSPEED_SOURCE_DIR", "STSRL_SOURCE_DIR"):
        if not env.get(name):
            raise RuntimeError(f"{name} must name its exact clean, pinned source before execution")
    pinned_python = Path(backend.PINNED_PYTHON)
    if os.name == "nt" or not pinned_python.is_file() or Path(sys.executable).resolve() != pinned_python.resolve():
        raise RuntimeError("formal execution must be launched inside the source-locked pinned WSL Python runtime")
    formal_entry = Path(formal.__file__).resolve()
    stop_after_failure = False
    last_heartbeat = time.monotonic()

    def publish() -> None:
        group.update({
            "successful_completions": len(complete),
            "successful_unit_ids": sorted(complete),
            "pending_unit_ids": [str(row["unit_id"]) for row in pending],
            "active_unit_ids": sorted(str(item[2]["unit_id"]) for item in active.values()),
            "attempt_counts": dict(attempts_by_unit),
            "historical_attempts": histories,
            "updated_utc": _now(),
        })
        _snapshot_stage(formal, progress_path=progress_path, group=group)

    while pending or active:
        if clock.expired():
            stop_after_failure = True
            group["stop_reason"] = "WHOLE_TASK_ACTIVE_WALL_CAP_REACHED"
            for process, _log, job, _began in active.values():
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            for process, log, job, _began in list(active.values()):
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                log.close()
                attempt_dir = Path(job["attempt_dir"])
                formal.pilot._write_or_verify_once(attempt_dir / "global-wall-stop.json", {
                    "schema_id": "issue85-formal-global-wall-stop-v1",
                    "unit_id": job["unit_id"],
                    "run_id": job["run_id"],
                    "whole_task_active_wall_cap_seconds": authorization["whole_task_active_wall_cap_seconds"],
                    "cumulative_active_wall_seconds": clock.elapsed(),
                })
            active.clear()
            clock.set_processes(set())
            publish()
            break
        while pending and len(active) < int(backend.CONCURRENCY) and not stop_after_failure:
            row = pending.pop(0)
            unit_id = str(row["unit_id"])
            unit_root = root / "units" / backend.unit_key(unit_id)
            unit_root.mkdir(parents=True, exist_ok=True)
            attempt_number = attempts_by_unit[unit_id] + 1
            attempt_dir = unit_root / f"attempt-{attempt_number:04d}"
            attempt_dir.mkdir(parents=False, exist_ok=False)
            run_id = f"{unit_id}-{attempt_dir.name}"
            job = {
                "schema_id": "issue79-owner-worker-job-v1",
                "artifact_root": str(root),
                "attempt_dir": str(attempt_dir),
                "result_path": str(attempt_dir / "result.json"),
                "output_dir": str(attempt_dir / "native"),
                "run_id": run_id,
                "unit_id": unit_id,
                "phase": row["phase"],
                "row": row,
                "pilot": False,
                "formal": True,
                "package_commit": package_commit,
                "runner_code_commit": package_commit,
                "source_lock_sha256": source_lock_sha256,
                "seed_table_sha256": seed_table_sha256,
                "authorization_receipt_path": authorization["path"],
                "authorization_receipt_sha256": authorization["sha256"],
                "issue84_source_root": str(issue84_source),
                "supervisor_pid": os.getpid(),
            }
            job_path = attempt_dir / "worker-job.json"
            formal.pilot._write_or_verify_once(job_path, job)
            log = (attempt_dir / "worker.log").open("ab")
            process = subprocess.Popen(
                [str(pinned_python), str(formal_entry), "--worker", str(job_path)],
                cwd=Path(backend.REPO), env=env, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            active[run_id] = (process, log, job, time.monotonic())
            attempts_by_unit[unit_id] = attempt_number
            clock.set_processes({item[0].pid for item in active.values()})
            publish()
        exits = []
        for run_id, (process, log, job, began) in list(active.items()):
            code = process.poll()
            if code is not None:
                exits.append((run_id, process, log, job, began, code))
        for run_id, process, log, job, began, code in exits:
            log.close()
            unit_id = str(job["unit_id"])
            row = job["row"]
            result, history = _unit_attempts(
                formal, backend, root=root, row=row, package_commit=package_commit,
                lock_sha=source_lock_sha256, seed_table_sha256=seed_table_sha256,
                authorization_receipt_sha256=str(authorization["sha256"]),
            )
            del active[run_id]
            histories[unit_id] = carried_histories.get(unit_id, []) + history
            if code == 0 and result is not None:
                complete[unit_id] = result
            else:
                stop_after_failure = True
                group.setdefault("recent_failures", []).append({
                    "unit_id": unit_id,
                    "attempt": Path(job["attempt_dir"]).name,
                    "exit_code": code,
                    "failure_detail": str((formal.read_json(Path(job["result_path"])) if Path(job["result_path"]).is_file() else {}).get("failure_detail", "worker failed without a result"))[:1000],
                })
                group["recent_failures"] = group["recent_failures"][-10:]
            clock.set_processes({item[0].pid for item in active.values()})
            publish()
        if not exits:
            time.sleep(0.25)
            if time.monotonic() - last_heartbeat >= 5.0:
                publish()
                last_heartbeat = time.monotonic()
    for row in rows:
        unit_id = str(row["unit_id"])
        _result, history = _unit_attempts(
            formal, backend, root=root, row=row, package_commit=package_commit,
            lock_sha=source_lock_sha256, seed_table_sha256=seed_table_sha256,
            authorization_receipt_sha256=str(authorization["sha256"]),
        )
        if complete.get(unit_id) is None:
            histories[unit_id] = carried_histories.get(unit_id, []) + history
    final_units = []
    carried_sources = {}
    for row in rows:
        unit_id = str(row["unit_id"])
        carried = carried_results.get(unit_id)
        if carried is not None and carried.get("result") is not None:
            reference = dict(carried["reference"])
            source_commit = str(carried["package_commit"])
            reference.update({
                "source_package_commit": source_commit,
                "source_group_root": RESUMED_B03_GROUP_ROOT,
            })
            final_units.append(reference)
            carried_sources[unit_id] = {
                "package_commit": source_commit,
                "group_root": RESUMED_B03_GROUP_ROOT,
            }
            continue
        result, result_path = backend.existing_success_record(root, row, package_commit) if complete.get(unit_id) is not None else (None, None)
        if result is None or result_path is None:
            continue
        final_units.append({
            "unit_id": unit_id,
            "owner_seed": int(row["owner_seed"]),
            "attempt_path": result_path.parent.relative_to(Path(formal.DATA_ROOT) / "issue85" / "artifacts" / "formal" / package_commit).as_posix(),
            "result_sha256": formal.file_sha256(result_path),
            "model_digest": str(result["model_digest"]),
            "attempt_count": attempts_by_unit[unit_id],
        })
    final = {
        "schema_id": GROUP_SCHEMA,
        "stage_id": stage_id,
        "status": "SUCCEEDED" if len(final_units) == len(rows) else str(group.get("stop_reason", "PAUSED_AFTER_FAILED_ATTEMPT")),
        "package_commit": package_commit,
        "source_lock_sha256": source_lock_sha256,
        "seed_table_sha256": seed_table_sha256,
        "authorization_receipt_sha256": authorization["sha256"],
        "expected": len(rows),
        "completed": len(final_units),
        "successful_unit_ids": [row["unit_id"] for row in final_units],
        "units": final_units,
        "historical_attempts": histories,
        "failed_attempt_count": sum(1 for entries in histories.values() for item in entries if item.get("status") != "SUCCEEDED"),
        "active_wall_seconds_at_stage_close": clock.elapsed(),
    }
    if carried_sources:
        final["carried_unit_sources"] = carried_sources
        final["training_data_package_commit"] = package_commit
        final["predecessor_carry_manifest_sha256"] = predecessor_carry_manifest_sha256
    if rows[0].get("issue85_phase") == "validation":
        final["model_selection_or_tuning"] = False
    group_receipt = root / "group-receipt.json"
    if final["status"] == "SUCCEEDED":
        if group_receipt.is_file():
            existing = formal.read_json(group_receipt)
            comparable_existing = {
                key: value for key, value in existing.items()
                if key != "active_wall_seconds_at_stage_close"
            }
            comparable_final = {
                key: value for key, value in final.items()
                if key != "active_wall_seconds_at_stage_close"
            }
            if comparable_existing != comparable_final:
                raise RuntimeError("immutable formal training group receipt differs on resumed reconstruction")
            final = existing
            digest = formal.file_sha256(group_receipt)
        else:
            digest = formal.pilot._write_or_verify_once(group_receipt, final)
        final["receipt_path"] = str(group_receipt)
        final["receipt_sha256"] = digest
    _snapshot_stage(formal, progress_path=progress_path, group={**final, "updated_utc": _now()})
    return final


def _training_rows(seed: Mapping[str, Any], arm: str, block: int) -> list[dict[str, Any]]:
    return sorted(
        (dict(row) for row in seed["rows"] if row.get("issue85_phase") == "training"
         and row.get("issue85_arm") == arm and row.get("block_index") == block),
        key=lambda row: int(row["owner_seed"]),
    )


def _policy_rows(seed: Mapping[str, Any], phase: str, policy: str) -> list[dict[str, Any]]:
    return sorted(
        (dict(row) for row in seed["rows"] if row.get("issue85_phase") == phase and row.get("arm") == policy),
        key=lambda row: int(row["owner_seed"]),
    )


def _owner_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(",".join(str(int(row["owner_seed"])) for row in rows).encode("ascii")).hexdigest()


def _read_update_receipt(
    formal: Any, package_root: Path, identity: Mapping[str, str], *, arm: str, block: int,
) -> dict[str, Any]:
    path = package_root / "training" / arm / "updates" / f"block-{block:02d}" / "success-receipt.json"
    if arm == "B" and block in (1, 2) and _resumed_formal_continuation_required(str(identity.get("package_commit", ""))):
        carry_manifest, carry_manifest_sha256 = _validate_carry_manifest(formal, package_root, identity)
        continuation = carry_manifest.get("resumed_formal_continuation")
        carry_key = "b01_actor_update" if block == 1 else "b02_actor_update"
        carry = continuation.get(carry_key) if isinstance(continuation, Mapping) else None
        if not isinstance(carry, Mapping):
            raise RuntimeError(f"B/{block:02d} update has no reviewed b8 source receipt")
        if path.parent.exists() and any(path.parent.iterdir()):
            raise RuntimeError(f"B/{block:02d} update is both carried by reference and has local attempts")
        if (
            carry.get("arm") != "B" or carry.get("block_index") != block
            or carry.get("source_package_commit") != RESUMED_FORMAL_PACKAGE_COMMIT
            or carry.get("source_package_root") != RESUMED_FORMAL_PACKAGE_ROOT
            or carry.get("source_lock_sha256") != RESUMED_FORMAL_SOURCE_LOCK_SHA256
        ):
            raise RuntimeError(f"B/{block:02d} actor-update carry is bound to another source")
        source_root = _inside(Path(formal.DATA_ROOT), RESUMED_FORMAL_PACKAGE_ROOT)
        source_receipt = _read_update_receipt(
            formal, source_root,
            {"package_commit": RESUMED_FORMAL_PACKAGE_COMMIT, "source_lock_sha256": RESUMED_FORMAL_SOURCE_LOCK_SHA256},
            arm="B", block=block,
        )
        bindings = {
            "source_update_receipt_path": "receipt_path",
            "source_update_receipt_file_sha256": "receipt_file_sha256",
            "receipt_sha256": "receipt_sha256",
            "training_group_receipt_sha256": "training_group_receipt_sha256",
            "training_data_package_commit": "training_data_package_commit",
            "source_predecessor_carry_manifest_sha256": "predecessor_carry_manifest_sha256",
            "actor_checkpoint_path": "actor_checkpoint_path",
            "actor_checkpoint_sha256": "actor_checkpoint_sha256",
            "optimizer_checkpoint_path": "optimizer_checkpoint_path",
            "optimizer_checkpoint_sha256": "optimizer_checkpoint_sha256",
            "result_model_digest": "result_model_digest",
            "completed_steps": "completed_steps",
            "actor_update_seed": "actor_update_seed",
            "actor_update_schedule_sha256": "actor_update_schedule_sha256",
            "owner_seed_sha256": "owner_seed_sha256",
        }
        if any(carry.get(carry_key) != source_receipt.get(receipt_key) for carry_key, receipt_key in bindings.items()):
            raise RuntimeError(f"B/{block:02d} carried actor update differs from its immutable b8 source receipt")
        return {
            **source_receipt,
            "producer_package_root": str(source_root),
            "carried_from_package_commit": RESUMED_FORMAL_PACKAGE_COMMIT,
            "carried_by_manifest_sha256": carry_manifest_sha256,
        }
    if arm == "B" and block == 0:
        carry_manifest, carry_manifest_sha256 = _validate_carry_manifest(formal, package_root, identity)
        carries = carry_manifest.get("actor_update_carries", [])
        if carries:
            if path.parent.exists() and any(path.parent.iterdir()):
                raise RuntimeError("B/00 update is both carried by reference and has local update attempts")
            if len(carries) != 1 or not isinstance(carries[0], Mapping):
                raise RuntimeError("formal predecessor carry has an invalid B/00 actor-update reference")
            carry = carries[0]
            if (
                carry.get("arm") != "B"
                or carry.get("block_index") != 0
                or carry.get("source_package_commit") != OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT
                or carry.get("source_package_root") != OPERATIONAL_PREDECESSOR_ROOT
                or carry.get("source_lock_sha256") != OPERATIONAL_PREDECESSOR_LOCK_SHA256
            ):
                raise RuntimeError("formal predecessor B/00 update reference is bound to another source")
            source_root = _inside(Path(formal.DATA_ROOT), str(carry["source_package_root"]))
            source_identity = {
                "package_commit": OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT,
                "source_lock_sha256": OPERATIONAL_PREDECESSOR_LOCK_SHA256,
            }
            source_receipt = _read_update_receipt(
                formal, source_root, source_identity, arm="B", block=0,
            )
            bindings = {
                "source_seed_table_sha256": "seed_table_sha256",
                "source_predecessor_carry_manifest_sha256": "predecessor_carry_manifest_sha256",
                "source_update_receipt_file_sha256": "receipt_file_sha256",
                "actor_checkpoint_path": "actor_checkpoint_path",
                "actor_checkpoint_sha256": "actor_checkpoint_sha256",
                "optimizer_checkpoint_path": "optimizer_checkpoint_path",
                "optimizer_checkpoint_sha256": "optimizer_checkpoint_sha256",
                "receipt_sha256": "receipt_sha256",
                "actor_update_seed": "actor_update_seed",
                "actor_update_schedule_sha256": "actor_update_schedule_sha256",
                "owner_seed_sha256": "owner_seed_sha256",
                "completed_steps": "completed_steps",
                "result_model_digest": "result_model_digest",
            }
            if any(carry.get(carry_key) != source_receipt.get(receipt_key) for carry_key, receipt_key in bindings.items()):
                raise RuntimeError("carried B/00 update reference differs from its source receipt")
            if carry.get("source_update_receipt_path") != source_receipt.get("receipt_path"):
                raise RuntimeError("carried B/00 update receipt path differs from its source record")
            return {
                **source_receipt,
                "producer_package_root": str(source_root),
                "carried_from_package_commit": OPERATIONAL_PREDECESSOR_PACKAGE_COMMIT,
                "carried_by_manifest_sha256": carry_manifest_sha256,
            }
    if not path.is_file():
        raise RuntimeError(f"formal actor update receipt is missing for {arm} block {block}")
    receipt = formal.read_json(path)
    digest = formal.file_sha256(path)
    training_data_package = identity["package_commit"]
    carry_manifest_sha256 = None
    if arm == "B" and block == 0:
        training_data_package = PREDECESSOR_PACKAGE_COMMIT
        _carry, carry_manifest_sha256 = _validate_carry_manifest(formal, package_root, identity)
    elif arm == "B" and block == 1:
        _manifest, carry_manifest_sha256 = _validate_carry_manifest(formal, package_root, identity)
        if _manifest.get("resumed_training_group_carries") != [RESUMED_B01_GROUP_CARRY]:
            raise RuntimeError("B/01 actor update receipt has no verified carried games reference")
        training_data_package = RESUMED_GAMES_PACKAGE_COMMIT
    elif arm == "B" and block == 3:
        _manifest, carry_manifest_sha256 = _validate_carry_manifest(formal, package_root, identity)
        if not isinstance(_manifest.get("resumed_formal_continuation"), Mapping):
            raise RuntimeError("B/03 actor update has no verified partial-group continuation")
        training_data_package = identity["package_commit"]
        group_receipt_path = package_root / "training/B/block-03/games/group-receipt.json"
        if (
            not group_receipt_path.is_file()
            or formal.file_sha256(group_receipt_path) != receipt.get("training_group_receipt_sha256")
        ):
            raise RuntimeError("B/03 actor update is not bound to its immutable mixed-source game group")
    if (
        receipt.get("training_data_package_commit") != training_data_package
        or receipt.get("predecessor_carry_manifest_sha256") != carry_manifest_sha256
        or (arm == "B" and block == 0 and receipt.get("training_group_receipt_sha256") != PREDECESSOR_GROUP_RECEIPT_SHA256)
        or (arm == "B" and block == 1 and receipt.get("training_group_receipt_sha256") != RESUMED_B01_GROUP_RECEIPT_SHA256)
        or (arm == "B" and block == 3 and receipt.get("predecessor_carry_manifest_sha256") != carry_manifest_sha256)
        or receipt.get("schema_id") != UPDATE_SCHEMA or receipt.get("status") != "SUCCEEDED"
        or receipt.get("arm") != arm or receipt.get("block_index") != block
        or receipt.get("package_commit") != identity["package_commit"]
        or receipt.get("source_lock_sha256") != identity["source_lock_sha256"]
        or receipt.get("owner_count") != int(formal.CONTRACT["training"]["owners_per_block"])
        or receipt.get("completed_steps") != int(formal.CONTRACT["training"]["updates_per_block"])
        or receipt.get("minibatch_owners") != int(formal.CONTRACT["training"]["minibatch_owners"])
        or receipt.get("receipt_sha256") != _hash_json(formal, {key: value for key, value in receipt.items() if key != "receipt_sha256"})
        or formal.file_sha256(_inside(package_root, str(receipt.get("actor_checkpoint_path", "")))) != receipt.get("actor_checkpoint_sha256")
        or formal.file_sha256(_inside(package_root, str(receipt.get("optimizer_checkpoint_path", "")))) != receipt.get("optimizer_checkpoint_sha256")
    ):
        raise RuntimeError(f"formal actor update receipt/checkpoint differs for {arm} block {block}")
    return {
        **receipt,
        "receipt_file_sha256": digest,
        "receipt_path": path.relative_to(package_root).as_posix(),
        "producer_package_root": str(package_root),
    }


def _next_attempt(update_root: Path) -> int:
    attempts = [path for path in update_root.glob("attempt-*") if path.is_dir()]
    numbers = []
    for path in attempts:
        match = re.fullmatch(r"attempt-([0-9]{4,})", path.name)
        if match is None:
            raise RuntimeError(f"formal actor update has an invalid attempt directory: {path}")
        numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def _rng_to_json(state: tuple[Any, ...]) -> dict[str, Any]:
    return {"version": int(state[0]), "state": [int(value) for value in state[1]], "gauss_next": state[2]}


def _rng_from_json(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return int(value["version"]), tuple(int(item) for item in value["state"]), value.get("gauss_next")


def _valid_checkpoint(
    formal: Any, checkpoint_dir: Path, *, identity: Mapping[str, str], arm: str,
    block: int, owner_sha: str, train_group_sha: str, schedule_sha: str, update_seed: int,
    training_data_package_commit: str, predecessor_carry_manifest_sha256: str | None,
) -> tuple[int, dict[str, Any], Path, Path]:
    metadata_path = checkpoint_dir / "checkpoint.json"
    actor_path = checkpoint_dir / "actor-state.pt"
    optimizer_path = checkpoint_dir / "optimizer-state.pt"
    metadata = formal.read_json(metadata_path)
    step = metadata.get("completed_steps")
    interval = 8
    if (
        metadata.get("schema_id") != "issue85-formal-actor-checkpoint-v1"
        or metadata.get("package_commit") != identity["package_commit"]
        or metadata.get("source_lock_sha256") != identity["source_lock_sha256"]
        or metadata.get("arm") != arm or metadata.get("block_index") != block
        or metadata.get("owner_seed_sha256") != owner_sha
        or metadata.get("training_group_receipt_sha256") != train_group_sha
        or metadata.get("training_data_package_commit") != training_data_package_commit
        or metadata.get("predecessor_carry_manifest_sha256") != predecessor_carry_manifest_sha256
        or metadata.get("actor_update_schedule_sha256") != schedule_sha
        or metadata.get("actor_update_seed") != update_seed
        or isinstance(step, bool) or not isinstance(step, int) or step <= 0 or step > 64 or step % interval
        or not actor_path.is_file() or not optimizer_path.is_file()
        or formal.file_sha256(actor_path) != metadata.get("actor_checkpoint_sha256")
        or formal.file_sha256(optimizer_path) != metadata.get("optimizer_checkpoint_sha256")
        or len(metadata.get("diagnostics", [])) != step
        or len(metadata.get("owner_order", [])) != 128
        or len(metadata.get("rng_state", {}).get("state", [])) != 625
    ):
        raise RuntimeError(f"formal actor update checkpoint is stale or corrupt: {checkpoint_dir}")
    return step, metadata, actor_path, optimizer_path


def _latest_resume_checkpoint(
    formal: Any, update_root: Path, *, identity: Mapping[str, str], arm: str, block: int,
    owner_sha: str, train_group_sha: str, schedule_sha: str, update_seed: int,
    training_data_package_commit: str, predecessor_carry_manifest_sha256: str | None,
) -> tuple[int, dict[str, Any], Path, Path] | None:
    candidates = []
    for attempt in sorted(path for path in update_root.glob("attempt-*") if path.is_dir()):
        for checkpoint in sorted((attempt / "checkpoints").glob("step-*") if (attempt / "checkpoints").is_dir() else []):
            candidates.append(_valid_checkpoint(
                formal, checkpoint, identity=identity, arm=arm, block=block,
                owner_sha=owner_sha, train_group_sha=train_group_sha,
                schedule_sha=schedule_sha, update_seed=update_seed,
                training_data_package_commit=training_data_package_commit,
                predecessor_carry_manifest_sha256=predecessor_carry_manifest_sha256,
            ))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    if len({item[0] for item in candidates}) != len(candidates):
        raise RuntimeError("formal actor update has multiple checkpoints at the same step")
    return candidates[-1]


def _validate_formal_adamw_lineage(
    actual_steps: Sequence[int], *, parameter_count: int, allowed_steps: set[int],
    arm: str, block: int, context: str,
) -> None:
    """Check every trainable parameter has an allowed AdamW step count."""
    if (
        len(actual_steps) != parameter_count
        or not actual_steps
        or any(step not in allowed_steps for step in actual_steps)
    ):
        label = "resumed AdamW step state is invalid" if context == "resumed" else "formal AdamW lineage differs"
        raise RuntimeError(f"{label} for {arm}/{block}: {list(actual_steps)}")


def _train_actor_block(
    formal: Any, backend: Any, issue84_driver: Any, *, arm: str, block: int,
    rows: list[dict[str, Any]], package_root: Path, identity: Mapping[str, str],
    seed_table: Mapping[str, Any], seed_table_sha256: str,
    training_group: Mapping[str, Any], clock: Any,
) -> dict[str, Any]:
    expected_owners = int(formal.CONTRACT["training"]["owners_per_block"])
    expected_steps = int(formal.CONTRACT["training"]["updates_per_block"])
    minibatch_owners = int(formal.CONTRACT["training"]["minibatch_owners"])
    if len(rows) != expected_owners or [int(row["owner_seed"]) for row in rows] != list(range(rows[0]["owner_seed"], rows[0]["owner_seed"] + expected_owners)):
        raise RuntimeError(f"formal actor update {arm}/{block} does not have its exact 128-owner block")
    update_row = next(
        item for item in seed_table["actor_update_schedule"]
        if item["arm"] == arm and item["block_index"] == block
    )
    schedule_sha = seed_table["actor_update_schedule_sha256"]
    update_seed = int(update_row["seed"])
    owner_sha = _owner_hash(rows)
    group_sha = str(training_group["receipt_sha256"])
    training_data_package = str(training_group.get("training_data_package_commit", identity["package_commit"]))
    carry_manifest_sha256 = training_group.get("predecessor_carry_manifest_sha256")
    predecessor_carry_manifest = None
    predecessor_carry_sha256 = None
    if arm == "B" and block == 0:
        predecessor_carry_manifest, predecessor_carry_sha256 = _validate_carry_manifest(
            formal, package_root, identity,
            expected_sha256=str(carry_manifest_sha256) if carry_manifest_sha256 is not None else None,
        )
        if (
            training_data_package != PREDECESSOR_PACKAGE_COMMIT
            or group_sha != PREDECESSOR_GROUP_RECEIPT_SHA256
            or carry_manifest_sha256 != predecessor_carry_sha256
        ):
            raise RuntimeError("B/block-00 actor update is not bound to the verified predecessor group")
    elif arm == "B" and block == 1:
        _manifest, expected_carry_sha256 = _validate_carry_manifest(
            formal, package_root, identity,
            expected_sha256=str(carry_manifest_sha256) if carry_manifest_sha256 is not None else None,
        )
        if (
            training_data_package != RESUMED_GAMES_PACKAGE_COMMIT
            or group_sha != RESUMED_B01_GROUP_RECEIPT_SHA256
            or carry_manifest_sha256 != expected_carry_sha256
            or _manifest.get("resumed_training_group_carries") != [RESUMED_B01_GROUP_CARRY]
        ):
            raise RuntimeError("B/block-01 actor update is not bound to the verified resumed game group")
    elif arm == "B" and block == 3:
        manifest, expected_carry_sha256 = _validate_carry_manifest(
            formal, package_root, identity,
            expected_sha256=str(carry_manifest_sha256) if carry_manifest_sha256 is not None else None,
        )
        carried_sources = training_group.get("carried_unit_sources")
        group_receipt_path = package_root / "training/B/block-03/games/group-receipt.json"
        if (
            training_data_package != identity["package_commit"]
            or carry_manifest_sha256 != expected_carry_sha256
            or not isinstance(manifest.get("resumed_formal_continuation"), Mapping)
            or not isinstance(carried_sources, Mapping)
            or len(carried_sources) != RESUMED_B03_COMPLETED
            or not group_receipt_path.is_file()
            or formal.file_sha256(group_receipt_path) != group_sha
        ):
            raise RuntimeError("B/block-03 actor update is not bound to the reviewed mixed-source game group")
    elif training_data_package != identity["package_commit"] or carry_manifest_sha256 is not None:
        raise RuntimeError("non-predecessor actor update has an unexpected training-data package")
    update_root = package_root / "training" / arm / "updates" / f"block-{block:02d}"
    success_path = update_root / "success-receipt.json"
    if success_path.is_file():
        return _read_update_receipt(formal, package_root, identity, arm=arm, block=block)
    if arm == "B" and block == 0:
        if predecessor_carry_manifest is None:
            raise RuntimeError("B/block-00 update lost its validated predecessor carry")
        if predecessor_carry_manifest.get("actor_update_carries"):
            return _read_update_receipt(formal, package_root, identity, arm=arm, block=block)
    update_root.mkdir(parents=True, exist_ok=True)
    loaded = backend.load_modules(update_root, pilot=True)
    _algorithm, _runtime, model, learning, executor = loaded
    row_token = formal.pilot._CURRENT_ROW.set(rows[0])
    try:
        actor, source_model_digest, source_record = executor._load_model(f"pi{12 + block}", proxy=False)
    finally:
        formal.pilot._CURRENT_ROW.reset(row_token)
    actor.train()
    carried_sources = training_group.get("carried_unit_sources", {})
    receipt_roots_by_owner = {}
    if carried_sources:
        for unit_id, source in carried_sources.items():
            if not isinstance(source, Mapping) or source.get("package_commit") != RESUMED_FORMAL_PACKAGE_COMMIT:
                raise RuntimeError(f"mixed-source training group has a malformed carried receipt binding: {unit_id}")
            receipt_roots_by_owner[str(unit_id)] = (
                _inside(Path(formal.DATA_ROOT), str(source.get("group_root", ""))),
                str(source["package_commit"]),
            )
        examples = formal.pilot._load_pilot_training_examples(
            issue84_driver, rows, root=Path(training_group["group_root"]),
            executor=executor, package_commit=training_data_package,
            receipt_roots_by_owner=receipt_roots_by_owner,
        )
        _outcomes = None
    elif arm == "B" and block == 0:
        if predecessor_carry_manifest is None or predecessor_carry_sha256 is None:
            raise RuntimeError("B/block-00 training lacks its validated predecessor content hashes")
        examples, _outcomes = _load_pinned_predecessor_training_outcomes(
            formal, backend, issue84_driver, rows,
            training_group=training_group,
            carry_manifest=predecessor_carry_manifest,
            carry_manifest_sha256=predecessor_carry_sha256,
            executor=executor, package=training_data_package,
        )
    else:
        examples, _outcomes = issue84_driver.load_training_outcomes(
            rows, root=Path(training_group["group_root"]), executor=executor,
            package=training_data_package,
        )
    if len(examples) != expected_owners or [int(item.owner_seed) for item in examples] != [int(row["owner_seed"]) for row in rows]:
        raise RuntimeError(f"formal learner {arm}/{block} loaded an incorrect owner set or order")
    if any(not getattr(getattr(item, "base", None), "complete", False) for item in examples):
        raise RuntimeError(f"formal learner {arm}/{block} includes an incomplete natural game")
    # Loader API returns examples and a parallel outcome set; the Issue 85
    # gradients bind all 32 sampled owners against the complete 128-owner block.
    owner_outcomes = examples
    gradients = __import__("gradients")
    from representations import continue_parent_adamw
    parent_optimizer = formal.LOCK["baseline_P"]
    optimizer_path = formal.pilot._path_from_wsl(formal.DATA_ROOT_WSL) / parent_optimizer["optimizer_data_path"]
    if formal.file_sha256(optimizer_path) != parent_optimizer["optimizer_sha256"]:
        raise RuntimeError("formal block 0 cannot inherit the exact locked pi12 AdamW checkpoint")
    import torch
    if block == 0:
        optimizer_bundle = torch.load(optimizer_path, map_location="cpu", weights_only=True)
        optimizer, optimizer_mapping = continue_parent_adamw(
            actor, optimizer_bundle, expected_parent_digest=parent_optimizer["model_digest"],
        )
    else:
        optimizer = learning._new_optimizer(actor)
        optimizer_mapping = {"source": "fresh Issue 84 AdamW for this registered block", "carried_parent_state": False}

    resume = _latest_resume_checkpoint(
        formal, update_root, identity=identity, arm=arm, block=block,
        owner_sha=owner_sha, train_group_sha=group_sha,
        schedule_sha=schedule_sha, update_seed=update_seed,
        training_data_package_commit=training_data_package,
        predecessor_carry_manifest_sha256=carry_manifest_sha256,
    )
    attempt_number = _next_attempt(update_root)
    attempt = update_root / f"attempt-{attempt_number:04d}"
    attempt.mkdir(parents=False, exist_ok=False)
    rng = random.Random(update_seed)
    order = list(range(expected_owners))
    completed = 0
    diagnostics: list[dict[str, Any]] = []
    if resume is not None:
        completed, checkpoint_meta, actor_path, optimizer_checkpoint = resume
        actor.load_state_dict(torch.load(actor_path, map_location="cpu", weights_only=True), strict=True)
        optimizer.load_state_dict(torch.load(optimizer_checkpoint, map_location="cpu", weights_only=True))
        rng.setstate(_rng_from_json(checkpoint_meta["rng_state"]))
        order = [int(value) for value in checkpoint_meta["owner_order"]]
        diagnostics = list(checkpoint_meta["diagnostics"])
        parameter_steps = sorted(
            int(value["step"].item()) for value in optimizer.state.values()
            if isinstance(value, dict) and "step" in value
        )
        expected_step_values = {64 + completed, completed} if block == 0 else {completed}
        _validate_formal_adamw_lineage(
            parameter_steps,
            parameter_count=len(tuple(actor.parameters())),
            allowed_steps=expected_step_values,
            arm=arm,
            block=block,
            context="resumed",
        )
    last_checkpoint: dict[str, Any] | None = None
    status = "RUNNING"
    began = time.monotonic()
    attempts_live = {
        "schema_id": "issue85-formal-actor-update-progress-v1",
        "status": status,
        "arm": arm,
        "block_index": block,
        "attempt": attempt.name,
        "package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "seed_table_sha256": seed_table_sha256,
        "training_group_receipt_sha256": group_sha,
        "training_data_package_commit": training_data_package,
        "predecessor_carry_manifest_sha256": carry_manifest_sha256,
        "owner_seed_sha256": owner_sha,
        "actor_update_schedule_sha256": schedule_sha,
        "actor_update_seed": update_seed,
        "completed_steps": completed,
        "resumed_from": None if resume is None else {
            "checkpoint_path": resume[2].relative_to(package_root).as_posix(),
            "checkpoint_sha256": formal.file_sha256(resume[2]),
            "completed_steps": completed,
        },
        "started_utc": _now(),
    }
    progress_path = attempt / "progress.json"
    _atomic_json(formal, progress_path, attempts_live)
    attempt_rng = random.Random(update_seed)
    minibatch_samples: list[dict[str, Any]] = []

    def save_checkpoint(step: int) -> dict[str, Any]:
        checkpoint_dir = attempt / "checkpoints" / f"step-{step:03d}"
        checkpoint_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = checkpoint_dir.with_name(f".{checkpoint_dir.name}.tmp-{os.getpid()}-{time.time_ns()}")
        temporary.mkdir(parents=False, exist_ok=False)
        actor_path = temporary / "actor-state.pt"
        optimizer_state_path = temporary / "optimizer-state.pt"
        actor_sha = formal.pilot._atomic_torch_save(actor_path, actor.state_dict())
        optimizer_sha = formal.pilot._atomic_torch_save(optimizer_state_path, optimizer.state_dict())
        metadata = {
            "schema_id": "issue85-formal-actor-checkpoint-v1",
            "package_commit": identity["package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "seed_table_sha256": seed_table_sha256,
            "training_group_receipt_sha256": group_sha,
            "training_data_package_commit": training_data_package,
            "predecessor_carry_manifest_sha256": carry_manifest_sha256,
            "arm": arm,
            "block_index": block,
            "owner_seed_sha256": owner_sha,
            "actor_update_schedule_sha256": schedule_sha,
            "actor_update_seed": update_seed,
            "completed_steps": step,
            "owner_order": list(order),
            "rng_state": _rng_to_json(rng.getstate()),
            "diagnostics": list(diagnostics),
            "minibatch_owner_ids": list(minibatch_samples),
            "actor_checkpoint_sha256": actor_sha,
            "optimizer_checkpoint_sha256": optimizer_sha,
            "elapsed_seconds": time.monotonic() - began,
        }
        _atomic_json(formal, temporary / "checkpoint.json", metadata)
        os.replace(temporary, checkpoint_dir)
        attempts_live.update({
            "completed_steps": step,
            "last_checkpoint": {
                "path": checkpoint_dir.relative_to(package_root).as_posix(),
                "checkpoint_sha256": actor_sha,
                "optimizer_checkpoint_sha256": optimizer_sha,
                "completed_steps": step,
            },
            "diagnostics": list(diagnostics),
            "heartbeat_utc": _now(),
        })
        _atomic_json(formal, progress_path, attempts_live)
        return {**metadata, "checkpoint_dir": checkpoint_dir}

    try:
        rng.setstate(_rng_from_json(resume[1]["rng_state"])) if resume is not None else None
        order = list(range(expected_owners)) if resume is None else list(resume[1]["owner_order"])
        minibatch_samples = [] if resume is None else list(resume[1]["minibatch_owner_ids"])
        batches_per_epoch = expected_owners // minibatch_owners
        for step in range(completed, expected_steps):
            if clock.expired():
                raise TimeoutError("whole-task active-wall cap reached during a formal actor update")
            if step % batches_per_epoch == 0:
                rng.shuffle(order)
            offset = (step % batches_per_epoch) * minibatch_owners
            indices = order[offset:offset + minibatch_owners]
            if len(indices) != minibatch_owners or len(set(indices)) != minibatch_owners:
                raise RuntimeError("formal actor minibatch is not exactly 32 distinct owners")
            minibatch_samples = [*minibatch_samples, [int(rows[index]["owner_seed"]) for index in indices]]
            loss = gradients.minibatch_loss(
                actor, [owner_outcomes[index] for index in indices],
                arm=arm, block_examples=owner_outcomes,
            )
            loss_value = float(loss.detach().item())
            if not math.isfinite(loss_value):
                raise RuntimeError(f"formal actor loss became non-finite at step {step + 1}")
            parameters = tuple(parameter for parameter in actor.parameters() if parameter.requires_grad)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            grad_norm = float(torch.nn.utils.clip_grad_norm_(parameters, formal.pilot._issue84_actor_gradient_norm_clip(issue84_driver)).item())
            if not math.isfinite(grad_norm):
                raise RuntimeError(f"formal actor gradient norm became non-finite at step {step + 1}")
            optimizer.step()
            diagnostics.append({"step": step + 1, "loss": loss_value, "grad_norm_before_clip": grad_norm})
            next_step = step + 1
            if next_step % 8 == 0 or next_step == expected_steps:
                if not all(bool(torch.isfinite(parameter).all().item()) for parameter in actor.parameters()):
                    raise RuntimeError(f"formal actor parameters became non-finite at step {next_step}")
                for state in optimizer.state.values():
                    for value in state.values():
                        if torch.is_tensor(value) and value.is_floating_point() and not bool(torch.isfinite(value).all().item()):
                            raise RuntimeError(f"formal AdamW state became non-finite at step {next_step}")
                last_checkpoint = save_checkpoint(next_step)
        actual_steps = sorted(
            int(value["step"].item()) for value in optimizer.state.values()
            if isinstance(value, dict) and "step" in value
        )
        expected_step_values = {64, 128} if block == 0 else {64}
        _validate_formal_adamw_lineage(
            actual_steps,
            parameter_count=len(tuple(actor.parameters())),
            allowed_steps=expected_step_values,
            arm=arm,
            block=block,
            context="formal",
        )
        actor.eval()
        digest = model.model_digest(actor)
        actor_path = attempt / "actor-state.pt"
        optimizer_path = attempt / "optimizer-state.pt"
        actor_sha = formal.pilot._atomic_torch_save(actor_path, actor.state_dict())
        optimizer_sha = formal.pilot._atomic_torch_save(optimizer_path, optimizer.state_dict())
        parent_digest = formal.LOCK["baseline_P"]["model_digest"]
        receipt = {
            "schema_id": UPDATE_SCHEMA,
            "status": "SUCCEEDED",
            "scope": "REGISTERED FORMAL / NOT A VALIDATION-SELECTED CHECKPOINT",
            "arm": arm,
            "block_index": block,
            "package_commit": identity["package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "seed_table_sha256": seed_table_sha256,
            "training_group_receipt_sha256": group_sha,
            "training_data_package_commit": training_data_package,
            "predecessor_carry_manifest_sha256": carry_manifest_sha256,
            "owner_count": expected_owners,
            "owner_seed_sha256": owner_sha,
            "actor_update_schedule_sha256": schedule_sha,
            "actor_update_seed": update_seed,
            "minibatch_owners": minibatch_owners,
            "minibatch_owner_ids_by_step": minibatch_samples,
            "minibatch_owner_schedule_sha256": _hash_json(formal, minibatch_samples),
            "completed_steps": expected_steps,
            "optimizer_parameter_step_values": sorted(set(actual_steps)),
            "optimizer_parameter_count": len(actual_steps),
            "optimizer_mapping": optimizer_mapping,
            "source_model_digest": source_model_digest,
            "source_model_record": source_record,
            "parent_model_digest": parent_digest,
            "result_model_digest": digest,
            "actor_checkpoint_path": actor_path.relative_to(package_root).as_posix(),
            "actor_checkpoint_sha256": actor_sha,
            "optimizer_checkpoint_path": optimizer_path.relative_to(package_root).as_posix(),
            "optimizer_checkpoint_sha256": optimizer_sha,
            "checkpoint_path": None if last_checkpoint is None else last_checkpoint["checkpoint_dir"].relative_to(package_root).as_posix(),
            "checkpoint_sha256": None if last_checkpoint is None else last_checkpoint["actor_checkpoint_sha256"],
            "diagnostics": diagnostics,
            "loss_summary": {
                "finite_all_steps": len(diagnostics) == expected_steps and all(math.isfinite(item["loss"]) for item in diagnostics),
                "first": diagnostics[0], "last": diagnostics[-1],
            },
            "cache_equivalence_claim": "none beyond the retained 16-owner/two-update v10 evidence scope",
            "finished_utc": _now(),
        }
        if receipt["loss_summary"]["finite_all_steps"] is not True:
            raise RuntimeError("formal update diagnostics are incomplete or non-finite")
        receipt["receipt_sha256"] = _hash_json(formal, receipt)
        formal.pilot._write_or_verify_once(success_path, receipt)
        _atomic_json(formal, progress_path, {
            **attempts_live, "status": "SUCCEEDED", "completed_steps": expected_steps,
            "result_model_digest": digest, "actor_checkpoint_sha256": actor_sha,
            "optimizer_checkpoint_sha256": optimizer_sha, "finished_utc": _now(),
        })
        return _read_update_receipt(formal, package_root, identity, arm=arm, block=block)
    except BaseException as exc:
        failure = {
            "schema_id": "issue85-formal-actor-update-failure-v1",
            "status": "FAILED",
            "arm": arm,
            "block_index": block,
            "attempt": attempt.name,
            "package_commit": identity["package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "seed_table_sha256": seed_table_sha256,
            "training_group_receipt_sha256": group_sha,
            "training_data_package_commit": training_data_package,
            "predecessor_carry_manifest_sha256": carry_manifest_sha256,
            "completed_steps": int(attempts_live.get("completed_steps", completed)),
            "failure_type": type(exc).__name__,
            "failure_detail": str(exc)[:2000],
            "failed_utc": _now(),
        }
        failure["receipt_sha256"] = _hash_json(formal, failure)
        formal.pilot._write_or_verify_once(attempt / "failure-receipt.json", failure)
        attempts_live.update({"status": "FAILED", "failure": {"type": type(exc).__name__, "detail": str(exc)[:2000]}, "failed_utc": _now()})
        _atomic_json(formal, progress_path, attempts_live)
        raise


def _stage_receipt(
    formal: Any, backend: Any, *, package_root: Path, phase: str, group: str,
    rows: list[dict[str, Any]], package_commit: str, source_lock_sha256: str,
    seed_table_sha256: str, authorization_receipt_sha256: str,
) -> dict[str, Any] | None:
    root = package_root / phase / group
    successes = []
    for row in rows:
        result, _history = _unit_attempts(
            formal, backend, root=root, row=row, package_commit=package_commit,
            lock_sha=source_lock_sha256, seed_table_sha256=seed_table_sha256,
            authorization_receipt_sha256=authorization_receipt_sha256,
        )
        if result is None:
            return None
        record = backend.existing_success_record(root, row, package_commit)
        if record is None:
            return None
        result, path = record
        successes.append({
            "unit_id": row["unit_id"],
            "owner_seed": int(row["owner_seed"]),
            "attempt_path": path.parent.relative_to(package_root).as_posix(),
            "result_sha256": formal.file_sha256(path),
            "model_digest": str(result["model_digest"]),
            "attempt_count": len(list((root / "units" / backend.unit_key(row["unit_id"])).glob("attempt-*"))),
        })
    expected_id = f"{phase.upper()}-{group}"
    receipt = {
        "schema_id": GROUP_SCHEMA,
        "status": "SUCCEEDED",
        "stage_id": expected_id,
        "package_commit": package_commit,
        "source_lock_sha256": source_lock_sha256,
        "seed_table_sha256": seed_table_sha256,
        "authorization_receipt_sha256": authorization_receipt_sha256,
        "row_count": len(rows),
        "completed": len(successes),
        "units": successes,
        "model_selection_or_tuning": False if phase == "validation" else None,
        "created_utc": _now(),
    }
    receipt_path = root / "group-receipt.json"
    if receipt_path.is_file():
        existing = formal.read_json(receipt_path)
        if (
            existing.get("schema_id") != GROUP_SCHEMA
            or existing.get("status") != "SUCCEEDED"
            or existing.get("stage_id") != expected_id
            or existing.get("package_commit") != package_commit
            or existing.get("source_lock_sha256") != source_lock_sha256
            or existing.get("seed_table_sha256") != seed_table_sha256
            or existing.get("authorization_receipt_sha256") != authorization_receipt_sha256
            or existing.get("row_count") != len(rows)
            or existing.get("completed") != len(rows)
            or existing.get("units") != successes
        ):
            raise RuntimeError(f"immutable formal group receipt differs: {receipt_path}")
        receipt = existing
    else:
        formal.pilot._write_or_verify_once(receipt_path, receipt)
    return {**receipt, "receipt_sha256": formal.file_sha256(receipt_path), "group_root": str(root)}


def _run_stage(
    formal: Any, backend: Any, *, package_root: Path, seed: Mapping[str, Any],
    phase: str, group: str, clock: Any, identity: Mapping[str, str],
    seed_table_sha256: str, authorization: Mapping[str, Any], issue84_source: Path,
    progress_path: Path,
) -> dict[str, Any]:
    rows = _policy_rows(seed, phase, group)
    expected = len(formal._registered_ranges()[phase])
    if len(rows) != expected:
        raise RuntimeError(f"formal {phase}/{group} group has {len(rows)} rows, expected {expected}")
    root = package_root / phase / group
    receipt = _stage_receipt(
        formal, backend, package_root=package_root, phase=phase, group=group,
        rows=rows, package_commit=identity["package_commit"],
        source_lock_sha256=identity["source_lock_sha256"], seed_table_sha256=seed_table_sha256,
        authorization_receipt_sha256=str(authorization["sha256"]),
    )
    if receipt is not None:
        return receipt
    return _run_formal_units(
        formal, backend, rows=rows, root=root, stage_id=f"{phase.upper()}-{group}",
        package_commit=identity["package_commit"], source_lock_sha256=identity["source_lock_sha256"],
        seed_table_sha256=seed_table_sha256, authorization=authorization,
        issue84_source=issue84_source, clock=clock,
        progress_path=progress_path.with_name(f"live-{phase}-{group}.json"),
    ) | {"group_root": str(root)}


def _run_training_games(
    formal: Any, backend: Any, *, package_root: Path, seed: Mapping[str, Any],
    arm: str, block: int, clock: Any, identity: Mapping[str, str],
    seed_table_sha256: str, authorization: Mapping[str, Any], issue84_source: Path,
    progress_path: Path,
    resumed_formal_continuation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rows = _training_rows(seed, arm, block)
    expected = int(formal.CONTRACT["training"]["owners_per_block"])
    if len(rows) != expected:
        raise RuntimeError(f"formal training {arm}/{block} has {len(rows)} rows, expected {expected}")
    root = package_root / "training" / arm / f"block-{block:02d}" / "games"
    existing = []
    for row in rows:
        result, _history = _unit_attempts(
            formal, backend, root=root, row=row, package_commit=identity["package_commit"],
            lock_sha=identity["source_lock_sha256"], seed_table_sha256=seed_table_sha256,
            authorization_receipt_sha256=str(authorization["sha256"]),
        )
        existing.append(result)
    if all(result is not None for result in existing):
        successes = []
        for row in rows:
            result, path = backend.existing_success_record(root, row, identity["package_commit"])
            if result is None:
                raise RuntimeError("formal training game success was not recoverable")
            successes.append({
                "unit_id": row["unit_id"], "owner_seed": int(row["owner_seed"]),
                "attempt_path": path.parent.relative_to(package_root).as_posix(),
                "result_sha256": formal.file_sha256(path),
                "attempt_count": len(list((root / "units" / backend.unit_key(row["unit_id"])).glob("attempt-*"))),
            })
        receipt = {
            "schema_id": GROUP_SCHEMA, "status": "SUCCEEDED",
            "stage_id": f"TRAIN-{arm}-BLOCK-{block:02d}-GAMES",
            "package_commit": identity["package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "seed_table_sha256": seed_table_sha256,
            "authorization_receipt_sha256": str(authorization["sha256"]),
            "row_count": len(rows), "completed": len(successes), "units": successes,
            "created_utc": _now(),
        }
        receipt_path = root / "group-receipt.json"
        if receipt_path.is_file():
            existing_receipt = formal.read_json(receipt_path)
            if {key: value for key, value in existing_receipt.items() if key != "created_utc"} != {key: value for key, value in receipt.items() if key != "created_utc"}:
                raise RuntimeError("immutable formal training group receipt differs")
            receipt = existing_receipt
        else:
            formal.pilot._write_or_verify_once(receipt_path, receipt)
        return {**receipt, "receipt_sha256": formal.file_sha256(receipt_path), "group_root": str(root)}
    carried_unit_results = None
    carry_manifest_sha256 = None
    if arm == "B" and block == 3:
        if not isinstance(resumed_formal_continuation, Mapping):
            raise RuntimeError("B/03 continuation requires the reviewed read-only b8 package references")
        carried_unit_results = resumed_formal_continuation.get("b03_carried_units")
        carry_manifest_sha256 = resumed_formal_continuation.get("manifest_sha256")
        if not isinstance(carried_unit_results, Mapping) or len(carried_unit_results) != RESUMED_B03_COMPLETED + RESUMED_B03_FAILED:
            raise RuntimeError("B/03 continuation reference count differs from retained progress")
    return _run_formal_units(
        formal, backend, rows=rows, root=root,
        stage_id=f"TRAIN-{arm}-BLOCK-{block:02d}-GAMES",
        package_commit=identity["package_commit"], source_lock_sha256=identity["source_lock_sha256"],
        seed_table_sha256=seed_table_sha256, authorization=authorization,
        issue84_source=issue84_source, clock=clock,
        progress_path=progress_path.with_name(f"live-training-{arm}-{block:02d}.json"),
        carried_unit_results=carried_unit_results,
        predecessor_carry_manifest_sha256=carry_manifest_sha256,
    ) | {"group_root": str(root)}


def _test_open_receipt(
    formal: Any, *, package_root: Path, identity: Mapping[str, str], readiness: Mapping[str, Any],
    seed: Mapping[str, Any], authorization: Mapping[str, Any],
    validation_groups: Mapping[str, Mapping[str, Any]], model_digests: Mapping[str, str],
) -> tuple[Path, str]:
    expected_policies = set(formal.POLICIES)
    validation_count = len(formal._registered_ranges()["validation"])
    if set(validation_groups) != expected_policies or set(model_digests) != expected_policies:
        raise RuntimeError("TEST remains closed until all seven exact validation groups and final models are frozen")
    if any(
        not isinstance(validation_groups[policy], Mapping)
        or validation_groups[policy].get("status") != "SUCCEEDED"
        or type(validation_groups[policy].get("completed")) is not int
        or validation_groups[policy].get("completed") != validation_count
        or validation_groups[policy].get("row_count") != validation_count
        or validation_groups[policy].get("stage_id") != f"VALIDATION-{policy}"
        or validation_groups[policy].get("schema_id") != GROUP_SCHEMA
        or validation_groups[policy].get("package_commit") != identity["package_commit"]
        or validation_groups[policy].get("source_lock_sha256") != identity["source_lock_sha256"]
        or validation_groups[policy].get("seed_table_sha256") != readiness.get("formal_seed_table_sha256")
        or validation_groups[policy].get("authorization_receipt_sha256") != authorization.get("sha256")
        or validation_groups[policy].get("model_selection_or_tuning") is not False
        or not re.fullmatch(r"[0-9a-f]{64}", str(validation_groups[policy].get("receipt_sha256", "")))
        or not isinstance(validation_groups[policy].get("units"), list)
        or len(validation_groups[policy].get("units", [])) != validation_count
        or any(
            not isinstance(unit, Mapping)
            or unit.get("model_digest") != model_digests.get(policy)
            for unit in validation_groups[policy].get("units", [])
        )
        or not re.fullmatch(r"[0-9a-f]{64}", str(model_digests.get(policy, "")))
        for policy in formal.POLICIES
    ):
        raise RuntimeError("TEST remains closed until all seven exact validation groups and final models are frozen")
    validation_bindings = {
        policy: {
            "receipt_sha256": validation_groups[policy]["receipt_sha256"],
            "completed": validation_groups[policy]["completed"],
        }
        for policy in sorted(formal.POLICIES)
    }
    for policy in formal.POLICIES:
        item = validation_groups[policy]
        group_root = package_root / "validation" / policy
        receipt_path = group_root / "group-receipt.json"
        if (
            Path(str(item.get("group_root", ""))).resolve(strict=False) != group_root.resolve(strict=False)
            or not receipt_path.is_file()
            or formal.file_sha256(receipt_path) != item["receipt_sha256"]
            or formal.read_json(receipt_path) != {
                key: value for key, value in item.items() if key not in {"receipt_sha256", "group_root"}
            }
        ):
            raise RuntimeError(f"TEST remains closed until the exact {policy} validation receipt is verified")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", str(authorization.get("sha256", "")))
        or not _issue_comment_url(authorization.get("owner_approval_comment_url"))
        or not _issue_comment_url(authorization.get("reviewer_approval_comment_url"))
    ):
        raise RuntimeError("TEST remains closed until both source-bound approval comment receipts are valid")
    record = {
        "schema_id": TEST_OPEN_SCHEMA,
        "status": "TEST_OPEN / ALL SOURCES, SEEDS, ANALYSIS, VALIDATION, MODELS, OWNER GO AND REVIEW FROZEN",
        "package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "contract_sha256": formal.file_sha256(formal.CONTRACT_PATH),
        "seed_table_sha256": readiness["formal_seed_table_sha256"],
        "analysis_source_sha256": formal.file_sha256(formal.STUDY / "formal_analysis.py"),
        "actor_update_schedule_sha256": seed["actor_update_schedule_sha256"],
        "authorization_receipt_sha256": authorization["sha256"],
        "owner_approval_comment_url": authorization["owner_approval_comment_url"],
        "reviewer_approval_comment_url": authorization["reviewer_approval_comment_url"],
        "validation_group_receipts": validation_bindings,
        "validation_used_for_model_selection": False,
        "test_owner_range": list(formal.CONTRACT["registered_design_unchanged"]["test"]["owners"]),
        "test_policy_models": dict(sorted(model_digests.items())),
        "test_games_started_before_open_receipt": 0,
    }
    path = package_root / "test-open-receipt.json"
    if path.is_file():
        saved = formal.read_json(path)
        opened_utc = saved.get("opened_utc")
        try:
            parsed_opened_utc = datetime.fromisoformat(opened_utc) if isinstance(opened_utc, str) else None
        except ValueError:
            parsed_opened_utc = None
        if (
            parsed_opened_utc is None
            or parsed_opened_utc.tzinfo is None
            or parsed_opened_utc.utcoffset() != timezone.utc.utcoffset(parsed_opened_utc)
            or type(saved.get("test_games_started_before_open_receipt")) is not int
            or saved.get("test_games_started_before_open_receipt") != 0
            or saved.get("validation_used_for_model_selection") is not False
            or saved != {**record, "opened_utc": opened_utc}
        ):
            raise RuntimeError("existing TEST-open receipt differs from the complete frozen validation/source package")
        return path, formal.file_sha256(path)
    record["opened_utc"] = _now()
    digest = formal.pilot._write_or_verify_once(path, record)
    if formal.read_json(path) != record:
        raise RuntimeError("immutable TEST-open receipt differs from the frozen validation/source package")
    return path, digest


def _make_test_bundle(
    formal: Any, backend: Any, *, package_root: Path, identity: Mapping[str, str],
    seed_table_sha256: str, test_open_sha256: str, authorization_receipt_sha256: str,
) -> tuple[Path, str]:
    groups: dict[str, Any] = {}
    for policy in formal.POLICIES:
        rows = _policy_rows(formal.read_json(package_root / "formal-seed-table.json"), "test", policy)
        attempts = []
        for row in rows:
            root = package_root / "test" / policy
            result, _history = _unit_attempts(
                formal, backend, root=root, row=row, package_commit=identity["package_commit"],
                lock_sha=identity["source_lock_sha256"], seed_table_sha256=seed_table_sha256,
                authorization_receipt_sha256=authorization_receipt_sha256,
            )
            if result is None:
                raise RuntimeError(f"formal TEST policy {policy} lacks a validated success for {row['unit_id']}")
            record = backend.existing_success_record(root, row, identity["package_commit"])
            if record is None:
                raise RuntimeError(f"formal TEST success is not recoverable for {row['unit_id']}")
            _value, result_path = record
            attempts.append({
                "unit_id": row["unit_id"],
                "attempt_path": result_path.parent.relative_to(package_root).as_posix(),
                "result_sha256": formal.file_sha256(result_path),
                "attempt_count": len(list((root / "units" / backend.unit_key(row["unit_id"])).glob("attempt-*"))),
            })
        groups[policy] = {"attempts": attempts}
    bundle = {
        "schema_id": "issue85-formal-test-receipts-v1",
        "status": "SUCCEEDED",
        "package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "seed_table_sha256": seed_table_sha256,
        "authorization_receipt_sha256": authorization_receipt_sha256,
        "test_open_receipt_sha256": test_open_sha256,
        "formal_games_started": sum(len(item["attempts"]) for item in groups.values()),
        "groups": groups,
    }
    path = package_root / "test-receipt-bundle.json"
    digest = formal.pilot._write_or_verify_once(path, bundle)
    if formal.read_json(path) != bundle:
        raise RuntimeError("immutable TEST receipt bundle differs from successful owner receipts")
    return path, digest


def execute_formal(
    formal: Any, *, issue84_source: Path, native_source: Path, stsr_source: Path,
    authorization_receipt: Path,
) -> dict[str, Any]:
    if os.name == "nt":
        raise RuntimeError("formal execution is only supported from the pinned WSL runtime; no work was started")
    identity = formal.pilot.source_identity(require_clean=True)
    package_root, readiness, seed = _validate_readiness(formal, identity)
    authorization = validate_authorization(
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
    seed_table_path = package_root / "formal-seed-table.json"
    seed_table_sha256 = formal.file_sha256(seed_table_path)
    progress_path = package_root / "formal-progress.json"
    final_path = package_root / "formal-closeout.json"
    if final_path.is_file():
        final = formal.read_json(final_path)
        if (
            final.get("status") != "SUCCEEDED"
            or final.get("package_commit") != identity["package_commit"]
            or final.get("source_lock_sha256") != identity["source_lock_sha256"]
            or final.get("authorization_receipt_sha256") != authorization["sha256"]
        ):
            raise RuntimeError("existing formal closeout is not the same authorized execution")
        return final
    prior_progress = formal.read_json(progress_path) if progress_path.is_file() else None
    if prior_progress is not None and (
        prior_progress.get("schema_id") != PROGRESS_SCHEMA
        or prior_progress.get("package_commit") != identity["package_commit"]
        or prior_progress.get("source_lock_sha256") != identity["source_lock_sha256"]
        or prior_progress.get("seed_table_sha256") != seed_table_sha256
        or prior_progress.get("authorization_receipt_sha256") != authorization["sha256"]
        or prior_progress.get("predecessor_carry_manifest_sha256") != authorization["predecessor_carry_manifest_sha256"]
    ):
        raise RuntimeError("formal progress is bound to a different source, seed, carry manifest or owner authorization")
    backend, issue84_driver = _configure_formal_runtime(formal, issue84_source, identity, package_root)
    carry_manifest, carry_manifest_sha256 = _validate_carry_manifest(
        formal, package_root, identity,
        expected_sha256=authorization["predecessor_carry_manifest_sha256"],
    )
    if carry_manifest_sha256 != authorization["predecessor_carry_manifest_sha256"]:
        raise RuntimeError("source-bound authorization references a different predecessor carry manifest")
    resumed_formal_continuation = _validate_resumed_formal_continuation(
        formal, package_root, identity, backend,
    )
    carried_training_groups = {
        ("B", 0): _predecessor_training_group(
            formal, package_root, identity,
            expected_carry_sha256=authorization["predecessor_carry_manifest_sha256"],
        ),
        ("B", 1): _resumed_b01_training_group(
            formal, package_root, identity,
            expected_carry_sha256=authorization["predecessor_carry_manifest_sha256"],
        ),
    }
    if resumed_formal_continuation is not None:
        carried_training_groups[("B", 2)] = _resumed_b02_training_group(
            formal, package_root, identity, resumed_formal_continuation,
        )
    clock = backend.ActiveClock("issue85-task", float(authorization["whole_task_active_wall_cap_seconds"]))
    progress = prior_progress or {
        "schema_id": PROGRESS_SCHEMA,
        "status": "STARTING",
        "package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "seed_table_sha256": seed_table_sha256,
        "authorization_receipt_sha256": authorization["sha256"],
        "predecessor_carry_manifest_path": _predecessor_carry_path(package_root).relative_to(package_root).as_posix(),
        "predecessor_carry_manifest_sha256": authorization["predecessor_carry_manifest_sha256"],
        "predecessor_game_group_receipt_sha256": PREDECESSOR_GROUP_RECEIPT_SHA256,
        "game_groups": {},
        "actor_updates": {},
        "started_utc": _now(),
    }
    progress.update({"status": "RUNNING", "supervisor_pid": os.getpid(), "active_stage": "STARTUP", "clock_start_seconds": clock.elapsed_before})
    _atomic_json(formal, progress_path, progress)

    def save_progress() -> None:
        progress["cumulative_active_wall_seconds"] = clock.elapsed()
        progress["heartbeat_utc"] = _now()
        _atomic_json(formal, progress_path, progress)

    try:
        # Complete all five 1,024-owner learner lineages before validation.
        for arm in formal.NEW_ARMS:
            for block in range(int(formal.formal_plan()["training_blocks_per_arm"])):
                if clock.expired():
                    raise TimeoutError("whole-task active-wall cap reached before the next registered training block")
                block_key = f"{arm}:{block}"
                is_carried_game_group = (arm, block) in carried_training_groups
                progress["active_stage"] = (
                    f"TRAIN-{arm}-BLOCK-{block:02d}-GAMES-CARRY-READ"
                    if is_carried_game_group else
                    f"TRAIN-{arm}-BLOCK-{block:02d}-GAMES"
                )
                save_progress()
                if is_carried_game_group:
                    game_group = dict(carried_training_groups[(arm, block)])
                else:
                    game_group = _run_training_games(
                        formal, backend, package_root=package_root, seed=seed,
                        arm=arm, block=block, clock=clock, identity=identity,
                        seed_table_sha256=seed_table_sha256, authorization=authorization,
                        issue84_source=Path(issue84_source).resolve(strict=True), progress_path=progress_path,
                        resumed_formal_continuation=resumed_formal_continuation,
                    )
                progress["game_groups"][f"TRAIN-{arm}-BLOCK-{block:02d}"] = game_group
                save_progress()
                if game_group.get("status") != "SUCCEEDED" or game_group.get("completed") != int(formal.CONTRACT["training"]["owners_per_block"]):
                    raise RuntimeError(f"formal training games paused at {arm}/{block}; attempts are retained")
                update_path = package_root / "training" / arm / "updates" / f"block-{block:02d}" / "success-receipt.json"
                if not update_path.is_file() and arm == "B" and block in (0, 1, 2) and (
                    block == 0 or resumed_formal_continuation is not None
                ):
                    progress["active_stage"] = f"TRAIN-B-BLOCK-{block:02d}-ACTOR-UPDATE-CARRY-READ"
                    save_progress()
                    progress["actor_updates"][block_key] = _read_update_receipt(
                        formal, package_root, identity, arm=arm, block=block,
                    )
                elif not update_path.is_file():
                    progress["active_stage"] = f"TRAIN-{arm}-BLOCK-{block:02d}-ACTOR-UPDATE"
                    save_progress()
                    progress["actor_updates"][block_key] = _train_actor_block(
                        formal, backend, issue84_driver, arm=arm, block=block,
                        rows=_training_rows(seed, arm, block), package_root=package_root,
                        identity=identity, seed_table=seed, seed_table_sha256=seed_table_sha256,
                        training_group=game_group, clock=clock,
                    )
                else:
                    progress["actor_updates"][block_key] = _read_update_receipt(
                        formal, package_root, identity, arm=arm, block=block,
                    )
                save_progress()
        # Validation is descriptive and never selects or tunes a checkpoint.
        validation_groups: dict[str, dict[str, Any]] = {}
        for policy in formal.POLICIES:
            progress["active_stage"] = f"VALIDATION-{policy}"
            save_progress()
            validation_groups[policy] = _run_stage(
                formal, backend, package_root=package_root, seed=seed,
                phase="validation", group=policy, clock=clock, identity=identity,
                seed_table_sha256=seed_table_sha256, authorization=authorization,
                issue84_source=Path(issue84_source).resolve(strict=True), progress_path=progress_path,
            )
            progress["game_groups"][f"VALIDATION-{policy}"] = validation_groups[policy]
            if validation_groups[policy].get("status") != "SUCCEEDED":
                raise RuntimeError(f"validation paused for {policy}; no TEST owners were opened")
            save_progress()
        model_digests = {
            "PARENT": str(formal.LOCK["baseline_P"]["model_digest"]),
            "A": str(formal.LOCK["baseline_A"]["model_digest"]),
            **{
                arm: str(_read_update_receipt(formal, package_root, identity, arm=arm, block=7)["result_model_digest"])
                for arm in formal.NEW_ARMS
            },
        }
        test_open_path = package_root / "test-open-receipt.json"
        test_open_path, test_open_sha = _test_open_receipt(
            formal, package_root=package_root, identity=identity, readiness=readiness,
            seed=seed, authorization=authorization,
            validation_groups=validation_groups, model_digests=model_digests,
        )
        progress["test_open_receipt_sha256"] = test_open_sha
        save_progress()
        test_groups: dict[str, dict[str, Any]] = {}
        for policy in formal.POLICIES:
            progress["active_stage"] = f"TEST-{policy}"
            save_progress()
            test_groups[policy] = _run_stage(
                formal, backend, package_root=package_root, seed=seed,
                phase="test", group=policy, clock=clock, identity=identity,
                seed_table_sha256=seed_table_sha256, authorization=authorization,
                issue84_source=Path(issue84_source).resolve(strict=True), progress_path=progress_path,
            )
            progress["game_groups"][f"TEST-{policy}"] = test_groups[policy]
            if test_groups[policy].get("status") != "SUCCEEDED":
                raise RuntimeError(f"TEST paused for {policy}; the frozen TEST remains resumable")
            save_progress()
        bundle_path, bundle_sha = _make_test_bundle(
            formal, backend, package_root=package_root, identity=identity,
            seed_table_sha256=seed_table_sha256, test_open_sha256=test_open_sha,
            authorization_receipt_sha256=str(authorization["sha256"]),
        )
        progress["active_stage"] = "FROZEN-ANALYSIS"
        save_progress()
        analysis = formal.__dict__.get("formal_analysis")
        if analysis is None:
            import formal_analysis as analysis
        report = analysis.analyze_test_bundle(
            package_root=package_root, seed_table_path=package_root / "formal-seed-table.json",
            test_bundle_path=bundle_path, policy_model_digests=model_digests,
        )
        analysis_path, analysis_sha = analysis.write_analysis_once(package_root, report)
        clock.set_processes(set())
        active_wall = clock.close()
        final = {
            "schema_id": "issue85-formal-closeout-v1",
            "status": "SUCCEEDED",
            "claim": "registered source-locked formal study; interpretation remains limited to its pinned privileged native surrogate",
            "package_commit": identity["package_commit"],
            "source_lock_sha256": identity["source_lock_sha256"],
            "contract_sha256": formal.file_sha256(formal.CONTRACT_PATH),
            "seed_table_sha256": seed_table_sha256,
            "analysis_source_sha256": formal.file_sha256(formal.STUDY / "formal_analysis.py"),
            "authorization_receipt_sha256": authorization["sha256"],
            "predecessor_carry_manifest_sha256": authorization["predecessor_carry_manifest_sha256"],
            "predecessor_game_group_receipt_sha256": PREDECESSOR_GROUP_RECEIPT_SHA256,
            "owner_approval_comment_url": authorization["owner_approval_comment_url"],
            "reviewer_approval_comment_url": authorization["reviewer_approval_comment_url"],
            "pilot_report_sha256": authorization["pilot_report_sha256"],
            "whole_task_active_wall_cap_seconds": authorization["whole_task_active_wall_cap_seconds"],
            "cumulative_active_wall_seconds": active_wall,
            "training_games": int(formal.formal_plan()["training_games"]),
            "validation_games": int(formal.formal_plan()["validation_games"]),
            "test_games": int(formal.formal_plan()["test_games"]),
            "formal_games": int(formal.formal_plan()["formal_games"]),
            "training_blocks_per_arm": 8,
            "updates_per_block": 64,
            "minibatch_owners": 32,
            "actor_update_count": 40,
            "validation_used_for_model_selection": False,
            "test_open_receipt_sha256": test_open_sha,
            "test_receipt_bundle_sha256": bundle_sha,
            "analysis_path": analysis_path.relative_to(package_root).as_posix(),
            "analysis_sha256": analysis_sha,
            "analysis_primary_endpoint": report["primary_endpoint"],
            "analysis_confirmatory_family": report["confirmatory_family"]["contrasts"],
            "analysis_bootstrap_resamples": report["bootstrap_resamples"],
            "cache_equivalence_claim": "v10 remains limited to the retained 16-owner/two-update evidence scope",
            "finished_utc": _now(),
        }
        formal.pilot._write_or_verify_once(final_path, final)
        progress.update({"status": "SUCCEEDED", "active_stage": "COMPLETE", "cumulative_active_wall_seconds": active_wall, "finished_utc": final["finished_utc"]})
        _atomic_json(formal, progress_path, progress)
        return final
    except BaseException as exc:
        clock.set_processes(set())
        elapsed = clock.elapsed()
        clock.close()
        progress.update({
            "status": "FAILED_OR_PAUSED",
            "failure": {"type": type(exc).__name__, "detail": str(exc)[:3000]},
            "cumulative_active_wall_seconds": elapsed,
            "finished_or_paused_utc": _now(),
        })
        _atomic_json(formal, progress_path, progress)
        raise
