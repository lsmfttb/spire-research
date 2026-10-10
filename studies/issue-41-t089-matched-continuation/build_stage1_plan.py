#!/usr/bin/env python3
"""Artifact-only preregistration and resource gate; performs no simulator calls."""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
I40 = REPO / "studies/issue-40-native-recovery"
ART = Path("/mnt/d/DeadlyCatCoding/STSRL/artifacts")
T075 = ART / "t075-leakage-safe-non-combat-cohort-repair"
T089 = ART / "t089-frozen-search-self-generated-non-combat-policy"
SOURCE = T075 / "selected-states.jsonl"
TARGETS = T089 / "target-table-formal-attempt-2.json"
GATE = T089 / "heldout-attempt-1/heldout-gate.json"
CHECKPOINT = T089 / "training-attempt-4/checkpoints/model-893002.pt"
T075_MANIFEST = T075 / "retention.json"
T089_MANIFEST = T089 / "t089-retention-manifest.json"
OWNERSHIP = T075 / "ownership-audit.json"
OUT = HERE / "stage1-plan.json"
FAMILIES = ("MAP_SCREEN", "REST_ROOM", "REWARDS", "TREASURE_ROOM")
SEEDS = (892201, 892202, 892203, 892204)
EXPECTED = {
    "source": "94857d0e310f34cdd2780920ec81f9dc60e179c94244b9e231952a43a5f4e8b8",
    "targets": "b8551454f764d47b43d068d507df783226f4b2651ea74fe82bec7fd4b0b8de11",
    "checkpoint": "a3a136f729b5cee69815a0b128411eec49a9b4f7695c6aae5a3796345fbd972e",
    "gate": "ed891e663eeb3e33d7eceda3d590368bcf1d918e0d620403edcb40e2629984c7",
    "t075_manifest": "ebc6d107ba36322890a1e522a18ab5265d8003d0ba7c6529dc49d454346d4e46",
    "t089_manifest": "c29145e102089432ff920102ef9a1e56056e05f32e4e6b4ddd19c8dfdb7b4a1f",
}
MAX_ATTEMPTS = 512
MAX_WALL = 12000
MAX_CPU = 12000


class Stop(RuntimeError):
    def __init__(self, terminal: str, message: str):
        super().__init__(message)
        self.terminal = terminal


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write(path: Path, obj: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def iter_array(path: Path, key: str) -> Iterable[dict[str, Any]]:
    marker = f'  "{key}": ['
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.startswith(marker):
                break
        else:
            raise Stop("PAIRED_TARGET_MISMATCH", f"target JSON lacks array {key!r}")
        buffer = stream.read(65536)
        while True:
            buffer = buffer.lstrip()
            if buffer.startswith("]"):
                return
            if buffer.startswith(","):
                buffer = buffer[1:]
                continue
            if not buffer:
                buffer = stream.read(65536)
                if not buffer:
                    raise Stop("PAIRED_TARGET_MISMATCH", "target array ended unexpectedly")
                continue
            try:
                row, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                chunk = stream.read(65536)
                if not chunk:
                    raise Stop("PAIRED_TARGET_MISMATCH", "target array contains invalid JSON")
                buffer += chunk
                continue
            if not isinstance(row, dict):
                raise Stop("PAIRED_TARGET_MISMATCH", "target array contains non-object row")
            yield row
            buffer = buffer[end:]


def load_source() -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    rows = []
    by_index = {}
    with SOURCE.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"invalid source row {line_no}: {exc}") from exc
            idx = row.get("selected_state_index")
            if not isinstance(idx, int) or idx in by_index:
                raise Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"invalid or duplicate source index at line {line_no}")
            by_index[idx] = row
            rows.append(row)
    if len(rows) != 320 or sorted(by_index) != list(range(320)):
        raise Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T075 source state indices are not exact 0..319")
    return rows, by_index


