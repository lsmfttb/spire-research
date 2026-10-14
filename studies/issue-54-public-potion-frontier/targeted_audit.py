#!/usr/bin/env python3
"""Read-only, metadata-first audit of the named retained complete-run cohorts."""
from __future__ import annotations
import argparse, collections, gzip, hashlib, json, pathlib, re, time

EXPECTED = {
    "issue45": "624b656dee2afe33cf0e0410dd4cdf1a198c456d3d050a3ac061cb1aec18c160",
    "issue46": "9807c5a02554f57947db1d507d0ed49b10fa07fab88042976b44e60a879e0cd1",
    "issue47": "119a514692836954ab205284c35d1070a9a089cebcc9344fd27ded2c850ff1de",
    "issue48": "210cb9c5aa5423645fc2f41c95f16c0e37784e7523d9b66fa0357afa3a7202ed",
}
OLD_NATIVE = "18bcd565b587186425bb46ad3d70a2eb80d947fc"
STSRL = "7d172f7c24c9d29429b6946ba44f6b59760b0d96"
SLOT_RE = re.compile(r"(?:idx1|potion_index|slot|potion_slot)\s*=\s*(\d+)")
USE = {"potion", "game_potion_use"}
DISCARD = {"game_potion_discard", "potion_discard", "reward_potion_discard", "shop_potion_discard"}
TAKE = {"reward_potion", "shop_reward_potion"}
SKIP = {"skip", "skip_reward", "reward_skip", "skip_card", "card_skip", "skip_potion"}
EMPTY = {"", "EMPTY_POTION_SLOT", "EMPTY_POTION_ID", "NONE", "NULL"}

def digest(path: pathlib.Path):
    h, n = hashlib.sha256(), 0
    with path.open("rb") as f:
        while b := f.read(1024 * 1024):
            n += len(b); h.update(b)
    return {"bytes": n, "sha256": h.hexdigest()}

def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))

def owner_key(seed):
    return hashlib.sha256(f"issue54-owner-v1:{seed}".encode()).hexdigest()[:20]

def potion_name(v):
    if isinstance(v, dict): v = v.get("name", v.get("id_label", ""))
    # Canonicalize display names ("Blood Potion") and identifiers
    # ("BLOOD_POTION") to the same token before event and slot-label checks.
    return re.sub(r"[^A-Z0-9]+", "_", str(v or "").strip().upper()).strip("_")

def relic_inventory(*snapshots):
    observed = False
    names = set()
    for snapshot in snapshots:
        if not isinstance(snapshot, dict): continue
        for key in ("battle_relics", "relics"):
            values = snapshot.get(key)
            if isinstance(values, list):
                observed = True
                names.update(potion_name(x) for x in values if isinstance(x, (str, dict)))
    return observed, names

def action_kind(v):
    if isinstance(v, dict): v = v.get("kind")
    return str(v or "").strip().lower()

def action_slot(v):
    if not isinstance(v, dict): return None
    for x in (v.get("label"), v.get("action_id")):
        m = SLOT_RE.search(x) if isinstance(x, str) else None
        if m: return int(m.group(1))
    label = v.get("label")
    m = re.search(r"\((\d+)\)", label) if isinstance(label, str) else None
    if m: return int(m.group(1))
    return None

def action_class(k):
    if k in USE: return "use"
    if k in DISCARD: return "discard"
    if k in TAKE: return "take"
    if k in SKIP: return "skip"
    if "potion" in k: return "other_potion_action"
    return "other"

def slot_inventory(s, screen):
    slots = s.get("battle_potions") if screen == "BATTLE" else None
    if not isinstance(slots, list): slots = s.get("potions")
    if not isinstance(slots, list): slots = []
    names = [potion_name(x) for x in slots]
    names = [x for x in names if x not in EMPTY]
    count = s.get("battle_potion_count") if screen == "BATTLE" else None
    if count is None: count = s.get("potion_count")
    try: count = int(count) if count is not None else None
    except (TypeError, ValueError): count = None
    return count, names, slots

