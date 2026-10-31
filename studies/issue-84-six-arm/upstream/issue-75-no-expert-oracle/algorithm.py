from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import random
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

try:
    import resource
except ImportError:  # pragma: no cover - native Stage 0 is pinned to Linux
    resource = None  # type: ignore[assignment]


SEARCH_CPU_CAP_SECONDS = 32.0
# The prior receipts showed up to 0.503407 CPU-s of boundary overshoot.
# Stop two seconds early so a final in-flight native transition remains below
# the registered 32 CPU-s hard ceiling.
SEARCH_CPU_STOP_MARGIN_SECONDS = 2.0
SEARCH_CPU_STOP_SECONDS = SEARCH_CPU_CAP_SECONDS - SEARCH_CPU_STOP_MARGIN_SECONDS
SEARCH_STARTED_ATTEMPT_CAP = 16
R_ROOT_TRIAL_CAP = 16
J_ROOT_TRIAL_CAP = 8
J_LATER_MUTATION_TRIAL_CAP = 8
J_COMPLETED_PORTFOLIO_CAP = 4


class SearchBudgetExhausted(RuntimeError):
    pass


def process_tree_cpu_seconds() -> float:
    """In the locked single-process simulator topology, include process and reaped children."""
    if resource is None:
        return time.process_time()
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return float(own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime)


class InvocationBudget:
    """Charge before every started continuation, including failures and incomplete runs."""

    def __init__(
        self,
        *,
        cpu_cap_seconds: float = SEARCH_CPU_CAP_SECONDS,
        started_attempt_cap: int = SEARCH_STARTED_ATTEMPT_CAP,
        cpu_reader: Callable[[], float] = process_tree_cpu_seconds,
    ) -> None:
        if cpu_cap_seconds <= 0 or started_attempt_cap <= 0:
            raise ValueError("search invocation budgets must be positive")
        self.cpu_cap_seconds = float(cpu_cap_seconds)
        self.started_attempt_cap = int(started_attempt_cap)
        self.cpu_reader = cpu_reader
        self._start_cpu = float(cpu_reader())
        self.started_attempts = 0
        self.attempt_cpu_seconds: list[float] = []
        self.stop_reason: str | None = None

    @property
    def cpu_seconds(self) -> float:
        return max(0.0, float(self.cpu_reader()) - self._start_cpu)

    def can_start_attempt(self) -> bool:
        if self.stop_reason == "CPU_CAP":
            return False
        if self.started_attempts >= self.started_attempt_cap:
            self.stop_reason = self.stop_reason or "STARTED_ATTEMPT_CAP"
            return False
        if self.cpu_seconds >= self.cpu_cap_seconds:
            self.stop_reason = self.stop_reason or "CPU_CAP"
            return False
        return True

    def guard_transition(self, safety_margin_seconds: float = 0.0) -> None:
        if self.cpu_seconds + safety_margin_seconds >= self.cpu_cap_seconds:
            self.stop_reason = "CPU_CAP"
            raise SearchBudgetExhausted("per-search process-tree CPU watchdog stopped before a native transition")

    @contextmanager
    def attempt(self) -> Iterator[int]:
        if not self.can_start_attempt():
            raise SearchBudgetExhausted(self.stop_reason or "search invocation budget exhausted")
        self.started_attempts += 1
        index = self.started_attempts
        start = float(self.cpu_reader())
        try:
            yield index
        finally:
            spent = max(0.0, float(self.cpu_reader()) - start)
            self.attempt_cpu_seconds.append(spent)
            if self.cpu_seconds >= self.cpu_cap_seconds:
                self.stop_reason = self.stop_reason or "CPU_CAP"
            if self.started_attempts >= self.started_attempt_cap:
                self.stop_reason = self.stop_reason or "STARTED_ATTEMPT_CAP"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def identity_key(identity: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(identity).encode("utf-8")).hexdigest()


def derive_attempt_seed(study_id: str, root_id: str, invocation_seed: int, attempt_index: int, purpose: str) -> int:
    payload = f"{study_id}|{root_id}|{invocation_seed}|{attempt_index}|{purpose}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & 0x7FFFFFFF


def root_action_schedule(
    legal_occurrence_count: int,
    *,
    study_id: str,
    root_id: str,
    invocation_seed: int,
    trial_count: int,
) -> list[int]:
    if legal_occurrence_count < 1:
        raise ValueError("a root proposal requires at least one live legal occurrence")
    if trial_count < 0:
        raise ValueError("trial count cannot be negative")
    offset = derive_attempt_seed(study_id, root_id, invocation_seed, 0, "root-action-order") % legal_occurrence_count
    return [int((offset + index) % legal_occurrence_count) for index in range(trial_count)]


