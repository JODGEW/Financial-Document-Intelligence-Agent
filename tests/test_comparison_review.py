"""Tests for attributable comparison-review decisions (comparison_review.py).

Synthetic held reviews are produced through the real governance path over
schema-valid synthetic results; edit revalidation uses an injected resolver
over the changes' own evidence chunk ids (full text, as the detector would
resolve them). Entirely offline.
"""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api
import comparison_governance as cg
import comparison_review
import comparison_store
import config
import filing_registry
from comparison_review import ReviewDecisionError, decide
from governance.comparison_schema import load_comparison

client = TestClient(api.app)

PREV = "acme-corporation:10-k:2024-12-31"
CURR = "acme-corporation:10-k:2025-12-31"
SECTION = "item_1a_risk_factors"
PREV_CHUNK = "acme-2024.pdf:1:aaa111aaa111"
CURR_CHUNK = "acme-2025.pdf:1:bbb222bbb222"

_PREV_TEXT = (
    "Cybersecurity and Data Security Risks We carry cyber liability insurance "
    "with aggregate coverage of $35 million per incident, covering 41% of "
    "revenue."
)
_CURR_TEXT = (
    "Cybersecurity and Data Security Risks We carry cyber liability insurance "
    "with aggregate coverage of $50 million per incident, covering 38% of "
    "revenue."
)

RESOLVER = {
    PREV_CHUNK: {"filing_id": PREV, "text": _PREV_TEXT},
    CURR_CHUNK: {"filing_id": CURR, "text": _CURR_TEXT},
}.get

HEADING = "Cybersecurity and Data Security Risks"
ORIGINAL_SUMMARY = (
    f"Risk factor '{HEADING}' changed between the two filing periods."
)
SUPPORTED_EDIT = (
    f"Risk factor '{HEADING}' changed between the two filing periods; "
    "coverage increased from $35 to $50."
)


def _filing(document_id, period_end, source):
    return {
        "document_id": document_id,
        "company_key": "acme corporation",
        "company_name": "Acme Corporation",
        "form_type": "10-k",
        "filing_date": None,
        "period_end": period_end,
        "source_name": source,
        "version_hash": "abcdefabcdef",
    }


def _ev(side):
    doc, src, chunk, text = (
        (PREV, "acme-2024.pdf", PREV_CHUNK, _PREV_TEXT)
        if side == "prev"
        else (CURR, "acme-2025.pdf", CURR_CHUNK, _CURR_TEXT)
    )
    return {
        "document_id": doc,
        "chunk_id": chunk,
        "source_name": src,
        "page": 1,
        "section_key": SECTION,
        "section_title": "ITEM 1A. RISK FACTORS",
        "excerpt": " ".join(text.split())[:700].strip(),
        "content_hash": "cafecafecafe",
    }


def _chk(name, status, reason_code=None):
    return {
        "check": name,
        "status": status,
        "reason_code": reason_code
        or ("required_reason" if status == "failed" else None),
        "detail": f"{name} {status}.",
        "validator_version": None if status == "not_run" else "x.v1",
    }


def _held_wire():
    """One modified change with real evidence; held via an unexpected not_run."""
    checks = [
        _chk("evidence_presence", "passed"),
        _chk("entity_consistency", "passed"),
        _chk("period_consistency", "passed"),
        _chk("citation_support", "passed"),
        _chk("numeric_consistency", "not_applicable", "no_numeric_claim"),
        _chk("direction_consistency", "not_run"),
    ]
    change = {
        "change_id": "chg-cyber0000001",
        "change_type": "modified",
        "category": "risk_factor",
        "section_key": SECTION,
        "summary": ORIGINAL_SUMMARY,
        "previous_evidence": [_ev("prev")],
        "current_evidence": [_ev("curr")],
        "validation": checks,
        "undetermined_reason": None,
    }
    counts = {"passed": 4, "failed": 0, "not_run": 1, "not_applicable": 1}
    return {
        "schema_version": "comparison.v1",
        "comparison_id": "cmp-synthetic",
        "previous_filing": _filing(PREV, "2024-12-31", "acme-2024.pdf"),
        "current_filing": _filing(CURR, "2025-12-31", "acme-2025.pdf"),
        "section_scope": [SECTION],
        "changes": [change],
        "validation_summary": {"total_checks": 6, **counts},
        "risk": {"decision": "not_evaluated", "reason_codes": [],
                 "risk_score": None, "risk_level": None},
        "review": {"status": "not_required", "review_id": None},
        "created_at": "2026-07-01T12:00:00Z",
        "producer": "item1a_detector.v2",
    }


