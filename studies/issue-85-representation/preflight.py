"""Verify frozen Issue 85 P/A inputs and their exact source/training ancestry.

The audit is read-only with respect to existing source, checkpoint and game
evidence.  Its only write is the caller-selected report, created exclusively.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence


HERE = Path(__file__).resolve().parent
LOCK = json.loads((HERE / "source-lock.json").read_text(encoding="utf-8"))
REPO_ROOT = HERE.parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def block_update_receipt_identity(
    update: Mapping[str, Any], *, block_index: int, arm: str,
    owner_seeds: Sequence[int],
) -> dict[str, Any]:
    """Validate the receipt's declared block against its exact owner cohort."""
    owners = list(owner_seeds)
    if (
        not owners
        or any(type(owner) is not int for owner in owners)
        or owners != sorted(set(owners))
    ):
        raise ValueError("block owner seeds must be a nonempty, ascending unique integer sequence")
    expected_owner_seed_sha256 = hashlib.sha256(
        ",".join(str(owner) for owner in owners).encode("ascii")
    ).hexdigest()
    actual_block_index = update.get("block_index")
    actual_arm = update.get("arm")
    actual_owner_seed_sha256 = update.get("owner_seed_sha256")
    return {
        "expected_block_index": block_index,
        "block_index": actual_block_index,
        "block_index_matches": type(actual_block_index) is int and actual_block_index == block_index,
        "expected_arm": arm,
        "arm": actual_arm,
        "arm_matches": isinstance(actual_arm, str) and actual_arm == arm,
        "owner_seed_range": [owners[0], owners[-1]],
        "expected_owner_seed_sha256": expected_owner_seed_sha256,
        "owner_seed_sha256": actual_owner_seed_sha256,
        "owner_seed_sha256_matches": (
            isinstance(actual_owner_seed_sha256, str)
            and actual_owner_seed_sha256 == expected_owner_seed_sha256
        ),
    }


def git_bytes(repo: Path, *args: str) -> bytes:
    # The pinned Python runs under WSL, but these task/source repositories are
    # Windows worktrees whose .git files contain Windows paths. Linux Git
    # cannot resolve those pointers. When available, call Git for Windows and
    # translate the WSL mount path before invoking it.
    git_exe = shutil.which("git.exe")
    repo_arg = str(repo)
    if git_exe and repo_arg.startswith("/mnt/"):
        repo_arg = subprocess.check_output(
            ["wslpath", "-w", repo_arg], text=True
        ).strip()
    executable = git_exe or shutil.which("git")
    if not executable:
        raise RuntimeError("neither git.exe nor git is available for source verification")
    return subprocess.check_output([executable, "-C", repo_arg, *args])


def git_text(repo: Path, *args: str) -> str:
    return git_bytes(repo, *args).decode("utf-8").strip()


