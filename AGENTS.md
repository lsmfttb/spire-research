# AGENTS.md

Keep the active project small.

Before working, read only:
1. the current GitHub Issue;
2. the code/API directly involved;
3. this file.

Global rules:

- `sts_lightspeed` or another explicitly adopted external simulator owns game mechanics. Do not implement game rules locally.
- Normal-public code must not receive simulator-private information.
- Study-specific code is disposable by default and should not become a core dependency automatically.
- A permanent core addition must represent a reusable semantic capability. Prefer replacement/consolidation over coexistence with historical versions.
- Do not preserve legacy schemas, adapters, runners, or validators without a current named consumer.
- Fix ordinary implementation, logging, serialization, launcher, artifact-writing, and similar operational bugs inside the implementation/review loop when scientific meaning is unchanged.
- Record enough durable coarse execution state that a crash does not require a new diagnostic study just to locate the failed boundary.
- Review the scientific claim independently from the implementation that produced it.
- Do not create task-history registries or copy historical governance into this repository.

Roles are intentionally lightweight:

- Planner: scientific question, information boundary, evaluation, interpretation, and long-lived architecture.
- Builder: implementation and experiment execution.
- Reviewer: independent correctness/evidence review.

If a change needs extensive global history to justify itself, first assume the active abstraction is too complicated.
