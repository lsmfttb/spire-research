# Issue #59: natural two-battle potion allocation

This folder contains one disposable `STUDY_ONLY` controlled study: independent natural standard-start Ironclad A20 owner seeds 559001–559064, one first selected legal non-Smoke potion-use root per natural run, paired consume-now vs reserve-current-B1 interventions, and a B1-to-next-authentic-B2 survival/resource horizon. It is not a rerun of Issue #56, and contains no #56 traces or post-search filtering. No Native or STSRL source is edited.

## Frozen runtime

- Native `lsmfttb/sts_lightspeed` source: commit `0990ef48d51cd48444a707839d65226d52c1d73e` at `/home/lsmft/stsrl-spikes/sts_lightspeed-issue59`.
- STSRL executor: commit `7d172f7c24c9d29429b6946ba44f6b59760b0d96` at `/home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c`.
- Python 3.14.4, CMake 4.2.3, GNU g++ 15.2.0. Native extension is built outside the repository at `/home/lsmft/stsrl-spikes/build-issue59-native-0990-py314`; its exact SHA-256 is frozen in `study.py`.
- Native CMake configure uses `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` for the pinned JSON submodule's old minimum version; this is a build compatibility setting, not a source change.
- Keep these external source/build paths available for independent replay through 2026-11-15, then delete them with the study artifacts.

The exact Native #57 focused slot-exclusion test passed at 400 simulations; its source hash, stdout, exit code, loaded module hash and measured configure/build/test CPU are recorded in `native_api_verification.json`. No wide Native suite was rerun. The successful native configure/build commands are:

```bash
cmake -S /home/lsmft/stsrl-spikes/sts_lightspeed-issue59 \
  -B /home/lsmft/stsrl-spikes/build-issue59-native-0990-py314 \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5
cmake --build /home/lsmft/stsrl-spikes/build-issue59-native-0990-py314 --target slaythespire -j4
```

## Run and verify

From the WSL repository root, set:

```bash
export STSRL_ROOT=/home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c
export NATIVE_MODULE_DIR=/home/lsmft/stsrl-spikes/build-issue59-native-0990-py314
export ISSUE59_NATIVE_ROOT=/home/lsmft/stsrl-spikes/sts_lightspeed-issue59
export ISSUE59_NATIVE_CONFIGURE_CPU_S=0.57
export ISSUE59_NATIVE_BUILD_CPU_S=83.37
export SPIRE_WORKTREE_COMMIT="$(git rev-parse HEAD)"
```

The hard all-in cap is 12,000 process-CPU seconds. The study child ceiling is 10,198 seconds; the measured runner adds a 1-second accounting margin, keeping the Builder below 10,200 and reserving at least 1,800 for independent review. Before first study execution only, account for setup/verification CPU in `budget_events.jsonl` with `record_external_cpu.py`; do not append the baseline a second time when an existing ledger is present. For each later process, use `run_measured_stage.py`; it records child and wrapper CPU plus the margin, appends an fsynced ledger event, refreshes the output summary's ledger hash, and refuses further work above the all-in Builder ceiling.

```bash
if [ ! -f studies/issue59/budget_events.jsonl ]; then
  python3 studies/issue59/record_external_cpu.py native-build-and-initial-verification 90 --conservative --measurement "90s conservative allocation covering 83.94s native configure+build and measured test/tooling overhead rounded upward"
fi
python3 studies/issue59/run_measured_stage.py --summary studies/issue59/preflight/summary.json focused-tests \
  -- python3 -m unittest discover -s studies/issue59 -p test_study.py -v
python3 studies/issue59/run_measured_stage.py --summary studies/issue59/preflight/summary.json semantic-preflight \
  -- python3 studies/issue59/study.py --preflight
```

Only if `preflight/summary.json` says `SEMANTIC_PREFLIGHT_PASS`, execute the fixed 64-owner cohort once:

```bash
python3 studies/issue59/run_measured_stage.py --summary studies/issue59/results/summary.json fixed-cohort \
  -- python3 studies/issue59/study.py
```

The study itself rechecks the loaded extension hash, pinned source commits and STSRL source hashes, producer commit, preflight code/source/ledger hashes, and current process-CPU ceiling. Native checkpoints and captured noncombat RNG are process-memory only; never serialize them. A missing B2 without a genuine terminal outcome is incomplete. No owner seed is replaced after observing results.

## Data handling

The event ledger and progress files record coarse durable boundaries; owner, source-attempt, manifest, summary, and public transition artifacts support review. Keep only the named Issue #59 reviewer replay inputs. Compact and delete this study's traces, ledgers, native build, and pinned temporary worktrees by 2026-11-15 after review. Do not merge this study machinery into core or use it for training/controller promotion.
