#!/usr/bin/env python3
"""Rebuild Issue 75 Stage 1 cost projections from retained Stage 0 receipts only."""
from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve()
WORKTREE = SCRIPT.parents[3]
STUDY = SCRIPT.parents[1]
ATTEMPT = STUDY / "stage0/attempts/source-c77d6424-b3ce3d33-pass1"
RECEIPTS = ATTEMPT / "receipts"
ROOTS = RECEIPTS / "roots"
RUNTIME = ATTEMPT / "native-output"
REPORT = RECEIPTS / "stage0-report.json"
SOURCE = "c77d64243320639bf156330760d511d2a13eee85"
SNAPSHOT = "7f7538a706710626740cdb635d3711e69eac900f"
LOCK = "b3ce3d33c27ec392f6d88151bce3c86a8fdce8c9dc0152d417069441f7140d38"
BRANCH = "codex/issue-75-stage0-repair-20261026"
BOOTSTRAPS = 20_000
BOOTSTRAP_SEED = 75_010_2026  # Analysis RNG only; never a game seed.
MARGIN = 0.25
REVIEWER_RESERVE = 10_000.0
ROUNDING = 10_000.0


def stop(message: str) -> None:
    raise SystemExit("cost-model verification failed: " + message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    return digest(path.read_bytes().replace(b"\r\n", b"\n"))


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        stop(f"cannot parse {path.relative_to(WORKTREE)}: {exc}")


def quantile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        stop("empty quantile sample")
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def stats(values: list[float]) -> dict:
    if not values:
        stop("empty summary sample")
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "sample_sd": statistics.stdev(values) if len(values) > 1 else 0.0,
        "median": statistics.median(values),
        "p2_5_nearest_rank": quantile(values, 0.025),
        "p97_5_nearest_rank": quantile(values, 0.975),
        "p95_nearest_rank": quantile(values, 0.95),
        "max": max(values),
    }


