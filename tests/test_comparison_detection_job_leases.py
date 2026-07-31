"""Finite detection-job leases, heartbeats, fencing, and explicit reclaim.

All clocks are injected. The suite is credential-free, uses local SQLite, and
never sleeps or starts a polling worker.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api
import comparison_detection_worker
import comparison_detector
import comparison_reliability
import comparison_store
import config
import detection_job_lease
from governance.policy_validation import GovernancePolicyConfigError
from scripts import run_comparison_detection_worker as worker_cli
from tests.auth_helpers import authorization_headers


T0 = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
LEASE = 120
HEARTBEAT = 120
GRACE = 15
MAX_GENERATIONS = 3
PREVIOUS_HASH = "previous-source-hash"
CURRENT_HASH = "current-source-hash"


def _insert_comparison(db: Path, comparison_id: str) -> None:
    comparison_store.init_db(db)
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "INSERT INTO comparisons (comparison_id, schema_version, "
            "workflow_version, previous_filing_id, current_filing_id, "
            "section_scope, status, created_at, updated_at, failure_code, "
            "failure_summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
            (
                comparison_id,
                comparison_store.COMPARISON_SCHEMA_VERSION,
                comparison_store.WORKFLOW_VERSION,
                f"{comparison_id}:previous",
                f"{comparison_id}:current",
                '["item_1a_risk_factors"]',
                comparison_store.STATUS_READY_FOR_DETECTION,
                now,
                now,
            ),
        )


def _enqueue(db: Path, comparison_id: str = "cmp_lease") -> dict:
    _insert_comparison(db, comparison_id)
    return comparison_store.enqueue_detection_job(
        comparison_id,
        detector_version=comparison_detector.DETECTOR_VERSION,
        workflow_version=comparison_store.WORKFLOW_VERSION,
        previous_filing_id=f"{comparison_id}:previous",
        current_filing_id=f"{comparison_id}:current",
        previous_source_hash=PREVIOUS_HASH,
        current_source_hash=CURRENT_HASH,
        requested_by_subject="lease-test@example.local",
        requested_by_auth_method=comparison_store.ACTOR_AUTH_LOCAL_HS256,
        requested_by_token_id=f"jti-{comparison_id}",
        requested_by_policy_id="comparison_access_control_v1",
        requested_by_policy_version="1",
        db_path=db,
    )["job"]


def _claim(
    db: Path,
    job_id: str,
    *,
    worker: str = "worker-one",
    now: datetime = T0,
    previous_hash: str = PREVIOUS_HASH,
    workflow_version: str = comparison_store.WORKFLOW_VERSION,
    max_generations: int = MAX_GENERATIONS,
) -> dict | None:
    return comparison_store.claim_detection_job(
        job_id=job_id,
        worker_id=worker,
        detector_version=comparison_detector.DETECTOR_VERSION,
        workflow_version=workflow_version,
        previous_source_hash=previous_hash,
        current_source_hash=CURRENT_HASH,
        lease_duration_seconds=LEASE,
        reclaim_grace_seconds=GRACE,
        max_claim_generations=max_generations,
        now=now,
        db_path=db,
    )


def _heartbeat(
    db: Path,
    claimed: dict,
    *,
    now: datetime,
    worker: str = "worker-one",
    generation: int | None = None,
    token: str | None = None,
) -> dict:
    return comparison_store.heartbeat_detection_job(
        claimed["job"]["job_id"],
        worker_id=worker,
        claim_generation=(
            claimed["job"]["claim_generation"]
            if generation is None
            else generation
        ),
        claim_token=claimed["claim_token"] if token is None else token,
        heartbeat_extension_seconds=HEARTBEAT,
        now=now,
        db_path=db,
    )


def _fail(
    db: Path,
    claimed: dict,
    *,
    now: datetime,
    worker: str = "worker-one",
    generation: int | None = None,
    token: str | None = None,
) -> dict:
    return comparison_store.fail_detection_job(
        claimed["job"]["job_id"],
        claimed["attempt"]["attempt_id"],
        worker_id=worker,
        claim_generation=(
            claimed["job"]["claim_generation"]
            if generation is None
            else generation
        ),
        claim_token=claimed["claim_token"] if token is None else token,
        failure_code="controlled_detector_failure",
        failure_summary="controlled safe failure",
        now=now,
        db_path=db,
    )


def _complete(db: Path, claimed: dict, *, now: datetime) -> dict:
    result_json = json.dumps(
        {"schema_version": "comparison.v1", "created_at": now.isoformat()},
        sort_keys=True,
    )
    result_hash = hashlib.sha256(
        json.dumps({"schema_version": "comparison.v1"}, sort_keys=True).encode()
    ).hexdigest()
    return comparison_store.complete_detection_job(
        claimed["job"]["job_id"],
        claimed["attempt"]["attempt_id"],
        worker_id="worker-one",
        claim_generation=claimed["job"]["claim_generation"],
        claim_token=claimed["claim_token"],
        result_json=result_json,
        result_hash=result_hash,
        detector_version=comparison_detector.DETECTOR_VERSION,
        workflow_version=comparison_store.WORKFLOW_VERSION,
        previous_source_hash=PREVIOUS_HASH,
        current_source_hash=CURRENT_HASH,
        now=now,
        db_path=db,
    )


def _row_counts(db: Path) -> tuple[int, int]:
    with closing(sqlite3.connect(db)) as conn:
        attempts = conn.execute(
            "SELECT COUNT(*) FROM comparison_detection_attempts"
        ).fetchone()[0]
        events = conn.execute(
            "SELECT COUNT(*) FROM comparison_detection_job_events"
        ).fetchone()[0]
    return attempts, events


def _resolved_inputs(comparison_id: str = "cmp_lease") -> dict:
    return {
        "record": {"comparison_id": comparison_id},
        "previous_ref": {},
        "current_ref": {},
        "previous_entry": {},
        "current_entry": {},
        "previous_hash": PREVIOUS_HASH,
        "current_hash": CURRENT_HASH,
    }


class _HeartbeatStepper:
    """Advance a logical clock at each controller wait without sleeping."""

    def __init__(self, *, steps: int, start: datetime = T0):
        self._current = start
        self._steps = steps
        self._completed_steps = 0
        self._lock = threading.Lock()
        self.ready = threading.Event()

    def clock(self) -> datetime:
        with self._lock:
            return self._current

    def wait(self, stop: threading.Event, interval_seconds: float) -> bool:
        with self._lock:
            if self._completed_steps < self._steps:
                self._current += timedelta(seconds=interval_seconds)
                self._completed_steps += 1
                return False
            self.ready.set()
        return stop.wait(2)


class _ControllerFactory:
    def __init__(self, stepper: _HeartbeatStepper):
        self.stepper = stepper
        self.instance = None
        self.claim_token = None

    def __call__(self, **kwargs):
        self.claim_token = kwargs["claim_token"]
        self.instance = (
            comparison_detection_worker.DetectionJobHeartbeatController(
                **kwargs,
                clock=self.stepper.clock,
                wait=self.stepper.wait,
            )
        )
        return self.instance


def test_policy_defaults_and_fail_fast_validation(tmp_path):
    assert detection_job_lease.POLICY == {
        "policy_id": "detection_job_lease_v1",
        "policy_version": "1",
        "lease_duration_seconds": LEASE,
        "heartbeat_extension_seconds": HEARTBEAT,
        "reclaim_grace_seconds": GRACE,
        "max_claim_generations": MAX_GENERATIONS,
    }
    assert detection_job_lease.load_policy(tmp_path / "missing.yaml") == (
        detection_job_lease.POLICY
    )

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(
        "policy_id: p\npolicy_version: '1'\n"
        "lease_duration_seconds: true\nheartbeat_extension_seconds: 120\n"
        "reclaim_grace_seconds: 15\nmax_claim_generations: 3\n",
        encoding="utf-8",
    )
    with pytest.raises(GovernancePolicyConfigError) as exc:
        detection_job_lease.load_policy(invalid)
    assert "lease_duration_seconds" in str(exc.value)
    assert str(tmp_path) not in str(exc.value)

    invalid.write_text(
        "policy_id: p\npolicy_version: '1'\nlease_duration_seconds: 120\n"
        "heartbeat_extension_seconds: 120\nreclaim_grace_seconds: 15\n"
        "max_claim_generations: 3\nunexpected: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(GovernancePolicyConfigError, match="unknown keys"):
        detection_job_lease.load_policy(invalid)


def test_initial_claim_and_repeated_heartbeats_use_injected_clock(tmp_path):
    db = tmp_path / "lease.db"
    job = _enqueue(db)
    claimed = _claim(db, job["job_id"])
    assert claimed is not None and claimed["kind"] == "claimed"
    stored = claimed["job"]
    assert stored["claim_generation"] == 1
    assert stored["lease_started_at"] == T0.isoformat()
    assert stored["heartbeat_at"] == T0.isoformat()
    assert stored["lease_expires_at"] == (T0 + timedelta(seconds=LEASE)).isoformat()
    assert claimed["claim_token"].encode() not in db.read_bytes()

    first = _heartbeat(db, claimed, now=T0 + timedelta(seconds=60))
    second = _heartbeat(db, claimed, now=T0 + timedelta(seconds=90))
    assert first["lease_expires_at"] == (
        T0 + timedelta(seconds=180)
    ).isoformat()
    assert second["lease_expires_at"] == (
        T0 + timedelta(seconds=210)
    ).isoformat()
    events = comparison_store.list_detection_job_events(job["job_id"], db)
    assert [event["event_type"] for event in events] == [
        comparison_store.EVENT_JOB_QUEUED,
        comparison_store.EVENT_JOB_CLAIMED,
        comparison_store.EVENT_JOB_HEARTBEAT,
        comparison_store.EVENT_JOB_HEARTBEAT,
    ]
    assert [event["event_seq"] for event in events] == [0, 1, 2, 3]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"worker": "worker-other"}, comparison_store.REASON_JOB_WORKER_MISMATCH),
        ({"token": "wrong-token"}, comparison_store.REASON_JOB_CLAIM_INVALID),
        ({"generation": 2}, comparison_store.REASON_JOB_CLAIM_FENCED),
    ],
)
def test_heartbeat_rejects_wrong_ownership_without_mutation(
    tmp_path, overrides, code
):
    db = tmp_path / f"{code}.db"
    claimed = _claim(db, _enqueue(db)["job_id"])
    before = comparison_store.get_detection_job(claimed["job"]["job_id"], db)
    with pytest.raises(comparison_store.DetectionStateError) as exc:
        _heartbeat(db, claimed, now=T0 + timedelta(seconds=1), **overrides)
    assert exc.value.code == code
    assert comparison_store.get_detection_job(claimed["job"]["job_id"], db) == before


def test_expiry_boundary_is_inclusive_for_heartbeat_and_finalization(tmp_path):
    heartbeat_db = tmp_path / "heartbeat-boundary.db"
    heartbeat_claim = _claim(
        heartbeat_db, _enqueue(heartbeat_db)["job_id"]
    )
    at_expiry = T0 + timedelta(seconds=LEASE)
    assert _heartbeat(heartbeat_db, heartbeat_claim, now=at_expiry)[
        "heartbeat_at"
    ] == at_expiry.isoformat()

    success_db = tmp_path / "success-boundary.db"
    success_claim = _claim(success_db, _enqueue(success_db)["job_id"])
    completed = _complete(success_db, success_claim, now=at_expiry)
    assert completed["job"]["status"] == comparison_store.JOB_SUCCEEDED

    expired_db = tmp_path / "expired-boundary.db"
    expired_claim = _claim(expired_db, _enqueue(expired_db)["job_id"])
    before = _row_counts(expired_db)
    with pytest.raises(comparison_store.DetectionStateError) as exc:
        _fail(
            expired_db,
            expired_claim,
            now=at_expiry + timedelta(microseconds=1),
        )
    assert exc.value.code == comparison_store.REASON_JOB_LEASE_EXPIRED
    assert _row_counts(expired_db) == before
    assert comparison_store.get_detection_job(
        expired_claim["job"]["job_id"], expired_db
    )["status"] == comparison_store.JOB_RUNNING


def test_terminal_and_expired_heartbeat_are_rejected(tmp_path):
    expired_db = tmp_path / "expired.db"
    expired = _claim(expired_db, _enqueue(expired_db)["job_id"])
    with pytest.raises(comparison_store.DetectionStateError) as exc:
        _heartbeat(
            expired_db,
            expired,
            now=T0 + timedelta(seconds=LEASE, microseconds=1),
        )
    assert exc.value.code == comparison_store.REASON_JOB_LEASE_EXPIRED

    terminal_db = tmp_path / "terminal.db"
    terminal = _claim(terminal_db, _enqueue(terminal_db)["job_id"])
    _fail(terminal_db, terminal, now=T0 + timedelta(seconds=1))
    with pytest.raises(comparison_store.DetectionStateError) as exc:
        _heartbeat(terminal_db, terminal, now=T0 + timedelta(seconds=2))
    assert exc.value.code == comparison_store.REASON_JOB_NOT_RUNNING


def test_queued_jobs_precede_expired_and_grace_boundary_is_strict(tmp_path):
    db = tmp_path / "selection.db"
    expired_job = _enqueue(db, "cmp_expired")
    _claim(db, expired_job["job_id"])
    queued_job = _enqueue(db, "cmp_queued")
    after_grace = T0 + timedelta(seconds=LEASE + GRACE, microseconds=1)
    assert comparison_store.peek_claimable_detection_job(
        reclaim_grace_seconds=GRACE, now=after_grace, db_path=db
    )["job_id"] == queued_job["job_id"]

    other = tmp_path / "grace.db"
    claimed = _claim(other, _enqueue(other)["job_id"])
    boundary = T0 + timedelta(seconds=LEASE + GRACE)
    assert comparison_store.peek_claimable_detection_job(
        reclaim_grace_seconds=GRACE, now=boundary, db_path=other
    ) is None
    assert comparison_store.peek_claimable_detection_job(
        reclaim_grace_seconds=GRACE,
        now=boundary + timedelta(microseconds=1),
        db_path=other,
    )["job_id"] == claimed["job"]["job_id"]


def test_reclaim_retires_attempt_replaces_token_and_fences_old_worker(tmp_path):
    db = tmp_path / "reclaim.db"
    first = _claim(db, _enqueue(db)["job_id"])
    reclaim_at = T0 + timedelta(seconds=LEASE + GRACE + 1)
    second = _claim(
        db,
        first["job"]["job_id"],
        worker="worker-two",
        now=reclaim_at,
    )
    assert second is not None and second["kind"] == "reclaimed"
    assert second["job"]["status"] == comparison_store.JOB_RUNNING
    assert second["job"]["claim_generation"] == 2
    assert second["attempt"]["attempt_number"] == 2
    assert second["claim_token"] != first["claim_token"]
    assert second["source_attempt"]["status"] == comparison_store.ATTEMPT_TIMED_OUT
    assert second["source_attempt"]["failure_code"] == (
        comparison_store.FAILURE_ATTEMPT_WORKER_LEASE_EXPIRED
    )
    assert comparison_store.get_comparison(
        second["job"]["comparison_id"], db
    )["status"] == comparison_store.STATUS_DETECTING

    for operation in (
        lambda: _heartbeat(db, first, now=reclaim_at),
        lambda: _fail(db, first, now=reclaim_at),
        lambda: _complete(db, first, now=reclaim_at),
    ):
        with pytest.raises(comparison_store.DetectionStateError) as exc:
            operation()
        assert exc.value.code == comparison_store.REASON_JOB_CLAIM_FENCED
    active = comparison_store.get_detection_attempt(
        second["attempt"]["attempt_id"], db
    )
    assert active["status"] == comparison_store.ATTEMPT_RUNNING


def test_concurrent_reclaim_creates_one_replacement_and_no_generation_gap(tmp_path):
    for iteration in range(8):
        db = tmp_path / f"concurrent-{iteration}.db"
        first = _claim(db, _enqueue(db)["job_id"])
        now = T0 + timedelta(seconds=LEASE + GRACE + 1)

        def reclaim(worker: str):
            return _claim(db, first["job"]["job_id"], worker=worker, now=now)

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(
                pool.map(reclaim, [f"worker-{i}" for i in range(8)])
            )
        winners = [outcome for outcome in outcomes if outcome is not None]
        assert len(winners) == 1
        assert winners[0]["kind"] == "reclaimed"
        assert winners[0]["job"]["claim_generation"] == 2
        with closing(sqlite3.connect(db)) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM comparison_detection_attempts"
            ).fetchone()[0] == 2
            assert conn.execute(
                "SELECT COUNT(*) FROM comparison_detection_job_events "
                "WHERE event_type = ?",
                (comparison_store.EVENT_JOB_RECLAIMED,),
            ).fetchone()[0] == 1


def test_reclaim_rolls_back_as_one_unit(tmp_path, monkeypatch):
    db = tmp_path / "rollback.db"
    first = _claim(db, _enqueue(db)["job_id"])
    before_job = comparison_store.get_detection_job(first["job"]["job_id"], db)
    before_attempt = comparison_store.get_detection_attempt(
        first["attempt"]["attempt_id"], db
    )
    before_counts = _row_counts(db)
    original = comparison_store._insert_detection_job_event

    def fail_reclaim_event(conn, **kwargs):
        if kwargs["event_type"] == comparison_store.EVENT_JOB_RECLAIMED:
            raise RuntimeError("controlled rollback")
        return original(conn, **kwargs)

    monkeypatch.setattr(
        comparison_store, "_insert_detection_job_event", fail_reclaim_event
    )
    with pytest.raises(RuntimeError, match="controlled rollback"):
        _claim(
            db,
            first["job"]["job_id"],
            worker="worker-two",
            now=T0 + timedelta(seconds=LEASE + GRACE + 1),
        )
    assert comparison_store.get_detection_job(first["job"]["job_id"], db) == (
        before_job
    )
    assert comparison_store.get_detection_attempt(
        first["attempt"]["attempt_id"], db
    ) == before_attempt
    assert _row_counts(db) == before_counts


def test_reclaimed_work_can_expire_and_be_reclaimed_again_after_reopen(tmp_path):
    db = tmp_path / "reopen.db"
    first = _claim(db, _enqueue(db)["job_id"])
    second_time = T0 + timedelta(seconds=LEASE + GRACE + 1)
    second = _claim(
        db, first["job"]["job_id"], worker="worker-two", now=second_time
    )
    comparison_store.init_db(db)
    persisted = comparison_store.get_detection_job(first["job"]["job_id"], db)
    assert persisted["claim_generation"] == 2
    third = _claim(
        db,
        first["job"]["job_id"],
        worker="worker-three",
        now=second_time + timedelta(seconds=LEASE + GRACE + 1),
    )
    assert third["kind"] == "reclaimed"
    assert third["job"]["claim_generation"] == 3
    assert third["attempt"]["attempt_number"] == 3
    assert len(comparison_store.list_detection_job_events(
        first["job"]["job_id"], db
    )) == 4


def test_claim_exhaustion_is_terminal_and_creates_no_replacement(tmp_path):
    db = tmp_path / "exhausted.db"
    first = _claim(
        db, _enqueue(db)["job_id"], max_generations=1
    )
    outcome = _claim(
        db,
        first["job"]["job_id"],
        worker="worker-two",
        now=T0 + timedelta(seconds=LEASE + GRACE + 1),
        max_generations=1,
    )
    assert outcome["kind"] == "exhausted"
    assert outcome["job"]["status"] == comparison_store.JOB_FAILED
    assert outcome["job"]["failure_code"] == (
        comparison_store.REASON_JOB_CLAIMS_EXHAUSTED
    )
    assert outcome["attempt"]["status"] == comparison_store.ATTEMPT_TIMED_OUT
    assert _row_counts(db)[0] == 1
    assert comparison_store.get_comparison(
        outcome["job"]["comparison_id"], db
    )["status"] == comparison_store.STATUS_FAILED
    assert [event["event_type"] for event in
            comparison_store.list_detection_job_events(
                first["job"]["job_id"], db
            )][-2:] == [
        comparison_store.EVENT_JOB_CLAIM_EXHAUSTED,
        comparison_store.EVENT_JOB_FAILED,
    ]
    report = comparison_reliability.summary(
        now=T0 + timedelta(seconds=LEASE + GRACE + 2), db_path=db
    )
    issues = comparison_reliability.issues(
        now=T0 + timedelta(seconds=LEASE + GRACE + 2), db_path=db
    )
    assert report["gauges"]["claim_exhausted_jobs"] == 1
    assert report["jobs"]["jobs_claim_exhausted"] == 1
    assert issues["issues"][0]["issue_type"] == (
        comparison_reliability.ISSUE_JOB_CLAIMS_EXHAUSTED
    )


@pytest.mark.parametrize(
    ("overrides", "failure_code"),
    [
        ({"previous_hash": "changed"}, comparison_store.REASON_JOB_INPUTS_CHANGED),
        (
            {"workflow_version": "comparison_workflow.changed"},
            comparison_store.REASON_JOB_VERSION_CHANGED,
        ),
    ],
)
def test_reclaim_source_or_version_drift_fails_without_replacement(
    tmp_path, overrides, failure_code
):
    db = tmp_path / f"{failure_code}.db"
    first = _claim(db, _enqueue(db)["job_id"])
    outcome = _claim(
        db,
        first["job"]["job_id"],
        worker="worker-two",
        now=T0 + timedelta(seconds=LEASE + GRACE + 1),
        **overrides,
    )
    assert outcome["kind"] == "failed"
    assert outcome["job"]["failure_code"] == failure_code
    assert outcome["attempt"]["status"] == comparison_store.ATTEMPT_TIMED_OUT
    assert _row_counts(db)[0] == 1


def test_worker_executes_reclaimed_attempt_once_and_skips_active_claim(
    tmp_path, monkeypatch, caplog
):
    db = tmp_path / "worker.db"
    first = _claim(db, _enqueue(db)["job_id"])
    executed: list[str] = []

    monkeypatch.setattr(
        comparison_detector,
        "resolve_detection_inputs",
        lambda *args, **kwargs: {
            "record": {"comparison_id": "cmp_lease"},
            "previous_ref": {},
            "current_ref": {},
            "previous_entry": {},
            "current_entry": {},
            "previous_hash": PREVIOUS_HASH,
            "current_hash": CURRENT_HASH,
        },
    )

    def execute(attempt_id, **kwargs):
        executed.append(attempt_id)
        comparison_store.fail_detection_job(
            kwargs["job_id"],
            attempt_id,
            worker_id=kwargs["worker_id"],
            claim_generation=kwargs["claim_generation"],
            claim_token=kwargs["claim_token"],
            failure_code="controlled_detector_failure",
            failure_summary="controlled safe failure",
            now=kwargs["job_now"],
            db_path=kwargs["db_path"],
        )
        raise comparison_detector.DetectionError(
            "controlled_detector_failure", "controlled"
        )

    monkeypatch.setattr(comparison_detector, "execute_attempt", execute)
    active = comparison_detection_worker.run_one_job(
        worker_id="worker-two",
        db_path=db,
        now=T0 + timedelta(seconds=1),
    )
    assert active == {"no_job_available": True}
    assert executed == []

    with caplog.at_level(logging.INFO):
        outcome = comparison_detection_worker.run_one_job(
            worker_id="worker-two",
            db_path=db,
            now=T0 + timedelta(seconds=LEASE + GRACE + 1),
        )
    assert outcome["job_status"] == comparison_store.JOB_FAILED
    assert outcome["claim_generation"] == 2
    assert len(executed) == 1
    assert executed[0] != first["attempt"]["attempt_id"]
    assert any(
        getattr(record, "event", None)
        == comparison_reliability.EVENT_JOB_RECLAIMED
        for record in caplog.records
    )


def test_worker_heartbeats_one_long_execution_and_stops_after_success(
    tmp_path, monkeypatch, caplog
):
    db = tmp_path / "worker-long-success.db"
    first_job = _enqueue(db, "cmp_long")
    second_job = _enqueue(db, "cmp_waiting")
    monkeypatch.setattr(
        comparison_detector,
        "resolve_detection_inputs",
        lambda comparison_id, **kwargs: _resolved_inputs(comparison_id),
    )
    stepper = _HeartbeatStepper(steps=5)
    factory = _ControllerFactory(stepper)

    def execute(attempt_id, **kwargs):
        assert stepper.ready.wait(2)
        completed_at = stepper.clock()
        assert completed_at > T0 + timedelta(seconds=LEASE)
        result_json = json.dumps(
            {"schema_version": "comparison.v1", "created_at": completed_at.isoformat()}
        )
        result_hash = comparison_store._canonical_result_hash(result_json)
        comparison_store.complete_detection_job(
            kwargs["job_id"],
            attempt_id,
            worker_id=kwargs["worker_id"],
            claim_generation=kwargs["claim_generation"],
            claim_token=kwargs["claim_token"],
            result_json=result_json,
            result_hash=result_hash,
            detector_version=comparison_detector.DETECTOR_VERSION,
            workflow_version=comparison_store.WORKFLOW_VERSION,
            previous_source_hash=PREVIOUS_HASH,
            current_source_hash=CURRENT_HASH,
            now=completed_at,
            db_path=kwargs["db_path"],
        )
        return {"schema_version": "comparison.v1"}, True, attempt_id

    monkeypatch.setattr(comparison_detector, "execute_attempt", execute)
    with caplog.at_level(logging.INFO):
        outcome = comparison_detection_worker.run_one_job(
            worker_id="worker-one",
            db_path=db,
            policy=detection_job_lease.POLICY,
            now=T0,
            heartbeat_controller_factory=factory,
        )

    assert outcome["job_id"] == first_job["job_id"]
    assert outcome["job_status"] == comparison_store.JOB_SUCCEEDED
    assert comparison_store.get_detection_job(
        second_job["job_id"], db
    )["status"] == comparison_store.JOB_QUEUED
    events = comparison_store.list_detection_job_events(
        first_job["job_id"], db
    )
    heartbeats = [
        event
        for event in events
        if event["event_type"] == comparison_store.EVENT_JOB_HEARTBEAT
    ]
    assert len(heartbeats) == 5
    assert [event["event_seq"] for event in heartbeats] == sorted(
        event["event_seq"] for event in heartbeats
    )
    assert [event["created_at"] for event in heartbeats] == sorted(
        event["created_at"] for event in heartbeats
    )
    assert factory.instance.interval_seconds == 30.0
    assert factory.instance.running is False
    assert factory.instance.thread_name == "detection-job-heartbeat"
    token = factory.claim_token
    assert token not in caplog.text
    assert token not in repr(outcome)
    assert token.encode() not in db.read_bytes()
    assert token not in factory.instance.thread_name
    assert all(
        token not in thread.name for thread in threading.enumerate()
    )


def test_worker_heartbeat_controller_stops_after_failed_finalization(
    tmp_path, monkeypatch
):
    db = tmp_path / "worker-long-failure.db"
    _enqueue(db, "cmp_failure")
    monkeypatch.setattr(
        comparison_detector,
        "resolve_detection_inputs",
        lambda comparison_id, **kwargs: _resolved_inputs(comparison_id),
    )
    stepper = _HeartbeatStepper(steps=1)
    factory = _ControllerFactory(stepper)

    def execute(attempt_id, **kwargs):
        assert stepper.ready.wait(2)
        comparison_store.fail_detection_job(
            kwargs["job_id"],
            attempt_id,
            worker_id=kwargs["worker_id"],
            claim_generation=kwargs["claim_generation"],
            claim_token=kwargs["claim_token"],
            failure_code="controlled_detector_failure",
            failure_summary="controlled safe failure",
            now=stepper.clock(),
            db_path=kwargs["db_path"],
        )
        raise comparison_detector.DetectionError(
            "controlled_detector_failure", "controlled"
        )

    monkeypatch.setattr(comparison_detector, "execute_attempt", execute)
    outcome = comparison_detection_worker.run_one_job(
        worker_id="worker-one",
        db_path=db,
        now=T0,
        heartbeat_controller_factory=factory,
    )
    assert outcome["job_status"] == comparison_store.JOB_FAILED
    assert factory.instance.running is False


def test_reclaim_fences_old_worker_controller_and_finalization(
    tmp_path, monkeypatch, caplog
):
    db = tmp_path / "worker-controller-fenced.db"
    _enqueue(db, "cmp_fenced")
    monkeypatch.setattr(
        comparison_detector,
        "resolve_detection_inputs",
        lambda comparison_id, **kwargs: _resolved_inputs(comparison_id),
    )
    reclaim_at = T0 + timedelta(seconds=LEASE + GRACE + 1)
    waiting = threading.Event()
    wake = threading.Event()
    replacement = {}

    class ReclaimFactory:
        instance = None
        claim_token = None

        def __call__(self, **kwargs):
            self.claim_token = kwargs["claim_token"]

            def wait(stop, interval_seconds):
                waiting.set()
                wake.wait(2)
                return stop.is_set()

            self.instance = (
                comparison_detection_worker.DetectionJobHeartbeatController(
                    **kwargs,
                    clock=lambda: reclaim_at,
                    wait=wait,
                )
            )
            return self.instance

    factory = ReclaimFactory()

    def execute(attempt_id, **kwargs):
        assert waiting.wait(2)
        replacement["claim"] = _claim(
            db,
            kwargs["job_id"],
            worker="worker-two",
            now=reclaim_at,
        )
        wake.set()
        assert factory.instance.wait_for_ownership_loss(2)
        result_json = json.dumps({"schema_version": "comparison.v1"})
        comparison_store.complete_detection_job(
            kwargs["job_id"],
            attempt_id,
            worker_id=kwargs["worker_id"],
            claim_generation=kwargs["claim_generation"],
            claim_token=kwargs["claim_token"],
            result_json=result_json,
            result_hash=hashlib.sha256(result_json.encode()).hexdigest(),
            detector_version=comparison_detector.DETECTOR_VERSION,
            workflow_version=comparison_store.WORKFLOW_VERSION,
            previous_source_hash=PREVIOUS_HASH,
            current_source_hash=CURRENT_HASH,
            now=reclaim_at,
            db_path=kwargs["db_path"],
        )

    monkeypatch.setattr(comparison_detector, "execute_attempt", execute)
    with caplog.at_level(logging.INFO):
        outcome = comparison_detection_worker.run_one_job(
            worker_id="worker-one",
            db_path=db,
            now=T0,
            heartbeat_controller_factory=factory,
        )
    active = replacement["claim"]
    assert active["kind"] == "reclaimed"
    assert outcome["ownership_lost"] is True
    assert outcome["failure_code"] == comparison_store.REASON_JOB_CLAIM_FENCED
    assert outcome["claim_generation"] == 2
    assert comparison_store.get_detection_attempt(
        active["attempt"]["attempt_id"], db
    )["status"] == comparison_store.ATTEMPT_RUNNING
    assert factory.instance.ownership_lost_code == (
        comparison_store.REASON_JOB_CLAIM_FENCED
    )
    assert factory.instance.running is False
    token = factory.claim_token
    assert token not in caplog.text
    with pytest.raises(comparison_store.DetectionStateError) as exc:
        comparison_store.heartbeat_detection_job(
            active["job"]["job_id"],
            worker_id="worker-one",
            claim_generation=1,
            claim_token=token,
            heartbeat_extension_seconds=HEARTBEAT,
            now=reclaim_at,
            db_path=db,
        )
    assert exc.value.code == comparison_store.REASON_JOB_CLAIM_FENCED
    assert token not in str(exc.value)


def test_stopped_controller_models_crash_and_job_becomes_reclaimable(
    tmp_path
):
    db = tmp_path / "worker-controller-crash.db"
    claimed = _claim(db, _enqueue(db, "cmp_crash")["job_id"])
    stepper = _HeartbeatStepper(steps=2)
    controller = (
        comparison_detection_worker.DetectionJobHeartbeatController(
            job_id=claimed["job"]["job_id"],
            worker_id="worker-one",
            claim_generation=1,
            claim_token=claimed["claim_token"],
            db_path=db,
            policy=detection_job_lease.POLICY,
            clock=stepper.clock,
            wait=stepper.wait,
        )
    )
    controller.start()
    assert stepper.ready.wait(2)
    controller.stop()
    before = comparison_store.list_detection_job_events(
        claimed["job"]["job_id"], db
    )
    assert len(
        [
            event
            for event in before
            if event["event_type"] == comparison_store.EVENT_JOB_HEARTBEAT
        ]
    ) == 2
    assert controller.running is False
    latest = comparison_store.get_detection_job(
        claimed["job"]["job_id"], db
    )
    reclaim_at = (
        comparison_store.parse_utc_timestamp(latest["lease_expires_at"])
        + timedelta(seconds=GRACE + 1)
    )
    reclaimed = _claim(
        db,
        claimed["job"]["job_id"],
        worker="worker-two",
        now=reclaim_at,
    )
    assert reclaimed["kind"] == "reclaimed"
    assert reclaimed["job"]["claim_generation"] == 2


def test_transient_heartbeat_fault_is_recorded_and_finalization_revalidates(
    tmp_path, monkeypatch, caplog
):
    db = tmp_path / "worker-controller-fault.db"
    claimed = _claim(db, _enqueue(db, "cmp_fault")["job_id"])
    stepper = _HeartbeatStepper(steps=2)
    actual_heartbeat = comparison_detection_worker.heartbeat_job
    calls = 0

    def flaky_heartbeat(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError(
                "TOP-SECRET raw infrastructure diagnostic"
            )
        return actual_heartbeat(**kwargs)

    monkeypatch.setattr(
        comparison_detection_worker, "heartbeat_job", flaky_heartbeat
    )
    controller = (
        comparison_detection_worker.DetectionJobHeartbeatController(
            job_id=claimed["job"]["job_id"],
            worker_id="worker-one",
            claim_generation=1,
            claim_token=claimed["claim_token"],
            db_path=db,
            policy=detection_job_lease.POLICY,
            clock=stepper.clock,
            wait=stepper.wait,
        )
    )
    with caplog.at_level(logging.WARNING):
        controller.start()
        assert stepper.ready.wait(2)
        controller.stop()
    assert controller.heartbeat_fault_code == (
        comparison_detection_worker.HEARTBEAT_FAULT_CODE
    )
    assert "TOP-SECRET" not in caplog.text
    assert claimed["claim_token"] not in caplog.text
    heartbeats = [
        event
        for event in comparison_store.list_detection_job_events(
            claimed["job"]["job_id"], db
        )
        if event["event_type"] == comparison_store.EVENT_JOB_HEARTBEAT
    ]
    assert len(heartbeats) == 1
    terminal = _complete(db, claimed, now=stepper.clock())
    assert terminal["job"]["status"] == comparison_store.JOB_SUCCEEDED


def test_heartbeat_controller_cadence_is_bounded_and_never_selects_work():
    assert detection_job_lease.heartbeat_interval_seconds(
        {
            **detection_job_lease.POLICY,
            "lease_duration_seconds": 1,
            "heartbeat_extension_seconds": 1,
        }
    ) == pytest.approx(1 / 3)
    assert detection_job_lease.heartbeat_interval_seconds(
        {
            **detection_job_lease.POLICY,
            "lease_duration_seconds": 3600,
            "heartbeat_extension_seconds": 3600,
        }
    ) == detection_job_lease.MAX_HEARTBEAT_INTERVAL_SECONDS
    source = inspect.getsource(
        comparison_detection_worker.DetectionJobHeartbeatController
    )
    assert "Event.wait" in source
    assert "time.sleep" not in source
    assert "while True" not in source
    for forbidden in (
        "peek_claimable_detection_job",
        "claim_detection_job",
        "execute_attempt",
        "run_one_job",
    ):
        assert forbidden not in source


def test_worker_claim_exhaustion_logs_and_runs_no_detector(
    tmp_path, monkeypatch, caplog
):
    db = tmp_path / "worker-exhausted.db"
    first = _claim(
        db, _enqueue(db)["job_id"], max_generations=1
    )
    detector_calls: list[str] = []
    monkeypatch.setattr(
        comparison_detector,
        "resolve_detection_inputs",
        lambda *args, **kwargs: {
            "record": {"comparison_id": "cmp_lease"},
            "previous_ref": {},
            "current_ref": {},
            "previous_entry": {},
            "current_entry": {},
            "previous_hash": PREVIOUS_HASH,
            "current_hash": CURRENT_HASH,
        },
    )
    monkeypatch.setattr(
        comparison_detector,
        "execute_attempt",
        lambda attempt_id, **kwargs: detector_calls.append(attempt_id),
    )
    policy = {**detection_job_lease.POLICY, "max_claim_generations": 1}
    with caplog.at_level(logging.INFO):
        outcome = comparison_detection_worker.run_one_job(
            worker_id="worker-two",
            db_path=db,
            policy=policy,
            now=T0 + timedelta(seconds=LEASE + GRACE + 1),
        )
    assert outcome["job_id"] == first["job"]["job_id"]
    assert outcome["job_status"] == comparison_store.JOB_FAILED
    assert outcome["failure_code"] == comparison_store.REASON_JOB_CLAIMS_EXHAUSTED
    assert detector_calls == []
    assert any(
        getattr(record, "event", None)
        == comparison_reliability.EVENT_JOB_CLAIM_EXHAUSTED
        for record in caplog.records
    )


def test_detector_logs_fenced_success_without_mutating_replacement(
    tmp_path, monkeypatch, caplog
):
    db = tmp_path / "finalize-rejected.db"
    first = _claim(db, _enqueue(db)["job_id"])
    reclaim_at = T0 + timedelta(seconds=LEASE + GRACE + 1)
    replacement: dict[str, dict] = {}

    def compute(*args, **kwargs):
        replacement["outcome"] = _claim(
            db,
            first["job"]["job_id"],
            worker="worker-two",
            now=reclaim_at,
        )
        return {"schema_version": "comparison.v1"}

    monkeypatch.setattr(comparison_detector, "_compute_result", compute)
    with caplog.at_level(logging.INFO):
        with pytest.raises(comparison_store.DetectionStateError) as exc:
            comparison_detector.execute_attempt(
                first["attempt"]["attempt_id"],
                record={
                    "comparison_id": first["job"]["comparison_id"],
                    "schema_version": comparison_store.COMPARISON_SCHEMA_VERSION,
                },
                previous_ref={},
                current_ref={},
                previous_entry={},
                current_entry={},
                previous_hash=PREVIOUS_HASH,
                current_hash=CURRENT_HASH,
                db_path=db,
                job_id=first["job"]["job_id"],
                worker_id="worker-one",
                claim_generation=1,
                claim_token=first["claim_token"],
                job_now=reclaim_at,
            )
    assert exc.value.code == comparison_store.REASON_JOB_CLAIM_FENCED
    active = replacement["outcome"]["attempt"]
    assert comparison_store.get_detection_attempt(
        active["attempt_id"], db
    )["status"] == comparison_store.ATTEMPT_RUNNING
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None)
        == comparison_reliability.EVENT_JOB_FINALIZE_REJECTED
    ]
    assert len(records) == 1
    assert records[0].failure_code == comparison_store.REASON_JOB_CLAIM_FENCED
    assert records[0].source_attempt_id == first["attempt"]["attempt_id"]
    assert records[0].replacement_attempt_id == active["attempt_id"]


def test_cli_heartbeat_uses_environment_and_never_prints_token(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "cli.db"
    job = _enqueue(db)
    claimed = _claim(
        db, job["job_id"], now=datetime.now(timezone.utc)
    )
    token = claimed["claim_token"]
    monkeypatch.setenv("LEASE_TEST_CLAIM_TOKEN", token)
    status = worker_cli.main(
        [
            "--db-path",
            str(db),
            "--worker-id",
            "worker-one",
            "--heartbeat-job-id",
            job["job_id"],
            "--claim-generation",
            "1",
            "--claim-token-env",
            "LEASE_TEST_CLAIM_TOKEN",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert status == 0
    assert json.loads(captured.out)["heartbeatAccepted"] is True
    assert token not in captured.out + captured.err
    assert worker_cli.main(
        [
            "--db-path",
            str(db),
            "--worker-id",
            "worker-one",
            "--heartbeat-job-id",
            job["job_id"],
            "--claim-generation",
            "0",
            "--claim-token-env",
            "LEASE_TEST_CLAIM_TOKEN",
        ]
    ) == 2


def test_api_and_reliability_expose_lease_metadata_without_claim_material(
    tmp_path, monkeypatch
):
    db = tmp_path / "visibility.db"
    claimed = _claim(db, _enqueue(db)["job_id"])
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(db))
    client = TestClient(api.app, headers=authorization_headers())
    response = client.get(
        f"/api/comparison-detection-jobs/{claimed['job']['job_id']}"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["claimGeneration"] == 1
    assert body["leaseStartedAt"] == T0.isoformat()
    assert body["heartbeatAt"] == T0.isoformat()
    assert body["leaseExpiresAt"] == (
        T0 + timedelta(seconds=LEASE)
    ).isoformat()
    assert body["leaseState"] == "active"
    assert claimed["claim_token"] not in response.text
    assert "claimToken" not in response.text

    expired_at = T0 + timedelta(seconds=LEASE + GRACE + 1)
    before = db.read_bytes()
    report = comparison_reliability.summary(now=expired_at, db_path=db)
    issues = comparison_reliability.issues(now=expired_at, db_path=db)
    assert report["gauges"]["active_job_leases"] == 0
    assert report["gauges"]["expired_job_leases"] == 1
    assert report["gauges"]["reclaimable_jobs"] == 1
    assert report["jobs"]["jobs_reclaimed"] == 0
    assert issues["issues"][0]["issue_type"] == (
        comparison_reliability.ISSUE_EXPIRED_DETECTION_JOB_LEASE
    )
    assert db.read_bytes() == before


def test_structured_heartbeat_log_is_allowlisted_and_secret_free(
    tmp_path, caplog
):
    db = tmp_path / "logging.db"
    claimed = _claim(db, _enqueue(db)["job_id"])
    with caplog.at_level(logging.INFO):
        outcome = comparison_detection_worker.heartbeat_job(
            job_id=claimed["job"]["job_id"],
            worker_id="worker-one",
            claim_generation=1,
            claim_token=claimed["claim_token"],
            now=T0 + timedelta(seconds=1),
            db_path=db,
        )
    assert outcome["heartbeat_accepted"] is True
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None)
        == comparison_reliability.EVENT_JOB_HEARTBEAT
    ]
    assert len(records) == 1
    record = records[0]
    assert record.claim_generation == 1
    assert record.worker_id == "worker-one"
    assert claimed["claim_token"] not in caplog.text
    assert not hasattr(record, "claim_token")
    assert not hasattr(record, "claim_token_hash")


def test_negative_lease_duration_is_visible_without_reliability_mutation(tmp_path):
    db = tmp_path / "negative-lease.db"
    claimed = _claim(db, _enqueue(db)["job_id"])
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            "UPDATE comparison_detection_jobs SET lease_expires_at = ? "
            "WHERE job_id = ?",
            (
                (T0 - timedelta(seconds=1)).isoformat(),
                claimed["job"]["job_id"],
            ),
        )
    before = db.read_bytes()
    report = comparison_reliability.summary(now=T0, db_path=db)
    issues = comparison_reliability.issues(now=T0, db_path=db)
    assert report["job_durations"]["negative_lease_duration_jobs"] == 1
    assert any(
        issue["issue_type"]
        == comparison_reliability.ISSUE_INVALID_NEGATIVE_LEASE_DURATION
        for issue in issues["issues"]
    )
    assert db.read_bytes() == before


def test_integrity_foreign_keys_and_no_background_mechanism(tmp_path):
    db = tmp_path / "integrity.db"
    _claim(db, _enqueue(db)["job_id"])
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    root = Path(__file__).resolve().parent.parent
    source = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in (
            "comparison_detection_worker.py",
            "detection_job_lease.py",
            "scripts/run_comparison_detection_worker.py",
        )
    )
    for forbidden in (
        "time.sleep(",
        "while True",
        "celery",
        "rabbitmq",
        "kafka",
        "boto3",
        "redis",
        "next_attempt_at",
    ):
        assert forbidden not in source.lower()


def test_prelease_migration_is_honest_idempotent_and_concurrent_safe(tmp_path):
    db = tmp_path / "prelease.db"
    comparison_store.init_db(db)
    stamp = "2029-01-01T00:00:00+00:00"
    token_hash = "a" * 64
    with closing(sqlite3.connect(db)) as conn, conn:
        for comparison_id, status in (
            ("cmp_q", comparison_store.STATUS_QUEUED_FOR_DETECTION),
            ("cmp_r", comparison_store.STATUS_DETECTING),
            ("cmp_t", comparison_store.STATUS_DETECTED),
        ):
            conn.execute(
                "INSERT INTO comparisons VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "NULL, NULL)",
                (
                    comparison_id,
                    comparison_store.COMPARISON_SCHEMA_VERSION,
                    comparison_store.WORKFLOW_VERSION,
                    f"{comparison_id}:previous",
                    f"{comparison_id}:current",
                    '["item_1a_risk_factors"]',
                    status,
                    stamp,
                    stamp,
                ),
            )
        for attempt_id, comparison_id, status, result_hash in (
            ("att_r", "cmp_r", comparison_store.ATTEMPT_RUNNING, None),
            ("att_t", "cmp_t", comparison_store.ATTEMPT_SUCCEEDED, "result-t"),
        ):
            conn.execute(
                "INSERT INTO comparison_detection_attempts VALUES "
                "(?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    attempt_id,
                    comparison_id,
                    status,
                    comparison_detector.DETECTOR_VERSION,
                    comparison_store.WORKFLOW_VERSION,
                    PREVIOUS_HASH,
                    CURRENT_HASH,
                    stamp,
                    None if status == comparison_store.ATTEMPT_RUNNING else stamp,
                    result_hash,
                ),
            )
        conn.executescript(
            """
            DROP TABLE comparison_detection_job_events;
            DROP TABLE comparison_detection_jobs;
            CREATE TABLE comparison_detection_jobs (
                job_id TEXT PRIMARY KEY, comparison_id TEXT NOT NULL,
                attempt_id TEXT, trigger_type TEXT NOT NULL, status TEXT NOT NULL,
                request_hash TEXT NOT NULL, detector_version TEXT NOT NULL,
                workflow_version TEXT NOT NULL, previous_source_hash TEXT NOT NULL,
                current_source_hash TEXT NOT NULL, requested_by_subject TEXT NOT NULL,
                requested_by_auth_method TEXT NOT NULL,
                requested_by_token_id TEXT NOT NULL,
                requested_by_policy_id TEXT NOT NULL,
                requested_by_policy_version TEXT NOT NULL, queued_at TEXT NOT NULL,
                claimed_at TEXT, finished_at TEXT, worker_id TEXT,
                claim_token_hash TEXT, result_hash TEXT, failure_code TEXT,
                failure_summary TEXT
            );
            CREATE TABLE comparison_detection_job_events (
                event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                comparison_id TEXT NOT NULL, attempt_id TEXT,
                event_type TEXT NOT NULL, event_seq INTEGER NOT NULL,
                created_at TEXT NOT NULL, worker_id TEXT, result_hash TEXT,
                failure_code TEXT
            );
            """
        )
        shared = (
            comparison_store.JOB_TRIGGER_INITIAL_DETECTION,
            "b" * 64,
            comparison_detector.DETECTOR_VERSION,
            comparison_store.WORKFLOW_VERSION,
            PREVIOUS_HASH,
            CURRENT_HASH,
            "legacy@example.local",
            comparison_store.ACTOR_AUTH_LOCAL_HS256,
            "legacy-jti",
            "comparison_access_control_v1",
            "1",
            stamp,
        )
        conn.execute(
            "INSERT INTO comparison_detection_jobs VALUES "
            "(?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
            (
                "job_q",
                "cmp_q",
                shared[0],
                comparison_store.JOB_QUEUED,
                *shared[1:],
            ),
        )
        conn.execute(
            "INSERT INTO comparison_detection_jobs VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "NULL, ?, ?, NULL, NULL, NULL)",
            (
                "job_r",
                "cmp_r",
                "att_r",
                shared[0],
                comparison_store.JOB_RUNNING,
                *shared[1:],
                stamp,
                "legacy-worker",
                token_hash,
            ),
        )
        conn.execute(
            "INSERT INTO comparison_detection_jobs VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, NULL, NULL)",
            (
                "job_t",
                "cmp_t",
                "att_t",
                shared[0],
                comparison_store.JOB_SUCCEEDED,
                *shared[1:],
                stamp,
                stamp,
                "legacy-worker",
                token_hash,
                "result-t",
            ),
        )
        for values in (
            (
                "evt_q",
                "job_q",
                "cmp_q",
                None,
                comparison_store.EVENT_JOB_QUEUED,
                0,
                stamp,
                None,
                None,
                None,
            ),
            (
                "evt_r",
                "job_r",
                "cmp_r",
                "att_r",
                comparison_store.EVENT_JOB_CLAIMED,
                1,
                stamp,
                "legacy-worker",
                None,
                None,
            ),
            (
                "evt_t1",
                "job_t",
                "cmp_t",
                "att_t",
                comparison_store.EVENT_JOB_CLAIMED,
                1,
                stamp,
                "legacy-worker",
                None,
                None,
            ),
            (
                "evt_t2",
                "job_t",
                "cmp_t",
                "att_t",
                comparison_store.EVENT_JOB_SUCCEEDED,
                2,
                stamp,
                "legacy-worker",
                "result-t",
                None,
            ),
        ):
            conn.execute(
                "INSERT INTO comparison_detection_job_events VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: comparison_store.init_db(db), range(4)))
    comparison_store.init_db(db)

    queued = comparison_store.get_detection_job("job_q", db)
    running = comparison_store.get_detection_job("job_r", db)
    terminal = comparison_store.get_detection_job("job_t", db)
    assert queued["claim_generation"] == 0
    assert queued["lease_expires_at"] is None
    assert running["claim_generation"] == 1
    assert (
        running["lease_started_at"],
        running["heartbeat_at"],
        running["lease_expires_at"],
    ) == (stamp, stamp, stamp)
    assert detection_job_lease.lease_state(running, now=T0) == "expired"
    assert terminal["claim_generation"] == 1
    assert terminal["lease_expires_at"] == stamp
    events = comparison_store.list_detection_job_events("job_t", db)
    assert [event["claim_generation"] for event in events] == [1, 1]
    assert [event["lease_expires_at"] for event in events] == [stamp, stamp]
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
