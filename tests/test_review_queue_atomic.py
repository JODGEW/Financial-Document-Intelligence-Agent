"""Atomicity, failure-injection, and concurrency tests for the review queue.

Invariants under test: a resolution moves exactly one item to exactly one
terminal file; unrelated items survive untouched; concurrent resolutions of
the same id produce one winner and one deterministic conflict; any injected
write failure leaves the original files byte-for-byte intact with no temp
files left behind; and the crash-duplicate state (item in pending AND a
terminal file) heals on the next resolution attempt.
"""

import json
import threading
from pathlib import Path

import pytest

from governance import review_queue
from governance.review_queue import (
    APPROVED_FILE,
    PENDING_FILE,
    REJECTED_FILE,
    _atomic_write_items,
    approve,
    enqueue,
    get_any,
    list_pending,
    reject,
)


def _item(review_id: str) -> dict:
    return {
        "reviewId": review_id,
        "auditId": review_id.removeprefix("review_"),
        "question": f"question for {review_id}",
        "draftAnswer": f"draft for {review_id}",
        "riskScore": 0.5,
        "riskLevel": "medium",
        "riskReasons": ["grounding_below_review_floor"],
        "retrievedSources": [],
        "decision": "held_for_review",
        "reviewStatus": "pending",
        "createdAt": "2026-07-28T00:00:00+00:00",
        "wasWithheld": True,
    }


def _seed(tmp_path, ids):
    for review_id in ids:
        enqueue(_item(review_id), tmp_path)


def _raw_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def _no_temp_files(tmp_path: Path) -> bool:
    return not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".*.tmp"))


# --- successful moves --------------------------------------------------------


def test_approve_moves_exactly_one_item_and_unrelated_items_survive(tmp_path):
    _seed(tmp_path, ["review_a", "review_b", "review_c"])
    before_b = json.dumps(list_pending(tmp_path)[1], sort_keys=True)

    resolved = approve("review_a", tmp_path, note="ok")

    assert resolved["reviewStatus"] == "approved"
    pending_ids = [item["reviewId"] for item in list_pending(tmp_path)]
    assert pending_ids == ["review_b", "review_c"]
    # Unrelated item is byte-identical after the rewrite.
    assert json.dumps(list_pending(tmp_path)[0], sort_keys=True) == before_b
    approved = review_queue._read_items(tmp_path / APPROVED_FILE)
    assert [item["reviewId"] for item in approved] == ["review_a"]
    assert review_queue._read_items(tmp_path / REJECTED_FILE) == []
    assert _no_temp_files(tmp_path)


def test_reject_moves_exactly_one_item(tmp_path):
    _seed(tmp_path, ["review_a", "review_b"])
    resolved = reject("review_b", tmp_path, note="nope")
    assert resolved["reviewStatus"] == "rejected"
    assert [i["reviewId"] for i in list_pending(tmp_path)] == ["review_a"]
    rejected = review_queue._read_items(tmp_path / REJECTED_FILE)
    assert [i["reviewId"] for i in rejected] == ["review_b"]
    assert _no_temp_files(tmp_path)


def test_every_line_is_a_json_object_after_operations(tmp_path):
    _seed(tmp_path, ["review_a", "review_b", "review_c"])
    approve("review_a", tmp_path)
    reject("review_b", tmp_path)
    for name in (PENDING_FILE, APPROVED_FILE, REJECTED_FILE):
        for line in _raw_lines(tmp_path / name):
            parsed = json.loads(line)
            assert isinstance(parsed, dict)


def test_legacy_item_without_newer_fields_is_readable_and_resolvable(tmp_path):
    enqueue({"reviewId": "review_legacy", "question": "old"}, tmp_path)
    found = get_any("review_legacy", tmp_path)
    assert found is not None and found[1] == "pending"
    resolved = approve("review_legacy", tmp_path)
    assert resolved["reviewStatus"] == "approved"
    assert get_any("review_legacy", tmp_path)[1] == "approved"


# --- concurrency -------------------------------------------------------------


