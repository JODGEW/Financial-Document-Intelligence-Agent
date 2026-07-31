"""Process-level fault injection over the durable detection workflow.

Every other suite proves the state machine inside a single Python test process.
This one proves the same invariants survive **real operating-system process
termination**: an API killed after its enqueue commit, a worker killed after
its claim, a paused worker fenced by a reclaiming successor, a terminal commit
whose output was never delivered, retry state surviving restart, concurrent
processes racing for the same transition, and deliberate SQLite lock
contention.

Credential-free and offline: controlled filing fixtures in temporary
directories, fake embeddings only while seeding Chroma, stdlib SQLite, and a
child environment whose provider variables are explicitly neutralized. No
network, AWS, Bedrock, or Tavily call is reachable from any process started
here.

Everything is bounded. Every wait has a deadline, every child is terminated and
reaped in a ``finally``, and no test sleeps for a real lease to elapse when a
checked short policy or an injected clock will do.
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import comparison_detector
import comparison_store
import detection_job_lease
import runtime_fault_hooks
import runtime_readiness
from tests.auth_helpers import issue_test_access_token
from tests.helpers import fault_injection
from tests.helpers import process_harness as ph
from tests.test_comparison_detection_jobs import (  # noqa: F401
    ACTOR,
    CURR_ID,
    PREV_ID,
    _claim,
    _comparison,
    _enqueue,
    corpus,
    db,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- Checked short test policies ---------------------------------------------
# Same shape and validation as the shipped policies, with a lease short enough
# that expiry is reachable in a test and a heartbeat cadence (lease/3) that
# actually fires. Long real sleeps are never used: expiry is crossed with an
# injected clock wherever the transition under test is not itself time-based.

FAST_LEASE = {
    "policy_id": "lease_process_fault_test",
    "policy_version": "1",
    "lease_duration_seconds": 2,
    "heartbeat_extension_seconds": 2,
    "reclaim_grace_seconds": 0,
    "max_claim_generations": 3,
}
FAST_RETRY = {
    "policy_id": "retry_process_fault_test",
    "policy_version": "1",
    "max_retry_attempts": 2,
    "retry_delays_seconds": [5, 30],
    "retryable_failure_codes": ["detection_dependency_unavailable"],
}
NO_RETRY = {
    "policy_id": "retry_process_fault_test_terminal",
    "policy_version": "1",
    "max_retry_attempts": 0,
    "retry_delays_seconds": [],
    "retryable_failure_codes": ["detection_dependency_unavailable"],
}

CP = runtime_fault_hooks


# --- shared helpers -----------------------------------------------------------


def _rows(db_path, table: str) -> list[dict]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


def _snapshot(db_path) -> dict[str, list[dict]]:
    """Full contents of every workflow table, for exact before/after equality."""
    return {
        table: _rows(db_path, table)
        for table in (
            "comparisons",
            "comparison_detection_jobs",
            "comparison_detection_job_events",
            "comparison_detection_attempts",
            "comparison_detection_events",
            "comparison_results",
        )
    }


def _counts(db_path) -> dict[str, int]:
    return {table: len(rows) for table, rows in _snapshot(db_path).items()}


def _job_events(db_path, job_id: str, event_type: str) -> list[dict]:
    return [
        row
        for row in _rows(db_path, "comparison_detection_job_events")
        if row["job_id"] == job_id and row["event_type"] == event_type
    ]


def _integrity(db_path) -> tuple[str, list]:
    with closing(sqlite3.connect(db_path)) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
    return integrity, foreign


def _assert_integrity(db_path) -> None:
    integrity, foreign = _integrity(db_path)
    assert integrity == "ok"
    assert foreign == []


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    return detection_job_lease.parse_utc(value, field="timestamp")


@pytest.fixture
def gate(tmp_path):
    return ph.Gate(tmp_path / "gate")


def _worker(corpus, db, **kwargs):
    """Run one worker child to completion with the short checked policies."""
    kwargs.setdefault("lease_policy", FAST_LEASE)
    kwargs.setdefault("retry_policy", FAST_RETRY)
    return ph.run_worker(
        db_path=db,
        registry_path=corpus.registry,
        persist_dir=corpus.persist_dir,
        **kwargs,
    )


def _worker_bg(corpus, db, **kwargs):
    kwargs.setdefault("lease_policy", FAST_LEASE)
    kwargs.setdefault("retry_policy", FAST_RETRY)
    return ph.worker_process(
        db_path=db,
        registry_path=corpus.registry,
        persist_dir=corpus.persist_dir,
        **kwargs,
    )


# =============================================================================
# 1-2. The seam is unreachable from any ordinary runtime caller
# =============================================================================


def test_fault_injection_is_disabled_and_uninstallable_from_configuration():
    """Tests 1-2 (static half).

    The hook slot is empty in this process, no production module installs one,
    and the module itself reads no environment variable — so there is no
    configuration, policy, or deployment setting that could turn it on.
    """
    assert runtime_fault_hooks.installed() is False

    production = sorted(REPO_ROOT.glob("*.py")) + sorted(
        (REPO_ROOT / "scripts").glob("*.py")
    )
    offenders = []
    for path in production:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"install", "clear"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "runtime_fault_hooks"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], offenders

    # No ambient activation path inside the hook module itself.
    source = (REPO_ROOT / "runtime_fault_hooks.py").read_text(encoding="utf-8")
    for forbidden in ("os.environ", "getenv", "argparse", "open(", "Path("):
        assert forbidden not in source, forbidden


def test_checkpoint_call_sites_use_declared_names_and_no_claim_material():
    """Every production checkpoint uses a declared name and leaks nothing."""
    declared = set(runtime_fault_hooks.CHECKPOINTS)
    module_names = {
        name
        for name, value in vars(runtime_fault_hooks).items()
        if isinstance(value, str) and value in declared
    }
    seen: set[str] = set()
    for path in (
        REPO_ROOT / "comparison_detection_worker.py",
        REPO_ROOT / "comparison_detector.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "checkpoint"
            ):
                continue
            for keyword in node.keywords:
                assert keyword.arg not in runtime_fault_hooks.FORBIDDEN_CONTEXT_KEYS
            # Only the checkpoint-name expression matters; a call site may also
            # branch on unrelated store constants to choose between two names.
            names = [
                argument.attr
                for argument in ast.walk(node.args[0])
                if isinstance(argument, ast.Attribute)
                and isinstance(argument.value, ast.Name)
                and argument.value.id == "runtime_fault_hooks"
            ]
            assert names, ast.dump(node.args[0])
            for name in names:
                assert name in module_names, name
            seen.update(names)
    # Every declared checkpoint is actually wired somewhere.
    assert seen == module_names


def test_disabled_checkpoints_do_not_change_business_state(corpus, db):
    """Test 9 of section A: with no hook installed, behaviour is unchanged."""
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    assert runtime_fault_hooks.installed() is False
    process = _worker(corpus, db, worker_id="no-hook-worker", job_id=job["job_id"])
    assert process.returncode == 0, process.diagnostics()
    assert json.loads(process.stdout)["job_status"] == "succeeded"
    assert comparison_store.get_comparison(comparison_id, db)["status"] == "detected"
    assert comparison_store.get_result(comparison_id, db) is not None


def test_production_worker_cli_cannot_activate_a_checkpoint(corpus, db, gate):
    """Test 2: the shipped worker CLI has no flag and honours no environment.

    A fault plan that *would* stop the process is offered through every channel
    an operator or attacker controls; the run completes normally and the gate
    directory stays empty, proving nothing was installed.
    """
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    spec = gate.spec({CP.WORKER_AFTER_CLAIM_COMMIT: {"action": "exit"}})

    # An argv flag simply does not exist: argparse rejects it.
    rejected = subprocess.run(
        [
            sys.executable,
            "scripts/run_comparison_detection_worker.py",
            "--db-path",
            str(db),
            "--registry-path",
            str(corpus.registry),
            "--worker-id",
            "cli-flag-probe",
            "--once",
            "--faults",
            spec,
        ],
        cwd=str(REPO_ROOT),
        env=ph.child_env(db_path=db, registry_path=corpus.registry),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert rejected.returncode == 2
    assert "unrecognized arguments" in rejected.stderr

    # And no environment variable installs one either.
    env = ph.child_env(db_path=db, registry_path=corpus.registry)
    env.update(
        {
            "RUNTIME_FAULT_HOOKS": spec,
            "FDIA_FAULTS": spec,
            "FAULT_CHECKPOINT": CP.WORKER_AFTER_CLAIM_COMMIT,
            "FAULT_ACTION": "exit",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_comparison_detection_worker.py",
            "--db-path",
            str(db),
            "--registry-path",
            str(corpus.registry),
            "--worker-id",
            "cli-env-probe",
            "--job-id",
            job["job_id"],
            "--once",
            "--json",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["jobStatus"] == "succeeded"
    assert list(gate.directory.iterdir()) == []


def test_ordinary_api_callers_cannot_activate_a_checkpoint(corpus, db, gate):
    """Test 1: no header, query parameter, or body field installs a hook."""
    comparison_store.init_db(db)
    token = issue_test_access_token()
    spec = gate.spec(
        {CP.API_AFTER_ENQUEUE_COMMIT_BEFORE_RESPONSE: {"action": "exit"}}
    )
    with ph.api_process(
        db_path=db, registry_path=corpus.registry, persist_dir=corpus.persist_dir
    ) as api:
        status, created = api.request(
            "POST",
            "/api/comparisons",
            token=token,
            body={
                "previousFilingId": PREV_ID,
                "currentFilingId": CURR_ID,
                "faults": spec,
                "faultCheckpoint": CP.API_BEFORE_ENQUEUE,
            },
        )
        assert status == 201
        comparison_id = created["comparison"]["comparisonId"]

        import urllib.request

        request = urllib.request.Request(
            f"{api.base_url}/api/comparisons/{comparison_id}/detect"
            f"?faults={CP.API_BEFORE_ENQUEUE}&fault_action=exit",
            data=b"{}",
            method="POST",
        )
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Fault-Checkpoint", CP.API_BEFORE_ENQUEUE)
        request.add_header("X-Runtime-Fault-Hooks", spec)
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.status == 202

        # The process is still alive and nothing was ever announced.
        assert api.process.alive()
        assert api.request("GET", "/api/health")[0] == 200

    assert list(gate.directory.iterdir()) == []
    assert len(_rows(db, "comparison_detection_jobs")) == 1


# =============================================================================
# 3-4. Lost API response (section C)
# =============================================================================


def test_api_killed_after_enqueue_commit_keeps_exactly_one_durable_job(
    corpus, db, gate
):
    """Tests 3-4: the durable job outlives the process that created it.

    The API is stopped after its enqueue transaction commits but before the
    response is delivered — the client learns nothing. A fresh API process over
    the same SQLite and registry state must then answer the repeated request
    idempotently rather than queueing a second job.
    """
    comparison_store.init_db(db)
    token = issue_test_access_token()
    spec = gate.spec(
        {CP.API_AFTER_ENQUEUE_COMMIT_BEFORE_RESPONSE: {"action": "block"}}
    )

    with ph.api_process(
        db_path=db,
        registry_path=corpus.registry,
        persist_dir=corpus.persist_dir,
        faults=spec,
    ) as api:
        status, created = api.request(
            "POST",
            "/api/comparisons",
            token=token,
            body={"previousFilingId": PREV_ID, "currentFilingId": CURR_ID},
        )
        assert status == 201
        comparison_id = created["comparison"]["comparisonId"]

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                api.request,
                "POST",
                f"/api/comparisons/{comparison_id}/detect",
                token=token,
            )
            reached = gate.wait_reached(
                CP.API_AFTER_ENQUEUE_COMMIT_BEFORE_RESPONSE
            )
            assert reached["comparison_id"] == comparison_id
            # SIGKILL: not caught, no cleanup, no response ever written.
            ph.kill_now(api.process)
            with pytest.raises(Exception):
                pending.result(timeout=30)

    # Durable state, read from this (separate) process.
    jobs = _rows(db, "comparison_detection_jobs")
    assert len(jobs) == 1
    job_id = jobs[0]["job_id"]
    assert jobs[0]["status"] == comparison_store.JOB_QUEUED
    assert jobs[0]["attempt_id"] is None
    assert len(_job_events(db, job_id, comparison_store.EVENT_JOB_QUEUED)) == 1
    assert (
        comparison_store.get_comparison(comparison_id, db)["status"]
        == "queued_for_detection"
    )
    # No attempt and no detector execution happened in the API process.
    assert _rows(db, "comparison_detection_attempts") == []
    assert _rows(db, "comparison_results") == []

    # A restarted API answers the repeated request idempotently.
    with ph.api_process(
        db_path=db, registry_path=corpus.registry, persist_dir=corpus.persist_dir
    ) as restarted:
        status, body = restarted.request(
            "POST",
            f"/api/comparisons/{comparison_id}/detect",
            token=token,
        )
        assert status == 202
        assert body["created"] is False
        assert body["jobId"] == job_id
        assert body["jobStatus"] == comparison_store.JOB_QUEUED
        assert body["comparisonStatus"] == "queued_for_detection"

    assert len(_rows(db, "comparison_detection_jobs")) == 1
    assert len(_job_events(db, job_id, comparison_store.EVENT_JOB_QUEUED)) == 1
    assert _rows(db, "comparison_detection_attempts") == []
    _assert_integrity(db)


# =============================================================================
# 5-8. Worker crash after claim, and reclaim (section D)
# =============================================================================


def test_worker_killed_after_claim_leaves_recoverable_running_state(
    corpus, db, gate
):
    """Tests 5-8: claimed work survives its worker, and only expiry frees it."""
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    spec = gate.spec({CP.WORKER_AFTER_CLAIM_COMMIT: {"action": "exit"}})

    crashed = _worker(
        corpus,
        db,
        worker_id="crash-after-claim",
        job_id=job["job_id"],
        faults=spec,
    )
    assert crashed.returncode == fault_injection.CHECKPOINT_EXIT_CODE
    assert crashed.stdout == ""

    # Test 6: the running state is durable, read from another process.
    stored = comparison_store.get_detection_job(job["job_id"], db)
    assert stored["status"] == comparison_store.JOB_RUNNING
    assert stored["claim_generation"] == 1
    assert stored["worker_id"] == "crash-after-claim"
    assert stored["lease_started_at"] and stored["lease_expires_at"]
    assert stored["heartbeat_at"]
    attempt = comparison_store.get_detection_attempt(stored["attempt_id"], db)
    assert attempt["status"] == comparison_store.ATTEMPT_RUNNING
    assert (
        comparison_store.get_comparison(comparison_id, db)["status"] == "detecting"
    )
    assert len(_job_events(db, job["job_id"], comparison_store.EVENT_JOB_CLAIMED)) == 1
    assert [
        row["event_type"]
        for row in _rows(db, "comparison_detection_events")
        if row["attempt_id"] == attempt["attempt_id"]
    ] == ["detection_started"]
    assert _rows(db, "comparison_results") == []
    for terminal in (
        comparison_store.EVENT_JOB_SUCCEEDED,
        comparison_store.EVENT_JOB_FAILED,
    ):
        assert _job_events(db, job["job_id"], terminal) == []

    expires = _parse(stored["lease_expires_at"])
    before = _snapshot(db)

    # Test 7: before expiry, another process cannot claim or reclaim it.
    early = _worker(
        corpus,
        db,
        worker_id="too-early-worker",
        now=_iso(expires - timedelta(milliseconds=500)),
    )
    assert early.returncode == 0, early.diagnostics()
    assert json.loads(early.stdout) == {"no_job_available": True}
    assert _snapshot(db) == before

    # Test 8: strictly after expiry plus grace, a later one-shot process
    # reclaims — and that reclaim executes exactly once.
    due = expires + timedelta(
        seconds=FAST_LEASE["reclaim_grace_seconds"] + 1
    )
    reclaimed = _worker(
        corpus, db, worker_id="reclaiming-worker", now=_iso(due)
    )
    assert reclaimed.returncode == 0, reclaimed.diagnostics()
    outcome = json.loads(reclaimed.stdout)
    assert outcome["claim_type"] == "reclaim"
    assert outcome["job_status"] == comparison_store.JOB_SUCCEEDED
    assert outcome["claim_generation"] == 2

    assert (
        comparison_store.get_detection_attempt(attempt["attempt_id"], db)["status"]
        == comparison_store.ATTEMPT_TIMED_OUT
    )
    attempts = _rows(db, "comparison_detection_attempts")
    assert len(attempts) == 2
    replacement = [
        row for row in attempts if row["attempt_id"] != attempt["attempt_id"]
    ][0]
    assert replacement["status"] == comparison_store.ATTEMPT_SUCCEEDED
    assert len(_rows(db, "comparison_results")) == 1
    assert len(_job_events(db, job["job_id"], comparison_store.EVENT_JOB_RECLAIMED)) == 1
    assert (
        comparison_store.get_comparison(comparison_id, db)["status"] == "detected"
    )
    _assert_integrity(db)


# =============================================================================
# 9-11. Paused old worker fencing (section E)
# =============================================================================


def _fence_setup(corpus, db, gate, *, tail_action: dict):
    """Pause a real worker mid-execution, let another reclaim, then release it.

    Returns ``(comparison_id, job_id, source_attempt_id, worker_a_outcome)``.
    """
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    spec = gate.spec(
        {
            # Suppressing the heartbeat is what lets A's lease lapse while A
            # itself keeps running — the state a crash cannot reproduce.
            CP.HEARTBEAT_BEFORE_EXTEND: {"action": "block", "once": True},
            CP.WORKER_AFTER_DETECTOR_COMPUTE_BEFORE_TERMINAL_COMMIT: tail_action,
        }
    )

    with _worker_bg(
        corpus,
        db,
        worker_id="worker-a-paused",
        job_id=job["job_id"],
        faults=spec,
    ) as worker_a:
        gate.wait_reached(
            CP.WORKER_AFTER_DETECTOR_COMPUTE_BEFORE_TERMINAL_COMMIT
        )
        claimed = comparison_store.get_detection_job(job["job_id"], db)
        assert claimed["claim_generation"] == 1
        source_attempt_id = claimed["attempt_id"]
        expires = _parse(claimed["lease_expires_at"])

        # Worker B reclaims after expiry plus grace and runs to completion.
        worker_b = _worker(
            corpus,
            db,
            worker_id="worker-b-reclaimer",
            now=_iso(
                expires + timedelta(seconds=FAST_LEASE["reclaim_grace_seconds"] + 1)
            ),
        )
        assert worker_b.returncode == 0, worker_b.diagnostics()
        b_outcome = json.loads(worker_b.stdout)
        assert b_outcome["claim_type"] == "reclaim"
        assert b_outcome["claim_generation"] == 2
        assert b_outcome["job_status"] == comparison_store.JOB_SUCCEEDED

        # A is still alive and still believes it owns the work. Release its
        # heartbeat first so the fenced heartbeat is attempted, then its commit.
        assert worker_a.alive()
        gate.wait_reached(CP.HEARTBEAT_BEFORE_EXTEND)
        gate.release(CP.HEARTBEAT_BEFORE_EXTEND)
        gate.release(CP.WORKER_AFTER_DETECTOR_COMPUTE_BEFORE_TERMINAL_COMMIT)
        worker_a.wait(timeout=60)

    return comparison_id, job["job_id"], source_attempt_id, worker_a


def test_paused_worker_is_fenced_after_another_process_reclaims(corpus, db, gate):
    """Tests 9-10: the old generation cannot heartbeat and cannot succeed."""
    comparison_id, job_id, source_attempt_id, worker_a = _fence_setup(
        corpus, db, gate, tail_action={"action": "block"}
    )
    assert worker_a.returncode == 0, worker_a.diagnostics()
    outcome = json.loads(worker_a.stdout)
    assert outcome["ownership_lost"] is True
    assert outcome["worker_completed_responsibility"] is False
    assert outcome["failure_code"] in comparison_store.JOB_OWNERSHIP_LOST_CODES

    # A reached its heartbeat and was released, yet no generation-1 heartbeat
    # was ever recorded: it could not heartbeat.
    assert gate.reached(CP.HEARTBEAT_BEFORE_EXTEND)
    heartbeats = _job_events(db, job_id, comparison_store.EVENT_JOB_HEARTBEAT)
    assert [row for row in heartbeats if row["claim_generation"] == 1] == []

    # B remains the owner and produced the only terminal outcome.
    job = comparison_store.get_detection_job(job_id, db)
    assert job["status"] == comparison_store.JOB_SUCCEEDED
    assert job["claim_generation"] == 2
    assert job["worker_id"] == "worker-b-reclaimer"
    assert job["attempt_id"] != source_attempt_id

    assert (
        comparison_store.get_detection_attempt(source_attempt_id, db)["status"]
        == comparison_store.ATTEMPT_TIMED_OUT
    )
    replacement = comparison_store.get_detection_attempt(job["attempt_id"], db)
    assert replacement["status"] == comparison_store.ATTEMPT_SUCCEEDED

    # Exactly one result, and every hash and state agrees.
    results = _rows(db, "comparison_results")
    assert len(results) == 1
    assert results[0]["result_hash"] == job["result_hash"] == replacement["result_hash"]
    assert len(_job_events(db, job_id, comparison_store.EVENT_JOB_SUCCEEDED)) == 1
    assert (
        comparison_store.get_comparison(comparison_id, db)["status"] == "detected"
    )
    _assert_integrity(db)


def test_fenced_old_worker_cannot_fail_the_replacement(corpus, db, gate):
    """Test 11: a fenced worker cannot commit a failure either.

    Worker A raises the one allowlisted transient failure *after* being fenced.
    That failure must not touch the replacement attempt, the succeeded job, or
    the stored result.
    """
    comparison_id, job_id, source_attempt_id, worker_a = _fence_setup(
        corpus,
        db,
        gate,
        tail_action={"action": "block"},
    )
    before = _snapshot(db)

    # A already lost ownership; re-running its finalization is impossible
    # because the generation moved on. Prove the durable state is untouched.
    job = comparison_store.get_detection_job(job_id, db)
    assert job["status"] == comparison_store.JOB_SUCCEEDED
    assert job["failure_code"] is None
    assert job["claim_generation"] == 2
    replacement_id = job["attempt_id"]

    # An explicit generation-1 failure attempt, from this process, is refused
    # for the same reason the child's was: the fence is in the transaction.
    with pytest.raises(comparison_store.DetectionStateError) as raised:
        comparison_store.fail_detection_job(
            job_id,
            source_attempt_id,
            worker_id="worker-a-paused",
            claim_generation=1,
            claim_token="stale-token-never-valid",
            failure_code="detection_dependency_unavailable",
            failure_summary="a detection dependency was temporarily unavailable",
            retry_policy=FAST_RETRY,
            max_claim_generations=FAST_LEASE["max_claim_generations"],
            db_path=db,
        )
    assert raised.value.code in comparison_store.JOB_OWNERSHIP_LOST_CODES

    assert _snapshot(db) == before
    assert (
        comparison_store.get_detection_attempt(replacement_id, db)["status"]
        == comparison_store.ATTEMPT_SUCCEEDED
    )
    assert len(_rows(db, "comparison_results")) == 1
    assert (
        comparison_store.get_comparison(comparison_id, db)["status"] == "detected"
    )
    assert worker_a.returncode == 0
    _assert_integrity(db)


# =============================================================================
# 12-14. Terminal commit with lost output (section F)
# =============================================================================


def test_terminal_success_survives_lost_worker_output(corpus, db, gate):
    """Tests 12-13: the commit is the record; the CLI's output is not."""
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    spec = gate.spec(
        {CP.WORKER_AFTER_TERMINAL_COMMIT_BEFORE_OUTPUT: {"action": "exit"}}
    )

    killed = _worker(
        corpus,
        db,
        worker_id="terminal-lost-output",
        job_id=job["job_id"],
        faults=spec,
    )
    assert killed.returncode == fault_injection.CHECKPOINT_EXIT_CODE
    assert killed.stdout == ""  # the client learned nothing

    stored = comparison_store.get_detection_job(job["job_id"], db)
    assert stored["status"] == comparison_store.JOB_SUCCEEDED
    attempt = comparison_store.get_detection_attempt(stored["attempt_id"], db)
    assert attempt["status"] == comparison_store.ATTEMPT_SUCCEEDED
    assert (
        comparison_store.get_comparison(comparison_id, db)["status"] == "detected"
    )
    result = comparison_store.get_result(comparison_id, db)
    assert result is not None
    before = _snapshot(db)

    # A second invocation — through the shipped CLI — finds nothing to do.
    again = _worker(
        corpus, db, worker_id="second-invocation", use_cli=True
    )
    assert again.returncode == 0, again.diagnostics()
    assert json.loads(again.stdout) == {"noJobAvailable": True}

    assert _snapshot(db) == before
    assert len(_rows(db, "comparison_detection_attempts")) == 1
    assert len(_rows(db, "comparison_results")) == 1
    assert len(_job_events(db, job["job_id"], comparison_store.EVENT_JOB_SUCCEEDED)) == 1
    _assert_integrity(db)


