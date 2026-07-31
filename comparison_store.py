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
import hmac
import json
import secrets
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timedelta, timezone
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
STATUS_QUEUED_FOR_DETECTION = "queued_for_detection"
STATUS_DETECTING = "detecting"
STATUS_DETECTED = "detected"
STATUS_FAILED = "failed"
COMPARISON_STATUSES = (
    STATUS_READY_FOR_DETECTION,
    STATUS_QUEUED_FOR_DETECTION,
    STATUS_DETECTING,
    STATUS_DETECTED,
    STATUS_FAILED,
)

# Detection-attempt states (Stage 3.5 reliability step 1).
ATTEMPT_RUNNING = "running"
ATTEMPT_SUCCEEDED = "succeeded"
ATTEMPT_FAILED = "failed"
# Retired by an explicit operator replay after the configured stale threshold
# (Stage 3.5 step 2). Terminal, and reachable ONLY from inside the replay
# transaction — never from age alone and never because a GET observed the row.
ATTEMPT_TIMED_OUT = "timed_out"
ATTEMPT_STATUSES = (
    ATTEMPT_RUNNING,
    ATTEMPT_SUCCEEDED,
    ATTEMPT_FAILED,
    ATTEMPT_TIMED_OUT,
)

EVENT_DETECTION_STARTED = "detection_started"
EVENT_DETECTION_SUCCEEDED = "detection_succeeded"
EVENT_DETECTION_FAILED = "detection_failed"
EVENT_DETECTION_TIMED_OUT = "detection_timed_out"
DETECTION_EVENT_TYPES = (
    EVENT_DETECTION_STARTED,
    EVENT_DETECTION_SUCCEEDED,
    EVENT_DETECTION_FAILED,
    EVENT_DETECTION_TIMED_OUT,
)

# Durable initial-detection jobs (Stage 3.5 reliability step 5). Only the
# authenticated API enqueue path creates these rows. Direct library detection
# and operator replay remain synchronous and create attempts without jobs.
JOB_TRIGGER_INITIAL_DETECTION = "initial_detection"
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"
JOB_STATUSES = (JOB_QUEUED, JOB_RUNNING, JOB_SUCCEEDED, JOB_FAILED)

EVENT_JOB_QUEUED = "detection_job_queued"
EVENT_JOB_CLAIMED = "detection_job_claimed"
EVENT_JOB_HEARTBEAT = "detection_job_heartbeat"
EVENT_JOB_RECLAIMED = "detection_job_reclaimed"
EVENT_JOB_CLAIM_EXHAUSTED = "detection_job_claim_exhausted"
EVENT_JOB_SUCCEEDED = "detection_job_succeeded"
EVENT_JOB_FAILED = "detection_job_failed"
JOB_EVENT_TYPES = (
    EVENT_JOB_QUEUED,
    EVENT_JOB_CLAIMED,
    EVENT_JOB_HEARTBEAT,
    EVENT_JOB_RECLAIMED,
    EVENT_JOB_CLAIM_EXHAUSTED,
    EVENT_JOB_SUCCEEDED,
    EVENT_JOB_FAILED,
)

# Deterministic event ordering key: started is always 0, the single terminal
# event is always 1, so ordering never depends on timestamp resolution.
_EVENT_SEQ = {
    EVENT_DETECTION_STARTED: 0,
    EVENT_DETECTION_SUCCEEDED: 1,
    EVENT_DETECTION_FAILED: 1,
    EVENT_DETECTION_TIMED_OUT: 1,
}

# Stable reason codes for detection-attempt state errors (API: 409).
REASON_COMPARISON_NOT_READY = "comparison_not_ready"
REASON_DETECTION_IN_PROGRESS = "detection_in_progress"
REASON_ATTEMPT_NOT_FOUND = "detection_attempt_not_found"
REASON_ATTEMPT_NOT_RUNNING = "detection_attempt_not_running"
REASON_TRANSITION_INVALID = "detection_transition_invalid"
REASON_INPUTS_CHANGED = "detection_inputs_changed"
# Replay-specific codes (Stage 3.5 step 2).
REASON_ATTEMPT_NOT_STALE = "detection_attempt_not_stale"
REASON_ATTEMPT_LIMIT_REACHED = "detection_attempt_limit_reached"
REASON_REPLAY_ALREADY_EXISTS = "detection_replay_already_exists"
REASON_REPLAY_INPUTS_CHANGED = "detection_replay_inputs_changed"
REASON_REPLAY_VERSION_CHANGED = "detection_replay_version_changed"
REASON_JOB_ACTIVE_CONFLICT = "detection_job_conflict"
REASON_JOB_NOT_FOUND = "detection_job_not_found"
REASON_JOB_NOT_QUEUED = "detection_job_not_queued"
REASON_JOB_NOT_RUNNING = "detection_job_not_running"
REASON_JOB_CLAIM_INVALID = "detection_job_claim_invalid"
REASON_JOB_WORKER_MISMATCH = "detection_job_worker_mismatch"
REASON_JOB_ATTEMPT_MISMATCH = "detection_job_attempt_mismatch"
REASON_JOB_RESULT_HASH_MISMATCH = "detection_job_result_hash_mismatch"
REASON_JOB_INPUTS_CHANGED = "detection_job_inputs_changed"
REASON_JOB_VERSION_CHANGED = "detection_job_version_changed"
REASON_ATTEMPT_MANAGED_BY_JOB = "detection_attempt_managed_by_job"
ATTEMPT_MANAGED_BY_JOB_MESSAGE = (
    "this detection attempt is managed by a worker job and must recover "
    "through fenced lease reclaim"
)
REASON_JOB_LEASE_EXPIRED = "detection_job_lease_expired"
REASON_JOB_CLAIM_FENCED = "detection_job_claim_fenced"
REASON_JOB_CLAIMS_EXHAUSTED = "detection_job_claims_exhausted"
REASON_JOB_CLOCK_INVALID = "detection_job_clock_invalid"
REASON_RESULT_INPUTS_STALE = "comparison_inputs_stale"
REASON_RESULT_VERSION_SUPERSEDED = "detector_version_superseded"

MAX_FAILURE_SUMMARY_CHARS = 200
MAX_WORKER_ID_CHARS = 120

# The one failure code a timed-out attempt carries.
FAILURE_ATTEMPT_TIMED_OUT = "detection_attempt_timed_out"
TIMED_OUT_SUMMARY = (
    "the attempt exceeded the configured stale threshold and was retired by an "
    "explicit operator replay request"
)
FAILURE_ATTEMPT_WORKER_LEASE_EXPIRED = (
    "detection_attempt_worker_lease_expired"
)
WORKER_LEASE_EXPIRED_SUMMARY = (
    "the worker claim lease expired and an explicit one-shot worker invocation "
    "retired the attempt"
)
JOB_CLAIMS_EXHAUSTED_SUMMARY = (
    "the detection job exhausted its bounded worker claim generations"
)
JOB_OWNERSHIP_LOST_CODES = frozenset(
    {
        REASON_JOB_LEASE_EXPIRED,
        REASON_JOB_CLAIM_FENCED,
        REASON_JOB_WORKER_MISMATCH,
        REASON_JOB_CLAIM_INVALID,
        REASON_JOB_ATTEMPT_MISMATCH,
        REASON_JOB_NOT_RUNNING,
    }
)

# Allowlisted replay reason codes. Operator prose lives in operator_note; the
# reason code stays a fixed machine-readable vocabulary.
REPLAY_REASON_CODES = (
    "operator_replay_stale_attempt",
    "operator_replay_after_process_restart",
)

# Actor-attribution vocabulary. Existing operator/reviewer identifiers predate
# authentication and remain readable as explicitly legacy, self-asserted
# application metadata. Only the API boundary may supply local_hs256 context;
# direct library callers intentionally keep the legacy default.
ACTOR_AUTH_LEGACY_SELF_ASSERTED = "legacy_self_asserted"
ACTOR_AUTH_LOCAL_HS256 = "local_hs256"
ACTOR_AUTH_METHODS = (
    ACTOR_AUTH_LEGACY_SELF_ASSERTED,
    ACTOR_AUTH_LOCAL_HS256,
)
MAX_ACTOR_TOKEN_ID_CHARS = 128
MAX_ACTOR_POLICY_ID_CHARS = 128
MAX_ACTOR_POLICY_VERSION_CHARS = 64

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


class DetectionStateError(Exception):
    """A detection-attempt transition is not permitted (API: 409).

    ``code`` is one of the stable REASON_* codes above; ``message`` is safe to
    display — never SQL, a filesystem path, or document content.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        comparison_id: str | None = None,
        attempt_id: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.comparison_id = comparison_id
        self.attempt_id = attempt_id


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
                        CHECK (status IN ('ready_for_detection',
                                          'queued_for_detection', 'detecting',
                                          'detected', 'failed')),
    created_at          TEXT NOT NULL,  -- timezone-aware UTC ISO 8601
    updated_at          TEXT NOT NULL,  -- timezone-aware UTC ISO 8601
    failure_code        TEXT,           -- stable code when status='failed'
    failure_summary     TEXT            -- safe one-line summary only
)
"""

_DETECTION_ATTEMPTS_DDL = """
CREATE TABLE IF NOT EXISTS comparison_detection_attempts (
    -- One durable record per STARTED detection execution. A row exists before
    -- the detector reads anything, so an interrupted run is distinguishable
    -- from one that never started. Application records, NOT tamper-proof
    -- storage. No filing content, evidence, SQL, paths, or raw exception text
    -- is ever stored here.
    attempt_id           TEXT PRIMARY KEY NOT NULL,  -- att_<sha256[:16]>
    comparison_id        TEXT NOT NULL
                         REFERENCES comparisons (comparison_id),
    attempt_number       INTEGER NOT NULL,           -- 1-based, per comparison
    status               TEXT NOT NULL
                         CHECK (status IN ('running', 'succeeded', 'failed',
                                           'timed_out')),
    detector_version     TEXT NOT NULL,
    workflow_version     TEXT NOT NULL,
    previous_source_hash TEXT NOT NULL,  -- registry hash captured at start
    current_source_hash  TEXT NOT NULL,  -- registry hash captured at start
    started_at           TEXT NOT NULL,  -- timezone-aware UTC ISO 8601
    finished_at          TEXT,
    result_hash          TEXT,
    failure_code         TEXT,
    failure_summary      TEXT,
    -- Per-status field coherence is a STORAGE invariant, not just an
    -- application rule: a committed result can never sit beside a running
    -- attempt, and a terminal attempt can never lack its outcome.
    CHECK (
        (status = 'running'
            AND finished_at IS NULL
            AND result_hash IS NULL
            AND failure_code IS NULL
            AND failure_summary IS NULL)
        OR (status = 'succeeded'
            AND finished_at IS NOT NULL
            AND result_hash IS NOT NULL
            AND failure_code IS NULL
            AND failure_summary IS NULL)
        OR (status = 'failed'
            AND finished_at IS NOT NULL
            AND result_hash IS NULL
            AND failure_code IS NOT NULL
            AND failure_summary IS NOT NULL)
        -- Retired by an explicit operator replay: carries its finish time and
        -- failure code exactly like a failed attempt, and never a result.
        OR (status = 'timed_out'
            AND finished_at IS NOT NULL
            AND result_hash IS NULL
            AND failure_code IS NOT NULL
            AND failure_summary IS NOT NULL)
    ),
    UNIQUE (comparison_id, attempt_number)
)
"""

_DETECTION_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS comparison_detection_events (
    -- Append-only transition history: this module inserts, and there is no
    -- update or delete path anywhere in the codebase. These are application
    -- records, NOT tamper-proof audit storage — anyone with file access can
    -- alter a local SQLite database. No result JSON, evidence, reviewer
    -- notes, paths, SQL, or raw error text is ever stored here.
    event_id      TEXT PRIMARY KEY NOT NULL,  -- det_evt_<sha256[:16]>
    attempt_id    TEXT NOT NULL
                  REFERENCES comparison_detection_attempts (attempt_id),
    comparison_id TEXT NOT NULL
                  REFERENCES comparisons (comparison_id),
    event_type    TEXT NOT NULL
                  CHECK (event_type IN ('detection_started',
                                        'detection_succeeded',
                                        'detection_failed',
                                        'detection_timed_out')),
    event_seq     INTEGER NOT NULL,  -- 0 started, 1 terminal: stable ordering
    created_at    TEXT NOT NULL,     -- timezone-aware UTC ISO 8601
    result_hash   TEXT,
    failure_code  TEXT,
    -- Exactly one event of each type per attempt.
    UNIQUE (attempt_id, event_type)
)
"""

_DETECTION_REPLAYS_DDL = """
CREATE TABLE IF NOT EXISTS comparison_detection_replays (
    -- One operator-requested replay: the durable link from a stale attempt
    -- that was retired to the replacement attempt that took its place.
    --
    -- operator_id is the actor subject. actor_auth_method distinguishes new
    -- locally authenticated actions from historical/direct-library
    -- self-asserted metadata. These rows are insert-only application records,
    -- NOT tamper-proof storage: anyone with file access can alter a local
    -- SQLite database. No token, secret, raw claims, raw error text, evidence,
    -- document content, paths, SQL, or environment values are stored here.
    replay_id              TEXT PRIMARY KEY NOT NULL,  -- rpl_<sha256[:16]>
    comparison_id          TEXT NOT NULL
                           REFERENCES comparisons (comparison_id),
    -- One stale attempt can produce at most ONE replacement, and one
    -- replacement can come from at most one source: both sides UNIQUE.
    source_attempt_id      TEXT NOT NULL UNIQUE
                           REFERENCES comparison_detection_attempts (attempt_id),
    replacement_attempt_id TEXT NOT NULL UNIQUE
                           REFERENCES comparison_detection_attempts (attempt_id),
    operator_id            TEXT NOT NULL,  -- actor subject
    actor_auth_method      TEXT NOT NULL DEFAULT 'legacy_self_asserted'
                           CHECK (actor_auth_method IN (
                               'legacy_self_asserted', 'local_hs256'
                           )),
    actor_token_id         TEXT,           -- verified jti; never the token
    actor_policy_id        TEXT,           -- access-control policy identity
    actor_policy_version   TEXT,
    reason_code            TEXT NOT NULL,  -- allowlisted stable code
    operator_note          TEXT NOT NULL,  -- bounded operator prose
    request_hash           TEXT NOT NULL,  -- canonical request, idempotent replay
    policy_id              TEXT NOT NULL,  -- recovery policy that authorized it
    policy_version         TEXT NOT NULL,
    requested_at           TEXT NOT NULL,  -- timezone-aware UTC ISO 8601
    CHECK (
        (actor_auth_method = 'legacy_self_asserted'
            AND actor_token_id IS NULL
            AND actor_policy_id IS NULL
            AND actor_policy_version IS NULL)
        OR (actor_auth_method = 'local_hs256'
            AND actor_token_id IS NOT NULL
            AND actor_policy_id IS NOT NULL
            AND actor_policy_version IS NOT NULL
            AND length(trim(actor_token_id)) BETWEEN 1 AND 128
            AND length(trim(actor_policy_id)) BETWEEN 1 AND 128
            AND length(trim(actor_policy_version)) BETWEEN 1 AND 64)
    )
)
"""

_DETECTION_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS comparison_detection_jobs (
    -- Durable single-node SQLite queue for authenticated initial detection.
    -- No bearer token, secret, arbitrary JWT claims, filing content, evidence,
    -- result JSON, SQL, paths, or raw exception text is stored here.
    job_id                       TEXT PRIMARY KEY NOT NULL,
    comparison_id                TEXT NOT NULL
                                 REFERENCES comparisons (comparison_id),
    attempt_id                   TEXT UNIQUE
                                 REFERENCES comparison_detection_attempts (attempt_id),
    trigger_type                 TEXT NOT NULL
                                 CHECK (trigger_type = 'initial_detection'),
    status                       TEXT NOT NULL
                                 CHECK (status IN (
                                     'queued', 'running', 'succeeded', 'failed'
                                 )),
    request_hash                 TEXT NOT NULL,
    detector_version             TEXT NOT NULL,
    workflow_version             TEXT NOT NULL,
    previous_source_hash         TEXT NOT NULL,
    current_source_hash          TEXT NOT NULL,
    requested_by_subject         TEXT NOT NULL,
    requested_by_auth_method     TEXT NOT NULL
                                 CHECK (requested_by_auth_method = 'local_hs256'),
    requested_by_token_id        TEXT NOT NULL,
    requested_by_policy_id       TEXT NOT NULL,
    requested_by_policy_version  TEXT NOT NULL,
    queued_at                    TEXT NOT NULL,
    claimed_at                   TEXT,
    finished_at                  TEXT,
    worker_id                    TEXT,
    claim_token_hash             TEXT,
    claim_generation             INTEGER NOT NULL,
    lease_started_at             TEXT,
    heartbeat_at                 TEXT,
    lease_expires_at             TEXT,
    result_hash                  TEXT,
    failure_code                 TEXT,
    failure_summary              TEXT,
    CHECK (length(trim(request_hash)) = 64),
    CHECK (length(trim(requested_by_subject)) BETWEEN 1 AND 120),
    CHECK (length(trim(requested_by_token_id)) BETWEEN 1 AND 128),
    CHECK (length(trim(requested_by_policy_id)) BETWEEN 1 AND 128),
    CHECK (length(trim(requested_by_policy_version)) BETWEEN 1 AND 64),
    CHECK (
        (status = 'queued'
            AND attempt_id IS NULL
            AND claimed_at IS NULL
            AND finished_at IS NULL
            AND worker_id IS NULL
            AND claim_token_hash IS NULL
            AND claim_generation = 0
            AND lease_started_at IS NULL
            AND heartbeat_at IS NULL
            AND lease_expires_at IS NULL
            AND result_hash IS NULL
            AND failure_code IS NULL
            AND failure_summary IS NULL)
        OR (status = 'running'
            AND attempt_id IS NOT NULL
            AND claimed_at IS NOT NULL
            AND finished_at IS NULL
            AND worker_id IS NOT NULL
            AND length(trim(worker_id)) BETWEEN 1 AND 120
            AND claim_token_hash IS NOT NULL
            AND length(claim_token_hash) = 64
            AND claim_generation >= 1
            AND lease_started_at IS NOT NULL
            AND heartbeat_at IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND julianday(lease_started_at) IS NOT NULL
            AND julianday(heartbeat_at) IS NOT NULL
            AND julianday(lease_expires_at) IS NOT NULL
            AND julianday(heartbeat_at) >= julianday(lease_started_at)
            AND julianday(lease_expires_at) >= julianday(heartbeat_at)
            AND result_hash IS NULL
            AND failure_code IS NULL
            AND failure_summary IS NULL)
        OR (status = 'succeeded'
            AND attempt_id IS NOT NULL
            AND claimed_at IS NOT NULL
            AND finished_at IS NOT NULL
            AND worker_id IS NOT NULL
            AND length(trim(worker_id)) BETWEEN 1 AND 120
            AND claim_token_hash IS NOT NULL
            AND length(claim_token_hash) = 64
            AND claim_generation >= 1
            AND lease_started_at IS NOT NULL
            AND heartbeat_at IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND julianday(lease_started_at) IS NOT NULL
            AND julianday(heartbeat_at) IS NOT NULL
            AND julianday(lease_expires_at) IS NOT NULL
            AND julianday(heartbeat_at) >= julianday(lease_started_at)
            AND julianday(lease_expires_at) >= julianday(heartbeat_at)
            AND result_hash IS NOT NULL
            AND failure_code IS NULL
            AND failure_summary IS NULL)
        OR (status = 'failed'
            AND finished_at IS NOT NULL
            AND result_hash IS NULL
            AND failure_code IS NOT NULL
            AND failure_summary IS NOT NULL
            AND length(failure_summary) BETWEEN 1 AND 200
            AND (
                (attempt_id IS NULL
                    AND claimed_at IS NULL
                    AND worker_id IS NULL
                    AND claim_token_hash IS NULL
                    AND claim_generation = 0
                    AND lease_started_at IS NULL
                    AND heartbeat_at IS NULL
                    AND lease_expires_at IS NULL)
                OR (attempt_id IS NOT NULL
                    AND claimed_at IS NOT NULL
                    AND worker_id IS NOT NULL
                    AND length(trim(worker_id)) BETWEEN 1 AND 120
                    AND claim_token_hash IS NOT NULL
                    AND length(claim_token_hash) = 64
                    AND claim_generation >= 1
                    AND lease_started_at IS NOT NULL
                    AND heartbeat_at IS NOT NULL
                    AND lease_expires_at IS NOT NULL
                    AND julianday(lease_started_at) IS NOT NULL
                    AND julianday(heartbeat_at) IS NOT NULL
                    AND julianday(lease_expires_at) IS NOT NULL
                    AND julianday(heartbeat_at) >= julianday(lease_started_at)
                    AND julianday(lease_expires_at) >= julianday(heartbeat_at))
            ))
    )
)
"""

