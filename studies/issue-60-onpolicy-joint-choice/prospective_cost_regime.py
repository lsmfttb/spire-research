"""Study-local evidence filter for Issue 60's cached execution regime."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping


REGIME_ID = "issue60-cached-guard-v1"
AUTHORIZATION_COMMENT_ID = 6215441116
BRIDGE_STAGE = "block1:batch-01"
BRIDGE_SEEDS = tuple(range(660070, 660129))
BRIDGE_CPU_LIMIT = 5000.0
FINALIZATION_RESERVE = 30.0
BUILDER_CAP = 180000.0
REVIEWER_RESERVE = 20000.0
PROCESS_CAP = 200000.0
ARM_ORDER = ("final_block1", "final_block2", "p0", "A", "Expert")
PRODUCER_FILES = (
    "prospective_cost_regime.py", "run_cost_bridge.py", "run_study_parallel.py",
    "parallel_execution.py", "guard_optimization.py", "run_study_r2.py",
    "resource_amendment_r2.py", "run_study.py", "config.json",
)


class CostRegimeError(RuntimeError):
    pass


def _declared_order(row: Mapping[str, Any]) -> tuple[int, ...]:
    stage = row.get("stage_key")
    seed = int(row["game_seed"])
    if isinstance(stage, str) and stage.startswith("block1:batch-"):
        return (1, 0, int(stage.rsplit("-", 1)[1]), seed)
    if isinstance(stage, str) and stage.startswith("block2:batch-"):
        return (1, 1, int(stage.rsplit("-", 1)[1]), seed)
    if isinstance(stage, str) and stage.startswith("owner-"):
        try:
            arm = ARM_ORDER.index(str(row["arm"]))
        except ValueError as exc:
            raise CostRegimeError(f"unknown formal evaluation arm: {row.get('arm')}") from exc
        return (2, int(stage.split("-", 1)[1]), arm)
    raise CostRegimeError(f"formal cost is outside the registered grid: {stage!r}")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        capture_output=True, text=True, env=_git_environment(root),
    )
    return result.stdout.strip()


def _git_environment(root: Path) -> dict[str, str]:
    """Keep WSL worktree overrides local to Git calls for this checkout."""
    environment = os.environ.copy()
    environment.pop("GIT_DIR", None)
    environment.pop("GIT_WORK_TREE", None)
    if os.name != "posix":
        return environment
    for candidate in (root, *root.parents):
        git_file = candidate / ".git"
        if not git_file.is_file():
            continue
        marker = git_file.read_text(encoding="utf-8").strip().removeprefix("gitdir:").strip()
        if len(marker) >= 4 and marker[1:3] == ":/":
            worktree_git_dir = subprocess.run(
                ["wslpath", "-u", marker], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            environment["GIT_DIR"] = worktree_git_dir
            environment["GIT_WORK_TREE"] = str(candidate)
        break
    return environment


def producer_manifest(root: Path) -> dict[str, str]:
    return {name: sha256(root / name)
            for name in PRODUCER_FILES}


def validate_bridge_population(seeds: Iterable[int]) -> None:
    observed = tuple(sorted(int(seed) for seed in seeds))
    if observed != BRIDGE_SEEDS:
        raise CostRegimeError("bridge population must be exactly registered seeds 660070..660128")


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    payload = json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _safe_attempt(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    runs = (root / "evidence" / "runs").resolve()
    if runs not in path.parents:
        raise CostRegimeError("prospective cost must point inside evidence/runs")
    if not path.is_dir() or not (path / "run.json").is_file():
        raise CostRegimeError("prospective cost attempt is missing run.json")
    return path


def write_cost_tag(root: Path, attempt_relative: str, *, producer_commit: str,
                   manifest: Mapping[str, str]) -> dict[str, Any]:
    attempt = _safe_attempt(root, attempt_relative)
    run_path = attempt / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if run.get("status") != "COMPLETE":
        raise CostRegimeError("only complete formal runs can enter prospective unit costs")
    stage = str(run.get("stage_key", ""))
    if not (stage.startswith("block1:batch-") or stage.startswith("block2:batch-")
            or stage.startswith("owner-")):
        raise CostRegimeError("preflight, validation and diagnostic runs are not formal cost samples")
    seed = int(run["game_seed"])
    arm = str(run["arm"])
    cost = float(run["process_cpu_seconds"])
    if not math.isfinite(cost) or cost < 0:
        raise CostRegimeError("formal run has invalid process CPU cost")
    if stage == BRIDGE_STAGE and seed not in BRIDGE_SEEDS:
        raise CostRegimeError("bridge run seed is outside the authorized 59-game set")
    tag = {
        "schema_id": "issue60-prospective-cost-tag-v1",
        "regime_id": REGIME_ID,
        "authorization_comment_id": AUTHORIZATION_COMMENT_ID,
        "producer_commit": producer_commit,
        "producer_files": dict(manifest),
        "attempt_path": attempt_relative.replace("\\", "/"),
        "run_json_sha256": sha256(run_path),
        "stage_key": stage,
        "game_seed": seed,
        "arm": arm,
        "process_cpu_seconds": cost,
    }
    name = hashlib.sha256(tag["attempt_path"].encode("utf-8")).hexdigest() + ".json"
    path = root / "evidence" / "prospective-cost" / "tags" / name
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CostRegimeError(f"existing prospective cost tag is unreadable: {path}") from exc
        if previous != tag:
            raise CostRegimeError(f"existing prospective cost tag conflicts with this run: {path}")
        return previous
    atomic_json(path, tag)
    return tag


def load_cost_tags(root: Path, *, current_manifest: Mapping[str, str]) -> list[dict[str, Any]]:
    tags: dict[tuple[str, int, str], dict[str, Any]] = {}
    verified_producers: set[str] = set()
    directory = root / "evidence" / "prospective-cost" / "tags"
    for tag_path in sorted(directory.glob("*.json")) if directory.exists() else ():
        try:
            tag = json.loads(tag_path.read_text(encoding="utf-8"))
            if tag.get("schema_id") != "issue60-prospective-cost-tag-v1" or tag.get("regime_id") != REGIME_ID:
                raise CostRegimeError(f"unknown prospective cost tag: {tag_path.name}")
            if tag.get("authorization_comment_id") != AUTHORIZATION_COMMENT_ID:
                raise CostRegimeError("prospective cost tag has wrong Planner authorization")
            if dict(tag.get("producer_files", {})) != dict(current_manifest):
                raise CostRegimeError("prospective cost producer file hashes differ from active runner")
            producer = str(tag["producer_commit"])
            if producer not in verified_producers:
                git(root, "merge-base", "--is-ancestor", producer, "HEAD")
                for name, digest in current_manifest.items():
                    if hashlib.sha256(git_bytes(root, "show", f"{producer}:studies/issue-60-onpolicy-joint-choice/{name}")).hexdigest() != digest:
                        raise CostRegimeError(f"prospective producer commit does not contain {name}")
                verified_producers.add(producer)
            attempt = _safe_attempt(root, str(tag["attempt_path"]))
            run_path = attempt / "run.json"
            if sha256(run_path) != tag.get("run_json_sha256"):
                raise CostRegimeError("tagged run.json changed after cost attribution")
            run = json.loads(run_path.read_text(encoding="utf-8"))
            if (run.get("status") != "COMPLETE" or run.get("stage_key") != tag.get("stage_key")
                    or int(run.get("game_seed", -1)) != int(tag.get("game_seed", -2))
                    or str(run.get("arm")) != str(tag.get("arm"))
                    or float(run.get("process_cpu_seconds", -1)) != float(tag.get("process_cpu_seconds", -2))):
                raise CostRegimeError("tag identity or cost disagrees with complete run.json")
            key = (str(tag["stage_key"]), int(tag["game_seed"]), str(tag["arm"]))
            if key in tags and tags[key] != tag:
                raise CostRegimeError(f"duplicate tagged cost identity: {key}")
            tags[key] = tag
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError,
                subprocess.CalledProcessError) as exc:
            if isinstance(exc, CostRegimeError):
                raise
            raise CostRegimeError(f"invalid prospective cost tag {tag_path.name}: {exc}") from exc
    return sorted(tags.values(), key=_declared_order)


def git_bytes(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        env=_git_environment(root),
    )
    return result.stdout


def prospective_game_costs(root: Path, *, current_manifest: Mapping[str, str],
                           bridge_only: bool = False) -> list[float]:
    rows = load_cost_tags(root, current_manifest=current_manifest)
    if bridge_only:
        rows = [row for row in rows if row["stage_key"] == BRIDGE_STAGE and int(row["game_seed"]) in BRIDGE_SEEDS]
    costs = [float(row["process_cpu_seconds"]) for row in rows]
    if not costs or any(not math.isfinite(value) or value < 0 for value in costs):
        raise CostRegimeError("prospective cost sample is empty or invalid")
    return costs


def enforce_actual_caps(*, actual_cpu: float, in_flight_workers: int,
                        max_game_cpu: float, bridge_baseline_cpu: float | None = None,
                        bridge_spent_cpu: float = 0.0) -> None:
    reserve = max(0, int(in_flight_workers)) * max(0.0, float(max_game_cpu)) + FINALIZATION_RESERVE
    if actual_cpu + reserve >= BUILDER_CAP:
        raise CostRegimeError("RESOURCE_BLOCKED: Builder cap plus in-flight/finalization reserve")
    if actual_cpu + reserve + REVIEWER_RESERVE >= PROCESS_CAP:
        raise CostRegimeError("RESOURCE_BLOCKED: aggregate cap plus in-flight/finalization reserve")
    if bridge_baseline_cpu is not None and bridge_spent_cpu + reserve >= BRIDGE_CPU_LIMIT:
        raise CostRegimeError("RESOURCE_BLOCKED: 5,000 CPU-s bridge allowance plus reserves")
