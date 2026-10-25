"""Issue 73 Stage 1 O/C/E runner; formal execution is locked off by default."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from typing import Any, Mapping

import protocol


HERE = Path(__file__).resolve().parent
ARTIFACT_ROOT = HERE / "artifacts"
CANONICAL_RUN_ROOT = ARTIFACT_ROOT / "stage1-formal-oce-v1"


def verify_local_source_lock(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Check the complete local lock and its committed Git blobs before activation."""
    lock_path = HERE / "source-lock.json"
    try:
        source_lock = json.loads(protocol._normalized_text(lock_path))
    except (OSError, json.JSONDecodeError) as exc:
        raise protocol.ContractError("Stage 1 preparation source lock is missing or malformed") from exc
    if source_lock.get("schema_id") != "issue73-stage1-preparation-source-lock-v1":
        raise protocol.ContractError("Stage 1 preparation source-lock schema mismatch")
    required = {"config.json", "config.lock.sha256", "protocol.py", "runner.py",
                "test_stage1_preparation.py", "preparation-resource-receipt.json"}
    files = source_lock.get("files")
    if not isinstance(files, Mapping) or set(files) != required:
        raise protocol.ContractError("Stage 1 preparation source-lock membership mismatch")
    for rel, expected in sorted(files.items()):
        path = HERE / rel
        if not path.is_file() or protocol.sha256_file(path) != expected:
            raise protocol.ContractError(f"Stage 1 preparation source lock mismatch: {rel}")
    if config is None:
        config = protocol.read_config()
    else:
        locked_config = protocol.read_config()
        if dict(config) != locked_config:
            raise protocol.ContractError("formal runner config differs from its locked on-disk config")
    repo = HERE.parents[1]
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    package_rel = HERE.resolve().relative_to(repo.resolve()).as_posix()
    manifest_rel = f"{package_rel}/source-lock.json"
    try:
        committed_manifest = subprocess.check_output(["git", "-C", str(repo), "show", f"{head}:{manifest_rel}"])
    except subprocess.CalledProcessError as exc:
        raise protocol.ContractError("Stage 1 source lock is not present in the current commit") from exc
    if protocol.sha256_bytes(committed_manifest) != protocol.sha256_file(lock_path):
        raise protocol.ContractError("Stage 1 source lock differs from its committed Git blob")
    for rel, expected in sorted(files.items()):
        try:
            committed = subprocess.check_output(["git", "-C", str(repo), "show", f"{head}:{package_rel}/{rel}"])
        except subprocess.CalledProcessError as exc:
            raise protocol.ContractError(f"locked package file is absent from current commit: {rel}") from exc
        if protocol.sha256_bytes(committed) != expected:
            raise protocol.ContractError(f"locked Git blob differs from source lock: {rel}")
    if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor",
                       config["source"]["spire_base_commit"], head], check=False).returncode != 0:
        raise protocol.ContractError("current study checkout does not descend from the registered remote source")
    return {"head": head, "source_lock_sha256": protocol.sha256_file(lock_path),
            "locked_files": dict(files)}


def _activation_is_enabled(config: Mapping[str, Any]) -> bool:
    resource = config.get("resource_proposal_not_authorization", {})
    formal = config.get("formal_execution", {})
    flags = (config.get("stage1_authorized"), config.get("stage1_formal_execution_authorized"),
             formal.get("authorized", False), resource.get("formal_cpu_authorized"))
    if all(value is False for value in flags):
        return False
    if all(value is True for value in flags):
        _require_formal_authorization(config)
        return True
    raise protocol.ContractError("Stage 1 activation fields must be committed together")


