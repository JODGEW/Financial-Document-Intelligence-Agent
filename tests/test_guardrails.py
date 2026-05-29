"""Tests for Bedrock Guardrails wiring."""

from types import SimpleNamespace

from agent import detect_guardrail_intervention
from eval_runner import EvalCase, ExpectedSource, evaluate_case
from scripts.eval_diff import diff_reports
from scripts.setup_guardrail import (
    BLOCKED_INPUT_MESSAGE,
    BLOCKED_OUTPUT_MESSAGE,
    DENIED_TOPICS,
    PII_ENTITIES,
)


def test_detect_intervention_via_blocked_message():
    text = "This request was blocked by the ReAct-RAG safety policy. See policies/..."
    assert detect_guardrail_intervention(text, []) == "blocked"


def test_detect_intervention_via_response_metadata_stop_reason():
    blocked_message = SimpleNamespace(
        type="ai",
        content="some text",
        response_metadata={"stop_reason": "guardrail_intervened"},
    )
    assert detect_guardrail_intervention("some text", [blocked_message]) == "blocked"


def test_detect_returns_none_for_normal_response():
    text = "Acme reported revenue of $284.7 million in fiscal 2025."
    assert detect_guardrail_intervention(text, []) is None


def test_detect_handles_empty_inputs():
    assert detect_guardrail_intervention("", []) is None
    assert detect_guardrail_intervention(None, []) is None


def test_evaluate_case_guardrail_block_workflow():
    case = EvalCase(
        id="denied_topic",
        question="Should I buy NVDA?",
        workflow_type="guardrail_block",
        expected_tools=[],
        expected_sources=[],
        expected_terms=[],
        expected_guardrail_outcome="blocked",
    )

    def fake_query(_question):
        return {
            "output": BLOCKED_OUTPUT_MESSAGE,
            "sources": [],
            "messages": [],
            "guardrail_outcome": "blocked",
        }

    result = evaluate_case(case, run_query=fake_query, retrieve=lambda _q: [])

    assert result.guardrail_outcome == "blocked"
    assert result.guardrail_outcome_correct is True
    assert result.grounded_answer is True
    assert result.unsupported_claim_rate is None
    assert result.citation_accuracy is None
    assert result.local_refusal_correct is None


def test_evaluate_case_guardrail_block_fails_when_not_blocked():
    case = EvalCase(
        id="denied_topic",
        question="Should I buy NVDA?",
        workflow_type="guardrail_block",
        expected_tools=[],
        expected_sources=[],
        expected_terms=[],
        expected_guardrail_outcome="blocked",
    )

    def fake_query(_question):
        # Agent answered the denied question instead of refusing — guardrail miss.
        return {
            "output": "You should buy NVDA because...",
            "sources": [],
            "messages": [],
            "guardrail_outcome": None,
        }

    result = evaluate_case(case, run_query=fake_query, retrieve=lambda _q: [])

    assert result.guardrail_outcome is None
    assert result.guardrail_outcome_correct is False
    assert result.grounded_answer is False


def test_diff_reports_no_drift_when_identical():
    snapshot = {
        "summary": {
            "retrieval_hit_rate": 1.0,
            "grounded_answer_rate": 0.8,
            "latency_by_workflow_type": {"local_only": 6.7},
        },
        "results": [{"case_id": "a", "grounded_answer": True}],
    }
    assert diff_reports(snapshot, snapshot) == []


def test_diff_reports_flags_metric_move():
    base = {
        "summary": {"grounded_answer_rate": 0.8, "latency_by_workflow_type": {}},
        "results": [],
    }
    current = {
        "summary": {"grounded_answer_rate": 0.6, "latency_by_workflow_type": {}},
        "results": [],
    }
    lines = diff_reports(base, current)
    assert any("grounded_answer_rate" in l for l in lines)


def test_diff_reports_flags_case_flip_and_latency():
    base = {
        "summary": {
            "grounded_answer_rate": 0.8,
            "latency_by_workflow_type": {"local_only": 5.0},
        },
        "results": [{"case_id": "acme_revenue", "grounded_answer": True}],
    }
    current = {
        "summary": {
            "grounded_answer_rate": 0.8,
            "latency_by_workflow_type": {"local_only": 8.0},
        },
        "results": [{"case_id": "acme_revenue", "grounded_answer": False}],
    }
    lines = diff_reports(base, current)
    assert any("acme_revenue" in l for l in lines)
    assert any("local_only" in l for l in lines)


def test_setup_guardrail_constants_match_policy_doc():
    """The script's denied topics and PII categories must match the policy doc."""
    topic_names = {t["name"] for t in DENIED_TOPICS}
    assert topic_names == {
        "personalized_investment_advice",
        "legal_opinion",
    }
    assert all(t["type"] == "DENY" for t in DENIED_TOPICS)
    # All definitions must fit Bedrock's ~200-char cap with margin.
    assert all(len(t["definition"]) <= 200 for t in DENIED_TOPICS)

    pii_types = {p["type"] for p in PII_ENTITIES}
    assert pii_types == {
        "EMAIL",
        "PHONE",
        "US_SOCIAL_SECURITY_NUMBER",
        "CREDIT_DEBIT_CARD_NUMBER",
        "ADDRESS",
    }

    assert "ReAct-RAG safety policy" in BLOCKED_INPUT_MESSAGE
    assert "ReAct-RAG safety policy" in BLOCKED_OUTPUT_MESSAGE
