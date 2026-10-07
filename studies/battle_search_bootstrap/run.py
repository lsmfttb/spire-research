from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SIMULATOR_COMMIT = "d61c7c2a120aa90730fd8530718a7cd7cd856b46"
INFORMATION_REGIME = "full_simulator_state_oracle_like"
ACTION_FIELDS = ("scope", "bits", "kind", "idx1", "idx2", "idx3", "label")
ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(__file__).with_name("config.json")
ARTIFACT_ROOT = ROOT / "artifacts" / "battle_search_bootstrap"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "value"):
        return _plain(value.value)
    if hasattr(value, "__int__"):
        return int(value)
    raise TypeError(f"unsupported simulator result value: {type(value).__name__}")


def _action_identity(action: Any) -> dict[str, Any]:
    source = action if isinstance(action, Mapping) else {
        field: getattr(action, field) for field in ACTION_FIELDS
    }
    return {field: _plain(source[field]) for field in ACTION_FIELDS}


def _record_phase(path: Path, phase: str, **details: Any) -> None:
    import os

    event = {"at_utc": _utc_now(), "phase": phase, **_plain(details)}
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_result(path: Path, result: Mapping[str, Any]) -> str:
    import os

    payload = json.dumps(
        result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    ).encode("utf-8") + b"\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    return hashlib.sha256(payload).hexdigest()


def _git_commit() -> str:
    import os

    supplied_commit = os.environ.get("SPIRE_RESEARCH_COMMIT")
    if supplied_commit is not None:
        if len(supplied_commit) != 40 or any(
            character not in "0123456789abcdefABCDEF" for character in supplied_commit
        ):
            raise RuntimeError("SPIRE_RESEARCH_COMMIT must be a full Git commit SHA")
        return supplied_commit.lower()

    status = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=normal"],
        check=True, capture_output=True, text=True,
    ).stdout
    if status.strip():
        raise RuntimeError("run from a clean spire-research commit so provenance is exact")
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _make_simulator(config: Mapping[str, Any]) -> Any:
    import slaythespire

    character = getattr(slaythespire.CharacterClass, config["character"])
    return slaythespire.StepSimulator(character, int(config["seed"]), int(config["ascension"]))


