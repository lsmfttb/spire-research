# Non-combat visible-choice policy input v1

This is disposable study code for turning the native simulator's public projection into a structured policy callback input. It implements no game mechanics, does not train or evaluate a model, and does not replace the native action executor.

The native source base is `lsmfttb/sts_lightspeed` `stsrl/main` at `1d609c7d7600b0e73a4e1cd25c12af0c24eeb26f`; the selectively adapted public MAP/REWARDS reference is `origin/archive/spire-v3-2026-10-09` at `d1dcd6534ec4a1f38ac1f7f916f01a3f931fcfd0`. The native review candidate is `codex/issue-42-public-choice-semantics` at `bbd5127d78413f522b4020afef6e7ad6d3154de7` and remains unmerged pending independent review. The study branch base is `lsmfttb/spire-research` `main` at `9525d416b9488bb2455435699893b03fe19fe30a`. Handoff commits and their hashes are recorded in the handoff issue and PRs.

## Coverage

| Native screen | Adapter status | Source-backed input | New-model action allowed |
| --- | --- | --- | --- |
| `MAP_SCREEN` | Supported only when map graph, current node, visible Act boss, and native route actions validate | Public map room symbols/edges, current node, visible current-Act boss, ordered legal route destinations | Yes |
| `REWARDS` | Supported only when every legal action has a unique public candidate and a source-backed offer | Card identity/upgrade, indexed gold amount, relic, potion, key/Sapphire removal, skip, and held-potion slot actions | Yes |
| `EVENT_SCREEN` | Unsupported | No audited event option renderer in this capability | No; reject before callback/execution |
| `REST_ROOM` | Unsupported | No exact rest choice renderer in this capability | No; reject before callback/execution |
| `TREASURE_ROOM` | Unsupported | No exact chest/leave choice renderer in this capability | No; reject before callback/execution |
| `BOSS_RELIC_REWARDS`, `CARD_SELECT`, `SHOP_ROOM`, `INVALID` | Unsupported | Not part of this study input | No; reject before callback/execution |
| `NEOW` | Outside this native contract | The active `ScreenState` enum has no Neow screen identity | No; it cannot be represented as a supported adapter state |
| `BATTLE` | Existing native battle public API remains separate | Existing T096 public-information projection | Not through this adapter |

If any candidate index is out of range, missing, duplicated, stale, or not uniquely associated with its public option, the adapter raises `ChoiceSurfaceError`. Partial MAP coverage and unsupported REWARDS subcases raise `UnsupportedChoiceSurface`; the executor must not fall back to hidden action bits or a legacy vector.

## Boundary and mapping

`prepare_policy_input(native_projection, native_actions)` validates the native v3 projection against the current ordered `legal_actions()` list. It returns a `PreparedPolicyInput` containing callback JSON and an executor-only tuple of native action objects. `call_policy(prepared, callback)` passes only the JSON string to the policy. A policy returns an integer `candidate_index`. Immediately before stepping, `resolve_native_action(...)` requires a fresh projection and fresh action list, revalidates their order and public identity, and returns the fresh native action object. Action bits remain inside the trusted executor path and do not appear in serialized callback input.

The policy JSON has four top-level fields: `availability`, `public_state`, `legal_options`, and `schema_id`. Each legal option keeps its native candidate ordinal and separates public action identity from visible screen semantics. MAP destination and REWARDS contents are association-checked against the same ordinal.

## Focused verification

Run the study-only adapter contract tests with the Python standard library:

```powershell
python -m unittest -v studies/noncombat_visible_choice_input_v1/test_adapter.py
```

The native worktree adds a focused `test-public-noncombat-projection` target. On a clean WSL clone/worktree with its submodules initialized, run:

```bash
cmake -S . -B build-issue42 -DCMAKE_BUILD_TYPE=Release
cmake --build build-issue42 --target test-public-noncombat-projection -j4
./build-issue42/test-public-noncombat-projection
```

Set `PYTHONPATH` to the study directory before running the native target so its embedded Python test imports this adapter. The native test follows seed 49 at A20 through actual `StepSimulator` REWARDS and MAP screens, passes each projection through `prepare_policy_input` and a callback that receives only serialized JSON, resolves the selected ordinal against a fresh ordered action list, then checks that the selected card is awarded and selected route is entered. A controlled same-old-observation pair checks different visible gold offers produce different adapter policy inputs and execute the matching gold amounts. It also checks two distinct gold slots, hidden-future invariance, private-field exclusion, and unsupported screens. It makes no win-rate or training claim. Its single JSON output reports tested native source commit `bbd5127d78413f522b4020afef6e7ad6d3154de7`, public native projection examples, and the corresponding study policy inputs with private native bits omitted.

Coverage is intentionally limited to MAP and REWARDS. REST and TREASURE remain explicitly unsupported; this handoff does not claim all four historically learned screen families are ready. NEOW is absent from the active native `ScreenState` API and has no supported adapter representation.

The sanitized policy inputs from the fixed seed 49, A20 native run are checked in as [REWARDS JSON](examples/a20-seed49-rewards-policy-input.json) and [MAP JSON](examples/a20-seed49-map-policy-input.json). Both contain only the adapter's serialized callback schema, without native action bits or trusted executor handles.