_DETECTION_JOB_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS comparison_detection_job_events (
    -- Insert-only application transition records. They are not tamper-proof.
    -- No tokens, hashes of tokens, secrets, evidence, notes, paths, SQL, or
    -- exception text are stored.
    event_id       TEXT PRIMARY KEY NOT NULL,
    job_id         TEXT NOT NULL
                   REFERENCES comparison_detection_jobs (job_id),
    comparison_id  TEXT NOT NULL
                   REFERENCES comparisons (comparison_id),
    attempt_id     TEXT
                   REFERENCES comparison_detection_attempts (attempt_id),
    event_type     TEXT NOT NULL
                   CHECK (event_type IN (
                       'detection_job_queued', 'detection_job_claimed',
                       'detection_job_heartbeat', 'detection_job_reclaimed',
                       'detection_job_claim_exhausted',
                       'detection_job_succeeded', 'detection_job_failed'
                   )),
    event_seq      INTEGER NOT NULL CHECK (event_seq >= 0),
    created_at     TEXT NOT NULL,
    worker_id      TEXT,
    claim_generation INTEGER NOT NULL,
    source_attempt_id TEXT
                      REFERENCES comparison_detection_attempts (attempt_id),
    replacement_attempt_id TEXT
                           REFERENCES comparison_detection_attempts (attempt_id),
    lease_expires_at TEXT,
    result_hash    TEXT,
    failure_code   TEXT,
    UNIQUE (job_id, event_seq),
    CHECK (
        (event_type = 'detection_job_queued'
            AND event_seq = 0
            AND attempt_id IS NULL
            AND worker_id IS NULL
            AND claim_generation = 0
            AND source_attempt_id IS NULL
            AND replacement_attempt_id IS NULL
            AND lease_expires_at IS NULL
            AND result_hash IS NULL
            AND failure_code IS NULL)
        OR (event_type = 'detection_job_claimed'
            AND event_seq = 1
            AND attempt_id IS NOT NULL
            AND worker_id IS NOT NULL
            AND claim_generation = 1
            AND source_attempt_id IS NULL
            AND replacement_attempt_id IS NULL
            AND lease_expires_at IS NOT NULL
            AND result_hash IS NULL
            AND failure_code IS NULL)
        OR (event_type = 'detection_job_heartbeat'
            AND attempt_id IS NOT NULL
            AND worker_id IS NOT NULL
            AND claim_generation >= 1
            AND source_attempt_id IS NULL
            AND replacement_attempt_id IS NULL
            AND lease_expires_at IS NOT NULL
            AND result_hash IS NULL
            AND failure_code IS NULL)
        OR (event_type = 'detection_job_reclaimed'
            AND attempt_id IS NOT NULL
            AND worker_id IS NOT NULL
            AND claim_generation >= 2
            AND source_attempt_id IS NOT NULL
            AND replacement_attempt_id = attempt_id
            AND lease_expires_at IS NOT NULL
            AND result_hash IS NULL
            AND failure_code IS NULL)
        OR (event_type = 'detection_job_claim_exhausted'
            AND attempt_id IS NOT NULL
            AND worker_id IS NOT NULL
            AND claim_generation >= 1
            AND source_attempt_id = attempt_id
            AND replacement_attempt_id IS NULL
            AND lease_expires_at IS NOT NULL
            AND result_hash IS NULL
            AND failure_code = 'detection_job_claims_exhausted')
        OR (event_type = 'detection_job_succeeded'
            AND attempt_id IS NOT NULL
            AND worker_id IS NOT NULL
            AND claim_generation >= 1
            AND source_attempt_id IS NULL
            AND replacement_attempt_id IS NULL
            AND lease_expires_at IS NOT NULL
            AND result_hash IS NOT NULL
            AND failure_code IS NULL)
        OR (event_type = 'detection_job_failed'
            AND result_hash IS NULL
            AND failure_code IS NOT NULL
            AND (
                (attempt_id IS NULL
                    AND worker_id IS NULL
                    AND claim_generation = 0
                    AND source_attempt_id IS NULL
                    AND replacement_attempt_id IS NULL
                    AND lease_expires_at IS NULL)
                OR (attempt_id IS NOT NULL
                    AND worker_id IS NOT NULL
                    AND claim_generation >= 1
                    AND source_attempt_id IS NULL
                    AND replacement_attempt_id IS NULL
                    AND lease_expires_at IS NOT NULL)
            ))
    )
)
"""


def _actor_attribution_trigger_statements(table: str) -> tuple[str, str]:
    """Storage-level provenance coherence for both fresh and upgraded tables.

    SQLite cannot add a composite CHECK with ALTER TABLE, so upgraded schemas
    receive equivalent INSERT/UPDATE triggers. Fresh tables retain their CHECK
    and get the same triggers, keeping both schema paths behaviorally equal.
    """
    if table not in {
        "comparison_detection_replays",
        "comparison_review_events",
    }:  # pragma: no cover - closed internal call sites
        raise ValueError("unsupported actor attribution table")
    valid = """
        (
            NEW.actor_auth_method = 'legacy_self_asserted'
            AND NEW.actor_token_id IS NULL
            AND NEW.actor_policy_id IS NULL
            AND NEW.actor_policy_version IS NULL
        )
        OR (
            NEW.actor_auth_method = 'local_hs256'
            AND NEW.actor_token_id IS NOT NULL
            AND length(trim(NEW.actor_token_id)) BETWEEN 1 AND 128
            AND NEW.actor_policy_id IS NOT NULL
            AND length(trim(NEW.actor_policy_id)) BETWEEN 1 AND 128
            AND NEW.actor_policy_version IS NOT NULL
            AND length(trim(NEW.actor_policy_version)) BETWEEN 1 AND 64
        )
    """
    return tuple(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_{table}_actor_{action.lower()}
        BEFORE {action} ON {table}
        WHEN NOT ({valid})
        BEGIN
            SELECT RAISE(ABORT, 'invalid actor attribution');
        END
        """
        for action in ("INSERT", "UPDATE")
    )


def _actor_attribution_triggers_sql(table: str) -> str:
    return ";\n".join(_actor_attribution_trigger_statements(table)) + ";"


