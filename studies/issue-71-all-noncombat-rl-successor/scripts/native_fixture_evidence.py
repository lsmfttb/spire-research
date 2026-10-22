#!/usr/bin/env python3
"""Rebuild and capture the exact-source native noncombat fixture gate for Issue 71.

This is a study-only evidence generator. It does not launch a study runner, consume
formal seeds, modify the simulator, or change the locked study configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import subprocess
import time
from pathlib import Path
from typing import Any


NATIVE_COMMIT = "8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2"
NATIVE_TREE = "e45ef2a485a6dc33266161852693ce9ace1ffc30"
NATIVE_MODULE_SHA256 = "bb22332a4a7108e8f9691482df2624b3b132d75ca07e1aa7b394b932193e5cad"
STSRL_COMMIT = "7d172f7c24c9d29429b6946ba44f6b59760b0d96"
JSON_SUBMODULE = "0b345b20c888f7dc8888485768e4bf9a6be29de0"
PYBIND11_SUBMODULE = "d03662f0984f652b60e7ddce53d3868002275197"
EXPECTED_MODULE_NAME = "slaythespire.cpython-313-x86_64-linux-gnu.so"
EXPECTED_COL = "EVENT_SCREEN:COLOSSEUM"
EXPECTED_MK = "EVENT_SCREEN:MATCH_AND_KEEP"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def git_source_identity(path: Path) -> dict[str, Any]:
    return {
        "head_commit": git(path, "rev-parse", "HEAD"),
        "head_tree": git(path, "rev-parse", "HEAD^{tree}"),
        "status_porcelain": git(path, "status", "--porcelain"),
        "submodules": git(path, "submodule", "status"),
    }


def tree_file_hashes(root: Path) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return output
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        output[rel] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    return output


def cpu_snapshot() -> tuple[float, float]:
    self_usage = resource.getrusage(resource.RUSAGE_SELF)
    child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return self_usage.ru_utime + self_usage.ru_stime, child_usage.ru_utime + child_usage.ru_stime


def run_recorded(
    argv: list[str], *, cwd: Path, env: dict[str, str], records: list[dict[str, Any]],
    timeout: int = 1800,
) -> subprocess.CompletedProcess[bytes]:
    self_before, children_before = cpu_snapshot()
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        completed = subprocess.CompletedProcess(argv, 124, exc.stdout or b"", exc.stderr or b"")
        timed_out = True
    elapsed = time.perf_counter() - started
    self_after, children_after = cpu_snapshot()
    stdout = completed.stdout if isinstance(completed.stdout, bytes) else str(completed.stdout).encode()
    stderr = completed.stderr if isinstance(completed.stderr, bytes) else str(completed.stderr).encode()
    record = {
        "argv": argv,
        "cwd": str(cwd),
        "environment_overrides": {
            key: env[key] for key in (
                "CMAKE_POLICY_VERSION_MINIMUM", "Python_EXECUTABLE", "PYTHONHOME",
                "PYTHONPATH", "STS_LIGHTSPEED_SOURCE_DIR", "STSRL_SOURCE_DIR",
            ) if key in env
        },
        "exit_status": completed.returncode,
        "signal": -completed.returncode if completed.returncode < 0 else None,
        "timed_out": timed_out,
        "wall_seconds": round(elapsed, 6),
        "python_process_cpu_delta_seconds": round(self_after - self_before, 6),
        "descendant_process_cpu_delta_seconds": round(children_after - children_before, 6),
        "stdout_bytes": len(stdout),
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_bytes": len(stderr),
        "stderr_sha256": sha256_bytes(stderr),
        "stdout_utf8": stdout.decode("utf-8", errors="replace"),
        "stderr_utf8": stderr.decode("utf-8", errors="replace"),
    }
    records.append(record)
    return completed


def extract_json(stdout: bytes) -> dict[str, Any]:
    text = stdout.decode("utf-8")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("native fixture produced no JSON object on stdout")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("native fixture JSON root is not an object")
    return parsed


def criterion(name: str, ok: bool, detail: Any) -> dict[str, Any]:
    return {"name": name, "passed": bool(ok), "observed": detail}


def validate_named_witnesses(payload: dict[str, Any]) -> list[dict[str, Any]]:
    coverage = payload.get("coverage_matrix", {})
    col = coverage.get(EXPECTED_COL, {})
    mk = coverage.get(EXPECTED_MK, {})
    return [
        criterion("native semantic capability schema", payload.get("schema_id") == "native-privileged-noncombat-semantic-capability-evidence-v1", payload.get("schema_id")),
        criterion("exact tested native source commit", payload.get("tested_native_source_commit") == NATIVE_COMMIT, payload.get("tested_native_source_commit")),
        criterion("all currently registered noncombat multichoice families supported", payload.get("coverage_status") == "NATIVE_FULL_NONCOMBAT_SEMANTIC_CAPABILITY_ACCEPTED", payload.get("coverage_status")),
        criterion("Colosseum reachable native event fixture", col.get("reachability_classification") == "actual_reachable_act2_event" and col.get("status") == "supported", {"status": col.get("status"), "reachability_classification": col.get("reachability_classification")}),
        criterion("Colosseum first encounter and second encounter", col.get("opening_encounter") == "COLOSSEUM_EVENT_SLAVERS" and col.get("second_encounter") == "COLOSSEUM_EVENT_NOBS", {"opening_encounter": col.get("opening_encounter"), "second_encounter": col.get("second_encounter")}),
        criterion("Colosseum continuation, hidden potion roll and no ordinary first-fight rewards", col.get("first_fight_smoke_bomb_continues_event") is True and col.get("first_fight_hidden_potion_roll_consumed_but_not_shown") is True and col.get("first_fight_ordinary_rewards") is False, {key: col.get(key) for key in ("first_fight_smoke_bomb_continues_event", "first_fight_hidden_potion_roll_consumed_but_not_shown", "first_fight_ordinary_rewards")}),
        criterion("Colosseum checkpoint reward/RNG replay", col.get("checkpoint_replay_exact") is True and isinstance(col.get("second_reward_replay"), dict), {"checkpoint_replay_exact": col.get("checkpoint_replay_exact"), "second_reward_replay": col.get("second_reward_replay")}),
        criterion("Match-and-Keep actual reachable shrine fixture", mk.get("reachability_classification") == "actual_reachable_act1_shrine" and mk.get("status") == "supported", {"status": mk.get("status"), "reachability_classification": mk.get("reachability_classification")}),
        criterion("Match-and-Keep first/second legal choice counts and pair rejection", mk.get("starting_first_pick_actions") == 12 and mk.get("second_pick_actions_after_first_reveal") == 11 and mk.get("pair_actions_rejected") is True, {key: mk.get(key) for key in ("starting_first_pick_actions", "second_pick_actions_after_first_reveal", "pair_actions_rejected")}),
        criterion("Match-and-Keep reveal, public hidden-card projection and progression", mk.get("first_card_revealed_before_second_choice") is True and mk.get("public_projection_hides_other_face_down_card_identities") is True and mk.get("mismatch_match_and_five_attempt_progression") is True, {key: mk.get(key) for key in ("first_card_revealed_before_second_choice", "public_projection_hides_other_face_down_card_identities", "mismatch_match_and_five_attempt_progression")}),
        criterion("Match-and-Keep native checkpoint/RNG replay", mk.get("checkpoint_rng_replay") is True, mk.get("checkpoint_rng_replay")),
        criterion("No training or evaluation authorized or run by fixture", payload.get("training_authorized") is False and payload.get("no_training_or_evaluation_ran") is True, {"training_authorized": payload.get("training_authorized"), "no_training_or_evaluation_ran": payload.get("no_training_or_evaluation_ran")}),
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-source", type=Path, required=True)
    parser.add_argument("--stsrL-source", dest="stsrL_source", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--pinned-module", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--study-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--study-script-source-commit", required=True)
    parser.add_argument("--study-parent-commit", required=True)
    parser.add_argument("--prior-measured-build-cpu-seconds", type=float, default=135.21)
    parser.add_argument("--prior-unmetered-preparation-allowance-seconds", type=float, default=20.0)
    args = parser.parse_args()

    native_source = args.native_source.resolve()
    stsrL_source = args.stsrL_source.resolve()
    python = args.python.resolve()
    pinned_module = args.pinned_module.resolve()
    build_dir = args.build_dir.resolve()
    study_root = args.study_root.resolve()
    if not native_source.is_dir() or not stsrL_source.is_dir() or not python.is_file() or not pinned_module.is_file():
        parser.error("all source and Python paths must exist")
    if os.name == "nt":
        parser.error("this resource-accounted generator must run under Linux")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    native_identity = git_source_identity(native_source)
    stsrL_identity = git_source_identity(stsrL_source)
    pinned_module_sha256 = sha256_file(pinned_module)
    pinned_module_hash_matches = pinned_module_sha256 == NATIVE_MODULE_SHA256
    source_checks = {
        "native_commit_exact": native_identity["head_commit"] == NATIVE_COMMIT,
        "native_tree_exact": native_identity["head_tree"] == NATIVE_TREE,
        "native_worktree_clean": native_identity["status_porcelain"] == "",
        "json_submodule_exact": JSON_SUBMODULE in native_identity["submodules"],
        "pybind11_submodule_exact": PYBIND11_SUBMODULE in native_identity["submodules"],
        "stsrl_commit_exact": stsrL_identity["head_commit"] == STSRL_COMMIT,
        "stsrl_worktree_clean": stsrL_identity["status_porcelain"] == "",
    }

    config_path = study_root / "config.json"
    lock_path = study_root / "config.lock.sha256"
    config_sha256 = sha256_file(config_path)
    lock_value = lock_path.read_text(encoding="utf-8").strip()
    config_lock_matches = config_sha256 == lock_value
    historical_artifacts_before = tree_file_hashes(study_root / "stage1-artifacts")

    script_rel = Path(__file__).resolve().relative_to(study_root).as_posix()
    generator_script_sha256 = sha256_file(Path(__file__).resolve())
    generator_commit = args.study_script_source_commit
    root_commit_before = args.study_parent_commit
    command_records: list[dict[str, Any]] = []
    module_candidates: list[Path] = []

    env = os.environ.copy()
    env.update({
        "CMAKE_POLICY_VERSION_MINIMUM": "3.5",
        "PYTHONHOME": str(python.parent.parent),
        "PYTHONPATH": os.pathsep.join((str(pinned_module.parent), str(build_dir), str(study_root), str(stsrL_source / "src"))),
        "STS_LIGHTSPEED_SOURCE_DIR": str(native_source),
        "STSRL_SOURCE_DIR": str(stsrL_source),
    })
    configure = [
        "cmake", "-S", str(native_source), "-B", str(build_dir),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        f"-DPython_EXECUTABLE={python}",
        f"-DSTS_LIGHTSPEED_TESTED_SOURCE_COMMIT_OVERRIDE={NATIVE_COMMIT}",
    ]
    configure_result = run_recorded(configure, cwd=study_root, env=env, records=command_records)
    configure_ok = configure_result.returncode == 0
    build_result: subprocess.CompletedProcess[bytes] | None = None
    if configure_ok:
        build_result = run_recorded(
            ["cmake", "--build", str(build_dir), "--target", "slaythespire", "test-public-noncombat-projection", "test-potion-semantics", "-j2"],
            cwd=study_root, env=env, records=command_records,
        )
    build_ok = build_result is not None and build_result.returncode == 0
    if build_ok:
        module_candidates = sorted(build_dir.glob("slaythespire*.so"))
    module = module_candidates[0] if module_candidates else None
    module_sha256 = sha256_file(module) if module else None
    module_hash_matches = module_sha256 == NATIVE_MODULE_SHA256

    witness_payload: dict[str, Any] | None = None
    witness_parse_error: str | None = None
    witness_result: subprocess.CompletedProcess[bytes] | None = None
    potion_result: subprocess.CompletedProcess[bytes] | None = None
    default_result: subprocess.CompletedProcess[bytes] | None = None
    module_smoke_result: subprocess.CompletedProcess[bytes] | None = None
    module_smoke_payload: dict[str, Any] | None = None
    module_smoke_error: str | None = None
    module_smoke_pass = False
    criteria: list[dict[str, Any]] = []
    potion_pass = False
    if build_ok:
        fixture_exe = build_dir / "test-public-noncombat-projection"
        potion_exe = build_dir / "test-potion-semantics"
        module_smoke_code = (
            "import json,slaythespire; "
            "s=slaythespire.StepSimulator(slaythespire.CharacterClass.IRONCLAD,49,20); "
            "a=slaythespire.PrivilegedNoncombatProjectionAccess(s,'training_oracle_only'); "
            "print(json.dumps({'module_file':slaythespire.__file__,'qualification':a.training_population_qualification_v1()},sort_keys=True))"
        )
        module_smoke_result = run_recorded([str(python), "-c", module_smoke_code], cwd=study_root, env=env, records=command_records)
        if module_smoke_result.returncode == 0:
            try:
                module_smoke_payload = json.loads(module_smoke_result.stdout.decode("utf-8").strip())
                qualification = module_smoke_payload.get("qualification", {})
                module_smoke_pass = (
                    Path(module_smoke_payload.get("module_file", "")).resolve() == pinned_module
                    and qualification.get("native_source_commit") == NATIVE_COMMIT
                    and qualification.get("source_identity_bound") is True
                    and qualification.get("eligible_for_restricted_no_shard_population") is True
                    and qualification.get("training_authorized_by_this_api") is False
                )
            except Exception as exc:
                module_smoke_error = f"{type(exc).__name__}: {exc}"
        witness_result = run_recorded([str(fixture_exe), "--privileged-noncombat-only"], cwd=study_root, env=env, records=command_records)
        if witness_result.returncode == 0:
            try:
                witness_payload = extract_json(witness_result.stdout)
                criteria = validate_named_witnesses(witness_payload)
            except Exception as exc:  # evidence records parser/validator failures instead of hiding them
                witness_parse_error = f"{type(exc).__name__}: {exc}"
        potion_result = run_recorded([str(potion_exe)], cwd=study_root, env=env, records=command_records)
        potion_stdout = potion_result.stdout.decode("utf-8", errors="replace")
        potion_pass = potion_result.returncode == 0 and "native potion semantics regression passed" in potion_stdout
        # The generic test is an additional diagnostic, not part of the required named
        # event fixture subset. It exercises the study adapter smoke path as well.
        default_result = run_recorded([str(fixture_exe)], cwd=study_root, env=env, records=command_records)

    historical_artifacts_after = tree_file_hashes(study_root / "stage1-artifacts")
    historical_unchanged = historical_artifacts_before == historical_artifacts_after
    config_unchanged = sha256_file(config_path) == config_sha256 and lock_path.read_text(encoding="utf-8").strip() == lock_value

    witness_process_pass = witness_result is not None and witness_result.returncode == 0 and witness_payload is not None
    required_witnesses_pass = bool(criteria) and all(item["passed"] for item in criteria)
    # Full qualification is intentionally fail-closed: the accepted module hash and
    # its runtime source marker must match, and the generic executable mode must pass.
    # The current generic mode is known to depend on an absent study adapter and has
    # exited 139, so a passing privileged subset alone cannot authorize preflight.
    default_mode_pass = default_result is not None and default_result.returncode == 0
    overall_pass = all((
        all(source_checks.values()), config_lock_matches, configure_ok, build_ok,
        pinned_module_hash_matches, module_smoke_pass, witness_process_pass, required_witnesses_pass,
        potion_pass, default_mode_pass, historical_unchanged, config_unchanged,
    ))

    python_version = run_recorded([str(python), "--version"], cwd=study_root, env=env, records=command_records)
    cmake_version = run_recorded(["cmake", "--version"], cwd=study_root, env=env, records=command_records)
    compiler_version = run_recorded(["c++", "--version"], cwd=study_root, env=env, records=command_records)
    own_cpu, child_cpu = cpu_snapshot()
    subprocess_cpu = sum(r["descendant_process_cpu_delta_seconds"] for r in command_records)
    own_script_cpu = sum(r["python_process_cpu_delta_seconds"] for r in command_records)
    measured_current_cpu = round(max(child_cpu, subprocess_cpu) + own_cpu, 6)
    prep_allowance = args.prior_unmetered_preparation_allowance_seconds
    previous_conservative_cpu = round(args.prior_measured_build_cpu_seconds + prep_allowance, 6)
    fixture_work_cpu = round(previous_conservative_cpu + measured_current_cpu, 6)

    recipe = {
        "schema_id": "issue-71-native-fixture-reproduction-v1",
        "issue": "https://github.com/lsmfttb/spire-research/issues/71",
        "study_only": True,
        "formal_preflight_launched": False,
        "formal_preflight_owner_arms_consumed": 0,
        "formal_worker_repeats_consumed": 0,
        "training_or_evaluation_launched": False,
        "generator_source": {
            "study_script_path": script_rel,
            "study_script_sha256": generator_script_sha256,
            "script_source_commit": generator_commit,
            "study_head_at_generation": root_commit_before,
        },
        "source_identity": {"native": native_identity, "stsrl": stsrL_identity, "checks": source_checks},
        "study_config": {
            "config_sha256": config_sha256,
            "config_lock_value": lock_value,
            "lock_matches_before": config_lock_matches,
            "config_unchanged_after": config_unchanged,
        },
        "toolchain": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python_executable": str(python),
            "python_version_output": python_version.stdout.decode("utf-8", errors="replace").strip(),
            "cmake_version_output": cmake_version.stdout.decode("utf-8", errors="replace").splitlines()[0] if cmake_version.stdout else "",
            "compiler_version_output": compiler_version.stdout.decode("utf-8", errors="replace").splitlines()[0] if compiler_version.stdout else "",
            "pythonhome": env["PYTHONHOME"],
            "pythonpath": env["PYTHONPATH"],
            "numpy_torch_versions": "not imported by native fixture generator; pinned runtime separately recorded as NumPy 2.4.6 and Torch 2.12.0+cpu",
        },
        "expected_reproduction_command": [
            str(python), script_rel,
            "--native-source", str(native_source),
            "--stsrL-source", str(stsrL_source),
            "--python", str(python),
            "--pinned-module", str(pinned_module),
            "--build-dir", str(build_dir),
            "--study-root", str(study_root),
            "--study-script-source-commit", generator_commit,
            "--study-parent-commit", root_commit_before,
            "--prior-measured-build-cpu-seconds", str(args.prior_measured_build_cpu_seconds),
            "--prior-unmetered-preparation-allowance-seconds", str(prep_allowance),
        ],
        "source_override_compile_define": f"STS_LIGHTSPEED_TESTED_SOURCE_COMMIT=\"{NATIVE_COMMIT}\"",
        "required_build_targets": ["slaythespire", "test-public-noncombat-projection", "test-potion-semantics"],
        "focused_fixture_command": [str(build_dir / "test-public-noncombat-projection"), "--privileged-noncombat-only"],
        "additional_regression_command": [str(build_dir / "test-potion-semantics")],
        "additional_generic_mode_diagnostic": [str(build_dir / "test-public-noncombat-projection")],
        "native_module": {
            "loaded_module_path": str(pinned_module),
            "loaded_module_sha256": pinned_module_sha256,
            "loaded_module_matches_accepted_binary_pin": pinned_module_hash_matches,
            "expected_filename": EXPECTED_MODULE_NAME,
            "fresh_build_module_path": str(module) if module else None,
            "expected_sha256": NATIVE_MODULE_SHA256,
            "fresh_build_module_sha256": module_sha256,
            "fresh_build_module_matches_accepted_binary_pin": module_hash_matches,
        },
        "historical_failed_attempt_artifacts": {
            "stage1_artifact_files_before": historical_artifacts_before,
            "stage1_artifact_files_after": historical_artifacts_after,
            "all_unchanged": historical_unchanged,
            "prior_failure_debit_cpu_seconds": 3.38108897,
            "prior_failure_receipt_sha256": "bad155de9983d023c40c0b4ed262243f007e4ba578ea635ba760d0fe256c4432",
        },
        "resource_accounting": {
            "prior_measured_build_cpu_seconds": args.prior_measured_build_cpu_seconds,
            "prior_unmetered_preparation_allowance_seconds": prep_allowance,
            "prior_builder_fixture_cpu_conservative_seconds": previous_conservative_cpu,
            "current_generator_and_subprocess_cpu_seconds": measured_current_cpu,
            "current_script_self_cpu_seconds": round(own_cpu, 6),
            "current_descendant_process_cpu_seconds": round(child_cpu, 6),
            "current_record_sum_subprocess_cpu_seconds": round(subprocess_cpu, 6),
            "current_record_sum_script_cpu_delta_seconds": round(own_script_cpu, 6),
            "this_builder_fixture_work_cpu_conservative_seconds": fixture_work_cpu,
            "builder_allocation_ceiling_cpu_seconds": 52000,
            "reviewer_reserve_cpu_seconds": 8000,
            "overall_cumulative_cpu_ceiling_seconds": 60000,
        },
        "commands": command_records,
    }
    recipe_path = study_root / "native-fixture-reproduction.json"
    recipe_path.write_text(json.dumps(recipe, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    recipe_sha256 = sha256_file(recipe_path)

    limitations = [
        "The required privileged noncombat source fixture covers its named native Colosseum and Match-and-Keep witnesses, but the generic no-argument test mode exited by signal 11 (shell status 139) with empty stdout/stderr. The default source path includes a study-adapter boundary check; the study root has no adapter.py, but the signal cause was not isolated.",
        "Because the broader generic test mode did not pass, overall qualification is false and the registered preflight remains blocked pending the required independent focused review; the signal does not establish whether the named fixture semantics failed.",
        "No live CommunicationMod parity was tested; real-game parity remains unverified, Prismatic Shard remains restricted, and Mysterious Sphere/Golden Idol scaling remains unresolved.",
        "The focused native suite is semantic fixture evidence only. It is not a natural experimental owner, an RL outcome, or permission to train/evaluate.",
    ]
    evidence = {
        "schema_id": "issue-71-native-fixture-evidence-v1",
        "issue": "https://github.com/lsmfttb/spire-research/issues/71",
        "native_source_commit": native_identity["head_commit"],
        "native_source_tree": native_identity["head_tree"],
        "native_module_sha256": pinned_module_sha256,
        "fresh_build_native_module_sha256": module_sha256,
        "expected_native_module_sha256": NATIVE_MODULE_SHA256,
        "passed": bool(overall_pass),
        "required_witness_checks_passed": bool(required_witnesses_pass and potion_pass and witness_process_pass),
        "qualification_scope": "exact-source native privileged noncombat semantic fixtures; study-only preflight gate",
        "formal_preflight_owner_arms_consumed": 0,
        "formal_worker_repeats_consumed": 0,
        "formal_training_or_evaluation_seeds_consumed": 0,
        "training_or_evaluation_ran": False,
        "checks": {
            "native_source_and_submodule_identity": source_checks,
            "study_config_lock_matches": config_lock_matches,
            "native_configure_passed": configure_ok,
            "native_build_passed": build_ok,
            "built_native_module_matches_accepted_sha256": module_hash_matches,
            "pinned_native_module_matches_accepted_sha256": pinned_module_hash_matches,
            "loaded_native_module_reports_exact_source_commit": module_smoke_pass,
            "privileged_noncombat_fixture_exited_zero": witness_process_pass,
            "named_colosseum_and_match_and_keep_witnesses": criteria,
            "potion_semantics_regression_passed": potion_pass,
            "generic_no_argument_mode_passed": default_mode_pass,
            "historical_failure_artifacts_unchanged": historical_unchanged,
            "study_config_unchanged": config_unchanged,
        },
        "native_test_records": {
            "pinned_module_source_marker": module_smoke_result.stdout.decode("utf-8", errors="replace") if module_smoke_result else None,
            "privileged_noncombat_fixture": next((r for r in command_records if r["argv"] and r["argv"][0].endswith("test-public-noncombat-projection") and len(r["argv"]) == 2), None),
            "potion_semantics": next((r for r in command_records if r["argv"] and r["argv"][0].endswith("test-potion-semantics")), None),
            "generic_no_argument_mode": next((r for r in command_records if r["argv"] and r["argv"][0].endswith("test-public-noncombat-projection") and len(r["argv"]) == 1), None),
            "witness_parse_error": witness_parse_error,
            "pinned_module_smoke_error": module_smoke_error,
        },
        "pinned_module_source_marker_payload": module_smoke_payload,
        "native_witness_payload": witness_payload,
        "potion_semantics_stdout": potion_result.stdout.decode("utf-8", errors="replace") if potion_result else None,
        "limitations": limitations,
        "reproduction_receipt": {
            "path": "native-fixture-reproduction.json",
            "sha256": recipe_sha256,
            "generator_script_sha256": generator_script_sha256,
            "generator_script_source_commit": generator_commit,
        },
    }
    evidence_path = study_root / "native-fixture-evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "evidence_path": str(evidence_path),
        "reproduction_receipt_path": str(recipe_path),
        "passed": overall_pass,
        "required_witness_checks_passed": required_witnesses_pass,
        "native_module_sha256": module_sha256,
        "fixture_work_cpu_conservative_seconds": fixture_work_cpu,
        "reproduction_receipt_sha256": recipe_sha256,
        "evidence_sha256": sha256_file(evidence_path),
    }, indent=2, sort_keys=True))
    return 0 if overall_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
