"""Tests for the .json / .jsonl format handler."""

from __future__ import annotations

import json

import pytest

from ingest import load_documents, split_documents
from loaders.json_doc import _load


@pytest.fixture
def sample_json(tmp_path):
    path = tmp_path / "api-export-controls.json"
    payload = {
        "company": "Example Corp",
        "data": {
            "controls": [
                {"id": "C-001", "name": "Access Review", "owner": "Compliance"},
                {"id": "C-002", "name": "Change Management", "owner": "Eng"},
            ]
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def sample_jsonl(tmp_path):
    path = tmp_path / "audit-trail-2026.jsonl"
    lines = [
        {"event": "login", "user": "alice", "ts": "2026-05-01T09:00:00Z"},
        {"event": "approve", "user": "bob", "ts": "2026-05-01T09:05:00Z"},
        {"event": "logout", "user": "alice", "ts": "2026-05-01T09:10:00Z"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in lines), encoding="utf-8")
    return path


def test_json_load_emits_one_document_per_leaf_record_with_key_path(sample_json):
    docs = _load(str(sample_json))
    # Two control records under data.controls[*]; the top-level "company"
    # scalar lives on its own leaf path.
    paths = sorted(doc.metadata["json_key_path"] for doc in docs)
    assert "data.controls[0]" in paths
    assert "data.controls[1]" in paths
    record_doc = next(d for d in docs if d.metadata["json_key_path"] == "data.controls[0]")
    assert "id: C-001" in record_doc.page_content
    assert "name: Access Review" in record_doc.page_content
    assert record_doc.metadata["section_title"] == "data.controls[0]"


def test_jsonl_load_emits_one_document_per_line_with_record_index(sample_jsonl):
    docs = _load(str(sample_jsonl))
    assert len(docs) == 3
    indices = [doc.metadata["jsonl_record_index"] for doc in docs]
    assert indices == [1, 2, 3]
    assert "event: login" in docs[0].page_content
    assert "user: alice" in docs[0].page_content
    assert docs[0].metadata["section_title"] == "Record 1"


def test_json_routed_through_pipeline_and_classified(tmp_path, sample_json):
    docs = load_documents(str(tmp_path))
    assert docs
    chunks = split_documents(docs)
    assert chunks
    # api-export-controls.json triggers the PR3 api_export pattern.
    filing_types = {chunk.metadata.get("filing_type") for chunk in chunks}
    assert "api_export" in filing_types
