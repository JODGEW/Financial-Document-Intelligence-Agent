"""Markdown format handler."""

from __future__ import annotations

from langchain_community.document_loaders import TextLoader

from .registry import FormatHandler, register


def _load(path: str):
    return TextLoader(path).load()


def _split(docs):
    from ingest import _split_markdown

    chunks = []
    for doc in docs:
        chunks.extend(_split_markdown(doc))
    return chunks


register(
    FormatHandler(
        extensions=(".md",),
        loader=_load,
        splitter=_split,
        format_family="text",
    )
)
