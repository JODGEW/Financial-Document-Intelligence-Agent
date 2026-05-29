"""Tests for the format-handler registry."""

from __future__ import annotations

from langchain_core.documents import Document

from loaders.registry import (
    FormatHandler,
    REGISTRY,
    handler_for,
    register,
    supported_extensions,
)


def test_builtin_handlers_are_registered():
    """PR1 ships handlers for PDF, Markdown, and plain text."""
    # Importing loaders triggers registration as a side effect.
    import loaders  # noqa: F401

    assert ".pdf" in REGISTRY
    assert ".md" in REGISTRY
    assert ".txt" in REGISTRY


def test_handler_for_is_case_insensitive():
    pdf = handler_for(".PDF")
    assert pdf is not None
    assert ".pdf" in pdf.extensions


def test_handler_for_unknown_extension_returns_none():
    assert handler_for(".bin") is None
    assert handler_for(".xyz") is None


def test_register_idempotent_overwrite():
    """Re-registering the same extension overrides the prior handler."""

    def fake_loader(path: str):
        return [Document(page_content="fake", metadata={"source": path})]

    sentinel = FormatHandler(
        extensions=(".__test_ext__",),
        loader=fake_loader,
        format_family="text",
    )
    register(sentinel)
    try:
        looked_up = handler_for(".__test_ext__")
        assert looked_up is sentinel
        assert ".__test_ext__" in supported_extensions()
    finally:
        REGISTRY.pop(".__test_ext__", None)


def test_supported_extensions_returns_sorted_list():
    exts = supported_extensions()
    assert exts == sorted(exts)
    assert ".md" in exts
    assert ".pdf" in exts
    assert ".txt" in exts


def test_pdf_handler_dispatches_to_ingest_split_pdf():
    """The registered PDF handler's splitter must reach ingest._split_pdf."""
    handler = handler_for(".pdf")
    assert handler is not None
    assert handler.splitter is not None

    page = Document(
        page_content="ITEM 1A. RISK FACTORS\nWe face risks.\n",
        metadata={"source": "/repo/acme.pdf", "page": 0},
    )
    chunks = handler.splitter([page])
    assert chunks
    assert any(
        "ITEM 1A" in (c.metadata.get("section_title") or "").upper() for c in chunks
    )
