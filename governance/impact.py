"""Document change impact analysis (Governance_layer.md §8).

Answer one question: when a document changes, which past answers used the chunks
that are now gone or different, and should those answers be re-checked?

The pieces:
- load_current_chunks: pull a document's chunks from Chroma (read-only).
- compute_new_chunks: re-run ingest's load + split on a new file version, in
  memory, without touching Chroma.
- diff_chunks: compare old vs new and report added / removed / modified.
- scan_audit_log: stream the JSONL audit log and find past queries that used the
  affected chunks.
- build_impact_report: assemble the §8.3 output shape.

This module reads from the ingest pipeline and Chroma; it never writes either.

Chunk identity (read this before changing anything)
---------------------------------------------------
The ingest pipeline keys Chroma by ``source_name:page:sha1(content)`` (see
ingest.chunk_id). The audit log predates a chunk_id field: each retrieved source
carries only ``source_name``, ``page``, and a whitespace-normalized ``excerpt``
truncated to 700 chars. So the real ingest chunk_id cannot be reconstructed from
the audit log.

To match past queries against affected chunks on the EXISTING log history (no
re-ingest, no log enrichment, so it works on day one), PR3 uses an
audit-reproducible identity instead:

    chunk_id = f"{source_name}:{page_token}:{sha1(normalized_excerpt[:700])[:12]}"

Both a Chroma chunk (full content known) and an audit retrieved source (excerpt
stored) produce the same string for the same underlying text, because the audit
excerpt was derived by the same transform. ``content_hash`` (sha1 of the full
content) is carried separately and drives the diff, which compares full content
rather than the 700-char prefix.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import config

EXCERPT_CHARS = 700  # must match audit.MAX_EXCERPT_CHARS so fingerprints align


def _normalized_excerpt(text: str) -> str:
    """Whitespace-normalize and truncate text the same way audit.py does."""
    return " ".join((text or "").split())[:EXCERPT_CHARS]


def _excerpt_hash(excerpt: str) -> str:
    """Fingerprint of an already-normalized excerpt (audit-reproducible)."""
    return hashlib.sha1(excerpt.encode("utf-8")).hexdigest()[:12]


def _content_hash(text: str) -> str:
    """Full-content hash, used for the diff (not reconstructable from audit)."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def _page_token(page: Any) -> str:
    """Normalize a page value to a string token (None / '' both mean no page)."""
    if page is None or page == "":
        return ""
    return str(page)


def _chunk_id(source_name: str, page: Any, excerpt: str) -> str:
    """The audit-reproducible chunk identity. See the module docstring."""
    return f"{source_name}:{_page_token(page)}:{_excerpt_hash(excerpt)}"


def _make_chunk(
    *,
    content: str,
    source_name: str,
    page: Any,
    section_title: str | None,
    source: str | None,
    document_id: str | None,
    document_version: str | None,
    chroma_id: str | None,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build the common chunk dict shape used across this module."""
    excerpt = _normalized_excerpt(content)
    return {
        "chunk_id": _chunk_id(source_name, page, excerpt),
        "chroma_id": chroma_id,
        "content_hash": _content_hash(content),
        "source": source,
        "source_name": source_name,
        "page": page,
        "section_title": section_title,
        "document_id": document_id,
        "document_version": document_version,
        "content": content,
        "excerpt": excerpt,
        "metadata": metadata,
    }


def load_current_chunks(document_id: str, chroma_client) -> list[dict[str, Any]]:
    """Fetch all chunks for a document_id from Chroma (read-only).

    ``chroma_client`` is anything exposing Chroma's ``get`` (a langchain_chroma
    ``Chroma`` or a raw chromadb collection). Returns chunk dicts; empty list if
    the document_id is not present.
    """
    result = chroma_client.get(
        where={"document_id": document_id},
        include=["metadatas", "documents"],
    )
    ids = result.get("ids") or []
    documents = result.get("documents") or []
    metadatas = result.get("metadatas") or []

    chunks: list[dict[str, Any]] = []
    for chroma_id, content, metadata in zip(ids, documents, metadatas):
        metadata = metadata or {}
        chunks.append(
            _make_chunk(
                content=content or "",
                source_name=metadata.get("source_name") or Path(metadata.get("source", "")).name,
                page=metadata.get("page"),
                section_title=metadata.get("section_title"),
                source=metadata.get("source"),
                document_id=metadata.get("document_id"),
                document_version=metadata.get("document_version"),
                chroma_id=chroma_id,
                metadata=metadata,
            )
        )
    return chunks


def compute_new_chunks(source_path: str) -> list[dict[str, Any]]:
    """Produce chunks from a file using ingest's load + split, WITHOUT writing.

    Pure: re-uses ingest.handler_for / build_source_metadata / split_documents and
    never calls embed_and_persist, so Chroma is untouched. Same return shape as
    load_current_chunks (chroma_id is None since these chunks are not persisted).
    """
    import ingest

    path = Path(source_path)
    handler = ingest.handler_for(path.suffix.lower())
    if handler is None:
        raise ValueError(f"No loader registered for {path.suffix!r}")

    docs = handler.loader(str(path))
    ingest._maybe_redact(docs, handler)
    text_hint = "\n".join(doc.page_content[:4000] for doc in docs[:2])
    source_metadata = ingest.build_source_metadata(str(path), text_hint)
    for doc in docs:
        doc.metadata.setdefault("source", str(path))
        doc.metadata.update(source_metadata)

    split = ingest.split_documents(docs)

    chunks: list[dict[str, Any]] = []
    for doc in split:
        metadata = doc.metadata or {}
        chunks.append(
            _make_chunk(
                content=doc.page_content or "",
                source_name=metadata.get("source_name") or path.name,
                page=metadata.get("page"),
                section_title=metadata.get("section_title"),
                source=metadata.get("source"),
                document_id=metadata.get("document_id"),
                document_version=metadata.get("document_version"),
                chroma_id=None,
                metadata=metadata,
            )
        )
    return chunks


def _position_key(chunk: dict[str, Any]) -> tuple[str, str]:
    """Source-independent position group for pairing modified chunks."""
    return (_page_token(chunk.get("page")), chunk.get("section_title") or "")


def diff_chunks(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> dict[str, list]:
    """Compare two chunk lists by full content. Return added / removed / modified.

    The comparison is on ``content_hash`` (full content), which is source-filename
    independent so a new version supplied under a different path still compares
    cleanly. A chunk present in both (same content_hash) is unchanged and omitted.

    "modified" given the content-hashed identity: an in-place edit cannot be seen
    as a single chunk whose id stayed put, because any content change produces a
    new content_hash. So a removed old chunk plus an added new chunk that share a
    position group (page + section_title) are paired and reported as "modified".
    Unpaired leftovers stay in removed / added. This pairing is presentational;
    the affected-old set (every old chunk whose content is gone from new) drives
    the audit scan regardless of how it is split.
    """
    old_hashes = {c["content_hash"] for c in old}
    new_hashes = {c["content_hash"] for c in new}

    removed = [c for c in old if c["content_hash"] not in new_hashes]
    added = [c for c in new if c["content_hash"] not in old_hashes]

    # Pair removed and added within the same position group as "modified".
    removed_by_pos: dict[tuple, list] = {}
    added_by_pos: dict[tuple, list] = {}
    for c in removed:
        removed_by_pos.setdefault(_position_key(c), []).append(c)
    for c in added:
        added_by_pos.setdefault(_position_key(c), []).append(c)

    modified: list[dict[str, Any]] = []
    paired_removed: set[int] = set()
    paired_added: set[int] = set()
    for pos, old_group in removed_by_pos.items():
        new_group = added_by_pos.get(pos, [])
        for old_chunk, new_chunk in zip(old_group, new_group):
            modified.append({"old": old_chunk, "new": new_chunk})
            paired_removed.add(id(old_chunk))
            paired_added.add(id(new_chunk))

    pure_removed = [c for c in removed if id(c) not in paired_removed]
    pure_added = [c for c in added if id(c) not in paired_added]

    return {"added": pure_added, "removed": pure_removed, "modified": modified}


def affected_old_chunks(diff: dict[str, list]) -> list[dict[str, Any]]:
    """Old chunks gone from the new version: every removed plus modified-old."""
    return list(diff.get("removed", [])) + [m["old"] for m in diff.get("modified", [])]


def _audit_source_chunk_id(source: dict[str, Any]) -> str:
    """Reconstruct a retrieved source's chunk identity from its audit fields."""
    source_name = source.get("source_name") or Path(source.get("source", "")).name
    return _chunk_id(source_name, source.get("page"), source.get("excerpt") or "")