@pytest.fixture
def env(tmp_path):
    """Registry + db + a governed HELD review over the synthetic result."""
    reg = tmp_path / "registry.jsonl"
    for src, fid, pe, h in (
        ("acme-2024.pdf", PREV, "2024-12-31", "h24"),
        ("acme-2025.pdf", CURR, "2025-12-31", "h25"),
    ):
        filing_registry.record_outcome(
            reg, source_path=src, source_name=src, source_hash=h,
            parse_status=filing_registry.PARSED, filing_id=fid,
            company_key="acme corporation", company_name="Acme Corporation",
            form_type="10-k", period_end=pe,
            document_family_id="acme-corp-10k-excerpt", identity_source="manifest",
        )
    db = tmp_path / "comparisons.db"
    record, _ = comparison_store.create_comparison(
        PREV, CURR, db_path=db, registry_path=reg
    )
    wire = _held_wire()
    wire["comparison_id"] = record["comparison_id"]
    load_comparison(wire)
    result_hash = hashlib.sha256(
        json.dumps({k: v for k, v in wire.items() if k != "created_at"},
                   sort_keys=True).encode()
    ).hexdigest()
    comparison_store.record_result(
        record["comparison_id"], result_json=json.dumps(wire),
        result_hash=result_hash, detector_version="item1a_detector.v2",
        previous_source_hash="h24", current_source_hash="h25", db_path=db,
    )
    evaluation, _ = cg.govern(record["comparison_id"], db_path=db)
    assert evaluation["decision"] == "held_for_review"
    reviews = comparison_store.list_comparison_reviews(db_path=db)
    assert len(reviews) == 1
    return SimpleNamespace(
        db=db, reg=reg, root=tmp_path,
        comparison_id=record["comparison_id"],
        review_id=reviews[0]["review_id"],
        evaluation=evaluation,
    )


def _decide(env, action="approved", reason="approved_as_is",
            reviewer="reviewer@example.com", note="Looks correct.",
            edits=None, resolve=RESOLVER):
    return decide(
        env.review_id, action=action, reviewer_id=reviewer, reason_code=reason,
        reviewer_note=note, edits=edits, db_path=env.db, resolve=resolve,
    )


def _item(env):
    return comparison_store.get_review_item(env.review_id, db_path=env.db)


# --- Core decisions (tests 1-3) -----------------------------------------------


def test_approve_as_is(env):
    event, created = _decide(env)
    assert created is True
    assert event["action"] == "approved"
    assert event["reviewer_id"] == "reviewer@example.com"
    assert event["reason_code"] == "approved_as_is"
    assert event["edits"] == []
    item = _item(env)
    assert item["status"] == "approved"
    assert item["terminal_event_id"] == event["event_id"]
    assert item["decided_at"]
    model = load_comparison(event["reviewed_result"])  # test 23
    assert model.review.status == "approved"
    assert model.review.review_id == env.review_id
    assert model.risk.decision == "held_for_review"  # risk preserved


