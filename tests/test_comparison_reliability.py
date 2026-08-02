"""Tests for read-only comparison reliability visibility.

Covers the reliability service (metrics, denominators, percentiles, issues,
data validation), the read-only API routes, the operator CLI, and the
structured lifecycle log records.

Two construction styles on purpose:

* Most tests build controlled SQLite rows directly, so every counter,
  denominator, boundary, and window case is exact and fast. Where an invalid
  state is required, ``PRAGMA ignore_check_constraints`` is used deliberately —
  the store's CHECK constraints normally make those rows unrepresentable, and
  the point is to prove the service refuses them if they ever appear.
* The structured-logging tests run REAL detections and a REAL replay through
  the controlled corpus, because the whole claim is that the records are
  emitted at the actual mutation boundaries.

Time is ALWAYS injected: not one sleep-based test in this file. Entirely
offline — no AWS credentials, no Bedrock, no network.
"""

import ast
import hashlib
import json
import logging
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings

import api
import comparison_detector
import comparison_reliability
import comparison_store
import config
import detection_recovery
import filing_registry
import ingest
from comparison_detector import DETECTOR_VERSION
from comparison_store import WORKFLOW_VERSION
from tests.auth_helpers import authorization_headers

client = TestClient(api.app, headers=authorization_headers())

REPO_ROOT = Path(__file__).resolve().parent.parent

PREV_ID = "acme-corporation:10-k:2024-12-31"
CURR_ID = "acme-corporation:10-k:2025-12-31"

T0 = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
STALE_AFTER = 900
OPERATOR = "ops.engineer@example.com"
NOTE = "Process was killed during a deploy; restarting detection."
REASON = "operator_replay_stale_attempt"


# --- Controlled row construction ---------------------------------------------


def _sql(db, statement, params=(), *, ignore_checks=False):
    with closing(sqlite3.connect(str(db))) as conn, conn:
        if ignore_checks:
            # Deliberate: the store's CHECK constraints make the invalid states
            # in section "data validation" unrepresentable through any normal
            # path. Bypassing them is the only way to prove the service refuses
            # them rather than quietly averaging over them.
            conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(statement, params)


def _comparison(
    db, comparison_id, status, *, created=T0, updated=None, failure_code=None
):
    _sql(
        db,
        "INSERT INTO comparisons (comparison_id, schema_version, workflow_version, "
        "previous_filing_id, current_filing_id, section_scope, status, created_at, "
        "updated_at, failure_code, failure_summary) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            comparison_id,
            "comparison.v1",
            WORKFLOW_VERSION,
            f"{comparison_id}:prev",
            f"{comparison_id}:curr",
            '["item_1a_risk_factors"]',
            status,
            created.isoformat(),
            (updated or created).isoformat(),
            failure_code,
            "bounded safe summary" if failure_code else None,
        ),
    )


def _attempt(
    db,
    attempt_id,
    comparison_id,
    number,
    status,
    *,
    started=T0,
    finished=None,
    result_hash=None,
    failure_code=None,
    detector_version=DETECTOR_VERSION,
    workflow_version=WORKFLOW_VERSION,
    ignore_checks=False,
):
    _sql(
        db,
        "INSERT INTO comparison_detection_attempts (attempt_id, comparison_id, "
        "attempt_number, status, detector_version, workflow_version, "
        "previous_source_hash, current_source_hash, started_at, finished_at, "
        "result_hash, failure_code, failure_summary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            attempt_id,
            comparison_id,
            number,
            status,
            detector_version,
            workflow_version,
            "prev-hash",
            "curr-hash",
            started.isoformat(),
            finished.isoformat() if finished else None,
            result_hash,
            failure_code,
            "bounded safe summary" if failure_code else None,
        ),
        ignore_checks=ignore_checks,
    )


def _replay(db, replay_id, comparison_id, source_id, replacement_id, *, requested=T0):
    _sql(
        db,
        "INSERT INTO comparison_detection_replays (replay_id, comparison_id, "
        "source_attempt_id, replacement_attempt_id, operator_id, reason_code, "
        "operator_note, request_hash, policy_id, policy_version, requested_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            replay_id,
            comparison_id,
            source_id,
            replacement_id,
            OPERATOR,
            REASON,
            NOTE,
            hashlib.sha256(replay_id.encode()).hexdigest(),
            "detection_recovery_v1",
            "1",
            requested.isoformat(),
        ),
    )


def _write_registry(path, *, entries=1):
    """A minimal READABLE filing registry.

    Controlled tests must supply one explicitly: `filing_registry/registry.jsonl`
    is gitignored runtime state, so a fresh checkout (and CI) has none, and
    replay eligibility now fails closed rather than reporting a false zero when
    the registry cannot answer. These synthetic filing ids are deliberately
    unknown to it — the registry ANSWERS "not this pair", which is registry
    truth, not an unavailable dependency.
    """
    lines = []
    for index in range(entries):
        lines.append(
            json.dumps(
                {
                    "source_path": f"docs/controlled-{index}.pdf",
                    "source_name": f"controlled-{index}.pdf",
                    "source_hash": hashlib.sha256(str(index).encode()).hexdigest(),
                    "filing_id": f"controlled-co:10-k:202{index}-12-31",
                    "document_family_id": "controlled-co-10-k",
                    "company_key": "controlled-co",
                    "form_type": "10-K",
                    "period_end": f"202{index}-12-31",
                    "filing_date": f"202{index}-12-31",
                    "identity_source": "manifest",
                    "parse_status": filing_registry.PARSED,
                    "loader": "pdf",
                    "ingested_at": T0.isoformat(),
                    "chunk_count": 3,
                }
            )
        )
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Path(path)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "comparisons.db"
    comparison_store.init_db(path)
    registry = _write_registry(tmp_path / "registry.jsonl")
    # The CLI has no registry flag, so it resolves config. Point config at the
    # controlled registry too, and never at the gitignored runtime one. Tests
    # that need an unavailable dependency override this explicitly.
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(registry))
    return path


def _registry_for(db):
    return Path(db).parent / "registry.jsonl"


def _summary(db, **kwargs):
    kwargs.setdefault("now", T0 + timedelta(seconds=60))
    kwargs.setdefault("registry_path", _registry_for(db))
    return comparison_reliability.summary(db_path=db, **kwargs)


def _issues(db, **kwargs):
    kwargs.setdefault("now", T0 + timedelta(seconds=60))
    kwargs.setdefault("registry_path", _registry_for(db))
    return comparison_reliability.issues(db_path=db, **kwargs)


# --- 1: empty database --------------------------------------------------------


def test_empty_database_reports_explicit_zero_denominators(db):
    """Test 1. Every rate is null with its denominator visible — never NaN,
    never 0.0, never silently absent."""
    report = _summary(db)

    assert report["gauges"] == {
        "comparisons_ready_for_detection": 0,
        "comparisons_queued_for_detection": 0,
        "comparisons_detecting": 0,
        "comparisons_waiting_for_detection_retry": 0,
        "comparisons_detected": 0,
        "comparisons_failed": 0,
        "running_attempts": 0,
        "stale_running_attempts": 0,
        "replay_eligible_attempts": 0,
        "attempt_limit_exhausted_comparisons": 0,
        "detection_jobs_queued": 0,
        "detection_jobs_running": 0,
        "detection_jobs_waiting_for_retry": 0,
        "detection_jobs_succeeded": 0,
        "detection_jobs_failed": 0,
        "active_job_leases": 0,
        "expired_job_leases": 0,
        "reclaimable_jobs": 0,
        "claim_exhausted_jobs": 0,
        "detection_jobs_retry_due": 0,
        "detection_jobs_retry_not_due": 0,
        "detection_jobs_retry_exhausted": 0,
        "unresolved_operational_issues": 0,
    }
    assert report["attempts"]["terminal_attempts"] == 0
    for group in ("attempt_rates", "replay_rates"):
        for name, metric in report[group].items():
            assert metric["value"] is None, name
            assert metric["denominator"] == 0, name
            assert metric["zero_denominator"] is True, name
            assert metric["zero_denominator_policy"] == (
                comparison_reliability.ZERO_DENOMINATOR_POLICY
            )
    assert report["durations"]["duration_count"] == 0
    assert report["durations"]["duration_seconds_p50"] is None
    assert report["failure_breakdown"]["failed_attempts_by_code"] == {}
    assert report["detector_versions"] == []
    # A serializable report even when nothing exists.
    assert json.loads(json.dumps(report))["since"] is None


def test_initialized_empty_database_returns_empty_issue_and_failure_listings(db):
    """Test 2. A valid empty system reports empty listings, not a refusal."""
    issues = _issues(db)
    failures = comparison_reliability.failures(now=T0, db_path=db)
    assert (issues["total"], issues["returned"], issues["truncated"]) == (0, 0, False)
    assert issues["issues"] == []
    assert (failures["total"], failures["returned"], failures["truncated"]) == (
        0, 0, False,
    )
    assert failures["failures"] == []


# --- 2-4, 6-7: attempt counters, denominators, durations ----------------------