_ACTOR_ATTRIBUTION_TRIGGERS_DDL = "\n".join(
    _actor_attribution_triggers_sql(table)
    for table in (
        "comparison_detection_replays",
        "comparison_review_events",
    )
)

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
    reviewer_id                   TEXT NOT NULL,  -- actor subject
    actor_auth_method             TEXT NOT NULL DEFAULT 'legacy_self_asserted'
                                  CHECK (actor_auth_method IN (
                                      'legacy_self_asserted', 'local_hs256'
                                  )),
    actor_token_id                TEXT,           -- verified jti; never token
    actor_policy_id               TEXT,           -- access-control policy id
    actor_policy_version          TEXT,
    reason_code                   TEXT NOT NULL,  -- allowlisted stable code
    reviewer_note                 TEXT NOT NULL,  -- bounded reviewer prose
    request_hash                  TEXT NOT NULL,  -- canonical request, for idempotent replay
    original_governed_result_hash TEXT NOT NULL,
    final_reviewed_result_hash    TEXT NOT NULL,
    reviewed_result_json          TEXT NOT NULL,  -- complete final comparison.v1 snapshot
    edit_summary_json             TEXT,           -- JSON array of change_id/original/new summaries
    created_at                    TEXT NOT NULL,
    CHECK (
        (actor_auth_method = 'legacy_self_asserted'
            AND actor_token_id IS NULL
            AND actor_policy_id IS NULL
            AND actor_policy_version IS NULL)
        OR (actor_auth_method = 'local_hs256'
            AND actor_token_id IS NOT NULL
            AND actor_policy_id IS NOT NULL
            AND actor_policy_version IS NOT NULL
            AND length(trim(actor_token_id)) BETWEEN 1 AND 128
            AND length(trim(actor_policy_id)) BETWEEN 1 AND 128
            AND length(trim(actor_policy_version)) BETWEEN 1 AND 64)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_events_terminal
    ON comparison_review_events (review_id);
CREATE TABLE IF NOT EXISTS comparison_exports (
    -- Release-gated export artifacts: one row per (evaluation, released
    -- snapshot hash, export schema version). Deterministic application
    -- records, NOT signed or tamper-proof storage. Rows are never updated
    -- or deleted; a replay reads the stored payload back verbatim.
    export_id             TEXT PRIMARY KEY NOT NULL,  -- exp_<sha256[:16]>
    export_schema_version TEXT NOT NULL,
    comparison_id         TEXT NOT NULL
                          REFERENCES comparisons (comparison_id),
    evaluation_id         TEXT NOT NULL
                          REFERENCES comparison_governance_evaluations (evaluation_id),
    review_id             TEXT REFERENCES comparison_review_items (review_id),
    release_basis         TEXT NOT NULL
                          CHECK (release_basis IN (
                              'returned_by_policy',
                              'returned_with_warning_by_policy',
                              'approved_after_review'
                          )),
    source_result_hash    TEXT NOT NULL,  -- the evaluation's governed hash
    final_result_hash     TEXT NOT NULL,  -- hash of the released snapshot
    export_payload_json   TEXT NOT NULL,  -- canonical comparison.export.v1 document
    export_payload_hash   TEXT NOT NULL,  -- sha256 over the timestamp-independent form
    created_at            TEXT NOT NULL   -- timezone-aware UTC ISO 8601
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_comparison_exports_logical_key
    ON comparison_exports (evaluation_id, final_result_hash, export_schema_version);
{_DETECTION_ATTEMPTS_DDL};
-- At most ONE running attempt per comparison, enforced by storage rather than
-- by a read-then-write check: concurrent starts race on this index and exactly
-- one wins.
CREATE UNIQUE INDEX IF NOT EXISTS idx_detection_attempt_running
    ON comparison_detection_attempts (comparison_id) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_detection_attempts_comparison
    ON comparison_detection_attempts (comparison_id, attempt_number);
{_DETECTION_EVENTS_DDL};
CREATE INDEX IF NOT EXISTS idx_detection_events_attempt
    ON comparison_detection_events (attempt_id, event_seq);
{_DETECTION_JOBS_DDL};
-- Storage-level concurrency invariant: one active initial-detection job per
-- comparison, regardless of how many API or worker processes race.
CREATE UNIQUE INDEX IF NOT EXISTS idx_detection_job_active
    ON comparison_detection_jobs (comparison_id)
    WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS idx_detection_jobs_queue
    ON comparison_detection_jobs (status, queued_at, job_id);
CREATE INDEX IF NOT EXISTS idx_detection_jobs_expiry
    ON comparison_detection_jobs (status, lease_expires_at, job_id);
CREATE INDEX IF NOT EXISTS idx_detection_jobs_comparison
    ON comparison_detection_jobs (comparison_id, queued_at, job_id);
{_DETECTION_JOB_EVENTS_DDL};
CREATE INDEX IF NOT EXISTS idx_detection_job_events_job
    ON comparison_detection_job_events (job_id, event_seq, created_at, event_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_detection_job_event_singletons
    ON comparison_detection_job_events (job_id, event_type)
    WHERE event_type IN (
        'detection_job_queued', 'detection_job_claimed',
        'detection_job_claim_exhausted',
        'detection_job_succeeded', 'detection_job_failed'
    );
{_DETECTION_REPLAYS_DDL};
CREATE INDEX IF NOT EXISTS idx_detection_replays_comparison
    ON comparison_detection_replays (comparison_id, requested_at);
{_ACTOR_ATTRIBUTION_TRIGGERS_DDL}
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


def _migrate_actor_attribution(db_path: Path) -> None:
    """Add authenticated actor provenance without rewriting historical rows.

    The migration is additive and idempotent. SQLite applies the
    ``legacy_self_asserted`` default to every pre-existing replay/review event;
    the remaining nullable columns stay NULL, so no authentication is invented
    for history. A partially applied migration is safe to reopen: each column
    is discovered independently through ``PRAGMA table_info``.

    After adding columns, narrow integrity queries verify that legacy rows do
    not carry token/policy metadata and authenticated rows carry all three
    bounded identifiers. They intentionally inspect metadata only, never
    operator/reviewer prose or result payloads.
    """
    if not db_path.exists():
        return
    migrations = {
        "comparison_detection_replays": (
            (
                "actor_auth_method",
                "TEXT NOT NULL DEFAULT 'legacy_self_asserted' "
                "CHECK (actor_auth_method IN "
                "('legacy_self_asserted', 'local_hs256'))",
            ),
            ("actor_token_id", "TEXT"),
            ("actor_policy_id", "TEXT"),
            ("actor_policy_version", "TEXT"),
        ),
        "comparison_review_events": (
            (
                "actor_auth_method",
                "TEXT NOT NULL DEFAULT 'legacy_self_asserted' "
                "CHECK (actor_auth_method IN "
                "('legacy_self_asserted', 'local_hs256'))",
            ),
            ("actor_token_id", "TEXT"),
            ("actor_policy_id", "TEXT"),
            ("actor_policy_version", "TEXT"),
        ),
    }
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        # Hold a write lock across BOTH schema discovery and ALTER. Otherwise,
        # two first requests can observe the same pre-auth schema and the loser
        # attempts to add a column the winner just committed.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table, columns in migrations.items():
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if exists is None:
                    continue
                present = {
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                }
                for name, declaration in columns:
                    if name not in present:
                        conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                        )
                        present.add(name)
                for statement in _actor_attribution_trigger_statements(table):
                    conn.execute(statement)

                invalid = conn.execute(
                    f"""
                    SELECT 1 FROM {table}
                    WHERE actor_auth_method IS NULL
                       OR actor_auth_method NOT IN (?, ?)
                       OR (
                           actor_auth_method = ?
                           AND (
                               actor_token_id IS NOT NULL
                               OR actor_policy_id IS NOT NULL
                               OR actor_policy_version IS NOT NULL
                           )
                       )
                       OR (
                           actor_auth_method = ?
                           AND (
                               actor_token_id IS NULL
                               OR length(trim(actor_token_id)) NOT BETWEEN 1 AND 128
                               OR actor_policy_id IS NULL
                               OR length(trim(actor_policy_id)) NOT BETWEEN 1 AND 128
                               OR actor_policy_version IS NULL
                               OR length(trim(actor_policy_version)) NOT BETWEEN 1 AND 64
                           )
                       )
                    LIMIT 1
                    """,
                    (
                        ACTOR_AUTH_LEGACY_SELF_ASSERTED,
                        ACTOR_AUTH_LOCAL_HS256,
                        ACTOR_AUTH_LEGACY_SELF_ASSERTED,
                        ACTOR_AUTH_LOCAL_HS256,
                    ),
                ).fetchone()
                if invalid is not None:
                    raise sqlite3.IntegrityError(
                        f"invalid actor attribution in {table}"
                    )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


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


def _migrate_detecting_status(db_path: Path) -> None:
    """Rebuild the comparisons table if its CHECK predates 'detecting'.

    Idempotent: inspects the stored DDL and returns immediately once
    'detecting' is present. Unlike the pre-detector migration, this one can run
    on a database that already HAS child tables (results, evaluations, reviews,
    exports, attempts), so the rebuild is create-new / copy / drop / rename
    rather than rename-then-recreate, and ``legacy_alter_table`` is enabled so
    SQLite does not rewrite those children's ``REFERENCES comparisons`` clauses
    to point at a temporary name. Existing rows and their statuses are
    preserved byte for byte; results, evaluations, reviews, and exports are
    never touched.
    """
    if not db_path.exists():
        return
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='comparisons'"
        ).fetchone()
        if row is None or "'detecting'" in (row[0] or ""):
            return
        conn.execute("PRAGMA legacy_alter_table = ON")
        with conn:
            conn.execute(
                _COMPARISONS_DDL.replace(
                    "CREATE TABLE IF NOT EXISTS comparisons",
                    "CREATE TABLE comparisons_rebuilt",
                )
            )
            conn.execute(
                "INSERT INTO comparisons_rebuilt SELECT * FROM comparisons"
            )
            conn.execute("DROP TABLE comparisons")
            conn.execute("ALTER TABLE comparisons_rebuilt RENAME TO comparisons")


def _migrate_queued_for_detection_status(db_path: Path) -> None:
    """Add ``queued_for_detection`` to the comparison CHECK, idempotently.

    A write lock is acquired before schema discovery, so concurrent first-open
    initialization cannot make two processes rebuild the same table. Existing
    comparisons and every child row are copied unchanged; legacy_alter_table
    prevents SQLite from retargeting child foreign keys to the temporary name.
    """
    if not db_path.exists():
        return
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='comparisons'"
            ).fetchone()
            if row is not None and "'queued_for_detection'" not in (row[0] or ""):
                conn.execute(
                    _COMPARISONS_DDL.replace(
                        "CREATE TABLE IF NOT EXISTS comparisons",
                        "CREATE TABLE comparisons_queued_rebuilt",
                    )
                )
                conn.execute(
                    "INSERT INTO comparisons_queued_rebuilt "
                    "SELECT * FROM comparisons"
                )
                conn.execute("DROP TABLE comparisons")
                conn.execute(
                    "ALTER TABLE comparisons_queued_rebuilt "
                    "RENAME TO comparisons"
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def _migrate_attempt_timed_out(db_path: Path) -> None:
    """Rebuild the attempt/event tables if their CHECKs predate 'timed_out'.

    Idempotent: inspects the stored DDL of each table and rebuilds only the ones
    whose CHECK lacks the new vocabulary (SQLite cannot ALTER a CHECK in place).
    Existing running/succeeded/failed attempts and all existing events are
    preserved byte for byte.

    Both rebuilds use create-new / copy / drop / rename with
    ``legacy_alter_table`` enabled, because comparison_detection_events holds a
    foreign key to comparison_detection_attempts: without the pragma SQLite
    would rewrite that reference to point at the temporary table name.
    """
    if not db_path.exists():
        return
    rebuilds = (
        (
            "comparison_detection_attempts",
            "'timed_out'",
            _DETECTION_ATTEMPTS_DDL,
            "comparison_detection_attempts_rebuilt",
        ),
        (
            "comparison_detection_events",
            "'detection_timed_out'",
            _DETECTION_EVENTS_DDL,
            "comparison_detection_events_rebuilt",
        ),
    )
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA legacy_alter_table = ON")
        for table, marker, ddl, temp_name in rebuilds:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if row is None or marker in (row[0] or ""):
                continue
            with conn:
                conn.execute(
                    ddl.replace(
                        f"CREATE TABLE IF NOT EXISTS {table}",
                        f"CREATE TABLE {temp_name}",
                    )
                )
                conn.execute(f"INSERT INTO {temp_name} SELECT * FROM {table}")
                conn.execute(f"DROP TABLE {table}")
                conn.execute(f"ALTER TABLE {temp_name} RENAME TO {table}")


def _migrate_detection_job_leases(db_path: Path) -> None:
    """Rebuild pre-lease job/event tables with an honest historical mapping.

    Existing queued and pre-attempt failed jobs become generation 0 with no
    lease. Existing claimed/running/terminal jobs become generation 1. For a
    historical running claim, ``claimed_at`` is copied into every lease
    timestamp, so it is conservatively expired rather than silently granted a
    fresh lease during migration.

    Both tables are rebuilt in one serialized transaction because the event
    table references jobs and attempts. Repeated heartbeat/reclaim events also
    require removing the old ``UNIQUE(job_id, event_type)`` constraint.
    """
    if not db_path.exists():
        return
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            job_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='comparison_detection_jobs'"
            ).fetchone()
            event_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='comparison_detection_job_events'"
            ).fetchone()
            rebuild_jobs = (
                job_row is not None
                and "claim_generation" not in (job_row[0] or "")
            )
            rebuild_events = (
                event_row is not None
                and "'detection_job_heartbeat'" not in (event_row[0] or "")
            )

            if rebuild_jobs:
                conn.execute(
                    _DETECTION_JOBS_DDL.replace(
                        "CREATE TABLE IF NOT EXISTS comparison_detection_jobs",
                        "CREATE TABLE comparison_detection_jobs_lease_rebuilt",
                    )
                )
                conn.execute(
                    """
                    INSERT INTO comparison_detection_jobs_lease_rebuilt (
                        job_id, comparison_id, attempt_id, trigger_type, status,
                        request_hash, detector_version, workflow_version,
                        previous_source_hash, current_source_hash,
                        requested_by_subject, requested_by_auth_method,
                        requested_by_token_id, requested_by_policy_id,
                        requested_by_policy_version, queued_at, claimed_at,
                        finished_at, worker_id, claim_token_hash,
                        claim_generation, lease_started_at, heartbeat_at,
                        lease_expires_at, result_hash, failure_code,
                        failure_summary
                    )
                    SELECT
                        job_id, comparison_id, attempt_id, trigger_type, status,
                        request_hash, detector_version, workflow_version,
                        previous_source_hash, current_source_hash,
                        requested_by_subject, requested_by_auth_method,
                        requested_by_token_id, requested_by_policy_id,
                        requested_by_policy_version, queued_at, claimed_at,
                        finished_at, worker_id, claim_token_hash,
                        CASE WHEN attempt_id IS NULL THEN 0 ELSE 1 END,
                        CASE WHEN attempt_id IS NULL THEN NULL ELSE claimed_at END,
                        CASE WHEN attempt_id IS NULL THEN NULL ELSE claimed_at END,
                        CASE WHEN attempt_id IS NULL THEN NULL ELSE claimed_at END,
                        result_hash, failure_code, failure_summary
                    FROM comparison_detection_jobs
                    """
                )

            if rebuild_events:
                conn.execute(
                    _DETECTION_JOB_EVENTS_DDL.replace(
                        "CREATE TABLE IF NOT EXISTS "
                        "comparison_detection_job_events",
                        "CREATE TABLE "
                        "comparison_detection_job_events_lease_rebuilt",
                    )
                )
                generation = (
                    "CASE WHEN e.attempt_id IS NULL THEN 0 ELSE 1 END"
                    if rebuild_jobs
                    else "j.claim_generation"
                )
                lease_expires = (
                    "CASE WHEN e.attempt_id IS NULL "
                    "THEN NULL ELSE j.claimed_at END"
                    if rebuild_jobs
                    else "CASE WHEN e.attempt_id IS NULL "
                    "THEN NULL ELSE j.lease_expires_at END"
                )
                conn.execute(
                    f"""
                    INSERT INTO comparison_detection_job_events_lease_rebuilt (
                        event_id, job_id, comparison_id, attempt_id, event_type,
                        event_seq, created_at, worker_id, claim_generation,
                        source_attempt_id, replacement_attempt_id,
                        lease_expires_at, result_hash, failure_code
                    )
                    SELECT
                        e.event_id, e.job_id, e.comparison_id, e.attempt_id,
                        e.event_type, e.event_seq, e.created_at, e.worker_id,
                        {generation}, NULL, NULL, {lease_expires},
                        e.result_hash, e.failure_code
                    FROM comparison_detection_job_events e
                    JOIN comparison_detection_jobs j ON j.job_id = e.job_id
                    """
                )

            if rebuild_events:
                conn.execute("DROP TABLE comparison_detection_job_events")
            if rebuild_jobs:
                conn.execute("DROP TABLE comparison_detection_jobs")
                conn.execute(
                    "ALTER TABLE comparison_detection_jobs_lease_rebuilt "
                    "RENAME TO comparison_detection_jobs"
                )
            if rebuild_events:
                conn.execute(
                    "ALTER TABLE comparison_detection_job_events_lease_rebuilt "
                    "RENAME TO comparison_detection_job_events"
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


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
    _migrate_detecting_status(db_path)
    _migrate_queued_for_detection_status(db_path)
    _migrate_attempt_timed_out(db_path)
    _migrate_detection_job_leases(db_path)
    _migrate_review_items_vocabulary(db_path)
    _migrate_actor_attribution(db_path)
    with closing(_connect(db_path)) as conn, conn:
        conn.executescript(_SCHEMA_SQL)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_moment(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("now must be timezone-aware")
    return moment.astimezone(timezone.utc)


def _validated_policy_int(
    value: Any, *, field: str, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return value


def _bounded_actor_value(value: Any, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    cleaned = value.strip()
    if len(cleaned) > max_chars:
        raise ValueError(f"{field} must be at most {max_chars} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
        raise ValueError(f"{field} must not contain control characters")
    return cleaned


def validated_actor_attribution(
    *,
    actor_auth_method: str = ACTOR_AUTH_LEGACY_SELF_ASSERTED,
    actor_token_id: str | None = None,
    actor_policy_id: str | None = None,
    actor_policy_version: str | None = None,
) -> dict[str, str | None]:
    """Validate the narrow attribution fields persisted with an actor action.

    Legacy attribution is deliberately metadata-free. Authenticated
    attribution is all-or-nothing: a verified token id and the access-control
    policy identity are required. No bearer token or arbitrary claims have a
    parameter through which they could reach storage.
    """
    if actor_auth_method not in ACTOR_AUTH_METHODS:
        raise ValueError("actor_auth_method is not supported")
    if actor_auth_method == ACTOR_AUTH_LEGACY_SELF_ASSERTED:
        if any(
            value is not None
            for value in (
                actor_token_id,
                actor_policy_id,
                actor_policy_version,
            )
        ):
            raise ValueError(
                "legacy actor attribution cannot carry token or policy metadata"
            )
        return {
            "actor_auth_method": ACTOR_AUTH_LEGACY_SELF_ASSERTED,
            "actor_token_id": None,
            "actor_policy_id": None,
            "actor_policy_version": None,
        }
    return {
        "actor_auth_method": ACTOR_AUTH_LOCAL_HS256,
        "actor_token_id": _bounded_actor_value(
            actor_token_id,
            field="actor_token_id",
            max_chars=MAX_ACTOR_TOKEN_ID_CHARS,
        ),
        "actor_policy_id": _bounded_actor_value(
            actor_policy_id,
            field="actor_policy_id",
            max_chars=MAX_ACTOR_POLICY_ID_CHARS,
        ),
        "actor_policy_version": _bounded_actor_value(
            actor_policy_version,
            field="actor_policy_version",
            max_chars=MAX_ACTOR_POLICY_VERSION_CHARS,
        ),
    }


def actor_attribution_from_context(
    actor_subject: str,
    actor_context: Mapping[str, Any] | None,
    *,
    actor_policy_id: str | None = None,
    actor_policy_version: str | None = None,
) -> dict[str, str | None]:
    """Convert a Principal-derived logging context to storage attribution.

    ``actor_context`` is intentionally a closed mapping. It contains only the
    fields the structured logger accepts and must name the same subject the
    caller is persisting as ``operator_id`` or ``reviewer_id``. Authentication
    policy identity is passed separately because it is persisted for audit
    linkage but deliberately absent from lifecycle logs.
    """
    if actor_context is None:
        return validated_actor_attribution(
            actor_policy_id=actor_policy_id,
            actor_policy_version=actor_policy_version,
        )
    if not isinstance(actor_context, Mapping):
        raise ValueError("actor_context must be a mapping")
    allowed = {
        "actor_subject",
        "actor_auth_method",
        "actor_token_id",
        "required_permission",
    }
    unknown = set(actor_context) - allowed
    missing = allowed - set(actor_context)
    if unknown or missing:
        raise ValueError("actor_context must contain only allowlisted fields")
    if actor_context["actor_subject"] != actor_subject:
        raise ValueError("actor_context subject does not match persisted actor")
    _bounded_actor_value(
        actor_context["required_permission"],
        field="actor_context required_permission",
        max_chars=120,
    )
    return validated_actor_attribution(
        actor_auth_method=actor_context["actor_auth_method"],
        actor_token_id=actor_context["actor_token_id"],
        actor_policy_id=actor_policy_id,
        actor_policy_version=actor_policy_version,
    )


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
            "section_scope contains an unsupported section key; v1 supports "
            "only item_1a_risk_factors",
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


def _insert_result(
    conn: sqlite3.Connection,
    comparison_id: str,
    *,
    result_json: str,
    result_hash: str,
    detector_version: str,
    previous_source_hash: str,
    current_source_hash: str,
    now: str,
) -> None:
    """The ONE canonical comparison_results insert.

    Both result-persistence paths go through here — ``record_result`` (the
    plain library/test path) and ``complete_detection_attempt`` (the
    attempt-tracked detector path) — so the stored column set can never
    diverge between them. Caller owns the transaction.
    """
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


def _result_row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    stored = dict(row)
    stored["result"] = json.loads(stored.pop("result_json"))
    return stored


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
    """LOW-LEVEL: persist a result and transition the comparison to detected.

    NOT the detection orchestration path, and no production code calls it.
    Every runtime detection entry point (``comparison_detector.detect`` and
    ``detect_with_attempt``, and therefore the API route, the CLI, and the
    regression runner) goes through ``start_detection_attempt`` +
    ``complete_detection_attempt`` instead, so a result is never persisted
    without the durable attempt that produced it. This function records NO
    attempt and NO transition event, so calling it from a new detection path
    would silently reintroduce untracked execution — don't.

    It remains only as a narrow fixture primitive for tests that need a stored
    result without running the detector (the governance, review, and export
    suites seed synthetic comparison.v1 results this way), mirroring how
    ``load_documents`` without ``registry_path=`` keeps the plain pre-registry
    behavior for library callers.

    One BEGIN IMMEDIATE transaction covers the lifecycle check, the result
    insert, and the status update. A comparison that is not
    ready_for_detection raises ComparisonLifecycleError; an existing result
    raises ComparisonResultExists (results are never overwritten).
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
                    comparison_id, "absent", "unknown comparison"
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
            _insert_result(
                conn,
                comparison_id,
                result_json=result_json,
                result_hash=result_hash,
                detector_version=detector_version,
                previous_source_hash=previous_source_hash,
                current_source_hash=current_source_hash,
                now=now,
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
    return _result_row_to_record(row)


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
    actor_auth_method: str = ACTOR_AUTH_LEGACY_SELF_ASSERTED,
    actor_token_id: str | None = None,
    actor_policy_id: str | None = None,
    actor_policy_version: str | None = None,
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
    attribution = validated_actor_attribution(
        actor_auth_method=actor_auth_method,
        actor_token_id=actor_token_id,
        actor_policy_id=actor_policy_id,
        actor_policy_version=actor_policy_version,
    )
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
                    review_id, "absent", "unknown review item"
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
                    action, reviewer_id, actor_auth_method, actor_token_id,
                    actor_policy_id, actor_policy_version,
                    reason_code, reviewer_note,
                    request_hash, original_governed_result_hash,
                    final_reviewed_result_hash, reviewed_result_json,
                    edit_summary_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    review_id,
                    item["comparison_id"],
                    item["evaluation_id"],
                    action,
                    reviewer_id,
                    attribution["actor_auth_method"],
                    attribution["actor_token_id"],
                    attribution["actor_policy_id"],
                    attribution["actor_policy_version"],
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


# --- Durable detection attempts and transition events ------------------------
#
# Every STARTED detection execution gets a durable attempt row plus append-only
# transition events, and the comparison carries an explicit `detecting` state
# while one is running. The store owns the state machine: these functions
# re-read and re-verify state inside their own transaction rather than trusting
# the caller to pass a valid state.
#
#   direct comparison: ready_for_detection -> detecting -> detected | failed
#   API job comparison: ready -> queued -> detecting -> detected | failed
#   attempt:                          (none) -> running -> succeeded | failed
#
# Terminal states do not transition. Explicit bounded replay remains available
# for eligible direct/replay attempts, but there is no automatic retry.
# One-shot initial-detection claims have finite leases; only a later explicit
# worker invocation can atomically retire and replace an expired claimed
# attempt. Merely crossing the expiry changes nothing.


def detection_attempt_id_for(
    comparison_id: str,
    attempt_number: int,
    *,
    detector_version: str,
    workflow_version: str,
    previous_source_hash: str,
    current_source_hash: str,
) -> str:
    """Deterministic attempt id that is nonetheless unique per attempt.

    ``attempt_number`` participates in the key, so two attempts of the same
    comparison — even with identical versions and identical input hashes —
    can never collide.
    """
    key = "|".join(
        [
            comparison_id,
            str(attempt_number),
            detector_version,
            workflow_version,
            previous_source_hash,
            current_source_hash,
        ]
    )
    return f"att_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


def _detection_event_id(attempt_id: str, event_type: str) -> str:
    """Deterministic event id. Exactly one event of each type per attempt
    exists, so this is unique and a duplicate insert fails on the primary key
    as well as on the (attempt_id, event_type) uniqueness constraint."""
    key = f"{attempt_id}|{event_type}"
    return f"det_evt_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


def _insert_detection_event(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    comparison_id: str,
    event_type: str,
    now: str,
    result_hash: str | None = None,
    failure_code: str | None = None,
) -> None:
    """Append one transition event. Insert-only: no update or delete path."""
    conn.execute(
        """
        INSERT INTO comparison_detection_events (
            event_id, attempt_id, comparison_id, event_type, event_seq,
            created_at, result_hash, failure_code
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _detection_event_id(attempt_id, event_type),
            attempt_id,
            comparison_id,
            event_type,
            _EVENT_SEQ[event_type],
            now,
            result_hash,
            failure_code,
        ),
    )


def start_detection_attempt(
    comparison_id: str,
    *,
    detector_version: str,
    workflow_version: str,
    previous_source_hash: str,
    current_source_hash: str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Durably start one detection execution; returns the running attempt.

    One BEGIN IMMEDIATE transaction covers: re-reading the comparison,
    verifying it is ready_for_detection, verifying no attempt is already
    running, allocating the next attempt number, inserting the running attempt,
    appending detection_started, and transitioning the comparison to
    ``detecting``. Nothing is left in memory — after this returns, an
    interrupted execution is observable.

    Concurrent callers serialize here and exactly one wins; the losers see the
    winner's committed state and raise DetectionStateError with
    ``detection_in_progress``. The caller MUST NOT hold this transaction while
    reading the index or running the detector — it is closed before returning.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    now = _utc_now_iso()
    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            record = conn.execute(
                "SELECT status FROM comparisons WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
            if record is None:
                raise ComparisonLifecycleError(
                    comparison_id, "absent", "unknown comparison"
                )

            # Checked BEFORE the status check so a comparison left in
            # 'detecting' reports the specific in-progress code.
            running = conn.execute(
                "SELECT attempt_id FROM comparison_detection_attempts "
                "WHERE comparison_id = ? AND status = ?",
                (comparison_id, ATTEMPT_RUNNING),
            ).fetchone()
            if running is not None:
                raise DetectionStateError(
                    REASON_DETECTION_IN_PROGRESS,
                    "a detection attempt is already running for this "
                    "comparison; concurrent detection is not started twice",
                    comparison_id=comparison_id,
                    attempt_id=running["attempt_id"],
                )
            if record["status"] != STATUS_READY_FOR_DETECTION:
                raise DetectionStateError(
                    REASON_COMPARISON_NOT_READY,
                    f"comparison is '{record['status']}', not "
                    "'ready_for_detection'",
                    comparison_id=comparison_id,
                )

            next_number = (
                conn.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 "
                    "FROM comparison_detection_attempts WHERE comparison_id = ?",
                    (comparison_id,),
                ).fetchone()[0]
            )
            attempt_id = detection_attempt_id_for(
                comparison_id,
                next_number,
                detector_version=detector_version,
                workflow_version=workflow_version,
                previous_source_hash=previous_source_hash,
                current_source_hash=current_source_hash,
            )
            conn.execute(
                """
                INSERT INTO comparison_detection_attempts (
                    attempt_id, comparison_id, attempt_number, status,
                    detector_version, workflow_version,
                    previous_source_hash, current_source_hash,
                    started_at, finished_at, result_hash,
                    failure_code, failure_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
                """,
                (
                    attempt_id,
                    comparison_id,
                    next_number,
                    ATTEMPT_RUNNING,
                    detector_version,
                    workflow_version,
                    previous_source_hash,
                    current_source_hash,
                    now,
                ),
            )
            _insert_detection_event(
                conn,
                attempt_id=attempt_id,
                comparison_id=comparison_id,
                event_type=EVENT_DETECTION_STARTED,
                now=now,
            )
            cursor = conn.execute(
                "UPDATE comparisons SET status = ?, updated_at = ? "
                "WHERE comparison_id = ? AND status = ?",
                (STATUS_DETECTING, now, comparison_id, STATUS_READY_FOR_DETECTION),
            )
            if cursor.rowcount != 1:
                raise DetectionStateError(
                    REASON_TRANSITION_INVALID,
                    "the comparison could not be transitioned to 'detecting'",
                    comparison_id=comparison_id,
                )
            row = conn.execute(
                "SELECT * FROM comparison_detection_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return dict(row)


def complete_detection_attempt(
    attempt_id: str,
    *,
    result_json: str,
    result_hash: str,
    detector_version: str,
    workflow_version: str,
    previous_source_hash: str,
    current_source_hash: str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Persist the result and finalize the attempt as succeeded, atomically.

    One BEGIN IMMEDIATE transaction covers: verifying the attempt is still
    running, verifying the comparison is still detecting, verifying the
    detector/workflow versions and source hashes still match what the attempt
    captured at start, inserting the result through the canonical result path,
    marking the attempt succeeded with its finished_at and result_hash,
    appending detection_succeeded, and transitioning detecting -> detected.

    Because it is one transaction, a committed result can never coexist with a
    still-running attempt, and a succeeded attempt can never exist without its
    stored result. Raises DetectionStateError (stable code) for every state or
    input mismatch, and ComparisonResultExists if a result somehow already
    exists — results are never overwritten.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    now = _utc_now_iso()
    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            attempt = _require_running_attempt(conn, attempt_id)
            comparison_id = attempt["comparison_id"]

            status = conn.execute(
                "SELECT status FROM comparisons WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
            if status is None or status["status"] != STATUS_DETECTING:
                raise DetectionStateError(
                    REASON_TRANSITION_INVALID,
                    "the comparison is no longer in 'detecting' state; a "
                    "result is not committed outside its own attempt",
                    comparison_id=comparison_id,
                    attempt_id=attempt_id,
                )
            if (
                attempt["detector_version"] != detector_version
                or attempt["workflow_version"] != workflow_version
                or attempt["previous_source_hash"] != previous_source_hash
                or attempt["current_source_hash"] != current_source_hash
            ):
                raise DetectionStateError(
                    REASON_INPUTS_CHANGED,
                    "the detector/workflow versions or filing source hashes "
                    "no longer match the ones captured when this attempt "
                    "started",
                    comparison_id=comparison_id,
                    attempt_id=attempt_id,
                )
            if conn.execute(
                "SELECT 1 FROM comparison_results WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone() is not None:
                raise ComparisonResultExists(comparison_id)

            _insert_result(
                conn,
                comparison_id,
                result_json=result_json,
                result_hash=result_hash,
                detector_version=detector_version,
                previous_source_hash=previous_source_hash,
                current_source_hash=current_source_hash,
                now=now,
            )
            conn.execute(
                "UPDATE comparison_detection_attempts SET status = ?, "
                "finished_at = ?, result_hash = ? "
                "WHERE attempt_id = ? AND status = ?",
                (ATTEMPT_SUCCEEDED, now, result_hash, attempt_id, ATTEMPT_RUNNING),
            )
            _insert_detection_event(
                conn,
                attempt_id=attempt_id,
                comparison_id=comparison_id,
                event_type=EVENT_DETECTION_SUCCEEDED,
                now=now,
                result_hash=result_hash,
            )
            cursor = conn.execute(
                "UPDATE comparisons SET status = ?, updated_at = ? "
                "WHERE comparison_id = ? AND status = ?",
                (STATUS_DETECTED, now, comparison_id, STATUS_DETECTING),
            )
            if cursor.rowcount != 1:
                raise DetectionStateError(
                    REASON_TRANSITION_INVALID,
                    "the comparison could not be transitioned to 'detected'",
                    comparison_id=comparison_id,
                    attempt_id=attempt_id,
                )
            row = conn.execute(
                "SELECT * FROM comparison_detection_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return dict(row)


def fail_detection_attempt(
    attempt_id: str,
    *,
    failure_code: str,
    failure_summary: str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Finalize a running attempt as failed, atomically.

    One BEGIN IMMEDIATE transaction covers: verifying the attempt is running,
    marking it failed with finished_at / stable code / bounded safe summary,
    appending detection_failed, and transitioning the comparison
    detecting -> failed. The caller is responsible for the summary being safe;
    it is truncated here but never sanitized, so pass a code-derived string —
    never an exception message, path, or SQL.

    If this transaction itself fails, nothing is applied: the attempt stays
    running and the comparison stays detecting (the interruption boundary).
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    now = _utc_now_iso()
    summary = (failure_summary or "detection failed")[:MAX_FAILURE_SUMMARY_CHARS]
    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            attempt = _require_running_attempt(conn, attempt_id)
            comparison_id = attempt["comparison_id"]
            conn.execute(
                "UPDATE comparison_detection_attempts SET status = ?, "
                "finished_at = ?, failure_code = ?, failure_summary = ? "
                "WHERE attempt_id = ? AND status = ?",
                (
                    ATTEMPT_FAILED,
                    now,
                    failure_code,
                    summary,
                    attempt_id,
                    ATTEMPT_RUNNING,
                ),
            )
            _insert_detection_event(
                conn,
                attempt_id=attempt_id,
                comparison_id=comparison_id,
                event_type=EVENT_DETECTION_FAILED,
                now=now,
                failure_code=failure_code,
            )
            cursor = conn.execute(
                "UPDATE comparisons SET status = ?, failure_code = ?, "
                "failure_summary = ?, updated_at = ? "
                "WHERE comparison_id = ? AND status = ?",
                (
                    STATUS_FAILED,
                    failure_code,
                    summary,
                    now,
                    comparison_id,
                    STATUS_DETECTING,
                ),
            )
            if cursor.rowcount != 1:
                raise DetectionStateError(
                    REASON_TRANSITION_INVALID,
                    "the comparison could not be transitioned to 'failed'",
                    comparison_id=comparison_id,
                    attempt_id=attempt_id,
                )
            row = conn.execute(
                "SELECT * FROM comparison_detection_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return dict(row)


def _require_running_attempt(
    conn: sqlite3.Connection, attempt_id: str
) -> sqlite3.Row:
    """Read an attempt that must exist and must still be running."""
    attempt = conn.execute(
        "SELECT * FROM comparison_detection_attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    if attempt is None:
        raise DetectionStateError(
            REASON_ATTEMPT_NOT_FOUND,
            "no detection attempt with this id exists",
            attempt_id=attempt_id,
        )
    if attempt["status"] != ATTEMPT_RUNNING:
        raise DetectionStateError(
            REASON_ATTEMPT_NOT_RUNNING,
            f"the detection attempt is already '{attempt['status']}'; "
            "terminal attempts are never re-finalized",
            comparison_id=attempt["comparison_id"],
            attempt_id=attempt_id,
        )
    return attempt


def get_detection_attempt(
    attempt_id: str, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    """Fetch one detection attempt by id, or None."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM comparison_detection_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    return dict(row) if row else None


def get_running_detection_attempt(
    comparison_id: str, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    """The comparison's running attempt, or None. At most one can exist."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM comparison_detection_attempts "
            "WHERE comparison_id = ? AND status = ?",
            (comparison_id, ATTEMPT_RUNNING),
        ).fetchone()
    return dict(row) if row else None


def list_detection_attempts(
    comparison_id: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Every attempt for a comparison in execution order (attempt_number)."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM comparison_detection_attempts "
            "WHERE comparison_id = ? ORDER BY attempt_number",
            (comparison_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_detection_events(
    attempt_id: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Append-only transition events for one attempt, oldest first.

    Ordered by (event_seq, created_at, event_id): the sequence key makes the
    order deterministic even when two events share a timestamp.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM comparison_detection_events WHERE attempt_id = ? "
            "ORDER BY event_seq, created_at, event_id",
            (attempt_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# --- Durable asynchronous initial-detection jobs -----------------------------


def validate_worker_id(worker_id: Any) -> str:
    """Validate local process metadata used to own one claim."""
    return _bounded_actor_value(
        worker_id, field="worker_id", max_chars=MAX_WORKER_ID_CHARS
    )


def detection_job_request_hash(
    *,
    comparison_id: str,
    detector_version: str,
    workflow_version: str,
    previous_source_hash: str,
    current_source_hash: str,
    requested_by_subject: str,
    requested_by_auth_method: str,
    requested_by_policy_id: str,
    requested_by_policy_version: str,
    trigger_type: str = JOB_TRIGGER_INITIAL_DETECTION,
) -> str:
    """Canonical idempotency hash with no bearer token, secret, or JTI."""
    canonical = json.dumps(
        {
            "comparison_id": comparison_id,
            "detector_version": detector_version,
            "workflow_version": workflow_version,
            "previous_source_hash": previous_source_hash,
            "current_source_hash": current_source_hash,
            "requested_by_subject": requested_by_subject,
            "requested_by_auth_method": requested_by_auth_method,
            "requested_by_policy_id": requested_by_policy_id,
            "requested_by_policy_version": requested_by_policy_version,
            "trigger_type": trigger_type,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def detection_job_id_for(request_hash: str) -> str:
    """Stable collision-resistant identity for an idempotent request."""
    return f"djob_{hashlib.sha256(request_hash.encode('utf-8')).hexdigest()[:24]}"


def _detection_job_event_id(
    job_id: str, event_type: str, event_seq: int
) -> str:
    key = f"{job_id}|{event_type}|{event_seq}"
    return f"djob_evt_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"


def _insert_detection_job_event(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    comparison_id: str,
    event_type: str,
    now: str,
    event_seq: int | None = None,
    attempt_id: str | None = None,
    worker_id: str | None = None,
    claim_generation: int = 0,
    source_attempt_id: str | None = None,
    replacement_attempt_id: str | None = None,
    lease_expires_at: str | None = None,
    result_hash: str | None = None,
    failure_code: str | None = None,
) -> None:
    """Append one job transition. Repeated events get monotonic sequences."""
    if event_seq is None:
        event_seq = conn.execute(
            "SELECT COALESCE(MAX(event_seq), -1) + 1 "
            "FROM comparison_detection_job_events WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO comparison_detection_job_events (
            event_id, job_id, comparison_id, attempt_id, event_type,
            event_seq, created_at, worker_id, claim_generation,
            source_attempt_id, replacement_attempt_id, lease_expires_at,
            result_hash, failure_code
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _detection_job_event_id(job_id, event_type, event_seq),
            job_id,
            comparison_id,
            attempt_id,
            event_type,
            event_seq,
            now,
            worker_id,
            claim_generation,
            source_attempt_id,
            replacement_attempt_id,
            lease_expires_at,
            result_hash,
            failure_code,
        ),
    )


def _safe_job_failure(
    failure_code: Any, failure_summary: Any
) -> tuple[str, str]:
    code = _bounded_actor_value(
        failure_code, field="failure_code", max_chars=120
    )
    summary = _bounded_actor_value(
        failure_summary,
        field="failure_summary",
        max_chars=MAX_FAILURE_SUMMARY_CHARS,
    )
    return code, summary


def enqueue_detection_job(
    comparison_id: str,
    *,
    detector_version: str,
    workflow_version: str,
    previous_filing_id: str,
    current_filing_id: str,
    previous_source_hash: str,
    current_source_hash: str,
    requested_by_subject: str,
    requested_by_auth_method: str,
    requested_by_token_id: str,
    requested_by_policy_id: str,
    requested_by_policy_version: str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically enqueue one authenticated initial-detection request.

    Registry reads happen in the caller before this transaction. The persisted
    comparison identity, current result, active job, and running-attempt state
    are re-read under one BEGIN IMMEDIATE. Returns a tagged outcome:
    ``{"kind": "job", "job": ..., "created": bool}`` or
    ``{"kind": "result", "result": ..., "attempt_id": ...}``.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    if requested_by_auth_method != ACTOR_AUTH_LOCAL_HS256:
        raise ValueError("detection jobs require local_hs256 attribution")
    subject = _bounded_actor_value(
        requested_by_subject,
        field="requested_by_subject",
        max_chars=120,
    )
    attribution = validated_actor_attribution(
        actor_auth_method=requested_by_auth_method,
        actor_token_id=requested_by_token_id,
        actor_policy_id=requested_by_policy_id,
        actor_policy_version=requested_by_policy_version,
    )
    detector_version = _bounded_actor_value(
        detector_version, field="detector_version", max_chars=120
    )
    workflow_version = _bounded_actor_value(
        workflow_version, field="workflow_version", max_chars=120
    )
    request_hash = detection_job_request_hash(
        comparison_id=comparison_id,
        detector_version=detector_version,
        workflow_version=workflow_version,
        previous_source_hash=previous_source_hash,
        current_source_hash=current_source_hash,
        requested_by_subject=subject,
        requested_by_auth_method=requested_by_auth_method,
        requested_by_policy_id=attribution["actor_policy_id"],
        requested_by_policy_version=attribution["actor_policy_version"],
    )
    job_id = detection_job_id_for(request_hash)
    now = _utc_now_iso()
    init_db(db_path)

    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            comparison = conn.execute(
                "SELECT * FROM comparisons WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
            if comparison is None:
                raise ComparisonLifecycleError(
                    comparison_id, "absent", "unknown comparison"
                )
            if (
                comparison["previous_filing_id"] != previous_filing_id
                or comparison["current_filing_id"] != current_filing_id
            ):
                raise DetectionStateError(
                    REASON_JOB_INPUTS_CHANGED,
                    "the comparison filing identity changed before enqueue",
                    comparison_id=comparison_id,
                )
            if comparison["workflow_version"] != workflow_version:
                raise DetectionStateError(
                    REASON_JOB_VERSION_CHANGED,
                    "the comparison workflow version does not match the "
                    "enqueue request",
                    comparison_id=comparison_id,
                )

            stored = conn.execute(
                "SELECT * FROM comparison_results WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
            if stored is not None:
                if stored["detector_version"] != detector_version:
                    raise DetectionStateError(
                        REASON_RESULT_VERSION_SUPERSEDED,
                        "a result from an older detector version already exists",
                        comparison_id=comparison_id,
                    )
                if (
                    stored["previous_source_hash"] != previous_source_hash
                    or stored["current_source_hash"] != current_source_hash
                ):
                    raise DetectionStateError(
                        REASON_RESULT_INPUTS_STALE,
                        "a detection result exists but its source content changed",
                        comparison_id=comparison_id,
                    )
                attempt = conn.execute(
                    "SELECT attempt_id FROM comparison_detection_attempts "
                    "WHERE comparison_id = ? AND status = ? AND result_hash = ? "
                    "ORDER BY attempt_number LIMIT 1",
                    (
                        comparison_id,
                        ATTEMPT_SUCCEEDED,
                        stored["result_hash"],
                    ),
                ).fetchone()
                conn.execute("COMMIT")
                return {
                    "kind": "result",
                    "result": _result_row_to_record(stored),
                    "attempt_id": attempt["attempt_id"] if attempt else None,
                }

            active = conn.execute(
                "SELECT * FROM comparison_detection_jobs "
                "WHERE comparison_id = ? AND status IN (?, ?)",
                (comparison_id, JOB_QUEUED, JOB_RUNNING),
            ).fetchone()
            if active is not None:
                if active["request_hash"] == request_hash:
                    conn.execute("COMMIT")
                    return {
                        "kind": "job",
                        "job": dict(active),
                        "created": False,
                    }
                raise DetectionStateError(
                    REASON_JOB_ACTIVE_CONFLICT,
                    "a different active detection job already exists for this "
                    "comparison",
                    comparison_id=comparison_id,
                    attempt_id=active["attempt_id"],
                )

            running = conn.execute(
                "SELECT attempt_id FROM comparison_detection_attempts "
                "WHERE comparison_id = ? AND status = ?",
                (comparison_id, ATTEMPT_RUNNING),
            ).fetchone()
            if running is not None:
                raise DetectionStateError(
                    REASON_DETECTION_IN_PROGRESS,
                    "a direct or replay detection attempt is already running",
                    comparison_id=comparison_id,
                    attempt_id=running["attempt_id"],
                )
            if comparison["status"] != STATUS_READY_FOR_DETECTION:
                raise DetectionStateError(
                    REASON_COMPARISON_NOT_READY,
                    f"comparison is '{comparison['status']}', not "
                    "'ready_for_detection'",
                    comparison_id=comparison_id,
                )

            conn.execute(
                """
                INSERT INTO comparison_detection_jobs (
                    job_id, comparison_id, attempt_id, trigger_type, status,
                    request_hash, detector_version, workflow_version,
                    previous_source_hash, current_source_hash,
                    requested_by_subject, requested_by_auth_method,
                    requested_by_token_id, requested_by_policy_id,
                    requested_by_policy_version, queued_at, claimed_at,
                    finished_at, worker_id, claim_token_hash,
                    claim_generation, lease_started_at, heartbeat_at,
                    lease_expires_at, result_hash, failure_code,
                    failure_summary
                ) VALUES (
                    ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    NULL, NULL, NULL, NULL, 0, NULL, NULL, NULL,
                    NULL, NULL, NULL
                )
                """,
                (
                    job_id,
                    comparison_id,
                    JOB_TRIGGER_INITIAL_DETECTION,
                    JOB_QUEUED,
                    request_hash,
                    detector_version,
                    workflow_version,
                    previous_source_hash,
                    current_source_hash,
                    subject,
                    requested_by_auth_method,
                    attribution["actor_token_id"],
                    attribution["actor_policy_id"],
                    attribution["actor_policy_version"],
                    now,
                ),
            )
            _insert_detection_job_event(
                conn,
                job_id=job_id,
                comparison_id=comparison_id,
                event_type=EVENT_JOB_QUEUED,
                event_seq=0,
                now=now,
            )
            cursor = conn.execute(
                "UPDATE comparisons SET status = ?, updated_at = ? "
                "WHERE comparison_id = ? AND status = ?",
                (
                    STATUS_QUEUED_FOR_DETECTION,
                    now,
                    comparison_id,
                    STATUS_READY_FOR_DETECTION,
                ),
            )
            if cursor.rowcount != 1:
                raise DetectionStateError(
                    REASON_TRANSITION_INVALID,
                    "the comparison could not be transitioned to "
                    "'queued_for_detection'",
                    comparison_id=comparison_id,
                )
            row = conn.execute(
                "SELECT * FROM comparison_detection_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return {"kind": "job", "job": dict(row), "created": True}


def peek_queued_detection_job(
    *,
    job_id: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read the next claim candidate without initializing or writing storage."""
    path = Path(db_path or config.COMPARISON_DB_PATH)
    with closing(_connect_readonly(path)) as conn:
        if job_id is not None:
            row = conn.execute(
                "SELECT * FROM comparison_detection_jobs "
                "WHERE job_id = ? AND status = ?",
                (job_id, JOB_QUEUED),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM comparison_detection_jobs WHERE status = ? "
                "ORDER BY queued_at, job_id LIMIT 1",
                (JOB_QUEUED,),
            ).fetchone()
    return dict(row) if row else None


def peek_claimable_detection_job(
    *,
    job_id: str | None = None,
    reclaim_grace_seconds: int,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read the next queued or reclaim-eligible job without mutating storage.

    Queued work always wins. With none queued, expired running jobs are ordered
    by parsed lease expiry then job id. Expiry plus grace must be STRICTLY in
    the past, avoiding overlap with the inclusive finalization boundary.
    """
    grace = _validated_policy_int(
        reclaim_grace_seconds,
        field="reclaim_grace_seconds",
        minimum=0,
        maximum=86_400,
    )
    moment = _utc_moment(now)
    path = Path(db_path or config.COMPARISON_DB_PATH)
    with closing(_connect_readonly(path)) as conn:
        if job_id is not None:
            row = conn.execute(
                "SELECT * FROM comparison_detection_jobs "
                "WHERE job_id = ? AND status IN (?, ?)",
                (job_id, JOB_QUEUED, JOB_RUNNING),
            ).fetchone()
            candidates = [row] if row is not None else []
        else:
            queued = conn.execute(
                "SELECT * FROM comparison_detection_jobs WHERE status = ? "
                "ORDER BY queued_at, job_id LIMIT 1",
                (JOB_QUEUED,),
            ).fetchone()
            if queued is not None:
                return dict(queued)
            candidates = conn.execute(
                "SELECT * FROM comparison_detection_jobs WHERE status = ? "
                "ORDER BY lease_expires_at, job_id",
                (JOB_RUNNING,),
            ).fetchall()

    ready: list[tuple[datetime, str, sqlite3.Row]] = []
    for row in candidates:
        if row["status"] == JOB_QUEUED:
            return dict(row)
        expires = parse_utc_timestamp(
            row["lease_expires_at"], field="lease_expires_at"
        )
        if moment > expires + timedelta(seconds=grace):
            ready.append((expires, row["job_id"], row))
    if not ready:
        return None
    ready.sort(key=lambda item: (item[0], item[1]))
    return dict(ready[0][2])


def _fail_queued_job_in_transaction(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    *,
    failure_code: str,
    failure_summary: str,
    now: str,
) -> sqlite3.Row:
    comparison_id = job["comparison_id"]
    comparison = conn.execute(
        "SELECT status FROM comparisons WHERE comparison_id = ?",
        (comparison_id,),
    ).fetchone()
    if (
        comparison is None
        or comparison["status"] != STATUS_QUEUED_FOR_DETECTION
    ):
        raise DetectionStateError(
            REASON_TRANSITION_INVALID,
            "the queued job's comparison is not queued_for_detection",
            comparison_id=comparison_id,
        )
    conn.execute(
        "UPDATE comparison_detection_jobs SET status = ?, finished_at = ?, "
        "failure_code = ?, failure_summary = ? "
        "WHERE job_id = ? AND status = ?",
        (
            JOB_FAILED,
            now,
            failure_code,
            failure_summary,
            job["job_id"],
            JOB_QUEUED,
        ),
    )
    _insert_detection_job_event(
        conn,
        job_id=job["job_id"],
        comparison_id=comparison_id,
        event_type=EVENT_JOB_FAILED,
        event_seq=1,
        now=now,
        failure_code=failure_code,
    )
    conn.execute(
        "UPDATE comparisons SET status = ?, failure_code = ?, "
        "failure_summary = ?, updated_at = ? "
        "WHERE comparison_id = ? AND status = ?",
        (
            STATUS_FAILED,
            failure_code,
            failure_summary,
            now,
            comparison_id,
            STATUS_QUEUED_FOR_DETECTION,
        ),
    )
    return conn.execute(
        "SELECT * FROM comparison_detection_jobs WHERE job_id = ?",
        (job["job_id"],),
    ).fetchone()


def fail_queued_detection_job(
    job_id: str,
    *,
    failure_code: str,
    failure_summary: str,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Fail a still-queued job before an attempt exists; race-safe."""
    db_path = db_path or config.COMPARISON_DB_PATH
    code, summary = _safe_job_failure(failure_code, failure_summary)
    now = _utc_now_iso()
    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = conn.execute(
                "SELECT * FROM comparison_detection_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if job is None or job["status"] != JOB_QUEUED:
                conn.execute("COMMIT")
                return None
            row = _fail_queued_job_in_transaction(
                conn,
                job,
                failure_code=code,
                failure_summary=summary,
                now=now,
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return dict(row)


def _insert_running_job_attempt(
    conn: sqlite3.Connection, job: sqlite3.Row, *, now: str
) -> sqlite3.Row:
    comparison_id = job["comparison_id"]
    next_number = conn.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) + 1 "
        "FROM comparison_detection_attempts WHERE comparison_id = ?",
        (comparison_id,),
    ).fetchone()[0]
    attempt_id = detection_attempt_id_for(
        comparison_id,
        next_number,
        detector_version=job["detector_version"],
        workflow_version=job["workflow_version"],
        previous_source_hash=job["previous_source_hash"],
        current_source_hash=job["current_source_hash"],
    )
    conn.execute(
        """
        INSERT INTO comparison_detection_attempts (
            attempt_id, comparison_id, attempt_number, status,
            detector_version, workflow_version,
            previous_source_hash, current_source_hash,
            started_at, finished_at, result_hash,
            failure_code, failure_summary
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
        """,
        (
            attempt_id,
            comparison_id,
            next_number,
            ATTEMPT_RUNNING,
            job["detector_version"],
            job["workflow_version"],
            job["previous_source_hash"],
            job["current_source_hash"],
            now,
        ),
    )
    _insert_detection_event(
        conn,
        attempt_id=attempt_id,
        comparison_id=comparison_id,
        event_type=EVENT_DETECTION_STARTED,
        now=now,
    )
    return conn.execute(
        "SELECT * FROM comparison_detection_attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()


def _time_out_worker_attempt(
    conn: sqlite3.Connection, attempt: sqlite3.Row, *, now: str
) -> sqlite3.Row:
    cursor = conn.execute(
        "UPDATE comparison_detection_attempts SET status = ?, "
        "finished_at = ?, failure_code = ?, failure_summary = ? "
        "WHERE attempt_id = ? AND status = ?",
        (
            ATTEMPT_TIMED_OUT,
            now,
            FAILURE_ATTEMPT_WORKER_LEASE_EXPIRED,
            WORKER_LEASE_EXPIRED_SUMMARY[:MAX_FAILURE_SUMMARY_CHARS],
            attempt["attempt_id"],
            ATTEMPT_RUNNING,
        ),
    )
    if cursor.rowcount != 1:
        raise DetectionStateError(
            REASON_JOB_CLAIM_FENCED,
            "the expired detection attempt is no longer owned by this claim",
            comparison_id=attempt["comparison_id"],
            attempt_id=attempt["attempt_id"],
        )
    _insert_detection_event(
        conn,
        attempt_id=attempt["attempt_id"],
        comparison_id=attempt["comparison_id"],
        event_type=EVENT_DETECTION_TIMED_OUT,
        now=now,
        failure_code=FAILURE_ATTEMPT_WORKER_LEASE_EXPIRED,
    )
    return conn.execute(
        "SELECT * FROM comparison_detection_attempts WHERE attempt_id = ?",
        (attempt["attempt_id"],),
    ).fetchone()


def _fail_expired_running_job(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    attempt: sqlite3.Row,
    *,
    failure_code: str,
    failure_summary: str,
    now: str,
    claim_exhausted: bool = False,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    timed_out = _time_out_worker_attempt(conn, attempt, now=now)
    if claim_exhausted:
        _insert_detection_job_event(
            conn,
            job_id=job["job_id"],
            comparison_id=job["comparison_id"],
            attempt_id=attempt["attempt_id"],
            event_type=EVENT_JOB_CLAIM_EXHAUSTED,
            now=now,
            worker_id=job["worker_id"],
            claim_generation=job["claim_generation"],
            source_attempt_id=attempt["attempt_id"],
            lease_expires_at=job["lease_expires_at"],
            failure_code=REASON_JOB_CLAIMS_EXHAUSTED,
        )
    cursor = conn.execute(
        "UPDATE comparison_detection_jobs SET status = ?, finished_at = ?, "
        "failure_code = ?, failure_summary = ? "
        "WHERE job_id = ? AND status = ?",
        (
            JOB_FAILED,
            now,
            failure_code,
            failure_summary[:MAX_FAILURE_SUMMARY_CHARS],
            job["job_id"],
            JOB_RUNNING,
        ),
    )
    if cursor.rowcount != 1:
        raise DetectionStateError(
            REASON_JOB_CLAIM_FENCED,
            "the expired detection job is no longer running",
            comparison_id=job["comparison_id"],
            attempt_id=attempt["attempt_id"],
        )
    _insert_detection_job_event(
        conn,
        job_id=job["job_id"],
        comparison_id=job["comparison_id"],
        attempt_id=attempt["attempt_id"],
        event_type=EVENT_JOB_FAILED,
        now=now,
        worker_id=job["worker_id"],
        claim_generation=job["claim_generation"],
        lease_expires_at=job["lease_expires_at"],
        failure_code=failure_code,
    )
    cursor = conn.execute(
        "UPDATE comparisons SET status = ?, failure_code = ?, "
        "failure_summary = ?, updated_at = ? "
        "WHERE comparison_id = ? AND status = ?",
        (
            STATUS_FAILED,
            failure_code,
            failure_summary[:MAX_FAILURE_SUMMARY_CHARS],
            now,
            job["comparison_id"],
            STATUS_DETECTING,
        ),
    )
    if cursor.rowcount != 1:
        raise DetectionStateError(
            REASON_TRANSITION_INVALID,
            "the comparison could not be failed after lease expiry",
            comparison_id=job["comparison_id"],
            attempt_id=attempt["attempt_id"],
        )
    final_job = conn.execute(
        "SELECT * FROM comparison_detection_jobs WHERE job_id = ?",
        (job["job_id"],),
    ).fetchone()
    return final_job, timed_out


def claim_detection_job(
    *,
    worker_id: str,
    detector_version: str,
    workflow_version: str,
    previous_source_hash: str,
    current_source_hash: str,
    lease_duration_seconds: int,
    reclaim_grace_seconds: int,
    max_claim_generations: int,
    job_id: str | None = None,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Claim queued work or atomically reclaim one expired running claim."""
    db_path = db_path or config.COMPARISON_DB_PATH
    worker = validate_worker_id(worker_id)
    lease_seconds = _validated_policy_int(
        lease_duration_seconds,
        field="lease_duration_seconds",
        minimum=1,
        maximum=86_400,
    )
    grace_seconds = _validated_policy_int(
        reclaim_grace_seconds,
        field="reclaim_grace_seconds",
        minimum=0,
        maximum=86_400,
    )
    max_generations = _validated_policy_int(
        max_claim_generations,
        field="max_claim_generations",
        minimum=1,
        maximum=1_000,
    )
    init_db(db_path)
    moment = _utc_moment(now)
    now_iso = moment.isoformat()
    lease_expires_at = (
        moment + timedelta(seconds=lease_seconds)
    ).isoformat()

    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            if job_id is not None:
                job = conn.execute(
                    "SELECT * FROM comparison_detection_jobs "
                    "WHERE job_id = ? AND status IN (?, ?)",
                    (job_id, JOB_QUEUED, JOB_RUNNING),
                ).fetchone()
            else:
                job = conn.execute(
                    "SELECT * FROM comparison_detection_jobs WHERE status = ? "
                    "ORDER BY queued_at, job_id LIMIT 1",
                    (JOB_QUEUED,),
                ).fetchone()
                if job is None:
                    running = conn.execute(
                        "SELECT * FROM comparison_detection_jobs "
                        "WHERE status = ? ORDER BY lease_expires_at, job_id",
                        (JOB_RUNNING,),
                    ).fetchall()
                    job = next(
                        (
                            item
                            for item in running
                            if moment
                            > parse_utc_timestamp(
                                item["lease_expires_at"],
                                field="lease_expires_at",
                            )
                            + timedelta(seconds=grace_seconds)
                        ),
                        None,
                    )
            if job is None:
                conn.execute("COMMIT")
                return None

            comparison = conn.execute(
                "SELECT * FROM comparisons WHERE comparison_id = ?",
                (job["comparison_id"],),
            ).fetchone()
            version_changed = (
                job["detector_version"] != detector_version
                or job["workflow_version"] != workflow_version
                or comparison is None
                or comparison["workflow_version"] != workflow_version
            )
            inputs_changed = (
                job["previous_source_hash"] != previous_source_hash
                or job["current_source_hash"] != current_source_hash
            )

            if job["status"] == JOB_QUEUED:
                if (
                    comparison is None
                    or comparison["status"] != STATUS_QUEUED_FOR_DETECTION
                ):
                    raise DetectionStateError(
                        REASON_TRANSITION_INVALID,
                        "the queued job's comparison is not queued_for_detection",
                        comparison_id=job["comparison_id"],
                    )
                if version_changed or inputs_changed:
                    code = (
                        REASON_JOB_VERSION_CHANGED
                        if version_changed
                        else REASON_JOB_INPUTS_CHANGED
                    )
                    summary = (
                        "the detector or workflow version changed before worker claim"
                        if version_changed
                        else "the filing source content changed before worker claim"
                    )
                    failed = _fail_queued_job_in_transaction(
                        conn,
                        job,
                        failure_code=code,
                        failure_summary=summary,
                        now=now_iso,
                    )
                    conn.execute("COMMIT")
                    return {"kind": "failed", "job": dict(failed)}
                running = conn.execute(
                    "SELECT attempt_id FROM comparison_detection_attempts "
                    "WHERE comparison_id = ? AND status = ?",
                    (job["comparison_id"], ATTEMPT_RUNNING),
                ).fetchone()
                if running is not None:
                    raise DetectionStateError(
                        REASON_DETECTION_IN_PROGRESS,
                        "a detection attempt is already running for this comparison",
                        comparison_id=job["comparison_id"],
                        attempt_id=running["attempt_id"],
                    )
                attempt = _insert_running_job_attempt(
                    conn, job, now=now_iso
                )
                raw_claim_token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(
                    raw_claim_token.encode("utf-8")
                ).hexdigest()
                cursor = conn.execute(
                    "UPDATE comparison_detection_jobs SET status = ?, "
                    "attempt_id = ?, claimed_at = ?, worker_id = ?, "
                    "claim_token_hash = ?, claim_generation = 1, "
                    "lease_started_at = ?, heartbeat_at = ?, "
                    "lease_expires_at = ? "
                    "WHERE job_id = ? AND status = ?",
                    (
                        JOB_RUNNING,
                        attempt["attempt_id"],
                        now_iso,
                        worker,
                        token_hash,
                        now_iso,
                        now_iso,
                        lease_expires_at,
                        job["job_id"],
                        JOB_QUEUED,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DetectionStateError(
                        REASON_JOB_CLAIM_FENCED,
                        "the queued detection job was claimed concurrently",
                        comparison_id=job["comparison_id"],
                        attempt_id=attempt["attempt_id"],
                    )
                _insert_detection_job_event(
                    conn,
                    job_id=job["job_id"],
                    comparison_id=job["comparison_id"],
                    attempt_id=attempt["attempt_id"],
                    event_type=EVENT_JOB_CLAIMED,
                    event_seq=1,
                    now=now_iso,
                    worker_id=worker,
                    claim_generation=1,
                    lease_expires_at=lease_expires_at,
                )
                cursor = conn.execute(
                    "UPDATE comparisons SET status = ?, updated_at = ? "
                    "WHERE comparison_id = ? AND status = ?",
                    (
                        STATUS_DETECTING,
                        now_iso,
                        job["comparison_id"],
                        STATUS_QUEUED_FOR_DETECTION,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DetectionStateError(
                        REASON_TRANSITION_INVALID,
                        "the comparison could not be transitioned to 'detecting'",
                        comparison_id=job["comparison_id"],
                        attempt_id=attempt["attempt_id"],
                    )
                outcome_kind = "claimed"
                source_attempt = None
            else:
                expiry = parse_utc_timestamp(
                    job["lease_expires_at"], field="lease_expires_at"
                )
                if moment <= expiry + timedelta(seconds=grace_seconds):
                    conn.execute("COMMIT")
                    return None
                if comparison is None or comparison["status"] != STATUS_DETECTING:
                    raise DetectionStateError(
                        REASON_TRANSITION_INVALID,
                        "the expired job's comparison is not detecting",
                        comparison_id=job["comparison_id"],
                        attempt_id=job["attempt_id"],
                    )
                source_attempt = _require_running_attempt(
                    conn, job["attempt_id"]
                )
                if source_attempt["comparison_id"] != job["comparison_id"]:
                    raise DetectionStateError(
                        REASON_JOB_ATTEMPT_MISMATCH,
                        "the expired attempt belongs to another comparison",
                        comparison_id=job["comparison_id"],
                        attempt_id=job["attempt_id"],
                    )
                running = conn.execute(
                    "SELECT attempt_id FROM comparison_detection_attempts "
                    "WHERE comparison_id = ? AND status = ?",
                    (job["comparison_id"], ATTEMPT_RUNNING),
                ).fetchall()
                if len(running) != 1 or (
                    running[0]["attempt_id"] != source_attempt["attempt_id"]
                ):
                    raise DetectionStateError(
                        REASON_TRANSITION_INVALID,
                        "the expired attempt is not the only running attempt",
                        comparison_id=job["comparison_id"],
                        attempt_id=source_attempt["attempt_id"],
                    )
                if version_changed or inputs_changed:
                    code = (
                        REASON_JOB_VERSION_CHANGED
                        if version_changed
                        else REASON_JOB_INPUTS_CHANGED
                    )
                    summary = (
                        "the detector or workflow version changed before reclaim"
                        if version_changed
                        else "the filing source content changed before reclaim"
                    )
                    final_job, timed_out = _fail_expired_running_job(
                        conn,
                        job,
                        source_attempt,
                        failure_code=code,
                        failure_summary=summary,
                        now=now_iso,
                    )
                    conn.execute("COMMIT")
                    return {
                        "kind": "failed",
                        "job": dict(final_job),
                        "attempt": dict(timed_out),
                    }
                if job["claim_generation"] >= max_generations:
                    final_job, timed_out = _fail_expired_running_job(
                        conn,
                        job,
                        source_attempt,
                        failure_code=REASON_JOB_CLAIMS_EXHAUSTED,
                        failure_summary=JOB_CLAIMS_EXHAUSTED_SUMMARY,
                        now=now_iso,
                        claim_exhausted=True,
                    )
                    conn.execute("COMMIT")
                    return {
                        "kind": "exhausted",
                        "job": dict(final_job),
                        "attempt": dict(timed_out),
                    }

                source_attempt = _time_out_worker_attempt(
                    conn, source_attempt, now=now_iso
                )
                attempt = _insert_running_job_attempt(
                    conn, job, now=now_iso
                )
                raw_claim_token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(
                    raw_claim_token.encode("utf-8")
                ).hexdigest()
                generation = job["claim_generation"] + 1
                cursor = conn.execute(
                    "UPDATE comparison_detection_jobs SET attempt_id = ?, "
                    "worker_id = ?, claim_token_hash = ?, "
                    "claim_generation = ?, lease_started_at = ?, "
                    "heartbeat_at = ?, lease_expires_at = ? "
                    "WHERE job_id = ? AND status = ? "
                    "AND claim_generation = ?",
                    (
                        attempt["attempt_id"],
                        worker,
                        token_hash,
                        generation,
                        now_iso,
                        now_iso,
                        lease_expires_at,
                        job["job_id"],
                        JOB_RUNNING,
                        job["claim_generation"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise DetectionStateError(
                        REASON_JOB_CLAIM_FENCED,
                        "the expired detection job claim changed during reclaim",
                        comparison_id=job["comparison_id"],
                        attempt_id=source_attempt["attempt_id"],
                    )
                _insert_detection_job_event(
                    conn,
                    job_id=job["job_id"],
                    comparison_id=job["comparison_id"],
                    attempt_id=attempt["attempt_id"],
                    event_type=EVENT_JOB_RECLAIMED,
                    now=now_iso,
                    worker_id=worker,
                    claim_generation=generation,
                    source_attempt_id=source_attempt["attempt_id"],
                    replacement_attempt_id=attempt["attempt_id"],
                    lease_expires_at=lease_expires_at,
                )
                conn.execute(
                    "UPDATE comparisons SET updated_at = ? "
                    "WHERE comparison_id = ? AND status = ?",
                    (now_iso, job["comparison_id"], STATUS_DETECTING),
                )
                outcome_kind = "reclaimed"

            claimed_job = conn.execute(
                "SELECT * FROM comparison_detection_jobs WHERE job_id = ?",
                (job["job_id"],),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    outcome = {
        "kind": outcome_kind,
        "job": dict(claimed_job),
        "attempt": dict(attempt),
        "claim_token": raw_claim_token,
    }
    if source_attempt is not None:
        outcome["source_attempt"] = dict(source_attempt)
    return outcome


def _require_running_job_claim(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    attempt_id: str,
    worker_id: str,
    claim_generation: int,
    claim_token: str,
    now: datetime,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    job = conn.execute(
        "SELECT * FROM comparison_detection_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if job is None:
        raise DetectionStateError(
            REASON_JOB_NOT_FOUND, "no detection job with this id exists"
        )
    if job["status"] != JOB_RUNNING:
        raise DetectionStateError(
            REASON_JOB_NOT_RUNNING,
            f"the detection job is already '{job['status']}'",
            comparison_id=job["comparison_id"],
            attempt_id=job["attempt_id"],
        )
    generation = _validated_policy_int(
        claim_generation,
        field="claim_generation",
        minimum=1,
        maximum=1_000_000,
    )
    if job["claim_generation"] != generation:
        raise DetectionStateError(
            REASON_JOB_CLAIM_FENCED,
            "the detection job claim generation is no longer current",
            comparison_id=job["comparison_id"],
            attempt_id=attempt_id,
        )
    worker = validate_worker_id(worker_id)
    if job["worker_id"] != worker:
        raise DetectionStateError(
            REASON_JOB_WORKER_MISMATCH,
            "the worker id does not own this detection job claim",
            comparison_id=job["comparison_id"],
            attempt_id=job["attempt_id"],
        )
    if job["attempt_id"] != attempt_id:
        raise DetectionStateError(
            REASON_JOB_ATTEMPT_MISMATCH,
            "the attempt does not match the detection job claim",
            comparison_id=job["comparison_id"],
            attempt_id=attempt_id,
        )
    if not isinstance(claim_token, str) or not claim_token:
        supplied_hash = ""
    else:
        supplied_hash = hashlib.sha256(claim_token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(job["claim_token_hash"], supplied_hash):
        raise DetectionStateError(
            REASON_JOB_CLAIM_INVALID,
            "the detection job claim is invalid",
            comparison_id=job["comparison_id"],
            attempt_id=attempt_id,
        )
    expires = parse_utc_timestamp(
        job["lease_expires_at"], field="lease_expires_at"
    )
    if now > expires:
        raise DetectionStateError(
            REASON_JOB_LEASE_EXPIRED,
            "the detection job claim lease has expired",
            comparison_id=job["comparison_id"],
            attempt_id=attempt_id,
        )
    attempt = _require_running_attempt(conn, attempt_id)
    if attempt["comparison_id"] != job["comparison_id"]:
        raise DetectionStateError(
            REASON_JOB_ATTEMPT_MISMATCH,
            "the attempt belongs to a different comparison",
            comparison_id=job["comparison_id"],
            attempt_id=attempt_id,
        )
    comparison = conn.execute(
        "SELECT * FROM comparisons WHERE comparison_id = ?",
        (job["comparison_id"],),
    ).fetchone()
    if comparison is None or comparison["status"] != STATUS_DETECTING:
        raise DetectionStateError(
            REASON_TRANSITION_INVALID,
            "the claimed job's comparison is not detecting",
            comparison_id=job["comparison_id"],
            attempt_id=attempt_id,
        )
    return job, attempt, comparison


def heartbeat_detection_job(
    job_id: str,
    *,
    worker_id: str,
    claim_generation: int,
    claim_token: str,
    heartbeat_extension_seconds: int,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Extend one still-active lease and append a repeatable heartbeat event."""
    db_path = db_path or config.COMPARISON_DB_PATH
    extension = _validated_policy_int(
        heartbeat_extension_seconds,
        field="heartbeat_extension_seconds",
        minimum=1,
        maximum=86_400,
    )
    moment = _utc_moment(now)
    now_iso = moment.isoformat()
    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                "SELECT * FROM comparison_detection_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if current is None:
                raise DetectionStateError(
                    REASON_JOB_NOT_FOUND,
                    "no detection job with this id exists",
                )
            job, _attempt, _comparison = _require_running_job_claim(
                conn,
                job_id=job_id,
                attempt_id=current["attempt_id"] or "",
                worker_id=worker_id,
                claim_generation=claim_generation,
                claim_token=claim_token,
                now=moment,
            )
            previous_heartbeat = parse_utc_timestamp(
                job["heartbeat_at"], field="heartbeat_at"
            )
            if moment < previous_heartbeat:
                raise DetectionStateError(
                    REASON_JOB_CLOCK_INVALID,
                    "heartbeat time precedes the previous heartbeat",
                    comparison_id=job["comparison_id"],
                    attempt_id=job["attempt_id"],
                )
            current_expiry = parse_utc_timestamp(
                job["lease_expires_at"], field="lease_expires_at"
            )
            extended = max(
                current_expiry,
                moment + timedelta(seconds=extension),
            )
            lease_expires_at = extended.isoformat()
            cursor = conn.execute(
                "UPDATE comparison_detection_jobs SET heartbeat_at = ?, "
                "lease_expires_at = ? WHERE job_id = ? AND status = ? "
                "AND claim_generation = ?",
                (
                    now_iso,
                    lease_expires_at,
                    job_id,
                    JOB_RUNNING,
                    claim_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise DetectionStateError(
                    REASON_JOB_CLAIM_FENCED,
                    "the detection job claim changed during heartbeat",
                    comparison_id=job["comparison_id"],
                    attempt_id=job["attempt_id"],
                )
            _insert_detection_job_event(
                conn,
                job_id=job_id,
                comparison_id=job["comparison_id"],
                attempt_id=job["attempt_id"],
                event_type=EVENT_JOB_HEARTBEAT,
                now=now_iso,
                worker_id=job["worker_id"],
                claim_generation=job["claim_generation"],
                lease_expires_at=lease_expires_at,
            )
            updated = conn.execute(
                "SELECT * FROM comparison_detection_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return dict(updated)


def _canonical_result_hash(result_json: str) -> str:
    parsed = json.loads(result_json)
    if not isinstance(parsed, dict):
        raise ValueError("result_json must contain an object")
    stable = {key: value for key, value in parsed.items() if key != "created_at"}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True).encode("utf-8")
    ).hexdigest()


def complete_detection_job(
    job_id: str,
    attempt_id: str,
    *,
    worker_id: str,
    claim_generation: int,
    claim_token: str,
    result_json: str,
    result_hash: str,
    detector_version: str,
    workflow_version: str,
    previous_source_hash: str,
    current_source_hash: str,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically finalize job, attempt, result, events, and comparison."""
    db_path = db_path or config.COMPARISON_DB_PATH
    if _canonical_result_hash(result_json) != result_hash:
        raise DetectionStateError(
            REASON_JOB_RESULT_HASH_MISMATCH,
            "the supplied result hash does not match the canonical result",
            attempt_id=attempt_id,
        )
    moment = _utc_moment(now)
    now_iso = moment.isoformat()
    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            job, attempt, _comparison = _require_running_job_claim(
                conn,
                job_id=job_id,
                attempt_id=attempt_id,
                worker_id=worker_id,
                claim_generation=claim_generation,
                claim_token=claim_token,
                now=moment,
            )
            expected = (
                detector_version,
                workflow_version,
                previous_source_hash,
                current_source_hash,
            )
            if expected != (
                job["detector_version"],
                job["workflow_version"],
                job["previous_source_hash"],
                job["current_source_hash"],
            ) or expected != (
                attempt["detector_version"],
                attempt["workflow_version"],
                attempt["previous_source_hash"],
                attempt["current_source_hash"],
            ):
                raise DetectionStateError(
                    REASON_INPUTS_CHANGED,
                    "job, attempt, detector/workflow versions, or source hashes "
                    "do not match",
                    comparison_id=job["comparison_id"],
                    attempt_id=attempt_id,
                )
            if conn.execute(
                "SELECT 1 FROM comparison_results WHERE comparison_id = ?",
                (job["comparison_id"],),
            ).fetchone() is not None:
                raise ComparisonResultExists(job["comparison_id"])
            _insert_result(
                conn,
                job["comparison_id"],
                result_json=result_json,
                result_hash=result_hash,
                detector_version=detector_version,
                previous_source_hash=previous_source_hash,
                current_source_hash=current_source_hash,
                now=now_iso,
            )
            conn.execute(
                "UPDATE comparison_detection_attempts SET status = ?, "
                "finished_at = ?, result_hash = ? "
                "WHERE attempt_id = ? AND status = ?",
                (
                    ATTEMPT_SUCCEEDED,
                    now_iso,
                    result_hash,
                    attempt_id,
                    ATTEMPT_RUNNING,
                ),
            )
            _insert_detection_event(
                conn,
                attempt_id=attempt_id,
                comparison_id=job["comparison_id"],
                event_type=EVENT_DETECTION_SUCCEEDED,
                now=now_iso,
                result_hash=result_hash,
            )
            conn.execute(
                "UPDATE comparison_detection_jobs SET status = ?, "
                "finished_at = ?, result_hash = ? "
                "WHERE job_id = ? AND status = ?",
                (JOB_SUCCEEDED, now_iso, result_hash, job_id, JOB_RUNNING),
            )
            _insert_detection_job_event(
                conn,
                job_id=job_id,
                comparison_id=job["comparison_id"],
                attempt_id=attempt_id,
                event_type=EVENT_JOB_SUCCEEDED,
                now=now_iso,
                worker_id=job["worker_id"],
                claim_generation=job["claim_generation"],
                lease_expires_at=job["lease_expires_at"],
                result_hash=result_hash,
            )
            cursor = conn.execute(
                "UPDATE comparisons SET status = ?, updated_at = ? "
                "WHERE comparison_id = ? AND status = ?",
                (
                    STATUS_DETECTED,
                    now_iso,
                    job["comparison_id"],
                    STATUS_DETECTING,
                ),
            )
            if cursor.rowcount != 1:
                raise DetectionStateError(
                    REASON_TRANSITION_INVALID,
                    "the comparison could not be transitioned to 'detected'",
                    comparison_id=job["comparison_id"],
                    attempt_id=attempt_id,
                )
            final_job = conn.execute(
                "SELECT * FROM comparison_detection_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            final_attempt = conn.execute(
                "SELECT * FROM comparison_detection_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return {"job": dict(final_job), "attempt": dict(final_attempt)}


def fail_detection_job(
    job_id: str,
    attempt_id: str,
    *,
    worker_id: str,
    claim_generation: int,
    claim_token: str,
    failure_code: str,
    failure_summary: str,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically fail a claimed job and its linked running attempt."""
    db_path = db_path or config.COMPARISON_DB_PATH
    code, summary = _safe_job_failure(failure_code, failure_summary)
    moment = _utc_moment(now)
    now_iso = moment.isoformat()
    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            job, _attempt, _comparison = _require_running_job_claim(
                conn,
                job_id=job_id,
                attempt_id=attempt_id,
                worker_id=worker_id,
                claim_generation=claim_generation,
                claim_token=claim_token,
                now=moment,
            )
            conn.execute(
                "UPDATE comparison_detection_attempts SET status = ?, "
                "finished_at = ?, failure_code = ?, failure_summary = ? "
                "WHERE attempt_id = ? AND status = ?",
                (
                    ATTEMPT_FAILED,
                    now_iso,
                    code,
                    summary,
                    attempt_id,
                    ATTEMPT_RUNNING,
                ),
            )
            _insert_detection_event(
                conn,
                attempt_id=attempt_id,
                comparison_id=job["comparison_id"],
                event_type=EVENT_DETECTION_FAILED,
                now=now_iso,
                failure_code=code,
            )
            conn.execute(
                "UPDATE comparison_detection_jobs SET status = ?, "
                "finished_at = ?, failure_code = ?, failure_summary = ? "
                "WHERE job_id = ? AND status = ?",
                (
                    JOB_FAILED,
                    now_iso,
                    code,
                    summary,
                    job_id,
                    JOB_RUNNING,
                ),
            )
            _insert_detection_job_event(
                conn,
                job_id=job_id,
                comparison_id=job["comparison_id"],
                attempt_id=attempt_id,
                event_type=EVENT_JOB_FAILED,
                now=now_iso,
                worker_id=job["worker_id"],
                claim_generation=job["claim_generation"],
                lease_expires_at=job["lease_expires_at"],
                failure_code=code,
            )
            cursor = conn.execute(
                "UPDATE comparisons SET status = ?, failure_code = ?, "
                "failure_summary = ?, updated_at = ? "
                "WHERE comparison_id = ? AND status = ?",
                (
                    STATUS_FAILED,
                    code,
                    summary,
                    now_iso,
                    job["comparison_id"],
                    STATUS_DETECTING,
                ),
            )
            if cursor.rowcount != 1:
                raise DetectionStateError(
                    REASON_TRANSITION_INVALID,
                    "the comparison could not be transitioned to 'failed'",
                    comparison_id=job["comparison_id"],
                    attempt_id=attempt_id,
                )
            final_job = conn.execute(
                "SELECT * FROM comparison_detection_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            final_attempt = conn.execute(
                "SELECT * FROM comparison_detection_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return {"job": dict(final_job), "attempt": dict(final_attempt)}


def get_detection_job(
    job_id: str, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM comparison_detection_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


def get_detection_job_for_attempt(
    attempt_id: str, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM comparison_detection_jobs WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    return dict(row) if row else None


def list_detection_jobs(
    comparison_id: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM comparison_detection_jobs WHERE comparison_id = ? "
            "ORDER BY queued_at, job_id",
            (comparison_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_detection_job_events(
    job_id: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM comparison_detection_job_events WHERE job_id = ? "
            "ORDER BY event_seq, created_at, event_id",
            (job_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# --- Staleness: pure, side-effect-free ---------------------------------------


def parse_utc_timestamp(value: Any, *, field: str = "timestamp") -> datetime:
    """Parse a stored timezone-aware UTC ISO 8601 timestamp.

    NAIVE TIMESTAMPS ARE REJECTED rather than assumed to be UTC: silently
    guessing a timezone would make an age calculation quietly wrong by hours,
    which for a staleness threshold is the difference between refusing a replay
    and declaring a live run dead. A trailing 'Z' is accepted defensively even
    though every writer here emits the '+00:00' offset form.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO 8601 string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(
            f"{field} must be timezone-aware; a naive timestamp is never "
            "assumed to be UTC"
        )
    return parsed.astimezone(timezone.utc)


def evaluate_staleness(
    started_at: Any, now: datetime, stale_after_seconds: int
) -> dict[str, Any]:
    """Pure staleness evaluation. No storage access, no side effects.

    Returns ``{age_seconds, stale_at, is_stale}``. The boundary is INCLUSIVE:
    ``age_seconds >= stale_after_seconds`` is stale. A negative age — the clock
    moved backwards, or the row was written by a host whose clock is ahead — is
    never stale: clock skew must not be able to authorize retiring a live
    attempt.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")
    started = parse_utc_timestamp(started_at, field="started_at")
    stale_at = started + timedelta(seconds=stale_after_seconds)
    age_seconds = (now.astimezone(timezone.utc) - started).total_seconds()
    return {
        "age_seconds": age_seconds,
        "stale_at": stale_at.isoformat(),
        "is_stale": age_seconds >= 0 and age_seconds >= stale_after_seconds,
    }


# --- Operator-controlled replay ----------------------------------------------


def detection_replay_id_for(source_attempt_id: str, request_hash: str) -> str:
    """Deterministic replay id: a byte-equivalent request lands on the same row."""
    key = f"{source_attempt_id}|{request_hash}"
    return f"rpl_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


def start_detection_replay(
    source_attempt_id: str,
    *,
    operator_id: str,
    reason_code: str,
    operator_note: str,
    request_hash: str,
    policy_id: str,
    policy_version: str,
    stale_after_seconds: int,
    max_attempts_per_comparison: int,
    detector_version: str,
    workflow_version: str,
    previous_source_hash: str,
    current_source_hash: str,
    actor_auth_method: str = ACTOR_AUTH_LEGACY_SELF_ASSERTED,
    actor_token_id: str | None = None,
    actor_policy_id: str | None = None,
    actor_policy_version: str | None = None,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Retire a stale running attempt and start its replacement, atomically.

    ONE BEGIN IMMEDIATE transaction covers every check and every write, so the
    database can never be observed with two running attempts, with no running
    attempt after a successful replay start, with a timed-out source and no
    replacement, or with a replay row whose linked attempts do not both exist.

    Returns ``(replay_record, created)``. ``created=False`` means this exact
    request was already applied and the stored replay is returned unchanged —
    the replacement is NOT executed twice. A DIFFERENT request against an
    already-replayed attempt raises DetectionStateError with
    ``detection_replay_already_exists``.

    ``now`` is injected so staleness is testable without sleeping. The
    transaction is closed before returning: the caller runs the detector
    afterwards, never inside it.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    attribution = validated_actor_attribution(
        actor_auth_method=actor_auth_method,
        actor_token_id=actor_token_id,
        actor_policy_id=actor_policy_id,
        actor_policy_version=actor_policy_version,
    )
    init_db(db_path)
    moment = now or datetime.now(timezone.utc)
    now_iso = moment.astimezone(timezone.utc).isoformat()

    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            source = conn.execute(
                "SELECT * FROM comparison_detection_attempts WHERE attempt_id = ?",
                (source_attempt_id,),
            ).fetchone()
            if source is None:
                raise DetectionStateError(
                    REASON_ATTEMPT_NOT_FOUND,
                    "no detection attempt with this id exists",
                    attempt_id=source_attempt_id,
                )

            # IDEMPOTENCY IS CHECKED FIRST, before any lifecycle check. After a
            # replay the source attempt is timed_out and the comparison may
            # already be detected, so the lifecycle checks below would misreport
            # a legitimate replay of an existing request as a state error.
            existing = conn.execute(
                "SELECT * FROM comparison_detection_replays "
                "WHERE source_attempt_id = ?",
                (source_attempt_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] == request_hash:
                    record = dict(existing)
                    conn.execute("COMMIT")
                    return record, False
                raise DetectionStateError(
                    REASON_REPLAY_ALREADY_EXISTS,
                    "this attempt has already been replaced by a different "
                    "replay request; a stale attempt yields at most one "
                    "replacement",
                    comparison_id=source["comparison_id"],
                    attempt_id=source_attempt_id,
                )

            linked_job = conn.execute(
                "SELECT job_id FROM comparison_detection_jobs "
                "WHERE attempt_id = ?",
                (source_attempt_id,),
            ).fetchone()
            if linked_job is not None:
                raise DetectionStateError(
                    REASON_ATTEMPT_MANAGED_BY_JOB,
                    ATTEMPT_MANAGED_BY_JOB_MESSAGE,
                    comparison_id=source["comparison_id"],
                    attempt_id=source_attempt_id,
                )

            comparison_id = source["comparison_id"]
            comparison = conn.execute(
                "SELECT status FROM comparisons WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
            if comparison is None:
                raise ComparisonLifecycleError(
                    comparison_id, "absent", "unknown comparison"
                )
            if comparison["status"] != STATUS_DETECTING:
                raise DetectionStateError(
                    REASON_TRANSITION_INVALID,
                    f"the comparison is '{comparison['status']}', not "
                    "'detecting'; only an in-progress detection can be replayed",
                    comparison_id=comparison_id,
                    attempt_id=source_attempt_id,
                )
            if source["status"] != ATTEMPT_RUNNING:
                raise DetectionStateError(
                    REASON_ATTEMPT_NOT_RUNNING,
                    f"the detection attempt is '{source['status']}'; only a "
                    "running attempt can be replayed",
                    comparison_id=comparison_id,
                    attempt_id=source_attempt_id,
                )
            running = conn.execute(
                "SELECT attempt_id FROM comparison_detection_attempts "
                "WHERE comparison_id = ? AND status = ?",
                (comparison_id, ATTEMPT_RUNNING),
            ).fetchall()
            if len(running) != 1 or running[0]["attempt_id"] != source_attempt_id:
                raise DetectionStateError(
                    REASON_TRANSITION_INVALID,
                    "the attempt is not the comparison's only running attempt",
                    comparison_id=comparison_id,
                    attempt_id=source_attempt_id,
                )

            staleness = evaluate_staleness(
                source["started_at"], moment, stale_after_seconds
            )
            if not staleness["is_stale"]:
                raise DetectionStateError(
                    REASON_ATTEMPT_NOT_STALE,
                    "the attempt has not exceeded the configured stale "
                    "threshold; it may still be running and is not retired on "
                    "suspicion",
                    comparison_id=comparison_id,
                    attempt_id=source_attempt_id,
                )

            attempts_used = conn.execute(
                "SELECT COUNT(*) FROM comparison_detection_attempts "
                "WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()[0]
            if attempts_used >= max_attempts_per_comparison:
                raise DetectionStateError(
                    REASON_ATTEMPT_LIMIT_REACHED,
                    f"this comparison has used all {max_attempts_per_comparison} "
                    "permitted detection attempts; no further replay is allowed",
                    comparison_id=comparison_id,
                    attempt_id=source_attempt_id,
                )

            if (
                source["detector_version"] != detector_version
                or source["workflow_version"] != workflow_version
            ):
                raise DetectionStateError(
                    REASON_REPLAY_VERSION_CHANGED,
                    "the detector or workflow version changed since this "
                    "attempt started; create a new comparison under the current "
                    "workflow version instead of replaying this one",
                    comparison_id=comparison_id,
                    attempt_id=source_attempt_id,
                )
            if (
                source["previous_source_hash"] != previous_source_hash
                or source["current_source_hash"] != current_source_hash
            ):
                raise DetectionStateError(
                    REASON_REPLAY_INPUTS_CHANGED,
                    "the filing source content changed since this attempt "
                    "started; a replay would silently compare different inputs "
                    "under the same comparison",
                    comparison_id=comparison_id,
                    attempt_id=source_attempt_id,
                )

            next_number = conn.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 "
                "FROM comparison_detection_attempts WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()[0]
            replacement_id = detection_attempt_id_for(
                comparison_id,
                next_number,
                detector_version=detector_version,
                workflow_version=workflow_version,
                previous_source_hash=previous_source_hash,
                current_source_hash=current_source_hash,
            )

            # Retire the stale attempt.
            conn.execute(
                "UPDATE comparison_detection_attempts SET status = ?, "
                "finished_at = ?, failure_code = ?, failure_summary = ? "
                "WHERE attempt_id = ? AND status = ?",
                (
                    ATTEMPT_TIMED_OUT,
                    now_iso,
                    FAILURE_ATTEMPT_TIMED_OUT,
                    TIMED_OUT_SUMMARY[:MAX_FAILURE_SUMMARY_CHARS],
                    source_attempt_id,
                    ATTEMPT_RUNNING,
                ),
            )
            _insert_detection_event(
                conn,
                attempt_id=source_attempt_id,
                comparison_id=comparison_id,
                event_type=EVENT_DETECTION_TIMED_OUT,
                now=now_iso,
                failure_code=FAILURE_ATTEMPT_TIMED_OUT,
            )
            # Start the replacement.
            conn.execute(
                """
                INSERT INTO comparison_detection_attempts (
                    attempt_id, comparison_id, attempt_number, status,
                    detector_version, workflow_version,
                    previous_source_hash, current_source_hash,
                    started_at, finished_at, result_hash,
                    failure_code, failure_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
                """,
                (
                    replacement_id,
                    comparison_id,
                    next_number,
                    ATTEMPT_RUNNING,
                    detector_version,
                    workflow_version,
                    previous_source_hash,
                    current_source_hash,
                    now_iso,
                ),
            )
            _insert_detection_event(
                conn,
                attempt_id=replacement_id,
                comparison_id=comparison_id,
                event_type=EVENT_DETECTION_STARTED,
                now=now_iso,
            )
            replay_id = detection_replay_id_for(source_attempt_id, request_hash)
            conn.execute(
                """
                INSERT INTO comparison_detection_replays (
                    replay_id, comparison_id, source_attempt_id,
                    replacement_attempt_id, operator_id, actor_auth_method,
                    actor_token_id, actor_policy_id, actor_policy_version, reason_code,
                    operator_note, request_hash, policy_id, policy_version,
                    requested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    replay_id,
                    comparison_id,
                    source_attempt_id,
                    replacement_id,
                    operator_id,
                    attribution["actor_auth_method"],
                    attribution["actor_token_id"],
                    attribution["actor_policy_id"],
                    attribution["actor_policy_version"],
                    reason_code,
                    operator_note,
                    request_hash,
                    policy_id,
                    policy_version,
                    now_iso,
                ),
            )
            # The comparison STAYS 'detecting': one running attempt replaced
            # another, so the workflow is still mid-flight.
            conn.execute(
                "UPDATE comparisons SET updated_at = ? "
                "WHERE comparison_id = ? AND status = ?",
                (now_iso, comparison_id, STATUS_DETECTING),
            )
            row = conn.execute(
                "SELECT * FROM comparison_detection_replays WHERE replay_id = ?",
                (replay_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return dict(row), True


def get_detection_replay_for_source(
    source_attempt_id: str, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    """The replay that retired this attempt, or None (at most one exists)."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM comparison_detection_replays "
            "WHERE source_attempt_id = ?",
            (source_attempt_id,),
        ).fetchone()
    return dict(row) if row else None


def list_detection_replays(
    comparison_id: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Every replay for a comparison, oldest first."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM comparison_detection_replays WHERE comparison_id = ? "
            "ORDER BY requested_at, replay_id",
            (comparison_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def count_detection_attempts(
    comparison_id: str, db_path: str | Path | None = None
) -> int:
    """How many attempts this comparison has used, in any status."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM comparison_detection_attempts "
            "WHERE comparison_id = ?",
            (comparison_id,),
        ).fetchone()[0]


# --- Release-gated export artifacts ------------------------------------------


def _export_row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["export"] = json.loads(record.pop("export_payload_json"))
    return record


def record_export(
    *,
    export_id: str,
    export_schema_version: str,
    comparison_id: str,
    evaluation_id: str,
    review_id: str | None,
    release_basis: str,
    source_result_hash: str,
    final_result_hash: str,
    export_payload_json: str,
    export_payload_hash: str,
    db_path: str | Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Persist (or idempotently return) one release-gated export artifact.

    One BEGIN IMMEDIATE transaction covers the conditional insert and the
    read-back, so concurrent identical exports serialize: exactly one insert
    wins and every caller reads back the SAME stored row — including its
    original payload and created_at, which a replay must return unchanged.
    Detector results, governance evaluations, review items, and events are
    never touched. Returns (stored_export, created).
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    now = _utc_now_iso()
    with closing(_connect(db_path)) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                INSERT INTO comparison_exports (
                    export_id, export_schema_version, comparison_id,
                    evaluation_id, review_id, release_basis,
                    source_result_hash, final_result_hash,
                    export_payload_json, export_payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    export_id,
                    export_schema_version,
                    comparison_id,
                    evaluation_id,
                    review_id,
                    release_basis,
                    source_result_hash,
                    final_result_hash,
                    export_payload_json,
                    export_payload_hash,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            row = conn.execute(
                "SELECT * FROM comparison_exports WHERE export_id = ?",
                (export_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return _export_row_to_record(row), created


def get_export(
    export_id: str, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    """Fetch one stored export artifact by id, or None."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM comparison_exports WHERE export_id = ?",
            (export_id,),
        ).fetchone()
    return _export_row_to_record(row) if row else None


def list_exports(
    comparison_id: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Every stored export for a comparison, newest first. Full records —
    the API layer maps list responses onto a payload-free summary allowlist."""
    db_path = db_path or config.COMPARISON_DB_PATH
    init_db(db_path)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM comparison_exports WHERE comparison_id = ? "
            "ORDER BY created_at DESC, export_id",
            (comparison_id,),
        ).fetchall()
    return [_export_row_to_record(row) for row in rows]


# --- Read-only reliability snapshot ------------------------------------------
#
# The ONE read path the reliability service uses, so that service never issues
# SQL of its own across internal tables. Four properties are deliberate:
#
#   1. STRICTLY READ-ONLY, AND NEVER INITIALIZING. The connection is opened
#      with SQLite's `mode=ro` URI flag, so a write is refused by the driver
#      rather than merely avoided by convention, and `init_db` is NOT called:
#      reporting on a database must never create it, add a table to it, or run
#      a migration against it.
#   2. AN UNOBSERVABLE DATABASE IS NOT AN EMPTY ONE. A correctly initialized
#      database holding zero rows is a valid empty system and reads as empty.
#      A MISSING or UNREADABLE database, or one lacking a required reliability
#      source table, is refused — reporting it as "zero detecting comparisons,
#      zero failures, zero issues" would be a false clean signal produced at
#      exactly the moment the service cannot see workflow state at all. The
#      five cases are distinguished rather than collapsed:
#        A valid schema, zero rows      -> empty record sets
#        B database file missing        -> ReliabilityStorageUnavailable
#        C database unreadable/corrupt  -> ReliabilityStorageUnavailable
#        D required table missing       -> ReliabilitySchemaIncomplete
#        E contradictory stored records -> refused by the caller's validation
#      sqlite3 errors are therefore never caught broadly and turned into [].
#   3. ONE CONSISTENT SNAPSHOT. Every record set is read inside a single
#      deferred read transaction, so a concurrent write cannot tear the report
#      into halves that disagree with each other.
#   4. COLUMN ALLOWLIST. Each SELECT names its columns. result_json,
#      governed_result_json, reviewed_result_json, export_payload_json,
#      evidence, excerpts, reviewer notes, and operator notes are never
#      selected — replay actor identifiers, authentication linkage, and
#      operator_note are omitted here on purpose, so reliability output cannot
#      carry actor metadata or operator prose even by accident.

# Stable reasons for a database that cannot be observed at all.
RELIABILITY_STORAGE_ABSENT = "comparison_database_absent"
RELIABILITY_STORAGE_UNREADABLE = "comparison_database_unreadable"


class ReliabilityStorageUnavailable(Exception):
    """The comparison database cannot be opened for reading (API: safe 500).

    Missing or unreadable storage, NOT an empty system. ``reason`` is a stable
    code; ``detail`` names the configured path and the underlying fault for the
    SERVER LOG only — callers surface the stable code and a correlation id.
    """

    code = "reliability_storage_unavailable"

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class ReliabilitySchemaIncomplete(Exception):
    """The database opened, but a required reliability source table is absent.

    A structurally incompatible schema, not an empty system. Raised INSTEAD of
    returning empty record sets, and raised instead of creating the missing
    table: this read path never initializes or migrates anything.
    """

    def __init__(self, missing_tables: list[str], detail: str):
        super().__init__(detail)
        self.missing_tables = sorted(missing_tables)
        self.detail = detail


# Every table this snapshot actually consumes. Each must be present before any
# record set is reported; a database missing one cannot be summarized honestly.
RELIABILITY_REQUIRED_TABLES = (
    "comparisons",
    "comparison_detection_attempts",
    "comparison_detection_jobs",
    "comparison_detection_job_events",
    "comparison_detection_replays",
)

# Filing ids and captured source hashes are part of the allowlist because the
# PURE replay-eligibility calculation needs them (registry revalidation + the
# inputs-changed check). They are identifiers and digests already exposed by
# the public attempt/comparison DTOs — never content, evidence, or notes.
_RELIABILITY_COMPARISON_COLUMNS = (
    "comparison_id",
    "workflow_version",
    "previous_filing_id",
    "current_filing_id",
    "status",
    "created_at",
    "updated_at",
    "failure_code",
)
_RELIABILITY_ATTEMPT_COLUMNS = (
    "attempt_id",
    "comparison_id",
    "attempt_number",
    "status",
    "detector_version",
    "workflow_version",
    "previous_source_hash",
    "current_source_hash",
    "started_at",
    "finished_at",
    "result_hash",
    "failure_code",
    "failure_summary",
)
_RELIABILITY_REPLAY_COLUMNS = (
    "replay_id",
    "comparison_id",
    "source_attempt_id",
    "replacement_attempt_id",
    "reason_code",
    "policy_id",
    "policy_version",
    "requested_at",
)
_RELIABILITY_JOB_COLUMNS = (
    "job_id",
    "comparison_id",
    "attempt_id",
    "trigger_type",
    "status",
    "detector_version",
    "workflow_version",
    "queued_at",
    "claimed_at",
    "finished_at",
    "worker_id",
    "claim_generation",
    "lease_started_at",
    "heartbeat_at",
    "lease_expires_at",
    "result_hash",
    "failure_code",
)
_RELIABILITY_JOB_EVENT_COLUMNS = (
    "event_id",
    "job_id",
    "comparison_id",
    "attempt_id",
    "event_type",
    "event_seq",
    "created_at",
    "worker_id",
    "claim_generation",
    "source_attempt_id",
    "replacement_attempt_id",
    "lease_expires_at",
    "result_hash",
    "failure_code",
)


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open an existing database read-only. Writes are refused by the driver."""
    conn = sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro", uri=True, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _missing_reliability_tables(conn: sqlite3.Connection) -> list[str]:
    """Required tables absent from this database's own schema catalogue."""
    present = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    return [name for name in RELIABILITY_REQUIRED_TABLES if name not in present]


def read_reliability_snapshot(db_path: str | Path | None = None) -> dict[str, Any]:
    """One read-only snapshot of every record the reliability report needs.

    Returns ``{comparisons, attempts, jobs, replays,
    attempts_per_comparison}`` with allowlisted columns only and deterministic
    ordering, for a database that is readable and carries every required table
    — including when it holds zero rows, which is a valid empty system.

    Creates nothing, initializes nothing, migrates nothing, writes nothing.

    Raises ReliabilityStorageUnavailable when the file is missing or cannot be
    read, and ReliabilitySchemaIncomplete when a required table is absent.
    Neither is reported as an empty result: a database the service cannot
    observe must not be indistinguishable from one with nothing in it.
    """
    path = Path(db_path or config.COMPARISON_DB_PATH)
    if not path.exists():
        raise ReliabilityStorageUnavailable(
            RELIABILITY_STORAGE_ABSENT,
            f"comparison database does not exist: {path}. It is NOT created by "
            "a reliability read.",
        )

    try:
        conn = _connect_readonly(path)
    except (sqlite3.Error, OSError) as exc:
        raise ReliabilityStorageUnavailable(
            RELIABILITY_STORAGE_UNREADABLE,
            f"comparison database at {path} could not be opened read-only: {exc!r}",
        ) from exc

    try:
        with closing(conn):
            conn.execute("BEGIN DEFERRED")
            try:
                missing = _missing_reliability_tables(conn)
                if missing:
                    raise ReliabilitySchemaIncomplete(
                        missing,
                        f"comparison database at {path} is missing required "
                        f"reliability tables {missing}. They are NOT created by "
                        "a reliability read.",
                    )
                comparisons = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT "
                        + ", ".join(_RELIABILITY_COMPARISON_COLUMNS)
                        + " FROM comparisons ORDER BY created_at, comparison_id"
                    ).fetchall()
                ]
                attempts = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT "
                        + ", ".join(_RELIABILITY_ATTEMPT_COLUMNS)
                        + " FROM comparison_detection_attempts "
                        "ORDER BY started_at, comparison_id, attempt_number"
                    ).fetchall()
                ]
                counts = {
                    row["comparison_id"]: row["attempt_count"]
                    for row in conn.execute(
                        "SELECT comparison_id, COUNT(*) AS attempt_count "
                        "FROM comparison_detection_attempts "
                        "GROUP BY comparison_id ORDER BY comparison_id"
                    ).fetchall()
                }
                jobs = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT "
                        + ", ".join(_RELIABILITY_JOB_COLUMNS)
                        + " FROM comparison_detection_jobs "
                        "ORDER BY queued_at, job_id"
                    ).fetchall()
                ]
                job_events = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT "
                        + ", ".join(_RELIABILITY_JOB_EVENT_COLUMNS)
                        + " FROM comparison_detection_job_events "
                        "ORDER BY created_at, job_id, event_seq, event_id"
                    ).fetchall()
                ]
                replays = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT "
                        + ", ".join(_RELIABILITY_REPLAY_COLUMNS)
                        + " FROM comparison_detection_replays "
                        "ORDER BY requested_at, replay_id"
                    ).fetchall()
                ]
            finally:
                # A read transaction is ended, never committed as a write. The
                # rollback is itself guarded: on a corrupt file even this can
                # fail, and that must surface as unavailable storage, not as a
                # bare sqlite3 error escaping the store.
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
    except ReliabilitySchemaIncomplete:
        raise
    except (sqlite3.Error, OSError) as exc:
        # Corrupt file, truncated page, revoked permission mid-read. Narrow on
        # purpose: sqlite3 failures become an explicit unavailable-storage
        # error, never an empty snapshot.
        raise ReliabilityStorageUnavailable(
            RELIABILITY_STORAGE_UNREADABLE,
            f"comparison database at {path} could not be read: {exc!r}",
        ) from exc

    return {
        "comparisons": comparisons,
        "attempts": attempts,
        "jobs": jobs,
        "job_events": job_events,
        "replays": replays,
        "attempts_per_comparison": counts,
    }
