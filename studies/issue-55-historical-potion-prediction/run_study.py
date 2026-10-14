from __future__ import annotations

import copy
import gzip
import hashlib
import json
import math
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE / "artifacts"
PREREG_PATH = HERE / "preregistration.json"
AMENDMENT_PATH = HERE / "preregistration-amendment-001.json"
AMENDMENT_PATHS = [
    AMENDMENT_PATH,
    HERE / "preregistration-amendment-002.json",
    HERE / "preregistration-amendment-003.json",
]
FEATURE_DOMAIN = b"issue55-feature-v1\0"
BOOTSTRAP_DOMAIN = "issue55-owner-bootstrap-v1"
BOOTSTRAP_SEED = 550055
DIMENSION = 256
ALPHA = 1.0
ADMIN_CPU_UPPER_SECONDS = 25.0
BUILDER_CPU_LIMIT_SECONDS = 1900.0
ALL_IN_CPU_LIMIT_SECONDS = 2500.0
REVIEWER_CPU_RESERVE_SECONDS = 600.0
BOOTSTRAP_REPLICATES = 5000

SOURCE_ROOT = Path("/home/lsmft/.cache/issue45-potion-paired/reviewed-native-18bcd565")
EXPECTED_MANIFEST_SHA256 = "624b656dee2afe33cf0e0410dd4cdf1a198c456d3d050a3ac061cb1aec18c160"
EXPECTED_SOURCE_METADATA_SHA256 = "265a0a5d07083d4fe80fe26d6a802b250f3df1b5600b46689b351d2c1a4e5197"
EXPECTED_NATIVE_COMMIT = "18bcd565b587186425bb46ad3d70a2eb80d947fc"
EXPECTED_STSRL_COMMIT = "7d172f7c24c9d29429b6946ba44f6b59760b0d96"
EXPECTED_CONTROLLER_CONFIG_SHA256 = "2c5e2c2ef06c903d625d6ef7b84d3f0809ac34d8eb1023357d1af0b728817300"

POTION_LABELS = frozenset(
    {
        "INVALID_ID",
        "EMPTY_POTION_ID",
        "Ambrosia",
        "Ancient Potion",
        "AttackPotion",
        "BlessingOfTheForge",
        "Block Potion",
        "BloodPotion",
        "BottledMiracle",
        "ColorlessPotion",
        "CultistPotion",
        "CunningPotion",
        "Dexterity Potion",
        "DistilledChaos",
        "DuplicationPotion",
        "ElixirPotion",
        "Energy Potion",
        "EntropicBrew",
        "EssenceOfDarkness",
        "EssenceOfSteel",
        "Explosive Potion",
        "FairyPotion",
        "FearPotion",
        "Fire Potion",
        "SteroidPotion",
        "FocusPotion",
        "Fruit Juice",
        "GamblersBrew",
        "GhostInAJar",
        "HeartOfIron",
        "LiquidBronze",
        "LiquidMemories",
        "Poison Potion",
        "PotionOfCapacity",
        "PowerPotion",
        "Regen Potion",
        "SkillPotion",
        "SmokeBomb",
        "SneckoOil",
        "SpeedPotion",
        "StancePotion",
        "Strength Potion",
        "Swift Potion",
        "Weak Potion",
    }
)


class StudyFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class StopStudy(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ResourceBlocked(StudyFailure):
    def __init__(self, message: str):
        super().__init__("RESOURCE_BLOCKED", message)


_stage_name: str | None = None
_stage_start_cpu = 0.0
_stage_attempt: dict[str, Any] | None = None
_state: dict[str, Any] | None = None
_ledger: dict[str, Any] | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def write_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json_bytes(value)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)
    return hashlib.sha256(payload).hexdigest()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            digest.update(block)
            ensure_budget()
    return digest.hexdigest(), size


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _default_state() -> dict[str, Any]:
    return {
        "schema": "issue55-execution-state-v1",
        "disposition": "STUDY_ONLY",
        "terminal_classification": None,
        "stages": {},
        "updated_utc": utc_now(),
    }


def _default_ledger() -> dict[str, Any]:
    return {
        "schema": "issue55-cpu-ledger-v1",
        "unit": "process CPU seconds",
        "admin_cpu_upper_bound_seconds": ADMIN_CPU_UPPER_SECONDS,
        "stage_attempts": [],
        "interrupted_attempt_reserve_cpu_seconds": 0.0,
        "builder_plus_admin_limit_seconds": BUILDER_CPU_LIMIT_SECONDS,
        "all_in_limit_seconds": ALL_IN_CPU_LIMIT_SECONDS,
        "reviewer_reserved_min_seconds": REVIEWER_CPU_RESERVE_SECONDS,
    }


def load_state_and_ledger() -> None:
    global _state, _ledger
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    state_path = ARTIFACTS / "execution_state.json"
    ledger_path = ARTIFACTS / "cpu_ledger.json"
    _state = read_json(state_path) if state_path.exists() else _default_state()
    _ledger = read_json(ledger_path) if ledger_path.exists() else _default_ledger()

    # A RUNNING attempt left by a process crash remains visible and receives a
    # conservative 60 CPU-second reserve; its exact final process-time reading
    # cannot be recovered after the process has exited.
    for name, stage in _state.get("stages", {}).items():
        for attempt in stage.get("attempts", []):
            if attempt.get("status") == "RUNNING":
                attempt["status"] = "INTERRUPTED"
                attempt["ended_utc"] = utc_now()
                attempt["error"] = "Process exited before writing a terminal stage state."
                attempt["interrupted_attempt_reserve_cpu_seconds"] = 60.0
                _ledger["interrupted_attempt_reserve_cpu_seconds"] += 60.0
    write_json(state_path, _state)
    write_json(ledger_path, _ledger)


def _ledger_total(stage_extra: float = 0.0) -> float:
    assert _ledger is not None
    return (
        float(_ledger["admin_cpu_upper_bound_seconds"])
        + float(_ledger.get("interrupted_attempt_reserve_cpu_seconds", 0.0))
        + sum(float(row.get("cpu_seconds", 0.0)) for row in _ledger.get("stage_attempts", []))
        + stage_extra
    )


def ensure_budget() -> None:
    if _stage_name is None:
        return
    current = max(0.0, time.process_time() - _stage_start_cpu)
    if _ledger_total(current) > BUILDER_CPU_LIMIT_SECONDS:
        raise ResourceBlocked(
            f"Builder plus admin charged CPU would exceed {BUILDER_CPU_LIMIT_SECONDS:.1f}s; stopping before the next data operation."
        )


def _write_state() -> None:
    assert _state is not None
    _state["updated_utc"] = utc_now()
    write_json(ARTIFACTS / "execution_state.json", _state)


def _write_ledger() -> None:
    assert _ledger is not None
    total = _ledger_total()
    _ledger["builder_plus_admin_charged_seconds"] = total
    _ledger["all_in_remaining_after_reviewer_reserve_seconds"] = max(
        0.0, ALL_IN_CPU_LIMIT_SECONDS - REVIEWER_CPU_RESERVE_SECONDS - total
    )
    write_json(ARTIFACTS / "cpu_ledger.json", _ledger)


