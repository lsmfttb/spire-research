#!/usr/bin/env python3
"""Evidence-only append-only Issue 84 pilot wall reforecast. Reads existing receipts/checkpoints only."""
from __future__ import annotations
import argparse, hashlib, json, math, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PACKAGE = "23b21a1205426e2a26ab65bc8535bcf48b89dcf0"
LOCK = "cc75d99ff9f158f64dca831a69128cae32448a44469aa0b8eea78a3c1ce50496"
REPORT_SHA = "4ffac689d5dabb3eb8783db8393351f8397c8bb597be327b12616b6779d04cd4"
PROGRESS_SHA = "bd84dd2c6dc404b1e80de318ac65bc7c246849fa06326a45c443d5454ba46eb1"
FORECAST_SHA = "2ec3f9fe72ebcfc315c132f10b9e024b9c60a03a0f4c86d28b5d3a25fde29b15"
ARMS = ("A", "B", "C", "D", "E", "F")
EVAL_ARMS = ("PARENT", *ARMS)
PREDICTOR_ARMS = ("D", "E", "F")
PILOT = Path("pilot") / PACKAGE
TREF = 36.6474609375
TRAIN_GAMES, EVAL_GAMES = 12_288, 3_456
TRAIN_BLOCKS, EVAL_INVOCATIONS = 96, 26
PRIOR_OWNERS_PER_MODEL = 15_360


def stop(message):
    raise RuntimeError(message)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def load(path):
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        stop(f"expected JSON object: {path}")
    return obj


def iso(value):
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def union_seconds(intervals):
    spans = sorted((s, e) for s, e in intervals if e > s)
    if not spans:
        return 0.0
    start, end = spans[0]
    result = 0.0
    for s, e in spans[1:]:
        if s <= end:
            end = max(end, e)
        else:
            result += (end - start).total_seconds()
            start, end = s, e
    return result + (end - start).total_seconds()


def secs(value):
    if not math.isfinite(value) or value < 0:
        stop(f"invalid duration: {value}")
    return round(value, 6)


def game_receipts(root, token, expected):
    intervals, receipt_hashes = [], []
    for started_path in root.rglob("started.json"):
        if token not in str(started_path):
            continue
        started = load(started_path)
        finished_path = started_path.parent / "finished.json"
        if not finished_path.is_file():
            stop(f"missing game finish receipt: {started_path}")
        finished = load(finished_path)
        s, e = iso(started["started_utc"]), iso(finished["finished_utc"])
        if e <= s or finished.get("status") != "SUCCEEDED":
            stop(f"unexpected complete-game attempt: {started_path}")
        intervals.append((s, e))
        receipt_hashes.extend((digest(started_path), digest(finished_path)))
    if len(intervals) != expected:
        stop(f"{root}: expected {expected} completed {token} receipts, found {len(intervals)}")
    return intervals, receipt_hashes


def session_summary(artifacts):
    found, hashes, declared = [], [], 0.0
    inferred = 0
    for path in sorted((artifacts / "wall-sessions").glob("issue84-task-*.json")):
        x = load(path)
        if x.get("schema_id") != "issue79-active-wall-session-v1" or x.get("mode") != "issue84-task":
            continue
        s = iso(x["session_start_utc"])
        duration = float(x["session_seconds"])
        if x.get("session_end_utc"):
            e = iso(x["session_end_utc"])
        else:
            e, inferred = s + timedelta(seconds=duration), inferred + 1
        found.append((s, e))
        hashes.append(digest(path))
        declared += duration
    if not found:
        stop("no standard Issue 84 active wall session receipts")
    return {
        "schema_id": "issue79-active-wall-session-v1", "mode": "issue84-task",
        "receipt_count": len(found), "declared_seconds_sum": secs(declared),
        "timestamp_union_seconds": secs(union_seconds(found)),
        "inferred_end_count": inferred, "receipt_sha256": sorted(hashes),
    }


