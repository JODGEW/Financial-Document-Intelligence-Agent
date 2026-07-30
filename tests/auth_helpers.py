"""Small helpers for authenticated comparison API tests."""

import os
from typing import Dict, Sequence


DEFAULT_TEST_SUBJECT = "test-admin"
DEFAULT_TEST_ROLES = ("admin",)


def issue_test_access_token(
    *,
    subject: str = DEFAULT_TEST_SUBJECT,
    roles: Sequence[str] = DEFAULT_TEST_ROLES,
    ttl_seconds: int = 3600,
) -> str:
    """Issue a short-lived token through the same code used by the local CLI."""
    from access_control import issue_access_token, load_access_control_policy

    policy = load_access_control_policy()
    return issue_access_token(
        subject=subject,
        roles=tuple(roles),
        ttl_seconds=ttl_seconds,
        secret=os.environ["FDIA_AUTH_SECRET"],
        policy=policy,
    )


def authorization_headers(
    *,
    subject: str = DEFAULT_TEST_SUBJECT,
    roles: Sequence[str] = DEFAULT_TEST_ROLES,
    ttl_seconds: int = 3600,
) -> Dict[str, str]:
    """Return a fresh Bearer header for a deterministic test principal."""
    token = issue_test_access_token(
        subject=subject,
        roles=roles,
        ttl_seconds=ttl_seconds,
    )
    return {"Authorization": f"Bearer {token}"}
