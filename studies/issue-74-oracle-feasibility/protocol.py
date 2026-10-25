"""Pure, study-local preregistration helpers for Issue 74.

This module deliberately does not import the simulator or any privileged API.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


OWNER_SEEDS = tuple(range(173730001, 173730009))
SELECTION_DOMAIN = "issue74-root-selection-v1"
SEED_DOMAIN = "issue74-rng-v1"
GATE_C_ROOT_IDS = frozenset({
    "root-173730001-0000", "root-173730001-0001",
    "root-173730001-0002", "root-173730001-0003",
})


class ContractError(ValueError):
    """A source, identity, budget, or registered-design invariant failed."""


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def canonical_payload(value: Any) -> bytes:
    """Match the frozen Stage 0 canonical JSON convention (without newline)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def selection_key(root: Mapping[str, Any]) -> str:
    """Hash only the four prespecified, outcome-independent root fields."""
    required = ("owner_seed", "screen", "policy_decision_index", "root_id")
    if any(name not in root for name in required):
        raise ContractError("root lacks a registered selection field")
    owner = _int(root["owner_seed"], "owner_seed")
    decision = _int(root["policy_decision_index"], "policy_decision_index")
    screen, root_id = str(root["screen"]), str(root["root_id"])
    if not screen or not root_id:
        raise ContractError("root selection identity is empty")
    value = f"{SELECTION_DOMAIN}|{owner}|{screen}|{decision}|{root_id}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def select_roots(records: Iterable[Mapping[str, Any]], *,
                 excluded_ids: Iterable[str] = GATE_C_ROOT_IDS) -> list[dict[str, Any]]:
    """Take exactly the first four hash-ranked roots per fixed owner.

    Selection is performed before checking candidate count or outcomes. Returned
    metadata is an allowlist and excludes privileged projections, action payloads,
    and terminal outcomes.
    """
    source = list(records)
    if len(source) != 225:
        raise ContractError(f"expected exactly 225 locked source roots, got {len(source)}")
    excluded = frozenset(str(value) for value in excluded_ids)
    seen: set[str] = set()
    by_owner: dict[int, list[Mapping[str, Any]]] = {seed: [] for seed in OWNER_SEEDS}
    for row in source:
        root_id = str(row.get("root_id", ""))
        if not root_id or root_id in seen:
            raise ContractError("source root IDs must be present and unique")
        seen.add(root_id)
        owner = _int(row.get("owner_seed"), "owner_seed")
        if owner not in by_owner:
            raise ContractError(f"unexpected debug owner seed: {owner}")
        if row.get("candidate_binding_validation_passed") is not True:
            raise ContractError(f"source root {root_id} is not provenance-qualified")
        by_owner[owner].append(row)
    if not excluded.issubset(seen):
        raise ContractError("a Gate-C exclusion is absent from the locked source population")
    selected: list[dict[str, Any]] = []
    for owner in OWNER_SEEDS:
        eligible_for_selection = [row for row in by_owner[owner]
                                  if str(row["root_id"]) not in excluded]
        if len(eligible_for_selection) < 4:
            raise ContractError(f"owner {owner} has fewer than four non-Gate-C roots")
        ranked = sorted(eligible_for_selection,
                        key=lambda row: (selection_key(row), str(row["root_id"])))
        for row in ranked[:4]:
            fingerprints = row.get("root_fingerprints")
            if not isinstance(fingerprints, Mapping) or not fingerprints:
                raise ContractError(f"root {row['root_id']} has no locked identity fingerprints")
            selected.append({
                "owner_seed": owner,
                "root_id": str(row["root_id"]),
                "screen": str(row["screen"]),
                "owner_decision_index": _int(row["owner_decision_index"], "owner_decision_index"),
                "policy_decision_index": _int(row["policy_decision_index"], "policy_decision_index"),
                "legal_occurrence_count": _int(len(row["legal_action_occurrences"]), "legal_occurrence_count"),
                "legal_action_identities_sha256": sha256_bytes(canonical_payload(row["legal_action_occurrences"])),
                "projection_digest": str(row["projection_digest"]),
                "feature_matrix_sha256": str(row["feature_matrix_sha256"]),
                "root_fingerprints": dict(sorted((str(k), str(v)) for k, v in fingerprints.items())),
                "source_file_sha256": str(row["_source_file_sha256"]),
                "selection_sha256": selection_key(row),
                "selection_rank_within_owner": ranked.index(row) + 1,
                "selected_before_eligibility_checks": True,
            })
    if len(selected) != 32 or len({row["root_id"] for row in selected}) != 32:
        raise ContractError("selection must contain 32 unique roots without replacement")
    return selected