def _mixed(db):
    """Two succeeded, one failed, one timed_out, one running."""
    _comparison(db, "cmp_a", "detected", updated=T0 + timedelta(seconds=4))
    _attempt(db, "att_a", "cmp_a", 1, "succeeded",
             finished=T0 + timedelta(seconds=4), result_hash="h1")
    _comparison(db, "cmp_b", "detected", updated=T0 + timedelta(seconds=10))
    _attempt(db, "att_b", "cmp_b", 1, "succeeded",
             finished=T0 + timedelta(seconds=10), result_hash="h2")
    _comparison(db, "cmp_c", "failed", updated=T0 + timedelta(seconds=6),
                failure_code="detector_internal_error")
    _attempt(db, "att_c", "cmp_c", 1, "failed", finished=T0 + timedelta(seconds=6),
             failure_code="detector_internal_error")
    _comparison(db, "cmp_d", "detecting", updated=T0 + timedelta(seconds=2))
    _attempt(db, "att_d1", "cmp_d", 1, "timed_out",
             finished=T0 + timedelta(seconds=2),
             failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(db, "att_d2", "cmp_d", 2, "running", started=T0 + timedelta(seconds=2))
    _replay(db, "rpl_d", "cmp_d", "att_d1", "att_d2",
            requested=T0 + timedelta(seconds=2))


def test_mixed_attempt_counts_and_exact_denominators(db):
    """Tests 2, 3, 4: running attempts are counted as started but excluded from
    every terminal rate denominator."""
    _mixed(db)
    report = _summary(db)

    assert report["attempts"] == {
        "attempts_started": 5,
        "attempts_succeeded": 2,
        "attempts_failed": 1,
        "attempts_timed_out": 1,
        "attempts_running_in_window": 1,
        "terminal_attempts": 4,
    }
    rates = report["attempt_rates"]
    assert rates["success_rate"]["numerator"] == 2
    assert rates["success_rate"]["denominator"] == 4
    assert rates["success_rate"]["value"] == 0.5
    assert rates["failure_rate"]["numerator"] == 1
    assert rates["failure_rate"]["denominator"] == 4
    assert rates["timeout_rate"]["numerator"] == 1
    assert rates["timeout_rate"]["denominator"] == 4
    # The three numerators sum to the shared denominator: the running attempt is
    # in none of them.
    assert (
        rates["success_rate"]["numerator"]
        + rates["failure_rate"]["numerator"]
        + rates["timeout_rate"]["numerator"]
        == report["attempts"]["terminal_attempts"]
        == 4
    )
    assert all(metric["zero_denominator"] is False for metric in rates.values())


def test_duration_min_max_mean_over_terminal_attempts(db):
    """Test 6. Durations are finished_at - started_at over terminal attempts."""
    _mixed(db)
    durations = _summary(db)["durations"]
    # 4s, 10s, 6s, 2s
    assert durations["duration_count"] == 4
    assert durations["duration_seconds_min"] == 2.0
    assert durations["duration_seconds_max"] == 10.0
    assert durations["duration_seconds_mean"] == 5.5
    assert durations["negative_duration_attempts"] == 0
    assert durations["percentile_method"] == comparison_reliability.PERCENTILE_METHOD


@pytest.mark.parametrize(
    "count,expected_p50,expected_p95",
    [(1, 1.0, 1.0), (2, 1.0, 2.0), (3, 2.0, 3.0), (4, 2.0, 4.0), (20, 10.0, 19.0)],
)
def test_nearest_rank_percentiles_are_pinned(count, expected_p50, expected_p95):
    """Test 7 (pure): rank = ceil(p * n), value at index rank - 1."""
    values = [float(index) for index in range(1, count + 1)]
    assert comparison_reliability.percentile(values, 0.50) == expected_p50
    assert comparison_reliability.percentile(values, 0.95) == expected_p95
    # Order of input must not matter.
    assert comparison_reliability.percentile(list(reversed(values)), 0.95) == (
        expected_p95
    )


@pytest.mark.parametrize(
    "count,expected_p50,expected_p95",
    [(1, 1.0, 1.0), (2, 1.0, 2.0), (3, 2.0, 3.0), (4, 2.0, 4.0), (20, 10.0, 19.0)],
)
def test_percentiles_through_the_report(db, count, expected_p50, expected_p95):
    """Test 7 (end to end): the same definition through real attempt rows."""
    for index in range(1, count + 1):
        _comparison(db, f"cmp_{index}", "detected")
        _attempt(db, f"att_{index}", f"cmp_{index}", 1, "succeeded",
                 finished=T0 + timedelta(seconds=index), result_hash=f"h{index}")
    durations = _summary(db)["durations"]
    assert durations["duration_count"] == count
    assert durations["duration_seconds_p50"] == expected_p50
    assert durations["duration_seconds_p95"] == expected_p95


def test_percentile_of_empty_sample_is_none():
    assert comparison_reliability.percentile([], 0.5) is None


# --- 5: replay denominators ---------------------------------------------------


def test_replay_success_denominator_excludes_running_replacements(db):
    """Test 5. A replacement that is still running is not a failure."""
    # succeeded replacement
    _comparison(db, "cmp_s", "detected", updated=T0 + timedelta(seconds=5))
    _attempt(db, "att_s1", "cmp_s", 1, "timed_out", finished=T0 + timedelta(seconds=1),
             failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(db, "att_s2", "cmp_s", 2, "succeeded", started=T0 + timedelta(seconds=1),
             finished=T0 + timedelta(seconds=5), result_hash="hs")
    _replay(db, "rpl_s", "cmp_s", "att_s1", "att_s2",
            requested=T0 + timedelta(seconds=1))
    # failed replacement
    _comparison(db, "cmp_f", "failed", updated=T0 + timedelta(seconds=7),
                failure_code="detector_internal_error")
    _attempt(db, "att_f1", "cmp_f", 1, "timed_out", finished=T0 + timedelta(seconds=1),
             failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(db, "att_f2", "cmp_f", 2, "failed", started=T0 + timedelta(seconds=1),
             finished=T0 + timedelta(seconds=7),
             failure_code="detector_internal_error")
    _replay(db, "rpl_f", "cmp_f", "att_f1", "att_f2",
            requested=T0 + timedelta(seconds=1))
    # still-running replacement
    _comparison(db, "cmp_r", "detecting", updated=T0 + timedelta(seconds=1))
    _attempt(db, "att_r1", "cmp_r", 1, "timed_out", finished=T0 + timedelta(seconds=1),
             failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(db, "att_r2", "cmp_r", 2, "running", started=T0 + timedelta(seconds=1))
    _replay(db, "rpl_r", "cmp_r", "att_r1", "att_r2",
            requested=T0 + timedelta(seconds=1))

    report = _summary(db)
    assert report["replays"] == {
        "replays_started": 3,
        "replay_replacements_succeeded": 1,
        "replay_replacements_failed": 1,
        "replay_replacements_running": 1,
        "replay_replacements_timed_out": 0,
        "terminal_replay_replacements": 2,
    }
    rate = report["replay_rates"]["replay_success_rate"]
    assert (rate["numerator"], rate["denominator"], rate["value"]) == (1, 2, 0.5)


# --- 8: negative duration -----------------------------------------------------


def test_negative_duration_becomes_an_issue_and_is_excluded(db):
    """Test 8. A skewed clock never silently changes a min/mean."""
    _comparison(db, "cmp_ok", "detected", updated=T0 + timedelta(seconds=4))
    _attempt(db, "att_ok", "cmp_ok", 1, "succeeded",
             finished=T0 + timedelta(seconds=4), result_hash="h1")
    _comparison(db, "cmp_skew", "detected", updated=T0)
    _attempt(db, "att_skew", "cmp_skew", 1, "succeeded",
             started=T0, finished=T0 - timedelta(seconds=30), result_hash="h2")

    report = _summary(db)
    durations = report["durations"]
    assert durations["duration_count"] == 1
    assert durations["duration_seconds_min"] == 4.0
    assert durations["duration_seconds_max"] == 4.0
    assert durations["duration_seconds_mean"] == 4.0
    assert durations["negative_duration_attempts"] == 1

    issues = _issues(db)["issues"]
    negative = [
        issue
        for issue in issues
        if issue["issue_type"] == comparison_reliability.ISSUE_INVALID_NEGATIVE_DURATION
    ]
    assert len(negative) == 1
    assert negative[0]["attempt_id"] == "att_skew"
    assert negative[0]["recommended_action_code"] == (
        comparison_reliability.ACTION_INSPECT_CLOCK
    )
    # It is the most severe issue, so it sorts first.
    assert issues[0]["issue_type"] == (
        comparison_reliability.ISSUE_INVALID_NEGATIVE_DURATION
    )


# --- 9-11: gauges and window semantics ---------------------------------------


def test_current_state_gauges_are_correct(db):
    """Test 9."""
    _mixed(db)
    _comparison(db, "cmp_ready", "ready_for_detection")
    gauges = _summary(db)["gauges"]
    assert gauges["comparisons_ready_for_detection"] == 1
    assert gauges["comparisons_detecting"] == 1
    assert gauges["comparisons_detected"] == 2
    assert gauges["comparisons_failed"] == 1
    assert gauges["running_attempts"] == 1


def test_historical_window_uses_started_at_and_requested_at(db):
    """Test 10. Attempts enter by started_at, replays by requested_at."""
    early, late = T0, T0 + timedelta(hours=5)
    _comparison(db, "cmp_early", "detected", updated=early + timedelta(seconds=3))
    _attempt(db, "att_early", "cmp_early", 1, "succeeded", started=early,
             finished=early + timedelta(seconds=3), result_hash="h1")
    _comparison(db, "cmp_late", "detected", updated=late + timedelta(seconds=3))
    _attempt(db, "att_late1", "cmp_late", 1, "timed_out", started=late,
             finished=late + timedelta(seconds=1),
             failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(db, "att_late2", "cmp_late", 2, "succeeded",
             started=late + timedelta(seconds=1),
             finished=late + timedelta(seconds=3), result_hash="h2")
    _replay(db, "rpl_late", "cmp_late", "att_late1", "att_late2",
            requested=late + timedelta(seconds=1))

    now = late + timedelta(hours=1)
    unbounded = _summary(db, now=now)
    assert unbounded["attempts"]["attempts_started"] == 3
    assert unbounded["replays"]["replays_started"] == 1

    # Only the early attempt.
    windowed = _summary(db, now=now, until=(early + timedelta(minutes=1)).isoformat())
    assert windowed["attempts"]["attempts_started"] == 1
    assert windowed["attempts"]["attempts_succeeded"] == 1
    assert windowed["replays"]["replays_started"] == 0
    assert windowed["until"] == (early + timedelta(minutes=1)).isoformat()

    # Only the late pair (and its replay).
    windowed = _summary(db, now=now, since=(late - timedelta(minutes=1)).isoformat())
    assert windowed["attempts"]["attempts_started"] == 2
    assert windowed["replays"]["replays_started"] == 1

    # Inclusive on both bounds: an exact-boundary attempt is included.
    exact = _summary(db, now=now, since=early.isoformat(), until=early.isoformat())
    assert exact["attempts"]["attempts_started"] == 1


def test_window_narrows_history_but_never_the_current_gauges(db):
    """Test 11. A running attempt older than `since` still counts as running."""
    _comparison(db, "cmp_run", "detecting")
    _attempt(db, "att_run", "cmp_run", 1, "running", started=T0)

    later = T0 + timedelta(hours=10)
    report = _summary(db, now=later, since=(T0 + timedelta(hours=5)).isoformat())
    assert report["attempts"]["attempts_started"] == 0
    assert report["gauges"]["running_attempts"] == 1
    assert report["gauges"]["stale_running_attempts"] == 1
    assert report["gauges"]["comparisons_detecting"] == 1
    # And the issue set is likewise not window-scoped.
    assert report["gauges"]["unresolved_operational_issues"] == 1


def test_detector_and_workflow_versions_come_from_windowed_attempts(db):
    """Which versions produced the records in scope."""
    _comparison(db, "cmp_v1", "detected", updated=T0 + timedelta(seconds=2))
    _attempt(db, "att_v1", "cmp_v1", 1, "succeeded",
             finished=T0 + timedelta(seconds=2), result_hash="h1",
             detector_version="item1a_detector.v1",
             workflow_version="comparison_workflow.v1")
    _comparison(db, "cmp_v2", "detected", updated=T0 + timedelta(seconds=2))
    _attempt(db, "att_v2", "cmp_v2", 1, "succeeded",
             finished=T0 + timedelta(seconds=2), result_hash="h2")

    report = _summary(db)
    assert report["detector_versions"] == ["item1a_detector.v1", DETECTOR_VERSION]
    assert report["workflow_versions"] == ["comparison_workflow.v1", WORKFLOW_VERSION]


# --- 12-13: window validation -------------------------------------------------


@pytest.mark.parametrize(
    "since,until",
    [
        ("2026-07-29T12:00:00", None),          # naive since
        (None, "2026-07-29T12:00:00"),          # naive until
        ("2026-07-29T12:00:00", "2026-07-29T13:00:00+00:00"),
        ("not-a-timestamp", None),
        ("", None),
    ],
)
def test_naive_or_invalid_timestamps_are_rejected(since, until):
    """Test 12. A naive timestamp is never assumed to be UTC."""
    with pytest.raises(comparison_reliability.ReliabilityQueryError) as excinfo:
        comparison_reliability.parse_window(since, until)
    assert excinfo.value.code == comparison_reliability.CODE_INVALID_TIMESTAMP


def test_naive_datetime_object_is_rejected_too():
    with pytest.raises(comparison_reliability.ReliabilityQueryError) as excinfo:
        comparison_reliability.parse_window(datetime(2026, 7, 29, 12, 0), None)
    assert excinfo.value.code == comparison_reliability.CODE_INVALID_TIMESTAMP


def test_inverted_range_is_rejected_and_equal_bounds_allowed():
    """Test 13. until >= since; equal bounds are a valid single instant."""
    with pytest.raises(comparison_reliability.ReliabilityQueryError) as excinfo:
        comparison_reliability.parse_window(
            "2026-07-29T13:00:00+00:00", "2026-07-29T12:00:00+00:00"
        )
    assert excinfo.value.code == comparison_reliability.CODE_INVALID_TIME_RANGE

    since, until = comparison_reliability.parse_window(
        "2026-07-29T12:00:00+00:00", "2026-07-29T12:00:00+00:00"
    )
    assert since == until

    # Omitted bounds mean unbounded, and offsets normalize to UTC.
    assert comparison_reliability.parse_window(None, None) == (None, None)
    since, _ = comparison_reliability.parse_window("2026-07-29T08:00:00-04:00", None)
    assert since == datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def test_limit_and_issue_type_are_validated(db):
    for bad in (0, -1, comparison_reliability.MAX_LIMIT + 1):
        with pytest.raises(comparison_reliability.ReliabilityQueryError) as excinfo:
            _issues(db, limit=bad)
        assert excinfo.value.code == comparison_reliability.CODE_INVALID_LIMIT
    with pytest.raises(comparison_reliability.ReliabilityQueryError) as excinfo:
        _issues(db, issue_type="not_a_type")
    assert excinfo.value.code == comparison_reliability.CODE_INVALID_ISSUE_TYPE


def test_naive_now_is_rejected(db):
    with pytest.raises(comparison_reliability.ReliabilityQueryError):
        comparison_reliability.summary(now=datetime(2026, 7, 29, 12, 0), db_path=db)


# --- 14-15: staleness boundary and agreement with the recovery view -----------


def test_stale_issue_appears_only_at_the_inclusive_boundary(db):
    """Test 14. age >= stale_after_seconds is stale; one second earlier is not."""
    _comparison(db, "cmp_run", "detecting")
    _attempt(db, "att_run", "cmp_run", 1, "running", started=T0)

    just_before = _summary(db, now=T0 + timedelta(seconds=STALE_AFTER - 1))
    assert just_before["gauges"]["stale_running_attempts"] == 0
    assert just_before["gauges"]["unresolved_operational_issues"] == 0

    boundary = _summary(db, now=T0 + timedelta(seconds=STALE_AFTER))
    assert boundary["gauges"]["stale_running_attempts"] == 1
    assert boundary["gauges"]["unresolved_operational_issues"] == 1

    # A negative age (clock moved backwards) is never stale.
    skewed = _summary(db, now=T0 - timedelta(hours=1))
    assert skewed["gauges"]["stale_running_attempts"] == 0


def test_recovery_view_and_summary_agree_on_staleness(db):
    """Test 15. Both read staleness through the same pure store helper."""
    _comparison(db, "cmp_run", "detecting")
    _attempt(db, "att_run", "cmp_run", 1, "running", started=T0)

    for now in (
        T0 + timedelta(seconds=STALE_AFTER - 1),
        T0 + timedelta(seconds=STALE_AFTER),
        T0 + timedelta(hours=3),
    ):
        view = detection_recovery.recovery_view("att_run", now=now, db_path=db)
        report = _summary(db, now=now)
        assert report["gauges"]["stale_running_attempts"] == (
            1 if view["is_stale"] else 0
        )
        issues = _issues(db, now=now)["issues"]
        stale = [
            issue
            for issue in issues
            if issue["issue_type"]
            == comparison_reliability.ISSUE_STALE_RUNNING_ATTEMPT
        ]
        assert len(stale) == (1 if view["is_stale"] else 0)
        if stale:
            assert stale[0]["stale_at"] == view["stale_at"]
            assert stale[0]["age_seconds"] == pytest.approx(view["age_seconds"])
            assert stale[0]["attempts_used"] == view["attempts_used"]
            assert stale[0]["max_attempts"] == view["max_attempts"]


# --- 16-18, 22: the remaining issue types and ordering ------------------------


def test_attempt_limit_exhausted_issue(db):
    """Test 16. A detecting comparison at the attempt budget cannot be replayed."""
    _comparison(db, "cmp_x", "detecting", updated=T0 + timedelta(seconds=20))
    _attempt(db, "att_x1", "cmp_x", 1, "timed_out", started=T0,
             finished=T0 + timedelta(seconds=10),
             failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(db, "att_x2", "cmp_x", 2, "timed_out",
             started=T0 + timedelta(seconds=10),
             finished=T0 + timedelta(seconds=20),
             failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(db, "att_x3", "cmp_x", 3, "running", started=T0 + timedelta(seconds=20))
    _replay(db, "rpl_x1", "cmp_x", "att_x1", "att_x2",
            requested=T0 + timedelta(seconds=10))
    _replay(db, "rpl_x2", "cmp_x", "att_x2", "att_x3",
            requested=T0 + timedelta(seconds=20))

    now = T0 + timedelta(hours=2)
    report = _summary(db, now=now)
    assert report["gauges"]["attempt_limit_exhausted_comparisons"] == 1

    issues = _issues(db, now=now)["issues"]
    exhausted = [
        issue
        for issue in issues
        if issue["issue_type"] == comparison_reliability.ISSUE_ATTEMPT_LIMIT_EXHAUSTED
    ]
    assert len(exhausted) == 1
    assert exhausted[0]["attempts_used"] == 3
    assert exhausted[0]["max_attempts"] == 3
    assert exhausted[0]["status"] == "detecting"
    assert exhausted[0]["attempt_id"] == "att_x3"
    assert exhausted[0]["recommended_action_code"] == (
        comparison_reliability.ACTION_NEW_WORKFLOW_VERSION
    )
    # The stale running attempt is reported too — and, being at the budget, it
    # reports that no replay is available rather than inviting one.
    stale = [
        issue
        for issue in issues
        if issue["issue_type"] == comparison_reliability.ISSUE_STALE_RUNNING_ATTEMPT
    ]
    assert stale[0]["recommended_action_code"] == (
        comparison_reliability.ACTION_NO_REPLAY_AVAILABLE
    )
    # Exhausted outranks merely stale.
    assert [issue["issue_type"] for issue in issues][:2] == [
        comparison_reliability.ISSUE_ATTEMPT_LIMIT_EXHAUSTED,
        comparison_reliability.ISSUE_STALE_RUNNING_ATTEMPT,
    ]


def _at_limit(db, comparison_id, status, *, final_status, failure_code=None):
    """A comparison that consumed all three permitted attempts, ending in the
    given terminal (or running) third attempt."""
    _comparison(db, comparison_id, status, updated=T0 + timedelta(seconds=30),
                failure_code=failure_code)
    for number in (1, 2):
        _attempt(db, f"att_{comparison_id}_{number}", comparison_id, number,
                 "timed_out", started=T0 + timedelta(seconds=10 * (number - 1)),
                 finished=T0 + timedelta(seconds=10 * number),
                 failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(
        db, f"att_{comparison_id}_3", comparison_id, 3, final_status,
        started=T0 + timedelta(seconds=20),
        finished=None if final_status == "running" else T0 + timedelta(seconds=30),
        result_hash="h1" if final_status == "succeeded" else None,
        failure_code=failure_code if final_status == "failed" else None,
    )


def test_exhausted_gauge_counts_detecting_at_limit(db):
    """Issue-3 test 1: a detecting comparison at the budget is a current problem."""
    _at_limit(db, "cmpdetecting", "detecting", final_status="running")
    report = _summary(db, now=T0 + timedelta(hours=2))
    assert report["gauges"]["attempt_limit_exhausted_comparisons"] == 1


def test_exhausted_gauge_ignores_detecting_below_limit(db):
    """Issue-3 test 2."""
    _comparison(db, "cmp_under", "detecting", updated=T0 + timedelta(seconds=10))
    _attempt(db, "att_u1", "cmp_under", 1, "timed_out", started=T0,
             finished=T0 + timedelta(seconds=10),
             failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(db, "att_u2", "cmp_under", 2, "running",
             started=T0 + timedelta(seconds=10))
    report = _summary(db, now=T0 + timedelta(hours=2))
    assert report["gauges"]["attempt_limit_exhausted_comparisons"] == 0
    issue_types = {
        issue["issue_type"]
        for issue in _issues(db, now=T0 + timedelta(hours=2))["issues"]
    }
    assert comparison_reliability.ISSUE_ATTEMPT_LIMIT_EXHAUSTED not in issue_types


def test_exhausted_gauge_ignores_detected_at_limit(db):
    """Issue-3 test 3: a comparison that spent the budget and SUCCEEDED is not a
    current unresolved operational issue."""
    _at_limit(db, "cmpdetected", "detected", final_status="succeeded")
    report = _summary(db, now=T0 + timedelta(hours=2))
    assert report["gauges"]["comparisons_detected"] == 1
    assert report["gauges"]["attempt_limit_exhausted_comparisons"] == 0
    assert report["gauges"]["unresolved_operational_issues"] == 0
    # Historical counters are untouched by the gauge's scope.
    assert report["attempts"]["attempts_started"] == 3
    assert report["attempts"]["attempts_timed_out"] == 2
    assert report["attempts"]["attempts_succeeded"] == 1


def test_exhausted_gauge_ignores_failed_at_limit(db):
    """Issue-3 test 4: a failed comparison is reported as comparison_failed, and
    the attempt budget is not the operative constraint there."""
    _at_limit(db, "cmpfailed", "failed", final_status="failed",
              failure_code="detector_internal_error")
    report = _summary(db, now=T0 + timedelta(hours=2))
    assert report["gauges"]["comparisons_failed"] == 1
    assert report["gauges"]["attempt_limit_exhausted_comparisons"] == 0
    issue_types = [
        issue["issue_type"]
        for issue in _issues(db, now=T0 + timedelta(hours=2))["issues"]
    ]
    assert comparison_reliability.ISSUE_ATTEMPT_LIMIT_EXHAUSTED not in issue_types
    assert comparison_reliability.ISSUE_COMPARISON_FAILED in issue_types
    # Historical counters are untouched by the gauge's scope.
    assert report["attempts"]["attempts_started"] == 3


def test_exhausted_gauge_equals_the_exhausted_issue_count(db):
    """Issue-3 test 5: the gauge and the issue share one scope, so they agree on
    every mix of lifecycle states."""
    _at_limit(db, "cmpdetecting", "detecting", final_status="running")
    _at_limit(db, "cmpdetected", "detected", final_status="succeeded")
    _at_limit(db, "cmpfailed", "failed", final_status="failed",
              failure_code="detector_internal_error")
    _comparison(db, "cmp_under", "detecting", updated=T0)
    _attempt(db, "att_under", "cmp_under", 1, "running", started=T0)

    now = T0 + timedelta(hours=2)
    report = _summary(db, now=now)
    exhausted_issues = [
        issue
        for issue in _issues(db, now=now)["issues"]
        if issue["issue_type"] == comparison_reliability.ISSUE_ATTEMPT_LIMIT_EXHAUSTED
    ]
    assert report["gauges"]["attempt_limit_exhausted_comparisons"] == len(
        exhausted_issues
    ) == 1
    assert exhausted_issues[0]["comparison_id"] == "cmpdetecting"
    assert exhausted_issues[0]["status"] == "detecting"


def test_comparison_failed_issue(db):
    """Test 17."""
    _comparison(db, "cmp_f", "failed", updated=T0 + timedelta(seconds=9),
                failure_code="detector_internal_error")
    _attempt(db, "att_f", "cmp_f", 1, "failed", finished=T0 + timedelta(seconds=9),
             failure_code="detector_internal_error")

    issues = _issues(db)["issues"]
    assert len(issues) == 1
    issue = issues[0]
    assert issue["issue_type"] == comparison_reliability.ISSUE_COMPARISON_FAILED
    assert issue["status"] == "failed"
    assert issue["failure_code"] == "detector_internal_error"
    assert issue["attempt_id"] == "att_f"
    assert issue["detector_version"] == DETECTOR_VERSION
    assert issue["recommended_action_code"] == (
        comparison_reliability.ACTION_INSPECT_FAILURE
    )
    # created_at anchors on when the comparison became failed, and age follows.
    assert issue["created_at"] == (T0 + timedelta(seconds=9)).isoformat()
    assert issue["age_seconds"] == pytest.approx(51.0)


def test_replacement_attempt_failed_issue(db):
    """Test 18. A replay that did not fix the comparison is its own signal."""
    _comparison(db, "cmp_r", "failed", updated=T0 + timedelta(seconds=30),
                failure_code="detector_internal_error")
    _attempt(db, "att_r1", "cmp_r", 1, "timed_out", started=T0,
             finished=T0 + timedelta(seconds=10),
             failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(db, "att_r2", "cmp_r", 2, "failed", started=T0 + timedelta(seconds=10),
             finished=T0 + timedelta(seconds=30),
             failure_code="detector_internal_error")
    _replay(db, "rpl_r", "cmp_r", "att_r1", "att_r2",
            requested=T0 + timedelta(seconds=10))

    issues = _issues(db, now=T0 + timedelta(minutes=5))["issues"]
    replacement = [
        issue
        for issue in issues
        if issue["issue_type"]
        == comparison_reliability.ISSUE_REPLACEMENT_ATTEMPT_FAILED
    ]
    assert len(replacement) == 1
    assert replacement[0]["replay_id"] == "rpl_r"
    assert replacement[0]["attempt_id"] == "att_r2"
    assert replacement[0]["status"] == "failed"
    assert replacement[0]["created_at"] == (T0 + timedelta(seconds=30)).isoformat()
    # The comparison-level failure is reported alongside it, less severe.
    assert [issue["issue_type"] for issue in issues] == [
        comparison_reliability.ISSUE_REPLACEMENT_ATTEMPT_FAILED,
        comparison_reliability.ISSUE_COMPARISON_FAILED,
    ]


def test_timed_out_replacement_is_not_reported_as_a_replacement_failure(db):
    """A timed_out replacement was itself retired by a further replay, so its
    successor's state is the live signal."""
    _comparison(db, "cmp_c", "detected", updated=T0 + timedelta(seconds=30))
    _attempt(db, "att_c1", "cmp_c", 1, "timed_out", started=T0,
             finished=T0 + timedelta(seconds=10),
             failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(db, "att_c2", "cmp_c", 2, "timed_out",
             started=T0 + timedelta(seconds=10),
             finished=T0 + timedelta(seconds=20),
             failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(db, "att_c3", "cmp_c", 3, "succeeded",
             started=T0 + timedelta(seconds=20),
             finished=T0 + timedelta(seconds=30), result_hash="h1")
    _replay(db, "rpl_c1", "cmp_c", "att_c1", "att_c2",
            requested=T0 + timedelta(seconds=10))
    _replay(db, "rpl_c2", "cmp_c", "att_c2", "att_c3",
            requested=T0 + timedelta(seconds=20))

    report = _summary(db, now=T0 + timedelta(hours=1))
    assert report["replays"]["replay_replacements_timed_out"] == 1
    assert report["replays"]["replay_replacements_succeeded"] == 1
    assert report["gauges"]["unresolved_operational_issues"] == 0


def test_issue_ordering_is_deterministic(db):
    """Test 22. Severity, then oldest condition first, then stable ids."""
    # Two failed comparisons with distinct failure times, plus a stale attempt.
    _comparison(db, "cmp_f2", "failed", updated=T0 + timedelta(seconds=200),
                failure_code="detector_internal_error")
    _attempt(db, "att_f2", "cmp_f2", 1, "failed",
             finished=T0 + timedelta(seconds=200),
             failure_code="detector_internal_error")
    _comparison(db, "cmp_f1", "failed", updated=T0 + timedelta(seconds=100),
                failure_code="section_metadata_incomplete")
    _attempt(db, "att_f1", "cmp_f1", 1, "failed",
             finished=T0 + timedelta(seconds=100),
             failure_code="section_metadata_incomplete")
    _comparison(db, "cmp_run", "detecting")
    _attempt(db, "att_run", "cmp_run", 1, "running", started=T0)

    now = T0 + timedelta(hours=2)
    first = _issues(db, now=now)["issues"]
    assert [(issue["issue_type"], issue["comparison_id"]) for issue in first] == [
        (comparison_reliability.ISSUE_STALE_RUNNING_ATTEMPT, "cmp_run"),
        (comparison_reliability.ISSUE_COMPARISON_FAILED, "cmp_f1"),
        (comparison_reliability.ISSUE_COMPARISON_FAILED, "cmp_f2"),
    ]
    # Repeated calls are byte-identical.
    for _ in range(3):
        assert _issues(db, now=now)["issues"] == first


def test_issue_filters_and_bounded_limit(db):
    _comparison(db, "cmp_run", "detecting")
    _attempt(db, "att_run", "cmp_run", 1, "running", started=T0)
    _comparison(db, "cmp_f", "failed", updated=T0 + timedelta(seconds=9),
                failure_code="detector_internal_error")
    _attempt(db, "att_f", "cmp_f", 1, "failed", finished=T0 + timedelta(seconds=9),
             failure_code="detector_internal_error")

    now = T0 + timedelta(hours=2)
    assert _issues(db, now=now)["total"] == 2
    filtered = _issues(
        db, now=now, issue_type=comparison_reliability.ISSUE_COMPARISON_FAILED
    )
    assert filtered["total"] == 1
    assert filtered["issues"][0]["comparison_id"] == "cmp_f"
    assert _issues(db, now=now, comparison_id="cmp_run")["total"] == 1

    capped = _issues(db, now=now, limit=1)
    assert (capped["total"], capped["returned"], capped["truncated"]) == (2, 1, True)
    assert len(capped["issues"]) == 1
    uncapped = _issues(db, now=now)
    assert uncapped["truncated"] is False


def test_issue_records_carry_only_the_allowlisted_fields(db):
    _comparison(db, "cmp_run", "detecting")
    _attempt(db, "att_run", "cmp_run", 1, "running", started=T0)
    issue = _issues(db, now=T0 + timedelta(hours=2))["issues"][0]
    assert tuple(issue) == comparison_reliability.ISSUE_FIELDS
    assert issue["recommended_action_code"] in (
        comparison_reliability.RECOMMENDED_ACTION_CODES
    )


# --- 19-21: failure breakdowns and the failures listing -----------------------


def _failure_mix(db):
    codes = [
        ("cmp_1", "att_1", "failed", "detector_internal_error", DETECTOR_VERSION,
         WORKFLOW_VERSION),
        ("cmp_2", "att_2", "failed", "detector_internal_error", DETECTOR_VERSION,
         WORKFLOW_VERSION),
        ("cmp_3", "att_3", "failed", "section_metadata_incomplete",
         "item1a_detector.v1", "comparison_workflow.v1"),
        ("cmp_4", "att_4", "timed_out",
         comparison_store.FAILURE_ATTEMPT_TIMED_OUT, DETECTOR_VERSION,
         WORKFLOW_VERSION),
    ]
    for index, (cid, aid, status, code, detector, workflow) in enumerate(codes, 1):
        comparison_status = "failed" if status == "failed" else "detecting"
        _comparison(db, cid, comparison_status,
                    updated=T0 + timedelta(seconds=index),
                    failure_code=code if status == "failed" else None)
        _attempt(db, aid, cid, 1, status, started=T0 + timedelta(seconds=index),
                 finished=T0 + timedelta(seconds=index + 5), failure_code=code,
                 detector_version=detector, workflow_version=workflow)


def test_failure_breakdowns_by_code_and_version(db):
    """Tests 19, 20, 21."""
    _failure_mix(db)
    breakdown = _summary(db, now=T0 + timedelta(hours=1))["failure_breakdown"]
    assert breakdown["failed_attempts_by_code"] == {
        "detector_internal_error": 2,
        "section_metadata_incomplete": 1,
    }
    assert breakdown["timed_out_attempts_by_code"] == {
        comparison_store.FAILURE_ATTEMPT_TIMED_OUT: 1
    }
    # Version breakdowns count failed AND timed_out attempts.
    assert breakdown["failures_by_detector_version"] == {
        "item1a_detector.v1": 1,
        DETECTOR_VERSION: 3,
    }
    assert breakdown["failures_by_workflow_version"] == {
        "comparison_workflow.v1": 1,
        WORKFLOW_VERSION: 3,
    }


def test_failures_listing_filters_orders_and_bounds(db):
    _failure_mix(db)
    now = T0 + timedelta(hours=1)
    report = comparison_reliability.failures(now=now, db_path=db)
    assert report["total"] == 4
    # Newest first by started_at.
    assert [item["attempt_id"] for item in report["failures"]] == [
        "att_4", "att_3", "att_2", "att_1",
    ]
    assert tuple(report["failures"][0]) == comparison_reliability.FAILURE_FIELDS
    assert report["failures"][0]["duration_seconds"] == 5.0

    assert comparison_reliability.failures(
        now=now, db_path=db, failure_code="detector_internal_error"
    )["total"] == 2
    assert comparison_reliability.failures(
        now=now, db_path=db, detector_version="item1a_detector.v1"
    )["total"] == 1
    assert comparison_reliability.failures(
        now=now, db_path=db, workflow_version="comparison_workflow.v1"
    )["total"] == 1
    assert comparison_reliability.failures(
        now=now, db_path=db, comparison_id="cmp_2"
    )["total"] == 1
    windowed = comparison_reliability.failures(
        now=now, db_path=db, since=(T0 + timedelta(seconds=3)).isoformat()
    )
    assert [item["attempt_id"] for item in windowed["failures"]] == ["att_4", "att_3"]

    capped = comparison_reliability.failures(now=now, db_path=db, limit=2)
    assert (capped["total"], capped["returned"], capped["truncated"]) == (4, 2, True)
    # A succeeded attempt never appears in the failures listing.
    assert all(
        item["status"] in ("failed", "timed_out") for item in report["failures"]
    )


def test_failures_listing_links_replacements_to_their_replay(db):
    _comparison(db, "cmp_r", "failed", updated=T0 + timedelta(seconds=30),
                failure_code="detector_internal_error")
    _attempt(db, "att_r1", "cmp_r", 1, "timed_out", started=T0,
             finished=T0 + timedelta(seconds=10),
             failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(db, "att_r2", "cmp_r", 2, "failed", started=T0 + timedelta(seconds=10),
             finished=T0 + timedelta(seconds=30),
             failure_code="detector_internal_error")
    _replay(db, "rpl_r", "cmp_r", "att_r1", "att_r2",
            requested=T0 + timedelta(seconds=10))

    report = comparison_reliability.failures(now=T0 + timedelta(hours=1), db_path=db)
    replacement = next(
        item for item in report["failures"] if item["attempt_id"] == "att_r2"
    )
    assert replacement["replay_id"] == "rpl_r"
    assert replacement["source_attempt_id"] == "att_r1"
    source = next(item for item in report["failures"] if item["attempt_id"] == "att_r1")
    assert source["replay_id"] is None


# --- Data validation (section I): fail closed ---------------------------------


def test_terminal_attempt_without_finished_at_is_refused(db):
    _comparison(db, "cmp_a", "failed", failure_code="detector_internal_error")
    _attempt(db, "att_a", "cmp_a", 1, "failed", finished=T0 + timedelta(seconds=1),
             failure_code="detector_internal_error")
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET finished_at = NULL "
        "WHERE attempt_id = 'att_a'",
        ignore_checks=True,
    )
    with pytest.raises(comparison_reliability.ReliabilityDataError) as excinfo:
        _summary(db)
    assert excinfo.value.reasons == [
        comparison_reliability.DATA_TERMINAL_MISSING_FINISH
    ]
    assert excinfo.value.code == comparison_reliability.CODE_DATA_INVALID


def test_running_attempt_with_finished_at_is_refused(db):
    _comparison(db, "cmp_a", "detecting")
    _attempt(db, "att_a", "cmp_a", 1, "running")
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET finished_at = ? "
        "WHERE attempt_id = 'att_a'",
        ((T0 + timedelta(seconds=1)).isoformat(),),
        ignore_checks=True,
    )
    with pytest.raises(comparison_reliability.ReliabilityDataError) as excinfo:
        _summary(db)
    assert excinfo.value.reasons == [comparison_reliability.DATA_RUNNING_HAS_FINISH]


def test_unknown_attempt_status_is_refused(db):
    _comparison(db, "cmp_a", "detecting")
    _attempt(db, "att_a", "cmp_a", 1, "running")
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET status = 'wedged' "
        "WHERE attempt_id = 'att_a'",
        ignore_checks=True,
    )
    with pytest.raises(comparison_reliability.ReliabilityDataError) as excinfo:
        _summary(db)
    assert comparison_reliability.DATA_UNKNOWN_ATTEMPT_STATUS in excinfo.value.reasons


def test_unknown_comparison_status_is_refused(db):
    _comparison(db, "cmp_a", "detecting")
    _sql(
        db,
        "UPDATE comparisons SET status = 'archived' WHERE comparison_id = 'cmp_a'",
        ignore_checks=True,
    )
    with pytest.raises(comparison_reliability.ReliabilityDataError) as excinfo:
        _summary(db)
    assert excinfo.value.reasons == [
        comparison_reliability.DATA_UNKNOWN_COMPARISON_STATUS
    ]


def test_replay_referencing_a_missing_attempt_is_refused(db):
    _comparison(db, "cmp_a", "detecting")
    _attempt(db, "att_a1", "cmp_a", 1, "timed_out",
             finished=T0 + timedelta(seconds=1),
             failure_code=comparison_store.FAILURE_ATTEMPT_TIMED_OUT)
    _attempt(db, "att_a2", "cmp_a", 2, "running", started=T0 + timedelta(seconds=1))
    _replay(db, "rpl_a", "cmp_a", "att_a1", "att_a2")
    _sql(
        db,
        "UPDATE comparison_detection_replays SET replacement_attempt_id = 'att_gone' "
        "WHERE replay_id = 'rpl_a'",
    )
    with pytest.raises(comparison_reliability.ReliabilityDataError) as excinfo:
        _summary(db)
    assert excinfo.value.reasons == [
        comparison_reliability.DATA_REPLAY_MISSING_REPLACEMENT
    ]

    _sql(
        db,
        "UPDATE comparison_detection_replays SET replacement_attempt_id = 'att_a2', "
        "source_attempt_id = 'att_gone' WHERE replay_id = 'rpl_a'",
    )
    with pytest.raises(comparison_reliability.ReliabilityDataError) as excinfo:
        _summary(db)
    assert excinfo.value.reasons == [comparison_reliability.DATA_REPLAY_MISSING_SOURCE]


def test_unparsable_stored_timestamp_is_refused(db):
    """Naive stored timestamps fail the report closed rather than shifting
    every age and duration by an unknown offset."""
    _comparison(db, "cmp_a", "detecting")
    _attempt(db, "att_a", "cmp_a", 1, "running")
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET started_at = '2026-07-29T12:00:00' "
        "WHERE attempt_id = 'att_a'",
    )
    with pytest.raises(comparison_reliability.ReliabilityDataError) as excinfo:
        _summary(db)
    assert excinfo.value.reasons == [comparison_reliability.DATA_UNPARSABLE_TIMESTAMP]


def test_data_error_refuses_issues_and_failures_too(db):
    """No partial metrics after a structural error, on any surface."""
    _comparison(db, "cmp_a", "detecting")
    _attempt(db, "att_a", "cmp_a", 1, "running")
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET status = 'wedged' "
        "WHERE attempt_id = 'att_a'",
        ignore_checks=True,
    )
    for call in (
        lambda: _summary(db),
        lambda: _issues(db),
        lambda: comparison_reliability.failures(now=T0, db_path=db),
    ):
        with pytest.raises(comparison_reliability.ReliabilityDataError):
            call()


# --- Dependency availability: fail closed, never a false clean zero ----------


def _stale_running(db, comparison_id="cmp_run", attempt_id="att_run"):
    _comparison(db, comparison_id, "detecting")
    _attempt(db, attempt_id, comparison_id, 1, "running", started=T0)


def test_no_running_attempts_reports_exact_zero_without_the_registry(db, tmp_path):
    """Nothing needs a recovery evaluation, so an absent registry is irrelevant
    and the zero is exact rather than a substitute for an unanswered question."""
    absent = tmp_path / "absent-registry.jsonl"
    assert not absent.exists()
    _comparison(db, "cmp_ok", "detected", updated=T0 + timedelta(seconds=4))
    _attempt(db, "att_ok", "cmp_ok", 1, "succeeded",
             finished=T0 + timedelta(seconds=4), result_hash="h1")

    report = _summary(db, registry_path=absent)
    assert report["gauges"]["running_attempts"] == 0
    assert report["gauges"]["stale_running_attempts"] == 0
    assert report["gauges"]["replay_eligible_attempts"] == 0
    assert report["gauges"]["unresolved_operational_issues"] == 0
    assert _issues(db, registry_path=absent)["total"] == 0
    # The registry was never consulted, so it was never created either.
    assert not absent.exists()


def test_running_but_not_stale_needs_no_registry(db, tmp_path):
    """A non-stale attempt can never be replay-eligible, so it requires no
    recovery evaluation and therefore no registry."""
    absent = tmp_path / "absent-registry.jsonl"
    _stale_running(db)
    report = _summary(db, now=T0 + timedelta(seconds=STALE_AFTER - 1),
                      registry_path=absent)
    assert report["gauges"]["running_attempts"] == 1
    assert report["gauges"]["stale_running_attempts"] == 0
    assert report["gauges"]["replay_eligible_attempts"] == 0


def test_stale_attempt_with_absent_registry_fails_closed(db, tmp_path):
    """The core correction: eligibility cannot be evaluated, so the report is
    refused instead of reporting zero eligible attempts."""
    absent = tmp_path / "absent-registry.jsonl"
    _stale_running(db)
    with pytest.raises(comparison_reliability.ReliabilityDependencyUnavailable) as exc:
        _summary(db, now=T0 + timedelta(hours=2), registry_path=absent)
    assert exc.value.code == comparison_reliability.CODE_DEPENDENCY_UNAVAILABLE
    assert exc.value.dependency == comparison_reliability.DEPENDENCY_FILING_REGISTRY
    assert exc.value.reason == comparison_reliability.DEPENDENCY_REGISTRY_ABSENT


def test_stale_attempt_with_malformed_registry_fails_closed(db, tmp_path):
    """A registry that cannot be parsed is unavailable, not empty."""
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text('{"filing_id": "broken"\nnot json at all\n', encoding="utf-8")
    _stale_running(db)
    with pytest.raises(comparison_reliability.ReliabilityDependencyUnavailable) as exc:
        _summary(db, now=T0 + timedelta(hours=2), registry_path=malformed)
    assert exc.value.reason == comparison_reliability.DEPENDENCY_REGISTRY_UNREADABLE


def test_stale_attempt_with_empty_registry_fails_closed(db, tmp_path):
    """A present-but-empty registry lost the data the metric depends on: a
    comparison cannot be created without registry entries."""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    _stale_running(db)
    with pytest.raises(comparison_reliability.ReliabilityDependencyUnavailable) as exc:
        _summary(db, now=T0 + timedelta(hours=2), registry_path=empty)
    assert exc.value.reason == comparison_reliability.DEPENDENCY_REGISTRY_EMPTY


def test_dependency_failure_is_never_substituted_with_a_partial_result(db, tmp_path):
    """Requirement 7: no zero, no empty list, no replay_eligible=false."""
    absent = tmp_path / "absent-registry.jsonl"
    _stale_running(db)
    now = T0 + timedelta(hours=2)
    for call in (
        lambda: _summary(db, now=now, registry_path=absent),
        lambda: _issues(db, now=now, registry_path=absent),
        # Even a filter that would exclude the stale issue still fails closed,
        # because the full set is generated before filtering.
        lambda: _issues(
            db, now=now, registry_path=absent,
            issue_type=comparison_reliability.ISSUE_COMPARISON_FAILED,
        ),
        lambda: _issues(db, now=now, registry_path=absent, comparison_id="cmp_other"),
    ):
        with pytest.raises(comparison_reliability.ReliabilityDependencyUnavailable):
            call()


def test_failures_listing_never_depends_on_the_registry(db, tmp_path):
    """The failures calculation requires no recovery eligibility, so it keeps
    working with no registry at all — documented, not accidental."""
    absent = tmp_path / "absent-registry.jsonl"
    _stale_running(db)
    _comparison(db, "cmp_f", "failed", updated=T0 + timedelta(seconds=9),
                failure_code="detector_internal_error")
    _attempt(db, "att_f", "cmp_f", 1, "failed", finished=T0 + timedelta(seconds=9),
             failure_code="detector_internal_error")
    # Not given a registry path at all, and the summary for this same state
    # would fail closed.
    report = comparison_reliability.failures(now=T0 + timedelta(hours=2), db_path=db)
    assert report["total"] == 1
    assert report["failures"][0]["attempt_id"] == "att_f"
    assert not absent.exists()


def test_valid_registry_behavior_is_unchanged(db):
    """A readable registry that simply does not know a synthetic pair ANSWERS:
    the attempt is genuinely not eligible, and the report succeeds."""
    _stale_running(db)
    now = T0 + timedelta(hours=2)
    report = _summary(db, now=now)
    assert report["gauges"]["stale_running_attempts"] == 1
    assert report["gauges"]["replay_eligible_attempts"] == 0
    issues = _issues(db, now=now)["issues"]
    assert len(issues) == 1
    assert issues[0]["issue_type"] == (
        comparison_reliability.ISSUE_STALE_RUNNING_ATTEMPT
    )
    assert issues[0]["recommended_action_code"] == (
        comparison_reliability.ACTION_NO_REPLAY_AVAILABLE
    )
    # And the recovery view reaches the same conclusion under its own code.
    view = detection_recovery.recovery_view(
        "att_run", now=now, db_path=db, registry_path=_registry_for(db)
    )
    assert view["is_stale"] is True
    assert view["replay_eligible"] is False


def test_a_genuinely_eligible_attempt_still_reports_eligible(corpus, tmp_path):
    """The honest positive case: a real registry, a real pair, a stale attempt
    that a replay WOULD accept."""
    db = tmp_path / "eligible.db"
    comparison_store.init_db(db)
    record, _ = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry
    )
    previous = filing_registry.get_filing(PREV_ID, corpus.registry)["source_hash"]
    current = filing_registry.get_filing(CURR_ID, corpus.registry)["source_hash"]
    attempt = comparison_store.start_detection_attempt(
        record["comparison_id"],
        detector_version=DETECTOR_VERSION,
        workflow_version=WORKFLOW_VERSION,
        previous_source_hash=previous,
        current_source_hash=current,
        db_path=db,
    )
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET started_at = ? WHERE attempt_id = ?",
        (T0.isoformat(), attempt["attempt_id"]),
    )
    now = T0 + timedelta(hours=2)
    report = comparison_reliability.summary(
        now=now, db_path=db, registry_path=corpus.registry
    )
    assert report["gauges"]["stale_running_attempts"] == 1
    assert report["gauges"]["replay_eligible_attempts"] == 1
    issues = comparison_reliability.issues(
        now=now, db_path=db, registry_path=corpus.registry
    )["issues"]
    assert issues[0]["recommended_action_code"] == (
        comparison_reliability.ACTION_INSPECT_AND_REPLAY
    )
    # Removing the registry turns that same state into a refusal, not a zero.
    with pytest.raises(comparison_reliability.ReliabilityDependencyUnavailable):
        comparison_reliability.summary(
            now=now, db_path=db, registry_path=tmp_path / "gone.jsonl"
        )


# --- Transitively read-only: no reliability path can reach init_db -----------


def _poison_init_db(monkeypatch):
    """After this, ANY init_db call fails the test loudly. The reliability
    read path must keep working anyway."""

    def boom(*args, **kwargs):
        raise AssertionError(
            "comparison_store.init_db is unreachable from the reliability "
            "read path; something initializing was called"
        )

    monkeypatch.setattr(comparison_store, "init_db", boom)


def test_summary_issues_and_failures_never_reach_init_db(db, monkeypatch):
    """Poison test 1: a stale running attempt plus a valid registry, with
    init_db raising — the whole report must still be produced."""
    _stale_running(db)
    _poison_init_db(monkeypatch)
    now = T0 + timedelta(hours=2)

    report = _summary(db, now=now)
    assert report["gauges"]["stale_running_attempts"] == 1
    assert report["gauges"]["replay_eligible_attempts"] == 0
    assert report["gauges"]["unresolved_operational_issues"] == 1

    issues = _issues(db, now=now)
    assert issues["total"] == 1
    assert issues["issues"][0]["issue_type"] == (
        comparison_reliability.ISSUE_STALE_RUNNING_ATTEMPT
    )
    assert issues["issues"][0]["recommended_action_code"] == (
        comparison_reliability.ACTION_NO_REPLAY_AVAILABLE
    )
    assert comparison_reliability.failures(now=now, db_path=db)["total"] == 0


def test_eligible_verdict_never_reaches_init_db(corpus, tmp_path, monkeypatch):
    """Poison test 1 (positive direction): the full success path — registry
    resolution, matching hashes, replay_eligible=True — without init_db."""
    db = tmp_path / "eligible.db"
    comparison_store.init_db(db)
    record, _ = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry
    )
    previous = filing_registry.get_filing(PREV_ID, corpus.registry)["source_hash"]
    current = filing_registry.get_filing(CURR_ID, corpus.registry)["source_hash"]
    attempt = comparison_store.start_detection_attempt(
        record["comparison_id"],
        detector_version=DETECTOR_VERSION,
        workflow_version=WORKFLOW_VERSION,
        previous_source_hash=previous,
        current_source_hash=current,
        db_path=db,
    )
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET started_at = ? WHERE attempt_id = ?",
        (T0.isoformat(), attempt["attempt_id"]),
    )

    _poison_init_db(monkeypatch)
    now = T0 + timedelta(hours=2)
    report = comparison_reliability.summary(
        now=now, db_path=db, registry_path=corpus.registry
    )
    assert report["gauges"]["stale_running_attempts"] == 1
    assert report["gauges"]["replay_eligible_attempts"] == 1
    issues = comparison_reliability.issues(
        now=now, db_path=db, registry_path=corpus.registry
    )["issues"]
    assert issues[0]["recommended_action_code"] == (
        comparison_reliability.ACTION_INSPECT_AND_REPLAY
    )


def test_api_reliability_routes_never_reach_init_db(api_db, monkeypatch):
    """Poison test 2: all three endpoints answer with init_db raising."""
    _stale_running(api_db)
    _poison_init_db(monkeypatch)
    summary = client.get("/api/comparison-reliability/summary")
    issues = client.get("/api/comparison-reliability/issues")
    failures = client.get("/api/comparison-reliability/failures")
    assert (summary.status_code, issues.status_code, failures.status_code) == (
        200, 200, 200,
    )
    assert summary.json()["gauges"]["staleRunningAttempts"] == 1
    assert issues.json()["total"] == 1


def test_cli_never_reaches_init_db(db, monkeypatch, capsys):
    """Poison test 3: the operator CLI reports without initializing storage."""
    _stale_running(db)
    _poison_init_db(monkeypatch)
    assert _cli(["--db-path", str(db), "--json", "--issues", "--failures"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["gauges"]["stale_running_attempts"] == 1
    assert payload["issues"]["total"] == 1


# --- Partial schema: only the reliability tables exist ------------------------

# Tables the reliability path must NEVER create — present in the full store
# schema, deliberately absent from the partial database below.
_UNRELATED_TABLES = (
    "comparison_results",
    "comparison_governance_evaluations",
    "comparison_review_items",
    "comparison_review_events",
    "comparison_exports",
    "comparison_detection_events",
)


def _partial_reliability_db(path):
    """A database holding exactly the five reliability source tables.

    Hand-written DDL with the real column names, so the allowlisted SELECTs
    succeed while every unrelated store table is deliberately missing.
    """
    with closing(sqlite3.connect(str(path))) as conn, conn:
        conn.execute(
            "CREATE TABLE comparisons (comparison_id TEXT PRIMARY KEY, "
            "schema_version TEXT, workflow_version TEXT, previous_filing_id TEXT, "
            "current_filing_id TEXT, section_scope TEXT, status TEXT, "
            "created_at TEXT, updated_at TEXT, failure_code TEXT, "
            "failure_summary TEXT)"
        )
        conn.execute(
            "CREATE TABLE comparison_detection_attempts (attempt_id TEXT PRIMARY "
            "KEY, comparison_id TEXT, attempt_number INTEGER, status TEXT, "
            "detector_version TEXT, workflow_version TEXT, "
            "previous_source_hash TEXT, current_source_hash TEXT, started_at TEXT, "
            "finished_at TEXT, result_hash TEXT, failure_code TEXT, "
            "failure_summary TEXT)"
        )
        conn.execute(
            "CREATE TABLE comparison_detection_jobs (job_id TEXT PRIMARY KEY, "
            "comparison_id TEXT, attempt_id TEXT, trigger_type TEXT, status TEXT, "
            "detector_version TEXT, workflow_version TEXT, queued_at TEXT, "
            "claimed_at TEXT, finished_at TEXT, worker_id TEXT, result_hash TEXT, "
            "failure_code TEXT, claim_generation INTEGER, lease_started_at TEXT, "
            "heartbeat_at TEXT, lease_expires_at TEXT, retry_count INTEGER, "
            "next_attempt_at TEXT, last_failure_code TEXT, "
            "last_failure_classification TEXT, last_failure_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE comparison_detection_job_events ("
            "event_id TEXT PRIMARY KEY, job_id TEXT, comparison_id TEXT, "
            "attempt_id TEXT, event_type TEXT, event_seq INTEGER, created_at TEXT, "
            "worker_id TEXT, claim_generation INTEGER, source_attempt_id TEXT, "
            "replacement_attempt_id TEXT, lease_expires_at TEXT, result_hash TEXT, "
            "failure_code TEXT, retry_count INTEGER, "
            "failure_classification TEXT, next_attempt_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE comparison_detection_replays (replay_id TEXT PRIMARY "
            "KEY, comparison_id TEXT, source_attempt_id TEXT, "
            "replacement_attempt_id TEXT, operator_id TEXT, reason_code TEXT, "
            "operator_note TEXT, request_hash TEXT, policy_id TEXT, "
            "policy_version TEXT, requested_at TEXT)"
        )
    return path


def _table_names(path):
    with closing(sqlite3.connect(str(path))) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def test_partial_schema_service_reads_are_correct_and_create_nothing(tmp_path):
    """Partial-schema tests 4-6: the five reliability tables suffice, and no
    absent unrelated table comes into existence."""
    path = _partial_reliability_db(tmp_path / "partial.db")
    _write_registry(tmp_path / "registry.jsonl")
    _comparison(path, "cmp_run", "detecting")
    _attempt(path, "att_run", "cmp_run", 1, "running", started=T0)

    assert _table_names(path) & set(_UNRELATED_TABLES) == set()
    objects_before = _schema_objects(path)
    identity_before = _file_identity(path)

    now = T0 + timedelta(hours=2)
    registry = tmp_path / "registry.jsonl"
    report = comparison_reliability.summary(
        now=now, db_path=path, registry_path=registry
    )
    assert report["gauges"]["comparisons_detecting"] == 1
    assert report["gauges"]["stale_running_attempts"] == 1
    assert report["gauges"]["replay_eligible_attempts"] == 0
    issues = comparison_reliability.issues(
        now=now, db_path=path, registry_path=registry
    )
    assert issues["total"] == 1
    assert issues["issues"][0]["issue_type"] == (
        comparison_reliability.ISSUE_STALE_RUNNING_ATTEMPT
    )
    assert comparison_reliability.failures(now=now, db_path=path)["total"] == 0

    # No unrelated table was created, sqlite_master is row-for-row identical,
    # the file bytes/size/mtime are untouched, and no sidecar exists.
    assert _table_names(path) & set(_UNRELATED_TABLES) == set()
    assert _schema_objects(path) == objects_before
    assert _file_identity(path) == identity_before
    assert not (tmp_path / "partial.db-journal").exists()
    assert not (tmp_path / "partial.db-wal").exists()


def test_partial_schema_api_and_cli_create_nothing(tmp_path, monkeypatch, capsys):
    """The same partial database through the API routes and the operator CLI."""
    path = _partial_reliability_db(tmp_path / "partial.db")
    registry = _write_registry(tmp_path / "registry.jsonl")
    _comparison(path, "cmp_run", "detecting")
    _attempt(path, "att_run", "cmp_run", 1, "running", started=T0)
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(path))
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(registry))
    objects_before = _schema_objects(path)
    identity_before = _file_identity(path)

    for route in ("summary", "issues", "failures"):
        assert client.get(f"/api/comparison-reliability/{route}").status_code == 200
    assert client.get("/api/comparison-reliability/summary").json()["gauges"][
        "staleRunningAttempts"
    ] == 1

    assert _cli(["--db-path", str(path), "--issues", "--failures"]) == 0
    assert "stale_running_attempt" in capsys.readouterr().out

    assert _table_names(path) & set(_UNRELATED_TABLES) == set()
    assert _schema_objects(path) == objects_before
    assert _file_identity(path) == identity_before
    assert not (tmp_path / "partial.db-journal").exists()
    assert not (tmp_path / "partial.db-wal").exists()


# --- The pure recovery calculation --------------------------------------------


def _pure_view(**overrides):
    inputs = dict(
        comparison={"comparison_id": "cmp_p", "status": "detecting"},
        source_attempt={
            "attempt_id": "att_p",
            "comparison_id": "cmp_p",
            "attempt_number": 1,
            "status": "running",
            "detector_version": DETECTOR_VERSION,
            "workflow_version": WORKFLOW_VERSION,
            "previous_source_hash": "prev-hash",
            "current_source_hash": "curr-hash",
            "started_at": T0.isoformat(),
        },
        attempts_used=1,
        existing_replay=None,
        resolve_source_hashes=lambda: ("prev-hash", "curr-hash"),
        policy={
            "policy_id": "detection_recovery_v1",
            "policy_version": "1",
            "stale_after_seconds": STALE_AFTER,
            "max_attempts_per_comparison": 3,
        },
        now=T0 + timedelta(seconds=STALE_AFTER),
    )
    inputs.update(overrides)
    return detection_recovery.build_recovery_view_from_records(**inputs)


def test_pure_view_eligible_fields():
    """Pure-function test 8: the storage-independent calculation on its own."""
    view = _pure_view()
    assert view["replay_eligible"] is True
    assert view["blocking_reason"] is None
    assert view["is_stale"] is True
    assert view["age_seconds"] == float(STALE_AFTER)
    assert view["stale_at"] == (T0 + timedelta(seconds=STALE_AFTER)).isoformat()
    assert (view["attempts_used"], view["max_attempts"]) == (1, 3)
    assert view["remaining_attempts"] == 2
    assert view["attempt_id"] == "att_p"
    assert view["comparison_id"] == "cmp_p"
    assert view["policy_id"] == "detection_recovery_v1"


@pytest.mark.parametrize(
    "overrides,expected_block",
    [
        ({"existing_replay": {"replay_id": "rpl_x"}},
         detection_recovery.BLOCK_ALREADY_REPLAYED),
        ({"comparison": None}, detection_recovery.BLOCK_LIFECYCLE_INVALID),
        ({"comparison": {"comparison_id": "cmp_p", "status": "detected"}},
         detection_recovery.BLOCK_LIFECYCLE_INVALID),
        ({"now": T0 + timedelta(seconds=STALE_AFTER - 1)},
         detection_recovery.BLOCK_NOT_STALE),
        ({"attempts_used": 3}, detection_recovery.BLOCK_LIMIT_REACHED),
        ({"resolve_source_hashes": lambda: ("prev-hash", "OTHER")},
         detection_recovery.BLOCK_INPUTS_CHANGED),
    ],
)
def test_pure_view_blocking_codes(overrides, expected_block):
    view = _pure_view(**overrides)
    assert view["replay_eligible"] is False
    assert view["blocking_reason"] == expected_block


def test_pure_view_not_running_and_version_changed():
    base = {
        "attempt_id": "att_p", "comparison_id": "cmp_p", "attempt_number": 1,
        "status": "succeeded", "detector_version": DETECTOR_VERSION,
        "workflow_version": WORKFLOW_VERSION, "previous_source_hash": "prev-hash",
        "current_source_hash": "curr-hash", "started_at": T0.isoformat(),
    }
    view = _pure_view(source_attempt=base)
    assert view["blocking_reason"] == detection_recovery.BLOCK_NOT_RUNNING
    view = _pure_view(source_attempt={**base, "status": "running",
                                      "detector_version": "item1a_detector.v1"})
    assert view["blocking_reason"] == detection_recovery.BLOCK_VERSION_CHANGED


def test_pure_view_resolver_exception_is_inputs_changed_and_lazy():
    """A raising resolver means inputs changed — and the resolver is not even
    consulted when an earlier check already blocks."""
    calls = {"count": 0}

    def resolver():
        calls["count"] += 1
        raise RuntimeError("registry says no")

    view = _pure_view(resolve_source_hashes=resolver)
    assert view["blocking_reason"] == detection_recovery.BLOCK_INPUTS_CHANGED
    assert calls["count"] == 1

    view = _pure_view(resolve_source_hashes=resolver, attempts_used=3)
    assert view["blocking_reason"] == detection_recovery.BLOCK_LIMIT_REACHED
    assert calls["count"] == 1  # unchanged: blocked before resolution


def test_pure_view_matches_storage_backed_recovery_view(db):
    """The delegating recovery_view and a hand-fed pure call agree exactly."""
    _stale_running(db)
    now = T0 + timedelta(hours=2)
    stored = detection_recovery.recovery_view(
        "att_run", now=now, db_path=db, registry_path=_registry_for(db)
    )
    snapshot = comparison_store.read_reliability_snapshot(db)
    attempt = next(a for a in snapshot["attempts"] if a["attempt_id"] == "att_run")
    comparison = next(
        c for c in snapshot["comparisons"] if c["comparison_id"] == "cmp_run"
    )

    def resolver():
        return comparison_reliability._registry_source_hashes(
            comparison, _registry_for(db)
        )

    pure = detection_recovery.build_recovery_view_from_records(
        comparison=comparison,
        source_attempt=attempt,
        attempts_used=snapshot["attempts_per_comparison"]["cmp_run"],
        existing_replay=None,
        resolve_source_hashes=resolver,
        policy=detection_recovery.POLICY,
        now=now,
    )
    assert pure == stored


def test_api_recovery_get_matches_reliability_calculation(corpus, tmp_path, monkeypatch):
    """Parity test 7: the recovery GET and the reliability summary/issues agree
    on the same attempt — in the ELIGIBLE direction, over a real registry."""
    db = tmp_path / "parity.db"
    comparison_store.init_db(db)
    record, _ = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry
    )
    previous = filing_registry.get_filing(PREV_ID, corpus.registry)["source_hash"]
    current = filing_registry.get_filing(CURR_ID, corpus.registry)["source_hash"]
    attempt = comparison_store.start_detection_attempt(
        record["comparison_id"],
        detector_version=DETECTOR_VERSION,
        workflow_version=WORKFLOW_VERSION,
        previous_source_hash=previous,
        current_source_hash=current,
        db_path=db,
    )
    attempt_id = attempt["attempt_id"]
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET started_at = ? WHERE attempt_id = ?",
        ((datetime.now(timezone.utc) - timedelta(seconds=5000)).isoformat(),
         attempt_id),
    )
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(db))
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(corpus.registry))

    recovery = client.get(f"/api/detection-attempts/{attempt_id}/recovery").json()
    summary = client.get("/api/comparison-reliability/summary").json()
    issue = client.get("/api/comparison-reliability/issues").json()["issues"][0]

    assert recovery["isStale"] is True
    assert recovery["replayEligible"] is True
    assert recovery["blockingReason"] is None
    assert summary["gauges"]["staleRunningAttempts"] == 1
    assert summary["gauges"]["replayEligibleAttempts"] == 1
    assert issue["issueType"] == "stale_running_attempt"
    assert issue["recommendedActionCode"] == "inspect_and_replay_if_valid"
    assert issue["attemptId"] == recovery["attemptId"]
    assert issue["staleAt"] == recovery["staleAt"]
    assert issue["attemptsUsed"] == recovery["attemptsUsed"]
    assert issue["maxAttempts"] == recovery["maxAttempts"]


