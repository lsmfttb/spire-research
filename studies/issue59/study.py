from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import random
import shutil
import sys
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from budget_ledger import ledger_path, sha256_jsonl

DEFAULT_STSRL = ("/home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c"
    if os.name == "posix" else r"D:\DeadlycatCoding\STSRL-issue59")
STSRL = Path(os.environ.get("STSRL_ROOT", DEFAULT_STSRL))
NATIVE_MODULE_DIR = Path(os.environ.get("NATIVE_MODULE_DIR", "/home/lsmft/stsrl-spikes/build-issue59-native-0990-py314"))
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

NATIVE_COMMIT = "0990ef48d51cd48444a707839d65226d52c1d73e"
NATIVE_MODULE_SHA256 = "7ce07ec44abda2133742161d7812fa8286438b71ab83a163e328ac617a44f1e5"
SEARCH_API = "StepSimulator.battle_search_v2.v1"
SEARCH_PATCH = "sts_lightspeed_battle_search_v2_tree_internal_v1"
CONSTRAINED_SEARCH_API = "StepSimulator.battle_search_v2_forbidden_potion_slot.v1"
CONSTRAINED_SEARCH_PATCH = "sts_lightspeed_battle_search_v2_forbidden_potion_slot_v1"
SIMULATIONS = 400
DRIVER_SEED = 559099
BOOTSTRAP_SEED = 559099
SEEDS = tuple(range(559001, 559065))
MAX_STEPS = 20000
CPU_CEILING_SECONDS = 10198.0
POTION_KINDS = frozenset({"potion", "potion_discard"})
PREFLIGHT_PATH = Path(os.environ.get("ISSUE59_PREFLIGHT_PATH", "studies/issue59/preflight"))


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
    source_root = owner.get("source_root", {})
    source_battle = int(source_root.get("source_battle_number", 0) or 0)
    prior_exits = owner.get("source_battle_exits", [])
    later_root = source_battle > 1 and len(prior_exits) == source_battle - 1
    prior_battles_had_no_use = all(
        row.get("qualifying_source_use_not_yet_selected") is True and not row.get("terminal")
        for row in prior_exits
    )
    reserve_trace = reserve.get("trace", {})
    root_search = reserve_trace.get("constrained_root_witness", {})
    constrained_b1 = reserve_trace.get("b1_constrained_decisions", 0) >= 1
    constrained_identity = (root_search.get("native_api") == CONSTRAINED_SEARCH_API
        and root_search.get("patch_identity") == CONSTRAINED_SEARCH_PATCH)
    source_potion_slot = source_root.get("potion", {}).get("potion_index")
    protected_slot_matches_source = root_search.get("forbidden_potion_slot") == source_potion_slot
    no_actual_use_or_discard = reserve_trace.get("b1_exact_source_potion_uses", 0) == 0 and reserve_trace.get("b1_exact_source_potion_discards", 0) == 0
    root_has_allowed_visit = root_search.get("visited_allowed_action_count", 0) >= 1
    protected_rows_excluded = bool(root_search.get("protected_rows")) and all(
        row.get("search_exclusion_reason") == "forbidden_potion_slot"
        and row.get("search_tree_present") is False
        and row.get("visits") == 0
        and row.get("evaluation_sum") is None
        and row.get("mean_value") is None
        for row in root_search.get("protected_rows", [])
    )
    b2_unmasked = (reserve_trace.get("b2_unmasked_decisions", 0) == reserve_trace.get("b2_decisions", -1))
    return {
        "source_root_is_first_qualifying_use_in_later_battle": later_root and prior_battles_had_no_use,
        "both_arms_restore_exact_root": bool(consume.get("root_restore_equal") and reserve.get("root_restore_equal")),
        "reserve_uses_constrained_native_search_during_b1": constrained_b1,
        "constrained_native_api_and_patch_match": constrained_identity,
        "constrained_slot_matches_source_potion_identity": protected_slot_matches_source,
        "protected_legal_root_occurrences_are_unsearched": protected_rows_excluded,
        "reserve_selects_a_visited_allowed_root_action": root_has_allowed_visit,
        "reserve_never_uses_or_discards_source_potion_in_b1": no_actual_use_or_discard,
        "reserve_search_constraint_ends_at_b1_exit": bool(reserve_trace.get("b1_constraint_closed")) if reserve_trace.get("battle_exits") else True,
        "b2_is_unmasked_if_reached": b2_unmasked,
        "qualifies": bool(owner.get("preflight_integrity_pass") and later_root and prior_battles_had_no_use
            and consume.get("root_restore_equal") and reserve.get("root_restore_equal") and constrained_b1 and constrained_identity
            and protected_slot_matches_source
            and protected_rows_excluded and root_has_allowed_visit and no_actual_use_or_discard and b2_unmasked),
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


class StopAtB2Exit(BaseException):
    pass


class BudgetStop(BaseException):
    pass


class InterventionNotExecutable(RuntimeError):
    pass


class EventJournal:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.path = ledger_path()
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
        append_jsonl(self.path, {"run_directory": str(self.root), "owner_seed": owner, "stage": stage,
            "elapsed_process_cpu_s": round(self.prior_cpu_s + time.process_time() - self.cpu_started, 3), **fields})

    def check_budget(self, owner: int | None = None) -> None:
        elapsed = self.prior_cpu_s + time.process_time() - self.cpu_started
        if elapsed >= CPU_CEILING_SECONDS:
            self.event(owner, "builder_cpu_ceiling", cpu_s=round(elapsed, 3))
            raise BudgetStop
        if elapsed - self.last_cpu_checkpoint_s >= 5.0:
            append_jsonl(self.path, {"owner_seed": owner, "stage": "cpu_checkpoint",
                "run_directory": str(self.root), "boundary": self.last_stage,
                "elapsed_process_cpu_s": round(elapsed, 3)})
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
    source_battle_number: int


def verify_protected_root_occurrences(
    raw_search: Mapping[str, Any], actions: Sequence[Any], slot: int, root: Root,
    *, initial_restored_root: bool = True,
) -> dict[str, Any]:
    """Prove current reserved-slot occurrences were omitted by Native Search."""
    if raw_search.get("native_api") != CONSTRAINED_SEARCH_API:
        raise RuntimeError("B1 did not use the forbidden-potion-slot Native API")
    if raw_search.get("patch_identity") != CONSTRAINED_SEARCH_PATCH:
        raise RuntimeError("B1 forbidden-potion-slot patch identity mismatch")
    if raw_search.get("forbidden_potion_slot") != slot:
        raise RuntimeError("B1 Native report names a different protected potion slot")
    if raw_search.get("search_constraint") != "potion_source_slot_omitted_from_tree_and_random_rollouts":
        raise RuntimeError("B1 Native report does not identify tree-and-rollout slot exclusion")

    rows = raw_search.get("root_rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise RuntimeError("B1 Native report omitted root rows")
    row_by_key: dict[tuple[str, int], Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise RuntimeError("B1 Native root row is malformed")
        key = (str(row.get("scope", "")), int(row.get("bits", -1)))
        if key in row_by_key:
            raise RuntimeError("B1 Native report duplicated a root action occurrence")
        row_by_key[key] = row

    protected: list[tuple[int, Any, Mapping[str, Any]]] = []
    for index, action in enumerate(actions):
        if action.kind not in POTION_KINDS or int(action.raw.get("idx1", -1)) != slot:
            continue
        key = (str(action.raw.get("scope", "")), int(action.raw.get("bits", -1)))
        row = row_by_key.get(key)
        if row is None:
            raise RuntimeError("B1 Native report omitted a legal protected-slot occurrence")
        if (row.get("search_exclusion_reason") != "forbidden_potion_slot"
                or row.get("search_tree_present") is not False
                or row.get("visits") != 0
                or row.get("evaluation_sum") is not None
                or row.get("mean_value") is not None):
            raise RuntimeError("B1 protected legal occurrence was not retained as zero-visit/null-mean")
        protected.append((index, action, row))

    source_index = None
    if initial_restored_root:
        from sts_combat_rl.sim.decision_record import find_action_index_by_identity
        source_index = find_action_index_by_identity(actions, root.selected_identity)
        if source_index not in {index for index, _, _ in protected}:
            raise RuntimeError("source-selected legal potion occurrence is not protected at the restored root")
    return {
        "protected_rows": [
            {"scope": row.get("scope"), "bits": row.get("bits"), "kind": row.get("kind"),
             "idx1": row.get("idx1"), "search_exclusion_reason": row.get("search_exclusion_reason"),
             "search_tree_present": row.get("search_tree_present"), "visits": row.get("visits"),
             "evaluation_sum": row.get("evaluation_sum"), "mean_value": row.get("mean_value")}
            for _, _, row in protected
        ],
        "source_selected_legal_index": source_index,
        "source_selected_identity_verified": initial_restored_root,
        "protected_legal_occurrence_count": len(protected),
        "tree_forbidden_potion_slot_exclusion_count": raw_search.get("tree_forbidden_potion_slot_exclusion_count"),
        "rollout_forbidden_potion_slot_exclusion_count": raw_search.get("rollout_forbidden_potion_slot_exclusion_count"),
    }


def call_constrained_b1_search(adapter: Any, snapshot: Any, slot: int) -> dict[str, Any]:
    """Study-local forwarding adapter for the pinned Native extension API."""
    if not callable(getattr(adapter, "_assert_snapshot_is_current", None)):
        raise RuntimeError("pinned adapter cannot verify the current snapshot")
    adapter._assert_snapshot_is_current(snapshot)
    native = getattr(adapter, "_sim", None)
    search = getattr(native, "battle_search_v2", None)
    if not callable(search):
        raise RuntimeError("pinned Native StepSimulator lacks battle_search_v2")
    return dict(search(SIMULATIONS, True, None, None, forbidden_potion_slot=int(slot)))


class StudySearch:
    def __init__(self, mode: str, root_state: dict[str, Any], root: Root | None = None) -> None:
        self.mode, self.root_state, self.root = mode, root_state, root
        self.provenance = ControllerProvenance(kind="study_native_search_v2",
            name=f"issue59_search_v2_400_{mode}", config={
                "simulations": SIMULATIONS, "include_potions": True,
                "selection": "highest_mean",
                "unconstrained_native_api": SEARCH_API,
                "unconstrained_native_patch": SEARCH_PATCH,
                "b1_reserve_native_api": CONSTRAINED_SEARCH_API if mode == "reserve" else None,
                "b1_reserve_native_patch": CONSTRAINED_SEARCH_PATCH if mode == "reserve" else None,
                "information_regime": "full_simulator_state_oracle_like",
                "b1_reservation": "fixed_original_occupied_slot_in_native_tree_and_rollouts" if mode == "reserve" else None})

    def select_action(self, adapter: Any, snapshot: Any, actions: Sequence[Any], context: DecisionContext, step_index: int) -> ControllerDecision:
        in_battle = is_battle_state(snapshot.raw, context.screen_state)
        b1_open = self.root is not None and in_battle and not self.root_state.get("b1_closed")
        if self.root is not None and b1_open:
            keys = getattr(adapter, "checkpoint_fingerprint_transition_only_raw_keys", ())
            self.root_state.setdefault("b1_raw_state_fingerprints", []).append(
                raw_state_fingerprint(snapshot.raw, keys))
        if self.mode == "force" and step_index == 0:
            from sts_combat_rl.sim.decision_record import find_action_index_by_identity
            return ControllerDecision(find_action_index_by_identity(actions, self.root.selected_identity),
                self.provenance, reason="force_exact_source_action")

        constrained = self.mode == "reserve" and self.root is not None and b1_open
        if self.root is not None and in_battle:
            if b1_open:
                self.root_state["b1_seen"] = True
            elif self.root_state.get("b1_closed"):
                self.root_state["b2_unmasked_decisions"] = self.root_state.get("b2_unmasked_decisions", 0) + 1

        start = time.process_time()
        slot = item_key(self.root.potion)[0] if constrained and self.root is not None else None
        if constrained:
            exact_present = [row for row in get_potions(snapshot.raw) if item_key(row) == item_key(self.root.potion)]
            if len(exact_present) != 1:
                raise InterventionNotExecutable("the source potion identity is not still present in its reserved slot")
            raw = call_constrained_b1_search(adapter, snapshot, int(slot))
            expected_api, expected_patch = CONSTRAINED_SEARCH_API, CONSTRAINED_SEARCH_PATCH
            self.root_state["b1_constrained_decisions"] = self.root_state.get("b1_constrained_decisions", 0) + 1
        else:
            raw = adapter.battle_search_v2(snapshot, simulations=SIMULATIONS,
                include_potions=True, policy_prior_callback=None, leaf_value_callback=None)
            expected_api, expected_patch = SEARCH_API, SEARCH_PATCH
            if self.root is not None and in_battle and not self.root_state.get("b1_closed"):
                self.root_state["b1_unconstrained_decisions"] = self.root_state.get("b1_unconstrained_decisions", 0) + 1

        journal = self.root_state.get("journal")
        if journal is not None:
            journal.check_budget(self.root_state.get("owner_seed"))
        report = build_oracle_search_report(raw, actions, context,
            expected_native_api=expected_api, expected_patch_identity=expected_patch,
            wall_clock_time_s=time.process_time() - start)
        if not report.search_ok:
            raise RuntimeError("native Search-v2 report invalid: " + "; ".join(report.problems))
        constrained_evidence = None
        if constrained and self.root is not None:
            constrained_evidence = verify_protected_root_occurrences(
                raw, actions, int(slot), self.root,
                initial_restored_root=self.root_state.get("b1_constrained_decisions") == 1)
            if (not constrained_evidence["protected_rows"]
                    and self.root_state.get("b1_constrained_decisions") == 1):
                raise InterventionNotExecutable("the initial constrained root has no legal protected occurrence")
        try:
            target = select_oracle_root_action(report, selection_rule="highest_mean")
        except ValueError as exc:
            if constrained:
                raise InterventionNotExecutable(f"constrained B1 root has no visited allowed action: {exc}") from exc
            raise
        action = actions[target.legal_action_index]
        if constrained and action.kind in POTION_KINDS and int(action.raw.get("idx1", -1)) == int(slot):
            raise RuntimeError("constrained Native Search selected a protected potion use/discard occurrence")
        selected_identity = action_identity_dicts_for_actions(actions)[target.legal_action_index]
        search_call = {
            "phase": "b1" if b1_open else ("b2" if self.root is not None and self.root_state.get("b1_closed") and in_battle else "source_or_unrestricted_battle"),
            "native_api": report.native_api, "patch_identity": report.patch_identity,
            "constrained_slot": slot, "simulations_requested": report.simulations_requested,
            "root_visits": report.root_visits, "selected_identity": selected_identity,
            "selected_visits": target.visits,
            "selected_action_uses_or_discards_protected_slot": bool(constrained and action.kind in POTION_KINDS and int(action.raw.get("idx1", -1)) == int(slot)),
        }
        if constrained_evidence is not None:
            search_call["protected_root_evidence"] = constrained_evidence
            self.root_state.setdefault("constrained_search_calls", []).append(search_call)
            if not self.root_state.get("search_calls"):
                self.root_state["constrained_root_witness"] = {
                    **constrained_evidence,
                    "native_api": report.native_api,
                    "patch_identity": report.patch_identity,
                    "forbidden_potion_slot": raw.get("forbidden_potion_slot"),
                    "search_constraint": raw.get("search_constraint"),
                    "visited_allowed_action_count": sum(
                        stat.eligible and stat.visits > 0
                        and stat.legal_action_index not in {
                            index for index, candidate in enumerate(actions)
                            if candidate.kind in POTION_KINDS and int(candidate.raw.get("idx1", -1)) == int(slot)
                        }
                        for stat in report.root_actions
                    ),
                    "selected_identity": selected_identity,
                    "selected_visits": target.visits,
                }
        self.root_state.setdefault("search_calls", []).append(search_call)
        if self.mode == "source" and self.root_state.get("source_battle_number") is not None and action.kind == "potion" and is_smoke(potion_at_action_slot(snapshot.raw, action)):
            self.root_state["smoke_uses_total"] += 1
        if self.mode == "source" and self.root_state.get("source_battle_number") is not None and is_qualifying_source_use(action, snapshot.raw):
            identities = action_identity_dicts_for_actions(actions)
            self.root = Root(checkpoint=adapter.capture_checkpoint(snapshot),
                fingerprint=adapter.checkpoint_fingerprint(snapshot), raw=dict(snapshot.raw),
                actions=list(actions), action_identities=identities,
                selected_identity=identities[target.legal_action_index],
                potion=potion_at_action_slot(snapshot.raw, action),
                rng_state=self.root_state["driver"]._rng.getstate(),
                source_battle_number=int(self.root_state["source_battle_number"]))
            self.root_state["search_root_report"] = {"native_api": report.native_api,
                "patch_identity": report.patch_identity, "include_potions": report.include_potions,
                "simulations_requested": report.simulations_requested, "root_visits": report.root_visits,
                "selected_identity": selected_identity, "selected_visits": target.visits}
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


def run_source(seed: int, journal: EventJournal, output: Path) -> tuple[dict[str, Any], Root | None, LightSpeedAdapter]:
    journal.check_budget(seed)
    adapter = LightSpeedAdapter(seed=seed, ascension=20, player_class="IRONCLAD", module=slaythespire)
    driver = ExpertNonCombatDriver(DRIVER_SEED)
    state: dict[str, Any] = {"source_battle_number": None, "driver": driver, "smoke_uses_total": 0,
        "journal": journal, "owner_seed": seed, "search_calls": []}
    battle = StudySearch("source", state)
    controller = RoutedRunController(battle=battle, non_combat=PolicyController(driver))
    source_battle_exits: list[dict[str, Any]] = []
    source_completion: dict[str, Any] = {}

    def before(snapshot: Any, actions: Sequence[Any], context: DecisionContext, step_index: int) -> None:
        del actions, step_index
        journal.check_budget(seed)
        if is_battle_state(snapshot.raw, context.screen_state):
            state["source_battle_number"] = len(source_battle_exits) + 1
        else:
            state["source_battle_number"] = None

    def after(step: ControlledRunStep) -> None:
        if step.battle_active and not step.next_battle_active:
            battle_number = len(source_battle_exits) + 1
            source_battle_exits.append({"battle_number": battle_number,
                "battle_outcome": step.next_battle_outcome or step.battle_outcome or "UNKNOWN",
                "terminal": step.terminal_after_step, "outcome": step.next_snapshot_raw.get("outcome", "UNKNOWN"),
                "floor": step.next_floor, "hp": step.next_player_hp, "max_hp": step.next_player_max_hp,
                "gold": step.next_gold, "potion_count": step.next_potion_count,
                "smoke_uses_total": state["smoke_uses_total"],
                "qualifying_source_use_not_yet_selected": True,
                "potion_inventory": step.next_snapshot_raw.get("battle_potions", step.next_snapshot_raw.get("potions"))})
            journal.event(seed, "source_battle_exit", battle_number=battle_number,
                battle_outcome=source_battle_exits[-1]["battle_outcome"],
                terminal=step.terminal_after_step, floor=step.next_floor)

    journal.event(seed, "source_started")
    try:
        result = execute_controlled_run(adapter, controller, seed=seed, max_steps=MAX_STEPS,
            action_space=ActionSpaceConfig.include_all(), before_decision=before,
            after_transition=after, retain_steps=False)
        source_completion.update({"terminal": result.terminal, "outcome": result.outcome,
            "problems": result.problems, "final_screen": result.final_raw.get("screen_state"),
            "final_floor": result.final_raw.get("floor_num"), "final_hp": result.final_raw.get("player_hp"),
            "final_max_hp": result.final_raw.get("player_max_hp")})
    except StopAtRoot:
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
    elif source_completion.get("problems"):
        disposition = "source_error"
    elif source_completion.get("terminal"):
        disposition = "source_terminal_no_qualifying_use"
    else:
        disposition = "source_truncated_before_qualifying_use"
    record = {"seed": seed, "source_disposition": disposition,
        "source_root_battle_number": None if root is None else root.source_battle_number,
        "source_battles_before_root": len(source_battle_exits) if root is not None else None,
        "source_battle_exits": source_battle_exits,
        "source_smoke_uses_total": state["smoke_uses_total"],
        "source_terminal": source_completion.get("terminal", False),
        "source_outcome": source_completion.get("outcome", "UNKNOWN"),
        "source_problems": source_completion.get("problems", []),
        "source_root": None if root is None else {"visible_snapshot": {k: root.raw.get(k) for k in ("screen_state", "floor_num", "player_hp", "player_max_hp", "gold", "battle_potions")},
            "potion": root.potion, "chosen_action": root.selected_identity,
            "source_battle_number": root.source_battle_number,
            "search": state.get("search_root_report")},
        "source_completion": source_completion or None}
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
        self.b1_constrained_decisions = 0
        self.b1_unconstrained_decisions = 0
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
        protected_slot_action = kind in POTION_KINDS and slot == item_key(self.root.potion)[0]
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
        if self.mode == "reserve" and battle_number == 1 and protected_slot_action:
            self.b1_mask_violations += 1
        super().transition(step)
        self.steps[-1]["selected_tactical_action"] = canonical(selected)





def run_branch(adapter: LightSpeedAdapter, root: Root, seed: int, mode: str, journal: EventJournal) -> tuple[RunTrace, Mapping[str, Any], dict[str, Any]]:
    driver = ExpertNonCombatDriver(DRIVER_SEED)
    driver._rng.setstate(root.rng_state)
    root_state: dict[str, Any] = {"b1_seen": False, "b1_closed": False,
        "b1_constrained_decisions": 0, "b1_unconstrained_decisions": 0,
        "b2_unmasked_decisions": 0, "search_calls": [], "constrained_search_calls": [],
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
        if step.battle_active and not step.next_battle_active:
            root_state["b1_closed"] = True
        if trace.b2_resolved:
            root_state["b2_resolved"] = True
            raise StopAtB2Exit

    journal.event(seed, f"{mode}_branch_started")
    try:
        result = execute_controlled_run(resume, controller, seed=seed, max_steps=MAX_STEPS,
            action_space=ActionSpaceConfig.include_all(), before_decision=before,
            after_transition=after, retain_steps=True)
    except StopAtB2Exit:
        result = None
    if result is not None:
        if result.problems:
            raise RuntimeError("controlled continuation problems: " + "; ".join(result.problems))
        trace.terminal, trace.outcome = result.terminal, result.outcome
    if not resume.fingerprint_ok:
        raise RuntimeError("executor did not restore the exact root")
    root_state["root_restore_equal"] = resume.fingerprint_ok
    if mode == "reserve":
        trace.b1_constrained_decisions = root_state["b1_constrained_decisions"]
        trace.b1_unconstrained_decisions = root_state["b1_unconstrained_decisions"]
        if trace.b1_constrained_decisions != trace.b1_decisions:
            raise RuntimeError("Native slot constraint was not used at every reserve B1 decision")
        if trace.b1_mask_violations:
            raise RuntimeError("B used or discarded the protected source potion slot during B1")
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
    # Raw source bytes from the exact detached 7d172f7c24c9d29429b6946ba44f6b59760b0d96 checkout.
    "src/sts_combat_rl/sim/controlled_run.py": "199114124b67e20e013aad6eba487a7bef564abd526610327f44ad2d620507c8",
    "src/sts_combat_rl/sim/lightspeed.py": "d2ba18eced21589fdfb853a3f2389828997330f84b9c34fc996d09a9d54e426b",
    "src/sts_combat_rl/sim/action_space.py": "555846f31c5fd332e914f55e55f64c1d7a492d8cfbdb2c143f95c52180c05532",
    "src/sts_combat_rl/sim/non_combat_policy.py": "5e494fb8924c0e459229195174224441875050c4f6423b19f15f0fdc3e22ecb3",
    "src/sts_combat_rl/sim/decision_record.py": "bce0b268d1eefc958ca2c3c793377a62058b4f91103e17dcb0c51e8a986325b0",
    "src/sts_combat_rl/sim/oracle_search.py": "3c213220e30d600b884fb7b9eb5b0613e34ada3bf9e7c056b7c72e6639015db4",
    "src/sts_combat_rl/sim/controller_contract.py": "6fbeb3600b6bee7686f65d3cde0b2716a2b17dfb2b34e0a9251a088e0e5aa042",
    "src/sts_combat_rl/sim/online_controller.py": "5bbc2499d757a5a5b405d5c9f489bf6b712260c1d44a4615e9d22c6b6981198a",
    "src/sts_combat_rl/sim/policy_contract.py": "1c369997868f3712cf59dbb667d7bbd9b3b350cd78d325aa45040a47680d3c2a",
    "src/sts_combat_rl/sim/features.py": "1e81ddae643fa277f099e9db93c818ef49dba3114fa58a263e93ba40b473c08a",
    "src/sts_combat_rl/sim/public_run_context.py": "99fabecb032a4d9dc6170cb65d09e9b3385f2edc8eccc4a01b1672e4bcc42759",
    "src/sts_combat_rl/sim/native_public_projection.py": "5c36807b5e52bfd9b364a56c2a6a5f304bc3170bd7af04a77a790c7044b7e386",
}


_TEXT_HASH_SUFFIXES = {".py", ".json", ".jsonl"}


def sha256_file(path: Path) -> str:
    """Hash text artifacts with Git-style LF bytes and binaries byte-for-byte."""
    content = path.read_bytes()
    if path.suffix.lower() in _TEXT_HASH_SUFFIXES:
        content = content.replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()


def is_ledger_prefix_sha256(path: Path, expected: str) -> bool:
    if not path.is_file() or not expected:
        return False
    digest = hashlib.sha256()
    if digest.hexdigest() == expected:
        return True
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line.replace(b"\r\n", b"\n"))
            if digest.hexdigest() == expected:
                return True
    return False


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def source_manifest(output: Path, preflight_gate: Mapping[str, Any] | None = None) -> dict[str, Any]:
    module_path = Path(slaythespire.__file__).resolve()
    if len(NATIVE_MODULE_SHA256) != 64 or not module_path.exists() or module_path.parent != NATIVE_MODULE_DIR.resolve() or sha256_file(module_path) != NATIVE_MODULE_SHA256:
        raise RuntimeError("loaded native extension path/hash is not the frozen build")
    default_native = "/home/lsmft/stsrl-spikes/sts_lightspeed-issue59" if os.name == "posix" else r"D:\DeadlycatCoding\sts_lightspeed-issue59"
    native_repo = Path(os.environ.get("ISSUE59_NATIVE_ROOT", default_native))
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
    study_root = Path(__file__).resolve().parents[2]
    if git(study_root, "rev-parse", "HEAD") != producer_commit:
        raise RuntimeError("SPIRE_WORKTREE_COMMIT does not match the current pushed producer checkout")
    native_build_cpu_s = float(os.environ.get("ISSUE59_NATIVE_BUILD_CPU_S", "nan"))
    native_configure_cpu_s = float(os.environ.get("ISSUE59_NATIVE_CONFIGURE_CPU_S", "nan"))
    if (not math.isfinite(native_build_cpu_s) or native_build_cpu_s < 0
            or not math.isfinite(native_configure_cpu_s) or native_configure_cpu_s < 0):
        raise RuntimeError("native configure/build CPU values must record the measured pinned native setup")
    manifest = {
        "issue": 59,
        "study_disposition": "STUDY_ONLY",
        "game": {"player_class": "IRONCLAD", "start": "standard_start", "ascension": 20},
        "source_seed_order": list(SEEDS),
        "source_seed_count": len(SEEDS),
        "source_selection": "first selected legal non-Smoke potion use anywhere in the natural source run before authentic terminal or max-step truncation; one root maximum per owner",
        "simulator": {"repository": "lsmfttb/sts_lightspeed", "commit": NATIVE_COMMIT,
            "module_path": str(module_path), "module_sha256": NATIVE_MODULE_SHA256,
            "build": {"type": "Release", "cmake": "4.2.3", "compiler": "GNU g++ 15.2.0",
                "python": "3.14.4", "configure_process_cpu_s": native_configure_cpu_s,
                "build_process_cpu_s": native_build_cpu_s}},
        "executor_repository": "lsmfttb/STSRL",
        "executor_commit": EXPECTED_STSRL_COMMIT,
        "executor_source_sha256": st_hashes,
        "search": {"unconstrained_api": SEARCH_API, "unconstrained_patch_identity": SEARCH_PATCH,
            "b1_reserve_api": CONSTRAINED_SEARCH_API, "b1_reserve_patch_identity": CONSTRAINED_SEARCH_PATCH,
            "simulations": SIMULATIONS, "include_potions": True,
            "selection": "highest_mean", "prior": None, "leaf_value": None},
        "non_combat_driver": {"name": "expert_non_combat_v1", "seed": DRIVER_SEED,
            "source": "same deterministic seeded stream; branch copies exact in-memory RNG state at root"},
        "bootstrap": {"unit": "original source owner", "seed": BOOTSTRAP_SEED,
            "replicates": 10000, "interval": "percentile 95%"},
        "budget": {"issue_hard_cpu_s": 12000, "builder_all_in_cpu_ceiling_s": 10200,
            "runner_process_cpu_ceiling_s": CPU_CEILING_SECONDS,
            "native_configure_process_cpu_s": native_configure_cpu_s,
            "native_build_process_cpu_s": native_build_cpu_s,
            "reviewer_reserved_cpu_s": 1800},
        "limits": {"max_steps_per_cursor": MAX_STEPS, "no outcome-adaptive seed replacement": True,
            "native checkpoint": "opaque process-memory only; never serialized"},
        "budget_event_ledger": str(ledger_path()),
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
            "b1_constraint_closed": bool(root_state.get("b1_closed")),
            "b1_constrained_decisions": root_state.get("b1_constrained_decisions", 0),
            "b1_unconstrained_decisions": root_state.get("b1_unconstrained_decisions", 0),
            "b1_forbidden_slot_violations": trace.b1_mask_violations,
            "constrained_root_witness": root_state.get("constrained_root_witness"),
            "constrained_search_calls": root_state.get("constrained_search_calls", []),
            "native_search_call_ledger": root_state.get("search_calls", []),
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


def pair_source_owner(source: Mapping[str, Any], root: Root, adapter: LightSpeedAdapter,
                      seed: int, journal: EventJournal) -> dict[str, Any]:
    owner: dict[str, Any] = dict(source)
    try:
        for mode, key in (("consume", "consume"), ("reserve", "reserve")):
            journal.check_budget(seed)
            journal.event(seed, f"{mode}_branch_started")
            try:
                trace, provenance, root_state = run_branch(adapter, root, seed, mode, journal)
                owner[key] = branch_record(trace, provenance, root_state, mode)
                journal.event(seed, f"{mode}_branch_completed",
                    endpoint=owner[key]["endpoint"],
                    root_restore_equal=owner[key]["root_restore_equal"],
                    decisions=owner[key]["trace"]["decisions"])
            except BudgetStop:
                raise
            except InterventionNotExecutable as exc:
                owner[key] = {"endpoint": {"primary": None, "status": "intervention_not_executable"},
                    "error": f"{type(exc).__name__}: {exc}"}
                owner["intervention_not_executable"] = True
                journal.event(seed, f"{mode}_branch_not_executable", error=str(exc))
            except Exception as exc:
                owner[key] = {"endpoint": {"primary": None, "status": "branch_validity_failure"},
                    "error": f"{type(exc).__name__}: {exc}"}
                owner["branch_validity_failure"] = True
                journal.event(seed, f"{mode}_branch_validity_failure", error=f"{type(exc).__name__}: {exc}")
        a, b = owner.get("consume"), owner.get("reserve")
        source_action = root.selected_identity
        b_trace = b.get("trace", {}) if b else {}
        b1_exits = b_trace.get("battle_exits", [])
        b1_inventory_at_resolution = (
            b1_exits[0].get("potion_inventory") if b1_exits else
            ((b_trace.get("public_transition_ledger") or [{}])[-1].get("next_potion_inventory")
             if b_trace.get("terminal_outcome") else None)
        )
        root_witness = b_trace.get("constrained_root_witness", {})
        checks = {
            "consume_forced_exact_source_action": bool(a and a.get("trace", {}).get("public_transition_ledger")
                and a["trace"]["public_transition_ledger"][0]["action"] == source_action),
            "consume_used_exact_source_potion_at_root": bool(a and a.get("trace", {}).get("b1_exact_source_potion_uses", 0) >= 1),
            "reserve_first_action_differs": bool(b and b.get("trace", {}).get("public_transition_ledger")
                and b["trace"]["public_transition_ledger"][0]["action"] != source_action),
            "reserve_uses_constrained_native_api_at_restored_root": bool(
                b and root_witness.get("protected_rows")
                and root_witness.get("protected_legal_occurrence_count", 0) >= 1),
            "reserve_native_api_and_patch_identity_match": bool(
                root_witness.get("native_api") == CONSTRAINED_SEARCH_API
                and root_witness.get("patch_identity") == CONSTRAINED_SEARCH_PATCH
                and root_witness.get("forbidden_potion_slot") == item_key(root.potion)[0]),
            "reserve_has_visited_allowed_root_action": bool(root_witness.get("visited_allowed_action_count", 0) >= 1),
            "reserve_never_used_or_discarded_protected_slot_in_b1": bool(
                b and b_trace.get("b1_forbidden_slot_violations", -1) == 0),
            "reserve_used_native_constraint_every_b1_decision": bool(
                b and b_trace.get("b1_constrained_decisions", 0) == b_trace.get("b1_decisions", -1)
                and b_trace.get("b1_constrained_decisions", 0) >= 1),
            "reserve_constraint_ended_at_b1_resolution": bool(
                b and (b_trace.get("b1_constraint_closed") or b_trace.get("terminal_outcome"))),
            "both_branches_restored_exact_root": bool(a and b and a.get("root_restore_equal") and b.get("root_restore_equal")),
            "reserve_potion_present_at_b1_resolution": bool(
                b and b1_inventory_at_resolution is not None and any(
                    isinstance(x, Mapping) and item_key(x) == item_key(root.potion)
                    for x in (b1_inventory_at_resolution or []))),
            "reserve_b2_unmasked_if_reached": bool(
                b and b_trace.get("b2_unmasked_decisions", 0) == b_trace.get("b2_decisions", -1)),
        }
        owner["preflight_integrity"] = checks
        owner["preflight_integrity_pass"] = all(checks.values())
        owner["preflight_qualification"] = preflight_qualification(owner)
    finally:
        adapter.close()
    journal.event(seed, "owner_completed", disposition="eligible_root",
        integrity_pass=owner.get("preflight_integrity_pass"))
    return owner


def run_owner(seed: int, journal: EventJournal, output: Path, source_attempts: Path) -> dict[str, Any]:
    journal.event(seed, "owner_started")
    source, root, adapter = run_source(seed, journal, source_attempts)
    if root is None:
        journal.event(seed, "owner_completed", disposition=source["source_disposition"])
        return source
    return pair_source_owner(source, root, adapter, seed, journal)





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
    required = ("act", "floor_num", "gold", "relics", "battle_relics", "deck",
        "deck_summary", "map_node", "next_map_node", "reward", "rewards", "keys")
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


def finalize(owners: Sequence[Mapping[str, Any]], journal: EventJournal) -> dict[str, Any]:
    source_counts = Counter(str(row.get("source_disposition")) for row in owners)
    stats = paired_statistics(owners)
    eligible_owners = [row for row in owners if row.get("source_disposition") == "eligible_root"]
    eligible = len(eligible_owners)
    any_b2 = stats["eligible_owners_with_any_branch_reaching_b2"]
    both_b2 = stats["eligible_owners_with_both_branches_reaching_b2"]
    intervention_failure_seeds = [int(row["seed"]) for row in owners
        if row.get("intervention_not_executable") and "seed" in row]
    validity_failure_seeds = [int(row["seed"]) for row in owners
        if row.get("source_disposition") == "source_error" or row.get("branch_validity_failure")]
    incomplete_sources = source_counts["source_truncated_before_qualifying_use"]
    not_run = sum(value for key, value in source_counts.items() if key.startswith("not_run"))
    incomplete_pairs = stats["incomplete_or_invalid_pairs"]
    unclassified_terminal_branches = sum(
        branch.get("endpoint", {}).get("status") in {"unclassified_terminal_before_b2", "unclassified_b2_outcome"}
        for owner in eligible_owners for branch in (owner.get("consume", {}), owner.get("reserve", {}))
    )
    if validity_failure_seeds or unclassified_terminal_branches:
        disposition = "VALIDITY_FAILURE"
    elif intervention_failure_seeds:
        disposition = "INTERVENTION_NOT_EXECUTABLE"
    elif len(owners) < len(SEEDS) or not_run or incomplete_sources or incomplete_pairs:
        disposition = "RESOURCE_BLOCKED"
    elif not eligible or not stats["complete_paired_owners"]:
        disposition = "INSUFFICIENT_NATURAL_EXPOSURE"
    else:
        envelope = stats["primary_owner_bootstrap_95_bound_envelope"]
        if envelope and envelope[0] and envelope[1] and (
            envelope[0][0] > 0 or envelope[1][1] < 0
        ):
            disposition = "TWO_BATTLE_MECHANISTIC_SIGNAL"
        else:
            disposition = "TWO_BATTLE_VALUE_UNRESOLVED"
    report = {
        "issue": 59, "study_disposition": "STUDY_ONLY",
        "terminal_disposition": disposition,
        "owners_processed": len(owners),
        "fixed_source_owner_denominator": len(SEEDS),
        "source_dispositions": dict(source_counts),
        "intervention_failure_owner_seeds": intervention_failure_seeds,
        "validity_failure_owner_seeds": validity_failure_seeds,
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
        "builder_all_in_process_cpu_s": round(journal.prior_cpu_s + time.process_time() - journal.cpu_started, 3),
        "issue_hard_process_cpu_s": 12000,
        "reviewer_reserved_process_cpu_s": 1800,
        "limitations": [
            "teacher-assisted privileged native Search-v2/400 study; no normal-public Battle-policy evidence",
            "at most 64 independent source owners; no equivalence or powered full-run Heart claim",
            "post-divergence maps/rewards/draws are authentic and are not synchronized",
            "opaque native checkpoints and expert-driver RNG states are process-memory only",
            "a missing B2 without an authentic terminal status is incomplete, never coded as a tie or death",
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


def recorded_source_starts(journal_path: Path, output: Path) -> set[int]:
    """Find source runs that crossed the durable start boundary for this output."""
    if not journal_path.exists():
        return set()
    run_directory = str(Path(output).resolve())
    started: set[int] = set()
    with journal_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
                if (row.get("run_directory") == run_directory
                        and row.get("stage") == "source_started"
                        and row.get("owner_seed") is not None):
                    started.add(int(row["owner_seed"]))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
    return started


def verify_preflight_gate(preflight_path: Path, producer_commit: str) -> dict[str, Any]:
    manifest_path, summary_path = preflight_path / "manifest.json", preflight_path / "summary.json"
    owners_path, source_path = preflight_path / "owners.jsonl", preflight_path / "source_attempts.jsonl"
    if not all(path.is_file() for path in (manifest_path, summary_path, owners_path, source_path)):
        raise RuntimeError("formal run requires the complete passing Issue #59 semantic preflight")
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
    budget_path = ledger_path()
    if not is_ledger_prefix_sha256(budget_path, str(summary.get("budget_events_sha256", ""))):
        raise RuntimeError("preflight summary does not identify an append-only budget-ledger prefix")
    owners = load_completed(owners_path)
    witnesses = [row for row in owners if row.get("preflight_role") == "later_battle_semantic_witness"]
    if (summary.get("preflight_semantic_pass") is not True
            or summary.get("terminal_disposition") != "SEMANTIC_PREFLIGHT_PASS"
            or len(witnesses) != 1
            or not preflight_qualification(witnesses[0]).get("qualifies")):
        raise RuntimeError("preflight lacks the required later-battle source root and constrained-Native witness")
    return {
        "summary_sha256": sha256_file(summary_path),
        "manifest_sha256": sha256_file(manifest_path),
        "owners_jsonl_sha256": sha256_file(owners_path),
        "source_attempts_sha256": sha256_file(source_path),
        "budget_events_sha256": summary["budget_events_sha256"],
        "witness_owner_seed": int(witnesses[0]["seed"]),
        "witness_source_battle_number": int(witnesses[0]["source_root"]["source_battle_number"]),
        "semantic_witness": preflight_qualification(witnesses[0]),
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


def write_summary(output: Path, report: dict[str, Any], journal: EventJournal,
                  owner_file: Path, source_attempts: Path) -> dict[str, Any]:
    report["producer_manifest_sha256"] = sha256_file(output / "manifest.json")
    report["owners_jsonl_sha256"] = sha256_file(owner_file) if owner_file.exists() else None
    report["source_attempts_sha256"] = sha256_file(source_attempts) if source_attempts.exists() else None
    journal.event(None, "summary_durable", terminal_disposition=report["terminal_disposition"],
        owners_processed=report.get("owners_processed"), eligible_owners=report.get("eligible_owner_denominator"))
    report["budget_events_sha256"] = sha256_jsonl(journal.path)
    (output / "summary.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_progress(output / "progress.json", journal, load_completed(owner_file), status=report["terminal_disposition"])
    return report


def finalize_preflight(owners: Sequence[Mapping[str, Any]], journal: EventJournal,
                       disposition: str, witness_seed: int | None) -> dict[str, Any]:
    counts = Counter(str(row.get("source_disposition")) for row in owners)
    witness = next((row for row in owners if row.get("seed") == witness_seed), None)
    return {
        "issue": 59, "phase": "semantic_preflight", "study_disposition": "STUDY_ONLY",
        "terminal_disposition": disposition,
        "preflight_semantic_pass": disposition == "SEMANTIC_PREFLIGHT_PASS",
        "owners_scanned": len(owners), "fixed_seed_order_scanned": [int(row["seed"]) for row in owners],
        "source_dispositions_in_scan": dict(counts),
        "witness_owner_seed": witness_seed,
        "witness_source_battle_number": None if witness is None else witness.get("source_root", {}).get("source_battle_number"),
        "witness_checks": None if witness is None else preflight_qualification(witness),
        "builder_all_in_process_cpu_s": round(journal.prior_cpu_s + time.process_time() - journal.cpu_started, 3),
        "reviewer_reserved_process_cpu_s": 1800,
        "limitations": ["preflight is an integration witness, not a cohort estimate", "no B2, real reconvergence, or survival tie is required for semantic pass"],
    }


def run_preflight(output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    source_manifest(output)
    journal = EventJournal(output)
    owner_file, source_attempts = output / "owners.jsonl", output / "source_attempts.jsonl"
    owners = load_completed(owner_file)
    completed = {int(row["seed"]) for row in owners}
    attempts = load_completed(source_attempts)
    attempts_by_seed = {int(row["seed"]): row for row in attempts if row.get("seed") is not None}
    orphan_started = sorted(recorded_source_starts(journal.path, output) - completed - set(attempts_by_seed))
    witness_seed = next((int(row["seed"]) for row in owners
        if row.get("preflight_role") == "later_battle_semantic_witness"), None)
    status = "PREFLIGHT_IN_PROGRESS"
    if orphan_started:
        # A hard interruption inside a source run leaves no safe native
        # checkpoint to restore. Record the boundary and never reselect it.
        for seed in orphan_started:
            owner = {"seed": seed, "source_disposition": "source_interrupted_before_attempt_record",
                "preflight_role": "source_interrupted_before_durable_attempt"}
            append_jsonl(owner_file, owner)
            owners.append(owner)
            completed.add(seed)
            journal.event(seed, "preflight_source_interruption_recovered")
        status = "RESOURCE_BLOCKED"
        write_progress(output / "progress.json", journal, owners, status=status)
    if witness_seed is not None and status == "PREFLIGHT_IN_PROGRESS":
        witness = next(row for row in owners if int(row["seed"]) == witness_seed)
        if witness.get("branch_execution_incomplete"):
            status = "RESOURCE_BLOCKED"
        elif preflight_qualification(witness).get("qualifies"):
            status = "SEMANTIC_PREFLIGHT_PASS"
        elif witness.get("intervention_not_executable"):
            status = "INTERVENTION_NOT_EXECUTABLE"
        else:
            status = "VALIDITY_FAILURE"
    if status == "PREFLIGHT_IN_PROGRESS":
        for seed in SEEDS:
            if seed in completed:
                continue
            prior_attempt = attempts_by_seed.get(seed)
            if prior_attempt is not None:
                if prior_attempt.get("source_disposition") != "eligible_root":
                    owner = dict(prior_attempt)
                    owner["preflight_role"] = "source_disposition_scan"
                    append_jsonl(owner_file, owner)
                    owners.append(owner)
                    completed.add(seed)
                    journal.event(seed, "preflight_source_attempt_recovered",
                        disposition=owner.get("source_disposition"))
                    write_progress(output / "progress.json", journal, owners, status=status)
                    continue
                owner = incomplete_owner_from_source(prior_attempt, "preflight_interrupted_after_source_root")
                owner["preflight_role"] = "later_battle_semantic_witness"
                owner["branch_execution_incomplete"] = True
                append_jsonl(owner_file, owner)
                owners.append(owner)
                witness_seed = seed
                status = "RESOURCE_BLOCKED"
                journal.event(seed, "preflight_root_recovered_without_native_checkpoint")
                write_progress(output / "progress.json", journal, owners, status=status)
                break
            try:
                journal.check_budget(seed)
                journal.event(seed, "preflight_source_started")
                source, root, adapter = run_source(seed, journal, source_attempts)
            except BudgetStop:
                status = "RESOURCE_BLOCKED"
                journal.event(seed, "preflight_stopped_at_cpu_ceiling", prior_stage=journal.last_stage)
                write_progress(output / "progress.json", journal, owners, status=status)
                break
            except Exception as exc:
                source = {"seed": seed, "source_disposition": "source_error",
                    "source_problems": [f"{type(exc).__name__}: {exc}"]}
                root, adapter = None, None
                journal.event(seed, "preflight_source_error", error=f"{type(exc).__name__}: {exc}")
            if root is None:
                owner = dict(source)
                owner["preflight_role"] = "source_disposition_scan"
            elif root.source_battle_number == 1:
                adapter.close()
                owner = dict(source)
                owner["preflight_role"] = "first_battle_root_not_later_battle_witness"
            else:
                try:
                    owner = pair_source_owner(source, root, adapter, seed, journal)
                    owner["preflight_role"] = "later_battle_semantic_witness"
                    witness_seed = seed
                    if preflight_qualification(owner).get("qualifies"):
                        status = "SEMANTIC_PREFLIGHT_PASS"
                    elif owner.get("intervention_not_executable"):
                        status = "INTERVENTION_NOT_EXECUTABLE"
                    else:
                        status = "VALIDITY_FAILURE"
                except BudgetStop:
                    owner = incomplete_owner_from_source(source, "preflight_interrupted_at_cpu_ceiling")
                    owner["preflight_role"] = "later_battle_semantic_witness"
                    owner["branch_execution_incomplete"] = True
                    witness_seed = seed
                    status = "RESOURCE_BLOCKED"
                    journal.event(seed, "preflight_branch_pair_stopped_at_cpu_ceiling")
            append_jsonl(owner_file, owner)
            owners.append(owner)
            completed.add(seed)
            journal.event(seed, "preflight_owner_result_durable",
                disposition=owner.get("source_disposition"), role=owner.get("preflight_role"),
                terminal_disposition=status if status != "PREFLIGHT_IN_PROGRESS" else None)
            write_progress(output / "progress.json", journal, owners,
                status=status if status != "PREFLIGHT_IN_PROGRESS" else "PREFLIGHT_IN_PROGRESS")
            if status != "PREFLIGHT_IN_PROGRESS":
                break
        if status == "PREFLIGHT_IN_PROGRESS" and len(owners) == len(SEEDS):
            status = "INSUFFICIENT_NATURAL_EXPOSURE"
    report = finalize_preflight(owners, journal, status, witness_seed)
    return write_summary(output, report, journal, owner_file, source_attempts)


def incomplete_owner_from_source(source: Mapping[str, Any], status: str) -> dict[str, Any]:
    owner = dict(source)
    if owner.get("source_disposition") == "eligible_root":
        for mode in ("consume", "reserve"):
            owner.setdefault(mode, {"endpoint": {"primary": None, "status": status}})
        owner["branch_execution_incomplete"] = True
    return owner


def run_formal(output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    producer_commit = os.environ.get("SPIRE_WORKTREE_COMMIT", "")
    gate = verify_preflight_gate(PREFLIGHT_PATH, producer_commit)
    source_manifest(output, gate)
    journal = EventJournal(output)
    owner_file, source_attempts = output / "owners.jsonl", output / "source_attempts.jsonl"
    owners = load_completed(owner_file)
    completed = {int(row["seed"]) for row in owners if row.get("seed") is not None}
    attempts = load_completed(source_attempts)
    attempts_by_seed = {int(row["seed"]): row for row in attempts if row.get("seed") is not None}
    orphan_started = sorted(recorded_source_starts(journal.path, output) - completed - set(attempts_by_seed))

    # An opaque native root cannot be restored after a crash. Recover a durable
    # source disposition from its attempt record and leave eligible branches
    # incomplete instead of silently reselecting/replaying that owner.
    for seed in SEEDS:
        if seed in attempts_by_seed and seed not in completed:
            row = incomplete_owner_from_source(attempts_by_seed[seed], "incomplete_after_interruption")
            row["interrupted_after_source_attempt"] = True
            append_jsonl(owner_file, row)
            owners.append(row)
            completed.add(seed)
            journal.event(seed, "owner_recovered_after_interruption",
                disposition=row.get("source_disposition"), branches_incomplete=bool(row.get("branch_execution_incomplete")))
    for seed in orphan_started:
        row = {"seed": seed, "source_disposition": "source_interrupted_before_attempt_record",
            "interrupted_before_source_attempt_record": True}
        append_jsonl(owner_file, row)
        owners.append(row)
        completed.add(seed)
        journal.event(seed, "source_interruption_recovered_without_rerun")

    status = "RESOURCE_BLOCKED" if orphan_started else "FORMAL_IN_PROGRESS"
    for seed in SEEDS:
        if status == "RESOURCE_BLOCKED":
            break
        if seed in completed:
            continue
        try:
            journal.check_budget(seed)
            owner = run_owner(seed, journal, output, source_attempts)
        except BudgetStop:
            journal.event(seed, "owner_stopped_at_cpu_ceiling", prior_stage=journal.last_stage)
            current_attempts = load_completed(source_attempts)
            source_record = next((row for row in reversed(current_attempts)
                if row.get("seed") == seed), attempts_by_seed.get(seed))
            if source_record is not None:
                owner = incomplete_owner_from_source(source_record, "incomplete_at_builder_cpu_ceiling")
            elif seed in recorded_source_starts(journal.path, output):
                owner = {"seed": seed, "source_disposition": "source_interrupted_at_budget_boundary",
                    "source_execution_incomplete": True}
            else:
                owner = {"seed": seed, "source_disposition": "not_run_budget_stop"}
            append_jsonl(owner_file, owner)
            owners.append(owner)
            completed.add(seed)
            status = "RESOURCE_BLOCKED"
            for rest in SEEDS:
                if rest not in completed:
                    row = {"seed": rest, "source_disposition": "not_run_budget_stop"}
                    append_jsonl(owner_file, row)
                    owners.append(row)
                    completed.add(rest)
            write_progress(output / "progress.json", journal, owners, status=status)
            break
        except Exception as exc:
            journal.event(seed, "owner_failed", error=f"{type(exc).__name__}: {exc}")
            source_record = attempts_by_seed.get(seed)
            if source_record is None:
                owner = {"seed": seed, "source_disposition": "source_error",
                    "source_problems": [f"{type(exc).__name__}: {exc}"]}
            else:
                owner = incomplete_owner_from_source(source_record, "incomplete_after_execution_error")
                owner["branch_validity_failure"] = True
                owner["source_problems"] = [f"{type(exc).__name__}: {exc}"]
        append_jsonl(owner_file, owner)
        owners.append(owner)
        completed.add(seed)
        journal.event(seed, "owner_result_durable", disposition=owner.get("source_disposition"),
            intervention_not_executable=owner.get("intervention_not_executable", False),
            branch_validity_failure=owner.get("branch_validity_failure", False))
        write_progress(output / "progress.json", journal, owners,
            status="FORMAL_IN_PROGRESS" if status == "FORMAL_IN_PROGRESS" else status)
    if status == "RESOURCE_BLOCKED":
        for seed in SEEDS:
            if seed not in completed:
                row = {"seed": seed, "source_disposition": "not_run_resource_blocked"}
                append_jsonl(owner_file, row)
                owners.append(row)
                completed.add(seed)
    owners_by_seed = {int(row["seed"]): row for row in owners if row.get("seed") is not None}
    ordered_owners = [owners_by_seed[seed] for seed in SEEDS if seed in owners_by_seed]
    report = finalize(ordered_owners, journal)
    if status == "RESOURCE_BLOCKED":
        report["terminal_disposition"] = status
    return write_summary(output, report, journal, owner_file, source_attempts)


def run(output: Path, *, preflight: bool) -> dict[str, Any]:
    return run_preflight(output) if preflight else run_formal(output)


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue #59 frozen A20 two-battle potion-allocation study")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    default = Path("studies/issue59/preflight") if args.preflight else Path("studies/issue59/results")
    output = args.output or default
    try:
        report = run(output, preflight=args.preflight)
    except BudgetStop:
        return 2
    print(json.dumps({"terminal_disposition": report["terminal_disposition"],
        "owners_processed": report.get("owners_processed", report.get("owners_scanned")),
        "eligible_owner_denominator": report.get("eligible_owner_denominator"),
        "builder_all_in_process_cpu_s": report.get("builder_all_in_process_cpu_s"),
        "summary": str(output / "summary.json")}, sort_keys=True))
    if args.preflight and report.get("terminal_disposition") == "INTERVENTION_NOT_EXECUTABLE":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
