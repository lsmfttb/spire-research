from __future__ import annotations

import hashlib
import json
from pathlib import Path

OWNER_IDS = list(range(437001, 437097))
DOMAIN = b"spire-research/issue55/owner-split/v1\x00"
OUT = Path(__file__).with_name("preregistration.json")

ranked = sorted(
    (
        hashlib.sha256(DOMAIN + str(owner_id).encode("ascii")).hexdigest(),
        owner_id,
    )
    for owner_id in OWNER_IDS
)
train = ranked[:64]
validation = ranked[64:80]
heldout = ranked[80:]

record = {
    "schema": "issue55-preregistration-v1",
    "disposition": "STUDY_ONLY",
    "estimand": "V^{oldNative, Expert+privilegedBattle}_{restricted-public-subset}",
    "source": {
        "issue": 45,
        "root": "/home/lsmft/.cache/issue45-potion-paired/reviewed-native-18bcd565",
        "manifest_sha256": "624b656dee2afe33cf0e0410dd4cdf1a198c456d3d050a3ac061cb1aec18c160",
        "source_metadata_sha256": "265a0a5d07083d4fe80fe26d6a802b250f3df1b5600b46689b351d2c1a4e5197",
        "native_commit": "18bcd565b587186425bb46ad3d70a2eb80d947fc",
        "stsrl_commit": "7d172f7c24c9d29429b6946ba44f6b59760b0d96",
        "owner_id_field": "seed",
        "owner_ids": OWNER_IDS,
    },
    "split": {
        "algorithm": "SHA256(domain || canonical_owner_id_ascii), sort ascending by digest then numeric owner_id; first64 train, next16 validation, final16 heldout",
        "domain_hex": DOMAIN.hex(),
        "canonical_owner_id": "base-10 integer ASCII without leading zeroes",
        "tie_break": "numeric owner_id ascending",
        "partitions": {
            "train": [owner_id for _, owner_id in train],
            "validation": [owner_id for _, owner_id in validation],
            "heldout": [owner_id for _, owner_id in heldout],
        },
        "ranked_owner_digests": [
            {"owner_id": owner_id, "sha256": digest}
            for digest, owner_id in ranked
        ],
    },
    "target": {
        "name": "remaining_terminal_floors",
        "formula": "genuine_terminal_floor - current_floor_num",
        "terminal_source": "verified per-run root metadata terminal floor; never a feature",
        "current_floor_source": "decision-time snapshot_raw.floor_num",
    },
    "public_feature_allowlist": {
        "projection_evidence": {
            "native_source": "18bcd565b587186425bb46ad3d70a2eb80d947fc:bindings/slaythespire.cpp",
            "projection": "StepSimulator::publicProjectionV3; screen_identity from GameContext::screenState; persistent_resources exposes current_hp, max_hp, deck, relics, potion_count, potion_capacity, potion_identities",
            "stsrl_recording": "7d172f7c24c9d29429b6946ba44f6b59760b0d96:src/sts_combat_rl/sim/controlled_run.py records decision-time SimulatorSnapshot.raw; this study extracts only explicitly listed fields",
            "normal_semantics": "Act/floor, current/max HP, deck/relic identities, potion identities/slots/count/capacity are ordinary player-visible state; the raw native snapshot contains additional simulator and battle internals that are excluded",
            "limitation": "The original #45 decision records do not serialize a separate per-row projection payload. Admission will crosscheck allowed raw fields against the exact pinned projection source expressions and co-recorded decision-time values; unsupported or inconsistent rows fail closed. This is not complete observation closure.",
        },
        "baseline": [
            {"field": "act", "source": "snapshot_raw.act", "encoding": "categorical", "basis": "visible current Act"},
            {"field": "current_floor", "source": "snapshot_raw.floor_num", "encoding": "categorical", "basis": "visible current floor; also target origin"},
            {"field": "screen_family", "source": "snapshot_raw.screen_state", "encoding": "categorical", "basis": "publicProjectionV3 screen_identity"},
            {"field": "current_hp", "source": "snapshot_raw.battle_player_hp when battle_active else snapshot_raw.cur_hp", "encoding": "categorical", "basis": "publicProjectionV3 BattleContext::player.curHp or GameContext::curHp"},
            {"field": "max_hp", "source": "snapshot_raw.battle_player.max_hp when battle_active else snapshot_raw.max_hp", "encoding": "categorical", "basis": "publicProjectionV3 BattleContext::player.maxHp or GameContext::maxHp"},
            {"field": "deck_identity_multiset", "source": "snapshot_raw.deck[].id_label", "encoding": "one token per card identity; no order, upgrade, misc, or deck_index", "basis": "publicProjectionV3 persistent_resources.deck from GameContext::deck"},
            {"field": "relic_identity_multiset", "source": "snapshot_raw.relics[].id_label", "encoding": "one token per relic identity; no counter or relic_index", "basis": "publicProjectionV3 persistent_resources.relics from GameContext::relics"},
        ],
        "potion_only": [
            {"field": "potion_count", "source": "snapshot_raw.battle_potion_count when battle_active else snapshot_raw.potion_count", "encoding": "categorical", "basis": "publicProjectionV3 BattleContext::potionCount or GameContext::potionCount"},
            {"field": "potion_capacity", "source": "snapshot_raw.battle_potion_capacity when battle_active else snapshot_raw.potion_capacity", "encoding": "categorical", "basis": "publicProjectionV3 BattleContext::potionCapacity or GameContext::potionCapacity"},
            {"field": "potion_slot_identity", "source": "snapshot_raw.battle_potions[] when battle_active else snapshot_raw.potions[]; use potion_index and id_label", "encoding": "ordered slot identity; EMPTY_POTION_ID records an empty visible slot", "basis": "publicProjectionV3 persistent_resources.potion_identities from BattleContext::potions or GameContext::potions"},
        ],
        "not_features": [
            "owner_id", "seed", "terminal_floor", "outcome", "current or next chosen action", "policy/controller/search metadata", "native packed observations", "next_snapshot_raw", "full-run summaries", "map/boss/reward/screen payloads not explicitly allowed", "gold", "keys", "room_type", "all battle hand/draw/discard/exhaust/monster/intent/energy/block/status fields", "relic counters", "card upgrade/misc/order fields", "unknown or unsupported identity values"
        ],
        "unknown_or_inconsistent_allowlist_value": "fail closed as PUBLIC_FEATURE_SUBSET_UNAVAILABLE; do not impute",
        "eligible_rows": "decision records with non-empty legal action set, complete allowlisted current snapshot, and a verified terminal owner label; same eligibility for both model arms and baselines",
        "action_time_consistency": "check redundant current snapshot summaries and adjacent pre/post snapshots where present; next state, action choice and terminal label are excluded from feature tokens",
    },
    "models": {
        "visible": "POTION_VISIBLE uses baseline allowlist plus potion-only allowlist",
        "masked": "POTION_MASKED removes only the potion-only allowlist; all other inputs, eligible rows, owner weights, mapping, model capacity and alpha are identical",
        "feature_map": {
            "dimension": 256,
            "token_hash": "SHA256(UTF8('issue55-feature-v1\\0' + token)); bucket=first4bytes big-endian mod256; sign=+1 if low bit of byte4 is0 else -1; repeated categorical items accumulate signed counts",
            "numeric_handling": "categorical integer buckets only; no normalization fitted from validation/heldout",
            "intercept": "constant token included and uniformly regularized",
        },
        "estimator": "float64 ridge normal equations (X^T W X + 1.0 I)^-1 X^T W y; no intercept exemption, optimizer, tuning, or validation use",
        "owner_weighting": "each training row weight=1/number_of_eligible_rows_for_that_owner; objective sums owner-mean squared errors over training owners, plus alpha*L2 norm",
        "validation": "fixed split only; unused for model selection or tuning",
    },
    "baselines": {
        "floor_only": "fixed training-set equal-owner mean terminal-floor prior minus current floor; a one-scalar null value anchor, no fitted weights",
        "floor_hp": "fixed training-set equal-owner mean terminal-floor prior minus current floor, multiplied by current_hp/max_hp; deterministic survival-context heuristic with no fitted weights",
        "warning": "These are frozen descriptive heuristics only; neither is a trained model or potion-use counterfactual. Report denominators and do not infer mechanism.",
    },
    "evaluation": {
        "heldout": "open only after both ridge weight files and a freeze receipt are durably written",
        "primary_metrics": ["owner-equal MAE", "owner-equal MSE", "mean paired owner-level absolute_error(masked-visible)"],
        "bootstrap": {"unit": "whole owner cluster", "replicates": 5000, "rng_seed": 550055, "interval": "percentile 95%", "strata": "overall; then potion-presence, public screen family, and floor bands F00-F08/F09-F16/F17+ where at least 2 heldout owners contribute; preserve all rows within sampled owners"},
        "calibration": "owner-equal mean prediction, mean actual, bias, and slope/intercept over owner-mean prediction/actual pairs when defined",
        "coverage": "descriptive heldout owner-equal row fraction within the 90th-percentile absolute training residual band; not a calibrated or guaranteed interval",
    },
    "termination": ["PUBLIC_FEATURE_SUBSET_UNAVAILABLE", "NO_IDENTIFIABLE_POTION_FEATURE_VARIATION", "RESTRICTED_HISTORICAL_PREDICTION_SIGNAL_SUPPORTED", "SIGNAL_NOT_ESTABLISHED", "SIGNAL_HARM_SUPPORTED", "RESOURCE_BLOCKED"],
    "resource_budget": {
        "all_in_cpu_seconds_max": 2500,
        "reviewer_reserved_min_cpu_seconds": 600,
        "builder_plus_admin_max_cpu_seconds": 1900,
        "no_new_games_builds_labels_or_replays": True,
    },
    "interpretation": "Observational predictive signal under historical old-Native Expert+privileged-Battle-search visitation only; not causal use/retain value, current-Native V^pi, policy readiness, or actual Heart strength. Null is not absence of potion value. STUDY_ONLY regardless of result.",
}

OUT.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(OUT)
print(hashlib.sha256(OUT.read_bytes()).hexdigest())
print(json.dumps({k: len(v) for k,v in record["split"]["partitions"].items()}, sort_keys=True))
