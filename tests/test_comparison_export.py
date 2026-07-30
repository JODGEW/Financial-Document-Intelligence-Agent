"""Tests for release-gated comparison export (comparison_export.py).

Synthetic comparison.v1 results drive every release path through the REAL
store + governance + review workflows (returned, returned-with-warning via a
mandatory-rules-off policy, held/approved/rejected via decisions, blocked via
a directly recorded synthetic evaluation). Entirely offline: no Bedrock, no
embeddings, no network.
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
import comparison_export
import comparison_governance as cg
import comparison_review
import comparison_store
import config
import filing_registry
from governance.comparison_export_schema import dump_export, load_export
from governance.comparison_schema import dump_comparison, load_comparison
from tests.auth_helpers import authorization_headers

client = TestClient(api.app, headers=authorization_headers())

SECTION = "item_1a_risk_factors"
YEARS = list(range(2018, 2028))

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
HEADING = "Cybersecurity and Data Security Risks"
ORIGINAL_SUMMARY = (
    f"Risk factor '{HEADING}' changed between the two filing periods."
)
SUPPORTED_EDIT = (
    f"Risk factor '{HEADING}' changed between the two filing periods; "
    "coverage increased from $35 to $50."
)

# Same weights/thresholds as the default policy, mandatory rules off, distinct
# version: makes returned_with_warning reachable (the documented operator
# relaxation) without touching the checked-in policy.
WARN_POLICY = {
    "policy_id": "comparison_risk_v1",
    "policy_version": "warn-test",
    "weights": dict(cg._DEFAULT_WEIGHTS),
    "thresholds": dict(cg._DEFAULT_THRESHOLDS),
    "mandatory": {key: False for key in cg._DEFAULT_MANDATORY},
}


def _fid(year):
    return f"acme-corporation:10-k:{year}-12-31"


def _src(year):
    return f"acme-{year}.pdf"


def _chunk(year, side):
    tag = "aaa111aaa111" if side == "prev" else "bbb222bbb222"
    return f"{_src(year)}:1:{tag}"


RESOLVER = {
    _chunk(year, side): {
        "filing_id": _fid(year),
        "text": _PREV_TEXT if side == "prev" else _CURR_TEXT,
    }
    for year in YEARS
    for side in ("prev", "curr")
}.get


def _filing(year):
    return {
        "document_id": _fid(year),
        "company_key": "acme corporation",
        "company_name": "Acme Corporation",
        "form_type": "10-k",
        "filing_date": None,
        "period_end": f"{year}-12-31",
        "source_name": _src(year),
        "version_hash": "abcdefabcdef",
    }


def _ev(year, side):
    text = _PREV_TEXT if side == "prev" else _CURR_TEXT
    return {
        "document_id": _fid(year),
        "chunk_id": _chunk(year, side),
        "source_name": _src(year),
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


def _checks(kind):
    if kind == "clean":  # score 0 -> returned
        return [
            _chk("evidence_presence", "passed"),
            _chk("entity_consistency", "passed"),
            _chk("period_consistency", "passed"),
            _chk("citation_support", "passed"),
            _chk("numeric_consistency", "not_applicable", "no_numeric_claim"),
            _chk("direction_consistency", "not_applicable", "no_directional_claim"),
        ]
    if kind == "held":  # unexpected not_run -> mandatory hold
        return [
            _chk("evidence_presence", "passed"),
            _chk("entity_consistency", "passed"),
            _chk("period_consistency", "passed"),
            _chk("citation_support", "passed"),
            _chk("numeric_consistency", "not_applicable", "no_numeric_claim"),
            _chk("direction_consistency", "not_run"),
        ]
    if kind == "warn":  # score 0.325: warn band once mandatory rules are off
        return [
            _chk("evidence_presence", "passed"),
            _chk("entity_consistency", "passed"),
            _chk("period_consistency", "passed"),
            _chk("citation_support", "failed", "summary_heading_unsupported"),
            _chk("numeric_consistency", "not_applicable", "no_numeric_claim"),
            _chk("direction_consistency", "not_applicable", "no_directional_claim"),
        ]
    raise AssertionError(kind)


def _wire(kind, comparison_id, prev_year, curr_year):
    checks = _checks(kind)
    change = {
        "change_id": "chg-cyber0000001",
        "change_type": "modified",
        "category": "risk_factor",
        "section_key": SECTION,
        "summary": ORIGINAL_SUMMARY,
        "previous_evidence": [_ev(prev_year, "prev")],
        "current_evidence": [_ev(curr_year, "curr")],
        "validation": checks,
        "undetermined_reason": None,
    }
    counts = {"passed": 0, "failed": 0, "not_run": 0, "not_applicable": 0}
    for check in checks:
        counts[check["status"]] += 1
    return {
        "schema_version": "comparison.v1",
        "comparison_id": comparison_id,
        "previous_filing": _filing(prev_year),
        "current_filing": _filing(curr_year),
        "section_scope": [SECTION],
        "changes": [change],
        "validation_summary": {"total_checks": sum(counts.values()), **counts},
        "risk": {"decision": "not_evaluated", "reason_codes": [],
                 "risk_score": None, "risk_level": None},
        "review": {"status": "not_required", "review_id": None},
        "created_at": "2026-07-01T12:00:00Z",
        "producer": "item1a_detector.v2",
    }


def _hash_snapshot(snapshot):
    return hashlib.sha256(
        json.dumps({k: v for k, v in snapshot.items() if k != "created_at"},
                   sort_keys=True).encode()
    ).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Registry with a decade of Acme 10-Ks + an empty comparison db, with
    config patched so API routes hit the same storage."""
    reg = tmp_path / "registry.jsonl"
    for year in YEARS:
        filing_registry.record_outcome(
            reg, source_path=_src(year), source_name=_src(year),
            source_hash=f"h{year}", parse_status=filing_registry.PARSED,
            filing_id=_fid(year), company_key="acme corporation",
            company_name="Acme Corporation", form_type="10-k",
            period_end=f"{year}-12-31",
            document_family_id="acme-corp-10k-excerpt",
            identity_source="manifest",
        )
    db = tmp_path / "comparisons.db"
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(db))
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(reg))
    return SimpleNamespace(db=db, reg=reg)


