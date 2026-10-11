# Readiness-only follow-up to Issue43

The 2026-10-11 Planner amendment retains the original config Git blob
`12a1119933beecb72e52922444b387c9d9eaa0c2`. Only admission execution and
synthetic readiness checks are authorized. There are no formal fitted models,
formal return tables, or fresh 433xxx runs in this follow-up.

## Pilot selection and repair

`pilot.py` scans the fixed 434001..434064 stream in ascending eight-seed
blocks. Results are consumed in seed order; an unused tail still counts as
source cost. Each source runs to true terminal, retaining its first qualifying
MAP and REWARDS independently. MAP-only owners remain eligible. No source
outcome or execution timing determines selection. All original legal options
are retained and each receives drivers 430071 and 430072.

Opaque checkpoints stay in their creating process. Every forced branch checks
hidden checkpoint fingerprint, original serialized public identity and fresh
native legal mapping. The pilot stores execution cost/validity, not Q labels.

The first attempt's per-root cost guard stopped an incomplete REWARDS branch.
The attempt remains immutable. `resume_pilot.py` debits its complete sampled
worker CPU plus a 20s parent/launch allowance and its wall time. It verifies
completed-key checksums, prices every unfinished original key, and subdivides
the remaining work into jobs of at most six branches. This is a scheduling
repair; it does not replace roots/actions/drivers or treat partial observations
as Stage1 admission. A second failure stops rather than extending this repair.

## Exact formal execution plan, conditional on separate admission

1. Verify config/native extension/backend/adapter/producer source hashes.
   Use the existing 8-worker process-tree CPU/wall/memory supervision pattern
   with Stage1 72000 CPU/18000 wall and Stage2 36000 CPU/12000 wall limits.
   Retain cumulative completed and failed attempt costs on resumption; never
   reset a scientific stage's budget by making a new attempt directory.
2. Generate source owners in ascending immutable config ranges. Call
   `formal.capture_owner` under the common source driver and Battle teacher;
   retain both first qualifying families independently, plus counters for
   OTHER/single-action screens and every rejected owner's full source cost.
   Invalid supported variants fail closed, not fallback or sample replacement.
3. `formal.cohort` freezes exactly train 96 MAP/160 REWARDS, validation 24/40,
   heldout 24/40. `validate_splits` checks config owner ranges, family occupancy,
   duplicate owner-family exclusion and minimum independent owner counts.
   Hash and atomically publish this cohort before branch returns. Keep heldout
   output under a separate executor-owned directory; the learner receives
   only train and validation tables.
4. For each selected owner, call `formal.execute_owner`. Regenerate the frozen
   source and compare complete root metadata. Checkpoints remain same-process;
   every original action/driver has an exact key including public/candidate and
   privileged checkpoint identities. `Completed` owns one append-only fsynced
   manifest per worker. Atomic completed results precede completion records;
   an orphan result is validated and republished, while corrupt/missing records
   stop. Started/progress/invalid boundaries locate failures. On interruption,
   reconstruct the same source and skip only checksum-verified completed keys.
   A truncated final manifest line fails closed; preserve the attempt and use a
   new manifest referencing verified immutable results. No invalid/capped run
   becomes a death, zero return or completed key.
5. `formal.targets` requires exactly every original ordinal × config driver,
   then averages `max(0, terminal_floor-source_floor)` over those drivers.
   Terminal resources/outcomes remain separately privileged execution evidence.
   `learning.fit(train, validation)` has no heldout argument. Both arms consume
   identical all-action Q tables, weight each root equally, use the fixed
   feature/target transform, 2081 parameters, two shared seeds and 100 full
   batch SGD updates. Select by mean per-root validation MSE, earliest update,
   then smaller seed. Atomically freeze/hash both checkpoints before opening
   heldout. `learning.load` verifies checkpoint/config integrity.
6. Report heldout all-action Q MSE, chosen-action realized-world Q/regret,
   visible/masked ordinal disagreement, family results and owner-clustered
   uncertainty with fixed 432091 bootstrap seed. Never train or select with
   heldout. These are realized-world curriculum-proxy measurements, not fresh
   A20 causal outcomes.
