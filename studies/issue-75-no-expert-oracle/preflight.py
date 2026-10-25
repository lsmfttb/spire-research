from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STUDY = Path(__file__).resolve().parent
CONFIG_PATH = STUDY / "config.json"
SELECTION_PATH = STUDY / "issue74-root-selection-lock.json"
ROOTS = ROOT / "studies" / "issue-73-privileged-noncombat-oracle-stage0" / "receipts" / "stage0" / "roots"
OWNER_RUNS = ROOT / "studies" / "issue-73-privileged-noncombat-oracle-stage0" / "receipts" / "stage0" / "debug-owner-runs"
ISSUE73_LOCK = ROOT / "studies" / "issue-73-privileged-noncombat-oracle-stage0" / "receipts" / "gate-a-requalification-v5" / "source-lock.json"
ISSUE73_PI_INIT = ROOT / "studies" / "issue-73-privileged-noncombat-oracle-stage0" / "receipts" / "stage0" / "policies" / "pi_init.pt"
LOCKED_FILES = (
    "config.json",
    "issue74-root-selection-lock.json",
    "root-provenance.json",
    "algorithm.py",
    "model.py",
    "learning.py",
    "runtime.py",
    "stage0.py",
    "preflight.py",
    "native_negative.py",
    "native-negative-test.json",
    "test_issue75_contract.py",
)
ISSUE74_SELECTION_PATH = "studies/issue-74-oracle-feasibility/selection-lock.json"
ISSUE73_ROOT_LOCK_PATH = "studies/issue-73-privileged-noncombat-oracle-stage0/receipts/gate-a-requalification-v5/source-lock.json"
ISSUE73_ROOT_TREE_PATH = "studies/issue-73-privileged-noncombat-oracle-stage0/receipts/stage0/roots"


def normalized_sha256(path: Path) -> str:
    """Hash text as UTF-8/LF without decoding or retaining native receipt contents."""
    digest = hashlib.sha256()
    pending_cr = False
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if pending_cr:
                chunk = b"\r" + chunk
                pending_cr = False
            if chunk.endswith(b"\r"):
                chunk = chunk[:-1]
                pending_cr = True
            chunk = chunk.replace(b"\r\n", b"\n")
            digest.update(chunk)
    if pending_cr:
        digest.update(b"\r")
    return digest.hexdigest()


def binary_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

def git(*args: str, cwd: Path = ROOT) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def git_blob_sha256(commit: str, relative_path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{commit}:{relative_path}"], cwd=ROOT, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return hashlib.sha256(result.stdout.replace(b"\r\n", b"\n")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object: {path.name}")
    return value


def identity_row(row: dict[str, Any]) -> dict[str, Any]:
    """Only static identities and hashes enter this receipt; no root payload is read."""
    fingerprints = row.get("root_fingerprints")
    if not isinstance(fingerprints, dict):
        raise ValueError("locked root fingerprints are missing")
    allowed_fingerprints = {
        key: str(fingerprints[key])
        for key in (
            "native_state_sha256",
            "public_projection_sha256",
            "public_resource_state_sha256",
        )
    }
    return {
        "owner_seed": int(row["owner_seed"]),
        "root_id": str(row["root_id"]),
        "selection_rank_within_owner": int(row["selection_rank_within_owner"]),
        "owner_decision_index": int(row["owner_decision_index"]),
        "policy_decision_index": int(row["policy_decision_index"]),
        "screen": str(row["screen"]),
        "legal_occurrence_count": int(row["legal_occurrence_count"]),
        "source_file_sha256_lf": str(row["source_file_sha256"]).lower(),
        "legal_action_identities_sha256": str(row["legal_action_identities_sha256"]).lower(),
        "feature_matrix_sha256": str(row["feature_matrix_sha256"]).lower(),
        "projection_digest": str(row["projection_digest"]).lower(),
        "selection_sha256": str(row["selection_sha256"]).lower(),
        "root_fingerprints": allowed_fingerprints,
    }


