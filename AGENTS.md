# AGENTS.md

Keep the active project small.

Before working, read:
1. the current GitHub Issue and this file;
2. the accepted STSRL `docs/current_status.md` and `docs/project_architecture.md` relevant to the question, including its cited task/experiment evidence;
3. the existing STSRL and pinned `sts_lightspeed` code/API directly involved.

Do not use a new Issue or this repository's experimental native branch as evidence that mature STSRL infrastructure is missing.

Global rules:

- `sts_lightspeed` or another explicitly adopted external simulator owns game mechanics. Do not implement game rules locally.
- **AUTHORITATIVE EXISTING RUNTIME:** `lsmfttb/STSRL:main` with the exact `lsmfttb/sts_lightspeed:stsrl/main` commit pinned in STSRL's `docs/sts_lightspeed_source_manifest.json`. It already has whole-run execution, action enumeration, noncombat control, public-context code, Battle search and extensive experiments. Verify source pins; do not replace it because an alternative interface is incomplete.
- **RETIRED EXPERIMENTAL LINE:** `lsmfttb/sts_lightspeed:spire/main` and `native-public-projection-v3` are not the active integration base or a development roadmap. Their historical reviewed snapshot is archived at `archive/spire-v3-2026-10-09` (SHA `d1dcd6534ec4a1f38ac1f7f916f01a3f931fcfd0`). Do not extend, revive or migrate to v3 to improve coverage. The current v3 retirement PR must be independently reviewed before its code removal is treated as landed.
- **NO INTERFACE-COVERAGE TASKS:** Unsupported v3 screens, parser version mismatch or a neater replacement API are not infrastructure defects. Before a task changing any simulator adapter/public projection, exhibit a real noSL violation or a decision-relevant missing public fact on the existing paired runtime, compare the smallest reuse/adapter-only fix, and obtain explicit Planner scientific authorization. No Builder may infer authorization from a successful previous Issue or from a new schema's missing field.
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
