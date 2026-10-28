from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zlib


STUDY = Path(__file__).resolve().parent
REPO = STUDY.parents[1]
STUDY75 = REPO / "studies" / "issue-75-no-expert-oracle"
STAGE1 = STUDY / "stage1"
RETAINED = STUDY / "retained"
ARTIFACTS = STUDY / "artifacts"
SEED_TABLE = STUDY / "seed-table.json"
SOURCE_LOCK = STUDY / "source-lock.json"
PINNED_PYTHON = Path("/home/lsmft/stsrl-spikes/py313-torch/bin/python3.13")
PILOT_TRAINING = tuple(range(7_620_001, 7_620_017))
PILOT_EVALUATION = tuple(range(7_630_001, 7_630_005))
FORMAL_TRAINING = tuple(range(7_600_001, 7_600_385))
FORMAL_EVALUATION = tuple(range(7_610_001, 7_610_097))
ARMS = ("P0", "P1", "R", "J", "E")
CONCURRENCY = 8
ALLOWANCE_FACTOR = 1.75
HEARTBEAT_SECONDS = 2.0


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_once(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError(f"immutable successor evidence differs: {path}")
        return sha256(payload)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise FileExistsError(f"immutable successor evidence differs: {path}")
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return sha256(payload)


def write_json_once(path: Path, value: object) -> str:
    return write_once(path, canonical(value) + b"\n")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def derive_seed(*parts: object) -> int:
    payload = "issue79-successor-v1|" + "|".join(map(str, parts))
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:4], "big") & 0x7FFFFFFF


def _row(owner: int, *, phase: str, arm: str, formal: bool, block: int | None) -> dict:
    roots = 4 if phase == "training" else (7 if arm in {"R", "J"} else 0)
    row = {
        "unit_id": f"TRAIN:{owner}" if phase == "training" else f"EVAL:{owner}:{arm}",
        "phase": phase,
        "arm": arm,
        "formal": formal,
        "block_index": block,
        "owner_seed": owner,
        "game_seed": owner,
        "paired_owner_key": f"{'train' if phase == 'training' else 'eval'}-{owner}",
        "policy_rng_seed": None if arm == "E" else derive_seed("policy", owner, arm),
        "search_invocation_seeds": [
            derive_seed("search", owner, arm, index) for index in range(roots)
        ],
    }
    return row


def make_seed_table() -> dict:
    rows: list[dict] = []
    for index, owner in enumerate(PILOT_TRAINING):
        rows.append(_row(owner, phase="training", arm="TRAIN", formal=False, block=1))
    for owner in PILOT_EVALUATION:
        for arm in ARMS:
            rows.append(_row(owner, phase="heldout", arm=arm, formal=False, block=None))
    for index, owner in enumerate(FORMAL_TRAINING):
        rows.append(_row(
            owner, phase="training", arm="TRAIN", formal=True,
            block=1 + index // 128,
        ))
    for owner in FORMAL_EVALUATION:
        for arm in ARMS:
            rows.append(_row(owner, phase="heldout", arm=arm, formal=True, block=None))
    return {
        "schema_id": "issue79-preregistered-seed-table-v1",
        "study_id": "issue-79-privileged-oracle-successor",
        "semantics": {
            "game_seed_is_owner_seed": True,
            "evaluation_owner_block_is_not_a_common_future_rng_path": True,
            "policy_and_search_streams_are_domain_separated": True,
            "pilot_is_nonformal_and_never_replaces_formal_seeds": True,
            "formal_training_blocks": [1, 2, 3],
            "training_root_opportunities_per_owner": 4,
            "evaluation_root_opportunities_per_R_or_J_owner": 7,
            "fixed_proposals_per_root": 16,
            "training_starts_from_inherited_pi1": True,
        },
        "rows": rows,
    }


def prepare_layout() -> None:
    STAGE1.mkdir(parents=True, exist_ok=True)
    RETAINED.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "pilot").mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "formal").mkdir(parents=True, exist_ok=True)
    seed_table = make_seed_table()
    write_json_once(SEED_TABLE, seed_table)
    source_config = STUDY75 / "stage1" / "config.json"
    write_once(STAGE1 / "config.json", source_config.read_bytes())
    model_source = Path(
        "/home/lsmft/stsrl-data/issue75/update0-00d6405b-UPDATE-0-4db46e9573a4405a"
    )
    for source, destination in (
        (model_source / "models" / "pi1.pt", RETAINED / "pi1.pt"),
        (model_source / "models" / "block-0-step.pt", RETAINED / "block-0-step.pt"),
        (model_source / "retention-manifest.json", RETAINED / "retention-manifest.json"),
    ):
        write_once(destination, source.read_bytes())
    config = read_json(STUDY75 / "config.json")
    source_files = (
        "algorithm.py", "runtime.py", "model.py", "learning.py",
        "stage1/executor.py", "stage1_budget.py",
    )
    repo_relative = STUDY75.relative_to(REPO)
    hashes = {
        relative: file_sha256(STUDY75 / relative)
        for relative in source_files
    }
    lock = {
        "schema_id": "issue79-successor-source-and-inheritance-v1",
        "source_reference": {
            "branch": "codex/issue75-exact-prestart-retry-20261028",
            "commit": "b5a73c0d1155ca7a1681cbca53b17622fe0aaf7a",
            "study_path": repo_relative.as_posix(),
            "files_sha256": hashes,
        },
        "external_runtime": {
            "sts_lightspeed_commit": config["source"]["sts_lightspeed_commit"],
            "stsr_source_commit": config["source"]["stsr_source_commit"],
            "native_module_sha256": config["source"]["native_module_sha256"],
            "native_module_path": config["source"]["native_module_path"],
        },
        "inherited_pi1": {
            "model_sha256": file_sha256(RETAINED / "pi1.pt"),
            "model_digest": "33b3d3d891e53b7434919318ccb1c8411a0bebe1e2e48616ef040206261db557",
            "optimizer_sha256": file_sha256(RETAINED / "block-0-step.pt"),
            "manifest_sha256": file_sha256(RETAINED / "retention-manifest.json"),
        },
        "contract_sha256": file_sha256(STUDY / "contract.json"),
        "seed_table_sha256": file_sha256(SEED_TABLE),
        "model_width_and_input": {
            "architecture": "2048 -> 128 tanh -> 64 tanh -> 1",
            "trainable_parameters": 270593,
            "input_dimension": 2048,
            "inherited_from_pi1_without_change": True,
        },
        "no_per_search_or_per_owner_cpu_hard_kill": True,
        "resource_allowance": "single complete-study cumulative active-program wall clock, derived after pilot",
    }
    write_json_once(SOURCE_LOCK, lock)


