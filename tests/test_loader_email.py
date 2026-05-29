"""Tests for the .eml / .msg format handler."""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from ingest import load_documents, split_documents
from loaders.email_doc import _load


EML_BODY = (
    "Please find the latest regulatory correspondence attached.\n"
    "Reply to confirm receipt by Friday.\n"
)


@pytest.fixture
def sample_eml(tmp_path):
    path = tmp_path / "regulatory-correspondence-2026.eml"
    message = EmailMessage()
    message["Subject"] = "Q1 Regulatory Update"
    message["From"] = "compliance@example.com"
    message["To"] = "ceo@example.com, cfo@example.com"
    message["Date"] = "Mon, 04 May 2026 09:15:00 -0400"
    message.set_content(EML_BODY)
    # Attachment by filename only — content is not parsed by the loader.
    message.add_attachment(
        b"PDF-DATA",
        maintype="application",
        subtype="pdf",
        filename="filing.pdf",
    )
    path.write_bytes(bytes(message))
    return path


def test_eml_load_emits_one_document_with_header_block(sample_eml):
    docs = _load(str(sample_eml))
    assert len(docs) == 1
    text = docs[0].page_content
    assert "Subject: Q1 Regulatory Update" in text
    assert "From: compliance@example.com" in text
    assert "To: ceo@example.com" in text
    assert "Date: Mon, 04 May 2026" in text
    # Body still present after the header block.
    assert "regulatory correspondence" in text


def test_eml_load_surfaces_headers_and_attachment_names_in_metadata(sample_eml):
    docs = _load(str(sample_eml))
    meta = docs[0].metadata
    assert meta["email_subject"] == "Q1 Regulatory Update"
    assert meta["email_from"] == "compliance@example.com"
    assert "ceo@example.com" in meta["email_to"]
    assert meta["section_title"] == "Q1 Regulatory Update"
    assert meta.get("attachments") == ["filing.pdf"]


def test_eml_routed_through_pipeline_and_classified(tmp_path, sample_eml):
    docs = load_documents(str(tmp_path))
    assert docs
    chunks = split_documents(docs)
    assert chunks
    # Filename "regulatory-correspondence-2026.eml" matches the existing
    # regulatory_letter pattern (which already covers correspondence/notice).
    filing_types = {chunk.metadata.get("filing_type") for chunk in chunks}
    assert "regulatory_letter" in filing_types