def test_terminal_domain_failure_survives_lost_worker_output(corpus, db, gate):
    """Test 14: the same guarantee for a durably persisted terminal failure."""
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    spec = gate.spec(
        {
            CP.WORKER_BEFORE_DETECTOR_COMPUTE: {"action": "raise_transient"},
            CP.WORKER_AFTER_TERMINAL_COMMIT_BEFORE_OUTPUT: {"action": "exit"},
        }
    )

    killed = _worker(
        corpus,
        db,
        worker_id="terminal-failure-lost",
        job_id=job["job_id"],
        faults=spec,
        retry_policy=NO_RETRY,  # no retry budget: the first failure is terminal
    )
    assert killed.returncode == fault_injection.CHECKPOINT_EXIT_CODE
    assert killed.stdout == ""

    stored = comparison_store.get_detection_job(job["job_id"], db)
    assert stored["status"] == comparison_store.JOB_FAILED
    assert stored["failure_code"]
    attempt = comparison_store.get_detection_attempt(stored["attempt_id"], db)
    assert attempt["status"] == comparison_store.ATTEMPT_FAILED
    assert attempt["failure_code"] == "detection_dependency_unavailable"
    assert _rows(db, "comparison_results") == []
    before = _snapshot(db)

    again = _worker(corpus, db, worker_id="second-after-failure", use_cli=True)
    assert again.returncode == 0, again.diagnostics()
    assert json.loads(again.stdout) == {"noJobAvailable": True}
    assert _snapshot(db) == before
    assert len(_rows(db, "comparison_detection_attempts")) == 1
    _assert_integrity(db)


