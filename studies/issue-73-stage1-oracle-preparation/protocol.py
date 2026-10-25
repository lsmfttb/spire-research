"""Frozen, study-local Stage 1 contract helpers for GitHub Issue 73.

This module is intentionally independent of the native simulator so that
contract checks and small adversarial tests cannot accidentally start owners.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import random
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
LOCK_PATH = HERE / "config.lock.sha256"
STAGE0 = HERE.parent / "issue-73-privileged-noncombat-oracle-stage0"
STAGE0_CONFIG = STAGE0 / "config.json"
STAGE0_HANDOFF = STAGE0 / "receipts" / "stage0-gate-bc-v14" / "reports" / "gate-c-v14-handoff.json"
ARMS = ("O", "C", "E")
BOOTSTRAP_SEED = 173739999


class ContractError(ValueError):
    """A frozen Stage 1 protocol, source, or evidence contract was violated."""


def _normalized_text(path: Path) -> bytes:
    payload = path.read_bytes()
    if b"\r" in payload.replace(b"\r\n", b""):
        raise ContractError(f"{path} contains a bare CR")
    return payload.replace(b"\r\n", b"\n")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(_normalized_text(path))


def read_config(path: Path = CONFIG_PATH, lock_path: Path = LOCK_PATH) -> dict[str, Any]:
    digest = sha256_file(path)
    try:
        expected = lock_path.read_text(encoding="ascii").strip().lower()
    except OSError as exc:
        raise ContractError("Stage 1 config lock is missing") from exc
    if digest != expected:
        raise ContractError(f"Stage 1 config differs from its committed lock: {digest} != {expected}")
    value = json.loads(_normalized_text(path))
    if not isinstance(value, dict) or value.get("schema_id") != "issue73-stage1-oce-preregistration-v1":
        raise ContractError("unexpected Stage 1 config schema")
    return value



def verify_preparation_resource_receipt(path: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the conservative accounting debit for authorized preparation work."""
    try:
        receipt = json.loads(_normalized_text(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("durable preparation resource receipt is missing or malformed") from exc
    if receipt.get("schema_id") != "issue73-stage1-preparation-resource-receipt-v1":
        raise ContractError("unexpected preparation resource receipt schema")
    if receipt.get("status") != "CONSERVATIVE_ALLOWANCE_CHARGED":
        raise ContractError("preparation resource receipt does not record a conservative charge")
    if receipt.get("measured_builder_preparation_cpu_seconds") is not None:
        raise ContractError("unmetered preparation CPU must not be reported as measured")
    if receipt.get("formal_owner_seeds_consumed") != 0 or receipt.get("native_game_execution_performed") is not False:
        raise ContractError("preparation accounting cannot claim formal owners or native game execution")
    accounting = receipt.get("accounting")
    if not isinstance(accounting, Mapping):
        raise ContractError("preparation resource accounting block is missing")
    budget = config["resource_proposal_not_authorization"]
    def require_close(key: str, expected: float) -> float:
        value = accounting.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ContractError(f"preparation resource accounting field is invalid: {key}")
        if not math.isclose(float(value), float(expected), rel_tol=0, abs_tol=1e-6):
            raise ContractError(f"preparation resource accounting mismatch: {key}")
        return float(value)
    prior_builder = require_close("prior_builder_charged_cpu_seconds", 5330.657158)
    prep_cap = require_close("authorized_preparation_increment_cap_cpu_seconds",
                             budget["builder_preparation_incremental_cpu_cap_seconds"])
    charged = require_close("conservative_preparation_increment_cpu_seconds_charged", prep_cap)
    builder_ceiling = require_close("builder_cumulative_cpu_ceiling_seconds", 10000)
    builder_total = require_close("builder_cumulative_conservative_cpu_seconds", prior_builder + charged)
    require_close("builder_remaining_cpu_seconds", builder_ceiling - builder_total)
    reviewer_reserve = require_close("protected_reviewer_reserve_cpu_seconds", 2000)
    aggregate_ceiling = require_close("aggregate_cpu_ceiling_seconds", 12000)
    aggregate_total = require_close("aggregate_conservative_including_reviewer_reserve_cpu_seconds",
                                    builder_total + reviewer_reserve)
    require_close("aggregate_remaining_after_reviewer_reserve_cpu_seconds",
                  aggregate_ceiling - aggregate_total)
    prior_native = require_close("prior_native_stage0_charged_cpu_seconds", 1899.047024)
    native_game_cpu = require_close("native_game_cpu_seconds_charged_for_preparation", 0)
    native_ceiling = require_close("native_cumulative_cpu_ceiling_seconds", 6000)
    native_total = require_close("native_cumulative_cpu_seconds_after_preparation", prior_native + native_game_cpu)
    require_close("native_remaining_cpu_seconds", native_ceiling - native_total)
    preflight_cap = require_close("authorized_native_preflight_cap_cpu_seconds",
                                  budget["builder_preparation_native_preflight_cpu_cap_seconds"])
    require_close("native_preflight_cpu_seconds_charged", 0)
    require_close("native_preflight_cpu_seconds_remaining", preflight_cap)
    if builder_total > builder_ceiling or aggregate_total > aggregate_ceiling or native_total > native_ceiling:
        raise ContractError("conservative preparation charge exceeds an authorized cumulative ceiling")
    return receipt

def verify_stage0_contract(config: Mapping[str, Any]) -> dict[str, Any]:
    """Verify the committed Stage 0 source tree and its accepted Gate C receipt."""
    source = config["source"]
    stage0_config = json.loads(_normalized_text(STAGE0_CONFIG))
    if sha256_file(STAGE0_CONFIG) != source["stage0_config_sha256"]:
        raise ContractError("Stage 0 config no longer matches the registered Stage 0 identity")
    if (stage0_config.get("stage0", {}).get("stage1_authorized") is not False
            or stage0_config.get("resources", {}).get("stage1_budget", {}).get("formal_execution_authorized") is not False):
        raise ContractError("Stage 0 source config has an unexpected Stage 1 activation state")
    lock = (STAGE0 / "config.lock.sha256").read_text(encoding="ascii").strip().lower()
    if lock != source["stage0_config_sha256"]:
        raise ContractError("Stage 0 config.lock.sha256 does not match its config")
    for rel, expected in stage0_config["identity"]["study_source_sha256"].items():
        path = STAGE0 / rel
        if not path.is_file() or sha256_file(path) != expected:
            raise ContractError(f"locked Stage 0 source changed: {rel}")
    try:
        gate = json.loads(_normalized_text(STAGE0_HANDOFF))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("accepted Stage 0 Gate C handoff is missing or malformed") from exc
    if sha256_file(STAGE0_HANDOFF) != source["stage0_gate_c_handoff_sha256"]:
        raise ContractError("Stage 0 Gate C handoff digest changed")
    if (gate.get("status") != "PASS" or gate.get("registered_suffix_count") != 64
            or gate.get("formal_seed_consumed") is not False
            or gate.get("stage1_authorized") is not False):
        raise ContractError("Stage 0 handoff does not match the accepted feasibility-only boundary")
    return {"status": "PASS", "stage0_config_sha256": source["stage0_config_sha256"],
            "gate_c_handoff_sha256": source["stage0_gate_c_handoff_sha256"],
            "gate_c_suffixes": gate["registered_suffix_count"],
            "formal_stage1_authorized": False}


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(root), *args], text=True, stderr=subprocess.STDOUT).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContractError(f"cannot inspect pinned source checkout {root}: {exc}") from exc


