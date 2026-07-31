#!/usr/bin/env python3
"""Back up and verify the comparison **workflow database** only.

Scope, stated plainly so it cannot be misread: this copies the SQLite database
holding comparisons, detection jobs, job events, attempts, replays, governance
evaluations, review items and events, and results. It does **not** cover the
filing registry (``filing_registry/registry.jsonl``) or the vector store
(``chroma_db/``). Those are separate artifacts on separate write paths, and
restoring this database alone will not reconstitute a working filing workflow —
a restored database can reference filings the registry and index no longer
describe. Treat a full recovery as a coordinated restore of all three, taken at
a consistent point; see OPERATIONS.md.

Calling this a "workflow-database backup" rather than a system backup is the
whole point. A SQLite-only copy presented as a filing-workflow backup would be
a false assurance at exactly the moment it mattered.

    python scripts/backup_workflow_db.py --db-path <db> --out <file>
    python scripts/backup_workflow_db.py --db-path <db> --out <file> --force
    python scripts/backup_workflow_db.py --verify <file>

The copy is taken with SQLite's own online backup API, so it is consistent
without quiescing writers, and the source database is never modified. Every
backup is integrity-checked before the command reports success. Output carries
counts and stable codes only — never an absolute path, SQL, schema text, an
auth secret, or an access token.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import comparison_store  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_INVALID = 2

CODE_SOURCE_UNAVAILABLE = "backup_source_unavailable"
CODE_SOURCE_SCHEMA_INCOMPLETE = "backup_source_schema_incomplete"
CODE_DESTINATION_EXISTS = "backup_destination_exists"
CODE_BACKUP_FAILED = "backup_failed"
CODE_INTEGRITY_FAILED = "backup_integrity_check_failed"

# What this backup covers, and — just as importantly — what it does not.
COVERS = ("comparison_workflow_database",)
DOES_NOT_COVER = ("filing_registry", "vector_store")


def _fail(code: str, message: str) -> int:
    print(json.dumps({"status": "refused", "code": code, "message": message}))
    return EXIT_REFUSED


def _row_counts(db_path: Path) -> dict[str, int]:
    """Counts per required workflow table, read through a read-only connection."""
    counts: dict[str, int] = {}
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        for table in sorted(comparison_store.RELIABILITY_REQUIRED_TABLES):
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
    return counts


def _integrity_ok(db_path: Path) -> bool:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
    return integrity == "ok" and not foreign


def _verify(path: Path) -> int:
    """Prove a backup file is an independently readable, coherent database."""
    if not path.is_file():
        return _fail(CODE_SOURCE_UNAVAILABLE, "the backup file does not exist")
    try:
        missing = comparison_store.probe_readonly_schema(path)
    except comparison_store.ReliabilityStorageUnavailable:
        return _fail(CODE_SOURCE_UNAVAILABLE, "the backup could not be opened")
    if missing:
        return _fail(
            CODE_SOURCE_SCHEMA_INCOMPLETE,
            "the backup is missing a required workflow table",
        )
    if not _integrity_ok(path):
        return _fail(CODE_INTEGRITY_FAILED, "the backup failed integrity_check")
    print(
        json.dumps(
            {
                "status": "verified",
                "scope": "workflow_database_only",
                "covers": list(COVERS),
                "doesNotCover": list(DOES_NOT_COVER),
                "integrityCheck": "ok",
                "foreignKeyCheck": "ok",
                "rowCounts": _row_counts(path),
            },
            sort_keys=True,
        )
    )
    return EXIT_OK


def _backup(db_path: Path, out_path: Path, *, force: bool) -> int:
    if not db_path.is_file():
        return _fail(
            CODE_SOURCE_UNAVAILABLE, "the source workflow database does not exist"
        )
    try:
        missing = comparison_store.probe_readonly_schema(db_path)
    except comparison_store.ReliabilityStorageUnavailable:
        return _fail(
            CODE_SOURCE_UNAVAILABLE, "the source database could not be opened"
        )
    if missing:
        return _fail(
            CODE_SOURCE_SCHEMA_INCOMPLETE,
            "the source is missing a required workflow table",
        )
    if out_path.exists() and not force:
        return _fail(
            CODE_DESTINATION_EXISTS,
            "the destination already exists; pass --force to replace it",
        )

    source_before = db_path.read_bytes()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    staging = out_path.with_name(out_path.name + ".partial")
    try:
        # SQLite's online backup API: a consistent copy without quiescing
        # writers, and strictly read-only with respect to the source.
        source_uri = f"file:{db_path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source, closing(
            sqlite3.connect(staging)
        ) as destination:
            source.backup(destination)
    except (sqlite3.Error, OSError):
        staging.unlink(missing_ok=True)
        return _fail(CODE_BACKUP_FAILED, "the backup could not be completed")

    if not _integrity_ok(staging):
        staging.unlink(missing_ok=True)
        return _fail(CODE_INTEGRITY_FAILED, "the backup failed integrity_check")

    staging.replace(out_path)

    # The source must be byte-identical to what it was before this command ran.
    if db_path.read_bytes() != source_before:
        return _fail(CODE_BACKUP_FAILED, "the source database changed unexpectedly")

    print(
        json.dumps(
            {
                "status": "created",
                "scope": "workflow_database_only",
                "covers": list(COVERS),
                "doesNotCover": list(DOES_NOT_COVER),
                "integrityCheck": "ok",
                "foreignKeyCheck": "ok",
                "sourceUnmodified": True,
                "rowCounts": _row_counts(out_path),
            },
            sort_keys=True,
        )
    )
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Back up or verify the comparison workflow database (SQLite only). "
            "The filing registry and vector store require separate coordinated "
            "backup; this is not a full filing-workflow backup."
        )
    )
    parser.add_argument("--db-path", metavar="PATH")
    parser.add_argument("--out", metavar="PATH")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing destination file (refused by default)",
    )
    parser.add_argument(
        "--verify", metavar="PATH", help="verify an existing backup instead"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.verify:
        if args.db_path or args.out:
            print(
                "error: --verify does not accept --db-path or --out",
                file=sys.stderr,
            )
            return EXIT_INVALID
        return _verify(Path(args.verify))
    if not args.db_path or not args.out:
        print("error: --db-path and --out are required", file=sys.stderr)
        return EXIT_INVALID
    return _backup(Path(args.db_path), Path(args.out), force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