def verify_lineage(config: dict[str, Any]) -> dict[str, Any]:
    source = config["source"]
    issue73_commit = str(source["spire_reviewed_commit"])
    if git("rev-parse", issue73_commit) != issue73_commit:
        raise ValueError("reviewed Issue 73 source commit is unavailable")
    if git("rev-parse", f"{issue73_commit}:{ISSUE73_ROOT_TREE_PATH}") != source["issue73_root_tree_git_sha1"]:
        raise ValueError("Issue 73 root tree does not match the registered Git tree SHA")
    root_lock_sha = normalized_sha256(ISSUE73_LOCK)
    if binary_sha256(ISSUE73_PI_INIT) != source["issue73_pi_init_sha256"]:
        raise ValueError("historical Issue 73 P0 prefix policy checkpoint hash changed")
    if root_lock_sha != source["issue73_root_source_lock_sha256_lf"]:
        raise ValueError("Issue 73 source lock changed")
    issue74_commit = str(source["issue74_selection_lock_source_commit"])
    issue74_ref_path = str(source["issue74_selection_lock_source_path"])
    issue74_blob_sha = git_blob_sha256(issue74_commit, issue74_ref_path)
    copied_sha = normalized_sha256(SELECTION_PATH)
    if issue74_blob_sha != source["issue74_selection_lock_sha256_lf"] or copied_sha != issue74_blob_sha:
        raise ValueError("Issue 74 selection lock differs from the cited, normalized remote source")
    selection = load_json(SELECTION_PATH)
    if selection.get("schema_id") != "issue74-prospective-selection-lock-v1":
        raise ValueError("unexpected Issue 74 selection-lock schema")
    if selection.get("status") != "PRESELECTION_LOCKED_PENDING_INDEPENDENT_REVIEW":
        raise ValueError("Issue 74 selection-lock status is not the cited preselection state")
    if str(selection.get("source_code_commit")) != source["issue74_selection_source_code_commit"]:
        raise ValueError("Issue 74 owner/root provenance source commit changed")
    rows = [identity_row(row) for row in selection.get("selected_roots", [])]
    owners = [int(value) for value in config["stage0"]["historical_debug_owner_seeds"]]
    if len(rows) != 32 or {row["owner_seed"] for row in rows} != set(owners):
        raise ValueError("the locked 32-root provenance population changed")
    source_valid: list[dict[str, Any]] = []
    for row in rows:
        if row["legal_occurrence_count"] < 2:
            raise ValueError("a preselected candidate is no longer a genuine multi-choice root")
        if row["screen"] in {"BATTLE", "COMBAT"}:
            raise ValueError("a preselected root is a battle screen")
        root_path = ROOTS / f"{row['root_id']}.json"
        if normalized_sha256(root_path) != row["source_file_sha256_lf"]:
            raise ValueError(f"locked source root hash mismatch: {row['root_id']}")
        source_valid.append(row)
    selected: list[dict[str, Any]] = []
    for owner in owners:
        ordered = sorted(
            (row for row in source_valid if row["owner_seed"] == owner),
            key=lambda row: row["selection_rank_within_owner"],
        )
        if len(ordered) != 4:
            raise ValueError(f"owner {owner} does not have exactly four locked candidates")
        chosen = ordered[0]
        if chosen["root_id"] != config["stage0"]["root_ids"][owners.index(owner)]:
            raise ValueError(f"predeclared first source-valid root changed for owner {owner}")
        selected.append({**chosen, "selection_rule": "first_source_hash_valid_in_locked_rank_order"})
    for owner in owners:
        run_path = OWNER_RUNS / f"{owner}.json"
        if not run_path.is_file():
            raise ValueError(f"historical owner run receipt is missing: {owner}")
    return {
        "issue73_source_commit": issue73_commit,
        "issue73_root_tree_git_sha1": source["issue73_root_tree_git_sha1"],
        "issue73_root_source_lock_sha256_lf": root_lock_sha,
        "issue73_pi_init_sha256": binary_sha256(ISSUE73_PI_INIT),
        "issue74_selection_source_commit": issue74_commit,
        "issue74_selection_source_path": issue74_ref_path,
        "issue74_selection_lock_sha256_lf": copied_sha,
        "issue74_source_code_commit": source["issue74_selection_source_code_commit"],
        "candidate_roots": sorted(rows, key=lambda row: (row["owner_seed"], row["selection_rank_within_owner"])),
        "selected_roots": selected,
        "owner_run_receipts": [
            {
                "owner_seed": owner,
                "path": str(run_path.relative_to(ROOT).as_posix()),
                "sha256_lf": normalized_sha256(OWNER_RUNS / f"{owner}.json"),
            }
            for owner in owners
            for run_path in [OWNER_RUNS / f"{owner}.json"]
        ],
        "selection_basis": "static source file hashes and the locked per-owner selection order only; no game outcome fields read",
        "native_checkpoints_loaded": False,
        "persisted_privileged_projections_loaded": False,
    }


