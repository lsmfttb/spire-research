# Optimizer-only Phase A follow-up

One frozen contrast: exact #47 P objective, features, network and initializer;
SGD0.01 versus Adam0.001, two seeds, 100 epochs. No new game, source or Q label.

Before opening Q tables, freeze metadata from the sealed #46 `root.json` and
#47 `source.json` lineage: SHA256 UTF-8 compact JSON `[50017,owner]`. Natural
cells select 8 old45 and 8 fresh46 origins each; deterministic Hamilton
proportional fallback is declared in protocol.json. Assisted cells select16.
Publish and record the producer commit before execution. No diagnostic outcomes
are read until all four epoch100 checkpoints exist; epoch0 is reconstructed
from the exact same seed, not selected by validation.

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
