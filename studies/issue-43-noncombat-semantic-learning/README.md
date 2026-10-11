# Issue 43 — public choice semantics

**STUDY_ONLY.** This directory is a disposable admission study, not a core learner.
`config.json` prospectively fixes the experiment before any formal return labels.
Synthetic gradient smoke targets are explicitly unrelated to simulator outcomes.
Formal Stage 1/2 must remain closed until every Stage 0 gate passes.

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
Current admission runner cannot resume opaque native checkpoints across process
termination: a new attempt must replay the same two source seeds and account
all prior work. Formal generation needs a separate completed-key manifest and
fidelity-checked checkpoint persistence before it is admitted; no formal runner
is claimed here. Raw/evidence retention consumer: independent Reviewer for
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
