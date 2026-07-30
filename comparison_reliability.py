"""Read-only reliability visibility over the persisted comparison lifecycle.

Stage 3.5 reliability step 3. Steps 1 and 2 made detection durable and made an
interrupted detection resolvable by an explicit operator replay. Both are
inspectable one record at a time — if you already know the id. This module
answers the operational questions that need aggregation instead: how many
comparisons are mid-flight, how many running attempts are stale, which ones
need attention, how attempts and replays have actually turned out, and which
detector/workflow versions produced those records.

What this module does NOT do, stated plainly
--------------------------------------------
It EXPOSES state. It never repairs, retries, replays, deletes, or mutates a
workflow record, and it adds no automatic retry, background worker, scheduler,
polling loop, alert, notification, or external monitoring integration — there
are still none of those anywhere in this repository. A stale attempt reported
here stays exactly as stale as it was: resolving it means an operator
explicitly POSTing to the existing replay endpoint.

TRANSITIVELY READ-ONLY, by construction: the ONLY SQLite access on any
reliability path is ``comparison_store.read_reliability_snapshot``, which opens
the database with SQLite's ``mode=ro`` flag, so a write is refused by the
driver rather than merely avoided by convention. Replay eligibility is
evaluated by ``detection_recovery.build_recovery_view_from_records`` — a PURE
calculation over records this module already loaded — so no reliability API or
CLI request can reach ``init_db``, a store getter, ``recovery_view``, a table
or index creation, or a migration, even transitively. Filing-registry reads
stay external JSONL file reads that never create or rewrite the registry. An
AST guard in the test suite pins all of this.

There is no LLM, Bedrock call, embedding, vector retrieval, network access, or
credential requirement in this path — it reads local SQLite rows and the
checked-in recovery policy.

Time windows
------------
``since`` / ``until`` are optional, timezone-aware, and INCLUSIVE
(``since <= t <= until``). A naive timestamp is rejected rather than assumed to
be UTC, and ``until`` must not precede ``since``. Historical attempt metrics
are windowed on ``started_at``; historical replay metrics on ``requested_at``
(which is the same instant as its replacement attempt's ``started_at`` — both
are written by the one replay transaction).

CURRENT-STATE GAUGES AND THE ENTIRE ISSUE SET ARE NOT WINDOWED. They are
evaluated at query time, because "how many attempts are stuck right now" is not
a question about a historical interval. That is also why
``unresolved_operational_issues`` always equals the total number of generated
issue records: the two can never disagree by construction.

Denominators
------------
Rates carry their numerator and denominator inline, and a zero denominator is
reported as ``value: null`` with ``zero_denominator: true`` — never NaN, never
silently 0.0, never omitted (same convention as the comparison regression
runner). RUNNING WORK NEVER ENTERS A TERMINAL DENOMINATOR: a still-running
attempt is not a failure, and neither is a still-running replay replacement.

Fail-closed storage handling
---------------------------
An UNOBSERVABLE store is not an empty one. A correctly initialized comparison
database holding zero rows is a valid empty system and produces the explicit
empty report (zero counters, null zero-denominator rates). A missing or
unreadable database raises ``ReliabilityStorageUnavailable``
(``reliability_storage_unavailable``), and one lacking a required reliability
source table raises ``ReliabilityDataError`` naming the absent table — because
reporting either as "zero detecting comparisons, zero failures, zero issues"
would be a false clean signal produced exactly when workflow state cannot be
seen at all. The read path never creates the database, adds a table, or runs a
migration; a refusal leaves the filesystem untouched.

Fail-closed data validation
---------------------------
Metrics are computed from stored rows, so internally inconsistent rows would
otherwise produce clean-looking numbers that are false. This module refuses:
a terminal attempt without ``finished_at``, a running attempt with one, an
attempt status outside the store's vocabulary, a comparison status outside it,
a replay whose source or replacement attempt is missing, and any stored
timestamp that is not parseable as timezone-aware UTC. Those raise
``ReliabilityDataError`` (stable code ``reliability_data_invalid``) rather than
returning partial metrics. A NEGATIVE duration is the one deliberate
exception: it is excluded from duration statistics and surfaced as an
``invalid_negative_duration`` issue, because a single skewed clock should not
suppress the whole report.

Fail-closed dependency handling
-------------------------------
Replay eligibility is a statement about FILING-REGISTRY truth, so it is the one
metric here with an external dependency. When at least one attempt needs a
recovery evaluation and the registry is absent, unreadable, malformed, or
empty, the report is refused with ``ReliabilityDependencyUnavailable`` (stable
code ``reliability_dependency_unavailable``). An unanswerable question is never
converted into ``replay_eligible_attempts: 0``, an empty issue list, or a
``no_replay_available`` action code: that would tell an operator nothing needs
action at exactly the moment the system cannot tell.

When NOTHING needs a recovery evaluation — no stale running attempt — the count
is an exact zero and the registry is not read at all. The failures listing never
requires eligibility, so it never depends on the registry.

Detection transition events are deliberately NOT consumed here: no metric in
this contract needs them, so this module does not read the event table at all.

Structured logging
------------------
This module also owns the allowlisted lifecycle log payload used by
``comparison_detector``, ``detection_recovery``, ``comparison_review``, and the
API's comparison-create, governance, and export mutation boundaries. Fields
travel through stdlib ``logging`` via ``extra`` — THEY ARE NOT JSON. No JSON
formatter is configured anywhere in this repository, so these are structured
*fields* carried on the LogRecord, and what a handler renders is whatever that
handler's formatter does.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import comparison_store
import config

logger = logging.getLogger("comparison_reliability")

RELIABILITY_CONTRACT_VERSION = "comparison_reliability.v1"

# A rate whose denominator is zero asserts nothing. It is reported as null with
# its numerator and denominator visible — never NaN, never 0.0, never dropped.
ZERO_DENOMINATOR_POLICY = "null_value_no_rate_asserted"

# One deterministic percentile definition for the whole module (nearest-rank):
# sort ascending, rank = ceil(p * n), take index rank - 1. For n = 1 every
# percentile is the sole value. No NumPy, no interpolation, no dependency.
PERCENTILE_METHOD = "nearest_rank"

# Comparison statuses this report maps onto gauges. Tied to the store's
# vocabulary: a status outside this tuple fails the report closed rather than
# vanishing from every count.
_GAUGED_COMPARISON_STATUSES = (
    comparison_store.STATUS_READY_FOR_DETECTION,
    comparison_store.STATUS_DETECTING,
    comparison_store.STATUS_DETECTED,
    comparison_store.STATUS_FAILED,
)

# --- Operational issue vocabulary --------------------------------------------

ISSUE_STALE_RUNNING_ATTEMPT = "stale_running_attempt"
ISSUE_ATTEMPT_LIMIT_EXHAUSTED = "attempt_limit_exhausted"
ISSUE_COMPARISON_FAILED = "comparison_failed"
ISSUE_REPLACEMENT_ATTEMPT_FAILED = "replacement_attempt_failed"
ISSUE_INVALID_NEGATIVE_DURATION = "invalid_negative_duration"

ISSUE_TYPES = (
    ISSUE_STALE_RUNNING_ATTEMPT,
    ISSUE_ATTEMPT_LIMIT_EXHAUSTED,
    ISSUE_COMPARISON_FAILED,
    ISSUE_REPLACEMENT_ATTEMPT_FAILED,
    ISSUE_INVALID_NEGATIVE_DURATION,
)

ACTION_INSPECT_AND_REPLAY = "inspect_and_replay_if_valid"
ACTION_NEW_WORKFLOW_VERSION = "create_new_workflow_version"
ACTION_INSPECT_FAILURE = "inspect_failure"
ACTION_NO_REPLAY_AVAILABLE = "no_replay_available"
ACTION_INSPECT_CLOCK = "inspect_clock_integrity"

RECOMMENDED_ACTION_CODES = (
    ACTION_INSPECT_AND_REPLAY,
    ACTION_NEW_WORKFLOW_VERSION,
    ACTION_INSPECT_FAILURE,
    ACTION_NO_REPLAY_AVAILABLE,
    ACTION_INSPECT_CLOCK,
)

# Triage order. A skewed clock comes first because it makes the duration
# numbers themselves untrustworthy; an exhausted comparison outranks a merely
# stale one because no replay can rescue it; a terminal failure comes last
# because it is diagnosable at leisure rather than blocking a workflow.
_ISSUE_SEVERITY = {
    ISSUE_INVALID_NEGATIVE_DURATION: 0,
    ISSUE_ATTEMPT_LIMIT_EXHAUSTED: 1,
    ISSUE_STALE_RUNNING_ATTEMPT: 2,
    ISSUE_REPLACEMENT_ATTEMPT_FAILED: 3,
    ISSUE_COMPARISON_FAILED: 4,
}

# The complete issue field allowlist. No evidence, no result JSON, no filing
# content, no reviewer or operator notes, no filesystem paths, no SQL, no raw
# exception text — and no free-form prose at all: the only guidance an issue
# carries is a stable recommended_action_code.
ISSUE_FIELDS = (
    "issue_type",
    "comparison_id",
    "attempt_id",
    "replay_id",
    "status",
    "failure_code",
    "started_at",
    "created_at",
    "detected_at",
    "age_seconds",
    "stale_at",
    "attempts_used",
    "max_attempts",
    "detector_version",
    "workflow_version",
    "recommended_action_code",
)

# Failure listing allowlist: attempt summaries, never results or exceptions.
# failure_summary is the store's bounded, code-derived string (it is never an
# exception message, path, or SQL fragment — see fail_detection_attempt).
FAILURE_FIELDS = (
    "attempt_id",
    "comparison_id",
    "attempt_number",
    "status",
    "failure_code",
    "failure_summary",
    "detector_version",
    "workflow_version",
    "started_at",
    "finished_at",
    "duration_seconds",
    "replay_id",
    "source_attempt_id",
)

# Conservative bound on any listing. A reliability report is a triage surface,
# not a bulk export, and there is no pagination framework in this API.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

# --- Stable error codes -------------------------------------------------------

CODE_INVALID_TIMESTAMP = "invalid_timestamp"
CODE_INVALID_TIME_RANGE = "invalid_time_range"
CODE_INVALID_LIMIT = "invalid_limit"
CODE_INVALID_ISSUE_TYPE = "invalid_issue_type"
CODE_DATA_INVALID = "reliability_data_invalid"
CODE_DEPENDENCY_UNAVAILABLE = "reliability_dependency_unavailable"

# Missing or unreadable comparison storage. Deliberately DISTINCT from
# reliability_data_invalid (a readable store whose records or schema are wrong)
# and from reliability_dependency_unavailable (the store is fine; the filing
# registry cannot answer). An operator needs to tell those three apart.
ReliabilityStorageUnavailable = comparison_store.ReliabilityStorageUnavailable
CODE_STORAGE_UNAVAILABLE = ReliabilityStorageUnavailable.code

# Stable sub-codes naming WHICH structural invariant a stored row broke. They
# are logged and reported as codes; the offending ids stay in the server log.
DATA_TERMINAL_MISSING_FINISH = "terminal_attempt_missing_finished_at"
DATA_RUNNING_HAS_FINISH = "running_attempt_has_finished_at"
DATA_UNKNOWN_ATTEMPT_STATUS = "unknown_attempt_status"
DATA_UNKNOWN_COMPARISON_STATUS = "unknown_comparison_status"
DATA_REPLAY_MISSING_REPLACEMENT = "replay_missing_replacement_attempt"
DATA_REPLAY_MISSING_SOURCE = "replay_missing_source_attempt"
DATA_UNPARSABLE_TIMESTAMP = "unparsable_timestamp"


def data_missing_table_reason(table: str) -> str:
    """Stable sub-code for a required reliability source table being absent."""
    return f"missing_required_table_{table}"

# The one external dependency any reliability metric needs: replay eligibility
# is a statement about registry truth, so it cannot be computed without the
# filing registry.
DEPENDENCY_FILING_REGISTRY = "filing_registry"

# Stable sub-codes naming HOW the dependency was unavailable.
DEPENDENCY_REGISTRY_ABSENT = "filing_registry_absent"
DEPENDENCY_REGISTRY_UNREADABLE = "filing_registry_unreadable"
DEPENDENCY_REGISTRY_EMPTY = "filing_registry_empty"
DEPENDENCY_RESOLUTION_FAILED = "filing_registry_resolution_failed"


class ReliabilityQueryError(Exception):
    """Invalid reliability query input (API: 422).

    ``code`` is stable and ``message`` is safe to display — no paths, SQL, or
    stored content.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ReliabilityDataError(Exception):
    """Stored workflow records are internally inconsistent (fail closed).

    Raised instead of returning metrics computed over contradictory rows.
    ``reasons`` is a sorted list of stable sub-codes; ``detail`` names the
    offending ids for the SERVER LOG only — the API surfaces the stable code
    and a correlation id, never this text.
    """

    code = CODE_DATA_INVALID

    def __init__(self, reasons: list[str], detail: str):
        super().__init__(detail)
        self.reasons = sorted(set(reasons))
        self.detail = detail