@dataclass(frozen=True)
class Decision:
    """Training-only decision evidence; checkpoint is volatile and never serialized."""

    step_index: int
    state_fingerprint: str
    legal_identities: tuple[Mapping[str, Any], ...]
    chosen_index: int
    candidate_features: Any = field(default=None, repr=False, compare=False)
    log_probabilities: Any = field(default=None, repr=False, compare=False)
    checkpoint: Any = field(default=None, repr=False, compare=False)
    adapter: Any = field(default=None, repr=False, compare=False)
    is_root: bool = False
    screen: str = ""
    root_fingerprints: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)

    @property
    def chosen_identity(self) -> Mapping[str, Any]:
        return self.legal_identities[self.chosen_index]


def safe_exception_trace(
    error: BaseException, *, max_frames: int = 8,
) -> tuple[tuple[str, str, int], ...]:
    """Return bounded static call-site metadata without exception text or locals."""
    import traceback

    if not 1 <= int(max_frames) <= 16:
        raise ValueError("safe exception trace frame limit must be in [1, 16]")
    frames = traceback.extract_tb(error.__traceback__)[-int(max_frames):]
    result = []
    for frame in frames:
        filename = str(frame.filename).replace("\\", "/").rsplit("/", 1)[-1]
        result.append((filename[:128] or "<unknown>", str(frame.name)[:128], int(frame.lineno)))
    return tuple(result)


def safe_exception_detail(error: BaseException) -> dict[str, str]:
    """Return bounded static exception metadata without messages or local values."""
    error_type = type(error).__name__
    if not error_type.isidentifier():
        error_type = "UnknownError"
    result = {"kind": "exception", "exception_type": error_type[:64]}
    if isinstance(error, AttributeError):
        missing = getattr(error, "name", None)
        if isinstance(missing, str) and missing.isidentifier() and len(missing) <= 128:
            result["kind"] = "missing_attribute"
            result["missing_attribute"] = missing
    return result


@dataclass(frozen=True)
class TrialResult:
    attempt_index: int
    complete: bool
    heart_win: bool = False
    terminal_floor: float | None = None
    root_identity: Mapping[str, Any] | None = None
    decisions: tuple[Decision, ...] = ()
    failure_boundary: str | None = None
    failure_trace: tuple[tuple[str, str, int], ...] = field(default_factory=tuple, repr=False, compare=False)
    failure_detail: Mapping[str, str] | None = field(default=None, repr=False, compare=False)
    cpu_seconds: float | None = None
    wall_seconds: float | None = None
    native_rng_source: str = "restored_checkpoint_native_rng_same_future_stream"
    natural_owner: bool = False
    search_root_id: str | None = None

    def __post_init__(self) -> None:
        if self.attempt_index < 1:
            raise ValueError("attempt index must be positive")
        if self.complete:
            if self.terminal_floor is None or not math.isfinite(float(self.terminal_floor)):
                raise ValueError("a completed native trajectory requires a finite terminal floor")
            if self.decisions and not self.decisions[0].is_root:
                raise ValueError("a completed trajectory must retain its actual root decision")
            if not self.decisions and not self.natural_owner:
                raise ValueError("a completed branch requires its actual root decision")
            if self.failure_boundary is not None:
                raise ValueError("completed trajectory cannot carry an incomplete failure boundary")
        elif self.heart_win or self.terminal_floor is not None:
            raise ValueError("incomplete trajectories cannot carry fabricated terminal outcomes")
        if self.complete and self.failure_trace:
            raise ValueError("completed trajectory cannot carry a failure trace")
        if self.complete and self.failure_detail is not None:
            raise ValueError("completed trajectory cannot carry failure details")
        if len(self.failure_trace) > 8:
            raise ValueError("failure trace exceeds its bounded receipt limit")
        for filename, function, line in self.failure_trace:
            if (
                not filename or "/" in filename or "\\" in filename
                or not function or int(line) < 1
            ):
                raise ValueError("failure trace must contain only basename/function/line metadata")
        if self.failure_detail is not None:
            detail = dict(self.failure_detail)
            if detail.get("kind") not in {"exception", "missing_attribute"}:
                raise ValueError("failure detail has an unknown kind")
            if set(detail) - {"kind", "exception_type", "missing_attribute"}:
                raise ValueError("failure detail contains an unsupported field")
            exception_type = detail.get("exception_type")
            if not isinstance(exception_type, str) or not exception_type.isidentifier() or len(exception_type) > 64:
                raise ValueError("failure detail has an invalid exception type")
            missing = detail.get("missing_attribute")
            if detail["kind"] == "missing_attribute":
                if not isinstance(missing, str) or not missing.isidentifier() or len(missing) > 128:
                    raise ValueError("failure detail has an invalid missing attribute")
            elif missing is not None:
                raise ValueError("generic failure detail cannot carry a missing attribute")

    @property
    def rank(self) -> tuple[int, float, int]:
        if not self.complete:
            raise ValueError("incomplete trajectories are not rankable")
        return (int(self.heart_win), float(self.terminal_floor), -int(self.attempt_index))


