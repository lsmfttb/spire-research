from __future__ import annotations

import unittest

import protocol


def synthetic_roots() -> list[dict]:
    result = []
    for owner_index, owner in enumerate(protocol.OWNER_SEEDS):
        count = 29 if owner_index == 7 else 28
        for decision in range(count):
            root_id = f"root-{owner}-{decision:04d}"
            result.append({
                "root_id": root_id,
                "owner_seed": owner,
                "owner_decision_index": decision,
                "policy_decision_index": decision,
                "screen": ("EVENT_SCREEN", "REWARDS", "MAP_SCREEN", "REST_ROOM")[decision % 4],
                "candidate_binding_validation_passed": True,
                "legal_action_occurrences": [{"stable_id": f"{root_id}:{i}"} for i in range(2 + decision % 4)],
                "projection_digest": f"projection-{root_id}",
                "feature_matrix_sha256": f"features-{root_id}",
                "root_fingerprints": {"native_state_sha256": f"native-{root_id}"},
                "_source_file_sha256": f"source-{root_id}",
                "terminal_outcome_forbidden_selection": decision,
            })
    return result


class Issue74ProtocolTests(unittest.TestCase):
    def test_selection_is_fixed_top_four_per_owner_and_ignores_outcomes(self) -> None:
        records = synthetic_roots()
        first = protocol.select_roots(records)
        for record in records:
            record["terminal_outcome_forbidden_selection"] = -record["owner_decision_index"]
        second = protocol.select_roots(records)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)
        self.assertEqual({owner: sum(row["owner_seed"] == owner for row in first)
                          for owner in protocol.OWNER_SEEDS}, {owner: 4 for owner in protocol.OWNER_SEEDS})
        self.assertFalse(protocol.GATE_C_ROOT_IDS.intersection(row["root_id"] for row in first))
        self.assertTrue(all(row["selected_before_eligibility_checks"] for row in first))

    def test_selection_hash_uses_the_registered_coordinates(self) -> None:
        row = synthetic_roots()[0]
        original = protocol.selection_key(row)
        row["terminal_outcome_forbidden_selection"] = "changed"
        self.assertEqual(protocol.selection_key(row), original)
        row["screen"] = "different screen"
        self.assertNotEqual(protocol.selection_key(row), original)

    def test_rng_streams_are_stable_and_independent(self) -> None:
        common = {"owner_seed": 173730001, "root_id": "root-173730001-0004",
                  "decision_index": 4, "candidate_index": 1, "replicate": 3}
        control = protocol.stream_seed(**common, stream="control_policy")
        self.assertEqual(control, protocol.stream_seed(**common, stream="control_policy"))
        values = {control}
        for stream in ("control_expert", "oracle_policy", "oracle_expert"):
            value = protocol.stream_seed(**common, stream=stream)
            self.assertNotIn(value, values)
            values.add(value)

    def test_resource_ceiling_keeps_reviewer_reserve(self) -> None:
        charge = protocol.ResourceCharge(0, 0)
        charge = charge.add(builder_cpu_seconds=100000, native_cpu_seconds=85000)
        with self.assertRaises(protocol.ContractError):
            charge.add(builder_cpu_seconds=0.1, native_cpu_seconds=0)

    def test_selection_rejects_unqualified_or_wrong_sized_population(self) -> None:
        records = synthetic_roots()
        records[4]["candidate_binding_validation_passed"] = False
        with self.assertRaises(protocol.ContractError):
            protocol.select_roots(records)
        with self.assertRaises(protocol.ContractError):
            protocol.select_roots(records[:-1])

    def test_source_trace_match_rejects_reordered_legal_occurrences(self) -> None:
        row = synthetic_roots()[0]
        event = {"kind": "categorical", "native_screen": row["screen"],
                 "projection_digest": row["projection_digest"],
                 "legal_action_identities": row["legal_action_occurrences"],
                 "chosen_index": 0}
        protocol.verify_source_trace_event(row, event)
        event["legal_action_identities"] = list(reversed(event["legal_action_identities"]))
        with self.assertRaises(protocol.ContractError):
            protocol.verify_source_trace_event(row, event)

    def test_replayed_root_fingerprints_and_forced_occurrence_are_exact(self) -> None:
        row = synthetic_roots()[0]
        actual = {"screen": row["screen"], "projection_digest": row["projection_digest"],
                  "legal_action_identities": row["legal_action_occurrences"],
                  **row["root_fingerprints"], "checkpoint": object()}
        protocol.verify_replayed_root(row, actual)
        identities = row["legal_action_occurrences"]
        index = 1
        receipt = {"forced_root_consumed": True, "candidate_index": index,
                   "candidate_identity_sha256": protocol.sha256_bytes(protocol.canonical_payload(identities[index])),
                   "legal_action_identities_sha256": protocol.sha256_bytes(protocol.canonical_payload(identities))}
        protocol.verify_forced_root_receipt(row, index, receipt)
        receipt["candidate_index"] = 0
        with self.assertRaises(protocol.ContractError):
            protocol.verify_forced_root_receipt(row, index, receipt)


if __name__ == "__main__":
    unittest.main()
