"""Streaming leak tests: no draft content may leave stream_query before the
governance decision.

The fake graph stands in for Bedrock only; buffering, marker handling,
grounding validation, risk scoring, decision routing, queue writes, and event
emission are all the real code. Nothing monkeypatches score_risk or the
thresholds.
"""

import json
from types import SimpleNamespace

import agent
from governance import review_queue

_SECRET = "XYZZY-UNRELEASED-DRAFT-MARKER"

_TOOL_CONTENT = (
    "[Source 1: docs/acme-10k.pdf, page 2]\n"
    "Total revenue was $284.7 million, an increase of 18 percent."
)

# Ungrounded draft carrying the secret phrase: ghost citation + numbers absent
# from the evidence, so the real scorer holds it via the grounding floor.
_HELD_DRAFT = (
    "## Result Summary\n\n"
    f"Internal Corpus Answer: Available. {_SECRET} Revenue grew 47% to "
    "$512 million and remediation costs hit $63 million [ghost-report.pdf p.9].\n\n"
    "External Context: Unavailable."
)

# Grounded draft: cited, numerically supported -> decision "returned".
_RETURNED_DRAFT = (
    "## Result Summary\n\n"
    "Internal Corpus Answer: Available. Total revenue was $284.7 million "
    "[acme-10k.pdf p.2].\n\n"
    "External Context: Unavailable."
)

_BLOCK_MESSAGE = "This request was blocked by the ReAct-RAG safety policy."


def _tool_message():
    return SimpleNamespace(
        type="tool", name="local_search", content=_TOOL_CONTENT, tool_calls=[]
    )


class _FakeStreamingGraph:
    """Emits token chunks ("messages" mode) then the final trace ("updates")."""

    def __init__(self, chunk_texts, final_answer):
        self._chunk_texts = chunk_texts
        self._final_answer = final_answer

    def stream(self, payload, config=None, stream_mode=None):
        for text in self._chunk_texts:
            yield "messages", (SimpleNamespace(content=text), {})
        yield "updates", {
            "agent": {
                "messages": [
                    _tool_message(),
                    SimpleNamespace(
                        type="ai", content=self._final_answer, tool_calls=[]
                    ),
                ]
            }
        }


def _run_stream(monkeypatch, tmp_path, chunk_texts, final_answer, audit_writer=None):
    monkeypatch.setattr(
        agent, "build_agent", lambda **kw: _FakeStreamingGraph(chunk_texts, final_answer)
    )
    monkeypatch.setattr(
        agent,
        "write_audit_record",
        audit_writer or (lambda record, *a, **k: record["audit_id"]),
    )
    monkeypatch.setattr(agent.config, "REVIEW_QUEUE_DIR", str(tmp_path))
    monkeypatch.setattr(agent.config, "HUMAN_REVIEW_HOLD", True)
    return list(agent.stream_query("What was revenue growth?"))


def _events_of(events, event_type):
    return [event for event in events if event.get("type") == event_type]


def test_held_stream_never_emits_the_draft(monkeypatch, tmp_path):
    """No event of a held response contains the draft; the queue keeps it."""
    # The draft streams in several chunks, planning prose first — the exact
    # shape that leaked token events before the buffering change.
    events = _run_stream(
        monkeypatch,
        tmp_path,
        chunk_texts=[
            "Let me search the corpus for revenue figures. ",
            _HELD_DRAFT[:60],
            _HELD_DRAFT[60:],
        ],
        final_answer=_HELD_DRAFT,
    )

    # The secret phrase appears in no event of any type, serialized or raw.
    assert _SECRET not in json.dumps(events)
    # No token events exist at all: content is released once, post-decision.
    assert _events_of(events, "token") == []

    replaces = _events_of(events, "replace")
    assert len(replaces) == 1
    assert "held for human review" in replaces[0]["content"]
    assert "Review ID: review_" in replaces[0]["content"]

    # Held responses send no evidence chunks; the reviewer gets them instead.
    assert _events_of(events, "sources")[0]["sources"] == []

    # Governance metadata still streams.
    report = _events_of(events, "governance_report")[0]["report"]
    assert report["decision"] == "held_for_review"
    assert "grounding_below_review_floor" in report["risk"]["riskReasons"]
    assert _events_of(events, "audit_id")[0]["audit_id"]
    assert events[-1] == {"type": "done"}

    # The draft — secret included — is preserved for the reviewer.
    pending = review_queue.list_pending(tmp_path)
    assert len(pending) == 1
    assert _SECRET in pending[0]["draftAnswer"]