def pointer(artifacts, current, arm, kind):
    actor = kind == "actor"
    name = "pilot-update-carry-forward.json" if actor else "pilot-predictor-fit-carry-forward.json"
    path = current / arm / "diagnostics" / name
    x = load(path)
    if x.get("arm") != arm or x.get("current_source_lock_sha256") != LOCK:
        stop(f"bad {kind} pointer for {arm}")
    root = artifacts / Path(x["source_artifact_root"])
    lockfile = artifacts / "source-lock-history" / f"{x['source_package_commit']}-{x['source_lock_sha256']}.json"
    if not root.is_dir() or not lockfile.is_file() or digest(lockfile) != x["source_lock_sha256"]:
        stop(f"missing source or source-lock archive for {arm} {kind}")
    return x, path, root


def checkpoints(artifacts, current):
    files, pointer_hashes = [], []
    for arm in ARMS:
        p, pp, root = pointer(artifacts, current, arm, "actor")
        pointer_hashes.append(digest(pp))
        for role, rel, field in (
            ("actor_model", "models/pi13.pt", "model_sha256"),
            ("actor_optimizer", "models/blocks/pilot-block-00-step.pt", "optimizer_checkpoint_sha256"),
        ):
            path = root / rel
            if not path.is_file() or digest(path) != p[field]:
                stop(f"{arm} {role} missing/hash changed: {path}")
            files.append({"arm": arm, "role": role, "path": (Path(p["source_artifact_root"]) / rel).as_posix(),
                          "sha256": p[field], "source_pointer_sha256": digest(pp),
                          "source_package_commit": p["source_package_commit"], "source_lock_sha256": p["source_lock_sha256"],
                          "retained_as_provenance": True, "eligible_for_formal_init": False, "physically_deleted": False})
    for arm in PREDICTOR_ARMS:
        p, pp, root = pointer(artifacts, current, arm, "predictor")
        pointer_hashes.append(digest(pp))
        path = root / "models/predictors/pilot-after-block-00.pt"
        if not path.is_file() or digest(path) != p["checkpoint_sha256"]:
            stop(f"{arm} predictor checkpoint missing/hash changed: {path}")
        files.append({"arm": arm, "role": "predictor_state",
                      "path": (Path(p["source_artifact_root"]) / "models/predictors/pilot-after-block-00.pt").as_posix(),
                      "sha256": p["checkpoint_sha256"], "source_pointer_sha256": digest(pp),
                      "source_package_commit": p["source_package_commit"], "source_lock_sha256": p["source_lock_sha256"],
                      "retained_as_provenance": True, "eligible_for_formal_init": False, "physically_deleted": False})
    local = [f for a in ARMS for f in (current / a / "models").rglob("*")
             if f.is_file() and f.suffix.lower() in {".pt", ".pth", ".ckpt"}]
    if len(files) != 15 or len({x["sha256"] for x in files}) != 15 or local:
        stop(f"expected 15 unique retained checkpoints and zero current-package copies; got {len(files)}, {len(local)}")
    return files, pointer_hashes


