"""Tests for the FastAPI chat surface."""

import json

from fastapi.testclient import TestClient

import api
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
