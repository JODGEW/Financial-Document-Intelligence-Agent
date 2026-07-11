"""Tests for the human review hold/flag path in agent._finalize_query_result."""

import agent
from governance import review_queue

_HELD_RISK = {
    "risk_score": 0.86,
    "risk_level": "high",
    "risk_reasons": ["grounding_score_below_target", "external_context_used"],
    "human_review_required": True,
}

_ANSWER = (
    "## Result Summary\n\n"
    "Internal Corpus Answer: Available. Preclearance is required [policy.md].\n\n"
    "External Context: Unavailable."
)


def _force_held(monkeypatch, tmp_path):
    """Force humanReviewRequired and isolate audit + queue I/O to tmp_path."""
    monkeypatch.setattr(agent, "score_risk", lambda *a, **k: dict(_HELD_RISK))
    monkeypatch.setattr(
        agent, "write_audit_record", lambda record, *a, **k: record["audit_id"]
    )
    monkeypatch.setattr(agent.config, "REVIEW_QUEUE_DIR", str(tmp_path))


def test_finalize_holds_answer_and_enqueues_draft(monkeypatch, tmp_path):
    """HOLD=true: the returned answer is the notice; the draft is queued + auditable."""
    _force_held(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.config, "HUMAN_REVIEW_HOLD", True)

    result = agent._finalize_query_result(
        question="What does the policy require?",
        output=_ANSWER,
        result_messages=[],
        trace_messages=[],
        guardrail_outcome=None,
    )

    assert result["governance_report"]["decision"] == "held_for_review"
    # User-facing answer is the held notice, not the draft.
    assert result["output"] != _ANSWER
    assert "held for human review" in result["output"]
    assert "Review ID: review_" in result["output"]

    pending = review_queue.list_pending(tmp_path)
    assert len(pending) == 1
    item = pending[0]
    assert item["draftAnswer"] == _ANSWER  # the real answer is preserved
    assert item["reviewId"] == f"review_{result['audit_id']}"
    assert item["riskReasons"] == _HELD_RISK["risk_reasons"]
    assert item["reviewStatus"] == "pending"


def test_finalize_flag_mode_returns_answer_but_still_enqueues(monkeypatch, tmp_path):
    """HOLD=false: the answer returns unchanged and the item is still enqueued."""
    _force_held(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.config, "HUMAN_REVIEW_HOLD", False)

    result = agent._finalize_query_result(
        question="What does the policy require?",
        output=_ANSWER,
        result_messages=[],
        trace_messages=[],
        guardrail_outcome=None,
    )

    assert result["governance_report"]["decision"] == "held_for_review"
    # Flag mode: the user still sees the real answer.
    assert result["output"] == _ANSWER

    pending = review_queue.list_pending(tmp_path)
    assert len(pending) == 1
    assert pending[0]["draftAnswer"] == _ANSWER


def test_review_item_snapshots_hold_mode_as_was_withheld(monkeypatch, tmp_path):
    """Each queued item records the hold/flag mode in effect when it was created."""
    _force_held(monkeypatch, tmp_path)

    monkeypatch.setattr(agent.config, "HUMAN_REVIEW_HOLD", True)
    agent._finalize_query_result(
        question="held in hold mode",
        output=_ANSWER,
        result_messages=[],
        trace_messages=[],
        guardrail_outcome=None,
    )
    monkeypatch.setattr(agent.config, "HUMAN_REVIEW_HOLD", False)
    agent._finalize_query_result(
        question="held in flag mode",
        output=_ANSWER,
        result_messages=[],
        trace_messages=[],
        guardrail_outcome=None,
    )

    pending = review_queue.list_pending(tmp_path)
    assert [item["wasWithheld"] for item in pending] == [True, False]
