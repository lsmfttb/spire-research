# spire-research

A lightweight research platform for search and learning agents in **Slay the Spire**.

The project goal is an agent that can eventually win A20 Heart runs while acting from normal player-visible information. Search is the primary battle-policy direction; learned models may guide or accelerate search. Privileged simulator information may be used for training or diagnostics only when it is explicitly separated from deployable normal-information behavior.

## Why this repository exists

This repository is a clean restart. It intentionally does **not** inherit the full software, task-document, compatibility, or workflow structure of the previous research codebase.

The active system should stay small enough that a new contributor or coding agent can understand the relevant path from the current issue and the code it touches, without reconstructing years of task history.

**The accepted STSRL runtime and experiment record are the reuse-first baseline.** The existing STSRL `execute_controlled_run`, its separately named Battle/Non-Combat controllers, and the simulator commit pinned in STSRL's `docs/sts_lightspeed_source_manifest.json` already support full-run simulation and source/evaluation workflows. Reuse working capabilities and accepted positive/negative findings; do not wholesale port old task scaffolding. The `sts_lightspeed:spire/main` public-projection-v3 path is **retired as an integration/development direction**; its prior SHA is preserved on `archive/spire-v3-2026-10-09` for study reproducibility.

## Working model

Research studies and platform code have different lifecycles.

A study may use a disposable branch/worktree and one-off code to answer a scientific question. Study code does not enter the long-lived platform merely because the study completed.

Code is promoted to `main` only when it is a genuinely reusable current capability. Promotion should produce a small semantic interface, not preserve the study's task-specific structure.

Git history, study commits, Issues, and retained artifact manifests preserve provenance. The current runtime does not need to remain backward-compatible with every historical experiment.

## Existing architecture and change boundary

These capabilities already exist in the accepted STSRL/native pair. Propose additions only after a concrete failure of the existing control/observation path, with a minimal reuse-first alternative:

- authoritative simulator adapter;
- public state and legal actions;
- battle-state restore;
- search;
- hidden-future / belief sampling when required;
- experiment execution and durable result recording;
- training/evaluation only when justified by an active research question.

There is no task-ID-based core architecture. In particular, `native-public-projection-v3` screen coverage is **not** a research KPI or a prerequisite to ordinary full-run experiments. A new public-input guard must protect a concrete named policy and must not silently rebuild an existing simulator/run path.

## Global invariants

- The authoritative game/simulator implementation is external; do not reimplement Slay the Spire mechanics locally.
- Normal-information controllers and features must not consume hidden RNG, hidden draw order, unrevealed future information, or other simulator-private state.
- Study code is disposable by default.
- Backward compatibility requires a current named consumer; history alone is not a reason to keep compatibility in the active runtime.
- Prefer replacing or consolidating a current capability over adding another permanent wrapper, validator, schema, or version.
- Experimental evidence must survive ordinary process failure well enough to identify the last coarse execution boundary reached.
- Scientific claims require independent review, but ordinary implementation and operational repairs do not require Planner micromanagement when scientific meaning is unchanged.

## Project coordination

GitHub Issues are the primary research and planning surface. Pull requests are for changes intended to become part of the active platform.

A research study may finish with a recorded result and retained commit without merging its study implementation.

The repository intentionally avoids a parallel task archive, current-status database, or long workflow protocol in Git.
