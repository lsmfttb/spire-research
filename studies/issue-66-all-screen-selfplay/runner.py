"""Fail-closed native-source preflight for the Issue 66 study."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from source_audit import audit_native_source


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
IDENTITY = CONFIG["identity"]
STUDY_ID = CONFIG["study_id"]
MAX_STEPS = 4096


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def git_identity(path: str) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True).strip()
    return {"path": str(root), "commit": commit, "clean": not bool(dirty)}


def validate_environment(native_source: str, stsrl_source: str) -> dict[str, Any]:
    native = git_identity(native_source)
    stsrl = git_identity(stsrl_source)
    if native["commit"] != IDENTITY["native_commit"] or not native["clean"]:
        raise RuntimeError("pinned native source identity is unavailable or dirty")
    if stsrl["commit"] != IDENTITY["stsrl_source_commit"] or not stsrl["clean"]:
        raise RuntimeError("pinned STSRL source identity is unavailable or dirty")
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError(f"Python 3.14 required, got {sys.version.split()[0]}")
    module = importlib.import_module("slaythespire")
    module_path = Path(module.__file__).resolve()
    module_sha = sha256_file(module_path)
    if module_sha != IDENTITY["native_binary_sha256"]:
        raise RuntimeError(f"native extension SHA-256 mismatch: {module_sha}")
    return {
        "native_repository": IDENTITY["native_repository"],
        "native_branch": IDENTITY["native_branch"],
        "native_source": native,
        "native_binary_path": str(module_path),
        "native_binary_sha256": module_sha,
        "stsrl_source": stsrl,
        "python": sys.version.split()[0],
    }


def ensure_external(output: Path) -> None:
    resolved = output.expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise RuntimeError("study artifacts must remain outside the disposable worktree")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(canonical(value) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def source_identity(native_source: str, stsrl_source: str) -> dict[str, Any]:
    env = validate_environment(native_source, stsrl_source)
    config_bytes = CONFIG_PATH.read_bytes()
    return {
        **env,
        "config_sha256": sha256_bytes(config_bytes),
        "runner_sha256": sha256_file(Path(__file__).resolve()),
        "source_audit_sha256": sha256_file(ROOT / "source_audit.py"),
    }


def run_preflight(output: Path, native_source: str, stsrl_source: str) -> dict[str, Any]:
    started = time.process_time()
    ensure_external(output)
    output = output.expanduser().resolve()
    identity = source_identity(native_source, stsrl_source)
    audit = audit_native_source(native_source, IDENTITY["native_commit"])
    eligible = audit["status"] == "SUPPORTED"
    decision = {
        "schema_id": "issue66-preflight-decision-v1",
        "study_id": STUDY_ID,
        "study_status": "SOURCE_PREFLIGHT_ONLY" if eligible else "SCIENTIFIC_INCOMPLETE",
        "formal_execution_authorized_by_preflight": False,
        "training_authorized": False,
        "evaluation_authorized": False,
        "preflight_seed_owners_consumed": [],
        "source_identity": identity,
        "config_sha256": identity["config_sha256"],
        "runner_sha256": identity["runner_sha256"],
        "source_audit_sha256": identity["source_audit_sha256"],
        "semantic_source_audit": audit,
        "cpu_seconds": max(0.0, time.process_time() - started),
        "completed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "blocker": audit.get("blocker"),
        "proposed_disposition": (
            "review this bounded source failure; a complete study needs a separately reviewed native semantic-choice capability and source re-pin"
            if not eligible else
            "source-level semantic capability present; this record alone does not authorize production"
        ),
    }
    # A failed preflight is an immutable study endpoint. Never replace it with
    # a different source or start the registered cohort in this output folder.
    output.mkdir(parents=True, exist_ok=True)
    decision_path = output / "preflight-decision.json"
    if decision_path.exists():
        old = json.loads(decision_path.read_text(encoding="utf-8"))
        if old.get("source_identity") != identity or old.get("schema_id") != decision["schema_id"]:
            raise RuntimeError("existing preflight belongs to a different source identity")
        return old
    manifest = {
        "schema_id": "issue66-study-manifest-v1",
        "study_id": STUDY_ID,
        "issue_url": "https://github.com/lsmfttb/spire-research/issues/66",
        "artifact_consumer": "Issue 66 independent Reviewer and Planner",
        "delete_after": CONFIG["retention"]["delete_after"],
        "config": CONFIG,
        "source_identity": identity,
        "preflight_decision_sha256": sha256_bytes(canonical(decision).encode("utf-8")),
        "study_status": decision["study_status"],
        "registered_training_owners_consumed": 0,
        "registered_evaluation_owners_consumed": 0,
    }
    atomic_json(output / "study-manifest.json", manifest)
    atomic_json(decision_path, decision)
    append_jsonl(output / "study-events.jsonl", {
        "event": "preflight_bounded_failure" if not eligible else "preflight_source_gate_passed",
        "study_id": STUDY_ID,
        "native_commit": identity["native_source"]["commit"],
        "source_audit_status": audit["status"],
        "blocker": audit.get("blocker"),
        "training_owners_started": 0,
        "evaluation_owners_started": 0,
        "cpu_seconds": decision["cpu_seconds"],
    })
    return decision


def run_production(output: Path) -> None:
    decision_path = output.expanduser().resolve() / "preflight-decision.json"
    if not decision_path.is_file():
        raise RuntimeError("preflight-decision.json is missing")
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    if decision.get("schema_id") != "issue66-preflight-decision-v1":
        raise RuntimeError("preflight decision is legacy or has an unknown schema")
    if not decision.get("formal_execution_authorized_by_preflight"):
        raise RuntimeError("Issue 66 training/evaluation is fail-closed by preflight")
    raise RuntimeError("production runner is intentionally not enabled by this source audit")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--output", type=Path, required=True)
    preflight.add_argument("--native-source", required=True)
    preflight.add_argument("--stsrl-source", required=True)
    production = commands.add_parser("production")
    production.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            result = run_preflight(args.output, args.native_source, args.stsrl_source)
        else:
            run_production(args.output)
            return 0
    except Exception as exc:
        print(json.dumps({"event": "issue66_fail_closed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["study_status"] == "SOURCE_PREFLIGHT_ONLY" else 3


if __name__ == "__main__":
    raise SystemExit(main())
