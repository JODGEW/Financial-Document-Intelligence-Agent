"""Tests for durable detection attempts and the explicit detection lifecycle.

Builds the controlled corpus once into a temporary Chroma index (fake
embeddings — the detector reads by metadata and never embeds) plus a temporary
filing registry, then exercises the attempt state machine, transition events,
transaction boundaries, concurrency, the process-interruption boundary,
migration, and the read-only API surface. Entirely offline: no AWS
credentials, no Bedrock, no network.
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings

import api
import comparison_detector
import comparison_store
import config
import filing_registry
import ingest
from comparison_detector import (
    DETECTOR_VERSION,
    DetectionInProgress,
    DetectionInternalError,
    DetectionNotReady,
    detect,
    detect_with_attempt,
)
from comparison_store import (
    ATTEMPT_FAILED,
    ATTEMPT_RUNNING,
    ATTEMPT_SUCCEEDED,
    DetectionStateError,
    STATUS_DETECTED,
    STATUS_DETECTING,
    STATUS_FAILED,
    STATUS_READY_FOR_DETECTION,
    WORKFLOW_VERSION,
    init_db,
)
from tests.auth_helpers import authorization_headers

client = TestClient(api.app, headers=authorization_headers())

PREV_ID = "acme-corporation:10-k:2024-12-31"
CURR_ID = "acme-corporation:10-k:2025-12-31"


class _FakeEmbeddings(Embeddings):
    """Offline embeddings for seeding; raises if detection ever embeds."""

    def embed_documents(self, texts):
        return [[float(len(t) % 7), 1.0] for t in texts]

    def embed_query(self, text):
        raise AssertionError("vector retrieval must not be used by the detector")


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """Controlled corpus ingested into a temp registry + temp Chroma once."""
    from langchain_chroma import Chroma

    td = tmp_path_factory.mktemp("attempt-corpus")
    registry = td / "registry.jsonl"
    docs = ingest.load_documents(
        config.DOCS_DIR,
        manifest=filing_registry.load_manifest(),
        registry_path=registry,
    )
    chunks = ingest.split_documents(docs)
    unique, ids = ingest._dedupe_by_id(chunks)
    counts = {}
    for chunk in unique:
        rel = chunk.metadata.get("source_path")
        counts[rel] = counts.get(rel, 0) + 1
    filing_registry.update_chunk_counts(counts, registry)

    chroma = Chroma(
        collection_name="attemptidx",
        persist_directory=str(td / "chroma"),
        embedding_function=_FakeEmbeddings(),
    )
    chroma.add_documents(documents=unique, ids=ids)
    return SimpleNamespace(registry=registry, chroma=chroma)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "comparisons.db"


def _comparison(corpus, db):
    record, _ = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry
    )
    return record["comparison_id"]


def _detect(corpus, db, comparison_id):
    return detect_with_attempt(
        comparison_id,
        db_path=db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )


def _hashes(corpus):
    return (
        filing_registry.get_filing(PREV_ID, corpus.registry)["source_hash"],
        filing_registry.get_filing(CURR_ID, corpus.registry)["source_hash"],
    )


def _start(corpus, db, comparison_id):
    previous_hash, current_hash = _hashes(corpus)
    return comparison_store.start_detection_attempt(
        comparison_id,
        detector_version=DETECTOR_VERSION,
        workflow_version=WORKFLOW_VERSION,
        previous_source_hash=previous_hash,
        current_source_hash=current_hash,
        db_path=db,
    )


def _status(db, comparison_id):
    return comparison_store.get_comparison(comparison_id, db_path=db)["status"]


def _event_types(db, attempt_id):
    return [
        event["event_type"]
        for event in comparison_store.list_detection_events(attempt_id, db_path=db)
    ]


def _sql(db, statement, params=()):
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute(statement, params)


# --- Successful detection (tests 1-6) ----------------------------------------


def test_normal_detection_creates_one_attempt_numbered_one(corpus, db):
    """Tests 1-3, 5: one succeeded attempt, number 1, hash matching the result."""
    comparison_id = _comparison(corpus, db)
    result, created, attempt_id = _detect(corpus, db, comparison_id)
    assert created is True
    assert attempt_id and attempt_id.startswith("att_")

    attempts = comparison_store.list_detection_attempts(comparison_id, db_path=db)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["attempt_id"] == attempt_id
    assert attempt["attempt_number"] == 1
    assert attempt["status"] == ATTEMPT_SUCCEEDED
    assert attempt["detector_version"] == DETECTOR_VERSION
    assert attempt["workflow_version"] == WORKFLOW_VERSION
    assert attempt["started_at"] and attempt["finished_at"]
    assert attempt["failure_code"] is None and attempt["failure_summary"] is None

    stored = comparison_store.get_result(comparison_id, db_path=db)
    assert attempt["result_hash"] == stored["result_hash"]
    assert (attempt["previous_source_hash"], attempt["current_source_hash"]) == _hashes(
        corpus
    )
    assert _status(db, comparison_id) == STATUS_DETECTED
    assert result == stored["result"]


def test_successful_detection_creates_started_and_succeeded_events(corpus, db):
    """Test 4: exactly one started + one succeeded event, deterministically
    ordered, carrying the result hash and no failure fields."""
    comparison_id = _comparison(corpus, db)
    _result, _created, attempt_id = _detect(corpus, db, comparison_id)
    events = comparison_store.list_detection_events(attempt_id, db_path=db)
    assert [event["event_type"] for event in events] == [
        "detection_started",
        "detection_succeeded",
    ]
    assert [event["event_seq"] for event in events] == [0, 1]
    assert events[0]["result_hash"] is None
    assert events[1]["result_hash"] == comparison_store.get_result(
        comparison_id, db_path=db
    )["result_hash"]
    assert all(event["failure_code"] is None for event in events)
    assert all(event["comparison_id"] == comparison_id for event in events)


def test_repeat_after_success_creates_no_second_attempt(corpus, db):
    """Test 6: the idempotent replay returns the stored result and the ORIGINAL
    attempt id, without starting a new execution."""
    comparison_id = _comparison(corpus, db)
    first, created_first, attempt_first = _detect(corpus, db, comparison_id)
    second, created_second, attempt_second = _detect(corpus, db, comparison_id)
    assert created_first is True and created_second is False
    assert second == first
    assert attempt_second == attempt_first
    assert len(comparison_store.list_detection_attempts(comparison_id, db_path=db)) == 1
    assert _event_types(db, attempt_first) == [
        "detection_started",
        "detection_succeeded",
    ]


# --- The PUBLIC entry point is attempt-tracked --------------------------------
#
# detect() is the two-tuple entry point every existing caller uses (the API
# route, the CLI, the regression runner). These tests pin that it orchestrates
# attempts rather than persisting a result behind their back, so a future
# refactor cannot quietly reintroduce untracked execution.


def test_public_detect_creates_exactly_one_succeeded_attempt(corpus, db):
    """Issue-1 tests 1-2: the public two-tuple detect() — not just
    detect_with_attempt — produces one succeeded attempt with both events."""
    comparison_id = _comparison(corpus, db)
    outcome = detect(
        comparison_id,
        db_path=db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    # Issue-1 test 6: the legacy return shape is exactly a two-tuple.
    assert isinstance(outcome, tuple) and len(outcome) == 2
    result, created = outcome
    assert created is True and isinstance(result, dict)

    attempts = comparison_store.list_detection_attempts(comparison_id, db_path=db)
    assert len(attempts) == 1
    assert attempts[0]["attempt_number"] == 1
    assert attempts[0]["status"] == ATTEMPT_SUCCEEDED
    assert attempts[0]["result_hash"] == comparison_store.get_result(
        comparison_id, db_path=db
    )["result_hash"]
    assert _event_types(db, attempts[0]["attempt_id"]) == [
        "detection_started",
        "detection_succeeded",
    ]
    assert _status(db, comparison_id) == STATUS_DETECTED


def test_public_detect_replay_creates_no_new_attempt(corpus, db):
    """Issue-1 test 3: the idempotent replay through detect() returns the
    stored result and starts no second execution."""
    comparison_id = _comparison(corpus, db)
    first, created_first = detect(
        comparison_id, db_path=db, registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    second, created_second = detect(
        comparison_id, db_path=db, registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    assert created_first is True and created_second is False
    assert second == first
    assert len(comparison_store.list_detection_attempts(comparison_id, db_path=db)) == 1


def test_public_detect_never_uses_the_low_level_record_result(corpus, db):
    """Issue-1 test 5: no public runtime detection path can persist a fresh
    result through the untracked low-level primitive.

    Enforced by making record_result explode: detection must still succeed.
    """
    comparison_id = _comparison(corpus, db)

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "detection must not persist a result through record_result"
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(comparison_store, "record_result", forbidden)
        result, created = detect(
            comparison_id, db_path=db, registry_path=corpus.registry,
            chroma_client=corpus.chroma,
        )
    assert created is True and result
    assert (
        comparison_store.list_detection_attempts(comparison_id, db_path=db)[0][
            "status"
        ]
        == ATTEMPT_SUCCEEDED
    )


def test_no_production_module_calls_record_result():
    """Issue-1 test 5 (static half): the audit that keeps it that way.

    Only comparison_store may reference record_result (it defines it) and only
    tests may call it. A new production call site fails this test.
    """
    import ast

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted(root.glob("*.py")) + sorted((root / "scripts").glob("*.py")):
        if path.name == "comparison_store.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "record_result"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], offenders


def test_regression_execution_path_creates_attempts(tmp_path):
    """Issue-1 test 4: the controlled regression runner's own detection path
    leaves one succeeded attempt and the expected two events."""
    from scripts import eval_comparison_regression as ecr

    labels = ecr.load_labels()
    fixture = next(
        item for item in labels["fixtures"] if item["fixture_id"] == "no-change-pair"
    )
    outcome = ecr.run_fixture(fixture, tmp_path)
    fixture_db = tmp_path / "comparisons.db"
    attempts = comparison_store.list_detection_attempts(
        outcome["comparison_id"], db_path=fixture_db
    )
    assert len(attempts) == 1
    assert attempts[0]["status"] == ATTEMPT_SUCCEEDED
    assert [
        event["event_type"]
        for event in comparison_store.list_detection_events(
            attempts[0]["attempt_id"], db_path=fixture_db
        )
    ] == ["detection_started", "detection_succeeded"]


def test_computation_helper_performs_no_lifecycle_persistence(corpus, db):
    """The pure/orchestration split is real: _compute_result touches no
    lifecycle storage, so reaching it always implies a durable attempt."""
    comparison_id = _comparison(corpus, db)
    record = comparison_store.get_comparison(comparison_id, db_path=db)
    previous_ref, current_ref = comparison_store.validate_pair(
        PREV_ID, CURR_ID, corpus.registry
    )
    wire = comparison_detector._compute_result(
        record,
        previous_ref,
        current_ref,
        filing_registry.get_filing(PREV_ID, corpus.registry),
        filing_registry.get_filing(CURR_ID, corpus.registry),
        corpus.chroma,
    )
    assert wire["comparison_id"] == comparison_id
    # Nothing was persisted: no attempt, no event, no result, no transition.
    assert comparison_store.list_detection_attempts(comparison_id, db_path=db) == []
    assert comparison_store.get_result(comparison_id, db_path=db) is None
    assert _status(db, comparison_id) == STATUS_READY_FOR_DETECTION


# --- Concurrency (tests 7-8) --------------------------------------------------


def test_concurrent_detection_starts_exactly_one_running_attempt(corpus, tmp_path):
    """Tests 7-8: one execution runs; every loser gets detection_in_progress or
    the already-completed idempotent response."""
    db = tmp_path / "concurrent.db"
    comparison_id = _comparison(corpus, db)

    def attempt(_n):
        try:
            return _detect(corpus, db, comparison_id)
        except DetectionInProgress as exc:
            return exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))

    refused = [o for o in outcomes if isinstance(o, DetectionInProgress)]
    ran = [o for o in outcomes if not isinstance(o, Exception)]
    assert len(refused) + len(ran) == 8
    assert all(exc.code == "detection_in_progress" for exc in refused)
    assert sum(1 for _r, created, _a in ran if created) == 1

    attempts = comparison_store.list_detection_attempts(comparison_id, db_path=db)
    assert len(attempts) == 1
    assert attempts[0]["status"] == ATTEMPT_SUCCEEDED
    with closing(sqlite3.connect(str(db))) as conn:
        assert conn.execute("SELECT COUNT(*) FROM comparison_results").fetchone()[0] == 1


def test_concurrent_start_attempts_serialize_at_the_store(corpus, tmp_path):
    """The store itself admits exactly one running attempt, independent of the
    detector: eight racing starts, one winner."""
    db = tmp_path / "start-race.db"
    comparison_id = _comparison(corpus, db)

    def start(_n):
        try:
            return _start(corpus, db, comparison_id)
        except DetectionStateError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(start, range(8)))

    started = [o for o in outcomes if isinstance(o, dict)]
    refused = [o for o in outcomes if isinstance(o, DetectionStateError)]
    assert len(started) == 1
    assert all(exc.code == "detection_in_progress" for exc in refused)
    assert _status(db, comparison_id) == STATUS_DETECTING
    assert (
        comparison_store.get_running_detection_attempt(comparison_id, db_path=db)[
            "attempt_id"
        ]
        == started[0]["attempt_id"]
    )


def test_start_losing_to_a_completed_run_returns_the_stored_result(corpus, db):
    """A request that decides to start, but whose start transaction finds the
    comparison already 'detected' because a concurrent execution finished in
    between, gets the winner's result under the existing idempotency contract —
    not a spurious lifecycle 409.

    Simulated deterministically: the start transaction is made to observe a
    comparison that a concurrent request has already completed.
    """
    comparison_id = _comparison(corpus, db)
    # The winning concurrent request: completes normally.
    winning_result, created_first, winning_attempt = _detect(corpus, db, comparison_id)
    assert created_first is True

    # The losing request: its PRE-START read of the stored result happened
    # before the winner committed, so make exactly that first read return None.
    # Every later read (including the post-race recheck) sees the truth.
    real_stored_outcome = comparison_detector._stored_outcome
    calls = {"n": 0}

    def stale_first_read(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_stored_outcome(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(comparison_detector, "_stored_outcome", stale_first_read)
        result, created, attempt_id = _detect(corpus, db, comparison_id)

    assert calls["n"] == 2  # stale pre-check, then the post-race recheck
    assert created is False
    assert result == winning_result
    assert attempt_id == winning_attempt
    stored = comparison_store.get_result(comparison_id, db_path=db)
    assert result == stored["result"]
    attempts = comparison_store.list_detection_attempts(comparison_id, db_path=db)
    assert len(attempts) == 1
    assert attempts[0]["status"] == ATTEMPT_SUCCEEDED
    assert attempt_id == attempts[0]["attempt_id"]
    assert _status(db, comparison_id) == STATUS_DETECTED


# --- Failure paths (tests 9-12) ----------------------------------------------


def test_unexpected_detector_failure_marks_attempt_failed_safely(
    corpus, db, monkeypatch
):
    """Tests 10-12: an unexpected fault finalizes the attempt as failed with a
    stable code and a safe summary, emits started+failed events, and leaks no
    exception text anywhere."""
    comparison_id = _comparison(corpus, db)
    secret = "kaboom /absolute/secret/path SELECT * FROM comparisons"

    def boom(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(comparison_detector, "detect_changes", boom)
    with pytest.raises(DetectionInternalError) as excinfo:
        _detect(corpus, db, comparison_id)
    assert secret not in str(excinfo.value)

    attempts = comparison_store.list_detection_attempts(comparison_id, db_path=db)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["status"] == ATTEMPT_FAILED
    assert attempt["failure_code"] == "detector_internal_error"
    assert attempt["finished_at"] is not None
    assert attempt["result_hash"] is None
    for forbidden in ("/absolute", "SELECT", "kaboom", "Traceback"):
        assert forbidden not in attempt["failure_summary"]
    assert _event_types(db, attempt["attempt_id"]) == [
        "detection_started",
        "detection_failed",
    ]
    events = comparison_store.list_detection_events(
        attempt["attempt_id"], db_path=db
    )
    assert events[1]["failure_code"] == "detector_internal_error"
    assert events[1]["result_hash"] is None
    assert _status(db, comparison_id) == STATUS_FAILED
    assert comparison_store.get_result(comparison_id, db_path=db) is None


@pytest.mark.parametrize(
    "raised, expected_code",
    [
        # Issue-2 test 1: a KNOWN domain condition keeps its own stable code
        # rather than being collapsed into detector_internal_error.
        (
            comparison_detector.DetectionNotReady(
                "comparison_not_ready", "not ready"
            ),
            "comparison_not_ready",
        ),
        (
            comparison_detector.DetectionInputsStale(
                "comparison_inputs_stale", "sources moved"
            ),
            "comparison_inputs_stale",
        ),
        (
            comparison_detector.DetectionVersionSuperseded(
                "detector_version_superseded", "older version"
            ),
            "detector_version_superseded",
        ),
        (
            comparison_detector.DetectionError(
                "section_unit_parse_failed", "units unparseable"
            ),
            "section_unit_parse_failed",
        ),
        (
            comparison_detector.DetectionInternalError(
                "detector_internal_error", "deterministic failure"
            ),
            "detector_internal_error",
        ),
        # An unrecognized code is not trusted into storage.
        (
            comparison_detector.DetectionError(
                "made_up_code_/tmp/secret", "unknown"
            ),
            "detector_internal_error",
        ),
    ],
)
def test_known_post_start_domain_code_is_preserved(
    corpus, tmp_path, raised, expected_code
):
    """Issue-2 tests 1-3: the persisted failure_code is the domain code for a
    known condition, detector_internal_error for anything unrecognized, and the
    summary is always code-derived."""
    db = tmp_path / f"{expected_code}.db"
    comparison_id = _comparison(corpus, db)

    def boom(*args, **kwargs):
        raise raised

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(comparison_detector, "detect_changes", boom)
        with pytest.raises(comparison_detector.DetectionError):
            _detect(corpus, db, comparison_id)

    attempt = comparison_store.list_detection_attempts(comparison_id, db_path=db)[0]
    assert attempt["status"] == ATTEMPT_FAILED
    assert attempt["failure_code"] == expected_code
    assert attempt["finished_at"] is not None
    assert attempt["result_hash"] is None
    # The summary comes from the code allowlist, never from the exception.
    assert attempt["failure_summary"] == comparison_detector._safe_failure_summary(
        expected_code
    )
    for forbidden in ("/tmp", "secret", "Traceback", "SELECT"):
        assert forbidden not in attempt["failure_summary"]
    assert _event_types(db, attempt["attempt_id"]) == [
        "detection_started",
        "detection_failed",
    ]
    events = comparison_store.list_detection_events(
        attempt["attempt_id"], db_path=db
    )
    assert events[1]["failure_code"] == expected_code
    assert _status(db, comparison_id) == STATUS_FAILED
    # The comparison's own failure fields agree with the attempt's.
    row = comparison_store.get_comparison(comparison_id, db_path=db)
    assert row["failure_code"] == expected_code
    assert row["failure_summary"] == attempt["failure_summary"]


def test_every_domain_failure_code_has_an_allowlisted_summary():
    """Issue-2 test 3: no domain code falls back to an ad-hoc string, and no
    summary exceeds the store's bound."""
    for code in comparison_detector.DOMAIN_FAILURE_CODES:
        summary = comparison_detector._safe_failure_summary(code)
        assert code in comparison_detector._FAILURE_SUMMARIES, code
        assert summary and len(summary) <= comparison_store.MAX_FAILURE_SUMMARY_CHARS
        assert "/" not in summary and "SELECT" not in summary
    # An unknown code still yields a bounded, path-free string.
    fallback = comparison_detector._safe_failure_summary("x" * 500)
    assert len(fallback) <= comparison_store.MAX_FAILURE_SUMMARY_CHARS


