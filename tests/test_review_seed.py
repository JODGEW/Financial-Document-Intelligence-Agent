"""Tests for review-queue data hygiene and the demo seed command.

The seed command must generate demo items through the real validation ->
hold -> resolve pipeline under the currently loaded policy, with no absolute
developer paths, no guardrail substitution artifacts, and explicit rerun
semantics (refuse without --reset, replace with it, deletion confined to the
three queue JSONL files).
"""

import json
from pathlib import Path

import agent
import scripts.review_queue as cli
from governance import review_queue

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _seed(monkeypatch, tmp_path, argv_extra=()):
    """Run the seed command with audit writes captured instead of hitting disk."""
    written = []

    def capture_audit(record, *args, **kwargs):
        written.append(record)
        return record["audit_id"]

    monkeypatch.setattr(agent, "write_audit_record", capture_audit)
    code = cli.main(["seed", "--queue-dir", str(tmp_path), *argv_extra])
    return code, written


def _all_items(tmp_path):
    return review_queue.list_items(tmp_path, "all")


def test_gitignore_covers_runtime_queue_files():
    """Runtime queue JSONL files are ignored; the directory is kept."""
    gitignore = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "review_queue/*.jsonl" in gitignore
    assert (_REPO_ROOT / "review_queue" / ".gitkeep").exists()


def test_fresh_queue_dir_lists_zero_items(tmp_path):
    assert review_queue.list_items(tmp_path, "all") == []
    assert review_queue.list_pending(tmp_path) == []


def test_seed_creates_one_item_per_status_with_clean_data(monkeypatch, tmp_path):
    code, audit_records = _seed(monkeypatch, tmp_path)
    assert code == 0

    by_status = {status: item for item, status in _all_items(tmp_path)}
    assert set(by_status) == {"pending", "approved", "rejected"}

    serialized = json.dumps([item for item, _ in _all_items(tmp_path)])
    # No developer-specific absolute paths, no guardrail artifacts.
    assert str(_REPO_ROOT) not in serialized
    assert "/Users/" not in serialized
    assert "{ADDRESS}" not in serialized

    # Held records carry the CURRENT policy's reason codes, not a snapshot.
    for item in by_status.values():
        assert "grounding_below_review_floor" in item["riskReasons"]
        assert item["decision"] == "held_for_review"
        # Labels come from the shipped weighted policy: sub-0.75 scores are
        # never labeled "high" (the stale-snapshot failure of the old data).
        assert item["riskScore"] < 0.75
        assert item["riskLevel"] != "high"

    # Terminal items went through the real resolve functions.
    assert by_status["approved"]["reviewStatus"] == "approved"
    assert by_status["approved"]["reviewedAt"]
    assert by_status["approved"]["reviewerNote"]
    assert by_status["rejected"]["reviewStatus"] == "rejected"
    assert by_status["rejected"]["reviewedAt"]

    # Matching audit records exist for the governance-report join.
    audit_ids = {record["audit_id"] for record in audit_records}
    for item in by_status.values():
        assert item["auditId"] in audit_ids
        assert item["reviewId"] == f"review_{item['auditId']}"


def test_seed_refuses_rerun_without_reset(monkeypatch, tmp_path):
    code, _ = _seed(monkeypatch, tmp_path)
    assert code == 0
    ids_before = sorted(item["reviewId"] for item, _ in _all_items(tmp_path))

    code, _ = _seed(monkeypatch, tmp_path)
    assert code == 1  # additive rerun refused
    assert sorted(item["reviewId"] for item, _ in _all_items(tmp_path)) == ids_before


def test_seed_reset_replaces_items_and_confines_deletion(monkeypatch, tmp_path):
    code, _ = _seed(monkeypatch, tmp_path)
    assert code == 0
    ids_before = sorted(item["reviewId"] for item, _ in _all_items(tmp_path))
    unrelated = tmp_path / "operator-notes.txt"
    unrelated.write_text("keep me", encoding="utf-8")

    code, _ = _seed(monkeypatch, tmp_path, ["--reset"])
    assert code == 0
    ids_after = sorted(item["reviewId"] for item, _ in _all_items(tmp_path))
    assert ids_after != ids_before
    by_status = {status for _, status in _all_items(tmp_path)}
    assert by_status == {"pending", "approved", "rejected"}
    # --reset deleted only the three queue JSONL files.
    assert unrelated.read_text(encoding="utf-8") == "keep me"


def test_seed_does_not_mutate_risk_policy_or_queue_config(monkeypatch, tmp_path):
    from governance import risk_scorer
    import config

    thresholds_before = dict(risk_scorer.THRESHOLDS)
    queue_dir_before = config.REVIEW_QUEUE_DIR

    code, _ = _seed(monkeypatch, tmp_path)
    assert code == 0
    assert dict(risk_scorer.THRESHOLDS) == thresholds_before
    assert config.REVIEW_QUEUE_DIR == queue_dir_before