def _case(env, kind, prev_year, curr_year, policy=None, govern=True):
    """Create + detect (synthetic result) + optionally govern one comparison."""
    record, _ = comparison_store.create_comparison(
        _fid(prev_year), _fid(curr_year), db_path=env.db, registry_path=env.reg
    )
    wire = _wire(kind, record["comparison_id"], prev_year, curr_year)
    load_comparison(wire)
    result_hash = _hash_snapshot(wire)
    comparison_store.record_result(
        record["comparison_id"], result_json=json.dumps(wire),
        result_hash=result_hash, detector_version="item1a_detector.v2",
        previous_source_hash=f"h{prev_year}", current_source_hash=f"h{curr_year}",
        db_path=env.db,
    )
    evaluation = None
    if govern:
        evaluation, _ = cg.govern(
            record["comparison_id"], db_path=env.db, policy=policy
        )
    return SimpleNamespace(
        comparison_id=record["comparison_id"], wire=wire,
        result_hash=result_hash, evaluation=evaluation,
    )


def _decide(env, case, action="approved", reason="approved_as_is",
            note="Looks correct.", edits=None):
    reviews = comparison_store.list_comparison_reviews(
        db_path=env.db, comparison_id=case.comparison_id
    )
    assert len(reviews) == 1
    review_id = reviews[0]["review_id"]
    event, created = comparison_review.decide(
        review_id, action=action, reviewer_id="reviewer@example.com",
        reason_code=reason, reviewer_note=note, edits=edits,
        db_path=env.db, resolve=RESOLVER,
    )
    return review_id, event


def _export(env, case):
    return comparison_export.export_comparison(
        case.comparison_id, case.evaluation["evaluation_id"], db_path=env.db
    )


def _blocked_case(env):
    """A synthetic 'blocked' evaluation (unreachable under policy v1)."""
    case = _case(env, "clean", 2018, 2019, govern=False)
    governed = json.loads(json.dumps(case.wire))
    governed["risk"] = {
        "decision": "blocked", "reason_codes": ["synthetic_block"],
        "risk_score": 1.0, "risk_level": "high",
    }
    governed = dump_comparison(load_comparison(governed))
    governed_hash = hashlib.sha256(
        json.dumps(governed, sort_keys=True).encode()
    ).hexdigest()
    evaluation_id = cg._evaluation_id(
        case.comparison_id, case.result_hash, "comparison_risk_v1", "blocked-test"
    )
    evaluation, _ = comparison_store.record_evaluation(
        comparison_id=case.comparison_id, evaluation_id=evaluation_id,
        comparison_result_hash=case.result_hash, policy_id="comparison_risk_v1",
        policy_version="blocked-test", risk_score=1.0, risk_level="high",
        decision="blocked", reason_codes=["synthetic_block"],
        governed_result_json=json.dumps(governed),
        governed_result_hash=governed_hash, review_id=None, db_path=env.db,
    )
    case.evaluation = evaluation
    return case


