"""Read-only renderer for Issue 63's public-input diagnostic packet."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
ROOT_REL = Path("studies/issue-62-natural-root-q/evidence/root-cohort.json")
ADAPTER_REL = Path("studies/issue-60-onpolicy-joint-choice/adapter_v47.py")
OUTPUT = Path(__file__).with_name("packet.md")
ROOT_COMMIT = "863b0ab047fb541cadce0b46eda5d1bfd0a99458"
ROOT_BLOB = "9b928ae813b2ba716ff1cf35a29b4923663ff67d"
ADAPTER_COMMIT = "1cce7997c648121742f0df0cfb95a2211aa3492c"
ADAPTER_BLOB = "3d0c4104043456a3ade4baf739c18704c1b80b7c"
DOMAIN = "issue63-natural-review-v1"
SCREEN_COUNTS = {"MAP_SCREEN": 35, "REWARDS": 61}
ACTION_FIELDS = ("scope", "kind", "idx1", "idx2", "idx3", "label")
UNKNOWN = "未记录/不可见"
MAP_PREVIEW_STEPS = 4


class PacketBlocked(RuntimeError):
    pass


def block(message: str) -> None:
    raise PacketBlocked(message)


def git(*args: str) -> str:
    try:
        p = subprocess.run(["git", *args], cwd=REPO, check=True,
                           capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        block(f"固定来源校验失败: {exc}")
    return p.stdout.strip()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def verify_sources(root_path: Path, adapter_path: Path) -> None:
    git("merge-base", "--is-ancestor", ROOT_COMMIT, "HEAD")
    if git("rev-parse", f"{ROOT_COMMIT}:{ROOT_REL.as_posix()}") != ROOT_BLOB:
        block("固定 Issue 62 root-cohort blob 不匹配")
    if git("hash-object", str(root_path)) != ROOT_BLOB:
        block("root-cohort.json 已变更")
    if git("rev-parse", f"{ADAPTER_COMMIT}:{ADAPTER_REL.as_posix()}") != ADAPTER_BLOB:
        block("固定 Issue 60 adapter blob 不匹配")
    if git("hash-object", str(adapter_path)) != ADAPTER_BLOB:
        block("adapter_v47.py 已变更")


def select(cohort: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    rows = cohort.get("roots")
    if not isinstance(rows, list) or len(rows) != 96:
        block("固定 cohort 不是 96 个自然根")
    result: list[tuple[str, Mapping[str, Any]]] = []
    owners: set[int] = set()
    for screen, prefix in (("MAP_SCREEN", "M"), ("REWARDS", "R")):
        eligible = [r for r in rows if isinstance(r, Mapping)
                    and r.get("status") == "ROOT_FOUND" and r.get("screen") == screen]
        if len(eligible) != SCREEN_COUNTS[screen]:
            block(f"{screen} eligible root count 不匹配")
        if any(type(r.get("legal_n")) is not int or r["legal_n"] < 2 for r in eligible):
            block(f"{screen} eligible population includes a non-multi-choice root")
        ranked = []
        for row in eligible:
            owner = row.get("owner_seed")
            if type(owner) is not int:
                block(f"{screen} eligible root 缺少排序所需 owner")
            key = f"{DOMAIN}|{screen}|{owner}".encode("utf-8")
            ranked.append((hashlib.sha256(key).hexdigest(), row))
        ranked.sort(key=lambda item: item[0])
        for rank, (_, row) in enumerate(ranked[:4], 1):
            owner = row["owner_seed"]
            if owner in owners:
                block("抽样重复使用同一 owner")
            owners.add(owner)
            if type(row.get("legal_n")) is not int or row["legal_n"] < 2:
                block(f"{screen} selected root 不是多选根")
            result.append((f"{prefix}{rank:02d}", row))
    return result


def wrapper(container: Any, key: str, where: str) -> Any:
    if not isinstance(container, Mapping) or key not in container:
        return None
    item = container[key]
    if not isinstance(item, Mapping):
        block(f"{where}.{key} wrapper 无效")
    availability = item.get("availability")
    if availability not in {"available", "unavailable", "unsupported", "not_applicable"}:
        block(f"{where}.{key} availability 无效")
    if availability != "available":
        return None
    if "value" not in item:
        block(f"{where}.{key} 标为 available 但缺少 value")
    return item["value"]


def available_direct(state: Mapping[str, Any], availability: Mapping[str, Any],
                     key: str) -> Any:
    if availability.get(key) != "available":
        return None
    if key not in state or state[key] is None:
        block(f"{key} 标为 available 但没有公开值")
    return state[key]


def check_input(row: Mapping[str, Any], case_id: str) -> dict[str, Any]:
    raw, expected = row.get("public_input"), row.get("public_input_sha256")
    if not isinstance(raw, str) or not isinstance(expected, str):
        block(f"{case_id}: 缺少 public_input 或 SHA-256")
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected:
        block(f"{case_id}: public_input SHA-256 不匹配")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        block(f"{case_id}: public_input JSON 无效: {exc}")
    if not isinstance(data, dict) or data.get("schema_id") != "noncombat-visible-choice-input-v1":
        block(f"{case_id}: adapter schema 不匹配")
    avail, state = data.get("availability"), data.get("public_state")
    if not isinstance(avail, Mapping) or not isinstance(state, Mapping):
        block(f"{case_id}: public_input 缺少 availability/state")
    screen = row.get("screen")
    if state.get("screen") != screen:
        block(f"{case_id}: screen 与固定 cohort 不一致")
    for key in ("screen_identity", "screen_payload", "candidate_actions"):
        if avail.get(key) != "available":
            block(f"{case_id}: adapter 未提供 {key}")
    context = state.get("screen_context")
    if not isinstance(context, Mapping) or context.get("coverage_status") != "supported":
        block(f"{case_id}: screen context 不受支持")
    if screen == "MAP_SCREEN":
        for key in ("visible_act_boss", "visible_map_graph", "current_map_node",
                    "immediately_legal_routes"):
            if avail.get(key) != "available":
                block(f"{case_id}: MAP_SCREEN 缺少 {key}")
    options, identities, n = data.get("legal_options"), row.get("legal_action_identities"), row.get("legal_n")
    if not isinstance(options, list) or type(n) is not int or len(options) != n:
        block(f"{case_id}: legal option count 不匹配")
    if not isinstance(identities, list) or len(identities) != n:
        block(f"{case_id}: 缺少候选身份 parity 校验")
    indices = [o.get("candidate_index") if isinstance(o, Mapping) else None for o in options]
    if indices != list(range(n)):
        block(f"{case_id}: 原始 option/candidate 顺序不一致")
    seen: set[str] = set()
    for i, (option, identity) in enumerate(zip(options, identities)):
        if not isinstance(option, Mapping) or not isinstance(identity, list):
            block(f"{case_id}: 第 {i + 1} 个选项结构无效")
        action, semantics = option.get("action"), option.get("semantics")
        if not isinstance(action, Mapping) or set(action) != set(ACTION_FIELDS):
            block(f"{case_id}: 第 {i + 1} 个候选动作 identity 不完整")
        if (any(type(action.get(k)) is not int for k in ACTION_FIELDS[2:5])
                or any(not isinstance(action.get(k), str) for k in ACTION_FIELDS[:2] + ACTION_FIELDS[5:])
                or "bits=" in action["label"]):
            block(f"{case_id}: 第 {i + 1} 个候选动作 identity 不可安全呈现")
        if not isinstance(semantics, Mapping):
            block(f"{case_id}: 第 {i + 1} 个语义绑定缺失")
        if identity != [action[k] for k in ACTION_FIELDS[:5]]:
            block(f"{case_id}: 第 {i + 1} 个 choice↔candidate 绑定不匹配")
        fingerprint = canonical(action)
        if fingerprint in seen:
            block(f"{case_id}: 候选 identity 重复，无法无歧义呈现")
        seen.add(fingerprint)
    if screen == "MAP_SCREEN" and not any(
            o["semantics"].get("choice_type") == "map_route" for o in options):
        block(f"{case_id}: MAP_SCREEN 未呈现合法路线")
    return {"screen": screen, "availability": avail, "state": state,
            "options": options, "legal_n": n}


def text_value(value: Any) -> str:
    if value is None:
        return UNKNOWN
    if isinstance(value, str):
        return value.replace("|", "\\|").replace("\n", " ")
    return canonical(value)


def field_value(state: Mapping[str, Any], field: str) -> Any:
    location = state.get("ordinary_location")
    value = wrapper(location, field, "ordinary_location")
    return UNKNOWN if value is None else value


def resource_value(resources: Any, field: str) -> Any:
    value = wrapper(resources, field, "persistent_resources")
    return UNKNOWN if value is None else value


def card_line(card: Any, ordinal: int) -> str:
    if not isinstance(card, Mapping):
        block(f"卡组第 {ordinal} 项结构无效")
    name = card.get("name") or card.get("id_label") or UNKNOWN
    fields = (("deck_index", "序号"), ("type", "类型"), ("rarity", "稀有度"),
              ("upgrade_count", "升级数"), ("upgraded", "已升级"))
    rest = "；".join(f"{label} {text_value(card.get(key))}" for key, label in fields)
    return f"- {ordinal}. {text_value(name)}；{rest}"


def list_lines(value: Any, keys: tuple[str, ...]) -> list[str]:
    if value == UNKNOWN:
        return [f"- {UNKNOWN}"]
    if not isinstance(value, list):
        block("公开资源项标为 available 但不是列表")
    if not value:
        return ["- 无"]
    result = []
    for item in value:
        if isinstance(item, Mapping):
            result.append("- " + "；".join(
                f"{key} {text_value(item.get(key))}" for key in keys))
        else:
            result.append("- " + text_value(item))
    return result


def map_nodes(record: Mapping[str, Any]) -> dict[tuple[int, int], Mapping[str, Any]]:
    state, avail = record["state"], record["availability"]
    if avail.get("visible_map_graph") != "available":
        block("visible_map_graph 不可用，不能呈现路线下游拓扑")
    graph = state.get("map_graph")
    if not isinstance(graph, list):
        block("visible_map_graph available 但 public_state.map_graph 不是列表")
    nodes: dict[tuple[int, int], Mapping[str, Any]] = {}
    for ordinal, entry in enumerate(graph, 1):
        if not isinstance(entry, Mapping):
            block(f"公开地图图第 {ordinal} 项不是对象")
        x, y, symbol, edges = (entry.get("x"), entry.get("y"),
                               entry.get("room_symbol"), entry.get("outgoing_edges"))
        if type(x) is not int or type(y) is not int or not isinstance(symbol, str) or not symbol:
            block(f"公开地图图第 {ordinal} 项缺少有效坐标或房型")
        if not isinstance(edges, list):
            block(f"公开地图图第 {ordinal} 项 outgoing_edges 不是列表")
        coord = (x, y)
        if coord in nodes:
            block(f"公开地图图坐标重复: {coord}")
        nodes[coord] = entry
    return nodes


def map_context(record: Mapping[str, Any]) -> tuple[str, str]:
    state, avail = record["state"], record["availability"]
    node = available_direct(state, avail, "current_map_node")
    graph_available = avail.get("visible_map_graph") == "available"
    graph = state.get("map_graph") if graph_available else None
    if graph_available and graph is None:
        block("visible_map_graph available but public_state.map_graph is missing")
    if node is None:
        return UNKNOWN, UNKNOWN
    if not isinstance(node, Mapping):
        block("current_map_node 不是对象")
    if graph is None:
        return canonical(node), UNKNOWN
    if not isinstance(graph, list):
        block("visible_map_graph 不是列表")
    if node.get("location") != "map_node":
        return canonical(node), UNKNOWN
    matches = [entry for entry in graph if isinstance(entry, Mapping)
               and entry.get("x") == node.get("x") and entry.get("y") == node.get("y")]
    if len(matches) != 1:
        return canonical(node), UNKNOWN
    edges = matches[0].get("outgoing_edges")
    return canonical(node), canonical(edges) if isinstance(edges, list) else UNKNOWN


def map_node_label(node: Mapping[str, Any]) -> str:
    return f"{text_value(node['room_symbol'])}@({node['x']},{node['y']})"


def route_previews(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project four directed-edge steps after each legal route from public map data."""
    if record["screen"] != "MAP_SCREEN":
        return []
    nodes = map_nodes(record)
    previews = []
    for option_number, option in enumerate(record["options"], 1):
        semantics = option["semantics"]
        if semantics.get("choice_type") != "map_route":
            continue
        destination = semantics.get("destination")
        if (not isinstance(destination, Mapping)
                or destination.get("location") != "map_node"
                or type(destination.get("x")) is not int
                or type(destination.get("y")) is not int):
            block(f"合法 MAP 选项 #{option_number} 缺少公开地图目的地")
        root = (destination["x"], destination["y"])
        if root not in nodes:
            block(f"合法 MAP 选项 #{option_number} 的目的地不在可见地图图中")
        root_node = nodes[root]
        if root_node.get("room_symbol") != destination.get("room_symbol"):
            block(f"合法 MAP 选项 #{option_number} 的目的地房型与地图图不一致")

        levels = []
        frontier = [root]
        visited = {root}
        for step in range(MAP_PREVIEW_STEPS + 1):
            rows = []
            next_frontier = []
            for coord in sorted(frontier):
                node = nodes[coord]
                successors = []
                if step < MAP_PREVIEW_STEPS:
                    for edge in node["outgoing_edges"]:
                        if (not isinstance(edge, Mapping)
                                or type(edge.get("x")) is not int
                                or type(edge.get("y")) is not int):
                            block(f"地图节点 {coord} 的公开出边坐标无效")
                        target = (edge["x"], edge["y"])
                        child = nodes.get(target)
                        if child is None:
                            block(f"地图节点 {coord} 的公开出边目标 {target} 不在图中")
                        successors.append(map_node_label(child))
                        if target not in visited:
                            visited.add(target)
                            next_frontier.append(target)
                rows.append({"node": map_node_label(node),
                             "successors": successors,
                             "at_preview_limit": step == MAP_PREVIEW_STEPS})
            levels.append(rows)
            if not next_frontier:
                break
            frontier = next_frontier
        previews.append({"option_number": option_number,
                         "destination": map_node_label(root_node),
                         "levels": levels})
    if not previews:
        block("MAP_SCREEN 没有可呈现的合法 map_route 选项")
    return previews


