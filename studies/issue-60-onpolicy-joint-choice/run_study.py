"""Resumable, budget-gated Issue 60 training and evaluation runner.

All game advancement is delegated to the pinned STSRL controlled-run
executor.  This file contains study orchestration and evidence writing only.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import sys
import time
from typing import Any, Mapping, Sequence

import torch

from bootstrap_v47 import Scorer
from controllers import (
    FrozenBattleSearch, PublicChoicePolicy, load_bootstrap, routed_controller,
)
from encoder import Vocabulary
from model import JointChoiceActorCritic
from preflight_witness import run_witnesses
from public_resources import RESOURCE_FIELDS, boundary_resources
from trainer import DecisionRow, IncompleteRun, derive_seed, update_batch


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
EVIDENCE = HERE / "evidence"
FROZEN_FILES = (
    "adapter_v47.py", "bootstrap_v47.py", "bootstrap/A.json", "config.json",
    "controllers.py", "encoder.py", "freeze_manifest.py", "model.py",
    "normalizer_v49.py", "preflight_witness.py", "public_resources.py",
    "run_study.py", "trainer.py",
    "vocabulary.json",
)
TRAINING_SEEDS = {
    "block1": range(660001, 661025),
    "block2": range(662001, 663025),
}
MODEL_SEEDS = {"block1": 660101, "block2": 660102}
PRE_FLIGHT_SEEDS = tuple(range(668001, 668009))
EVAL_SEEDS = tuple(range(669001, 669129))
EVAL_ARMS = ("final_block1", "final_block2", "p0", "A", "Expert")
EXPERT_SEED = 660099
MAX_STEPS = 20000
BUILD_AND_STATIC_ALLOWANCE = 600.0
REVIEWER_RESERVE = 20000.0
PROCESS_BUDGET = 200000.0
BUILDER_BUDGET = PROCESS_BUDGET - REVIEWER_RESERVE
POST_PREFLIGHT_GAMES = 2048 + 640


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def config_sha() -> str:
    return sha256_bytes(CONFIG_PATH.read_bytes())


def verify_frozen_manifest() -> dict[str, Any]:
    path = HERE / "frozen-manifest.json"
    if not path.is_file():
        raise RuntimeError("frozen study manifest is missing; no simulator run is authorized")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_id") != "issue60-frozen-manifest-v1":
        raise RuntimeError("frozen study manifest has an unexpected schema")
    if manifest.get("config_sha256") != config_sha():
        raise RuntimeError("current config differs from the frozen preflight config")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if (manifest.get("source_pins") != config.get("sources")
            or manifest.get("base_commit") != config.get("base", {}).get("commit")):
        raise RuntimeError("frozen source/base pins disagree with the registered config")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise RuntimeError("frozen study manifest has no file-hash list")
    paths = [entry.get("path") for entry in entries if isinstance(entry, Mapping)]
    if len(paths) != len(entries) or set(paths) != set(FROZEN_FILES) or len(paths) != len(set(paths)):
        raise RuntimeError("frozen study manifest file list does not match the fixed implementation")
    for entry in manifest.get("files", []):
        relative = entry.get("path")
        if (not isinstance(relative, str) or Path(relative).is_absolute()
                or ".." in Path(relative).parts):
            raise RuntimeError("frozen manifest contains an unsafe path")
        source = HERE / relative
        if not source.is_file() or sha256_bytes(source.read_bytes()) != entry.get("sha256"):
            raise RuntimeError(f"frozen source file changed or is missing: {relative}")
    return manifest


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


class JsonlWriter:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.stream = path.open("w", encoding="utf-8", newline="\n")
        self.rows = 0

    def __call__(self, row: Mapping[str, Any]) -> None:
        self.stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        self.rows += 1
        if self.rows % 16 == 0:
            self.stream.flush()
            os.fsync(self.stream.fileno())

    def close(self) -> None:
        if not self.stream.closed:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".gz":
        opener = gzip.open
    else:
        opener = open
    with opener(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def compress_jsonl(path: Path) -> Path:
    compressed = path.with_suffix(path.suffix + ".gz")
    temp = compressed.with_name(compressed.name + f".tmp-{os.getpid()}")
    with path.open("rb") as source, gzip.open(temp, "wb", compresslevel=6) as target:
        shutil.copyfileobj(source, target)
    os.replace(temp, compressed)
    path.unlink()
    return compressed


def _repo_head(path: str) -> str:
    import subprocess
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def verify_runtime(config: Mapping[str, Any]) -> dict[str, Any]:
    runtime = config["runtime"]
    if list(sys.version_info[:3]) != [3, 13, 13]:
        raise RuntimeError(f"expected Python 3.13.13, received {sys.version.split()[0]}")
    if torch.__version__ != runtime["torch"]:
        raise RuntimeError(f"expected torch {runtime['torch']}, received {torch.__version__}")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    if torch.get_num_threads() != 1 or not torch.are_deterministic_algorithms_enabled():
        raise RuntimeError("CPU deterministic runtime could not be fixed")
    stsr = os.environ.get("ISSUE60_STSRL_ROOT", "/tmp/issue60-stsrl")
    native = os.environ.get("ISSUE60_NATIVE_ROOT", "/tmp/issue60-lightspeed")
    if _repo_head(stsr) != config["sources"]["stsrl_commit"]:
        raise RuntimeError("STSRL source checkout does not match the pinned commit")
    if _repo_head(native) != config["sources"]["native_commit"]:
        raise RuntimeError("native source checkout does not match the pinned commit")
    import slaythespire
    module_path = Path(slaythespire.__file__).resolve()
    binary_hash = sha256_bytes(module_path.read_bytes())
    if binary_hash != config["sources"]["native_binary_sha256"]:
        raise RuntimeError("loaded native simulator binary SHA-256 differs from the pinned build")
    from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
    from sts_combat_rl.sim.controlled_run import execute_controlled_run
    from sts_combat_rl.sim.action_space import ActionSpaceConfig
    return {
        "python": sys.version.split()[0], "torch": torch.__version__,
        "torch_threads": torch.get_num_threads(),
        "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
        "stsr_source": stsr, "stsr_commit": _repo_head(stsr),
        "native_source": native, "native_commit": _repo_head(native),
        "native_module": str(module_path), "native_binary_sha256": binary_hash,
        "runtime_api": {
            "LightSpeedAdapter": LightSpeedAdapter.__module__,
            "execute_controlled_run": execute_controlled_run.__module__,
            "action_space": ActionSpaceConfig.include_all().to_dict(),
        },
    }


def model_fingerprint(model: torch.nn.Module, vocab: Vocabulary, config_digest: str) -> str:
    digest = hashlib.sha256()
    digest.update(vocab.sha256.encode("ascii"))
    digest.update(config_digest.encode("ascii"))
    for name, value in sorted(model.state_dict().items()):
        digest.update(name.encode("utf-8"))
        tensor = value.detach().cpu().contiguous().view(torch.uint8).flatten()
        digest.update(bytes(tensor.tolist()))
    return digest.hexdigest()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _available(value: Any) -> Any:
    if isinstance(value, Mapping) and value.get("availability") == "available":
        return value.get("value")
    return None


def _terminal_floor(run: Any) -> float | None:
    location = run.public_run_context.get("current", {}).get("location", {})
    value = location.get("floor") if isinstance(location, Mapping) else None
    if isinstance(value, Mapping):
        value = _available(value)
    return _number(value)


def _public_location(run: Any) -> dict[str, Any]:
    location = run.public_run_context.get("current", {}).get("location", {})
    if not isinstance(location, Mapping):
        return {}
    result: dict[str, Any] = {}
    for name in ("act", "floor", "room_type"):
        value = _available(location.get(name))
        if value is not None and isinstance(value, (str, int, float)) and not isinstance(value, bool):
            result[name] = value
    return result


def _boundary_record(step: Any, *, boundary: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "boundary": boundary,
        "screen": raw.get("screen_state") if isinstance(raw.get("screen_state"), str) else None,
        "act": _number(raw.get("act")),
        "floor": _number(raw.get("floor_num", raw.get("floor"))),
        "room_type": raw.get("room_type") if isinstance(raw.get("room_type"), str) else None,
        # after_transition observes the post-step state for every boundary.
        "player_hp": step.next_player_hp,
        "player_max_hp": step.next_player_max_hp,
        "gold": step.next_gold,
        "potion_count": step.next_potion_count,
        "battle_outcome": step.next_battle_outcome,
    }


def _source_controller(*, vocabulary: Vocabulary, bootstrap: Scorer,
                       model: JointChoiceActorCritic | None, mode: str,
                       runtime_seed: int, block_seed: int | None,
                       provenance_config: Mapping[str, Any],
                       event_sink: JsonlWriter,
                       decision_sink: Callable[[Mapping[str, Any]], None],
                       require_targets: bool) -> tuple[Any, PublicChoicePolicy, FrozenBattleSearch]:
    from sts_combat_rl.sim.online_controller import PolicyController
    from sts_combat_rl.sim.non_combat_policy import ExpertNonCombatDriver

    fallback = PolicyController(ExpertNonCombatDriver(seed=EXPERT_SEED))
    public = PublicChoicePolicy(
        vocabulary=vocabulary, bootstrap=bootstrap, model=model, mode=mode,
        rng_seed=runtime_seed, block_seed=block_seed, fallback=fallback,
        decision_sink=decision_sink, require_targets=require_targets,
        provenance_config=provenance_config,
    )
    controller, battle = routed_controller(noncombat=public, event_sink=event_sink)
    return controller, public, battle


def _expert_controller(event_sink: JsonlWriter) -> tuple[Any, FrozenBattleSearch]:
    from sts_combat_rl.sim.online_controller import PolicyController
    from sts_combat_rl.sim.non_combat_policy import ExpertNonCombatDriver
    from sts_combat_rl.sim.online_controller import RoutedRunController

    noncombat = PolicyController(ExpertNonCombatDriver(seed=EXPERT_SEED))
    battle = FrozenBattleSearch(event_sink)
    return RoutedRunController(battle=battle, non_combat=noncombat), battle


def _next_attempt(directory: Path) -> int:
    attempts = [
        int(path.name.split("-")[-1])
        for path in directory.glob("attempt-*")
        if path.name.split("-")[-1].isdigit()
    ]
    return max(attempts, default=0) + 1


def successful_attempt(directory: Path, expected_model_sha: str,
                       expected_config_sha: str) -> tuple[Path, dict[str, Any]] | None:
    for path in sorted(directory.glob("attempt-*/run.json")):
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (summary.get("status") == "COMPLETE"
                and summary.get("model_sha256") == expected_model_sha
                and summary.get("config_sha256") == expected_config_sha
                and (path.parent / "decisions.jsonl.gz").is_file()):
            return path.parent, summary
    return None


def run_game(*, seed: int, mode: str, model: JointChoiceActorCritic | None,
             block_name: str, runtime_seed: int, output_dir: Path,
             model_sha: str, config_digest: str, vocab: Vocabulary,
             bootstrap: Scorer, require_targets: bool,
             provenance_config: Mapping[str, Any],
             stage_key: str, arm: str) -> tuple[Path, dict[str, Any]]:
    from sts_combat_rl.sim.action_space import ActionSpaceConfig
    from sts_combat_rl.sim.controlled_run import execute_controlled_run
    from sts_combat_rl.sim.lightspeed import LightSpeedAdapter

    output_dir.mkdir(parents=True, exist_ok=True)
    attempt_dir = output_dir / f"attempt-{_next_attempt(output_dir):04d}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    started_cpu = time.process_time()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    status = {
        "schema_id": "issue60-game-attempt-v1", "status": "RUNNING",
        "game_seed": seed, "mode": mode, "block": block_name,
        "stage_key": stage_key, "arm": arm,
        "runtime_policy_seed": runtime_seed, "model_sha256": model_sha,
        "config_sha256": config_digest,
        "pid": os.getpid(), "started_cpu_seconds": started_cpu,
    }
    atomic_json(attempt_dir / "status.json", status)
    decision_writer = JsonlWriter(attempt_dir / "decisions.jsonl")
    search_writer = JsonlWriter(attempt_dir / "battle-search.jsonl")
    boundaries: list[dict[str, Any]] = []
    screen_counts: dict[str, int] = {}
    transitions = 0
    was_battle = False
    adapter = None
    run = None
    controller = None
    policy = None
    battle = None
    failure: str | None = None
    visible_bosses_seen: set[str] = set()
    heart_entry_selected = False

    def record_decision(meta: Mapping[str, Any]) -> None:
        nonlocal heart_entry_selected
        event = {**dict(meta), "game_seed": seed}
        serialized = event.get("issue60_public_input")
        if isinstance(serialized, str):
            public = json.loads(serialized)
            state = public.get("public_state", {})
            boss = state.get("visible_act_boss") if isinstance(state, Mapping) else None
            if isinstance(boss, Mapping):
                boss = _available(boss)
            if isinstance(boss, str):
                visible_bosses_seen.add(boss)
            options = public.get("legal_options", [])
            selected = event.get("issue60_selected_index")
            if (isinstance(state, Mapping) and state.get("screen") == "MAP_SCREEN"
                    and isinstance(selected, int) and 0 <= selected < len(options)):
                semantics = options[selected].get("semantics", {})
                destination = semantics.get("destination", {}) if isinstance(semantics, Mapping) else {}
                if (isinstance(destination, Mapping)
                        and destination.get("location") == "boss_entry"
                        and boss == "THE_HEART"):
                    heart_entry_selected = True
        decision_writer(event)

    try:
        controller, policy, battle = _source_controller(
            vocabulary=vocab, bootstrap=bootstrap, model=model, mode=mode,
            runtime_seed=runtime_seed, block_seed=MODEL_SEEDS.get(block_name),
            provenance_config=provenance_config, event_sink=search_writer,
            decision_sink=record_decision, require_targets=require_targets,
        ) if mode != "Expert" else (None, None, None)
        if mode == "Expert":
            controller, battle = _expert_controller(search_writer)
        adapter = LightSpeedAdapter(
            seed=seed, ascension=config["environment"]["ascension"],
            player_class=config["environment"]["player_class"],
        )

        def before(snapshot, actions, context, step_index):
            del snapshot, actions, step_index
            screen = str(context.screen_state)
            screen_counts[screen] = screen_counts.get(screen, 0) + 1

        def after(step):
            nonlocal transitions, was_battle
            transitions += 1

            def append_boundary(boundary: str, raw: Mapping[str, Any]) -> None:
                row = _boundary_record(step, boundary=boundary, raw=raw)
                row["public_resources"] = boundary_resources(
                    adapter._sim.public_projection_v3()
                )
                row["heart_encounter"] = bool(
                    heart_entry_selected and row.get("act") == 4
                    and row.get("room_type") == "BOSS"
                )
                boundaries.append(row)

            if step.next_battle_active and not was_battle:
                append_boundary("battle_enter", step.next_snapshot_raw)
            if was_battle and not step.next_battle_active:
                append_boundary("battle_exit", step.next_snapshot_raw)
            boss_screen_transition = ("BOSS" in step.screen_state) != ("BOSS" in step.next_screen_state)
            if boss_screen_transition:
                append_boundary("boss_boundary", step.next_snapshot_raw)
            was_battle = bool(step.next_battle_active)

        run = execute_controlled_run(
            adapter, controller, seed=seed, max_steps=MAX_STEPS,
            action_space=ActionSpaceConfig.include_all(),
            before_decision=before, after_transition=after, retain_steps=False,
        )
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        decision_writer.close()
        search_writer.close()
        if adapter is not None:
            adapter.close()
    run_cpu = time.process_time() - started_cpu
    if run is None:
        summary = {
            **status, "status": "INCOMPLETE", "failure": failure,
            "process_cpu_seconds": run_cpu, "screen_counts": screen_counts,
            "transitions": transitions, "boundaries": boundaries,
            "search_calls": battle.calls if battle is not None else 0,
            "search_cpu_seconds": battle.cpu_seconds if battle is not None else 0.0,
            "supported_choices": policy.counts.get("supported_choices", 0) if policy is not None else 0,
            "bootstrap_shifts": policy.counts.get("bootstrap_shifts", 0) if policy is not None else 0,
            "visible_bosses_seen": sorted(visible_bosses_seen),
            "heart_entry_selected": heart_entry_selected,
            "problems": [failure] if failure else ["controlled run did not return"],
        }
    else:
        final_floor = _terminal_floor(run)
        location = _public_location(run)
        complete = (
            run.terminal and not run.problems
            and run.outcome in {"PLAYER_LOSS", "PLAYER_VICTORY"}
            and final_floor is not None
        )
        summary = {
            **status, "status": "COMPLETE" if complete else "INCOMPLETE",
            "terminal": bool(run.terminal), "outcome": run.outcome,
            "terminal_floor": final_floor, "terminal_public_location": location,
            "death_location": location if run.outcome == "PLAYER_LOSS" else None,
            "process_cpu_seconds": run_cpu,
            "search_cpu_seconds": battle.cpu_seconds if battle is not None else 0.0,
            "search_calls": battle.calls if battle is not None else 0,
            "supported_choices": policy.counts.get("supported_choices", 0) if policy is not None else 0,
            "bootstrap_shifts": policy.counts.get("bootstrap_shifts", 0) if policy is not None else 0,
            "visible_bosses_seen": sorted(visible_bosses_seen),
            "heart_entry_selected": heart_entry_selected,
            "screen_counts": screen_counts, "transitions": transitions,
            "boundaries": boundaries, "problems": list(run.problems),
            "failure": failure,
        }
        if not complete and not summary["problems"]:
            summary["problems"].append("run capped, terminal outcome unknown, or terminal floor unavailable")
    before_compress_cpu = time.process_time()
    compressed_decisions = compress_jsonl(attempt_dir / "decisions.jsonl")
    compress_jsonl(attempt_dir / "battle-search.jsonl")
    summary["serialization_compression_cpu_seconds"] = time.process_time() - before_compress_cpu
    summary["process_cpu_seconds"] = time.process_time() - started_cpu
    summary["decision_file"] = compressed_decisions.name
    summary["decision_count"] = decision_writer.rows
    summary["search_log_file"] = "battle-search.jsonl.gz"
    summary["completed_cpu_seconds"] = time.process_time()
    atomic_json(attempt_dir / "run.json", summary)
    from attempt_lifecycle import finalized_attempt_status
    atomic_json(attempt_dir / "status.json", finalized_attempt_status(status, summary))
    return attempt_dir, summary


def decision_rows(attempt_dir: Path, summary: Mapping[str, Any]) -> list[DecisionRow]:
    floor = summary.get("terminal_floor")
    if floor is None:
        raise IncompleteRun("complete game has no terminal floor")
    rows = []
    for event in read_jsonl(attempt_dir / "decisions.jsonl.gz"):
        joined = dict(event)
        joined.update({
            "terminal": summary["terminal"], "run_problems": summary["problems"],
            "run_outcome": summary["outcome"], "terminal_floor": floor,
        })
        rows.append(DecisionRow.from_json(joined))
    return rows


def _ledger_path() -> Path:
    return EVIDENCE / "cpu-ledger.jsonl"


def _ledger_rows() -> list[dict[str, Any]]:
    path = _ledger_path()
    return read_jsonl(path) if path.exists() else []


def _append_ledger(row: Mapping[str, Any]) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _accounted_cpu(current_stage_cpu: float = 0.0) -> float:
    return BUILD_AND_STATIC_ALLOWANCE + sum(
        float(row.get("process_cpu_seconds", 0.0))
        for row in _ledger_rows()
    ) + current_stage_cpu


def _all_attempts(stage: str) -> list[dict[str, Any]]:
    root = EVIDENCE / "runs" / stage
    summaries = []
    if not root.exists():
        return summaries
    for path in root.rglob("run.json"):
        try:
            summaries.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return summaries


def _max_observed_game_seconds() -> float:
    observed = [
        float(row["process_cpu_seconds"])
        for stage in ("preflight", "training", "evaluation")
        for row in _all_attempts(stage)
        if row.get("status") == "COMPLETE"
    ]
    return max(observed, default=0.0)


def _completed_run_count() -> int:
    rows = _all_attempts("training") + _all_attempts("evaluation")
    return len({
        (row.get("stage_key"), row.get("game_seed"), row.get("arm"))
        for row in rows if row.get("status") == "COMPLETE"
    })


def _remaining_optimizer_projection() -> float:
    path = EVIDENCE / "preflight" / "native.json"
    if not path.exists():
        return 0.0
    gate = json.loads(path.read_text(encoding="utf-8"))
    measured = float(gate["optimizer_projection"]["measured_full_256_row_minibatch_cpu_seconds"])
    projected_choices = max((
        int(row.get("supported_choices", 0))
        for stage in ("preflight", "training")
        for row in _all_attempts(stage)
        if row.get("status") == "COMPLETE"
    ), default=0)
    projected_rows_per_batch = math.ceil(projected_choices * 1.5) * 128
    projected_minibatches_per_epoch = math.ceil(projected_rows_per_batch / 256)
    completed_batches = 0
    for path in (EVIDENCE / "training").glob("block*/batch-*.json"):
        try:
            json.loads(path.read_text(encoding="utf-8"))
            completed_batches += 1
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    remaining_batches = max(0, 16 - completed_batches)
    return remaining_batches * 4 * projected_minibatches_per_epoch * measured * 1.5


def _optimizer_projection(preflight_summaries: Sequence[Mapping[str, Any]],
                          minibatch_cpu: float) -> dict[str, Any]:
    support_counts = [int(row.get("supported_choices", 0)) for row in preflight_summaries]
    max_choices = max(support_counts, default=0)
    max_rows_per_batch = math.ceil(max_choices * 1.5) * 128
    mini_batches_per_epoch = math.ceil(max_rows_per_batch / 256)
    update_steps = 16 * 4 * mini_batches_per_epoch
    return {
        "max_supported_choices_per_preflight_game": max_choices,
        "projected_decisions_per_training_batch": max_rows_per_batch,
        "projected_minibatches_per_epoch": mini_batches_per_epoch,
        "projected_optimizer_minibatches": update_steps,
        "measured_full_256_row_minibatch_cpu_seconds": minibatch_cpu,
        "projected_optimizer_cpu_seconds": update_steps * minibatch_cpu * 1.5,
    }


def _benchmark_minibatch(model_seed: int, vocab: Vocabulary,
                         rows: Sequence[DecisionRow]) -> float:
    if not rows:
        raise RuntimeError("preflight produced no supported decision for update-cost measurement")
    torch.manual_seed(model_seed)
    model = JointChoiceActorCritic(vocab)
    optimizer = torch.optim.Adam(model.parameters(), lr=3e-4)
    batch = [rows[index % len(rows)] for index in range(256)]
    from trainer import centered_advantages, ppo_minibatch_loss
    advantages = centered_advantages(batch)
    started = time.process_time()
    optimizer.zero_grad(set_to_none=True)
    loss, _ = ppo_minibatch_loss(model, batch, advantages, vocab)
    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), 0.5)
    optimizer.step()
    return time.process_time() - started


def _write_stage_summary(stage: str, started_cpu: float, status: str,
                         extra: Mapping[str, Any] | None = None) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    _append_ledger({
        "stage": stage,
        "status": status,
        "process_cpu_seconds": time.process_time() - started_cpu,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **dict(extra or {}),
    })


def validate_config(config: Mapping[str, Any]) -> None:
    """Check that code constants and the committed study registration agree."""
    expected = {
        "base": {"remote": "origin/main", "commit": "9525d416b9488bb2455435699893b03fe19fe30a"},
        "preflight": {"seeds": [PRE_FLIGHT_SEEDS[0], PRE_FLIGHT_SEEDS[-1]]},
        "training": {
            "model_seeds": [MODEL_SEEDS["block1"], MODEL_SEEDS["block2"]],
            "block1_seeds": [TRAINING_SEEDS["block1"].start, TRAINING_SEEDS["block1"].stop - 1],
            "block2_seeds": [TRAINING_SEEDS["block2"].start, TRAINING_SEEDS["block2"].stop - 1],
            "batch_size_games": 128, "batches_per_block": 8,
            "ppo_epochs": 4, "minibatch_decision_rows": 256,
            "learning_rate": 0.0003, "ratio_clip": 0.2,
            "value_mse_coefficient": 0.5, "entropy_coefficient": 0.0,
            "global_gradient_norm_cap": 0.5,
        },
        "evaluation": {
            "seeds": [EVAL_SEEDS[0], EVAL_SEEDS[-1]], "owners": len(EVAL_SEEDS),
            "arms_per_owner": list(EVAL_ARMS),
            "run_count": len(EVAL_SEEDS) * len(EVAL_ARMS),
        },
    }
    if config.get("base") != expected["base"]:
        raise RuntimeError("config base does not match the Issue 60 fixed remote base")
    for section, values in expected.items():
        if section == "base":
            continue
        for name, value in values.items():
            if config.get(section, {}).get(name) != value:
                raise RuntimeError(f"config {section}.{name} disagrees with the fixed implementation")
    if config.get("environment", {}).get("max_executor_steps") != MAX_STEPS:
        raise RuntimeError("config executor-step cap disagrees with the fixed implementation")


def _current_policy_provenance(config: Mapping[str, Any], vocab: Vocabulary,
                               model_sha: str, mode: str) -> dict[str, Any]:
    return {
        "issue": 60, "config_sha256": config_sha(),
        "vocabulary_sha256": vocab.sha256,
        "native_source_commit": config["sources"]["native_commit"],
        "stsr_source_commit": config["sources"]["stsrl_commit"],
        "model_sha256": model_sha, "mode": mode,
        "action_space": config["environment"]["action_space"],
        "max_steps": MAX_STEPS,
    }


def _preflight_model(vocab: Vocabulary, seed: int) -> JointChoiceActorCritic:
    torch.manual_seed(seed)
    model = JointChoiceActorCritic(vocab)
    return model


def run_preflight(config: Mapping[str, Any], runtime: Mapping[str, Any],
                  vocab: Vocabulary, bootstrap: Scorer,
                  stage_started: float) -> dict[str, Any]:
    witness_path = EVIDENCE / "preflight" / "synthetic.json"
    synthetic = run_witnesses(HERE)
    existing = None
    if witness_path.exists():
        existing = json.loads(witness_path.read_text(encoding="utf-8"))
    if existing is not None and existing.get("config_sha256") != synthetic["config_sha256"]:
        raise RuntimeError("committed synthetic witness does not match the frozen config")
    if existing is None:
        atomic_json(witness_path, synthetic)
    if not synthetic["passed"]:
        raise RuntimeError("synthetic preflight witness failed")

    started_path = EVIDENCE / "preflight" / "native-started.json"
    if not started_path.exists():
        atomic_json(started_path, {
            "schema_id": "issue60-native-preflight-started-v1",
            "config_sha256": config_sha(),
            "manifest_implementation_commit": json.loads(
                (HERE / "frozen-manifest.json").read_text(encoding="utf-8")
            )["implementation_commit"],
            "preflight_seeds": list(PRE_FLIGHT_SEEDS),
            "first_game_may_start_after_this_marker": True,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    summaries: list[dict[str, Any]] = []
    row_for_update: list[DecisionRow] = []
    for index, seed in enumerate(PRE_FLIGHT_SEEDS):
        block_name = "block1" if index % 2 == 0 else "block2"
        model_seed = MODEL_SEEDS[block_name]
        model = _preflight_model(vocab, model_seed)
        model_sha = model_fingerprint(model, vocab, config_sha())
        game_dir = EVIDENCE / "runs" / "preflight" / block_name / f"seed-{seed}"
        completed = successful_attempt(game_dir, model_sha, config_sha())
        if completed is None:
            runtime_seed = derive_seed("preflight-policy", block_name, model_seed, seed)
            provenance = _current_policy_provenance(config, vocab, model_sha, "residual")
            attempt, summary = run_game(
                seed=seed, mode="residual", model=model, block_name=block_name,
                runtime_seed=runtime_seed, output_dir=game_dir,
                model_sha=model_sha, config_digest=config_sha(), vocab=vocab,
                bootstrap=bootstrap, require_targets=True,
                provenance_config=provenance,
                stage_key="preflight", arm="residual",
            )
        else:
            attempt, summary = completed
        summary["stage_key"] = "preflight"
        summary["arm"] = "residual"
        summary["model_seed"] = model_seed
        summaries.append(summary)
        if summary["status"] != "COMPLETE":
            raise RuntimeError(f"preflight game {seed} is INCOMPLETE: {summary.get('problems')}")
        row_for_update.extend(decision_rows(attempt, summary))

    if len(summaries) != 8:
        raise RuntimeError("all eight fixed preflight seeds were not completed")
    minibatch_cpu = _benchmark_minibatch(MODEL_SEEDS["block1"], vocab, row_for_update)
    optimizer = _optimizer_projection(summaries, minibatch_cpu)
    max_game = max(float(row["process_cpu_seconds"]) for row in summaries)
    accounted = _accounted_cpu(time.process_time() - stage_started)
    game_projection = max_game * POST_PREFLIGHT_GAMES * 1.5
    total_projection = (game_projection + optimizer["projected_optimizer_cpu_seconds"]) * 1.2
    projected_after_preflight = accounted + total_projection
    gate_status = "PASS" if projected_after_preflight <= BUILDER_BUDGET else "RESOURCE_BLOCKED"
    gate = {
        "schema_id": "issue60-preflight-budget-gate-v1",
        "status": gate_status,
        "config_sha256": config_sha(),
        "runtime": dict(runtime),
        "synthetic_witness_sha256": sha256_bytes(_canonical_bytes(synthetic)),
        "preflight_seeds": [row["game_seed"] for row in summaries],
        "preflight_runs_complete": len([row for row in summaries if row["status"] == "COMPLETE"]),
        "max_observed_game_cpu_seconds": max_game,
        "projected_remaining_game_cpu_seconds_before_headroom": game_projection,
        "optimizer_projection": optimizer,
        "headroom_fraction": 0.2,
        "projected_remaining_cpu_seconds_with_headroom": total_projection,
        "implementation_and_static_allowance_cpu_seconds": BUILD_AND_STATIC_ALLOWANCE,
        "preflight_and_implementation_accounted_cpu_seconds": accounted,
        "builder_cpu_budget_after_review_reserve": BUILDER_BUDGET,
        "projected_builder_total_cpu_seconds": projected_after_preflight,
        "reviewer_reserved_cpu_seconds": REVIEWER_RESERVE,
        "decisions_for_optimizer_benchmark": len(row_for_update),
        "one_minibatch_benchmark_cpu_seconds": minibatch_cpu,
        "decision": "continue with the fixed grid" if gate_status == "PASS" else "stop before formal training/evaluation",
        "preflight_summary": summaries,
    }
    atomic_json(EVIDENCE / "preflight" / "native.json", gate)
    if gate_status != "PASS":
        raise RuntimeError("RESOURCE_BLOCKED: fixed grid does not fit before formal sampling")
    return gate


def budget_guard(*, remaining_games: int, remaining_optimizer_cpu: float,
                 current_stage_cpu: float) -> None:
    max_game = _max_observed_game_seconds()
    projected_remaining_games = max_game * remaining_games * 1.5 * 1.2
    forecast = _accounted_cpu(current_stage_cpu) + projected_remaining_games + remaining_optimizer_cpu * 1.2
    if forecast > BUILDER_BUDGET:
        raise RuntimeError(
            f"RESOURCE_BLOCKED before next fixed game: projected builder CPU {forecast:.3f}s exceeds {BUILDER_BUDGET:.0f}s"
        )


def _training_game_dir(block: str, batch: int, seed: int) -> Path:
    return EVIDENCE / "runs" / "training" / block / f"batch-{batch:02d}" / f"seed-{seed}"


def _state_checkpoint(model: JointChoiceActorCritic, optimizer: torch.optim.Optimizer,
                      *, block: str, completed_batches: int, vocab: Vocabulary,
                      digest: str, batch_metrics: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_id": "issue60-ppo-checkpoint-v1", "purpose": "STUDY_ONLY",
        "block": block, "model_seed": MODEL_SEEDS[block],
        "completed_batches": completed_batches,
        "config_sha256": digest, "vocabulary_sha256": vocab.sha256,
        "model_sha256": model_fingerprint(model, vocab, digest),
        "model_state": model.state_dict(), "optimizer_state": optimizer.state_dict(),
        "batch_metrics": [dict(row) for row in batch_metrics],
    }


def atomic_torch_save(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temp.open("wb") as stream:
        torch.save(dict(value), stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def load_checkpoint(path: Path, model: JointChoiceActorCritic,
                    optimizer: torch.optim.Optimizer,
                    vocab: Vocabulary, digest: str) -> dict[str, Any]:
    payload = torch.load(path, map_location="cpu", weights_only=False)
    if payload.get("config_sha256") != digest or payload.get("vocabulary_sha256") != vocab.sha256:
        raise RuntimeError(f"checkpoint {path} does not match frozen study inputs")
    model.load_state_dict(payload["model_state"], strict=True)
    optimizer.load_state_dict(payload["optimizer_state"])
    if model_fingerprint(model, vocab, digest) != payload["model_sha256"]:
        raise RuntimeError(f"checkpoint {path} model fingerprint mismatch")
    return payload


def train_block(block: str, config: Mapping[str, Any], vocab: Vocabulary,
                bootstrap: Scorer, runtime: Mapping[str, Any],
                stage_started: float) -> dict[str, Any]:
    block_dir = EVIDENCE / "training" / block
    block_dir.mkdir(parents=True, exist_ok=True)
    training_config = config["training"]
    torch.manual_seed(MODEL_SEEDS[block])
    model = JointChoiceActorCritic(vocab)
    optimizer = torch.optim.Adam(
        model.parameters(), lr=float(training_config["learning_rate"])
    )
    digest = config_sha()
    checkpoint_path = block_dir / "latest.pt"
    batch_history: list[dict[str, Any]] = []
    start_batch = 0
    if checkpoint_path.exists():
        payload = load_checkpoint(checkpoint_path, model, optimizer, vocab, digest)
        start_batch = int(payload["completed_batches"])
        batch_history = list(payload.get("batch_metrics", []))
    else:
        initial = _state_checkpoint(
            model, optimizer, block=block, completed_batches=0,
            vocab=vocab, digest=digest, batch_metrics=(),
        )
        atomic_torch_save(checkpoint_path, initial)
    for batch_index in range(start_batch + 1, 9):
        if _accounted_cpu(time.process_time() - stage_started) > BUILDER_BUDGET:
            raise RuntimeError("RESOURCE_BLOCKED before the next training batch")
        frozen_sha = model_fingerprint(model, vocab, digest)
        rows: list[DecisionRow] = []
        used_runs = []
        for seed in list(TRAINING_SEEDS[block])[(batch_index - 1) * 128:batch_index * 128]:
            budget_guard(
                remaining_games=POST_PREFLIGHT_GAMES - _completed_run_count(),
                remaining_optimizer_cpu=_remaining_optimizer_projection(),
                current_stage_cpu=time.process_time() - stage_started,
            )
            game_dir = _training_game_dir(block, batch_index, seed)
            completed = successful_attempt(game_dir, frozen_sha, digest)
            if completed is None:
                runtime_seed = derive_seed("training-policy", MODEL_SEEDS[block], seed, batch_index)
                provenance = _current_policy_provenance(config, vocab, frozen_sha, "residual")
                attempt, summary = run_game(
                    seed=seed, mode="residual", model=model, block_name=block,
                    runtime_seed=runtime_seed, output_dir=game_dir,
                    model_sha=frozen_sha, config_digest=digest, vocab=vocab,
                    bootstrap=bootstrap, require_targets=True,
                    provenance_config=provenance,
                    stage_key=f"{block}:batch-{batch_index:02d}", arm="residual",
                )
            else:
                attempt, summary = completed
            summary["stage_key"] = f"{block}:batch-{batch_index:02d}"
            summary["batch_index"] = batch_index
            summary["arm"] = "residual"
            if summary["status"] != "COMPLETE":
                raise RuntimeError(f"training game {seed} is INCOMPLETE: {summary.get('problems')}")
            game_rows = decision_rows(attempt, summary)
            rows.extend(game_rows)
            used_runs.append({
                "seed": seed, "attempt": str(attempt.relative_to(HERE)),
                "process_cpu_seconds": summary["process_cpu_seconds"],
                "supported_choices": summary["supported_choices"],
            })
        if not rows:
            raise RuntimeError(f"batch {batch_index} collected no supported decisions")
        rows_file = block_dir / f"batch-{batch_index:02d}-training-rows.jsonl.gz"
        temp_rows = rows_file.with_name(rows_file.name + f".tmp-{os.getpid()}")
        with gzip.open(temp_rows, "wt", encoding="utf-8", compresslevel=6) as stream:
            for row in rows:
                stream.write(json.dumps(row.to_json(), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        os.replace(temp_rows, rows_file)
        update_started = time.process_time()
        metrics = update_batch(
            model, optimizer, rows, vocab, block_seed=MODEL_SEEDS[block],
            batch_index=batch_index, epochs=int(training_config["ppo_epochs"]),
            minibatch_size=int(training_config["minibatch_decision_rows"]),
            gradient_norm=float(training_config["global_gradient_norm_cap"]),
            clip_ratio=float(training_config["ratio_clip"]),
            value_coefficient=float(training_config["value_mse_coefficient"]),
            entropy_coefficient=float(training_config["entropy_coefficient"]),
        )
        update_cpu = time.process_time() - update_started
        aggregate = {
            "batch_index": batch_index, "block": block,
            "model_sha256_before": frozen_sha,
            "model_sha256_after": model_fingerprint(model, vocab, digest),
            "training_game_count": 128,
            "decision_count": len(rows),
            "actor_update_rows": sum(row.actor_update_mask > 0 for row in rows),
            "value_only_rows": sum(row.actor_update_mask == 0 for row in rows),
            "game_occupancy": used_runs,
            "optimizer_cpu_seconds": update_cpu,
            "minibatch_count": len(metrics), "minibatch_metrics": metrics,
            "fixed_grid_batch_seeds": [row["seed"] for row in used_runs],
            "training_rows_sha256": sha256_bytes(rows_file.read_bytes()),
        }
        batch_history.append(aggregate)
        payload = _state_checkpoint(
            model, optimizer, block=block, completed_batches=batch_index,
            vocab=vocab, digest=digest, batch_metrics=batch_history,
        )
        atomic_torch_save(checkpoint_path, payload)
        atomic_json(block_dir / f"batch-{batch_index:02d}.json", aggregate)
    final_payload = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    if int(final_payload["completed_batches"]) != 8:
        raise RuntimeError(f"{block} final checkpoint is not after batch eight")
    final_path = block_dir / "final.pt"
    atomic_torch_save(final_path, final_payload)
    summary = {
        "status": "COMPLETE", "block": block, "model_seed": MODEL_SEEDS[block],
        "completed_batches": 8, "final_checkpoint": str(final_path.relative_to(HERE)),
        "final_checkpoint_sha256": sha256_bytes(final_path.read_bytes()),
        "model_sha256": final_payload["model_sha256"],
        "config_sha256": digest, "vocabulary_sha256": vocab.sha256,
        "batch_summaries": batch_history,
    }
    atomic_json(block_dir / "final.json", summary)
    return summary


def run_training(config: Mapping[str, Any], runtime: Mapping[str, Any],
                 vocab: Vocabulary, bootstrap: Scorer,
                 stage_started: float) -> dict[str, Any]:
    from resource_amendment_r1 import require_training_gate
    require_training_gate(HERE, config)
    block_results = []
    for block in ("block1", "block2"):
        block_results.append(train_block(block, config, vocab, bootstrap, runtime, stage_started))
    result = {
        "schema_id": "issue60-training-result-v1", "status": "COMPLETE",
        "config_sha256": config_sha(), "blocks": block_results,
        "source_pins": dict(config["sources"]),
    }
    atomic_json(EVIDENCE / "training" / "result.json", result)
    return result


def _load_final_model(block: str, vocab: Vocabulary, digest: str) -> tuple[JointChoiceActorCritic, str]:
    path = EVIDENCE / "training" / block / "final.pt"
    if not path.is_file():
        raise RuntimeError(f"final checkpoint for {block} is missing")
    payload = torch.load(path, map_location="cpu", weights_only=False)
    if payload.get("completed_batches") != 8 or payload.get("config_sha256") != digest:
        raise RuntimeError(f"final checkpoint for {block} does not match the fixed endpoint")
    model = JointChoiceActorCritic(vocab)
    model.load_state_dict(payload["model_state"], strict=True)
    if model_fingerprint(model, vocab, digest) != payload["model_sha256"]:
        raise RuntimeError(f"final checkpoint for {block} failed model checksum")
    model.eval()
    return model, payload["model_sha256"]


def run_evaluation(config: Mapping[str, Any], runtime: Mapping[str, Any],
                   vocab: Vocabulary, bootstrap: Scorer,
                   stage_started: float) -> dict[str, Any]:
    if not (EVIDENCE / "training" / "result.json").is_file():
        raise RuntimeError("both fixed training blocks must finish before evaluation")
    digest = config_sha()
    final1, sha1 = _load_final_model("block1", vocab, digest)
    final2, sha2 = _load_final_model("block2", vocab, digest)
    summaries = []
    for owner_index, seed in enumerate(EVAL_SEEDS):
        common_rng = derive_seed("evaluation-common-policy-stream", seed)
        for arm in EVAL_ARMS:
            budget_guard(
                remaining_games=(len(EVAL_SEEDS) - owner_index) * len(EVAL_ARMS),
                remaining_optimizer_cpu=0.0,
                current_stage_cpu=time.process_time() - stage_started,
            )
            if arm == "final_block1":
                mode, model, block, model_sha = "residual", final1, "block1", sha1
            elif arm == "final_block2":
                mode, model, block, model_sha = "residual", final2, "block2", sha2
            elif arm == "p0":
                mode, model, block, model_sha = "p0", None, "evaluation", "p0"
            elif arm == "A":
                mode, model, block, model_sha = "A", None, "evaluation", "A"
            else:
                mode, model, block, model_sha = "Expert", None, "evaluation", "Expert"
            game_dir = EVIDENCE / "runs" / "evaluation" / f"owner-{owner_index + 1:03d}" / arm
            completed = successful_attempt(game_dir, model_sha, digest)
            if completed is None:
                provenance = _current_policy_provenance(config, vocab, model_sha, mode)
                attempt, summary = run_game(
                    seed=seed, mode=mode, model=model, block_name=block,
                    runtime_seed=common_rng, output_dir=game_dir,
                    model_sha=model_sha, config_digest=digest, vocab=vocab,
                    bootstrap=bootstrap, require_targets=False,
                    provenance_config=provenance,
                    stage_key=f"owner-{owner_index + 1:03d}", arm=arm,
                )
            else:
                attempt, summary = completed
            summary.update({"stage_key": f"owner-{owner_index + 1:03d}", "owner": owner_index + 1, "arm": arm})
            summaries.append(summary)
            if summary["status"] != "COMPLETE":
                raise RuntimeError(f"evaluation owner {seed}/{arm} is INCOMPLETE: {summary.get('problems')}")
    if len(summaries) != 640:
        raise RuntimeError("the fixed 640-run evaluation grid is incomplete")
    result = {
        "schema_id": "issue60-evaluation-result-v1", "status": "COMPLETE",
        "config_sha256": digest, "runtime": dict(runtime),
        "owner_count": 128, "arm_count": 5, "run_count": len(summaries),
        "runs": summaries,
    }
    atomic_json(EVIDENCE / "evaluation" / "all-owners.json", result)
    return result


def _percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires values")
    position = (len(ordered) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def analyze(config: Mapping[str, Any]) -> dict[str, Any]:
    path = EVIDENCE / "evaluation" / "all-owners.json"
    evaluation = json.loads(path.read_text(encoding="utf-8"))
    if evaluation.get("status") != "COMPLETE" or len(evaluation.get("runs", [])) != 640:
        raise RuntimeError("primary analysis requires all 640 complete evaluation runs")
    owners: dict[int, dict[str, Any]] = {}
    for row in evaluation["runs"]:
        if row["status"] != "COMPLETE":
            raise RuntimeError("incomplete evaluation owner cannot enter the primary analysis")
        owners.setdefault(int(row["owner"]), {})[row["arm"]] = row
    if set(owners) != set(range(1, 129)) or any(set(row) != set(EVAL_ARMS) for row in owners.values()):
        raise RuntimeError("evaluation owner/arm partition is not the fixed 128 x 5 grid")
    paired = []
    owner_rows = []
    for owner in range(1, 129):
        arms = owners[owner]
        paired.append({arm: arms[arm] for arm in EVAL_ARMS})
        owner_rows.append({"owner": owner, **{
            arm: {
                "terminal_floor": arms[arm]["terminal_floor"],
                "outcome": arms[arm]["outcome"],
                "terminal_public_location": arms[arm]["terminal_public_location"],
                "death_location": arms[arm]["death_location"],
                "supported_choices": arms[arm]["supported_choices"],
                "bootstrap_shifts": arms[arm]["bootstrap_shifts"],
                "screen_counts": arms[arm]["screen_counts"],
                "boundaries": arms[arm]["boundaries"],
                "process_cpu_seconds": arms[arm]["process_cpu_seconds"],
            }
            for arm in EVAL_ARMS
        }})
    cfg = config["evaluation"]
    results = {
        "schema_id": "issue60-analysis-v1", "status": "COMPLETE",
        "config_sha256": config_sha(),
    }
    # Average the two fixed final blocks per owner for the registered contrasts.
    for comparator in ("p0", "A", "Expert"):
        values = [
            (float(owners[i]["final_block1"]["terminal_floor"])
             + float(owners[i]["final_block2"]["terminal_floor"])) / 2.0
            - float(owners[i][comparator]["terminal_floor"])
            for i in range(1, 129)
        ]
        rng = random.Random(int(cfg["bootstrap_seed"]))
        boot = [
            sum(values[rng.randrange(128)] for _ in range(128)) / 128
            for _ in range(int(cfg["bootstrap_replicates"]))
        ]
        results.setdefault("paired_contrasts", {})[comparator] = {
            "mean": sum(values) / 128,
            "positive": sum(x > 0 for x in values),
            "negative": sum(x < 0 for x in values),
            "zero": sum(x == 0 for x in values),
            "paired_owner_differences": values,
            "bootstrap": {
                "replicates": int(cfg["bootstrap_replicates"]),
                "seed": int(cfg["bootstrap_seed"]),
                "method": "percentile with linear interpolation at (n-1)*p",
                "ci_95": [_percentile(boot, .025), _percentile(boot, .975)],
            },
        }
    results["primary"] = results["paired_contrasts"]["p0"]
    outcomes = {}
    for arm in EVAL_ARMS:
        rows = [owners[index][arm] for index in range(1, 129)]
        outcomes[arm] = {
            "terminal_outcomes": {
                value: sum(row["outcome"] == value for row in rows)
                for value in ("PLAYER_LOSS", "PLAYER_VICTORY")
            },
            "act_reached_counts": {
                str(act): sum(
                    any(boundary.get("act") == act for boundary in row["boundaries"])
                    or row["terminal_public_location"].get("act") == act
                    for row in rows
                ) for act in (2, 3, 4)
            },
            "heart_act_terminal_count": sum(
                row["terminal_public_location"].get("act") == 4 for row in rows
            ),
            "heart_battle_count": sum(
                boundary.get("heart_encounter") is True
                for row in rows for boundary in row["boundaries"]
            ),
            "battle_encounter_count": sum(
                boundary.get("boundary") == "battle_enter"
                for row in rows for boundary in row["boundaries"]
            ),
            "battle_outcome_counts": {
                value: sum(
                    boundary.get("boundary") == "battle_exit"
                    and boundary.get("battle_outcome") == value
                    for row in rows for boundary in row["boundaries"]
                ) for value in ("VICTORY", "PLAYER_LOSS", "UNKNOWN")
            },
            "visible_act_bosses_seen": sorted({
                boss for row in rows for boss in row.get("visible_bosses_seen", [])
            }),
            "heart_entry_selected_runs": sum(row.get("heart_entry_selected", False) for row in rows),
            "boundary_resource_availability": {
                field: {
                    state: sum(
                        boundary.get("public_resources", {}).get(field, {}).get("availability") == state
                        for row in rows for boundary in row["boundaries"]
                        if boundary.get("boundary") in {"battle_enter", "battle_exit", "boss_boundary"}
                    )
                    for state in ("available", "unavailable", "unsupported", "not_applicable")
                }
                for field in RESOURCE_FIELDS
            },
            "mean_terminal_floor": sum(float(row["terminal_floor"]) for row in rows) / 128,
            "supported_choices": sum(int(row["supported_choices"]) for row in rows),
            "bootstrap_shifts": sum(int(row["bootstrap_shifts"]) for row in rows),
            "battle_boundaries": [
                {"owner": index, **boundary}
                for index in range(1, 129)
                for boundary in owners[index][arm]["boundaries"]
                if boundary["boundary"] in {"battle_enter", "battle_exit", "boss_boundary"}
            ],
            "death_locations": [
                {"owner": row["owner"], "location": row["death_location"]}
                for row in rows if row["death_location"] is not None
            ],
        }
    results["outcomes_by_arm"] = outcomes
    results["owner_outcomes"] = owner_rows
    results["interpretation"] = (
        "The interval is conditional on these two fixed training blocks; it is not an estimate over retraining randomness."
    )
    atomic_json(EVIDENCE / "evaluation" / "analysis.json", results)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("synthetic", "verify", "preflight", "resource-r1", "seal-resource-r1", "train", "evaluate", "analyze"), required=True)
    args = parser.parse_args()
    if args.stage in {"synthetic", "preflight"}:
        parser.error("original evidence-producing stages are immutable; use the R1 resource stage")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    validate_config(config)
    stage_started = time.process_time()
    status = "FAILED"
    failure: str | None = None
    runtime: dict[str, Any] = {}
    try:
        if args.stage in {"resource-r1", "seal-resource-r1"}:
            from resource_amendment_r1 import run_resource_stage, seal_resource_gate
            if args.stage == "resource-r1":
                result = run_resource_stage(HERE, stage_started)
            else:
                result = seal_resource_gate(HERE, stage_started)
            print(json.dumps(result, indent=2))
            status = "COMPLETE"
            return 0
        from resource_amendment_r1 import verify_resource_chain
        verify_resource_chain(
            HERE,
            require_gate_seal=args.stage in {"verify", "train", "evaluate", "analyze"},
        )
        if args.stage == "verify":
            runtime = verify_runtime(config)
            print(json.dumps({"status": "PASS", "config_sha256": config_sha(),
                              "manifest": "PASS", "runtime": runtime}, indent=2))
            status = "COMPLETE"
            return 0
        if args.stage == "analyze":
            result = analyze(config)
            print(json.dumps({k: v for k, v in result.items() if k not in {"owner_outcomes"}}, indent=2))
            status = "COMPLETE"
            return 0
        runtime = verify_runtime(config)
        vocab = Vocabulary(HERE / "vocabulary.json")
        if vocab.sha256 != config["sources"]["vocabulary_sha256"]:
            raise RuntimeError("frozen vocabulary file hash mismatch")
        bootstrap, _ = load_bootstrap(HERE / config["bootstrap"]["checkpoint"])
        if args.stage == "preflight":
            result = run_preflight(config, runtime, vocab, bootstrap, stage_started)
        elif args.stage == "train":
            result = run_training(config, runtime, vocab, bootstrap, stage_started)
        else:
            result = run_evaluation(config, runtime, vocab, bootstrap, stage_started)
        print(json.dumps({k: v for k, v in result.items() if k not in {"runs", "blocks", "preflight_summary"}}, indent=2))
        status = "COMPLETE"
        return 0
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        atomic_json(EVIDENCE / "stage-status" / f"{args.stage}.json", {
            "schema_id": "issue60-stage-status-v1", "stage": args.stage,
            "status": status, "failure": failure,
            "process_cpu_seconds": time.process_time() - stage_started,
            "runtime": runtime,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        _write_stage_summary(args.stage, stage_started, status, {"runtime": runtime})


if __name__ == "__main__":
    raise SystemExit(main())
