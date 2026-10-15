from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
import shutil
import sys
import subprocess
from typing import Any, Mapping


ISSUE_DIR = Path(__file__).resolve().parents[1]
RECOVERY_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = ISSUE_DIR.parent.parent
RECOVERY_RESULTS_IDENTITY = (RECOVERY_DIR.relative_to(REPOSITORY_ROOT) / "results").as_posix()
SEEDS = list(range(559001, 559065))
VOLATILE_KEYS = {"wall_clock_time_s", "process_cpu_s", "elapsed_process_cpu_s"}
TRACE_ANNOTATION_KEYS = {"source_selected_identity_verified"}
SEALED_VERIFIER_COMMIT = "b34dacefec685eac8c4db625cb1b364b8b5fd802"
SEALED_PRODUCER_COMMIT = "12de824ed8a3c7fcca817df13a8abda15190658a"
SOURCE_FIELDS = (
    "seed", "source_disposition", "source_root_battle_number", "source_battles_before_root",
    "source_battle_exits", "source_smoke_uses_total", "source_terminal", "source_outcome",
    "source_problems", "source_root", "source_completion",
)
BRANCH_LIST_FIELDS = (
    "public_transition_ledger", "native_search_call_ledger", "constrained_search_calls",
    "b1_raw_state_fingerprint_sha256",
)


def stable(value: Any) -> Any:
    if isinstance(value, dict):
        ignored = VOLATILE_KEYS | TRACE_ANNOTATION_KEYS
        return {key: stable(item) for key, item in value.items() if key not in ignored}
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix.lower() in {".py", ".json", ".jsonl"}:
        data = data.replace(b"\r\n", b"\n")
    return digest_bytes(data)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def git_file_digest(repo: Path, revision: str, relative_path: str) -> str:
    data = subprocess.check_output(["git", "-C", str(repo), "show", f"{revision}:{relative_path}"])
    if Path(relative_path).suffix.lower() in {".py", ".json", ".jsonl"}:
        data = data.replace(b"\r\n", b"\n")
    return digest_bytes(data)


def json_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return digest_bytes(payload)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def matches_run_identity(run_directory: Any, relative_identity: str) -> bool:
    """Match a ledger path to its repository-relative run identity across checkouts."""
    if not isinstance(run_directory, str):
        return False
    normalized = "/" + run_directory.replace("\\", "/").strip("/") + "/"
    identity = "/" + relative_identity.replace("\\", "/").strip("/") + "/"
    return normalized.endswith(identity)


def source_signature(row: Mapping[str, Any]) -> dict[str, Any]:
    return stable({key: row.get(key) for key in SOURCE_FIELDS})


def seed_map(rows: list[dict[str, Any]], label: str, failures: list[str]) -> dict[int, dict[str, Any]]:
    seeds = [int(row["seed"]) for row in rows if row.get("seed") is not None]
    if seeds != SEEDS:
        failures.append(f"{label} seed sequence is not exactly 559001..559064")
    return {int(row["seed"]): row for row in rows if row.get("seed") is not None}


def branch_signature(branch: Mapping[str, Any]) -> dict[str, Any]:
    trace = branch.get("trace")
    if not isinstance(trace, Mapping):
        return {"trace_available": False}
    trace_rows = {key: stable(trace.get(key, [])) for key in BRANCH_LIST_FIELDS}
    for field in ("native_search_call_ledger", "constrained_search_calls"):
        rows = trace_rows[field]
        first_constrained_root = True
        if isinstance(rows, list):
            for row in rows:
                if (not isinstance(row, dict) or row.get("phase") != "b1"
                        or row.get("native_api") != "StepSimulator.battle_search_v2_forbidden_potion_slot.v1"):
                    continue
                if first_constrained_root:
                    first_constrained_root = False
                    continue
                evidence = row.get("protected_root_evidence")
                if isinstance(evidence, dict):
                    evidence.pop("source_selected_legal_index", None)
    return {
        "trace_available": True,
        "endpoint": stable(branch.get("endpoint")),
        "root_restore_equal": branch.get("root_restore_equal"),
        "controller_provenance": stable(branch.get("controller_provenance")),
        "trace": trace_rows,
    }


