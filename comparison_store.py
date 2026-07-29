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

# Version of the comparison workflow whose records this store holds. Part of
# the logical identity key: a new workflow version legitimately re-compares
# the same pair under a NEW comparison id, which is exactly how results
# produced by an older detector version get re-detected without ever being
# overwritten (v2: detector gained the citation/numeric/direction validators).
WORKFLOW_VERSION = "comparison_workflow.v2"

STATUS_READY_FOR_DETECTION = "ready_for_detection"
STATUS_DETECTED = "detected"
STATUS_FAILED = "failed"
COMPARISON_STATUSES = (STATUS_READY_FOR_DETECTION, STATUS_DETECTED, STATUS_FAILED)

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


class ComparisonResultExists(Exception):
    """A detection result is already stored; results are never overwritten."""


class ComparisonLifecycleError(Exception):
    """The comparison is not in a state that allows the requested transition."""

    def __init__(self, comparison_id: str, status: str, message: str):
        super().__init__(message)
        self.comparison_id = comparison_id
        self.status = status


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


_COMPARISONS_DDL = """
CREATE TABLE IF NOT EXISTS comparisons (
    comparison_id       TEXT PRIMARY KEY,
    schema_version      TEXT NOT NULL,
    workflow_version    TEXT NOT NULL,
    previous_filing_id  TEXT NOT NULL,
    current_filing_id   TEXT NOT NULL,
    section_scope       TEXT NOT NULL,  -- JSON array, normalized (sorted, deduped)
    status              TEXT NOT NULL
                        CHECK (status IN ('ready_for_detection', 'detected', 'failed')),
    created_at          TEXT NOT NULL,  -- timezone-aware UTC ISO 8601
    updated_at          TEXT NOT NULL,  -- timezone-aware UTC ISO 8601
    failure_code        TEXT,           -- stable code when status='failed'
    failure_summary     TEXT            -- safe one-line summary only
)
"""

