"""Load documents from /docs, chunk, embed, and persist to Chroma.

Chunking is section-aware where the source supports it:
- Markdown: split on ``#``/``##``/``###`` headers, then sub-split long sections.
- PDF (10-K style): split on ``PART X`` / ``ITEM N[A]`` headings detected per page.
- Plain text and unknown: recursive character splitting only.

Each chunk gets a stable ID derived from ``(source filename, page, content hash)``,
so re-running ingestion upserts existing chunks instead of duplicating them.
"""

import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import fitz
from langchain_aws import BedrockEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

import config
import loaders  # noqa: F401  (side-effect: register format handlers)
from loaders.pii import redact as _pii_redact, should_redact as _should_redact
from loaders.registry import handler_for
from loaders.tables import format_table_rows as _format_table_rows  # re-export

SEC_SECTION_RE = re.compile(
    r"(?im)^(?:PART\s+[IVX]+|ITEM\s+\d+[A-Z]?)\b[^\n]*$"
)
MARKDOWN_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3")]
COMPANY_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,4}\s+"
    r"(?:Corporation|Corp\.?|Inc\.?|LLC|Ltd\.?|PLC))\b",
    re.I,
)
YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _metadata_key(value: str) -> str:
    """Normalize a metadata label for exact-match filtering."""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def company_metadata_key(value: str) -> str:
    """Normalize company suffix variants to one filter key."""
    key = _metadata_key(value)
    key = re.sub(r"\bcorp\b", "corporation", key)
    key = re.sub(r"\binc\b", "incorporated", key)
    return key


def _display_company(raw_company: str) -> str:
    """Normalize common company suffix punctuation while preserving display case."""
    company = " ".join(raw_company.replace("-", " ").split())
    heading_words = {
        "all",
        "applicable",
        "at",
        "business",
        "company",
        "corporate",
        "covered",
        "for",
        "overview",
        "persons",
        "policy",
        "the",
        "this",
        "to",
    }
    raw_parts = company.split()
    while len(raw_parts) > 2 and raw_parts[0].lower().rstrip(".") in heading_words:
        raw_parts = raw_parts[1:]
    company = " ".join(raw_parts)
    replacements = {
        "corp": "Corp",
        "corporation": "Corporation",
        "inc": "Inc",
        "llc": "LLC",
        "ltd": "Ltd",
        "plc": "PLC",
    }
    parts = []
    for part in company.split():
        cleaned = part.rstrip(".")
        parts.append(replacements.get(cleaned.lower(), cleaned.capitalize()))
    return " ".join(parts)


def infer_company(source_path: str | Path, text_hint: str = "") -> str | None:
    """Infer a company name from document text or filename."""
    stem = Path(source_path).stem
    filing_match = re.match(r"(?P<company>.+?)[-_ ](?:10[-_ ]?[kq]|8[-_ ]?k)\b", stem, re.I)
    if filing_match:
        return _display_company(filing_match.group("company"))

    match = COMPANY_RE.search(text_hint)
    if match:
        return _display_company(match.group(1))
    return None


# Additional filing-type patterns added in PR2 for P0 formats. They run after
# the original five checks so existing precedence (research_note > policy > 10-K
# > 10-Q > 8-K) is preserved verbatim.
# Separator class ``[- _]`` lets the same regex match both filename
# (``board-committee-charter``) and natural-language body text
# (``board committee charter``).
_EXTRA_FILING_TYPE_PATTERNS: list[tuple[str, str]] = [
    (r"board[- _](meeting[- _])?minutes", "board_minutes"),
    (r"committee[- _]charter", "committee_charter"),
    (r"control[- _]matrix|controls?[- _]register", "control_matrix"),
    (r"risk[- _]register", "risk_register"),
    (r"\bkyc\b|know[- _]?your[- _]?customer", "kyc"),
    (r"audit[- _]trail|transaction[- _]log", "audit_trail"),
    (r"regulatory[- _](letter|correspondence|notice)", "regulatory_letter"),
    # PR3 — P1 formats
    (r"board[- _]deck|board[- _]slides", "board_deck"),
    (r"proxy[- _]statement", "proxy_statement"),
    (r"training[- _]material|training[- _]deck", "training_material"),
    (r"email[- _]archive|inbox[- _]export", "email_archive"),
    (r"(?:inline[- _]xbrl|xbrl[- _]inline|ixbrl)", "xbrl_inline"),
    (r"api[- _]export|api[- _]dump", "api_export"),
]


