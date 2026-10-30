#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export STS_LIGHTSPEED_SOURCE_DIR="/mnt/d/DeadlycatCoding/stsrl-data/issue81/sources/sts_lightspeed"
export STSRL_SOURCE_DIR="/mnt/d/DeadlycatCoding/stsrl-data/issue81/sources/STSRL"
exec "/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/bin/python3.13" "$ROOT/studies/issue-83-pgonly-scale/driver.py" "$@"