class ReliabilityDependencyUnavailable(Exception):
    """A dependency a requested metric needs cannot answer (fail closed).

    Raised INSTEAD of reporting a metric that would look clean and be false.
    Replay eligibility is a statement about filing-registry truth: when the
    registry is absent, unreadable, malformed, or empty, "no attempt is
    replay-eligible" is not a measurement — it is an unanswered question, and
    reporting it as zero would tell an operator that nothing needs action at
    exactly the moment the system cannot tell.

    ``dependency`` and ``reason`` are stable codes. ``detail`` may name the
    configured path and the underlying fault for the SERVER LOG only — the API
    surfaces the stable code plus a correlation id, and the CLI surfaces the
    codes, never this text.
    """

    code = CODE_DEPENDENCY_UNAVAILABLE

    def __init__(self, dependency: str, reason: str, detail: str):
        super().__init__(detail)
        self.dependency = dependency
        self.reason = reason
        self.detail = detail


# --- Structured lifecycle logging --------------------------------------------

EVENT_ATTEMPT_STARTED = "detection_attempt_started"
EVENT_ATTEMPT_SUCCEEDED = "detection_attempt_succeeded"
EVENT_ATTEMPT_FAILED = "detection_attempt_failed"
EVENT_ATTEMPT_TIMED_OUT = "detection_attempt_timed_out"
EVENT_REPLAY_CREATED = "detection_replay_created"
EVENT_REPLAY_COMPLETED = "detection_replay_completed"
EVENT_REVIEW_DECIDED = "comparison_review_decided"
EVENT_COMPARISON_CREATED = "comparison_created"
EVENT_GOVERNANCE_EVALUATED = "comparison_governance_evaluated"
EVENT_EXPORT_CREATED = "comparison_export_created"

