# Issue46: source scale × learner capacity

Disposable implementation of the fixed experiment in GitHub Issue46. Base:
spire origin/main 9525d41; external STSRL 7d172f7; repaired reviewed native
18bcd565, extension SHA256 in config. No game mechanics are implemented here.

`adapter.py` and `student.py` are exact reused Issue43 public contract and
feature encoder/scorer code. Both capacities will use `visible` treatment.
Architecture 128→256→256→1 actually has 99,073 parameters including biases
(Issue's ~98,817 is approximate). No representation or scientific contrast
changes. Hidden native snapshots belong only to executor, replay, and evidence.

Before labels, config freezes source/root stratification, small subset rule,
two continuation driver seeds, two model initializations, 100 full-batch SGD
epochs, learning rate .01, no regularization, and validation-only checkpoint
selection. Label target is absolute genuine terminal floor /60. Each option
has inverse legal-count weight, each root has equal weight. Retained labels
include all legal options even when there is only one.

Training owners: original96 authentic Issue45-on owners, new96 natural owners
440001..440096, new192 T042 assist_hp75_potion owners440097..440288.
Validation440289..440352 and heldout440353..440416 each alternate natural
and assisted. Source-only assistance uses existing T042 native transform.
Every continuation restores the frozen same-process native root; two fresh
Expert random streams (446001/446002, each composed with source seed) run
without new assistance. The native root RNG is retained, matching the mature
Issue43 restore design. No source/restore/fork owner crosses splits.

Source roots are selected from reached MAP/REWARDS only, prospectively by
outcome-blind family/act/floor hash stratification. Full source occupancy and
all assistance provenance are recorded. Sealed Issue45 manifest and every
raw file are rehashed; its original-on paths are exactly replayed, with every
snapshot and terminal compared before source reuse. Original evaluation is
training source only.

Launch collection with system Python3.14 and reviewed extension:

    ISSUE46_PRODUCER=<pushed SHA> PYTHONPATH=<reviewed extension>:<pinned STSRL>/src python3 launch_collect.py /home/lsmft/.cache/issue46-source-capacity

8-worker cap, 8GiB per worker, 4GiB free memory reserve planned. Source/label
worker starts have per-owner progress/results and gzip native step evidence.
The original task ledger records settled wait4 CPU, live /proc CPU, parent
CPU and100 charged administrative CPU seconds (preparation/failed environment
checks). Stop at199,800 CPU to settle under200,000; no wall-time cap. First
actual8 prescribed owner keys provide rough costs; there is no separate
canary/readiness phase. Raw data outside worktrees has named consumers Issue46
Reviewer/Planner and review/delete date2026-11-12. Do not restart ledger or
silently replace/thin owner/action/seed keys.
