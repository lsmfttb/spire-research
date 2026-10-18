#!/usr/bin/env python3
"""Bounded fixed-action integration check for Issue 60's history optimizer.

Uses the pinned STSRL executor and native simulator, replays the retained
owner-662496 action ordinals without Search or training, and compares the
optimized production context at every MAP/REWARDS choice with the pinned
original builder and retained actor input bytes.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import math
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


EXPECTED_STSRL_COMMIT = "7d172f7c24c9d29429b6946ba44f6b59760b0d96"
EXPECTED_NATIVE_COMMIT = "0990ef48d51cd48444a707839d65226d52c1d73e"
EXPECTED_NATIVE_BINARY_SHA256 = "68e9df911849f3f406d793c4419fddae7522f7e66c8a46ee6c40d3b6e417ea73"
SUPPORTED_SCREENS = frozenset({"MAP_SCREEN", "REWARDS"})
DEFAULT_STSRL_SOURCE = "/home/lsmft/stsrl-spikes/STSRL-issue59-source-7d172f7c"
DEFAULT_NATIVE_BUILD = "/home/lsmft/stsrl-spikes/issue60-runtime/build-issue60"


def _git_head(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _action_key(action: Any) -> tuple[Any, ...]:
    raw = dict(action.raw)
    return (
        raw.get("scope"), getattr(action, "kind", raw.get("kind")),
        raw.get("idx1"), raw.get("idx2"), raw.get("idx3"),
    )


def _public_action_key(action: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(action.get(key) for key in ("scope", "kind", "idx1", "idx2", "idx3"))


def _verify_concurrent_metric_append(writer_type: Any, path: Path, workers: int = 4, rows_per_worker: int = 25) -> None:
    """Exercise the same append writer from forked workers and check framing."""
    writer = writer_type.__new__(writer_type)
    writer.metrics_path = path
    children: list[int] = []
    for worker in range(workers):
        pid = os.fork()
        if pid == 0:
            try:
                for ordinal in range(rows_per_worker):
                    writer._append_metric({
                        "schema_id": "issue60-metric-append-concurrency-probe-v1",
                        "worker": worker,
                        "ordinal": ordinal,
                        "pid": os.getpid(),
                    })
            except BaseException:
                os._exit(1)
            os._exit(0)
        children.append(pid)
    errors = []
    for pid in children:
        waited, status = os.waitpid(pid, 0)
        if waited != pid or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
            errors.append({"pid": pid, "status": status})
    if errors:
        raise RuntimeError(f"forked metric writers failed: {errors}")
    with path.open("r", encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    expected = {(worker, ordinal) for worker in range(workers) for ordinal in range(rows_per_worker)}
    observed = {(row.get("worker"), row.get("ordinal")) for row in records}
    if len(records) != workers * rows_per_worker or observed != expected:
        raise RuntimeError("concurrent metric appends lost, duplicated, or malformed a JSONL record")


def main() -> int:
    study = Path(__file__).resolve().parent
    repository = study.parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--stsr-source", type=Path, default=Path(os.environ.get("ISSUE60_STSRL_SOURCE", DEFAULT_STSRL_SOURCE)))
    parser.add_argument("--native-build", type=Path, default=Path(os.environ.get("ISSUE60_NATIVE_BUILD", DEFAULT_NATIVE_BUILD)))
    parser.add_argument("--max-replay-cpu-seconds", type=float, default=120.0)
    parser.add_argument("--repository-commit", default=os.environ.get("ISSUE60_REPOSITORY_COMMIT"))
    args = parser.parse_args()
    if not math.isfinite(args.max_replay_cpu_seconds) or args.max_replay_cpu_seconds <= 0:
        raise ValueError("max replay CPU seconds must be finite and positive")

    stsr_source = args.stsr_source.resolve()
    native_build = args.native_build.resolve()
    sys.path.insert(0, str(stsr_source / "src"))
    sys.path.insert(0, str(native_build))

    from sts_combat_rl.sim import controlled_run, public_run_context
    from sts_combat_rl.sim.action_space import ActionSpaceConfig
    from sts_combat_rl.sim.controller_contract import ControllerDecision, ControllerProvenance
    from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
    import slaythespire
    from adapter_v47 import prepare_policy_input
    from bootstrap_v47 import with_public_history
    from public_history_execution import PublicHistoryExecution, install

    stsr_commit = _git_head(stsr_source)
    native_source = Path("/home/lsmft/stsrl-spikes/sts_lightspeed-issue59")
    native_commit = _git_head(native_source)
    native_binary = Path(slaythespire.__file__).resolve()
    native_sha = _sha256(native_binary)
    if stsr_commit != EXPECTED_STSRL_COMMIT or native_commit != EXPECTED_NATIVE_COMMIT:
        raise RuntimeError(f"pinned checkout mismatch: STSRL={stsr_commit}, Native={native_commit}")
    if native_sha != EXPECTED_NATIVE_BINARY_SHA256:
        raise RuntimeError(f"pinned Native extension hash mismatch: {native_sha}")

    run_dir = study / "evidence/runs/training/block2/batch-04/seed-662496/attempt-0001"
    run_path = run_dir / "run.json"
    decisions_path = run_dir / "decisions.jsonl.gz"
    with gzip.open(decisions_path, "rt", encoding="utf-8") as stream:
        decisions = [json.loads(line) for line in stream if line.strip()]
    supported_records: dict[int, dict[str, Any]] = {}
    for row in decisions:
        serialized = row.get("issue60_public_input")
        if not isinstance(serialized, str):
            continue
        value = json.loads(serialized)
        public_state = value.get("public_state", {})
        screen = public_state.get("screen") if isinstance(public_state, Mapping) else None
        if screen not in SUPPORTED_SCREENS:
            continue
        history = public_state.get("history", [])
        if not isinstance(history, list):
            raise RuntimeError("retained supported actor input has malformed history")
        index = len(history)
        if index in supported_records:
            raise RuntimeError(f"duplicate retained supported input at history length {index}")
        supported_records[index] = {"row": row, "value": value, "history": history}
    if not supported_records:
        raise RuntimeError("no retained supported actor inputs were found")

    latest_index = max(supported_records)
    retained_history = supported_records[latest_index]["history"]
    if any(entry.get("chosen_ordinal") is None for entry in retained_history):
        raise RuntimeError("retained actor history has a missing chosen ordinal")
    latest_row = supported_records[latest_index]["row"]
    latest_value = supported_records[latest_index]["value"]
    legal_options = latest_value.get("legal_options", [])
    selected_index = latest_row.get("issue60_selected_index")
    if isinstance(selected_index, bool) or not isinstance(selected_index, int) or not 0 <= selected_index < len(legal_options):
        raise RuntimeError("latest retained supported choice index is invalid")
    extension_action = legal_options[selected_index].get("action", {})
    if not isinstance(extension_action, Mapping):
        raise RuntimeError("latest retained supported action identity is malformed")

    # Preserve the original helper before the installer replaces module-level
    # references used by execute_controlled_run.
    original_build = controlled_run.build_public_run_context
    diagnostic_stamp = time.time_ns()
    metrics_path = study / "evidence/diagnostics" / f"public-history-execution-metrics-{diagnostic_stamp}.jsonl"
    concurrency_probe_path = study / "evidence/diagnostics" / f"public-history-concurrency-probe-{diagnostic_stamp}.jsonl"
    install(metrics_path=metrics_path, controlled_run_module=controlled_run, public_context_module=public_run_context)
    _verify_concurrent_metric_append(PublicHistoryExecution, concurrency_probe_path)
    cpu_start = time.process_time()
    deadline = args.max_replay_cpu_seconds
    compared_contexts = 0
    compared_actor_inputs = 0

    class FixedOrdinalReplay:
        provenance = ControllerProvenance(kind="diagnostic_replay", name="owner_662496_fixed_ordinals")

        def reset_for_run(self, seed: int | None) -> None:
            if seed != 662496:
                raise RuntimeError(f"unexpected replay seed {seed}")

        def select_action(self, adapter: Any, snapshot: Any, actions: Sequence[Any], context: Any, step_index: int) -> Any:
            nonlocal compared_contexts, compared_actor_inputs
            if time.process_time() - cpu_start > deadline:
                raise RuntimeError("bounded optimized fixed-action replay CPU limit reached")

            if context.screen_state in SUPPORTED_SCREENS:
                public_context = context.public_run_context
                if "history" not in public_context:
                    raise RuntimeError("supported production consumer received no public history")
                history = public_context["history"]
                projection = public_run_context.read_native_public_projection(adapter, snapshot)
                reference = original_build(
                    snapshot.raw, actions, projection=projection,
                    history=history, include_candidates=True,
                )
                if _canonical(public_context) != _canonical(reference):
                    raise RuntimeError(f"pinned public context differs on supported step {step_index}")
                compared_contexts += 1

                record = supported_records.get(step_index)
                if record is not None:
                    native_projection = dict(adapter._sim.public_projection_v3())
                    native_actions = list(adapter._sim.legal_actions())
                    prepared = prepare_policy_input(native_projection, native_actions)
                    actor_input = with_public_history(prepared.serialized_input, history, public_context)
                    expected_actor_input = record["row"]["issue60_public_input"]
                    if actor_input != expected_actor_input:
                        raise RuntimeError(f"retained actor input differs at supported step {step_index}")
                    compared_actor_inputs += 1

            if step_index < len(retained_history):
                ordinal = retained_history[step_index]["chosen_ordinal"]
            else:
                identity = _public_action_key(extension_action)
                matches = [index for index, action in enumerate(actions) if _action_key(action) == identity]
                if len(matches) != 1:
                    raise RuntimeError(f"retained latest action maps to {len(matches)} native legal actions")
                ordinal = matches[0]
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 0 <= ordinal < len(actions):
                raise RuntimeError(f"illegal retained action ordinal {ordinal!r} at step {step_index}")
            return ControllerDecision(
                selected_index=ordinal,
                provenance=self.provenance,
                reason="fixed retained ordinal replay",
                metadata={"controller_role": "fixed_retained_replay"},
            )

    replay_controller = FixedOrdinalReplay()
    adapter = LightSpeedAdapter(seed=662496, ascension=20, player_class="IRONCLAD")
    try:
        run = controlled_run.execute_controlled_run(
            adapter, replay_controller, seed=662496,
            max_steps=len(retained_history) + 1,
            action_space=ActionSpaceConfig.include_all(),
            retain_steps=False,
        )
        if run.problems:
            raise RuntimeError(f"pinned executor replay incomplete: {run.problems[:3]}")
        if len(run.public_history) != len(retained_history) + 1:
            raise RuntimeError(f"expected {len(retained_history) + 1} history entries, got {len(run.public_history)}")
        if compared_actor_inputs != len(supported_records):
            raise RuntimeError(f"compared {compared_actor_inputs}/{len(supported_records)} retained actor inputs")

        terminal_snapshot = adapter._snapshot()
        terminal_projection = public_run_context.read_native_public_projection(adapter, terminal_snapshot)
        terminal_reference = original_build(
            run.final_raw, (), projection=terminal_projection,
            history=run.public_history, include_candidates=False,
        )
        if _canonical(run.public_run_context) != _canonical(terminal_reference):
            raise RuntimeError("terminal public context differs from pinned original builder")

        execution = getattr(controlled_run, "_issue60_public_history_execution")
        record = execution.stats
        if record["previous_entries_recursively_revalidated"] != 0:
            raise RuntimeError("optimizer unexpectedly recursively revalidated prior entries")
        report = {
            "schema_id": "issue60-public-history-execution-parity-v1",
            "repository_head": args.repository_commit or _git_head(repository),
            "source_pins": {
                "stsr_commit": stsr_commit,
                "native_commit": native_commit,
                "native_binary_sha256": native_sha,
            },
            "input_artifacts": {
                "run_json_sha256": _sha256(run_path),
                "decisions_jsonl_gz_sha256": _sha256(decisions_path),
                "retained_history_entries": len(retained_history),
                "supported_actor_inputs": len(supported_records),
            },
            "verification": {
                "path": "pinned execute_controlled_run with study-local optimizer and Native adapter; no Search and no training/update",
                "steps_completed": len(run.public_history),
                "problems": run.problems,
                "supported_contexts_equal_original_builder": compared_contexts,
                "retained_actor_input_bytes_equal": compared_actor_inputs,
                "retained_compact_actor_history_equal": True,
                "terminal_context_equal_original_builder": True,
                "prior_entries_recursively_revalidated": record["previous_entries_recursively_revalidated"],
                "full_history_materializations": record["supported_full_history_materializations"],
                "materialized_history_entries": record["supported_materialized_entries"],
                "terminal_materialized_entries": record["terminal_materialized_entries"],
                "cpu_seconds": max(0.0, time.process_time() - cpu_start),
                "metrics_path": str(metrics_path.relative_to(repository)).replace("\\", "/"),
                "concurrency_probe_records": 100,
                "concurrency_probe_path": str(concurrency_probe_path.relative_to(repository)).replace("\\", "/"),
            },
            "limitations": [
                "Fixed-action replay validates executor and public-history parity; it bypasses Battle Search and PPO updates.",
                "This bounded diagnostic does not repeat the historical 8,538.89-second study run or make a causal claim about Search action quality.",
            ],
        }
        report_path = study / "evidence/diagnostics" / f"public-history-execution-parity-{diagnostic_stamp}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, sort_keys=True, indent=2))
    finally:
        adapter.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
