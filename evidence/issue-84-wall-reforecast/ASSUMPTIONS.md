# Issue 84 source-locked pilot wall reforecast

This is an evidence-only follow-up to Planner decision `#6545529633`. It keeps all formal seeds on hold. It does not launch games, training, validation, test, or predictor fitting. The original pilot report, progress, v1 forecast, active clock, and all pilot checkpoints stay untouched.

## Reproduce

From the repository root in PowerShell:

```powershell
python evidence/issue-84-wall-reforecast/reforecast.py --self-test
python evidence/issue-84-wall-reforecast/reforecast.py --repo-root . --artifacts-root 'D:\DeadlycatCoding\stsrl-data\issue84\artifacts'
```

The second command is a read-only dry run. After this producer is committed, `--write` creates two **new** files with exclusive create semantics:

- `pilot/23b21a1205426e2a26ab65bc8535bcf48b89dcf0/task-wall-forecast-correction-v2.json`
- `pilot/23b21a1205426e2a26ab65bc8535bcf48b89dcf0/pilot-weight-retention-v1.json`

It refuses to replace existing different bytes. It verifies the locked pilot/report/forecast hashes and source lock before it calculates anything.

## How the reforecast separates cost

The pilot has 192 training and 56 evaluation games. For each of the six training arm invocations and seven evaluation group invocations, the producer reads only start/finish timestamps and status, then computes the first-to-last complete-game interval envelope. The run's recorded `elapsed_seconds` minus that envelope is the observed launch/setup/scheduling time. Concurrent per-game intervals are unioned when reconciling actual pilot wall; their worker durations are never summed as physical wall.

The registered formal execution is estimated as sequential training then evaluation phases. Training has 12,288 games in 96 arm blocks of 128; validation has 19 invocations and test has seven, for 26 evaluation invocations and 3,456 games. The game envelope rates are scaled by complete-game count; setup is scaled by invocation/block count. The 96 actor blocks scale the six observed pilot actor updates by 16.

Predictor cost is split into model-fit and feature-cache build time. It scales with the registered 15,360 cumulative prior-owner presentations per model over all lagged blocks, at frozen `T_REF=36.6474609375`: four fits (D-V, E-V, E-Q, F-V) and three shared actor feature caches (D, E, F). This is an explicit linear-row assumption, not a new fit.

The actual pilot cumulative wall clock is carried forward once. The v1 `legacy_unallocated_residual_seconds` is preserved but not scaled by game count: its old calculation subtracts summed invocation timers despite the pilot's concurrent training/evaluation intervals. Known deterministic C failures and PARENT pre-start failures are recorded as pilot-only recovery; C attempt-19 remains `UNKNOWN`. The final pilot closeout increment is used once as a future task-level proxy.

## Scenarios and limits

`low`, `base`, and `high` are sensitivity cases, not probabilities or confidence bounds. They use respectively 0%, 25%, and 50% judgmental operational margin on measured future work. They repeat the legacy task-level residual by 0%, 25%, and 100% once for the whole formal task; no case multiplies it by games. Known historical deterministic failure classes are not projected per game. No source-locked transient retry rate is estimable; the judgmental margins cover that uncertainty, aggregation, verification, reporting, and closeout.

The producer reports candidate 150%, 175%, and 200% whole-task allowances but never installs one. The active allowance remains unset pending independent Reviewer verification and Planner feasibility decision. The model/checkpoint inventory verifies 12 actor/optimizer files and three predictor files by path and SHA, marks every one ineligible for formal initialization, and records that this task copied or deleted none.
