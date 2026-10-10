# Issue #37: Rehydrate T087 Boss review cases A–F

This is a compact, replay-free projection of the six already selected T087 Boss cases. It uses only the retained T087 `natural_evidence` rows, the existing blind bundle, and its hidden trace-to-source map. It does not scan T088's formal raw data, rerun a simulator, or change STSRL/native code.

## Open the review pages

1. Open `index.html`. It shows only the public battle-entry view and the complete recorded deck for each case. It omits source identities, selected actions, and terminal outcomes.
2. After inspecting the entry view, use its gate to open `timeline.html` or `prior-notes.html`. The timeline contains only public per-step snapshots and the selected action. It explicitly says the complete legal-action alternatives were not recorded; the card-level `playable` flag is not a substitute.
3. `outcomes.html` is a separate optional page linked only after the timeline gate. Results are raw recorded terminal fields, not judgments about deck winnability or controller correctness.

No new human annotations are collected. The old `poor` / `adequate` notes remain provisional resource impressions from an incomplete view, never `start_winnability`, `dominant_failure_source`, or adjudicated outcomes. A–F are a deliberately selected audit subset, not a representative sample.

`source-provenance.private.json` contains the original trace IDs and exact `selection_identity` joins, bound to the input artifact hashes. It is deliberately separate from the human-facing pages and packets. No source ID or terminal result is present in `index.html` or `entry-view.json`.

## Evidence and limitations

The generator verifies file size, SHA-256, and schema against the retained `t087-retention-manifest.json`; maps each fixed A–F trace ID through `t087-blind-audit-hidden-provenance.json`; joins the exact `selection_identity` to one raw row; and checks every retained step's public tactical state and chosen-action identity against the accepted blind bundle.

The `t087-natural-run-manifest.json` records native identity `96052d24b9c2c16ff25b6f7241edd972613be997`. The retention manifest separately records the source-lineage check from historical `d62ff35579b54d70a7428afdf84743c94df3fe0c` to verified ancestor `96052d24b9c2c16ff25b6f7241edd972613be997`. The current native ref quoted in Issue #37 is context for the same already available public snapshot fields; this task consumes no current native binary.

The retained action rows have no legal-action alternatives. They contain the selected action identity and public snapshots only. The human packet therefore does not reconstruct or infer the full legal set. The action space used by the source Search controller was `initial_no_potions`; inventory potions are shown as resources, not as eligible Search choices.

Enemy move display preserves the native snapshot's public `current_move` and `intent_category` names. It does not guess a future move from an ID or rewrite those fields as an inferred intent.

No raw 403 MB input is copied into this repository. Source identities, terminal results, and old notes are kept in distinct machine-readable files. `field-allowlist.json`, `field-coverage.json`, `output-manifest.json`, and `run-state.json` record the exact projection, evidence hashes, coverage, and execution boundary.

## Rebuild and verify

On the original WSL environment, with the retained artifact root available:

```powershell
wsl.exe -e /home/lsmft/stsrl-spikes/py313-torch/bin/python studies/issue-37-rehydrate-t087-boss/rehydrate.py
```

The script uses only the Python standard library and writes bounded outputs to this directory. It fails closed on hash/schema mismatches, absent exact source joins, a blind-trace mismatch, missing required public start resources, or unexpected legal-action fields.

The completed state means `STAGE_0_PASS_STAGE_1_PACKET_READY_FOR_REVIEW`; it is not a scientific verdict and does not authorize new annotations. Planner should first verify the packet and the independent Reviewer should check the exact mapping, allowlist, and disclosure order.
