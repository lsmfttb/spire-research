"""Fail-closed resource-only continuation for the Issue 60 R1 amendment.

This module owns only provenance, budget arithmetic, and authorization checks.
It does not change the policy, simulator, training objective, keys, or endpoint.
"""
from __future__ import annotations

import ast
import copy
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import time
from typing import Any, Mapping


ISSUE = 60
PARENT_HANDOFF = "d42b04235f4a8fead844998428e8cc35d2d64f03"
R1_STATUS_PARENT_HANDOFF = "25916a05e959e359b8aa63be28891f2ac1e4c43f"
ORIGINAL_PRODUCER = "a7e8593558e6817d2ed34a5cd6bd2d5bd0a98768"
ORIGINAL_CONFIG_SHA256 = "07020535931cd81bef9b40fa1c30e75b553ac160bc53057e58f82bd8cf2007ca"
ORIGINAL_GATE = "evidence/preflight/native.json"
ORIGINAL_MANIFEST = "frozen-manifest.json"
ORIGINAL_RUNNER = "run_study.py"
R1_MANIFEST = "frozen-manifest-r1.json"
R1_AUTHORIZATION = "resource-authorization-r1.json"
R1_GATE = "evidence/preflight/resource-r1.json"
R1_SEAL = "evidence/preflight/resource-r1-seal.json"
R1_SYNTHETIC = "evidence/preflight/resource-r1-synthetic.json"
R1_STATUS_RECONCILIATION = "evidence/attempt-status-r1-reconciliation.json"
CPU_LEDGER = "evidence/cpu-ledger.jsonl"
R1_ALLOWANCES = (
    ("issue60-r1-planner-administration", "planner", 200.0),
    ("issue60-r1-builder-administration", "builder", 200.0),
)
R1_RUNNER_SYMBOLS = ("REVIEWER_RESERVE", "PROCESS_BUDGET", "run_training", "main", "run_game")
R1_ADDED_SOURCE_PATHS = (
    "attempt_lifecycle.py",
    "resource_amendment_r1.py",
    "resource-amendment-r1.json",
    "prepare_resource_amendment_r1.py",
    "freeze_manifest_r1.py",
    "test_resource_amendment_r1.py",
)
R1_MODIFIED_SOURCE_PATHS = ("run_study.py",)
R1_DELETED_SOURCE_PATHS: tuple[str, ...] = ()
EXPECTED_R1_SOURCE_PATHS = tuple(sorted((*R1_ADDED_SOURCE_PATHS, *R1_MODIFIED_SOURCE_PATHS)))
R1_RESOURCE_VALUES = {
    "process_cpu_cap_seconds": 200000.0,
    "reviewer_reserve_seconds": 20000.0,
    "builder_allowance_seconds": 180000.0,
    "prior_reviewer_allowance_seconds": 1000.0,
    "future_reviewer_reserve_seconds": 19000.0,
    "planner_administration_allowance_seconds": 200.0,
    "builder_administration_allowance_seconds": 200.0,
    "game_cost_multiplier": 1.5,
    "forecast_headroom_fraction": 0.2,
    "remaining_games": 2688,
    "remaining_optimizer_minibatches": 1984,
    "optimizer_cost_multiplier": 1.5,
}
R1_STAGE_FINALIZATION_MARGIN_SECONDS = 10.0


class ResourceAmendmentError(RuntimeError):
    """The resource-only amendment carry-over chain cannot be established."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResourceAmendmentError(f"required R1 evidence is missing or invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise ResourceAmendmentError(f"expected a JSON object in {path.name}")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temp.open("wb") as stream:
        stream.write(_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _repo_root(study_root: Path) -> Path:
    return study_root.resolve().parents[1]


def _git_process(study_root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    root = _repo_root(study_root)
    git_file = root / ".git"
    if os.name != "nt" and git_file.is_file():
        marker = git_file.read_text(encoding="utf-8").strip().removeprefix("gitdir:").strip()
        if len(marker) >= 4 and marker[1:3] == ":/":
            executable = shutil.which("git.exe")
            if not executable:
                raise ResourceAmendmentError("Windows worktree pointer requires git.exe inside WSL")
            win_root = subprocess.run(["wslpath", "-w", str(root)], check=True,
                                      capture_output=True, text=True).stdout.strip().replace("\\", "/")
            command = [executable, "-c", f"safe.directory={win_root}", "-C", win_root, *args]
        else:
            command = ["git", "-c", f"safe.directory={root}", "-C", str(root), *args]
    else:
        command = ["git", "-c", f"safe.directory={root}", "-C", str(root), *args]
    return subprocess.run(command, capture_output=True, text=text)


def _git(study_root: Path, *args: str, check: bool = True) -> str:
    result = _git_process(study_root, *args)
    if check and result.returncode:
        raise ResourceAmendmentError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_bytes(study_root: Path, *args: str) -> bytes:
    result = _git_process(study_root, *args, text=False)
    if result.returncode:
        raise ResourceAmendmentError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def _committed_bytes(study_root: Path, relative: str, revision: str = "HEAD") -> bytes:
    return _git_bytes(study_root, "show", f"{revision}:{_study_git_path(study_root, relative)}")


def _study_git_path(study_root: Path, relative: str) -> str:
    return study_root.resolve().relative_to(_repo_root(study_root).resolve()).as_posix() + "/" + relative


def _assert_committed_unchanged(study_root: Path, relative: str) -> None:
    path = study_root / relative
    if not path.is_file() or path.read_bytes() != _committed_bytes(study_root, relative):
        raise ResourceAmendmentError(f"committed R1 anchor changed in the worktree: {relative}")


def _amendment(study_root: Path) -> dict[str, Any]:
    return _read_json(study_root / "resource-amendment-r1.json")


def _check_config_identity(config_bytes: bytes, expected_sha256: str = ORIGINAL_CONFIG_SHA256) -> None:
    if sha256_bytes(config_bytes) != expected_sha256:
        raise ResourceAmendmentError("scientific config differs from the original registered config")


def _source_nodes(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    rows: list[tuple[str, ast.AST]] = []
    for index, node in enumerate(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            name = "assignment:" + ",".join(names) if names else f"assignment@{index}"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            name = f"import@{index}"
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            name = "module-docstring"
        else:
            name = f"statement@{index}"
        rows.append((name, node))
    return rows


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    found = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(found) != 1:
        raise ResourceAmendmentError(f"runner whitelist expected exactly one {name} function")
    return found[0]


def _expected_ast_statements(source: str) -> list[ast.stmt]:
    return ast.parse(source).body


def _without_terminal_status_write(function: ast.FunctionDef) -> list[ast.stmt]:
    body = copy.deepcopy(function.body)
    expected = _expected_ast_statements("""
from attempt_lifecycle import finalized_attempt_status
atomic_json(attempt_dir / "status.json", finalized_attempt_status(status, summary))
""")
    wanted = ast.dump(ast.Module(body=expected, type_ignores=[]), include_attributes=False)
    matches = []
    for index in range(len(body) - len(expected) + 1):
        candidate = body[index:index + len(expected)]
        if ast.dump(ast.Module(body=candidate, type_ignores=[]), include_attributes=False) == wanted:
            matches.append(index)
    if len(matches) != 1:
        raise ResourceAmendmentError("R1 runner must add exactly one terminal attempt-status write")
    index = matches[0]
    if index + len(expected) != len(body) - 1 or not isinstance(body[-1], ast.Return):
        raise ResourceAmendmentError("terminal attempt-status write must follow run.json and precede return")
    body[index:index + len(expected)] = []
    return body


def _nested_statement_lists(node: ast.AST | list[ast.stmt]):
    if isinstance(node, list):
        if node and all(isinstance(value, ast.stmt) for value in node):
            yield node
        for value in node:
            if isinstance(value, ast.AST):
                yield from _nested_statement_lists(value)
    elif isinstance(node, ast.AST):
        for _, value in ast.iter_fields(node):
            if isinstance(value, (ast.AST, list)):
                yield from _nested_statement_lists(value)


def _normalize_choices(function: ast.FunctionDef) -> list[ast.stmt]:
    output = []
    found = 0
    for statement in function.body:
        cloned = ast.parse(ast.unparse(statement)).body[0]
        for node in ast.walk(cloned):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "parser"
                    and node.func.attr == "add_argument" and node.args
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value == "--stage"):
                continue
            choices = next((keyword for keyword in node.keywords if keyword.arg == "choices"), None)
            if choices is None or not isinstance(choices.value, (ast.Tuple, ast.List)):
                raise ResourceAmendmentError("runner whitelist could not read stage choices")
            values = [element.value for element in choices.value.elts if isinstance(element, ast.Constant)]
            if len(values) != len(choices.value.elts):
                raise ResourceAmendmentError("runner stage choices contain a dynamic expression")
            expected = ["synthetic", "verify", "preflight", "train", "evaluate", "analyze"]
            if values not in (expected, [*expected[:3], "resource-r1", "seal-resource-r1", *expected[3:]]):
                raise ResourceAmendmentError("runner stage choice change exceeds the R1 whitelist")
            choices.value = ast.Constant(value="<R1_RESOURCE_STAGE_CHOICES>")
            found += 1
        output.append(cloned)
    if found != 1:
        raise ResourceAmendmentError("runner whitelist expected one --stage choices argument")
    return output


def _normalize_main(tree: ast.Module, *, is_r1: bool) -> list[ast.stmt]:
    function = _function(tree, "main")
    body = copy.deepcopy(function.body)
    proxy = ast.Module(body=body, type_ignores=[])
    body = _normalize_choices(proxy)
    lists = list(_nested_statement_lists(body))
    if not is_r1:
        expected_legacy = _expected_ast_statements("""
