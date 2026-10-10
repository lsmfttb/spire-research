# Issue 39: continuation-policy mismatch

This disposable study audits whether the frozen T089 Oracle Battle checkpoint,
held-out cohort, and exact historical simulator build are locally available
before any changed-continuation smoke run. It does not train a model, generate
targets, choose states from outcome values, or start a simulator.

The audit reads the retained T075 selected-state JSONL and T089 retention
manifest artifacts under `STSR_ROOT`. It verifies their recorded sizes and
SHA-256 hashes, the 320-state cohort and 64-state held-out split, the selected
checkpoint 893002 and target provenance, and each held-out public-state/legal-
action identity. It checks target-table row coverage, continuation-seed lists,
and per-seed result-array lengths without inspecting outcome values. It only
reads the retained gate's expert/model action indices and identities. Model
weights are hashed but never loaded.

Run from a Python environment with Git available:

```sh
python3 studies/issue-39-continuation-mismatch/audit_stage0.py \
  --repository-root /path/to/spire-research \
  --stsr-root /path/to/STSRL \
  --native-repo /path/to/sts_lightspeed \
  --native-worktree /path/to/sts_lightspeed-at-20a6c2b \
  --historical-native-build /path/to/sts_lightspeed-at-20a6c2b/build-py
```

The required native identity is `lsmfttb/sts_lightspeed`,
`refs/heads/stsrl/main@20a6c2b3a9cea817c988178b814f083ff889853f`, with pinned
JSON and pybind11 submodules. The audit reports
`HISTORICAL_IDENTITY_UNAVAILABLE` if the accepted T089 source commits, exact
native worktree, pinned submodules, or documented build directory are missing.
That terminal state is a stop condition: do not substitute a later native
build. Only `STAGE0_READY_FOR_SMOKE` permits the separately gated eight-state
smoke to be considered; it does not authorize Stage 1.

Outputs are `source-audit.json`, `run-state.json`, and `output-manifest.json`
beside the script. Run-state is updated at each coarse audit boundary so an
interruption can be located without repeating a diagnostic study.