def test_rejected_request_creates_no_attempt(corpus, db):
    """F.4: requests refused BEFORE an attempt starts are not failed
    executions — terminal lifecycle, stale inputs, and superseded versions all
    leave the attempt table untouched."""
    comparison_id = _comparison(corpus, db)
    _detect(corpus, db, comparison_id)  # -> detected, attempt 1
    assert len(comparison_store.list_detection_attempts(comparison_id, db_path=db)) == 1

    # Superseded version: a 409 that must not manufacture an attempt.
    _sql(
        db,
        "UPDATE comparison_results SET detector_version = 'item1a_detector.v1' "
        "WHERE comparison_id = ?",
        (comparison_id,),
    )
    with pytest.raises(comparison_detector.DetectionVersionSuperseded):
        _detect(corpus, db, comparison_id)
    assert len(comparison_store.list_detection_attempts(comparison_id, db_path=db)) == 1

    # A terminal (failed) comparison refuses without creating an attempt.
    _sql(
        db,
        "UPDATE comparisons SET status = 'failed' WHERE comparison_id = ?",
        (comparison_id,),
    )
    _sql(db, "DELETE FROM comparison_results WHERE comparison_id = ?", (comparison_id,))
    with pytest.raises(DetectionNotReady):
        _detect(corpus, db, comparison_id)
    assert len(comparison_store.list_detection_attempts(comparison_id, db_path=db)) == 1