def test_approve_with_supported_summary_edit(env):
    """Tests 2/15: an evidence-supported edit passes revalidation."""
    event, created = _decide(
        env, reason="approved_with_summary_edits",
        edits=[{"change_id": "chg-cyber0000001", "summary": SUPPORTED_EDIT}],
    )
    assert created is True
    assert event["edits"] == [{
        "change_id": "chg-cyber0000001",
        "original_summary": ORIGINAL_SUMMARY,
        "new_summary": SUPPORTED_EDIT,
    }]
    reviewed = event["reviewed_result"]
    change = reviewed["changes"][0]
    assert change["summary"] == SUPPORTED_EDIT
    # Test 19: the three content checks were re-run against the evidence
    # (numeric now passed instead of not_applicable; direction now passed
    # instead of not_run) and the summary tallies were recomputed.
    checks = {c["check"]: c for c in change["validation"]}
    assert checks["citation_support"]["status"] == "passed"
    assert checks["numeric_consistency"]["status"] == "passed"
    assert checks["direction_consistency"]["status"] == "passed"
    assert reviewed["validation_summary"]["not_run"] == 0
    assert reviewed["validation_summary"]["passed"] == 6
    load_comparison(reviewed)


def test_reject(env):
    """Tests 3/24: rejection preserves the governed snapshot verbatim."""
    event, created = _decide(
        env, action="rejected", reason="rejected_ambiguous_change",
        note="Cannot verify the alignment.",
    )
    assert created is True
    assert _item(env)["status"] == "rejected"
    reviewed = event["reviewed_result"]
    model = load_comparison(reviewed)
    assert model.review.status == "rejected"
    governed = env.evaluation["governed_result"]
    assert reviewed["changes"] == governed["changes"]  # unchanged
    assert reviewed["risk"] == governed["risk"]  # risk decision unchanged


# --- Attribution and reason validation (tests 4-10) ---------------------------


def test_reviewer_id_required_validated_and_persisted(env):
    """Tests 4-5."""
    with pytest.raises(ReviewDecisionError, match="reviewerId") as excinfo:
        _decide(env, reviewer="   ")
    assert excinfo.value.code == "invalid_reviewer_id"
    with pytest.raises(ReviewDecisionError):
        _decide(env, reviewer="x" * 121)
    with pytest.raises(ReviewDecisionError, match="control"):
        _decide(env, reviewer="bad\x00name")

    event, _ = _decide(env, reviewer="  Casey.Reviewer@Example.com  ")
    assert event["reviewer_id"] == "Casey.Reviewer@Example.com"  # case kept


def test_reviewer_note_required_and_persisted(env):
    """Test 6."""
    with pytest.raises(ReviewDecisionError) as excinfo:
        _decide(env, note="")
    assert excinfo.value.code == "invalid_reviewer_note"
    with pytest.raises(ReviewDecisionError):
        _decide(env, note="n" * 501)
    event, _ = _decide(env, note="Checked both filings by hand.")
    assert event["reviewer_note"] == "Checked both filings by hand."


def test_reason_allowlists_enforced(env):
    """Tests 7-10."""
    with pytest.raises(ReviewDecisionError) as excinfo:
        _decide(env, reason="rejected_other")  # reject reason on approve
    assert excinfo.value.code == "invalid_reason_code"
    with pytest.raises(ReviewDecisionError):
        _decide(env, action="rejected", reason="approved_as_is")
    with pytest.raises(ReviewDecisionError):
        _decide(env, reason="looks_fine_to_me")  # arbitrary string

    edits = [{"change_id": "chg-cyber0000001", "summary": SUPPORTED_EDIT}]
    with pytest.raises(ReviewDecisionError) as excinfo:
        _decide(env, reason="approved_as_is", edits=edits)  # test 8
    assert excinfo.value.code == "edits_require_summary_edit_reason"
    with pytest.raises(ReviewDecisionError) as excinfo:
        _decide(env, reason="approved_with_summary_edits")  # test 9
    assert excinfo.value.code == "summary_edit_reason_requires_edits"
    with pytest.raises(ReviewDecisionError) as excinfo:
        _decide(env, action="rejected", reason="rejected_other", edits=edits)
    assert excinfo.value.code == "reject_does_not_accept_edits"  # test 10