def load_modules(active_root: Path, *, pilot: bool):
    for path in (STUDY75 / "stage1", STUDY75):
        sys.path.insert(0, str(path))
    import algorithm
    import runtime
    import model
    import learning
    import executor

    active_root.mkdir(parents=True, exist_ok=True)
    executor.STAGE1 = STAGE1
    executor.ARTIFACT_ROOT = active_root
    executor.ROOT = REPO
    executor.STUDY_ID = "issue-79-privileged-oracle-successor"
    original_model_path = executor._model_path

    def model_path(label: str) -> Path:
        if label == "pi1":
            return RETAINED / "pi1.pt"
        return original_model_path(label)

    executor._model_path = model_path
    original_arm_policy = executor._arm_policy

    def arm_policy(phase: str, arm: str, row: dict):
        if arm == "E":
            return "pi0", False
        if row.get("phase") == "training":
            return f"pi{int(row['block_index'])}", False
        if arm == "P0":
            return "pi1", False
        if arm in {"P1", "R", "J"}:
            return ("pi2" if pilot else "pi4"), False
        return original_arm_policy(phase, arm, row)

    executor._arm_policy = arm_policy
    def save_features(directory: Path, value: dict):
        raw = canonical(value) + b"\n"
        compressed = zlib.compress(raw, level=9)
        path = directory / "training-features.json.zlib"
        write_once(path, compressed)
        return {
            "path": path.resolve().relative_to(active_root.resolve()).as_posix(),
            "sha256": sha256(compressed),
            "uncompressed_sha256": sha256(raw),
            "compression": "zlib",
            "uncompressed_bytes": len(raw),
            "stored_bytes": len(compressed),
        }

    executor._save_training_features = save_features

    class ProposalBudget:
        """Retain the registered proposal count while collecting, never capping, CPU."""
        def __init__(self, *, cpu_cap_seconds: float, started_attempt_cap: int, cpu_reader):
            self.started_attempt_cap = int(started_attempt_cap)
            self.cpu_reader = cpu_reader
            self.start_cpu = float(cpu_reader())
            self.started_attempts = 0
            self.attempt_cpu_seconds: list[float] = []
            self.stop_reason: str | None = None

        @property
        def cpu_seconds(self) -> float:
            return max(0.0, float(self.cpu_reader()) - self.start_cpu)

        def can_start_attempt(self) -> bool:
            if self.started_attempts < self.started_attempt_cap:
                return True
            self.stop_reason = "STARTED_ATTEMPT_CAP"
            return False

        def guard_transition(self, safety_margin_seconds: float = 0.0) -> None:
            return None

        @contextmanager
        def attempt(self):
            from algorithm import SearchBudgetExhausted
            if not self.can_start_attempt():
                raise SearchBudgetExhausted("registered proposal count reached")
            self.started_attempts += 1
            start = float(self.cpu_reader())
            try:
                yield self.started_attempts
            finally:
                self.attempt_cpu_seconds.append(max(0.0, float(self.cpu_reader()) - start))
                if self.started_attempts >= self.started_attempt_cap:
                    self.stop_reason = "STARTED_ATTEMPT_CAP"

    algorithm.InvocationBudget = ProposalBudget
    # Legacy inline arithmetic needs a positive finite sentinel; all budget checks
    # use ProposalBudget and the native runner is explicitly called with None.
    algorithm.SEARCH_CPU_CAP_SECONDS = 1e30
    algorithm.SEARCH_CPU_STOP_MARGIN_SECONDS = 0.0
    original_writer = algorithm.write_immutable_json

    def write_search_receipt(path: Path, value: dict) -> str:
        normalized = dict(value)
        if "registered_cpu_cap_seconds" in normalized:
            normalized.pop("registered_cpu_cap_seconds", None)
            normalized.pop("operational_stop_margin_cpu_seconds", None)
            normalized["per_search_cpu_hard_limit"] = None
        if "search" in normalized and isinstance(normalized["search"], list):
            for receipt in normalized["search"]:
                if isinstance(receipt, dict):
                    receipt.pop("registered_cpu_cap_seconds", None)
                    receipt.pop("operational_stop_margin_cpu_seconds", None)
                    receipt["per_search_cpu_hard_limit"] = None
        if "search" in path.parts:
            target = path.with_name(path.name + ".zlib")
            payload = zlib.compress(canonical(normalized) + b"\n", level=9)
            write_once(target, payload)
            return sha256(payload)
        return original_writer(path, normalized)

    algorithm.write_immutable_json = write_search_receipt
    original_search = algorithm.run_search
    runtime._issue79_search_receipt_active = False

    def run_search(*args, **kwargs):
        runtime._issue79_search_receipt_active = True
        try:
            return original_search(*args, **kwargs)
        finally:
            runtime._issue79_search_receipt_active = False

    algorithm.run_search = run_search
    original_payload = runtime._safe_trial_payload

    def compact_search_payload(trial):
        payload = original_payload(trial)
        if runtime._issue79_search_receipt_active and payload and payload.get("complete"):
            decisions = payload.pop("decisions", [])
            payload["decision_count"] = len(decisions)
            payload["decision_feature_hashes_sha256"] = sha256(
                "\n".join(
                    str(item.get("candidate_features_sha256", ""))
                    for item in decisions
                ).encode("ascii")
            )
        return payload

    runtime._safe_trial_payload = compact_search_payload
    @contextmanager
    def no_cpu_alarm(seconds: float):
        yield

    runtime.process_cpu_alarm = no_cpu_alarm
    original_initialize = runtime.initialize_native_runtime

    def initialize_without_legacy_sampling_gate(
        output_dir: Path, *, sampling: bool, reviewer_proof=None, stage1_binding=None,
    ):
        config, runner, module, encoder = original_initialize(
            output_dir, sampling=False, reviewer_proof=None, stage1_binding=None,
        )
        original_run_one = runner._run_one

        def run_one_without_cpu_limit(*args, **kwargs):
            if "cpu_cap_seconds" in kwargs:
                kwargs["cpu_cap_seconds"] = None
            if "transition_guard" in kwargs:
                kwargs["transition_guard"] = None
            return original_run_one(*args, **kwargs)

        runner._run_one = run_one_without_cpu_limit
        return config, runner, module, encoder

    runtime.initialize_native_runtime = initialize_without_legacy_sampling_gate
    if pilot:
        learning.FORMAL_BLOCK_OWNERS = 16
        learning.FORMAL_MINIBATCH_OWNERS = 16
    return algorithm, runtime, model, learning, executor


