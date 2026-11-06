from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import torch


DATA_ROOT = Path(
    "/mnt/d/DeadlyCatCoding/stsrl-data/issue85/artifacts/performance/"
    "20261106-actor-cache-equivalence-v6"
)
BASELINE_ROOT = DATA_ROOT / "baseline"
CANDIDATE_ROOT = DATA_ROOT / "candidate"
OUTPUT = DATA_ROOT / "equivalence-comparison-v3.json"
ARMS = ("B", "C", "D", "E", "F")
RUNNER_COMMIT = "5cd70c00d03f1104728efdc4b66c764592e5acf8"
RUNNER_LOCK_SHA256 = "a25c25cd4144cbe362c7c241460e9d71f4554627c9bb129407ef69ec8366404a"
ISSUE84_COMMIT = "556128df66797bf979d36b56b6920ac88bab5b35"
ISSUE84_LOCK_SHA256 = "58ce736dbab1ac750e6e5771f644391610930ec4e9a87876f013bfd28838ef52"
FIELDS = (
    "inputs",
    "encoded_features",
    "distributions",
    "owner_losses",
    "total_loss",
    "gradients_before_clip",
    "gradients_after_clip",
    "grad_norm_before_clip",
    "model_state",
    "optimizer_state",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compare_value(left: Any, right: Any, path: str, mismatches: list[dict[str, Any]]) -> None:
    if torch.is_tensor(left) and torch.is_tensor(right):
        if left.shape != right.shape or left.dtype != right.dtype:
            mismatches.append({"path": path, "kind": "tensor_shape_or_dtype", "left": [str(left.dtype), list(left.shape)], "right": [str(right.dtype), list(right.shape)]})
            return
        if torch.equal(left, right):
            return
        if left.numel() and (left.is_floating_point() or left.is_complex()):
            delta = (left - right).abs()
            finite = delta[torch.isfinite(delta)]
            max_abs = float(finite.max().item()) if finite.numel() else None
            changed = int(torch.count_nonzero(left != right).item())
        else:
            max_abs = None
            changed = int(torch.count_nonzero(left != right).item())
        mismatches.append({"path": path, "kind": "tensor_values", "changed_elements": changed, "max_abs_difference": max_abs})
        return
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            mismatches.append({"path": path, "kind": "mapping_keys", "left_only": sorted(map(str, set(left) - set(right))), "right_only": sorted(map(str, set(right) - set(left)))})
            return
        for key in sorted(left, key=str):
            compare_value(left[key], right[key], f"{path}.{key}", mismatches)
        return
    if isinstance(left, (list, tuple)) and isinstance(right, type(left)):
        if len(left) != len(right):
            mismatches.append({"path": path, "kind": "sequence_length", "left": len(left), "right": len(right)})
            return
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            compare_value(a, b, f"{path}[{index}]", mismatches)
        return
    if isinstance(left, (float, int, bool)) and isinstance(right, (float, int, bool)):
        if left == right:
            return
        delta = abs(float(left) - float(right))
        mismatches.append({"path": path, "kind": "scalar_value", "left": left, "right": right, "absolute_difference": delta})
        return
    if left != right:
        mismatches.append({"path": path, "kind": "value", "left": repr(left), "right": repr(right)})


def metrics_path(root: Path, arm: str) -> Path:
    return root / arm / "metrics.json"


def snapshots_path(root: Path, arm: str) -> Path:
    return root / arm / "snapshots.pt"


def main() -> None:
    if OUTPUT.exists():
        raise SystemExit(f"refusing to overwrite comparison evidence: {OUTPUT}")
    comparisons: dict[str, Any] = {}
    baseline_projected_64 = 0.0
    candidate_projected_64 = 0.0

    for arm in ARMS:
        bmetrics_path = metrics_path(BASELINE_ROOT, arm)
        cmetrics_path = metrics_path(CANDIDATE_ROOT, arm)
        bsnap_path = snapshots_path(BASELINE_ROOT, arm)
        csnap_path = snapshots_path(CANDIDATE_ROOT, arm)
        for path in (bmetrics_path, cmetrics_path, bsnap_path, csnap_path):
            if not path.is_file():
                raise SystemExit(f"missing required immutable measurement: {path}")
        bmetrics = json.loads(bmetrics_path.read_text(encoding="utf-8"))
        cmetrics = json.loads(cmetrics_path.read_text(encoding="utf-8"))
        for mode, report, snapshot_path in (
            ("baseline", bmetrics, bsnap_path),
            ("candidate", cmetrics, csnap_path),
        ):
            if report.get("runner_commit") != RUNNER_COMMIT or report.get("runner_source_lock_sha256") != RUNNER_LOCK_SHA256:
                raise SystemExit(f"{mode} {arm} report is not bound to the locked Issue 85 runner/source lock")
            if report.get("issue84_commit") != ISSUE84_COMMIT or report.get("issue84_source_lock_sha256") != ISSUE84_LOCK_SHA256:
                raise SystemExit(f"{mode} {arm} report is not bound to the locked Issue 84 gradient source")
            expected_mode = "uncached_baseline" if mode == "baseline" else "candidate"
            if report.get("arm") != arm or report.get("mode") != expected_mode:
                raise SystemExit(f"{mode} {arm} report has the wrong arm/mode identity")
            if report.get("snapshot_sha256") != sha256(snapshot_path) or report.get("snapshot_bytes") != snapshot_path.stat().st_size:
                raise SystemExit(f"{mode} {arm} snapshot does not match its immutable report hash/size")
        paired_fields = (
            "pilot_package_commit",
            "input_receipts",
            "actor_checkpoint_sha256",
            "optimizer_checkpoint_sha256",
            "actor_model_digest_start",
            "owner_count",
            "decision_count",
        )
        provenance_mismatches = [field for field in paired_fields if bmetrics.get(field) != cmetrics.get(field)]
        if provenance_mismatches:
            raise SystemExit(f"baseline/candidate provenance differs for {arm}: {provenance_mismatches}")
        baseline = torch.load(bsnap_path, map_location="cpu", weights_only=True)
        candidate = torch.load(csnap_path, map_location="cpu", weights_only=True)
        if baseline.get("arm") != arm or candidate.get("arm") != arm:
            raise SystemExit(f"snapshot arm identity mismatch for {arm}")
        bsteps = baseline["steps"]
        csteps = candidate["steps"]
        if len(bsteps) != 2 or len(csteps) != 2:
            raise SystemExit(f"expected two sequential updates for {arm}")

        arm_results = []
        for index, (bstep, cstep) in enumerate(zip(bsteps, csteps, strict=True)):
            if index == 0 and arm == "C" and len(cstep["encoded_features"]) == 2 * len(bstep["encoded_features"]):
                half = len(bstep["encoded_features"])
                capture_mismatches: list[dict[str, Any]] = []
                compare_value(cstep["encoded_features"][:half], cstep["encoded_features"][half:], "duplicate_encoder_capture", capture_mismatches)
                if capture_mismatches:
                    raise SystemExit("C first-step encoder capture duplication was not identical")
                # The initial C harness recorded cache preparation and use of the
                # same tensor separately. Keep raw snapshots unchanged; compare
                # one verified copy per decision here.
                cstep = dict(cstep)
                cstep["encoded_features"] = cstep["encoded_features"][half:]
                capture_note = "raw snapshot contains two bitwise-identical ordered captures of each cached tensor; one copy selected for equivalence only"
            else:
                capture_note = None

            mismatches: list[dict[str, Any]] = []
            for field in FIELDS:
                if field not in bstep or field not in cstep:
                    mismatches.append({"path": field, "kind": "missing_field"})
                    continue
                compare_value(bstep[field], cstep[field], field, mismatches)
            digest_status = "unavailable_in_uncached_snapshot" if "model_digest_after_step" not in bstep else "bitwise_equal" if bstep.get("model_digest_after_step") == cstep.get("model_digest_after_step") else "different"
            if "model_digest_after_step" in bstep and "model_digest_after_step" in cstep:
                compare_value(bstep["model_digest_after_step"], cstep["model_digest_after_step"], "model_digest_after_step", mismatches)
            arm_results.append({
                "step": index + 1,
                "bitwise_equal": not mismatches,
                "comparison_fields": list(FIELDS),
                "mismatch_count": len(mismatches),
                "mismatches": mismatches[:50],
                "candidate_encoded_capture_note": capture_note,
                "snapshot_model_digest_status": digest_status,
            })

        btimes = [float(step["timings"]["wall_seconds"]) for step in bmetrics["steps"]]
        ctimes = [float(step["timings"]["wall_seconds"]) for step in cmetrics["steps"]]
        if len(btimes) != 2 or len(ctimes) != 2:
            raise SystemExit(f"measurement JSON lacks two per-step wall times for {arm}")
        b64 = btimes[0] + 63.0 * btimes[1]
        c64 = ctimes[0] + 63.0 * ctimes[1]
        baseline_projected_64 += b64
        candidate_projected_64 += c64
        comparisons[arm] = {
            "steps": arm_results,
            "all_two_step_fields_bitwise_equal": all(row["bitwise_equal"] for row in arm_results),
            "baseline_step_wall_seconds": btimes,
            "candidate_step_wall_seconds": ctimes,
            "two_step_end_to_end_speedup": sum(btimes) / sum(ctimes),
            "first_step_speedup_including_cache_build": btimes[0] / ctimes[0],
            "repeated_step_speedup": btimes[1] / ctimes[1],
            "conditional_64_update_block_baseline_seconds": b64,
            "conditional_64_update_block_candidate_seconds": c64,
            "conditional_block_speedup": b64 / c64,
            "candidate_cache": cmetrics.get("input_cache"),
            "baseline_max_rss_kib_after_updates": bmetrics.get("ru_maxrss_after_updates_kib"),
            "candidate_max_rss_kib_after_updates": cmetrics.get("ru_maxrss_after_updates_kib"),
            "baseline_rss_bytes_before_updates": bmetrics.get("rss_bytes_before_updates"),
            "candidate_rss_bytes_before_updates": cmetrics.get("rss_bytes_before_updates"),
            "baseline_snapshot_sha256": sha256(bsnap_path),
            "candidate_snapshot_sha256": sha256(csnap_path),
            "baseline_metrics_sha256": sha256(bmetrics_path),
            "candidate_metrics_sha256": sha256(cmetrics_path),
        }

    source_update_seconds = 484571.603271408
    conditional_ratio = candidate_projected_64 / baseline_projected_64
    candidate_update_seconds = source_update_seconds * conditional_ratio
    forecast = {
        "source_forecast_actor_updates_seconds": source_update_seconds,
        "source_forecast_actor_updates_hours": source_update_seconds / 3600.0,
        "measured_16_owner_arms_equal_weight_64_step_baseline_seconds": baseline_projected_64,
        "measured_16_owner_arms_equal_weight_64_step_candidate_seconds": candidate_projected_64,
        "candidate_to_baseline_actor_update_ratio": conditional_ratio,
        "conditional_actor_update_projection_seconds": candidate_update_seconds,
        "conditional_actor_update_projection_hours": candidate_update_seconds / 3600.0,
        "unchanged_formal_game_group_projection_hours": (39294.678576831844 + 19373.453460936144) / 3600.0,
        "conditional_formal_core_projection_hours": candidate_update_seconds / 3600.0 + (39294.678576831844 + 19373.453460936144) / 3600.0,
        "conditional_pilot_plus_formal_core_hours": candidate_update_seconds / 3600.0 + (39294.678576831844 + 19373.453460936144 + 32643.740648911004) / 3600.0,
        "limitations": [
            "Projection applies the measured 16-owner two-update ratio to the prior 32-owner formal actor-update component, preserving its linear batch-scaling assumption; 32-owner throughput was not measured.",
            "Each 64-step block is modeled as one cache build plus 63 repeats; the two observed steps do not validate all 64-step thermal, allocator or long-tail behavior.",
            "Formal training/evaluation games, retry/restart variance, build/restore/queue time, frozen bootstrap analysis and human review remain as in the prior partial forecast; this is not an all-in estimate or cap.",
            "A conditional projection does not authorize or start formal work and is not policy-efficacy evidence.",
        ],
    }
    result = {
        "schema_id": "issue85-offline-cache-equivalence-comparison-v1",
        "scope": "existing nonformal Pilot training traces only; no game/replay/formal run",
        "comparison": "uncached source path versus parameter-independent per-block input cache",
        "comparison_rule": "bitwise exact for every listed tensor and scalar; no tolerance applied",
        "comparison_v1_disposition": "superseded: v1 incorrectly treated a per-step model digest omitted from the original B/C snapshots as a mismatch; this version compares full model-state tensors directly and reports digest availability separately",
        "arms": comparisons,
        "all_arms_bitwise_equivalent": all(x["all_two_step_fields_bitwise_equal"] for x in comparisons.values()),
        "conditional_cost_projection": forecast,
    }
    with OUTPUT.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"output": str(OUTPUT), "all_arms_bitwise_equivalent": result["all_arms_bitwise_equivalent"], "conditional_actor_update_hours": forecast["conditional_actor_update_projection_hours"], "conditional_formal_core_hours": forecast["conditional_formal_core_projection_hours"]}, sort_keys=True))
    if not result["all_arms_bitwise_equivalent"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