# --- Edit surface validation (tests 11-14, 16-18) -----------------------------


def test_edit_addressing_rules(env):
    """Tests 11-13."""
    with pytest.raises(ReviewDecisionError) as excinfo:
        _decide(env, reason="approved_with_summary_edits",
                edits=[{"change_id": "chg-nope", "summary": "New summary."}])
    assert excinfo.value.code == "unknown_change_id"

    duplicate = [
        {"change_id": "chg-cyber0000001", "summary": SUPPORTED_EDIT},
        {"change_id": "chg-cyber0000001", "summary": SUPPORTED_EDIT + " More."},
    ]
    with pytest.raises(ReviewDecisionError) as excinfo:
        _decide(env, reason="approved_with_summary_edits", edits=duplicate)
    assert excinfo.value.code == "duplicate_edit_change_id"

    with pytest.raises(ReviewDecisionError) as excinfo:
        _decide(env, reason="approved_with_summary_edits",
                edits=[{"change_id": "chg-cyber0000001", "summary": "   "}])
    assert excinfo.value.code == "invalid_edit_summary"
    with pytest.raises(ReviewDecisionError) as excinfo:
        _decide(env, reason="approved_with_summary_edits",
                edits=[{"change_id": "chg-cyber0000001",
                        "summary": ORIGINAL_SUMMARY}])
    assert excinfo.value.code == "unchanged_edit_summary"
    with pytest.raises(ReviewDecisionError):
        _decide(env, reason="approved_with_summary_edits",
                edits=[{"change_id": "chg-cyber0000001", "summary": "x" * 1001}])


def test_only_summaries_are_editable(env):
    """Test 14: the request surface carries change_id+summary only; every
    other field of the reviewed snapshot is byte-equal to the governed one."""
    event, _ = _decide(
        env, reason="approved_with_summary_edits",
        edits=[{"change_id": "chg-cyber0000001", "summary": SUPPORTED_EDIT}],
    )
    governed = env.evaluation["governed_result"]
    reviewed = event["reviewed_result"]
    g_change, r_change = governed["changes"][0], reviewed["changes"][0]
    for field in ("change_id", "change_type", "category", "section_key",
                  "previous_evidence", "current_evidence", "undetermined_reason"):
        assert r_change[field] == g_change[field]
    for field in ("schema_version", "comparison_id", "previous_filing",
                  "current_filing", "section_scope", "risk", "created_at",
                  "producer"):
        assert reviewed[field] == governed[field]
    # And the API request model rejects unknown edit fields implicitly: the
    # decide() edit surface only reads change_id and summary.


@pytest.mark.parametrize(
    "bad_summary,expected_fragment",
    [
        ("Risk factor 'Liquidity Risks' changed between the two filing "
         "periods.", "citation_heading_not_supported"),  # test 16
        (f"Risk factor '{HEADING}' now cites $99.",
         "numeric"),  # test 17: unsupported/ambiguous numeric
        (f"Risk factor '{HEADING}' coverage increased from $50 to $35.",
         "direction_inverted"),  # test 18
    ],
)
def test_unsupported_edits_rejected(env, bad_summary, expected_fragment):
    with pytest.raises(ReviewDecisionError) as excinfo:
        _decide(env, reason="approved_with_summary_edits",
                edits=[{"change_id": "chg-cyber0000001", "summary": bad_summary}])
    assert excinfo.value.code == "edit_validation_failed"
    assert expected_fragment in excinfo.value.message
    # Nothing was persisted: the review is still pending, no events exist.
    assert _item(env)["status"] == "pending"
    assert comparison_store.list_review_events(env.review_id, db_path=env.db) == []


# --- Snapshot preservation (tests 20-22, 25) ----------------------------------


