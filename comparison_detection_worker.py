"""Durable asynchronous initial comparison detection and one-shot execution.

The authenticated API calls :func:`enqueue_initial_detection`; it validates
registry truth and commits a queued SQLite job without opening Chroma or
running detector computation. A separate credential-free process calls
:func:`run_one_job`, claims at most one queued job, and executes the existing
``comparison_detector.execute_attempt`` seam.

This is deliberately a local, single-node reference queue. There is no lease,
heartbeat, fencing generation, reclaim, retry, scheduler, daemon loop, sleep,
external queue, or multi-node coordination. A process death after claim leaves
the job and attempt running until a later bounded reliability commit adds a
safe reclaim protocol.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import comparison_detector
import comparison_reliability
import comparison_store
import config


FAILURE_INPUTS_CHANGED_SUMMARY = (
    "the filing registry no longer validates the queued comparison inputs"
)


def enqueue_initial_detection(
    comparison_id: str,
    *,
    requested_by_subject: str,
    requested_by_auth_method: str,
    requested_by_token_id: str,
    requested_by_policy_id: str,
    requested_by_policy_version: str,
    actor_context: Mapping[str, Any] | None = None,
    db_path: str | Path | None = None,
    registry_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate registry inputs and atomically enqueue an initial job.

    No Chroma client is accepted and no detector computation function is
    reachable from this path. The returned tagged outcome is either an active
    job or the already-persisted current result.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    registry_path = registry_path or config.FILING_REGISTRY_PATH
    inputs = comparison_detector.resolve_detection_inputs(
        comparison_id,
        db_path=db_path,
        registry_path=registry_path,
    )
    outcome = comparison_store.enqueue_detection_job(
        comparison_id,
        detector_version=comparison_detector.DETECTOR_VERSION,
        workflow_version=comparison_store.WORKFLOW_VERSION,
        previous_filing_id=inputs["record"]["previous_filing_id"],
        current_filing_id=inputs["record"]["current_filing_id"],
        previous_source_hash=inputs["previous_hash"],
        current_source_hash=inputs["current_hash"],
        requested_by_subject=requested_by_subject,
        requested_by_auth_method=requested_by_auth_method,
        requested_by_token_id=requested_by_token_id,
        requested_by_policy_id=requested_by_policy_id,
        requested_by_policy_version=requested_by_policy_version,
        db_path=db_path,
    )
    if outcome["kind"] == "job" and outcome["created"]:
        comparison_reliability.log_detection_job_event(
            comparison_reliability.EVENT_JOB_QUEUED,
            job=outcome["job"],
            actor_context=actor_context,
        )
    return outcome


def run_one_job(
    *,
    worker_id: str,
    job_id: str | None = None,
    db_path: str | Path | None = None,
    registry_path: str | Path | None = None,
    chroma_client=None,
) -> dict[str, Any]:
    """Claim and execute at most one queued job.

    The claim token exists only in this stack frame and the called store
    finalizer. It is never returned in the outcome, persisted raw, or logged.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    registry_path = registry_path or config.FILING_REGISTRY_PATH
    worker = comparison_store.validate_worker_id(worker_id)

    candidate = comparison_store.peek_queued_detection_job(
        job_id=job_id,
        db_path=db_path,
    )
    if candidate is None:
        return {"no_job_available": True}

    try:
        inputs = comparison_detector.resolve_detection_inputs(
            candidate["comparison_id"],
            db_path=db_path,
            registry_path=registry_path,
        )
    except (
        comparison_detector.UnknownComparison,
        comparison_store.ComparisonPairError,
    ):
        failed = comparison_store.fail_queued_detection_job(
            candidate["job_id"],
            failure_code=comparison_store.REASON_JOB_INPUTS_CHANGED,
            failure_summary=FAILURE_INPUTS_CHANGED_SUMMARY,
            db_path=db_path,
        )
        if failed is None:
            return {"no_job_available": True}
        comparison_reliability.log_detection_job_event(
            comparison_reliability.EVENT_JOB_FAILED,
            job=failed,
        )
        return _worker_outcome(failed, None)

    claimed = comparison_store.claim_detection_job(
        job_id=candidate["job_id"],
        worker_id=worker,
        detector_version=comparison_detector.DETECTOR_VERSION,
        workflow_version=comparison_store.WORKFLOW_VERSION,
        previous_source_hash=inputs["previous_hash"],
        current_source_hash=inputs["current_hash"],
        db_path=db_path,
    )
    if claimed is None:
        return {"no_job_available": True}
    if claimed["kind"] == "failed":
        comparison_reliability.log_detection_job_event(
            comparison_reliability.EVENT_JOB_FAILED,
            job=claimed["job"],
        )
        return _worker_outcome(claimed["job"], None)

    job = claimed["job"]
    attempt = claimed["attempt"]
    claim_token = claimed["claim_token"]
    comparison_reliability.log_lifecycle_event(
        comparison_reliability.EVENT_ATTEMPT_STARTED,
        attempt=attempt,
    )
    comparison_reliability.log_detection_job_event(
        comparison_reliability.EVENT_JOB_CLAIMED,
        job=job,
        attempt=attempt,
    )

    try:
        comparison_detector.execute_attempt(
            attempt["attempt_id"],
            record=inputs["record"],
            previous_ref=inputs["previous_ref"],
            current_ref=inputs["current_ref"],
            previous_entry=inputs["previous_entry"],
            current_entry=inputs["current_entry"],
            previous_hash=inputs["previous_hash"],
            current_hash=inputs["current_hash"],
            chroma_client=chroma_client,
            db_path=db_path,
            job_id=job["job_id"],
            worker_id=worker,
            claim_token=claim_token,
        )
    except comparison_detector.DetectionError:
        terminal_job = comparison_store.get_detection_job(
            job["job_id"], db_path=db_path
        )
        terminal_attempt = comparison_store.get_detection_attempt(
            attempt["attempt_id"], db_path=db_path
        )
        if (
            terminal_job is None
            or terminal_job["status"] != comparison_store.JOB_FAILED
            or terminal_attempt is None
            or terminal_attempt["status"] != comparison_store.ATTEMPT_FAILED
        ):
            raise RuntimeError(
                "detector failure did not reach a durable terminal job state"
            )
        comparison_reliability.log_detection_job_event(
            comparison_reliability.EVENT_JOB_FAILED,
            job=terminal_job,
            attempt=terminal_attempt,
        )
        return _worker_outcome(terminal_job, terminal_attempt)

    terminal_job = comparison_store.get_detection_job(
        job["job_id"], db_path=db_path
    )
    terminal_attempt = comparison_store.get_detection_attempt(
        attempt["attempt_id"], db_path=db_path
    )
    if (
        terminal_job is None
        or terminal_job["status"] != comparison_store.JOB_SUCCEEDED
        or terminal_attempt is None
        or terminal_attempt["status"] != comparison_store.ATTEMPT_SUCCEEDED
    ):
        raise RuntimeError("detector execution did not reach a durable success state")
    comparison_reliability.log_detection_job_event(
        comparison_reliability.EVENT_JOB_SUCCEEDED,
        job=terminal_job,
        attempt=terminal_attempt,
    )
    return _worker_outcome(terminal_job, terminal_attempt)


def _worker_outcome(
    job: dict[str, Any], attempt: dict[str, Any] | None
) -> dict[str, Any]:
    """Narrow process/API-neutral outcome; never includes claim material."""
    return {
        "no_job_available": False,
        "job_id": job["job_id"],
        "attempt_id": job.get("attempt_id"),
        "job_status": job["status"],
        "attempt_status": attempt.get("status") if attempt else None,
        "result_hash": job.get("result_hash"),
        "failure_code": job.get("failure_code"),
        "worker_completed_responsibility": job["status"]
        in {comparison_store.JOB_SUCCEEDED, comparison_store.JOB_FAILED},
    }