def verify_roles(audit: Mapping[str, Any]) -> dict[str, Any]:
    stsr_root = Path(audit["source_paths"]["stsr_root"])
    groups = audit["retained_artifacts"]
    roles: dict[str, tuple[Path, str, int]] = {}
    for group in ("T075_required_roles", "T089_required_roles"):
        for name, item in groups[group].items():
            p = Path(item["path"])
            digest = str(item.get("expected_sha256") or item.get("sha256"))
            if item.get("verified") is not True or not p.is_file() or p.stat().st_size != int(item["size_bytes"]) or sha(p) != digest:
                terminal = "SOURCE_OR_TARGET_UNAVAILABLE" if group.startswith("T075") else "HISTORICAL_IDENTITY_UNAVAILABLE"
                raise Stop(terminal, f"retained role mismatch: {group}/{name}")
            roles[f"{group}/{name}"] = (p, digest, int(item["size_bytes"]))
    if len(roles) != 14:
        raise Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"expected 14 retained roles, found {len(roles)}")

    manifests = []
    for group, path, expected_sha, schema in (
        ("T075_required_roles", T075_MANIFEST, EXPECTED["t075_manifest"], "t075-retention-v1"),
        ("T089_required_roles", T089_MANIFEST, EXPECTED["t089_manifest"], "t089-retention-manifest-v1"),
    ):
        if not path.is_file() or sha(path) != expected_sha:
            raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", f"retention manifest mismatch: {path}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema_id") != schema:
            raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", f"retention schema mismatch: {path}")
        entry_rows = doc.get("entries", doc.get("artifacts"))
        manifest_entries = {}
        for entry in entry_rows:
            item = entry.get("artifact", entry) if isinstance(entry, dict) else None
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                manifest_entries[item["path"].replace("\\", "/")] = item
        covered = []
        for key, (artifact, digest, size) in roles.items():
            if not key.startswith(group + "/"):
                continue
            rel = artifact.resolve().relative_to(stsr_root.resolve()).as_posix()
            item = manifest_entries.get(rel)
            if not item or item.get("sha256") != digest or item.get("size_bytes") != size:
                raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", f"manifest does not pin exact role: {rel}")
            covered.append(key.split("/", 1)[1])
        expected_count = 5 if group.startswith("T075") else 9
        if len(covered) != expected_count:
            raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", f"manifest role count mismatch: {group}: {len(covered)}")
        manifests.append({"path": path.name, "sha256": expected_sha, "schema_id": schema, "role_count": len(covered)})
    if sha(SOURCE) != EXPECTED["source"] or sha(TARGETS) != EXPECTED["targets"] or sha(CHECKPOINT) != EXPECTED["checkpoint"] or sha(GATE) != EXPECTED["gate"]:
        raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "a frozen source/target/checkpoint/gate hash changed")
    if audit.get("identity", {}).get("T075_retention_manifest_sha256") != EXPECTED["t075_manifest"] or audit.get("identity", {}).get("T089_retention_manifest_sha256") != EXPECTED["t089_manifest"]:
        raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "Issue 40 source audit manifest hashes differ")
    return {"retained_role_count": 14, "retained_roles": [{"role": k, "path": v[0].resolve().relative_to(stsr_root.resolve()).as_posix(), "sha256": v[1], "size_bytes": v[2]} for k, v in sorted(roles.items())], "retention_manifests": manifests, "source_selection_sha256": EXPECTED["source"], "target_table_sha256": EXPECTED["targets"], "checkpoint_sha256": EXPECTED["checkpoint"], "heldout_gate_sha256": EXPECTED["gate"]}


