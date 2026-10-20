from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        raise ValueError("cannot calculate a percentile from an empty sample")
    position = (len(sorted_values) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def paired_bootstrap_ci(
    differences: list[float], *, resamples: int, seed: int
) -> dict[str, float | int]:
    if len(differences) != 64:
        raise ValueError(f"expected 64 paired owner differences, got {len(differences)}")
    rng = random.Random(seed)
    n = len(differences)
    means = []
    for _ in range(resamples):
        means.append(sum(differences[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return {
        "paired_mean_difference": sum(differences) / n,
        "ci95_lower": percentile(means, 0.025),
        "ci95_upper": percentile(means, 0.975),
        "bootstrap_resamples": resamples,
        "bootstrap_seed": seed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize the frozen Issue 64 paired owner endpoints")
    parser.add_argument("--output", type=Path, required=True, help="Issue 64 artifact directory")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    production = json.loads((output / "production-complete.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "study-manifest.json").read_text(encoding="utf-8"))
    preflight = json.loads((output / "preflight-decision.json").read_text(encoding="utf-8"))
    if manifest["runner_sha256"] != preflight.get("runner_sha256"):
        raise SystemExit("production runner hash does not match the frozen preflight runner")
    config = manifest["config"]
    expected_seeds = config["owners"]["production"]
    owners = sorted(production["owners"], key=lambda row: row["owner_seed"])
    observed_seeds = [int(row["owner_seed"]) for row in owners]
    if observed_seeds != expected_seeds or len(owners) != 64:
        raise SystemExit("production owners do not match the frozen 64-seed list")
    if any(set(row["arms"]) != {"B0", "B1", "B2"} for row in owners):
        raise SystemExit("every owner must have a native terminal result for B0, B1 and B2")

    config_sha = hashlib.sha256(canonical(config).encode()).hexdigest()
    boss_reached: dict[str, list[int]] = {arm: [] for arm in ("B0", "B1", "B2")}
    for owner in owners:
        attempts = sorted(
            (
                path
                for path in (output / "production" / f"seed-{owner['owner_seed']}").glob("owner-attempt-*")
                if path.is_dir()
            ),
            key=lambda path: int(path.name.rsplit("-", 1)[1]),
        )
        if not attempts:
            raise SystemExit(f"no durable arm artifacts for owner {owner['owner_seed']}")
        latest = attempts[-1]
        for arm in ("B0", "B1", "B2"):
            arm_path = latest / arm / "arm-result.json"
            if not arm_path.is_file():
                raise SystemExit(f"missing {arm} durable arm result for owner {owner['owner_seed']}")
            arm_result = json.loads(arm_path.read_text(encoding="utf-8"))
            if arm_result.get("config_sha256") != config_sha:
                raise SystemExit(f"{arm} arm config mismatch for owner {owner['owner_seed']}")
            if arm_result.get("terminal") != owner["arms"][arm]:
                raise SystemExit(f"{arm} terminal mismatch for owner {owner['owner_seed']}")
            boss_reached[arm].append(int(bool(arm_result.get("boss_reached", False))))

    values = {
        arm: {
            "terminal_floor": [int(row["arms"][arm]["terminal_floor"]) for row in owners],
            "heart_victory": [int(bool(row["arms"][arm]["heart_victory"])) for row in owners],
            "act2_plus": [int(bool(row["arms"][arm]["act2_plus"])) for row in owners],
            "act3_plus": [int(bool(row["arms"][arm]["act3_plus"])) for row in owners],
            "boss_reached": boss_reached[arm],
        }
        for arm in ("B0", "B1", "B2")
    }
    resamples = 20_000
    bootstrap_seed = 64_064
    contrasts = {}
    metric_names = list(values["B2"])
    for comparator in ("B1", "B0"):
        contrast = f"B2_minus_{comparator}"
        contrasts[contrast] = {}
        for metric_index, metric in enumerate(metric_names):
            differences = [
                float(left - right)
                for left, right in zip(values["B2"][metric], values[comparator][metric], strict=True)
            ]
            contrasts[contrast][metric] = paired_bootstrap_ci(
                differences,
                resamples=resamples,
                seed=bootstrap_seed + (0 if comparator == "B1" else 1) + metric_index * 10,
            )

    arms = {}
    for arm, metrics in values.items():
        arms[arm] = {
            metric: {
                "mean": sum(numbers) / len(numbers),
                "count": sum(numbers) if metric != "terminal_floor" else None,
                "owners": len(numbers),
            }
            for metric, numbers in metrics.items()
        }

    floor_b2_b1 = contrasts["B2_minus_B1"]["terminal_floor"]
    floor_b2_b0 = contrasts["B2_minus_B0"]["terminal_floor"]
    floor_gate = (
        floor_b2_b1["ci95_lower"] > 0
        and floor_b2_b0["ci95_lower"] > 0
        and floor_b2_b0["paired_mean_difference"] >= 1.0
    )
    summary = {
        "schema": "issue64-paired-endpoint-summary-v1",
        "study_id": config["study_id"],
        "config_sha256": config_sha,
        "production_runner_sha256": manifest["runner_sha256"],
        "summary_script_sha256": sha256_file(Path(__file__).resolve()),
        "owner_count": len(owners),
        "interval": "paired owner bootstrap percentile 95% CI",
        "bootstrap_resamples": resamples,
        "bootstrap_seed_base": bootstrap_seed,
        "arm_endpoints": arms,
        "paired_contrasts": contrasts,
        "frozen_floor_gate_passed": floor_gate,
        "act2_plus_noninferiority": {
            "paired_difference_ci": {
                "B2_minus_B1": contrasts["B2_minus_B1"]["act2_plus"],
                "B2_minus_B0": contrasts["B2_minus_B0"]["act2_plus"],
            },
            "status": "not_decided; Issue 64 does not specify a noninferiority margin",
        },
        "strong_oracle_gate_passed": False,
        "disposition": (
            "No strong-Oracle claim is authorized by this summary because the Act2+ "
            "noninferiority margin remains unspecified for Planner disposition."
        ),
    }
    result_path = output / "analysis-summary.json"
    temp_path = result_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(result_path)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())