@dataclass(frozen=True)
class MutationTarget:
    parent_attempt_index: int
    parent_decision: Decision
    alternative_index: int

    def __post_init__(self) -> None:
        if self.parent_decision.is_root or self.parent_decision.checkpoint is None:
            raise ValueError("joint mutation target must be a later actually reached checkpoint")
        if not 0 <= self.alternative_index < len(self.parent_decision.legal_identities):
            raise ValueError("mutation occurrence is outside the live legal list")
        if self.alternative_index == self.parent_decision.chosen_index:
            raise ValueError("later mutation must select a different legal occurrence")


@dataclass(frozen=True)
class SearchOutcome:
    arm: str
    selected_root_identity: Mapping[str, Any] | None
    best_completed_attempt_index: int | None
    status: str
    started_attempts: int
    completed_trajectories: int
    cpu_seconds: float
    attempt_cpu_seconds: tuple[float, ...]
    stop_reason: str | None
    mutation_attempts_started: int
    native_rng_source: str = "same restored native RNG state/path; policy RNG alone varies"


AttemptRunner = Callable[[int, int, MutationTarget | None], TrialResult]


def _sort_completed(rows: Sequence[TrialResult]) -> list[TrialResult]:
    return sorted((row for row in rows if row.complete), key=lambda row: row.rank, reverse=True)


def _target_key(target: Decision) -> tuple[int, str]:
    return (int(target.step_index), str(target.state_fingerprint))


def _choose_mutation_target(
    portfolio: Sequence[TrialResult],
    used: set[tuple[int, str, str]],
    *,
    study_id: str,
    root_id: str,
    invocation_seed: int,
    proposal_index: int,
) -> MutationTarget | None:
    for parent in _sort_completed(portfolio):
        target_rows = sorted(
            (decision for decision in parent.decisions[1:] if decision.checkpoint is not None),
            key=_target_key,
        )
        for decision in target_rows:
            alternatives = [
                index for index in range(len(decision.legal_identities))
                if index != decision.chosen_index
            ]
            if not alternatives:
                continue
            offset = derive_attempt_seed(
                study_id, root_id, invocation_seed, proposal_index,
                f"later-alternative:{parent.attempt_index}:{decision.step_index}",
            ) % len(alternatives)
            rotated = alternatives[offset:] + alternatives[:offset]
            for alternative in rotated:
                key = (
                    parent.attempt_index,
                    decision.state_fingerprint,
                    identity_key(decision.legal_identities[alternative]),
                )
                if key not in used:
                    used.add(key)
                    return MutationTarget(parent.attempt_index, decision, alternative)
    return None


def _remember_completed(
    portfolio: list[TrialResult],
    result: TrialResult,
    *,
    cap: int,
) -> None:
    if not result.complete:
        return
    portfolio.append(result)
    portfolio[:] = _sort_completed(portfolio)[:cap]