def verify_inheritance(active_root: Path | None = None) -> dict:
    active_root = active_root or (ARTIFACTS / "verify")
    algorithm, runtime, model, learning, executor = load_modules(active_root, pilot=False)
    lock = read_json(SOURCE_LOCK)
    if file_sha256(STUDY / "contract.json") != lock["contract_sha256"]:
        raise RuntimeError("successor contract differs from its registered source lock")
    if file_sha256(SEED_TABLE) != lock["seed_table_sha256"]:
        raise RuntimeError("registered seed table differs from its source lock")
    for relative, expected_sha in lock["source_reference"]["files_sha256"].items():
        if file_sha256(STUDY75 / relative) != expected_sha:
            raise RuntimeError(f"reviewed #75 source differs from the successor lock: {relative}")
    if (STAGE1 / "config.json").read_bytes() != (STUDY75 / "stage1" / "config.json").read_bytes():
        raise RuntimeError("copied Stage 1 config differs from its reviewed source bytes")
    config = read_json(STUDY75 / "config.json")
    external = config["source"]
    for key in ("sts_lightspeed_commit", "stsr_source_commit", "native_module_sha256", "native_module_path"):
        if external.get(key) != lock["external_runtime"][key]:
            raise RuntimeError(f"pinned external source identity differs from the source lock: {key}")
    runtime.verify_source_repositories(config)
    native = runtime.verify_native_module(config)
    native_sha = file_sha256(native)
    if native_sha != lock["external_runtime"]["native_module_sha256"]:
        raise RuntimeError("loaded native module bytes differ from the successor source lock")
    loaded, digest, _ = executor._load_model("pi1")
    manifest = read_json(RETAINED / "retention-manifest.json")
    manifest_result = manifest.get("result", {})
    if digest != lock["inherited_pi1"]["model_digest"]:
        raise RuntimeError("inherited pi1 parameter digest differs from the retained scientific artifact")
    for path, key in (
        (RETAINED / "pi1.pt", "model_sha256"),
        (RETAINED / "block-0-step.pt", "optimizer_sha256"),
        (RETAINED / "retention-manifest.json", "manifest_sha256"),
    ):
        if file_sha256(path) != lock["inherited_pi1"][key]:
            raise RuntimeError(f"retained inherited bytes differ from their source lock: {path.name}")
    if manifest_result.get("pi1_state_digest") != digest:
        raise RuntimeError("retention manifest does not bind the inherited pi1 state digest")
    if manifest_result.get("optimizer_parameter_steps") != [64] or manifest_result.get("expert_decisions") != 0:
        raise RuntimeError("retention manifest does not establish the no-Expert 64-step pi1 lineage")
    import torch
    optimizer_state = torch.load(RETAINED / "block-0-step.pt", map_location="cpu", weights_only=True)
    if optimizer_state.get("completed_steps") != 64 or optimizer_state.get("schema_id") != "issue75-policy-block-step-checkpoint-v1":
        raise RuntimeError("retained 64-step optimizer checkpoint is not loadable or has an unexpected schema")
    checkpoint_model = optimizer_state.get("model_state_dict", {})
    loaded_model = loaded.state_dict()
    if checkpoint_model.keys() != loaded_model.keys() or any(
        not torch.equal(checkpoint_model[key], loaded_model[key]) for key in loaded_model
    ):
        raise RuntimeError("retained optimizer checkpoint model bytes do not match inherited pi1")
    if not optimizer_state.get("optimizer_state_dict", {}).get("state"):
        raise RuntimeError("retained optimizer state is empty")
    parameter_count = sum(parameter.numel() for parameter in loaded.parameters())
    if parameter_count != lock["model_width_and_input"]["trainable_parameters"]:
        raise RuntimeError("inherited pi1 parameter count differs from the registered architecture")
    return {
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "numpy": __import__("numpy").__version__,
        "reviewed_source_commit": lock["source_reference"]["commit"],
        "reviewed_source_files_sha256": lock["source_reference"]["files_sha256"],
        "external_source_commits": {
            "sts_lightspeed": external["sts_lightspeed_commit"],
            "stsr_source": external["stsr_source_commit"],
        },
        "native_module_sha256": native_sha,
        "pi1_file_sha256": file_sha256(RETAINED / "pi1.pt"),
        "pi1_model_digest": digest,
        "pi1_optimizer_sha256": file_sha256(RETAINED / "block-0-step.pt"),
        "optimizer_checkpoint_completed_steps": optimizer_state["completed_steps"],
        "optimizer_checkpoint_model_matches_pi1": True,
        "pi1_manifest_sha256": file_sha256(RETAINED / "retention-manifest.json"),
        "retention_manifest_pi1_digest": manifest_result["pi1_state_digest"],
        "retention_optimizer_steps": manifest_result["optimizer_parameter_steps"],
        "retention_no_expert_decisions": manifest_result["expert_decisions"],
        "trainable_parameters": parameter_count,
        "source_lock_sha256": source_lock_sha(),
    }


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class ActiveClock:
    """One cumulative active-program wall clock for the entire formal successor."""
    def __init__(self, mode: str, allowance: float | None):
        self.mode = mode
        self.allowance = allowance
        self.path = ARTIFACTS / f"active-wall-{mode}.json"
        self.lock_path = ARTIFACTS / f"active-wall-{mode}.lock"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.lock_path.open("a+b")
        fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = read_json(self.path) if self.path.exists() else {
            "elapsed_seconds": 0.0, "active_session": None, "process_ids": [],
        }
        if previous.get("active_session"):
            alive = [int(pid) for pid in previous.get("process_ids", []) if _pid_alive(int(pid))]
            if alive:
                raise RuntimeError(f"prior {mode} study program remains active: pids={alive}")
            previous_session = ARTIFACTS / "wall-sessions" / f"{mode}-{previous['active_session']}.json"
            if previous_session.exists():
                interrupted = read_json(previous_session)
                interrupted["status"] = "INTERRUPTED_PROCESS_ENDED"
                interrupted["session_seconds"] = max(
                    0.0,
                    float(previous.get("elapsed_seconds", 0.0))
                    - float(previous.get("session_base_seconds", 0.0)),
                )
                interrupted["interval_end_observation"] = previous.get("heartbeat_utc")
                interrupted["idle_gap_excluded"] = True
                atomic_json(previous_session, interrupted)
        self.elapsed_before = float(previous.get("elapsed_seconds", 0.0))
        self.started = time.monotonic()
        self.session_id = uuid.uuid4().hex
        self.pids: set[int] = {os.getpid()}
        self.stop_event = threading.Event()
        self.guard = threading.RLock()
        self.session_path = ARTIFACTS / "wall-sessions" / f"{mode}-{self.session_id}.json"
        self.session_start_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.persist()
        self.thread = threading.Thread(target=self._heartbeat, name=f"issue79-{mode}-wall", daemon=True)
        self.thread.start()

    def elapsed(self) -> float:
        return self.elapsed_before + max(0.0, time.monotonic() - self.started)

    def expired(self) -> bool:
        return self.allowance is not None and self.elapsed() > self.allowance

    def set_processes(self, pids: set[int]) -> None:
        with self.guard:
            self.pids = {os.getpid(), *pids}
            self.persist()

    def persist(self) -> None:
        with self.guard:
            atomic_json(self.path, {
                "schema_id": "issue79-active-wall-clock-v1",
                "mode": self.mode,
                "elapsed_seconds": self.elapsed(),
                "session_base_seconds": self.elapsed_before,
                "allowance_seconds": self.allowance,
                "active_session": self.session_id,
                "process_ids": sorted(self.pids),
                "heartbeat_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "counting_rule": "program-active intervals only; idle gaps are excluded",
            })
            atomic_json(self.session_path, {
                "schema_id": "issue79-active-wall-session-v1",
                "mode": self.mode,
                "session_id": self.session_id,
                "status": "ACTIVE",
                "session_start_utc": self.session_start_utc,
                "session_seconds": max(0.0, time.monotonic() - self.started),
                "cumulative_seconds": self.elapsed(),
                "allowance_seconds": self.allowance,
                "process_ids": sorted(self.pids),
                "heartbeat_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "idle_gap_excluded": True,
            })

    def _heartbeat(self) -> None:
        while not self.stop_event.wait(HEARTBEAT_SECONDS):
            self.persist()

    def close(self) -> float:
        self.stop_event.set()
        self.thread.join(timeout=HEARTBEAT_SECONDS + 1)
        total = self.elapsed()
        with self.guard:
            self.pids = set()
            atomic_json(self.path, {
                "schema_id": "issue79-active-wall-clock-v1",
                "mode": self.mode,
                "elapsed_seconds": total,
                "session_base_seconds": total,
                "allowance_seconds": self.allowance,
                "active_session": None,
                "process_ids": [],
                "heartbeat_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "counting_rule": "program-active intervals only; idle gaps are excluded",
            })
            atomic_json(self.session_path, {
                "schema_id": "issue79-active-wall-session-v1",
                "mode": self.mode,
                "session_id": self.session_id,
                "status": "COMPLETED",
                "session_start_utc": self.session_start_utc,
                "session_end_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "session_seconds": max(0.0, total - self.elapsed_before),
                "cumulative_seconds": total,
                "allowance_seconds": self.allowance,
                "process_ids": [],
                "idle_gap_excluded": True,
            })
        fcntl.flock(self.lock.fileno(), fcntl.LOCK_UN)
        self.lock.close()
        return total


