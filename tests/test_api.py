"""Tests for the FastAPI chat surface and the review queue routes."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api
import config
from governance import review_queue
from loaders.registry import supported_extensions


def test_chat_response_exposes_sources_and_audit_id(monkeypatch):
    """The chat API should return retrieved metadata for the answer view."""
    def fake_query(question, chat_history=None):
        return {
            "output": "Answer with citations.",
            "sources": [
                {
                    "rank": 1,
                    "source": "/repo/docs/policy.md",
                    "source_name": "policy.md",
                    "source_path": "policy.md",
                    "section_title": "Preclearance",
                    "page": 3,
                    "excerpt": "Policy evidence.",
                }
            ],
            "audit_id": "audit-123",
        }

    monkeypatch.setattr(api, "query", fake_query)
    client = TestClient(api.app)

    response = client.post(
        "/api/chat",
        json={"message": "What is the policy?", "history": []},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["answer"] == "Answer with citations."
    assert payload["audit_id"] == "audit-123"
    assert payload["sources"][0]["source_name"] == "policy.md"
    assert payload["sources"][0]["page"] == 3
    assert payload["sources"][0]["section_title"] == "Preclearance"
    # The absolute `source` path stays server-side, as in the review API.
    assert "source" not in payload["sources"][0]


def test_chat_stream_returns_ndjson_events(monkeypatch):
    """The streaming chat API should return incremental JSON events."""
    def fake_stream_query(question, chat_history=None):
        assert question == "What is the policy?"
        assert chat_history == []
        yield {"type": "status", "message": "Searching local documents..."}
        yield {"type": "token", "content": "Answer"}
        yield {
            "type": "sources",
            "sources": [
                {
                    "rank": 1,
                    "source": "/repo/docs/policy.md",
                    "source_name": "policy.md",
                    "source_path": "policy.md",
                    "page": 3,
                    "excerpt": "Policy evidence.",
                }
            ],
        }
        yield {"type": "audit_id", "audit_id": "audit-123"}
        yield {"type": "done"}

    monkeypatch.setattr(api, "stream_query", fake_stream_query)
    client = TestClient(api.app)

    response = client.post(
        "/api/chat/stream",
        json={"message": "What is the policy?", "history": []},
    )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.strip().splitlines()]
    assert events[0]["type"] == "status"
    assert events[1] == {"type": "token", "content": "Answer"}
    assert events[2]["sources"][0]["source_name"] == "policy.md"
    # The stream re-shapes raw agent source dicts onto the chat allowlist.
    assert "source" not in events[2]["sources"][0]
    assert events[-1]["type"] == "done"


_SAMPLE_REPORT = {
    "auditId": "audit-123",
    "model": "claude-haiku-4.5",
    "promptPolicyId": "regulated_doc_agent_v1",
    "contextPolicyId": "regulated_doc_agent_v1",
    "sourceUsage": {
        "internalSourcesUsed": 2,
        "externalSourcesUsed": 0,
        "documentVersionsUsed": 1,
        "expiredDocumentsUsed": 0,
    },
    "validation": {
        "citationCoverage": 1.0,
        "groundingScore": 1.0,
        "unsupportedClaims": 0,
        "guardrailOutcome": "passed",
        "piiDetected": False,
    },
    "risk": {"riskScore": 0.0, "riskLevel": "low", "humanReviewRequired": False},
    "decision": "returned",
}


def test_chat_response_includes_governance_report(monkeypatch):
    """The chat API should surface the per-answer governance report."""
    def fake_query(question, chat_history=None):
        return {
            "output": "Answer with citations.",
            "sources": [],
            "audit_id": "audit-123",
            "governance_report": _SAMPLE_REPORT,
        }

    monkeypatch.setattr(api, "query", fake_query)
    client = TestClient(api.app)

    response = client.post(
        "/api/chat",
        json={"message": "What is the policy?", "history": []},
    )

    assert response.status_code == 200
    report = response.json()["governance_report"]
    assert report["decision"] == "returned"
    assert report["risk"]["riskLevel"] == "low"
    assert report["validation"]["groundingScore"] == 1.0


def test_chat_response_includes_context_policy_section(monkeypatch):
    """The chat API should surface the contextPolicy section in the report payload."""
    report = {
        **_SAMPLE_REPORT,
        "contextPolicyId": "regulated_doc_agent_v1",
        "contextPolicy": {
            "id": "regulated_doc_agent_v1",
            "selectedChunks": 2,
            "droppedChunks": 1,
            "dropReasons": ["stale_document_version"],
            "internalTokens": 120,
            "externalTokens": 0,
            "totalPromptTokens": 120,
        },
    }

    def fake_query(question, chat_history=None):
        return {
            "output": "Answer with citations.",
            "sources": [],
            "audit_id": "audit-123",
            "governance_report": report,
        }

    monkeypatch.setattr(api, "query", fake_query)
    client = TestClient(api.app)

    response = client.post(
        "/api/chat",
        json={"message": "What is the policy?", "history": []},
    )

    assert response.status_code == 200
    context_policy = response.json()["governance_report"]["contextPolicy"]
    assert context_policy["id"] == "regulated_doc_agent_v1"
    assert context_policy["selectedChunks"] == 2
    assert context_policy["droppedChunks"] == 1
    assert context_policy["dropReasons"] == ["stale_document_version"]
    assert context_policy["totalPromptTokens"] == 120


def test_chat_stream_emits_governance_report_event(monkeypatch):
    """The streaming API should emit a final governance_report event."""
    def fake_stream_query(question, chat_history=None):
        yield {"type": "status", "message": "Searching local documents..."}
        yield {"type": "token", "content": "Answer"}
        yield {"type": "governance_report", "report": _SAMPLE_REPORT}
        yield {"type": "done"}

    monkeypatch.setattr(api, "stream_query", fake_stream_query)
    client = TestClient(api.app)

    response = client.post(
        "/api/chat/stream",
        json={"message": "What is the policy?", "history": []},
    )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.strip().splitlines()]
    governance_events = [e for e in events if e["type"] == "governance_report"]
    assert len(governance_events) == 1
    assert governance_events[0]["report"]["decision"] == "returned"


def test_history_drops_blocked_turns_so_denied_query_does_not_poison_guardrail():
    """A blocked turn must not be replayed to the model.

    Bedrock's guardrail scans history, so a denied question left in history blocks
    every later turn. The refused question and its block message are dropped; the
    later good turn survives.
    """
    history = [
        api.ChatMessage(role="user", content="Should I buy Acme stock?"),
        api.ChatMessage(
            role="assistant",
            content=(
                "This request was blocked by the ReAct-RAG safety policy. "
                "See policies/guardrails-policy.md for the full rule set."
            ),
        ),
        api.ChatMessage(role="user", content="What does the policy say about blackout periods?"),
        api.ChatMessage(role="assistant", content="## Result Summary\nBlackout periods [policy.md]."),
    ]

    cleaned = api._to_agent_history(history)
    contents = [content for _, content in cleaned]

    assert not any("buy Acme stock" in c for c in contents)
    assert not any("blocked by the ReAct-RAG safety policy" in c for c in contents)
    assert any("blackout periods" in c for c in contents)
    assert all(role in ("human", "ai") for role, _ in cleaned)


def test_supported_doc_suffixes_track_loader_registry():
    """Regression: sidebar must list every format the ingestion registry supports.

    PR2 self-test caught a missed coupling — ingest.py supported new formats
    but api.py was hard-coded to {.pdf, .md, .txt}, so the React sidebar never
    surfaced the new documents. Drive the suffix set from the registry to
    enforce single-source-of-truth.
    """
    assert api.SUPPORTED_DOC_SUFFIXES == set(supported_extensions())
    # Sanity: PR2 formats must be visible to the API.
    for required in (".pdf", ".md", ".txt", ".docx", ".xlsx", ".html", ".csv"):
        assert required in api.SUPPORTED_DOC_SUFFIXES, required


def test_documents_endpoint_lists_every_supported_format(tmp_path, monkeypatch):
    """``GET /api/documents`` returns at least one entry per registered suffix
    when the docs dir contains a sample of each."""
    monkeypatch.setattr(api, "DOCS_DIR", tmp_path)

    samples = {
        ".pdf": b"%PDF-1.4\n%fake\n",
        ".md": b"# heading\n",
        ".txt": b"plain text\n",
        ".docx": b"PK\x03\x04stub",
        ".xlsx": b"PK\x03\x04stub",
        ".html": b"<html><body>hi</body></html>",
        ".csv": b"a,b\n1,2\n",
    }
    for suffix, blob in samples.items():
        (tmp_path / f"sample{suffix}").write_bytes(blob)

    client = TestClient(api.app)
    response = client.get("/api/documents")
    assert response.status_code == 200
    listed_suffixes = {f".{doc['file_type']}" for doc in response.json()}
    for suffix in samples:
        assert suffix in listed_suffixes, f"missing {suffix} in API listing"


# --- Review queue routes -----------------------------------------------------


@pytest.fixture
def review_env(tmp_path, monkeypatch):
    """Point the API at temporary queue, audit, and corpus locations.

    Keeps every review test away from the committed review_queue/ files and the
    real audit log.
    """
    queue_dir = tmp_path / "queue"
    audit_log = tmp_path / "audit" / "query_audit.jsonl"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "policy.md").write_text("# Policy\nPreclearance is required.\n")
    monkeypatch.setattr(config, "REVIEW_QUEUE_DIR", str(queue_dir))
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", str(audit_log))
    monkeypatch.setattr(api, "DOCS_DIR", docs_dir)
    return SimpleNamespace(queue_dir=queue_dir, audit_log=audit_log, docs_dir=docs_dir)


def _review_item(review_id="review_audit-1", **overrides):
    """A stored queue item shaped like agent._build_review_item output."""
    item = {
        "reviewId": review_id,
        "auditId": review_id.removeprefix("review_"),
        "question": "What does the policy say?",
        "draftAnswer": "Preclearance is required [policy.md].",
        "riskScore": 0.86,
        "riskLevel": "high",
        "riskReasons": ["grounding_score_below_target"],
        "retrievedSources": [
            {
                "rank": 1,
                "source": "/Users/someone/Desktop/ReAct-RAG/docs/policy.md",
                "source_name": "policy.md",
                "source_path": "policy.md",
                "page": 2,
                "excerpt": "Preclearance is required.",
                "section_title": "Preclearance",
            },
            {
                "rank": 2,
                "source": "/Users/someone/Desktop/ReAct-RAG/docs/gone.md",
                "source_name": "gone.md",
                "source_path": "gone.md",
                "page": None,
                "excerpt": "Removed from the corpus.",
            },
        ],
        "decision": "held_for_review",
        "reviewStatus": "pending",
        "createdAt": "2026-06-01T00:00:00+00:00",
        "wasWithheld": True,
    }
    item.update(overrides)
    return item


def test_reviews_list_defaults_to_pending_oldest_first(review_env):
    """GET /api/reviews defaults to pending and sorts oldest first by createdAt."""
    review_queue.enqueue(
        _review_item("review_b", createdAt="2026-06-02T00:00:00+00:00"),
        review_env.queue_dir,
    )
    review_queue.enqueue(
        _review_item("review_a", createdAt="2026-06-01T00:00:00+00:00"),
        review_env.queue_dir,
    )
    review_queue.enqueue(
        _review_item("review_z", createdAt="2026-06-03T00:00:00+00:00"),
        review_env.queue_dir,
    )
    review_queue.approve("review_z", review_env.queue_dir)

    client = TestClient(api.app)
    response = client.get("/api/reviews")

    assert response.status_code == 200
    payload = response.json()
    assert [item["reviewId"] for item in payload] == ["review_a", "review_b"]
    assert all(item["reviewStatus"] == "pending" for item in payload)
    # Summary rows never carry the draft or its source excerpts.
    assert "draftAnswer" not in payload[0]
    assert "retrievedSources" not in payload[0]


def test_reviews_list_terminal_desc_and_all_block_order(review_env):
    """Terminal lists sort most recently reviewed first; all = pending then terminal."""
    for review_id, created in (
        ("review_a", "2026-06-01T00:00:00+00:00"),
        ("review_b", "2026-06-02T00:00:00+00:00"),
        ("review_c", "2026-06-03T00:00:00+00:00"),
        ("review_d", "2026-06-04T00:00:00+00:00"),
    ):
        review_queue.enqueue(_review_item(review_id, createdAt=created), review_env.queue_dir)
    review_queue.approve("review_a", review_env.queue_dir)
    review_queue.approve("review_b", review_env.queue_dir)
    review_queue.reject("review_d", review_env.queue_dir)

    client = TestClient(api.app)
    approved = client.get("/api/reviews", params={"status": "approved"}).json()
    assert [item["reviewId"] for item in approved] == ["review_b", "review_a"]

    rejected = client.get("/api/reviews", params={"status": "rejected"}).json()
    assert [item["reviewId"] for item in rejected] == ["review_d"]

    everything = client.get("/api/reviews", params={"status": "all"}).json()
    assert [item["reviewId"] for item in everything] == [
        "review_c",
        "review_d",
        "review_b",
        "review_a",
    ]
    assert [item["reviewStatus"] for item in everything] == [
        "pending",
        "rejected",
        "approved",
        "approved",
    ]


def test_reviews_list_empty_queue_and_invalid_status(review_env):
    """Every status lists [] on an empty queue; an unknown status is a 422."""
    client = TestClient(api.app)
    for status in ("pending", "approved", "rejected", "all"):
        response = client.get("/api/reviews", params={"status": status})
        assert response.status_code == 200
        assert response.json() == []

    assert client.get("/api/reviews", params={"status": "bogus"}).status_code == 422


def test_review_detail_maps_sources_onto_allowlist(review_env):
    """Detail returns camelCase allowlisted sources and drops the absolute path."""
    review_queue.enqueue(_review_item("review_a"), review_env.queue_dir)

    client = TestClient(api.app)
    response = client.get("/api/reviews/review_a")

    assert response.status_code == 200
    payload = response.json()
    assert payload["reviewId"] == "review_a"
    assert payload["auditId"] == "a"
    assert payload["reviewStatus"] == "pending"
    assert payload["decision"] == "held_for_review"
    assert payload["draftAnswer"].startswith("Preclearance")
    assert payload["reviewedAt"] is None
    assert payload["reviewerNote"] is None

    first, second = payload["retrievedSources"]
    assert first == {
        "rank": 1,
        "sourceName": "policy.md",
        "sourcePath": "policy.md",
        "sectionTitle": "Preclearance",
        "page": 2,
        "excerpt": "Preclearance is required.",
        "documentUrl": "/api/documents/policy.md",
    }
    # page=null serializes cleanly; a file gone from the corpus gets no URL.
    assert second["page"] is None
    assert second["documentUrl"] is None
    assert second["sectionTitle"] is None
    assert "source" not in first and "source" not in second


def test_review_detail_finds_terminal_items_across_statuses(review_env):
    """Detail works for approved/rejected items, not just pending ones."""
    review_queue.enqueue(_review_item("review_a"), review_env.queue_dir)
    review_queue.reject("review_a", review_env.queue_dir, note="ungrounded")

    client = TestClient(api.app)
    response = client.get("/api/reviews/review_a")

    assert response.status_code == 200
    payload = response.json()
    assert payload["reviewStatus"] == "rejected"
    assert payload["reviewerNote"] == "ungrounded"
    assert payload["reviewedAt"]


def test_review_detail_absent_everywhere_is_404(review_env):
    """An id missing from all three files is a 404."""
    review_queue.enqueue(_review_item("review_a"), review_env.queue_dir)
    review_queue.approve("review_a", review_env.queue_dir)

    client = TestClient(api.app)
    assert client.get("/api/reviews/review_missing").status_code == 404


def test_approve_pending_item_returns_final_state(review_env):
    """POST approve resolves the item, stamps reviewedAt, and empties pending."""
    review_queue.enqueue(_review_item("review_a"), review_env.queue_dir)

    client = TestClient(api.app)
    response = client.post("/api/reviews/review_a/approve", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["reviewStatus"] == "approved"
    assert payload["reviewedAt"]
    assert payload["reviewerNote"] is None

    assert review_queue.list_pending(review_env.queue_dir) == []
    _, status = review_queue.get_any("review_a", review_env.queue_dir)
    assert status == "approved"


def test_reject_pending_item_persists_note(review_env):
    """POST reject stores the reviewer note; a malformed body is a 422 no-op."""
    review_queue.enqueue(_review_item("review_a"), review_env.queue_dir)

    client = TestClient(api.app)
    assert client.post("/api/reviews/review_a/reject", json={"note": 5}).status_code == 422
    assert review_queue.get_any("review_a", review_env.queue_dir)[1] == "pending"

    response = client.post(
        "/api/reviews/review_a/reject", json={"note": "numbers unsupported"}
    )
    assert response.status_code == 200
    assert response.json()["reviewStatus"] == "rejected"
    assert response.json()["reviewerNote"] == "numbers unsupported"

    item, status = review_queue.get_any("review_a", review_env.queue_dir)
    assert status == "rejected"
    assert item["reviewerNote"] == "numbers unsupported"


def test_mutations_on_terminal_items_409_and_absent_404(review_env):
    """Approve/reject on a resolved item is a 409; on an unknown id a 404."""
    review_queue.enqueue(_review_item("review_a"), review_env.queue_dir)
    review_queue.approve("review_a", review_env.queue_dir)

    client = TestClient(api.app)
    assert client.post("/api/reviews/review_a/approve", json={}).status_code == 409
    assert client.post("/api/reviews/review_a/reject", json={}).status_code == 409
    assert client.post("/api/reviews/review_missing/approve", json={}).status_code == 404

    # The conflicting calls did not touch the resolved item.
    _, status = review_queue.get_any("review_a", review_env.queue_dir)
    assert status == "approved"


def _walk_strings(node, path="$"):
    """Yield (json_path, value) for every string in a decoded JSON tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def test_no_absolute_paths_anywhere_in_review_responses(review_env):
    """No string in any review payload is a local path; only documentUrl starts with /."""
    review_queue.enqueue(_review_item("review_a"), review_env.queue_dir)
    review_queue.enqueue(
        _review_item("review_b", createdAt="2026-06-02T00:00:00+00:00"),
        review_env.queue_dir,
    )
    review_queue.approve("review_b", review_env.queue_dir)

    client = TestClient(api.app)
    payloads = [
        client.get("/api/reviews", params={"status": "all"}).json(),
        client.get("/api/reviews/review_a").json(),
        client.get("/api/reviews/review_b").json(),
    ]

    for json_path, value in _walk_strings(payloads):
        assert "/Users" not in value, f"{json_path} leaks a local path: {value!r}"
        if value.startswith("/"):
            assert json_path.endswith(".documentUrl"), f"{json_path} = {value!r}"
            assert value.startswith("/api/")