def verify_package(*, config: Mapping[str, Any] | None = None, native_root: str | None = None,
                   stsr_root: str | None = None, native_module: str | None = None,
                   require_formal_authorization: bool = False) -> dict[str, Any]:
    local = verify_local_source_lock(config)
    config = protocol.read_config()
    stage0 = protocol.verify_stage0_contract(config)
    resource_receipt = protocol.verify_preparation_resource_receipt(
        HERE / "preparation-resource-receipt.json", config)
    seeds = protocol.owner_seeds(config)
    if len(set(seeds) & set(range(173730001, 173730009))):
        raise protocol.ContractError("Stage 1 owner seeds overlap Stage 0 debug seeds")
    if len(set(seeds) & set(range(7410001, 7410513)) or set(seeds) & set(range(7420001, 7420097))):
        raise protocol.ContractError("Stage 1 owner seeds overlap Issue 71 training/evaluation seeds")
    activated = _activation_is_enabled(config)
    if require_formal_authorization and not activated:
        raise protocol.ContractError("STAGE1_NOT_AUTHORIZED: no formal owner or training seed was consumed")
    boundary = config.get("information_boundary", {})
    if (boundary.get("normal_public_api_safe") is not False
            or boundary.get("public_deployment") is not False
            or boundary.get("student_training") is not False
            or boundary.get("persist_raw_privileged_projection_or_native_state") is not False):
        raise protocol.ContractError("Stage 1 package violates the study-only privileged-information boundary")
    present = (native_root is not None, stsr_root is not None, native_module is not None)
    if any(present) and not all(present):
        raise protocol.ContractError("provide all three pinned source paths or none")
    external = (protocol.verify_external_sources(config, native_root, stsr_root, native_module)
                if all(present) else None)
    return {"status": "PREPARATION_PACKAGE_LOCKS_PASS", "activation_state": "AUTHORIZED" if activated else "DISABLED",
            "stage1_authorized": activated, "formal_owner_seeds_consumed": 0, "source": stage0,
            "local_source": local, "resource_accounting": resource_receipt["accounting"],
            "external_sources": external, "owner_count": len(seeds),
            "arms": list(config["arms"]["order"]),
            "config_sha256": protocol.sha256_file(protocol.CONFIG_PATH)}


def _require_formal_authorization(config: Mapping[str, Any]) -> float:
    """Fail closed unless a separately committed Planner decision enables the formal run."""
    resource = config.get("resource_proposal_not_authorization", {})
    formal = config.get("formal_execution", {})
    if (config.get("stage1_authorized") is not True
            or config.get("stage1_formal_execution_authorized") is not True
            or formal.get("authorized") is not True
            or resource.get("formal_cpu_authorized") is not True):
        raise protocol.ContractError("STAGE1_NOT_AUTHORIZED: no formal owner or training seed was consumed")
    cap = formal.get("cpu_budget_seconds")
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not 0 < cap <= 1_324_800:
        raise protocol.ContractError("future activation must commit an explicit formal CPU budget within the preregistered cap")
    return float(cap)


def _canonical_formal_output(path: Path | None) -> Path:
    expected = Path(os.path.abspath(CANONICAL_RUN_ROOT))
    requested = expected if path is None else Path(os.path.abspath(path))
    if requested != expected:
        raise protocol.ContractError(f"formal execution uses one canonical no-rerun directory: {expected}")
    for candidate in (ARTIFACT_ROOT, expected):
        is_junction = getattr(candidate, "is_junction", None)
        if candidate.is_symlink() or (callable(is_junction) and is_junction()):
            raise protocol.ContractError("formal output path cannot use a symlink or junction")
    return expected

def _load_stage0_runner() -> Any:
    stage0_dir = protocol.STAGE0
    sys.path.insert(0, str(stage0_dir))
    return importlib.import_module("runner")


