# Issue #29: native-line cutover readiness

**Readiness: PASS for independent review. Proposed code disposition: `STUDY_ONLY`.** The evidence supports retiring `spire/main` as an active development line. It does not authorize this Builder handoff to delete that ref; the Reviewer should validate the gates, then Planner/Maintainer can execute and verify the separately scoped retirement transaction.

## Verified source and repository state

| Ref | Exact SHA | Finding |
|---|---|---|
| `spire-research/main` | `90ca579da09fdc0d6d72fa49f1e903e751e60472` | Contains only `AGENTS.md` and `README.md`; the governance pointer already names accepted `stsrl/main`. No runtime import or native call exists on main. |
| `sts_lightspeed/stsrl/main` | `d61c7c2a120aa90730fd8530718a7cd7cd856b46` | Exact integration source pinned by STSRL’s `docs/sts_lightspeed_source_manifest.json` schema v1. |
| STSRL `main` | `3037b75eca4bd73fa70d018ffd4442a1f2d65628` | Current source repository revision used for the adapter smoke and focused unit tests; manifest still pins native `stsrl/main@d61c7c2a…`. |
| `sts_lightspeed/spire/main` | `c34d3ce6253084169447b474372416832b0aa53c` | Post-rollback v2 source still exists. No open native PR targets `spire/main`. |
| `archive/spire-v2-postrollback-2026-10-09` | `c34d3ce6253084169447b474372416832b0aa53c` | Exact post-rollback source snapshot verified. |
| `archive/spire-v3-2026-10-09` | `d1dcd6534ec4a1f38ac1f7f916f01a3f931fcfd0` | Exact historical v3 snapshot verified. |

`spire-research` has no open PRs. Its only extant study branch is the closed Issue #28 audit branch `codex/issue-28-lineage-audit@d849c2e2d8d0d612458eb74a1f293849372757e5`; it contains historical probes/report, not a deployed consumer. The merged pointer-change branch is already an ancestor of current main. Open issues are #1 (research direction) and #29. STSRL T116 PR #137 remains open against STSRL `main` as a separate, bounded multi-GOLD repair; no implementation or manifest change was duplicated here.

## Consumer and capability inventory

| Candidate consumer | Path and exact native call | Disposition |
|---|---|---|
| Current `spire-research` runtime | `main`: no Python/C++ runtime or native call sites; no study runtime is merged. | No migration required. |
| Issue #28 audit probe | `studies/issue28/adapter_transition_probe.py` on the closed audit branch calls `LightSpeedAdapter.reset`, `legal_actions`, `step`, and `public_projection`; it is a one-transition study probe. `gold_reward_pair_audit.cpp` is a source-pair audit. | Preserve branch/commit as historical evidence; neither is a live integration consumer. |
| Spire generic public battle API | On `spire/main`, `bindings/slaythespire.cpp` binds `StepSimulator.public_battle_state()` and `step_public_action()`; `scripts/test_native_capability_smoke.py` exercises both. | Native-line self-test only. No downstream `spire-research` consumer or immediately needed normal-public run depends on them. Do not port solely to preserve the old line. |
| Hidden-future sampler | `spire/main@c34…` has no `sample_hidden_future_particles` API. Accepted STSRL `stsrl/main@d61…` does; STSRL `src/sts_combat_rl/sim/t096_public_information_sampler.py` calls `LightSpeedAdapter.sample_hidden_future_particles()` for T096 research. | The named sampler consumer remains on the accepted STSRL line; it is not a missing spire-line dependency or blocker to retiring `spire/main`. It remains a research/search API, not a normal-public policy input. |

## Accepted-source noSL control smoke

The reproducible witness is [`public_control_smoke.py`](public_control_smoke.py). It built the native `slaythespire` target from exact `stsrl/main@d61c7c2…` in WSL (GCC 15.2, Python 3.14.4), then used STSRL `LightSpeedAdapter` and the existing `execute_controlled_run` with a non-search first-eligible-action policy at A20, seed `20261009`, no potions, and no checkpoint restore. The continuous reset-to-terminal run finished naturally as `PLAYER_LOSS` after 9 policy decisions (7 battle, 1 event, 1 map); it retained no step records and reported no run problems.

`PolicyController.select_action` discards its raw adapter, snapshot, action-list, and step-index arguments and calls the policy with only `DecisionContext`. The witness recursively audits that context with the existing forbidden-public-context checker before each action. Provenance reported `normal_public_policy`; the native public projection schema was `native-public-projection-v1`.

This establishes the existing full-run/control path and public-input boundary for one seeded witness. It is not an efficacy result, a trajectory/parity comparison, a broad seed study, or evidence that the first-eligible policy is useful.

## Focused verification

- WSL CMake: `cmake -S . -B build-issue29 -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5`; `cmake --build build-issue29 --target slaythespire -j2` — passed against native source `d61c7c2…`.
- STSRL unit tests under Windows Python 3.12: `pytest -q tests/test_public_run_context.py tests/test_lightspeed_adapter.py tests/test_online_controller.py tests/test_battle_agent.py` — **123 passed**.
- Natural controlled-run smoke described above — **terminal, `PLAYER_LOSS`, 9 policy calls, zero problems**.
- GitHub query for open native PRs with base `spire/main` — **none**.
- Remote ref reads confirmed both archives and exact `stsrl/main` / `spire/main` SHAs above.

## Cutover gates and disposition

- **PASS:** accepted native source and STSRL manifest pin agree; target source builds and runs through the existing executor.
- **PASS:** no merged `spire-research` runtime consumer or open project PR remains; the only study branch is historical Issue #28 evidence.
- **PASS:** post-rollback v2 and historical v3 commits have exact immutable archive refs; no open native PR targets `spire/main`.
- **PASS, pending Reviewer confirmation:** the one-line AGENTS pointer change was separately user-authorized and merged by PR #30 at `90ca579…`; this handoff makes no AGENTS or native-source edit.
- **NEXT, Planner/Maintainer-owned:** after independent review, retire only `refs/heads/spire/main`, verify its remote absence and both archive refs’ continued presence, and update the standing Issue #1 current state. Keep T116 on its separate accepted-STSRL review path.

No current consumer or missing capability justifies continued feature development on `spire/main`. Recommended disposition is therefore **STUDY_ONLY / readiness PASS**, with actual branch deletion reserved for the post-review retirement transaction.
