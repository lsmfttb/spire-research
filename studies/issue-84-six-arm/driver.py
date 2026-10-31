"""Source-locked runner for the registered Issue 84 six-arm study.

Game mechanics, Search-v2, legal-action projection, and native execution remain
owned by the pinned sts_lightspeed/STSRL runtime. This study supplies only the
registered policy mapping, loss, predictor fitting, and durable orchestration.
"""
from __future__ import annotations

import argparse
import ast
import copy
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


REPO = Path(__file__).resolve().parents[2]
STUDY = REPO / "studies" / "issue-84-six-arm"
UPSTREAM = STUDY / "upstream"
UPSTREAM75 = UPSTREAM / "issue-75-no-expert-oracle"
BACKEND_PATH = UPSTREAM / "issue-79-successor" / "driver.py"
CONTRACT_PATH = STUDY / "contract.json"
SOURCE_LOCK_PATH = STUDY / "source-lock.json"
UPSTREAM_MANIFEST_PATH = UPSTREAM / "upstream-manifest.json"

DATA_ROOT_WSL = "/mnt/d/DeadlycatCoding/stsrl-data/issue84"
SOURCE_REPO_WSL = "/mnt/d/DeadlycatCoding/spire-research"
ISSUE73_SOURCE_STUDY = "studies/issue-73-privileged-noncombat-oracle-stage0"
PARENT_ROOT_WSL = (
    "/mnt/d/DeadlycatCoding/stsrl-data/issue82/artifacts/formal/"
    "8f88f1554821707db6228d2de384f2334ec2615b"
)
PARENT_MODELS_WSL = f"{PARENT_ROOT_WSL}/models"
STS_SOURCE_WSL = "/mnt/d/DeadlycatCoding/stsrl-data/issue81/sources/sts_lightspeed"
STSRL_SOURCE_WSL = "/mnt/d/DeadlycatCoding/stsrl-data/issue81/sources/STSRL"
PYTHON_WSL = "/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/bin/python3.13"
NATIVE_WSL = (
    "/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/build-issue71-8ded-py313/"
    "slaythespire.cpython-313-x86_64-linux-gnu.so"
)

NATIVE_COMMIT = "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2"
STSRL_COMMIT = "7d172f7c24c9d29429b6946ba44f6b59760b0d96"
NATIVE_SHA256 = "bb22332a4a7108e8f9691482df2624b3b132d75ca07e1aa7b394b932193e5cad"
PYTHON_SHA256 = "bd6137c8855ac3b679cde2b7b4620518f2a017bbb5a15d7c3b3b5a23ea30c18a"
PARENT_EXECUTION_COMMIT = "8f88f1554821707db6228d2de384f2334ec2615b"
PARENT_MODEL_SHA256 = "6f72fdd60d684e4667f2357ba578f84f6e087a18d4d3944c65bf1e0aabd0cf19"
PARENT_MODEL_DIGEST = "7c530520b5e107702575f9c8e612bff70e345b5c73715c33aa16eb75e00653ee"
PARENT_OPTIMIZER_SHA256 = "96a5599f389be0a90adfab2a245b88359a166b5b859101688200a492be893993"
PARENT_UPDATE_SHA256 = "60fc4ebbefb5ee88cd4dbe25407567e8fd38b7e38bf8e10d1fa3287a0bab9441"
UPSTREAM_COMMIT = "ecb588e580c0226da3aabaf54d79f2c2267308e3"

ARMS = ("A", "B", "C", "D", "E", "F")
CONCURRENCY = 8
T_REF = 36.6474609375
S_REF = 5.360108706240238

# The first authorized pilot attempt completed arm A's 32 training games under
# this exact source package. A later code repair may carry those immutable games
# forward only while the episode-execution adapters and all external identities
# remain byte/contract compatible.
PILOT_PREDECESSOR_PACKAGE = "fbbcae6d55a505ce4c4a12968c7a09abb2f06f3d"
PILOT_PREDECESSOR_LOCK_SHA256 = "d23bda9d60572814fbeef8d245246d1cccda74cf4ba565a87d0f1e98f7c49533"
PILOT_ORIGINAL_RUNTIME_AST_SHA256 = "47b9dda9e86843c760dffe9b3a548a6338cbda7fc095420e965282a24571e35a"
PILOT_PREDECESSOR_RUNTIME_AST_SHA256 = "f25cbe4d154392497f242bce54feec336896b4f6007a1569b9c33394c7fca155"
PILOT_CANDIDATE_COVERAGE_OVERLAY_PATH = "issue-79-successor/driver.py"
PILOT_CANDIDATE_COVERAGE_BASE_SHA256 = "d8e3f2540cd18c05f0545be713addd3b72469dd71de4bcca72cdc9d50e8966bb"
PILOT_CARRY_FORWARD = {
    "A": {
        "package": PILOT_PREDECESSOR_PACKAGE,
        "source_lock_sha256": PILOT_PREDECESSOR_LOCK_SHA256,
        "runtime_ast_sha256": PILOT_ORIGINAL_RUNTIME_AST_SHA256,
    },
    "B": {
        "package": "1179b14dbed830ce915299e5ee63ab190d16e4a6",
        "source_lock_sha256": "625b812e9e174c1cd52381820beae6ea6e402daeedc3180a3329b375878d93e4",
        "runtime_ast_sha256": PILOT_ORIGINAL_RUNTIME_AST_SHA256,
    },
}
PILOT_ACTOR_UPDATE_CARRY_FORWARD = {
    "A": {
        "package": "1179b14dbed830ce915299e5ee63ab190d16e4a6",
        "source_lock_sha256": "625b812e9e174c1cd52381820beae6ea6e402daeedc3180a3329b375878d93e4",
        "training_package": PILOT_PREDECESSOR_PACKAGE,
        "training_source_lock_sha256": PILOT_PREDECESSOR_LOCK_SHA256,
        "progress_sha256": "ea75cbc5725829e30ebdd10654fe40442b9e3955ba657d94fcf2870399574d92",
        "receipt_sha256": "b97334afc6fdacc2ebd40770ecb061372adf620b8c7f6782515b0c7eceacb0c9",
        "model_sha256": "5604149f55ceeb648242d54b9f407414bf9e40897a11283b88846433fa33e585",
        "checkpoint_sha256": "11612f6c6be7869c00bd9abb57113c7863af2301e8433ab2edabb989b62677dc",
        "model_digest": "0f5069385b35ca50a7376500f6f39a53a2beada1db24725765dedcb97e41cfda",
        "owner_seed_sha256": "c531521816644d45ee26d2fe52ef6ddd17f166cbb52c9852c3def7a9e3ecf9c6",
        "owner_training_feature_manifest_sha256": "63d069af7202fee5af1599229916e352ec021b863d6493b0cfdc87381d48a339",
        "decision_count": 1220,
    },
    "B": {
        "package": "1179b14dbed830ce915299e5ee63ab190d16e4a6",
        "source_lock_sha256": "625b812e9e174c1cd52381820beae6ea6e402daeedc3180a3329b375878d93e4",
        "training_package": "1179b14dbed830ce915299e5ee63ab190d16e4a6",
        "training_source_lock_sha256": "625b812e9e174c1cd52381820beae6ea6e402daeedc3180a3329b375878d93e4",
        "progress_sha256": "ea75cbc5725829e30ebdd10654fe40442b9e3955ba657d94fcf2870399574d92",
        "receipt_sha256": "5b7d4891aaa10b608fd4db511280e7e165c77d40b482e2032c892e61072afaec",
        "model_sha256": "1b08b409ef15fe75015b2ffbcbdfd38b5fcfe802523ab6ee81dea9e56702168f",
        "checkpoint_sha256": "d39245ec28af767e42beac5f46464ca30d3a4641c13c9220fbb0cb251c3527c7",
        "model_digest": "a05253324eac7b566c7704fd258b40a86945232057704b94352ea3cb7d197b75",
        "owner_seed_sha256": "c531521816644d45ee26d2fe52ef6ddd17f166cbb52c9852c3def7a9e3ecf9c6",
        "owner_training_feature_manifest_sha256": "7127b4dd127346e29fa26248ea3597dcf5cbfdcee86acfd7db3d2456b0de5dcf",
        "decision_count": 1155,
    },
}
PILOT_C_INTERRUPTED_ATTEMPT_PACKAGE = "1179b14dbed830ce915299e5ee63ab190d16e4a6"
PILOT_C_INTERRUPTED_ATTEMPT_LOCK_SHA256 = "625b812e9e174c1cd52381820beae6ea6e402daeedc3180a3329b375878d93e4"
PILOT_C_INTERRUPTED_ATTEMPT_ROW_SHA256 = "7449f89ab7df2f7277680901627544fd82cda5286b1eec79c3771c8fec611b72"
PILOT_C_INTERRUPTED_ATTEMPT_COUNT = 19
PILOT_C_COVERAGE_FAILURE_DETAIL = (
    "TRAIN_8480022 is incomplete or invalid: "
    "['native projection does not support every live legal candidate']"
)

sys.path.insert(0, str(STUDY))

from candidate_coverage import CoverageRepairingEncoder


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class _RootCaptureLimit:
    """Expose the pinned #73 first-N checkpoint behavior expected by #75."""

    def __init__(self, roots: list, limit: int):
        if type(limit) is not int or limit <= 0:
            raise ValueError("capture_root_limit must be a positive integer")
        self.roots = roots
        self.limit = limit

    def __bool__(self) -> bool:
        return len(self.roots) < self.limit


def call_pinned_issue73_run_one(runner, original_run_one, *args, **kwargs):
    """Adapt #75's root cap, policy RNG, and volatile training features.

    The #73 controller captures its first two roots by default. #75's stage-1
    executor explicitly requests the first four training roots. Installing a
    per-call gate preserves that first-N capture behavior without changing the
    pinned runner source or capturing every later decision state. The pinned
    #73 runner retains candidate matrices in its controller trace but omits
    them from its returned summary when durable=False; #75 consumes those
    matrices through its volatile training-decision interface.
    """
    root_limit = kwargs.pop("capture_root_limit", None)
    training_features = kwargs.pop("capture_training_features", None)
    policy_rng_seed = kwargs.pop("policy_rng_seed", None)
    if training_features is not None and type(training_features) is not bool:
        raise ValueError("capture_training_features must be boolean when supplied")
    if training_features is not None and bool(training_features) != bool(kwargs.get("capture_trace", True)):
        raise ValueError("pinned #73 trace capture is the source of #75 training features")
    if root_limit is None and policy_rng_seed is None and training_features is not True:
        return original_run_one(*args, **kwargs)
    if root_limit is not None:
        if type(root_limit) is not int or root_limit <= 0:
            raise ValueError("capture_root_limit must be a positive integer")
        if (
            not kwargs.get("capture_checkpoints")
            or kwargs.get("capture_all_roots", False)
            or kwargs.get("checkpoint_screens")
            or not callable(kwargs.get("root_callback"))
        ):
            raise ValueError("capture_root_limit requires checkpoint capture with a callback and no other root filter")
    if policy_rng_seed is not None and (type(policy_rng_seed) is not int or policy_rng_seed < 0):
        raise ValueError("policy_rng_seed must be a nonnegative integer")

    controller_class = runner.StudyController
    original_init = controller_class.__init__
    owner_pending = True
    owner_controllers = []

    def initialize_with_root_limit(controller, *init_args, **init_kwargs):
        nonlocal owner_pending
        original_init(controller, *init_args, **init_kwargs)
        # _run_one may invoke nested root-search trajectories through the same
        # runner. Apply this call's options to its own controller only; nested
        # calls install their own wrapper and policy RNG context.
        if not owner_pending:
            return
        owner_pending = False
        owner_controllers.append(controller)
        if root_limit is not None:
            controller.capture_all_roots = _RootCaptureLimit(controller.roots, root_limit)
        if policy_rng_seed is not None:
            controller.rng.manual_seed(policy_rng_seed)

    controller_class.__init__ = initialize_with_root_limit
    try:
        result = original_run_one(*args, **kwargs)
        if training_features:
            if len(owner_controllers) != 1:
                raise RuntimeError("pinned #73 did not initialize exactly one owner controller")
            trace = getattr(owner_controllers[0], "trace", None)
            if not isinstance(trace, list):
                raise RuntimeError("pinned #73 owner controller has no in-memory decision trace")
            if not isinstance(result, dict):
                raise TypeError("pinned #73 run result must be a dictionary")
            decisions = []
            for row in trace:
                if row.get("kind") != "categorical":
                    continue
                if "matrix" not in row:
                    raise RuntimeError("captured categorical training decision lacks candidate features")
                decisions.append({
                    **{key: value for key, value in row.items() if key != "matrix"},
                    "candidate_features": row["matrix"],
                })
            result["_volatile_training_decisions"] = decisions
        return result
    finally:
        controller_class.__init__ = original_init


def install_issue73_capture_limit_compat(runtime) -> None:
    """Bridge the #75 root-limit keyword at the pinned #73 runtime boundary."""
    if getattr(runtime, "_issue84_capture_limit_compat_installed", False):
        return
    original_initialize = runtime.initialize_native_runtime

    def initialize_with_capture_limit_compat(*args, **kwargs):
        config, runner, module, encoder = original_initialize(*args, **kwargs)
        encoder = CoverageRepairingEncoder(encoder)
        original_run_one = runner._run_one

        def run_one_with_capture_limit(*run_args, **run_kwargs):
            return call_pinned_issue73_run_one(
                runner, original_run_one, *run_args, **run_kwargs,
            )

        runner._run_one = run_one_with_capture_limit
        return config, runner, module, encoder

    runtime.initialize_native_runtime = initialize_with_capture_limit_compat
    runtime._issue84_capture_limit_compat_installed = True


def _preflight_optimizer_evidence(
    *, optimizer_id: str, source_sha256: str, runtime_optimizer_path: Path,
    parent_optimizer_path: Path,
) -> dict[str, Any]:
    return {
        "issue73_runtime_optimizer": {
            "status": "VERIFIED_NO_GAMES_STARTED",
            "optimizer_id": optimizer_id,
            "source_sha256": source_sha256,
            "loaded_path": Path(runtime_optimizer_path).as_posix(),
        },
        "parent_optimizer_checkpoint_sha256": file_sha256(parent_optimizer_path),
    }


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def write_once(path: Path, payload: bytes) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError(f"immutable Issue 84 evidence differs: {path}")
        return sha256(payload)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise FileExistsError(f"immutable Issue 84 evidence differs: {path}")
        return sha256(payload)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_once(path: Path, value: object) -> str:
    return write_once(path, canonical(value) + b"\n")


