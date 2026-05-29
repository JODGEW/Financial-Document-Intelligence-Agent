"""CSV format handler.

Tabular family — eligible for ingest-time PII redaction by default. The
loader emits the whole CSV as one markdown table in ``page_content``, prefixed
with a human-readable description block so retrieval has semantic anchors
(filename, row count, column names). The splitter then chunks the
already-redacted text by row, repeating both the description prefix and the
markdown header on every chunk so context survives across chunks.

Doing the row-by-row chunking on the redacted markdown (rather than re-reading
the raw CSV) is critical: re-reading would silently undo any PII redaction
that ``ingest._maybe_redact`` applied between load and split.
"""

from __future__ import annotations

import csv as _csv
from pathlib import Path

from langchain_core.documents import Document

from .common import humanize_stem
from .registry import FormatHandler, register
from .tables import format_table_rows


CSV_ROWS_PER_CHUNK = 20


def _description(path: str, header: list[str], row_count: int) -> str:
    human = humanize_stem(path)
    cols = ", ".join(h.strip() for h in header if h and h.strip()) if header else "(no header)"
    return (
        f"Document: {human}\n"
        f"File: {Path(path).name}\n"
        f"Rows: {row_count}\n"
        f"Columns: {cols}"
    )


def _load(path: str) -> list[Document]:
    rows: list[list[str]] = []
    with open(path, newline="", encoding="utf-8", errors="ignore") as csvfile:
        reader = _csv.reader(csvfile)
        for row in reader:
            rows.append(row)

    if not rows:
        return []

    header = rows[0] if rows else []
    body_count = max(0, len(rows) - 1)
    description = _description(path, header, body_count)
    table_text = format_table_rows(rows)
    return [
        Document(
            page_content=f"{description}\n\n{table_text}",
            metadata={
                "source": path,
                "csv_total_rows": len(rows),
            },
        )
    ]


def _slice_doc_lines(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Return (description_prefix, table_header, body_rows).

    The description prefix is every line before the first ``| ... |`` line.
    The table header is the first two ``|`` lines (markdown header + separator)
    when both are present, otherwise empty.
    """
    table_start = None
    for i, line in enumerate(lines):
        if line.startswith("|"):
            table_start = i
            break

    if table_start is None:
        return lines, [], []

    prefix = lines[:table_start]
    rest = lines[table_start:]
    if len(rest) >= 2 and rest[1].startswith("|") and "---" in rest[1]:
        return prefix, rest[:2], rest[2:]
    return prefix, [], rest


def _split(docs: list[Document]) -> list[Document]:
    """Chunk the markdown-formatted CSV by row; repeat the description + header on every chunk."""
    chunks: list[Document] = []
    for doc in docs:
        lines = doc.page_content.split("\n")
        if not lines:
            continue

        prefix, header_block, body_lines = _slice_doc_lines(lines)
        # Drop blank separator lines between prefix and header (we add them back).
        while prefix and not prefix[-1].strip():
            prefix.pop()

        if len(body_lines) <= CSV_ROWS_PER_CHUNK:
            chunks.append(doc)
            continue

        total_chunks = (len(body_lines) + CSV_ROWS_PER_CHUNK - 1) // CSV_ROWS_PER_CHUNK
        for chunk_index in range(total_chunks):
            start = chunk_index * CSV_ROWS_PER_CHUNK
            end = start + CSV_ROWS_PER_CHUNK
            assembled: list[str] = []
            if prefix:
                assembled.extend(prefix)
                assembled.append("")  # blank line between prefix and table
            assembled.extend(header_block)
            assembled.extend(body_lines[start:end])
            metadata = {
                **doc.metadata,
                "csv_chunk_index": chunk_index,
                "csv_chunk_first_row": start,
                "csv_chunk_last_row": min(end, len(body_lines)) - 1,
            }
            chunks.append(Document(page_content="\n".join(assembled), metadata=metadata))
    return chunks


register(
    FormatHandler(
        extensions=(".csv",),
        loader=_load,
        splitter=_split,
        format_family="tabular",
    )
)
