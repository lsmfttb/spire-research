# Issue 49 Phase A — public context and optimization diagnostics

Study only. No merge or model promotion. No generated source owners, Q continuations, model fitting, fresh A20 runs, or Issue 48 results were used.

## Functional evidence

Initial weights are reconstructed from the selected checkpoint seed and exact original initializer. Selected checkpoints remain unchanged. Every one of the 512 derivative inputs is matched byte-hash to its sealed #47 receipt and Q/serialized content to its sealed #46 source table. Scores use float64, one Torch thread. MSE is against Q/60 with equal root then equal option weights. Q regret is in native floor units. Old heldout is retrospective, not an untouched new test.

| Model | Split | Initial/selected Q regret | Argmax agreement | Initial/selected score margin | Initial/selected centered MSE |
|---|---|---|---|---|---|
| 46/LxB | heldout | 1.47656 / 1.42969 | 0.90625 | 0.00233681 / 0.00211658 | 0.000608352 / 0.00061115 |
| 46/LxB | train | 0.967448 / 0.917969 | 0.888021 | 0.00204426 / 0.00196036 | 0.000589776 / 0.000584039 |
| 46/LxB | validation | 1.14844 / 1.08594 | 0.96875 | 0.00217676 / 0.00218091 | 0.000579714 / 0.000578926 |
| 46/LxT | heldout | 1.32031 / 1.20312 | 0.859375 | 0.00129522 / 0.00172001 | 0.00060286 / 0.0006076 |
| 46/LxT | train | 0.817708 / 0.744792 | 0.835938 | 0.0014331 / 0.00168997 | 0.000585791 / 0.000581653 |
| 46/LxT | validation | 0.71875 / 0.679688 | 0.828125 | 0.00121542 / 0.0012669 | 0.000567636 / 0.00056317 |
| 46/SxB | heldout | 1.47656 / 1.42969 | 0.921875 | 0.00233681 / 0.00214119 | 0.000608352 / 0.000611103 |
| 46/SxB | train | 0.967448 / 0.89974 | 0.867188 | 0.00204426 / 0.00197299 | 0.000589776 / 0.000584397 |
| 46/SxB | validation | 1.14844 / 1.13281 | 0.953125 | 0.00217676 / 0.00216606 | 0.000579714 / 0.000578851 |
| 46/SxT | heldout | 1.32031 / 1.20312 | 0.859375 | 0.00129522 / 0.00174384 | 0.00060286 / 0.00060765 |
| 46/SxT | train | 0.817708 / 0.783854 | 0.835938 | 0.0014331 / 0.00170408 | 0.000585791 / 0.00058233 |
| 46/SxT | validation | 0.71875 / 0.625 | 0.859375 | 0.00121542 / 0.00131401 | 0.000567636 / 0.000561691 |
| 47/A | heldout | 1.32031 / 1.11719 | 0.921875 | 0.00129522 / 0.00155169 | 0.00060286 / 0.000605449 |
| 47/A | train | 0.817708 / 0.786458 | 0.90625 | 0.0014331 / 0.00155611 | 0.000585791 / 0.00058331 |
| 47/A | validation | 0.71875 / 0.453125 | 0.890625 | 0.00121542 / 0.0012066 | 0.000567636 / 0.000564502 |
| 47/P | heldout | 1.21875 / 1.28125 | 0.984375 | 0.00119326 / 0.00119336 | 0.000606092 / 0.000606095 |
| 47/P | train | 0.802083 / 0.802083 | 1 | 0.00126006 / 0.00126017 | 0.000583752 / 0.000583733 |
| 47/P | validation | 0.640625 / 0.640625 | 1 | 0.00115578 / 0.0011554 | 0.000576482 / 0.00057648 |

Complete family/provenance cells and per-root metrics are in diagnostics.json and per-root.json. Natural MAP and assisted REWARDS are absent in both old validation and heldout; do not infer balanced deployment generalization. Training has 96 roots in each of four cells.

## Absolute floor fit versus action ranking

| Model | Actual training roots MSE, initial → selected |
|---|---|
| 46/LxB | 0.0343095689 → 0.00937880329 |
| 46/LxT | 0.0600837996 → 0.00907514041 |
| 46/SxB | 0.0501861163 → 0.0140468226 |
| 46/SxT | 0.0811311409 → 0.013363588 |
| 47/A | 0.0600837996 → 0.0172014798 |
| 47/P | 0.0450250072 → 0.0450232351 |

S models use their actual 96-owner training subset for this MSE; partition rows describe all historical roots, including 288 training-labelled roots outside S fitting. #46 Bigger did learn substantial common floor prediction. Small parameter displacement alone does not establish lack of training. Action regret, centered score changes and concordance must be examined independently.

## Preference gradients

| Objective | Full gradient L2 | Cancellation ratio | Mean root cosine | Pair logistic derivative mean | Hypothetical SGD0.01 centered score-change RMS |
|---|---|---|---|---|---|
| 47/A | 0.736513394 | 0.881039764 | 0.771900666 | -0.500101044 | 6.96655187e-05 |
| 47/P | 0.00726645799 | 0.131474446 | 0.0109227903 | -0.499962923 | 2.40467681e-06 |

P gradients do not vanish: the output bias cancels exactly (floating error ~1e-18), hidden bias gradients are small, and the informative-root average retains about 13% of the mean root-gradient norm. Logistic derivatives remain near −0.5, not saturated. Small action-difference gradients, partial root cancellation and SGD0.01 jointly produce very small centered score movement. This receipt supports slow accumulation under the frozen objective; it does not uniquely establish a causal optimizer defect. Existing first/final objective records are preserved alongside layer norms. The hypothetical direction is evaluated once and restored immediately; no model is fitted or saved.

## Public-v2 prototype and limitations

The normalizer binds card fields within each entity, retains all currently projected legal offers, canonicalizes mutually exclusive groups, and uses public local group/card content keys. Native indices establish public grouping before they are stripped; opaque executor fields do not enter features. Duplicates retain multiplicity. Exact semantic and candidate×context tokens demonstrate representability and reorder equivariance without selecting a neural architecture or implying learned strategic quality. Full public_state is retained from the trusted adapter; arbitrary nested objects supplied by an untrusted caller are outside this contract.

Complete normal-player observation equivalence is not established: pinned reward projection omits misc and exact upgrade_count, whose inspectability relevance needs further source-public contract review. Unavailable fields cannot be recovered by the normalizer. See visibility.md for exact reward-field gaps and transition caveats. Unknown hidden RNG/top-level future content is excluded; no post-action future facts are backfilled. Honest synthetic public fixtures are not game-strength evidence.

## Recommended single next treatment for Planner approval

After resolving the source-public snapshot gaps documented by the audit, prescribe one complete current-public choice-set conditioning treatment with explicit group/entity pointers versus the unchanged frozen baseline. Freeze its optimizer and function-space learning thresholds prospectively, with balanced natural/assisted × MAP/REWARDS validation owners. Keep labels, Battle controller and all other treatment choices fixed. No Phase B fitting or complete-run comparison is authorized here; no winner or new cohort is selected.

## Reproduction

Use Python 3.13 Torch 2.12.0+cpu for diagnose.py and verify_gradients.py; system Python 3.14 for prototype/native checks. Commands accept an independent output directory; original stores remain read-only. Native harness uses explicitly synthetic default GameContext data and pinned source transitions, never a seeded game constructor.

```text
python diagnose.py /home/lsmft/.cache/issue49-public-diagnostics
python verify_gradients.py /home/lsmft/.cache/issue49-public-diagnostics
python test_prototype.py /home/lsmft/.cache/issue49-public-diagnostics
```
