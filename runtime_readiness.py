"""Read-only readiness checks for the local single-node reference runtime.

Liveness answers "is this process running". Readiness answers the strictly
harder question "can this process do its job right now", and it answers it
**without doing that job**: nothing here creates a database, creates a table,
runs a migration, creates a registry, writes to Chroma, or executes a
detector. Initialization is a separate, explicit operator action documented in
OPERATIONS.md — a readiness probe that silently initialized storage would
report ready by having caused the very state it was asked to verify.

Readiness fails closed. A dependency that cannot be observed is reported
``failed``, never ``ok``, because "nothing is wrong" and "nothing can be seen"
must not look the same to an operator. This mirrors the reliability module's
existing fail-closed contract.

The API and the worker do not need the same things, so checks are scoped:

===========================  =====  ======  ===================================
check                        api    worker  what it proves
===========================  =====  ======  ===================================
auth_policy                  yes    no      the checked access-control policy loads
auth_secret                  yes    no      a usable runtime signing secret exists
workflow_policies            yes    yes     lease/retry/recovery policies are valid
comparison_database          yes    yes     SQLite exists and carries every table
filing_registry              yes    yes     the registry file exists and is readable
vector_store                 no     yes     the controlled Chroma directory is present
===========================  =====  ======  ===================================

The worker deliberately does not check authentication: it is credential-free
and never imports the API boundary. The API deliberately does not check the
vector store: it enqueues and never runs detector computation.

Public output carries stable codes only — never a secret value, filesystem
path, SQL, schema DDL, SQLite error text, or raw exception text.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import comparison_store
import config

ROLE_API = "api"
ROLE_WORKER = "worker"
ROLES = (ROLE_API, ROLE_WORKER)

STATUS_READY = "ready"
STATUS_NOT_READY = "not_ready"
CHECK_OK = "ok"
CHECK_FAILED = "failed"

# Stable public failure codes. These are part of the operator contract.
CODE_AUTH_POLICY_INVALID = "auth_policy_invalid"
CODE_AUTH_SECRET_UNAVAILABLE = "auth_secret_unavailable"
CODE_WORKFLOW_POLICY_INVALID = "workflow_policy_invalid"
CODE_DATABASE_UNAVAILABLE = "comparison_database_unavailable"
CODE_DATABASE_SCHEMA_INCOMPLETE = "comparison_database_schema_incomplete"
CODE_REGISTRY_UNAVAILABLE = "filing_registry_unavailable"
CODE_VECTOR_STORE_UNAVAILABLE = "vector_store_unavailable"

NOT_READY_CODE = "runtime_not_ready"
NOT_READY_MESSAGE = "One or more runtime dependencies are unavailable."

CHECK_AUTH_POLICY = "auth_policy"
CHECK_AUTH_SECRET = "auth_secret"
CHECK_WORKFLOW_POLICIES = "workflow_policies"
CHECK_COMPARISON_DATABASE = "comparison_database"
CHECK_FILING_REGISTRY = "filing_registry"
CHECK_VECTOR_STORE = "vector_store"

_API_CHECKS = (
    CHECK_AUTH_POLICY,
    CHECK_AUTH_SECRET,
    CHECK_WORKFLOW_POLICIES,
    CHECK_COMPARISON_DATABASE,
    CHECK_FILING_REGISTRY,
)
_WORKER_CHECKS = (
    CHECK_WORKFLOW_POLICIES,
    CHECK_COMPARISON_DATABASE,
    CHECK_FILING_REGISTRY,
    CHECK_VECTOR_STORE,
)
CHECKS_FOR_ROLE = {ROLE_API: _API_CHECKS, ROLE_WORKER: _WORKER_CHECKS}


def _ok(name: str) -> dict[str, Any]:
    return {"name": name, "status": CHECK_OK, "code": None}


def _failed(name: str, code: str) -> dict[str, Any]:
    return {"name": name, "status": CHECK_FAILED, "code": code}


def _check_auth_policy() -> dict[str, Any]:
    """The checked access-control policy parses and validates."""
    try:
        import access_control

        access_control.load_access_control_policy()
    except Exception:
        return _failed(CHECK_AUTH_POLICY, CODE_AUTH_POLICY_INVALID)
    return _ok(CHECK_AUTH_POLICY)


def _check_auth_secret() -> dict[str, Any]:
    """A runtime signing secret exists and satisfies the strength rule.

    Only validity is reported. The value is never returned, logged, hashed into
    output, or described by length.
    """
    try:
        import access_control

        access_control.validate_auth_secret(
            os.environ.get(access_control.AUTH_SECRET_ENV)
        )
    except Exception:
        return _failed(CHECK_AUTH_SECRET, CODE_AUTH_SECRET_UNAVAILABLE)
    return _ok(CHECK_AUTH_SECRET)


def _check_workflow_policies() -> dict[str, Any]:
    """Lease, retry, and recovery policies load under their own validators."""
    try:
        import detection_job_lease
        import detection_job_retry
        import detection_recovery

        detection_job_lease.load_policy()
        detection_job_retry.load_policy()
        detection_recovery.load_policy()
    except Exception:
        return _failed(CHECK_WORKFLOW_POLICIES, CODE_WORKFLOW_POLICY_INVALID)
    return _ok(CHECK_WORKFLOW_POLICIES)


def _check_comparison_database(db_path: str | Path | None) -> dict[str, Any]:
    """The database exists, opens read-only, and carries every required table.

    Delegates to the store's read-only probe, so readiness cannot create or
    migrate storage even by mistake: the connection is opened ``mode=ro`` and a
    write is refused by the driver rather than merely avoided by convention.
    """
    try:
        missing = comparison_store.probe_readonly_schema(db_path)
    except comparison_store.ReliabilityStorageUnavailable:
        return _failed(CHECK_COMPARISON_DATABASE, CODE_DATABASE_UNAVAILABLE)
    except Exception:
        return _failed(CHECK_COMPARISON_DATABASE, CODE_DATABASE_UNAVAILABLE)
    if missing:
        # Which table is absent stays server-side; the public code says only
        # that the schema is incomplete.
        return _failed(
            CHECK_COMPARISON_DATABASE, CODE_DATABASE_SCHEMA_INCOMPLETE
        )
    return _ok(CHECK_COMPARISON_DATABASE)


def _check_filing_registry(registry_path: str | Path | None) -> dict[str, Any]:
    """The registry file exists and is readable. Its contents are not parsed."""
    path = Path(registry_path or config.FILING_REGISTRY_PATH)
    try:
        if not path.is_file():
            return _failed(CHECK_FILING_REGISTRY, CODE_REGISTRY_UNAVAILABLE)
        with path.open("rb"):
            pass
    except OSError:
        return _failed(CHECK_FILING_REGISTRY, CODE_REGISTRY_UNAVAILABLE)
    return _ok(CHECK_FILING_REGISTRY)


def _check_vector_store(persist_dir: str | Path | None) -> dict[str, Any]:
    """The controlled Chroma directory exists and is readable.

    Presence only: no collection is opened, no embedding is computed, and
    nothing is written. The worker needs the directory to run a detector; the
    API never touches it.
    """
    path = Path(persist_dir or config.CHROMA_PERSIST_DIR)
    try:
        if not path.is_dir():
            return _failed(CHECK_VECTOR_STORE, CODE_VECTOR_STORE_UNAVAILABLE)
        os.listdir(path)
    except OSError:
        return _failed(CHECK_VECTOR_STORE, CODE_VECTOR_STORE_UNAVAILABLE)
    return _ok(CHECK_VECTOR_STORE)


def evaluate(
    role: str,
    *,
    db_path: str | Path | None = None,
    registry_path: str | Path | None = None,
    persist_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run this role's read-only checks and return a safe structured report.

    Returns ``{"role", "status", "checks"}`` where every check carries a name,
    ``ok``/``failed``, and a stable code. Nothing in the report is a path,
    secret, SQL fragment, or exception string.
    """
    if role not in CHECKS_FOR_ROLE:
        raise ValueError(f"unknown readiness role: {role!r}")

    runners: dict[str, Callable[[], dict[str, Any]]] = {
        CHECK_AUTH_POLICY: _check_auth_policy,
        CHECK_AUTH_SECRET: _check_auth_secret,
        CHECK_WORKFLOW_POLICIES: _check_workflow_policies,
        CHECK_COMPARISON_DATABASE: lambda: _check_comparison_database(db_path),
        CHECK_FILING_REGISTRY: lambda: _check_filing_registry(registry_path),
        CHECK_VECTOR_STORE: lambda: _check_vector_store(persist_dir),
    }
    checks = [runners[name]() for name in CHECKS_FOR_ROLE[role]]
    ready = all(check["status"] == CHECK_OK for check in checks)
    return {
        "role": role,
        "status": STATUS_READY if ready else STATUS_NOT_READY,
        "checks": checks,
    }
