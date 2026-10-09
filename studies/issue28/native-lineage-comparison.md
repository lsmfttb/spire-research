# Native lineage comparison — Issue #28

**Audit date:** 2026-10-09
**Disposition:** study evidence only; no native source, manifest, active ref, or PR was changed.
**Recommendation:** **A — one shared `stsrl/main` native line**, with narrowly selected public-state/API additions and ordinary bug fixes proposed through the STSRL review process. This is a recommendation for independent Reviewer/Planner review, not merge authorization.

## Pins and method

All refs were fetched/pruned before inspection. The native source refs were:

| Line | Exact commit | Projection / status |
|---|---|---|
| STSRL accepted line | `stsrl/main` `d61c7c2a120aa90730fd8530718a7cd7cd856b46` | projection v1; consumed by STSRL's `docs/sts_lightspeed_source_manifest.json` |
| spire before v3 | `spire/main` `ab2b11bc3b5b6c6b68d9d855bc9545e9aca62a28` | projection v2 |
| reviewed spire v3 | `d1dcd6534ec4a1f38ac1f7f916f01a3f931fcfd0`, preserved by `archive/spire-v3-2026-10-09` | projection v3; includes noncombat projection work |
| PR #33 current head | `f69c7c866f8341be9c7cc177dd356f439af0bd56` | post-v3 rollback; projection v2; PR remains separate and untouched |

GitHub's compare API reports common ancestor `7476a81954020087da31d41d16fddf475746ec2d`, with 63 commits on the STSRL side and 15 on the reviewed v3 side relative to that base. This is commit-history distance, not a complexity or utility score. The native clone used for local work is shallow, so its local `rev-list` counts were not used. The `json` and `pybind11` submodule pins are identical at all four commits (`0b345b20c888f7dc8888485768e4bf9a6be29de0` and `d03662f0984f652b60e7ddce53d3868002275197`).

The clean builds used Ubuntu/WSL, CMake 4.2.3, GCC 15.2, Python 3.14.4, Release, and `-DCMAKE_POLICY_VERSION_MINIMUM=3.5`. The old JSON CMake minimum emitted a deprecation warning. `stsrl/main`'s native module clean build took 65.57 s after 4.70 s configure; reviewed spire v3's module-only clean build took 52.16 s after 5.25 s configure on the same host. Thus the measured module-build difference was 13.41 s in this run, not evidence of a decisive operational burden by itself. Building v3's module plus its two focused native targets took 157.28 s; each test target recompiles the native source list separately. No human-hours estimate is asserted.

## What a new noSL consumer actually needs

The reusable runtime subset is reset/snapshot, legal-action enumeration, step, checkpoint/restore where the run boundary needs it, public observations/projection, and stable public action identity. Native search, particle sampling, telemetry, and audit methods are not prerequisites for a deployed noSL policy. STSRL's `LightSpeedAdapter` and `execute_controlled_run` already separate the Python run loop from simulator mechanics; their current STSRL tests and consumers are concrete, not hypothetical. STSRL also has actual study consumers for T096 sampling and T114 shared-belief search, but those are teacher/search tools and should not be mistaken for deployed policy dependencies.

The study-only `adapter_transition_probe.py` exercised `reset → legal_actions → step` through STSRL's existing adapter on all three native pins. A one-step transition succeeded on STSRL, spire v2, and spire v3. STSRL's parser accepted v1 and rejected spire v2/v3 with `unsupported native public projection schema`; this is a strict projection-contract gap, not absent simulator mechanics and not a need to write a second executor. The v2 and v3 payloads also differ in fields/coverage, so compatibility needs an explicit, tested translation/version path rather than merely accepting a new schema string.

## File and function comparison

The full pinned-tree comparison is `git diff --ignore-submodules=all d61c7c2..d1dcd65`; the following groups account for its 39 paths and describe the functional differences relevant to a new research consumer.

