"""Tests for comparison governance: policy, evaluation, routing, API.

Synthetic valid comparison.v1 results drive the held paths (the controlled
detector labels are never altered); the real controlled corpus drives the
clean returned path. Entirely offline.
"""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api
import comparison_governance as cg
import comparison_store
import config
import filing_registry
import ingest
from governance.comparison_schema import load_comparison
from governance.policy_validation import GovernancePolicyConfigError

client = TestClient(api.app)

PREV = "acme-corporation:10-k:2024-12-31"
CURR = "acme-corporation:10-k:2025-12-31"
SECTION = "item_1a_risk_factors"


# --- Synthetic result builders ------------------------------------------------


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
    doc, src, chunk = (
        (PREV, "acme-2024.pdf", "acme-2024.pdf:1:aaa111aaa111")
        if side == "prev"
        else (CURR, "acme-2025.pdf", "acme-2025.pdf:1:bbb222bbb222")
    )
    return {
        "document_id": doc,
        "chunk_id": chunk,
        "source_name": src,
        "page": 1,
        "section_key": SECTION,
        "section_title": "ITEM 1A. RISK FACTORS",
        "excerpt": "Cybersecurity and Data Security Risks We face risks.",
        "content_hash": "cafecafecafe",
    }


def _chk(name, status, reason_code=None, version="x.v1"):
    return {
        "check": name,
        "status": status,
        "reason_code": reason_code
        or ("required_reason" if status == "failed" else None),
        "detail": f"{name} {status}.",
        "validator_version": None if status == "not_run" else version,
    }


_ALL_PASSED = [
    _chk("evidence_presence", "passed"),
    _chk("entity_consistency", "passed"),
    _chk("period_consistency", "passed"),
    _chk("citation_support", "passed"),
    _chk("numeric_consistency", "not_applicable", reason_code="no_numeric_claim"),
    _chk("direction_consistency", "not_applicable", reason_code="no_directional_claim"),
]


def _chg(change_type="modified", key="cyber", checks=None, reason=None):
    change = {
        "change_id": f"chg-{hashlib.sha1(f'{change_type}:{key}'.encode()).hexdigest()[:12]}",
        "change_type": change_type,
        "category": "risk_factor",
        "section_key": SECTION,
        "summary": f"Risk factor '{key}' changed between the two filing periods."
        if change_type == "modified"
        else f"Risk factor '{key}' could not be classified.",
        "previous_evidence": [],
        "current_evidence": [],
        "validation": list(checks if checks is not None else _ALL_PASSED),
        "undetermined_reason": reason,
    }
    if change_type == "modified":
        change["previous_evidence"] = [_ev("prev")]
        change["current_evidence"] = [_ev("curr")]
    elif change_type == "undetermined":
        change["undetermined_reason"] = reason or (
            "ambiguous_unit_alignment: heading not unique"
        )
    return change


def _summary_for(changes):
    counts = {"passed": 0, "failed": 0, "not_run": 0, "not_applicable": 0}
    for change in changes:
        for check in change["validation"]:
            counts[check["status"]] += 1
    return {"total_checks": sum(counts.values()), **counts}


def _wire(changes):
    doc = {
        "schema_version": "comparison.v1",
        "comparison_id": "cmp-synthetic",
        "previous_filing": _filing(PREV, "2024-12-31", "acme-2024.pdf"),
        "current_filing": _filing(CURR, "2025-12-31", "acme-2025.pdf"),
        "section_scope": [SECTION],
        "changes": changes,
        "validation_summary": _summary_for(changes),
        "risk": {
            "decision": "not_evaluated",
            "reason_codes": [],
            "risk_score": None,
            "risk_level": None,
        },
        "review": {"status": "not_required", "review_id": None},
        "created_at": "2026-07-01T12:00:00Z",
        "producer": "item1a_detector.v2",
    }
    load_comparison(doc)  # builder sanity: synthetic inputs must be schema-valid
    return doc