# --- Structural guard: the reliability module cannot initialize anything ------

# Store entry points that create, migrate, or lazily initialize storage. The
# reliability module must never CALL any of these (importing the module that
# defines them is fine).
_INITIALIZING_OR_MUTATING_CALLS = frozenset({
    "init_db", "recovery_view", "replay_attempt", "resolve_detection_inputs",
    "detect_with_attempt", "execute_attempt", "detect",
    # writers
    "create_comparison", "start_detection_attempt", "complete_detection_attempt",
    "fail_detection_attempt", "start_detection_replay", "record_result",
    "record_evaluation", "decide_review", "record_export", "mark_failed",
    # ordinary getters — every one lazily calls init_db
    "get_comparison", "list_comparisons", "get_result", "get_detection_attempt",
    "get_running_detection_attempt", "list_detection_attempts",
    "list_detection_events", "get_detection_replay_for_source",
    "list_detection_replays", "count_detection_attempts", "get_evaluation",
    "list_evaluations", "list_comparison_reviews", "get_review_item",
    "list_review_events", "get_export", "list_exports",
})


def test_reliability_module_calls_no_initializing_path():
    """Guard test 12 (AST, not substring): comparison_reliability.py never
    calls init_db, recovery_view, or any lazily-initializing store getter."""
    tree = ast.parse(
        (REPO_ROOT / "comparison_reliability.py").read_text(encoding="utf-8")
    )
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name in _INITIALIZING_OR_MUTATING_CALLS:
                offenders.append(f"{name}:{node.lineno}")
    assert offenders == [], offenders

    # The only detection_recovery surface used is the pure calculation and the
    # loaded policy; comparison_detector is not referenced at all.
    recovery_attrs = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "detection_recovery"
    }
    assert recovery_attrs <= {"build_recovery_view_from_records", "POLICY"}
    referenced_names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert "comparison_detector" not in referenced_names
    assert "sqlite3" not in referenced_names


