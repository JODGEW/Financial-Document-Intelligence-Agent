"""Format-handler registry for the document ingestion pipeline.

A FormatHandler bundles everything needed to ingest one file extension:
- a loader callable (path -> list[Document])
- an optional splitter callable (docs -> chunks); None means use the
  pipeline's fallback recursive splitter
- a format_family classification used by the PII redaction dispatch
- optional flags / hints for downstream metadata enrichment

Handlers register themselves at import time. The ingestion pipeline calls
``handler_for(ext)`` to dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

from langchain_core.documents import Document


LoaderFn = Callable[[str], list[Document]]
SplitterFn = Callable[[list[Document]], list[Document]]

FormatFamily = Literal["text", "tabular", "structured"]


@dataclass(frozen=True)
class FormatHandler:
    extensions: tuple[str, ...]
    loader: LoaderFn
    splitter: SplitterFn | None = None
    format_family: FormatFamily = "text"
    extract_tables: bool = False
    filing_type_hints: tuple[str, ...] = field(default_factory=tuple)


REGISTRY: dict[str, FormatHandler] = {}


def register(handler: FormatHandler) -> None:
    """Register a handler for each of its declared extensions."""
    for ext in handler.extensions:
        REGISTRY[ext.lower()] = handler


def handler_for(ext: str) -> FormatHandler | None:
    """Look up the handler for a file extension (case-insensitive)."""
    return REGISTRY.get(ext.lower())


def supported_extensions() -> list[str]:
    """Return all registered extensions, sorted."""
    return sorted(REGISTRY.keys())


def reset_registry() -> None:
    """Test helper. Clears the registry."""
    REGISTRY.clear()