_SCHEMA_SQL = f"""
{_COMPARISONS_DDL};
CREATE UNIQUE INDEX IF NOT EXISTS idx_comparisons_logical_key
    ON comparisons (
        schema_version, workflow_version,
        previous_filing_id, current_filing_id, section_scope
    );
CREATE INDEX IF NOT EXISTS idx_comparisons_status ON comparisons (status);
CREATE TABLE IF NOT EXISTS comparison_results (
    comparison_id        TEXT PRIMARY KEY
                         REFERENCES comparisons (comparison_id),
    schema_version       TEXT NOT NULL,
    detector_version     TEXT NOT NULL,
    previous_source_hash TEXT NOT NULL,  -- registry source_hash at detect time
    current_source_hash  TEXT NOT NULL,  -- registry source_hash at detect time
    result_json          TEXT NOT NULL,  -- canonical comparison.v1 wire document
    result_hash          TEXT NOT NULL,  -- sha256 over the timestamp-independent form
    created_at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comparison_governance_evaluations (
    -- NOT NULL is load-bearing: SQLite's non-INTEGER PRIMARY KEYs otherwise
    -- accept NULL (historic quirk kept for compatibility).
    evaluation_id          TEXT PRIMARY KEY NOT NULL,  -- gov_<sha256(...)[:16]>
    comparison_id          TEXT NOT NULL
                           REFERENCES comparisons (comparison_id),
    comparison_result_hash TEXT NOT NULL,     -- must match comparison_results
    policy_id              TEXT NOT NULL,
    policy_version         TEXT NOT NULL,
    risk_score             REAL NOT NULL,
    risk_level             TEXT NOT NULL,
    decision               TEXT NOT NULL,
    reason_codes           TEXT NOT NULL,     -- JSON array of stable codes
    evaluated_at           TEXT NOT NULL,     -- timezone-aware UTC ISO 8601
    governed_result_json   TEXT NOT NULL,     -- validated comparison.v1 snapshot
    governed_result_hash   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_governance_logical_key
    ON comparison_governance_evaluations (
        comparison_id, comparison_result_hash, policy_id, policy_version
    );
CREATE TABLE IF NOT EXISTS comparison_review_items (
    review_id              TEXT PRIMARY KEY NOT NULL,  -- crev_<same suffix>
    comparison_id          TEXT NOT NULL
                           REFERENCES comparisons (comparison_id),
    evaluation_id          TEXT NOT NULL UNIQUE
                           REFERENCES comparison_governance_evaluations (evaluation_id),
    comparison_result_hash TEXT NOT NULL,
    governed_result_hash   TEXT NOT NULL,
    status                 TEXT NOT NULL
                           CHECK (status IN ('pending', 'approved', 'rejected')),
    terminal_event_id      TEXT REFERENCES comparison_review_events (event_id),
    decided_at             TEXT,
    created_at             TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comparison_review_events (
    -- Append-only decision history: no update or delete path exists in this
    -- codebase. These are application records, NOT tamper-proof audit
    -- storage — anyone with file access can alter a local SQLite database.
    event_id                      TEXT PRIMARY KEY NOT NULL,  -- rev_evt_<sha[:16]>
    review_id                     TEXT NOT NULL
                                  REFERENCES comparison_review_items (review_id),
    comparison_id                 TEXT NOT NULL
                                  REFERENCES comparisons (comparison_id),
    evaluation_id                 TEXT NOT NULL
                                  REFERENCES comparison_governance_evaluations (evaluation_id),
    action                        TEXT NOT NULL
                                  CHECK (action IN ('approved', 'rejected')),
    reviewer_id                   TEXT NOT NULL,  -- self-asserted local id, NOT authenticated
    reason_code                   TEXT NOT NULL,  -- allowlisted stable code
    reviewer_note                 TEXT NOT NULL,  -- bounded reviewer prose
    request_hash                  TEXT NOT NULL,  -- canonical request, for idempotent replay
    original_governed_result_hash TEXT NOT NULL,
    final_reviewed_result_hash    TEXT NOT NULL,
    reviewed_result_json          TEXT NOT NULL,  -- complete final comparison.v1 snapshot
    edit_summary_json             TEXT,           -- JSON array of change_id/original/new summaries
    created_at                    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_events_terminal
    ON comparison_review_events (review_id);
"""