# Backward-compatible detection/replay registry consumed by the existing
# reliability contract tests. Review decisions use their own closed registry;
# ALL_LOG_EVENTS is the complete lifecycle logger vocabulary.
LOG_EVENTS = (
    EVENT_ATTEMPT_STARTED,
    EVENT_ATTEMPT_SUCCEEDED,
    EVENT_ATTEMPT_FAILED,
    EVENT_ATTEMPT_TIMED_OUT,
    EVENT_REPLAY_CREATED,
    EVENT_REPLAY_COMPLETED,
)
REVIEW_LOG_EVENTS = (EVENT_REVIEW_DECIDED,)
AUTHENTICATED_MUTATION_LOG_EVENTS = (
    EVENT_COMPARISON_CREATED,
    EVENT_GOVERNANCE_EVALUATED,
    EVENT_EXPORT_CREATED,
)
ALL_LOG_EVENTS = (
    LOG_EVENTS + REVIEW_LOG_EVENTS + AUTHENTICATED_MUTATION_LOG_EVENTS
)

# The complete structured-log field allowlist. Actor fields contain only
# Principal-derived subject/auth method/token jti and the exact permission
# checked by the route. operator_id, reviewer_id, notes, reason codes, bearer
# tokens, claims, evidence, excerpts, document text, source paths, SQL,
# environment values, credentials, and exception text are absent by
# construction: this builder cannot emit a key that is not listed here.
LOG_FIELDS = (
    "event",
    "comparison_id",
    "attempt_id",
    "attempt_number",
    "replay_id",
    "source_attempt_id",
    "review_id",
    "review_event_id",
    "review_action",
    "evaluation_id",
    "export_id",
    "detector_version",
    "workflow_version",
    "status",
    "failure_code",
    "result_hash",
    "elapsed_ms",
    "actor_subject",
    "actor_auth_method",
    "actor_token_id",
    "required_permission",
)


def elapsed_ms_for(attempt: dict[str, Any] | None) -> int | None:
    """Execution time in whole milliseconds, derived from persisted timestamps.

    Returns None when the attempt has not finished, when either timestamp is
    unusable, or when the result would be negative — a skewed clock is reported
    as "unknown", never as a nonsense duration.
    """
    if not attempt or not attempt.get("finished_at") or not attempt.get("started_at"):
        return None
    try:
        started = comparison_store.parse_utc_timestamp(attempt["started_at"])
        finished = comparison_store.parse_utc_timestamp(attempt["finished_at"])
    except ValueError:
        return None
    elapsed = (finished - started).total_seconds()
    return None if elapsed < 0 else int(round(elapsed * 1000))


def log_lifecycle_event(
    event: str,
    *,
    attempt: dict[str, Any] | None = None,
    comparison_id: str | None = None,
    replay_id: str | None = None,
    source_attempt_id: str | None = None,
    review_id: str | None = None,
    review_event_id: str | None = None,
    review_action: str | None = None,
    evaluation_id: str | None = None,
    export_id: str | None = None,
    status: str | None = None,
    failure_code: str | None = None,
    result_hash: str | None = None,
    elapsed_ms: int | None = None,
    actor_context: Mapping[str, Any] | None = None,
) -> None:
    """Emit one allowlisted lifecycle record through stdlib logging.

    Call this AFTER the transaction it describes has committed: logging a
    transition that then rolled back would be a lie, and a logging fault must
    never abort a workflow transaction. Every failure here is swallowed for the
    same reason — observability is not allowed to break the thing it observes.

    Fields ride on the LogRecord through ``extra``. They are NOT JSON; no JSON
    formatter is configured in this repository.
    """
    try:
        if event not in ALL_LOG_EVENTS:  # pragma: no cover - guarded by callers
            return
        attempt = attempt or {}
        actor = _safe_actor_log_context(actor_context)
        payload = {
            "event": event,
            "comparison_id": comparison_id or attempt.get("comparison_id"),
            "attempt_id": attempt.get("attempt_id"),
            "attempt_number": attempt.get("attempt_number"),
            "replay_id": replay_id,
            "source_attempt_id": source_attempt_id,
            "review_id": review_id,
            "review_event_id": review_event_id,
            "review_action": review_action,
            "evaluation_id": evaluation_id,
            "export_id": export_id,
            "detector_version": attempt.get("detector_version"),
            "workflow_version": attempt.get("workflow_version"),
            "status": status if status is not None else attempt.get("status"),
            "failure_code": (
                failure_code
                if failure_code is not None
                else attempt.get("failure_code")
            ),
            "result_hash": (
                result_hash if result_hash is not None else attempt.get("result_hash")
            ),
            "elapsed_ms": elapsed_ms,
            **actor,
        }
        logger.info(
            "%s comparison=%s attempt=%s status=%s",
            payload["event"],
            payload["comparison_id"],
            payload["attempt_id"],
            payload["status"],
            extra={key: payload[key] for key in LOG_FIELDS},
        )
    except Exception:  # pragma: no cover - defensive
        # Deliberately silent: a logging failure must not affect the committed
        # workflow state or the caller's control flow.
        pass


def _safe_actor_log_context(
    actor_context: Mapping[str, Any] | None,
) -> dict[str, str | None]:
    """Return only complete, bounded Principal-derived actor log fields.

    A malformed mapping is treated as absent rather than stringified. Logging
    is observability, not an authorization decision, and must neither leak
    arbitrary values nor interrupt committed workflow state.
    """
    empty = {
        "actor_subject": None,
        "actor_auth_method": None,
        "actor_token_id": None,
        "required_permission": None,
    }
    if not isinstance(actor_context, Mapping):
        return empty
    if (
        set(actor_context)
        != {
            "actor_subject",
            "actor_auth_method",
            "actor_token_id",
            "required_permission",
        }
        or actor_context.get("actor_auth_method")
        != "local_hs256"
    ):
        return empty
    bounds = {
        "actor_subject": 120,
        "actor_auth_method": 32,
        "actor_token_id": 128,
        "required_permission": 120,
    }
    safe: dict[str, str | None] = {}
    for field, maximum in bounds.items():
        value = actor_context.get(field)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > maximum
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
        ):
            return empty
        safe[field] = value
    return safe


# --- Window parsing -----------------------------------------------------------


def parse_window(
    since: str | datetime | None, until: str | datetime | None
) -> tuple[datetime | None, datetime | None]:
    """Validate an optional inclusive UTC window.

    Naive timestamps are REJECTED rather than assumed to be UTC: guessing a
    timezone would quietly shift which records a report covers. An omitted
    bound means unbounded, and ``until`` must not precede ``since``.
    """
    parsed_since = _window_bound(since, "since")
    parsed_until = _window_bound(until, "until")
    if parsed_since and parsed_until and parsed_until < parsed_since:
        raise ReliabilityQueryError(
            CODE_INVALID_TIME_RANGE,
            "until must be greater than or equal to since",
        )
    return parsed_since, parsed_until