def main() -> dict:
    lock_doc = load(STUDY / "source-lock.json")
    lock_digest = file_digest(STUDY / "source-lock.json")
    if lock_digest != LOCK:
        stop(f"source-lock digest changed: {lock_digest}")
    if lock_doc.get("source_branch") != BRANCH:
        stop("source branch changed")
    ancestry = subprocess.run(["git", "-C", str(WORKTREE), "merge-base", "--is-ancestor", SOURCE, SNAPSHOT], check=False)
    if ancestry.returncode != 0:
        stop("reviewed source is not an ancestor of the evidence snapshot")
    if not lock_doc.get("stage0_only") or lock_doc.get("formal_training_enabled") is not False:
        stop("Stage 0-only lock changed")
    if lock_doc.get("formal_evaluation_enabled") is not False or lock_doc.get("formal_seed_table_present") is not False:
        stop("formal Stage 1 was enabled or gained a seed table")
    locked = lock_doc.get("files_sha256_lf", {})
    if len(locked) != 12:
        stop("expected 12 source-locked files")
    bad = [name for name, want in locked.items() if not (STUDY / name).is_file() or file_digest(STUDY / name) != want]
    if bad:
        stop("locked file mismatch: " + ", ".join(bad))

    config = load(STUDY / "config.json")
    report = load(REPORT)
    proposal = config["formal_stage1_proposal_only"]
    search = config["search"]
    if report.get("status") != "STAGE0_NONFORMAL_REVIEWABLE_COMPLETE":
        stop("Stage 0 is not reviewable-complete")
    if report.get("source_commit") != SOURCE or report.get("source_lock_sha256_lf") != LOCK:
        stop("Stage 0 report lineage mismatch")
    for key in ("formal_training_seed_consumed", "formal_evaluation_seed_consumed", "formal_seed_table_present"):
        if report.get(key) is not False:
            stop("Stage 0 formal state changed: " + key)
    if proposal.get("execution_enabled") is not False or proposal.get("planner_activation_required") is not True:
        stop("formal Stage 1 is not still disabled pending Planner activation")
    if proposal.get("formal_seed_table_included") is not False:
        stop("formal seed table was added to proposal")
    if len(report.get("root_results", [])) != 8 or report.get("completed_later_mutation_trajectories") != 64:
        stop("expected the reviewed eight-root, 64-mutation Stage 0 receipt")

    json_receipts = sorted(ATTEMPT.rglob("*.json"))
    if len(json_receipts) != 628:
        stop(f"expected 628 JSON receipts, found {len(json_receipts)}")
    for path in json_receipts:
        obj = load(path)
        if not isinstance(obj, dict):
            stop("non-object receipt: " + str(path.relative_to(WORKTREE)))
        if obj.get("source_commit") not in (None, SOURCE) or obj.get("source_lock_sha256_lf") not in (None, LOCK):
            stop("receipt lineage mismatch: " + str(path.relative_to(WORKTREE)))
        if any(obj.get(k) is True for k in ("formal_training_seed_consumed", "formal_evaluation_seed_consumed")):
            stop("formal seed marked consumed: " + str(path.relative_to(WORKTREE)))
        if any(obj.get(k) is True for k in ("native_checkpoint_persisted", "privileged_projection_persisted")):
            stop("unexpected private artifact persisted: " + str(path.relative_to(WORKTREE)))
    runtime_logs = sorted(RUNTIME.rglob("runtime-metrics.jsonl"))
    if len(runtime_logs) != 272:
        stop(f"expected 272 runtime logs, found {len(runtime_logs)}")
    runtime_records = 0
    for path in runtime_logs:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except Exception as exc:
                stop(f"bad runtime record {path.relative_to(WORKTREE)}:{n}: {exc}")
            runtime_records += 1

    prefix_by_root = {}
    base_by_root = {}
    for path in ROOTS.glob("*-prefix-result.json"):
        obj = load(path)
        prefix_by_root[obj["root_id"]] = obj
    for path in ROOTS.glob("*-base-result.json"):
        obj = load(path)
        base_by_root[path.name.removesuffix("-base-result.json")] = obj
    if len(prefix_by_root) != 8 or len(base_by_root) != 8:
        stop("expected one prefix and base result per root")

    profiles, r_attempts, j_attempts = [], [], []
    for root in report["root_results"]:
        rid = root["root_id"]
        if root.get("source_commit") != SOURCE or root.get("source_lock_sha256_lf") != LOCK:
            stop("root source mismatch: " + rid)
        prefix, base = prefix_by_root.get(rid), base_by_root.get(rid)
        if prefix is None or prefix.get("status") != "SOURCE_ROOT_MATCHED":
            stop("missing/mismatched source prefix: " + rid)
        if base is None or base.get("status") != "TERMINAL_COMPLETE" or base.get("complete") is not True:
            stop("missing/incomplete terminal base: " + rid)
        game = float(prefix["cpu_seconds"]) + float(base["cpu_seconds"])
        arm_cost = {}
        for arm, attempts in (("R", r_attempts), ("J", j_attempts)):
            summary = root[arm]
            vals = [float(v) for v in summary["attempt_cpu_seconds"]]
            attempts.extend(vals)
            if summary.get("arm") != arm or summary.get("started_attempts") != 16 or summary.get("completed_trajectories") != 16 or len(vals) != 16:
                stop(f"{rid}/{arm} was not 16/16 completed")
            if sum(vals) > float(summary["cpu_seconds"]) + 1e-6:
                stop(f"{rid}/{arm} attempt CPU exceeds invocation CPU")
            if float(summary["cpu_seconds"]) > float(search["search_invocation_cpu_cap_seconds"]):
                stop(f"{rid}/{arm} exceeded search CPU cap")
            expected_mutations = 0 if arm == "R" else 8
            if summary.get("mutation_attempts_started") != expected_mutations:
                stop(f"{rid}/{arm} mutation count mismatch")
            arm_cost[arm] = float(summary["cpu_seconds"])
        residual = float(root["worker_process_cpu_seconds"]) - game - arm_cost["R"] - arm_cost["J"]
        if residual < -1e-5:
            stop("negative worker residual: " + rid)
        profiles.append({"root_id": rid, "game": game, "overhead": max(0.0, residual), "R": arm_cost["R"], "J": arm_cost["J"]})
    if len(r_attempts) != 128 or len(j_attempts) != 128:
        stop("expected 128 attempt CPU samples per arm")

    training = int(proposal["training_owner_count"])
    eval_owners = int(proposal["held_out_owner_count"])
    held_arms = list(proposal["held_out_arms"])
    blocks = int(proposal["training_blocks"])
    block_owners = int(proposal["owners_per_block"])
    batch = int(config["learning"]["optimizer"]["formal_batch_owners"])
    updates = blocks * int(proposal["adamw_steps_per_block"])
    stage0_batch = int(config["learning"]["optimizer"]["stage0_diagnostic_batch_size"])
    if training != blocks * block_owners or held_arms != ["P0", "P1", "R", "J", "E"]:
        stop("registered population/arm arithmetic changed")
    training_calls = training * int(search["training_base_game_root_opportunity_cap"])
    eval_r = eval_owners * int(search["evaluation_rj_root_opportunity_cap"])
    eval_j = eval_r
    j_calls, r_calls = training_calls + eval_j, eval_r
    calls = j_calls + r_calls
    cap = float(search["search_invocation_cpu_cap_seconds"])
    game_paths = training + eval_owners * len(held_arms)
    attempt_cap = int(search["search_invocation_started_attempt_cap"])
    attempts_cap = calls * attempt_cap

    games = [p["game"] for p in profiles]
    overheads = [p["overhead"] for p in profiles]
    r_calls_cpu = [p["R"] for p in profiles]
    j_calls_cpu = [p["J"] for p in profiles]
    expected_search = r_calls * statistics.fmean(r_calls_cpu) + j_calls * statistics.fmean(j_calls_cpu)

    # Stage 0 reports controller CPU only as one aggregate (eight roots + one batch-8 update);
    # it does not isolate optimizer timing. Scale that whole aggregate by update count and
    # batch ratio, or by roots, whichever is larger. This is a planning allocation, not a bound.
    controller_cpu = float(report["resource_accounting"]["controller_cpu_seconds"])
    controller_guard_updates = controller_cpu * updates * (batch / stage0_batch)
    controller_guard_roots = (controller_cpu / len(profiles)) * game_paths
    controller_guard = max(controller_guard_updates, controller_guard_roots)
    mean_game, mean_overhead = statistics.fmean(games), statistics.fmean(overheads)
    expected_game = game_paths * mean_game
    expected_overhead = game_paths * mean_overhead
    expected_builder = expected_search + expected_game + expected_overhead + controller_guard

    rng = random.Random(BOOTSTRAP_SEED)
    bs_search, bs_nonsearch, bs_total = [], [], []
    for _ in range(BOOTSTRAPS):
        ix = [rng.randrange(len(profiles)) for _ in profiles]
        r_mean = statistics.fmean(r_calls_cpu[i] for i in ix)
        j_mean = statistics.fmean(j_calls_cpu[i] for i in ix)
        g_mean = statistics.fmean(games[i] for i in ix)
        o_mean = statistics.fmean(overheads[i] for i in ix)
        sc = r_calls * r_mean + j_calls * j_mean
        nc = game_paths * (g_mean + o_mean) + controller_guard
        bs_search.append(sc)
        bs_nonsearch.append(nc)
        bs_total.append(sc + nc)

    search_hard_cap = calls * cap
    cap_basis = search_hard_cap + quantile(bs_nonsearch, 0.95)
    engineering = cap_basis * MARGIN
    builder_pre_round = cap_basis + engineering
    builder_cap = math.ceil(builder_pre_round / ROUNDING) * ROUNDING
    all_in = builder_cap + REVIEWER_RESERVE
    resources = report["resource_accounting"]

    model = {
        "schema_id": "issue75-stage1-cost-model-v1",
        "status": "PLANNING_ONLY_REQUIRES_SEPARATE_PLANNER_ACTIVATION",
        "disposition": "STUDY_ONLY",
        "input_lineage": {
            "stage0_evidence_snapshot_sha": SNAPSHOT,
            "stage0_source_sha": SOURCE,
            "stage0_source_branch": BRANCH,
            "source_lock_sha256_lf": LOCK,
            "locked_file_count": len(locked),
            "stage0_report_sha256": digest(REPORT.read_bytes()),
            "json_receipt_count_verified": len(json_receipts),
            "runtime_log_count_verified": len(runtime_logs),
            "runtime_record_count_verified": runtime_records,
            "formal_seeds_consumed": False,
            "formal_seed_table_present": False,
        },
        "registered_workload": {
            "training_owners": training,
            "training_blocks": blocks,
            "owners_per_block": block_owners,
            "optimizer_batch_owners": batch,
            "adamw_updates_total": updates,
            "heldout_owners": eval_owners,
            "heldout_arms": held_arms,
            "heldout_scenarios": eval_owners * len(held_arms),
            "complete_game_path_analogs": game_paths,
            "training_j_calls": training_calls,
            "evaluation_j_calls": eval_j,
            "evaluation_r_calls": eval_r,
            "total_j_calls": j_calls,
            "total_r_calls": r_calls,
            "total_search_calls": calls,
            "maximum_started_search_attempts": attempts_cap,
            "search_cpu_cap_per_invocation": cap,
            "maximum_search_cpu_seconds": search_hard_cap,
            "stage1_execution_enabled": proposal["execution_enabled"],
            "planner_activation_required": proposal["planner_activation_required"],
            "formal_seed_table_included": proposal["formal_seed_table_included"],
        },
        "measured_stage0": {
            "root_game_path_proxy": stats(games),
            "root_worker_residual_overhead": stats(overheads),
            "r_search_invocation": stats(r_calls_cpu),
            "j_search_invocation": stats(j_calls_cpu),
            "r_individual_attempt": stats(r_attempts),
            "j_individual_attempt": stats(j_attempts),
            "root_count": len(profiles),
            "controller_cpu_total": controller_cpu,
            "controller_cpu_scope": "Whole Stage 0 controller; not isolated optimizer-step time.",
            "diagnostic_updates": report["diagnostic_training"]["optimizer_steps"],
            "diagnostic_batch_owners": stage0_batch,
            "diagnostic_probe_states": report["diagnostic_training"]["probe_diagnostics"]["probe_state_count"],
        },
        "model_method": {
            "point_estimate": "Eight paired root means scaled to registered Stage 1 counts.",
            "bootstrap": f"{BOOTSTRAPS} resamples of eight paired root profiles with replacement; nearest-rank quantiles.",
            "bootstrap_rng_seed": BOOTSTRAP_SEED,
            "bootstrap_rng_is_game_seed": False,
            "bootstrap_quantiles_are_predictive_guarantees": False,
            "game_path_proxy": "Historical-source prefix replay CPU + Stage 0 pi0 terminal continuation CPU from each fixed historical checkpoint.",
            "worker_residual": "Worker process CPU minus prefix, base continuation, R invocation and J invocation CPU.",
            "controller_training_allocation": "max(aggregate Stage 0 controller CPU x 256 updates x (32/8) batch ratio, aggregate controller CPU / 8 roots x 992 game-path analogs); not a measured mean or formal upper bound.",
            "engineering_margin": "25 percent on search hard cap plus root-bootstrap p95 non-search CPU; an explicit planning margin.",
            "reviewer_reserve": "Protected 10,000 CPU-s, outside Builder ceiling and not included as expected experiment spend.",
        },
        "stage1_builder_projection_cpu_seconds": {
            "expected_search": expected_search,
            "search_root_bootstrap": stats(bs_search),
            "search_hard_cap": search_hard_cap,
            "expected_game_path_proxy": expected_game,
            "expected_root_worker_residual_overhead": expected_overhead,
            "controller_training_allocation_guard": controller_guard,
            "expected_total": expected_builder,
            "total_root_bootstrap": stats(bs_total),
            "total_root_bootstrap_p95": quantile(bs_total, 0.95),
            "nonsearch_root_bootstrap_p95": quantile(bs_nonsearch, 0.95),
            "cap_basis_before_margin": cap_basis,
            "engineering_margin_fraction": MARGIN,
            "engineering_allowance": engineering,
            "rounding_remainder": builder_cap - builder_pre_round,
            "proposed_builder_hard_cap": builder_cap,
            "protected_reviewer_reserve": REVIEWER_RESERVE,
            "proposed_all_in_hard_cap": all_in,
            "expected_plus_full_reviewer_reserve": expected_builder + REVIEWER_RESERVE,
        },
        "stage0_cumulative_context": {
            "source_handoff_comment_id": 6402937706,
            "builder_cpu_cumulative_including_allowance": 14213.839745,
            "builder_cpu_hard_cap": 50000,
            "native_cpu_cumulative": 4271.675870,
            "native_cpu_hard_cap": 30000,
            "reviewer_protected_reserve": 10000,
            "formal_stage1_reserve": 0,
            "ledger_carryover_to_stage1": False,
        },        "stage0_resource_context": {
            "builder_attempt_charged": resources["builder_cpu_charged_seconds"],
            "native_worker_charged": resources["native_worker_cpu_charged_seconds"],
            "controller_cpu": resources["controller_cpu_seconds"],
            "reviewer_protected_reserve": resources["reviewer_cpu_protected_seconds"],
            "formal_stage1_reserve": resources["formal_stage1_cpu_reserved_seconds"],
        },
        "limitations": [
            "Only eight historical Issue 73 checkpoint roots were measured; they are not standard-start pi0 or Stage 1 policy games. Prefix plus continuation is a hybrid CPU proxy, not a Stage 1 outcome.",
            "Eight root clusters are too few for predictive tail guarantees. Bootstrap quantiles describe resampling uncertainty over those observed roots only.",
            "Search means use 16 completed Stage 0 continuations per invocation. Stage 1 hard caps, not these means, define the maximum search cost.",
            "Aggregate Stage 0 controller CPU includes one batch-8 diagnostic update but is not an isolated optimizer timer. Its scaled term is a planning guard, not a proven bound.",
            "Model excludes any unregistered dependency rebuild, portability repair, checkpoint recovery, source drift or cost beyond the explicit margin. Any activation needs a project-level watchdog and safe stop at the aggregate cap.",
            "This report is not a scientific outcome or Stage 1 authorization. No formal Stage 1 seeds, games, training or simulator runs are performed by this analysis.",
        ],
    }

    out = STUDY / "planning"
    out.mkdir(exist_ok=True)
    (out / "stage1-cost-model.json").write_text(json.dumps(model, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "stage1-cost-model.md").write_text(render_markdown(model), encoding="utf-8")
    return model


def fmt(value: float) -> str:
    return f"{value:,.2f}"


def render_markdown(m: dict) -> str:
    w, s, c, meas = m["registered_workload"], m["stage1_builder_projection_cpu_seconds"], m["model_method"], m["measured_stage0"]
    lines = [
        "# Issue 75 Stage 1 cost model (planning only)",
        "",
        f"**Status:** {m['status']}<br>",
        f"**Disposition:** {m['disposition']}<br>",
        f"**Stage 0 evidence snapshot:** {m['input_lineage']['stage0_evidence_snapshot_sha']}<br>",
        f"**Measured source / lock:** {m['input_lineage']['stage0_source_sha']} / {m['input_lineage']['source_lock_sha256_lf']}",
        "",
        "This receipt-derived estimate does not activate Stage 1. Formal training/evaluation stay disabled; no formal seed table is added.",
        "",
        "## Registered workload",
        "",
        f"- Training: {w['training_owners']} owners; {w['training_blocks']} blocks × {w['owners_per_block']}; {w['adamw_updates_total']} AdamW updates.",
        f"- Held out: {w['heldout_owners']} owners × {len(w['heldout_arms'])} arms = {w['heldout_scenarios']} scenarios.",
        f"- Complete-game path analogs: {w['complete_game_path_analogs']}; searches: {w['total_search_calls']:,} ({w['total_j_calls']:,} J, {w['total_r_calls']:,} R).",
        f"- Search hard cap: {w['maximum_search_cpu_seconds']:,.0f} CPU-s, from {w['total_search_calls']:,} calls × {w['search_cpu_cap_per_invocation']:.0f} CPU-s.",
        "",
        "## Measured Stage 0 profiles",
        "",
        "| Component | n | Mean CPU-s | Sample SD | Median | p95 | Max |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, key in [
        ("Hybrid game-path proxy", "root_game_path_proxy"),
        ("Root worker residual", "root_worker_residual_overhead"),
        ("R search invocation", "r_search_invocation"),
        ("J search invocation", "j_search_invocation"),
        ("R individual attempt", "r_individual_attempt"),
        ("J individual attempt", "j_individual_attempt"),
    ]:
        row = meas[key]
        lines.append(f"| {label} | {row['n']} | {fmt(row['mean'])} | {fmt(row['sample_sd'])} | {fmt(row['median'])} | {fmt(row['p95_nearest_rank'])} | {fmt(row['max'])} |")
    lines += [
        "",
        "The game-path proxy is historical-source prefix replay plus the Stage 0 pi0 terminal continuation from the fixed checkpoint. It is not a standard-start Stage 1 game.",
        "",
        "Existing Stage 0 cumulative ledger from handoff #6402937706: 14,213.84/50,000 Builder CPU-s and 4,271.68/30,000 native CPU-s; Reviewer reserve 10,000; Stage 1 reserve 0. This proposal is a separate ledger and carries no credit forward.",
        "",
        "## Stage 1 projection",
        "",
        "| Builder cost item | CPU-s |",
        "|---|---:|",
        f"| Expected search (observed means × calls) | {fmt(s['expected_search'])} |",
        f"| Search maximum (registered caps) | {fmt(s['search_hard_cap'])} |",
        f"| Expected hybrid game-path proxy | {fmt(s['expected_game_path_proxy'])} |",
        f"| Expected worker residual extrapolation | {fmt(s['expected_root_worker_residual_overhead'])} |",
        f"| Controller/training allocation guard | {fmt(s['controller_training_allocation_guard'])} |",
        f"| Modeled expected Builder CPU | **{fmt(s['expected_total'])}** |",
        f"| Root-bootstrap modeled total p95 | {fmt(s['total_root_bootstrap_p95'])} |",
        f"| Root-bootstrap total central 95% interval | {fmt(s['total_root_bootstrap']['p2_5_nearest_rank'])}–{fmt(s['total_root_bootstrap']['p97_5_nearest_rank'])} |",
        f"| Root-bootstrap non-search p95 | {fmt(s['nonsearch_root_bootstrap_p95'])} |",
        "",
        f"Tail method: {c['bootstrap']} Analysis RNG seed {m['model_method']['bootstrap_rng_seed']} is not a game seed. Quantiles are not predictive guarantees.",
        "",
        "## Proposed budget for a separate Planner decision",
        "",
        f"- Search hard cap + non-search bootstrap p95: {fmt(s['cap_basis_before_margin'])} CPU-s.",
        f"- Explicit 25% engineering margin: {fmt(s['engineering_allowance'])} CPU-s (not a measurement).",
        f"- Proposed Builder hard ceiling, rounded up to 10,000: **{fmt(s['proposed_builder_hard_cap'])} CPU-s**.",
        f"- Protected Reviewer reserve: **{fmt(s['protected_reviewer_reserve'])} CPU-s**.",
        f"- Proposed all-in hard ceiling: **{fmt(s['proposed_all_in_hard_cap'])} CPU-s**.",
        "",
        "These Stage 1 ceilings are proposals only, not reserved or authorized. Separate activation must register the budget and implement an aggregate Builder watchdog that stops safely at the cap. Reviewer reserve is protected and is not included in expected experiment spend.",
        "",
        "## Limitations",
        "",
    ]
    lines.extend("- " + item for item in m["limitations"])
    lines += [
        "",
        "Rebuild: python -B studies/issue-75-no-expert-oracle/planning/build_stage1_cost_model.py. The script verifies the source lock, all retained JSON receipts, runtime logs and formal-seed-disabled state before producing this report.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    model = main()
    projection = model["stage1_builder_projection_cpu_seconds"]
    print(json.dumps({
        "expected_builder_cpu_seconds": projection["expected_total"],
        "total_bootstrap_p95_cpu_seconds": projection["total_root_bootstrap_p95"],
        "nonsearch_bootstrap_p95_cpu_seconds": projection["nonsearch_root_bootstrap_p95"],
        "proposed_builder_hard_cap_cpu_seconds": projection["proposed_builder_hard_cap"],
        "reviewer_reserve_cpu_seconds": projection["protected_reviewer_reserve"],
        "proposed_all_in_hard_cap_cpu_seconds": projection["proposed_all_in_hard_cap"],
    }, indent=2, sort_keys=True))