def test_originals_preserved_and_hash_deterministic(env):
    """Tests 21-22, 25 (+20 via the single-change byte checks above)."""
    before_result = comparison_store.get_result(env.comparison_id, db_path=env.db)
    before_eval = comparison_store.get_evaluation(
        env.evaluation["evaluation_id"], db_path=env.db
    )
    event, _ = _decide(
        env, reason="approved_with_summary_edits",
        edits=[{"change_id": "chg-cyber0000001", "summary": SUPPORTED_EDIT}],
    )
    after_result = comparison_store.get_result(env.comparison_id, db_path=env.db)
    after_eval = comparison_store.get_evaluation(
        env.evaluation["evaluation_id"], db_path=env.db
    )
    assert after_result == before_result  # detector result untouched
    assert after_eval == before_eval  # evaluation + governed snapshot untouched
    assert event["original_governed_result_hash"] == before_eval["governed_result_hash"]
    # Deterministic final hash: recompute from the stored snapshot.
    assert event["final_reviewed_result_hash"] == hashlib.sha256(
        json.dumps(event["reviewed_result"], sort_keys=True).encode()
    ).hexdigest()


# --- Idempotency and concurrency (tests 26-30) --------------------------------


def test_same_decision_replay_is_idempotent(env):
    first, created_first = _decide(env)
    second, created_second = _decide(env)
    assert (created_first, created_second) == (True, False)
    assert second == first
    assert len(comparison_store.list_review_events(env.review_id, db_path=env.db)) == 1


def test_different_request_after_decision_conflicts(env):
    _decide(env)
    with pytest.raises(comparison_store.ReviewAlreadyDecided):
        _decide(env, action="rejected", reason="rejected_other",
                note="Changed my mind.")


def test_concurrent_identical_decisions_one_event(env):
    """Test 27."""
    def attempt(_):
        return _decide(env)

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))
    assert sum(1 for _e, created in outcomes if created) == 1
    assert len({e["event_id"] for e, _ in outcomes}) == 1
    with closing(sqlite3.connect(env.db)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM comparison_review_events"
        ).fetchone()[0] == 1


