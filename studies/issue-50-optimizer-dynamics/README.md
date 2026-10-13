# Optimizer-only Phase A follow-up

One frozen contrast: exact #47 P objective, features, network and initializer;
SGD0.01 versus Adam0.001, two seeds, 100 epochs. No new game, source or Q label.

Before opening Q tables, freeze metadata from the sealed #46 `root.json` and
#47 `source.json` lineage: SHA256 UTF-8 compact JSON `[50017,owner]`. Natural
cells select 8 old45 and 8 fresh46 origins each; deterministic Hamilton
proportional fallback is declared in protocol.json. Assisted cells select16.
Publish and record the producer commit before execution. The metadata split is
frozen before Q tables are opened. For each arm and seed, run.py computes
deterministic epoch0 metrics on training and diagnostic roots before fitting,
then epoch100 metrics after that arm's fixed 100 epochs. Later arms may not yet
have completed when these metrics are computed. Diagnostic metrics do not
select checkpoints or change training, tuning, or the fixed execution plan.
Epoch0 uses the exact same initialization seed; it is not selected by validation.

The original 87ab17c evidence seal preserves the earlier, incorrect README
timing statement. The additive documentation-repair receipt records this
correction and verifies the original source and output hashes without refitting.

WSL Python 3.13 Torch runtime:

```text
ISSUE50_PRODUCER=EXACT_REMOTE_PRODUCER_SHA python freeze.py STORE
python run.py STORE
python report.py STORE
python seal.py STORE
```

`train47.py`, `student.py`, `common.py`, `adapter47.py` and original config are
exact #47 source copies, with direct Git blob integrity checks. The old trainer
main is never invoked. Only its initialization, feature arrays, pairwise loss
and checkpoint serialization are reused. Models consume serialized public
features only. Q labels and assistance/source metadata are external.

Named artifact consumer: Issue50 Reviewer then Planner; review/delete2026-11-14
unless explicitly extended. All-in CPU upper ceiling3000, including imports,
setup/failures/publication and root coordination; reserve1000 for independent
Reviewer. No outcome-dependent optimizer/encoder/model variants; study only,
no merge or promotion. Original caches and retention remain unchanged.