# =============================================================================
# 15-17. Retry-wait restart (section G)
# =============================================================================


def test_retry_wait_survives_restart_and_is_claimable_only_when_due(
    corpus, db, gate
):
    """Tests 15-17: scheduled retry state is durable, and time alone runs nothing."""
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    spec = gate.spec(
        {
            CP.WORKER_BEFORE_DETECTOR_COMPUTE: {"action": "raise_transient"},
            CP.WORKER_AFTER_RETRY_SCHEDULE_COMMIT: {"action": "exit"},
        }
    )

    killed = _worker(
        corpus,
        db,
        worker_id="transient-then-crash",
        job_id=job["job_id"],
        faults=spec,
    )
    assert killed.returncode == fault_injection.CHECKPOINT_EXIT_CODE
    assert killed.stdout == ""

    # Test 15: retry_wait survived the process that scheduled it.
    stored = comparison_store.get_detection_job(job["job_id"], db)
    assert stored["status"] == comparison_store.JOB_RETRY_WAIT
    assert stored["retry_count"] == 1
    assert stored["next_attempt_at"]
    assert stored["last_failure_code"] == "detection_dependency_unavailable"
    assert stored["last_failure_classification"] == "retryable_transient"
    assert (
        comparison_store.get_comparison(comparison_id, db)["status"]
        == "waiting_for_detection_retry"
    )
    first_attempt = comparison_store.get_detection_attempt(
        _rows(db, "comparison_detection_attempts")[0]["attempt_id"], db
    )
    assert first_attempt["status"] == comparison_store.ATTEMPT_FAILED
    assert _rows(db, "comparison_results") == []
    assert len(_rows(db, "comparison_detection_attempts")) == 1

    due = _parse(stored["next_attempt_at"])
    before = _snapshot(db)

    # Test 16: a worker started before the due time mutates nothing.
    early = _worker(
        corpus,
        db,
        worker_id="before-due-worker",
        now=_iso(due - timedelta(milliseconds=500)),
    )
    assert early.returncode == 0, early.diagnostics()
    assert json.loads(early.stdout) == {"no_job_available": True}
    assert _snapshot(db) == before

    # Test 17: at the due instant, exactly one replacement attempt is created
    # and runs through the existing fenced path.
    on_time = _worker(corpus, db, worker_id="due-worker", now=_iso(due))
    assert on_time.returncode == 0, on_time.diagnostics()
    outcome = json.loads(on_time.stdout)
    assert outcome["claim_type"] == "retry"
    assert outcome["claim_generation"] == 2
    assert outcome["retry_count"] == 1
    assert outcome["job_status"] == comparison_store.JOB_SUCCEEDED

    attempts = _rows(db, "comparison_detection_attempts")
    assert len(attempts) == 2
    assert len(_job_events(db, job["job_id"], comparison_store.EVENT_JOB_RETRY_SCHEDULED)) == 1
    assert len(_job_events(db, job["job_id"], comparison_store.EVENT_JOB_RETRY_CLAIMED)) == 1
    assert len(_rows(db, "comparison_results")) == 1
    assert (
        comparison_store.get_comparison(comparison_id, db)["status"] == "detected"
    )
    _assert_integrity(db)


