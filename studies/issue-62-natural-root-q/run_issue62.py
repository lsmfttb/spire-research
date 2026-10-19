"""Issue 62 one-step natural-root Q study, reusing the frozen Issue 60 runner."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import random
import resource
import sys
import time
from dataclasses import replace
from typing import Any, Mapping, Sequence

MODULE_CPU_START = time.process_time()
HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "issue-60-onpolicy-joint-choice"
EVIDENCE = HERE / "evidence"
ROOTS_PATH = EVIDENCE / "roots.jsonl"
STATUS_PATH = EVIDENCE / "status.json"
LEDGER_PATH = EVIDENCE / "cpu-ledger.jsonl"
OWNER_SEEDS = tuple(range(6210001, 6210097))
ROOT_DOMAIN = "issue62-root"
PREFIX_DOMAIN = "issue62-prefix"
FUTURE_DOMAIN = "issue62-future"
BOOTSTRAP_SEED = 6219999
BOOTSTRAP_RESAMPLES = 5000
TOTAL_CPU_CAP = 12000.0
REVIEWER_RESERVE = 1200.0
BUILDER_STATIC_RESERVE = 300.0
RUN_CPU_CAP = TOTAL_CPU_CAP - REVIEWER_RESERVE - BUILDER_STATIC_RESERVE
MAX_GAME_CPU_RESERVE = 60.0
SOURCE_COMMIT = "1cce7997c648121742f0df0cfb95a2211aa3492c"

RS: Any = None
TORCH: Any = None
TRACKER: Any = None
PRIOR_CPU = 0.0
RUNTIME: dict[str, Any] = {}
CONFIG: dict[str, Any] = {}
VOCAB: Any = None
BOOTSTRAP: Any = None
MODELS: dict[str, tuple[Any, str]] = {}
DESIGN_SHA = ""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + chr(10))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + chr(10))
        stream.flush()
        os.fsync(stream.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def derive_seed(domain: str, *parts: int) -> int:
    payload = "|".join([domain, *(str(part) for part in parts)]).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest(), "big")


def selected_root_ordinal(owner: int) -> int:
    return 1 + derive_seed(ROOT_DOMAIN, owner) % 6


def current_cpu() -> float:
    if TRACKER is None:
        return max(0.0, time.process_time() - MODULE_CPU_START)
    return TRACKER.current_stage_cpu()


def all_in_cpu() -> float:
    return PRIOR_CPU + current_cpu()


def hard_budget_guard() -> None:
    active = max(1, TRACKER.active_worker_count() if TRACKER is not None else 1)
    if all_in_cpu() + active * MAX_GAME_CPU_RESERVE >= RUN_CPU_CAP:
        raise RuntimeError("ISSUE62_RESOURCE_BLOCKED: preserving the fixed cohort and Reviewer reserve")


def identity_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def legal_identities(actions: Sequence[Any]) -> list[list[Any]]:
    fields = ("scope", "kind", "idx1", "idx2", "idx3")
    result = []
    for action in actions:
        native = action.raw.get("native")
        if native is None:
            raise RuntimeError("native legal-action identity is missing")
        result.append([identity_value(getattr(native, name)) for name in fields])
    return result


def native_fingerprint(adapter: Any, snapshot: Any) -> str:
    observation, stateful_raw = adapter.checkpoint_fingerprint(snapshot)
    return sha256(canonical_bytes([list(observation), stateful_raw]))


def public_location(context: Any) -> dict[str, Any]:
    location = context.public_run_context.get("current", {}).get("location", {})
    if not isinstance(location, Mapping):
        return {}
    output = {}
    for key in ("act", "floor", "room_type"):
        value = location.get(key)
        if isinstance(value, Mapping) and value.get("availability") == "available":
            value = value.get("value")
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            output[key] = value
    return output


def make_root_record(adapter: Any, snapshot: Any, actions: Sequence[Any], context: Any,
                     step_index: int, k: int, decision: Any, owner: int) -> dict[str, Any]:
    metadata = decision.metadata
    public_input = metadata.get("issue60_public_input")
    input_sha = metadata.get("issue60_public_input_sha256")
    probabilities = metadata.get("issue60_base_probabilities")
    if (not isinstance(public_input, str) or not isinstance(input_sha, str)
            or sha256(public_input.encode("utf-8")) != input_sha
            or not isinstance(probabilities, list)
            or len(probabilities) != len(actions)):
        raise RuntimeError("p0 root decision lacks a valid public input or probability vector")
    return {
        "owner_seed": owner, "selected_root_ordinal": k, "root_ordinal_observed": k,
        "status": "ROOT_FOUND", "step_index": int(step_index),
        "screen": str(context.screen_state), "location": public_location(context),
        "legal_n": len(actions), "sampled_p0_index": int(decision.selected_index),
        "p0": [float(value) for value in probabilities],
        "native_checkpoint_fingerprint_sha256": native_fingerprint(adapter, snapshot),
        "public_history_sha256": sha256(canonical_bytes(context.public_run_context)),
        "public_input_sha256": input_sha, "public_input": public_input,
        "legal_action_identities": legal_identities(actions), "design_sha256": DESIGN_SHA,
    }


class StopAtRoot(RuntimeError):
    pass


class Issue62Controller:
    """Thin intervention wrapper around Issue 60's actual routed controller."""
    def __init__(self, controller: Any, policy: Any, *, owner: int, k: int,
                 expected_root: Mapping[str, Any] | None = None,
                 forced_identity: Sequence[Any] | None = None, future_seed: int | None = None):
        self.controller = controller
        self.policy = policy
        self.owner = owner
        self.k = k
        self.expected_root = expected_root
        self.forced_identity = list(forced_identity) if forced_identity is not None else None
        self.future_seed = future_seed
        self.provenance = controller.provenance
        self.supported_multi_count = 0
        self.root_record: dict[str, Any] | None = None
        self.root_verified = False
        self.failure: str | None = None

    def reset_for_run(self, seed: int | None) -> None:
        self.controller.reset_for_run(seed)
        self.supported_multi_count = 0
        self.root_record = None
        self.root_verified = False
        self.failure = None

    def select_action(self, adapter: Any, snapshot: Any, actions: Sequence[Any],
                      context: Any, step_index: int) -> Any:
        hard_budget_guard()
        decision = self.controller.select_action(adapter, snapshot, actions, context, step_index)
        if context.screen_state not in {"MAP_SCREEN", "REWARDS"} or len(actions) <= 1:
            return decision
        self.supported_multi_count += 1
        if self.root_verified or self.supported_multi_count != self.k:
            return decision
        try:
            observed = make_root_record(adapter, snapshot, actions, context, step_index,
                                        self.k, decision, self.owner)
        except Exception as exc:
            self.failure = f"ROOT_REPLAY_BLOCKED: {type(exc).__name__}: {exc}"
            raise RuntimeError(self.failure) from exc
        self.root_record = observed
        if self.expected_root is None:
            raise StopAtRoot("ISSUE62_ROOT_CAPTURED")
        fields = (
            "step_index", "screen", "legal_n", "sampled_p0_index",
            "native_checkpoint_fingerprint_sha256", "public_history_sha256",
            "public_input_sha256", "legal_action_identities",
        )
        mismatches = [name for name in fields if observed.get(name) != self.expected_root.get(name)]
        if mismatches:
            self.failure = "ROOT_REPLAY_BLOCKED: parity mismatch " + ",".join(mismatches)
            raise RuntimeError(self.failure)
        if self.forced_identity is None or self.future_seed is None:
            self.failure = "ROOT_REPLAY_BLOCKED: branch intervention is incomplete"
            raise RuntimeError(self.failure)
        matching = [i for i, item in enumerate(observed["legal_action_identities"])
                    if item == self.forced_identity]
        if len(matching) != 1:
            self.failure = "ROOT_REPLAY_BLOCKED: forced native action is not uniquely legal"
            raise RuntimeError(self.failure)
        # The underlying p0 decision above consumes the registered root draw.
        # Switch only subsequent supported-choice draws to the paired future stream.
        self.policy.rng = random.Random(self.future_seed)
        self.root_verified = True
        metadata = dict(decision.metadata)
        metadata.update({
            "issue62_forced_root_action": True,
            "issue62_forced_action_identity": self.forced_identity,
            "issue62_root_fingerprint_sha256": observed["native_checkpoint_fingerprint_sha256"],
        })
        return replace(decision, selected_index=matching[0], metadata=metadata)
