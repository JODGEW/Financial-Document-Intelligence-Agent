#!/usr/bin/env python3
"""Run at most one detection job, or heartbeat one active claim.

Credential-free: this command does not import the API/authentication boundary
and does not require FDIA_AUTH_SECRET, AWS credentials, or network access.
There is no polling loop, sleep, detector retry, scheduler, or daemon.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
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
            "Claim/reclaim and execute at most one comparison-detection job, "
            "or heartbeat one active claim. "
            "Local SQLite, offline, credential-free, and intentionally one-shot."
        )
    )
    parser.add_argument("--db-path", required=True, metavar="PATH")
    parser.add_argument("--registry-path", metavar="PATH")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--job-id", default=None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--once",
        action="store_true",
        help="required acknowledgement that this invocation handles at most one job",
    )
    mode.add_argument("--heartbeat-job-id")
    parser.add_argument("--claim-generation", type=int)
    parser.add_argument("--claim-token-env")
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
        "claimGeneration": outcome["claim_generation"],
        "leaseExpiresAt": outcome["lease_expires_at"],
        "resultHash": outcome["result_hash"],
        "failureCode": outcome["failure_code"],
        "ownershipLost": outcome["ownership_lost"],
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
        ("claimGeneration", payload["claimGeneration"]),
        ("leaseExpiresAt", payload["leaseExpiresAt"] or "none"),
        ("resultHash", payload["resultHash"] or "none"),
        ("failureCode", payload["failureCode"] or "none"),
        ("ownershipLost", str(payload["ownershipLost"]).lower()),
        (
            "workerCompletedResponsibility",
            str(payload["workerCompletedResponsibility"]).lower(),
        ),
    )
    print(" ".join(f"{key}={value}" for key, value in fields))


def _heartbeat_wire(outcome: dict) -> dict:
    return {
        "heartbeatAccepted": outcome["heartbeat_accepted"],
        "jobId": outcome["job_id"],
        "jobStatus": outcome.get("job_status"),
        "claimGeneration": outcome.get("claim_generation"),
        "leaseExpiresAt": outcome.get("lease_expires_at"),
        "failureCode": outcome.get("failure_code"),
    }


def _print_heartbeat_plain(payload: dict) -> None:
    fields = (
        ("heartbeatAccepted", str(payload["heartbeatAccepted"]).lower()),
        ("jobId", payload["jobId"]),
        ("jobStatus", payload["jobStatus"] or "none"),
        ("claimGeneration", payload["claimGeneration"] or "none"),
        ("leaseExpiresAt", payload["leaseExpiresAt"] or "none"),
        ("failureCode", payload["failureCode"] or "none"),
    )
    print(" ".join(f"{key}={value}" for key, value in fields))


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _invalid(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db_path = Path(args.db_path)
    if not db_path.is_file():
        print("worker_infrastructure_error: required local storage is unavailable",
              file=sys.stderr)
        return 1
    try:
        comparison_store.validate_worker_id(args.worker_id)
    except ValueError:
        print("error: invalid --worker-id", file=sys.stderr)
        return 2

    heartbeat_mode = args.heartbeat_job_id is not None
    if heartbeat_mode:
        if args.job_id is not None or args.registry_path is not None:
            return _invalid(
                "heartbeat mode does not accept --job-id or --registry-path"
            )
        if (
            isinstance(args.claim_generation, bool)
            or not isinstance(args.claim_generation, int)
            or args.claim_generation < 1
        ):
            return _invalid(
                "heartbeat mode requires positive --claim-generation"
            )
        if (
            not isinstance(args.claim_token_env, str)
            or not _ENV_NAME_RE.fullmatch(args.claim_token_env)
        ):
            return _invalid(
                "heartbeat mode requires a valid --claim-token-env name"
            )
        claim_token = os.environ.get(args.claim_token_env)
        if not isinstance(claim_token, str) or not claim_token:
            return _invalid("heartbeat claim-token environment value is absent")
    else:
        if args.registry_path is None:
            return _invalid("--registry-path is required with --once")
        if args.claim_generation is not None or args.claim_token_env is not None:
            return _invalid(
                "--claim-generation and --claim-token-env require heartbeat mode"
            )
        registry_path = Path(args.registry_path)
        if not registry_path.is_file():
            print(
                "worker_infrastructure_error: required local storage is unavailable",
                file=sys.stderr,
            )
            return 1

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
            if heartbeat_mode:
                outcome = comparison_detection_worker.heartbeat_job(
                    job_id=args.heartbeat_job_id,
                    worker_id=args.worker_id,
                    claim_generation=args.claim_generation,
                    claim_token=claim_token,
                    db_path=db_path,
                )
            else:
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
    except comparison_store.DetectionStateError as exc:
        if not heartbeat_mode:
            print(
                "worker_infrastructure_error: execution could not be completed",
                file=sys.stderr,
            )
            return 1
        outcome = {
            "heartbeat_accepted": False,
            "job_id": args.heartbeat_job_id,
            "failure_code": exc.code,
        }
    except Exception:
        print("worker_infrastructure_error: execution could not be completed",
              file=sys.stderr)
        return 1

    payload = _heartbeat_wire(outcome) if heartbeat_mode else _wire(outcome)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    elif heartbeat_mode:
        _print_heartbeat_plain(payload)
    else:
        _print_plain(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
