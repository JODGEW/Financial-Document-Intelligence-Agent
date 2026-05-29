"""Risk scorer: combine governance signals into one score and decision level.

v1 (PR1) uses three signals: grounding score, guardrail outcome, and whether
external context was used. Thresholds and signal weights load from
``policies/risk_thresholds.yaml`` at import. The fuller §7.6 input set (PII,
retrieval confidence, document freshness, topic sensitivity) is deferred until
the human review queue can act on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "risk_thresholds.yaml"

# Used when the YAML is missing or unreadable, so the agent never crashes on a
# bad policy file. Kept identical to policies/risk_thresholds.yaml.
_DEFAULT_THRESHOLDS = {
    "auto_return_below": 0.50,
    "return_with_warning_below": 0.75,
    "require_review_at_or_above": 0.75,
}
_DEFAULT_WEIGHTS = {
    "grounding_score_weight": 0.5,
    "guardrail_outcome_weight": 0.3,
    "external_context_weight": 0.2,
}

# A grounding score at or below this reads as "below target" in risk reasons.
_GROUNDING_TARGET = 0.75

# Guardrail outcome to its 0..1 risk contribution.
_GUARDRAIL_RISK = {
    "blocked": 1.0,
    "anonymized": 0.5,
    "passed": 0.0,
}


def _load_policy(path: Path = _POLICY_PATH) -> tuple[dict[str, float], dict[str, float]]:
    """Load thresholds and weights from YAML, falling back to defaults."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return dict(_DEFAULT_THRESHOLDS), dict(_DEFAULT_WEIGHTS)

    thresholds = {**_DEFAULT_THRESHOLDS, **(raw.get("risk_thresholds") or {})}
    weights = {**_DEFAULT_WEIGHTS, **(raw.get("signal_weights") or {})}
    return thresholds, weights


THRESHOLDS, WEIGHTS = _load_policy()


def _guardrail_risk(guardrail_outcome: str | None) -> float:
    """Map a guardrail outcome to a 0..1 risk contribution."""
    if not guardrail_outcome:
        return _GUARDRAIL_RISK["passed"]
    return _GUARDRAIL_RISK.get(guardrail_outcome.lower(), 0.0)


def _risk_level(risk_score: float, thresholds: dict[str, float]) -> str:
    """Bucket a risk score into low / medium / high."""
    if risk_score >= thresholds["require_review_at_or_above"]:
        return "high"
    if risk_score >= thresholds["auto_return_below"]:
        return "medium"
    return "low"


def score(
    grounding_score: float,
    guardrail_outcome: str | None,
    external_context_used: bool,
    thresholds: dict[str, float] | None = None,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Combine the three v1 signals into a risk score, level, and reasons."""
    thresholds = thresholds or THRESHOLDS
    weights = weights or WEIGHTS

    grounding_risk = max(0.0, 1.0 - grounding_score)
    guardrail_risk = _guardrail_risk(guardrail_outcome)
    external_risk = 1.0 if external_context_used else 0.0

    risk_score = (
        weights["grounding_score_weight"] * grounding_risk
        + weights["guardrail_outcome_weight"] * guardrail_risk
        + weights["external_context_weight"] * external_risk
    )
    risk_score = round(min(1.0, max(0.0, risk_score)), 4)

    reasons: list[str] = []
    if grounding_score <= _GROUNDING_TARGET:
        reasons.append("grounding_score_below_target")
    if guardrail_outcome and guardrail_outcome.lower() == "blocked":
        reasons.append("guardrail_blocked")
    elif guardrail_outcome and guardrail_outcome.lower() == "anonymized":
        reasons.append("pii_anonymized")
    if external_context_used:
        reasons.append("external_context_used")

    risk_level = _risk_level(risk_score, thresholds)
    return {
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_reasons": reasons,
        "human_review_required": risk_score >= thresholds["require_review_at_or_above"],
    }
