"""File-backed human review queue (Governance_layer.md §7.8, Phase 5).

Persists answers flagged for human review as JSONL under a queue directory.
Three files: pending.jsonl, approved.jsonl, rejected.jsonl. The agent enqueues a
held item; the review_queue CLI lists, shows, approves, and rejects.

Approve/reject moves an item from pending into the matching terminal file and
stamps it with a status, timestamp, and optional note. It does not mutate the
audit log and does not re-deliver the held answer; both are out of scope for this
phase. Concurrent writers are not a concern at this scale.

All functions take the queue directory as a parameter so callers (agent, CLI,
API, tests) control where the files live. This module is the only reader and
writer of queue files; the API never opens them directly.
"""

from __future__ import annotations

import json
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


def _append_item(path: Path, item: dict[str, Any]) -> None:
    """Append one item as a JSONL line, creating the directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, default=str) + "\n")


def _rewrite_items(path: Path, items: list[dict[str, Any]]) -> None:
    """Replace a queue file with the given items (used to drop a resolved item)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, default=str) + "\n")


def enqueue(item: dict[str, Any], queue_dir: str | Path) -> dict[str, Any]:
    """Append a held review item to pending.jsonl."""
    _append_item(Path(queue_dir) / PENDING_FILE, item)
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

    Unknown review_id is a no-op that returns None (no crash).
    """
    queue_dir = Path(queue_dir)
    pending = list_pending(queue_dir)
    match = next((item for item in pending if item.get("reviewId") == review_id), None)
    if match is None:
        return None

    match["reviewStatus"] = status
    match["reviewedAt"] = datetime.now(timezone.utc).isoformat()
    match["reviewerNote"] = note

    remaining = [item for item in pending if item.get("reviewId") != review_id]
    _append_item(queue_dir / dest_file, match)
    _rewrite_items(queue_dir / PENDING_FILE, remaining)
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
