# Issue 64: joint non-combat trajectory repair

This is a disposable, study-only controller and runner. It does not train a model, modify native battle search, or define a normal-public deployable policy. It uses the STSRL controlled-run executor, native checkpoints, the exact native Search-v2 API, and ExpertNonCombatDriver.

## Frozen treatment

B0 uses unchanged expert_non_combat_v1. B1 uses the same terminal-continuation quota to compare the current legal non-combat root actions with Expert continuation. B2 keeps an elite set of completed terminal trajectories; descendants inherit exact root and later action identities from an elite parent, restore the same native checkpoint, and replay with each identity revalidated against the live legal actions. A stale identity rejects the candidate. The runner executes only the selected legal root action on the actual run and searches again at the next actual eligible decision.

The cap is the first 8 actual meaningful non-combat points per owner. B1/B2 plan at the first 7; the eighth is reserved as a shared Expert continuation so a root decision can have a later in-cap choice for the required joint B2 mutation. Both search arms use exactly 32 completed full-terminal continuations per planning call. Battle decisions everywhere use unmodified StepSimulator.battle_search_v2 with 400 simulations, potions enabled, no callbacks, and highest_mean. The fixed source policy and battle search seeds are reset at each owner/continuation. Branch seeds are paired by call and candidate index between B1 and B2; diverged states are not represented as common-random-number coupled.

Terminal utility is the lexicographic pair (Heart victory, native terminal floor). The runner does not score incomplete runs, infer deaths from caps, or use HP/gold/reward shaping. All outcomes are privileged training-time Oracle evidence and cannot establish normal-public transfer.

## Pinned execution environment

The runner fails closed unless both source checkouts and the imported native extension match the pins in config.json. The verified native module is Python 3.14 at the pinned source commit; its SHA-256 is recorded in the config. It imports STSRL from the pinned source commit. Set PYTHONPATH to the native build directory and STSRL src directory before invoking the runner.

Example in WSL:

~~~bash
export PYTHONPATH=/home/lsmft/stsrl-spikes/build-issue59-native-0990-py314:/home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c/src
python3.14 studies/issue-64-joint-trajectory-repair/runner.py audit \
  --native-source /home/lsmft/stsrl-spikes/sts_lightspeed-issue59 \
  --stsrl-source /home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c

python3.14 studies/issue-64-joint-trajectory-repair/runner.py preflight \
  --output /mnt/d/DeadlycatCoding/issue64-artifacts-rerun3 \
  --native-source /home/lsmft/stsrl-spikes/sts_lightspeed-issue59 \
  --stsrl-source /home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c

python3.14 studies/issue-64-joint-trajectory-repair/run_production.py \
  --output /mnt/d/DeadlycatCoding/issue64-artifacts-rerun3 \
  --native-source /home/lsmft/stsrl-spikes/sts_lightspeed-issue59 \
  --stsrl-source /home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c
~~~

The output directory must remain outside the disposable worktree. Its named consumer is Issue 64 review, with deletion after reviewer/planner disposition or by 2027-01-31. Every owner and candidate writes coarse progress boundaries before/after execution; a new attempt is kept separate after interruption.

## Feasibility and stops

Preflight uses only owners 646901–646908. It runs B0/B1/B2 with the same frozen per-call caps, executes one alternate native legal root action from each natural MAP and REWARDS checkpoint, restores the checkpoint and verifies its fingerprint and legal actions, then compares two exact restored Expert continuations. It audits actual native legal-action execution and Search-v2 result mapping, records observed worker RSS, and samples full game tails. It estimates the complete 64-owner production cost using the maximum preflight owner cost. Production refuses to start unless this estimate fits the 165,000 Builder CPU-second ceiling while holding 15,000 CPU-seconds for independent review. The parent watchdog accumulates parent and worker process CPU across resumptions and asks active workers to stop at the ceiling. There is no wall-time cutoff and no quota adjustment after outcomes.

A preflight failure, insufficient treatment exposure, unsupported native action, non-deterministic checkpoint replay, or resource projection failure is a bounded failure. It produces no performance claim. The pinned source audit found that EVENT_SCREEN with MATCH_AND_KEEP can be a valid state while generic native getAllActionsInState returns no action. The runner accounts for this exception by requiring every actual and branch step to come from native legal_actions; if the pinned enumerator returns no action there, the run stops with an explicit unsupported-action failure. No local game-rule fallback or unreviewed native repair is provided.

## Evidence files

The runner writes its immutable config/code/native hashes, process CPU state, owner and arm ledgers, actual action traces, full branch decision traces, branch terminal endpoints, and preflight decision outside this repository. After all 64 production owner triplets finish, run python3.14 studies/issue-64-joint-trajectory-repair/summarize.py --output /mnt/d/DeadlycatCoding/issue64-artifacts-rerun3-rerun2 to emit paired owner-bootstrap 95% intervals for B2-B1 and B2-B0 endpoints. The summary fixes 20,000 percentile-bootstrap resamples and records its own hash. It reports, but does not adjudicate, the Act2+ noninferiority criterion because Issue 64 supplies no margin; a strong-Oracle claim stays closed pending Planner disposition. Branch traces omit raw hidden game snapshots; the pinned sources, config, owner seeds, and native binary hash define reproducible branch inputs. The source is study-only and should not be promoted into core.
