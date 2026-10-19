"""Read-only Issue 61 diagnosis of the frozen Issue 60 model checkpoints.

This script verifies model identities, summarizes parameter movement, measures
centered actor scores on retained batch-08 public observations, and computes
single-row autograd probes. It never creates a game, reads private simulator
state, writes artifacts, or calls an optimizer.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
from pathlib import Path
import sys
import time

_CPU_START = time.process_time()
HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE))

import torch
import torch.nn.functional as F

torch.set_num_threads(1)
torch.use_deterministic_algorithms(True)

from encoder import Vocabulary, encode  # noqa: E402
from model import JointChoiceActorCritic  # noqa: E402
from run_study import MODEL_SEEDS, config_sha, model_fingerprint  # noqa: E402

EVIDENCE = HERE / "evidence"
BLOCKS = ("block1", "block2")
GROUPS = (
    "actor[-1].weight",
    "actor[-1].bias",
    "actor[0]",
    "category",
    "entity",
    "map_node",
    "map_message",
    "choice_combine",
    "global_context",
    "value",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _group(name: str) -> str:
    if name == "actor.2.weight":
        return "actor[-1].weight"
    if name == "actor.2.bias":
        return "actor[-1].bias"
    if name.startswith("actor.0."):
        return "actor[0]"
    for prefix in ("category", "entity", "map_node", "map_message",
                   "choice_combine", "global_context", "value"):
        if name == prefix or name.startswith(prefix + "."):
            return prefix
    raise ValueError(f"unclassified parameter: {name}")


def _flat(tensors: list[torch.Tensor]) -> torch.Tensor:
    if not tensors:
        return torch.empty(0, dtype=torch.float64)
    return torch.cat([value.detach().cpu().double().reshape(-1) for value in tensors])


def _delta_summary(initial: list[torch.Tensor], final: list[torch.Tensor]) -> dict[str, float | int | None]:
    before = _flat(initial)
    after = _flat(final)
    delta = after - before
    init_l2 = float(torch.linalg.vector_norm(before))
    final_l2 = float(torch.linalg.vector_norm(after))
    delta_l2 = float(torch.linalg.vector_norm(delta))
    return {
        "parameter_count": int(before.numel()),
        "init_l2": init_l2,
        "final_l2": final_l2,
        "delta_l2": delta_l2,
        "relative_delta": delta_l2 / init_l2 if init_l2 else None,
        "delta_rms": float(torch.sqrt(torch.mean(delta.square()))) if delta.numel() else 0.0,
        "delta_max_abs": float(torch.max(torch.abs(delta))) if delta.numel() else 0.0,
    }


def _read_first_per_game(path: Path, limit: int = 128) -> list[dict]:
    selected: list[dict] = []
    seen: set[int] = set()
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            seed = int(row["game_seed"])
            if seed in seen or row.get("actor_update_mask", 0) <= 0 or len(row.get("p0", ())) < 2:
                continue
            seen.add(seed)
            selected.append(row)
            if len(selected) >= limit:
                break
    return selected


def _score_summary(model: JointChoiceActorCritic, rows: list[dict], vocab: Vocabulary) -> dict:
    rms: list[float] = []
    max_abs: list[float] = []
    pairwise: list[float] = []
    adverse_margin_fraction: list[float] = []
    max_absolute_margin_fraction: list[float] = []
    candidate_counts: dict[str, int] = {}
    model.eval()
    with torch.no_grad():
        for row in rows:
            logits, _ = model(encode(row["serialized_public"], vocab))
            if len(logits) != len(row["p0"]):
                raise ValueError("candidate count changed while decoding retained public input")
            centered = logits - logits.mean()
            n = int(len(logits))
            candidate_counts[str(n)] = candidate_counts.get(str(n), 0) + 1
            rms.append(float(torch.sqrt(torch.mean(centered.double().square()))))
            max_abs.append(float(torch.max(torch.abs(centered))))
            pairwise.append(float(centered.max() - centered.min()))
            p0 = [float(x) for x in row["p0"]]
            best = max(range(n), key=p0.__getitem__)
            alternatives = [i for i in range(n) if i != best]
            if any(p0[i] <= 0.0 or p0[best] <= p0[i] for i in alternatives):
                raise ValueError("expected one p0 winner above positive alternative probabilities")
            margin_fractions = [
                float(logits[i] - logits[best]) / math.log(p0[best] / p0[i])
                for i in alternatives
            ]
            # Positive alternative-minus-winner shift erodes the p0 winner margin.
            # The absolute maximum also captures shifts favoring the winner.
            adverse_margin_fraction.append(max(0.0, max(margin_fractions)))
            max_absolute_margin_fraction.append(max(abs(x) for x in margin_fractions))

    def percentile(values: list[float], p: float) -> float:
        values = sorted(values)
        if not values:
            return 0.0
        position = (len(values) - 1) * p
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return values[lower]
        return values[lower] + (position - lower) * (values[upper] - values[lower])

    return {
        "retained_decision_rows": len(rows),
        "distinct_game_seeds": len({int(row["game_seed"]) for row in rows}),
        "candidate_count_histogram": candidate_counts,
        "centered_residual_rms_mean": sum(rms) / len(rms) if rms else None,
        "centered_residual_rms_median": percentile(rms, 0.5),
        "centered_residual_rms_p95": percentile(rms, 0.95),
        "centered_residual_max_abs_mean": sum(max_abs) / len(max_abs) if max_abs else None,
        "centered_residual_max_abs_max": max(max_abs) if max_abs else None,
        "pairwise_log_odds_shift_median": percentile(pairwise, 0.5),
        "pairwise_log_odds_shift_p95": percentile(pairwise, 0.95),
        "pairwise_log_odds_shift_max": max(pairwise) if pairwise else None,
        "max_adverse_alt_score_shift_over_p0_margin_median": percentile(adverse_margin_fraction, 0.5),
        "max_adverse_alt_score_shift_over_p0_margin_p95": percentile(adverse_margin_fraction, 0.95),
        "max_abs_winner_to_alt_score_shift_over_p0_margin_median": percentile(max_absolute_margin_fraction, 0.5),
        "max_abs_winner_to_alt_score_shift_over_p0_margin_p95": percentile(max_absolute_margin_fraction, 0.95),
    }


def _grad_norm(named: dict[str, torch.Tensor | None], predicate) -> float:
    parts = [value.detach().double().reshape(-1) for name, value in named.items()
             if predicate(name) and value is not None]
    return float(torch.linalg.vector_norm(torch.cat(parts))) if parts else 0.0


def _gradient_probe(model: JointChoiceActorCritic, row: dict, advantage: float,
                    vocab: Vocabulary) -> dict:
    model.zero_grad(set_to_none=True)
    observation = encode(row["serialized_public"], vocab)
    logits, value = model(observation)
    p0 = torch.tensor(row["p0"], dtype=logits.dtype)
    log_probs = F.log_softmax(torch.log(p0) + logits, dim=0)
    ratio = torch.exp(log_probs[int(row["action"])] - float(row["old_log_probability"]))
    clipped = torch.clamp(ratio, 0.8, 1.2)
    policy_loss = -torch.minimum(ratio * advantage, clipped * advantage)
    policy_loss = policy_loss * float(row["actor_update_mask"]) / 128.0
    policy_loss.backward(retain_graph=True)
    policy_grads = {name: None if p.grad is None else p.grad.detach().clone()
                    for name, p in model.named_parameters()}
    model.zero_grad(set_to_none=True)
    critic_loss = 0.5 * (value - float(row["value_target"])).square() / 128.0
    critic_loss.backward()
    critic_grads = {name: None if p.grad is None else p.grad.detach().clone()
                    for name, p in model.named_parameters()}

    policy_shared_norm = _grad_norm(policy_grads, lambda n: not n.startswith(("actor.", "value.")))
    critic_shared_norm = _grad_norm(critic_grads, lambda n: not n.startswith(("actor.", "value.")))
    shared_names = [n for n in policy_grads if not n.startswith(("actor.", "value."))]
    pairs = [(policy_grads[n].detach().double().reshape(-1),
              critic_grads[n].detach().double().reshape(-1))
             for n in shared_names if policy_grads[n] is not None and critic_grads[n] is not None]
    dot = sum(float(torch.dot(a, b)) for a, b in pairs)
    cosine = dot / (policy_shared_norm * critic_shared_norm) if policy_shared_norm and critic_shared_norm else None
    return {
        "game_seed": int(row["game_seed"]),
        "candidate_count": len(row["p0"]),
        "stored_raw_advantage": float(row["advantage"]),
        "probe_advantage": float(advantage),
        "ppo_ratio": float(ratio.detach()),
        "policy_row_loss_gradient_norm": float(torch.linalg.vector_norm(torch.cat([
            x.detach().double().reshape(-1) for x in policy_grads.values() if x is not None
        ]))),
        "policy_actor[-1]_gradient_norm": _grad_norm(policy_grads, lambda n: n.startswith("actor.2.")),
        "policy_actor[0]_gradient_norm": _grad_norm(policy_grads, lambda n: n.startswith("actor.0.")),
        "policy_shared_gradient_norm": policy_shared_norm,
        "critic_0.5_value_head_gradient_norm": _grad_norm(critic_grads, lambda n: n.startswith("value.")),
        "critic_0.5_shared_gradient_norm": critic_shared_norm,
        "policy_vs_0.5_critic_shared_cosine": cosine,
        "optimizer_steps": 0,
    }


def _first_row(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("actor_update_mask", 0) > 0 and row.get("advantage") not in (0, 0.0):
                return row
    raise ValueError(f"no actor row in {path}")


def _final_active_row(model: JointChoiceActorCritic, path: Path, vocab: Vocabulary) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as stream, torch.no_grad():
        for line in stream:
            row = json.loads(line)
            if row.get("actor_update_mask", 0) <= 0 or row.get("advantage") in (0, 0.0):
                continue
            logits, _ = model(encode(row["serialized_public"], vocab))
            p0 = torch.tensor(row["p0"], dtype=logits.dtype)
            log_probs = F.log_softmax(torch.log(p0) + logits, dim=0)
            ratio = float(torch.exp(log_probs[int(row["action"])] - float(row["old_log_probability"])))
            if 0.800001 < ratio < 1.199999:
                return row
    raise ValueError("no active, unclipped retained batch-08 row found for post-hoc probe")


def main() -> None:
    vocab = Vocabulary(HERE / "vocabulary.json")
    digest = config_sha()
    models: dict[str, tuple[JointChoiceActorCritic, JointChoiceActorCritic]] = {}
    integrity: dict[str, dict] = {}
    parameter_deltas: dict[str, dict] = {}

    for block in BLOCKS:
        batch01_path = EVIDENCE / "training" / block / "batch-01.json"
        final_path = EVIDENCE / "training" / block / "final.pt"
        final_json_path = EVIDENCE / "training" / block / "final.json"
        batch01 = json.loads(batch01_path.read_text(encoding="utf-8"))
        summary = json.loads(final_json_path.read_text(encoding="utf-8"))
        torch.manual_seed(MODEL_SEEDS[block])
        initial = JointChoiceActorCritic(vocab)
        init_fp = model_fingerprint(initial, vocab, digest)
        payload = torch.load(final_path, map_location="cpu", weights_only=False)
        final = JointChoiceActorCritic(vocab)
        final.load_state_dict(payload["model_state"], strict=True)
        final_fp = model_fingerprint(final, vocab, digest)
        final_bytes_sha = _sha(final_path)

        assert digest == summary["config_sha256"] == payload["config_sha256"]
        assert batch01["block"] == block and int(batch01["batch_index"]) == 1
        assert vocab.sha256 == summary["vocabulary_sha256"] == payload["vocabulary_sha256"]
        assert init_fp == batch01["model_sha256_before"]
        assert summary["status"] == "COMPLETE" and summary["completed_batches"] == 8
        assert int(payload["completed_batches"]) == 8
        assert payload["block"] == block and int(payload["model_seed"]) == MODEL_SEEDS[block]
        assert final_bytes_sha == summary["final_checkpoint_sha256"]
        assert final_fp == summary["model_sha256"] == payload["model_sha256"]
        integrity[block] = {
            "model_seed": MODEL_SEEDS[block],
            "config_sha256": digest,
            "vocabulary_sha256": vocab.sha256,
            "batch01_model_sha256_before": batch01["model_sha256_before"],
            "reconstructed_initial_model_sha256": init_fp,
            "final_json_checkpoint_sha256": summary["final_checkpoint_sha256"],
            "actual_final_pt_sha256": final_bytes_sha,
            "final_model_sha256": final_fp,
            "completed_batches": int(payload["completed_batches"]),
            "integrity": "PASS",
        }
        models[block] = (initial, final)

        names = {name: p.detach() for name, p in initial.named_parameters()}
        final_names = {name: p.detach() for name, p in final.named_parameters()}
        grouped: dict[str, list[str]] = {group: [] for group in GROUPS}
        for name in names:
            grouped[_group(name)].append(name)
        metrics = {}
        for group, members in grouped.items():
            metrics[group] = _delta_summary([names[n] for n in members], [final_names[n] for n in members])
        shared_members = [name for group in GROUPS if group in {
            "category", "entity", "map_node", "map_message", "choice_combine", "global_context"
        } for name in grouped[group]]
        metrics["shared_encoder_and_global_context"] = _delta_summary(
            [names[n] for n in shared_members], [final_names[n] for n in shared_members]
        )
        metrics["all_parameters"] = _delta_summary(list(names.values()), list(final_names.values()))
        parameter_deltas[block] = metrics

    score_movement = {}
    batch08_rows = {}
    for block in BLOCKS:
        path = EVIDENCE / "training" / block / "batch-08-training-rows.jsonl.gz"
        rows = _read_first_per_game(path)
        batch08_rows[block] = rows
        score_movement[block] = _score_summary(models[block][1], rows, vocab)

    # One retained row at initialization tests the output-layer gradient gate;
    # one final-block row checks whether a policy path can propagate later.
    init_path = EVIDENCE / "training" / "block1" / "batch-01-training-rows.jsonl.gz"
    init_row = _first_row(init_path)
    final_path = EVIDENCE / "training" / "block1" / "batch-08-training-rows.jsonl.gz"
    final_row = _final_active_row(models["block1"][1], final_path, vocab)
    gradients = {
        "method": "single retained decision-row autograd decomposition; no optimizer step; stored raw advantage, per-game denominator 128; critic coefficient 0.5",
        "block1_initial_policy_vs_critic": _gradient_probe(
            models["block1"][0], init_row, float(init_row["advantage"]), vocab
        ),
        "block1_final_policy_vs_critic": _gradient_probe(
            models["block1"][1], final_row, float(final_row["advantage"]), vocab
        ),
    }
    report = {
        "schema_id": "issue61-actor-critic-initialization-diagnosis-v1",
        "purpose": "STUDY_ONLY; read-only post-hoc diagnosis of frozen Issue 60 checkpoints",
        "frozen_issue60_commit": "1cce7997c648121742f0df0cfb95a2211aa3492c",
        "config_sha256": digest,
        "vocabulary_sha256": vocab.sha256,
        "cpu_runtime": "2.12.0+cpu; 1 thread; deterministic algorithms enabled",
        "integrity": integrity,
        "parameter_deltas": parameter_deltas,
        "score_movement": {
            "method": "first supported multi-choice row per game_seed in each retained batch-08 training file; on each public input, compare every alternative i to the unique p0 winner w using margin_i=log(p0[w]/p0[i]). Per row, adverse fraction=max(0,max_i((logit[i]-logit[w])/margin_i)); absolute fraction=max_i(abs(logit[i]-logit[w])/margin_i). Both take maxima over all alternatives and are candidate-order invariant. This is not the retained evaluation action-flip audit.",
            "block1_checkpoint_on_block1_batch08": score_movement["block1"],
            "block2_checkpoint_on_block2_batch08": score_movement["block2"],
        },
        "gradient_probes": gradients,
        "simulator_calls": 0,
        "optimizer_steps": 0,
        "process_cpu_seconds": time.process_time() - _CPU_START,
    }
    print(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