# =============================================================================
# 18-21. Concurrent processes (section H)
# =============================================================================

RACE_ITERATIONS = 2
RACE_WIDTH = 3


def _race_db(corpus, tmp_path, iteration: int) -> Path:
    """A fresh database per race iteration.

    The comparison id is deterministic from the filing pair, so reusing one
    database would make the second iteration idempotently reopen the first
    iteration's already-detected comparison instead of racing a new enqueue.
    """
    return tmp_path / f"race-{iteration}.db"


def test_concurrent_process_enqueue_produces_one_job(corpus, tmp_path):
    """Test 18: identical authenticated enqueues race to a single durable job."""
    token = issue_test_access_token()
    for iteration in range(RACE_ITERATIONS):
        db = _race_db(corpus, tmp_path, iteration)
        comparison_store.init_db(db)
        with ph.api_process(
            db_path=db,
            registry_path=corpus.registry,
            persist_dir=corpus.persist_dir,
        ) as api:
            status, created = api.request(
                "POST",
                "/api/comparisons",
                token=token,
                body={"previousFilingId": PREV_ID, "currentFilingId": CURR_ID},
            )
            assert status == 201
            comparison_id = created["comparison"]["comparisonId"]
            with ThreadPoolExecutor(max_workers=RACE_WIDTH + 1) as pool:
                responses = list(
                    pool.map(
                        lambda _: api.request(
                            "POST",
                            f"/api/comparisons/{comparison_id}/detect",
                            token=token,
                        ),
                        range(RACE_WIDTH + 1),
                    )
                )
        assert {status for status, _ in responses} == {202}
        assert len({body["jobId"] for _, body in responses}) == 1
        assert sum(1 for _, body in responses if body["created"]) == 1

        jobs = _rows(db, "comparison_detection_jobs")
        assert len(jobs) == 1
        assert (
            len(
                _job_events(
                    db, jobs[0]["job_id"], comparison_store.EVENT_JOB_QUEUED
                )
            )
            == 1
        )
        assert _rows(db, "comparison_detection_attempts") == []
        _assert_integrity(db)


def _race_workers(corpus, db, *, prefix: str, now: str | None = None):
    """Start RACE_WIDTH real worker processes at once and collect outcomes."""
    processes = [
        _worker_bg(
            corpus,
            db,
            worker_id=f"{prefix}-{index}",
            now=now,
        )
        for index in range(RACE_WIDTH)
    ]
    entered = [context.__enter__() for context in processes]
    try:
        for process in entered:
            process.wait(timeout=90)
        return [
            json.loads(process.stdout) if process.stdout else None
            for process in entered
        ]
    finally:
        for context in processes:
            context.__exit__(None, None, None)