def infer_filing_type(source_path: str | Path, text_hint: str = "") -> str | None:
    """Infer a document/filing type for metadata filtering."""
    source_name = Path(source_path).name.lower()
    combined = f"{source_name} {text_hint}".lower()
    if "research note" in source_name or "research note" in combined:
        return "research_note"
    if "policy" in source_name or "policy" in combined:
        return "policy"
    if re.search(r"\b10[-_ ]?k\b|annual report", combined):
        return "10-k"
    if re.search(r"\b10[-_ ]?q\b|quarterly report", combined):
        return "10-q"
    if re.search(r"\b8[-_ ]?k\b|current report", combined):
        return "8-k"
    for pattern, filing_type in _EXTRA_FILING_TYPE_PATTERNS:
        if re.search(pattern, combined):
            return filing_type
    return None


def infer_document_year(source_path: str | Path, text_hint: str = "") -> int | None:
    """Infer the most relevant document year, preferring the filename."""
    filename_match = YEAR_RE.search(Path(source_path).stem)
    if filename_match:
        return int(filename_match.group(1))

    text_match = YEAR_RE.search(text_hint[:4000])
    if text_match:
        return int(text_match.group(1))
    return None


def _relative_source_path(path: Path) -> str:
    """Return a corpus-relative path when possible."""
    try:
        return path.resolve().relative_to(Path(config.DOCS_DIR).resolve()).as_posix()
    except ValueError:
        return path.name


def _document_id(source_path: str) -> str:
    """Stable document family ID across content versions."""
    stem = Path(source_path).with_suffix("").as_posix()
    stem = re.sub(r"\b20\d{2}\b", "", stem)
    stem = re.sub(r"[-_/]+", "-", stem).strip("-")
    return _metadata_key(stem).replace(" ", "-")


def build_source_metadata(source_path: str | Path, text_hint: str = "") -> dict:
    """Build file-level metadata used for versioning and retrieval filters."""
    path = Path(source_path)
    file_bytes = path.read_bytes()
    document_hash = hashlib.sha256(file_bytes).hexdigest()
    relative_path = _relative_source_path(path)
    stat = path.stat()

    metadata = {
        "source_name": path.name,
        "source_path": relative_path,
        "document_id": _document_id(relative_path),
        "document_hash": document_hash,
        "document_version": document_hash[:12],
        "file_size": stat.st_size,
        "file_modified_at": datetime.fromtimestamp(
            stat.st_mtime,
            tz=timezone.utc,
        ).isoformat(),
    }

    company = infer_company(path, text_hint)
    if company:
        metadata["company"] = company
        metadata["company_key"] = company_metadata_key(company)

    filing_type = infer_filing_type(path, text_hint)
    if filing_type:
        metadata["filing_type"] = filing_type

    year = infer_document_year(path, text_hint)
    if year:
        metadata["year"] = year

    return metadata


def _extract_pdf_tables(source_path: str | Path) -> dict[int, str]:
    """Extract page-indexed table text from a PDF using PyMuPDF."""
    tables_by_page: dict[int, list[str]] = {}
    try:
        with fitz.open(source_path) as pdf:
            for page_index, page in enumerate(pdf):
                finder = page.find_tables()
                tables = getattr(finder, "tables", [])
                for table_index, table in enumerate(tables, 1):
                    formatted = _format_table_rows(table.extract())
                    if formatted:
                        tables_by_page.setdefault(page_index, []).append(
                            f"Table {table_index}\n{formatted}"
                        )
    except Exception as exc:
        print(f"  WARN: failed to extract tables from {Path(source_path).name}: {exc}")

    return {
        page_index: "\n\n".join(page_tables)
        for page_index, page_tables in tables_by_page.items()
    }


