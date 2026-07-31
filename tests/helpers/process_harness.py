"""Credential-free harness for launching and reaping real child processes.

Every child started here runs offline against temporary directories: no
internet access, no Bedrock, Tavily, or other provider call, no production or
user corpus, and no inherited real provider credentials — the environment is
built by allowlist, not by copying the developer's shell.

Every wait is bounded, every child is terminated and reaped even when a test
fails, and diagnostics printed on timeout are limited to a process label, exit
code, and truncated output. Nothing here leaves a database, socket, PID file,
or orphaned process behind, so the suite is safe to rerun repeatedly.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence

from tests.helpers import fault_injection

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The same deterministic value tests/conftest.py installs. It is derived in
# code, never read from a repository secret, a developer's environment, or a
# checked-in file.
TEST_AUTH_SECRET = hashlib.sha256(
    b"fdia deterministic pytest auth secret v1"
).hexdigest()

# Anything that could reach a paid or networked provider from a child process.
# Cleared rather than trusted: a developer running the suite locally almost
# certainly has real values for several of these exported.
PROVIDER_ENV_VARS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "TAVILY_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "BEDROCK_GUARDRAIL_ID",
    "BEDROCK_GUARDRAIL_VERSION",
    "LANGSMITH_API_KEY",
    "LANGSMITH_TRACING",
    "LANGCHAIN_API_KEY",
    "LANGCHAIN_TRACING_V2",
)

DEFAULT_TIMEOUT = 60.0
_POLL_SECONDS = 0.02


def child_env(
    *,
    db_path: Path | str | None = None,
    registry_path: Path | str | None = None,
    auth_secret: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment with every provider variable neutralized.

    Provider variables are **replaced with empty values, not deleted**. This is
    deliberate: ``config.py`` calls ``load_dotenv()`` at import, and python-dotenv
    defaults to ``override=False`` — it fills in names that are *absent*. Popping
    a real ``AWS_SECRET_ACCESS_KEY`` would therefore let the developer's checked-out
    ``.env`` put it straight back. Setting the name to an empty string keeps it
    present, so dotenv leaves it alone and the child provably holds no usable
    provider credential.
    """
    env = dict(os.environ)
    for name in PROVIDER_ENV_VARS:
        env[name] = ""
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    # Same reasoning: empty, not absent, so .env cannot supply a real secret.
    env["FDIA_AUTH_SECRET"] = auth_secret if auth_secret is not None else ""
    if db_path is not None:
        env["COMPARISON_DB_PATH"] = str(db_path)
    if registry_path is not None:
        env["FILING_REGISTRY_PATH"] = str(registry_path)
    if extra:
        env.update({key: str(value) for key, value in extra.items()})
    return env


