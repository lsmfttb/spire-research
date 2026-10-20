from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib
import importlib.util
import json
import math
import os
import random
import resource
import subprocess
import sys
import time
import traceback
from contextlib import nullcontext
from dataclasses import dataclass
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from sts_combat_rl.sim.action_space import ActionSpaceConfig
from sts_combat_rl.sim.controlled_run import ControlledRun, ControlledRunStep, execute_controlled_run
from sts_combat_rl.sim.controller_contract import ControllerDecision, ControllerProvenance
from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions
from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
from sts_combat_rl.sim.non_combat_policy import ExpertNonCombatDriver
from sts_combat_rl.sim.online_controller import is_battle_state
from sts_combat_rl.sim.oracle_search import (
    build_oracle_search_report,
    oracle_search_controller_metadata,
    select_oracle_root_action,
)

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
IDENTITY = CONFIG["identity"]
BATTLE = CONFIG["battle"]
TRAJECTORY = CONFIG["trajectory_search"]
PROPOSAL = TRAJECTORY["candidate_proposal"]
MAX_CPU = CONFIG["resources"]["builder_cpu_ceiling_cpu_seconds"]
SETUP_ALLOWANCE = CONFIG["resources"]["preflight_setup_allowance_cpu_seconds"]
REVIEW_RESERVE = CONFIG["resources"]["independent_review_reserve_cpu_seconds"]
EXPERT_SEED = 640001
MAX_STEPS = 4096
NATIVE_API = BATTLE["api"]
NATIVE_PATCH = BATTLE["patch_identity"]
ACTION_SPACE = ActionSpaceConfig.include_all()
STOP_FILE: Path | None = None
_LAST_STOP_POLL_CPU: float | None = None
_RUNTIME_EXECUTION: Any | None = None
_RUNTIME_MODULE: Any | None = None
ENGINEERING_LIMITS: dict[str, int] | None = None
ENGINEERING_SEEDS = (646901, 646902, 646903, 646904)
ENGINEERING_BUILDER_SOFT_STOP_CPU = 16000.0
ENGINEERING_BUILDER_HARD_CPU = 18000.0
ENGINEERING_TOTAL_CPU = 20000.0
ENGINEERING_REVIEW_RESERVE_CPU = 2000.0
ENGINEERING_TRACE_BATCH_LINES = 128
ISSUE65_PREVIEW_BUDGET_FILE = "issue65-preview-budget-state.json"
LEGACY_ISSUE64_BUDGET_FILE = "budget-state.json"


class StudyFailure(RuntimeError):
    pass


class CandidateRejected(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_identity(path: str) -> tuple[str, str]:
    root = Path(path).expanduser().resolve()
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True).strip()
    return commit, status


def validate_environment(native_source: str, stsrl_source: str) -> dict[str, Any]:
    native_commit, native_dirty = git_identity(native_source)
    stsrl_commit, stsrl_dirty = git_identity(stsrl_source)
    if native_commit != IDENTITY["native_commit"]:
        raise StudyFailure(f"native source commit mismatch: {native_commit}")
    if stsrl_commit != IDENTITY["stsrl_source_commit"]:
        raise StudyFailure(f"STSRL source commit mismatch: {stsrl_commit}")
    if native_dirty or stsrl_dirty:
        raise StudyFailure("pinned source checkout is dirty")
    if sys.version_info[:2] != (3, 14):
        raise StudyFailure(f"Python 3.14 required, got {sys.version.split()[0]}")
    module = importlib.import_module("slaythespire")
    module_path = Path(module.__file__).resolve()
    module_sha = sha256_file(module_path)
    if module_sha != IDENTITY["native_binary_sha256"]:
        raise StudyFailure(f"native extension SHA-256 mismatch: {module_sha}")
    return {
        "native_repository": IDENTITY["native_repository"],
        "native_branch": IDENTITY["native_branch"],
        "native_source_commit": native_commit,
        "native_source_clean": True,
        "native_binary_path": str(module_path),
        "native_binary_sha256": module_sha,
        "stsrl_source_commit": stsrl_commit,
        "stsrl_source_clean": True,
        "python": sys.version.split()[0],
    }


