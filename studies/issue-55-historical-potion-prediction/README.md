# Issue 55: historical potion information prediction pilot

This is a disposable, `STUDY_ONLY` analysis of the 96 reviewed #45 on-arm owners. It fits the two preregistered 256-dimensional owner-weighted ridge regressions against remaining terminal floors. It does not train a controller, estimate potion use-versus-retain value, or make a current-Native claim.

## Run order

Run with Python 3.11+ from the shared workspace or a machine with the sealed #45 source cache mounted at the preregistered path:

```sh
python3 run_study.py prepare
python3 run_study.py fit
python3 run_study.py evaluate
python3 run_study.py repair-reporting
```

`prepare` verifies the source seals and opens only the preregistered 64 training owners. It writes a compressed, allowlisted public-feature/target file without raw snapshots. `fit` fits the frozen visible and masked models from that file, writes both coefficient files, then writes the freeze receipt. Only then may `evaluate` open the 16 heldout owners. The 16 validation owners are never opened or used.

`repair-reporting` is the bounded independent-review follow-up. It requires the completed heldout evaluation and verifies the unchanged freeze, model, admission, and safe-feature hashes. It adds owner-equal heldout coverage against the frozen training 90th-percentile residual bands, plus descriptive present/offered/legally-usable support by partition, screen family, and Act 1 floor band from the already sealed train and heldout decision receipts. It does not refit either model, change the split or metrics/bootstrap results, read validation receipts, or add legal/selected action identities to model inputs. The CPU ledger carries the independent review charge and the measured reporting-repair stage.

Study JSON files keep LF line endings so the byte-level freeze hashes remain stable in Windows/WSL shared worktrees. The reporting-only opportunity audit reduces sealed legal action kinds to binary support counts; it does not export candidate identities or read selected-action identities.

If a stage ends with a source-admission failure and an ordinary implementation correction is made, use the matching `retry-prepare`, `retry-fit`, `retry-evaluate`, or `retry-reporting` command. A reporting stage left at an ordinary `FAILED` boundary can be rerun directly with `repair-reporting`. The previous attempt stays in the execution history and CPU ledger. A `NO_IDENTIFIABLE_POTION_FEATURE_VARIATION` terminal result is not rerun into another specification.

Each stage persists its start, last owner checkpoint, CPU time, status, and error boundary in `artifacts/execution_state.json`. The separate CPU ledger charges a conservative 25 CPU seconds for Builder setup, records the independent review charges, includes measured stage process time, and stops work before Builder plus admin exceeds 1,900 CPU seconds. The 600-second reviewer reserve is reduced by already charged reviewer work. If a process dies mid-stage, the next invocation records the interrupted boundary and reserves 60 CPU seconds for that attempt.

## Public-feature boundary

The source decision records contain the mixed raw Native snapshot. The extractor reads only `act`, current floor, current screen identity, current/max HP, deck card `id_label` multiset, relic `id_label` multiset, potion count/capacity, and ordered potion slot `id_label`s. For battle snapshots it reads only `battle_player.current_hp/max_hp` and the separate battle potion inventory fields. It checks redundant decision-time values when present and adjacent public pre/post fields, but does not put chosen actions, outcomes, owner IDs, seeds, controller/search metadata, gold, keys, map/reward payloads, or private battle internals in the feature vectors. Field-shape corrections and an evaluation integrity correction are recorded in `preregistration-amendment-001.json` through `preregistration-amendment-004.json`.

The pinned STSRL trace helper reads GameContext `cur_hp` and `max_hp` before nested BattleContext values. Some battle rows therefore have legacy summaries that differ from the BattleContext HP exposed by the pinned Native public projection. The feature uses the projection-backed BattleContext fields in battle, validates the legacy summaries against their raw source fields, and reports the count of such disagreements.

The code does not copy source traces into outputs. `train_public_features.jsonl.gz` and `heldout_public_features.jsonl.gz` contain only owner/row bookkeeping, the terminal-return target, and the explicit public feature tokens. No validation data, private snapshots, or large raw traces are exported.

## Result interpretation

The primary heldout statistic is the 16-owner equal-weight mean of each owner's masked-model MAE minus visible-model MAE. Positive values favor the visible-potion model. The 95% interval resamples whole owners using the frozen SHA-256 counter stream. `report.json` records a Builder preliminary classification; the independent Reviewer must confirm integrity and the classification before the result is terminal. Regardless of the result, this pilot remains `STUDY_ONLY`. A prediction difference cannot distinguish information from Expert visitation/selection effects and does not estimate potion-use strategy value.
