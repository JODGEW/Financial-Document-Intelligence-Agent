"""Comparison-specific governance: risk policy, evaluation, review routing.

Evaluates a persisted, detected comparison.v1 result under the checked-in
comparison risk policy (policies/comparison_risk_policy.yaml) and persists an
IMMUTABLE governance evaluation plus — for held decisions only — exactly one
durable pending comparison-review record, both in SQLite via comparison_store.

What the score measures, stated plainly: output trust and review necessity of
the comparison's own deterministic validation — never financial materiality,
investment/legal risk, or the business importance of the detected changes
(change counts are deliberately not a signal). The generic chat risk scorer
(governance/risk_scorer.py) is unsuitable here and untouched: its signals
(grounding score, guardrail outcome, external-context use) describe a
model-generated chat answer, none of which exist for a deterministic
comparison result.

Decision vocabulary honesty (policy v1):
- returned            — no mandatory condition, score under the warn band.
- held_for_review     — any mandatory condition, or score >= hold threshold.
- returned_with_warning — supported vocabulary, honest-but-unreachable under
  the default policy (every score-raising condition also mandates a hold);
  reachable only when an operator disables mandatory rules, and tested that
  way rather than manufactured.
- blocked             — NOT produced by this policy. A stored result that no
  longer validates against comparison.v1 is an error (409-shaped), not a
  governance decision; nothing reviewable exists to block.

The detector result (risk=not_evaluated, review=not_required) is never
modified: the governed snapshot is a separate validated comparison.v1
document updating only its risk and review blocks, hashed deterministically
(the snapshot preserves the detector's created_at and producer).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import comparison_store
import config
from governance.comparison_schema import dump_comparison, load_comparison
from governance.policy_validation import (
    GovernancePolicyConfigError,
    load_policy_mapping,
    require_mapping_section,
    validated_bool,
    validated_number,
    validated_policy_id,
)

_POLICY_PATH = (
    Path(__file__).resolve().parent / "policies" / "comparison_risk_policy.yaml"
)
_POLICY_NAME = "comparison_risk"

# Baked-in defaults, used ONLY when the YAML file is absent. Kept identical to
# policies/comparison_risk_policy.yaml (documented governance convention).
_DEFAULT_IDENTITY = {"policy_id": "comparison_risk_v1", "policy_version": "1"}
_DEFAULT_WEIGHTS = {
    "failed_validation_rate_weight": 0.5,
    "undetermined_change_rate_weight": 0.3,
    "evidence_failure_rate_weight": 0.2,
}
_DEFAULT_THRESHOLDS = {"warn_at_or_above": 0.25, "hold_at_or_above": 0.50}
_DEFAULT_MANDATORY = {
    "hold_on_any_failed_check": True,
    "hold_on_any_undetermined_change": True,
    "hold_on_unexpected_not_run": True,
}

# Reason codes. One specific code per condition — no redundant umbrellas.
REASON_FAILED_CHECK_PREFIX = "failed_check_"  # + check name
REASON_UNDETERMINED_PRESENT = "undetermined_changes_present"
REASON_UNEXPECTED_NOT_RUN = "unexpected_not_run_check"
REASON_SCORE_AT_HOLD = "risk_score_at_or_above_hold_threshold"
REASON_SCORE_AT_WARN = "risk_score_at_or_above_warn_threshold"

_EVIDENCE_LAYER_CHECKS = ("evidence_presence", "citation_support")


class ComparisonGovernanceError(Exception):
    """Governance cannot evaluate this comparison. ``code`` is stable; the
    message is safe (no paths, SQL, or document content)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class GovernanceNotFound(ComparisonGovernanceError):
    """Unknown comparison or no detector result to govern (API: 404)."""


