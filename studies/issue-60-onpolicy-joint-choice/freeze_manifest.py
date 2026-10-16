"""Create the committed code/config lock before any native preflight game."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess


HERE = Path(__file__).resolve().parent
FILES = (
    "adapter_v47.py", "bootstrap_v47.py", "bootstrap/A.json", "config.json",
    "controllers.py", "encoder.py", "freeze_manifest.py", "model.py",
    "normalizer_v49.py", "preflight_witness.py", "run_study.py", "trainer.py",
    "vocabulary.json",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    status = subprocess.run(
        ["git", "-C", str(HERE), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    if status:
        raise SystemExit("commit the complete source/config first; worktree must be clean before freezing")
    implementation_commit = subprocess.run(
        ["git", "-C", str(HERE), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    config = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    files = []
    for relative in FILES:
        path = HERE / relative
        if not path.is_file():
            raise SystemExit(f"required frozen input is missing: {relative}")
        files.append({"path": relative, "sha256": _sha(path)})
    manifest = {
        "schema_id": "issue60-frozen-manifest-v1",
        "purpose": "STUDY_ONLY",
        "issue": 60,
        "implementation_commit": implementation_commit,
        "base_commit": config["base"]["commit"],
        "source_pins": config["sources"],
        "config_sha256": _sha(HERE / "config.json"),
        "files": files,
        "freeze_rule": "No code, config, schema, vocabulary, or checkpoint bytes may change after a simulator preflight game.",
    }
    destination = HERE / "frozen-manifest.json"
    if destination.exists():
        raise SystemExit("a frozen manifest already exists; do not silently replace the registered program")
    payload = json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n"
    temp = destination.with_name(destination.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)
        stream.flush()
    temp.replace(destination)
    print(payload, end="")


if __name__ == "__main__":
    main()
