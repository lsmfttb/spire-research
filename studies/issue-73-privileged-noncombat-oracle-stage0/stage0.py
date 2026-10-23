"""Integrated Issue 73 Stage 0 gates A/B/C; never launches Stage 1."""
from __future__ import annotations
import argparse, hashlib, json, math, os, pickle, resource, signal, subprocess, sys, time
from pathlib import Path
from typing import Any, Mapping, Sequence
import numpy as np
import gate_a, runner
from policy import AllNoncombatPolicy

STUDY = Path(__file__).resolve().parent
REPOSITORY = STUDY.parent.parent
STUDY_RELATIVE = STUDY.relative_to(REPOSITORY).as_posix()
OUT = STUDY / "receipts" / "stage0"
BRANCH = "codex/issue-73-stage0"
CONFIG, CONFIG_SHA = runner.CONFIG, runner.CONFIG_SHA256
GATE_CEILING = float(CONFIG["resources"]["native_gate_bc_cpu_ceiling_seconds"])
BUILDER_CEILING = float(CONFIG["resources"]["builder_cpu_hard_cap_seconds"])

class Stage0Failure(RuntimeError):
    pass

def git(*args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(REPOSITORY), *args], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode:
        raise Stage0Failure(f"git {' '.join(args)} failed: {p.stderr[-800:]}")
    return p.stdout.strip()

def tree_cpu() -> float:
    own, child = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return float(own.ru_utime + own.ru_stime + child.ru_utime + child.ru_stime)

def source_hashes() -> dict[str, str]:
    names = ("runner.py", "policy.py", "public_history_execution.py", "gate_a.py", "stage0.py", "gate-a-target-direction.json", ".gitignore")
    return {n: runner.sha256_file(STUDY / n) for n in names}

def verify_sources() -> dict[str, str]:
    observed = source_hashes()
    if dict(CONFIG["identity"].get("study_source_sha256", {})) != observed:
        raise Stage0Failure("study source/input hashes differ from the preseed-committed config")
    for env_name, pin, file_hashes in (
        ("STS_LIGHTSPEED_SOURCE_DIR", CONFIG["identity"]["native_commit"], CONFIG["identity"].get("native_source_files_sha256", {})),
        ("STSRL_SOURCE_DIR", CONFIG["identity"]["stsr_commit"], CONFIG["identity"].get("stsr_source_files_sha256", {})),
    ):
        path = os.environ.get(env_name)
        if not path:
            raise Stage0Failure(f"{env_name} is unset")
        head = subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", path, "status", "--porcelain"], text=True).strip()
        if head != pin or dirty:
            raise Stage0Failure(f"{env_name} is not the clean pinned source commit")
        for name, digest in dict(file_hashes).items():
            if runner.sha256_file(Path(path) / name) != digest:
                raise Stage0Failure(f"{env_name}/{name} source hash differs")
    return observed

def commit_push(message: str) -> str:
    git("add", "--", STUDY_RELATIVE)
    staged = [x for x in git("diff", "--cached", "--name-only").splitlines() if x]
    outside = [x for x in staged if x != STUDY_RELATIVE and not x.startswith(STUDY_RELATIVE + "/")]
    if outside:
        raise Stage0Failure(f"refusing to commit staged files outside the Issue 73 study folder: {outside}")
    changed = subprocess.run(["git", "-C", str(REPOSITORY), "diff", "--cached", "--quiet"], check=False).returncode != 0
    if changed:
        git("commit", "-m", message)
    commit = git("rev-parse", "HEAD")
    git("push", "origin", f"HEAD:refs/heads/{BRANCH}")
    ref = git("ls-remote", "origin", f"refs/heads/{BRANCH}").split()
    if not ref or ref[0] != commit:
        raise Stage0Failure("remote branch is not at the exact pushed commit")
    return commit

def atomic_pickle(path: Path, value: Any) -> dict[str, Any]:
    try:
        blob = pickle.dumps(value, protocol=5)
        restored = pickle.loads(blob)
        if getattr(restored, "checkpoint_id", None) != getattr(value, "checkpoint_id", None):
            raise ValueError("checkpoint id changed on pickle roundtrip")
        runner.atomic_bytes(path, blob)
        if path.read_bytes() != blob:
            raise IOError("checkpoint bytes changed after durable write")
        return {"status": "SERIALIZED_AND_ROUNDTRIPPED", "path": path.relative_to(STUDY).as_posix(), "sha256": runner.sha256_bytes(blob), "bytes": len(blob), "protocol": 5}
    except BaseException as exc:
        return {"status": "UNSERIALIZABLE", "error_type": type(exc).__name__, "error": str(exc)[:400], "path": None}