def _stage1_controller_factory(stage0: Any, *, config: Mapping[str, Any], out: Path, module: Any, model: Any,
                               model_digest: str, encoder: Any, owner_seed: int,
                               oracle_decisions: list[dict[str, Any]], oracle_attempts: list[dict[str, Any]],
                               formal_cpu_cap: float, charged_prior_cpu: float,
                               phase_cpu_start: float) -> Any:
    original = stage0.StudyController

    class OracleController:
        """O arm: C fallback at every decision, replaced only by the locked full-state rule."""
        def __init__(self, **kwargs: Any):
            if kwargs.get("arm") != "O":
                raise protocol.ContractError("OracleController factory only accepts arm O")
            base_kwargs = dict(kwargs)
            base_kwargs["arm"] = "SUFFIX"
            self.base = original(**base_kwargs)
            self.module, self.owner_seed = module, owner_seed
            self.out, self.model, self.model_digest, self.encoder = out, model, model_digest, encoder
            self.oracle_decisions, self.oracle_attempts = oracle_decisions, oracle_attempts
            self.query_count = 0
            self.provenance = self.base.provenance
            # O has its own provenance while retaining the exact baseline policy machinery.
            from sts_combat_rl.sim.controller_contract import ControllerProvenance
            self.provenance = ControllerProvenance(
                kind="issue73_stage1_controller", name="issue73_privileged_oracle_O_v1",
                config={"study_id": protocol.read_config()["study_id"],
                        "suffix": "pi_suffix_v1", "selection": "frozen_2se_abstain",
                        "native_source_commit": stage0.EXPECTED_NATIVE})
            for name in ("trace", "roots", "screen_counts", "action_kind_counts", "exposures",
                         "privacy_counts", "captured_checkpoint_screens"):
                setattr(self, name, getattr(self.base, name))
            self.capture_checkpoints = self.base.capture_checkpoints
            self.capture_all_roots = self.base.capture_all_roots
            self.capture_trace = self.base.capture_trace
            self.run_id = None

        def reset_for_run(self, seed: int | None) -> None:
            self.base.reset_for_run(seed)

        def _restore_and_verify(self, adapter: Any, snapshot: Any, checkpoint: Any,
                                fingerprints: Mapping[str, str], legal_ids: list[Any]) -> None:
            restored = adapter.restore_checkpoint(checkpoint)
            # The outer controlled-run still owns its snapshot. It must remain exactly
            # the same root after each paused suffix simulation.
            current = snapshot if snapshot is not None else restored
            actual = stage0._snapshot_debug_fingerprints(adapter, current)
            if dict(actual) != dict(fingerprints):
                raise protocol.ContractError("suffix restore changed the live O decision root")
            from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions
            current_ids = action_identity_dicts_for_actions(adapter.legal_actions(current))
            if current_ids != legal_ids:
                raise protocol.ContractError("suffix restore changed or reordered live legal occurrences")

        def select_action(self, adapter: Any, snapshot: Any, actions: Any, context: Any, step_index: int) -> Any:
            from sts_combat_rl.sim.controller_contract import ControllerDecision
            from sts_combat_rl.sim.online_controller import is_battle_state
            from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions

            # The C policy supplies the O fallback and the unchanged Battle Search-v2 choice.
            fallback = self.base.select_action(adapter, snapshot, actions, context, step_index)
            if is_battle_state(snapshot.raw, context.screen_state) or len(actions) <= 1:
                return fallback
            if self.query_count >= 6:
                self.oracle_decisions.append({"step": int(step_index), "fallback": True,
                                              "reason": "query_budget_exhausted",
                                              "fallback_index": int(fallback.selected_index)})
                return fallback
            if len(actions) > 14:
                self.oracle_decisions.append({"step": int(step_index), "fallback": True,
                                              "reason": "candidate_count_over_cap",
                                              "fallback_index": int(fallback.selected_index),
                                              "candidate_count": len(actions)})
                return fallback
            if list(context.eligible_action_indices) != list(range(len(actions))):
                raise protocol.ContractError("full native legal-action occurrence set was not eligible")

            sim = stage0.native_simulator(adapter)
            projection = self.module.PrivilegedNoncombatProjectionAccess(
                sim, "training_oracle_only").project_v1()
            if projection.get("native_source_commit") != stage0.EXPECTED_NATIVE:
                raise protocol.ContractError("privileged candidate projection has a wrong native source")
            stage0.validate_live_native_candidate_bindings(projection, actions)
            legal_ids = action_identity_dicts_for_actions(actions)
            # Stage 0's live-binding validator has checked each source-ordered
            # projection occurrence against these exact legal action identities.
            candidate_ids = list(legal_ids)
            protocol.validate_live_occurrences(candidate_ids, legal_ids, int(fallback.selected_index))
            checkpoint = adapter.capture_checkpoint(snapshot)
            fingerprints = stage0._snapshot_debug_fingerprints(adapter, snapshot)
            native_screen = str(self.base.trace[-1].get("native_screen", context.screen_state))
            query_index = self.query_count
            self.query_count += 1

            def sample(candidate: int, replicate: int, seed: int, cap: float) -> Mapping[str, Any]:
                label = f"O{owner_seed}_D{step_index}_A{candidate}_R{replicate}"
                started = stage0.process_tree_cpu()
                stage0.append_jsonl(self.out / "coarse-state.jsonl", {
                    "study_id": config["study_id"], "boundary": "oracle_suffix_attempt_started",
                    "run_id": label, "owner_seed": owner_seed,
                    "decision_step": int(step_index), "candidate_occurrence": candidate,
                    "replicate": replicate, "attempt_seed": seed,
                    "native_source_commit": stage0.EXPECTED_NATIVE})
                try:
                    remaining = formal_cpu_cap - charged_prior_cpu - (stage0.process_tree_cpu() - phase_cpu_start)
                    effective_cap = min(cap, remaining)
                    if effective_cap <= 0:
                        raise protocol.ContractError("formal Stage 1 cumulative CPU authorization exhausted before suffix start")
                    result = stage0._run_one(
                        out=self.out, module=self.module, model=self.model, model_digest=self.model_digest,
                        seed=seed, arm="SUFFIX", encoder=self.encoder, checkpoint=checkpoint,
                        forced_root={"root_fingerprints": dict(fingerprints),
                                     "screen": native_screen,
                                     "legal_action_identities": legal_ids,
                                     "candidate_index": candidate},
                        purpose=f"issue73-stage1-O-{owner_seed}-{step_index}-{candidate}-{replicate}",
                        resource_label=label, adapter_override=adapter, durable=False,
                        capture_trace=False, cpu_cap_seconds=effective_cap,
                        suffix_random_seed=protocol.continuation_seed(owner_seed, step_index, candidate, replicate, stream="suffix"),
                        expert_random_seed=protocol.continuation_seed(owner_seed, step_index, candidate, replicate, stream="expert"),
                    )
                    forced = result.get("forced_root_receipt")
                    if (not isinstance(forced, Mapping)
                            or forced.get("forced_root_consumed") is not True
                            or forced.get("candidate_index") != candidate):
                        raise protocol.ContractError("suffix did not consume the exact locked legal root occurrence")
                    record = {"owner_seed": owner_seed, "decision_step": int(step_index),
                              "candidate_occurrence": candidate, "replicate": replicate,
                              "seed": seed, "terminal_complete": result.get("terminal_complete"),
                              "terminal": result.get("terminal"), "native_source_commit": result.get("native_source_commit"),
                              "cpu_seconds": result.get("cpu_seconds"),
                              "wall_seconds": result.get("wall_seconds"),
                              "forced_root_receipt": dict(forced), "run_id": label}
                    self.oracle_attempts.append(record)
                    stage0.append_jsonl(self.out / "coarse-state.jsonl", {
                        "study_id": config["study_id"], "boundary": "oracle_suffix_attempt_terminal",
                        "run_id": label, "terminal_floor": record["terminal"].get("terminal_floor"),
                        "cpu_seconds": record.get("cpu_seconds"),
                        "native_source_commit": record.get("native_source_commit")})
                    return record
                except BaseException as exc:
                    failure = {"owner_seed": owner_seed, "decision_step": int(step_index),
                               "candidate_occurrence": candidate, "replicate": replicate,
                               "seed": seed, "terminal_complete": False,
                               "native_source_commit": stage0.EXPECTED_NATIVE,
                               "cpu_seconds": max(0.0, stage0.process_tree_cpu() - started),
                               "run_id": label, "failure_type": type(exc).__name__,
                               "failure": str(exc)[:800]}
                    self.oracle_attempts.append(failure)
                    stage0.append_jsonl(self.out / "coarse-state.jsonl", {
                        "study_id": config["study_id"], "boundary": "oracle_suffix_attempt_failed",
                        "run_id": label, "failure_type": failure["failure_type"],
                        "cpu_seconds": failure["cpu_seconds"]})
                    raise
                finally:
                    try:
                        self._restore_and_verify(adapter, snapshot, checkpoint, fingerprints, legal_ids)
                    except BaseException as exc:
                        stage0.append_jsonl(self.out / "coarse-state.jsonl", {
                            "study_id": config["study_id"], "boundary": "oracle_root_restore_failed",
                            "run_id": label, "failure_type": type(exc).__name__, "failure": str(exc)[:800]})
                        raise

            choice, attempts = protocol.evaluate_candidate_grid(
                len(actions), owner_seed=owner_seed, decision_index=int(step_index),
                fallback_index=int(fallback.selected_index), sampler=sample,
                expected_native_commit=stage0.EXPECTED_NATIVE, query_index=query_index)
            # Each fixed attempt is recorded by sample before validation; this list is
            # intentionally also returned for a complete decision receipt.
            if attempts and attempts != self.oracle_attempts[-len(attempts):]:
                raise protocol.ContractError("Oracle continuation receipt sequence changed")
            selected = protocol.validate_live_occurrences(candidate_ids, legal_ids, choice.chosen_index)
            row = protocol.oracle_decision_receipt(
                step=int(step_index), query_index=query_index, candidate_count=len(actions),
                fallback_index=int(fallback.selected_index), selected_index=selected,
                screen=str(context.screen_state), choice=choice, fingerprints=fingerprints,
                legal_ids=legal_ids, attempt_count=len(attempts))
            self.oracle_decisions.append(row)
            # Preserve the C trace's other diagnostics, but make its final action
            # reflect the actual O decision and retain the sampled C fallback.
            if self.trace and isinstance(self.trace[-1], dict):
                self.trace[-1]["c_fallback_index"] = int(fallback.selected_index)
                self.trace[-1]["chosen_index"] = selected
                self.trace[-1]["issue73_stage1_oracle"] = {
                    "fallback": choice.fallback, "reason": choice.reason,
                    "query_index": query_index}
            return ControllerDecision(selected_index=selected, provenance=self.provenance,
                                     reason="stage1_c_fallback" if choice.fallback else "stage1_privileged_oracle",
                                     metadata={"study_arm": "O", "fallback": choice.fallback,
                                               "fallback_index": int(fallback.selected_index),
                                               "oracle_reason": choice.reason})

    def factory(**kwargs: Any) -> Any:
        if kwargs.get("arm") == "O":
            return OracleController(**kwargs)
        return original(**kwargs)

    return factory