def source_controller(owner: int, prefix_seed: int) -> tuple[Any, Any, Any]:
    return RS._source_controller(
        vocabulary=VOCAB, bootstrap=BOOTSTRAP, model=None, mode="p0",
        runtime_seed=prefix_seed, block_seed=None,
        provenance_config={"study": "issue62-one-step-natural-root-q"},
        event_sink=None, decision_sink=None, require_targets=False,
    )


def make_adapter(owner: int) -> Any:
    from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
    return LightSpeedAdapter(
        seed=owner,
        ascension=CONFIG["environment"]["ascension"],
        player_class=CONFIG["environment"]["player_class"],
    )


def scan_root(owner: int) -> dict[str, Any]:
    from sts_combat_rl.sim.action_space import ActionSpaceConfig
    from sts_combat_rl.sim.controlled_run import execute_controlled_run
    k = selected_root_ordinal(owner)
    prefix_seed = derive_seed(PREFIX_DOMAIN, owner)
    controller, policy, battle = source_controller(owner, prefix_seed)
    wrapper = Issue62Controller(controller, policy, owner=owner, k=k)
    adapter = make_adapter(owner)
    started_cpu, started_wall = time.process_time(), time.monotonic()
    try:
        run = execute_controlled_run(
            adapter, wrapper, seed=owner, max_steps=RS.MAX_STEPS,
            action_space=ActionSpaceConfig.include_all(), retain_steps=False,
        )
        if wrapper.root_record is not None:
            row = dict(wrapper.root_record)
            row["status"] = "ROOT_FOUND"
        elif run.terminal and not run.problems:
            row = {"owner_seed": owner, "selected_root_ordinal": k,
                   "root_ordinal_observed": wrapper.supported_multi_count,
                   "status": "INELIGIBLE_TERMINAL_BEFORE_K", "design_sha256": DESIGN_SHA}
        elif any("RESOURCE_BLOCKED" in str(problem) for problem in run.problems):
            row = {"owner_seed": owner, "selected_root_ordinal": k,
                   "root_ordinal_observed": wrapper.supported_multi_count,
                   "status": "CENSORED_RESOURCE_BLOCKED", "design_sha256": DESIGN_SHA}
        elif not run.problems:
            row = {"owner_seed": owner, "selected_root_ordinal": k,
                   "root_ordinal_observed": wrapper.supported_multi_count,
                   "status": "CENSORED_STEP_CAP", "design_sha256": DESIGN_SHA}
        else:
            row = {"owner_seed": owner, "selected_root_ordinal": k,
                   "root_ordinal_observed": wrapper.supported_multi_count,
                   "status": "INELIGIBLE_INVALID_BEFORE_K", "problems": list(run.problems),
                   "design_sha256": DESIGN_SHA}
    except Exception as exc:
        row = {"owner_seed": owner, "selected_root_ordinal": k,
               "root_ordinal_observed": wrapper.supported_multi_count,
               "status": "INELIGIBLE_INVALID_BEFORE_K",
               "problems": [f"{type(exc).__name__}: {exc}"], "design_sha256": DESIGN_SHA}
    finally:
        adapter.close()
    row["search_calls_to_root"] = battle.calls
    row["search_cpu_to_root_seconds"] = battle.cpu_seconds
    row["source_prefix_cpu_seconds"] = max(0.0, time.process_time() - started_cpu)
    row["source_prefix_wall_seconds"] = max(0.0, time.monotonic() - started_wall)
    return row