class Gate:
    """Parent side of the checkpoint rendezvous."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def spec(self, actions: Mapping[str, Mapping[str, Any]]) -> str:
        return json.dumps(
            {"gate_dir": str(self.directory), "actions": dict(actions)},
            sort_keys=True,
        )

    def reached(self, checkpoint: str) -> bool:
        return fault_injection.reached_marker(self.directory, checkpoint).exists()

    def wait_reached(
        self, checkpoint: str, timeout: float = DEFAULT_TIMEOUT
    ) -> dict[str, Any]:
        """Block until a child announces the checkpoint; return its context."""
        marker = fault_injection.reached_marker(self.directory, checkpoint)
        deadline = time.monotonic() + timeout
        while not marker.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"checkpoint {checkpoint} was never reached")
            time.sleep(_POLL_SECONDS)
        return json.loads(marker.read_text(encoding="utf-8"))

    def release(self, checkpoint: str) -> None:
        fault_injection.release_marker(self.directory, checkpoint).write_text(
            "release", encoding="utf-8"
        )


class ManagedProcess:
    """A child process that is always terminated and reaped."""

    def __init__(self, popen: subprocess.Popen, label: str):
        self._popen = popen
        self.label = label
        self.stdout = ""
        self.stderr = ""

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def returncode(self) -> int | None:
        return self._popen.returncode

    def poll(self) -> int | None:
        return self._popen.poll()

    def alive(self) -> bool:
        return self._popen.poll() is None

    def send(self, sig: int) -> None:
        if self.alive():
            self._popen.send_signal(sig)

    def wait(self, timeout: float = DEFAULT_TIMEOUT) -> int:
        """Wait for exit, capturing output. Kills and reports safely on timeout."""
        try:
            out, err = self._popen.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._popen.kill()
            out, err = self._popen.communicate(timeout=timeout)
            self.stdout, self.stderr = out or "", err or ""
            raise TimeoutError(
                f"{self.label} did not exit within {timeout}s; "
                f"killed. {self.diagnostics()}"
            )
        self.stdout, self.stderr = out or "", err or ""
        return self._popen.returncode

    def diagnostics(self, limit: int = 400) -> str:
        """Safe, truncated diagnostics. Never prints the environment or a path."""
        return (
            f"[{self.label}] exit={self.returncode} "
            f"stdout={self.stdout[:limit]!r} stderr={self.stderr[:limit]!r}"
        )

    def json_stdout(self) -> dict[str, Any]:
        return json.loads(self.stdout)

    def close(self) -> None:
        """Terminate, then kill, then reap. Safe to call repeatedly."""
        if self._popen.poll() is None:
            self._popen.terminate()
            try:
                self._popen.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                self._popen.kill()
                try:
                    self._popen.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        if self._popen.stdout is not None:
            self._popen.stdout.close()
        if self._popen.stderr is not None:
            self._popen.stderr.close()


@contextmanager
def managed(popen: subprocess.Popen, label: str):
    process = ManagedProcess(popen, label)
    try:
        yield process
    finally:
        process.close()


def spawn(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    label: str,
) -> subprocess.Popen:
    return subprocess.Popen(
        list(argv),
        cwd=str(REPO_ROOT),
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# --- worker -------------------------------------------------------------------


def worker_argv(
    *,
    db_path: Path,
    registry_path: Path,
    persist_dir: Path,
    worker_id: str,
    job_id: str | None = None,
    faults: str | None = None,
    lease_policy: Mapping[str, Any] | None = None,
    retry_policy: Mapping[str, Any] | None = None,
    now: str | None = None,
    use_cli: bool = False,
) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "tests.helpers.worker_child",
        "--db-path",
        str(db_path),
        "--registry-path",
        str(registry_path),
        "--persist-dir",
        str(persist_dir),
        "--worker-id",
        worker_id,
    ]
    if job_id:
        argv += ["--job-id", job_id]
    if faults:
        argv += ["--faults", faults]
    if lease_policy:
        argv += ["--lease-policy", json.dumps(lease_policy, sort_keys=True)]
    if retry_policy:
        argv += ["--retry-policy", json.dumps(retry_policy, sort_keys=True)]
    if now:
        argv += ["--now", now]
    if use_cli:
        argv += ["--use-cli"]
    return argv


@contextmanager
def worker_process(**kwargs):
    """Start a one-shot worker child. Always reaped."""
    label = f"worker:{kwargs.get('worker_id', 'unknown')}"
    env = child_env(
        db_path=kwargs["db_path"],
        registry_path=kwargs["registry_path"],
    )
    with managed(spawn(worker_argv(**kwargs), env=env, label=label), label) as p:
        yield p


def run_worker(timeout: float = DEFAULT_TIMEOUT, **kwargs) -> ManagedProcess:
    """Run one worker child to completion and return the finished process."""
    with worker_process(**kwargs) as process:
        process.wait(timeout=timeout)
        return process


# --- API ----------------------------------------------------------------------


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ApiProcess:
    """A real uvicorn process bound to an ephemeral localhost port."""

    def __init__(self, process: ManagedProcess, port: int):
        self.process = process
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"

    def wait_until_live(self, timeout: float = 45.0) -> None:
        import urllib.error
        import urllib.request

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.process.alive():
                raise RuntimeError(
                    f"api process exited before becoming live. "
                    f"{self.process.diagnostics()}"
                )
            try:
                with urllib.request.urlopen(
                    f"{self.base_url}/api/health", timeout=2
                ) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                time.sleep(_POLL_SECONDS * 5)
        raise TimeoutError("api process never became live")

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, Any]:
        """One bounded localhost HTTP request. Returns (status, parsed body)."""
        import urllib.error
        import urllib.request

        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
                return response.status, (json.loads(payload) if payload else None)
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8")
            return exc.code, (json.loads(payload) if payload else None)


@contextmanager
def api_process(
    *,
    db_path: Path,
    registry_path: Path,
    persist_dir: Path,
    faults: str | None = None,
    port: int | None = None,
    wait: bool = True,
):
    """Start a real API process against the given durable state. Always reaped."""
    chosen = port or free_port()
    argv = [
        sys.executable,
        "-m",
        "tests.helpers.api_child",
        "--host",
        "127.0.0.1",
        "--port",
        str(chosen),
        "--persist-dir",
        str(persist_dir),
    ]
    if faults:
        argv += ["--faults", faults]
    env = child_env(
        db_path=db_path,
        registry_path=registry_path,
        auth_secret=TEST_AUTH_SECRET,
    )
    label = f"api:{chosen}"
    with managed(spawn(argv, env=env, label=label), label) as process:
        api = ApiProcess(process, chosen)
        if wait:
            api.wait_until_live()
        yield api


# --- SQLite lock holder -------------------------------------------------------


@contextmanager
def lock_holder(*, db_path: Path, hold_seconds: float, ready_file: Path):
    """Hold a deliberate SQLite write transaction for a bounded interval."""
    argv = [
        sys.executable,
        "-m",
        "tests.helpers.lock_holder",
        "--db-path",
        str(db_path),
        "--hold-seconds",
        str(hold_seconds),
        "--ready-file",
        str(ready_file),
    ]
    env = child_env(db_path=db_path)
    label = "lock-holder"
    with managed(spawn(argv, env=env, label=label), label) as process:
        deadline = time.monotonic() + 30.0
        while not ready_file.exists():
            if not process.alive():
                raise RuntimeError(
                    f"lock holder exited early. {process.diagnostics()}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError("lock holder never acquired the write lock")
            time.sleep(_POLL_SECONDS)
        yield process


def kill_now(process: ManagedProcess) -> None:
    """SIGKILL a child. Never caught, never handled, no cleanup runs."""
    process.send(signal.SIGKILL)