def start_stage(name: str) -> bool:
    global _stage_name, _stage_start_cpu, _stage_attempt
    assert _state is not None and _ledger is not None
    existing = _state.setdefault("stages", {}).get(name, {})
    if existing.get("status") == "COMPLETE":
        return False
    if existing.get("status") == "TERMINAL":
        attempts = existing.get("attempts", [])
        terminal_code = attempts[-1].get("terminal_classification") if attempts else None
        if terminal_code == "NO_IDENTIFIABLE_POTION_FEATURE_VARIATION":
            return False
        raise StudyFailure(
            "PUBLIC_FEATURE_SUBSET_UNAVAILABLE",
            "A prior source-admission failure is terminal; correct the cause and use the matching retry command.",
        )
    if existing.get("status") == "BLOCKED":
        raise ResourceBlocked(existing.get("message", "A prior stage blocked the study."))
    ensure_budget()
    _stage_name = name
    _stage_start_cpu = time.process_time()
    attempt = {
        "attempt": len(existing.get("attempts", [])) + 1,
        "status": "RUNNING",
        "started_utc": utc_now(),
        "cpu_seconds_observed": 0.0,
        "checkpoint": None,
    }
    existing.setdefault("attempts", []).append(attempt)
    existing["status"] = "RUNNING"
    existing["started_utc"] = attempt["started_utc"]
    existing.pop("ended_utc", None)
    existing.pop("message", None)
    _state["stages"][name] = existing
    _stage_attempt = attempt
    _write_state()
    _write_ledger()
    return True


def checkpoint_stage(checkpoint: Any = None) -> None:
    if _stage_attempt is None:
        return
    elapsed = max(0.0, time.process_time() - _stage_start_cpu)
    _stage_attempt["cpu_seconds_observed"] = elapsed
    if checkpoint is not None:
        _stage_attempt["checkpoint"] = checkpoint
    _write_state()
    ensure_budget()


def finish_stage(status: str, code: str | None = None, message: str | None = None) -> float:
    global _stage_name, _stage_attempt
    assert _state is not None and _ledger is not None and _stage_name is not None and _stage_attempt is not None
    elapsed = max(0.0, time.process_time() - _stage_start_cpu)
    _stage_attempt["cpu_seconds_observed"] = elapsed
    _stage_attempt["cpu_seconds"] = elapsed
    _stage_attempt["status"] = status
    _stage_attempt["ended_utc"] = utc_now()
    if code:
        _stage_attempt["terminal_classification"] = code
    if message:
        _stage_attempt["message"] = message
    stage = _state["stages"][_stage_name]
    stage["status"] = status
    stage["ended_utc"] = _stage_attempt["ended_utc"]
    if code:
        _state["terminal_classification"] = code
    if message:
        stage["message"] = message
    _ledger["stage_attempts"].append(
        {"stage": _stage_name, "attempt": _stage_attempt["attempt"], "cpu_seconds": elapsed, "status": status}
    )
    _write_state()
    _write_ledger()
    _stage_name = None
    _stage_attempt = None
    return elapsed


def fail_stage(code: str, message: str) -> None:
    status = "BLOCKED" if code == "RESOURCE_BLOCKED" else ("FAILED" if code == "BUILDER_REPAIR_REQUIRED" else "TERMINAL")
    finish_stage(status, None if code == "BUILDER_REPAIR_REQUIRED" else code, message)
    if _state is not None:
        _state["disposition"] = "STUDY_ONLY"
        if code == "BUILDER_REPAIR_REQUIRED":
            _state["execution_error"] = message
        _write_state()


def require(condition: bool, message: str, code: str = "PUBLIC_FEATURE_SUBSET_UNAVAILABLE") -> None:
    if not condition:
        raise StudyFailure(code, message)


def finite_number(value: Any, label: str) -> float:
    require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{label} is not numeric.")
    number = float(value)
    require(math.isfinite(number), f"{label} is not finite.")
    return number


def strict_int(value: Any, label: str) -> int:
    number = finite_number(value, label)
    require(number.is_integer(), f"{label} is not an integer.")
    return int(number)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{label} is not an object.")
    return value


def _visible_hp(snapshot: dict[str, Any], battle: bool) -> tuple[int, int]:
    if battle:
        player = _mapping(snapshot.get("battle_player"), "battle_player")
        current = strict_int(player.get("current_hp"), "battle_player.current_hp")
        maximum = strict_int(player.get("max_hp"), "battle_player.max_hp")
    else:
        current = strict_int(snapshot.get("cur_hp"), "cur_hp")
        maximum = strict_int(snapshot.get("max_hp"), "max_hp")
    require(maximum > 0 and 0 <= current <= maximum, "Public HP values are outside supported bounds.")
    return current, maximum


def _inventory(snapshot: dict[str, Any], battle: bool) -> tuple[int, int, list[str]]:
    count_key, capacity_key, slots_key = (
        ("battle_potion_count", "battle_potion_capacity", "battle_potions")
        if battle
        else ("potion_count", "potion_capacity", "potions")
    )
    count = strict_int(snapshot.get(count_key), count_key)
    capacity = strict_int(snapshot.get(capacity_key), capacity_key)
    slots = snapshot.get(slots_key)
    require(0 <= count <= capacity <= 16, "Potion count/capacity is outside supported bounds.")
    require(isinstance(slots, list) and len(slots) == capacity, f"{slots_key} does not match the declared capacity.")
    ordered: list[tuple[int, str]] = []
    for slot in slots:
        slot = _mapping(slot, "potion slot")
        index = strict_int(slot.get("potion_index"), "potion_index")
        label = slot.get("id_label")
        require(
            isinstance(label, str) and label in POTION_LABELS and label != "INVALID_ID",
            "Potion identity is unknown or invalid in the pinned Native enum.",
        )
        ordered.append((index, label))
    require(sorted(index for index, _ in ordered) == list(range(capacity)), "Potion slot indices are not a complete ordered inventory.")
    ordered.sort()
    labels = [label for _, label in ordered]
    require(sum(label != "EMPTY_POTION_ID" for label in labels) == count, "Potion count disagrees with the visible slot identities.")
    return count, capacity, labels


def _identity_multiset(value: Any, label: str) -> list[str]:
    require(isinstance(value, list), f"{label} is missing or not a list.")
    identities: list[str] = []
    for entry in value:
        entry = _mapping(entry, f"{label} item")
        identity = entry.get("id_label")
        require(isinstance(identity, str) and identity.strip(), f"{label} identity is missing.")
        require(identity not in {"UNKNOWN", "INVALID", "INVALID_ID"}, f"{label} identity is an unsupported Native sentinel.")
        require(all(ch.isprintable() for ch in identity), f"{label} identity contains non-printable characters.")
        identities.append(identity)
    return sorted(identities)


def feature_tokens(snapshot_value: Any) -> tuple[list[str], list[str], dict[str, Any]]:
    """Extract only the preregistered public fields from one raw snapshot."""
    snapshot = _mapping(snapshot_value, "snapshot_raw")
    battle = snapshot.get("battle_active")
    require(isinstance(battle, bool), "battle_active is not boolean.")
    act = strict_int(snapshot.get("act"), "act")
    floor = strict_int(snapshot.get("floor_num"), "floor_num")
    screen = snapshot.get("screen_state")
    require(isinstance(screen, str) and screen and screen != "(none)", "screen_state is missing or unsupported.")
    hp, max_hp = _visible_hp(snapshot, battle)
    potion_count, potion_capacity, potion_labels = _inventory(snapshot, battle)
    deck = _identity_multiset(snapshot.get("deck"), "deck")
    relics = _identity_multiset(snapshot.get("relics"), "relics")

    baseline = [
        "intercept=1",
        f"act={act}",
        f"current_floor={floor}",
        f"screen_family={screen}",
        f"current_hp={hp}",
        f"max_hp={max_hp}",
    ]
    baseline.extend(f"deck_identity={identity}" for identity in deck)
    baseline.extend(f"relic_identity={identity}" for identity in relics)
    potion = [f"potion_count={potion_count}", f"potion_capacity={potion_capacity}"]
    potion.extend(f"potion_slot_{index}={identity}" for index, identity in enumerate(potion_labels))
    context = {
        "act": act,
        "current_floor": floor,
        "screen_family": screen,
        "current_hp": hp,
        "max_hp": max_hp,
        "potion_present": potion_count > 0,
        "potion_count": potion_count,
        "potion_capacity": potion_capacity,
        "floor_band": "F00-F08" if floor <= 8 else ("F09-F16" if floor <= 16 else "F17+"),
        "battle_active": battle,
    }
    return baseline, potion, context


