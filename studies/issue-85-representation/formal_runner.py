"""Issue 85 formal cohort, source-readiness and gated execution tooling.

Formal execution is available only with a source/seed-bound owner GO receipt,
a whole-task active-wall cap, and an independent review of the exact commit.
The registered design and the nonformal Pilot remain separate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

import pilot_runner as pilot
import preflight as baseline_preflight


STUDY = Path(__file__).resolve().parent
REPO = STUDY.parents[1]
LOCK = pilot.LOCK
CONTRACT = pilot.CONTRACT
DATA_ROOT = pilot.DATA_ROOT
LOCK_PATH = pilot.LOCK_PATH
CONTRACT_PATH = pilot.CONTRACT_PATH
NEW_ARMS = tuple(CONTRACT["registered_design_unchanged"]["new_training_arms"])
POLICIES = ("PARENT", "A", *NEW_ARMS)
FORMAL_SEED_NAMESPACE = "issue85-formal-seed-v1"
ANALYSIS_SEED_NAMESPACE = "issue85-test-bootstrap-v1"
SEARCH_SEED_COUNT = 4
CACHE_EQUIVALENCE_RELATIVE_PATH = Path(
    "issue85/artifacts/performance/20261106-actor-cache-equivalence-v10-cache-control/"
    "equivalence-comparison-source-bound.json"
)
CACHE_EQUIVALENCE_SHA256 = "482e1d59f61f37286ce2f308a438ec6d189150fdb5a55f00194858105f911877"
CACHE_MEASUREMENT_SOURCE_COMMIT = "b5c8bc43e030449c10c30c088c4d6171dcce9033"
LOCKED_PILOT_RUNNER_COMMIT = "5cd70c00d03f1104728efdc4b66c764592e5acf8"
EXPECTED_MEASUREMENT_FILES = (
    "studies/issue-85-representation/compare_performance.py",
    "studies/issue-85-representation/performance_harness.py",
    "studies/issue-85-representation/performance_provenance.py",
    "studies/issue-85-representation/pilot_runner.py",
    "studies/issue-85-representation/representations.py",
    "studies/issue-85-representation/runtime_integration.py",
    "studies/issue-85-representation/source-lock.json",
)


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object at {path}")
    return value


def validate_cache_equivalence_report(
    report: Mapping[str, Any], *, report_sha256: str,
) -> dict[str, Any]:
    """Bind formal readiness to the reviewed, source-bound v10 cache evidence."""
    if report_sha256 != CACHE_EQUIVALENCE_SHA256:
        raise RuntimeError("the retained v10 cache-equivalence report digest differs")
    source = report.get("measurement_source")
    binding = report.get("source_binding_validation")
    arms = report.get("arms")
    if (
        report.get("schema_id") != "issue85-offline-cache-equivalence-comparison-v2"
        or report.get("scope") != "existing nonformal Pilot training traces only; no game/replay/formal run"
        or report.get("comparison") != "uncached source path versus parameter-independent per-block input cache"
        or report.get("all_arms_bitwise_equivalent") is not True
        or not isinstance(source, Mapping)
        or source.get("commit") != CACHE_MEASUREMENT_SOURCE_COMMIT
        or source.get("tree") != _git_text(REPO, "rev-parse", f"{CACHE_MEASUREMENT_SOURCE_COMMIT}^{{tree}}")
        or source.get("clean_worktree_at_capture") is not True
        or not isinstance(binding, Mapping)
        or binding.get("baseline_source_lock_validated") != file_sha256(LOCK_PATH)
        or binding.get("issue84_source_lock_validated") != LOCK["baseline_A"]["issue84_final_accepted_source_lock_sha256"]
        or binding.get("metric_snapshot_hashes_validated") is not True
        or "baseline cache is disabled" not in str(binding.get("rule", ""))
        or "candidate cache is verified" not in str(binding.get("rule", ""))
        or not isinstance(arms, Mapping)
        or set(arms) != set(NEW_ARMS)
    ):
        raise RuntimeError("v10 cache-equivalence provenance is incomplete or outside its accepted scope")
    for arm in NEW_ARMS:
        entry = arms[arm]
        steps = entry.get("steps") if isinstance(entry, Mapping) else None
        if (
            not isinstance(entry, Mapping)
            or entry.get("all_two_step_fields_bitwise_equal") is not True
            or not isinstance(steps, list)
            or len(steps) != 2
            or any(
                not isinstance(step, Mapping)
                or step.get("step") != index
                or step.get("bitwise_equal") is not True
                or step.get("mismatch_count") != 0
                or step.get("mismatches") != []
                for index, step in enumerate(steps, start=1)
            )
            or (arm == "B" and entry.get("candidate_cache") is not None)
            or (
                arm != "B"
                and (
                    not isinstance(entry.get("candidate_cache"), Mapping)
                    or entry["candidate_cache"].get("arm") != arm
                    or entry["candidate_cache"].get("owner_count") != 16
                )
            )
        ):
            raise RuntimeError(f"v10 cache-equivalence evidence does not bind arm {arm}")
    files = source.get("files")
    if not isinstance(files, list) or {entry.get("path") for entry in files if isinstance(entry, Mapping)} != set(EXPECTED_MEASUREMENT_FILES):
        raise RuntimeError("v10 comparison does not enumerate the expected exact measurement source files")
    for entry in files:
        relative = Path(str(entry["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("v10 comparison contains an unsafe source path")
        current = REPO / relative
        expected_sha = str(entry.get("git_content_sha256", ""))
        expected_bytes = entry.get("git_content_bytes")
        if (
            not current.is_file()
            or file_sha256(current) != expected_sha
            or current.stat().st_size != expected_bytes
            or entry.get("bytes") != expected_bytes
            or entry.get("worktree_sha256") != expected_sha
            or entry.get("git_blob_sha1") != _git_text(
                REPO, "rev-parse", f"{CACHE_MEASUREMENT_SOURCE_COMMIT}:{relative.as_posix()}"
            )
        ):
            raise RuntimeError(f"current formal source no longer matches reviewed v10 input: {relative}")
    pilot_runner_path = "studies/issue-85-representation/pilot_runner.py"
    pilot_runner_entry = next((entry for entry in files if entry.get("path") == pilot_runner_path), None)
    if (
        pilot_runner_entry is None
        or _git_text(REPO, "merge-base", "--is-ancestor", LOCKED_PILOT_RUNNER_COMMIT, CACHE_MEASUREMENT_SOURCE_COMMIT) != ""
        or _git_text(REPO, "rev-parse", f"{LOCKED_PILOT_RUNNER_COMMIT}:{pilot_runner_path}")
        != pilot_runner_entry.get("git_blob_sha1")
    ):
        raise RuntimeError("the v10 pilot-runner file differs from the locked Pilot baseline")
    return {
        "report_path": CACHE_EQUIVALENCE_RELATIVE_PATH.as_posix(),
        "report_sha256": report_sha256,
        "measurement_source_commit": CACHE_MEASUREMENT_SOURCE_COMMIT,
        "pilot_runner_baseline_commit": LOCKED_PILOT_RUNNER_COMMIT,
        "baseline_cache_disabled": True,
        "candidate_cache_policy": {"B": "bypass", "C": "per-block cache", "D": "per-block cache", "E": "per-block cache", "F": "per-block cache"},
        "equivalence_scope": "two sequential optimizer updates on retained 16-owner Pilot traces; not 32-owner/64-step validation or policy efficacy",
    }


def verify_cache_equivalence(data_root: Path = DATA_ROOT) -> dict[str, Any]:
    path = Path(data_root) / CACHE_EQUIVALENCE_RELATIVE_PATH
    if not path.is_file():
        raise RuntimeError(f"the reviewed v10 cache evidence is missing: {path}")
    digest = file_sha256(path)
    return validate_cache_equivalence_report(read_json(path), report_sha256=digest)


def whole_task_active_wall_estimate(cache_report: Mapping[str, Any]) -> dict[str, Any]:
    projection = cache_report.get("conditional_cost_projection")
    if not isinstance(projection, Mapping):
        raise RuntimeError("the reviewed v10 evidence lacks the conditional formal-core projection")
    core = float(projection["conditional_formal_core_projection_hours"])
    cumulative_with_pilot = float(projection["conditional_pilot_plus_formal_core_hours"])
    actor_updates = float(projection["conditional_actor_update_projection_hours"])
    game_groups = float(projection["unchanged_formal_game_group_projection_hours"])
    if not math.isclose(actor_updates + game_groups, core, rel_tol=0.0, abs_tol=0.002):
        raise RuntimeError("v10 formal-core components do not sum to their registered projection")
    pilot_hours = cumulative_with_pilot - core
    reserve_assumptions = {
        "frozen_analysis_and_machine_closeout": 1.0,
        "program_active_build_restore_and_queue": 1.0,
        "unmeasured_32_owner_eight_worker_contention_retries_restarts_and_long_tails": core * 0.5,
    }
    assumed_reserve = math.fsum(reserve_assumptions.values())
    cumulative_estimate = cumulative_with_pilot + assumed_reserve
    return {
        "schema_id": "issue85-whole-task-active-wall-estimate-v1",
        "status": "CONDITIONAL_PLANNING_ESTIMATE / NOT_MEASURED_ALL_IN / NO_GO",
        "cumulative_active_wall_hours_approx": round(cumulative_estimate, 2),
        "unrounded_cumulative_active_wall_hours": cumulative_estimate,
        "cumulative_components_hours": {
            "pilot_already_spent_and_included_once": pilot_hours,
            "conditional_formal_core": core,
            "assumed_retry_and_long_tail_reserve": reserve_assumptions[
                "unmeasured_32_owner_eight_worker_contention_retries_restarts_and_long_tails"
            ],
            "assumed_analysis_build_restore_and_queue": math.fsum((
                reserve_assumptions["frozen_analysis_and_machine_closeout"],
                reserve_assumptions["program_active_build_restore_and_queue"],
            )),
        },
        "formal_core_breakdown_hours": {
            "conditional_actor_update_component": actor_updates,
            "conditional_train_val_test_game_component": game_groups,
        },
        "reserve_assumptions_hours": reserve_assumptions,
        "human_review_or_wait_hours": 0.0,
        "limitations": [
            "Pilot plus formal-core arithmetic is conditional; it is not measured full-study runtime.",
            "The 50%-of-core retry/long-tail reserve and 2-hour program-active allowance are explicit planning assumptions, not a measured upper bound or time cap.",
            "The formal 32-owner/eight-worker path, retry and restart tails, restore/build/queue work and bootstrap runtime remain unmeasured.",
            "Pilot wall already includes its observed failed attempts; those are not added a second time.",
            "Human review and waiting are excluded under the active physical-wall policy.",
            "This estimate does not grant formal GO and no registered game or actor update has started.",
        ],
    }


def _write_or_verify_once(path: Path, value: object) -> str:
    path = Path(path)
    raw = canonical(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        observed = path.read_bytes()
        if observed != raw:
            raise RuntimeError(f"immutable formal-readiness file differs: {path}")
        return sha256_bytes(observed)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return sha256_bytes(raw)


def _registered_ranges() -> dict[str, range]:
    baseline_owners = LOCK["baseline_A"]["training_owners"]
    train_count = int(CONTRACT["registered_design_unchanged"]["training_owners_per_arm"])
    if (
        len(baseline_owners) != 2
        or int(baseline_owners[1]) - int(baseline_owners[0]) + 1 != train_count
        or int(LOCK["baseline_A"]["training_owner_count"]) != train_count
    ):
        raise RuntimeError("the locked A/B1024 training-owner range is inconsistent")
    validation = CONTRACT["registered_design_unchanged"]["validation"]
    test = CONTRACT["registered_design_unchanged"]["test"]
    return {
        "training": range(int(baseline_owners[0]), int(baseline_owners[1]) + 1),
        "validation": range(int(validation["owners"][0]), int(validation["owners"][1]) + 1),
        "test": range(int(test["owners"][0]), int(test["owners"][1]) + 1),
    }


def formal_plan() -> dict[str, Any]:
    ranges = _registered_ranges()
    training = len(NEW_ARMS) * len(ranges["training"])
    validation = len(POLICIES) * len(ranges["validation"])
    test = len(POLICIES) * len(ranges["test"])
    total = training + validation + test
    registered = int(CONTRACT["registered_design_unchanged"]["formal_games"])
    if total != registered:
        raise RuntimeError(f"formal plan count {total} differs from the registered {registered}")
    owners_per_block = int(CONTRACT["training"]["owners_per_block"])
    update_steps = int(CONTRACT["training"]["updates_per_block"])
    training_schedule: list[dict[str, Any]] = []
    for arm in NEW_ARMS:
        for block in range(len(ranges["training"]) // owners_per_block):
            owner_start = ranges["training"].start + block * owners_per_block
            training_schedule.extend((
                {
                    "stage_id": f"TRAIN-{arm}-BLOCK-{block:02d}-GAMES",
                    "stage": "training_games",
                    "arm": arm,
                    "block_index": block,
                    "owners": [owner_start, owner_start + owners_per_block - 1],
                    "expected_games": owners_per_block,
                    "fresh_complete_games": True,
                },
                {
                    "stage_id": f"TRAIN-{arm}-BLOCK-{block:02d}-ACTOR-UPDATE",
                    "stage": "actor_update",
                    "arm": arm,
                    "block_index": block,
                    "depends_on": f"TRAIN-{arm}-BLOCK-{block:02d}-GAMES",
                    "minibatch_owners": int(CONTRACT["training"]["minibatch_owners"]),
                    "update_steps": update_steps,
                    "block_cache_lifetime": "this block only; rebuild after each block",
                    "actor_distribution_loss_optimizer_live_each_step": True,
                },
            ))
    training_stage_games = sum(
        int(stage["expected_games"]) for stage in training_schedule
        if stage["stage"] == "training_games"
    )
    if training_stage_games != training:
        raise RuntimeError("formal training stage schedule differs from the registered game count")
    return {
        "schema_id": "issue85-formal-plan-v1",
        "scope": "REGISTERED_FORMAL_PLAN / NOT_STARTED",
        "execution_status": "NOT_STARTED",
        "formal_execution_command_available": True,
        "formal_execution_gate": "owner FORMAL_GO + numeric whole-task active-wall cap + Reviewer approval of exact commit",
        "formal_games_started": 0,
        "source_lock_sha256": file_sha256(LOCK_PATH),
        "contract_sha256": file_sha256(CONTRACT_PATH),
        "new_training_arms": list(NEW_ARMS),
        "evaluation_policies": list(POLICIES),
        "owner_ranges": {
            name: [owners.start, owners.stop - 1]
            for name, owners in ranges.items()
        },
        "training_games": training,
        "validation_games": validation,
        "test_games": test,
        "formal_games": total,
        "training_blocks_per_arm": (
            len(ranges["training"]) // owners_per_block
        ),
        "owners_per_training_block": owners_per_block,
        "actor_updates_per_block": update_steps,
        "training_stage_schedule": training_schedule,
        "validation_stage": {
            "stage_id": "VALIDATION",
            "owners": [ranges["validation"].start, ranges["validation"].stop - 1],
            "policies": list(POLICIES),
            "games": validation,
            "model_selection_or_tuning": False,
        },
        "test_stage": {
            "stage_id": "TEST",
            "owners": [ranges["test"].start, ranges["test"].stop - 1],
            "policies": list(POLICIES),
            "games": test,
            "opens_only_after_source_seed_analysis_freeze": True,
        },
        "pilot_owner_ids_excluded": list(range(
            int(CONTRACT["registered_design_unchanged"]["pilot_owners"][0]),
            int(CONTRACT["registered_design_unchanged"]["pilot_owners"][1]) + 1,
        )),
        "formal_cohorts_changed": False,
    }


def _derive_formal_seed(*parts: object) -> int:
    text = FORMAL_SEED_NAMESPACE + "|" + "|".join(str(part) for part in parts)
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:4], "big") & 0x7FFFFFFF


def _seed_row(
    backend: Any, *, owner: int, stage: str, policy: str,
    block: int | None = None,
) -> dict[str, Any]:
    phase = "training" if stage == "training" else "evaluation"
    backend_arm = "TRAIN" if stage == "training" else policy
    row = dict(backend._row(
        owner, phase=phase, arm=backend_arm, formal=True, block=block,
    ))
    if stage == "training":
        row.update({
            "unit_id": f"TRAIN:{owner}:{policy}",
            "arm": "TRAIN",
            "phase": "training",
            "formal": True,
            "block_index": block,
            "paired_owner_key": f"issue85-formal-train-{owner}",
            "issue84_arm": policy,
            "issue84_policy_label": "pi12",
            "issue85_arm": policy,
            "issue85_phase": "training",
        })
    else:
        row.update({
            "unit_id": f"{stage.upper()}:{owner}:{policy}",
            "arm": policy,
            "phase": "evaluation",
            "formal": True,
            "block_index": None,
            "paired_owner_key": f"issue85-formal-{stage}-{owner}",
            "issue84_arm": policy if policy != "PARENT" else None,
            "issue84_policy_label": "pi12" if policy == "PARENT" else "pi20",
            "issue85_phase": stage,
        })
        if policy in NEW_ARMS:
            row["issue85_arm"] = policy
        else:
            row.pop("issue85_arm", None)

    row["owner_seed"] = int(owner)
    row["game_seed"] = int(owner)
    seed_stage = "training" if stage == "training" else stage
    row["policy_rng_seed"] = _derive_formal_seed(seed_stage, policy, owner, "policy")
    row["search_invocation_seeds"] = [
        _derive_formal_seed(seed_stage, policy, owner, "search", index)
        for index in range(SEARCH_SEED_COUNT)
    ]
    return row


def build_formal_seed_table(backend: Any, identity: Mapping[str, str]) -> dict[str, Any]:
    ranges = _registered_ranges()
    rows: list[dict[str, Any]] = []
    train_owners = ranges["training"]
    owners_per_block = int(CONTRACT["training"]["owners_per_block"])
    if len(train_owners) % owners_per_block:
        raise RuntimeError("registered training range does not divide into complete owner blocks")
    for arm in NEW_ARMS:
        for owner in train_owners:
            block = (owner - train_owners.start) // owners_per_block
            rows.append(_seed_row(backend, owner=owner, stage="training", policy=arm, block=block))
    for stage in ("validation", "test"):
        for owner in ranges[stage]:
            for policy in POLICIES:
                rows.append(_seed_row(backend, owner=owner, stage=stage, policy=policy))
    phase_order = {"training": 0, "validation": 1, "test": 2}
    rows.sort(key=lambda row: (
        phase_order[str(row["issue85_phase"])], int(row["owner_seed"]),
        str(row["arm"]), str(row.get("issue85_arm", "")),
    ))
    plan = formal_plan()
    actor_update_schedule = [
        {
            "arm": arm,
            "block_index": block,
            "seed": _derive_formal_seed("actor-update", arm, block),
            "owners": [
                train_owners.start + block * owners_per_block,
                train_owners.start + (block + 1) * owners_per_block - 1,
            ],
            "owner_count": owners_per_block,
            "updates": int(CONTRACT["training"]["updates_per_block"]),
            "minibatch_owners": int(CONTRACT["training"]["minibatch_owners"]),
            "sampling": "Python random.Random(seed); shuffle all 128 owners each epoch; four consecutive 32-owner minibatches per epoch",
        }
        for arm in NEW_ARMS
        for block in range(len(train_owners) // owners_per_block)
    ]
    expected_counts = {
        "training": plan["training_games"],
        "validation": plan["validation_games"],
        "test": plan["test_games"],
    }
    table = {
        "schema_id": "issue85-formal-seed-table-v1",
        "scope": "REGISTERED_FORMAL_COHORTS / NOT_EXECUTED",
        "execution_status": "NOT_STARTED",
        "formal_games_started": 0,
        "package_commit": str(identity["package_commit"]),
        "source_lock_sha256": str(identity["source_lock_sha256"]),
        "contract_sha256": file_sha256(CONTRACT_PATH),
        "seed_derivation": (
            f"{FORMAL_SEED_NAMESPACE}|stage|policy|owner|purpose|index; "
            "SHA256 first 31 bits; game_seed equals natural owner id"
        ),
        "same_training_start_owners_across_B_F": True,
        "arm_specific_policy_and_search_rng": True,
        "common_evaluation_owner_ids_do_not_imply_common_policy_rng": True,
        "pilot_rows_included": False,
        "expected_games_by_phase": expected_counts,
        "row_count": len(rows),
        "rows_sha256": sha256_bytes(canonical(rows)),
        "actor_update_schedule": actor_update_schedule,
        "actor_update_schedule_sha256": sha256_bytes(canonical(actor_update_schedule)),
        "rows": rows,
    }
    validate_formal_seed_table(table, identity=identity)
    return table


def validate_formal_seed_table(
    table: Mapping[str, Any], *, identity: Mapping[str, str] | None = None,
) -> None:
    if table.get("schema_id") != "issue85-formal-seed-table-v1":
        raise ValueError("formal seed-table schema differs")
    if table.get("scope") != "REGISTERED_FORMAL_COHORTS / NOT_EXECUTED":
        raise ValueError("formal seed-table scope/status differs")
    if table.get("execution_status") != "NOT_STARTED" or table.get("formal_games_started") != 0:
        raise ValueError("formal seed table must remain explicitly unexecuted")
    if identity is not None and (
        table.get("package_commit") != identity.get("package_commit")
        or table.get("source_lock_sha256") != identity.get("source_lock_sha256")
    ):
        raise ValueError("formal seed table is bound to another source commit or lock")
    rows = table.get("rows")
    if not isinstance(rows, list):
        raise ValueError("formal seed table rows must be a list")
    if table.get("rows_sha256") != sha256_bytes(canonical(rows)):
        raise ValueError("formal seed-table row digest differs")
    updates = table.get("actor_update_schedule")
    plan = formal_plan()
    owners_per_block = int(CONTRACT["training"]["owners_per_block"])
    expected_updates = [
        {
            "arm": arm,
            "block_index": block,
            "seed": _derive_formal_seed("actor-update", arm, block),
            "owners": [
                int(plan["owner_ranges"]["training"][0]) + block * owners_per_block,
                int(plan["owner_ranges"]["training"][0]) + (block + 1) * owners_per_block - 1,
            ],
            "owner_count": owners_per_block,
            "updates": int(CONTRACT["training"]["updates_per_block"]),
            "minibatch_owners": int(CONTRACT["training"]["minibatch_owners"]),
            "sampling": "Python random.Random(seed); shuffle all 128 owners each epoch; four consecutive 32-owner minibatches per epoch",
        }
        for arm in NEW_ARMS
        for block in range(int(plan["training_blocks_per_arm"]))
    ]
    if (
        updates != expected_updates
        or table.get("actor_update_schedule_sha256") != sha256_bytes(canonical(expected_updates))
    ):
        raise ValueError("formal actor-update RNG schedule is not the exact registered 128/64/32 schedule")
    ranges = _registered_ranges()
    expected: dict[str, tuple[int, str, int | None]] = {}
    for arm in NEW_ARMS:
        for owner in ranges["training"]:
            block = (owner - ranges["training"].start) // int(CONTRACT["training"]["owners_per_block"])
            expected[f"TRAIN:{owner}:{arm}"] = (owner, "training", block)
    for stage in ("validation", "test"):
        for owner in ranges[stage]:
            for policy in POLICIES:
                expected[f"{stage.upper()}:{owner}:{policy}"] = (owner, stage, None)
    if len(rows) != len(expected) or int(table.get("row_count", -1)) != len(expected):
        raise ValueError("formal seed table does not have exactly the registered number of rows")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("formal seed table contains a non-object row")
        unit_id = str(row.get("unit_id", ""))
        if unit_id in seen or unit_id not in expected:
            raise ValueError(f"formal seed table has a duplicate or unregistered unit: {unit_id}")
        seen.add(unit_id)
        owner, stage, block = expected[unit_id]
        policy = unit_id.rsplit(":", 1)[1]
        if (
            row.get("formal") is not True
            or int(row.get("owner_seed", -1)) != owner
            or int(row.get("game_seed", -1)) != owner
            or row.get("issue85_phase") != stage
            or row.get("block_index") != block
        ):
            raise ValueError(f"formal seed row has an invalid owner, phase, or block: {unit_id}")
        expected_policy = policy
        if row.get("issue85_phase") == "training":
            if row.get("arm") != "TRAIN" or row.get("issue85_arm") != expected_policy:
                raise ValueError(f"formal training row has an invalid learner binding: {unit_id}")
            seed_stage = "training"
            paired = f"issue85-formal-train-{owner}"
        else:
            if row.get("arm") != expected_policy:
                raise ValueError(f"formal evaluation row has an invalid policy binding: {unit_id}")
            if expected_policy in NEW_ARMS and row.get("issue85_arm") != expected_policy:
                raise ValueError(f"new policy row lacks its Issue 85 arm binding: {unit_id}")
            if expected_policy not in NEW_ARMS and "issue85_arm" in row:
                raise ValueError(f"frozen baseline row must not be tagged as a new arm: {unit_id}")
            seed_stage = stage
            paired = f"issue85-formal-{stage}-{owner}"
        if row.get("paired_owner_key") != paired:
            raise ValueError(f"formal seed row has an incorrect paired-owner key: {unit_id}")
        if row.get("policy_rng_seed") != _derive_formal_seed(seed_stage, expected_policy, owner, "policy"):
            raise ValueError(f"formal policy RNG seed is not reproducible: {unit_id}")
        search_seeds = row.get("search_invocation_seeds")
        expected_search = [
            _derive_formal_seed(seed_stage, expected_policy, owner, "search", index)
            for index in range(SEARCH_SEED_COUNT)
        ]
        if search_seeds != expected_search:
            raise ValueError(f"formal search invocation seeds are not reproducible: {unit_id}")
    if seen != set(expected):
        raise ValueError("formal seed table omits registered rows")
    plan = formal_plan()
    for stage, expected_count in (
        ("training", plan["training_games"]),
        ("validation", plan["validation_games"]),
        ("test", plan["test_games"]),
    ):
        actual_count = sum(row.get("issue85_phase") == stage for row in rows)
        if actual_count != expected_count:
            raise ValueError(f"formal {stage} row count is {actual_count}, expected {expected_count}")


def _git_text(repo: Path, *args: str) -> str:
    return baseline_preflight.git_bytes(repo, *args).decode("utf-8").strip()


def _verify_locked_repository(root: Path, expected_commit: str, *, label: str) -> dict[str, str]:
    root = Path(root).resolve(strict=True)
    commit = _git_text(root, "rev-parse", "HEAD")
    dirty = _git_text(root, "status", "--porcelain", "--untracked-files=all")
    if commit != expected_commit:
        raise RuntimeError(f"{label} source is {commit}; expected {expected_commit}")
    if dirty:
        raise RuntimeError(f"{label} source worktree is not clean")
    return {"root": str(root), "commit": commit, "clean": True}


def _verify_native_source(root: Path) -> dict[str, Any]:
    native = LOCK["native"]
    identity = _verify_locked_repository(root, native["commit"], label="native")
    blob = _git_text(
        Path(identity["root"]), "rev-parse", f"{native['commit']}:{native['source_file']}",
    )
    if blob != native["source_file_git_blob_sha1"]:
        raise RuntimeError(f"locked native binding blob differs: {blob}")
    binary = DATA_ROOT / native["compiled_module_data_path"]
    if not binary.is_file() or file_sha256(binary) != native["compiled_module_sha256"]:
        raise RuntimeError("locked native extension is missing or its digest differs")
    return {**identity, "source_file_blob_sha1": blob, "compiled_module_sha256": file_sha256(binary)}


def _verify_stsr_source(root: Path) -> dict[str, Any]:
    source = LOCK["stsr_source"]
    return _verify_locked_repository(root, source["commit"], label="STSRL")


def _host_path(wsl_path: str) -> Path:
    if os.name != "nt":
        return Path(wsl_path)
    for prefix, drive in (("/mnt/d/", "D:/"), ("/mnt/c/", "C:/")):
        if wsl_path.startswith(prefix):
            return Path(drive) / wsl_path[len(prefix):]
    raise ValueError(f"unsupported WSL data path: {wsl_path}")


def _validate_issue84_source_for_runtime(path: Path) -> Path:
    """Pass the legacy driver's clean-tree guard only after our own check.

    The accepted Issue 84 driver requires ISSUE84_WORKTREE_CLEAN as an
    explicit caller attestation.  The Issue 85 validator below verifies the
    exact accepted commit and a clean worktree, so set that attestation only
    after validation succeeds and never trust an inherited value.
    """
    os.environ.pop("ISSUE84_WORKTREE_CLEAN", None)
    root = pilot.validate_issue84_source(path)
    os.environ["ISSUE84_WORKTREE_CLEAN"] = "1"
    return root


def run_preflight(
    *, issue84_source: Path, native_source: Path, stsr_source: Path,
) -> dict[str, Any]:
    identity = pilot.source_identity(require_clean=True)
    issue84_root = _validate_issue84_source_for_runtime(issue84_source)
    native_identity = _verify_native_source(native_source)
    stsr_identity = _verify_stsr_source(stsr_source)
    cache_report_path = DATA_ROOT / CACHE_EQUIVALENCE_RELATIVE_PATH
    cache_report = read_json(cache_report_path)
    cache_evidence = validate_cache_equivalence_report(
        cache_report, report_sha256=file_sha256(cache_report_path),
    )
    task_wall_estimate = whole_task_active_wall_estimate(cache_report)
    preflight_report = baseline_preflight.verify(
        DATA_ROOT, Path(native_source).resolve(strict=True), REPO,
    )
    if not preflight_report.get("verified"):
        raise RuntimeError("source/baseline preflight failed: " + "; ".join(preflight_report.get("errors", [])))
    readiness_root = DATA_ROOT / "issue85" / "artifacts" / "formal-readiness" / identity["package_commit"]
    readiness_root.mkdir(parents=True, exist_ok=True)
    package_root = DATA_ROOT / "issue85" / "artifacts" / "formal" / identity["package_commit"]
    package_root.mkdir(parents=True, exist_ok=True)
    backend, _issue84_driver = pilot._configure_runtime(issue84_root, readiness_root, identity)
    seed_table = build_formal_seed_table(backend, identity)
    table_path = package_root / "formal-seed-table.json"
    table_sha = _write_or_verify_once(table_path, seed_table)
    preflight_path = package_root / "source-preflight.json"
    if preflight_path.is_file():
        prior = read_json(preflight_path)
        if prior.get("checks") != preflight_report.get("checks") or prior.get("verified") is not True:
            raise RuntimeError("immutable source preflight evidence differs from the current locked inputs")
        preflight_sha = file_sha256(preflight_path)
    else:
        preflight_sha = _write_or_verify_once(preflight_path, preflight_report)
    plan = formal_plan()
    report = {
        "schema_id": "issue85-formal-readiness-v1",
        "status": "SOURCE_AND_SEED_PREFLIGHT_PASSED / FORMAL_NOT_STARTED",
        "package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "contract_sha256": file_sha256(CONTRACT_PATH),
        "analysis_source_sha256": file_sha256(STUDY / "formal_analysis.py"),
        "plan": plan,
        "issue84_source_commit": LOCK["baseline_A"]["issue84_final_accepted_source_commit"],
        "native_source": native_identity,
        "stsr_source": stsr_identity,
        "cache_equivalence_evidence": cache_evidence,
        "whole_task_active_wall_estimate": task_wall_estimate,
        "baseline_preflight_path": preflight_path.relative_to(DATA_ROOT).as_posix(),
        "baseline_preflight_sha256": preflight_sha,
        "formal_seed_table_path": table_path.relative_to(DATA_ROOT).as_posix(),
        "formal_seed_table_sha256": table_sha,
        "formal_games_started": 0,
        "formal_cohorts_changed": False,
        "execution_command_available": True,
        "execution_gate": "owner FORMAL_GO + numeric whole-task active-wall cap + Reviewer approval of exact commit",
        "entrypoint_modes": ["--plan", "--preflight", "--verify-bundle", "--execute"],
    }
    report_path = readiness_root / "formal-readiness.json"
    if report_path.is_file():
        previous = read_json(report_path)
        stable_previous = {key: value for key, value in previous.items() if key != "verified_utc"}
        if stable_previous != report or not isinstance(previous.get("verified_utc"), str):
            raise RuntimeError("immutable formal-readiness report differs from this source/preflight")
        report = previous
    else:
        report["verified_utc"] = datetime.now(timezone.utc).isoformat()
        _write_or_verify_once(report_path, report)
    return {**report, "readiness_path": str(report_path)}


def verify_dry_bundle(
    *, issue84_source: Path, native_source: Path, stsr_source: Path,
) -> dict[str, Any]:
    """Recheck frozen source, seed and analysis receipts without creating evidence or starting work."""
    identity = pilot.source_identity(require_clean=True)
    issue84_root = _validate_issue84_source_for_runtime(issue84_source)
    native_identity = _verify_native_source(native_source)
    stsr_identity = _verify_stsr_source(stsr_source)
    readiness_path = (
        DATA_ROOT / "issue85" / "artifacts" / "formal-readiness"
        / identity["package_commit"] / "formal-readiness.json"
    )
    package_root = DATA_ROOT / "issue85" / "artifacts" / "formal" / identity["package_commit"]
    seed_path = package_root / "formal-seed-table.json"
    preflight_path = package_root / "source-preflight.json"
    if not readiness_path.is_file() or not seed_path.is_file() or not preflight_path.is_file():
        raise RuntimeError("dry bundle check requires completed immutable source/seed preflight receipts")
    readiness = read_json(readiness_path)
    seed_table = read_json(seed_path)
    preflight = read_json(preflight_path)
    validate_formal_seed_table(seed_table, identity=identity)
    if (
        readiness.get("schema_id") != "issue85-formal-readiness-v1"
        or readiness.get("package_commit") != identity["package_commit"]
        or readiness.get("source_lock_sha256") != identity["source_lock_sha256"]
        or readiness.get("contract_sha256") != file_sha256(CONTRACT_PATH)
        or readiness.get("analysis_source_sha256") != file_sha256(STUDY / "formal_analysis.py")
        or readiness.get("formal_seed_table_sha256") != file_sha256(seed_path)
        or readiness.get("formal_games_started") != 0
        or readiness.get("formal_cohorts_changed") is not False
        or "NO_GO" not in str(readiness.get("whole_task_active_wall_estimate", {}).get("status", ""))
        or preflight.get("verified") is not True
        or readiness.get("baseline_preflight_path") != preflight_path.relative_to(DATA_ROOT).as_posix()
        or readiness.get("baseline_preflight_sha256") != file_sha256(preflight_path)
    ):
        raise RuntimeError("dry bundle check found a changed source, seed, analysis, or readiness receipt")
    cache_path = DATA_ROOT / CACHE_EQUIVALENCE_RELATIVE_PATH
    cache = validate_cache_equivalence_report(
        read_json(cache_path), report_sha256=file_sha256(cache_path),
    )
    if not cache.get("equivalence_scope", "").startswith("two sequential optimizer updates on retained 16-owner Pilot traces"):
        raise RuntimeError("dry bundle check found a broadened cache-equivalence scope")
    return {
        "schema_id": "issue85-formal-dry-bundle-check-v1",
        "status": "VERIFIED / NO GAMES OR UPDATES STARTED",
        "package_commit": identity["package_commit"],
        "source_lock_sha256": identity["source_lock_sha256"],
        "contract_sha256": file_sha256(CONTRACT_PATH),
        "readiness_sha256": file_sha256(readiness_path),
        "seed_table_sha256": file_sha256(seed_path),
        "actor_update_schedule_sha256": seed_table["actor_update_schedule_sha256"],
        "analysis_source_sha256": file_sha256(STUDY / "formal_analysis.py"),
        "issue84_source_commit": LOCK["baseline_A"]["issue84_final_accepted_source_commit"],
        "issue84_source_root": str(issue84_root),
        "native_source": native_identity,
        "stsr_source": stsr_identity,
        "cache_equivalence_evidence": cache,
        "formal_games_started": 0,
        "execution_authorized": False,
        "writes_performed": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan", action="store_true", help="print the registered formal cohorts without execution")
    modes.add_argument("--preflight", action="store_true", help="verify source bindings and freeze the formal seed table; never starts games")
    modes.add_argument("--verify-bundle", action="store_true", help="dry-check source, seed, receipts and frozen analysis; never starts games")
    modes.add_argument("--execute", action="store_true", help="run the registered formal study; requires exact owner GO/cap and Reviewer receipt")
    modes.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--issue84-source", type=Path, default=os.environ.get("ISSUE84_SOURCE_DIR"))
    parser.add_argument("--native-source", type=Path, default=os.environ.get("STS_LIGHTSPEED_SOURCE_DIR"))
    parser.add_argument("--stsr-source", type=Path, default=os.environ.get("STSRL_SOURCE_DIR"))
    parser.add_argument("--authorization-receipt", type=Path, help="owner-supplied, source/seed-bound FORMAL_GO and numeric whole-task cap receipt")
    args = parser.parse_args(argv)
    if args.plan:
        print(json.dumps(formal_plan(), sort_keys=True))
        return 0
    if args.worker is not None:
        from formal_execution import worker_main
        return worker_main(sys.modules[__name__], args.worker)
    missing = [
        name for name, value in (
            ("--issue84-source", args.issue84_source),
            ("--native-source", args.native_source),
            ("--stsr-source", args.stsr_source),
        ) if value is None
    ]
    if missing:
        parser.error("this mode requires " + ", ".join(missing))
    if args.verify_bundle:
        report = verify_dry_bundle(
            issue84_source=Path(args.issue84_source),
            native_source=Path(args.native_source),
            stsr_source=Path(args.stsr_source),
        )
    elif args.preflight:
        report = run_preflight(
            issue84_source=Path(args.issue84_source),
            native_source=Path(args.native_source),
            stsr_source=Path(args.stsr_source),
        )
    else:
        if args.authorization_receipt is None:
            parser.error("--execute requires --authorization-receipt; no game or update was started")
        from formal_execution import execute_formal
        report = execute_formal(
            sys.modules[__name__],
            issue84_source=Path(args.issue84_source),
            native_source=Path(args.native_source),
            stsr_source=Path(args.stsr_source),
            authorization_receipt=Path(args.authorization_receipt),
        )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
