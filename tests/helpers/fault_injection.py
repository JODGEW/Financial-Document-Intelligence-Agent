"""Test-only fault actions and the inter-process rendezvous behind them.

``runtime_fault_hooks`` in the application deliberately knows nothing about
faults: it holds one hook slot and calls it. Every action vocabulary lives
here, so a fault behaviour cannot leak into the production runtime.

The rendezvous is a plain directory of marker files, chosen because both sides
of it must work across a real ``fork``/``exec`` boundary and must survive the
child being ``SIGKILL``-ed mid-wait. A child announces "I reached checkpoint X"
by creating a file; a parent releases it by creating another. There is no
shared memory, no socket, and no state that outlives the temporary directory.

Actions
-------
``signal``           announce arrival and continue
``block``            announce arrival, then wait (bounded) for an explicit release
``exit``             announce arrival, then ``os._exit`` — no cleanup, uncatchable
``raise_transient``  announce arrival, then raise the one allowlisted transient
                     detection failure, so the real retry path runs
``stop_thread``      announce arrival, then raise ``SystemExit`` — used only on
                     the heartbeat thread, where it ends that thread silently and
                     lets the claim's lease lapse while the worker keeps running

Every action takes an optional ``once`` flag so a checkpoint on a loop (the
heartbeat) can act exactly once.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

ACTION_SIGNAL = "signal"
ACTION_BLOCK = "block"
ACTION_EXIT = "exit"
ACTION_RAISE_TRANSIENT = "raise_transient"
ACTION_STOP_THREAD = "stop_thread"

ACTIONS = frozenset(
    {
        ACTION_SIGNAL,
        ACTION_BLOCK,
        ACTION_EXIT,
        ACTION_RAISE_TRANSIENT,
        ACTION_STOP_THREAD,
    }
)

# Controlled exit code for a child terminated at a checkpoint. Distinct from
# 0 (clean), 1 (worker_infrastructure_error), and 2 (invalid arguments).
CHECKPOINT_EXIT_CODE = 91

_POLL_SECONDS = 0.02
_DEFAULT_BLOCK_TIMEOUT = 60.0


def reached_marker(gate_dir: Path, checkpoint: str) -> Path:
    return Path(gate_dir) / f"reached.{checkpoint}"


def release_marker(gate_dir: Path, checkpoint: str) -> Path:
    return Path(gate_dir) / f"release.{checkpoint}"


def announce(gate_dir: Path, checkpoint: str, context: Mapping[str, Any]) -> None:
    """Record arrival atomically, with workflow identifiers only.

    Written to a temporary name and renamed so a parent can never observe a
    half-written marker, and never carries claim material: the checkpoint
    contract already forbids it, and this is the file a test reads back.
    """
    payload = {
        key: value
        for key, value in context.items()
        if key
        in {
            "comparison_id",
            "job_id",
            "attempt_id",
            "worker_id",
            "claim_generation",
            "job_status",
            "retry_count",
            "result_hash",
        }
    }
    target = reached_marker(gate_dir, checkpoint)
    staging = target.with_suffix(target.suffix + ".partial")
    staging.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(staging, target)


def _wait_for_release(gate_dir: Path, checkpoint: str, timeout: float) -> None:
    """Block until released, or give up so a wedged child cannot hang CI."""
    marker = release_marker(gate_dir, checkpoint)
    deadline = time.monotonic() + timeout
    while not marker.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"fault checkpoint {checkpoint} was never released"
            )
        time.sleep(_POLL_SECONDS)


def build_hook(spec: Mapping[str, Any]):
    """Return the callable installed into ``runtime_fault_hooks``.

    ``spec`` is ``{"gate_dir": str, "actions": {checkpoint: {...}}}``. Any
    checkpoint not named in ``actions`` is a no-op, so a fault plan only
    affects the boundaries it explicitly lists.
    """
    gate_dir = Path(spec["gate_dir"])
    actions = dict(spec["actions"])
    for checkpoint, action in actions.items():
        if action["action"] not in ACTIONS:
            raise ValueError(f"unknown fault action for {checkpoint}")
    fired: set[str] = set()

    def hook(checkpoint: str, context: Mapping[str, Any]) -> None:
        action = actions.get(checkpoint)
        if action is None:
            return
        if action.get("once") and checkpoint in fired:
            return
        fired.add(checkpoint)
        announce(gate_dir, checkpoint, context)

        kind = action["action"]
        if kind == ACTION_SIGNAL:
            return
        if kind == ACTION_BLOCK:
            _wait_for_release(
                gate_dir,
                checkpoint,
                float(action.get("timeout", _DEFAULT_BLOCK_TIMEOUT)),
            )
            return
        if kind == ACTION_EXIT:
            # os._exit, not sys.exit: no unwinding, no finally blocks, no
            # atexit, no buffered output. The closest a test can get to a
            # process that simply stopped existing.
            os._exit(int(action.get("code", CHECKPOINT_EXIT_CODE)))
        if kind == ACTION_RAISE_TRANSIENT:
            import comparison_detector

            raise comparison_detector.DetectionDependencyUnavailable(
                "detection_dependency_unavailable",
                "a detection dependency was temporarily unavailable",
            )
        if kind == ACTION_STOP_THREAD:
            # SystemExit is not an Exception, so the heartbeat controller's
            # fault handling does not swallow it, and threading.excepthook
            # ignores it. The thread ends; the worker process does not.
            raise SystemExit(0)

    return hook


def install_from_spec(raw: str | None) -> bool:
    """Install a hook from a JSON spec. Returns whether one was installed."""
    if not raw:
        return False
    import runtime_fault_hooks

    runtime_fault_hooks.install(build_hook(json.loads(raw)))
    return True