# --- Process-interruption boundary (tests 13-14) ------------------------------


def test_interrupted_detection_leaves_detecting_and_running_visible(corpus, db):
    """Test 13: a start with no terminal completion — exactly the state a
    process kill leaves — is durable and observable."""
    comparison_id = _comparison(corpus, db)
    attempt = _start(corpus, db, comparison_id)

    assert _status(db, comparison_id) == STATUS_DETECTING
    stored = comparison_store.get_detection_attempt(attempt["attempt_id"], db_path=db)
    assert stored["status"] == ATTEMPT_RUNNING
    assert stored["finished_at"] is None
    assert stored["result_hash"] is None
    assert stored["failure_code"] is None and stored["failure_summary"] is None
    assert _event_types(db, attempt["attempt_id"]) == ["detection_started"]
    assert comparison_store.get_result(comparison_id, db_path=db) is None


def test_later_request_does_not_auto_recover_interrupted_state(corpus, db):
    """Test 14: no age-based takeover, no replacement attempt — every later
    request keeps reporting detection_in_progress."""
    comparison_id = _comparison(corpus, db)
    attempt = _start(corpus, db, comparison_id)

    for _ in range(3):
        with pytest.raises(DetectionInProgress) as excinfo:
            _detect(corpus, db, comparison_id)
        assert excinfo.value.code == "detection_in_progress"

    assert _status(db, comparison_id) == STATUS_DETECTING
    still = comparison_store.get_detection_attempt(attempt["attempt_id"], db_path=db)
    assert still["status"] == ATTEMPT_RUNNING
    assert still["finished_at"] is None
    assert len(comparison_store.list_detection_attempts(comparison_id, db_path=db)) == 1
    assert _event_types(db, attempt["attempt_id"]) == ["detection_started"]
    # No terminal event ever appears on its own.
    assert comparison_store.get_running_detection_attempt(
        comparison_id, db_path=db
    )["attempt_id"] == attempt["attempt_id"]