if args.stage == "synthetic":
    result = run_witnesses(HERE, EVIDENCE / "preflight" / "synthetic.json")
    print(json.dumps(result, indent=2))
    status = "COMPLETE"
    return 0
""")[0]
        legacy_test = ast.dump(ast.parse('args.stage == "synthetic"').body[0].value, include_attributes=False)
        locations = [(statements, index) for statements in lists for index, node in enumerate(statements)
                     if isinstance(node, ast.If) and ast.dump(node.test, include_attributes=False) == legacy_test]
        if len(locations) != 1 or ast.dump(locations[0][0][locations[0][1]], include_attributes=False) != ast.dump(expected_legacy, include_attributes=False):
            raise ResourceAmendmentError("original synthetic evidence stage differs from its registered baseline")
        statements, index = locations[0]
        statements.pop(index)
    else:
        expected_guard = _expected_ast_statements("""
if args.stage in {"synthetic", "preflight"}:
    parser.error("original evidence-producing stages are immutable; use the R1 resource stage")
""")[0]
        guard_test = ast.dump(ast.parse('args.stage in {"synthetic", "preflight"}').body[0].value,
                              include_attributes=False)
        locations = [(statements, index) for statements in lists for index, node in enumerate(statements)
                     if isinstance(node, ast.If) and ast.dump(node.test, include_attributes=False) == guard_test]
        if len(locations) != 1 or ast.dump(locations[0][0][locations[0][1]], include_attributes=False) != ast.dump(expected_guard, include_attributes=False):
            raise ResourceAmendmentError("R1 must fail closed before overwriting original synthetic/preflight evidence")
        statements, index = locations[0]
        statements.pop(index)
    old_call = _expected_ast_statements("verify_frozen_manifest()")[0]
    expected_new_gate = _expected_ast_statements("""
if args.stage in {"resource-r1", "seal-resource-r1"}:
    from resource_amendment_r1 import run_resource_stage, seal_resource_gate
    if args.stage == "resource-r1":
        result = run_resource_stage(HERE, stage_started)
    else:
        result = seal_resource_gate(HERE, stage_started)
    print(json.dumps(result, indent=2))
    status = "COMPLETE"
    return 0
from resource_amendment_r1 import verify_resource_chain
verify_resource_chain(
    HERE,
    require_gate_seal=args.stage in {"verify", "train", "evaluate", "analyze"},
)
""")
    lists = list(_nested_statement_lists(body))
    old_locations = [(statements, i) for statements in lists for i, node in enumerate(statements)
                     if ast.dump(node, include_attributes=False) == ast.dump(old_call, include_attributes=False)]
    if not is_r1:
        if len(old_locations) != 1:
            raise ResourceAmendmentError("original runner no longer has its registered manifest gate")
        statements, index = old_locations[0]
        statements[index:index + 1] = [old_call]
        return body

    resource_test = ast.dump(ast.parse('args.stage in {"resource-r1", "seal-resource-r1"}').body[0].value,
                             include_attributes=False)
    start_locations = [(statements, i) for statements in lists for i, node in enumerate(statements)
                       if isinstance(node, ast.If) and ast.dump(node.test, include_attributes=False) == resource_test]
    if len(start_locations) != 1 or old_locations:
        raise ResourceAmendmentError("R1 runner lacks exactly one authorized resource-stage dispatch")
    statements, start = start_locations[0]
    import_index = next((i for i in range(start, len(statements))
                         if isinstance(statements[i], ast.ImportFrom)
                         and statements[i].module == "resource_amendment_r1"
                         and any(alias.name == "verify_resource_chain" for alias in statements[i].names)), None)
    if import_index is None:
        raise ResourceAmendmentError("R1 runner is missing the resource chain gate")
    chain_index = next((i for i in range(import_index + 1, len(statements))
                        if isinstance(statements[i], ast.Expr)
                        and isinstance(statements[i].value, ast.Call)
                        and isinstance(statements[i].value.func, ast.Name)
                        and statements[i].value.func.id == "verify_resource_chain"), None)
    if chain_index is None:
        raise ResourceAmendmentError("R1 runner is missing the resource chain verification call")
    actual_new = statements[start:chain_index + 1]
    if ast.dump(ast.Module(body=actual_new, type_ignores=[]), include_attributes=False) != ast.dump(ast.Module(body=expected_new_gate, type_ignores=[]), include_attributes=False):
        raise ResourceAmendmentError("R1 main() resource-stage/gate dispatch differs from the approved form: "
                                     + ast.unparse(ast.Module(body=actual_new, type_ignores=[])))
    statements[start:chain_index + 1] = [old_call]
    return body


def verify_runner_whitelist(original_source: bytes, r1_source: bytes) -> None:
    """Require the exact R1 budget and gate changes in the old runner.

    All original top-level declarations other than the two resource constants
    and the narrowly replaced train/main gates must be AST-identical.
    """
    try:
        original = ast.parse(original_source.decode("utf-8"))
        amended = ast.parse(r1_source.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ResourceAmendmentError("R1 runner is not valid UTF-8 Python source") from exc
    old_nodes = _source_nodes(original)
    new_nodes = _source_nodes(amended)
    if [name for name, _ in old_nodes] != [name for name, _ in new_nodes]:
        raise ResourceAmendmentError("R1 runner added, removed, or reordered top-level declarations")
    permitted = {"assignment:REVIEWER_RESERVE", "assignment:PROCESS_BUDGET", "run_training", "main", "run_game"}
    for (name, old), (_, new) in zip(old_nodes, new_nodes):
        if name not in permitted and ast.dump(old, include_attributes=False) != ast.dump(new, include_attributes=False):
            raise ResourceAmendmentError(f"R1 changed non-whitelisted runner declaration: {name}")
        if name == "assignment:REVIEWER_RESERVE":
            if not (isinstance(new, ast.Assign) and isinstance(new.value, ast.Constant)
                    and new.value.value == R1_RESOURCE_VALUES["reviewer_reserve_seconds"]):
                raise ResourceAmendmentError("R1 reviewer reserve is not the authorized 20,000 CPU-s")
        if name == "assignment:PROCESS_BUDGET":
            if not (isinstance(new, ast.Assign) and isinstance(new.value, ast.Constant)
                    and new.value.value == R1_RESOURCE_VALUES["process_cpu_cap_seconds"]):
                raise ResourceAmendmentError("R1 cumulative cap is not the authorized 200,000 CPU-s")
        if name == "run_training":
            old_function = old
            new_function = new
            assert isinstance(old_function, ast.FunctionDef) and isinstance(new_function, ast.FunctionDef)
            old_prefix = _expected_ast_statements("""
gate_path = EVIDENCE / "preflight" / "native.json"
if not gate_path.exists():
    raise RuntimeError("native preflight gate is missing")
gate = json.loads(gate_path.read_text(encoding="utf-8"))
if gate.get("status") != "PASS" or gate.get("config_sha256") != config_sha():
    raise RuntimeError("fixed preflight has not authorized this exact config")
""")
            new_prefix = _expected_ast_statements("""
