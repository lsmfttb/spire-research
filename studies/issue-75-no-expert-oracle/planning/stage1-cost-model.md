# Issue 75 Stage 1 cost model (planning only)

**Status:** PLANNING_ONLY_REQUIRES_SEPARATE_PLANNER_ACTIVATION<br>
**Disposition:** STUDY_ONLY<br>
**Stage 0 evidence snapshot:** 7f7538a706710626740cdb635d3711e69eac900f<br>
**Measured source / lock:** c77d64243320639bf156330760d511d2a13eee85 / b3ce3d33c27ec392f6d88151bce3c86a8fdce8c9dc0152d417069441f7140d38

This receipt-derived estimate does not activate Stage 1. Formal training/evaluation stay disabled; no formal seed table is added.

## Registered workload

- Training: 512 owners; 4 blocks × 128; 256 AdamW updates.
- Held out: 96 owners × 5 arms = 480 scenarios.
- Complete-game path analogs: 992; searches: 3,392 (2,720 J, 672 R).
- Search hard cap: 108,544 CPU-s, from 3,392 calls × 32 CPU-s.

## Measured Stage 0 profiles

| Component | n | Mean CPU-s | Sample SD | Median | p95 | Max |
|---|---:|---:|---:|---:|---:|---:|
| Hybrid game-path proxy | 8 | 1.47 | 0.36 | 1.43 | 1.97 | 1.97 |
| Root worker residual | 8 | 9.69 | 0.16 | 9.65 | 10.04 | 10.04 |
| R search invocation | 8 | 15.62 | 5.57 | 16.26 | 24.34 | 24.34 |
| J search invocation | 8 | 15.67 | 6.01 | 16.39 | 24.56 | 24.56 |
| R individual attempt | 128 | 0.97 | 0.64 | 0.79 | 2.22 | 4.87 |
| J individual attempt | 128 | 0.98 | 0.62 | 0.82 | 2.03 | 3.69 |

The game-path proxy is historical-source prefix replay plus the Stage 0 pi0 terminal continuation from the fixed checkpoint. It is not a standard-start Stage 1 game.

Existing Stage 0 cumulative ledger from handoff #6402937706: 14,213.84/50,000 Builder CPU-s and 4,271.68/30,000 native CPU-s; Reviewer reserve 10,000; Stage 1 reserve 0. This proposal is a separate ledger and carries no credit forward.

## Stage 1 projection

| Builder cost item | CPU-s |
|---|---:|
| Expected search (observed means × calls) | 53,121.92 |
| Search maximum (registered caps) | 108,544.00 |
| Expected hybrid game-path proxy | 1,459.72 |
| Expected worker residual extrapolation | 9,610.67 |
| Controller/training allocation guard | 15,243.51 |
| Modeled expected Builder CPU | **79,435.82** |
| Root-bootstrap modeled total p95 | 90,195.22 |
| Root-bootstrap total central 95% interval | 66,794.54–92,061.81 |
| Root-bootstrap non-search p95 | 26,520.94 |

Tail method: 20000 resamples of eight paired root profiles with replacement; nearest-rank quantiles. Analysis RNG seed 750102026 is not a game seed. Quantiles are not predictive guarantees.

## Proposed budget for a separate Planner decision

- Search hard cap + non-search bootstrap p95: 135,064.94 CPU-s.
- Explicit 25% engineering margin: 33,766.23 CPU-s (not a measurement).
- Proposed Builder hard ceiling, rounded up to 10,000: **170,000.00 CPU-s**.
- Protected Reviewer reserve: **10,000.00 CPU-s**.
- Proposed all-in hard ceiling: **180,000.00 CPU-s**.

These Stage 1 ceilings are proposals only, not reserved or authorized. Separate activation must register the budget and implement an aggregate Builder watchdog that stops safely at the cap. Reviewer reserve is protected and is not included in expected experiment spend.

## Limitations

- Only eight historical Issue 73 checkpoint roots were measured; they are not standard-start pi0 or Stage 1 policy games. Prefix plus continuation is a hybrid CPU proxy, not a Stage 1 outcome.
- Eight root clusters are too few for predictive tail guarantees. Bootstrap quantiles describe resampling uncertainty over those observed roots only.
- Search means use 16 completed Stage 0 continuations per invocation. Stage 1 hard caps, not these means, define the maximum search cost.
- Aggregate Stage 0 controller CPU includes one batch-8 diagnostic update but is not an isolated optimizer timer. Its scaled term is a planning guard, not a proven bound.
- Model excludes any unregistered dependency rebuild, portability repair, checkpoint recovery, source drift or cost beyond the explicit margin. Any activation needs a project-level watchdog and safe stop at the aggregate cap.
- This report is not a scientific outcome or Stage 1 authorization. No formal Stage 1 seeds, games, training or simulator runs are performed by this analysis.

Rebuild: python -B studies/issue-75-no-expert-oracle/planning/build_stage1_cost_model.py. The script verifies the source lock, all retained JSON receipts, runtime logs and formal-seed-disabled state before producing this report.