# --- Invalid transitions (tests 15-20) ---------------------------------------


def test_second_start_while_running_is_rejected(corpus, db):
    """Test 15: running -> running is refused."""
    comparison_id = _comparison(corpus, db)
    _start(corpus, db, comparison_id)
    with pytest.raises(DetectionStateError) as excinfo:
        _start(corpus, db, comparison_id)
    assert excinfo.value.code == "detection_in_progress"
    assert len(comparison_store.list_detection_attempts(comparison_id, db_path=db)) == 1


def test_terminal_attempt_is_never_refinalized(corpus, db, monkeypatch):
    """Tests 16-17: succeeded -> failed and failed -> succeeded are refused."""
    comparison_id = _comparison(corpus, db)
    _result, _created, succeeded_id = _detect(corpus, db, comparison_id)

    with pytest.raises(DetectionStateError) as excinfo:
        comparison_store.fail_detection_attempt(
            succeeded_id, failure_code="x", failure_summary="y", db_path=db
        )
    assert excinfo.value.code == "detection_attempt_not_running"
    assert (
        comparison_store.get_detection_attempt(succeeded_id, db_path=db)["status"]
        == ATTEMPT_SUCCEEDED
    )

    # Now a failed attempt on a second comparison.
    other = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry,
        section_scope=["item_1a_risk_factors"],
    )[0]["comparison_id"]
    assert other == comparison_id  # deterministic id: same logical comparison

    fresh_db = db.parent / "second.db"
    second_id = _comparison(corpus, fresh_db)
    monkeypatch.setattr(
        comparison_detector, "detect_changes", lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
    )
    with pytest.raises(DetectionInternalError):
        _detect(corpus, fresh_db, second_id)
    monkeypatch.undo()
    failed_id = comparison_store.list_detection_attempts(
        second_id, db_path=fresh_db
    )[0]["attempt_id"]

    with pytest.raises(DetectionStateError) as excinfo:
        comparison_store.complete_detection_attempt(
            failed_id,
            result_json="{}",
            result_hash="deadbeef",
            detector_version=DETECTOR_VERSION,
            workflow_version=WORKFLOW_VERSION,
            previous_source_hash="a",
            current_source_hash="b",
            db_path=fresh_db,
        )
    assert excinfo.value.code == "detection_attempt_not_running"
    assert (
        comparison_store.get_detection_attempt(failed_id, db_path=fresh_db)["status"]
        == ATTEMPT_FAILED
    )