def verify_checkout(root: str | Path, *, commit: str, source_files: Mapping[str, str]) -> dict[str, Any]:
    """Require an exact clean checkout and exact normalized hashes for locked files."""
    path = Path(root).resolve(strict=True)
    actual_commit = _git(path, "rev-parse", "HEAD")
    if actual_commit != commit:
        raise ContractError(f"wrong source commit at {path}: {actual_commit} != {commit}")
    if _git(path, "status", "--porcelain"):
        raise ContractError(f"source checkout is dirty: {path}")
    checked: dict[str, str] = {}
    for rel, expected in sorted(source_files.items()):
        item = path / rel
        if not item.is_file() or sha256_file(item) != expected:
            raise ContractError(f"locked source file differs or is missing: {rel}")
        committed = subprocess.check_output(["git", "-C", str(path), "show", f"{commit}:{rel}"])
        if sha256_bytes(committed.replace(b"\r\n", b"\n")) != expected:
            raise ContractError(f"locked Git blob differs from config: {rel}")
        checked[rel] = expected
    return {"commit": actual_commit, "source_files": checked, "clean": True}


def verify_external_sources(config: Mapping[str, Any], native_root: str | Path,
                            stsr_root: str | Path, native_module: str | Path) -> dict[str, Any]:
    stage0_config = json.loads(_normalized_text(STAGE0_CONFIG))
    identity = stage0_config["identity"]
    if sys.version.split()[0] != identity["python"]:
        raise ContractError(f"Python runtime differs from the pinned Stage 0 runtime: {sys.version.split()[0]}")
    for package in ("numpy", "torch"):
        actual = importlib.metadata.version(package)
        if actual != identity[package]:
            raise ContractError(f"{package} runtime differs from the pinned Stage 0 runtime: {actual}")
    native = verify_checkout(native_root, commit=config["source"]["native_commit"],
                             source_files=identity["native_source_files_sha256"])
    integration = config["source"]["native_integration_commit"]
    _git(Path(native_root).resolve(strict=True), "cat-file", "-e", f"{integration}^{{commit}}")
    if subprocess.run(["git", "-C", str(Path(native_root).resolve(strict=True)),
                       "merge-base", "--is-ancestor", integration,
                       config["source"]["native_commit"]], check=False).returncode != 0:
        raise ContractError("locked native integration commit is not an ancestor of the native source commit")
    stsr = verify_checkout(stsr_root, commit=config["source"]["stsr_commit"],
                           source_files=identity["stsr_source_files_sha256"])
    binary = Path(native_module).resolve(strict=True)
    digest = sha256_bytes(binary.read_bytes())
    if digest != identity["native_binary_sha256"]:
        raise ContractError(f"loaded native module digest differs from its lock: {digest}")
    return {"native": native, "stsr": stsr,
            "native_module_sha256": digest, "native_module": str(binary)}


