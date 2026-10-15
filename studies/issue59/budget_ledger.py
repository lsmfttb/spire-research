from __future__ import annotations

import hashlib
import os
from pathlib import Path


def ledger_path(explicit: str | Path | None = None) -> Path:
    """Resolve one ledger path consistently for study and wrapper processes."""
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    configured = os.environ.get("ISSUE59_BUDGET_LEDGER")
    path = Path(configured).expanduser() if configured is not None else Path(__file__).with_name("budget_events.jsonl")
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path.resolve()


def sha256_jsonl(path: str | Path) -> str:
    """Hash JSONL bytes canonically so Git newline conversion is transparent."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for line in handle:
            digest.update(line.replace(b"\r\n", b"\n"))
    return digest.hexdigest()
