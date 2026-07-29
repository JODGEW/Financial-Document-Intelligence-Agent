"""Persistent comparison entity: create/read/list over SQLite (roadmap step 3).

A ComparisonRecord binds one previous filing and one current filing from the
filing registry into a durable comparison that survives process restarts. This
module implements creation (with strict pair validation), retrieval, and
listing — nothing else. No section alignment, no change detection, no risk
evaluation, no review routing.

ComparisonRecord is NOT a comparison.v1 ComparisonResult
--------------------------------------------------------
The record deliberately has no changes, evidence, validation, risk, or review
fields, so a pre-detection comparison cannot masquerade as a completed one —
persisting a ComparisonResult with ``changes=[]`` would falsely claim a
detector ran and found nothing. The future detector commit consumes a
``ready_for_detection`` record and produces a validated ComparisonResult; that
output gets its own storage when it exists.

Lifecycle (this commit)
-----------------------
- ``ready_for_detection`` — the only state creation produces: the filing pair
  was validated against the registry and persisted; no detector has run.
  Validation happens BEFORE insert, so an unvalidated row is unrepresentable
  and no separate "created" stage exists.
- ``failed`` — reserved for future detection failures (``failure_code`` /
  ``failure_summary`` columns exist now so the schema will not need a
  migration); nothing writes it in this commit.

States like completed/approved/held_for_review are deliberately absent: no
detector, validator, or reviewer has run.

Why SQLite (and not the JSONL pattern)
--------------------------------------
The JSONL stores here are append-mostly logs and queues, and each documents
the same limitation: no cross-process safety, no transactions, "resolved
properly only by a real database" (governance/review_queue.py). A comparison
is exactly that case — a mutable workflow entity that later commits will
transition (detection states), attach one-to-many children to (changes), and
join review/export state onto. stdlib ``sqlite3`` provides transactions and
cross-process correctness with zero new dependencies and no ORM. Filing ids
stay EXTERNAL references (validated against the JSONL registry at create
time) rather than foreign keys: the registry is not mirrored into SQLite, so
there is no local table for an FK to target.

Identity and idempotency
------------------------
``comparison_id`` is deterministic: ``cmp_`` + sha256 over the logical key
(schema_version | workflow_version | previous | current | normalized scope),
matching the repo's content-derived id style (chunk ids, filing ids) — the
same logical comparison keeps its id even across database resets. A UNIQUE
constraint over the same five columns backs it as defense-in-depth. Creating
an existing comparison returns the stored record with ``created=False``;
concurrent identical creates race on ``INSERT .. ON CONFLICT DO NOTHING`` and
exactly one wins, all callers reading back the same row.

Timestamps are timezone-aware UTC ISO strings (audit/review convention).
Connections are opened per operation with ``busy_timeout=5000``; writes run
inside a transaction (``with conn:``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
import filing_registry
from governance.comparison_schema import (
    COMPARISON_SCHEMA_VERSION,
    SECTION_ITEM_1A,
    FilingReference,
    filing_pair_violations,
)

# Version of the (future) comparison workflow whose records this store holds.
# Part of the logical identity key: a new workflow version may legitimately
# re-compare the same pair.
WORKFLOW_VERSION = "comparison_workflow.v1"

STATUS_READY_FOR_DETECTION = "ready_for_detection"
STATUS_FAILED = "failed"
COMPARISON_STATUSES = (STATUS_READY_FOR_DETECTION, STATUS_FAILED)

# Section keys the v1 comparison workflow can actually target. The schema
# itself accepts any non-empty key; this is the workflow capability set.
SUPPORTED_SECTION_KEYS = (SECTION_ITEM_1A,)
DEFAULT_SECTION_SCOPE = [SECTION_ITEM_1A]

# Registry-level reason codes (pair-rule codes come from comparison_schema).
REASON_UNKNOWN_PREVIOUS = "unknown_previous_filing"
REASON_UNKNOWN_CURRENT = "unknown_current_filing"
REASON_PREVIOUS_NOT_PARSED = "previous_filing_not_parsed"
REASON_CURRENT_NOT_PARSED = "current_filing_not_parsed"
REASON_PREVIOUS_CONFLICTED = "previous_filing_identity_conflicted"
REASON_CURRENT_CONFLICTED = "current_filing_identity_conflicted"
REASON_PREVIOUS_INCOMPLETE = "previous_filing_metadata_incomplete"
REASON_CURRENT_INCOMPLETE = "current_filing_metadata_incomplete"
REASON_EMPTY_SCOPE = "empty_section_scope"
REASON_UNSUPPORTED_SCOPE = "unsupported_section_scope"


class ComparisonPairError(ValueError):
    """The requested filing pair is not eligible for a v1 comparison.

    ``reasons`` is a non-empty list of stable machine-readable codes;
    ``detail`` is a safe human-readable summary (no source content, no
    storage internals).
    """

    def __init__(self, reasons: list[str], detail: str):
        super().__init__(detail)
        self.reasons = reasons
        self.detail = detail


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS comparisons (
    comparison_id       TEXT PRIMARY KEY,
    schema_version      TEXT NOT NULL,
    workflow_version    TEXT NOT NULL,
    previous_filing_id  TEXT NOT NULL,
    current_filing_id   TEXT NOT NULL,
    section_scope       TEXT NOT NULL,  -- JSON array, normalized (sorted, deduped)
    status              TEXT NOT NULL
                        CHECK (status IN ('ready_for_detection', 'failed')),
    created_at          TEXT NOT NULL,  -- timezone-aware UTC ISO 8601
    updated_at          TEXT NOT NULL,  -- timezone-aware UTC ISO 8601
    failure_code        TEXT,           -- reserved: future detection failures
    failure_summary     TEXT            -- reserved: safe one-line summary only
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_comparisons_logical_key
    ON comparisons (
        schema_version, workflow_version,
        previous_filing_id, current_filing_id, section_scope
    );
CREATE INDEX IF NOT EXISTS idx_comparisons_status ON comparisons (status);
"""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a per-operation connection with the store's pragmas applied."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    # No foreign keys are declared (filing ids reference the external JSONL
    # registry — module docstring), but enabling the pragma keeps any future
    # child tables honest by default.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path | None = None) -> None:
    """Create the comparisons table and indexes. Idempotent."""
    db_path = db_path or config.COMPARISON_DB_PATH
    with closing(_connect(db_path)) as conn, conn:
        conn.executescript(_SCHEMA_SQL)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_section_scope(section_scope: list[str] | None) -> list[str]:
    """Deterministic scope normalization: strip, dedupe, sort.

    Raises ComparisonPairError for an empty result or unsupported keys —
    the comparison.v1 workflow currently supports only Item 1A Risk Factors.
    """
    cleaned = sorted({key.strip() for key in (section_scope or []) if key.strip()})
    if not cleaned:
        raise ComparisonPairError(
            [REASON_EMPTY_SCOPE], "section_scope must contain at least one section key"
        )
    unsupported = [key for key in cleaned if key not in SUPPORTED_SECTION_KEYS]
    if unsupported:
        raise ComparisonPairError(
            [REASON_UNSUPPORTED_SCOPE],
            f"unsupported section keys {unsupported}; v1 supports "
            f"{list(SUPPORTED_SECTION_KEYS)}",
        )
    return cleaned