7. After complete Stage1 validity and further authorization, freeze
   `formal.evaluation_plan`: exactly 433001..433128 × visible/masked/expert_source,
   keyed by config and checkpoint hashes. `evaluate_one` uses the exact common
   Battle teacher and fresh-driver433042 fallback; OTHER is explicit Expert,
   MAP/REWARDS use public serialized inputs only and malformed support fails.
   Preserve complete terminal raw execution evidence, per-family learned/
   fallback counters, public checkpoint hashes/ordinals and failed executions.
   Shared-state disagreement is only identifiable where public hashes match;
   never pair action ordinals after trajectories diverge.
8. Require all384 genuine-terminal runs, no seed exclusions, at least64 MAP
   and64 REWARDS learned choices/student, and zero supported silent fallback.
   Pair terminal floors by simulator seed. `clustered_interval` resamples seed
   owners 10000 times: 433091 visible-minus-masked, 433092 visible-minus-Expert.
   Report Act2+ nonregression using the native terminal progress fields, Heart
   wins, resources, source/option shift and family occupancy separately. Apply
   Issue43's fixed claim rules; privileged Battle means no noSL whole-run claim.

## Scope of completed smoke and remaining gates

`test_formal.py` exercises exact 384-root synthetic quotas/owner splits,
all-original-action keys, Q semantics, missing/duplicate/nonterminal rejection,
manifest restart/orphan/checksum handling, equal-target 2×100 training, frozen
model integrity, heldout reservation, metadata exclusion, malformed supported
fail-closed behavior, 384 fresh-key planning and deterministic clustered
bootstrap. These fixtures are not scientific labels.

`readiness_smoke.py` trains synthetic ordinal targets using admission public
fixtures, freezes two synthetic checkpoints, and executes all three controller
arms on admission seed434001 to true terminal. Learned arms must execute both
supported families and explicit OTHER fallback. Its model files are marked
SYNTHETIC_READINESS_ONLY and must never enter formal evaluation.

Remaining independent review gates: exact-head recovery/cost audit, full formal
launcher orchestration and cumulative stage watchdog wiring, complete heldout
metric/report and fresh coverage/shift/claim reporting, and scientific-cohort
occupancy/terminal validity once admitted. The tested primitives and synthetic
dispatch are readiness evidence; neither a complete formal dataset nor a
certified runtime guarantee exists. If the inclusive conservative price is
RESOURCE_BLOCKED, none of these gaps permits starting a smaller study.

## Retention and resumption

Raw follow-up artifacts are outside worktrees at
`/home/lsmft/.cache/issue43-admission-followup`, produced by the recorded exact
commits/source hashes. Named consumer: Issue43 independent Reviewer;
review/delete date: 2026-10-18. Original and independent replay artifacts retain
their previous ownership/lifecycle. The loaded native binary remains available
for this consumer. Compact reports/manifests only are committed.

Example admission commands (frozen PYTHONPATH points at native cache, read-only
STSRL src and this study; set ISSUE43_PRODUCER_COMMIT to the executed commit):

```sh
python3 pilot.py --scan-only --artifacts /home/lsmft/.cache/issue43-admission-followup/scan01
python3 pilot.py --roots /home/lsmft/.cache/issue43-admission-followup/scan01/roots.json --artifacts /home/lsmft/.cache/issue43-admission-followup/pilot01
python3 resume_pilot.py --scan /home/lsmft/.cache/issue43-admission-followup/scan01/roots.json --previous /home/lsmft/.cache/issue43-admission-followup/pilot01 --artifacts /home/lsmft/.cache/issue43-admission-followup/pilot02
python3 -m unittest test_adapter test_student test_formal
python3 readiness_smoke.py --roots /home/lsmft/.cache/issue43-admission-followup/scan01/roots.json --output /home/lsmft/.cache/issue43-admission-followup/readiness-smoke.json
```

Existing immutable directories are evidence, not runnable command targets for
another attempt. Preserve them; a new repair must first debit their costs and
review its full remaining-work preflight. Formal stage launch has no automatic
CLI authorization in this branch.
