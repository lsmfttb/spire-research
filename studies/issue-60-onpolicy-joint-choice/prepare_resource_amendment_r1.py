"""Freeze exact original-evidence anchors and the Planner's R1 whitelist."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from resource_amendment_r1 import (
    PARENT_HANDOFF, ORIGINAL_PRODUCER, R1_ADDED_RESOURCE_ARTIFACTS, R1_ADDED_SOURCE_PATHS,
    R1_MODIFIED_SOURCE_PATHS, R1_DELETED_SOURCE_PATHS, R1_RESOURCE_VALUES,
)


HERE = Path(__file__).resolve().parent
ISSUE_URL = "https://github.com/lsmfttb/spire-research/issues/60"
PLANNER_COMMENT_URL = f"{ISSUE_URL}#issuecomment-6208819496"
PLANNER_COMMENT_ID = 6208819496
SCIENTIFIC_FILES = (
    "adapter_v47.py", "bootstrap_v47.py", "bootstrap/A.json", "config.json",
    "controllers.py", "encoder.py", "freeze_manifest.py", "model.py",
    "normalizer_v49.py", "preflight_witness.py", "public_resources.py",
    "trainer.py", "vocabulary.json",
)


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main() -> None:
    old_manifest_path = HERE / "frozen-manifest.json"
    old_gate_path = HERE / "evidence" / "preflight" / "native.json"
    old_synthetic_path = HERE / "evidence" / "preflight" / "synthetic.json"
    old_synthetic_status_path = HERE / "evidence" / "stage-status" / "synthetic.json"
    old_preflight_status_path = HERE / "evidence" / "stage-status" / "preflight.json"
    old_config = (HERE / "config.json").read_bytes()
    old_manifest_bytes = old_manifest_path.read_bytes()
    old_gate_bytes = old_gate_path.read_bytes()
    old_manifest = json.loads(old_manifest_bytes)
    ledger = (HERE / "evidence" / "cpu-ledger.jsonl").read_bytes()
    ledger_lines = ledger.splitlines(keepends=True)
    if len(ledger_lines) != 8:
        raise SystemExit(f"expected the original eight-row CPU history before R1, found {len(ledger_lines)}")
    if (old_manifest.get("implementation_commit") != ORIGINAL_PRODUCER
            or old_manifest.get("config_sha256") != "07020535931cd81bef9b40fa1c30e75b553ac160bc53057e58f82bd8cf2007ca"):
        raise SystemExit("original frozen manifest is not the accepted Issue 60 producer/config")
    old_files = {entry["path"]: entry["sha256"] for entry in old_manifest["files"]}
    if old_files.get("run_study.py") is None:
        raise SystemExit("original manifest does not freeze run_study.py")
    payload = {
        "schema_id": "issue60-resource-amendment-r1-v1",
        "issue": 60,
        "amendment_id": "R1",
        "planner_decision": {
            "url": PLANNER_COMMENT_URL,
            "comment_id": PLANNER_COMMENT_ID,
            "status": "ORIGINAL_RESOURCE_BLOCKED_STOP_ACCEPTED_R1_CONTINUATION_AUTHORIZED",
            "study_disposition": "STUDY_ONLY",
        },
        "parent_handoff_commit": PARENT_HANDOFF,
        "original_producer_commit": ORIGINAL_PRODUCER,
        "original_config_sha256": sha(old_config),
        "original_manifest_sha256": sha(old_manifest_bytes),
        "original_preflight_gate_sha256": sha(old_gate_bytes),
        "original_synthetic_witness_sha256": sha(old_synthetic_path.read_bytes()),
        "original_synthetic_stage_status_sha256": sha(old_synthetic_status_path.read_bytes()),
        "original_preflight_stage_status_sha256": sha(old_preflight_status_path.read_bytes()),
        "original_runner_sha256": old_files["run_study.py"],
        "original_cpu_ledger_line_count": len(ledger_lines),
        "original_cpu_ledger_prefix_bytes": len(ledger),
        "original_cpu_ledger_prefix_sha256": sha(ledger),
        "unchanged_scientific_files": list(SCIENTIFIC_FILES),
        "resource": R1_RESOURCE_VALUES,
        "source_diff_whitelist": {
            "modified_paths": list(R1_MODIFIED_SOURCE_PATHS),
            "added_paths": [*R1_ADDED_SOURCE_PATHS, *R1_ADDED_RESOURCE_ARTIFACTS],
            "deleted_paths": list(R1_DELETED_SOURCE_PATHS),
            "modified_runner_symbols": ["REVIEWER_RESERVE", "PROCESS_BUDGET", "run_training", "main", "run_game"],
            "permitted_runner_semantics": [
                "change cumulative cap and review reserve constants only",
                "replace the original RESOURCE_BLOCKED training consumer with a verified R1 gate consumer",
                "add separate R1 resource-check/seal stages and route old preflight to a fail-closed stop",
                "write repeat synthetic evidence to an R1-specific path, preserving the original witness",
                "finalize each attempt status from its terminal run summary without changing game or training decisions",
            ],
        },
        "preservation_rules": {
            "original_config_path": "config.json",
            "original_manifest_path": "frozen-manifest.json",
            "original_blocked_gate_path": "evidence/preflight/native.json",
            "original_cpu_ledger_prefix_is_immutable": True,
            "old_RESOURCE_BLOCKED_receipt_must_not_be_relabelled": True,
            "original_eight_preflight_games_are_reused": True,
            "no_new_native_preflight_game": True,
        },
    }
    destination = HERE / "resource-amendment-r1.json"
    encoded = json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n"
    destination.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")


if __name__ == "__main__":
    main()