def test_concurrent_process_claim_produces_one_attempt(corpus, tmp_path):
    """Test 19: only one process may claim a queued job."""
    for iteration in range(RACE_ITERATIONS):
        db = _race_db(corpus, tmp_path, iteration)
        comparison_id = _comparison(corpus, db)
        job = _enqueue(corpus, db, comparison_id)["job"]
        outcomes = _race_workers(corpus, db, prefix="claim-race")

        winners = [item for item in outcomes if not item["no_job_available"]]
        assert len(winners) == 1
        assert winners[0]["job_status"] == comparison_store.JOB_SUCCEEDED
        assert winners[0]["claim_generation"] == 1

        attempts = [
            row
            for row in _rows(db, "comparison_detection_attempts")
            if row["comparison_id"] == comparison_id
        ]
        assert len(attempts) == 1
        assert len(_job_events(db, job["job_id"], comparison_store.EVENT_JOB_CLAIMED)) == 1
        assert len(_job_events(db, job["job_id"], comparison_store.EVENT_JOB_SUCCEEDED)) == 1
        assert (
            len(
                [
                    row
                    for row in _rows(db, "comparison_results")
                    if row["comparison_id"] == comparison_id
                ]
            )
            == 1
        )
        _assert_integrity(db)


def test_concurrent_process_reclaim_produces_one_replacement(corpus, tmp_path, gate):
    """Test 20: only one process may reclaim an expired claim."""
    for iteration in range(RACE_ITERATIONS):
        db = _race_db(corpus, tmp_path, iteration)
        comparison_id = _comparison(corpus, db)
        job = _enqueue(corpus, db, comparison_id)["job"]
        spec = gate.spec({CP.WORKER_AFTER_CLAIM_COMMIT: {"action": "exit"}})
        crashed = _worker(
            corpus,
            db,
            worker_id=f"reclaim-source-{iteration}",
            job_id=job["job_id"],
            faults=spec,
        )
        assert crashed.returncode == fault_injection.CHECKPOINT_EXIT_CODE
        for marker in gate.directory.iterdir():
            marker.unlink()

        stored = comparison_store.get_detection_job(job["job_id"], db)
        due = _parse(stored["lease_expires_at"]) + timedelta(
            seconds=FAST_LEASE["reclaim_grace_seconds"] + 1
        )
        outcomes = _race_workers(
            corpus, db, prefix=f"reclaim-race-{iteration}", now=_iso(due)
        )

        winners = [item for item in outcomes if not item["no_job_available"]]
        assert len(winners) == 1
        assert winners[0]["claim_type"] == "reclaim"
        assert winners[0]["claim_generation"] == 2
        assert winners[0]["job_status"] == comparison_store.JOB_SUCCEEDED

        attempts = [
            row
            for row in _rows(db, "comparison_detection_attempts")
            if row["comparison_id"] == comparison_id
        ]
        assert len(attempts) == 2  # no generation gap, no duplicate replacement
        assert len(_job_events(db, job["job_id"], comparison_store.EVENT_JOB_RECLAIMED)) == 1
        assert (
            len(
                [
                    row
                    for row in _rows(db, "comparison_results")
                    if row["comparison_id"] == comparison_id
                ]
            )
            == 1
        )
        _assert_integrity(db)


def test_concurrent_process_retry_claim_produces_one_replacement(
    corpus, tmp_path, gate
):
    """Test 21: only one process may claim a due retry."""
    for iteration in range(RACE_ITERATIONS):
        db = _race_db(corpus, tmp_path, iteration)
        comparison_id = _comparison(corpus, db)
        job = _enqueue(corpus, db, comparison_id)["job"]
        spec = gate.spec(
            {
                CP.WORKER_BEFORE_DETECTOR_COMPUTE: {"action": "raise_transient"},
                CP.WORKER_AFTER_RETRY_SCHEDULE_COMMIT: {"action": "exit"},
            }
        )
        scheduled = _worker(
            corpus,
            db,
            worker_id=f"retry-source-{iteration}",
            job_id=job["job_id"],
            faults=spec,
        )
        assert scheduled.returncode == fault_injection.CHECKPOINT_EXIT_CODE
        for marker in gate.directory.iterdir():
            marker.unlink()

        stored = comparison_store.get_detection_job(job["job_id"], db)
        assert stored["status"] == comparison_store.JOB_RETRY_WAIT
        due = _parse(stored["next_attempt_at"])
        outcomes = _race_workers(
            corpus, db, prefix=f"retry-race-{iteration}", now=_iso(due)
        )

        winners = [item for item in outcomes if not item["no_job_available"]]
        assert len(winners) == 1
        assert winners[0]["claim_type"] == "retry"
        assert winners[0]["claim_generation"] == 2
        assert winners[0]["retry_count"] == 1

        attempts = [
            row
            for row in _rows(db, "comparison_detection_attempts")
            if row["comparison_id"] == comparison_id
        ]
        assert len(attempts) == 2
        assert len(_job_events(db, job["job_id"], comparison_store.EVENT_JOB_RETRY_CLAIMED)) == 1
        _assert_integrity(db)


# =============================================================================
# 22-28. SQLite lock contention (section I)
# =============================================================================

# Longer than the store's documented busy_timeout (5000 ms), so the acquisition
# genuinely times out rather than merely queueing. The value in the store is
# left untouched: inspection shows it is both documented and deliberate.
LOCK_HOLD_SECONDS = 7.0
# The worker CLI must import the application before it touches SQLite, so its
# holder must outlast (process start + import) plus the full busy timeout.
CLI_LOCK_HOLD_SECONDS = 12.0

# Lock contention is not a lease test. These cases use the shipped lease
# durations so a claim cannot expire merely because the lock was held for
# longer than the deliberately tiny FAST_LEASE window.
LOCK_LEASE = {
    "policy_id": "lease_lock_contention_test",
    "policy_version": "1",
    "lease_duration_seconds": 120,
    "heartbeat_extension_seconds": 120,
    "reclaim_grace_seconds": 15,
    "max_claim_generations": 3,
}


def _lock_case(corpus, tmp_path, name: str):
    """Build one isolated database in the precondition a lock case needs."""
    db_path = tmp_path / f"lock-{name}.db"
    comparison_id = _comparison(corpus, db_path)
    if name == "enqueue":
        return db_path, lambda: _enqueue(corpus, db_path, comparison_id)

    job = _enqueue(corpus, db_path, comparison_id)["job"]
    inputs = comparison_detector.resolve_detection_inputs(
        comparison_id, db_path=db_path, registry_path=corpus.registry
    )

    def _claim_call(*, now=None, worker="lock-worker"):
        return comparison_store.claim_detection_job(
            job_id=job["job_id"],
            worker_id=worker,
            detector_version=comparison_detector.DETECTOR_VERSION,
            workflow_version=comparison_store.WORKFLOW_VERSION,
            previous_source_hash=inputs["previous_hash"],
            current_source_hash=inputs["current_hash"],
            lease_duration_seconds=LOCK_LEASE["lease_duration_seconds"],
            reclaim_grace_seconds=LOCK_LEASE["reclaim_grace_seconds"],
            max_claim_generations=LOCK_LEASE["max_claim_generations"],
            max_retry_attempts=FAST_RETRY["max_retry_attempts"],
            now=now,
            db_path=db_path,
        )

    if name == "claim":
        return db_path, _claim_call

    claimed = _claim_call()
    token = claimed["claim_token"]
    generation = claimed["job"]["claim_generation"]
    attempt_id = claimed["attempt"]["attempt_id"]

    if name == "heartbeat":
        return db_path, lambda: comparison_store.heartbeat_detection_job(
            job["job_id"],
            worker_id="lock-worker",
            claim_generation=generation,
            claim_token=token,
            heartbeat_extension_seconds=LOCK_LEASE["heartbeat_extension_seconds"],
            db_path=db_path,
        )

    if name == "reclaim":
        expires = _parse(claimed["job"]["lease_expires_at"])
        due = expires + timedelta(
            seconds=LOCK_LEASE["reclaim_grace_seconds"] + 1
        )
        return db_path, lambda: _claim_call(now=due, worker="lock-reclaimer")

    if name == "terminal_finalization":
        # A minimal well-formed payload: this case exercises the transaction's
        # lock behaviour, not detector output, and the store still requires the
        # hash to be the canonical one for the JSON it is given.
        result_json = json.dumps(
            {"comparison_id": comparison_id, "probe": "lock_contention"},
            sort_keys=True,
        )
        return db_path, lambda: comparison_store.complete_detection_job(
            job["job_id"],
            attempt_id,
            worker_id="lock-worker",
            claim_generation=generation,
            claim_token=token,
            result_json=result_json,
            result_hash=comparison_store._canonical_result_hash(result_json),
            detector_version=comparison_detector.DETECTOR_VERSION,
            workflow_version=comparison_store.WORKFLOW_VERSION,
            previous_source_hash=inputs["previous_hash"],
            current_source_hash=inputs["current_hash"],
            db_path=db_path,
        )

    def _fail_call():
        return comparison_store.fail_detection_job(
            job["job_id"],
            attempt_id,
            worker_id="lock-worker",
            claim_generation=generation,
            claim_token=token,
            failure_code="detection_dependency_unavailable",
            failure_summary="a detection dependency was temporarily unavailable",
            retry_policy=FAST_RETRY,
            max_claim_generations=LOCK_LEASE["max_claim_generations"],
            db_path=db_path,
        )

    if name == "retry_scheduling":
        return db_path, _fail_call

    if name == "due_retry_claim":
        scheduled = _fail_call()
        due = _parse(scheduled["job"]["next_attempt_at"])
        return db_path, lambda: _claim_call(now=due, worker="lock-retry")

    raise AssertionError(f"unknown lock case: {name}")


