"""Bounded, deterministic Chroma upserts shared by every ingestion path.

Chroma refuses a single write larger than the client's own maximum batch size
(5,461 on the installed client), and one real 10-K comfortably exceeds it — the
largest filing in the benchmark corpus produces roughly 5.8k chunks on its own.
Every path that writes chunks into a vector store therefore goes through
``add_documents_in_batches`` rather than calling ``add_documents`` directly.

What this module is
-------------------
Only a delivery mechanism. It slices an already-built payload into contiguous
client-sized batches and writes them in order. It does not chunk, re-chunk,
normalize, sort, deduplicate, re-embed, retry, sleep, or parallelize. Same
documents, same stable ids, same order, same metadata — the only difference is
how many records travel per call.

What it is not
--------------
**Chroma writes are not transactional and this module does not make them so.**
There is no multi-batch atomic write available at this layer, so when batch N
fails the records from batches 1..N-1 remain physically present in the store.
This is deliberately not hidden: the caller learns exactly how many items
completed, records no completion in the filing registry, and an explicit rerun
upserts the same deterministic ids to finish the job. That is *deterministic
idempotent upsert on explicit rerun* — it is not exactly-once insertion, and no
part of this repository claims otherwise.

Batch size
----------
Discovered from the client (``get_max_batch_size``), never guessed. If the
client cannot report a usable limit the write fails closed with a stable
configuration error rather than issuing an unbounded call: a guessed bound is
not a bound, and a wrong guess reproduces exactly the failure this module
exists to remove. Callers that must proceed against such a client pass an
explicit ``batch_size``; tests use the same parameter to exercise boundaries
without building 5k-item payloads.

Error hygiene
-------------
Raised errors carry an operation label, a stable code, and counters only. They
never carry document text, metadata values, ids, filesystem paths, or the
client's own exception message — a Chroma error can quote the offending record
and the persist directory, so the cause is dropped (``raise ... from None``)
rather than chained into a traceback a CLI might print.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

# Attribute names that may hold the chromadb client on a store object, in the
# order they are tried. ``_client`` is what langchain_chroma's Chroma exposes;
# the others let a plain chromadb collection or a differently-wrapped store
# work without this module importing chromadb at all.
_CLIENT_ATTRS = ("_client", "_chroma_client", "client")

# Stable failure codes. These are part of the contract: they appear in logs and
# in test assertions, and they never vary with the underlying client's wording.
CODE_PAYLOAD_LENGTH_MISMATCH = "chroma_batch_payload_length_mismatch"
CODE_INVALID_ID = "chroma_batch_invalid_id"
CODE_INVALID_BATCH_SIZE = "chroma_batch_invalid_batch_size"
CODE_BATCH_SIZE_UNAVAILABLE = "chroma_batch_size_unavailable"
CODE_WRITE_FAILED = "chroma_batch_write_failed"


class ChromaBatchError(RuntimeError):
    """Base for every bounded-write failure. Carries a stable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ChromaBatchConfigurationError(ChromaBatchError):
    """The payload or the discovered batch size is unusable; nothing written."""


class ChromaBatchWriteError(ChromaBatchError):
    """A batch write failed. Earlier batches may remain in the store."""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        batch_index: int,
        batch_count: int,
        total_items: int,
        completed_items: int,
        effective_batch_size: int,
        client_error_type: str,
    ):
        super().__init__(CODE_WRITE_FAILED, message)
        self.operation = operation
        self.batch_index = batch_index
        self.batch_count = batch_count
        self.total_items = total_items
        self.completed_items = completed_items
        self.effective_batch_size = effective_batch_size
        # A class name, never a message: provably free of document content,
        # metadata values, and paths, and the same convention the filing
        # registry already uses for ``error_code``.
        self.client_error_type = client_error_type


@dataclass(frozen=True)
class BatchWriteResult:
    """Narrow, content-free outcome of one bounded write.

    Counts only. No documents, no metadata, no embeddings, no ids, no paths,
    and no client internals, so this is safe to log and safe to surface.
    """

    total_items: int
    batch_count: int
    effective_batch_size: int
    completed_items: int


