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
    if not decision.get("formal_production_authorized_by_preflight"):
        raise SystemExit("preflight did not authorize the frozen 64-owner production")
    expected = decision.get("runner_sha256")
    actual = sha256_file(RUNNER)
    if not expected or actual != expected:
        raise SystemExit(
            f"runner differs from preflighted source: expected={expected}, actual={actual}"
        )
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
        "preflight_decision": str(preflight_path),
        "command": command,
    }, sort_keys=True), flush=True)
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())