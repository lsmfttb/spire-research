from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Iterator, Mapping, Sequence

from algorithm import Decision, SearchBudgetExhausted, TrialResult
from model import ActionConditionedPolicy, model_digest

STUDY = Path(__file__).resolve().parent
REPO_ROOT = STUDY.parents[1]
ISSUE73_DIR = REPO_ROOT / "studies" / "issue-73-privileged-noncombat-oracle-stage0"
ISSUE73_POLICY_PATH = ISSUE73_DIR / "policy.py"
ISSUE73_RUNNER_PATH = ISSUE73_DIR / "runner.py"
PI_INIT_PATH = ISSUE73_DIR / "receipts" / "stage0" / "policies" / "pi_init.pt"


class RootPrefixStop(Exception):
    """Internal sentinel raised before taking the selected historical root action."""


class SourceRootMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class LockedRoot:
    owner_seed: int
    root_id: str
    decision_index: int
    policy_decision_index: int
    screen: str
    checkpoint: Any = field(repr=False, compare=False)
    adapter: Any = field(repr=False, compare=False)
    legal_identities: tuple[Mapping[str, Any], ...]
    fingerprints: Mapping[str, str]
    projection_digest: str
    feature_matrix_sha256: str

    def forced_root(self, selected_index: int) -> dict[str, Any]:
        return {
            "screen": self.screen,
            "root_fingerprints": dict(self.fingerprints),
            "legal_action_identities": [dict(item) for item in self.legal_identities],
            "candidate_index": int(selected_index),
        }


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def binary_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def process_tree_cpu_seconds() -> float:
    import resource
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return float(own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime)


def configure_stsrl_import_path(source_dir: str | Path) -> Path:
    """Prefer the source-locked STSRL worktree for the native identity import."""
    source_root = Path(source_dir).expanduser().resolve(strict=True)
    package_root_path = source_root / "src"
    if not package_root_path.is_dir():
        raise RuntimeError("pinned STSRL source lacks src/sts_combat_rl")
    package_root = package_root_path.resolve(strict=True)
    package_dir = package_root / "sts_combat_rl"
    if not package_dir.is_dir():
        raise RuntimeError("pinned STSRL source lacks src/sts_combat_rl")

    existing_package = sys.modules.get("sts_combat_rl")
    if existing_package is not None:
        package_paths = [
            Path(value).resolve(strict=True)
            for value in getattr(existing_package, "__path__", ())
        ]
        if not package_paths or any(not path.is_relative_to(package_root) for path in package_paths):
            raise RuntimeError("sts_combat_rl was imported outside the pinned STSRL source")

    package_root_text = str(package_root)
    resolved_paths = {str(Path(entry or ".").resolve()) for entry in sys.path}
    if package_root_text not in resolved_paths:
        sys.path.insert(0, package_root_text)
    return package_root