def _is_positive_int(value: Any) -> bool:
    """True for a genuine positive int.

    ``bool`` is a subclass of ``int`` in Python, so ``True`` would otherwise
    pass as batch size 1 and turn a misconfiguration into 5,000 single-item
    writes instead of an error.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _client_reported_batch_size(store: Any) -> Any:
    """Ask the store's client for its maximum batch size, or return None.

    Returns whatever the client reported without judging it; validation is the
    caller's job so that an invalid report and a missing one produce distinct,
    accurate errors.
    """
    candidates = [getattr(store, attr, None) for attr in _CLIENT_ATTRS]
    candidates.append(store)
    for candidate in candidates:
        if candidate is None:
            continue
        getter = getattr(candidate, "get_max_batch_size", None)
        if callable(getter):
            return getter()
    return None


def resolve_batch_size(store: Any, *, explicit: int | None = None) -> int:
    """The effective per-write item limit.

    An explicit value wins (tests inject small limits; an operator can use it
    against a client that cannot report its own bound). Otherwise the client is
    asked. There is no fallback constant: an unbounded write is never issued,
    and a guessed bound would not be a bound.

    Raises ChromaBatchConfigurationError when the explicit value is invalid,
    when the client reports something that is not a positive int (booleans
    included), or when no client exposes the capability at all.
    """
    if explicit is not None:
        if not _is_positive_int(explicit):
            raise ChromaBatchConfigurationError(
                CODE_INVALID_BATCH_SIZE,
                f"{CODE_INVALID_BATCH_SIZE}: explicit batch size must be a "
                f"positive int, got {type(explicit).__name__}",
            )
        return explicit

    try:
        reported = _client_reported_batch_size(store)
    except Exception:  # noqa: BLE001 - a raising client is an unusable client
        raise ChromaBatchConfigurationError(
            CODE_BATCH_SIZE_UNAVAILABLE,
            f"{CODE_BATCH_SIZE_UNAVAILABLE}: the vector store client raised "
            "when asked for its maximum batch size; pass an explicit "
            "batch_size to proceed",
        ) from None

    if reported is None:
        raise ChromaBatchConfigurationError(
            CODE_BATCH_SIZE_UNAVAILABLE,
            f"{CODE_BATCH_SIZE_UNAVAILABLE}: the vector store client does not "
            "report a maximum batch size; pass an explicit batch_size to "
            "proceed",
        )
    if not _is_positive_int(reported):
        raise ChromaBatchConfigurationError(
            CODE_INVALID_BATCH_SIZE,
            f"{CODE_INVALID_BATCH_SIZE}: the vector store client reported a "
            f"maximum batch size of type {type(reported).__name__}, which is "
            "not a positive int",
        )
    return reported


def _validate_payload(documents: Sequence[Any], ids: Sequence[str]) -> None:
    """Length agreement and id shape only.

    Deliberately *not* checked here: id uniqueness. Deduplication is the
    caller's contract (``ingest._dedupe_by_id`` runs before every production
    call), and rejecting duplicates here would impose a new id policy on
    callers that legitimately pass a list this module did not build.
    """
    if len(documents) != len(ids):
        raise ChromaBatchConfigurationError(
            CODE_PAYLOAD_LENGTH_MISMATCH,
            f"{CODE_PAYLOAD_LENGTH_MISMATCH}: {len(documents)} documents and "
            f"{len(ids)} ids",
        )
    for position, item in enumerate(ids):
        if not isinstance(item, str) or not item:
            raise ChromaBatchConfigurationError(
                CODE_INVALID_ID,
                f"{CODE_INVALID_ID}: id at position {position} is empty or not "
                "a string",
            )


def add_documents_in_batches(
    store: Any,
    documents: Sequence[Any],
    ids: Sequence[str],
    *,
    batch_size: int | None = None,
    operation: str = "chroma_upsert",
) -> BatchWriteResult:
    """Upsert ``documents`` under ``ids`` in contiguous client-sized batches.

    Batch k covers ``items[k * size : (k + 1) * size]`` — deterministic
    boundaries, no overlap, no omission, no reordering. An exact multiple of
    the limit produces a final full batch; one item more produces a one-item
    final batch. An empty payload writes nothing and returns a zero result.

    ``store`` is written through its existing ``add_documents(documents=, ids=)``
    entry point, which upserts, so a rerun over the same deterministic ids
    replaces rather than duplicates.

    Raises ChromaBatchConfigurationError before any write when the payload or
    batch size is unusable, and ChromaBatchWriteError when a batch fails —
    the latter reporting how many items completed. Earlier batches are not
    rolled back; there is no transaction spanning them.
    """
    documents = list(documents)
    ids = list(ids)
    _validate_payload(documents, ids)

    # Resolution happens before the first write (and even for an empty payload)
    # so an unusable configuration fails as configuration, never halfway
    # through a corpus.
    size = resolve_batch_size(store, explicit=batch_size)

    total = len(documents)
    if total == 0:
        return BatchWriteResult(
            total_items=0,
            batch_count=0,
            effective_batch_size=size,
            completed_items=0,
        )

    batch_count = (total + size - 1) // size
    completed = 0
    for index, start in enumerate(range(0, total, size)):
        stop = min(start + size, total)
        try:
            store.add_documents(
                documents=documents[start:stop], ids=ids[start:stop]
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as a sanitized error
            raise ChromaBatchWriteError(
                f"{CODE_WRITE_FAILED}: {operation} failed on batch "
                f"{index + 1} of {batch_count} after {completed} of {total} "
                f"items (batch size {size}, client error "
                f"{type(exc).__name__})",
                operation=operation,
                batch_index=index,
                batch_count=batch_count,
                total_items=total,
                completed_items=completed,
                effective_batch_size=size,
                client_error_type=type(exc).__name__,
            ) from None  # the client's message can quote records and paths
        completed = stop

    return BatchWriteResult(
        total_items=total,
        batch_count=batch_count,
        effective_batch_size=size,
        completed_items=completed,
    )