def test_pure_recovery_functions_touch_no_storage():
    """The extracted calculation itself is storage-free: its AST uses only pure
    comparison_store helpers/constants and never sqlite3 or a db path."""
    tree = ast.parse((REPO_ROOT / "detection_recovery.py").read_text(encoding="utf-8"))
    pure_functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in ("build_recovery_view_from_records",
                          "_blocking_reason_from_records")
    ]
    assert len(pure_functions) == 2
    for function in pure_functions:
        store_attrs = {
            node.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "comparison_store"
        }
        assert store_attrs <= {
            "evaluate_staleness", "STATUS_DETECTING", "ATTEMPT_RUNNING",
            "JOB_RUNNING", "WORKFLOW_VERSION",
        }, (function.name, store_attrs)
        referenced = {
            node.id for node in ast.walk(function) if isinstance(node, ast.Name)
        }
        assert "sqlite3" not in referenced, function.name
        argument_names = {arg.arg for arg in function.args.kwonlyargs}
        assert "db_path" not in argument_names, function.name


# --- Store read surface -------------------------------------------------------


def test_snapshot_is_read_only_and_column_allowlisted(db):
    _mixed(db)
    before = hashlib.sha256(Path(db).read_bytes()).hexdigest()
    snapshot = comparison_store.read_reliability_snapshot(db)
    assert hashlib.sha256(Path(db).read_bytes()).hexdigest() == before

    assert set(snapshot) == {
        "comparisons", "attempts", "jobs", "job_events", "replays",
        "attempts_per_comparison",
    }
    assert set(snapshot["comparisons"][0]) == set(
        comparison_store._RELIABILITY_COMPARISON_COLUMNS
    )
    assert set(snapshot["attempts"][0]) == set(
        comparison_store._RELIABILITY_ATTEMPT_COLUMNS
    )
    assert snapshot["jobs"] == []
    assert snapshot["job_events"] == []
    replay_keys = set(snapshot["replays"][0])
    assert replay_keys == set(comparison_store._RELIABILITY_REPLAY_COLUMNS)
    # Operator prose can never reach reliability output: it is not even read.
    assert "operator_id" not in replay_keys
    assert "operator_note" not in replay_keys
    assert snapshot["attempts_per_comparison"]["cmp_d"] == 2
    # Deterministic ordering, and repeated reads are identical.
    def ordering(attempt):
        return (
            attempt["started_at"], attempt["comparison_id"], attempt["attempt_number"]
        )

    assert snapshot["attempts"] == sorted(snapshot["attempts"], key=ordering)
    assert comparison_store.read_reliability_snapshot(db) == snapshot


