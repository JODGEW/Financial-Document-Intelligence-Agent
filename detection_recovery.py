"""Bounded stale-detection recognition and operator-controlled replay.

Stage 3.5 reliability step 2. The previous commit made every detection
execution durable: a killed process leaves ``comparison.status='detecting'``
with a ``running`` attempt and only a ``detection_started`` event. That state
was visible but permanent. This module lets an operator resolve it — and
nothing else does.

What this module does NOT do, stated plainly
--------------------------------------------
There is no automatic retry or replay, replay worker, scheduler, polling loop,
cron entry, dead-letter queue, or exponential backoff. The separate
initial-detection worker is one-shot and never handles replay. Crossing the
configured stale threshold changes NOTHING on its own: an eligible
direct/replay attempt remains ``running`` until a human explicitly POSTs a
replay request. A worker-owned job attempt is not eligible in this commit
because no lease, heartbeat, fencing, or safe reclaim exists. Reading the
recovery endpoint is side-effect free — observing an old attempt never retires
it. "Stale" is a statement about eligibility, not an action.

Failed attempts are also NOT replayable here. Replay exists for one narrow
condition: a ``running`` attempt whose process is gone. General retry of a
failed detection is deliberately out of scope.

Operator identity is attributable at two explicit trust levels. Direct library
callers retain ``legacy_self_asserted`` metadata for backward compatibility.
The protected API supplies a verified Principal subject plus narrow
``local_hs256`` token/policy provenance. Replay records and transition events
are append-only application records, NOT tamper-proof storage.

Boundedness
-----------
``max_attempts_per_comparison`` caps how many attempts one comparison may ever
accumulate, counting every status. The limit is enforced inside the replay
transaction, so it holds under concurrent requests. Once reached, the
comparison is done: a genuinely fresh run means a new comparison, which is what
a workflow-version bump already produces.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import comparison_detector
import comparison_reliability
import comparison_store
import config
from governance.policy_validation import (
    GovernancePolicyConfigError,
    load_policy_mapping,
    require_mapping_section,
    validated_int,
    validated_policy_id,
)

_POLICY_PATH = (
    Path(__file__).resolve().parent / "policies" / "detection_recovery_policy.yaml"
)
_POLICY_NAME = "detection_recovery"

# Baked-in defaults, used ONLY when the YAML file is absent. Kept identical to
# policies/detection_recovery_policy.yaml (documented governance convention).
_DEFAULT_IDENTITY = {
    "policy_id": "detection_recovery_v1",
    "policy_version": "1",
}
_DEFAULT_STALE_AFTER_SECONDS = 900
_DEFAULT_MAX_ATTEMPTS = 3

# Explicit upper bounds so an accidental extreme value fails loudly instead of
# quietly disabling recovery (or permitting unbounded attempts) forever.
_MAX_STALE_AFTER_SECONDS = 86_400  # 24h
_MAX_ATTEMPTS_CEILING = 10

MAX_OPERATOR_ID_CHARS = 120
MAX_OPERATOR_NOTE_CHARS = 500

# Stable blocking codes reported by the read-only recovery view. They are the
# SAME codes a replay request would return, so the preview cannot disagree with
# the decision.
BLOCK_NOT_RUNNING = comparison_store.REASON_ATTEMPT_NOT_RUNNING
BLOCK_NOT_STALE = comparison_store.REASON_ATTEMPT_NOT_STALE
BLOCK_LIMIT_REACHED = comparison_store.REASON_ATTEMPT_LIMIT_REACHED
BLOCK_ALREADY_REPLAYED = comparison_store.REASON_REPLAY_ALREADY_EXISTS
BLOCK_INPUTS_CHANGED = comparison_store.REASON_REPLAY_INPUTS_CHANGED
BLOCK_VERSION_CHANGED = comparison_store.REASON_REPLAY_VERSION_CHANGED
BLOCK_LIFECYCLE_INVALID = comparison_store.REASON_TRANSITION_INVALID
BLOCK_JOB_RECLAIM_NOT_SUPPORTED = (
    comparison_store.REASON_JOB_RECLAIM_NOT_SUPPORTED
)


class ReplayRequestError(Exception):
    """Invalid replay input (API: 422). ``code`` is stable; the message is safe
    for display — no operator prose, paths, SQL, or document content."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ReplayAttemptNotFound(Exception):
    """Unknown detection attempt (API: 404)."""


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the detection recovery policy.

    Missing file -> baked-in defaults (documented contract). Present invalid
    file -> GovernancePolicyConfigError at import, never a silent fallback:
    reliability configuration that cannot be trusted must stop startup.
    """
    raw = load_policy_mapping(Path(path or _POLICY_PATH), _POLICY_NAME)
    if raw is None:
        return {
            "policy_id": _DEFAULT_IDENTITY["policy_id"],
            "policy_version": _DEFAULT_IDENTITY["policy_version"],
            "stale_after_seconds": _DEFAULT_STALE_AFTER_SECONDS,
            "max_attempts_per_comparison": _DEFAULT_MAX_ATTEMPTS,
        }

    identity = require_mapping_section(raw, "detection_recovery_policy", _POLICY_NAME)
    staleness = require_mapping_section(raw, "staleness", _POLICY_NAME)
    limits = require_mapping_section(raw, "attempt_limits", _POLICY_NAME)
    if identity is None or staleness is None or limits is None:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: all three sections are required "
            "('detection_recovery_policy', 'staleness', 'attempt_limits'). "
            "Restore the documented mapping or delete the file to use the "
            "built-in defaults."
        )

    policy_id = validated_policy_id(
        _POLICY_NAME, "detection_recovery_policy.policy_id", identity.get("policy_id")
    )
    policy_version = identity.get("policy_version")
    if isinstance(policy_version, (int, float)) and not isinstance(
        policy_version, bool
    ):
        policy_version = str(policy_version)
    policy_version = validated_policy_id(
        _POLICY_NAME,
        "detection_recovery_policy.policy_version",
        policy_version,
    )

    stale_after_seconds = _bounded_int(
        staleness,
        section="staleness",
        key="stale_after_seconds",
        minimum=1,
        maximum=_MAX_STALE_AFTER_SECONDS,
    )
    max_attempts = _bounded_int(
        limits,
        section="attempt_limits",
        key="max_attempts_per_comparison",
        minimum=1,
        maximum=_MAX_ATTEMPTS_CEILING,
    )

    # Unknown keys are REJECTED, not ignored. A typo'd 'stale_after_second'
    # would otherwise silently keep the default, and for a safety threshold
    # that fails in the dangerous direction (the operator believes they
    # configured a value that is not in effect).
    _reject_unknown(staleness, {"stale_after_seconds"}, "staleness")
    _reject_unknown(limits, {"max_attempts_per_comparison"}, "attempt_limits")
    _reject_unknown(
        identity, {"policy_id", "policy_version"}, "detection_recovery_policy"
    )

    return {
        "policy_id": policy_id,
        "policy_version": policy_version,
        "stale_after_seconds": stale_after_seconds,
        "max_attempts_per_comparison": max_attempts,
    }


def _bounded_int(
    values: dict[str, Any],
    *,
    section: str,
    key: str,
    minimum: int,
    maximum: int,
) -> int:
    """Required integer with both bounds and a field-specific safe message."""
    section_values, field_name = values, f"{section}.{key}"
    if key not in section_values:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: '{field_name}' is required."
        )
    # validated_int enforces actual-int (bool is rejected) and the lower bound.
    value = validated_int(
        _POLICY_NAME, field_name, section_values[key], minimum=minimum
    )
    if value > maximum:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: field '{field_name}' must be <= {maximum}, "
            f"got {value}. An extreme value is refused rather than silently "
            "disabling bounded recovery."
        )
    return value


def _reject_unknown(section: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(section) - allowed
    if unknown:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: unknown {name} keys {sorted(unknown)}. "
            "Unknown keys are rejected so a mistyped field cannot leave the "
            "default silently in effect."
        )


POLICY = load_policy()


def _clean_bounded(value: Any, *, field: str, code: str, max_chars: int) -> str:
    """Strip surrounding whitespace, require non-empty, bound length, and reject
    control characters (unsafe for plain display). Case is PRESERVED — an
    operator id is metadata to record verbatim, not to normalize."""
    if not isinstance(value, str) or not value.strip():
        raise ReplayRequestError(
            code, f"{field} is required and must be a non-empty string"
        )
    cleaned = value.strip()
    if len(cleaned) > max_chars:
        raise ReplayRequestError(
            code, f"{field} must be at most {max_chars} characters"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
        raise ReplayRequestError(
            code, f"{field} must not contain control characters"
        )
    return cleaned


def replay_request_hash(
    *,
    source_attempt_id: str,
    operator_id: str,
    reason_code: str,
    operator_note: str,
    policy_id: str,
    policy_version: str,
) -> str:
    """Canonical request hash: byte-equivalent requests collide by design."""
    canonical = json.dumps(
        {
            "source_attempt_id": source_attempt_id,
            "operator_id": operator_id,
            "reason_code": reason_code,
            "operator_note": operator_note,
            "policy_id": policy_id,
            "policy_version": policy_version,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_recovery_view_from_records(
    *,
    comparison: dict[str, Any] | None,
    source_attempt: dict[str, Any],
    attempts_used: int,
    existing_replay: dict[str, Any] | None,
    resolve_source_hashes: Callable[[], tuple[str, str]],
    policy: dict[str, Any],
    now: datetime,
    active_detection_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """PURE recovery assessment over already-loaded records. No storage access.

    The storage-independent half of ``recovery_view``: every input is a value
    the caller already holds — the comparison record (or None when it does not
    exist), the attempt row, the comparison's total attempt count, the replay
    that retired this attempt if one exists, the recovery policy, and an
    injected clock. This function performs NO SQLite access, NO initialization,
    and NO mutation of any kind, which is what lets the read-only reliability
    report evaluate replay eligibility from its snapshot without ever touching
    a store getter.

    ``resolve_source_hashes`` supplies registry truth for the pair as
    ``(previous_hash, current_hash)`` and is invoked ONLY when every earlier
    check passes — mirroring how the storage-backed view resolved inputs last.
    Any exception it raises is reported as ``detection_replay_inputs_changed``,
    exactly as before: the registry no longer supports this pair as-captured.

    Blocking checks run in the same order the replay transaction applies them,
    so the preview and the decision cannot disagree.
    """
    staleness = comparison_store.evaluate_staleness(
        source_attempt["started_at"], now, policy["stale_after_seconds"]
    )
    max_attempts = policy["max_attempts_per_comparison"]
    blocking = _blocking_reason_from_records(
        comparison=comparison,
        source_attempt=source_attempt,
        staleness=staleness,
        attempts_used=attempts_used,
        max_attempts=max_attempts,
        existing_replay=existing_replay,
        active_detection_job=active_detection_job,
        resolve_source_hashes=resolve_source_hashes,
    )
    return {
        "attempt_id": source_attempt["attempt_id"],
        "comparison_id": source_attempt["comparison_id"],
        "status": source_attempt["status"],
        "started_at": source_attempt["started_at"],
        "stale_at": staleness["stale_at"],
        "age_seconds": staleness["age_seconds"],
        "is_stale": staleness["is_stale"],
        "replay_eligible": blocking is None,
        "attempts_used": attempts_used,
        "max_attempts": max_attempts,
        "remaining_attempts": max(0, max_attempts - attempts_used),
        "policy_id": policy["policy_id"],
        "policy_version": policy["policy_version"],
        "blocking_reason": blocking,
    }


def recovery_view(
    attempt_id: str,
    *,
    now: datetime | None = None,
    db_path: str | Path | None = None,
    registry_path: str | Path | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """READ-ONLY recovery assessment for one attempt. Mutates nothing.

    Reports whether the attempt has crossed the configured stale threshold and
    whether a replay would currently be accepted, using the SAME stable codes a
    replay request would return. Calling this never retires an attempt, never
    starts one, and never writes anything.

    This is the storage-backed loader for one attempt id: it reads the records
    through the ordinary store interfaces and delegates the entire assessment
    to ``build_recovery_view_from_records``, so this endpoint and the
    aggregate reliability report share one calculation and cannot disagree.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    policy = policy or POLICY
    moment = now or datetime.now(timezone.utc)

    attempt = comparison_store.get_detection_attempt(attempt_id, db_path=db_path)
    if attempt is None:
        raise ReplayAttemptNotFound(attempt_id)
    comparison_id = attempt["comparison_id"]

    def resolve_source_hashes() -> tuple[str, str]:
        inputs = comparison_detector.resolve_detection_inputs(
            comparison_id, db_path=db_path, registry_path=registry_path
        )
        return inputs["previous_hash"], inputs["current_hash"]

    return build_recovery_view_from_records(
        comparison=comparison_store.get_comparison(comparison_id, db_path=db_path),
        source_attempt=attempt,
        attempts_used=comparison_store.count_detection_attempts(
            comparison_id, db_path=db_path
        ),
        existing_replay=comparison_store.get_detection_replay_for_source(
            attempt_id, db_path=db_path
        ),
        active_detection_job=comparison_store.get_detection_job_for_attempt(
            attempt_id, db_path=db_path
        ),
        resolve_source_hashes=resolve_source_hashes,
        policy=policy,
        now=moment,
    )


