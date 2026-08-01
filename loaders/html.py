"""HTML format handler.

Two section-identification strategies, chosen deterministically per document:

1. **SEC Item structure** (``sec_headings``). Real filings carry no heading
   tags at all — an Item heading is a styled ``div``/``p``/``span``/table cell.
   When the document contains at least one substantive Item heading, sections
   are cut at those headings and ``section_title`` is the canonical heading, so
   ``ingest.section_key_for`` can stamp the Item 1A section key exactly as it
   does for a markdown or PDF filing.
2. **Generic header tags** (``HTMLHeaderTextSplitter`` over ``h1``/``h2``/
   ``h3``). Unchanged behavior for ordinary HTML documents, and the path taken
   whenever no substantive Item heading is found.

Both strategies strip ``<script>`` / ``<style>`` blocks and preserve external
anchors per chunk in the ``links`` metadata field.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import HTMLHeaderTextSplitter

from . import sec_headings
from .registry import FormatHandler, register
from .tables import extract_html_tables


#: Advanced when HTML section identification changes in a way that can move
#: extracted content. ``v1`` was header-tags-only; ``v2`` adds SEC Item
#: structure. Recorded on documents produced by the SEC path so a build record
#: can state which parser produced a section.
HTML_PARSER_VERSION = sec_headings.PARSER_VERSION

#: The Item whose section key the comparison path consumes today.
_ITEM_1A = "1A"

_HEADERS_TO_SPLIT_ON = [
    ("h1", "h1"),
    ("h2", "h2"),
    ("h3", "h3"),
]


def _read_html(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def _soup(html_text: str):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html_text, "lxml")


def _strip_inert(html_text: str) -> tuple[str, str, list[str]]:
    """Return (cleaned_html, page_title, anchor_urls)."""
    soup = _soup(html_text)
    for tag in soup(["script", "style"]):
        tag.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string else "")
    links = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href and not href.startswith("#"):
            links.append(href)
    return str(soup), title, links


def _item_1a_metadata(selection: sec_headings.SectionSelection) -> dict:
    """Flat, bounded extraction diagnostics for Chroma metadata.

    Chroma metadata must be flat scalars, so every value here is a str/int/bool
    and ``None`` values are dropped rather than written. Nothing carries filing
    prose: only counts, a canonical heading label, an element tag, and a reason
    code.
    """
    diagnostics = selection.diagnostics()
    metadata = {
        "sec_parser_version": diagnostics["parser_version"],
        "sec_item1a_outcome": diagnostics["outcome"],
        "sec_item1a_reason": diagnostics["extraction_reason"],
        "sec_item1a_candidate_count": diagnostics["candidate_count"],
        "sec_item1a_substantive_count": diagnostics["substantive_candidate_count"],
        "sec_item1a_navigation_rejected": diagnostics["navigation_rejected_count"],
        "sec_item1a_designator_only": diagnostics["designator_only_count"],
        "sec_item1a_insufficient_content": diagnostics["insufficient_content_count"],
        "sec_item1a_heading": diagnostics["selected_canonical_heading"],
        "sec_item1a_element_tag": diagnostics["selected_element_tag"],
        "sec_item1a_boundary_heading": diagnostics["boundary_heading"],
    }
    return {key: value for key, value in metadata.items() if value is not None}


def _sec_documents(
    soup,
    path: str,
    title: str,
    links: list[str],
) -> list[Document] | None:
    """Section-aware documents cut at SEC Item headings, or None.

    Returns ``None`` when the document establishes no substantive Item heading,
    which is the signal to fall back to the generic header-tag path.
    """
    blocks, candidates, selection = sec_headings.detect_item_sections(soup, _ITEM_1A)
    substantive = sec_headings.substantive_headings(candidates)
    if not substantive:
        return None

    extracted = selection.outcome == sec_headings.OUTCOME_EXTRACTED
    titles_by_start = {
        candidate.block_index: candidate.canonical for candidate in substantive
    }
    # An Item 1A occurrence only gets a heading — and so only reaches
    # section_key_for — when selection actually resolved it. An ambiguous or
    # bounded-out Item 1A must not launder into a stamped section.
    for candidate in substantive:
        if candidate.item_id != _ITEM_1A:
            continue
        if not extracted or candidate.block_index != selection.start_block:
            titles_by_start[candidate.block_index] = None

    cuts = {0} | set(titles_by_start)
    if extracted:
        cuts.add(selection.end_block)
    ordered = sorted(cut for cut in cuts if 0 <= cut <= len(blocks))
    bounds = ordered + [len(blocks)]

    base: dict = {"source": path}
    if title:
        base["document_title"] = title
    if links:
        base["links"] = links
    base.update(_item_1a_metadata(selection))

    docs: list[Document] = []
    for start, end in zip(bounds, bounds[1:]):
        if start >= end:
            continue
        section_title = titles_by_start.get(start)
        # The heading block names the section; it is not part of its body.
        first = start + 1 if section_title else start
        content = sec_headings.blocks_text(blocks, first, end)
        if not content:
            continue
        metadata = dict(base)
        if section_title:
            metadata["section_title"] = section_title
        docs.append(Document(page_content=content, metadata=metadata))

    return docs or None


def _header_documents(
    cleaned_html: str,
    path: str,
    title: str,
    links: list[str],
) -> list[Document]:
    """Generic ``h1``/``h2``/``h3`` sectioning (pre-existing behavior)."""
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
        plain = _soup(cleaned_html).get_text("\n", strip=True)
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


def _load(path: str) -> list[Document]:
    raw_html = _read_html(path)
    cleaned_html, title, links = _strip_inert(raw_html)

    # The SEC path reads table cells as ordinary blocks in document order, so
    # it needs no separate [Extracted Tables] append — appending would duplicate
    # every table a filing contains.
    sec_docs = _sec_documents(_soup(cleaned_html), path, title, links)
    if sec_docs is not None:
        return sec_docs

    return _header_documents(cleaned_html, path, title, links)


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
    )
)
