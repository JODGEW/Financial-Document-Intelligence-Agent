"""Checked lease policy and pure lease-state calculations.

The policy is loaded at import so a present-but-invalid checked-in policy stops
API/worker startup. A missing file uses identical baked-in defaults, matching
the repository's existing governance-policy convention.

Nothing in this module mutates workflow state. Lease expiry only makes work
eligible for a later explicit one-shot worker invocation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from governance.policy_validation import (
    GovernancePolicyConfigError,
    load_policy_mapping,
    validated_int,
    validated_policy_id,
)

_POLICY_PATH = (
    Path(__file__).resolve().parent
    / "policies"
    / "detection_job_lease_policy.yaml"
)
_POLICY_NAME = "detection_job_lease"

_DEFAULT_POLICY = {
    "policy_id": "detection_job_lease_v1",
    "policy_version": "1",
    "lease_duration_seconds": 120,
    "heartbeat_extension_seconds": 120,
    "reclaim_grace_seconds": 15,
    "max_claim_generations": 3,
}
_ALLOWED_KEYS = frozenset(_DEFAULT_POLICY)

_MAX_LEASE_SECONDS = 3_600
_MAX_HEARTBEAT_SECONDS = 3_600
_MAX_RECLAIM_GRACE_SECONDS = 300
_MAX_CLAIM_GENERATIONS = 10

# The worker wakes well before either controlled lease duration can elapse.
# The bounds prevent sub-quarter-second churn under a very small test policy
# and avoid a long silent gap under the checked two-minute defaults.
MIN_HEARTBEAT_INTERVAL_SECONDS = 0.25
MAX_HEARTBEAT_INTERVAL_SECONDS = 30.0


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load the exact lease-policy mapping, failing safely on invalid input."""
    raw = load_policy_mapping(Path(path or _POLICY_PATH), _POLICY_NAME)
    if raw is None:
        return dict(_DEFAULT_POLICY)

    unknown = set(raw) - _ALLOWED_KEYS
    missing = _ALLOWED_KEYS - set(raw)
    if unknown:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: unknown keys {sorted(unknown)}."
        )
    if missing:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: missing required keys {sorted(missing)}."
        )

    policy_version = raw["policy_version"]
    if isinstance(policy_version, (int, float)) and not isinstance(
        policy_version, bool
    ):
        policy_version = str(policy_version)

    return {
        "policy_id": validated_policy_id(
            _POLICY_NAME, "policy_id", raw["policy_id"]
        ),
        "policy_version": validated_policy_id(
            _POLICY_NAME, "policy_version", policy_version
        ),
        "lease_duration_seconds": _bounded_int(
            raw,
            "lease_duration_seconds",
            minimum=1,
            maximum=_MAX_LEASE_SECONDS,
        ),
        "heartbeat_extension_seconds": _bounded_int(
            raw,
            "heartbeat_extension_seconds",
            minimum=1,
            maximum=_MAX_HEARTBEAT_SECONDS,
        ),
        "reclaim_grace_seconds": _bounded_int(
            raw,
            "reclaim_grace_seconds",
            minimum=0,
            maximum=_MAX_RECLAIM_GRACE_SECONDS,
        ),
        "max_claim_generations": _bounded_int(
            raw,
            "max_claim_generations",
            minimum=1,
            maximum=_MAX_CLAIM_GENERATIONS,
        ),
    }


def _bounded_int(
    values: dict[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = validated_int(
        _POLICY_NAME, key, values[key], minimum=minimum
    )
    if value > maximum:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: field '{key}' must be <= {maximum}, "
            f"got {value}."
        )
    return value


POLICY = load_policy()


def heartbeat_interval_seconds(policy: dict[str, Any]) -> float:
    """Return the bounded worker-owned heartbeat cadence.

    One third of the smaller lease/extension window gives two further
    opportunities before that window elapses. Explicit bounds keep the
    controller conservative without permitting a busy loop.
    """
    lease_seconds = _bounded_int(
        policy,
        "lease_duration_seconds",
        minimum=1,
        maximum=_MAX_LEASE_SECONDS,
    )
    extension_seconds = _bounded_int(
        policy,
        "heartbeat_extension_seconds",
        minimum=1,
        maximum=_MAX_HEARTBEAT_SECONDS,
    )
    unbounded = min(lease_seconds, extension_seconds) / 3.0
    return max(
        MIN_HEARTBEAT_INTERVAL_SECONDS,
        min(MAX_HEARTBEAT_INTERVAL_SECONDS, unbounded),
    )


def utc_moment(now: datetime | None = None) -> datetime:
    """Return an aware UTC moment; naive injected clocks are refused."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("now must be timezone-aware")
    return moment.astimezone(timezone.utc)


def parse_utc(value: Any, *, field: str) -> datetime:
    """Parse one stored aware timestamp without exposing its value in errors."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO 8601 timestamp")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def lease_state(
    job: dict[str, Any], *, now: datetime | None = None
) -> str:
    """Return ``not_claimed|active|expired|terminal`` for a coherent job."""
    status = job.get("status")
    if status == "queued":
        return "not_claimed"
    if status in {"succeeded", "failed"}:
        return "terminal"
    if status != "running":
        raise ValueError("job status is not supported")
    expires = parse_utc(job.get("lease_expires_at"), field="lease_expires_at")
    return "active" if utc_moment(now) <= expires else "expired"


def reclaim_ready(
    job: dict[str, Any],
    *,
    reclaim_grace_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Whether expiry plus grace has strictly passed.

    Strictness avoids overlap with the inclusive finalization boundary at the
    exact expiry instant, including when grace is zero.
    """
    if job.get("status") != "running":
        return False
    expires = parse_utc(job.get("lease_expires_at"), field="lease_expires_at")
    boundary = expires + timedelta(seconds=reclaim_grace_seconds)
    return utc_moment(now) > boundary