def test_audit_join_returns_only_governance_report(review_env):
    """The detail join exposes governance_report and nothing else from the record."""
    report = {
        "auditId": "audit-1",
        "decision": "held_for_review",
        "risk": {"riskScore": 0.86, "riskLevel": "high", "humanReviewRequired": True},
    }
    audit_record = {
        "audit_id": "audit-1",
        "timestamp": "2026-06-01T00:00:00+00:00",
        "query": "What does the policy say?",
        "answer": "the raw audit answer",
        "retrieved_sources": [{"source": "/Users/someone/docs/secret.pdf"}],
        "response_trace": [{"type": "tool", "content": "RAW_TOOL_TRACE"}],
        "governance_report": report,
    }
    review_env.audit_log.parent.mkdir(parents=True, exist_ok=True)
    review_env.audit_log.write_text(json.dumps(audit_record) + "\n")
    review_queue.enqueue(
        _review_item("review_audit-1", retrievedSources=[]), review_env.queue_dir
    )

    client = TestClient(api.app)
    response = client.get("/api/reviews/review_audit-1")

    assert response.status_code == 200
    assert response.json()["governanceReport"] == report
    for leaked in (
        "retrieved_sources",
        "response_trace",
        "RAW_TOOL_TRACE",
        "the raw audit answer",
        "/Users",
    ):
        assert leaked not in response.text, leaked