def comparison_id_for(
    previous_filing_id: str,
    current_filing_id: str,
    normalized_scope: list[str],
    *,
    schema_version: str = COMPARISON_SCHEMA_VERSION,
    workflow_version: str = WORKFLOW_VERSION,
) -> str:
    """Deterministic id over the logical comparison key (module docstring)."""
    key = "|".join(
        [
            schema_version,
            workflow_version,
            previous_filing_id,
            current_filing_id,
            ",".join(normalized_scope),
        ]
    )
    return f"cmp_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


def _resolve_side(
    side: str, filing_id: str, registry_path: str | Path
) -> tuple[FilingReference | None, list[str]]:
    """Resolve one side of the pair against the registry.

    Returns (reference, reason_codes): a constructed FilingReference and no
    codes when the side is eligible, or None plus the codes explaining why it
    is not. Reasons are registry-truth based: the filing must exist as a
    parsed entry, must not be identity-disputed by a conflict record, and its
    entry must construct a complete FilingReference without guessing.
    """
    unknown, not_parsed, conflicted, incomplete = {
        "previous": (
            REASON_UNKNOWN_PREVIOUS,
            REASON_PREVIOUS_NOT_PARSED,
            REASON_PREVIOUS_CONFLICTED,
            REASON_PREVIOUS_INCOMPLETE,
        ),
        "current": (
            REASON_UNKNOWN_CURRENT,
            REASON_CURRENT_NOT_PARSED,
            REASON_CURRENT_CONFLICTED,
            REASON_CURRENT_INCOMPLETE,
        ),
    }[side]

    matching = [
        entry
        for entry in filing_registry.list_entries(registry_path)
        if entry.get("filing_id") == filing_id
    ]
    if not matching:
        return None, [unknown]

    parsed = [
        entry for entry in matching if entry.get("parse_status") == filing_registry.PARSED
    ]
    if not parsed:
        # The id exists only on duplicate/failed/conflict outcome records —
        # there is no indexed filing behind it.
        return None, [not_parsed]

    # An unresolved conflict record disputes this filing's identity: another
    # source claimed the same company/form/period with different content.
    # Refuse to compare a disputed filing until a human resolves the sources.
    if any(entry.get("parse_status") == filing_registry.CONFLICT for entry in matching):
        return None, [conflicted]

    try:
        return filing_registry.to_filing_reference(parsed[0]), []
    except (ValueError, TypeError):
        return None, [incomplete]