def _construct_battle(simulator: Any, config: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    entry = config["battle_entry"]
    max_steps = int(entry["max_steps"])
    if entry["selector"] != "first_legal_action":
        raise ValueError("unsupported battle-entry selector in bootstrap config")

    route_actions: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {}
    for step_index in range(max_steps + 1):
        snapshot = _plain(simulator.snapshot())
        if snapshot.get("battle_active") is True:
            break
        if step_index == max_steps:
            raise RuntimeError(f"no active battle after {max_steps} simulator steps")
        actions = simulator.legal_actions()
        if not actions:
            raise RuntimeError("simulator returned no legal action before the first battle")
        action = actions[0]
        route_actions.append(_action_identity(action))
        simulator.step(action)
    else:
        raise RuntimeError("simulator battle-entry loop ended unexpectedly")

    if snapshot.get("battle_active") is not True:
        raise RuntimeError("simulator did not expose an active battle")
    return snapshot, route_actions


def run_bootstrap(
    config_path: Path = CONFIG_PATH,
    output_dir: Path | None = None,
    simulator_factory: Callable[[Mapping[str, Any]], Any] | None = None,
    code_commit: str | None = None,
) -> dict[str, Any]:
    config_path = Path(config_path)
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    if config["simulator_commit"] != SIMULATOR_COMMIT:
        raise ValueError("config simulator commit does not match the pinned public API")
    if config["information_regime"] != INFORMATION_REGIME:
        raise ValueError("bootstrap information regime must be explicit and oracle-like")
    search_config = config["search"]
    if search_config["api"] != "battle_search_v2":
        raise ValueError("bootstrap must call native battle_search_v2")
    if search_config["policy_prior_callback"] is not None or search_config["leaf_value_callback"] is not None:
        raise ValueError("bootstrap Search-v2 call must be unguided")

    if code_commit is None:
        code_commit = _git_commit()
    started_utc = _utc_now()
    run_id = started_utc.replace("-", "").replace(":", "").replace(".", "")
    if output_dir is None:
        output_dir = ARTIFACT_ROOT / run_id
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / "progress.jsonl"
    progress_path.write_text("", encoding="utf-8")
    last_phase = "prepared"
    _record_phase(
        progress_path, "prepared", run_id=output_dir.name,
        simulator_commit=SIMULATOR_COMMIT,
        experiment_config_sha256=hashlib.sha256(config_bytes).hexdigest(),
    )

    try:
        simulator = (simulator_factory or _make_simulator)(config)
        _record_phase(progress_path, "simulator_ready")
        last_phase = "simulator_ready"

        battle_snapshot, route_actions = _construct_battle(simulator, config)
        legal_actions = [_action_identity(action) for action in simulator.legal_actions()]
        if not legal_actions:
            raise RuntimeError("simulator returned no legal actions in the battle")
        legal_surface_sha256 = hashlib.sha256(_canonical_json(legal_actions)).hexdigest()
        battle_snapshot_sha256 = hashlib.sha256(_canonical_json(battle_snapshot)).hexdigest()
        config_sha256 = hashlib.sha256(config_bytes).hexdigest()
        state_identity_payload = {
            "simulator_commit": SIMULATOR_COMMIT,
            "experiment_config_sha256": config_sha256,
            "entry_actions": route_actions,
            "battle_snapshot_sha256": battle_snapshot_sha256,
        }
        state_identity_sha256 = hashlib.sha256(_canonical_json(state_identity_payload)).hexdigest()
        _record_phase(
            progress_path, "input_qualified", battle_active=True,
            legal_action_count=len(legal_actions),
            legal_action_surface_sha256=legal_surface_sha256,
            state_identity_sha256=state_identity_sha256,
        )
        last_phase = "input_qualified"

        simulations = int(search_config["simulations"])
        include_potions = bool(search_config["include_potions"])
        _record_phase(progress_path, "search_entered", simulations=simulations)
        last_phase = "search_entered"
        search_started = time.perf_counter()
        raw_report = simulator.battle_search_v2(simulations, include_potions)
        runtime_seconds = time.perf_counter() - search_started
        _record_phase(
            progress_path, "search_returned", runtime_seconds=round(runtime_seconds, 6)
        )
        last_phase = "search_returned"

        report = _plain(raw_report)
        if report.get("information_regime") != INFORMATION_REGIME:
            raise RuntimeError("native Search-v2 report information regime did not match")
        if report.get("simulations_requested") != simulations:
            raise RuntimeError("native Search-v2 report did not confirm the configured budget")
        root_rows = report.get("root_rows")
        if not isinstance(root_rows, list) or len(root_rows) != len(legal_actions):
            raise RuntimeError("Search-v2 root rows did not match the simulator legal-action count")
        root_identities = [_action_identity(row) for row in root_rows]
        if root_identities != legal_actions:
            raise RuntimeError("Search-v2 root actions did not match simulator legal_actions")

        visits = [int(row["visits"]) for row in root_rows]
        if not visits or max(visits) <= 0:
            raise RuntimeError("Search-v2 returned no visited legal root action")
        selected_ordinal = max(range(len(visits)), key=lambda index: (visits[index], -index))
        selected_row = root_rows[selected_ordinal]
        search_root_rows = [
            {
                "legal_action_ordinal": index,
                "action": legal_actions[index],
                "search_tree_present": bool(row["search_tree_present"]),
                "search_edge_index": row["search_edge_index"],
                "visits": int(row["visits"]),
                "mean_value": row["mean_value"],
            }
            for index, row in enumerate(root_rows)
        ]
        metric_fields = (
            "simulations_requested", "root_visits", "native_simulator_steps",
            "model_calls", "root_row_count", "search_edge_count",
            "unsearched_legal_action_count", "unmapped_search_edge_count",
        )
        search_metrics = {field: report.get(field) for field in metric_fields}
        search_metrics["runtime_seconds"] = round(runtime_seconds, 6)

        result = {
            "result_schema": "battle-search-bootstrap-v1",
            "run_id": output_dir.name,
            "created_at_utc": _utc_now(),
            "information_regime": INFORMATION_REGIME,
            "deployable_normal_public_controller": False,
            "provenance": {
                "spire_research_commit": code_commit,
                "sts_lightspeed_commit": SIMULATOR_COMMIT,
                "experiment_config_sha256": config_sha256,
                "experiment_config": config,
            },
            "state_identity": {
                "character": config["character"],
                "ascension": int(config["ascension"]),
                "seed": int(config["seed"]),
                "construction": "seeded StepSimulator start; first simulator legal action until battle_active",
                "entry_actions": route_actions,
                "battle_snapshot_sha256": battle_snapshot_sha256,
                "identity_sha256": state_identity_sha256,
                "battle_screen_state": battle_snapshot.get("screen_state"),
            },
            "legal_action_surface": {
                "sha256": legal_surface_sha256,
                "actions": legal_actions,
            },
            "search": {
                "native_api": report.get("native_api"),
                "native_information_regime": report["information_regime"],
                "selection_rule": "maximum root visits; ties preserve simulator legal-action order",
                "metrics": search_metrics,
                "root_rows": search_root_rows,
                "selected_action": {
                    "legal_action_ordinal": selected_ordinal,
                    "action": legal_actions[selected_ordinal],
                    "visits": int(selected_row["visits"]),
                    "mean_value": selected_row["mean_value"],
                },
            },
        }
        result_path = output_dir / "result.json"
        result_sha256 = _write_result(result_path, result)
        _record_phase(
            progress_path, "result_written", result_file=result_path.name,
            result_sha256=result_sha256,
        )
        return result
    except Exception as error:
        _record_phase(
            progress_path, "failed", last_completed_phase=last_phase,
            error_type=type(error).__name__, error=str(error),
        )
        raise


def main() -> None:
    result = run_bootstrap()
    run_dir = ARTIFACT_ROOT / result["run_id"]
    print(json.dumps({
        "result": str((run_dir / "result.json").resolve()),
        "progress": str((run_dir / "progress.jsonl").resolve()),
        "selected_action": result["search"]["selected_action"],
        "metrics": result["search"]["metrics"],
    }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()