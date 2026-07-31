"""Test-only API entry point with an optional fault injector.

Runs the real ``api.app`` under a real uvicorn server on localhost. The hook is
installed here, in a module under ``tests/``, before the server starts; the
documented way to run this service — ``python -m uvicorn api:app`` — reaches no
such code and has no flag, header, or environment variable that could.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402
from tests.helpers import fault_injection  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="test-only API process")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--persist-dir", required=True)
    parser.add_argument("--faults", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config.CHROMA_PERSIST_DIR = args.persist_dir

    import uvicorn

    import api

    fault_injection.install_from_spec(args.faults)

    uvicorn.run(
        api.app,
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
