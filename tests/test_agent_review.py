"""Tests for the human review hold/flag path in agent._finalize_query_result.

The held state is produced by REAL runtime signals: an ungrounded draft (ghost
citation + numbers absent from the retrieved evidence) scored by the real
grounding validator and risk scorer under the shipped policy. Nothing here
monkeypatches score_risk or touches the review thresholds.
"""

from types import SimpleNamespace

import agent
from governance import review_queue

# Evidence actually "retrieved": one real-format local_search block.
_TOOL_CONTENT = (
    "[Source 1: docs/acme-10k.pdf, page 2]\n"
    "Total revenue was $284.7 million, an increase of 18 percent."
)

# Ungrounded draft: cites a file that was never retrieved and states numbers
# that appear nowhere in the evidence. Real scoring: citation coverage 0.0,
# every numeric claim unsupported, grounding 0.0 < the 0.50 review floor.
_UNGROUNDED_ANSWER = (
    "## Result Summary\n\n"
    "Internal Corpus Answer: Available. Revenue grew 47% to $512 million and "
    "remediation costs hit $63 million [ghost-report.pdf p.9].\n\n"
    "External Context: Unavailable."
)

# Grounded control: cites the retrieved file/page, numbers match the evidence.
_GROUNDED_ANSWER = (
    "## Result Summary\n\n"
    "Internal Corpus Answer: Available. Total revenue was $284.7 million "
    "[acme-10k.pdf p.2].\n\n"
    "External Context: Unavailable."
)


def _tool_message():
    return SimpleNamespace(
        type="tool", name="local_search", content=_TOOL_CONTENT, tool_calls=[]
    )


def _isolate_io(monkeypatch, tmp_path):
    """Isolate audit + queue I/O to tmp_path. The scorer is NOT touched."""
    monkeypatch.setattr(
        agent, "write_audit_record", lambda record, *a, **k: record["audit_id"]
    )
    monkeypatch.setattr(agent.config, "REVIEW_QUEUE_DIR", str(tmp_path))


def _finalize(question: str, output: str):
    return agent._finalize_query_result(
        question=question,
        output=output,
        result_messages=[_tool_message()],
        trace_messages=[_tool_message()],
        guardrail_outcome=None,
    )


def test_finalize_holds_answer_and_enqueues_draft(monkeypatch, tmp_path):
    """HOLD=true: the returned answer is the notice; the draft is queued + auditable."""
    _isolate_io(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.config, "HUMAN_REVIEW_HOLD", True)

    result = _finalize("What was revenue growth?", _UNGROUNDED_ANSWER)

    report = result["governance_report"]
    assert report["decision"] == "held_for_review"
    assert report["risk"]["humanReviewRequired"] is True
    # The hold came from the explicit floor gate, not a manipulated score: the
    # weighted score stays below the 0.75 threshold.
    assert report["risk"]["riskScore"] < 0.75
    assert "grounding_below_review_floor" in report["risk"]["riskReasons"]
    # User-facing answer is the held notice, not the draft.
    assert result["output"] != _UNGROUNDED_ANSWER
    assert "held for human review" in result["output"]
    assert "Review ID: review_" in result["output"]
    # A withheld answer sends no evidence chunks; the reviewer gets them.
    assert result["sources"] == []

    pending = review_queue.list_pending(tmp_path)
    assert len(pending) == 1
    item = pending[0]
    assert item["draftAnswer"] == _UNGROUNDED_ANSWER  # the real answer is preserved
    assert item["reviewId"] == f"review_{result['audit_id']}"
    # The queue item carries the same reason codes as the governance report.
    assert item["riskReasons"] == report["risk"]["riskReasons"]
    assert item["reviewStatus"] == "pending"


def test_finalize_flag_mode_returns_answer_but_still_enqueues(monkeypatch, tmp_path):
    """HOLD=false: the answer returns unchanged and the item is still enqueued."""
    _isolate_io(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.config, "HUMAN_REVIEW_HOLD", False)

    result = _finalize("What was revenue growth?", _UNGROUNDED_ANSWER)

    assert result["governance_report"]["decision"] == "held_for_review"
    # Flag mode: the user still sees the real answer, sources included.
    assert result["output"] == _UNGROUNDED_ANSWER
    assert len(result["sources"]) == 1

    pending = review_queue.list_pending(tmp_path)
    assert len(pending) == 1
    assert pending[0]["draftAnswer"] == _UNGROUNDED_ANSWER