@contextmanager
def load_pinned_model_module(git_repo: Path, data_root: Path):
    """Load the exact Issue 84 model sources from their locked Git commit.

    Issue 85 implementation is based on current origin/main, which does not
    contain the unmerged Issue 84 task branch. Retrieve only the two pinned
    Python source files from the accepted source commit, verify their hashes,
    and use a temporary directory inside managed research storage.
    """
    source_lock = LOCK["model_source"]
    expected_commit = LOCK["baseline_A"]["issue84_final_accepted_source_commit"]
    if source_lock["commit"] != expected_commit:
        raise RuntimeError(
            "pinned model source commit differs from the accepted Issue 84 source: "
            f"expected {expected_commit}, got {source_lock['commit']}"
        )
    temp_parent = data_root / "issue85" / "preflight-tmp"
    temp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pinned-model-", dir=temp_parent) as temp_name:
        source_dir = Path(temp_name)
        for item in source_lock["files"]:
            raw = git_bytes(git_repo, "show", f"{source_lock['commit']}:{item['path']}")
            actual = hashlib.sha256(raw).hexdigest()
            if actual != item["sha256"]:
                raise RuntimeError(
                    f"pinned model source SHA mismatch for {item['path']}: "
                    f"expected {item['sha256']}, got {actual}"
                )
            (source_dir / Path(item["path"]).name).write_bytes(raw)

        model_path = source_dir / "model.py"
        spec_name = "issue85_pinned_issue84_model"
        spec = importlib.util.spec_from_file_location(spec_name, model_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load pinned Issue 84 model module: {model_path}")
        module = importlib.util.module_from_spec(spec)
        previous_model = sys.modules.get(spec_name)
        previous_algorithm = sys.modules.get("algorithm")
        previous_path = str(source_dir) in sys.path
        sys.modules.pop("algorithm", None)
        if not previous_path:
            sys.path.insert(0, str(source_dir))
        sys.modules[spec_name] = module
        try:
            spec.loader.exec_module(module)
            yield module
        finally:
            sys.modules.pop(spec_name, None)
            if previous_model is not None:
                sys.modules[spec_name] = previous_model
            if previous_algorithm is not None:
                sys.modules["algorithm"] = previous_algorithm
            else:
                sys.modules.pop("algorithm", None)
            if not previous_path:
                sys.path.remove(str(source_dir))


def model_digest(checkpoint: Path, model_module: Any) -> str:
    import torch

    runtime = LOCK["model_runtime"]
    model = model_module.ActionConditionedPolicy(
        dimension=runtime["architecture_dimension"],
        seed=runtime["initial_model_seed"],
        final_weight_std=runtime["final_weight_std"],
    ).eval()
    model.load_state_dict(torch.load(checkpoint, map_location="cpu", weights_only=True), strict=True)
    return model_module.model_digest(model)


def verify(data_root: Path, native_repo: Path, git_repo: Path) -> dict[str, Any]:
    errors: list[str] = []
    checks: dict[str, Any] = {}

    def require(condition: bool, label: str, detail: Any = None) -> None:
        if not condition:
            errors.append(f"{label}: {detail if detail is not None else 'mismatch'}")

    def locked_file(relative: str, expected_sha: str, label: str) -> Path:
        path = data_root / relative
        if not path.is_file():
            errors.append(f"{label}: missing file {path}")
            return path
        actual = sha256(path)
        require(actual == expected_sha, label, {"expected": expected_sha, "actual": actual})
        checks[label] = {"path": str(path), "sha256": actual, "bytes": path.stat().st_size}
        return path

    # Verify the exact historical encoder and the runtime artifacts that fed #84.
    native = LOCK["native"]
    encoder = locked_file(
        LOCK["legacy_encoder"]["data_path"],
        LOCK["legacy_encoder"]["sha256"],
        "issue73_encoder",
    )
    vendored_encoder = HERE / LOCK["legacy_encoder"]["local_vendored_path"]
    if vendored_encoder.is_file():
        require(
            sha256(vendored_encoder) == LOCK["legacy_encoder"]["sha256"],
            "vendored_issue73_encoder_sha256",
            sha256(vendored_encoder),
        )
    else:
        errors.append(f"vendored_issue73_encoder_sha256: missing file {vendored_encoder}")

    native_module = locked_file(
        native["compiled_module_data_path"], native["compiled_module_sha256"], "native_module"
    )
    runtime = LOCK["model_runtime"]
    python_binary = locked_file(
        runtime["python_data_path"], runtime["python_sha256"], "pinned_python_binary"
    )
    require(
        list(sys.version_info[:3]) == [3, 13, 13],
        "python_version",
        {"expected": runtime["python_version"], "actual": sys.version.split()[0]},
    )
    try:
        import numpy
        import torch

        require(numpy.__version__ == runtime["numpy_version"], "numpy_version", numpy.__version__)
        require(torch.__version__ == runtime["torch_version"], "torch_version", torch.__version__)
        require(Path(sys.executable).resolve() == python_binary.resolve(), "python_executable", sys.executable)
    except Exception as exc:  # pragma: no cover - exact pinned runtime path only
        errors.append(f"pinned_model_runtime_import: {type(exc).__name__}: {exc}")

    # Bind exact C++ source, field paths, compiled module, and STSRL source.
    native_commit = native["commit"]
    actual_native_head = git_text(native_repo, "rev-parse", "HEAD")
    require(actual_native_head == native_commit, "native_source_head", actual_native_head)
    native_blob = git_text(native_repo, "rev-parse", f"{native_commit}:{native['source_file']}")
    require(native_blob == native["source_file_git_blob_sha1"], "native_source_blob", native_blob)
    native_text = git_bytes(native_repo, "show", f"{native_commit}:{native['source_file']}").decode("utf-8")
    occurrences = native_text.count('features["target_deck_index"]')
    require(occurrences == 3, "native_target_deck_index_occurrences", occurrences)
    for field in native["target_deck_index_paths"]:
        line = field["line"]
        excerpt = "\n".join(native_text.splitlines()[line - 2 : line + 3])
        require(
            'features["target_deck_index"]' in excerpt and '"oracle_only"' in excerpt,
            f"native_target_deck_index_line_{line}",
            excerpt,
        )
    stsr_repo = data_root / LOCK["stsr_source"]["data_path"]
    stsr_head = git_text(stsr_repo, "rev-parse", "HEAD")
    require(stsr_head == LOCK["stsr_source"]["commit"], "stsr_source_head", stsr_head)
    checks["native_source"] = {
        "commit": actual_native_head,
        "file_blob_sha1": native_blob,
        "target_deck_index_occurrences": occurrences,
        "compiled_module_sha256": sha256(native_module),
        "stsr_commit": stsr_head,
    }

    # Verify P/A checkpoint bytes and strict model-state digests.
    parent = LOCK["baseline_P"]
    base = LOCK["baseline_A"]
    parent_model = locked_file(parent["checkpoint_data_path"], parent["checkpoint_sha256"], "P_checkpoint")
    parent_optimizer = locked_file(parent["optimizer_data_path"], parent["optimizer_sha256"], "P_optimizer")
    base_model = locked_file(base["checkpoint_data_path"], base["checkpoint_sha256"], "A_B1024_checkpoint")
    with load_pinned_model_module(git_repo, data_root) as model_module:
        p_digest = model_digest(parent_model, model_module)
        a_digest = model_digest(base_model, model_module)
    require(p_digest == parent["model_digest"], "P_model_digest", p_digest)
    require(a_digest == base["model_digest"], "A_model_digest", a_digest)

    # The B1024 checkpoint was produced at d7; 556 is the later final accepted
    # #84 source. Preserve and verify both instead of conflating the two locks.
    source_commit = base["issue84_training_package_commit"]
    source_lock_path = "studies/issue-84-six-arm/source-lock.json"
    producer_lock_bytes = git_bytes(git_repo, "show", f"{source_commit}:{source_lock_path}")
    producer_lock_sha = hashlib.sha256(producer_lock_bytes).hexdigest()
    require(
        producer_lock_sha == base["issue84_training_source_lock_sha256"],
        "A_training_source_lock_sha256",
        producer_lock_sha,
    )
    producer_contract_sha = hashlib.sha256(
        git_bytes(git_repo, "show", f"{source_commit}:studies/issue-84-six-arm/contract.json")
    ).hexdigest()
    require(producer_contract_sha == base["issue84_contract_sha256"], "A_training_contract_sha256", producer_contract_sha)
    accepted_lock = git_bytes(
        git_repo,
        "show",
        f"{base['issue84_final_accepted_source_commit']}:{source_lock_path}",
    )
    accepted_lock_sha = hashlib.sha256(accepted_lock).hexdigest()
    require(
        accepted_lock_sha == base["issue84_final_accepted_source_lock_sha256"],
        "issue84_final_source_lock_sha256",
        accepted_lock_sha,
    )

    b_root = data_root / "issue84" / "artifacts" / "formal" / source_commit / "B"
    expected_source_digest = p_digest
    block_summaries: list[dict[str, Any]] = []
    training_owner_start, training_owner_end = map(int, base["training_owners"])
    training_owner_count = int(base["training_owner_count"])
    block_count = int(base["block_count"])
    require(
        training_owner_count == training_owner_end - training_owner_start + 1,
        "A_training_owner_range_count",
        {"range": [training_owner_start, training_owner_end], "count": training_owner_count},
    )
    require(block_count > 0, "A_training_block_count", block_count)
    owners_per_block = training_owner_count // block_count if block_count > 0 else 0
    require(
        block_count > 0 and training_owner_count % block_count == 0,
        "A_training_owner_partition",
        {"owner_count": training_owner_count, "block_count": block_count},
    )
    require(owners_per_block == 128, "A_training_owners_per_block", owners_per_block)
    for block in range(max(0, block_count)):
        block_owner_start = training_owner_start + block * owners_per_block
        block_owner_seeds = list(range(block_owner_start, block_owner_start + owners_per_block))
        update_path = b_root / "models" / "blocks" / f"block-{block:02d}-update.json"
        if not update_path.is_file():
            errors.append(f"A_block_{block}: missing {update_path}")
            continue
        update = json.loads(update_path.read_text(encoding="utf-8"))
        require(update.get("status") == "SUCCEEDED", f"A_block_{block}_status", update.get("status"))
        identity = block_update_receipt_identity(
            update, block_index=block, arm="B", owner_seeds=block_owner_seeds,
        )
        require(identity["block_index_matches"], f"A_block_{block}_block_index", identity)
        require(identity["arm_matches"], f"A_block_{block}_arm", identity)
        require(identity["owner_seed_sha256_matches"], f"A_block_{block}_owner_seed_sha256", identity)
        require(update.get("owner_count") == 128, f"A_block_{block}_owner_count", update.get("owner_count"))
        require(update.get("completed_steps") == 64, f"A_block_{block}_completed_steps", update.get("completed_steps"))
        require(update.get("package_commit") == source_commit, f"A_block_{block}_package_commit", update.get("package_commit"))
        require(
            update.get("source_lock_sha256") == base["issue84_training_source_lock_sha256"],
            f"A_block_{block}_source_lock_sha256",
            update.get("source_lock_sha256"),
        )
        require(update.get("source_model_digest") == expected_source_digest, f"A_block_{block}_source_model_digest", update.get("source_model_digest"))
        model_record = update.get("model_checkpoint") or {}
        result_digest = update.get("result_model_digest")
        require(result_digest == model_record.get("model_digest"), f"A_block_{block}_model_digest_receipt", model_record)
        expected_source_digest = str(result_digest)
        checkpoint_path = b_root / str(model_record.get("path", ""))
        if checkpoint_path.is_file():
            actual_checkpoint_sha = sha256(checkpoint_path)
            require(actual_checkpoint_sha == model_record.get("sha256"), f"A_block_{block}_checkpoint_sha256", actual_checkpoint_sha)
        else:
            errors.append(f"A_block_{block}_checkpoint: missing {checkpoint_path}")
        block_summaries.append(
            {
                "block": block,
                "block_index": identity["block_index"],
                "arm": identity["arm"],
                "owner_seed_range": identity["owner_seed_range"],
                "owner_seed_sha256": identity["owner_seed_sha256"],
                "expected_owner_seed_sha256": identity["expected_owner_seed_sha256"],
                "status": update.get("status"),
                "owners": update.get("owner_count"),
                "updates": update.get("completed_steps"),
                "source_model_digest": update.get("source_model_digest"),
                "result_model_digest": result_digest,
                "checkpoint_sha256": model_record.get("sha256"),
            }
        )
    require(expected_source_digest == a_digest, "A_block_chain_final_digest", expected_source_digest)
    last_block_sha = block_summaries[-1].get("checkpoint_sha256") if block_summaries else None
    require(last_block_sha == base["checkpoint_sha256"], "A_block7_checkpoint_sha256", last_block_sha)

    # Audit only the registered first 1,024 B owners. The wider archive has
    # 2,048 folders; it is not used to inflate or relabel the selected baseline.
    units_root = b_root / "units"
    selected_units: list[Path] = []
    for path in units_root.iterdir():
        match = re.fullmatch(r"TRAIN-(\d+)-B", path.name)
        if match and 8400001 <= int(match.group(1)) <= 8401024:
            selected_units.append(path)
    selected_units.sort(key=lambda path: int(re.search(r"\d+", path.name).group()))
    require(len(selected_units) == 1024, "A_selected_owner_folder_count", len(selected_units))
    statuses: dict[str, int] = {}
    retries = 0
    for expected_owner, unit in zip(range(8400001, 8401025), selected_units):
        require(unit.name == f"TRAIN-{expected_owner}-B", "A_selected_owner_identity", unit.name)
        attempts = sorted(unit.glob("attempt-*"))
        if len(attempts) != 1:
            retries += max(0, len(attempts) - 1)
            errors.append(f"A_owner_{expected_owner}_attempt_count: expected 1, got {len(attempts)}")
            continue
        result_path = attempts[0] / "result.json"
        finished_path = attempts[0] / "finished.json"
        if not result_path.is_file() or not finished_path.is_file():
            errors.append(f"A_owner_{expected_owner}_missing_terminal_receipt")
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        finished = json.loads(finished_path.read_text(encoding="utf-8"))
        status = str(result.get("status"))
        statuses[status] = statuses.get(status, 0) + 1
        require(status == "SUCCEEDED", f"A_owner_{expected_owner}_result_status", status)
        require(finished.get("status") == "SUCCEEDED", f"A_owner_{expected_owner}_finished_status", finished.get("status"))
        require(finished.get("unit_id") == f"TRAIN:{expected_owner}:B", f"A_owner_{expected_owner}_finished_unit", finished.get("unit_id"))
        result_sha = sha256(result_path)
        require(result_sha == finished.get("result_sha256"), f"A_owner_{expected_owner}_result_sha256", result_sha)

    checks["A_training_ancestry"] = {
        "producer_commit": source_commit,
        "producer_source_lock_sha256": producer_lock_sha,
        "accepted_issue84_source_commit": base["issue84_final_accepted_source_commit"],
        "accepted_issue84_source_lock_sha256": accepted_lock_sha,
        "first_block_source_model_digest": block_summaries[0].get("source_model_digest") if block_summaries else None,
        "last_block_result_model_digest": block_summaries[-1].get("result_model_digest") if block_summaries else None,
        "blocks": block_summaries,
        "selected_owner_folders": len(selected_units),
        "selected_terminal_statuses": statuses,
        "retries": retries,
    }
    checks["P_A_model_digests"] = {
        "P_checkpoint_sha256": sha256(parent_model),
        "P_model_digest": p_digest,
        "P_optimizer_sha256": sha256(parent_optimizer),
        "A_checkpoint_sha256": sha256(base_model),
        "A_model_digest": a_digest,
    }
    return {
        "schema_id": "issue85-frozen-baseline-preflight-v1",
        "verified": not errors,
        "verified_utc": datetime.now(timezone.utc).isoformat(),
        "formal_or_pilot_games_started_by_this_check": 0,
        "checks": checks,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--native-source", type=Path, required=True)
    parser.add_argument("--git-repo", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.data_root.resolve(), args.native_source.resolve(), args.git_repo.resolve())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"verified": report["verified"], "report": str(args.report), "errors": report["errors"]}, sort_keys=True))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
