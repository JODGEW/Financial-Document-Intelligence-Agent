"""Bounded deterministic retry scheduling for worker-owned detection jobs."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from governance.policy_validation import GovernancePolicyConfigError

import comparison_detection_worker
import comparison_detector
import comparison_reliability
import comparison_store
import detection_job_lease
import detection_job_retry
from tests.test_comparison_detection_jobs import (
    _api_create,
    _claim,
    _comparison,
    _enqueue,
    corpus,
    db,
    api_env,
)


T0 = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
TRANSIENT = detection_job_retry.FAILURE_DEPENDENCY_UNAVAILABLE


def _retry_policy(*, maximum=2, delays=None):
    return {
        "policy_id": "retry-test",
        "policy_version": "1",
        "max_retry_attempts": maximum,
        "retry_delays_seconds": delays if delays is not None else [5, 30][:maximum],
        "retryable_failure_codes": [TRANSIENT],
    }


def _schedule(corpus, db, *, maximum=2, max_generations=3):
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    claimed = _claim(corpus, db, job["job_id"], "worker-retry-source")
    outcome = comparison_store.fail_detection_job(
        job["job_id"],
        claimed["attempt"]["attempt_id"],
        worker_id="worker-retry-source",
        claim_generation=claimed["job"]["claim_generation"],
        claim_token=claimed["claim_token"],
        failure_code=TRANSIENT,
        failure_summary="a detection dependency was temporarily unavailable",
        retry_policy=_retry_policy(maximum=maximum),
        max_claim_generations=max_generations,
        now=T0,
        db_path=db,
    )
    return comparison_id, outcome


def _claim_due(corpus, db, job_id, *, now):
    inputs = comparison_detector.resolve_detection_inputs(
        comparison_store.get_detection_job(job_id, db)["comparison_id"],
        db_path=db,
        registry_path=corpus.registry,
    )
    return comparison_store.claim_detection_job(
        job_id=job_id,
        worker_id="worker-retry-claim",
        detector_version=comparison_detector.DETECTOR_VERSION,
        workflow_version=comparison_store.WORKFLOW_VERSION,
        previous_source_hash=inputs["previous_hash"],
        current_source_hash=inputs["current_hash"],
        lease_duration_seconds=120,
        reclaim_grace_seconds=15,
        max_claim_generations=3,
        max_retry_attempts=2,
        now=now,
        db_path=db,
    )


def test_policy_defaults_classification_and_validation(tmp_path):
    assert detection_job_retry.POLICY == {
        "policy_id": "detection_job_retry_v1",
        "policy_version": "1",
        "max_retry_attempts": 2,
        "retry_delays_seconds": [5, 30],
        "retryable_failure_codes": [TRANSIENT],
    }
    assert detection_job_retry.classify_failure_code(TRANSIENT) == (
        detection_job_retry.RETRYABLE_TRANSIENT
    )
    assert detection_job_retry.classify_failure_code(
        "detector_internal_error"
    ) == detection_job_retry.UNKNOWN_INTERNAL
    assert not detection_job_retry.is_registry_retryable(
        "detector_internal_error"
    )
    for name, body in {
        "bool.yaml": """
policy_id: x
policy_version: "1"
max_retry_attempts: true
retry_delays_seconds: [5]
retryable_failure_codes: [detection_dependency_unavailable]
""",
        "unknown.yaml": """
policy_id: x
policy_version: "1"
max_retry_attempts: 1
retry_delays_seconds: [5]
retryable_failure_codes: [unknown_transient]
""",
        "domain.yaml": """
policy_id: x
policy_version: "1"
max_retry_attempts: 1
retry_delays_seconds: [5]
retryable_failure_codes: [previous_section_missing]
""",
        "duplicate.yaml": """