def branch_is_complete(branch: Mapping[str, Any]) -> bool:
    endpoint = branch.get("endpoint")
    return isinstance(endpoint, Mapping) and endpoint.get("primary") in (0, 1)


def add_difference(failures: list[str], label: str, seed: int, field: str) -> None:
    failures.append(f"seed {seed}: {label} mismatch in {field}")


def compare_branch(old: Mapping[str, Any], new: Mapping[str, Any], seed: int,
                   mode: str, failures: list[str], counts: Counter[str]) -> None:
    old_sig, new_sig = branch_signature(old), branch_signature(new)
    if not old_sig["trace_available"]:
        return
    if not new_sig["trace_available"]:
        add_difference(failures, mode, seed, "missing recovery trace")
        return
    counts["historical_branch_traces_checked"] += 1
    complete = branch_is_complete(old)
    if old_sig["root_restore_equal"] is not None and old_sig["root_restore_equal"] != new_sig["root_restore_equal"]:
        add_difference(failures, mode, seed, "root_restore_equal")
    if complete:
        for field in ("endpoint", "controller_provenance"):
            if old_sig[field] != new_sig[field]:
                add_difference(failures, mode, seed, field)
    for field in BRANCH_LIST_FIELDS:
        before = old_sig["trace"][field]
        after = new_sig["trace"][field]
        if len(after) < len(before) or after[:len(before)] != before:
            add_difference(failures, mode, seed, f"{field} prefix")
        elif complete and len(after) != len(before):
            add_difference(failures, mode, seed, f"{field} length")


