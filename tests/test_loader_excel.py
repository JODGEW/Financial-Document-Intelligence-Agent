"""Tests for the .xlsx format handler."""

from __future__ import annotations

import pytest

openpyxl = pytest.importorskip("openpyxl")

import config
from ingest import load_documents, split_documents
from loaders.excel import _load, _split, EXCEL_MAX_ROWS_PER_CHUNK


@pytest.fixture
def sample_xlsx(tmp_path):
    path = tmp_path / "controls-risk-register.xlsx"
    workbook = openpyxl.Workbook()
    sheet1 = workbook.active
    sheet1.title = "Controls"
    sheet1.append(["Control ID", "Owner", "Email"])
    sheet1.append(["C-001", "Compliance", "alice@example.com"])
    sheet1.append(["C-002", "Risk", "bob.smith@example.com"])

    sheet2 = workbook.create_sheet("Risks")
    sheet2.append(["Risk ID", "Description"])
    sheet2.append(["R-001", "Cybersecurity exposure"])

    workbook.save(str(path))
    return path


def test_xlsx_load_emits_one_doc_per_sheet(sample_xlsx):
    docs = _load(str(sample_xlsx))
    assert len(docs) == 2
    sheet_names = {doc.metadata["sheet_name"] for doc in docs}
    assert sheet_names == {"Controls", "Risks"}
    titles = {doc.metadata.get("section_title") for doc in docs}
    assert "Sheet: Controls" in titles
    assert "Sheet: Risks" in titles


def test_xlsx_load_formats_cells_as_markdown_table(sample_xlsx):
    docs = _load(str(sample_xlsx))
    controls = next(d for d in docs if d.metadata["sheet_name"] == "Controls")
    assert "| Control ID | Owner | Email |" in controls.page_content
    assert "| C-001 | Compliance |" in controls.page_content


def test_xlsx_load_prefixes_description_for_retrieval(sample_xlsx):
    """PR2.1: per-sheet description gives the embedder semantic anchors."""
    docs = _load(str(sample_xlsx))
    controls = next(d for d in docs if d.metadata["sheet_name"] == "Controls")
    text = controls.page_content
    # Fixture filename: controls-risk-register.xlsx → "Controls Risk Register"
    assert text.startswith("Document: Controls Risk Register")
    assert "Sheet: Controls" in text
    assert "Columns: Control ID, Owner, Email" in text


def test_xlsx_pii_redacted_by_default_through_pipeline(tmp_path, sample_xlsx):
    """PII_REDACT_TABULAR_AT_INGEST defaults to true; emails should be redacted."""
    assert config.PII_REDACT_TABULAR_AT_INGEST is True
    docs = load_documents(str(tmp_path))
    assert docs
    controls = next(d for d in docs if d.metadata.get("sheet_name") == "Controls")
    assert "alice@example.com" not in controls.page_content
    assert "[REDACTED:EMAIL]" in controls.page_content
    assert controls.metadata.get("pii_redaction_email", 0) >= 2
    assert controls.metadata.get("pii_redaction_total", 0) >= 2


def test_xlsx_pii_redaction_can_be_disabled(tmp_path, sample_xlsx, monkeypatch):
    """When both PII flags are off, raw emails survive ingestion."""
    monkeypatch.setattr(config, "PII_REDACT_AT_INGEST", False)
    monkeypatch.setattr(config, "PII_REDACT_TABULAR_AT_INGEST", False)
    docs = load_documents(str(tmp_path))
    assert docs
    controls = next(d for d in docs if d.metadata.get("sheet_name") == "Controls")
    assert "alice@example.com" in controls.page_content
    assert "[REDACTED" not in controls.page_content


def test_xlsx_split_caps_oversized_sheet_into_multiple_chunks():
    from langchain_core.documents import Document

    huge_rows = (EXCEL_MAX_ROWS_PER_CHUNK * 2) + 5
    body = "\n".join(f"| row{i} | val{i} |" for i in range(huge_rows))
    doc = Document(
        page_content="| Col1 | Col2 |\n| --- | --- |\n" + body,
        metadata={"source": "fake.xlsx", "sheet_name": "Big"},
    )
    chunks = _split([doc])
    assert len(chunks) >= 3
    # Header repeats on every chunk.
    for chunk in chunks:
        assert chunk.page_content.startswith("| Col1 | Col2 |")
        assert "| --- | --- |" in chunk.page_content
