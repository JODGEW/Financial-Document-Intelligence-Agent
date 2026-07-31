"""Deterministic named process-interruption checkpoints, disabled by default.

Unit and integration tests prove the detection-job state machine inside a
single Python test process. Proving it also survives real operating-system
process termination needs a way to stop a *child* process at an exact point
relative to a committed SQLite transaction. That is what this module provides,
and nothing else.

**No production runtime enables it.** The hook slot starts empty and only an
in-process Python call to :func:`install` fills it. There is deliberately no
environment variable, HTTP header, query parameter, request body, config file,
policy key, or command-line flag that can install a hook, so neither
``uvicorn api:app`` nor ``scripts/run_comparison_detection_worker.py`` can
reach one. The only callers of :func:`install` are the test-only child entry
points under ``tests/helpers``.

**This module knows nothing about faults.** :func:`checkpoint` looks up the
slot and calls the installed hook; blocking, terminating the process,
signalling a parent, and raising a controlled failure are all decided
test-side. Keeping the action vocabulary out of the application means a fault
behaviour can never be added here by accident.

When no hook is installed — every production invocation — :func:`checkpoint`
performs one module-global read and returns ``None``. It touches no storage,
emits no log record, and cannot change business state.

Context values are bounded workflow identifiers only. A claim token, token
hash, bearer token, shared secret, evidence excerpt, document text, path, or
SQL fragment must never be passed to a checkpoint.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

# --- The closed checkpoint vocabulary ----------------------------------------
# Each name marks one deterministic boundary in the durable detection workflow,
# expressed relative to the transaction that makes the state change durable.

API_BEFORE_ENQUEUE = "api_before_enqueue"
API_AFTER_ENQUEUE_COMMIT_BEFORE_RESPONSE = (
    "api_after_enqueue_commit_before_response"
)
WORKER_AFTER_CLAIM_COMMIT = "worker_after_claim_commit"
WORKER_AFTER_RECLAIM_COMMIT = "worker_after_reclaim_commit"
WORKER_AFTER_RETRY_CLAIM_COMMIT = "worker_after_retry_claim_commit"
WORKER_BEFORE_DETECTOR_COMPUTE = "worker_before_detector_compute"
WORKER_AFTER_DETECTOR_COMPUTE_BEFORE_TERMINAL_COMMIT = (
    "worker_after_detector_compute_before_terminal_commit"
)
WORKER_AFTER_TERMINAL_COMMIT_BEFORE_OUTPUT = (
    "worker_after_terminal_commit_before_output"
)
WORKER_AFTER_RETRY_SCHEDULE_COMMIT = "worker_after_retry_schedule_commit"
HEARTBEAT_BEFORE_EXTEND = "heartbeat_before_extend"

CHECKPOINTS = frozenset(
    {
        API_BEFORE_ENQUEUE,
        API_AFTER_ENQUEUE_COMMIT_BEFORE_RESPONSE,
        WORKER_AFTER_CLAIM_COMMIT,
        WORKER_AFTER_RECLAIM_COMMIT,
        WORKER_AFTER_RETRY_CLAIM_COMMIT,
        WORKER_BEFORE_DETECTOR_COMPUTE,
        WORKER_AFTER_DETECTOR_COMPUTE_BEFORE_TERMINAL_COMMIT,
        WORKER_AFTER_TERMINAL_COMMIT_BEFORE_OUTPUT,
        WORKER_AFTER_RETRY_SCHEDULE_COMMIT,
        HEARTBEAT_BEFORE_EXTEND,
    }
)

# Never accepted in checkpoint context, whatever a future call site intends.
FORBIDDEN_CONTEXT_KEYS = frozenset(
    {
        "claim_token",
        "claim_token_hash",
        "token",
        "token_hash",
        "secret",
        "authorization",
        "bearer",
        "password",
        "evidence",
        "excerpt",
        "result_json",
        "db_path",
        "path",
        "sql",
    }
)

_hook: Callable[[str, Mapping[str, Any]], None] | None = None


def install(hook: Callable[[str, Mapping[str, Any]], None]) -> None:
    """Install the single process-wide hook. Test-only; never called in production."""
    global _hook
    if not callable(hook):
        raise TypeError("fault hook must be callable")
    _hook = hook


def clear() -> None:
    """Remove any installed hook, restoring the production no-op behaviour."""
    global _hook
    _hook = None


def installed() -> bool:
    """Whether a hook is currently installed. False in every production process."""
    return _hook is not None


def checkpoint(name: str, /, **context: Any) -> None:
    """Reach a named boundary.

    A no-op returning ``None`` whenever no hook is installed, which is every
    production invocation. The name and context are validated only on the
    enabled path so the disabled path stays a single global read.
    """
    hook = _hook
    if hook is None:
        return
    if name not in CHECKPOINTS:
        raise ValueError(f"unknown fault checkpoint: {name!r}")
    forbidden = FORBIDDEN_CONTEXT_KEYS.intersection(context)
    if forbidden:
        raise ValueError(
            f"checkpoint context may not carry {sorted(forbidden)}"
        )
    hook(name, dict(context))