def negative_control_check(snapshot: dict[str, Any]) -> dict[str, Any]:
    baseline, potion, _ = feature_tokens(snapshot)
    changed = copy.deepcopy(snapshot)
    changed.update(
        {
            "outcome": "INJECTED_PRIVATE_OUTCOME",
            "battle_outcome": "INJECTED_PRIVATE_BATTLE_OUTCOME",
            "gold": -999999,
            "ascension": -999999,
            "room_type": "INJECTED_PRIVATE_ROOM",
            "blue_key": True,
            "green_key": True,
            "red_key": True,
            "battle_turn": -999999,
            "battle_hand": [{"id_label": "INJECTED_PRIVATE_HAND"}],
            "battle_monsters": [{"intent_category": "INJECTED_PRIVATE_INTENT"}],
            "next_snapshot_raw": {"current_hp": -999999},
            "chosen_action_identity": "INJECTED_PRIVATE_ACTION",
            "seed": -999999,
            "controller_role": "INJECTED_PRIVATE_CONTROLLER",
            "decision_metadata": {"search": "INJECTED_PRIVATE_SEARCH"},
            "native_packed_state": "INJECTED_PRIVATE_NATIVE_STATE",
        }
    )
    if isinstance(changed.get("battle_player"), dict):
        changed["battle_player"].update(
            {
                "hand": [{"id_label": "INJECTED_PRIVATE_HAND"}],
                "energy": -999999,
                "block": -999999,
                "draw_pile": ["INJECTED_PRIVATE_CARD"],
            }
        )
    changed_baseline, changed_potion, _ = feature_tokens(changed)
    require((baseline, potion) == (changed_baseline, changed_potion), "Private-field negative control changed feature tokens.")
    return {
        "status": "PASS",
        "fields_injected": [
            "outcome", "battle_outcome", "gold", "ascension", "room_type", "key flags",
            "battle turn/hand/monsters", "next_snapshot_raw", "chosen action", "seed",
            "controller/search metadata", "native packed state", "battle hand/energy/block/draw pile",
        ],
        "baseline_feature_tokens_unchanged": True,
        "potion_feature_tokens_unchanged": True,
    }


def validate_redundant_row_fields(
    row: dict[str, Any], snapshot: dict[str, Any], context: dict[str, Any]
) -> dict[str, bool]:
    require(isinstance(row.get("legal_action_identities"), list) and bool(row["legal_action_identities"]), "Decision record has no legal actions.")
    require(isinstance(row.get("eligible_action_indices"), list) and bool(row["eligible_action_indices"]), "Decision record has no eligible action indices.")
    require(strict_int(row.get("floor"), "row.floor") == context["current_floor"], "Co-recorded current floor disagrees with snapshot.")
    row_player_hp = strict_int(row.get("player_hp"), "row.player_hp")
    battle_hp_mismatch = False
    battle_max_hp_mismatch = False
    if context["battle_active"]:
        game_hp = snapshot.get("cur_hp")
        require(game_hp is not None, "Battle snapshot is missing the raw GameContext cur_hp summary.")
        require(row_player_hp == strict_int(game_hp, "snapshot_raw.cur_hp"), "Co-recorded legacy current HP disagrees with GameContext cur_hp.")
        battle_hp_mismatch = strict_int(game_hp, "snapshot_raw.cur_hp") != context["current_hp"]
        battle_hp_alias = snapshot.get("battle_player_hp")
        require(battle_hp_alias is not None, "Battle snapshot is missing the duplicate BattleContext HP alias.")
        require(strict_int(battle_hp_alias, "snapshot_raw.battle_player_hp") == context["current_hp"], "BattleContext HP aliases disagree.")
        game_max_hp = snapshot.get("max_hp")
        require(game_max_hp is not None, "Battle snapshot is missing the raw GameContext max_hp summary.")
        battle_max_hp_mismatch = strict_int(game_max_hp, "snapshot_raw.max_hp") != context["max_hp"]
        max_hp_available = row.get("player_max_hp") is not None
        if max_hp_available:
            expected_row_max = game_max_hp if game_max_hp is not None else context["max_hp"]
            require(strict_int(row["player_max_hp"], "row.player_max_hp") == strict_int(expected_row_max, "expected legacy max_hp"), "Co-recorded legacy max HP disagrees with its source.")
    else:
        require(row_player_hp == context["current_hp"], "Co-recorded current HP disagrees with public projection source.")
        max_hp_available = row.get("player_max_hp") is not None
        if max_hp_available:
            require(strict_int(row["player_max_hp"], "row.player_max_hp") == context["max_hp"], "Co-recorded max HP disagrees with public projection source.")
    require(strict_int(row.get("potion_count"), "row.potion_count") == context["potion_count"], "Co-recorded potion count disagrees with public inventory.")
    require(row.get("screen_state") == context["screen_family"], "Co-recorded screen identity disagrees with snapshot.")
    require(row.get("battle_active") is context["battle_active"], "Co-recorded battle-active flag disagrees with snapshot.")
    return {
        "max_hp_redundant_row_available": max_hp_available,
        "battle_game_context_hp_differs_from_public_battle_context": battle_hp_mismatch,
        "battle_game_context_max_hp_differs_from_public_battle_context": battle_max_hp_mismatch,
    }


def manifest_index() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    manifest_path = SOURCE_ROOT / "manifest.json"
    source_path = SOURCE_ROOT / "source.json"
    manifest_hash, manifest_size = sha256_file(manifest_path)
    source_hash, source_size = sha256_file(source_path)
    require(manifest_hash == EXPECTED_MANIFEST_SHA256, "The source manifest hash does not match the reviewed #45 seal.")
    require(source_hash == EXPECTED_SOURCE_METADATA_SHA256, "The source metadata hash does not match the #54 receipt.")
    entries = read_json(manifest_path)
    require(isinstance(entries, list), "The reviewed source manifest has an unsupported shape.")
    indexed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        entry = _mapping(entry, "manifest entry")
        path = Path(str(entry.get("path", "")))
        basename = path.name
        require(basename not in indexed, f"Duplicate manifest entry for {basename}.")
        require(path == SOURCE_ROOT / basename, f"Manifest path root mismatch for {basename}.")
        indexed[basename] = entry
    source = read_json(source_path)
    require(source.get("native_commit") == EXPECTED_NATIVE_COMMIT, "Reviewed source Native commit mismatch.")
    require(source.get("stsrl_commit") == EXPECTED_STSRL_COMMIT, "Reviewed source STSRL commit mismatch.")
    return indexed, {
        "manifest_sha256": manifest_hash,
        "manifest_bytes": manifest_size,
        "source_metadata_sha256": source_hash,
        "source_metadata_bytes": source_size,
        "native_commit": source.get("native_commit"),
        "stsrl_commit": source.get("stsrl_commit"),
        "controller_config_fingerprint": EXPECTED_CONTROLLER_CONFIG_SHA256,
    }


def verify_manifested_file(index: dict[str, dict[str, Any]], basename: str) -> dict[str, Any]:
    ensure_budget()
    path = SOURCE_ROOT / basename
    entry = index.get(basename)
    require(entry is not None, f"{basename} is absent from the reviewed source manifest.")
    digest, size = sha256_file(path)
    require(size == int(entry["bytes"]), f"Size mismatch for {basename}.")
    require(digest == str(entry["sha256"]), f"SHA-256 mismatch for {basename}.")
    return {"basename": basename, "bytes": size, "sha256": digest}