def test_concurrent_conflicting_decisions_one_winner(env):
    """Tests 28-29: approve vs reject, and approve vs different-edit approve."""
    requests = [
        dict(action="approved", reason="approved_as_is", note="ok"),
        dict(action="rejected", reason="rejected_other", note="no"),
        dict(action="approved", reason="approved_with_summary_edits",
             note="edited",
             edits=[{"change_id": "chg-cyber0000001", "summary": SUPPORTED_EDIT}]),
    ]

    def attempt(request):
        try:
            return ("ok", _decide(env, **request))
        except comparison_store.ReviewAlreadyDecided:
            return ("conflict", None)

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(attempt, requests))
    winners = [o for o in outcomes if o[0] == "ok"]
    conflicts = [o for o in outcomes if o[0] == "conflict"]
    assert len(winners) == 1
    assert len(conflicts) == 2
    with closing(sqlite3.connect(env.db)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM comparison_review_events"
        ).fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_transaction_failure_leaves_pending_and_no_event(env, monkeypatch):
    """Test 30: a fault between the event insert and the item transition
    rolls everything back (sqlite3.Connection is an immutable C type, so the
    fault is injected through the store's _connect factory)."""
    real_connect = comparison_store._connect

    class _FaultyConn:
        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def execute(self, sql, *args, **kwargs):
            if "UPDATE comparison_review_items" in sql:
                raise sqlite3.OperationalError("simulated fault")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

        def __enter__(self):
            return self._real.__enter__()

        def __exit__(self, *args):
            return self._real.__exit__(*args)

    monkeypatch.setattr(
        comparison_store, "_connect", lambda path: _FaultyConn(real_connect(path))
    )
    with pytest.raises(sqlite3.OperationalError):
        _decide(env)
    monkeypatch.undo()

    assert _item(env)["status"] == "pending"
    assert comparison_store.list_review_events(env.review_id, db_path=env.db) == []
    with closing(sqlite3.connect(env.db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# --- Durability and migration (tests 31-32) -----------------------------------


def test_reopen_preserves_decision_and_snapshot(env):
    """Test 31: fresh reads return the decision and final snapshot."""
    event, _ = _decide(
        env, reason="approved_with_summary_edits",
        edits=[{"change_id": "chg-cyber0000001", "summary": SUPPORTED_EDIT}],
    )
    events = comparison_store.list_review_events(env.review_id, db_path=env.db)
    assert events == [event]
    assert load_comparison(events[0]["reviewed_result"]).review.status == "approved"
    assert _item(env)["status"] == "approved"


def test_migration_preserves_pending_only_rows(tmp_path):
    """Test 32: a pre-decision database (CHECK pending-only, no terminal
    columns) migrates idempotently with its pending row intact."""
    db = tmp_path / "old.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            """
            CREATE TABLE comparison_review_items (
                review_id              TEXT PRIMARY KEY NOT NULL,
                comparison_id          TEXT NOT NULL,
                evaluation_id          TEXT NOT NULL UNIQUE,
                comparison_result_hash TEXT NOT NULL,
                governed_result_hash   TEXT NOT NULL,
                status                 TEXT NOT NULL CHECK (status IN ('pending')),
                created_at             TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO comparison_review_items VALUES "
            "('crev_old', 'cmp_old', 'gov_old', 'h1', 'h2', 'pending', 't0')"
        )
    for _ in range(2):
        comparison_store.init_db(db)
    item = comparison_store.get_review_item("crev_old", db_path=db)
    assert item["status"] == "pending"
    assert item["terminal_event_id"] is None
    assert item["decided_at"] is None
    with closing(sqlite3.connect(db)) as conn:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='comparison_review_items'"
        ).fetchone()[0]
        assert "'approved'" in ddl
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# --- API surface (tests 33-36) ------------------------------------------------


@pytest.fixture
def api_env(env, monkeypatch):
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(env.db))
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(env.reg))
    monkeypatch.setattr(
        comparison_review, "_default_resolver", lambda ids: RESOLVER
    )
    return env


def _post_decision(review_id, body):
    return client.post(f"/api/comparison-reviews/{review_id}/decision", json=body)


