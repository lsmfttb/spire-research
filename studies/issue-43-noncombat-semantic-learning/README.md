# Issue 43 — public choice semantics

**STUDY_ONLY.** This directory is a disposable admission study, not a core learner.
`config.json` prospectively fixes the experiment before any formal return labels.
Synthetic gradient smoke targets are explicitly unrelated to simulator outcomes.
Formal Stage 1/2 must remain closed until every Stage 0 gate passes.

## Current admission follow-up

`followup-admission.json`: **RESOURCE_BLOCKED**, readiness only. The Planner's
2026-10-11 amendment retained every original cap and authorized a fixed cost
pilot plus synthetic pipeline checks. Eight ascending admission source owners
434001..434008 supplied eight MAP and eight REWARDS roots; all 146 original
action/driver keys reached true terminal. All eight owners are shared across
families. MAP-only owners were eligible; none occurred among the selected roots.

The first pilot attempt stopped at a per-root CPU guard. Recovery preserved 98
completed keys and replayed all 48 unfinished keys under a separately debited
full-remaining preflight. Combined Stage0 upper accounting is 6267.15 CPU and
2064.48 wall seconds, within 14400/3600. Peak job RSS is 0.964 GiB.

| Cost sample | Original formula + 240 sources | Action-count-weighted + 240 sources |
|---|---:|---:|
| Original Builder, two owners |96352.44 CPU s|99453.19 CPU s|
| Independent Reviewer, same two owners |52249.89 CPU s|53950.47 CPU s|
| New all-action pilot, eight owners |106341.23 CPU s|99947.36 CPU s|

Including source regeneration, training/checkpoint/launch allowances and an
explicit 20% uncertainty/retry reserve gives 123839.06 CPU seconds centrally.
The source-owner bootstrap 90th sensitivity with full-range source-scan and
owner-regeneration allowances is 155373.66 CPU / 22124.30 wall seconds. Both
exceed the unchanged Stage1 caps 72000/18000. Small-owner sensitivity is not a
certified confidence bound or runtime guarantee.

Nineteen focused Python tests and exact-native Sapphire/indexed-gold checks
pass. Synthetic frozen scorers plus Expert complete three admission-only
controller runs on 434001. No formal labels/models, heldout results, 433xxx runs
or learning claim exist. See `READINESS.md` for tested primitives, exact
conditional plan, recovery commands, lifecycle and remaining formal launcher/
report gates. Code remains disposable STUDY_ONLY; independent review precedes
any separate Planner decision. No CPU-cap increase is implied.

## Runtime and boundaries

Build native `stsrl/main@7fef4e19c7ee13f049ee0fb45c9967e7e349fcfb` with
its pinned JSON/pybind11 dependencies, Release GCC15/CMake, Python 3.14.
Use read-only STSRL `95abc8b1afe7d84e567aa70fb7225eba09fdff25/src`.
No frozen STSRL manifest is changed: the study declares its current native
identity separately. `Battle` calls the existing unguided native v2 surface,
400 simulations, no Battle potions, both callbacks absent, and reuses the
frozen occurrence-safe root mapping/highest-mean selector.
The canonical executor and exact checkpoint adapter are reused. Its reset is
wrapped only for continuation from a restored post-candidate cursor.
Expert is the unchanged frozen driver with per-run RNG separation.

Only serialized v3 public JSON reaches `student.py`. The copied #42 adapter
retains executor-native mapping separately and freshly revalidates every
chosen ordinal. Supported variants fail closed; there is no Expert fallback
for malformed supported inputs. All native legal options are retained.
Both students strip action labels/indices; masked additionally removes current
offer semantics, boss/map/current-node/context. Both retain public resources,
deck, legal ordinal/count/kind and the same coarse public history of screen
transitions and resource changes. Prior candidate lists/labels are omitted
from both histories. Full graph edge/node tuples enter the ordinary set encoder.
The fixed signed feature hash never includes seeds, private raw state, labels,
teacher Q, or an executor action handle. Hash collisions are a limitation.