def _table_rows(db, table):
    with closing(sqlite3.connect(str(db))) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid"  # test-only literal
            ).fetchall()
        ]


def _sql(db, statement, params=()):
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute(statement, params)


# --- Release paths (tests 1-10) -----------------------------------------------


def test_returned_exports_governed_snapshot(env):
    case = _case(env, "clean", 2024, 2025)
    assert case.evaluation["decision"] == "returned"
    stored, created = _export(env, case)
    assert created is True
    export = stored["export"]
    assert export["export_schema_version"] == "comparison.export.v1"
    assert export["release_basis"] == "returned_by_policy"
    assert export["comparison_result"] == case.evaluation["governed_result"]
    assert export["review_id"] is None
    assert export["review_decision"] is None
    assert export["detector_result_hash"] == case.result_hash
    assert export["governed_result_hash"] == case.evaluation["governed_result_hash"]
    assert export["final_result_hash"] == case.evaluation["governed_result_hash"]
    assert export["policy_id"] == case.evaluation["policy_id"]
    assert export["policy_version"] == case.evaluation["policy_version"]


def test_returned_with_warning_exports_governed_snapshot(env):
    case = _case(env, "warn", 2024, 2025, policy=WARN_POLICY)
    assert case.evaluation["decision"] == "returned_with_warning"
    stored, created = _export(env, case)
    assert created is True
    export = stored["export"]
    assert export["release_basis"] == "returned_with_warning_by_policy"
    risk = export["comparison_result"]["risk"]
    assert risk["decision"] == "returned_with_warning"
    assert cg.REASON_SCORE_AT_WARN in risk["reason_codes"]
    assert export["review_decision"] is None


def test_pending_held_review_cannot_export(env):
    case = _case(env, "held", 2024, 2025)
    assert case.evaluation["decision"] == "held_for_review"
    with pytest.raises(comparison_export.ExportNotEligible) as excinfo:
        _export(env, case)
    assert excinfo.value.code == "review_pending"


def test_approved_review_exports_final_reviewed_snapshot(env):
    case = _case(env, "held", 2024, 2025)
    review_id, event = _decide(env, case)
    stored, created = _export(env, case)
    assert created is True
    export = stored["export"]
    assert export["release_basis"] == "approved_after_review"
    assert export["review_id"] == review_id
    assert export["comparison_result"] == event["reviewed_result"]
    assert export["comparison_result"]["review"]["status"] == "approved"
    assert export["final_result_hash"] == event["final_reviewed_result_hash"]
    assert export["governed_result_hash"] == case.evaluation["governed_result_hash"]


def test_approved_summary_edits_appear_in_export(env):
    case = _case(env, "held", 2024, 2025)
    _review_id, _event = _decide(
        env, case, reason="approved_with_summary_edits",
        edits=[{"change_id": "chg-cyber0000001", "summary": SUPPORTED_EDIT}],
    )
    stored, _ = _export(env, case)
    export = stored["export"]
    assert export["comparison_result"]["changes"][0]["summary"] == SUPPORTED_EDIT
    assert export["review_decision"]["edited_change_ids"] == ["chg-cyber0000001"]
    assert export["review_decision"]["reason_code"] == "approved_with_summary_edits"


def test_rejected_review_cannot_export(env):
    case = _case(env, "held", 2024, 2025)
    _decide(env, case, action="rejected", reason="rejected_other",
            note="Not supported.")
    with pytest.raises(comparison_export.ExportNotEligible) as excinfo:
        _export(env, case)
    assert excinfo.value.code == "review_rejected"


def test_blocked_decision_cannot_export(env):
    case = _blocked_case(env)
    with pytest.raises(comparison_export.ExportNotEligible) as excinfo:
        _export(env, case)
    assert excinfo.value.code == "export_not_release_eligible"