LOCK_CASES = (
    "enqueue",
    "claim",
    "heartbeat",
    "reclaim",
    "retry_scheduling",
    "due_retry_claim",
    "terminal_finalization",
)


def test_lock_contention_never_partially_applies_any_workflow_operation(
    corpus, tmp_path
):
    """Tests 22-28, run in parallel over isolated databases to stay bounded.

    Each case holds a deliberate write transaction for longer than the store's
    busy timeout, drives one workflow operation into that contention, and
    proves the operation either fully applied or did not apply at all — never
    half. The busy timeout itself is not modified: inspection showed the
    existing 5000 ms value is documented and deliberate.
    """

    def _run(name: str) -> tuple[str, str]:
        db_path, operation = _lock_case(corpus, tmp_path, name)
        before = _snapshot(db_path)
        ready = tmp_path / f"lock-{name}.ready"
        with ph.lock_holder(
            db_path=db_path, hold_seconds=LOCK_HOLD_SECONDS, ready_file=ready
        ):
            with pytest.raises(sqlite3.OperationalError) as raised:
                operation()
            assert "locked" in str(raised.value) or "busy" in str(raised.value)
            # Nothing partially applied while the lock was still held.
            assert _snapshot(db_path) == before

        # After release, the explicit retry reaches a valid state.
        operation()
        after = _snapshot(db_path)
        assert after != before
        integrity, foreign = _integrity(db_path)
        assert integrity == "ok" and foreign == []
        return name, "ok"

    with ThreadPoolExecutor(max_workers=len(LOCK_CASES)) as pool:
        results = dict(pool.map(_run, LOCK_CASES))
    assert results == {name: "ok" for name in LOCK_CASES}


def test_lock_contention_surfaces_stay_safe_and_retryable(corpus, db, tmp_path):
    """Tests 22-28 (surface half) plus test 29 for the contended paths.

    A timed-out lock acquisition must reach the client as a stable
    infrastructure failure — never SQL, a path, or a raw SQLite exception.
    """
    comparison_store.init_db(db)
    token = issue_test_access_token()
    ready = tmp_path / "surface.ready"

    with ph.api_process(
        db_path=db, registry_path=corpus.registry, persist_dir=corpus.persist_dir
    ) as api:
        status, created = api.request(
            "POST",
            "/api/comparisons",
            token=token,
            body={"previousFilingId": PREV_ID, "currentFilingId": CURR_ID},
        )
        comparison_id = created["comparison"]["comparisonId"]

        # The API is already running, so its holder only has to outlast the
        # busy timeout.
        with ph.lock_holder(
            db_path=db, hold_seconds=LOCK_HOLD_SECONDS, ready_file=ready
        ):
            status, body = api.request(
                "POST",
                f"/api/comparisons/{comparison_id}/detect",
                token=token,
                timeout=60,
            )
            assert status == 500
            detail = body["detail"]
            assert detail["code"] == "comparison_storage_error"
            assert detail["error_id"].startswith("err_")
            _assert_safe_text(json.dumps(body))

        # The CLI gets its own holder: a fresh process must first start and
        # import the application, and only then does it contend for the lock.
        with ph.lock_holder(
            db_path=db,
            hold_seconds=CLI_LOCK_HOLD_SECONDS,
            ready_file=tmp_path / "surface-cli.ready",
        ):
            blocked = _worker(corpus, db, worker_id="lock-cli", use_cli=True)
            assert blocked.returncode == 1, blocked.diagnostics()
            assert blocked.stdout == ""
            assert blocked.stderr.strip() == (
                "worker_infrastructure_error: execution could not be completed"
            )
            _assert_safe_text(blocked.stderr)

        # Test: retrying the explicit command after release reaches a valid state.
        status, body = api.request(
            "POST", f"/api/comparisons/{comparison_id}/detect", token=token
        )
        assert status == 202
        assert body["created"] is True

    recovered = _worker(corpus, db, worker_id="lock-recovered", use_cli=True)
    assert recovered.returncode == 0, recovered.diagnostics()
    assert json.loads(recovered.stdout)["jobStatus"] == "succeeded"
    # No implicit automatic retry was introduced: one attempt, one result.
    assert len(_rows(db, "comparison_detection_attempts")) == 1
    assert len(_rows(db, "comparison_results")) == 1
    _assert_integrity(db)


# =============================================================================
# 29-30. Safe output, and cleanup
# =============================================================================

_UNSAFE_MARKERS = (
    "SELECT ",
    "INSERT ",
    "UPDATE ",
    "BEGIN IMMEDIATE",
    "sqlite3.",
    "Traceback",
    "claim_token",
    "FDIA_AUTH_SECRET",
    "Bearer ",
    "AWS_SECRET",
    str(REPO_ROOT),
    "/private/",
)


def _assert_safe_text(text: str) -> None:
    for marker in _UNSAFE_MARKERS:
        assert marker not in text, marker


def test_process_outputs_carry_no_paths_sql_tokens_or_secrets(corpus, db, gate):
    """Test 29: every observed child output stays inside the safe vocabulary."""
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]

    succeeded = _worker(
        corpus, db, worker_id="safe-output-worker", job_id=job["job_id"],
        use_cli=True,
    )
    _assert_safe_text(succeeded.stdout)
    _assert_safe_text(succeeded.stderr)
    payload = json.loads(succeeded.stdout)
    # Structured lifecycle identifiers correlate; claim material never appears.
    assert set(payload) >= {
        "jobId",
        "attemptId",
        "jobStatus",
        "attemptStatus",
        "claimGeneration",
        "retryCount",
        "failureCode",
        "resultHash",
    }
    assert not any("token" in key.lower() for key in payload)

    # A missing database is an infrastructure refusal, not a stack trace.
    missing = ph.run_worker(
        db_path=db.parent / "absent.db",
        registry_path=corpus.registry,
        persist_dir=corpus.persist_dir,
        worker_id="missing-db-worker",
        use_cli=True,
    )
    assert missing.returncode == 1
    assert missing.stdout == ""
    _assert_safe_text(missing.stderr)
    assert "required local storage is unavailable" in missing.stderr


def test_no_child_process_or_temporary_artifact_survives(corpus, db, tmp_path, gate):
    """Test 30: children are reaped and nothing is left on disk.

    Includes the hardest case: a child blocked at a checkpoint when its
    context manager exits. The harness must still terminate and reap it.
    """
    comparison_id = _comparison(corpus, db)
    job = _enqueue(corpus, db, comparison_id)["job"]
    spec = gate.spec(
        {
            CP.WORKER_AFTER_DETECTOR_COMPUTE_BEFORE_TERMINAL_COMMIT: {
                "action": "block"
            }
        }
    )

    with _worker_bg(
        corpus, db, worker_id="abandoned-worker", job_id=job["job_id"], faults=spec
    ) as worker:
        gate.wait_reached(
            CP.WORKER_AFTER_DETECTOR_COMPUTE_BEFORE_TERMINAL_COMMIT
        )
        assert worker.alive()
        pid = worker.pid
    # Leaving the context terminated and reaped it.
    assert not worker.alive()
    with pytest.raises(OSError):
        os.kill(pid, 0)

    # No stray database, journal, socket, or pid file was created outside the
    # temporary directories the harness owns.
    assert not list(REPO_ROOT.glob("*.db-journal"))
    assert not list(REPO_ROOT.glob("*.sock"))
    assert not list(REPO_ROOT.glob("*.pid"))
    leftovers = [
        path.name
        for path in tmp_path.rglob("*")
        if path.suffix in {".sock", ".pid"}
    ]
    assert leftovers == []