def stream_seed(*, owner_seed: int, root_id: str, decision_index: int,
                stream: str, candidate_index: int = 0, replicate: int = 0) -> int:
    """Derive independent C, Oracle-policy, and expert RNG streams."""
    if stream not in {"control_policy", "control_expert", "oracle_policy", "oracle_expert"}:
        raise ContractError("unknown Issue 74 RNG stream")
    coords = (_int(owner_seed, "owner_seed"), str(root_id),
              _int(decision_index, "decision_index"),
              _int(candidate_index, "candidate_index"), _int(replicate, "replicate"))
    if min(coords[0], coords[2], coords[3], coords[4]) < 0 or not coords[1]:
        raise ContractError("negative or empty RNG coordinate")
    payload = "|".join((SEED_DOMAIN, stream, *(str(value) for value in coords))).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def verify_source_trace_event(root: Mapping[str, Any], event: Mapping[str, Any]) -> None:
    """Bind a selected source root to its exact historical decision event."""
    if (event.get("kind") != "categorical"
            or event.get("native_screen") != root.get("screen")
            or event.get("projection_digest") != root.get("projection_digest")
            or event.get("legal_action_identities") != root.get("legal_action_occurrences")):
        raise ContractError(f"selected root does not match its locked owner trace: {root.get('root_id')}")
    ids = event["legal_action_identities"]
    chosen = event.get("chosen_index")
    if len(ids) != len(root.get("legal_action_occurrences", [])):
        raise ContractError("historical root legal occurrence count changed")
    if isinstance(chosen, bool) or not isinstance(chosen, int) or not 0 <= chosen < len(ids):
        raise ContractError("historical debug-owner choice is not a live legal occurrence")


def verify_replayed_root(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    """Require the replayed in-memory checkpoint root to match its locked receipt."""
    if actual.get("screen") != expected.get("screen"):
        raise ContractError("replayed root screen differs from its source receipt")
    if actual.get("projection_digest") != expected.get("projection_digest"):
        raise ContractError("replayed root privileged-projection digest differs from source")
    if actual.get("legal_action_identities") != expected.get("legal_action_occurrences"):
        raise ContractError("replayed root legal occurrences differ or were reordered")
    wanted = expected.get("root_fingerprints", {})
    observed = actual.get("root_fingerprints")
    if not isinstance(observed, Mapping):
        observed = {key: actual.get(key) for key in wanted}
    if not isinstance(observed, Mapping) or not isinstance(wanted, Mapping) or dict(observed) != dict(wanted):
        raise ContractError("replayed full-state/public/resource fingerprints differ from source")
    if actual.get("checkpoint") is None:
        raise ContractError("native checkpoint must be captured and consumed in memory")


def verify_forced_root_receipt(root: Mapping[str, Any], candidate_index: int,
                               receipt: Mapping[str, Any]) -> None:
    """Verify Stage 0's locked exact-occurrence acknowledgment for one suffix."""
    identities = root.get("legal_action_occurrences")
    if (not isinstance(identities, Sequence) or isinstance(identities, (str, bytes))
            or isinstance(candidate_index, bool) or not isinstance(candidate_index, int)
            or not 0 <= candidate_index < len(identities)):
        raise ContractError("requested suffix action is outside the locked legal occurrences")
    expected_identity = sha256_bytes(canonical_payload(identities[candidate_index]))
    expected_legal = sha256_bytes(canonical_payload(identities))
    if (receipt.get("forced_root_consumed") is not True
            or receipt.get("candidate_index") != candidate_index
            or receipt.get("candidate_identity_sha256") != expected_identity
            or receipt.get("legal_action_identities_sha256") != expected_legal):
        raise ContractError("suffix did not consume the exact locked native legal occurrence")


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{name} must be an integer")
    return value


def write_once(path: Path, value: Mapping[str, Any]) -> str:
    """Create a durable JSON receipt exclusively; never overwrite a prior result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json(value)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise ContractError(f"immutable receipt already exists: {path.name}") from exc
    return sha256_bytes(data)


@dataclass(frozen=True)
class ResourceCharge:
    builder_cpu_seconds: float
    native_cpu_seconds: float
    reviewer_reserve_seconds: float = 20000.0

    def add(self, *, builder_cpu_seconds: float, native_cpu_seconds: float) -> "ResourceCharge":
        values = (builder_cpu_seconds, native_cpu_seconds)
        if any(not math.isfinite(x) or x < 0 for x in values):
            raise ContractError("resource charges must be finite and nonnegative")
        result = ResourceCharge(self.builder_cpu_seconds + values[0],
                                self.native_cpu_seconds + values[1],
                                self.reviewer_reserve_seconds)
        if result.builder_cpu_seconds > 100000 or result.native_cpu_seconds > 85000:
            raise ContractError("Issue 74 cumulative CPU ceiling would be exceeded")
        if result.builder_cpu_seconds + result.reviewer_reserve_seconds > 120000:
            raise ContractError("aggregate ceiling would consume protected Reviewer reserve")
        return result