def prepare() -> None:
    original = ISSUE_DIR / "results"
    original_preflight = ISSUE_DIR / "preflight"
    recovery_preflight = RECOVERY_DIR / "preflight"
    recovery_ledger = RECOVERY_DIR / "budget_events.jsonl"
    plan_path = RECOVERY_DIR / "recovery_plan.json"
    if plan_path.exists() or recovery_ledger.exists() or recovery_preflight.exists():
        raise RuntimeError("recovery outputs already exist; refusing to overwrite a prior attempt")

    old_summary = load_json(original / "summary.json")
    old_manifest = load_json(original / "manifest.json")
    old_owners = load_jsonl(original / "owners.jsonl")
    old_sources = load_jsonl(original / "source_attempts.jsonl")
    if old_summary.get("terminal_disposition") != "VALIDITY_FAILURE":
        raise RuntimeError("historical cohort is not the authorized immutable VALIDITY_FAILURE record")
    failures: list[str] = []
    owners_by_seed = seed_map(old_owners, "historical owners", failures)
    sources_by_seed = seed_map(old_sources, "historical source attempts", failures)
    if failures:
        raise RuntimeError("; ".join(failures))
    if len(old_owners) != 64 or len(old_sources) != 64:
        raise RuntimeError("historical cohort does not contain exactly 64 owners and source attempts")

    dispositions = Counter(str(row.get("source_disposition")) for row in old_sources)
    eligible = sum(row.get("source_disposition") == "eligible_root" for row in old_sources)
    if eligible != 55 or dispositions.get("source_terminal_no_qualifying_use") != 9:
        raise RuntimeError(f"historical fixed population differs from 55 eligible / 9 no-use: {dict(dispositions)}")
    signatures = []
    for seed in SEEDS:
        source = sources_by_seed[seed]
        owner = owners_by_seed[seed]
        if source_signature(source) != source_signature(owner):
            raise RuntimeError(f"historical source attempt and owner disagree for seed {seed}")
        signatures.append(source_signature(source))

    old_ledger = ISSUE_DIR / "budget_events.jsonl"
    ledger_rows = load_jsonl(old_ledger)
    if not ledger_rows:
        raise RuntimeError("historical budget ledger is empty")
    prior_cpu = float(ledger_rows[-1].get("elapsed_process_cpu_s", 0.0))
    witness_rows = [row for row in load_jsonl(original_preflight / "owners.jsonl")
                    if row.get("preflight_role") == "later_battle_semantic_witness"]
    if len(witness_rows) != 1 or int(witness_rows[0].get("seed", -1)) != 559001:
        raise RuntimeError("retained later-battle preflight witness is missing or unexpected")

    recovery_preflight.mkdir(parents=True)
    shutil.copyfile(original_preflight / "owners.jsonl", recovery_preflight / "owners.jsonl")
    shutil.copyfile(original_preflight / "source_attempts.jsonl", recovery_preflight / "source_attempts.jsonl")
    shutil.copyfile(old_ledger, recovery_ledger)
    historical_files = (
        "results/summary.json", "results/manifest.json", "results/owners.jsonl",
        "results/source_attempts.jsonl", "preflight/summary.json", "preflight/manifest.json",
        "preflight/owners.jsonl", "preflight/source_attempts.jsonl", "budget_events.jsonl",
    )
    plan = {
        "issue": 59,
        "planner_authorization_comment": "https://github.com/lsmfttb/spire-research/issues/59#issuecomment-6188817927",
        "recovery_code_base": "96447a8a73af174f86e89094e59e33a3ee6595d4",
        "recovery_support_sha256": file_digest(Path(__file__).resolve()),
        "study_code_sha256": file_digest(ISSUE_DIR / "study.py"),
        "immutable_historical_artifacts": {name: file_digest(ISSUE_DIR / name) for name in historical_files},
        "population": {
            "seed_order": SEEDS, "owner_count": 64,
            "historical_source_dispositions": dict(sorted(dispositions.items())),
            "historical_eligible_count": eligible,
            "historical_source_signature_sha256": json_digest(signatures),
            "source_signature_fields": list(SOURCE_FIELDS),
        },
        "retained_preflight_witness": {
            "seed": 559001,
            "source_battle_number": witness_rows[0].get("source_root", {}).get("source_battle_number"),
            "requalification": "copy the historical owner/source attempt and re-evaluate with current preflight_qualification; do not replay the source owner",
        },
        "pinned_configuration": {
            "native_commit": old_manifest["simulator"]["commit"],
            "native_module_sha256": old_manifest["simulator"]["module_sha256"],
            "executor_commit": old_manifest["executor_commit"],
            "executor_source_sha256": old_manifest["executor_source_sha256"],
            "controller": old_manifest["search"],
            "non_combat_driver": old_manifest["non_combat_driver"],
        },
        "artifact_destinations": {
            "preflight": "studies/issue59/recovery/preflight",
            "results": "studies/issue59/recovery/results",
            "budget_ledger": "studies/issue59/recovery/budget_events.jsonl",
            "parity_report": "studies/issue59/recovery/source_parity.json",
            "historical_artifacts_modified": False,
        },
        "budget": {
            "hard_cap_process_cpu_s": 12000,
            "builder_ceiling_process_cpu_s": 10200,
            "reviewer_reserve_process_cpu_s": 1800,
            "historical_ledger_last_total_process_cpu_s": prior_cpu,
            "recovery_setup_conservative_charge_process_cpu_s": 30.0,
            "builder_total_after_setup_charge_process_cpu_s": prior_cpu + 30.0,
            "historical_ledger_rows_copied": len(ledger_rows),
        },
        "parity_policy": {
            "compare_all_64_source_dispositions_and_root_identities": True,
            "eligible_owner_count_must_remain_55": True,
            "compare_saved_initial_b1_raw_state_fingerprints_and_restore_checks_where_available": True,
            "compare_historical_complete_branch_traces_exactly_and_incomplete_trace_prefixes": True,
            "do_not_interpret_or_pool_results_if_any_source_parity_check_fails": True,
            "opaque_native_checkpoint_objects_are_not_serialized": True,
        },
    }
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"plan": str(plan_path), "historical_owners": 64, "eligible": eligible,
        "historical_builder_cpu_s": prior_cpu, "recovery_setup_charge_cpu_s": 30.0,
        "retained_witness_seed": 559001}, sort_keys=True))


