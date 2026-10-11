# Stage 1 result — pending independent review

**STUDY_ONLY. Stage 2 has not been authorized or executed.**

Producer: `9d5deb3bbd87b98a2531300d1a262fe4e2777f2f`, admitted by
[Planner grant6111078759](https://github.com/lsmfttb/spire-research/issues/43#issuecomment-6111078759).
Execution ended normally with exit0, one authentic attempt, no failed keys or
retries. Executable Python/config/overlay were unchanged during execution.
Compact evidence is in `stage1-result.json`; authoritative raw data remain at
`/home/lsmft/.cache/issue43-formal-lineage`.

| Split | Ascending scanned seeds | MAP / REWARDS roots | Distinct owners | Legal actions | Terminal continuation keys |
|---|---|---:|---:|---:|---:|
| Train | 430001–430160 | 96 / 160 | 160 | 1146 | 2292 |
| Validation | 431001–431040 | 24 / 40 | 40 | 298 | 596 |
| Heldout | 432001–432040 | 24 / 40 | 40 | 287 | 1148 |

All 4036 exact original legal-action/continuation keys are complete. Both arms
used the same training returns and fixed two optimization seeds with 100 updates
per seed. Validation selected visible seed430082/update70 and masked
seed430081/update66. Model freeze preceded heldout opening. Both have2081
parameters and the prospectively fixed, unfitted normalizer.

- Visible checkpoint: `a27d04aebaddac7137b0bab3340319258015a17311cf681b306feaf7f5b8f27e`.
- Masked checkpoint: `54388f07767b8125d528fca157c35f1e7417d8c2f08e6f3775a092d96fbf57f3`.
- Normalizer hash: `5f7b068012448ea28b0517c16165f70654643a5c34994d037dd49d356cb1ea2a`.

## Heldout observations

These are local realized-world curriculum-return metrics under privileged
Search-v2@400 Battle and frozen Expert continuation. They do not estimate
independent complete-run improvement.

| Mean over 64 heldout roots | Visible | Masked |
|---|---:|---:|
| All-action Q MSE | 10.41299 | 8.89555 |
| Chosen Q | 9.59766 | 10.12891 |
| Regret | 2.21484 | 1.68359 |
| Pairwise ranking accuracy | 0.44455 | 0.53446 |
| Optimal-action fraction | 0.15625 | 0.29688 |

The visible arm's descriptive means are worse on these local metrics. This
table does not claim a paired treatment-effect confidence interval. Full
prospective per-family metrics and 10000-replicate source-owner-clustered
intervals are retained in the result JSON and original heldout report. Model
choice disagreement is75%, owner-clustered95% interval[64.52%,85.25%].

All4036 continuation episodes ended in native PLAYER_LOSS;3958 reached Act1
and78 reached Act2; terminal floors range4–33; zero Heart wins. Genuine terminal
losses are distinct from invalid/timeout execution, of which none was recorded.
The early curriculum/source distribution and privileged teacher limit what
these observations can support. No model revision, retraining or seed sweep
followed heldout inspection. No scientific gain claim is emitted.

## Resources, verification and retention

Sealed execution journal:102435.934CPU /14535.085wall seconds, attempt1;
peak8workers, maximum recorded job RSS2167624KiB. Journal chain has257219rows,
no abandoned/failed leases, and no active leases at close. Hard caps remain
180000 aggregateCPU /27000wall, RSS8GiB/job and system available>=4GiB.

Bounded read-only Builder audit rechecked deterministic source selection,
disjoint owners/family quotas, each exact branch terminal and target table,
completion hashes, fixed optimization/validation selection, model/report
hashes and the full resource chain. It did not rerun the simulator or training.
Audit measured26.844CPU /28.652wall;200CPU/300wall handoff preparation reserve
includes external control, monitoring, packaging and supplementary inspection.
Stage1 inclusive conservative upper:102662.779CPU /14863.737wall. Stage0 retains
its separate accepted7508.41CPU /3585.59wall upper; no cap reset.

Raw manifest:5977files,2432562296bytes; SHA256
`8b1aa5d3f4f750c62c982d0e6545bd1c87dcff306ab77db28eff1bf8a81f0b7a`.
Audit bundle manifest SHA256:
`232c2e29de96717f02c33792d67461deebe6a147f1e762de786445ecc0756270`.
Resource journal SHA256:
`047b2dc929d570501f8218ee368f25818cdad963e26e4ba2112b9b0a8f3b6191`.

Raw `audit/verify.py` documents the Builder check; `audit/summary.json`,
`audit/manifest.json`, launch/exit records and `audit/audit-manifest.json` retain
the evidence. Named consumers:Issue43 independent Reviewer and Planner;
review/delete2026-10-26. Earlier Stage0 evidence obligations remain2026-10-18.
Reviewers should charge new verification separately against the same Stage1
caps and preserve the sealed execution history. Later execution must retain
the authentic producer/identity and directory; the evidence commit does not
replace the authorized producer. No merge or core/model promotion is proposed.