def test_snapshot_refuses_to_write_even_when_asked(db):
    """The read handle is mode=ro: a write is refused by the driver."""
    conn = comparison_store._connect_readonly(Path(db))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM comparisons")
    finally:
        conn.close()


# --- Storage observability: an unobservable store is not an empty one ---------


def _schema_objects(path):
    """This database's own catalogue: proof that a read created nothing."""
    with closing(sqlite3.connect(str(path))) as conn:
        return sorted(
            tuple(row)
            for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            )
        )


def _file_identity(path):
    path = Path(path)
    stat = path.stat()
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        stat.st_mtime_ns,
        stat.st_size,
    )


def _all_reliability_reads(db_path, registry_path=None):
    """Every reliability surface, so a storage case is proven on all of them."""
    return (
        lambda: comparison_reliability.summary(
            now=T0, db_path=db_path, registry_path=registry_path
        ),
        lambda: comparison_reliability.issues(
            now=T0, db_path=db_path, registry_path=registry_path
        ),
        lambda: comparison_reliability.failures(now=T0, db_path=db_path),
    )


def test_missing_database_is_refused_and_never_created(tmp_path):
    """Tests 3 and 8: a missing database is not an empty system, and asking
    about it does not bring it into existence."""
    missing = tmp_path / "nope.db"
    for call in _all_reliability_reads(missing):
        with pytest.raises(comparison_reliability.ReliabilityStorageUnavailable) as exc:
            call()
        assert exc.value.code == comparison_reliability.CODE_STORAGE_UNAVAILABLE
        assert exc.value.reason == comparison_store.RELIABILITY_STORAGE_ABSENT
    assert not missing.exists()
    assert list(tmp_path.glob("nope.db*")) == []


def test_unreadable_database_is_refused(tmp_path):
    """Test 4: a file that is not a SQLite database cannot be summarized."""
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is definitely not a sqlite database\n" * 64)
    before = _file_identity(corrupt)
    for call in _all_reliability_reads(corrupt):
        with pytest.raises(comparison_reliability.ReliabilityStorageUnavailable) as exc:
            call()
        assert exc.value.reason == comparison_store.RELIABILITY_STORAGE_UNREADABLE
    # Test 9: the file was not rewritten, and no journal/WAL sidecar appeared.
    assert _file_identity(corrupt) == before
    assert not (tmp_path / "corrupt.db-journal").exists()
    assert not (tmp_path / "corrupt.db-wal").exists()


def test_unopenable_database_is_refused(tmp_path):
    """A path that exists but cannot be opened as a database (a directory)."""
    directory = tmp_path / "directory.db"
    directory.mkdir()
    with pytest.raises(comparison_reliability.ReliabilityStorageUnavailable) as exc:
        comparison_reliability.summary(now=T0, db_path=directory)
    assert exc.value.reason == comparison_store.RELIABILITY_STORAGE_UNREADABLE


@pytest.mark.parametrize("dropped", comparison_store.RELIABILITY_REQUIRED_TABLES)
def test_each_missing_required_table_fails_closed(tmp_path, dropped):
    """Tests 5, 6, 7, 8, 9: dropping any one required table refuses the report,
    names the table, creates nothing, and leaves the file untouched."""
    path = tmp_path / f"without-{dropped}.db"
    comparison_store.init_db(path)
    with closing(sqlite3.connect(str(path))) as conn, conn:
        conn.execute(f"DROP TABLE {dropped}")
    objects_before = _schema_objects(path)
    identity_before = _file_identity(path)
    assert dropped not in {name for _type, name, _sql in objects_before}

    for call in _all_reliability_reads(path):
        with pytest.raises(comparison_reliability.ReliabilityDataError) as exc:
            call()
        assert exc.value.code == comparison_reliability.CODE_DATA_INVALID
        assert exc.value.reasons == [
            comparison_reliability.data_missing_table_reason(dropped)
        ]

    # The missing table was NOT recreated, nothing else was migrated, and the
    # file is byte-identical with an unchanged mtime.
    assert _schema_objects(path) == objects_before
    assert dropped not in {name for _type, name, _sql in _schema_objects(path)}
    assert _file_identity(path) == identity_before


def test_a_legacy_store_predating_detection_attempts_fails_closed(tmp_path):
    """The exact false-clean-signal case: a database with `comparisons` but no
    attempt or replay tables must NOT report zero failures and zero issues."""
    legacy = tmp_path / "legacy.db"
    with closing(sqlite3.connect(str(legacy))) as conn, conn:
        conn.execute(
            "CREATE TABLE comparisons (comparison_id TEXT PRIMARY KEY, "
            "workflow_version TEXT, status TEXT, created_at TEXT, updated_at TEXT, "
            "failure_code TEXT)"
        )
        conn.execute(
            "INSERT INTO comparisons VALUES ('cmp_old', ?, 'detecting', ?, ?, NULL)",
            (WORKFLOW_VERSION, T0.isoformat(), T0.isoformat()),
        )
    objects_before = _schema_objects(legacy)

    with pytest.raises(comparison_reliability.ReliabilityDataError) as exc:
        comparison_reliability.summary(now=T0, db_path=legacy)
    assert exc.value.reasons == [
        comparison_reliability.data_missing_table_reason(
            "comparison_detection_attempts"
        ),
        comparison_reliability.data_missing_table_reason(
            "comparison_detection_job_events"
        ),
        comparison_reliability.data_missing_table_reason(
            "comparison_detection_jobs"
        ),
        comparison_reliability.data_missing_table_reason(
            "comparison_detection_replays"
        ),
    ]
    # No initialization happened behind the refusal.
    assert _schema_objects(legacy) == objects_before