def failure_evidence(artifacts, current):
    cpath = current / "C/execution-lineage/TRAIN-8480022-C.json"
    c = load(cpath)
    croot = artifacts / Path(c["source_artifact_root"]) / "units/TRAIN-8480022-C"
    cints, attempt_hashes = [], []
    for n in range(1, 19):
        d = croot / f"attempt-{n:04d}"
        a, b = load(d / "started.json"), load(d / "finished.json")
        if b.get("status") != "FAILED":
            stop(f"C attempt {n} no longer has its failed receipt")
        cints.append((iso(a["started_utc"]), iso(b["finished_utc"])))
        attempt_hashes.extend((digest(d / "started.json"), digest(d / "finished.json")))
    if c.get("attempt_19_active_wall_tail") != "UNKNOWN" or c.get("prior_attempt_count") != 19:
        stop("C attempt-19 UNKNOWN tail marker changed")
    parentpath = current / "lineage/previous-parent-prestart-failures.json"
    parent = load(parentpath)
    proot = artifacts / "pilot" / parent["source_package_commit"] / "PARENT/units"
    pints, parent_attempt_hashes = [], []
    for row in parent["units"]:
        unit = row["unit_id"].replace("EVAL:", "EVAL-").replace(":PARENT", "-PARENT")
        d = proot / unit / "attempt-0001"
        a, b = load(d / "started.json"), load(d / "finished.json")
        if b.get("status") != "FAILED" or row.get("game_created") is not False:
            stop(f"PARENT pre-start receipt changed: {unit}")
        pints.append((iso(a["started_utc"]), iso(b["finished_utc"])))
        parent_attempt_hashes.extend((digest(d / "started.json"), digest(d / "finished.json")))
    corrpath = artifacts / "wall-sessions/issue84-task-repair-accounting-correction-c1f31e4.json"
    corr = load(corrpath)
    return {
        "C_deterministic_failures": {"count": len(cints), "attempts": "1-18",
            "failure_interval_union_seconds": secs(union_seconds(cints)),
            "campaign_envelope_seconds": secs((max(e for _, e in cints)-min(s for s, _ in cints)).total_seconds()),
            "treatment": "pilot-only recovery, never scaled by games/blocks"},
        "C_attempt_19_tail": {"status": "UNKNOWN", "treatment": "not zeroed and no duration invented"},
        "PARENT_prestart_failures": {"count": len(pints), "game_created": False,
            "interval_union_seconds": secs(union_seconds(pints)),
            "treatment": "pilot-only recovery, never scaled by games/blocks"},
        "prior_accounting_correction": {"sha256": digest(corrpath),
            "net_seconds_already_applied_once": corr["additional_seconds_applied"],
            "treatment": "already included once in pilot cumulative wall"},
    }, [digest(cpath), digest(parentpath), digest(corrpath), *attempt_hashes, *parent_attempt_hashes]


def predictor_projection(progress):
    fits = progress["predictor_fits"]
    if set(fits) != set(PREDICTOR_ARMS):
        stop("pilot predictor fit coverage changed")
    fit_seconds = cache_seconds = fit_rows = cache_rows = 0.0
    rows_by_arm = {}
    for arm in PREDICTOR_ARMS:
        fit = fits[arm]["fit"]
        v = fit["value"]
        fit_seconds += float(v["elapsed_seconds"])
        fit_rows += int(v["examples"])
        cache_seconds += float(fit["feature_cache_build_elapsed_seconds"])
        cache_rows += int(v["examples"])
        rows_by_arm[arm] = {"V_rows": int(v["examples"])}
        q = fit.get("action_value")
        if arm == "E":
            if not q:
                stop("E pilot Q fit is missing")
            fit_seconds += float(q["elapsed_seconds"])
            fit_rows += int(q["examples"])
            rows_by_arm[arm]["Q_rows"] = int(q["examples"])
        elif q is not None:
            stop(f"unexpected Q fit for {arm}")
    formal_fit_rows = round(TREF * PRIOR_OWNERS_PER_MODEL * 4)
    formal_cache_rows = round(TREF * PRIOR_OWNERS_PER_MODEL * 3)
    return {
        "pilot_fit_rows_by_arm": rows_by_arm,
        "pilot_fit_rows": int(fit_rows), "pilot_cache_rows": int(cache_rows),
        "pilot_fit_seconds_excluding_cache": secs(fit_seconds),
        "pilot_cache_seconds": secs(cache_seconds),
        "formal_fit_rows": formal_fit_rows, "formal_cache_rows": formal_cache_rows,
        "formal_fit_seconds": secs(fit_seconds * formal_fit_rows / fit_rows),
        "formal_cache_seconds": secs(cache_seconds * formal_cache_rows / cache_rows),
        "formal_fit_plus_cache_seconds": secs(fit_seconds * formal_fit_rows / fit_rows +
                                              cache_seconds * formal_cache_rows / cache_rows),
        "scaling": "linear decision rows; 15,360 cumulative prior-owner presentations per model; four fits D-V/E-V/E-Q/F-V; three shared D/E/F feature caches",
    }