def run_search(
    *,
    arm: str,
    study_id: str,
    root_id: str,
    invocation_seed: int,
    root_legal_identities: Sequence[Mapping[str, Any]],
    attempt_runner: AttemptRunner,
    budget: InvocationBudget | None = None,
    attempt_started_recorder: Callable[[int, int, MutationTarget | None], None] | None = None,
    attempt_result_recorder: Callable[[TrialResult, int, int, MutationTarget | None], None] | None = None,
    completed_portfolio_recorder: Callable[[Sequence[TrialResult]], None] | None = None,
) -> SearchOutcome:
    """Run locked R/J proposals; callbacks must start one actual native attempt."""
    arm = str(arm).upper()
    if arm not in {"R", "J"}:
        raise ValueError("only root-only R and joint J search are implemented")
    if len(root_legal_identities) < 2:
        raise ValueError("search only applies to a genuine native multi-choice root")
    budget = budget or InvocationBudget()
    root_cap = R_ROOT_TRIAL_CAP if arm == "R" else J_ROOT_TRIAL_CAP
    schedule = root_action_schedule(
        len(root_legal_identities),
        study_id=study_id,
        root_id=root_id,
        invocation_seed=invocation_seed,
        trial_count=root_cap,
    )
    portfolio: list[TrialResult] = []
    completed_count = 0

    for root_index in schedule:
        if not budget.can_start_attempt():
            break
        try:
            with budget.attempt() as attempt_index:
                if attempt_started_recorder is not None:
                    attempt_started_recorder(attempt_index, root_index, None)
                result = attempt_runner(attempt_index, root_index, None)
                if result.attempt_index != attempt_index:
                    raise ValueError("native attempt result index differs from the charged attempt")
        except SearchBudgetExhausted:
            break
        if attempt_result_recorder is not None:
            attempt_result_recorder(result, root_index, attempt_index, None)
        if result.complete:
            completed_count += 1
            _remember_completed(portfolio, result, cap=(R_ROOT_TRIAL_CAP if arm == "R" else J_COMPLETED_PORTFOLIO_CAP))
            if completed_portfolio_recorder is not None:
                completed_portfolio_recorder(tuple(portfolio))

    mutation_started = 0
    if arm == "J":
        used_mutations: set[tuple[int, str, str]] = set()
        delivered = False
        for proposal_index in range(1, J_LATER_MUTATION_TRIAL_CAP + 1):
            target = _choose_mutation_target(
                portfolio, used_mutations,
                study_id=study_id, root_id=root_id,
                invocation_seed=invocation_seed, proposal_index=proposal_index,
            )
            if target is None:
                break
            if not budget.can_start_attempt():
                break
            try:
                with budget.attempt() as attempt_index:
                    mutation_started += 1
                    delivered = True
                    if attempt_started_recorder is not None:
                        attempt_started_recorder(attempt_index, -1, target)
                    result = attempt_runner(attempt_index, -1, target)
                    if result.attempt_index != attempt_index:
                        raise ValueError("mutation attempt result index differs from its charged attempt")
            except SearchBudgetExhausted:
                break
            if attempt_result_recorder is not None:
                attempt_result_recorder(result, -1, attempt_index, target)
            if result.complete:
                completed_count += 1
                _remember_completed(portfolio, result, cap=(R_ROOT_TRIAL_CAP if arm == "R" else J_COMPLETED_PORTFOLIO_CAP))
                if completed_portfolio_recorder is not None:
                    completed_portfolio_recorder(tuple(portfolio))
        status = "MUTATION_ATTEMPTED" if delivered else "TREATMENT_NOT_DELIVERED"

    else:
        status = "ROOT_ONLY_COMPLETED" if portfolio else "ROOT_ONLY_NO_COMPLETE_TRIAL"

    best = _sort_completed(portfolio)[0] if portfolio else None
    return SearchOutcome(
        arm=arm,
        selected_root_identity=best.root_identity if best else None,
        best_completed_attempt_index=best.attempt_index if best else None,
        status=status,
        started_attempts=budget.started_attempts,
        completed_trajectories=completed_count,
        cpu_seconds=budget.cpu_seconds,
        attempt_cpu_seconds=tuple(budget.attempt_cpu_seconds),
        stop_reason=budget.stop_reason,
        mutation_attempts_started=mutation_started,
    )


def elite_candidate(
    base: TrialResult,
    joint_trajectories: Sequence[TrialResult],
) -> TrialResult | None:
    """Return only a completed J trajectory strictly better than the same-owner base."""
    if not base.complete:
        return None
    candidates = _sort_completed(joint_trajectories)
    for candidate in candidates:
        if candidate.rank[:2] > base.rank[:2]:
            return candidate
    return None


def immutable_attempt_receipt_path(root_dir: Path, attempt_key: str) -> Path:
    if not attempt_key or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for character in attempt_key):
        raise ValueError("attempt key must be a path-safe immutable identifier")
    return root_dir / f"{attempt_key}.json"


def write_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    """Create receipts exactly once; never overwrite an existing attempt or failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (canonical(value) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError(f"immutable attempt receipt already exists: {path.name}")
        return
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except FileExistsError as exc:
        raise FileExistsError(f"immutable attempt receipt already exists: {path.name}") from exc