policy_id: x
policy_id: y
policy_version: "1"
max_retry_attempts: 0
retry_delays_seconds: []
retryable_failure_codes: []
""",
    }.items():
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        with pytest.raises(GovernancePolicyConfigError):
            detection_job_retry.load_policy(path)


def test_retryable_failure_schedules_without_replacement(corpus, db):
    comparison_id, outcome = _schedule(corpus, db)
    job, attempt = outcome["job"], outcome["attempt"]
    assert outcome["kind"] == "retry_scheduled"
    assert job["status"] == comparison_store.JOB_RETRY_WAIT
    assert job["retry_count"] == 1
    assert job["next_attempt_at"] == (T0 + timedelta(seconds=5)).isoformat()
    assert job["worker_id"] is job["claim_token_hash"] is None
    assert job["lease_started_at"] is job["heartbeat_at"] is None
    assert job["lease_expires_at"] is None
    assert job["last_failure_code"] == TRANSIENT
    assert job["last_failure_classification"] == (
        detection_job_retry.RETRYABLE_TRANSIENT
    )
    assert attempt["status"] == comparison_store.ATTEMPT_FAILED
    assert len(comparison_store.list_detection_attempts(comparison_id, db)) == 1
    assert comparison_store.get_comparison(comparison_id, db)["status"] == (
        comparison_store.STATUS_WAITING_FOR_DETECTION_RETRY
    )


def test_not_due_is_read_only_and_due_boundary_is_inclusive(corpus, db):
    comparison_id, scheduled = _schedule(corpus, db)
    job_id = scheduled["job"]["job_id"]
    before = db.read_bytes()
    assert _claim_due(
        corpus, db, job_id, now=T0 + timedelta(seconds=4, microseconds=999999)
    ) is None
    assert db.read_bytes() == before
    claimed = _claim_due(
        corpus, db, job_id, now=T0 + timedelta(seconds=5)
    )
    assert claimed["kind"] == "retry"
    assert claimed["job"]["claim_generation"] == 2
    assert claimed["job"]["retry_count"] == 1
    assert claimed["job"]["next_attempt_at"] is None
    assert claimed["attempt"]["attempt_number"] == 2
    assert claimed["attempt"]["status"] == comparison_store.ATTEMPT_RUNNING
    assert claimed["source_attempt"]["status"] == comparison_store.ATTEMPT_FAILED
    assert comparison_store.get_comparison(comparison_id, db)["status"] == (
        comparison_store.STATUS_DETECTING
    )


def test_old_claim_is_fenced_after_retry_claim(corpus, db):
    _comparison_id, scheduled = _schedule(corpus, db)
    job_id = scheduled["job"]["job_id"]
    claimed = _claim_due(corpus, db, job_id, now=T0 + timedelta(seconds=5))
    with pytest.raises(comparison_store.DetectionStateError) as exc_info:
        comparison_store.heartbeat_detection_job(
            job_id,
            worker_id="worker-retry-source",
            claim_generation=1,
            claim_token="obsolete-token",
            heartbeat_extension_seconds=120,
            now=T0 + timedelta(seconds=6),
            db_path=db,
        )
    assert exc_info.value.code in comparison_store.JOB_OWNERSHIP_LOST_CODES
    assert claimed["job"]["attempt_id"] == claimed["attempt"]["attempt_id"]


def test_retry_budget_and_generation_budget_exhaust_terminally(corpus, tmp_path):
    for name, maximum, generations, expected in (
        (
            "retry",
            0,
            3,
            comparison_store.REASON_JOB_RETRIES_EXHAUSTED,
        ),
        (
            "generation",
            2,
            1,
            comparison_store.REASON_JOB_EXECUTION_BUDGET_EXHAUSTED,
        ),
    ):
        case_db = tmp_path / f"{name}.db"
        comparison_id, outcome = _schedule(
            corpus,
            case_db,
            maximum=maximum,
            max_generations=generations,
        )
        assert outcome["kind"] == "retry_exhausted"
        assert outcome["attempt"]["failure_code"] == TRANSIENT
        assert outcome["job"]["failure_code"] == expected
        assert outcome["job"]["next_attempt_at"] is None
        assert comparison_store.get_comparison(comparison_id, case_db)[
            "status"
        ] == comparison_store.STATUS_FAILED
        assert len(
            comparison_store.list_detection_attempts(comparison_id, case_db)
        ) == 1


def test_second_retry_uses_second_delay_then_exhausts(corpus, db):
    comparison_id, first = _schedule(corpus, db)
    job_id = first["job"]["job_id"]
    second_claim = _claim_due(
        corpus, db, job_id, now=T0 + timedelta(seconds=5)
    )
    second = comparison_store.fail_detection_job(
        job_id,
        second_claim["attempt"]["attempt_id"],
        worker_id="worker-retry-claim",
        claim_generation=2,
        claim_token=second_claim["claim_token"],
        failure_code=TRANSIENT,
        failure_summary="a detection dependency was temporarily unavailable",
        retry_policy=_retry_policy(),
        max_claim_generations=3,
        now=T0 + timedelta(seconds=5),
        db_path=db,
    )
    assert second["kind"] == "retry_scheduled"
    assert second["job"]["retry_count"] == 2
    assert second["job"]["next_attempt_at"] == (
        T0 + timedelta(seconds=35)
    ).isoformat()
    third_claim = _claim_due(
        corpus, db, job_id, now=T0 + timedelta(seconds=35)
    )
    third = comparison_store.fail_detection_job(
        job_id,
        third_claim["attempt"]["attempt_id"],
        worker_id="worker-retry-claim",
        claim_generation=3,
        claim_token=third_claim["claim_token"],
        failure_code=TRANSIENT,
        failure_summary="a detection dependency was temporarily unavailable",
        retry_policy=_retry_policy(),
        max_claim_generations=3,
        now=T0 + timedelta(seconds=35),
        db_path=db,
    )
    assert third["kind"] == "retry_exhausted"
    assert third["job"]["failure_code"] == (
        comparison_store.REASON_JOB_RETRIES_EXHAUSTED
    )
    assert len(comparison_store.list_detection_attempts(comparison_id, db)) == 3


def test_unknown_failure_is_terminal_without_retry(corpus, db):
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    claimed = _claim(corpus, db, job["job_id"], "worker-unknown")
    outcome = comparison_store.fail_detection_job(
        job["job_id"],
        claimed["attempt"]["attempt_id"],
        worker_id="worker-unknown",
        claim_generation=1,
        claim_token=claimed["claim_token"],
        failure_code="unregistered_failure",
        failure_summary="safe failure",
        retry_policy=_retry_policy(),
        max_claim_generations=3,
        now=T0,
        db_path=db,
    )
    assert outcome["kind"] == "failed"
    assert outcome["job"]["status"] == comparison_store.JOB_FAILED
    assert outcome["job"]["retry_count"] == 0
    assert outcome["job"]["last_failure_classification"] == (
        detection_job_retry.UNKNOWN_INTERNAL
    )


def test_concurrent_due_claim_creates_one_replacement(corpus, db):
    comparison_id, scheduled = _schedule(corpus, db)
    job_id = scheduled["job"]["job_id"]

    def claim(_):
        return _claim_due(corpus, db, job_id, now=T0 + timedelta(seconds=5))

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(claim, range(24)))
    winners = [item for item in outcomes if item is not None]
    assert len(winners) == 1
    attempts = comparison_store.list_detection_attempts(comparison_id, db)
    assert [(item["attempt_number"], item["status"]) for item in attempts] == [
        (1, comparison_store.ATTEMPT_FAILED),
        (2, comparison_store.ATTEMPT_RUNNING),
    ]


def test_worker_schedules_transient_then_executes_due_retry(
    corpus, db, monkeypatch, caplog
):
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    original = comparison_detector._compute_result
    calls = 0

    def once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise comparison_detector.DetectionDependencyUnavailable(
                TRANSIENT, "safe dependency failure"
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(comparison_detector, "_compute_result", once)
    caplog.set_level("INFO", logger="comparison_reliability")
    first = comparison_detection_worker.run_one_job(
        worker_id="worker-transient-one",
        job_id=job["job_id"],
        db_path=db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
        now=T0,
    )
    assert first["job_status"] == comparison_store.JOB_RETRY_WAIT
    assert first["worker_completed_responsibility"] is True
    assert first["claim_type"] == "initial"
    assert calls == 1

    not_due = comparison_detection_worker.run_one_job(
        worker_id="worker-too-early",
        job_id=job["job_id"],
        db_path=db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
        now=T0 + timedelta(seconds=4),
    )
    assert not_due == {"no_job_available": True}
    assert calls == 1

    second = comparison_detection_worker.run_one_job(
        worker_id="worker-transient-two",
        job_id=job["job_id"],
        db_path=db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
        now=T0 + timedelta(seconds=5),
    )
    assert second["job_status"] == comparison_store.JOB_SUCCEEDED
    assert second["claim_type"] == "retry"
    assert second["retry_count"] == 1
    assert calls == 2
    assert len(comparison_store.list_detection_attempts(comparison_id, db)) == 2
    retry_records = [
        record
        for record in caplog.records
        if getattr(record, "event", None)
        in {
            comparison_store.EVENT_JOB_RETRY_SCHEDULED,
            comparison_store.EVENT_JOB_RETRY_CLAIMED,
        }
    ]
    assert [record.event for record in retry_records] == [
        comparison_store.EVENT_JOB_RETRY_SCHEDULED,
        comparison_store.EVENT_JOB_RETRY_CLAIMED,
    ]
    assert all(record.retry_count == 1 for record in retry_records)
    assert all(not hasattr(record, "claim_token") for record in retry_records)
    assert all(not hasattr(record, "claim_token_hash") for record in retry_records)


def test_retry_events_dto_reliability_and_storage_are_sanitized(corpus, db):
    comparison_id, scheduled = _schedule(corpus, db)
    job = scheduled["job"]
    events = comparison_store.list_detection_job_events(job["job_id"], db)
    scheduled_event = events[-1]
    assert scheduled_event["event_type"] == (
        comparison_store.EVENT_JOB_RETRY_SCHEDULED
    )
    assert scheduled_event["retry_count"] == 1
    assert scheduled_event["failure_classification"] == (
        detection_job_retry.RETRYABLE_TRANSIENT
    )
    report = comparison_reliability.summary(
        now=T0,
        db_path=db,
        registry_path=corpus.registry,
    )
    assert report["contract_version"] == "comparison_reliability.v4"
    assert report["gauges"]["detection_jobs_waiting_for_retry"] == 1
    assert report["gauges"]["detection_jobs_retry_not_due"] == 1
    assert report["jobs"]["retries_scheduled"] == 1
    assert report["failure_breakdown"]["retryable_failures_by_code"] == {
        TRANSIENT: 1
    }
    payload = json.dumps({"job": job, "events": events})
    for forbidden in ("raw exception", "SELECT *", "/private/"):
        assert forbidden not in payload
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_api_repeated_detect_returns_retry_wait_and_allowlisted_retry_dto(
    corpus, api_env
):
    comparison_id = _api_create(api_env)
    queued = api_env.client.post(
        f"/api/comparisons/{comparison_id}/detect"
    ).json()
    claimed = _claim(
        corpus, api_env.db, queued["jobId"], "worker-api-retry"
    )
    now = datetime.now(timezone.utc)
    comparison_store.fail_detection_job(
        queued["jobId"],
        claimed["attempt"]["attempt_id"],
        worker_id="worker-api-retry",
        claim_generation=1,
        claim_token=claimed["claim_token"],
        failure_code=TRANSIENT,
        failure_summary="a detection dependency was temporarily unavailable",
        retry_policy=_retry_policy(),
        max_claim_generations=3,
        now=now,
        db_path=api_env.db,
    )
    repeated = api_env.client.post(
        f"/api/comparisons/{comparison_id}/detect"
    )
    assert repeated.status_code == 202
    assert repeated.json()["created"] is False
    assert repeated.json()["jobStatus"] == comparison_store.JOB_RETRY_WAIT
    assert repeated.json()["comparisonStatus"] == (
        comparison_store.STATUS_WAITING_FOR_DETECTION_RETRY
    )
    dto = api_env.client.get(
        f"/api/comparison-detection-jobs/{queued['jobId']}"
    ).json()
    assert dto["retryCount"] == 1
    assert dto["maxRetryAttempts"] == 2
    assert dto["lastFailureCode"] == TRANSIENT
    assert dto["lastFailureClassification"] == (
        detection_job_retry.RETRYABLE_TRANSIENT
    )
    assert dto["retryState"] == "waiting"
    encoded = json.dumps(dto)
    for forbidden in ("claimToken", "claim_token", "claimTokenHash"):
        assert forbidden not in encoded
