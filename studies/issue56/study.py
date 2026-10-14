from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import random
import shutil
import sys
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_STSRL = "/mnt/d/DeadlycatCoding/issue-56-STSRL" if os.name == "posix" else r"D:\DeadlycatCoding\issue-56-STSRL"
STSRL = Path(os.environ.get("STSRL_ROOT", DEFAULT_STSRL))
NATIVE_MODULE_DIR = Path(os.environ.get("NATIVE_MODULE_DIR", "/tmp/issue56-native-build"))
for item in (STSRL / "src", NATIVE_MODULE_DIR):
    sys.path.insert(0, str(item))

import slaythespire  # type: ignore[import-not-found]
from sts_combat_rl.sim.action_space import ActionSpaceConfig
from sts_combat_rl.sim.controlled_run import ControlledRunStep, execute_controlled_run
from sts_combat_rl.sim.controller_contract import ControllerDecision, ControllerProvenance
from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions
from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
from sts_combat_rl.sim.non_combat_policy import ExpertNonCombatDriver
from sts_combat_rl.sim.online_controller import PolicyController, RoutedRunController, is_battle_state
from sts_combat_rl.sim.oracle_search import build_oracle_search_report, select_oracle_root_action
from sts_combat_rl.sim.policy_contract import DecisionContext

NATIVE_COMMIT = "9e6f2acc50188a05ea5cc10b760e6f96c6856c8e"
NATIVE_MODULE_SHA256 = "b0b21b7d145fecf4bd077fb9a351e094d5bfd76e17004bfeb64c1b7a825aef7c"
SEARCH_API = "StepSimulator.battle_search_v2.v1"
SEARCH_PATCH = "sts_lightspeed_battle_search_v2_tree_internal_v1"
SIMULATIONS = 400
DRIVER_SEED = 558099
BOOTSTRAP_SEED = 558099
SEEDS = tuple(range(558001, 558065))
MAX_STEPS = 20000
CPU_CEILING_SECONDS = 5900.0
POTION_KINDS = frozenset({"potion", "potion_discard"})
PREFLIGHT_PATH = Path(os.environ.get("ISSUE56_PREFLIGHT_PATH", "studies/issue56/preflight"))


def canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(x) for x in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def raw_state_fingerprint(raw: Mapping[str, Any], transition_only_keys: Sequence[str] = ()) -> str:
    ignored = set(transition_only_keys)
    stateful = {key: value for key, value in raw.items() if key not in ignored}
    payload = json.dumps(canonical(stateful), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def b2_resolved(branch: Mapping[str, Any]) -> bool:
    return (branch.get("endpoint", {}).get("status") == "b2_resolved"
        and len(branch.get("trace", {}).get("battle_exits", [])) >= 2)


def preflight_qualification(owner: Mapping[str, Any]) -> dict[str, Any]:
    consume = owner.get("consume", {})
    reserve = owner.get("reserve", {})
    a_states = consume.get("trace", {}).get("b1_raw_state_fingerprint_sha256", [])
    b_states = reserve.get("trace", {}).get("b1_raw_state_fingerprint_sha256", [])
    # The shared root is the first fingerprint on both arms. Assess collisions
    # after it, across every recorded B1 decision and transition state.
    a_after_root, b_after_root = set(a_states[1:]), set(b_states[1:])
    nonreconverging = bool(a_after_root and b_after_root and not (a_after_root & b_after_root))
    b1_long = reserve.get("trace", {}).get("b1_masked_decisions", 0) >= 2
    interbattle_noncombat = (consume.get("trace", {}).get("between_b1_b2_noncombat_decisions", 0) >= 1
        and reserve.get("trace", {}).get("between_b1_b2_noncombat_decisions", 0) >= 1)
    pair_b2 = b2_resolved(consume) and b2_resolved(reserve)
    return {
        "b1_long": b1_long,
        "no_post_root_raw_state_fingerprint_collision": nonreconverging,
        "interbattle_noncombat_observed_both_arms": interbattle_noncombat,
        "both_arms_resolved_b2": pair_b2,
        "qualifies": bool(owner.get("preflight_integrity_pass") and b1_long and nonreconverging
            and interbattle_noncombat and pair_b2),
    }


def qualifying_preflight_seeds(owners: Sequence[Mapping[str, Any]]) -> list[int]:
    return [int(row["seed"]) for row in owners
        if row.get("source_disposition") == "eligible_root" and preflight_qualification(row)["qualifies"]]


def append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(canonical(row), sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def item_key(item: Mapping[str, Any]) -> tuple[int, int, str, str]:
    return (int(item["potion_index"]), int(item["id"]), str(item["id_label"]), str(item["name"]))


def get_potions(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = raw.get("battle_potions")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise RuntimeError("native battle_potions identity is unavailable")
    return [dict(x) for x in rows if isinstance(x, Mapping)]


class StopAtRoot(BaseException):
    pass


class StopAtB1Exit(BaseException):
    pass


class BudgetStop(BaseException):
    pass


class EventJournal:
    def __init__(self, root: Path) -> None:
        self.path = Path(os.environ.get("ISSUE56_BUDGET_LEDGER", "studies/issue56/budget_events.jsonl"))
        self.cpu_started = time.process_time()
        self.prior_cpu_s = 0.0
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        self.prior_cpu_s = float(json.loads(line).get("elapsed_process_cpu_s", self.prior_cpu_s))
                    except (ValueError, TypeError):
                        continue
        self.last_stage = "initialized"
        self.last_cpu_checkpoint_s = self.prior_cpu_s

    def event(self, owner: int | None, stage: str, **fields: Any) -> None:
        self.last_stage = stage
        append_jsonl(self.path, {"owner_seed": owner, "stage": stage,
            "elapsed_process_cpu_s": round(self.prior_cpu_s + time.process_time() - self.cpu_started, 3), **fields})

    def check_budget(self, owner: int | None = None) -> None:
        elapsed = self.prior_cpu_s + time.process_time() - self.cpu_started
        if elapsed >= CPU_CEILING_SECONDS:
            self.event(owner, "builder_cpu_ceiling", cpu_s=round(elapsed, 3))
            raise BudgetStop
        if elapsed - self.last_cpu_checkpoint_s >= 5.0:
            append_jsonl(self.path, {"owner_seed": owner, "stage": "cpu_checkpoint",
                "boundary": self.last_stage, "elapsed_process_cpu_s": round(elapsed, 3)})
            self.last_cpu_checkpoint_s = elapsed


@dataclasses.dataclass
class Root:
    checkpoint: Any
    fingerprint: Any
    raw: dict[str, Any]
    actions: list[Any]
    action_identities: list[dict[str, Any]]
    selected_identity: dict[str, Any]
    potion: dict[str, Any]
    rng_state: Any


class StudySearch:
    def __init__(self, mode: str, root_state: dict[str, Any], root: Root | None = None) -> None:
        self.mode, self.root_state, self.root = mode, root_state, root
        self.provenance = ControllerProvenance(kind="study_native_search_v2",
            name=f"issue56_search_v2_400_{mode}", config={
                "simulations": SIMULATIONS, "include_potions": True,
                "selection": "highest_mean", "native_api": SEARCH_API,
                "native_patch": SEARCH_PATCH, "information_regime": "full_simulator_state_oracle_like",
                "root_mask": "exact_b1_potion_instance_use_and_discard" if mode == "reserve" else None})

    def select_action(self, adapter: Any, snapshot: Any, actions: Sequence[Any], context: DecisionContext, step_index: int) -> ControllerDecision:
        if self.mode in {"force", "reserve"} and self.root is not None and not self.root_state.get("b1_closed"):
            if is_battle_state(snapshot.raw, context.screen_state):
                keys = getattr(adapter, "checkpoint_fingerprint_transition_only_raw_keys", ())
                self.root_state.setdefault("b1_raw_state_fingerprints", []).append(
                    raw_state_fingerprint(snapshot.raw, keys))
        if self.mode == "force" and step_index == 0:
            from sts_combat_rl.sim.decision_record import find_action_index_by_identity
            return ControllerDecision(find_action_index_by_identity(actions, self.root.selected_identity),
                self.provenance, reason="force_exact_source_action")
        masked_context = context
        if self.mode == "reserve" and self.root is not None and not self.root_state.get("b1_closed"):
            if is_battle_state(snapshot.raw, context.screen_state):
                self.root_state["b1_seen"] = True
                allowed = eligible_after_exact_potion_mask(actions, context, self.root.potion, snapshot.raw)
                if not allowed:
                    raise RuntimeError("B1 potion mask leaves no legal action")
                self.root_state["masked_decisions"] = self.root_state.get("masked_decisions", 0) + 1
                self.root_state["masked_candidates"] = self.root_state.get("masked_candidates", 0) + len(context.eligible_action_indices) - len(allowed)
                masked_context = dataclasses.replace(context, eligible_action_indices=allowed)
            elif self.root_state.get("b1_seen"):
                self.root_state["b1_closed"] = True
        if self.mode == "reserve" and self.root_state.get("b1_closed") and is_battle_state(snapshot.raw, context.screen_state):
            self.root_state["b2_unmasked_decisions"] = self.root_state.get("b2_unmasked_decisions", 0) + 1
        start = time.process_time()
        raw = adapter.battle_search_v2(snapshot, simulations=SIMULATIONS,
            include_potions=True, policy_prior_callback=None, leaf_value_callback=None)
        journal = self.root_state.get("journal")
        if journal is not None:
            journal.check_budget(self.root_state.get("owner_seed"))
        report = build_oracle_search_report(raw, actions, masked_context,
            expected_native_api=SEARCH_API, expected_patch_identity=SEARCH_PATCH,
            wall_clock_time_s=time.process_time() - start)
        if not report.search_ok:
            raise RuntimeError("native Search-v2 report invalid: " + "; ".join(report.problems))
        target = select_oracle_root_action(report, selection_rule="highest_mean")
        action = actions[target.legal_action_index]
        if self.mode == "source" and self.root_state.get("b1_seen") and action.kind == "potion" and is_smoke(potion_at_action_slot(snapshot.raw, action)):
            self.root_state["smoke_uses"] += 1
        if self.mode == "source" and self.root_state.get("b1_seen") and is_qualifying_source_use(action, snapshot.raw):
            identities = action_identity_dicts_for_actions(actions)
            self.root = Root(checkpoint=adapter.capture_checkpoint(snapshot),
                fingerprint=adapter.checkpoint_fingerprint(snapshot), raw=dict(snapshot.raw),
                actions=list(actions), action_identities=identities,
                selected_identity=identities[target.legal_action_index],
                potion=potion_at_action_slot(snapshot.raw, action),
                rng_state=self.root_state["driver"]._rng.getstate())
            self.root_state["search_root_report"] = {"native_api": report.native_api,
                "patch_identity": report.patch_identity, "include_potions": report.include_potions,
                "simulations_requested": report.simulations_requested, "root_visits": report.root_visits}
            raise StopAtRoot
        return ControllerDecision(target.legal_action_index, self.provenance,
            reason="native_search_v2_highest_mean", score=target.score)




def potion_at_action_slot(raw: Mapping[str, Any], action: Any) -> dict[str, Any]:
    slot = int(action.raw.get("idx1", -1))
    rows = get_potions(raw)
    matches = [row for row in rows if int(row.get("potion_index", -1)) == slot]
    if len(matches) != 1:
        raise RuntimeError(f"potion slot {slot} does not map to one native item")
    item = matches[0]
    if str(item.get("id_label", "")).upper() in {"", "INVALID", "NONE"}:
        raise RuntimeError(f"potion use at slot {slot} has no item identity")
    return item


def is_smoke(item: Mapping[str, Any]) -> bool:
    return str(item.get("id_label", "")).upper() == "SMOKE_BOMB" or str(item.get("name", "")) == "Smoke Bomb"


def is_qualifying_source_use(action: Any, raw: Mapping[str, Any]) -> bool:
    if action.kind != "potion":
        return False
    return not is_smoke(potion_at_action_slot(raw, action))


class ResumeAdapter:
    """One-run view that makes the canonical executor restore this exact native root."""

    def __init__(self, base: LightSpeedAdapter, root: Root) -> None:
        self.base, self.root = base, root
        self.restored = False
        self.fingerprint_ok = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)

    def reset(self, seed: int | None = None) -> Any:
        if self.restored:
            raise RuntimeError("resume adapter can restore only once")
        self.restored = True
        snapshot = self.base.restore_checkpoint(self.root.checkpoint)
        self.fingerprint_ok = self.base.checkpoint_fingerprint(snapshot) == self.root.fingerprint
        if not self.fingerprint_ok:
            raise RuntimeError("root checkpoint observation/raw fingerprint mismatch")
        return snapshot


class PreservedDriver:
    """Keep the captured expert RNG stream; it is never serialized."""

    name = "expert_non_combat_v1"

    def __init__(self, driver: ExpertNonCombatDriver) -> None:
        self.driver = driver
        self.provenance_config = driver.provenance_config

    def reset_for_run(self, seed: int | None) -> None:
        del seed

    def select_action(self, context: DecisionContext) -> Any:
        return self.driver.select_action(context)


class RunTrace:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.battle_exits: list[dict[str, Any]] = []
        self.battle_entries = 0
        self.b2_resolved = False
        self.terminal = False
        self.outcome = "UNKNOWN"
        self.terminal_act: Any = None
        self.b2_outcome: str | None = None
        self.masked_decisions = 0
        self.masked_candidates = 0

    def transition(self, step: ControlledRunStep) -> None:
        row = {
            "step": step.step_index, "controller_role": step.controller_role,
            "screen": step.screen_state, "next_screen": step.next_screen_state,
            "kind": step.chosen_action_kind, "action": step.chosen_action_identity,
            "floor": step.floor, "hp": step.player_hp, "max_hp": step.player_max_hp,
            "gold": step.gold, "potion_count": step.potion_count,
            "next_floor": step.next_floor, "next_hp": step.next_player_hp,
            "next_max_hp": step.next_player_max_hp, "next_gold": step.next_gold,
            "next_potion_count": step.next_potion_count,
            "battle_active": step.battle_active, "next_battle_active": step.next_battle_active,
            "battle_outcome": step.next_battle_outcome,
            "potion_inventory": step.snapshot_raw.get("battle_potions", step.snapshot_raw.get("potions")),
            "next_potion_inventory": step.next_snapshot_raw.get("battle_potions", step.next_snapshot_raw.get("potions")),
            "visible_reward_context": {k: step.next_snapshot_raw.get(k) for k in ("gold", "relics", "keys", "floor_num", "map_node", "rewards", "reward_cards", "reward_gold", "reward_relics", "reward_potions") if k in step.next_snapshot_raw},
        }
        self.steps.append(row)
        if step.next_battle_active and not step.battle_active:
            self.battle_entries += 1
        if step.battle_active and not step.next_battle_active:
            outcome = step.next_battle_outcome or step.battle_outcome or "UNKNOWN"
            exit_row = {
                "battle_number": len(self.battle_exits) + 1,
                "outcome": outcome,
                "next_floor": step.next_floor, "next_hp": step.next_player_hp,
                "next_max_hp": step.next_player_max_hp, "next_gold": step.next_gold,
                "next_potion_count": step.next_potion_count,
                "potion_inventory": row["next_potion_inventory"],
                "next_screen": step.next_screen_state,
            }
            self.battle_exits.append(exit_row)
            if exit_row["battle_number"] == 2:
                self.b2_resolved, self.b2_outcome = True, outcome
            exit_keys = ("act", "floor_num", "gold", "relics", "battle_relics", "deck", "deck_summary", "map_node", "next_map_node", "reward", "rewards", "keys")
            exit_row["equivalence_state"] = {k: step.next_snapshot_raw.get(k) for k in exit_keys if k in step.next_snapshot_raw}
        self.terminal = bool(step.terminal_after_step)
        self.outcome = str(step.next_snapshot_raw.get("outcome", "UNKNOWN"))
        if self.terminal:
            self.terminal_act = step.next_snapshot_raw.get("act")


def make_controllers(driver: ExpertNonCombatDriver, battle: StudySearch) -> RoutedRunController:
    return RoutedRunController(battle=battle, non_combat=PolicyController(PreservedDriver(driver)))


def branch_primary(trace: RunTrace) -> dict[str, Any]:
    heart_victory = (trace.terminal and trace.outcome.upper() in {"VICTORY", "HEART_VICTORY", "PLAYER_VICTORY"}
        and trace.terminal_act == 4)
    if trace.b2_resolved:
        out = str(trace.b2_outcome or "UNKNOWN").upper()
        if out in {"PLAYER_VICTORY", "PLAYER_ESCAPE", "VICTORY", "ESCAPE"}:
            b2_survived = True
            return {"primary": 1, "status": "b2_resolved", "b2_outcome": out,
                "b2_survived": b2_survived, "b2_escape": out in {"PLAYER_ESCAPE", "ESCAPE"}}
        if out in {"PLAYER_LOSS", "LOSS", "DEATH"}:
            return {"primary": 0, "status": "b2_true_death", "b2_outcome": out,
                "b2_survived": False, "b2_escape": False}
        return {"primary": None, "status": "unclassified_b2_outcome", "b2_outcome": out,
            "b2_survived": None, "b2_escape": False}
    if heart_victory:
        return {"primary": 1, "status": "terminal_heart_victory_before_b2", "b2_outcome": None,
            "b2_survived": None, "b2_escape": False}
    if trace.terminal:
        if trace.outcome.upper() in {"PLAYER_LOSS", "LOSS", "DEATH"}:
            return {"primary": 0, "status": "true_death_before_b2", "b2_outcome": None,
                "b2_survived": None, "b2_escape": False}
        return {"primary": None, "status": "unclassified_terminal_before_b2", "b2_outcome": None,
            "b2_survived": None, "b2_escape": False}
    return {"primary": None, "status": "incomplete_no_b2_resolution", "b2_outcome": None,
        "b2_survived": None, "b2_escape": False}


def eligible_after_exact_potion_mask(actions: Sequence[Any], context: DecisionContext, source: Mapping[str, Any], raw: Mapping[str, Any]) -> list[int]:
    expected = item_key(source)
    current = [row for row in get_potions(raw) if str(row.get("id_label", "")).upper() not in {"", "INVALID", "NONE"}]
    matches = [row for row in current if item_key(row) == expected]
    if len(matches) != 1:
        raise RuntimeError("exact source potion identity moved or changed under B1 reserve intervention")
    banned = set()
    for index in context.eligible_action_indices:
        action = actions[index]
        if action.kind in POTION_KINDS and int(action.raw.get("idx1", -1)) == expected[0]:
            banned.add(index)
    return [index for index in context.eligible_action_indices if index not in banned]


def run_source(seed: int, journal: EventJournal, output: Path) -> tuple[dict[str, Any], Root | None, LightSpeedAdapter]:
    journal.check_budget(seed)
    adapter = LightSpeedAdapter(seed=seed, ascension=20, module=slaythespire)
    driver = ExpertNonCombatDriver(DRIVER_SEED)
    state: dict[str, Any] = {"b1_seen": False, "driver": driver, "smoke_uses": 0,
        "journal": journal, "owner_seed": seed}
    battle = StudySearch("source", state)
    controller = RoutedRunController(battle=battle, non_combat=PolicyController(driver))
    source_exit: dict[str, Any] = {}

    def before(snapshot: Any, actions: Sequence[Any], context: DecisionContext, step_index: int) -> None:
        del actions, step_index
        if is_battle_state(snapshot.raw, context.screen_state):
            state["b1_seen"] = True

    def after(step: ControlledRunStep) -> None:
        if step.battle_active and not step.next_battle_active:
            source_exit.update({"battle_outcome": step.next_battle_outcome or step.battle_outcome or "UNKNOWN",
                "terminal": step.terminal_after_step, "outcome": step.next_snapshot_raw.get("outcome", "UNKNOWN"),
                "floor": step.next_floor, "hp": step.next_player_hp, "max_hp": step.next_player_max_hp,
                "gold": step.next_gold, "potion_count": step.next_potion_count,
                "potion_inventory": step.next_snapshot_raw.get("battle_potions", step.next_snapshot_raw.get("potions"))})
            raise StopAtB1Exit

    journal.event(seed, "source_started")
    try:
        result = execute_controlled_run(adapter, controller, seed=seed, max_steps=MAX_STEPS,
            action_space=ActionSpaceConfig.include_all(), before_decision=before,
            after_transition=after, retain_steps=False)
        source_exit.update({"terminal": result.terminal, "outcome": result.outcome,
            "problems": result.problems, "final_screen": result.final_raw.get("screen_state"),
            "final_floor": result.final_raw.get("floor_num"), "final_hp": result.final_raw.get("player_hp"),
            "final_max_hp": result.final_raw.get("player_max_hp")})
    except StopAtRoot:
        pass
    except StopAtB1Exit:
        pass
    except BudgetStop:
        adapter.close()
        raise
    except Exception:
        adapter.close()
        raise

    root = battle.root
    if root is not None:
        disposition = "eligible_root"
    elif source_exit.get("problems"):
        disposition = "source_error"
    elif source_exit.get("terminal"):
        disposition = "source_terminal_before_use"
    elif source_exit.get("battle_outcome") is not None:
        disposition = "smoke_only" if state["smoke_uses"] else "no_qualifying_use_b1"
    else:
        disposition = "source_incomplete_before_b1_exit"
    record = {"seed": seed, "source_disposition": disposition,
        "source_b1_outcome": source_exit.get("battle_outcome"),
        "source_smoke_uses_b1": state["smoke_uses"],
        "source_terminal": source_exit.get("terminal", False),
        "source_outcome": source_exit.get("outcome", "UNKNOWN"),
        "source_problems": source_exit.get("problems", []),
        "source_root": None if root is None else {"visible_snapshot": {k: root.raw.get(k) for k in ("screen_state", "floor_num", "player_hp", "player_max_hp", "gold", "battle_potions")},
            "potion": root.potion, "chosen_action": root.selected_identity,
            "search": state.get("search_root_report")},
        "source_b1_exit": source_exit or None}
    append_jsonl(output, record)
    journal.event(seed, "source_root_found" if root else disposition,
        potion=None if root is None else root.potion)
    if root is None:
        adapter.close()
    return record, root, adapter





class DetailedTrace(RunTrace):
    def __init__(self, root: Root, mode: str, adapter: Any) -> None:
        super().__init__()
        self.root, self.mode = root, mode
        self.transition_only_raw_keys = getattr(adapter, "checkpoint_fingerprint_transition_only_raw_keys", ())
        self.b1_transition_state_fingerprints: list[str] = []
        self.b1_exact_use = 0
        self.b1_exact_discard = 0
        self.b2_exact_use = 0
        self.b2_exact_discard = 0
        self.b2_exact_use_opportunity = False
        self.b1_decisions = 0
        self.b2_decisions = 0
        self.b1_mask_violations = 0
        self.screens = Counter()
        self.actions_by_screen = Counter()

    def transition(self, step: ControlledRunStep) -> None:
        battle_number = len(self.battle_exits) + 1
        if step.battle_active and battle_number == 1:
            self.b1_transition_state_fingerprints.append(
                raw_state_fingerprint(step.next_snapshot_raw, self.transition_only_raw_keys))
        tactical = step.tactical_legal_actions
        selected = tactical[step.chosen_action_index] if step.chosen_action_index < len(tactical) else {}
        params = selected.get("parameters", {}) if isinstance(selected, Mapping) else {}
        kind = str(selected.get("kind", step.chosen_action_kind)) if isinstance(selected, Mapping) else step.chosen_action_kind
        slot = params.get("idx1") if isinstance(params, Mapping) else None
        current_items = step.snapshot_raw.get("battle_potions", [])
        exact_present = any(isinstance(x, Mapping) and item_key(x) == item_key(self.root.potion) for x in current_items)
        exact_action = kind in POTION_KINDS and slot == item_key(self.root.potion)[0] and exact_present
        if step.battle_active:
            self.screens[step.screen_state] += 1
            self.actions_by_screen[f"{step.screen_state}:{kind}"] += 1
            if battle_number == 1:
                self.b1_decisions += 1
            elif battle_number == 2:
                self.b2_decisions += 1
                self.b2_exact_use_opportunity = self.b2_exact_use_opportunity or any(
                    isinstance(candidate, Mapping)
                    and candidate.get("kind") == "potion"
                    and isinstance(candidate.get("parameters"), Mapping)
                    and candidate["parameters"].get("idx1") == item_key(self.root.potion)[0]
                    and exact_present
                    for candidate in tactical
                )
        if exact_action:
            if kind == "potion":
                if battle_number == 1:
                    self.b1_exact_use += 1
                elif battle_number == 2:
                    self.b2_exact_use += 1
            else:
                if battle_number == 1:
                    self.b1_exact_discard += 1
                elif battle_number == 2:
                    self.b2_exact_discard += 1
        if self.mode == "reserve" and battle_number == 1 and exact_action:
            self.b1_mask_violations += 1
        super().transition(step)
        self.steps[-1]["selected_tactical_action"] = canonical(selected)





def run_branch(adapter: LightSpeedAdapter, root: Root, seed: int, mode: str, journal: EventJournal) -> tuple[RunTrace, Mapping[str, Any], dict[str, Any]]:
    driver = ExpertNonCombatDriver(DRIVER_SEED)
    driver._rng.setstate(root.rng_state)
    root_state: dict[str, Any] = {"b1_seen": False, "b1_closed": False,
        "masked_decisions": 0, "masked_candidates": 0, "b2_unmasked_decisions": 0,
        "journal": journal, "owner_seed": seed, "b1_raw_state_fingerprints": []}
    battle = StudySearch(mode, root_state, root)
    if mode == "consume":
        battle.mode = "force"
    controller = make_controllers(driver, battle)
    resume = ResumeAdapter(adapter, root)
    trace = DetailedTrace(root, mode, resume)

    def before(snapshot: Any, actions: Sequence[Any], context: DecisionContext, step: int) -> None:
        del snapshot, actions, context, step
        journal.check_budget(seed)

    def after(step: ControlledRunStep) -> None:
        trace.transition(step)
        if mode == "reserve" and step.battle_active and not step.next_battle_active:
            root_state["b1_closed"] = True

    journal.event(seed, f"{mode}_branch_started")
    result = execute_controlled_run(resume, controller, seed=seed, max_steps=MAX_STEPS,
        action_space=ActionSpaceConfig.include_all(), before_decision=before,
        after_transition=after, retain_steps=True)
    if result.problems:
        raise RuntimeError("controlled continuation problems: " + "; ".join(result.problems))
    trace.terminal, trace.outcome = result.terminal, result.outcome
    if not resume.fingerprint_ok:
        raise RuntimeError("executor did not restore the exact root")
    root_state["root_restore_equal"] = resume.fingerprint_ok
    if mode == "reserve":
        trace.masked_decisions = root_state["masked_decisions"]
        trace.masked_candidates = root_state["masked_candidates"]
        if trace.masked_candidates < 1:
            raise RuntimeError("B reserve mask did not suppress the exact source action")
        if trace.b1_mask_violations:
            raise RuntimeError("B consumed or discarded the source potion during B1")
        if not root_state["b1_closed"] and trace.b1_decisions > 0 and not trace.terminal:
            raise RuntimeError("B1 did not close before continuation reached its cap")
        if root_state["b2_unmasked_decisions"] != trace.b2_decisions:
            raise RuntimeError("B2 did not run under the unmasked policy")
    return trace, controller.provenance.to_dict(), root_state


def percentiles(values: Sequence[float], probs: tuple[float, ...] = (0.025, 0.975)) -> list[float] | None:
    if not values:
        return None
    ordered = sorted(float(x) for x in values)
    result = []
    for p in probs:
        position = (len(ordered) - 1) * p
        lo, hi = int(position), min(int(position) + 1, len(ordered) - 1)
        frac = position - lo
        result.append(ordered[lo] * (1.0 - frac) + ordered[hi] * frac)
    return result


def paired_statistics(owners: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in owners if row.get("source_disposition") == "eligible_root"]
    complete = []
    lower: list[float] = []
    upper: list[float] = []
    discordant = {"consume_only_success": 0, "reserve_only_success": 0, "ties": 0}
    for owner in eligible:
        a = owner.get("consume", {}).get("endpoint", {}).get("primary")
        b = owner.get("reserve", {}).get("endpoint", {}).get("primary")
        if a in (0, 1) and b in (0, 1):
            d = int(a) - int(b)
            complete.append((d, int(a), int(b)))
            lower.append(d)
            upper.append(d)
            if a == 1 and b == 0:
                discordant["consume_only_success"] += 1
            elif a == 0 and b == 1:
                discordant["reserve_only_success"] += 1
            else:
                discordant["ties"] += 1
        else:
            low = (0 if a is None else int(a)) - (1 if b is None else int(b))
            high = (1 if a is None else int(a)) - (0 if b is None else int(b))
            lower.append(low)
            upper.append(high)
    n = len(eligible)
    dvals = [x[0] for x in complete]
    rng = random.Random(BOOTSTRAP_SEED)
    boot_low: list[float] = []
    boot_high: list[float] = []
    if n:
        for _ in range(10000):
            sample = [rng.randrange(n) for _ in range(n)]
            # Re-sample owner indices, retaining each owner's paired bound.
            boot_low.append(sum(lower[i] for i in sample) / n)
            boot_high.append(sum(upper[i] for i in sample) / n)
    complete_ci = None
    if complete:
        rng2 = random.Random(BOOTSTRAP_SEED)
        samples = []
        for _ in range(10000):
            samples.append(sum(dvals[rng2.randrange(len(dvals))] for _ in dvals) / len(dvals))
        complete_ci = percentiles(samples)
    any_missing = len(complete) != n
    fixed_bounds = [sum(lower) / n, sum(upper) / n] if n else None
    return {
        "eligible_owner_denominator": n,
        "complete_paired_owners": len(complete),
        "complete_case_paired_difference_consume_minus_reserve": sum(dvals) / len(dvals) if complete else None,
        "complete_case_owner_bootstrap_percentile_95_ci": complete_ci,
        "primary_fixed_denominator_difference_bounds": fixed_bounds,
        "primary_owner_bootstrap_95_bound_envelope": [percentiles(boot_low), percentiles(boot_high)] if n else None,
        "incomplete_or_invalid_pairs": n - len(complete),
        "discordant_pairs": discordant,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "bootstrap_replicates": 10000,
        "primary_estimator": "fixed-denominator worst-case bounds with owner-resampled paired bootstrap envelope",
        "eligible_owners_with_any_branch_reaching_b2": sum(
            bool(owner.get("consume", {}).get("trace", {}).get("b2_decisions", 0)
                 or owner.get("reserve", {}).get("trace", {}).get("b2_decisions", 0))
            for owner in eligible),
        "eligible_owners_with_both_branches_reaching_b2": sum(
            bool(owner.get("consume", {}).get("trace", {}).get("b2_decisions", 0)
                 and owner.get("reserve", {}).get("trace", {}).get("b2_decisions", 0))
            for owner in eligible),
    }





EXPECTED_STSRL_COMMIT = "7d172f7c24c9d29429b6946ba44f6b59760b0d96"
EXPECTED_STSRL_HASHES = {
    "src/sts_combat_rl/sim/controlled_run.py": "dd466e40c240dcc7a8d9ec9fa4f8259dae093b477684d2cb89746e29514ebe03",
    "src/sts_combat_rl/sim/lightspeed.py": "b611748a823c7f2d0e4961dd88e9d392248b67829e62d326358162238cf1f0b0",
    "src/sts_combat_rl/sim/action_space.py": "20502318a7b290d47fd43c0b176e1141df6bd1ec2ca6bedb6951ab289e48a229",
    "src/sts_combat_rl/sim/non_combat_policy.py": "6af4e3b27e1f9066699137ed9947c1897cd16404e14b4d647555e6d36f3a580d",
    "src/sts_combat_rl/sim/decision_record.py": "1aa3bb511d756c850f08f9612c2ff5adaa7a3c309451d35e13793a93c4b11126",
    "src/sts_combat_rl/sim/oracle_search.py": "b4814e7c5584112ed2b3944917950e1a6b42e39e4cd8410f0519f0b28a3a40cc",
    "src/sts_combat_rl/sim/controller_contract.py": "2e6a9d771201a39d882f8226e36497345243fc6006a13e98c43375a7b66dc16f",
    "src/sts_combat_rl/sim/online_controller.py": "7762b6093a76cbee1c1f805a992bf396760df60c62d9dd43758bb073f0a23b8b",
    "src/sts_combat_rl/sim/policy_contract.py": "d7370fb2115cabc5b3288a1f1609ed6e6a44d3d6c7b23eb76f95adee6c36c56a",
    "src/sts_combat_rl/sim/features.py": "bffac00044f72eee3f8fc7b3f105a6ac5a4ee4caa07e9dac0dc9372ec98392a0",
    "src/sts_combat_rl/sim/public_run_context.py": "d7f0de4fb99e3fae5ce0cc6e18799f9ca39470adedb276094381dd1e1281bc46",
    "src/sts_combat_rl/sim/native_public_projection.py": "64b0fb995c84cf8b624555b05edd210b2e721cae6d43cc507bb7976b6bbd1426",
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def source_manifest(output: Path, preflight_gate: Mapping[str, Any] | None = None) -> dict[str, Any]:
    module_path = Path(slaythespire.__file__).resolve()
    if not module_path.exists() or module_path.parent != NATIVE_MODULE_DIR.resolve() or sha256_file(module_path) != NATIVE_MODULE_SHA256:
        raise RuntimeError("loaded native extension path/hash is not the frozen build")
    native_repo = Path("/mnt/d/DeadlycatCoding/issue-56-sts_lightspeed") if os.name == "posix" else Path(r"D:\DeadlycatCoding\issue-56-sts_lightspeed")
    if git(STSRL, "rev-parse", "HEAD") != EXPECTED_STSRL_COMMIT:
        raise RuntimeError("STSRL checkout does not match the frozen commit")
    if git(native_repo, "rev-parse", "HEAD") != NATIVE_COMMIT:
        raise RuntimeError("sts_lightspeed source does not match the frozen commit")
    st_hashes = {name: sha256_file(STSRL / name) for name in EXPECTED_STSRL_HASHES}
    mismatches = {name: (value, EXPECTED_STSRL_HASHES[name]) for name, value in st_hashes.items()
                  if value != EXPECTED_STSRL_HASHES[name]}
    if mismatches:
        raise RuntimeError("pinned STSRL source hash mismatch: " + repr(mismatches))
    producer_commit = os.environ.get("SPIRE_WORKTREE_COMMIT")
    if not producer_commit or len(producer_commit) != 40:
        raise RuntimeError("SPIRE_WORKTREE_COMMIT must identify the exact pushed producer commit")
    manifest = {
        "issue": 56,
        "study_disposition": "STUDY_ONLY",
        "source_seed_order": list(SEEDS),
        "source_seed_count": len(SEEDS),
        "source_selection": "first selected legal non-Smoke potion use in B1; one root maximum per owner",
        "simulator": {"repository": "lsmfttb/sts_lightspeed", "commit": NATIVE_COMMIT,
            "module_path": str(module_path), "module_sha256": NATIVE_MODULE_SHA256,
            "build": {"type": "Release", "cmake": "4.2.3", "compiler": "GNU g++ 15",
                "python": "3.14.4", "build_cpu_s": 76.69}},
        "executor_repository": "lsmfttb/STSRL",
        "executor_commit": EXPECTED_STSRL_COMMIT,
        "executor_source_sha256": st_hashes,
        "search": {"api": SEARCH_API, "patch_identity": SEARCH_PATCH,
            "simulations": SIMULATIONS, "include_potions": True,
            "selection": "highest_mean", "prior": None, "leaf_value": None},
        "non_combat_driver": {"name": "expert_non_combat_v1", "seed": DRIVER_SEED,
            "source": "same deterministic seeded stream; branch copies exact in-memory RNG state at root"},
        "bootstrap": {"unit": "original source owner", "seed": BOOTSTRAP_SEED,
            "replicates": 10000, "interval": "percentile 95%"},
        "budget": {"issue_hard_cpu_s": 10000, "runner_cpu_ceiling_s": CPU_CEILING_SECONDS,
            "native_build_cpu_s": 76.69, "remaining_setup_admin_allowance_cpu_s": 1200,
            "reviewer_reserved_cpu_s": 1500},
        "limits": {"max_steps_per_cursor": MAX_STEPS, "no outcome-adaptive seed replacement": True,
            "native checkpoint": "opaque process-memory only; never serialized"},
        "budget_event_ledger": "studies/issue56/budget_events.jsonl",
        "study_code_sha256": sha256_file(Path(__file__).resolve()),
        "producer_checkout": producer_commit,
    }
    if preflight_gate is not None:
        manifest["preflight_gate"] = dict(preflight_gate)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "manifest.json"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old != manifest:
            raise RuntimeError("existing artifact directory has a different frozen producer/preflight manifest")
        return old
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def branch_record(trace: DetailedTrace, provenance: Mapping[str, Any], root_state: Mapping[str, Any], mode: str) -> dict[str, Any]:
    endpoint = branch_primary(trace)
    # Keep the full public decision/transition ledger through B2 or authentic terminal.
    noncombat_between = 0
    after_b1 = False
    for row in trace.steps:
        if after_b1 and row["controller_role"] == "non_combat_driver":
            noncombat_between += 1
        if row["battle_active"] and not row["next_battle_active"]:
            after_b1 = True
        elif after_b1 and row["battle_active"]:
            after_b1 = False
    return {
        "mode": mode,
        "root_restore_equal": bool(root_state.get("root_restore_equal")),
        "endpoint": endpoint,
        "trace": {
            "decisions": len(trace.steps),
            "b1_decisions": trace.b1_decisions,
            "between_b1_b2_noncombat_decisions": noncombat_between,
            "b2_decisions": trace.b2_decisions,
            "b2_exact_potion_use_opportunity": trace.b2_exact_use_opportunity,
            "b1_exact_source_potion_uses": trace.b1_exact_use,
            "b1_exact_source_potion_discards": trace.b1_exact_discard,
            "b2_exact_source_potion_uses": trace.b2_exact_use,
            "b2_exact_source_potion_discards": trace.b2_exact_discard,
            "b1_mask_closed": bool(root_state.get("b1_closed")),
            "b1_masked_decisions": trace.masked_decisions,
            "b1_masked_candidates": trace.masked_candidates,
            "b1_mask_violations": trace.b1_mask_violations,
            "b1_raw_state_fingerprint_sha256": [
                *root_state.get("b1_raw_state_fingerprints", []),
                *trace.b1_transition_state_fingerprints,
            ],
            "b2_unmasked_decisions": root_state.get("b2_unmasked_decisions", 0),
            "battle_entries_after_root": trace.battle_entries,
            "battle_exits": trace.battle_exits,
            "terminal_outcome": trace.outcome if trace.terminal else None,
            "terminal_act": trace.terminal_act,
            "screens": dict(trace.screens),
            "actions_by_screen": dict(trace.actions_by_screen),
            "public_transition_ledger": trace.steps,
        },
        "controller_provenance": provenance,
    }


def run_owner(seed: int, journal: EventJournal, output: Path, source_attempts: Path) -> dict[str, Any]:
    journal.event(seed, "owner_started")
    source, root, adapter = run_source(seed, journal, source_attempts)
    owner: dict[str, Any] = dict(source)
    if root is None:
        journal.event(seed, "owner_completed", disposition=source["source_disposition"])
        return owner
    try:
        for mode, key in (("consume", "consume"), ("reserve", "reserve")):
            journal.check_budget(seed)
            try:
                trace, provenance, root_state = run_branch(adapter, root, seed, mode, journal)
                owner[key] = branch_record(trace, provenance, root_state, mode)
            except BudgetStop:
                raise
            except Exception as exc:
                owner[key] = {"endpoint": {"primary": None, "status": "branch_validity_failure"},
                    "error": f"{type(exc).__name__}: {exc}"}
        a, b = owner.get("consume"), owner.get("reserve")
        source_action = root.selected_identity
        checks = {
            "consume_forced_exact_source_action": bool(a and a.get("trace", {}).get("public_transition_ledger")
                and a["trace"]["public_transition_ledger"][0]["action"] == source_action),
            "consume_used_exact_source_potion_at_root": bool(a and a.get("trace", {}).get("b1_exact_source_potion_uses", 0) >= 1),
            "reserve_first_action_differs": bool(b and b.get("trace", {}).get("public_transition_ledger")
                and b["trace"]["public_transition_ledger"][0]["action"] != source_action),
            "reserve_never_used_or_discarded_exact_source_potion_in_b1": bool(b and b.get("trace", {}).get("b1_mask_violations", -1) == 0),
            "reserve_masked_source_action": bool(b and b.get("trace", {}).get("b1_masked_candidates", 0) >= 1),
            "reserve_mask_ended_at_b1_exit": bool(b and b.get("trace", {}).get("battle_exits") and
                b.get("trace", {}).get("b1_mask_closed") and b.get("trace", {}).get("b1_masked_decisions", 0) >= 1),
            "both_branches_restored_exact_root": bool(a and b and a.get("root_restore_equal") and b.get("root_restore_equal")),
            "reserve_potion_present_at_b1_exit": bool(b and b.get("trace", {}).get("battle_exits") and
                any(isinstance(x, Mapping) and item_key(x) == item_key(root.potion)
                    for x in (b["trace"]["battle_exits"][0].get("potion_inventory") or []))),
            "reserve_b2_unmasked": bool(b and b.get("trace", {}).get("b2_unmasked_decisions", 0) ==
                b.get("trace", {}).get("b2_decisions", -1)),
        }
        owner["preflight_integrity"] = checks
        owner["preflight_integrity_pass"] = all(checks.values())
        owner["preflight_qualification"] = preflight_qualification(owner)
    finally:
        adapter.close()
    journal.event(seed, "owner_completed", disposition="eligible_root",
        integrity_pass=owner.get("preflight_integrity_pass"))
    return owner





def strata_report(owners: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in owners if row.get("source_disposition") == "eligible_root"]
    dimensions: dict[str, dict[str, list[Mapping[str, Any]]]] = {
        "floor_band": {}, "hp_fraction_band": {}, "potion_id_label": {}}
    for owner in eligible:
        root = owner.get("source_root", {})
        snap = root.get("visible_snapshot", {})
        floor = snap.get("floor_num")
        floor = float(floor) if isinstance(floor, (int, float)) else None
        floor_band = "act1_0_17" if floor is not None and floor <= 17 else (
            "act2_18_34" if floor is not None and floor <= 34 else "act3_35_plus" if floor is not None else None)
        hp, max_hp = snap.get("player_hp"), snap.get("player_max_hp")
        fraction = float(hp) / float(max_hp) if isinstance(hp, (int, float)) and isinstance(max_hp, (int, float)) and max_hp > 0 else None
        hp_band = ("0_25" if fraction <= .25 else "25_50" if fraction <= .5 else
                   "50_75" if fraction <= .75 else "75_100") if fraction is not None else None
        item = root.get("potion", {})
        potion = str(item.get("id_label", "UNKNOWN"))
        for name, value in (("floor_band", floor_band), ("hp_fraction_band", hp_band), ("potion_id_label", potion)):
            if value is not None:
                dimensions[name].setdefault(value, []).append(owner)
    out: dict[str, Any] = {}
    for name, groups in dimensions.items():
        out[name] = {}
        for label, group in sorted(groups.items()):
            if len(group) < 5:
                continue
            pairs = []
            for owner in group:
                a = owner.get("consume", {}).get("endpoint", {}).get("primary")
                b = owner.get("reserve", {}).get("endpoint", {}).get("primary")
                if a in (0, 1) and b in (0, 1):
                    pairs.append(int(a) - int(b))
            out[name][label] = {"owner_n": len(group), "complete_pair_n": len(pairs),
                "descriptive_complete_pair_difference": sum(pairs) / len(pairs) if pairs else None}
    return {"predeclared_public_strata": ["floor_band", "visible_hp_fraction_band", "source_potion_id_label"],
        "minimum_owner_n": 5, "reported": out}


def pareto_report(owners: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    candidates = comparable = a_dominates = b_dominates = incomparable = 0
    missing_fields = Counter()
    required = ("floor_num", "gold", "relics", "deck", "map_node", "keys")
    for owner in owners:
        if owner.get("source_disposition") != "eligible_root":
            continue
        exits_a = owner.get("consume", {}).get("trace", {}).get("battle_exits", [])
        exits_b = owner.get("reserve", {}).get("trace", {}).get("battle_exits", [])
        if len(exits_a) < 2 or len(exits_b) < 2:
            continue
        candidates += 1
        a, b = exits_a[1], exits_b[1]
        sa, sb = a.get("equivalence_state", {}), b.get("equivalence_state", {})
        absent = [k for k in required if k not in sa or k not in sb]
        if absent:
            missing_fields.update(absent)
            continue
        if any(sa[k] != sb[k] for k in required):
            incomparable += 1
            continue
        comparable += 1
        ah, bh = a.get("next_hp"), b.get("next_hp")
        if not isinstance(ah, (int, float)) or not isinstance(bh, (int, float)):
            incomparable += 1
            comparable -= 1
            continue
        ac = Counter(str(x.get("id_label")) for x in a.get("potion_inventory", [])
                     if isinstance(x, Mapping) and str(x.get("id_label", "")).upper() not in {"", "INVALID", "NONE"})
        bc = Counter(str(x.get("id_label")) for x in b.get("potion_inventory", [])
                     if isinstance(x, Mapping) and str(x.get("id_label", "")).upper() not in {"", "INVALID", "NONE"})
        a_ge = ah >= bh and all(ac[k] >= bc[k] for k in set(ac) | set(bc))
        b_ge = bh >= ah and all(bc[k] >= ac[k] for k in set(ac) | set(bc))
        if a_ge and not b_ge:
            a_dominates += 1
        elif b_ge and not a_ge:
            b_dominates += 1
        elif not a_ge and not b_ge:
            incomparable += 1
    return {"b2_state_pair_candidates": candidates, "same_context_pairs_assessed": comparable,
        "consume_resource_dominance": a_dominates, "reserve_resource_dominance": b_dominates,
        "hp_potion_tradeoffs_or_other_incomparables": incomparable,
        "pairs_missing_required_equivalence_fields": candidates - comparable - incomparable,
        "missing_fields": dict(missing_fields),
        "equivalence_fields_required": list(required),
        "interpretation": "No dominance claim without same deck/relic/gold/map/floor/key state and componentwise HP plus potion inventory."}


def finalize(owners: Sequence[Mapping[str, Any]], journal: EventJournal, preflight: bool = False) -> dict[str, Any]:
    source_counts = Counter(str(row.get("source_disposition")) for row in owners)
    stats = paired_statistics(owners)
    eligible = stats["eligible_owner_denominator"]
    any_b2 = stats["eligible_owners_with_any_branch_reaching_b2"]
    both_b2 = stats["eligible_owners_with_both_branches_reaching_b2"]
    preflight_fail = any(row.get("preflight_integrity_pass") is False for row in owners)
    incomplete_sources = sum(source_counts[k] for k in ("source_incomplete_before_b1_exit", "source_error"))
    not_run = sum(value for key, value in source_counts.items() if key.startswith("not_run"))
    incomplete_pairs = stats["incomplete_or_invalid_pairs"]
    unclassified_terminal_branches = sum(
        branch.get("endpoint", {}).get("status") in {"unclassified_terminal_before_b2", "unclassified_b2_outcome"}
        for owner in eligible for branch in (owner.get("consume", {}), owner.get("reserve", {}))
    )
    if preflight_fail:
        disposition = "INTERVENTION_NOT_EXECUTABLE"
    elif len(owners) < len(SEEDS) or not_run:
        disposition = "RESOURCE_BLOCKED_INCOMPLETE"
    elif incomplete_sources:
        disposition = "VALIDITY_FAILURE" if source_counts["source_error"] else "RESOURCE_BLOCKED_INCOMPLETE"
    elif unclassified_terminal_branches:
        disposition = "VALIDITY_FAILURE"
    elif incomplete_pairs:
        disposition = "RESOURCE_BLOCKED_INCOMPLETE"
    elif not eligible or not both_b2:
        disposition = "INSUFFICIENT_NATURAL_EXPOSURE"
    else:
        envelope = stats["primary_owner_bootstrap_95_bound_envelope"]
        if envelope and envelope[0] and envelope[1] and (
            envelope[0][0] > 0 or envelope[1][1] < 0
        ):
            disposition = "TWO_ENCOUNTER_ALLOCATION_SIGNAL_OBSERVED"
        else:
            disposition = "TWO_ENCOUNTER_VALUE_UNRESOLVED"
    report = {
        "issue": 56, "study_disposition": "STUDY_ONLY",
        "terminal_disposition": disposition,
        "preflight_only": preflight, "owners_processed": len(owners),
        "fixed_source_owner_denominator": len(SEEDS),
        "source_dispositions": dict(source_counts),
        "unrun_fixed_seed_owners": not_run,
        "unclassified_terminal_branches_before_b2": unclassified_terminal_branches,
        "eligible_owner_fraction": eligible / len(owners) if owners else 0.0,
        "eligible_owner_denominator": eligible,
        "eligible_owners_reaching_b2_any_branch": any_b2,
        "eligible_owners_reaching_b2_both_branches": both_b2,
        "b2_exposure_percent_of_eligible": 100.0 * both_b2 / eligible if eligible else None,
        "paired_primary": stats,
        "pareto_resource_comparison": pareto_report(owners),
        "descriptive_public_strata": strata_report(owners),
        "builder_runner_process_cpu_s": round(journal.prior_cpu_s + time.process_time() - journal.cpu_started, 3),
        "reviewer_cpu_reserve_s": 1500,
        "limitations": [
            "teacher-assisted privileged native Search-v2/400 study; no normal-public Battle-policy evidence",
            "at most 64 independent source owners; no equivalence or powered full-run Heart claim",
            "post-divergence maps/rewards/draws are authentic and are not synchronized",
            "opaque native checkpoints and expert-driver RNG states are process-memory only",
        ],
    }
    return report





def load_completed(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def verify_preflight_gate(preflight_path: Path, producer_commit: str) -> dict[str, Any]:
    manifest_path, summary_path = preflight_path / "manifest.json", preflight_path / "summary.json"
    owners_path, source_path = preflight_path / "owners.jsonl", preflight_path / "source_attempts.jsonl"
    if not all(path.is_file() for path in (manifest_path, summary_path, owners_path, source_path)):
        raise RuntimeError("formal run requires a complete passing Issue #56 preflight artifact")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    current_code_sha = sha256_file(Path(__file__).resolve())
    if (manifest.get("study_code_sha256") != current_code_sha
            or manifest.get("producer_checkout") != producer_commit
            or manifest.get("simulator", {}).get("module_sha256") != NATIVE_MODULE_SHA256
            or manifest.get("executor_commit") != EXPECTED_STSRL_COMMIT):
        raise RuntimeError("preflight producer/source identities do not match this formal run")
    if summary.get("producer_manifest_sha256") != sha256_file(manifest_path):
        raise RuntimeError("preflight summary does not match its producer manifest")
    if summary.get("owners_jsonl_sha256") != sha256_file(owners_path):
        raise RuntimeError("preflight summary does not match its owner ledger")
    if summary.get("source_attempts_sha256") != sha256_file(source_path):
        raise RuntimeError("preflight summary does not match its source-attempt ledger")
    owners = load_completed(owners_path)
    qualified = qualifying_preflight_seeds(owners)
    declared = [int(seed) for seed in summary.get("preflight_qualified_owner_seeds", [])]
    if (summary.get("preflight_integrity_pass") is not True
            or len(qualified) < 2
            or qualified != declared):
        raise RuntimeError("preflight failed the fixed two-owner paired B1-to-B2 integrity gate")
    return {
        "summary_sha256": sha256_file(summary_path),
        "manifest_sha256": sha256_file(manifest_path),
        "owners_jsonl_sha256": sha256_file(owners_path),
        "source_attempts_sha256": sha256_file(source_path),
        "qualified_owner_seeds": qualified,
        "preflight_path": str(preflight_path),
    }


def write_progress(path: Path, journal: EventJournal, owners: Sequence[Mapping[str, Any]], *, status: str) -> None:
    value = {"status": status, "owners_processed": len(owners),
        "last_owner_seed": owners[-1].get("seed") if owners else None,
        "runner_process_cpu_s": round(journal.prior_cpu_s + time.process_time() - journal.cpu_started, 3),
        "last_stage": journal.last_stage}
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def run(output: Path, *, preflight: bool) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    producer_commit = os.environ.get("SPIRE_WORKTREE_COMMIT", "")
    gate = None if preflight else verify_preflight_gate(PREFLIGHT_PATH, producer_commit)
    owner_file = output / "owners.jsonl"
    source_attempts = output / "source_attempts.jsonl"
    if not preflight and not owner_file.exists():
        if source_attempts.exists():
            raise RuntimeError("formal output has source attempts but no owner ledger; refusing ambiguous preflight reuse")
        shutil.copyfile(PREFLIGHT_PATH / "owners.jsonl", owner_file)
        shutil.copyfile(PREFLIGHT_PATH / "source_attempts.jsonl", source_attempts)
    manifest = source_manifest(output, gate)
    journal = EventJournal(output)
    owners = load_completed(owner_file)
    completed = {int(row["seed"]) for row in owners}
    eligible_seen = sum(row.get("source_disposition") == "eligible_root" for row in owners)
    preflight_seeds = set(qualifying_preflight_seeds(owners))
    status = "PREFLIGHT_IN_PROGRESS" if preflight else "FORMAL_IN_PROGRESS"
    for seed in SEEDS:
        if seed in completed:
            continue
        try:
            journal.check_budget(seed)
            owner = run_owner(seed, journal, output, source_attempts)
        except BudgetStop:
            journal.event(seed, "owner_stopped_at_cpu_ceiling", prior_stage=journal.last_stage)
            status = "RESOURCE_BLOCKED_INCOMPLETE"
            write_progress(output / "progress.json", journal, owners, status=status)
            break
        except Exception as exc:
            journal.event(seed, "owner_failed", error=f"{type(exc).__name__}: {exc}")
            owner = {"seed": seed, "source_disposition": "source_error",
                "source_problems": [f"{type(exc).__name__}: {exc}"]}
        append_jsonl(owner_file, owner)
        owners.append(owner)
        completed.add(seed)
        eligible_seen += owner.get("source_disposition") == "eligible_root"
        if owner.get("preflight_integrity_pass") is False:
            status = "INTERVENTION_NOT_EXECUTABLE"
            write_progress(output / "progress.json", journal, owners, status=status)
            break
        if owner.get("source_disposition") in {"source_error", "source_incomplete_before_b1_exit"}:
            status = "VALIDITY_FAILURE" if owner.get("source_disposition") == "source_error" else "RESOURCE_BLOCKED_INCOMPLETE"
            write_progress(output / "progress.json", journal, owners, status=status)
            break
        preflight_seeds.update(qualifying_preflight_seeds([owner]))
        journal.event(seed, "owner_result_durable",
            disposition=owner.get("source_disposition"),
            eligible_roots=eligible_seen,
            preflight_qualified_owner_seeds=sorted(preflight_seeds))
        write_progress(output / "progress.json", journal, owners, status=status)
        if preflight and len(preflight_seeds) >= 2:
            status = "PREFLIGHT_INTEGRITY_PASS"
            break
    if preflight and len(owners) == len(SEEDS) and len(preflight_seeds) < 2 and status == "PREFLIGHT_IN_PROGRESS":
        status = "INSUFFICIENT_NATURAL_EXPOSURE"
    if not preflight and (len(owners) < len(SEEDS) or status in {"INTERVENTION_NOT_EXECUTABLE", "VALIDITY_FAILURE", "RESOURCE_BLOCKED_INCOMPLETE"}):
        if status == "RESOURCE_BLOCKED_INCOMPLETE":
            disposition = "not_run_budget_stop"
        elif status == "INTERVENTION_NOT_EXECUTABLE":
            disposition = "not_run_after_intervention_gate"
        else:
            disposition = "not_run_after_validity_stop"
        for seed in SEEDS:
            if seed not in completed:
                row = {"seed": seed, "source_disposition": disposition}
                append_jsonl(owner_file, row)
                owners.append(row)
                completed.add(seed)
    report = finalize(owners, journal, preflight=preflight)
    if preflight:
        report["preflight_integrity_pass"] = status == "PREFLIGHT_INTEGRITY_PASS"
        report["preflight_eligible_root_count"] = eligible_seen
        report["preflight_required_paired_b2_owner_count"] = 2
        report["preflight_qualified_owner_seeds"] = sorted(preflight_seeds)
        if status == "PREFLIGHT_INTEGRITY_PASS":
            report["terminal_disposition"] = status
        elif status == "INTERVENTION_NOT_EXECUTABLE":
            report["terminal_disposition"] = "INTERVENTION_NOT_EXECUTABLE"
        elif status == "VALIDITY_FAILURE":
            report["terminal_disposition"] = "VALIDITY_FAILURE"
        elif status == "RESOURCE_BLOCKED_INCOMPLETE":
            report["terminal_disposition"] = status
        elif status == "INSUFFICIENT_NATURAL_EXPOSURE":
            report["terminal_disposition"] = status
    elif status in {"INTERVENTION_NOT_EXECUTABLE", "VALIDITY_FAILURE", "RESOURCE_BLOCKED_INCOMPLETE"}:
        report["terminal_disposition"] = status
    report["producer_manifest_sha256"] = sha256_file(output / "manifest.json")
    report["owners_jsonl_sha256"] = sha256_file(owner_file) if owner_file.exists() else None
    report["source_attempts_sha256"] = sha256_file(source_attempts) if source_attempts.exists() else None
    journal.event(None, "summary_durable", terminal_disposition=report["terminal_disposition"],
        owners_processed=len(owners), eligible_owners=report["eligible_owner_denominator"])
    report["budget_events_sha256"] = sha256_file(journal.path)
    (output / "summary.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_progress(output / "progress.json", journal, owners, status=report["terminal_disposition"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue #56 frozen A20 potion-allocation study")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    default = Path("studies/issue56/preflight") if args.preflight else Path("studies/issue56/results")
    output = args.output or default
    try:
        report = run(output, preflight=args.preflight)
    except BudgetStop:
        return 2
    print(json.dumps({"terminal_disposition": report["terminal_disposition"],
        "owners_processed": report["owners_processed"],
        "eligible_owner_denominator": report["eligible_owner_denominator"],
        "builder_runner_process_cpu_s": report["builder_runner_process_cpu_s"],
        "summary": str(output / "summary.json")}, sort_keys=True))
    if args.preflight and report.get("terminal_disposition") == "INTERVENTION_NOT_EXECUTABLE":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