def test_review_item_snapshots_hold_mode_as_was_withheld(monkeypatch, tmp_path):
    """Each queued item records the hold/flag mode in effect when it was created."""
    _isolate_io(monkeypatch, tmp_path)

    monkeypatch.setattr(agent.config, "HUMAN_REVIEW_HOLD", True)
    _finalize("held in hold mode", _UNGROUNDED_ANSWER)
    monkeypatch.setattr(agent.config, "HUMAN_REVIEW_HOLD", False)
    _finalize("held in flag mode", _UNGROUNDED_ANSWER)

    pending = review_queue.list_pending(tmp_path)
    assert [item["wasWithheld"] for item in pending] == [True, False]


def test_grounded_answer_is_returned_and_not_enqueued(monkeypatch, tmp_path):
    """A cited, numerically supported answer returns normally under the same policy."""
    _isolate_io(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.config, "HUMAN_REVIEW_HOLD", True)

    result = _finalize("What was total revenue?", _GROUNDED_ANSWER)

    report = result["governance_report"]
    assert report["decision"] == "returned"
    assert report["risk"]["humanReviewRequired"] is False
    assert result["output"] == _GROUNDED_ANSWER
    assert review_queue.list_pending(tmp_path) == []


# The profile that regressed in live testing: internal answer unavailable,
# external web context answering instead, with a .pdf token inside a web URL
# that parses as an unmatched citation against irrelevant retrieved chunks.
_FALLBACK_ANSWER = (
    "## Result Summary\n\n"
    "Internal Corpus Answer: unavailable in current local corpus.\n\n"
    "External Context: available.\n\n"
    "- [ir.example.com](https://ir.example.com/sec/xyz-20251231-gen.pdf) | "
    "2026 | XYZ 2025 10-K | Risk factors include recalls affecting 47% of units "
    "and $512 million in remediation costs.\n"
)


def test_fallback_answer_with_unavailable_internal_is_not_held(monkeypatch, tmp_path):
    """A refusal/web-fallback answer returns normally despite artifact grounding.

    Retrieval returned (irrelevant) local chunks, the answer declared the
    internal corpus answer unavailable, and the web bullet's .pdf URL parses as
    an unmatched citation — grounding is near 0.0, but the floor must not judge
    an answer with no internal claims.
    """
    _isolate_io(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.config, "HUMAN_REVIEW_HOLD", True)

    result = _finalize("What are XYZ's risk factors?", _FALLBACK_ANSWER)

    report = result["governance_report"]
    assert report["decision"] != "held_for_review"
    assert report["risk"]["humanReviewRequired"] is False
    assert "grounding_below_review_floor" not in report["risk"]["riskReasons"]
    assert result["output"] == _FALLBACK_ANSWER
    assert review_queue.list_pending(tmp_path) == []


class _FakeGraph:
    """Stands in for the Bedrock LangGraph; everything downstream is real."""

    def __init__(self, answer: str):
        self._answer = answer

    def invoke(self, payload, config=None):
        return {
            "messages": [
                _tool_message(),
                SimpleNamespace(type="ai", content=self._answer, tool_calls=[]),
            ]
        }


def test_ungrounded_answer_reaches_pending_review_through_query(monkeypatch, tmp_path):
    """End to end through query(): real validator, real scorer, real routing, real
    queue write — only the model call is faked. No score_risk monkeypatch, no
    threshold change."""
    _isolate_io(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.config, "HUMAN_REVIEW_HOLD", True)
    monkeypatch.setattr(agent, "build_agent", lambda **kw: _FakeGraph(_UNGROUNDED_ANSWER))

    result = agent.query("What was revenue growth?")

    report = result["governance_report"]
    assert report["decision"] == "held_for_review"
    assert "grounding_below_review_floor" in report["risk"]["riskReasons"]
    assert "held for human review" in result["output"]

    pending = review_queue.list_pending(tmp_path)
    assert len(pending) == 1
    assert pending[0]["draftAnswer"] == _UNGROUNDED_ANSWER
    assert pending[0]["riskReasons"] == report["risk"]["riskReasons"]