def owner_seeds(config: Mapping[str, Any]) -> tuple[int, ...]:
    values = tuple(int(seed) for seed in config["population"]["seeds"])
    if (len(values) != 96 or len(set(values)) != 96
            or values != tuple(range(config["population"]["seed_first"], config["population"]["seed_last"] + 1))):
        raise ContractError("the fixed 96-owner Stage 1 seed table is malformed")
    return values


def validate_owner_block(arms: Mapping[str, Any]) -> None:
    if set(arms.keys()) != set(ARMS):
        raise ContractError("owner block must contain exactly the frozen O, C, E arms")
    for arm in ARMS:
        result = arms[arm]
        if not isinstance(result, Mapping) or result.get("terminal_complete") is not True:
            raise ContractError(f"owner arm {arm} is missing or nonterminal")
        terminal = result.get("terminal")
        floor = terminal.get("terminal_floor") if isinstance(terminal, Mapping) else None
        if isinstance(floor, bool) or not isinstance(floor, int) or not 0 <= floor <= 99:
            raise ContractError(f"owner arm {arm} has an invalid terminal floor")


def paired_differences(owner_rows: Sequence[Mapping[str, Any]], *, arm: str) -> list[float]:
    if arm not in {"C", "E"} or len(owner_rows) != 96:
        raise ContractError("paired differences require 96 complete owners and comparison arm C or E")
    out: list[float] = []
    for row in owner_rows:
        if set(row) != set(ARMS):
            raise ContractError("partial O/C/E owner block cannot enter the primary analysis")
        validate_owner_block(row)
        out.append(float(row["O"]["terminal"]["terminal_floor"] - row[arm]["terminal"]["terminal_floor"]))
    return out


