"""Standard-library tests for the live Stage 1 activation-to-ledger gate."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

STUDY = Path(__file__).resolve().parents[1]
REPO = STUDY.parents[1]
sys.path.insert(0, str(STUDY))

import stage1_budget as budget

PACKAGE_COMMIT, LOCK_DIGEST = budget._current_package_binding()


def decision(comment_id, marker, timestamp, *, package=PACKAGE_COMMIT, lock=LOCK_DIGEST, url=True, login=None):
    comment = {
        "id": comment_id,
        "body": (
            f"{marker}\npackage_commit: {package}\n"
            f"source_lock_sha256: {lock}\nscope: STUDY_ONLY"
        ),
        "created_at": timestamp,
        "user": {"login": login or budget.OWNER_LOGIN, "type": "User"},
    }
    if url:
        comment["html_url"] = (
            f"https://github.com/{budget.REPOSITORY}/issues/{budget.ISSUE_NUMBER}"
            f"#issuecomment-{comment_id}"
        )
    return comment


class ActivationGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=REPO / ".task-tmp")
        self.addCleanup(self.temp.cleanup)

    def open_with_comments(self, comments):
        with mock.patch.object(
            budget, "_fetch_authoritative_issue_comments", return_value=comments,
        ) as fetch_comments:
            ledger = budget.open_activated_stage1_ledger(Path(self.temp.name) / "ledger")
            fetch_comments.assert_called_once_with()
            return ledger

    def assert_rejected(self, comments):
        ledger_path = Path(self.temp.name) / "rejected-ledger"
        with mock.patch.object(budget, "_fetch_authoritative_issue_comments", return_value=comments):
            with self.assertRaises(PermissionError):
                budget.open_activated_stage1_ledger(ledger_path)
        self.assertFalse(ledger_path.exists())

    def valid_pair(self):
        return [
            decision(101, budget.REVIEW_MARKER, "2026-01-01T00:00:00Z"),
            decision(102, budget.PLANNER_MARKER, "2026-01-01T00:01:00Z"),
        ]

    def test_gate_fetches_records_and_binds_current_checkout(self):
        ledger = self.open_with_comments(self.valid_pair())
        self.assertEqual(ledger.activation.package_commit, PACKAGE_COMMIT)
        self.assertEqual(ledger.activation.source_lock_sha256, LOCK_DIGEST)
        self.assertEqual(ledger.activation.reviewer_comment_id, 101)
        self.assertEqual(ledger.activation.planner_comment_id, 102)

    def test_comment_source_is_the_fixed_github_issue_api(self):
        with mock.patch.object(budget, "urlopen", return_value=BytesIO(b"[]")) as api:
            self.assertEqual(budget._fetch_authoritative_issue_comments(), [])
        request = api.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/lsmfttb/spire-research/issues/75/comments?per_page=100&page=1",
        )
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(api.call_args.kwargs["timeout"], 30)

    def test_directly_forged_receipt_cannot_create_or_write_ledger(self):
        forged = budget.ActivationReceipt("0" * 40, "0" * 64, 1, 2, "2026-01-01T00:01:00+00:00")
        ledger_path = Path(self.temp.name) / "forged-ledger"
        with self.assertRaises(PermissionError):
            budget.Stage1Ledger(ledger_path, forged)
        self.assertFalse(ledger_path.exists())

    def test_missing_comment_url_or_id_is_rejected(self):
        reviewer, planner = self.valid_pair()
        planner.pop("html_url")
        self.assert_rejected([reviewer, planner])

        reviewer, planner = self.valid_pair()
        planner.pop("id")
        self.assert_rejected([reviewer, planner])

    def test_comment_url_must_name_issue_75_and_its_exact_comment_id(self):
        reviewer, planner = self.valid_pair()
        planner["html_url"] = (
            f"https://github.com/{budget.REPOSITORY}/issues/74#issuecomment-{planner['id']}"
        )
        self.assert_rejected([reviewer, planner])

        reviewer, planner = self.valid_pair()
        planner["html_url"] = (
            f"https://github.com/{budget.REPOSITORY}/issues/75#issuecomment-999"
        )
        self.assert_rejected([reviewer, planner])

    def test_comments_must_bind_exact_current_package_and_lock(self):
        reviewer, _ = self.valid_pair()
        wrong_package = decision(
            102, budget.PLANNER_MARKER, "2026-01-01T00:01:00Z", package="d" * 40,
        )
        self.assert_rejected([reviewer, wrong_package])

        reviewer, _ = self.valid_pair()
        wrong_lock = decision(
            102, budget.PLANNER_MARKER, "2026-01-01T00:01:00Z", lock="e" * 64,
        )
        self.assert_rejected([reviewer, wrong_lock])

    def test_dirty_study_checkout_cannot_open_the_ledger(self):
        dirty = budget.subprocess.CompletedProcess(args=["git"], returncode=1)
        with mock.patch.object(budget.subprocess, "run", return_value=dirty):
            with mock.patch.object(budget, "_fetch_authoritative_issue_comments") as fetch_comments:
                ledger_path = Path(self.temp.name) / "dirty-ledger"
                with self.assertRaises(PermissionError):
                    budget.open_activated_stage1_ledger(ledger_path)
                fetch_comments.assert_not_called()
        self.assertFalse(ledger_path.exists())

    def test_same_owner_roles_still_require_distinct_ordered_comments(self):
        reviewer = decision(101, budget.REVIEW_MARKER, "2026-01-01T00:01:00Z")
        planner = decision(102, budget.PLANNER_MARKER, "2026-01-01T00:00:00Z")
        self.assert_rejected([reviewer, planner])

        reviewer = decision(101, budget.REVIEW_MARKER, "2026-01-01T00:00:00Z")
        planner = decision(101, budget.PLANNER_MARKER, "2026-01-01T00:01:00Z")
        self.assert_rejected([reviewer, planner])


if __name__ == "__main__":
    unittest.main()