def _pilot_phase(name: str, clock: ActiveClock, operation, package_commit: str) -> dict:
    receipt_path = ARTIFACTS / f"pilot-phase-{package_commit}-{name}.json"
    progress_path = ARTIFACTS / f"pilot-phase-{package_commit}-{name}-progress.json"
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        if receipt.get("package_commit") != package_commit:
            raise RuntimeError("pilot phase receipt is bound to a different package commit")
        progress = read_json(progress_path) if progress_path.exists() else {
            "schema_id": "issue79-pilot-phase-progress-v1",
            "phase": name, "phase_start_cumulative_seconds": 0.0,
        }
        progress["status"] = "COMPLETE"
        progress["phase_elapsed_active_seconds"] = receipt["elapsed_active_seconds"]
        progress["finished_utc"] = receipt["finished_utc"]
        atomic_json(progress_path, progress)
        return receipt
    if progress_path.exists():
        progress = read_json(progress_path)
        started_at = float(progress["phase_start_cumulative_seconds"])
    else:
        started_at = clock.elapsed()
        progress = {
            "schema_id": "issue79-pilot-phase-progress-v1",
            "phase": name, "status": "ACTIVE",
            "phase_start_cumulative_seconds": started_at,
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        atomic_json(progress_path, progress)
    result = operation()
    if result.get("status") != "SUCCEEDED":
        raise RuntimeError(f"pilot phase {name} did not finish its registered workload")
    receipt = {
        "schema_id": "issue79-pilot-phase-v1",
        "phase": name,
        "package_commit": package_commit,
        "status": "SUCCEEDED",
        "elapsed_active_seconds": clock.elapsed() - started_at,
        "result_summary": {
            key: result[key] for key in ("completed", "row_wall_seconds", "tries", "block_index",
                                         "owner_count", "completed_steps", "result_model_digest",
                                         "native_wall_seconds_by_unit")
            if key in result
        },
        "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_json_once(receipt_path, receipt)
    progress["status"] = "COMPLETE"
    progress["phase_elapsed_active_seconds"] = receipt["elapsed_active_seconds"]
    progress["finished_utc"] = receipt["finished_utc"]
    atomic_json(progress_path, progress)
    return receipt


def source_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def source_lock_sha() -> str:
    return file_sha256(SOURCE_LOCK)


def unit_key(unit_id: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in unit_id)


def existing_success_record(root: Path, row: dict) -> tuple[dict, Path] | None:
    directory = root / "units" / unit_key(row["unit_id"])
    if not directory.exists():
        return None
    for result in sorted(directory.glob("attempt-*/result.json")):
        value = read_json(result)
        if value.get("status") != "SUCCEEDED":
            continue
        if value.get("unit_id") != row["unit_id"] or value.get("owner_seed") != row["owner_seed"]:
            raise RuntimeError(f"success receipt is bound to a different registered owner: {result}")
        if (
            value.get("package_commit") != source_commit()
            or value.get("source_lock_sha256") != source_lock_sha()
            or value.get("phase") != row["phase"]
            or value.get("arm") != row["arm"]
            or bool(value.get("formal")) != bool(row["formal"])
        ):
            continue
        return value, result
    return None


def existing_success(root: Path, row: dict) -> dict | None:
    record = existing_success_record(root, row)
    return None if record is None else record[0]


def worker_main(job_path: Path) -> int:
    job = read_json(job_path)
    root = Path(job["artifact_root"])
    active_root = root
    if Path(sys.executable).resolve() != PINNED_PYTHON.resolve():
        raise RuntimeError(f"worker Python differs from pinned runtime: {sys.executable}")
    alg, runtime, model, learning, executor = load_modules(active_root, pilot=bool(job["pilot"]))
    row = job["row"]
    job["row"] = row
    started = {
        "schema_id": "issue79-owner-attempt-start-v1",
        "unit_id": row["unit_id"], "owner_seed": row["owner_seed"],
        "arm": row["arm"], "run_id": job["run_id"],
        "pid": os.getpid(), "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "started_monotonic": time.monotonic(),
        "package_commit": job["package_commit"],
        "seed_row_sha256": sha256(canonical(row)),
    }
    write_json_once(Path(job["attempt_dir"]) / "started.json", started)
    binding = type("StudyBinding", (), {
        "package_commit": job["package_commit"],
        "source_lock_sha256": job["source_lock_sha256"],
    })()
    ledger = type("StudyContext", (), {"binding": binding})()
    try:
        report = executor._worker_owner(job, ledger)
        for receipt in report.get("search", []):
            receipt.pop("registered_cpu_cap_seconds", None)
            receipt.pop("operational_stop_margin_cpu_seconds", None)
            receipt["per_search_cpu_hard_limit"] = None
        report["resource"]["active_wall_limit_source"] = "one global study clock only"
        result_path = Path(job["result_path"])
        write_json_once(result_path, report)
        write_json_once(Path(job["attempt_dir"]) / "finished.json", {
            "schema_id": "issue79-owner-attempt-finish-v1",
            "unit_id": row["unit_id"], "run_id": job["run_id"],
            "status": "SUCCEEDED",
            "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "finished_monotonic": time.monotonic(),
            "result_sha256": file_sha256(result_path),
        })
        return 0
    except BaseException as exc:
        failure = {
            "schema_id": "issue79-owner-attempt-result-v1",
            "status": "FAILED", "unit_id": row["unit_id"],
            "owner_seed": row["owner_seed"], "arm": row["arm"],
            "run_id": job["run_id"],
            "failure_boundary": type(exc).__name__,
            "failure_detail": str(exc)[:4096],
            "traceback": traceback.format_exc()[-16000:],
            "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        write_json_once(Path(job["result_path"]), failure)
        write_json_once(Path(job["attempt_dir"]) / "finished.json", {
            "schema_id": "issue79-owner-attempt-finish-v1",
            "unit_id": row["unit_id"], "run_id": job["run_id"],
            "status": "FAILED",
            "finished_utc": failure["finished_utc"],
            "finished_monotonic": time.monotonic(),
            "result_sha256": file_sha256(Path(job["result_path"])),
        })
        return 1


def run_units(
    rows: list[dict], *, root: Path, pilot: bool, clock: ActiveClock | None,
) -> dict:
    started = time.monotonic()
    pending = list(rows)
    active: dict[str, tuple[subprocess.Popen, object, dict, float]] = {}
    complete: dict[str, dict] = {}
    durations: dict[str, float] = {}
    tries: dict[str, int] = {}
    python = str(PINNED_PYTHON)
    env = os.environ.copy()
    for name in ("STS_LIGHTSPEED_SOURCE_DIR", "STSRL_SOURCE_DIR"):
        if not env.get(name):
            raise RuntimeError(f"{name} must name the exact pre-existing clean pinned worktree")
    while pending or active:
        if clock is not None and clock.expired():
            for process, log, job, began in active.values():
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            for process, log, job, began in active.values():
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                log.close()
                write_json_once(Path(job["attempt_dir"]) / "global-wall-stop.json", {
                    "schema_id": "issue79-global-wall-stop-v1",
                    "unit_id": job["unit_id"], "run_id": job["run_id"],
                    "allowance_seconds": clock.allowance,
                    "cumulative_active_seconds": clock.elapsed(),
                })
            if clock is not None:
                clock.set_processes(set())
            return {
                "status": "GLOBAL_ACTIVE_WALL_ALLOWANCE_EXCEEDED",
                "elapsed_seconds": time.monotonic() - started,
                "completed": len(complete), "remaining": [row["unit_id"] for row in pending],
            }
        while pending and len(active) < CONCURRENCY:
            row = pending.pop(0)
            done = existing_success(root, row)
            if done is not None:
                complete[row["unit_id"]] = done
                continue
            base = root / "units" / unit_key(row["unit_id"])
            base.mkdir(parents=True, exist_ok=True)
            number = 1 + len(list(base.glob("attempt-*")))
            attempt = base / f"attempt-{number:04d}"
            attempt.mkdir(parents=True, exist_ok=False)
            run_id = f"{row['unit_id']}-attempt-{number:04d}"
            worker_job = {
                "schema_id": "issue79-owner-worker-job-v1",
                "artifact_root": str(root),
                "attempt_dir": str(attempt),
                "result_path": str(attempt / "result.json"),
                "output_dir": str(attempt / "native"),
                "run_id": run_id, "unit_id": row["unit_id"],
                "phase": row["phase"], "row": row, "pilot": pilot,
                "package_commit": source_commit(),
                "source_lock_sha256": source_lock_sha(),
            }
            job_path = attempt / "worker-job.json"
            write_json_once(job_path, worker_job)
            log = (attempt / "worker.log").open("ab")
            process = subprocess.Popen(
                [python, str(Path(__file__).resolve()), "--worker", str(job_path)],
                cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            active[run_id] = (process, log, worker_job, time.monotonic())
            tries[row["unit_id"]] = number
            if clock is not None:
                clock.set_processes({item[0].pid for item in active.values()})
        observed_exit = False
        for run_id, (process, log, job, began) in list(active.items()):
            return_code = process.poll()
            if return_code is None:
                continue
            observed_exit = True
            log.close()
            duration = time.monotonic() - began
            row = job["row"]
            durations[row["unit_id"]] = durations.get(row["unit_id"], 0.0) + duration
            result_path = Path(job["result_path"])
            value = read_json(result_path) if result_path.exists() else None
            del active[run_id]
            if value and value.get("status") == "SUCCEEDED":
                complete[row["unit_id"]] = value
            else:
                # Same registered owner and streams; preserve every failed receipt.
                pending.append(row)
            if clock is not None:
                clock.set_processes({item[0].pid for item in active.values()})
        if not observed_exit:
            time.sleep(0.25)
    row_wall_seconds = {}
    tries = {}
    native_wall = {}
    for row in rows:
        base = root / "units" / unit_key(row["unit_id"])
        durations = []
        if base.exists():
            for finish_path in base.glob("attempt-*/finished.json"):
                finish = read_json(finish_path)
                started_path = finish_path.parent / "started.json"
                if started_path.exists() and finish.get("finished_monotonic") is not None:
                    began = float(read_json(started_path)["started_monotonic"])
                    durations.append(max(0.0, float(finish["finished_monotonic"]) - began))
        row_wall_seconds[row["unit_id"]] = sum(durations)
        tries[row["unit_id"]] = len(list(base.glob("attempt-*"))) if base.exists() else 0
        report = complete.get(row["unit_id"])
        if report is not None:
            native_wall[row["unit_id"]] = float(
                report.get("resource", {}).get("worker_wall_seconds", 0.0)
            )
    return {
        "status": "SUCCEEDED",
        "elapsed_seconds": time.monotonic() - started,
        "completed": len(complete),
        "row_wall_seconds": row_wall_seconds,
        "native_wall_seconds_by_unit": native_wall,
        "tries": tries,
        "results": complete,
    }


def _training_examples(rows: list[dict], *, root: Path, executor) -> list:
    examples = []
    for row in sorted(rows, key=lambda item: int(item["owner_seed"])):
        report = existing_success(root, row)
        if report is None or report.get("phase") != "training" or report.get("arm") != "TRAIN":
            raise RuntimeError(f"training block lacks its registered natural owner: {row['unit_id']}")
        if report.get("expert_decisions") != 0 or report.get("terminal_outcome") is None:
            raise RuntimeError(f"training owner violates the no-Expert/terminal contract: {row['unit_id']}")
        pointer = report.get("training_features")
        if not isinstance(pointer, dict) or pointer.get("compression") != "zlib":
            raise RuntimeError(f"training owner lacks its lossless feature artifact: {row['unit_id']}")
        path = root / pointer["path"]
        compressed = path.read_bytes()
        if sha256(compressed) != pointer["sha256"]:
            raise RuntimeError(f"training feature compressed bytes failed SHA-256: {row['unit_id']}")
        raw = zlib.decompress(compressed)
        if sha256(raw) != pointer["uncompressed_sha256"]:
            raise RuntimeError(f"training feature content failed SHA-256: {row['unit_id']}")
        payload = json.loads(raw)
        example = executor._training_example_from_payload(payload)
        if example.owner_seed != row["owner_seed"] or not example.base.complete:
            raise RuntimeError(f"training features are bound to the wrong owner: {row['unit_id']}")
        examples.append(example)
    return examples


def train_update(block: int, examples: list, *, root: Path, pilot: bool, clock: ActiveClock | None) -> dict:
    algorithm, runtime, model, learning, executor = load_modules(root, pilot=pilot)
    frozen_label = f"pi{block}"
    frozen, source_digest, _ = executor._load_model(frozen_label)
    owner_seed_sha = hashlib.sha256(
        ",".join(str(row.owner_seed) for row in examples).encode("ascii")
    ).hexdigest()
    order_seed = derive_seed("pilot-block-order" if pilot else "formal-block-order", block)
    checkpoint_path = root / "models" / f"block-{block}-step.pt"
    target_path = executor._model_path(f"pi{block + 1}")
    if len(examples) != (16 if pilot else 128):
        raise RuntimeError(f"update {block} owner count differs from its fixed block")
    if block == 1:
        _save_policy_probes(examples, root)
    receipt_path = root / "models" / f"update-{block}.json"
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        saved, digest, _ = executor._load_model(f"pi{block + 1}")
        if receipt.get("result_model_digest") != digest or receipt.get("source_model_digest") != source_digest:
            raise RuntimeError(f"completed update {block} receipt does not match saved policy bytes")
        return receipt
    if target_path.exists() and checkpoint_path.exists():
        import torch
        state = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
        if (
            state.get("completed_steps") == 64
            and state.get("source_model_digest") == source_digest
            and state.get("owner_seed_sha256") == owner_seed_sha
            and state.get("block_order_seed") == order_seed
        ):
            saved, digest, _ = executor._load_model(f"pi{block + 1}")
            diagnostics = state.get("diagnostics", [])
            receipt = {
                "schema_id": "issue79-policy-update-v1", "status": "SUCCEEDED",
                "block_index": block, "owner_count": len(examples),
                "completed_steps": len(diagnostics), "block_order_seed": order_seed,
                "owner_seed_sha256": owner_seed_sha, "source_model_digest": source_digest,
                "result_model_digest": digest, "checkpoint_sha256": file_sha256(checkpoint_path),
                "expert_decisions": 0, "recovered_after_model_write": True,
            }
            write_json_once(receipt_path, receipt)
            return receipt
    resume_state = None
    if checkpoint_path.exists():
        import torch
        resume_state = torch.load(checkpoint_path, map_location="cpu", weights_only=True)

    def save_step(step, trained, optimizer, rng_state, order, diagnostics):
        if clock is not None and clock.expired():
            raise RuntimeError("the one global active-wall allowance was exceeded during policy update")
        executor._atomic_torch_save(checkpoint_path, {
            "schema_id": executor.RESUME_SCHEMA,
            "block_index": block,
            "completed_steps": step,
            "block_order_seed": order_seed,
            "source_model_digest": source_digest,
            "owner_seed_sha256": owner_seed_sha,
            "model_state_dict": trained.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "python_rng_state": rng_state,
            "owner_order": [int(value) for value in order],
            "diagnostics": [vars(value) for value in diagnostics],
        })

    began = time.monotonic()
    result = learning.train_policy_iteration_block(
        frozen, examples, block_order_seed=order_seed,
        step_callback=save_step, resume_state=resume_state,
    )
    record = executor._save_model_once(result.model, target_path, model.model_digest)
    receipt = {
        "schema_id": "issue79-policy-update-v1", "status": "SUCCEEDED",
        "block_index": block, "owner_count": len(examples),
        "completed_steps": len(result.diagnostics), "block_order_seed": order_seed,
        "owner_order_sha256": result.owner_order_sha256,
        "owner_seed_sha256": owner_seed_sha,
        "source_model_digest": result.source_model_digest,
        "result_model_digest": result.result_model_digest,
        "model_checkpoint": record,
        "optimizer_checkpoint": {
            "path": checkpoint_path.relative_to(root).as_posix(),
            "sha256": file_sha256(checkpoint_path),
        },
        "diagnostics": [vars(value) for value in result.diagnostics],
        "elapsed_seconds": time.monotonic() - began,
        "expert_decisions": 0,
        "pilot_nonformal": bool(pilot),
    }
    write_json_once(receipt_path, receipt)
    return receipt


def _save_policy_probes(examples: list, root: Path) -> Path:
    import base64
    import numpy as np
    probes = []
    for example in sorted(examples, key=lambda row: int(row.owner_seed)):
        decision = next(
            (
                item for item in example.base.decisions
                if len(item.legal_identities) > 1 and item.candidate_features is not None
            ),
            None,
        )
        if decision is None:
            continue
        matrix = np.asarray(decision.candidate_features, dtype="<f4")
        raw = matrix.tobytes()
        probes.append({
            "owner_seed": int(example.owner_seed),
            "step_index": int(decision.step_index),
            "state_fingerprint": str(decision.state_fingerprint),
            "shape": list(matrix.shape),
            "sha256": sha256(raw),
            "features_f32le_zlib_base64": base64.b64encode(zlib.compress(raw, level=9)).decode("ascii"),
        })
    path = root / "diagnostics" / "policy-probes.json.zlib"
    write_once(path, zlib.compress(canonical({
        "schema_id": "issue79-training-owner-policy-probes-v1",
        "selection": "first actual multichoice training decision in each block-1 natural owner",
        "outcome_conditioned": False,
        "owners": probes,
    }) + b"\n", level=9))
    return path


def _load_policy_probes(path: Path) -> list:
    import base64
    import numpy as np
    payload = json.loads(zlib.decompress(path.read_bytes()))
    matrices = []
    for row in payload["owners"]:
        raw = zlib.decompress(base64.b64decode(row["features_f32le_zlib_base64"], validate=True))
        shape = tuple(int(value) for value in row["shape"])
        if len(raw) != shape[0] * shape[1] * 4 or sha256(raw) != row["sha256"]:
            raise RuntimeError("policy diagnostic probe feature bytes failed size/hash validation")
        matrix = np.frombuffer(raw, dtype="<f4").reshape(shape).copy()
        if not np.isfinite(matrix).all():
            raise RuntimeError("policy diagnostic probe contains nonfinite features")
        matrices.append(matrix)
    return matrices


def _rows_for(table: dict, *, phase: str, formal: bool, block: int | None = None) -> list[dict]:
    return [
        row for row in table["rows"]
        if row["phase"] == phase and row["formal"] is formal
        and (block is None or row.get("block_index") == block)
    ]


def _max_concurrent_workers(root: Path, rows: list[dict]) -> int:
    events = []
    for row in rows:
        directory = root / "units" / unit_key(row["unit_id"])
        for started_path in directory.glob("attempt-*/started.json"):
            finished_path = started_path.parent / "finished.json"
            if not finished_path.is_file():
                continue
            started = read_json(started_path).get("started_monotonic")
            finished = read_json(finished_path).get("finished_monotonic")
            if not isinstance(started, (int, float)) or not isinstance(finished, (int, float)):
                continue
            if math.isfinite(float(started)) and math.isfinite(float(finished)) and finished >= started:
                events.extend(((float(started), 1), (float(finished), -1)))
    active = maximum = 0
    for _, delta in sorted(events, key=lambda item: (item[0], item[1])):
        active += delta
        maximum = max(maximum, active)
    return maximum


def _attempt_wall_seconds(root: Path) -> list[float]:
    result = []
    for path in root.glob("search/**/*-result.json.zlib"):
        try:
            value = json.loads(zlib.decompress(path.read_bytes()))
        except (OSError, ValueError, zlib.error):
            continue
        trial = value.get("result")
        if isinstance(trial, dict) and trial.get("complete"):
            duration = trial.get("wall_seconds")
            if isinstance(duration, (int, float)) and math.isfinite(float(duration)):
                result.append(float(duration))
    return sorted(result)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(pct * len(ordered)) - 1))
    return ordered[index]


def _distribution(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "mean": None, "median": None, "sd": None, "p95": None, "min": None, "max": None}
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "sd": statistics.stdev(values) if len(values) > 1 else 0.0,
        "p95": _percentile(values, 0.95),
        "min": min(values), "max": max(values),
    }


def _pilot_artifact_paths(package_commit: str, report_generator_commit: str) -> tuple[Path, Path]:
    return (
        ARTIFACTS / f"pilot-report-{package_commit}-{report_generator_commit}.json",
        ARTIFACTS / f"time-policy-{package_commit}-{report_generator_commit}.json",
    )


def pilot(execution_commit: str | None = None) -> dict:
    report_generator_commit = source_commit()
    package_commit = execution_commit or report_generator_commit
    table = read_json(SEED_TABLE)
    root = ARTIFACTS / "pilot" / package_commit
    final_path, policy_path = _pilot_artifact_paths(package_commit, report_generator_commit)
    prior_report_path = ARTIFACTS / f"pilot-report-{package_commit}.json"
    prior_policy_path = ARTIFACTS / f"time-policy-{package_commit}.json"
    if final_path.exists():
        return read_json(final_path)
    if package_commit != report_generator_commit:
        for phase in ("training", "update", "evaluation"):
            receipt_path = ARTIFACTS / f"pilot-phase-{package_commit}-{phase}.json"
            if not receipt_path.is_file():
                raise RuntimeError("cross-commit pilot recovery requires all old-package phase receipts")
            receipt = read_json(receipt_path)
            if receipt.get("package_commit") != package_commit or receipt.get("status") != "SUCCEEDED":
                raise RuntimeError("cross-commit pilot recovery found an incompatible phase receipt")
    clock = ActiveClock(f"pilot-{package_commit[:12]}", None)
    try:
        inheritance = verify_inheritance(root / "verify")
        training_rows = _rows_for(table, phase="training", formal=False)
        training_phase = _pilot_phase("training", clock, lambda: run_units(
            training_rows, root=root, pilot=True, clock=clock,
        ), package_commit)
        training_wall = float(training_phase["elapsed_active_seconds"])
        def pilot_update():
            algorithm, runtime, model, learning, executor = load_modules(root, pilot=True)
            examples = _training_examples(training_rows, root=root, executor=executor)
            update = train_update(1, examples, root=root, pilot=True, clock=clock)
            return {
                "status": "SUCCEEDED", "block_index": 1,
                "owner_count": len(examples), "completed_steps": update["completed_steps"],
                "result_model_digest": update["result_model_digest"],
            }
        update_phase = _pilot_phase("update", clock, pilot_update, package_commit)
        update_wall = float(update_phase["elapsed_active_seconds"])
        eval_rows = _rows_for(table, phase="heldout", formal=False)
        eval_phase = _pilot_phase("evaluation", clock, lambda: run_units(
            eval_rows, root=root, pilot=True, clock=clock,
        ), package_commit)
        eval_wall = float(eval_phase["elapsed_active_seconds"])
        training_reports = [existing_success(root, row) for row in training_rows]
        evaluation_reports = [existing_success(root, row) for row in eval_rows]
        if any(report is None for report in (*training_reports, *evaluation_reports)):
            raise RuntimeError("successful pilot phase receipt lacks an immutable owner result")
        measured = clock.elapsed()
        train_projection = training_wall * (384 / len(training_rows))
        update_projection = update_wall * (32 / 16) * 3
        eval_projection = eval_wall * (480 / len(eval_rows))
        projected = train_projection + update_projection + eval_projection
        if not math.isfinite(projected) or projected <= 0:
            raise RuntimeError("pilot-derived complete-task projection is invalid")
        training_summary = training_phase["result_summary"]
        eval_summary = eval_phase["result_summary"]
        row_times = {
            **training_summary.get("row_wall_seconds", {}),
            **eval_summary.get("row_wall_seconds", {}),
        }
        native_times = {
            **training_summary.get("native_wall_seconds_by_unit", {}),
            **eval_summary.get("native_wall_seconds_by_unit", {}),
        }
        startup_overheads = [
            max(0.0, float(duration) - float(native_times.get(unit_id, 0.0)))
            for unit_id, duration in row_times.items()
        ]
        attempt_times = _attempt_wall_seconds(root)
        training_concurrency = _max_concurrent_workers(root, training_rows)
        evaluation_concurrency = _max_concurrent_workers(root, eval_rows)
        observed_concurrency = max(training_concurrency, evaluation_concurrency)
        if observed_concurrency > CONCURRENCY:
            raise RuntimeError("observed pilot worker concurrency exceeded the configured launch width")
        training_retries = sum(max(0, n - 1) for n in training_summary.get("tries", {}).values())
        evaluation_retries = sum(max(0, n - 1) for n in eval_summary.get("tries", {}).values())
        if policy_path.exists():
            policy = read_json(policy_path)
            projected = float(policy["projection_seconds"])
            measured = float(policy["pilot_active_seconds"])
        else:
            policy = {
                "schema_id": "issue79-single-global-wall-policy-v1",
                "source_branch_sha": package_commit,
                "report_generator_commit": report_generator_commit,
                "source_lock_sha256": source_lock_sha(),
                "seed_table_sha256": file_sha256(SEED_TABLE),
                "supersedes_prior_unversioned_policy": (
                    {"path": prior_policy_path.relative_to(STUDY).as_posix(),
                     "sha256": file_sha256(prior_policy_path)}
                    if prior_policy_path.is_file() else None
                ),
                "projection_seconds": projected,
                "allowance_factor": ALLOWANCE_FACTOR,
                "allowance_seconds": math.ceil(projected * ALLOWANCE_FACTOR),
                "pilot_active_seconds": measured,
                "configured_concurrency": CONCURRENCY,
                "actual_max_concurrent_training_workers": training_concurrency,
                "actual_max_concurrent_evaluation_workers": evaluation_concurrency,
                "actual_max_concurrent_pilot_workers": observed_concurrency,
                "complete_task_projection_components_seconds": {
                    "384_training_owners": train_projection,
                    "three_64_step_policy_updates": update_projection,
                    "96_owners_x_five_heldout_arms": eval_projection,
                },
                "extrapolation": {
                    "training_phase_elapsed_x_384_over_16": training_wall,
                    "update_64_step_elapsed_x_three_with_batch_size_scaling": update_wall,
                    "heldout_phase_elapsed_x_480_over_20": eval_wall,
                    "pilot_training_owner_count": len(training_rows),
                    "pilot_heldout_owner_arm_count": len(eval_rows),
                    "actual_proposal_paths_exercised": True,
                    "training_owner_duration_distribution": _distribution([
                        float(value) for value in training_summary.get("row_wall_seconds", {}).values()
                    ]),
                    "heldout_owner_arm_duration_distribution": _distribution([
                        float(value) for value in eval_summary.get("row_wall_seconds", {}).values()
                    ]),
                    "worker_startup_and_artifact_overhead_distribution": _distribution(startup_overheads),
                    "retry_variance_observed": {
                        "training_retries": training_retries,
                        "evaluation_retries": evaluation_retries,
                        "training_attempts_per_owner": training_summary.get("tries", {}),
                        "evaluation_attempts_per_owner_arm": eval_summary.get("tries", {}),
                        "limitation": "when no retry occurs, retry variance is not estimable; owner/arm duration variation is reported separately",
                    },
                    "pilot_owner_wall_seconds": row_times,
                    "search_attempt_wall_seconds": {
                        "count": len(attempt_times),
                        "distribution": _distribution(attempt_times),
                    },
                },
                "counting_rule": "one cumulative active experiment/training/evaluation program wall clock; idle gaps excluded",
                "per_search_cpu_hard_limit": None,
                "per_owner_cpu_hard_limit": None,
                "scheduler_cpu_reserve": None,
            }
            write_json_once(policy_path, policy)
        if (
            policy.get("source_branch_sha") != package_commit
            or policy.get("source_lock_sha256") != source_lock_sha()
            or policy.get("seed_table_sha256") != file_sha256(SEED_TABLE)
        ):
            raise RuntimeError("pilot time policy is bound to different source, contract, or seeds")
        report = {
            "schema_id": "issue79-representative-pilot-v1",
            "status": "PILOT_COMPLETE_NONFORMAL",
            "source_branch_sha": package_commit,
            "report_generator_commit": report_generator_commit,
            "source_lock_sha256": source_lock_sha(),
            "supersedes_prior_unversioned_report": (
                {"path": prior_report_path.relative_to(STUDY).as_posix(),
                 "sha256": file_sha256(prior_report_path)}
                if prior_report_path.is_file() else None
            ),
            "verified_inheritance": inheritance,
            "configured_concurrency": CONCURRENCY,
            "concurrency_measurement": (
                "maximum overlap of persisted worker-attempt monotonic start/finish intervals; "
                "training and evaluation phase maxima are reported separately because phases are sequential"
            ),
            "actual_max_concurrent_pilot_workers": observed_concurrency,
            "training_run": {
                "elapsed_active_seconds": training_wall,
                "actual_max_concurrent_workers": training_concurrency,
                "registered_debug_owner_count": len(training_rows),
                "rows": training_phase["result_summary"].get("row_wall_seconds", {}),
                "tries": training_phase["result_summary"].get("tries", {}),
            },
            "representative_policy_update": {
                "elapsed_wall_seconds": update_wall,
                "owner_count": update_phase["result_summary"].get("owner_count"),
                "steps": update_phase["result_summary"].get("completed_steps"),
                "debug_policy_digest": update_phase["result_summary"].get("result_model_digest"),
                "formal_checkpoint": False,
            },
            "five_arm_evaluation_paths": {
                "elapsed_active_seconds": eval_wall,
                "actual_max_concurrent_workers": evaluation_concurrency,
                "owner_arm_count": len(eval_rows),
                "rows": eval_phase["result_summary"].get("row_wall_seconds", {}),
                "tries": eval_phase["result_summary"].get("tries", {}),
            },
            "search_coverage": {
                "training": _search_coverage(training_reports),
                "five_arm_evaluation": _search_coverage(evaluation_reports),
                "R_and_J_heldout": _search_coverage([
                    report for report in evaluation_reports if report.get("arm") in {"R", "J"}
                ]),
            },
            "total_pilot_active_seconds": measured,
            "complete_task_projection_seconds": projected,
            "projection_limitations": [
                "one 16-training-owner and 20-heldout-owner-arm debug cohort; owner-count scaling assumes similar throughput at the same worker width",
                "the 64-step update projection scales the 16-owner minibatch measurement linearly to 32 owners and three blocks",
                "the pilot was resumed between phases; cumulative active wall includes executed restart overhead while phase projections use each completed phase duration",
            ],
            "single_global_active_wall_allowance": policy["allowance_seconds"],
            "allowance_factor": ALLOWANCE_FACTOR,
            "time_policy_path": policy_path.relative_to(STUDY).as_posix(),
            "search_proposal_attempts_wall_seconds": policy["extrapolation"]["search_attempt_wall_seconds"],
            "formal_results": False,
            "efficacy_inference": False,
        }
        write_json_once(final_path, report)
        return report
    finally:
        clock.close()


def _paired_bootstrap(rows: list[dict], arm_a: str, arm_b: str, metric: str) -> dict:
    import numpy as np
    grouped: dict[int, dict[str, dict]] = {}
    for row in rows:
        grouped.setdefault(int(row["owner_seed"]), {})[str(row["arm"])] = row
    pairs = [
        (float(arms[arm_a][metric]), float(arms[arm_b][metric]))
        for arms in grouped.values()
        if arm_a in arms and arm_b in arms
    ]
    if len(pairs) != len(FORMAL_EVALUATION):
        return {"paired_owner_count": len(pairs), "mean_difference": None, "ci95": None}
    differences = np.asarray([a - b for a, b in pairs], dtype=np.float64)
    rng = np.random.default_rng(derive_seed("bootstrap", arm_a, arm_b, metric))
    indices = rng.integers(0, len(differences), size=(20_000, len(differences)))
    means = differences[indices].mean(axis=1)
    return {
        "paired_owner_count": len(pairs),
        "mean_difference": float(differences.mean()),
        "ci95": [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))],
        "bootstrap_replicates": 20_000,
        "cluster_unit": "registered evaluation owner seed",
    }


