from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RUNNER = ROOT / "runner.py"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run production only with the exact preflighted Issue 64 runner")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-source", default="/home/lsmft/stsrl-spikes/sts_lightspeed-issue59")
    parser.add_argument("--stsrl-source", default="/home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c")
    parser.add_argument("--workers", type=int)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    preflight_path = output / "preflight-decision.json"
    if not preflight_path.is_file():
        raise SystemExit("preflight-decision.json is missing")
    decision = json.loads(preflight_path.read_text(encoding="utf-8"))
    if decision.get("schema_id") != "issue65-preflight-decision-v2":
        raise SystemExit("preflight decision is legacy or has an unknown schema")
    if not decision.get("formal_production_authorized_by_preflight"):
        raise SystemExit("preflight did not authorize the frozen 64-owner production")
    actual = sha256_file(RUNNER)
    runtime_module = ROOT / "public_history_execution.py"
    actual_runtime = sha256_file(runtime_module) if runtime_module.is_file() else None
    source_identity = decision.get("source_identity")
    if (
        not isinstance(source_identity, dict)
        or not isinstance(source_identity.get("native_identity"), dict)
    ):
        raise SystemExit("preflight decision has no complete source identity")
    if (
        source_identity.get("runner_sha256") != actual
        or decision.get("runner_sha256") != actual
    ):
        raise SystemExit(
            f"runner differs from preflighted source: expected={source_identity.get('runner_sha256')}, actual={actual}"
        )
    expected_runtime = source_identity.get("optimizer_module_sha256")
    if (
        not expected_runtime
        or actual_runtime != expected_runtime
        or decision.get("optimizer_module_sha256") != actual_runtime
    ):
        raise SystemExit(
            "Issue 65 runtime differs from preflighted source: "
            f"expected={expected_runtime}, actual={actual_runtime}"
        )
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    config_digest = hashlib.sha256(
        json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        decision.get("config_sha256") != config_digest
        or source_identity.get("config_sha256") != config_digest
    ):
        raise SystemExit("preflight decision config differs from the installed Issue 65 config")
    command = [
        sys.executable,
        str(RUNNER),
        "production",
        "--output",
        str(output),
        "--native-source",
        args.native_source,
        "--stsrl-source",
        args.stsrl_source,
    ]
    if args.workers is not None:
        command.extend(["--workers", str(args.workers)])
    print(json.dumps({
        "event": "production_launch",
        "runner_sha256": actual,
        "optimizer_module_sha256": actual_runtime,
        "preflight_decision": str(preflight_path),
        "command": command,
    }, sort_keys=True), flush=True)
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