def expected_partitions(prereg: dict[str, Any]) -> dict[str, list[int]]:
    partitions = prereg["split"]["partitions"]
    require(len(partitions["train"]) == 64, "Training split is not exactly 64 owners.")
    require(len(partitions["validation"]) == 16, "Validation split is not exactly 16 owners.")
    require(len(partitions["heldout"]) == 16, "Heldout split is not exactly 16 owners.")
    all_ids = partitions["train"] + partitions["validation"] + partitions["heldout"]
    require(len(set(all_ids)) == 96, "The three owner partitions are not disjoint and complete.")
    require(set(all_ids) == set(range(437001, 437097)), "Owner split does not match the assigned #45 on-arm owner set.")
    domain = bytes.fromhex(prereg["split"]["domain_hex"])
    ranked = sorted(
        (hashlib.sha256(domain + str(owner).encode("ascii")).hexdigest(), owner)
        for owner in range(437001, 437097)
    )
    expected = {
        "train": [owner for _, owner in ranked[:64]],
        "validation": [owner for _, owner in ranked[64:80]],
        "heldout": [owner for _, owner in ranked[80:]],
    }
    require(partitions == expected, "Published owner splits do not match the frozen split algorithm.")
    published_digests = prereg["split"].get("ranked_owner_digests", [])
    require(
        published_digests == [{"owner_id": owner, "sha256": digest} for digest, owner in ranked],
        "Published owner split digests do not match the frozen split algorithm.",
    )
    return partitions


def owner_metadata(owner: int) -> dict[str, Any]:
    path = SOURCE_ROOT / f"{owner}-on.json"
    meta = read_json(path)
    require(meta.get("seed") == owner, f"Metadata owner key mismatch for {owner}.")
    require(meta.get("arm") == "on", f"Unexpected arm for owner {owner}.")
    require(meta.get("valid") is True and meta.get("terminal") is True, f"Owner {owner} is not a valid terminal whole-run record.")
    require(meta.get("source", {}).get("native_commit") == EXPECTED_NATIVE_COMMIT, f"Native lineage mismatch for owner {owner}.")
    require(meta.get("source", {}).get("stsrl_commit") == EXPECTED_STSRL_COMMIT, f"STSRL lineage mismatch for owner {owner}.")
    config_hash = canonical_hash(meta.get("controller", {}).get("config"))
    require(config_hash == EXPECTED_CONTROLLER_CONFIG_SHA256, f"Controller config fingerprint mismatch for owner {owner}.")
    initial_raw = _mapping(meta.get("initial_raw"), f"owner {owner} initial_raw")
    require(strict_int(initial_raw.get("ascension"), f"owner {owner} initial ascension") == 20, f"Owner {owner} is not an A20 start.")
    require(strict_int(initial_raw.get("floor_num"), f"owner {owner} initial floor") == 0, f"Owner {owner} is not a standard starting floor.")
    terminal_floor = strict_int(meta.get("floor"), f"owner {owner} terminal floor")
    final_raw = _mapping(meta.get("final_raw"), f"owner {owner} final_raw")
    require(strict_int(final_raw.get("floor_num"), f"owner {owner} final floor_num") == terminal_floor, f"Terminal floor sources disagree for owner {owner}.")
    return {"terminal_floor": terminal_floor, "controller_config_fingerprint": config_hash}


