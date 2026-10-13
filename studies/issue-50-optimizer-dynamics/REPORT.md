# Issue 50 optimizer-only retrospective diagnostic

Study only. SGD0.01 versus Adam0.001 is the sole frozen intervention; exact #47 public features, 2,081-parameter network, initialization and pairwise objective. Two seeds, exactly 100 full-batch epochs each. No checkpoint selection, fresh native games, new owners or new Q labels.

## Primary diagnostic

D−S is negative when Adam has lower empirical Q regret. Each owner averages the two seed-specific regrets; seeds are not additional owners.

Mean D−S: **0.52734375 floors**; stratified owner-paired 95% linear percentile interval **[0.17568359375000003, 0.9571289062499986]**. Adam lower/higher/tied regret: **9/14/41** of 64 owners.

| Diagnostic cell | D−S floors | 95% within-cell empirical interval |
|---|---|---|
| assisted/MAP_SCREEN | 0.625 | [0.078125, 1.296875] |
| assisted/REWARDS | 0.25 | [-0.375, 1.203125] |
| natural/MAP_SCREEN | 0.890625 | [-0.03125, 2.078125] |
| natural/REWARDS | 0.34375 | [-0.09375, 0.921875] |

## Initialization versus epoch 100

| Arm / seed | Split | Initial → final regret | Choice changes | Initial → final margin | Common shift / centered motion RMS |
|---|---|---|---|---|---|
| D-446101 | diagnostic | 0.625 → 1.1484375 | 28/64 | 0.0014152471 → 0.10373244 | 0.20946154 / 0.10116821 |
| D-446101 | train | 0.8375 → 0.4890625 | 149/320 | 0.0012290263 → 0.11557169 | 0.08868265 / 0.10336374 |
| D-446102 | diagnostic | 0.90625 → 1.2734375 | 25/64 | 0.0013076356 → 0.10056773 | 0.30879374 / 0.097017016 |
| D-446102 | train | 0.8 → 0.4640625 | 126/320 | 0.0014581903 → 0.11478266 | 0.17985646 / 0.10077195 |
| S-446101 | diagnostic | 0.625 → 0.578125 | 3/64 | 0.0014152471 → 0.0014072386 | 0.00059545955 / 0.00013085775 |
| S-446101 | train | 0.8375 → 0.790625 | 11/320 | 0.0012290263 → 0.0012009812 | 0.00067106735 / 0.00013108721 |
| S-446102 | diagnostic | 0.90625 → 0.7890625 | 6/64 | 0.0013076356 → 0.0013369099 | 0.0016575121 / 0.00018357733 |
| S-446102 | train | 0.8 → 0.7875 | 14/320 | 0.0014581903 → 0.0015032667 | 0.0014794849 / 0.0001774134 |

## Epoch 100 versus initialization intervals

| Arm | Split | Cell | Epoch100−epoch0 regret | 95% empirical interval |
|---|---|---|---|---|
| S | train | all | -0.0296875 | [-0.0859375, 0.01796875] |
| S | train | assisted/MAP_SCREEN | 0 | [0.0, 0.0] |
| S | train | assisted/REWARDS | 0.05 | [-0.01875, 0.134375] |
| S | train | natural/MAP_SCREEN | -0.053125 | [-0.21875, 0.06882812499999887] |
| S | train | natural/REWARDS | -0.115625 | [-0.259375, -0.009375] |
| S | diagnostic | all | -0.08203125 | [-0.18359375, 0.0] |
| S | diagnostic | assisted/MAP_SCREEN | 0 | [0.0, 0.0] |
| S | diagnostic | assisted/REWARDS | -0.15625 | [-0.46875, 0.0] |
| S | diagnostic | natural/MAP_SCREEN | -0.09375 | [-0.28125, 0.0] |
| S | diagnostic | natural/REWARDS | -0.078125 | [-0.234375, 0.0] |
| D | train | all | -0.3421875 | [-0.50234375, -0.18671875] |
| D | train | assisted/MAP_SCREEN | -0.25 | [-0.565703125, 0.025] |
| D | train | assisted/REWARDS | -0.040625 | [-0.30625, 0.234375] |
| D | train | natural/MAP_SCREEN | -0.503125 | [-0.95625, -0.1125] |
| D | train | natural/REWARDS | -0.575 | [-0.878125, -0.296875] |
| D | diagnostic | all | 0.4453125 | [0.125, 0.83203125] |
| D | diagnostic | assisted/MAP_SCREEN | 0.625 | [0.0625, 1.28125] |
| D | diagnostic | assisted/REWARDS | 0.09375 | [-0.390625, 0.7347656249999943] |
| D | diagnostic | natural/MAP_SCREEN | 0.796875 | [-0.03125, 1.953125] |
| D | diagnostic | natural/REWARDS | 0.265625 | [-0.171875, 0.875] |

Training mean D−S: -0.3125; diagnostic minus training contrast gap: 0.83984375.

## Limits and disposition

Both 320 training and 64 diagnostic roots were historical #47 training data and retrospectively exposed. The balanced diagnostic partition is not a pristine test. Its intervals summarize these empirical noisy Q labels; they are not confidence intervals for normal A20 game outcomes. Separate seed robustness and all four source/family cells are reported without hiding natural MAP.

The future policy-test gate is unmet: Adam exhibits substantial candidate/margin movement, but diagnostic mean regret is higher than SGD and natural MAP mean is adverse. This is not an A20 harm classification or independent deployment estimate. No full policy test is automatically authorized; no optimizer/loss/encoder variants follow from the outcome.

Old hash encoder limitations, missing joint reward-choice context and native reward misc/exact upgrade count remain unresolved. This optimizer contrast cannot adjudicate a fully public structured non-combat agent. Return independently reviewed evidence to Planner; no core merge or promotion.

## Reproduction and integrity

Original #47 student.py, train.py (local train47.py), common.py, adapter.py (local adapter47.py), and config are byte-pinned. freeze.py seals owner metadata before reading Q. Source hashes, epoch0/100 states, all 400 training records, full per-root scores/Q/choices, optimizer manual-moment checks and CPU boundaries are retained for independent review. Run under Python 3.13 / Torch2.12.0+cpu, float64, one thread; no simulator import or constructor is invoked.
