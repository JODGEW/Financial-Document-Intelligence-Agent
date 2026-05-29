"""JSON / JSONL format handler.

For ``.json``: walks the parsed object and emits one ``Document`` per leaf
record. The jq-style key path that reaches each leaf becomes ``section_title``
so retrieval can ground answers on where the value lived in the tree.

For ``.jsonl``: one record per line → one ``Document`` per line. The line
number is the section title fallback.

A "leaf record" is a dict whose values are all scalar/list, or any non-dict
that lives one level below a dict. This stops the walker from sub-chunking
inside a flat dict and keeps small records intact for retrieval. Listed
arrays of scalars are kept attached to their parent record rather than
becoming their own Documents.

Filename ``json_doc.py`` avoids shadowing the stdlib ``json`` module on the
import path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from .common import humanize_stem
from .registry import FormatHandler, register


def _format_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _is_leaf_record(node: Any) -> bool:
    """A leaf is anything that isn't a dict containing further records.

    We descend through dicts when any value is a dict OR a list-of-dicts;
    otherwise the dict itself is the leaf record. Scalars and primitive lists
    are always leaves.
    """
    if not isinstance(node, dict):
        return True
    for value in node.values():
        if isinstance(value, dict):
            return False
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            return False
    return True


def _format_record(node: Any) -> str:
    if isinstance(node, dict):
        lines = []
        for key, value in node.items():
            lines.append(f"{key}: {_format_value(value)}")
        return "\n".join(lines)
    if isinstance(node, list):
        return _format_value(node)
    return _format_value(node)


def _walk(node: Any, path: str, records: list[tuple[str, Any]]) -> None:
    """Collect (path, leaf_record) pairs by walking the JSON tree."""
    if _is_leaf_record(node):
        records.append((path, node))
        return

    if isinstance(node, dict):
        for key, value in node.items():
            next_path = f"{path}.{key}" if path else key
            if isinstance(value, list):
                for index, item in enumerate(value):
                    item_path = f"{next_path}[{index}]"
                    _walk(item, item_path, records)
            else:
                _walk(value, next_path, records)


def _document_header(source_path: str, key_path: str) -> str:
    human = humanize_stem(source_path)
    return f"Document: {human}\nFile: {Path(source_path).name}\nPath: {key_path or '$'}"


def _load_json(path: str) -> list[Document]:
    raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    parsed = json.loads(raw)

    records: list[tuple[str, Any]] = []
    if isinstance(parsed, list):
        for index, item in enumerate(parsed):
            _walk(item, f"[{index}]", records)
    else:
        _walk(parsed, "", records)

    if not records:
        # Empty container / scalar at the root — emit the whole file.
        records = [("", parsed)]

    docs: list[Document] = []
    for key_path, leaf in records:
        body = _format_record(leaf)
        header = _document_header(path, key_path)
        page_content = f"{header}\n\n{body}".strip()
        section_title = key_path or "$"
        docs.append(
            Document(
                page_content=page_content,
                metadata={
                    "source": path,
                    "section_title": section_title,
                    "json_key_path": section_title,
                },
            )
        )
    return docs


def _load_jsonl(path: str) -> list[Document]:
    docs: list[Document] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line_number, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                print(
                    f"  WARN: skipping malformed JSONL line {line_number} in "
                    f"{Path(path).name}: {exc}"
                )
                continue
            body = _format_record(record)
            section_title = f"Record {line_number}"
            header = (
                f"Document: {humanize_stem(path)}\n"
                f"File: {Path(path).name}\n"
                f"Path: {section_title}"
            )
            docs.append(
                Document(
                    page_content=f"{header}\n\n{body}".strip(),
                    metadata={
                        "source": path,
                        "section_title": section_title,
                        "jsonl_record_index": line_number,
                    },
                )
            )
    return docs


def _load(path: str) -> list[Document]:
    suffix = Path(path).suffix.lower()
    if suffix == ".jsonl":
        return _load_jsonl(path)
    return _load_json(path)


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
        extensions=(".json", ".jsonl"),
        loader=_load,
        splitter=_split,
        format_family="structured",
    )
)