def write_safe_gzip(path: Path, records: list[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as output:
            for record in records:
                output.write(json_bytes(record))
    os.replace(temporary, path)
    return sha256_file(path)[0]


def iter_gzip_jsonl(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def prepare() -> None:
    prereg = read_json(PREREG_PATH)
    amendments = [read_json(path) for path in AMENDMENT_PATHS]
    partitions = expected_partitions(prereg)
    prereg_hash, _ = sha256_file(PREREG_PATH)
    amendment_hashes = {path.name: sha256_file(path)[0] for path in AMENDMENT_PATHS}
    index, source_seal = manifest_index()
    train_records_path = ARTIFACTS / "train_public_features.jsonl.gz"
    temporary = train_records_path.with_name(train_records_path.name + ".tmp")
    temporary.unlink(missing_ok=True)

    owner_rows: dict[str, dict[str, Any]] = {}
    verified_inputs: list[dict[str, Any]] = []
    support_screens: Counter[str] = Counter()
    support_potion_states: Counter[str] = Counter()
    total_rows = 0
    checked_adjacencies = 0
    max_hp_redundant_rows = 0
    battle_hp_summary_mismatches = 0
    battle_max_hp_summary_mismatches = 0
    control: dict[str, Any] | None = None

    with temporary.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as output:
            for owner in partitions["train"]:
                ensure_budget()
                metadata_record = verify_manifested_file(index, f"{owner}-on.json")
                trace_record = verify_manifested_file(index, f"{owner}-on.jsonl")
                verified_inputs.extend([metadata_record, trace_record])
                owner_meta = owner_metadata(owner)
                terminal_floor = owner_meta["terminal_floor"]
                row_count = 0
                potion_present_rows = 0
                previous_next_tokens: tuple[list[str], list[str]] | None = None
                with (SOURCE_ROOT / f"{owner}-on.jsonl").open("r", encoding="utf-8") as rows_file:
                    for line_number, line in enumerate(rows_file, start=1):
                        ensure_budget()
                        try:
                            row = _mapping(json.loads(line), f"owner {owner} row {line_number}")
                            snapshot = _mapping(row.get("snapshot_raw"), f"owner {owner} row {line_number} snapshot_raw")
                            baseline, potion, context = feature_tokens(snapshot)
                            redundancy = validate_redundant_row_fields(row, snapshot, context)
                            max_hp_redundant_rows += int(redundancy["max_hp_redundant_row_available"])
                            battle_hp_summary_mismatches += int(redundancy["battle_game_context_hp_differs_from_public_battle_context"])
                            battle_max_hp_summary_mismatches += int(redundancy["battle_game_context_max_hp_differs_from_public_battle_context"])
                        except StudyFailure as exc:
                            raise StudyFailure(exc.code, f"owner {owner} row {line_number}: {exc}") from exc
                        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                            raise StudyFailure("PUBLIC_FEATURE_SUBSET_UNAVAILABLE", f"owner {owner} row {line_number}: malformed JSONL record") from exc
                        if previous_next_tokens is not None:
                            require(previous_next_tokens == (baseline, potion), f"Adjacent pre/post public state mismatch for owner {owner}, row {line_number}.")
                            checked_adjacencies += 1
                        if control is None:
                            control = negative_control_check(snapshot)
                        record = {
                            "owner_id": owner,
                            "source_row_number": line_number,
                            "target_remaining_floors": terminal_floor - context["current_floor"],
                            "terminal_floor": terminal_floor,
                            "current_floor": context["current_floor"],
                            "screen_family": context["screen_family"],
                            "floor_band": context["floor_band"],
                            "potion_present": context["potion_present"],
                            "current_hp": context["current_hp"],
                            "max_hp": context["max_hp"],
                            "baseline_tokens": baseline,
                            "potion_tokens": potion,
                        }
                        require(record["target_remaining_floors"] >= 0, f"Current floor exceeds terminal floor for owner {owner}.")
                        output.write(json_bytes(record))
                        next_snapshot = row.get("next_snapshot_raw")
                        if next_snapshot is None:
                            previous_next_tokens = None
                        else:
                            next_baseline, next_potion, _ = feature_tokens(next_snapshot)
                            previous_next_tokens = (next_baseline, next_potion)
                        row_count += 1
                        total_rows += 1
                        potion_present_rows += int(context["potion_present"])
                        support_screens[context["screen_family"]] += 1
                        support_potion_states["present" if context["potion_present"] else "absent"] += 1
                require(row_count > 0, f"Training owner {owner} has no eligible decision rows.")
                owner_rows[str(owner)] = {
                    "eligible_rows": row_count,
                    "potion_present_rows": potion_present_rows,
                    "terminal_floor": terminal_floor,
                }
                checkpoint_stage({"train_owners_complete": len(owner_rows), "train_rows_complete": total_rows})
    os.replace(temporary, train_records_path)
    require(control is not None, "No eligible training feature rows were found.")
    potion_state_count = len(support_potion_states)
    inventory_states: set[tuple[str, ...]] = set()
    for record in iter_gzip_jsonl(train_records_path):
        inventory_states.add(tuple(record["potion_tokens"]))
        ensure_budget()
        if len(inventory_states) > 1:
            break
    train_sha = sha256_file(train_records_path)[0]
    report = {
        "schema": "issue55-admission-report-v1",
        "disposition": "STUDY_ONLY",
        "source_seal": source_seal,
        "preregistration_sha256": prereg_hash,
        "preregistration_amendment_sha256s": amendment_hashes,
        "partition_counts": {name: len(ids) for name, ids in partitions.items()},
        "opened_partitions": ["train"],
        "validation_opened": False,
        "heldout_opened": False,
        "training_input_hashes": sorted(verified_inputs, key=lambda item: item["basename"]),
        "training_public_features": {"path": train_records_path.name, "sha256": train_sha, "bytes": train_records_path.stat().st_size},
        "training_owner_rows": owner_rows,
        "training_owner_count": len(owner_rows),
        "eligible_training_rows": total_rows,
        "training_screen_row_counts": dict(sorted(support_screens.items())),
        "training_potion_presence_row_counts": dict(sorted(support_potion_states.items())),
        "training_inventory_variation_count_at_least": 2 if len(inventory_states) > 1 else len(inventory_states),
        "negative_control": control,
        "action_time_checks": {
            "redundant_current_floor_potion_count_screen_and_battle_flag": "PASS",
            "co_recorded_max_hp_rows": max_hp_redundant_rows,
            "max_hp_source_when_not_co_recorded": "allowlisted raw current state field checked against the pinned public projection mapping",
            "battle_game_context_hp_vs_public_battle_context_mismatch_rows": battle_hp_summary_mismatches,
            "battle_game_context_max_hp_vs_public_battle_context_mismatch_rows": battle_max_hp_summary_mismatches,
            "adjacent_previous_next_to_current_public_allowlist_tokens": "PASS",
            "adjacent_transitions_checked": checked_adjacencies,
            "legal_action_set_nonempty_each_included_row": "PASS",
        },
        "validation_used": False,
        "limitations": [
            "Only explicitly allowlisted projection-backed fields are used; this does not establish full observation closure.",
            "Raw Native snapshots include excluded simulator and battle fields; they are not copied to study outputs.",
            "Historical prediction is observational and is not a potion-use counterfactual or current-policy value estimate.",
        ],
    }
    write_json(ARTIFACTS / "admission_report.json", report)
    require(len(owner_rows) == 64, "Some assigned training owners were not included.")
    require(potion_state_count >= 2 and len(inventory_states) >= 2, "Training split has no identifiable potion-information variation.", "NO_IDENTIFIABLE_POTION_FEATURE_VARIATION")
    checkpoint_stage({"train_owners_complete": len(owner_rows), "train_rows_complete": total_rows, "admission_report_written": True})


def feature_vector(tokens: list[str]) -> dict[int, float]:
    vector: defaultdict[int, float] = defaultdict(float)
    for token in tokens:
        digest = hashlib.sha256(FEATURE_DOMAIN + token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % DIMENSION
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vector[bucket] += sign
    return {bucket: value for bucket, value in vector.items() if value != 0.0}


def dot(coefficients: list[float], vector: dict[int, float]) -> float:
    return sum(coefficients[index] * value for index, value in vector.items())


def cholesky_solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    size = len(rhs)
    lower = [[0.0] * size for _ in range(size)]
    for i in range(size):
        if i % 32 == 0:
            ensure_budget()
        for j in range(i + 1):
            total = matrix[i][j] - sum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                require(total > 0.0 and math.isfinite(total), "Ridge normal matrix is not positive definite.")
                lower[i][j] = math.sqrt(total)
            else:
                lower[i][j] = total / lower[j][j]
    forward = [0.0] * size
    for i in range(size):
        forward[i] = (rhs[i] - sum(lower[i][k] * forward[k] for k in range(i))) / lower[i][i]
    result = [0.0] * size
    for i in range(size - 1, -1, -1):
        result[i] = (forward[i] - sum(lower[k][i] * result[k] for k in range(i + 1, size))) / lower[i][i]
    require(all(math.isfinite(value) for value in result), "Ridge coefficients contain a non-finite value.")
    return result


def fit_model(training_path: Path, row_counts: dict[str, int], visible: bool) -> dict[str, Any]:
    matrix = [[0.0] * DIMENSION for _ in range(DIMENSION)]
    rhs = [0.0] * DIMENSION
    row_count = 0
    for record in iter_gzip_jsonl(training_path):
        ensure_budget()
        owner = str(record["owner_id"])
        tokens = record["baseline_tokens"] + (record["potion_tokens"] if visible else [])
        vector = feature_vector(tokens)
        target = float(record["target_remaining_floors"])
        weight = 1.0 / int(row_counts[owner])
        for i, value in vector.items():
            rhs[i] += weight * value * target
        entries = sorted(vector.items())
        for position, (i, value_i) in enumerate(entries):
            row = matrix[i]
            for j, value_j in entries[position:]:
                product = weight * value_i * value_j
                row[j] += product
                if j != i:
                    matrix[j][i] += product
        row_count += 1
    require(row_count > 0, "No training rows were available to fit the model.")
    for index in range(DIMENSION):
        matrix[index][index] += ALPHA
    coefficients = cholesky_solve(matrix, rhs)

    residuals: list[float] = []
    owner_residuals: defaultdict[str, list[float]] = defaultdict(list)
    for record in iter_gzip_jsonl(training_path):
        ensure_budget()
        tokens = record["baseline_tokens"] + (record["potion_tokens"] if visible else [])
        prediction = dot(coefficients, feature_vector(tokens))
        residual = abs(float(record["target_remaining_floors"]) - prediction)
        residuals.append(residual)
        owner_residuals[str(record["owner_id"])].append(residual)
    residuals.sort()
    q90_index = max(0, math.ceil(0.9 * len(residuals)) - 1)
    return {
        "schema": "issue55-ridge-model-v1",
        "name": "POTION_VISIBLE" if visible else "POTION_MASKED",
        "disposition": "STUDY_ONLY",
        "feature_dimension": DIMENSION,
        "feature_domain": FEATURE_DOMAIN.decode("utf-8", errors="replace"),
        "alpha": ALPHA,
        "owner_equal_row_weight": "1 / eligible decision rows for that training owner",
        "training_owner_count": len(row_counts),
        "training_rows": row_count,
        "coefficient_fit": "float64 normal equations and Cholesky; all 256 weights including intercept regularized",
        "training_input_sha256": sha256_file(training_path)[0],
        "training_residual_absolute_q90_nearest_rank": residuals[q90_index],
        "training_residual_count": len(residuals),
        "training_owner_residual_mae": {owner: statistics.fmean(values) for owner, values in sorted(owner_residuals.items())},
        "coefficients": coefficients,
    }


def fit() -> None:
    admission = read_json(ARTIFACTS / "admission_report.json")
    require(admission.get("validation_opened") is False and admission.get("heldout_opened") is False, "Training admission report says validation or heldout was opened.")
    training_path = ARTIFACTS / admission["training_public_features"]["path"]
    training_sha, _ = sha256_file(training_path)
    require(training_sha == admission["training_public_features"]["sha256"], "Training feature artifact hash changed after admission.")
    row_counts = {str(owner): int(row["eligible_rows"]) for owner, row in admission["training_owner_rows"].items()}
    model_records = [fit_model(training_path, row_counts, visible=True), fit_model(training_path, row_counts, visible=False)]
    names = ["model_visible.json", "model_masked.json"]
    weight_hashes = {}
    for record, name in zip(model_records, names):
        weight_hashes[name] = write_json(ARTIFACTS / name, record)
    freeze = {
        "schema": "issue55-model-freeze-v1",
        "status": "FROZEN_BEFORE_HELDOUT_ACCESS",
        "frozen_utc": utc_now(),
        "disposition": "STUDY_ONLY",
        "preregistration_sha256": sha256_file(PREREG_PATH)[0],
        "preregistration_amendment_sha256s": {path.name: sha256_file(path)[0] for path in AMENDMENT_PATHS},
        "admission_report_sha256": sha256_file(ARTIFACTS / "admission_report.json")[0],
        "training_feature_sha256": training_sha,
        "models": {
            name: {
                "sha256": weight_hashes[name],
                "model_name": "POTION_VISIBLE" if name == "model_visible.json" else "POTION_MASKED",
            }
            for name in names
        },
        "validation_used": False,
        "heldout_opened": False,
    }
    write_json(ARTIFACTS / "freeze_receipt.json", freeze)
    checkpoint_stage({"both_models_written": True, "freeze_receipt_written": True})


def deterministic_owner_index(stratum_key: str, replicate: int, draw_index: int, owner_count: int) -> int:
    payload = f"{BOOTSTRAP_DOMAIN}\0{BOOTSTRAP_SEED}\0{stratum_key}\0{replicate}\0{draw_index}".encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big") % owner_count


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def bootstrap_ci(owner_deltas: dict[str, float], stratum_key: str) -> dict[str, Any]:
    owners = sorted(owner_deltas)
    values = [owner_deltas[owner] for owner in owners]
    require(len(values) >= 2, "Bootstrap interval requested with fewer than two owners.")
    samples: list[float] = []
    for replicate in range(BOOTSTRAP_REPLICATES):
        if replicate % 250 == 0:
            ensure_budget()
        total = 0.0
        for draw in range(len(owners)):
            total += values[deterministic_owner_index(stratum_key, replicate, draw, len(owners))]
        samples.append(total / len(owners))
    return {
        "method": "owner-cluster bootstrap with replacement; SHA-256 counter stream; linear-interpolated percentile interval",
        "seed": BOOTSTRAP_SEED,
        "replicates": BOOTSTRAP_REPLICATES,
        "owner_count": len(owners),
        "point_estimate": statistics.fmean(values),
        "ci_95": [percentile(samples, 0.025), percentile(samples, 0.975)],
    }


def _model_prediction(model: dict[str, Any], record: dict[str, Any], visible: bool) -> float:
    tokens = record["baseline_tokens"] + (record["potion_tokens"] if visible else [])
    return dot(model["coefficients"], feature_vector(tokens))


def _calibration(owner_rows: dict[str, list[dict[str, Any]]], pred_key: str) -> dict[str, Any]:
    pairs = []
    for owner, rows in sorted(owner_rows.items()):
        pairs.append((statistics.fmean(row[pred_key] for row in rows), statistics.fmean(row["target_remaining_floors"] for row in rows)))
    mean_prediction = statistics.fmean(x for x, _ in pairs)
    mean_actual = statistics.fmean(y for _, y in pairs)
    variance = sum((x - mean_prediction) ** 2 for x, _ in pairs)
    covariance = sum((x - mean_prediction) * (y - mean_actual) for x, y in pairs)
    slope = covariance / variance if variance > 0.0 else None
    intercept = mean_actual - slope * mean_prediction if slope is not None else None
    return {
        "owner_equal_mean_prediction": mean_prediction,
        "owner_equal_mean_actual": mean_actual,
        "owner_equal_bias_prediction_minus_actual": mean_prediction - mean_actual,
        "slope_actual_on_prediction": slope,
        "intercept_actual_on_prediction": intercept,
        "owner_count": len(pairs),
    }


def _metrics_for_rows(
    owner_rows: dict[str, list[dict[str, Any]]], *, include_bootstrap: bool = True
) -> dict[str, Any]:
    owner_errors: dict[str, dict[str, float]] = {}
    owner_deltas: dict[str, float] = {}
    for owner, rows in sorted(owner_rows.items()):
        visible_abs = [abs(row["target_remaining_floors"] - row["prediction_visible"]) for row in rows]
        masked_abs = [abs(row["target_remaining_floors"] - row["prediction_masked"]) for row in rows]
        visible_sq = [(row["target_remaining_floors"] - row["prediction_visible"]) ** 2 for row in rows]
        masked_sq = [(row["target_remaining_floors"] - row["prediction_masked"]) ** 2 for row in rows]
        floor_only_abs = [abs(row["target_remaining_floors"] - row["prediction_floor_only"]) for row in rows]
        floor_only_sq = [(row["target_remaining_floors"] - row["prediction_floor_only"]) ** 2 for row in rows]
        floor_hp_abs = [abs(row["target_remaining_floors"] - row["prediction_floor_hp"]) for row in rows]
        floor_hp_sq = [(row["target_remaining_floors"] - row["prediction_floor_hp"]) ** 2 for row in rows]
        error = {
            "rows": len(rows),
            "mae_visible": statistics.fmean(visible_abs),
            "mse_visible": statistics.fmean(visible_sq),
            "mae_masked": statistics.fmean(masked_abs),
            "mse_masked": statistics.fmean(masked_sq),
            "mae_floor_only": statistics.fmean(floor_only_abs),
            "mse_floor_only": statistics.fmean(floor_only_sq),
            "mae_floor_hp": statistics.fmean(floor_hp_abs),
            "mse_floor_hp": statistics.fmean(floor_hp_sq),
            "paired_delta_masked_minus_visible": statistics.fmean(masked_abs) - statistics.fmean(visible_abs),
        }
        owner_errors[owner] = error
        owner_deltas[owner] = error["paired_delta_masked_minus_visible"]

    def mean(key: str) -> float:
        return statistics.fmean(row[key] for row in owner_errors.values())

    result = {
        "owner_equal_metrics": {
            "POTION_VISIBLE": {"mae": mean("mae_visible"), "mse": mean("mse_visible")},
            "POTION_MASKED": {"mae": mean("mae_masked"), "mse": mean("mse_masked")},
            "FLOOR_ONLY": {"mae": mean("mae_floor_only"), "mse": mean("mse_floor_only")},
            "FLOOR_PLUS_HP_HEURISTIC": {"mae": mean("mae_floor_hp"), "mse": mean("mse_floor_hp")},
            "paired_owner_mean_absolute_error_masked_minus_visible": statistics.fmean(owner_deltas.values()),
        },
        "owner_errors": owner_errors,
        "owner_count": len(owner_rows),
        "decision_row_count": sum(len(rows) for rows in owner_rows.values()),
    }
    if include_bootstrap:
        result["primary_paired_owner_cluster_bootstrap"] = bootstrap_ci(owner_deltas, "overall")
    return result


def evaluate() -> None:
    freeze_path = ARTIFACTS / "freeze_receipt.json"
    require(freeze_path.exists(), "Heldout cannot be opened before a freeze receipt exists.")
    freeze = read_json(freeze_path)
    require(freeze.get("status") == "FROZEN_BEFORE_HELDOUT_ACCESS", "Models were not frozen before heldout access.")
    require(freeze.get("heldout_opened") is False, "Freeze receipt indicates heldout access before freezing.")
    for name, record in freeze["models"].items():
        path = ARTIFACTS / name
        require(path.exists() and sha256_file(path)[0] == record["sha256"], f"Frozen model hash mismatch: {name}.")
    visible = read_json(ARTIFACTS / "model_visible.json")
    masked = read_json(ARTIFACTS / "model_masked.json")
    admission = read_json(ARTIFACTS / "admission_report.json")
    prereg = read_json(PREREG_PATH)
    partitions = expected_partitions(prereg)
    index, _ = manifest_index()
    train_floors = [int(row["terminal_floor"]) for row in admission["training_owner_rows"].values()]
    terminal_floor_prior = statistics.fmean(train_floors)
    heldout_owner_rows: dict[str, list[dict[str, Any]]] = {}
    heldout_counts: dict[str, Any] = {}
    heldout_inputs: list[dict[str, Any]] = []
    safe_records: list[dict[str, Any]] = []
    heldout_battle_hp_summary_mismatches = 0
    heldout_battle_max_hp_summary_mismatches = 0

    for owner in partitions["heldout"]:
        ensure_budget()
        heldout_inputs.append(verify_manifested_file(index, f"{owner}-on.json"))
        heldout_inputs.append(verify_manifested_file(index, f"{owner}-on.jsonl"))
        owner_meta = owner_metadata(owner)
        terminal_floor = owner_meta["terminal_floor"]
        rows_for_owner: list[dict[str, Any]] = []
        with (SOURCE_ROOT / f"{owner}-on.jsonl").open("r", encoding="utf-8") as rows_file:
            for line_number, line in enumerate(rows_file, start=1):
                ensure_budget()
                try:
                    row = _mapping(json.loads(line), f"heldout owner {owner} row {line_number}")
                    snapshot = _mapping(row.get("snapshot_raw"), f"heldout owner {owner} row {line_number} snapshot_raw")
                    baseline, potion, context = feature_tokens(snapshot)
                    redundancy = validate_redundant_row_fields(row, snapshot, context)
                    heldout_battle_hp_summary_mismatches += int(redundancy["battle_game_context_hp_differs_from_public_battle_context"])
                    heldout_battle_max_hp_summary_mismatches += int(redundancy["battle_game_context_max_hp_differs_from_public_battle_context"])
                except StudyFailure as exc:
                    raise StudyFailure(exc.code, f"heldout owner {owner} row {line_number}: {exc}") from exc
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise StudyFailure("PUBLIC_FEATURE_SUBSET_UNAVAILABLE", f"heldout owner {owner} row {line_number}: malformed JSONL record") from exc
                record = {
                    "owner_id": owner,
                    "source_row_number": line_number,
                    "target_remaining_floors": terminal_floor - context["current_floor"],
                    "terminal_floor": terminal_floor,
                    "current_floor": context["current_floor"],
                    "screen_family": context["screen_family"],
                    "floor_band": context["floor_band"],
                    "potion_present": context["potion_present"],
                    "current_hp": context["current_hp"],
                    "max_hp": context["max_hp"],
                    "baseline_tokens": baseline,
                    "potion_tokens": potion,
                }
                require(record["target_remaining_floors"] >= 0, f"Current floor exceeds terminal floor for heldout owner {owner}.")
                rows_for_owner.append(record)
                safe_records.append(record)
        require(rows_for_owner, f"Heldout owner {owner} has no eligible decision rows.")
        heldout_owner_rows[str(owner)] = rows_for_owner
        heldout_counts[str(owner)] = {
            "eligible_rows": len(rows_for_owner),
            "potion_present_rows": sum(int(row["potion_present"]) for row in rows_for_owner),
            "terminal_floor": terminal_floor,
        }
        checkpoint_stage({"heldout_owners_complete": len(heldout_owner_rows), "heldout_rows_complete": sum(len(rows) for rows in heldout_owner_rows.values())})

    inventories = {tuple(record["potion_tokens"]) for record in safe_records}
    require(len(inventories) > 1, "Heldout split has no identifiable potion-information variation.", "NO_IDENTIFIABLE_POTION_FEATURE_VARIATION")

    heldout_path = ARTIFACTS / "heldout_public_features.jsonl.gz"
    heldout_sha = write_safe_gzip(heldout_path, safe_records)
    for owner, rows in heldout_owner_rows.items():
        for row in rows:
            row["prediction_visible"] = _model_prediction(visible, row, visible=True)
            row["prediction_masked"] = _model_prediction(masked, row, visible=False)
            row["prediction_floor_only"] = terminal_floor_prior - row["current_floor"]
            row["prediction_floor_hp"] = row["prediction_floor_only"] * row["current_hp"] / row["max_hp"]
    overall = _metrics_for_rows(heldout_owner_rows)
    training_rows = list(iter_gzip_jsonl(ARTIFACTS / admission["training_public_features"]["path"]))
    training_owner_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in training_rows:
        row["prediction_visible"] = _model_prediction(visible, row, visible=True)
        row["prediction_masked"] = _model_prediction(masked, row, visible=False)
        target = float(row["target_remaining_floors"])
        training_owner_rows[str(row["owner_id"])].append(
            {
                "target_remaining_floors": target,
                "abs_visible": abs(target - row["prediction_visible"]),
                "abs_masked": abs(target - row["prediction_masked"]),
            }
        )
    residual_q90_visible = float(visible["training_residual_absolute_q90_nearest_rank"])
    residual_q90_masked = float(masked["training_residual_absolute_q90_nearest_rank"])
    coverage_visible = statistics.fmean(
        statistics.fmean(float(row["abs_visible"]) <= residual_q90_visible for row in rows)
        for rows in training_owner_rows.values()
    )
    coverage_masked = statistics.fmean(
        statistics.fmean(float(row["abs_masked"]) <= residual_q90_masked for row in rows)
        for rows in training_owner_rows.values()
    )

    owner_predictions: list[dict[str, Any]] = []
    for owner, rows in sorted(heldout_owner_rows.items()):
        owner_predictions.append(
            {
                "owner_id": int(owner),
                "terminal_floor": heldout_counts[owner]["terminal_floor"],
                "eligible_rows": len(rows),
                "mean_current_floor": statistics.fmean(row["current_floor"] for row in rows),
                "mean_actual_remaining_floors": statistics.fmean(row["target_remaining_floors"] for row in rows),
                "mean_prediction_visible": statistics.fmean(row["prediction_visible"] for row in rows),
                "mean_prediction_masked": statistics.fmean(row["prediction_masked"] for row in rows),
                "mean_prediction_floor_only": statistics.fmean(row["prediction_floor_only"] for row in rows),
                "mean_prediction_floor_hp": statistics.fmean(row["prediction_floor_hp"] for row in rows),
                "owner_errors": overall["owner_errors"][owner],
                "potion_present_rows": heldout_counts[owner]["potion_present_rows"],
            }
        )

    stratified: dict[str, Any] = {}
    strata: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for key in ("potion_presence", "screen_family", "floor_band"):
        strata[key] = defaultdict(list)
        for owner, rows in heldout_owner_rows.items():
            for row in rows:
                if key == "potion_presence":
                    value = "present" if row["potion_present"] else "absent"
                else:
                    value = str(row[key])
                strata[key][value].append({"owner_id": owner, **row})
        stratified[key] = {}
        for value, group_rows in sorted(strata[key].items()):
            group_owner_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in group_rows:
                owner = row.pop("owner_id")
                group_owner_rows[owner].append(row)
            support = len(group_owner_rows)
            if support < 2:
                stratified[key][value] = {"supporting_owners": support, "decision_rows": len(group_rows), "status": "SKIPPED_LT_2_OWNERS"}
                continue
            group_metrics = _metrics_for_rows(group_owner_rows, include_bootstrap=False)
            stratified[key][value] = {
                "supporting_owners": support,
                "decision_rows": sum(len(rows) for rows in group_owner_rows.values()),
                "owner_equal_metrics": group_metrics["owner_equal_metrics"],
                "paired_owner_cluster_bootstrap": bootstrap_ci(
                    {
                        owner: metrics["paired_delta_masked_minus_visible"]
                        for owner, metrics in group_metrics["owner_errors"].items()
                    },
                    f"{key}={value}",
                ),
            }
            ensure_budget()

    report = {
        "schema": "issue55-evaluation-report-v1",
        "disposition": "STUDY_ONLY",
        "builder_preliminary_classification": None,
        "independent_reviewer_classification_pending": True,
        "estimand": prereg["estimand"],
        "historical_controller_config_fingerprint": EXPECTED_CONTROLLER_CONFIG_SHA256,
        "source": {
            "manifest_sha256": EXPECTED_MANIFEST_SHA256,
            "source_metadata_sha256": EXPECTED_SOURCE_METADATA_SHA256,
            "training_inputs": admission["training_input_hashes"],
            "heldout_inputs": sorted(heldout_inputs, key=lambda item: item["basename"]),
            "validation_files_opened": False,
            "only_assigned_on_arm_records_used": True,
            "projection_field_evidence": {
                "native_commit": EXPECTED_NATIVE_COMMIT,
                "native_public_projection_v3": "bindings/slaythespire.cpp:3842-3920",
                "native_raw_snapshot_representation": "bindings/slaythespire.cpp:3628-3692",
                "native_potion_id_label_and_slot_helpers": "bindings/slaythespire.cpp:583-600; include/constants/Potions.h:116-163",
                "stsrl_decision_time_record_fields": "src/sts_combat_rl/sim/controlled_run.py:368-384 and _player_hp at 577-589",
                "public_projection_is_not_serialized_separately_in_issue45_records": True,
            },
        },
        "pre_registration": {
            "sha256": sha256_file(PREREG_PATH)[0],
            "amendments_sha256": {path.name: sha256_file(path)[0] for path in AMENDMENT_PATHS},
            "freeze_receipt_sha256": sha256_file(freeze_path)[0],
            "heldout_opened_after_models_frozen": True,
        },
        "heldout_support": {
            "owner_count": len(heldout_owner_rows),
            "decision_row_count": sum(len(rows) for rows in heldout_owner_rows.values()),
            "owner_rows": heldout_counts,
            "battle_legacy_summary_disagreements": {
                "game_context_hp_vs_projection_battle_context_rows": heldout_battle_hp_summary_mismatches,
                "game_context_max_hp_vs_projection_battle_context_rows": heldout_battle_max_hp_summary_mismatches,
            },
            "public_feature_artifact": {"path": heldout_path.name, "sha256": heldout_sha, "bytes": heldout_path.stat().st_size},
        },
        "primary_evaluation": overall,
        "calibration": {
            "POTION_VISIBLE": _calibration(heldout_owner_rows, "prediction_visible"),
            "POTION_MASKED": _calibration(heldout_owner_rows, "prediction_masked"),
            "FLOOR_ONLY": _calibration(heldout_owner_rows, "prediction_floor_only"),
            "FLOOR_PLUS_HP_HEURISTIC": _calibration(heldout_owner_rows, "prediction_floor_hp"),
        },
        "descriptive_training_residual_band_coverage": {
            "POTION_VISIBLE": {"q90_absolute_training_residual": residual_q90_visible, "owner_equal_row_fraction_within_band": coverage_visible, "warning": "Descriptive only; not calibrated or guaranteed."},
            "POTION_MASKED": {"q90_absolute_training_residual": residual_q90_masked, "owner_equal_row_fraction_within_band": coverage_masked, "warning": "Descriptive only; not calibrated or guaranteed."},
        },
        "predictions_vs_actual_by_owner": owner_predictions,
        "stratified_descriptives": stratified,
        "interpretation": "Historical out-of-owner observational prediction only. A positive paired error difference cannot distinguish information from Expert selection/confounding and is not a potion use-versus-retain value, current Native value, policy readiness, or Heart-strength claim.",
        "limitations": [
            "Only 16 heldout owner clusters; intervals may be wide and this is not a powered efficacy test.",
            "The input omits visible map, boss, reward, and other unsupported screen facts; this is a restricted public subset, not observation closure.",
            "The floor-plus-HP baseline is a fixed heuristic frozen before fitting, not a trained model.",
            "Validation owners remain unused and unopened.",
            "During battle, the historical STSRL player_hp summary can reflect GameContext cur_hp while the pinned Native public projection exposes BattleContext player.curHp; the feature uses the latter and records their disagreement count.",
        ],
    }
    delta_ci = overall["primary_paired_owner_cluster_bootstrap"]["ci_95"]
    if delta_ci[0] > 0.0:
        report["builder_preliminary_classification"] = "RESTRICTED_HISTORICAL_PREDICTION_SIGNAL_SUPPORTED"
    elif delta_ci[1] < 0.0:
        report["builder_preliminary_classification"] = "SIGNAL_HARM_SUPPORTED"
    else:
        report["builder_preliminary_classification"] = "SIGNAL_NOT_ESTABLISHED"
    write_json(ARTIFACTS / "report.json", report)
    checkpoint_stage({"evaluation_report_written": True, "heldout_owners_complete": len(heldout_owner_rows)})


def run_stage(name: str, function: Callable[[], None]) -> int:
    try:
        if not start_stage(name):
            print(f"{name}: already complete")
            return 0
        function()
        finish_stage("COMPLETE")
        print(f"{name}: complete")
        return 0
    except StopStudy as exc:
        fail_stage(exc.code, str(exc))
        print(f"{name}: {exc.code}: {exc}")
        return 0
    except StudyFailure as exc:
        if _stage_name is None:
            print(f"{name}: {exc.code}: {exc}", file=sys.stderr)
            return 2 if exc.code != "RESOURCE_BLOCKED" else 3
        fail_stage(exc.code, str(exc))
        print(f"{name}: {exc.code}: {exc}", file=sys.stderr)
        return 2 if exc.code != "RESOURCE_BLOCKED" else 3
    except Exception as exc:  # durable boundary state is more useful than losing the failed stage
        fail_stage("BUILDER_REPAIR_REQUIRED", f"Unexpected {type(exc).__name__}: {exc}")
        print(f"{name}: failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def retry_terminal_stage(name: str) -> None:
    assert _state is not None
    stage = _state.get("stages", {}).get(name)
    require(
        stage is not None and stage.get("status") == "TERMINAL",
        f"Stage {name} is not in a retryable terminal state.",
        "BUILDER_REPAIR_REQUIRED",
    )
    stage["status"] = "FAILED"
    stage["retry_note"] = "Explicit retry after an implementation or source-admission correction; earlier attempt remains in the audit history."
    _state["terminal_classification"] = None
    _write_state()


def main(argv: list[str]) -> int:
    commands = {
        "prepare": ("prepare_train_only", prepare),
        "fit": ("fit_frozen_models", fit),
        "evaluate": ("evaluate_heldout", evaluate),
    }
    retry_commands = {
        "retry-prepare": ("prepare_train_only", prepare),
        "retry-fit": ("fit_frozen_models", fit),
        "retry-evaluate": ("evaluate_heldout", evaluate),
    }
    all_commands = commands | retry_commands
    if len(argv) != 2 or argv[1] not in all_commands:
        print("usage: python3 run_study.py {prepare|fit|evaluate|retry-prepare|retry-fit|retry-evaluate}", file=sys.stderr)
        return 64
    load_state_and_ledger()
    command = argv[1]
    stage_name, function = all_commands[command]
    if command.startswith("retry-"):
        try:
            retry_terminal_stage(stage_name)
        except StudyFailure as exc:
            print(f"{command}: {exc.code}: {exc}", file=sys.stderr)
            return 2
    return run_stage(stage_name, function)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
