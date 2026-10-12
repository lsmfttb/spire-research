# Stage 2 — complete, pending independent review

**STUDY_ONLY. No merge, core promotion or policy deployment proposed.**

The unchanged executable producer is `9d5deb3bbd87b98a2531300d1a262fe4e2777f2f`.
[Planner's Stage2 grant](https://github.com/lsmfttb/spire-research/issues/43#issuecomment-6115190633)
authorized the original lineage, frozen Stage1 models and exactly 128 original
seeds 433001–433128 across visible, masked and expert_source. All 384 runs
completed genuine native PLAYER_LOSS with HP zero; no invalid run was classified
as loss. No retraining, tuning, additional simulator runs or source changes occurred.

## Prespecified fresh results

| Mean terminal-floor difference | Estimate | 95% paired seed bootstrap interval |
|---|---:|---:|
| Visible minus masked | 0.984375 | [0.1328125, 1.84375] |
| Visible minus Expert source | 0.578125 | [-0.34375, 1.4609375] |

Both use 10,000 replicates, fixed seeds 433091/433092 respectively. The primary
positive interval meets `PUBLIC_SEMANTICS_LEARNING_GAIN_ESTABLISHED` in the
registered teacher-assisted regime, subject to independent review. The stronger
`NONCOMBAT_TEACHER_ASSISTED_RUN_IMPROVEMENT_ESTABLISHED` gate is not met.

| Arm | Learned MAP | Learned REWARDS | Act2 reach | Act3/4 reach | Heart wins |
|---|---:|---:|---:|---:|---:|
| Visible | 1350 | 2704 | 3/128 | 0 | 0 |
| Masked | 1261 | 1367 | 1/128 | 0 | 0 |
| Expert source | — | — | 3/128 | 0 | 0 |

Both students exceed 64 learned choices in each supported family, with zero
supported-family fallback. OTHER screens use the explicit frozen Expert fallback.
At genuinely identical public states and repeated-state occurrence within seed,
MAP actions differ in 48/92 matches and REWARDS in 80/91. No comparison after
state divergence is interpreted as matched action disagreement.

The source curriculum consists of early floor0/1 checkpoints. Fresh descriptor
total variation versus training source is visible0.07768, masked0.07156 and
Expert0.06880. Full option/room/ID frequency counts and unseen descriptors are
in `stage2-result.json`, together with every paired seed and 384 compact run
records including terminal classification, reach, steps, Battle calls and costs.
These descriptor differences are descriptive; they do not identify a causal
mechanism or eliminate the curriculum/distribution limitation.

Battle uses privileged full-state Search-v2@400 and no Battle potions in every
arm. These results do not establish full normal-public noSL performance or
Heart win rate. The primary conclusion is confined to the specified students,
seeds, frozen fallback and teacher-assisted information regime.

## Evidence and resource accounting

Authoritative raw data: `/home/lsmft/.cache/issue43-formal-lineage/stage2`.
Builder's bounded audit: `/home/lsmft/.cache/issue43-formal-lineage/audit-stage2`.
The audit checks exact run keys, genuine terminal state, immutable completion
hashes, public hashes, frozen model decisions, recomputed report/bootstrap and
the full resource hash chain. Its manifest covers Stage2 and referenced Stage1
identity/model/target artifacts; the earlier Stage1 audit is preserved.
Exact hashes and measured audit costs are recorded in `stage2-result.json`.
Raw manifest SHA256: `8d83450c04b96ddd2539f160e6f572d9b4cb282183e2158cf2656a90c64941d9`
(2320 files, 3562127142 bytes including referenced Stage1 artifacts).
Audit bundle manifest SHA256: `500960c5cd1e2d0f7aeef693d13baa19b80977bb81f7e4809d91dabc9bd01647`.
All385 job completion checksums and384 native HP-zero terminal records pass.

Execution ended exit0 in one attempt; sealed resource charge8297.610 CPU /
1154.688 wall seconds, no active leases. Audit costs are added separately.
A conservative600CPU/600wall external reserve covers inspection, failed audit
setup/check attempts, monitoring, packaging, Git operations and cleanup.
Stage2 limits36000CPU/12000wall, RSS8GiB/job and available memory>=4GiB remain
unchanged. Prior inclusive Stage1 upper102852.33CPU/15560.79wall and separate
Stage0 upper7508.41CPU/3585.59wall remain unchanged; no ledger is reset.
Measured Builder audit97.899CPU/101.247wall plus supplementary seal3.521CPU/
3.954wall and the reserve give Stage2 inclusive upper8999.030CPU/1859.890wall.
Initial audit import failed without backend PYTHONPATH; correcting the audit
environment resolved it. This preparation failure is included in the reserve.

No simulator replay or training is used for this Builder audit. Independent
Reviewer should charge further verification against the same Stage2 limits and
validate the scientific claim separately. The evidence commit does not replace
the executable producer. Named consumers: Issue43 independent Reviewer and
Planner; review/delete2026-10-26, with Stage0 obligations2026-10-18 preserved.
