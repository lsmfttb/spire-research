from __future__ import annotations

import json
import unittest

import source_qualification_v9_engineering_6 as v6


class SourceHistoryCaptureV9Engineering6Tests(unittest.TestCase):
    def test_parent_capture_matches_its_own_engineering5_lock(self):
        parent_lock, report, attempt, history, roots = v6._parent_capture_values()
        mismatches = v6.capture_receipt_mismatches_v9(
            report,
            attempt,
            history,
            roots,
            parent_lock=parent_lock,
            parent_lock_sha256=v6.sha256_text_file(v6.parent.LOCK_PATH),
            roots_path=v6.CAPTURE_ROOTS_PATH.relative_to(v6.STUDY).as_posix(),
            roots_sha256=v6.sha256_text_file(v6.CAPTURE_ROOTS_PATH),
            history_sha256=v6.sha256_text_file(v6.CAPTURE_HISTORY_PATH),
        )
        self.assertEqual(mismatches, [])
        self.assertEqual(len(history["events"]), 111)
        self.assertEqual(len(roots["roots"]), 39)

    def test_stale_engineering1_capture_references_are_rejected(self):
        parent_lock, report, attempt, history, roots = v6._parent_capture_values()
        old_lock_path = v6.parent.v9_engineering_1.LOCK_PATH
        old_roots_path = v6.parent.ENGINEERING_1_CAPTURE_ROOTS_PATH
        old_lock_sha = v6.sha256_text_file(old_lock_path)
        old_roots_sha = v6.sha256_text_file(old_roots_path)
        stale_report = json.loads(json.dumps(report))
        stale_attempt = json.loads(json.dumps(attempt))
        stale_report["source_lock_sha256"] = old_lock_sha
        stale_report["root_snapshots_path"] = old_roots_path.relative_to(v6.STUDY).as_posix()
        stale_report["root_snapshots_sha256"] = old_roots_sha
        stale_attempt["source_lock_sha256"] = old_lock_sha
        stale_attempt["root_snapshots_sha256"] = old_roots_sha

        mismatches = v6.capture_receipt_mismatches_v9(
            stale_report,
            stale_attempt,
            history,
            roots,
            parent_lock=parent_lock,
            parent_lock_sha256=v6.sha256_text_file(v6.parent.LOCK_PATH),
            roots_path=v6.CAPTURE_ROOTS_PATH.relative_to(v6.STUDY).as_posix(),
            roots_sha256=v6.sha256_text_file(v6.CAPTURE_ROOTS_PATH),
            history_sha256=v6.sha256_text_file(v6.CAPTURE_HISTORY_PATH),
        )
        self.assertIn("capture_report_lock", mismatches)
        self.assertIn("capture_roots_path", mismatches)
        self.assertIn("capture_roots_digest", mismatches)
        self.assertIn("capture_attempt_lock", mismatches)

    def test_parent_engineering1_is_not_the_v9_capture_parent(self):
        parent_lock, report, attempt, _history, _roots = v6._parent_capture_values()
        self.assertEqual(report["source_lock_sha256"], v6.sha256_text_file(v6.parent.LOCK_PATH))
        self.assertEqual(attempt["source_lock_sha256"], v6.sha256_text_file(v6.parent.LOCK_PATH))
        self.assertNotEqual(report["source_lock_sha256"], v6.sha256_text_file(v6.parent.v9_engineering_1.LOCK_PATH))
        self.assertEqual(parent_lock["lock_revision"], 14)
        self.assertFalse(parent_lock["stage1_authorized"])

    def test_static_debit_uses_only_remaining_authorized_nonnative_pool(self):
        base = v6._base_prior_ledger()
        ledger = v6._ledger_template(base)
        ledger["attempts"].append({
            "key": v6.STATIC_KEY,
            "cpu_charged_seconds": v6.STATIC_RESERVE,
            "native_gate_bc": False,
            "contingency_pool": v6.NEW_NONNATIVE_POOL,
        })
        projection = v6._resource_projection(base, ledger)
        self.assertTrue(projection["forecast_fits_registered_caps"])
        self.assertEqual(projection["nonnative_contingency_remaining_cpu_seconds"], 0.0)
        self.assertEqual(projection["previous_nonnative_contingency_remaining_cpu_seconds"], 0.0)
        self.assertTrue(projection["reviewer_reserve_untouched"])
        self.assertTrue(projection["no_prior_charge_reclaimed"])


if __name__ == "__main__":
    unittest.main()