class GovernanceResultInvalid(ComparisonGovernanceError):
    """The stored result no longer validates or its hash mismatches (API: 409)."""


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the comparison risk policy.

    Missing file -> baked-in defaults (documented contract). Present invalid
    file -> GovernancePolicyConfigError at import, never a silent fallback.
    """
    raw = load_policy_mapping(Path(path or _POLICY_PATH), _POLICY_NAME)
    if raw is None:
        return {
            "policy_id": _DEFAULT_IDENTITY["policy_id"],
            "policy_version": _DEFAULT_IDENTITY["policy_version"],
            "weights": dict(_DEFAULT_WEIGHTS),
            "thresholds": dict(_DEFAULT_THRESHOLDS),
            "mandatory": dict(_DEFAULT_MANDATORY),
        }

    identity = require_mapping_section(raw, "comparison_risk_policy", _POLICY_NAME)
    weights_section = require_mapping_section(raw, "signal_weights", _POLICY_NAME)
    thresholds_section = require_mapping_section(raw, "risk_thresholds", _POLICY_NAME)
    mandatory_section = require_mapping_section(raw, "mandatory_review", _POLICY_NAME)
    if identity is None or weights_section is None or thresholds_section is None \
            or mandatory_section is None:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: all four sections are required "
            "('comparison_risk_policy', 'signal_weights', 'risk_thresholds', "
            "'mandatory_review'). Restore the documented mapping or delete "
            "the file to use the built-in defaults."
        )

    policy_id = validated_policy_id(
        _POLICY_NAME, "comparison_risk_policy.policy_id", identity.get("policy_id")
    )
    policy_version = identity.get("policy_version")
    if isinstance(policy_version, (int, float)) and not isinstance(policy_version, bool):
        policy_version = str(policy_version)
    policy_version = validated_policy_id(
        _POLICY_NAME, "comparison_risk_policy.policy_version", policy_version
    )

    weights = {}
    for key in _DEFAULT_WEIGHTS:
        if key not in weights_section:
            raise GovernancePolicyConfigError(
                f"{_POLICY_NAME} policy: 'signal_weights.{key}' is required."
            )
        weights[key] = validated_number(
            _POLICY_NAME, f"signal_weights.{key}", weights_section[key],
            minimum=0.0, maximum=1.0,
        )
    unknown_weights = set(weights_section) - set(_DEFAULT_WEIGHTS)
    if unknown_weights:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: unknown signal_weights keys "
            f"{sorted(unknown_weights)}."
        )
    weight_sum = sum(weights.values())
    if abs(weight_sum - 1.0) > 1e-9:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: signal weights must sum to 1.0, got "
            f"{weight_sum}."
        )

    thresholds = {}
    for key in _DEFAULT_THRESHOLDS:
        if key not in thresholds_section:
            raise GovernancePolicyConfigError(
                f"{_POLICY_NAME} policy: 'risk_thresholds.{key}' is required."
            )
        thresholds[key] = validated_number(
            _POLICY_NAME, f"risk_thresholds.{key}", thresholds_section[key],
            minimum=0.0, maximum=1.0,
        )
    if thresholds["warn_at_or_above"] > thresholds["hold_at_or_above"]:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: 'warn_at_or_above' must be <= "
            f"'hold_at_or_above', got {thresholds['warn_at_or_above']} > "
            f"{thresholds['hold_at_or_above']}."
        )

    mandatory = {}
    for key in _DEFAULT_MANDATORY:
        if key not in mandatory_section:
            raise GovernancePolicyConfigError(
                f"{_POLICY_NAME} policy: 'mandatory_review.{key}' is required."
            )
        mandatory[key] = validated_bool(
            _POLICY_NAME, f"mandatory_review.{key}", mandatory_section[key]
        )
    unknown_rules = set(mandatory_section) - set(_DEFAULT_MANDATORY)
    if unknown_rules:
        raise GovernancePolicyConfigError(
            f"{_POLICY_NAME} policy: unknown mandatory_review keys "
            f"{sorted(unknown_rules)}."
        )

    return {
        "policy_id": policy_id,
        "policy_version": policy_version,
        "weights": weights,
        "thresholds": thresholds,
        "mandatory": mandatory,
    }


POLICY = load_policy()


def _signals(result: dict[str, Any]) -> dict[str, float]:
    """The three deterministic 0..1 signals with their documented denominators."""
    changes = result.get("changes") or []
    passed = failed = 0
    for change in changes:
        for check in change.get("validation") or []:
            if check.get("status") == "passed":
                passed += 1
            elif check.get("status") == "failed":
                failed += 1
    applicable = passed + failed
    failed_validation_rate = failed / applicable if applicable else 0.0

    total_changes = len(changes)
    undetermined = sum(
        1 for change in changes if change.get("change_type") == "undetermined"
    )
    undetermined_change_rate = undetermined / total_changes if total_changes else 0.0

    evidence_failed_changes = sum(
        1
        for change in changes
        if any(
            check.get("check") in _EVIDENCE_LAYER_CHECKS
            and check.get("status") == "failed"
            for check in change.get("validation") or []
        )
    )
    evidence_failure_rate = (
        evidence_failed_changes / total_changes if total_changes else 0.0
    )
    return {
        "failed_validation_rate": failed_validation_rate,
        "undetermined_change_rate": undetermined_change_rate,
        "evidence_failure_rate": evidence_failure_rate,
    }


def evaluate(result: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic governance evaluation of one comparison.v1 result dict.

    Returns risk_score, risk_level, decision, and reason_codes. Pure function
    of (result, policy): no storage, no side effects.
    """
    policy = policy or POLICY
    weights, thresholds, mandatory = (
        policy["weights"], policy["thresholds"], policy["mandatory"]
    )
    signals = _signals(result)
    score = (
        weights["failed_validation_rate_weight"] * signals["failed_validation_rate"]
        + weights["undetermined_change_rate_weight"] * signals["undetermined_change_rate"]
        + weights["evidence_failure_rate_weight"] * signals["evidence_failure_rate"]
    )
    score = round(min(1.0, max(0.0, score)), 4)

    reasons: list[str] = []
    changes = result.get("changes") or []

    failed_check_names: list[str] = []
    not_run_present = False
    for change in changes:
        for check in change.get("validation") or []:
            if check.get("status") == "failed":
                name = str(check.get("check"))
                if name not in failed_check_names:
                    failed_check_names.append(name)
            elif check.get("status") == "not_run":
                not_run_present = True

    mandatory_hold = False
    if mandatory["hold_on_any_failed_check"] and failed_check_names:
        mandatory_hold = True
        reasons.extend(
            f"{REASON_FAILED_CHECK_PREFIX}{name}" for name in sorted(failed_check_names)
        )
    if mandatory["hold_on_any_undetermined_change"] and any(
        change.get("change_type") == "undetermined" for change in changes
    ):
        mandatory_hold = True
        reasons.append(REASON_UNDETERMINED_PRESENT)
    if mandatory["hold_on_unexpected_not_run"] and not_run_present:
        # All six checks are implemented as of item1a_detector.v2, so any
        # not_run in a governed result is unexpected by definition.
        mandatory_hold = True
        reasons.append(REASON_UNEXPECTED_NOT_RUN)

    weighted_hold = score >= thresholds["hold_at_or_above"]
    if weighted_hold:
        reasons.append(REASON_SCORE_AT_HOLD)

    if mandatory_hold or weighted_hold:
        decision = "held_for_review"
    elif score >= thresholds["warn_at_or_above"]:
        decision = "returned_with_warning"
        reasons.append(REASON_SCORE_AT_WARN)
    else:
        decision = "returned"

    if score >= thresholds["hold_at_or_above"]:
        risk_level = "high"
    elif score >= thresholds["warn_at_or_above"]:
        risk_level = "medium"
    else:
        risk_level = "low"

    return {
        "risk_score": score,
        "risk_level": risk_level,
        "decision": decision,
        "reason_codes": reasons,
        "signals": signals,
    }