| Files / functions | Difference and consequence |
|---|---|
| `bindings/slaythespire.cpp`; added `bindings/public_battle_state.h` | The binding file is 7,906 lines at d61, 2,414 at ab2, 2,890 at d1, and 2,414 at f69. These counts are a surface measure only. STSRL binds its mature search, sampler, diagnostics, and study APIs. Lean spire removes most of that native research surface and factors public combat-state construction into a 453-line header; d1 adds a screen-specific public projection path. The common mechanics still build into the Python module. The smaller binding is a real audit/extension-surface reduction, but the retained API is not automatically simpler if both trees must evolve. |
| `include/combat/BattleContext.h`, `src/combat/BattleContext.cpp`, `include/combat/CardManager.h`, `src/combat/CardManager.cpp` | STSRL stores `knownGeneratedCardPublicIdentity` on `BattleContext`. Spire moves this runtime-only map into `CardManager`, adds `persistentDeckSize`, and records generated identity at hand/discard/exhaust additions and known draw operations (`noteKnownDrawTop/Bottom`, top/index consumption, shuffle insertion, Dual Wield). `public_battle_state.h` uses this map while omitting unique IDs from public output. STSRL T115 and spire's public-battle test cover meaningful secrecy/visibility cases. There is **no paired cross-line checkpoint/restore and generated-card transition equivalence test** covering all requested insertion, reshuffle, and hidden-anchor cases; equivalence is not established here. |
| `src/sim/search/GameAction.cpp`, `getAllRewardActions()` / reward execution | Spire changes one line: it emits `GOLD, i` for each gold reward. STSRL emits repeated unindexed `GOLD` actions, while `isValidAction` and execution use `idx1` to read/remove the reward. A two-reward fixture `[17, 41]` against d61 produced two legal candidates with `idx1=0`; both executed 17, so candidate 1 did not select 41. This state is reachable: `GameContext::afterBattle()` creates ordinary combat gold and adds `info.stolenGold` for stolen-gold encounters. The d1 noncombat regression reports both indexed candidates execute their own visible amount. This is an ordinary behavior bug, not an architectural reason to fork. No native source was silently patched. |
| `include/sim/search/BattleScumSearcher2.h`, `src/sim/search/BattleScumSearcher2.cpp` | 1,086 lines at d61 versus 540 at all spire pins. Spire removes policy-prior/learned-leaf callbacks, progressive-bias child heuristic/audit state, tree-geometry rows, state-utilization digests, and related counters/recorders; it retains the core search/action selection. These are genuine optional study capabilities, not proven requirements for a noSL deployed policy. STSRL's source manifest and T096/T114/STSR* tests demonstrate current STSRL consumers, so pruning them from STSRL would break research/evidence consumers unless separately migrated. The large reduction supports spire's lean-maintenance argument, but the common run loop is not entangled with these APIs. |
| `src/combat/Actions.cpp` | No difference between d61 and d1. The audit found no reason to port or maintain a duplicate action-mechanics implementation from this file. |
| `CMakeLists.txt`, `apps/*`, `scripts/*` | STSRL has `test-progressive-bias`, native API smoke, T096/T114/T115 and Search-v2 diagnostic scripts. d1 removes those STSRL-specific fixtures and adds `test_public_battle_state_semantics.cpp` (1,111 lines) and `test_public_noncombat_projection.cpp` (670 lines), both opt-in CMake targets; `scripts/test_native_capability_smoke.py` is the lean-line smoke. d1's focused C++ tests check hidden-future invariance, public identities/action binding, and noncombat coverage. This exchanges legacy study coverage for research-focused public-visibility regressions; it is not a simple test-count win. |
| `docs/*`, `.github/*`, `.gitignore` | d1 removes the STSRL-specific change/PR templates, STSRL maintenance/API docs, and T004–T115 study verification docs. `.gitignore` also drops `build-py` and Python cache ignores. These are governance/artifact-history cleanup, not simulator mechanics; deleting them reduces local surface but loses in-tree STSRL workflow and reproducibility material. |
| d1-only study evidence | `studies/public-generated-card-membership-coverage-reentry.md` and `studies/public-status-timing-coverage-reentry.md` document bounded coverage re-entry. These are evidence, not permanent API. |

