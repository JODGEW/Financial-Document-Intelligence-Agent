"""Durable asynchronous initial comparison detection and one-shot execution.

The authenticated API calls :func:`enqueue_initial_detection`; it validates
registry truth and commits a queued SQLite job without opening Chroma or
running detector computation. A separate credential-free process calls
:func:`run_one_job`, claims at most one queued job, and executes the existing
``comparison_detector.execute_attempt`` seam.

This is deliberately a local, single-node reference queue. Running claims have
finite leases and monotonically increasing generations. A later explicit
one-shot invocation may reclaim expired work; no wall-clock transition happens
by itself. While detector computation is active, one bounded in-process
controller heartbeats only that claim. It never selects jobs or retries
detection. There is no detector retry, scheduler, job-processing daemon loop,
sleep, external queue, or multi-node coordination.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import comparison_detector
import comparison_reliability
import comparison_store
import config
import detection_job_lease

logger = logging.getLogger("comparison_detection_worker")

HEARTBEAT_FAULT_CODE = "detection_job_heartbeat_internal_error"
_HEARTBEAT_THREAD_NAME = "detection-job-heartbeat"


class DetectionJobHeartbeatController:
    """Maintain one claimed job's finite lease during detector computation.

    This controller never selects work and never executes a detector. It owns
    exactly one static-name thread whose only loop is an interruptible timed
    wait followed by the existing atomic heartbeat operation. The raw claim
    token stays in this object and is never formatted, logged, persisted raw,
    exposed through an exception, or placed in the thread name.

    ``clock`` and ``wait`` are deterministic test seams. Production uses the
    store's UTC clock and ``threading.Event.wait``.
    """

    def __init__(
        self,
        *,
        job_id: str,
        worker_id: str,
        claim_generation: int,
        claim_token: str,
        db_path: str | Path,
        policy: dict[str, Any],
        clock: Callable[[], datetime] | None = None,
        wait: Callable[[threading.Event, float], bool] | None = None,
    ):
        self._job_id = job_id
        self._worker_id = worker_id
        self._claim_generation = claim_generation
        self._claim_token = claim_token
        self._db_path = db_path
        self._heartbeat_extension_seconds = policy[
            "heartbeat_extension_seconds"
        ]
        self._clock = clock
        self._wait = wait or (
            lambda stop, seconds: stop.wait(seconds)
        )
        self._interval_seconds = (
            detection_job_lease.heartbeat_interval_seconds(policy)
        )
        self._stop = threading.Event()
        self._ownership_lost = threading.Event()
        self._ownership_lost_code: str | None = None
        self._heartbeat_fault_code: str | None = None
        self._thread: threading.Thread | None = None

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    @property
    def thread_name(self) -> str:
        return _HEARTBEAT_THREAD_NAME

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def ownership_lost_code(self) -> str | None:
        return self._ownership_lost_code

    @property
    def heartbeat_fault_code(self) -> str | None:
        return self._heartbeat_fault_code

    def wait_for_ownership_loss(self, timeout: float | None = None) -> bool:
        """Deterministic synchronization seam; no polling or sleeping."""
        return self._ownership_lost.wait(timeout)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("heartbeat controller may be started only once")
        self._thread = threading.Thread(
            target=self._run,
            name=_HEARTBEAT_THREAD_NAME,
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop promptly and prove the heartbeat thread has terminated."""
        self._stop.set()
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("heartbeat controller did not stop")

    def _run(self) -> None:
        while not self._wait(self._stop, self._interval_seconds):
            if self._stop.is_set():
                return
            try:
                heartbeat_job(
                    job_id=self._job_id,
                    worker_id=self._worker_id,
                    claim_generation=self._claim_generation,
                    claim_token=self._claim_token,
                    db_path=self._db_path,
                    policy={
                        "heartbeat_extension_seconds": (
                            self._heartbeat_extension_seconds
                        )
                    },
                    now=self._clock() if self._clock is not None else None,
                )
            except comparison_store.DetectionStateError as exc:
                if exc.code in comparison_store.JOB_OWNERSHIP_LOST_CODES:
                    self._ownership_lost_code = exc.code
                    self._ownership_lost.set()
                    self._stop.set()
                    return
                self._record_heartbeat_fault()
            except Exception:
                # Do not retain or log a raw exception. A later heartbeat may
                # recover; terminal finalization remains the authoritative
                # ownership/lease check and will fail closed if the lease did
                # not survive.
                self._record_heartbeat_fault()

    def _record_heartbeat_fault(self) -> None:
        self._heartbeat_fault_code = HEARTBEAT_FAULT_CODE
        logger.warning(
            "Detection-job heartbeat fault recorded; terminal finalization "
            "will revalidate claim ownership"
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
    policy: dict[str, Any] | None = None,
    now: datetime | None = None,
    heartbeat_controller_factory: Callable[..., DetectionJobHeartbeatController]
    | None = None,
) -> dict[str, Any]:
    """Claim or reclaim and execute at most one eligible job.

    The generation and raw token exist only in this stack frame and the called
    fenced finalizer/controller. A bounded controller maintains only this
    claim's lease while detector computation is active; it never selects
    another job. The token is never returned, persisted raw, or logged.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    registry_path = registry_path or config.FILING_REGISTRY_PATH
    policy = policy or detection_job_lease.POLICY
    worker = comparison_store.validate_worker_id(worker_id)
    comparison_store.init_db(db_path)

    candidate = comparison_store.peek_claimable_detection_job(
        job_id=job_id,
        reclaim_grace_seconds=policy["reclaim_grace_seconds"],
        now=now,
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
        claimed = comparison_store.claim_detection_job(
            job_id=candidate["job_id"],
            worker_id=worker,
            detector_version=comparison_detector.DETECTOR_VERSION,
            workflow_version=comparison_store.WORKFLOW_VERSION,
            previous_source_hash=(
                "invalid:" + candidate["previous_source_hash"]
            ),
            current_source_hash=candidate["current_source_hash"],
            lease_duration_seconds=policy["lease_duration_seconds"],
            reclaim_grace_seconds=policy["reclaim_grace_seconds"],
            max_claim_generations=policy["max_claim_generations"],
            now=now,
            db_path=db_path,
        )
        if claimed is None:
            return {"no_job_available": True}
        failed = claimed["job"]
        attempt = claimed.get("attempt")
        if claimed["kind"] == "exhausted":
            comparison_reliability.log_detection_job_event(
                comparison_reliability.EVENT_JOB_CLAIM_EXHAUSTED,
                job=failed,
                attempt=attempt,
                source_attempt_id=failed.get("attempt_id"),
            )
        comparison_reliability.log_detection_job_event(
            comparison_reliability.EVENT_JOB_FAILED,
            job=failed,
            attempt=attempt,
        )
        return _worker_outcome(failed, attempt)

    claimed = comparison_store.claim_detection_job(
        job_id=candidate["job_id"],
        worker_id=worker,
        detector_version=comparison_detector.DETECTOR_VERSION,
        workflow_version=comparison_store.WORKFLOW_VERSION,
        previous_source_hash=inputs["previous_hash"],
        current_source_hash=inputs["current_hash"],
        lease_duration_seconds=policy["lease_duration_seconds"],
        reclaim_grace_seconds=policy["reclaim_grace_seconds"],
        max_claim_generations=policy["max_claim_generations"],
        now=now,
        db_path=db_path,
    )
    if claimed is None:
        return {"no_job_available": True}
    if claimed["kind"] in {"failed", "exhausted"}:
        terminal_attempt = claimed.get("attempt")
        if terminal_attempt is not None:
            comparison_reliability.log_lifecycle_event(
                comparison_reliability.EVENT_ATTEMPT_TIMED_OUT,
                attempt=terminal_attempt,
                elapsed_ms=comparison_reliability.elapsed_ms_for(
                    terminal_attempt
                ),
            )
        if claimed["kind"] == "exhausted":
            comparison_reliability.log_detection_job_event(
                comparison_reliability.EVENT_JOB_CLAIM_EXHAUSTED,
                job=claimed["job"],
                attempt=terminal_attempt,
                source_attempt_id=(
                    terminal_attempt.get("attempt_id")
                    if terminal_attempt
                    else None
                ),
            )
        comparison_reliability.log_detection_job_event(
            comparison_reliability.EVENT_JOB_FAILED,
            job=claimed["job"],
            attempt=terminal_attempt,
        )
        return _worker_outcome(claimed["job"], terminal_attempt)

    job = claimed["job"]
    attempt = claimed["attempt"]
    claim_token = claimed["claim_token"]
    if claimed["kind"] == "reclaimed":
        source_attempt = claimed["source_attempt"]
        comparison_reliability.log_lifecycle_event(
            comparison_reliability.EVENT_ATTEMPT_TIMED_OUT,
            attempt=source_attempt,
            elapsed_ms=comparison_reliability.elapsed_ms_for(source_attempt),
        )
    comparison_reliability.log_lifecycle_event(
        comparison_reliability.EVENT_ATTEMPT_STARTED,
        attempt=attempt,
    )
    comparison_reliability.log_detection_job_event(
        (
            comparison_reliability.EVENT_JOB_RECLAIMED
            if claimed["kind"] == "reclaimed"
            else comparison_reliability.EVENT_JOB_CLAIMED
        ),
        job=job,
        attempt=attempt,
        source_attempt_id=(
            claimed["source_attempt"]["attempt_id"]
            if claimed["kind"] == "reclaimed"
            else None
        ),
        replacement_attempt_id=(
            attempt["attempt_id"]
            if claimed["kind"] == "reclaimed"
            else None
        ),
    )

    controller_factory = (
        heartbeat_controller_factory or DetectionJobHeartbeatController
    )
    heartbeat_controller = controller_factory(
        job_id=job["job_id"],
        worker_id=worker,
        claim_generation=job["claim_generation"],
        claim_token=claim_token,
        db_path=db_path,
        policy=policy,
    )
    heartbeat_controller.start()
    try:
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
                claim_generation=job["claim_generation"],
                claim_token=claim_token,
                job_now=now,
            )
        finally:
            heartbeat_controller.stop()
    except comparison_store.DetectionStateError as exc:
        if exc.code not in comparison_store.JOB_OWNERSHIP_LOST_CODES:
            raise
        current_job = comparison_store.get_detection_job(
            job["job_id"], db_path=db_path
        )
        attempted = comparison_store.get_detection_attempt(
            attempt["attempt_id"], db_path=db_path
        )
        return _worker_outcome(
            current_job or job,
            attempted,
            failure_code=exc.code,
            ownership_lost=True,
            attempted_attempt_id=attempt["attempt_id"],
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
    job: dict[str, Any],
    attempt: dict[str, Any] | None,
    *,
    failure_code: str | None = None,
    ownership_lost: bool = False,
    attempted_attempt_id: str | None = None,
) -> dict[str, Any]:
    """Narrow process/API-neutral outcome; never includes claim material."""
    return {
        "no_job_available": False,
        "job_id": job["job_id"],
        "attempt_id": attempted_attempt_id or job.get("attempt_id"),
        "job_status": job["status"],
        "attempt_status": attempt.get("status") if attempt else None,
        "claim_generation": job.get("claim_generation"),
        "lease_expires_at": job.get("lease_expires_at"),
        "result_hash": job.get("result_hash"),
        "failure_code": failure_code or job.get("failure_code"),
        "ownership_lost": ownership_lost,
        "worker_completed_responsibility": (
            not ownership_lost
            and job["status"]
            in {comparison_store.JOB_SUCCEEDED, comparison_store.JOB_FAILED}
        ),
    }


def heartbeat_job(
    *,
    job_id: str,
    worker_id: str,
    claim_generation: int,
    claim_token: str,
    db_path: str | Path | None = None,
    policy: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Extend one active claim and return a secret-free outcome."""
    policy = policy or detection_job_lease.POLICY
    updated = comparison_store.heartbeat_detection_job(
        job_id,
        worker_id=worker_id,
        claim_generation=claim_generation,
        claim_token=claim_token,
        heartbeat_extension_seconds=policy["heartbeat_extension_seconds"],
        now=now,
        db_path=db_path,
    )
    comparison_reliability.log_detection_job_event(
        comparison_reliability.EVENT_JOB_HEARTBEAT,
        job=updated,
        attempt=comparison_store.get_detection_attempt(
            updated["attempt_id"], db_path=db_path
        ),
    )
    return {
        "heartbeat_accepted": True,
        "job_id": updated["job_id"],
        "job_status": updated["status"],
        "claim_generation": updated["claim_generation"],
        "lease_expires_at": updated["lease_expires_at"],
    }