def _attach_pdf_tables(docs: list[Document], source_path: str | Path) -> list[Document]:
    """Append extracted table text to matching PDF page documents."""
    tables_by_page = _extract_pdf_tables(source_path)
    if not tables_by_page:
        return docs

    for doc in docs:
        page = doc.metadata.get("page")
        if page not in tables_by_page:
            continue
        table_text = tables_by_page[page]
        doc.page_content = f"{doc.page_content.rstrip()}\n\n[Extracted Tables]\n{table_text}"
        doc.metadata["contains_tables"] = True
    return docs


def _maybe_redact(docs: list[Document], handler) -> None:
    """Apply ingest-time PII redaction in place when policy says so.

    Counts are stored as flat scalar metadata keys (``pii_redaction_total``
    and ``pii_redaction_<type>``) rather than a nested dict so Chroma's
    metadata schema (scalar / list / None) accepts the upsert.
    """
    if not _should_redact(
        handler,
        global_flag=config.PII_REDACT_AT_INGEST,
        tabular_flag=config.PII_REDACT_TABULAR_AT_INGEST,
    ):
        return
    for doc in docs:
        redacted_text, counts = _pii_redact(doc.page_content)
        doc.page_content = redacted_text
        if counts:
            doc.metadata["pii_redaction_total"] = sum(counts.values())
            for pii_type, n in counts.items():
                doc.metadata[f"pii_redaction_{pii_type}"] = n


def load_documents(docs_dir: str = config.DOCS_DIR):
    """Walk the docs directory and load all supported files."""
    documents = []
    for root, _dirs, files in os.walk(docs_dir):
        for fname in sorted(files):
            ext = os.path.splitext(fname)[1].lower()
            handler = handler_for(ext)
            if handler is None:
                continue
            path = os.path.join(root, fname)
            try:
                docs = handler.loader(path)
                _maybe_redact(docs, handler)
                text_hint = "\n".join(doc.page_content[:4000] for doc in docs[:2])
                source_metadata = build_source_metadata(path, text_hint)
                for doc in docs:
                    doc.metadata.setdefault("source", path)
                    doc.metadata.update(source_metadata)
                documents.extend(docs)
                print(f"  Loaded {len(docs)} page(s) from {fname}")
            except Exception as exc:
                print(f"  WARN: failed to load {fname}: {exc}")
    return documents


def _recursive_splitter(chunk_size: int = config.CHUNK_SIZE, chunk_overlap: int = config.CHUNK_OVERLAP):
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


def _split_markdown(doc: Document) -> list[Document]:
    """Header-aware split for markdown documents."""
    header_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=MARKDOWN_HEADERS)
    sections = header_splitter.split_text(doc.page_content)
    sub_splitter = _recursive_splitter(chunk_size=900, chunk_overlap=100)

    chunks: list[Document] = []
    for section in sections:
        title_parts = [section.metadata[key] for _, key in MARKDOWN_HEADERS if section.metadata.get(key)]
        section_title = " > ".join(title_parts) if title_parts else None
        for piece in sub_splitter.split_documents([section]):
            piece.metadata = {**doc.metadata, **piece.metadata}
            if section_title:
                piece.metadata["section_title"] = section_title
            chunks.append(piece)
    return chunks