def validate_pair(
    previous_filing_id: str,
    current_filing_id: str,
    registry_path: str | Path,
) -> tuple[FilingReference, FilingReference]:
    """Validate pair eligibility against the registry; return the references.

    Raises ComparisonPairError with every applicable stable reason code. Side
    resolution failures are reported for both sides at once; the cross-filing
    pair rules (identical/company/form/period, shared with comparison.v1)
    run only when both sides resolve.
    """
    previous_ref, previous_reasons = _resolve_side(
        "previous", previous_filing_id, registry_path
    )
    current_ref, current_reasons = _resolve_side(
        "current", current_filing_id, registry_path
    )
    reasons = previous_reasons + current_reasons
    if reasons:
        raise ComparisonPairError(
            reasons,
            "filing pair is not eligible for comparison: " + ", ".join(reasons),
        )

    violations = filing_pair_violations(previous_ref, current_ref)
    if violations:
        raise ComparisonPairError(
            [code for code, _ in violations],
            "; ".join(message for _, message in violations),
        )
    return previous_ref, current_ref


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["section_scope"] = json.loads(record["section_scope"])
    return record


def create_comparison(
    previous_filing_id: str,
    current_filing_id: str,
    section_scope: list[str] | None = None,
    *,
    db_path: str | Path | None = None,
    registry_path: str | Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create (or idempotently return) the comparison for a validated pair.

    Returns (record, created). ``created`` is False when the identical
    logical comparison already exists — the stored record is returned
    unchanged and no new id is minted. Raises ComparisonPairError when the
    pair is ineligible; nothing is persisted in that case.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    registry_path = registry_path or config.FILING_REGISTRY_PATH

    scope = normalize_section_scope(
        section_scope if section_scope is not None else DEFAULT_SECTION_SCOPE
    )
    validate_pair(previous_filing_id, current_filing_id, registry_path)

    comparison_id = comparison_id_for(previous_filing_id, current_filing_id, scope)
    now = _utc_now_iso()
    scope_json = json.dumps(scope)

    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        with conn:  # one transaction for the conditional insert + read-back
            cursor = conn.execute(
                """
                INSERT INTO comparisons (
                    comparison_id, schema_version, workflow_version,
                    previous_filing_id, current_filing_id, section_scope,
                    status, created_at, updated_at, failure_code, failure_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT DO NOTHING
                """,
                (
                    comparison_id,
                    COMPARISON_SCHEMA_VERSION,
                    WORKFLOW_VERSION,
                    previous_filing_id,
                    current_filing_id,
                    scope_json,
                    STATUS_READY_FOR_DETECTION,
                    now,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            row = conn.execute(
                "SELECT * FROM comparisons WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
    return _row_to_record(row), created


def get_comparison(
    comparison_id: str, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    """Fetch one comparison by id, or None when it does not exist."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM comparisons WHERE comparison_id = ?", (comparison_id,)
        ).fetchone()
    return _row_to_record(row) if row else None


def list_comparisons(
    db_path: str | Path | None = None,
    *,
    filing_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List comparisons, newest first, with the minimal stable filter set.

    ``filing_id`` matches either side of the pair; ``status`` matches the
    lifecycle state exactly (unknown status values simply match nothing —
    the API layer constrains them to the known vocabulary).
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    query = "SELECT * FROM comparisons"
    conditions, params = [], []
    if filing_id:
        conditions.append("(previous_filing_id = ? OR current_filing_id = ?)")
        params += [filing_id, filing_id]
    if status:
        conditions.append("status = ?")
        params.append(status)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC, comparison_id"
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_record(row) for row in rows]
