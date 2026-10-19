# Issue 62: frozen one-step natural-root Q test

Study-only evaluation package for the question in [Issue #62](https://github.com/lsmfttb/spire-research/issues/62): did the two frozen Issue #60 residual actors shift probability mass toward higher terminal-floor values at naturally reached public MAP/REWARDS roots, followed by the frozen stochastic p0 policy?

## Result

**Classification: `LOCAL_DIRECTION_INCONCLUSIVE`.** The registered primary mean change was **+0.0061465266 floor**, with a 5,000-resample owner-cluster paired 95% bootstrap interval of **[-0.0039637459, +0.0178636515]**. The interval crosses zero, so this study does not establish positive or negative local direction. It does not promote either actor.

Block 1 mean: +0.0069494694 floor (95% CI [-0.0046211038, +0.0208683575]). Block 2 mean: +0.0053435838 floor (95% CI [-0.0063668379, +0.0181902019]). The owner-weighted mean within-root Kendall tau-a between residual score and estimated action Q was 0.0311921296. These are descriptive secondaries.

The fixed 96 owners yielded 35 MAP_SCREEN roots and 61 REWARDS roots, with no ineligible or censored owners. Every root's legal actions were evaluated under two paired future p0 streams: **364 action choices, 728 terminal continuations, all complete and root-verified**. The primary owner-level sign counts were 37 positive, 35 negative, and 24 ties under the registered 1e-12 tie rule.

## Execution and integrity

- Frozen Spire Research source: `1cce7997c648121742f0df0cfb95a2211aa3492c`.
- STSRL source: `7d172f7c24c9d29429b6946ba44f6b59760b0d96`.
- Native simulator source: `0990ef48d51cd48444a707839d65226d52c1d73e`.
- Native extension SHA-256: `68e9df911849f3f406d793c4419fddae7522f7e66c8a46ee6c40d3b6e417ea73`.
- Frozen block 1 checkpoint SHA-256: `463ac5426cb9a1a8a3f952652ca0d986616c2d744f2d7130b5a98109bc928303`.
- Frozen block 2 checkpoint SHA-256: `16da45d181e713d8d33fb97344ebada9720b1f19565725a87a93009f0153f52d`.
- Study runner SHA-256 after the source-snapshot guard repair: `56ee81a8d76650aaa00c0358ccc2e64af528c97cb6935720757eec6ffea6190b`.
- Resource amendment R1 chain validation: PASS. Runtime pins, APIs, deterministic-algorithm setting, and single-thread Torch setting are recorded in `evidence/manifest.json`.

Each branch replayed from standard start through the natural p0 prefix. At the source root and branch root, the runner checked the sampled ordinal, legal action identities, public history/input digests, and native checkpoint fingerprint before forcing the selected lawful action. The pilot parity witness is in `evidence/pilot.json`; every dispatched row in `evidence/branches.json` records `root_verified: true`.

One initial parallel-dispatch attempt completed its game work but failed collection because the frozen dispatcher's result-path check was rooted at the source directory. The study-local dispatcher setup was corrected to resolve the exact Issue #60 output paths; the attempt's **14.027164 CPU seconds remain included** in the ledger. The final dispatch then completed and verified the fixed 728-branch matrix.

Measured all-in study runner CPU was **1,716.231189 seconds**. Aggregate ledger wall time was **252.706897 seconds**; the successful 8-worker dispatch itself used 219.896903 wall seconds, about 3.31 terminal branches/second. Including the registered 300-second Builder static allowance and 1,200-second Reviewer reserve gives a 3,216.231-second envelope against the 12,000-second cap.

## Interpretation limits

This estimates one forced root action followed by the frozen stochastic p0 continuation. The two streams vary future public policy draws; they do not create independent hidden worlds for a given owner. Therefore `Qhat` is not a hidden-world conditional expectation at each individual public input. This is not repeated deployment of a residual actor, full-game efficacy, a claim about Battle decisions, or a Heart claim.

## Package map

- `run_issue62.py`: study-local source-prefix, replay, action intervention, parallel dispatch, accounting, and analysis.
- `test_issue62.py`: focused tests for the registered owner/root population, stream derivation, Kendall tau-a, and percentile interpolation.
- `evidence/manifest.json`: frozen source/runtime/model/code hashes and resource-amendment validation.
- `evidence/root-cohort.json`, `evidence/roots.jsonl`: fixed owner census, inclusion status, natural roots, legal choices, public inputs/history, and root fingerprints.
- `evidence/pilot.json`: source/branch parity pilot and frozen actor probabilities.
- `evidence/dispatch-plan.json`, `evidence/dispatch.json`, `evidence/branches.json`: admitted task plan, execution status, and complete owner/action/stream matrix with terminal floors.
- `evidence/analysis.json`: owner-level Q estimates, primary and secondary summaries, bootstrap interval, and classification.
- `evidence/cpu-ledger.jsonl`, `evidence/status.json`: durable execution-boundary and CPU accounting.

Focused verification:

```sh
python -m unittest discover -s studies/issue-62-natural-root-q -p test_issue62.py -v
```

No further simulator runs or training are part of this handoff. The independent Reviewer should check the actual forced-action path, full-history p0 continuation, terminal accounting and Q arithmetic, then reproduce one or two frozen keys and the registered bootstrap before routing to Planner.

## Review startup re-run

After the independent review found that the checkout-HEAD guard rejected the study commit, the same owner 6210001/action-0/stream-0 pilot was rerun from a clean checkout with the fixed source-tree guard. It passed with the same root parity and terminal floor. This startup/reproducibility rerun added no cohort or analysis rows; its CPU and wall time are included in the ledger above.