def _write_owner_arm_receipt(out: Path, seed: int, arm: str, result: Mapping[str, Any]) -> None:
    arm_dir = out / "arms"
    arm_dir.mkdir(parents=True, exist_ok=True)
    protocol.atomic_json(arm_dir / f"{seed}_{arm}.json", result)


def analyze_complete_owners(config: Mapping[str, Any], out: Path,
                            source_identity: Mapping[str, Any]) -> dict[str, Any]:
    """Aggregate only a complete, exact O/C/E cohort; this consumes no owner seeds."""
    owners: list[dict[str, Any]] = []
    for seed in protocol.owner_seeds(config):
        path = out / "owners" / f"{seed}.complete.json"
        if not path.is_file():
            raise protocol.ContractError(f"cannot analyze incomplete Stage 1 cohort; missing owner {seed}")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("status") != "COMPLETE" or record.get("seed") != seed:
            raise protocol.ContractError(f"owner completion receipt is invalid: {path}")
        arms = record.get("arms")
        protocol.validate_owner_block(arms)
        for arm in protocol.ARMS:
            if arms[arm].get("registered_arm") != arm:
                raise protocol.ContractError(f"owner {seed} arm receipt mismatch: {arm}")
        owners.append(arms)
    report = {"status": "STAGE1_OCE_COMPLETE", "stage1_authorized": True,
              "owner_count": len(owners), "arm_counts": {arm: len(owners) for arm in protocol.ARMS},
              "primary_O_minus_C": protocol.bootstrap_percentile(protocol.paired_differences(owners, arm="C")),
              "secondary_O_minus_E": protocol.bootstrap_percentile(protocol.paired_differences(owners, arm="E")),
              "owner_arm_cpu_seconds": sum(float(owner[arm]["cpu_seconds"])
                                            for owner in owners for arm in protocol.ARMS),
              "owner_all_in_cpu_seconds": sum(float(json.loads((out / "owners" / f"{seed}.complete.json")
                  .read_text(encoding="utf-8")).get("cpu_seconds", 0.0)) for seed in protocol.owner_seeds(config)),
              "setup_cpu_seconds": sum(float(json.loads(path.read_text(encoding="utf-8")).get("cpu_seconds", 0.0))
                                       for path in (out / "setup-attempts").glob("*.complete.json")),
              "source_identity": dict(source_identity)}
    report["formal_total_cpu_seconds"] = report["owner_all_in_cpu_seconds"] + report["setup_cpu_seconds"]
    analysis_path = out / "analysis.json"
    if analysis_path.exists():
        existing = json.loads(analysis_path.read_text(encoding="utf-8"))
        if existing != report:
            raise protocol.ContractError("existing analysis differs from complete durable owner receipts")
    else:
        protocol.atomic_json(analysis_path, report)
    complete_path = out / "phase.complete.json"
    complete = {"status": report["status"], "owner_count": len(owners),
                "completed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "formal_seed_range": [min(protocol.owner_seeds(config)), max(protocol.owner_seeds(config))]}
    if complete_path.exists():
        if json.loads(complete_path.read_text(encoding="utf-8")).get("status") != report["status"]:
            raise protocol.ContractError("existing phase completion receipt is inconsistent")
    else:
        protocol.atomic_json(complete_path, complete)
    return report


def run_formal(config: Mapping[str, Any], out: Path, *, native_module_path: str,
               native_root: str, stsr_root: str) -> dict[str, Any]:
    """Execute only after activation, source, environment, and output identity are locked."""
    out = _canonical_formal_output(out)
    verify_package(config=config, native_root=native_root, stsr_root=stsr_root,
                   native_module=native_module_path, require_formal_authorization=True)
    formal_cpu_cap = _require_formal_authorization(config)
    if out.exists() and (out / "phase.complete.json").is_file():
        environment = json.loads((out / "environment.json").read_text(encoding="utf-8"))
        return analyze_complete_owners(config, out, environment["identity"])
    out.mkdir(parents=True, exist_ok=True)
    phase_start_path = out / "phase.started.json"
    if phase_start_path.exists():
        previous_phase = json.loads(phase_start_path.read_text(encoding="utf-8"))
        if (previous_phase.get("config_sha256") != protocol.sha256_file(protocol.CONFIG_PATH)
                or previous_phase.get("formal_cpu_budget_seconds") != formal_cpu_cap):
            raise protocol.ContractError("resume config/budget differs from the immutable phase start")
    else:
        protocol.atomic_json(phase_start_path, {
            "schema_id": "issue73-stage1-phase-start-v1", "status": "STARTED_NO_RERUN",
            "config_sha256": protocol.sha256_file(protocol.CONFIG_PATH),
            "formal_cpu_budget_seconds": formal_cpu_cap,
            "formal_owner_seeds": list(protocol.owner_seeds(config)),
            "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    import_cpu_start = time.process_time()
    stage0 = _load_stage0_runner()
    os.environ["STS_LIGHTSPEED_SOURCE_DIR"] = str(Path(native_root).resolve(strict=True))
    os.environ["STSRL_SOURCE_DIR"] = str(Path(stsr_root).resolve(strict=True))
    setup_dir = out / "setup-attempts"
    setup_dir.mkdir(parents=True, exist_ok=True)
    starts = sorted(setup_dir.glob("*.started.json"))
    setup_attempt = len(starts) + 1
    if any(not item.with_name(item.name.replace(".started.json", ".complete.json")).exists()
           for item in starts):
        raise protocol.ContractError("an environment setup attempt was interrupted; no automatic setup replay")
    setup_label = f"{setup_attempt:04d}"
    setup_start = setup_dir / f"{setup_label}.started.json"
    setup_complete = setup_dir / f"{setup_label}.complete.json"
    setup_started_cpu = stage0.process_tree_cpu()
    protocol.atomic_json(setup_start, {"status": "STARTED", "native_seed_consumed": False,
                                      "config_sha256": protocol.sha256_file(protocol.CONFIG_PATH)})
    try:
        env, module, encoder, runtime = stage0._setup(out, native_module_path=native_module_path)
    except BaseException as exc:
        protocol.atomic_json(setup_dir / f"{setup_label}.failure.json", {
            "status": "FAILED_NO_OWNER_SEED", "failure_type": type(exc).__name__,
            "failure": str(exc)[:1000], "cpu_seconds": max(0.0, stage0.process_tree_cpu() - setup_started_cpu)})
        raise
    del runtime
    model, model_digest = stage0._load_policy(Path("unused"), initial=True)
    arms_by_seed: dict[int, dict[str, Any]] = {}
    setup_cpu = max(0.0, stage0.process_tree_cpu() - import_cpu_start)
    protocol.atomic_json(setup_complete, {"status": "COMPLETE", "native_seed_consumed": False,
                                        "cpu_seconds": setup_cpu, "source_identity": env["identity"]})
    completed_owner_cpu = 0.0
    owners_dir = out / "owners"
    if owners_dir.exists():
        for receipt in owners_dir.glob("*.complete.json"):
            record = json.loads(receipt.read_text(encoding="utf-8"))
            completed_owner_cpu += float(record.get("cpu_seconds", 0.0))
    completed_setup_cpu = sum(float(json.loads(path.read_text(encoding="utf-8")).get("cpu_seconds", 0.0))
                              for path in setup_dir.glob("*.complete.json"))
    charged_prior_cpu = completed_owner_cpu + completed_setup_cpu
    if charged_prior_cpu >= formal_cpu_cap:
        raise protocol.ContractError("formal Stage 1 cumulative CPU authorization exhausted before next owner")
    phase_cpu_start = stage0.process_tree_cpu()
    for seed in protocol.owner_seeds(config):
        owner_start = out / "owners" / f"{seed}.started.json"
        owner_result_path = out / "owners" / f"{seed}.complete.json"
        owner_failure_path = out / "owners" / f"{seed}.failure.json"
        if owner_result_path.exists():
            record = json.loads(owner_result_path.read_text(encoding="utf-8"))
            if record.get("status") != "COMPLETE" or record.get("seed") != seed:
                raise protocol.ContractError(f"durable owner receipt is invalid; no rerun: {owner_result_path}")
            protocol.validate_owner_block(record.get("arms"))
            arms_by_seed[seed] = dict(record["arms"])
            continue
        if owner_failure_path.exists():
            raise protocol.ContractError(f"owner has a durable failure receipt; no retry: {owner_failure_path}")
        if owner_start.exists():
            # This seed may have been interrupted after starting. Preserve that
            # boundary and stop; no retry, omission, replacement, or later seeds.
            protocol.atomic_json(owner_failure_path, {"status": "INTERRUPTED_NO_RERUN",
                "seed": seed, "failure_boundary": "owner_started_without_terminal_receipt",
                "formal_seed_consumed": True,
                "coarse_state_path": str(out / "coarse-state.jsonl")})
            raise protocol.ContractError(f"owner {seed} started without a complete terminal receipt; no retry")
        if charged_prior_cpu + (stage0.process_tree_cpu() - phase_cpu_start) >= formal_cpu_cap:
            raise protocol.ContractError("formal Stage 1 cumulative CPU authorization exhausted before next owner")
        started_cpu = stage0.process_tree_cpu()
        protocol.atomic_json(owner_start, {"status": "STARTED_NO_RERUN", "seed": seed,
                                          "arms": list(protocol.ARMS), "started_cpu_seconds": started_cpu})
        owner_arms: dict[str, Any] = {}
        oracle_decisions: list[dict[str, Any]] = []
        oracle_attempts: list[dict[str, Any]] = []
        try:
            for arm in protocol.ARMS:
                elapsed_phase_cpu = stage0.process_tree_cpu() - phase_cpu_start
                if charged_prior_cpu + elapsed_phase_cpu >= formal_cpu_cap:
                    raise protocol.ContractError("formal Stage 1 cumulative CPU authorization exhausted")
                factory = _stage1_controller_factory(
                    stage0, config=config, out=out, module=module, model=model, model_digest=model_digest,
                    encoder=encoder, owner_seed=seed, oracle_decisions=oracle_decisions,
                    oracle_attempts=oracle_attempts, formal_cpu_cap=formal_cpu_cap,
                    charged_prior_cpu=charged_prior_cpu, phase_cpu_start=phase_cpu_start)
                original = stage0.StudyController
                stage0.StudyController = factory
                try:
                    remaining_cpu = formal_cpu_cap - charged_prior_cpu - (stage0.process_tree_cpu() - phase_cpu_start)
                    if remaining_cpu <= 0:
                        raise protocol.ContractError("formal Stage 1 cumulative CPU authorization exhausted")
                    result = stage0._run_one(
                        out=out, module=module, model=model, model_digest=model_digest,
                        seed=seed, arm=arm if arm != "C" else "SUFFIX", encoder=encoder,
                        purpose=f"issue73-stage1-{arm}-owner-{seed}",
                        resource_label=f"{seed}_{arm}", durable=False, capture_trace=False,
                        cpu_cap_seconds=min(13560.0 if arm == "O" else 120.0, remaining_cpu))
                finally:
                    stage0.StudyController = original
                if result.get("terminal_complete") is not True:
                    raise protocol.ContractError(f"owner {seed} arm {arm} is not terminal")
                safe_result = protocol.safe_arm_receipt(
                    result, registered_arm=arm,
                    policy_mechanics_arm="SUFFIX" if arm == "C" else arm,
                    oracle_decisions=oracle_decisions if arm == "O" else ())
                owner_arms[arm] = safe_result
                _write_owner_arm_receipt(out, seed, arm, safe_result)
            protocol.validate_owner_block(owner_arms)
            protocol.atomic_json(owner_result_path, {"status": "COMPLETE", "seed": seed,
                                                     "arms": owner_arms,
                                                     "oracle_decisions": oracle_decisions,
                                                     "oracle_attempts": oracle_attempts,
                                                     "cpu_seconds": stage0.process_tree_cpu() - started_cpu,
                                                     "source_identity": env["identity"]})
            arms_by_seed[seed] = owner_arms
        except BaseException as exc:
            protocol.atomic_json(owner_failure_path, {"status": "FAILED_NO_RERUN",
                "seed": seed, "failure_type": type(exc).__name__, "failure": str(exc)[:1200],
                "completed_arms": list(owner_arms), "oracle_decisions": oracle_decisions,
                "oracle_attempts": oracle_attempts,
                "cpu_seconds": stage0.process_tree_cpu() - started_cpu,
                "traceback": traceback.format_exc()[-5000:]})
            raise

    return analyze_complete_owners(config, out, env["identity"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true", help="verify committed locks without starting a game")
    modes.add_argument("--run", action="store_true", help="run formally only after a separately committed Planner activation")
    modes.add_argument("--analyze", action="store_true", help="analyze only an already-complete 96-owner O/C/E receipt set")
    parser.add_argument("--native-root", help="exact clean pinned sts_lightspeed checkout")
    parser.add_argument("--stsr-root", help="exact clean pinned STSRL checkout")
    parser.add_argument("--native-module", help="binary whose digest is locked in the Stage 0 source contract")
    parser.add_argument("--output", type=Path, help="canonical Stage-1 run directory, or existing analysis directory")
    args = parser.parse_args(argv)
    try:
        if args.check:
            report = verify_package(native_root=args.native_root, stsr_root=args.stsr_root,
                                    native_module=args.native_module)
            print(json.dumps(report, sort_keys=True))
            return 0
        config = protocol.read_config()
        if args.analyze:
            if not args.output:
                raise protocol.ContractError("--analyze requires an existing study artifact directory")
            _require_formal_authorization(config)
            analysis_out = args.output.resolve()
            if not analysis_out.is_relative_to(ARTIFACT_ROOT.resolve()):
                raise protocol.ContractError("analysis input must be inside this study's artifacts directory")
            environment = json.loads((analysis_out / "environment.json").read_text(encoding="utf-8"))
            report = analyze_complete_owners(config, analysis_out, environment["identity"])
        else:
            if not args.native_module or not args.native_root or not args.stsr_root:
                raise protocol.ContractError("--run requires all pinned source paths after activation")
            run_out = _canonical_formal_output(args.output)
            report = run_formal(config, run_out, native_module_path=args.native_module,
                                native_root=args.native_root, stsr_root=args.stsr_root)
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception as exc:
        consumed = 0
        if args.output is not None:
            owner_dir = args.output / "owners"
            if owner_dir.exists():
                consumed = len(list(owner_dir.glob("*.started.json")))
        print(json.dumps({"status": "BLOCKED_NO_RERUN", "error_type": type(exc).__name__,
                          "error": str(exc), "formal_owner_seeds_started": consumed}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
