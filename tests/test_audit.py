"""Tests for audit metadata extraction and query logging."""

import json
from types import SimpleNamespace

import config
from audit import (
    build_audit_record,
    build_response_trace,
    extract_retrieved_sources,
    parse_local_search_sources,
    write_audit_record,
)


def test_parse_local_search_sources_exposes_chunk_metadata(monkeypatch, tmp_path):
    """Retrieved chunks should expose source, page, path, and excerpt metadata."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    source_file = docs_dir / "policy.md"
    source_file.write_text("policy text")
    monkeypatch.setattr(config, "DOCS_DIR", str(docs_dir))

    output = (
        f"[Source 1: {source_file}, page 2]\n"
        "Employees may not trade during blackout periods.\n\n---\n\n"
        f"[Source 2: {source_file}]\n"
        "All transactions require preclearance."
    )

    sources = parse_local_search_sources(output)

    assert sources[0]["rank"] == 1
    assert sources[0]["source_name"] == "policy.md"
    assert sources[0]["source_path"] == "policy.md"
    assert sources[0]["page"] == 2
    assert "blackout periods" in sources[0]["excerpt"]
    assert sources[1]["page"] is None


def test_parse_local_search_sources_tolerates_enriched_metadata(monkeypatch, tmp_path):
    """Additional company/type/year header fields should not break parsing."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    source_file = docs_dir / "acme.pdf"
    source_file.write_text("placeholder")
    monkeypatch.setattr(config, "DOCS_DIR", str(docs_dir))

    output = (
        f"[Source 1: {source_file}, page 2, section: ITEM 1A. RISK FACTORS, "
        "company: Acme Corporation, type: 10-k, year: 2025]\n"
        "Cybersecurity risk evidence."
    )

    sources = parse_local_search_sources(output)

    assert sources[0]["source_name"] == "acme.pdf"
    assert sources[0]["page"] == 2
    assert sources[0]["section_title"] == "ITEM 1A. RISK FACTORS"
    assert "Cybersecurity risk evidence" in sources[0]["excerpt"]


def test_extract_retrieved_sources_from_response_trace(monkeypatch, tmp_path):
    """Tool traces should yield retrieved chunk metadata for API/UI display."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    source_file = docs_dir / "research.txt"
    source_file.write_text("research text")
    monkeypatch.setattr(config, "DOCS_DIR", str(docs_dir))

    messages = [
        SimpleNamespace(
            type="ai",
            content="",
            tool_calls=[
                {"id": "call-1", "name": "local_search", "args": {"query": "risk"}}
            ],
        ),
        SimpleNamespace(
            type="tool",
            name=None,
            tool_call_id="call-1",
            content=f"[Source 1: {source_file}, page 4]\nCybersecurity risk disclosure.",
        ),
    ]

    sources = extract_retrieved_sources(messages)

    assert len(sources) == 1
    assert sources[0]["source_name"] == "research.txt"
    assert sources[0]["page"] == 4


def test_extract_retrieved_sources_deduplicates_repeated_tool_results(monkeypatch, tmp_path):
    """Repeated searches should not show duplicate chunks in the answer view."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    source_file = docs_dir / "research.txt"
    source_file.write_text("research text")
    monkeypatch.setattr(config, "DOCS_DIR", str(docs_dir))
    tool_content = f"[Source 5: {source_file}, page 4]\nCybersecurity risk disclosure."

    messages = [
        SimpleNamespace(
            type="ai",
            content="",
            tool_calls=[
                {"id": "call-1", "name": "local_search", "args": {"query": "risk"}},
                {"id": "call-2", "name": "local_search", "args": {"query": "cyber"}},
            ],
        ),
        SimpleNamespace(
            type="tool",
            name=None,
            tool_call_id="call-1",
            content=tool_content,
        ),
        SimpleNamespace(
            type="tool",
            name=None,
            tool_call_id="call-2",
            content=tool_content.replace("[Source 5:", "[Source 1:"),
        ),
    ]

    sources = extract_retrieved_sources(messages)

    assert len(sources) == 1
    assert sources[0]["rank"] == 1
    assert sources[0]["source_name"] == "research.txt"


def test_audit_log_contains_query_sources_and_response_trace(tmp_path):
    """Audit logs should persist the query, retrieved sources, and trace."""
    messages = [
        SimpleNamespace(type="human", content="What are the risks?"),
        SimpleNamespace(
            type="ai",
            content="",
            tool_calls=[
                {"id": "call-1", "name": "local_search", "args": {"query": "risks"}}
            ],
        ),
        SimpleNamespace(
            type="tool",
            name="local_search",
            tool_call_id="call-1",
            content="[Source 1: docs/risk.txt, page 1]\nRisk evidence.",
        ),
        SimpleNamespace(type="ai", content="Risks include ...", tool_calls=[]),
    ]
    retrieved_sources = extract_retrieved_sources(messages)
    record = build_audit_record(
        query="What are the risks?",
        answer="Risks include ...",
        messages=messages,
        retrieved_sources=retrieved_sources,
    )

    log_path = tmp_path / "query_audit.jsonl"
    audit_id = write_audit_record(record, log_path)
    logged = json.loads(log_path.read_text().strip())

    assert logged["audit_id"] == audit_id
    assert logged["query"] == "What are the risks?"
    assert logged["retrieved_sources"][0]["source_name"] == "risk.txt"
    assert any(
        entry.get("tool_name") == "local_search"
        for entry in logged["response_trace"]
    )


def test_audit_record_persists_nested_governance_report(tmp_path):
    """A governance_report attached to the record round-trips as nested JSON.

    The field is optional: records written before it existed (without the key)
    stay valid, so old log consumers do not break.
    """
    record = build_audit_record(
        query="What does the policy require?",
        answer="Preclearance is required [policy.md].",
        messages=[SimpleNamespace(type="ai", content="Preclearance is required.", tool_calls=[])],
        retrieved_sources=[],
    )
    # Backward compatibility: a record is valid before the field is attached.
    assert "governance_report" not in record

    record["governance_report"] = {
        "auditId": record["audit_id"],
        "validation": {"groundingScore": 1.0, "citationCoverage": 1.0},
        "risk": {"riskLevel": "low", "humanReviewRequired": False},
        "decision": "returned",
    }

    log_path = tmp_path / "query_audit.jsonl"
    write_audit_record(record, log_path)
    logged = json.loads(log_path.read_text().strip())

    assert logged["governance_report"]["decision"] == "returned"
    assert logged["governance_report"]["validation"]["groundingScore"] == 1.0
    assert logged["audit_id"] == logged["governance_report"]["auditId"]


def test_response_trace_supports_dict_tool_messages():
    """Synthetic fallback tool traces should be serialized for audit logs."""
    trace = build_response_trace(
        [
            {
                "type": "tool",
                "name": "web_search",
                "content": "- [sec.gov](https://www.sec.gov/) | SEC filing",
            }
        ]
    )

    assert trace == [
        {
            "type": "tool",
            "content": "- [sec.gov](https://www.sec.gov/) | SEC filing",
            "tool_name": "web_search",
        }
    ]
