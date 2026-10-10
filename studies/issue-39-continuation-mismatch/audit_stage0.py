#!/usr/bin/env python3
"""Fail-closed Stage 0 audit for spire-research Issue 39.

This audit reads retained public cohort/provenance and compact T089 reports.
It deliberately does not inspect outcome fields to choose smoke roots, load a
teacher target into the learned policy, or start a simulator run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


ISSUE = 39
T075_SELECTION_SHA256 = "94857d0e310f34cdd2780920ec81f9dc60e179c94244b9e231952a43a5f4e8b8"
T089_CODE_HEAD = "95abc8b1afe7d84e567aa70fb7225eba09fdff25"
T089_SPEC_COMMIT = "afffcdda5cebfe47a2cfa1624911d916191bab76"
NATIVE_IDENTITY = {
    "repository": "lsmfttb/sts_lightspeed",
    "ref": "refs/heads/stsrl/main",
    "commit": "20a6c2b3a9cea817c988178b814f083ff889853f",
}
FAMILIES = ("MAP_SCREEN", "REST_ROOM", "REWARDS", "TREASURE_ROOM")
REQUIRED_T089_ROLES = {
    "checkpoint_893002",
    "current_native_revalidation",
    "formal_target_status",
    "heldout_gate",
    "heldout_status",
    "input_eligibility",
    "target_table",
    "training_batch_plans",
    "validation_selection",
}
REQUIRED_T075_ROLES = {
    "ownership_audit",
    "preflight_audit",
    "selected_states",
    "source_reuse_audit",
    "terminal_report",
}
HELDOUT_CONTINUATION_SEEDS = [892201, 892202, 892203, 892204]
PINNED_NATIVE_SUBMODULES = {
    "json": "0b345b20c888f7dc8888485768e4bf9a6be29de0",
    "pybind11": "d03662f0984f652b60e7ddce53d3868002275197",
}
RUN_STATE_PATH = Path(__file__).resolve().parent / "run-state.json"
LAST_COMPLETED_STAGE = "NOT_STARTED"


class Stage0Stop(Exception):
    def __init__(self, terminal: str, reason: str, evidence: dict[str, Any] | None = None):
        super().__init__(reason)
        self.terminal = terminal
        self.evidence = evidence or {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def read_top_level_value(path: Path, key: str) -> Any:
    """Read one top-level JSON value without loading sibling large arrays."""
    marker = f'  "{key}":'
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.startswith(marker):
                buffer = line[len(marker):].lstrip() + stream.read(64 * 1024)
                while True:
                    try:
                        return decoder.raw_decode(buffer)[0]
                    except json.JSONDecodeError:
                        chunk = stream.read(64 * 1024)
                        if not chunk:
                            raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"T089 target table top-level {key!r} is invalid JSON")
                        buffer += chunk
    raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"T089 target table has no top-level {key!r} field")


def verify_identity(path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "available": False, "problems": ["missing"]}
    observed_size = path.stat().st_size
    observed_sha = sha256_file(path)
    problems = []
    if observed_size != expected.get("size_bytes"):
        problems.append("size mismatch")
    if observed_sha != expected.get("sha256"):
        problems.append("sha256 mismatch")
    return {
        "path": str(path),
        "available": True,
        "size_bytes": observed_size,
        "sha256": observed_sha,
        "expected_size_bytes": expected.get("size_bytes"),
        "expected_sha256": expected.get("sha256"),
        "verified": not problems,
        "problems": problems,
    }


def t075_entry(manifest: dict[str, Any], role: str) -> dict[str, Any]:
    for item in manifest.get("entries", []):
        artifact = item.get("artifact", {})
        if artifact.get("role") == role:
            return artifact
    raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"T075 retention manifest has no {role!r} artifact")


def t089_entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for item in manifest.get("artifacts", []):
        role = item.get("role")
        if role:
            found.setdefault(str(role), item)
    missing = sorted(REQUIRED_T089_ROLES - set(found))
    if missing:
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"T089 retention manifest lacks roles: {missing}")
    return found


def iter_top_level_array(path: Path, key: str) -> Iterable[dict[str, Any]]:
    """Stream objects from one top-level JSON array without loading the file."""
    marker = f'  "{key}": ['
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.startswith(marker):
                break
        else:
            raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"target table has no top-level {key!r} array")

        buffer = stream.read(64 * 1024)
        while True:
            buffer = buffer.lstrip()
            if buffer.startswith("]"):
                return
            if buffer.startswith(","):
                buffer = buffer[1:]
                continue
            if not buffer:
                buffer = stream.read(64 * 1024)
                if not buffer:
                    raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"target table {key!r} array ended unexpectedly")
                continue
            try:
                item, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"target table {key!r} array contains invalid JSON")
                buffer += chunk
                continue
            if not isinstance(item, dict):
                raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"target table {key!r} contains a non-object row")
            yield item
            buffer = buffer[end:]


def iter_source_records(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"T075 selected-state row {line_no} is not an object")
            if record.get("schema_id") != "t065-source-state-v1":
                raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"T075 selected-state row {line_no} has wrong schema")
            yield record


def native_commit_available(native_repo: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(native_repo), "cat-file", "-e", f"{NATIVE_IDENTITY['commit']}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def git_commit_available(repo: Path, commit: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def git_head(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def historical_build_available(path: str) -> bool:
    if os.name == "nt" and path.startswith("/"):
        result = subprocess.run(
            ["wsl.exe", "--exec", "test", "-d", path],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0
    return Path(path).is_dir()


def persist_run_state(
    terminal: str,
    error: str | None = None,
    terminal_detail: str | None = None,
) -> None:
    RUN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_id": "issue-39-run-state-v1",
        "schema_version": 1,
        "issue": ISSUE,
        "last_completed_stage": LAST_COMPLETED_STAGE,
        "terminal": terminal,
        "smoke_started": False,
        "stage1_started": False,
        "stage1_completed": False,
    }
    if error is not None:
        state["error"] = error
    if terminal_detail is not None:
        state["terminal_detail"] = terminal_detail
    temporary = RUN_STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(RUN_STATE_PATH)


def write_output_manifest() -> None:
    output_dir = Path(__file__).resolve().parent
    manifest = {
        "schema_id": "issue-39-output-manifest-v1",
        "schema_version": 1,
        "issue": ISSUE,
        "artifacts": [],
    }
    for path, role in (
        (Path(__file__).resolve(), "stage0_audit_script"),
        (output_dir / "source-audit.json", "stage0_source_audit"),
        (RUN_STATE_PATH, "run_state"),
        (output_dir / "README.md", "study_readme"),
    ):
        if path.is_file():
            manifest["artifacts"].append(
                {
                    "path": path.name,
                    "role": role,
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    (output_dir / "output-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def advance_stage(stage: str) -> None:
    global LAST_COMPLETED_STAGE
    LAST_COMPLETED_STAGE = stage
    persist_run_state("STAGE0_RUNNING")


def native_submodules(native_worktree: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "-C", str(native_worktree), "submodule", "status", "--recursive"],
        capture_output=True,
        text=True,
        check=False,
    )
    rows = []
    for line in result.stdout.splitlines():
        match = re.match(r"^([-+U ])([0-9a-f]{40})\s+([^ ]+)", line)
        if match:
            marker, commit, path = match.groups()
            rows.append(
                {
                    "path": path,
                    "commit": commit,
                    "initialized": marker == " ",
                    "marker": marker,
                }
            )
    return {
        "command_succeeded": result.returncode == 0,
        "rows": rows,
        "all_initialized": bool(rows) and all(row["initialized"] for row in rows),
        "stderr": result.stderr.strip() or None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--stsr-root", type=Path, required=True)
    parser.add_argument("--native-repo", type=Path, required=True)
    parser.add_argument("--native-worktree", type=Path, required=True)
    parser.add_argument("--historical-native-build", type=str, required=True)
    args = parser.parse_args()

    global LAST_COMPLETED_STAGE
    persist_run_state("STAGE0_RUNNING")
    repo_root = args.repository_root.resolve()
    stsr_root = args.stsr_root.resolve()
    spire_head = git_head(repo_root)
    expected_spire_base = "9525d416b9488bb2455435699893b03fe19fe30a"
    if spire_head != expected_spire_base:
        raise Stage0Stop(
            "HISTORICAL_IDENTITY_UNAVAILABLE",
            f"study worktree is not at required remote base {expected_spire_base}: {spire_head}",
            {"expected_base": expected_spire_base, "observed_head": spire_head},
        )
    artifact_root = stsr_root / "artifacts"
    t075_dir = artifact_root / "t075-leakage-safe-non-combat-cohort-repair"
    t089_dir = artifact_root / "t089-frozen-search-self-generated-non-combat-policy"
    t075_manifest_path = t075_dir / "retention.json"
    t089_manifest_path = t089_dir / "t089-retention-manifest.json"

    t075_manifest = read_json(t075_manifest_path)
    t089_manifest = read_json(t089_manifest_path)
    if t075_manifest.get("schema_id") != "t075-retention-v1":
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T075 retention schema mismatch")
    if t089_manifest.get("schema_id") != "t089-retention-manifest-v1":
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T089 retention schema mismatch")
    if t089_manifest.get("native_identity") != NATIVE_IDENTITY:
        raise Stage0Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "T089 retention native identity mismatch")
    if t089_manifest.get("approved_spec_commit") != T089_SPEC_COMMIT:
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T089 retention spec commit mismatch")
    t089_code_commit_available = git_commit_available(stsr_root, T089_CODE_HEAD)
    t089_spec_commit_available = git_commit_available(stsr_root, T089_SPEC_COMMIT)
    advance_stage("RETENTION_MANIFESTS_VERIFIED")

    t075_retained = {role: t075_entry(t075_manifest, role) for role in REQUIRED_T075_ROLES}
    t075_checks = {
        role: verify_identity(stsr_root / entry["path"], entry)
        for role, entry in t075_retained.items()
    }
    t075_failures = [role for role, check in t075_checks.items() if not check.get("verified", False)]
    if t075_failures:
        raise Stage0Stop(
            "SOURCE_OR_TARGET_UNAVAILABLE",
            "T075 retained source/ownership audit artifacts failed identity verification",
            {"failed_roles": t075_failures, "checks": t075_checks},
        )
    source_artifact = t075_retained["selected_states"]
    source_path = stsr_root / source_artifact["path"]
    if source_artifact.get("sha256") != T075_SELECTION_SHA256:
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T075 selected-state SHA differs from the frozen Issue input")
    source_identity = verify_identity(source_path, source_artifact)

    retained = t089_entries(t089_manifest)
    t089_checks: dict[str, dict[str, Any]] = {}
    for role in sorted(REQUIRED_T089_ROLES):
        entry = retained[role]
        t089_checks[role] = verify_identity(stsr_root / entry["path"], entry)
    failures = [
        result["path"]
        for result in [source_identity, *t089_checks.values()]
        if not result.get("verified", False)
    ]
    if failures:
        raise Stage0Stop(
            "SOURCE_OR_TARGET_UNAVAILABLE",
            "retained artifact identity check failed",
            {"failed_paths": failures},
        )
    advance_stage("RETAINED_ARTIFACT_IDENTITIES_VERIFIED")

    preflight = read_json(stsr_root / t075_retained["preflight_audit"]["path"])
    source_reuse = read_json(stsr_root / t075_retained["source_reuse_audit"]["path"])
    ownership = read_json(stsr_root / t075_retained["ownership_audit"]["path"])
    t075_terminal = read_json(stsr_root / t075_retained["terminal_report"]["path"])
    t075_run_head = t075_manifest.get("run_head")
    t075_audit_heads = {
        preflight.get("run_head"),
        source_reuse.get("run_head"),
        ownership.get("run_head"),
        t075_terminal.get("run_head"),
    }
    if (
        preflight.get("schema_id") != "t075-preflight-audit-v1"
        or preflight.get("task_id") != "T075"
        or preflight.get("model_input_schema_id") != "non-combat-model-input-v1"
        or preflight.get("state_dim") != 4737
        or preflight.get("action_dim") != 92
        or len(preflight.get("checks_passed", [])) != 7
        or source_reuse.get("schema_id") != "t075-source-reuse-audit-v1"
        or source_reuse.get("metadata_passed") is not True
        or source_reuse.get("strict_reader_passed") is not True
        or ownership.get("schema_id") != "t075-ownership-audit-v1"
        or ownership.get("strategy_id") != "leakage-safe-global-owner-v1"
        or ownership.get("replay_group_domain") != "T075-replay-group-v1"
        or t075_terminal.get("schema_id") != "t075-terminal-decision-v1"
        or t075_run_head is None
        or t075_audit_heads != {t075_run_head}
    ):
        raise Stage0Stop(
            "SOURCE_OR_TARGET_UNAVAILABLE",
            "T075 source-reuse, ownership, or preflight provenance is incomplete or inconsistent",
            {
                "retention_run_head": t075_run_head,
                "audit_run_heads": sorted(str(head) for head in t075_audit_heads),
                "preflight_check_count": len(preflight.get("checks_passed", [])),
                "source_reuse_metadata_passed": source_reuse.get("metadata_passed"),
                "source_reuse_strict_reader_passed": source_reuse.get("strict_reader_passed"),
                "ownership_strategy": ownership.get("strategy_id"),
            },
        )

    # Keep only public identity/ownership fields. Outcome values in the source
    # records and target table are never consulted for smoke-root selection.
    states_by_index: dict[int, dict[str, Any]] = {}
    family_counts: Counter[str] = Counter()
    split_counts: Counter[str] = Counter()
    heldout_run_ids: set[str] = set()
    for record in iter_source_records(source_path):
        index = record["selected_state_index"]
        if index in states_by_index:
            raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", f"duplicate T075 selected_state_index {index}")
        state = {
            "selected_state_index": index,
            "family": record["family"],
            "split": record["split"],
            "simulator_seed": record["simulator_seed"],
            "source_run_id": record["source_run_id"],
            "source_arm": record["source_arm"],
            "selection_digest": record["selection_digest"],
            "source_step_index": record["source_step_index"],
            "action_trace_length": len(record.get("action_trace", [])),
            "public_state_identity": record["public_state_identity"],
            "eligible_action_indices": record["eligible_action_indices"],
            "legal_action_identities": record["legal_action_identities"],
        }
        states_by_index[index] = state
        family_counts[state["family"]] += 1
        split_counts[state["split"]] += 1
        if state["split"] == "heldout":
            heldout_run_ids.add(state["source_run_id"])

    if sorted(states_by_index) != list(range(320)):
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T075 selected-state indices are not exactly 0..319")
    if any(state["action_trace_length"] <= 0 for state in states_by_index.values()):
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T075 selected states lack the retained replay action trace")

    owner_by_digest: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for group in ownership.get("groups", []):
        for member in group.get("members", []):
            if member.get("owner") is True:
                owner_by_digest[member.get("selection_digest")] = (group, member)
    ownership_mismatches = []
    for state in states_by_index.values():
        owned = owner_by_digest.get(state["selection_digest"])
        if owned is None:
            ownership_mismatches.append(state["selected_state_index"])
            continue
        group, member = owned
        if (
            group.get("family") != state["family"]
            or member.get("split") != state["split"]
            or member.get("simulator_seed") != state["simulator_seed"]
            or member.get("source_arm") != state["source_arm"]
        ):
            ownership_mismatches.append(state["selected_state_index"])
    if ownership_mismatches:
        raise Stage0Stop(
            "SOURCE_OR_TARGET_UNAVAILABLE",
            "T075 selected states do not map to their retained global-owner records",
            {"mismatched_selected_state_indices": ownership_mismatches},
        )
    heldout = [states_by_index[i] for i in range(320) if states_by_index[i]["split"] == "heldout"]
    heldout_family_counts = Counter(state["family"] for state in heldout)
    if len(heldout) != 64 or any(heldout_family_counts[f] != 16 for f in FAMILIES):
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T075 held-out cohort is not 16 states per frozen family")
    # Smoke membership is deterministic original-order selection, not outcome selection.
    smoke_indices_by_family: dict[str, list[int]] = {}
    for family in FAMILIES:
        family_states = [state for state in heldout if state["family"] == family]
        smoke_indices_by_family[family] = [state["selected_state_index"] for state in family_states[:2]]

    revalidation_entry = retained["current_native_revalidation"]
    revalidation = read_json(stsr_root / revalidation_entry["path"])
    if revalidation.get("schema_id") != "t089-current-native-revalidation-v1":
        raise Stage0Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "T089 revalidation schema mismatch")
    if revalidation.get("native_identity") != NATIVE_IDENTITY:
        raise Stage0Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "T089 revalidation native identity mismatch")
    if (
        revalidation.get("passed") is not True
        or revalidation.get("state_count") != 320
        or revalidation.get("mismatch_count") != 0
        or revalidation.get("dropped_state_count") != 0
        or revalidation.get("replacement_performed") is not False
        or revalidation.get("split_movement_count") != 0
    ):
        raise Stage0Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "T089 current-native revalidation did not pass exact 320-state gate")
    revalidation_rows = {
        row["selected_state_index"]: row for row in revalidation.get("rows", [])
    }
    if len(revalidation_rows) != 320:
        raise Stage0Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "T089 revalidation row coverage is not exactly 320")
    for state in states_by_index.values():
        row = revalidation_rows.get(state["selected_state_index"])
        if row is None:
            raise Stage0Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "T089 revalidation omitted a source state")
        if (
            row.get("family") != state["family"]
            or row.get("split") != state["split"]
            or row.get("simulator_seed") != state["simulator_seed"]
            or row.get("public_state_identity") != state["public_state_identity"]
            or row.get("ordered_legal_action_identities") != state["legal_action_identities"]
        ):
            raise Stage0Stop(
                "HISTORICAL_IDENTITY_UNAVAILABLE",
                f"T089 public/legal replay parity failed at state {state['selected_state_index']}",
            )
    advance_stage("ALL_320_PUBLIC_AND_LEGAL_IDENTITIES_VERIFIED")

    eligibility = read_json(stsr_root / retained["input_eligibility"]["path"])
    if eligibility.get("native_identity") != NATIVE_IDENTITY:
        raise Stage0Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "T089 input eligibility native identity mismatch")
    if eligibility.get("config", {}).get("heldout_expert_comparator_seed") != 895001:
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T089 held-out comparator seed is not 895001")

    selection = read_json(stsr_root / retained["validation_selection"]["path"])
    checkpoint = retained["checkpoint_893002"]
    if (
        selection.get("selected_model_seed") != 893002
        or selection.get("selected_checkpoint", {}).get("sha256") != checkpoint.get("sha256")
        or selection.get("selected_checkpoint", {}).get("size_bytes") != checkpoint.get("size_bytes")
    ):
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T089 validation-selected checkpoint identity mismatch")

    training = read_json(stsr_root / retained["training_batch_plans"]["path"])
    model_provenance = training.get("models", {}).get("893002", {})
    normalizer = model_provenance.get("normalizer_schema", {})
    target_identity = model_provenance.get("target_artifact_identity", {})
    source_provenance = model_provenance.get("source_artifact_identity", {})
    if (
        normalizer.get("state_mean_length") != 4737
        or normalizer.get("state_std_length") != 4737
        or normalizer.get("action_mean_length") != 92
        or normalizer.get("action_std_length") != 92
        or normalizer.get("embedded_in_checkpoint") is not True
    ):
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T089 seed 893002 checkpoint normalizer provenance is incomplete")
    if (
        target_identity.get("sha256") != retained["target_table"].get("sha256")
        or target_identity.get("size_bytes") != retained["target_table"].get("size_bytes")
        or target_identity.get("record_count") != 1111
    ):
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T089 trained model target-table provenance mismatch")
    if (
        source_provenance.get("sha256") != T075_SELECTION_SHA256
        or source_provenance.get("record_count") != 320
    ):
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T089 trained model source provenance mismatch")

    gate = read_json(stsr_root / retained["heldout_gate"]["path"])
    if (
        gate.get("schema_id") != "t089-heldout-gate-report-v1"
        or gate.get("passed") is not True
        or gate.get("selected_model_seed") != 893002
    ):
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T089 retained held-out gate is not the selected candidate pass")
    selected_rows = gate.get("model_results", {}).get("893002", [])
    if len(selected_rows) != 64:
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T089 selected-candidate held-out action identities lack 64 rows")
    gate_rows = {row.get("selected_state_index"): row for row in selected_rows}
    if set(gate_rows) != {state["selected_state_index"] for state in heldout}:
        raise Stage0Stop("HISTORICAL_IDENTITY_UNAVAILABLE", "T089 held-out gate states differ from exact T075 held-out cohort")
    for state in heldout:
        row = gate_rows[state["selected_state_index"]]
        expert_index = row.get("expert_action_index")
        model_index = row.get("model_action_index")
        legal = state["legal_action_identities"]
        eligible = state["eligible_action_indices"]
        if (
            row.get("split") != "heldout"
            or row.get("family") != state["family"]
            or row.get("public_state_identity") != state["public_state_identity"]
            or expert_index not in eligible
            or model_index not in eligible
            or row.get("expert_action_identity") != legal[expert_index]
            or row.get("model_action_identity") != legal[model_index]
        ):
            raise Stage0Stop(
                "HISTORICAL_IDENTITY_UNAVAILABLE",
                f"T089 held-out action identity mismatch at state {state['selected_state_index']}",
            )

    # Parse the retained target table only to audit coverage and per-seed shape.
    # Outcome values such as q_floor and terminal floors are deliberately never
    # read by this code and never participate in smoke-root selection.
    target_path = stsr_root / retained["target_table"]["path"]
    target_table_identity = read_top_level_value(target_path, "simulator_identity")
    target_source_identity = read_top_level_value(target_path, "source_artifact_identity")
    target_approved_spec = read_top_level_value(target_path, "approved_spec_commit")
    target_seed_map = read_top_level_value(target_path, "continuation_seed_map")
    target_execution = read_top_level_value(target_path, "execution_evidence")
    target_config = read_top_level_value(target_path, "frozen_config")
    target_model_schema = read_top_level_value(target_path, "model_input_schema")
    battle_config = target_config.get("battle", {})
    battle_actions = battle_config.get("action_space", {})
    expected_excluded_potions = {
        "game_potion_discard",
        "game_potion_use",
        "potion",
        "potion_discard",
        "reward_potion",
        "shop_reward_potion",
    }
    if (
        target_table_identity != NATIVE_IDENTITY
        or target_execution.get("native_identity") != NATIVE_IDENTITY
        or target_source_identity.get("sha256") != T075_SELECTION_SHA256
        or target_source_identity.get("size_bytes") != 226521456
        or target_source_identity.get("record_count") != 320
        or target_source_identity.get("schema_id") != "t065-source-state-v1"
        or target_approved_spec != T089_SPEC_COMMIT
        or target_seed_map.get("heldout") != HELDOUT_CONTINUATION_SEEDS
        or target_execution.get("schema_id") != "t089-target-generation-execution-v1"
        or target_execution.get("artifact_classification") != "formal_target_table"
        or target_execution.get("requested_state_count") != 320
        or target_execution.get("completed_state_count") != 320
        or target_execution.get("target_count") != 1111
        or target_execution.get("search_budget") != 400
        or target_execution.get("battle_controller_provenance") != battle_config
        or battle_config.get("controller") != "unguided_search_v2"
        or battle_config.get("implementation") != "BattleScumSearcher2"
        or battle_config.get("information_regime") != "full_simulator_state_oracle_like"
        or battle_config.get("version") != "search-v2"
        or battle_config.get("rollout_continuation") != "playoutRandom"
        or battle_config.get("root_selection") != "highest_mean"
        or battle_config.get("search_budget") != {"budget_unit": "native_tree_search_playouts", "simulations": 400}
        or battle_config.get("native_identity") != NATIVE_IDENTITY
        or battle_config.get("model_calls") != 0
        or battle_config.get("learned_leaf_value_callback") is not None
        or battle_config.get("policy_prior_callback") is not None
        or not expected_excluded_potions.issubset(set(battle_actions.get("excluded_kinds", [])))
        or target_config.get("approved_spec_commit") != T089_SPEC_COMMIT
        or target_config.get("native_identity") != NATIVE_IDENTITY
        or target_config.get("continuation_seeds", {}).get("heldout") != HELDOUT_CONTINUATION_SEEDS
        or target_config.get("heldout_expert_comparator_seed") != 895001
        or target_model_schema.get("schema_id") != "non-combat-model-input-v1"
        or target_model_schema.get("state_feature_size") != 4737
        or target_model_schema.get("action_feature_size") != 92
    ):
        raise Stage0Stop(
            "HISTORICAL_IDENTITY_UNAVAILABLE",
            "retained target table does not match the frozen T089 simulator/controller/model-input contract",
        )
    target_total_rows = 0
    heldout_target_rows: Counter[int] = Counter()
    heldout_target_actions: dict[int, set[int]] = {state["selected_state_index"]: set() for state in heldout}
    heldout_target_seed_shapes: Counter[tuple[int, ...]] = Counter()
    invalid_target_rows: list[dict[str, Any]] = []
    target_arrays = (
        "terminal_acts",
        "terminal_current_hps",
        "terminal_floors",
        "terminal_golds",
        "terminal_max_hps",
        "terminal_potion_counts",
        "terminal_statuses",
    )
    for target_row in iter_top_level_array(target_path, "targets"):
        target_total_rows += 1
        if target_row.get("split") != "heldout":
            continue
        state_index = target_row.get("selected_state_index")
        state = states_by_index.get(state_index)
        if state is None or state["split"] != "heldout":
            invalid_target_rows.append({"selected_state_index": state_index, "reason": "heldout target row has no source state"})
            continue
        action_index = target_row.get("legal_action_index")
        if not isinstance(action_index, int) or not 0 <= action_index < len(state["legal_action_identities"]):
            invalid_target_rows.append({"selected_state_index": state_index, "reason": "invalid legal action index"})
            continue
        action_identity = state["legal_action_identities"][action_index]
        seeds = target_row.get("continuation_seeds")
        heldout_target_rows[state_index] += 1
        if action_index in heldout_target_actions[state_index]:
            invalid_target_rows.append({"selected_state_index": state_index, "reason": "duplicate action target row"})
        heldout_target_actions[state_index].add(action_index)
        if isinstance(seeds, list):
            heldout_target_seed_shapes[tuple(seeds)] += 1
        if (
            target_row.get("schema_id") != "t065-counterfactual-target-table-v1"
            or target_row.get("family") != state["family"]
            or target_row.get("state_identity") != state["public_state_identity"]
            or target_row.get("legal_action_identity") != action_identity
            or target_row.get("continuation_seeds") != HELDOUT_CONTINUATION_SEEDS
            or target_row.get("target_status") != "complete"
            or target_row.get("problems")
            or any(not isinstance(target_row.get(name), list) or len(target_row[name]) != 4 for name in target_arrays)
        ):
            invalid_target_rows.append({"selected_state_index": state_index, "reason": "identity, seed, or per-branch target coverage mismatch"})

    expected_action_indices = {
        state["selected_state_index"]: set(state["eligible_action_indices"])
        for state in heldout
    }
    missing_target_actions = {
        index: sorted(expected_action_indices[index] - heldout_target_actions[index])
        for index in expected_action_indices
        if expected_action_indices[index] != heldout_target_actions[index]
    }
    expected_heldout_row_count = sum(len(indices) for indices in expected_action_indices.values())
    if (
        target_total_rows != 1111
        or sum(heldout_target_rows.values()) != expected_heldout_row_count
        or len(heldout_target_rows) != 64
        or missing_target_actions
        or invalid_target_rows
    ):
        raise Stage0Stop(
            "SOURCE_OR_TARGET_UNAVAILABLE",
            "retained T089 target table lacks exact held-out all-action, per-seed coverage",
            {
                "target_table_total_rows_observed": target_total_rows,
                "target_table_total_rows_expected": 1111,
                "heldout_target_rows_observed": sum(heldout_target_rows.values()),
                "heldout_target_rows_expected": expected_heldout_row_count,
                "heldout_states_with_target_rows": len(heldout_target_rows),
                "missing_action_indices_by_state": missing_target_actions,
                "invalid_target_rows": invalid_target_rows[:20],
                "continuation_seed_shapes": {str(list(key)): value for key, value in heldout_target_seed_shapes.items()},
            },
        )
    advance_stage("HELDOUT_ALL_ACTION_TARGETS_VERIFIED")

    target_status = read_json(stsr_root / retained["formal_target_status"]["path"])
    heldout_status = read_json(stsr_root / retained["heldout_status"]["path"])
    if target_status.get("state") != "SUCCEEDED" or target_status.get("exit_code") != 0:
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T089 retained formal target generation did not succeed")
    if heldout_status.get("state") != "SUCCEEDED" or heldout_status.get("exit_code") != 0:
        raise Stage0Stop("SOURCE_OR_TARGET_UNAVAILABLE", "T089 retained held-out target validation did not succeed")
    advance_stage("T089_CHECKPOINT_AND_HELDOUT_PROVENANCE_VERIFIED")

    commit_available = native_commit_available(args.native_repo)
    native_worktree_head = git_head(args.native_worktree)
    submodules = native_submodules(args.native_worktree)
    native_build_available = historical_build_available(args.historical_native_build)
    pinned_submodules_match = {
        row["path"]: row["commit"]
        for row in submodules["rows"]
        if row["path"] in PINNED_NATIVE_SUBMODULES
    } == PINNED_NATIVE_SUBMODULES
    exact_native_ready = (
        t089_code_commit_available
        and t089_spec_commit_available
        and commit_available
        and native_worktree_head == NATIVE_IDENTITY["commit"]
        and native_build_available
        and submodules["all_initialized"]
        and pinned_submodules_match
    )
    if exact_native_ready:
        terminal = "STAGE0_READY_FOR_SMOKE"
        blocker = None
    else:
        terminal = "HISTORICAL_IDENTITY_UNAVAILABLE"
        missing_dependencies = []
        if not t089_code_commit_available:
            missing_dependencies.append(f"accepted T089 policy commit {T089_CODE_HEAD} in the STSRL object database")
        if not t089_spec_commit_available:
            missing_dependencies.append(f"approved T089 spec commit {T089_SPEC_COMMIT} in the STSRL object database")
        if not commit_available:
            missing_dependencies.append(f"native source commit {NATIVE_IDENTITY['commit']} in {args.native_repo}")
        if native_worktree_head != NATIVE_IDENTITY["commit"]:
            missing_dependencies.append(f"native worktree at {NATIVE_IDENTITY['commit']} (observed {native_worktree_head})")
        if not native_build_available:
            missing_dependencies.append(f"historical native build directory {args.historical_native_build}")
        if not submodules["all_initialized"] or not pinned_submodules_match:
            observed_submodules = {row["path"]: row for row in submodules["rows"]}
            for name, commit in PINNED_NATIVE_SUBMODULES.items():
                row = observed_submodules.get(name)
                if row is None or not row["initialized"] or row["commit"] != commit:
                    missing_dependencies.append(f"initialized {name} submodule at {commit}")
        blocker = {
            "T089_accepted_code_commit_available": t089_code_commit_available,
            "T089_approved_spec_commit_available": t089_spec_commit_available,
            "native_source_commit_available": commit_available,
            "native_worktree_head": native_worktree_head,
            "native_worktree_head_matches_required_commit": native_worktree_head == NATIVE_IDENTITY["commit"],
            "historical_native_build_path": args.historical_native_build,
            "historical_native_build_available": native_build_available,
            "historical_native_submodules": submodules,
            "pinned_submodules_match": pinned_submodules_match,
            "required_pinned_submodules": PINNED_NATIVE_SUBMODULES,
            "missing_dependencies": missing_dependencies,
            "required_action": "restore the listed exact dependencies, then rerun Stage 0",
            "prohibited_substitute": "do not use the later 1d609c7d native or an unproven local extension",
        }

    report = {
        "schema_id": "issue-39-stage0-source-audit-v1",
        "schema_version": 1,
        "issue": ISSUE,
        "stage": "STAGE0_SOURCE_AVAILABILITY_AND_IDENTITY",
        "terminal": terminal,
        "source_paths": {
            "spire_research_head": spire_head,
            "expected_spire_research_base": expected_spire_base,
            "stsr_root": str(stsr_root),
            "T089_accepted_code_commit_available": t089_code_commit_available,
            "T089_approved_spec_commit_available": t089_spec_commit_available,
            "artifact_root": str(artifact_root),
            "native_repository": str(args.native_repo),
            "native_worktree": str(args.native_worktree),
        },
        "identity": {
            "T075_selection_sha256": T075_SELECTION_SHA256,
            "T089_accepted_code_head": T089_CODE_HEAD,
            "T089_approved_spec_commit": T089_SPEC_COMMIT,
            "native": NATIVE_IDENTITY,
            "T075_retention_manifest_sha256": sha256_file(t075_manifest_path),
            "T089_retention_manifest_sha256": sha256_file(t089_manifest_path),
        },
        "retained_artifacts": {
            "T075_selected_states": source_identity,
            "T089_required_roles": t089_checks,
            "T075_required_roles": t075_checks,
            "T089_target_record_count_from_training_provenance": target_identity.get("record_count"),
            "T089_model_checkpoint_seed": 893002,
            "T089_checkpoint_sha256": checkpoint.get("sha256"),
            "T089_normalizer_schema": {
                "state_width": normalizer.get("state_mean_length"),
                "action_width": normalizer.get("action_mean_length"),
                "embedded": normalizer.get("embedded_in_checkpoint"),
            },
        },
        "frozen_T089_contract": {
            "native_identity": target_table_identity,
            "target_generation_code_identity": target_execution.get("code_identity"),
            "target_source_identity": target_source_identity,
            "approved_spec_commit": target_approved_spec,
            "battle_controller": battle_config,
            "heldout_continuation_seeds": target_seed_map.get("heldout"),
            "heldout_expert_comparator_seed": target_config.get("heldout_expert_comparator_seed"),
            "model_input_schema": {
                "schema_id": target_model_schema.get("schema_id"),
                "state_feature_size": target_model_schema.get("state_feature_size"),
                "action_feature_size": target_model_schema.get("action_feature_size"),
            },
        },
        "cohort": {
            "T075_selected_state_count": len(states_by_index),
            "family_counts_all_splits": dict(sorted(family_counts.items())),
            "split_counts": dict(sorted(split_counts.items())),
            "heldout_state_count": len(heldout),
            "heldout_family_counts": dict(sorted(heldout_family_counts.items())),
            "distinct_heldout_source_run_count": len(heldout_run_ids),
            "public_state_and_ordered_legal_action_parity_verified": True,
            "selected_state_global_owner_membership_verified": len(states_by_index),
            "historical_expert_comparator_action_identities_verified": len(selected_rows),
            "model_action_identities_verified": len(selected_rows),
            "target_table_total_rows_verified": target_total_rows,
            "heldout_target_rows_per_eligible_action": sum(heldout_target_rows.values()),
            "heldout_target_rows_expected": expected_heldout_row_count,
            "heldout_target_states_verified": len(heldout_target_rows),
            "heldout_target_seed_shapes": {str(list(key)): value for key, value in heldout_target_seed_shapes.items()},
            "heldout_comparator_seed": 895001,
            "preselected_smoke_states_per_family": 2,
            "smoke_state_indices_by_family": smoke_indices_by_family,
        },
        "native_readiness": {
            "T089_accepted_code_commit_available": t089_code_commit_available,
            "T089_approved_spec_commit_available": t089_spec_commit_available,
            "source_commit_available_in_local_git_object_database": commit_available,
            "worktree_head": native_worktree_head,
            "worktree_head_matches_required_commit": native_worktree_head == NATIVE_IDENTITY["commit"],
            "historical_native_build_path": args.historical_native_build,
            "historical_native_build_available": native_build_available,
            "pinned_submodules": submodules,
            "pinned_submodules_match": pinned_submodules_match,
            "required_pinned_submodules": PINNED_NATIVE_SUBMODULES,
            "exact_native_ready_for_smoke": exact_native_ready,
        },
        "execution": {
            "eight_state_smoke_executed": False,
            "stage1_attempts": 0,
            "new_simulator_runs": 0,
            "reason": blocker["required_action"] if blocker else None,
        },
        "blocker": blocker,
        "limitations": [
            "The source commit exists, but no provenance-qualified T089 native binary is retained at the path recorded by T089.",
            "The exact native worktree has uninitialized pinned JSON and pybind11 submodules; GitHub fetches for those dependencies failed in this run.",
            "No result is reported for changed-continuation outcomes; current/later native builds were not substituted.",
            "The T089 retained held-out gate and target-table hashes were verified; this audit did not recompute targets or use them as model input.",
        ],
    }

    output_dir = Path(__file__).resolve().parent
    report_path = output_dir / "source-audit.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    LAST_COMPLETED_STAGE = "STAGE0_SOURCE_AVAILABILITY_AND_IDENTITY"
    persist_run_state(terminal)
    write_output_manifest()
    print(json.dumps({"terminal": terminal, "heldout_states": len(heldout), "source_rows": len(states_by_index), "stage1_attempts": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Stage0Stop as stop:
        output_dir = Path(__file__).resolve().parent
        output_dir.mkdir(parents=True, exist_ok=True)
        persist_run_state(stop.terminal, terminal_detail=str(stop))
        report = {
            "schema_id": "issue-39-stage0-source-audit-v1",
            "schema_version": 1,
            "issue": ISSUE,
            "stage": "STAGE0_SOURCE_AVAILABILITY_AND_IDENTITY",
            "terminal": stop.terminal,
            "last_completed_stage": LAST_COMPLETED_STAGE,
            "blocker": {"reason": str(stop), **stop.evidence},
            "execution": {
                "eight_state_smoke_executed": False,
                "stage1_attempts": 0,
                "new_simulator_runs": 0,
            },
        }
        (output_dir / "source-audit.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        write_output_manifest()
        print(json.dumps({"terminal": stop.terminal, "reason": str(stop), "stage1_attempts": 0}, sort_keys=True))
        raise SystemExit(0)
    except Exception as exc:  # unexpected audit failure; retain a coarse crash boundary
        try:
            persist_run_state("STAGE0_FAILED", f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        print(f"STAGE0_AUDIT_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