def provenance_document(config: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_id": "issue75-static-root-provenance-v1",
        "status": "SOURCE_HASHES_VERIFIED_NO_NATIVE_SAMPLING",
        "selection_source": {
            "commit": config["source"]["issue74_selection_lock_source_commit"],
            "path": config["source"]["issue74_selection_lock_source_path"],
            "sha256_lf": config["source"]["issue74_selection_lock_sha256_lf"],
            "use": "owner/root provenance only",
        },
        **provenance,
    }


def verify_provenance_file(config: dict[str, Any], provenance: dict[str, Any]) -> None:
    path = STUDY / "root-provenance.json"
    if not path.is_file():
        raise ValueError("static root provenance must exist before source-lock verification")
    if load_json(path) != provenance_document(config, provenance):
        raise ValueError("static root provenance differs from the locked hashes and root identities")

def make_source_lock(config: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    files = {}
    for name in LOCKED_FILES:
        path = STUDY / name
        if not path.is_file():
            raise ValueError(f"cannot lock missing source file: {name}")
        files[name] = normalized_sha256(path)
    return {
        "schema_id": "issue75-no-expert-oracle-source-lock-v1",
        "status": "SOURCE_LOCKED_PENDING_INDEPENDENT_REVIEW",
        "study_id": config["study_id"],
        "source_parent_commit": config["source"]["spire_reviewed_commit"],
        "source_branch": "codex/issue-75-joint-oracle-stage0-20261025",
        "scope": config["study_scope"],
        "stage0_only": True,
        "formal_training_enabled": False,
        "formal_evaluation_enabled": False,
        "formal_seed_table_present": False,
        "files_sha256_lf": files,
        "provenance": provenance,
        "source_execution_contract": {
            "executor": config["source"]["pinned_executor"],
            "native_module_sha256": config["source"]["native_module_sha256"],
            "sts_lightspeed_commit": config["source"]["sts_lightspeed_commit"],
            "stsr_source_commit": config["source"]["stsr_source_commit"],
            "native_sampling_requires_exact_commit_reviewer_pass": True,
            "no_native_checkpoint_or_privileged_projection_persisted": True,
            "expert_used_by_algorithm": False,
        },
    }


def write_locked_file(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError(f"immutable lock already exists with different bytes: {path.name}")
        return
    path.write_bytes(payload)


def create_provenance() -> dict[str, Any]:
    config = load_json(CONFIG_PATH)
    provenance = verify_lineage(config)
    write_locked_file(STUDY / "root-provenance.json", provenance_document(config, provenance))
    return provenance


def create_source_lock() -> dict[str, Any]:
    config = load_json(CONFIG_PATH)
    provenance = verify_lineage(config)
    verify_provenance_file(config, provenance)
    lock = make_source_lock(config, provenance)
    write_locked_file(STUDY / "source-lock.json", lock)
    sha = normalized_sha256(STUDY / "source-lock.json")
    sha_path = STUDY / "source-lock.sha256"
    text = sha + "  source-lock.json\n"
    if sha_path.exists():
        if sha_path.read_text(encoding="utf-8") != text:
            raise FileExistsError("source-lock.sha256 is immutable")
    else:
        sha_path.write_text(text, encoding="utf-8", newline="\n")
    return lock


def verify_source_lock() -> dict[str, Any]:
    config = load_json(CONFIG_PATH)
    provenance = verify_lineage(config)
    verify_provenance_file(config, provenance)
    lock = load_json(STUDY / "source-lock.json")
    expected = make_source_lock(config, provenance)
    if lock != expected:
        raise ValueError("source lock differs from current code, source lineage or static root provenance")
    if normalized_sha256(STUDY / "source-lock.json") != (STUDY / "source-lock.sha256").read_text(encoding="utf-8").split()[0]:
        raise ValueError("source-lock digest receipt mismatch")
    return lock


def _github_get_json(path: str, token: str) -> tuple[Any, str | None]:
    """Read authenticated GitHub API JSON without using an external cache or config file."""
    import urllib.request

    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "spire-research-issue75-source-review-gate",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8")), response.headers.get("Link")


def reviewer_authentication(
    comment: dict[str, Any], current_login: str, gate: dict[str, Any]
) -> dict[str, Any] | None:
    """Require a human GitHub principal distinct from the Builder and issue owner."""
    user = comment.get("user") or {}
    raw_login = str(user.get("login", ""))
    login = raw_login.casefold()
    required_user_type = str(gate["required_github_user_type"]).casefold()
    if not login or str(user.get("type", "")).casefold() != required_user_type:
        return None
    shared_logins = {
        current_login.casefold(),
        str(gate["owner_login"]).casefold(),
    }
    if login in shared_logins:
        return None
    return {
        "reviewer_login": raw_login,
        "identity_mode": str(gate["required_identity_mode"]),
    }


def reviewer_pass_proof() -> dict[str, Any]:
    """Require an exact-commit review PASS from a distinct human GitHub principal."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GH_TOKEN or GITHUB_TOKEN is required to verify the GitHub review gate")
    lock = verify_source_lock()
    reviewer_gate = load_json(CONFIG_PATH)["authorization"]["reviewer_gate"]
    current_commit = git("rev-parse", "HEAD")
    lock_sha = normalized_sha256(STUDY / "source-lock.json")
    expected_lines = [
        "ISSUE75 REVIEW PASS",
        f"source_commit: {current_commit}",
        f"source_lock_sha256_lf: {lock_sha}",
    ]
    current_user, _ = _github_get_json("/user", token)
    current_login = str(current_user.get("login", "")).casefold()
    if not current_login:
        raise RuntimeError("GitHub authenticated user identity is unavailable")
    next_url = "/repos/lsmfttb/spire-research/issues/75/comments?per_page=100&page=1"
    matches: list[dict[str, Any]] = []
    while next_url:
        page, link_header = _github_get_json(next_url, token)
        if not isinstance(page, list):
            raise RuntimeError("GitHub issue-comment response is not a list")
        for comment in page:
            body = str(comment.get("body", "")).strip().splitlines()
            authentication = reviewer_authentication(comment, current_login, reviewer_gate)
            if body == expected_lines and authentication is not None:
                matches.append({
                    "comment_id": int(comment["id"]),
                    "reviewer_login": authentication["reviewer_login"],
                    "reviewer_authentication": authentication,
                })
        next_url = None
        if link_header:
            for part in link_header.split(","):
                if 'rel="next"' in part:
                    next_url = part.split(";", 1)[0].strip().lstrip("<").rstrip(">")
                    if next_url.startswith("https://api.github.com"):
                        next_url = next_url[len("https://api.github.com"):]
                    break
    if not matches:
        raise RuntimeError(
            "no independent GitHub Reviewer PASS from a distinct human account "
            "matches this exact source commit and lock"
        )
    selected = matches[-1]
    return {
        "comment_id": selected["comment_id"],
        "reviewer_login": selected["reviewer_login"],
        "reviewer_authentication": selected["reviewer_authentication"],
        "source_commit": current_commit,
        "source_lock_sha256_lf": lock_sha,
        "formal_training_enabled": lock["formal_training_enabled"],
        "formal_evaluation_enabled": lock["formal_evaluation_enabled"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-provenance", action="store_true")
    parser.add_argument("--write-source-lock", action="store_true")
    args = parser.parse_args()
    if args.write_provenance:
        receipt = create_provenance()
        print(json.dumps({
            "status": "SOURCE_HASHES_VERIFIED_NO_NATIVE_SAMPLING",
            "selected_roots": [row["root_id"] for row in receipt["selected_roots"]],
            "candidate_count": len(receipt["candidate_roots"]),
            "owner_run_receipt_count": len(receipt["owner_run_receipts"]),
            "native_checkpoints_loaded": False,
            "persisted_privileged_projections_loaded": False,
        }, sort_keys=True))
    if args.write_source_lock:
        lock = create_source_lock()
        print(json.dumps({
            "status": lock["status"],
            "source_lock_sha256_lf": normalized_sha256(STUDY / "source-lock.json"),
            "locked_file_count": len(lock["files_sha256_lf"]),
            "formal_training_enabled": lock["formal_training_enabled"],
            "formal_seed_table_present": lock["formal_seed_table_present"],
        }, sort_keys=True))
    if not args.write_provenance and not args.write_source_lock:
        lock = verify_source_lock()
        print(json.dumps({
            "status": "PASS",
            "source_lock_sha256_lf": normalized_sha256(STUDY / "source-lock.json"),
            "locked_file_count": len(lock["files_sha256_lf"]),
            "selected_root_count": len(lock["provenance"]["selected_roots"]),
            "native_checkpoints_loaded": False,
            "persisted_privileged_projections_loaded": False,
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
