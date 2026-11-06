# Issue 85 formal execution

The source provides three read-only/readiness modes and one gated executor:

```text
python formal_runner.py --plan
python formal_runner.py --preflight --issue84-source PATH --native-source PATH --stsr-source PATH
python formal_runner.py --verify-bundle --issue84-source PATH --native-source PATH --stsr-source PATH
python formal_runner.py --execute --issue84-source PATH --native-source PATH --stsr-source PATH --authorization-receipt PATH
```

`--preflight` binds the exact clean source and the registered 7,808-row seed/update tables. `--verify-bundle` rechecks those immutable receipts and source bindings without writing evidence or starting games. `--execute` is intentionally unavailable to an ordinary invocation unless its receipt binds the exact commit, source lock, contract, seed table, update schedule and analysis file.

Run these checks from a clean WSL Git worktree with the pinned Python 3.13.13 environment. WSL Git must resolve the checkout's worktree pointer and source object alternates before the runner can attest to its source identity.

The execution receipt has schema `issue85-formal-execution-authorization-v1` and must include:

- `issue: 85`, `status: "APPROVED"`, and `formal_go: true`;
- the exact `package_commit`, `source_lock_sha256`, `contract_sha256`, `seed_table_sha256`, `actor_update_schedule_sha256`, and `analysis_source_sha256` reported by the source-bound preflight;
- `whole_task_active_wall_cap_seconds`, a positive integer explicitly accepted by the research owner under the existing whole-task active-wall policy;
- `pilot_report_sha256`, which must equal the accepted retained nonformal Pilot report;
- `owner_approval: {status: "APPROVED", decision: "FORMAL_GO", comment_url: ...}`;
- `reviewer_approval: {status: "APPROVED", decision: "PASS", package_commit: ..., source_lock_sha256: ..., seed_table_sha256: ..., actor_update_schedule_sha256: ..., analysis_source_sha256: ..., comment_url: ...}`.

Both `comment_url` values must point to comments on Issue 85. The JSON is a provenance receipt; its URLs do not authenticate their authors by themselves. Create it only after the owner has directly approved the numeric whole-task allowance and formal GO and the Reviewer has independently passed the exact source. Those decisions are still outstanding for the current implementation, so no authorization receipt is present and this work does not start a formal game or actor update.

The executor resumes only from source/seed/receipt-bound evidence in the durable Issue 85 package. It preserves attempt directories, failure receipts, checkpoints, optimizer state, and elapsed active wall; idle time is excluded. A worker failure pauses dispatch for that invocation while already-running workers are safely joined. A later authorized invocation can resume with the same receipt and cap if the cumulative clock remains within it. There is no per-owner attempt limit or consecutive-failure stop rule.

TEST remains closed until all seven registered 64-owner validation groups and final model digests are frozen. Opening TEST writes an immutable receipt before any TEST worker starts. Frozen analysis checks the open receipt and each underlying validation/test attempt chain before computing the preregistered 20,000 owner-cluster bootstrap and four Holm-controlled confirmatory comparisons.