# =============================================================================
# 31-32. Readiness (section K)
# =============================================================================


def test_readiness_is_read_only_and_creates_nothing(corpus, db, tmp_path):
    """Test 31: readiness never initializes, migrates, or writes."""
    comparison_store.init_db(db)
    before_bytes = db.read_bytes()
    before_mtime = db.stat().st_mtime_ns

    for _ in range(3):
        report = runtime_readiness.evaluate(
            runtime_readiness.ROLE_API,
            db_path=db,
            registry_path=corpus.registry,
        )
        assert report["status"] == runtime_readiness.STATUS_READY
        worker_report = runtime_readiness.evaluate(
            runtime_readiness.ROLE_WORKER,
            db_path=db,
            registry_path=corpus.registry,
            persist_dir=corpus.persist_dir,
        )
        assert worker_report["status"] == runtime_readiness.STATUS_READY

    assert db.read_bytes() == before_bytes
    assert db.stat().st_mtime_ns == before_mtime
    assert not list(db.parent.glob("*.db-wal"))
    assert not list(db.parent.glob("*.db-journal"))

    # A database that does not exist is NOT created by a readiness probe.
    absent = tmp_path / "never-created.db"
    report = runtime_readiness.evaluate(
        runtime_readiness.ROLE_WORKER,
        db_path=absent,
        registry_path=corpus.registry,
        persist_dir=corpus.persist_dir,
    )
    assert report["status"] == runtime_readiness.STATUS_NOT_READY
    assert not absent.exists()

    # Role scoping is explicit and documented.
    assert runtime_readiness.CHECKS_FOR_ROLE[runtime_readiness.ROLE_WORKER] != (
        runtime_readiness.CHECKS_FOR_ROLE[runtime_readiness.ROLE_API]
    )
    assert (
        runtime_readiness.CHECK_AUTH_SECRET
        not in runtime_readiness.CHECKS_FOR_ROLE[runtime_readiness.ROLE_WORKER]
    )
    assert (
        runtime_readiness.CHECK_VECTOR_STORE
        not in runtime_readiness.CHECKS_FOR_ROLE[runtime_readiness.ROLE_API]
    )


def test_readiness_refuses_missing_or_incomplete_dependencies(
    corpus, db, tmp_path
):
    """Test 32: readiness fails closed, with safe codes and no leakage."""
    # Missing database.
    report = runtime_readiness.evaluate(
        runtime_readiness.ROLE_API,
        db_path=tmp_path / "absent.db",
        registry_path=corpus.registry,
    )
    assert report["status"] == runtime_readiness.STATUS_NOT_READY
    codes = {check["name"]: check["code"] for check in report["checks"]}
    assert codes["comparison_database"] == runtime_readiness.CODE_DATABASE_UNAVAILABLE

    # Present but incomplete schema.
    partial = tmp_path / "partial.db"
    with closing(sqlite3.connect(partial)) as conn:
        conn.execute("CREATE TABLE comparisons (comparison_id TEXT)")
        conn.commit()
    report = runtime_readiness.evaluate(
        runtime_readiness.ROLE_API,
        db_path=partial,
        registry_path=corpus.registry,
    )
    codes = {check["name"]: check["code"] for check in report["checks"]}
    assert (
        codes["comparison_database"]
        == runtime_readiness.CODE_DATABASE_SCHEMA_INCOMPLETE
    )

    # Missing registry and vector store.
    comparison_store.init_db(db)
    report = runtime_readiness.evaluate(
        runtime_readiness.ROLE_WORKER,
        db_path=db,
        registry_path=tmp_path / "absent.jsonl",
        persist_dir=tmp_path / "absent-chroma",
    )
    codes = {check["name"]: check["code"] for check in report["checks"]}
    assert codes["filing_registry"] == runtime_readiness.CODE_REGISTRY_UNAVAILABLE
    assert codes["vector_store"] == runtime_readiness.CODE_VECTOR_STORE_UNAVAILABLE
    _assert_safe_text(json.dumps(report))


def test_readiness_route_and_cli_expose_no_paths_or_secrets(corpus, db, tmp_path):
    """Health stays simpler than readiness; both stay safe."""
    comparison_store.init_db(db)
    with ph.api_process(
        db_path=db, registry_path=corpus.registry, persist_dir=corpus.persist_dir
    ) as api:
        status, body = api.request("GET", "/api/health")
        assert status == 200 and body == {"status": "ok"}
        status, body = api.request("GET", "/api/ready")
        assert status == 200
        assert body["status"] == "ready"
        _assert_safe_text(json.dumps(body))

    # Not ready: 503, stable code, correlation id, still no leakage.
    with ph.api_process(
        db_path=tmp_path / "absent.db",
        registry_path=corpus.registry,
        persist_dir=corpus.persist_dir,
    ) as api:
        status, body = api.request("GET", "/api/ready")
        assert status == 503
        assert body["status"] == "not_ready"
        assert body["code"] == runtime_readiness.NOT_READY_CODE
        assert body["error_id"].startswith("err_")
        _assert_safe_text(json.dumps(body))

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/check_runtime_readiness.py",
            "--role",
            "worker",
            "--db-path",
            str(db),
            "--registry-path",
            str(corpus.registry),
            "--persist-dir",
            str(corpus.persist_dir),
            "--json",
        ],
        cwd=str(REPO_ROOT),
        env=ph.child_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "ready"
    _assert_safe_text(completed.stdout)


# =============================================================================
# 33-35. Local reference runtime packaging (section J)
# =============================================================================

REFERENCE_ENV_TEMPLATE = REPO_ROOT / ".env.reference.example"
REFERENCE_API_SCRIPT = REPO_ROOT / "scripts" / "run_reference_api.sh"
REFERENCE_WORKER_SCRIPT = REPO_ROOT / "scripts" / "run_reference_worker.sh"


def test_reference_runtime_config_contains_no_committed_secret():
    """Test 33: the template is a template, not a place to keep a secret."""
    template = REFERENCE_ENV_TEMPLATE.read_text(encoding="utf-8")

    # Every sensitive name appears only as documentation or a commented key.
    for sensitive in (
        "FDIA_AUTH_SECRET",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "TAVILY_API_KEY",
    ):
        for line in template.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() == sensitive:
                assert value.strip() == "", f"{sensitive} carries a value"

    # No assigned value anywhere looks like a credential.
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        _, _, value = stripped.partition("=")
        assert not value.strip().startswith("AKIA"), stripped
        assert "BEGIN PRIVATE KEY" not in value

    # And it is honest about what this runtime is.
    lowered = template.lower()
    assert "not a production deployment" in lowered
    assert "one-shot" in lowered
    for absent in ("scheduler", "daemon", "external queue"):
        assert absent in lowered, absent