def scan_audit_log(log_path: str, affected_chunk_ids: set[str]) -> list[dict[str, Any]]:
    """Stream the JSONL audit log; return past queries that used affected chunks.

    ``affected_chunk_ids`` are the audit-reproducible chunk ids (see module
    docstring) of the chunks that changed. Each returned entry is one
    (query, used-old-chunk) match: auditId, timestamp, question, usedOldChunk.
    Deduped on (auditId, usedOldChunk).
    """
    if not affected_chunk_ids:
        return []

    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    path = Path(log_path)
    if not path.exists():
        return []

    with path.open(encoding="utf-8") as log_file:
        for line in log_file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            for source in record.get("retrieved_sources", []) or []:
                cid = _audit_source_chunk_id(source)
                if cid not in affected_chunk_ids:
                    continue
                audit_id = record.get("audit_id")
                key = (str(audit_id), cid)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(
                    {
                        "auditId": audit_id,
                        "timestamp": record.get("timestamp"),
                        "question": record.get("query"),
                        "usedOldChunk": cid,
                    }
                )
    return matches


def build_impact_report(
    document_id: str,
    diff: dict[str, list],
    affected_queries: list[dict[str, Any]],
    *,
    old_version_hash: str | None = None,
    new_version_hash: str | None = None,
) -> dict[str, Any]:
    """Assemble the §8.3 impact report. requiresReevaluation = any affected query."""
    affected_old = affected_old_chunks(diff)
    new_chunks = list(diff.get("added", [])) + [m["new"] for m in diff.get("modified", [])]

    return {
        "documentId": document_id,
        "oldVersionHash": old_version_hash,
        "newVersionHash": new_version_hash,
        "changedChunks": [c["chunk_id"] for c in affected_old],
        "newChunks": [c["chunk_id"] for c in new_chunks],
        "affectedPastQueries": affected_queries,
        "requiresReevaluation": bool(affected_queries),
    }


def open_chroma():
    """Open the persisted Chroma collection read-only, mirroring ingest config."""
    from langchain_aws import BedrockEmbeddings
    from langchain_chroma import Chroma

    embeddings = BedrockEmbeddings(
        model_id=config.EMBEDDING_MODEL_ID,
        region_name=config.AWS_REGION,
    )
    return Chroma(
        collection_name=config.CHROMA_COLLECTION,
        persist_directory=config.CHROMA_PERSIST_DIR,
        embedding_function=embeddings,
    )


def document_version(chunks: list[dict[str, Any]]) -> str | None:
    """Most common document_version across chunks, or None."""
    versions = [c.get("document_version") for c in chunks if c.get("document_version")]
    if not versions:
        return None
    return max(set(versions), key=versions.count)
