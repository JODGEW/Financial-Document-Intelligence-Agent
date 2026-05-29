"""Plain-text format handler.

Falls through to the pipeline's recursive character splitter (no custom
splitter registered).
"""

from __future__ import annotations

from langchain_community.document_loaders import TextLoader

from .registry import FormatHandler, register


def _load(path: str):
    return TextLoader(path).load()


register(
    FormatHandler(
        extensions=(".txt",),
        loader=_load,
        splitter=None,
        format_family="text",
    )
)
