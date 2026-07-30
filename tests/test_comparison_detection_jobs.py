"""Durable asynchronous comparison detection jobs and one-shot worker tests.

Entirely local and credential-free: controlled filing fixtures, fake
embeddings only while seeding Chroma, stdlib SQLite, and no network/AWS calls.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings

import api
import comparison_detection_worker
import comparison_detector
import comparison_reliability
import comparison_store
import config
import detection_recovery
import filing_registry
import ingest
from scripts import run_comparison_detection_worker as worker_cli
from tests.auth_helpers import authorization_headers


PREV_ID = "acme-corporation:10-k:2024-12-31"
CURR_ID = "acme-corporation:10-k:2025-12-31"
ACTOR = {
    "requested_by_subject": "operator@example.local",
    "requested_by_auth_method": "local_hs256",
    "requested_by_token_id": "jti-job-test",
    "requested_by_policy_id": "comparison_access_control_v1",
    "requested_by_policy_version": "1",
}
ACTOR_CONTEXT = {
    "actor_subject": ACTOR["requested_by_subject"],
    "actor_auth_method": ACTOR["requested_by_auth_method"],
    "actor_token_id": ACTOR["requested_by_token_id"],
    "required_permission": "comparison.detect",
}


class _FakeEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[float(len(text) % 11), 1.0] for text in texts]

    def embed_query(self, text):
        raise AssertionError("detection must never perform vector retrieval")


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    from langchain_chroma import Chroma

    root = tmp_path_factory.mktemp("detection-job-corpus")
    registry = root / "registry.jsonl"
    docs = ingest.load_documents(
        config.DOCS_DIR,
        manifest=filing_registry.load_manifest(),
        registry_path=registry,
    )
    chunks = ingest.split_documents(docs)
    unique, ids = ingest._dedupe_by_id(chunks)
    counts: dict[str, int] = {}
    for chunk in unique:
        source = chunk.metadata.get("source_path")
        counts[source] = counts.get(source, 0) + 1
    filing_registry.update_chunk_counts(counts, registry)
    persist_dir = root / "chroma"
    chroma = Chroma(
        collection_name=config.CHROMA_COLLECTION,
        persist_directory=str(persist_dir),
        embedding_function=_FakeEmbeddings(),
    )
    chroma.add_documents(documents=unique, ids=ids)
    return SimpleNamespace(
        registry=registry,
        chroma=chroma,
        persist_dir=persist_dir,
    )


@pytest.fixture
def db(tmp_path):
    return tmp_path / "comparisons.db"


@pytest.fixture
def api_env(corpus, db, monkeypatch):
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(db))
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(corpus.registry))
    return SimpleNamespace(
        db=db,
        client=TestClient(api.app, headers=authorization_headers()),
    )


def _comparison(corpus, db):
    record, _ = comparison_store.create_comparison(
        PREV_ID,
        CURR_ID,
        db_path=db,
        registry_path=corpus.registry,
    )
    return record["comparison_id"]


def _enqueue(corpus, db, comparison_id, **overrides):
    actor = {**ACTOR, **overrides}
    return comparison_detection_worker.enqueue_initial_detection(
        comparison_id,
        **actor,
        actor_context={
            **ACTOR_CONTEXT,
            "actor_subject": actor["requested_by_subject"],
            "actor_token_id": actor["requested_by_token_id"],
        },
        db_path=db,
        registry_path=corpus.registry,
    )


def _claim(corpus, db, job_id, worker_id="worker-test"):
    inputs = comparison_detector.resolve_detection_inputs(
        comparison_store.get_detection_job(job_id, db)["comparison_id"],
        db_path=db,
        registry_path=corpus.registry,
    )
    return comparison_store.claim_detection_job(
        job_id=job_id,
        worker_id=worker_id,
        detector_version=comparison_detector.DETECTOR_VERSION,
        workflow_version=comparison_store.WORKFLOW_VERSION,
        previous_source_hash=inputs["previous_hash"],
        current_source_hash=inputs["current_hash"],
        db_path=db,
    )


def _counts(db):
    with closing(sqlite3.connect(db)) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "comparison_detection_jobs",
                "comparison_detection_job_events",
                "comparison_detection_attempts",
                "comparison_results",
            )
        }


def _api_create(env):
    response = env.client.post(
        "/api/comparisons",
        json={"previousFilingId": PREV_ID, "currentFilingId": CURR_ID},
    )
    assert response.status_code in (200, 201)
    return response.json()["comparison"]["comparisonId"]


def test_api_enqueue_is_202_idempotent_and_performs_no_detector_or_chroma_work(
    api_env, monkeypatch
):
    documented = api_env.client.get("/openapi.json").json()["paths"][
        "/api/comparisons/{comparison_id}/detect"
    ]["post"]["responses"]
    assert {"200", "202"} <= set(documented)
    assert documented["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ComparisonDetectionJobResponse"
    }

    comparison_id = _api_create(api_env)

    def forbidden(*args, **kwargs):
        raise AssertionError("enqueue reached detector/Chroma computation")

    monkeypatch.setattr(comparison_detector, "_compute_result", forbidden)
    monkeypatch.setattr(comparison_detector, "open_index", forbidden)

    first = api_env.client.post(f"/api/comparisons/{comparison_id}/detect")
    assert first.status_code == 202
    assert first.json() == {
        "created": True,
        "comparisonId": comparison_id,
        "jobId": first.json()["jobId"],
        "jobStatus": "queued",
        "comparisonStatus": "queued_for_detection",
        "queuedAt": first.json()["queuedAt"],
        "attemptId": None,
    }
    assert first.json()["jobId"].startswith("djob_")
    assert _counts(api_env.db) == {
        "comparison_detection_jobs": 1,
        "comparison_detection_job_events": 1,
        "comparison_detection_attempts": 0,
        "comparison_results": 0,
    }
    assert comparison_store.get_comparison(
        comparison_id, api_env.db
    )["status"] == comparison_store.STATUS_QUEUED_FOR_DETECTION

    second = api_env.client.post(f"/api/comparisons/{comparison_id}/detect")
    assert second.status_code == 202
    assert second.json()["created"] is False
    assert second.json()["jobId"] == first.json()["jobId"]
    assert _counts(api_env.db)["comparison_detection_job_events"] == 1


def test_already_detected_preserves_200_result_contract_and_creates_no_job(
    corpus, api_env
):
    comparison_id = _api_create(api_env)
    result, created, attempt_id = comparison_detector.detect_with_attempt(
        comparison_id,
        db_path=api_env.db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    assert created is True

    response = api_env.client.post(f"/api/comparisons/{comparison_id}/detect")
    assert response.status_code == 200
    assert response.json() == {
        "created": False,
        "result": result,
        "attemptId": attempt_id,
    }
    assert comparison_store.list_detection_jobs(
        comparison_id, api_env.db
    ) == []


def test_concurrent_identical_and_conflicting_enqueues_are_serialized(corpus, tmp_path):
    identical_db = tmp_path / "identical.db"
    comparison_id = _comparison(corpus, identical_db)

    def same(index):
        return _enqueue(
            corpus,
            identical_db,
            comparison_id,
            requested_by_token_id=f"rotated-jti-{index}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(same, range(8)))
    assert sum(outcome["created"] for outcome in outcomes) == 1
    assert len({outcome["job"]["job_id"] for outcome in outcomes}) == 1
    assert _counts(identical_db) == {
        "comparison_detection_jobs": 1,
        "comparison_detection_job_events": 1,
        "comparison_detection_attempts": 0,
        "comparison_results": 0,
    }

    conflicting_db = tmp_path / "conflicting.db"
    conflict_id = _comparison(corpus, conflicting_db)

    def different(subject):
        try:
            return _enqueue(
                corpus,
                conflicting_db,
                conflict_id,
                requested_by_subject=subject,
            )
        except comparison_store.DetectionStateError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        conflicts = list(
            pool.map(different, ("operator-a", "operator-b"))
        )
    assert sum(isinstance(item, dict) for item in conflicts) == 1
    loser = next(item for item in conflicts if isinstance(item, Exception))
    assert loser.code == comparison_store.REASON_JOB_ACTIVE_CONFLICT
    assert _counts(conflicting_db)["comparison_detection_jobs"] == 1


def test_concurrent_workers_claim_once_and_only_winner_executes(corpus, db, monkeypatch):
    comparison_id = _comparison(corpus, db)
    job_id = _enqueue(corpus, db, comparison_id)["job"]["job_id"]
    original = comparison_detector._compute_result
    calls = 0
    lock = Lock()

    def counted(*args, **kwargs):
        nonlocal calls
        with lock:
            calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(comparison_detector, "_compute_result", counted)

    def run(index):
        return comparison_detection_worker.run_one_job(
            worker_id=f"worker-{index}",
            db_path=db,
            registry_path=corpus.registry,
            chroma_client=corpus.chroma,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(run, range(6)))

    terminal = [item for item in outcomes if not item["no_job_available"]]
    assert len(terminal) == 1
    assert terminal[0]["job_id"] == job_id
    assert terminal[0]["job_status"] == "succeeded"
    assert calls == 1
    attempts = comparison_store.list_detection_attempts(comparison_id, db)
    assert [(item["attempt_number"], item["status"]) for item in attempts] == [
        (1, "succeeded")
    ]


def test_worker_success_atomically_finalizes_every_record(corpus, db):
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    outcome = comparison_detection_worker.run_one_job(
        worker_id="worker-success",
        job_id=job["job_id"],
        db_path=db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    assert outcome["job_status"] == comparison_store.JOB_SUCCEEDED
    assert outcome["attempt_status"] == comparison_store.ATTEMPT_SUCCEEDED
    stored_job = comparison_store.get_detection_job(job["job_id"], db)
    attempt = comparison_store.get_detection_attempt(outcome["attempt_id"], db)
    result = comparison_store.get_result(comparison_id, db)
    comparison = comparison_store.get_comparison(comparison_id, db)
    assert stored_job["result_hash"] == attempt["result_hash"] == result["result_hash"]
    assert comparison["status"] == comparison_store.STATUS_DETECTED
    assert [
        event["event_type"]
        for event in comparison_store.list_detection_job_events(job["job_id"], db)
    ] == [
        "detection_job_queued",
        "detection_job_claimed",
        "detection_job_succeeded",
    ]
    assert [
        event["event_type"]
        for event in comparison_store.list_detection_events(
            outcome["attempt_id"], db
        )
    ] == ["detection_started", "detection_succeeded"]


def test_worker_domain_failure_atomically_fails_job_attempt_and_comparison(
    corpus, db, monkeypatch, tmp_path
):
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    secret = "raw /private/secret SELECT * FROM filing"

    def boom(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(comparison_detector, "detect_changes", boom)
    outcome = comparison_detection_worker.run_one_job(
        worker_id="worker-failure",
        db_path=db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    assert outcome["job_status"] == comparison_store.JOB_FAILED
    assert outcome["attempt_status"] == comparison_store.ATTEMPT_FAILED
    assert outcome["failure_code"] == "detector_internal_error"
    stored_job = comparison_store.get_detection_job(job["job_id"], db)
    attempt = comparison_store.get_detection_attempt(outcome["attempt_id"], db)
    comparison = comparison_store.get_comparison(comparison_id, db)
    assert stored_job["failure_code"] == attempt["failure_code"]
    assert comparison["status"] == comparison_store.STATUS_FAILED
    assert secret not in json.dumps(stored_job)
    assert secret not in json.dumps(attempt)
    assert comparison_store.get_result(comparison_id, db) is None

    for name, detector_version, previous_hash, expected_code in (
        (
            "input-drift",
            comparison_detector.DETECTOR_VERSION,
            "changed-source-hash",
            comparison_store.REASON_JOB_INPUTS_CHANGED,
        ),
        (
            "version-drift",
            "future-detector-version",
            None,
            comparison_store.REASON_JOB_VERSION_CHANGED,
        ),
    ):
        drift_db = tmp_path / f"{name}.db"
        drift_comparison = _comparison(corpus, drift_db)
        drift_job = _enqueue(corpus, drift_db, drift_comparison)["job"]
        drift = comparison_store.claim_detection_job(
            job_id=drift_job["job_id"],
            worker_id=f"worker-{name}",
            detector_version=detector_version,
            workflow_version=comparison_store.WORKFLOW_VERSION,
            previous_source_hash=(
                previous_hash or drift_job["previous_source_hash"]
            ),
            current_source_hash=drift_job["current_source_hash"],
            db_path=drift_db,
        )
        assert drift["kind"] == "failed"
        assert drift["job"]["failure_code"] == expected_code
        assert drift["job"]["attempt_id"] is None
        assert comparison_store.list_detection_attempts(
            drift_comparison, drift_db
        ) == []
        assert comparison_store.get_comparison(
            drift_comparison, drift_db
        )["status"] == "failed"
        assert [
            event["event_type"]
            for event in comparison_store.list_detection_job_events(
                drift_job["job_id"], drift_db
            )
        ] == ["detection_job_queued", "detection_job_failed"]


def test_claim_ownership_and_terminal_invariants_reject_invalid_finalization(
    corpus, db
):
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    claimed = _claim(corpus, db, job["job_id"], worker_id="worker-owner")
    attempt_id = claimed["attempt"]["attempt_id"]
    result_json = json.dumps({"created_at": "now", "value": 1})
    result_hash = hashlib.sha256(
        json.dumps({"value": 1}, sort_keys=True).encode()
    ).hexdigest()
    kwargs = dict(
        result_json=result_json,
        result_hash=result_hash,
        detector_version=comparison_detector.DETECTOR_VERSION,
        workflow_version=comparison_store.WORKFLOW_VERSION,
        previous_source_hash=claimed["job"]["previous_source_hash"],
        current_source_hash=claimed["job"]["current_source_hash"],
        db_path=db,
    )

    cases = (
        (
            dict(worker_id="worker-owner", claim_token="wrong"),
            comparison_store.REASON_JOB_CLAIM_INVALID,
        ),
        (
            dict(worker_id="worker-other", claim_token=claimed["claim_token"]),
            comparison_store.REASON_JOB_WORKER_MISMATCH,
        ),
    )
    for ownership, code in cases:
        with pytest.raises(comparison_store.DetectionStateError) as exc:
            comparison_store.complete_detection_job(
                job["job_id"], attempt_id, **ownership, **kwargs
            )
        assert exc.value.code == code

    with pytest.raises(comparison_store.DetectionStateError) as mismatch:
        comparison_store.complete_detection_job(
            job["job_id"],
            "att_wrong",
            worker_id="worker-owner",
            claim_token=claimed["claim_token"],
            **kwargs,
        )
    assert mismatch.value.code == comparison_store.REASON_JOB_ATTEMPT_MISMATCH

    with pytest.raises(comparison_store.DetectionStateError) as bad_hash:
        comparison_store.complete_detection_job(
            job["job_id"],
            attempt_id,
            worker_id="worker-owner",
            claim_token=claimed["claim_token"],
            **{**kwargs, "result_hash": "0" * 64},
        )
    assert bad_hash.value.code == comparison_store.REASON_JOB_RESULT_HASH_MISMATCH

    with pytest.raises(comparison_store.DetectionStateError) as unknown:
        comparison_store.complete_detection_job(
            "djob_missing",
            attempt_id,
            worker_id="worker-owner",
            claim_token=claimed["claim_token"],
            **kwargs,
        )
    assert unknown.value.code == comparison_store.REASON_JOB_NOT_FOUND
    assert comparison_store.get_detection_job(job["job_id"], db)["status"] == "running"
    assert comparison_store.get_detection_attempt(attempt_id, db)["status"] == "running"
    assert comparison_store.get_result(comparison_id, db) is None

    comparison_store.complete_detection_job(
        job["job_id"],
        attempt_id,
        worker_id="worker-owner",
        claim_token=claimed["claim_token"],
        **kwargs,
    )
    with pytest.raises(comparison_store.DetectionStateError) as terminal:
        comparison_store.complete_detection_job(
            job["job_id"],
            attempt_id,
            worker_id="worker-owner",
            claim_token=claimed["claim_token"],
            **kwargs,
        )
    assert terminal.value.code == comparison_store.REASON_JOB_NOT_RUNNING


def test_raw_claim_token_is_never_persisted_or_returned_by_http(corpus, api_env):
    comparison_id = _api_create(api_env)
    queued = api_env.client.post(
        f"/api/comparisons/{comparison_id}/detect"
    ).json()
    claimed = _claim(corpus, api_env.db, queued["jobId"], "worker-secret-safe")
    token = claimed["claim_token"]
    assert token.encode() not in api_env.db.read_bytes()

    single = api_env.client.get(
        f"/api/comparison-detection-jobs/{queued['jobId']}"
    )
    events = api_env.client.get(
        f"/api/comparison-detection-jobs/{queued['jobId']}/events"
    )
    listing = api_env.client.get(
        f"/api/comparisons/{comparison_id}/detection-jobs"
    )
    assert single.status_code == events.status_code == listing.status_code == 200
    combined = single.text + events.text + listing.text
    assert token not in combined
    for forbidden in (
        "claimToken",
        "claim_token",
        "requestHash",
        "request_hash",
        "tokenId",
        "token_id",
        "result_json",
        "failureSummary",
    ):
        assert forbidden not in combined
    assert set(single.json()) == {
        "jobId",
        "comparisonId",
        "attemptId",
        "triggerType",
        "status",
        "detectorVersion",
        "workflowVersion",
        "requestedBySubject",
        "requestedByAuthMethod",
        "queuedAt",
        "claimedAt",
        "finishedAt",
        "workerId",
        "resultHash",
        "failureCode",
    }


def test_authentication_refusals_create_no_job(api_env):
    comparison_id = _api_create(api_env)
    unauthenticated = TestClient(api.app)
    viewer = TestClient(
        api.app,
        headers=authorization_headers(subject="viewer", roles=("viewer",)),
    )
    assert unauthenticated.post(
        f"/api/comparisons/{comparison_id}/detect"
    ).status_code == 401
    assert viewer.post(
        f"/api/comparisons/{comparison_id}/detect"
    ).status_code == 403
    assert _counts(api_env.db)["comparison_detection_jobs"] == 0


def test_principal_attribution_is_persisted_but_only_narrow_fields_are_exposed(
    corpus, db, monkeypatch
):
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(db))
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(corpus.registry))
    comparison_id = _comparison(corpus, db)
    client = TestClient(
        api.app,
        headers=authorization_headers(
            subject="verified-operator@example.local", roles=("operator",)
        ),
    )
    response = client.post(f"/api/comparisons/{comparison_id}/detect")
    assert response.status_code == 202
    stored = comparison_store.get_detection_job(response.json()["jobId"], db)
    assert stored["requested_by_subject"] == "verified-operator@example.local"
    assert stored["requested_by_auth_method"] == "local_hs256"
    public = client.get(
        f"/api/comparison-detection-jobs/{stored['job_id']}"
    ).json()
    assert public["requestedBySubject"] == stored["requested_by_subject"]
    assert "requested_by_token_id" not in public


def test_direct_detection_and_synchronous_replay_create_no_initial_job(corpus, tmp_path):
    direct_db = tmp_path / "direct.db"
    comparison_id = _comparison(corpus, direct_db)
    comparison_detector.detect(
        comparison_id,
        db_path=direct_db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    assert comparison_store.list_detection_jobs(comparison_id, direct_db) == []

    replay_db = tmp_path / "replay.db"
    replay_comparison = _comparison(corpus, replay_db)
    inputs = comparison_detector.resolve_detection_inputs(
        replay_comparison,
        db_path=replay_db,
        registry_path=corpus.registry,
    )
    source = comparison_store.start_detection_attempt(
        replay_comparison,
        detector_version=comparison_detector.DETECTOR_VERSION,
        workflow_version=comparison_store.WORKFLOW_VERSION,
        previous_source_hash=inputs["previous_hash"],
        current_source_hash=inputs["current_hash"],
        db_path=replay_db,
    )
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    with closing(sqlite3.connect(replay_db)) as conn, conn:
        conn.execute(
            "UPDATE comparison_detection_attempts SET started_at = ? "
            "WHERE attempt_id = ?",
            (old.isoformat(), source["attempt_id"]),
        )
    outcome, created = detection_recovery.replay_attempt(
        source["attempt_id"],
        operator_id="local-operator",
        reason_code="operator_replay_stale_attempt",
        operator_note="explicit controlled replay",
        now=datetime.now(timezone.utc),
        db_path=replay_db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    assert created is True
    assert outcome["replacement_status"] == "succeeded"
    assert comparison_store.list_detection_jobs(replay_comparison, replay_db) == []


def test_process_interruption_boundaries_survive_reopen(corpus, tmp_path, api_env):
    queued_db = tmp_path / "queued.db"
    comparison_id = _comparison(corpus, queued_db)
    job = _enqueue(corpus, queued_db, comparison_id)["job"]
    assert comparison_store.get_detection_job(job["job_id"], queued_db)["status"] == "queued"
    assert comparison_store.list_detection_attempts(comparison_id, queued_db) == []
    assert comparison_store.get_comparison(
        comparison_id, queued_db
    )["status"] == "queued_for_detection"

    claimed = _claim(corpus, queued_db, job["job_id"], "worker-crashed")
    attempt_id = claimed["attempt"]["attempt_id"]
    assert comparison_store.get_detection_job(job["job_id"], queued_db)["status"] == "running"
    assert comparison_store.get_detection_attempt(attempt_id, queued_db)["status"] == "running"
    assert comparison_store.get_comparison(comparison_id, queued_db)["status"] == "detecting"
    assert comparison_store.get_result(comparison_id, queued_db) is None
    assert _claim(corpus, queued_db, job["job_id"], "worker-other") is None
    view = detection_recovery.recovery_view(
        attempt_id,
        now=datetime.now(timezone.utc) + timedelta(hours=1),
        db_path=queued_db,
        registry_path=corpus.registry,
    )
    assert view["replay_eligible"] is False
    assert (
        view["blocking_reason"]
        == comparison_store.REASON_JOB_RECLAIM_NOT_SUPPORTED
    )

    terminal_db = tmp_path / "terminal.db"
    terminal_comparison = _comparison(corpus, terminal_db)
    _enqueue(corpus, terminal_db, terminal_comparison)
    first = comparison_detection_worker.run_one_job(
        worker_id="worker-terminal",
        db_path=terminal_db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    assert first["job_status"] == "succeeded"
    second = comparison_detection_worker.run_one_job(
        worker_id="worker-after-output-loss",
        db_path=terminal_db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    assert second == {"no_job_available": True}
    assert len(
        comparison_store.list_detection_attempts(terminal_comparison, terminal_db)
    ) == 1

    # The authenticated API process only enqueues; a distinct credential-free
    # Python process opens the same durable state and runs the one-shot worker.
    process_comparison = _api_create(api_env)
    queued = api_env.client.post(
        f"/api/comparisons/{process_comparison}/detect"
    )
    assert queued.status_code == 202
    child = (
        "import config, sys; "
        "config.CHROMA_PERSIST_DIR = sys.argv[1]; "
        "from scripts.run_comparison_detection_worker import main; "
        "raise SystemExit(main(sys.argv[2:]))"
    )
    child_env = os.environ.copy()
    child_env.pop("FDIA_AUTH_SECRET", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            str(corpus.persist_dir),
            "--db-path",
            str(api_env.db),
            "--registry-path",
            str(corpus.registry),
            "--worker-id",
            "separate-process-worker",
            "--job-id",
            queued.json()["jobId"],
            "--once",
            "--json",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["jobStatus"] == "succeeded"
    assert completed.stderr == ""
    assert comparison_store.get_comparison(
        process_comparison, api_env.db
    )["status"] == "detected"
    assert comparison_store.get_result(process_comparison, api_env.db) is not None


def test_reliability_job_metrics_issues_and_reads_are_read_only(corpus, db):
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    before = db.stat().st_mtime_ns
    report = comparison_reliability.summary(
        db_path=db, registry_path=corpus.registry
    )
    assert report["gauges"]["comparisons_queued_for_detection"] == 1
    assert report["gauges"]["detection_jobs_queued"] == 1
    assert report["jobs"] == {
        "jobs_queued": 1,
        "jobs_claimed": 0,
        "jobs_succeeded": 0,
        "jobs_failed": 0,
    }
    assert {
        issue["issue_type"]
        for issue in comparison_reliability.issues(
            db_path=db, registry_path=corpus.registry
        )["issues"]
    } == {"queued_detection_job"}
    assert db.stat().st_mtime_ns == before

    claimed = _claim(corpus, db, job["job_id"], "worker-stale")
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "UPDATE comparison_detection_jobs SET queued_at = ?, claimed_at = ? "
            "WHERE job_id = ?",
            ((old - timedelta(seconds=5)).isoformat(), old.isoformat(), job["job_id"]),
        )
        conn.execute(
            "UPDATE comparison_detection_attempts SET started_at = ? "
            "WHERE attempt_id = ?",
            (old.isoformat(), claimed["attempt"]["attempt_id"]),
        )
    report = comparison_reliability.summary(
        now=datetime.now(timezone.utc),
        db_path=db,
        registry_path=corpus.registry,
    )
    assert report["gauges"]["detection_jobs_running"] == 1
    assert report["jobs"]["jobs_claimed"] == 1
    types = {
        issue["issue_type"]
        for issue in comparison_reliability.issues(
            now=datetime.now(timezone.utc),
            db_path=db,
            registry_path=corpus.registry,
        )["issues"]
    }
    assert "running_detection_job_without_lease" in types


def test_job_structured_logs_are_allowlisted_and_post_commit(
    corpus, db, caplog, tmp_path, monkeypatch
):
    comparison_id = _comparison(corpus, db)
    with caplog.at_level(logging.INFO, logger="comparison_reliability"):
        job = _enqueue(corpus, db, comparison_id)["job"]
        outcome = comparison_detection_worker.run_one_job(
            worker_id="worker-logs",
            db_path=db,
            registry_path=corpus.registry,
            chroma_client=corpus.chroma,
        )
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None)
        in comparison_reliability.JOB_LOG_EVENTS
    ]
    assert [record.event for record in records] == [
        "detection_job_queued",
        "detection_job_claimed",
        "detection_job_succeeded",
    ]
    for record in records:
        for field in comparison_reliability.JOB_LOG_FIELDS:
            assert hasattr(record, field)
        for forbidden in (
            "claim_token",
            "claim_token_hash",
            "authorization",
            "bearer_token",
            "evidence",
            "result_json",
        ):
            assert not hasattr(record, forbidden)
    assert records[0].actor_subject == ACTOR["requested_by_subject"]
    assert records[1].actor_subject is None
    assert records[-1].result_hash == outcome["result_hash"]
    assert comparison_store.get_detection_job(job["job_id"], db)["status"] == "succeeded"

    fault_db = tmp_path / "logging-fault.db"
    fault_comparison = _comparison(corpus, fault_db)

    def logging_boom(*args, **kwargs):
        raise RuntimeError("logging backend unavailable")

    monkeypatch.setattr(comparison_reliability.logger, "info", logging_boom)
    fault_job = _enqueue(corpus, fault_db, fault_comparison)["job"]
    fault_outcome = comparison_detection_worker.run_one_job(
        worker_id="worker-logging-fault",
        db_path=fault_db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )
    assert fault_outcome["job_status"] == "succeeded"
    assert comparison_store.get_detection_job(
        fault_job["job_id"], fault_db
    )["status"] == "succeeded"
    assert comparison_store.get_result(fault_comparison, fault_db) is not None


def test_worker_cli_no_job_success_domain_failure_and_infrastructure_paths(
    corpus, tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("FDIA_AUTH_SECRET", raising=False)
    monkeypatch.setattr(config, "CHROMA_PERSIST_DIR", str(corpus.persist_dir))

    empty_db = tmp_path / "empty.db"
    comparison_store.init_db(empty_db)
    before = empty_db.read_bytes()
    assert worker_cli.main(
        [
            "--db-path", str(empty_db),
            "--registry-path", str(corpus.registry),
            "--worker-id", "cli-empty",
            "--once",
            "--json",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"noJobAvailable": True}
    assert empty_db.read_bytes() == before

    with pytest.raises(SystemExit) as invalid_args:
        worker_cli.main(
            [
                "--db-path", str(empty_db),
                "--registry-path", str(corpus.registry),
                "--worker-id", "cli-missing-once",
            ]
        )
    assert invalid_args.value.code == 2
    invalid_capture = capsys.readouterr()
    assert invalid_capture.out == ""

    assert worker_cli.main(
        [
            "--db-path", str(empty_db),
            "--registry-path", str(corpus.registry),
            "--worker-id", "bad\nworker",
            "--once",
        ]
    ) == 2
    invalid_worker = capsys.readouterr()
    assert invalid_worker.out == ""
    assert str(tmp_path) not in invalid_worker.err
    assert empty_db.read_bytes() == before

    success_db = tmp_path / "success.db"
    success_id = _comparison(corpus, success_db)
    _enqueue(corpus, success_db, success_id)
    assert worker_cli.main(
        [
            "--db-path", str(success_db),
            "--registry-path", str(corpus.registry),
            "--worker-id", "cli-success",
            "--once",
            "--json",
        ]
    ) == 0
    success = json.loads(capsys.readouterr().out)
    assert success["jobStatus"] == "succeeded"
    assert success["attemptStatus"] == "succeeded"

    failed_db = tmp_path / "failed.db"
    failed_id = _comparison(corpus, failed_db)
    _enqueue(corpus, failed_db, failed_id)
    monkeypatch.setattr(
        comparison_detector,
        "detect_changes",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("secret /absolute/path SELECT")
        ),
    )
    assert worker_cli.main(
        [
            "--db-path", str(failed_db),
            "--registry-path", str(corpus.registry),
            "--worker-id", "cli-domain-failure",
            "--once",
            "--json",
        ]
    ) == 0
    failed_capture = capsys.readouterr()
    failed = json.loads(failed_capture.out)
    assert failed["jobStatus"] == "failed"
    assert failed["failureCode"] == "detector_internal_error"
    assert "/absolute" not in failed_capture.out + failed_capture.err
    assert "SELECT" not in failed_capture.out + failed_capture.err

    assert worker_cli.main(
        [
            "--db-path", str(tmp_path / "missing.db"),
            "--registry-path", str(corpus.registry),
            "--worker-id", "cli-infra",
            "--once",
        ]
    ) == 1
    infra = capsys.readouterr()
    assert infra.out == ""
    assert str(tmp_path) not in infra.err


def test_migration_and_concurrent_initialization_preserve_rows(tmp_path):
    db = tmp_path / "legacy.db"
    comparison_store.init_db(db)
    timestamp = "2026-01-01T00:00:00+00:00"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript(
            f"""
            INSERT INTO comparisons VALUES (
                'cmp_legacy', 'comparison.v1', 'comparison_workflow.v2',
                'prev', 'curr', '["item_1a_risk_factors"]', 'detected',
                '{timestamp}', '{timestamp}', NULL, NULL
            );
            INSERT INTO comparison_results VALUES (
                'cmp_legacy', 'comparison.v1', 'item1a_detector.v2',
                'a', 'b', '{{"legacy":true}}', 'result-hash', '{timestamp}'
            );
            INSERT INTO comparison_detection_attempts VALUES (
                'att_source', 'cmp_legacy', 1, 'timed_out',
                'item1a_detector.v2', 'comparison_workflow.v2', 'a', 'b',
                '{timestamp}', '{timestamp}', NULL,
                'detection_attempt_timed_out', 'bounded timeout summary'
            );
            INSERT INTO comparison_detection_attempts VALUES (
                'att_replacement', 'cmp_legacy', 2, 'succeeded',
                'item1a_detector.v2', 'comparison_workflow.v2', 'a', 'b',
                '{timestamp}', '{timestamp}', 'result-hash', NULL, NULL
            );
            INSERT INTO comparison_detection_replays (
                replay_id, comparison_id, source_attempt_id,
                replacement_attempt_id, operator_id, reason_code,
                operator_note, request_hash, policy_id, policy_version,
                requested_at
            ) VALUES (
                'rpl_legacy', 'cmp_legacy', 'att_source', 'att_replacement',
                'legacy-operator', 'operator_replay_stale_attempt',
                'bounded legacy note', 'request-hash',
                'detection_recovery_v1', '1', '{timestamp}'
            );
            INSERT INTO comparison_governance_evaluations VALUES (
                'gov_legacy', 'cmp_legacy', 'result-hash',
                'comparison_risk_v1', '1', 0.2, 'low', 'returned',
                '[]', '{timestamp}', '{{"governed":true}}', 'governed-hash'
            );
            INSERT INTO comparison_review_items VALUES (
                'crev_legacy', 'cmp_legacy', 'gov_legacy', 'result-hash',
                'governed-hash', 'pending', NULL, NULL, '{timestamp}'
            );
            INSERT INTO comparison_exports VALUES (
                'exp_legacy', 'comparison.export.v1', 'cmp_legacy',
                'gov_legacy', NULL, 'returned_by_policy', 'governed-hash',
                'governed-hash', '{{"export":true}}', 'export-hash',
                '{timestamp}'
            );
            """
        )
        preserved_tables = (
            "comparisons",
            "comparison_results",
            "comparison_detection_attempts",
            "comparison_detection_replays",
            "comparison_governance_evaluations",
            "comparison_review_items",
            "comparison_exports",
        )
        before = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in preserved_tables
        }
        # Simulate the pre-job comparison CHECK while retaining every child
        # table and row. The migration must rebuild only this parent table.
        conn.executescript(
            """
            CREATE TABLE comparisons_legacy (
                comparison_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                workflow_version TEXT NOT NULL,
                previous_filing_id TEXT NOT NULL,
                current_filing_id TEXT NOT NULL,
                section_scope TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'ready_for_detection', 'detecting', 'detected', 'failed'
                )),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                failure_code TEXT,
                failure_summary TEXT
            );
            INSERT INTO comparisons_legacy SELECT * FROM comparisons;
            DROP TABLE comparisons;
            ALTER TABLE comparisons_legacy RENAME TO comparisons;
            """
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: comparison_store.init_db(db), range(4)))
    comparison_store.init_db(db)
    assert comparison_store.get_comparison("cmp_legacy", db)["status"] == "detected"
    assert comparison_store.get_result("cmp_legacy", db)["result"] == {
        "legacy": True
    }
    with closing(sqlite3.connect(db)) as conn:
        after = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in preserved_tables
        }
        assert after == before
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='comparisons'"
        ).fetchone()[0]
        assert "'queued_for_detection'" in ddl
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_constraints_and_integrity_checks(corpus, db):
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    with closing(sqlite3.connect(db)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE comparison_detection_jobs SET status='running' "
                "WHERE job_id=?",
                (job["job_id"],),
            )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