def _race(tmp_path, first, second):
    """Run two resolutions of review_x from a barrier; return both results."""
    barrier = threading.Barrier(2)
    results = {}

    def run(name, fn):
        barrier.wait()
        try:
            results[name] = fn("review_x", tmp_path, note=name)
        except Exception as exc:  # pragma: no cover - would fail the assertions
            results[name] = exc

    threads = [
        threading.Thread(target=run, args=("first", first)),
        threading.Thread(target=run, args=("second", second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


@pytest.mark.parametrize("second_action", [reject, approve])
def test_concurrent_resolutions_produce_one_winner_and_one_conflict(
    tmp_path, second_action
):
    """approve-vs-reject and approve-vs-approve both end with one winner."""
    _seed(tmp_path, ["review_x", "review_other"])

    results = _race(tmp_path, approve, second_action)

    outcomes = [results["first"], results["second"]]
    winners = [r for r in outcomes if isinstance(r, dict)]
    conflicts = [r for r in outcomes if r is None]
    assert len(winners) == 1 and len(conflicts) == 1

    # The id sits in exactly one terminal file and is gone from pending.
    approved = [
        i["reviewId"] for i in review_queue._read_items(tmp_path / APPROVED_FILE)
    ]
    rejected = [
        i["reviewId"] for i in review_queue._read_items(tmp_path / REJECTED_FILE)
    ]
    assert (approved + rejected).count("review_x") == 1
    assert "review_x" not in [i["reviewId"] for i in list_pending(tmp_path)]
    # The unrelated pending item survived the race.
    assert [i["reviewId"] for i in list_pending(tmp_path)] == ["review_other"]
    assert _no_temp_files(tmp_path)


# --- failure injection -------------------------------------------------------


def test_fsync_failure_leaves_files_byte_for_byte_unchanged(tmp_path, monkeypatch):
    _seed(tmp_path, ["review_a", "review_b"])
    pending_before = (tmp_path / PENDING_FILE).read_bytes()

    def failing_fsync(fd):
        raise OSError("disk said no")

    monkeypatch.setattr(review_queue.os, "fsync", failing_fsync)
    with pytest.raises(OSError):
        approve("review_a", tmp_path)

    assert (tmp_path / PENDING_FILE).read_bytes() == pending_before
    assert not (tmp_path / APPROVED_FILE).exists()
    assert _no_temp_files(tmp_path)

    # The item is still pending and resolvable once the disk recovers.
    monkeypatch.undo()
    assert approve("review_a", tmp_path)["reviewStatus"] == "approved"


def test_replace_failure_leaves_original_valid_and_cleans_temp(
    tmp_path, monkeypatch
):
    _seed(tmp_path, ["review_a"])
    pending_before = (tmp_path / PENDING_FILE).read_bytes()

    def failing_replace(src, dst):
        raise OSError("replace refused")

    monkeypatch.setattr(review_queue.os, "replace", failing_replace)
    with pytest.raises(OSError):
        reject("review_a", tmp_path)

    assert (tmp_path / PENDING_FILE).read_bytes() == pending_before
    for line in _raw_lines(tmp_path / PENDING_FILE):
        assert isinstance(json.loads(line), dict)
    assert _no_temp_files(tmp_path)


def test_serialization_failure_touches_nothing(tmp_path):
    """A non-serializable item fails before any temp file or replacement."""
    target = tmp_path / PENDING_FILE
    _atomic_write_items(target, [_item("review_a")])
    before = target.read_bytes()

    circular: dict = {}
    circular["self"] = circular
    with pytest.raises(ValueError):
        _atomic_write_items(target, [circular])

    assert target.read_bytes() == before
    assert _no_temp_files(tmp_path)


def test_partial_move_failure_then_heal(tmp_path, monkeypatch):
    """Terminal write lands, pending rewrite fails (the documented crash
    window): the caller sees an error, and the next attempt heals — the
    terminal decision is honored, pending is repaired, no duplicate copy."""
    _seed(tmp_path, ["review_a", "review_b"])

    real_replace = review_queue.os.replace
    calls = {"n": 0}

    def replace_second_fails(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("killed between the two replacements")
        return real_replace(src, dst)

    monkeypatch.setattr(review_queue.os, "replace", replace_second_fails)
    with pytest.raises(OSError):
        approve("review_a", tmp_path, note="first try")
    monkeypatch.undo()

    # The crash-duplicate state: approved has the decision, pending still has it.
    assert "review_a" in [
        i["reviewId"] for i in review_queue._read_items(tmp_path / APPROVED_FILE)
    ]
    assert "review_a" in [i["reviewId"] for i in list_pending(tmp_path)]

    # Next resolution attempt honors the terminal decision and repairs pending.
    assert approve("review_a", tmp_path, note="retry") is None
    assert "review_a" not in [i["reviewId"] for i in list_pending(tmp_path)]
    approved = [
        i["reviewId"] for i in review_queue._read_items(tmp_path / APPROVED_FILE)
    ]
    assert approved.count("review_a") == 1
    # The unrelated item survived both the failure and the heal.
    assert [i["reviewId"] for i in list_pending(tmp_path)] == ["review_b"]
    assert _no_temp_files(tmp_path)


def test_reject_after_crash_duplicate_does_not_double_resolve(tmp_path):
    """A pending+approved duplicate cannot be rejected into a second terminal."""
    _seed(tmp_path, ["review_a"])
    # Simulate the crash window directly: copy the item into approved while
    # leaving it pending.
    stamped = dict(_item("review_a"), reviewStatus="approved")
    _atomic_write_items(tmp_path / APPROVED_FILE, [stamped])

    assert reject("review_a", tmp_path) is None
    assert list_pending(tmp_path) == []
    assert review_queue._read_items(tmp_path / REJECTED_FILE) == []
    assert [
        i["reviewId"] for i in review_queue._read_items(tmp_path / APPROVED_FILE)
    ] == ["review_a"]
