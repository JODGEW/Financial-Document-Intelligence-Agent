"""File-backed human review queue (Governance_layer.md §7.8, Phase 5).

Persists answers flagged for human review as JSONL under a queue directory.
Three files: pending.jsonl, approved.jsonl, rejected.jsonl. The agent enqueues a
held item; the review_queue CLI lists, shows, approves, and rejects.

Approve/reject moves an item from pending into the matching terminal file and
stamps it with a status, timestamp, and optional note. It does not mutate the
audit log and does not re-deliver the held answer; both are out of scope for this
phase.

Durability and concurrency model:

- Every file write is atomic: content is serialized fully in memory, written to
  a randomly named temp file in the queue directory, flushed, fsynced, then
  os.replace'd over the destination. A queue file is therefore always either
  its previous complete content or its new complete content — never truncated,
  never half-written.
- Write transitions (enqueue, approve, reject) are serialized by one
  module-level lock, which covers the whole read-check-write sequence across
  BOTH files involved in a resolution. This matches the actual execution
  model: one uvicorn worker whose sync handlers run on a threadpool.
  Cross-process concurrency (e.g. the CLI racing the API) is NOT protected —
  a known limitation of the JSONL design, acceptable for a local
  single-operator pilot and resolved properly only by a real database.
- The two-file move (terminal file first, then pending) is not transactional:
  a process killed between the two replacements leaves the item in both files.
  That ordering is deliberate — the item and its decision are never lost; the
  reverse order could lose both. The duplicate state is detected on the next
  resolution attempt, which honors the terminal decision, repairs pending, and
  reports the item as already resolved.

All functions take the queue directory as a parameter so callers (agent, CLI,
API, tests) control where the files live. This module is the only reader and
writer of queue files; the API never opens them directly.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PENDING_FILE = "pending.jsonl"
APPROVED_FILE = "approved.jsonl"
REJECTED_FILE = "rejected.jsonl"

_STATUS_FILES = {
    "pending": PENDING_FILE,
    "approved": APPROVED_FILE,
    "rejected": REJECTED_FILE,
}

# One lock for every queue write transition. A single lock (rather than
# per-file locks) is what keeps an item from being observed mid-move across
# pending and a terminal file by another writer.
_QUEUE_LOCK = threading.RLock()


def _read_items(path: Path) -> list[dict[str, Any]]:
    """Read JSONL items from a queue file, returning [] when it is absent."""
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def _atomic_write_items(path: Path, items: list[dict[str, Any]]) -> None:
    """Atomically replace a queue file with the given items.

    Serialize first (a serialization failure touches nothing on disk), then
    write to a randomly named temp file in the destination directory, flush,
    fsync, and os.replace. On any failure the temp file is removed and the
    original destination is left byte-for-byte intact.
    """
    lines = "".join(json.dumps(item, default=str) + "\n" for item in items)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def enqueue(item: dict[str, Any], queue_dir: str | Path) -> dict[str, Any]:
    """Add a held review item to pending.jsonl (atomic rewrite, under the lock)."""
    path = Path(queue_dir) / PENDING_FILE
    with _QUEUE_LOCK:
        items = _read_items(path)
        items.append(item)
        _atomic_write_items(path, items)
    return item


def list_pending(queue_dir: str | Path) -> list[dict[str, Any]]:
    """Return pending review items (empty list when the file is absent)."""
    return _read_items(Path(queue_dir) / PENDING_FILE)


def get(review_id: str, queue_dir: str | Path) -> dict[str, Any] | None:
    """Return a pending review item by id, or None when it is not pending."""
    for item in list_pending(queue_dir):
        if item.get("reviewId") == review_id:
            return item
    return None


def list_items(
    queue_dir: str | Path, status: str
) -> list[tuple[dict[str, Any], str]]:
    """Return (item, status) pairs for one status, or across all three files.

    status is one of pending, approved, rejected, or all. For "all", items come
    back in file order: pending, then approved, then rejected. The status in
    each pair is derived from the file the item was read from, not from the
    item body. Unknown status raises ValueError.
    """
    if status != "all" and status not in _STATUS_FILES:
        raise ValueError(f"unknown review status: {status!r}")
    statuses = list(_STATUS_FILES) if status == "all" else [status]
    queue_dir = Path(queue_dir)
    pairs: list[tuple[dict[str, Any], str]] = []
    for name in statuses:
        for item in _read_items(queue_dir / _STATUS_FILES[name]):
            pairs.append((item, name))
    return pairs


def get_any(
    review_id: str, queue_dir: str | Path
) -> tuple[dict[str, Any], str] | None:
    """Find a review item in any of the three files, returning (item, status).

    Returns None when the id is absent everywhere.
    """
    for item, status in list_items(queue_dir, "all"):
        if item.get("reviewId") == review_id:
            return item, status
    return None


def _resolve(
    review_id: str,
    queue_dir: str | Path,
    status: str,
    dest_file: str,
    note: str | None,
) -> dict[str, Any] | None:
    """Move a pending item into a terminal file, stamping status/timestamp/note.

    Unknown review_id — and an id that is no longer pending — is a no-op that
    returns None (the API maps that to its existing 404/409 handling). The
    whole read-check-write sequence holds the queue lock, so concurrent
    approve/reject calls against the same id produce exactly one winner; the
    loser re-reads pending, finds the item gone, and returns None.

    File ordering: terminal file first, then pending (module docstring). If a
    previous process died between those two replacements, the item exists in
    both files; that state is detected here, the terminal decision is honored,
    the stale pending line is repaired away, and None is returned.
    """
    queue_dir = Path(queue_dir)
    with _QUEUE_LOCK:
        pending = list_pending(queue_dir)
        match = next(
            (item for item in pending if item.get("reviewId") == review_id), None
        )
        if match is None:
            return None

        remaining = [
            item for item in pending if item.get("reviewId") != review_id
        ]

        # Crash-duplicate self-heal: a terminal decision already exists, so
        # this item is not actually pending. Repair pending and report
        # "already resolved" instead of writing a second terminal copy.
        for terminal_file in (APPROVED_FILE, REJECTED_FILE):
            terminal_items = _read_items(queue_dir / terminal_file)
            if any(item.get("reviewId") == review_id for item in terminal_items):
                _atomic_write_items(queue_dir / PENDING_FILE, remaining)
                return None

        match["reviewStatus"] = status
        match["reviewedAt"] = datetime.now(timezone.utc).isoformat()
        match["reviewerNote"] = note

        dest_path = queue_dir / dest_file
        dest_items = _read_items(dest_path)
        dest_items.append(match)
        _atomic_write_items(dest_path, dest_items)
        _atomic_write_items(queue_dir / PENDING_FILE, remaining)
        return match


def approve(
    review_id: str, queue_dir: str | Path, *, note: str | None = None
) -> dict[str, Any] | None:
    """Approve a pending item: status approved, moved to approved.jsonl."""
    return _resolve(review_id, queue_dir, "approved", APPROVED_FILE, note)


def reject(
    review_id: str, queue_dir: str | Path, *, note: str | None = None
) -> dict[str, Any] | None:
    """Reject a pending item: status rejected, moved to rejected.jsonl."""
    return _resolve(review_id, queue_dir, "rejected", REJECTED_FILE, note)
