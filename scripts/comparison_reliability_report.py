"""Credential-free operator reliability report for the comparison workflow.

Reads the same service layer the API routes use, so the terminal report and
`GET /api/comparison-reliability/*` can never disagree.

READ-ONLY. This command never starts, retires, heartbeats, reclaims, retries,
or replays a detection attempt, never repairs a comparison, and never writes to the database — the
service opens SQLite with `mode=ro`, so a write is refused by the driver rather
than merely avoided. Resolving a stale attempt still means an operator
explicitly POSTing to the replay endpoint.

Offline: no AWS credentials, no Bedrock, no embeddings, no network. Output
carries stable codes, identifiers, and counts only — never evidence, filing
content, reviewer or operator notes, SQL, absolute database paths, or raw
exception text.

Examples:
    python scripts/comparison_reliability_report.py
    python scripts/comparison_reliability_report.py --issues --failures
    python scripts/comparison_reliability_report.py --json
    python scripts/comparison_reliability_report.py \\
        --since 2026-07-01T00:00:00+00:00 --until 2026-07-29T00:00:00+00:00

Exit codes:
    0  a valid report was produced (EVEN IF unresolved issues exist — this
       command reports state, it does not gate on it). A correctly initialized
       database holding zero records reports an explicit empty system here
    1  the report was refused: storage exists but could not be read or lacks a
       required workflow table (reliability_storage_unavailable /
       reliability_data_invalid), stored records are inconsistent
       (reliability_data_invalid), or a dependency needed to evaluate replay
       eligibility is unavailable (reliability_dependency_unavailable). Nothing
       partial is printed in any of those cases
    2  invalid arguments or configuration, including a --db-path that does not
       exist (this command never creates a database)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import comparison_reliability
import config

# Shown instead of any path, so a report can be pasted anywhere safely.
_DB_LABEL = "configured comparison database"


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}s"


def _rate_line(metric: dict) -> str:
    suffix = " ZERO-DENOMINATOR (no rate asserted)" if metric["zero_denominator"] else ""
    return (
        f"{_fmt(metric['value'])} "
        f"({metric['numerator']}/{metric['denominator']}){suffix}"
    )


def print_summary(report: dict) -> None:
    """Human-readable terminal report. No paths, no notes, no evidence."""
    print("Comparison reliability report (read-only view of persisted records)")
    print(f"  contract={report['contract_version']} generated={report['generated_at']}")
    print(
        f"  window since={report['since'] or 'unbounded'} "
        f"until={report['until'] or 'unbounded'} (inclusive; attempts by "
        "started_at, replays by requested_at)"
    )
    print(
        f"  recovery policy={report['recovery_policy_id']}"
        f"/{report['recovery_policy_version']} "
        f"stale_after={report['stale_after_seconds']}s "
        f"max_attempts={report['max_attempts_per_comparison']}"
    )
    print(
        f"  lease policy={report['lease_policy_id']}"
        f"/{report['lease_policy_version']} "
        f"lease={report['lease_duration_seconds']}s "
        f"heartbeat_extension={report['heartbeat_extension_seconds']}s "
        f"reclaim_grace={report['reclaim_grace_seconds']}s "
        f"max_generations={report['max_claim_generations']}"
    )
    print(
        "  detector versions="
        + (", ".join(report["detector_versions"]) or "none")
        + " workflow versions="
        + (", ".join(report["workflow_versions"]) or "none")
    )

    gauges = report["gauges"]
    print("\nCurrent state (evaluated now, NOT restricted by the window):")
    for label, key in (
        ("comparisons ready_for_detection", "comparisons_ready_for_detection"),
        ("comparisons queued_for_detection", "comparisons_queued_for_detection"),
        ("comparisons detecting", "comparisons_detecting"),
        ("comparisons detected", "comparisons_detected"),
        ("comparisons failed", "comparisons_failed"),
        ("running attempts", "running_attempts"),
        ("stale running attempts", "stale_running_attempts"),
        ("replay-eligible attempts", "replay_eligible_attempts"),
        ("attempt-limit-exhausted comparisons", "attempt_limit_exhausted_comparisons"),
        ("detection jobs queued", "detection_jobs_queued"),
        ("detection jobs running", "detection_jobs_running"),
        ("detection jobs succeeded", "detection_jobs_succeeded"),
        ("detection jobs failed", "detection_jobs_failed"),
        ("active job leases", "active_job_leases"),
        ("expired job leases", "expired_job_leases"),
        ("reclaimable jobs", "reclaimable_jobs"),
        ("claim-exhausted jobs", "claim_exhausted_jobs"),
        ("unresolved operational issues", "unresolved_operational_issues"),
    ):
        print(f"  {label:<38} {gauges[key]}")

    jobs = report["jobs"]
    print("\nDetection jobs in window (queued_at):")
    for label, key in (
        ("queued", "jobs_queued"),
        ("claimed", "jobs_claimed"),
        ("succeeded", "jobs_succeeded"),
        ("failed", "jobs_failed"),
        ("heartbeat events", "job_heartbeats"),
        ("reclaimed", "jobs_reclaimed"),
        ("claim generations exhausted", "jobs_claim_exhausted"),
    ):
        print(f"  {label:<38} {jobs[key]}")

    job_durations = report["job_durations"]
    print(
        f"\nJob durations (percentile={job_durations['percentile_method']}):"
    )
    for prefix, label in (
        ("queue_wait", "queue wait"),
        ("execution", "execution"),
    ):
        print(f"  {label + ' count':<38} {job_durations[prefix + '_count']}")
        for statistic in ("min", "max", "mean", "p50", "p95"):
            key = f"{prefix}_seconds_{statistic}"
            print(f"  {(label + ' ' + statistic):<38} {_seconds(job_durations[key])}")
    print(
        f"  {'negative lease durations':<38} "
        f"{job_durations['negative_lease_duration_jobs']}"
    )

    attempts = report["attempts"]
    print("\nAttempts in window:")
    for label, key in (
        ("started", "attempts_started"),
        ("succeeded", "attempts_succeeded"),
        ("failed", "attempts_failed"),
        ("timed_out", "attempts_timed_out"),
        ("still running", "attempts_running_in_window"),
        ("terminal (rate denominator)", "terminal_attempts"),
    ):
        print(f"  {label:<38} {attempts[key]}")

    print("\nAttempt rates (numerator/denominator; running never in denominator):")
    for name, metric in report["attempt_rates"].items():
        print(f"  {name:<38} {_rate_line(metric)}")

    replays = report["replays"]
    print("\nReplays in window:")
    for label, key in (
        ("requested", "replays_started"),
        ("replacements succeeded", "replay_replacements_succeeded"),
        ("replacements failed", "replay_replacements_failed"),
        ("replacements running", "replay_replacements_running"),
        ("replacements timed_out", "replay_replacements_timed_out"),
        ("terminal replacements (denominator)", "terminal_replay_replacements"),
    ):
        print(f"  {label:<38} {replays[key]}")
    print("\nReplay rates (a running replacement is not a failure):")
    for name, metric in report["replay_rates"].items():
        print(f"  {name:<38} {_rate_line(metric)}")

    durations = report["durations"]
    print(
        f"\nTerminal attempt durations (finished_at - started_at, "
        f"percentile={durations['percentile_method']}):"
    )
    print(f"  {'count':<38} {durations['duration_count']}")
    for label, key in (
        ("min", "duration_seconds_min"),
        ("max", "duration_seconds_max"),
        ("mean", "duration_seconds_mean"),
        ("p50", "duration_seconds_p50"),
        ("p95", "duration_seconds_p95"),
    ):
        print(f"  {label:<38} {_seconds(durations[key])}")
    print(
        f"  {'negative (excluded, raises an issue)':<38} "
        f"{durations['negative_duration_attempts']}"
    )

    breakdown = report["failure_breakdown"]
    print("\nTop failure codes (failed attempts):")
    _print_counts(breakdown["failed_attempts_by_code"])
    print("Top failure codes (timed-out attempts):")
    _print_counts(breakdown["timed_out_attempts_by_code"])
    print("Failures by detector version (failed + timed_out):")
    _print_counts(breakdown["failures_by_detector_version"])
    print("Failures by workflow version (failed + timed_out):")
    _print_counts(breakdown["failures_by_workflow_version"])

    print(
        f"\nUnresolved operational issues: "
        f"{gauges['unresolved_operational_issues']} "
        "(reported only — nothing here is heartbeated, reclaimed, retried, "
        "replayed, or repaired "
        "automatically)"
    )


def _print_counts(counts: dict) -> None:
    if not counts:
        print("  none")
        return
    # Deterministic: descending count, then code ascending.
    for code, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {code:<38} {count}")


def print_issues(report: dict) -> None:
    print(
        f"\nIssues ({report['returned']} of {report['total']}"
        + (", TRUNCATED by --limit" if report["truncated"] else "")
        + "), most severe first:"
    )
    if not report["issues"]:
        print("  none")
        return
    for issue in report["issues"]:
        print(
            f"  {issue['issue_type']:<28} {issue['comparison_id']} "
            f"attempt={issue['attempt_id'] or 'none'} "
            f"status={issue['status']} "
            f"age={_seconds(issue['age_seconds'])} "
            f"attempts={issue['attempts_used']}/{issue['max_attempts']} "
            f"action={issue['recommended_action_code']}"
        )


def print_failures(report: dict) -> None:
    print(
        f"\nFailed and timed-out attempts ({report['returned']} of "
        f"{report['total']}"
        + (", TRUNCATED by --limit" if report["truncated"] else "")
        + "), newest first:"
    )
    if not report["failures"]:
        print("  none")
        return
    for failure in report["failures"]:
        print(
            f"  {failure['status']:<10} {failure['attempt_id']} "
            f"#{failure['attempt_number']} "
            f"{failure['comparison_id']} "
            f"code={failure['failure_code'] or 'none'} "
            f"duration={_seconds(failure['duration_seconds'])} "
            f"detector={failure['detector_version']} "
            f"replay={failure['replay_id'] or 'none'}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only reliability report over the persisted comparison "
            "lifecycle. Offline: no AWS credentials, no network, no LLM. "
            "Mutates nothing."
        )
    )
    parser.add_argument(
        "--db-path",
        metavar="PATH",
        default=None,
        help="comparison database to report on (default: COMPARISON_DB_PATH)",
    )
    parser.add_argument(
        "--since",
        metavar="TIMESTAMP",
        default=None,
        help=(
            "inclusive lower bound, timezone-aware ISO 8601 "
            "(e.g. 2026-07-01T00:00:00+00:00); naive timestamps are rejected"
        ),
    )
    parser.add_argument(
        "--until",
        metavar="TIMESTAMP",
        default=None,
        help="inclusive upper bound, timezone-aware ISO 8601",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the machine-readable report only (same contract as the API)",
    )
    parser.add_argument(
        "--issues", action="store_true", help="also list unresolved operational issues"
    )
    parser.add_argument(
        "--failures",
        action="store_true",
        help="also list failed and timed-out attempt summaries",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=comparison_reliability.DEFAULT_LIMIT,
        metavar="N",
        help=(
            "maximum issue/failure rows to list "
            f"(1-{comparison_reliability.MAX_LIMIT}, "
            f"default {comparison_reliability.DEFAULT_LIMIT})"
        ),
    )
    args = parser.parse_args(argv)

    # Validated up front so an out-of-range --limit is an argument error even
    # when no listing was requested, rather than silently accepted.
    try:
        comparison_reliability.parse_window(args.since, args.until)
        comparison_reliability.validate_limit(args.limit)
    except comparison_reliability.ReliabilityQueryError as exc:
        print(f"Invalid argument ({exc.code}): {exc.message}", file=sys.stderr)
        return 2

    db_path = Path(args.db_path or config.COMPARISON_DB_PATH)
    if not db_path.exists():
        # A typo'd path must not look like a healthy empty system. The file NAME
        # is printed; the absolute path deliberately is not.
        print(
            f"No comparison database found ({_DB_LABEL}, file "
            f"{db_path.name!r}). Nothing is created by this command.",
            file=sys.stderr,
        )
        return 2

    try:
        report = comparison_reliability.summary(
            since=args.since, until=args.until, db_path=db_path
        )
        issues = (
            comparison_reliability.issues(limit=args.limit, db_path=db_path)
            if args.issues
            else None
        )
        failures = (
            comparison_reliability.failures(
                since=args.since,
                until=args.until,
                limit=args.limit,
                db_path=db_path,
            )
            if args.failures
            else None
        )
    except comparison_reliability.ReliabilityQueryError as exc:
        print(f"Invalid argument ({exc.code}): {exc.message}", file=sys.stderr)
        return 2
    except comparison_reliability.ReliabilityDataError as exc:
        # Fail closed: stored records contradict themselves, so no numbers are
        # printed. Stable sub-codes only — no SQL, no paths, no row contents.
        print(
            f"Refusing to report ({exc.code}): stored workflow records are "
            f"internally inconsistent [{', '.join(exc.reasons)}].",
            file=sys.stderr,
        )
        return 1
    except comparison_reliability.ReliabilityStorageUnavailable as exc:
        # Fail closed: the database exists but cannot be read. An empty report
        # here would be a false clean signal. Stable reason code only — no path,
        # no SQLite message, no schema text.
        print(
            f"Refusing to report ({exc.code}): comparison workflow storage "
            f"could not be read [{exc.reason}]. Reporting an empty system here "
            "would be false. Nothing was modified or created.",
            file=sys.stderr,
        )
        return 1
    except comparison_reliability.ReliabilityDependencyUnavailable as exc:
        # Fail closed: replay eligibility cannot be evaluated, so NO report is
        # printed — not a partial one, and not a zero. Stable codes only; the
        # configured registry path and the underlying fault are deliberately
        # withheld.
        print(
            f"Refusing to report ({exc.code}): the {exc.dependency} dependency "
            f"required to evaluate replay eligibility is unavailable "
            f"[{exc.reason}]. Reporting zero eligible attempts here would be "
            "false. Nothing was modified.",
            file=sys.stderr,
        )
        return 1
    except (sqlite3.Error, OSError):
        print(
            f"Could not read the {_DB_LABEL}. No detail is printed here on "
            "purpose; nothing was modified.",
            file=sys.stderr,
        )
        return 1

    if args.json:
        payload = {"summary": report}
        if issues is not None:
            payload["issues"] = issues
        if failures is not None:
            payload["failures"] = failures
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print_summary(report)
    if issues is not None:
        print_issues(issues)
    if failures is not None:
        print_failures(failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