def test_store_snapshot_distinguishes_every_storage_case(tmp_path):
    """The store contract itself: A valid-empty, B missing, C unreadable,
    D table missing — four outcomes, not one empty dict."""
    # A: initialized, zero rows.
    valid = tmp_path / "valid.db"
    comparison_store.init_db(valid)
    snapshot = comparison_store.read_reliability_snapshot(valid)
    assert snapshot == {
        "comparisons": [], "attempts": [], "jobs": [], "job_events": [],
        "replays": [],
        "attempts_per_comparison": {},
    }
    # B: missing.
    with pytest.raises(comparison_store.ReliabilityStorageUnavailable) as exc:
        comparison_store.read_reliability_snapshot(tmp_path / "absent.db")
    assert exc.value.reason == comparison_store.RELIABILITY_STORAGE_ABSENT
    # C: unreadable.
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"nope" * 100)
    with pytest.raises(comparison_store.ReliabilityStorageUnavailable) as exc:
        comparison_store.read_reliability_snapshot(corrupt)
    assert exc.value.reason == comparison_store.RELIABILITY_STORAGE_UNREADABLE
    # D: required table missing.
    incomplete = tmp_path / "incomplete.db"
    comparison_store.init_db(incomplete)
    with closing(sqlite3.connect(str(incomplete))) as conn, conn:
        conn.execute("DROP TABLE comparison_detection_replays")
    with pytest.raises(comparison_store.ReliabilitySchemaIncomplete) as schema_exc:
        comparison_store.read_reliability_snapshot(incomplete)
    assert schema_exc.value.missing_tables == ["comparison_detection_replays"]
    # The two storage failures are distinguishable types, not one broad error.
    assert not issubclass(
        comparison_store.ReliabilitySchemaIncomplete,
        comparison_store.ReliabilityStorageUnavailable,
    )


def test_valid_populated_database_behaviour_is_unchanged(db):
    """Test 16: the schema gate changes nothing for a real database."""
    _mixed(db)
    report = _summary(db)
    assert report["attempts"]["terminal_attempts"] == 4
    assert report["gauges"]["comparisons_detected"] == 2
    assert report["durations"]["duration_count"] == 4
    assert comparison_reliability.failures(now=T0, db_path=db)["total"] == 2
    with closing(sqlite3.connect(str(db))) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_store_initialization_remains_idempotent(tmp_path):
    """No new schema, migration, or index was introduced by this commit."""
    path = tmp_path / "idempotent.db"
    comparison_store.init_db(path)
    first = _schema_fingerprint(path)
    for _ in range(3):
        comparison_store.init_db(path)
    assert _schema_fingerprint(path) == first
    with closing(sqlite3.connect(str(path))) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _schema_fingerprint(path):
    with closing(sqlite3.connect(str(path))) as conn:
        return sorted(
            (row[0], row[1], row[2])
            for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            )
        )


# --- 23-26: API surface -------------------------------------------------------


@pytest.fixture
def api_db(tmp_path, monkeypatch):
    path = tmp_path / "api.db"
    comparison_store.init_db(path)
    # A controlled readable registry: the routes resolve config, and the real
    # registry is gitignored runtime state that a fresh checkout does not have.
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(path))
    monkeypatch.setattr(
        config, "FILING_REGISTRY_PATH", str(_write_registry(tmp_path / "registry.jsonl"))
    )
    return path


def test_api_summary_dto_is_allowlisted(api_db):
    """Test 23."""
    _mixed(api_db)
    response = client.get("/api/comparison-reliability/summary")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "contractVersion", "generatedAt", "since", "until", "detectorVersions",
        "workflowVersions", "recoveryPolicyId", "recoveryPolicyVersion",
        "staleAfterSeconds", "maxAttemptsPerComparison", "gauges", "jobs",
        "jobDurations", "attempts", "attemptRates", "replays", "replayRates",
        "durations", "failureBreakdown", "leasePolicyId", "leasePolicyVersion",
        "leaseDurationSeconds", "heartbeatExtensionSeconds",
        "reclaimGraceSeconds", "maxClaimGenerations",
        "retryPolicyId", "retryPolicyVersion", "maxRetryAttempts",
    }
    assert set(body["gauges"]) == {
        "comparisonsReadyForDetection", "comparisonsQueuedForDetection",
        "comparisonsDetecting", "comparisonsDetected", "comparisonsFailed",
        "comparisonsWaitingForDetectionRetry",
        "runningAttempts", "staleRunningAttempts", "replayEligibleAttempts",
        "attemptLimitExhaustedComparisons", "detectionJobsQueued",
        "detectionJobsRunning", "detectionJobsSucceeded", "detectionJobsFailed",
        "detectionJobsWaitingForRetry", "detectionJobsRetryDue",
        "detectionJobsRetryNotDue", "detectionJobsRetryExhausted",
        "activeJobLeases", "expiredJobLeases", "reclaimableJobs",
        "claimExhaustedJobs",
        "unresolvedOperationalIssues",
    }
    assert set(body["jobs"]) == {
        "jobsQueued", "jobsClaimed", "jobsSucceeded", "jobsFailed",
        "jobHeartbeats", "jobsReclaimed", "jobsClaimExhausted",
        "retriesScheduled", "retriesClaimed", "retriesSucceeded",
        "retriesFailed", "retriesExhausted",
    }
    assert set(body["jobDurations"]) == {
        "queueWaitCount", "queueWaitSecondsMin", "queueWaitSecondsMax",
        "queueWaitSecondsMean", "queueWaitSecondsP50", "queueWaitSecondsP95",
        "executionCount", "executionSecondsMin", "executionSecondsMax",
        "executionSecondsMean", "executionSecondsP50", "executionSecondsP95",
        "negativeQueueWaitJobs", "negativeExecutionJobs",
        "negativeLeaseDurationJobs", "percentileMethod",
    }
    assert set(body["attempts"]) == {
        "attemptsStarted", "attemptsSucceeded", "attemptsFailed", "attemptsTimedOut",
        "attemptsRunningInWindow", "terminalAttempts",
    }
    assert set(body["attemptRates"]) == {"successRate", "failureRate", "timeoutRate"}
    assert set(body["attemptRates"]["successRate"]) == {
        "metric", "value", "numerator", "denominator", "zeroDenominator",
        "zeroDenominatorPolicy",
    }
    assert set(body["replays"]) == {
        "replaysStarted", "replayReplacementsSucceeded", "replayReplacementsFailed",
        "replayReplacementsRunning", "replayReplacementsTimedOut",
        "terminalReplayReplacements",
    }
    assert set(body["durations"]) == {
        "durationCount", "durationSecondsMin", "durationSecondsMax",
        "durationSecondsMean", "durationSecondsP50", "durationSecondsP95",
        "negativeDurationAttempts", "percentileMethod",
    }
    assert set(body["failureBreakdown"]) == {
        "failedAttemptsByCode", "timedOutAttemptsByCode", "failuresByDetectorVersion",
        "failuresByWorkflowVersion",
        "retryableFailuresByCode", "nonRetryableFailuresByCode",
        "retryExhaustionsByOriginalCode",
    }
    assert body["attempts"]["terminalAttempts"] == 4
    assert body["gauges"]["runningAttempts"] == 1
    assert body["contractVersion"] == (
        comparison_reliability.RELIABILITY_CONTRACT_VERSION
    )


def test_api_issue_and_failure_dtos_expose_nothing_sensitive(api_db):
    """Test 24."""
    _mixed(api_db)
    _comparison(api_db, "cmp_stale", "detecting")
    _attempt(api_db, "att_stale", "cmp_stale", 1, "running",
             started=datetime.now(timezone.utc) - timedelta(seconds=5000))

    issues = client.get("/api/comparison-reliability/issues")
    failures = client.get("/api/comparison-reliability/failures")
    assert issues.status_code == 200
    assert failures.status_code == 200

    assert set(issues.json()) == {
        "contractVersion", "generatedAt", "recoveryPolicyId", "recoveryPolicyVersion",
        "leasePolicyId", "leasePolicyVersion", "total", "returned", "truncated",
        "retryPolicyId", "retryPolicyVersion",
        "issues",
    }
    assert issues.json()["total"] >= 1
    for issue in issues.json()["issues"]:
        assert set(issue) == {
            "issueType", "comparisonId", "jobId", "attemptId", "replayId",
            "status", "failureCode", "startedAt", "queuedAt", "claimedAt",
            "claimGeneration", "leaseStartedAt", "heartbeatAt", "leaseExpiresAt",
            "leaseState",
            "createdAt", "detectedAt", "ageSeconds", "staleAt", "attemptsUsed",
            "maxAttempts", "detectorVersion", "workflowVersion",
            "recommendedActionCode",
        }
    assert set(failures.json()) == {
        "contractVersion", "generatedAt", "since", "until", "total", "returned",
        "truncated", "failures",
    }
    for failure in failures.json()["failures"]:
        assert set(failure) == {
            "attemptId", "comparisonId", "attemptNumber", "status", "failureCode",
            "failureSummary", "detectorVersion", "workflowVersion", "startedAt",
            "finishedAt", "durationSeconds", "replayId", "sourceAttemptId",
        }

    combined = (
        issues.text
        + failures.text
        + client.get("/api/comparison-reliability/summary").text
    )
    for forbidden in (
        "/Users", "/private", "/var/folders", ".db", "SELECT", "INSERT", "UPDATE",
        "sqlite", "result_json", "excerpt", "chunk_id", "Traceback",
        "operator_note", "operatorNote", "operatorId", "reviewer", NOTE, OPERATOR,
    ):
        assert forbidden not in combined, forbidden


def test_api_invalid_filters_return_422(api_db):
    """Test 25."""
    naive = client.get(
        "/api/comparison-reliability/summary", params={"since": "2026-07-29T12:00:00"}
    )
    assert naive.status_code == 422
    assert naive.json()["detail"]["code"] == (
        comparison_reliability.CODE_INVALID_TIMESTAMP
    )

    inverted = client.get(
        "/api/comparison-reliability/summary",
        params={
            "since": "2026-07-29T13:00:00+00:00",
            "until": "2026-07-29T12:00:00+00:00",
        },
    )
    assert inverted.status_code == 422
    assert inverted.json()["detail"]["code"] == (
        comparison_reliability.CODE_INVALID_TIME_RANGE
    )

    assert client.get(
        "/api/comparison-reliability/failures", params={"since": "nonsense"}
    ).status_code == 422
    assert client.get(
        "/api/comparison-reliability/issues", params={"issue_type": "nope"}
    ).status_code == 422
    for limit in (0, -3, comparison_reliability.MAX_LIMIT + 1):
        assert client.get(
            "/api/comparison-reliability/issues", params={"limit": limit}
        ).status_code == 422
        assert client.get(
            "/api/comparison-reliability/failures", params={"limit": limit}
        ).status_code == 422


def test_api_storage_fault_returns_a_safe_correlation_id(api_db, monkeypatch, caplog):
    """Test 26."""
    secret = "/private/var/secret-path/comparisons.db"

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError(f"disk I/O error reading {secret}")

    monkeypatch.setattr(comparison_store, "read_reliability_snapshot", boom)
    with caplog.at_level(logging.ERROR, logger="api"):
        response = client.get("/api/comparison-reliability/summary")
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["code"] == "comparison_storage_error"
    assert detail["error_id"].startswith("err_")
    assert secret not in response.text
    assert "sqlite" not in response.text.lower()
    assert detail["error_id"] in caplog.text


def test_api_data_invalid_returns_the_stable_code_and_correlation_id(
    api_db, caplog
):
    """Fail closed through the API, with the offending ids only in the log."""
    _comparison(api_db, "cmp_a", "detecting")
    _attempt(api_db, "att_a", "cmp_a", 1, "running")
    _sql(
        api_db,
        "UPDATE comparison_detection_attempts SET status = 'wedged' "
        "WHERE attempt_id = 'att_a'",
        ignore_checks=True,
    )
    with caplog.at_level(logging.ERROR, logger="api"):
        response = client.get("/api/comparison-reliability/summary")
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["code"] == comparison_reliability.CODE_DATA_INVALID
    assert detail["error_id"].startswith("err_")
    assert "att_a" not in response.text
    assert "att_a" in caplog.text
    assert detail["error_id"] in caplog.text


def test_api_dependency_unavailable_is_sanitized_and_correlated(
    api_db, tmp_path, monkeypatch, caplog
):
    """A safe 500 with a stable code and correlation id — never the registry
    path, its contents, or the raw fault."""
    secret_registry = tmp_path / "secret-registry-location" / "registry.jsonl"
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(secret_registry))
    _stale_running(
        api_db,
        attempt_id="att_stale_api",
        comparison_id="cmp_stale_api",
    )
    _sql(
        api_db,
        "UPDATE comparison_detection_attempts SET started_at = ? "
        "WHERE attempt_id = 'att_stale_api'",
        ((datetime.now(timezone.utc) - timedelta(seconds=5000)).isoformat(),),
    )

    with caplog.at_level(logging.ERROR, logger="api"):
        summary = client.get("/api/comparison-reliability/summary")
        issues = client.get("/api/comparison-reliability/issues")

    for response in (summary, issues):
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail["code"] == comparison_reliability.CODE_DEPENDENCY_UNAVAILABLE
        assert detail["dependency"] == (
            comparison_reliability.DEPENDENCY_FILING_REGISTRY
        )
        assert detail["error_id"].startswith("err_")
        # No zero-valued report leaked out alongside the error.
        assert "gauges" not in response.json()
        for forbidden in (
            "secret-registry-location", str(tmp_path), "/Users", "/private",
            "registry.jsonl", "Traceback", "JSONDecodeError", "FileNotFoundError",
            "SELECT", "sqlite",
        ):
            assert forbidden not in response.text, forbidden
        assert detail["error_id"] in caplog.text

    # The full cause IS available server-side.
    assert comparison_reliability.DEPENDENCY_REGISTRY_ABSENT in caplog.text
    assert "secret-registry-location" in caplog.text

    # The failures listing needs no eligibility, so it still answers.
    failures = client.get("/api/comparison-reliability/failures")
    assert failures.status_code == 200


def test_api_missing_database_is_sanitized_and_correlated(
    api_db, tmp_path, monkeypatch, caplog
):
    """Test 10: a missing database is a safe correlated 500 with no counters."""
    absent = tmp_path / "secret-storage-dir" / "comparisons.db"
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(absent))

    with caplog.at_level(logging.ERROR, logger="api"):
        responses = [
            client.get(f"/api/comparison-reliability/{path}")
            for path in ("summary", "issues", "failures")
        ]

    for response in responses:
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail["code"] == comparison_reliability.CODE_STORAGE_UNAVAILABLE
        assert detail["error_id"].startswith("err_")
        assert set(detail) == {"code", "message", "error_id"}
        _assert_no_storage_leak(response.text)
        assert detail["error_id"] in caplog.text

    # Full diagnostics server-side only.
    assert comparison_store.RELIABILITY_STORAGE_ABSENT in caplog.text
    assert "secret-storage-dir" in caplog.text
    assert not absent.exists()


def test_api_missing_table_is_sanitized_and_correlated(
    api_db, tmp_path, monkeypatch, caplog
):
    """Test 11: a structurally incomplete schema is a safe correlated 500."""
    broken = tmp_path / "secret-storage-dir" / "broken.db"
    broken.parent.mkdir(parents=True, exist_ok=True)
    comparison_store.init_db(broken)
    with closing(sqlite3.connect(str(broken))) as conn, conn:
        conn.execute("DROP TABLE comparison_detection_attempts")
    monkeypatch.setattr(config, "COMPARISON_DB_PATH", str(broken))
    objects_before = _schema_objects(broken)

    with caplog.at_level(logging.ERROR, logger="api"):
        responses = [
            client.get(f"/api/comparison-reliability/{path}")
            for path in ("summary", "issues", "failures")
        ]

    for response in responses:
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail["code"] == comparison_reliability.CODE_DATA_INVALID
        assert detail["error_id"].startswith("err_")
        _assert_no_storage_leak(response.text)
        # The absent table name is not disclosed to the client either.
        assert "comparison_detection_attempts" not in response.text

    assert "comparison_detection_attempts" in caplog.text
    # Serving the errors did not recreate the table.
    assert _schema_objects(broken) == objects_before