def _evaluation_id(
    comparison_id: str, result_hash: str, policy_id: str, policy_version: str
) -> str:
    key = f"{comparison_id}|{result_hash}|{policy_id}|{policy_version}"
    return "gov_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _governed_snapshot(
    result: dict[str, Any], evaluation: dict[str, Any], review_id: str | None
) -> dict[str, Any]:
    """A validated comparison.v1 snapshot with ONLY risk and review updated.

    Everything else — schema version, filing references, changes, evidence,
    validation, created_at, producer — is preserved from the detector result,
    which keeps the governed hash deterministic for fixed inputs.
    """
    governed = json.loads(json.dumps(result))
    governed["risk"] = {
        "decision": evaluation["decision"],
        "reason_codes": list(evaluation["reason_codes"]),
        "risk_score": evaluation["risk_score"],
        "risk_level": evaluation["risk_level"],
    }
    governed["review"] = (
        {"status": "pending", "review_id": review_id}
        if evaluation["decision"] == "held_for_review"
        else {"status": "not_required", "review_id": None}
    )
    # Schema validation enforces the risk/review coherence invariants too.
    return dump_comparison(load_comparison(governed))


def _governed_hash(governed: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(governed, sort_keys=True).encode("utf-8")
    ).hexdigest()


def govern(
    comparison_id: str,
    *,
    db_path: str | Path | None = None,
    policy: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Evaluate a detected comparison and persist the immutable evaluation.

    Returns (evaluation_record, created). Idempotent over (comparison_id,
    result hash, policy id, policy version); a held decision creates exactly
    one pending comparison_review_items row in the same transaction. The
    detector result row is never modified.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    policy = policy or POLICY

    record = comparison_store.get_comparison(comparison_id, db_path=db_path)
    if record is None:
        raise GovernanceNotFound(
            "comparison_not_found", f"comparison {comparison_id!r} does not exist"
        )
    stored = comparison_store.get_result(comparison_id, db_path=db_path)
    if stored is None:
        raise GovernanceNotFound(
            "comparison_result_missing",
            "no detector result exists for this comparison; run detection first",
        )

    result = stored["result"]
    try:
        load_comparison(result)  # the stored document must still validate
    except Exception as exc:
        raise GovernanceResultInvalid(
            "comparison_result_invalid",
            "the stored detector result no longer validates against "
            "comparison.v1 and cannot be governed",
        ) from exc

    evaluation = evaluate(result, policy)
    evaluation_id = _evaluation_id(
        comparison_id, stored["result_hash"], policy["policy_id"], policy["policy_version"]
    )
    review_id = (
        "crev_" + evaluation_id[len("gov_"):]
        if evaluation["decision"] == "held_for_review"
        else None
    )
    governed = _governed_snapshot(result, evaluation, review_id)

    stored_record, created = comparison_store.record_evaluation(
        comparison_id=comparison_id,
        evaluation_id=evaluation_id,
        comparison_result_hash=stored["result_hash"],
        policy_id=policy["policy_id"],
        policy_version=policy["policy_version"],
        risk_score=evaluation["risk_score"],
        risk_level=evaluation["risk_level"],
        decision=evaluation["decision"],
        reason_codes=evaluation["reason_codes"],
        governed_result_json=json.dumps(governed),
        governed_result_hash=_governed_hash(governed),
        review_id=review_id,
        db_path=db_path,
    )
    return stored_record, created


def get_current_evaluation(
    comparison_id: str,
    *,
    db_path: str | Path | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The stored evaluation for the CURRENT policy id+version and the
    comparison's current result hash, or None. Older-policy evaluations stay
    readable through comparison_store.list_evaluations."""
    db_path = db_path or config.COMPARISON_DB_PATH
    policy = policy or POLICY
    stored = comparison_store.get_result(comparison_id, db_path=db_path)
    if stored is None:
        return None
    evaluation_id = _evaluation_id(
        comparison_id, stored["result_hash"], policy["policy_id"], policy["policy_version"]
    )
    return comparison_store.get_evaluation(evaluation_id, db_path=db_path)