def scan_on_trace(path):
    out = {
        "decision_count": 0, "battle_potion_use_opportunity_decisions": 0,
        "battle_potion_use_at_opportunity_decisions": 0,
        "battle_potion_nonuse_while_use_legal_decisions": 0,
        "act1_battle_potion_use_opportunity_decisions": 0,
        "act1_battle_potion_use_at_opportunity_decisions": 0,
        "act1_battle_potion_nonuse_while_use_legal_decisions": 0,
        "battle_potion_held_decisions": 0, "battle_potion_full_slot_decisions": 0,
        "reward_potion_offer_decisions": 0, "reward_potion_take_decisions": 0,
        "reward_potion_skip_decisions": 0, "reward_potion_other_decisions": 0,
        "act1_reward_potion_offer_decisions": 0, "act1_reward_potion_take_decisions": 0,
        "act1_reward_potion_skip_decisions": 0, "act1_reward_potion_other_decisions": 0,
        "reward_potion_full_inventory_offer_decisions": 0,
        "reward_potion_overflow_discard_option_occurrences": 0,
        "reward_potion_overflow_discard_choice_decisions": 0,
        "act1_reward_potion_full_inventory_offer_decisions": 0,
        "act1_reward_potion_overflow_discard_option_occurrences": 0,
        "act1_reward_potion_overflow_discard_choice_decisions": 0,
        "legal_potion_action_occurrences_by_class": collections.Counter(),
        "selected_potion_action_occurrences_by_class_and_screen": collections.Counter(),
        "act1_legal_potion_action_occurrences_by_class": collections.Counter(),
        "act1_selected_potion_action_occurrences_by_class_and_screen": collections.Counter(),
        "potion_use_occurrences_by_screen": collections.Counter(),
        "potion_use_identity_mapped_count": 0, "potion_use_identity_unmapped_count": 0,
        "potion_use_slot_label_identity_match_count": 0, "potion_use_slot_label_identity_mismatch_count": 0,
        "blood_potion_uses": 0, "blood_potion_uses_with_sacred_bark": 0,
        "blood_potion_battle_uses": 0,
        "blood_potion_battle_uses_with_sacred_bark": 0,
        "blood_potion_battle_uses_without_sacred_bark": 0,
        "blood_potion_battle_bark_context_unobserved": 0,
        "smoke_bomb_uses": 0, "smoke_bomb_escapes": 0,
        "smoke_bomb_battle_uses": 0,
        "victory_with_meat_on_bone_and_burning_blood": 0,
        "victory_face_of_cleric_to_rewards": 0,
        "sensitive_event_context_unobserved": 0,
        "potentially_affected_event_counts": collections.Counter(),
        "owners_actions": set(), "floor_decisions": collections.Counter(),
        "screen_decisions": collections.Counter(), "pre_snapshot_rows": 0,
        "post_snapshot_rows": 0, "pre_post_rows": 0,
        "pre_post_key_coverage": collections.Counter(),
        "checks": collections.Counter(), "terminal_step": False,
    }
    with path.open("rt", encoding="utf-8") as f:
        for i, line in enumerate(f):
            r = json.loads(line); out["decision_count"] += 1
            if r.get("step_index") != i: out["checks"]["step_order_mismatch"] += 1
            s = r.get("snapshot_raw") if isinstance(r.get("snapshot_raw"), dict) else None
            n = r.get("next_snapshot_raw") if isinstance(r.get("next_snapshot_raw"), dict) else None
            if s is not None: out["pre_snapshot_rows"] += 1
            if n is not None: out["post_snapshot_rows"] += 1
            if s is not None and n is not None:
                out["pre_post_rows"] += 1
                for key in ("screen_state", "act", "floor_num", "potion_count", "potion_capacity", "potions"):
                    if key in s: out["pre_post_key_coverage"][f"pre_{key}"] += 1
                    if key in n: out["pre_post_key_coverage"][f"post_{key}"] += 1
            screen = str(r.get("screen_state") or (s or {}).get("screen_state") or "unknown")
            try: act1 = int((s or {}).get("act")) == 1
            except (TypeError, ValueError): act1 = False
            out["screen_decisions"][screen] += 1
            floor = (s or {}).get("floor_num", r.get("floor"))
            act = (s or {}).get("act")
            try:
                if int(act) == 1:
                    fnum = int(floor)
                    band = "F00-F08" if fnum <= 8 else ("F09-F16" if fnum <= 16 else "F17+")
                    out["floor_decisions"][band] += 1
            except (TypeError, ValueError):
                pass
            count, held, slots = slot_inventory(s or {}, screen)
            if screen == "BATTLE" and count is not None and count > 0:
                out["battle_potion_held_decisions"] += 1
                cap = (s or {}).get("battle_potion_capacity", (s or {}).get("potion_capacity"))
                try:
                    if cap is not None and count >= int(cap): out["battle_potion_full_slot_decisions"] += 1
                except (TypeError, ValueError): pass
            legal = r.get("legal_action_identities") if isinstance(r.get("legal_action_identities"), list) else []
            kinds = [action_kind(x) for x in legal]
            for k in kinds:
                c = action_class(k)
                if c != "other":
                    out["legal_potion_action_occurrences_by_class"][f"{screen}:{c}"] += 1
                    if act1: out["act1_legal_potion_action_occurrences_by_class"][f"{screen}:{c}"] += 1
            chosen = r.get("chosen_action_identity")
            ck = action_kind(chosen) if isinstance(chosen, dict) else action_kind(r.get("chosen_action_kind"))
            cclass = action_class(ck)
            if cclass != "other":
                out["selected_potion_action_occurrences_by_class_and_screen"][f"{screen}:{cclass}"] += 1
                if act1: out["act1_selected_potion_action_occurrences_by_class_and_screen"][f"{screen}:{cclass}"] += 1
            if isinstance(chosen, dict) and r.get("chosen_action_kind") and ck != str(r["chosen_action_kind"]).lower():
                out["checks"]["chosen_kind_mismatch"] += 1
            idx = r.get("chosen_action_index")
            if isinstance(idx, int) and 0 <= idx < len(legal):
                out["checks"]["selected_index_in_legal"] += 1
                if chosen == legal[idx]: out["checks"]["selected_identity_matches_index"] += 1
                else: out["checks"]["selected_identity_index_mismatch"] += 1
            else: out["checks"]["selected_index_not_in_legal"] += 1
            eligible = r.get("eligible_action_indices")
            if isinstance(eligible, list) and isinstance(idx, int) and idx in eligible:
                out["checks"]["selected_index_in_eligible"] += 1
            elif isinstance(eligible, list): out["checks"]["selected_index_not_in_eligible"] += 1
            else: out["checks"]["eligible_candidates_unobserved"] += 1
            if screen == "BATTLE" and count is not None and count > 0 and any(k in USE for k in kinds):
                out["battle_potion_use_opportunity_decisions"] += 1
                if act1: out["act1_battle_potion_use_opportunity_decisions"] += 1
                if cclass == "use":
                    out["battle_potion_use_at_opportunity_decisions"] += 1
                    if act1: out["act1_battle_potion_use_at_opportunity_decisions"] += 1
                    out["owners_actions"].add("use")
                else:
                    out["battle_potion_nonuse_while_use_legal_decisions"] += 1
                    if act1: out["act1_battle_potion_nonuse_while_use_legal_decisions"] += 1
                    out["owners_actions"].add("nonuse_at_decision")
            if screen == "REWARDS" and any(k in {"reward_potion", "shop_reward_potion"} for k in kinds):
                out["reward_potion_offer_decisions"] += 1
                if act1: out["act1_reward_potion_offer_decisions"] += 1
                cap = (s or {}).get("potion_capacity")
                try: full = count is not None and cap is not None and count >= int(cap)
                except (TypeError, ValueError): full = False
                if full:
                    out["reward_potion_full_inventory_offer_decisions"] += 1
                    if act1: out["act1_reward_potion_full_inventory_offer_decisions"] += 1
                    out["reward_potion_overflow_discard_option_occurrences"] += sum(k in DISCARD for k in kinds)
                    if act1: out["act1_reward_potion_overflow_discard_option_occurrences"] += sum(k in DISCARD for k in kinds)
                    if cclass == "discard": out["reward_potion_overflow_discard_choice_decisions"] += 1
                    if act1 and cclass == "discard": out["act1_reward_potion_overflow_discard_choice_decisions"] += 1
                if cclass == "take":
                    out["reward_potion_take_decisions"] += 1
                    if act1: out["act1_reward_potion_take_decisions"] += 1
                elif cclass == "skip":
                    out["reward_potion_skip_decisions"] += 1
                    if act1: out["act1_reward_potion_skip_decisions"] += 1
                else:
                    out["reward_potion_other_decisions"] += 1
                    if act1: out["act1_reward_potion_other_decisions"] += 1
            if cclass == "use":
                out["potion_use_occurrences_by_screen"][screen] += 1
                slot = action_slot(chosen if isinstance(chosen, dict) else {})
                if slot is None or slot < 0 or slot >= len(slots):
                    out["potion_use_identity_unmapped_count"] += 1
                    item = ""
                else:
                    item = potion_name(slots[slot]); out["potion_use_identity_mapped_count"] += bool(item)
                    if not item: out["potion_use_identity_unmapped_count"] += 1
                    elif isinstance(chosen, dict) and item in potion_name(chosen.get("label", "")):
                        out["potion_use_slot_label_identity_match_count"] += 1
                    else: out["potion_use_slot_label_identity_mismatch_count"] += 1
                relics_observed, rel = relic_inventory(s, n)
                if "BLOOD_POTION" in item:
                    out["blood_potion_uses"] += 1
                    if "SACRED_BARK" in rel:
                        out["blood_potion_uses_with_sacred_bark"] += 1
                    if screen == "BATTLE":
                        out["blood_potion_battle_uses"] += 1
                        out["potentially_affected_event_counts"]["battle_blood_potion_use"] += 1
                        if not relics_observed:
                            out["blood_potion_battle_bark_context_unobserved"] += 1
                            out["sensitive_event_context_unobserved"] += 1
                        elif "SACRED_BARK" in rel:
                            out["blood_potion_battle_uses_with_sacred_bark"] += 1
                        else:
                            out["blood_potion_battle_uses_without_sacred_bark"] += 1
                if "SMOKE_BOMB" in item:
                    out["smoke_bomb_uses"] += 1
                    out["potentially_affected_event_counts"]["smoke_bomb_use"] += 1
                    if screen == "BATTLE": out["smoke_bomb_battle_uses"] += 1
                    outcome = n.get("completed_battle_outcome", n.get("battle_outcome")) if n is not None else None
                    if outcome == "PLAYER_ESCAPE":
                        out["smoke_bomb_escapes"] += 1
                        out["potentially_affected_event_counts"]["smoke_bomb_escape_transition"] += 1
                    elif outcome is None:
                        out["sensitive_event_context_unobserved"] += 1
            if screen == "BATTLE" and n is not None and not bool(n.get("battle_active", False)):
                outcome = n.get("completed_battle_outcome", n.get("battle_outcome"))
                nscreen = n.get("screen_state")
                relics_observed, relics = relic_inventory(s, n)
                if outcome == "PLAYER_VICTORY" and not relics_observed:
                    out["sensitive_event_context_unobserved"] += 1
                if outcome == "PLAYER_VICTORY" and "MEAT_ON_THE_BONE" in relics and ({"BURNING_BLOOD", "BLACK_BLOOD"} & relics):
                    out["victory_with_meat_on_bone_and_burning_blood"] += 1
                    out["potentially_affected_event_counts"]["victory_with_meat_on_bone_and_blood_relic"] += 1
                if outcome == "PLAYER_VICTORY" and "FACE_OF_CLERIC" in relics and nscreen == "REWARDS":
                    out["victory_face_of_cleric_to_rewards"] += 1
                    out["potentially_affected_event_counts"]["victory_face_of_cleric_to_rewards"] += 1
            if r.get("terminal_after_step") is True: out["checks"]["terminal_step_marked"] += 1
            out["terminal_step"] = r.get("terminal_after_step") is True
    if out["terminal_step"]: out["checks"]["last_step_terminal"] += 1
    else: out["checks"]["last_step_not_terminal"] += 1
    # Convert only aggregate counters; no action IDs, labels, snapshots, or simulator-state values leave this process.
    for k, v in list(out.items()):
        if isinstance(v, (collections.Counter, set)):
            out[k] = dict(sorted((x, 1) for x in v)) if isinstance(v, set) else dict(sorted(v.items()))
    return out

