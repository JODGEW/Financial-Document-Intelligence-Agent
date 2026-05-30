"""Tests for document change impact analysis (Governance_layer.md §8)."""

import json

import pytest
from langchain_core.embeddings import Embeddings

import scripts.document_impact as cli
from governance.impact import (
    build_impact_report,
    compute_new_chunks,
    diff_chunks,
    load_current_chunks,
    scan_audit_log,
)


class _FakeEmbeddings(Embeddings):
    """Deterministic, offline embeddings so the test Chroma needs no Bedrock.

    Similarity is irrelevant here: PR3 only ever calls Chroma's ``get`` with a
    metadata filter, never a vector search.
    """

    def embed_documents(self, texts):
        return [[float(len(t) % 5), 1.0, 2.0] for t in texts]

    def embed_query(self, text):
        return [float(len(text) % 5), 1.0, 2.0]


@pytest.fixture
def chroma(tmp_path):
    """A fresh, empty Chroma collection backed by fake embeddings."""
    from langchain_chroma import Chroma

    return Chroma(
        collection_name="impact_test",
        persist_directory=str(tmp_path / "chroma"),
        embedding_function=_FakeEmbeddings(),
    )


def _seed(chroma_client, chunks):
    """Add computed chunks to a Chroma collection under their content as docs."""
    texts, metadatas, ids = [], [], []
    for i, c in enumerate(chunks):
        texts.append(c["content"])
        # Chroma rejects None metadata values; keep only set scalars.
        metadatas.append({k: v for k, v in c["metadata"].items() if v is not None})
        ids.append(f"{c['source_name']}:{c['metadata'].get('page', '')}:{i}-{c['content_hash']}")
    chroma_client.add_texts(texts=texts, metadatas=metadatas, ids=ids)


_V1 = """# Personal Trading Policy

This policy governs employee trading.

## Blackout Periods

No covered person may trade two weeks before quarterly earnings.

## Penalties

Violations result in disgorgement of profits and trading suspension.
"""

_V2 = """# Personal Trading Policy

This policy governs employee trading.

## Blackout Periods

No covered person may trade FOUR weeks before quarterly earnings, a change from the prior window.

## Penalties

Violations result in disgorgement of profits and trading suspension.
"""


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_load_current_chunks_filters_by_document_id(chroma, tmp_path):
    """Only chunks whose metadata document_id matches are returned."""
    v1 = compute_new_chunks(_write(tmp_path, "policy.md", _V1))
    for c in v1:
        c["metadata"]["document_id"] = "policy"
    _seed(chroma, v1)
    # Seed a second, unrelated document.
    other = compute_new_chunks(_write(tmp_path, "other.md", "# Other\n\nUnrelated text.\n"))
    for c in other:
        c["metadata"]["document_id"] = "other"
    _seed(chroma, other)

    loaded = load_current_chunks("policy", chroma)
    assert loaded, "expected chunks for document_id 'policy'"
    assert {c["document_id"] for c in loaded} == {"policy"}
    assert len(loaded) == len(v1)


def test_compute_new_chunks_does_not_write_chroma(chroma, tmp_path):
    """compute_new_chunks is pure: the collection count is unchanged."""
    seed = compute_new_chunks(_write(tmp_path, "seed.md", _V1))
    _seed(chroma, seed)
    before = chroma._collection.count()

    produced = compute_new_chunks(_write(tmp_path, "v2.md", _V2))

    after = chroma._collection.count()
    assert before == after
    assert produced, "expected chunks to be produced in memory"


def test_diff_chunks_detects_added_removed_modified():
    """Diff categorizes by full content with position-based modified pairing."""
    old = [
        {"chunk_id": "doc.md::aaa", "content_hash": "aaa", "page": None, "section_title": "Intro"},
        {"chunk_id": "doc.md::bbb", "content_hash": "bbb", "page": None, "section_title": "Body"},
        {"chunk_id": "doc.md::ccc", "content_hash": "ccc", "page": None, "section_title": "Tail"},
    ]
    new = [
        {"chunk_id": "doc.md::aaa", "content_hash": "aaa", "page": None, "section_title": "Intro"},  # unchanged
        {"chunk_id": "doc.md::bbb2", "content_hash": "bbb2", "page": None, "section_title": "Body"},  # modified
        {"chunk_id": "doc.md::ddd", "content_hash": "ddd", "page": None, "section_title": "New"},  # added
    ]
    diff = diff_chunks(old, new)

    assert [c["content_hash"] for c in diff["removed"]] == ["ccc"]
    assert [c["content_hash"] for c in diff["added"]] == ["ddd"]
    assert len(diff["modified"]) == 1
    assert diff["modified"][0]["old"]["content_hash"] == "bbb"
    assert diff["modified"][0]["new"]["content_hash"] == "bbb2"