def _search_coverage(reports: list[dict]) -> dict:
    roots = [
        receipt
        for report in reports
        for receipt in report.get("search", [])
    ]
    proposal_cpu = [
        float(receipt.get("cpu_seconds", 0.0))
        for receipt in roots
        if isinstance(receipt.get("cpu_seconds"), (int, float))
    ]
    return {
        "owner_arm_count": len(reports),
        "root_opportunities": len(roots),
        "started_proposals": sum(int(row.get("started_attempts", 0)) for row in roots),
        "completed_terminal_proposals": sum(int(row.get("completed_trajectories", 0)) for row in roots),
        "later_mutations_started": sum(int(row.get("mutation_attempts_started", 0)) for row in roots),
        "roots_with_reachable_mutation": sum(int(row.get("mutation_attempts_started", 0)) > 0 for row in roots),
        "roots_without_reachable_mutation": sum(int(row.get("mutation_attempts_started", 0)) == 0 for row in roots),
        "first_action_switches": sum(row.get("root_action_changed_from_sampled_policy") is True for row in roots),
        "first_action_non_switches": sum(row.get("root_action_changed_from_sampled_policy") is False for row in roots),
        "total_search_process_tree_cpu_seconds": sum(proposal_cpu),
        "search_root_cpu_seconds_distribution": _distribution(proposal_cpu),
        "whole_owner_process_tree_cpu_seconds": sum(
            float(row.get("resource", {}).get("worker_process_cpu_seconds", 0.0))
            for row in reports
        ),
        "whole_owner_wall_seconds": sum(
            float(row.get("resource", {}).get("worker_wall_seconds", 0.0))
            for row in reports
        ),
    }