def _blocking_reason_from_records(
    *,
    comparison: dict[str, Any] | None,
    source_attempt: dict[str, Any],
    staleness: dict[str, Any],
    attempts_used: int,
    max_attempts: int,
    existing_replay: dict[str, Any] | None,
    active_detection_job: dict[str, Any] | None,
    resolve_source_hashes: Callable[[], tuple[str, str]],
) -> str | None:
    """The first stable code that would refuse a replay right now, or None.

    Pure: consumes only the passed records and the resolver callable. Evaluated
    in the same order the replay transaction applies its checks, so the preview
    and the decision cannot disagree.
    """
    if existing_replay is not None:
        return BLOCK_ALREADY_REPLAYED
    if (
        active_detection_job is not None
        and active_detection_job.get("status") == comparison_store.JOB_RUNNING
    ):
        return BLOCK_JOB_RECLAIM_NOT_SUPPORTED
    if comparison is None or comparison["status"] != comparison_store.STATUS_DETECTING:
        return BLOCK_LIFECYCLE_INVALID
    if source_attempt["status"] != comparison_store.ATTEMPT_RUNNING:
        return BLOCK_NOT_RUNNING
    if not staleness["is_stale"]:
        return BLOCK_NOT_STALE
    if attempts_used >= max_attempts:
        return BLOCK_LIMIT_REACHED
    if (
        source_attempt["detector_version"] != comparison_detector.DETECTOR_VERSION
        or source_attempt["workflow_version"] != comparison_store.WORKFLOW_VERSION
    ):
        return BLOCK_VERSION_CHANGED
    try:
        previous_hash, current_hash = resolve_source_hashes()
    except Exception:
        # The registry no longer supports this pair. Surface it as an input
        # change rather than failing the read-only view.
        return BLOCK_INPUTS_CHANGED
    if (
        source_attempt["previous_source_hash"] != previous_hash
        or source_attempt["current_source_hash"] != current_hash
    ):
        return BLOCK_INPUTS_CHANGED
    return None


