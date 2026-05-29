"""Tests for the .csv format handler."""

from __future__ import annotations

import pytest

import config
from ingest import load_documents, split_documents
from loaders.csv import _load, _split, CSV_ROWS_PER_CHUNK


SAMPLE_CSV_HEADER = "id,actor,email\n"
SAMPLE_ROWS = [
    f"{i},user_{i},user{i}@example.com" for i in range(1, 51)
]


@pytest.fixture
def sample_csv(tmp_path):
    path = tmp_path / "kyc-audit-trail-2026.csv"
    body = SAMPLE_CSV_HEADER + "\n".join(SAMPLE_ROWS) + "\n"
    path.write_text(body, encoding="utf-8")
    return path


def test_csv_load_returns_one_document_with_description_and_markdown_table(sample_csv):
    docs = _load(str(sample_csv))
    assert len(docs) == 1
    text = docs[0].page_content
    # Description prefix (PR2.1) gives retrieval semantic anchors.
    assert text.startswith("Document: KYC Audit Trail")
    assert "File: kyc-audit-trail-2026.csv" in text
    assert "Columns: id, actor, email" in text
    # Original markdown table still present.
    assert "| id | actor | email |" in text
    assert "| --- | --- | --- |" in text
    assert "| 1 | user_1 | user1@example.com |" in text


def test_csv_split_repeats_description_and_header_on_every_chunk(sample_csv):
    docs = _load(str(sample_csv))
    chunks = _split(docs)
    assert len(chunks) >= 3  # 50 rows / 20 per chunk = 3 chunks
    for chunk in chunks:
        # Description prefix repeats so each chunk's embedding has semantic context.
        assert chunk.page_content.startswith("Document: KYC Audit Trail")
        assert "File: kyc-audit-trail-2026.csv" in chunk.page_content
        # Markdown header repeats so column labels survive too.
        assert "| id | actor | email |" in chunk.page_content
        assert "| --- | --- | --- |" in chunk.page_content
    assert chunks[0].metadata["csv_chunk_index"] == 0
    assert chunks[-1].metadata["csv_chunk_first_row"] == CSV_ROWS_PER_CHUNK * (
        len(chunks) - 1
    )


def test_csv_pii_redacted_by_default_through_pipeline(tmp_path, sample_csv):
    assert config.PII_REDACT_TABULAR_AT_INGEST is True
    docs = load_documents(str(tmp_path))
    assert docs
    text = docs[0].page_content
    assert "user1@example.com" not in text
    assert "[REDACTED:EMAIL]" in text
    assert docs[0].metadata.get("pii_redaction_email", 0) >= 50
    assert docs[0].metadata.get("pii_redaction_total", 0) >= 50


def test_csv_pii_redaction_can_be_disabled(tmp_path, sample_csv, monkeypatch):
    monkeypatch.setattr(config, "PII_REDACT_AT_INGEST", False)
    monkeypatch.setattr(config, "PII_REDACT_TABULAR_AT_INGEST", False)
    docs = load_documents(str(tmp_path))
    assert docs
    text = docs[0].page_content
    assert "user1@example.com" in text
    assert "[REDACTED" not in text


def test_csv_filing_type_inferred_from_filename(tmp_path, sample_csv):
    docs = load_documents(str(tmp_path))
    chunks = split_documents(docs)
    filing_types = {chunk.metadata.get("filing_type") for chunk in chunks}
    # Filename has both "kyc" and "audit-trail"; either is a valid pattern,
    # whichever matches first in the regex list wins.
    assert filing_types & {"kyc", "audit_trail"}


def test_pii_metadata_is_chroma_compatible(tmp_path, sample_csv):
    """Regression: Chroma rejects dict metadata. All values must be scalar/list/None.

    Without this guard, the PII counts dict broke ``python ingest.py`` even
    though the unit tests passed (they never reached ``embed_and_persist``).
    """
    docs = load_documents(str(tmp_path))
    chunks = split_documents(docs)
    allowed_types = (str, int, float, bool, type(None))
    for chunk in chunks:
        for key, value in chunk.metadata.items():
            assert isinstance(value, allowed_types + (list,)), (
                f"Chroma cannot persist non-scalar metadata; "
                f"key={key!r} value={value!r} type={type(value).__name__}"
            )