def load_roots() -> list[dict[str, Any]]:
    rows = read_jsonl(ROOTS_PATH)
    if any(row.get("design_sha256") != DESIGN_SHA for row in rows):
        raise RuntimeError("root records do not match the registered Issue 62 design")
    by_owner = {int(row["owner_seed"]): row for row in rows}
    return [by_owner[seed] for seed in OWNER_SEEDS if seed in by_owner]


def run_roots() -> None:
    prior = {int(row["owner_seed"]): row for row in load_roots()}
    for index, owner in enumerate(OWNER_SEEDS, 1):
        if owner in prior:
            continue
        hard_budget_guard()
        atomic_json(STATUS_PATH, {
            "schema_id": "issue62-stage-status-v1", "stage": "ROOT_SCAN",
            "status": "RUNNING", "last_owner_seed": owner,
            "completed_root_scans": len(prior), "total_fixed_owner_seeds": len(OWNER_SEEDS),
            "all_in_cpu_seconds": all_in_cpu(),
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        row = scan_root(owner)
        append_jsonl(ROOTS_PATH, row)
        prior[owner] = row
        atomic_json(STATUS_PATH, {
            "schema_id": "issue62-stage-status-v1", "stage": "ROOT_SCAN",
            "status": "RUNNING", "last_owner_seed": owner,
            "last_owner_status": row["status"], "completed_root_scans": len(prior),
            "total_fixed_owner_seeds": len(OWNER_SEEDS), "all_in_cpu_seconds": all_in_cpu(),
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        print(f"ROOT_SCAN {index}/96 seed={owner} k={row['selected_root_ordinal']} status={row['status']} cpu={row['source_prefix_cpu_seconds']:.3f}", flush=True)
    roots = load_roots()
    eligible = [row for row in roots if row["status"] == "ROOT_FOUND"]
    summary = {
        "schema_id": "issue62-root-cohort-v1", "design_sha256": DESIGN_SHA,
        "fixed_owner_seed_count": len(OWNER_SEEDS), "scanned_owner_seed_count": len(roots),
        "eligible_root_count": len(eligible),
        "ineligible_or_censored_count": len(roots) - len(eligible),
        "status": "SOURCE_COVERAGE_INSUFFICIENT" if len(eligible) < 48 else "ROOTS_READY",
        "status_counts": {name: sum(row["status"] == name for row in roots)
                          for name in sorted({row["status"] for row in roots})},
        "eligible_screen_counts": {name: sum(row.get("screen") == name for row in eligible)
                                   for name in ("MAP_SCREEN", "REWARDS")},
        "roots": roots,
    }
    atomic_json(EVIDENCE / "root-cohort.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "roots"}, sort_keys=True), flush=True)


def score_models(root: Mapping[str, Any]) -> dict[str, Any]:
    from encoder import encode
    from controllers import policy_distribution
    observation = encode(str(root["public_input"]), VOCAB)
    if len(observation.candidates) != int(root["legal_n"]):
        raise RuntimeError("frozen actor candidate count differs from native root legal count")
    scores: dict[str, Any] = {}
    for block, (model, model_sha) in MODELS.items():
        with TORCH.no_grad():
            logits, _value = model(observation)
            residual = [float(value) for value in logits.detach().cpu().tolist()]
        if len(residual) != int(root["legal_n"]):
            raise RuntimeError(f"{block} actor emitted a misaligned root score vector")
        pi = policy_distribution(root["p0"], residual)
        scores[block] = {"model_sha256": model_sha, "residual_scores": residual,
                         "policy_probabilities": pi}
    return scores


def branch_path(owner: int, action_index: int, stream: int) -> Path:
    return EVIDENCE / "branches" / f"owner-{owner}" / f"option-{action_index:02d}" / f"stream-{stream}"


def run_branch(*, owner: int, root: Mapping[str, Any], action_index: int,
               stream: int, output_dir: str, model_sha: str,
               config_digest: str, **_unused: Any) -> tuple[Path, dict[str, Any]]:
    from sts_combat_rl.sim.action_space import ActionSpaceConfig
    from sts_combat_rl.sim.controlled_run import execute_controlled_run
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    attempts = [int(path.name.split("-")[-1]) for path in output.glob("attempt-*")
                if path.name.split("-")[-1].isdigit()]
    attempt = output / f"attempt-{max(attempts, default=0) + 1:04d}"
    attempt.mkdir()
    stage_key = f"owner-{owner}-option-{action_index}-stream-{stream}"
    future_seed = derive_seed(FUTURE_DOMAIN, owner, stream)
    initial = {
        "schema_id": "issue62-branch-attempt-v1", "status": "RUNNING",
        "game_seed": owner, "mode": "p0", "block": "issue62",
        "stage_key": stage_key, "arm": "p0-continuation",
        "runtime_policy_seed": derive_seed(PREFIX_DOMAIN, owner),
        "model_sha256": model_sha, "config_sha256": config_digest,
        "owner_seed": owner, "root_step_index": root["step_index"],
        "root_fingerprint_sha256": root["native_checkpoint_fingerprint_sha256"],
        "forced_action_index": action_index, "future_stream": stream,
        "future_stream_seed_sha256": f"{future_seed:064x}",
        "pid": os.getpid(), "started_cpu_seconds": time.process_time(),
    }
    atomic_json(attempt / "status.json", initial)
    started_cpu, started_wall = time.process_time(), time.monotonic()
    adapter = None
    run = None
    wrapper = None
    battle = None
    failure: str | None = None
    transitions = 0
    try:
        controller, policy, battle = source_controller(owner, derive_seed(PREFIX_DOMAIN, owner))
        expected_identity = root["legal_action_identities"][action_index]
        wrapper = Issue62Controller(
            controller, policy, owner=owner, k=int(root["selected_root_ordinal"]),
            expected_root=root, forced_identity=expected_identity, future_seed=future_seed,
        )
        adapter = make_adapter(owner)

        def after_transition(_step: Any) -> None:
            nonlocal transitions
            transitions += 1
            hard_budget_guard()

        run = execute_controlled_run(
            adapter, wrapper, seed=owner, max_steps=RS.MAX_STEPS,
            action_space=ActionSpaceConfig.include_all(),
            after_transition=after_transition, retain_steps=False,
        )
        if wrapper.failure:
            failure = wrapper.failure
        elif not wrapper.root_verified:
            failure = "ROOT_REPLAY_BLOCKED: registered natural root was not reached"
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if adapter is not None:
            adapter.close()
    run_cpu = max(0.0, time.process_time() - started_cpu)
    complete = bool(
        run is not None and wrapper is not None and wrapper.root_verified
        and run.terminal and not run.problems
        and run.outcome in {"PLAYER_LOSS", "PLAYER_VICTORY"}
        and RS._terminal_floor(run) is not None
    )
    if run is not None and run.problems and failure is None:
        failure = "; ".join(str(value) for value in run.problems)
    summary = {
        **initial,
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "terminal": bool(run.terminal) if run is not None else False,
        "outcome": run.outcome if run is not None else "UNKNOWN",
        "terminal_floor": RS._terminal_floor(run) if complete and run is not None else None,
        "transitions": transitions, "root_verified": bool(wrapper.root_verified) if wrapper is not None else False,
        "process_cpu_seconds": run_cpu, "completed_cpu_seconds": time.process_time(),
        "wall_seconds": max(0.0, time.monotonic() - started_wall),
        "search_cpu_seconds": battle.cpu_seconds if battle is not None else 0.0,
        "search_calls": battle.calls if battle is not None else 0,
        "problems": list(run.problems) if run is not None else [], "failure": failure,
        "model_sha256": model_sha, "config_sha256": config_digest,
        "decision_file": "decisions.jsonl.gz",
    }
    decision = {
        "owner_seed": owner, "root_step_index": root["step_index"],
        "root_public_input_sha256": root["public_input_sha256"],
        "native_root_fingerprint_sha256": root["native_checkpoint_fingerprint_sha256"],
        "forced_action_index": action_index,
        "forced_action_identity": expected_identity,
        "future_stream": stream, "root_replay_verified": bool(summary["root_verified"]),
    }
    with gzip.open(attempt / "decisions.jsonl.gz", "wt", encoding="utf-8") as stream_out:
        stream_out.write(json.dumps(decision, sort_keys=True, separators=(",", ":")) + chr(10))
    atomic_json(attempt / "run.json", summary)
    from attempt_lifecycle import finalized_attempt_status
    atomic_json(attempt / "status.json", finalized_attempt_status(initial, summary))
    return attempt, summary

def planned_tasks(roots: Sequence[Mapping[str, Any]],
                  pilot_key: tuple[int, int, int] | None = None) -> list[dict[str, Any]]:
    tasks = []
    for root in roots:
        owner = int(root["owner_seed"])
        for action_index in range(int(root["legal_n"])):
            for stream in (0, 1):
                if pilot_key == (owner, action_index, stream):
                    continue
                out = branch_path(owner, action_index, stream)
                key = ("issue62", owner, f"option-{action_index:02d}-stream-{stream}")
                tasks.append({
                    "key": key,
                    "kwargs": {
                        "owner": owner, "root": root, "action_index": action_index,
                        "stream": stream, "output_dir": str(out),
                        "model_sha": "issue62-p0-continuation", "config_digest": DESIGN_SHA,
                        "vocab": VOCAB, "bootstrap": BOOTSTRAP,
                    },
                })
    return tasks


def read_latest_branch(path: Path) -> dict[str, Any] | None:
    runs = []
    for run_path in path.glob("attempt-*/run.json"):
        try:
            row = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        runs.append((run_path.parent.name, row))
    return sorted(runs)[-1][1] if runs else None


def root_rows_and_branches() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    roots = load_roots()
    eligible = [row for row in roots if row["status"] == "ROOT_FOUND"]
    branches = []
    for root in eligible:
        owner = int(root["owner_seed"])
        for action_index in range(int(root["legal_n"])):
            for stream in (0, 1):
                summary = read_latest_branch(branch_path(owner, action_index, stream))
                branches.append({
                    "owner_seed": owner, "action_index": action_index, "stream": stream,
                    "status": summary["status"] if summary else "UNATTEMPTED",
                    "terminal_floor": summary.get("terminal_floor") if summary else None,
                    "process_cpu_seconds": summary.get("process_cpu_seconds") if summary else None,
                    "wall_seconds": summary.get("wall_seconds") if summary else None,
                    "root_verified": summary.get("root_verified", False) if summary else False,
                    "failure": summary.get("failure") if summary else None,
                    "run_path": str(branch_path(owner, action_index, stream).relative_to(HERE)),
                })
    return roots, branches


def percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("cannot compute a percentile from no values")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] if low == high else ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def historical_game_costs() -> list[float]:
    values = []
    for path in (SOURCE / "evidence" / "runs" / "evaluation").glob("**/run.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if row.get("status") == "COMPLETE" and isinstance(row.get("process_cpu_seconds"), (int, float)):
            values.append(float(row["process_cpu_seconds"]))
    return values


def load_completed_branch(root: Mapping[str, Any], action_index: int, stream: int) -> float:
    row = read_latest_branch(branch_path(int(root["owner_seed"]), action_index, stream))
    if row is None or row.get("status") != "COMPLETE" or not row.get("root_verified"):
        raise RuntimeError("pilot branch did not produce a complete parity-verified terminal run")
    return float(row["terminal_floor"])


def run_pilot() -> dict[str, Any]:
    cohort = json.loads((EVIDENCE / "root-cohort.json").read_text(encoding="utf-8"))
    roots = [row for row in load_roots() if row["status"] == "ROOT_FOUND"]
    if int(cohort.get("eligible_root_count", -1)) < 48:
        result = {"status": "SOURCE_COVERAGE_INSUFFICIENT", "eligible_root_count": len(roots)}
        atomic_json(EVIDENCE / "pilot.json", result)
        return result
    if not roots:
        raise RuntimeError("no registered eligible root is available for the replay witness")
    root = roots[0]
    owner = int(root["owner_seed"])
    scores = score_models(root)
    pilot_key = (owner, 0, 0)
    previous = read_latest_branch(branch_path(*pilot_key))
    if previous is None:
        hard_budget_guard()
        path, summary = run_branch(
            owner=owner, root=root, action_index=0, stream=0,
            output_dir=str(branch_path(*pilot_key)),
            model_sha="issue62-p0-continuation", config_digest=DESIGN_SHA,
        )
    else:
        path, summary = branch_path(*pilot_key), previous
    pilot_cpu = float(summary.get("process_cpu_seconds", 0.0))
    hist = historical_game_costs()
    hist_p90 = percentile(hist, 0.90) if hist else 0.0
    tasks = planned_tasks(roots, pilot_key=pilot_key)
    actual = all_in_cpu()
    forecast_per_game = max(hist_p90, pilot_cpu * 1.5, 1.0)
    projected_total = actual + len(tasks) * forecast_per_game + BUILDER_STATIC_RESERVE + REVIEWER_RESERVE
    result = {
        "schema_id": "issue62-replay-witness-v1",
        "status": "PASS" if summary.get("status") == "COMPLETE" and summary.get("root_verified") else "ROOT_REPLAY_BLOCKED",
        "pilot_owner_seed": owner, "pilot_action_index": 0, "pilot_future_stream": 0,
        "pilot_run_status": summary.get("status"), "pilot_failure": summary.get("failure"),
        "pilot_terminal_floor": summary.get("terminal_floor"),
        "pilot_process_cpu_seconds": pilot_cpu, "pilot_wall_seconds": summary.get("wall_seconds"),
        "source_prefix_cpu_seconds": root.get("source_prefix_cpu_seconds"),
        "source_root_parity": {
            key: root[key] for key in ("step_index", "screen", "legal_n", "sampled_p0_index",
                                       "native_checkpoint_fingerprint_sha256", "public_history_sha256",
                                       "public_input_sha256", "location")
        },
        "frozen_policy_probabilities": scores,
        "fixed_branch_tasks_total": len(tasks) + 1,
        "fixed_branch_tasks_remaining_after_pilot": len(tasks),
        "historical_issue60_complete_run_count": len(hist),
        "historical_issue60_all_arm_p90_cpu_seconds": hist_p90,
        "forecast_cpu_seconds_per_remaining_branch": forecast_per_game,
        "actual_all_in_runner_cpu_seconds": actual,
        "builder_static_reserve_cpu_seconds": BUILDER_STATIC_RESERVE,
        "reviewer_reserve_cpu_seconds": REVIEWER_RESERVE,
        "projected_total_cpu_seconds": projected_total,
        "total_hard_cap_cpu_seconds": TOTAL_CPU_CAP,
        "continuation_dispatch_admissible": projected_total < TOTAL_CPU_CAP,
        "pilot_run_path": str(path.relative_to(HERE)),
    }
    if result["status"] == "PASS" and not result["continuation_dispatch_admissible"]:
        result["status"] = "BUDGET_PROJECTION_INSUFFICIENT"
    atomic_json(EVIDENCE / "pilot.json", result)
    return result


def tau_a(scores: Sequence[float], values: Sequence[float]) -> float | None:
    if len(scores) != len(values) or len(scores) < 2:
        return None
    concordant = discordant = 0
    for i in range(len(scores)):
        for j in range(i + 1, len(scores)):
            delta_score = scores[i] - scores[j]
            delta_value = values[i] - values[j]
            if delta_score == 0 or delta_value == 0:
                continue
            if delta_score * delta_value > 0:
                concordant += 1
            else:
                discordant += 1
    total_pairs = len(scores) * (len(scores) - 1) // 2
    return (concordant - discordant) / total_pairs if total_pairs else None


def summarize_analysis(roots: Sequence[Mapping[str, Any]],
                       branches: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in roots if row["status"] == "ROOT_FOUND"]
    if len(roots) != len(OWNER_SEEDS) or len(eligible) < 48:
        return {"status": "SOURCE_COVERAGE_INSUFFICIENT", "eligible_root_count": len(eligible)}
    expected_count = sum(int(row["legal_n"]) * 2 for row in eligible)
    if len(branches) != expected_count or any(
        row["status"] != "COMPLETE" or not row["root_verified"] for row in branches
    ):
        return {
            "status": "INCOMPLETE", "expected_branch_count": expected_count,
            "completed_branch_count": sum(row["status"] == "COMPLETE" and row["root_verified"] for row in branches),
            "incomplete_branch_count": sum(row["status"] != "COMPLETE" or not row["root_verified"] for row in branches),
        }
    owner_rows = []
    for root in eligible:
        owner = int(root["owner_seed"])
        q = []
        for action_index in range(int(root["legal_n"])):
            floors = [load_completed_branch(root, action_index, stream) for stream in (0, 1)]
            q.append(math.fsum(floors) / 2.0)
        observation = __import__("encoder").encode(root["public_input"], VOCAB)
        block_deltas = {}
        block_tau = {}
        for block, (model, _model_sha) in MODELS.items():
            with TORCH.no_grad():
                logits, _value = model(observation)
                residual = [float(value) for value in logits.detach().cpu().tolist()]
            pi = __import__("controllers").policy_distribution(root["p0"], residual)
            block_deltas[block] = math.fsum((pi[i] - root["p0"][i]) * q[i] for i in range(len(q)))
            block_tau[block] = tau_a(residual, q)
        primary = (block_deltas["block1"] + block_deltas["block2"]) / 2.0
        tau_values = [value for value in block_tau.values() if value is not None]
        owner_rows.append({
            "owner_seed": owner, "root_screen": root["screen"],
            "location": root.get("location", {}), "legal_n": root["legal_n"],
            "qhat_terminal_floor_by_action": q,
            "delta_block1": block_deltas["block1"], "delta_block2": block_deltas["block2"],
            "primary_delta": primary,
            "within_root_tau_a_mean_blocks": math.fsum(tau_values) / len(tau_values) if tau_values else None,
        })
    n = len(owner_rows)
    values = {
        "block1": [row["delta_block1"] for row in owner_rows],
        "block2": [row["delta_block2"] for row in owner_rows],
        "primary": [row["primary_delta"] for row in owner_rows],
    }
    observed = {key: math.fsum(rows) / n for key, rows in values.items()}
    rng = random.Random(BOOTSTRAP_SEED)
    boot = {key: [] for key in values}
    for _ in range(BOOTSTRAP_RESAMPLES):
        indexes = [rng.randrange(n) for _ in range(n)]
        for key, rows in values.items():
            boot[key].append(math.fsum(rows[i] for i in indexes) / n)
    ci = {key: [percentile(rows, 0.025), percentile(rows, 0.975)] for key, rows in boot.items()}
    primary = values["primary"]
    tau_values = [row["within_root_tau_a_mean_blocks"] for row in owner_rows
                  if row["within_root_tau_a_mean_blocks"] is not None]
    primary_status = ("LOCAL_DIRECTIONAL_POSITIVE" if ci["primary"][0] > 0 else
                      "LOCAL_DIRECTIONAL_NEGATIVE" if ci["primary"][1] < 0 else
                      "LOCAL_DIRECTION_INCONCLUSIVE")
    return {
        "status": primary_status, "eligible_owner_count": n,
        "completed_terminal_branch_count": len(branches),
        "primary_mean_delta_floor": observed["primary"],
        "primary_95pct_owner_cluster_paired_bootstrap_ci": ci["primary"],
        "block1_mean_delta_floor": observed["block1"],
        "block1_95pct_owner_cluster_paired_bootstrap_ci": ci["block1"],
        "block2_mean_delta_floor": observed["block2"],
        "block2_95pct_owner_cluster_paired_bootstrap_ci": ci["block2"],
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "bootstrap_rng": "python.random.Random(6219999), paired owner index draws",
        "primary_sign_ties": {
            "positive": sum(value > 1e-12 for value in primary),
            "negative": sum(value < -1e-12 for value in primary),
            "tie_abs_le_1e-12": sum(abs(value) <= 1e-12 for value in primary),
        },
        "mean_within_root_tau_a_residual_score_vs_qhat": math.fsum(tau_values) / len(tau_values) if tau_values else None,
        "owner_screen_counts": {name: sum(row["root_screen"] == name for row in owner_rows)
                                for name in ("MAP_SCREEN", "REWARDS")},
        "owner_rows": owner_rows,
        "interpretation_limit": "One forced root action followed by frozen stochastic p0; not repeated residual-policy deployment, full-game efficacy, or a hidden-world conditional expectation at each root.",
    }

def run_dispatch() -> dict[str, Any]:
    from parallel_execution import ParallelGameDispatcher, choose_worker_count, host_capacity
    cohort = json.loads((EVIDENCE / "root-cohort.json").read_text(encoding="utf-8"))
    pilot = json.loads((EVIDENCE / "pilot.json").read_text(encoding="utf-8"))
    roots = [row for row in load_roots() if row["status"] == "ROOT_FOUND"]
    if cohort.get("status") == "SOURCE_COVERAGE_INSUFFICIENT" or len(roots) < 48:
        return {"status": "SOURCE_COVERAGE_INSUFFICIENT", "eligible_root_count": len(roots)}
    if pilot.get("status") != "PASS" or not pilot.get("continuation_dispatch_admissible"):
        return {"status": pilot.get("status", "ROOT_REPLAY_BLOCKED"), "continuation_dispatched": False}
    pilot_owner = int(pilot["pilot_owner_seed"])
    pilot_key = (pilot_owner, int(pilot["pilot_action_index"]), int(pilot["pilot_future_stream"]))
    tasks = planned_tasks(roots, pilot_key=pilot_key)
    hist_costs = historical_game_costs()
    hist_p90 = percentile(hist_costs, 0.90) if hist_costs else 0.0
    pilot_cpu = float(pilot["pilot_process_cpu_seconds"])
    forecast_per_game = max(hist_p90, pilot_cpu * 1.5, 1.0)
    actual_before = all_in_cpu()
    projected = actual_before + len(tasks) * forecast_per_game + BUILDER_STATIC_RESERVE + REVIEWER_RESERVE
    admission = {
        "fixed_branch_tasks_after_pilot": len(tasks),
        "historical_issue60_complete_run_count": len(hist_costs),
        "historical_issue60_all_arm_p90_cpu_seconds": hist_p90,
        "pilot_process_cpu_seconds": pilot_cpu,
        "forecast_cpu_seconds_per_remaining_branch": forecast_per_game,
        "actual_all_in_runner_cpu_seconds_before_dispatch": actual_before,
        "builder_static_reserve_cpu_seconds": BUILDER_STATIC_RESERVE,
        "reviewer_reserve_cpu_seconds": REVIEWER_RESERVE,
        "projected_total_cpu_seconds": projected,
        "total_hard_cap_cpu_seconds": TOTAL_CPU_CAP,
    }
    if projected >= TOTAL_CPU_CAP:
        result = {"status": "BUDGET_PROJECTION_INSUFFICIENT",
                  "continuation_dispatched": False, "admission": admission}
        atomic_json(EVIDENCE / "dispatch.json", result)
        return result
    host = host_capacity()
    rss_sample = max(1, int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024)
    workers = choose_worker_count(
        effective_cpus=max(1, int(host.get("effective_cpu_count") or 1)),
        available_bytes=max(0, int(host.get("effective_memory_available_bytes") or 0)),
        worker_peak_rss_samples=[rss_sample],
    )
    # The pinned dispatcher serializes attempt paths relative to rs.HERE.
    # Use the repository root for this one dispatch, then keep its frozen
    # vocabulary/bootstrap loaders bound to the Issue 60 source directory.
    RS.HERE = SOURCE.parent.parent
    dispatcher = ParallelGameDispatcher(RS, TRACKER, workers["worker_count"])
    dispatcher.raw_run_game = run_branch
    def prepare_worker(kwargs: Mapping[str, Any]) -> dict[str, Any]:
        worker = dict(kwargs)
        worker["vocab"] = RS.Vocabulary(SOURCE / "vocabulary.json")
        worker["bootstrap"] = __import__("copy").deepcopy(BOOTSTRAP)
        return worker
    dispatcher._prepare_worker_kwargs = prepare_worker

    def guard() -> None:
        completed = len(dispatcher.executions)
        unsettled = max(0, len(tasks) - completed)
        actual = all_in_cpu()
        projection = actual + unsettled * forecast_per_game
        active = max(1, TRACKER.active_worker_count())
        if projection >= RUN_CPU_CAP or actual + active * MAX_GAME_CPU_RESERVE >= RUN_CPU_CAP:
            raise RuntimeError("ISSUE62_RESOURCE_BLOCKED: fixed branch cohort cannot be reserved")

    dispatcher._budget_guard = guard
    atomic_json(EVIDENCE / "dispatch-plan.json", {
        "schema_id": "issue62-fixed-branch-dispatch-v1", "design_sha256": DESIGN_SHA,
        "task_count_after_pilot": len(tasks), "worker_selection": workers,
        "host_capacity": host, "admission": admission,
        "fixed_branch_keys": [list(task["key"]) for task in tasks],
    })
    atomic_json(STATUS_PATH, {
        "schema_id": "issue62-stage-status-v1", "stage": "BRANCH_DISPATCH",
        "status": "RUNNING", "fixed_task_count": len(tasks) + 1,
        "pilot_key": list(pilot_key), "all_in_cpu_seconds": all_in_cpu(),
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    error = None
    try:
        dispatcher.run_jobs(tasks)
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
    roots_all, branches = root_rows_and_branches()
    expected = sum(int(row["legal_n"]) * 2 for row in roots)
    complete = len(branches) == expected and all(
        row["status"] == "COMPLETE" and row["root_verified"] for row in branches
    )
    result = {
        "schema_id": "issue62-branch-dispatch-v1",
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "failure": error, "continuation_dispatched": True,
        "expected_branch_count_including_pilot": expected,
        "completed_verified_branch_count": sum(row["status"] == "COMPLETE" and row["root_verified"] for row in branches),
        "branch_status_counts": {name: sum(row["status"] == name for row in branches)
                                 for name in sorted({row["status"] for row in branches})},
        "actual_all_in_runner_cpu_seconds": all_in_cpu(),
        "fixed_task_executions": dispatcher.evidence(),
        "admission": admission, "branches": branches,
    }
    atomic_json(EVIDENCE / "dispatch.json", result)
    atomic_json(EVIDENCE / "branches.json", {"schema_id": "issue62-branch-matrix-v1", "branches": branches})
    if complete:
        analysis = summarize_analysis(roots_all, branches)
        atomic_json(EVIDENCE / "analysis.json", analysis)
        result["analysis_status"] = analysis["status"]
    return result


def init_runtime() -> None:
    global RS, TORCH, TRACKER, PRIOR_CPU, RUNTIME, CONFIG, VOCAB, BOOTSTRAP, MODELS, DESIGN_SHA
    sys.path.insert(0, str(SOURCE))
    import subprocess
    import torch
    import run_study as rs
    from resource_amendment_r1 import verify_resource_chain
    from public_history_execution import install
    from parallel_execution import ProcessCpuTracker
    TORCH, RS = torch, rs
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    CONFIG = json.loads(rs.CONFIG_PATH.read_text(encoding="utf-8"))
    rs.validate_config(CONFIG)
    repo_root = SOURCE.parent.parent
    base = subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True).strip()
    if base != SOURCE_COMMIT:
        raise RuntimeError(f"frozen Issue 60 source commit mismatch: {base}")
    chain = verify_resource_chain(SOURCE, require_gate_seal=False)
    RUNTIME = rs.verify_runtime(CONFIG)
    VOCAB = rs.Vocabulary(SOURCE / "vocabulary.json")
    if VOCAB.sha256 != CONFIG["sources"]["vocabulary_sha256"]:
        raise RuntimeError("frozen vocabulary hash mismatch")
    BOOTSTRAP, _ = rs.load_bootstrap(SOURCE / CONFIG["bootstrap"]["checkpoint"])
    MODELS = {block: rs._load_final_model(block, VOCAB, rs.config_sha())
              for block in ("block1", "block2")}
    install()
    design = {
        "issue": 62, "study_only": True, "source_commit": SOURCE_COMMIT,
        "owner_seeds": list(OWNER_SEEDS),
        "root_rule": "1+SHA256(issue62-root|owner) mod 6",
        "prefix_stream": "SHA256(issue62-prefix|owner)",
        "future_streams": ["SHA256(issue62-future|owner|0)", "SHA256(issue62-future|owner|1)"],
        "branches_per_legal_action": 2, "max_steps": RS.MAX_STEPS,
        "bootstrap_seed": BOOTSTRAP_SEED, "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "sources": CONFIG["sources"], "config_sha256": RS.config_sha(),
    }
    DESIGN_SHA = sha256(canonical_bytes(design))
    ledger = read_jsonl(LEDGER_PATH)
    PRIOR_CPU = math.fsum(float(row.get("cpu_seconds", 0.0)) for row in ledger)
    TRACKER = ProcessCpuTracker.start(MODULE_CPU_START)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_id": "issue62-study-manifest-v1", "study": "STUDY_ONLY",
        "source_commit": SOURCE_COMMIT, "design_sha256": DESIGN_SHA,
        "study_runner_sha256": sha256(Path(__file__).read_bytes()),
        "source_runtime": RUNTIME, "resource_chain": chain,
        "source_code_sha256": {name: sha256((SOURCE / name).read_bytes()) for name in (
            "run_study.py", "controllers.py", "adapter_v47.py", "bootstrap_v47.py",
            "encoder.py", "model.py", "public_history_execution.py", "parallel_execution.py",
            "attempt_lifecycle.py", "resource_amendment_r1.py", "resource_amendment_r2.py",
        )},
        "checkpoint_sha256": {block: model_sha for block, (_model, model_sha) in MODELS.items()},
        "all_in_cpu_cap_seconds": TOTAL_CPU_CAP,
        "reviewer_reserve_cpu_seconds": REVIEWER_RESERVE,
        "builder_static_reserve_cpu_seconds": BUILDER_STATIC_RESERVE,
    }
    manifest_path = EVIDENCE / "manifest.json"
    if manifest_path.is_file():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old.get("design_sha256") != DESIGN_SHA or old.get("source_commit") != SOURCE_COMMIT:
            raise RuntimeError("existing Issue 62 evidence belongs to another source/design")
    atomic_json(manifest_path, manifest)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("roots", "pilot", "dispatch", "all"), default="all")
    args = parser.parse_args()
    init_runtime()
    failure = None
    stage = args.stage.upper()
    try:
        atomic_json(STATUS_PATH, {
            "schema_id": "issue62-stage-status-v1", "stage": stage,
            "status": "RUNNING", "design_sha256": DESIGN_SHA,
            "all_in_cpu_seconds": all_in_cpu(),
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        if args.stage in {"roots", "all"}:
            run_roots()
        if args.stage in {"pilot", "all"}:
            pilot = run_pilot()
            print("PILOT " + json.dumps(pilot, sort_keys=True), flush=True)
            if pilot.get("status") != "PASS":
                atomic_json(STATUS_PATH, {
                    "schema_id": "issue62-stage-status-v1", "stage": "PILOT",
                    "status": pilot.get("status"), "all_in_cpu_seconds": all_in_cpu(),
                })
                return 2
        if args.stage in {"dispatch", "all"}:
            result = run_dispatch()
            public = {k: v for k, v in result.items()
                      if k not in {"branches", "fixed_task_executions"}}
            print("DISPATCH " + json.dumps(public, sort_keys=True), flush=True)
            if result.get("status") != "COMPLETE":
                atomic_json(STATUS_PATH, {
                    "schema_id": "issue62-stage-status-v1", "stage": "DISPATCH",
                    "status": result.get("status"), "all_in_cpu_seconds": all_in_cpu(),
                    "failure": result.get("failure"),
                })
                return 2
        atomic_json(STATUS_PATH, {
            "schema_id": "issue62-stage-status-v1", "stage": stage,
            "status": "COMPLETE", "all_in_cpu_seconds": all_in_cpu(),
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        return 0
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        delta = current_cpu()
        append_jsonl(LEDGER_PATH, {
            "schema_id": "issue62-cpu-ledger-v1", "stage": stage,
            "cpu_seconds": delta, "prior_recorded_cpu_seconds": PRIOR_CPU,
            "all_in_runner_cpu_seconds": PRIOR_CPU + delta,
            "wall_seconds": max(0.0, time.monotonic() - main.started_wall),
            "failure": failure, "ended_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        state = json.loads(STATUS_PATH.read_text(encoding="utf-8")) if STATUS_PATH.is_file() else {}
        if state.get("status") == "RUNNING":
            atomic_json(STATUS_PATH, {
                "schema_id": "issue62-stage-status-v1", "stage": stage,
                "status": "FAILED" if failure else "COMPLETE",
                "failure": failure, "all_in_cpu_seconds": all_in_cpu(),
            })


if __name__ == "__main__":
    main.started_wall = time.monotonic()
    raise SystemExit(main())