The v2 → v3 change is confined to `CMakeLists.txt`, `apps/test_public_noncombat_projection.cpp`, `bindings/slaythespire.cpp`, and the indexed-gold line in `GameAction.cpp`. V3 adds supported public map graph/routes, current node, visible Act boss identity, Neow choices, event phase, and reward choice payloads, with fail-closed cases. Its native regression uses seeds 49/50 and marks five other screens unsupported. PR #33's exact f69 head rolls back the v3 screen payload and test, restores schema v2, and adds a workflow guard against accidental v3 reintroduction. The gold index fix, public battle state, and `CardManager` identity changes survive that rollback. V3 findings must not be described as capabilities of the current PR head.

For exhaustive path coverage, the d61→d1 diff also deletes `.github/ISSUE_TEMPLATE/stsrl-native-change.md`, `.github/PULL_REQUEST_TEMPLATE/stsrl-native-change.md`, `apps/test-progressive-bias.cpp`, `docs/stsrl-004-tree-geometry-verification.md`, `docs/stsrl-005-visibility-verification.md`, `docs/stsrl-006-native-particle-search-bridge.md`, `docs/stsrl-007-particle-search-stage-observability.md`, `docs/stsrl-008-root-occurrence-mapping-observability.md`, `docs/stsrl-009-configuration-aware-root-mapping.md`, `docs/stsrl-maintenance.md`, `docs/stsrl-native-api-smoke.md`, `scripts/stsrl_api_smoke.py`, `scripts/test_battle_search_v2_state_utilization.py`, `scripts/test_battle_search_v2_tree_geometry.py`, `scripts/test_step_simulator_rebuild_terminal.py`, `scripts/test_stsr007_particle_search_stage_observability.py`, `scripts/test_stsr008_root_occurrence_mapping.py`, `scripts/test_stsr009_configuration_aware_root_mapping.py`, `scripts/test_t096_particle_search_bridge.py`, `scripts/test_t096_public_information_sampler.py`, `scripts/test_t096_visibility_transitions.py`, `scripts/test_t114_shared_public_belief_search.py`, and `scripts/test_t115_public_draw_constraints.py`. It adds `scripts/test_native_capability_smoke.py`, the two public-state apps/header already listed above, and the two re-entry studies. The remaining changed paths are `.gitignore`, `CMakeLists.txt`, the binding/header/context/card-manager/searcher/GameAction files already listed above. `src/combat/Actions.cpp` has no diff.

## Evidence and scope limits

| Check | Result |
|---|---|
| Clean STSRL d61 native module build | Pass; 65.57 s compile after 4.70 s configure. |
| Clean spire v2 ab2 native module build | Pass; focused test target from its older CMake line was not run. |
| Clean spire v3 d1 module + `test-public-battle-state-semantics` + `test-public-noncombat-projection` build | Pass; 157.28 s total. Module-only clean build: 52.16 s. |
| STSRL `scripts/stsrl_api_smoke.py` | Pass; checked reset/snapshot/observation/legal actions/step/checkpoint/restore/public projection and native search/sampler API surfaces. |
| STSRL T096 sampler smoke, `--steps 200 --particles 4` | Pass; `T096_NATIVE_SMOKE_PASS step=4 particles=4 distinct_hidden=4`. |
| STSRL T115 draw constraints | Pass; all 17 reported public identity, repeated insertion, known top/bottom, and hidden-realization constraints true. |
| STSRL Python adapter/projection/manifest/T096/T115 focused group | 104 passed in 1.19 s. |
| STSRL controller/noncombat/controlled-run group | 149 passed, 1 deselected in 52.09 s. The deselected case is `test_regeneration_command_is_full_pinned_wsl_command`; its assertion hard-codes another developer's `/mnt/d/DeadlycatCoding/STSRL/.claude/worktrees/` path and fails in this checkout. The first unfiltered run was 149 pass / 1 environment-path failure. |
| Spire d1 public battle-state semantic test | Pass: `PUBLIC_BATTLE_STATE_SEMANTICS_PASS`. |
| Spire d1 noncombat projection test | Pass: `PUBLIC_NONCOMBAT_PROJECTION_PASS`; included map/reward/event/boss visibility, hidden-future equality, action mapping, and `[17,41]` indexed gold regression. Cursed Tome phase and Match-and-Keep are explicitly unsupported; five additional screens report unsupported. |
| Existing STSRL adapter transition probe | Reset/legal-actions/step succeeded on d61, ab2, and d1; projection parser accepted d61 v1 and rejected v2/v3. This is a one-step smoke, not full-run trajectory parity. |

