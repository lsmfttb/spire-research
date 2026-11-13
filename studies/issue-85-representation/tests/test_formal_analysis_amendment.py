"""Focused tests for the Planner-authorised post-TEST analysis amendment.

These cover the two things the amendment changed and nothing else: how the
pre-TEST analysis source is authenticated from historic bytes rather than from
the current checkout, and how each VALIDATION group is resolved to the package
that really produced it. They deliberately use synthetic, non-TEST inputs.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

STUDY = pathlib.Path(__file__).resolve().parents[1]
if str(STUDY) not in sys.path:
    sys.path.insert(0, str(STUDY))

import formal_analysis as fa

TEST_PRODUCER = "43b1da688d5e1fd69b16be71bc45d3d12b5a8423"
VAL_PRODUCER = "0c06faa11a5db26451808fde051f9be51bd46b0a"
VAL_PRODUCER_2 = "57ed39411c8024283a930ed30e80339597b9006e"


def _provenance(carried: dict[str, bool], producers: dict[str, str]) -> dict[str, dict]:
    return {
        policy: {
            "carried_by_reference": carried[policy],
            "producer_package_commit": producers[policy],
        }
        for policy in fa.POLICIES
    }


def _all_carried() -> dict[str, dict]:
    carried = {policy: True for policy in fa.POLICIES}
    producers = {
        policy: (VAL_PRODUCER_2 if policy in ("PARENT", "A") else VAL_PRODUCER)
        for policy in fa.POLICIES
    }
    return _provenance(carried, producers)


def test_pre_test_analysis_source_is_recovered_from_historic_bytes():
    """The recorded digest must come from git, not from the amended working file."""
    historic = fa._original_analysis_source_sha256(TEST_PRODUCER)
    assert len(historic) == 64
    # The amendment changed this file, so the current bytes are a different source.
    current = fa.formal.file_sha256(pathlib.Path(fa.__file__).resolve())
    assert current != historic


def test_pre_test_analysis_source_rejects_an_unknown_commit():
    with pytest.raises(ValueError, match="cannot recover the pre-TEST analysis source"):
        fa._original_analysis_source_sha256("0" * 40)


def test_producers_accept_the_real_carried_mapping():
    resolved = fa._validation_producers({"validation_provenance": _all_carried()}, TEST_PRODUCER)
    assert resolved["PARENT"] == (True, VAL_PRODUCER_2)
    assert resolved["A"] == (True, VAL_PRODUCER_2)
    for policy in ("B", "C", "D", "E", "F"):
        assert resolved[policy] == (True, VAL_PRODUCER)


def test_producers_accept_a_locally_produced_group():
    carried = {policy: False for policy in fa.POLICIES}
    producers = {policy: TEST_PRODUCER for policy in fa.POLICIES}
    resolved = fa._validation_producers(
        {"validation_provenance": _provenance(carried, producers)}, TEST_PRODUCER
    )
    assert all(resolved[policy] == (False, TEST_PRODUCER) for policy in fa.POLICIES)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p: p.pop("B"), "does not declare an authenticated producer"),
        (lambda p: p.update({"B": {"carried_by_reference": True}}), "authenticated producer"),
        (
            lambda p: p.update({
                "B": {"carried_by_reference": "yes", "producer_package_commit": VAL_PRODUCER}
            }),
            "authenticated producer",
        ),
        (
            lambda p: p.update({
                "B": {"carried_by_reference": True, "producer_package_commit": "not-a-commit"}
            }),
            "authenticated producer",
        ),
        # A group claiming to be carried from the reading package would let this
        # package pass off inherited evidence as its own work.
        (
            lambda p: p.update({
                "B": {"carried_by_reference": True, "producer_package_commit": TEST_PRODUCER}
            }),
            "carried from the reading package itself",
        ),
        # A local group may not quietly credit another producer.
        (
            lambda p: p.update({
                "B": {"carried_by_reference": False, "producer_package_commit": VAL_PRODUCER}
            }),
            "local but names another producer",
        ),
    ],
)
def test_producers_reject_malformed_or_dishonest_mappings(mutate, message):
    provenance = _all_carried()
    mutate(provenance)
    with pytest.raises(ValueError, match=message):
        fa._validation_producers({"validation_provenance": provenance}, TEST_PRODUCER)


def test_producers_reject_a_missing_provenance_block():
    with pytest.raises(ValueError, match="authenticated producer"):
        fa._validation_producers({}, TEST_PRODUCER)


def test_analysis_does_not_require_the_checkout_to_be_the_test_producer():
    """The amendment exists because the reader is a later commit than the data.

    Assert the old identity requirement is gone rather than merely bypassed in one
    call site: the module must not compare the reading checkout's HEAD to the
    TEST-producing package commit.
    """
    source = pathlib.Path(fa.__file__).read_text(encoding="utf-8")
    assert "source_identity(require_clean=True)" not in source
    assert "_original_analysis_source_sha256(package_commit)" in source
