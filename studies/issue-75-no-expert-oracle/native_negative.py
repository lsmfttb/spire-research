from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

from preflight import binary_sha256, load_json, write_locked_file

STUDY = Path(__file__).resolve().parent


def run_negative_check() -> dict[str, Any]:
    config = load_json(STUDY / "config.json")
    native = config["source"]
    actual_python_version = sys.version.split()[0]
    if actual_python_version != str(native["python_version"]):
        raise RuntimeError("pinned native module must be checked with its exact Python version")
    import numpy
    import torch
    actual_numpy_version = str(numpy.__version__)
    actual_torch_version = str(torch.__version__)
    if actual_numpy_version != str(native["numpy_version"]):
        raise RuntimeError("pinned native module must be checked with its exact NumPy version")
    if actual_torch_version != str(native["torch_version"]):
        raise RuntimeError("pinned native module must be checked with its exact PyTorch version")
    path = Path(str(native["native_module_path"])).expanduser().resolve(strict=True)
    digest = binary_sha256(path)
    if digest != native["native_module_sha256"]:
        raise RuntimeError("pinned native module binary SHA-256 mismatch")
    spec = importlib.util.spec_from_file_location("slaythespire", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("pinned native extension could not be imported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    required = (
        hasattr(module, "StepSimulator")
        and hasattr(module.StepSimulator, "battle_search_v2")
        and hasattr(module, "PrivilegedNoncombatProjectionAccess")
    )
    if not required:
        raise RuntimeError("pinned native extension lacks required reviewed APIs")
    # Deliberately inspect names only: no StepSimulator, game, checkpoint, projection, or policy is constructed.
    if not isinstance(module.StepSimulator, type):
        raise RuntimeError("native StepSimulator API is not an inspectable type")
    return {
        "status": "PINNED_NATIVE_IMPORT_API_NEGATIVE_PASS",
        "native_module_sha256": digest,
        "native_source_commit": native["sts_lightspeed_commit"],
        "python_version": actual_python_version,
        "numpy_version": actual_numpy_version,
        "torch_version": actual_torch_version,
        "import_only": True,
        "simulator_instances_created": 0,
        "games_started": 0,
        "checkpoints_loaded_or_created": 0,
        "privileged_projections_read": 0,
        "expert_instances_created_or_called": 0,
        "formal_seeds_consumed": 0,
    }


def main() -> int:
    result = run_negative_check()
    target = STUDY / "native-negative-test.json"
    write_locked_file(target, result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