def verify_source_repositories(config: Mapping[str, Any]) -> None:
    import subprocess

    for env_name, expected in (
        ("STS_LIGHTSPEED_SOURCE_DIR", config["source"]["sts_lightspeed_commit"]),
        ("STSRL_SOURCE_DIR", config["source"]["stsr_source_commit"]),
    ):
        raw = os.environ.get(env_name)
        if not raw:
            raise RuntimeError(f"{env_name} must name the separate pinned source worktree")
        result = subprocess.run(
            ["git", "-C", raw, "rev-parse", "HEAD"], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        dirty = subprocess.run(
            ["git", "-C", raw, "status", "--porcelain"], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()
        if result.stdout.strip() != expected or dirty:
            raise RuntimeError(f"{env_name} source is not the exact clean pinned commit")

    configure_stsrl_import_path(os.environ["STSRL_SOURCE_DIR"])


def verify_native_module(config: Mapping[str, Any]) -> Path:
    path = Path(str(config["source"]["native_module_path"])).expanduser().resolve(strict=True)
    if not path.is_file() or binary_sha256(path) != str(config["source"]["native_module_sha256"]):
        raise RuntimeError("native module binary differs from the exact pinned digest")
    return path


def load_issue73_runner(config: Mapping[str, Any]) -> Any:
    """Import only the reviewed runner source; never constructs a simulator here."""
    if not ISSUE73_RUNNER_PATH.is_file() or not ISSUE73_POLICY_PATH.is_file():
        raise RuntimeError("reviewed Issue 73 source files are unavailable")
    if str(ISSUE73_DIR) not in sys.path:
        sys.path.insert(0, str(ISSUE73_DIR))
    if str(ISSUE73_DIR.parent) not in sys.path:
        sys.path.insert(0, str(ISSUE73_DIR.parent))
    existing_policy = sys.modules.get("policy")
    if existing_policy is not None and Path(getattr(existing_policy, "__file__", "")).resolve() != ISSUE73_POLICY_PATH.resolve():
        raise RuntimeError("a different policy module is already loaded in this interpreter")
    spec = importlib.util.spec_from_file_location("issue73_locked_runner", ISSUE73_RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the exact reviewed Issue 73 runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if module.EXPECTED_NATIVE != config["source"]["sts_lightspeed_commit"]:
        raise RuntimeError("Issue 73 runner and Issue 75 native-source locks differ")
    return module


def initialize_native_runtime(output_dir: Path, *, sampling: bool) -> tuple[dict[str, Any], Any, Any, Any]:
    """Validate exact source/runtime pins and install the accepted execution path."""
    from preflight import load_json

    config = load_json(STUDY / "config.json")
    if sampling:
        if config["authorization"]["native_sampling_blocked_until_reviewer_pass"] is not True:
            raise RuntimeError("source contract does not keep native sampling behind independent review")
        from preflight import reviewer_pass_proof
        reviewer_pass_proof()
    verify_source_repositories(config)
    native_path = verify_native_module(config)
    runner = load_issue73_runner(config)
    output_dir.mkdir(parents=True, exist_ok=True)
    env, module, encoder, runtime = runner._setup(output_dir, native_module_path=native_path)
    loaded_module_path = Path(module.__file__).resolve(strict=True)
    if loaded_module_path != native_path or binary_sha256(loaded_module_path) != config["source"]["native_module_sha256"]:
        raise RuntimeError("Python did not load the pinned native extension")
    if env["identity"].get("native_source_commit") != config["source"]["sts_lightspeed_commit"]:
        raise RuntimeError("loaded native source identity differs from the task source lock")
    return config, runner, module, encoder


def feature_matrix_sha256(matrix: Any) -> str:
    import numpy as np
    return hashlib.sha256(np.asarray(matrix, dtype="<f4").tobytes()).hexdigest()


def _safe_root_capture(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "checkpoint": row["checkpoint"],
        "adapter": row["adapter"],
        "decision_index": int(row["decision_index"]),
        "policy_decision_index": int(row["policy_decision_index"]),
        "screen": str(row["screen"]),
        "legal_action_identities": tuple(dict(item) for item in row["legal_action_identities"]),
        "fingerprints": {
            key: str(row[key]) for key in (
                "native_state_sha256", "public_projection_sha256", "public_resource_state_sha256",
            )
        },
        "projection_digest": str(row["projection_digest"]),
        "feature_matrix_sha256": feature_matrix_sha256(row["feature_matrix"]),
    }


def replay_locked_root(
    *, runner: Any, module: Any, encoder: Any, output_dir: Path,
    owner_seed: int, expected: Mapping[str, Any],
) -> LockedRoot:
    """Replay exact historical P0 prefix and stop before the registered root action."""
    if binary_sha256(PI_INIT_PATH) != str(json.loads((STUDY / "config.json").read_text(encoding="utf-8"))["source"]["issue73_pi_init_sha256"]):
        raise SourceRootMismatch("historical Issue 73 P0 prefix policy checkpoint changed")
    model, digest = runner._load_policy(PI_INIT_PATH, initial=False)
    found: dict[str, Any] = {}
    target_decision = int(expected["owner_decision_index"])

    def on_root(row: dict[str, Any]) -> None:
        decision_index = int(row["decision_index"])
        if decision_index < target_decision:
            row.clear()
            return
        if decision_index != target_decision:
            row.clear()
            raise SourceRootMismatch("preselected historical root was not reached at its locked source-prefix boundary")
        capture = _safe_root_capture(row)
        checks = {
            "policy_decision_index": capture["policy_decision_index"] == int(expected["policy_decision_index"]),
            "screen": capture["screen"] == str(expected["screen"]),
            "legal_action_identities_sha256": hashlib.sha256(
                canonical(list(capture["legal_action_identities"])).encode("utf-8")
            ).hexdigest() == str(expected["legal_action_identities_sha256"]),
            "feature_matrix_sha256": capture["feature_matrix_sha256"] == str(expected["feature_matrix_sha256"]),
            "projection_digest": capture["projection_digest"] == str(expected["projection_digest"]),
        }
        checks.update({
            key: capture["fingerprints"].get(key) == str(expected["root_fingerprints"].get(key))
            for key in capture["fingerprints"]
        })
        row.clear()
        if not all(checks.values()):
            raise SourceRootMismatch("replayed native root fails its locked legal/state/feature identity checks")
        found.update(capture)
        raise RootPrefixStop()

    from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
    adapter = LightSpeedAdapter(
        seed=int(owner_seed), ascension=20, player_class="IRONCLAD", module=module,
    )
    try:
        runner._run_one(
            out=output_dir, module=module, model=model, model_digest=digest,
            seed=int(owner_seed), arm="P0", encoder=encoder,
            adapter_override=adapter,
            purpose="issue73-stage0-debug-owner", durable=False,
            capture_trace=False, capture_checkpoints=True, capture_all_roots=True,
            root_callback=on_root, cpu_cap_seconds=32.0,
        )
    except RootPrefixStop:
        pass
    except Exception as exc:
        roots = getattr(exc, "issue73_captured_roots", ())
        for root in roots:
            root.clear()
        adapter.close()
        if isinstance(exc, SourceRootMismatch):
            raise
        raise
    if not found:
        adapter.close()
        raise SourceRootMismatch("the exact registered nonbattle multi-choice root was not reached")
    return LockedRoot(
        owner_seed=int(owner_seed), root_id=str(expected["root_id"]),
        decision_index=int(found["decision_index"]),
        policy_decision_index=int(found["policy_decision_index"]),
        screen=str(found["screen"]), checkpoint=found["checkpoint"],
        adapter=found["adapter"], legal_identities=found["legal_action_identities"],
        fingerprints=found["fingerprints"], projection_digest=str(found["projection_digest"]),
        feature_matrix_sha256=str(found["feature_matrix_sha256"]),
    )


class PolicyAdapter:
    """Compatibility surface consumed by the reviewed runner's BRANCH controller."""
    def __init__(self, policy: ActionConditionedPolicy):
        self.policy = policy

    def choose(self, matrix: Any, generator: Any) -> tuple[int, Any, float]:
        import torch
        with torch.no_grad():
            chosen, probabilities, _, _, _ = self.policy.sample(features=matrix, generator=generator)
        return int(chosen), probabilities, 0.0


def _decision_rows(runner: Any, result: dict[str, Any], *, is_root_run: bool) -> tuple[Decision, ...]:
    import numpy as np

    roots = result.pop("_volatile_roots", None)
    if roots is None:
        raise RuntimeError("terminal branch did not return its in-memory native checkpoints")
    trace = result.get("decisions", [])
    by_index: dict[int, dict[str, Any]] = {}
    try:
        for root in roots:
            index = int(root["decision_index"])
            if index in by_index:
                raise RuntimeError("duplicate multichoice root decision index")
            by_index[index] = root
        rows: list[Decision] = []
        first = True
        for index, root in sorted(by_index.items()):
            if not 0 <= index < len(trace):
                raise RuntimeError("captured checkpoint does not map to the actual controller trace")
            item = trace[index]
            if item.get("kind") != "categorical":
                raise RuntimeError("captured multichoice native checkpoint maps to a noncategorical action")
            identities = tuple(dict(value) for value in item["legal_action_identities"])
            matrix = np.asarray(root["feature_matrix"], dtype="<f4").copy()
            chosen = int(item["chosen_index"])
            if matrix.shape[0] != len(identities) or not 0 <= chosen < len(identities):
                raise RuntimeError("live legal occurrence count differs from candidate feature rows")
            rows.append(Decision(
                step_index=int(item["step"]),
                state_fingerprint=str(root["native_state_sha256"]),
                legal_identities=identities,
                chosen_index=chosen,
                candidate_features=matrix,
                log_probabilities=item.get("probabilities"),
                checkpoint=root["checkpoint"],
                adapter=root["adapter"],
                is_root=bool(first and is_root_run),
                screen=str(root["screen"]),
                root_fingerprints={
                    key: str(root[key]) for key in (
                        "native_state_sha256", "public_projection_sha256", "public_resource_state_sha256",
                    )
                },
            ))
            first = False
        if not rows or not rows[0].is_root:
            raise RuntimeError("a complete continuation must contain its actual root multi-choice decision")
        return tuple(rows)
    finally:
        for root in roots:
            root.clear()


def _hash_only_decisions(decisions: Sequence[Decision]) -> list[dict[str, Any]]:
    from algorithm import identity_key
    import base64
    import numpy as np
    import zlib

    result: list[dict[str, Any]] = []
    for row in decisions:
        matrix = np.asarray(row.candidate_features, dtype="<f4")
        packed = base64.b64encode(zlib.compress(matrix.tobytes(), level=6)).decode("ascii")
        result.append({
            "step_index": int(row.step_index),
            "state_fingerprint": str(row.state_fingerprint),
            "legal_identity_sha256": [identity_key(item) for item in row.legal_identities],
            "chosen_index": int(row.chosen_index),
            "candidate_features_f32le_zlib_base64": packed,
            "candidate_features_sha256": hashlib.sha256(matrix.tobytes()).hexdigest(),
            "candidate_features_shape": list(matrix.shape),
            "is_root": bool(row.is_root),
        })
    return result


def _safe_trial_payload(trial: TrialResult | None) -> dict[str, Any] | None:
    if trial is None:
        return None
    if not trial.complete:
        return {
            "attempt_index": int(trial.attempt_index), "complete": False,
            "failure_boundary": trial.failure_boundary,
            "cpu_seconds": trial.cpu_seconds, "wall_seconds": trial.wall_seconds,
        }
    return {
        "attempt_index": int(trial.attempt_index), "complete": True,
        "heart_win": bool(trial.heart_win), "terminal_floor": float(trial.terminal_floor),
        "root_identity_sha256": hashlib.sha256(canonical(trial.root_identity).encode("utf-8")).hexdigest(),
        "decisions": _hash_only_decisions(trial.decisions),
        "cpu_seconds": trial.cpu_seconds, "wall_seconds": trial.wall_seconds,
        "native_rng_source": trial.native_rng_source,
    }


def execute_policy_trajectory(
    *, runner: Any, module: Any, encoder: Any, output_dir: Path,
    policy: ActionConditionedPolicy, owner_seed: int, root: LockedRoot,
    attempt_index: int, invocation_seed: int, root_action_index: int | None,
    mutation_target: Any | None, parent_trial: TrialResult | None = None,
    cpu_cap_seconds: float = 32.0, source_arm: str = "BRANCH",
) -> TrialResult:
    """Restore one genuine in-memory checkpoint and run a true native terminal trajectory."""
    import time

    if mutation_target is not None:
        if parent_trial is None or not parent_trial.complete:
            raise ValueError("joint mutation parent must be a complete retained trajectory")
        start_decision = mutation_target.parent_decision
        checkpoint = start_decision.checkpoint
        adapter_override = start_decision.adapter
        forced_index = int(mutation_target.alternative_index)
        force_data = {
            "screen": start_decision.screen,
            "root_fingerprints": dict(start_decision.root_fingerprints),
            "legal_action_identities": [dict(value) for value in start_decision.legal_identities],
            "candidate_index": forced_index,
        }
        try:
            prefix_end = next(index for index, row in enumerate(parent_trial.decisions) if row is start_decision)
        except StopIteration as exc:
            raise ValueError("mutation target is not a decision in its retained parent trajectory") from exc
        prefix = parent_trial.decisions[:prefix_end]
    else:
        start_decision = None
        checkpoint = root.checkpoint
        adapter_override = root.adapter
        prefix = ()
        if root_action_index is None:
            force_data = None
        else:
            if not 0 <= int(root_action_index) < len(root.legal_identities):
                raise ValueError("root proposal does not map to the locked current legal occurrence list")
            force_data = root.forced_root(int(root_action_index))
    if source_arm not in {"P0", "BRANCH"}:
        raise ValueError("only the non-Expert P0 or BRANCH execution path is available")
    if adapter_override is None:
        raise ValueError("native checkpoint continuation requires its owning simulator adapter")
    policy_digest = model_digest(policy)
    policy_adapter = PolicyAdapter(policy)
    purpose = f"issue75-{root.root_id}-{invocation_seed}-{attempt_index}"
    started_cpu = process_tree_cpu_seconds()
    started_wall = time.monotonic()
    try:
        result = runner._run_one(
            out=output_dir, module=module, model=policy_adapter, model_digest=policy_digest,
            seed=int(owner_seed), arm=source_arm, encoder=encoder,
            adapter_override=adapter_override, checkpoint=checkpoint, forced_root=force_data,
            purpose=purpose, durable=False, capture_trace=True,
            capture_checkpoints=True, capture_all_roots=True,
            cpu_cap_seconds=float(cpu_cap_seconds),
        )
    except SearchBudgetExhausted as exc:
        for row in getattr(exc, "issue73_captured_roots", ()):
            row.clear()
        return TrialResult(
            attempt_index=int(attempt_index), complete=False,
            failure_boundary="per_search_cpu_watchdog",
            cpu_seconds=max(0.0, process_tree_cpu_seconds() - started_cpu),
            wall_seconds=max(0.0, time.monotonic() - started_wall),
        )
    except BaseException as exc:
        roots = getattr(exc, "issue73_captured_roots", ())
        for row in roots:
            row.clear()
        return TrialResult(
            attempt_index=int(attempt_index), complete=False,
            failure_boundary=f"native_continuation_error:{type(exc).__name__}",
            cpu_seconds=max(0.0, process_tree_cpu_seconds() - started_cpu),
            wall_seconds=max(0.0, time.monotonic() - started_wall),
        )
    decisions = _decision_rows(runner, result, is_root_run=True)
    if mutation_target is not None:
        from dataclasses import replace
        offset = int(start_decision.step_index)
        suffix = tuple(replace(row, step_index=offset + int(row.step_index), is_root=False) for row in decisions)
        decisions = tuple(prefix) + suffix
        if not decisions or not decisions[0].is_root:
            raise RuntimeError("mutated terminal trajectory lost its original root prefix")
    terminal = result["terminal"]
    return TrialResult(
        attempt_index=int(attempt_index), complete=True,
        heart_win=bool(terminal["heart_victory"]),
        terminal_floor=float(terminal["terminal_floor"]),
        root_identity=(dict(parent_trial.root_identity) if mutation_target is not None else dict(decisions[0].chosen_identity)), decisions=decisions,
        cpu_seconds=max(0.0, process_tree_cpu_seconds() - started_cpu),
        wall_seconds=max(0.0, time.monotonic() - started_wall),
    )


@contextmanager
def process_cpu_alarm(seconds: float) -> Iterator[None]:
    """Hard source-level process-CPU watchdog for one bounded R or J invocation."""
    if not hasattr(signal, "ITIMER_PROF") or not hasattr(signal, "SIGPROF"):
        raise RuntimeError("pinned Linux process-CPU timer is unavailable")
    if seconds <= 0:
        raise ValueError("CPU alarm must be positive")
    old_handler = signal.getsignal(signal.SIGPROF)
    old_timer = signal.getitimer(signal.ITIMER_PROF)

    def expire(signum: int, frame: Any) -> None:
        raise SearchBudgetExhausted("process CPU time limit expired inside native continuation")

    signal.signal(signal.SIGPROF, expire)
    signal.setitimer(signal.ITIMER_PROF, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_PROF, *old_timer)
        signal.signal(signal.SIGPROF, old_handler)