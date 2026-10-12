# Issue 45 bounded failure evidence

Classification: **POTION_ACTION_MAPPING_INVALID**. This is a failed execution, with no estimated potion effect or bootstrap interval. All 192 prescribed keys are accounted for: 25 valid native terminal runs, 1 mapping failure, 6 interrupted processes, and 160 not started. The scheduler stopped and settled every active process.

Executed producer: `78122615193ac00600205805fcfa491d6186fc98`. This evidence commit does not change the executed scripts. Native source: `7fef4e19c7ee13f049ee0fb45c9967e7e349fcfb`; STSRL source: `7d172f7c24c9d29429b6946ba44f6b59760b0d96`. Extension hash is recorded in summary.json.

## Concrete failure

Seed 437016, on arm, decision 27, floor 2, TWO_LOUSE, PLAYER_NORMAL, energy 0, Fairy Potion in slot 0. Legal discard has idx2=6 and bits=537264128. Native `src/sim/search/BattleScumSearcher2.cpp::enumeratePotionActions` constructs the Fairy edge with target -1. Native `bindings/slaythespire.cpp::buildBattleSearchReport` maps edges by action-bit equality. With potions enabled, all 400 visits belong to an unmapped edge and every legal root row has zero visits. The same actual state with potions disabled gives end_turn 400 visits. The full same-state reports and snapshot are in counterexample.json.

The existing T041 highest_mean selector correctly refuses this report. Its Python controller cannot recover the missing edge value/action from returned root rows. Repair requires native canonical discard/root mapping work; no local game rules, invented values, alternative search, or core migration were introduced. Reviewer should assess this narrow source/capability boundary and then return the blocked study to Planner for disposition.

## Verification and limitations

Both existing T041 potion identity/root mapping fixtures passed through direct invocation of their actual functions (pytest is unavailable). First scheduled potion-bearing root passed native off/on parity. That parity did not cover the passive Fairy discard root. Deterministic replay of the exact failed seed reproduced the mismatch and retained both 400-visit reports; it is diagnostic evidence, not a replacement study outcome. All completed outcomes, producer/seed/arm identities, off-arm Battle consumption, and the full 192-key disposition were audited. Heart-specific and detailed loss-type analysis remain incomplete. Partial descriptive metrics do not support a gain or harm claim.

One task ledger charged 686.105714014 CPU seconds, including 100 preparation, 1 fixture, diagnostic cost, and 30 cleanup/handoff allowance; sealed wall snapshot 1241.4353368282318 seconds. Caps were 12000 CPU / 3600 wall. Killed jobs were charged their actual settled CPU, not unused leases. Final handoff wall elapsed is reported in the issue comment.

Raw artifacts: `/home/lsmft/.cache/issue45-potion-paired`. Manifest covers 138 files (62,688,755 bytes), SHA256 `7d0ecf2f9e024ae0dc40e529209b3a032f7ee5b70ea8a59ee98e46e20b88c90f`. Remote evidence includes the manifest, all-key disposition, fixtures, parity, counterexample, summary and partial report. Large raw streams remain at the named path for independent Issue45 Reviewer/Planner, with review/delete date 2026-11-09. No Issue43/44 raw data or models were used.

Disposition: **STUDY_ONLY**, no merge, deployment, or core promotion. Retain this remote branch as the exact failed-study handoff; delete disposable study code after review if no named consumer remains.