def source_tags(projection: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    def add(scope: str, fields: Any, candidate: int | None = None) -> None:
        if isinstance(fields, Mapping):
            for key, item in sorted(fields.items(), key=lambda pair: str(pair[0])):
                if isinstance(item, Mapping):
                    rows.append({"scope": scope, "candidate_index": candidate, "field": str(key), "availability": item.get("availability"), "visibility": item.get("visibility"), "source": item.get("source"), "reason": item.get("reason")})
    add("shared_context", projection.get("shared_context"))
    add("screen_identity", {"screen": projection.get("screen_identity")})
    for i, candidate in enumerate(projection.get("candidates", [])):
        if isinstance(candidate, Mapping): add("candidate", candidate.get("features"), i)
    return rows

def root_id(root: Mapping[str, Any]) -> str:
    return f"root-{int(root['seed'])}-{int(root['decision_index']):04d}"

def persist_root(root: Mapping[str, Any]) -> dict[str, Any]:
    rid = root_id(root)
    cp = root["checkpoint"]
    cp_record = atomic_pickle(OUT / "checkpoints" / f"{rid}.pickle", cp)
    projection = root["projection"]
    receipt = {
        "schema_id": "issue73-native-debug-root-v1", "root_id": rid, "config_sha256": CONFIG_SHA,
        "owner_run_id": root.get("run_id"), "owner_seed": int(root["seed"]), "owner_root_index": int(root["root_index"]),
        "owner_decision_index": int(root["decision_index"]), "policy_decision_index": int(root["policy_decision_index"]),
        "screen": str(root["screen"]), "root_fingerprints": {k: root[k] for k in ("native_state_sha256", "public_projection_sha256", "public_resource_state_sha256")},
        "native_source_commit": CONFIG["identity"]["native_commit"], "native_binary_sha256": CONFIG["identity"]["native_binary_sha256"],
        "projection_digest": root["projection_digest"], "raw_privileged_projection": projection,
        "sanitized_public_projection": root["public_projection"], "source_tag_mapping": source_tags(projection),
        "legal_action_occurrences": root["legal_action_identities"], "selected_owner_occurrence": int(root["candidate_index"]),
        "semantic_feature_matrix": root["feature_matrix"],
        "feature_matrix_sha256": runner.sha256_bytes(np.asarray(root["feature_matrix"], dtype="<f4").tobytes()),
        "candidate_binding_validation_passed": bool(root.get("live_binding_validation_passed")),
        "native_checkpoint": {**cp_record, "checkpoint_id": root.get("native_checkpoint_id"), "adapter_id": root.get("checkpoint_adapter_id"), "metadata": root.get("native_checkpoint_metadata", {})},
        "owner_attempt_status_at_capture": "RUNNING",
    }
    path = OUT / "roots" / f"{rid}.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != receipt:
        raise Stage0Failure(f"immutable root receipt collision: {rid}")
    if not path.exists(): runner.atomic_json(path, receipt)
    runner.append_jsonl(OUT / "coarse-state.jsonl", {"boundary": "native_root_checkpoint_captured", "root_id": rid, "owner_seed": root["seed"], "policy_decision_index": root["policy_decision_index"], "screen": root["screen"], "checkpoint_persisted": cp_record["status"] == "SERIALIZED_AND_ROUNDTRIPPED"})
    commit_push(f"Issue 73: preserve Stage 0 root {rid}")
    return receipt

def load_roots() -> list[dict[str, Any]]:
    rows=[]
    for path in sorted((OUT / "roots").glob("root-*.json")):
        v=json.loads(path.read_text(encoding="utf-8"))
        if v.get("config_sha256") != CONFIG_SHA: raise Stage0Failure(f"{path.name} config lock mismatch")
        rows.append({"root_id":v["root_id"], "run_id":v["owner_run_id"], "seed":v["owner_seed"], "decision_index":v["owner_decision_index"], "policy_decision_index":v["policy_decision_index"], "screen":v["screen"], **v["root_fingerprints"], "projection_digest":v["projection_digest"], "projection":v["raw_privileged_projection"], "public_projection":v["sanitized_public_projection"], "source_tag_mapping":v["source_tag_mapping"], "legal_action_identities":v["legal_action_occurrences"], "candidate_index":v["selected_owner_occurrence"], "feature_matrix":v["semantic_feature_matrix"], "feature_matrix_sha256":v["feature_matrix_sha256"], "live_binding_validation_passed":v["candidate_binding_validation_passed"], "checkpoint_record":v["native_checkpoint"]})
    return sorted(rows, key=lambda r:(int(r["seed"]),int(r["decision_index"])))
def ensure_layout() -> None:
    for name in ("roots", "checkpoints", "attempts", "reports", "plans", "runs", "policies", "progress"):
        (OUT / name).mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_receipt(path: Path, value: Mapping[str, Any], message: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = read_json(path)
        if previous != dict(value):
            state_transition = previous.get("status") == "STARTED" and value.get("status") != "STARTED" and previous.get("key") == value.get("key")
            if not state_transition:
                raise Stage0Failure(f"immutable receipt collision at {path.relative_to(STUDY)}")
            runner.atomic_json(path, value)
    else:
        runner.atomic_json(path, value)
    return commit_push(message)


def ledger() -> dict[str, Any]:
    value = read_json(OUT / "resource-ledger.json", {"schema_id": "issue73-stage0-resource-ledger-v1", "attempts": [], "setup_process_cpu_seconds": 0.0})
    if value.get("schema_id") != "issue73-stage0-resource-ledger-v1":
        raise Stage0Failure("resource ledger schema mismatch")
    return value


def charged_cpu(value: Mapping[str, Any] | None = None) -> float:
    value = value or ledger()
    used = float(value.get("setup_process_cpu_seconds", 0.0))
    for row in value.get("attempts", []):
        if "cpu_charged_seconds" in row:
            used += max(0.0, float(row["cpu_charged_seconds"]))
        elif row.get("status") == "COMPLETE":
            used += max(0.0, float(row.get("measured_process_cpu_seconds", 0.0)))
        else:
            used += max(0.0, float(row.get("reserved_cpu_seconds", 0.0)))
    return used


def start_attempt(key: str, *, kind: str, cpu_reserve: float, native: bool) -> tuple[dict[str, Any], bool]:
    path = OUT / "attempts" / f"{key}.json"
    existing = read_json(path)
    if existing is not None:
        if existing.get("status") == "COMPLETE":
            return existing, False
        if existing.get("status") == "STARTED":
            interrupted = {**existing, "status": "INTERRUPTED_NO_RERUN", "closed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "cpu_charged_seconds": float(existing["reserved_cpu_seconds"]), "failure": "started receipt had no terminal receipt; this fixed attempt will never be rerun"}
            write_receipt(path, interrupted, f"Issue 73: preserve interrupted attempt {key}")
            value = ledger()
            for row in value["attempts"]:
                if row.get("key") == key: row.update(interrupted)
            runner.atomic_json(OUT / "resource-ledger.json", value)
            commit_push(f"Issue 73: debit interrupted attempt {key}")
            return interrupted, False
        raise Stage0Failure(f"attempt {key} has unexpected prior state {existing.get('status')}")
    value = ledger()
    projected = charged_cpu(value) + float(cpu_reserve)
    if projected > BUILDER_CEILING:
        raise Stage0Failure(f"Builder CPU reserve would exceed {BUILDER_CEILING}: projected {projected:.3f}")
    native_used = sum(float(row.get("cpu_charged_seconds", row.get("reserved_cpu_seconds", 0.0))) for row in value["attempts"] if row.get("native"))
    if native and native_used + float(cpu_reserve) > GATE_CEILING:
        raise Stage0Failure(f"native Gate B+C/root reserve would exceed {GATE_CEILING}: projected {native_used + float(cpu_reserve):.3f}")
    row = {"schema_id": "issue73-stage0-attempt-v1", "key": key, "kind": kind, "status": "STARTED", "native": native, "reserved_cpu_seconds": float(cpu_reserve), "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "config_sha256": CONFIG_SHA}
    value["attempts"].append(row)
    runner.atomic_json(OUT / "resource-ledger.json", value)
    write_receipt(path, row, f"Issue 73: start fixed Stage 0 attempt {key}")
    return row, True


def finish_attempt(key: str, *, measured_cpu: float, status: str, details: Mapping[str, Any]) -> dict[str, Any]:
    path = OUT / "attempts" / f"{key}.json"
    previous = read_json(path)
    if not previous or previous.get("status") != "STARTED":
        raise Stage0Failure(f"attempt {key} lacks its pushed STARTED receipt")
    measured = max(0.0, float(measured_cpu))
    exceeded = measured > float(previous["reserved_cpu_seconds"])
    final_status = "FAIL_CPU_RESERVATION_EXCEEDED" if exceeded else status
    charge = measured if final_status == "COMPLETE" or exceeded else max(float(previous["reserved_cpu_seconds"]), measured)
    row = {**previous, **dict(details), "status": final_status, "measured_process_cpu_seconds": measured, "cpu_charged_seconds": charge, "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    write_receipt(path, row, f"Issue 73: finish fixed Stage 0 attempt {key}")
    value = ledger()
    value["attempts"] = [row if x.get("key") == key else x for x in value["attempts"]]
    runner.atomic_json(OUT / "resource-ledger.json", value)
    commit_push(f"Issue 73: account Stage 0 attempt {key}")
    return row


def add_setup_cpu(seconds: float, label: str) -> None:
    value = ledger()
    value["setup_process_cpu_seconds"] = float(value.get("setup_process_cpu_seconds", 0.0)) + max(0.0, seconds)
    value.setdefault("setup_segments", []).append({"label": label, "process_cpu_seconds": max(0.0, seconds)})
    if charged_cpu(value) > BUILDER_CEILING:
        raise Stage0Failure("Builder CPU hard ceiling reached while accounting setup")
    runner.atomic_json(OUT / "resource-ledger.json", value)
    commit_push(f"Issue 73: account Stage 0 setup {label}")

def study_source_inputs() -> dict[str, str]:
    paths = ["runner.py", "policy.py", "public_history_execution.py", "gate_a.py", "stage0.py", "gate-a-target-direction.json", ".gitignore"]
    return {name: runner.sha256_file(STUDY / name) for name in paths}


def pin_external_sources(config: dict[str, Any]) -> None:
    native_root = Path("/home/lsmft/stsrl-spikes/sts_lightspeed-issue71-stage1-8ded-20261022")
    stsr_root = Path("/home/lsmft/stsrl-issue71-source")
    for source, expected in ((native_root, config["identity"]["native_commit"]), (stsr_root, config["identity"]["stsr_commit"])):
        head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip()
        if head != expected or dirty:
            raise Stage0Failure(f"external source checkout is not clean at its exact registered commit: {source} {head}")
    config["identity"]["native_source_files_sha256"] = {
        name: runner.sha256_file(native_root / name) for name in (
            "bindings/slaythespire.cpp",
            "src/game/GameContext.cpp",
            "src/sim/search/GameAction.cpp",
        )
    }
    config["identity"]["stsr_source_files_sha256"] = {
        name: runner.sha256_file(stsr_root / name) for name in (
            "src/sts_combat_rl/sim/lightspeed.py",
            "src/sts_combat_rl/sim/controller_contract.py",
            "src/sts_combat_rl/sim/online_controller.py",
            "src/sts_combat_rl/sim/decision_record.py",
            "src/sts_combat_rl/sim/lightspeed_source.py",
        )
    }


def prepare() -> str:
    config_path = STUDY / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("stage0", {}).get("stage1_authorized") is not False or config.get("resources", {}).get("stage1_budget", {}).get("formal_execution_authorized") is not False:
        raise Stage0Failure("preseed config must keep Stage 1 unauthorized")
    if any((STUDY / "receipts").glob("**/*")) if (STUDY / "receipts").exists() else False:
        raise Stage0Failure("preseed prepare refuses to overwrite existing receipt history")
    if git("rev-parse", "HEAD") != str(config["identity"]["base_study_commit"]):
        raise Stage0Failure("source work must begin at the exact Issue 72 remote base commit")
    if git("rev-parse", "origin/codex/issue-72-attribution") != str(config["identity"]["base_study_commit"]):
        raise Stage0Failure("Issue 72 remote lineage moved or is unavailable")
    forbidden = [line for line in (git("diff", "--name-only") + "\n" + git("diff", "--cached", "--name-only")).splitlines() if line]
    outside = [line for line in forbidden if line != STUDY_RELATIVE and not line.startswith(STUDY_RELATIVE + "/")]
    if outside:
        raise Stage0Failure(f"preseed branch contains tracked or staged modifications outside the new study folder: {outside}")
    config["identity"]["study_source_sha256"] = source_hashes()
    pin_external_sources(config)
    encoded = (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8")
    runner.atomic_bytes(config_path, encoded)
    digest = runner.sha256_bytes(encoded)
    runner.atomic_bytes(STUDY / "config.lock.sha256", (digest + "\n").encode("ascii"))
    record = {
        "schema_id": "issue73-preseed-source-lock-v1", "status": "PRESEED_LOCKED_BEFORE_ANY_DEBUG_SEED",
        "study_branch": BRANCH, "base_remote_branch": "codex/issue-72-attribution",
        "base_commit": config["identity"]["base_study_commit"], "config_sha256": digest,
        "study_source_sha256": config["identity"]["study_source_sha256"],
        "native_commit": config["identity"]["native_commit"], "native_source_files_sha256": config["identity"]["native_source_files_sha256"],
        "native_module_sha256": config["identity"]["native_binary_sha256"],
        "stsr_commit": config["identity"]["stsr_commit"], "stsr_source_files_sha256": config["identity"]["stsr_source_files_sha256"],
        "runtime": {k: config["identity"][k] for k in ("python", "torch", "numpy")},
        "authorized_phase": "debug-only-gates-A-B-C", "stage1_authorized": False,
        "formal_training_seed_consumed": False, "formal_evaluation_seed_consumed": False,
        "resource_ceiling_cpu_seconds": config["resources"]["all_in_ceiling_seconds"],
        "builder_cpu_ceiling_seconds": config["resources"]["builder_cpu_hard_cap_seconds"],
        "reviewer_cpu_reserve_seconds": config["resources"]["reviewer_cpu_reserve_seconds"],
        "native_gate_bc_cpu_ceiling_seconds": config["resources"]["native_gate_bc_cpu_ceiling_seconds"],
        "debug_seeds": config["stage0"]["debug_owner_seeds"], "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    runner.atomic_json(OUT / "preseed-source-lock.json", record)
    commit = commit_push("Issue 73: freeze Stage 0 source and config before debug seeds")
    record["preseed_commit"] = commit
    runner.atomic_json(OUT / "preseed-source-lock.json", record)
    commit = commit_push("Issue 73: bind preseed lock to exact source commit")
    return f"PRESEED_LOCKED config_sha256={digest} commit={commit}"


def setup_environment() -> tuple[dict[str, Any], Any, Any, dict[str, Any], Any, str]:
    verify_sources()
    env, module, encoder, runtime = runner._setup(OUT)
    identity = env["identity"]
    if identity.get("native_module_sha256") != CONFIG["identity"]["native_binary_sha256"]:
        raise Stage0Failure("loaded native binary differs from the committed source lock")
    init_path = OUT / "policies" / "pi_init.pt"
    model, digest = runner._load_policy(init_path, initial=True)
    saved_digest = runner._save_policy(model, init_path)
    if saved_digest != digest or runner._model_digest(model) != digest:
        raise Stage0Failure("deterministic initial policy did not roundtrip under the committed runtime")
    return env, module, encoder, runtime, model, digest


def budget_summary() -> dict[str, Any]:
    value = ledger()
    native = sum(float(row.get("cpu_charged_seconds", row.get("reserved_cpu_seconds", 0.0))) for row in value["attempts"] if row.get("native"))
    return {"charged_builder_process_cpu_seconds": charged_cpu(value), "builder_ceiling_seconds": BUILDER_CEILING, "reviewer_reserved_seconds": float(CONFIG["resources"]["reviewer_cpu_reserve_seconds"]), "all_in_ceiling_seconds": float(CONFIG["resources"]["all_in_ceiling_seconds"]), "charged_native_root_gate_bc_cpu_seconds": native, "native_gate_bc_ceiling_seconds": GATE_CEILING, "remaining_builder_cpu_seconds": max(0.0, BUILDER_CEILING - charged_cpu(value)), "remaining_native_gate_bc_cpu_seconds": max(0.0, GATE_CEILING - native)}

def root_has_hidden_semantic(root: Mapping[str, Any]) -> bool:
    if str(root.get("screen", "")).upper() != "MAP_SCREEN": return False
    for candidate in root.get("projection", {}).get("candidates", []):
        feature = candidate.get("features", {}) if isinstance(candidate, Mapping) else {}
        item = feature.get("oracle_destination_room") if isinstance(feature, Mapping) else None
        if isinstance(item, Mapping) and item.get("source") == "MapNode::room" and item.get("visibility") == "oracle_only": return True
    return False


def root_has_rng_consequence(root: Mapping[str, Any]) -> bool:
    if str(root.get("screen", "")).upper() != "TREASURE_ROOM": return False
    required_reason = "specific chest loot is not materialized until openTreasureRoomChest executes"
    tags = root.get("source_tag_mapping", [])
    unavailable = any(row.get("field") == "unrevealed_loot" and row.get("availability") == "unavailable" and required_reason in str(row.get("reason", "")) for row in tags)
    open_indices = []
    for i, candidate in enumerate(root.get("projection", {}).get("candidates", [])):
        feature = candidate.get("features", {}) if isinstance(candidate, Mapping) else {}
        item = feature.get("choice_type") if isinstance(feature, Mapping) else None
        value = item.get("value") if isinstance(item, Mapping) else item
        if value == "open_chest": open_indices.append(i)
    return unavailable and 0 in open_indices


def native_chest_rng_source_evidence() -> dict[str, Any]:
    native_root = Path(os.environ["STS_LIGHTSPEED_SOURCE_DIR"])
    context_path = native_root / "src/game/GameContext.cpp"
    action_path = native_root / "src/sim/search/GameAction.cpp"
    context_lines = context_path.read_text(encoding="utf-8").splitlines()
    action_lines = action_path.read_text(encoding="utf-8").splitlines()
    signature = "void GameContext::openTreasureRoomChest()"
    try: start = next(i for i, line in enumerate(context_lines) if signature in line)
    except StopIteration as exc: raise Stage0Failure("pinned native chest RNG function was not found") from exc
    depth = 0
    seen = False
    body: list[str] = []
    for line in context_lines[start:]:
        body.append(line)
        depth += line.count("{") - line.count("}")
        seen = seen or "{" in line
        if seen and depth == 0: break
    function_body = "\n".join(body)
    try: transition_line = next((i, line) for i, line in enumerate(action_lines) if "gc.openTreasureRoomChest();" in line)
    except StopIteration as exc: raise Stage0Failure("pinned native TREASURE_ROOM action does not call openTreasureRoomChest") from exc
    required = {
        "treasure_rng_draw": "treasureRng.random(" in function_body,
        "random_relic_selection": "returnRandomRelic(" in function_body,
        "public_reward_transition": "openCombatRewardScreen(reward);" in function_body,
        "legal_action_to_native_chest_call": "gc.openTreasureRoomChest();" in transition_line[1],
    }
    if not all(required.values()): raise Stage0Failure(f"pinned native chest RNG source contract incomplete: {required}")
    return {
        "status": "PASS",
        "native_commit": CONFIG["identity"]["native_commit"],
        "source_sha256": {name: CONFIG["identity"]["native_source_files_sha256"][name] for name in ("src/game/GameContext.cpp", "src/sim/search/GameAction.cpp")},
        "source_evidence": {
            "GameContext.cpp": {"function_start_line": start + 1, "matched_lines": [{"line": i + 1, "text": line.strip()} for i, line in enumerate(context_lines[start:], start) if any(token in line for token in ("treasureRng.random(", "returnRandomRelic(", "openCombatRewardScreen(reward);"))]},
            "GameAction.cpp": {"matched_line": {"line": transition_line[0] + 1, "text": transition_line[1].strip()}},
        },
        "checks": required,
    }


def public_reward_choices(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        if value.get("choice_type") in {"relic", "gold", "key"}:
            found.append(dict(value))
        for child in value.values(): found.extend(public_reward_choices(child))
    elif isinstance(value, list):
        for child in value: found.extend(public_reward_choices(child))
    return found


def root_after_two_third_family(root: Mapping[str, Any]) -> bool:
    screen = str(root.get("screen", "")).upper()
    return int(root.get("policy_decision_index", -1)) >= 2 and screen not in {"MAP_SCREEN", "TREASURE_ROOM"}


def root_collection_ready(roots: Sequence[Mapping[str, Any]], live_ids: set[str]) -> tuple[bool, dict[str, Any]]:
    families = sorted({str(r.get("screen", "")).upper() for r in roots})
    hidden = next((r for r in roots if root_has_hidden_semantic(r)), None)
    rng = next((r for r in roots if root_has_rng_consequence(r)), None)
    post_two = next((r for r in roots if root_after_two_third_family(r)), None)
    live = [r for r in roots if str(r.get("root_id")) in live_ids]
    live_hidden = next((r for r in live if root_has_hidden_semantic(r)), None)
    live_rng = next((r for r in live if root_has_rng_consequence(r)), None)
    live_post = next((r for r in live if root_after_two_third_family(r)), None)
    ready = len(roots) >= int(CONFIG["stage0"]["required_multichoice_roots"]) and len(families) >= int(CONFIG["stage0"]["required_screen_family_count"]) and bool(hidden and rng and post_two and live_hidden and live_rng and live_post)
    return ready, {"persisted_root_count": len(roots), "screen_families": families, "hidden_root_id": None if hidden is None else hidden["root_id"], "rng_consequence_root_id": None if rng is None else rng["root_id"], "post_two_root_id": None if post_two is None else post_two["root_id"], "live_adapter_witnesses_available": {"hidden": None if live_hidden is None else live_hidden["root_id"], "rng_consequence": None if live_rng is None else live_rng["root_id"], "post_two": None if live_post is None else live_post["root_id"]}}


def load_roots(live_by_id: Mapping[str, Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    live_by_id = live_by_id or {}
    rows: list[dict[str, Any]] = []
    for path in sorted((OUT / "roots").glob("root-*.json")):
        v = json.loads(path.read_text(encoding="utf-8"))
        if v.get("config_sha256") != CONFIG_SHA: raise Stage0Failure(f"{path.name} config lock mismatch")
        cp_record = v.get("native_checkpoint", {})
        cp = None
        if cp_record.get("status") == "SERIALIZED_AND_ROUNDTRIPPED":
            cp_path = STUDY / str(cp_record["path"])
            if cp_path.exists() and runner.sha256_file(cp_path) == cp_record.get("sha256"):
                try: cp = pickle.loads(cp_path.read_bytes())
                except Exception: cp = None
        row = {"root_id": v["root_id"], "root_index": int(v.get("owner_root_index", v["owner_decision_index"])), "run_id": v["owner_run_id"], "seed": v["owner_seed"], "decision_index": v["owner_decision_index"], "policy_decision_index": v["policy_decision_index"], "screen": v["screen"], **v["root_fingerprints"], "projection_digest": v["projection_digest"], "projection": v["raw_privileged_projection"], "public_projection": v["sanitized_public_projection"], "source_tag_mapping": v["source_tag_mapping"], "legal_action_identities": v["legal_action_occurrences"], "candidate_index": v["selected_owner_occurrence"], "feature_matrix": v["semantic_feature_matrix"], "feature_matrix_sha256": v["feature_matrix_sha256"], "live_binding_validation_passed": v["candidate_binding_validation_passed"], "checkpoint_record": cp_record, "checkpoint": cp}
        if row["root_id"] in live_by_id:
            row["adapter"] = live_by_id[row["root_id"]]["adapter"]
            row["checkpoint"] = live_by_id[row["root_id"]]["checkpoint"]
        rows.append(row)
    return sorted(rows, key=lambda r: (int(r["seed"]), int(r["decision_index"])))


def install_process_cpu_ceiling(native: bool = True) -> None:
    value = ledger()
    remaining = BUILDER_CEILING - charged_cpu(value)
    if native:
        native_used = sum(float(row.get("cpu_charged_seconds", row.get("reserved_cpu_seconds", 0.0))) for row in value["attempts"] if row.get("native"))
        remaining = min(remaining, GATE_CEILING - native_used)
    hard = int(math.floor(remaining))
    if hard < 1: raise Stage0Failure("no process CPU remains in the registered Stage 0 budget")
    current_soft, current_hard = resource.getrlimit(resource.RLIMIT_CPU)
    new_soft, new_hard = min(hard - 1, int(current_soft) if current_soft != resource.RLIM_INFINITY else hard - 1), min(hard, int(current_hard) if current_hard != resource.RLIM_INFINITY else hard)
    resource.setrlimit(resource.RLIMIT_CPU, (new_soft, new_hard))
    signal.signal(signal.SIGXCPU, lambda *_: (_ for _ in ()).throw(Stage0Failure("registered Stage 0 process CPU hard ceiling reached")))


def collect_roots(module: Any, encoder: Any, model: Any, model_digest: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    live_by_id: dict[str, Mapping[str, Any]] = {}
    seeds = [int(x) for x in CONFIG["stage0"]["debug_owner_seeds"]]
    if seeds != list(range(int(CONFIG["stage0"]["debug_seed_first"]), int(CONFIG["stage0"]["debug_seed_last"]) + 1)):
        raise Stage0Failure("fixed debug seed list differs from its committed contiguous bounds")
    for seed in seeds:
        roots = load_roots(live_by_id)
        ready, status = root_collection_ready(roots, set(live_by_id))
        if ready: break
        key = f"debug-owner-{seed}"
        existing = read_json(OUT / "attempts" / f"{key}.json")
        if existing and existing.get("status") == "COMPLETE": continue
        if existing and existing.get("status") not in {"COMPLETE", "STARTED"}: continue
        if existing and existing.get("status") == "STARTED":
            start_attempt(key, kind="natural_debug_owner", cpu_reserve=float(CONFIG["resources"]["root_attempt_cpu_cap_seconds"]), native=True)
            continue
        _, should_run = start_attempt(key, kind="natural_debug_owner", cpu_reserve=float(CONFIG["resources"]["root_attempt_cpu_cap_seconds"]), native=True)
        if not should_run: continue
        started = tree_cpu()
        captured: list[str] = []
        def on_root(root: Mapping[str, Any]) -> None:
            receipt = persist_root(root)
            rid = str(receipt["root_id"])
            live_by_id[rid] = root
            captured.append(rid)
        run_id = f"DEBUG_{seed}"
        try:
            result = runner._run_one(out=OUT, module=module, model=model, model_digest=model_digest, seed=seed, arm="P0", encoder=encoder, capture_checkpoints=True, capture_all_roots=True, purpose="issue73-stage0-debug-owner", resource_label=run_id, adapter_override=runner.native_adapter(module, seed), capture_trace=False, root_callback=on_root, cpu_cap_seconds=float(CONFIG["resources"]["root_attempt_cpu_cap_seconds"]))
            if result.get("terminal_complete") is not True: raise Stage0Failure("debug natural owner did not reach a genuine terminal")
            runner.atomic_json(OUT / "debug-owner-runs" / f"{seed}.json", {k: v for k, v in result.items() if k != "_volatile_roots"})
            terminal = {"terminal": result.get("terminal"), "terminal_complete": True, "screen_counts": result.get("screen_counts"), "completed_transitions": result.get("completed_transitions"), "run_id": run_id, "source_commit": result.get("native_source_commit"), "binary_sha256": result.get("native_module_sha256"), "root_ids": captured}
            row = finish_attempt(key, measured_cpu=tree_cpu() - started, status="COMPLETE", details=terminal)
            if row.get("status") != "COMPLETE": break
        except BaseException as exc:
            details = {"failure_type": type(exc).__name__, "failure": str(exc)[:1000], "captured_root_ids": captured, "native_run_receipt": str((OUT / "runs" / f"{run_id}.json").relative_to(STUDY)) if (OUT / "runs" / f"{run_id}.json").exists() else None}
            finish_attempt(key, measured_cpu=tree_cpu() - started, status="FAIL_OR_INCOMPLETE", details=details)
        roots = load_roots(live_by_id)
        ready, status = root_collection_ready(roots, set(live_by_id))
        runner.atomic_json(OUT / "root-collection-status.json", {"schema_id": "issue73-root-collection-status-v1", "status": "READY" if ready else "COLLECTING", "debug_seeds_considered": [int(x) for x in seeds if read_json(OUT / "attempts" / f"debug-owner-{x}.json") is not None], **status})
        commit_push("Issue 73: update fixed debug-root collection status")
    roots = load_roots(live_by_id)
    ready, status = root_collection_ready(roots, set(live_by_id))
    terminal = "READY" if ready else "BLOCKED_FIXED_DEBUG_ROOT_SET_EXHAUSTED_OR_INTERRUPTED"
    runner.atomic_json(OUT / "root-collection-status.json", {"schema_id": "issue73-root-collection-status-v1", "status": terminal, "debug_seeds": seeds, **status})
    commit_push("Issue 73: finalize fixed debug-root collection")
    return roots, {"status": terminal, **status}

def run_gate_a(roots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    path = OUT / "attempts" / "gate-a.json"
    previous = read_json(path)
    if previous:
        if previous.get("status") == "COMPLETE": return read_json(OUT / "gate-a-report.json", {"status": previous.get("gate_status", "FAIL_REPORT_MISSING")})
        if previous.get("status") not in {"COMPLETE", "STARTED"}: return read_json(OUT / "gate-a-report.json", {"status": previous.get("gate_status", "BLOCKED_PRIOR_FAILURE_NO_RERUN")})
        if previous.get("status") == "STARTED":
            start_attempt("gate-a", kind="synthetic_policy_response", cpu_reserve=120.0, native=False)
            return {"status": "BLOCKED_INTERRUPTED_NO_RERUN"}
    _, should_run = start_attempt("gate-a", kind="synthetic_policy_response", cpu_reserve=120.0, native=False)
    if not should_run: return {"status": "BLOCKED_PRIOR_ATTEMPT_UNAVAILABLE"}
    started = tree_cpu()
    try:
        selected = gate_a.select_corpus(roots)
        if len(selected) < int(CONFIG["stage0"]["required_multichoice_roots"]) or len({str(r["screen"]) for r in selected}) < int(CONFIG["stage0"]["required_screen_family_count"]):
            report = {"schema_id": "issue73-gate-a-policy-response-v1", "status": "FAIL_INSUFFICIENT_FIXED_ROOT_CORPUS", "selected_root_count": len(selected), "required_root_count": int(CONFIG["stage0"]["required_multichoice_roots"]), "observed_screen_families": sorted({str(r.get("screen")) for r in selected}), "required_screen_family_count": int(CONFIG["stage0"]["required_screen_family_count"]), "target_reselected": False, "formal_training_or_evaluation_seeds_consumed": False}
            runner.atomic_json(OUT / "gate-a-report.json", report)
        else:
            report = gate_a.run_gate_a(roots, OUT)
        finished = finish_attempt("gate-a", measured_cpu=tree_cpu() - started, status="COMPLETE", details={"gate_status": report.get("status"), "root_count": report.get("root_count", report.get("selected_root_count", 0)), "gradient_updates": report.get("synthetic_target", {}).get("total_gradient_updates_across_fixed_and_legacy_actors", 0)})
        report["attempt_status"] = finished["status"]
        report["process_cpu_seconds"] = finished.get("measured_process_cpu_seconds")
        if finished["status"] != "COMPLETE":
            report["status"] = "FAIL_CPU_RESERVATION_EXCEEDED"
            report["cpu_reservation_seconds"] = finished.get("reserved_cpu_seconds")
        runner.atomic_json(OUT / "gate-a-report.json", report)
        commit_push("Issue 73: preserve Gate A policy-response result")
        return report
    except BaseException as exc:
        finish_attempt("gate-a", measured_cpu=tree_cpu() - started, status="FAIL_OR_INCOMPLETE", details={"gate_status": "FAIL_EXCEPTION", "failure_type": type(exc).__name__, "failure": str(exc)[:1000]})
        report = {"schema_id": "issue73-gate-a-policy-response-v1", "status": "FAIL_EXCEPTION", "failure_type": type(exc).__name__, "failure": str(exc)[:1000], "target_reselected": False, "formal_training_or_evaluation_seeds_consumed": False}
        runner.atomic_json(OUT / "gate-a-report.json", report)
        commit_push("Issue 73: preserve Gate A failure evidence")
        return report


def candidate_feature(root: Mapping[str, Any], index: int, key: str) -> Any:
    rows = root.get("projection", {}).get("candidates", [])
    if not 0 <= index < len(rows): return None
    candidate = rows[index]
    features = candidate.get("features", {}) if isinstance(candidate, Mapping) else {}
    return features.get(key) if isinstance(features, Mapping) else None


def hidden_public_negative(module: Any, root: Mapping[str, Any]) -> dict[str, Any]:
    base = root.get("adapter")
    checkpoint = root.get("checkpoint")
    if base is None or checkpoint is None: return {"status": "BLOCKED_NO_LIVE_ADAPTER_CHECKPOINT"}
    wrapper = runner.CheckpointAdapter(base, checkpoint)
    snapshot = wrapper.reset(int(root["seed"]))
    public_payload = root.get("public_projection", {})
    public_json = runner.canonical(public_payload).lower()
    hidden_fields: list[dict[str, Any]] = []
    for i, candidate in enumerate(root.get("projection", {}).get("candidates", [])):
        features = candidate.get("features", {}) if isinstance(candidate, Mapping) else {}
        if not isinstance(features, Mapping): continue
        for name, item in features.items():
            if not isinstance(item, Mapping) or (item.get("visibility") != "oracle_only" and item.get("availability") != "unavailable"): continue
            value = item.get("value")
            value_present = value is not None and runner.canonical(value).lower() in public_json
            name_present = str(name).lower() in public_json
            hidden_fields.append({"candidate_index": i, "field": str(name), "visibility": item.get("visibility"), "availability": item.get("availability"), "source": item.get("source"), "reason": item.get("reason"), "value_sha256": runner.sha256_bytes(runner.canonical(value).encode("utf-8")) if value is not None else None, "field_name_present_in_sanitized_public_projection": name_present, "value_present_in_sanitized_public_projection": value_present})
    rejected = False
    failure = None
    try:
        access = module.PrivilegedNoncombatProjectionAccess(runner.native_simulator(wrapper), "public_only")
        access.project_v1()
    except Exception as exc:
        rejected = True
        failure = {"type": type(exc).__name__, "message": str(exc)[:400]}
    return {"status": "PASS" if rejected and all(not x["field_name_present_in_sanitized_public_projection"] and not x["value_present_in_sanitized_public_projection"] for x in hidden_fields) else "FAIL_PUBLIC_NEGATIVE_OR_SANITIZATION", "public_only_audience_rejected": rejected, "public_only_error": failure, "sanitized_public_projection_sha256": runner.sha256_bytes(runner.canonical(public_payload).encode("utf-8")), "oracle_or_unavailable_features": hidden_fields}


def transition_witness(root: Mapping[str, Any], target_index: int) -> dict[str, Any]:
    from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions
    base, checkpoint = root.get("adapter"), root.get("checkpoint")
    if base is None or checkpoint is None: raise Stage0Failure("durable checkpoint cannot be restored in a live bound adapter")
    def once() -> dict[str, Any]:
        wrapper = runner.CheckpointAdapter(base, checkpoint)
        snapshot = wrapper.reset(int(root["seed"]))
        fingerprints = runner._snapshot_debug_fingerprints(wrapper, snapshot)
        for field in ("native_state_sha256", "public_projection_sha256", "public_resource_state_sha256"):
            if fingerprints.get(field) != root.get(field): raise Stage0Failure(f"restored {field} changed before the forced native legal action")
        actions = base.legal_actions(snapshot)
        identities = action_identity_dicts_for_actions(actions)
        if runner.canonical(identities) != runner.canonical(root["legal_action_identities"]): raise Stage0Failure("restored source-ordered legal action occurrences changed")
        if not 0 <= int(target_index) < len(actions): raise Stage0Failure("witness target is outside the current native legal-action list")
        transition = wrapper.step(actions[int(target_index)])
        post_public = base.public_projection(transition.snapshot)
        post_payload = json.loads(post_public.canonical_payload) if hasattr(post_public, "canonical_payload") else dict(post_public)
        post_fp = runner._snapshot_debug_fingerprints(base, transition.snapshot)
        return {"pre_action_native_state_sha256": fingerprints["native_state_sha256"], "pre_action_public_projection_sha256": fingerprints["public_projection_sha256"], "legal_action_count": len(actions), "legal_actions_sha256": runner.sha256_bytes(runner.canonical(identities).encode("utf-8")), "forced_action_index": int(target_index), "forced_action_identity": identities[int(target_index)], "transition_terminal": bool(transition.terminal), "transition_info": dict(transition.info), "revealed_public_projection": post_payload, "post_action_native_state_sha256": post_fp["native_state_sha256"], "post_action_public_projection_sha256": post_fp["public_projection_sha256"], "post_action_resource_state_sha256": post_fp["public_resource_state_sha256"]}
    first, second = once(), once()
    if first != second: raise Stage0Failure("restore/action-step produced a nondeterministic immediate native transition witness")
    return {"status": "PASS", "repeated_restore_action_step_equal": True, "transition": first}


def run_gate_b(roots: Sequence[Mapping[str, Any]], module: Any, encoder: Any, model: Any, model_digest: str) -> dict[str, Any]:
    cfg = CONFIG["stage0"]["gate_b"]
    hidden = next((r for r in roots if root_has_hidden_semantic(r) and r.get("live_binding_validation_passed") is True and r.get("adapter") is not None and r.get("checkpoint") is not None), None)
    rng = next((r for r in roots if root_has_rng_consequence(r) and r.get("live_binding_validation_passed") is True and r.get("adapter") is not None and r.get("checkpoint") is not None), None)
    post = next((r for r in roots if root_after_two_third_family(r) and r.get("live_binding_validation_passed") is True and r.get("adapter") is not None and r.get("checkpoint") is not None), None)
    chosen = {"hidden_semantic": hidden, "native_rng_consequence": rng, "post_two_third_family": post}
    if not all(chosen.values()):
        report = {"schema_id": "issue73-gate-b-privileged-transition-v1", "status": "BLOCKED_REQUIRED_LIVE_ROOT_WITNESSES_UNAVAILABLE", "witness_root_ids": {k: None if v is None else v["root_id"] for k, v in chosen.items()}, "source_claim_scope": "hidden native state is not a future realization; chest open shows one native RNG-dependent public consequence only; no conditional value or distribution claimed", "formal_training_or_evaluation_seeds_consumed": False}
        runner.atomic_json(OUT / "gate-b-report.json", report); commit_push("Issue 73: preserve Gate B prerequisite blocker"); return report
    keys = ("hidden_semantic", "native_rng_consequence", "post_two_third_family")
    witnesses: dict[str, Any] = {}
    overall = True
    for name in keys:
        root = chosen[name]
        checkpoint_record = root.get("checkpoint_record", {})
        if checkpoint_record.get("status") != "SERIALIZED_AND_ROUNDTRIPPED" or root.get("checkpoint") is None:
            witnesses[name] = {"status": "BLOCKED_CHECKPOINT_NOT_DURABLY_SERIALIZED", "root_id": root["root_id"], "checkpoint": checkpoint_record}
            overall = False
            continue
        key = "gate-b-" + name.replace("_", "-")
        prior = read_json(OUT / "attempts" / f"{key}.json")
        if prior and prior.get("status") == "COMPLETE":
            witnesses[name] = read_json(OUT / "gate-b-witnesses" / f"{key}.json", {"status": "FAIL_WITNESS_FILE_MISSING", "attempt_witness_status": prior.get("witness_status")})
            overall = overall and witnesses[name].get("status") == "PASS"
            continue
        if prior and prior.get("status") not in {"COMPLETE", "STARTED"}:
            witnesses[name] = read_json(OUT / "gate-b-witnesses" / f"{key}.json", {"status": prior.get("witness_status", "BLOCKED_PRIOR_FAILURE_NO_RERUN")})
            overall = False
            continue
        if prior and prior.get("status") == "STARTED":
            start_attempt(key, kind="gate_b_checkpoint_transition", cpu_reserve=30.0, native=True)
            witnesses[name] = {"status": "BLOCKED_INTERRUPTED_NO_RERUN", "root_id": root["root_id"]}; overall = False; continue
        _, should_run = start_attempt(key, kind="gate_b_checkpoint_transition", cpu_reserve=30.0, native=True)
        if not should_run: witnesses[name] = {"status": "BLOCKED_PRIOR_ATTEMPT_UNAVAILABLE"}; overall = False; continue
        started = tree_cpu()
        try:
            old_handler = signal.signal(signal.SIGPROF, lambda *_: (_ for _ in ()).throw(Stage0Failure("Gate B witness exceeded its 30 CPU-second reservation")))
            signal.setitimer(signal.ITIMER_PROF, 30.0)
            try:
                replay = runner._checkpoint_replay_probe(module=module, base=root["adapter"], root=root, model=model, model_digest=model_digest, encoder=encoder, seed=int(root["seed"]))
                if replay.get("repeated_restore_and_step_equal") is not True: raise Stage0Failure("repeat restore/action-step did not agree")
                if name == "hidden_semantic":
                    evidence = [{"field": row.get("field"), "source": row.get("source"), "visibility": row.get("visibility"), "availability": row.get("availability"), "reason": row.get("reason")} for row in root["source_tag_mapping"] if row.get("field") == "oracle_destination_room"]
                    if not evidence or not any(row.get("source") == "MapNode::room" and row.get("visibility") == "oracle_only" for row in evidence): raise Stage0Failure("MAP_SCREEN did not expose the predeclared hidden MapNode::room semantic")
                    target_indices = [i for i in range(len(root["legal_action_identities"])) if candidate_feature(root, i, "oracle_destination_room") is not None]
                    if not target_indices: raise Stage0Failure("no live legal candidate occurrence carries the hidden MapNode::room feature")
                    step = transition_witness(root, target_indices[0])
                    negative = hidden_public_negative(module, root)
                    if negative.get("status") != "PASS": raise Stage0Failure("public-only audience or sanitized projection negative test failed")
                    witness = {"status": "PASS", "root_id": root["root_id"], "source_tags": evidence, "hidden_semantic_candidate_index": target_indices[0], "action_step_restore": step, "public_negative_test": negative, "privileged_value_interpretation": "currently hidden semantic state only; not a future stochastic realization and not a conditional return"}
                elif name == "native_rng_consequence":
                    evidence = [row for row in root["source_tag_mapping"] if row.get("field") == "unrevealed_loot"]
                    if not root_has_rng_consequence(root): raise Stage0Failure("TREASURE_ROOM loot was not explicitly tagged unavailable for the exact pinned source reason")
                    open_indices = []
                    for i in range(len(root["legal_action_identities"])):
                        item = candidate_feature(root, i, "choice_type")
                        value = item.get("value") if isinstance(item, Mapping) else item
                        if value == "open_chest": open_indices.append(i)
                    if not open_indices: raise Stage0Failure("pinned native TREASURE_ROOM has no legal choice_type=open_chest occurrence")
                    source_evidence = native_chest_rng_source_evidence()
                    if open_indices[0] != 0: raise Stage0Failure("the source-pinned TREASURE_ROOM open_chest action must map to GameAction index 0")
                    step = transition_witness(root, open_indices[0])
                    revealed = step["transition"]["revealed_public_projection"]
                    reward_choices = public_reward_choices(revealed)
                    if not reward_choices or step["transition"]["post_action_public_projection_sha256"] == root["public_projection_sha256"]:
                        raise Stage0Failure("legal open-chest transition did not reveal a changed public relic/gold/key reward choice")
                    witness = {"status": "PASS", "root_id": root["root_id"], "unavailable_loot_source_tags": evidence, "native_rng_source": source_evidence, "forced_action_index": open_indices[0], "revealed_public_reward_choices": reward_choices, "action_step_restore_and_revealed_consequence": step, "claim_limit": "one observed legal open-chest realization only; native RNG is restored with the checkpoint; no independent game-future sample, conditional distribution, or Q claim"}
                else:
                    step = transition_witness(root, int(root["candidate_index"]))
                    witness = {"status": "PASS", "root_id": root["root_id"], "screen_family": root["screen"], "policy_decision_index": int(root["policy_decision_index"]), "action_step_restore": step, "post_two_root": int(root["policy_decision_index"]) >= int(cfg["post_two"]["policy_decision_index_min"])}
                witnesses[name] = witness
            finally:
                signal.setitimer(signal.ITIMER_PROF, 0.0)
                signal.signal(signal.SIGPROF, old_handler)
            finished = finish_attempt(key, measured_cpu=tree_cpu() - started, status="COMPLETE", details={"witness_status": witness["status"], "root_id": root["root_id"]})
            if finished["status"] != "COMPLETE":
                witness = {**witness, "observed_witness_status": witness["status"], "status": "FAIL_CPU_RESERVATION_EXCEEDED", "reserved_cpu_seconds": finished["reserved_cpu_seconds"], "measured_process_cpu_seconds": finished["measured_process_cpu_seconds"]}
            runner.atomic_json(OUT / "gate-b-witnesses" / f"{key}.json", witness)
            commit_push(f"Issue 73: preserve Gate B witness {name}")
            overall = overall and witness["status"] == "PASS"
        except BaseException as exc:
            failure = {"status": "FAIL_OR_INCOMPLETE", "root_id": root["root_id"], "failure_type": type(exc).__name__, "failure": str(exc)[:1000]}
            runner.atomic_json(OUT / "gate-b-witnesses" / f"{key}.json", failure)
            finish_attempt(key, measured_cpu=tree_cpu() - started, status="FAIL_OR_INCOMPLETE", details={"witness_status": failure["status"], "failure_type": failure["failure_type"], "failure": failure["failure"], "root_id": root["root_id"]})
            witnesses[name] = failure; overall = False
    families = {str(r["screen"]) for r in chosen.values() if r}
    report = {"schema_id": "issue73-gate-b-privileged-transition-v1", "status": "PASS" if overall and len(families) >= int(cfg["minimum_distinct_screen_families"]) else "FAIL_OR_BLOCKED", "witness_root_ids": {k: None if v is None else v["root_id"] for k, v in chosen.items()}, "distinct_witness_screen_families": sorted(families), "minimum_distinct_screen_families": int(cfg["minimum_distinct_screen_families"]), "witnesses": witnesses, "source_interpretation": {"static_hidden_state": "MapNode::room semantic tagged oracle_only and absent from sanitized public projection", "rng_dependent_consequence": "specific chest loot is unavailable before openTreasureRoomChest; one actual public open-chest result is observed", "conditional_value_or_distribution": "not identified by these witnesses"}, "native_source_commit": CONFIG["identity"]["native_commit"], "native_binary_sha256": CONFIG["identity"]["native_binary_sha256"], "normal_public_api_receives_oracle_only_values": False, "formal_training_or_evaluation_seeds_consumed": False, "resource_accounting": budget_summary()}
    runner.atomic_json(OUT / "gate-b-report.json", report); commit_push("Issue 73: finalize Gate B evidence"); return report

def derived_gate_c_seed(tag: str, root: Mapping[str, Any], action_index: int, replicate: int) -> int:
    payload = f"issue73|gateC|{tag}|{int(root['seed'])}|{root['run_id']}|{int(root['root_index'])}|{int(action_index)}|{int(replicate)}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & ((1 << 63) - 1)


def gate_c_plan(roots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    path = OUT / "plans" / "gate-c-plan.json"
    prior = read_json(path)
    if prior:
        if prior.get("config_sha256") != CONFIG_SHA: raise Stage0Failure("committed Gate C plan config lock changed")
        return prior
    ordered = [r for r in roots if r.get("live_binding_validation_passed") is True and r.get("checkpoint_record", {}).get("status") == "SERIALIZED_AND_ROUNDTRIPPED" and r.get("checkpoint") is not None and len(r.get("legal_action_identities", [])) >= 2]
    ordered.sort(key=lambda r: (int(r["seed"]), int(r["decision_index"])))
    chosen: list[Mapping[str, Any]] = []
    families: set[str] = set()
    for root in ordered:
        family = str(root["screen"])
        if family not in families:
            chosen.append(root); families.add(family)
            if len(families) >= int(CONFIG["stage0"]["gate_c"]["screen_family_min"]): break
    post = next((r for r in ordered if int(r["policy_decision_index"]) >= int(CONFIG["stage0"]["gate_c"]["require_policy_decision_index_min"])), None)
    if post is not None and all(post["root_id"] != r["root_id"] for r in chosen): chosen.append(post)
    for root in ordered:
        if len(chosen) >= int(CONFIG["stage0"]["gate_c"]["root_count"]): break
        if all(root["root_id"] != r["root_id"] for r in chosen): chosen.append(root)
    chosen = chosen[:int(CONFIG["stage0"]["gate_c"]["root_count"])]
    plan_roots=[]
    streams=[]
    for root in chosen:
        action_indices = [0, len(root["legal_action_identities"]) - 1]
        if len(root["legal_action_identities"]) < 2 or action_indices[0] == action_indices[1]:
            continue
        for action_index in action_indices:
            for replicate in range(int(CONFIG["stage0"]["gate_c"]["repetitions_per_action"])):
                streams.append({"root_id": root["root_id"], "root_index": int(root["root_index"]), "owner_seed": int(root["seed"]), "run_id": root["run_id"], "candidate_index": int(action_index), "replicate": replicate, "suffix_policy_seed": derived_gate_c_seed("suffix", root, action_index, replicate), "expert_seed": derived_gate_c_seed("expert", root, action_index, replicate)})
        plan_roots.append({"root_id": root["root_id"], "owner_seed": int(root["seed"]), "run_id": root["run_id"], "root_index": int(root["root_index"]), "decision_index": int(root["decision_index"]), "policy_decision_index": int(root["policy_decision_index"]), "screen": root["screen"], "serialized_checkpoint": root["checkpoint_record"], "legal_action_count": len(root["legal_action_identities"]), "action_indices": action_indices, "action_identities": [root["legal_action_identities"][i] for i in action_indices]})
    all_seeds = [x["suffix_policy_seed"] for x in streams] + [x["expert_seed"] for x in streams]
    unique = len(set(all_seeds)) == len(all_seeds)
    ready = len(plan_roots) == int(CONFIG["stage0"]["gate_c"]["root_count"]) and len({x["screen"] for x in plan_roots}) >= int(CONFIG["stage0"]["gate_c"]["screen_family_min"]) and any(x["policy_decision_index"] >= int(CONFIG["stage0"]["gate_c"]["require_policy_decision_index_min"]) for x in plan_roots) and len(streams) == int(CONFIG["stage0"]["gate_c"]["nominal_total"]) and unique
    plan = {"schema_id": "issue73-gate-c-fixed-suffix-plan-v1", "status": "READY" if ready else "BLOCKED_PREREQUISITE_ROOTS_OR_CHECKPOINTS", "config_sha256": CONFIG_SHA, "selection_rule": CONFIG["stage0"]["gate_c"]["root_selection"], "root_count_required": int(CONFIG["stage0"]["gate_c"]["root_count"]), "root_count_selected": len(plan_roots), "roots": plan_roots, "policy_rng_seed_streams": streams, "independent_streams_no_reuse": unique, "planned_suffix_attempt_count": len(streams), "native_rng_resampling": "none; exact checkpoint restores preserve native game RNG state; only policy-sampling RNG is independently seeded", "full_native_state_q_estimand": "Q(s_full,a;pi_suffix) conditional on the exact full checkpoint and only policy-sampling randomness", "public_information_set_q_estimand": "Q(I_public,a) is not identified without legal hidden-state sampling conditioned on the public observation", "created_before_any_gate_c_suffix_attempt": True}
    write_receipt(path, plan, "Issue 73: precommit Gate C roots, actions, and independent suffix seed streams")
    return plan


def first_action_value(values: Sequence[float]) -> dict[str, Any]:
    data=np.asarray(values,dtype=np.float64)
    if data.size == 0: return {"n": 0}
    return {"n": int(data.size), "mean_terminal_floor": float(np.mean(data)), "sample_variance_terminal_floor": float(np.var(data,ddof=1)) if data.size > 1 else 0.0, "standard_error": float(np.std(data,ddof=1)/math.sqrt(data.size)) if data.size > 1 else 0.0, "values": [float(x) for x in data]}


def gate_c_uncertainty(groups: Mapping[int, Sequence[float]]) -> dict[str, Any]:
    idx=sorted(groups)
    if len(idx)!=2 or any(len(groups[i])!=8 for i in idx): return {"status":"INCOMPLETE", "per_action":{str(i):first_action_value(groups[i]) for i in idx}}
    left,right=(np.asarray(groups[i],dtype=np.float64) for i in idx)
    v0,v1=float(np.var(left,ddof=1)),float(np.var(right,ddof=1))
    se=math.sqrt(v0/len(left)+v1/len(right))
    delta=float(np.mean(right)-np.mean(left))
    rng=np.random.default_rng(int(CONFIG["stage0"]["gate_c"]["bootstrap_seed"]))
    reps=int(CONFIG["stage0"]["gate_c"]["bootstrap_repetitions"])
    samples=np.empty(reps,dtype=np.float64)
    for i in range(reps): samples[i]=float(np.mean(rng.choice(right,size=len(right),replace=True))-np.mean(rng.choice(left,size=len(left),replace=True)))
    rank=float(np.mean((right[:,None]>left[None,:]) + 0.5*(right[:,None]==left[None,:])))
    return {"status":"COMPLETE", "action0":first_action_value(left), "action1":first_action_value(right), "unpaired_mean_difference_action1_minus_action0":delta, "unpaired_welch_standard_error":se, "independent_owner_policy_stream_percentile_95_ci": [float(x) for x in np.quantile(samples,[0.025,0.975])], "empirical_pairwise_rank_probability_action1_better_ties_half_weighted":rank, "two_standard_error_abstention":bool(abs(delta)<=2.0*se), "bootstrap_repetitions":reps, "bootstrap_seed":int(CONFIG["stage0"]["gate_c"]["bootstrap_seed"]), "native_game_rng_resampled":False, "interpretation":"uncertainty is conditional on the fixed full checkpoint and independent pi_suffix policy sampling; it does not estimate hidden-state/game-future randomness or Q(I_public,a)"}


def run_gate_c(roots: Sequence[Mapping[str, Any]], module: Any, encoder: Any, model: Any, model_digest: str) -> dict[str, Any]:
    plan=gate_c_plan(roots)
    if plan.get("status") != "READY":
        report={"schema_id":"issue73-gate-c-independent-policy-suffix-uncertainty-v1","status":"BLOCKED_PREREQUISITE_ROOTS_OR_CHECKPOINTS","planned_suffix_attempt_count":int(plan.get("planned_suffix_attempt_count",0)),"completed_suffix_attempt_count":0,"replay_substitutions":0,"plan_sha256":runner.sha256_file(OUT / "plans" / "gate-c-plan.json"),"reason":"fixed four-root, two-family, post-two-decision source-qualified set with serializable opaque checkpoints unavailable"}
        runner.atomic_json(OUT / "gate-c-report.json",report); commit_push("Issue 73: preserve Gate C prerequisite blocker"); return report
    path=OUT / "attempts" / "gate-c-fresh-adapter-probe.json"
    prior=read_json(path)
    if prior and prior.get("status")=="COMPLETE":
        probe=read_json(OUT / "gate-c-fresh-adapter-probe.json")
        if probe is None or prior.get("witness_status") != probe.get("status"):
            report={"schema_id":"issue73-gate-c-independent-policy-suffix-uncertainty-v1","status":"BLOCKED_CAPABILITY_PROBE_RECEIPT_MISSING_OR_MISMATCHED_NO_RERUN","planned_suffix_attempt_count":64,"completed_suffix_attempt_count":0,"replay_substitutions":0,"prior_attempt_receipt":prior,"probe_receipt":probe}
            runner.atomic_json(OUT / "gate-c-report.json",report); commit_push("Issue 73: preserve Gate C probe receipt mismatch"); return report
    elif prior and prior.get("status")=="STARTED":
        start_attempt("gate-c-fresh-adapter-probe",kind="fresh_adapter_checkpoint_restore_probe",cpu_reserve=10.0,native=True)
        report={"schema_id":"issue73-gate-c-independent-policy-suffix-uncertainty-v1","status":"BLOCKED_INTERRUPTED_FRESH_ADAPTER_PROBE_NO_RERUN","planned_suffix_attempt_count":64,"completed_suffix_attempt_count":0,"replay_substitutions":0}
        runner.atomic_json(OUT / "gate-c-report.json",report); commit_push("Issue 73: preserve interrupted Gate C capability probe"); return report
    elif prior and prior.get("status") != "STARTED":
        prior_probe=read_json(OUT / "gate-c-fresh-adapter-probe.json")
        if prior_probe is None:
            report={"schema_id":"issue73-gate-c-independent-policy-suffix-uncertainty-v1","status":"BLOCKED_PRIOR_CAPABILITY_PROBE_FAILED_NO_RERUN","planned_suffix_attempt_count":64,"completed_suffix_attempt_count":0,"replay_substitutions":0,"prior_probe_receipt":prior}
            runner.atomic_json(OUT / "gate-c-report.json",report); commit_push("Issue 73: preserve prior Gate C probe failure without rerun"); return report
        probe=prior_probe
    else:
        _,run=start_attempt("gate-c-fresh-adapter-probe",kind="fresh_adapter_checkpoint_restore_probe",cpu_reserve=10.0,native=True)
        started=tree_cpu(); first=next(r for r in roots if r["root_id"]==plan["roots"][0]["root_id"])
        fresh=runner.native_adapter(module,int(first["seed"]))
        cp=first["checkpoint"]
        error=None
        try:
            fresh.restore_checkpoint(cp)
            restored=True
        except Exception as exc:
            restored=False; error={"type":type(exc).__name__,"message":str(exc)[:500]}
        probe={"schema_id":"issue73-gate-c-fresh-adapter-capability-v1","status":"SUPPORTED" if restored else "BLOCKED_UNSUPPORTED_FRESH_ADAPTER_CHECKPOINT_RESTORE","fresh_adapter_checkpoint_id":getattr(fresh,"checkpoint_adapter_id",None),"checkpoint_adapter_id":getattr(cp,"adapter_id",None),"checkpoint_id":getattr(cp,"checkpoint_id",None),"native_restore_succeeded":restored,"native_restore_error":error,"stsr_source_file_sha256":CONFIG["identity"]["stsr_source_files_sha256"].get("src/sts_combat_rl/sim/lightspeed.py"),"pinned_api_contract":"LightSpeedAdapter.restore_checkpoint rejects a SimulatorCheckpoint whose adapter_id differs from its process-local adapter id","no_adapter_identity_bypass":True,"no_suffix_attempts_started_before_capability_probe":True}
        measured=tree_cpu()-started
        if measured > 10.0:
            probe={**probe,"observed_capability_status":probe["status"],"status":"FAIL_CPU_RESERVATION_EXCEEDED","reserved_cpu_seconds":10.0,"measured_process_cpu_seconds":measured}
        finish_attempt("gate-c-fresh-adapter-probe",measured_cpu=measured,status="COMPLETE",details={"witness_status":probe["status"],"native_restore_succeeded":restored})
        runner.atomic_json(OUT / "gate-c-fresh-adapter-probe.json",probe)
        commit_push("Issue 73: preserve Gate C fresh-adapter checkpoint capability result")
    if probe.get("status") != "SUPPORTED":
        report={"schema_id":"issue73-gate-c-independent-policy-suffix-uncertainty-v1","status":"BLOCKED_UNSUPPORTED_FRESH_ADAPTER_CHECKPOINT_RESTORE","planned_suffix_attempt_count":int(plan["planned_suffix_attempt_count"]),"completed_suffix_attempt_count":0,"failed_suffix_attempt_count":0,"replay_substitutions":0,"capability_probe":probe,"plan_sha256":runner.sha256_file(OUT / "plans" / "gate-c-plan.json"),"native_rng_resampling":"none","conditional_independence_claim":"not reached; no suffix draw was attempted", "full_native_state_q_estimand":"Q(s_full,a;pi_suffix) not estimated", "public_information_set_q_estimand":"Q(I_public,a) not estimated", "stop_reason":"the frozen adapter API is explicitly process-bound and rejected restoring this persisted checkpoint into a fresh adapter; no identity bypass or substitute was used"}
        runner.atomic_json(OUT / "gate-c-report.json",report); commit_push("Issue 73: stop Gate C on predeclared checkpoint restore limitation"); return report
    return _run_gate_c_suffixes(plan,roots,module,encoder,model,model_digest)


def _run_gate_c_suffixes(plan: Mapping[str, Any], roots: Sequence[Mapping[str, Any]], module: Any, encoder: Any, model: Any, model_digest: str) -> dict[str, Any]:
    by_id={r["root_id"]:r for r in roots}
    observations=[]; groups: dict[str,dict[int,list[float]]]={}
    for stream in plan["policy_rng_seed_streams"]:
        root=by_id[stream["root_id"]]; a=int(stream["candidate_index"]); rep=int(stream["replicate"])
        key=f"gate-c-{root['root_id']}-a{a}-r{rep:02d}"
        existing=read_json(OUT / "attempts" / f"{key}.json")
        if existing and existing.get("status")=="COMPLETE":
            run=read_json(OUT / "runs" / f"{key}.json")
            if not run or run.get("terminal_complete") is not True: break
            result={"run_id":key,"root_id":root["root_id"],"candidate_index":a,"replicate":rep,"suffix_policy_seed":stream["suffix_policy_seed"],"expert_seed":stream["expert_seed"],"terminal":run["terminal"],"cpu_seconds":run.get("cpu_seconds"),"wall_seconds":run.get("wall_seconds"),"max_rss_kib":run.get("max_rss_kib_sampled_for_run")}
            observations.append(result); groups.setdefault(root["root_id"],{}).setdefault(a,[]).append(float(run["terminal"]["terminal_floor"])); continue
        if existing:
            if existing.get("status")=="STARTED": start_attempt(key,kind="gate_c_terminal_suffix",cpu_reserve=float(CONFIG["stage0"]["gate_c"]["per_suffix_cpu_cap_seconds"]),native=True)
            break
        _,should=start_attempt(key,kind="gate_c_terminal_suffix",cpu_reserve=float(CONFIG["stage0"]["gate_c"]["per_suffix_cpu_cap_seconds"]),native=True)
        if not should: break
        started=tree_cpu()
        try:
            fresh=runner.native_adapter(module,int(root["seed"]))
            forced={"root_fingerprints":{k:root[k] for k in ("native_state_sha256","public_projection_sha256","public_resource_state_sha256")},"screen":root["screen"],"legal_action_identities":root["legal_action_identities"],"candidate_index":a}
            run=runner._run_one(out=OUT,module=module,model=model,model_digest=model_digest,seed=int(root["seed"]),arm="SUFFIX",encoder=encoder,checkpoint=root["checkpoint"],forced_root=forced,purpose="issue73-gatec-independent-policy-suffix",resource_label=key,adapter_override=fresh,capture_trace=False,suffix_random_seed=int(stream["suffix_policy_seed"]),expert_random_seed=int(stream["expert_seed"]),cpu_cap_seconds=float(CONFIG["stage0"]["gate_c"]["per_suffix_cpu_cap_seconds"]))
            if run.get("terminal_complete") is not True: raise Stage0Failure("suffix did not reach a genuine terminal")
            result={"run_id":key,"root_id":root["root_id"],"candidate_index":a,"replicate":rep,"suffix_policy_seed":stream["suffix_policy_seed"],"expert_seed":stream["expert_seed"],"terminal":run["terminal"],"cpu_seconds":run.get("cpu_seconds"),"wall_seconds":run.get("wall_seconds"),"max_rss_kib":run.get("max_rss_kib_sampled_for_run")}
            observations.append(result); groups.setdefault(root["root_id"],{}).setdefault(a,[]).append(float(run["terminal"]["terminal_floor"]))
            finished=finish_attempt(key,measured_cpu=tree_cpu()-started,status="COMPLETE",details={"root_id":root["root_id"],"candidate_index":a,"replicate":rep,"terminal_floor":run["terminal"]["terminal_floor"],"suffix_policy_seed":stream["suffix_policy_seed"],"expert_seed":stream["expert_seed"],"native_game_rng_resampled":False})
            runner.append_jsonl(OUT / "gate-c-suffixes.jsonl", {"attempt":key,"observation":result})
            commit_push(f"Issue 73: preserve Gate C suffix {key}")
            if finished["status"] != "COMPLETE":
                report={"schema_id":"issue73-gate-c-independent-policy-suffix-uncertainty-v1","status":"FAIL_CPU_RESERVATION_EXCEEDED_NO_REPLACEMENTS","planned_suffix_attempt_count":int(CONFIG["stage0"]["gate_c"]["nominal_total"]),"completed_suffix_attempt_count":len(observations),"failed_suffix_attempt_count":0,"over_budget_terminal_attempt":key,"partial_observations":observations,"replay_substitutions":0,"resource_accounting":budget_summary()}
                runner.atomic_json(OUT / "gate-c-report.json",report); commit_push("Issue 73: preserve Gate C suffix CPU reservation breach"); return report
        except BaseException as exc:
            finish_attempt(key,measured_cpu=tree_cpu()-started,status="FAIL_OR_INCOMPLETE",details={"root_id":root["root_id"],"candidate_index":a,"replicate":rep,"failure_type":type(exc).__name__,"failure":str(exc)[:1000]})
            report={"schema_id":"issue73-gate-c-independent-policy-suffix-uncertainty-v1","status":"FAIL_OR_PARTIAL_NO_REPLACEMENTS","planned_suffix_attempt_count":64,"completed_suffix_attempt_count":len(observations),"failed_attempt":key,"failure_type":type(exc).__name__,"failure":str(exc)[:1000],"partial_observations":observations,"replay_substitutions":0,"native_rng_resampling":False,"resource_accounting":budget_summary()}
            runner.atomic_json(OUT / "gate-c-report.json",report); commit_push("Issue 73: preserve Gate C incomplete suffix without replacement"); return report
    expected=int(CONFIG["stage0"]["gate_c"]["nominal_total"])
    if len(observations)!=expected:
        report={"schema_id":"issue73-gate-c-independent-policy-suffix-uncertainty-v1","status":"FAIL_OR_PARTIAL_NO_REPLACEMENTS","planned_suffix_attempt_count":expected,"completed_suffix_attempt_count":len(observations),"failed_attempt_or_missing":True,"partial_observations":observations,"replay_substitutions":0,"resource_accounting":budget_summary()}
        runner.atomic_json(OUT / "gate-c-report.json",report); commit_push("Issue 73: preserve Gate C incomplete planned suffix set"); return report
    summary={root_id:gate_c_uncertainty(actions) for root_id,actions in groups.items()}
    report={"schema_id":"issue73-gate-c-independent-policy-suffix-uncertainty-v1","status":"PASS" if len(summary)==int(CONFIG["stage0"]["gate_c"]["root_count"]) and all(x.get("status")=="COMPLETE" for x in summary.values()) else "FAIL_INCOMPLETE_ROOT_ACTION_GROUPS","planned_suffix_attempt_count":expected,"completed_suffix_attempt_count":len(observations),"failed_suffix_attempt_count":0,"replay_substitutions":0,"per_root_action_uncertainty":summary,"attempts":observations,"native_rng_resampling":"none; fixed checkpoint RNG state restored in each suffix; only independent pi_suffix policy RNG streams resampled", "full_native_state_q_estimand":"Q(s_full,a;pi_suffix) conditional on exact checkpoint and stochastic continuation policy", "public_information_set_q_estimand":"Q(I_public,a) not estimated absent legal hidden-state sampling conditioned on the public observation", "resource_accounting":budget_summary()}
    runner.atomic_json(OUT / "gate-c-report.json",report); commit_push("Issue 73: finalize Gate C return and uncertainty evidence"); return report

def evidence_manifest() -> dict[str, Any]:
    excluded={"evidence-manifest.json","stage0-report.json"}
    rows=[]
    for path in sorted(STUDY.rglob("*")):
        if not path.is_file() or path.name in excluded or "__pycache__" in path.parts: continue
        rows.append({"path":path.relative_to(STUDY).as_posix(),"bytes":path.stat().st_size,"sha256":runner.sha256_file(path)})
    manifest={"schema_id":"issue73-stage0-evidence-manifest-v1","files":rows,"excluded_for_nonrecursive_manifest_reference":sorted(excluded),"total_included_bytes":sum(x["bytes"] for x in rows)}
    runner.atomic_json(OUT / "evidence-manifest.json",manifest)
    return {"path":(OUT / "evidence-manifest.json").relative_to(STUDY).as_posix(),"sha256":runner.sha256_file(OUT / "evidence-manifest.json"),"bytes":(OUT / "evidence-manifest.json").stat().st_size,"included_file_count":len(rows),"included_bytes":manifest["total_included_bytes"],"excluded":sorted(excluded)}


def execute() -> dict[str, Any]:
    ensure_layout()
    locked=read_json(OUT / "preseed-source-lock.json")
    if not locked or locked.get("status") != "PRESEED_LOCKED_BEFORE_ANY_DEBUG_SEED" or locked.get("config_sha256") != CONFIG_SHA:
        raise Stage0Failure("exact committed Stage 0 preseed lock is missing or mismatched")
    if locked.get("base_commit") != CONFIG["identity"]["base_study_commit"] or locked.get("stage1_authorized") is not False:
        raise Stage0Failure("preseed lineage or Stage 1 denial differs from registered config")
    commit_push("Issue 73: recover any preseeded Stage 0 local receipts before resume")
    remote=git("ls-remote","origin",f"refs/heads/{BRANCH}").split()
    head=git("rev-parse","HEAD")
    if not remote or remote[0] != head: raise Stage0Failure("Stage 0 branch is not exactly durable on the remote before execution")
    install_process_cpu_ceiling(native=False)
    setup_start=tree_cpu()
    env,module,encoder,runtime,model,model_digest=setup_environment()
    setup_elapsed=tree_cpu()-setup_start
    if runtime.get("optimizer_id") != "issue65-demand-driven-public-history-v1": raise Stage0Failure("source-pinned Issue 65 public-history runtime is not active")
    runner.atomic_json(OUT / "execution-identity.json",{"schema_id":"issue73-stage0-execution-identity-v1","preseed_commit":locked.get("preseed_commit"),"config_sha256":CONFIG_SHA,"loaded_native_module_path":env["identity"]["native_module_path"],"loaded_native_module_sha256":env["identity"]["native_module_sha256"],"native_source_commit":env["identity"]["native_source_commit"],"stsr_source_commit":env["identity"]["stsr_source_commit"],"stsr_native_integration_commit":env["identity"]["stsr_native_integration_commit"],"optimizer_runtime":runtime})
    add_setup_cpu(setup_elapsed,"environment-validation-and-initial-policy")
    commit_push("Issue 73: preserve verified Stage 0 runtime before debug owners")
    install_process_cpu_ceiling(native=True)
    roots,root_status=collect_roots(module,encoder,model,model_digest)
    gate_a_report=run_gate_a(roots)
    gate_b_report=run_gate_b(roots,module,encoder,model,model_digest)
    gate_c_report=run_gate_c(roots,module,encoder,model,model_digest)
    all_passed=all(x.get("status")=="PASS" for x in (gate_a_report,gate_b_report,gate_c_report))
    terminal="PRESEED_READY_FOR_PLANNER_ACTIVATION_DECISION" if all_passed else "PRESEED_BLOCKED"
    manifest=evidence_manifest()
    report={"schema_id":"issue73-stage0-integrated-gates-v1","terminal_status":terminal,"registered_scope":"Stage 0 debug-only Gates A/B/C; no formal owners, training, evaluation, or Stage 1 execution", "source_status":"SOURCE_PINNED_AND_VERIFIED", "config_sha256":CONFIG_SHA,"preseed_source_lock":locked,"root_collection":root_status,"gates":{"A":gate_a_report,"B":gate_b_report,"C":gate_c_report},"gate_statuses":{"A":gate_a_report.get("status"),"B":gate_b_report.get("status"),"C":gate_c_report.get("status")},"formal_training_seed_consumed":False,"formal_evaluation_seed_consumed":False,"stage1_authorized":False,"stage1_launched":False,"native_rng_resampling_for_gate_c":False,"source_claim_limitations":["Gate A is synthetic optimization responsiveness only, not return quality or policy generalization.","Gate B hidden projection and one chest opening are native-surrogate semantics, not CommunicationMod/live fidelity or a conditional return distribution.","Gate C, if reached, estimates only full-state checkpoint conditional returns under independent stochastic policy sampling; native hidden/game future RNG is not independently resampled and Q(I_public,a) is not identified."],"resource_accounting":budget_summary(),"evidence_manifest":manifest,"finished_at_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
    runner.atomic_json(OUT / "stage0-report.json",report)
    commit_push("Issue 73: preserve integrated Stage 0 terminal report and evidence manifest")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description="Issue 73 Stage 0 gates only; there is intentionally no Stage 1 command")
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare",action="store_true",help="freeze and push source/config lock before any debug owner")
    group.add_argument("--execute",action="store_true",help="resume or execute the already-locked Stage 0 gates")
    group.add_argument("--status",action="store_true",help="print durable Stage 0 status without running seeds")
    args=parser.parse_args(argv)
    try:
        if args.prepare: print(prepare()); return 0
        if args.status:
            report=read_json(OUT / "stage0-report.json")
            print(json.dumps(report or {"status":"PRESEED_LOCKED_NOT_EXECUTED","resource_accounting":budget_summary()},indent=2,sort_keys=True)); return 0
        report=execute()
        print(json.dumps({"terminal_status":report["terminal_status"],"gate_statuses":report["gate_statuses"],"resource_accounting":report["resource_accounting"],"evidence_manifest":report["evidence_manifest"]},indent=2,sort_keys=True))
        return 0 if report["terminal_status"] == "PRESEED_READY_FOR_PLANNER_ACTIVATION_DECISION" else 2
    except BaseException as exc:
        failure={"schema_id":"issue73-stage0-execution-failure-v1","status":"SOURCE_INVALID" if isinstance(exc,Stage0Failure) else "BUILDER_FAILURE","failure_type":type(exc).__name__,"failure":str(exc)[:1500],"config_sha256":CONFIG_SHA,"resource_accounting":budget_summary() if OUT.exists() and (OUT / "resource-ledger.json").exists() else None,"stage1_authorized":False,"formal_training_or_evaluation_seeds_consumed":False}
        if OUT.exists():
            first_failure = OUT / "execution-failure.json"
            if not first_failure.exists():
                runner.atomic_json(first_failure,failure)
            event_id = runner.sha256_bytes(json.dumps(failure,sort_keys=True,separators=(",",":")).encode("utf-8"))
            events_path = OUT / "execution-failure-events.jsonl"
            prior_ids = set()
            if events_path.exists():
                for line in events_path.read_text(encoding="utf-8").splitlines():
                    try: prior_ids.add(json.loads(line).get("event_id"))
                    except Exception: pass
            if event_id not in prior_ids:
                runner.append_jsonl(events_path,{**failure,"event_id":event_id,"first_failure_receipt_preserved":first_failure.exists()})
            try: commit_push("Issue 73: preserve Stage 0 execution failure boundary")
            except Exception: pass
        print(json.dumps(failure,indent=2,sort_keys=True),file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
