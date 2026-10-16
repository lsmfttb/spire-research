"""Pure terminal-state mapping for one study game-attempt status record."""
from __future__ import annotations

from typing import Any, Mapping


_IDENTITY_FIELDS = (
    "schema_id", "game_seed", "mode", "block", "stage_key", "arm",
    "runtime_policy_seed", "model_sha256", "config_sha256",
)


def finalized_attempt_status(started: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
    """Return the terminal status marker corresponding to a completed run record.

    A process crash before a run summary is written leaves the start marker at
    RUNNING. Normal returns, including incomplete/capped games, replace that
    marker with the terminal state recorded in run.json.
    """
    if started.get("status") not in {"RUNNING", "COMPLETE", "INCOMPLETE"}:
        raise ValueError("attempt start record has an unknown lifecycle state")
    if summary.get("status") not in {"COMPLETE", "INCOMPLETE"}:
        raise ValueError("run summary must have a terminal lifecycle state")
    for field in _IDENTITY_FIELDS:
        if started.get(field) != summary.get(field):
            raise ValueError(f"attempt status and run summary disagree on {field}")
    if started.get("status") in {"COMPLETE", "INCOMPLETE"} and started.get("status") != summary.get("status"):
        raise ValueError("attempt status already records a different terminal state")

    terminal = dict(started)
    terminal["status"] = summary["status"]
    terminal["process_cpu_seconds"] = summary["process_cpu_seconds"]
    terminal["completed_cpu_seconds"] = summary["completed_cpu_seconds"]
    failure = summary.get("failure")
    if failure is None:
        terminal.pop("failure", None)
    else:
        terminal["failure"] = failure
    return terminal