def test_blocked_stream_does_not_leak_partial_draft(monkeypatch, tmp_path):
    """Partial prohibited text that streamed before an intervention never leaves.

    Simulates a guardrail intervening mid-answer: draft chunks (marker already
    seen) carry prohibited content, then the final message is Bedrock's block
    text. Previously the partial draft had already left as token events.
    """
    events = _run_stream(
        monkeypatch,
        tmp_path,
        chunk_texts=[
            f"## Result Summary\n\nInternal Corpus Answer: Available. {_SECRET} ",
        ],
        final_answer=_BLOCK_MESSAGE,
    )

    assert _SECRET not in json.dumps(events)
    assert _events_of(events, "token") == []

    replaces = _events_of(events, "replace")
    assert len(replaces) == 1
    assert replaces[0]["content"] == _BLOCK_MESSAGE

    assert _events_of(events, "sources")[0]["sources"] == []
    report = _events_of(events, "governance_report")[0]["report"]
    assert report["decision"] == "blocked"
    # Blocked answers are never enqueued.
    assert review_queue.list_pending(tmp_path) == []


def test_returned_stream_still_delivers_the_answer(monkeypatch, tmp_path):
    """A clean answer reaches the client in full, with sources and metadata."""
    events = _run_stream(
        monkeypatch,
        tmp_path,
        chunk_texts=[_RETURNED_DRAFT[:40], _RETURNED_DRAFT[40:]],
        final_answer=_RETURNED_DRAFT,
    )

    replaces = _events_of(events, "replace")
    assert len(replaces) == 1
    assert replaces[0]["content"] == _RETURNED_DRAFT

    sources = _events_of(events, "sources")[0]["sources"]
    assert len(sources) == 1
    assert sources[0]["source_name"] == "acme-10k.pdf"

    report = _events_of(events, "governance_report")[0]["report"]
    assert report["decision"] == "returned"
    assert events[-1] == {"type": "done"}
    assert review_queue.list_pending(tmp_path) == []


def test_audit_write_failure_warning_is_sanitized(monkeypatch, tmp_path, caplog):
    """An OSError from the audit write keeps its warn-and-return semantics but
    the warning event carries a stable code, never the raw path-bearing text."""
    import logging

    secret_path = "/Users/wenhaohe/private/audit-secret-dir/query_audit.jsonl"

    def failing_writer(record, *args, **kwargs):
        raise OSError(f"[Errno 13] Permission denied: '{secret_path}'")

    with caplog.at_level(logging.ERROR, logger="agent"):
        events = _run_stream(
            monkeypatch,
            tmp_path,
            chunk_texts=[_RETURNED_DRAFT],
            final_answer=_RETURNED_DRAFT,
            audit_writer=failing_writer,
        )

    # The answer still returns (unchanged semantics) and a warning is emitted.
    assert _events_of(events, "replace")[0]["content"] == _RETURNED_DRAFT
    warnings = _events_of(events, "warning")
    assert warnings == [{"type": "warning", "message": "audit_record_write_failed"}]
    # The raw OSError text never reaches any event; it lands in the server log.
    assert secret_path not in json.dumps(events)
    assert secret_path in caplog.text


def test_enqueue_failure_warning_is_sanitized_and_hold_still_applies(
    monkeypatch, tmp_path, caplog
):
    """An OSError from the queue write still withholds the draft (the safer
    default) and warns with a stable code instead of the raw path."""
    import logging

    secret_path = "/Users/wenhaohe/private/queue-secret-dir/pending.jsonl"

    def failing_enqueue(item, queue_dir):
        raise OSError(f"[Errno 13] Permission denied: '{secret_path}'")

    monkeypatch.setattr(agent.review_queue, "enqueue", failing_enqueue)

    with caplog.at_level(logging.ERROR, logger="agent"):
        events = _run_stream(
            monkeypatch,
            tmp_path,
            chunk_texts=[_HELD_DRAFT],
            final_answer=_HELD_DRAFT,
        )

    # The draft is still withheld even though the queue write failed.
    replaces = _events_of(events, "replace")
    assert "held for human review" in replaces[0]["content"]
    assert _SECRET not in json.dumps(events)
    warnings = _events_of(events, "warning")
    assert warnings == [{"type": "warning", "message": "review_queue_write_failed"}]
    assert secret_path not in json.dumps(events)
    assert secret_path in caplog.text


def test_stream_event_order_releases_content_only_after_validation(monkeypatch, tmp_path):
    """Statuses stream first; replace is the single release point after the
    validation status; the metadata tail follows in the stable order."""
    events = _run_stream(
        monkeypatch,
        tmp_path,
        chunk_texts=[_RETURNED_DRAFT],
        final_answer=_RETURNED_DRAFT,
    )

    types = [event["type"] for event in events]
    # Everything before the replace is a status event — no content-bearing
    # event precedes the governance decision.
    replace_index = types.index("replace")
    assert set(types[:replace_index]) == {"status"}
    validating = [
        i for i, event in enumerate(events)
        if event["type"] == "status" and "Validating" in event["message"]
    ]
    assert validating and validating[0] < replace_index
    assert types[replace_index:] == [
        "replace", "sources", "audit_id", "governance_report", "done",
    ]
