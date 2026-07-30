#!/usr/bin/env python3
"""Run at most one durable comparison-detection job.

Credential-free: this command does not import the API/authentication boundary
and does not require FDIA_AUTH_SECRET, AWS credentials, or network access.
There is no polling loop, sleep, retry, lease, heartbeat, or reclaim.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import comparison_detection_worker  # noqa: E402
import comparison_store  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Claim and execute at most one queued comparison-detection job. "
            "Local SQLite, offline, credential-free, and intentionally one-shot."
        )
    )
    parser.add_argument("--db-path", required=True, metavar="PATH")
    parser.add_argument("--registry-path", required=True, metavar="PATH")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--job-id", default=None)
    parser.add_argument(
        "--once",
        required=True,
        action="store_true",
        help="required acknowledgement that this invocation handles at most one job",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def _wire(outcome: dict) -> dict:
    if outcome["no_job_available"]:
        return {"noJobAvailable": True}
    return {
        "noJobAvailable": False,
        "jobId": outcome["job_id"],
        "attemptId": outcome["attempt_id"],
        "jobStatus": outcome["job_status"],
        "attemptStatus": outcome["attempt_status"],
        "resultHash": outcome["result_hash"],
        "failureCode": outcome["failure_code"],
        "workerCompletedResponsibility": outcome[
            "worker_completed_responsibility"
        ],
    }


def _print_plain(payload: dict) -> None:
    if payload["noJobAvailable"]:
        print("noJobAvailable=true")
        return
    fields = (
        ("jobId", payload["jobId"]),
        ("attemptId", payload["attemptId"] or "none"),
        ("jobStatus", payload["jobStatus"]),
        ("attemptStatus", payload["attemptStatus"] or "none"),
        ("resultHash", payload["resultHash"] or "none"),
        ("failureCode", payload["failureCode"] or "none"),
        (
            "workerCompletedResponsibility",
            str(payload["workerCompletedResponsibility"]).lower(),
        ),
    )
    print(" ".join(f"{key}={value}" for key, value in fields))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db_path = Path(args.db_path)
    registry_path = Path(args.registry_path)
    if not db_path.is_file() or not registry_path.is_file():
        print("worker_infrastructure_error: required local storage is unavailable",
              file=sys.stderr)
        return 1
    try:
        comparison_store.validate_worker_id(args.worker_id)
    except ValueError:
        print("error: invalid --worker-id", file=sys.stderr)
        return 2

    # The detector keeps full unexpected diagnostics for server deployments.
    # This standalone CLI has no private server log, so prevent its fallback
    # handler from rendering exception text or paths to stderr.
    logger_states = []
    for logger_name in ("comparison_detector", "comparison_detection_worker"):
        target = logging.getLogger(logger_name)
        null_handler = logging.NullHandler()
        logger_states.append((target, null_handler, target.propagate))
        target.addHandler(null_handler)
        target.propagate = False

    try:
        try:
            outcome = comparison_detection_worker.run_one_job(
                worker_id=args.worker_id,
                job_id=args.job_id,
                db_path=db_path,
                registry_path=registry_path,
            )
        finally:
            for target, null_handler, propagate in logger_states:
                target.removeHandler(null_handler)
                target.propagate = propagate
    except Exception:
        print("worker_infrastructure_error: execution could not be completed",
              file=sys.stderr)
        return 1

    payload = _wire(outcome)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        _print_plain(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