def host_path(wsl_path: str) -> Path:
    if os.name != "nt":
        return Path(wsl_path)
    match = re.match(r"^/mnt/([a-z])/(.*)$", wsl_path)
    if not match:
        raise ValueError(f"not a /mnt/<drive> path: {wsl_path}")
    return Path(f"{match.group(1).upper()}:/{match.group(2)}")


def git_output(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def package_commit() -> str:
    value = os.environ.get("ISSUE84_PACKAGE_COMMIT", "")
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise RuntimeError(
            "ISSUE84_PACKAGE_COMMIT must be supplied by the PowerShell Git wrapper; "
            "the WSL mount's Windows worktree metadata is not a native Git checkout"
        )
    return value


def seed_table_path(package: str) -> Path:
    """Keep each source-locked seed schedule immutable under its package SHA."""
    if not re.fullmatch(r"[0-9a-f]{40}", package):
        raise ValueError("seed-table package must be a full commit SHA")
    return host_path(DATA_ROOT_WSL) / "artifacts" / "seed-tables" / package / "seed-table.json"


def _git_blob_sha1(payload: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()


def _issue73_snapshot_paths(config: Mapping[str, Any]) -> tuple[str, ...]:
    """List only the reviewed #73 inputs needed by the pinned #75 runtime."""
    selection = read_json(UPSTREAM75 / "issue74-root-selection-lock.json")
    rows = selection.get("selected_roots", [])
    stage0 = config.get("stage0", {})
    owners = [int(value) for value in stage0.get("historical_debug_owner_seeds", [])]
    if len(rows) != 32 or len(set(owners)) != len(owners) or not owners:
        raise RuntimeError("pinned Issue 75 selection inputs are incomplete")
    if {int(row["owner_seed"]) for row in rows} != set(owners):
        raise RuntimeError("pinned Issue 75 roots do not match the registered owner population")
    paths = {
        "runner.py",
        "policy.py",
        "public_history_execution.py",
        "source_lock_integrity.py",
        "config.json",
        "config.lock.sha256",
        "receipts/gate-a-requalification-v5/source-lock.json",
        "receipts/stage0/policies/pi_init.pt",
    }
    for row in rows:
        root_id = str(row["root_id"])
        if not re.fullmatch(r"root-[0-9]{9}-[0-9]{4}", root_id):
            raise RuntimeError("pinned Issue 75 selection contains an invalid root ID")
        paths.add(f"receipts/stage0/roots/{root_id}.json")
    for owner in owners:
        paths.add(f"receipts/stage0/debug-owner-runs/{owner}.json")
    return tuple(sorted(paths))


def issue73_source_identity() -> dict[str, Any]:
    """Verify the minimal external snapshot against its immutable manifest and Git objects."""
    config = read_json(UPSTREAM75 / "config.json")
    source = config["source"]
    commit = str(source["spire_reviewed_commit"])
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Issue 75 does not name a full locked Issue 73 commit")
    repo = host_path(SOURCE_REPO_WSL)
    if git_output(repo, "rev-parse", commit) != commit:
        raise RuntimeError("pinned Issue 73 Git commit is unavailable in the source repository")
    root_tree = str(source["issue73_root_tree_git_sha1"])
    actual_root_tree = git_output(
        repo, "rev-parse", f"{commit}:{ISSUE73_SOURCE_STUDY}/receipts/stage0/roots",
    )
    if actual_root_tree != root_tree:
        raise RuntimeError("pinned Issue 73 root tree differs from the registered Git tree")

    issue73_config_bytes = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{ISSUE73_SOURCE_STUDY}/config.json"],
        check=True, capture_output=True,
    ).stdout
    issue73_config = json.loads(issue73_config_bytes)
    runtime_optimizer = issue73_config["runtime_optimizer"]
    snapshot_wsl = f"{DATA_ROOT_WSL}/sources/issue73-{commit}-issue84-runtime-v2"
    snapshot = host_path(snapshot_wsl)
    manifest_path = snapshot / "source-snapshot.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"durable pinned Issue 73 source snapshot is missing: {snapshot_wsl}")
    manifest = read_json(manifest_path)
    expected_manifest = {
        "schema_id": "issue84-locked-issue73-source-snapshot-v2",
        "snapshot_revision": 2,
        "consumer": "issue-84-six-arm",
        "issue73_source_commit": commit,
        "issue73_root_tree_git_sha1": root_tree,
        "issue74_selection_lock_source_commit": source["issue74_selection_lock_source_commit"],
        "issue74_selection_lock_source_path": source["issue74_selection_lock_source_path"],
        "issue74_selection_lock_sha256_lf": source["issue74_selection_lock_sha256_lf"],
        "issue73_root_source_lock_sha256_lf": source["issue73_root_source_lock_sha256_lf"],
        "issue73_pi_init_sha256": source["issue73_pi_init_sha256"],
        "runtime_optimizer_id": runtime_optimizer["optimizer_id"],
        "runtime_optimizer_source_path": runtime_optimizer["source_path"],
        "runtime_optimizer_sha256": runtime_optimizer["sha256"],
    }
    for key, value in expected_manifest.items():
        if manifest.get(key) != value:
            raise RuntimeError(f"pinned Issue 73 snapshot manifest differs: {key}")
    files = manifest.get("files")
    required_paths = set(_issue73_snapshot_paths(config))
    if not isinstance(files, dict) or set(files) != required_paths:
        raise RuntimeError("pinned Issue 73 snapshot file list differs from the locked runtime inputs")

    specs: list[str] = []
    total_bytes = 0
    file_hashes: dict[str, str] = {}
    for relative in sorted(required_paths):
        parts = Path(relative).parts
        if Path(relative).is_absolute() or ".." in parts:
            raise RuntimeError("pinned Issue 73 snapshot contains an unsafe relative path")
        path = snapshot / ISSUE73_SOURCE_STUDY
        for part in parts:
            path = path / part
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to((snapshot / ISSUE73_SOURCE_STUDY).resolve(strict=True)):
            raise RuntimeError("pinned Issue 73 snapshot path escaped its source root")
        payload = resolved.read_bytes()
        record = files[relative]
        digest = hashlib.sha256(payload).hexdigest()
        blob = _git_blob_sha1(payload)
        if (
            record.get("bytes") != len(payload)
            or record.get("sha256") != digest
            or record.get("git_blob_sha1") != blob
        ):
            raise RuntimeError(f"pinned Issue 73 snapshot file changed: {relative}")
        total_bytes += len(payload)
        file_hashes[relative] = digest
        specs.append(f"{commit}:{ISSUE73_SOURCE_STUDY}/{relative}")

    object_check = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        input="\n".join(specs) + "\n", check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    if len(object_check) != len(specs):
        raise RuntimeError("Git did not return every pinned Issue 73 source object")
    for relative, line in zip(sorted(required_paths), object_check, strict=True):
        record = files[relative]
        expected = f"{record['git_blob_sha1']} blob {record['bytes']}"
        if line != expected:
            raise RuntimeError(f"pinned Issue 73 source object mismatch: {relative}")

    root_lock_path = snapshot / ISSUE73_SOURCE_STUDY / "receipts/gate-a-requalification-v5/source-lock.json"
    pi_init_path = snapshot / ISSUE73_SOURCE_STUDY / "receipts/stage0/policies/pi_init.pt"
    if file_sha256(root_lock_path) != source["issue73_root_source_lock_sha256_lf"]:
        raise RuntimeError("pinned Issue 73 source lock differs from its registered normalized hash")
    if file_sha256(pi_init_path) != source["issue73_pi_init_sha256"]:
        raise RuntimeError("pinned Issue 73 pi_init checkpoint differs from its registered hash")
    runtime_optimizer_path = snapshot / ISSUE73_SOURCE_STUDY / "public_history_execution.py"
    optimizer_digest = hashlib.sha256(
        runtime_optimizer_path.read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()
    if optimizer_digest != runtime_optimizer["sha256"]:
        raise RuntimeError("pinned Issue 73 runtime optimizer differs from its registered hash")
    return {
        "path": snapshot_wsl,
        "source_repository": SOURCE_REPO_WSL,
        "source_commit": commit,
        "root_tree_git_sha1": root_tree,
        "manifest_sha256": file_sha256(manifest_path),
        "file_count": len(required_paths),
        "total_bytes": total_bytes,
        "runtime_optimizer_id": runtime_optimizer["optimizer_id"],
        "runtime_optimizer_sha256": optimizer_digest,
        "files_sha256": file_hashes,
    }


def bind_issue73_source_paths(runtime: Any, preflight: Any, snapshot_root: Path) -> None:
    """Rebase verbatim Issue 75 modules from their original repo layout to Issue 84 inputs."""
    issue73 = snapshot_root / ISSUE73_SOURCE_STUDY
    source_repo = host_path(SOURCE_REPO_WSL)
    runtime.REPO_ROOT = source_repo
    runtime.ISSUE73_DIR = issue73
    runtime.ISSUE73_POLICY_PATH = issue73 / "policy.py"
    runtime.ISSUE73_RUNNER_PATH = issue73 / "runner.py"
    runtime.ISSUE73_RUNTIME_OPTIMIZER_PATH = issue73 / "public_history_execution.py"
    runtime.PI_INIT_PATH = issue73 / "receipts/stage0/policies/pi_init.pt"
    preflight.ROOT = snapshot_root
    def source_git(*args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd or source_repo, check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return result.stdout.strip()

    preflight.git = source_git
    def source_git_blob_sha256(commit: str, relative_path: str) -> str:
        result = subprocess.run(
            ["git", "show", f"{commit}:{relative_path}"], cwd=source_repo,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return hashlib.sha256(result.stdout.replace(b"\r\n", b"\n")).hexdigest()

    preflight.git_blob_sha256 = source_git_blob_sha256
    preflight.ISSUE73_LOCK = issue73 / "receipts/gate-a-requalification-v5/source-lock.json"
    preflight.ISSUE73_PI_INIT = issue73 / "receipts/stage0/policies/pi_init.pt"
    preflight.ROOTS = issue73 / "receipts/stage0/roots"
    preflight.OWNER_RUNS = issue73 / "receipts/stage0/debug-owner-runs"
    preflight.SELECTION_PATH = UPSTREAM75 / "issue74-root-selection-lock.json"


def _source_files() -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(STUDY.rglob("*")):
        if not path.is_file() or path.name == SOURCE_LOCK_PATH.name:
            continue
        if "__pycache__" in path.parts or (path.suffix not in {".py", ".json"} and path.name != ".gitignore"):
            continue
        files[path.relative_to(STUDY).as_posix()] = file_sha256(path)
    return files


def _upstream_files() -> dict[str, str]:
    manifest = read_json(UPSTREAM_MANIFEST_PATH)
    if manifest.get("upstream_commit") != UPSTREAM_COMMIT:
        raise RuntimeError("vendored upstream source commit differs from the registered pin")
    overlays = manifest.get("overlays", {})
    if not isinstance(overlays, Mapping):
        raise RuntimeError("vendored upstream overlay manifest is malformed")
    observed: dict[str, str] = {}
    for relative, record in manifest["files"].items():
        path = UPSTREAM / relative
        digest = file_sha256(path)
        overlay = overlays.get(relative)
        expected = record["sha256"]
        if overlay is not None:
            if (
                not isinstance(overlay, Mapping)
                or overlay.get("issue") != 84
                or overlay.get("base_commit") != UPSTREAM_COMMIT
                or overlay.get("base_sha256") != record.get("sha256")
                or not isinstance(overlay.get("reason"), str)
                or not overlay["reason"].strip()
                or not isinstance(overlay.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", overlay["sha256"])
                or not isinstance(record.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                or not isinstance(overlay.get("bytes"), int)
            ):
                raise RuntimeError(f"vendored upstream overlay provenance is invalid: {relative}")
            expected = overlay.get("sha256")
            if overlay.get("bytes") != path.stat().st_size:
                raise RuntimeError(f"vendored upstream overlay size differs: {relative}")
        if digest != expected:
            raise RuntimeError(f"vendored upstream file changed: {relative}")
        observed[relative] = digest
    if set(overlays).difference(manifest["files"]):
        raise RuntimeError("vendored upstream overlay names an unregistered file")
    return observed


def _external_identities() -> dict[str, Any]:
    sts = host_path(STS_SOURCE_WSL)
    stsr = host_path(STSRL_SOURCE_WSL)
    native = host_path(NATIVE_WSL)
    python = host_path(PYTHON_WSL)
    identities = {
        "sts_lightspeed": {
            "path": STS_SOURCE_WSL,
            "commit": git_output(sts, "rev-parse", "HEAD"),
            "status": git_output(sts, "status", "--porcelain", "--untracked-files=all"),
        },
        "STSRL": {
            "path": STSRL_SOURCE_WSL,
            "commit": git_output(stsr, "rev-parse", "HEAD"),
            "status": git_output(stsr, "status", "--porcelain", "--untracked-files=all"),
        },
        "native_module": {"path": NATIVE_WSL, "sha256": file_sha256(native)},
        "python": {"path": PYTHON_WSL, "sha256": file_sha256(python)},
    }
    return identities


def _parent_identities() -> dict[str, Any]:
    models = host_path(PARENT_MODELS_WSL)
    pi12 = models / "pi12.pt"
    optimizer = models / "block-11-step.pt"
    update = models / "issue82-update-11.json"
    return {
        "execution_commit": PARENT_EXECUTION_COMMIT,
        "pi12_path": f"{PARENT_MODELS_WSL}/pi12.pt",
        "pi12_sha256": file_sha256(pi12),
        "pi12_digest": PARENT_MODEL_DIGEST,
        "optimizer_path": f"{PARENT_MODELS_WSL}/block-11-step.pt",
        "optimizer_sha256": file_sha256(optimizer),
        "update_receipt_sha256": file_sha256(update),
        "optimizer_step": 64,
        "training_feature_receipt_manifest_sha256": (
            "376948ed54207dd55011da2d8d65c9437af0e57903147ddd3f71b72d7ce213b0"
        ),
    }


def make_source_lock() -> dict[str, Any]:
    contract = read_json(CONTRACT_PATH)
    if contract["native_commit"] != NATIVE_COMMIT or contract["stsr_source_commit"] != STSRL_COMMIT:
        raise RuntimeError("contract native/STSRL pin differs from the registered source line")
    upstream = _upstream_files()
    external = _external_identities()
    parent = _parent_identities()
    issue73 = issue73_source_identity()
    expected_external = {
        "sts_lightspeed": (NATIVE_COMMIT, ""),
        "STSRL": (STSRL_COMMIT, ""),
    }
    for name, (commit, _) in expected_external.items():
        if external[name]["commit"] != commit or external[name]["status"]:
            raise RuntimeError(f"external source tree is not the clean registered revision: {name}")
    if external["native_module"]["sha256"] != NATIVE_SHA256:
        raise RuntimeError("native extension bytes differ from the registered native artifact")
    if external["python"]["sha256"] != PYTHON_SHA256:
        raise RuntimeError("pinned Python executable differs from the registered runtime")
    expected_parent = {
        "pi12_sha256": PARENT_MODEL_SHA256,
        "optimizer_sha256": PARENT_OPTIMIZER_SHA256,
        "update_receipt_sha256": PARENT_UPDATE_SHA256,
    }
    for name, digest in expected_parent.items():
        if parent[name] != digest:
            raise RuntimeError(f"#82 parent evidence hash mismatch: {name}")
    return {
        "schema_id": "issue84-source-lock-v1",
        "study_id": "issue-84-six-arm",
        "scope": "STUDY_ONLY",
        "generated_from_commit": package_commit(),
        "contract_sha256": file_sha256(CONTRACT_PATH),
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_files": upstream,
        "issue73_source_snapshot": issue73,
        "external_runtime": external,
        "parent": parent,
        "fixed_scales": {"t_ref": T_REF, "s_ref": S_REF},
        "local_code_files": _source_files(),
        "seed_derivation": "issue84-v1|phase|arm|owner|purpose|index; SHA256 first 31 bits",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def write_source_lock() -> dict[str, Any]:
    lock = make_source_lock()
    path = SOURCE_LOCK_PATH
    payload = canonical(lock) + b"\n"
    if path.exists() and path.read_bytes() != payload:
        raise FileExistsError("source lock already exists and differs; do not rewrite a frozen lock")
    write_once(path, payload)
    return {"source_lock_sha256": sha256(payload), "locked_code_files": len(lock["local_code_files"])}


def refresh_source_lock(*, lock: Mapping[str, Any] | None = None, data_root: Path | None = None) -> dict[str, Any]:
    """Archive the current lock before atomically binding an implementation repair."""
    frozen = dict(lock) if lock is not None else make_source_lock()
    payload = canonical(frozen) + b"\n"
    path = SOURCE_LOCK_PATH
    previous = path.read_bytes() if path.is_file() else None
    if previous == payload:
        return {"source_lock_sha256": sha256(payload), "archived_previous_lock": None}

    archived_path = None
    if previous is not None:
        previous_sha = sha256(previous)
        root = Path(data_root) if data_root is not None else host_path(DATA_ROOT_WSL)
        archive = root / "artifacts" / "source-lock-history" / (
            f"{package_commit()}-{previous_sha}.json"
        )
        write_once(archive, previous)
        if sha256(archive.read_bytes()) != previous_sha:
            raise RuntimeError("archived prior source lock failed its SHA-256 check")
        archived_path = str(archive)

    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return {
        "source_lock_sha256": sha256(payload),
        "locked_code_files": len(frozen["local_code_files"]),
        "archived_previous_lock": archived_path,
        "archived_previous_lock_sha256": None if previous is None else sha256(previous),
    }


def verify_source_lock(*, require_clean_worktree: bool) -> dict[str, Any]:
    if not SOURCE_LOCK_PATH.is_file():
        raise FileNotFoundError("frozen source-lock.json has not been created")
    lock = read_json(SOURCE_LOCK_PATH)
    if lock.get("schema_id") != "issue84-source-lock-v1":
        raise RuntimeError("unexpected Issue 84 source-lock schema")
    if lock.get("local_code_files") != _source_files():
        raise RuntimeError("study source files changed after source lock")
    upstream = _upstream_files()
    if lock.get("upstream_files") != upstream:
        raise RuntimeError("upstream source snapshot differs from source lock")
    if lock.get("issue73_source_snapshot") != issue73_source_identity():
        raise RuntimeError("pinned Issue 73 runtime source snapshot differs from source lock")
    if lock.get("external_runtime") != _external_identities():
        raise RuntimeError("native/STSRL/runtime identity differs from source lock")
    if lock.get("parent") != _parent_identities():
        raise RuntimeError("#82 parent model or optimizer evidence differs from source lock")
    if lock.get("fixed_scales") != {"t_ref": T_REF, "s_ref": S_REF}:
        raise RuntimeError("fixed scales differ from source lock")
    if require_clean_worktree:
        if os.environ.get("ISSUE84_WORKTREE_CLEAN") != "1":
            raise RuntimeError("PowerShell Git wrapper did not confirm a clean source-locked worktree")
    return {"lock": lock, "sha256": file_sha256(SOURCE_LOCK_PATH)}


BACKEND = None
BACKEND_ORIGINAL_LOAD_MODULES = None


def install_failure_classification_receipt_check(backend) -> None:
    """Fail closed if a persisted failure sidecar differs from its result."""
    original = backend._attempt_failure_history
    if getattr(original, "_issue84_failure_classification_receipt_check", False):
        return

    def checked_history(base: Path, row: dict) -> tuple[list[dict], bool]:
        history, incomplete = original(base, row)
        if incomplete:
            return history, True
        for attempt in sorted(Path(base).glob("attempt-*")):
            if not attempt.is_dir():
                continue
            classification_path = attempt / "failure-classification.json"
            if not classification_path.is_file():
                continue
            try:
                recorded = backend.read_json(classification_path)
                result = backend.read_json(attempt / "result.json")
                expected = backend.classify_attempt_failure(result)
            except (OSError, ValueError):
                return [], True
            if recorded != expected:
                return [], True
        return history, False

    checked_history._issue84_failure_classification_receipt_check = True
    backend._attempt_failure_history = checked_history


def configure_backend():
    global BACKEND, BACKEND_ORIGINAL_LOAD_MODULES
    if BACKEND is not None:
        return BACKEND
    spec = importlib.util.spec_from_file_location("issue84_vendored_issue79_backend", BACKEND_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("vendored #79 native execution backend cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.REPO = REPO
    module.STUDY = STUDY
    module.STUDY75 = UPSTREAM75
    module.STAGE1 = UPSTREAM75 / "stage1"
    module.RETAINED = host_path(PARENT_MODELS_WSL)
    module.ARTIFACTS = host_path(DATA_ROOT_WSL) / "artifacts"
    module.SEED_TABLE = seed_table_path(package_commit())
    module.SOURCE_LOCK = SOURCE_LOCK_PATH
    module.PINNED_PYTHON = Path(PYTHON_WSL)
    module.PILOT_TRAINING = tuple(range(8_480_001, 8_480_033))
    module.PILOT_EVALUATION = tuple(range(8_490_001, 8_490_009))
    module.FORMAL_TRAINING = tuple(range(8_400_001, 8_402_049))
    module.FORMAL_EVALUATION = tuple(range(8_460_001, 8_460_065))
    module.CONCURRENCY = CONCURRENCY
    module.__file__ = str(STUDY / "driver.py")
    module.source_commit = package_commit
    install_failure_classification_receipt_check(module)
    BACKEND = module
    BACKEND_ORIGINAL_LOAD_MODULES = module.load_modules
    module.load_modules = load_modules
    return module


def _pilot_actor_update_model_path(active_root: Path, package: str) -> Path | None:
    arm = Path(active_root).name
    spec = PILOT_ACTOR_UPDATE_CARRY_FORWARD.get(arm)
    pointer_path = Path(active_root) / "diagnostics" / "pilot-update-carry-forward.json"
    if spec is None or not pointer_path.is_file():
        return None

    backend = configure_backend()
    pointer = read_json(pointer_path)
    pointer_hash = pointer.get("manifest_sha256")
    unhashed = dict(pointer)
    unhashed.pop("manifest_sha256", None)
    expected_root = Path("pilot") / str(spec["package"]) / arm
    expected = {
        "schema_id": "issue84-pilot-actor-update-carry-forward-v1",
        "current_package_commit": package,
        "current_source_lock_sha256": file_sha256(SOURCE_LOCK_PATH),
        "arm": arm,
        "source_package_commit": spec["package"],
        "source_lock_sha256": spec["source_lock_sha256"],
        "source_progress_sha256": spec["progress_sha256"],
        "source_receipt_sha256": spec["receipt_sha256"],
        "source_artifact_root": expected_root.as_posix(),
        "model_sha256": spec["model_sha256"],
        "optimizer_checkpoint_sha256": spec["checkpoint_sha256"],
        "model_digest": spec["model_digest"],
        "owner_training_feature_manifest_sha256": spec["owner_training_feature_manifest_sha256"],
    }
    if (
        set(pointer) != set(expected) | {"training_carry_forward_manifest_sha256", "manifest_sha256"}
        or any(pointer.get(key) != value for key, value in expected.items())
        or not isinstance(pointer.get("training_carry_forward_manifest_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", pointer["training_carry_forward_manifest_sha256"])
        or package != package_commit()
        or pointer_hash != sha256(canonical(unhashed))
    ):
        raise RuntimeError(f"pilot actor-update carry-forward pointer changed for arm {arm}")
    source_root = (backend.ARTIFACTS / expected_root).resolve(strict=True)
    try:
        source_root.relative_to(backend.ARTIFACTS.resolve(strict=True))
    except ValueError as exc:
        raise RuntimeError("pilot actor-update carry-forward path escapes the artifact root") from exc
    source_lock_path = backend.ARTIFACTS / "source-lock-history" / (
        f"{spec['package']}-{spec['source_lock_sha256']}.json"
    )
    receipt_path = source_root / "diagnostics" / "pilot-update.json"
    progress_path = source_root.parent / "pilot-progress.json"
    model_path = source_root / "models" / "pi13.pt"
    checkpoint_path = source_root / "models" / "blocks" / "pilot-block-00-step.pt"
    try:
        progress_path.resolve(strict=True).relative_to(source_root.parent)
        for path in (receipt_path, model_path, checkpoint_path):
            path.resolve(strict=True).relative_to(source_root)
    except ValueError as exc:
        raise RuntimeError("pilot actor-update evidence path escapes its source artifact root") from exc
    if (
        file_sha256(source_lock_path) != spec["source_lock_sha256"]
        or file_sha256(progress_path) != spec["progress_sha256"]
        or file_sha256(receipt_path) != spec["receipt_sha256"]
        or file_sha256(model_path) != spec["model_sha256"]
        or file_sha256(checkpoint_path) != spec["checkpoint_sha256"]
    ):
        raise RuntimeError(f"pilot actor-update carry-forward evidence changed for arm {arm}")
    return model_path


def load_modules(active_root: Path, *, pilot: bool):
    """Load the exact #75 API and bind it to this arm's durable data root."""
    backend = configure_backend()
    for path in (str(UPSTREAM75 / "stage1"), str(UPSTREAM75)):
        while path in sys.path:
            sys.path.remove(path)
    for name in ("algorithm", "runtime", "model", "learning", "executor", "stage1_budget", "preflight"):
        sys.modules.pop(name, None)
    algorithm, runtime, model, learning, executor = BACKEND_ORIGINAL_LOAD_MODULES(
        Path(active_root), pilot=pilot,
    )
    preflight = importlib.import_module("preflight")
    snapshot = host_path(issue73_source_identity()["path"])
    bind_issue73_source_paths(runtime, preflight, snapshot)
    install_issue73_capture_limit_compat(runtime)
    # The vendored #75 config records an absolute path from the original host.
    # Preserve its source hash, but adapt only that machine-specific path to the
    # task's explicitly locked D:-backed WSL location and verify the same bytes.
    def verify_locked_native_module(config: Mapping[str, Any]) -> Path:
        source = config.get("source", {})
        if source.get("native_module_sha256") != NATIVE_SHA256:
            raise RuntimeError("vendored config native digest differs from the Issue 84 source lock")
        native_path = host_path(NATIVE_WSL).resolve(strict=True)
        if not native_path.is_file() or file_sha256(native_path) != NATIVE_SHA256:
            raise RuntimeError("D:-backed native module bytes differ from the Issue 84 source lock")
        return native_path

    runtime.verify_native_module = verify_locked_native_module
    old_model_path = executor._model_path
    old_load_model = executor._load_model

    def model_path(label: str) -> Path:
        match = re.fullmatch(r"pi(\d+)", str(label))
        if match and 12 <= int(match.group(1)) <= 28:
            if int(match.group(1)) == 12:
                return host_path(PARENT_MODELS_WSL) / "pi12.pt"
            if int(match.group(1)) == 13:
                carried = _pilot_actor_update_model_path(Path(active_root), package_commit())
                if carried is not None:
                    return carried
            return Path(active_root) / "models" / f"{label}.pt"
        return old_model_path(label)

    def load_model(label: str, *, proxy: bool = False):
        match = re.fullmatch(r"pi(\d+)", str(label))
        if not match or not 12 <= int(match.group(1)) <= 28:
            return old_load_model(label, proxy=proxy)
        import torch
        from model import ActionConditionedPolicy, model_digest

        config = executor._read_json(executor.STUDY / "config.json")
        policy = config["policy"]
        actor = ActionConditionedPolicy(
            dimension=executor.POLICY_DIMENSION,
            seed=int(policy["initial_model_seed"]),
            final_weight_std=float(policy["actor_final_weight_std"]),
        ).eval()
        path = model_path(label)
        if not path.is_file():
            raise FileNotFoundError(f"registered Issue 84 checkpoint is missing: {label}")
        actor.load_state_dict(torch.load(path, map_location="cpu", weights_only=True), strict=True)
        digest = model_digest(actor)
        return actor, digest, {
            "source": "issue82-parent-pi12" if label == "pi12" else f"issue84-{label}",
            "checkpoint_sha256": file_sha256(path),
            "formal_use": True,
        }

    def arm_policy(phase: str, arm: str, row: Mapping[str, Any]):
        if phase == "training":
            study_arm = str(row.get("issue84_arm", ""))
            if study_arm not in ARMS:
                raise ValueError("training row lacks a registered Issue 84 arm")
            return f"pi{12 + int(row['block_index'])}", False
        if arm == "PARENT":
            return "pi12", False
        label = row.get("issue84_policy_label")
        if not isinstance(label, str) or not re.fullmatch(r"pi(?:1[2-9]|2[0-8])", label):
            raise ValueError("evaluation row lacks a registered frozen Issue 84 policy checkpoint")
        return label, False

    executor._model_path = model_path
    executor._load_model = load_model
    executor._arm_policy = arm_policy
    executor.STUDY_ID = "issue-84-six-arm"
    return algorithm, runtime, model, learning, executor


def verify_preflight() -> dict[str, Any]:
    source = verify_source_lock(require_clean_worktree=True)
    backend = configure_backend()
    os.environ["STS_LIGHTSPEED_SOURCE_DIR"] = STS_SOURCE_WSL
    os.environ["STSRL_SOURCE_DIR"] = STSRL_SOURCE_WSL
    root = host_path(DATA_ROOT_WSL) / "artifacts" / "preflight"
    algorithm, runtime, model, learning, executor = load_modules(root, pilot=False)
    config = read_json(UPSTREAM75 / "config.json")
    preflight = importlib.import_module("preflight")
    issue73_lineage = preflight.verify_lineage(config)
    preflight.verify_provenance_file(config, issue73_lineage)
    runtime.verify_source_repositories(config)
    native = runtime.verify_native_module(config)
    if file_sha256(Path(native)) != NATIVE_SHA256:
        raise RuntimeError("loaded native module is not the source-locked binary")
    runtime_probe = root / "issue73-runtime-probe" / source["sha256"]
    runtime.initialize_native_runtime(
        runtime_probe, sampling=False, stage1_binding=None,
    )
    runtime_optimizer = importlib.import_module("public_history_execution")
    runtime_optimizer_path = Path(runtime_optimizer.__file__).resolve(strict=True)
    expected_optimizer_path = (
        host_path(source["lock"]["issue73_source_snapshot"]["path"])
        / ISSUE73_SOURCE_STUDY / "public_history_execution.py"
    ).resolve(strict=True)
    if runtime_optimizer_path != expected_optimizer_path:
        raise RuntimeError("Issue 73 loaded the Issue 65 optimizer from an unlocked path")
    optimizer_id = source["lock"]["issue73_source_snapshot"]["runtime_optimizer_id"]
    optimizer_sha256 = file_sha256(runtime_optimizer_path)
    if optimizer_id != "issue65-demand-driven-public-history-v1" or optimizer_sha256 != (
        source["lock"]["issue73_source_snapshot"]["runtime_optimizer_sha256"]
    ):
        raise RuntimeError("pinned Issue 65 runtime optimizer identity differs after initialization")
    parent, parent_digest, parent_record = executor._load_model("pi12")
    if parent_digest != PARENT_MODEL_DIGEST or parent_record["checkpoint_sha256"] != PARENT_MODEL_SHA256:
        raise RuntimeError("#82 pi12 model failed exact hash/digest inheritance check")
    import torch
    parent_optimizer_path = host_path(PARENT_MODELS_WSL) / "block-11-step.pt"
    optimizer_blob = torch.load(parent_optimizer_path, map_location="cpu", weights_only=True)
    if optimizer_blob.get("completed_steps") != 64:
        raise RuntimeError("#82 optimizer checkpoint is not step 64")
    optimizer = learning._new_optimizer(parent)
    optimizer.load_state_dict(optimizer_blob["optimizer_state_dict"])
    steps = sorted(int(state["step"].item()) for state in optimizer.state.values())
    if not steps or set(steps) != {64} or len(steps) != len(tuple(parent.parameters())):
        raise RuntimeError("#82 optimizer state does not map exactly to pi12 parameters at step 64")
    optimizer_evidence = _preflight_optimizer_evidence(
        optimizer_id=optimizer_id,
        source_sha256=optimizer_sha256,
        runtime_optimizer_path=runtime_optimizer_path,
        parent_optimizer_path=parent_optimizer_path,
    )
    return {
        "schema_id": "issue84-native-preflight-v1",
        "status": "SUCCEEDED_NO_GAMES_STARTED",
        "source_lock_sha256": source["sha256"],
        "issue73_source_snapshot": source["lock"]["issue73_source_snapshot"],
        "issue73_lineage": issue73_lineage,
        **optimizer_evidence,
        "source_commit": backend.source_commit(),
        "native_module_sha256": file_sha256(Path(native)),
        "parent_model_sha256": parent_record["checkpoint_sha256"],
        "parent_model_digest": parent_digest,
        "parent_optimizer_parameter_steps": steps,
        "torch_version": torch.__version__,
        "python_version": sys.version,
    }


def save_preflight_receipt(result: Mapping[str, Any], *, data_root: Path | None = None) -> dict[str, Any]:
    """Persist one immutable no-game preflight receipt under the external data root."""
    commit = str(result.get("source_commit", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("preflight result has no exact package commit")
    if result.get("status") != "SUCCEEDED_NO_GAMES_STARTED":
        raise ValueError("only a successful no-game preflight can be receipted")
    root = Path(data_root) if data_root is not None else host_path(DATA_ROOT_WSL)
    path = root / "artifacts" / "preflight" / commit / "preflight-receipt.json"
    if path.is_file():
        existing = read_json(path)
        stable = {key: value for key, value in existing.items() if key != "recorded_utc"}
        if stable != dict(result):
            raise FileExistsError("existing immutable preflight receipt differs from current verification")
        return existing
    receipt = {
        **dict(result),
        "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_json_once(path, receipt)
    return receipt


def derive_seed(*parts: object) -> int:
    payload = "issue84-v1|" + "|".join(str(part) for part in parts)
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:4], "big") & 0x7FFFFFFF


def make_seed_table() -> dict[str, Any]:
    backend = configure_backend()
    rows: list[dict[str, Any]] = []

    def add(
        phase: str, owner: int, group: str, *, formal: bool,
        block: int | None = None, policy_label: str | None = None,
    ) -> None:
        row = backend._row(owner, phase=phase, arm=group, formal=formal, block=block)
        issue84_arm = group if group in ARMS else None
        row["policy_rng_seed"] = derive_seed("policy", phase, group, owner)
        row["search_invocation_seeds"] = [
            derive_seed("search", phase, group, owner, index)
            for index in range(len(row["search_invocation_seeds"]))
        ]
        if phase == "training":
            row["unit_id"] = f"TRAIN:{owner}:{issue84_arm}"
        row["issue84_arm"] = issue84_arm
        row["issue84_policy_label"] = policy_label
        if phase == "evaluation":
            row["paired_owner_key"] = f"eval-{owner}"
        rows.append(row)

    for arm in ARMS:
        for owner in range(8_480_001, 8_480_033):
            add("training", owner, "TRAIN", formal=False, block=0)
            rows[-1]["issue84_arm"] = arm
            rows[-1]["unit_id"] = f"TRAIN:{owner}:{arm}"
            rows[-1]["policy_rng_seed"] = derive_seed("policy", "pilot-training", arm, owner)
            rows[-1]["search_invocation_seeds"] = [
                derive_seed("search", "pilot-training", arm, owner, index) for index in range(4)
            ]
        for owner in range(8_400_001, 8_402_049):
            block = (owner - 8_400_001) // 128
            add("training", owner, "TRAIN", formal=True, block=block)
            rows[-1]["issue84_arm"] = arm
            rows[-1]["unit_id"] = f"TRAIN:{owner}:{arm}"
            rows[-1]["policy_rng_seed"] = derive_seed("policy", "formal-training", arm, owner)
            rows[-1]["search_invocation_seeds"] = [
                derive_seed("search", "formal-training", arm, owner, index) for index in range(4)
            ]

    for owner in range(8_490_001, 8_490_009):
        add("evaluation", owner, "PARENT", formal=False, policy_label="pi12")
        for arm in ARMS:
            add("evaluation", owner, arm, formal=False, policy_label="pi13")
            rows[-1]["issue84_arm"] = arm
            rows[-1]["policy_rng_seed"] = derive_seed("policy", "pilot-evaluation", arm, owner)

    for owner in range(8_460_001, 8_460_065):
        add("evaluation", owner, "PARENT", formal=True, policy_label="pi12")
        for arm in ARMS:
            for games in (512, 1024, 2048):
                group = f"{arm}{games}"
                policy_label = f"pi{12 + games // 128}"
                add("evaluation", owner, group, formal=True, policy_label=policy_label)
                rows[-1]["issue84_arm"] = arm
                rows[-1]["policy_rng_seed"] = derive_seed("policy", "validation", group, owner)

    for owner in range(8_470_001, 8_470_321):
        add("evaluation", owner, "PARENT", formal=True, policy_label="pi12")
        for arm in ARMS:
            add("evaluation", owner, arm, formal=True, policy_label="pi28")
            rows[-1]["issue84_arm"] = arm
            rows[-1]["policy_rng_seed"] = derive_seed("policy", "test", arm, owner)

    rows.sort(key=lambda row: (
        row["phase"], int(row["owner_seed"]), str(row["arm"]),
        str(row.get("issue84_arm") or ""),
    ))
    return {
        "schema_id": "issue84-seed-table-v1",
        "source_lock_sha256": file_sha256(SOURCE_LOCK_PATH),
        "seed_derivation": "issue84-v1|phase|arm|owner|purpose|index; SHA256 first 31 bits",
        "game_seed_rule": "game_seed equals the registered natural owner seed",
        "arm_specific_policy_rng": True,
        "row_count": len(rows),
        "rows_sha256": sha256(canonical(rows)),
        "rows": rows,
    }


def ensure_seed_table() -> dict[str, Any]:
    backend = configure_backend()
    table = make_seed_table()
    backend.write_json_once(backend.SEED_TABLE, table)
    observed = read_json(backend.SEED_TABLE)
    if observed != table:
        raise RuntimeError("durable seed table differs from source-locked generation")
    return observed


def seed_table_rows(table: Mapping[str, Any], *, phase: str, formal: bool, owners: range) -> list[dict[str, Any]]:
    return [
        row for row in table["rows"]
        if row["phase"] == phase and bool(row["formal"]) == formal
        and int(row["owner_seed"]) in owners
    ]


def run_summary(
    result: Mapping[str, Any], *, rows: Sequence[Mapping[str, Any]] = (),
    root: Path | None = None, package: str | None = None,
) -> dict[str, Any]:
    backend = configure_backend()
    by_id = {str(row["unit_id"]): row for row in rows}
    reports = list(result.get("results", {}).values())
    outcomes = []
    for report in reports:
        unit_id = str(report.get("unit_id"))
        row = by_id.get(unit_id, {})
        record = {
            "unit_id": unit_id,
            "owner_seed": report.get("owner_seed"),
            "phase": report.get("phase"),
            "arm": report.get("arm"),
            "issue84_arm": row.get("issue84_arm"),
            "issue84_policy_label": row.get("issue84_policy_label"),
            "paired_owner_key": report.get("paired_owner_key", row.get("paired_owner_key")),
            "game_seed": report.get("game_seed", row.get("game_seed")),
            "policy_rng_seed": row.get("policy_rng_seed"),
            "search_invocation_seeds": row.get("search_invocation_seeds"),
            "heart_win": report.get("heart_victory"),
            "terminal_floor": report.get("terminal_floor"),
            "terminal_act": report.get("terminal_act"),
            "terminal_outcome": report.get("terminal_outcome"),
            "act2_plus": report.get("act2_plus"),
            "act3_plus": report.get("act3_plus"),
            "act4_reached": report.get("act4_reached"),
            "completed_transitions": report.get("completed_transitions"),
            "policy_model_digest": report.get("model_digest"),
            "policy_source": report.get("policy_source"),
            "package_commit": report.get("package_commit"),
            "source_lock_sha256": report.get("source_lock_sha256"),
            "native_source_commit": report.get("native_source_commit"),
            "stsr_source_commit": report.get("stsr_source_commit"),
            "native_module_sha256": report.get("native_module_sha256"),
            "expert_decisions": report.get("expert_decisions"),
            "training_features": report.get("training_features"),
            "worker_wall_seconds": report.get("resource", {}).get("worker_wall_seconds"),
            "worker_process_cpu_seconds": report.get("resource", {}).get("worker_process_cpu_seconds"),
        }
        if root is not None:
            registered = by_id.get(unit_id)
            if registered is None:
                raise RuntimeError(f"worker report was not in its frozen seed rows: {unit_id}")
            success = backend.existing_success_record(
                Path(root), dict(registered), package,
            )
            if success is None:
                raise RuntimeError(f"worker report lacks a source-bound durable success receipt: {unit_id}")
            result_path = success[1]
            record["result_path"] = result_path.relative_to(Path(root)).as_posix()
            record["result_sha256"] = file_sha256(result_path)
        outcomes.append(record)
    return {
        "status": result.get("status"),
        "completed": int(result.get("completed", 0)),
        "elapsed_seconds": float(result.get("elapsed_seconds", 0.0)),
        "native_worker_wall_seconds": sum(
            float(value) for value in result.get("native_wall_seconds_by_unit", {}).values()
        ),
        "retry_count": sum(max(0, int(value) - 1) for value in result.get("tries", {}).values()),
        "outcomes": sorted(outcomes, key=lambda item: (str(item["arm"]), int(item["owner_seed"]))),
    }


def _terminal_game_reward(result: Mapping[str, Any]) -> float:
    """Apply the registered R=100*HeartWin+terminal_floor definition."""
    return 100.0 * float(bool(result["heart_victory"])) + float(result["terminal_floor"])


def _pilot_runtime_fingerprint() -> str:
    names = {
        "bind_issue73_source_paths",
        "install_issue73_capture_limit_compat",
        "load_modules",
        "_pilot_actor_update_model_path",
    }
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    if {node.name for node in functions} != names:
        raise RuntimeError("pilot predecessor compatibility fingerprint lacks a runtime adapter")
    payload = "\n".join(
        node.name + "\0" + ast.dump(node, annotate_fields=True, include_attributes=False)
        for node in functions
    ).encode("utf-8")
    payload += b"\0candidate_coverage.py\0" + file_sha256(STUDY / "candidate_coverage.py").encode("ascii")
    return sha256(payload)


def _historical_pilot_runtime_fingerprint(package: str) -> str:
    names = {
        "bind_issue73_source_paths",
        "install_issue73_capture_limit_compat",
        "load_modules",
    }
    relative = "studies/issue-84-six-arm/driver.py"
    source = subprocess.run(
        ["git", "show", f"{package}:{relative}"], cwd=REPO, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout
    tree = ast.parse(source)
    functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    if {node.name for node in functions} != names:
        raise RuntimeError("pilot predecessor source lacks a complete runtime adapter")
    payload = "\n".join(
        node.name + "\0" + ast.dump(node, annotate_fields=True, include_attributes=False)
        for node in functions
    ).encode("utf-8")
    return sha256(payload)


def _pilot_update_algorithm_fingerprint(source: str) -> str:
    names = {
        "_terminal_game_reward",
        "load_training_outcomes",
        "owner_feature_manifest",
        "pilot_actor_update",
    }
    tree = ast.parse(source)
    functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    if {node.name for node in functions} != names:
        raise RuntimeError("pilot actor-update lineage lacks a required training function")
    payload = "\n".join(
        node.name + "\0" + ast.dump(node, annotate_fields=True, include_attributes=False)
        for node in functions
    ).encode("utf-8")
    return sha256(payload)


def _verify_pilot_update_algorithm_unchanged(package: str) -> None:
    relative = "studies/issue-84-six-arm/driver.py"
    historical = subprocess.run(
        ["git", "show", f"{package}:{relative}"], cwd=REPO, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout
    current = Path(__file__).read_text(encoding="utf-8")
    if _pilot_update_algorithm_fingerprint(current) != _pilot_update_algorithm_fingerprint(historical):
        raise RuntimeError("pilot actor-update inputs or optimizer algorithm differ from the source receipt")


@contextmanager
def _predecessor_source_lock(package: str):
    """Temporarily validate predecessor receipts against their archived lock."""
    lock_sha256 = next((
        value["source_lock_sha256"]
        for value in PILOT_CARRY_FORWARD.values()
        if value["package"] == package
    ), None)
    if lock_sha256 is None:
        yield
        return
    backend = configure_backend()
    lock_path = backend.ARTIFACTS / "source-lock-history" / (
        f"{package}-{lock_sha256}.json"
    )
    if not lock_path.is_file() or file_sha256(lock_path) != lock_sha256:
        raise RuntimeError("pilot predecessor source-lock archive is missing or changed")
    previous_path = backend.SOURCE_LOCK
    backend.SOURCE_LOCK = lock_path
    try:
        if backend.source_lock_sha() != lock_sha256:
            raise RuntimeError("pilot predecessor source-lock identity changed")
        yield
    finally:
        backend.SOURCE_LOCK = previous_path


def _verify_predecessor_lock_compatibility(
    predecessor_lock: Mapping[str, Any], current_lock: Mapping[str, Any],
    *, package: str,
) -> None:
    compatible_fields = (
        "contract_sha256", "upstream_commit",
        "issue73_source_snapshot", "external_runtime", "parent",
        "fixed_scales", "seed_derivation",
    )
    if any(predecessor_lock.get(key) != current_lock.get(key) for key in compatible_fields):
        raise RuntimeError("pilot predecessor changes a locked contract, runtime, or parent input")
    predecessor_files = predecessor_lock.get("upstream_files")
    current_files = current_lock.get("upstream_files")
    manifest = read_json(UPSTREAM_MANIFEST_PATH)
    manifest_file = manifest.get("files", {}).get(PILOT_CANDIDATE_COVERAGE_OVERLAY_PATH, {})
    overlay = manifest.get("overlays", {}).get(PILOT_CANDIDATE_COVERAGE_OVERLAY_PATH, {})
    if (
        predecessor_lock.get("upstream_commit") != UPSTREAM_COMMIT
        or current_lock.get("upstream_commit") != UPSTREAM_COMMIT
        or not isinstance(predecessor_files, Mapping)
        or not isinstance(current_files, Mapping)
        or set(predecessor_files) != set(current_files)
        or manifest_file.get("sha256") != PILOT_CANDIDATE_COVERAGE_BASE_SHA256
        or not isinstance(overlay, Mapping)
        or overlay.get("issue") != 84
        or overlay.get("base_commit") != UPSTREAM_COMMIT
        or overlay.get("base_sha256") != PILOT_CANDIDATE_COVERAGE_BASE_SHA256
        or predecessor_files.get(PILOT_CANDIDATE_COVERAGE_OVERLAY_PATH)
        != PILOT_CANDIDATE_COVERAGE_BASE_SHA256
        or current_files.get(PILOT_CANDIDATE_COVERAGE_OVERLAY_PATH)
        != overlay.get("sha256")
        or any(
            predecessor_files[path] != current_files[path]
            for path in predecessor_files
            if path != PILOT_CANDIDATE_COVERAGE_OVERLAY_PATH
        )
    ):
        raise RuntimeError("pilot predecessor differs beyond the registered #84 scheduler overlay")
    predecessor_spec = next((
        value for value in PILOT_CARRY_FORWARD.values()
        if value["package"] == package
    ), None)
    if predecessor_spec is None:
        raise RuntimeError("pilot predecessor package has no registered carry-forward source lock")
    if _historical_pilot_runtime_fingerprint(package) != predecessor_spec["runtime_ast_sha256"]:
        raise RuntimeError("pilot predecessor episode-execution adapter differs from its registered source")
    if _pilot_runtime_fingerprint() != PILOT_PREDECESSOR_RUNTIME_AST_SHA256:
        raise RuntimeError("current pilot adapter differs from the reviewed coverage-equivalence source")


def _validate_carried_attempt(
    row: Mapping[str, Any], root: Path, report: Mapping[str, Any], result_path: Path,
    *, package: str, source_lock_sha256: str,
) -> dict[str, str]:
    unit_id = str(row["unit_id"])
    if (
        report.get("status") != "SUCCEEDED"
        or report.get("unit_id") != unit_id
        or report.get("owner_seed") != row.get("owner_seed")
        or report.get("phase") != "training"
        or report.get("arm") != "TRAIN"
        or report.get("formal") is not False
        or report.get("package_commit") != package
        or report.get("runner_code_commit") != package
        or report.get("source_lock_sha256") != source_lock_sha256
        or report.get("game_seed") != row.get("game_seed")
        or report.get("paired_owner_key") != row.get("paired_owner_key")
        or report.get("policy_source") != {
            "checkpoint_sha256": PARENT_MODEL_SHA256,
            "formal_use": True,
            "source": "issue82-parent-pi12",
        }
        or report.get("model_digest") != PARENT_MODEL_DIGEST
        or report.get("native_source_commit") != NATIVE_COMMIT
        or report.get("stsr_source_commit") != STSRL_COMMIT
        or report.get("native_module_sha256") != NATIVE_SHA256
        or report.get("expert_decisions") != 0
        or report.get("terminal_outcome") is None
    ):
        raise RuntimeError(f"pilot predecessor receipt violates the registered owner/source binding: {unit_id}")

    attempt_dir = result_path.parent
    started_path = attempt_dir / "started.json"
    finished_path = attempt_dir / "finished.json"
    started = read_json(started_path)
    finished = read_json(finished_path)
    if (
        started.get("unit_id") != unit_id
        or started.get("owner_seed") != row.get("owner_seed")
        or started.get("package_commit") != package
        or started.get("seed_row_sha256") != sha256(canonical(dict(row)))
        or finished.get("unit_id") != unit_id
        or finished.get("run_id") != started.get("run_id")
        or finished.get("status") != "SUCCEEDED"
        or finished.get("result_sha256") != file_sha256(result_path)
    ):
        raise RuntimeError(f"pilot predecessor start/finish receipts do not bind the frozen seed row: {unit_id}")

    pointer = report.get("training_features")
    if not isinstance(pointer, Mapping) or pointer.get("compression") != "zlib":
        raise RuntimeError(f"pilot predecessor lacks lossless training features: {unit_id}")
    feature_path = (root / str(pointer["path"])).resolve(strict=True)
    try:
        feature_path.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"pilot predecessor feature path escapes its artifact root: {unit_id}") from exc
    compressed = feature_path.read_bytes()
    if sha256(compressed) != pointer.get("sha256"):
        raise RuntimeError(f"pilot predecessor compressed feature bytes changed: {unit_id}")
    import zlib
    raw = zlib.decompress(compressed)
    if sha256(raw) != pointer.get("uncompressed_sha256"):
        raise RuntimeError(f"pilot predecessor feature content changed: {unit_id}")
    return {
        "unit_id": unit_id,
        "result_sha256": file_sha256(result_path),
        "started_sha256": file_sha256(started_path),
        "finished_sha256": file_sha256(finished_path),
        "training_features_sha256": str(pointer["sha256"]),
    }


def _pilot_training_resume(
    *, arm: str, package: str, pilot_root: Path, rows: Sequence[dict[str, Any]],
    seed_table: Mapping[str, Any], current_lock: Mapping[str, Any],
) -> tuple[Path, str, dict[str, Any] | None, dict[str, Any] | None]:
    current_root = pilot_root / arm
    predecessor_spec = PILOT_CARRY_FORWARD.get(arm)
    if predecessor_spec is None or package == predecessor_spec["package"]:
        return current_root, package, None, None

    backend = configure_backend()
    predecessor = str(predecessor_spec["package"])
    predecessor_lock_sha256 = str(predecessor_spec["source_lock_sha256"])
    predecessor_root = pilot_root.parent / predecessor / arm
    predecessor_table_path = seed_table_path(predecessor)
    if not predecessor_table_path.is_file():
        raise RuntimeError("pilot predecessor seed table is missing; refusing to repeat completed games")
    predecessor_table = read_json(predecessor_table_path)
    if (
        predecessor_table.get("source_lock_sha256") != predecessor_lock_sha256
        or predecessor_table.get("rows") != seed_table.get("rows")
        or predecessor_table.get("rows_sha256") != sha256(canonical(predecessor_table.get("rows")))
    ):
        raise RuntimeError("pilot predecessor seed schedule differs; refusing to repeat completed games")

    lock_path = backend.ARTIFACTS / "source-lock-history" / (
        f"{predecessor}-{predecessor_lock_sha256}.json"
    )
    if not lock_path.is_file() or file_sha256(lock_path) != predecessor_lock_sha256:
        raise RuntimeError("pilot predecessor source-lock archive is missing or changed")
    predecessor_lock = read_json(lock_path)
    _verify_predecessor_lock_compatibility(
        predecessor_lock, current_lock, package=predecessor,
    )

    prior_progress_path = pilot_root.parent / predecessor / "pilot-progress.json"
    if not prior_progress_path.is_file():
        raise RuntimeError("pilot predecessor progress record is missing; refusing to repeat completed games")
    prior_progress = read_json(prior_progress_path)
    prior_summary = prior_progress.get("training_runs", {}).get(arm)
    if (
        prior_progress.get("package_commit") != predecessor
        or prior_progress.get("source_lock_sha256") != predecessor_lock_sha256
        or not isinstance(prior_summary, dict)
        or prior_summary.get("status") != "SUCCEEDED"
        or prior_summary.get("completed") != len(rows)
        or len(rows) != 32
    ):
        raise RuntimeError(f"pilot predecessor does not contain a complete arm-{arm} training cohort")

    reports: dict[str, dict[str, Any]] = {}
    unit_manifest = []
    with _predecessor_source_lock(predecessor):
        for row in rows:
            record = backend.existing_success_record(predecessor_root, row, predecessor)
            if record is None:
                raise RuntimeError(
                    f"pilot predecessor is missing a source-bound success for {row['unit_id']}; "
                    "refusing to repeat completed games"
                )
            report, result_path = record
            unit_manifest.append(_validate_carried_attempt(
                row, predecessor_root, report, result_path,
                package=predecessor, source_lock_sha256=predecessor_lock_sha256,
            ))
            reports[str(row["unit_id"])] = report

        retry_count = int(prior_summary.get("retry_count", 0))
        elapsed_seconds = float(prior_summary.get("elapsed_seconds", 0.0))
        result = {
            "status": "SUCCEEDED",
            "completed": len(rows),
            "elapsed_seconds": elapsed_seconds,
            "native_wall_seconds_by_unit": {
                unit_id: float(report.get("resource", {}).get("worker_wall_seconds", 0.0))
                for unit_id, report in reports.items()
            },
            "tries": {"carried-forward": retry_count + 1},
            "results": reports,
        }
        verified_summary = run_summary(
            result, rows=rows, root=predecessor_root, package=predecessor,
        )
    if verified_summary["outcomes"] != prior_summary.get("outcomes"):
        raise RuntimeError("pilot predecessor progress summary differs from immutable game receipts")

    manifest = {
        "schema_id": "issue84-pilot-carry-forward-v1",
        "source_package_commit": predecessor,
        "source_lock_sha256": predecessor_lock_sha256,
        "seed_table_rows_sha256": predecessor_table["rows_sha256"],
        "predecessor_runtime_adapter_fingerprint_sha256": predecessor_spec["runtime_ast_sha256"],
        "current_runtime_adapter_fingerprint_sha256": _pilot_runtime_fingerprint(),
        "supported_projection_equivalence": "verified-by-coverage-adapter-and-locked-encoder-row-witness",
        "units": unit_manifest,
    }
    carry_forward = {
        **manifest,
        "manifest_sha256": sha256(canonical(manifest)),
        "artifact_root": predecessor_root.relative_to(backend.ARTIFACTS).as_posix(),
    }
    summary = {
        **verified_summary,
        "native_worker_wall_seconds": float(prior_summary["native_worker_wall_seconds"]),
        "evidence_origin": {
            "package_commit": predecessor,
            "source_lock_sha256": PILOT_PREDECESSOR_LOCK_SHA256,
            "artifact_root": carry_forward["artifact_root"],
            "manifest_sha256": carry_forward["manifest_sha256"],
        },
    }
    return predecessor_root, predecessor, summary, carry_forward


def _pilot_actor_update_resume(
    *, arm: str, package: str, pilot_root: Path, training_root: Path,
    training_package: str, training_carry_manifest: Mapping[str, Any] | None,
    training_summary: Mapping[str, Any], current_lock: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    spec = PILOT_ACTOR_UPDATE_CARRY_FORWARD.get(arm)
    if spec is None or package == spec["package"]:
        return None
    backend = configure_backend()
    expected_training_root = Path("pilot") / str(spec["training_package"]) / arm
    if (
        training_package != spec["training_package"]
        or training_carry_manifest is None
        or training_carry_manifest.get("source_package_commit") != spec["training_package"]
        or training_carry_manifest.get("source_lock_sha256") != spec["training_source_lock_sha256"]
        or len(training_carry_manifest.get("units", [])) != 32
        or training_summary.get("status") != "SUCCEEDED"
        or training_summary.get("completed") != 32
    ):
        raise RuntimeError(f"pilot arm {arm} actor update is not bound to its retained training cohort")
    try:
        training_root.resolve(strict=True).relative_to(backend.ARTIFACTS.resolve(strict=True))
    except ValueError as exc:
        raise RuntimeError("pilot actor-update training root escapes the artifact root") from exc
    if training_root.resolve(strict=True) != (backend.ARTIFACTS / expected_training_root).resolve(strict=True):
        raise RuntimeError(f"pilot arm {arm} training root differs from its registered carry-forward source")

    source_package = str(spec["package"])
    source_lock_sha256 = str(spec["source_lock_sha256"])
    lock_path = backend.ARTIFACTS / "source-lock-history" / (
        f"{source_package}-{source_lock_sha256}.json"
    )
    if not lock_path.is_file() or file_sha256(lock_path) != source_lock_sha256:
        raise RuntimeError(f"pilot arm {arm} actor-update source lock is missing or changed")
    source_lock = read_json(lock_path)
    _verify_predecessor_lock_compatibility(source_lock, current_lock, package=source_package)

    source_root = backend.ARTIFACTS / "pilot" / source_package / arm
    progress_path = source_root.parent / "pilot-progress.json"
    receipt_path = source_root / "diagnostics" / "pilot-update.json"
    model_path = source_root / "models" / "pi13.pt"
    checkpoint_path = source_root / "models" / "blocks" / "pilot-block-00-step.pt"
    if (
        not source_root.is_dir()
        or file_sha256(progress_path) != spec["progress_sha256"]
        or file_sha256(receipt_path) != spec["receipt_sha256"]
        or file_sha256(model_path) != spec["model_sha256"]
        or file_sha256(checkpoint_path) != spec["checkpoint_sha256"]
    ):
        raise RuntimeError(f"pilot arm {arm} actor-update lineage bytes differ from the registered receipts")
    receipt = read_json(receipt_path)
    expected_receipt = {
        "schema_id": "issue84-pilot-actor-update-v1",
        "status": "SUCCEEDED",
        "arm": arm,
        "block_index": 0,
        "owner_count": 32,
        "owner_seed_sha256": spec["owner_seed_sha256"],
        "owner_training_feature_manifest_sha256": spec["owner_training_feature_manifest_sha256"],
        "source_model_digest": PARENT_MODEL_DIGEST,
        "result_model_digest": spec["model_digest"],
        "model_checkpoint": {
            "model_digest": spec["model_digest"],
            "path": "models/pi13.pt",
            "sha256": spec["model_sha256"],
        },
        "model_checkpoint_sha256": spec["model_sha256"],
        "optimizer_checkpoint_sha256": spec["checkpoint_sha256"],
        "inherited_optimizer_steps": [64] * 6,
        "result_optimizer_steps": [128] * 6,
        "completed_steps": 64,
        "pilot_nonformal": True,
    }
    if any(receipt.get(key) != value for key, value in expected_receipt.items()):
        raise RuntimeError(f"pilot arm {arm} actor-update receipt does not match its registered source")
    _verify_pilot_update_algorithm_unchanged(source_package)

    artifact_root = source_root.relative_to(backend.ARTIFACTS).as_posix()
    pointer = {
        "schema_id": "issue84-pilot-actor-update-carry-forward-v1",
        "current_package_commit": package,
        "current_source_lock_sha256": file_sha256(SOURCE_LOCK_PATH),
        "arm": arm,
        "source_package_commit": source_package,
        "source_lock_sha256": source_lock_sha256,
        "source_progress_sha256": str(spec["progress_sha256"]),
        "source_receipt_sha256": str(spec["receipt_sha256"]),
        "source_artifact_root": artifact_root,
        "training_carry_forward_manifest_sha256": training_carry_manifest["manifest_sha256"],
        "model_sha256": str(spec["model_sha256"]),
        "optimizer_checkpoint_sha256": str(spec["checkpoint_sha256"]),
        "model_digest": str(spec["model_digest"]),
        "owner_training_feature_manifest_sha256": str(spec["owner_training_feature_manifest_sha256"]),
    }
    carry_forward = {**pointer, "manifest_sha256": sha256(canonical(pointer))}
    pointer_path = pilot_root / arm / "diagnostics" / "pilot-update-carry-forward.json"
    write_json_once(pointer_path, carry_forward)
    if read_json(pointer_path) != carry_forward:
        raise RuntimeError(f"pilot arm {arm} actor-update pointer changed after persistence")
    summary = {
        "status": receipt["status"],
        "completed_steps": int(receipt["completed_steps"]),
        "elapsed_seconds": float(receipt["elapsed_seconds"]),
        "model_digest": receipt["result_model_digest"],
        "optimizer_steps": receipt["result_optimizer_steps"],
        "checkpoint_sha256": receipt["optimizer_checkpoint_sha256"],
        "evidence_origin": {
            "package_commit": source_package,
            "source_lock_sha256": source_lock_sha256,
            "artifact_root": artifact_root,
            "progress_sha256": str(spec["progress_sha256"]),
            "receipt_sha256": str(spec["receipt_sha256"]),
            "manifest_sha256": carry_forward["manifest_sha256"],
        },
    }
    return summary, carry_forward


def load_training_outcomes(rows: list[dict[str, Any]], *, root: Path, executor, package: str):
    from gradients import OwnerOutcome

    examples = BACKEND._training_examples(rows, root=root, executor=executor, package_commit=package)
    outcomes = [
        OwnerOutcome(
            owner_seed=int(example.owner_seed),
            heart_win=bool(example.base.heart_win),
            terminal_floor=float(example.base.terminal_floor),
            decisions=example.base.decisions,
            complete=bool(example.base.complete),
        )
        for example in examples
    ]
    return examples, outcomes


def owner_feature_manifest(rows: Sequence[dict[str, Any]], *, root: Path, package: str) -> str:
    records = []
    for row in sorted(rows, key=lambda item: int(item["owner_seed"])):
        result = BACKEND.existing_success(root, row, package)
        if result is None or not isinstance(result.get("training_features"), dict):
            raise RuntimeError(f"owner lacks an immutable training-feature receipt: {row['unit_id']}")
        records.append({
            "owner_seed": int(row["owner_seed"]),
            "feature_sha256": result["training_features"]["sha256"],
            "reward": _terminal_game_reward(result),
        })
    return sha256(canonical(records))


def pilot_actor_update(
    arm: str, examples: list, outcomes: list, *, root: Path, executor, learning,
    training_root: Path | None = None, training_package: str | None = None,
) -> dict[str, Any]:
    from gradients import ScalarPredictor
    from training import optimizer_parameter_steps, train_actor_block
    from model import model_digest

    receipt_path = root / "diagnostics" / "pilot-update.json"
    if receipt_path.is_file():
        return read_json(receipt_path)
    package = package_commit()
    training_root = root if training_root is None else training_root
    training_package = package if training_package is None else training_package
    parent_optimizer_path = host_path(PARENT_MODELS_WSL) / "block-11-step.pt"
    actor, source_digest, parent_record = executor._load_model("pi12")
    if parent_record["checkpoint_sha256"] != PARENT_MODEL_SHA256:
        raise RuntimeError("pilot actor failed to load exact parent pi12 bytes")
    optimizer = learning._new_optimizer(actor)
    torch = __import__("torch")
    parent_state = torch.load(parent_optimizer_path, map_location="cpu", weights_only=True)
    optimizer.load_state_dict(parent_state["optimizer_state_dict"])
    inherited_steps = optimizer_parameter_steps(optimizer)
    if not inherited_steps or set(inherited_steps) != {64}:
        raise RuntimeError("pilot AdamW did not inherit exact #82 step-64 state")

    value_model = value_optimizer = action_value_model = action_value_optimizer = None
    if arm in {"D", "E", "F"}:
        from predictors import new_predictor_optimizer
        value_model = ScalarPredictor(seed=derive_seed("pilot", arm, "V"))
        value_optimizer = new_predictor_optimizer(value_model)
        if arm == "E":
            action_value_model = ScalarPredictor(seed=derive_seed("pilot", arm, "Q"))
            action_value_optimizer = new_predictor_optimizer(action_value_model)
    behavior_actor = copy.deepcopy(actor) if arm == "F" else None
    block_seed = derive_seed("pilot", arm, "block-order", 0)
    seed_manifest = hashlib.sha256(
        ",".join(str(int(row.owner_seed)) for row in examples).encode("ascii")
    ).hexdigest()
    training_rows = [
        row for row in seed_table_rows(
            ensure_seed_table(), phase="training", formal=False,
            owners=range(8_480_001, 8_480_033),
        )
        if row.get("issue84_arm") == arm
    ]
    feature_manifest = owner_feature_manifest(
        training_rows, root=training_root, package=training_package,
    )
    checkpoint_path = root / "models" / "blocks" / "pilot-block-00-step.pt"
    model_path = executor._model_path("pi13")
    resume = None
    if checkpoint_path.is_file():
        resume = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
        if (
            resume.get("arm") != arm
            or resume.get("owner_training_feature_manifest_sha256") != feature_manifest
            or resume.get("source_model_digest") != source_digest
            or resume.get("package_commit") != package
        ):
            raise RuntimeError("pilot actor checkpoint is bound to another arm/source/owner block")

    def save_step(step, trained, current_optimizer, rng_state, order, diagnostics):
        value = {
            "schema_id": "issue84-actor-step-checkpoint-v1",
            "arm": arm,
            "block_index": 0,
            "completed_steps": int(step),
            "block_order_seed": block_seed,
            "source_model_digest": source_digest,
            "owner_seed_sha256": seed_manifest,
            "owner_training_feature_manifest_sha256": feature_manifest,
            "actor_state_dict": trained.state_dict(),
            "optimizer_state_dict": current_optimizer.state_dict(),
            "python_rng_state": rng_state,
            "owner_order": [int(index) for index in order],
            "diagnostics": list(diagnostics),
            "value_model_state_dict": None if value_model is None else value_model.state_dict(),
            "value_optimizer_state_dict": None if value_optimizer is None else value_optimizer.state_dict(),
            "action_value_model_state_dict": None if action_value_model is None else action_value_model.state_dict(),
            "action_value_optimizer_state_dict": None if action_value_optimizer is None else action_value_optimizer.state_dict(),
            "behavior_actor_state_dict": None if behavior_actor is None else behavior_actor.state_dict(),
            "source_lock_sha256": file_sha256(SOURCE_LOCK_PATH),
            "package_commit": package,
        }
        executor._atomic_torch_save(checkpoint_path, value)

    started = time.monotonic()
    result = train_actor_block(
        actor, optimizer, outcomes, arm=arm, block_index=0,
        block_order_seed=block_seed, value_model=value_model,
        action_value_model=action_value_model, behavior_actor=behavior_actor,
        pilot=True, resume_state=resume, step_callback=save_step,
    )
    model_record = executor._save_model_once(actor, model_path, model_digest)
    receipt = {
        "schema_id": "issue84-pilot-actor-update-v1",
        "status": "SUCCEEDED",
        "arm": arm,
        "block_index": 0,
        "owner_count": len(outcomes),
        "owner_seed_sha256": seed_manifest,
        "owner_training_feature_manifest_sha256": feature_manifest,
        "source_model_digest": source_digest,
        "result_model_digest": model_digest(actor),
        "model_checkpoint": model_record,
        "model_checkpoint_sha256": file_sha256(model_path),
        "optimizer_checkpoint_sha256": file_sha256(checkpoint_path),
        "inherited_optimizer_steps": inherited_steps,
        "result_optimizer_steps": optimizer_parameter_steps(optimizer),
        "completed_steps": int(result["completed_steps"]),
        "block_order_seed": block_seed,
        "resume_completed_steps": 0 if resume is None else int(resume["completed_steps"]),
        "diagnostics": result["diagnostics"],
        "elapsed_seconds": time.monotonic() - started,
        "pilot_nonformal": True,
    }
    write_json_once(receipt_path, receipt)
    return receipt


def fit_pilot_predictors(arm: str, rows: list[dict[str, Any]], *, root: Path, executor, package: str) -> dict[str, Any] | None:
    if arm not in {"D", "E", "F"}:
        return None
    receipt_path = root / "diagnostics" / "pilot-predictor-fit.json"
    if receipt_path.is_file():
        saved_receipt = read_json(receipt_path)
        model_path = root / "models" / "predictors" / "pilot-after-block-00.pt"
        if (
            saved_receipt.get("status") != "SUCCEEDED"
            or saved_receipt.get("arm") != arm
            or saved_receipt.get("package_commit") != package
            or saved_receipt.get("source_lock_sha256") != file_sha256(SOURCE_LOCK_PATH)
            or not model_path.is_file()
            or saved_receipt.get("checkpoint_sha256") != file_sha256(model_path)
        ):
            raise RuntimeError(f"saved pilot predictor receipt failed source/checkpoint validation for arm {arm}")
        return saved_receipt
    import torch
    from gradients import ScalarPredictor
    from predictors import (
        build_predictor_cache,
        discard_predictor_cache,
        fit_cached_prior_block_predictors,
        new_predictor_optimizer,
    )

    value_model = ScalarPredictor(seed=derive_seed("pilot", arm, "V"))
    value_optimizer = new_predictor_optimizer(value_model)
    action_value_model = action_value_optimizer = None
    if arm == "E":
        action_value_model = ScalarPredictor(seed=derive_seed("pilot", arm, "Q"))
        action_value_optimizer = new_predictor_optimizer(action_value_model)
    cache_started = time.monotonic()
    binding = {
        "arm": arm,
        "current_block_index": 1,
        "prior_block_indices": [0],
        "source_lock_sha256": file_sha256(SOURCE_LOCK_PATH),
        "package_commit": package,
        "prior_block_feature_manifest_sha256": [owner_feature_manifest(rows, root=root, package=package)],
    }

    def history():
        for row in sorted(rows, key=lambda item: int(item["owner_seed"])):
            _, owner_outcomes = load_training_outcomes(
                [row], root=training_root, executor=executor, package=training_package,
            )
            if len(owner_outcomes) != 1:
                raise RuntimeError(f"pilot arm {arm} owner did not produce exactly one training outcome")
            yield 0, owner_outcomes

    cache_path = root / "predictor-cache" / f"pilot-{arm}"
    cache_manifest = build_predictor_cache(history(), cache_path=cache_path, binding=binding)
    cache_elapsed = time.monotonic() - cache_started
    fit = fit_cached_prior_block_predictors(
        arm=arm, current_block_index=1, cache_path=cache_path, cache_manifest=cache_manifest,
        value_model=value_model, value_optimizer=value_optimizer,
        action_value_model=action_value_model, action_value_optimizer=action_value_optimizer,
    )
    fit["feature_cache_build_elapsed_seconds"] = cache_elapsed
    expected_examples = int(cache_manifest["examples"])
    if expected_examples <= 0 or int(fit["value"]["examples"]) != expected_examples:
        raise RuntimeError(f"pilot V fit did not cover all cached natural-game decisions for arm {arm}")
    if arm == "E" and int(fit["action_value"]["examples"]) != expected_examples:
        raise RuntimeError("pilot E Q fit did not cover only its complete chosen-action decision rows")
    model_path = root / "models" / "predictors" / "pilot-after-block-00.pt"
    state = {
        "schema_id": "issue84-pilot-predictor-checkpoint-v1",
        "arm": arm,
        "completed_prior_blocks": [0],
        "current_block_index": 1,
        "value_model_state_dict": value_model.state_dict(),
        "value_optimizer_state_dict": value_optimizer.state_dict(),
        "action_value_model_state_dict": None if action_value_model is None else action_value_model.state_dict(),
        "action_value_optimizer_state_dict": None if action_value_optimizer is None else action_value_optimizer.state_dict(),
        "fit_receipt": fit,
    }
    if model_path.is_file():
        saved = torch.load(model_path, map_location="cpu", weights_only=False)
        def stable_timing(value):
            if isinstance(value, dict):
                return {key: stable_timing(item) for key, item in value.items()
                        if key not in {"elapsed_seconds", "feature_cache_build_elapsed_seconds"}}
            if isinstance(value, list):
                return [stable_timing(item) for item in value]
            return value

        stable_saved = stable_timing(saved.get("fit_receipt", {}))
        stable_fit = stable_timing(fit)
        if stable_saved != stable_fit:
            raise FileExistsError("existing pilot predictor checkpoint differs from deterministic refit")
        fit = saved["fit_receipt"]
    else:
        executor._atomic_torch_save(model_path, state)
    discard_predictor_cache(cache_path, binding=binding)
    receipt = {
        "schema_id": "issue84-pilot-predictor-fit-v1",
        "status": "SUCCEEDED",
        "arm": arm,
        "package_commit": package,
        "source_lock_sha256": binding["source_lock_sha256"],
        "completed_prior_blocks": [0],
        "current_block_index": 1,
        "q_uses_only_actual_chosen_actions": arm == "E",
        "fit": fit,
        "checkpoint_sha256": file_sha256(model_path),
    }
    write_json_once(receipt_path, receipt)
    return receipt


def pilot_weight_receipts(pilot_root: Path) -> list[dict[str, str]]:
    files = []
    for arm in ARMS:
        root = pilot_root / arm
        for relative in (
            Path("models/pi13.pt"),
            Path("models/blocks/pilot-block-00-step.pt"),
            Path("models/predictors/pilot-after-block-00.pt"),
        ):
            path = root / relative
            if path.is_file():
                files.append({"arm": arm, "path": relative.as_posix(), "sha256": file_sha256(path)})
    return files


def discard_pilot_weights(
    pilot_root: Path, receipts: Sequence[Mapping[str, str]], closeout_path: Path,
) -> None:
    """Discard only pilot weights named in the immutable, hash-verified closeout."""
    if not closeout_path.is_file():
        raise FileNotFoundError("pilot weight closeout receipt is required before cleanup")
    closeout = read_json(closeout_path)
    if closeout.get("weights") != list(receipts):
        raise RuntimeError("pilot weight cleanup list differs from its immutable closeout receipt")
    for record in receipts:
        if record.get("arm") not in ARMS:
            raise ValueError("pilot weight receipt names an unknown arm")
        path = pilot_root / str(record["arm"]) / str(record["path"])
        if path.exists():
            if file_sha256(path) != record["sha256"]:
                raise FileExistsError(f"pilot weight changed after its receipt: {path}")
            path.unlink()
    for arm in ARMS:
        model_root = pilot_root / arm / "models"
        if model_root.is_dir():
            for path in sorted(model_root.rglob("*"), reverse=True):
                if path.is_dir():
                    path.rmdir()
            if not any(model_root.iterdir()):
                model_root.rmdir()


def _pilot_c_attempt_lineage(
    *, package: str, pilot_root: Path, rows: Sequence[Mapping[str, Any]],
    seed_table: Mapping[str, Any], current_lock: Mapping[str, Any],
) -> dict[str, Any] | None:
    if package == PILOT_C_INTERRUPTED_ATTEMPT_PACKAGE:
        return None
    selected = [row for row in rows if row.get("unit_id") == "TRAIN:8480022:C"]
    if len(selected) != 1:
        raise RuntimeError("pilot C attempt lineage requires exactly the frozen TRAIN-8480022-C row")
    row = dict(selected[0])
    row_sha256 = sha256(canonical(row))
    if row_sha256 != PILOT_C_INTERRUPTED_ATTEMPT_ROW_SHA256:
        raise RuntimeError("pilot C interrupted attempts do not bind the registered frozen seed row")

    backend = configure_backend()
    source_package = PILOT_C_INTERRUPTED_ATTEMPT_PACKAGE
    source_lock_sha256 = PILOT_C_INTERRUPTED_ATTEMPT_LOCK_SHA256
    source_lock_path = backend.ARTIFACTS / "source-lock-history" / (
        f"{source_package}-{source_lock_sha256}.json"
    )
    if not source_lock_path.is_file() or file_sha256(source_lock_path) != source_lock_sha256:
        raise RuntimeError("pilot C attempt source-lock archive is missing or changed")
    _verify_predecessor_lock_compatibility(
        read_json(source_lock_path), current_lock, package=source_package,
    )
    predecessor_table_path = seed_table_path(source_package)
    if not predecessor_table_path.is_file():
        raise RuntimeError("pilot C interrupted-attempt seed table is missing")
    predecessor_table = read_json(predecessor_table_path)
    if (
        predecessor_table.get("source_lock_sha256") != source_lock_sha256
        or predecessor_table.get("rows") != seed_table.get("rows")
        or predecessor_table.get("rows_sha256") != sha256(canonical(predecessor_table.get("rows")))
        or seed_table.get("rows_sha256") != sha256(canonical(seed_table.get("rows")))
    ):
        raise RuntimeError("pilot C interrupted-attempt seed schedule differs from the frozen current schedule")

    source_root = (backend.ARTIFACTS / "pilot" / source_package / "C").resolve(strict=True)
    unit_root = source_root / "units" / "TRAIN-8480022-C"
    attempt_dirs = sorted(unit_root.glob("attempt-*"))
    expected_names = [f"attempt-{index:04d}" for index in range(1, PILOT_C_INTERRUPTED_ATTEMPT_COUNT + 1)]
    if [path.name for path in attempt_dirs] != expected_names:
        raise RuntimeError("pilot C interrupted-attempt lineage is not the exact registered 18+1 sequence")
    records = []
    for number, attempt in enumerate(attempt_dirs, start=1):
        try:
            attempt.resolve(strict=True).relative_to(unit_root.resolve(strict=True))
        except ValueError as exc:
            raise RuntimeError("pilot C attempt path escapes its frozen source root") from exc
        start_path = attempt / "started.json"
        job_path = attempt / "worker-job.json"
        result_path = attempt / "result.json"
        finish_path = attempt / "finished.json"
        if not start_path.is_file() or not job_path.is_file():
            raise RuntimeError(f"pilot C attempt {number} lacks its start or worker-job receipt")
        started = read_json(start_path)
        job = read_json(job_path)
        expected_run_id = f"TRAIN:8480022:C-attempt-{number:04d}"
        if (
            started.get("schema_id") != "issue79-owner-attempt-start-v1"
            or started.get("unit_id") != row["unit_id"]
            or started.get("owner_seed") != row["owner_seed"]
            or started.get("run_id") != expected_run_id
            or started.get("package_commit") != source_package
            or started.get("seed_row_sha256") != row_sha256
            or job.get("schema_id") != "issue79-owner-worker-job-v1"
            or job.get("unit_id") != row["unit_id"]
            or job.get("run_id") != expected_run_id
            or job.get("package_commit") != source_package
            or job.get("source_lock_sha256") != source_lock_sha256
            or job.get("row") != row
        ):
            raise RuntimeError(f"pilot C attempt {number} is not bound to the original frozen owner row/source")
        record = {
            "attempt_number": number,
            "started_sha256": file_sha256(start_path),
            "worker_job_sha256": file_sha256(job_path),
        }
        if number < PILOT_C_INTERRUPTED_ATTEMPT_COUNT:
            if not result_path.is_file() or not finish_path.is_file():
                raise RuntimeError(f"pilot C failed attempt {number} lacks its immutable result/finish pair")
            result = read_json(result_path)
            finished = read_json(finish_path)
            if any(key in result for key in ("heart_victory", "terminal_floor", "terminal_outcome")):
                raise RuntimeError("pilot C failed-attempt receipt unexpectedly contains terminal outcomes")
            if (
                result.get("schema_id") != "issue79-owner-attempt-result-v1"
                or result.get("status") != "FAILED"
                or result.get("unit_id") != row["unit_id"]
                or result.get("owner_seed") != row["owner_seed"]
                or result.get("run_id") != expected_run_id
                or result.get("failure_boundary") != "StudyFailure"
                or result.get("failure_detail") != PILOT_C_COVERAGE_FAILURE_DETAIL
                or finished.get("schema_id") != "issue79-owner-attempt-finish-v1"
                or finished.get("unit_id") != row["unit_id"]
                or finished.get("run_id") != expected_run_id
                or finished.get("status") != "FAILED"
                or finished.get("result_sha256") != file_sha256(result_path)
            ):
                raise RuntimeError(f"pilot C failed attempt {number} does not match the witnessed coverage failure")
            record.update({
                "status": "FAILED_DETERMINISTIC_COVERAGE",
                "result_sha256": file_sha256(result_path),
                "finished_sha256": file_sha256(finish_path),
            })
        else:
            if result_path.exists() or finish_path.exists():
                raise RuntimeError("pilot C attempt 19 is no longer the recorded incomplete attempt")
            record.update({
                "status": "INCOMPLETE_UNKNOWN_TAIL",
                "result_sha256": None,
                "finished_sha256": None,
            })
        records.append(record)

    payload = {
        "schema_id": "issue84-prior-attempt-lineage-v1",
        "package_commit": package,
        "source_lock_sha256": file_sha256(SOURCE_LOCK_PATH),
        "source_package_commit": source_package,
        "source_lock_archive_sha256": source_lock_sha256,
        "source_artifact_root": source_root.relative_to(backend.ARTIFACTS).as_posix(),
        "unit_id": row["unit_id"],
        "seed_row_sha256": row_sha256,
        "prior_attempt_count": PILOT_C_INTERRUPTED_ATTEMPT_COUNT,
        "attempts": records,
        "attempt_19_active_wall_tail": "UNKNOWN",
        "resume_attempt_number": PILOT_C_INTERRUPTED_ATTEMPT_COUNT + 1,
    }
    manifest = {**payload, "manifest_sha256": sha256(canonical(payload))}
    path = pilot_root / "C" / "execution-lineage" / "TRAIN-8480022-C.json"
    write_json_once(path, manifest)
    if read_json(path) != manifest:
        raise RuntimeError("pilot C prior-attempt lineage changed after persistence")
    return manifest


def run_pilot() -> dict[str, Any]:
    source = verify_source_lock(require_clean_worktree=True)
    backend = configure_backend()
    os.environ["STS_LIGHTSPEED_SOURCE_DIR"] = STS_SOURCE_WSL
    os.environ["STSRL_SOURCE_DIR"] = STSRL_SOURCE_WSL
    package = package_commit()
    save_preflight_receipt(verify_preflight())
    table = ensure_seed_table()
    pilot_root = backend.ARTIFACTS / "pilot" / package
    pilot_root.mkdir(parents=True, exist_ok=True)
    report_path = pilot_root / "pilot-report.json"
    closeout_path = pilot_root / "pilot-closeout-pending.json"
    if report_path.is_file():
        report = read_json(report_path)
        if report.get("status") != "SUCCEEDED" or report.get("source_lock_sha256") != source["sha256"]:
            raise RuntimeError("existing pilot report does not match this locked source")
        forecast_path = pilot_root / "task-wall-forecast.json"
        if not forecast_path.is_file() or file_sha256(forecast_path) != report.get("task_wall_forecast_sha256"):
            raise RuntimeError("completed pilot report lacks its immutable matching whole-task forecast")
        discard_pilot_weights(pilot_root, report.get("discarded_pilot_weights", []), closeout_path)
        return report

    progress_path = pilot_root / "pilot-progress.json"
    progress = read_json(progress_path) if progress_path.is_file() else {
        "schema_id": "issue84-pilot-progress-v1",
        "package_commit": package,
        "source_lock_sha256": source["sha256"],
        "training_runs": {},
        "training_decision_counts": {},
        "actor_updates": {},
        "actor_update_carry_forward_inputs": {},
        "execution_lineage_inputs": {},
        "predictor_fits": {},
        "evaluation_runs": {},
    }
    if progress.get("package_commit") != package or progress.get("source_lock_sha256") != source["sha256"]:
        raise RuntimeError("pilot recovery state is bound to another source lock")
    progress.setdefault("training_decision_counts", {})
    progress.setdefault("carry_forward_inputs", {})
    progress.setdefault("actor_update_carry_forward_inputs", {})
    progress.setdefault("execution_lineage_inputs", {})
    clock = backend.ActiveClock("issue84-task", None)
    clock_start = clock.elapsed_before
    if "clock_elapsed_before" not in progress:
        progress["clock_elapsed_before"] = clock.elapsed_before
        backend.atomic_json(progress_path, progress)
    try:
        for arm in ARMS:
            root = pilot_root / arm
            rows = [
                row for row in seed_table_rows(
                    table, phase="training", formal=False,
                    owners=range(8_480_001, 8_480_033),
                )
                if row.get("issue84_arm") == arm
            ]
            if len(rows) != 32:
                raise RuntimeError(f"pilot arm {arm} has {len(rows)} training owners instead of 32")
            training_root, training_package, carried_summary, carry_manifest = _pilot_training_resume(
                arm=arm, package=package, pilot_root=pilot_root, rows=rows,
                seed_table=table, current_lock=source["lock"],
            )
            if carry_manifest is not None:
                recorded = progress["carry_forward_inputs"].get(arm)
                if recorded is not None and recorded != carry_manifest:
                    raise RuntimeError(f"pilot carry-forward evidence changed for arm {arm}")
                if recorded is None:
                    progress["carry_forward_inputs"][arm] = carry_manifest
                if arm in progress["training_runs"]:
                    saved_summary = progress["training_runs"][arm]
                    if (
                        saved_summary.get("status") != "SUCCEEDED"
                        or saved_summary.get("completed") != 32
                        or saved_summary.get("outcomes") != carried_summary["outcomes"]
                        or saved_summary.get("evidence_origin") != carried_summary["evidence_origin"]
                    ):
                        raise RuntimeError(f"pilot progress differs from carried-forward receipts for arm {arm}")
                else:
                    progress["active_stage"] = "carry-forward-training-evidence"
                    progress["active_arm"] = arm
                    progress["training_runs"][arm] = carried_summary
                    backend.atomic_json(progress_path, progress)
            elif arm in progress["carry_forward_inputs"]:
                raise RuntimeError(f"pilot progress has unexpected carry-forward provenance for arm {arm}")
            if arm == "C":
                lineage = _pilot_c_attempt_lineage(
                    package=package, pilot_root=pilot_root, rows=rows,
                    seed_table=table, current_lock=source["lock"],
                )
                if lineage is not None:
                    recorded = progress["execution_lineage_inputs"].get(arm)
                    if recorded is not None and recorded != lineage:
                        raise RuntimeError("pilot C prior-attempt lineage changed during continuation")
                    if recorded is None:
                        progress["execution_lineage_inputs"][arm] = lineage
                        backend.atomic_json(progress_path, progress)
                elif arm in progress["execution_lineage_inputs"]:
                    raise RuntimeError("pilot progress has unexpected C prior-attempt lineage")
            if arm not in progress["training_runs"]:
                progress["active_stage"] = "training-games"
                progress["active_arm"] = arm
                backend.atomic_json(progress_path, progress)
                result = backend.run_units(
                    rows, root=root, pilot=True, clock=clock, package_commit=package,
                )
                summary = run_summary(result, rows=rows, root=root, package=package)
                if summary["status"] != "SUCCEEDED" or summary["completed"] != 32:
                    raise RuntimeError(f"pilot training games did not complete for arm {arm}: {summary['status']}")
                progress["training_runs"][arm] = summary
                backend.atomic_json(progress_path, progress)

            actor_update_carry = _pilot_actor_update_resume(
                arm=arm, package=package, pilot_root=pilot_root,
                training_root=training_root, training_package=training_package,
                training_carry_manifest=carry_manifest,
                training_summary=progress["training_runs"][arm],
                current_lock=source["lock"],
            )
            if actor_update_carry is not None:
                carried_update, carry_manifest = actor_update_carry
                recorded = progress["actor_update_carry_forward_inputs"].get(arm)
                if recorded is not None and recorded != carry_manifest:
                    raise RuntimeError(f"pilot actor-update carry-forward changed for arm {arm}")
                if recorded is None:
                    progress["actor_update_carry_forward_inputs"][arm] = carry_manifest
                saved_update = progress["actor_updates"].get(arm)
                if saved_update is not None and saved_update != carried_update:
                    raise RuntimeError(f"pilot actor-update progress differs from retained receipt for arm {arm}")
                if saved_update is None:
                    progress["actor_updates"][arm] = carried_update
                backend.atomic_json(progress_path, progress)
            elif arm in progress["actor_update_carry_forward_inputs"]:
                raise RuntimeError(f"pilot progress has unexpected actor-update carry-forward for arm {arm}")

            algorithm, runtime, model, learning, executor = load_modules(root, pilot=True)
            if actor_update_carry is not None:
                decision_count = int(PILOT_ACTOR_UPDATE_CARRY_FORWARD[arm]["decision_count"])
            else:
                with _predecessor_source_lock(training_package):
                    examples, outcomes = load_training_outcomes(
                        rows, root=training_root, executor=executor, package=training_package,
                    )
                decision_count = sum(len(outcome.decisions) for outcome in outcomes)
            if arm in progress["training_decision_counts"]:
                if int(progress["training_decision_counts"][arm]) != decision_count:
                    raise RuntimeError(f"pilot training feature count changed for arm {arm}")
            else:
                progress["training_decision_counts"][arm] = decision_count
                backend.atomic_json(progress_path, progress)
            if arm not in progress["actor_updates"]:
                if actor_update_carry is not None:
                    raise RuntimeError(f"pilot actor update carry-forward was not recorded for arm {arm}")
                progress["active_stage"] = "actor-update"
                progress["active_arm"] = arm
                backend.atomic_json(progress_path, progress)
                with _predecessor_source_lock(training_package):
                    update = pilot_actor_update(
                        arm, examples, outcomes, root=root, executor=executor, learning=learning,
                        training_root=training_root, training_package=training_package,
                    )
                progress["actor_updates"][arm] = {
                    "status": update["status"],
                    "completed_steps": update["completed_steps"],
                    "elapsed_seconds": update["elapsed_seconds"],
                    "model_digest": update["result_model_digest"],
                    "optimizer_steps": update["result_optimizer_steps"],
                    "checkpoint_sha256": update["optimizer_checkpoint_sha256"],
                }
                backend.atomic_json(progress_path, progress)
            if arm in {"D", "E", "F"} and arm not in progress["predictor_fits"]:
                progress["active_stage"] = "lagged-predictor-fit"
                progress["active_arm"] = arm
                backend.atomic_json(progress_path, progress)
                fit = fit_pilot_predictors(arm, rows, root=root, executor=executor, package=package)
                progress["predictor_fits"][arm] = {
                    "status": fit["status"],
                    "fit": fit["fit"],
                    "checkpoint_sha256": fit["checkpoint_sha256"],
                }
                backend.atomic_json(progress_path, progress)

        for group in ("PARENT", *ARMS):
            root = (pilot_root / "PARENT") if group == "PARENT" else (pilot_root / group)
            rows = [
                row for row in seed_table_rows(
                    table, phase="evaluation", formal=False,
                    owners=range(8_490_001, 8_490_009),
                )
                if row["arm"] == group
            ]
            if len(rows) != 8:
                raise RuntimeError(f"pilot evaluation {group} has {len(rows)} owners instead of 8")
            if group not in progress["evaluation_runs"]:
                progress["active_stage"] = "evaluation-games"
                progress["active_arm"] = group
                backend.atomic_json(progress_path, progress)
                result = backend.run_units(
                    rows, root=root, pilot=True, clock=clock, package_commit=package,
                )
                summary = run_summary(result, rows=rows, root=root, package=package)
                if summary["status"] != "SUCCEEDED" or summary["completed"] != 8:
                    raise RuntimeError(f"pilot evaluation games did not complete for {group}: {summary['status']}")
                progress["evaluation_runs"][group] = summary
                backend.atomic_json(progress_path, progress)
        clock.set_processes(set())
        task_wall = clock.elapsed()
    finally:
        clock.close()

    if closeout_path.is_file():
        pending_closeout = read_json(closeout_path)
        if (
            pending_closeout.get("schema_id") != "issue84-pilot-weight-discard-v1"
            or pending_closeout.get("package_commit") != package
            or pending_closeout.get("source_lock_sha256") != source["sha256"]
        ):
            raise RuntimeError("existing pilot weight closeout belongs to a different source lock")
        discarded = pending_closeout.get("weights")
        if not isinstance(discarded, list):
            raise RuntimeError("pilot closeout receipt has no exact weight inventory")
    else:
        discarded = pilot_weight_receipts(pilot_root)
        pending_closeout = {
            "schema_id": "issue84-pilot-weight-discard-v1",
            "package_commit": package,
            "source_lock_sha256": source["sha256"],
            "weights": discarded,
        }
        backend.write_json_once(closeout_path, pending_closeout)
    discard_pilot_weights(pilot_root, discarded, closeout_path)
    report = {
        "schema_id": "issue84-pilot-report-v1",
        "status": "SUCCEEDED",
        "claim": "execution-path and feasibility pilot only; pilot outcomes are not efficacy evidence",
        "pilot_training_games": sum(int(item["completed"]) for item in progress["training_runs"].values()),
        "pilot_evaluation_games": sum(int(item["completed"]) for item in progress["evaluation_runs"].values()),
        "training_runs": progress["training_runs"],
        "evaluation_runs": progress["evaluation_runs"],
        "actor_updates": progress["actor_updates"],
        "actor_update_carry_forward_inputs": progress["actor_update_carry_forward_inputs"],
        "execution_lineage_inputs": progress["execution_lineage_inputs"],
        "lagged_predictor_fits": progress["predictor_fits"],
        "pilot_weights_discarded": True,
        "discarded_pilot_weights": discarded,
        "discarded_pilot_weight_count": len(discarded),
        "training_owner_range_per_arm": [8_480_001, 8_480_032],
        "evaluation_owner_range": [8_490_001, 8_490_008],
        "validation_or_test_started": False,
        "source_lock_sha256": source["sha256"],
        "seed_table_sha256": file_sha256(backend.SEED_TABLE),
        "native_source_commit": NATIVE_COMMIT,
        "stsr_source_commit": STSRL_COMMIT,
        "package_commit": package,
        "actual_task_cumulative_active_wall_seconds": task_wall,
        "pilot_incremental_active_wall_seconds": max(0.0, task_wall - clock_start),
        "closeout_receipt_sha256": file_sha256(closeout_path),
        "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    forecast_path = pilot_root / "task-wall-forecast.json"
    if forecast_path.is_file():
        forecast = read_json(forecast_path)
        if forecast.get("source_lock_sha256") != source["sha256"] or forecast.get("package_commit") != package:
            raise RuntimeError("existing pilot wall forecast belongs to another source-locked run")
    else:
        forecast = make_task_wall_forecast(
            progress, task_wall=task_wall, source_lock_sha256=source["sha256"], package=package,
        )
        backend.write_json_once(forecast_path, forecast)
    report["task_wall_forecast_path"] = forecast_path.relative_to(pilot_root).as_posix()
    report["task_wall_forecast_sha256"] = file_sha256(forecast_path)
    report["estimated_all_in_active_wall_seconds"] = forecast["estimated_all_in_active_wall_seconds"]
    report["one_task_active_wall_allowance_seconds"] = forecast["one_task_active_wall_allowance_seconds"]
    backend.write_json_once(report_path, report)
    return report


def make_task_wall_forecast(
    progress: Mapping[str, Any], *, task_wall: float, source_lock_sha256: str, package: str,
) -> dict[str, Any]:
    """Freeze one full-workload forecast and 1.75x global allowance after the real pilot."""
    training_wall = sum(float(item["elapsed_seconds"]) for item in progress["training_runs"].values())
    evaluation_wall = sum(float(item["elapsed_seconds"]) for item in progress["evaluation_runs"].values())
    actor_wall = sum(float(item["elapsed_seconds"]) for item in progress["actor_updates"].values())
    pilot_predictor_rows = 0
    predictor_wall = 0.0
    predictor_cache_build_wall = 0.0
    for item in progress["predictor_fits"].values():
        fit = item["fit"]
        predictor_cache_build_wall += float(fit.get("feature_cache_build_elapsed_seconds", 0.0))
        for key in ("value", "action_value"):
            stats = fit.get(key)
            if stats:
                pilot_predictor_rows += int(stats["examples"])
                predictor_wall += float(stats.get("elapsed_seconds", 0.0))
    predictor_wall += predictor_cache_build_wall
    if len(progress["training_runs"]) != 6 or len(progress["evaluation_runs"]) != 7:
        raise RuntimeError("pilot workload is incomplete; refusing to forecast formal cost")
    if len(progress["actor_updates"]) != 6 or len(progress["predictor_fits"]) != 3:
        raise RuntimeError("pilot model-update coverage is incomplete; refusing to forecast formal cost")
    if pilot_predictor_rows <= 0 or actor_wall <= 0 or task_wall <= 0:
        raise RuntimeError("pilot timing/feature measurements are missing or nonpositive")
    pilot_game_count = 248
    formal_game_count = 15_744
    formal_actor_update_count = 96
    pilot_actor_update_count = 6
    formal_predictor_rows = round(T_REF * 15_360 * 4)
    game_factor = formal_game_count / pilot_game_count
    game_forecast = (training_wall + evaluation_wall) * game_factor
    actor_forecast = actor_wall * (formal_actor_update_count / pilot_actor_update_count)
    predictor_forecast = predictor_wall * (formal_predictor_rows / pilot_predictor_rows)
    measured_pilot_stages = training_wall + evaluation_wall + actor_wall + predictor_wall
    pilot_unattributed = max(0.0, task_wall - measured_pilot_stages)
    unmodeled_formal_overhead = pilot_unattributed * game_factor
    formal_forecast = game_forecast + actor_forecast + predictor_forecast + unmodeled_formal_overhead
    # Add 25% to the measured component forecast for full-cohort data aggregation,
    # verification, report generation, and retry/closeout work not represented by 248 games.
    estimated_all_in = task_wall + formal_forecast * 1.25
    return {
        "schema_id": "issue84-task-wall-forecast-v1",
        "status": "PILOT_MEASURED_FORMAL_ESTIMATE",
        "source_lock_sha256": source_lock_sha256,
        "package_commit": package,
        "pilot_games": pilot_game_count,
        "formal_games": formal_game_count,
        "pilot_task_cumulative_active_wall_seconds": task_wall,
        "pilot_incremental_active_wall_seconds": max(0.0, task_wall - float(progress.get("clock_elapsed_before", 0.0))),
        "pilot_game_stage_wall_seconds": training_wall + evaluation_wall,
        "pilot_actor_update_wall_seconds": actor_wall,
        "pilot_predictor_fit_wall_seconds": predictor_wall,
        "pilot_predictor_cache_build_wall_seconds": predictor_cache_build_wall,
        "pilot_predictor_training_rows": pilot_predictor_rows,
        "formal_predictor_training_rows_estimated_from_t_ref": formal_predictor_rows,
        "formal_game_wall_seconds_estimate": game_forecast,
        "formal_actor_update_wall_seconds_estimate": actor_forecast,
        "formal_predictor_fit_wall_seconds_estimate": predictor_forecast,
        "formal_unattributed_overhead_seconds_estimate": unmodeled_formal_overhead,
        "formal_component_wall_seconds_estimate": formal_forecast,
        "formal_estimate_margin_factor": 1.25,
        "estimated_all_in_active_wall_seconds": estimated_all_in,
        "one_task_active_wall_allowance_factor": 1.75,
        "one_task_active_wall_allowance_seconds": estimated_all_in * 1.75,
        "forecast_method": (
            "scale measured concurrent game wall by 15744/248; actor update time by 96/6; "
            "predictor cache-build plus fit time by registered 15360 prior-owner presentations "
            "per predictor and T_REF decisions, relative to actual pilot feature rows; scale unattributed "
            "pilot wall by game count; add 25% formal allowance for cohort aggregation, "
            "verification, reporting, retries, and closeout"
        ),
        "concurrency": CONCURRENCY,
        "pilot_weight_discarded": True,
        "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--write-source-lock", action="store_true")
    modes.add_argument("--refresh-source-lock", action="store_true")
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--pilot", action="store_true")
    modes.add_argument("--formal", action="store_true")
    modes.add_argument("--worker", type=Path)
    args = parser.parse_args(argv)
    if args.write_source_lock:
        result = write_source_lock()
    elif args.refresh_source_lock:
        result = refresh_source_lock()
    elif args.preflight:
        result = verify_preflight()
        result = save_preflight_receipt(result)
    elif args.pilot:
        result = run_pilot()
    elif args.formal:
        from formal import run_formal
        result = run_formal()
    else:
        job = read_json(args.worker)
        if package_commit() != job.get("package_commit"):
            raise RuntimeError("worker environment commit differs from its immutable job receipt")
        backend = configure_backend()
        lock_sha = file_sha256(SOURCE_LOCK_PATH)
        if job.get("source_lock_sha256") != lock_sha:
            raise RuntimeError("worker job source-lock hash differs from the committed source lock")
        if job.get("package_commit") != backend.source_commit():
            raise RuntimeError("worker job commit differs from the current source-locked runner commit")
        return int(backend.worker_main(args.worker))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
