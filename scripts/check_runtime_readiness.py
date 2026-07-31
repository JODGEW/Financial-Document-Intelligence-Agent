#!/usr/bin/env python3
"""Report read-only readiness for one role of the local reference runtime.

Checks only. This command creates no database, no table, no migration, no
registry, and no Chroma content, and it never runs a detector: storage
initialization is a separate explicit operator action documented in
OPERATIONS.md.

    python scripts/check_runtime_readiness.py --role worker \
      --db-path comparisons/comparisons.db \
      --registry-path filing_registry/registry.jsonl

Exit 0 when ready, 1 when not ready, 2 for invalid arguments. Output carries
stable check names and codes only — never a secret, path, SQL, or exception
text. ``--role worker`` is credential-free and does not check authentication.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import runtime_readiness  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only readiness check for the local single-node reference "
            "runtime. Creates, migrates, and initializes nothing."
        )
    )
    parser.add_argument(
        "--role",
        required=True,
        choices=list(runtime_readiness.ROLES),
        help="which dependency set to verify",
    )
    parser.add_argument("--db-path", metavar="PATH")
    parser.add_argument("--registry-path", metavar="PATH")
    parser.add_argument("--persist-dir", metavar="PATH")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = runtime_readiness.evaluate(
            args.role,
            db_path=args.db_path,
            registry_path=args.registry_path,
            persist_dir=args.persist_dir,
        )
    except ValueError:
        print("error: invalid --role", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"role={report['role']} status={report['status']}")
        for check in report["checks"]:
            print(
                f"  {check['name']}={check['status']}"
                + (f" code={check['code']}" if check["code"] else "")
            )
    return 0 if report["status"] == runtime_readiness.STATUS_READY else 1


if __name__ == "__main__":
    raise SystemExit(main())
