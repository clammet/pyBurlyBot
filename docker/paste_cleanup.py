#!/usr/bin/env python
"""Periodically remove expired static paste files from the shared volume."""

import os
import time
from pathlib import Path


PASTE_SUFFIXES = frozenset({".html", ".txt"})


def remove_expired_pastes(root: Path, cutoff: float) -> int:
    """Remove immutable paste files older than ``cutoff`` and return the count."""
    removed = 0
    for path in root.iterdir():
        if path.suffix not in PASTE_SUFFIXES or not path.is_file():
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
        except FileNotFoundError:
            # The bot or another cleanup process may have raced with this pass.
            continue
        removed += 1
    return removed


def positive_int_from_env(name: str, default: int) -> int:
    """Read a positive integer environment setting."""
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def main() -> None:
    root = Path(os.environ.get("PYBB_PASTE_ROOT", "shared/pastes"))
    retention_days = positive_int_from_env("PYBB_PASTE_RETENTION_DAYS", 365)
    interval = positive_int_from_env("PYBB_PASTE_CLEANUP_INTERVAL_SECONDS", 86400)
    retention_seconds = retention_days * 24 * 60 * 60

    root.mkdir(parents=True, exist_ok=True)
    while True:
        removed = remove_expired_pastes(root, time.time() - retention_seconds)
        print(f"paste-cleanup: removed {removed} expired paste(s)", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