def verify() -> int:
    plan = load_json(RECOVERY_DIR / "recovery_plan.json")
    failures: list[str] = []
    counts: Counter[str] = Counter()
    recovery_support_relative = "studies/issue59/recovery/verify_recovery.py"
    if git_file_digest(ISSUE_DIR.parent.parent, SEALED_VERIFIER_COMMIT,
                       recovery_support_relative) != plan["recovery_support_sha256"]:
        failures.append("sealed pre-outcome recovery verifier does not match its pushed source commit")
    if file_digest(ISSUE_DIR / "study.py") != plan["study_code_sha256"]:
        failures.append("study implementation differs from the version sealed before outcomes")
    for relative, expected in plan["immutable_historical_artifacts"].items():
        path = ISSUE_DIR / relative
        if file_digest(path) != expected:
            failures.append(f"immutable historical artifact changed: {relative}")

    expected_pins = plan["pinned_configuration"]
    historical_manifest = load_json(ISSUE_DIR / "results" / "manifest.json")
    preflight_manifest = load_json(RECOVERY_DIR / "preflight" / "manifest.json")
    recovery_manifest = load_json(RECOVERY_DIR / "results" / "manifest.json")
    for label, manifest in (("preflight", preflight_manifest), ("recovery", recovery_manifest)):
        simulator = manifest.get("simulator", {})
        checks = {
            "native commit": simulator.get("commit") == expected_pins["native_commit"],
            "native module hash": simulator.get("module_sha256") == expected_pins["native_module_sha256"],
            "executor commit": manifest.get("executor_commit") == expected_pins["executor_commit"],
            "executor source hashes": manifest.get("executor_source_sha256") == expected_pins["executor_source_sha256"],
            "controller": manifest.get("search") == expected_pins["controller"],
            "non-combat driver": manifest.get("non_combat_driver") == expected_pins["non_combat_driver"],
            "study code hash": manifest.get("study_code_sha256") == plan["study_code_sha256"],
            "producer checkout": manifest.get("producer_checkout") == SEALED_PRODUCER_COMMIT,
            "same producer checkout as retained preflight": manifest.get("producer_checkout") == preflight_manifest.get("producer_checkout"),
        }
        counts[f"{label}_manifest_pins_checked"] += len(checks)
        for name, passed in checks.items():
            if not passed:
                failures.append(f"{label} manifest does not match sealed {name}")

    old_owners_rows = load_jsonl(ISSUE_DIR / "results" / "owners.jsonl")
    old_sources_rows = load_jsonl(ISSUE_DIR / "results" / "source_attempts.jsonl")
    new_owners_rows = load_jsonl(RECOVERY_DIR / "results" / "owners.jsonl")
    new_sources_rows = load_jsonl(RECOVERY_DIR / "results" / "source_attempts.jsonl")
    old_owners = seed_map(old_owners_rows, "historical owners", failures)
    old_sources = seed_map(old_sources_rows, "historical source attempts", failures)
    new_owners = seed_map(new_owners_rows, "recovery owners", failures)
    new_sources = seed_map(new_sources_rows, "recovery source attempts", failures)

    old_signatures = [source_signature(old_sources[seed]) for seed in SEEDS if seed in old_sources]
    new_signatures = [source_signature(new_sources[seed]) for seed in SEEDS if seed in new_sources]
    if json_digest(old_signatures) != plan["population"]["historical_source_signature_sha256"]:
        failures.append("historical source signature no longer matches the sealed recovery plan")
    source_mismatch_seeds: list[int] = []
    for seed in SEEDS:
        if seed not in old_sources or seed not in new_sources or seed not in old_owners or seed not in new_owners:
            source_mismatch_seeds.append(seed)
            continue
        old_sig, new_sig = source_signature(old_sources[seed]), source_signature(new_sources[seed])
        if old_sig != new_sig:
            add_difference(failures, "source disposition/root identity", seed, "source signature")
            source_mismatch_seeds.append(seed)
        if source_signature(old_owners[seed]) != old_sig or source_signature(new_owners[seed]) != new_sig:
            add_difference(failures, "owner/source consistency", seed, "source signature")
            source_mismatch_seeds.append(seed)

    dispositions = Counter(str(row.get("source_disposition")) for row in new_sources_rows)
    eligible = sum(row.get("source_disposition") == "eligible_root" for row in new_sources_rows)
    if len(new_sources_rows) != 64 or eligible != 55:
        failures.append(f"recovery population is not 64 owners / 55 eligible: count={len(new_sources_rows)}, eligible={eligible}")

    recovery_summary_events = [event for event in load_jsonl(RECOVERY_DIR / "budget_events.jsonl")
        if (event.get("stage") == "summary_durable"
            and matches_run_identity(event.get("run_directory"), RECOVERY_RESULTS_IDENTITY))]
    study_terminal_disposition = None
    if len(recovery_summary_events) != 1:
        failures.append(
            "recovery run's durable ledger summary event is not unique for repository-relative "
            f"run identity {RECOVERY_RESULTS_IDENTITY!r}: found {len(recovery_summary_events)}"
        )
    else:
        study_terminal_disposition = recovery_summary_events[0].get("terminal_disposition")
        if not study_terminal_disposition:
            failures.append("recovery run's original study terminal disposition is missing from the durable ledger")

    fingerprint_mismatch_seeds: list[int] = []
    for seed in SEEDS:
        old, new = old_owners.get(seed, {}), new_owners.get(seed, {})
        if old.get("source_disposition") != "eligible_root" or new.get("source_disposition") != "eligible_root":
            continue
        counts["eligible_roots_checked"] += 1
        old_roots, new_roots = {}, {}
        for mode in ("consume", "reserve"):
            old_branch, new_branch = old.get(mode, {}), new.get(mode, {})
            old_trace, new_trace = old_branch.get("trace", {}), new_branch.get("trace", {})
            old_hashes = old_trace.get("b1_raw_state_fingerprint_sha256", [])
            new_hashes = new_trace.get("b1_raw_state_fingerprint_sha256", [])
            old_roots[mode] = old_hashes[0] if old_hashes else None
            new_roots[mode] = new_hashes[0] if new_hashes else None
            if old_branch.get("trace") is not None:
                compare_branch(old_branch, new_branch, seed, mode, failures, counts)
            if new_branch.get("root_restore_equal") is not True:
                add_difference(failures, mode, seed, "recovery root checkpoint restore")
            if mode == "reserve":
                constrained_calls = new_trace.get("constrained_search_calls", [])
                first_root_call = next((row for row in constrained_calls
                    if row.get("phase") == "b1" and row.get("native_api") == "StepSimulator.battle_search_v2_forbidden_potion_slot.v1"), None)
                if (first_root_call is None
                        or first_root_call.get("protected_root_evidence", {}).get("source_selected_identity_verified") is not True):
                    add_difference(failures, mode, seed, "initial restored-root source identity verification")
        old_available = old_roots["consume"] or old_roots["reserve"]
        if old_available is not None:
            counts["historical_b1_raw_root_fingerprints_checked"] += 1
            if new_roots["consume"] != old_available or new_roots["reserve"] != old_available:
                add_difference(failures, "B1 root", seed, "historical raw-state fingerprint")
                fingerprint_mismatch_seeds.append(seed)
        if new_roots["consume"] is None or new_roots["reserve"] is None or new_roots["consume"] != new_roots["reserve"]:
            add_difference(failures, "B1 root", seed, "paired raw-state fingerprint equality")
            fingerprint_mismatch_seeds.append(seed)

    preflight = load_json(RECOVERY_DIR / "preflight" / "summary.json")
    if (preflight.get("terminal_disposition") != "SEMANTIC_PREFLIGHT_PASS"
            or preflight.get("witness_owner_seed") != 559001):
        failures.append("recovery preflight did not requalify retained late-battle witness seed 559001")

    report = {
        "issue": 59,
        "status": "PASS" if not failures else "FAIL",
        "outcome_interpretation_allowed": not failures,
        "historical_source_dispositions": plan["population"]["historical_source_dispositions"],
        "recovery_source_dispositions": dict(sorted(dispositions.items())),
        "study_code_sha256_comparison": {
            "historical": historical_manifest.get("study_code_sha256"),
            "recovery": recovery_manifest.get("study_code_sha256"),
            "equal": historical_manifest.get("study_code_sha256") == recovery_manifest.get("study_code_sha256"),
            "review_note": "Source/controller/policy manifests are compared directly; branch trace parity is also checked after excluding only the documented post-search verification metadata. Reviewer should assess the study-code revision difference independently.",
        },
        "historical_eligible_owner_count": plan["population"]["historical_eligible_count"],
        "recovery_eligible_owner_count": eligible,
        "source_mismatch_seeds": sorted(set(source_mismatch_seeds)),
        "b1_root_fingerprint_mismatch_seeds": sorted(set(fingerprint_mismatch_seeds)),
        "checks": dict(counts),
        "historical_source_signature_sha256": json_digest(old_signatures),
        "recovery_source_signature_sha256": json_digest(new_signatures),
        "historical_trace_note": "Compare selected source action/root/search and saved first restored-B1 raw-state fingerprints. Opaque native checkpoint objects were not serialized; each recovery branch must independently report root_restore_equal=true.",
        "trace_schema_note": "Ignore the study-local source_selected_identity_verified annotation added after the historical run. Keep comparing source_selected_legal_index at the initial restored root, where it is rechecked; omit it only at later constrained nodes because the recovery records it only for the initial root. Native API/patch, protected rows, chosen action, visits, raw-state fingerprints, and all other trace fields remain compared.",
        "study_terminal_disposition": study_terminal_disposition,
        "pre_outcome_verifier_sha256": plan["recovery_support_sha256"],
        "pre_outcome_verifier_commit": SEALED_VERIFIER_COMMIT,
        "audit_verifier_revision_sha256": file_digest(Path(__file__).resolve()),
        "audit_verifier_repair": "Added the missing stdlib git helper after the first audit launch failed before comparison; the sealed pre-outcome verifier blob is checked against the recovery plan above.",
        "failures": failures,
    }
    report_path = RECOVERY_DIR / "source_parity.json"
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    report_path.write_text(payload, encoding="utf-8")
    summary_path = RECOVERY_DIR / "results" / "summary.json"
    summary = load_json(summary_path)
    report_hash = digest_bytes(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    summary["recovery_source_parity"] = {"status": report["status"],
        "report_path": str(report_path), "report_sha256": report_hash}
    summary["study_terminal_disposition"] = study_terminal_disposition
    summary["recovery_outcome_interpretation_allowed"] = not failures
    if failures:
        summary["terminal_disposition"] = "VALIDITY_FAILURE"
        summary.setdefault("limitations", []).append("source/root/trace parity failed; do not interpret or pool recovery outcomes")
    else:
        summary["terminal_disposition"] = study_terminal_disposition
        summary["limitations"] = [item for item in summary.get("limitations", [])
            if item != "source/root/trace parity failed; do not interpret or pool recovery outcomes"]
    temp = summary_path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, summary_path)
    print(json.dumps({"source_parity": report["status"], "failures": len(failures),
        "source_mismatch_seeds": report["source_mismatch_seeds"],
        "b1_root_fingerprint_mismatch_seeds": report["b1_root_fingerprint_mismatch_seeds"]}, sort_keys=True))
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare and audit the authorized Issue #59 fixed-cohort recovery")
    parser.add_argument("mode", choices=("prepare", "verify"))
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare()
        return 0
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
