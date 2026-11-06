# Issue 85 actor input cache: offline performance and equivalence

> **Current source-bound result (2026-11-06):** use the v10 addendum below for the valid uncached-baseline comparison. The earlier v6 comparison recorded a `baseline` label but did not disable the runtime input cache; its equivalence measurements remain historical evidence, but its performance ratios are not an uncached-versus-cached estimate. The v9 rerun had the same control-path defect and is also invalid for that estimate. Both raw packages are retained unchanged. No game, replay, pilot, formal training, evaluation, or TEST was run for this correction.

## Source-bound uncached-versus-cached correction (v10)

**Disposition:** all five arms (B–F) are bitwise equivalent at both sequential optimizer updates, and the corrected baseline/candidate costs are source-bound. This is an offline engineering measurement on the retained 16-owner B–F Pilot training traces, not policy-efficacy evidence or a formal-run authorization.

- Candidate measurement source: `b5c8bc43e030449c10c30c088c4d6171dcce9033` (`codex/issue85-source-binding-20261106`).
- Baseline runner source: `5cd70c00d03f1104728efdc4b66c764592e5acf8`; lock SHA-256 `a25c25cd4144cbe362c7c2414600e9d71f4554627c9bb129407ef69ec8366404a`.
- Pinned Issue 84 source: `556128df66797bf979d36b56b6920ac88bab5b35`; lock SHA-256 `58ce736dbab1ac750e6e5771f644391610930ec4e9a87876f013bfd28838ef52`.
- Durable v10 evidence: `D:\DeadlyCatCoding\stsrl-data\issue85\artifacts\performance\20261106-actor-cache-equivalence-v10-cache-control` (21 files; 1,242,280,010 bytes). The comparison JSON SHA-256 is `482e1d59f61f37286ce2f308a438ec6d189150fdb5a55f00194858105f911877`.
- The source-binding validator independently verified both source locks, clean-commit/tree/blob identities, metric-to-snapshot digests and byte lengths, and the cache policy: baseline uncached for every arm; candidate cache absent for B and present for C–F.

| Arm | Baseline two-step wall (s) | Candidate two-step wall (s) | Measured two-step ratio | Conditional 64-step block speedup | Candidate cache (MiB, 16 owners) |
|---|---:|---:|---:|---:|---:|
| B | 3.899 | 2.703 | 1.44× | 1.50× | 0 |
| C | 408.046 | 202.715 | 2.01× | 53.60× | 23.51 |
| D | 426.625 | 214.635 | 1.99× | 41.71× | 83.54 |
| E | 7.638 | 5.347 | 1.43× | 5.36× | 23.00 |
| F | 20.070 | 12.134 | 1.65× | 10.37× | 27.75 |

The repeated cached step was 0.687 s for C, 1.864 s for D, 0.643 s for E, and 0.803 s for F; the first step includes cache construction (201.324, 211.035, 3.990, and 10.472 s, respectively). B bypasses the cache, so its measured change is a no-cache process/control observation and must not be attributed to caching. The two-step results cover only one short paired replay per arm.

Across the five arms with equal weight, the measured 16-owner conditional 64-step block totals are 27,931.816 s uncached and 767.123 s cached (candidate/baseline ratio 0.02746). Applying that ratio only to the previous forecast's actor-update component gives 3.697 h rather than 134.603 h; adding the unchanged 16.297 h formal game-group projection yields a conditional 19.993 h formal core, or 29.061 h with the prior 9.068 h Pilot clock. This is a partial conditional projection: the 32-owner batch and 64-step behavior were not measured, and formal games, retry/restart variance, setup/queue time, bootstrap analysis, and human review remain outside the estimate. It is neither an all-in forecast nor a time cap.

Comparison used exact equality (no tolerance) for inputs, encoded features, distributions, owner losses, total loss, gradients before/after clipping, gradient norm, model state, and optimizer state. Each arm had zero mismatches at both updates. The comparator was run with `--require-source-binding` and exited successfully. Targeted provenance tests passed (8 tests); Python compilation and `git diff --check` passed. No evidence was deleted or overwritten; no disk space was reclaimed.

The sections below preserve the original v6 report and must be read as historical measurements, subject to the control-path correction above.

