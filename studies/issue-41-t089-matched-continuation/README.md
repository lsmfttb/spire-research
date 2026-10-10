# Issue 41: T089 matched continuation Stage 1

Bounded `STUDY_ONLY` follow-up from Issue 40's exact recovered native stack. The experiment compares the original T089 Expert-continuation targets with matched roots whose later Non-Combat decisions use the frozen T089 learned policy. It does not train, promote a controller, perform new Battle search, or change the simulator.

The runner must pass the retained Issue 40 verification, exact 14 source/target role hashes and both retention manifests, source-owner mapping, exact frozen native and extension identity, exact 64-state cohort and 221 historical target rows, original T089 gate root choices, full public/legal replay parity, and exact reuse of all 44 Issue 40 continuation rows. It stops fail-closed on any identity, target, public-input, resource, or terminal mismatch.

The Stage 1 cap is 12,000 seconds wall and aggregate process CPU, concurrency one, RSS at most 8192 MiB, available memory at least 4096 MiB, 500 steps per continuation, and 512 total continuation attempts including the 45 prior Issue 40 attempts. The external supervisor enforces caps across preflight and execution invocations. Progress is append-only and completed keys are resumed without recomputation.

The preflight supervisor first regenerates the deterministic artifact-only plan, then checks all 64 source replays and exact root decisions. It does not execute a paired continuation. Its complete process-tree resource use is recorded in `stage1-budget.json`; it then re-estimates the total budget using the observed 64-root preflight cost and writes `stage1-resource-review.json`. Only after that gate passes should the reviewed plan be committed and execution started.

Commands (inside the exact Issue 41 worktree, via WSL):

```sh
cd /mnt/d/DeadlycatCoding/spire-research-issue41
/home/lsmft/stsrl-spikes/py313-torch/bin/python studies/issue-41-t089-matched-continuation/stage1_supervisor.py preflight
```

Review and commit `stage1-plan.json` and `stage1-preflight.json` before execution. The supervisor preserves the resource history across both commands and terminates the full child process group at any cap:

```sh
/home/lsmft/stsrl-spikes/py313-torch/bin/python studies/issue-41-t089-matched-continuation/stage1_supervisor.py execute
/home/lsmft/stsrl-spikes/py313-torch/bin/python studies/issue-41-t089-matched-continuation/verify_issue41.py
```

The primary bootstrap is 10,000 family-stratified paired resamples over the 64 source-state units using NumPy `default_rng(391089)` and linear 2.5/97.5 percentiles. It preserves paired seed outcomes and applies only the predeclared Issue 39 terminal rules after all valid keys complete. The 43-source-run dependence is reported as a limitation; the primary CI is not replaced.

`stage1-progress.jsonl` is append-only and fsynced before and after each continuation. `stage1-state.json` records the durable boundary. `stage1-result.json` reports one of the three preregistered hypotheses only when all 380 physical keys are complete; otherwise it reports a fail-closed terminal and no hypothesis result. `verify_issue41.py` checks the original 44 reused rows, target arithmetic, full key coverage, bootstrap, resource ledger, and exact terminal rule, then writes `verification.json` and a hash manifest. The manifest covers the compact study artifacts and source code; the large frozen state and target tables remain external and are represented by their pinned hashes.