def ensure_output_is_external(output: Path) -> None:
    resolved = output.expanduser().resolve()
    if ROOT == resolved or ROOT in resolved.parents:
        raise StudyFailure("study artifacts must be outside the disposable worktree")


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with runtime_phase("artifact_json_serialize_write"):
        tmp.write_text(canonical(payload) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def append_jsonl(path: Path, payload: Mapping[str, Any], *, sync: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with runtime_phase("boundary_ledger_serialize_write"):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(canonical(payload) + "\n")
            handle.flush()
            if sync:
                os.fsync(handle.fileno())


class JsonlWriter:
    def __init__(self, path: Path, *, buffered: bool = False, batch_lines: int = 128):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a", encoding="utf-8")
        self.buffered = buffered
        self.batch_lines = max(1, int(batch_lines))
        self.pending: list[str] = []

    def write(self, payload: Mapping[str, Any], *, sync: bool = False) -> None:
        with runtime_phase("trace_serialize"):
            line = canonical(payload) + "\n"
        if self.buffered and not sync:
            self.pending.append(line)
            if len(self.pending) >= self.batch_lines:
                self.flush(sync=False)
            return
        self.pending.append(line)
        self.flush(sync=sync)

    def flush(self, *, sync: bool = False) -> None:
        if not self.pending and not sync:
            return
        with runtime_phase("trace_write_flush"):
            if self.pending:
                self.handle.write("".join(self.pending))
                self.pending.clear()
            self.handle.flush()
        if sync:
            with runtime_phase("trace_fsync"):
                os.fsync(self.handle.fileno())

    def close(self) -> None:
        self.flush(sync=True)
        self.handle.close()


def check_stop() -> None:
    global _LAST_STOP_POLL_CPU
    if STOP_FILE is None:
        return
    now = time.process_time()
    if _LAST_STOP_POLL_CPU is not None and now - _LAST_STOP_POLL_CPU < 0.25:
        return
    _LAST_STOP_POLL_CPU = now
    if STOP_FILE.exists():
        raise StudyFailure("aggregate Builder CPU ceiling reached; stopped at a durable boundary")


def runtime_phase(name: str):
    if _RUNTIME_MODULE is None:
        return nullcontext()
    return _RUNTIME_MODULE.phase(name)


def derive_seed(*parts: Any) -> int:
    return int.from_bytes(hashlib.sha256(canonical(parts).encode("utf-8")).digest()[:4], "big")


def action_identity(action: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "action_id": action.get("action_id"),
        "occurrence": action.get("occurrence"),
        "kind": str(action.get("kind", "")),
        "label": str(action.get("label", "")),
        "stable_id": str(action.get("stable_id", "")),
    }


def same_action(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return canonical(action_identity(left)) == canonical(action_identity(right))


def find_identity_index(actions: Sequence[Any], target: Mapping[str, Any]) -> int:
    identities = action_identity_dicts_for_actions(actions)
    matches = [i for i, value in enumerate(identities) if same_action(value, target)]
    if len(matches) != 1:
        raise CandidateRejected("b2 candidate rejected: saved native action identity is stale or ambiguous")
    return matches[0]


def choose_b2_root(
    root_identities: Sequence[Mapping[str, Any]],
    *,
    baseline_root: int,
    proposed_root: int,
    candidate_index: int,
    call_index: int,
    parent: Mapping[str, Any] | None,
) -> tuple[int, int | None, list[dict[str, Any]], str | None]:
    """Choose a root and only retain a suffix when its parent's root is retained."""
    if not root_identities or not 0 <= baseline_root < len(root_identities):
        raise StudyFailure("candidate Expert root is outside the current legal root actions")
    if not 0 <= proposed_root < len(root_identities):
        raise StudyFailure("proposed root is outside the current legal root actions")
    fresh_roots = [i for i in range(len(root_identities)) if i != baseline_root]
    if not fresh_roots:
        raise StudyFailure("B2 requires at least one root action different from the candidate Expert action")
    fresh_root = fresh_roots[(candidate_index + call_index) % len(fresh_roots)]

    if parent is not None:
        matches = [
            i for i, identity in enumerate(root_identities)
            if same_action(identity, parent["root_identity"])
        ]
        if len(matches) != 1:
            raise StudyFailure("elite parent root is not uniquely present in the current legal root actions")
        parent_root = matches[0]
        if parent_root != baseline_root:
            return (
                parent_root,
                int(parent["candidate_index"]),
                list(parent["materialized_overrides"]),
                None,
            )
        return fresh_root, None, [], "parent_root_matches_candidate_expert"

    if proposed_root == baseline_root:
        return fresh_root, None, [], "proposed_root_matches_candidate_expert"
    return proposed_root, None, [], None


def terminal_fields(run: ControlledRun) -> dict[str, Any]:
    if not run.terminal:
        raise StudyFailure("run is incomplete and cannot be scored")
    final = dict(run.final_raw)
    outcome = str(final.get("outcome", "UNKNOWN"))
    floor_value = final.get("floor_num")
    if isinstance(floor_value, bool) or not isinstance(floor_value, (int, float)):
        raise StudyFailure("native terminal floor is unavailable")
    floor = int(floor_value)
    act_value = final.get("act")
    act = int(act_value) if isinstance(act_value, (int, float)) and not isinstance(act_value, bool) else None
    return {
        "outcome": outcome,
        "terminal_floor": floor,
        "terminal_act": act,
        "heart_victory": outcome == "PLAYER_VICTORY" and act == 4,
        "act2_plus": act is not None and act >= 2,
        "act3_plus": act is not None and act >= 3,
    }


def terminal_fitness(fields: Mapping[str, Any]) -> tuple[int, int]:
    return (1 if fields["heart_victory"] else 0, int(fields["terminal_floor"]))


def selfcheck_b2_root_selection() -> None:
    roots = [
        {
            "action_id": f"game:{index}",
            "occurrence": 0,
            "kind": "map",
            "label": f"map-{index}",
            "stable_id": f"root-{index}",
        }
        for index in range(3)
    ]
    suffix = [{"eligible_ordinal": 3, "action_identity": roots[2]}]
    parent = {
        "candidate_index": 7,
        "root_identity": roots[0],
        "materialized_overrides": suffix,
    }

    root, parent_index, inherited, discard_reason = choose_b2_root(
        roots,
        baseline_root=0,
        proposed_root=1,
        candidate_index=1,
        call_index=0,
        parent=parent,
    )
    if root == 0 or parent_index is not None or inherited or discard_reason != "parent_root_matches_candidate_expert":
        raise StudyFailure("B2 root-collision self-check retained a parent suffix or Expert root")

    root, parent_index, inherited, discard_reason = choose_b2_root(
        roots,
        baseline_root=2,
        proposed_root=1,
        candidate_index=2,
        call_index=0,
        parent=parent,
    )
    if root != 0 or parent_index != 7 or inherited != suffix or discard_reason is not None:
        raise StudyFailure("B2 non-colliding parent self-check did not preserve its root and suffix")

    root, parent_index, inherited, discard_reason = choose_b2_root(
        roots,
        baseline_root=1,
        proposed_root=1,
        candidate_index=0,
        call_index=0,
        parent=None,
    )
    if root == 1 or parent_index is not None or inherited or discard_reason != "proposed_root_matches_candidate_expert":
        raise StudyFailure("B2 root-proposal self-check selected the Expert root")


def legal_action_record(actions: Sequence[Any]) -> list[dict[str, Any]]:
    return [action_identity(value) for value in action_identity_dicts_for_actions(actions)]


def battle_decision(adapter: Any, snapshot: Any, actions: Sequence[Any], context: Any) -> tuple[int, dict[str, Any]]:
    started = time.perf_counter()
    raw = adapter.battle_search_v2(
        snapshot,
        simulations=BATTLE["simulations"],
        include_potions=BATTLE["include_potions"],
        policy_prior_callback=None,
        leaf_value_callback=None,
    )
    report = build_oracle_search_report(
        raw,
        actions,
        context,
        expected_native_api=NATIVE_API,
        expected_patch_identity=NATIVE_PATCH,
        wall_clock_time_s=time.perf_counter() - started,
    )
    if not report.search_ok:
        raise StudyFailure("native Search-v2 root mapping failed: " + "; ".join(report.problems))
    target = select_oracle_root_action(report, selection_rule=BATTLE["root_selection"])
    return target.legal_action_index, {
        "search_api": NATIVE_API,
        "patch_identity": NATIVE_PATCH,
        "simulations": BATTLE["simulations"],
        "include_potions": BATTLE["include_potions"],
        "root_selection": BATTLE["root_selection"],
        "target": target.to_dict(),
        "report": oracle_search_controller_metadata(report, target),
    }


class CheckpointAdapter:
    """Restore an exact LightSpeed checkpoint for one full controlled continuation."""

    def __init__(self, base: LightSpeedAdapter, checkpoint: Any):
        self.base = base
        self.checkpoint = checkpoint

    def reset(self, seed: int | None = None) -> Any:
        del seed
        return self.base.restore_checkpoint(self.checkpoint)

    def legal_actions(self, snapshot: Any) -> list[Any]:
        return self.base.legal_actions(snapshot)

    def step(self, action: Any) -> Any:
        return self.base.step(action)

    def battle_search_v2(self, snapshot: Any, **kwargs: Any) -> dict[str, Any]:
        return self.base.battle_search_v2(snapshot, **kwargs)


@dataclass
class CandidatePlan:
    candidate_index: int
    root_identity: dict[str, Any]
    parent_candidate_index: int | None
    inherited_overrides: list[dict[str, Any]]
    new_target: str


class BranchController:
    def __init__(
        self,
        *,
        arm: str,
        owner_seed: int,
        call_index: int,
        candidate_index: int,
        policy_seed: int,
        root_identity: dict[str, Any] | None,
        plan: CandidatePlan | None,
        branch_writer: JsonlWriter,
        branch_id: str,
        expert_only: bool = False,
    ):
        self.arm = arm
        self.owner_seed = owner_seed
        self.call_index = call_index
        self.candidate_index = candidate_index
        self.policy_seed = policy_seed
        self.root_identity = root_identity
        self.plan = plan
        self.branch_writer = branch_writer
        self.branch_id = branch_id
        self.expert_only = expert_only
        self.expert = ExpertNonCombatDriver(seed=EXPERT_SEED)
        self.provenance = ControllerProvenance(
            kind="study_trajectory_continuation",
            name=f"{arm.lower()}_branch_v1",
            config={
                "study_id": CONFIG["study_id"],
                "information_regime": "full_simulator_state_oracle_like",
                "owner_seed": owner_seed,
                "planning_call": call_index,
                "candidate_index": candidate_index,
                "policy_seed": policy_seed,
                "root_identity": root_identity,
                "candidate_plan": None if plan is None else {
                    "parent_candidate_index": plan.parent_candidate_index,
                    "inherited_overrides": plan.inherited_overrides,
                    "new_target": plan.new_target,
                },
                "battle_search": BATTLE,
                "source_policy": "expert_non_combat_v1",
            },
        )
        self.eligible_ordinal = 0
        self.future_changed = 0
        self.root_changed = False
        self.map_direction_changed = False
        self.card_reward_choice_changed = False
        self.materialized_overrides: list[dict[str, Any]] = []
        self.counts: Counter[str] = Counter()
        self.pending: dict[str, Any] = {}

    def reset_for_run(self, seed: int | None) -> None:
        self.expert.reset_for_run(seed)

    def select_action(
        self,
        adapter: Any,
        snapshot: Any,
        actions: Sequence[Any],
        context: Any,
        step_index: int,
    ) -> ControllerDecision:
        check_stop()
        battle = is_battle_state(snapshot.raw, context.screen_state)
        identities = action_identity_dicts_for_actions(actions)
        if battle:
            selected, battle_meta = battle_decision(adapter, snapshot, actions, context)
            role = "unchanged_native_search_v2"
            self.counts[f"{context.screen_state}:{role}"] += 1
        else:
            baseline = self.expert.select_action(context)
            selected = baseline.legal_action_index
            role = "expert_non_combat_v1"
            meaningful = len(context.eligible_action_indices) > 1
            ordinal = self.eligible_ordinal
            if meaningful:
                self.eligible_ordinal += 1
                if ordinal == 0 and self.root_identity is not None:
                    selected = find_identity_index(actions, self.root_identity)
                    self.root_changed = not same_action(identities[selected], identities[baseline.legal_action_index])
                    role = "b1_fixed_root" if self.arm == "B1" else "b2_root_mutation"
                    if self.arm == "B2" and not self.root_changed:
                        raise CandidateRejected("b2 candidate rejected: root did not change the Expert action")
                    self._record_goal_change(
                        context.screen_state,
                        identities[baseline.legal_action_index],
                        identities[selected],
                    )
                elif ordinal > 0 and not self.expert_only and self.arm == "B2":
                    selected, role = self._choose_b2_future_action(
                        context, actions, identities, baseline.legal_action_index, ordinal
                    )
                if role != "expert_non_combat_v1":
                    self.counts[f"{context.screen_state}:{role}"] += 1
            else:
                self.counts[f"{context.screen_state}:one_action_confirmation"] += 1
            battle_meta = None
        self.pending = {
            "branch_id": self.branch_id,
            "step_index": step_index,
            "screen_state": str(context.screen_state),
            "controller_role": role,
            "chosen_action": action_identity(identities[selected]),
            "legal_actions": legal_action_record(actions),
            "battle_search": battle_meta,
            "eligible_ordinal": self.eligible_ordinal - 1
            if not battle and len(context.eligible_action_indices) > 1 else None,
            "root_changed": self.root_changed,
        }
        return ControllerDecision(
            selected_index=selected,
            provenance=self.provenance,
            reason=role,
            metadata={"study_id": CONFIG["study_id"], "branch_id": self.branch_id},
        )

    def _choose_b2_future_action(
        self,
        context: Any,
        actions: Sequence[Any],
        identities: Sequence[Mapping[str, Any]],
        baseline_index: int,
        ordinal: int,
    ) -> tuple[int, str]:
        assert self.plan is not None
        decision_cap = int(CONFIG["non_combat"]["meaningful_decisions_per_owner_max"])
        if self.call_index + ordinal >= decision_cap:
            return baseline_index, "expert_non_combat_v1"
        inherited = {int(value["eligible_ordinal"]): value for value in self.plan.inherited_overrides}
        if ordinal in inherited:
            saved = inherited[ordinal]
            if str(saved.get("screen_state")) != str(context.screen_state):
                raise CandidateRejected("b2 candidate rejected: inherited action reached a different screen")
            selected = find_identity_index(actions, saved["action_identity"])
            if same_action(identities[selected], identities[baseline_index]):
                raise CandidateRejected("b2 candidate rejected: inherited action no longer changes Expert")
            self.materialized_overrides.append(dict(saved))
            self.future_changed += 1
            self._record_goal_change(context.screen_state, identities[baseline_index], identities[selected])
            return selected, "b2_replayed_elite_override"

        target = self.plan.new_target
        screen = str(context.screen_state).upper()
        eligible = list(context.eligible_action_indices)
        candidates: list[int] = []
        if target == "ANY" and ordinal == 1:
            candidates = [i for i in eligible if i != baseline_index]
        elif target == "MAP" and screen == "MAP_SCREEN":
            candidates = [i for i in eligible if i != baseline_index and str(actions[i].kind) == "map"]
        elif target == "REWARD" and screen == "REWARDS":
            candidates = [
                i for i in eligible
                if str(actions[i].kind) == "reward_card"
                and (i != baseline_index or str(actions[baseline_index].kind) != "reward_card")
            ]
            if len(candidates) == 1 and str(actions[baseline_index].kind) == "reward_card":
                candidates = []
        if not candidates:
            return baseline_index, "expert_non_combat_v1"
        proposal_seed = derive_seed(
            "issue64-proposal-v1", self.owner_seed, self.call_index,
            self.candidate_index, ordinal, target,
        )
        selected = random.Random(proposal_seed).choice(candidates)
        self.materialized_overrides.append({
            "eligible_ordinal": ordinal,
            "screen_state": str(context.screen_state),
            "action_identity": action_identity(identities[selected]),
            "target": target,
        })
        self.future_changed += 1
        self._record_goal_change(context.screen_state, identities[baseline_index], identities[selected])
        return selected, "b2_new_mutation"

    def _record_goal_change(
        self, screen: str, baseline: Mapping[str, Any], chosen: Mapping[str, Any]
    ) -> None:
        if same_action(baseline, chosen):
            return
        if (
            str(screen).upper() == "MAP_SCREEN"
            and str(baseline.get("kind")) == "map"
            and str(chosen.get("kind")) == "map"
            and baseline.get("stable_id") != chosen.get("stable_id")
        ):
            self.map_direction_changed = True
        if (
            str(screen).upper() == "REWARDS"
            and str(baseline.get("kind")) == "reward_card"
            and str(chosen.get("kind")) == "reward_card"
            and baseline.get("stable_id") != chosen.get("stable_id")
        ):
            self.card_reward_choice_changed = True

    def after_transition(self, step: ControlledRunStep) -> None:
        row = dict(self.pending)
        row.update({
            "next_screen_state": step.next_screen_state,
            "terminal_after_step": step.terminal_after_step,
            "floor": step.floor,
            "next_floor": step.next_floor,
            "battle_outcome": step.next_battle_outcome,
        })
        self.branch_writer.write(row)
        self.pending = {}


class StudyController:
    def __init__(
        self,
        *,
        arm: str,
        owner_seed: int,
        attempt_dir: Path,
        ledger: JsonlWriter,
        actual_writer: JsonlWriter,
        branch_writer: JsonlWriter,
        preflight: bool,
        witness_checkpoints: dict[str, Any] | None = None,
        engineering_limits: Mapping[str, int] | None = None,
    ):
        self.arm = arm
        self.owner_seed = owner_seed
        self.attempt_dir = attempt_dir
        self.ledger = ledger
        self.actual_writer = actual_writer
        self.branch_writer = branch_writer
        self.preflight = preflight
        self.engineering_limits = dict(engineering_limits or {})
        self.planning_call_limit = int(
            self.engineering_limits.get("planning_calls_max", CONFIG["non_combat"]["planning_calls_max"])
        )
        self.continuation_quota = int(
            self.engineering_limits.get(
                "complete_terminal_continuations_per_call_max",
                TRAJECTORY["complete_terminal_continuations_per_call_max"],
            )
        )
        self.retry_attempt_limit = int(
            self.engineering_limits.get("retry_attempts_per_call_max", TRAJECTORY["retry_attempts_per_call_max"])
        )
        self.witness_checkpoints = witness_checkpoints if witness_checkpoints is not None else {}
        self.expert = ExpertNonCombatDriver(seed=EXPERT_SEED)
        self.provenance = ControllerProvenance(
            kind="study_actual_controller",
            name=f"issue64_{arm.lower()}_v1",
            config={
                "study_id": CONFIG["study_id"],
                "information_regime": "full_simulator_state_oracle_like" if arm != "B0" else "normal_public_policy",
                "owner_seed": owner_seed,
                "max_planning_calls": self.planning_call_limit if arm != "B0" else 0,
                "eligible_decision_cap": CONFIG["non_combat"]["meaningful_decisions_per_owner_max"],
                "complete_continuations_per_call": self.continuation_quota if arm != "B0" else 0,
                "engineering_limits": self.engineering_limits or None,
                "battle_search": BATTLE,
                "source_policy": "expert_non_combat_v1",
                "native_source": {
                    "repository": IDENTITY["native_repository"],
                    "branch": IDENTITY["native_branch"],
                    "commit": IDENTITY["native_commit"],
                    "binary_sha256": IDENTITY["native_binary_sha256"],
                },
            },
        )
        self.eligible_seen = 0
        self.planning_calls = 0
        self.pending: dict[str, Any] = {}
        self.pending_plan: Mapping[str, Any] | None = None
        self.actual_counts: Counter[str] = Counter()
        self.branch_coverage = Counter()
        self.search_summaries: list[dict[str, Any]] = []
        self.max_act = 0
        self.max_floor = 0
        self.boss_reached = False
        self.room_types: set[str] = set()
        self._active_adapter: LightSpeedAdapter | None = None

    def reset_for_run(self, seed: int | None) -> None:
        self.expert.reset_for_run(seed)

    def select_action(
        self,
        adapter: LightSpeedAdapter,
        snapshot: Any,
        actions: Sequence[Any],
        context: Any,
        step_index: int,
    ) -> ControllerDecision:
        check_stop()
        battle = is_battle_state(snapshot.raw, context.screen_state)
        if battle:
            selected, meta = battle_decision(adapter, snapshot, actions, context)
            role = "unchanged_native_search_v2"
        else:
            baseline = self.expert.select_action(context)
            selected = baseline.legal_action_index
            role = "expert_non_combat_v1"
            self.pending_plan = None
            if len(context.eligible_action_indices) > 1:
                self.eligible_seen += 1
                # Reserve the eighth eligible point as a shared continuation.
                # A joint trajectory at a root with no later in-cap decision cannot
                # meet the B2 intervention definition.
                if self.arm in ("B1", "B2") and self.planning_calls < self.planning_call_limit:
                    call_index = self.planning_calls
                    self.planning_calls += 1
                    checkpoint = adapter.capture_checkpoint(snapshot)
                    root_identities = action_identity_dicts_for_actions(actions)
                    summary = self._plan_call(
                        adapter, snapshot, context, actions, checkpoint,
                        root_identities, call_index,
                    )
                    selected = find_identity_index(actions, summary["selected_root_identity"])
                    role = f"{self.arm.lower()}_selected_actual_root"
                    self.pending_plan = summary
            meta = None
        if self.pending_plan is not None:
            self.search_summaries.append(dict(self.pending_plan))
        identities = action_identity_dicts_for_actions(actions)
        witness_screen = str(context.screen_state).upper()
        if self.arm == "B0" and self.preflight and witness_screen in self.witness_checkpoints:
            self.witness_checkpoints[witness_screen]["source_action_identity"] = action_identity(
                identities[selected]
            )
        self.actual_counts[f"{context.screen_state}:{role}"] += 1
        self.pending = {
            "owner_seed": self.owner_seed,
            "arm": self.arm,
            "step_index": step_index,
            "screen_state": str(context.screen_state),
            "controller_role": role,
            "chosen_action": action_identity(identities[selected]),
            "legal_actions": legal_action_record(actions),
            "battle_search": meta if battle else None,
            "planning_call": dict(self.pending_plan) if self.pending_plan is not None else None,
        }
        return ControllerDecision(
            selected_index=selected,
            provenance=self.provenance,
            reason=role,
            metadata={"study_id": CONFIG["study_id"], "planning_calls_used": self.planning_calls},
        )

    def _plan_call(
        self,
        adapter: LightSpeedAdapter,
        snapshot: Any,
        context: Any,
        actions: Sequence[Any],
        checkpoint: Any,
        root_identities: Sequence[Mapping[str, Any]],
        call_index: int,
    ) -> dict[str, Any]:
        check_stop()
        if len(root_identities) < 2:
            raise StudyFailure("planning call was counted with fewer than two eligible actions")
        call_id = f"{self.owner_seed}-{self.arm}-{call_index}"
        call_dir = self.attempt_dir / "calls" / call_id
        call_dir.mkdir(parents=True, exist_ok=True)
        self.ledger.write({
            "event": "planning_call_started",
            "owner_seed": self.owner_seed,
            "arm": self.arm,
            "call_index": call_index,
            "root_screen": str(context.screen_state),
            "root_legal_actions": [action_identity(x) for x in root_identities],
            "checkpoint_id": str(getattr(checkpoint, "checkpoint_id", "")),
            "cpu_seconds": time.process_time(),
        }, sync=True)
        results: list[dict[str, Any]] = []
        completed = 0
        attempts = 0
        max_attempts = self.retry_attempt_limit
        target_completed = self.continuation_quota
        elite_size = max(1, math.ceil(target_completed * TRAJECTORY["elite_fraction"]))
        while completed < target_completed and attempts < max_attempts:
            check_stop()
            candidate_index = attempts
            policy_seed = derive_seed("issue64-expert-stream-v1", self.owner_seed, call_index, candidate_index)
            root_idx = candidate_index % len(root_identities)
            parent_index: int | None = None
            inherited: list[dict[str, Any]] = []
            parent_discard_reason: str | None = None
            baseline_root_identity: dict[str, Any] | None = None
            target_cycle = PROPOSAL["future_target_cycle"]
            new_target = target_cycle[(candidate_index + call_index) % len(target_cycle)]
            if self.arm == "B2":
                baseline_driver = ExpertNonCombatDriver(seed=EXPERT_SEED)
                baseline_driver.reset_for_run(policy_seed)
                baseline_root = baseline_driver.select_action(context).legal_action_index
                baseline_root_identity = action_identity(root_identities[baseline_root])
                parents = sorted(
                    (row for row in results if row.get("joint_valid")),
                    key=lambda row: (-row["fitness"][0], -row["fitness"][1], row["candidate_index"]),
                )[:elite_size]
                parent: Mapping[str, Any] | None = None
                if parents:
                    parent = parents[(candidate_index + call_index) % len(parents)]
                root_idx, parent_index, inherited, parent_discard_reason = choose_b2_root(
                    root_identities,
                    baseline_root=baseline_root,
                    proposed_root=root_idx,
                    candidate_index=candidate_index,
                    call_index=call_index,
                    parent=parent,
                )
                plan = CandidatePlan(
                    candidate_index=candidate_index,
                    root_identity=action_identity(root_identities[root_idx]),
                    parent_candidate_index=parent_index,
                    inherited_overrides=inherited,
                    new_target=new_target,
                )
                root_identity = plan.root_identity
            else:
                plan = None
                root_identity = action_identity(root_identities[root_idx])
            branch_id = f"{call_id}-candidate-{candidate_index:03d}"
            self.ledger.write({
                "event": "continuation_started",
                "branch_id": branch_id,
                "candidate_index": candidate_index,
                "parent_candidate_index": parent_index,
                "parent_discard_reason": parent_discard_reason,
                "baseline_root_identity": baseline_root_identity,
                "root_identity": root_identity,
                "inherited_override_count": len(inherited),
                "new_target": new_target if self.arm == "B2" else None,
                "cpu_seconds": time.process_time(),
            }, sync=True)
            attempts += 1
            branch_controller = BranchController(
                arm=self.arm,
                owner_seed=self.owner_seed,
                call_index=call_index,
                candidate_index=candidate_index,
                policy_seed=policy_seed,
                root_identity=root_identity,
                plan=plan,
                branch_writer=self.branch_writer,
                branch_id=branch_id,
            )
            branch_adapter = CheckpointAdapter(adapter, checkpoint)
            started = time.process_time()
            try:
                run = execute_controlled_run(
                    branch_adapter,
                    branch_controller,
                    seed=policy_seed,
                    max_steps=MAX_STEPS,
                    action_space=ACTION_SPACE,
                    after_transition=branch_controller.after_transition,
                    retain_steps=False,
                )
                self.branch_writer.flush(sync=True)
                if run.problems:
                    rejected = self.arm == "B2" and all(
                        "b2 candidate rejected:" in str(problem).lower()
                        for problem in run.problems
                    )
                    if rejected:
                        self.ledger.write({
                            "event": "continuation_rejected",
                            "branch_id": branch_id,
                            "reason": [str(x) for x in run.problems],
                            "cpu_seconds": time.process_time() - started,
                        }, sync=True)
                        continue
                    raise StudyFailure(f"{branch_id} failed before native terminal: {run.problems}")
                if not run.terminal:
                    self.ledger.write({
                        "event": "continuation_incomplete",
                        "branch_id": branch_id,
                        "reason": "step_cap_or_nonterminal",
                        "cpu_seconds": time.process_time() - started,
                    }, sync=True)
                    continue
                terminal = terminal_fields(run)
                fitness = terminal_fitness(terminal)
                joint_valid = self.arm != "B2" or (
                    branch_controller.root_changed and branch_controller.future_changed >= 1
                )
                result = {
                    "candidate_index": candidate_index,
                    "parent_candidate_index": parent_index,
                    "parent_discard_reason": parent_discard_reason,
                    "baseline_root_identity": baseline_root_identity,
                    "root_identity": root_identity,
                    "materialized_overrides": branch_controller.materialized_overrides,
                    "terminal": terminal,
                    "fitness": list(fitness),
                    "joint_valid": joint_valid,
                    "root_changed": branch_controller.root_changed,
                    "future_changed_count": branch_controller.future_changed,
                    "map_direction_changed": branch_controller.map_direction_changed,
                    "card_reward_choice_changed": branch_controller.card_reward_choice_changed,
                    "controller_counts": dict(branch_controller.counts),
                    "branch_cpu_seconds": time.process_time() - started,
                }
                results.append(result)
                completed += 1
                self.branch_coverage["completed"] += 1
                self.branch_coverage["joint_valid"] += int(joint_valid)
                self.branch_coverage["map_direction_changed"] += int(branch_controller.map_direction_changed)
                self.branch_coverage["card_reward_choice_changed"] += int(branch_controller.card_reward_choice_changed)
                self.branch_coverage["map_and_card_joint_branch"] += int(
                    joint_valid and branch_controller.map_direction_changed and branch_controller.card_reward_choice_changed
                )
                append_jsonl(call_dir / "branch-results.jsonl", {"branch_id": branch_id, **result}, sync=True)
                self.ledger.write({
                    "event": "continuation_completed",
                    "branch_id": branch_id,
                    "completed_quota": completed,
                    "required_quota": target_completed,
                    "terminal": terminal,
                    "joint_valid": joint_valid,
                    "fitness": list(fitness),
                    "branch_cpu_seconds": result["branch_cpu_seconds"],
                    "cpu_seconds": time.process_time(),
                }, sync=True)
            finally:
                self.branch_writer.flush(sync=True)
                restored = adapter.restore_checkpoint(checkpoint)
                restored_ids = action_identity_dicts_for_actions(adapter.legal_actions(restored))
                if [action_identity(x) for x in restored_ids] != [action_identity(x) for x in root_identities]:
                    raise StudyFailure("root legal actions changed after checkpoint restore")
                if canonical(adapter.checkpoint_fingerprint(snapshot)) != canonical(adapter.checkpoint_fingerprint(restored)):
                    raise StudyFailure("checkpoint restore did not reproduce the root state")
        if completed != target_completed:
            raise StudyFailure(
                f"{self.arm} call {call_index} completed {completed}/{target_completed} "
                f"terminal continuations after {attempts} attempts"
            )
        selectable = [row for row in results if self.arm != "B2" or row["joint_valid"]]
        if not selectable:
            raise StudyFailure(f"B2 call {call_index} has no completed multi-decision trajectory")
        winner = min(
            selectable,
            key=lambda row: (-row["fitness"][0], -row["fitness"][1], row["candidate_index"]),
        )
        self.ledger.write({
            "event": "planning_call_completed",
            "owner_seed": self.owner_seed,
            "arm": self.arm,
            "call_index": call_index,
            "attempts": attempts,
            "completed_continuations": completed,
            "selectable_trajectories": len(selectable),
            "selected_candidate_index": winner["candidate_index"],
            "selected_root_identity": winner["root_identity"],
            "selected_fitness": winner["fitness"],
            "cpu_seconds": time.process_time(),
        }, sync=True)
        summary = {
            "call_index": call_index,
            "attempts": attempts,
            "completed_continuations": completed,
            "selectable_trajectories": len(selectable),
            "selected_candidate_index": winner["candidate_index"],
            "selected_root_identity": winner["root_identity"],
            "selected_fitness": winner["fitness"],
        }
        atomic_json(call_dir / "call-result.json", {
            **summary,
            "arm": self.arm,
            "root_screen": str(context.screen_state),
            "root_legal_actions": [action_identity(x) for x in root_identities],
            "coverage": dict(self.branch_coverage),
            "selected_plan": winner,
        })
        return summary

    def before_decision(self, snapshot: Any, actions: Sequence[Any], context: Any, step_index: int) -> None:
        del step_index
        if self.arm != "B0" or not self.preflight:
            return
        screen = str(context.screen_state).upper()
        if screen not in ("MAP_SCREEN", "REWARDS") or screen in self.witness_checkpoints:
            return
        if len(context.eligible_action_indices) <= 1:
            return
        assert self._active_adapter is not None
        checkpoint = self._active_adapter.capture_checkpoint(snapshot)
        self.witness_checkpoints[screen] = {
            "checkpoint": checkpoint,
            "legal_identities": legal_action_record(actions),
            "eligible_action_indices": [int(x) for x in context.eligible_action_indices],
            "floor": snapshot.raw.get("floor_num"),
        }

    def set_adapter(self, adapter: LightSpeedAdapter) -> None:
        self._active_adapter = adapter

    def after_transition(self, step: ControlledRunStep) -> None:
        row = dict(self.pending)
        row.update({
            "next_screen_state": step.next_screen_state,
            "terminal_after_step": step.terminal_after_step,
            "floor": step.floor,
            "next_floor": step.next_floor,
            "next_player_hp": step.next_player_hp,
            "next_gold": step.next_gold,
            "next_potion_count": step.next_potion_count,
            "next_battle_outcome": step.next_battle_outcome,
        })
        self.actual_writer.write(row)
        for raw in (step.snapshot_raw, step.next_snapshot_raw):
            act = raw.get("act")
            floor = raw.get("floor_num")
            if isinstance(act, (int, float)) and not isinstance(act, bool):
                self.max_act = max(self.max_act, int(act))
            if isinstance(floor, (int, float)) and not isinstance(floor, bool):
                self.max_floor = max(self.max_floor, int(floor))
            room_type = str(raw.get("room_type", "")).upper()
            if room_type:
                self.room_types.add(room_type)
            if "BOSS" in room_type:
                self.boss_reached = True
        self.pending = {}


def run_checkpoint_witnesses(
    adapter: LightSpeedAdapter,
    owner_seed: int,
    witness_checkpoints: Mapping[str, Any],
    attempt_dir: Path,
    branch_writer: JsonlWriter,
    native_identity: Mapping[str, Any],
    config_hash: str,
    replay_count: int = 2,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for screen in ("MAP_SCREEN", "REWARDS"):
        value = witness_checkpoints.get(screen)
        if value is None:
            results[screen] = {"available": False, "reason": "no natural multi-action root observed"}
            continue
        checkpoint = value["checkpoint"]
        policy_seed = derive_seed("issue64-checkpoint-witness-v1", owner_seed, screen)
        root = adapter.restore_checkpoint(checkpoint)
        root_fingerprint = canonical(adapter.checkpoint_fingerprint(root))
        initial_actions = adapter.legal_actions(root)
        initial_identities = action_identity_dicts_for_actions(initial_actions)
        if [action_identity(item) for item in initial_identities] != value["legal_identities"]:
            raise StudyFailure(f"{screen} checkpoint legal actions do not reproduce")
        eligible_indices = [int(index) for index in value["eligible_action_indices"]]
        if len(eligible_indices) < 2 or any(index < 0 or index >= len(initial_actions) for index in eligible_indices):
            raise StudyFailure(f"{screen} checkpoint lacks two eligible native legal actions")
        alternate_branches = []
        for root_action_index in eligible_indices[:2]:
            root = adapter.restore_checkpoint(checkpoint)
            root_actions = adapter.legal_actions(root)
            root_identities = action_identity_dicts_for_actions(root_actions)
            if [action_identity(item) for item in root_identities] != value["legal_identities"]:
                raise StudyFailure(f"{screen} legal actions changed before alternate root action")
            chosen_identity = action_identity(root_identities[root_action_index])
            transition = adapter.step(root_actions[root_action_index])
            restored = adapter.restore_checkpoint(checkpoint)
            restored_identities = action_identity_dicts_for_actions(adapter.legal_actions(restored))
            if [action_identity(item) for item in restored_identities] != value["legal_identities"]:
                raise StudyFailure(f"{screen} legal actions changed after alternate-action restore")
            if canonical(adapter.checkpoint_fingerprint(restored)) != root_fingerprint:
                raise StudyFailure(f"{screen} checkpoint did not restore after alternate legal action")
            alternate_branches.append({
                "root_action_identity": chosen_identity,
                "discarded_next_screen_state": str(transition.snapshot.raw.get("screen_state", "")),
                "discarded_branch_terminal": bool(transition.terminal),
                "restored_root_fingerprint_sha256": hashlib.sha256(root_fingerprint.encode()).hexdigest(),
            })
        replays = []
        for replay_index in range(replay_count):
            controller = BranchController(
                arm="WITNESS", owner_seed=owner_seed, call_index=-1,
                candidate_index=replay_index, policy_seed=policy_seed,
                root_identity=None, plan=None, branch_writer=branch_writer,
                branch_id=f"witness-{owner_seed}-{screen}-{replay_index}",
                expert_only=True,
            )
            started = time.process_time()
            run = execute_controlled_run(
                CheckpointAdapter(adapter, checkpoint), controller,
                seed=policy_seed, max_steps=MAX_STEPS, action_space=ACTION_SPACE,
                after_transition=controller.after_transition, retain_steps=False,
            )
            # Witness digests read the durable JSONL trace by branch id. The
            # engineering writer batches ordinary candidate rows, so force a
            # synced boundary here before reading or accepting replay evidence.
            branch_writer.flush(sync=True)
            if run.problems or not run.terminal:
                raise StudyFailure(
                    f"{screen} deterministic checkpoint replay failed: "
                    f"{run.problems or 'not terminal'}"
                )
            replays.append({
                "trace_digest": controller_trace_digest_path(attempt_dir, controller.branch_id),
                "action_count": controller.counts.total(),
                "endpoint": terminal_fields(run),
                "final_raw_digest": hashlib.sha256(canonical(dict(run.final_raw)).encode()).hexdigest(),
                "cpu_seconds": time.process_time() - started,
            })
        trace_equal = all(
            replay["trace_digest"] == replays[0]["trace_digest"] for replay in replays[1:]
        )
        endpoint_equal = all(
            replay["endpoint"] == replays[0]["endpoint"] for replay in replays[1:]
        )
        final_raw_equal = all(
            replay["final_raw_digest"] == replays[0]["final_raw_digest"] for replay in replays[1:]
        )
        equal = trace_equal and endpoint_equal and final_raw_equal
        results[screen] = {
            "available": True,
            "checkpoint_floor": value.get("floor"),
            "root_legal_actions": value["legal_identities"],
            "alternate_action_restore": alternate_branches,
            "replay_equal": equal,
            "action_trace_equal": trace_equal,
            "terminal_endpoint_equal": endpoint_equal,
            "final_raw_equal": final_raw_equal,
            "replays": replays,
        }
        if not equal:
            atomic_json(attempt_dir / "checkpoint-witnesses-partial.json", {
                "owner_seed": owner_seed,
                "config_sha256": config_hash,
                "native_identity": dict(native_identity),
                "witnesses": results,
                "failed_screen": screen,
            })
            raise StudyFailure(f"{screen} checkpoint replay was not deterministic")
    atomic_json(attempt_dir / "checkpoint-witnesses.json", {
        "owner_seed": owner_seed,
        "config_sha256": config_hash,
        "native_identity": dict(native_identity),
        "witnesses": results,
    })
    return results


def controller_trace_digest_path(attempt_dir: Path, branch_id: str) -> str:
    rows = []
    path = attempt_dir / "branch-decisions.jsonl"
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("branch_id") == branch_id:
                    rows.append({
                        "screen_state": row.get("screen_state"),
                        "chosen_action": row.get("chosen_action"),
                        "legal_actions": row.get("legal_actions"),
                        "controller_role": row.get("controller_role"),
                    })
    return hashlib.sha256(canonical(rows).encode()).hexdigest()


def run_arm(
    owner_seed: int,
    arm: str,
    stage: str,
    owner_dir: Path,
    *,
    preflight: bool,
    config_hash: str,
    native_identity: Mapping[str, Any],
    source_identity: Mapping[str, Any],
    engineering_limits: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    result_path = owner_dir / arm / "arm-result.json"
    if result_path.exists():
        existing = read_receipt(result_path, f"stored {stage} {arm} arm result")
        require_receipt_identity(
            existing,
            {
                "schema_id": "issue65-arm-result-v2",
                "config_sha256": config_hash,
                "source_identity": dict(source_identity),
                "stage": stage,
                "owner_seed": int(owner_seed),
                "arm": arm,
            },
            f"stored {stage} {arm} arm result",
        )
        return existing
    arm_dir = owner_dir / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    attempts = sorted(arm_dir.glob("attempt-*"))
    attempt_dir = arm_dir / f"attempt-{len(attempts) + 1:03d}"
    attempt_dir.mkdir()
    ledger = JsonlWriter(attempt_dir / "ledger.jsonl")
    actual_writer = JsonlWriter(attempt_dir / "actual-decisions.jsonl")
    branch_writer = JsonlWriter(
        attempt_dir / "branch-decisions.jsonl",
        buffered=True,
        batch_lines=ENGINEERING_TRACE_BATCH_LINES,
    )
    witness_checkpoints: dict[str, Any] = {}
    adapter = LightSpeedAdapter(seed=owner_seed, ascension=20, player_class="IRONCLAD")
    controller = StudyController(
        arm=arm, owner_seed=owner_seed, attempt_dir=attempt_dir,
        ledger=ledger, actual_writer=actual_writer, branch_writer=branch_writer,
        preflight=preflight, witness_checkpoints=witness_checkpoints,
        engineering_limits=engineering_limits,
    )
    controller.set_adapter(adapter)
    started_wall = time.monotonic()
    started_cpu = time.process_time()
    ledger.write({
        "event": "arm_started",
        "owner_seed": owner_seed,
        "arm": arm,
        "attempt": attempt_dir.name,
        "native_commit": IDENTITY["native_commit"],
        "native_binary_sha256": IDENTITY["native_binary_sha256"],
        "config_sha256": config_hash,
        "cpu_seconds": started_cpu,
    }, sync=True)
    try:
        run = execute_controlled_run(
            adapter, controller, seed=owner_seed, max_steps=MAX_STEPS,
            action_space=ACTION_SPACE, before_decision=controller.before_decision,
            after_transition=controller.after_transition, retain_steps=False,
        )
        if run.problems:
            event = "EVENT_SCREEN with empty native legal_actions" if "no legal actions before terminal state" in run.problems else "controlled-run problem"
            raise StudyFailure(f"{event}: {run.problems}")
        if not run.terminal:
            raise StudyFailure("controlled run hit its transition cap before native terminal")
        terminal = terminal_fields(run)
        elapsed_cpu = time.process_time() - started_cpu
        result = {
            "schema_id": "issue65-arm-result-v2",
            "config_sha256": config_hash,
            "source_identity": dict(source_identity),
            "stage": stage,
            "owner_seed": owner_seed,
            "arm": arm,
            "terminal": terminal,
            "planning_calls": controller.planning_calls,
            "eligible_actual_decisions": controller.eligible_seen,
            "actual_role_screen_counts": dict(controller.actual_counts),
            "trajectory_coverage": dict(controller.branch_coverage),
            "search_call_summaries": controller.search_summaries,
            "max_act_reached": controller.max_act,
            "max_floor_reached": controller.max_floor,
            "boss_reached": controller.boss_reached,
            "room_types_seen": sorted(controller.room_types),
            "step_count": len(run.public_history),
            "final_raw_digest": hashlib.sha256(canonical(dict(run.final_raw)).encode()).hexdigest(),
            "run_cpu_seconds": elapsed_cpu,
            "run_wall_seconds": time.monotonic() - started_wall,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "native_identity": dict(native_identity),
            "controller_provenance": dict(run.controller_provenance),
            "problems": [],
        }
        ledger.write({
            "event": "arm_completed",
            "owner_seed": owner_seed,
            "arm": arm,
            "terminal": terminal,
            "planning_calls": controller.planning_calls,
            "cpu_seconds": time.process_time(),
        }, sync=True)
        atomic_json(attempt_dir / "arm-result.json", result)
        if arm == "B0" and preflight:
            result["checkpoint_witnesses"] = run_checkpoint_witnesses(
                adapter, owner_seed, witness_checkpoints, attempt_dir,
                branch_writer, native_identity, config_hash,
                replay_count=int((engineering_limits or {}).get("checkpoint_replays", 2)),
            )
        atomic_json(result_path, result)
        return result
    except Exception as exc:
        ledger.write({
            "event": "arm_failed",
            "owner_seed": owner_seed,
            "arm": arm,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(limit=12),
            "cpu_seconds": time.process_time(),
        }, sync=True)
        raise
    finally:
        ledger.close()
        actual_writer.close()
        branch_writer.close()
        adapter.close()


def _run_owner_impl(
    owner_seed: int,
    stage: str,
    output: str,
    config_hash: str,
    native_identity: Mapping[str, Any],
    engineering_limits: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    global STOP_FILE
    output_dir = Path(output)
    STOP_FILE = output_dir / (
        "issue65-budget-stop.flag" if stage == "engineering" else "issue65-preview-budget-stop.flag"
    )
    check_stop()
    owner_dir = output_dir / stage / f"seed-{owner_seed}"
    owner_dir.mkdir(parents=True, exist_ok=True)
    owner_result_path = owner_dir / "owner-result.json"
    source_identity = issue65_source_identity(config_hash, native_identity)
    if owner_result_path.exists():
        existing = read_receipt(owner_result_path, f"stored {stage} owner {owner_seed} result")
        require_receipt_identity(
            existing,
            {
                "schema_id": "issue65-owner-result-v2",
                "config_sha256": config_hash,
                "source_identity": source_identity,
                "stage": stage,
                "owner_seed": int(owner_seed),
            },
            f"stored {stage} owner {owner_seed} result",
        )
        return existing
    owner_attempts = sorted(owner_dir.glob("owner-attempt-*"))
    attempt_dir = owner_dir / f"owner-attempt-{len(owner_attempts) + 1:03d}"
    attempt_dir.mkdir()
    append_jsonl(owner_dir / "owner-ledger.jsonl", {
        "event": "owner_started", "owner_seed": owner_seed, "stage": stage,
        "attempt": attempt_dir.name, "config_sha256": config_hash,
    }, sync=True)
    started = time.process_time()
    arms: dict[str, Any] = {}
    try:
        for arm in CONFIG["arms"]:
            check_stop()
            with runtime_phase(f"arm_execution:{arm}"):
                arms[arm] = run_arm(
                    owner_seed, arm, stage, attempt_dir,
                    preflight=(stage in ("preflight", "engineering")),
                    config_hash=config_hash, native_identity=native_identity,
                    source_identity=source_identity,
                    engineering_limits=engineering_limits,
                )
            append_jsonl(owner_dir / "owner-ledger.jsonl", {
                "event": "arm_result_durable", "owner_seed": owner_seed,
                "arm": arm, "terminal": arms[arm]["terminal"],
            }, sync=True)
        result = {
            "schema_id": "issue65-owner-result-v2",
            "config_sha256": config_hash,
            "source_identity": source_identity,
            "owner_seed": owner_seed,
            "stage": stage,
            "arms": {arm: arms[arm]["terminal"] for arm in CONFIG["arms"]},
            "arm_cpu_seconds": {arm: arms[arm]["run_cpu_seconds"] for arm in CONFIG["arms"]},
            "owner_cpu_seconds": time.process_time() - started,
            "peak_rss_kib": max(arms[arm]["peak_rss_kib"] for arm in CONFIG["arms"]),
            "b2_coverage": {
                key: int(arms["B2"].get("trajectory_coverage", {}).get(key, 0))
                for key in (
                    "completed", "joint_valid", "map_direction_changed",
                    "card_reward_choice_changed", "map_and_card_joint_branch",
                )
            },
            "checkpoint_witnesses": arms["B0"].get("checkpoint_witnesses", {}),
        }
        atomic_json(owner_result_path, result)
        append_jsonl(owner_dir / "owner-ledger.jsonl", {
            "event": "owner_completed", "owner_seed": owner_seed,
            "owner_cpu_seconds": result["owner_cpu_seconds"],
            "peak_rss_kib": result["peak_rss_kib"],
        }, sync=True)
        return result
    except Exception as exc:
        append_jsonl(owner_dir / "owner-ledger.jsonl", {
            "event": "owner_failed", "owner_seed": owner_seed, "stage": stage,
            "error_type": type(exc).__name__, "error": str(exc),
        }, sync=True)
        raise


def ensure_runtime() -> Any:
    """Install the study-local optimizer on the exact runner's imported path."""
    global _RUNTIME_EXECUTION, _RUNTIME_MODULE
    if _RUNTIME_EXECUTION is not None:
        return _RUNTIME_EXECUTION
    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    _RUNTIME_MODULE = importlib.import_module("public_history_execution")
    controlled = importlib.import_module("sts_combat_rl.sim.controlled_run")
    public_context = importlib.import_module("sts_combat_rl.sim.public_run_context")
    _RUNTIME_EXECUTION = _RUNTIME_MODULE.install(
        optimized=True,
        controlled_run_module=controlled,
        public_context_module=public_context,
        adapter_class=LightSpeedAdapter,
        expert_driver_class=ExpertNonCombatDriver,
        runner_module=sys.modules[__name__],
    )
    # Function imports above are aliases; update the namespace used by this
    # module explicitly so spawn/forkserver workers execute the wrapped path.
    globals()["execute_controlled_run"] = controlled.execute_controlled_run
    globals()["action_identity_dicts_for_actions"] = controlled.action_identity_dicts_for_actions
    return _RUNTIME_EXECUTION


def run_owner(
    owner_seed: int,
    stage: str,
    output: str,
    config_hash: str,
    native_identity: Mapping[str, Any],
    engineering_limits: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    global _LAST_STOP_POLL_CPU
    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] | None = None
    _LAST_STOP_POLL_CPU = None
    try:
        runtime = ensure_runtime()
        with runtime.scope(
            metrics_path=output_dir / "issue65-runtime-metrics.jsonl",
            labels={"stage": stage, "owner_seed": int(owner_seed)},
            telemetry_enabled=True,
        ):
            with runtime_phase("owner_total"):
                result = _run_owner_impl(
                    owner_seed, stage, output, config_hash, native_identity, engineering_limits
                )
    finally:
        receipt = (
            write_worker_cpu_receipt(output_dir, stage, owner_seed)
            if engineering_limits is not None or stage == "engineering"
            else {}
        )
    if result is None:
        raise StudyFailure("owner returned without a result")
    result.update(receipt)
    return result


def memory_available_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 2 * 1024 * 1024 * 1024


def proc_cpu_details(pid: int) -> tuple[int | None, int, str | None, int | None]:
    try:
        row = Path(f"/proc/{pid}/stat").read_text()
        rest = row[row.rfind(")") + 2 :].split()
        return int(rest[1]), int(rest[11]) + int(rest[12]), rest[0], int(rest[19])
    except (OSError, ValueError, IndexError):
        return None, 0, None, None


def write_worker_cpu_receipt(output_dir: Path, stage: str, owner_seed: int) -> dict[str, Any]:
    """Persist a worker's process CPU receipt even when owner execution raises."""
    _ppid, _ticks, _state, start_ticks = proc_cpu_details(os.getpid())
    receipt = {
        "stage": stage,
        "owner_seed": int(owner_seed),
        "worker_pid": os.getpid(),
        "worker_start_ticks": start_ticks,
        # Linux process_time is the process lifetime CPU clock, including imports
        # and prior tasks when ProcessPoolExecutor reuses a worker.
        "worker_process_cpu_seconds": time.process_time(),
    }
    receipt_dir = output_dir / "issue65-worker-cpu-receipts"
    receipt_path = receipt_dir / f"{stage}-seed-{int(owner_seed)}.json"
    atomic_json(receipt_path, receipt)
    return receipt


class ProcessTreeCPUSampler:
    """Accumulate process-tree CPU without double-counting the root PID.

    PID/start-time keys and maximum observed ticks keep completed workers in the
    total even when a short task exits between parent polls. Worker receipts
    fill in such unseen completions; max() avoids double-counting sampled ones.
    """

    def __init__(self, root_pid: int):
        self.root_pid = root_pid
        self.hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        self.own_start = time.process_time()
        self.initial_ticks: dict[tuple[int, int], int] = {}
        self.max_child_cpu: dict[tuple[int, int], float] = {}
        for pid, (ppid, ticks, _state, start_ticks) in self._descendants().items():
            if start_ticks is not None:
                self.initial_ticks[(pid, start_ticks)] = ticks

    def _descendants(self) -> dict[int, tuple[int | None, int, str | None, int | None]]:
        rows: dict[int, tuple[int | None, int, str | None, int | None]] = {}
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                pid = int(entry.name)
                ppid, ticks, state, start_ticks = proc_cpu_details(pid)
                if state is not None:
                    rows[pid] = (ppid, ticks, state, start_ticks)
        descendants = {self.root_pid}
        changed = True
        while changed:
            changed = False
            for pid, (ppid, _ticks, _state, _start_ticks) in rows.items():
                if ppid in descendants and pid not in descendants:
                    descendants.add(pid)
                    changed = True
        return {pid: rows[pid] for pid in descendants if pid != self.root_pid and pid in rows}

    def sample(self) -> float:
        for pid, (_ppid, ticks, state, start_ticks) in self._descendants().items():
            if state == "Z" or start_ticks is None:
                continue
            key = (pid, start_ticks)
            baseline = self.initial_ticks.get(key, 0)
            observed = max(0.0, (ticks - baseline) / self.hz)
            self.max_child_cpu[key] = max(self.max_child_cpu.get(key, 0.0), observed)
        return max(0.0, time.process_time() - self.own_start) + sum(self.max_child_cpu.values())

    def record_worker_result(self, result: Mapping[str, Any]) -> None:
        try:
            key = (int(result["worker_pid"]), int(result["worker_start_ticks"]))
            worker_cpu = max(0.0, float(result["worker_process_cpu_seconds"]))
        except (KeyError, TypeError, ValueError):
            return
        baseline_ticks = self.initial_ticks.get(key, 0)
        self.max_child_cpu[key] = max(
            self.max_child_cpu.get(key, 0.0),
            worker_cpu - baseline_ticks / self.hz,
        )


def read_budget_state(
    path: Path,
    *,
    config_hash: str,
    source_identity: Mapping[str, Any],
) -> dict[str, Any]:
    if path.exists():
        data = read_receipt(path, "Issue 65 preview budget")
        require_receipt_identity(
            data,
            {
                "schema_id": "issue65-preview-budget-v1",
                "study_id": CONFIG["study_id"],
                "config_sha256": config_hash,
                "source_identity": dict(source_identity),
            },
            "Issue 65 preview budget",
        )
        return data
    return {
        "schema_id": "issue65-preview-budget-v1",
        "study_id": CONFIG["study_id"],
        "config_sha256": config_hash,
        "source_identity": dict(source_identity),
        "builder_cpu_seconds": float(SETUP_ALLOWANCE),
        "setup_allowance_cpu_seconds": float(SETUP_ALLOWANCE),
        "last_process_cpu_checkpoint": 0.0,
        "stop_requested": False,
    }


def write_budget_state(path: Path, state: Mapping[str, Any]) -> None:
    atomic_json(path, state)


def read_engineering_budget(path: Path, runner_sha256: str) -> dict[str, Any]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("schema_id") != "issue65-engineering-budget-v1":
            raise StudyFailure("engineering budget file has an unknown schema")
        if state.get("runner_sha256") != runner_sha256:
            raise StudyFailure("engineering receipts belong to a different runner source hash")
        if (
            float(state.get("aggregate_unique_process_cpu_hard_max", -1)) != ENGINEERING_TOTAL_CPU
            or float(state.get("independent_review_reserve_cpu_seconds", -1)) != ENGINEERING_REVIEW_RESERVE_CPU
            or float(state.get("builder_cpu_limit_cpu_seconds", -1)) != ENGINEERING_BUILDER_HARD_CPU
        ):
            raise StudyFailure("engineering budget constants differ from the original Issue 65 receipt")
        return state
    return {
        "schema_id": "issue65-engineering-budget-v1",
        "issue": 65,
        "runner_sha256": runner_sha256,
        "builder_cpu_seconds": 0.0,
        "builder_soft_stop_cpu_seconds": ENGINEERING_BUILDER_SOFT_STOP_CPU,
        "builder_hard_stop_cpu_seconds": ENGINEERING_BUILDER_HARD_CPU,
        "aggregate_unique_process_cpu_hard_max": ENGINEERING_TOTAL_CPU,
        "independent_review_reserve_cpu_seconds": ENGINEERING_REVIEW_RESERVE_CPU,
        "builder_cpu_limit_cpu_seconds": ENGINEERING_BUILDER_HARD_CPU,
        "stop_requested": False,
        "hard_stop_requested": False,
        "last_process_cpu_checkpoint": 0.0,
    }


def optimized_source_identity() -> dict[str, str]:
    runtime_path = ROOT / "public_history_execution.py"
    if not runtime_path.is_file():
        raise StudyFailure("installed Issue 65 runtime module is missing")
    return {
        "runner_sha256": sha256_file(Path(__file__).resolve()),
        "optimizer_module_sha256": sha256_file(runtime_path),
    }


def issue65_source_identity(
    config_hash: str, native_identity: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        **optimized_source_identity(),
        "config_sha256": config_hash,
        "native_identity": dict(native_identity),
    }


def require_receipt_identity(
    receipt: Mapping[str, Any], expected: Mapping[str, Any], description: str
) -> None:
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise StudyFailure(
                f"{description} is legacy or belongs to a different execution identity ({key})"
            )


def read_receipt(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StudyFailure(f"{description} is unreadable; refusing to resume") from exc
    if not isinstance(value, dict):
        raise StudyFailure(f"{description} has an invalid receipt shape; refusing to resume")
    return value


def validate_cached_stage_receipts(
    output: Path,
    stage: str,
    config_hash: str,
    native_identity: Mapping[str, Any],
) -> None:
    stage_dir = output / stage
    if not stage_dir.exists():
        return
    source_identity = issue65_source_identity(config_hash, native_identity)
    for owner_path in stage_dir.glob("seed-*/owner-result.json"):
        try:
            owner_seed = int(owner_path.parent.name.removeprefix("seed-"))
        except ValueError as exc:
            raise StudyFailure(f"unexpected owner receipt path: {owner_path}") from exc
        receipt = read_receipt(owner_path, f"{stage} owner receipt {owner_seed}")
        require_receipt_identity(
            receipt,
            {
                "schema_id": "issue65-owner-result-v2",
                "config_sha256": config_hash,
                "source_identity": source_identity,
                "stage": stage,
                "owner_seed": owner_seed,
            },
            f"{stage} owner receipt {owner_seed}",
        )
    for arm_path in stage_dir.rglob("arm-result.json"):
        seed_dir = next((parent for parent in arm_path.parents if parent.parent == stage_dir), None)
        if seed_dir is None or not seed_dir.name.startswith("seed-"):
            raise StudyFailure(f"unexpected arm receipt path: {arm_path}")
        try:
            owner_seed = int(seed_dir.name.removeprefix("seed-"))
        except ValueError as exc:
            raise StudyFailure(f"unexpected arm receipt path: {arm_path}") from exc
        receipt = read_receipt(arm_path, f"{stage} arm receipt {owner_seed}/{arm_path.parent.name}")
        require_receipt_identity(
            receipt,
            {
                "schema_id": "issue65-arm-result-v2",
                "config_sha256": config_hash,
                "source_identity": source_identity,
                "stage": stage,
                "owner_seed": owner_seed,
                "arm": arm_path.parent.name,
            },
            f"{stage} arm receipt {owner_seed}/{arm_path.parent.name}",
        )


def validate_issue65_resume_state(
    output: Path,
    *,
    mode: str,
    config_hash: str,
    native_identity: Mapping[str, Any],
) -> dict[str, Any] | None:
    source_identity = issue65_source_identity(config_hash, native_identity)
    legacy_budget_path = output / LEGACY_ISSUE64_BUDGET_FILE
    if legacy_budget_path.exists():
        raise StudyFailure(
            "refusing legacy Issue 64 budget-state.json; Issue 65 requires a fresh budget namespace"
        )
    manifest_path = output / "study-manifest.json"
    if manifest_path.exists():
        manifest = read_receipt(manifest_path, "study manifest")
        require_receipt_identity(
            manifest,
            {
                "schema_id": "issue65-study-manifest-v2",
                "config_sha256": config_hash,
                "source_identity": source_identity,
            },
            "study manifest",
        )
    decision_path = output / "preflight-decision.json"
    decision: dict[str, Any] | None = None
    if decision_path.exists():
        decision = read_receipt(decision_path, "preflight decision")
        require_receipt_identity(
            decision,
            {
                "schema_id": "issue65-preflight-decision-v2",
                "config_sha256": config_hash,
                "source_identity": source_identity,
            },
            "preflight decision",
        )
    if mode == "production":
        if decision is None:
            raise StudyFailure("production requires a completed preflight-decision.json")
        if not decision.get("formal_production_authorized_by_preflight"):
            raise StudyFailure("preflight did not authorize the frozen 64-owner production")
    stages = ("preflight", "production") if mode == "production" else ("preflight",)
    for stage in stages:
        validate_cached_stage_receipts(output, stage, config_hash, native_identity)
    complete_path = output / "production-complete.json"
    if complete_path.exists():
        complete = read_receipt(complete_path, "production completion receipt")
        require_receipt_identity(
            complete,
            {
                "schema_id": "issue65-production-complete-v2",
                "config_sha256": config_hash,
                "source_identity": source_identity,
            },
            "production completion receipt",
        )
    return decision


def select_workers(target: int, estimated_worker_rss_mb: int = 1024) -> int:
    memory_budget = int(memory_available_bytes() * 0.7)
    max_by_memory = max(1, memory_budget // (estimated_worker_rss_mb * 1024 * 1024))
    return max(1, min(int(target), int(CONFIG["resources"]["max_workers"]), max_by_memory))


def execute_stage(
    *,
    stage: str,
    seeds: Sequence[int],
    output: Path,
    native_identity: Mapping[str, Any],
    config_hash: str,
    target_workers: int,
    budget_state: dict[str, Any],
    budget_path: Path,
    engineering_limits: Mapping[str, int] | None = None,
    soft_stop_cpu_seconds: float | None = None,
    hard_stop_cpu_seconds: float | None = None,
    event_log_name: str = "issue65-preview-events.jsonl",
    stop_file_name: str = "issue65-preview-budget-stop.flag",
) -> list[dict[str, Any]]:
    global STOP_FILE
    STOP_FILE = output / stop_file_name
    if bool(budget_state.get("stop_requested")):
        raise StudyFailure("a previous budget stop is recorded; resume is refused")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    workers = select_workers(target_workers)
    base_consumed = float(budget_state["builder_cpu_seconds"])
    cpu_sampler = ProcessTreeCPUSampler(os.getpid())
    parent_start_cpu = cpu_sampler.sample()
    started_wall = time.monotonic()
    results: list[dict[str, Any]] = []
    append_jsonl(output / event_log_name, {
        "event": "stage_started", "stage": stage, "seeds": list(seeds),
        "workers": workers, "mem_available_bytes": memory_available_bytes(),
        "estimated_worker_rss_mb": 1024, "builder_cpu_seconds_before": base_consumed,
    }, sync=True)
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                run_owner, int(seed), stage, str(output), config_hash, dict(native_identity),
                dict(engineering_limits) if engineering_limits is not None else None,
            ): int(seed)
            for seed in seeds
        }
        pending = set(futures)
        while pending:
            current = cpu_sampler.sample()
            delta = max(0.0, current - parent_start_cpu)
            consumed = base_consumed + delta
            budget_state = dict(budget_state)
            budget_state.update({
                "builder_cpu_seconds": consumed,
                "last_process_cpu_checkpoint": delta,
                "stop_requested": consumed >= (
                    soft_stop_cpu_seconds if soft_stop_cpu_seconds is not None else MAX_CPU
                ),
                "hard_stop_requested": (
                    hard_stop_cpu_seconds is not None and consumed >= hard_stop_cpu_seconds
                ),
                "last_updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            write_budget_state(budget_path, budget_state)
            if budget_state["stop_requested"]:
                STOP_FILE.touch()
            if budget_state.get("hard_stop_requested"):
                append_jsonl(output / event_log_name, {
                    "event": "aggregate_cpu_hard_stop",
                    "stage": stage,
                    "builder_cpu_seconds": consumed,
                    "hard_limit_cpu_seconds": hard_stop_cpu_seconds,
                    "workers": workers,
                }, sync=True)
                if hasattr(pool, "terminate_workers"):
                    pool.terminate_workers()
                else:
                    for process in getattr(pool, "_processes", {}).values():
                        process.terminate()
                raise StudyFailure("Issue 65 aggregate Builder CPU hard stop; worker pool terminated")
            done, pending = concurrent.futures.wait(
                pending, timeout=0.5, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                seed = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                    cpu_sampler.record_worker_result(result)
                    append_jsonl(output / event_log_name, {
                        "event": "owner_future_completed", "stage": stage,
                        "owner_seed": seed, "owner_cpu_seconds": result["owner_cpu_seconds"],
                        "peak_rss_kib": result["peak_rss_kib"],
                        "builder_cpu_seconds": budget_state["builder_cpu_seconds"],
                    }, sync=True)
                except Exception as exc:
                    STOP_FILE.touch()
                    for receipt_seed in (seed, *[futures[item] for item in pending]):
                        receipt_path = output / "issue65-worker-cpu-receipts" / f"{stage}-seed-{receipt_seed}.json"
                        if receipt_path.exists():
                            cpu_sampler.record_worker_result(json.loads(receipt_path.read_text(encoding="utf-8")))
                    cpu_sampler.sample()
                    for remaining in pending:
                        remaining.cancel()
                    if hasattr(pool, "terminate_workers"):
                        pool.terminate_workers()
                    else:
                        for process in getattr(pool, "_processes", {}).values():
                            process.terminate()
                    delta = max(0.0, cpu_sampler.sample() - parent_start_cpu)
                    consumed = base_consumed + delta
                    budget_state = dict(budget_state)
                    budget_state.update({
                        "builder_cpu_seconds": consumed,
                        "last_process_cpu_checkpoint": delta,
                        "stop_requested": True,
                        "hard_stop_requested": (
                            hard_stop_cpu_seconds is not None and consumed >= hard_stop_cpu_seconds
                        ),
                        "last_updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    })
                    write_budget_state(budget_path, budget_state)
                    append_jsonl(output / event_log_name, {
                        "event": "owner_future_failed", "stage": stage, "owner_seed": seed,
                        "error_type": type(exc).__name__, "error": str(exc),
                        "builder_cpu_seconds": consumed,
                    }, sync=True)
                    raise StudyFailure(f"{stage} owner {seed} failed: {exc}") from exc
    # Sample after executor shutdown so completed worker CPU is in RUSAGE_CHILDREN.
    final_cpu = cpu_sampler.sample()
    delta = max(0.0, final_cpu - parent_start_cpu)
    budget_state = dict(budget_state)
    effective_soft_stop = soft_stop_cpu_seconds if soft_stop_cpu_seconds is not None else MAX_CPU
    budget_state.update({
        "builder_cpu_seconds": base_consumed + delta,
        "last_process_cpu_checkpoint": delta,
        "stop_requested": base_consumed + delta >= effective_soft_stop,
        "hard_stop_requested": (
            hard_stop_cpu_seconds is not None and base_consumed + delta >= hard_stop_cpu_seconds
        ),
        "last_updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    write_budget_state(budget_path, budget_state)
    results.sort(key=lambda row: row["owner_seed"])
    append_jsonl(output / event_log_name, {
        "event": "stage_completed", "stage": stage, "owner_count": len(results),
        "builder_cpu_seconds": budget_state["builder_cpu_seconds"],
        "wall_seconds": time.monotonic() - started_wall,
    }, sync=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue 64 full-terminal non-combat oracle study")
    parser.add_argument("mode", choices=("audit", "preflight", "production", "selfcheck", "engineering"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--native-source", default="/home/lsmft/stsrl-spikes/sts_lightspeed-issue59")
    parser.add_argument("--stsrl-source", default="/home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--seeds", type=int, nargs="+")
    args = parser.parse_args()
    config_hash = hashlib.sha256(canonical(CONFIG).encode()).hexdigest()

    if args.mode == "selfcheck":
        if terminal_fitness({"heart_victory": False, "terminal_floor": 10}) != (0, 10):
            raise StudyFailure("terminal utility self-check failed")
        if terminal_fitness({"heart_victory": True, "terminal_floor": 3}) != (1, 3):
            raise StudyFailure("Heart-first terminal utility self-check failed")
        selfcheck_b2_root_selection()
        print("selfcheck: terminal utility ordering and B2 root/suffix invariants passed")
        return 0

    native_identity = validate_environment(args.native_source, args.stsrl_source)
    if args.mode == "audit":
        print(canonical({
            "environment": native_identity, "config_sha256": config_hash,
            **optimized_source_identity(),
            "reported_stsrl_native_manifest": "recorded separately; runner uses the pinned native source and binary identity",
        }))
        return 0

    if args.mode == "engineering":
        if args.output is None:
            parser.error("--output is required for engineering")
        output = args.output.expanduser().resolve()
        ensure_output_is_external(output)
        output.mkdir(parents=True, exist_ok=True)
        if (output / "budget-state.json").exists():
            raise StudyFailure("refusing legacy Issue 64 budget-state.json; Issue 65 owns a separate budget")
        if (output / "study-manifest.json").exists():
            raise StudyFailure("refusing an Issue 64 study output directory")
        if (output / "preflight-decision.json").exists():
            raise StudyFailure("refusing an Issue 64 preflight output directory")
        seeds = tuple(args.seeds or ENGINEERING_SEEDS[:1])
        if not seeds or len(seeds) > len(ENGINEERING_SEEDS) or any(seed not in ENGINEERING_SEEDS for seed in seeds):
            raise StudyFailure(f"engineering seeds must be selected from {ENGINEERING_SEEDS}")
        if len(set(seeds)) != len(seeds):
            raise StudyFailure("engineering seeds must be unique")
        worker_count = args.workers if args.workers is not None else min(2, len(seeds))
        if worker_count < 1 or worker_count > 2 or worker_count > len(seeds):
            raise StudyFailure("engineering mode permits one or two workers, no more than selected seeds")
        source_identity = optimized_source_identity()
        limits = {
            "planning_calls_max": 1,
            "complete_terminal_continuations_per_call_max": 1,
            "retry_attempts_per_call_max": 8,
            "checkpoint_replays": 2,
        }
        engineering_hash = hashlib.sha256(canonical({
            "config": CONFIG,
            "limits": limits,
            "seeds": list(seeds),
            "source_identity": source_identity,
            "optimizer_id": "issue65-demand-driven-public-history-v1",
        }).encode()).hexdigest()
        budget_path = output / "engineering-budget-state.json"
        state = read_engineering_budget(budget_path, source_identity["runner_sha256"])
        if state.get("engineering_config_sha256") not in (None, engineering_hash):
            raise StudyFailure("engineering receipts were created with different seeds or limits")
        consumed = float(state["builder_cpu_seconds"])
        if state.get("stop_requested") or consumed >= ENGINEERING_BUILDER_SOFT_STOP_CPU:
            raise StudyFailure("Issue 65 engineering budget is stopped or already at its soft limit")
        state.update({
            "engineering_config_sha256": engineering_hash,
            "native_identity": native_identity,
            "optimizer_module_sha256": source_identity["optimizer_module_sha256"],
            "selected_fixed_seeds": list(seeds),
            "engineering_limits": limits,
            "workers": worker_count,
        })
        write_budget_state(budget_path, state)
        atomic_json(output / "issue65-engineering-manifest.json", {
            "schema_id": "issue65-engineering-manifest-v1",
            "issue_url": "https://github.com/lsmfttb/spire-research/issues/65",
            "study_only": True,
            "source_base_commit": "e13f97402a0716c4e2618a6d4800d58602a3058d",
            "source_identity": source_identity,
            "engineering_config_sha256": engineering_hash,
            "native_identity": native_identity,
            "selected_fixed_seeds": list(seeds),
            "limits": limits,
            "budget": {
                "aggregate_unique_process_cpu_hard_max": ENGINEERING_TOTAL_CPU,
                "independent_review_reserve_cpu_seconds": ENGINEERING_REVIEW_RESERVE_CPU,
                "builder_cpu_hard_limit": ENGINEERING_BUILDER_HARD_CPU,
                "builder_cpu_soft_stop": ENGINEERING_BUILDER_SOFT_STOP_CPU,
            },
            "disposition": "EFFICIENCY_NOT_ESTABLISHED until paired baseline, parity, cost projection, and review evidence are complete",
        })
        ensure_runtime()
        results = execute_stage(
            stage="engineering",
            seeds=seeds,
            output=output,
            native_identity=native_identity,
            config_hash=engineering_hash,
            target_workers=worker_count,
            budget_state=state,
            budget_path=budget_path,
            engineering_limits=limits,
            soft_stop_cpu_seconds=ENGINEERING_BUILDER_SOFT_STOP_CPU,
            hard_stop_cpu_seconds=ENGINEERING_BUILDER_HARD_CPU,
            event_log_name="issue65-engineering-events.jsonl",
            stop_file_name="issue65-budget-stop.flag",
        )
        final_state = json.loads(budget_path.read_text(encoding="utf-8"))
        if final_state.get("stop_requested") or final_state.get("hard_stop_requested"):
            raise StudyFailure("bounded Issue 65 engineering run reached its fail-closed stop boundary")
        atomic_json(output / "issue65-engineering-results.json", {
            "schema_id": "issue65-engineering-results-v1",
            "engineering_config_sha256": engineering_hash,
            "source_identity": source_identity,
            "owner_results": results,
            "builder_cpu_seconds": final_state["builder_cpu_seconds"],
            "terminal": "engineering_validation_only",
            "disposition": "EFFICIENCY_NOT_ESTABLISHED pending baseline comparison, coverage, static audit and Reviewer audit",
        })
        print(canonical({
            "stage": "engineering",
            "selected_seeds": list(seeds),
            "owner_count": len(results),
            "builder_cpu_seconds": final_state["builder_cpu_seconds"],
            "source_identity": source_identity,
        }))
        return 0

    if args.output is None:
        parser.error("--output is required for preflight and production")
    output = args.output.expanduser().resolve()
    ensure_output_is_external(output)
    output.mkdir(parents=True, exist_ok=True)
    source_identity = issue65_source_identity(config_hash, native_identity)
    decision = validate_issue65_resume_state(
        output,
        mode=args.mode,
        config_hash=config_hash,
        native_identity=native_identity,
    )
    budget_path = output / ISSUE65_PREVIEW_BUDGET_FILE
    state = read_budget_state(
        budget_path,
        config_hash=config_hash,
        source_identity=source_identity,
    )
    if state.get("stop_requested") or float(state["builder_cpu_seconds"]) >= MAX_CPU:
        raise StudyFailure("Builder CPU ceiling is already exhausted")
    state.update({
        "source_identity": source_identity,
        "native_identity": native_identity, "builder_cpu_ceiling_seconds": MAX_CPU,
        "review_reserve_seconds": REVIEW_RESERVE,
    })
    write_budget_state(budget_path, state)
    atomic_json(output / "study-manifest.json", {
        "schema_id": "issue65-study-manifest-v2",
        "study_id": CONFIG["study_id"], "config": CONFIG,
        "config_sha256": config_hash,
        "source_identity": source_identity,
        "native_identity": native_identity,
        "issue_url": "https://github.com/lsmfttb/spire-research/issues/64",
        "artifact_consumer": "Issue 64 independent Reviewer and Planner",
        "delete_after": "2027-01-31",
    })

    if args.mode == "preflight":
        seeds = CONFIG["owners"]["preflight"]
        target = args.workers or CONFIG["resources"]["preflight_workers_default"]
        results = execute_stage(
            stage="preflight", seeds=seeds, output=output,
            native_identity=native_identity, config_hash=config_hash,
            target_workers=target, budget_state=state, budget_path=budget_path,
        )
        if len(results) != len(seeds):
            raise StudyFailure("preflight did not complete all eight disjoint owners")
        max_owner_cpu = max(float(row["owner_cpu_seconds"]) for row in results)
        current_cpu = float(read_budget_state(
            budget_path, config_hash=config_hash, source_identity=source_identity
        )["builder_cpu_seconds"])
        projected = current_cpu + max_owner_cpu * len(CONFIG["owners"]["production"]) + REVIEW_RESERVE
        checkpoint_ok = all(
            row.get("checkpoint_witnesses", {}).get(screen, {}).get("replay_equal") is True
            for row in results for screen in ("MAP_SCREEN", "REWARDS")
        )
        coverage = {
            key: sum(int(row["b2_coverage"].get(key, 0)) for row in results)
            for key in (
                "completed", "joint_valid", "map_direction_changed",
                "card_reward_choice_changed", "map_and_card_joint_branch",
            )
        }
        exposure_ok = coverage["map_and_card_joint_branch"] > 0
        rss_peak = max(int(row["peak_rss_kib"]) for row in results)
        workers16 = select_workers(16, max(1, math.ceil(rss_peak / 1024)))
        worker_fit = rss_peak * workers16 <= int(memory_available_bytes() * 0.8 / 1024)
        fit = checkpoint_ok and exposure_ok and worker_fit and projected <= 180000
        decision = {
            "schema_id": "issue65-preflight-decision-v2",
            "config_sha256": config_hash,
            "source_identity": source_identity,
            "runner_sha256": source_identity["runner_sha256"],
            "optimizer_module_sha256": source_identity["optimizer_module_sha256"],
            "preflight_owner_count": len(results),
            "max_preflight_owner_cpu_seconds": max_owner_cpu,
            "builder_cpu_seconds_after_preflight": current_cpu,
            "projected_cpu_seconds_including_64_owners_and_review_reserve": projected,
            "projected_fit": projected <= 180000,
            "checkpoint_replay_ok": checkpoint_ok,
            "trajectory_exposure": coverage,
            "required_map_and_card_joint_exposure_ok": exposure_ok,
            "peak_worker_rss_kib": rss_peak,
            "available_memory_bytes_after_preflight": memory_available_bytes(),
            "production_workers_supported_by_memory": workers16,
            "worker_memory_fit": worker_fit,
            "formal_production_authorized_by_preflight": fit,
            "limitations": [
                "preflight owners are excluded from the 64-owner production endpoint",
                "a missing MAP or REWARDS witness is a failed feasibility check",
                "resource projection uses maximum observed preflight owner cost times 64 production owners",
            ],
        }
        atomic_json(output / "preflight-decision.json", decision)
        print(canonical(decision))
        return 0 if fit else 2

    if decision is None:
        raise StudyFailure("production requires a completed preflight-decision.json")
    seeds = CONFIG["owners"]["production"]
    target = args.workers or int(decision["production_workers_supported_by_memory"])
    results = execute_stage(
        stage="production", seeds=seeds, output=output,
        native_identity=native_identity, config_hash=config_hash,
        target_workers=target, budget_state=state, budget_path=budget_path,
    )
    if len(results) != len(seeds):
        raise StudyFailure("production stopped before all 64 independent owners completed")
    atomic_json(output / "production-complete.json", {
        "schema_id": "issue65-production-complete-v2",
        "config_sha256": config_hash,
        "source_identity": source_identity,
        "owner_count": len(results),
        "owners": results,
        "builder_cpu_seconds": read_budget_state(
            budget_path, config_hash=config_hash, source_identity=source_identity
        )["builder_cpu_seconds"],
    })
    print("production: all 64 paired owner triplets reached native terminal")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StudyFailure as exc:
        print(f"study failure: {exc}", file=sys.stderr)
        raise SystemExit(2)