@pytest.fixture
def reg(tmp_path):
    path = tmp_path / "registry.jsonl"
    for src, fid, pe, h in (
        ("acme-2024.pdf", PREV, "2024-12-31", "h24"),
        ("acme-2025.pdf", CURR, "2025-12-31", "h25"),
    ):
        filing_registry.record_outcome(
            path, source_path=src, source_name=src, source_hash=h,
            parse_status=filing_registry.PARSED, filing_id=fid,
            company_key="acme corporation", company_name="Acme Corporation",
            form_type="10-k", period_end=pe,
            document_family_id="acme-corp-10k-excerpt", identity_source="manifest",
        )
    return path


@pytest.fixture
def db(tmp_path):
    return tmp_path / "comparisons.db"


def _persist(db, reg, changes):
    """Create a comparison and store a synthetic detected result for it."""
    record, _ = comparison_store.create_comparison(
        PREV, CURR, db_path=db, registry_path=reg
    )
    wire = _wire(changes)
    wire["comparison_id"] = record["comparison_id"]
    stable = {k: v for k, v in wire.items() if k != "created_at"}
    result_hash = hashlib.sha256(
        json.dumps(stable, sort_keys=True).encode()
    ).hexdigest()
    comparison_store.record_result(
        record["comparison_id"],
        result_json=json.dumps(wire),
        result_hash=result_hash,
        detector_version="item1a_detector.v2",
        previous_source_hash="h24",
        current_source_hash="h25",
        db_path=db,
    )
    return record["comparison_id"], result_hash


def _reviews(db, **kwargs):
    return comparison_store.list_comparison_reviews(db_path=db, **kwargs)


# --- Decision semantics on synthetic results (tests 3-12) ---------------------


def test_clean_synthetic_result_returns_without_review(db, reg):
    """Passed + not_applicable checks only -> returned, low, no review item."""
    comparison_id, _ = _persist(db, reg, [_chg()])
    record, created = cg.govern(comparison_id, db_path=db)
    assert created is True
    assert record["decision"] == "returned"
    assert record["risk_score"] == 0.0
    assert record["risk_level"] == "low"
    assert record["reason_codes"] == []
    assert _reviews(db) == []  # test 2


@pytest.mark.parametrize(
    "failed_check,expected_reason",
    [
        ("citation_support", "failed_check_citation_support"),  # test 3
        ("evidence_presence", "failed_check_evidence_presence"),  # test 5
        ("entity_consistency", "failed_check_entity_consistency"),  # test 6
        ("period_consistency", "failed_check_period_consistency"),  # test 7
    ],
)
def test_failed_check_holds(db, reg, failed_check, expected_reason):
    checks = [
        _chk(c["check"], "failed" if c["check"] == failed_check else c["status"],
             reason_code=c["reason_code"])
        for c in _ALL_PASSED
    ]
    comparison_id, _ = _persist(db, reg, [_chg(checks=checks)])
    record, _ = cg.govern(comparison_id, db_path=db)
    assert record["decision"] == "held_for_review"
    assert expected_reason in record["reason_codes"]
    reviews = _reviews(db)
    assert len(reviews) == 1  # exactly one pending item
    assert reviews[0]["evaluation_id"] == record["evaluation_id"]
    assert reviews[0]["status"] == "pending"


def test_undetermined_change_holds(db, reg):
    """Test 4."""
    comparison_id, _ = _persist(
        db, reg, [_chg("undetermined", key="ambiguous")]
    )
    record, _ = cg.govern(comparison_id, db_path=db)
    assert record["decision"] == "held_for_review"
    assert "undetermined_changes_present" in record["reason_codes"]
    assert len(_reviews(db)) == 1


def test_unexpected_not_run_holds(db, reg):
    """Test 8: all six validators are implemented, so not_run is a hold."""
    checks = list(_ALL_PASSED[:-1]) + [_chk("direction_consistency", "not_run")]
    comparison_id, _ = _persist(db, reg, [_chg(checks=checks)])
    record, _ = cg.govern(comparison_id, db_path=db)
    assert record["decision"] == "held_for_review"
    assert "unexpected_not_run_check" in record["reason_codes"]


