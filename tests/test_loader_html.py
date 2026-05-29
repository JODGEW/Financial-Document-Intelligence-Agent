"""Tests for the .html / .htm format handler."""

from __future__ import annotations

import pytest

pytest.importorskip("bs4")

from ingest import load_documents, split_documents
from loaders.html import _load


SAMPLE_HTML = """<!DOCTYPE html>
<html>
  <head>
    <title>Acme Regulatory Notice 2026</title>
    <style>body { color: red; }</style>
    <script>alert("xss");</script>
  </head>
  <body>
    <h1>Notice of Examination</h1>
    <p>The agency is opening a routine examination.</p>
    <h2>Scope</h2>
    <p>The exam will cover the period 2024 through 2025.</p>
    <p>For details see <a href="https://regulator.example.com/notice/2026-001">the public docket</a>.</p>
    <table>
      <tr><th>Item</th><th>Due</th></tr>
      <tr><td>Production request</td><td>2026-06-01</td></tr>
    </table>
  </body>
</html>
"""


@pytest.fixture
def sample_html(tmp_path):
    path = tmp_path / "acme-regulatory-notice-2026.html"
    path.write_text(SAMPLE_HTML, encoding="utf-8")
    return path


def test_html_load_emits_section_aware_documents(sample_html):
    docs = _load(str(sample_html))
    assert docs
    titles = [doc.metadata.get("section_title") for doc in docs]
    assert any(t and "Notice of Examination" in t for t in titles)
    assert any(t and "Scope" in t for t in titles)


def test_html_load_strips_script_and_style(sample_html):
    docs = _load(str(sample_html))
    combined = "\n".join(doc.page_content for doc in docs)
    assert "alert(" not in combined
    assert "color: red" not in combined


def test_html_load_preserves_anchor_links_in_metadata(sample_html):
    docs = _load(str(sample_html))
    assert docs
    links = docs[0].metadata.get("links") or []
    assert any("regulator.example.com/notice/2026-001" in link for link in links)


def test_html_load_appends_extracted_tables(sample_html):
    docs = _load(str(sample_html))
    joined = "\n".join(doc.page_content for doc in docs)
    assert "[Extracted Tables]" in joined
    assert "Production request" in joined
    assert "2026-06-01" in joined


def test_html_routes_through_full_pipeline(tmp_path, sample_html):
    docs = load_documents(str(tmp_path))
    assert docs
    chunks = split_documents(docs)
    assert chunks
    filing_types = {chunk.metadata.get("filing_type") for chunk in chunks}
    assert "regulatory_letter" in filing_types
