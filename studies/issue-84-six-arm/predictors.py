"""One-block-lagged V/Q fitting for the Issue 84 training arms."""
from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import torch
from torch import nn

from gradients import OwnerOutcome, ScalarPredictor, _feature_matrix, state_features


AUXILIARY_LEARNING_RATE = 3e-4
AUXILIARY_WEIGHT_DECAY = 0.0
AUXILIARY_BETAS = (0.9, 0.999)
AUXILIARY_EPSILON = 1e-8
AUXILIARY_BATCH_SIZE = 256
AUXILIARY_EPOCHS_PER_BLOCK = 1


def new_predictor_optimizer(model: ScalarPredictor) -> torch.optim.AdamW:
    return torch.optim.AdamW(
        model.parameters(),
        lr=AUXILIARY_LEARNING_RATE,
        weight_decay=AUXILIARY_WEIGHT_DECAY,
        betas=AUXILIARY_BETAS,
        eps=AUXILIARY_EPSILON,
    )


def _fit_rows(
    history: Sequence[tuple[int, Sequence[OwnerOutcome]]],
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    states: list[torch.Tensor] = []
    actions: list[torch.Tensor] = []
    targets: list[float] = []
    seen_owners: set[int] = set()
    for _block, owners in history:
        for owner in owners:
            if not owner.complete:
                raise ValueError("predictors may use only complete genuine natural-game labels")
            if int(owner.owner_seed) in seen_owners:
                raise ValueError("owner seed repeated in an auxiliary predictor's history")
            seen_owners.add(int(owner.owner_seed))
            for decision in owner.decisions:
                features = _feature_matrix(decision)
                chosen = int(decision.chosen_index)
                states.append(state_features(decision).squeeze(0).detach())
                actions.append(features[chosen].detach())
                targets.append(float(owner.reward))
    if not states:
        return torch.empty((0, 2048)), torch.empty((0, 2048)), torch.empty((0,))
    return torch.stack(states), torch.stack(actions), torch.tensor(targets, dtype=torch.float32)


def _fit_one_epoch(
    model: ScalarPredictor,
    optimizer: torch.optim.Optimizer,
    inputs: torch.Tensor,
    targets: torch.Tensor,
    *,
    seed: int,
) -> dict[str, float | int]:
    if inputs.ndim != 2 or inputs.shape[1] != 2048 or targets.shape != (inputs.shape[0],):
        raise ValueError("auxiliary training rows have incompatible shapes")
    if len(inputs) == 0:
        return {"examples": 0, "steps": 0, "mse_before": 0.0, "mse_after": 0.0}
    started = time.monotonic()
    model.train()
    with torch.no_grad():
        before = float(torch.mean((model(inputs).reshape(-1) - targets) ** 2).item())
    order = list(range(len(inputs)))
    random.Random(int(seed)).shuffle(order)
    step_count = 0
    for offset in range(0, len(order), AUXILIARY_BATCH_SIZE):
        indices = torch.tensor(order[offset:offset + AUXILIARY_BATCH_SIZE], dtype=torch.long)
        prediction = model(inputs.index_select(0, indices)).reshape(-1)
        loss = torch.mean((prediction - targets.index_select(0, indices)) ** 2)
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        step_count += 1
    model.eval()
    with torch.no_grad():
        after = float(torch.mean((model(inputs).reshape(-1) - targets) ** 2).item())
    return {
        "examples": int(len(inputs)),
        "steps": step_count,
        "mse_before": before,
        "mse_after": after,
        "elapsed_seconds": time.monotonic() - started,
    }


def _streaming_mse(model: ScalarPredictor, inputs: np.memmap, targets: np.memmap) -> float:
    """Measure MSE in bounded batches without materializing the feature matrix."""
    squared_error = 0.0
    count = int(len(inputs))
    with torch.no_grad():
        for offset in range(0, count, AUXILIARY_BATCH_SIZE):
            end = min(count, offset + AUXILIARY_BATCH_SIZE)
            batch = torch.from_numpy(np.asarray(inputs[offset:end]).copy())
            labels = torch.from_numpy(np.asarray(targets[offset:end]).copy())
            errors = model(batch).reshape(-1) - labels
            squared_error += float(torch.sum(errors * errors, dtype=torch.float64).item())
    return squared_error / count if count else 0.0


def _fit_memmap_epoch(
    model: ScalarPredictor,
    optimizer: torch.optim.Optimizer,
    inputs: np.memmap,
    targets: np.memmap,
    *,
    seed: int,
) -> dict[str, float | int]:
    if inputs.ndim != 2 or inputs.shape[1] != 2048 or targets.shape != (inputs.shape[0],):
        raise ValueError("disk-backed auxiliary rows have incompatible shapes")
    if len(inputs) == 0:
        return {"examples": 0, "steps": 0, "mse_before": 0.0, "mse_after": 0.0}
    started = time.monotonic()
    model.train()
    before = _streaming_mse(model, inputs, targets)
    # This is the same global shuffle and minibatch boundary as _fit_one_epoch;
    # only the backing store changes, not the order of optimizer updates.
    order = list(range(len(inputs)))
    random.Random(int(seed)).shuffle(order)
    step_count = 0
    for offset in range(0, len(order), AUXILIARY_BATCH_SIZE):
        indices = order[offset:offset + AUXILIARY_BATCH_SIZE]
        batch = torch.from_numpy(np.asarray(inputs[indices]).copy())
        labels = torch.from_numpy(np.asarray(targets[indices]).copy())
        prediction = model(batch).reshape(-1)
        loss = torch.mean((prediction - labels) ** 2)
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        step_count += 1
    model.eval()
    after = _streaming_mse(model, inputs, targets)
    return {
        "examples": int(len(inputs)),
        "steps": step_count,
        "mse_before": before,
        "mse_after": after,
        "elapsed_seconds": time.monotonic() - started,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_cache_files(cache_path: Path, manifest: dict[str, object], binding: dict[str, object]) -> None:
    if manifest.get("schema_id") != "issue84-predictor-feature-cache-v1":
        raise RuntimeError("predictor cache schema identifier is invalid")
    if any(manifest.get(key) != value for key, value in binding.items()):
        raise RuntimeError("existing predictor cache has a different source/history binding")
    count = manifest.get("examples")
    if not isinstance(count, int) or count < 0:
        raise RuntimeError("predictor cache example count is invalid")
    if manifest.get("feature_dimension") != 2048 or manifest.get("dtype") != "float32-le":
        raise RuntimeError("predictor cache feature layout differs from the registered model input")
    expected_sizes = {
        "states.f32": count * 2048 * 4,
        "actions.f32": count * 2048 * 4,
        "targets.f32": count * 4,
    }
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(expected_sizes):
        raise RuntimeError("predictor cache file inventory is incomplete")
    for name, size in expected_sizes.items():
        path = cache_path / name
        if not path.is_file() or path.stat().st_size != size:
            raise RuntimeError(f"predictor cache size mismatch: {name}")
        if _file_sha256(path) != files[name]:
            raise RuntimeError(f"predictor cache hash mismatch: {name}")


def build_predictor_cache(
    history: Iterable[tuple[int, Sequence[OwnerOutcome]]],
    *,
    cache_path: Path,
    binding: dict[str, object],
) -> dict[str, object]:
    """Stream labels into an immutable, hash-bound disk cache.

    A temporary directory is atomically renamed only after all three arrays and
    their manifest are complete. A crash therefore leaves no partial canonical
    cache that a later recovery could mistake for evidence.
    """
    cache_path = Path(cache_path)
    manifest_path = cache_path / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _validate_cache_files(cache_path, manifest, binding)
        return manifest
    if cache_path.exists():
        raise RuntimeError("predictor cache directory exists without a complete immutable manifest")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = Path(tempfile.mkdtemp(prefix=f".{cache_path.name}.building-", dir=cache_path.parent))
    row_count = 0
    seen_owners: set[int] = set()
    state_path = temp_path / "states.f32"
    action_path = temp_path / "actions.f32"
    target_path = temp_path / "targets.f32"
    try:
        with state_path.open("wb") as states, action_path.open("wb") as actions, target_path.open("wb") as targets:
            for _block, owners in history:
                for owner in owners:
                    if not owner.complete:
                        raise ValueError("predictors may use only complete genuine natural-game labels")
                    seed = int(owner.owner_seed)
                    if seed in seen_owners:
                        raise ValueError("owner seed repeated in an auxiliary predictor's history")
                    seen_owners.add(seed)
                    reward = np.float32(owner.reward)
                    for decision in owner.decisions:
                        features = _feature_matrix(decision)
                        chosen = int(decision.chosen_index)
                        state = state_features(decision).squeeze(0).detach().cpu().numpy().astype(np.float32, copy=False)
                        action = features[chosen].detach().cpu().numpy().astype(np.float32, copy=False)
                        if state.shape != (2048,) or action.shape != (2048,):
                            raise ValueError("predictor feature cache received an incompatible row")
                        states.write(state.tobytes(order="C"))
                        actions.write(action.tobytes(order="C"))
                        targets.write(reward.tobytes())
                        row_count += 1
            for stream in (states, actions, targets):
                stream.flush()
                os.fsync(stream.fileno())
        manifest: dict[str, object] = {
            "schema_id": "issue84-predictor-feature-cache-v1",
            **binding,
            "examples": row_count,
            "feature_dimension": 2048,
            "dtype": "float32-le",
            "files": {
                state_path.name: _file_sha256(state_path),
                action_path.name: _file_sha256(action_path),
                target_path.name: _file_sha256(target_path),
            },
        }
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with (temp_path / "manifest.json").open("xb") as stream:
            stream.write(manifest_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        # Re-read and hash before publishing; this catches short writes and bad bindings.
        _validate_cache_files(temp_path, manifest, binding)
        os.replace(temp_path, cache_path)
        return manifest
    except BaseException:
        shutil.rmtree(temp_path, ignore_errors=True)
        raise


def discard_predictor_cache(cache_path: Path, *, binding: dict[str, object]) -> None:
    """Remove a verified reproducible cache after its predictor checkpoint is durable."""
    cache_path = Path(cache_path)
    manifest_path = cache_path / "manifest.json"
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_cache_files(cache_path, manifest, binding)
    shutil.rmtree(cache_path)


def fit_cached_prior_block_predictors(
    *,
    arm: str,
    current_block_index: int,
    cache_path: Path,
    cache_manifest: dict[str, object],
    value_model: ScalarPredictor,
    value_optimizer: torch.optim.Optimizer,
    action_value_model: ScalarPredictor | None = None,
    action_value_optimizer: torch.optim.Optimizer | None = None,
) -> dict[str, object]:
    """Fit the registered one-epoch schedule from a bounded-memory cache."""
    arm = arm.upper()
    block_ids = list(cache_manifest.get("prior_block_indices", []))
    if arm not in {"D", "E", "F"} or current_block_index < 0:
        raise ValueError("invalid cached predictor arm or block")
    if block_ids != list(range(current_block_index)):
        raise ValueError("cached predictor history is not exactly the completed prior blocks")
    if arm == "E" and (action_value_model is None or action_value_optimizer is None):
        raise ValueError("arm E needs its own Q model and separate optimizer")
    if arm != "E" and (action_value_model is not None or action_value_optimizer is not None):
        raise ValueError("only arm E has a registered Q predictor")
    if not block_ids:
        value_model.eval()
        if action_value_model is not None:
            action_value_model.eval()
        return {
            "schema_id": "issue84-auxiliary-fit-v1", "arm": arm,
            "current_block_index": current_block_index, "prior_block_indices": [],
            "value": {"examples": 0, "steps": 0, "cold_start_zero": True},
            "action_value": None,
            "feature_cache_manifest_sha256": hashlib.sha256(
                json.dumps(cache_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
    count = int(cache_manifest["examples"])
    cache_path = Path(cache_path)
    _validate_cache_files(cache_path, cache_manifest, {
        key: value for key, value in cache_manifest.items()
        if key not in {"schema_id", "examples", "feature_dimension", "dtype", "files"}
    })
    states = np.memmap(cache_path / "states.f32", mode="r", dtype="<f4", shape=(count, 2048))
    actions = np.memmap(cache_path / "actions.f32", mode="r", dtype="<f4", shape=(count, 2048))
    targets = np.memmap(cache_path / "targets.f32", mode="r", dtype="<f4", shape=(count,))
    value_stats = _fit_memmap_epoch(
        value_model, value_optimizer, states, targets,
        seed=_seed(arm, current_block_index, "V"),
    )
    action_stats = None
    if arm == "E":
        action_stats = _fit_memmap_epoch(
            action_value_model, action_value_optimizer, actions, targets,
            seed=_seed(arm, current_block_index, "Q"),
        )
    value_model.eval()
    if action_value_model is not None:
        action_value_model.eval()
    return {
        "schema_id": "issue84-auxiliary-fit-v1", "arm": arm,
        "current_block_index": current_block_index, "prior_block_indices": block_ids,
        "value": value_stats, "action_value": action_stats,
        "feature_cache_manifest_sha256": hashlib.sha256(
            json.dumps(cache_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def _seed(arm: str, current_block_index: int, predictor: str) -> int:
    digest = hashlib.sha256(
        f"issue84-aux-v1|{arm}|{current_block_index}|{predictor}".encode("ascii")
    ).digest()
    return int.from_bytes(digest[:8], "big")


def fit_prior_block_predictors(
    *,
    arm: str,
    current_block_index: int,
    prior_blocks: Sequence[tuple[int, Sequence[OwnerOutcome]]],
    value_model: ScalarPredictor,
    value_optimizer: torch.optim.Optimizer,
    action_value_model: ScalarPredictor | None = None,
    action_value_optimizer: torch.optim.Optimizer | None = None,
) -> dict[str, object]:
    """Fit only on completed prior blocks and freeze the models for this block.

    D/E/F call the same V fit schedule with arm-specific earlier outcomes. E
    additionally fits Q on its own earlier chosen actions. Call this before
    generating/updating the current block; block 0 must be the cold-start.
    """
    arm = arm.upper()
    if arm not in {"D", "E", "F"}:
        raise ValueError("only arms D/E/F have registered auxiliary predictors")
    if current_block_index < 0:
        raise ValueError("block index must be nonnegative")
    block_ids = [int(block_index) for block_index, _ in prior_blocks]
    if block_ids != sorted(set(block_ids)) or any(index >= current_block_index for index in block_ids):
        raise ValueError("predictor fit includes current/future data or unordered/duplicate blocks")
    if arm == "E" and (action_value_model is None or action_value_optimizer is None):
        raise ValueError("arm E needs its own Q model and separate optimizer")
    if arm != "E" and (action_value_model is not None or action_value_optimizer is not None):
        raise ValueError("only arm E has a registered Q predictor")

    if not prior_blocks:
        # The exact registered block-1 state baseline is zero. Predictors are
        # initialized with a zero final layer and no fitting data are fabricated.
        value_model.eval()
        if action_value_model is not None:
            action_value_model.eval()
        return {
            "schema_id": "issue84-auxiliary-fit-v1",
            "arm": arm,
            "current_block_index": current_block_index,
            "prior_block_indices": [],
            "value": {"examples": 0, "steps": 0, "cold_start_zero": True},
            "action_value": None,
        }

    states, actions, targets = _fit_rows(prior_blocks)
    value_stats = _fit_one_epoch(
        value_model, value_optimizer, states, targets,
        seed=_seed(arm, current_block_index, "V"),
    )
    action_stats = None
    if arm == "E":
        action_stats = _fit_one_epoch(
            action_value_model, action_value_optimizer, actions, targets,
            seed=_seed(arm, current_block_index, "Q"),
        )
    value_model.eval()
    if action_value_model is not None:
        action_value_model.eval()
    return {
        "schema_id": "issue84-auxiliary-fit-v1",
        "arm": arm,
        "current_block_index": current_block_index,
        "prior_block_indices": block_ids,
        "value": value_stats,
        "action_value": action_stats,
    }