def test_api_and_worker_point_at_the_same_persistent_state(tmp_path):
    """Test 34: both scripts derive identical paths from one state directory."""
    api_script = REFERENCE_API_SCRIPT.read_text(encoding="utf-8")
    worker_script = REFERENCE_WORKER_SCRIPT.read_text(encoding="utf-8")

    shared = [
        'COMPARISON_DB_PATH="${COMPARISON_DB_PATH:-$STATE_DIR/comparisons/comparisons.db}"',
        'FILING_REGISTRY_PATH="${FILING_REGISTRY_PATH:-$STATE_DIR/filing_registry/registry.jsonl}"',
        'CHROMA_PERSIST_DIR="${CHROMA_PERSIST_DIR:-$STATE_DIR/chroma_db}"',
        'STATE_DIR="${FDIA_STATE_DIR:-./reference-state}"',
    ]
    for line in shared:
        assert line in api_script, line
        assert line in worker_script, line

    # Structural validation of the shell itself (Docker packaging is
    # deliberately not part of this commit; see README).
    for script in (REFERENCE_API_SCRIPT, REFERENCE_WORKER_SCRIPT):
        checked = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert checked.returncode == 0, checked.stderr
        assert os.access(script, os.X_OK), f"{script.name} is not executable"

    # The paths the scripts derive are exactly the names config.py reads.
    import config

    state = tmp_path / "reference-state"
    env = ph.child_env(
        db_path=state / "comparisons" / "comparisons.db",
        registry_path=state / "filing_registry" / "registry.jsonl",
        extra={"CHROMA_PERSIST_DIR": str(state / "chroma_db")},
    )
    resolved = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config, json; print(json.dumps({"
            "'db': config.COMPARISON_DB_PATH,"
            "'registry': config.FILING_REGISTRY_PATH,"
            "'chroma': config.CHROMA_PERSIST_DIR}))",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert resolved.returncode == 0, resolved.stderr
    values = json.loads(resolved.stdout)
    assert values["db"] == str(state / "comparisons" / "comparisons.db")
    assert values["registry"] == str(state / "filing_registry" / "registry.jsonl")
    assert values["chroma"] == str(state / "chroma_db")
    assert config.CHROMA_PERSIST_DIR  # default still resolves with no override


def test_reference_worker_stays_manually_invoked_and_one_shot():
    """Test 35: nothing in the packaging implies automatic job processing."""
    worker_script = REFERENCE_WORKER_SCRIPT.read_text(encoding="utf-8")
    api_script = REFERENCE_API_SCRIPT.read_text(encoding="utf-8")

    # The worker runs the shipped one-shot CLI, with --once, exactly once.
    assert "scripts/run_comparison_detection_worker.py" in worker_script
    assert "--once" in worker_script
    assert worker_script.count("exec python") == 1

    # Executable lines only: the comments deliberately explain that nothing
    # runs "until an operator runs this command again", and prose about the
    # absence of a loop must not read as a loop.
    def _code(script: str) -> str:
        return "\n".join(
            line
            for line in script.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

    for forbidden in (
        "while true",
        "until ",
        "sleep ",
        "crontab",
        "systemd",
        "restart: always",
        "restart=always",
        "watch ",
    ):
        assert forbidden not in _code(worker_script), forbidden
        assert forbidden not in _code(api_script), forbidden

    # Nothing is backgrounded: a trailing '&' would detach the process and make
    # "one-shot" a claim about the command rather than about the execution.
    for script in (worker_script, api_script):
        for line in _code(script).splitlines():
            assert not line.rstrip().endswith("&") or line.rstrip().endswith(
                "&&"
            ), line

    # And it says so, so an operator cannot mistake it for a daemon.
    lowered = worker_script.lower()
    assert "one-shot" in lowered
    assert "does not start a loop" in lowered

    # The worker is credential-free: it explicitly drops the API's secret.
    assert "unset FDIA_AUTH_SECRET" in worker_script
    assert "FDIA_AUTH_SECRET" in api_script  # the API does require it


# =============================================================================
# 36-37. Workflow-database backup and restore verification (section L)
# =============================================================================


def _backup_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/backup_workflow_db.py", *argv],
        cwd=str(REPO_ROOT),
        env=ph.child_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_workflow_database_backup_preserves_source_and_passes_integrity(
    corpus, db, tmp_path
):
    """Test 36: a real backup, integrity-checked, with the source untouched."""
    comparison_id = _comparison(corpus, db)
    _enqueue(corpus, db, comparison_id)
    _worker(corpus, db, worker_id="backup-source-worker", use_cli=True)

    source_bytes = db.read_bytes()
    source_snapshot = _snapshot(db)
    out = tmp_path / "workflow-backup.db"

    created = _backup_cli("--db-path", str(db), "--out", str(out))
    assert created.returncode == 0, created.stderr
    report = json.loads(created.stdout)
    assert report["status"] == "created"
    assert report["scope"] == "workflow_database_only"
    assert report["integrityCheck"] == "ok"
    assert report["foreignKeyCheck"] == "ok"
    assert report["sourceUnmodified"] is True
    # Honest scope: the registry and vector store are named as NOT covered.
    assert set(report["doesNotCover"]) == {"filing_registry", "vector_store"}
    _assert_safe_text(created.stdout)

    # The source database is byte-for-byte what it was.
    assert db.read_bytes() == source_bytes

    # Overwriting is refused unless explicitly requested.
    refused = _backup_cli("--db-path", str(db), "--out", str(out))
    assert refused.returncode == 1
    assert json.loads(refused.stdout)["code"] == "backup_destination_exists"
    forced = _backup_cli("--db-path", str(db), "--out", str(out), "--force")
    assert forced.returncode == 0, forced.stderr

    # No partial artifact is left behind on any path.
    assert not list(tmp_path.glob("*.partial"))

    # A missing source is refused, not created.
    absent = tmp_path / "absent.db"
    missing = _backup_cli(
        "--db-path", str(absent), "--out", str(tmp_path / "never.db")
    )
    assert missing.returncode == 1
    assert json.loads(missing.stdout)["code"] == "backup_source_unavailable"
    assert not absent.exists()
    assert not (tmp_path / "never.db").exists()

    assert source_snapshot == _snapshot(db)


def test_backup_restore_verification_produces_an_independent_readable_copy(
    corpus, db, tmp_path
):
    """Test 37: the verified copy stands alone and equals the source."""
    comparison_id = _comparison(corpus, db)
    _enqueue(corpus, db, comparison_id)
    _worker(corpus, db, worker_id="restore-source-worker", use_cli=True)

    out = tmp_path / "restore-check.db"
    assert _backup_cli("--db-path", str(db), "--out", str(out)).returncode == 0

    verified = _backup_cli("--verify", str(out))
    assert verified.returncode == 0, verified.stderr
    report = json.loads(verified.stdout)
    assert report["status"] == "verified"
    assert report["integrityCheck"] == "ok"
    _assert_safe_text(verified.stdout)

    # Independently readable: the restored copy carries the same workflow rows.
    assert _snapshot(out) == _snapshot(db)
    assert comparison_store.get_result(comparison_id, out) is not None
    _assert_integrity(out)

    # A corrupt or non-database file is refused rather than reported verified.
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a sqlite database")
    rejected = _backup_cli("--verify", str(broken))
    assert rejected.returncode == 1
    assert json.loads(rejected.stdout)["code"] in {
        "backup_source_unavailable",
        "backup_source_schema_incomplete",
    }
    _assert_safe_text(rejected.stdout)


# =============================================================================
# 40. No scheduler, daemon, polling loop, or external queue was introduced
# =============================================================================


def test_no_scheduler_daemon_queue_or_cloud_dependency_was_introduced():
    """Test 40, checked at the import graph and identifier level."""
    new_modules = (
        REPO_ROOT / "runtime_fault_hooks.py",
        REPO_ROOT / "runtime_readiness.py",
        REPO_ROOT / "scripts" / "check_runtime_readiness.py",
        REPO_ROOT / "scripts" / "backup_workflow_db.py",
    )
    forbidden_imports = {
        "celery", "kombu", "redis", "kafka", "confluent_kafka", "pika",
        "boto3", "botocore", "requests", "httpx", "aiohttp", "psycopg2",
        "psycopg", "sqlalchemy", "apscheduler", "schedule", "sched",
        "crontab", "prometheus_client", "opentelemetry", "smtplib",
        "kubernetes", "docker", "langchain_aws", "chromadb", "agent",
    }
    for path in new_modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported & forbidden_imports == set(), (
            path.name,
            sorted(imported & forbidden_imports),
        )

    # The existing guard already covers the workflow modules; this pins that
    # the fault-hook edits did not smuggle timing machinery into them.
    forbidden_identifiers = {
        "sleep", "Timer", "timer", "create_task", "run_forever", "crontab",
        "apscheduler", "celery", "sched", "backoff", "dead_letter", "dlq",
        "worker_thread", "background_task", "BackgroundTasks",
    }
    for name in (
        "comparison_detection_worker.py",
        "comparison_detector.py",
        "runtime_fault_hooks.py",
        "runtime_readiness.py",
    ):
        identifiers: set[str] = set()
        for node in ast.walk(
            ast.parse((REPO_ROOT / name).read_text(encoding="utf-8"))
        ):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.keyword) and node.arg:
                identifiers.add(node.arg)
        found = identifiers & forbidden_identifiers
        assert found == set(), f"{name}: {sorted(found)}"

    # No cloud-deployment or container-orchestration artifact was added.
    for artifact in ("k8s", "kubernetes", "helm", ".github/workflows/deploy.yml"):
        assert not (REPO_ROOT / artifact).exists(), artifact
