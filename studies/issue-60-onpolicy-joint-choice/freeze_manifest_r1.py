"""Freeze R1's resource-only source patch and authorization receipt."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from resource_amendment_r1 import (
    ORIGINAL_MANIFEST, PARENT_HANDOFF, R1_ADDED_SOURCE_PATHS,
    R1_AUTHORIZATION, R1_MANIFEST,
    _git, _git_bytes, _study_git_path, verify_runner_whitelist,
)


HERE = Path(__file__).resolve().parent
PATCH_PATH = "evidence/resource-amendment-r1/runner.patch"


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_paths() -> tuple[str, ...]:
    original = json.loads((HERE / ORIGINAL_MANIFEST).read_text(encoding="utf-8"))
    return tuple(sorted({entry["path"] for entry in original["files"]}
                        | set(R1_ADDED_SOURCE_PATHS)))


def freeze_manifest() -> dict:
    amendment = json.loads((HERE / "resource-amendment-r1.json").read_text(encoding="utf-8"))
    producer = _git(HERE, "rev-parse", "HEAD")
    changed = _git(HERE, "status", "--porcelain", "--", _study_git_path(HERE, "."))
    if changed:
        raise SystemExit("R1 source producer must be committed and source-clean before freezing")
    actual_diff = _git(HERE, "diff", "--name-status", "--no-renames", PARENT_HANDOFF,
                       producer, "--", _study_git_path(HERE, "."))
    expected = {path: "M" for path in amendment["source_diff_whitelist"]["modified_paths"]}
    expected.update({path: "A" for path in amendment["source_diff_whitelist"]["added_paths"]})
    expected.update({path: "D" for path in amendment["source_diff_whitelist"]["deleted_paths"]})
    actual = {}
    for line in actual_diff.splitlines():
        status, path = line.split("\t", 1)
        actual[path.removeprefix(_study_git_path(HERE, ".").removesuffix("/." ) + "/")] = status
    if actual != expected:
        raise SystemExit(f"resource-only path whitelist mismatch: expected {expected}, observed {actual}")

    old_runner = _git_bytes(HERE, "show", f"{PARENT_HANDOFF}:{_study_git_path(HERE, 'run_study.py')}")
    current_runner = (HERE / "run_study.py").read_bytes()
    verify_runner_whitelist(old_runner, current_runner)
    patch = _git_bytes(HERE, "diff", "--no-ext-diff", "--binary", PARENT_HANDOFF, producer,
                       "--", _study_git_path(HERE, "run_study.py"))
    patch_file = HERE / PATCH_PATH
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_bytes(patch)
    original = json.loads((HERE / ORIGINAL_MANIFEST).read_text(encoding="utf-8"))
    entries = []
    for relative in _source_paths():
        path = HERE / relative
        if not path.is_file():
            raise SystemExit(f"R1 frozen source is missing: {relative}")
        content = path.read_bytes()
        producer_content = _git_bytes(HERE, "show", f"{producer}:{_study_git_path(HERE, relative)}")
        if content != producer_content:
            raise SystemExit(f"R1 source does not match the producer commit: {relative}")
        entries.append({"path": relative, "sha256": sha(content)})
    manifest = {
        "schema_id": "issue60-resource-frozen-manifest-r1-v1",
        "purpose": "STUDY_ONLY",
        "issue": 60,
        "amendment_id": "R1",
        "implementation_commit": producer,
        "base_handoff_commit": PARENT_HANDOFF,
        "original_producer_commit": original["implementation_commit"],
        "original_config_sha256": original["config_sha256"],
        "original_manifest_sha256": sha((HERE / ORIGINAL_MANIFEST).read_bytes()),
        "amendment_config_sha256": sha((HERE / "resource-amendment-r1.json").read_bytes()),
        "source_pins": original["source_pins"],
        "files": entries,
        "runner_diff_path": PATCH_PATH,
        "runner_diff_sha256": sha(patch),
        "source_diff_whitelist": amendment["source_diff_whitelist"],
        "freeze_rule": "R1 changes resource authorization/provenance only; original study config and scientific source remain frozen.",
    }
    out = HERE / R1_MANIFEST
    out.write_text(json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False))
    return manifest


def freeze_authorization() -> dict:
    amendment = json.loads((HERE / "resource-amendment-r1.json").read_text(encoding="utf-8"))
    manifest_path = HERE / R1_MANIFEST
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if _git_bytes(HERE, "show", f"HEAD:{_study_git_path(HERE, R1_MANIFEST)}") != manifest_bytes:
        raise SystemExit("commit the exact R1 source manifest and runner patch before freezing authorization")
    if manifest["implementation_commit"] != _git(HERE, "rev-parse", f"{manifest['implementation_commit']}^{0}"):
        raise SystemExit("R1 implementation commit cannot be resolved")
    receipt = {
        "schema_id": "issue60-resource-authorization-r1-v1",
        "issue": 60, "amendment_id": "R1", "status": "PLANNER_AUTHORIZED_RESOURCE_ONLY_CONTINUATION",
        "planner_decision_url": amendment["planner_decision"]["url"],
        "planner_decision_comment_id": amendment["planner_decision"]["comment_id"],
        "parent_handoff_commit": PARENT_HANDOFF,
        "original_producer_commit": amendment["original_producer_commit"],
        "producer_commit": manifest["implementation_commit"],
        "original_config_sha256": amendment["original_config_sha256"],
        "original_manifest_sha256": amendment["original_manifest_sha256"],
        "original_preflight_gate_sha256": amendment["original_preflight_gate_sha256"],
        "original_cpu_ledger_prefix_sha256": amendment["original_cpu_ledger_prefix_sha256"],
        "amendment_config_sha256": sha((HERE / "resource-amendment-r1.json").read_bytes()),
        "frozen_manifest_sha256": sha(manifest_bytes),
        "runner_diff_path": manifest["runner_diff_path"],
        "runner_diff_sha256": manifest["runner_diff_sha256"],
        "resource": amendment["resource"],
        "source_diff_whitelist": amendment["source_diff_whitelist"],
        "explicitly_unchanged": amendment["preservation_rules"],
        "review_reserve_accounting": {
            "total_review_reserve_cpu_seconds": 20000.0,
            "prior_reviewer_charge_or_conservative_allowance_cpu_seconds": 1000.0,
            "prior_reviewer_allowance_basis": "conservative allowance pending exact independent-review process CPU; charged within the review reserve, not the Builder allowance",
            "remaining_future_reviewer_reserve_cpu_seconds": 19000.0,
        },
        "scientific_design_changes": [],
    }
    out = HERE / R1_AUTHORIZATION
    out.write_text(json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False))
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorization", action="store_true")
    args = parser.parse_args()
    if args.authorization:
        freeze_authorization()
    else:
        freeze_manifest()


if __name__ == "__main__":
    main()
