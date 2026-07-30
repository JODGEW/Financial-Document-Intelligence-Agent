#!/usr/bin/env python3
"""Issue one short-lived local comparison-workflow access token.

The command is offline and credential-free apart from ``FDIA_AUTH_SECRET``:
it makes no network, AWS, database, or filesystem writes.

Examples:

    python scripts/issue_local_access_token.py \
      --subject operator@example.local --role operator --ttl-seconds 900

    python scripts/issue_local_access_token.py \
      --subject reviewer@example.local --role reviewer --json

The token is printed once to stdout.  Treat stdout and shell history as
sensitive: do not put the secret or a previously issued token directly in a
command line, and avoid redirecting token output into a tracked file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from governance.policy_validation import GovernancePolicyConfigError  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Issue one signed local comparison-workflow access token. "
            "Offline: no network, AWS credentials, user database, or token file."
        ),
        epilog=(
            "Security: token output and FDIA_AUTH_SECRET are credentials. "
            "Avoid command-line values or redirection patterns that preserve "
            "them in shell history or tracked files."
        ),
    )
    parser.add_argument(
        "--subject",
        required=True,
        help="authenticated local subject (1-120 characters; no control characters)",
    )
    parser.add_argument(
        "--role",
        required=True,
        action="append",
        help="policy-defined role; repeat for a multi-role token",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=900,
        metavar="N",
        help="token lifetime in seconds (default 900; policy maximum applies)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print only the allowlisted JSON issuance response",
    )
    return parser


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    # Match application configuration behavior while keeping this command
    # independent of config.py and therefore of unrelated AWS/RAG settings.
    load_dotenv()

    try:
        # Keep this import inside the refusal boundary: access_control validates
        # the checked-in policy at import, and a present-invalid policy must be
        # a clean exit-2 configuration refusal rather than a traceback.
        import access_control
    except GovernancePolicyConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        policy = access_control.load_access_control_policy()
        secret = access_control.validate_auth_secret(
            os.environ.get(access_control.AUTH_SECRET_ENV)
        )
        issued_at = datetime.now(timezone.utc).replace(microsecond=0)
        token = access_control.issue_access_token(
            policy=policy,
            secret=secret,
            subject=args.subject,
            roles=tuple(args.role),
            ttl_seconds=args.ttl_seconds,
            now=issued_at,
        )
    except (GovernancePolicyConfigError, access_control.TokenIssuanceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "accessToken": token,
                    "tokenType": "Bearer",
                    "expiresAt": _format_utc(
                        issued_at + timedelta(seconds=args.ttl_seconds)
                    ),
                    "subject": args.subject,
                    "roles": sorted(args.role),
                },
                sort_keys=True,
            )
        )
    else:
        print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
