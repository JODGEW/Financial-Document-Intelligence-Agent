"""Test-only worker entry point with an optional fault injector.

This exists so a test can install a fault hook into a *real* child process.
The production worker CLI (``scripts/run_comparison_detection_worker.py``)
deliberately has no such flag and no environment path to one; this module is
the only entry point that can install a hook, it lives under ``tests/``, and it
is never imported by application code.

Apart from the injector and the short checked test policies, it calls exactly
the seam the production CLI calls — ``comparison_detection_worker.run_one_job``
— so what these tests exercise is the shipped code path, not a parallel one.

``--use-cli`` runs the real production CLI's ``main()`` instead, for the cases
that need no policy override and should prove the shipped command itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402
from tests.helpers import fault_injection  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="test-only one-shot worker")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--registry-path", required=True)
    parser.add_argument("--persist-dir", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--faults", default=None)
    parser.add_argument("--lease-policy", default=None)
    parser.add_argument("--retry-policy", default=None)
    parser.add_argument("--now", default=None)
    parser.add_argument("--use-cli", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    # The controlled Chroma store lives in a temporary directory; the detector
    # resolves it from config at compute time.
    config.CHROMA_PERSIST_DIR = args.persist_dir

    if args.use_cli:
        from scripts import run_comparison_detection_worker as cli

        return cli.main(
            [
                "--db-path",
                args.db_path,
                "--registry-path",
                args.registry_path,
                "--worker-id",
                args.worker_id,
                *(["--job-id", args.job_id] if args.job_id else []),
                "--once",
                "--json",
            ]
        )

    import comparison_detection_worker

    fault_injection.install_from_spec(args.faults)

    outcome = comparison_detection_worker.run_one_job(
        worker_id=args.worker_id,
        job_id=args.job_id,
        db_path=args.db_path,
        registry_path=args.registry_path,
        policy=json.loads(args.lease_policy) if args.lease_policy else None,
        retry_policy=json.loads(args.retry_policy) if args.retry_policy else None,
        now=datetime.fromisoformat(args.now) if args.now else None,
    )
    print(json.dumps(outcome, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
