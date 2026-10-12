# Issue44 fixed-data model-seed replication

## Operational resume repair

Original producer557ab023 sealed all12 authentic models and the1536-key manifest,
then received SIGXCPU before creating any fresh request. Its list comprehension
hashed the same1.4MB frozen manifest1536 times without renewing the10CPU-second
parent lease. The repaired orchestration hashes it once and ticks the unchanged
budget guard during request preparation and completed-result collection.

Resume needs independent PASS of the replacement exact head and a subsequent
explicit replacement Planner grant. In addition to the normal exact-head grant
fields below, require standalone
`RESUME_LINEAGE_PRODUCER=557ab0232b3d42ab15e09913ab4850802f308062` and add
`--resume-original-producer 557ab0232b3d42ab15e09913ab4850802f308062` to the
formal invocation. Use the same `/home/lsmft/.cache/issue44-formal-lineage`.
Original identity, authorization, six train completions,12 checkpoints, all
optimization curves, full manifest and budget journal remain unchanged.
Current code/grant bindings are append-only runtime-authorizations and request/
completion provenance. Old completed keys are verified and reused before any
request creation; no authentic model is retrained. Scientific replication.py,
contract.json and all #43 dependencies must have identical original Git blobs;
only launcher/worker admission/fixtures may differ. No second formal budget,
sample/seed change or reset is permitted. An explicit maintenance close through
the original audited guard charged the crash parent lease and clock exposure;
future attempts continue that same now-closed journal.

**STUDY_ONLY. Readiness only; formal training and fresh435xxx execution remain
closed until independent exact-head review and a subsequent Planner grant.**

[Issue44](https://github.com/lsmfttb/spire-research/issues/44) is the scientific
contract. Base: `origin/main@9525d416b9488bb2455435699893b03fe19fe30a`.
The code reuses #43 executable producer
`9d5deb3bbd87b98a2531300d1a262fe4e2777f2f` from a separate clean checkout.
It checks every Python/config/overlay Git blob against the authentic #43
lineage identity, checks the loaded native extension and frozen backend blobs,
and imports the unchanged `learning.fit`, scorer, public adapter, controlled
executor, completion store and budget supervisor.

No #43 artifacts are modified. A pinned Stage1 raw manifest authenticates the
train/validation tables and train/validation/heldout split roots plus original
identity and completion record. Root/table identity, owner disjointness,
serialized public inputs, all-action counts and original training target digest
are checked before work. Heldout roots are used only to check reserved owner
splits; heldout labels/results, old checkpoints and433xxx runs are never opened
for training/selection. Source retention is enforced through2026-10-26; expiry,
absence or corruption requires a Planner decision. New artifacts are outside
worktrees, with Issue44 Reviewer/Planner consumers, review/delete2026-11-09.

## Reused-data training and evaluation

Six blocks each use the two prospective seeds in `contract.json`, both arms
sharing each block's candidate seeds. `learning.fit` is called unchanged with
an in-memory seed override; it still performs200 observations/arm,100 SGD
updates/seed and selects `(validation MSE, update, seed)` minimum. Checkpoints
retain the original semantic config hash expected by the unchanged loader;
the new seeds and all optimization curves are bound in the block manifest.
Neither the original config file nor source modules are rewritten.

All12 checkpoint/state hashes and curves are atomically frozen before the
1536-key fresh manifest and before any fresh worker dispatch. Every worker
rechecks source, code, identity, authorization, all model hashes and the full
run manifest. Fresh keys use435001–435128, no Expert arm, matched between
visible/masked within block and repeated across all blocks. The unchanged
native executor supplies A20 Ironclad, privileged Search-v2@400 Battle/no
potions, fixed Expert fallback433042 and20000 max steps. The wrapper changes
only the in-memory evaluation seed range and includes block identity in the
storage path; it never implements game mechanics.

Reports require all1536 valid original keys and≥64 learned MAP/REWARDS in each
arm of every block, zero supported-family fallback. Per-block seed-paired
10000-replicate intervals use440911–440916. The crossed bootstrap independently
draws six blocks and128 seeds with replacement per replicate, retaining their
Cartesian paired grid, seed440901. Its implementation uses multiplicity
weights; a fixture compares it with a separate explicit Cartesian reference.
Signs/ties, all block means/intervals, pooled/block floors/reach/choices,
same-public-state/occurrence disagreement, descriptor shift, model selection,
terminal resources and Search work are reported. Historical #43 is separate.
Classification follows the Issue's crossed-CI plus≥5/6 same-sign block gates;
partial favorable data cannot produce a full positive classification.

## Commands and authority

Use WSL Python with the accepted native extension and clean frozen STSRL source
on PYTHONPATH. A source checkout argument is the directory containing #43
`learning.py`, not the repository root. Runtime paths are inputs and do not
substitute for durable Git/source hashes.

```text
PYTHONPATH=/home/lsmft/.cache/issue43-native-7fef4e19:/path/to/frozen-STSRL/src
python3 launch.py readiness --source-code /path/to/exact-9d5deb3/studies/issue-43-noncombat-semantic-learning --directory /home/lsmft/.cache/issue44-readiness-EXACTSHA --producer-commit EXACTSHA
```

Readiness executes bounded public/action/split/selection/freeze/full-grid/
crossed-bootstrap/authority/resource fixtures, including the pinned adapter
and student fixture suites. It uses synthetic tiny labels only; no authentic
model training or simulator episode occurs. `readiness.json`, worker log,
completion checksums and hash-chained resource journal are retained. No
fixture classification is a scientific result.

Formal command has the same source/producer arguments and one *new* immutable
directory authorized by the Planner, plus:

```text
python3 launch.py formal ... --review-comment REVIEW_URL --planner-comment GRANT_URL
```

The independent Issue44 review comment must contain a standalone line
`ISSUE44_READINESS_PASS` and the exact producer SHA. A later explicit Planner
Issue44 comment must contain standalone `ISSUE44_FRESH_LAUNCH_AUTHORIZED`, the
same exact SHA, the review URL and standalone
`LINEAGE_DIRECTORY=/home/lsmft/.cache/issue44-formal-lineage` (or the exact
chosen outside-worktree path). GitHub URL/owner/order/body hashes are checked.
Use this same grant, commit and directory on every resume. Missing authority,
changed source/model/input/identity or terminal errors fail closed.

One formal append-only resource lineage covers training, evaluation, reporting,
retries and launcher CPU. Limits48000 aggregate process-tree CPU/9000 cumulative
wall, eight workers, RSS8GiB/job, available memory≥4GiB. Kernel worker limits,
external GNU timeout, conservative abandoned/full failed leases, crash clock
exposure and renewed parent leases reuse the pinned audited #43 supervisor.
Only its dispatch directory is assigned to this study's `worker.py`; the guard
implementation is unchanged. Readiness uses separately explicit600CPU/600wall
per attempt; the handoff must report all readiness attempts and preparation
charges together. New independent Reviewer audits must also be charged, never
resetting a formal ledger. All raw results survive outside deleted worktrees.

No cost-only simulator pilot is permitted. #43 audited8297.61CPU/384runs gives
a planning estimate33190.44CPU/1536runs before model training/reporting/audits;
it is not a guaranteed runtime bound. No source regeneration, feature/optimizer
repair, extra seeds, architecture search or automatic policy deployment.
Any eventual result is conditional on one frozen source dataset and the
privileged teacher-assisted regime; it cannot establish independent-source
generalization, Expert superiority, normal-public noSL or Heart performance.
