# Issue48 — frozen structured public package

STUDY_ONLY. Base origin/main 9525d41. Exact adapter.py and student.py reused
from reviewed Issue47 source 41484c97; frozen A remains its original checkpoint.
No public API or permanent core addition. Native and STSRL pins in config.json.

The sole S package is fixed before training/validation inspection: 8-dimensional
learned typed tokens, mean scalar-sibling entity pooling, shared tanh8 entity
layer, separate state/candidate/destination/incoming/outgoing map pools, then a
joint 42-input tanh32 scorer and scalar output. Actual public map coordinates
resolve candidate destinations and incoming/outgoing neighbors. Whole graph
nodes/edges are also retained in the state pool. Dictionary scalar siblings
retain identity/upgrade/resource association; ancestor list positions and list
lengths remain numeric tokens. No history truncation. All public_state fields
are represented, including availability, source descriptions and public history.
Selected option semantic fields remain intact. Same visible exclusions as frozen
hash: action executor indices/label/scope and top-level adapter metadata outside
public_state. No source owner, assistance, RNG, native snapshot or future outcome
enters either model. The frozen comparator also excludes other legal options'
semantics; both policies receive their own option, full public state and count.

Train-only vocabulary: at most 3,000 tokens, frequency then lexical ties;
lexical IDs, OOV0. Numeric signed log1p(abs(x))/10, including list positions;
identity/upgrade categorical tokens shared across deck/reward cards, relics and
potions. Every scalar has a token even when unknown; unseen/capped identities
map to OOV. 8V + 1,481 parameters, at most 25,489. Empty neighbor pools zero.
All weights learned; no game rules, rankings, tactical route/reward heuristics.

Two seeds 446101/446102, float64 CPU one thread, full-batch Adam lr .001 with
default betas/epsilon, no weight decay, exactly 200 epochs each. Embeddings
uniform[-.05,.05], PyTorch Linear default initialization. Same Q/60 labels and
root-equal option-balanced MSE. Lowest all64 validation empirical Q-regret,
earliest epoch, smaller seed; first ordinal score ties. Selected S is written
before heldout opens. No alternative architecture or optimization sweep.

Minimal byte-identical public table copy rechecks original #46 table hashes,
#47 derivative seal and exact A file; old stores never written. New store
/home/lsmft/.cache/issue48-structured-public, consumer Issue48 independent
Reviewer/Planner, review/delete date 2026-11-14. Original schedules unchanged.

Fresh 481001..481128 S/A/Expert; same seeded OTHER Expert and unguided privileged
Search-v2@400 legal Battle potions ON. No assistance. 384 actual terminal games.
Primary S-A, 5000 paired bootstrap replicates RNG481999, linear95% percentile CI.
Gain/harm gates require all valid, >=64 MAP and REWARDS for both learners,
zero supported silent fallback. Expert contrasts descriptive only. Package
changes representation/network/capacity/optimization together; causal encoder
isolation, independent hidden-future return labels, normal-public Battle and
Heart competence are not established. No gain ends this old-Q first-Act branch.

run.py owns all-in 40,000 CPU ledger: 60 administrative CPU seconds for setup,
actual wait4 worker CPU, live /proc CPU, parent CPU; 39,800 stop reserve. Dynamic
evaluation worker slots reserve 8GiB per worker plus 4GiB host; maximum8.
Training one worker (deterministic CPU tensor optimization), derivation/audit/
report/seal one worker (artifact aggregation). Crash state execution.json records
stage/PID/CPU and complete seed-arm keys. No truncation as death or seed replacement.
seal.py hashes final artifacts and preserves producers. Reviewer independently
checks science and exact source/evidence before Planner disposition; no merge.