def produce(repo, artifacts):
    current = artifacts / PILOT
    pp, rp = current / "pilot-progress.json", current / "pilot-report.json"
    fp, cp = current / "task-wall-forecast.json", artifacts / "active-wall-issue84-task.json"
    lockpath = repo / "studies/issue-84-six-arm/source-lock.json"
    for path, expected in ((pp, PROGRESS_SHA), (rp, REPORT_SHA), (fp, FORECAST_SHA)):
        if not path.is_file() or digest(path) != expected:
            stop(f"locked original receipt missing or changed: {path}")
    if digest(lockpath) != LOCK:
        stop("registered Issue 84 source lock changed")
    progress, report, old, clock = load(pp), load(rp), load(fp), load(cp)
    if progress.get("source_lock_sha256") != LOCK or report.get("source_lock_sha256") != LOCK:
        stop("pilot progress/report source lock changed")
    if report.get("package_commit") != PACKAGE or report.get("task_wall_forecast_sha256") != FORECAST_SHA:
        stop("pilot report no longer binds the locked package and original forecast")
    if clock.get("schema_id") != "issue79-active-wall-clock-v1" or clock.get("active_session") is not None:
        stop("active task clock not idle or unexpected")
    if clock.get("allowance_seconds") is not None:
        stop("active wall allowance is already set; never mutate it in reforecast")
    if report.get("validation_or_test_started") is not False:
        stop("pilot receipt no longer confirms validation/test were not started")

    trains, evals, all_game_intervals, game_hashes = {}, {}, [], []
    train_env, train_timer, train_setup = 0.0, 0.0, 0.0
    eval_env, eval_timer, eval_setup = 0.0, 0.0, 0.0
    for arm in ARMS:
        run = progress["training_runs"][arm]
        if run.get("status") != "SUCCEEDED" or int(run.get("completed", 0)) != 32:
            stop(f"training arm {arm} incomplete")
        root = artifacts / Path(run["evidence_origin"]["artifact_root"])
        spans, receipt_hashes = game_receipts(root, "TRAIN-", 32)
        all_game_intervals.extend(spans)
        game_hashes.extend(receipt_hashes)
        start, end = min(s for s, _ in spans), max(e for _, e in spans)
        env, timer = (end-start).total_seconds(), float(run["elapsed_seconds"])
        if timer < env:
            stop(f"training {arm} invocation timer less than game envelope")
        setup = timer - env
        train_env += env; train_timer += timer; train_setup += setup
        trains[arm] = {"games": 32, "status": "SUCCEEDED", "run_timer_seconds": secs(timer),
                       "complete_game_envelope_seconds": secs(env), "launch_setup_scheduling_seconds": secs(setup),
                       "first_game_utc": start.isoformat().replace("+00:00","Z"),
                       "last_game_utc": end.isoformat().replace("+00:00","Z")}
    for arm in EVAL_ARMS:
        run = progress["evaluation_runs"][arm]
        if run.get("status") != "SUCCEEDED" or int(run.get("completed", 0)) != 8:
            stop(f"evaluation group {arm} incomplete")
        spans, receipt_hashes = game_receipts(current / arm, "EVAL-", 8)
        all_game_intervals.extend(spans)
        game_hashes.extend(receipt_hashes)
        start, end = min(s for s, _ in spans), max(e for _, e in spans)
        env, timer = (end-start).total_seconds(), float(run["elapsed_seconds"])
        if timer < env:
            stop(f"evaluation {arm} invocation timer less than game envelope")
        setup = timer - env
        eval_env += env; eval_timer += timer; eval_setup += setup
        evals[arm] = {"games": 8, "status": "SUCCEEDED", "run_timer_seconds": secs(timer),
                      "complete_game_envelope_seconds": secs(env), "launch_setup_scheduling_seconds": secs(setup),
                      "first_game_utc": start.isoformat().replace("+00:00","Z"),
                      "last_game_utc": end.isoformat().replace("+00:00","Z")}

    pilot_clock = float(clock["elapsed_seconds"])
    sessions = session_summary(artifacts)
    game_union = union_seconds(all_game_intervals)
    actor = progress["actor_updates"]
    if set(actor) != set(ARMS) or any(x.get("status") != "SUCCEEDED" for x in actor.values()):
        stop("actor-update evidence changed")
    actor_pilot = sum(float(x["elapsed_seconds"]) for x in actor.values())
    actor_formal = actor_pilot * 16
    predictor = predictor_projection(progress)
    closeout_proxy = max(0.0, pilot_clock-float(progress["clock_elapsed_before"]))

    # Future registered phases are sequential. Use within-invocation wall envelopes
    # at pilot concurrency; never sum worker CPU/concurrent intervals for actual pilot wall.
    formal = {
        "training_game_throughput": train_env / 192 * TRAIN_GAMES,
        "training_setup_for_96_128_owner_blocks": train_setup / 6 * TRAIN_BLOCKS,
        "evaluation_game_throughput": eval_env / 56 * EVAL_GAMES,
        "evaluation_setup_for_26_invocations": eval_setup / 7 * EVAL_INVOCATIONS,
        "actor_updates_96_blocks": actor_formal,
        "lagged_predictor_fits_and_caches": predictor["formal_fit_plus_cache_seconds"],
    }
    formal_total = sum(formal.values())
    legacy_stages = sum(float(old[k]) for k in (
        "pilot_game_stage_wall_seconds", "pilot_actor_update_wall_seconds", "pilot_predictor_fit_wall_seconds"))
    legacy_residual = max(0.0, float(old["pilot_task_cumulative_active_wall_seconds"])-legacy_stages)
    failures, lineage_hashes = failure_evidence(artifacts, current)
    scenario_specs = {"low": (0.0, 0.0), "base": (0.25, 0.25), "high": (1.0, 0.50)}
    scenarios = {}
    for name, (residual_fraction, margin_fraction) in scenario_specs.items():
        margin = formal_total * margin_fraction
        whole = pilot_clock + formal_total + closeout_proxy + margin + legacy_residual * residual_fraction
        scenarios[name] = {
            "pilot_wall_already_spent_seconds": secs(pilot_clock),
            "measured_formal_workload_seconds": secs(formal_total),
            "future_closeout_proxy_seconds": secs(closeout_proxy),
            "judgmental_margin_fraction": margin_fraction,
            "judgmental_margin_seconds": secs(margin),
            "legacy_task_residual_repeat_fraction": residual_fraction,
            "legacy_task_residual_repeated_once_at_task_level_seconds": secs(legacy_residual*residual_fraction),
            "whole_task_active_wall_forecast_seconds": secs(whole),
            "whole_task_active_wall_forecast_hours": secs(whole/3600),
            "candidate_150pct_allowance_not_installed_seconds": secs(whole*1.5),
            "candidate_175pct_allowance_not_installed_seconds": secs(whole*1.75),
            "candidate_200pct_allowance_not_installed_seconds": secs(whole*2),
        }


    cp_records, pointer_hashes = checkpoints(artifacts, current)
    correction_path = artifacts / "wall-sessions/issue84-task-repair-accounting-correction-c1f31e4.json"
    correction = load(correction_path)
    report_hashes = {
        "progress": digest(pp), "report": digest(rp), "old_forecast": digest(fp),
        "active_clock": digest(cp), "current_source_lock_file": digest(lockpath),
        "correction_receipt": digest(correction_path),
        "complete_game_attempt_receipt_hash_set": hashlib.sha256("".join(sorted(game_hashes)).encode("ascii")).hexdigest(),
    }
    producer = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
    script = Path(__file__).resolve()
    assumptions = script.with_name("ASSUMPTIONS.md")
    receipts = {
        "schema_id": "issue84-source-locked-wall-reforecast-v2",
        "status": "EVIDENCE_ONLY_PENDING_INDEPENDENT_REVIEW_AND_PLANNER_FEASIBILITY",
        "study_only": True, "formal_seeds_authorized": False, "formal_seeds_started": False,
        "source_lock_sha256": LOCK, "pilot_package_commit": PACKAGE, "producer_commit": producer,
        "producer_file_sha256": {str(script.relative_to(repo)): digest(script),
                                 str(assumptions.relative_to(repo)): digest(assumptions)},
        "input_sha256": report_hashes,
        "immutable_original_receipts": {
            "report_sha256": REPORT_SHA, "progress_sha256": PROGRESS_SHA, "forecast_sha256": FORECAST_SHA,
            "forecast_modified": False, "active_clock_allowance_seconds": clock.get("allowance_seconds"),
        },
        "pilot_actual_wall": {
            "active_clock_cumulative_seconds": secs(pilot_clock),
            "clock_seconds_before_final_closeout": secs(progress["clock_elapsed_before"]),
            "final_incremental_closeout_proxy_seconds": secs(closeout_proxy),
            "active_wall_session_receipts": sessions,
            "complete_game_interval_union_seconds": secs(game_union),
            "wall_counter_minus_game_union_seconds": secs(max(0.0, pilot_clock-game_union)),
            "legacy_unallocated_residual_seconds": secs(legacy_residual),
            "legacy_residual_treatment": "retained for sensitivity only; v1 subtracts summed concurrent run timers and is not a per-game/block rate",
            "historical_pilot_only_recovery": failures,
        },
        "registered_formal_workload": {
            "training_games": TRAIN_GAMES, "validation_and_test_games": EVAL_GAMES,
            "total_games": TRAIN_GAMES+EVAL_GAMES, "training_128_owner_arm_blocks": TRAIN_BLOCKS,
            "validation_invocations": 19, "final_test_invocations": 7,
            "evaluation_invocations_total": EVAL_INVOCATIONS, "pilot_concurrency": old["concurrency"],
        },
        "pilot_execution": {
            "training_groups": trains, "evaluation_groups": evals,
            "game_attempt_interval_union_seconds": secs(game_union),
            "separate_training_and_evaluation_invocation_envelopes_seconds": secs(train_env+eval_env),
            "cross_phase_interval_overlap_seconds": secs(train_env+eval_env-game_union),
            "pilot_training_run_timer_sum_seconds": secs(train_timer),
            "pilot_training_game_envelopes_sum_seconds": secs(train_env),
            "pilot_training_launch_setup_seconds": secs(train_setup),
            "pilot_evaluation_run_timer_sum_seconds": secs(eval_timer),
            "pilot_evaluation_game_envelopes_sum_seconds": secs(eval_env),
            "pilot_evaluation_launch_setup_seconds": secs(eval_setup),
            "pilot_actor_update_timer_sum_seconds": secs(actor_pilot),
            "pilot_predictor_costs": predictor,
        },
        "formal_component_estimates_seconds": {k: secs(v) for k, v in formal.items()} | {
            "measured_formal_subtotal": secs(formal_total)},
        "low_base_high_scenarios": scenarios,
        "allowance_status": "UNSET; all 150-200% values are candidates only",
        "limitations": [
            "No formal game, training, evaluation, or predictor fit ran in this reforecast.",
            "Pilot actual wall uses the active cumulative clock and game-attempt interval union; overlapping training/evaluation intervals count once.",
            "Formal phases are forecast sequentially as registered; game group envelope time and per-invocation setup are separated.",
            "Predictor fit and cache costs scale linearly with registered cumulative decision rows; future fit time may depart from this assumption.",
            "Historical deterministic C failures and PARENT pre-start failures stay pilot-only. C attempt-19 tail remains UNKNOWN.",
            "No source-locked transient retry probability is estimable; operational margins are judgmental sensitivities, not confidence bounds.",
            "Legacy residual is never scaled by game count. Sensitivities repeat it only at task level.",
            "No task-wide allowance is set pending independent review and Planner feasibility decision.",
        ],
    }
    retention = {
        "schema_id": "issue84-pilot-weight-retention-v1",
        "status": "CHECKPOINTS_HASH_VERIFIED_AS_IMMUTABLE_PROVENANCE",
        "pilot_package_commit": PACKAGE, "source_lock_sha256": LOCK, "producer_commit": producer,
        "producer_file_sha256": receipts["producer_file_sha256"], "input_sha256": report_hashes,
        "retention_policy": {
            "discard_means": "exclude from formal initialization, selection, evaluation, and promotion; do not delete",
            "current_package_local_discard_inventory": 0,
            "current_package_local_inventory_scope": "A-F/models files with .pt/.pth/.ckpt extensions",
            "files_copied": False, "files_deleted": False,
            "formal_initialization_source": "Issue 82 exact pi12 plus paired AdamW, per registered contract",
        },
        "checkpoint_count": len(cp_records), "checkpoints": cp_records,
    }
    if len(cp_records) != 15:
        stop("checkpoint inventory does not contain exactly 15 files")
    lineage_hashset = hashlib.sha256("".join(sorted(lineage_hashes)).encode("ascii")).hexdigest()
    receipts["input_sha256"]["failure_lineage_hash_set"] = lineage_hashset
    receipts["input_sha256"]["checkpoint_pointer_hash_set"] = hashlib.sha256(
        "".join(sorted(pointer_hashes)).encode("ascii")).hexdigest()
    return receipts, retention