def main() -> int:
    try:
        audit = json.loads((I40 / "source-audit.json").read_text(encoding="utf-8"))
        if audit.get("terminal") != "STAGE0_READY_FOR_SMOKE":
            raise Stop("SOURCE_OR_TARGET_UNAVAILABLE", "Issue 40 Stage-0 gate is not ready")
        proc = subprocess.run([sys.executable, str(I40 / "verify_issue40.py")], cwd=str(REPO), text=True, capture_output=True)
        if proc.returncode or "PASS Issue 40 verification: 66 checks" not in proc.stdout:
            raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", f"Issue 40 verification failed: {proc.stdout[-1000:]} {proc.stderr[-1000:]}")
        identity = verify_roles(audit)
        source_rows, source_by_index = load_source()
        heldout = [r for r in source_rows if r.get("split") == "heldout"]
        family_counts = Counter(r["family"] for r in heldout)
        if len(heldout) != 64 or dict(family_counts) != {f: 16 for f in FAMILIES}:
            raise Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"held-out family/count gate failed: {len(heldout)}, {dict(family_counts)}")
        run_ids = {r["source_run_id"] for r in heldout}
        if len(run_ids) != 43 or audit.get("cohort", {}).get("distinct_heldout_source_run_count") != 43:
            raise Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"held-out source-run count changed: {len(run_ids)}")
        ownership = json.loads(OWNERSHIP.read_text(encoding="utf-8"))
        owner_by_digest = {}
        for group in ownership.get("groups", []):
            for member in group.get("members", []):
                if member.get("owner") is True:
                    digest = member.get("selection_digest")
                    if digest in owner_by_digest:
                        raise Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"duplicate ownership digest {digest}")
                    owner_by_digest[digest] = (group, member)
        for row in source_rows:
            owner = owner_by_digest.get(row.get("selection_digest"))
            if not owner or owner[0].get("family") != row.get("family") or owner[1].get("split") != row.get("split") or owner[1].get("simulator_seed") != row.get("simulator_seed") or owner[1].get("source_arm") != row.get("source_arm"):
                raise Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"source ownership mismatch at index {row['selected_state_index']}")
        gate = json.loads(GATE.read_text(encoding="utf-8"))
        gate_rows = gate.get("model_results", {}).get("893002", [])
        gate_by_index = {int(r["selected_state_index"]): r for r in gate_rows}
        if gate.get("schema_id") != "t089-heldout-gate-report-v1" or gate.get("passed") is not True or gate.get("selected_model_seed") != 893002 or len(gate_by_index) != 64 or set(gate_by_index) != {r["selected_state_index"] for r in heldout}:
            raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "original T089 held-out gate is not exact 64-state coverage")
        plan_states = []
        expected_actions = {}
        for state in heldout:
            idx = int(state["selected_state_index"])
            gate_row = gate_by_index[idx]
            legal = state["legal_action_identities"]
            for role in ("model", "expert"):
                action_index = gate_row.get(f"{role}_action_index")
                if not isinstance(action_index, int) or action_index not in state["eligible_action_indices"] or gate_row.get(f"{role}_action_identity") != legal[action_index]:
                    raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", f"frozen {role} action identity mismatch at state {idx}")
            if gate_row.get("split") != "heldout" or gate_row.get("family") != state["family"] or gate_row.get("public_state_identity") != state["public_state_identity"]:
                raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", f"frozen state identity mismatch at {idx}")
            if state.get("source_floor") is None:
                raise Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"source floor missing at {idx}")
            plan_states.append({"index": idx, "family": state["family"], "source_run_id": state["source_run_id"], "simulator_seed": state["simulator_seed"], "source_step_index": state["source_step_index"], "source_floor": state["source_floor"], "public_state_identity": state["public_state_identity"], "eligible_action_indices": state["eligible_action_indices"], "model": {"index": gate_row["model_action_index"], "action_id": gate_row["model_action_identity"]["action_id"], "identity": gate_row["model_action_identity"]}, "expert": {"index": gate_row["expert_action_index"], "action_id": gate_row["expert_action_identity"]["action_id"], "identity": gate_row["expert_action_identity"]}, "coincident_root_action": gate_row["model_action_index"] == gate_row["expert_action_index"]})
            expected_actions[idx] = set(state["eligible_action_indices"])
        target_rows = {}
        target_counts = Counter()
        for row in iter_array(TARGETS, "targets"):
            idx, action = row.get("selected_state_index"), row.get("legal_action_index")
            if idx in expected_actions and isinstance(action, int):
                if row.get("split") != "heldout" or action not in expected_actions[idx]:
                    raise Stop("PAIRED_TARGET_MISMATCH", f"unexpected held-out target row {(idx, action)}")
                target_counts[idx] += 1
                key = (idx, action)
                if key in target_rows:
                    raise Stop("PAIRED_TARGET_MISMATCH", f"duplicate target row {key}")
                source = source_by_index[idx]
                if row.get("target_status") != "complete" or row.get("problems") or row.get("continuation_seeds") != list(SEEDS) or row.get("legal_action_identity") != source["legal_action_identities"][action]:
                    raise Stop("PAIRED_TARGET_MISMATCH", f"target identity/status/seed mismatch {key}")
                floors = row.get("terminal_floors")
                if not isinstance(floors, list) or len(floors) != 4 or source["source_floor"] is None:
                    raise Stop("PAIRED_TARGET_MISMATCH", f"target floors/source floor missing {key}")
                expected_q = statistics.mean(max(0.0, float(x) - float(source["source_floor"])) for x in floors)
                if abs(float(row.get("q_floor", float("nan"))) - expected_q) > 1e-9:
                    raise Stop("PAIRED_TARGET_MISMATCH", f"historical Q arithmetic mismatch {key}")
                target_rows[key] = row
        expected_targets = sum(len(r["eligible_action_indices"]) for r in heldout)
        if expected_targets != 221 or sum(target_counts.values()) != 221 or len(target_counts) != 64 or any(target_counts[r["selected_state_index"]] != len(r["eligible_action_indices"]) for r in heldout):
            raise Stop("PAIRED_TARGET_MISMATCH", f"held-out target coverage is not exact 221 rows: {sum(target_counts.values())}")

        branches = []
        for state in plan_states:
            for role in ("model", "expert"):
                action = int(state[role]["index"])
                previous = next((b for b in branches if b["state_index"] == state["index"] and b["root_action_index"] == action), None)
                if previous:
                    previous["root_roles"].append(f"{role}_root")
                else:
                    branches.append({"state_index": state["index"], "family": state["family"], "root_action_index": action, "root_action_identity": state[role]["identity"], "root_roles": [f"{role}_root"]})
        all_keys = {(b["state_index"], b["root_action_index"], seed) for b in branches for seed in SEEDS}
        if len(all_keys) != len(branches) * 4:
            raise Stop("COST_OR_REPLAY_BLOCKED", "planned physical continuation keys are not unique")
        old = json.loads((I40 / "smoke-result.json").read_text(encoding="utf-8"))
        old_plan = json.loads((I40 / "smoke-plan.json").read_text(encoding="utf-8"))
        old_budget = json.loads((I40 / "smoke-budget.json").read_text(encoding="utf-8"))
        if old.get("terminal") != "SMOKE_PASS_STAGE1_AWAITING_PLANNER" or old.get("stage1_started") is not False or len(old.get("attempts", [])) != 44 or old_budget.get("attempted_continuations_before_current") != 45:
            raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "Issue 40's 44 completed rows and 45-attempt ledger are not exact")
        by_old_key = {}
        for row in old["attempts"]:
            key = (int(row["state_index"]), int(row["root_action_index"]), int(row["continuation_seed"]))
            if key in by_old_key:
                raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", f"duplicate Issue 40 key {key}")
            by_old_key[key] = row
        plan_state_by_idx = {s["index"]: s for s in plan_states}
        smoke_keys = set()
        for fixed in old_plan["selection"]["heldout_states"]:
            full = plan_state_by_idx.get(int(fixed["index"]))
            if not full or full["model"]["index"] != fixed["model"]["index"] or full["expert"]["index"] != fixed["expert"]["index"] or full["model"]["action_id"] != fixed["model"]["action_id"] or full["expert"]["action_id"] != fixed["expert"]["action_id"]:
                raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", f"Issue 40 root selection mismatch at {fixed['index']}")
            for action in {int(fixed["model"]["index"]), int(fixed["expert"]["index"])}:
                for seed in SEEDS:
                    smoke_keys.add((int(fixed["index"]), action, seed))
        if smoke_keys != set(by_old_key) or not smoke_keys.issubset(all_keys) or len(smoke_keys) != 44:
            raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "the exact 44 Issue 40 rows do not map into the 64-state plan")
        for key, row in by_old_key.items():
            branch = next(b for b in branches if (b["state_index"], b["root_action_index"]) == key[:2])
            target = target_rows[key[:2]]
            state = plan_state_by_idx[key[0]]
            offset = SEEDS.index(key[2])
            ge = max(0.0, float(target["terminal_floors"][offset]) - float(state["source_floor"]))
            gm = max(0.0, float(row["terminal_floor"]) - float(state["source_floor"]))
            if row.get("terminal") is not True or row.get("event") != "continuation_completed" or row["root_action_identity"] != branch["root_action_identity"] or set(row["root_roles"]) != set(branch["root_roles"]) or abs(float(row["smoke_q_floor"]) - gm) > 1e-9 or abs(float(row["historical_expert_q_floor"]) - ge) > 1e-9 or abs(float(row["historical_expert_terminal_floor"]) - float(target["terminal_floors"][offset])) > 1e-9:
                raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", f"Issue 40 row identity/arithmetic mismatch {key}")

        attempts_before = int(old_budget["attempted_continuations_before_current"])
        new_keys = sorted(all_keys - set(by_old_key))
        if attempts_before + len(new_keys) > MAX_ATTEMPTS:
            raise Stop("COST_OR_REPLAY_BLOCKED", f"attempt cap would be exceeded: {attempts_before} + {len(new_keys)} > {MAX_ATTEMPTS}")
        smoke_rows = old["attempts"]
        total_wall, total_cpu = 776.518, 718.170
        typical_row_wall = statistics.mean(float(r["attempt_wall_seconds"]) for r in smoke_rows)
        row_starts = {r.get("attempt"): r for r in [json.loads(line) for line in (I40 / "smoke-progress.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()] if r.get("event") == "continuation_started"}
        row_cpu = []
        for row in smoke_rows:
            start = row_starts.get(row["attempt"])
            if not start:
                raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "Issue 40 per-row CPU start sample missing")
            row_cpu.append(max(0.0, float(row["resource_sample"]["process_cpu_seconds"]) - float(start["sample_before"]["process_cpu_seconds"])))
        if len(row_cpu) != 44:
            raise Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "Issue 40 row CPU samples are incomplete")
        wall_prep_full64 = max(0.0, total_wall - sum(float(r["attempt_wall_seconds"]) for r in smoke_rows)) * 8
        cpu_prep_full64 = max(0.0, total_cpu - sum(row_cpu)) * 8
        upper_wall = wall_prep_full64 + len(new_keys) * total_wall / 44
        upper_cpu = cpu_prep_full64 + len(new_keys) * total_cpu / 44
        typical_wall = max(0.0, 619.287 - sum(float(r["attempt_wall_seconds"]) for r in smoke_rows)) * 8 + len(new_keys) * typical_row_wall
        typical_cpu = max(0.0, 591.08 - sum(row_cpu)) * 8 + len(new_keys) * statistics.mean(row_cpu)
        if max(upper_wall, upper_cpu, typical_wall, typical_cpu) > min(MAX_WALL, MAX_CPU):
            raise Stop("COST_OR_REPLAY_BLOCKED", f"cost projection exceeds cap: upper {upper_wall:.1f}/{upper_cpu:.1f}s, typical {typical_wall:.1f}/{typical_cpu:.1f}s")

        plan = {
            "schema_id": "issue-41-t089-matched-continuation-plan-v1", "schema_version": 1, "issue": 41,
            "study_disposition": "STUDY_ONLY", "authorized_terminal_after_pass": "STAGE1_COMPLETE_REVIEWER_PENDING",
            "source": {"artifact_sha256": EXPECTED["source"], "source_state_count": 320, "heldout_state_count": 64, "heldout_family_counts": {f: 16 for f in FAMILIES}, "distinct_heldout_source_run_count": 43},
            "baseline": {"target_table_sha256": EXPECTED["targets"], "target_rows_all": 1111, "target_rows_heldout": 221, "checkpoint_sha256": EXPECTED["checkpoint"], "checkpoint_seed": 893002, "approved_spec_commit": "afffcdda5cebfe47a2cfa1624911d916191bab76", "heldout_gate_sha256": EXPECTED["gate"]},
            "native": {"repository": "lsmfttb/sts_lightspeed", "commit": "20a6c2b3a9cea817c988178b814f083ff889853f", "json_commit": "0b345b20c888f7dc8888485768e4bf9a6be29de0", "pybind11_commit": "d03662f0984f652b60e7ddce53d3868002275197", "extension_sha256": "108416736aa83973e963cb06e02411d0eb389109003c62e414631053e35ef062", "search": "Search-v2@400", "root_selection": "highest_mean", "potions": "disabled"},
            "model": {"code_commit": "95abc8b1afe7d84e567aa70fb7225eba09fdff25", "checkpoint_seed": 893002, "checkpoint_sha256": EXPECTED["checkpoint"]},
            "continuation": {"seeds": list(SEEDS), "model_input": "non-combat-model-input-v1", "supported_noncombat": list(FAMILIES), "unsupported_noncombat": "exact frozen Expert fallback seeded as T089 fresh-run controller", "battle_policy": "frozen Search-v2@400 highest_mean no-potions"},
            "selection": {"method": "All original T089 held-out gate rows in original source order; no reselection.", "heldout_states": plan_states},
            "execution": {"unique_root_state_action_pairs": len(branches), "total_unique_root_action_seed_keys": len(all_keys), "reused_issue40_complete_rows": 44, "new_unique_continuations": len(new_keys), "historical_attempts": attempts_before, "remaining_attempt_slots": MAX_ATTEMPTS - attempts_before, "total_attempts_if_complete_no_retry": attempts_before + len(new_keys), "max_total_attempts": MAX_ATTEMPTS, "max_steps": 500, "concurrency": 1, "physical_branches": branches},
            "resource_projection": {"additional_cap_seconds": {"wall": MAX_WALL, "process_cpu": MAX_CPU}, "smoke_observed": {"wall_seconds_cumulative": total_wall, "process_cpu_seconds_cumulative": total_cpu, "attempts_counted": attempts_before, "complete_rows": 44, "mean_completed_row_wall_seconds": round(typical_row_wall, 3), "max_completed_row_wall_seconds": max(float(r["attempt_wall_seconds"]) for r in smoke_rows), "mean_completed_row_cpu_seconds": round(statistics.mean(row_cpu), 3), "full_64_root_preflight_wall_estimate_seconds": round(wall_prep_full64, 3), "full_64_root_preflight_cpu_estimate_seconds": round(cpu_prep_full64, 3)}, "upper_empirical_projection": {"wall_seconds": round(upper_wall, 3), "process_cpu_seconds": round(upper_cpu, 3), "method": "remaining unique keys charged the full Issue 40 cumulative cost per completed row, including prior failures and setup, plus 8x historical preflight overhead for 64 roots"}, "typical_projection": {"wall_seconds": round(typical_wall, 3), "process_cpu_seconds": round(typical_cpu, 3), "method": "remaining keys at successful completed-row mean plus 8x successful Issue 40 setup/preflight overhead"}, "projection_gate_passed": True, "limits": {"wall_seconds": MAX_WALL, "process_cpu_seconds": MAX_CPU, "peak_process_rss_mib": 8192, "minimum_available_memory_mib": 4096, "max_steps_per_continuation": 500}},
            "input_identity": identity,
            "reuse": {"issue40_head": "7250abbba99c53a7d64f30483858d3b57310c766", "issue40_verification_sha256": sha(I40 / "verification.json"), "issue40_smoke_result_sha256": sha(I40 / "smoke-result.json"), "issue40_smoke_plan_sha256": sha(I40 / "smoke-plan.json"), "issue40_attempt_ledger": old_budget["execution_history"]},
            "source_ownership": {"verified_state_count": len(source_rows), "heldout_source_run_count": len(run_ids), "ownership_audit_sha256": sha(OWNERSHIP), "ownership_strategy": ownership.get("strategy_id"), "ownership_run_head": ownership.get("run_head")},
            "preflight_required_before_continuations": ["exact native source/submodules/loaded extension", "T089 model/checkpoint identity", "64 replayed public state and ordered legal identities", "root action recomputation equals original held-out gate", "source checkpoint restore equality", "attempt cap", "wall/CPU/RSS/memory caps"],
        }
        write(OUT, plan)
        print(json.dumps({"terminal": "PREFLIGHT_PLAN_READY", "unique_pairs": len(branches), "total_unique_keys": len(all_keys), "reused_rows": 44, "new_keys": len(new_keys), "attempts_if_complete": attempts_before + len(new_keys), "upper_wall_cpu_seconds": [round(upper_wall, 3), round(upper_cpu, 3)], "typical_wall_cpu_seconds": [round(typical_wall, 3), round(typical_cpu, 3)], "plan_sha256": sha(OUT)}, sort_keys=True))
        return 0
    except Stop as exc:
        write(HERE / "stage1-preflight.json", {"schema_id": "issue-41-stage1-preflight-v1", "schema_version": 1, "issue": 41, "terminal": exc.terminal, "stage1_started": False, "stop_detail": str(exc), "hypothesis_result_reported": False})
        print(json.dumps({"terminal": exc.terminal, "stop_detail": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
