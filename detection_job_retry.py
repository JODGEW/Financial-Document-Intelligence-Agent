"""Checked bounded retry policy and closed detection-failure classification.

Retry eligibility is determined only from this stable-code registry. Unknown
codes fail closed as ``unknown_internal`` and are never retryable. Loading this
module performs no workflow mutation; a retry still requires a later explicit
one-shot worker invocation after its deterministic due time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from governance.policy_validation import (
    GovernancePolicyConfigError,
    load_policy_mapping,
    validated_int,
    validated_policy_id,
)

RETRYABLE_TRANSIENT = "retryable_transient"
NON_RETRYABLE_DOMAIN = "non_retryable_domain"
NON_RETRYABLE_INTEGRITY = "non_retryable_integrity"
NON_RETRYABLE_CONFIGURATION = "non_retryable_configuration"
OWNERSHIP_LOST = "ownership_lost"
UNKNOWN_INTERNAL = "unknown_internal"

FAILURE_CLASSIFICATIONS = frozenset(
    {
        RETRYABLE_TRANSIENT,
        NON_RETRYABLE_DOMAIN,
        NON_RETRYABLE_INTEGRITY,
        NON_RETRYABLE_CONFIGURATION,
        OWNERSHIP_LOST,
        UNKNOWN_INTERNAL,
    }
)

# The controlled detector currently has no genuine transient domain failure.
# This explicit dependency-boundary code is the sole initial retryable seam;
# arbitrary provider exceptions and detector_internal_error remain fail-closed.
FAILURE_DEPENDENCY_UNAVAILABLE = "detection_dependency_unavailable"

_DOMAIN_CODES = {
    "comparison_not_ready",
    "previous_section_missing",
    "current_section_missing",
    "section_metadata_incomplete",
    "section_unit_parse_failed",
    "ambiguous_unit_alignment",
    "evidence_resolution_failed",
    "comparison_inputs_stale",
    "detector_version_superseded",
    "detection_in_progress",
    "detection_attempt_not_found",
    "detection_attempt_not_running",
    "detection_attempt_not_stale",
    "detection_attempt_limit_reached",
    "detection_replay_already_exists",
    "detection_replay_inputs_changed",
    "detection_replay_version_changed",
    "detection_attempt_managed_by_job",
    "unknown_previous_filing",
    "unknown_current_filing",
    "previous_filing_not_parsed",
    "current_filing_not_parsed",
    "previous_filing_identity_conflicted",
    "current_filing_identity_conflicted",
    "previous_filing_metadata_incomplete",
    "current_filing_metadata_incomplete",
    "empty_section_scope",
    "unsupported_section_scope",
    "detection_attempt_timed_out",
    "detection_attempt_worker_lease_expired",
}
_INTEGRITY_CODES = {
    "detection_transition_invalid",
    "detection_inputs_changed",
    "detection_job_conflict",
    "detection_job_not_found",
    "detection_job_not_queued",
    "detection_job_attempt_mismatch",
    "detection_job_result_hash_mismatch",
    "detection_job_inputs_changed",
    "detection_job_version_changed",
    "detection_job_claims_exhausted",
    "detection_job_clock_invalid",
    "detection_job_retries_exhausted",
    "detection_job_execution_budget_exhausted",
}
_OWNERSHIP_CODES = {
    "detection_job_not_running",
    "detection_job_claim_invalid",
    "detection_job_worker_mismatch",
    "detection_job_lease_expired",
    "detection_job_claim_fenced",
}
_CONFIGURATION_CODES = {
    "detection_job_retry_policy_invalid",
    "detection_job_lease_policy_invalid",
}
_UNKNOWN_CODES = {
    "detector_internal_error",
    "detection_job_heartbeat_internal_error",
}

FAILURE_REGISTRY: dict[str, dict[str, Any]] = {
    **{
        code: {"classification": NON_RETRYABLE_DOMAIN, "retryable": False}
        for code in sorted(_DOMAIN_CODES)
    },
    **{
        code: {"classification": NON_RETRYABLE_INTEGRITY, "retryable": False}
        for code in sorted(_INTEGRITY_CODES)
    },
    **{
        code: {"classification": OWNERSHIP_LOST, "retryable": False}
        for code in sorted(_OWNERSHIP_CODES)
    },
    **{
        code: {
            "classification": NON_RETRYABLE_CONFIGURATION,
            "retryable": False,
        }
        for code in sorted(_CONFIGURATION_CODES)
    },
    **{
        code: {"classification": UNKNOWN_INTERNAL, "retryable": False}
        for code in sorted(_UNKNOWN_CODES)
    },
    FAILURE_DEPENDENCY_UNAVAILABLE: {
        "classification": RETRYABLE_TRANSIENT,
        "retryable": True,
    },
}

_POLICY_PATH = (
    Path(__file__).resolve().parent
    / "policies"
    / "detection_job_retry_policy.yaml"
)
_POLICY_NAME = "detection_job_retry"
_DEFAULT_POLICY = {
    "policy_id": "detection_job_retry_v1",
    "policy_version": "1",
    "max_retry_attempts": 2,
    "retry_delays_seconds": [5, 30],
    "retryable_failure_codes": [FAILURE_DEPENDENCY_UNAVAILABLE],
}
_ALLOWED_KEYS = frozenset(_DEFAULT_POLICY)
MAX_RETRY_ATTEMPTS = 10
MAX_RETRY_DELAY_SECONDS = 86_400


def classify_failure_code(code: Any) -> str:
    """Return one closed classification; unknown values fail closed."""
    if not isinstance(code, str):
        return UNKNOWN_INTERNAL
    entry = FAILURE_REGISTRY.get(code)
    return entry["classification"] if entry else UNKNOWN_INTERNAL


def is_registry_retryable(code: Any) -> bool:
    if not isinstance(code, str):
        return False
    entry = FAILURE_REGISTRY.get(code)
    return bool(entry and entry["retryable"])


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load the exact checked mapping, failing safely on invalid input."""
    raw = load_policy_mapping(Path(path or _POLICY_PATH), _POLICY_NAME)
    if raw is None:
        return {
            **_DEFAULT_POLICY,
            "retry_delays_seconds": list(
                _DEFAULT_POLICY["retry_delays_seconds"]
            ),
            "retryable_failure_codes": list(
                _DEFAULT_POLICY["retryable_failure_codes"]
            ),
        }

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
    maximum = _bounded_int(
        raw,
        "max_retry_attempts",
        minimum=0,
        maximum=MAX_RETRY_ATTEMPTS,
    )
    delays = raw["retry_delays_seconds"]
    if not isinstance(delays, list):
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: field 'retry_delays_seconds' must be a list."
        )
    if len(delays) != maximum:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: field 'retry_delays_seconds' must contain "
            "exactly max_retry_attempts entries."
        )
    checked_delays: list[int] = []
    for index, value in enumerate(delays):
        if isinstance(value, bool) or not isinstance(value, int):
            raise GovernancePolicyConfigError(
                f"{_POLICY_NAME} policy: field 'retry_delays_seconds[{index}]' "
                "must be an integer."
            )
        if value < 1 or value > MAX_RETRY_DELAY_SECONDS:
            raise GovernancePolicyConfigError(
                f"{_POLICY_NAME} policy: field 'retry_delays_seconds[{index}]' "
                f"must be between 1 and {MAX_RETRY_DELAY_SECONDS}."
            )
        checked_delays.append(value)

    codes = raw["retryable_failure_codes"]
    if not isinstance(codes, list):
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: field 'retryable_failure_codes' must be a list."
        )
    if any(not isinstance(code, str) or not code.strip() for code in codes):
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: field 'retryable_failure_codes' must "
            "contain non-empty stable codes."
        )
    if len(codes) != len(set(codes)):
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: field 'retryable_failure_codes' must be unique."
        )
    for code in codes:
        if code not in FAILURE_REGISTRY:
            raise GovernancePolicyConfigError(
                f"{_POLICY_NAME} policy: unknown retryable failure code '{code}'."
            )
        if not is_registry_retryable(code):
            raise GovernancePolicyConfigError(
                f"{_POLICY_NAME} policy: failure code '{code}' is not retryable."
            )

    return {
        "policy_id": validated_policy_id(
            _POLICY_NAME, "policy_id", raw["policy_id"]
        ),
        "policy_version": validated_policy_id(
            _POLICY_NAME, "policy_version", policy_version
        ),
        "max_retry_attempts": maximum,
        "retry_delays_seconds": checked_delays,
        "retryable_failure_codes": list(codes),
    }


