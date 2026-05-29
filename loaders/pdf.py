"""PDF format handler.

The loader and splitter still live in ``ingest`` for PR1 (so the existing
monkeypatch tests continue to work). This module just wires them into the
registry.
"""

from __future__ import annotations

from langchain_community.document_loaders import PyMuPDFLoader

from .registry import FormatHandler, register


def _load(path: str):
    docs = PyMuPDFLoader(path).load()
    from ingest import _attach_pdf_tables  # late import to avoid circular deps

    return _attach_pdf_tables(docs, path)


def _split(docs):
    from ingest import _split_pdf

    return _split_pdf(docs)


register(
    FormatHandler(
        extensions=(".pdf",),
        loader=_load,
        splitter=_split,
        format_family="text",
        extract_tables=True,
    )
)