from resource_amendment_r1 import require_training_gate
require_training_gate(HERE, config)
""")
            if ast.dump(ast.Module(body=old_function.body[:4], type_ignores=[]), include_attributes=False) != ast.dump(ast.Module(body=old_prefix, type_ignores=[]), include_attributes=False):
                raise ResourceAmendmentError("original training gate differs from the registered baseline")
            if ast.dump(ast.Module(body=new_function.body[:2], type_ignores=[]), include_attributes=False) != ast.dump(ast.Module(body=new_prefix, type_ignores=[]), include_attributes=False):
                raise ResourceAmendmentError("R1 training gate differs from the authorized chain consumer")
            if ast.dump(ast.Module(body=old_function.body[4:], type_ignores=[]), include_attributes=False) != ast.dump(ast.Module(body=new_function.body[2:], type_ignores=[]), include_attributes=False):
                raise ResourceAmendmentError("R1 altered training behavior beyond the resource gate")
        if name == "run_game":
            if not (isinstance(old, ast.FunctionDef) and isinstance(new, ast.FunctionDef)):
                raise ResourceAmendmentError("R1 game runner declaration changed type")
            if ast.dump(ast.Module(body=old.body, type_ignores=[]), include_attributes=False) != ast.dump(
                    ast.Module(body=_without_terminal_status_write(new), type_ignores=[]), include_attributes=False):
                raise ResourceAmendmentError("R1 changed game behavior beyond terminal status serialization")
    old_main = _normalize_main(original, is_r1=False)
    new_main = _normalize_main(amended, is_r1=True)
    if ast.dump(ast.Module(body=old_main, type_ignores=[]), include_attributes=False) != ast.dump(ast.Module(body=new_main, type_ignores=[]), include_attributes=False):
        raise ResourceAmendmentError("R1 altered main() beyond the stage/gate resource dispatch")


def _ledger_prefix(ledger_bytes: bytes, line_count: int) -> tuple[bytes, list[bytes]]:
    lines = ledger_bytes.splitlines(keepends=True)
    if len(lines) < line_count:
        raise ResourceAmendmentError("CPU ledger has fewer rows than the original handoff")
    prefix = b"".join(lines[:line_count])
    return prefix, lines


def _verify_original_evidence(study_root: Path, amendment: Mapping[str, Any]) -> dict[str, Any]:
    config_bytes = (study_root / "config.json").read_bytes()
    _check_config_identity(config_bytes, str(amendment["original_config_sha256"]))
    original_manifest_bytes = (study_root / ORIGINAL_MANIFEST).read_bytes()
    original_gate_bytes = (study_root / ORIGINAL_GATE).read_bytes()
    if sha256_bytes(original_manifest_bytes) != amendment["original_manifest_sha256"]:
        raise ResourceAmendmentError("the original frozen manifest identity changed")
    if sha256_bytes(original_gate_bytes) != amendment["original_preflight_gate_sha256"]:
        raise ResourceAmendmentError("the original RESOURCE_BLOCKED gate identity changed")
    preserved_original_files = {
        "evidence/preflight/synthetic.json": amendment["original_synthetic_witness_sha256"],
        "evidence/stage-status/synthetic.json": amendment["original_synthetic_stage_status_sha256"],
        "evidence/stage-status/preflight.json": amendment["original_preflight_stage_status_sha256"],
    }
    for relative, expected_hash in preserved_original_files.items():
        path = study_root / relative
        if not path.is_file() or sha256_bytes(path.read_bytes()) != expected_hash:
            raise ResourceAmendmentError(f"original preflight-stage evidence changed: {relative}")
    old_manifest = json.loads(original_manifest_bytes)
    old_gate = json.loads(original_gate_bytes)
    if old_manifest.get("schema_id") != "issue60-frozen-manifest-v1":
        raise ResourceAmendmentError("the original manifest schema is unexpected")
    if old_manifest.get("implementation_commit") != ORIGINAL_PRODUCER:
        raise ResourceAmendmentError("the original manifest producer identity changed")
    if old_manifest.get("config_sha256") != amendment["original_config_sha256"]:
        raise ResourceAmendmentError("the original manifest config identity disagrees with R1")
    if old_gate.get("schema_id") != "issue60-preflight-budget-gate-v1" or old_gate.get("status") != "RESOURCE_BLOCKED":
        raise ResourceAmendmentError("the original resource failure must remain RESOURCE_BLOCKED")
    if old_gate.get("config_sha256") != amendment["original_config_sha256"]:
        raise ResourceAmendmentError("the original resource gate belongs to a different config")
    if old_gate.get("preflight_runs_complete") != 8:
        raise ResourceAmendmentError("the original gate no longer records eight completed preflight games")
    if old_gate.get("preflight_seeds") != list(range(668001, 668009)):
        raise ResourceAmendmentError("the original gate no longer records the fixed preflight seeds")
    if old_gate.get("projected_builder_total_cpu_seconds", 0) <= 108000:
        raise ResourceAmendmentError("the original gate no longer establishes the original budget stop")
    old_files = {entry["path"]: entry["sha256"] for entry in old_manifest.get("files", [])}
    runner_hash = old_files.get(ORIGINAL_RUNNER)
    if not isinstance(runner_hash, str) or runner_hash != amendment["original_runner_sha256"]:
        raise ResourceAmendmentError("the original runner hash is not anchored by its original manifest")
    for relative, expected_hash in old_files.items():
        if relative == ORIGINAL_RUNNER:
            continue
        path = study_root / relative
        if not path.is_file() or sha256_bytes(path.read_bytes()) != expected_hash:
            raise ResourceAmendmentError(f"original scientific source/config bytes changed: {relative}")
    original_runner = _git_bytes(
        study_root, "show", f"{PARENT_HANDOFF}:{_study_git_path(study_root, ORIGINAL_RUNNER)}"
    )
    if sha256_bytes(original_runner) != runner_hash:
        raise ResourceAmendmentError("the parent handoff runner differs from the original frozen manifest")
    ledger = (study_root / CPU_LEDGER).read_bytes()
    count = int(amendment["original_cpu_ledger_line_count"])
    prefix, _ = _ledger_prefix(ledger, count)
    if sha256_bytes(prefix) != amendment["original_cpu_ledger_prefix_sha256"]:
        raise ResourceAmendmentError("the pre-amendment CPU history is not an intact prefix")
    if len(prefix) != int(amendment["original_cpu_ledger_prefix_bytes"]):
        raise ResourceAmendmentError("the pre-amendment CPU history has an unexpected byte length")
    prior_rows = [json.loads(line) for line in prefix.splitlines() if line.strip()]
    prior_accounted = float(old_gate["implementation_and_static_allowance_cpu_seconds"]) + sum(
        float(row.get("process_cpu_seconds", 0.0)) for row in prior_rows
    )
    if not math.isclose(prior_accounted, float(old_gate["preflight_and_implementation_accounted_cpu_seconds"]), rel_tol=0.0, abs_tol=0.01):
        raise ResourceAmendmentError("original CPU ledger and original resource gate accountant do not reconcile")
    return {"old_manifest": old_manifest, "old_gate": old_gate, "old_files": old_files,
            "original_runner": original_runner, "ledger_prefix": prefix,
            "original_accounting_reconciliation_difference_seconds": (
                float(old_gate["preflight_and_implementation_accounted_cpu_seconds"]) - prior_accounted
            )}


def _verify_formal_runs_absent(study_root: Path) -> None:
    for stage in ("training", "evaluation"):
        directory = study_root / "evidence" / "runs" / stage
        if directory.exists() and any(directory.rglob("attempt-*")):
            raise ResourceAmendmentError(f"formal {stage} attempt exists before the R1 gate is sealed")


def _verify_original_preflight(study_root: Path, old_gate: Mapping[str, Any]) -> list[dict[str, Any]]:
    summaries = old_gate.get("preflight_summary")
    if not isinstance(summaries, list) or len(summaries) != 8:
        raise ResourceAmendmentError("the original resource gate lacks its eight run summaries")
    by_seed = {row.get("game_seed"): row for row in summaries if isinstance(row, Mapping)}
    if len(by_seed) != 8 or set(by_seed) != set(range(668001, 668009)):
        raise ResourceAmendmentError("the original preflight summary seed set is not exact")
    attempts: list[dict[str, Any]] = []
    for index, seed in enumerate(range(668001, 668009)):
        block = "block1" if index % 2 == 0 else "block2"
        directory = study_root / "evidence" / "runs" / "preflight" / block / f"seed-{seed}"
        complete = []
        for path in sorted(directory.glob("attempt-*/run.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if row.get("status") == "COMPLETE":
                complete.append((path, row))
        if len(complete) != 1:
            raise ResourceAmendmentError(f"preflight seed {seed} does not have exactly one complete run")
        if len(list(directory.glob("attempt-*"))) != 1:
            raise ResourceAmendmentError(f"preflight seed {seed} has extra or incomplete attempts")
        path, row = complete[0]
        if row.get("game_seed") != seed or row.get("config_sha256") != ORIGINAL_CONFIG_SHA256:
            raise ResourceAmendmentError(f"preflight seed {seed} does not match the original fixed config")
        if row.get("stage_key") != "preflight" or row.get("status") != "COMPLETE":
            raise ResourceAmendmentError(f"preflight seed {seed} has invalid stage/status")
        expected_row = by_seed[seed]
        if (expected_row.get("status") != "COMPLETE"
                or float(expected_row.get("process_cpu_seconds", -1)) != float(row.get("process_cpu_seconds", -2))):
            raise ResourceAmendmentError(f"preflight seed {seed} summary differs from its retained run record")
        decision_path = path.parent / "decisions.jsonl.gz"
        search_path = path.parent / "battle-search.jsonl.gz"
        for compressed in (decision_path, search_path):
            if not compressed.is_file():
                raise ResourceAmendmentError(f"preflight compressed evidence is missing: {compressed.name}")
            try:
                with gzip.open(compressed, "rb") as stream:
                    while stream.read(1024 * 1024):
                        pass
            except OSError as exc:
                raise ResourceAmendmentError(f"preflight gzip integrity failed: {compressed.name}") from exc
        attempts.append({"game_seed": seed, "block": block,
                         "run_sha256": sha256_bytes(path.read_bytes()),
                         "decisions_sha256": sha256_bytes(decision_path.read_bytes()),
                         "battle_search_sha256": sha256_bytes(search_path.read_bytes()),
                         "process_cpu_seconds": float(row["process_cpu_seconds"])})
    return attempts


def _verify_source_manifest(study_root: Path, amendment: Mapping[str, Any]) -> dict[str, Any]:
    manifest_path = study_root / R1_MANIFEST
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema_id") != "issue60-resource-frozen-manifest-r1-v1":
        raise ResourceAmendmentError("R1 frozen manifest has an unexpected schema")
    if manifest.get("base_handoff_commit") != PARENT_HANDOFF:
        raise ResourceAmendmentError("R1 frozen manifest has the wrong reviewed parent")
    producer = manifest.get("implementation_commit")
    if not isinstance(producer, str) or len(producer) != 40:
        raise ResourceAmendmentError("R1 producer commit is missing")
    check = _git_process(study_root, "merge-base", "--is-ancestor", producer, "HEAD")
    if check.returncode != 0:
        raise ResourceAmendmentError("the R1 producer is not an ancestor of the current execution commit")
    if manifest.get("amendment_config_sha256") != sha256_bytes((study_root / "resource-amendment-r1.json").read_bytes()):
        raise ResourceAmendmentError("R1 amendment config differs from its frozen hash")
    _assert_committed_unchanged(study_root, R1_MANIFEST)
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ResourceAmendmentError("R1 frozen manifest has no source file list")
    paths = [entry.get("path") for entry in entries if isinstance(entry, Mapping)]
    if len(paths) != len(entries) or len(paths) != len(set(paths)):
        raise ResourceAmendmentError("R1 frozen file list is malformed or duplicated")
    original_manifest = json.loads((study_root / ORIGINAL_MANIFEST).read_text(encoding="utf-8"))
    expected_paths = {entry["path"] for entry in original_manifest["files"]} | set(R1_ADDED_SOURCE_PATHS)
    if set(paths) != expected_paths:
        raise ResourceAmendmentError("R1 frozen source file set is incomplete or contains an unapproved path")
    if manifest.get("source_diff_whitelist") != amendment.get("source_diff_whitelist"):
        raise ResourceAmendmentError("R1 manifest source whitelist differs from the amendment config")
    for entry in entries:
        relative = entry.get("path")
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ResourceAmendmentError("R1 manifest contains an unsafe path")
        source = study_root / relative
        if not source.is_file() or sha256_bytes(source.read_bytes()) != entry.get("sha256"):
            raise ResourceAmendmentError(f"R1 frozen source changed or is missing: {relative}")
        producer_bytes = _committed_bytes(study_root, relative, producer)
        if sha256_bytes(producer_bytes) != entry.get("sha256"):
            raise ResourceAmendmentError(f"R1 producer tree differs from the frozen source: {relative}")
    patch_path = manifest.get("runner_diff_path")
    patch_sha = manifest.get("runner_diff_sha256")
    if patch_path != "evidence/resource-amendment-r1/runner.patch":
        raise ResourceAmendmentError("R1 manifest runner diff path is unexpected")
    patch = study_root / patch_path
    if not patch.is_file() or sha256_bytes(patch.read_bytes()) != patch_sha:
        raise ResourceAmendmentError("R1 runner diff artifact is missing or changed")
    _assert_committed_unchanged(study_root, patch_path)
    return {"manifest": manifest, "manifest_bytes": manifest_bytes, "manifest_sha256": sha256_bytes(manifest_bytes),
            "producer_commit": producer, "patch_sha256": patch_sha}


def _parse_diff_names(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split("\t", 1)
        if len(fields) != 2 or fields[0] not in {"A", "M", "D"}:
            raise ResourceAmendmentError(f"unexpected path status in R1 whitelist diff: {line!r}")
        result[fields[1].removeprefix("studies/issue-60-onpolicy-joint-choice/")] = fields[0]
    return result


def _expected_status_reconciliation_paths() -> tuple[str, ...]:
    return tuple(
        f"evidence/runs/training/block1/batch-01/seed-{seed}/attempt-0001/status.json"
        for seed in range(660001, 660047)
    )


def _verify_status_reconciliation_scope(study_root: Path, producer: str) -> dict[str, str]:
    diff = _git(study_root, "diff", "--name-status", "--no-renames",
                R1_STATUS_PARENT_HANDOFF, producer, "--", _study_git_path(study_root, "."))
    observed = _parse_diff_names(diff)
    expected = {path: "M" for path in _expected_status_reconciliation_paths()}
    if observed != expected:
        raise ResourceAmendmentError(
            "post-stop evidence changes must be exactly the 46 finalized training attempt status files"
        )
    return observed


def _verify_whitelist(study_root: Path, amendment: Mapping[str, Any], producer: str,
                      original_runner: bytes) -> dict[str, Any]:
    diff = _git(study_root, "diff", "--name-status", "--no-renames", PARENT_HANDOFF,
                producer, "--", _study_git_path(study_root, "."))
    actual = {path: status for path, status in _parse_diff_names(diff).items()
              if not path.startswith("evidence/")}
    expected = {path: "M" for path in amendment["source_diff_whitelist"]["modified_paths"]}
    expected.update({path: "A" for path in amendment["source_diff_whitelist"]["added_paths"]})
    expected.update({path: "D" for path in amendment["source_diff_whitelist"]["deleted_paths"]})
    if actual != expected:
        raise ResourceAmendmentError(f"R1 source path whitelist mismatch; expected {expected}, observed {actual}")
    _verify_status_reconciliation_scope(study_root, producer)
    amended_runner = (study_root / ORIGINAL_RUNNER).read_bytes()
    verify_runner_whitelist(original_runner, amended_runner)
    # The committed patch is generated by freeze_manifest_r1.py from the same
    # parent/producer pair. Its path is listed in the authorization receipt.
    return {"changed_paths": actual,
            "changed_runner_symbols": list(R1_RUNNER_SYMBOLS),
            "runner_ast_whitelist_passed": True}


def _verify_status_reconciliation(study_root: Path, manifest_info: Mapping[str, Any],
                                  auth_info: Mapping[str, Any], gate_bytes: bytes,
                                  seal_bytes: bytes, amendment: Mapping[str, Any]) -> dict[str, Any]:
    """Verify the post-stop lifecycle correction without rewriting the sealed R1 forecast."""
    path = R1_STATUS_RECONCILIATION
    note_bytes = (study_root / path).read_bytes()
    note = json.loads(note_bytes)
    if note.get("schema_id") != "issue60-attempt-status-reconciliation-r1-v1":
        raise ResourceAmendmentError("attempt status reconciliation schema is unexpected")
    if note.get("parent_handoff_commit") != R1_STATUS_PARENT_HANDOFF:
        raise ResourceAmendmentError("attempt status reconciliation has the wrong pre-run handoff anchor")
    if note.get("current_manifest_sha256") != manifest_info["manifest_sha256"]:
        raise ResourceAmendmentError("attempt status reconciliation does not bind the current R1 manifest")
    if note.get("current_authorization_sha256") != auth_info["receipt_sha256"]:
        raise ResourceAmendmentError("attempt status reconciliation does not bind the current authorization")
    amendment_bytes = (study_root / "resource-amendment-r1.json").read_bytes()
    if note.get("current_amendment_sha256") != sha256_bytes(amendment_bytes):
        raise ResourceAmendmentError("attempt status reconciliation does not bind the current amendment")

    old_manifest_bytes = _committed_bytes(study_root, R1_MANIFEST, R1_STATUS_PARENT_HANDOFF)
    old_auth_bytes = _committed_bytes(study_root, R1_AUTHORIZATION, R1_STATUS_PARENT_HANDOFF)
    old_amendment_bytes = _committed_bytes(study_root, "resource-amendment-r1.json", R1_STATUS_PARENT_HANDOFF)
    old_gate_bytes = _committed_bytes(study_root, R1_GATE, R1_STATUS_PARENT_HANDOFF)
    old_seal_bytes = _committed_bytes(study_root, R1_SEAL, R1_STATUS_PARENT_HANDOFF)
    for key, value in (
        ("previous_manifest_sha256", sha256_bytes(old_manifest_bytes)),
        ("previous_authorization_sha256", sha256_bytes(old_auth_bytes)),
        ("previous_amendment_sha256", sha256_bytes(old_amendment_bytes)),
        ("sealed_resource_gate_sha256", sha256_bytes(old_gate_bytes)),
        ("sealed_resource_seal_sha256", sha256_bytes(old_seal_bytes)),
    ):
        if note.get(key) != value:
            raise ResourceAmendmentError(f"attempt status reconciliation does not preserve {key}")
    if gate_bytes != old_gate_bytes or seal_bytes != old_seal_bytes:
        raise ResourceAmendmentError("attempt status reconciliation must preserve the original sealed forecast bytes")
    old_gate = json.loads(old_gate_bytes)
    old_seal = json.loads(old_seal_bytes)
    old_amendment = json.loads(old_amendment_bytes)
    if (old_gate.get("resource_amendment_sha256") != sha256_bytes(old_amendment_bytes)
            or old_gate.get("r1_manifest_sha256") != sha256_bytes(old_manifest_bytes)
            or old_gate.get("authorization_receipt_sha256") != sha256_bytes(old_auth_bytes)
            or old_seal.get("resource_gate_sha256") != sha256_bytes(old_gate_bytes)
            or old_seal.get("frozen_manifest_sha256") != sha256_bytes(old_manifest_bytes)
            or old_seal.get("authorization_receipt_sha256") != sha256_bytes(old_auth_bytes)):
        raise ResourceAmendmentError("pre-run R1 forecast/seal is internally inconsistent at its handoff anchor")
    if old_amendment.get("original_cpu_ledger_prefix_sha256") != amendment.get("original_cpu_ledger_prefix_sha256"):
        raise ResourceAmendmentError("attempt status reconciliation changed the sealed CPU ledger prefix")

    attempts = note.get("attempts")
    expected_paths = _expected_status_reconciliation_paths()
    if (not isinstance(attempts, list) or any(not isinstance(row, Mapping) for row in attempts)
            or [row.get("status_path") for row in attempts] != list(expected_paths)):
        raise ResourceAmendmentError("attempt status reconciliation must enumerate the exact 46 stopped training attempts")
    from attempt_lifecycle import finalized_attempt_status
    for row, status_relative in zip(attempts, expected_paths):
        run_relative = status_relative.removesuffix("status.json") + "run.json"
        old_status_bytes = _committed_bytes(study_root, status_relative, R1_STATUS_PARENT_HANDOFF)
        old_run_bytes = _committed_bytes(study_root, run_relative, R1_STATUS_PARENT_HANDOFF)
        current_status_bytes = (study_root / status_relative).read_bytes()
        current_run_bytes = (study_root / run_relative).read_bytes()
        if (row.get("previous_status_sha256") != sha256_bytes(old_status_bytes)
                or row.get("run_summary_sha256") != sha256_bytes(old_run_bytes)
                or row.get("terminal_status_sha256") != sha256_bytes(current_status_bytes)
                or row.get("run_path") != run_relative):
            raise ResourceAmendmentError(f"attempt status reconciliation hash mismatch: {status_relative}")
        if current_run_bytes != old_run_bytes:
            raise ResourceAmendmentError(f"attempt status reconciliation changed a scientific run summary: {run_relative}")
        started = json.loads(old_status_bytes)
        summary = json.loads(current_run_bytes)
        terminal = json.loads(current_status_bytes)
        if started.get("status") != "RUNNING" or terminal != finalized_attempt_status(started, summary):
            raise ResourceAmendmentError(f"attempt status does not match its terminal run summary: {status_relative}")
    if len(attempts) != len(expected_paths):
        raise ResourceAmendmentError("attempt status reconciliation has extra or missing attempt rows")
    _assert_committed_unchanged(study_root, path)
    return {"schema_id": note["schema_id"], "attempt_count": len(attempts),
            "status_reconciliation_sha256": sha256_bytes(note_bytes)}


def write_attempt_status_reconciliation(study_root: Path) -> dict[str, Any]:
    """Record the narrow post-stop correction while preserving the sealed forecast."""
    study_root = study_root.resolve()
    old_manifest = _committed_bytes(study_root, R1_MANIFEST, R1_STATUS_PARENT_HANDOFF)
    old_auth = _committed_bytes(study_root, R1_AUTHORIZATION, R1_STATUS_PARENT_HANDOFF)
    old_amendment = _committed_bytes(study_root, "resource-amendment-r1.json", R1_STATUS_PARENT_HANDOFF)
    old_gate = _committed_bytes(study_root, R1_GATE, R1_STATUS_PARENT_HANDOFF)
    old_seal = _committed_bytes(study_root, R1_SEAL, R1_STATUS_PARENT_HANDOFF)
    if ((study_root / R1_GATE).read_bytes() != old_gate
            or (study_root / R1_SEAL).read_bytes() != old_seal):
        raise ResourceAmendmentError("post-stop status repair may not rewrite the resource gate or seal")
    attempts = []
    from attempt_lifecycle import finalized_attempt_status
    for status_relative in _expected_status_reconciliation_paths():
        run_relative = status_relative.removesuffix("status.json") + "run.json"
        old_status = _committed_bytes(study_root, status_relative, R1_STATUS_PARENT_HANDOFF)
        old_run = _committed_bytes(study_root, run_relative, R1_STATUS_PARENT_HANDOFF)
        status_path = study_root / status_relative
        run_path = study_root / run_relative
        current_status = status_path.read_bytes()
        current_run = run_path.read_bytes()
        started = json.loads(old_status)
        summary = json.loads(current_run)
        terminal = json.loads(current_status)
        if started.get("status") != "RUNNING" or old_run != current_run:
            raise ResourceAmendmentError(f"post-stop repair changed the run summary or lacks a RUNNING marker: {run_relative}")
        if terminal != finalized_attempt_status(started, summary):
            raise ResourceAmendmentError(f"post-stop status is not the terminal summary state: {status_relative}")
        attempts.append({
            "status_path": status_relative,
            "run_path": run_relative,
            "previous_status_sha256": sha256_bytes(old_status),
            "run_summary_sha256": sha256_bytes(old_run),
            "terminal_status_sha256": sha256_bytes(current_status),
        })
    record = {
        "schema_id": "issue60-attempt-status-reconciliation-r1-v1",
        "parent_handoff_commit": R1_STATUS_PARENT_HANDOFF,
        "current_manifest_sha256": sha256_bytes((study_root / R1_MANIFEST).read_bytes()),
        "current_authorization_sha256": sha256_bytes((study_root / R1_AUTHORIZATION).read_bytes()),
        "current_amendment_sha256": sha256_bytes((study_root / "resource-amendment-r1.json").read_bytes()),
        "previous_manifest_sha256": sha256_bytes(old_manifest),
        "previous_authorization_sha256": sha256_bytes(old_auth),
        "previous_amendment_sha256": sha256_bytes(old_amendment),
        "sealed_resource_gate_sha256": sha256_bytes(old_gate),
        "sealed_resource_seal_sha256": sha256_bytes(old_seal),
        "attempts": attempts,
    }
    current_manifest = _read_json(study_root / R1_MANIFEST)
    _verify_status_reconciliation_scope(study_root, str(current_manifest.get("implementation_commit", "")))
    _atomic_json(study_root / R1_STATUS_RECONCILIATION, record)
    return record


def _verify_authorization_receipt(study_root: Path, amendment: Mapping[str, Any],
                                  manifest_info: Mapping[str, Any],
                                  original_runner: bytes) -> dict[str, Any]:
    receipt_path = study_root / R1_AUTHORIZATION
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    if receipt.get("schema_id") != "issue60-resource-authorization-r1-v1":
        raise ResourceAmendmentError("R1 resource authorization receipt has an unexpected schema")
    if receipt.get("parent_handoff_commit") != PARENT_HANDOFF:
        raise ResourceAmendmentError("R1 receipt parent does not match the reviewed handoff")
    if receipt.get("producer_commit") != manifest_info["producer_commit"]:
        raise ResourceAmendmentError("R1 receipt producer does not match the frozen producer")
    if receipt.get("frozen_manifest_sha256") != manifest_info["manifest_sha256"]:
        raise ResourceAmendmentError("R1 receipt does not bind the R1 source manifest")
    if receipt.get("amendment_config_sha256") != sha256_bytes((study_root / "resource-amendment-r1.json").read_bytes()):
        raise ResourceAmendmentError("R1 receipt does not bind the resource amendment")
    if receipt.get("runner_diff_sha256") != manifest_info["patch_sha256"]:
        raise ResourceAmendmentError("R1 receipt does not bind the reviewed runner diff")
    if receipt.get("original_config_sha256") != amendment["original_config_sha256"]:
        raise ResourceAmendmentError("R1 receipt has a mismatched original config hash")
    if receipt.get("original_manifest_sha256") != amendment["original_manifest_sha256"]:
        raise ResourceAmendmentError("R1 receipt has a mismatched original manifest hash")
    if receipt.get("original_preflight_gate_sha256") != amendment["original_preflight_gate_sha256"]:
        raise ResourceAmendmentError("R1 receipt has a mismatched original blocked gate hash")
    if receipt.get("resource") != amendment["resource"]:
        raise ResourceAmendmentError("R1 receipt resource values differ from the Planner-amended values")
    if receipt.get("scientific_design_changes") != []:
        raise ResourceAmendmentError("R1 receipt authorizes a scientific design change")
    review = receipt.get("review_reserve_accounting", {})
    if review != {
        "total_review_reserve_cpu_seconds": 20000.0,
        "prior_reviewer_charge_or_conservative_allowance_cpu_seconds": 1000.0,
        "prior_reviewer_allowance_basis": "conservative allowance pending exact independent-review process CPU; charged within the review reserve, not the Builder allowance",
        "remaining_future_reviewer_reserve_cpu_seconds": 19000.0,
    }:
        raise ResourceAmendmentError("R1 receipt does not preserve the full Reviewer reserve")
    if receipt.get("source_diff_whitelist") != amendment.get("source_diff_whitelist"):
        raise ResourceAmendmentError("R1 receipt path/symbol whitelist differs from the frozen amendment")
    whitelist_result = _verify_whitelist(study_root, amendment, str(manifest_info["producer_commit"]), original_runner)
    committed = _committed_bytes(study_root, R1_AUTHORIZATION)
    if committed != receipt_bytes:
        raise ResourceAmendmentError("R1 authorization receipt has uncommitted or changed bytes")
    return {"receipt": receipt, "receipt_sha256": sha256_bytes(receipt_bytes),
            "whitelist": whitelist_result}


def _verify_gate_and_seal(study_root: Path, amendment: Mapping[str, Any],
                          manifest_info: Mapping[str, Any], auth_info: Mapping[str, Any]) -> dict[str, Any]:
    gate_path = study_root / R1_GATE
    seal_path = study_root / R1_SEAL
    gate_bytes = gate_path.read_bytes()
    seal_bytes = seal_path.read_bytes()
    gate = json.loads(gate_bytes)
    seal = json.loads(seal_bytes)
    if gate.get("schema_id") != "issue60-resource-gate-r1-v1" or gate.get("status") != "PASS":
        raise ResourceAmendmentError("the new R1 cost forecast is not a passing resource gate")
    if seal.get("schema_id") != "issue60-resource-gate-seal-r1-v1":
        raise ResourceAmendmentError("R1 resource gate seal has an unexpected schema")
    if seal.get("resource_gate_sha256") != sha256_bytes(gate_bytes):
        raise ResourceAmendmentError("R1 gate seal does not match the passing resource gate")
    current_amendment_sha = sha256_bytes((study_root / "resource-amendment-r1.json").read_bytes())
    bindings_match = (
        seal.get("authorization_receipt_sha256") == auth_info["receipt_sha256"]
        and seal.get("frozen_manifest_sha256") == manifest_info["manifest_sha256"]
        and gate.get("resource_amendment_sha256") == current_amendment_sha
    )
    reconciliation = None
    if not bindings_match:
        reconciliation = _verify_status_reconciliation(
            study_root, manifest_info, auth_info, gate_bytes, seal_bytes, amendment
        )
    synthetic_path = study_root / R1_SYNTHETIC
    synthetic = json.loads(synthetic_path.read_text(encoding="utf-8"))
    if (synthetic.get("passed") is not True
            or gate.get("synthetic_public_witness_sha256") != sha256_bytes(synthetic_path.read_bytes())):
        raise ResourceAmendmentError("R1 gate does not bind its passing synthetic/public-context witness")
    replay = gate.get("retained_output_replay", {})
    if (replay.get("passed") is not True or replay.get("games_checked") != 8
            or replay.get("supported_decisions_checked", 0) <= 0
            or not isinstance(replay.get("output_fingerprint_sha256"), str)):
        raise ResourceAmendmentError("R1 gate does not bind retained actor/critic/scorer outputs")
    if gate.get("original_blocked_gate_sha256") != amendment["original_preflight_gate_sha256"]:
        raise ResourceAmendmentError("R1 gate does not preserve the original blocked gate identity")
    accounting = gate.get("accounting")
    if not isinstance(accounting, Mapping):
        raise ResourceAmendmentError("R1 gate lacks its detailed resource arithmetic")
    if accounting.get("builder_budget_seconds") != R1_RESOURCE_VALUES["builder_allowance_seconds"]:
        raise ResourceAmendmentError("R1 gate has the wrong Builder allowance")
    if accounting.get("all_in_projected_cpu_seconds", math.inf) > R1_RESOURCE_VALUES["process_cpu_cap_seconds"]:
        raise ResourceAmendmentError("R1 aggregate forecast exceeds the amended cumulative cap")
    if accounting.get("projected_builder_cpu_seconds", math.inf) > R1_RESOURCE_VALUES["builder_allowance_seconds"]:
        raise ResourceAmendmentError("R1 Builder forecast exceeds its amended allowance")
    if seal.get("reviewer_reserved_cpu_seconds") != 20000.0 or seal.get("prior_reviewer_allowance_cpu_seconds") != 1000.0:
        raise ResourceAmendmentError("R1 review reserve accounting is inconsistent")
    old_gate = json.loads((study_root / ORIGINAL_GATE).read_text(encoding="utf-8"))
    recomputed = assert_resource_projection(
        old_gate,
        float(accounting.get("preflight_and_implementation_accounted_cpu_seconds", -1)),
        prior_reviewer_allowance=float(amendment["resource"]["prior_reviewer_allowance_seconds"]),
    )
    for key, expected in recomputed.items():
        observed = accounting.get(key)
        if not isinstance(observed, (int, float)) or not math.isclose(float(observed), expected, rel_tol=0.0, abs_tol=1e-8):
            raise ResourceAmendmentError(f"R1 forecast arithmetic does not verify: {key}")
    if (seal.get("process_cpu_cap_seconds") != recomputed["process_cpu_cap_seconds"]
            or seal.get("builder_allowance_seconds") != recomputed["builder_budget_seconds"]
            or seal.get("future_reviewer_reserve_cpu_seconds") != recomputed["future_reviewer_reserve_cpu_seconds"]):
        raise ResourceAmendmentError("R1 gate seal resource bounds disagree with the recomputed forecast")
    _assert_committed_unchanged(study_root, R1_GATE)
    _assert_committed_unchanged(study_root, R1_SEAL)
    prefix_count = seal.get("cpu_ledger_prefix_line_count")
    prefix_hash = seal.get("cpu_ledger_prefix_sha256")
    _, lines = _ledger_prefix((study_root / CPU_LEDGER).read_bytes(), int(prefix_count))
    if sha256_bytes(b"".join(lines[:int(prefix_count)])) != prefix_hash:
        raise ResourceAmendmentError("CPU ledger differs within its sealed pre-training prefix")
    _assert_committed_unchanged(study_root, R1_AUTHORIZATION)
    return {"gate": gate, "gate_sha256": sha256_bytes(gate_bytes), "seal": seal,
            "seal_sha256": sha256_bytes(seal_bytes),
            "status_reconciliation": reconciliation}


def verify_resource_chain(study_root: Path, *, require_no_formal_runs: bool = False,
                          require_gate_seal: bool = False) -> dict[str, Any]:
    """Verify both original evidence and the prospective, resource-only R1 chain."""
    study_root = study_root.resolve()
    amendment = _amendment(study_root)
    if amendment.get("schema_id") != "issue60-resource-amendment-r1-v1":
        raise ResourceAmendmentError("R1 amendment config schema is unexpected")
    if amendment.get("issue") != ISSUE or amendment.get("amendment_id") != "R1":
        raise ResourceAmendmentError("R1 amendment config identifies a different task")
    if amendment.get("resource") != R1_RESOURCE_VALUES:
        raise ResourceAmendmentError("R1 amendment resource values do not match the Planner decision")
    if amendment.get("parent_handoff_commit") != PARENT_HANDOFF or amendment.get("original_producer_commit") != ORIGINAL_PRODUCER:
        raise ResourceAmendmentError("R1 amendment source chain does not start at the reviewed handoff")
    original = _verify_original_evidence(study_root, amendment)
    manifest_info = _verify_source_manifest(study_root, amendment)
    auth_info = _verify_authorization_receipt(study_root, amendment, manifest_info, original["original_runner"])
    if require_no_formal_runs:
        _verify_formal_runs_absent(study_root)
    seal_info = None
    if require_gate_seal:
        seal_info = _verify_gate_and_seal(study_root, amendment, manifest_info, auth_info)
    return {
        "status": "PASS", "amendment_id": "R1",
        "original_blocked_gate_sha256": amendment["original_preflight_gate_sha256"],
        "original_config_sha256": amendment["original_config_sha256"],
        "producer_commit": manifest_info["producer_commit"],
        "frozen_manifest_sha256": manifest_info["manifest_sha256"],
        "authorization_receipt_sha256": auth_info["receipt_sha256"],
        "source_change_whitelist": auth_info["whitelist"],
        "gate_seal": seal_info,
    }


def _assert_allowance_ledger(study_root: Path, amendment: Mapping[str, Any], *, append: bool) -> None:
    path = study_root / CPU_LEDGER
    raw = path.read_bytes()
    _, lines = _ledger_prefix(raw, int(amendment["original_cpu_ledger_line_count"]))
    rows = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ResourceAmendmentError("CPU ledger contains invalid JSON") from exc
    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        charge_id = row.get("charge_id")
        if isinstance(charge_id, str):
            by_id.setdefault(charge_id, []).append(row)
    for charge_id, role, amount in R1_ALLOWANCES:
        existing = by_id.get(charge_id, [])
        if existing:
            if (len(existing) != 1 or existing[0].get("process_cpu_seconds") != amount
                    or existing[0].get("charged_role") != role
                    or existing[0].get("accounting_kind") != "authorized_conservative_cpu_allowance"
                    or existing[0].get("actual_process_cpu_seconds") != 0.0):
                raise ResourceAmendmentError(f"R1 allowance ledger row is duplicated or changed: {charge_id}")
            continue
        if not append:
            raise ResourceAmendmentError(f"R1 allowance has not been charged to the CPU ledger: {charge_id}")
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({
                "schema_id": "issue60-cpu-allowance-r1-v1",
                "stage": "resource-amendment-r1-administration",
                "charge_id": charge_id, "charged_role": role,
                "accounting_kind": "authorized_conservative_cpu_allowance",
                "actual_process_cpu_seconds": 0.0,
                "process_cpu_seconds": amount,
                "reason": "Planner-authorized bounded resource-only continuation",
                "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        rows.append({"charge_id": charge_id, "charged_role": role, "process_cpu_seconds": amount})
        by_id[charge_id] = [rows[-1]]


def _replay_retained_outputs(study_root: Path) -> dict[str, Any]:
    """Recompute scorer, residual actor and critic outputs on all retained decisions."""
    import torch
    from controllers import load_bootstrap, p0_distribution, policy_distribution, sample_index
    from encoder import Vocabulary, encode
    from model import JointChoiceActorCritic
    from trainer import derive_seed

    config = json.loads((study_root / "config.json").read_text(encoding="utf-8"))
    vocabulary = Vocabulary(study_root / "vocabulary.json")
    bootstrap, _ = load_bootstrap(study_root / config["bootstrap"]["checkpoint"])
    decisions_checked = 0
    game_summaries = []
    output_fingerprint = hashlib.sha256()
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    for index, seed in enumerate(range(668001, 668009)):
        block = "block1" if index % 2 == 0 else "block2"
        model_seed = 660101 if block == "block1" else 660102
        torch.manual_seed(model_seed)
        model = JointChoiceActorCritic(vocabulary)
        model.eval()
        attempt_root = study_root / "evidence" / "runs" / "preflight" / block / f"seed-{seed}" / "attempt-0001"
        summary = _read_json(attempt_root / "run.json")
        expected_model_sha = summary.get("model_sha256")
        from run_study import model_fingerprint
        if model_fingerprint(model, vocabulary, ORIGINAL_CONFIG_SHA256) != expected_model_sha:
            raise ResourceAmendmentError(f"retained preflight model identity changed for seed {seed}")
        rng_seed = derive_seed("preflight-policy", block, model_seed, seed)
        rng = random.Random(rng_seed)
        with gzip.open(attempt_root / "decisions.jsonl.gz", "rt", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                event = json.loads(line)
                serialized = event.get("issue60_public_input")
                count = event.get("issue60_choice_count")
                if not isinstance(serialized, str) or not isinstance(count, int):
                    raise ResourceAmendmentError(f"retained public decision is malformed at seed {seed}, line {line_number}")
                if event.get("issue60_mode") != "residual":
                    raise ResourceAmendmentError("retained preflight contains a non-residual policy decision")
                if sha256_bytes(serialized.encode("utf-8")) != event.get("issue60_public_input_sha256"):
                    raise ResourceAmendmentError("retained public input bytes do not match their recorded hash")
                bootstrap_index, base_probabilities = p0_distribution(bootstrap, serialized, count)
                observation = encode(serialized, vocabulary)
                with torch.no_grad():
                    logits, value = model(observation)
                scores = [float(value) for value in logits.detach().cpu().tolist()]
                value_prediction = float(value.detach().cpu())
                probabilities = policy_distribution(base_probabilities, scores)
                selected = sample_index(probabilities, rng)
                def close_vector(actual: Any, expected: list[float], field: str) -> None:
                    if (not isinstance(actual, list) or len(actual) != len(expected)
                            or any(not math.isclose(float(a), b, rel_tol=0.0, abs_tol=1e-8)
                                   for a, b in zip(actual, expected))):
                        raise ResourceAmendmentError(f"retained {field} differs on seed {seed}, line {line_number}")
                close_vector(event.get("issue60_base_probabilities"), base_probabilities, "bootstrap scorer output")
                close_vector(event.get("issue60_residual_scores"), scores, "actor output")
                close_vector(event.get("issue60_behavior_probabilities"), probabilities, "behavior output")
                recorded_value = event.get("issue60_value_prediction")
                if not isinstance(recorded_value, (int, float)) or not math.isclose(float(recorded_value), value_prediction, rel_tol=0.0, abs_tol=1e-8):
                    raise ResourceAmendmentError(f"retained critic output differs on seed {seed}, line {line_number}")
                if event.get("issue60_bootstrap_index") != bootstrap_index or event.get("issue60_selected_index") != selected:
                    raise ResourceAmendmentError(f"retained policy selection replay differs on seed {seed}, line {line_number}")
                if not math.isclose(float(event.get("issue60_old_log_probability")), math.log(probabilities[selected]), rel_tol=0.0, abs_tol=1e-10):
                    raise ResourceAmendmentError(f"retained behavior log probability differs on seed {seed}, line {line_number}")
                replay_row = {
                    "game_seed": seed, "line": line_number,
                    "bootstrap_index": bootstrap_index,
                    "base_probabilities": base_probabilities,
                    "residual_scores": scores,
                    "value_prediction": value_prediction,
                    "behavior_probabilities": probabilities,
                    "selected_index": selected,
                }
                output_fingerprint.update(_json_bytes(replay_row))
                decisions_checked += 1
        game_summaries.append({"game_seed": seed, "model_sha256": expected_model_sha,
                               "runtime_policy_seed": rng_seed})
    return {"passed": True, "games_checked": len(game_summaries),
            "supported_decisions_checked": decisions_checked,
            "output_fingerprint_sha256": output_fingerprint.hexdigest(),
            "games": game_summaries}


def assert_resource_projection(old_gate: Mapping[str, Any], accounted_cpu: float,
                               *, prior_reviewer_allowance: float = 1000.0) -> dict[str, float]:
    max_game = float(old_gate["max_observed_game_cpu_seconds"])
    optimizer = old_gate["optimizer_projection"]
    minibatch_cpu = float(optimizer["measured_full_256_row_minibatch_cpu_seconds"])
    minibatches = int(optimizer["projected_optimizer_minibatches"])
    if minibatches != R1_RESOURCE_VALUES["remaining_optimizer_minibatches"]:
        raise ResourceAmendmentError("optimizer projection no longer matches the original fixed grid")
    game_projection = max_game * R1_RESOURCE_VALUES["remaining_games"] * R1_RESOURCE_VALUES["game_cost_multiplier"]
    optimizer_projection = minibatches * minibatch_cpu * R1_RESOURCE_VALUES["optimizer_cost_multiplier"]
    remaining_with_headroom = (game_projection + optimizer_projection) * (1 + R1_RESOURCE_VALUES["forecast_headroom_fraction"])
    if not math.isclose(
        remaining_with_headroom,
        float(old_gate["projected_remaining_cpu_seconds_with_headroom"]),
        rel_tol=0.0, abs_tol=0.01,
    ):
        raise ResourceAmendmentError("R1 arithmetic does not reproduce the accepted original fixed-grid forecast")
    builder_projection = accounted_cpu + remaining_with_headroom
    review_reserve = R1_RESOURCE_VALUES["reviewer_reserve_seconds"]
    future_review = review_reserve - prior_reviewer_allowance
    all_in_projection = builder_projection + review_reserve
    if prior_reviewer_allowance < 0 or future_review < 0:
        raise ResourceAmendmentError("prior Reviewer allowance exceeds the reserved review envelope")
    return {
        "max_observed_game_cpu_seconds": max_game,
        "projected_remaining_game_cpu_seconds_before_headroom": game_projection,
        "projected_optimizer_cpu_seconds_before_headroom": optimizer_projection,
        "projected_remaining_cpu_seconds_with_headroom": remaining_with_headroom,
        "preflight_and_implementation_accounted_cpu_seconds": accounted_cpu,
        "projected_builder_cpu_seconds": builder_projection,
        "builder_budget_seconds": R1_RESOURCE_VALUES["builder_allowance_seconds"],
        "reviewer_reserved_cpu_seconds": review_reserve,
        "prior_reviewer_allowance_cpu_seconds": prior_reviewer_allowance,
        "future_reviewer_reserve_cpu_seconds": future_review,
        "all_in_projected_cpu_seconds": all_in_projection,
        "process_cpu_cap_seconds": R1_RESOURCE_VALUES["process_cpu_cap_seconds"],
    }


def _committed_receipt(study_root: Path) -> dict[str, Any]:
    amendment = _amendment(study_root)
    original = _verify_original_evidence(study_root, amendment)
    manifest_info = _verify_source_manifest(study_root, amendment)
    auth_info = _verify_authorization_receipt(study_root, amendment, manifest_info, original["original_runner"])
    return {"amendment": amendment, "original": original,
            "manifest_info": manifest_info, "auth_info": auth_info}


def run_resource_stage(study_root: Path, stage_started_cpu: float) -> dict[str, Any]:
    """Run cheap carry-over/public checks and produce a new, separate R1 gate."""
    chain = verify_resource_chain(study_root, require_no_formal_runs=True)
    data = _committed_receipt(study_root)
    amendment = data["amendment"]
    old_gate = data["original"]["old_gate"]
    _verify_formal_runs_absent(study_root)
    preflight = _verify_original_preflight(study_root, old_gate)
    _assert_allowance_ledger(study_root, amendment, append=True)
    config = json.loads((study_root / "config.json").read_text(encoding="utf-8"))
    from run_study import verify_runtime
    runtime = verify_runtime(config)
    from preflight_witness import run_witnesses
    synthetic = run_witnesses(study_root, study_root / R1_SYNTHETIC)
    if not synthetic.get("passed"):
        raise ResourceAmendmentError("R1 synthetic/public-context witness did not pass")
    replay = _replay_retained_outputs(study_root)
    stage_cpu = time.process_time() - stage_started_cpu
    from run_study import BUILD_AND_STATIC_ALLOWANCE, _accounted_cpu
    accounted = _accounted_cpu(stage_cpu + R1_STAGE_FINALIZATION_MARGIN_SECONDS)
    projection = assert_resource_projection(
        old_gate, accounted,
        prior_reviewer_allowance=float(amendment["resource"]["prior_reviewer_allowance_seconds"]),
    )
    if projection["builder_budget_seconds"] != 180000.0:
        raise ResourceAmendmentError("resource projection uses the wrong Builder allowance")
    status = "PASS" if (projection["projected_builder_cpu_seconds"] <= projection["builder_budget_seconds"]
                        and projection["all_in_projected_cpu_seconds"] <= projection["process_cpu_cap_seconds"]
                        and projection["future_reviewer_reserve_cpu_seconds"] == 19000.0) else "RESOURCE_BLOCKED"
    result = {
        "schema_id": "issue60-resource-gate-r1-v1", "status": status,
        "amendment_id": "R1", "issue": 60,
        "parent_handoff_commit": PARENT_HANDOFF,
        "original_producer_commit": ORIGINAL_PRODUCER,
        "r1_producer_commit": data["manifest_info"]["producer_commit"],
        "original_config_sha256": amendment["original_config_sha256"],
        "resource_amendment_sha256": sha256_bytes((study_root / "resource-amendment-r1.json").read_bytes()),
        "original_manifest_sha256": amendment["original_manifest_sha256"],
        "original_blocked_gate_sha256": amendment["original_preflight_gate_sha256"],
        "r1_manifest_sha256": data["manifest_info"]["manifest_sha256"],
        "authorization_receipt_sha256": data["auth_info"]["receipt_sha256"],
        "original_cpu_ledger_prefix_sha256": amendment["original_cpu_ledger_prefix_sha256"],
        "r1_allowance_charges": [{"charge_id": row[0], "role": row[1], "cpu_seconds": row[2]} for row in R1_ALLOWANCES],
        "r1_source_change_whitelist": chain["source_change_whitelist"],
        "original_preflight_runs": preflight,
        "runtime_pins": runtime,
        "synthetic_public_witness": synthetic,
        "synthetic_public_witness_sha256": sha256_bytes((study_root / R1_SYNTHETIC).read_bytes()),
        "retained_output_replay": replay,
        "accounting": {
            **projection,
            "builder_static_allowance_cpu_seconds": BUILD_AND_STATIC_ALLOWANCE,
            "original_gate_vs_ledger_snapshot_difference_seconds": data["original"]["original_accounting_reconciliation_difference_seconds"],
            "stage_current_cpu_seconds": stage_cpu,
            "stage_finalization_margin_cpu_seconds": R1_STAGE_FINALIZATION_MARGIN_SECONDS,
            "accounted_cpu_ledger_line_count_before_gate": len((study_root / CPU_LEDGER).read_bytes().splitlines()),
        },
        "decision": "continue unchanged fixed grid" if status == "PASS" else "stop before formal training/evaluation",
    }
    path = study_root / R1_GATE
    _atomic_json(path, result)
    if status != "PASS":
        raise ResourceAmendmentError("RESOURCE_BLOCKED: amended fixed-grid projection does not fit the authorized cap")
    return result


def seal_resource_gate(study_root: Path, stage_started_cpu: float) -> dict[str, Any]:
    """Seal a committed R1 forecast and the pre-training CPU history prefix."""
    verify_resource_chain(study_root, require_no_formal_runs=True)
    data = _committed_receipt(study_root)
    amendment = data["amendment"]
    gate_path = study_root / R1_GATE
    gate_bytes = gate_path.read_bytes()
    _assert_committed_unchanged(study_root, R1_GATE)
    _assert_committed_unchanged(study_root, R1_SYNTHETIC)
    gate = json.loads(gate_bytes)
    if gate.get("schema_id") != "issue60-resource-gate-r1-v1" or gate.get("status") != "PASS":
        raise ResourceAmendmentError("only a passing R1 resource gate can be sealed")
    _assert_allowance_ledger(study_root, amendment, append=False)
    ledger_bytes = (study_root / CPU_LEDGER).read_bytes()
    lines = ledger_bytes.splitlines(keepends=True)
    result = {
        "schema_id": "issue60-resource-gate-seal-r1-v1",
        "status": "PASS", "amendment_id": "R1",
        "parent_handoff_commit": PARENT_HANDOFF,
        "r1_producer_commit": data["manifest_info"]["producer_commit"],
        "frozen_manifest_sha256": data["manifest_info"]["manifest_sha256"],
        "authorization_receipt_sha256": data["auth_info"]["receipt_sha256"],
        "resource_gate_sha256": sha256_bytes(gate_bytes),
        "reviewer_reserved_cpu_seconds": 20000.0,
        "prior_reviewer_allowance_cpu_seconds": 1000.0,
        "future_reviewer_reserve_cpu_seconds": 19000.0,
        "process_cpu_cap_seconds": 200000.0,
        "builder_allowance_seconds": 180000.0,
        "cpu_ledger_prefix_line_count": len(lines),
        "cpu_ledger_prefix_sha256": sha256_bytes(ledger_bytes),
        "cpu_ledger_prefix_bytes": len(ledger_bytes),
        "formal_training_or_evaluation_started": False,
        "seal_stage_cpu_at_write_seconds": time.process_time() - stage_started_cpu,
        "seal_margin_cpu_seconds": 10.0,
    }
    _atomic_json(study_root / R1_SEAL, result)
    return result


def require_training_gate(study_root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed unless R1 is committed, verified, sealed and forecast PASS."""
    verify_resource_chain(study_root, require_gate_seal=True)
    _check_config_identity((study_root / "config.json").read_bytes())
    if config.get("base", {}).get("commit") != "9525d416b9488bb2455435699893b03fe19fe30a":
        raise ResourceAmendmentError("training config base changed after the original registration")
    gate = json.loads((study_root / R1_GATE).read_text(encoding="utf-8"))
    if gate.get("status") != "PASS":
        raise ResourceAmendmentError("R1 resource gate does not authorize the fixed grid")
    return gate