def test_review_decision_metadata_only_for_approved_exports(env):
    returned = _case(env, "clean", 2020, 2021)
    held = _case(env, "held", 2024, 2025)
    _decide(env, held)
    returned_export = _export(env, returned)[0]["export"]
    approved_export = _export(env, held)[0]["export"]
    assert returned_export["review_decision"] is None
    decision = approved_export["review_decision"]
    assert decision["action"] == "approved"
    assert decision["reviewer_id"] == "reviewer@example.com"
    assert decision["reason_code"] == "approved_as_is"
    assert decision["reviewer_note"] == "Looks correct."
    assert decision["original_governed_result_hash"] == (
        held.evaluation["governed_result_hash"]
    )
    assert decision["final_reviewed_result_hash"] == (
        approved_export["final_result_hash"]
    )


def test_reviewer_identity_labeled_self_asserted(env):
    case = _case(env, "held", 2024, 2025)
    _decide(env, case)
    export = _export(env, case)[0]["export"]
    decision = export["review_decision"]
    assert decision["reviewer_id"] == "reviewer@example.com"
    assert decision["reviewer_id_basis"] == "self_asserted_local_metadata"


# --- Eligibility enforcement (tests 11-16) ------------------------------------


def test_unknown_comparison_and_evaluation(env):
    case = _case(env, "clean", 2024, 2025)
    with pytest.raises(comparison_export.ExportNotFound) as excinfo:
        comparison_export.export_comparison(
            "cmp_nope", case.evaluation["evaluation_id"], db_path=env.db
        )
    assert excinfo.value.code == "comparison_not_found"
    with pytest.raises(comparison_export.ExportNotFound) as excinfo:
        comparison_export.export_comparison(
            case.comparison_id, "gov_nope", db_path=env.db
        )
    assert excinfo.value.code == "comparison_not_governed"


def test_evaluation_must_belong_to_comparison(env):
    first = _case(env, "clean", 2024, 2025)
    second = _case(env, "clean", 2022, 2023)
    with pytest.raises(comparison_export.ExportNotEligible) as excinfo:
        comparison_export.export_comparison(
            first.comparison_id, second.evaluation["evaluation_id"],
            db_path=env.db,
        )
    assert excinfo.value.code == "evaluation_mismatch"


def test_stale_detector_result_hash_rejected(env):
    case = _case(env, "clean", 2024, 2025)
    _sql(env.db,
         "UPDATE comparison_results SET result_hash = 'deadbeef' "
         "WHERE comparison_id = ?", (case.comparison_id,))
    with pytest.raises(comparison_export.ExportNotEligible) as excinfo:
        _export(env, case)
    assert excinfo.value.code == "evaluation_result_stale"


def test_governed_result_hash_mismatch_rejected(env):
    case = _case(env, "clean", 2024, 2025)
    _sql(env.db,
         "UPDATE comparison_governance_evaluations "
         "SET governed_result_hash = 'deadbeef' WHERE evaluation_id = ?",
         (case.evaluation["evaluation_id"],))
    with pytest.raises(comparison_export.ExportNotEligible) as excinfo:
        _export(env, case)
    assert excinfo.value.code == "export_snapshot_invalid"


def test_review_event_linkage_mismatch_rejected(env):
    case = _case(env, "held", 2024, 2025)
    _review_id, event = _decide(env, case)
    _sql(env.db,
         "UPDATE comparison_review_events "
         "SET original_governed_result_hash = 'deadbeef' WHERE event_id = ?",
         (event["event_id"],))
    with pytest.raises(comparison_export.ExportNotEligible) as excinfo:
        _export(env, case)
    assert excinfo.value.code == "review_event_mismatch"


def test_invalid_final_reviewed_snapshot_rejected(env):
    case = _case(env, "held", 2024, 2025)
    _review_id, event = _decide(env, case)
    _sql(env.db,
         "UPDATE comparison_review_events SET reviewed_result_json = ? "
         "WHERE event_id = ?",
         (json.dumps({"schema_version": "comparison.v1"}), event["event_id"]))
    with pytest.raises(comparison_export.ExportNotEligible) as excinfo:
        _export(env, case)
    assert excinfo.value.code == "export_snapshot_invalid"


