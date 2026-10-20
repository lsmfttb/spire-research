# Issue 66 all-screen self-play

This is an Issue-local, study-only source preflight. It does not train a policy,
run owners, score outcomes, or authorize a normal-public policy. The source
identity is pinned to native `sts_lightspeed` `stsrl/main` at
`0990ef48d51cd48444a707839d65226d52c1d73e`, its verified Python 3.14.4 binary,
and STSRL `7d172f7c24c9d29429b6946ba44f6b59760b0d96`. The working repository base
is the reviewed Issue 65 receipt-identity commit `30efe12bcedb65a432f1a57756a930baed90c5d5`.

## Bounded source failure

The pinned native `StepSimulator.snapshot()` returns current resources and
battle state, but no current noncombat choice content. Its
`publicChoiceActionSnapshot()` exposes an action scope/kind and `idx1`/`idx2`/
`idx3`; those values are only native action parameters. The native
`publicChoiceScreenPayload()` supplies candidate-bound semantic descriptions
for MAP and REWARDS choices, while its source explicitly marks EVENT, BOSS_RELIC,
CARD_SELECT, TREASURE, REST, and SHOP choice content unsupported. NEOW has no
choice case in this capability. The audited source locations and file digest
are recorded by `preflight-decision.json`.

That pinned API cannot provide semantic action-conditioned features on all
required noncombat screens. Using indices or action IDs as substitutes would
violate the registered model contract; adding Python game rules or an Expert
fallback would change the registered intervention. Therefore the preflight
records `SCIENTIFIC_INCOMPLETE` before consuming any training or evaluation
owner seed. The registered 1024 training owners and 96 five-arm evaluation
owners were not started. This is a bounded source blocker, not a negative or
null experiment result. A complete attempt needs an independently reviewed
native semantic-choice capability and a new source pin before a new preflight.

`runner.py preflight` validates the repository, STSRL, Python, loaded native
binary hashes, performs the source capability audit and writes immutable
coarse status outside the worktree. `runner.py production` rejects every
preflight that does not explicitly authorize execution. The source audit never
uses an action ID/index as a model feature and makes no game-mechanics change.

Example in WSL:

```bash
export PYTHONPATH=/home/lsmft/stsrl-spikes/build-issue59-native-0990-py314:/home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c/src
python3.14 studies/issue-66-all-screen-selfplay/runner.py preflight \
  --output /mnt/d/DeadlycatCoding/issue66-artifacts \
  --native-source /home/lsmft/stsrl-spikes/sts_lightspeed-issue59 \
  --stsrl-source /home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c
```

Run the focused pure-Python checks with:

```bash
python3.14 -m unittest discover -s studies/issue-66-all-screen-selfplay -p 'test_issue66.py'
```

The external artifact folder's named consumer is Issue 66 independent Reviewer
and Planner review; delete after disposition or by 2027-01-31. Do not reuse the
preflight output folder with another source identity.