def test_scan_audit_log_finds_affected(tmp_path):
    """An audit record referencing an affected chunk is returned with usedOldChunk."""
    # Build a chunk and its audit-reproducible id from a known excerpt.
    from governance.impact import _chunk_id, _normalized_excerpt

    content = "Covered persons may not trade during the blackout window before earnings."
    excerpt = _normalized_excerpt(content)
    affected_id = _chunk_id("policy.md", None, excerpt)

    log = tmp_path / "audit.jsonl"
    log.write_text(
        json.dumps(
            {
                "audit_id": "audit_42",
                "timestamp": "2026-05-09T16:12:00Z",
                "query": "What does the policy require before trading?",
                "retrieved_sources": [
                    {"source_name": "policy.md", "page": None, "excerpt": excerpt}
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    matches = scan_audit_log(str(log), {affected_id})
    assert len(matches) == 1
    assert matches[0]["auditId"] == "audit_42"
    assert matches[0]["usedOldChunk"] == affected_id
    assert matches[0]["question"].startswith("What does the policy")


def test_scan_audit_log_no_match_returns_empty(tmp_path):
    """No audit entry references the affected chunks -> empty result."""
    log = tmp_path / "audit.jsonl"
    log.write_text(
        json.dumps(
            {
                "audit_id": "audit_99",
                "timestamp": "2026-05-09T00:00:00Z",
                "query": "unrelated",
                "retrieved_sources": [
                    {"source_name": "other.md", "page": 3, "excerpt": "some other text"}
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert scan_audit_log(str(log), {"policy.md::deadbeef0000"}) == []


def test_build_impact_report_shape():
    """Report matches the §8.3 field set; requiresReevaluation tracks affected queries."""
    diff = {
        "added": [{"chunk_id": "v2.md::new1"}],
        "removed": [{"chunk_id": "policy.md::old1"}],
        "modified": [{"old": {"chunk_id": "policy.md::old2"}, "new": {"chunk_id": "v2.md::new2"}}],
    }
    affected = [
        {"auditId": "a1", "timestamp": "t", "question": "q", "usedOldChunk": "policy.md::old1"}
    ]
    report = build_impact_report(
        "policy", diff, affected, old_version_hash="abc123", new_version_hash="def456"
    )

    assert set(report) == {
        "documentId", "oldVersionHash", "newVersionHash", "changedChunks",
        "newChunks", "affectedPastQueries", "requiresReevaluation",
    }
    assert report["documentId"] == "policy"
    assert report["oldVersionHash"] == "abc123"
    assert report["newVersionHash"] == "def456"
    assert set(report["changedChunks"]) == {"policy.md::old1", "policy.md::old2"}
    assert set(report["newChunks"]) == {"v2.md::new1", "v2.md::new2"}
    assert report["requiresReevaluation"] is True

    # No affected queries -> requiresReevaluation False.
    empty = build_impact_report("policy", diff, [])
    assert empty["requiresReevaluation"] is False


def test_end_to_end_modified_doc_flags_past_query(chroma, tmp_path):
    """Ingest v1, record a past query that used it, diff v2, see the query flagged."""
    v1_path = _write(tmp_path, "personal-trading-policy.md", _V1)
    v1 = compute_new_chunks(v1_path)
    for c in v1:
        c["metadata"]["document_id"] = "personal-trading-policy"
    _seed(chroma, v1)

    # Simulate a past query that retrieved every v1 chunk.
    log = tmp_path / "audit.jsonl"
    log.write_text(
        json.dumps(
            {
                "audit_id": "audit_e2e",
                "timestamp": "2026-05-20T10:00:00Z",
                "query": "How long is the blackout period?",
                "retrieved_sources": [
                    {"source_name": c["source_name"], "page": c["page"], "excerpt": c["excerpt"]}
                    for c in v1
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # New version changes the Blackout section text.
    v2 = compute_new_chunks(_write(tmp_path, "modified-policy.md", _V2))

    old_chunks = load_current_chunks("personal-trading-policy", chroma)
    diff = diff_chunks(old_chunks, v2)
    affected_ids = {c["chunk_id"] for c in (diff["removed"] + [m["old"] for m in diff["modified"]])}
    affected_queries = scan_audit_log(str(log), affected_ids)
    report = build_impact_report("personal-trading-policy", diff, affected_queries)

    assert report["requiresReevaluation"] is True
    assert "audit_e2e" in {q["auditId"] for q in affected_queries}
    # The flagged chunk is one that actually changed.
    assert affected_ids, "expected at least one affected chunk"
    assert {q["usedOldChunk"] for q in affected_queries} <= affected_ids


def test_cli_text_output_exits_zero(chroma, tmp_path, monkeypatch, capsys):
    """CLI text mode prints a human-readable summary and exits 0."""
    v1 = compute_new_chunks(_write(tmp_path, "policy.md", _V1))
    for c in v1:
        c["metadata"]["document_id"] = "policy"
    _seed(chroma, v1)
    log = tmp_path / "audit.jsonl"
    log.write_text("", encoding="utf-8")

    monkeypatch.setattr(cli, "open_chroma", lambda: chroma)
    new_source = _write(tmp_path, "v2.md", _V2)

    capsys.readouterr()  # flush ingest progress noise from test setup
    code = cli.main(
        ["--document-id", "policy", "--new-source", new_source, "--audit-log", str(log)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Document: policy" in out
    assert "Changed chunks" in out
    assert "Recommendation" in out


def test_cli_json_output_parseable(chroma, tmp_path, monkeypatch, capsys):
    """CLI json mode emits a parseable §8.3 document and exits 0."""
    v1 = compute_new_chunks(_write(tmp_path, "policy.md", _V1))
    for c in v1:
        c["metadata"]["document_id"] = "policy"
    _seed(chroma, v1)
    log = tmp_path / "audit.jsonl"
    log.write_text("", encoding="utf-8")

    monkeypatch.setattr(cli, "open_chroma", lambda: chroma)
    new_source = _write(tmp_path, "v2.md", _V2)

    capsys.readouterr()  # flush ingest progress noise from test setup
    code = cli.main(
        ["--document-id", "policy", "--new-source", new_source, "--audit-log", str(log), "--output", "json"]
    )
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["documentId"] == "policy"
    assert "changedChunks" in payload
    assert "requiresReevaluation" in payload


def test_cli_document_id_not_found(chroma, tmp_path, monkeypatch, capsys):
    """An unknown document id exits 1 with a clear message."""
    monkeypatch.setattr(cli, "open_chroma", lambda: chroma)
    code = cli.main(["--document-id", "does-not-exist"])
    out = capsys.readouterr().out
    assert code == 1
    assert "not found" in out.lower()
