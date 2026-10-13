# Issue51 — frozen two-driver Q audit

STUDY_ONLY. All 512 original owners, 2886 label JSON receipts and 2886 gzip traces verified against original seals. No games, new labels, learning or simulator import. Traces begin after the forced action; full original raw states are audit-only and are not exported.

## Ranking stability

| Original cell | Owners | Multi-action | Q informative | All mean-Q ties | Roots with reversal | Root-weighted reversal | Top sets disjoint |
|---|---:|---:|---:|---:|---:|---:|---:|
| heldout/assisted/MAP_SCREEN | 32 | 21 | 15 | 17 | 1 | 2.381% | 1 |
| heldout/natural/REWARDS | 32 | 27 | 23 | 9 | 13 | 19.259% | 10 |
| train/assisted/MAP_SCREEN | 96 | 55 | 38 | 58 | 6 | 5.333% | 3 |
| train/assisted/REWARDS | 96 | 84 | 64 | 32 | 23 | 10.454% | 18 |
| train/natural/MAP_SCREEN | 96 | 63 | 41 | 55 | 7 | 7.143% | 6 |
| train/natural/REWARDS | 96 | 79 | 60 | 36 | 23 | 10.386% | 15 |
| validation/assisted/MAP_SCREEN | 32 | 24 | 17 | 15 | 2 | 5.556% | 1 |
| validation/natural/REWARDS | 32 | 24 | 16 | 16 | 5 | 8.194% | 2 |

Train has 103/384 single-action roots. Of 181 total mean-Q-tied roots, 78 are multi-action; 2/78 conceal opposing driver action differences. Strict reversals occur in 59/281 multi-action roots, distinct from the root-mean pair fraction. Across 281 multi-action roots, equal-root pair rates are concordant 21.428%, strict reversal 8.690%, one-driver tie 31.392%, both-driver tie 38.490%. Raw legal pairs are descriptive, never independent replicates.

The 181/384 all-mean-Q ties include 179 roots where each driver is internally constant and 2 roots with driver action variation hidden by the mean. 121 roots have one identical floor across all options AND drivers. 20 training roots have some artificial mean-equality pair, even when other actions make the root informative.

Exact top-action sets have mean Jaccard 0.638544 across all multi-action roots and 0.509512 across 203 mean-Q-informative roots. Midrank Spearman 0.340806 and tau-b 0.332663 are defined on only 131 training roots; constant rankings are undefined. Either-driver informative roots (205) are also separately reported.

## Floor-label signal

Equal roots, equal legal options within root: train mean common floor 13.627286; q/60 mean 0.227121; within-root variance 2.108179 floors² versus between-root variance 30.376759. The state-common share of globally centered label variance is 93.510%. E[q²] = E[mu_root²] + E[within variance] = 216.079694 + 2.108179. This is a target decomposition / hypothetical zero-prediction error, not a fitted model's MSE attribution. Divide squared terms by 3600 for q/60.

Full report includes floor/act, original provenance/origin, potion-held and potion-action strata, conditional action-category summaries, action-centered labels and terminal-floor/max-act masses. Denominators are explicit. Categories overlap; no reweighting or altered loss is proposed.

## Public trace evidence and pinned policy

All trace chains and terminal outcomes match. 6 deterministic equal-floor matched public-gate examples and 4 paired-driver differing-decision examples are in public_examples.json with exact owner/root/ordinal/driver and label/gzip hashes. Only explicit public HP, gold, potion/deck identities, floor/act and chosen-action fields are projected. Matched gates need not be identical hidden states; no causal outcome comparison follows.

Pinned STSRL 7d172f7c24c9d29429b6946ba44f6b59760b0d96 Expert source is category-first weighted stochastic sampling from public resources and card/route labels, including positive 0.02 category/action floors. Fixed unguided privileged Battle Search-v2@400 includes potions. Source facts do not establish strong optimization or causally weak noncombat play. Battle remains privileged, with no learner features created.

## Interpretation

Observable strict within-root preference reversals and one-driver ties make finite two-driver resolution a plausible contributor, especially reward cells. They do not identify variance over independent hidden worlds or establish it as the major cause of optimizer diagnostic failure.

Most all-mean-Q ties do not conceal opposing driver preferences: count the driver-internally constant cases separately, including single-action roots. Such ties still do not prove strategic indifference.

Natural versus assisted/origin/floor differences are observational source mixtures; old validation/heldout omit natural MAP and assisted REWARDS. They cannot identify a causal assistance or policy effect.

Q under forced action then fixed Expert/Search continuation is different from value under repeated learned noncombat decisions. More samples under the same Expert improve precision of that target but cannot change the continuation policy.

Integer terminal-floor equality coexists with different public resources/decisions at matched public act/floor/screen gates. This exposes outcome resolution, not which choice increases Heart win probability.

Pinned category-first weighted heuristic has positive low-probability category/action floors. Observed stochastic trajectories plus source establish sampling behavior; not causal weakness or comparative strength.

Original validation and heldout each have 32 assisted MAP and 32 natural REWARDS; natural MAP and assisted REWARDS are absent. Old labels are retrospectively exposed. No #48/#49/#50 model outcome is used to select a variant.

## Exactly one unexecuted next option

Controlled policy evaluation under one newly frozen public incumbent. On prospectively sampled owner-disjoint naturally reached public noncombat roots, fixed terminal-floor action advantages under a frozen public incumbent differ reproducibly from those under the frozen Expert continuation.
Planner would separately preregister one incumbent, authentic independent root selection and split, continuation randomization/repetitions, paired initial-action protocol with same Battle/Search regime, uncertainty and compute cap. Compare fixed Expert versus incumbent continuation with terminal floor unchanged; evaluate preference consistency and counterfactual action ranking on heldout roots. No label/source/model selection from current exposed data.
If paired heldout advantages and ranking preference are stable across the two frozen continuation policies within prespecified uncertainty, evidence for substantial continuation-target mismatch is weakened.
NOT EXECUTED; requires separate Planner authorization. No extra sampling, labels, training or game started.

## Reproducibility and retention

Run audit.py, then run_traces.py, then report.py against the original sealed stores. Inputs, source pins, full per-root metrics and public-only examples have SHA256 receipts. Original #46/#47 seals and November12/13 retention remain unchanged. Issue51 derived evidence belongs to independent Reviewer and Planner through 2026-11-14. Code is disposable and has no permanent integration or merge disposition.