def test_not_applicable_never_counts_as_failure(db, reg):
    """Test 9: an all-not_applicable change still returns cleanly."""
    checks = [_chk(c["check"], "not_applicable", reason_code="n_a") for c in _ALL_PASSED]
    comparison_id, _ = _persist(db, reg, [_chg(checks=checks)])
    record, _ = cg.govern(comparison_id, db_path=db)
    assert record["decision"] == "returned"
    assert record["risk_score"] == 0.0
    assert record["reason_codes"] == []


def test_score_deterministic_bounded_and_denominators():
    """Tests 10-11: pure evaluate() — bounds, determinism, denominators."""
    # Empty changes: every denominator is empty -> score 0, returned.
    empty = cg.evaluate(_wire([]))
    assert empty["risk_score"] == 0.0
    assert empty["decision"] == "returned"
    assert empty["signals"] == {
        "failed_validation_rate": 0.0,
        "undetermined_change_rate": 0.0,
        "evidence_failure_rate": 0.0,
    }

    # Everything failed + undetermined: score capped at 1.0 exactly.
    worst_checks = [
        _chk(c["check"], "failed", reason_code="r") for c in _ALL_PASSED
    ]
    worst = _wire([_chg("undetermined", key="u", checks=worst_checks)])
    outcome = cg.evaluate(worst)
    assert outcome["risk_score"] == 1.0
    assert outcome["risk_level"] == "high"
    assert outcome["decision"] == "held_for_review"

    # Mixed: 1 failed / 5 passed+failed applicable in one of two changes.
    mixed_checks = [
        _chk("evidence_presence", "passed"),
        _chk("entity_consistency", "failed", reason_code="r"),
        _chk("period_consistency", "passed"),
        _chk("citation_support", "passed"),
        _chk("numeric_consistency", "not_applicable", reason_code="n"),
        _chk("direction_consistency", "not_run"),
    ]
    mixed = _wire([_chg(checks=mixed_checks), _chg(key="second")])
    outcome_a = cg.evaluate(mixed)
    outcome_b = cg.evaluate(mixed)
    assert outcome_a == outcome_b  # deterministic
    # applicable = 4 passed+failed in change one + 4 in change two = 8; 1 failed.
    assert outcome_a["signals"]["failed_validation_rate"] == 1 / 8
    assert outcome_a["signals"]["undetermined_change_rate"] == 0.0
    assert outcome_a["signals"]["evidence_failure_rate"] == 0.0
    assert 0.0 <= outcome_a["risk_score"] <= 1.0


def test_low_weighted_score_still_held_by_mandatory_rule(db, reg):
    """Test 12: one failed check among many passed -> tiny score, low level,
    but the mandatory rule holds it anyway."""
    checks = [
        _chk(c["check"], "failed" if c["check"] == "period_consistency" else "passed")
        if c["check"] == "period_consistency"
        else _chk(c["check"], "passed")
        for c in _ALL_PASSED
    ]
    changes = [_chg(key=f"unit-{i}", checks=list(_ALL_PASSED)) for i in range(4)]
    changes.append(_chg(key="bad", checks=checks))
    comparison_id, _ = _persist(db, reg, changes)
    record, _ = cg.govern(comparison_id, db_path=db)
    assert record["risk_level"] == "low"  # weighted score stays under warn
    assert record["risk_score"] < 0.25
    assert record["decision"] == "held_for_review"
    assert "failed_check_period_consistency" in record["reason_codes"]
    assert "risk_score_at_or_above_hold_threshold" not in record["reason_codes"]


def test_returned_with_warning_reachable_only_without_mandatory_rules():
    """Honest vocabulary: the warning band exists but needs mandatory rules
    off; the default policy always holds first."""
    relaxed = json.loads(json.dumps(cg.POLICY))
    relaxed["mandatory"] = {k: False for k in relaxed["mandatory"]}
    checks_half_failed = [
        _chk("evidence_presence", "failed", reason_code="r"),
        _chk("entity_consistency", "failed", reason_code="r"),
        _chk("period_consistency", "passed"),
        _chk("citation_support", "passed"),
    ]
    wire = _wire([_chg(checks=checks_half_failed)])
    outcome = cg.evaluate(wire, relaxed)
    # score = 0.5*(2/4) + 0.3*0 + 0.2*1.0 = 0.45 -> warning band under relaxed.
    assert outcome["decision"] == "returned_with_warning"
    assert outcome["risk_level"] == "medium"
    assert "risk_score_at_or_above_warn_threshold" in outcome["reason_codes"]

    # The same result under the DEFAULT policy is held, never warned.
    assert cg.evaluate(wire)["decision"] == "held_for_review"


