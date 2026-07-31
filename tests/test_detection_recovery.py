"""Tests for bounded stale-detection recognition and operator replay.

Builds the controlled corpus once into a temporary Chroma index (fake
embeddings — the detector reads by metadata and never embeds) plus a temporary
filing registry, then exercises the recovery policy, the pure staleness helper,
the atomic replay transaction, idempotency and concurrency, the attempt limit,
input/version drift, rollback, migration, and the read-only API surface.

Time is ALWAYS injected: there is not one sleep-based test in this file.
Entirely offline — no AWS credentials, no Bedrock, no network.
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings

import api
import comparison_detector
import comparison_reliability
import comparison_store
import config
import detection_job_lease
import detection_recovery
import filing_registry
import ingest
from comparison_store import (
    ATTEMPT_FAILED,
    ATTEMPT_RUNNING,
    ATTEMPT_SUCCEEDED,
    ATTEMPT_TIMED_OUT,
    DetectionStateError,
    STATUS_DETECTED,
    STATUS_DETECTING,
    STATUS_FAILED,
    WORKFLOW_VERSION,
    init_db,
)
from comparison_detector import DETECTOR_VERSION
from governance.policy_validation import GovernancePolicyConfigError
from tests.auth_helpers import DEFAULT_TEST_SUBJECT, authorization_headers

client = TestClient(api.app, headers=authorization_headers())

PREV_ID = "acme-corporation:10-k:2024-12-31"
CURR_ID = "acme-corporation:10-k:2025-12-31"

T0 = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
STALE = T0 + timedelta(seconds=900)          # exactly the boundary
WELL_STALE = T0 + timedelta(seconds=3600)
OPERATOR = "ops.engineer@example.com"
NOTE = "Process was killed during a deploy; restarting detection."
REASON = "operator_replay_stale_attempt"


class _FakeEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[float(len(t) % 7), 1.0] for t in texts]

    def embed_query(self, text):
        raise AssertionError("vector retrieval must not be used by the detector")


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    from langchain_chroma import Chroma

    td = tmp_path_factory.mktemp("recovery-corpus")
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
        collection_name="recoveryidx",
        persist_directory=str(td / "chroma"),
        embedding_function=_FakeEmbeddings(),
    )
    chroma.add_documents(documents=unique, ids=ids)
    return SimpleNamespace(registry=registry, chroma=chroma)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "comparisons.db"


def _hashes(corpus):
    return (
        filing_registry.get_filing(PREV_ID, corpus.registry)["source_hash"],
        filing_registry.get_filing(CURR_ID, corpus.registry)["source_hash"],
    )


def _interrupted(corpus, db, *, started_at=None):
    """A comparison left exactly as a killed process would leave it: detecting,
    with one running attempt and only a detection_started event."""
    record, _ = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry
    )
    comparison_id = record["comparison_id"]
    previous_hash, current_hash = _hashes(corpus)
    attempt = comparison_store.start_detection_attempt(
        comparison_id,
        detector_version=DETECTOR_VERSION,
        workflow_version=WORKFLOW_VERSION,
        previous_source_hash=previous_hash,
        current_source_hash=current_hash,
        db_path=db,
    )
    # Backdate started_at so staleness is controlled by data, never by sleeping.
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET started_at = ? WHERE attempt_id = ?",
        ((started_at or T0).isoformat(), attempt["attempt_id"]),
    )
    return comparison_id, attempt["attempt_id"]


def _job_interrupted(corpus, db, *, comparison_id_suffix="job"):
    """One active job-owned attempt with generation 1 and a finite lease."""
    record, _ = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry
    )
    comparison_id = record["comparison_id"]
    previous_hash, current_hash = _hashes(corpus)
    job = comparison_store.enqueue_detection_job(
        comparison_id,
        detector_version=DETECTOR_VERSION,
        workflow_version=WORKFLOW_VERSION,
        previous_filing_id=PREV_ID,
        current_filing_id=CURR_ID,
        previous_source_hash=previous_hash,
        current_source_hash=current_hash,
        requested_by_subject=f"{comparison_id_suffix}@example.local",
        requested_by_auth_method=comparison_store.ACTOR_AUTH_LOCAL_HS256,
        requested_by_token_id=f"jti-{comparison_id_suffix}",
        requested_by_policy_id="comparison_access_control_v1",
        requested_by_policy_version="1",
        db_path=db,
    )["job"]
    claimed = comparison_store.claim_detection_job(
        job_id=job["job_id"],
        worker_id="worker-one",
        detector_version=DETECTOR_VERSION,
        workflow_version=WORKFLOW_VERSION,
        previous_source_hash=previous_hash,
        current_source_hash=current_hash,
        lease_duration_seconds=(
            detection_job_lease.POLICY["lease_duration_seconds"]
        ),
        reclaim_grace_seconds=(
            detection_job_lease.POLICY["reclaim_grace_seconds"]
        ),
        max_claim_generations=(
            detection_job_lease.POLICY["max_claim_generations"]
        ),
        now=T0,
        db_path=db,
    )
    return comparison_id, claimed, previous_hash, current_hash


def _sql(db, statement, params=()):
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute(statement, params)


def _replay(corpus, db, attempt_id, *, now=WELL_STALE, operator=OPERATOR,
            note=NOTE, reason=REASON, policy=None):
    return detection_recovery.replay_attempt(
        attempt_id,
        operator_id=operator,
        reason_code=reason,
        operator_note=note,
        now=now,
        db_path=db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
        policy=policy,
    )


def _attempt(db, attempt_id):
    return comparison_store.get_detection_attempt(attempt_id, db_path=db)


def _status(db, comparison_id):
    return comparison_store.get_comparison(comparison_id, db_path=db)["status"]


def _events(db, attempt_id):
    return [
        event["event_type"]
        for event in comparison_store.list_detection_events(attempt_id, db_path=db)
    ]


def _snapshot(db):
    """Every row of every reliability table, for rollback assertions."""
    rows = {}
    with closing(sqlite3.connect(str(db))) as conn:
        for table in (
            "comparisons",
            "comparison_results",
            "comparison_detection_attempts",
            "comparison_detection_events",
            "comparison_detection_replays",
            "comparison_detection_jobs",
            "comparison_detection_job_events",
        ):
            rows[table] = [
                tuple(row)
                for row in conn.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"  # fixed names
                ).fetchall()
            ]
    return rows


# --- Policy (A) ---------------------------------------------------------------


def test_checked_in_policy_matches_the_baked_in_defaults():
    """The documented convention: the YAML and the code defaults agree."""
    loaded = detection_recovery.load_policy()
    assert loaded == {
        "policy_id": "detection_recovery_v1",
        "policy_version": "1",
        "stale_after_seconds": 900,
        "max_attempts_per_comparison": 3,
    }
    missing = detection_recovery.load_policy("/nonexistent/recovery.yaml")
    assert missing == loaded


@pytest.mark.parametrize(
    "document, fragment",
    [
        ("[]", "mapping"),
        ("staleness:\n  stale_after_seconds: 900\n", "all three sections"),
        # Non-empty id / version.
        ({"detection_recovery_policy": {"policy_id": "", "policy_version": "1"},
          "staleness": {"stale_after_seconds": 900},
          "attempt_limits": {"max_attempts_per_comparison": 3}}, "policy_id"),
        # Actual integer, and bool rejected as int.
        ({"detection_recovery_policy": {"policy_id": "x", "policy_version": "1"},
          "staleness": {"stale_after_seconds": "900"},
          "attempt_limits": {"max_attempts_per_comparison": 3}}, "must be an integer"),
        ({"detection_recovery_policy": {"policy_id": "x", "policy_version": "1"},
          "staleness": {"stale_after_seconds": True},
          "attempt_limits": {"max_attempts_per_comparison": 3}}, "must be an integer"),
        ({"detection_recovery_policy": {"policy_id": "x", "policy_version": "1"},
          "staleness": {"stale_after_seconds": 900},
          "attempt_limits": {"max_attempts_per_comparison": False}},
         "must be an integer"),
        # stale_after_seconds > 0.
        ({"detection_recovery_policy": {"policy_id": "x", "policy_version": "1"},
          "staleness": {"stale_after_seconds": 0},
          "attempt_limits": {"max_attempts_per_comparison": 3}}, "must be >= 1"),
        # max_attempts >= 1.
        ({"detection_recovery_policy": {"policy_id": "x", "policy_version": "1"},
          "staleness": {"stale_after_seconds": 900},
          "attempt_limits": {"max_attempts_per_comparison": 0}}, "must be >= 1"),
        # Explicit upper bounds.
        ({"detection_recovery_policy": {"policy_id": "x", "policy_version": "1"},
          "staleness": {"stale_after_seconds": 900_000},
          "attempt_limits": {"max_attempts_per_comparison": 3}}, "must be <= 86400"),
        ({"detection_recovery_policy": {"policy_id": "x", "policy_version": "1"},
          "staleness": {"stale_after_seconds": 900},
          "attempt_limits": {"max_attempts_per_comparison": 999}}, "must be <= 10"),
        # Required keys.
        ({"detection_recovery_policy": {"policy_id": "x", "policy_version": "1"},
          "staleness": {},
          "attempt_limits": {"max_attempts_per_comparison": 3}},
         "'staleness.stale_after_seconds' is required"),
        # Unknown keys rejected (a typo must not leave the default in effect).
        ({"detection_recovery_policy": {"policy_id": "x", "policy_version": "1"},
          "staleness": {"stale_after_seconds": 900, "stale_after_second": 5},
          "attempt_limits": {"max_attempts_per_comparison": 3}}, "unknown staleness"),
        ({"detection_recovery_policy": {"policy_id": "x", "policy_version": "1"},
          "staleness": {"stale_after_seconds": 900},
          "attempt_limits": {"max_attempts_per_comparison": 3, "extra": 1}},
         "unknown attempt_limits"),
    ],
)
def test_present_but_invalid_policy_fails_loudly(tmp_path, document, fragment):
    """A present-invalid file raises GovernancePolicyConfigError with a safe,
    field-specific message — never a silent permissive fallback."""
    path = tmp_path / "recovery.yaml"
    path.write_text(
        document if isinstance(document, str) else yaml.safe_dump(document),
        encoding="utf-8",
    )
    with pytest.raises(GovernancePolicyConfigError) as excinfo:
        detection_recovery.load_policy(path)
    assert fragment in str(excinfo.value)
    assert str(tmp_path) not in str(excinfo.value)  # no paths in the message


# --- Staleness helper (C) -----------------------------------------------------


@pytest.mark.parametrize(
    "age, expected",
    [(0, False), (1, False), (899, False), (900, True), (901, True), (10_000, True)],
)
def test_staleness_boundary_is_inclusive(age, expected):
    """Test 2: age >= threshold is stale; the boundary itself qualifies."""
    result = comparison_store.evaluate_staleness(
        T0.isoformat(), T0 + timedelta(seconds=age), 900
    )
    assert result["is_stale"] is expected
    assert result["age_seconds"] == age
    assert result["stale_at"] == (T0 + timedelta(seconds=900)).isoformat()


def test_negative_age_from_clock_skew_is_never_stale():
    """Test 3: a backwards clock must not authorize retiring a live attempt."""
    for skew in (-1, -900, -100_000):
        result = comparison_store.evaluate_staleness(
            T0.isoformat(), T0 + timedelta(seconds=skew), 900
        )
        assert result["age_seconds"] == skew
        assert result["is_stale"] is False


def test_naive_timestamps_are_rejected():
    """Test 4: a naive timestamp is never assumed to be UTC."""
    with pytest.raises(ValueError, match="timezone-aware"):
        comparison_store.evaluate_staleness("2026-07-29T12:00:00", T0, 900)
    with pytest.raises(ValueError, match="timezone-aware"):
        comparison_store.evaluate_staleness(T0.isoformat(), datetime(2026, 7, 29), 900)
    for bad in ("", "   ", None, 12345, "not-a-timestamp"):
        with pytest.raises(ValueError):
            comparison_store.evaluate_staleness(bad, T0, 900)
    # The '+00:00' and 'Z' spellings agree.
    assert comparison_store.evaluate_staleness(
        "2026-07-29T12:00:00Z", STALE, 900
    ) == comparison_store.evaluate_staleness(T0.isoformat(), STALE, 900)


def test_staleness_helper_has_no_storage_side_effects(corpus, db):
    """Test 5 of C: evaluating staleness writes nothing."""
    comparison_id, attempt_id = _interrupted(corpus, db)
    before = _snapshot(db)
    for _ in range(5):
        comparison_store.evaluate_staleness(
            _attempt(db, attempt_id)["started_at"], WELL_STALE, 900
        )
    assert _snapshot(db) == before


# --- Read-only recovery view (J1) --------------------------------------------


def test_recovery_view_before_threshold_is_not_stale_and_mutates_nothing(corpus, db):
    """Test 1: before the threshold the attempt is not stale, replay is blocked
    with the stable code, and repeated reads change nothing."""
    comparison_id, attempt_id = _interrupted(corpus, db)
    before = _snapshot(db)

    for _ in range(3):
        view = detection_recovery.recovery_view(
            attempt_id, now=T0 + timedelta(seconds=100), db_path=db,
            registry_path=corpus.registry,
        )
        assert view["status"] == ATTEMPT_RUNNING
        assert view["is_stale"] is False
        assert view["age_seconds"] == 100
        assert view["replay_eligible"] is False
        assert view["blocking_reason"] == "detection_attempt_not_stale"
        assert view["attempts_used"] == 1
        assert view["max_attempts"] == 3
        assert view["remaining_attempts"] == 2
        assert view["policy_id"] == "detection_recovery_v1"

    assert _snapshot(db) == before
    assert _attempt(db, attempt_id)["status"] == ATTEMPT_RUNNING
    assert _status(db, comparison_id) == STATUS_DETECTING


def test_recovery_view_after_threshold_reports_eligible_without_mutating(corpus, db):
    """Crossing the threshold changes eligibility only — never state."""
    comparison_id, attempt_id = _interrupted(corpus, db)
    before = _snapshot(db)
    view = detection_recovery.recovery_view(
        attempt_id, now=STALE, db_path=db, registry_path=corpus.registry
    )
    assert view["is_stale"] is True
    assert view["replay_eligible"] is True
    assert view["blocking_reason"] is None
    # Nothing was retired by merely looking.
    assert _snapshot(db) == before
    assert _attempt(db, attempt_id)["status"] == ATTEMPT_RUNNING
    assert _events(db, attempt_id) == ["detection_started"]


# --- Replay refusal before threshold (J5) ------------------------------------


def test_replay_before_threshold_is_refused_without_mutation(corpus, db):
    """Test 5: detection_attempt_not_stale, and nothing changes."""
    comparison_id, attempt_id = _interrupted(corpus, db)
    before = _snapshot(db)
    with pytest.raises(DetectionStateError) as excinfo:
        _replay(corpus, db, attempt_id, now=T0 + timedelta(seconds=899))
    assert excinfo.value.code == "detection_attempt_not_stale"
    assert _snapshot(db) == before
    assert _attempt(db, attempt_id)["status"] == ATTEMPT_RUNNING
    assert _status(db, comparison_id) == STATUS_DETECTING


# --- Successful replay (J6-J12) ---------------------------------------------


def test_explicit_replay_retires_stale_attempt_and_completes_replacement(corpus, db):
    """Tests 6-12: the stale attempt becomes timed_out with one timeout event,
    the replacement is attempt 2 with a started event, the linkage records
    operator metadata and policy version, and the replacement succeeds."""
    comparison_id, source_id = _interrupted(corpus, db)
    outcome, created = _replay(corpus, db, source_id)
    assert created is True

    source = _attempt(db, source_id)
    assert source["status"] == ATTEMPT_TIMED_OUT
    assert source["finished_at"] is not None
    assert source["result_hash"] is None
    assert source["failure_code"] == "detection_attempt_timed_out"
    assert source["failure_summary"]
    assert _events(db, source_id) == ["detection_started", "detection_timed_out"]

    replacement_id = outcome["replacement_attempt_id"]
    replacement = _attempt(db, replacement_id)
    assert replacement["attempt_number"] == 2
    assert replacement["status"] == ATTEMPT_SUCCEEDED
    assert _events(db, replacement_id) == [
        "detection_started",
        "detection_succeeded",
    ]
    assert outcome["replacement_status"] == ATTEMPT_SUCCEEDED
    assert _status(db, comparison_id) == STATUS_DETECTED

    stored = comparison_store.get_result(comparison_id, db_path=db)
    assert stored["result_hash"] == replacement["result_hash"]
    assert outcome["result"] == stored["result"]

    replay = outcome["replay"]
    assert replay["operator_id"] == OPERATOR
    assert replay["operator_note"] == NOTE
    assert replay["reason_code"] == REASON
    assert replay["policy_id"] == "detection_recovery_v1"
    assert replay["policy_version"] == "1"
    assert replay["source_attempt_id"] == source_id
    assert replay["replacement_attempt_id"] == replacement_id
    assert comparison_store.list_detection_replays(comparison_id, db_path=db) == [
        replay
    ]


def test_direct_library_operator_identity_is_labeled_legacy(corpus, db):
    """Direct callers stay visibly legacy; no authentication is invented."""
    _comparison_id, source_id = _interrupted(corpus, db)
    _replay(corpus, db, source_id)
    replay = comparison_store.get_detection_replay_for_source(source_id, db_path=db)
    dto = api._to_detection_replay_dto(replay)
    assert dto.operatorId == OPERATOR  # case preserved, verbatim
    assert dto.operatorIdBasis == "legacy_self_asserted"
    # And the module says so where an operator would read it (whitespace
    # normalized, because the statement wraps across lines).
    source = " ".join(
        Path(detection_recovery.__file__).read_text(encoding="utf-8").split()
    )
    assert "legacy_self_asserted" in source
    # The store's replay table and the API DTO say it too.
    store_source = " ".join(
        Path(comparison_store.__file__).read_text(encoding="utf-8").split()
    )
    assert "legacy_self_asserted" in store_source
    assert "local_hs256" in store_source


@pytest.mark.parametrize(
    "field, value, code",
    [
        ("operator", "", "invalid_operator_id"),
        ("operator", "   ", "invalid_operator_id"),
        ("operator", "x" * 200, "invalid_operator_id"),
        ("operator", "bad\nid", "invalid_operator_id"),
        ("operator", "bad\x7fid", "invalid_operator_id"),
        ("note", "", "invalid_operator_note"),
        ("note", "n" * 900, "invalid_operator_note"),
        ("note", "bad\tnote", "invalid_operator_note"),
        ("reason", "made_up_reason", "invalid_reason_code"),
        ("reason", "", "invalid_reason_code"),
    ],
)
def test_invalid_operator_fields_are_refused_without_mutation(
    corpus, db, field, value, code
):
    """Operator metadata is validated before anything is touched."""
    comparison_id, attempt_id = _interrupted(corpus, db)
    before = _snapshot(db)
    kwargs = {"operator": OPERATOR, "note": NOTE, "reason": REASON}
    kwargs[field] = value
    with pytest.raises(detection_recovery.ReplayRequestError) as excinfo:
        _replay(corpus, db, attempt_id, **kwargs)
    assert excinfo.value.code == code
    assert _snapshot(db) == before


def test_failed_replacement_leaves_source_timed_out(corpus, db, monkeypatch):
    """Test 13: the replacement fails through the existing terminal
    transaction while the retired attempt stays timed_out and the replay record
    remains as truthful linkage."""
    comparison_id, source_id = _interrupted(corpus, db)
    monkeypatch.setattr(
        comparison_detector,
        "detect_changes",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom /secret SELECT")),
    )
    with pytest.raises(comparison_detector.DetectionInternalError):
        _replay(corpus, db, source_id)
    monkeypatch.undo()

    assert _attempt(db, source_id)["status"] == ATTEMPT_TIMED_OUT
    replay = comparison_store.get_detection_replay_for_source(source_id, db_path=db)
    assert replay is not None
    replacement = _attempt(db, replay["replacement_attempt_id"])
    assert replacement["status"] == ATTEMPT_FAILED
    assert replacement["failure_code"] == "detector_internal_error"
    for forbidden in ("/secret", "SELECT", "boom", "Traceback"):
        assert forbidden not in replacement["failure_summary"]
    assert _events(db, replay["replacement_attempt_id"]) == [
        "detection_started",
        "detection_failed",
    ]
    assert _status(db, comparison_id) == STATUS_FAILED


def test_interruption_after_replay_start_leaves_replacement_running(corpus, db):
    """Test 14: the process dies between the replay transaction and the
    replacement's completion — the durable state is exactly as documented, and
    that replacement is itself replayable later."""
    comparison_id, source_id = _interrupted(corpus, db)
    previous_hash, current_hash = _hashes(corpus)
    # The replay transaction alone, with no execution afterwards.
    replay, created = comparison_store.start_detection_replay(
        source_id,
        operator_id=OPERATOR, reason_code=REASON, operator_note=NOTE,
        request_hash="rh", policy_id="detection_recovery_v1", policy_version="1",
        stale_after_seconds=900, max_attempts_per_comparison=3,
        detector_version=DETECTOR_VERSION, workflow_version=WORKFLOW_VERSION,
        previous_source_hash=previous_hash, current_source_hash=current_hash,
        now=WELL_STALE, db_path=db,
    )
    assert created is True
    replacement_id = replay["replacement_attempt_id"]
    assert _attempt(db, source_id)["status"] == ATTEMPT_TIMED_OUT
    assert _attempt(db, replacement_id)["status"] == ATTEMPT_RUNNING
    assert _status(db, comparison_id) == STATUS_DETECTING
    assert comparison_store.get_result(comparison_id, db_path=db) is None
    assert _events(db, replacement_id) == ["detection_started"]

    # The replacement is not stale yet at its own start time.
    view = detection_recovery.recovery_view(
        replacement_id, now=WELL_STALE, db_path=db, registry_path=corpus.registry
    )
    assert view["is_stale"] is False
    assert view["attempts_used"] == 2
    assert view["remaining_attempts"] == 1
    # After ITS threshold it becomes eligible — bounded by max attempts.
    later = detection_recovery.recovery_view(
        replacement_id,
        now=WELL_STALE + timedelta(seconds=900),
        db_path=db,
        registry_path=corpus.registry,
    )
    assert later["is_stale"] is True
    assert later["replay_eligible"] is True


# --- Idempotency and concurrency (G, J15-J18) --------------------------------


def test_identical_replay_request_is_idempotent(corpus, db):
    """Test 15: the same request returns the stored replay and does NOT execute
    the replacement a second time."""
    comparison_id, source_id = _interrupted(corpus, db)
    first, created_first = _replay(corpus, db, source_id)
    snapshot = _snapshot(db)
    second, created_second = _replay(corpus, db, source_id)
    assert created_first is True and created_second is False
    assert second["replay"] == first["replay"]
    assert second["replacement_attempt_id"] == first["replacement_attempt_id"]
    assert second["replacement_status"] == ATTEMPT_SUCCEEDED
    assert second["result"] == first["result"]
    # Test 8 of G: the stored terminal outcome is returned; nothing re-ran.
    assert _snapshot(db) == snapshot
    assert len(
        comparison_store.list_detection_attempts(comparison_id, db_path=db)
    ) == 2


def test_different_replay_request_for_replayed_attempt_conflicts(corpus, db):
    """Test 16: one stale attempt yields at most one replacement."""
    comparison_id, source_id = _interrupted(corpus, db)
    _replay(corpus, db, source_id)
    snapshot = _snapshot(db)
    with pytest.raises(DetectionStateError) as excinfo:
        _replay(corpus, db, source_id, note="A different note entirely.")
    assert excinfo.value.code == "detection_replay_already_exists"
    assert _snapshot(db) == snapshot
    assert len(
        comparison_store.list_detection_attempts(comparison_id, db_path=db)
    ) == 2


def test_concurrent_identical_replays_produce_one_replay(corpus, tmp_path):
    """Test 17: eight racing identical requests -> one replay, one replacement,
    one detector execution."""
    db = tmp_path / "concurrent-same.db"
    comparison_id, source_id = _interrupted(corpus, db)

    def attempt(_n):
        try:
            return _replay(corpus, db, source_id)
        except (DetectionStateError, comparison_detector.DetectionError) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))

    successes = [o for o in outcomes if isinstance(o, tuple)]
    assert sum(1 for _o, created in successes if created) == 1
    replay_ids = {o[0]["replay"]["replay_id"] for o in successes}
    assert len(replay_ids) == 1
    with closing(sqlite3.connect(str(db))) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM comparison_detection_replays"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM comparison_detection_attempts"
        ).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM comparison_results").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_concurrent_different_replays_produce_one_winner(corpus, tmp_path):
    """Test 18: distinct requests race; exactly one wins and the rest conflict."""
    db = tmp_path / "concurrent-diff.db"
    comparison_id, source_id = _interrupted(corpus, db)

    def attempt(n):
        try:
            return _replay(corpus, db, source_id, note=f"Distinct note {n}.")
        except DetectionStateError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))

    winners = [o for o in outcomes if isinstance(o, tuple)]
    conflicts = [o for o in outcomes if isinstance(o, DetectionStateError)]
    assert len(winners) == 1
    assert len(conflicts) == 5
    assert all(
        exc.code == "detection_replay_already_exists" for exc in conflicts
    )
    with closing(sqlite3.connect(str(db))) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM comparison_detection_replays"
        ).fetchone()[0] == 1
        numbers = [
            row[0]
            for row in conn.execute(
                "SELECT attempt_number FROM comparison_detection_attempts "
                "ORDER BY attempt_number"
            )
        ]
    assert numbers == [1, 2]  # monotonic, no gaps or duplicates


# --- Attempt limit (J19-J20) -------------------------------------------------


def _exhaust_to_limit(corpus, db):
    """Drive a comparison to its 3-attempt limit, ending with a running attempt.

    Each replay's replacement is itself interrupted (started but never
    completed) by backdating it, so the next replay is eligible.
    """
    comparison_id, first_id = _interrupted(corpus, db)
    previous_hash, current_hash = _hashes(corpus)
    current_source = first_id
    for step in range(2):  # attempts 2 and 3
        replay, _created = comparison_store.start_detection_replay(
            current_source,
            operator_id=OPERATOR, reason_code=REASON,
            operator_note=f"Interrupted step {step}.",
            request_hash=f"rh{step}", policy_id="detection_recovery_v1",
            policy_version="1", stale_after_seconds=900,
            max_attempts_per_comparison=3,
            detector_version=DETECTOR_VERSION, workflow_version=WORKFLOW_VERSION,
            previous_source_hash=previous_hash, current_source_hash=current_hash,
            now=WELL_STALE, db_path=db,
        )
        current_source = replay["replacement_attempt_id"]
        _sql(
            db,
            "UPDATE comparison_detection_attempts SET started_at = ? "
            "WHERE attempt_id = ?",
            (T0.isoformat(), current_source),
        )
    return comparison_id, current_source


def test_attempt_limit_is_enforced(corpus, db):
    """Test 19: the third attempt cannot be replayed a fourth time."""
    comparison_id, last_id = _exhaust_to_limit(corpus, db)
    assert comparison_store.count_detection_attempts(comparison_id, db_path=db) == 3

    view = detection_recovery.recovery_view(
        last_id, now=WELL_STALE, db_path=db, registry_path=corpus.registry
    )
    assert view["is_stale"] is True
    assert view["replay_eligible"] is False
    assert view["blocking_reason"] == "detection_attempt_limit_reached"
    assert view["attempts_used"] == 3
    assert view["remaining_attempts"] == 0

    before = _snapshot(db)
    with pytest.raises(DetectionStateError) as excinfo:
        _replay(corpus, db, last_id)
    assert excinfo.value.code == "detection_attempt_limit_reached"
    assert _snapshot(db) == before


def test_attempt_limit_holds_under_concurrency(corpus, tmp_path):
    """Test 20: racing replays at the boundary cannot exceed the limit."""
    db = tmp_path / "limit-race.db"
    comparison_id, first_id = _interrupted(corpus, db)
    previous_hash, current_hash = _hashes(corpus)
    # Limit of 2: the original plus exactly one replay.
    policy = dict(detection_recovery.POLICY, max_attempts_per_comparison=2)

    def attempt(n):
        try:
            return _replay(
                corpus, db, first_id, note=f"Race {n}.", policy=policy
            )
        except DetectionStateError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(attempt, range(6)))

    assert comparison_store.count_detection_attempts(comparison_id, db_path=db) == 2
    # A further replay of the replacement is refused by the limit.
    replacement = comparison_store.get_detection_replay_for_source(
        first_id, db_path=db
    )["replacement_attempt_id"]
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET started_at = ? WHERE attempt_id = ?",
        (T0.isoformat(), replacement),
    )
    if _attempt(db, replacement)["status"] == ATTEMPT_RUNNING:
        with pytest.raises(DetectionStateError) as excinfo:
            _replay(corpus, db, replacement, policy=policy)
        assert excinfo.value.code == "detection_attempt_limit_reached"


# --- Input / version drift (F, J21-J23) --------------------------------------


def test_changed_source_hash_refuses_replay_without_mutation(corpus, db):
    """Test 21: replay never silently compares different inputs."""
    comparison_id, attempt_id = _interrupted(corpus, db)
    before = _snapshot(db)
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET previous_source_hash = ? "
        "WHERE attempt_id = ?",
        ("0" * 64, attempt_id),
    )
    baseline = _snapshot(db)
    with pytest.raises(DetectionStateError) as excinfo:
        _replay(corpus, db, attempt_id)
    assert excinfo.value.code == "detection_replay_inputs_changed"
    assert _snapshot(db) == baseline
    assert _attempt(db, attempt_id)["status"] == ATTEMPT_RUNNING

    view = detection_recovery.recovery_view(
        attempt_id, now=WELL_STALE, db_path=db, registry_path=corpus.registry
    )
    assert view["replay_eligible"] is False
    assert view["blocking_reason"] == "detection_replay_inputs_changed"
    assert before != baseline  # the test really did perturb the hash


@pytest.mark.parametrize(
    "column, value",
    [
        ("detector_version", "item1a_detector.v1"),
        ("workflow_version", "comparison_workflow.v1"),
    ],
)
def test_changed_versions_refuse_replay_without_mutation(corpus, db, column, value):
    """Tests 22-23: a detector or workflow version change is a NEW comparison,
    never a replay of this one."""
    comparison_id, attempt_id = _interrupted(corpus, db)
    _sql(
        db,
        f"UPDATE comparison_detection_attempts SET {column} = ? WHERE attempt_id = ?",
        (value, attempt_id),
    )
    before = _snapshot(db)
    with pytest.raises(DetectionStateError) as excinfo:
        _replay(corpus, db, attempt_id)
    assert excinfo.value.code == "detection_replay_version_changed"
    assert _snapshot(db) == before

    view = detection_recovery.recovery_view(
        attempt_id, now=WELL_STALE, db_path=db, registry_path=corpus.registry
    )
    assert view["blocking_reason"] == "detection_replay_version_changed"


def test_non_running_attempt_cannot_be_replayed(corpus, db):
    """A succeeded or failed attempt is not a stale execution: general retry of
    a finished attempt is deliberately out of scope."""
    comparison_id, attempt_id = _interrupted(corpus, db)
    _replay(corpus, db, attempt_id)  # replacement succeeded -> detected
    replacement = comparison_store.get_detection_replay_for_source(
        attempt_id, db_path=db
    )["replacement_attempt_id"]
    before = _snapshot(db)
    with pytest.raises(DetectionStateError) as excinfo:
        _replay(corpus, db, replacement, now=WELL_STALE + timedelta(seconds=5000))
    # The comparison is no longer detecting, which is reported first.
    assert excinfo.value.code == "detection_transition_invalid"
    assert _snapshot(db) == before


# --- Rollback and durability (I, J24-J25) ------------------------------------


def test_replay_transaction_rollback_leaves_original_running(corpus, db, monkeypatch):
    """Test 24: a failure inside the replay transaction applies nothing — no
    timeout event, no replacement, no replay record."""
    comparison_id, attempt_id = _interrupted(corpus, db)
    before = _snapshot(db)
    real_insert_event = comparison_store._insert_detection_event

    def boom(conn, **kwargs):
        if kwargs.get("event_type") == comparison_store.EVENT_DETECTION_STARTED:
            raise sqlite3.OperationalError("simulated mid-transaction failure")
        return real_insert_event(conn, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(comparison_store, "_insert_detection_event", boom)
        with pytest.raises(sqlite3.OperationalError):
            _replay(corpus, db, attempt_id)

    assert _snapshot(db) == before
    assert _attempt(db, attempt_id)["status"] == ATTEMPT_RUNNING
    assert _events(db, attempt_id) == ["detection_started"]
    assert _status(db, comparison_id) == STATUS_DETECTING
    assert comparison_store.get_detection_replay_for_source(
        attempt_id, db_path=db
    ) is None
    with closing(sqlite3.connect(str(db))) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_replay_and_linked_attempts_survive_reopen(corpus, db):
    """Test 25: replay linkage and both attempts persist across connections."""
    comparison_id, source_id = _interrupted(corpus, db)
    outcome, _created = _replay(corpus, db, source_id)
    replacement_id = outcome["replacement_attempt_id"]

    reopened = comparison_store.get_detection_replay_for_source(source_id, db_path=db)
    assert reopened == outcome["replay"]
    assert _attempt(db, source_id)["status"] == ATTEMPT_TIMED_OUT
    assert _attempt(db, replacement_id)["status"] == ATTEMPT_SUCCEEDED
    numbers = [
        attempt["attempt_number"]
        for attempt in comparison_store.list_detection_attempts(
            comparison_id, db_path=db
        )
    ]
    assert numbers == [1, 2]
    with closing(sqlite3.connect(str(db))) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --- Migration (J26-J27) -----------------------------------------------------


def test_migration_preserves_pre_timeout_attempts_and_events(tmp_path):
    """Tests 26-27: a database whose CHECKs predate timed_out migrates in
    place, keeps every attempt and event, accepts the new vocabulary, and
    migrates only once."""
    db = tmp_path / "old.db"
    init_db(db)
    # Roll the two tables back to their pre-timeout DDL, with rows.
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.execute("DROP TABLE comparison_detection_replays")
        conn.execute("DROP TABLE comparison_detection_events")
        conn.execute("DROP TABLE comparison_detection_attempts")
        conn.execute(
            """
            CREATE TABLE comparison_detection_attempts (
                attempt_id           TEXT PRIMARY KEY NOT NULL,
                comparison_id        TEXT NOT NULL,
                attempt_number       INTEGER NOT NULL,
                status               TEXT NOT NULL
                                     CHECK (status IN ('running', 'succeeded',
                                                       'failed')),
                detector_version     TEXT NOT NULL,
                workflow_version     TEXT NOT NULL,
                previous_source_hash TEXT NOT NULL,
                current_source_hash  TEXT NOT NULL,
                started_at           TEXT NOT NULL,
                finished_at          TEXT,
                result_hash          TEXT,
                failure_code         TEXT,
                failure_summary      TEXT,
                UNIQUE (comparison_id, attempt_number)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE comparison_detection_events (
                event_id      TEXT PRIMARY KEY NOT NULL,
                attempt_id    TEXT NOT NULL,
                comparison_id TEXT NOT NULL,
                event_type    TEXT NOT NULL
                              CHECK (event_type IN ('detection_started',
                                                    'detection_succeeded',
                                                    'detection_failed')),
                event_seq     INTEGER NOT NULL,
                created_at    TEXT NOT NULL,
                result_hash   TEXT,
                failure_code  TEXT,
                UNIQUE (attempt_id, event_type)
            )
            """
        )
        conn.execute(
            "INSERT INTO comparisons VALUES ('cmp_x', 'comparison.v1', "
            "'comparison_workflow.v2', 'p', 'c', '[\"item_1a_risk_factors\"]', "
            "'detected', 't0', 't0', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO comparison_detection_attempts VALUES "
            "('att_ok', 'cmp_x', 1, 'succeeded', 'item1a_detector.v2', "
            "'comparison_workflow.v2', 'h1', 'h2', 't0', 't1', 'rh', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO comparison_detection_events VALUES "
            "('evt_1', 'att_ok', 'cmp_x', 'detection_started', 0, 't0', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO comparison_detection_events VALUES "
            "('evt_2', 'att_ok', 'cmp_x', 'detection_succeeded', 1, 't1', 'rh', NULL)"
        )

    for _ in range(3):  # idempotent
        init_db(db)

    with closing(sqlite3.connect(str(db))) as conn:
        conn.row_factory = sqlite3.Row
        attempts_ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='comparison_detection_attempts'"
        ).fetchone()[0]
        events_ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='comparison_detection_events'"
        ).fetchone()[0]
        assert "'timed_out'" in attempts_ddl
        assert "'detection_timed_out'" in events_ddl
        assert "_rebuilt" not in attempts_ddl and "_rebuilt" not in events_ddl
        # Existing rows preserved byte for byte.
        attempt = dict(
            conn.execute("SELECT * FROM comparison_detection_attempts").fetchone()
        )
        assert attempt["attempt_id"] == "att_ok"
        assert attempt["status"] == "succeeded"
        assert attempt["result_hash"] == "rh"
        assert [
            row["event_type"]
            for row in conn.execute(
                "SELECT event_type FROM comparison_detection_events ORDER BY event_seq"
            )
        ] == ["detection_started", "detection_succeeded"]
        # The replays table was created by the same init.
        assert conn.execute(
            "SELECT COUNT(*) FROM comparison_detection_replays"
        ).fetchone()[0] == 0
        # The new vocabulary is accepted post-migration.
        with conn:
            conn.execute(
                "UPDATE comparison_detection_attempts SET status='timed_out', "
                "result_hash=NULL, failure_code='detection_attempt_timed_out', "
                "failure_summary='s' WHERE attempt_id='att_ok'"
            )
            conn.execute(
                "INSERT INTO comparison_detection_events VALUES "
                "('evt_3', 'att_ok', 'cmp_x', 'detection_timed_out', 1, 't2', "
                "NULL, 'detection_attempt_timed_out')"
            )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_timed_out_coherence_is_a_storage_invariant(corpus, db):
    """B4/B6/B7: storage refuses an incoherent or non-terminal timed-out row."""
    comparison_id, source_id = _interrupted(corpus, db)
    _replay(corpus, db, source_id)
    # A timed-out attempt may not gain a result hash or lose its failure code.
    for statement in (
        "UPDATE comparison_detection_attempts SET result_hash='h' "
        "WHERE attempt_id = ?",
        "UPDATE comparison_detection_attempts SET failure_code=NULL "
        "WHERE attempt_id = ?",
        "UPDATE comparison_detection_attempts SET finished_at=NULL "
        "WHERE attempt_id = ?",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _sql(db, statement, (source_id,))
    # Exactly one timeout event per attempt.
    with pytest.raises(sqlite3.IntegrityError):
        _sql(
            db,
            "INSERT INTO comparison_detection_events (event_id, attempt_id,"
            " comparison_id, event_type, event_seq, created_at)"
            " VALUES ('evt_dupe', ?, ?, 'detection_timed_out', 1, 't')",
            (source_id, comparison_id),
        )
    # Terminal: a timed-out attempt is never re-finalized.
    for call in (
        lambda: comparison_store.fail_detection_attempt(
            source_id, failure_code="c", failure_summary="s", db_path=db
        ),
        lambda: comparison_store.complete_detection_attempt(
            source_id, result_json="{}", result_hash="h",
            detector_version=DETECTOR_VERSION, workflow_version=WORKFLOW_VERSION,
            previous_source_hash="a", current_source_hash="b", db_path=db,
        ),
    ):
        with pytest.raises(DetectionStateError) as excinfo:
            call()
        assert excinfo.value.code == "detection_attempt_not_running"


# --- API surface (H, J28-J29) -----------------------------------------------


@pytest.fixture
def api_env(corpus, tmp_path, monkeypatch):
    db = tmp_path / "api.db"
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(db))
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(corpus.registry))
    monkeypatch.setattr(comparison_detector, "open_index", lambda: corpus.chroma)
    return SimpleNamespace(db=db)


def test_recovery_and_replay_routes(api_env, corpus):
    """The full API contract: read-only recovery, 409 before threshold, 201
    replay, 200 idempotent replay, and the replays listing."""
    comparison_id, source_id = _interrupted(corpus, api_env.db)

    recovery = client.get(f"/api/detection-attempts/{source_id}/recovery")
    assert recovery.status_code == 200
    body = recovery.json()
    assert set(body) == {
        "attemptId", "comparisonId", "status", "startedAt", "staleAt",
        "ageSeconds", "isStale", "replayEligible", "attemptsUsed",
        "maxAttempts", "remainingAttempts", "policyId", "policyVersion",
        "blockingReason",
    }
    assert body["status"] == "running"
    assert body["maxAttempts"] == 3
    # Real wall-clock now: the backdated attempt IS stale.
    assert body["isStale"] is True
    assert client.get(f"/api/detection-attempts/{source_id}/replays").json() == []

    # A replay before the threshold: 409 with the stable code.
    _sql(
        api_env.db,
        "UPDATE comparison_detection_attempts SET started_at = ? WHERE attempt_id = ?",
        (datetime.now(timezone.utc).isoformat(), source_id),
    )
    early = client.post(
        f"/api/detection-attempts/{source_id}/replay",
        json={"reasonCode": REASON, "operatorNote": NOTE},
    )
    assert early.status_code == 409
    assert early.json()["detail"]["code"] == "detection_attempt_not_stale"
    assert _attempt(api_env.db, source_id)["status"] == ATTEMPT_RUNNING

    # Backdate again and replay for real.
    _sql(
        api_env.db,
        "UPDATE comparison_detection_attempts SET started_at = ? WHERE attempt_id = ?",
        ((datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat(), source_id),
    )
    replayed = client.post(
        f"/api/detection-attempts/{source_id}/replay",
        json={"reasonCode": REASON, "operatorNote": NOTE},
    )
    assert replayed.status_code == 201
    payload = replayed.json()
    assert payload["created"] is True
    assert payload["sourceAttemptId"] == source_id
    assert payload["replacementStatus"] == "succeeded"
    assert payload["result"]["comparison_id"] == comparison_id
    # Recovery metadata never enters comparison.v1.
    for key in ("replay", "replayId", "operatorId", "attemptId", "staleAt"):
        assert key not in payload["result"]
    assert set(payload["replay"]) == {
        "replayId", "comparisonId", "sourceAttemptId", "replacementAttemptId",
        "operatorId", "operatorIdBasis", "reasonCode", "operatorNote",
        "policyId", "policyVersion", "requestedAt",
    }
    assert payload["replay"]["operatorId"] == DEFAULT_TEST_SUBJECT
    assert payload["replay"]["operatorIdBasis"] == "local_hs256"

    # Idempotent repeat.
    again = client.post(
        f"/api/detection-attempts/{source_id}/replay",
        json={"reasonCode": REASON, "operatorNote": NOTE},
    )
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["replay"] == payload["replay"]

    listing = client.get(f"/api/detection-attempts/{source_id}/replays")
    assert listing.status_code == 200
    assert listing.json() == [payload["replay"]]

    # The retired attempt and its timeout event are visible.
    assert client.get(
        f"/api/detection-attempts/{source_id}"
    ).json()["status"] == "timed_out"
    assert [
        event["eventType"]
        for event in client.get(
            f"/api/detection-attempts/{source_id}/events"
        ).json()
    ] == ["detection_started", "detection_timed_out"]

    # 404s and 422s.
    assert client.get("/api/detection-attempts/att_nope/recovery").status_code == 404
    assert client.get("/api/detection-attempts/att_nope/replays").status_code == 404
    assert client.post(
        "/api/detection-attempts/att_nope/replay",
        json={"reasonCode": REASON, "operatorNote": NOTE},
    ).status_code == 404
    for body in (
        {"operatorId": "", "reasonCode": REASON, "operatorNote": NOTE},
        {"reasonCode": "nope", "operatorNote": NOTE},
        {"reasonCode": REASON, "operatorNote": ""},
        # The client cannot submit workflow state.
        {"reasonCode": REASON, "operatorNote": NOTE, "isStale": True},
        {"reasonCode": REASON, "operatorNote": NOTE, "policyId": "other"},
        {"reasonCode": REASON, "operatorNote": NOTE,
         "replacementAttemptId": "att_x"},
    ):
        response = client.post(
            f"/api/detection-attempts/{source_id}/replay", json=body
        )
        assert response.status_code == 422, body


def test_job_owned_attempt_uses_lease_reclaim_not_operator_replay(
    api_env, corpus
):
    comparison_id, claimed, previous_hash, current_hash = _job_interrupted(
        corpus, api_env.db, comparison_id_suffix="api-job"
    )
    attempt_id = claimed["attempt"]["attempt_id"]
    recovery = client.get(
        f"/api/detection-attempts/{attempt_id}/recovery"
    )
    assert recovery.status_code == 200
    body = recovery.json()
    assert body["comparisonId"] == comparison_id
    assert body["status"] == comparison_store.ATTEMPT_RUNNING
    assert body["isStale"] is True
    assert body["attemptsUsed"] == 1
    assert body["replayEligible"] is False
    assert body["blockingReason"] == (
        comparison_store.REASON_ATTEMPT_MANAGED_BY_JOB
    )

    before = _snapshot(api_env.db)
    before_bytes = api_env.db.read_bytes()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            comparison_detector,
            "resolve_detection_inputs",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError(
                    "job-managed replay must refuse before registry resolution"
                )
            ),
        )
        replayed = client.post(
            f"/api/detection-attempts/{attempt_id}/replay",
            json={"reasonCode": REASON, "operatorNote": NOTE},
        )
    assert replayed.status_code == 409
    assert replayed.json()["detail"] == {
        "code": comparison_store.REASON_ATTEMPT_MANAGED_BY_JOB,
        "message": (
            "this detection attempt is managed by a worker job and must "
            "recover through fenced lease reclaim"
        ),
    }
    assert _snapshot(api_env.db) == before
    assert api_env.db.read_bytes() == before_bytes
    assert comparison_store.get_detection_replay_for_source(
        attempt_id, db_path=api_env.db
    ) is None

    issue_rows = comparison_reliability.issues(
        now=WELL_STALE,
        db_path=api_env.db,
        registry_path=corpus.registry,
    )["issues"]
    expired = [
        issue
        for issue in issue_rows
        if issue["issue_type"]
        == comparison_reliability.ISSUE_EXPIRED_DETECTION_JOB_LEASE
    ]
    assert len(expired) == 1
    assert expired[0]["attempt_id"] == attempt_id
    assert expired[0]["recommended_action_code"] == (
        comparison_reliability.ACTION_RECLAIM_EXPIRED_JOB
    )
    assert not any(
        issue["issue_type"]
        == comparison_reliability.ISSUE_STALE_RUNNING_ATTEMPT
        and issue["attempt_id"] == attempt_id
        for issue in issue_rows
    )

    reclaimed = comparison_store.claim_detection_job(
        job_id=claimed["job"]["job_id"],
        worker_id="worker-two",
        detector_version=DETECTOR_VERSION,
        workflow_version=WORKFLOW_VERSION,
        previous_source_hash=previous_hash,
        current_source_hash=current_hash,
        lease_duration_seconds=(
            detection_job_lease.POLICY["lease_duration_seconds"]
        ),
        reclaim_grace_seconds=(
            detection_job_lease.POLICY["reclaim_grace_seconds"]
        ),
        max_claim_generations=(
            detection_job_lease.POLICY["max_claim_generations"]
        ),
        now=WELL_STALE,
        db_path=api_env.db,
    )
    assert reclaimed["kind"] == "reclaimed"
    assert reclaimed["job"]["claim_generation"] == 2
    assert comparison_store.get_detection_replay_for_source(
        attempt_id, db_path=api_env.db
    ) is None
    retired_view = detection_recovery.recovery_view(
        attempt_id,
        now=WELL_STALE + timedelta(seconds=1),
        db_path=api_env.db,
        registry_path=corpus.registry,
    )
    assert retired_view["status"] == comparison_store.ATTEMPT_TIMED_OUT
    assert retired_view["replay_eligible"] is False
    assert retired_view["blocking_reason"] == (
        comparison_store.REASON_ATTEMPT_MANAGED_BY_JOB
    )

    terminal = comparison_store.fail_detection_job(
        reclaimed["job"]["job_id"],
        reclaimed["attempt"]["attempt_id"],
        worker_id="worker-two",
        claim_generation=2,
        claim_token=reclaimed["claim_token"],
        failure_code="controlled_detector_failure",
        failure_summary="controlled safe failure",
        now=WELL_STALE + timedelta(seconds=1),
        db_path=api_env.db,
    )
    assert terminal["job"]["status"] == comparison_store.JOB_FAILED
    terminal_view = detection_recovery.recovery_view(
        terminal["attempt"]["attempt_id"],
        now=WELL_STALE + timedelta(seconds=2),
        db_path=api_env.db,
        registry_path=corpus.registry,
    )
    assert terminal_view["replay_eligible"] is False
    assert terminal_view["blocking_reason"] == (
        comparison_store.REASON_ATTEMPT_MANAGED_BY_JOB
    )


def test_concurrent_operator_replay_and_job_reclaim_share_one_attempt_budget(
    corpus, db
):
    comparison_id, claimed, previous_hash, current_hash = _job_interrupted(
        corpus, db, comparison_id_suffix="race-job"
    )
    source_attempt_id = claimed["attempt"]["attempt_id"]

    def replay():
        try:
            _replay(corpus, db, source_attempt_id, now=WELL_STALE)
        except comparison_store.DetectionStateError as exc:
            return ("replay_refused", exc.code)
        return ("replay_created", None)

    def reclaim():
        outcome = comparison_store.claim_detection_job(
            job_id=claimed["job"]["job_id"],
            worker_id="worker-two",
            detector_version=DETECTOR_VERSION,
            workflow_version=WORKFLOW_VERSION,
            previous_source_hash=previous_hash,
            current_source_hash=current_hash,
            lease_duration_seconds=(
                detection_job_lease.POLICY["lease_duration_seconds"]
            ),
            reclaim_grace_seconds=(
                detection_job_lease.POLICY["reclaim_grace_seconds"]
            ),
            max_claim_generations=(
                detection_job_lease.POLICY["max_claim_generations"]
            ),
            now=WELL_STALE,
            db_path=db,
        )
        return ("reclaimed", outcome)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(replay), pool.submit(reclaim))
        results = [future.result() for future in futures]
    replay_result = next(
        result for result in results if result[0].startswith("replay")
    )
    reclaim_result = next(
        result for result in results if result[0] == "reclaimed"
    )
    assert replay_result[0] == "replay_refused"
    assert replay_result[1] in {
        comparison_store.REASON_ATTEMPT_MANAGED_BY_JOB,
        comparison_store.REASON_ATTEMPT_NOT_RUNNING,
    }
    reclaimed = reclaim_result[1]
    assert reclaimed["kind"] == "reclaimed"
    assert reclaimed["job"]["claim_generation"] == 2
    assert comparison_store.count_detection_attempts(
        comparison_id, db_path=db
    ) == 2
    assert comparison_store.list_detection_replays(
        comparison_id, db_path=db
    ) == []

    replacement_id = reclaimed["attempt"]["attempt_id"]
    replacement_view = detection_recovery.recovery_view(
        replacement_id,
        now=WELL_STALE + timedelta(seconds=1),
        db_path=db,
        registry_path=corpus.registry,
    )
    assert replacement_view["replay_eligible"] is False
    assert replacement_view["blocking_reason"] == (
        comparison_store.REASON_ATTEMPT_MANAGED_BY_JOB
    )
    assert replacement_view["attempts_used"] == 2

    step = timedelta(
        seconds=detection_job_lease.POLICY["lease_duration_seconds"]
        + detection_job_lease.POLICY["reclaim_grace_seconds"]
        + 1
    )
    generation_three_at = WELL_STALE + step
    generation_three = comparison_store.claim_detection_job(
        job_id=claimed["job"]["job_id"],
        worker_id="worker-three",
        detector_version=DETECTOR_VERSION,
        workflow_version=WORKFLOW_VERSION,
        previous_source_hash=previous_hash,
        current_source_hash=current_hash,
        lease_duration_seconds=(
            detection_job_lease.POLICY["lease_duration_seconds"]
        ),
        reclaim_grace_seconds=(
            detection_job_lease.POLICY["reclaim_grace_seconds"]
        ),
        max_claim_generations=(
            detection_job_lease.POLICY["max_claim_generations"]
        ),
        now=generation_three_at,
        db_path=db,
    )
    assert generation_three["kind"] == "reclaimed"
    assert generation_three["job"]["claim_generation"] == 3
    assert comparison_store.count_detection_attempts(
        comparison_id, db_path=db
    ) == 3

    exhausted = comparison_store.claim_detection_job(
        job_id=claimed["job"]["job_id"],
        worker_id="worker-four",
        detector_version=DETECTOR_VERSION,
        workflow_version=WORKFLOW_VERSION,
        previous_source_hash=previous_hash,
        current_source_hash=current_hash,
        lease_duration_seconds=(
            detection_job_lease.POLICY["lease_duration_seconds"]
        ),
        reclaim_grace_seconds=(
            detection_job_lease.POLICY["reclaim_grace_seconds"]
        ),
        max_claim_generations=(
            detection_job_lease.POLICY["max_claim_generations"]
        ),
        now=generation_three_at + step,
        db_path=db,
    )
    assert exhausted["kind"] == "exhausted"
    assert comparison_store.count_detection_attempts(
        comparison_id, db_path=db
    ) == 3
    assert comparison_store.list_detection_replays(
        comparison_id, db_path=db
    ) == []


def test_api_responses_carry_no_paths_sql_or_evidence(api_env, corpus):
    """Test 28."""
    comparison_id, source_id = _interrupted(corpus, api_env.db)
    _sql(
        api_env.db,
        "UPDATE comparison_detection_attempts SET started_at = ? WHERE attempt_id = ?",
        ((datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat(), source_id),
    )
    replayed = client.post(
        f"/api/detection-attempts/{source_id}/replay",
        json={"reasonCode": REASON, "operatorNote": NOTE},
    )
    recovery = client.get(f"/api/detection-attempts/{source_id}/recovery")
    replays = client.get(f"/api/detection-attempts/{source_id}/replays")

    combined = recovery.text + replays.text
    for forbidden in (
        "/Users", "/private", "/var/folders", "SELECT", "INSERT", "sqlite",
        "result_json", "excerpt", "chunk_id", "Traceback",
    ):
        assert forbidden not in combined, forbidden
    # The replay response carries the comparison.v1 document by design, but no
    # storage internals.
    for forbidden in ("/Users", "/private", "SELECT", "sqlite3", "Traceback"):
        assert forbidden not in replayed.text, forbidden


def test_operator_note_never_reaches_server_logs(api_env, corpus, caplog):
    """Test 29: operator prose stays out of error logs."""
    comparison_id, source_id = _interrupted(corpus, api_env.db)
    _sql(
        api_env.db,
        "UPDATE comparison_detection_attempts SET started_at = ? WHERE attempt_id = ?",
        ((datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat(), source_id),
    )
    secret_note = "CONFIDENTIAL incident ticket INC-4711 details"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            comparison_detector,
            "detect_changes",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with caplog.at_level("ERROR"):
            response = client.post(
                f"/api/detection-attempts/{source_id}/replay",
                json={
                    "reasonCode": REASON,
                    "operatorNote": secret_note,
                },
            )
    assert response.status_code == 500
    assert response.json()["detail"]["error_id"].startswith("err_")
    assert secret_note not in caplog.text
    assert secret_note not in response.text
    assert "CONFIDENTIAL" not in caplog.text
    # It IS durably recorded where it belongs: the replay row.
    replay = comparison_store.get_detection_replay_for_source(
        source_id, db_path=api_env.db
    )
    assert replay["operator_note"] == secret_note


# --- Existing behavior unchanged (J30, J33) ---------------------------------


def test_normal_detect_idempotency_is_unchanged(corpus, db):
    """Test 30: adding replay did not disturb the first-attempt contract."""
    record, _ = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry
    )
    comparison_id = record["comparison_id"]
    first, created_first = comparison_detector.detect(
        comparison_id, db_path=db, registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    second, created_second = comparison_detector.detect(
        comparison_id, db_path=db, registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    assert created_first is True and created_second is False
    assert second == first
    attempts = comparison_store.list_detection_attempts(comparison_id, db_path=db)
    assert len(attempts) == 1
    assert attempts[0]["status"] == ATTEMPT_SUCCEEDED
    assert comparison_store.list_detection_replays(comparison_id, db_path=db) == []


def _code_identifiers(path: Path) -> set[str]:
    """Every name, attribute, and keyword-argument identifier in a module's CODE.

    AST-based on purpose: a substring scan of the raw file cannot tell the
    difference between a scheduler and a docstring promising there is no
    scheduler, so it would flag this repository's own honesty.
    """
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_no_automatic_recovery_machinery_exists():
    """Test 33: replay is operator-triggered only. No timer, scheduler, worker,
    queue, backoff, retry counter, or mtime inference anywhere — checked against
    code identifiers, not prose."""
    root = Path(__file__).resolve().parent.parent
    forbidden = {
        "sleep", "Timer", "timer", "create_task", "run_forever", "getmtime",
        "st_mtime", "crontab", "apscheduler", "celery", "sched",
        "max_retries", "retry_count", "retries", "backoff", "dead_letter",
        "dlq", "worker_thread", "background_task", "BackgroundTasks",
    }
    # Generic operator replay remains free of automatic machinery. Detection
    # jobs now have a separate bounded retry state machine in the store.
    for name in ("detection_recovery.py",):
        found = _code_identifiers(root / name) & forbidden
        assert found == set(), f"{name}: {sorted(found)}"

    # And no network, model, or concurrency client reaches the recovery path.
    recovery_names = _code_identifiers(root / "detection_recovery.py")
    assert recovery_names & {
        "boto3", "botocore", "requests", "httpx", "urllib", "socket", "openai",
        "anthropic", "langchain_aws", "threading", "asyncio", "multiprocessing",
        "subprocess",
    } == set(), sorted(recovery_names)


def test_replay_requires_an_explicit_operator_request(corpus, db):
    """The whole point: time passing does not retire anything. Only a call to
    replay_attempt with operator metadata does."""
    comparison_id, attempt_id = _interrupted(corpus, db)
    before = _snapshot(db)
    # Every read-only path, at a time far past the threshold.
    for _ in range(3):
        detection_recovery.recovery_view(
            attempt_id, now=WELL_STALE + timedelta(days=30), db_path=db,
            registry_path=corpus.registry,
        )
        comparison_store.get_detection_attempt(attempt_id, db_path=db)
        comparison_store.list_detection_attempts(comparison_id, db_path=db)
        comparison_store.list_detection_events(attempt_id, db_path=db)
        comparison_store.get_detection_replay_for_source(attempt_id, db_path=db)
        comparison_store.count_detection_attempts(comparison_id, db_path=db)
    assert _snapshot(db) == before
    assert _attempt(db, attempt_id)["status"] == ATTEMPT_RUNNING
    assert _status(db, comparison_id) == STATUS_DETECTING
