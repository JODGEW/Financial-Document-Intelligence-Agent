"""Tests for the .pptx format handler."""

from __future__ import annotations

import pytest

pptx_module = pytest.importorskip("pptx")
Presentation = pptx_module.Presentation
Inches = pptx_module.util.Inches

from ingest import load_documents, split_documents
from loaders.pptx import _load


@pytest.fixture
def sample_pptx(tmp_path):
    path = tmp_path / "board-deck-q1-2026.pptx"
    presentation = Presentation()
    blank_layout = presentation.slide_layouts[6]
    title_layout = presentation.slide_layouts[0]

    # Slide 1 — title + body
    slide1 = presentation.slides.add_slide(title_layout)
    slide1.shapes.title.text = "Quarterly Risk Update"
    if len(slide1.placeholders) > 1:
        slide1.placeholders[1].text = (
            "Cybersecurity incidents are trending down quarter over quarter."
        )

    # Slide 2 — table-only slide
    slide2 = presentation.slides.add_slide(blank_layout)
    table_shape = slide2.shapes.add_table(
        rows=3, cols=2, left=Inches(1), top=Inches(1),
        width=Inches(5), height=Inches(2),
    )
    table = table_shape.table
    table.cell(0, 0).text = "Risk"
    table.cell(0, 1).text = "Severity"
    table.cell(1, 0).text = "Cyber"
    table.cell(1, 1).text = "High"
    table.cell(2, 0).text = "Vendor"
    table.cell(2, 1).text = "Medium"

    presentation.save(str(path))
    return path


def test_pptx_load_emits_one_document_per_slide(sample_pptx):
    docs = _load(str(sample_pptx))
    # Slide 1 has a title + body; slide 2 has a table. Both should produce docs.
    slide_numbers = sorted(doc.metadata.get("slide_number") for doc in docs)
    assert slide_numbers == [1, 2]
    titles = [doc.metadata.get("section_title") for doc in docs]
    assert any(t and "Slide 1" in t and "Quarterly Risk Update" in t for t in titles)
    assert any(t and "Slide 2" in t for t in titles)


def test_pptx_load_appends_extracted_tables(sample_pptx):
    docs = _load(str(sample_pptx))
    table_doc = next(d for d in docs if d.metadata.get("slide_number") == 2)
    assert "[Extracted Tables]" in table_doc.page_content
    assert "| Risk | Severity |" in table_doc.page_content
    assert "Cyber" in table_doc.page_content
    assert table_doc.metadata.get("contains_tables") is True


def test_pptx_routed_through_pipeline_and_classified(tmp_path, sample_pptx):
    docs = load_documents(str(tmp_path))
    assert docs
    chunks = split_documents(docs)
    assert chunks
    # board-deck-q1-2026.pptx triggers the PR3 board_deck pattern.
    filing_types = {chunk.metadata.get("filing_type") for chunk in chunks}
    assert "board_deck" in filing_types