# --- Governed snapshots and immutability (tests 13-15) ------------------------


def test_governed_returned_snapshot_valid_and_original_untouched(db, reg):
    comparison_id, result_hash = _persist(db, reg, [_chg()])
    record, _ = cg.govern(comparison_id, db_path=db)

    governed = record["governed_result"]
    model = load_comparison(governed)  # test 13: schema-valid
    assert model.risk.decision == "returned"
    assert model.review.status == "not_required"
    assert governed["changes"] == _wire([_chg()])["changes"]  # preserved
    assert governed["producer"] == "item1a_detector.v2"

    # Test 15: the detector result row is untouched.
    stored = comparison_store.get_result(comparison_id, db_path=db)
    assert stored["result"]["risk"]["decision"] == "not_evaluated"
    assert stored["result"]["review"]["status"] == "not_required"
    assert stored["result_hash"] == result_hash


def test_governed_held_snapshot_valid_with_pending_review(db, reg):
    comparison_id, _ = _persist(db, reg, [_chg("undetermined", key="u")])
    record, _ = cg.govern(comparison_id, db_path=db)
    governed = record["governed_result"]
    model = load_comparison(governed)  # test 14
    assert model.risk.decision == "held_for_review"
    assert model.review.status == "pending"
    assert model.review.review_id == _reviews(db)[0]["review_id"]
    assert model.review.review_id.startswith("crev_")
    # Governed hash is deterministic: recompute.
    assert record["governed_result_hash"] == hashlib.sha256(
        json.dumps(governed, sort_keys=True).encode()
    ).hexdigest()


# --- Idempotency, concurrency, versioning (tests 16-21) -----------------------


def test_identical_evaluation_is_idempotent(db, reg):
    comparison_id, _ = _persist(db, reg, [_chg("undetermined", key="u")])
    first, created_first = cg.govern(comparison_id, db_path=db)
    second, created_second = cg.govern(comparison_id, db_path=db)
    assert (created_first, created_second) == (True, False)
    assert second == first
    assert len(_reviews(db)) == 1  # test 7 of G: exactly one pending item


def test_concurrent_evaluations_one_row_one_review(db, reg):
    """Tests 17-18."""
    comparison_id, _ = _persist(db, reg, [_chg("undetermined", key="u")])

    def attempt(_):
        return cg.govern(comparison_id, db_path=db)

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))

    assert sum(1 for _r, created in outcomes if created) == 1
    assert len({r["evaluation_id"] for r, _ in outcomes}) == 1
    with closing(sqlite3.connect(db)) as conn:
        evals = conn.execute(
            "SELECT COUNT(*) FROM comparison_governance_evaluations"
        ).fetchone()[0]
        reviews = conn.execute(
            "SELECT COUNT(*) FROM comparison_review_items"
        ).fetchone()[0]
    assert (evals, reviews) == (1, 1)


def test_new_policy_version_creates_separate_readable_evaluation(db, reg):
    """Tests 19-20."""
    comparison_id, _ = _persist(db, reg, [_chg()])
    old, _ = cg.govern(comparison_id, db_path=db)

    bumped = json.loads(json.dumps(cg.POLICY))
    bumped["policy_version"] = "2"
    new, created = cg.govern(comparison_id, db_path=db, policy=bumped)
    assert created is True
    assert new["evaluation_id"] != old["evaluation_id"]
    assert new["policy_version"] == "2"

    both = comparison_store.list_evaluations(comparison_id, db_path=db)
    assert {e["evaluation_id"] for e in both} == {
        old["evaluation_id"], new["evaluation_id"]
    }
    assert comparison_store.get_evaluation(old["evaluation_id"], db_path=db) == old


