# Issue 56: two-encounter potion allocation

This is a disposable, study-only runner for the fixed 64-seed A20 Ironclad
consume-now versus reserve-through-B1 intervention in GitHub Issue #56. It
reuses the pinned STSRL executor and native Search-v2 controller; it does not
change simulator mechanics, train a policy, or produce a deployable controller.

## Frozen runtime

Run from the pushed study checkout in WSL. The native extension, simulator
source, and STSRL source must match the exact identities pinned in
`study.py`. Set `SPIRE_WORKTREE_COMMIT` to the exact pushed producer commit;
the runner fails closed if it is absent or if any source or binary hash differs.

```bash
cd /mnt/d/DeadlycatCoding/spire-research-issue-56
export STSRL_ROOT=/mnt/d/DeadlycatCoding/issue-56-STSRL
export NATIVE_MODULE_DIR=/tmp/issue56-native-build
export SPIRE_WORKTREE_COMMIT=<exact-pushed-commit-sha>
```

## Run and artifacts

The source cursor follows each fixed seed through its natural run until the
first selected legal non-Smoke potion use, an authentic game terminal, or the
20,000-step cursor limit. The battle containing that first qualifying use is
the study's B1; it may occur after the game's first battle. Earlier battles
remain part of source collection only and are recorded as source battle exits.
The consume/reserve branches both resume at the selected root, so their B1 is
the battle containing that use and B2 is the next battle after it.

First run the fixed-order preflight. It stops only after two distinct eligible
owners each pass the root/action/mask checks, have at least two reserve-mask
decisions, show no post-root raw-state fingerprint collision across the two
arms during B1, retain an actual interbattle Non-Combat decision in both arms,
and resolve B2 in both arms. It preserves every earlier source owner in the
preflight ledger.

```bash
python3 studies/issue56/study.py --preflight --output studies/issue56/preflight
```

The formal command verifies the preflight summary, producer and source hashes,
the two-owner paired-B2 gate, and the preflight owner/source-ledger hashes. It
then copies all preflight owners into the formal denominator and runs the
remaining fixed seeds once, without replacement, extending or result-driven
selection.

```bash
python3 studies/issue56/study.py --output studies/issue56/results
```

Set `ISSUE56_PREFLIGHT_PATH` if the preflight directory differs from the
default `studies/issue56/preflight`. Both stages append measured process CPU to
`studies/issue56/budget_events.jsonl` and stop at the runner's 5,900 CPU-second
ceiling, leaving the issue's setup allowance and 1,500-second reviewer reserve.
The checkpoint handle and expert-driver RNG state remain in memory and are
never written to artifacts. `progress.json` and the append-only event log record
durable coarse progress; manifests, owner ledgers, summaries, transition
ledgers, and their hashes identify the evidence.

The earlier first-battle-only outputs are retained as
`preflight-first-battle-only-superseded/` and
`results-first-battle-only-superseded/`; they are not part of the corrected
formal gate or denominator. The regression test requires a real fixed-cohort
owner whose first qualifying use occurs after at least one earlier battle
exit without a qualifying use.

The artifacts remain study-only. Paired differences are local to this frozen
privileged teacher-assisted intervention and do not establish normal-public
policy value, full-run Heart performance, or a promotion decision.