def test_completing_unknown_attempt_is_rejected(corpus, db):
    """Test 18."""
    _comparison(corpus, db)
    with pytest.raises(DetectionStateError) as excinfo:
        comparison_store.complete_detection_attempt(
            "att_does_not_exist",
            result_json="{}",
            result_hash="h",
            detector_version=DETECTOR_VERSION,
            workflow_version=WORKFLOW_VERSION,
            previous_source_hash="a",
            current_source_hash="b",
            db_path=db,
        )
    assert excinfo.value.code == "detection_attempt_not_found"

    with pytest.raises(DetectionStateError) as excinfo:
        comparison_store.fail_detection_attempt(
            "att_does_not_exist", failure_code="c", failure_summary="s", db_path=db
        )
    assert excinfo.value.code == "detection_attempt_not_found"


def test_completing_with_changed_inputs_is_rejected(corpus, db):
    """Test 19: version or source-hash drift between start and completion
    refuses, and nothing is persisted."""
    comparison_id = _comparison(corpus, db)
    attempt = _start(corpus, db, comparison_id)
    previous_hash, current_hash = _hashes(corpus)

    for overrides in (
        {"previous_source_hash": "0" * 64},
        {"current_source_hash": "0" * 64},
        {"detector_version": "item1a_detector.v1"},
        {"workflow_version": "comparison_workflow.v1"},
    ):
        payload = dict(
            result_json="{}",
            result_hash="h",
            detector_version=DETECTOR_VERSION,
            workflow_version=WORKFLOW_VERSION,
            previous_source_hash=previous_hash,
            current_source_hash=current_hash,
        )
        payload.update(overrides)
        with pytest.raises(DetectionStateError) as excinfo:
            comparison_store.complete_detection_attempt(
                attempt["attempt_id"], db_path=db, **payload
            )
        assert excinfo.value.code == "detection_inputs_changed", overrides

    assert comparison_store.get_result(comparison_id, db_path=db) is None
    assert (
        comparison_store.get_detection_attempt(attempt["attempt_id"], db_path=db)[
            "status"
        ]
        == ATTEMPT_RUNNING
    )
    assert _status(db, comparison_id) == STATUS_DETECTING


def test_completing_when_comparison_left_detecting_state_is_rejected(corpus, db):
    """Test 20 (companion): a result is never committed outside its own
    attempt's detecting window, so the stored result hash can never disagree
    with the attempt that claims it."""
    comparison_id = _comparison(corpus, db)
    attempt = _start(corpus, db, comparison_id)
    previous_hash, current_hash = _hashes(corpus)
    _sql(
        db,
        "UPDATE comparisons SET status = 'detected' WHERE comparison_id = ?",
        (comparison_id,),
    )
    with pytest.raises(DetectionStateError) as excinfo:
        comparison_store.complete_detection_attempt(
            attempt["attempt_id"],
            result_json="{}",
            result_hash="h",
            detector_version=DETECTOR_VERSION,
            workflow_version=WORKFLOW_VERSION,
            previous_source_hash=previous_hash,
            current_source_hash=current_hash,
            db_path=db,
        )
    assert excinfo.value.code == "detection_transition_invalid"
    assert comparison_store.get_result(comparison_id, db_path=db) is None