def _window_bound(value: str | datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ReliabilityQueryError(
                CODE_INVALID_TIMESTAMP,
                f"{field} must be timezone-aware; a naive timestamp is never "
                "assumed to be UTC",
            )
        return value.astimezone(timezone.utc)
    try:
        return comparison_store.parse_utc_timestamp(value, field=field)
    except ValueError as exc:
        raise ReliabilityQueryError(CODE_INVALID_TIMESTAMP, str(exc)) from exc


def _in_window(
    moment: datetime, since: datetime | None, until: datetime | None
) -> bool:
    """Inclusive on both bounds."""
    if since is not None and moment < since:
        return False
    if until is not None and moment > until:
        return False
    return True


def validate_limit(limit: int | None) -> int:
    """Bound a listing size. Public so the CLI validates identically."""
    if limit is None:
        return DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ReliabilityQueryError(CODE_INVALID_LIMIT, "limit must be an integer")
    if limit < 1 or limit > MAX_LIMIT:
        raise ReliabilityQueryError(
            CODE_INVALID_LIMIT, f"limit must be between 1 and {MAX_LIMIT}"
        )
    return limit


def _validated_issue_type(issue_type: str | None) -> str | None:
    if issue_type is None:
        return None
    if issue_type not in ISSUE_TYPES:
        raise ReliabilityQueryError(
            CODE_INVALID_ISSUE_TYPE,
            f"issue_type must be one of {list(ISSUE_TYPES)}",
        )
    return issue_type


# --- Percentiles and rates ----------------------------------------------------


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile: rank = ceil(p * n), value at index rank - 1.

    One definition, used everywhere, documented in the module docstring and the
    README. Deterministic for every sample size (n = 1 returns the sole value
    for every fraction) and stdlib-only — no NumPy, no interpolation.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _rate(numerator: int, denominator: int, name: str) -> dict[str, Any]:
    """A rate with its denominator visible and zero-denominator made explicit."""
    if denominator == 0:
        return {
            "metric": name,
            "value": None,
            "numerator": numerator,
            "denominator": 0,
            "zero_denominator": True,
            "zero_denominator_policy": ZERO_DENOMINATOR_POLICY,
        }
    return {
        "metric": name,
        "value": round(numerator / denominator, 6),
        "numerator": numerator,
        "denominator": denominator,
        "zero_denominator": False,
    }


# --- Loading and validating the snapshot -------------------------------------


def _recovery_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    """The recovery policy this report interprets staleness and limits with.

    Imported lazily and ON PURPOSE: ``detection_recovery`` imports
    ``comparison_detector``, which imports this module for lifecycle logging.
    A module-level import here would close that cycle. The logging half of this
    module therefore keeps no heavy imports, and the reporting half resolves
    the policy at call time.
    """
    if policy is not None:
        return policy
    import detection_recovery

    return detection_recovery.POLICY


def _parsed(value: Any, field: str, owner: str, problems: list[tuple[str, str]]):
    """Parse a stored timestamp, recording a structural problem on failure."""
    try:
        return comparison_store.parse_utc_timestamp(value, field=field)
    except ValueError:
        problems.append((DATA_UNPARSABLE_TIMESTAMP, f"{owner}.{field}"))
        return None


def _load(db_path: str | Path | None) -> dict[str, Any]:
    """Read the snapshot and refuse it if the stored rows contradict themselves.

    Fail-closed: partial metrics over inconsistent records would look clean and
    be wrong, so every structural problem is collected and raised together.

    Storage that cannot be observed is refused before any of that. A correctly
    initialized database holding zero rows loads as empty; a missing or
    unreadable one raises ReliabilityStorageUnavailable, and one lacking a
    required table becomes a ReliabilityDataInvalid refusal naming which table
    is absent — never an empty result set, because "nothing is wrong" and
    "nothing can be seen" must not look the same to an operator.
    """
    try:
        snapshot = comparison_store.read_reliability_snapshot(
            db_path or config.COMPARISON_DB_PATH
        )
    except comparison_store.ReliabilitySchemaIncomplete as exc:
        # A structurally incompatible schema is a data-integrity refusal, which
        # keeps the public vocabulary at two storage-side codes: unavailable
        # (cannot read) versus invalid (read, but not usable).
        raise ReliabilityDataError(
            [data_missing_table_reason(table) for table in exc.missing_tables],
            exc.detail,
        ) from exc
    problems: list[tuple[str, str]] = []
    attempt_ids = {attempt["attempt_id"] for attempt in snapshot["attempts"]}

    for comparison in snapshot["comparisons"]:
        if comparison["status"] not in _GAUGED_COMPARISON_STATUSES:
            problems.append(
                (DATA_UNKNOWN_COMPARISON_STATUS, comparison["comparison_id"])
            )
        comparison["created_dt"] = _parsed(
            comparison["created_at"], "created_at", comparison["comparison_id"], problems
        )
        comparison["updated_dt"] = _parsed(
            comparison["updated_at"], "updated_at", comparison["comparison_id"], problems
        )

    for attempt in snapshot["attempts"]:
        status, attempt_id = attempt["status"], attempt["attempt_id"]
        if status not in comparison_store.ATTEMPT_STATUSES:
            problems.append((DATA_UNKNOWN_ATTEMPT_STATUS, attempt_id))
        elif status == comparison_store.ATTEMPT_RUNNING:
            if attempt["finished_at"] is not None:
                problems.append((DATA_RUNNING_HAS_FINISH, attempt_id))
        elif attempt["finished_at"] is None:
            problems.append((DATA_TERMINAL_MISSING_FINISH, attempt_id))
        attempt["started_dt"] = _parsed(
            attempt["started_at"], "started_at", attempt_id, problems
        )
        attempt["finished_dt"] = (
            _parsed(attempt["finished_at"], "finished_at", attempt_id, problems)
            if attempt["finished_at"] is not None
            else None
        )

    for replay in snapshot["replays"]:
        if replay["replacement_attempt_id"] not in attempt_ids:
            problems.append((DATA_REPLAY_MISSING_REPLACEMENT, replay["replay_id"]))
        if replay["source_attempt_id"] not in attempt_ids:
            problems.append((DATA_REPLAY_MISSING_SOURCE, replay["replay_id"]))
        replay["requested_dt"] = _parsed(
            replay["requested_at"], "requested_at", replay["replay_id"], problems
        )

    if problems:
        raise ReliabilityDataError(
            [code for code, _ in problems],
            "; ".join(f"{code}: {owner}" for code, owner in sorted(problems)),
        )

    snapshot["attempts_by_id"] = {
        attempt["attempt_id"]: attempt for attempt in snapshot["attempts"]
    }
    snapshot["comparisons_by_id"] = {
        comparison["comparison_id"]: comparison
        for comparison in snapshot["comparisons"]
    }
    # At most one replay per source attempt (UNIQUE in storage), so this index
    # is what lets the pure eligibility calculation see "already replayed"
    # without any further store read.
    snapshot["replays_by_source"] = {
        replay["source_attempt_id"]: replay for replay in snapshot["replays"]
    }
    return snapshot


def _now(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ReliabilityQueryError(
            CODE_INVALID_TIMESTAMP, "now must be timezone-aware"
        )
    return moment.astimezone(timezone.utc)


# --- Current-state evaluation -------------------------------------------------


def _running_attempts(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        attempt
        for attempt in snapshot["attempts"]
        if attempt["status"] == comparison_store.ATTEMPT_RUNNING
    ]


def _staleness(attempt: dict[str, Any], moment: datetime, policy: dict[str, Any]):
    """Staleness through the store's own pure helper, so this report and
    ``GET /api/detection-attempts/{id}/recovery`` cannot disagree."""
    return comparison_store.evaluate_staleness(
        attempt["started_at"], moment, policy["stale_after_seconds"]
    )


def _require_filing_registry(registry_path: str | Path | None) -> None:
    """Prove the filing registry can answer, BEFORE any eligibility is reported.

    Called only when at least one attempt actually needs a recovery evaluation,
    so a report with nothing to evaluate never touches the registry at all.

    All four refused conditions are indistinguishable from "no filing matches"
    once resolution has run — ``filing_registry`` returns ``[]`` for an absent
    file, and an empty result then surfaces as an ordinary pair-validation
    failure. That is precisely why the check happens here instead: an
    unanswerable dependency must not be reportable as a clean zero.

    An empty-but-present registry is refused for the same reason as an absent
    one: a comparison cannot be created without registry entries, so zero
    entries beside a live attempt means the registry lost the data the metric
    depends on.
    """
    import filing_registry

    path = Path(registry_path or config.FILING_REGISTRY_PATH)
    if not path.exists():
        raise ReliabilityDependencyUnavailable(
            DEPENDENCY_FILING_REGISTRY,
            DEPENDENCY_REGISTRY_ABSENT,
            f"filing registry file does not exist: {path}",
        )
    try:
        entries = filing_registry.list_entries(path)
    except Exception as exc:
        raise ReliabilityDependencyUnavailable(
            DEPENDENCY_FILING_REGISTRY,
            DEPENDENCY_REGISTRY_UNREADABLE,
            f"filing registry at {path} could not be read: {exc!r}",
        ) from exc
    if not entries:
        raise ReliabilityDependencyUnavailable(
            DEPENDENCY_FILING_REGISTRY,
            DEPENDENCY_REGISTRY_EMPTY,
            f"filing registry at {path} contains no outcome records",
        )


def _registry_source_hashes(
    comparison: dict[str, Any], registry_path: str | Path | None
) -> tuple[str, str]:
    """Registry truth for the pair, resolved WITHOUT any SQLite access.

    The same steps ``resolve_detection_inputs`` performs against the registry —
    pair validation, then both parsed entries' source hashes — minus its
    comparison-row lookup, because this caller already holds the comparison
    from the read-only snapshot. Everything here is a JSONL file read; the
    registry is never created or rewritten.
    """
    import filing_registry

    registry = registry_path or config.FILING_REGISTRY_PATH
    comparison_store.validate_pair(
        comparison["previous_filing_id"], comparison["current_filing_id"], registry
    )
    previous = filing_registry.get_filing(comparison["previous_filing_id"], registry)
    current = filing_registry.get_filing(comparison["current_filing_id"], registry)
    return (
        (previous or {}).get("source_hash") or "",
        (current or {}).get("source_hash") or "",
    )


def _raising_resolver(exc: Exception) -> Any:
    """A resolver that reports a pre-classified resolution outcome."""

    def resolve() -> tuple[str, str]:
        raise exc

    return resolve


def _replay_eligible(
    attempt: dict[str, Any],
    *,
    snapshot: dict[str, Any],
    moment: datetime,
    registry_path: str | Path | None,
    policy: dict[str, Any],
) -> bool:
    """Whether a replay would be accepted for this attempt right now.

    The verdict comes from ``detection_recovery.build_recovery_view_from_records``
    — the PURE calculation the recovery endpoint itself delegates to — fed
    entirely from records this report already loaded through the read-only
    snapshot. The aggregate count and the per-attempt recovery view therefore
    share one calculation and can never contradict each other, and NOTHING on
    this path touches SQLite: no store getter, no ``recovery_view``, no
    ``init_db``, no table creation, no migration.

    Registry resolution stays an external FILE read, eager and DISCRIMINATED
    (unchanged dependency semantics):

    * ``ComparisonPairError`` means the registry ANSWERED — this pair no longer
      validates, so the attempt genuinely is not replay-eligible. The error is
      handed to the pure calculation, which reports it under the same stable
      inputs-changed blocking code the recovery endpoint would use.
    * anything else means the dependency failed mid-flight (a permission fault,
      a file deleted between the check and the read), which fails the report
      closed rather than being counted as "not eligible".
    """
    import detection_recovery

    _require_filing_registry(registry_path)
    comparison = snapshot["comparisons_by_id"].get(attempt["comparison_id"])
    if comparison is None:
        # No comparison row: the lifecycle check blocks before the resolver
        # could ever run, so the outcome cannot depend on this exception.
        resolve_source_hashes = _raising_resolver(
            RuntimeError("comparison record absent from snapshot")
        )
    else:
        try:
            hashes = _registry_source_hashes(comparison, registry_path)
        except comparison_store.ComparisonPairError as pair_error:
            resolve_source_hashes = _raising_resolver(pair_error)
        except Exception as exc:
            raise ReliabilityDependencyUnavailable(
                DEPENDENCY_FILING_REGISTRY,
                DEPENDENCY_RESOLUTION_FAILED,
                "resolving filing inputs for "
                f"{attempt['comparison_id']} failed: {exc!r}",
            ) from exc
        else:
            def resolve_source_hashes() -> tuple[str, str]:
                return hashes

    view = detection_recovery.build_recovery_view_from_records(
        comparison=comparison,
        source_attempt=attempt,
        attempts_used=snapshot["attempts_per_comparison"].get(
            attempt["comparison_id"], 0
        ),
        existing_replay=snapshot["replays_by_source"].get(attempt["attempt_id"]),
        resolve_source_hashes=resolve_source_hashes,
        policy=policy,
        now=moment,
    )
    return bool(view["replay_eligible"])


def _negative_duration_attempts(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Terminal attempts whose finish precedes their start (clock integrity).

    Not window-scoped: a skewed clock is a present data-integrity problem
    regardless of which historical interval a caller asked about.
    """
    negative = []
    for attempt in snapshot["attempts"]:
        if attempt["status"] == comparison_store.ATTEMPT_RUNNING:
            continue
        if attempt["finished_dt"] is None or attempt["started_dt"] is None:
            continue  # pragma: no cover - refused by _load
        if (attempt["finished_dt"] - attempt["started_dt"]).total_seconds() < 0:
            negative.append(attempt)
    return negative


def _issue(
    issue_type: str,
    *,
    comparison_id: str,
    detected_at: datetime,
    created_dt: datetime | None,
    attempts_used: int,
    max_attempts: int,
    recommended_action_code: str,
    status: str,
    attempt: dict[str, Any] | None = None,
    replay_id: str | None = None,
    failure_code: str | None = None,
    stale_at: str | None = None,
) -> dict[str, Any]:
    """One allowlisted issue record. Never carries prose, evidence, or paths."""
    age_seconds = (
        (detected_at - created_dt).total_seconds() if created_dt is not None else None
    )
    record = {
        "issue_type": issue_type,
        "comparison_id": comparison_id,
        "attempt_id": (attempt or {}).get("attempt_id"),
        "replay_id": replay_id,
        "status": status,
        "failure_code": failure_code,
        "started_at": (attempt or {}).get("started_at"),
        "created_at": created_dt.isoformat() if created_dt is not None else None,
        "detected_at": detected_at.isoformat(),
        "age_seconds": age_seconds,
        "stale_at": stale_at,
        "attempts_used": attempts_used,
        "max_attempts": max_attempts,
        "detector_version": (attempt or {}).get("detector_version"),
        "workflow_version": (attempt or {}).get("workflow_version"),
        "recommended_action_code": recommended_action_code,
    }
    return {field: record[field] for field in ISSUE_FIELDS}


def _issue_sort_key(issue: dict[str, Any]) -> tuple:
    """Deterministic: severity, then oldest condition first, then stable ids."""
    return (
        _ISSUE_SEVERITY[issue["issue_type"]],
        issue["created_at"] or "",
        issue["comparison_id"] or "",
        issue["attempt_id"] or "",
        issue["replay_id"] or "",
        issue["issue_type"],
    )


def _generate_issues(
    snapshot: dict[str, Any],
    *,
    moment: datetime,
    policy: dict[str, Any],
    registry_path: str | Path | None,
) -> list[dict[str, Any]]:
    """Derive every operational issue from source records. READ-ONLY.

    Issues are CALCULATED, never persisted: there is no issue table and no
    acknowledgement state, so an issue keeps appearing for exactly as long as
    the record state that produces it exists. Resolving one means an operator
    acting through the existing replay endpoint or accepting the failure — this
    function does nothing on its own. Everything derives from the snapshot plus
    registry file reads: no database path is even accepted here.
    """
    counts = snapshot["attempts_per_comparison"]
    max_attempts = policy["max_attempts_per_comparison"]
    issues: list[dict[str, Any]] = []

    running_by_comparison: dict[str, dict[str, Any]] = {}
    for attempt in _running_attempts(snapshot):
        running_by_comparison[attempt["comparison_id"]] = attempt
        staleness = _staleness(attempt, moment, policy)
        if not staleness["is_stale"]:
            continue
        eligible = _replay_eligible(
            attempt,
            snapshot=snapshot,
            moment=moment,
            registry_path=registry_path,
            policy=policy,
        )
        issues.append(
            _issue(
                ISSUE_STALE_RUNNING_ATTEMPT,
                comparison_id=attempt["comparison_id"],
                detected_at=moment,
                created_dt=attempt["started_dt"],
                attempts_used=counts.get(attempt["comparison_id"], 0),
                max_attempts=max_attempts,
                recommended_action_code=(
                    ACTION_INSPECT_AND_REPLAY if eligible else ACTION_NO_REPLAY_AVAILABLE
                ),
                status=attempt["status"],
                attempt=attempt,
                stale_at=staleness["stale_at"],
            )
        )

    for comparison in snapshot["comparisons"]:
        comparison_id = comparison["comparison_id"]
        used = counts.get(comparison_id, 0)
        # Exhausted-and-still-mid-flight only: a comparison that reached the
        # limit and then SUCCEEDED needs no attention, and a failed one is
        # reported as comparison_failed (a failed attempt is not replayable at
        # any attempt count, so the limit is not the operative constraint).
        if comparison["status"] == comparison_store.STATUS_DETECTING and (
            used >= max_attempts
        ):
            running = running_by_comparison.get(comparison_id)
            staleness = (
                _staleness(running, moment, policy) if running is not None else None
            )
            issues.append(
                _issue(
                    ISSUE_ATTEMPT_LIMIT_EXHAUSTED,
                    comparison_id=comparison_id,
                    detected_at=moment,
                    created_dt=comparison["updated_dt"],
                    attempts_used=used,
                    max_attempts=max_attempts,
                    recommended_action_code=ACTION_NEW_WORKFLOW_VERSION,
                    status=comparison["status"],
                    attempt=running,
                    stale_at=staleness["stale_at"] if staleness else None,
                )
            )
        if comparison["status"] == comparison_store.STATUS_FAILED:
            terminal = _last_failed_attempt(snapshot, comparison_id)
            issues.append(
                _issue(
                    ISSUE_COMPARISON_FAILED,
                    comparison_id=comparison_id,
                    detected_at=moment,
                    created_dt=comparison["updated_dt"],
                    attempts_used=used,
                    max_attempts=max_attempts,
                    recommended_action_code=ACTION_INSPECT_FAILURE,
                    status=comparison["status"],
                    attempt=terminal,
                    failure_code=comparison["failure_code"],
                )
            )

    for replay in snapshot["replays"]:
        replacement = snapshot["attempts_by_id"][replay["replacement_attempt_id"]]
        # A timed_out replacement is NOT reported here: timed_out is reachable
        # only inside a replay transaction that starts a successor, so that
        # successor's state is the live signal. A running replacement is not a
        # failure either.
        if replacement["status"] != comparison_store.ATTEMPT_FAILED:
            continue
        issues.append(
            _issue(
                ISSUE_REPLACEMENT_ATTEMPT_FAILED,
                comparison_id=replay["comparison_id"],
                detected_at=moment,
                created_dt=replacement["finished_dt"],
                attempts_used=counts.get(replay["comparison_id"], 0),
                max_attempts=max_attempts,
                recommended_action_code=ACTION_INSPECT_FAILURE,
                status=replacement["status"],
                attempt=replacement,
                replay_id=replay["replay_id"],
                failure_code=replacement["failure_code"],
            )
        )

    for attempt in _negative_duration_attempts(snapshot):
        issues.append(
            _issue(
                ISSUE_INVALID_NEGATIVE_DURATION,
                comparison_id=attempt["comparison_id"],
                detected_at=moment,
                created_dt=attempt["started_dt"],
                attempts_used=counts.get(attempt["comparison_id"], 0),
                max_attempts=max_attempts,
                recommended_action_code=ACTION_INSPECT_CLOCK,
                status=attempt["status"],
                attempt=attempt,
                failure_code=attempt["failure_code"],
            )
        )

    return sorted(issues, key=_issue_sort_key)


def _last_failed_attempt(
    snapshot: dict[str, Any], comparison_id: str
) -> dict[str, Any] | None:
    """The attempt whose failure the comparison carries, when determinable."""
    candidates = [
        attempt
        for attempt in snapshot["attempts"]
        if attempt["comparison_id"] == comparison_id
        and attempt["status"] == comparison_store.ATTEMPT_FAILED
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda attempt: attempt["attempt_number"])


# --- Public report surfaces ---------------------------------------------------


def summary(
    *,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
    now: datetime | None = None,
    db_path: str | Path | None = None,
    registry_path: str | Path | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The structured reliability aggregate. Mutates nothing.

    Historical counters, rates, durations, and failure breakdowns respect the
    inclusive window (attempts by ``started_at``, replays by ``requested_at``).
    Current-state gauges and the issue count are evaluated at query time and
    are NOT windowed.
    """
    parsed_since, parsed_until = parse_window(since, until)
    moment = _now(now)
    policy = _recovery_policy(policy)
    snapshot = _load(db_path)

    windowed = [
        attempt
        for attempt in snapshot["attempts"]
        if _in_window(attempt["started_dt"], parsed_since, parsed_until)
    ]
    by_status = {status: 0 for status in comparison_store.ATTEMPT_STATUSES}
    for attempt in windowed:
        by_status[attempt["status"]] += 1
    succeeded = by_status[comparison_store.ATTEMPT_SUCCEEDED]
    failed = by_status[comparison_store.ATTEMPT_FAILED]
    timed_out = by_status[comparison_store.ATTEMPT_TIMED_OUT]
    running_in_window = by_status[comparison_store.ATTEMPT_RUNNING]
    terminal = succeeded + failed + timed_out

    issues = _generate_issues(
        snapshot,
        moment=moment,
        policy=policy,
        registry_path=registry_path,
    )

    return {
        "contract_version": RELIABILITY_CONTRACT_VERSION,
        "generated_at": moment.isoformat(),
        "since": parsed_since.isoformat() if parsed_since else None,
        "until": parsed_until.isoformat() if parsed_until else None,
        "detector_versions": sorted(
            {attempt["detector_version"] for attempt in windowed}
        ),
        "workflow_versions": sorted(
            {attempt["workflow_version"] for attempt in windowed}
        ),
        "recovery_policy_id": policy["policy_id"],
        "recovery_policy_version": policy["policy_version"],
        "stale_after_seconds": policy["stale_after_seconds"],
        "max_attempts_per_comparison": policy["max_attempts_per_comparison"],
        "gauges": _gauges(
            snapshot,
            issues=issues,
            moment=moment,
            policy=policy,
            registry_path=registry_path,
        ),
        "attempts": {
            "attempts_started": len(windowed),
            "attempts_succeeded": succeeded,
            "attempts_failed": failed,
            "attempts_timed_out": timed_out,
            "attempts_running_in_window": running_in_window,
            "terminal_attempts": terminal,
        },
        "attempt_rates": {
            "success_rate": _rate(succeeded, terminal, "success_rate"),
            "failure_rate": _rate(failed, terminal, "failure_rate"),
            "timeout_rate": _rate(timed_out, terminal, "timeout_rate"),
        },
        **_replay_metrics(snapshot, parsed_since, parsed_until),
        "durations": _durations(windowed),
        "failure_breakdown": _failure_breakdown(windowed),
    }


def _gauges(
    snapshot: dict[str, Any],
    *,
    issues: list[dict[str, Any]],
    moment: datetime,
    policy: dict[str, Any],
    registry_path: str | Path | None,
) -> dict[str, Any]:
    """Current state at query time. Never restricted by the historical window.

    Computed from the snapshot plus registry file reads only — no database
    path is even accepted here, so no gauge can reach a store getter.
    """
    status_counts = {status: 0 for status in _GAUGED_COMPARISON_STATUSES}
    for comparison in snapshot["comparisons"]:
        status_counts[comparison["status"]] += 1

    running = _running_attempts(snapshot)
    stale = [
        attempt
        for attempt in running
        if _staleness(attempt, moment, policy)["is_stale"]
    ]
    # Only a stale attempt can be replay-eligible (the replay transaction
    # refuses a fresh one), so eligibility — and therefore the filing-registry
    # dependency — is evaluated over the stale set ONLY. With nothing stale,
    # this is an exact zero reached without reading the registry at all; with
    # something stale and the registry unable to answer, _replay_eligible fails
    # the whole report closed instead of reporting a clean zero.
    eligible = [
        attempt
        for attempt in stale
        if _replay_eligible(
            attempt,
            snapshot=snapshot,
            moment=moment,
            registry_path=registry_path,
            policy=policy,
        )
    ]
    max_attempts = policy["max_attempts_per_comparison"]
    # Same scope as the attempt_limit_exhausted ISSUE: a comparison still
    # mid-flight that has consumed its whole attempt budget. A comparison that
    # reached the limit and then reached a terminal state (detected or failed)
    # is not a current unresolved problem — the detected one succeeded, and the
    # failed one is reported as comparison_failed, where the attempt budget is
    # not the operative constraint (a failed attempt is not replayable at any
    # attempt count). Historical attempt counters are unaffected by this scope.
    exhausted = sum(
        1
        for comparison in snapshot["comparisons"]
        if comparison["status"] == comparison_store.STATUS_DETECTING
        and snapshot["attempts_per_comparison"].get(comparison["comparison_id"], 0)
        >= max_attempts
    )
    return {
        "comparisons_ready_for_detection": status_counts[
            comparison_store.STATUS_READY_FOR_DETECTION
        ],
        "comparisons_detecting": status_counts[comparison_store.STATUS_DETECTING],
        "comparisons_detected": status_counts[comparison_store.STATUS_DETECTED],
        "comparisons_failed": status_counts[comparison_store.STATUS_FAILED],
        "running_attempts": len(running),
        "stale_running_attempts": len(stale),
        "replay_eligible_attempts": len(eligible),
        "attempt_limit_exhausted_comparisons": exhausted,
        "unresolved_operational_issues": len(issues),
    }


def _replay_metrics(
    snapshot: dict[str, Any],
    since: datetime | None,
    until: datetime | None,
) -> dict[str, Any]:
    """Replay counters and the replay success rate.

    Windowed on ``requested_at``. A replacement that is still RUNNING is not a
    failure and is excluded from the rate's denominator.
    """
    windowed = [
        replay
        for replay in snapshot["replays"]
        if _in_window(replay["requested_dt"], since, until)
    ]
    counts = {status: 0 for status in comparison_store.ATTEMPT_STATUSES}
    for replay in windowed:
        replacement = snapshot["attempts_by_id"][replay["replacement_attempt_id"]]
        counts[replacement["status"]] += 1
    succeeded = counts[comparison_store.ATTEMPT_SUCCEEDED]
    failed = counts[comparison_store.ATTEMPT_FAILED]
    timed_out = counts[comparison_store.ATTEMPT_TIMED_OUT]
    running = counts[comparison_store.ATTEMPT_RUNNING]
    terminal = succeeded + failed + timed_out
    return {
        "replays": {
            "replays_started": len(windowed),
            "replay_replacements_succeeded": succeeded,
            "replay_replacements_failed": failed,
            "replay_replacements_running": running,
            "replay_replacements_timed_out": timed_out,
            "terminal_replay_replacements": terminal,
        },
        "replay_rates": {
            "replay_success_rate": _rate(succeeded, terminal, "replay_success_rate")
        },
    }


def _durations(windowed: list[dict[str, Any]]) -> dict[str, Any]:
    """Duration statistics over terminal in-window attempts.

    Duration is ``finished_at - started_at``. A NEGATIVE duration is excluded
    and counted separately (it also raises an ``invalid_negative_duration``
    issue) rather than being folded into a min/mean that would then be quietly
    wrong.
    """
    values: list[float] = []
    negative = 0
    for attempt in windowed:
        if attempt["status"] == comparison_store.ATTEMPT_RUNNING:
            continue
        seconds = (attempt["finished_dt"] - attempt["started_dt"]).total_seconds()
        if seconds < 0:
            negative += 1
            continue
        values.append(seconds)
    return {
        "duration_count": len(values),
        "duration_seconds_min": round(min(values), 6) if values else None,
        "duration_seconds_max": round(max(values), 6) if values else None,
        "duration_seconds_mean": (
            round(sum(values) / len(values), 6) if values else None
        ),
        "duration_seconds_p50": _rounded(percentile(values, 0.50)),
        "duration_seconds_p95": _rounded(percentile(values, 0.95)),
        "negative_duration_attempts": negative,
        "percentile_method": PERCENTILE_METHOD,
    }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _failure_breakdown(windowed: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts by stable failure code and by producing version.

    "Failures" for the version breakdowns means attempts whose status is
    ``failed`` OR ``timed_out`` — both are non-success terminal outcomes that
    carry a failure code.
    """
    failed_by_code: dict[str, int] = {}
    timed_out_by_code: dict[str, int] = {}
    by_detector: dict[str, int] = {}
    by_workflow: dict[str, int] = {}
    for attempt in windowed:
        status = attempt["status"]
        if status not in (
            comparison_store.ATTEMPT_FAILED,
            comparison_store.ATTEMPT_TIMED_OUT,
        ):
            continue
        code = attempt["failure_code"] or "unknown"
        target = (
            failed_by_code
            if status == comparison_store.ATTEMPT_FAILED
            else timed_out_by_code
        )
        target[code] = target.get(code, 0) + 1
        by_detector[attempt["detector_version"]] = (
            by_detector.get(attempt["detector_version"], 0) + 1
        )
        by_workflow[attempt["workflow_version"]] = (
            by_workflow.get(attempt["workflow_version"], 0) + 1
        )
    return {
        "failed_attempts_by_code": dict(sorted(failed_by_code.items())),
        "timed_out_attempts_by_code": dict(sorted(timed_out_by_code.items())),
        "failures_by_detector_version": dict(sorted(by_detector.items())),
        "failures_by_workflow_version": dict(sorted(by_workflow.items())),
    }


def issues(
    *,
    issue_type: str | None = None,
    comparison_id: str | None = None,
    limit: int | None = None,
    now: datetime | None = None,
    db_path: str | Path | None = None,
    registry_path: str | Path | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Currently unresolved operational issues, deterministically ordered.

    Current-state only: there is deliberately NO time window here, because the
    issue set is what needs attention now. That is also what keeps
    ``unresolved_operational_issues`` in the summary equal to the unfiltered
    total reported by this function.

    ``total`` counts the matching issues before ``limit`` truncates, and
    ``truncated`` says so explicitly — a cap is never silent.

    The full issue set is generated before ``issue_type`` / ``comparison_id``
    filtering, so a filtered request follows the same dependency contract as the
    summary: if a stale attempt needs a recovery evaluation the registry cannot
    answer, this fails closed rather than returning a shorter list.
    """
    issue_type = _validated_issue_type(issue_type)
    bound = validate_limit(limit)
    moment = _now(now)
    policy = _recovery_policy(policy)
    snapshot = _load(db_path)

    records = _generate_issues(
        snapshot,
        moment=moment,
        policy=policy,
        registry_path=registry_path,
    )
    if issue_type is not None:
        records = [item for item in records if item["issue_type"] == issue_type]
    if comparison_id:
        records = [item for item in records if item["comparison_id"] == comparison_id]
    return {
        "contract_version": RELIABILITY_CONTRACT_VERSION,
        "generated_at": moment.isoformat(),
        "recovery_policy_id": policy["policy_id"],
        "recovery_policy_version": policy["policy_version"],
        "total": len(records),
        "returned": min(len(records), bound),
        "truncated": len(records) > bound,
        "issues": records[:bound],
    }


def failures(
    *,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
    failure_code: str | None = None,
    detector_version: str | None = None,
    workflow_version: str | None = None,
    comparison_id: str | None = None,
    limit: int | None = None,
    now: datetime | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Failed and timed-out attempt summaries, newest first.

    Derived from the same allowlisted attempt read the summary uses, so the two
    can never disagree. Summaries only: no comparison result, no evidence, no
    exception text — ``failure_summary`` is the store's bounded, code-derived
    string.

    This listing needs no recovery evaluation, so it never reads the filing
    registry and cannot raise ReliabilityDependencyUnavailable. It still fails
    closed on inconsistent stored rows like every other surface.
    """
    parsed_since, parsed_until = parse_window(since, until)
    bound = validate_limit(limit)
    moment = _now(now)
    snapshot = _load(db_path)

    replay_by_replacement = {
        replay["replacement_attempt_id"]: replay for replay in snapshot["replays"]
    }
    records: list[dict[str, Any]] = []
    for attempt in snapshot["attempts"]:
        if attempt["status"] not in (
            comparison_store.ATTEMPT_FAILED,
            comparison_store.ATTEMPT_TIMED_OUT,
        ):
            continue
        if not _in_window(attempt["started_dt"], parsed_since, parsed_until):
            continue
        if failure_code and attempt["failure_code"] != failure_code:
            continue
        if detector_version and attempt["detector_version"] != detector_version:
            continue
        if workflow_version and attempt["workflow_version"] != workflow_version:
            continue
        if comparison_id and attempt["comparison_id"] != comparison_id:
            continue
        seconds = (attempt["finished_dt"] - attempt["started_dt"]).total_seconds()
        replay = replay_by_replacement.get(attempt["attempt_id"])
        record = {
            "attempt_id": attempt["attempt_id"],
            "comparison_id": attempt["comparison_id"],
            "attempt_number": attempt["attempt_number"],
            "status": attempt["status"],
            "failure_code": attempt["failure_code"],
            "failure_summary": attempt["failure_summary"],
            "detector_version": attempt["detector_version"],
            "workflow_version": attempt["workflow_version"],
            "started_at": attempt["started_at"],
            "finished_at": attempt["finished_at"],
            "duration_seconds": round(seconds, 6) if seconds >= 0 else None,
            "replay_id": replay["replay_id"] if replay else None,
            "source_attempt_id": replay["source_attempt_id"] if replay else None,
        }
        records.append({field: record[field] for field in FAILURE_FIELDS})

    # Newest failure first, ties broken by ascending attempt id. Two stable
    # sorts rather than one composite key, because a descending string sort
    # cannot be expressed as a negated key.
    records.sort(key=lambda item: item["attempt_id"])
    records.sort(key=lambda item: item["started_at"], reverse=True)
    return {
        "contract_version": RELIABILITY_CONTRACT_VERSION,
        "generated_at": moment.isoformat(),
        "since": parsed_since.isoformat() if parsed_since else None,
        "until": parsed_until.isoformat() if parsed_until else None,
        "total": len(records),
        "returned": min(len(records), bound),
        "truncated": len(records) > bound,
        "failures": records[:bound],
    }