def _bounded_int(
    values: dict[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = validated_int(_POLICY_NAME, key, values[key], minimum=minimum)
    if value > maximum:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: field '{key}' must be <= {maximum}, got {value}."
        )
    return value


POLICY = load_policy()


def retry_allowed(code: Any, policy: dict[str, Any]) -> bool:
    """Both the closed registry and checked policy must permit a retry."""
    return is_registry_retryable(code) and code in policy[
        "retryable_failure_codes"
    ]


def retry_delay_seconds(policy: dict[str, Any], retry_count: int) -> int:
    """Return the deterministic delay for a 1-based scheduled retry."""
    if (
        isinstance(retry_count, bool)
        or not isinstance(retry_count, int)
        or retry_count < 1
        or retry_count > policy["max_retry_attempts"]
    ):
        raise ValueError("retry_count is outside the configured retry budget")
    return policy["retry_delays_seconds"][retry_count - 1]


def utc_moment(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("now must be timezone-aware")
    return moment.astimezone(timezone.utc)


def retry_state(
    job: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """Return ``not_applicable|waiting|due|exhausted|terminal``."""
    policy = policy or POLICY
    status = job.get("status")
    if status == "retry_wait":
        value = job.get("next_attempt_at")
        if not isinstance(value, str):
            raise ValueError("next_attempt_at is required for retry_wait")
        text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        try:
            due = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("next_attempt_at is invalid") from exc
        if due.tzinfo is None or due.tzinfo.utcoffset(due) is None:
            raise ValueError("next_attempt_at must be timezone-aware")
        return "due" if utc_moment(now) >= due.astimezone(timezone.utc) else "waiting"
    if status == "failed":
        if job.get("failure_code") in {
            "detection_job_retries_exhausted",
            "detection_job_execution_budget_exhausted",
        }:
            return "exhausted"
        return "terminal"
    if status in {"succeeded"}:
        return "terminal"
    return "not_applicable"