def encode(obj):
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\\n").encode("utf-8")


def write_once(path, content):
    if path.exists():
        if path.read_bytes() != content:
            stop(f"append-only output exists with different bytes; refusing overwrite: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as f:
        f.write(content)


def self_test():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert union_seconds([(t,t+timedelta(seconds=10)),(t+timedelta(seconds=5),t+timedelta(seconds=15))]) == 15
    assert union_seconds([(t,t+timedelta(seconds=10)),(t+timedelta(seconds=10),t+timedelta(seconds=15))]) == 15
    print("self-test passed: concurrent intervals are counted once")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--artifacts-root", type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.artifacts_root is None:
        parser.error("--artifacts-root required unless --self-test")
    repo, artifacts = args.repo_root.resolve(), args.artifacts_root.resolve()
    forecast, retention = produce(repo, artifacts)
    outroot = artifacts / PILOT
    outputs = [(outroot/"task-wall-forecast-correction-v2.json", encode(forecast)),
               (outroot/"pilot-weight-retention-v1.json", encode(retention))]
    subtotal = forecast["formal_component_estimates_seconds"]["measured_formal_subtotal"]
    print("Issue 84 evidence-only reforecast; no formal seeds/games/training/fitting.")
    print(f"measured formal subtotal: {subtotal:.3f} s ({subtotal/3600:.3f} h)")
    for name, item in forecast["low_base_high_scenarios"].items():
        print(f"{name}: whole task {item['whole_task_active_wall_forecast_hours']:.3f} h; "
              f"candidate 1.75x allowance {item['candidate_175pct_allowance_not_installed_seconds']:.3f} s")
    print(f"retained checkpoints: {retention['checkpoint_count']}; local copies: "
          f"{retention['retention_policy']['current_package_local_discard_inventory']}")
    for path, data in outputs:
        print(f"{path}: sha256={hashlib.sha256(data).hexdigest()} ({len(data)} bytes)")
    if args.write:
        for path, data in outputs:
            write_once(path, data)
        print("new receipts appended; original receipts/checkpoints/clock unchanged")
    else:
        print("dry run only; no files written")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"reforecast stopped: {exc}", file=sys.stderr)
        raise
