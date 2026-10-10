from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
from pathlib import Path
from typing import Any


CASE_TRACE_IDS = {"A": 9, "B": 11, "C": 14, "D": 17, "E": 21, "F": 23}
FILES = {
    "natural_evidence": ("t087-natural-evidence.json", "t087-natural-evidence-v1"),
    "blind_audit_bundle": ("t087-blind-audit-bundle.json", "t087-blind-audit-bundle-v1"),
    "blind_audit_hidden_provenance": (
        "t087-blind-audit-hidden-provenance.json", "t087-blind-audit-hidden-provenance-v1"
    ),
    "natural_run_manifest": ("t087-natural-run-manifest.json", "t087-natural-run-manifest-v1"),
}
DECK_FIELDS = ("id_label", "name", "type", "rarity", "upgraded", "upgrade_count")
HAND_FIELDS = ("name", "type", "cost", "cost_for_turn", "upgraded", "upgrade_count", "playable", "requires_target")
RELIC_FIELDS = ("id_label", "name", "counter")
POTION_FIELDS = ("id_label", "name")
PLAYER_FIELDS = (
    "current_hp", "max_hp", "energy", "energy_per_turn", "block", "strength", "dexterity",
    "artifact", "focus", "vulnerable", "weak", "frail",
)
MONSTER_FIELDS = ("name", "current_hp", "max_hp", "block", "intent", "current_move", "intent_category", "alive")
FORBIDDEN_KEYS = {
    "outcome", "battle_outcome", "selection_identity", "trace_id", "provenance", "seed",
    "sampler_seed", "rng", "draw_pile", "battle_draw_pile", "search_trace", "oracle",
    "private", "next_snapshot_raw", "snapshot_raw",
}
PRIOR_NOTES = {
    "A": ("poor", "血量充足；当时可见的其他资源和起手一般；若牌组有更高价值牌或消耗体系，仍可能有帮助。", "HP ample, other visible resources/card opening unimpressive; possible with higher-value or exhaust support."),
    "B": ("adequate", "血量健康；遗物价值偏低；药水可能有用；Carnage 未升级。", "HP healthy, low-value relics, potions potentially useful; Carnage unupgraded."),
    "C": ("adequate", "第 2 回合按百分比造成伤害后，治疗药水可能尤其有用；Red Skull、Orichalcum、Anger 提供了可用选项；当时完整牌组仍未知。", "Healing potions may be especially useful after turn-2 percentage-HP damage; Red Skull/Orichalcum/Anger provide useful options; full deck still unknown."),
    "D": ("poor", "资源较少，起手偏弱；在当时可见信息中没有明显补救手段。", "Low resources, weak starting hand, no apparent rescue in available view."),
    "E": ("adequate", "判断取决于药水是否可用、牌组质量和前几回合表现；Disarm 可能很重要。", "Conditional on usable potions, deck quality and early turns; Disarm potentially important."),
    "F": ("poor", "入场时血量极低；只有在牌组合适时，Fire Breathing / Snecko Oil 的恢复情形才可能出现，且概率被认为较低。", "Extreme entry HP deficit, with only a low-probability Fire Breathing/Snecko Oil recovery scenario under suitable deck."),
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def file_identity(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"file": path.name, "size_bytes": size, "sha256": digest.hexdigest()}


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def stream_rows(path: Path, root_metadata: dict[str, Any], state: dict[str, Any]):
    """Read a top-level JSON rows array incrementally; retain only requested rows."""
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as stream:
        buffer = stream.read(64 * 1024)
        match = re.search(r'"rows"\s*:\s*\[', buffer)
        while match is None and len(buffer) < 1024 * 1024:
            more = stream.read(64 * 1024)
            if not more:
                break
            buffer += more
            match = re.search(r'"rows"\s*:\s*\[', buffer)
        if match is None:
            raise ValueError("natural evidence has no top-level rows array")
        cursor, count = match.end(), 0
        while True:
            while cursor < len(buffer) and (buffer[cursor].isspace() or buffer[cursor] == ","):
                cursor += 1
            if cursor >= len(buffer):
                more = stream.read(64 * 1024)
                if not more:
                    raise ValueError("natural evidence ended before the rows array closed")
                buffer, cursor = more, 0
                continue
            if buffer[cursor] == "]":
                tail = (buffer[cursor + 1:] + stream.read()).strip()
                if tail == "}":
                    root_metadata.update({})
                else:
                    if tail.startswith(","):
                        tail = tail[1:].lstrip()
                    if tail.startswith("}"):
                        root_metadata.update({})
                    elif tail.endswith("}"):
                        root_metadata.update(json.loads("{" + tail[:-1] + "}"))
                    else:
                        raise ValueError("natural evidence top-level metadata is malformed")
                state["raw_rows_seen"] = count
                return
            try:
                row, end = decoder.raw_decode(buffer, cursor)
            except json.JSONDecodeError:
                more = stream.read(64 * 1024)
                if not more:
                    raise ValueError("natural evidence contains an incomplete row")
                buffer, cursor = buffer[cursor:] + more, 0
                continue
            if not isinstance(row, dict):
                raise ValueError("natural evidence row is not an object")
            count += 1
            yield row
            cursor = end
            if cursor > 64 * 1024:
                buffer, cursor = buffer[cursor:], 0


def project_items(value: Any, fields: tuple[str, ...]) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    return [{key: item[key] for key in fields if key in item} if isinstance(item, dict) else {} for item in value]


def project_entry(snapshot: dict[str, Any], label: str) -> tuple[dict[str, Any], dict[str, bool]]:
    player = snapshot.get("battle_player") if isinstance(snapshot.get("battle_player"), dict) else {}
    hp = snapshot.get("battle_player_hp", snapshot.get("cur_hp", snapshot.get("current_hp")))
    max_hp = player.get("max_hp", snapshot.get("max_hp"))
    deck = project_items(snapshot.get("deck"), DECK_FIELDS)
    relics = project_items(snapshot.get("relics"), RELIC_FIELDS)
    potions = project_items(snapshot.get("potions"), POTION_FIELDS)
    hand = project_items(snapshot.get("battle_hand"), HAND_FIELDS)
    monsters = project_items(snapshot.get("battle_monsters"), MONSTER_FIELDS)
    known_identities = [item.get("id_label") or item.get("name") for item in (deck or []) if item.get("id_label") or item.get("name")]
    entry = {
        "case": label,
        "context": {"act": snapshot.get("act"), "floor": snapshot.get("floor_num", snapshot.get("floor")),
                    "room_type": snapshot.get("room_type"), "boss_encounter": snapshot.get("encounter_id")},
        "resources": {"starting_hp": hp, "max_hp": max_hp, "gold": snapshot.get("gold"),
                      "potion_count": snapshot.get("potion_count"), "potion_capacity": snapshot.get("potion_capacity"),
                      "potions": potions, "relics": relics},
        "full_deck": {"recorded_copy_count": len(deck) if deck is not None else None,
                      "known_distinct_identity_count": len(set(known_identities)) if deck is not None else None,
                      "cards": deck},
        "first_turn": {"turn": snapshot.get("battle_turn"),
                       "energy": snapshot.get("battle_player_energy", player.get("energy")),
                       "block": snapshot.get("battle_player_block", player.get("block")),
                       "hand": hand, "enemies": monsters},
    }
    coverage = {
        "act": "act" in snapshot,
        "floor": "floor_num" in snapshot or "floor" in snapshot,
        "room_type": "room_type" in snapshot,
        "boss_encounter": "encounter_id" in snapshot,
        "starting_hp": hp is not None,
        "max_hp": max_hp is not None,
        "deck": deck is not None,
        "relics": relics is not None,
        "potions": potions is not None,
        "potion_count": "potion_count" in snapshot,
        "potion_capacity": "potion_capacity" in snapshot,
        "first_turn": "battle_hand" in snapshot and "battle_turn" in snapshot,
        "first_turn_energy": snapshot.get("battle_player_energy", player.get("energy")) is not None,
        "first_turn_block": snapshot.get("battle_player_block", player.get("block")) is not None,
        "first_turn_hand": hand is not None,
        "first_turn_enemies": monsters is not None,
        "first_turn_enemy_intents": monsters is not None and all("intent" in item or "current_move" in item for item in monsters),
        "deck_upgrade_status": deck is not None and all("upgraded" in item or "upgrade_count" in item for item in deck),
    }
    uncertainties = [f"{key}_not_recorded" for key, present in coverage.items() if not present]
    if deck is not None:
        for index, card in enumerate(deck):
            if not (card.get("name") or card.get("id_label")):
                uncertainties.append(f"deck_card_{index + 1}_identity_unknown")
            if "upgraded" not in card and "upgrade_count" not in card:
                uncertainties.append(f"deck_card_{index + 1}_upgrade_status_unknown")
    for field, items in (("relic", relics), ("potion", potions), ("hand_card", hand), ("enemy", monsters)):
        for index, item in enumerate(items or []):
            if not (item.get("name") or item.get("id_label")):
                uncertainties.append(f"{field}_{index + 1}_identity_unknown")
            if field == "hand_card" and "playable" not in item:
                uncertainties.append(f"hand_card_{index + 1}_playable_status_unknown")
            if field == "relic" and "counter" not in item:
                uncertainties.append(f"relic_{index + 1}_counter_not_recorded")
            if field == "enemy":
                for value in ("current_hp", "max_hp", "block", "intent"):
                    if value not in item and not (value == "intent" and ("current_move" in item or "intent_category" in item)):
                        uncertainties.append(f"enemy_{index + 1}_{value}_not_recorded")
    entry["uncertainty_flags"] = uncertainties
    return entry, coverage


def project_battle_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    player = snapshot.get("battle_player") if isinstance(snapshot.get("battle_player"), dict) else {}
    return {
        "turn": snapshot.get("battle_turn"),
        "player": {key: player[key] for key in PLAYER_FIELDS if key in player},
        "hand": project_items(snapshot.get("battle_hand"), HAND_FIELDS),
        "enemies": project_items(snapshot.get("battle_monsters"), MONSTER_FIELDS),
        "potions": project_items(snapshot.get("battle_potions"), POTION_FIELDS),
    }


def project_action(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    allowed = ("action_id", "kind", "label", "occurrence", "stable_id")
    return {key: value[key] for key in allowed if key in value}


def reject_forbidden(value: Any, path: str = "$" ) -> None:
    if isinstance(value, dict):
        bad = FORBIDDEN_KEYS.intersection(value)
        if bad:
            raise ValueError(f"private/source field in public packet at {path}: {sorted(bad)}")
        for key, child in value.items():
            reject_forbidden(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_forbidden(child, f"{path}[{index}]")


def canonical_sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


STYLE = """
body{max-width:1100px;margin:24px auto;padding:0 16px;font:16px/1.55 system-ui,sans-serif;color:#202631;background:#f5f7fa}
h1,h2,h3{line-height:1.25}.notice{background:#fff4d6;border:1px solid #e5ca78;border-radius:8px;padding:12px 16px}
.case{background:white;border:1px solid #d9dee7;border-radius:10px;padding:16px;margin:16px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.panel{background:#f8fafc;border-radius:8px;padding:12px}.muted{color:#596273}.missing{color:#9c3b26;font-weight:600}
table{border-collapse:collapse;width:100%;font-size:.93em}th,td{border-bottom:1px solid #e0e4ea;padding:6px 8px;text-align:left;vertical-align:top}
a.button{display:inline-block;background:#2457a7;color:white;border-radius:7px;padding:10px 14px;text-decoration:none}code{overflow-wrap:anywhere}
@media(max-width:600px){body{margin:12px auto;padding:0 10px}}
"""


def h(value: Any) -> str:
    if value is None:
        return '<span class="missing">未记录</span>'
    if isinstance(value, bool):
        return "是" if value else "否"
    return html.escape(str(value))


def list_html(items: Any) -> str:
    if items is None:
        return '<span class="missing">原始快照未记录</span>'
    if not items:
        return '<span class="muted">原始快照记录为空</span>'
    return "<ul>" + "".join(f"<li>{h(item.get('name') or item.get('id_label') or '身份未记录')}</li>" for item in items) + "</ul>"


def entry_html(cases: list[dict[str, Any]]) -> str:
    sections = []
    for case in cases:
        context, resources, first = case["context"], case["resources"], case["first_turn"]
        cards = [] if case["full_deck"]["cards"] is None else case["full_deck"]["cards"]
        deck_rows = []
        for i, card in enumerate(cards, 1):
            upgrade = card.get("upgrade_count")
            if upgrade is None:
                upgrade = "已升级" if card.get("upgraded") is True else "未升级" if card.get("upgraded") is False else "升级信息未记录"
            deck_rows.append(f"<tr><td>{i}</td><td>{h(card.get('name') or card.get('id_label') or '未知身份')}</td><td>{h(card.get('type'))}</td><td>{h(card.get('rarity'))}</td><td>{h(upgrade)}</td></tr>")
        deck = ('<span class="missing">完整牌组未记录</span>' if case["full_deck"]["cards"] is None else
                '<p>记录副本数：{}；已知独特身份数：{}。全量显示，未截断。</p><table><thead><tr><th>#</th><th>卡牌</th><th>类型</th><th>稀有度</th><th>升级</th></tr></thead><tbody>{}</tbody></table>'.format(
                    h(case["full_deck"]["recorded_copy_count"]), h(case["full_deck"]["known_distinct_identity_count"]), "".join(deck_rows)))
        hand_rows = []
        for card in first["hand"] or []:
            name = card.get("name") or card.get("id_label") or "身份未记录"
            upgrade = card.get("upgrade_count", card.get("upgraded", "未记录"))
            hand_rows.append(f"<li>{h(name)}；费用 {h(card.get('cost_for_turn', card.get('cost')))}；牌面可用标记 {h(card.get('playable'))}；升级 {h(upgrade)}</li>")
        enemy_rows = []
        for enemy in first["enemies"] or []:
            move = enemy.get("intent", enemy.get("current_move"))
            category = f"（{h(enemy['intent_category'])}）" if "intent_category" in enemy else ""
            enemy_rows.append("<li>{}：HP {}/{}；格挡 {}；公开动作字段 {} {}</li>".format(h(enemy.get("name")), h(enemy.get("current_hp")), h(enemy.get("max_hp")), h(enemy.get("block")), h(move), category))
        relics = []
        for relic in resources["relics"] or []:
            suffix = f"；计数器 {h(relic['counter'])}" if "counter" in relic else ""
            relics.append(f"<li>{h(relic.get('name') or relic.get('id_label') or '身份未记录')}{suffix}</li>")
        sections.append(f"""
<section class="case"><h2>案例 {h(case['case'])}</h2><div class="grid">
<div class="panel"><h3>战斗入口</h3><p>Act {h(context['act'])}；Floor {h(context['floor'])}</p><p>房间 {h(context['room_type'])}；遭遇 {h(context['boss_encounter'])}</p><p>入场 HP {h(resources['starting_hp'])}/{h(resources['max_hp'])}</p><p>金币 {h(resources['gold'])}</p></div>
<div class="panel"><h3>药水与遗物</h3><p>药水槽 {h(resources['potion_count'])}/{h(resources['potion_capacity'])}</p><p>药水库存：{list_html(resources['potions'])}</p><p>遗物：{'<ul>'+''.join(relics)+'</ul>' if relics else ('<span class="missing">未记录</span>' if resources['relics'] is None else '<span class="muted">记录为空</span>')}</p></div>
<div class="panel"><h3>首个记录回合（入口状态）</h3><p>回合 {h(first['turn'])}；能量 {h(first['energy'])}；格挡 {h(first['block'])}</p><p>手牌：{'<ul>'+''.join(hand_rows)+'</ul>' if hand_rows else ('<span class="missing">未记录</span>' if first['hand'] is None else '<span class="muted">记录为空</span>')}</p><p>敌人：{'<ul>'+''.join(enemy_rows)+'</ul>' if enemy_rows else ('<span class="missing">未记录</span>' if first['enemies'] is None else '<span class="muted">记录为空</span>')}</p></div>
</div><div class="panel"><h3>完整持久牌组</h3>{deck}<p class="muted">缺失/不确定字段：{h(', '.join(case['uncertainty_flags']) if case['uncertainty_flags'] else '无')}</p></div></section>""")
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>T087 Boss 入口公开状态</title><style>""" + STYLE + """</style><body>
<h1>T087 六个 Boss 案例：公开入口信息</h1><div class="notice"><strong>入口优先。</strong>本页只显示战斗前可见状态，不显示已选动作、终局结果、源记录 ID 或 Search 内部轨迹。本包只整理现有数据；没有重放、模拟或新增人工标注。</div>
""" + "".join(sections) + """
<section class="case"><h2>后续查看</h2><p>请先检查六例的完整牌组和入口公开状态。保留的 T087 步骤没有记录权威的完整合法动作集合；手牌上的 playable 标记也不是完整合法动作集合。</p>
<label><input id="done" type="checkbox"> 我已检查入口信息</label><p id="links" hidden><a class="button" href="timeline.html">查看 Search 动作与公开状态时间线</a> <a class="button" href="prior-notes.html">查看旧版临时资源印象</a></p></section>
<script>document.getElementById("done").addEventListener("change",e=>document.getElementById("links").hidden=!e.target.checked)</script></body></html>
"""


def timeline_html(cases: list[dict[str, Any]]) -> str:
    sections = []
    for case in cases:
        rows = []
        for step in case["steps"]:
            state, player, action = step["public_state"], step["public_state"]["player"] or {}, step["chosen_action"] or {}
            hand = state["hand"]
            enemies = state["enemies"]
            hand_text = ", ".join(str(c.get("name") or c.get("id_label") or "身份未记录") for c in (hand or [])) or ("未记录" if hand is None else "空")
            enemy_text = "；".join("{} HP {}/{} 格挡 {} current_move/intent {} {}".format(e.get("name") or "未记录", e.get("current_hp"), e.get("max_hp"), e.get("block"), e.get("intent", e.get("current_move", "未记录")), e.get("intent_category", "")) for e in (enemies or [])) or ("未记录" if enemies is None else "空")
            statuses = "力量 {}；敏捷 {}；人工制品 {}；集中 {}；易伤 {}；虚弱 {}；脆弱 {}".format(
                h(player.get("strength")), h(player.get("dexterity")), h(player.get("artifact")), h(player.get("focus")),
                h(player.get("vulnerable")), h(player.get("weak")), h(player.get("frail")))
            rows.append("<tr><td>{}</td><td>{}</td><td>HP {}/{}；能量 {}；格挡 {}；{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                h(step.get("step_index")), h(state.get("turn")), h(player.get("current_hp")), h(player.get("max_hp")), h(player.get("energy")), h(player.get("block")), statuses,
                h(hand_text), h(enemy_text), h(action.get("label") or action.get("kind") or "动作身份未记录")))
        rows_html = "".join(rows)
        sections.append(f'<section class="case"><h2>案例 {h(case["case"])}</h2><p>每步显示所选动作前的公开状态和控制器实际选择。完整法律动作备选集合未记录。</p><table><thead><tr><th>步骤</th><th>回合</th><th>玩家公开状态</th><th>手牌</th><th>敌人公开状态</th><th>实际选择的动作</th></tr></thead><tbody>{rows_html}</tbody></table></section>')
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>T087 Search 动作时间线</title><style>""" + STYLE + """</style><body><h1>T087 Search 动作时间线</h1>
<div class="notice">本视图只显示每步原始公开 snapshot 的 allowlist 投影：手牌、能量、可见状态、敌人信息与被选动作；不显示终局结果。原 Search 动作空间为 <code>initial_no_potions</code>，药水库存不代表药水是 Search 可选动作。手牌 playable 是卡牌级标记，不等同于完整目标/卡牌选择合法动作集合。</div>
""" + "".join(sections) + """
<section class="case"><h2>可选下一步</h2><label><input id="done" type="checkbox"> 我已检查动作与逐步公开状态</label><p id="next" hidden><a class="button" href="outcomes.html">可选：查看终局结果</a></p></section><script>document.getElementById("done").addEventListener("change",e=>document.getElementById("next").hidden=!e.target.checked)</script></body></html>
"""


def outcomes_html(cases: list[dict[str, Any]]) -> str:
    sections = []
    for item in cases:
        result = item["result"]
        enemies = result.get("enemy_hp_summary")
        enemy_summary = "；".join("{} HP {}/{}".format(e.get("name", "未记录"), e.get("current_hp", "未记录"), e.get("max_hp", "未记录")) for e in (enemies or [])) or "未记录"
        sections.append(f'<section class="case"><h2>案例 {h(item["case"])}</h2><p>战斗终局：{h(result.get("battle_outcome"))}</p><p>游戏终局字段（若记录）：{h(result.get("outcome"))}</p><p>末步状态：{h(result.get("screen_state"))}；玩家 HP：{h(result.get("player_hp"))}/{h(result.get("max_hp"))}；末步敌人 HP：{h(enemy_summary)}</p></section>')
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>T087 终局结果（可选）</title><style>""" + STYLE + """</style><body><h1>终局结果（可选披露）</h1><div class="notice">该页仅在检查入口信息和动作时间线后按需查看。结果来自原始模拟记录，不是对牌组可赢性或控制器正确性的裁定；不得回填为旧版资源印象标签。</div>""" + "".join(sections) + "</body></html>\n"


def main() -> int:
    default_root = (Path(r"D:\DeadlyCatCoding\STSRL\artifacts\t087-final-8d7e44-20260911") if os.name == "nt" else Path("/mnt/d/DeadlyCatCoding/STSRL/artifacts/t087-final-8d7e44-20260911"))
    parser = argparse.ArgumentParser(description="Rehydrate T087 A-F public Boss packets without simulation or replay.")
    parser.add_argument("--artifact-root", type=Path, default=default_root)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    root, out = args.artifact_root, args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "run-state.json"
    state = {"schema_id": "issue-37-run-state-v1", "status": "running", "stage": "artifact_identity_check",
             "updated_utc": now(), "replay_performed": False, "simulation_performed": False,
             "human_annotations_started": False}
    write_json(state_path, state)
    try:
        retention_path = root / "t087-retention-manifest.json"
        retention = read_json(retention_path)
        if retention.get("schema_id") != "t087-retention-manifest-v1":
            raise ValueError("unexpected retained T087 manifest schema")
        source_artifacts = {}
        for key, (filename, schema) in FILES.items():
            path = root / filename
            identity = file_identity(path)
            ref = retention["artifact_references"][key]
            if identity["sha256"] != ref["sha256"] or identity["size_bytes"] != ref["size_bytes"] or ref["schema_id"] != schema:
                raise ValueError(f"retained artifact identity/schema mismatch: {filename}")
            source_artifacts[key] = {**identity, "schema_id": schema}
        source_artifacts["retention_manifest"] = {**file_identity(retention_path), "schema_id": retention["schema_id"]}

        bundle = read_json(root / FILES["blind_audit_bundle"][0])
        hidden = read_json(root / FILES["blind_audit_hidden_provenance"][0])
        run_manifest = read_json(root / FILES["natural_run_manifest"][0])
        if bundle.get("schema_id") != FILES["blind_audit_bundle"][1] or hidden.get("schema_id") != FILES["blind_audit_hidden_provenance"][1] or run_manifest.get("schema_id") != FILES["natural_run_manifest"][1] or run_manifest.get("task_id") != "T087":
            raise ValueError("retained T087 schema or task identity mismatch")
        traces, mapping_rows = bundle.get("traces"), hidden.get("trace_map")
        if not isinstance(traces, list) or not isinstance(mapping_rows, list):
            raise ValueError("blind bundle or hidden mapping is malformed")
        blind = {row.get("trace_id"): row for row in traces if isinstance(row, dict)}
        hidden_by_id = {row.get("trace_id"): row for row in mapping_rows if isinstance(row, dict)}
        if len(blind) != len(traces) or len(hidden_by_id) != len(mapping_rows) or set(blind) != set(range(24)) or set(hidden_by_id) != set(range(24)):
            raise ValueError("T087 trace IDs are not the exact unique range 0..23")
        source_ids = {case: hidden_by_id[trace_id].get("selection_identity") for case, trace_id in CASE_TRACE_IDS.items()}
        if any(not isinstance(identity, str) or not identity for identity in source_ids.values()) or len(set(source_ids.values())) != 6:
            raise ValueError("A-F do not map to six distinct exact source identities")

        state.update({"stage": "streaming_exact_source_rows", "updated_utc": now()})
        write_json(state_path, state)
        root_metadata: dict[str, Any] = {}
        selected: dict[str, dict[str, Any]] = {}
        id_to_case = {identity: case for case, identity in source_ids.items()}
        for row in stream_rows(root / FILES["natural_evidence"][0], root_metadata, state):
            case = id_to_case.get(row.get("selection_identity"))
            if case:
                if case in selected:
                    raise ValueError(f"duplicate exact source row for case {case}")
                selected[case] = row
        if len(selected) != 6:
            raise ValueError(f"exact retained source row missing for cases: {sorted(set(CASE_TRACE_IDS)-set(selected))}")
        if root_metadata.get("schema_id") not in (None, FILES["natural_evidence"][1]):
            raise ValueError("embedded natural-evidence schema disagrees with retained manifest")

        state.update({"stage": "cross_check_and_public_projection", "updated_utc": now()})
        write_json(state_path, state)
        entries, timelines, outcomes, coverage_by_case = [], [], [], {}
        legal_fields_by_case = {}
        missing_required = {}
        for case, trace_id in CASE_TRACE_IDS.items():
            row, blind_steps = selected[case], blind[trace_id].get("steps")
            steps = row.get("action_trace")
            if not isinstance(steps, list) or not steps or not isinstance(blind_steps, list) or len(steps) != len(blind_steps):
                raise ValueError(f"case {case} raw trace is missing or differs in length from blind bundle")
            for raw_step, blind_step in zip(steps, blind_steps, strict=True):
                if raw_step.get("public_state") != blind_step.get("public_state") or raw_step.get("chosen_action_identity") != blind_step.get("chosen_action_identity"):
                    raise ValueError(f"case {case} public state/action identity does not match blind trace")
            first_snapshot = steps[0].get("snapshot_raw")
            if not isinstance(first_snapshot, dict):
                raise ValueError(f"case {case} has no first-step raw snapshot")
            entry, coverage = project_entry(first_snapshot, case)
            entries.append(entry)
            coverage_by_case[case] = coverage
            required = ("starting_hp", "max_hp", "deck", "relics", "potions", "first_turn", "first_turn_enemies")
            missing = [key for key in required if not coverage[key]]
            if missing:
                missing_required[case] = missing

            projected_steps, action_keys = [], set()
            for step in steps:
                action_keys.update(step.keys())
                snapshot = step.get("snapshot_raw")
                if not isinstance(snapshot, dict):
                    raise ValueError(f"case {case} has a decision step without raw snapshot")
                projected_steps.append({"step_index": step.get("step_index"), "public_state": project_battle_state(snapshot),
                                        "chosen_action": project_action(step.get("chosen_action_identity"))})
            timelines.append({"case": case, "steps": projected_steps})
            legal_fields_by_case[case] = sorted(action_keys & {"legal_actions", "tactical_legal_actions", "eligible_actions", "eligible_action_indices"})
            terminal = steps[-1].get("next_snapshot_raw")
            terminal = terminal if isinstance(terminal, dict) else {}
            final_player = terminal.get("battle_player") if isinstance(terminal.get("battle_player"), dict) else {}
            monsters = terminal.get("battle_monsters")
            enemy_hp = [{key: monster[key] for key in ("name", "current_hp", "max_hp") if key in monster}
                        for monster in monsters if isinstance(monster, dict)] if isinstance(monsters, list) else []
            outcomes.append({"case": case, "result": {
                "battle_outcome": terminal.get("battle_outcome"), "outcome": terminal.get("outcome"),
                "screen_state": terminal.get("screen_state"),
                "player_hp": terminal.get("battle_player_hp", final_player.get("current_hp", terminal.get("cur_hp"))),
                "max_hp": final_player.get("max_hp", terminal.get("max_hp")), "enemy_hp_summary": enemy_hp,
            }})

        if missing_required:
            state.update({"status": "RAW_FIELD_MISSING", "stage": "required_public_start_fields", "missing_fields_by_case": missing_required, "updated_utc": now()})
            write_json(state_path, state)
            print(json.dumps({"status": state["status"], "missing_fields_by_case": missing_required}, ensure_ascii=False))
            return 2
        if any(legal_fields_by_case.values()):
            raise ValueError("raw steps unexpectedly contain legal-action fields; inspect before claiming alternatives absent")
        for entry in entries:
            reject_forbidden(entry)
        for timeline in timelines:
            reject_forbidden(timeline)
        if any(not step["chosen_action"] for case in timelines for step in case["steps"]):
            raise ValueError("a selected timeline step lacks its chosen action identity")

        binding = {key: source_artifacts[key] for key in source_artifacts}
        source_mapping = [{"case": case, "original_trace_id": trace_id, "selection_identity": source_ids[case], "source_row_join_field": "selection_identity"} for case, trace_id in CASE_TRACE_IDS.items()]
        run_native = run_manifest.get("native_identity")
        lineage = retention["native_lineage_source_verification"]
        private_map = {
            "schema_id": "issue-37-private-source-map-v1", "binding_artifacts": binding,
            "mapping": source_mapping, "mapping_sha256": canonical_sha(source_mapping),
            "binding_sha256": canonical_sha({"binding_artifacts": binding, "mapping": source_mapping}),
            "native_source_identity": {
                "natural_run_manifest_native_identity": run_native,
                "retention_manifest_historical_identity": lineage.get("historical_native_identity"),
                "retention_manifest_verified_ancestor": lineage.get("current_native_identity"),
                "lineage_verifier_result": lineage.get("lineage_verifier", {}).get("result"),
                "t087_implementation_run_head": retention.get("implementation_run_head"),
            },
            "provenance_is_separate_from_human_pages": True, "replay_performed": False,
        }
        allowlist = {
            "schema_id": "issue-37-public-field-allowlist-v1",
            "entry": {
                "context": {"act": "snapshot_raw.act", "floor": "snapshot_raw.floor_num", "room_type": "snapshot_raw.room_type", "boss_encounter": "snapshot_raw.encounter_id"},
                "resources": {"starting_hp": "snapshot_raw.battle_player_hp (fallback cur_hp/current_hp)",
                              "max_hp": "snapshot_raw.battle_player.max_hp (fallback snapshot_raw.max_hp)",
                              "gold": "snapshot_raw.gold", "potion_count": "snapshot_raw.potion_count", "potion_capacity": "snapshot_raw.potion_capacity"},
                "deck": {field: f"snapshot_raw.deck[*].{field}" for field in DECK_FIELDS},
                "relics": {field: f"snapshot_raw.relics[*].{field}" for field in RELIC_FIELDS},
                "potions": {field: f"snapshot_raw.potions[*].{field}" for field in POTION_FIELDS},
                "first_turn": {"turn": "snapshot_raw.battle_turn", "energy": "snapshot_raw.battle_player_energy (fallback battle_player.energy)",
                               "block": "snapshot_raw.battle_player_block (fallback battle_player.block)",
                               "hand": {field: f"snapshot_raw.battle_hand[*].{field}" for field in HAND_FIELDS},
                               "enemies": {field: f"snapshot_raw.battle_monsters[*].{field}" for field in MONSTER_FIELDS}},
            },
            "decision_timeline": {"player": {field: f"snapshot_raw.battle_player.{field}" for field in PLAYER_FIELDS},
                                  "hand": {field: f"snapshot_raw.battle_hand[*].{field}" for field in HAND_FIELDS},
                                  "enemies": {field: f"snapshot_raw.battle_monsters[*].{field}" for field in MONSTER_FIELDS},
                                  "potions": {field: f"snapshot_raw.battle_potions[*].{field}" for field in POTION_FIELDS},
                                  "chosen_action": "action_trace[*].chosen_action_identity (action_id/kind/label/occurrence/stable_id)"},
            "legal_action_alternatives": "not recorded in retained action_trace; not reconstructed",
            "explicitly_excluded": sorted(FORBIDDEN_KEYS),
        }
        coverage = {"schema_id": "issue-37-public-field-coverage-v1", "cases": list(CASE_TRACE_IDS),
                    "source_rows_seen": state.get("raw_rows_seen"), "exact_source_rows_found": len(selected),
                    "all_step_public_state_and_action_ids_match_blind_bundle": True,
                    "replay_performed": False, "simulation_performed": False, "new_annotations_started": False,
                    "legal_action_alternatives": "not recorded", "legal_action_fields_seen_by_case": legal_fields_by_case,
                    "field_coverage_by_case": coverage_by_case,
                    "multi_decision_trace_steps_by_case": {case: len(t["steps"]) for case, t in ((x["case"], x) for x in timelines)}}
        prior = {"schema_id": "issue-37-prior-impressions-v1", "status": "historical_resource_impressions_only",
                 "not_labels": ["start_winnability", "dominant_failure_source", "adjudicated_outcome"],
                 "note": "Issue-recorded first-pass impressions from an incomplete earlier view; not updated or treated as ground truth.",
                 "cases": [{"case": case, "label": PRIOR_NOTES[case][0], "zh": PRIOR_NOTES[case][1], "original_note": PRIOR_NOTES[case][2]} for case in CASE_TRACE_IDS]}
        packets = {
            "entry-view.json": {"schema_id": "issue-37-entry-view-v1", "disclosure_stage": "entry_only", "cases": entries, "source_ids_excluded": True, "terminal_results_excluded": True},
            "decision-timeline.json": {"schema_id": "issue-37-decision-timeline-v1", "disclosure_stage": "after_entry_review", "action_space_constraint": "initial_no_potions", "legal_action_alternatives": "not recorded", "cases": timelines},
            "terminal-results.private.json": {"schema_id": "issue-37-terminal-results-v1", "disclosure_stage": "optional_after_timeline", "cases": outcomes},
            "source-provenance.private.json": private_map, "prior-impressions.json": prior,
            "field-allowlist.json": allowlist, "field-coverage.json": coverage,
        }
        state.update({"stage": "writing_review_packet", "updated_utc": now()})
        write_json(state_path, state)
        for name, payload in packets.items():
            write_json(out / name, payload)
        (out / "index.html").write_text(entry_html(entries), encoding="utf-8")
        (out / "timeline.html").write_text(timeline_html(timelines), encoding="utf-8")
        (out / "outcomes.html").write_text(outcomes_html(outcomes), encoding="utf-8")
        prior_sections = []
        for note in prior["cases"]:
            prior_sections.append(f"<section class='case'><h2>案例 {h(note['case'])}：{h(note['label'])}（临时）</h2><p>{h(note['zh'])}</p><details><summary>Issue 中的原始英文备注</summary><p>{h(note['original_note'])}</p></details></section>")
        prior_html = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>旧版临时资源印象</title><style>" + STYLE + "</style><body><h1>旧版临时资源印象（非本次注释）</h1><div class='notice'>以下意见来自缺少完整牌组等信息的旧视图。poor/adequate 只表示当时的资源印象，不是可赢性、失败归因或裁定标签。请先查看完整入口信息。本页不收集新标注。</div>" + "".join(prior_sections) + "</body></html>\n"
        (out / "prior-notes.html").write_text(prior_html, encoding="utf-8")

        output_names = [*packets, "index.html", "timeline.html", "outcomes.html", "prior-notes.html"]
        output_identities = {name: {"sha256": file_identity(out / name)["sha256"], "size_bytes": file_identity(out / name)["size_bytes"]} for name in output_names}
        script_id = file_identity(Path(__file__))
        output_manifest = {"schema_id": "issue-37-output-manifest-v1", "status": "STAGE_0_PASS_STAGE_1_PACKET_READY_FOR_REVIEW",
                           "generated_utc": now(), "producer": script_id, "source_artifacts": source_artifacts,
                           "source_rows_seen": state.get("raw_rows_seen"), "outputs": output_identities,
                           "replay_performed": False, "simulation_performed": False, "new_human_annotations_started": False,
                           "limitations": ["Legal-action alternatives were not retained.", "A-F are a deliberate audit subset, not a representative sample.", "Old resource impressions remain provisional."]}
        write_json(out / "output-manifest.json", output_manifest)
        state.update({"status": "completed", "stage": "packet_ready_for_independent_review", "updated_utc": now(),
                      "source_rows_seen": state.get("raw_rows_seen"), "selected_rows": len(selected), "output_manifest": "output-manifest.json"})
        write_json(state_path, state)
        print(json.dumps({"status": state["status"], "stage": state["stage"], "selected_rows": len(selected), "source_rows_seen": state.get("raw_rows_seen")}, ensure_ascii=False))
        return 0
    except Exception as exc:
        state.update({"status": "failed", "stage_error": str(exc), "updated_utc": now()})
        write_json(state_path, state)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
