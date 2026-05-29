"""HTML format handler.

Strips ``<script>`` / ``<style>`` blocks, runs header-aware splitting via
``HTMLHeaderTextSplitter``, and appends extracted ``<table>`` content in the
same ``[Extracted Tables]`` format other handlers use. External anchors are
preserved per chunk in the ``links`` metadata field.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import HTMLHeaderTextSplitter

from .registry import FormatHandler, register
from .tables import extract_html_tables


_HEADERS_TO_SPLIT_ON = [
    ("h1", "h1"),
    ("h2", "h2"),
    ("h3", "h3"),
]


def _read_html(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def _strip_inert(html_text: str) -> tuple[str, str, list[str]]:
    """Return (cleaned_html, page_title, anchor_urls)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string else "")
    links = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href and not href.startswith("#"):
            links.append(href)
    return str(soup), title, links


def _load(path: str) -> list[Document]:
    raw_html = _read_html(path)
    cleaned_html, title, links = _strip_inert(raw_html)

    splitter = HTMLHeaderTextSplitter(headers_to_split_on=_HEADERS_TO_SPLIT_ON)
    sections = splitter.split_text(cleaned_html)

    docs: list[Document] = []
    for section in sections:
        title_parts = [
            section.metadata[key]
            for _tag, key in _HEADERS_TO_SPLIT_ON
            if section.metadata.get(key)
        ]
        section_title = " > ".join(title_parts) if title_parts else None

        metadata: dict = {"source": path}
        if title:
            metadata["document_title"] = title
        if section_title:
            metadata["section_title"] = section_title
        if links:
            metadata["links"] = links

        docs.append(Document(page_content=section.page_content, metadata=metadata))

    if not docs:
        from bs4 import BeautifulSoup

        plain = BeautifulSoup(cleaned_html, "lxml").get_text("\n", strip=True)
        if plain:
            metadata = {"source": path}
            if title:
                metadata["document_title"] = title
            if links:
                metadata["links"] = links
            docs.append(Document(page_content=plain, metadata=metadata))

    table_blocks = extract_html_tables(cleaned_html)
    if table_blocks and docs:
        joined = "\n\n".join(table_blocks)
        last = docs[-1]
        last.page_content = (
            f"{last.page_content.rstrip()}\n\n[Extracted Tables]\n{joined}"
        )
        last.metadata["contains_tables"] = True

    return docs


def _split(docs: list[Document]) -> list[Document]:
    from ingest import _recursive_splitter

    splitter = _recursive_splitter(chunk_size=900, chunk_overlap=100)
    chunks: list[Document] = []
    for doc in docs:
        for piece in splitter.split_documents([doc]):
            piece.metadata = {**doc.metadata, **piece.metadata}
            chunks.append(piece)
    return chunks


register(
    FormatHandler(
        extensions=(".html", ".htm"),
        loader=_load,
        splitter=_split,
        format_family="text",
        extract_tables=True,
    )
)
