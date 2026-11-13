"""Frozen Issue 85 TEST receipt validation and preregistered analysis."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import random
import re
from typing import Any, Mapping, Sequence

import formal_runner as formal


POLICIES = formal.POLICIES
PRIMARY_CONTRASTS = (("B", "A"), ("C", "A"), ("E", "A"), ("F", "A"))
MECHANISTIC_CONTRASTS = (
    ("D", "C"), ("F", "C"), ("F", "E"), ("D", "B"),
    ("PARENT", "A"),
    *((arm, "PARENT") for arm in formal.NEW_ARMS),
)
PROGRESSION_CONTRASTS = tuple((arm, "A") for arm in formal.NEW_ARMS)
ALPHA = 0.05


def _finite_number(value: Any, *, label: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    value = float(value)
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(f"{label} is outside its valid range")
    return value


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values or not 0.0 <= probability <= 1.0:
        raise ValueError("quantile requires nonempty values and a probability in [0, 1]")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def holm_adjust(p_values: Mapping[str, float], *, alpha: float = ALPHA) -> dict[str, dict[str, Any]]:
    if not p_values:
        raise ValueError("Holm adjustment requires at least one hypothesis")
    if not 0.0 < alpha < 1.0:
        raise ValueError("familywise alpha must be in (0, 1)")
    normalized = {
        label: _finite_number(value, label=f"p-value {label}", minimum=0.0)
        for label, value in p_values.items()
    }
    if any(value > 1.0 for value in normalized.values()):
        raise ValueError("p-values must not exceed one")
    ordered = sorted(normalized.items(), key=lambda item: (item[1], item[0]))
    adjusted: dict[str, dict[str, Any]] = {}
    running_max = 0.0
    count = len(ordered)
    for rank, (label, p_value) in enumerate(ordered):
        stepdown_p = min(1.0, (count - rank) * p_value)
        running_max = max(running_max, stepdown_p)
        adjusted[label] = {
            "raw_two_sided_p": p_value,
            "holm_adjusted_p": running_max,
            "rank_zero_based": rank,
            "reject_at_alpha": running_max <= alpha,
        }
    return adjusted


def bootstrap_mean_contrast(
    differences: Sequence[float], *, seed: int, replicates: int,
) -> dict[str, Any]:
    if not differences or replicates <= 0:
        raise ValueError("paired bootstrap needs nonempty owner differences and positive replicates")
    values = [_finite_number(value, label="owner difference") for value in differences]
    observed = math.fsum(values) / len(values)
    rng = random.Random(int(seed))
    estimates: list[float] = []
    for _ in range(replicates):
        estimates.append(math.fsum(values[rng.randrange(len(values))] for _ in values) / len(values))
    centered = [estimate - observed for estimate in estimates]
    two_sided_p = (
        1 + sum(abs(error) >= abs(observed) for error in centered)
    ) / (replicates + 1)
    return {
        "owner_count": len(values),
        "mean_difference": observed,
        "bootstrap_replicates": replicates,
        "bootstrap_seed": int(seed),
        "bootstrap_method": "owner-paired percentile interval; null-centered empirical bootstrap two-sided test",
        "ci95": [_quantile(estimates, 0.025), _quantile(estimates, 0.975)],
        "two_sided_bootstrap_p": two_sided_p,
    }


def _analysis_seed(package_commit: str, source_lock_sha256: str, contract_sha256: str) -> int:
    text = "|".join((
        formal.ANALYSIS_SEED_NAMESPACE,
        package_commit,
        source_lock_sha256,
        contract_sha256,
    ))
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:4], "big") & 0x7FFFFFFF


def _safe_package_path(package_root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise ValueError(f"receipt path must be a package-relative POSIX path: {relative}")
    root = Path(package_root).resolve(strict=True)
    candidate = root.joinpath(*pure.parts).resolve(strict=True)
    if root not in candidate.parents:
        raise ValueError(f"receipt path escapes the formal package: {relative}")
    return candidate


def _path_matches(saved: Any, expected: Path) -> bool:
    if not isinstance(saved, str) or not saved:
        return False
    try:
        return Path(saved).resolve(strict=False) == expected.resolve(strict=False)
    except (OSError, RuntimeError):
        return False


def _read_attempt(
    *, package_root: Path, policy: str, seed_row: Mapping[str, Any],
    attempt_relative: str, package_commit: str, source_lock_sha256: str, seed_table_sha256: str,
    authorization_receipt_sha256: str, expected_model_digest: str, stage: str = "test",
) -> dict[str, Any]:
    attempt_dir = _safe_package_path(package_root, attempt_relative)
    relative = PurePosixPath(attempt_relative)
    if (
        len(relative.parts) != 5
        or relative.parts[0:3] != (stage, policy, "units")
        or not relative.parts[4].startswith("attempt-")
        or not relative.parts[4][8:].isdigit()
    ):
        raise ValueError(f"{stage} attempt path is outside its registered policy/unit tree: {attempt_relative}")
    if not attempt_dir.is_dir():
        raise ValueError(f"{stage} attempt path is not a directory: {attempt_relative}")
    unit_id = str(seed_row["unit_id"])
    result_path = attempt_dir / "result.json"
    job_path = attempt_dir / "worker-job.json"
    started_path = attempt_dir / "started.json"
    finished_path = attempt_dir / "finished.json"
    if not all(path.is_file() for path in (result_path, job_path, started_path, finished_path)):
        raise ValueError(f"{stage} success {unit_id} lacks its complete job/start/finish/result receipt chain")
    attempts = [path for path in attempt_dir.parent.glob("attempt-*") if path.is_dir()]
    successes = []
    for candidate in attempts:
        result_candidate = candidate / "result.json"
        if result_candidate.is_file():
            record = formal.read_json(result_candidate)
            if record.get("status") == "SUCCEEDED":
                successes.append(result_candidate)
    if len(successes) != 1 or successes[0].resolve() != result_path.resolve():
        raise ValueError(f"{stage} unit {unit_id} does not have exactly one successful attempt receipt")

    job = formal.read_json(job_path)
    started = formal.read_json(started_path)
    finished = formal.read_json(finished_path)
    result = formal.read_json(result_path)
    run_id = f"{unit_id}-{relative.parts[4]}"
    result_sha = formal.file_sha256(result_path)
    started_mono = _finite_number(started.get("started_monotonic"), label=f"{unit_id} start time", minimum=0.0)
    finished_mono = _finite_number(finished.get("finished_monotonic"), label=f"{unit_id} finish time", minimum=0.0)
    if (
        job.get("schema_id") != "issue79-owner-worker-job-v1"
        or job.get("pilot") is not False
        or job.get("formal") is not True
        or job.get("package_commit") != package_commit
        or job.get("runner_code_commit") != package_commit
        or job.get("source_lock_sha256") != source_lock_sha256
        or job.get("seed_table_sha256") != seed_table_sha256
        or job.get("authorization_receipt_sha256") != authorization_receipt_sha256
        or job.get("unit_id") != unit_id
        or job.get("row") != dict(seed_row)
        or job.get("phase") != "evaluation"
        or not _path_matches(job.get("artifact_root"), package_root / stage / policy)
        or not _path_matches(job.get("attempt_dir"), attempt_dir)
        or not _path_matches(job.get("result_path"), result_path)
        or not _path_matches(job.get("output_dir"), attempt_dir / "native")
        or job.get("run_id") != run_id
    ):
        raise ValueError(f"{stage} worker-job receipt is not bound to its frozen source/row: {unit_id}")
    pid = started.get("pid")
    if (
        started.get("schema_id") != "issue79-owner-attempt-start-v1"
        or started.get("unit_id") != unit_id
        or started.get("owner_seed") != seed_row["owner_seed"]
        or started.get("arm") != seed_row["arm"]
        or started.get("run_id") != run_id
        or started.get("package_commit") != package_commit
        or started.get("seed_row_sha256") != hashlib.sha256(formal.canonical(dict(seed_row))).hexdigest()
        or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
        or finished.get("schema_id") != "issue79-owner-attempt-finish-v1"
        or finished.get("unit_id") != unit_id
        or finished.get("run_id") != run_id
        or finished.get("status") != "SUCCEEDED"
        or finished.get("result_sha256") != result_sha
        or finished_mono < started_mono
    ):
        raise ValueError(f"{stage} start/finish receipt chain is inconsistent: {unit_id}")

    resource = result.get("resource")
    if not isinstance(resource, Mapping):
        raise ValueError(f"{stage} result lacks resource accounting: {unit_id}")
    worker_wall = _finite_number(resource.get("worker_wall_seconds"), label=f"{unit_id} worker wall", minimum=0.0)
    worker_cpu = _finite_number(resource.get("worker_process_cpu_seconds"), label=f"{unit_id} worker CPU", minimum=0.0)
    floor = result.get("terminal_floor")
    for flag in ("act2_plus", "act3_plus", "act4_reached"):
        if not isinstance(result.get(flag), bool):
            raise ValueError(f"{stage} result lacks a source-recorded Boolean {flag}: {unit_id}")
    heart = result.get("heart_victory")
    if not isinstance(heart, bool):
        raise ValueError(f"{stage} result has an invalid Heart-victory field: {unit_id}")
    if (
        result.get("schema_id") != "issue75-stage1-unit-result-v1"
        or result.get("status") != "SUCCEEDED"
        or result.get("formal") is not True
        or result.get("unit_id") != unit_id
        or result.get("phase") != "evaluation"
        or result.get("arm") != policy
        or result.get("owner_seed") != seed_row["owner_seed"]
        or result.get("game_seed") != seed_row["game_seed"]
        or result.get("paired_owner_key") != seed_row["paired_owner_key"]
        or result.get("package_commit") != package_commit
        or result.get("runner_code_commit") != package_commit
        or result.get("source_lock_sha256") != source_lock_sha256
        or result.get("model_digest") != expected_model_digest
        or result.get("native_source_commit") != formal.LOCK["native"]["commit"]
        or result.get("stsr_source_commit") != formal.LOCK["stsr_source"]["commit"]
        or result.get("native_module_sha256") != formal.LOCK["native"]["compiled_module_sha256"]
        or result.get("resource", {}).get("ledger_run_id") != run_id
        or isinstance(floor, bool) or not isinstance(floor, int) or floor < 0
        or not isinstance(result.get("terminal_outcome"), str) or not result["terminal_outcome"]
        or (result["act3_plus"] and not result["act2_plus"])
        or (result["act4_reached"] and not result["act3_plus"])
        or (heart is True and not result["act4_reached"])
        or worker_wall < 0 or worker_cpu < 0
    ):
        raise ValueError(f"{stage} result is not a valid source-bound terminal for {unit_id}")
    return {
        **result,
        "result_sha256": result_sha,
        "result_receipt_path": result_path.relative_to(package_root).as_posix(),
        "attempt_count": len(attempts),
        "heart_win": heart is True,
        "worker_wall_seconds": worker_wall,
        "worker_process_cpu_seconds": worker_cpu,
    }


def _holm_bootstrap(
    contrast_differences: Mapping[str, Sequence[float]], *, seed: int,
    replicates: int,
) -> dict[str, dict[str, Any]]:
    if not contrast_differences or replicates != int(formal.CONTRACT["analysis"]["bootstrap_resamples"]):
        raise ValueError("formal TEST analysis must use the registered 20,000 bootstrap resamples")
    labels = list(contrast_differences)
    lengths = {len(contrast_differences[label]) for label in labels}
    if len(lengths) != 1 or not next(iter(lengths)):
        raise ValueError("all paired contrast vectors must have the same nonzero owner count")
    owner_count = next(iter(lengths))
    observed = {label: math.fsum(contrast_differences[label]) / owner_count for label in labels}
    estimates: dict[str, list[float]] = {label: [] for label in labels}
    rng = random.Random(seed)
    for _ in range(replicates):
        # One resampled owner-index vector is shared across all policies and contrasts.
        indexes = [rng.randrange(owner_count) for _ in range(owner_count)]
        for label in labels:
            values = contrast_differences[label]
            estimates[label].append(math.fsum(values[index] for index in indexes) / owner_count)
    out: dict[str, dict[str, Any]] = {}
    for label in labels:
        errors = [sample - observed[label] for sample in estimates[label]]
        p = (1 + sum(abs(error) >= abs(observed[label]) for error in errors)) / (replicates + 1)
        out[label] = {
            "owner_count": owner_count,
            "mean_difference": observed[label],
            "bootstrap_replicates": replicates,
            "ci95": [_quantile(estimates[label], 0.025), _quantile(estimates[label], 0.975)],
            "two_sided_bootstrap_p": p,
        }
    return out


def _original_analysis_source_sha256(test_producer_commit: str) -> str:
    """Hash the analysis source exactly as committed at the TEST-producing commit.

    After the Planner-authorised post-TEST amendment this file differs from the
    one that opened TEST, so the current checkout can no longer reproduce the
    pre-TEST digest. The original digest stays authenticable from git, which is
    what ties the recorded value to real bytes instead of to a claim.
    """
    study_relative = Path(__file__).resolve().relative_to(formal.REPO).as_posix()
    try:
        blob = formal.baseline_preflight.git_bytes(
            formal.REPO, "show", f"{test_producer_commit}:{study_relative}"
        )
    except Exception as error:
        raise ValueError(
            "cannot recover the pre-TEST analysis source at the TEST-producing commit "
            f"{test_producer_commit}"
        ) from error
    return hashlib.sha256(blob).hexdigest()


def _validation_producers(
    test_open: Mapping[str, Any], package_commit: str,
) -> dict[str, tuple[bool, str]]:
    """Resolve every VALIDATION group to the package that really produced it.

    The TEST-open receipt is the only authority for this mapping. A group may not
    be carried from the reading package itself, and a local group may not name a
    different producer; either mistake would make inherited evidence
    indistinguishable from work this package actually did.
    """
    provenance = test_open.get("validation_provenance")
    if (
        not isinstance(provenance, Mapping)
        or set(provenance) != set(POLICIES)
        or any(
            not isinstance(provenance.get(policy), Mapping)
            or not isinstance(provenance[policy].get("carried_by_reference"), bool)
            or re.fullmatch(
                r"[0-9a-f]{40}",
                str(provenance[policy].get("producer_package_commit", "")),
            )
            is None
            for policy in POLICIES
        )
    ):
        raise ValueError(
            "TEST-open receipt does not declare an authenticated producer for every validation group"
        )
    resolved: dict[str, tuple[bool, str]] = {}
    for policy in POLICIES:
        carried = provenance[policy]["carried_by_reference"]
        producer = str(provenance[policy]["producer_package_commit"])
        if carried and producer == package_commit:
            raise ValueError(
                f"formal {policy} validation group is declared carried from the reading package itself"
            )
        if not carried and producer != package_commit:
            raise ValueError(
                f"formal {policy} validation group is local but names another producer"
            )
        resolved[policy] = (carried, producer)
    return resolved


def analyze_test_bundle(
    *, package_root: Path, seed_table_path: Path, test_bundle_path: Path,
    policy_model_digests: Mapping[str, str],
) -> dict[str, Any]:
    package_root = Path(package_root).resolve(strict=True)
    seed_table_path = Path(seed_table_path).resolve(strict=True)
    test_bundle_path = Path(test_bundle_path).resolve(strict=True)
    if package_root not in seed_table_path.parents or package_root not in test_bundle_path.parents:
        raise ValueError("formal seed table and TEST bundle must be inside the source-bound package")
    if set(policy_model_digests) != set(POLICIES):
        raise ValueError("TEST analysis requires exactly the seven registered policy model digests")
    seed_table = formal.read_json(seed_table_path)
    if seed_table.get("schema_id") != "issue85-formal-seed-table-v1":
        raise ValueError("formal TEST analysis received a nonformal or unknown seed table")
    package_commit = str(seed_table.get("package_commit", ""))
    source_lock_sha256 = str(seed_table.get("source_lock_sha256", ""))
    # The reading code is allowed to differ from the TEST producer under the
    # Planner-authorised post-TEST technical amendment. What must still hold is
    # that the *data* package is the original producer and that the registered
    # contract is untouched. The original analysis source is authenticated
    # separately below, against its real bytes at the TEST-producing commit.
    if (
        seed_table.get("package_commit") != package_commit
        or seed_table.get("source_lock_sha256") != source_lock_sha256
        or seed_table.get("contract_sha256") != formal.file_sha256(formal.CONTRACT_PATH)
    ):
        raise ValueError("analysis code, source lock, or registered contract differs from the frozen TEST source")
    expected_package_root = (
        formal.DATA_ROOT / "issue85" / "artifacts" / "formal" / package_commit
    ).resolve(strict=False)
    if package_root != expected_package_root:
        raise ValueError("formal TEST analysis package path is not the registered durable package location")
    identity = {"package_commit": package_commit, "source_lock_sha256": source_lock_sha256}
    formal.validate_formal_seed_table(seed_table, identity=identity)
    seed_table_sha256 = formal.file_sha256(seed_table_path)
    bundle = formal.read_json(test_bundle_path)
    expected_model_digests = {policy: str(policy_model_digests.get(policy, "")) for policy in POLICIES}
    test_open_path = package_root / "test-open-receipt.json"
    if not test_open_path.is_file():
        raise ValueError("frozen TEST analysis requires a prior source/seed/validation/approval TEST-open receipt")
    test_open = formal.read_json(test_open_path)
    test_open_sha256 = formal.file_sha256(test_open_path)
    original_analysis_source_sha256 = _original_analysis_source_sha256(package_commit)
    analysis_code_sha256 = formal.file_sha256(Path(__file__).resolve())
    post_test_amendment = analysis_code_sha256 != original_analysis_source_sha256
    validation_bindings = test_open.get("validation_group_receipts")
    valid_issue_comment = re.compile(
        r"https://github\.com/lsmfttb/spire-research/issues/85#issuecomment-[1-9][0-9]*"
    )
    if (
        test_open.get("schema_id") != "issue85-formal-test-open-v1"
        or not str(test_open.get("status", "")).startswith("TEST_OPEN /")
        or test_open.get("package_commit") != package_commit
        or test_open.get("source_lock_sha256") != source_lock_sha256
        or test_open.get("contract_sha256") != formal.file_sha256(formal.CONTRACT_PATH)
        or test_open.get("seed_table_sha256") != seed_table_sha256
        or test_open.get("analysis_source_sha256") != original_analysis_source_sha256
        or test_open.get("actor_update_schedule_sha256") != seed_table.get("actor_update_schedule_sha256")
        or test_open.get("test_owner_range") != list(formal.CONTRACT["registered_design_unchanged"]["test"]["owners"])
        or test_open.get("test_games_started_before_open_receipt") != 0
        or test_open.get("validation_used_for_model_selection") is not False
        or not isinstance(test_open.get("authorization_receipt_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(test_open.get("authorization_receipt_sha256", "")))
        or not isinstance(test_open.get("owner_approval_comment_url"), str)
        or valid_issue_comment.fullmatch(str(test_open.get("owner_approval_comment_url", ""))) is None
        or not isinstance(test_open.get("reviewer_approval_comment_url"), str)
        or valid_issue_comment.fullmatch(str(test_open.get("reviewer_approval_comment_url", ""))) is None
        or not isinstance(validation_bindings, Mapping)
        or set(validation_bindings) != set(POLICIES)
        or any(
            not isinstance(validation_bindings.get(policy), Mapping)
            or validation_bindings[policy].get("completed") != 64
            or not re.fullmatch(r"[0-9a-f]{64}", str(validation_bindings[policy].get("receipt_sha256", "")))
            for policy in POLICIES
        )
        or test_open.get("test_policy_models") != expected_model_digests
        or bundle.get("schema_id") != "issue85-formal-test-receipts-v1"
        or bundle.get("status") != "SUCCEEDED"
        or bundle.get("package_commit") != package_commit
        or bundle.get("source_lock_sha256") != source_lock_sha256
        or bundle.get("seed_table_sha256") != seed_table_sha256
        or bundle.get("authorization_receipt_sha256") != test_open.get("authorization_receipt_sha256")
        or bundle.get("test_open_receipt_sha256") != test_open_sha256
        or bundle.get("formal_games_started") != 2_240
        or set(bundle.get("groups", {})) != set(POLICIES)
        or any(not digest for digest in expected_model_digests.values())
    ):
        raise ValueError("formal TEST receipt bundle is not bound to the frozen seed/model/source package")
    validation_config = formal.CONTRACT["registered_design_unchanged"]["validation"]
    validation_owners = range(int(validation_config["owners"][0]), int(validation_config["owners"][1]) + 1)
    validation_rows = {
        policy: {
            int(row["owner_seed"]): row
            for row in seed_table["rows"]
            if row.get("issue85_phase") == "validation" and row.get("arm") == policy
        }
        for policy in POLICIES
    }
    if any(set(validation_rows[policy]) != set(validation_owners) for policy in POLICIES):
        raise ValueError("frozen formal validation seed table does not contain the exact disjoint 64-owner roster")
    # Each validation group is read from the package that really produced it. The
    # TEST-open receipt is the authority for that mapping; a group may not be
    # carried from the very package that is reading it, and a producer may not be
    # silently reassigned to whatever package happens to be executing now.
    validation_producers = _validation_producers(test_open, package_commit)
    for policy in POLICIES:
        binding = validation_bindings[policy]
        carried, producer_commit = validation_producers[policy]
        producer_root = (
            (formal.DATA_ROOT / "issue85" / "artifacts" / "formal" / producer_commit).resolve(strict=False)
            if carried else package_root
        )
        validation_receipt_path = producer_root / "validation" / policy / "group-receipt.json"
        if not validation_receipt_path.is_file() or formal.file_sha256(validation_receipt_path) != binding["receipt_sha256"]:
            raise ValueError(f"TEST-open receipt does not bind the immutable {policy} validation group receipt")
        validation_receipt = formal.read_json(validation_receipt_path)
        if carried:
            producer_seed_path = producer_root / "formal-seed-table.json"
            producer_authorization_path = producer_root / "formal-execution-authorization.json"
            if not producer_seed_path.is_file() or not producer_authorization_path.is_file():
                raise ValueError(
                    f"carried {policy} validation group lacks its producer seed table or authorization"
                )
            producer_seed_table_sha256 = formal.file_sha256(producer_seed_path)
            producer_authorization_sha256 = formal.file_sha256(producer_authorization_path)
            producer_rows = {
                int(row["owner_seed"]): row
                for row in formal.read_json(producer_seed_path)["rows"]
                if row.get("issue85_phase") == "validation" and row.get("arm") == policy
            }
            if producer_rows != validation_rows[policy]:
                raise ValueError(
                    f"carried {policy} validation group was produced from a different registered roster"
                )
        else:
            producer_seed_table_sha256 = seed_table_sha256
            producer_authorization_sha256 = str(test_open["authorization_receipt_sha256"])
        units = validation_receipt.get("units")
        if (
            validation_receipt.get("schema_id") != "issue85-formal-game-group-v1"
            or validation_receipt.get("status") != "SUCCEEDED"
            or validation_receipt.get("stage_id") != f"VALIDATION-{policy}"
            or validation_receipt.get("package_commit") != producer_commit
            or validation_receipt.get("source_lock_sha256") != source_lock_sha256
            or validation_receipt.get("seed_table_sha256") != producer_seed_table_sha256
            or validation_receipt.get("authorization_receipt_sha256") != producer_authorization_sha256
            or validation_receipt.get("completed") != len(validation_owners)
            or validation_receipt.get("model_selection_or_tuning") is not False
            or binding.get("completed") != len(validation_owners)
            or not isinstance(units, list) or len(units) != len(validation_owners)
        ):
            raise ValueError(f"formal {policy} validation receipt is incomplete, misbound, or selection-contaminated")
        seen_validation_owners: set[int] = set()
        for unit in units:
            if not isinstance(unit, Mapping):
                raise ValueError(f"formal {policy} validation receipt contains a malformed unit")
            owner = int(unit.get("owner_seed", -1))
            row = validation_rows[policy].get(owner)
            if row is None or row["unit_id"] != unit.get("unit_id") or owner in seen_validation_owners:
                raise ValueError(f"formal {policy} validation receipt has a duplicate or unregistered owner")
            outcome = _read_attempt(
                package_root=producer_root,
                policy=policy,
                seed_row=row,
                attempt_relative=str(unit.get("attempt_path", "")),
                package_commit=producer_commit,
                source_lock_sha256=source_lock_sha256,
                seed_table_sha256=producer_seed_table_sha256,
                authorization_receipt_sha256=producer_authorization_sha256,
                expected_model_digest=expected_model_digests[policy],
                stage="validation",
            )
            if (
                unit.get("result_sha256") != outcome["result_sha256"]
                or unit.get("attempt_count") != outcome["attempt_count"]
                or unit.get("model_digest") != expected_model_digests[policy]
            ):
                raise ValueError(f"formal {policy} validation group receipt differs from its attempt lineage for owner {owner}")
            seen_validation_owners.add(owner)
        if seen_validation_owners != set(validation_owners):
            raise ValueError(f"formal {policy} validation receipt omits a registered owner")
    for policy, expected in (("PARENT", formal.LOCK["baseline_P"]["model_digest"]), ("A", formal.LOCK["baseline_A"]["model_digest"])):
        if expected_model_digests[policy] != expected:
            raise ValueError(f"TEST policy {policy} is not the exact source-locked baseline")
    test_rows = [row for row in seed_table["rows"] if row.get("issue85_phase") == "test"]
    expected_rows = {
        policy: {
            int(row["owner_seed"]): row
            for row in test_rows if row.get("arm") == policy
        }
        for policy in POLICIES
    }
    owners = range(
        int(formal.CONTRACT["registered_design_unchanged"]["test"]["owners"][0]),
        int(formal.CONTRACT["registered_design_unchanged"]["test"]["owners"][1]) + 1,
    )
    if any(set(expected_rows[p]) != set(owners) for p in POLICIES):
        raise ValueError("frozen formal TEST seed table does not contain the exact 320-owner roster")
    groups: dict[str, dict[int, dict[str, Any]]] = {}
    for policy in POLICIES:
        entries = bundle["groups"][policy].get("attempts")
        if not isinstance(entries, list) or len(entries) != len(owners):
            raise ValueError(f"TEST receipt manifest does not contain 320 {policy} attempts")
        by_owner: dict[int, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError(f"TEST {policy} manifest contains a malformed attempt pointer")
            unit_id = str(entry.get("unit_id", ""))
            owner = next((owner for owner in owners if unit_id == f"TEST:{owner}:{policy}"), None)
            if owner is None or owner in by_owner:
                raise ValueError(f"TEST {policy} manifest has a duplicate or unregistered unit: {unit_id}")
            if unit_id != expected_rows[policy][owner]["unit_id"]:
                raise ValueError(f"TEST unit {unit_id} differs from the frozen seed table")
            outcome = _read_attempt(
                package_root=package_root,
                policy=policy,
                seed_row=expected_rows[policy][owner],
                attempt_relative=str(entry.get("attempt_path", "")),
                package_commit=package_commit,
                source_lock_sha256=source_lock_sha256,
                seed_table_sha256=seed_table_sha256,
                authorization_receipt_sha256=str(test_open["authorization_receipt_sha256"]),
                expected_model_digest=expected_model_digests[policy],
            )
            if (
                entry.get("result_sha256") != outcome["result_sha256"]
                or entry.get("attempt_count") != outcome["attempt_count"]
            ):
                raise ValueError(f"TEST {policy} receipt manifest differs from its attempt lineage: {unit_id}")
            by_owner[owner] = outcome
        if set(by_owner) != set(owners):
            raise ValueError(f"TEST {policy} receipts omit a frozen owner")
        groups[policy] = by_owner

    registered_contrasts = tuple(formal.CONTRACT["analysis"]["confirmatory_contrasts"])
    if registered_contrasts != tuple(f"{left}-{right}" for left, right in PRIMARY_CONTRASTS):
        raise ValueError("formal analysis contrast family differs from the registered contract")
    differences: dict[str, list[float]] = {}
    for left, right in (*PRIMARY_CONTRASTS, *MECHANISTIC_CONTRASTS):
        label = f"{left}-{right}:terminal_floor"
        differences[label] = [
            float(groups[left][owner]["terminal_floor"] - groups[right][owner]["terminal_floor"])
            for owner in owners
        ]
    for left, right in PROGRESSION_CONTRASTS:
        label = f"{left}-{right}:act2_plus"
        differences[label] = [
            float(groups[left][owner]["act2_plus"] - groups[right][owner]["act2_plus"])
            for owner in owners
        ]
    analysis_seed = _analysis_seed(
        package_commit, source_lock_sha256, str(seed_table.get("contract_sha256", "")),
    )
    bootstrap = _holm_bootstrap(
        differences,
        seed=analysis_seed,
        replicates=int(formal.CONTRACT["analysis"]["bootstrap_resamples"]),
    )
    confirmatory_labels = {f"{left}-{right}:terminal_floor" for left, right in PRIMARY_CONTRASTS}
    adjusted = holm_adjust({label: bootstrap[label]["two_sided_bootstrap_p"] for label in confirmatory_labels})
    for label, values in adjusted.items():
        bootstrap[label]["holm_adjusted_p"] = values["holm_adjusted_p"]
        bootstrap[label]["confirmatory_reject_at_0_05"] = values["reject_at_alpha"]

    descriptive: dict[str, Any] = {}
    for label, result in bootstrap.items():
        if label not in confirmatory_labels:
            descriptive[label] = {**result, "interpretation": "prespecified secondary; unadjusted descriptive interval"}
    aggregate: dict[str, Any] = {}
    floor_histograms: dict[str, dict[str, int]] = {}
    for policy in POLICIES:
        outcomes = [groups[policy][owner] for owner in owners]
        histogram: dict[str, int] = {}
        for outcome in outcomes:
            key = str(int(outcome["terminal_floor"]))
            histogram[key] = histogram.get(key, 0) + 1
        histogram.setdefault("16", 0)
        floor_histograms[policy] = dict(sorted(histogram.items(), key=lambda item: int(item[0])))
        aggregate[policy] = {
            "owner_count": len(outcomes),
            "mean_terminal_floor": math.fsum(float(row["terminal_floor"]) for row in outcomes) / len(outcomes),
            "act2_plus_count": sum(row["act2_plus"] for row in outcomes),
            "act2_plus_rate": math.fsum(float(row["act2_plus"]) for row in outcomes) / len(outcomes),
            "act3_plus_count": sum(row["act3_plus"] for row in outcomes),
            "act3_plus_rate": math.fsum(float(row["act3_plus"]) for row in outcomes) / len(outcomes),
            "act4_reached_count": sum(row["act4_reached"] for row in outcomes),
            "act4_reached_rate": math.fsum(float(row["act4_reached"]) for row in outcomes) / len(outcomes),
            "heart_victory_count": sum(row["heart_win"] for row in outcomes),
            "heart_victory_rate": math.fsum(float(row["heart_win"]) for row in outcomes) / len(outcomes),
            "total_worker_process_cpu_seconds": math.fsum(row["worker_process_cpu_seconds"] for row in outcomes),
            "total_worker_wall_seconds": math.fsum(row["worker_wall_seconds"] for row in outcomes),
        }
    paired = []
    for owner in owners:
        paired.append({
            "owner_seed": owner,
            "terminal_floor_by_policy": {policy: groups[policy][owner]["terminal_floor"] for policy in POLICIES},
            "act2_plus_by_policy": {policy: groups[policy][owner]["act2_plus"] for policy in POLICIES},
            "act3_plus_by_policy": {policy: groups[policy][owner]["act3_plus"] for policy in POLICIES},
            "act4_reached_by_policy": {policy: groups[policy][owner]["act4_reached"] for policy in POLICIES},
            "heart_victory_by_policy": {policy: groups[policy][owner]["heart_win"] for policy in POLICIES},
            "result_sha256_by_policy": {policy: groups[policy][owner]["result_sha256"] for policy in POLICIES},
            "model_digest_by_policy": {policy: groups[policy][owner]["model_digest"] for policy in POLICIES},
            "policy_rng_seed_by_policy": {policy: expected_rows[policy][owner]["policy_rng_seed"] for policy in POLICIES},
        })
    report = {
        "schema_id": "issue85-formal-test-analysis-v1",
        "status": "ANALYZED / PENDING_INDEPENDENT_REVIEW_AND_PLANNER_ASSESSMENT",
        "package_commit": package_commit,
        "source_lock_sha256": source_lock_sha256,
        "seed_table_sha256": seed_table_sha256,
        "test_receipt_bundle_sha256": formal.file_sha256(test_bundle_path),
        "test_open_receipt_sha256": test_open_sha256,
        "analysis_code_sha256": analysis_code_sha256,
        # Both source identities stay on the record: the analysis that was frozen
        # before TEST existed, and the amended reader that actually computed this.
        "original_pre_test_analysis_source_sha256": original_analysis_source_sha256,
        "post_test_technical_erratum": post_test_amendment,
        "test_producer_package_commit": package_commit,
        "validation_producer_package_commits": {
            policy: validation_producers[policy][1] for policy in POLICIES
        },
        "validation_groups_carried_by_reference": {
            policy: validation_producers[policy][0] for policy in POLICIES
        },
        "analysis_seed": analysis_seed,
        "analysis_seed_rule": (
            f"first 31 bits of SHA256({formal.ANALYSIS_SEED_NAMESPACE}|package_commit|"
            "source_lock_sha256|contract_sha256)"
        ),
        "owner_count": len(owners),
        "bootstrap_resamples": int(formal.CONTRACT["analysis"]["bootstrap_resamples"]),
        "bootstrap_owner_resampling": "one owner-index vector shared by all seven policies and all contrasts per resample",
        "primary_endpoint": "mean terminal floor on the fresh registered 320-owner TEST roster",
        "confirmatory_family": {
            "contrasts": [f"{left}-{right}" for left, right in PRIMARY_CONTRASTS],
            "null_test": "two-sided null-centered empirical owner bootstrap",
            "interval": "95% percentile owner-cluster bootstrap with linear-interpolated sample quantile",
            "familywise_error_control": "Holm step-down over the four registered two-sided floor tests",
            "results": {label: bootstrap[label] for label in sorted(confirmatory_labels)},
        },
        "prespecified_secondary_unadjusted": descriptive,
        "per_policy_descriptive": aggregate,
        "terminal_floor_histograms_including_floor_16": floor_histograms,
        "paired_owner_outcomes": paired,
        "interpretation": (
            "A valid study requires all registered source-bound terminal receipts and an independent review. "
            "A positive confirmatory floor contrast alone is not a live-client claim; interpret it with the "
            "source-recorded Act-2/first-Boss progression endpoint. A null or harmful result remains a valid result."
        ),
        "limitations": [
            "one learner lineage per new arm does not establish independent-training stability",
            "results apply to the pinned privileged native surrogate, not public-information or live-client play",
            "secondary intervals are descriptive and unadjusted",
            "analysis does not itself certify model/checkpoint ancestry; source-bound training receipts must be reviewed",
        ],
    }
    return report


def write_analysis_once(package_root: Path, report: Mapping[str, Any]) -> tuple[Path, str]:
    package_root = Path(package_root).resolve(strict=True)
    output = package_root / "formal-test-analysis.json"
    digest = formal._write_or_verify_once(output, report)
    return output, digest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-bundle", type=Path, required=True)
    parser.add_argument("--policy-model-digests", type=Path, required=True)
    args = parser.parse_args(argv)
    # The amended reader is a later commit than the package that produced TEST, so
    # the package is located from the evidence itself, not from the checkout. The
    # bundle must sit directly inside a registered durable package, and
    # analyze_test_bundle re-checks that the package is the original producer.
    bundle_path = Path(args.test_bundle).resolve(strict=True)
    package_root = bundle_path.parent
    registered = (
        formal.DATA_ROOT / "issue85" / "artifacts" / "formal"
    ).resolve(strict=False)
    if package_root.parent != registered:
        raise SystemExit(
            f"TEST bundle must live inside a registered durable package, not {package_root}"
        )
    model_digests = formal.read_json(args.policy_model_digests)
    report = analyze_test_bundle(
        package_root=package_root,
        seed_table_path=package_root / "formal-seed-table.json",
        test_bundle_path=args.test_bundle,
        policy_model_digests=model_digests,
    )
    output, digest = write_analysis_once(package_root, report)
    print(json.dumps({
        "analysis_path": str(output),
        "analysis_sha256": digest,
        "status": report["status"],
        "package_commit": report["package_commit"],
        "source_lock_sha256": report["source_lock_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