The gold fixture is reproducible from `gold_reward_pair_audit.cpp`. It was compiled against the exact d61 source using this temporary CMake target, then the native CMake edit was restored:

```cmake
add_executable(issue28-gold-pair-audit
        apps/issue28_gold_reward_audit.cpp ${sts_lightspeed_SOURCES})
target_include_directories(issue28-gold-pair-audit PRIVATE include json/include)
```

Copy the study fixture to `apps/issue28_gold_reward_audit.cpp`, configure using the Release command above, build target `issue28-gold-pair-audit`, and run `build-issue28/issue28-gold-pair-audit`. The checked-in fixture and adapter probe are study-only and add no project/native dependency. The current report does not claim exhaustive semantics for every dynamic card, Headbutt/Frozen Eye/Runic Dome, checkpoint restore, hidden anchor, or noncombat screen across both lineages. Those are explicit follow-up evidence gaps before porting the identity-layout change or making a scientific compatibility claim.

## A/B/C decision

| Option | Evidence-based assessment |
|---|---|
| **A. One shared `stsrl/main` native line — recommended** | STSRL already owns a single accepted source line and real adapter, controlled-run, manifest, sampler/search and public-information consumers. Its additional search/study APIs are not needed by a deployed noSL policy, but can remain isolated from that consumer; the one-step test found a narrow versioned projection contract gap, not missing mechanics. Sharing mechanics avoids duplicate legality/action bugs such as indexed gold and reduces long-lived trajectory drift. The measured module build was only 13.41 s slower in this run; the much larger source/API delta is a meaningful audit-surface benefit for spire, but this audit did not show it makes the ordinary step path materially harder to operate. Selectively port the lean public contract rather than wholesale-importing every STSRL study feature. |
| **B. One lean `spire/main` line for new research; STSRL historical pin** | Viable if Planner wants noSL-specific public APIs and test scope to stay compact. Evidence: binding surface 7,906 → 2,414 lines from d61 to v2; Search-v2 implementation 1,086 → 540; generic step execution already reuses STSRL's Python loop. Costs: v2/v3 projection parsing is currently incompatible, public generated-identity lifecycle has not been proven equivalent, and an ongoing STSRL accepted line plus spire would still mean two mechanics lineages. Treat STSRL as historical only if its maintainers explicitly accept that operating model; the STSRL source policy alone does not decide spire architecture. Revisit B if future noSL work demonstrates repeated review/test friction from the retained native surface, not from counts alone. |
| **C. Two long-lived native lines** | Highest mechanics/test drift and duplicated bug-fix burden. Current evidence does not show a scientific semantic split that requires both lines to evolve independently. Avoid absent such a demonstrated requirement. |

### Smallest sequence if A is accepted

1. Reviewer verifies the source/test comparison and Planner confirms the shared-line disposition.
2. In a separately scoped STSRL task/PR, add a regression for indexed multi-gold candidates and fix `getAllRewardActions()` to preserve each reward index; update the source manifest only through the STSRL acceptance gates.
3. Port only the public fields/actions needed by the next noSL run contract, with an explicit versioned projection test. Keep search/particle APIs optional and out of deployed policy calls.
4. Before porting generated-card identity layout, add paired tests for known generated cards through hand/discard/exhaust, known-top/bottom and random insertions, reshuffle, checkpoint/restore, and hidden-anchor variation. Retain fail-closed behavior where identity is unknown.

If B is chosen instead, first specify the exact v2 projection contract for the next research consumer and test that small adapter translation against d1 and the current rolled-back f69 schema. Do not fork an executor or carry v3 noncombat semantics into PR #33 without a new Planner disposition. No implementation sequence authorizes modifying either native branch in this audit.