def test_attempt_field_coherence_is_a_storage_invariant(corpus, db):
    """Test 20: the attempt table itself refuses an incoherent row — a running
    attempt carrying a result hash, or a succeeded attempt without one."""
    comparison_id = _comparison(corpus, db)
    attempt = _start(corpus, db, comparison_id)
    for statement in (
        "UPDATE comparison_detection_attempts SET result_hash = 'h' "
        "WHERE attempt_id = ?",
        "UPDATE comparison_detection_attempts SET status = 'succeeded' "
        "WHERE attempt_id = ?",
        "UPDATE comparison_detection_attempts SET failure_code = 'c' "
        "WHERE attempt_id = ?",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _sql(db, statement, (attempt["attempt_id"],))
    # And a second running attempt for the same comparison is impossible.
    with pytest.raises(sqlite3.IntegrityError):
        _sql(
            db,
            "INSERT INTO comparison_detection_attempts (attempt_id, comparison_id,"
            " attempt_number, status, detector_version, workflow_version,"
            " previous_source_hash, current_source_hash, started_at)"
            " VALUES ('att_x', ?, 2, 'running', 'd', 'w', 'a', 'b', 't')",
            (comparison_id,),
        )


# --- Transaction rollback (tests 21-22) --------------------------------------


def test_failed_success_finalization_leaves_nothing_partial(corpus, db, monkeypatch):
    """Test 21: if the success transaction fails, there is no result, no
    terminal event, and no terminal attempt state."""
    comparison_id = _comparison(corpus, db)

    def boom_insert(*args, **kwargs):
        # Fails INSIDE complete_detection_attempt's transaction, after its
        # state checks passed, so the rollback path is what we observe — not a
        # pre-flight guard.
        raise sqlite3.OperationalError("simulated failure inside the transaction")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(comparison_store, "_insert_result", boom_insert)
        with pytest.raises(DetectionInternalError):
            _detect(corpus, db, comparison_id)

    # The detector treated the storage fault as an internal error and finalized
    # the attempt as failed — but no partial success survived.
    assert comparison_store.get_result(comparison_id, db_path=db) is None
    attempt = comparison_store.list_detection_attempts(comparison_id, db_path=db)[0]
    assert attempt["status"] == ATTEMPT_FAILED
    assert attempt["result_hash"] is None
    assert "detection_succeeded" not in _event_types(db, attempt["attempt_id"])
    with closing(sqlite3.connect(str(db))) as conn:
        assert conn.execute("SELECT COUNT(*) FROM comparison_results").fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_failed_failure_finalization_leaves_attempt_running(corpus, db, monkeypatch):
    """Test 22: if the FAILURE transaction cannot commit, the attempt stays
    running and the comparison stays detecting — the interruption boundary —
    and the original detector error still reaches the caller."""
    comparison_id = _comparison(corpus, db)

    def boom(*args, **kwargs):
        raise RuntimeError("detector exploded")

    def refuse_finalize(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(comparison_detector, "detect_changes", boom)
    monkeypatch.setattr(comparison_store, "fail_detection_attempt", refuse_finalize)
    with pytest.raises(DetectionInternalError):
        _detect(corpus, db, comparison_id)
    monkeypatch.undo()

    attempt = comparison_store.list_detection_attempts(comparison_id, db_path=db)[0]
    assert attempt["status"] == ATTEMPT_RUNNING
    assert attempt["finished_at"] is None
    assert attempt["failure_code"] is None
    assert _status(db, comparison_id) == STATUS_DETECTING
    assert _event_types(db, attempt["attempt_id"]) == ["detection_started"]


# --- Migration and persistence (tests 23-25) ---------------------------------


def test_migration_preserves_old_rows_results_and_children(tmp_path):
    """Tests 23-24: a database created before 'detecting' migrates in place,
    keeps its comparisons and results (and its child tables' foreign keys),
    accepts the new state afterwards, and migrates only once."""
    db = tmp_path / "old.db"
    old_comparisons = """
    CREATE TABLE comparisons (
        comparison_id       TEXT PRIMARY KEY,
        schema_version      TEXT NOT NULL,
        workflow_version    TEXT NOT NULL,
        previous_filing_id  TEXT NOT NULL,
        current_filing_id   TEXT NOT NULL,
        section_scope       TEXT NOT NULL,
        status              TEXT NOT NULL
                            CHECK (status IN ('ready_for_detection', 'detected',
                                              'failed')),
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        failure_code        TEXT,
        failure_summary     TEXT
    )
    """
    old_results = """
    CREATE TABLE comparison_results (
        comparison_id        TEXT PRIMARY KEY
                             REFERENCES comparisons (comparison_id),
        schema_version       TEXT NOT NULL,
        detector_version     TEXT NOT NULL,
        previous_source_hash TEXT NOT NULL,
        current_source_hash  TEXT NOT NULL,
        result_json          TEXT NOT NULL,
        result_hash          TEXT NOT NULL,
        created_at           TEXT NOT NULL
    )
    """
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute(old_comparisons)
        conn.execute(old_results)
        conn.execute(
            "INSERT INTO comparisons VALUES ('cmp_done', 'comparison.v1', "
            "'comparison_workflow.v2', 'p', 'c', '[\"item_1a_risk_factors\"]', "
            "'detected', 't0', 't0', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO comparisons VALUES ('cmp_bad', 'comparison.v1', "
            "'comparison_workflow.v2', 'p2', 'c2', '[\"item_1a_risk_factors\"]', "
            "'failed', 't0', 't0', 'detector_internal_error', 'safe summary')"
        )
        conn.execute(
            "INSERT INTO comparison_results VALUES ('cmp_done', 'comparison.v1', "
            "'item1a_detector.v2', 'h1', 'h2', '{\"a\": 1}', 'rh', 't0')"
        )

    for _ in range(3):  # idempotent
        init_db(db)

    with closing(sqlite3.connect(str(db))) as conn:
        conn.row_factory = sqlite3.Row
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='comparisons'"
        ).fetchone()[0]
        assert "'detecting'" in ddl and "'detected'" in ddl
        rows = {
            row["comparison_id"]: dict(row)
            for row in conn.execute("SELECT * FROM comparisons")
        }
        assert set(rows) == {"cmp_done", "cmp_bad"}
        assert rows["cmp_done"]["status"] == "detected"
        # An existing failed comparison is unchanged, code and summary intact.
        assert rows["cmp_bad"]["status"] == "failed"
        assert rows["cmp_bad"]["failure_code"] == "detector_internal_error"
        assert rows["cmp_bad"]["failure_summary"] == "safe summary"
        # The old result remains readable.
        result = conn.execute("SELECT * FROM comparison_results").fetchone()
        assert result["comparison_id"] == "cmp_done"
        assert json.loads(result["result_json"]) == {"a": 1}
        # The child table's foreign key still points at 'comparisons'.
        child_ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='comparison_results'"
        ).fetchone()[0]
        assert "REFERENCES comparisons" in child_ddl
        assert "comparisons_rebuilt" not in child_ddl
        with conn:
            conn.execute(
                "UPDATE comparisons SET status='detecting' "
                "WHERE comparison_id='cmp_bad'"
            )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    assert comparison_store.get_result("cmp_done", db_path=db)["result"] == {"a": 1}