def _split_pdf_page(page: Document, current_section: str | None) -> tuple[list[Document], str | None]:
    """Split one PDF page along SEC section markers; return chunks and the trailing section."""
    text = page.page_content
    matches = list(SEC_SECTION_RE.finditer(text))
    sub_splitter = _recursive_splitter(chunk_size=900, chunk_overlap=100)

    if not matches:
        chunks = sub_splitter.create_documents([text])
        for piece in chunks:
            piece.metadata = {**page.metadata}
            if current_section:
                piece.metadata["section_title"] = current_section
        return chunks, current_section

    boundaries = [0] + [m.start() for m in matches] + [len(text)]
    titles_for_segment = [current_section] + [m.group().strip() for m in matches]

    chunks: list[Document] = []
    for i in range(len(boundaries) - 1):
        segment = text[boundaries[i]:boundaries[i + 1]]
        if not segment.strip():
            continue
        section_title = titles_for_segment[i]
        for piece in sub_splitter.create_documents([segment]):
            piece.metadata = {**page.metadata}
            if section_title:
                piece.metadata["section_title"] = section_title
            chunks.append(piece)

    return chunks, matches[-1].group().strip()


def _split_pdf(pages: list[Document]) -> list[Document]:
    """Section-aware split across all pages of one PDF, carrying section state forward."""
    chunks: list[Document] = []
    current_section: str | None = None
    for page in pages:
        page_chunks, current_section = _split_pdf_page(page, current_section)
        chunks.extend(page_chunks)
    return chunks


def split_documents(documents: list[Document]) -> list[Document]:
    """Registry-driven splitter dispatch by file extension."""
    by_source: dict[str, list[Document]] = {}
    for doc in documents:
        by_source.setdefault(doc.metadata.get("source", ""), []).append(doc)

    chunks: list[Document] = []
    fallback = _recursive_splitter()

    for source, source_docs in by_source.items():
        ext = Path(source).suffix.lower()
        handler = handler_for(ext)
        if handler is not None and handler.splitter is not None:
            chunks.extend(handler.splitter(source_docs))
        else:
            chunks.extend(fallback.split_documents(source_docs))

    print(f"  Split into {len(chunks)} chunks")
    return chunks


def chunk_id(chunk: Document) -> str:
    """Stable ID for a chunk: same (source, page, content) -> same id."""
    source_name = Path(chunk.metadata.get("source", "")).name
    page = chunk.metadata.get("page", "")
    content_hash = hashlib.sha1(chunk.page_content.encode("utf-8")).hexdigest()[:12]
    return f"{source_name}:{page}:{content_hash}"


def chunk_ids(chunks: list[Document]) -> list[str]:
    return [chunk_id(c) for c in chunks]


def _dedupe_by_id(chunks: list[Document]) -> tuple[list[Document], list[str]]:
    seen: dict[str, Document] = {}
    for chunk in chunks:
        seen[chunk_id(chunk)] = chunk
    unique_ids = list(seen.keys())
    unique_chunks = list(seen.values())
    return unique_chunks, unique_ids


def embed_and_persist(chunks):
    """Embed chunks and upsert into Chroma using stable ids."""
    embeddings = BedrockEmbeddings(
        model_id=config.EMBEDDING_MODEL_ID,
        region_name=config.AWS_REGION,
    )
    vectorstore = Chroma(
        collection_name=config.CHROMA_COLLECTION,
        persist_directory=config.CHROMA_PERSIST_DIR,
        embedding_function=embeddings,
    )
    unique_chunks, unique_ids = _dedupe_by_id(chunks)
    vectorstore.add_documents(documents=unique_chunks, ids=unique_ids)
    print(
        f"  Upserted {len(unique_chunks)} chunks "
        f"(input {len(chunks)}, dedup-collapsed {len(chunks) - len(unique_chunks)}) "
        f"to {config.CHROMA_PERSIST_DIR}"
    )
    return vectorstore


def run():
    """Full ingestion pipeline."""
    print("Loading documents...")
    documents = load_documents()
    if not documents:
        print("No documents found in", config.DOCS_DIR)
        sys.exit(1)

    print("Splitting...")
    chunks = split_documents(documents)

    print("Embedding and persisting...")
    vectorstore = embed_and_persist(chunks)

    print("Done. Collection:", config.CHROMA_COLLECTION)
    return vectorstore


if __name__ == "__main__":
    run()
