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


def test_explicit_form_type_overrides_filename_and_body():
    """Precedence layer 1 (tests 6/7): declared metadata always wins — an
    explicit 10-Q stays 10-Q even when filename and body both say 10-K."""
    filing_type = infer_filing_type(
        "/repo/acme-annual-10k-package.pdf",
        "Annual Report on Form 10-K for the fiscal year.",
        explicit="10-Q",
    )
    assert filing_type == "10-q"


def test_filename_form_beats_body_mentions_of_other_forms():
    """Precedence layer 2 (test 7): a 10-Q that cites its 10-K in the body
    still classifies from its own filename."""
    filing_type = infer_filing_type(
        "/repo/acme-corp-10q-q3.pdf",
        "As described in our Annual Report on Form 10-K, results may vary.",
    )
    assert filing_type == "10-q"


def test_generic_policy_body_text_does_not_shadow_sec_form():
    """Test 8: the word 'policy' in a filing's body must not reclassify it."""
    filing_type = infer_filing_type(
        "/repo/acme-corp-10q-q3.pdf",
        "Our accounting policy for revenue recognition is described below.",
    )
    assert filing_type == "10-q"

    # And a policy named as such stays a policy even if it cites filings.
    assert (
        infer_filing_type(
            "/repo/compliance-policy-personal-trading.md",
            "See the annual report for details.",
        )
        == "policy"
    )


def test_body_only_heuristics_unchanged_as_last_resort():
    """Layer 3: with no explicit metadata and no filename token, the ordered
    body checks behave exactly as before."""
    assert infer_filing_type("/repo/document.pdf", "quarterly report text") == "10-q"
    assert infer_filing_type("/repo/document.pdf", "this policy applies") == "policy"
    assert infer_filing_type("/repo/document.pdf", "") is None


def test_build_source_metadata_writes_explicit_family_alias(tmp_path):
    """document_family_id is the explicit name; document_id is its legacy alias."""
    source = tmp_path / "acme-corp-10k-excerpt-2025.pdf"
    source.write_bytes(b"Acme Corporation Form 10-K fiscal year 2025")

    metadata = build_source_metadata(source, "Acme Corporation Form 10-K 2025")

    assert metadata["document_family_id"] == metadata["document_id"]
    assert "2025" not in metadata["document_family_id"]  # family strips the year
    # No filing identity without explicit manifest metadata.
    assert "filing_id" not in metadata


def test_build_source_metadata_manifest_entry_supplies_filing_identity(tmp_path):
    """A manifest entry decides identity fields and mints the filing_id."""
    from datetime import date

    source = tmp_path / "acme-corp-10k-excerpt-2025.pdf"
    source.write_bytes(b"Acme Corporation Form 10-K fiscal year 2025")

    metadata = build_source_metadata(
        source,
        "irrelevant body text mentioning policy and 10-Q",
        manifest_entry={
            "company_name": "Acme Corporation",
            "form_type": "10-K",
            "period_end": date(2025, 12, 31),
            "filing_date": date(2026, 2, 19),
        },
    )

    assert metadata["filing_id"] == "acme-corporation:10-k:2025-12-31"
    assert metadata["company"] == "Acme Corporation"
    assert metadata["company_key"] == "acme corporation"
    assert metadata["filing_type"] == "10-k"  # explicit beats the body text
    assert metadata["year"] == 2025  # from period_end, not the filename
    assert metadata["period_end"] == "2025-12-31"
    assert metadata["filing_date"] == "2026-02-19"


def test_section_key_for_recognizes_item_1a_variants():
    """Canonical Item 1A mapping over heading text, incl. common variants."""
    from ingest import SECTION_KEY_ITEM_1A, section_key_for

    for title in (
        "ITEM 1A. RISK FACTORS",
        "Item 1A – Risk Factors",
        "ITEM 1A: RISK FACTORS",
        "Item 1A Risk Factors",
        "PART I > Item 1A. Risk Factors",
        "ITEM 1A.  RISK FACTORS AND UNCERTAINTIES",
    ):
        assert section_key_for(title) == SECTION_KEY_ITEM_1A, title

    # Not Item 1A headings — and never body text (the mapper only ever sees
    # splitter-produced section titles, but reject lookalikes anyway).
    for title in (
        None,
        "",
        "ITEM 1. BUSINESS",
        "ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS",
        "Risk Factors discussed in this research note",
        "ITEM 1B. UNRESOLVED STAFF COMMENTS",
    ):
        assert section_key_for(title) is None, title


def test_split_stamps_chunk_seq_and_section_key(tmp_path):
    """Chunks get a deterministic per-source sequence; Item 1A chunks get the
    canonical section_key; other sections and generic docs get none."""
    from langchain_core.documents import Document

    page = Document(
        page_content=(
            "ITEM 1. BUSINESS\nWe make things.\n"
            "ITEM 1A. RISK FACTORS\nWe face risks including cyber incidents.\n"
        ),
        metadata={"source": "/repo/acme.pdf", "page": 0},
    )
    chunks = split_documents([page])

    assert [c.metadata["chunk_seq"] for c in chunks] == list(range(len(chunks)))
    risk_chunks = [c for c in chunks if "cyber incidents" in c.page_content]
    assert risk_chunks
    assert all(
        c.metadata.get("section_key") == "item_1a_risk_factors" for c in risk_chunks
    )
    business_chunks = [c for c in chunks if "make things" in c.page_content]
    assert business_chunks
    assert all("section_key" not in c.metadata for c in business_chunks)

    # Generic documents: chunk_seq only, never a section key.
    generic = Document(
        page_content="This policy mentions risk factors in passing. " * 10,
        metadata={"source": "/repo/policy.txt"},
    )
    generic_chunks = split_documents([generic])
    assert all("section_key" not in c.metadata for c in generic_chunks)
    assert [c.metadata["chunk_seq"] for c in generic_chunks] == list(
        range(len(generic_chunks))
    )
