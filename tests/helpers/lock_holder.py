"""Test-only process that deliberately holds a SQLite write transaction.

``BEGIN IMMEDIATE`` takes the RESERVED lock at once, so every writer in the
comparison store — which opens its own transactions the same way — contends
with this process for a bounded, controlled interval. That is what lets a test
observe the store's existing ``busy_timeout`` behaviour across real processes
instead of simulating it.

The transaction is always rolled back, so this process never changes a byte of
workflow state.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="test-only SQLite lock holder")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--hold-seconds", type=float, required=True)
    parser.add_argument("--ready-file", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with closing(sqlite3.connect(args.db_path, isolation_level=None)) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            Path(args.ready_file).write_text("locked", encoding="utf-8")
            time.sleep(args.hold_seconds)
        finally:
            # Never commit: the lock is the entire purpose, the write is not.
            conn.execute("ROLLBACK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