def test_attempts_and_events_survive_reopen(corpus, db):
    """Test 25: attempts and events are durable across fresh connections."""
    comparison_id = _comparison(corpus, db)
    _result, _created, attempt_id = _detect(corpus, db, comparison_id)

    reopened = comparison_store.get_detection_attempt(attempt_id, db_path=db)
    assert reopened["status"] == ATTEMPT_SUCCEEDED
    assert comparison_store.list_detection_attempts(comparison_id, db_path=db) == [
        reopened
    ]
    assert _event_types(db, attempt_id) == [
        "detection_started",
        "detection_succeeded",
    ]


def test_sqlite_integrity_and_foreign_keys_hold(corpus, db, monkeypatch):
    """Test 28: after successful, failed, and interrupted attempts."""
    succeeded_id = _comparison(corpus, db)
    _detect(corpus, db, succeeded_id)

    failed_db = db.parent / "failed.db"
    failed_id = _comparison(corpus, failed_db)
    monkeypatch.setattr(
        comparison_detector,
        "detect_changes",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(DetectionInternalError):
        _detect(corpus, failed_db, failed_id)
    monkeypatch.undo()

    running_db = db.parent / "running.db"
    running_id = _comparison(corpus, running_db)
    _start(corpus, running_db, running_id)

    for path in (db, failed_db, running_db):
        with closing(sqlite3.connect(str(path))) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --- API surface (tests 26-27) -----------------------------------------------


@pytest.fixture
def api_env(corpus, tmp_path, monkeypatch):
    db = tmp_path / "api.db"
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(db))
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(corpus.registry))
    monkeypatch.setattr(
        comparison_detector, "open_index", lambda: corpus.chroma
    )
    return SimpleNamespace(db=db)


def test_detect_route_exposes_attempt_id_without_touching_the_result(api_env):
    """The additive attemptId rides beside the unchanged result contract, and
    the comparison.v1 document itself gains no attempt metadata."""
    created = client.post(
        "/api/comparisons",
        json={"previousFilingId": PREV_ID, "currentFilingId": CURR_ID},
    )
    assert created.status_code == 201
    comparison_id = created.json()["comparison"]["comparisonId"]

    detected = client.post(f"/api/comparisons/{comparison_id}/detect")
    assert detected.status_code == 201
    body = detected.json()
    assert set(body) == {"created", "result", "attemptId"}
    assert body["created"] is True
    assert body["attemptId"].startswith("att_")
    # comparison.v1 is untouched: no attempt fields leaked into the schema.
    for key in ("attempt_id", "attemptId", "attempt_number", "status"):
        assert key not in body["result"]

    replay = client.post(f"/api/comparisons/{comparison_id}/detect")
    assert replay.status_code == 200
    assert replay.json()["created"] is False
    assert replay.json()["attemptId"] == body["attemptId"]
    assert replay.json()["result"] == body["result"]

    listed = client.get("/api/comparisons", params={"status": "detected"})
    assert listed.status_code == 200
    assert comparison_id in {row["comparisonId"] for row in listed.json()}
    return comparison_id


def test_attempt_routes_expose_only_the_allowlist(api_env):
    """Test 26: DTOs carry no paths, SQL, evidence, or result payload."""
    comparison_id = client.post(
        "/api/comparisons",
        json={"previousFilingId": PREV_ID, "currentFilingId": CURR_ID},
    ).json()["comparison"]["comparisonId"]
    attempt_id = client.post(
        f"/api/comparisons/{comparison_id}/detect"
    ).json()["attemptId"]

    listing = client.get(f"/api/comparisons/{comparison_id}/detection-attempts")
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert set(rows[0]) == {
        "attemptId", "comparisonId", "attemptNumber", "status",
        "detectorVersion", "workflowVersion", "previousSourceHash",
        "currentSourceHash", "startedAt", "finishedAt", "resultHash",
        "failureCode", "failureSummary",
    }
    assert rows[0]["status"] == "succeeded"

    single = client.get(f"/api/detection-attempts/{attempt_id}")
    assert single.status_code == 200
    assert single.json() == rows[0]

    events = client.get(f"/api/detection-attempts/{attempt_id}/events")
    assert events.status_code == 200
    assert [event["eventType"] for event in events.json()] == [
        "detection_started",
        "detection_succeeded",
    ]
    assert set(events.json()[0]) == {
        "eventId", "attemptId", "comparisonId", "eventType", "eventSeq",
        "createdAt", "resultHash", "failureCode",
    }

    # Nothing sensitive in any attempt-surface response.
    combined = listing.text + single.text + events.text
    for forbidden in (
        "/Users", "/private", "SELECT", "INSERT", "result_json", "excerpt",
        "Risk Factors", "chunk_id", "reviewer",
    ):
        assert forbidden not in combined, forbidden

    assert client.get("/api/detection-attempts/att_nope").status_code == 404
    assert client.get("/api/detection-attempts/att_nope/events").status_code == 404
    assert (
        client.get("/api/comparisons/cmp_nope/detection-attempts").status_code == 404
    )


