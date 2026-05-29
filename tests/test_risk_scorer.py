"""Tests for the governance risk scorer."""

from pathlib import Path

from governance import risk_scorer
from governance.risk_scorer import _DEFAULT_THRESHOLDS, _DEFAULT_WEIGHTS, _load_policy, score


def test_low_risk_for_grounded_passed_local_answer():
    """A grounded answer, guardrail passed, no external context, scores low."""
    result = score(grounding_score=1.0, guardrail_outcome=None, external_context_used=False)
    assert result["risk_score"] == 0.0
    assert result["risk_level"] == "low"
    assert result["human_review_required"] is False
    assert result["risk_reasons"] == []


def test_low_grounding_alone_lands_in_medium_band():
    """Grounding 0.0 contributes 0.5 risk: medium, below the review threshold."""
    result = score(grounding_score=0.0, guardrail_outcome="passed", external_context_used=False)
    assert result["risk_score"] == 0.5
    assert result["risk_level"] == "medium"
    assert result["human_review_required"] is False
    assert "grounding_score_below_target" in result["risk_reasons"]


def test_combined_signals_require_human_review():
    """Low grounding + anonymized guardrail + external context crosses 0.75."""
    result = score(grounding_score=0.0, guardrail_outcome="anonymized", external_context_used=True)
    # 0.5 (grounding) + 0.3*0.5 (anonymized) + 0.2 (external) = 0.85
    assert result["risk_score"] == 0.85
    assert result["risk_level"] == "high"
    assert result["human_review_required"] is True
    assert set(result["risk_reasons"]) == {
        "grounding_score_below_target",
        "pii_anonymized",
        "external_context_used",
    }


def test_policy_loads_from_yaml_and_falls_back_to_defaults(tmp_path):
    """Weights/thresholds load from YAML; a missing file falls back to defaults."""
    # The shipped policy file drives the module-level constants.
    assert risk_scorer.THRESHOLDS["require_review_at_or_above"] == 0.75
    assert risk_scorer.WEIGHTS["grounding_score_weight"] == 0.5

    # Missing file -> defaults, no crash.
    thresholds, weights = _load_policy(tmp_path / "does_not_exist.yaml")
    assert thresholds == _DEFAULT_THRESHOLDS
    assert weights == _DEFAULT_WEIGHTS

    # Custom file -> overrides are honored.
    custom = tmp_path / "risk.yaml"
    custom.write_text(
        "risk_thresholds:\n"
        "  require_review_at_or_above: 0.60\n"
        "signal_weights:\n"
        "  external_context_weight: 0.4\n"
    )
    thresholds, weights = _load_policy(custom)
    assert thresholds["require_review_at_or_above"] == 0.60
    assert weights["external_context_weight"] == 0.4
    # Unspecified keys keep their defaults.
    assert weights["grounding_score_weight"] == 0.5