def analyze(root: Path) -> dict:
    outcomes = []
    eval_reports = []
    for owner in FORMAL_EVALUATION:
        for arm in ARMS:
            row = _row(owner, phase="heldout", arm=arm, formal=True, block=None)
            record = existing_success_record(root, row)
            if record is None:
                continue
            report, result_path = record
            eval_reports.append(report)
            resource = report.get("resource", {})
            terminal = {
                "owner_seed": owner, "arm": arm,
                "terminal_floor": int(report["terminal_floor"]),
                "act4_reached": bool(report["act4_reached"]),
                "heart_victory": bool(report["heart_victory"]),
                "worker_process_cpu_seconds": float(resource.get("worker_process_cpu_seconds", 0.0)),
                "worker_wall_seconds": float(resource.get("worker_wall_seconds", 0.0)),
                "result_path": result_path.relative_to(root).as_posix(),
                "result_sha256": file_sha256(result_path),
            }
            outcomes.append(terminal)
    by_arm = {}
    for arm in ARMS:
        values = [row for row in outcomes if row["arm"] == arm]
        cpu = [row["worker_process_cpu_seconds"] for row in values]
        wall = [row["worker_wall_seconds"] for row in values]
        by_arm[arm] = {
            "owners": len(values),
            "mean_terminal_floor": None if not values else sum(row["terminal_floor"] for row in values) / len(values),
            "act4_rate": None if not values else sum(row["act4_reached"] for row in values) / len(values),
            "heart_rate": None if not values else sum(row["heart_victory"] for row in values) / len(values),
            "resource": {
                "worker_process_cpu_seconds_total": sum(cpu),
                "worker_process_cpu_seconds_per_owner_arm": _distribution(cpu),
                "worker_wall_seconds_summed_across_owner_arms": sum(wall),
                "worker_wall_seconds_per_owner_arm": _distribution(wall),
            },
        }
    report = {
        "schema_id": "issue79-complete-game-analysis-v1",
        "status": "COMPLETE" if len(outcomes) == 480 else "INCOMPLETE",
        "owner_arm_count": len(outcomes),
        "registered_owner_arm_count": 480,
        "per_arm": by_arm,
        "owner_arm_outcomes": outcomes,
        "search_coverage": {
            "training_J": _search_coverage([
                value
                for index, owner in enumerate(FORMAL_TRAINING)
                if (value := existing_success(
                    root, _row(
                        owner, phase="training", arm="TRAIN", formal=True,
                        block=1 + index // 128,
                    ),
                )) is not None
            ]),
            "heldout_R": _search_coverage([value for value in eval_reports if value.get("arm") == "R"]),
            "heldout_J": _search_coverage([value for value in eval_reports if value.get("arm") == "J"]),
        },
        "contrasts": {},
        "limitations": [
            "privileged native-surrogate controller only",
            "R and J are proposal-count matched, not equal-compute",
            "P0 is pretrained inherited pi1, not random pi0",
            "a missing arm or owner is incomplete, not an efficacy conclusion",
        ],
    }
    metrics = ("terminal_floor", "act4_reached", "heart_victory")
    for left, right, label in (
        ("J", "P1", "J-P1"),
        ("J", "E", "J-E"),
        ("P1", "P0", "P1-P0"),
        ("J", "R", "J-R"),
    ):
        report["contrasts"][label] = {
            metric: _paired_bootstrap(outcomes, left, right, metric)
            for metric in metrics
        }
    if len(outcomes) == 480:
        algorithm, runtime, model, learning, executor = load_modules(root, pilot=False)
        pi1, _, _ = executor._load_model("pi1")
        pi4, _, _ = executor._load_model("pi4")
        probes_path = root / "diagnostics" / "policy-probes.json.zlib"
        matrices = _load_policy_probes(probes_path)
        report["pi1_to_pi4_training_feature_response"] = {
            "source": "block-1 training owners only; first preselected natural multichoice state",
            "probe_count": len(matrices),
            "feature_artifact_sha256": file_sha256(probes_path),
            "diagnostics": learning.policy_probe_diagnostics(pi1, pi4, matrices),
        }
    return report


