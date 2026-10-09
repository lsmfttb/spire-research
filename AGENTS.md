# AGENTS.md

Keep the active project small.

Before working, read only:
1. the current GitHub Issue;
2. the code/API directly involved;
3. this file.

Global rules:

- `sts_lightspeed` or another explicitly adopted external simulator owns game mechanics. Do not implement game rules locally.
- The active native integration line for new research is `lsmfttb/sts_lightspeed` branch `stsrl/main`, at a currently reviewed and pinned commit on the accepted native lineage; exact native source identity lives on the spire task/PR. Treat `spire/main` as retiring and not a default development base; archived and task-named native branches remain provenance.
- Normal-public code must not receive simulator-private information.
- Study-specific code is disposable by default and should not become a core dependency automatically.
- A permanent core addition must represent a reusable semantic capability. Prefer replacement/consolidation over coexistence with historical versions.
- Do not preserve legacy schemas, adapters, runners, or validators without a current named consumer.
- Fix ordinary implementation, logging, serialization, launcher, artifact-writing, and similar operational bugs inside the implementation/review loop when scientific meaning is unchanged.
- Record enough durable coarse execution state that a crash does not require a new diagnostic study just to locate the failed boundary.
- Review the scientific claim independently from the implementation that produced it.
- Do not create task-history registries or copy historical governance into this repository.

Git and worktrees:

- Fetch/prune remote state before starting and base new work on current `origin/main`, or on the exact remote study branch for a repair.
- Use a separate worktree for non-trivial work. A worktree is a disposable execution environment, not durable task state.
- Push before handoff and report the remote branch plus exact commit SHA. Local-only commits are not reviewable project state.
- After handoff, remove the local worktree unless an active long-running process still needs it. A later repair should create a fresh worktree from remote state.
- Reviewer worktrees should be detached at the exact reviewed commit and removed after review.

Data:

- Generated data must not make a worktree non-disposable.
- Retained local data belongs outside worktrees and must have a producer commit plus either a named downstream consumer or a review/delete date.
- "Might be useful later" is not a retention reason.

Routing:

- GitHub is the durable handoff surface. Direct agent messages are notifications, not authority.
- When an open work Issue is routed to your role, that is sufficient authority to act within the Issue scope.
- Ordinary repair, rerun, push, review, cleanup, and handoff do not require Planner approval.
- Stop for Planner only when a decision would materially change scientific meaning, information regime, evaluation meaning, or long-lived architecture.

Roles are intentionally lightweight:

- Planner: scientific question, information boundary, evaluation, interpretation, and long-lived architecture.
- Builder: implementation and experiment execution.
- Reviewer: independent correctness/evidence review.

If a change needs extensive global history to justify itself, first assume the active abstraction is too complicated.
