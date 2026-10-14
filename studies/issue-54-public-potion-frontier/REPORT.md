# Issue #54 — targeted retained-cohort audit

**Purpose:** `STUDY_ONLY`. This audit checks only the named retained whole-run sources. It does not create games, labels, continuations, policy variants, or training data. Raw source files remain in their existing caches; only hashes and coarse per-owner counts are committed here.

## Finding

The reviewed #45 potion-on arm is physically available as 96 complete, distinct Act 1 starts under one identical recorded `issue45_search_v2_400+expert_non_combat_v1` controller configuration. The source and each on-arm metadata/transition file match the sealed manifest. It provides historical, descriptive action support. It does not establish current-Native or normal-public compatibility, and it is not a causal use-versus-keep comparison.

**Assessment status for a normal-public offline prediction task: `INCOMPLETE_PUBLIC_CONTEXT`.** This status is driven by the unverified normal-public projection and old simulator pin, not by an owner-count cutoff; no automatic sample-size or power gate is applied.

The indexed Expert arms for #46, #47, and #48 each name 128 candidate episodes, but none of the expected per-run metadata or transition files are physically present in the local cache (0 of 256 files per source). Their sealed manifests are evidence of expected artifacts, not evidence that those runs can be independently reverified or pooled now.

## #45 on-arm observations

| Observation | Owners / rows |
|---|---:|
| Verified complete Act 1 starts | 96 owners |
| Valid terminal outcome | 96 `PLAYER_LOSS` |
| Distinct terminal floors | 14; 41 at F00–F08, 50 at F09–F16, 5 at F17+ |
| Recorded decisions with pre- and post-snapshot objects | 12,206 of 12,206 |
| Selected identity equals indexed legal action and is eligible | 12,206 of 12,206 |
| Act 1 Battle decisions with a legal potion-use action | 1,023 rows / 83 owners |
| Actual use at such a decision | 183 rows / 79 owners |
| Another action selected while use was legal | 840 rows / 78 owners |
| Owners showing both observed classes | 74 |

“Another action selected while use was legal” is only a decision-time non-use observation; it does not mean the potion was retained to battle end. Owners may appear in both classes. The 186 total Battle potion uses are within 96 complete source runs, not 186 independent examples.

For potion reward decisions in Act 1, the traces contain 226 take choices, 34 skip choices, and 241 other choices while a potion option was present. The sealed run metadata records 429 potion-offer option occurrences. Six Act 1 offer contexts were recorded with a full potion inventory; those contexts exposed 12 discard options, with no discard choice recorded. Across the full runs there were 231 reward-potion acquisitions, 27 shop-potion purchases, 50 non-Battle potion discards, and 3 non-Battle potion uses. These counts describe recorded choices and opportunities; they do not estimate policy effects.

## Provenance and compatibility

The #45 on arm uses Native `18bcd565b587186425bb46ad3d70a2eb80d947fc`, extension SHA256 `0652b1ce6d757404278b840678b1f8548c81fb6b03cade598e7f3d82524b18d2`, and frozen STSRL `7d172f7c24c9d29429b6946ba44f6b59760b0d96`. All 96 metadata records share the same controller name and canonical configuration fingerprint. The current accepted Native branch is `stsrl/main@9e6f2acc50188a05ea5cc10b760e6f96c6856c8e`; the historical source pin differs, so current trajectory compatibility is **not ascertainable without a replay**, which this issue does not authorize.

No direct #52-sensitive event was observed for 93 owners: no Blood Potion use with Sacred Bark, Smoke Bomb escape transition, victory with Meat on the Bone and Burning Blood/Black Blood, or Face of Cleric victory-to-Rewards transition was recorded. Three owners have non-Battle potion-use records whose selected label did not match a captured inventory slot; those owners remain unascertainable for item-specific exposure. This path classification does not imply that the old run would reproduce under current Native.

Pre/post snapshots and action receipts exist in the historical traces, but this targeted cache check did not re-adjudicate their fields against the accepted normal-public projection contract. Thus normal-public input compatibility is not established. The cohort's Battle controller is privileged Search-v2@400, paired with Expert non-combat decisions; these outcomes are historical `V^Expert/Search-v2@400`, not labels for a new learner or a normal-public incumbent.

The #46–#48 source-level config fingerprints differ, and the per-run files needed to inspect their exact Expert controller configurations are absent. Do not merge their 384 manifest-indexed candidates with #45. The resulting 480 is only a manifest-level upper bound, not 480 verified or eligible owners.

## Disposition

Keep this as a historical descriptive cohort only. Do not promote it to model-ready data, train a shared critic, infer causal potion-use value, or treat it as current-Native evidence. The next step belongs to Planner under #53: choose the future-policy estimand and decide whether a narrowly defined historical prediction task is useful given the unverified normal-public inputs. Any future replay or new source collection requires separate authorization.

Detailed per-owner aggregates and file receipts are in `targeted-summary.json`; cohort file availability is in `availability-matrix.csv`; source pins and remote refs are in `source-inspection.json`. The bounded CPU ledger is `targeted-execution.json`. Source retention dates remain unchanged; the study artifacts are marked review/delete by 2026-11-14.