def test_client_cannot_submit_workflow_state(env):
    case = _case(env, "clean", 2024, 2025)
    for extra in (
        {"decision": "returned"},
        {"reviewStatus": "approved"},
        {"reviewerId": "attacker"},
        {"result": {}},
        {"resultHash": "deadbeef"},
        {"policyId": "other"},
        {"releaseBasis": "approved_after_review"},
    ):
        response = client.post(
            f"/api/comparisons/{case.comparison_id}/exports",
            json={"evaluationId": case.evaluation["evaluation_id"], **extra},
        )
        assert response.status_code == 422, extra


# --- Idempotency, concurrency, determinism (tests 17-23) ----------------------


def test_identical_export_is_idempotent_and_preserves_timestamp(env):
    case = _case(env, "clean", 2024, 2025)
    first, created_first = _export(env, case)
    second, created_second = _export(env, case)
    assert created_first is True and created_second is False
    assert second["export_id"] == first["export_id"]
    assert second["export"] == first["export"]
    assert second["export"]["exported_at"] == first["export"]["exported_at"]
    assert second["created_at"] == first["created_at"]
    assert len(_table_rows(env.db, "comparison_exports")) == 1


def test_concurrent_identical_exports_create_one_row(env):
    case = _case(env, "clean", 2024, 2025)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _n: _export(env, case), range(8)))
    assert sum(1 for _record, created in results if created) == 1
    assert len({record["export_id"] for record, _created in results}) == 1
    payloads = {json.dumps(record["export"], sort_keys=True)
                for record, _created in results}
    assert len(payloads) == 1
    assert len(_table_rows(env.db, "comparison_exports")) == 1


def test_new_policy_evaluation_creates_separate_export(env):
    case = _case(env, "clean", 2024, 2025)
    first = _export(env, case)[0]
    v2_policy = dict(cg.POLICY, policy_version="2")
    evaluation_v2, _ = cg.govern(case.comparison_id, db_path=env.db,
                                 policy=v2_policy)
    assert evaluation_v2["decision"] == "returned"
    case_v2 = SimpleNamespace(comparison_id=case.comparison_id,
                              evaluation=evaluation_v2)
    second = _export(env, case_v2)[0]
    assert second["export_id"] != first["export_id"]
    assert second["export"]["policy_version"] == "2"
    assert len(_table_rows(env.db, "comparison_exports")) == 2
    # Both stay readable.
    assert comparison_export.get_export(first["export_id"], db_path=env.db)
    assert comparison_export.get_export(second["export_id"], db_path=env.db)


def test_persisted_export_survives_reopen(env):
    case = _case(env, "clean", 2024, 2025)
    stored, _ = _export(env, case)
    reloaded = comparison_export.get_export(stored["export_id"], db_path=env.db)
    assert reloaded is not None
    assert reloaded["export"] == stored["export"]
    listed = comparison_export.list_exports(case.comparison_id, db_path=env.db)
    assert [record["export_id"] for record in listed] == [stored["export_id"]]


def test_export_payload_round_trips(env):
    case = _case(env, "held", 2024, 2025)
    _decide(env, case)
    export = _export(env, case)[0]["export"]
    assert dump_export(load_export(export)) == export
    assert dump_export(load_export(json.dumps(export))) == export


def test_payload_and_final_hashes_deterministic(env):
    case = _case(env, "clean", 2024, 2025)
    stored, _ = _export(env, case)
    export = stored["export"]
    assert stored["export_payload_hash"] == (
        comparison_export.payload_content_hash(export)
    )
    # Timestamp-independent: only exported_at is excluded from the hash.
    shifted = dict(export, exported_at="2030-01-01T00:00:00Z")
    assert comparison_export.payload_content_hash(shifted) == (
        stored["export_payload_hash"]
    )
    assert stored["final_result_hash"] == hashlib.sha256(
        json.dumps(export["comparison_result"], sort_keys=True).encode()
    ).hexdigest()
    assert stored["export_id"] == comparison_export.export_id_for(
        case.evaluation["evaluation_id"], stored["final_result_hash"]
    )


# --- Snapshot preservation (tests 24-26) --------------------------------------