def formal(
    pilot_execution_commit: str | None = None,
    pilot_report_generator_commit: str | None = None,
) -> dict:
    package_commit = source_commit()
    pilot_commit = pilot_execution_commit or package_commit
    report_generator_commit = pilot_report_generator_commit or package_commit
    final_path = ARTIFACTS / f"formal-analysis-{package_commit}.json"
    if final_path.exists():
        return read_json(final_path)
    pilot_report_path, policy_path = _pilot_artifact_paths(pilot_commit, report_generator_commit)
    if not policy_path.is_file():
        raise RuntimeError("formal study requires the selected pilot's matching time policy")
    policy = read_json(policy_path)
    if not pilot_report_path.is_file():
        raise RuntimeError("formal study requires the completed report for its selected pilot package")
    pilot_report = read_json(pilot_report_path)
    if (
        pilot_report.get("source_branch_sha") != pilot_commit
        or pilot_report.get("report_generator_commit") != policy.get("report_generator_commit")
        or policy.get("source_branch_sha") != pilot_commit
        or policy.get("source_lock_sha256") != source_lock_sha()
        or policy.get("seed_table_sha256") != file_sha256(SEED_TABLE)
    ):
        raise RuntimeError("formal time policy and completed pilot report have incompatible provenance")
    allowance = float(policy["allowance_seconds"])
    clock = ActiveClock(f"formal-{package_commit[:12]}", allowance)
    root = ARTIFACTS / "formal" / package_commit
    progress_path = root / "progress.json"
    root.mkdir(parents=True, exist_ok=True)
    table = read_json(SEED_TABLE)
    try:
        verify_inheritance(root / "verify")
        for block in (1, 2, 3):
            rows = _rows_for(table, phase="training", formal=True, block=block)
            phase = run_units(rows, root=root, pilot=False, clock=clock)
            if phase["status"] != "SUCCEEDED":
                return {
                    "schema_id": "issue79-formal-progress-v1",
                    "status": phase["status"], "block_index": block,
                    "active_seconds": clock.elapsed(), "allowance_seconds": allowance,
                }
            algorithm, runtime, model, learning, executor = load_modules(root, pilot=False)
            examples = _training_examples(rows, root=root, executor=executor)
            update = train_update(block, examples, root=root, pilot=False, clock=clock)
            atomic_json(progress_path, {
                "schema_id": "issue79-formal-progress-v1",
                "status": "BLOCK_COMPLETE", "block_index": block,
                "owner_count": len(examples), "completed_steps": update["completed_steps"],
                "result_model_digest": update["result_model_digest"],
                "active_seconds": clock.elapsed(), "allowance_seconds": allowance,
            })
        eval_rows = _rows_for(table, phase="heldout", formal=True)
        evaluation = run_units(eval_rows, root=root, pilot=False, clock=clock)
        if evaluation["status"] != "SUCCEEDED":
            return {
                "schema_id": "issue79-formal-progress-v1",
                "status": evaluation["status"], "phase": "heldout",
                "active_seconds": clock.elapsed(), "allowance_seconds": allowance,
            }
        if clock.expired():
            return {
                "schema_id": "issue79-formal-progress-v1",
                "status": "GLOBAL_ACTIVE_WALL_ALLOWANCE_EXCEEDED",
                "active_seconds": clock.elapsed(), "allowance_seconds": allowance,
            }
        result = analyze(root)
        result["source_branch_sha"] = package_commit
        result["source_lock_sha256"] = source_lock_sha()
        result["pilot_execution_commit"] = pilot_commit
        result["pilot_report_sha256"] = file_sha256(pilot_report_path)
        result["time_policy_sha256"] = file_sha256(policy_path)
        result["active_wall_seconds"] = clock.elapsed()
        result["active_wall_allowance_seconds"] = allowance
        write_json_once(final_path, result)
        return result
    finally:
        clock.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("prepare", "verify", "pilot", "formal"))
    parser.add_argument("--pilot-execution-commit", help="select a completed pilot with matching phase receipts")
    parser.add_argument("--pilot-report-generator-commit", help="select the immutable report revision for the pilot")
    parser.add_argument("--worker", type=Path)
    args = parser.parse_args()
    if args.worker is not None:
        return worker_main(args.worker)
    if args.mode == "prepare":
        prepare_layout()
        print(json.dumps({
            "source_lock_sha256": source_lock_sha(),
            "seed_table_sha256": file_sha256(SEED_TABLE),
            "seed_rows": len(read_json(SEED_TABLE)["rows"]),
            "inherited_pi1_sha256": file_sha256(RETAINED / "pi1.pt"),
            "inherited_optimizer_sha256": file_sha256(RETAINED / "block-0-step.pt"),
        }, sort_keys=True))
        return 0
    if args.mode == "verify":
        print(json.dumps(verify_inheritance(), sort_keys=True))
        return 0
    if args.mode == "pilot":
        print(json.dumps(pilot(args.pilot_execution_commit), sort_keys=True))
        return 0
    if args.mode == "formal":
        result = formal(args.pilot_execution_commit, args.pilot_report_generator_commit)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("status") in {"COMPLETE", "BLOCK_COMPLETE"} else 2
    parser.error("select a mode or a worker job")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
