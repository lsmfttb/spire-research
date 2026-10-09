from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import json

from sts_combat_rl.sim.action_space import ActionSpaceConfig
from sts_combat_rl.sim.controlled_run import execute_controlled_run
from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
from sts_combat_rl.sim.online_controller import PolicyController
from sts_combat_rl.sim.policy_contract import DecisionContext, PolicyDecision
from sts_combat_rl.sim.public_run_context import forbidden_public_context_problems


class FirstEligiblePublicWitness:
    name = "issue29_first_eligible_public_witness"

    def __init__(self) -> None:
        self.calls = 0
        self.screens: Counter[str] = Counter()

    @property
    def provenance_config(self) -> dict[str, object]:
        return {"selection": "first eligible action", "search": False}

    def select_action(self, context: DecisionContext) -> PolicyDecision:
        violations = forbidden_public_context_problems(asdict(context))
        if violations:
            raise ValueError(
                "forbidden data in public decision context: "
                + "; ".join(violations)
            )
        if not context.eligible_action_indices:
            raise ValueError("no eligible action in public decision context")
        self.calls += 1
        self.screens[context.screen_state] += 1
        return PolicyDecision(
            legal_action_index=context.eligible_action_indices[0],
            reason="first_eligible_public_witness",
        )


seed = 20261009
policy = FirstEligiblePublicWitness()
adapter = LightSpeedAdapter(seed=seed, ascension=20)
try:
    run = execute_controlled_run(
        adapter,
        PolicyController(policy),
        seed=seed,
        max_steps=2500,
        action_space=ActionSpaceConfig.initial_no_potions(),
        retain_steps=False,
    )
finally:
    adapter.close()

provenance = run.controller_provenance
result = {
    "stsr_repository_commit": "3037b75eca4bd73fa70d018ffd4442a1f2d65628",
    "native_integration_ref": "refs/heads/stsrl/main",
    "native_source_commit": "d61c7c2a120aa90730fd8530718a7cd7cd856b46",
    "native_public_projection_schema": run.public_run_context.get(
        "source_projection_schema_id"
    ),
    "policy": policy.name,
    "information_regime": provenance.get("config", {}).get("information_regime"),
    "seed": seed,
    "ascension": 20,
    "max_steps": 2500,
    "policy_calls": policy.calls,
    "screens_seen": dict(sorted(policy.screens.items())),
    "terminal": run.terminal,
    "outcome": run.outcome,
    "problems": run.problems,
    "retained_step_records": len(run.steps),
}
print(json.dumps(result, sort_keys=True))
if not run.terminal or not policy.calls or run.problems:
    raise SystemExit("public no-search full-run smoke did not complete cleanly")
if result["information_regime"] != "normal_public_policy":
    raise SystemExit("controlled run did not identify the normal-public policy boundary")
