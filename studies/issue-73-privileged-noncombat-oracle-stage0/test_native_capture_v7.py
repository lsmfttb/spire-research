from __future__ import annotations

import pytest

import checkpoint_replay_qualification as q
import source_qualification_v7 as v7


def test_native_capture_lock_is_separate_from_reviewed_preflight_lock() -> None:
    original = v7._lock_paths(v7.V7_LOCK_OUT)
    native = v7._lock_paths(v7.V7_CAPTURE_LOCK_OUT)
    assert original != native
    assert original[0] == v7.LOCK_PATH
    assert native[0].is_relative_to(v7.V7_OUT)
    assert native[0] != original[0]


def test_native_capture_requires_exact_local_and_remote_reviewed_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    source_sha = "a" * 40
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> str:
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return source_sha
        if args == ("ls-remote", "origin", f"refs/heads/{q.BRANCH}"):
            return f"{source_sha}\trefs/heads/{q.BRANCH}"
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(q, "git", fake_git)
    assert v7._require_reviewed_source_sha(source_sha) == source_sha
    assert len(calls) == 2

    with pytest.raises(v7.QualificationV7Failure, match="does not equal"):
        v7._require_reviewed_source_sha("b" * 40)

    def moved_remote(*args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return source_sha
        if args == ("ls-remote", "origin", f"refs/heads/{q.BRANCH}"):
            return f"{'c' * 40}\trefs/heads/{q.BRANCH}"
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(q, "git", moved_remote)
    with pytest.raises(v7.QualificationV7Failure, match="remote issue-branch head"):
        v7._require_reviewed_source_sha(source_sha)


def test_native_capture_rejects_missing_review_sha_before_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        v7,
        "_prepare_v7_capture_runtime",
        lambda **_kwargs: pytest.fail("preflight must not run before the reviewed SHA gate"),
    )
    with pytest.raises(v7.QualificationV7Failure, match="exact independently reviewed"):
        v7.capture_source_history_v7(reviewed_source_sha="")
    assert not v7.V7_CAPTURE_ATTEMPT_PATH.exists()


def test_native_capture_cli_requires_reviewed_profile_and_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        v7,
        "capture_source_history_v7",
        lambda **_kwargs: pytest.fail("native capture must not be invoked without a reviewed SHA"),
    )
    with pytest.raises(SystemExit) as legacy_profile:
        v7.main(["capture", "--capture-profile", "reviewed-v7"])
    assert legacy_profile.value.code == 2

    with pytest.raises(SystemExit) as no_sha:
        v7.main(["capture"])
    assert no_sha.value.code == 2


def test_production_launcher_forwards_exact_capture_review_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    import source_qualification_v7

    seen: list[str] = []
    monkeypatch.setattr(source_qualification_v7, "main", lambda argv: seen.extend(argv or []) or 0)
    source_sha = "d" * 40
    assert q.main([
        "--qualification-version", "v7", "capture",
        "--capture-profile", "native-capture-repair1",
        "--reviewed-source-sha", source_sha,
    ]) == 0
    assert seen == [
        "capture",
        "--capture-profile", "native-capture-repair1",
        "--reviewed-source-sha", source_sha,
    ]
