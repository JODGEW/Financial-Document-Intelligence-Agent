"""Tests for the ingestion pipeline."""

import os
import tempfile

import pytest
from langchain_core.documents import Document

from ingest import (
    build_source_metadata,
    chunk_id,
    chunk_ids,
    company_metadata_key,
    infer_company,
    infer_filing_type,
    load_documents,
    split_documents,
    _attach_pdf_tables,
    _format_table_rows,
    _split_markdown,
    _split_pdf,
)


@pytest.fixture
def sample_docs_dir(tmp_path):
    """Create a temp directory with a sample .txt file."""
    doc = tmp_path / "test.txt"
    doc.write_text("This is a test document with enough content to be meaningful. " * 20)
    return str(tmp_path)


def test_load_documents_finds_txt(sample_docs_dir):
    docs = load_documents(sample_docs_dir)
    assert len(docs) >= 1
    assert "test document" in docs[0].page_content


def test_load_documents_skips_unsupported(tmp_path):
    # CSV is now a supported format (PR2). Pick an extension the registry
    # does not know about so the skip path is what we exercise.
    (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02\x03")
    docs = load_documents(str(tmp_path))
    assert len(docs) == 0


def test_load_documents_empty_dir(tmp_path):
    docs = load_documents(str(tmp_path))
    assert docs == []


def test_split_documents_creates_chunks(sample_docs_dir):
    docs = load_documents(sample_docs_dir)
    chunks = split_documents(docs)
    assert len(chunks) >= 1
    for chunk in chunks:
        assert len(chunk.page_content) > 0


def test_split_preserves_metadata(sample_docs_dir):
    docs = load_documents(sample_docs_dir)
    chunks = split_documents(docs)
    for chunk in chunks:
        assert "source" in chunk.metadata


def test_load_real_docs():
    """Verify the actual /docs directory loads successfully."""
    import config
    if not os.path.isdir(config.DOCS_DIR):
        pytest.skip("docs directory not found")
    docs = load_documents(config.DOCS_DIR)
    assert len(docs) >= 3, f"Expected at least 3 docs, got {len(docs)}"


def test_chunk_id_is_stable_for_same_content():
    chunk_a = Document(
        page_content="Revenue was $284.7 million in FY 2025.",
        metadata={"source": "/repo/docs/acme.pdf", "page": 2},
    )
    chunk_b = Document(
        page_content="Revenue was $284.7 million in FY 2025.",
        metadata={"source": "/repo/docs/acme.pdf", "page": 2},
    )

    assert chunk_id(chunk_a) == chunk_id(chunk_b)


def test_chunk_id_changes_when_content_changes():
    base = Document(
        page_content="Revenue was $284.7 million.",
        metadata={"source": "/repo/docs/acme.pdf", "page": 2},
    )
    edited = Document(
        page_content="Revenue was $300.0 million.",
        metadata={"source": "/repo/docs/acme.pdf", "page": 2},
    )

    assert chunk_id(base) != chunk_id(edited)


def test_split_documents_is_idempotent_at_id_level():
    """Re-splitting the same docs must produce identical chunk IDs."""
    import config
    if not os.path.isdir(config.DOCS_DIR):
        pytest.skip("docs directory not found")

    docs = load_documents(config.DOCS_DIR)
    first_ids = chunk_ids(split_documents(docs))
    second_ids = chunk_ids(split_documents(docs))

    assert first_ids == second_ids
    assert len(set(first_ids)) == len(first_ids), "Chunk IDs must be unique within a run"


def test_markdown_split_attaches_section_title():
    md_text = (
        "# Top Title\n"
        "\n"
        "Intro line.\n"
        "\n"
        "## 5. Blackout Periods\n"
        "\n"
        "### 5.1 Quarterly Blackout\n"
        "\n"
        "No trades during the quarterly blackout.\n"
    )
    doc = Document(page_content=md_text, metadata={"source": "/repo/policy.md"})

    chunks = _split_markdown(doc)

    assert chunks
    blackout_chunks = [c for c in chunks if "blackout" in c.page_content.lower()]
    assert blackout_chunks
    titles = {c.metadata.get("section_title") for c in blackout_chunks}
    assert any(t and "5.1 Quarterly Blackout" in t for t in titles)
    assert any(t and "Top Title" in t for t in titles)  # hierarchy preserved


def test_pdf_split_attaches_sec_section_title():
    page_text = (
        "PART I\n"
        "ITEM 1A. RISK FACTORS\n"
        "We face risks including cyber incidents and regulatory change.\n"
        "Additional discussion of risk continues here.\n"
    )
    page = Document(page_content=page_text, metadata={"source": "/repo/acme.pdf", "page": 0})

    chunks = _split_pdf([page])

    assert chunks
    risk_chunks = [c for c in chunks if "risk" in c.page_content.lower()]
    assert risk_chunks
    titles = {c.metadata.get("section_title") for c in risk_chunks}
    assert any(t and "ITEM 1A" in t.upper() for t in titles)


def test_pdf_section_carries_across_pages():
    """A section heading on page 0 should label content on page 1 if no new heading appears."""
    page0 = Document(
        page_content="ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS\nResults of operations.\n",
        metadata={"source": "/repo/acme.pdf", "page": 0},
    )
    page1 = Document(
        page_content="Continued narrative discussion of revenue growth.\n",
        metadata={"source": "/repo/acme.pdf", "page": 1},
    )

    chunks = _split_pdf([page0, page1])

    page1_chunks = [c for c in chunks if c.metadata.get("page") == 1]
    assert page1_chunks
    assert all("ITEM 7" in (c.metadata.get("section_title") or "").upper() for c in page1_chunks)


def test_table_rows_are_formatted_for_retrieval():
    rows = [
        ["Metric", "FY2025"],
        ["Revenue", "$284.7 million"],
        [None, ""],
    ]

    formatted = _format_table_rows(rows)

    assert "| Metric | FY2025 |" in formatted
    assert "| --- | --- |" in formatted
    assert "| Revenue | $284.7 million |" in formatted


def test_pdf_table_text_is_attached_to_matching_page(monkeypatch):
    page = Document(
        page_content="Narrative disclosure.",
        metadata={"source": "/repo/acme.pdf", "page": 0},
    )

    monkeypatch.setattr(
        "ingest._extract_pdf_tables",
        lambda _source: {0: "Table 1\n| Metric | FY2025 |\n| --- | --- |\n| Revenue | $284.7 million |"},
    )

    docs = _attach_pdf_tables([page], "/repo/acme.pdf")

    assert "[Extracted Tables]" in docs[0].page_content
    assert "$284.7 million" in docs[0].page_content
    assert docs[0].metadata["contains_tables"] is True


def test_build_source_metadata_tracks_version_and_filters(tmp_path):
    source = tmp_path / "acme-corp-10k-excerpt-2025.pdf"
    source.write_bytes(b"Acme Corporation Form 10-K fiscal year 2025")

    metadata = build_source_metadata(source, "Acme Corporation Form 10-K fiscal year 2025")

    assert metadata["source_name"] == "acme-corp-10k-excerpt-2025.pdf"
    assert metadata["document_hash"]
    assert metadata["document_version"] == metadata["document_hash"][:12]
    assert metadata["company"] == "Acme Corp"
    assert metadata["company_key"] == "acme corporation"
    assert metadata["filing_type"] == "10-k"
    assert metadata["year"] == 2025


def test_company_metadata_key_normalizes_suffix_variants():
    assert company_metadata_key("Acme Corp") == company_metadata_key("Acme Corporation")


def test_infer_company_strips_policy_prefix_words():
    company = infer_company(
        "/repo/compliance-policy.md",
        "This policy applies to all Covered Persons at Acme Corporation.",
    )

    assert company == "Acme Corporation"


def test_research_note_type_takes_precedence_over_discussed_filings():
    filing_type = infer_filing_type(
        "/repo/cybersecurity-disclosure-research-note.txt",
        "This internal research note reviews companies that filed 10-K annual reports.",
    )

    assert filing_type == "research_note"
