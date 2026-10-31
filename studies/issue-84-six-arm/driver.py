"""Source-locked runner for the registered Issue 84 six-arm study.

Game mechanics, Search-v2, legal-action projection, and native execution remain
owned by the pinned sts_lightspeed/STSRL runtime. This study supplies only the
registered policy mapping, loss, predictor fitting, and durable orchestration.
"""
from __future__ import annotations

import argparse
import copy
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

sys.path.insert(0, str(STUDY))


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
    observed: dict[str, str] = {}
    for relative, record in manifest["files"].items():
        path = UPSTREAM / relative
        digest = file_sha256(path)
        if digest != record["sha256"]:
            raise RuntimeError(f"vendored upstream file changed: {relative}")
        observed[relative] = digest
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
    module.SEED_TABLE = host_path(DATA_ROOT_WSL) / "seed-table.json"
    module.SOURCE_LOCK = SOURCE_LOCK_PATH
    module.PINNED_PYTHON = Path(PYTHON_WSL)
    module.PILOT_TRAINING = tuple(range(8_480_001, 8_480_033))
    module.PILOT_EVALUATION = tuple(range(8_490_001, 8_490_009))
    module.FORMAL_TRAINING = tuple(range(8_400_001, 8_402_049))
    module.FORMAL_EVALUATION = tuple(range(8_460_001, 8_460_065))
    module.CONCURRENCY = CONCURRENCY
    module.__file__ = str(STUDY / "driver.py")
    module.source_commit = package_commit
    BACKEND = module
    BACKEND_ORIGINAL_LOAD_MODULES = module.load_modules
    module.load_modules = load_modules
    return module


def load_modules(active_root: Path, *, pilot: bool):
    """Load the exact #75 API and bind it to this arm's durable data root."""
    backend = configure_backend()
    for path in (str(UPSTREAM75 / "stage1"), str(UPSTREAM75)):
        while path in sys.path:
            sys.path.remove(path)
    for name in ("algorithm", "runtime", "model", "learning", "executor", "stage1_budget"):
        sys.modules.pop(name, None)
    algorithm, runtime, model, learning, executor = BACKEND_ORIGINAL_LOAD_MODULES(
        Path(active_root), pilot=pilot,
    )
    old_model_path = executor._model_path
    old_load_model = executor._load_model

    def model_path(label: str) -> Path:
        match = re.fullmatch(r"pi(\d+)", str(label))
        if match and 12 <= int(match.group(1)) <= 28:
            if int(match.group(1)) == 12:
                return host_path(PARENT_MODELS_WSL) / "pi12.pt"
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
    runtime.verify_source_repositories(config)
    native = runtime.verify_native_module(config)
    if file_sha256(Path(native)) != NATIVE_SHA256:
        raise RuntimeError("loaded native module is not the source-locked binary")
    parent, parent_digest, parent_record = executor._load_model("pi12")
    if parent_digest != PARENT_MODEL_DIGEST or parent_record["checkpoint_sha256"] != PARENT_MODEL_SHA256:
        raise RuntimeError("#82 pi12 model failed exact hash/digest inheritance check")
    import torch
    optimizer_path = host_path(PARENT_MODELS_WSL) / "block-11-step.pt"
    optimizer_blob = torch.load(optimizer_path, map_location="cpu", weights_only=True)
    if optimizer_blob.get("completed_steps") != 64:
        raise RuntimeError("#82 optimizer checkpoint is not step 64")
    optimizer = learning._new_optimizer(parent)
    optimizer.load_state_dict(optimizer_blob["optimizer_state_dict"])
    steps = sorted(int(state["step"].item()) for state in optimizer.state.values())
    if not steps or set(steps) != {64} or len(steps) != len(tuple(parent.parameters())):
        raise RuntimeError("#82 optimizer state does not map exactly to pi12 parameters at step 64")
    return {
        "schema_id": "issue84-native-preflight-v1",
        "status": "SUCCEEDED_NO_GAMES_STARTED",
        "source_lock_sha256": source["sha256"],
        "source_commit": backend.source_commit(),
        "native_module_sha256": file_sha256(Path(native)),
        "parent_model_sha256": parent_record["checkpoint_sha256"],
        "parent_model_digest": parent_digest,
        "parent_optimizer_checkpoint_sha256": file_sha256(optimizer_path),
        "parent_optimizer_parameter_steps": steps,
        "torch_version": torch.__version__,
        "python_version": sys.version,
    }


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
            "reward": float(result["game_reward"]),
        })
    return sha256(canonical(records))


def pilot_actor_update(
    arm: str, examples: list, outcomes: list, *, root: Path, executor, learning,
) -> dict[str, Any]:
    from gradients import ScalarPredictor
    from training import optimizer_parameter_steps, train_actor_block
    from model import model_digest

    receipt_path = root / "diagnostics" / "pilot-update.json"
    if receipt_path.is_file():
        return read_json(receipt_path)
    package = package_commit()
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
    feature_manifest = owner_feature_manifest(training_rows, root=root, package=package)
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
            _, owner_outcomes = load_training_outcomes([row], root=root, executor=executor, package=package)
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
        "predictor_fits": {},
        "evaluation_runs": {},
    }
    if progress.get("package_commit") != package or progress.get("source_lock_sha256") != source["sha256"]:
        raise RuntimeError("pilot recovery state is bound to another source lock")
    progress.setdefault("training_decision_counts", {})
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
            algorithm, runtime, model, learning, executor = load_modules(root, pilot=True)
            examples, outcomes = load_training_outcomes(rows, root=root, executor=executor, package=package)
            decision_count = sum(len(outcome.decisions) for outcome in outcomes)
            if arm in progress["training_decision_counts"]:
                if int(progress["training_decision_counts"][arm]) != decision_count:
                    raise RuntimeError(f"pilot training feature count changed for arm {arm}")
            else:
                progress["training_decision_counts"][arm] = decision_count
                backend.atomic_json(progress_path, progress)
            if arm not in progress["actor_updates"]:
                progress["active_stage"] = "actor-update"
                progress["active_arm"] = arm
                backend.atomic_json(progress_path, progress)
                update = pilot_actor_update(
                    arm, examples, outcomes, root=root, executor=executor, learning=learning,
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
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--pilot", action="store_true")
    modes.add_argument("--formal", action="store_true")
    modes.add_argument("--worker", type=Path)
    args = parser.parse_args(argv)
    if args.write_source_lock:
        result = write_source_lock()
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
