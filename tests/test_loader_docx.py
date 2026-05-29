"""Tests for the .docx format handler."""

from __future__ import annotations

import pytest

docx_module = pytest.importorskip("docx")  # python-docx
DocxDocument = docx_module.Document

from ingest import load_documents, split_documents
from loaders.docx import _load


@pytest.fixture
def sample_docx(tmp_path):
    path = tmp_path / "board-committee-charter.docx"
    document = DocxDocument()
    document.add_heading("Audit Committee Charter", level=1)
    document.add_paragraph(
        "The Audit Committee assists the Board in oversight of financial reporting."
    )
    document.add_heading("Membership", level=2)
    document.add_paragraph("Three independent directors serve on the committee.")
    table = document.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "Metric"
    table.rows[0].cells[1].text = "Threshold"
    table.rows[1].cells[0].text = "Quarterly review"
    table.rows[1].cells[1].text = "Mandatory"
    document.save(str(path))
    return path


def test_docx_load_emits_section_aware_documents(sample_docx):
    docs = _load(str(sample_docx))
    assert docs
    titles = [doc.metadata.get("section_title") for doc in docs]
    assert any(t and "Audit Committee Charter" in t for t in titles)
    assert any(t and "Membership" in t for t in titles)


def test_docx_load_appends_extracted_tables(sample_docx):
    docs = _load(str(sample_docx))
    assert any("[Extracted Tables]" in doc.page_content for doc in docs)
    assert any(
        "Quarterly review" in doc.page_content
        and "Mandatory" in doc.page_content
        for doc in docs
    )
    assert any(doc.metadata.get("contains_tables") for doc in docs)


def test_docx_routed_through_pipeline_and_classified(tmp_path, sample_docx):
    docs = load_documents(str(tmp_path))
    assert docs
    chunks = split_documents(docs)
    assert chunks
    # The filename "board-committee-charter.docx" should be classified by the
    # PR2 patterns we added to infer_filing_type.
    filing_types = {chunk.metadata.get("filing_type") for chunk in chunks}
    assert "committee_charter" in filing_types
