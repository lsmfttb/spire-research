#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA=/mnt/d/DeadlycatCoding/stsrl-data/issue81
PYTHON=/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/bin/python3.13
export STS_LIGHTSPEED_SOURCE_DIR="$DATA/sources/sts_lightspeed"
export STSRL_SOURCE_DIR="$DATA/sources/STSRL"
export PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$DATA/cache/pycache"
export XDG_CACHE_HOME="$DATA/cache/xdg" TORCH_HOME="$DATA/cache/torch"
export TMPDIR="$DATA/tmp" TMP="$DATA/tmp" TEMP="$DATA/tmp"
export LD_PRELOAD=/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/lib/libffi.so.8.2.0:/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/lib/libstdc++.so.6.0.34:/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/lib/libz.so.1.3.2:/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/lib/libgomp.so.1.0.0
mkdir -p "$DATA/cache/pycache" "$DATA/cache/xdg" "$DATA/cache/torch" "$DATA/tmp"
exec "$PYTHON" "$ROOT/studies/issue-81-pg-only/driver.py" "$@"