def test_transaction_failure_leaves_no_partial_rows(db, reg):
    """Test 21: a held evaluation whose review insert fails rolls both back."""
    comparison_id, result_hash = _persist(db, reg, [_chg("undetermined", key="u")])
    with pytest.raises(sqlite3.IntegrityError):
        comparison_store.record_evaluation(
            comparison_id=comparison_id,
            evaluation_id="gov_deadbeefdeadbeef",
            comparison_result_hash=result_hash,
            policy_id="p", policy_version="1",
            risk_score=1.0, risk_level="high", decision="held_for_review",
            reason_codes=["x"],
            governed_result_json="{}", governed_result_hash="h",
            review_id=None,  # NULL primary key -> review insert fails
            db_path=db,
        )
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM comparison_governance_evaluations"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM comparison_review_items"
        ).fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_stale_result_hash_cannot_attach(db, reg):
    """G5: an evaluation for a hash that is not the current stored result."""
    comparison_id, _ = _persist(db, reg, [_chg()])
    with pytest.raises(
        comparison_store.ComparisonLifecycleError,
        match="not the\\s+comparison's current",
    ) as excinfo:
        comparison_store.record_evaluation(
            comparison_id=comparison_id,
            evaluation_id="gov_feedfacefeedface",
            comparison_result_hash="0" * 64,
            policy_id="p", policy_version="1",
            risk_score=0.0, risk_level="low", decision="returned",
            reason_codes=[], governed_result_json="{}",
            governed_result_hash="h", review_id=None, db_path=db,
        )
    assert excinfo.value.status == "stale_result_hash"


# --- Policy validation (tests 22-24) ------------------------------------------


def _policy_yaml(tmp_path, body):
    path = tmp_path / "policy.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_VALID_POLICY = """comparison_risk_policy:
  policy_id: comparison_risk_v1
  policy_version: "1"
signal_weights:
  failed_validation_rate_weight: 0.5
  undetermined_change_rate_weight: 0.3
  evidence_failure_rate_weight: 0.2
risk_thresholds:
  warn_at_or_above: 0.25
  hold_at_or_above: 0.50
mandatory_review:
  hold_on_any_failed_check: true
  hold_on_any_undetermined_change: true
  hold_on_unexpected_not_run: true
