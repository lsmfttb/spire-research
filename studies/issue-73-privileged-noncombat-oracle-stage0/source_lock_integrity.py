"""Pure helpers for exact Git-blob source locks across checkout EOL styles."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from typing import Iterable


class SourceLockValidationError(ValueError):
    """A committed source blob or checkout does not match its locked identity."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def normalize_locked_source_eol(payload: bytes, *, label: str) -> bytes:
    """Canonicalize CRLF only; reject bare CR so other byte changes stay visible."""
    if b"\r" in payload.replace(b"\r\n", b""):
        raise SourceLockValidationError(f"{label} contains a bare CR outside CRLF line endings")
    return payload.replace(b"\r\n", b"\n")


def verify_locked_source_payload(
    *, label: str, expected_sha256: str, committed_blob: bytes, checkout_bytes: bytes,
) -> dict[str, object]:
    """Bind to exact Git blob bytes, then allow only checkout CRLF/LF conversion."""
    blob_sha256 = sha256_bytes(committed_blob)
    if blob_sha256 != expected_sha256:
        raise SourceLockValidationError(
            f"{label} committed Git blob SHA256 differs from the exact source lock: "
            f"{blob_sha256} != {expected_sha256}"
        )
    canonical_blob = normalize_locked_source_eol(committed_blob, label=f"{label} Git blob")
    canonical_checkout = normalize_locked_source_eol(checkout_bytes, label=f"{label} checkout")
    if canonical_checkout != canonical_blob:
        raise SourceLockValidationError(
            f"{label} checkout differs from the exact committed Git blob beyond CRLF/LF normalization"
        )
    return {
        "committed_git_blob_sha256": blob_sha256,
        "checkout_raw_sha256": sha256_bytes(checkout_bytes),
        "checkout_normalized_sha256": sha256_bytes(canonical_checkout),
        "committed_blob_bytes": len(committed_blob),
        "checkout_bytes": len(checkout_bytes),
        "checkout_crlf_count": checkout_bytes.count(b"\r\n"),
        "checkout_bare_cr_count": checkout_bytes.replace(b"\r\n", b"").count(b"\r"),
        "status": "PASS_EXACT_GIT_BLOB_WITH_EOL_ONLY_CHECKOUT_CONVERSION",
    }


def resolve_exact_python_package_root(
    source_root: str | Path,
    package_name: str,
    sys_path: Iterable[str],
) -> Path:
    """Require an importable package to resolve entirely under ``source_root/src``."""
    root = Path(source_root).resolve()
    package_root = (root / "src").resolve()
    if not (package_root / package_name).is_dir():
        raise SourceLockValidationError(f"{package_root} does not contain {package_name}")
    if not any(Path(entry or ".").resolve() == package_root for entry in sys_path):
        raise SourceLockValidationError(f"{package_root} is absent from the Python import path")
    try:
        spec = importlib.util.find_spec(package_name)
    except (ImportError, ValueError) as exc:
        raise SourceLockValidationError(f"{package_name} cannot be resolved: {exc}") from exc
    if spec is None:
        raise SourceLockValidationError(f"{package_name} cannot be resolved from {package_root}")
    locations = list(spec.submodule_search_locations or [])
    if spec.origin:
        locations.append(str(Path(spec.origin).resolve().parent))
    if not any(Path(location).resolve().is_relative_to(package_root) for location in locations):
        raise SourceLockValidationError(f"resolved {package_name} package is outside {package_root}")
    return package_root


def adapter_matches_history_target(candidate: object, expected: object) -> bool:
    """Accept the exact history adapter or the pinned single metering wrapper."""
    return candidate is expected or getattr(candidate, "_adapter", None) is expected