**Candidate source commit:** `48d5d861cf944619e61be7bd54463045b707cb51` on `codex/issue85-actor-cache-optimization-20261106`.
**Source baseline:** reviewed Issue 85 runner `5cd70c00d03f1104728efdc4b66c764592e5acf8`; `source-lock.json` SHA-256 `a25c25cd4144cbe362c7c241460e9d71f4554627c9bb129407ef69ec8366404a`.
**Pinned Issue 84 gradient source:** `556128df66797bf979d36b56b6920ac88bab5b35`; lock SHA-256 `58ce736dbab1ac750e6e5771f644391610930ec4e9a87876f013bfd28838ef52`.
**Durable raw evidence:** `D:\DeadlyCatCoding\stsrl-data\issue85\artifacts\performance\20261106-actor-cache-equivalence-v6` (23 files, 1,247,339,123 bytes / 1,189.56 MiB).

## Scope and method

This is an engineering measurement on the already completed, receipt-verified nonformal Pilot TRAIN traces. It ran no native game, replay, new owner, formal training, evaluation, or TEST. Five arms (B–F) each used the same saved 16-owner decisions, actor checkpoint, and AdamW state for two consecutive complete updates in baseline and candidate processes. The pinned runtime was Python 3.13.13, PyTorch 2.12.0+cpu, NumPy 2.4.6, one PyTorch CPU thread. Arms were measured serially.

The candidate caches only source-verified, parameter-independent candidate feature matrices and residual encoder tensors for the current ordered owner/decision block. It retains legal occurrence order and rechecks each decision fingerprint on cache use. Residual backbone/scorer forward, log probabilities, entropy, loss aggregation, backward, clipping, and AdamW updates remain per step. B bypasses residual caching. Cache tensors carry no autograd history and no weights, logits, probabilities, loss, gradients, optimizer state, or post-update activations are cached.

The first timed update includes block cache construction; the second is the repeated-update observation. There was no separate warmup update. The timing records split input/encoder construction, backbone and residual forward, backward, clipping, optimizer, repeated block validation, and wrapper adaptation. Raw per-decision candidate and token/cross-product distributions are retained in each metrics file.

## Pilot decision shapes

| Arm | Decisions | Legal candidates min / median / p90 / max | Context tokens min / median / p90 / max | Candidate tokens median | Candidate×context tokens median |
|---|---:|---:|---:|---:|---:|
| B | 589 | 2 / 3 / 6 / 21 | — | — | — |
| C | 569 | 2 / 3 / 7 / 17 | 468 / 662 / 873 / 1,141 | 48 | 29,624 |
| D | 629 | 2 / 3 / 6 / 19 | 501 / 681 / 878 / 1,057 | 42 | 27,025 |
| E | 541 | 2 / 4 / 7 / 19 | 164 / 208 / 263 / 292 | 33 | 6,698 |
| F | 686 | 2 / 3 / 6 / 22 | 511 / 672 / 890 / 1,202 | 44 | 28,322 |

## Measured timing

Times are per-process active wall and CPU seconds summed across two updates. “64-step block” is a conditional projection from one measured cache-build step plus 63 repeats; it is not a 64-step benchmark.

| Arm | Baseline 2-step wall / CPU | Candidate 2-step wall / CPU | 2-step speedup | Baseline → candidate 64-step block | Block speedup | Cached input tensors per 16 owners |
|---|---:|---:|---:|---:|---:|---:|
| B | 2.772 / 2.607 | 2.889 / 2.826 | 0.96× | 85.523 → 92.200 s | 0.93× | 0 MiB |
| C | 395.632 / 395.424 | 198.953 / 198.818 | 1.99× | 12,757.099 → 239.405 s | 53.29× | 23.51 MiB |
| D | 417.436 / 417.256 | 209.757 / 209.577 | 1.99× | 13,467.528 → 326.903 s | 41.20× | 83.54 MiB |
| E | 7.575 / 7.463 | 5.477 / 5.402 | 1.38× | 237.965 → 44.046 s | 5.40× | 23.00 MiB |
| F | 20.490 / 20.375 | 11.851 / 11.776 | 1.73× | 657.413 → 60.263 s | 10.91× | 27.75 MiB |

The two-step stage totals show where time moved (seconds, baseline → candidate):

| Arm | Residual encoder construction | Legacy feature tensorization | Backbone + residual forwards | Backward | Clip + optimizer |
|---|---:|---:|---:|---:|---:|
| B | 0.000 → 0.000 | 0.017 → 0.020 | 0.640 → 0.691 | 1.591 → 1.648 | 0.046 → 0.042 |
| C | 392.004 → 195.516 | 0.039 → 0.024 | 0.799 → 0.263 | 0.578 → 0.573 | 0.021 → 0.022 |
| D | 411.143 → 203.330 | 0.042 → 0.027 | 1.714 → 0.833 | 1.826 → 1.976 | 0.041 → 0.077 |
| E | 4.189 → 2.184 | 0.029 → 0.019 | 0.625 → 0.256 | 0.575 → 0.546 | 0.020 → 0.020 |
| F | 15.967 → 7.701 | 0.042 → 0.025 | 0.915 → 0.299 | 0.752 → 0.737 | 0.024 → 0.019 |

