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


def test_low_grounding_alone_forces_review_via_floor_without_touching_score():
    """Grounding 0.0 stays medium on the weighted score but trips the floor gate."""
    result = score(grounding_score=0.0, guardrail_outcome="passed", external_context_used=False)
    # The weighted score and level are untouched by the mandatory gate.
    assert result["risk_score"] == 0.5
    assert result["risk_level"] == "medium"
    # The mandatory floor forces review with its explicit reason code.
    assert result["human_review_required"] is True
    assert "grounding_below_review_floor" in result["risk_reasons"]
    assert "grounding_score_below_target" in result["risk_reasons"]
    # The weighted gate did not fire: 0.5 < 0.75.
    assert "risk_score_at_or_above_review_threshold" not in result["risk_reasons"]


def test_combined_signals_require_human_review():
    """Low grounding + anonymized guardrail + external context crosses 0.75."""
    result = score(grounding_score=0.0, guardrail_outcome="anonymized", external_context_used=True)
    # 0.5 (grounding) + 0.3*0.5 (anonymized) + 0.2 (external) = 0.85
    assert result["risk_score"] == 0.85
    assert result["risk_level"] == "high"
    assert result["human_review_required"] is True
    # Both gates fire here: the weighted threshold and the grounding floor.
    assert set(result["risk_reasons"]) == {
        "grounding_score_below_target",
        "pii_anonymized",
        "external_context_used",
        "risk_score_at_or_above_review_threshold",
        "grounding_below_review_floor",
    }


def test_weighted_gate_boundary_at_exactly_require_review_at_or_above():
    """The weighted gate fires at exactly 0.75 (>=) and not just below it."""
    # 0.5*(1-0.2) + 0.3*0.5 + 0.2 = 0.40 + 0.15 + 0.20 = exactly 0.75.
    at_threshold = score(
        grounding_score=0.2, guardrail_outcome="anonymized", external_context_used=True
    )
    assert at_threshold["risk_score"] == 0.75
    assert at_threshold["risk_level"] == "high"
    assert at_threshold["human_review_required"] is True
    assert "risk_score_at_or_above_review_threshold" in at_threshold["risk_reasons"]

    # 0.5*(1-0.21) + 0.15 + 0.20 = 0.745 < 0.75: the weighted gate does NOT
    # fire, and the reason list proves which gate held the answer (the floor,
    # because grounding 0.21 < 0.50).
    below_threshold = score(
        grounding_score=0.21, guardrail_outcome="anonymized", external_context_used=True
    )
    assert below_threshold["risk_score"] == 0.745
    assert below_threshold["risk_level"] == "medium"
    assert "risk_score_at_or_above_review_threshold" not in below_threshold["risk_reasons"]
    assert "grounding_below_review_floor" in below_threshold["risk_reasons"]
    assert below_threshold["human_review_required"] is True


def test_grounding_floor_boundary_is_strictly_below():
    """The floor fires strictly below 0.50; exactly 0.50 returns normally."""
    just_below = score(
        grounding_score=0.4999, guardrail_outcome="passed", external_context_used=False
    )
    assert just_below["human_review_required"] is True
    assert "grounding_below_review_floor" in just_below["risk_reasons"]

    at_floor = score(
        grounding_score=0.5, guardrail_outcome="passed", external_context_used=True
    )
    assert at_floor["risk_score"] == 0.45
    assert at_floor["risk_level"] == "low"
    assert at_floor["human_review_required"] is False
    assert "grounding_below_review_floor" not in at_floor["risk_reasons"]
    assert "risk_score_at_or_above_review_threshold" not in at_floor["risk_reasons"]


def test_grounding_floor_does_not_judge_unavailable_internal_answers():
    """A refusal/web-fallback answer is exempt from the floor.

    Its grounding numbers are artifacts (a ``.pdf`` token inside a web URL
    parses as an unmatched citation against irrelevant retrieved chunks), so
    even grounding 0.0 must not hold it when the internal answer was declared
    unavailable. The weighted gate still applies unchanged.
    """
    fallback = score(
        grounding_score=0.0,
        guardrail_outcome="passed",
        external_context_used=True,
        internal_answer_available=False,
    )
    assert fallback["human_review_required"] is False
    assert "grounding_below_review_floor" not in fallback["risk_reasons"]
    # The weighted score is unaffected by the exemption.
    assert fallback["risk_score"] == 0.7


def test_policy_loads_from_yaml_and_falls_back_to_defaults(tmp_path):
    """Weights/thresholds load from YAML; a missing file falls back to defaults."""
    # The shipped policy file drives the module-level constants.
    assert risk_scorer.THRESHOLDS["require_review_at_or_above"] == 0.75
    assert risk_scorer.THRESHOLDS["require_review_below_grounding"] == 0.50
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
