"""R2 operational CPU forecast and hard-budget guard for Issue 60.

The sealed R1 runner, model, seeds, objective, and endpoints remain unchanged.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import statistics
import time
from typing import Any, Callable, Mapping, Sequence

from resource_amendment_r1 import (
    ResourceAmendmentError, _git, _git_bytes, _study_git_path,
    sha256_bytes, verify_resource_chain,
)


ISSUE = 60
PARENT_HANDOFF = "b31f3f4e741f6ae4966ad1b75f0e24340d08f2bc"
PLANNER_DECISION_ID = 6210340781
PLANNER_DECISION_URL = "https://github.com/lsmfttb/spire-research/issues/60#issuecomment-6210340781"
R2_RECEIPT = "evidence/preflight/resource-r2.json"
R2_STOP_RECEIPT = "evidence/preflight/resource-r2-stop.json"
R2_SOURCE_PATHS = (
    "resource_amendment_r2.py",
    "run_study_r2.py",
    "test_resource_amendment_r2.py",
)
R1_ANCHORS = (
    "resource-amendment-r1.json",
    "frozen-manifest-r1.json",
    "resource-authorization-r1.json",
    "evidence/preflight/resource-r1.json",
    "evidence/preflight/resource-r1-seal.json",
    "evidence/attempt-status-r1-reconciliation.json",
)
CPU_LEDGER = "evidence/cpu-ledger.jsonl"
CONFIG_PATH = "config.json"
STATIC_ALLOWANCE_SECONDS = 600.0
BUILDER_BUDGET_SECONDS = 180_000.0
REVIEWER_RESERVE_SECONDS = 20_000.0
PROCESS_CPU_CAP_SECONDS = 200_000.0
FORMAL_GAME_COUNT = 2_688
GAME_COST_MULTIPLIER = 1.5
FORECAST_HEADROOM = 0.2
RECENT_WINDOW = 128
OPTIMIZER_COST_MULTIPLIER = 1.5
OPTIMIZER_HEADROOM = 0.2
GATE_FINALIZATION_MARGIN_SECONDS = 10.0
CPU_SIGNAL_GRACE_SECONDS = 30
ARM_ORDER = ("final_block1", "final_block2", "p0", "A", "Expert")


class ResourceBudgetExceeded(BaseException):
    """Stop before recording an incomplete game or applying a partial batch."""


class ResourceAmendmentErrorR2(RuntimeError):
    """R2 authorization, provenance, or resource-gate verification failed."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResourceAmendmentErrorR2(f"missing or invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ResourceAmendmentErrorR2(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temporary.open("wb") as stream:
        stream.write(_pretty_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _assert_committed_unchanged(study_root: Path, relative: str) -> None:
    path = study_root / relative
    committed = _git_bytes(study_root, "show", f"HEAD:{_study_git_path(study_root, relative)}")
    if not path.is_file() or path.read_bytes() != committed:
        raise ResourceAmendmentErrorR2(f"committed R2 anchor changed: {relative}")


def _source_symbol_hashes(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
    result: dict[str, str] = {}
    for index, node in enumerate(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            name = "assignment:" + ",".join(names) if names else f"assignment@{index}"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            name = f"import@{index}"
        else:
            name = f"statement@{index}"
        if name in result:
            raise ResourceAmendmentErrorR2(f"duplicate top-level symbol in {path.name}: {name}")
        result[name] = hashlib.sha256(
            ast.dump(node, include_attributes=False).encode("utf-8")
        ).hexdigest()
    return result


def _verify_source_scope(study_root: Path, source_commit: str,
                         source_hashes: Mapping[str, str],
                         symbol_hashes: Mapping[str, Mapping[str, str]]) -> None:
    if set(source_hashes) != set(R2_SOURCE_PATHS):
        raise ResourceAmendmentErrorR2("R2 source file set differs from the authorized narrow scope")
    _git(study_root, "merge-base", "--is-ancestor", source_commit, "HEAD")
    study_git_path = _study_git_path(study_root, ".")
    diff = _git(
        study_root, "diff", "--name-status", "--no-renames",
        PARENT_HANDOFF, source_commit, "--", study_git_path,
    )
    observed = {}
    for line in diff.splitlines():
        status, path = line.split("\t", 1)
        observed[path.removeprefix(study_git_path + "/")] = status
    if observed != {path: "A" for path in R2_SOURCE_PATHS}:
        raise ResourceAmendmentErrorR2(f"R2 source scope differs from authorization: {observed}")
    for relative in R2_SOURCE_PATHS:
        path = study_root / relative
        if not path.is_file() or sha256_bytes(path.read_bytes()) != source_hashes.get(relative):
            raise ResourceAmendmentErrorR2(f"R2 source hash mismatch: {relative}")
        if _source_symbol_hashes(path) != dict(symbol_hashes.get(relative, {})):
            raise ResourceAmendmentErrorR2(f"R2 changed-symbol hashes mismatch: {relative}")
        committed = _git_bytes(study_root, "show", f"{source_commit}:{_study_git_path(study_root, relative)}")
        if sha256_bytes(committed) != source_hashes[relative]:
            raise ResourceAmendmentErrorR2(f"R2 producer commit differs from source hash: {relative}")


def upper_mean_plus_three_se(values: Sequence[float]) -> dict[str, float | int]:
    if not values:
        raise ValueError("at least one complete game cost is required")
    numbers = [float(value) for value in values]
    if any(not math.isfinite(value) or value < 0 for value in numbers):
        raise ValueError("game costs must be finite and nonnegative")
    mean = math.fsum(numbers) / len(numbers)
    sample_sd = statistics.stdev(numbers) if len(numbers) > 1 else 0.0
    return {
        "n": len(numbers), "mean": mean, "sample_sd": sample_sd,
        "upper": mean + 3.0 * sample_sd / math.sqrt(len(numbers)),
    }


def _declared_order(row: Mapping[str, Any]) -> tuple[int, ...]:
    stage = row.get("stage_key")
    seed = int(row.get("game_seed"))
    if stage == "preflight":
        return (0, seed)
    if isinstance(stage, str) and stage.startswith("block1:batch-"):
        return (1, 0, int(stage.rsplit("-", 1)[1]), seed)
    if isinstance(stage, str) and stage.startswith("block2:batch-"):
        return (1, 1, int(stage.rsplit("-", 1)[1]), seed)
    if isinstance(stage, str) and stage.startswith("owner-"):
        try:
            arm = ARM_ORDER.index(str(row.get("arm")))
        except ValueError as exc:
            raise ResourceAmendmentErrorR2(f"unknown evaluation arm: {row.get('arm')}") from exc
        return (2, int(stage.split("-", 1)[1]), arm)
    raise ResourceAmendmentErrorR2(f"completed game is outside the declared grid: {stage!r}")


def complete_game_costs(study_root: Path) -> list[dict[str, Any]]:
    """Return one COMPLETE attempt per game key, in declared execution order."""
    root = study_root / "evidence" / "runs"
    candidates: dict[tuple[str, int, str], dict[str, Any]] = {}
    for path in sorted(root.rglob("run.json")) if root.exists() else ():
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if row.get("status") != "COMPLETE":
            continue
        try:
            key = (str(row["stage_key"]), int(row["game_seed"]), str(row["arm"]))
            cost = float(row["process_cpu_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ResourceAmendmentErrorR2(f"complete run lacks cost identity: {path}") from exc
        if not math.isfinite(cost) or cost < 0:
            raise ResourceAmendmentErrorR2(f"complete run has invalid process CPU: {path}")
        _declared_order(row)
        # The unchanged runner selects the lexically first successful attempt.
        candidates.setdefault(key, {
            "stage_key": key[0], "game_seed": key[1], "arm": key[2],
            "process_cpu_seconds": cost,
            "attempt_path": path.parent.relative_to(study_root).as_posix(),
            "run_json_sha256": sha256_bytes(path.read_bytes()),
        })
    return sorted(candidates.values(), key=_declared_order)


def _completed_summaries(study_root: Path, stage: str) -> list[dict[str, Any]]:
    root = study_root / "evidence" / "runs" / stage
    rows = []
    for path in sorted(root.rglob("run.json")) if root.exists() else ():
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if row.get("status") == "COMPLETE":
            rows.append(row)
    return rows


def remaining_optimizer_projection(study_root: Path) -> dict[str, Any]:
    """Mirror the unchanged runner's max-choice optimizer forecast exactly."""
    old_gate = _read_json(study_root / "evidence" / "preflight" / "native.json")
    measured = float(old_gate["optimizer_projection"]["measured_full_256_row_minibatch_cpu_seconds"])
    max_choices = max((
        int(row.get("supported_choices", 0))
        for stage in ("preflight", "training")
        for row in _completed_summaries(study_root, stage)
    ), default=0)
    rows_per_batch = math.ceil(max_choices * 1.5) * 128
    minibatches_per_epoch = math.ceil(rows_per_batch / 256)
    completed_batches = 0
    for path in (study_root / "evidence" / "training").glob("block*/batch-*.json"):
        try:
            json.loads(path.read_text(encoding="utf-8"))
            completed_batches += 1
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    remaining_batches = max(0, 16 - completed_batches)
    minibatches = remaining_batches * 4 * minibatches_per_epoch
    return {
        "max_supported_choices": max_choices,
        "projected_decisions_per_batch": rows_per_batch,
        "projected_minibatches_per_epoch": minibatches_per_epoch,
        "completed_batches": completed_batches,
        "remaining_batches": remaining_batches,
        "remaining_optimizer_minibatches": minibatches,
        "measured_full_256_row_minibatch_cpu_seconds": measured,
        "optimizer_cost_multiplier": OPTIMIZER_COST_MULTIPLIER,
        "remaining_optimizer_cpu_seconds": minibatches * measured * OPTIMIZER_COST_MULTIPLIER,
    }


def _ledger_total(study_root: Path) -> float:
    path = study_root / CPU_LEDGER
    if not path.exists():
        return 0.0
    total = 0.0
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            amount = float(json.loads(line).get("process_cpu_seconds", 0.0))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ResourceAmendmentErrorR2(f"invalid CPU ledger row {number}") from exc
        if not math.isfinite(amount) or amount < 0:
            raise ResourceAmendmentErrorR2(f"invalid CPU amount on ledger row {number}")
        total += amount
    return total


def _r1_anchor_hashes(study_root: Path) -> dict[str, str]:
    hashes = {}
    for relative in R1_ANCHORS:
        path = study_root / relative
        if not path.is_file():
            raise ResourceAmendmentErrorR2(f"retained R1 artifact is missing: {relative}")
        current = sha256_bytes(path.read_bytes())
        committed = sha256_bytes(_git_bytes(
            study_root, "show", f"{PARENT_HANDOFF}:{_study_git_path(study_root, relative)}"
        ))
        if current != committed:
            raise ResourceAmendmentErrorR2(f"R1 artifact changed after the reviewed R1 handoff: {relative}")
        hashes[relative] = current
    return hashes


def _verify_r1(study_root: Path) -> dict[str, Any]:
    try:
        result = verify_resource_chain(study_root, require_gate_seal=True)
    except (ResourceAmendmentError, OSError, ValueError, KeyError) as exc:
        raise ResourceAmendmentErrorR2(f"sealed R1 history no longer verifies: {exc}") from exc
    if result.get("status") != "PASS":
        raise ResourceAmendmentErrorR2("R1 history did not pass verification")
    return result


def _source_provenance(study_root: Path) -> tuple[str, dict[str, str], dict[str, dict[str, str]]]:
    source_commit = _git(study_root, "rev-parse", "HEAD")
    hashes = {relative: sha256_bytes((study_root / relative).read_bytes()) for relative in R2_SOURCE_PATHS}
    symbols = {relative: _source_symbol_hashes(study_root / relative) for relative in R2_SOURCE_PATHS}
    _verify_source_scope(study_root, source_commit, hashes, symbols)
    return source_commit, hashes, symbols


def build_forecast(*, costs: Sequence[float], remaining_games: int,
                   remaining_optimizer_cpu: float, accounted_cpu: float,
                   builder_budget: float = BUILDER_BUDGET_SECONDS,
                   reviewer_reserve: float = REVIEWER_RESERVE_SECONDS,
                   process_cap: float = PROCESS_CPU_CAP_SECONDS) -> dict[str, Any]:
    all_history = upper_mean_plus_three_se(costs)
    recent_costs = list(costs[-RECENT_WINDOW:])
    recent = upper_mean_plus_three_se(recent_costs)
    unit = max(float(all_history["upper"]), float(recent["upper"]))
    maximum = max(float(value) for value in costs)
    game_before_margin = remaining_games * unit * GAME_COST_MULTIPLIER
    game_forecast = game_before_margin * (1.0 + FORECAST_HEADROOM)
    optimizer_forecast = remaining_optimizer_cpu * (1.0 + OPTIMIZER_HEADROOM)
    remaining_forecast = game_forecast + optimizer_forecast
    builder_total = accounted_cpu + remaining_forecast
    all_in_total = builder_total + reviewer_reserve
    return {
        "cost_rule": "max(all-history and recent-128 mean + 3 sample-SD/sqrt(n))",
        "interpretation": "fixed operational heuristic; not a calibrated confidence bound or cost guarantee",
        "observed_complete_games": len(costs),
        "all_history": all_history,
        "recent_window_size": RECENT_WINDOW,
        "recent_observed_games": len(recent_costs),
        "recent_window": recent,
        "unit_cost_cpu_seconds": unit,
        "max_observed_game_cpu_seconds_stress_only": maximum,
        "remaining_games": remaining_games,
        "game_cost_multiplier": GAME_COST_MULTIPLIER,
        "projected_remaining_game_cpu_seconds_before_headroom": game_before_margin,
        "projected_remaining_game_cpu_seconds_with_headroom": game_forecast,
        "remaining_optimizer_cpu_seconds_before_headroom": remaining_optimizer_cpu,
        "optimizer_headroom_fraction": OPTIMIZER_HEADROOM,
        "projected_remaining_optimizer_cpu_seconds_with_headroom": optimizer_forecast,
        "projected_remaining_cpu_seconds": remaining_forecast,
        "accounted_cpu_seconds": accounted_cpu,
        "projected_builder_cpu_seconds": builder_total,
        "builder_budget_cpu_seconds": builder_budget,
        "reviewer_reserve_cpu_seconds": reviewer_reserve,
        "projected_all_in_cpu_seconds": all_in_total,
        "process_cpu_cap_seconds": process_cap,
        "max_based_stress_game_projection_cpu_seconds":
            remaining_games * maximum * GAME_COST_MULTIPLIER * (1.0 + FORECAST_HEADROOM),
        "status": "PASS" if builder_total <= builder_budget and all_in_total <= process_cap else "RESOURCE_BLOCKED",
    }


def make_resource_gate(study_root: Path, *, stage_started_cpu: float) -> dict[str, Any]:
    r1 = _verify_r1(study_root)
    anchors = _r1_anchor_hashes(study_root)
    source_commit, source_hashes, symbols = _source_provenance(study_root)
    config_hash = sha256_bytes((study_root / CONFIG_PATH).read_bytes())
    if config_hash != "07020535931cd81bef9b40fa1c30e75b553ac160bc53057e58f82bd8cf2007ca":
        raise ResourceAmendmentErrorR2("scientific config changed under the R2 resource-only authorization")
    observations = complete_game_costs(study_root)
    preflight = [row for row in observations if row["stage_key"] == "preflight"]
    if len(preflight) != 8:
        raise ResourceAmendmentErrorR2(f"expected 8 retained preflight games, found {len(preflight)}")
    formal_complete = sum(row["stage_key"] != "preflight" for row in observations)
    if formal_complete > FORMAL_GAME_COUNT:
        raise ResourceAmendmentErrorR2("completed games exceed the fixed grid")
    optimizer = remaining_optimizer_projection(study_root)
    stage_cpu = time.process_time() - stage_started_cpu
    ledger_total = _ledger_total(study_root)
    accounted = STATIC_ALLOWANCE_SECONDS + ledger_total + stage_cpu + GATE_FINALIZATION_MARGIN_SECONDS
    forecast = build_forecast(
        costs=[float(row["process_cpu_seconds"]) for row in observations],
        remaining_games=FORMAL_GAME_COUNT - formal_complete,
        remaining_optimizer_cpu=float(optimizer["remaining_optimizer_cpu_seconds"]),
        accounted_cpu=accounted,
    )
    return {
        "schema_id": "issue60-resource-amendment-r2-v1",
        "amendment_id": "R2", "issue": ISSUE, "status": forecast["status"],
        "decision": "continue the unchanged fixed grid" if forecast["status"] == "PASS" else "stop and return to Planner",
        "parent_handoff_commit": PARENT_HANDOFF,
        "planner_decision": {"comment_id": PLANNER_DECISION_ID, "url": PLANNER_DECISION_URL},
        "implementation_source_commit": source_commit,
        "implementation_source_files": source_hashes,
        "implementation_changed_symbol_sha256": symbols,
        "config_sha256": config_hash,
        "r1_resource_chain_status": r1["status"],
        "retained_r1_artifact_sha256": anchors,
        "r1_gate_sha256": r1["gate_seal"]["gate_sha256"],
        "r1_seal_sha256": r1["gate_seal"]["seal_sha256"],
        "r1_cpu_ledger_prefix": {
            "line_count": r1["gate_seal"]["seal"]["cpu_ledger_prefix_line_count"],
            "sha256": r1["gate_seal"]["seal"]["cpu_ledger_prefix_sha256"],
        },
        "execution_cost_inputs": observations,
        "optimizer_projection": optimizer,
        "forecast": forecast,
        "accounting": {
            "static_and_implementation_allowance_cpu_seconds": STATIC_ALLOWANCE_SECONDS,
            "previous_cpu_ledger_cpu_seconds": ledger_total,
            "resource_stage_cpu_seconds_at_gate_write": stage_cpu,
            "gate_finalization_margin_cpu_seconds": GATE_FINALIZATION_MARGIN_SECONDS,
        },
    }


def write_resource_gate(study_root: Path, *, stage_started_cpu: float) -> dict[str, Any]:
    gate = make_resource_gate(study_root, stage_started_cpu=stage_started_cpu)
    _write_json(study_root / R2_RECEIPT, {"gate": gate, "seal": None})
    return gate


def verify_r2_receipt(study_root: Path, *, require_seal: bool) -> dict[str, Any]:
    _verify_r1(study_root)
    path = study_root / R2_RECEIPT
    _assert_committed_unchanged(study_root, R2_RECEIPT)
    document = _read_json(path)
    gate = document.get("gate")
    if not isinstance(gate, Mapping) or gate.get("schema_id") != "issue60-resource-amendment-r2-v1":
        raise ResourceAmendmentErrorR2("R2 resource receipt schema is invalid")
    if document.get("seal") is not None and not isinstance(document.get("seal"), Mapping):
        raise ResourceAmendmentErrorR2("R2 receipt seal is malformed")
    expected_auth = {"comment_id": PLANNER_DECISION_ID, "url": PLANNER_DECISION_URL}
    if (gate.get("issue") != ISSUE or gate.get("amendment_id") != "R2"
            or gate.get("parent_handoff_commit") != PARENT_HANDOFF
            or gate.get("planner_decision") != expected_auth):
        raise ResourceAmendmentErrorR2("R2 receipt is not bound to Planner authorization")
    if gate.get("status") != "PASS":
        raise ResourceAmendmentErrorR2("R2 forecast does not authorize continuation")
    if gate.get("config_sha256") != sha256_bytes((study_root / CONFIG_PATH).read_bytes()):
        raise ResourceAmendmentErrorR2("R2 receipt config hash changed")
    _verify_source_scope(
        study_root, str(gate.get("implementation_source_commit", "")),
        gate.get("implementation_source_files", {}),
        gate.get("implementation_changed_symbol_sha256", {}),
    )
    if _r1_anchor_hashes(study_root) != dict(gate.get("retained_r1_artifact_sha256", {})):
        raise ResourceAmendmentErrorR2("retained R1 bytes no longer match the R2 receipt")
    seal = document.get("seal")
    if require_seal and not isinstance(seal, Mapping):
        raise ResourceAmendmentErrorR2("R2 forecast has not been sealed")
    if isinstance(seal, Mapping):
        if seal.get("status") != "PASS":
            raise ResourceAmendmentErrorR2("R2 receipt seal did not pass")
        preseal = {"gate": gate, "seal": None}
        if seal.get("preseal_receipt_sha256") != sha256_bytes(_pretty_bytes(preseal)):
            raise ResourceAmendmentErrorR2("R2 seal does not bind the pre-seal gate")
        lines = (study_root / CPU_LEDGER).read_bytes().splitlines(keepends=True)
        count = int(seal.get("cpu_ledger_prefix_line_count", -1))
        prefix = b"".join(lines[:count])
        if count < 0 or len(lines) < count or sha256_bytes(prefix) != seal.get("cpu_ledger_prefix_sha256"):
            raise ResourceAmendmentErrorR2("CPU ledger changed within the sealed R2 prefix")
        if seal.get("r1_cpu_ledger_prefix") != gate.get("r1_cpu_ledger_prefix"):
            raise ResourceAmendmentErrorR2("R2 seal changed the retained R1 ledger prefix")
    return dict(document)


def seal_resource_gate(study_root: Path, *, stage_started_cpu: float) -> dict[str, Any]:
    _verify_r1(study_root)
    path = study_root / R2_RECEIPT
    _assert_committed_unchanged(study_root, R2_RECEIPT)
    document = _read_json(path)
    gate = document.get("gate")
    if not isinstance(gate, Mapping) or gate.get("status") != "PASS" or document.get("seal") is not None:
        raise ResourceAmendmentErrorR2("only an unsealed passing R2 forecast can be sealed")
    _verify_source_scope(
        study_root, str(gate["implementation_source_commit"]),
        gate["implementation_source_files"], gate["implementation_changed_symbol_sha256"],
    )
    ledger_bytes = (study_root / CPU_LEDGER).read_bytes()
    seal = {
        "schema_id": "issue60-resource-gate-seal-r2-v1", "status": "PASS",
        "preseal_receipt_sha256": sha256_bytes(_pretty_bytes(document)),
        "r2_implementation_source_commit": gate["implementation_source_commit"],
        "parent_handoff_commit": PARENT_HANDOFF,
        "planner_decision_url": PLANNER_DECISION_URL,
        "builder_budget_cpu_seconds": BUILDER_BUDGET_SECONDS,
        "reviewer_reserve_cpu_seconds": REVIEWER_RESERVE_SECONDS,
        "process_cpu_cap_seconds": PROCESS_CPU_CAP_SECONDS,
        "cpu_ledger_prefix_line_count": len(ledger_bytes.splitlines(keepends=True)),
        "cpu_ledger_prefix_sha256": sha256_bytes(ledger_bytes),
        "r1_cpu_ledger_prefix": gate["r1_cpu_ledger_prefix"],
        "seal_stage_cpu_at_write_seconds": time.process_time() - stage_started_cpu,
        "seal_finalization_margin_cpu_seconds": GATE_FINALIZATION_MARGIN_SECONDS,
        "formal_grid_fixed": True, "model_or_training_settings_changed": False,
    }
    _write_json(path, {"gate": gate, "seal": seal})
    return seal


def current_accounted_cpu(study_root: Path, current_stage_cpu: float = 0.0) -> float:
    return STATIC_ALLOWANCE_SECONDS + _ledger_total(study_root) + max(0.0, current_stage_cpu)


def enforce_budget(*, accounted_cpu: float, current_stage_cpu: float,
                   costs: Sequence[float], remaining_games: int,
                   remaining_optimizer_cpu: float) -> dict[str, Any]:
    actual = accounted_cpu + max(0.0, current_stage_cpu)
    if actual >= BUILDER_BUDGET_SECONDS:
        raise ResourceBudgetExceeded(
            f"RESOURCE_BLOCKED: actual Builder CPU {actual:.3f}s reached {BUILDER_BUDGET_SECONDS:.0f}s"
        )
    forecast = build_forecast(
        costs=costs, remaining_games=remaining_games,
        remaining_optimizer_cpu=remaining_optimizer_cpu, accounted_cpu=actual,
    )
    if forecast["status"] != "PASS":
        raise ResourceBudgetExceeded(
            f"RESOURCE_BLOCKED: revised forecast projects {forecast['projected_builder_cpu_seconds']:.3f}s "
            f"Builder CPU against {BUILDER_BUDGET_SECONDS:.0f}s"
        )
    return forecast


def wrap_before_decision(callback: Callable[..., Any], guard: Callable[[], Any]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        guard()
        return callback(*args, **kwargs)
    return wrapped


def wrap_minibatch_loss(loss_function: Callable[..., Any], guard: Callable[[], Any],
                        on_complete: Callable[[], Any]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        guard()
        result = loss_function(*args, **kwargs)
        on_complete()
        return result
    return wrapped


def cpu_limit_values(current_process_cpu: float, remaining_cpu: float,
                     *, signal_grace: int = CPU_SIGNAL_GRACE_SECONDS) -> tuple[int, int]:
    if not math.isfinite(current_process_cpu) or not math.isfinite(remaining_cpu) or remaining_cpu <= 1:
        raise ResourceBudgetExceeded("RESOURCE_BLOCKED: insufficient CPU allowance to launch a stage")
    hard = math.floor(current_process_cpu + remaining_cpu)
    soft = max(math.floor(current_process_cpu) + 1, hard - signal_grace)
    if hard <= current_process_cpu:
        raise ResourceBudgetExceeded("RESOURCE_BLOCKED: remaining CPU rounds below current process usage")
    soft = min(soft, hard - 1)
    return soft, hard


def install_process_cpu_limit(*, current_process_cpu: float,
                              remaining_cpu: float) -> dict[str, Any]:
    """Install a POSIX process-CPU ceiling with a graceful soft-limit stop."""
    if os.name != "posix":
        raise ResourceAmendmentErrorR2("actual stage limiting requires POSIX RLIMIT_CPU; run in Linux/WSL")
    try:
        import resource
        soft, hard = cpu_limit_values(current_process_cpu, remaining_cpu)
        _, old_hard = resource.getrlimit(resource.RLIMIT_CPU)
        if old_hard != resource.RLIM_INFINITY:
            hard = min(hard, int(old_hard))
        if hard <= current_process_cpu:
            raise ResourceBudgetExceeded("RESOURCE_BLOCKED: existing process limit leaves no stage allowance")
        soft = min(max(math.floor(current_process_cpu) + 1, hard - CPU_SIGNAL_GRACE_SECONDS), hard - 1)

        def on_cpu_limit(signum: int, frame: Any) -> None:
            del signum, frame
            resource.setrlimit(resource.RLIMIT_CPU, (hard, hard))
            raise ResourceBudgetExceeded("RESOURCE_BLOCKED: RLIMIT_CPU soft limit reached")

        signal.signal(signal.SIGXCPU, on_cpu_limit)
        resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))
        return {
            "mechanism": "RLIMIT_CPU",
            "soft_limit_process_cpu_seconds": soft,
            "hard_limit_process_cpu_seconds": hard,
            "soft_signal_grace_seconds": hard - soft,
        }
    except ResourceBudgetExceeded:
        raise
    except (ImportError, AttributeError, OSError, ValueError) as exc:
        raise ResourceAmendmentErrorR2(f"could not install CPU hard limit: {exc}") from exc


def write_budget_stop(study_root: Path, *, stage: str, message: str,
                      process_cpu_seconds: float) -> dict[str, Any]:
    observations = complete_game_costs(study_root)
    result = {
        "schema_id": "issue60-resource-stop-r2-v1", "status": "RESOURCE_BLOCKED",
        "stage": stage, "message": message, "process_cpu_seconds_at_stop": process_cpu_seconds,
        "accounted_cpu_seconds_at_stop": current_accounted_cpu(study_root, process_cpu_seconds),
        "completed_game_count": len(observations),
        "completed_game_costs_sha256": sha256_bytes(_canonical_bytes(observations)),
        "parent_handoff_commit": PARENT_HANDOFF,
        "planner_decision_url": PLANNER_DECISION_URL,
    }
    _write_json(study_root / R2_STOP_RECEIPT, result)
    return result