def test_attempt_list_order_is_deterministic(api_env, corpus):
    """Test 27: attempts list by ascending attempt_number, events by sequence."""
    comparison_id = client.post(
        "/api/comparisons",
        json={"previousFilingId": PREV_ID, "currentFilingId": CURR_ID},
    ).json()["comparison"]["comparisonId"]

    # A failed attempt 1, then two more attempts inserted directly to prove
    # ordering with more than one row. A scoped MonkeyPatch context is used so
    # undoing the detector patch cannot also revert api_env's config patches.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            comparison_detector,
            "detect_changes",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        failing = client.post(f"/api/comparisons/{comparison_id}/detect")
    assert failing.status_code == 500
    assert failing.json()["detail"]["code"] == "detector_internal_error"
    assert failing.json()["detail"]["error_id"].startswith("err_")

    previous_hash, current_hash = _hashes(corpus)
    for number in (2, 3):
        _sql(
            api_env.db,
            "INSERT INTO comparison_detection_attempts (attempt_id, comparison_id,"
            " attempt_number, status, detector_version, workflow_version,"
            " previous_source_hash, current_source_hash, started_at, finished_at,"
            " failure_code, failure_summary)"
            " VALUES (?, ?, ?, 'failed', ?, ?, ?, ?, 't0', 't1', 'c', 's')",
            (
                f"att_manual{number}", comparison_id, number,
                DETECTOR_VERSION, WORKFLOW_VERSION, previous_hash, current_hash,
            ),
        )

    rows = client.get(
        f"/api/comparisons/{comparison_id}/detection-attempts"
    ).json()
    assert [row["attemptNumber"] for row in rows] == [1, 2, 3]
    for _ in range(3):  # stable across repeated reads
        assert [
            row["attemptId"]
            for row in client.get(
                f"/api/comparisons/{comparison_id}/detection-attempts"
            ).json()
        ] == [row["attemptId"] for row in rows]


def test_detection_in_progress_is_a_stable_409(api_env, corpus):
    """The interruption boundary through the API."""
    comparison_id = client.post(
        "/api/comparisons",
        json={"previousFilingId": PREV_ID, "currentFilingId": CURR_ID},
    ).json()["comparison"]["comparisonId"]
    _start(corpus, api_env.db, comparison_id)

    for _ in range(2):
        response = client.post(f"/api/comparisons/{comparison_id}/detect")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "detection_in_progress"

    record = client.get(f"/api/comparisons/{comparison_id}")
    assert record.json()["status"] == "detecting"
    filtered = client.get("/api/comparisons", params={"status": "detecting"})
    assert comparison_id in {row["comparisonId"] for row in filtered.json()}


def test_api_preserves_the_409_422_500_distinction(api_env, corpus):
    """Issue-2 test 4: known domain conditions stay 409 (or 422 for an invalid
    pair) and only unexpected faults become a safe 500 with a correlation id."""
    comparison_id = client.post(
        "/api/comparisons",
        json={"previousFilingId": PREV_ID, "currentFilingId": CURR_ID},
    ).json()["comparison"]["comparisonId"]

    # 409: a running attempt (the interruption boundary).
    _start(corpus, api_env.db, comparison_id)
    conflict = client.post(f"/api/comparisons/{comparison_id}/detect")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "detection_in_progress"

    # 500: an unexpected fault, safe body with a correlation id and no raw text.
    # Retire the running attempt and reset the comparison so the next request
    # gets past the start guard and reaches the detector.
    _sql(
        api_env.db,
        "UPDATE comparison_detection_attempts SET status='failed', "
        "finished_at='t', failure_code='c', failure_summary='s' "
        "WHERE comparison_id = ?",
        (comparison_id,),
    )
    _sql(
        api_env.db,
        "UPDATE comparisons SET status='ready_for_detection' WHERE comparison_id = ?",
        (comparison_id,),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            comparison_detector,
            "detect_changes",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("boom /abs/secret SELECT * FROM comparisons")
            ),
        )
        internal = client.post(f"/api/comparisons/{comparison_id}/detect")
    assert internal.status_code == 500
    detail = internal.json()["detail"]
    assert detail["code"] == "detector_internal_error"
    assert detail["error_id"].startswith("err_")
    for forbidden in ("/abs/secret", "SELECT", "boom", "Traceback"):
        assert forbidden not in internal.text

    # 409 again: the comparison is now terminal (failed), not in progress.
    terminal = client.post(f"/api/comparisons/{comparison_id}/detect")
    assert terminal.status_code == 409
    assert terminal.json()["detail"]["code"] == "comparison_not_ready"

    # 422: an ineligible pair is a validation error, never a failed attempt.
    invalid = client.post(
        "/api/comparisons",
        json={"previousFilingId": PREV_ID, "currentFilingId": "nope:10-k:2030-12-31"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_comparison_pair"


# --- Boundary: nothing added beyond durable attempts (test 30) ----------------


def test_no_automatic_retry_scheduler_or_worker_was_introduced():
    """Test 30: no AUTOMATIC recovery machinery exists in any module.

    Operator-controlled replay landed in the following commit, so the guard is
    no longer "no replay code at all" — it is the invariant that actually
    matters: nothing recovers on its own. There is no timer, scheduler, worker,
    queue, backoff, or retry counter, and nothing infers termination from file
    mtime. Replay is reachable only through an explicit operator request, which
    the accompanying recovery tests pin end to end.
    """
    import ast

    root = Path(__file__).resolve().parent.parent
    forbidden = {
        "sleep", "Timer", "timer", "create_task", "run_forever", "getmtime",
        "st_mtime", "crontab", "apscheduler", "celery", "sched",
        "max_retries", "retry_count", "retries", "backoff", "dead_letter",
        "dlq", "worker_thread", "background_task", "BackgroundTasks",
    }
    for name in (
        "comparison_store.py",
        "comparison_detector.py",
        "detection_recovery.py",
        "api.py",
    ):
        # AST-based: a raw substring scan cannot distinguish a scheduler from a
        # docstring promising there is no scheduler.
        identifiers: set[str] = set()
        for node in ast.walk(
            ast.parse((root / name).read_text(encoding="utf-8"))
        ):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.keyword) and node.arg:
                identifiers.add(node.arg)
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg)
            elif isinstance(node, ast.Import):
                identifiers.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                identifiers.add(node.module.split(".")[0])
        found = identifiers & forbidden
        assert found == set(), f"{name}: {sorted(found)}"