def paired_precision(*, n: int = 96) -> dict[str, float]:
    if n != 96:
        raise ContractError("Stage 1 owner count is fixed at 96")
    return {"n": float(n), "95pct_half_width_in_paired_sd": 1.96 / math.sqrt(n),
            "80pct_mde_in_paired_sd": (1.96 + 0.8416212335729143) / math.sqrt(n)}


def bootstrap_percentile(values: Sequence[float], *, resamples: int = 20000,
                         seed: int = BOOTSTRAP_SEED) -> dict[str, Any]:
    if len(values) != 96 or resamples != 20000:
        raise ContractError("the frozen analysis requires 96 owners and 20,000 owner bootstrap resamples")
    data = [float(value) for value in values]
    if not all(math.isfinite(value) for value in data):
        raise ContractError("paired outcome contains a nonfinite value")
    rng = random.Random(seed)
    n = len(data)
    draws = sorted(sum(data[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    return {"n_owners": n, "resamples": resamples, "seed": seed,
            "estimate": sum(data) / n, "ci95_nearest_rank": [draws[499], draws[19499]],
            "unit": "owner block", "method": "percentile bootstrap with replacement"}


def continuation_seed(owner_seed: int, decision_index: int, candidate_index: int, replicate: int,
                      *, stream: str = "native") -> int:
    if min(owner_seed, decision_index, candidate_index, replicate) < 0 or replicate >= 8:
        raise ContractError("continuation seed coordinates are outside the fixed Stage 1 design")
    if stream not in {"native", "suffix", "expert"}:
        raise ContractError("unknown continuation RNG stream")
    identity = f"issue73-stage1-v1|{stream}|{owner_seed}|{decision_index}|{candidate_index}|{replicate}".encode()
    return int.from_bytes(hashlib.sha256(identity).digest()[:4], "big") & 0x7FFFFFFF


@dataclass(frozen=True)
class OracleDecision:
    chosen_index: int
    fallback: bool
    reason: str
    means: tuple[float, ...]
    sample_variances: tuple[float, ...]
    thresholds: tuple[float, ...]


def choose_or_abstain(samples: Sequence[Sequence[float]], *, fallback_index: int) -> OracleDecision:
    """Use the frozen two-SE rule; no post-outcome threshold adjustment."""
    if not samples:
        raise ContractError("Oracle cannot choose from an empty candidate set")
    if isinstance(fallback_index, bool) or not 0 <= fallback_index < len(samples):
        raise ContractError("C fallback is not a live legal-action occurrence")
    if len(samples) > 14:
        return OracleDecision(fallback_index, True, "candidate_count_over_cap", (), (), ())
    if any(len(row) != 8 for row in samples):
        raise ContractError("every candidate requires all eight fixed terminal continuations")
    rows = [tuple(float(x) for x in row) for row in samples]
    if any(not math.isfinite(x) or x < 0 or x > 99 for row in rows for x in row):
        raise ContractError("continuation terminal floor is invalid")
    means = tuple(sum(row) / 8 for row in rows)
    variances = tuple(sum((x - means[i]) ** 2 for x in row) / 7 for i, row in enumerate(rows))
    best = max(range(len(rows)), key=lambda i: means[i])
    if sum(mean == means[best] for mean in means) != 1:
        return OracleDecision(fallback_index, True, "tied_best_mean", means, variances, ())
    thresholds = tuple(2.0 * math.sqrt((variances[best] + variances[i]) / 8)
                       for i in range(len(rows)))
    if all(best == i or means[best] - means[i] > thresholds[i] for i in range(len(rows))):
        return OracleDecision(best, False, "unique_best_exceeds_all_two_se_thresholds", means, variances, thresholds)
    return OracleDecision(fallback_index, True, "two_se_abstention", means, variances, thresholds)


def validate_live_occurrences(candidate_ids: Sequence[Any], legal_ids: Sequence[Any], selected: Any) -> int:
    """Bind by ordered occurrence identity; reject absent, reordered, or ambiguous bindings."""
    if not candidate_ids:
        raise ContractError("native projection contains no candidate occurrences")
    if len(candidate_ids) != len(legal_ids):
        raise ContractError("privileged projection count differs from live legal occurrences")
    if list(candidate_ids) != list(legal_ids):
        raise ContractError("privileged candidate order/identity differs from live legal occurrences")
    if isinstance(selected, bool) or not isinstance(selected, int) or not 0 <= selected < len(legal_ids):
        raise ContractError("selected candidate does not map to a legal occurrence")
    return selected


def oracle_decision_receipt(*, step: int, query_index: int, candidate_count: int,
                            fallback_index: int, selected_index: int, screen: str,
                            choice: OracleDecision, fingerprints: Mapping[str, str],
                            legal_ids: Sequence[Any], attempt_count: int) -> dict[str, Any]:
    """Allowlist only coarse decision evidence; never persist projection/state/action payloads."""
    if not 0 <= fallback_index < candidate_count or not 0 <= selected_index < candidate_count:
        raise ContractError("decision receipt occurrence is outside the legal action list")
    required_hashes = ("native_state_sha256", "public_projection_sha256")
    if any(len(str(fingerprints.get(key, ""))) != 64 for key in required_hashes):
        raise ContractError("decision receipt root fingerprints are missing")
    payload = json.dumps(list(legal_ids), sort_keys=True, separators=(",", ":"), default=str).encode()
    return {"schema_id": "issue73-stage1-oracle-decision-v1", "step": int(step),
            "query_index": int(query_index), "screen": str(screen),
            "candidate_count": int(candidate_count), "fallback_index": int(fallback_index),
            "selected_index": int(selected_index), "fallback": bool(choice.fallback),
            "reason": str(choice.reason), "means": list(choice.means),
            "sample_variances": list(choice.sample_variances),
            "two_se_thresholds": list(choice.thresholds),
            "root_native_state_sha256": str(fingerprints["native_state_sha256"]),
            "root_public_state_sha256": str(fingerprints["public_projection_sha256"]),
            "legal_occurrences_sha256": sha256_bytes(payload),
            "continuation_count": int(attempt_count)}


def safe_arm_receipt(result: Mapping[str, Any], *, registered_arm: str,
                     policy_mechanics_arm: str, oracle_decisions: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Discard native legal-action traces and retain only coarse registered outcomes."""
    if registered_arm not in ARMS or policy_mechanics_arm not in {"O", "SUFFIX", "E"}:
        raise ContractError("arm receipt has an unknown registered/mechanics arm")
    if result.get("terminal_complete") is not True:
        raise ContractError("incomplete owner run cannot produce a terminal arm receipt")
    terminal = result.get("terminal")
    if not isinstance(terminal, Mapping):
        raise ContractError("terminal arm outcome is missing")
    allowed_terminal = ("outcome", "terminal_floor", "terminal_act", "heart_victory",
                        "act2_plus", "act3_plus", "act4_reached")
    safe_terminal = {key: terminal[key] for key in allowed_terminal if key in terminal}
    if not isinstance(safe_terminal.get("terminal_floor"), int) or isinstance(safe_terminal.get("terminal_floor"), bool):
        raise ContractError("terminal arm floor is not an integer")
    transitions = result.get("completed_transitions")
    if isinstance(transitions, bool) or not isinstance(transitions, int) or transitions < 0:
        raise ContractError("completed transition count is invalid")
    raw_decisions = result.get("decisions", [])
    if not isinstance(raw_decisions, Sequence) or isinstance(raw_decisions, (str, bytes)):
        raise ContractError("run decision trace has the wrong shape")
    oracle_by_step = {int(row["step"]): dict(row) for row in oracle_decisions if "step" in row}
    decisions: list[dict[str, Any]] = []
    for row in raw_decisions:
        if not isinstance(row, Mapping) or row.get("kind") not in {"single_action", "categorical"}:
            continue
        step = row.get("step")
        if isinstance(step, bool) or not isinstance(step, int):
            raise ContractError("decision step index is invalid")
        summary = {"decision_step": step,
                   "timing": "early" if 2 * step < transitions else "later",
                   "screen": str(row.get("native_screen", row.get("screen", "unknown"))),
                   "kind": str(row["kind"])}
        oracle = oracle_by_step.get(step)
        if oracle is not None:
            summary.update({"oracle_fallback": bool(oracle.get("fallback", False)),
                            "oracle_reason": str(oracle.get("reason", "unknown")),
                            "oracle_query_index": oracle.get("query_index"),
                            "oracle_nonfallback_changed_action": bool(
                                "query_index" in oracle and not oracle.get("fallback")
                                and oracle.get("selected_index") != oracle.get("fallback_index"))})
        decisions.append(summary)
    cpu = result.get("cpu_seconds")
    wall = result.get("wall_seconds")
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value < 0 for value in (cpu, wall)):
        raise ContractError("arm CPU/wall accounting is invalid")
    return {"registered_arm": registered_arm, "policy_mechanics_arm": policy_mechanics_arm,
            "run_id": str(result.get("run_id", "")), "seed": int(result.get("seed", -1)),
            "terminal_complete": True, "terminal": safe_terminal,
            "completed_transitions": transitions,
            "requested_steps": int(result.get("requested_steps", transitions)),
            "cpu_seconds": float(cpu), "wall_seconds": float(wall),
            "native_source_commit": str(result.get("native_source_commit", "")),
            "native_module_sha256": str(result.get("native_module_sha256", "")),
            "screen_counts": {str(key): int(value) for key, value in result.get("screen_counts", {}).items()},
            "action_kind_counts": {str(key): int(value) for key, value in result.get("action_kind_counts", {}).items()},
            "exposures": {str(key): bool(value) for key, value in result.get("exposures", {}).items()},
            "decision_summary": decisions}


def evaluate_candidate_grid(candidate_count: int, *, owner_seed: int, decision_index: int,
                            fallback_index: int, sampler: Callable[[int, int, int, float], Mapping[str, Any]],
                            expected_native_commit: str, query_index: int) -> tuple[OracleDecision, list[dict[str, Any]]]:
    """Run a complete fixed suffix grid or fail; failed attempts are never subsetted/retried."""
    if candidate_count < 1:
        raise ContractError("Oracle candidate set is empty")
    if not 0 <= fallback_index < candidate_count:
        raise ContractError("C fallback occurrence is outside the live action list")
    if candidate_count > 14:
        return OracleDecision(fallback_index, True, "candidate_count_over_cap", (), (), ()), []
    if query_index >= 6:
        return OracleDecision(fallback_index, True, "query_budget_exhausted", (), (), ()), []
    samples: list[list[float]] = [[] for _ in range(candidate_count)]
    attempts: list[dict[str, Any]] = []
    for candidate in range(candidate_count):
        for replicate in range(8):
            seed = continuation_seed(owner_seed, decision_index, candidate, replicate)
            receipt = dict(sampler(candidate, replicate, seed, 20.0))
            attempts.append(receipt)
            if receipt.get("terminal_complete") is not True:
                raise ContractError(f"fixed continuation {candidate}/{replicate} is not terminal")
            if receipt.get("native_source_commit") != expected_native_commit:
                raise ContractError(f"fixed continuation {candidate}/{replicate} used a wrong native source")
            cpu = receipt.get("cpu_seconds")
            if isinstance(cpu, bool) or not isinstance(cpu, (int, float)) or not math.isfinite(cpu) or cpu > 20.0:
                raise ContractError(f"fixed continuation {candidate}/{replicate} exceeded its CPU cap")
            terminal = receipt.get("terminal")
            floor = terminal.get("terminal_floor") if isinstance(terminal, Mapping) else None
            if isinstance(floor, bool) or not isinstance(floor, int) or not 0 <= floor <= 99:
                raise ContractError(f"fixed continuation {candidate}/{replicate} has no valid terminal floor")
            samples[candidate].append(float(floor))
    return choose_or_abstain(samples, fallback_index=fallback_index), attempts


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write a durable JSON file without overwriting a pre-existing receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ContractError(f"refusing to overwrite durable receipt: {path}")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, sort_keys=True, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        import os
        os.fsync(handle.fileno())
    temporary.replace(path)