C and D's original flat contextual encoder dominates their repeated update time; caching its deterministic result removes the repeated construction. E and F also improve after the first cache build. B is unchanged and shows no material benefit.

Per two-update pair, pinned block validation stayed between 0.0002 and 0.0003 s and owner-outcome adaptation between 0.0025 and 0.0036 s; these checks remain enabled. Cache input fingerprints are included in the complete owner-loss/loss-forward timers, but are not isolated in a separate sub-timer. The end-to-end step wall/CPU figures include them.

## Memory

Cache tensor bytes are an isolated exact count. For the registered 32-owner batch, a simple linear count would be about 2× the measured Pilot block: C 47.01 MiB, D 167.08 MiB, E 46.00 MiB, F 55.51 MiB; B remains zero. This is a storage projection, not a measured 32-owner peak.

Observed process RSS after the second update (MiB, baseline → candidate) was B 624.1 → 624.1, C 471.0 → 479.0, D 723.6 → 790.4, E 444.3 → 462.9, and F 475.4 → 497.9. RSS includes the loaded examples, PyTorch allocator, gradients, and profiler-retained CPU snapshots; it does not isolate the cache. The cache-tensor count above is the direct added-memory measure.

## Bitwise equivalence

Final machine comparison: `equivalence-comparison-v3.json`, SHA-256 `88bde8aa1c747971584a3244c7db6757384ce2473f72ae78d85d010b045f8d4f`. It validates the runner and both source locks, report-to-snapshot hashes/sizes, and identical pilot input receipts, checkpoint and optimizer hashes, start model digest, owner count, and decision count for every paired arm.

All five arms passed **bitwise exact** comparison at both sequential updates, with no tolerance: candidate input matrices, encoded residual features, logits/log probabilities/entropy, per-owner loss/diagnostics, total loss, pre/post-clip gradients and gradient norm, model parameter state, and optimizer state. D/E/F step digests also match. The original B/C baseline snapshots do not contain per-step model-digest fields, so the comparison checks their complete model-state tensors instead.

The first C candidate harness recorded each cached tensor once at cache construction and a second time at its first use. The raw C report and snapshot are preserved; the comparator verified the two ordered copies are bitwise identical and compared one copy per decision. The harness was corrected before D/E/F/B runs. An initial comparator report (`equivalence-comparison.json`) was a false negative because it required omitted B/C digest fields; it is retained unchanged. The hash-validated v3 result is the final comparison.

## Conditional formal-cost scenario

The earlier accepted conditional projection assigned **134.603 h** to 40 blocks of actor-update work and **16.297 h** to formal game groups, totaling **150.900 h** for the formal core. Applying the measured equal-arm 64-step ratio (candidate 762.818 s / baseline 27,205.528 s = 0.028039) to the old actor-update component gives:

- Conditional actor-update component: **3.774 h** instead of 134.603 h.
- Unchanged formal game groups: **16.297 h**.
- Conditional formal core: **20.071 h** instead of 150.900 h.
- Adding the existing 9.068 h Pilot clock gives **29.139 h** for Pilot plus those projected core components.

This remains a conditional scenario, not an all-in estimate, guaranteed runtime, cap, formal-start authorization, or efficacy result. It carries forward the previous linear scaling assumption from 16-owner Pilot updates to the registered 32-owner batch and assumes the candidate's first-step-plus-repeat ratio persists over each 64-step block. Only two updates were measured per arm; 32-owner throughput and longer-run allocator/thermal behavior were not measured. Formal retries/long tails, build/restore/queue overhead, validation/test bootstrap analysis, and review/Planner time remain outside the estimate. The registered cohorts, study contract and resource policy are unchanged; no formal process was started.

## Verification and retained artifacts

The pinned environment compiled all five study scripts and passed the complete Issue 85 test suite: **86 passed, 57 subtests passed**. `git diff --check` passed before commit. Source and test artifacts are committed in `48d5d861cf944619e61be7bd54463045b707cb51`; the comparison and all raw paired snapshots/metrics are retained under the durable evidence path above for the independent Reviewer, then Planner feasibility follow-up. No evidence was deleted or overwritten.