def audit_secondary(cache, issue, prefix, expected_manifest, ids, arm):
    manifest_path = cache / "artifact-manifest.json"
    mh = digest(manifest_path)
    if mh["sha256"] != expected_manifest: raise SystemExit(f"{issue} manifest seal mismatch")
    manifest = read_json(manifest_path)
    files = {pathlib.Path(x["path"]).name: x for x in manifest["files"]}
    wanted = [f"{seed}-{arm}{ext}" for seed in ids for ext in (".json", ".jsonl.gz")]
    missing, found, expected_bytes, actual_bytes = [], 0, 0, 0
    for name in wanted:
        entry = files.get(name)
        if entry is None: raise SystemExit(f"{issue} sealed manifest lacks {name}")
        expected_bytes += int(entry["bytes"])
        path = pathlib.Path(entry["path"])
        if path.is_file():
            found += 1; actual_bytes += path.stat().st_size
        else: missing.append(name)
    source_path = cache / "source.json"
    sh = digest(source_path) if source_path.is_file() else None
    source = read_json(source_path) if source_path.is_file() else {}
    return {
        "manifest_sha256": mh["sha256"], "manifest_bytes": mh["bytes"],
        "manifest_file_count": manifest.get("file_count"), "manifest_total_bytes": manifest.get("total_bytes"),
        "review_delete_date": manifest.get("review_delete_date"),
        "source_metadata_sha256": sh["sha256"] if sh else None,
        "source_metadata_bytes": sh["bytes"] if sh else None,
        "native_commit": source.get("native_commit"), "stsrl_commit": source.get("stsrl_commit"),
        "config_sha256": source.get("config_sha256"),
        "sealed_expert_episode_count": len(ids), "sealed_target_metadata_and_trace_files": len(wanted),
        "physically_present_target_files": found, "missing_target_files": len(missing),
        "manifest_expected_target_bytes": expected_bytes, "actual_present_target_bytes": actual_bytes,
        "status": "RAW_DATA_AVAILABLE" if not missing else "RAW_DATA_UNAVAILABLE",
        "exact_candidate_upper_bound_only": len(ids),
        "note": "Manifest entries are provenance expectations only; absent files were not opened or reconstructed.",
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue45-reviewed", type=pathlib.Path, required=True)
    ap.add_argument("--issue46", type=pathlib.Path, required=True)
    ap.add_argument("--issue47", type=pathlib.Path, required=True)
    ap.add_argument("--issue48", type=pathlib.Path, required=True)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--execution", type=pathlib.Path, required=True)
    a = ap.parse_args(); start = time.process_time()
    mpath = a.issue45_reviewed / "manifest.json"
    mh = digest(mpath)
    if mh["sha256"] != EXPECTED["issue45"]: raise SystemExit("Issue45 reviewed pair manifest seal mismatch")
    manifest = read_json(mpath)
    entries = {pathlib.Path(x["path"]).name: x for x in manifest}
    srcpath = a.issue45_reviewed / "source.json"
    source_hash = digest(srcpath); src = read_json(srcpath)
    if (src.get("native_commit"), src.get("stsrl_commit")) != (OLD_NATIVE, STSRL): raise SystemExit("Issue45 source pin mismatch")
    owners=[]; policy_fingerprints=collections.Counter(); source_provenance=collections.Counter(); producer_counts=collections.Counter()
    meta_offer_option_occurrences=0
    expected_owner_ids = list(range(437001, 437097))
    for seed in expected_owner_ids:
        base=f"{seed}-on"; meta_name=base+".json"; trace_name=base+".jsonl"
        pair=[]
        for name in (meta_name, trace_name):
            e=entries.get(name)
            if e is None: raise SystemExit(f"Issue45 manifest lacks {name}")
            path=pathlib.Path(e["path"])
            if not path.is_file(): raise SystemExit(f"Issue45 on-arm source missing: {name}")
            h=digest(path)
            if h != {"bytes":e["bytes"],"sha256":e["sha256"]}: raise SystemExit(f"Issue45 on-arm file hash mismatch: {name}")
            pair.append(h)
        meta=read_json(a.issue45_reviewed/meta_name)
        if meta.get("seed") != seed or meta.get("arm") != "on" or meta.get("terminal") is not True or meta.get("valid") is not True or meta.get("problems"):
            raise SystemExit(f"Issue45 complete-run receipt invalid for owner key {owner_key(seed)}")
        run_source=meta.get("source") or {}
        if (run_source.get("native_commit"),run_source.get("stsrl_commit")) != (OLD_NATIVE,STSRL): raise SystemExit("Issue45 per-run pin mismatch")
        controller=meta.get("controller") or {}; config=controller.get("config") or {}
        policy_fingerprints[hashlib.sha256(json.dumps(config,sort_keys=True,separators=(",",":")).encode()).hexdigest()] += 1
        source_provenance[json.dumps({k:run_source.get(k) for k in ("native_commit","native_extension_sha256","stsrl_commit")},sort_keys=True)] += 1
        producer_counts[str(meta.get("producer") or "unobserved")] += 1
        meta_offer_option_occurrences += int(meta.get("potion_offer_option_occurrences") or 0)
        trace=scan_on_trace(a.issue45_reviewed/trace_name)
        row={
          "owner_key":owner_key(seed),"start_act":(meta.get("initial_raw") or {}).get("act"),
          "start_floor":(meta.get("initial_raw") or {}).get("floor_num"),"terminal_outcome":meta.get("outcome"),
          "terminal_floor":meta.get("floor"),"max_act":meta.get("max_act"),"loss_type":meta.get("loss_type"),
          "heart_reached":meta.get("metrics",{}).get("heart_reached") if isinstance(meta.get("metrics"),dict) else None,
          "decision_count":trace["decision_count"],"screen_decisions":trace["screen_decisions"],
          "act1_floor_decisions":trace["floor_decisions"],
          "battle_potion_held_decisions":trace["battle_potion_held_decisions"],
          "battle_potion_full_slot_decisions":trace["battle_potion_full_slot_decisions"],
          "battle_potion_use_opportunity_decisions":trace["battle_potion_use_opportunity_decisions"],
          "battle_potion_use_at_opportunity_decisions":trace["battle_potion_use_at_opportunity_decisions"],
          "battle_potion_nonuse_while_use_legal_decisions":trace["battle_potion_nonuse_while_use_legal_decisions"],
          "act1_battle_potion_use_opportunity_decisions":trace["act1_battle_potion_use_opportunity_decisions"],
          "act1_battle_potion_use_at_opportunity_decisions":trace["act1_battle_potion_use_at_opportunity_decisions"],
          "act1_battle_potion_nonuse_while_use_legal_decisions":trace["act1_battle_potion_nonuse_while_use_legal_decisions"],
          "reward_potion_offer_decisions":trace["reward_potion_offer_decisions"],
          "reward_potion_take_decisions":trace["reward_potion_take_decisions"],
          "reward_potion_skip_decisions":trace["reward_potion_skip_decisions"],
          "reward_potion_other_decisions":trace["reward_potion_other_decisions"],
          "reward_potion_full_inventory_offer_decisions":trace["reward_potion_full_inventory_offer_decisions"],
          "reward_potion_overflow_discard_option_occurrences":trace["reward_potion_overflow_discard_option_occurrences"],
          "reward_potion_overflow_discard_choice_decisions":trace["reward_potion_overflow_discard_choice_decisions"],
          "act1_reward_potion_offer_decisions":trace["act1_reward_potion_offer_decisions"],
          "act1_reward_potion_take_decisions":trace["act1_reward_potion_take_decisions"],
          "act1_reward_potion_skip_decisions":trace["act1_reward_potion_skip_decisions"],
          "act1_reward_potion_other_decisions":trace["act1_reward_potion_other_decisions"],
          "act1_reward_potion_full_inventory_offer_decisions":trace["act1_reward_potion_full_inventory_offer_decisions"],
          "act1_reward_potion_overflow_discard_option_occurrences":trace["act1_reward_potion_overflow_discard_option_occurrences"],
          "act1_reward_potion_overflow_discard_choice_decisions":trace["act1_reward_potion_overflow_discard_choice_decisions"],
          "legal_potion_action_occurrences_by_class":trace["legal_potion_action_occurrences_by_class"],
          "selected_potion_action_occurrences_by_class_and_screen":trace["selected_potion_action_occurrences_by_class_and_screen"],
          "act1_legal_potion_action_occurrences_by_class":trace["act1_legal_potion_action_occurrences_by_class"],
          "act1_selected_potion_action_occurrences_by_class_and_screen":trace["act1_selected_potion_action_occurrences_by_class_and_screen"],
          "potion_use_occurrences_by_screen":trace["potion_use_occurrences_by_screen"],
          "potion_use_identity_mapped_count":trace["potion_use_identity_mapped_count"],
          "potion_use_identity_unmapped_count":trace["potion_use_identity_unmapped_count"],
          "potion_use_slot_label_identity_match_count":trace["potion_use_slot_label_identity_match_count"],
          "potion_use_slot_label_identity_mismatch_count":trace["potion_use_slot_label_identity_mismatch_count"],
          "blood_potion_uses":trace["blood_potion_uses"],
          "blood_potion_uses_with_sacred_bark":trace["blood_potion_uses_with_sacred_bark"],
          "blood_potion_battle_uses":trace["blood_potion_battle_uses"],
          "blood_potion_battle_uses_with_sacred_bark":trace["blood_potion_battle_uses_with_sacred_bark"],
          "blood_potion_battle_uses_without_sacred_bark":trace["blood_potion_battle_uses_without_sacred_bark"],
          "blood_potion_battle_bark_context_unobserved":trace["blood_potion_battle_bark_context_unobserved"],
          "smoke_bomb_uses":trace["smoke_bomb_uses"],"smoke_bomb_escapes":trace["smoke_bomb_escapes"],
          "smoke_bomb_battle_uses":trace["smoke_bomb_battle_uses"],
          "victory_with_meat_on_bone_and_burning_blood":trace["victory_with_meat_on_bone_and_burning_blood"],
          "victory_face_of_cleric_to_rewards":trace["victory_face_of_cleric_to_rewards"],
          "sensitive_event_context_unobserved":trace["sensitive_event_context_unobserved"],
          "potentially_affected_event_counts":trace["potentially_affected_event_counts"],
          "observed_semantic_context_class":(
            "potentially_affected_context_observed_with_other_context_unresolved" if (
              trace["potentially_affected_event_counts"] and (
                trace["potion_use_identity_unmapped_count"] or trace["potion_use_slot_label_identity_mismatch_count"] or trace["sensitive_event_context_unobserved"]
              )
            ) else ("potentially_affected_context_observed" if trace["potentially_affected_event_counts"] else (
              "not_ascertainable" if (
                trace["potion_use_identity_unmapped_count"] or trace["potion_use_slot_label_identity_mismatch_count"] or trace["sensitive_event_context_unobserved"]
              ) else "no_direct_changed_event_observed"
            ))
          ),
          "numerical_trajectory_change_status":"not_established_without_counterfactual_replay",
          "recorded_pre_post_rows":trace["pre_post_rows"],"recorded_pre_snapshot_rows":trace["pre_snapshot_rows"],
          "recorded_post_snapshot_rows":trace["post_snapshot_rows"],"pre_post_key_coverage":trace["pre_post_key_coverage"],
          "integrity_checks":trace["checks"],
          "current_native_trajectory_compatibility":"not_ascertainable_without_replay_under_9e6f2acc",
          "receipt_hashes":{"metadata":pair[0],"trace":pair[1]},
        }
        owners.append(row)
    secondary={}
    for name,cache,ids,arm in (
        ("issue46_fresh_expert",a.issue46,range(441001,441129),"Expert"),
        ("issue47_fresh_expert",a.issue47,range(471001,471129),"Expert"),
        ("issue48_fresh_expert",a.issue48,range(481001,481129),"Expert"),
    ):
        secondary[name]=audit_secondary(cache,name,ids[0],EXPECTED[name[:7]],ids,arm)
    # Owner-level observed action classes are descriptive only. A non-use selection while a use was legal
    # is not relabeled as retained-to-battle-end, nor as a counterfactual.
    use_owners=sum(r["battle_potion_use_at_opportunity_decisions"]>0 for r in owners)
    nonuse_owners=sum(r["battle_potion_nonuse_while_use_legal_decisions"]>0 for r in owners)
    both_owners=sum(r["battle_potion_use_at_opportunity_decisions"]>0 and r["battle_potion_nonuse_while_use_legal_decisions"]>0 for r in owners)
    floors=collections.Counter(str(r["terminal_floor"]) for r in owners)
    bands=collections.Counter("F00-F08" if int(r["terminal_floor"])<=8 else ("F09-F16" if int(r["terminal_floor"])<=16 else "F17+") for r in owners)
    allchecks=collections.Counter()
    for row in owners:
        allchecks.update(row["integrity_checks"])
    row={
      "schema":"issue54-targeted-retained-complete-run-audit-v2","purpose":"STUDY_ONLY",
      "assessment_status":"INCOMPLETE_PUBLIC_CONTEXT",
      "assessment_reason":"Historical recorded inputs are present, but this targeted cache audit does not establish compatibility with the accepted normal-public projection; all complete runs use a pre-#52 Native pin.",
      "interpretation":"Descriptive retained-data inventory; observed potentially affected contexts do not prove a numerical trajectory change. This is not a policy-evaluation result, causal estimate, eligibility threshold, power claim, or approval to train.",
      "numerical_trajectory_change_status":"not_established_without_counterfactual_replay",
      "historical_policy_source":{
        "issue45_reviewed_manifest_sha256":mh["sha256"],"manifest_bytes":mh["bytes"],"manifest_files":len(manifest),
        "manifest_total_bytes":sum(int(x["bytes"]) for x in manifest),
        "source_metadata_sha256":source_hash["sha256"],"source_metadata_bytes":source_hash["bytes"],
        "native_commit":src.get("native_commit"),"native_extension_sha256":src.get("native_extension_sha256"),
        "stsrl_commit":src.get("stsrl_commit"),"retention_delete_date":"2026-11-09",
        "episode_count":len(owners),"unique_seed_owners":len({r["owner_key"] for r in owners}),
        "verified_on_arm_metadata_and_trace_files":2*len(owners),
        "verified_on_arm_metadata_and_trace_bytes":sum(
          r["receipt_hashes"][k]["bytes"] for r in owners for k in ("metadata","trace")
        ),
        "arm":"on","controller_name":"issue45_search_v2_400+expert_non_combat_v1",
        "controller_kind":"routed_run","controller_schema_version":1,
        "distinct_controller_config_fingerprints":len(policy_fingerprints),
        "controller_config_fingerprint_counts":dict(sorted(policy_fingerprints.items())),
        "distinct_source_pin_records":len(source_provenance),
        "producer_counts":dict(sorted(producer_counts.items())),
      },
      "fresh_expert_archive_availability":secondary,
      "issue45_on_rollup":{
        "episodes":len(owners),"terminal_complete_episodes":sum(r["terminal_outcome"] is not None for r in owners),
        "terminal_outcomes":dict(sorted(collections.Counter(str(r["terminal_outcome"]) for r in owners).items())),
        "terminal_floor_distribution":dict(sorted(floors.items(),key=lambda kv:int(kv[0]))),
        "terminal_floor_band_distribution":dict(sorted(bands.items())),
        "distinct_terminal_floors":len(floors),"start_act_distribution":dict(sorted(collections.Counter(str(r["start_act"]) for r in owners).items())),
        "max_act_distribution":dict(sorted(collections.Counter(str(r["max_act"]) for r in owners).items())),
        "episode_decisions":sum(r["decision_count"] for r in owners),
        "act1_decision_floor_bands":dict(sorted(sum_counters((r["act1_floor_decisions"] for r in owners)).items())),
      "act1_action_class_owner_counts":{
        "legal_use_opportunity":sum(r["act1_battle_potion_use_opportunity_decisions"]>0 for r in owners),
        "actual_use_at_opportunity":sum(r["act1_battle_potion_use_at_opportunity_decisions"]>0 for r in owners),
        "nonuse_while_use_legal":sum(r["act1_battle_potion_nonuse_while_use_legal_decisions"]>0 for r in owners),
        "both_use_and_nonuse":sum(r["act1_battle_potion_use_at_opportunity_decisions"]>0 and r["act1_battle_potion_nonuse_while_use_legal_decisions"]>0 for r in owners),
      },
        "owners_with_any_battle_potion_use_opportunity":sum(r["battle_potion_use_opportunity_decisions"]>0 for r in owners),
        "battle_potion_use_opportunity_decision_rows":sum(r["battle_potion_use_opportunity_decisions"] for r in owners),
        "act1_battle_potion_use_opportunity_decision_rows":sum(r["act1_battle_potion_use_opportunity_decisions"] for r in owners),
        "owners_with_act1_battle_potion_use_opportunity":sum(r["act1_battle_potion_use_opportunity_decisions"]>0 for r in owners),
        "owners_with_act1_battle_potion_use":sum(r["act1_battle_potion_use_at_opportunity_decisions"]>0 for r in owners),
        "act1_battle_potion_use_decision_rows":sum(r["act1_battle_potion_use_at_opportunity_decisions"] for r in owners),
        "owners_with_act1_nonuse_while_use_legal":sum(r["act1_battle_potion_nonuse_while_use_legal_decisions"]>0 for r in owners),
        "act1_nonuse_while_use_legal_decision_rows":sum(r["act1_battle_potion_nonuse_while_use_legal_decisions"] for r in owners),
        "owners_with_both_act1_use_and_nonuse_classes":sum(r["act1_battle_potion_use_at_opportunity_decisions"]>0 and r["act1_battle_potion_nonuse_while_use_legal_decisions"]>0 for r in owners),
        "owners_with_actual_use_at_legal_opportunity":use_owners,
        "actual_use_at_legal_opportunity_decision_rows":sum(r["battle_potion_use_at_opportunity_decisions"] for r in owners),
        "owners_with_nonuse_selection_while_use_legal":nonuse_owners,
        "nonuse_selection_rows_while_use_legal":sum(r["battle_potion_nonuse_while_use_legal_decisions"] for r in owners),
        "owners_observing_both_use_and_nonuse_classes":both_owners,
        "owners_with_reward_potion_take":sum(r["selected_potion_action_occurrences_by_class_and_screen"].get("REWARDS:take",0)>0 for r in owners),
        "owners_with_reward_potion_skip_at_offer_decision":sum(r["reward_potion_skip_decisions"]>0 for r in owners),
        "owners_with_reward_potion_overflow_discard_choice":sum(r["reward_potion_overflow_discard_choice_decisions"]>0 for r in owners),
        "owners_with_any_full_inventory_potion_offer_context":sum(r["reward_potion_full_inventory_offer_decisions"]>0 for r in owners),
        "battle_potion_uses_total":sum(r["potion_use_occurrences_by_screen"].get("BATTLE",0) for r in owners),
        "selected_potion_action_occurrences_by_class_and_screen":sum_counters((r["selected_potion_action_occurrences_by_class_and_screen"] for r in owners)),
        "legal_potion_action_occurrences_by_class":sum_counters((r["legal_potion_action_occurrences_by_class"] for r in owners)),
        "act1_selected_potion_action_occurrences_by_class_and_screen":sum_counters((r["act1_selected_potion_action_occurrences_by_class_and_screen"] for r in owners)),
        "act1_legal_potion_action_occurrences_by_class":sum_counters((r["act1_legal_potion_action_occurrences_by_class"] for r in owners)),
        "reward_potion_offer_decision_rows":sum(r["reward_potion_offer_decisions"] for r in owners),
        "reward_potion_take_decision_rows":sum(r["reward_potion_take_decisions"] for r in owners),
        "source_metadata_potion_offer_option_occurrences":meta_offer_option_occurrences,
        "reward_potion_skip_decision_rows":sum(r["reward_potion_skip_decisions"] for r in owners),
        "reward_potion_other_decision_rows":sum(r["reward_potion_other_decisions"] for r in owners),
        "reward_potion_full_inventory_offer_decision_rows":sum(r["reward_potion_full_inventory_offer_decisions"] for r in owners),
        "reward_potion_overflow_discard_option_occurrences":sum(r["reward_potion_overflow_discard_option_occurrences"] for r in owners),
        "reward_potion_overflow_discard_choice_decision_rows":sum(r["reward_potion_overflow_discard_choice_decisions"] for r in owners),
        "act1_reward_potion_offer_decision_rows":sum(r["act1_reward_potion_offer_decisions"] for r in owners),
        "act1_reward_potion_take_decision_rows":sum(r["act1_reward_potion_take_decisions"] for r in owners),
        "act1_reward_potion_skip_decision_rows":sum(r["act1_reward_potion_skip_decisions"] for r in owners),
        "act1_reward_potion_other_decision_rows":sum(r["act1_reward_potion_other_decisions"] for r in owners),
        "act1_reward_potion_full_inventory_offer_decision_rows":sum(r["act1_reward_potion_full_inventory_offer_decisions"] for r in owners),
        "act1_reward_potion_overflow_discard_option_occurrences":sum(r["act1_reward_potion_overflow_discard_option_occurrences"] for r in owners),
        "act1_reward_potion_overflow_discard_choice_decision_rows":sum(r["act1_reward_potion_overflow_discard_choice_decisions"] for r in owners),
        "selected_use_identity_mapped":sum(r["potion_use_identity_mapped_count"] for r in owners),
        "selected_use_identity_unmapped":sum(r["potion_use_identity_unmapped_count"] for r in owners),
        "selected_use_slot_label_identity_matches":sum(r["potion_use_slot_label_identity_match_count"] for r in owners),
        "selected_use_slot_label_identity_mismatches":sum(r["potion_use_slot_label_identity_mismatch_count"] for r in owners),
        "observed_semantic_event_counts":{
          "blood_potion_uses":sum(r["blood_potion_uses"] for r in owners),
          "blood_potion_uses_with_sacred_bark":sum(r["blood_potion_uses_with_sacred_bark"] for r in owners),
          "battle_blood_potion_uses":sum(r["blood_potion_battle_uses"] for r in owners),
          "battle_blood_potion_uses_with_sacred_bark":sum(r["blood_potion_battle_uses_with_sacred_bark"] for r in owners),
          "battle_blood_potion_uses_without_sacred_bark":sum(r["blood_potion_battle_uses_without_sacred_bark"] for r in owners),
          "battle_blood_bark_context_unobserved":sum(r["blood_potion_battle_bark_context_unobserved"] for r in owners),
          "smoke_bomb_uses":sum(r["smoke_bomb_uses"] for r in owners),
          "smoke_bomb_battle_uses":sum(r["smoke_bomb_battle_uses"] for r in owners),
          "smoke_bomb_escape_transitions":sum(r["smoke_bomb_escapes"] for r in owners),
          "victory_with_meat_on_bone_and_burning_blood":sum(r["victory_with_meat_on_bone_and_burning_blood"] for r in owners),
          "victory_face_of_cleric_to_rewards":sum(r["victory_face_of_cleric_to_rewards"] for r in owners),
          "sensitive_event_context_unobserved":sum(r["sensitive_event_context_unobserved"] for r in owners),
        },
        "observed_potentially_affected_event_occurrences":sum_counters((r["potentially_affected_event_counts"] for r in owners)),
        "owners_with_observed_potentially_affected_context":sum(bool(r["potentially_affected_event_counts"]) for r in owners),
        "observed_semantic_context_owner_counts":dict(sorted(collections.Counter(r["observed_semantic_context_class"] for r in owners).items())),
        "owners_with_unresolved_sensitive_context":sum(
          r["sensitive_event_context_unobserved"] > 0 or r["potion_use_identity_unmapped_count"] > 0 or r["potion_use_slot_label_identity_mismatch_count"] > 0
          for r in owners
        ),
        "recorded_pre_post_transition_rows":sum(r["recorded_pre_post_rows"] for r in owners),
        "recorded_decision_rows":sum(r["decision_count"] for r in owners),
        "normal_public_projection_compatibility":"not independently established by this targeted cache check; raw snapshots were not exported",
      },
      "integrity_checks":dict(sorted(allchecks.items())),
      "owner_level_aggregates":sorted(owners,key=lambda r:r["owner_key"]),
      "raw_source_values_exported":False,"raw_action_identities_or_snapshots_exported":False,
      "source_retention_extended":False,"model_ready":False,
      "eligibility_threshold_or_power_claim":"none; withdrawn numeric gate is not applied",
      "recommendation":"Use #45 only as historical, descriptive, owner-distinct data with explicit old Native and Search/Expert provenance. No current-policy or post-#52 outcome compatibility is established. The sealed #46–#48 Expert candidate manifest entries do not add usable raw owners while their files are absent.",
    }
    a.out.parent.mkdir(parents=True,exist_ok=True)
    a.out.write_text(json.dumps(row,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    cpu=time.process_time()-start
    # Preserve all already-recorded Builder and Reviewer charges when this bounded repair is rerun.
    previous_execution = read_json(a.execution) if a.execution.is_file() else {}
    prior = float(previous_execution.get("builder_total_charged_upper_seconds", 419.829365))
    reviewer_charge = max(float(previous_execution.get("reviewer_charged_upper_seconds", 0)), 200.25)
    current_admin_charge = 25.0
    execution={"purpose":"STUDY_ONLY","phase":"targeted retained complete-run source audit",
      "current_process_cpu_seconds":round(cpu,6),"current_builder_admin_charged_upper_seconds":current_admin_charge,
      "prior_builder_charged_upper_seconds":prior,
      "prior_charge_includes_inventory_setup_failed_diagnostics_and_targeted_source_inspection":True,
      "cumulative_before_current_run_all_participants_charged_upper_seconds":round(prior+reviewer_charge,6),
      "builder_total_charged_upper_seconds":round(prior+cpu+current_admin_charge,6),
      "reviewer_charged_upper_seconds":reviewer_charge,
      "cumulative_all_participants_charged_upper_seconds":round(prior+cpu+current_admin_charge+reviewer_charge,6),
      "remaining_all_in_hard_cap_seconds":round(2000-(prior+cpu+current_admin_charge+reviewer_charge),6),
      "all_in_hard_cap_seconds":2000,
      "reviewer_reserved_minimum_seconds":600,"builder_maximum_seconds":1400,
      "remaining_builder_cap_seconds":round(1400-(prior+cpu+current_admin_charge),6),
      "new_complete_seeded_games":0,"search_value_training":0,
      "continuation_or_label_generation":0,"policy_variants":0,"permanent_core_additions":0,
      "raw_artifact_copies":0,"review_delete_date":"2026-11-14","source_retention_extended":False}
    a.execution.write_text(json.dumps(execution,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps({"issue45_owners":len(owners),"policy_config_fingerprints":len(policy_fingerprints),
      "issue45_rollup":row["issue45_on_rollup"],"secondary_availability":secondary,
      "integrity":row["integrity_checks"],"execution":execution},indent=2,sort_keys=True))

def sum_counters(iterable):
    total=collections.Counter()
    for x in iterable: total.update(x)
    return total

if __name__ == "__main__": main()