def test_export_modifies_no_workflow_rows(env):
    held = _case(env, "held", 2024, 2025)
    _decide(env, held)
    returned = _case(env, "clean", 2020, 2021)
    tables = (
        "comparisons", "comparison_results",
        "comparison_governance_evaluations", "comparison_review_items",
        "comparison_review_events",
    )
    before = {table: _table_rows(env.db, table) for table in tables}
    _export(env, held)
    _export(env, returned)
    for table in tables:
        assert _table_rows(env.db, table) == before[table], table


# --- API surface (tests 27-29 + status codes) ---------------------------------


def test_post_and_get_export_routes(env):
    case = _case(env, "clean", 2024, 2025)
    posted = client.post(
        f"/api/comparisons/{case.comparison_id}/exports",
        json={"evaluationId": case.evaluation["evaluation_id"]},
    )
    assert posted.status_code == 201
    body = posted.json()
    assert body["created"] is True
    export = body["export"]

    replay = client.post(
        f"/api/comparisons/{case.comparison_id}/exports",
        json={"evaluationId": case.evaluation["evaluation_id"]},
    )
    assert replay.status_code == 200
    assert replay.json()["created"] is False
    assert replay.json()["export"] == export

    fetched = client.get(f"/api/comparison-exports/{export['export_id']}")
    assert fetched.status_code == 200
    assert fetched.headers["content-type"].startswith("application/json")
    assert fetched.json() == export
    # The full document exposes exactly the defined contract fields.
    assert set(fetched.json()) == {
        "export_schema_version", "export_id", "comparison_id",
        "evaluation_id", "review_id", "release_basis", "policy_id",
        "policy_version", "detector_result_hash", "governed_result_hash",
        "final_result_hash", "exported_at", "comparison_result",
        "review_decision",
    }

    assert client.get("/api/comparison-exports/exp_nope").status_code == 404
    assert client.post(
        "/api/comparisons/cmp_nope/exports", json={"evaluationId": "gov_x"}
    ).status_code == 404
    pending = _case(env, "held", 2022, 2023)
    conflicted = client.post(
        f"/api/comparisons/{pending.comparison_id}/exports",
        json={"evaluationId": pending.evaluation["evaluation_id"]},
    )
    assert conflicted.status_code == 409
    assert conflicted.json()["detail"]["code"] == "review_pending"


def test_list_dto_omits_evidence_and_reviewer_note(env):
    case = _case(env, "held", 2024, 2025)
    note = "Secret reviewer prose that must stay out of list rows."
    _decide(env, case, note=note)
    _export(env, case)
    response = client.get(f"/api/comparisons/{case.comparison_id}/exports")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert set(rows[0]) == {
        "exportId", "exportSchemaVersion", "comparisonId", "evaluationId",
        "reviewId", "releaseBasis", "sourceResultHash", "finalResultHash",
        "exportPayloadHash", "createdAt",
    }
    assert rows[0]["releaseBasis"] == "approved_after_review"
    assert note not in response.text
    assert HEADING not in response.text  # no evidence excerpts or summaries
    assert client.get("/api/comparisons/cmp_nope/exports").status_code == 404


def test_storage_failure_returns_correlation_id_only(env, monkeypatch):
    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError(
            "SELECT * FROM comparison_exports at /secret/path/comparisons.db"
        )

    monkeypatch.setattr(api.comparison_export, "export_comparison", boom)
    response = client.post(
        "/api/comparisons/cmp_x/exports", json={"evaluationId": "gov_x"}
    )
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["code"] == "comparison_storage_error"
    assert detail["error_id"].startswith("err_")
    assert "secret" not in response.text
    assert "SELECT" not in response.text


# --- Storage integrity (tests 30-31) ------------------------------------------


def test_init_and_migration_idempotent_for_pre_export_store(env):
    case = _case(env, "clean", 2024, 2025)
    # Simulate a database created before the export table existed.
    _sql(env.db, "DROP TABLE comparison_exports")
    comparison_store.init_db(env.db)
    comparison_store.init_db(env.db)  # twice: idempotent
    stored, created = _export(env, case)
    assert created is True
    assert comparison_export.get_export(stored["export_id"], db_path=env.db)


def test_sqlite_integrity_after_exports(env):
    held = _case(env, "held", 2024, 2025)
    _decide(env, held)
    _export(env, held)
    returned = _case(env, "clean", 2020, 2021)
    _export(env, returned)
    _export(env, returned)
    with closing(sqlite3.connect(str(env.db))) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
