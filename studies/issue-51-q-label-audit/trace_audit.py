"""Read-only receipt/trace audit; raw dictionaries never leave this module.

Only explicit public field projections are emitted. No simulator is imported.
"""
import collections
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import time
import re

DRIVERS = (446001, 446002)
STSRL = "7d172f7c24c9d29429b6946ba44f6b59760b0d96"
NATIVE = "18bcd565b587186425bb46ad3d70a2eb80d947fc"


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def public(raw):
    out = {k: raw[k] for k in ("cur_hp", "max_hp", "gold", "floor_num", "act",
           "potion_count", "potion_capacity") if k in raw}
    out["potions"] = [{k: p[k] for k in ("name", "id_label", "potion_index") if k in p}
                      for p in raw.get("potions", [])]
    out["deck"] = [{k: c[k] for k in ("name", "id_label", "upgraded") if k in c}
                   for c in raw.get("deck", [])]
    return out


def action(item):
    item = item.get("action", item)
    result = {k: item[k] for k in ("kind", "label") if k in item}
    label = result.get("label", "")
    if re.search(r"\bbits\s*=", label, re.IGNORECASE):
        # Match the accepted public-action convention: kind and legal option
        # indices are public; the opaque native executor integer is audit-only.
        kind = result.get("kind", "unknown")
        indices = re.findall(r"\bidx([123])=(-?\d+)\b", label)
        result["label"] = "game." + kind + "".join(
            f" idx{number}={value}" for number, value in indices)
    assert_public_export(result)
    return result


def assert_public_export(value):
    forbidden = ("bits", "hidden_rng", "rng_state", "random_state", "checkpoint",
                 "native_payload", "native_object", "simulator_state", "draw_order",
                 "draw_pile_order", "unrevealed", "future_encounter", "snapshot_raw",
                 "forced_pre_raw", "forced_post_raw")
    if isinstance(value, dict):
        for key, child in value.items():
            assert not any(token in key.lower() for token in forbidden), key
            assert_public_export(child)
    elif isinstance(value, list):
        for child in value:
            assert_public_export(child)
    elif isinstance(value, str):
        assert not re.search(r"\bbits\s*=", value, re.IGNORECASE), value
        assert not any(token in value.lower() for token in forbidden[1:]), value
    return True