def replay_attempt(
    attempt_id: str,
    *,
    operator_id: str,
    reason_code: str,
    operator_note: str,
    now: datetime | None = None,
    db_path: str | Path | None = None,
    registry_path: str | Path | None = None,
    chroma_client=None,
    policy: dict[str, Any] | None = None,
    actor_context: Mapping[str, Any] | None = None,
    actor_policy_id: str | None = None,
    actor_policy_version: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Retire a stale attempt and run its replacement. EXPLICIT operator action.

    Returns ``(outcome, created)``. ``created=True`` means this request retired
    the stale attempt, started the replacement, and executed it. ``False`` means
    the identical request was already applied: the stored replay and the
    replacement's CURRENT terminal outcome are returned and the detector is NOT
    run again.

    Raises ReplayRequestError (422) for invalid operator fields,
    ReplayAttemptNotFound (404), and comparison_store.DetectionStateError (409)
    with a stable code when the persisted state does not permit replay — not
    stale, already replayed differently, wrong lifecycle, attempt limit
    reached, or changed inputs/versions. Nothing is mutated on refusal.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    policy = policy or POLICY

    operator_id = _clean_bounded(
        operator_id, field="operatorId", code="invalid_operator_id",
        max_chars=MAX_OPERATOR_ID_CHARS,
    )
    operator_note = _clean_bounded(
        operator_note, field="operatorNote", code="invalid_operator_note",
        max_chars=MAX_OPERATOR_NOTE_CHARS,
    )
    attribution = comparison_store.actor_attribution_from_context(
        operator_id,
        actor_context,
        actor_policy_id=actor_policy_id,
        actor_policy_version=actor_policy_version,
    )
    if reason_code not in comparison_store.REPLAY_REASON_CODES:
        raise ReplayRequestError(
            "invalid_reason_code",
            "replay requires one of "
            f"{list(comparison_store.REPLAY_REASON_CODES)}",
        )

    attempt = comparison_store.get_detection_attempt(attempt_id, db_path=db_path)
    if attempt is None:
        raise ReplayAttemptNotFound(attempt_id)
    comparison_id = attempt["comparison_id"]

    # Registry truth, resolved OUTSIDE the transaction (it reads JSONL) and
    # revalidated inside it against what the stale attempt captured at start.
    inputs = comparison_detector.resolve_detection_inputs(
        comparison_id, db_path=db_path, registry_path=registry_path
    )

    request_hash = replay_request_hash(
        source_attempt_id=attempt_id,
        operator_id=operator_id,
        reason_code=reason_code,
        operator_note=operator_note,
        policy_id=policy["policy_id"],
        policy_version=policy["policy_version"],
    )

    replay, created = comparison_store.start_detection_replay(
        attempt_id,
        operator_id=operator_id,
        reason_code=reason_code,
        operator_note=operator_note,
        request_hash=request_hash,
        policy_id=policy["policy_id"],
        policy_version=policy["policy_version"],
        stale_after_seconds=policy["stale_after_seconds"],
        max_attempts_per_comparison=policy["max_attempts_per_comparison"],
        detector_version=comparison_detector.DETECTOR_VERSION,
        workflow_version=comparison_store.WORKFLOW_VERSION,
        previous_source_hash=inputs["previous_hash"],
        current_source_hash=inputs["current_hash"],
        **attribution,
        now=now,
        db_path=db_path,
    )
    replacement_id = replay["replacement_attempt_id"]

    if not created:
        # The identical request already ran. Report the replacement's CURRENT
        # terminal outcome without executing anything again — including the case
        # where it is still running because that execution was itself
        # interrupted. NOTHING is logged as a transition here: no state changed.
        return _replay_outcome(replay, replacement_id, db_path), False

    # Transaction closed and committed. Emit the three transitions it applied,
    # correlated by replay_id (Stage 3.5 step 3). Reads only.
    _log_replay_transitions(
        replay,
        attempt_id,
        replacement_id,
        db_path,
        actor_context=actor_context,
    )

    # Now run the replacement through the SAME execution seam a first attempt
    # uses (which emits the replacement's own succeeded/failed record).
    result, _result_created, _attempt = comparison_detector.execute_attempt(
        replacement_id,
        record=inputs["record"],
        previous_ref=inputs["previous_ref"],
        current_ref=inputs["current_ref"],
        previous_entry=inputs["previous_entry"],
        current_entry=inputs["current_entry"],
        previous_hash=inputs["previous_hash"],
        current_hash=inputs["current_hash"],
        chroma_client=chroma_client,
        db_path=db_path,
        actor_context=actor_context,
    )
    outcome = _replay_outcome(replay, replacement_id, db_path)
    outcome["result"] = result
    replacement = comparison_store.get_detection_attempt(
        replacement_id, db_path=db_path
    )
    comparison_reliability.log_lifecycle_event(
        comparison_reliability.EVENT_REPLAY_COMPLETED,
        attempt=replacement,
        comparison_id=replay["comparison_id"],
        replay_id=replay["replay_id"],
        source_attempt_id=attempt_id,
        elapsed_ms=comparison_reliability.elapsed_ms_for(replacement),
        actor_context=actor_context,
    )
    return outcome, True


def _log_replay_transitions(
    replay: dict[str, Any],
    source_attempt_id: str,
    replacement_id: str,
    db_path: str | Path,
    *,
    actor_context: Mapping[str, Any] | None = None,
) -> None:
    """Structured records for the three transitions one replay transaction made.

    Emitted after the commit and never inside it: a logging fault must not be
    able to abort the replay, and a rolled-back transaction must not leave a
    record claiming it happened. Operator note is deliberately absent. When
    present, actor context is the narrow Principal-derived allowlist; legacy
    self-asserted identifiers do not enter authenticated actor fields.
    """
    source = comparison_store.get_detection_attempt(
        source_attempt_id, db_path=db_path
    )
    replacement = comparison_store.get_detection_attempt(
        replacement_id, db_path=db_path
    )
    comparison_reliability.log_lifecycle_event(
        comparison_reliability.EVENT_ATTEMPT_TIMED_OUT,
        attempt=source,
        comparison_id=replay["comparison_id"],
        replay_id=replay["replay_id"],
        elapsed_ms=comparison_reliability.elapsed_ms_for(source),
        actor_context=actor_context,
    )
    comparison_reliability.log_lifecycle_event(
        comparison_reliability.EVENT_ATTEMPT_STARTED,
        attempt=replacement,
        comparison_id=replay["comparison_id"],
        replay_id=replay["replay_id"],
        source_attempt_id=source_attempt_id,
        actor_context=actor_context,
    )
    comparison_reliability.log_lifecycle_event(
        comparison_reliability.EVENT_REPLAY_CREATED,
        attempt=replacement,
        comparison_id=replay["comparison_id"],
        replay_id=replay["replay_id"],
        source_attempt_id=source_attempt_id,
        actor_context=actor_context,
    )


def _replay_outcome(
    replay: dict[str, Any], replacement_id: str, db_path: str | Path
) -> dict[str, Any]:
    """Assemble the replay response payload from persisted state only."""
    replacement = comparison_store.get_detection_attempt(
        replacement_id, db_path=db_path
    )
    stored = comparison_store.get_result(replay["comparison_id"], db_path=db_path)
    result = None
    if (
        replacement is not None
        and replacement["status"] == comparison_store.ATTEMPT_SUCCEEDED
        and stored is not None
        and stored["result_hash"] == replacement["result_hash"]
    ):
        result = stored["result"]
    return {
        "replay": dict(replay),
        "source_attempt_id": replay["source_attempt_id"],
        "replacement_attempt_id": replacement_id,
        "replacement_status": (
            replacement["status"] if replacement else None
        ),
        "result": result,
    }