## Admission command and budgets

Run from WSL Ubuntu with paths to these exact checkouts and extension:

```bash
PYTHONPATH=/home/lsmft/.cache/issue43-native-7fef4e19:/mnt/d/DeadlycatCoding/STSRL-issue43-frozen/src \
 timeout --signal=TERM 3600s python3 \
 /mnt/d/DeadlycatCoding/spire-issue43-builder/studies/issue-43-noncombat-semantic-learning/stage0.py \
 --artifacts /home/lsmft/.cache/issue43-artifacts/stage0-attempt04
```

Attempt directories are immutable. The append-only ledger identifies source
seed, family, root, action, continuation seed, start/completion/failure boundary,
extension/config hashes, CPU/wall and RSS. Never overwrite a failed attempt.
For an interrupted attempt, add `--resume-ledger /absolute/path/stage0.jsonl`.
The runner replays the same source seeds, checks config/native/public identity,
and skips completed exact `(root, ordinal, continuation seed)` keys. Opaque
checkpoints remain process-local; failed attempts and replay cost are retained.
Formal generation needs the separately tested completed-key manifest and
fidelity-checked source regeneration before it is admitted; opaque checkpoints
are not persisted across processes. Raw/evidence retention consumer: independent Reviewer for
Issue 43; delete/review by 2026-10-18 unless another consumer is named.

Stage 0 has 3,600 wall / 14,400 aggregate CPU seconds including failed smoke
attempts. Stage 1 has 18,000 wall / 72,000 CPU; Stage 2 12,000 wall / 36,000 CPU.
Each job is limited to 8 GiB process-tree RSS with system available memory at
least 4 GiB. Native build costs must be reported separately and count in admission.
Default formal concurrency is eight; the first admission pass is one worker
to validate exact restoration before scheduling 4/8-worker measurements.
No cohort or fresh comparison is authorized by a partial canary pass.

## Focused checks

```bash
python3 -m unittest discover -s studies/issue-43-noncombat-semantic-learning -p 'test_*.py'
PYTHONPATH=studies/issue-43-noncombat-semantic-learning build/test-public-noncombat-projection
build/test-gold-reward-actions
```

The native embedded-Python regression requires its adapter import path. Without
that path its exception teardown can segfault; do not interpret a pathless
launcher crash as a completed native regression. Artifact reports must identify
which exact commands passed and which failed. No learning claim follows from
synthetic gradients or Stage 0 continuation costs.

## Original four-root admission evidence

`admission.json`: **RESOURCE_BLOCKED**. All 16 canary branches reached true
terminal with identical restore/action mappings. Thirteen focused Python tests
pass. Both current students execute nonconstant scored choices after synthetic
gradient updates, retaining ordinary public act/floor/resources/history.
Four/eight-worker repeated keys have identical checkpoints and terminal results.
Eight workers take 83.65 s versus 101.28 s for four; worker peak RSS is below
0.46 GiB and available memory exceeds 22 GiB.

Observed MAP/REWARDS branch CPU means are 32.55/15.95 s and legal option means
3.5/6. The fixed root/action/seed quotas project to 91,880.69 continuation CPU
seconds, or 96,352.44 including the minimum source collection at observed
occupancy, **before training/retries**, versus the 72,000-second Stage1 cap.
This is a four-root cost extrapolation, not a proven lower bound. The Stage2
Expert-source baseline projects to 7,154.80 CPU seconds for 384 runs; learned
trajectory costs/support cannot be certified without Stage1.

Formal labels, fitted scientific model checkpoints, heldout estimation and
fresh 128x3 runs were not opened. No inference about learning gain or harm is
made. The later Planner amendment retained the original caps and authorized the
independent-owner follow-up above; any next scientific choice remains separate.
The reports record early failures and producer/accounting limitations. Retained
raw cost ledgers are under `/home/lsmft/.cache/issue43-artifacts`; compact audit
and native evidence live on this remote study branch.