def test_audit_join_missing_is_null_and_still_200(review_env):
    """No log file, then no matching record: both leave governanceReport null."""
    review_queue.enqueue(_review_item("review_a"), review_env.queue_dir)

    client = TestClient(api.app)
    response = client.get("/api/reviews/review_a")
    assert response.status_code == 200
    assert response.json()["governanceReport"] is None

    review_env.audit_log.parent.mkdir(parents=True, exist_ok=True)
    review_env.audit_log.write_text(
        json.dumps({"audit_id": "other", "governance_report": {"decision": "returned"}})
        + "\n"
    )
    response = client.get("/api/reviews/review_a")
    assert response.status_code == 200
    assert response.json()["governanceReport"] is None


def test_was_withheld_reads_null_for_legacy_items(review_env):
    """Items written before wasWithheld read as null; new items keep their value."""
    legacy = _review_item("review_legacy")
    legacy.pop("wasWithheld")
    review_queue.enqueue(legacy, review_env.queue_dir)
    review_queue.enqueue(
        _review_item(
            "review_new", createdAt="2026-06-02T00:00:00+00:00", wasWithheld=False
        ),
        review_env.queue_dir,
    )

    client = TestClient(api.app)
    listed = {item["reviewId"]: item for item in client.get("/api/reviews").json()}
    assert listed["review_legacy"]["wasWithheld"] is None
    assert listed["review_new"]["wasWithheld"] is False

    detail = client.get("/api/reviews/review_legacy").json()
    assert detail["wasWithheld"] is None


def test_queue_reads_go_through_review_queue_module(review_env, monkeypatch):
    """Routes read the queue only via review_queue.list_items / get_any."""
    calls = {}
    item = _review_item("review_a")

    def fake_list_items(queue_dir, status):
        calls["list_items"] = (str(queue_dir), status)
        return [(item, "pending")]

    def fake_get_any(review_id, queue_dir):
        calls["get_any"] = (review_id, str(queue_dir))
        return (item, "pending")

    monkeypatch.setattr(api.review_queue, "list_items", fake_list_items)
    monkeypatch.setattr(api.review_queue, "get_any", fake_get_any)

    client = TestClient(api.app)
    listed = client.get("/api/reviews", params={"status": "all"})
    assert [entry["reviewId"] for entry in listed.json()] == ["review_a"]
    assert calls["list_items"] == (str(config.REVIEW_QUEUE_DIR), "all")

    detail = client.get("/api/reviews/review_a")
    assert detail.json()["reviewId"] == "review_a"
    assert calls["get_any"] == ("review_a", str(config.REVIEW_QUEUE_DIR))
