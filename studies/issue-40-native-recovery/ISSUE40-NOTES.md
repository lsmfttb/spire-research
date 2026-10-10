# Issue 40 native recovery and continuation smoke

## Terminal

SMOKE_PASS_STAGE1_AWAITING_PLANNER. The recovered build passed the exact Stage-0 audit before smoke execution. The smoke ran only the predeclared eight held-out roots and the unique frozen model/Expert root-action pairs. Proposed code disposition: STUDY_ONLY.

The Stage-0 files in this directory remain an immutable snapshot. Their four entries still match output-manifest.json; the blocked Issue 39 snapshot was not modified. Smoke results and run state are separate files.

## Exact inputs

- STSRL code: 95abc8b1afe7d84e567aa70fb7225eba09fdff25; approved spec: afffcdda5cebfe47a2cfa1624911d916191bab76.
- Native source: lsmfttb/sts_lightspeed@20a6c2b3a9cea817c988178b814f083ff889853f; json@0b345b20c888f7dc8888485768e4bf9a6be29de0; pybind11@d03662f0984f652b60e7ddce53d3868002275197.
- Loaded extension: /home/lsmft/stsrl-spikes/sts_lightspeed-t088-20a6/build-py/slaythespire.cpython-313-x86_64-linux-gnu.so, SHA-256 108416736aa83973e963cb06e02411d0eb389109003c62e414631053e35ef062.
- T075 selected source SHA-256: 94857d0e310f34cdd2780920ec81f9dc60e179c94244b9e231952a43a5f4e8b8.
- T089 checkpoint seed 893002 SHA-256: a3a136f729b5cee69815a0b128411eec49a9b4f7695c6aae5a3796345fbd972e.
- T089 target table SHA-256: b8551454f764d47b43d068d507df783226f4b2651ea74fe82bec7fd4b0b8de11.

native-recovery.json records the exact successful configure/build command, tool versions, pinned submodule status, extension path/hash, and bounded recovery limitations. The STSRL remote fetch attempt failed with the recorded GnuTLS error; the accepted code commit was verified in the retained local Git object database and checked out detached. The exact submodule commits were restored from retained local caches only after matching their required object identities.

## Smoke

- Preselected original-order roots: indices 64–65 MAP_SCREEN, 144–145 REST_ROOM, 224–225 REWARDS, and 304–305 TREASURE_ROOM.
- Root model and Expert actions were recomputed from frozen public contexts and matched the fixed indices and identities in smoke-plan.json.
- Five roots had coincident actions; 11 unique state/action pairs × four continuation seeds produced 44 logged, terminal continuations. Seeds were 892201–892204.
- Every logged continuation reached a native terminal; the maximum was 103 steps including the forced root action. A 500-step truncation was never treated as a loss.
- Non-combat routing recorded 575 learned-policy decisions on supported public contexts and 122 explicit Expert fallback decisions on unsupported contexts. Native battle Search-v2 used the pinned Search-v2@400, highest_mean, no-potions configuration.
- Per-seed smoke Q-floor values and the exact historical Expert Q-floor reference are retained row by row. The smoke did not aggregate a hypothesis test or change root/state/action/seed selection. All 44 raw terminal outcomes happened to be PLAYER_LOSS; this is recorded as smoke output only.

One first continuation reached a terminal, then the study runner's cost-accounting dictionary raised KeyError before writing a result row. That exact predeclared branch was rerun once after fixing the ordinary logging bug. The first attempt is retained under smoke-attempt-1-*; it counts toward the issue's 64-attempt ceiling. A separate runner invocation was stopped during preflight after a supervisor-variable bug; it started no continuation and is retained under smoke-attempt-2-*. Its 10-second wall/5-second CPU allowance is included conservatively in smoke-budget.json. An earlier native API-name preflight check also stopped before constructing the simulator; it was corrected to the exact StepSimulator API.

## Resource and next decision

Before simulator work, the study reserved 1,800 seconds wall time, 1,800 seconds process CPU, 8,192 MiB peak RSS, 4,096 MiB minimum available-memory floor, concurrency 1, and at most 64 continuation attempts. The cumulative ledger records 776.518 seconds wall time, 718.17 seconds process CPU, 1,205.41 MiB runner peak RSS, and a minimum 20,766,265,344 bytes available memory during the supervised successful invocation. Total continuation attempts were 45 including the single harness retry, below the 64 ceiling. The earlier short preflight invocation is listed separately and conservatively accounted for.

Stage 1, the 64-root × four-seed follow-up, training, policy promotion, and simulator source changes were not performed. Independent Reviewer confirmation is next; any Stage-1 work requires a separate Planner decision.