def test_api_decision_lifecycle(api_env):
    assert client.get("/api/comparison-reviews/crev_nope").status_code == 404
    assert client.get("/api/comparison-reviews/crev_nope/events").status_code == 404
    assert _post_decision("crev_nope", {
        "action": "approved", "reviewerId": "r", "reasonCode": "approved_as_is",
        "reviewerNote": "n",
    }).status_code == 404

    detail = client.get(f"/api/comparison-reviews/{api_env.review_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "pending"
    assert detail.json()["decision"] is None
    assert client.get(
        f"/api/comparison-reviews/{api_env.review_id}/events"
    ).json() == []

    body = {
        "action": "approved",
        "reviewerId": "reviewer@example.com",
        "reasonCode": "approved_with_summary_edits",
        "reviewerNote": "Verified against both filings.",
        "edits": [{"changeId": "chg-cyber0000001", "summary": SUPPORTED_EDIT}],
    }
    first = _post_decision(api_env.review_id, body)
    assert first.status_code == 201
    assert first.json()["created"] is True
    decision = first.json()["decision"]
    assert decision["action"] == "approved"
    assert decision["editedChangeIds"] == ["chg-cyber0000001"]
    load_comparison(decision["reviewedResult"])

    replay = _post_decision(api_env.review_id, body)
    assert replay.status_code == 200
    assert replay.json()["created"] is False

    conflicting = _post_decision(api_env.review_id, {
        "action": "rejected", "reviewerId": "other",
        "reasonCode": "rejected_other", "reviewerNote": "no",
    })
    assert conflicting.status_code == 409
    assert conflicting.json()["detail"]["code"] == "review_already_decided"

    invalid = _post_decision(api_env.review_id, {
        "action": "approved", "reviewerId": "r",
        "reasonCode": "not_a_reason", "reviewerNote": "n",
    })
    assert invalid.status_code in (409, 422)  # decided-first wins as 409 here

    # Detail and events now carry the decision (tests 33/35).
    decided = client.get(f"/api/comparison-reviews/{api_env.review_id}").json()
    assert decided["status"] == "approved"
    assert decided["decidedAt"]
    assert decided["decision"]["reviewerId"] == "reviewer@example.com"
    assert decided["decision"]["reasonCode"] == "approved_with_summary_edits"
    assert decided["decision"]["reviewerNote"] == "Verified against both filings."
    events = client.get(
        f"/api/comparison-reviews/{api_env.review_id}/events"
    ).json()
    assert len(events) == 1
    assert set(events[0]) == {
        "eventId", "reviewId", "comparisonId", "evaluationId", "action",
        "reviewerId", "reasonCode", "reviewerNote",
        "originalGovernedResultHash", "finalReviewedResultHash",
        "editedChangeIds", "createdAt",
    }


def test_api_422_codes_for_invalid_input(api_env):
    bad_reason = _post_decision(api_env.review_id, {
        "action": "approved", "reviewerId": "r",
        "reasonCode": "rejected_other", "reviewerNote": "n",
    })
    assert bad_reason.status_code == 422
    assert bad_reason.json()["detail"]["code"] == "invalid_reason_code"

    bad_edit = _post_decision(api_env.review_id, {
        "action": "approved", "reviewerId": "r",
        "reasonCode": "approved_with_summary_edits", "reviewerNote": "n",
        "edits": [{"changeId": "chg-cyber0000001",
                   "summary": "Risk factor 'Liquidity Risks' changed."}],
    })
    assert bad_edit.status_code == 422
    assert bad_edit.json()["detail"]["code"] == "edit_validation_failed"
    # Still pending after failed attempts.
    assert client.get(
        f"/api/comparison-reviews/{api_env.review_id}"
    ).json()["status"] == "pending"


def test_api_dtos_expose_no_internals(api_env):
    """Tests 33-34: no storage internals; list stays excerpt-free."""
    detail = client.get(f"/api/comparison-reviews/{api_env.review_id}")
    assert str(api_env.root) not in detail.text
    assert ".db" not in detail.text and "sqlite" not in detail.text.lower()
    assert "/Users/" not in detail.text

    listing = client.get("/api/comparison-reviews")
    assert "excerpt" not in listing.text
    for item in listing.json():
        assert set(item) == {
            "reviewId", "comparisonId", "evaluationId", "status",
            "riskScore", "riskLevel", "reasonCodes", "createdAt",
        }


def test_api_storage_failure_sanitized(api_env, monkeypatch, caplog):
    """Test 36: no paths, SQL, or reviewer note in the safe error."""
    import logging

    secret = "disk I/O error at /secret/reviews SELECT reviewer_note FROM x"

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError(secret)

    monkeypatch.setattr(api.comparison_review, "decide", boom)
    with caplog.at_level(logging.ERROR, logger="api"):
        response = _post_decision(api_env.review_id, {
            "action": "approved", "reviewerId": "r",
            "reasonCode": "approved_as_is",
            "reviewerNote": "SUPER-PRIVATE-NOTE",
        })
    assert response.status_code == 500
    assert "/secret" not in response.text
    assert "SELECT" not in response.text
    detail = response.json()["detail"]
    assert detail["code"] == "comparison_storage_error"
    assert detail["error_id"].startswith("err_")
    assert secret in caplog.text
    assert "SUPER-PRIVATE-NOTE" not in caplog.text  # notes never logged