def write(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def source_audit():
    """Static text inspection of pinned code, without importing it."""
    git = "git.exe" if Path("/mnt/d").exists() else "git"
    def show(repo, rev, name):
        return subprocess.check_output([git, "-C", repo, "show", f"{rev}:{name}"])
    expert = show("D:/DeadlycatCoding/STSRL", STSRL,
                  "src/sts_combat_rl/sim/non_combat_policy.py")
    text = expert.decode()
    checks = {"category_first_weighted_sampling": "selected_category = _weighted_choice(" in text
              and "selected_index = _weighted_index_choice(" in text,
              "positive_category_and_action_floors": "_EXPERT_LOW_PROBABILITY_CATEGORY_FLOOR = 0.02" in text
              and "_EXPERT_LOW_PROBABILITY_ACTION_FLOOR = 0.02" in text,
              "public_resource_profile": all(f'"{k}"' in text for k in
                  ("current_hp", "max_hp", "gold", "potion_count", "floor", "act")),
              "card_and_route_label_heuristics": "def _expert_card_action_weight(" in text
              and "def _expert_route_action_weight(" in text}
    producer = "843d35ea9de69e8b11b7ee9f2228eda648c2a1aa"
    battle = show("D:/DeadlycatCoding/spire-research", producer,
                  "studies/issue-46-source-capacity/common.py")
    battle_text = battle.decode()
    checks["frozen_unguided_battle_v2_400"] = (
        "battle_search_v2(snapshot,simulations=400,include_potions=True,policy_prior_callback=None,leaf_value_callback=None)" in battle_text
        and "RoutedRunController(Battle(),PolicyController(ExpertNonCombatDriver(" in battle_text)
    return {"stsrl_commit": STSRL, "path": "src/sts_combat_rl/sim/non_combat_policy.py",
            "sha256": hashlib.sha256(expert).hexdigest(), "checks": checks,
            "battle_source": {"producer_commit": producer,
                 "path": "studies/issue-46-source-capacity/common.py",
                 "sha256": hashlib.sha256(battle).hexdigest()},
            "interpretation": "Weighted stochastic public category/action heuristics; this source does not establish strong optimizing search or causal weakness."}


def audit_traces(owner_rows, store46, output_dir):
    started = time.process_time()
    store, out = Path(store46), Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    seal_path = store / "artifact-manifest.json"
    sealed = {f["path"]: f["sha256"] for f in json.loads(seal_path.read_text())["files"]}
    expected = {f'{r["owner"]}/label-{i}-{d}.{ext}' for r in owner_rows
                for i in range(r["legal_count"]) for d in DRIVERS for ext in ("json", "jsonl.gz")}
    actual = {str(p.relative_to(store)) for p in store.glob("*/label-*")}
    summary = {"source_seal_sha256": digest(seal_path), "expected_label_and_trace_files": len(expected),
               "provided_expected_files": len(expected & actual), "missing_files": sorted(expected - actual),
               "orphan_files": sorted(actual - expected), "receipts": [], "violations": [],
               "owner_driver_patterns": [], "complete": False,
               "interpretation": "Decision counts are descriptive repeated trajectory events, not independent samples. Trace starts after the forced action. Equal-floor examples show resolution only, not superior choices."}
    examples = {"projection_fields": "HP, max HP, gold, floor, act, potion count/capacity, potion name/id_label/index, deck name/id_label/upgraded, chosen kind/label, controller_role only",
                "equal_floor_different_public_intermediates": [], "paired_driver_decision_examples": []}
    try:
        summary["pinned_expert_source"] = source_audit()
        if not all(summary["pinned_expert_source"]["checks"].values()):
            summary["violations"].append({"reason": "pinned_source_check_failed"})
        for row in sorted(owner_rows, key=lambda r: int(r["owner"])):
            trajectories = {}
            owner_counts = {d: collections.Counter() for d in DRIVERS}
            for ordinal in range(row["legal_count"]):
                for driver_index, driver in enumerate(DRIVERS):
                    if time.process_time() - started >= 400:
                        raise TimeoutError("400 CPU-second trace audit cap")
                    rel = f'{row["owner"]}/label-{ordinal}-{driver}.json'
                    trace_rel = rel[:-5] + ".jsonl.gz"
                    if rel not in actual or trace_rel not in actual:
                        continue
                    label_hash, trace_hash = digest(store / rel), digest(store / trace_rel)
                    errors = []
                    if sealed.get(rel) != label_hash or sealed.get(trace_rel) != trace_hash:
                        errors.append("source_seal_hash_mismatch")
                    label = json.loads((store / rel).read_text())
                    for key, expected_value in (("owner", row["owner"]), ("root_id", row["root_id"]),
                        ("ordinal", ordinal), ("driver_seed", driver),
                        ("terminal_floor", row["floors"][ordinal][driver_index])):
                        if label.get(key) != expected_value:
                            errors.append("label_" + key)
                    if not label.get("terminal") or label.get("problems") or label.get("new_assistance"):
                        errors.append("label_terminal_or_problem_or_assistance")
                    if label.get("source", {}).get("stsrl_commit") != STSRL or label.get("source", {}).get("native_commit") != NATIVE:
                        errors.append("source_pin")
                    if label["initial_raw"] != label["forced_post_raw"]:
                        errors.append("initial_forced_post")
                    previous = label["initial_raw"]
                    steps, screens, patterns = 0, collections.Counter(), collections.Counter()
                    public_steps, checkpoints = [], {}
                    last_terminal = False
                    with gzip.open(store / trace_rel, "rt") as stream:
                        for line in stream:
                            if time.process_time() - started >= 400:
                                raise TimeoutError("400 CPU-second trace audit cap")
                            event = json.loads(line)
                            if event["snapshot_raw"] != previous:
                                errors.append(f"snapshot_chain:{steps}")
                            if event["step_index"] != steps:
                                errors.append(f"step_index:{steps}")
                            if last_terminal:
                                errors.append("event_after_terminal")
                            screen = event["screen_state"]
                            screens[screen] += 1
                            if not event.get("battle_active"):
                                chosen = action(event["chosen_action_identity"])
                                counts_key = screen + ":" + chosen.get("kind", "unknown")
                                patterns[counts_key] += 1
                                snap = public(event["snapshot_raw"])
                                nxt = public(event["next_snapshot_raw"])
                                owner_counts[driver]["nonbattle_potion_count_delta"] += nxt.get("potion_count", 0) - snap.get("potion_count", 0)
                                if snap.get("potion_count", 0) > 0:
                                    owner_counts[driver]["nonbattle_decisions_holding_potions"] += 1
                                pub = {"step": steps, "screen": screen, "chosen": chosen,
                                       "before": snap, "after": nxt,
                                       "decision_metadata": {"controller_role": event.get("decision_metadata", {}).get("controller_role")}}
                                if len(public_steps) < 8:
                                    public_steps.append(pub)
                                checkpoints.setdefault((snap.get("act"), snap.get("floor_num"), screen), pub)
                            previous = event["next_snapshot_raw"]
                            last_terminal = event["terminal_after_step"]
                            steps += 1
                    if not steps or previous != label["final_raw"] or not last_terminal:
                        errors.append("final_or_terminal")
                    if previous.get("floor_num") != label["terminal_floor"] or previous.get("outcome") != label.get("outcome"):
                        errors.append("final_floor_or_outcome")
                    if dict(screens) != label.get("screens"):
                        errors.append("screen_counts")
                    receipt = {"owner": row["owner"], "root_id": row["root_id"], "ordinal": ordinal,
                               "driver": driver, "label_path": rel, "trace_path": trace_rel,
                               "label_sha256": label_hash, "trace_sha256": trace_hash,
                               "steps": steps, "terminal_floor": label["terminal_floor"],
                               "terminal_outcome": label["outcome"], "screens": dict(screens),
                               "nonbattle_patterns": dict(patterns), "valid": not errors}
                    summary["receipts"].append(receipt)
                    if errors:
                        summary["violations"].append({"owner": row["owner"], "ordinal": ordinal, "driver": driver, "reasons": errors})
                    else:
                        owner_counts[driver].update(patterns)
                        trajectories[ordinal, driver] = {"receipt": receipt, "forced_action": action(row["actions"][ordinal]),
                            "first_public_decisions": public_steps, "checkpoints": checkpoints}
            for driver in DRIVERS:
                summary["owner_driver_patterns"].append({"owner": row["owner"], "driver": driver,
                    "split": row["split"], "family": row["family"], "assisted": row["assisted"],
                    "patterns": dict(owner_counts[driver])})
            if len(examples["paired_driver_decision_examples"]) < 4:
                for ordinal in range(row["legal_count"]):
                    a, b = trajectories.get((ordinal, DRIVERS[0])), trajectories.get((ordinal, DRIVERS[1]))
                    if a and b and [s["chosen"] for s in a["first_public_decisions"]] != [s["chosen"] for s in b["first_public_decisions"]]:
                        examples["paired_driver_decision_examples"].append({"same_forced_ordinal": ordinal,
                            "continuations": [{k: x[k] for k in ("receipt", "forced_action", "first_public_decisions")} for x in (a, b)]})
                        break
            if len(examples["equal_floor_different_public_intermediates"]) < 6:
                found = False
                for driver in DRIVERS:
                    for i in range(row["legal_count"]):
                        for j in range(i + 1, row["legal_count"]):
                            a, b = trajectories.get((i, driver)), trajectories.get((j, driver))
                            if not a or not b or a["receipt"]["terminal_floor"] != b["receipt"]["terminal_floor"]:
                                continue
                            for gate in sorted(set(a["checkpoints"]) & set(b["checkpoints"])):
                                x, y = a["checkpoints"][gate], b["checkpoints"][gate]
                                if x["before"] != y["before"]:
                                    examples["equal_floor_different_public_intermediates"].append({"common_act_floor_screen": gate,
                                        "continuations": [{"receipt": t["receipt"], "forced_action": t["forced_action"], "public_decision": p}
                                                          for t, p in ((a, x), (b, y))]})
                                    found = True
                                    break
                            if found: break
                        if found: break
                    if found: break
        summary["complete"] = not summary["missing_files"] and len(summary["receipts"]) * 2 == len(expected)
    except Exception as exc:
        summary["failure"] = {"type": type(exc).__name__, "message": str(exc)}
    summary["measured_cpu_seconds"] = time.process_time() - started
    assert_public_export(examples)
    write(out / "public_examples.json", examples)
    write(out / "trace_summary.json", summary)
    return summary
