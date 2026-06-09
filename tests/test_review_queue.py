"""Tests for the human review queue store and its CLI."""

import json

import scripts.review_queue as cli
from governance import review_queue


def _item(review_id="review_audit-1", **overrides):
    """A minimal held review item with the required §7.8 fields."""
    item = {
        "reviewId": review_id,
        "auditId": review_id.removeprefix("review_"),
        "question": "What does the policy say?",
        "draftAnswer": "Preclearance is required [policy.md].",
        "riskScore": 0.86,
        "riskLevel": "high",
        "riskReasons": ["grounding_score_below_target", "external_context_used"],
        "retrievedSources": [{"source_name": "policy.md", "page": 2, "excerpt": "x"}],
        "decision": "held_for_review",
        "reviewStatus": "pending",
        "createdAt": "2026-06-08T00:00:00+00:00",
    }
    item.update(overrides)
    return item


def test_enqueue_writes_pending_item_with_required_fields(tmp_path):
    """enqueue appends a pending item carrying every required field."""
    review_queue.enqueue(_item(), tmp_path)

    pending = review_queue.list_pending(tmp_path)
    assert len(pending) == 1
    assert set(pending[0]) >= {
        "reviewId", "auditId", "question", "draftAnswer", "riskScore",
        "riskLevel", "riskReasons", "retrievedSources", "decision",
        "reviewStatus", "createdAt",
    }
    assert pending[0]["reviewStatus"] == "pending"
    assert pending[0]["decision"] == "held_for_review"


def test_list_pending_returns_items_and_empty_when_absent(tmp_path):
    """list_pending returns [] before any file exists, then the enqueued items."""
    assert review_queue.list_pending(tmp_path) == []

    review_queue.enqueue(_item("review_a"), tmp_path)
    review_queue.enqueue(_item("review_b"), tmp_path)
    ids = [item["reviewId"] for item in review_queue.list_pending(tmp_path)]
    assert ids == ["review_a", "review_b"]


def test_get_returns_item_or_none(tmp_path):
    """get finds a pending item by id; unknown id returns None."""
    review_queue.enqueue(_item("review_a"), tmp_path)
    assert review_queue.get("review_a", tmp_path)["reviewId"] == "review_a"
    assert review_queue.get("review_missing", tmp_path) is None


def test_approve_moves_pending_to_approved(tmp_path):
    """approve sets status, stamps timestamp/note, and removes it from pending."""
    review_queue.enqueue(_item("review_a"), tmp_path)
    review_queue.enqueue(_item("review_b"), tmp_path)

    result = review_queue.approve("review_a", tmp_path, note="Checked sources.")
    assert result["reviewStatus"] == "approved"
    assert result["reviewerNote"] == "Checked sources."
    assert result["reviewedAt"]

    pending_ids = [i["reviewId"] for i in review_queue.list_pending(tmp_path)]
    assert pending_ids == ["review_b"]
    approved = review_queue._read_items(tmp_path / review_queue.APPROVED_FILE)
    assert [i["reviewId"] for i in approved] == ["review_a"]


def test_reject_moves_pending_to_rejected(tmp_path):
    """reject sets status rejected and removes the item from pending."""
    review_queue.enqueue(_item("review_a"), tmp_path)

    result = review_queue.reject("review_a", tmp_path)
    assert result["reviewStatus"] == "rejected"
    assert result["reviewerNote"] is None

    assert review_queue.list_pending(tmp_path) == []
    rejected = review_queue._read_items(tmp_path / review_queue.REJECTED_FILE)
    assert [i["reviewId"] for i in rejected] == ["review_a"]


def test_approve_and_reject_unknown_id_is_noop_returning_none(tmp_path):
    """Resolving an unknown id returns None and never crashes or writes."""
    assert review_queue.approve("review_missing", tmp_path) is None
    assert review_queue.reject("review_missing", tmp_path) is None
    # No terminal files were created by the no-op.
    assert not (tmp_path / review_queue.APPROVED_FILE).exists()
    assert not (tmp_path / review_queue.REJECTED_FILE).exists()


def test_cli_list_show_text_and_json(tmp_path, capsys):
    """CLI list and show exit 0 in both text and json modes."""
    review_queue.enqueue(_item("review_a"), tmp_path)

    assert cli.main(["list", "--queue-dir", str(tmp_path)]) == 0
    assert "review_a" in capsys.readouterr().out

    assert cli.main(["list", "--queue-dir", str(tmp_path), "--output", "json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["reviewId"] == "review_a"
    assert set(listed[0]) == {"reviewId", "question", "riskScore", "riskLevel", "riskReasons"}

    assert cli.main(["show", "--review-id", "review_a", "--queue-dir", str(tmp_path)]) == 0
    assert "Draft answer:" in capsys.readouterr().out

    code = cli.main(
        ["show", "--review-id", "review_a", "--queue-dir", str(tmp_path), "--output", "json"]
    )
    assert code == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["draftAnswer"].startswith("Preclearance")


def test_cli_approve_and_reject_exit_zero(tmp_path, capsys):
    """CLI approve and reject move items between files and exit 0."""
    review_queue.enqueue(_item("review_a"), tmp_path)
    review_queue.enqueue(_item("review_b"), tmp_path)

    assert cli.main(
        ["approve", "--review-id", "review_a", "--queue-dir", str(tmp_path), "--note", "ok"]
    ) == 0
    assert "Approved review_a" in capsys.readouterr().out

    code = cli.main(
        ["reject", "--review-id", "review_b", "--queue-dir", str(tmp_path), "--output", "json"]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["reviewStatus"] == "rejected"

    assert review_queue.list_pending(tmp_path) == []


def test_cli_show_unknown_id_exits_one(tmp_path, capsys):
    """show on an unknown id reports not_found and exits 1."""
    code = cli.main(["show", "--review-id", "review_missing", "--queue-dir", str(tmp_path)])
    assert code == 1
    assert "not found" in capsys.readouterr().out