def _assert_no_storage_leak(text):
    """No path, SQL, SQLite error, schema DDL, or raw exception (test 15)."""
    for forbidden in (
        "/Users", "/private", "/var/folders", "secret-storage-dir", ".db",
        "SELECT", "INSERT", "UPDATE", "CREATE TABLE", "DROP TABLE", "sqlite",
        "sqlite3", "DatabaseError", "OperationalError", "Traceback",
        "no such table", "not a database", "unable to open",
        "gauges", "attemptsStarted", "unresolvedOperationalIssues",
    ):
        assert forbidden not in text, forbidden


def test_api_empty_initialized_database_reports_an_empty_system(api_db):
    """Test 12 (API half): the valid empty case still answers 200 with zeros."""
    summary = client.get("/api/comparison-reliability/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["gauges"]["comparisonsDetecting"] == 0
    assert body["gauges"]["unresolvedOperationalIssues"] == 0
    assert body["attempts"]["terminalAttempts"] == 0
    assert body["attemptRates"]["successRate"]["value"] is None
    assert body["attemptRates"]["successRate"]["zeroDenominator"] is True
    assert client.get("/api/comparison-reliability/issues").json()["total"] == 0
    assert client.get("/api/comparison-reliability/failures").json()["total"] == 0


def test_api_reads_do_not_mutate_anything(api_db):
    """Every reliability route is a pure read."""
    _mixed(api_db)
    before = hashlib.sha256(Path(api_db).read_bytes()).hexdigest()
    for path in (
        "/api/comparison-reliability/summary",
        "/api/comparison-reliability/issues",
        "/api/comparison-reliability/failures",
    ):
        assert client.get(path).status_code == 200
    assert hashlib.sha256(Path(api_db).read_bytes()).hexdigest() == before


def test_reliability_routes_are_get_only():
    """Test 38 (API half): no mutation route was added."""
    for route in api.app.routes:
        path = getattr(route, "path", "")
        if "comparison-reliability" in path:
            assert set(route.methods) <= {"GET", "HEAD"}, path


# --- 27-30: operator CLI ------------------------------------------------------


def _cli(argv):
    from scripts import comparison_reliability_report as cli

    return cli.main(argv)


def test_cli_human_mode_is_deterministic_and_path_free(db, capsys):
    """Test 27."""
    _mixed(db)
    assert _cli(["--db-path", str(db), "--issues", "--failures"]) == 0
    first = capsys.readouterr().out
    assert _cli(["--db-path", str(db), "--issues", "--failures"]) == 0
    second = capsys.readouterr().out

    # Byte-identical apart from the two clock-derived values a snapshot must
    # carry: the instant it was generated, and ages measured from that instant.
    def _normalize(text):
        import re

        text = re.sub(r"generated=\S+", "generated=<t>", text)
        return re.sub(r"age=\S+", "age=<t>", text)

    assert _normalize(first) == _normalize(second)

    for forbidden in (
        str(db), str(db.parent), "/Users", "/private", "/var/folders", "SELECT",
        "INSERT", "sqlite", "Traceback", "result_json", "excerpt", NOTE, OPERATOR,
    ):
        assert forbidden not in first, forbidden
    # The substance is present.
    assert "terminal (rate denominator)            4" in first
    assert "success_rate" in first
    assert "replay_success_rate" in first
    assert "percentile=nearest_rank" in first
    assert "unresolved operational issues" in first


def test_cli_json_matches_the_service_contract(db, capsys):
    """Test 28."""
    _mixed(db)
    assert _cli(["--db-path", str(db), "--json", "--issues", "--failures"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"summary", "issues", "failures"}

    # The CLI resolves its own wall-clock instant, so the comparison uses one
    # too. Every field asserted below is a count or a rate — stable regardless
    # of which instant inside this test the two reports were taken at.
    expected = comparison_reliability.summary(
        db_path=db, now=datetime.now(timezone.utc)
    )
    assert payload["summary"]["gauges"] == expected["gauges"]
    assert payload["summary"]["attempts"] == expected["attempts"]
    assert payload["summary"]["attempt_rates"] == expected["attempt_rates"]
    assert payload["summary"]["replays"] == expected["replays"]
    assert payload["summary"]["failure_breakdown"] == expected["failure_breakdown"]
    # JSON objects are unordered (the CLI emits sort_keys=True), so the
    # contract asserted here is the field SET; the ordered tuple is pinned
    # against the service return value elsewhere.
    for issue in payload["issues"]["issues"]:
        assert set(issue) == set(comparison_reliability.ISSUE_FIELDS)
    for failure in payload["failures"]["failures"]:
        assert set(failure) == set(comparison_reliability.FAILURE_FIELDS)


def test_cli_exits_zero_when_issues_exist(db, capsys):
    """Test 5(F): reporting state is not a gate."""
    _comparison(db, "cmp_f", "failed", updated=T0, failure_code="detector_internal_error")
    _attempt(db, "att_f", "cmp_f", 1, "failed", finished=T0 + timedelta(seconds=2),
             failure_code="detector_internal_error")
    assert _cli(["--db-path", str(db), "--issues"]) == 0
    assert "comparison_failed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--since", "2026-07-29T12:00:00"],                       # naive
        ["--since", "2026-07-29T13:00:00+00:00",
         "--until", "2026-07-29T12:00:00+00:00"],                  # inverted
        ["--limit", "0"],
        ["--limit", str(comparison_reliability.MAX_LIMIT + 1)],
    ],
)
def test_cli_invalid_arguments_exit_2(db, argv, capsys):
    """Test 29."""
    assert _cli(["--db-path", str(db), *argv]) == 2
    assert "Invalid argument" in capsys.readouterr().err


def test_cli_unknown_database_exits_2_without_creating_it(tmp_path, capsys):
    """Test 12: a missing --db-path keeps the documented argument/configuration
    contract (exit 2) and creates nothing."""
    missing = tmp_path / "not-there.db"
    assert _cli(["--db-path", str(missing)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not-there.db" in captured.err
    assert str(missing.parent) not in captured.err  # never the absolute path
    assert not missing.exists()
    assert list(tmp_path.glob("not-there.db*")) == []


def test_cli_empty_initialized_database_reports_an_empty_system(tmp_path, capsys):
    """Test 11 (empty half): a valid empty system is exit 0, not a refusal."""
    path = tmp_path / "empty.db"
    comparison_store.init_db(path)
    assert _cli(["--db-path", str(path), "--json", "--issues", "--failures"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["gauges"]["unresolved_operational_issues"] == 0
    assert payload["summary"]["attempts"]["terminal_attempts"] == 0
    assert payload["summary"]["attempt_rates"]["success_rate"]["value"] is None
    assert payload["issues"]["issues"] == []
    assert payload["failures"]["failures"] == []


@pytest.mark.parametrize("case", ["unreadable", "missing_table"])
def test_cli_unobservable_database_exits_1_with_no_partial_report(
    tmp_path, capsys, case
):
    """Tests 13, 14, 15: exit 1, empty stdout, safe stderr."""
    path = tmp_path / "secret-storage-dir" / "storage.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    if case == "unreadable":
        path.write_bytes(b"not a sqlite database at all\n" * 32)
        expected_code = comparison_reliability.CODE_STORAGE_UNAVAILABLE
    else:
        comparison_store.init_db(path)
        with closing(sqlite3.connect(str(path))) as conn, conn:
            conn.execute("DROP TABLE comparison_detection_replays")
        expected_code = comparison_reliability.CODE_DATA_INVALID
    identity_before = _file_identity(path)

    for argv in (
        ["--db-path", str(path)],
        ["--db-path", str(path), "--json"],
        ["--db-path", str(path), "--issues", "--failures"],
    ):
        assert _cli(argv) == 1, argv
        captured = capsys.readouterr()
        assert captured.out == "", captured.out
        assert expected_code in captured.err
        for forbidden in (
            str(path), str(tmp_path), "secret-storage-dir", "/Users", "/private",
            "SELECT", "CREATE TABLE", "DROP TABLE", "sqlite3", "DatabaseError",
            "OperationalError", "Traceback", "no such table", "not a database",
        ):
            assert forbidden not in captured.err, forbidden

    # The refusals neither rewrote nor initialized the file.
    assert _file_identity(path) == identity_before


def test_cli_bad_flag_exits_2(db):
    with pytest.raises(SystemExit) as excinfo:
        _cli(["--db-path", str(db), "--nonsense"])
    assert excinfo.value.code == 2


def test_cli_data_invalid_exits_1_with_codes_only(db, capsys):
    _comparison(db, "cmp_a", "detecting")
    _attempt(db, "att_a", "cmp_a", 1, "running")
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET status = 'wedged' "
        "WHERE attempt_id = 'att_a'",
        ignore_checks=True,
    )
    assert _cli(["--db-path", str(db)]) == 1
    error = capsys.readouterr().err
    assert comparison_reliability.CODE_DATA_INVALID in error
    assert comparison_reliability.DATA_UNKNOWN_ATTEMPT_STATUS in error
    for forbidden in (str(db), "SELECT", "sqlite", "Traceback"):
        assert forbidden not in error


@pytest.mark.parametrize(
    "registry_kind,expected_reason",
    [
        ("absent", comparison_reliability.DEPENDENCY_REGISTRY_ABSENT),
        ("malformed", comparison_reliability.DEPENDENCY_REGISTRY_UNREADABLE),
        ("empty", comparison_reliability.DEPENDENCY_REGISTRY_EMPTY),
    ],
)
def test_cli_dependency_unavailable_exits_1_with_no_partial_report(
    db, tmp_path, monkeypatch, capsys, registry_kind, expected_reason
):
    """Exit 1, a concise safe message, and NOTHING partial on stdout."""
    secret = tmp_path / "private-registry-dir" / "registry.jsonl"
    if registry_kind == "malformed":
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("{not json\n", encoding="utf-8")
    elif registry_kind == "empty":
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "FILING_REGISTRY_PATH", str(secret))

    _stale_running(db)
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET started_at = ? "
        "WHERE attempt_id = 'att_run'",
        ((datetime.now(timezone.utc) - timedelta(seconds=5000)).isoformat(),),
    )

    for argv in (
        ["--db-path", str(db)],
        ["--db-path", str(db), "--json"],
        ["--db-path", str(db), "--issues", "--failures"],
    ):
        assert _cli(argv) == 1, argv
        captured = capsys.readouterr()
        # No partial report, and in particular no JSON document.
        assert captured.out == "", captured.out
        assert comparison_reliability.CODE_DEPENDENCY_UNAVAILABLE in captured.err
        assert expected_reason in captured.err
        assert comparison_reliability.DEPENDENCY_FILING_REGISTRY in captured.err
        for forbidden in (
            str(secret), str(tmp_path), "private-registry-dir", "/Users", "/private",
            "Traceback", "JSONDecodeError", "FileNotFoundError", "SELECT", "sqlite",
        ):
            assert forbidden not in captured.err, forbidden


def test_cli_still_reports_when_nothing_needs_eligibility(db, tmp_path, monkeypatch):
    """The same absent registry is harmless when no attempt is stale."""
    monkeypatch.setattr(
        config, "FILING_REGISTRY_PATH", str(tmp_path / "absent" / "registry.jsonl")
    )
    _comparison(db, "cmp_ok", "detected", updated=T0 + timedelta(seconds=4))
    _attempt(db, "att_ok", "cmp_ok", 1, "succeeded",
             finished=T0 + timedelta(seconds=4), result_hash="h1")
    assert _cli(["--db-path", str(db), "--json"]) == 0


def test_cli_performs_no_writes(db, capsys):
    """Test 30. The database file is byte-identical afterwards."""
    _mixed(db)
    before = hashlib.sha256(Path(db).read_bytes()).hexdigest()
    stat_before = Path(db).stat().st_mtime_ns
    assert _cli(["--db-path", str(db), "--issues", "--failures"]) == 0
    assert _cli(["--db-path", str(db), "--json"]) == 0
    capsys.readouterr()
    assert hashlib.sha256(Path(db).read_bytes()).hexdigest() == before
    assert Path(db).stat().st_mtime_ns == stat_before
    # No journal or WAL sidecar was left behind either.
    assert not (Path(str(db) + "-journal")).exists()
    assert not (Path(str(db) + "-wal")).exists()


# --- 31-35: structured lifecycle logging -------------------------------------


class _FakeEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[float(len(text) % 7), 1.0] for text in texts]

    def embed_query(self, text):
        raise AssertionError("vector retrieval must not be used by the detector")


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    from langchain_chroma import Chroma

    directory = tmp_path_factory.mktemp("reliability-corpus")
    registry = directory / "registry.jsonl"
    docs = ingest.load_documents(
        config.DOCS_DIR, manifest=filing_registry.load_manifest(),
        registry_path=registry,
    )
    chunks = ingest.split_documents(docs)
    unique, ids = ingest._dedupe_by_id(chunks)
    counts = {}
    for chunk in unique:
        relative = chunk.metadata.get("source_path")
        counts[relative] = counts.get(relative, 0) + 1
    filing_registry.update_chunk_counts(counts, registry)
    chroma = Chroma(
        collection_name="reliabilityidx",
        persist_directory=str(directory / "chroma"),
        embedding_function=_FakeEmbeddings(),
    )
    chroma.add_documents(documents=unique, ids=ids)
    return SimpleNamespace(registry=registry, chroma=chroma)


def _records(caplog, event=None):
    """LogRecords, not formatted strings — the fields are the contract."""
    return [
        record
        for record in caplog.records
        if getattr(record, "event", None) is not None
        and (event is None or record.event == event)
    ]


def _detect(corpus, db):
    record, _ = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry
    )
    return comparison_detector.detect_with_attempt(
        record["comparison_id"],
        db_path=db,
        registry_path=corpus.registry,
        chroma_client=corpus.chroma,
    )


def test_structured_start_and_success_logs(corpus, db, caplog):
    """Tests 31, 32."""
    with caplog.at_level(logging.INFO, logger="comparison_reliability"):
        result, created, attempt_id = _detect(corpus, db)
    assert created is True

    started = _records(caplog, comparison_reliability.EVENT_ATTEMPT_STARTED)
    succeeded = _records(caplog, comparison_reliability.EVENT_ATTEMPT_SUCCEEDED)
    assert len(started) == 1 and len(succeeded) == 1

    for record in (started[0], succeeded[0]):
        for field in comparison_reliability.LOG_FIELDS:
            assert hasattr(record, field), field
        assert record.attempt_id == attempt_id
        assert record.comparison_id == result["comparison_id"]
        assert record.detector_version == DETECTOR_VERSION
        assert record.workflow_version == WORKFLOW_VERSION
        assert record.attempt_number == 1
        assert record.replay_id is None
        assert record.source_attempt_id is None

    assert started[0].status == "running"
    assert started[0].result_hash is None
    assert started[0].elapsed_ms is None
    assert succeeded[0].status == "succeeded"
    assert succeeded[0].result_hash
    assert succeeded[0].failure_code is None
    assert isinstance(succeeded[0].elapsed_ms, int)
    assert succeeded[0].elapsed_ms >= 0
    # The stored attempt agrees with what was logged.
    stored = comparison_store.get_detection_attempt(attempt_id, db_path=db)
    assert stored["result_hash"] == succeeded[0].result_hash


