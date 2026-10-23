"""Study-local Issue 73 Stage 0 native noncombat Oracle helpers.

This is disposable study code. Game transitions and every battle mechanic are
owned by the exact pinned sts_lightspeed native source. STSRL's one authoritative
controlled-run executor owns reset, legal-action validation and stepping.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
from dataclasses import dataclass
import hashlib
import importlib
import json
import math
import os
import random
import resource
import shutil
import signal
import sys
import threading
import time
MODULE_PROCESS_CPU_START = time.process_time()
from collections import Counter
from pathlib import Path
import traceback
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
import source_lock_integrity


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
def normalize_text_bytes(payload: bytes) -> bytes:
    """Return canonical LF bytes for locked UTF-8 source/config artifacts."""
    return payload.replace(b"\r\n", b"\n")


def sha256_text_file(path: Path) -> str:
    """Hash a text artifact independently of checkout CRLF conversion."""
    return sha256_bytes(normalize_text_bytes(path.read_bytes()))


CONFIG_BYTES = normalize_text_bytes(CONFIG_PATH.read_bytes())
CONFIG_SHA256 = hashlib.sha256(CONFIG_BYTES).hexdigest()
CONFIG_LOCK_PATH = ROOT / "config.lock.sha256"
try:
    CONFIG_LOCK_SHA256 = CONFIG_LOCK_PATH.read_text(encoding="ascii").strip().lower()
except OSError as exc:
    raise RuntimeError("committed config.lock.sha256 is missing; refusing to start") from exc
if CONFIG_LOCK_SHA256 != CONFIG_SHA256:
    raise RuntimeError("config.json bytes do not match the committed config.lock.sha256")
CONFIG = json.loads(CONFIG_BYTES)
STUDY_ID = CONFIG["study_id"]
EXPECTED_NATIVE = CONFIG["identity"]["native_commit"]
EXPECTED_STSRL = CONFIG["identity"]["stsr_commit"]
EXPECTED_STSRL_NATIVE_INTEGRATION = CONFIG["identity"]["stsr_native_integration_commit"]
BATTLE_SEARCH_PROTOCOL = CONFIG["battle_search"]
NATIVE_SEARCH_API = str(BATTLE_SEARCH_PROTOCOL["native_api"])
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
ACTIVE_STAGE1_PHASE: str | None = None
STAGE1_STAGE0_BASELINE_CACHE: dict[str, Any] | None = None
STAGE1_ACCOUNTING_SNAPSHOT: "Stage1AccountingSnapshot | None" = None


@dataclass
class BranchAccounting:
    """One-time crash/resume reconstruction for branch CPU charges."""

    per_root: dict[tuple[int, int], dict[str, float]]
    attempt_cpu_seconds: float
    last_committed_attempt_by_root: dict[tuple[int, int], str]


@dataclass
class Stage1AccountingSnapshot:
    """Phase-local receipt/accounting view, refreshed only after durable writes."""

    out: Path
    expected_run_receipts: dict[str, tuple[int, str]]
    completed_run_receipts: set[str]
    completed_preflight_runs: int
    completed_training_runs: int
    completed_evaluation_runs: int
    worker_repeats: int
    max_run_cpu_seconds: float
    branch_per_root: dict[tuple[int, int], dict[str, float]]
    last_committed_attempt_by_root: dict[tuple[int, int], str]


class StudyFailure(RuntimeError):
    """Fail-closed study contract or native-run failure with optional receipt."""

    def __init__(self, message: str, *, receipt: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.receipt = dict(receipt) if receipt is not None else None


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
        self._thread = threading.Thread(target=self._sample, name="issue71-rss-sampler", daemon=True)
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



def validate_native_module_identity(path: str | Path, *, expected_name: str, expected_sha256: str) -> str:
    """Validate the exact loaded native binary without text normalization."""
    module_path = Path(path)
    digest = sha256_file(module_path)
    if module_path.name != expected_name or digest != expected_sha256:
        raise StudyFailure(f"loaded native module identity mismatch: {module_path} {digest}")
    return digest


def validate_locked_optimizer_source(path: str | Path, *, expected_sha256: str | None = None) -> str:
    """Hash locked UTF-8 source after accepting only CRLF/LF checkout conversion."""
    source_path = Path(path)
    try:
        canonical_source = source_lock_integrity.normalize_locked_source_eol(
            source_path.read_bytes(), label=str(source_path),
        )
    except source_lock_integrity.SourceLockValidationError as exc:
        raise StudyFailure(str(exc)) from exc
    digest = sha256_bytes(canonical_source)
    expected = expected_sha256 or CONFIG["runtime_optimizer"]["sha256"]
    if digest != expected:
        raise StudyFailure("Issue 65 study-local optimizer is missing or changed")
    return digest


def validate_light_speed_adapter_class(adapter_class: type) -> None:
    """Fail closed unless the adapter is the pinned STSRL LightSpeedAdapter."""
    if (getattr(adapter_class, "__module__", None), getattr(adapter_class, "__qualname__", None)) != (
        "sts_combat_rl.sim.lightspeed", "LightSpeedAdapter",
    ):
        raise StudyFailure("loaded LightSpeed adapter identity differs from the pinned STSRL adapter")

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


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
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
    digest = sha256_text_file(CONFIG_PATH)
    lock = CONFIG_LOCK_PATH
    if not lock.exists():
        raise StudyFailure("committed config.lock.sha256 is missing; do not consume study seeds")
    expected = lock.read_text(encoding="ascii").strip().lower()
    if expected != digest.lower():
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


def git_invocation(path: str | Path) -> tuple[list[str], dict[str, str]]:
    """Return checkout-bound Git command/env, using Git for Windows for WSL worktrees."""
    root = Path(path).resolve()
    marker = root / ".git"
    raw_git_dir: str | None = None
    if marker.is_dir():
        git_dir = marker.resolve()
    else:
        try:
            line = marker.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise StudyFailure(f"Git worktree metadata is unavailable for {root}: {exc}") from exc
        prefix, separator, value = line.partition(":")
        if prefix.lower() != "gitdir" or not separator or not value.strip():
            raise StudyFailure(f"invalid Git worktree pointer for {root}")
        value = value.strip().replace("\\", "/")
        if len(value) >= 3 and value[1] == ":" and value[2] == "/":
            raw_git_dir = value
            if os.name != "nt":
                git_dir = Path(f"/mnt/{value[0].lower()}/{value[3:]}")
            else:
                git_dir = Path(value)
        else:
            git_dir = Path(value)
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
    env = os.environ.copy()
    if os.name != "nt" and raw_git_dir is not None:
        exe = env.get("ISSUE73_WINDOWS_GIT_EXE") or shutil.which("git.exe")
        if not exe:
            for candidate in ("/mnt/c/Program Files/Git/cmd/git.exe", "/mnt/d/Git/cmd/git.exe"):
                if Path(candidate).is_file():
                    exe = candidate
                    break
        if not exe or not Path(exe).is_file():
            raise StudyFailure("Windows Git is required to operate on this WSL-mounted Windows worktree; set ISSUE73_WINDOWS_GIT_EXE")
        root_text = str(root).replace("\\", "/")
        if len(root_text) < 7 or not root_text.startswith("/mnt/") or root_text[6] != "/":
            raise StudyFailure(f"WSL worktree path cannot be converted to a Windows Git path: {root_text}")
        windows_root = f"{root_text[5].upper()}:/{root_text[7:]}"
        env["GIT_DIR"] = raw_git_dir
        env["GIT_WORK_TREE"] = windows_root
        return [exe, "-C", windows_root], env
    env["GIT_DIR"] = str(git_dir)
    env["GIT_WORK_TREE"] = str(root)
    return ["git", "-C", str(root)], env


def git_environment(path: str | Path) -> dict[str, str]:
    """Backward-compatible checkout-bound environment for Git subprocesses."""
    return git_invocation(path)[1]


def _git_identity(path: str) -> tuple[str, str]:
    import subprocess
    command, env = git_invocation(path)
    commit = subprocess.check_output([*command, "rev-parse", "HEAD"], text=True, env=env).strip()
    dirty = subprocess.check_output([*command, "status", "--porcelain"], text=True, env=env).strip()
    return commit, dirty


def validate_environment(out: Path, *, native_module_path: str | Path | None = None) -> dict[str, Any]:
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
    # The v6 source-history launcher locks the exact binary path.  Importing
    # by module name alone is insufficient: a fresh capture process may not
    # inherit the native build directory in sys.path even though a smoke
    # process did.  Resolve and hash the configured binary before import, add
    # only its verified parent to sys.path, then prove Python loaded that exact
    # file.  Older callers retain their pinned sys.path behavior when no path
    # is supplied.
    configured_module_path = native_module_path or os.environ.get("ISSUE73_NATIVE_MODULE_PATH")
    expected_module_path: Path | None = None
    original_sys_path = list(sys.path)
    if configured_module_path:
        try:
            expected_module_path = Path(configured_module_path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise StudyFailure(f"configured native module path is unavailable: {configured_module_path}: {exc}") from exc
        if not expected_module_path.is_file():
            raise StudyFailure(f"configured native module path is not a file: {expected_module_path}")
        validate_native_module_identity(
            expected_module_path,
            expected_name=CONFIG["identity"]["native_module"],
            expected_sha256=CONFIG["identity"]["native_binary_sha256"],
        )
        module_directory = str(expected_module_path.parent)
        if module_directory not in {str(Path(entry or ".").resolve()) for entry in sys.path}:
            sys.path.insert(0, module_directory)
        cached_module = sys.modules.get("slaythespire")
        if cached_module is not None:
            cached_path_value = getattr(cached_module, "__file__", None)
            if not cached_path_value or Path(cached_path_value).resolve() != expected_module_path:
                raise StudyFailure(
                    "slaythespire was already imported from a path different from the locked native module"
                )
            module = cached_module
        else:
            module = importlib.import_module("slaythespire")
        module_path = Path(module.__file__).resolve(strict=True)
        if module_path != expected_module_path:
            raise StudyFailure(
                f"Python loaded slaythespire from {module_path}, not the locked path {expected_module_path}"
            )
    else:
        module = importlib.import_module("slaythespire")
        module_path = Path(module.__file__).resolve(strict=True)
    binary_hash = validate_native_module_identity(
        module_path,
        expected_name=CONFIG["identity"]["native_module"],
        expected_sha256=CONFIG["identity"]["native_binary_sha256"],
    )
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
        "native_module_search": {
            "configured_exact_path": str(expected_module_path) if expected_module_path else None,
            "configured_parent_present_before_import": (
                str(expected_module_path.parent) in {str(Path(entry or ".").resolve()) for entry in original_sys_path}
                if expected_module_path else None
            ),
            "configured_parent_present_after_import": (
                str(expected_module_path.parent) in {str(Path(entry or ".").resolve()) for entry in sys.path}
                if expected_module_path else None
            ),
            "loaded_path_exact_match": module_path == expected_module_path if expected_module_path else None,
        },
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


def _stage0_screen_key(value: Any) -> str:
    screen = str(value).upper()
    if screen in {"MAP", "MAP_SCREEN"}:
        return "MAP"
    if screen in {"REWARDS", "REWARD_SCREEN"}:
        return "REWARDS"
    return screen


def process_tree_cpu() -> float:
    """Return this process and its completed child-process CPU usage."""
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return float(own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime)


def _snapshot_debug_fingerprints(adapter: Any, snapshot: Any) -> dict[str, str]:
    """Hash native and public state in memory; never persist opaque raw state."""
    fingerprint = adapter.checkpoint_fingerprint(snapshot)
    state_digest = sha256_bytes(canonical(fingerprint).encode("utf-8"))
    public_digest = sha256_bytes(canonical(adapter.public_projection(snapshot)).encode("utf-8"))
    raw_state = fingerprint[1] if isinstance(fingerprint, tuple) and len(fingerprint) == 2 else {}
    resource_terms = (
        "hp", "gold", "potion", "relic", "key", "deck", "energy", "block",
        "floor", "act", "screen", "hand", "discard", "draw",
    )
    resources = {
        str(key): value for key, value in raw_state.items()
        if any(term in str(key).lower() for term in resource_terms)
    } if isinstance(raw_state, Mapping) else {}
    return {
        "native_state_sha256": state_digest,
        "public_projection_sha256": public_digest,
        "public_resource_state_sha256": sha256_bytes(canonical(resources).encode("utf-8")),
    }


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

    def public_projection(self, snapshot: Any) -> Any:
        """Preserve the pinned optional public-projection adapter capability."""
        return self.base.public_projection(snapshot)

    def checkpoint_fingerprint(self, snapshot: Any) -> Any:
        """Delegate the pinned exact-restore snapshot fingerprint."""
        return self.base.checkpoint_fingerprint(snapshot)


class StudyBattleSearchV2Controller:
    """Study-only null-guidance controller using the pinned native Search-v2 API."""

    def __init__(self):
        from sts_combat_rl.sim.action_space import ActionSpaceConfig
        from sts_combat_rl.sim.battle_search_v2 import (
            BATTLE_SEARCH_V2_NATIVE_API,
            BATTLE_SEARCH_V2_PATCH_IDENTITY,
        )
        from sts_combat_rl.sim.controller_contract import ControllerProvenance
        from sts_combat_rl.sim.lightspeed_source import lightspeed_source_identity_dict
        from sts_combat_rl.sim.online_controller import NATIVE_SEARCH_INFORMATION_REGIME
        from sts_combat_rl.sim.oracle_search import (
            ORACLE_SEARCH_SCHEMA_ID,
            _include_potions_for_battle_search,
            validate_oracle_root_selection_rule,
        )

        simulations = int(BATTLE_SEARCH_PROTOCOL["simulations"])
        root_selection_rule = str(BATTLE_SEARCH_PROTOCOL["root_selection_rule"])
        if simulations <= 0:
            raise ValueError("Issue 73 Search-v2 simulations must be positive")
        validate_oracle_root_selection_rule(root_selection_rule)
        if BATTLE_SEARCH_V2_NATIVE_API != NATIVE_SEARCH_API:
            raise RuntimeError("pinned STSRL Search-v2 API disagrees with committed study config")
        if BATTLE_SEARCH_V2_PATCH_IDENTITY != BATTLE_SEARCH_PROTOCOL["patch_identity"]:
            raise RuntimeError("pinned STSRL Search-v2 patch identity disagrees with committed study config")
        self.simulations = int(simulations)
        self.root_selection_rule = root_selection_rule
        self.action_space = ActionSpaceConfig.include_all()
        self.include_potions = _include_potions_for_battle_search(self.action_space)
        if self.include_potions != bool(BATTLE_SEARCH_PROTOCOL["include_potions"]):
            raise RuntimeError("native action-space potion setting disagrees with committed study config")
        if BATTLE_SEARCH_PROTOCOL["policy_prior_callback"] is not None:
            raise RuntimeError("Issue 73 Battle Search-v2 requires a null policy prior")
        if BATTLE_SEARCH_PROTOCOL["leaf_value_callback"] is not None:
            raise RuntimeError("Issue 73 Battle Search-v2 requires a null learned leaf value")
        self.native_search_api = BATTLE_SEARCH_V2_NATIVE_API
        self.native_search_patch_identity = BATTLE_SEARCH_V2_PATCH_IDENTITY
        self.provenance = ControllerProvenance(
            kind="oracle_battle_search",
            name=f"oracle_search_v2_{root_selection_rule}_s{self.simulations}",
            config={
                "controller_version": "issue71-native-search-v2-null-guidance-v1",
                "information_regime": NATIVE_SEARCH_INFORMATION_REGIME,
                "native_search_schema_id": ORACLE_SEARCH_SCHEMA_ID,
                "native_search_api": self.native_search_api,
                "native_search_patch_identity": self.native_search_patch_identity,
                "native_source_identity": lightspeed_source_identity_dict(),
                "search_budget": {
                    "simulations": self.simulations,
                    "budget_unit": "native_tree_search_playouts",
                },
                "root_selection_rule": self.root_selection_rule,
                "action_space": self.action_space.to_dict(),
                "include_potions": self.include_potions,
                "policy_prior": BATTLE_SEARCH_PROTOCOL["policy_prior_callback"],
                "learned_leaf_value": BATTLE_SEARCH_PROTOCOL["leaf_value_callback"],
                "rollout_configuration": {
                    "rollout_policy": BATTLE_SEARCH_PROTOCOL["rollout_policy"],
                    "leaf_value": BATTLE_SEARCH_PROTOCOL["terminal_utility"],
                    "model_calls": 0,
                },
                "reproducibility": {
                    "deterministic_given_restored_checkpoint": True,
                    "native_rng_seed_source": "BattleContext.seed+floorNum",
                    "python_rng_seed": None,
                },
                "root_mapping": "occurrence_safe_action_identity_v1",
            },
        )

    def select_action(
        self,
        adapter: Any,
        snapshot: Any,
        actions: Sequence[Any],
        context: Any,
        step_index: int,
    ) -> Any:
        del step_index
        from sts_combat_rl.sim.controller_contract import ControllerDecision
        from sts_combat_rl.sim.oracle_search import (
            build_oracle_search_report,
            oracle_search_controller_metadata,
            select_oracle_root_action,
        )

        search_fn = getattr(adapter, "battle_search_v2", None)
        if not callable(search_fn):
            raise ValueError("Issue 73 Search-v2 controller requires battle_search_v2 adapter")
        started = time.perf_counter()
        raw_search = search_fn(
            snapshot,
            simulations=self.simulations,
            include_potions=self.include_potions,
        )
        report = build_oracle_search_report(
            raw_search,
            actions,
            context,
            expected_native_api=self.native_search_api,
            expected_patch_identity=self.native_search_patch_identity,
            wall_clock_time_s=time.perf_counter() - started,
        )
        if not report.search_ok:
            raise ValueError("Issue 73 Search-v2 root mapping failed: " + "; ".join(report.problems))
        target = select_oracle_root_action(report, selection_rule=self.root_selection_rule)
        return ControllerDecision(
            selected_index=target.legal_action_index,
            provenance=self.provenance,
            reason=f"oracle_search_v2:{self.root_selection_rule}",
            score=target.score,
            metadata=oracle_search_controller_metadata(report, target),
        )


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
        checkpoint_screens: Sequence[str] | None = None,
        forced_root: Mapping[str, Any] | None = None,
        sampling_purpose: str = "policy",
        capture_trace: bool = True, capture_all_roots: bool = False,
        suffix_random_seed: int | None = None, expert_random_seed: int | None = None,
        root_callback: Any | None = None,
    ) -> None:
        from sts_combat_rl.sim.controller_contract import ControllerProvenance

        if arm not in {"TRAIN", "BRANCH", "P0", "P1", "E", "SUFFIX"}:
            raise ValueError(f"unknown study arm {arm}")
        self.module = module
        self.arm = arm
        self.model = model
        self.model_digest = model_digest
        self.encoder = encoder
        self.seed = seed
        self.capture_checkpoints = capture_checkpoints
        self.capture_all_roots = bool(capture_all_roots)
        self.root_callback = root_callback
        self.suffix_random_seed = int(
            suffix_random_seed if suffix_random_seed is not None
            else derived_seed(STUDY_ID, seed, arm, model_digest, sampling_purpose + "|suffix")
        )
        self.suffix_rng = random.Random(self.suffix_random_seed)
        self.suffix_expert_seed = int(
            expert_random_seed if expert_random_seed is not None
            else derived_seed(STUDY_ID, seed, arm, model_digest, sampling_purpose + "|expert")
        )
        self.suffix_expert_probability = float(CONFIG["stage0"]["pi_suffix_expert_probability"])
        self.checkpoint_screens = {_stage0_screen_key(item) for item in checkpoint_screens or ()}
        self.captured_checkpoint_screens: set[str] = set()
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
        self.oracle = StudyBattleSearchV2Controller()
        self.expert = None
        if arm in {"E", "SUFFIX"}:
            from sts_combat_rl.sim.non_combat_policy import ExpertNonCombatDriver
            self.expert = ExpertNonCombatDriver(seed=self.suffix_expert_seed if arm == "SUFFIX" else 1740001)
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
            kind="issue73_stage0_controller", name=f"issue73_{arm.lower()}_v1", config=provenance_config
        )

    def reset_for_run(self, seed: int | None) -> None:
        if seed is not None and int(seed) != self.seed:
            raise StudyFailure("controller simulator seed differs from its predeclared owner seed")
        if self.expert is not None:
            # The continuation policy stream is independent of the native owner
            # seed and has its own frozen per-suffix seed domain.
            policy_seed = self.suffix_expert_seed if self.arm == "SUFFIX" else self.seed
            self.expert.reset_for_run(policy_seed)

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
            if self.arm in {"E", "SUFFIX"} and self.expert is None:
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
        elif self.arm == "SUFFIX":
            with phase("pi_suffix_expert_uniform_mixture"):
                expert_decision = self.expert.select_action(context)
                expert_index = int(expert_decision.legal_action_index)
                if not 0 <= expert_index < len(actions):
                    raise StudyFailure("Expert returned an out-of-range legal occurrence")
                use_expert = self.suffix_rng.random() < self.suffix_expert_probability
                selected = expert_index if use_expert else self.suffix_rng.randrange(len(actions))
            reason = "pi_suffix_expert" if use_expert else "pi_suffix_uniform_legal"
            probabilities = None
            value = None
            policy_row = False
        else:
            with phase("policy_inference_and_sampling"):
                selected, probabilities, value = self.model.choose(matrix, self.rng)
            reason = "sampled_native_semantic_categorical_policy"
            policy_row = self.arm in {"TRAIN", "P0", "P1", "BRANCH"}

        sampled_index = int(selected) if self.arm != "E" else None
        if not 0 <= selected < len(actions):
            raise StudyFailure("controller selected an out-of-range native action")
        identities = action_identity_dicts_for_actions(actions)
        if self.forced_root_pending:
            expected_fingerprints = self.forced_root.get("root_fingerprints", {})
            actual_fingerprints = _snapshot_debug_fingerprints(adapter, snapshot)
            for fingerprint_name in (
                "native_state_sha256", "public_projection_sha256",
                "public_resource_state_sha256",
            ):
                if expected_fingerprints.get(fingerprint_name) != actual_fingerprints.get(fingerprint_name):
                    raise StudyFailure(f"restored forced root {fingerprint_name} differs from its source checkpoint")
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
            "policy_sampled_index": sampled_index,
            "probabilities": probabilities.tolist() if probabilities is not None else None,
            "value_estimate": value,
            "legal_action_identities": identities,
            "projection_audit": audit,
            "projection_digest": hashlib.sha256(canonical(projection).encode()).hexdigest(),
            "root_identity_digest": self.native_identity_digest,
            "policy_training_row": policy_row,
            "pi_suffix_component": reason if self.arm == "SUFFIX" else None,
            "pi_suffix_master_seed": self.suffix_random_seed if self.arm == "SUFFIX" else None,
            "pi_suffix_expert_seed": self.suffix_expert_seed if self.arm == "SUFFIX" else None,
        }
        self.trace.append({"matrix": matrix, **trace_item} if self.capture_trace else trace_item)
        screen_key = _stage0_screen_key(audit["screen"])
        if self.checkpoint_screens:
            capture_screen = self.capture_all_roots or (
                screen_key in self.checkpoint_screens
                and screen_key not in self.captured_checkpoint_screens
            )
        else:
            capture_screen = self.capture_all_roots or len(self.roots) < 2
        if self.capture_checkpoints and capture_screen:
            checkpoint = adapter.capture_checkpoint(snapshot)
            checkpoint_metadata = getattr(checkpoint, "metadata", {})
            root_fingerprints = _snapshot_debug_fingerprints(adapter, snapshot)
            public_projection = adapter.public_projection(snapshot)
            public_projection_payload = (
                json.loads(public_projection.canonical_payload)
                if hasattr(public_projection, "canonical_payload")
                else dict(public_projection) if isinstance(public_projection, Mapping) else None
            )
            if public_projection_payload is None:
                raise StudyFailure("sanitized public projection is not persistable JSON")
            self.roots.append({
                "root_index": len(self.roots),
                "run_id": getattr(self, "run_id", None),
                "seed": self.seed,
                "decision_index": decision_index,
                "policy_decision_index": sum(item.get("kind") == "categorical" for item in self.trace[:decision_index]),
                "checkpoint": checkpoint,
                "adapter": adapter,
                "native_checkpoint_id": getattr(checkpoint, "checkpoint_id", None),
                "checkpoint_adapter_id": getattr(checkpoint, "adapter_id", None),
                "native_checkpoint_metadata": dict(checkpoint_metadata) if isinstance(checkpoint_metadata, Mapping) else {},
                **root_fingerprints,
                "screen": audit["screen"],
                "candidate_index": selected,
                "legal_action_identities": identities,
                "probabilities": probabilities.tolist() if probabilities is not None else None,
                "audit": audit,
                "live_binding_validation_passed": True,
                "projection": projection,
                "public_projection": public_projection_payload,
                "feature_matrix": matrix.astype(np.float32).tolist(),
                "projection_digest": hashlib.sha256(canonical(projection).encode()).hexdigest(),
            })
            self.captured_checkpoint_screens.add(screen_key)
            if self.root_callback is not None:
                self.root_callback(self.roots[-1])
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


def _public_projection_payload(adapter: Any, snapshot: Any) -> dict[str, Any]:
    projection = adapter.public_projection(snapshot)
    payload = getattr(projection, "canonical_payload", None)
    if isinstance(payload, str):
        value = json.loads(payload)
    elif isinstance(projection, Mapping):
        value = dict(projection)
    else:
        raise StudyFailure("native public projection is not persistable JSON")
    if not isinstance(value, dict):
        raise StudyFailure("native public projection payload is not an object")
    return value


class ActionHistoryAdapter:
    """Record the actual action occurrence and transition for each native step."""

    def __init__(self, base: Any) -> None:
        self.base = base
        self.events: list[dict[str, Any]] = []
        self._current_event: dict[str, Any] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)

    def bind_event(self, event: dict[str, Any], actions: Sequence[Any], selected_index: int) -> None:
        if self._current_event is not None:
            raise StudyFailure("action-history recorder received a second decision before a native step")
        event["_selected_action_object"] = actions[selected_index]
        event["_selected_action_index"] = selected_index
        self._current_event = event

    def step(self, action: Any) -> Any:
        event = self._current_event
        if event is None:
            raise StudyFailure("native step occurred without a recorded live legal-action decision")
        selected = int(event.pop("_selected_action_index"))
        expected_action = event.pop("_selected_action_object")
        if action is not expected_action:
            raise StudyFailure("controlled runner stepped a different action object than the selected live occurrence")
        transition = self.base.step(action)
        post_snapshot = transition.snapshot
        event["transition"] = {
            "terminal": bool(transition.terminal),
            "info": dict(transition.info),
            "post_state_fingerprints": _snapshot_debug_fingerprints(self.base, post_snapshot),
            "post_public_projection": _public_projection_payload(self.base, post_snapshot),
        }
        event["selected_occurrence"] = selected
        self.events.append(event)
        self._current_event = None
        return transition


class ActionHistoryController:
    """Transparent controller wrapper that records every actual legal-action list."""

    def __init__(self, controller: Any, adapter: ActionHistoryAdapter) -> None:
        self._controller = controller
        self._adapter = adapter

    def __getattr__(self, name: str) -> Any:
        return getattr(self._controller, name)

    def reset_for_run(self, seed: int | None) -> None:
        self._controller.reset_for_run(seed)

    def select_action(self, adapter: Any, snapshot: Any, actions: Sequence[Any], context: Any, step_index: int) -> Any:
        from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions

        # Issue 65's process-time metering wrapper is transparent and passes
        # this exact history adapter through its _adapter field.
        if not source_lock_integrity.adapter_matches_history_target(adapter, self._adapter):
            raise StudyFailure("controlled runner replaced the adapter during action-history capture")
        state_fingerprint = adapter.checkpoint_fingerprint(snapshot)
        decision_trace_index = len(self._controller.trace)
        identities = action_identity_dicts_for_actions(actions)
        event: dict[str, Any] = {
            "event_index": len(self._adapter.events),
            "step_index": int(step_index),
            "controller_trace_index": decision_trace_index,
            "screen_state": str(context.screen_state),
            "state_fingerprint": state_fingerprint,
            "state_fingerprints": _snapshot_debug_fingerprints(adapter, snapshot),
            "public_projection": _public_projection_payload(adapter, snapshot),
            "legal_action_occurrences": identities,
        }
        decision = self._controller.select_action(adapter, snapshot, actions, context, step_index)
        selected_index = int(getattr(decision, "selected_index", -1))
        if selected_index < 0 or selected_index >= len(actions):
            raise StudyFailure("controller did not return an in-range native legal occurrence")
        event["selected_identity"] = identities[selected_index]
        self._adapter.bind_event(event, actions, selected_index)
        return decision


def _run_one(
    *, out: Path, module: Any, model: AllNoncombatPolicy, model_digest: str,
    seed: int, arm: str, encoder: SemanticHashEncoder, capture_checkpoints: bool = False,
    checkpoint_screens: Sequence[str] | None = None,
    checkpoint: Any | None = None, forced_root: Mapping[str, Any] | None = None,
    purpose: str = "policy", resource_label: str | None = None,
    adapter_override: Any | None = None, durable: bool = True,
    capture_trace: bool = True, capture_all_roots: bool = False,
    suffix_random_seed: int | None = None, expert_random_seed: int | None = None,
    root_callback: Any | None = None, cpu_cap_seconds: float | None = None,
    capture_action_history: bool = False,
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
        checkpoint_screens=checkpoint_screens,
        capture_all_roots=capture_all_roots,
        forced_root=forced_root, sampling_purpose=purpose, capture_trace=capture_trace,
        suffix_random_seed=suffix_random_seed, expert_random_seed=expert_random_seed,
        root_callback=root_callback,
    )
    controller.run_id = label
    adapter = adapter_override or LightSpeedAdapter(seed=seed, ascension=20, player_class="IRONCLAD", module=module)
    history_adapter = ActionHistoryAdapter(adapter) if capture_action_history else None
    if history_adapter is not None:
        controller = ActionHistoryController(controller, history_adapter)
    run_adapter = history_adapter if history_adapter is not None else adapter
    run_adapter = CheckpointAdapter(run_adapter, checkpoint) if checkpoint is not None else run_adapter
    started_cpu = process_tree_cpu()
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
        if cpu_cap_seconds is not None and process_tree_cpu() - started_cpu > cpu_cap_seconds:
            raise CpuBudgetExceeded("run exceeded its predeclared process-CPU ceiling", receipt={
                "failure_boundary": "per_attempt_cpu_cap", "cpu_cap_seconds": cpu_cap_seconds,
                "process_cpu_seconds": process_tree_cpu() - started_cpu,
            })
        nonlocal observer_steps
        observer_steps += 1
        if durable and observer_steps % 32 == 0:
            atomic_json(progress_path, {
                "study_id": STUDY_ID, "run": label, "seed": seed, "arm": arm,
                "boundary": "controlled_run_transition_batch",
                "completed_transitions": observer_steps,
                "process_tree_cpu_seconds": process_tree_cpu() - started_cpu,
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
                "cpu_seconds": process_tree_cpu() - started_cpu,
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
            raise StudyFailure(
                f"{label} is incomplete or invalid: {run.problems}",
                receipt={
                    "failure_boundary": "controlled_run_returned_incomplete",
                    "terminal_complete": False,
                    "terminal": bool(run.terminal),
                    "outcome": str(run.outcome),
                    "problems": list(run.problems),
                    "completed_transitions": observer_steps,
                    "requested_steps": int(run.requested_steps),
                    "screen_counts": dict(controller.screen_counts),
                    "native_source_commit": EXPECTED_NATIVE,
                },
            )
        terminal = _episode_terminal_fields(run)
        if capture_trace and durable:
            _save_decisions_npz(decision_path, controller.trace)
        trace_summary = [
            {key: value for key, value in item.items() if key != "matrix"}
            for item in controller.trace
        ]
        forced_root_receipt = None
        if forced_root is not None:
            forced_trace = next(
                (item for item in controller.trace
                 if item.get("kind") == "categorical"
                 and str(item.get("native_screen", item.get("screen", ""))) == str(forced_root.get("screen"))),
                None,
            )
            if forced_trace is not None:
                forced_index = int(forced_trace["chosen_index"])
                forced_identities = forced_trace.get("legal_action_identities", [])
                forced_root_receipt = {
                    "screen": str(forced_trace.get("native_screen", forced_trace.get("screen"))),
                    "forced_root_consumed": not controller.forced_root_pending,
                    "candidate_index": forced_index,
                    "policy_sampled_index": forced_trace.get("policy_sampled_index"),
                    "probabilities_sha256": sha256_bytes(canonical(forced_trace.get("probabilities")).encode("utf-8")),
                    "candidate_identity_sha256": sha256_bytes(
                        canonical(forced_identities[forced_index]).encode("utf-8")
                    ) if 0 <= forced_index < len(forced_identities) else None,
                    "legal_action_identities_sha256": sha256_bytes(canonical(forced_identities).encode("utf-8")),
                }
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
            "decision_artifact_bytes": decision_path.stat().st_size if capture_trace and durable else None,
            "decisions": trace_summary,
            "screen_counts": dict(controller.screen_counts),
            "action_kind_counts": dict(controller.action_kind_counts),
            "exposures": dict(controller.exposures),
            "native_source_commit": EXPECTED_NATIVE,
            "native_module_sha256": CONFIG["identity"]["native_binary_sha256"],
            "cpu_seconds": process_tree_cpu() - started_cpu,
            "wall_seconds": time.monotonic() - started_wall,
            "max_rss_kib_sampled_for_run": run_peak_rss_kib,
            "process_peak_rss_kib_cumulative": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
            "public_history_runtime": runtime.installation_record(),
            "phase_metrics": telemetry.get("phases", {}),
            "completed_native_terminal": True,
            "forced_root_receipt": forced_root_receipt,
            "problems": [],
        }
        if durable:
            atomic_json(run_path, result)
            atomic_json(progress_path, {"study_id": STUDY_ID, "run": label, "boundary": "terminal_committed", "completed_transitions": observer_steps})
        if capture_checkpoints:
            result["_volatile_roots"] = controller.roots
        if history_adapter is not None:
            if history_adapter._current_event is not None:
                raise StudyFailure("action-history run completed with a decision that was never stepped")
            result["_actual_action_history"] = history_adapter.events
        return result
    except BaseException as exc:
        setattr(exc, "issue73_captured_roots", controller.roots)
        if durable and not run_path.exists():
            receipt = {
                "study_id": STUDY_ID, "run_id": label, "seed": seed, "arm": arm,
                "terminal_complete": False, "failure_boundary": "native_or_controller_exception",
                "error_type": type(exc).__name__, "error": str(exc)[:800],
                "completed_transitions": observer_steps,
                "cpu_seconds": max(0.0, process_tree_cpu() - started_cpu),
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
    if not optimizer_path.is_file():
        raise StudyFailure("Issue 65 study-local optimizer is missing or changed")
    validate_locked_optimizer_source(optimizer_path)
    validate_light_speed_adapter_class(LightSpeedAdapter)
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
    _stage1_note_durable_run_receipt(out, result)


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


def _branch_accounting_from_disk(out: Path) -> BranchAccounting:
    charges: dict[tuple[int, int], dict[str, float]] = {}
    attempt_cpu_seconds = 0.0
    last_committed: dict[tuple[int, int], str] = {}
    last_committed_order: dict[tuple[int, int], int] = {}
    durable_attempt_ids: set[str] = set()
    receipt_parent_wait_by_execution: Counter[str] = Counter()

    def attempt_order(attempt_id: str) -> int:
        try:
            return int(attempt_id.rsplit(":", 2)[-2])
        except (IndexError, TypeError, ValueError):
            return -1

    def note_committed_attempt(key: tuple[int, int], attempt_id: str) -> None:
        order = attempt_order(attempt_id)
        if order >= last_committed_order.get(key, -1):
            last_committed[key] = attempt_id
            last_committed_order[key] = order

    def charge_for(key: tuple[int, int]) -> dict[str, float]:
        return charges.setdefault(key, {"total": 0.0, "child": 0.0, "parent": 0.0, "other": 0.0})

    for path in _json_paths(out / "branches", "*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            key = (int(item["seed"]), int(item["root_index"]))
            total = max(0.0, float(item.get("cpu_seconds_total", 0.0)))
            child = max(0.0, float(item.get("child_cpu_seconds", 0.0)))
            parent = max(0.0, float(item.get("parent_cpu_seconds", 0.0)))
            # Preserve the receipt's aggregate charge if an older schema lacks
            # its child/parent split; do not invent an attribution.
            other = max(0.0, total - child - parent)
            charge = charge_for(key)
            charge["total"] += total
            charge["child"] += child
            charge["parent"] += parent
            charge["other"] += other
            attempt_cpu_seconds += total
            attempt_id = str(item.get("attempt_id", ""))
            if attempt_id:
                # The atomic branch receipt is the durable commit point. If a
                # crash lands before the following coarse-state append, do not
                # reclassify the completed child as an interrupted attempt.
                durable_attempt_ids.add(attempt_id)
                note_committed_attempt(key, attempt_id)
            root_execution_id = str(item.get("root_execution_id", ""))
            if root_execution_id:
                receipt_parent_wait_by_execution[root_execution_id] += parent
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    states_path = out / "coarse-state.jsonl"
    started: dict[str, Mapping[str, Any]] = {}
    committed: set[str] = set(durable_attempt_ids)
    root_started: dict[str, Mapping[str, Any]] = {}
    latest_root_event: dict[str, Mapping[str, Any]] = {}
    if states_path.exists():
        for line in states_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            root_execution_id = str(event.get("root_execution_id", ""))
            boundary = event.get("boundary")
            if root_execution_id and boundary == "branch_root_started":
                root_started[root_execution_id] = event
            elif root_execution_id and boundary in {
                "branch_root_checkpoint", "branch_root_finished", "branch_root_failed",
                "branch_attempt_started", "branch_attempt_committed", "branch_attempt_cancelled",
            }:
                latest_root_event[root_execution_id] = event
            attempt_id = str(event.get("attempt_id", ""))
            if not attempt_id:
                continue
            if boundary == "branch_attempt_started":
                started[attempt_id] = event
            elif boundary == "branch_attempt_committed":
                committed.add(attempt_id)
                try:
                    key = (int(event["seed"]), int(event["root_index"]))
                    note_committed_attempt(key, attempt_id)
                except (KeyError, TypeError, ValueError):
                    pass
            elif boundary == "branch_attempt_cancelled":
                committed.add(attempt_id)
    for attempt_id, event in started.items():
        if attempt_id in committed:
            continue
        try:
            key = (int(event["seed"]), int(event["root_index"]))
            # A hard RLIMIT_CPU child can consume through the configured limit;
            # charge one extra second for the kernel tick and parent wait path.
            child = max(0.0, float(event["cpu_limit_seconds"]))
            parent = 1.0
            charge = charge_for(key)
            charge["total"] += child + parent
            charge["child"] += child
            charge["parent"] += parent
            attempt_cpu_seconds += child + parent
        except (KeyError, TypeError, ValueError):
            continue

    # Branch receipts already contain each child's CPU and parent wait. Add
    # only the surrounding root-session CPU from durable progress checkpoints,
    # avoiding double charging waits while still carrying root overhead across
    # a process crash/resume boundary.
    for root_execution_id, event in latest_root_event.items():
        try:
            key = (int(event["seed"]), int(event["root_index"]))
            parent_elapsed = max(0.0, float(event.get("parent_elapsed_cpu_seconds", 0.0)))
            committed_wait = max(0.0, float(event.get(
                "committed_parent_wait_cpu_seconds",
                receipt_parent_wait_by_execution[root_execution_id],
            )))
        except (KeyError, TypeError, ValueError):
            continue
        overhead = max(0.0, parent_elapsed - committed_wait)
        charge = charge_for(key)
        charge["total"] += overhead
        charge["parent"] += overhead

    # A crash between the durable start marker and its first checkpoint leaves
    # no reconstructible elapsed value. Conservatively consume the full root
    # allowance on resume instead of granting an unmeasured retry.
    for root_execution_id, event in root_started.items():
        if root_execution_id in latest_root_event:
            continue
        try:
            key = (int(event["seed"]), int(event["root_index"]))
            root_cap = max(0.0, float(event["root_aggregate_cpu_cap_seconds"]))
        except (KeyError, TypeError, ValueError):
            continue
        charge = charge_for(key)
        charge["total"] += root_cap
        charge["other"] += root_cap

    return BranchAccounting(charges, attempt_cpu_seconds, last_committed)


def _existing_cpu_charge(
    out: Path, *, branch_accounting: BranchAccounting | None = None,
) -> tuple[float, float]:
    """Return durable run, replay, phase-overhead and branch CPU across resumes."""
    runs = _run_and_replay_cpu_total(out)
    accounting = branch_accounting or _branch_accounting_from_disk(out)
    # Root-session parent overhead is already included in phase overhead. The
    # global branch allowance charges only attempts, not that same CPU twice.
    branches = accounting.attempt_cpu_seconds
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


def _stage1_budget_contract() -> dict[str, Any]:
    """Validate the locked Stage 1 totals against the registered design."""
    budget = CONFIG["resources"]["stage1_budget"]
    preflight_runs = (
        int(CONFIG["preflight"]["seed_last"]) - int(CONFIG["preflight"]["seed_first"]) + 1
    ) * len(CONFIG["preflight"]["arms"])
    preflight_owners = int(CONFIG["preflight"]["seed_last"]) - int(CONFIG["preflight"]["seed_first"]) + 1
    training_owners = int(CONFIG["training"]["seed_last"]) - int(CONFIG["training"]["seed_first"]) + 1
    evaluation_owners = int(CONFIG["evaluation"]["seed_last"]) - int(CONFIG["evaluation"]["seed_first"]) + 1
    future_owner_runs = (
        int(CONFIG["training"]["owner_count"])
        + int(CONFIG["evaluation"]["owner_count"]) * len(CONFIG["evaluation"]["arms"])
    )
    branch_allowance = (
        int(CONFIG["training"]["owner_count"])
        * int(CONFIG["continuations"]["roots_per_owner_max"])
        * float(CONFIG["continuations"]["aggregate_process_cpu_seconds_per_root_max"])
    )
    reviewer_total = float(budget["reviewer_reserve_min_seconds"])
    stage0_reviewer_credit = float(budget["stage0_reviewer_reserve_credit_seconds"])
    launch_gate_repair_cap = float(budget["launch_gate_repair_cpu_cap_seconds"])
    launch_gate_reviewer_credit = float(budget["launch_gate_repair_reviewer_credit_seconds"])
    cumulative_ceiling = float(budget["cumulative_ceiling_seconds"])
    if preflight_runs != int(budget["mandatory_preflight_owner_arm_count"]):
        raise StudyFailure("Stage 1 budget does not cover every mandatory P0/E preflight arm")
    if (
        preflight_owners != int(CONFIG["preflight"]["owner_count"])
        or training_owners != int(CONFIG["training"]["owner_count"])
        or evaluation_owners != int(CONFIG["evaluation"]["owner_count"])
    ):
        raise StudyFailure("Stage 1 CPU budget seed ranges differ from the registered owner counts")
    if future_owner_runs != int(PROJECTION["future_natural_owner_arm_runs"]):
        raise StudyFailure("Stage 1 future owner-arm budget differs from the registered training/evaluation design")
    if branch_allowance != float(budget["branch_allowance_seconds"]):
        raise StudyFailure("Stage 1 branch allowance must equal owners × roots × per-root CPU cap")
    if cumulative_ceiling != 60000 or int(budget["worker_consistency_repeat_run_count"]) != 2:
        raise StudyFailure("Stage 1 must retain the 60000-second ceiling and both worker-consistency runs")
    if reviewer_total < 8000 or stage0_reviewer_credit != REVIEWER_RESERVE:
        raise StudyFailure("Stage 1 reviewer reserve must include, not add to, the Stage 0 reviewer allocation")
    if reviewer_total <= stage0_reviewer_credit:
        raise StudyFailure("Stage 1 reviewer reserve leaves no incremental Stage 1 review allocation")
    if launch_gate_repair_cap != 2000 or launch_gate_reviewer_credit < 400 or launch_gate_reviewer_credit > launch_gate_repair_cap:
        raise StudyFailure("Stage 1 launch-gate repair reserve must stay within 2000 CPU-s including at least 400 Reviewer CPU-s")
    if reviewer_total <= stage0_reviewer_credit + launch_gate_reviewer_credit:
        raise StudyFailure("Stage 1 reviewer reserve does not cover both the Stage 0 and launch-gate review credits")
    return {
        **budget,
        "mandatory_preflight_owner_arm_count": preflight_runs,
        "worker_consistency_repeat_run_count": int(budget["worker_consistency_repeat_run_count"]),
        "future_natural_owner_arm_runs": future_owner_runs,
        "branch_allowance_seconds": branch_allowance,
        "run_cpu_inflation_factor": float(PROJECTION["run_cpu_inflation_factor"]),
        "engineering_margin_seconds": float(PROJECTION["engineering_margin_seconds"]),
        "reviewer_reserve_min_seconds": reviewer_total,
        "stage0_reviewer_reserve_credit_seconds": stage0_reviewer_credit,
        "launch_gate_repair_cpu_cap_seconds": launch_gate_repair_cap,
        "launch_gate_reviewer_reserve_credit_seconds": launch_gate_reviewer_credit,
        "launch_gate_builder_reserve_seconds": launch_gate_repair_cap - launch_gate_reviewer_credit,
        "stage1_reviewer_reserve_seconds": reviewer_total - stage0_reviewer_credit - launch_gate_reviewer_credit,
    }


def _stage1_budget_projection(
    *, stage0_builder_cpu: float, stage1_charged_cpu: float,
    pending_preflight_runs: int, pending_worker_repeats: int,
    pending_owner_runs: int, max_run_cpu: float, branch_cpu_remaining: float,
) -> dict[str, Any]:
    """Project cumulative Stage 0 + Stage 1 CPU without double-counting review."""
    contract = _stage1_budget_contract()
    future_runs = (
        max(0, int(pending_preflight_runs))
        + max(0, int(pending_worker_repeats))
        + max(0, int(pending_owner_runs))
    )
    inflation = float(contract["run_cpu_inflation_factor"])
    estimated_future_run_cpu = future_runs * max(0.0, float(max_run_cpu)) * inflation
    branches = max(0.0, float(branch_cpu_remaining))
    margin = float(contract["engineering_margin_seconds"])
    launch_gate_builder_reserve = float(contract["launch_gate_builder_reserve_seconds"])
    reviewer_total = float(contract["reviewer_reserve_min_seconds"])
    ceiling = float(contract["cumulative_ceiling_seconds"])
    projected_builder = (
        max(0.0, float(stage0_builder_cpu))
        + max(0.0, float(stage1_charged_cpu))
        + estimated_future_run_cpu + branches + margin + launch_gate_builder_reserve
    )
    projected_all_in = projected_builder + reviewer_total
    builder_ceiling = ceiling - reviewer_total
    return {
        "stage0_builder_cpu_charged_seconds": max(0.0, float(stage0_builder_cpu)),
        "stage1_cpu_charged_seconds": max(0.0, float(stage1_charged_cpu)),
        "pending_preflight_owner_arm_runs": max(0, int(pending_preflight_runs)),
        "pending_worker_consistency_repeat_runs": max(0, int(pending_worker_repeats)),
        "pending_training_and_evaluation_owner_arm_runs": max(0, int(pending_owner_runs)),
        "run_cpu_max_observed_seconds": max(0.0, float(max_run_cpu)),
        "run_cpu_inflation_factor": inflation,
        "projected_future_owner_arm_cpu_seconds": estimated_future_run_cpu,
        "remaining_branch_allowance_cpu_seconds": branches,
        "engineering_margin_cpu_seconds": margin,
        "launch_gate_repair_cpu_cap_seconds": float(contract["launch_gate_repair_cpu_cap_seconds"]),
        "launch_gate_builder_repair_reserve_seconds": launch_gate_builder_reserve,
        "launch_gate_reviewer_reserve_credited_seconds": float(contract["launch_gate_reviewer_reserve_credit_seconds"]),
        "stage0_reviewer_reserve_credited_seconds": float(contract["stage0_reviewer_reserve_credit_seconds"]),
        "incremental_stage1_reviewer_reserve_seconds": float(contract["stage1_reviewer_reserve_seconds"]),
        "reviewer_reserve_total_seconds": reviewer_total,
        "projected_builder_cpu_seconds": projected_builder,
        "projected_cumulative_all_in_cpu_seconds": projected_all_in,
        "cumulative_ceiling_seconds": ceiling,
        "builder_ceiling_after_reviewer_reserve_seconds": builder_ceiling,
        "within_builder_ceiling": projected_builder <= builder_ceiling,
        "within_all_in_ceiling": projected_all_in <= ceiling,
        "remaining_builder_headroom_seconds": max(0.0, builder_ceiling - projected_builder),
    }


def _require_stage1_projection(projection: Mapping[str, Any]) -> None:
    if not projection.get("within_builder_ceiling") or not projection.get("within_all_in_ceiling"):
        raise CpuBudgetExceeded(
            "SCIENTIFIC_INCOMPLETE_RESOURCE_PROJECTION: Stage 1 cumulative CPU projection exceeds the locked ceiling"
        )


def _stage1_stage0_baseline() -> dict[str, Any]:
    """Load and verify the accepted Stage 0 report that anchors cumulative CPU."""
    global STAGE1_STAGE0_BASELINE_CACHE
    if STAGE1_STAGE0_BASELINE_CACHE is not None:
        return STAGE1_STAGE0_BASELINE_CACHE
    contract = _stage1_budget_contract()
    stage0 = ROOT / str(contract["stage0_artifact_directory"])
    report_path = stage0 / "stage0-report.json"
    manifest_path = stage0 / "artifact-manifest.json"
    if not report_path.is_file() or not manifest_path.is_file():
        raise StudyFailure("Stage 1 requires the reviewed, checksummed Stage 0 evidence bundle")
    if sha256_file(report_path) != str(contract["stage0_report_sha256"]):
        raise StudyFailure("Stage 0 report differs from the accepted preseed baseline")
    if sha256_file(manifest_path) != str(contract["stage0_manifest_sha256"]):
        raise StudyFailure("Stage 0 artifact manifest differs from the accepted preseed baseline")
    if not _validate_artifact_manifest(stage0):
        raise StudyFailure("Stage 0 artifact manifest does not match its files")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (
        report.get("status") != "STAGE0_ENGINEERING_PASS_PENDING_INDEPENDENT_REVIEW"
        or report.get("config_sha256") != contract["stage0_config_sha256"]
        or report.get("formal_training_seed_consumed") is not False
        or report.get("formal_evaluation_seed_consumed") is not False
    ):
        raise StudyFailure("Stage 0 report does not carry the accepted no-formal-seed disposition")
    projection = report.get("resource_projection", {})
    charged = float(projection.get("current_builder_cpu_charged_seconds", -1.0))
    maximum = float(projection.get("observed_max_natural_run_cpu_seconds", -1.0))
    if not math.isclose(charged, float(contract["stage0_builder_cpu_seconds"]), rel_tol=0.0, abs_tol=1e-9):
        raise StudyFailure("Stage 0 cumulative CPU baseline differs from the locked accepted value")
    if maximum <= 0:
        raise StudyFailure("Stage 0 report has no positive observed natural-run CPU for conservative projection")
    STAGE1_STAGE0_BASELINE_CACHE = {
        "builder_cpu_seconds": charged, "max_run_cpu_seconds": maximum, "report": report,
    }
    return STAGE1_STAGE0_BASELINE_CACHE


def _stage1_completed_record(record: Mapping[str, Any], *, seed: int, arm: str) -> bool:
    try:
        return bool(
            record.get("terminal_complete") is True
            and int(record.get("seed", -1)) == int(seed)
            and str(record.get("arm")) == arm
        )
    except (TypeError, ValueError):
        return False


def _stage1_expected_run_receipts() -> dict[str, tuple[int, str]]:
    expected: dict[str, tuple[int, str]] = {}
    for seed in range(int(CONFIG["preflight"]["seed_first"]), int(CONFIG["preflight"]["seed_last"]) + 1):
        for arm in CONFIG["preflight"]["arms"]:
            expected[f"PRE_{seed}_{arm}.json"] = (seed, str(arm))
    worker_seed = int(CONFIG["preflight"]["seed_first"])
    expected[f"PRE_WORKER_CHECK_{worker_seed}_P0.json"] = (worker_seed, "P0")
    expected[f"PRE_WORKER_COMPANION_{worker_seed}_E.json"] = (worker_seed, "E")
    for seed in range(int(CONFIG["training"]["seed_first"]), int(CONFIG["training"]["seed_last"]) + 1):
        expected[f"TRAIN_{seed}.json"] = (seed, "TRAIN")
    for seed in range(int(CONFIG["evaluation"]["seed_first"]), int(CONFIG["evaluation"]["seed_last"]) + 1):
        for arm in CONFIG["evaluation"]["arms"]:
            expected[f"EVAL_{seed}_{arm}.json"] = (seed, str(arm))
    return expected


def _refresh_stage1_worker_repeat_snapshot(snapshot: Stage1AccountingSnapshot) -> None:
    repeats = int(_stage1_budget_contract()["worker_consistency_repeat_run_count"])
    worker_path = snapshot.out / "worker-consistency.json"
    if not worker_path.is_file():
        snapshot.worker_repeats = repeats
        return
    try:
        worker = json.loads(worker_path.read_text(encoding="utf-8"))
        seed = int(CONFIG["preflight"]["seed_first"])
        worker_ok = bool(
            worker.get("terminal_and_decision_trace_equal") is True
            and f"PRE_WORKER_CHECK_{seed}_P0.json" in snapshot.completed_run_receipts
            and f"PRE_WORKER_COMPANION_{seed}_E.json" in snapshot.completed_run_receipts
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        worker_ok = False
    snapshot.worker_repeats = 0 if worker_ok else repeats


def _load_stage1_accounting_snapshot(
    out: Path, *, branch_accounting: BranchAccounting | None = None,
) -> Stage1AccountingSnapshot:
    """Read receipts once at phase start; runtime updates follow durable commits."""
    expected = _stage1_expected_run_receipts()
    completed: set[str] = set()
    completed_preflight = 0
    completed_training = 0
    completed_evaluation = 0
    max_run_cpu = 0.0
    for path in _json_paths(out / "runs", "*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("terminal_complete"):
            try:
                cpu_seconds = float(record.get("cpu_seconds", 0.0))
                if cpu_seconds > 0.0:
                    max_run_cpu = max(max_run_cpu, cpu_seconds)
            except (TypeError, ValueError):
                pass
        specification = expected.get(path.name)
        if specification is not None and _stage1_completed_record(
            record, seed=specification[0], arm=specification[1],
        ):
            completed.add(path.name)
            if path.name.startswith("PRE_") and not path.name.startswith("PRE_WORKER_"):
                completed_preflight += 1
            elif path.name.startswith("TRAIN_"):
                completed_training += 1
            elif path.name.startswith("EVAL_"):
                completed_evaluation += 1

    accounting = branch_accounting or _branch_accounting_from_disk(out)
    snapshot = Stage1AccountingSnapshot(
        out=out.resolve(), expected_run_receipts=expected,
        completed_run_receipts=completed,
        completed_preflight_runs=completed_preflight,
        completed_training_runs=completed_training,
        completed_evaluation_runs=completed_evaluation,
        worker_repeats=0,
        max_run_cpu_seconds=max_run_cpu,
        branch_per_root={key: dict(value) for key, value in accounting.per_root.items()},
        last_committed_attempt_by_root=dict(accounting.last_committed_attempt_by_root),
    )
    _refresh_stage1_worker_repeat_snapshot(snapshot)
    return snapshot


def _stage1_accounting_snapshot_for(out: Path) -> Stage1AccountingSnapshot:
    global STAGE1_ACCOUNTING_SNAPSHOT
    resolved = out.resolve()
    if STAGE1_ACCOUNTING_SNAPSHOT is None or STAGE1_ACCOUNTING_SNAPSHOT.out != resolved:
        STAGE1_ACCOUNTING_SNAPSHOT = _load_stage1_accounting_snapshot(out)
    return STAGE1_ACCOUNTING_SNAPSHOT


def _stage1_note_durable_run_receipt(out: Path, result: Mapping[str, Any]) -> None:
    snapshot = STAGE1_ACCOUNTING_SNAPSHOT
    if snapshot is None or snapshot.out != out.resolve():
        return
    run_id = str(result.get("run_id", ""))
    receipt_path = out / "runs" / f"{run_id}.json"
    specification = snapshot.expected_run_receipts.get(receipt_path.name)
    if not receipt_path.is_file():
        return
    try:
        durable_record = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if specification is not None and _stage1_completed_record(
        durable_record, seed=specification[0], arm=specification[1],
    ) and receipt_path.name not in snapshot.completed_run_receipts:
        snapshot.completed_run_receipts.add(receipt_path.name)
        if receipt_path.name.startswith("PRE_") and not receipt_path.name.startswith("PRE_WORKER_"):
            snapshot.completed_preflight_runs += 1
        elif receipt_path.name.startswith("TRAIN_"):
            snapshot.completed_training_runs += 1
        elif receipt_path.name.startswith("EVAL_"):
            snapshot.completed_evaluation_runs += 1
    if durable_record.get("terminal_complete"):
        try:
            cpu_seconds = float(durable_record.get("cpu_seconds", 0.0))
            if cpu_seconds > 0.0:
                snapshot.max_run_cpu_seconds = max(snapshot.max_run_cpu_seconds, cpu_seconds)
        except (TypeError, ValueError):
            pass
    worker_seed = int(CONFIG["preflight"]["seed_first"])
    if receipt_path.name in {
        f"PRE_WORKER_CHECK_{worker_seed}_P0.json",
        f"PRE_WORKER_COMPANION_{worker_seed}_E.json",
    }:
        _refresh_stage1_worker_repeat_snapshot(snapshot)


def _branch_root_snapshot_for(
    out: Path, key: tuple[int, int],
) -> tuple[dict[str, float], str | None]:
    snapshot = STAGE1_ACCOUNTING_SNAPSHOT
    if snapshot is not None and snapshot.out == out.resolve():
        return (
            dict(snapshot.branch_per_root.get(key, {"total": 0.0, "child": 0.0, "parent": 0.0, "other": 0.0})),
            snapshot.last_committed_attempt_by_root.get(key),
        )
    accounting = _branch_accounting_from_disk(out)
    return (
        dict(accounting.per_root.get(key, {"total": 0.0, "child": 0.0, "parent": 0.0, "other": 0.0})),
        accounting.last_committed_attempt_by_root.get(key),
    )


def _stage1_note_durable_branch_receipt(out: Path, record: Mapping[str, Any]) -> None:
    snapshot = STAGE1_ACCOUNTING_SNAPSHOT
    if snapshot is None or snapshot.out != out.resolve():
        return
    receipt_path = out / "branches" / f"{record.get('run_id', '')}.json"
    if not receipt_path.is_file():
        raise StudyFailure("branch accounting snapshot cannot advance before its receipt is durable")
    try:
        durable_record = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StudyFailure("durable branch receipt cannot be read for accounting snapshot update") from exc
    if (
        durable_record.get("run_id") != record.get("run_id")
        or durable_record.get("attempt_id") != record.get("attempt_id")
        or durable_record.get("root_execution_id") != record.get("root_execution_id")
    ):
        raise StudyFailure("durable branch receipt identity changed before accounting snapshot update")
    try:
        key = (int(durable_record["seed"]), int(durable_record["root_index"]))
        total = max(0.0, float(durable_record.get("cpu_seconds_total", 0.0)))
        child = max(0.0, float(durable_record.get("child_cpu_seconds", 0.0)))
        parent = max(0.0, float(durable_record.get("parent_cpu_seconds", 0.0)))
    except (KeyError, TypeError, ValueError):
        raise StudyFailure("durable branch receipt has invalid CPU accounting fields")
    charge = snapshot.branch_per_root.setdefault(
        key, {"total": 0.0, "child": 0.0, "parent": 0.0, "other": 0.0},
    )
    charge["total"] += total
    charge["child"] += child
    charge["parent"] += parent
    charge["other"] += max(0.0, total - child - parent)
    attempt_id = str(durable_record.get("attempt_id", ""))
    if attempt_id:
        snapshot.last_committed_attempt_by_root[key] = attempt_id


def _stage1_note_durable_root_overhead(
    out: Path, *, key: tuple[int, int], overhead_seconds: float,
) -> None:
    snapshot = STAGE1_ACCOUNTING_SNAPSHOT
    if snapshot is None or snapshot.out != out.resolve():
        return
    overhead = max(0.0, overhead_seconds)
    charge = snapshot.branch_per_root.setdefault(
        key, {"total": 0.0, "child": 0.0, "parent": 0.0, "other": 0.0},
    )
    charge["total"] += overhead
    charge["parent"] += overhead


def _branch_root_cpu_fields(
    *, seed: int, root_index: int, root_execution_id: str,
    root_started: float, root_cap: float, historical_charge: Mapping[str, float],
    current_child_cpu: float, committed_parent_wait: float,
    last_committed_attempt_id: str | None,
) -> dict[str, Any]:
    parent_elapsed = max(0.0, time.process_time() - root_started)
    historical_total = max(0.0, float(historical_charge.get("total", 0.0)))
    child_cpu = max(0.0, float(historical_charge.get("child", 0.0))) + max(0.0, current_child_cpu)
    parent_cpu = (
        max(0.0, float(historical_charge.get("parent", 0.0)))
        + max(0.0, float(historical_charge.get("other", 0.0)))
        + parent_elapsed
    )
    aggregate_cpu = historical_total + parent_elapsed + max(0.0, current_child_cpu)
    return {
        "root_execution_id": root_execution_id,
        "process_id": os.getpid(),
        "seed": seed,
        "root_index": root_index,
        "root_started_process_cpu_seconds": root_started,
        "historical_root_charge_seconds": historical_total,
        "historical_child_cpu_seconds": max(0.0, float(historical_charge.get("child", 0.0))),
        "historical_parent_cpu_seconds": (
            max(0.0, float(historical_charge.get("parent", 0.0)))
            + max(0.0, float(historical_charge.get("other", 0.0)))
        ),
        "parent_elapsed_cpu_seconds": parent_elapsed,
        "current_child_cpu_seconds": max(0.0, current_child_cpu),
        "committed_parent_wait_cpu_seconds": max(0.0, committed_parent_wait),
        "cumulative_child_cpu_seconds": child_cpu,
        "cumulative_parent_cpu_seconds": parent_cpu,
        "aggregate_root_cpu_seconds": aggregate_cpu,
        "root_aggregate_cpu_cap_seconds": root_cap,
        "root_cpu_remaining_seconds": root_cap - aggregate_cpu,
        "last_committed_attempt_id": last_committed_attempt_id,
    }


def _append_branch_root_event(
    out: Path, *, boundary: str, event_fields: Mapping[str, Any],
    checkpoint_kind: str, candidate_index: int | None = None,
    child_cpu_limit_seconds: int | None = None, stop_reason: str | None = None,
) -> dict[str, Any]:
    event = {
        "study_id": STUDY_ID,
        "boundary": boundary,
        "checkpoint_kind": checkpoint_kind,
        **dict(event_fields),
        "candidate_index": candidate_index,
        "child_cpu_limit_seconds": child_cpu_limit_seconds,
        "stop_reason": stop_reason,
    }
    append_jsonl(out / "coarse-state.jsonl", event)
    return event


def _stage1_budget_projection_for(out: Path, phase: str) -> dict[str, Any]:
    contract = _stage1_budget_contract()
    stage0 = _stage1_stage0_baseline()
    snapshot = _stage1_accounting_snapshot_for(out)
    preflight_specs_count = (
        int(CONFIG["preflight"]["seed_last"])
        - int(CONFIG["preflight"]["seed_first"]) + 1
    ) * len(CONFIG["preflight"]["arms"])
    pending_preflight = max(0, preflight_specs_count - snapshot.completed_preflight_runs)
    worker_repeats = snapshot.worker_repeats

    training_specs_count = (
        int(CONFIG["training"]["seed_last"])
        - int(CONFIG["training"]["seed_first"]) + 1
    )
    evaluation_specs_count = (
        int(CONFIG["evaluation"]["seed_last"])
        - int(CONFIG["evaluation"]["seed_first"]) + 1
    ) * len(CONFIG["evaluation"]["arms"])
    pending_training = max(0, training_specs_count - snapshot.completed_training_runs)
    pending_evaluation = max(0, evaluation_specs_count - snapshot.completed_evaluation_runs)
    training_status_path = out / "training-status.json"
    training_complete = False
    if training_status_path.is_file():
        try:
            training_complete = json.loads(training_status_path.read_text(encoding="utf-8")).get("status") == "TRAINING_COMPLETE"
        except (OSError, json.JSONDecodeError):
            training_complete = False
    if phase == "evaluate":
        pending_owner_runs = pending_evaluation
        branch_remaining = 0.0
    elif training_complete:
        pending_owner_runs = pending_evaluation
        branch_remaining = 0.0
    else:
        pending_owner_runs = pending_training + pending_evaluation
        branch_remaining = max(
            0.0,
            float(contract["branch_allowance_seconds"])
            - BASELINE_BRANCH_CPU - CPU_BRANCH_CHARGE_CURRENT,
        )

    observed = [float(stage0["max_run_cpu_seconds"]), snapshot.max_run_cpu_seconds]
    max_run_cpu = max(observed)
    stage1_charged = BASELINE_PARENT_CPU + BASELINE_BRANCH_CPU + time.process_time() + CPU_CHILD_CHARGE
    return _stage1_budget_projection(
        stage0_builder_cpu=float(stage0["builder_cpu_seconds"]),
        stage1_charged_cpu=stage1_charged,
        pending_preflight_runs=pending_preflight,
        pending_worker_repeats=worker_repeats,
        pending_owner_runs=pending_owner_runs,
        max_run_cpu=max_run_cpu,
        branch_cpu_remaining=branch_remaining,
    )


def _check_budget(out: Path, *, reserve_branches: float = 0.0) -> None:
    if ACTIVE_STAGE1_PHASE is not None:
        del reserve_branches
        _require_stage1_projection(_stage1_budget_projection_for(out, ACTIVE_STAGE1_PHASE))
        return
    if _charged_now(out) + max(0.0, reserve_branches) + CPU_MARGIN >= MAX_CPU:
        raise CpuBudgetExceeded("Builder CPU ceiling plus current branch reserve is exhausted")


def _install_cpu_cap(out: Path) -> dict[str, Any]:
    if ACTIVE_STAGE1_PHASE is not None:
        projection = _stage1_budget_projection_for(out, ACTIVE_STAGE1_PHASE)
        _require_stage1_projection(projection)
        absolute_process_limit = int(math.floor(
            time.process_time() + float(projection["remaining_builder_headroom_seconds"])
        ))
        current = int(time.process_time())
        if absolute_process_limit <= current:
            raise CpuBudgetExceeded("remaining cumulative Stage 1 CPU budget is below the process start allowance")
        soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
        if hard != resource.RLIM_INFINITY:
            absolute_process_limit = min(absolute_process_limit, int(hard))
        resource.setrlimit(resource.RLIMIT_CPU, (absolute_process_limit, absolute_process_limit))
        return {
            "rlimit_cpu_seconds": absolute_process_limit,
            "stage0_builder_cpu_seconds": projection["stage0_builder_cpu_charged_seconds"],
            "stage1_prior_parent_cpu_seconds": BASELINE_PARENT_CPU,
            "stage1_prior_branch_cpu_seconds": BASELINE_BRANCH_CPU,
            "projected_future_owner_cpu_seconds": projection["projected_future_owner_arm_cpu_seconds"],
            "reserved_branch_cpu_seconds": projection["remaining_branch_allowance_cpu_seconds"],
            "reviewer_reserve_total_seconds": projection["reviewer_reserve_total_seconds"],
            "cumulative_ceiling_seconds": projection["cumulative_ceiling_seconds"],
            "engineering_margin_seconds": projection["engineering_margin_cpu_seconds"],
        }
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


def _write_child_payload(fd: int, payload: Mapping[str, Any]) -> None:
    data = canonical(payload).encode("utf-8")
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("child diagnostic pipe accepted no bytes")
        view = view[written:]


def _looks_like_invalid_choice(*values: Any) -> bool:
    text = " ".join(str(value) for value in values if value is not None).lower()
    markers = (
        "invalid choice", "invalid action", "illegal action", "not a legal",
        "not among legal", "selected index", "selected action index",
        "outside the active action space", "out of range", "candidate binding",
        "candidate occurrence", "does not map to a live native legal action",
        "restored branch root legal native actions differ",
    )
    native_bounds_problem = (
        "selected action index" in text
        and " outside " in text
        and " legal actions" in text
    )
    return native_bounds_problem or any(marker in text for marker in markers)


def _child_failure_class(
    *, wait_status: int, payload: Mapping[str, Any] | None,
    raw_payload: bytes, cpu_limit: int, child_cpu: float,
) -> str:
    if os.WIFSIGNALED(wait_status):
        signum = os.WTERMSIG(wait_status)
        if signum == getattr(signal, "SIGXCPU", -1):
            return "cpu_cap"
        if signum == getattr(signal, "SIGKILL", -2) and child_cpu >= max(0.0, cpu_limit - 0.25):
            return "cpu_cap"
        return "worker_death"
    if payload is None:
        return "malformed_pipe" if raw_payload else "worker_exit_without_receipt"
    exit_code = os.WEXITSTATUS(wait_status) if os.WIFEXITED(wait_status) else None
    receipt = payload.get("run_receipt")
    problems = receipt.get("problems", []) if isinstance(receipt, Mapping) else payload.get("problems", [])
    diagnostic_text = [payload.get("error"), problems]
    if payload.get("error_type"):
        if _looks_like_invalid_choice(*diagnostic_text):
            return "invalid_choice"
        if isinstance(receipt, Mapping) and receipt.get("terminal_complete") is False:
            return "incomplete_terminal"
        return "child_exception"
    if exit_code != 0:
        return "child_nonzero_exit"
    if payload.get("terminal_complete") is True:
        return "complete"
    if payload.get("terminal_complete") is False:
        return "invalid_choice" if _looks_like_invalid_choice(*diagnostic_text) else "incomplete_terminal"
    return "invalid_receipt"


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
            child_identity = {
                "seed": seed,
                "root_index": root_index,
                "root_screen": str(root["screen"]),
                "root_decision_index": root.get("decision_index"),
                "native_checkpoint_id": root.get("native_checkpoint_id"),
                "candidate_index": candidate_index,
                "legal_action_identities_sha256": sha256_bytes(
                    canonical(root["legal_action_identities"]).encode("utf-8")
                ),
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
                **child_identity,
                "terminal_complete": bool(result.get("terminal_complete")),
                "terminal": result.get("terminal"),
                "completed_transitions": result.get("completed_transitions"),
                "screen_counts": result.get("screen_counts"),
                "action_kind_counts": result.get("action_kind_counts"),
                "exposures": result.get("exposures"),
                "problems": result.get("problems", []),
                "forced_root_receipt": result.get("forced_root_receipt"),
                "native_source_commit": EXPECTED_NATIVE,
            }
            _write_child_payload(write_fd, payload)
            os.close(write_fd)
            os._exit(0)
        except BaseException as exc:
            try:
                _write_child_payload(write_fd, {
                    **locals().get("child_identity", {}),
                    "terminal_complete": False,
                    "failure_boundary": "child_exception",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "run_receipt": getattr(exc, "receipt", None),
                    "native_source_commit": EXPECTED_NATIVE,
                })
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
    global CPU_CHILD_CHARGE
    CPU_CHILD_CHARGE += child_cpu
    raw_payload = b"".join(chunks)
    payload: Any = None
    parse_error: str | None = None
    if raw_payload:
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            parse_error = f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, Mapping):
        if raw_payload and parse_error is None:
            parse_error = "child pipe JSON receipt was not an object"
        payload = None
    completed = (
        os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        and isinstance(payload, Mapping) and payload.get("terminal_complete") is True
    )
    failure_class = _child_failure_class(
        wait_status=status, payload=payload, raw_payload=raw_payload,
        cpu_limit=cpu_limit, child_cpu=child_cpu,
    )
    result_payload = dict(payload) if isinstance(payload, Mapping) else {
        "terminal_complete": False,
        "terminal": None,
        "native_source_commit": EXPECTED_NATIVE,
    }
    signal_name = None
    if os.WIFSIGNALED(status):
        try:
            signal_name = signal.Signals(os.WTERMSIG(status)).name
        except ValueError:
            signal_name = f"signal_{os.WTERMSIG(status)}"
    result_payload.update({
        "terminal_complete": bool(completed),
        "child_failure_class": failure_class,
        "wait_status": int(status),
        "child_exit_code": int(os.WEXITSTATUS(status)) if os.WIFEXITED(status) else None,
        "child_signal_number": int(os.WTERMSIG(status)) if os.WIFSIGNALED(status) else None,
        "child_signal_name": signal_name,
        "child_cpu_seconds": child_cpu,
        "parent_wait_cpu_seconds": parent_cpu,
        "cpu_limit_seconds": cpu_limit,
        "raw_child_payload": raw_payload.decode("utf-8", errors="replace") if raw_payload else None,
        "raw_child_payload_base64": base64.b64encode(raw_payload).decode("ascii") if parse_error else None,
        "pipe_parse_error": parse_error,
    })
    return result_payload, child_cpu, parent_cpu, status


def _run_branches_for_owner(
    *, out: Path, module: Any, model: AllNoncombatPolicy, model_digest: str,
    encoder: SemanticHashEncoder, seed: int, original: Mapping[str, Any], roots: Sequence[Mapping[str, Any]],
    adapter: Any, replay_mode: bool = False, phase_label: str = "TRAIN",
    stop_on_incomplete: bool = False,
) -> list[dict[str, Any]]:
    global CPU_BRANCH_CHARGE_CURRENT
    completed: list[dict[str, Any]] = []
    root_limit = int(CONFIG["continuations"]["roots_per_owner_max"])
    root_cpu_cap = float(CONFIG["continuations"]["aggregate_process_cpu_seconds_per_root_max"])
    for fallback_root_index, root in enumerate(roots[:root_limit]):
        root_index = int(root.get("root_index", fallback_root_index))
        root_name = f"{phase_label}_{seed}_ROOT_{root_index}"
        root_key = (seed, root_index)
        historical_charge, historical_last_attempt = _branch_root_snapshot_for(out, root_key)
        root_execution_id = f"{root_name}:{time.time_ns()}:{os.getpid()}"
        root_started = time.process_time()
        current_child_cpu = 0.0
        current_parent_wait_cpu = 0.0
        last_committed_attempt_id = historical_last_attempt
        root_overhead_accounted = 0.0

        def root_fields() -> dict[str, Any]:
            fields = _branch_root_cpu_fields(
                seed=seed, root_index=root_index,
                root_execution_id=root_execution_id, root_started=root_started,
                root_cap=root_cpu_cap, historical_charge=historical_charge,
                current_child_cpu=current_child_cpu,
                committed_parent_wait=current_parent_wait_cpu,
                last_committed_attempt_id=last_committed_attempt_id,
            )
            fields.update({
                "phase_label": phase_label,
                "root_screen": str(root.get("screen")),
                "native_checkpoint_id": root.get("native_checkpoint_id"),
                "branch_allowance_remaining_cpu_seconds": max(
                    0.0, INITIAL_BRANCH_RESERVE - BASELINE_BRANCH_CPU - CPU_BRANCH_CHARGE_CURRENT,
                ),
            })
            return fields

        def checkpoint(
            checkpoint_kind: str, *, candidate_index: int | None = None,
            child_cpu_limit_seconds: int | None = None, stop_reason: str | None = None,
            boundary: str = "branch_root_checkpoint",
        ) -> dict[str, Any]:
            return _append_branch_root_event(
                out, boundary=boundary, event_fields=root_fields(),
                checkpoint_kind=checkpoint_kind, candidate_index=candidate_index,
                child_cpu_limit_seconds=child_cpu_limit_seconds, stop_reason=stop_reason,
            )

        def close_root(boundary: str, checkpoint_kind: str, stop_reason: str | None) -> dict[str, Any]:
            nonlocal root_overhead_accounted
            event = checkpoint(
                checkpoint_kind, stop_reason=stop_reason, boundary=boundary,
            )
            accumulated_overhead = max(
                0.0,
                float(event["parent_elapsed_cpu_seconds"]) - current_parent_wait_cpu,
            )
            overhead_delta = max(0.0, accumulated_overhead - root_overhead_accounted)
            _stage1_note_durable_root_overhead(
                out, key=root_key, overhead_seconds=overhead_delta,
            )
            root_overhead_accounted = max(root_overhead_accounted, accumulated_overhead)
            return event

        checkpoint("root_started", boundary="branch_root_started")
        alternatives = _root_alternatives(root)
        if phase_label == "DEBUG":
            alternatives = alternatives[:1]
        for candidate_index in alternatives:
            label = f"{phase_label}_{seed}_ROOT_{root_index}_ALT_{candidate_index}"
            path = out / "branches" / f"{label}.json"
            if path.exists():
                record = json.loads(path.read_text(encoding="utf-8"))
                completed.append(record)
                continue
            checkpoint("before_phase_budget_check", candidate_index=candidate_index)
            try:
                _check_budget(out, reserve_branches=0.0)
            except Exception as exc:
                close_root(
                    "branch_root_failed", "phase_budget_gate_failed",
                    f"phase_budget_gate:{type(exc).__name__}:{exc}",
                )
                raise

            root_metrics = root_fields()
            if float(root_metrics["aggregate_root_cpu_seconds"]) > root_cpu_cap:
                reason = "aggregate_root_cpu_cap_exceeded_before_next_child"
                close_root("branch_root_failed", "root_cpu_cap_exceeded", reason)
                raise StudyFailure(f"{root_name} exceeded the registered aggregate six-CPU-second cap")
            root_remaining = max(0.0, float(root_metrics["root_cpu_remaining_seconds"]))
            global_remaining = max(
                0.0, INITIAL_BRANCH_RESERVE - BASELINE_BRANCH_CPU - CPU_BRANCH_CHARGE_CURRENT,
            )
            limit = min(root_remaining, global_remaining)
            cpu_limit = int(math.floor(limit)) - 1  # retain the registered kernel-tick margin
            if cpu_limit < 1:
                record = {
                    "study_id": STUDY_ID, "run_id": label, "seed": seed,
                    "root_index": root_index, "candidate_index": candidate_index,
                    "status": "NOT_ATTEMPTED_CPU_MARGIN",
                    "terminal_complete": False, "cpu_seconds_total": 0.0,
                    "child_cpu_seconds": 0.0, "parent_cpu_seconds": 0.0,
                    "branch_provenance": "privileged_hindsight_completed_terminal_branch",
                    "expert_decisions": 0, "native_source_commit": EXPECTED_NATIVE,
                }
                atomic_json(path, record)
                completed.append(record)
                continue
            attempt_id = f"{label}:{time.time_ns()}:{os.getpid()}"
            while True:
                fields = root_fields()
                append_jsonl(out / "coarse-state.jsonl", {
                    "study_id": STUDY_ID, "boundary": "branch_attempt_started",
                    "attempt_id": attempt_id, "root_execution_id": root_execution_id,
                    "run_id": label, "seed": seed, "root_index": root_index,
                    "candidate_index": candidate_index, "cpu_limit_seconds": cpu_limit,
                    "root_parent_elapsed_cpu_seconds": fields["parent_elapsed_cpu_seconds"],
                    "root_child_cpu_seconds": fields["cumulative_child_cpu_seconds"],
                    "root_aggregate_cpu_seconds": fields["aggregate_root_cpu_seconds"],
                    "root_aggregate_cpu_cap_seconds": root_cpu_cap,
                    "root_cpu_remaining_seconds": fields["root_cpu_remaining_seconds"],
                    "branch_allowance_remaining_cpu_seconds": fields["branch_allowance_remaining_cpu_seconds"],
                    "last_committed_attempt_id": last_committed_attempt_id,
                    **fields,
                })
                after_start = root_fields()
                if float(after_start["aggregate_root_cpu_seconds"]) > root_cpu_cap:
                    reason = "aggregate_root_cpu_cap_exceeded_before_child_launch"
                    append_jsonl(out / "coarse-state.jsonl", {
                        "study_id": STUDY_ID, "boundary": "branch_attempt_cancelled",
                        "attempt_id": attempt_id, "run_id": label,
                        "candidate_index": candidate_index, "reason": reason,
                        "stop_reason": reason, **root_fields(),
                    })
                    close_root("branch_root_failed", "root_cpu_cap_exceeded", reason)
                    raise StudyFailure(f"{root_name} exceeded the registered aggregate six-CPU-second cap")
                after_remaining = min(
                    max(0.0, float(after_start["root_cpu_remaining_seconds"])),
                    max(0.0, INITIAL_BRANCH_RESERVE - BASELINE_BRANCH_CPU - CPU_BRANCH_CHARGE_CURRENT),
                )
                safe_limit = int(math.floor(after_remaining)) - 1
                if safe_limit >= cpu_limit:
                    break
                cpu_limit = safe_limit
                if cpu_limit < 1:
                    append_jsonl(out / "coarse-state.jsonl", {
                        "study_id": STUDY_ID, "boundary": "branch_attempt_cancelled",
                        "attempt_id": attempt_id, "run_id": label,
                        "candidate_index": candidate_index,
                        "reason": "telemetry write consumed the remaining registered child CPU margin",
                        "stop_reason": "telemetry_write_consumed_registered_child_cpu_margin",
                        **root_fields(),
                    })
                    record = {
                        "study_id": STUDY_ID, "run_id": label, "seed": seed,
                        "root_index": root_index, "candidate_index": candidate_index,
                        "status": "NOT_ATTEMPTED_CPU_MARGIN",
                        "terminal_complete": False, "cpu_seconds_total": 0.0,
                        "child_cpu_seconds": 0.0, "parent_cpu_seconds": 0.0,
                        "branch_provenance": "privileged_hindsight_completed_terminal_branch",
                        "expert_decisions": 0, "native_source_commit": EXPECTED_NATIVE,
                    }
                    atomic_json(path, record)
                    completed.append(record)
                    break
            if cpu_limit < 1:
                continue
            root_checkpoint = root["checkpoint"]
            result, child_cpu, parent_cpu, child_status = _branch_child(
                out=out, module=module, base_adapter=adapter,
                checkpoint=root_checkpoint, model=model, model_digest=model_digest,
                encoder=encoder, seed=seed, root_index=root_index, root=root,
                candidate_index=candidate_index, cpu_limit=cpu_limit, label=label,
            )
            current_child_cpu += child_cpu
            current_parent_wait_cpu += parent_cpu
            cpu_total = child_cpu + parent_cpu
            terminal = result.get("terminal") if isinstance(result, Mapping) else None
            forced_receipt = result.get("forced_root_receipt") if isinstance(result, Mapping) else None
            expected_identity_digest = sha256_bytes(
                canonical(root["legal_action_identities"][candidate_index]).encode("utf-8")
            )
            forced_choice_valid = bool(
                isinstance(forced_receipt, Mapping)
                and forced_receipt.get("forced_root_consumed") is True
                and forced_receipt.get("candidate_index") == candidate_index
                and forced_receipt.get("candidate_identity_sha256") == expected_identity_digest
                and forced_receipt.get("legal_action_identities_sha256") == sha256_bytes(
                    canonical(root["legal_action_identities"]).encode("utf-8")
                )
            )
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
                "status": (
                    "INVALID_FORCED_ROOT_RECEIPT"
                    if result and result.get("terminal_complete") and not forced_choice_valid
                    else "COMPLETED_NATIVE_TERMINAL"
                    if result and result.get("terminal_complete")
                    else f"INCOMPLETE_{str(result.get('child_failure_class', 'unknown')).upper()}"
                ),
                "terminal_complete": bool(result and result.get("terminal_complete")),
                "forced_root_choice_valid": forced_choice_valid,
                "forced_root_receipt": forced_receipt,
                "root_screen": str(root.get("screen")),
                "native_checkpoint_id": root.get("native_checkpoint_id"),
                "native_checkpoint_metadata": root.get("native_checkpoint_metadata", {}),
                "terminal": terminal,
                "superior_to_original_lexicographically": superior,
                "branch_provenance": "privileged_hindsight_completed_terminal_branch",
                "actual_episode_target_rewritten": False,
                "policy_used_for_all_suffix_nonbattle_choices": True,
                "expert_decisions": 0,
                "native_search_api": NATIVE_SEARCH_API,
                "native_source_commit": EXPECTED_NATIVE,
                "root_execution_id": root_execution_id,
                "child_status": int(child_status),
                "child_exit_code": result.get("child_exit_code") if isinstance(result, Mapping) else None,
                "child_signal_number": result.get("child_signal_number") if isinstance(result, Mapping) else None,
                "child_signal_name": result.get("child_signal_name") if isinstance(result, Mapping) else None,
                "child_failure_class": result.get("child_failure_class") if isinstance(result, Mapping) else "missing_result",
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
            atomic_json(path, record)
            last_committed_attempt_id = attempt_id
            root_metrics = root_fields()
            append_jsonl(out / "coarse-state.jsonl", {
                "study_id": STUDY_ID, "boundary": "branch_attempt_committed",
                "attempt_id": attempt_id, "root_execution_id": root_execution_id,
                "run_id": label, "seed": seed, "root_index": root_index,
                "status": record["status"], "cpu_seconds": cpu_total,
                **root_metrics,
            })
            CPU_BRANCH_CHARGE_CURRENT += cpu_total
            _stage1_note_durable_branch_receipt(out, record)
            completed.append(record)
            over_cap = float(root_metrics["aggregate_root_cpu_seconds"]) > root_cpu_cap
            if over_cap:
                close_root(
                    "branch_root_failed", "root_cpu_cap_exceeded_after_attempt",
                    "aggregate_root_cpu_cap_exceeded_after_attempt",
                )
                raise StudyFailure(f"{root_name} exceeded the registered aggregate six-CPU-second cap")
            if stop_on_incomplete and (
                not record["terminal_complete"] or not record["forced_root_choice_valid"]
            ):
                closed_root = close_root(
                    "branch_root_failed", "incomplete_child_stopped_root",
                    f"child_incomplete:{record.get('child_failure_class') or record.get('status')}",
                )
                if float(closed_root["aggregate_root_cpu_seconds"]) > root_cpu_cap:
                    raise StudyFailure(
                        f"{root_name} exceeded the registered aggregate six-CPU-second cap"
                    )
                return completed
        root_metrics = root_fields()
        if float(root_metrics["aggregate_root_cpu_seconds"]) > root_cpu_cap:
            close_root(
                "branch_root_failed", "root_cpu_cap_exceeded_at_root_end",
                "aggregate_root_cpu_cap_exceeded_at_root_end",
            )
            raise StudyFailure(f"{root_name} exceeded the registered aggregate six-CPU-second cap")
        close_root("branch_root_finished", "root_alternatives_complete", None)
        # Include the durable finish checkpoint itself in the cap decision.
        after_finish = root_fields()
        if float(after_finish["aggregate_root_cpu_seconds"]) > root_cpu_cap:
            close_root(
                "branch_root_failed", "root_cpu_cap_exceeded_after_finish_checkpoint",
                "aggregate_root_cpu_cap_exceeded_after_finish_checkpoint",
            )
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


def _require_stage1_authorized(phase: str) -> None:
    if CONFIG.get("stage0", {}).get("stage1_authorized") is not True:
        raise StudyFailure(f"STAGE1_NOT_AUTHORIZED: {phase} is gated until explicit Planner authorization")
    expected_phase = {"training": "train", "evaluation": "evaluate"}.get(phase, phase)
    if ACTIVE_STAGE1_PHASE != expected_phase:
        raise StudyFailure(
            f"STAGE1_BUDGETED_CLI_REQUIRED: {phase} must run through the locked cumulative-budget launcher"
        )


def run_training(out: Path) -> dict[str, Any]:
    _require_stage1_authorized("training")
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
    if (
        evidence.get("native_source_commit") != EXPECTED_NATIVE
        or evidence.get("passed") is not True
        or evidence.get("required_fixture_gate_passed") is not True
    ):
        raise StudyFailure("required exact-source native forced-fixture checks did not pass")
    target = out / "native-fixture-evidence.json"
    if not target.exists():
        atomic_json(target, evidence)
    return evidence


def _setup(
    out: Path, *, native_module_path: str | Path | None = None,
) -> tuple[dict[str, Any], Any, SemanticHashEncoder, dict[str, Any]]:
    lock_config(out)
    env = validate_environment(out, native_module_path=native_module_path)
    module = env["module"]
    runtime = _install_runtime(out)
    optimizer_path = ROOT / "public_history_execution.py"
    if runtime.get("optimizer_id") != "issue65-demand-driven-public-history-v1":
        raise StudyFailure("expected Issue 65 demand-driven public-history path is not active")
    encoder = SemanticHashEncoder(CONFIG["model"]["hash_dimension"])
    return env, module, encoder, runtime


def run_preflight(out: Path) -> dict[str, Any]:
    _require_stage1_authorized("preflight")
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
    # Keep measured setup as a diagnostic. Cumulative CPU is charged through
    # the Stage 1 resume ledger, not by re-adding the Stage 0 fixed setup debit.
    setup_cpu = max(0.0, time.process_time() - setup_started)
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

    owner_results = [item for item in results if "WORKER" not in str(item.get("run_id", ""))]
    cpu = BASELINE_PARENT_CPU + BASELINE_BRANCH_CPU + time.process_time() + CPU_CHILD_CHARGE
    individual = [float(item["cpu_seconds"]) for item in results if item.get("terminal_complete") and item.get("arm") in {"P0", "E"} and "WORKER" not in str(item.get("run_id", ""))]
    if len(individual) < 16:
        raise StudyFailure("fewer than eight independent P0/E preflight owner arms completed")
    maximum_run = max(individual)
    mean_run = float(np.mean(individual))
    # The cumulative projection charges Stage 0 once, all durable Stage 1
    # receipts across resume, every still-mandatory arm/repeat, the complete
    # remaining branch allowance, margin, and the total reviewer reserve.
    projection = _stage1_budget_projection_for(out, "preflight")
    projected = float(projection["projected_builder_cpu_seconds"])
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
        "cumulative_stage1_cpu_seconds_charged": cpu,
        "projected_all_in_cpu_seconds_builder": projected,
        "reviewer_reserve_seconds": projection["reviewer_reserve_total_seconds"],
        "stage0_reviewer_reserve_credited_seconds": projection["stage0_reviewer_reserve_credited_seconds"],
        "incremental_stage1_reviewer_reserve_seconds": projection["incremental_stage1_reviewer_reserve_seconds"],
        "projected_all_in_with_reviewer_reserve": projection["projected_cumulative_all_in_cpu_seconds"],
        "stage1_budget_projection": projection,
        "within_builder_ceiling": projection["within_builder_ceiling"],
        "projection_factors": dict(PROJECTION),
        "within_all_in_ceiling": projection["within_all_in_ceiling"],
        "training_seed_consumed": False,
    }
    atomic_json(out / "preflight-report.json", report)
    if not projection["within_builder_ceiling"] or not projection["within_all_in_ceiling"]:
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
        "projected_all_in_with_reviewer_reserve": projection["projected_cumulative_all_in_cpu_seconds"],
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
    record = {"study_id": STUDY_ID, "schema_id": "issue71-output-artifact-manifest-v1", "files": entries}
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
        decision_path = out / "decisions" / f"{run_path.stem}.npz"
        if (not artifact or not decision_path.is_file()
                or sha256_file(decision_path) != run.get("decision_artifact_sha256")):
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
    _require_stage1_authorized("evaluation")
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


def _checkpoint_replay_probe(
    *, module: Any, base: Any, root: Mapping[str, Any], model: AllNoncombatPolicy,
    model_digest: str, encoder: SemanticHashEncoder, seed: int,
) -> dict[str, Any]:
    from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions

    candidate_index = _root_alternatives(root)[0]
    sample_seed = derived_seed(
        STUDY_ID, seed, "BRANCH", model_digest,
        f"branch-{int(root['root_index'])}-{candidate_index}",
    )

    def replay_once() -> dict[str, Any]:
        wrapper = CheckpointAdapter(base, root["checkpoint"])
        snapshot = wrapper.reset(seed)
        fingerprints = _snapshot_debug_fingerprints(wrapper, snapshot)
        for field in (
            "native_state_sha256", "public_projection_sha256",
            "public_resource_state_sha256",
        ):
            if fingerprints[field] != root.get(field):
                raise StudyFailure(f"restored {field} differs for {root['screen']} root")

        direct_projection = base.public_projection(snapshot)
        wrapped_projection = wrapper.public_projection(snapshot)
        direct_public_digest = sha256_bytes(canonical(direct_projection).encode("utf-8"))
        wrapped_public_digest = sha256_bytes(canonical(wrapped_projection).encode("utf-8"))
        if direct_public_digest != wrapped_public_digest:
            raise StudyFailure("CheckpointAdapter.public_projection delegation changed the native public projection")

        actions = base.legal_actions(snapshot)
        identities = action_identity_dicts_for_actions(actions)
        if canonical(identities) != canonical(root["legal_action_identities"]):
            raise StudyFailure(f"restored legal action identities differ for {root['screen']} root")
        if not 0 <= candidate_index < len(actions) or candidate_index == int(root["candidate_index"]):
            raise StudyFailure(f"{root['screen']} branch does not select a different live legal action")

        access = module.PrivilegedNoncombatProjectionAccess(
            native_simulator(base), "training_oracle_only"
        )
        projection = access.project_v1()
        validate_live_native_candidate_bindings(projection, actions)
        matrix, audit = encoder.encode(projection, len(actions))
        projection_digest = sha256_bytes(canonical(projection).encode("utf-8"))
        if projection_digest != root.get("projection_digest"):
            raise StudyFailure(f"restored semantic policy projection differs for {root['screen']} root")

        def sample() -> tuple[int, str]:
            generator = torch.Generator(device="cpu")
            generator.manual_seed(sample_seed)
            sampled_index, probabilities, _ = model.choose(matrix, generator)
            return int(sampled_index), sha256_bytes(canonical(probabilities.tolist()).encode("utf-8"))

        sample_a = sample()
        sample_b = sample()
        if sample_a != sample_b:
            raise StudyFailure("fixed model sampling changed across identical checkpoint restores")

        transition = wrapper.step(actions[candidate_index])
        after = _snapshot_debug_fingerprints(base, transition.snapshot)
        after_actions = base.legal_actions(transition.snapshot) if not transition.terminal else []
        after_identities = action_identity_dicts_for_actions(after_actions)
        return {
            "restored_native_state_sha256": fingerprints["native_state_sha256"],
            "restored_public_projection_sha256": wrapped_public_digest,
            "restored_resource_state_sha256": fingerprints["public_resource_state_sha256"],
            "legal_action_identities_sha256": sha256_bytes(canonical(identities).encode("utf-8")),
            "semantic_projection_sha256": projection_digest,
            "policy_sampled_index": sample_a[0],
            "policy_probabilities_sha256": sample_a[1],
            "forced_candidate_index": candidate_index,
            "forced_candidate_identity_sha256": sha256_bytes(
                canonical(identities[candidate_index]).encode("utf-8")
            ),
            "transition_terminal": bool(transition.terminal),
            "transition_info_sha256": sha256_bytes(canonical(transition.info).encode("utf-8")),
            "post_step_native_state_sha256": after["native_state_sha256"],
            "post_step_public_projection_sha256": after["public_projection_sha256"],
            "post_step_resource_state_sha256": after["public_resource_state_sha256"],
            "post_step_legal_action_identities_sha256": sha256_bytes(
                canonical(after_identities).encode("utf-8")
            ),
            "post_step_legal_action_count": len(after_actions),
            "encoder_audit": {
                "screen": audit.get("screen"),
                "projection_schema_id": audit.get("projection_schema_id"),
                "feature_source_counts": audit.get("feature_source_counts"),
                "opaque_bindings_used_for_model_input": False,
            },
        }

    first = replay_once()
    second = replay_once()
    if first != second:
        raise StudyFailure(f"native checkpoint replay diverged for {root['screen']} root")
    return {
        "root_index": int(root["root_index"]),
        "screen": str(root["screen"]),
        "native_checkpoint_id": root.get("native_checkpoint_id"),
        "native_checkpoint_metadata": root.get("native_checkpoint_metadata", {}),
        "root_state_sha256": root.get("native_state_sha256"),
        "root_public_projection_sha256": root.get("public_projection_sha256"),
        "root_public_resource_state_sha256": root.get("public_resource_state_sha256"),
        "candidate_index": candidate_index,
        "repeated_restore_and_step_equal": True,
        "replay": first,
    }


def run_stage0_debug(out: Path, debug_seed: int) -> dict[str, Any]:
    required_screens = [str(item).upper() for item in CONFIG["stage0"]["required_natural_screens"]]
    seed_first = int(CONFIG["stage0"]["debug_seed_first"])
    seed_last = int(CONFIG["stage0"]["debug_seed_last"])
    boundary = "source_provenance"
    base = None
    run_result: Mapping[str, Any] | None = None
    roots: list[Mapping[str, Any]] = []
    replay_evidence: list[dict[str, Any]] = []
    branch_evidence: list[dict[str, Any]] = []
    model_digest: str | None = None
    source_identity: Mapping[str, Any] | None = None
    try:
        if CONFIG.get("stage0", {}).get("stage1_authorized") is not False:
            raise StudyFailure("Stage 0 source lock must explicitly keep Stage 1 unauthorized")
        if not seed_first <= int(debug_seed) <= seed_last:
            raise StudyFailure("debug seed is outside the precommitted nonformal debug-only range")
        if any(_json_paths(out / "runs", "TRAIN_*.json")) or any(_json_paths(out / "runs", "EVAL_*.json")):
            raise StudyFailure("Stage 0 output contains formal training/evaluation owner artifacts")
        boundary = "environment_setup"
        env, module, encoder, runtime = _setup(out)
        source_identity = env["identity"]
        if runtime.get("optimizer_id") != "issue65-demand-driven-public-history-v1":
            raise StudyFailure("pinned Issue 65 public-history runtime is not active")
        init_path = out / "policies" / "pi_init.pt"
        model, model_digest = _load_policy(init_path, initial=True)
        initial_model_sha = _save_policy(model, init_path)
        if _model_digest(model) != model_digest:
            raise StudyFailure("deterministic initialized policy digest changed before the debug run")
        base = native_adapter(module, int(debug_seed))

        boundary = "natural_debug_owner_run"
        label = f"DEBUG_{int(debug_seed)}"
        run_result = _run_one(
            out=out, module=module, model=model, model_digest=model_digest,
            seed=int(debug_seed), arm="P0", encoder=encoder,
            capture_checkpoints=True, checkpoint_screens=required_screens,
            purpose="stage0-nonformal-debug-natural-run", resource_label=label,
            adapter_override=base, capture_trace=False,
        )
        _record_run_if_new(out, run_result)
        if not run_result.get("terminal_complete"):
            raise StudyFailure("nonformal natural debug run did not reach a genuine native terminal")
        roots = list(run_result.get("_volatile_roots", []))
        observed = {_stage0_screen_key(root.get("screen", "")) for root in roots}
        missing = [screen for screen in required_screens if screen not in observed]
        if missing:
            raise StudyFailure(f"natural debug run did not encounter required multichoice screens: {missing}")

        boundary = "checkpoint_restore_and_policy_sampling_probe"
        replay_by_root: dict[int, dict[str, Any]] = {}
        for root in roots:
            probe = _checkpoint_replay_probe(
                module=module, base=base, root=root, model=model,
                model_digest=model_digest, encoder=encoder, seed=int(debug_seed),
            )
            replay_evidence.append(probe)
            replay_by_root[int(root["root_index"])] = probe

        boundary = "natural_terminal_alternative_branches"
        branch_evidence = _run_branches_for_owner(
            out=out, module=module, model=model, model_digest=model_digest,
            encoder=encoder, seed=int(debug_seed), original=run_result,
            roots=roots, adapter=base, phase_label="DEBUG", stop_on_incomplete=True,
        )
        branch_by_screen = {
            _stage0_screen_key(item.get("root_screen", "")): item for item in branch_evidence
        }
        for screen in required_screens:
            attempt = branch_by_screen.get(screen)
            if not isinstance(attempt, Mapping):
                raise StudyFailure(f"no actual alternative branch attempt was recorded for {screen}")
            if attempt.get("terminal_complete") is not True:
                raise StudyFailure(f"{screen} alternative branch failed: {attempt.get('child_failure_class')}")
            if attempt.get("forced_root_choice_valid") is not True:
                raise StudyFailure(f"{screen} alternative was not proven to be a different live legal action")
            probe = replay_by_root.get(int(attempt["root_index"]))
            if not isinstance(probe, Mapping):
                raise StudyFailure(f"{screen} root has no checkpoint replay evidence")
            forced = attempt.get("forced_root_receipt") or {}
            replay = probe["replay"]
            if (
                forced.get("policy_sampled_index") != replay.get("policy_sampled_index")
                or forced.get("probabilities_sha256") != replay.get("policy_probabilities_sha256")
            ):
                raise StudyFailure(f"{screen} branch did not retain the fixed frozen-policy sampling result")
        if _model_digest(model) != model_digest:
            raise StudyFailure("debug branch execution changed the frozen model")
        if any(_json_paths(out / "runs", "TRAIN_*.json")) or any(_json_paths(out / "runs", "EVAL_*.json")):
            raise StudyFailure("formal owner seeds were consumed during Stage 0")

        maximum_run_cpu = float(run_result.get("cpu_seconds", 0.0))
        projection = CONFIG["resources"]["preflight_projection"]
        projected_stage1_owner_arm_cpu = (
            int(projection["future_natural_owner_arm_runs"])
            * maximum_run_cpu
            * float(projection["run_cpu_inflation_factor"])
        )
        stage1_contract = _stage1_budget_contract()
        projected_stage1_preflight_cpu = (
            int(stage1_contract["mandatory_preflight_owner_arm_count"])
            * maximum_run_cpu * float(stage1_contract["run_cpu_inflation_factor"])
        )
        projected_stage1_worker_repeat_cpu = (
            int(stage1_contract["worker_consistency_repeat_run_count"])
            * maximum_run_cpu * float(stage1_contract["run_cpu_inflation_factor"])
        )
        projected_stage1_run_cpu = (
            projected_stage1_owner_arm_cpu
            + projected_stage1_preflight_cpu
            + projected_stage1_worker_repeat_cpu
        )
        projected_stage1_branch_cpu = (
            int(CONFIG["training"]["owner_count"])
            * int(CONFIG["continuations"]["roots_per_owner_max"])
            * float(CONFIG["continuations"]["aggregate_process_cpu_seconds_per_root_max"])
        )
        stage1_reviewer_reserve = int(stage1_contract["reviewer_reserve_min_seconds"])
        stage1_ceiling = int(stage1_contract["cumulative_ceiling_seconds"])
        projected_total = (
            _charged_now(out) + projected_stage1_run_cpu
            + projected_stage1_branch_cpu
            + float(projection["engineering_margin_seconds"])
            + float(stage1_contract["launch_gate_builder_reserve_seconds"])
            + stage1_reviewer_reserve
        )
        phase_metrics = run_result.get("phase_metrics", {})
        report = {
            "study_id": STUDY_ID,
            "status": "STAGE0_ENGINEERING_PASS_PENDING_INDEPENDENT_REVIEW",
            "stage0_scope": "source-provenance-and-terminal-branch-preflight-only",
            "formal_training_seed_consumed": False,
            "formal_evaluation_seed_consumed": False,
            "debug_seed": int(debug_seed),
            "debug_seed_domain": [seed_first, seed_last],
            "source_identity": source_identity,
            "runtime_optimizer": runtime,
            "config_sha256": CONFIG_SHA256,
            "initial_policy_file_sha256": initial_model_sha,
            "initial_policy_model_digest": model_digest,
            "natural_owner_run_id": run_result.get("run_id"),
            "natural_terminal": run_result.get("terminal"),
            "natural_screen_counts": run_result.get("screen_counts"),
            "natural_run_phase_metrics": phase_metrics,
            "required_screens": required_screens,
            "observed_roots": [
                {
                    "root_index": int(root["root_index"]),
                    "screen": str(root["screen"]),
                    "native_checkpoint_id": root.get("native_checkpoint_id"),
                    "native_checkpoint_metadata": root.get("native_checkpoint_metadata", {}),
                    "candidate_count": len(root.get("legal_action_identities", [])),
                    "sampled_candidate_index": int(root["candidate_index"]),
                    "native_state_sha256": root.get("native_state_sha256"),
                    "public_projection_sha256": root.get("public_projection_sha256"),
                    "public_resource_state_sha256": root.get("public_resource_state_sha256"),
                }
                for root in roots
            ],
            "checkpoint_replay_probes": replay_evidence,
            "terminal_branch_witnesses": branch_evidence,
            "native_rng_replay_scope": {
                "source_checkpoint_type": "StepSimulatorCheckpoint copies GameContext and BattleContext plus battleActive",
                "rng_members_copied_with_game_context": [
                    "aiRng", "cardRandomRng", "cardRng", "eventRng", "mathUtilRng",
                    "merchantRng", "miscRng", "monsterHpRng", "monsterRng", "neowRng",
                    "potionRng", "relicRng", "shuffleRng", "treasureRng",
                ],
                "native_rng_bytes_exposed_to_python": False,
                "behavioral_check": "two identical restores, fixed legal action, equal state/resource/projection/transition/legal-action hashes, plus actual complete natural branch suffix",
            },
            "resource_projection": {
                "observed_max_natural_run_cpu_seconds": maximum_run_cpu,
                "projected_stage1_owner_arm_cpu_seconds": projected_stage1_owner_arm_cpu,
                "projected_stage1_mandatory_preflight_owner_arm_count": stage1_contract["mandatory_preflight_owner_arm_count"],
                "projected_stage1_mandatory_preflight_cpu_seconds": projected_stage1_preflight_cpu,
                "projected_stage1_worker_consistency_repeat_run_count": stage1_contract["worker_consistency_repeat_run_count"],
                "projected_stage1_worker_consistency_cpu_seconds": projected_stage1_worker_repeat_cpu,
                "projected_stage1_total_future_run_cpu_seconds": projected_stage1_run_cpu,
                "projected_stage1_branch_cpu_upper_bound_seconds": projected_stage1_branch_cpu,
                "engineering_margin_seconds": float(projection["engineering_margin_seconds"]),
                "launch_gate_repair_cpu_cap_seconds": stage1_contract["launch_gate_repair_cpu_cap_seconds"],
                "launch_gate_builder_repair_reserve_seconds": stage1_contract["launch_gate_builder_reserve_seconds"],
                "launch_gate_reviewer_reserve_credited_seconds": stage1_contract["launch_gate_reviewer_reserve_credit_seconds"],
                "current_builder_cpu_charged_seconds": _charged_now(out),
                "stage0_reviewer_reserve_seconds": REVIEWER_RESERVE,
                "stage1_reviewer_reserve_min_seconds": stage1_reviewer_reserve,
                "incremental_stage1_reviewer_reserve_seconds": stage1_contract["stage1_reviewer_reserve_seconds"],
                "projected_full_study_cpu_seconds_including_reviewer": projected_total,
                "absolute_ceiling_seconds": stage1_ceiling,
                "within_absolute_ceiling": projected_total <= stage1_ceiling,
                "stage1_compute_authorized": False,
            },
            "proposed_disposition": "Builder Stage 0 evidence only; independent Reviewer decision required before any Planner Stage 1 request",
        }
        atomic_json(out / "stage0-report.json", report)
        atomic_json(out / "study-status.json", {
            "status": report["status"], "config_sha256": CONFIG_SHA256,
            "formal_training_seed_consumed": False, "formal_evaluation_seed_consumed": False,
            "stage0_report": "stage0-report.json",
        })
        return report
    except BaseException as exc:
        status = "PRESEED_CONFIG_GATE_FAILED" if boundary in {"source_provenance", "environment_setup"} else "BRANCH_EXECUTOR_UNQUALIFIED"
        report = {
            "study_id": STUDY_ID, "status": status,
            "stage0_scope": "source-provenance-and-terminal-branch-preflight-only",
            "failure_boundary": boundary, "error_type": type(exc).__name__,
            "error": str(exc), "failure_receipt": getattr(exc, "receipt", None),
            "debug_seed": int(debug_seed), "required_screens": required_screens,
            "source_identity": source_identity,
            "natural_owner_run_id": run_result.get("run_id") if isinstance(run_result, Mapping) else None,
            "natural_terminal_complete": bool(run_result.get("terminal_complete")) if isinstance(run_result, Mapping) else False,
            "observed_roots": [
                {
                    "root_index": int(root.get("root_index", -1)),
                    "screen": root.get("screen"),
                    "native_checkpoint_id": root.get("native_checkpoint_id"),
                    "native_state_sha256": root.get("native_state_sha256"),
                    "public_projection_sha256": root.get("public_projection_sha256"),
                }
                for root in roots
            ],
            "checkpoint_replay_probes": replay_evidence,
            "terminal_branch_witnesses": branch_evidence,
            "formal_training_seed_consumed": any(_json_paths(out / "runs", "TRAIN_*.json")),
            "formal_evaluation_seed_consumed": any(_json_paths(out / "runs", "EVAL_*.json")),
            "builder_cpu_charged_seconds": _charged_now(out),
            "proposed_disposition": "Stop Stage 0; no formal seeds; retain exact failure evidence for independent review",
        }
        atomic_json(out / "stage0-report.json", report)
        atomic_json(out / "study-status.json", {
            "status": status, "config_sha256": CONFIG_SHA256,
            "formal_training_seed_consumed": report["formal_training_seed_consumed"],
            "formal_evaluation_seed_consumed": report["formal_evaluation_seed_consumed"],
            "stage0_report": "stage0-report.json",
        })
        return report
    finally:
        if base is not None:
            base.close()


def _validate_artifact_manifest(out: Path) -> bool:
    manifest_path = out / "artifact-manifest.json"
    if not manifest_path.exists():
        return False
    try:
        actual_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected_files = []
    for item in sorted(path for path in out.rglob("*") if path.is_file() and path.name != manifest_path.name):
        expected_files.append({
            "path": str(item.relative_to(out)).replace(os.sep, "/"),
            "bytes": item.stat().st_size,
            "sha256": sha256_file(item),
        })
    expected = {"study_id": STUDY_ID, "schema_id": "issue71-output-artifact-manifest-v1", "files": expected_files}
    return actual_manifest == expected


def _stage1_has_open_phase(out: Path) -> bool:
    started: set[str] = set()
    closed: set[str] = set()
    states = out / "coarse-state.jsonl"
    if not states.exists():
        return False
    for line in states.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        phase_id = str(event.get("phase_id", ""))
        if not phase_id:
            continue
        if event.get("boundary") == "phase_started" and str(event.get("phase", "")) in {"preflight", "train", "evaluate"}:
            started.add(phase_id)
        elif event.get("boundary") in {"phase_finished", "phase_failed"}:
            closed.add(phase_id)
    return bool(started - closed)


def _run_stage1_cli_phase(phase: str, out: Path) -> int:
    """Run one gated Stage 1 phase with a single durable cumulative ledger."""
    global BASELINE_PARENT_CPU, BASELINE_BRANCH_CPU, INITIAL_BRANCH_RESERVE
    global ACTIVE_STAGE1_PHASE, STAGE1_STAGE0_BASELINE_CACHE, STAGE1_ACCOUNTING_SNAPSHOT
    phase_id: str | None = None
    source_identity: dict[str, Any] | None = None
    try:
        expected_out = (ROOT / str(CONFIG["resources"]["stage1_budget"]["artifact_directory"])).resolve()
        if out != expected_out:
            raise StudyFailure(f"Stage 1 output must use the single locked ledger directory: {expected_out}")
        baseline = _stage1_stage0_baseline()
        source_commit, source_dirty = _git_identity(os.fspath(ROOT))
        if source_dirty:
            raise StudyFailure("Stage 1 requires a clean committed study source before phase execution")
        source_identity = {
            "study_source_commit": source_commit,
            "study_source_worktree_clean": True,
            "study_runner_sha256": sha256_file(ROOT / "runner.py"),
            "study_policy_sha256": sha256_file(ROOT / "policy.py"),
            "study_config_sha256": CONFIG_SHA256,
        }
        lock_config(out)
        manifest_path = out / "artifact-manifest.json"
        if manifest_path.exists() and not _validate_artifact_manifest(out) and not _stage1_has_open_phase(out):
            raise StudyFailure("completed Stage 1 artifact manifest is missing or does not match its files")
        branch_accounting = _branch_accounting_from_disk(out)
        BASELINE_PARENT_CPU, BASELINE_BRANCH_CPU = _existing_cpu_charge(
            out, branch_accounting=branch_accounting,
        )
        STAGE1_ACCOUNTING_SNAPSHOT = _load_stage1_accounting_snapshot(
            out, branch_accounting=branch_accounting,
        )
        INITIAL_BRANCH_RESERVE = float(_stage1_budget_contract()["branch_allowance_seconds"])
        ACTIVE_STAGE1_PHASE = phase
        resource_guard = _install_cpu_cap(out)
        atomic_json(out / f"resource-guard-{phase}.json", resource_guard)
        phase_id = f"stage1-{phase}:{time.time_ns()}:{os.getpid()}"
        phase_run_cpu_before = _run_and_replay_cpu_total(out)
        phase_branch_parent_cpu_before = _branch_parent_cpu_total(out)
        append_jsonl(out / "coarse-state.jsonl", {
            "study_id": STUDY_ID, "boundary": "phase_started", "phase": phase,
            "phase_id": phase_id,
            "charged_prior_stage0_builder_cpu_seconds": baseline["builder_cpu_seconds"],
            "charged_prior_stage1_parent_cpu_seconds": BASELINE_PARENT_CPU,
            "charged_prior_stage1_branch_cpu_seconds": BASELINE_BRANCH_CPU,
            "reviewer_reserve_total_seconds": _stage1_budget_contract()["reviewer_reserve_min_seconds"],
            "rlimit_cpu_seconds": resource_guard["rlimit_cpu_seconds"],
            **source_identity,
            "formal_training_seed_consumed": False,
            "formal_evaluation_seed_consumed": False,
        })
        runners = {"preflight": run_preflight, "train": run_training, "evaluate": run_evaluation}
        result = runners[phase](out)
        phase_cpu = _phase_unattributed_cpu(
            out, process_cpu=_phase_process_cpu(),
            run_cpu_before=phase_run_cpu_before,
            branch_parent_cpu_before=phase_branch_parent_cpu_before,
        )
        append_jsonl(out / "coarse-state.jsonl", {
            "study_id": STUDY_ID, "boundary": "phase_finished", "phase": phase,
            "phase_id": phase_id, **phase_cpu, "status": result.get("status"),
        })
        projection = _stage1_budget_projection_for(out, phase)
        result = dict(result)
        result["stage1_budget_projection"] = projection
        result["phase_cpu_accounting"] = phase_cpu
        if "cumulative_stage1_cpu_seconds_charged" in result:
            # run_preflight returns before this wrapper records phase_finished;
            # publish the final cumulative value from the now-closed ledger.
            result["cumulative_stage1_cpu_seconds_charged"] = float(
                projection["stage1_cpu_charged_seconds"]
            )
        if not projection["within_builder_ceiling"] or not projection["within_all_in_ceiling"]:
            result["status"] = "SCIENTIFIC_INCOMPLETE_RESOURCE_PROJECTION"
            result["training_seed_consumed"] = any(_json_paths(out / "runs", "TRAIN_*.json"))
            result["evaluation_seed_consumed"] = any(_json_paths(out / "runs", "EVAL_*.json"))
            atomic_json(out / "study-status.json", {
                "status": result["status"],
                "training_seed_consumed": result["training_seed_consumed"],
                "evaluation_seed_consumed": result["evaluation_seed_consumed"],
                "stage1_budget_projection": projection,
            })
        if phase == "preflight":
            result.update({
                "projected_all_in_cpu_seconds_builder": projection["projected_builder_cpu_seconds"],
                "reviewer_reserve_seconds": projection["reviewer_reserve_total_seconds"],
                "stage0_reviewer_reserve_credited_seconds": projection["stage0_reviewer_reserve_credited_seconds"],
                "incremental_stage1_reviewer_reserve_seconds": projection["incremental_stage1_reviewer_reserve_seconds"],
                "projected_all_in_with_reviewer_reserve": projection["projected_cumulative_all_in_cpu_seconds"],
                "within_builder_ceiling": projection["within_builder_ceiling"],
                "within_all_in_ceiling": projection["within_all_in_ceiling"],
            })
            atomic_json(out / "preflight-report.json", result)
            study_status_path = out / "study-status.json"
            if study_status_path.is_file():
                study_status = json.loads(study_status_path.read_text(encoding="utf-8"))
                if study_status.get("status") == result.get("status"):
                    study_status.update({
                        "preflight_report_sha256": sha256_file(out / "preflight-report.json"),
                        "projected_all_in_cpu_seconds_builder": projection["projected_builder_cpu_seconds"],
                        "projected_all_in_with_reviewer_reserve": projection["projected_cumulative_all_in_cpu_seconds"],
                    })
                    atomic_json(study_status_path, study_status)
        atomic_json(out / f"stage1-{phase}-report.json", result)
        atomic_json(out / "stage1-status.json", {
            "status": result.get("status"), "latest_phase": phase,
            "config_sha256": CONFIG_SHA256,
            "training_seed_consumed": any(_json_paths(out / "runs", "TRAIN_*.json")),
            "evaluation_seed_consumed": any(_json_paths(out / "runs", "EVAL_*.json")),
            "stage1_budget_projection": projection,
        })
        _write_artifact_manifest(out)
        print(canonical(result))
        return 0 if result.get("status") in {
            "PREFLIGHT_COMPLETE", "TRAINING_COMPLETE", "EVALUATION_COMPLETE_PENDING_INDEPENDENT_REVIEW",
        } and projection["within_all_in_ceiling"] else 2
    except Exception as exc:
        failure = {
            "status": "STAGE1_LAUNCH_GATE_FAILED",
            "phase": phase, "error_type": type(exc).__name__, "error": str(exc),
            "config_sha256": CONFIG_SHA256,
            **(source_identity or {}),
            "formal_training_seed_consumed": any(_json_paths(out / "runs", "TRAIN_*.json")) if out.is_dir() else False,
            "formal_evaluation_seed_consumed": any(_json_paths(out / "runs", "EVAL_*.json")) if out.is_dir() else False,
        }
        if phase_id is not None and out.is_relative_to(ROOT):
            phase_cpu = _phase_unattributed_cpu(
                out, process_cpu=_phase_process_cpu(),
                run_cpu_before=locals().get("phase_run_cpu_before", 0.0),
                branch_parent_cpu_before=locals().get("phase_branch_parent_cpu_before", 0.0),
            )
            append_jsonl(out / "coarse-state.jsonl", {
                "study_id": STUDY_ID, "boundary": "phase_failed", "phase": phase,
                "phase_id": phase_id, **phase_cpu, **failure,
            })
            failure["phase_cpu_accounting"] = phase_cpu
            atomic_json(out / f"stage1-{phase}-failure.json", failure)
            atomic_json(out / "study-status.json", failure)
            atomic_json(out / "stage1-status.json", failure)
            _write_artifact_manifest(out)
        print(canonical(failure), file=sys.stderr)
        expected_out = (ROOT / str(CONFIG["resources"]["stage1_budget"]["artifact_directory"])).resolve()
        if phase_id is None and out == expected_out and out.is_dir():
            atomic_json(out / "stage1-launch-failure.json", failure)
            atomic_json(out / "study-status.json", failure)
            atomic_json(out / "stage1-status.json", failure)
            if not (out / "artifact-manifest.json").exists():
                _write_artifact_manifest(out)
        return 2
    finally:
        ACTIVE_STAGE1_PHASE = None
        STAGE1_STAGE0_BASELINE_CACHE = None
        STAGE1_ACCOUNTING_SNAPSHOT = None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("stage0-debug", "preflight", "train", "evaluate"))
    parser.add_argument("--output", required=True, type=Path, help="repo-contained Stage 0 or single Stage 1 artifact directory")
    parser.add_argument(
        "--debug-seed", type=int, default=int(CONFIG["stage0"]["debug_seed_first"]),
        help="precommitted nonformal debug seed; formal owner IDs are rejected",
    )
    args = parser.parse_args(argv)
    out = args.output.expanduser().resolve()
    if args.phase in {"preflight", "train", "evaluate"}:
        if CONFIG.get("stage0", {}).get("stage1_authorized") is not True:
            print(canonical({
                "status": "STAGE1_NOT_AUTHORIZED",
                "phase": args.phase,
                "formal_preflight_seed_consumed": False,
                "formal_training_seed_consumed": False,
                "formal_evaluation_seed_consumed": False,
                "config_sha256": CONFIG_SHA256,
            }), file=sys.stderr)
            return 2
        return _run_stage1_cli_phase(args.phase, out)
    phase_id: str | None = None
    try:
        if out == ROOT or not out.is_relative_to(ROOT):
            raise StudyFailure("Stage 0 output must be contained in the committed study directory")
        lock_config(out)
        prior_report = out / "stage0-report.json"
        if prior_report.exists():
            if not _validate_artifact_manifest(out):
                raise StudyFailure("existing Stage 0 artifact manifest is missing or does not match its files")
            report = json.loads(prior_report.read_text(encoding="utf-8"))
            print(canonical(report))
            return 0 if report.get("status") == "STAGE0_ENGINEERING_PASS_PENDING_INDEPENDENT_REVIEW" else 2

        global BASELINE_PARENT_CPU, BASELINE_BRANCH_CPU, INITIAL_BRANCH_RESERVE
        BASELINE_PARENT_CPU, BASELINE_BRANCH_CPU = _existing_cpu_charge(out)
        INITIAL_BRANCH_RESERVE = BRANCH_CPU_UPPER
        resource_guard = _install_cpu_cap(out)
        atomic_json(out / "resource-guard-stage0-debug.json", resource_guard)
        phase_id = f"stage0-debug:{time.time_ns()}:{os.getpid()}"
        phase_run_cpu_before = _run_and_replay_cpu_total(out)
        phase_branch_parent_cpu_before = _branch_parent_cpu_total(out)
        append_jsonl(out / "coarse-state.jsonl", {
            "study_id": STUDY_ID, "boundary": "phase_started", "phase": "stage0-debug",
            "phase_id": phase_id,
            "charged_prior_parent_cpu_seconds": BASELINE_PARENT_CPU,
            "charged_prior_branch_cpu_seconds": BASELINE_BRANCH_CPU,
            "setup_cpu_seconds": SETUP_CPU,
            "rlimit_cpu_seconds": resource_guard["rlimit_cpu_seconds"],
            "formal_training_seed_consumed": False,
            "formal_evaluation_seed_consumed": False,
        })
        result = run_stage0_debug(out, int(args.debug_seed))
        phase_cpu = _phase_unattributed_cpu(
            out, process_cpu=_phase_process_cpu(),
            run_cpu_before=phase_run_cpu_before,
            branch_parent_cpu_before=phase_branch_parent_cpu_before,
        )
        append_jsonl(out / "coarse-state.jsonl", {
            "study_id": STUDY_ID, "boundary": "phase_finished", "phase": "stage0-debug",
            "phase_id": phase_id, **phase_cpu, "status": result.get("status"),
            "charged_all_in_cpu_seconds": _charged_now(out),
        })
        final_report = dict(result)
        final_report["phase_cpu_accounting"] = phase_cpu
        if isinstance(final_report.get("resource_projection"), dict):
            final_report["resource_projection"]["current_builder_cpu_charged_seconds"] = _charged_now(out)
        atomic_json(out / "stage0-report.json", final_report)
        _write_artifact_manifest(out)
        print(canonical(final_report))
        return 0 if result.get("status") == "STAGE0_ENGINEERING_PASS_PENDING_INDEPENDENT_REVIEW" else 2
    except Exception as exc:
        failure = {
            "status": "PRESEED_CONFIG_GATE_FAILED",
            "phase": "stage0-debug", "error_type": type(exc).__name__, "error": str(exc),
            "config_sha256": CONFIG_SHA256,
            "formal_training_seed_consumed": False, "formal_evaluation_seed_consumed": False,
        }
        if phase_id is not None and out.is_relative_to(ROOT):
            phase_cpu = _phase_unattributed_cpu(
                out, process_cpu=_phase_process_cpu(),
                run_cpu_before=locals().get("phase_run_cpu_before", 0.0),
                branch_parent_cpu_before=locals().get("phase_branch_parent_cpu_before", 0.0),
            )
            append_jsonl(out / "coarse-state.jsonl", {
                "study_id": STUDY_ID, "boundary": "phase_failed", "phase": "stage0-debug",
                "phase_id": phase_id, **phase_cpu, **failure,
            })
            failure["phase_cpu_accounting"] = phase_cpu
            atomic_json(out / "stage0-report.json", failure)
            atomic_json(out / "study-status.json", failure)
            _write_artifact_manifest(out)
        print(canonical(failure), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