def _migrate_review_items_vocabulary(db_path: Path) -> None:
    """Rebuild comparison_review_items if its CHECK predates decisions.

    Idempotent, same pattern as the comparisons status migration: only a
    database whose stored DDL lacks 'approved' is rebuilt (pending-only
    schema); existing pending rows are preserved with NULL terminal fields.
    Plain connection (no FK pragma) so the rebuild is unconstrained.
    """
    if not db_path.exists():
        return
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='comparison_review_items'"
        ).fetchone()
        if row is None or "'approved'" in (row[0] or ""):
            return
        with conn:
            conn.execute(
                "ALTER TABLE comparison_review_items "
                "RENAME TO comparison_review_items_migrating"
            )
            conn.execute(
                """
                CREATE TABLE comparison_review_items (
                    review_id              TEXT PRIMARY KEY NOT NULL,
                    comparison_id          TEXT NOT NULL
                                           REFERENCES comparisons (comparison_id),
                    evaluation_id          TEXT NOT NULL UNIQUE
                                           REFERENCES comparison_governance_evaluations (evaluation_id),
                    comparison_result_hash TEXT NOT NULL,
                    governed_result_hash   TEXT NOT NULL,
                    status                 TEXT NOT NULL
                                           CHECK (status IN ('pending', 'approved', 'rejected')),
                    terminal_event_id      TEXT REFERENCES comparison_review_events (event_id),
                    decided_at             TEXT,
                    created_at             TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO comparison_review_items ("
                "    review_id, comparison_id, evaluation_id,"
                "    comparison_result_hash, governed_result_hash, status,"
                "    terminal_event_id, decided_at, created_at"
                ") SELECT review_id, comparison_id, evaluation_id,"
                "    comparison_result_hash, governed_result_hash, status,"
                "    NULL, NULL, created_at "
                "FROM comparison_review_items_migrating"
            )
            conn.execute("DROP TABLE comparison_review_items_migrating")


def _migrate_status_vocabulary(db_path: Path) -> None:
    """Rebuild the comparisons table if its CHECK predates 'detected'.

    Idempotent: inspects the stored DDL and only rebuilds a database created
    by the pre-detector schema (whose CHECK listed only ready_for_detection
    and failed — SQLite cannot ALTER a CHECK in place). Runs on a plain
    connection (no foreign_keys pragma) so the rebuild is unconstrained; the
    old schema had no child tables.
    """
    if not db_path.exists():
        return
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='comparisons'"
        ).fetchone()
        if row is None or "'detected'" in (row[0] or ""):
            return
        with conn:
            conn.execute("ALTER TABLE comparisons RENAME TO comparisons_migrating")
            conn.execute(_COMPARISONS_DDL)
            conn.execute(
                "INSERT INTO comparisons SELECT * FROM comparisons_migrating"
            )
            conn.execute("DROP TABLE comparisons_migrating")


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
    """Create tables/indexes and run the idempotent migrations."""
    db_path = Path(db_path or config.COMPARISON_DB_PATH)
    _migrate_status_vocabulary(db_path)
    _migrate_review_items_vocabulary(db_path)
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


# --- Detection results and lifecycle transitions -----------------------------


def record_result(
    comparison_id: str,
    *,
    result_json: str,
    result_hash: str,
    detector_version: str,
    previous_source_hash: str,
    current_source_hash: str,
    db_path: str | Path | None = None,
) -> None:
    """Persist a detection result and transition the comparison to detected.

    One BEGIN IMMEDIATE transaction covers the lifecycle check, the result
    insert, and the status update, so concurrent detections serialize: the
    loser observes the winner's committed result and gets
    ComparisonResultExists (results are never overwritten). A comparison that
    is not ready_for_detection raises ComparisonLifecycleError.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    now = _utc_now_iso()
    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None  # manual transaction control
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM comparisons WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
            if row is None:
                raise ComparisonLifecycleError(
                    comparison_id, "absent", f"unknown comparison {comparison_id!r}"
                )
            existing = conn.execute(
                "SELECT 1 FROM comparison_results WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
            if existing is not None:
                raise ComparisonResultExists(comparison_id)
            if row["status"] != STATUS_READY_FOR_DETECTION:
                raise ComparisonLifecycleError(
                    comparison_id,
                    row["status"],
                    f"comparison is {row['status']}, not ready_for_detection",
                )
            conn.execute(
                """
                INSERT INTO comparison_results (
                    comparison_id, schema_version, detector_version,
                    previous_source_hash, current_source_hash,
                    result_json, result_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comparison_id,
                    COMPARISON_SCHEMA_VERSION,
                    detector_version,
                    previous_source_hash,
                    current_source_hash,
                    result_json,
                    result_hash,
                    now,
                ),
            )
            conn.execute(
                "UPDATE comparisons SET status = ?, updated_at = ? "
                "WHERE comparison_id = ? AND status = ?",
                (STATUS_DETECTED, now, comparison_id, STATUS_READY_FOR_DETECTION),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def get_result(
    comparison_id: str, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    """Fetch the stored detection result, or None. ``result`` is the parsed
    comparison.v1 wire document; hashes/versions ride alongside for staleness
    checks."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM comparison_results WHERE comparison_id = ?",
            (comparison_id,),
        ).fetchone()
    if row is None:
        return None
    stored = dict(row)
    stored["result"] = json.loads(stored.pop("result_json"))
    return stored


def record_evaluation(
    *,
    comparison_id: str,
    evaluation_id: str,
    comparison_result_hash: str,
    policy_id: str,
    policy_version: str,
    risk_score: float,
    risk_level: str,
    decision: str,
    reason_codes: list[str],
    governed_result_json: str,
    governed_result_hash: str,
    review_id: str | None,
    db_path: str | Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Persist an immutable governance evaluation (+ pending review if held).

    One BEGIN IMMEDIATE transaction covers: the stale-hash guard (the
    evaluation must attach to the comparison's CURRENT stored result hash),
    the idempotent evaluation insert, and — for held_for_review only — the
    single pending comparison_review_items row. Everything commits or rolls
    back together; concurrent identical evaluations serialize and yield one
    row each. Returns (stored_evaluation, created).
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    now = _utc_now_iso()
    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                "SELECT result_hash FROM comparison_results WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
            if current is None:
                raise ComparisonLifecycleError(
                    comparison_id, "no_result", "no detector result exists"
                )
            if current["result_hash"] != comparison_result_hash:
                raise ComparisonLifecycleError(
                    comparison_id,
                    "stale_result_hash",
                    "the evaluation targets a result hash that is not the "
                    "comparison's current stored result",
                )

            cursor = conn.execute(
                """
                INSERT INTO comparison_governance_evaluations (
                    evaluation_id, comparison_id, comparison_result_hash,
                    policy_id, policy_version, risk_score, risk_level,
                    decision, reason_codes, evaluated_at,
                    governed_result_json, governed_result_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    evaluation_id,
                    comparison_id,
                    comparison_result_hash,
                    policy_id,
                    policy_version,
                    risk_score,
                    risk_level,
                    decision,
                    json.dumps(reason_codes),
                    now,
                    governed_result_json,
                    governed_result_hash,
                ),
            )
            created = cursor.rowcount == 1
            if created and decision == "held_for_review":
                conn.execute(
                    """
                    INSERT INTO comparison_review_items (
                        review_id, comparison_id, evaluation_id,
                        comparison_result_hash, governed_result_hash,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        review_id,
                        comparison_id,
                        evaluation_id,
                        comparison_result_hash,
                        governed_result_hash,
                        now,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM comparison_governance_evaluations "
                "WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return _evaluation_row_to_record(row), created


def _evaluation_row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["reason_codes"] = json.loads(record["reason_codes"])
    record["governed_result"] = json.loads(record.pop("governed_result_json"))
    return record


def get_evaluation(
    evaluation_id: str, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    """Fetch one immutable governance evaluation by id, or None."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM comparison_governance_evaluations WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
    return _evaluation_row_to_record(row) if row else None


def list_evaluations(
    comparison_id: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Every stored evaluation for a comparison (all policy versions), oldest
    first — old-policy evaluations remain readable forever."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM comparison_governance_evaluations "
            "WHERE comparison_id = ? ORDER BY evaluated_at, evaluation_id",
            (comparison_id,),
        ).fetchall()
    return [_evaluation_row_to_record(row) for row in rows]


def list_comparison_reviews(
    db_path: str | Path | None = None, *, comparison_id: str | None = None
) -> list[dict[str, Any]]:
    """Pending comparison review items (newest first), optionally filtered.

    Summary rows only — the governed result lives on the evaluation; review
    rows reference it by hash rather than copying evidence.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    query = (
        "SELECT r.*, e.risk_score, e.risk_level, e.reason_codes "
        "FROM comparison_review_items r "
        "JOIN comparison_governance_evaluations e "
        "ON e.evaluation_id = r.evaluation_id "
        "WHERE r.status = 'pending'"
    )
    params: list[Any] = []
    if comparison_id:
        query += " AND r.comparison_id = ?"
        params.append(comparison_id)
    query += " ORDER BY r.created_at DESC, r.review_id"
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(query, params).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["reason_codes"] = json.loads(item["reason_codes"])
        items.append(item)
    return items


class ReviewAlreadyDecided(Exception):
    """The review already has a terminal decision from a DIFFERENT request.

    A byte-equivalent replay is handled idempotently before this is raised.
    """

    def __init__(self, review_id: str, status: str):
        super().__init__(f"review is already {status}")
        self.review_id = review_id
        self.status = status


def get_review_item(
    review_id: str, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    """Fetch one comparison review item by id (any status), or None."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM comparison_review_items WHERE review_id = ?",
            (review_id,),
        ).fetchone()
    return dict(row) if row else None


def _event_row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["reviewed_result"] = json.loads(record.pop("reviewed_result_json"))
    record["edits"] = (
        json.loads(record.pop("edit_summary_json"))
        if record.get("edit_summary_json")
        else []
    )
    record.pop("edit_summary_json", None)
    return record


def list_review_events(
    review_id: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Append-only events for one review, oldest first (currently 0 or 1)."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM comparison_review_events WHERE review_id = ? "
            "ORDER BY created_at, event_id",
            (review_id,),
        ).fetchall()
    return [_event_row_to_record(row) for row in rows]


def decide_review(
    review_id: str,
    *,
    event_id: str,
    action: str,
    reviewer_id: str,
    reason_code: str,
    reviewer_note: str,
    request_hash: str,
    original_governed_result_hash: str,
    final_reviewed_result_hash: str,
    reviewed_result_json: str,
    edit_summary_json: str | None,
    db_path: str | Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Record one terminal decision: append the event and transition the item.

    One BEGIN IMMEDIATE transaction covers the pending check, the append-only
    event insert, and the item transition, so concurrent decisions serialize:
    exactly one wins; a loser whose request is byte-equivalent (same
    request_hash) gets the stored event back with created=False; any other
    loser gets ReviewAlreadyDecided. There is no update or delete path for
    events — decisions are never overwritten.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    now = _utc_now_iso()
    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            item = conn.execute(
                "SELECT * FROM comparison_review_items WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            if item is None:
                raise ComparisonLifecycleError(
                    review_id, "absent", f"unknown review item {review_id!r}"
                )
            if item["status"] != "pending":
                existing = conn.execute(
                    "SELECT * FROM comparison_review_events WHERE review_id = ?",
                    (review_id,),
                ).fetchone()
                if (
                    existing is not None
                    and existing["request_hash"] == request_hash
                ):
                    record = _event_row_to_record(existing)
                    conn.execute("COMMIT")
                    return record, False
                raise ReviewAlreadyDecided(review_id, item["status"])

            conn.execute(
                """
                INSERT INTO comparison_review_events (
                    event_id, review_id, comparison_id, evaluation_id,
                    action, reviewer_id, reason_code, reviewer_note,
                    request_hash, original_governed_result_hash,
                    final_reviewed_result_hash, reviewed_result_json,
                    edit_summary_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    review_id,
                    item["comparison_id"],
                    item["evaluation_id"],
                    action,
                    reviewer_id,
                    reason_code,
                    reviewer_note,
                    request_hash,
                    original_governed_result_hash,
                    final_reviewed_result_hash,
                    reviewed_result_json,
                    edit_summary_json,
                    now,
                ),
            )
            conn.execute(
                "UPDATE comparison_review_items SET status = ?, "
                "terminal_event_id = ?, decided_at = ? "
                "WHERE review_id = ? AND status = 'pending'",
                (action, event_id, now, review_id),
            )
            row = conn.execute(
                "SELECT * FROM comparison_review_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return _event_row_to_record(row), True


def mark_failed(
    comparison_id: str,
    failure_code: str,
    failure_summary: str,
    db_path: str | Path | None = None,
) -> bool:
    """Transition ready_for_detection -> failed with a stable code + safe
    summary. Returns False (and changes nothing) when the comparison is not
    in ready_for_detection — a detected result is never clobbered."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn, conn:
        cursor = conn.execute(
            "UPDATE comparisons SET status = ?, failure_code = ?, "
            "failure_summary = ?, updated_at = ? "
            "WHERE comparison_id = ? AND status = ?",
            (
                STATUS_FAILED,
                failure_code,
                failure_summary[:200],
                _utc_now_iso(),
                comparison_id,
                STATUS_READY_FOR_DETECTION,
            ),
        )
        return cursor.rowcount == 1
