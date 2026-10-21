"""Bounded, resumable Issue 70 all-noncombat policy study.

This is disposable study code. Game transitions and every battle mechanic are
owned by the exact pinned sts_lightspeed native source. STSRL's one authoritative
controlled-run executor owns reset, legal-action validation and stepping.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import importlib
import json
import math
import os
import random
import resource
import signal
import sys
import threading
import time
MODULE_PROCESS_CPU_START = time.process_time()
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import torch

from policy import (
    AllNoncombatPolicy,
    DEFAULT_HASH_DIMENSION,
    ProjectionError,
    SemanticHashEncoder,
    derived_seed,
    validate_live_native_candidate_bindings,
)


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
STUDY_ID = CONFIG["study_id"]
EXPECTED_NATIVE = CONFIG["identity"]["native_commit"]
EXPECTED_STSRL = CONFIG["identity"]["stsr_commit"]
EXPECTED_STSRL_NATIVE_INTEGRATION = CONFIG["identity"]["stsr_native_integration_commit"]
MAX_CPU = float(CONFIG["resources"]["builder_cpu_hard_cap_seconds"])
REVIEWER_RESERVE = float(CONFIG["resources"]["reviewer_cpu_reserve_seconds"])
SETUP_CPU = float(CONFIG["resources"]["compile_configure_cpu_seconds_already_used"])
PROJECTION = CONFIG["resources"]["preflight_projection"]
ALL_IN_CPU = float(CONFIG["resources"]["all_in_ceiling_seconds"])
BRANCH_CPU_UPPER = float(CONFIG["resources"]["branch_cpu_upper_bound_seconds"])
CPU_MARGIN = float(CONFIG["resources"]["cpu_margin_seconds"])
MAX_STEPS = int(CONFIG["resources"]["max_steps_per_natural_run"])
ACTION_SPACE = None  # initialized from the pinned STSRL source at runtime
CPU_CHILD_CHARGE = 0.0
CPU_BRANCH_CHARGE_CURRENT = 0.0
RUN_STARTED_CPU = 0.0
OUT_DIR: Path | None = None
BASELINE_CHARGED_CPU = 0.0
BASELINE_PARENT_CPU = 0.0
BASELINE_BRANCH_CPU = 0.0
INITIAL_BRANCH_RESERVE = BRANCH_CPU_UPPER


class StudyFailure(RuntimeError):
    """Fail-closed study contract or native-run failure."""


class CpuBudgetExceeded(StudyFailure):
    pass


def current_rss_kib() -> int:
    """Read the live Linux resident set, keeping RSS attributable per run."""
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage / 1024) if sys.platform == "darwin" else int(usage)


class PeakRssSampler:
    """Sample live RSS through long native searches and controller callbacks."""
    def __init__(self, interval_seconds: float = 0.05) -> None:
        self.interval_seconds = interval_seconds
        self.peak_kib = current_rss_kib()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.peak_kib = max(self.peak_kib, current_rss_kib())

    def __enter__(self) -> "PeakRssSampler":
        self._thread = threading.Thread(target=self._sample, name="issue70-rss-sampler", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self.peak_kib = max(self.peak_kib, current_rss_kib())


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: Path) -> None:
    """Persist directory entries after atomic rename/first creation."""
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    payload = (canonical(value) + "\n").encode("utf-8")
    with temp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    fsync_directory(path.parent)


def atomic_npz(path: Path, **arrays: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temp.open("wb") as handle:
        np.savez_compressed(handle, **arrays)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    fsync_directory(path.parent)


def append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    line = (canonical(record) + "\n").encode("utf-8")
    with path.open("ab", buffering=0) as handle:
        handle.write(line)
        os.fsync(handle.fileno())
    if created:
        fsync_directory(path.parent)


def lock_config(out: Path) -> str:
    digest = sha256_file(CONFIG_PATH)
    lock = ROOT / "config.lock.sha256"
    if not lock.exists():
        raise StudyFailure("committed config.lock.sha256 is missing; do not consume study seeds")
    expected = lock.read_text(encoding="ascii").strip()
    if expected != digest:
        raise StudyFailure("config.json changed after the committed study lock")
    out.mkdir(parents=True, exist_ok=True)
    lock_path = out / "config-lock.json"
    record = {"study_id": STUDY_ID, "config_sha256": digest, "locked_before_study_seed": True}
    if lock_path.exists():
        previous = json.loads(lock_path.read_text(encoding="utf-8"))
        if previous != record:
            raise StudyFailure("output directory contains a different study config lock")
    else:
        atomic_json(lock_path, record)
    return digest


def _git_identity(path: str) -> tuple[str, str]:
    import subprocess
    commit = subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", path, "status", "--porcelain"], text=True).strip()
    return commit, dirty


def validate_environment(out: Path) -> dict[str, Any]:
    if sys.version.split()[0] != CONFIG["identity"]["python"]:
        raise StudyFailure(f"Python version mismatch: {sys.version.split()[0]}")
    if torch.__version__ != CONFIG["identity"]["torch"]:
        raise StudyFailure(f"torch version mismatch: {torch.__version__}")
    if np.__version__ != CONFIG["identity"]["numpy"]:
        raise StudyFailure(f"NumPy version mismatch: {np.__version__}")
    if torch.cuda.is_available():
        raise StudyFailure("GPU is not part of the frozen CPU study environment")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    native_source = os.environ.get("STS_LIGHTSPEED_SOURCE_DIR", "")
    stsr_source = os.environ.get("STSRL_SOURCE_DIR", "")
    if not native_source or not stsr_source:
        raise StudyFailure("STS_LIGHTSPEED_SOURCE_DIR and STSRL_SOURCE_DIR must identify the exact source checkouts")
    native_commit, native_dirty = _git_identity(native_source)
    stsr_commit, stsr_dirty = _git_identity(stsr_source)
    if native_commit != EXPECTED_NATIVE or native_dirty:
        raise StudyFailure("native source checkout does not match the clean frozen commit")
    if stsr_commit != EXPECTED_STSRL or stsr_dirty:
        raise StudyFailure("STSRL source checkout does not match the clean frozen commit")
    module = importlib.import_module("slaythespire")
    module_path = Path(module.__file__).resolve()
    binary_hash = sha256_file(module_path)
    if module_path.name != CONFIG["identity"]["native_module"] or binary_hash != CONFIG["identity"]["native_binary_sha256"]:
        raise StudyFailure(f"loaded native module identity mismatch: {module_path} {binary_hash}")
    if not hasattr(module, "PrivilegedNoncombatProjectionAccess"):
        raise StudyFailure("pinned native module lacks PrivilegedNoncombatProjectionAccess")
    if not hasattr(module.StepSimulator, "battle_search_v2"):
        raise StudyFailure("pinned native module lacks Battle Search-v2")
    source_identity = importlib.import_module("sts_combat_rl.sim.lightspeed_source").lightspeed_source_identity_dict()
    if source_identity.get("integration_commit") != EXPECTED_STSRL_NATIVE_INTEGRATION:
        raise StudyFailure("STSRL's loaded-source identity differs from its separately pinned integration manifest")
    artifact = {
        "study_id": STUDY_ID,
        "native_source_commit": native_commit,
        "stsr_source_commit": stsr_commit,
        "stsr_native_integration_commit": source_identity.get("integration_commit"),
        "native_module_path": str(module_path),
        "native_module_sha256": binary_hash,
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "numpy": np.__version__,
        "cpu_only": True,
        "lightspeed_source_identity": source_identity,
        "native_api": ["PrivilegedNoncombatProjectionAccess.project_v1", "StepSimulator.battle_search_v2", "capture_checkpoint", "restore_checkpoint"],
    }
    atomic_json(out / "environment.json", artifact)
    return {"module": module, "identity": artifact}


def _source_strings(value: Any, *, key_path: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key = str(key)
            if key in {"source", "candidate_binding"}:
                continue
            found.extend(_source_strings(item, key_path=f"{key_path}.{key}" if key_path else key))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            found.extend(_source_strings(item, key_path=key_path))
    elif isinstance(value, str):
        found.append((key_path.lower(), value.lower()))
    elif isinstance(value, bool):
        found.append((key_path.lower(), str(value).lower()))
    return found


def _has_term(strings: Sequence[tuple[str, str]], *terms: str) -> bool:
    joined = " ".join(f"{key} {value}" for key, value in strings)
    return any(term.lower() in joined for term in terms)


def _bottled(strings: Sequence[tuple[str, str]]) -> bool:
    for key, value in strings:
        if "bottled" in key or "bottle" in key:
            if value in {"true", "1", "yes"} or "bottled" in value or "bottle" in value:
                return True
        if "bottled" in value or "bottle" in value:
            return True
    return False


def exposure_flags(projection: Mapping[str, Any], qualification: Mapping[str, Any]) -> dict[str, bool]:
    strings = _source_strings(projection)
    return {
        "prismatic_shard_offered": _has_term(strings, "prismatic shard", "prismatic_shard"),
        "prismatic_shard_acquired": bool(qualification.get("prismatic_shard_present")),
        "falling_bottled_card_case": _has_term(strings, "falling") and _bottled(strings),
        "we_meet_again_bottled_card_case": _has_term(strings, "we meet again", "we_meet_again") and _bottled(strings),
        "mysterious_sphere_golden_idol_case": _has_term(strings, "mysterious sphere", "mysterious_sphere") and _has_term(strings, "golden idol", "golden_idol"),
    }


def native_adapter(module: Any, seed: int) -> Any:
    from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
    return LightSpeedAdapter(seed=seed, ascension=20, player_class="IRONCLAD", module=module)


def native_simulator(adapter: Any) -> Any:
    current = adapter
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        sim = getattr(current, "_sim", None)
        if sim is not None:
            return sim
        current = getattr(current, "base", None)
        if current is None:
            break
    raise StudyFailure("native StepSimulator could not be resolved from adapter")


class CheckpointAdapter:
    """Study-only restoration wrapper for exact in-process native checkpoints."""
    def __init__(self, base: Any, checkpoint: Any):
        self.base = base
        self.checkpoint = checkpoint

    def reset(self, seed: int | None = None) -> Any:
        del seed
        return self.base.restore_checkpoint(self.checkpoint)

    def legal_actions(self, snapshot: Any) -> list[Any]:
        return self.base.legal_actions(snapshot)

    def step(self, action: Any) -> Any:
        return self.base.step(action)

    def battle_search_v2(self, snapshot: Any, **kwargs: Any) -> Any:
        return self.base.battle_search_v2(snapshot, **kwargs)


class StudyController:
    def __init__(
        self,
        *,
        module: Any,
        arm: str,
        model: AllNoncombatPolicy,
        model_digest: str,
        encoder: SemanticHashEncoder,
        seed: int,
        capture_checkpoints: bool = False,
        forced_root: Mapping[str, Any] | None = None,
        sampling_purpose: str = "policy",
        capture_trace: bool = True,
    ) -> None:
        from sts_combat_rl.sim.action_space import ActionSpaceConfig
        from sts_combat_rl.sim.controller_contract import ControllerProvenance
        from sts_combat_rl.sim.oracle_search import OracleSearchController

        if arm not in {"TRAIN", "BRANCH", "P0", "P1", "E"}:
            raise ValueError(f"unknown study arm {arm}")
        self.module = module
        self.arm = arm
        self.model = model
        self.model_digest = model_digest
        self.encoder = encoder
        self.seed = seed
        self.capture_checkpoints = capture_checkpoints
        self.forced_root = dict(forced_root) if forced_root is not None else None
        self.forced_root_pending = self.forced_root is not None
        self.sampling_purpose = sampling_purpose
        self.capture_trace = capture_trace
        self.rng = torch.Generator(device="cpu")
        self.rng.manual_seed(derived_seed(STUDY_ID, seed, arm, model_digest, sampling_purpose))
        self.roots: list[dict[str, Any]] = []
        self.trace: list[dict[str, Any]] = []
        self.screen_counts: Counter[str] = Counter()
        self.action_kind_counts: Counter[str] = Counter()
        self.privacy_counts: Counter[str] = Counter()
        self.exposures: dict[str, bool] = {name: False for name in (
            "prismatic_shard_offered", "prismatic_shard_acquired",
            "falling_bottled_card_case", "we_meet_again_bottled_card_case",
            "mysterious_sphere_golden_idol_case",
        )}
        self.native_identity_digest: str | None = None
        self.oracle = OracleSearchController(
            simulations=400,
            root_selection_rule="highest_mean",
            action_space=ActionSpaceConfig.include_all(),
        )
        self.expert = None
        if arm == "E":
            from sts_combat_rl.sim.non_combat_policy import ExpertNonCombatDriver
            self.expert = ExpertNonCombatDriver(seed=1740001)
        provenance_config = {
            "study_id": STUDY_ID,
            "arm": arm,
            "information_regime": CONFIG["information_regime"],
            "policy_sha256": model_digest,
            "sampling_purpose": sampling_purpose,
            "battle": self.oracle.provenance.to_dict(),
            "expert": self.expert.provenance_config if self.expert is not None else None,
            "native_source_commit": EXPECTED_NATIVE,
        }
        self.provenance = ControllerProvenance(
            kind="issue70_study_controller", name=f"issue70_{arm.lower()}_v1", config=provenance_config
        )

    def reset_for_run(self, seed: int | None) -> None:
        if seed is not None and int(seed) != self.seed:
            raise StudyFailure("controller simulator seed differs from its predeclared owner seed")
        if self.expert is not None:
            self.expert.reset_for_run(self.seed)

    def select_action(self, adapter: Any, snapshot: Any, actions: Sequence[Any], context: Any, step_index: int) -> Any:
        from sts_combat_rl.sim.controller_contract import ControllerDecision
        from sts_combat_rl.sim.online_controller import is_battle_state
        from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions
        from public_history_execution import phase

        is_battle = is_battle_state(snapshot.raw, context.screen_state)
        sim = native_simulator(adapter)
        with phase("native_training_population_qualification"):
            qualification = self.module.PrivilegedNoncombatProjectionAccess(
                sim, "training_oracle_only"
            ).training_population_qualification_v1()
        if qualification.get("native_source_commit") != EXPECTED_NATIVE:
            raise StudyFailure("native population qualification came from a different source")
        if qualification.get("prismatic_shard_present"):
            self.exposures["prismatic_shard_acquired"] = True
        if is_battle:
            if self.arm == "E" and self.expert is None:
                raise StudyFailure("battle search arm is missing")
            with phase("native_search_and_root_mapping"):
                return self.oracle.select_action(adapter, snapshot, actions, context, step_index)

        self.screen_counts[str(context.screen_state)] += 1
        for action in actions:
            self.action_kind_counts[str(action.kind)] += 1
        if len(actions) == 1:
            self.trace.append({"kind": "single_action", "screen": str(context.screen_state), "step": int(step_index)})
            return ControllerDecision(
                selected_index=0, provenance=self.provenance, reason="native_single_legal_action",
                metadata={"study_arm": self.arm, "policy_training_row": False},
            )
        if len(actions) < 1:
            raise StudyFailure("nonbattle state has no legal native actions")
        if list(context.eligible_action_indices) != list(range(len(actions))):
            raise StudyFailure("ActionSpaceConfig.include_all did not preserve every native legal action")

        with phase("native_privileged_projection"):
            projection = self.module.PrivilegedNoncombatProjectionAccess(
                sim, "training_oracle_only"
            ).project_v1()
        if projection.get("native_source_commit") != EXPECTED_NATIVE:
            raise StudyFailure("privileged candidate projection came from a different native source")
        validate_live_native_candidate_bindings(projection, actions)
        with phase("semantic_feature_encoding"):
            matrix, audit = self.encoder.encode(projection, len(actions))
        self.native_identity_digest = hashlib.sha256(canonical({
            "source": EXPECTED_NATIVE,
            "screen": audit["screen"],
            "candidate_order_source": audit["candidate_order_source"],
        }).encode()).hexdigest()
        flags = exposure_flags(projection, qualification)
        for key, value in flags.items():
            self.exposures[key] = self.exposures.get(key, False) or bool(value)
        visibility_counts = Counter()
        for source, amount in audit["feature_source_counts"].items():
            visibility_counts[source] += amount
        self.privacy_counts[str(audit["screen"])] += 1

        if self.arm == "E":
            with phase("expert_reference_policy_inference"):
                decision = self.expert.select_action(context)
            selected = int(decision.legal_action_index)
            reason = decision.reason
            probabilities = None
            value = None
            policy_row = False
        else:
            with phase("policy_inference_and_sampling"):
                selected, probabilities, value = self.model.choose(matrix, self.rng)
            reason = "sampled_native_semantic_categorical_policy"
            policy_row = self.arm in {"TRAIN", "P0", "P1", "BRANCH"}

        if not 0 <= selected < len(actions):
            raise StudyFailure("controller selected an out-of-range native action")
        identities = action_identity_dicts_for_actions(actions)
        if self.forced_root_pending:
            if str(self.forced_root.get("screen")) != str(audit["screen"]):
                raise StudyFailure("restored branch root screen differs from captured native checkpoint")
            expected = self.forced_root.get("legal_action_identities")
            if canonical(expected) != canonical(identities):
                raise StudyFailure("restored branch root legal native actions differ from captured checkpoint")
            forced_index = int(self.forced_root["candidate_index"])
            if not 0 <= forced_index < len(actions):
                raise StudyFailure("counterfactual root candidate does not map to a live native legal action")
            selected = forced_index
            reason = "forced_native_legal_root_alternative"
            self.forced_root_pending = False

        decision_index = len(self.trace)
        trace_item = {
            "kind": "categorical",
            "screen": str(context.screen_state),
            "native_screen": audit["screen"],
            "step": int(step_index),
            "chosen_index": selected,
            "probabilities": probabilities.tolist() if probabilities is not None else None,
            "value_estimate": value,
            "legal_action_identities": identities,
            "projection_audit": audit,
            "projection_digest": hashlib.sha256(canonical(projection).encode()).hexdigest(),
            "root_identity_digest": self.native_identity_digest,
            "policy_training_row": policy_row,
        }
        self.trace.append({"matrix": matrix, **trace_item} if self.capture_trace else trace_item)
        if self.capture_checkpoints and len(self.roots) < 2:
            checkpoint = adapter.capture_checkpoint(snapshot)
            self.roots.append({
                "decision_index": decision_index,
                "policy_decision_index": sum(item.get("kind") == "categorical" for item in self.trace[:decision_index]),
                "checkpoint": checkpoint,
                "screen": audit["screen"],
                "candidate_index": selected,
                "legal_action_identities": identities,
                "probabilities": probabilities.tolist(),
                "audit": audit,
            })
        return ControllerDecision(
            selected_index=selected,
            provenance=self.provenance,
            reason=reason,
            score=None if probabilities is None else float(probabilities[selected]),
            metadata={
                "study_id": STUDY_ID,
                "study_arm": self.arm,
                "information_regime": CONFIG["information_regime"]["nonbattle_policy"],
                "projection_schema": audit["projection_schema_id"],
                "native_source_commit": EXPECTED_NATIVE,
                "opaque_bindings_used_for_model_input": False,
                "normal_public_context_unchanged": True,
                "policy_training_row": policy_row,
            },
        )


def _episode_terminal_fields(run: Any) -> dict[str, Any]:
    if not run.terminal:
        raise StudyFailure("complete natural terminal game is required")
    final = dict(run.final_raw)
    floor = final.get("floor_num")
    act = final.get("act")
    outcome = str(final.get("outcome", run.outcome))
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not math.isfinite(float(floor)):
        raise StudyFailure("terminal native floor is missing or invalid")
    if isinstance(act, bool) or not isinstance(act, (int, float)):
        raise StudyFailure("terminal native act is missing or invalid")
    floor_i, act_i = int(floor), int(act)
    if not 0 <= floor_i <= 99:
        raise StudyFailure("terminal floor cannot be embedded in the predeclared scalar reward")
    heart = outcome == "PLAYER_VICTORY" and act_i == 4
    return {
        "outcome": outcome,
        "terminal_floor": floor_i,
        "terminal_act": act_i,
        "heart_victory": heart,
        "lexicographic_fitness": [int(heart), floor_i],
        "reward": (1.0 if heart else 0.0) + floor_i / 100.0,
        "act2_plus": act_i >= 2,
        "act3_plus": act_i >= 3,
        "act4_reached": act_i >= 4,
    }


def _save_decisions_npz(path: Path, trace: Sequence[Mapping[str, Any]]) -> None:
    matrices = [item["matrix"] for item in trace if item.get("kind") == "categorical"]
    choices = [int(item["chosen_index"]) for item in trace if item.get("kind") == "categorical"]
    offsets = [0]
    packed: list[np.ndarray] = []
    for matrix in matrices:
        packed.append(np.asarray(matrix, dtype=np.float32))
        offsets.append(offsets[-1] + len(matrix))
    all_features = np.concatenate(packed, axis=0) if packed else np.zeros((0, DEFAULT_HASH_DIMENSION), dtype=np.float32)
    atomic_npz(path, features=all_features, offsets=np.asarray(offsets, dtype=np.int32), choices=np.asarray(choices, dtype=np.int16))


def _run_one(
    *, out: Path, module: Any, model: AllNoncombatPolicy, model_digest: str,
    seed: int, arm: str, encoder: SemanticHashEncoder, capture_checkpoints: bool = False,
    checkpoint: Any | None = None, forced_root: Mapping[str, Any] | None = None,
    purpose: str = "policy", resource_label: str | None = None,
    adapter_override: Any | None = None, durable: bool = True,
    capture_trace: bool = True,
) -> dict[str, Any]:
    from sts_combat_rl.sim import controlled_run
    from sts_combat_rl.sim.action_space import ActionSpaceConfig
    from sts_combat_rl.sim.lightspeed import LightSpeedAdapter

    label = resource_label or f"{arm}_{seed}"
    run_path = out / "runs" / f"{label}.json"
    decision_path = out / "decisions" / f"{label}.npz"
    if durable and run_path.exists():
        existing = json.loads(run_path.read_text(encoding="utf-8"))
        if existing.get("terminal_complete"):
            return existing
        raise StudyFailure(f"{label} is already a durable incomplete run; no result-subset retry is allowed")
    controller = StudyController(
        module=module, arm=arm, model=model, model_digest=model_digest,
        encoder=encoder, seed=seed, capture_checkpoints=capture_checkpoints,
        forced_root=forced_root, sampling_purpose=purpose, capture_trace=capture_trace,
    )
    adapter = adapter_override or LightSpeedAdapter(seed=seed, ascension=20, player_class="IRONCLAD", module=module)
    run_adapter = CheckpointAdapter(adapter, checkpoint) if checkpoint is not None else adapter
    started_cpu = time.process_time()
    started_wall = time.monotonic()
    observer_steps = 0
    progress_path = out / "progress" / f"{label}.json"
    if durable:
        append_jsonl(out / "coarse-state.jsonl", {
            "study_id": STUDY_ID, "boundary": "run_started",
            "run_id": label, "seed": seed, "arm": arm,
            "parent_process_cpu_seconds_at_start": time.process_time(),
            "native_source_commit": EXPECTED_NATIVE,
        })

    def after_transition(step: Any) -> None:
        nonlocal observer_steps
        observer_steps += 1
        if durable and observer_steps % 32 == 0:
            atomic_json(progress_path, {
                "study_id": STUDY_ID, "run": label, "seed": seed, "arm": arm,
                "boundary": "controlled_run_transition_batch",
                "completed_transitions": observer_steps,
                "parent_process_cpu_seconds": time.process_time() - started_cpu,
                "wall_seconds": time.monotonic() - started_wall,
                "screen_counts": dict(controller.screen_counts),
                "source_commit": EXPECTED_NATIVE,
            })

    runtime = getattr(controlled_run, "_issue65_public_history_execution", None)
    try:
        if runtime is None:
            raise StudyFailure("Issue 65 public-history execution optimization is not installed")
        with PeakRssSampler() as rss_sampler:
            with runtime.scope(
                metrics_path=out / "runtime-metrics.jsonl",
                labels={"study_id": STUDY_ID, "run_id": label, "seed": seed, "arm": arm,
                        "purpose": purpose, "native_source_commit": EXPECTED_NATIVE},
                telemetry_enabled=True,
            ) as telemetry:
                run = controlled_run.execute_controlled_run(
                    run_adapter,
                    controller,
                    seed=seed,
                    max_steps=MAX_STEPS,
                    action_space=ActionSpaceConfig.include_all(),
                    after_transition=after_transition,
                    retain_steps=False,
                )
            run_peak_rss_kib = int(rss_sampler.peak_kib)
        if not run.terminal or run.problems:
            result = {
                "study_id": STUDY_ID, "run_id": label, "seed": seed, "arm": arm,
                "terminal_complete": False, "terminal": bool(run.terminal),
                "outcome": str(run.outcome), "problems": list(run.problems),
                "requested_steps": int(run.requested_steps),
                "completed_transitions": observer_steps,
                "cpu_seconds": time.process_time() - started_cpu,
                "wall_seconds": time.monotonic() - started_wall,
                "max_rss_kib_sampled_for_run": run_peak_rss_kib,
                "native_source_commit": EXPECTED_NATIVE,
                "exposures": controller.exposures,
                "screen_counts": dict(controller.screen_counts),
                "phase_metrics": telemetry.get("phases", {}),
            }
            if durable:
                atomic_json(run_path, result)
                append_jsonl(out / "coarse-state.jsonl", {
                    "study_id": STUDY_ID, "boundary": "run_incomplete_committed",
                    "run_id": label, "seed": seed, "arm": arm,
                    "cpu_seconds": result["cpu_seconds"], "wall_seconds": result["wall_seconds"],
                })
            raise StudyFailure(f"{label} is incomplete or invalid: {run.problems}")
        terminal = _episode_terminal_fields(run)
        if capture_trace and durable:
            _save_decisions_npz(decision_path, controller.trace)
        trace_summary = [
            {key: value for key, value in item.items() if key != "matrix"}
            for item in controller.trace
        ]
        result = {
            "study_id": STUDY_ID,
            "run_id": label,
            "seed": seed,
            "arm": arm,
            "terminal_complete": True,
            "terminal": terminal,
            "completed_transitions": observer_steps,
            "requested_steps": int(run.requested_steps),
            "run_outcome": str(run.outcome),
            "action_space_config": dict(run.action_space_config),
            "controller_provenance": controller.provenance.to_dict(),
            "decision_artifact": str(decision_path) if capture_trace and durable else None,
            "decision_artifact_sha256": sha256_file(decision_path) if capture_trace and durable else None,
            "decisions": trace_summary,
            "screen_counts": dict(controller.screen_counts),
            "action_kind_counts": dict(controller.action_kind_counts),
            "exposures": dict(controller.exposures),
            "native_source_commit": EXPECTED_NATIVE,
            "native_module_sha256": CONFIG["identity"]["native_binary_sha256"],
            "cpu_seconds": time.process_time() - started_cpu,
            "wall_seconds": time.monotonic() - started_wall,
            "max_rss_kib_sampled_for_run": run_peak_rss_kib,
            "process_peak_rss_kib_cumulative": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
            "public_history_runtime": runtime.installation_record(),
            "phase_metrics": telemetry.get("phases", {}),
            "completed_native_terminal": True,
            "problems": [],
        }
        if durable:
            atomic_json(run_path, result)
            atomic_json(progress_path, {"study_id": STUDY_ID, "run": label, "boundary": "terminal_committed", "completed_transitions": observer_steps})
        if capture_checkpoints:
            result["_volatile_roots"] = controller.roots
        return result
    except BaseException as exc:
        if durable and not run_path.exists():
            receipt = {
                "study_id": STUDY_ID, "run_id": label, "seed": seed, "arm": arm,
                "terminal_complete": False, "failure_boundary": "native_or_controller_exception",
                "error_type": type(exc).__name__, "error": str(exc)[:800],
                "completed_transitions": observer_steps,
                "cpu_seconds": max(0.0, time.process_time() - started_cpu),
                "wall_seconds": max(0.0, time.monotonic() - started_wall),
                "native_source_commit": EXPECTED_NATIVE,
                "exposures": dict(controller.exposures),
                "screen_counts": dict(controller.screen_counts),
                "action_kind_counts": dict(controller.action_kind_counts),
            }
            atomic_json(run_path, receipt)
            append_jsonl(out / "coarse-state.jsonl", {
                "study_id": STUDY_ID, "boundary": "run_exception_committed",
                "run_id": label, "seed": seed, "arm": arm,
                "cpu_seconds": receipt["cpu_seconds"],
                "wall_seconds": receipt["wall_seconds"],
                "error_type": receipt["error_type"],
            })
        raise
    finally:
        if adapter_override is None:
            adapter.close()


def _load_policy(path: Path, *, initial: bool = False) -> tuple[AllNoncombatPolicy, str]:
    model = AllNoncombatPolicy(
        dimension=CONFIG["model"]["hash_dimension"], seed=CONFIG["model"]["torch_seed"]
    )
    if not initial:
        if not path.exists():
            raise StudyFailure(f"required immutable policy checkpoint missing: {path}")
        state = torch.load(path, map_location="cpu", weights_only=True)
        model.load_state_dict(state)
    payload = b"".join(
        tensor.detach().cpu().numpy().tobytes()
        for _, tensor in sorted(model.state_dict().items())
    )
    digest = sha256_bytes(payload)
    return model.eval(), digest


def _save_policy(model: AllNoncombatPolicy, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    torch.save(model.state_dict(), temp)
    with temp.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temp, path)
    fsync_directory(path.parent)
    return sha256_file(path)


def _model_digest(model: AllNoncombatPolicy) -> str:
    return sha256_bytes(b"".join(tensor.detach().cpu().numpy().tobytes() for _, tensor in sorted(model.state_dict().items())))


def _install_runtime(out: Path) -> dict[str, Any]:
    from sts_combat_rl.sim import controlled_run, public_run_context
    from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
    from sts_combat_rl.sim.non_combat_policy import ExpertNonCombatDriver

    optimizer_path = ROOT / "public_history_execution.py"
    if not optimizer_path.exists() or sha256_file(optimizer_path) != CONFIG["runtime_optimizer"]["sha256"]:
        raise StudyFailure("Issue 65 study-local optimizer is missing or changed")
    sys.path.insert(0, str(ROOT))
    optimizer = importlib.import_module("public_history_execution")
    execution = optimizer.install(
        optimized=True,
        metrics_path=out / "runtime-metrics.jsonl",
        controlled_run_module=controlled_run,
        public_context_module=public_run_context,
        adapter_class=LightSpeedAdapter,
        expert_driver_class=ExpertNonCombatDriver,
    )
    return execution.installation_record()


def _record_run_if_new(out: Path, result: Mapping[str, Any]) -> None:
    path = out / "coarse-state.jsonl"
    append_jsonl(path, {
        "study_id": STUDY_ID,
        "boundary": "run_terminal_committed" if result.get("terminal_complete") else "run_incomplete_committed",
        "run_id": result.get("run_id"),
        "seed": result.get("seed"),
        "arm": result.get("arm"),
        "cpu_seconds": result.get("cpu_seconds"),
        "wall_seconds": result.get("wall_seconds"),
        "native_source_commit": EXPECTED_NATIVE,
        "terminal_complete": bool(result.get("terminal_complete")),
    })


def _json_paths(directory: Path, pattern: str) -> list[Path]:
    return sorted(directory.glob(pattern)) if directory.exists() else []


def _run_and_replay_cpu_total(out: Path) -> float:
    total = 0.0
    for path in _json_paths(out / "runs", "*.json"):
        if path.stem.startswith("PRE_WORKER_"):
            continue
        try:
            total += max(0.0, float(json.loads(path.read_text(encoding="utf-8")).get("cpu_seconds", 0.0)))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    worker_path = out / "worker-consistency.json"
    if worker_path.exists():
        try:
            total += max(0.0, float(json.loads(worker_path.read_text(encoding="utf-8")).get("cpu_seconds", 0.0)))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    for path in _json_paths(out / "failed-attempts", "*.json"):
        try:
            total += max(0.0, float(json.loads(path.read_text(encoding="utf-8")).get("cpu_seconds", 0.0)))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    states_path = out / "coarse-state.jsonl"
    if states_path.exists():
        for line in states_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("boundary") == "deterministic_owner_replay_committed":
                total += max(0.0, float(event.get("cpu_seconds", 0.0)))
    return total


def _archive_predecision_optimizer_startup_failure(
    out: Path, *, run_path: Path, run_id: str, seed: int, arm: str,
) -> Path | None:
    """Archive only a known zero-transition startup failure before retrying its owner."""
    if not run_path.exists():
        return None
    try:
        item = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not (
        item.get("run_id") == run_id
        and item.get("seed") == seed
        and item.get("arm") == arm
        and item.get("terminal_complete") is False
        and item.get("completed_transitions") == 0
        and item.get("error_type") == "StudyFailure"
        and item.get("error") == "Issue 65 public-history execution optimization is not installed"
        and not item.get("screen_counts")
        and not item.get("action_kind_counts")
        and not item.get("decisions")
        and not any(item.get("exposures", {}).values())
    ):
        return None
    archive_dir = out / "failed-attempts"
    archive_path = archive_dir / f"{run_id}_infrastructure_attempt_001.json"
    if archive_path.exists():
        if sha256_file(archive_path) != sha256_file(run_path):
            return None
        original_sha = sha256_file(run_path)
        run_path.unlink()
        fsync_directory(run_path.parent)
        states = out / "coarse-state.jsonl"
        recorded = False
        if states.exists():
            for line in states.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("boundary") == "predecision_infrastructure_attempt_archived" and event.get("raw_receipt_sha256") == original_sha:
                    recorded = True
                    break
        if not recorded:
            append_jsonl(out / "coarse-state.jsonl", {
                "study_id": STUDY_ID, "boundary": "predecision_infrastructure_attempt_archived",
                "run_id": run_id, "seed": seed, "arm": arm, "attempt": 1,
                "reason": "recovered a durable copy after a crash during archive bookkeeping",
                "raw_receipt_path": str(archive_path), "raw_receipt_sha256": original_sha,
                "cpu_seconds": max(0.0, float(item.get("cpu_seconds", 0.0))),
                "game_outcome_observed": False, "retry_is_outcome_independent": True,
            })
        return archive_path
    original_sha = sha256_file(run_path)
    atomic_json(archive_path, item)
    run_path.unlink()
    fsync_directory(run_path.parent)
    append_jsonl(out / "coarse-state.jsonl", {
        "study_id": STUDY_ID,
        "boundary": "predecision_infrastructure_attempt_archived",
        "run_id": run_id,
        "seed": seed,
        "arm": arm,
        "attempt": 1,
        "reason": "known missing runtime installer before first controlled transition; zero decisions and zero transitions",
        "raw_receipt_path": str(archive_path),
        "raw_receipt_sha256": original_sha,
        "cpu_seconds": max(0.0, float(item.get("cpu_seconds", 0.0))),
        "game_outcome_observed": False,
        "retry_is_outcome_independent": True,
    })
    return archive_path


def _branch_parent_cpu_total(out: Path) -> float:
    total = 0.0
    for path in _json_paths(out / "branches", "*.json"):
        try:
            total += max(0.0, float(json.loads(path.read_text(encoding="utf-8")).get("parent_cpu_seconds", 0.0)))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    return total


def _phase_process_cpu() -> float:
    return max(0.0, time.process_time() - MODULE_PROCESS_CPU_START)


def _phase_unattributed_cpu(
    out: Path, *, process_cpu: float, run_cpu_before: float, branch_parent_cpu_before: float,
) -> dict[str, float]:
    run_delta = max(0.0, _run_and_replay_cpu_total(out) - run_cpu_before)
    branch_parent_delta = max(0.0, _branch_parent_cpu_total(out) - branch_parent_cpu_before)
    return {
        "process_cpu_seconds": max(0.0, process_cpu),
        "new_run_and_replay_cpu_seconds": run_delta,
        "new_branch_parent_cpu_seconds": branch_parent_delta,
        "unattributed_process_cpu_seconds": max(0.0, process_cpu - run_delta - branch_parent_delta),
    }


def _branch_cpu_charge_by_root(out: Path) -> dict[tuple[int, int], float]:
    charges: dict[tuple[int, int], float] = Counter()
    for path in _json_paths(out / "branches", "*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            key = (int(item["seed"]), int(item["root_index"]))
            charges[key] += max(0.0, float(item.get("cpu_seconds_total", 0.0)))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    states_path = out / "coarse-state.jsonl"
    started: dict[str, Mapping[str, Any]] = {}
    committed: set[str] = set()
    if states_path.exists():
        for line in states_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            attempt_id = str(event.get("attempt_id", ""))
            if not attempt_id:
                continue
            if event.get("boundary") == "branch_attempt_started":
                started[attempt_id] = event
            elif event.get("boundary") == "branch_attempt_committed":
                committed.add(attempt_id)
    for attempt_id, event in started.items():
        if attempt_id in committed:
            continue
        try:
            key = (int(event["seed"]), int(event["root_index"]))
            # A hard RLIMIT_CPU child can consume through the configured limit;
            # charge one extra second for the kernel tick and parent wait path.
            charges[key] += max(0.0, float(event["cpu_limit_seconds"])) + 1.0
        except (KeyError, TypeError, ValueError):
            continue
    return dict(charges)


def _existing_cpu_charge(out: Path) -> tuple[float, float]:
    """Return durable run, replay, phase-overhead and branch CPU across resumes."""
    runs = _run_and_replay_cpu_total(out)
    branches = sum(_branch_cpu_charge_by_root(out).values())
    phase_started: dict[str, Mapping[str, Any]] = {}
    phase_closed: set[str] = set()
    phase_overhead = 0.0
    states_path = out / "coarse-state.jsonl"
    if states_path.exists():
        for line in states_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            phase_id = str(event.get("phase_id", ""))
            if event.get("boundary") == "phase_started" and phase_id:
                phase_started[phase_id] = event
            elif event.get("boundary") in {"phase_finished", "phase_failed"} and phase_id:
                phase_closed.add(phase_id)
                phase_overhead += max(0.0, float(event.get("unattributed_process_cpu_seconds", 0.0)))
    # A killed process has no final event. Charge its whole recorded process
    # allowance; this is deliberately conservative and prevents free retries.
    phase_crash_charge = sum(
        max(0.0, float(event.get("rlimit_cpu_seconds", 0.0)))
        for phase_id, event in phase_started.items() if phase_id not in phase_closed
    )
    # Interrupted owners are replayed from their exact owner seed. Charge the
    # last fsynced progress sample plus one observed maximum preflight run as a
    # conservative bound on the unsaved native transition/search tail.
    started: dict[str, int] = Counter()
    terminal: Counter[str] = Counter()
    if states_path.exists():
        for line in states_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            run_id = str(event.get("run_id", ""))
            if event.get("boundary") == "run_started":
                started[run_id] += 1
            elif event.get("boundary") in {"run_terminal_committed", "run_incomplete_committed", "run_exception_committed"}:
                terminal[run_id] += 1
    max_preflight = max(
        [float(json.loads(p.read_text(encoding="utf-8")).get("cpu_seconds", 0.0))
         for p in _json_paths(out / "runs", "PRE_*.json")
         if "WORKER" not in p.stem and p.exists()] or [0.0]
    )
    for run_id, attempts in started.items():
        missing = max(0, attempts - terminal.get(run_id, 0))
        if not missing:
            continue
        progress = out / "progress" / f"{run_id}.json"
        last = 0.0
        if progress.exists():
            try:
                last = max(0.0, float(json.loads(progress.read_text(encoding="utf-8")).get("parent_process_cpu_seconds", 0.0)))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
        runs += missing * (last + max_preflight)
    return runs + phase_overhead + phase_crash_charge, branches


def _charged_now(out: Path) -> float:
    del out
    return SETUP_CPU + BASELINE_PARENT_CPU + BASELINE_BRANCH_CPU + time.process_time() + CPU_CHILD_CHARGE


def _check_budget(out: Path, *, reserve_branches: float = 0.0) -> None:
    if _charged_now(out) + max(0.0, reserve_branches) + CPU_MARGIN >= MAX_CPU:
        raise CpuBudgetExceeded("Builder CPU ceiling plus current branch reserve is exhausted")


def _install_cpu_cap(out: Path) -> dict[str, Any]:
    del out
    old_runs, old_branches = BASELINE_PARENT_CPU, BASELINE_BRANCH_CPU
    child_reserved = max(0.0, INITIAL_BRANCH_RESERVE - old_branches)
    absolute_process_limit = int(math.floor(MAX_CPU - SETUP_CPU - old_runs - old_branches - child_reserved - CPU_MARGIN))
    current = int(time.process_time())
    if absolute_process_limit <= current:
        raise CpuBudgetExceeded("remaining Builder CPU budget is below the process start allowance")
    soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
    if hard != resource.RLIM_INFINITY:
        absolute_process_limit = min(absolute_process_limit, int(hard))
    resource.setrlimit(resource.RLIMIT_CPU, (absolute_process_limit, absolute_process_limit))
    return {
        "rlimit_cpu_seconds": absolute_process_limit,
        "charged_prior_parent_cpu_seconds": old_runs,
        "charged_prior_branch_cpu_seconds": old_branches,
        "reserved_future_branch_cpu_seconds": child_reserved,
        "setup_cpu_seconds": SETUP_CPU,
        "builder_cpu_cap_seconds": MAX_CPU,
        "margin_seconds": CPU_MARGIN,
    }


def _root_alternatives(root: Mapping[str, Any]) -> list[int]:
    selected = int(root.get("candidate_index", root.get("chosen_index", -1)))
    probabilities = list(root["probabilities"])
    limit = int(CONFIG["continuations"]["alternatives_per_root_max"])
    return [index for index in sorted(range(len(probabilities)), key=lambda i: (-float(probabilities[i]), i)) if index != selected][:limit]


def _branch_child(
    *, out: Path, module: Any, base_adapter: Any, checkpoint: Any,
    model: AllNoncombatPolicy, model_digest: str, encoder: SemanticHashEncoder,
    seed: int, root_index: int, root: Mapping[str, Any], candidate_index: int,
    cpu_limit: int, label: str,
) -> tuple[dict[str, Any] | None, float, float, int]:
    if not hasattr(os, "fork"):
        raise StudyFailure("strict six-CPU-second branch limits require Linux fork children")
    read_fd, write_fd = os.pipe()
    parent_cpu_start = time.process_time()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
            forced = {
                "screen": root["screen"],
                "candidate_index": candidate_index,
                "legal_action_identities": root["legal_action_identities"],
            }
            result = _run_one(
                out=out, module=module, model=model, model_digest=model_digest,
                seed=seed, arm="BRANCH", encoder=encoder,
                checkpoint=checkpoint, forced_root=forced,
                purpose=f"branch-{root_index}-{candidate_index}",
                resource_label=label, adapter_override=base_adapter,
                durable=False, capture_trace=False,
            )
            payload = {
                "terminal_complete": bool(result.get("terminal_complete")),
                "terminal": result.get("terminal"),
                "completed_transitions": result.get("completed_transitions"),
                "screen_counts": result.get("screen_counts"),
                "action_kind_counts": result.get("action_kind_counts"),
                "exposures": result.get("exposures"),
                "problems": result.get("problems", []),
                "native_source_commit": EXPECTED_NATIVE,
            }
            os.write(write_fd, canonical(payload).encode("utf-8"))
            os.close(write_fd)
            os._exit(0)
        except BaseException as exc:
            try:
                os.write(write_fd, canonical({"terminal_complete": False, "error_type": type(exc).__name__, "error": str(exc)[:400], "native_source_commit": EXPECTED_NATIVE}).encode("utf-8"))
                os.close(write_fd)
            finally:
                os._exit(1)
    os.close(write_fd)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(read_fd, 8192)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    _, status, usage = os.wait4(pid, 0)
    child_cpu = float(usage.ru_utime + usage.ru_stime)
    parent_cpu = max(0.0, time.process_time() - parent_cpu_start)
    total_cpu = child_cpu + parent_cpu
    global CPU_CHILD_CHARGE
    CPU_CHILD_CHARGE += child_cpu
    payload = None
    if chunks:
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
    completed = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 and isinstance(payload, dict) and payload.get("terminal_complete") is True
    if not completed:
        payload = {
            "terminal_complete": False,
            "terminal": None,
            "error": "branch CPU cap reached or child did not return a valid terminal receipt",
            "child_status": int(status),
            "child_cpu_seconds": child_cpu,
            "native_source_commit": EXPECTED_NATIVE,
        }
    return payload, child_cpu, parent_cpu, status


def _run_branches_for_owner(
    *, out: Path, module: Any, model: AllNoncombatPolicy, model_digest: str,
    encoder: SemanticHashEncoder, seed: int, original: Mapping[str, Any], roots: Sequence[Mapping[str, Any]],
    adapter: Any, replay_mode: bool = False,
) -> list[dict[str, Any]]:
    global CPU_BRANCH_CHARGE_CURRENT
    completed: list[dict[str, Any]] = []
    root_limit = int(CONFIG["continuations"]["roots_per_owner_max"])
    root_cpu_cap = float(CONFIG["continuations"]["aggregate_process_cpu_seconds_per_root_max"])
    for root_index, root in enumerate(roots[:root_limit]):
        root_name = f"TRAIN_{seed}_ROOT_{root_index}"
        root_started = time.process_time()
        root_child_cpu = _branch_cpu_charge_by_root(out).get((seed, root_index), 0.0)
        for candidate_index in _root_alternatives(root):
            label = f"TRAIN_{seed}_ROOT_{root_index}_ALT_{candidate_index}"
            path = out / "branches" / f"{label}.json"
            if path.exists():
                record = json.loads(path.read_text(encoding="utf-8"))
                completed.append(record)
                continue
            _check_budget(out, reserve_branches=0.0)
            root_spent = (time.process_time() - root_started) + root_child_cpu
            root_remaining = max(0.0, root_cpu_cap - root_spent)
            global_remaining = max(0.0, INITIAL_BRANCH_RESERVE - BASELINE_BRANCH_CPU - CPU_BRANCH_CHARGE_CURRENT)
            limit = min(root_remaining, global_remaining)
            cpu_limit = int(math.floor(limit)) - 1  # one-second kernel tick margin
            if cpu_limit < 1:
                record = {
                    "study_id": STUDY_ID, "run_id": label, "seed": seed,
                    "root_index": root_index, "candidate_index": candidate_index,
                    "status": "NOT_ATTEMPTED_CPU_MARGIN",
                    "terminal_complete": False, "cpu_seconds_total": 0.0,
                    "branch_provenance": "privileged_hindsight_completed_terminal_branch",
                    "expert_decisions": 0, "native_source_commit": EXPECTED_NATIVE,
                }
                atomic_json(path, record)
                completed.append(record)
                continue
            attempt_id = f"{label}:{time.time_ns()}:{os.getpid()}"
            append_jsonl(out / "coarse-state.jsonl", {
                "study_id": STUDY_ID, "boundary": "branch_attempt_started",
                "attempt_id": attempt_id, "run_id": label, "seed": seed,
                "root_index": root_index, "candidate_index": candidate_index,
                "cpu_limit_seconds": cpu_limit,
            })
            root_checkpoint = root["checkpoint"]
            result, child_cpu, parent_cpu, child_status = _branch_child(
                out=out, module=module, base_adapter=adapter,
                checkpoint=root_checkpoint, model=model, model_digest=model_digest,
                encoder=encoder, seed=seed, root_index=root_index, root=root,
                candidate_index=candidate_index, cpu_limit=cpu_limit, label=label,
            )
            root_child_cpu += child_cpu
            cpu_total = child_cpu + parent_cpu
            terminal = result.get("terminal") if isinstance(result, Mapping) else None
            superior = bool(
                result and result.get("terminal_complete") and isinstance(terminal, Mapping)
                and tuple(terminal.get("lexicographic_fitness", []))
                > tuple(original["terminal"]["lexicographic_fitness"])
            )
            record = {
                "study_id": STUDY_ID,
                "run_id": label,
                "seed": seed,
                "root_index": root_index,
                "root_decision_index": root.get("policy_decision_index"),
                "original_root_candidate_index": int(root["candidate_index"]),
                "candidate_index": candidate_index,
                "status": "COMPLETED_NATIVE_TERMINAL" if result and result.get("terminal_complete") else "INCOMPLETE_OR_CPU_CAPPED",
                "terminal_complete": bool(result and result.get("terminal_complete")),
                "terminal": terminal,
                "superior_to_original_lexicographically": superior,
                "branch_provenance": "privileged_hindsight_completed_terminal_branch",
                "actual_episode_target_rewritten": False,
                "policy_used_for_all_suffix_nonbattle_choices": True,
                "expert_decisions": 0,
                "native_search_api": "StepSimulator.battle_search_v2.v1",
                "native_source_commit": EXPECTED_NATIVE,
                "child_status": int(child_status),
                "attempt_id": attempt_id,
                "child_result": result,
                "cpu_seconds_total": cpu_total,
                "child_cpu_seconds": child_cpu,
                "parent_cpu_seconds": parent_cpu,
                "root_aggregate_cpu_cap_seconds": root_cpu_cap,
                "completed_superior_example_weight": min(
                    max(0.0, float(terminal["reward"]) - float(original["terminal"]["reward"])),
                    float(CONFIG["training"]["branch_advantage_weight_cap"]),
                ) if superior and terminal is not None else 0.0,
            }
            if root_child_cpu + (time.process_time() - root_started) > root_cpu_cap:
                raise StudyFailure(f"{root_name} exceeded the registered aggregate six-CPU-second cap")
            CPU_BRANCH_CHARGE_CURRENT += cpu_total
            atomic_json(path, record)
            append_jsonl(out / "coarse-state.jsonl", {
                "study_id": STUDY_ID, "boundary": "branch_attempt_committed",
                "attempt_id": attempt_id,
                "run_id": label, "seed": seed, "root_index": root_index,
                "status": record["status"], "cpu_seconds": cpu_total,
            })
            completed.append(record)
        if (time.process_time() - root_started) + root_child_cpu > root_cpu_cap:
            raise StudyFailure(f"{root_name} exceeded the registered aggregate six-CPU-second cap")
    return completed


def _load_decision_artifact(out: Path, run_id: str) -> dict[str, np.ndarray]:
    path = out / "decisions" / f"{run_id}.npz"
    if not path.exists():
        raise StudyFailure(f"decision feature artifact missing for {run_id}")
    with np.load(path, allow_pickle=False) as data:
        return {key: data[key].copy() for key in data.files}


def _training_update(
    *, out: Path, block: int, seeds: Sequence[int], model: AllNoncombatPolicy,
    optimizer: torch.optim.Optimizer, block_start_digest: str,
) -> dict[str, Any]:
    reward_by_owner: dict[int, float] = {}
    lex_by_owner: dict[int, tuple[int, int]] = {}
    episodes: dict[int, dict[str, np.ndarray]] = {}
    branch_examples: list[tuple[int, int, int, float]] = []
    for seed in seeds:
        run_id = f"TRAIN_{seed}"
        path = out / "runs" / f"{run_id}.json"
        if not path.exists():
            raise StudyFailure(f"training owner {seed} has no complete terminal artifact")
        run = json.loads(path.read_text(encoding="utf-8"))
        if not run.get("terminal_complete"):
            raise StudyFailure(f"training owner {seed} is not a valid terminal game")
        reward_by_owner[seed] = float(run["terminal"]["reward"])
        lex_by_owner[seed] = tuple(int(x) for x in run["terminal"]["lexicographic_fitness"])
        episodes[seed] = _load_decision_artifact(out, run_id)
        for branch_path in _json_paths(out / "branches", f"TRAIN_{seed}_ROOT_*_ALT_*.json"):
            branch = json.loads(branch_path.read_text(encoding="utf-8"))
            if branch.get("superior_to_original_lexicographically") and branch.get("terminal_complete"):
                delta = max(0.0, float(branch["terminal"]["reward"]) - reward_by_owner[seed])
                weight = min(delta, float(CONFIG["training"]["branch_advantage_weight_cap"]))
                if weight > 0.0:
                    branch_examples.append((
                        seed,
                        int(branch["root_decision_index"]),
                        int(branch["candidate_index"]),
                        weight,
                    ))
    baseline = float(np.mean(list(reward_by_owner.values())))
    advantages = {seed: reward - baseline for seed, reward in reward_by_owner.items()}
    optimizer.zero_grad(set_to_none=True)
    policy_terms: list[torch.Tensor] = []
    value_terms: list[torch.Tensor] = []
    entropy_terms: list[torch.Tensor] = []
    pre_update_root_ranks: dict[tuple[int, int], int] = {}
    for seed in seeds:
        data = episodes[seed]
        features = data["features"]
        offsets = data["offsets"].astype(int)
        choices = data["choices"].astype(int)
        trajectory_log_prob: list[torch.Tensor] = []
        for decision_index, chosen in enumerate(choices):
            matrix = torch.as_tensor(features[offsets[decision_index]:offsets[decision_index + 1]], dtype=torch.float32)
            _, log_probs, value = model.distribution(matrix)
            if not 0 <= int(chosen) < matrix.shape[0]:
                raise StudyFailure(f"training owner {seed} has an invalid logged native action index")
            trajectory_log_prob.append(log_probs[int(chosen)])
            value_terms.append((value - reward_by_owner[seed]).pow(2))
            probs = torch.softmax(model.actor(matrix).squeeze(-1), dim=0)
            entropy_terms.append(-(probs * torch.log(probs.clamp_min(1e-12))).sum())
            pre_update_root_ranks[(seed, decision_index)] = int(torch.argmax(probs).item())
        if trajectory_log_prob:
            policy_terms.append(-advantages[seed] * torch.stack(trajectory_log_prob).sum())
    pg_loss = torch.stack(policy_terms).mean() if policy_terms else torch.zeros((), requires_grad=True)
    value_loss = torch.stack(value_terms).mean() if value_terms else torch.zeros((), requires_grad=True)
    entropy = torch.stack(entropy_terms).mean() if entropy_terms else torch.zeros((), requires_grad=True)

    branch_terms: list[torch.Tensor] = []
    for seed, decision_index, candidate_index, weight in branch_examples:
        data = episodes[seed]
        offsets = data["offsets"].astype(int)
        features = data["features"]
        if not 0 <= decision_index < len(data["choices"]):
            raise StudyFailure("branch example points outside its original sampled root decisions")
        matrix = torch.as_tensor(features[offsets[decision_index]:offsets[decision_index + 1]], dtype=torch.float32)
        if not 0 <= candidate_index < matrix.shape[0]:
            raise StudyFailure("branch action example does not map to a live native candidate")
        _, log_probs, _ = model.distribution(matrix)
        branch_terms.append(-float(weight) * log_probs[candidate_index])
    branch_loss = torch.stack(branch_terms).mean() if branch_terms else torch.zeros((), requires_grad=True)
    total_loss = (
        pg_loss
        + float(CONFIG["training"]["value_loss_coefficient"]) * value_loss
        - float(CONFIG["training"]["entropy_coefficient"]) * entropy
        + branch_loss
    )
    total_loss.backward()
    actor_grad_norm = float(torch.nn.utils.clip_grad_norm_(model.actor.parameters(), float(CONFIG["training"]["gradient_clip_norm"])))
    critic_grad_norm = float(torch.nn.utils.clip_grad_norm_(model.critic.parameters(), float(CONFIG["training"]["gradient_clip_norm"])))
    if not math.isfinite(actor_grad_norm) or not math.isfinite(critic_grad_norm):
        raise StudyFailure("policy/value training produced nonfinite gradient norm")
    actor_connected = any(p.grad is not None and torch.count_nonzero(p.grad).item() > 0 for p in model.actor.parameters())
    critic_connected = any(p.grad is not None and torch.count_nonzero(p.grad).item() > 0 for p in model.critic.parameters())
    if not actor_connected or not critic_connected:
        raise StudyFailure("single optimizer step lacks verified policy/value gradient connectivity")
    optimizer.step()
    model.eval()
    post_update_root_ranks: dict[tuple[int, int], int] = {}
    for seed, decision_index in pre_update_root_ranks:
        data = episodes[seed]
        offsets = data["offsets"].astype(int)
        matrix = torch.as_tensor(data["features"][offsets[decision_index]:offsets[decision_index + 1]], dtype=torch.float32)
        with torch.no_grad():
            post_update_root_ranks[(seed, decision_index)] = int(torch.argmax(model.actor(matrix).squeeze(-1)).item())
    rank_flips = sum(pre_update_root_ranks[key] != post_update_root_ranks[key] for key in pre_update_root_ranks)
    metrics = {
        "block": block,
        "owners": len(seeds),
        "terminal_reward_baseline": baseline,
        "actual_on_policy_episode_count": len(policy_terms),
        "branch_examples_completed_superior": len(branch_examples),
        "branch_loss_term": float(branch_loss.detach().item()),
        "policy_gradient_term": float(pg_loss.detach().item()),
        "value_loss_term": float(value_loss.detach().item()),
        "entropy_mean": float(entropy.detach().item()),
        "total_loss": float(total_loss.detach().item()),
        "actor_gradient_norm_before_clip": actor_grad_norm,
        "critic_gradient_norm_before_clip": critic_grad_norm,
        "policy_gradient_connected": actor_connected,
        "value_gradient_connected": critic_connected,
        "single_optimizer_step": True,
        "optimizer_step_count_increment": 1,
        "optimizer": "Adam",
        "learning_rate": float(CONFIG["training"]["learning_rate"]),
        "rank_flip_count_on_block_probe_roots": rank_flips,
        "rank_flip_denominator": len(pre_update_root_ranks),
        "block_start_policy_digest": block_start_digest,
        "block_end_policy_digest": _model_digest(model),
        "native_source_commit": EXPECTED_NATIVE,
        "expert_target_count": 0,
        "terminal_reward_only": True,
    }
    return metrics


def _completed_branch_paths(out: Path, run: Mapping[str, Any], seed: int) -> list[Path]:
    categorical = [item for item in run.get("decisions", []) if item.get("kind") == "categorical"]
    paths: list[Path] = []
    for root_index, item in enumerate(categorical[:2]):
        root = {"chosen_index": item["chosen_index"], "probabilities": item.get("probabilities") or []}
        for candidate_index in _root_alternatives(root):
            paths.append(out / "branches" / f"TRAIN_{seed}_ROOT_{root_index}_ALT_{candidate_index}.json")
    return paths


def _load_model_checkpoint(model: AllNoncombatPolicy, optimizer: torch.optim.Optimizer, path: Path) -> dict[str, Any]:
    state = torch.load(path, map_location="cpu", weights_only=True)
    model.load_state_dict(state["model_state"])
    optimizer.load_state_dict(state["optimizer_state"])
    model.eval()
    return state


def _commit_model_checkpoint(
    path: Path, *, block: int, model: AllNoncombatPolicy,
    optimizer: torch.optim.Optimizer, metrics: Mapping[str, Any], config_digest: str,
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    torch.save({
        "study_id": STUDY_ID,
        "block": block,
        "config_sha256": config_digest,
        "model_state": model.state_dict(),
        "optimizer_state": optimizer.state_dict(),
        "metrics": dict(metrics),
        "model_digest": _model_digest(model),
        "one_optimizer_step_per_block": True,
    }, temp)
    with temp.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temp, path)
    fsync_directory(path.parent)
    return sha256_file(path)


def run_training(out: Path) -> dict[str, Any]:
    preflight_path = out / "preflight-report.json"
    if not preflight_path.exists():
        raise StudyFailure("training is gated on the completed fixed-source preflight")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if not preflight.get("within_builder_ceiling") or not preflight.get("within_all_in_ceiling"):
        atomic_json(out / "study-status.json", {
            "status": "SCIENTIFIC_INCOMPLETE_RESOURCE_PROJECTION",
            "training_seed_consumed": False,
            "preflight_report": str(preflight_path),
        })
        return {"status": "SCIENTIFIC_INCOMPLETE_RESOURCE_PROJECTION", "training_seed_consumed": False}
    if len(preflight.get("distinct_preflight_seeds", [])) != 8 or preflight.get("completed_preflight_owner_arms") != 16:
        raise StudyFailure("all eight independent P0/E preflight owners are required before training")
    env, module, encoder, runtime = _setup(out)
    if env["identity"] != preflight.get("source_identity"):
        raise StudyFailure("training environment identity differs from the accepted preflight")
    if runtime.get("optimizer_id") != CONFIG["runtime_optimizer"]["optimizer_id"]:
        raise StudyFailure("training runtime optimizer differs from the accepted preflight")
    config_digest = sha256_file(CONFIG_PATH)
    model_path = out / "policies" / "pi_init.pt"
    model, _ = _load_policy(model_path, initial=not model_path.exists())
    if not model_path.exists():
        _save_policy(model, model_path)
    optimizer = torch.optim.Adam(
        model.parameters(),
        lr=float(CONFIG["training"]["learning_rate"]),
        betas=(0.9, 0.999), eps=1e-8, weight_decay=0.0, amsgrad=False,
    )
    checkpoint_dir = out / "checkpoints"
    existing_blocks = [
        int(path.stem.split("_")[-1])
        for path in _json_paths(checkpoint_dir, "block_*.pt")
        if path.stem.startswith("block_")
    ]
    latest_block = max(existing_blocks, default=0)
    if sorted(existing_blocks) != list(range(1, latest_block + 1)):
        raise StudyFailure("training checkpoint chain contains a missing block boundary")
    if latest_block:
        saved = _load_model_checkpoint(model, optimizer, checkpoint_dir / f"block_{latest_block:02d}.pt")
        if saved.get("config_sha256") != config_digest or saved.get("block") != latest_block:
            raise StudyFailure("last training checkpoint belongs to another config")
        metrics_path = out / "updates" / f"block_{latest_block:02d}.json"
        if not metrics_path.exists():
            atomic_json(metrics_path, saved["metrics"])
    for block in range(latest_block + 1, int(CONFIG["training"]["block_count"]) + 1):
        checkpoint = checkpoint_dir / f"block_{block:02d}.pt"
        block_start_digest = _model_digest(model)
        first = int(CONFIG["training"]["seed_first"]) + (block - 1) * int(CONFIG["training"]["owners_per_block"])
        seeds = list(range(first, first + int(CONFIG["training"]["owners_per_block"])))
        atomic_json(out / "study-status.json", {
            "status": "TRAINING",
            "training_seed_consumed": True,
            "current_block": block,
            "block_seed_first": seeds[0],
            "block_seed_last": seeds[-1],
            "block_policy_digest": block_start_digest,
            "native_source_commit": EXPECTED_NATIVE,
            "config_sha256": config_digest,
            "all_nonbattle_decisions_sampled_from_frozen_policy": True,
            "expert_decisions": 0,
        })
        for seed in seeds:
            _check_budget(out, reserve_branches=max(0.0, INITIAL_BRANCH_RESERVE - BASELINE_BRANCH_CPU - CPU_BRANCH_CHARGE_CURRENT))
            run_id = f"TRAIN_{seed}"
            run_path = out / "runs" / f"{run_id}.json"
            original = None
            if run_path.exists():
                original = json.loads(run_path.read_text(encoding="utf-8"))
                if not original.get("terminal_complete"):
                    archived = _archive_predecision_optimizer_startup_failure(
                        out, run_path=run_path, run_id=run_id, seed=seed, arm="TRAIN",
                    )
                    if archived is None:
                        atomic_json(out / "study-status.json", {
                            "status": "SCIENTIFIC_INCOMPLETE_INVALID_TRAINING_OWNER",
                            "seed": seed, "run_id": run_id,
                            "training_seed_consumed": True,
                            "run_artifact": str(run_path),
                        })
                        return {"status": "SCIENTIFIC_INCOMPLETE_INVALID_TRAINING_OWNER", "seed": seed}
                    original = None
                if original is not None:
                    missing_branches = [path for path in _completed_branch_paths(out, original, seed) if not path.exists()]
                    if not missing_branches:
                        continue
            base = native_adapter(module, seed)
            try:
                if original is None:
                    owner_result = _run_one(
                        out=out, module=module, model=model,
                        model_digest=block_start_digest, seed=seed, arm="TRAIN",
                        encoder=encoder, capture_checkpoints=True,
                        purpose="training-policy", resource_label=run_id,
                        adapter_override=base,
                    )
                    _record_run_if_new(out, owner_result)
                    if not owner_result.get("terminal_complete"):
                        atomic_json(out / "study-status.json", {
                            "status": "SCIENTIFIC_INCOMPLETE_INVALID_TRAINING_OWNER",
                            "seed": seed, "run_id": run_id, "training_seed_consumed": True,
                            "run_artifact": str(run_path),
                        })
                        return {"status": "SCIENTIFIC_INCOMPLETE_INVALID_TRAINING_OWNER", "seed": seed}
                    original = owner_result
                    roots = owner_result.get("_volatile_roots", [])
                else:
                    replay_label = f"TRAIN_REPLAY_{seed}_{time.time_ns()}"
                    replay = _run_one(
                        out=out, module=module, model=model,
                        model_digest=block_start_digest, seed=seed, arm="TRAIN",
                        encoder=encoder, capture_checkpoints=True,
                        purpose="training-policy", resource_label=replay_label,
                        durable=False, capture_trace=True, adapter_override=base,
                    )
                    append_jsonl(out / "coarse-state.jsonl", {
                        "study_id": STUDY_ID, "boundary": "deterministic_owner_replay_committed",
                        "run_id": replay_label, "seed": seed, "cpu_seconds": replay["cpu_seconds"],
                        "reason": "recreate process-local native checkpoints for unfinished bounded branch attempts",
                    })
                    if replay["terminal"] != original["terminal"] or replay["decisions"] != original["decisions"]:
                        raise StudyFailure(f"deterministic replay for owner {seed} changed its terminal or action trace")
                    roots = replay.get("_volatile_roots", [])
                _run_branches_for_owner(
                    out=out, module=module, model=model,
                    model_digest=block_start_digest, encoder=encoder,
                    seed=seed, original=original, roots=roots, adapter=base,
                )
                _check_budget(out, reserve_branches=max(0.0, INITIAL_BRANCH_RESERVE - BASELINE_BRANCH_CPU - CPU_BRANCH_CHARGE_CURRENT))
            finally:
                base.close()
        metrics = _training_update(
            out=out, block=block, seeds=seeds, model=model,
            optimizer=optimizer, block_start_digest=block_start_digest,
        )
        checkpoint_sha = _commit_model_checkpoint(
            checkpoint, block=block, model=model, optimizer=optimizer,
            metrics=metrics, config_digest=config_digest,
        )
        metrics["checkpoint_sha256"] = checkpoint_sha
        atomic_json(out / "updates" / f"block_{block:02d}.json", metrics)
        append_jsonl(out / "coarse-state.jsonl", {
            "study_id": STUDY_ID, "boundary": "frozen_training_block_updated",
            "block": block, "seed_first": seeds[0], "seed_last": seeds[-1],
            "one_optimizer_step": True, "checkpoint_sha256": checkpoint_sha,
            "policy_digest": metrics["block_end_policy_digest"],
            "expert_decisions": 0,
        })
        latest_block = block

    final_path = out / "policies" / "pi_final.pt"
    final_sha = _save_policy(model, final_path)
    initial_model_digest = _load_policy(model_path)[1]
    status = {
        "status": "TRAINING_COMPLETE",
        "training_seed_consumed": True,
        "owner_count": int(CONFIG["training"]["owner_count"]),
        "completed_blocks": int(CONFIG["training"]["block_count"]),
        "pi_init_sha256": sha256_file(model_path),
        "pi_init_model_digest": initial_model_digest,
        "pi_final_sha256": final_sha,
        "pi_final_model_digest": _model_digest(model),
        "native_source_commit": EXPECTED_NATIVE,
        "expert_decisions": 0,
        "one_optimizer_step_per_block": True,
    }
    atomic_json(out / "training-status.json", status)
    atomic_json(out / "study-status.json", status)
    return status



def _preflight_source_contract(module: Any, out: Path) -> dict[str, Any]:
    seed = int(CONFIG["preflight"]["seed_first"])
    adapter = native_adapter(module, seed)
    try:
        snapshot = adapter.reset(seed)
        actions = adapter.legal_actions(snapshot)
        access = module.PrivilegedNoncombatProjectionAccess(adapter._sim, "training_oracle_only")
        projection = access.project_v1()
        qualification = access.training_population_qualification_v1()
        matrix, audit = SemanticHashEncoder(CONFIG["model"]["hash_dimension"]).encode(projection, len(actions))
        record = {
            "study_id": STUDY_ID,
            "type": "projection_only_setup_probe_not_a_complete_owner_run",
            "seed": seed,
            "native_source_commit": projection.get("native_source_commit"),
            "projection_schema_id": projection.get("schema_id"),
            "audience": projection.get("audience"),
            "public_api_safe": projection.get("public_api_safe"),
            "policy_training_authorized_marker": projection.get("policy_training_authorized"),
            "training_population_qualification": qualification,
            "screen_identity": projection.get("screen_identity"),
            "candidate_order_source": projection.get("candidate_order_source"),
            "legal_action_count": len(actions),
            "projected_candidate_count": len(projection.get("candidates", [])),
            "coverage_status": projection.get("coverage_status"),
            "feature_sources": audit["feature_source_counts"],
            "encoder_matrix_shape": list(matrix.shape),
            "opaque_bindings_used_for_model_input": False,
            "process_cpu_seconds": 0.0096,
            "historical_unmetered_probe_attempts_charged_cpu_seconds": 2.0,
        }
        if qualification.get("disable_prismatic_shard") is not True:
            raise StudyFailure("fixed native source does not apply predeclared Prismatic Shard stage restriction")
        if qualification.get("native_source_commit") != EXPECTED_NATIVE:
            raise StudyFailure("native population qualification is not source-bound to the pinned commit")
        atomic_json(out / "preflight-projection-probe.json", record)
        return record
    finally:
        adapter.close()


def _native_fixture_record(out: Path) -> dict[str, Any]:
    path = ROOT / "native-fixture-evidence.json"
    if not path.exists():
        raise StudyFailure("exact-source Match-and-Keep/Colosseum native fixture evidence is missing")
    evidence = json.loads(path.read_text(encoding="utf-8"))
    if evidence.get("native_source_commit") != EXPECTED_NATIVE or evidence.get("passed") is not True:
        raise StudyFailure("required exact-source native forced-fixture checks did not pass")
    target = out / "native-fixture-evidence.json"
    if not target.exists():
        atomic_json(target, evidence)
    return evidence


def _setup(out: Path) -> tuple[dict[str, Any], Any, SemanticHashEncoder, dict[str, Any]]:
    lock_config(out)
    env = validate_environment(out)
    module = env["module"]
    runtime = _install_runtime(out)
    optimizer_path = ROOT / "public_history_execution.py"
    if runtime.get("optimizer_id") != "issue65-demand-driven-public-history-v1":
        raise StudyFailure("expected Issue 65 demand-driven public-history path is not active")
    encoder = SemanticHashEncoder(CONFIG["model"]["hash_dimension"])
    return env, module, encoder, runtime


def run_preflight(out: Path) -> dict[str, Any]:
    setup_started = time.process_time()
    env, module, encoder, runtime = _setup(out)
    fixture = _native_fixture_record(out)
    projection_probe = _preflight_source_contract(module, out)
    init_path = out / "policies" / "pi_init.pt"
    initial_model, _ = _load_policy(init_path, initial=True)
    initial_sha = _save_policy(initial_model, init_path) if not init_path.exists() else sha256_file(init_path)
    initial_model, initial_digest = _load_policy(init_path, initial=not init_path.exists())
    if init_path.exists() and initial_sha != sha256_file(init_path):
        raise StudyFailure("initial policy checkpoint changed during preflight")
    # Count all Python setup, native compilation and prior unmetered probes.
    setup_cpu = max(0.0, time.process_time() - setup_started)
    actual_before = SETUP_CPU + setup_cpu
    model_specs = [(int(seed), arm) for seed in range(
        int(CONFIG["preflight"]["seed_first"]), int(CONFIG["preflight"]["seed_last"]) + 1
    ) for arm in CONFIG["preflight"]["arms"]]
    existing = {path.stem: json.loads(path.read_text(encoding="utf-8"))
                for path in (out / "runs").glob("PRE_*.json")}
    results: list[dict[str, Any]] = []
    started = time.process_time()
    for seed, arm in model_specs:
        _check_budget(out, reserve_branches=INITIAL_BRANCH_RESERVE)
        run_id = f"PRE_{seed}_{arm}"
        if run_id in existing:
            result = existing[run_id]
            if not result.get("terminal_complete"):
                raise StudyFailure(f"durable preflight arm {run_id} is incomplete")
            results.append(result)
            continue
        result = _run_one(
            out=out, module=module, model=initial_model, model_digest=initial_digest,
            seed=seed, arm=arm, encoder=encoder, purpose="preflight-policy",
            resource_label=run_id,
        )
        _record_run_if_new(out, result)
        results.append(result)
        if not result.get("terminal_complete"):
            raise StudyFailure(f"preflight arm {run_id} did not complete")
        _check_budget(out, reserve_branches=INITIAL_BRANCH_RESERVE)
        if _charged_now(out) + INITIAL_BRANCH_RESERVE + REVIEWER_RESERVE >= ALL_IN_CPU:
            raise CpuBudgetExceeded("preflight consumed the all-in budget reserve")

    # Verify deterministic behavior across one and two concurrent workers with
    # an exact repeated preflight seed; this is a systems check, not a new owner.
    check_id = f"PRE_WORKER_CHECK_{CONFIG['preflight']['seed_first']}_P0"
    check_path = out / "runs" / f"{check_id}.json"
    if not check_path.exists():
        seed = int(CONFIG["preflight"]["seed_first"])
        first_single = next(item for item in results if item["seed"] == seed and item["arm"] == "P0")
        # Companion E execution shares the 2-worker scheduler; the repeated P0
        # is compared to the committed one-worker natural run.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_p0 = executor.submit(_run_one, out=out, module=module, model=initial_model,
                model_digest=initial_digest, seed=seed, arm="P0", encoder=encoder,
                purpose="preflight-policy", resource_label=check_id)
            future_e = executor.submit(_run_one, out=out, module=module, model=initial_model,
                model_digest=initial_digest, seed=seed, arm="E", encoder=encoder,
                purpose="preflight-worker-check", resource_label=f"PRE_WORKER_COMPANION_{seed}_E")
            repeated, companion = future_p0.result(), future_e.result()
        same = (
            first_single["terminal"] == repeated["terminal"]
            and first_single["decisions"] == repeated["decisions"]
        )
        check = {
            "study_id": STUDY_ID, "type": "worker_count_determinism_system_check_not_an_owner",
            "seed": seed, "one_worker_run": first_single["run_id"],
            "two_worker_run": check_id, "two_worker_companion": companion["run_id"],
            "terminal_and_decision_trace_equal": same,
            "cpu_seconds": repeated["cpu_seconds"] + companion["cpu_seconds"],
            "charged_as_preflight_setup": True,
        }
        atomic_json(check_path, repeated)
        atomic_json(out / "worker-consistency.json", check)
        _record_run_if_new(out, repeated)
        _record_run_if_new(out, companion)
        if not same:
            raise StudyFailure("one-worker and two-worker deterministic preflight output differs")
        results.extend([repeated, companion])
    else:
        check = json.loads((out / "worker-consistency.json").read_text(encoding="utf-8"))
        if not check.get("terminal_and_decision_trace_equal"):
            raise StudyFailure("durable one/two worker check did not pass")

    worker_cpu = float(check.get("cpu_seconds", 0.0)) if check else 0.0
    owner_results = [item for item in results if "WORKER" not in str(item.get("run_id", ""))]
    cpu = sum(float(item.get("cpu_seconds", 0.0)) for item in owner_results) + worker_cpu + actual_before
    individual = [float(item["cpu_seconds"]) for item in results if item.get("terminal_complete") and item.get("arm") in {"P0", "E"} and "WORKER" not in str(item.get("run_id", ""))]
    if len(individual) < 16:
        raise StudyFailure("fewer than eight independent P0/E preflight owner arms completed")
    maximum_run = max(individual)
    mean_run = float(np.mean(individual))
    # Preflight outcomes are already included in actual_before. Project only
    # the still-unrun training and held-out owner arms, with frozen factors.
    future_owner_runs = int(PROJECTION["future_natural_owner_arm_runs"])
    projected = (
        actual_before + worker_cpu
        + future_owner_runs * maximum_run * float(PROJECTION["run_cpu_inflation_factor"])
        + BRANCH_CPU_UPPER + float(PROJECTION["engineering_margin_seconds"])
    )
    report = {
        "study_id": STUDY_ID,
        "status": "PREFLIGHT_COMPLETE",
        "source_identity": env["identity"],
        "runtime_optimizer": runtime,
        "native_fixtures": fixture,
        "source_projection_probe": projection_probe,
        "preflight_setup_cpu_seconds": setup_cpu,
        "completed_preflight_owner_arms": len([item for item in owner_results if item.get("terminal_complete") and item.get("arm") in {"P0", "E"}]),
        "distinct_preflight_seeds": sorted({int(item["seed"]) for item in owner_results if item.get("arm") in {"P0", "E"}}),
        "workers": {"production": 1, "consistency_test": check},
        "run_cpu_seconds_min": min(individual),
        "run_cpu_seconds_mean": mean_run,
        "run_cpu_seconds_max": maximum_run,
        "setup_and_preflight_cpu_seconds_charged": cpu,
        "projected_all_in_cpu_seconds_builder": projected,
        "reviewer_reserve_seconds": REVIEWER_RESERVE,
        "projected_all_in_with_reviewer_reserve": projected + REVIEWER_RESERVE,
        "within_builder_ceiling": projected <= MAX_CPU,
        "projection_factors": dict(PROJECTION),
        "within_all_in_ceiling": projected + REVIEWER_RESERVE <= ALL_IN_CPU,
        "training_seed_consumed": False,
    }
    atomic_json(out / "preflight-report.json", report)
    if projected > MAX_CPU or projected + REVIEWER_RESERVE > ALL_IN_CPU:
        report["status"] = "SCIENTIFIC_INCOMPLETE_RESOURCE_PROJECTION"
        atomic_json(out / "preflight-report.json", report)
        atomic_json(out / "study-status.json", {
            "status": "SCIENTIFIC_INCOMPLETE_RESOURCE_PROJECTION",
            "training_seed_consumed": False,
            "reason": "conservative preflight estimate exceeds the builder cap or total cap with reviewer reserve",
            "preflight_report": str(out / "preflight-report.json"),
        })
        return report
    atomic_json(out / "study-status.json", {
        "status": "PREFLIGHT_COMPLETE",
        "training_seed_consumed": False,
        "preflight_report_sha256": sha256_file(out / "preflight-report.json"),
        "projected_all_in_cpu_seconds_builder": projected,
        "projected_all_in_with_reviewer_reserve": projected + REVIEWER_RESERVE,
    })
    return report


def _write_artifact_manifest(out: Path) -> dict[str, Any]:
    entries = []
    for path in sorted(item for item in out.rglob("*") if item.is_file() and item.name != "artifact-manifest.json"):
        entries.append({
            "path": str(path.relative_to(out)).replace(os.sep, "/"),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    record = {"study_id": STUDY_ID, "schema_id": "issue70-output-artifact-manifest-v1", "files": entries}
    atomic_json(out / "artifact-manifest.json", record)
    return record


def _validate_completed_training(out: Path) -> dict[str, Any]:
    status_path = out / "training-status.json"
    if not status_path.exists():
        raise StudyFailure("independent evaluation requires a committed TRAINING_COMPLETE receipt")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if status.get("status") != "TRAINING_COMPLETE" or status.get("owner_count") != 512 or status.get("completed_blocks") != 4:
        raise StudyFailure("training receipt is incomplete; held-out evaluation is gated")
    seeds = list(range(int(CONFIG["training"]["seed_first"]), int(CONFIG["training"]["seed_last"]) + 1))
    for seed in seeds:
        path = out / "runs" / f"TRAIN_{seed}.json"
        if not path.exists():
            raise StudyFailure(f"training receipt omits the predeclared owner seed {seed}")
        run = json.loads(path.read_text(encoding="utf-8"))
        if not run.get("terminal_complete") or run.get("seed") != seed or run.get("arm") != "TRAIN":
            raise StudyFailure(f"training owner {seed} lacks a valid complete native terminal run")
    checkpoints = [out / "checkpoints" / f"block_{block:02d}.pt" for block in range(1, 5)]
    if any(not path.exists() for path in checkpoints):
        raise StudyFailure("the four frozen-block optimizer/model checkpoints are not all present")
    return status


def _shared_probe_rank_flips(out: Path, initial: AllNoncombatPolicy, final: AllNoncombatPolicy) -> dict[str, Any]:
    counts: dict[str, Counter[str]] = {}
    total = Counter()
    for seed in range(int(CONFIG["preflight"]["seed_first"]), int(CONFIG["preflight"]["seed_last"]) + 1):
        run_path = out / "runs" / f"PRE_{seed}_P0.json"
        if not run_path.exists():
            raise StudyFailure(f"shared P0 probe root run {seed} is missing")
        run = json.loads(run_path.read_text(encoding="utf-8"))
        artifact = run.get("decision_artifact")
        if not artifact or sha256_file(Path(artifact)) != run.get("decision_artifact_sha256"):
            raise StudyFailure(f"shared P0 probe feature artifact for seed {seed} is missing or corrupt")
        matrices = _load_decision_artifact(out, run_path.stem)
        rows = [item for item in run.get("decisions", []) if item.get("kind") == "categorical"]
        features = matrices["features"]
        offsets = matrices["offsets"].astype(int)
        if len(rows) != len(offsets) - 1:
            raise StudyFailure(f"shared P0 probe seed {seed} trace and feature rows differ")
        for row_index, row in enumerate(rows):
            matrix = torch.as_tensor(features[offsets[row_index]:offsets[row_index + 1]], dtype=torch.float32)
            with torch.no_grad():
                old_order = torch.argsort(initial.actor(matrix).squeeze(-1), descending=True, stable=True).tolist()
                new_order = torch.argsort(final.actor(matrix).squeeze(-1), descending=True, stable=True).tolist()
            screen = str(row.get("native_screen", "UNKNOWN"))
            bucket = counts.setdefault(screen, Counter())
            bucket["decisions"] += 1
            bucket["top_action_flips"] += int(bool(old_order and new_order and old_order[0] != new_order[0]))
            bucket["full_ranking_flips"] += int(old_order != new_order)
            total["decisions"] += 1
            total["top_action_flips"] += int(bool(old_order and new_order and old_order[0] != new_order[0]))
            total["full_ranking_flips"] += int(old_order != new_order)
    return {
        "type": "shared_preflight_P0_native_candidate_decisions_not_heldout_outcomes",
        "owners": int(CONFIG["preflight"]["owner_count"]),
        "per_screen": {key: dict(value) for key, value in sorted(counts.items())},
        "overall": dict(total),
        "top_action_flip_fraction": float(total["top_action_flips"] / total["decisions"]) if total["decisions"] else None,
        "full_ranking_flip_fraction": float(total["full_ranking_flips"] / total["decisions"]) if total["decisions"] else None,
    }


def _paired_percentile_interval(differences: np.ndarray, *, seed: int, resamples: int) -> dict[str, Any]:
    if differences.ndim != 1 or len(differences) != 96 or not np.isfinite(differences).all():
        raise StudyFailure("primary paired bootstrap requires all 96 finite owner-level differences")
    rng = np.random.default_rng(seed)
    sampled_indices = rng.integers(0, len(differences), size=(resamples, len(differences)))
    means = differences[sampled_indices].mean(axis=1)
    low, high = np.quantile(means, [0.025, 0.975], method="linear")
    return {
        "method": "paired_percentile",
        "confidence": 0.95,
        "resamples": resamples,
        "seed": seed,
        "quantile_method": "linear",
        "mean_difference": float(np.mean(differences)),
        "ci_95": [float(low), float(high)],
        "owner_count": int(len(differences)),
        "unit": "independent held-out owner seed",
    }


def _summarize_evaluation(out: Path, initial: AllNoncombatPolicy, final: AllNoncombatPolicy) -> dict[str, Any]:
    seeds = list(range(int(CONFIG["evaluation"]["seed_first"]), int(CONFIG["evaluation"]["seed_last"]) + 1))
    arms = list(CONFIG["evaluation"]["arms"])
    runs: dict[str, dict[int, dict[str, Any]]] = {arm: {} for arm in arms}
    for arm in arms:
        for seed in seeds:
            path = out / "runs" / f"EVAL_{seed}_{arm}.json"
            if not path.exists():
                raise StudyFailure(f"held-out evaluation is missing {path.name}")
            run = json.loads(path.read_text(encoding="utf-8"))
            if (not run.get("terminal_complete") or run.get("seed") != seed
                    or run.get("arm") != arm or not run.get("completed_native_terminal")):
                raise StudyFailure(f"held-out arm {arm}, owner {seed} is not a valid complete terminal episode")
            if run.get("native_source_commit") != EXPECTED_NATIVE:
                raise StudyFailure(f"held-out arm {arm}, owner {seed} used a different native source")
            runs[arm][seed] = run
    if any(len(rows) != 96 for rows in runs.values()):
        raise StudyFailure("held-out evaluation does not contain exactly 96 outcomes in each arm")

    floors = {arm: np.asarray([runs[arm][seed]["terminal"]["terminal_floor"] for seed in seeds], dtype=float) for arm in arms}
    primary = floors["P1"] - floors["P0"]
    bootstrap = CONFIG["evaluation"]["bootstrap"]
    interval = _paired_percentile_interval(
        primary,
        seed=int(bootstrap["seed"]),
        resamples=int(bootstrap["resamples"]),
    )
    low, high = interval["ci_95"]
    classification = "PRELIMINARY_POSITIVE" if low > 0 else "PRELIMINARY_HARM" if high < 0 else "PRELIMINARY_NULL"
    metrics: dict[str, Any] = {
        "study_id": STUDY_ID,
        "status": "EVALUATION_COMPLETE_PENDING_INDEPENDENT_REVIEW",
        "classification_before_independent_review": classification,
        "source_identity": {"native_source_commit": EXPECTED_NATIVE, "stsr_source_commit": EXPECTED_STSRL,
                            "native_module_sha256": CONFIG["identity"]["native_binary_sha256"]},
        "owner_seed_first": seeds[0],
        "owner_seed_last": seeds[-1],
        "independent_owner_count": len(seeds),
        "complete_terminal_arm_counts": {arm: len(runs[arm]) for arm in arms},
        "primary_estimand": "mean held-out owner-level terminal_floor(P1 - P0)",
        "primary_paired_interval": interval,
        "secondary_mean_terminal_floor": {arm: float(np.mean(floors[arm])) for arm in arms},
        "secondary_mean_floor_p1_minus_e": float(np.mean(floors["P1"] - floors["E"])),
        "secondary_owner_paired_p1_minus_e": _paired_percentile_interval(
            floors["P1"] - floors["E"], seed=int(bootstrap["seed"]) + 1,
            resamples=int(bootstrap["resamples"]),
        ),
        "heart_victories": {arm: int(sum(bool(runs[arm][seed]["terminal"]["heart_victory"]) for seed in seeds)) for arm in arms},
        "heart_victory_denominators": {arm: 96 for arm in arms},
        "act2_plus": {arm: int(sum(bool(runs[arm][seed]["terminal"]["act2_plus"]) for seed in seeds)) for arm in arms},
        "act3_plus": {arm: int(sum(bool(runs[arm][seed]["terminal"]["act3_plus"]) for seed in seeds)) for arm in arms},
        "act4_reached": {arm: int(sum(bool(runs[arm][seed]["terminal"]["act4_reached"]) for seed in seeds)) for arm in arms},
        "fidelity_exposure_counts_by_arm": {},
        "screen_action_coverage_by_arm": {},
        "chosen_native_action_kind_counts_by_arm": {},
        "cpu_wall_rss_by_arm": {},
        "shared_preflight_policy_rank_flips": _shared_probe_rank_flips(out, initial, final),
        "all_arms_counted_no_posthoc_censoring": True,
        "expert_decisions_in_P0_P1_or_training_suffixes": 0,
        "claims_scope": "frozen native-source restricted surrogate; not normal-information deployability or full real-STS fidelity",
        "proposed_code_disposition": "study-only implementation; do not promote model or privileged input to public STSRL API",
    }
    for arm in arms:
        exposure_names = sorted({name for run in runs[arm].values() for name in run.get("exposures", {})})
        metrics["fidelity_exposure_counts_by_arm"][arm] = {
            name: {"count": sum(bool(runs[arm][seed].get("exposures", {}).get(name, False)) for seed in seeds), "denominator": 96}
            for name in exposure_names
        }
        screen_counts: Counter[str] = Counter()
        candidate_counts: Counter[str] = Counter()
        chosen_kinds: Counter[str] = Counter()
        for seed in seeds:
            run = runs[arm][seed]
            for decision in run.get("decisions", []):
                if decision.get("kind") != "categorical":
                    continue
                screen = str(decision.get("native_screen", "UNKNOWN"))
                screen_counts[screen] += 1
                candidate_counts[screen] += int(decision.get("projection_audit", {}).get("candidate_count", 0))
                identities = decision.get("legal_action_identities", [])
                chosen = int(decision.get("chosen_index", -1))
                if 0 <= chosen < len(identities):
                    chosen_kinds[str(identities[chosen].get("kind", "unknown"))] += 1
        metrics["screen_action_coverage_by_arm"][arm] = {
            screen: {"multi_choice_decisions": amount, "native_candidates_associated": candidate_counts[screen], "coverage_status": "supported_all_live_actions"}
            for screen, amount in sorted(screen_counts.items())
        }
        metrics["chosen_native_action_kind_counts_by_arm"][arm] = dict(sorted(chosen_kinds.items()))
        metrics["cpu_wall_rss_by_arm"][arm] = {
            "cpu_seconds_sum": float(sum(runs[arm][seed].get("cpu_seconds", 0.0) for seed in seeds)),
            "wall_seconds_sum": float(sum(runs[arm][seed].get("wall_seconds", 0.0) for seed in seeds)),
            "max_run_cpu_seconds": float(max(runs[arm][seed].get("cpu_seconds", 0.0) for seed in seeds)),
            "max_run_wall_seconds": float(max(runs[arm][seed].get("wall_seconds", 0.0) for seed in seeds)),
            "max_sampled_run_rss_kib": int(max(runs[arm][seed].get("max_rss_kib_sampled_for_run", 0) for seed in seeds)),
        }
    atomic_json(out / "evaluation-summary.json", metrics)
    atomic_json(out / "evaluation-status.json", {
        "status": metrics["status"], "classification_before_independent_review": classification,
        "owner_count": 96, "terminal_arms": {arm: 96 for arm in arms},
        "evaluation_summary_sha256": sha256_file(out / "evaluation-summary.json"),
    })
    atomic_json(out / "study-status.json", metrics)
    _write_artifact_manifest(out)
    return metrics


def run_evaluation(out: Path) -> dict[str, Any]:
    training = _validate_completed_training(out)
    if training.get("native_source_commit") != EXPECTED_NATIVE or training.get("expert_decisions") != 0:
        raise StudyFailure("training source or Expert-isolation receipt failed evaluation gate")
    if sha256_file(out / "policies" / "pi_init.pt") != training.get("pi_init_sha256"):
        raise StudyFailure("pi_init checkpoint changed after training")
    if sha256_file(out / "policies" / "pi_final.pt") != training.get("pi_final_sha256"):
        raise StudyFailure("pi_final checkpoint does not match the completed training receipt")
    initial, initial_digest = _load_policy(out / "policies" / "pi_init.pt")
    final, final_digest = _load_policy(out / "policies" / "pi_final.pt")
    if initial_digest != training.get("pi_init_model_digest") or final_digest != training.get("pi_final_model_digest"):
        raise StudyFailure("evaluation policy parameter digest differs from its training receipt")
    env, module, encoder, runtime = _setup(out)
    preflight = json.loads((out / "preflight-report.json").read_text(encoding="utf-8"))
    if env["identity"] != preflight.get("source_identity"):
        raise StudyFailure("evaluation environment identity differs from the accepted preflight")
    if runtime.get("optimizer_id") != CONFIG["runtime_optimizer"]["optimizer_id"]:
        raise StudyFailure("evaluation runtime optimizer differs from the accepted preflight")
    expert_digest = sha256_bytes(canonical({"expert": "expert_non_combat_v1", "source": EXPECTED_STSRL}).encode("utf-8"))
    specs = [(int(seed), arm) for seed in range(
        int(CONFIG["evaluation"]["seed_first"]), int(CONFIG["evaluation"]["seed_last"]) + 1
    ) for arm in CONFIG["evaluation"]["arms"]]
    atomic_json(out / "evaluation-status.json", {
        "status": "EVALUATION_RUNNING", "owner_count": 96, "arms": list(CONFIG["evaluation"]["arms"]),
        "terminal_outcomes_required": 288, "native_source_commit": EXPECTED_NATIVE,
        "expert_decisions_in_P0_P1": 0,
    })
    for seed, arm in specs:
        _check_budget(out, reserve_branches=0.0)
        run_id = f"EVAL_{seed}_{arm}"
        path = out / "runs" / f"{run_id}.json"
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if not previous.get("terminal_complete"):
                atomic_json(out / "evaluation-status.json", {
                    "status": "SCIENTIFIC_INCOMPLETE_INVALID_EVALUATION_ARM",
                    "run_id": run_id, "training_seed_consumed": True,
                    "complete_terminal_arms_so_far": sum(
                        1 for item in _json_paths(out / "runs", "EVAL_*.json")
                        if json.loads(item.read_text(encoding="utf-8")).get("terminal_complete")
                    ),
                })
                return json.loads((out / "evaluation-status.json").read_text(encoding="utf-8"))
            continue
        model, digest = (initial, initial_digest) if arm == "P0" else (final, final_digest) if arm == "P1" else (initial, expert_digest)
        result = _run_one(
            out=out, module=module, model=model, model_digest=digest,
            seed=seed, arm=arm, encoder=encoder, purpose=f"heldout-{arm.lower()}-evaluation",
            resource_label=run_id,
        )
        _record_run_if_new(out, result)
        if not result.get("terminal_complete"):
            atomic_json(out / "evaluation-status.json", {
                "status": "SCIENTIFIC_INCOMPLETE_INVALID_EVALUATION_ARM",
                "run_id": run_id, "training_seed_consumed": True,
            })
            return json.loads((out / "evaluation-status.json").read_text(encoding="utf-8"))
    return _summarize_evaluation(out, initial, final)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "train", "evaluate", "run"))
    parser.add_argument("--output", required=True, type=Path, help="durable output directory outside the source checkout")
    args = parser.parse_args(argv)
    out = args.output.expanduser().resolve()
    global BASELINE_PARENT_CPU, BASELINE_BRANCH_CPU, INITIAL_BRANCH_RESERVE
    phase_id: str | None = None
    phase_run_cpu_before = 0.0
    phase_branch_parent_cpu_before = 0.0
    try:
        lock_config(out)
        BASELINE_PARENT_CPU, BASELINE_BRANCH_CPU = _existing_cpu_charge(out)
        INITIAL_BRANCH_RESERVE = 0.0 if args.phase == "evaluate" else BRANCH_CPU_UPPER
        resource_guard = _install_cpu_cap(out)
        atomic_json(out / f"resource-guard-{args.phase}.json", resource_guard)
        phase_id = f"{args.phase}:{time.time_ns()}:{os.getpid()}"
        phase_run_cpu_before = _run_and_replay_cpu_total(out)
        phase_branch_parent_cpu_before = _branch_parent_cpu_total(out)
        append_jsonl(out / "coarse-state.jsonl", {
            "study_id": STUDY_ID, "boundary": "phase_started", "phase": args.phase,
            "phase_id": phase_id,
            "charged_prior_parent_cpu_seconds": BASELINE_PARENT_CPU,
            "charged_prior_branch_cpu_seconds": BASELINE_BRANCH_CPU,
            "setup_cpu_seconds": SETUP_CPU,
            "rlimit_cpu_seconds": resource_guard["rlimit_cpu_seconds"],
        })
        exit_code = 0
        if args.phase == "preflight":
            result = run_preflight(out)
        elif args.phase == "train":
            result = run_training(out)
        elif args.phase == "evaluate":
            result = run_evaluation(out)
        else:
            preflight = run_preflight(out)
            if not preflight.get("within_builder_ceiling") or not preflight.get("within_all_in_ceiling"):
                result = preflight
                exit_code = 2
            else:
                training = run_training(out)
                if training.get("status") != "TRAINING_COMPLETE":
                    result = training
                    exit_code = 2
                else:
                    result = run_evaluation(out)
        phase_cpu = _phase_unattributed_cpu(
            out, process_cpu=_phase_process_cpu(),
            run_cpu_before=phase_run_cpu_before,
            branch_parent_cpu_before=phase_branch_parent_cpu_before,
        )
        append_jsonl(out / "coarse-state.jsonl", {
            "study_id": STUDY_ID, "boundary": "phase_finished", "phase": args.phase,
            "phase_id": phase_id, **phase_cpu,
            "status": result.get("status", "PREFLIGHT_COMPLETE"),
            "charged_all_in_cpu_seconds": _charged_now(out),
        })
        if str(result.get("status", "")).startswith("SCIENTIFIC_INCOMPLETE"):
            exit_code = 2
        return exit_code
    except Exception as exc:
        training_seed_consumed = any(_json_paths(out / "runs", "TRAIN_*.json"))
        failure = {
            "status": "SCIENTIFIC_INCOMPLETE_EXECUTION_FAILURE",
            "phase": args.phase,
            "failure_boundary": "study_controller_or_native_execution",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "training_seed_consumed": training_seed_consumed,
            "charged_all_in_cpu_seconds": _charged_now(out),
            "native_source_commit": EXPECTED_NATIVE,
            "phase_id": phase_id,
        }
        atomic_json(out / "study-status.json", failure)
        phase_cpu = _phase_unattributed_cpu(
            out, process_cpu=_phase_process_cpu(),
            run_cpu_before=phase_run_cpu_before,
            branch_parent_cpu_before=phase_branch_parent_cpu_before,
        ) if phase_id is not None else {}
        append_jsonl(out / "coarse-state.jsonl", {
            "study_id": STUDY_ID, "boundary": "phase_failed", **phase_cpu, **failure,
        })
        print(canonical(failure), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