def _group_fields(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if not isinstance(value, Mapping):
        return found
    for key, item in value.items():
        name = str(key)
        normalized = name.lower()
        path = f"{prefix}.{name}" if prefix else name
        if "group" in normalized or "exclu" in normalized or "claim" in normalized:
            found.append((path, item))
        elif isinstance(item, Mapping):
            found.extend(_group_fields(item, path))
    return found


def reward_context_lines(record: Mapping[str, Any]) -> list[str]:
    context = record["state"].get("screen_context")
    if not isinstance(context, Mapping):
        block("REWARDS 缺少当前公开 screen_context")
    explicit = [(f"screen_context.{key}", value)
                for key, value in _group_fields(context)]
    categories: dict[str, list[int]] = {}
    for option_number, option in enumerate(record["options"], 1):
        semantics = option["semantics"]
        explicit.extend((f"option#{option_number}.{key}", value)
                        for key, value in _group_fields(semantics))
        choice_type = semantics.get("choice_type")
        if not isinstance(choice_type, str):
            block(f"REWARDS 合法选项 #{option_number} 缺少公开 choice_type")
        categories.setdefault(choice_type, []).append(option_number)
    lines = ["### 奖励选项类别与公开分组信息"]
    if explicit:
        lines.append("- public_input 中直接记录的分组/领取/互斥字段：")
        lines.extend(f"  - `{key}`：{fence(value)}" for key, value in explicit)
    else:
        lines.append("- 当前 screen_context 及各合法选项语义均未记录奖励组 ID、组成员、共同领取或互斥字段：未记录/不可见。")
    lines.append("- 按可见 choice_type 汇总（仅为语义类别，不代表实际领取分组或互斥规则）：")
    for choice_type, option_numbers in categories.items():
        numbers = ", ".join(f"#{number}" for number in option_numbers)
        lines.append(f"  - `{choice_type}`：合法选项 {numbers}")
    if not explicit:
        lines.append("- 不据此推断不同类别或同类别选项能否共同领取；共同领取/互斥关系：未记录/不可见。")
    return lines


def fence(value: Any) -> str:
    body = canonical(value)
    longest = max((len(m.group(0)) for m in re.finditer(chr(96) + "+", body)), default=0)
    marker = chr(96) * max(1, longest + 1)
    return f"{marker}{body}{marker}"

def render_case(case_id: str, record: Mapping[str, Any]) -> str:
    state, avail = record["state"], record["availability"]
    screen = record["screen"]
    screen_name = "地图选择" if screen == "MAP_SCREEN" else "奖励选择"
    resources = state.get("persistent_resources")
    lines = [
        f"## {case_id} · {screen_name}", "",
        "### 当前可见信息",
        f"- 局面：{screen}",
        f"- Act：{text_value(field_value(state, 'act'))}",
        f"- 层数：{text_value(field_value(state, 'floor'))}",
        f"- 房间类型：{text_value(field_value(state, 'room_type'))}",
        f"- 当前 HP / 最大 HP：{text_value(resource_value(resources, 'current_hp'))} / {text_value(resource_value(resources, 'max_hp'))}",
        f"- 金币：{text_value(resource_value(resources, 'gold'))}",
        f"- 已知 Boss：{text_value(available_direct(state, avail, 'visible_act_boss'))}",
        "- 当前卡组（保留每张卡及其升级绑定）：",
    ]
    deck = resource_value(resources, "deck")
    if deck == UNKNOWN:
        lines.append(f"  - {UNKNOWN}")
    elif not isinstance(deck, list):
        block(f"{case_id}: 可用卡组不是列表")
    elif not deck:
        lines.append("  - 无")
    else:
        lines.extend("  " + card_line(card, i) for i, card in enumerate(deck))
    lines.append("- 遗物：")
    lines.extend("  " + x for x in list_lines(resource_value(resources, "relics"),
                                               ("name", "id_label")))
    lines.append("- 药水：")
    lines.extend("  " + x for x in list_lines(resource_value(resources, "potion_identities"),
                                               ("potion_index", "name", "id_label")))
    lines.extend([
        f"- 药水容量 / 数量：{text_value(resource_value(resources, 'potion_capacity'))} / {text_value(resource_value(resources, 'potion_count'))}",
        f"- 钥匙：{text_value(resource_value(resources, 'keys'))}",
    ])
    node, edges = map_context(record)
    edge_text = text_value(edges)
    current_node = state.get("current_map_node")
    if (screen == "MAP_SCREEN" and isinstance(current_node, Mapping)
            and current_node.get("location") == "before_first_route"):
        edge_text += "（当前节点无唯一地图坐标；不表示候选目的地下游图不可见）"
    lines.extend([
        f"- 当前公开地图节点：{text_value(node)}",
        f"- 该节点在公开地图图中的直接后继：{edge_text}",
        "",
    ])
    if screen == "MAP_SCREEN":
        lines.extend([
            "### 各合法路线目的地的公开下游地图",
            f"- 按每个合法路线选项分别展开；从目的地起沿 public_state.map_graph 的有向边展示最多 {MAP_PREVIEW_STEPS} 跳。房型和坐标直接取公开图；跳数边界之外不呈现。",
        ])
        for preview in route_previews(record):
            lines.append(f"- 合法选项 #{preview['option_number']}，目的地 {preview['destination']}：")
            for step, rows in enumerate(preview["levels"]):
                parts = []
                for row in rows:
                    if row["at_preview_limit"]:
                        relation = "（预览边界；更下游未呈现）"
                    elif row["successors"]:
                        relation = " → " + ", ".join(row["successors"])
                    else:
                        relation = " → 无已记录后继"
                    parts.append(row["node"] + relation)
                lines.append(f"  - 第 {step} 跳：" + "；".join(parts))
        lines.append("")
    if screen == "REWARDS":
        lines.extend(reward_context_lines(record))
        lines.append("")
    lines.extend([
        "### 全部合法选项（保持 public_input 的原有顺序）",
        "每项保留公开选择语义及其对应候选动作身份；序号仅为本卡展示顺序。", "",
    ])
    for i, option in enumerate(record["options"], 1):
        lines.extend([
            f"{i}. **公开选项**", "",
            "   选择语义：", "",
            "   " + fence(option["semantics"]).replace("\n", "\n   "), "",
            "   对应公开候选动作身份：", "",
            "   " + fence(option["action"]).replace("\n", "\n   "), "",
        ])
    lines.extend([
        "### 请仅依据当前可见信息自愿判断",
        "1. 是否有明显风险较高/可能较差的选项，或值得进一步比较的短名单？不必指定唯一最佳项。",
        "2. 哪些可见事实或决策机制对判断重要？",
        "3. 这个案例是否有歧义？可以跳过。", "",
    ])
    return "\n".join(lines)


def render(selected: list[tuple[str, Mapping[str, Any]]],
           records: list[dict[str, Any]]) -> str:
    lines = [
        "# 早期 MAP / REWARDS 公开信息诊断卡", "",
        "> 自愿的分阶段人工诊断；仅呈现决策时 adapter public_input 的可见状态与选项。",
        "> 原始证据来自公开仓库，因此这不是权限隔离或密码学盲审。",
        "> 不要求唯一最佳答案；可以跳过。后续执行结果不在本包中。", "",
        "## 抽样与阅读方式", "",
        "- 固定总体：96 个自然多选根（MAP_SCREEN 35 个，REWARDS 61 个）。",
        "- 分层规则：按 Issue 63 预注册的固定哈希规则，对两类分别取排序前 4；不替换、不按结果字段筛选。",
        "- 案例编号匿名，按层内排序依次编号。本小样本仅作诊断，不估计策略错误发生率。",
        "- 选项按输入顺序保留；每项的语义与完整公开候选动作身份紧邻呈现。",
        "- 未记录或不可见值标明；不呈现历史轨迹、输入来源说明、样例来源标识、已抽取动作提示、模型判断或后续结果。",
        "",
    ]
    lines.extend(render_case(cid, rec) for (cid, _), rec in zip(selected, records))
    output = "\n".join(lines)
    banned = re.compile(
        r'"(?:owner_seed|sampled_p0_index|p0|public_history_sha256|'
        r'native_checkpoint_fingerprint_sha256|public_input_sha256|'
        r'source_prefix_cpu_seconds|source_prefix_wall_seconds|'
        r'root_ordinal_observed|selected_root_ordinal|analysis|branches|'
        r'branch_return|return|outcome|checkpoint|model_score)"\s*:',
        re.IGNORECASE,
    )
    if banned.search(output):
        block("输出意外包含受限来源或结果字段")
    return output


def build() -> tuple[list[tuple[str, Mapping[str, Any]]], list[dict[str, Any]]]:
    root_path, adapter_path = REPO / ROOT_REL, REPO / ADAPTER_REL
    if not root_path.is_file() or not adapter_path.is_file():
        block("固定 root-cohort 或 adapter 文件不存在")
    verify_sources(root_path, adapter_path)
    raw = root_path.read_bytes()
    try:
        cohort = json.loads(raw)
    except json.JSONDecodeError as exc:
        block(f"固定 root-cohort JSON 无效: {exc}")
    if not isinstance(cohort, Mapping) or cohort.get("schema_id") != "issue62-root-cohort-v1":
        block("root-cohort schema 不匹配")
    selected = select(cohort)
    records = [check_input(row, cid) for cid, row in selected]
    return selected, records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true",
                        help="只生成每个 screen 一张的本地渲染 smoke")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    started = time.process_time()
    try:
        selected, records = build()
        if args.smoke:
            selected = [selected[0], selected[4]]
            records = [records[0], records[4]]
        packet = render(selected, records)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8", newline="\n") as f:
            f.write(packet)
    except PacketBlocked as exc:
        print(f"PACKET_BLOCKED: {exc}", file=sys.stderr)
        return 2
    mode = "SMOKE" if args.smoke else "COMPLETE"
    counts = ",".join(f"{case_id}={record['legal_n']}" for (case_id, _), record in zip(selected, records))
    print(f"PACKET_{mode}: cases={len(selected)} candidates=[{counts}] cpu_s={time.process_time() - started:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