def test_structured_failure_log_carries_the_code_but_no_exception_text(
    corpus, db, caplog, monkeypatch
):
    """Test 33."""
    secret = "boom in /private/var/secret/detector.py line 42"
    record, _ = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry
    )

    def explode(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(comparison_detector, "load_section", explode)
    with caplog.at_level(logging.INFO, logger="comparison_reliability"):
        with pytest.raises(comparison_detector.DetectionInternalError):
            comparison_detector.detect_with_attempt(
                record["comparison_id"],
                db_path=db,
                registry_path=corpus.registry,
                chroma_client=corpus.chroma,
            )

    failed = _records(caplog, comparison_reliability.EVENT_ATTEMPT_FAILED)
    assert len(failed) == 1
    assert failed[0].status == "failed"
    assert failed[0].failure_code == comparison_detector.REASON_DETECTOR_INTERNAL_ERROR
    assert failed[0].result_hash is None
    assert isinstance(failed[0].elapsed_ms, int)
    # The raw fault never reaches a structured field or the rendered message.
    assert secret not in failed[0].getMessage()
    for field in comparison_reliability.LOG_FIELDS:
        assert secret != getattr(failed[0], field)
    assert not hasattr(failed[0], "operator_note")


def test_structured_replay_logs_carry_replay_and_attempt_ids(corpus, db, caplog):
    """Test 34."""
    record, _ = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry
    )
    comparison_id = record["comparison_id"]
    previous = filing_registry.get_filing(PREV_ID, corpus.registry)["source_hash"]
    current = filing_registry.get_filing(CURR_ID, corpus.registry)["source_hash"]
    attempt = comparison_store.start_detection_attempt(
        comparison_id,
        detector_version=DETECTOR_VERSION,
        workflow_version=WORKFLOW_VERSION,
        previous_source_hash=previous,
        current_source_hash=current,
        db_path=db,
    )
    source_id = attempt["attempt_id"]
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET started_at = ? WHERE attempt_id = ?",
        ((datetime.now(timezone.utc) - timedelta(seconds=5000)).isoformat(), source_id),
    )

    with caplog.at_level(logging.INFO, logger="comparison_reliability"):
        outcome, created = detection_recovery.replay_attempt(
            source_id,
            operator_id=OPERATOR,
            reason_code=REASON,
            operator_note=NOTE,
            db_path=db,
            registry_path=corpus.registry,
            chroma_client=corpus.chroma,
        )
    assert created is True
    replay_id = outcome["replay"]["replay_id"]
    replacement_id = outcome["replacement_attempt_id"]

    timed_out = _records(caplog, comparison_reliability.EVENT_ATTEMPT_TIMED_OUT)
    assert len(timed_out) == 1
    assert timed_out[0].attempt_id == source_id
    assert timed_out[0].status == "timed_out"
    assert timed_out[0].failure_code == comparison_store.FAILURE_ATTEMPT_TIMED_OUT
    assert timed_out[0].replay_id == replay_id

    started = _records(caplog, comparison_reliability.EVENT_ATTEMPT_STARTED)
    assert [record.attempt_id for record in started] == [replacement_id]
    assert started[0].replay_id == replay_id
    assert started[0].source_attempt_id == source_id

    created_records = _records(caplog, comparison_reliability.EVENT_REPLAY_CREATED)
    assert len(created_records) == 1
    assert created_records[0].replay_id == replay_id
    assert created_records[0].source_attempt_id == source_id
    assert created_records[0].attempt_id == replacement_id

    completed = _records(caplog, comparison_reliability.EVENT_REPLAY_COMPLETED)
    assert len(completed) == 1
    assert completed[0].replay_id == replay_id
    assert completed[0].source_attempt_id == source_id
    assert completed[0].attempt_id == replacement_id
    assert completed[0].status == "succeeded"
    assert isinstance(completed[0].elapsed_ms, int)

    # The replacement's own success record is emitted by the shared seam.
    assert [
        record.attempt_id
        for record in _records(caplog, comparison_reliability.EVENT_ATTEMPT_SUCCEEDED)
    ] == [replacement_id]

    # Operator prose never appears in any structured field or message.
    for record in _records(caplog):
        assert NOTE not in record.getMessage()
        for field in comparison_reliability.LOG_FIELDS:
            assert getattr(record, field) != NOTE
            assert getattr(record, field) != OPERATOR


def test_idempotent_replay_logs_no_new_transition(corpus, db, caplog):
    """A repeat of an applied request changed nothing, so it claims nothing."""
    record, _ = comparison_store.create_comparison(
        PREV_ID, CURR_ID, db_path=db, registry_path=corpus.registry
    )
    previous = filing_registry.get_filing(PREV_ID, corpus.registry)["source_hash"]
    current = filing_registry.get_filing(CURR_ID, corpus.registry)["source_hash"]
    attempt = comparison_store.start_detection_attempt(
        record["comparison_id"],
        detector_version=DETECTOR_VERSION,
        workflow_version=WORKFLOW_VERSION,
        previous_source_hash=previous,
        current_source_hash=current,
        db_path=db,
    )
    _sql(
        db,
        "UPDATE comparison_detection_attempts SET started_at = ? WHERE attempt_id = ?",
        (
            (datetime.now(timezone.utc) - timedelta(seconds=5000)).isoformat(),
            attempt["attempt_id"],
        ),
    )
    kwargs = dict(
        operator_id=OPERATOR, reason_code=REASON, operator_note=NOTE,
        db_path=db, registry_path=corpus.registry, chroma_client=corpus.chroma,
    )
    detection_recovery.replay_attempt(attempt["attempt_id"], **kwargs)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="comparison_reliability"):
        _outcome, created = detection_recovery.replay_attempt(
            attempt["attempt_id"], **kwargs
        )
    assert created is False
    assert _records(caplog) == []


def test_logging_failure_does_not_roll_back_workflow_state(corpus, db, monkeypatch):
    """Test 35. Observability may not break the thing it observes."""
    calls = {"count": 0}

    def explode(*args, **kwargs):
        calls["count"] += 1
        raise RuntimeError("logging subsystem is down")

    monkeypatch.setattr(comparison_reliability.logger, "info", explode)
    result, created, attempt_id = _detect(corpus, db)

    assert calls["count"] >= 2  # start and success both tried to log
    assert created is True
    stored = comparison_store.get_detection_attempt(attempt_id, db_path=db)
    assert stored["status"] == comparison_store.ATTEMPT_SUCCEEDED
    assert comparison_store.get_result(
        result["comparison_id"], db_path=db
    )["result_hash"] == stored["result_hash"]
    assert comparison_store.get_comparison(
        result["comparison_id"], db_path=db
    )["status"] == comparison_store.STATUS_DETECTED


def test_reliability_reads_agree_with_a_real_detection_run(corpus, db):
    """The report describes real runs, not just synthesized rows."""
    result, _created, attempt_id = _detect(corpus, db)
    now = datetime.now(timezone.utc)
    report = comparison_reliability.summary(now=now, db_path=db,
                                            registry_path=corpus.registry)
    assert report["gauges"]["comparisons_detected"] == 1
    assert report["gauges"]["running_attempts"] == 0
    assert report["attempts"]["attempts_succeeded"] == 1
    assert report["attempt_rates"]["success_rate"]["value"] == 1.0
    assert report["durations"]["duration_count"] == 1
    assert report["durations"]["duration_seconds_min"] >= 0
    assert report["detector_versions"] == [DETECTOR_VERSION]
    assert report["gauges"]["unresolved_operational_issues"] == 0
    assert comparison_reliability.failures(now=now, db_path=db)["total"] == 0
    assert attempt_id


# --- 38: static audits --------------------------------------------------------


_NEW_MODULES = (
    REPO_ROOT / "comparison_reliability.py",
    REPO_ROOT / "scripts" / "comparison_reliability_report.py",
)

# Nothing that would make this commit more than read-only visibility.
_FORBIDDEN_IMPORTS = {
    "boto3", "botocore", "requests", "httpx", "urllib", "urllib3", "socket",
    "http", "aiohttp", "sched", "schedule", "apscheduler", "celery", "kombu",
    "smtplib", "email", "prometheus_client", "opentelemetry", "statsd",
    "watchtower", "threading", "multiprocessing", "concurrent", "subprocess",
    "asyncio", "signal", "langchain", "langchain_aws", "langchain_chroma",
    "chromadb", "agent", "tools",
}


def _imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_no_scheduler_worker_network_or_monitoring_dependency_is_added():
    """Test 38 (import half)."""
    for path in _NEW_MODULES:
        offenders = _imported_modules(path) & _FORBIDDEN_IMPORTS
        assert offenders == set(), f"{path.name}: {sorted(offenders)}"


def test_reliability_service_only_calls_read_only_store_functions():
    """The service cannot mutate the store even by mistake: every
    comparison_store attribute it touches is on this read-only allowlist."""
    allowed = {
        # data access (read-only): the mode=ro snapshot, two pure helpers, and
        # registry-JSONL pair validation. None of these opens a writable
        # SQLite handle and none calls init_db.
        "read_reliability_snapshot", "parse_utc_timestamp", "evaluate_staleness",
        "validate_pair",
        # vocabulary constants
        "ATTEMPT_STATUSES", "ATTEMPT_RUNNING", "ATTEMPT_SUCCEEDED",
        "ATTEMPT_FAILED", "ATTEMPT_TIMED_OUT", "STATUS_READY_FOR_DETECTION",
            "STATUS_QUEUED_FOR_DETECTION", "STATUS_DETECTING", "STATUS_DETECTED",
            "STATUS_WAITING_FOR_DETECTION_RETRY",
        "STATUS_FAILED", "JOB_STATUSES", "JOB_QUEUED", "JOB_RUNNING",
            "JOB_SUCCEEDED", "JOB_FAILED",
            "JOB_RETRY_WAIT",
        "EVENT_JOB_QUEUED", "EVENT_JOB_CLAIMED", "EVENT_JOB_SUCCEEDED",
        "EVENT_JOB_FAILED", "EVENT_JOB_HEARTBEAT", "EVENT_JOB_RECLAIMED",
            "EVENT_JOB_CLAIM_EXHAUSTED", "JOB_EVENT_TYPES",
            "EVENT_JOB_RETRY_SCHEDULED", "EVENT_JOB_RETRY_CLAIMED",
            "EVENT_JOB_RETRY_EXHAUSTED",
            "REASON_JOB_CLAIMS_EXHAUSTED",
            "REASON_JOB_RETRIES_EXHAUSTED",
            "REASON_JOB_EXECUTION_BUDGET_EXHAUSTED",
        # exception types only: one classifies a registry answer, two carry the
        # store's read-path storage/schema verdicts. None of them writes.
        "ComparisonPairError",
        "ReliabilityStorageUnavailable",
        "ReliabilitySchemaIncomplete",
    }
    tree = ast.parse(
        (REPO_ROOT / "comparison_reliability.py").read_text(encoding="utf-8")
    )
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "comparison_store"
    }
    assert used <= allowed, sorted(used - allowed)


def test_no_sleep_timer_or_retry_loop_is_introduced():
    """No polling loop, no backoff, no timer-driven anything."""
    for path in _NEW_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("sleep", "Timer", "Thread", "Process"), (
                    f"{path.name}:{node.lineno}"
                )
            if isinstance(node, ast.While):
                raise AssertionError(f"{path.name}:{node.lineno} while-loop")


def test_no_frontend_file_references_the_reliability_surface():
    """No dashboard was added: the frontend does not know these routes exist."""
    frontend = REPO_ROOT / "frontend" / "src"
    if not frontend.exists():  # pragma: no cover - frontend always present here
        pytest.skip("no frontend directory")
    for path in frontend.rglob("*.ts*"):
        assert "comparison-reliability" not in path.read_text(encoding="utf-8"), path


def test_log_and_issue_vocabularies_are_closed():
    """The allowlists are the contract, and they contain no operator prose."""
    assert set(comparison_reliability.LOG_EVENTS) == {
        "detection_attempt_started", "detection_attempt_succeeded",
        "detection_attempt_failed", "detection_attempt_timed_out",
        "detection_replay_created", "detection_replay_completed",
    }
    assert set(comparison_reliability.JOB_LOG_EVENTS) == {
        "detection_job_queued", "detection_job_claimed",
        "detection_job_heartbeat", "detection_job_reclaimed",
        "detection_job_claim_exhausted", "detection_job_finalize_rejected",
        "detection_job_succeeded", "detection_job_failed",
        "detection_job_retry_scheduled", "detection_job_retry_claimed",
        "detection_job_retry_exhausted",
    }
    for forbidden in ("operator_id", "operator_note", "reviewer_id", "reviewer_note",
                      "reason_code", "excerpt", "evidence", "result_json"):
        assert forbidden not in comparison_reliability.LOG_FIELDS
        assert forbidden not in comparison_reliability.ISSUE_FIELDS
        assert forbidden not in comparison_reliability.FAILURE_FIELDS
    assert set(comparison_reliability.ISSUE_TYPES) == set(
        comparison_reliability._ISSUE_SEVERITY
    )
    # Every issue type maps to exactly one severity rank.
    assert len(set(comparison_reliability._ISSUE_SEVERITY.values())) == len(
        comparison_reliability.ISSUE_TYPES
    )


# --- The required CI check actually runs these suites -------------------------

_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"

# The merge-blocking check must execute the Stage 3.5 reliability suites and the
# two offline API suites. A required check that does not run a suite cannot block
# a regression in it, which is exactly what this test exists to prevent.
_REQUIRED_CI_SUITES = (
    # Stage 3.5 reliability
    "tests/test_comparison_detection_jobs.py",
    "tests/test_comparison_detection_job_leases.py",
    "tests/test_comparison_detection_job_retries.py",
    "tests/test_comparison_detection_attempts.py",
    "tests/test_detection_recovery.py",
    "tests/test_comparison_reliability.py",
    # Stage 3.5 local authentication and permission authorization
    "tests/test_access_control.py",
    # Stage 3.5 process-level fault injection and restart/recovery validation
    "tests/test_runtime_fault_injection.py",
    # Stage 3.5 bounded Chroma upserts: ingestion is upstream of every
    # comparison, so an over-limit write or a premature completion marker
    # would surface here as a partially indexed section.
    "tests/test_chroma_batching.py",
    # Stage 3.5 real-filing benchmark INFRASTRUCTURE (offline; the required
    # check never acquires a filing and never contacts SEC EDGAR).
    "tests/test_real_filing_benchmark_schema.py",
    "tests/test_real_filing_benchmark_tools.py",
    "tests/test_real_filing_benchmark_evaluator.py",
    # Stage 3.5 holdout human-annotation admission contract: the validator
    # that decides whether human-completed files may enter the gold corpus.
    "tests/test_holdout_human_annotation_validation.py",
    # offline API
    "tests/test_api.py",
    "tests/test_api_errors.py",
    # pre-existing comparison suites, none of which may be dropped
    "tests/test_comparison_regression.py",
    "tests/test_comparison_detector.py",
    "tests/test_comparison_validators.py",
    "tests/test_comparison_schema.py",
    "tests/test_comparison_store.py",
    "tests/test_comparison_governance.py",
    "tests/test_comparison_review.py",
    "tests/test_comparison_export.py",
)


def _workflow():
    import yaml

    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _pytest_step_commands(workflow):
    """Every `run:` script in the job that invokes pytest."""
    job = workflow["jobs"]["comparison-regression"]
    return [
        step["run"]
        for step in job["steps"]
        if isinstance(step.get("run"), str) and "pytest" in step["run"]
    ]


def test_required_check_runs_the_stage35_and_offline_api_suites():
    """Issue-1 guard: parse the workflow and prove the required command covers
    every suite it must, including this file."""
    workflow = _workflow()
    commands = _pytest_step_commands(workflow)
    assert commands, "the required check runs no pytest command"
    combined = "\n".join(commands)
    missing = [suite for suite in _REQUIRED_CI_SUITES if suite not in combined]
    assert missing == [], missing
    # Each named suite exists on disk, so a rename cannot silently stop gating.
    for suite in _REQUIRED_CI_SUITES:
        assert (REPO_ROOT / suite).is_file(), suite


def test_required_check_identity_is_unchanged():
    """The branch-protection contract: workflow name, job id, job name, and
    triggers must all stay exactly `comparison-regression`."""
    workflow = _workflow()
    assert workflow["name"] == "comparison-regression"
    assert list(workflow["jobs"]) == ["comparison-regression"]
    job = workflow["jobs"]["comparison-regression"]
    assert job["name"] == "comparison-regression"
    # PyYAML parses the bare `on:` key as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]
    # No path filters on any trigger.
    for value in triggers.values():
        if isinstance(value, dict):
            assert "paths" not in value
            assert "paths-ignore" not in value


def test_required_check_stays_credential_free_and_keeps_its_artifact():
    """No secrets, no AWS credential configuration, and the regression CLI plus
    its metric artifact still run."""
    raw = _WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in raw
    assert "aws-actions/" not in raw
    for forbidden in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "configure-aws-credentials",
    ):
        assert forbidden not in raw, forbidden

    job = _workflow()["jobs"]["comparison-regression"]
    runs = [step.get("run", "") for step in job["steps"]]
    assert any(
        "scripts/eval_comparison_regression.py" in run
        and "--report comparison-regression-report.json" in run
        for run in runs
    )
    uploads = [
        step for step in job["steps"] if "upload-artifact" in str(step.get("uses", ""))
    ]
    assert len(uploads) == 1
    assert uploads[0]["with"]["path"] == "comparison-regression-report.json"


def test_cli_module_requires_no_credentials_or_network(monkeypatch, db, capsys):
    """Credential-free by construction: the CLI runs with AWS env stripped."""
    for name in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_PROFILE", "AWS_REGION", "TAVILY_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    _mixed(db)
    assert _cli(["--db-path", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["attempts"][
        "terminal_attempts"
    ] == 4


def test_cli_is_runnable_as_a_script(db, tmp_path):
    """The documented invocation actually works from the repo root.

    A subprocess cannot see monkeypatched module state, so the controlled
    registry is passed through the documented FILING_REGISTRY_PATH environment
    variable — which also proves the CLI honours it.
    """
    import os
    import subprocess

    _mixed(db)
    env = {**os.environ, "FILING_REGISTRY_PATH": str(_registry_for(db))}
    completed = subprocess.run(
        [sys.executable, "scripts/comparison_reliability_report.py",
         "--db-path", str(db), "--json"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["summary"]["contract_version"] == (
        comparison_reliability.RELIABILITY_CONTRACT_VERSION
    )

    # Without it, the same state fails closed rather than printing a zero.
    refused = subprocess.run(
        [sys.executable, "scripts/comparison_reliability_report.py",
         "--db-path", str(db), "--json"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        env={**os.environ, "FILING_REGISTRY_PATH": str(tmp_path / "gone.jsonl")},
    )
    assert refused.returncode == 1
    assert refused.stdout == ""
    assert comparison_reliability.CODE_DEPENDENCY_UNAVAILABLE in refused.stderr