"""


def test_missing_policy_uses_documented_defaults(tmp_path):
    """Test 23: absent file -> baked-in defaults identical to the YAML."""
    policy = cg.load_policy(tmp_path / "missing.yaml")
    from_file = cg.load_policy(_policy_yaml(tmp_path, _VALID_POLICY))
    assert policy == from_file  # defaults kept identical to the checked-in YAML
    assert policy == cg.POLICY


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("failed_validation_rate_weight: 0.5", None),  # baseline sanity, no error
        ("failed_validation_rate_weight: '0.5'", "must be a number"),
        ("failed_validation_rate_weight: true", "must be a number"),
        ("failed_validation_rate_weight: .nan", "finite"),
        ("failed_validation_rate_weight: 1.5", "between"),
        ("hold_on_any_failed_check: true", None),
        ("hold_on_any_failed_check: maybe", "must be a boolean"),
        ("policy_id: comparison_risk_v1", None),
        ("policy_id: ''", "non-empty"),
    ],
)
def test_policy_field_validation(tmp_path, mutation, match):
    """Test 24: booleans, numbers, finiteness, range, id validation."""
    field = mutation.split(":")[0]
    body = "\n".join(
        line if line.strip().split(":")[0] != field else
        "  " + mutation
        for line in _VALID_POLICY.splitlines()
    )
    path = _policy_yaml(tmp_path, body)
    if match is None:
        assert cg.load_policy(path)["policy_id"] == "comparison_risk_v1"
    else:
        with pytest.raises(GovernancePolicyConfigError, match=match):
            cg.load_policy(path)


def test_policy_structural_validation(tmp_path):
    """Test 22/24: sections, ordering, weight sum, unknown keys, version."""
    with pytest.raises(GovernancePolicyConfigError, match="all four sections"):
        cg.load_policy(_policy_yaml(tmp_path, "signal_weights:\n  x: 1\n"))

    with pytest.raises(GovernancePolicyConfigError, match="sum to 1.0"):
        cg.load_policy(
            _policy_yaml(
                tmp_path,
                _VALID_POLICY.replace(
                    "failed_validation_rate_weight: 0.5",
                    "failed_validation_rate_weight: 0.9",
                ),
            )
        )

    with pytest.raises(GovernancePolicyConfigError, match="must be <="):
        cg.load_policy(
            _policy_yaml(
                tmp_path,
                _VALID_POLICY.replace("warn_at_or_above: 0.25", "warn_at_or_above: 0.9"),
            )
        )

    with pytest.raises(GovernancePolicyConfigError, match="unknown signal_weights"):
        cg.load_policy(
            _policy_yaml(
                tmp_path,
                _VALID_POLICY.replace(
                    "signal_weights:", "signal_weights:\n  surprise_weight: 0.0"
                ),
            )
        )

    with pytest.raises(GovernancePolicyConfigError, match="policy_version"):
        cg.load_policy(
            _policy_yaml(
                tmp_path, _VALID_POLICY.replace('policy_version: "1"', "policy_version: ''")
            )
        )

    with pytest.raises(GovernancePolicyConfigError, match="invalid YAML"):
        cg.load_policy(_policy_yaml(tmp_path, "comparison_risk_policy: ["))

    # Numeric policy_version normalizes to a string.
    numeric_version = cg.load_policy(
        _policy_yaml(tmp_path, _VALID_POLICY.replace('policy_version: "1"', "policy_version: 3"))
    )
    assert numeric_version["policy_version"] == "3"


# --- Controlled corpus integration (tests 1-2) --------------------------------


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """Real controlled corpus -> detected comparison, once per module."""
    from langchain_core.embeddings import Embeddings
    from langchain_chroma import Chroma
    import comparison_detector

    class Fake(Embeddings):
        def embed_documents(self, texts):
            return [[1.0, 2.0] for _ in texts]

        def embed_query(self, text):
            raise AssertionError("no vector retrieval")

    td = tmp_path_factory.mktemp("governance-corpus")
    registry = td / "registry.jsonl"
    manifest = filing_registry.load_manifest()
    docs = ingest.load_documents(
        config.DOCS_DIR, manifest=manifest, registry_path=registry
    )
    chunks = ingest.split_documents(docs)
    unique, ids = ingest._dedupe_by_id(chunks)
    counts = {}
    for chunk in unique:
        rel = chunk.metadata.get("source_path")
        counts[rel] = counts.get(rel, 0) + 1
    filing_registry.update_chunk_counts(counts, registry)
    chroma = Chroma(
        collection_name="govidx",
        persist_directory=str(td / "chroma"),
        embedding_function=Fake(),
    )
    chroma.add_documents(documents=unique, ids=ids)

    db = td / "comparisons.db"
    record, _ = comparison_store.create_comparison(
        PREV, CURR, db_path=db, registry_path=registry
    )
    comparison_detector.detect(
        record["comparison_id"], db_path=db, registry_path=registry,
        chroma_client=chroma,
    )
    return SimpleNamespace(db=db, comparison_id=record["comparison_id"])


def test_controlled_clean_result_returns(corpus):
    """Tests 1-2: the real detected Acme result has no failures or
    undetermined changes -> returned, no review item, governed doc valid."""
    record, created = cg.govern(corpus.comparison_id, db_path=corpus.db)
    assert record["decision"] == "returned"
    assert record["risk_score"] == 0.0
    assert record["risk_level"] == "low"
    assert record["reason_codes"] == []
    assert _reviews(corpus.db) == []
    model = load_comparison(record["governed_result"])
    assert model.risk.decision == "returned"
    assert model.review.status == "not_required"
    # Detector result untouched.
    stored = comparison_store.get_result(corpus.comparison_id, db_path=corpus.db)
    assert stored["result"]["risk"]["decision"] == "not_evaluated"


# --- API surface (tests 25-28) ------------------------------------------------


@pytest.fixture
def api_env(tmp_path, monkeypatch, reg):
    db = tmp_path / "api.db"
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(db))
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(reg))
    return SimpleNamespace(db=db, reg=reg, root=tmp_path)


def test_governance_api_lifecycle_and_codes(api_env):
    """Test 25."""
    assert client.post("/api/comparisons/cmp_nope/governance").status_code == 404
    assert client.get("/api/comparisons/cmp_nope/governance").status_code == 404

    # Created but undetected -> 404 (absent detector result).
    record, _ = comparison_store.create_comparison(
        PREV, CURR, db_path=api_env.db, registry_path=api_env.reg
    )
    undetected = client.post(f"/api/comparisons/{record['comparison_id']}/governance")
    assert undetected.status_code == 404

    comparison_id, _ = _persist(api_env.db, api_env.reg, [_chg("undetermined", key="u")])
    assert comparison_id == record["comparison_id"]

    first = client.post(f"/api/comparisons/{comparison_id}/governance")
    assert first.status_code == 201
    body = first.json()
    assert body["created"] is True
    assert body["evaluation"]["decision"] == "held_for_review"

    second = client.post(f"/api/comparisons/{comparison_id}/governance")
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert (
        second.json()["evaluation"]["evaluationId"]
        == body["evaluation"]["evaluationId"]
    )

    fetched = client.get(f"/api/comparisons/{comparison_id}/governance")
    assert fetched.status_code == 200
    assert fetched.json() == second.json()["evaluation"]

    reviews = client.get("/api/comparison-reviews")
    assert reviews.status_code == 200
    assert len(reviews.json()) == 1
    filtered = client.get(
        "/api/comparison-reviews", params={"comparison_id": "cmp_other"}
    )
    assert filtered.json() == []


def test_invalid_stored_result_is_409(api_env):
    comparison_id, _ = _persist(api_env.db, api_env.reg, [_chg()])
    with closing(sqlite3.connect(api_env.db)) as conn, conn:
        conn.execute(
            "UPDATE comparison_results SET result_json = '{\"schema_version\": \"comparison.v1\"}' "
            "WHERE comparison_id = ?",
            (comparison_id,),
        )
    response = client.post(f"/api/comparisons/{comparison_id}/governance")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "comparison_result_invalid"


def test_governance_dto_and_review_list_are_allowlisted(api_env):
    """Tests 26-27: no storage detail; review rows carry no excerpts."""
    comparison_id, _ = _persist(api_env.db, api_env.reg, [_chg("undetermined", key="u")])
    posted = client.post(f"/api/comparisons/{comparison_id}/governance")
    text = posted.text
    assert str(api_env.root) not in text
    assert ".db" not in text and "sqlite" not in text.lower()
    assert "/Users/" not in text
    evaluation = posted.json()["evaluation"]
    assert set(evaluation) == {
        "evaluationId", "comparisonId", "policyId", "policyVersion",
        "riskScore", "riskLevel", "decision", "reasonCodes", "evaluatedAt",
        "comparisonResultHash", "governedResultHash", "governedResult",
    }

    listing = client.get("/api/comparison-reviews")
    for item in listing.json():
        assert set(item) == {
            "reviewId", "comparisonId", "evaluationId", "status",
            "riskScore", "riskLevel", "reasonCodes", "createdAt",
        }
    assert "excerpt" not in listing.text
    assert "evidence" not in listing.text


def test_governance_storage_failure_sanitized(api_env, monkeypatch, caplog):
    """Test 28."""
    import logging

    secret = "disk I/O error on /secret/comparisons.db"

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError(secret)

    monkeypatch.setattr(api.comparison_governance, "govern", boom)
    with caplog.at_level(logging.ERROR, logger="api"):
        response = client.post("/api/comparisons/cmp_x/governance")
    assert response.status_code == 500
    assert "/secret" not in response.text
    detail = response.json()["detail"]
    assert detail["code"] == "comparison_storage_error"
    assert detail["error_id"].startswith("err_")
    assert secret in caplog.text
