"""Official-source acquisition for the real-filing benchmark corpus.

The ONLY module in this repository that can open a network socket on purpose,
and it refuses to do so unless a caller explicitly asks. Everything else about
the benchmark — schema validation, corpus build, annotation, evaluation — runs
offline, which is what keeps the merge-blocking CI check credential-free and
network-free.

Safety model, in the order the checks run
-----------------------------------------
1. **Network is off by default.** ``allow_network`` defaults to False and a
   disabled fetcher raises ``NetworkDisabled`` before resolving anything. There
   is no environment variable, config file, or implicit condition that turns it
   on; a human passes a flag.
2. **A descriptive user agent is mandatory.** SEC's access policy requires one
   that identifies the requester. It is supplied externally (``SEC_USER_AGENT``)
   and validated against both a shape rule and a placeholder blocklist, so
   ``your-email@example.com`` cannot be shipped as if it were an identity.
3. **Official hosts only, by exact hostname equality.** Suffix matching would
   admit ``www.sec.gov.example.test``; this module compares hostnames for
   equality and requires https.
4. **Conservative pacing.** A configurable minimum interval between requests,
   enforced through injected clock/sleep callables so tests exercise it
   without waiting.
5. **Bounded transport retry only.** Retries cover transport faults and an
   explicit allowlist of transient HTTP statuses. 403/404 and every other
   response fail immediately. This policy is deliberately separate from the
   detection-job retry policy: reusing that one would couple an external
   fetch's backoff to workflow-lifecycle semantics that have nothing to do
   with it.
6. **Retry-After is honored** when present, capped so a hostile or mistaken
   header cannot hang the run.
7. **Content-Encoding is decoded before anything is hashed.** The request
   advertises gzip and SEC honors it, but urllib does not decode transfer
   encodings. Hashing the compressed stream would freeze a digest of a gzip
   container rather than of the filing — and gzip headers carry an mtime and
   OS byte, so that digest would not even be reproducible. Decompression is
   bounded on the DECOMPRESSED size, and an unsupported or malformed encoding
   is a coded refusal rather than compressed bytes handed on as content.
8. **Atomic writes, verified content.** Bytes land in a temp file in the target
   directory, are fsynced, sha256-verified, and only then ``os.replace``-d into
   place. A verified cached file is reused instead of re-fetched; a cached file
   whose digest does not match is a refusal, never a silent overwrite.

Output hygiene: this module returns structured outcomes and never prints. Its
CLI prints counts, corpus-relative paths, and digests — never filing content
and never an absolute local path.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import real_filing_benchmark as rfb

ACQUISITION_METADATA_VERSION = "real-filing-benchmark.acquisition.v1"

# --- Policy -------------------------------------------------------------------

DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = (2.0, 5.0)
MAX_RETRY_AFTER_SECONDS = 120.0
# A 10-K primary document is large but bounded. A response bigger than this is
# refused rather than streamed to disk indefinitely.
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024

# Transport-level and explicitly transient response conditions ONLY. Anything
# absent here (403 Forbidden, 404 Not Found, 400, 410, ...) is a source failure
# a human must look at, not something to retry into.
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})

ACQUISITION_RETRY_POLICY = {
    "policy_id": "real_filing_acquisition_transport_retry",
    "policy_version": 1,
    "max_attempts": DEFAULT_MAX_ATTEMPTS,
    "backoff_seconds": list(DEFAULT_BACKOFF_SECONDS),
    "transient_http_statuses": sorted(TRANSIENT_HTTP_STATUSES),
    "max_retry_after_seconds": MAX_RETRY_AFTER_SECONDS,
    "scope": (
        "HTTP transport for benchmark corpus acquisition only. Deliberately "
        "independent of detection_job_retry.POLICY: workflow retry semantics "
        "and external-fetch backoff are different problems and must not share "
        "a knob."
    ),
}

# --- Stable outcome / failure codes ------------------------------------------

OUTCOME_DOWNLOADED = "downloaded"
OUTCOME_CACHED = "cached"
OUTCOME_FAILED = "failed"

FAILURE_NETWORK_DISABLED = "network_disabled"
FAILURE_INVALID_USER_AGENT = "invalid_user_agent"
FAILURE_NON_OFFICIAL_SOURCE = "non_official_source"
FAILURE_TRANSPORT = "transport_failure"
FAILURE_HTTP_STATUS = "http_status_failure"
FAILURE_CHECKSUM_MISMATCH = "checksum_mismatch"
FAILURE_CACHED_CONTENT_MISMATCH = "cached_content_mismatch"
FAILURE_PLACEHOLDER_HASH = "expected_hash_is_placeholder"
FAILURE_RESPONSE_TOO_LARGE = "response_too_large"
FAILURE_EMPTY_RESPONSE = "empty_response"
FAILURE_UNSUPPORTED_ENCODING = "unsupported_content_encoding"
FAILURE_MALFORMED_ENCODING = "malformed_content_encoding"


class AcquisitionError(rfb.BenchmarkError):
    """A bounded, code-carrying acquisition failure. Never carries content."""


class NetworkDisabled(AcquisitionError):
    """Network access was not explicitly enabled by the operator."""


class UserAgentRejected(AcquisitionError):
    """The SEC user agent is missing, malformed, or a placeholder."""


class NonOfficialSource(AcquisitionError):
    """A URL that is not an official SEC endpoint."""


class ChecksumMismatch(AcquisitionError):
    """Downloaded or cached bytes do not match the manifest's digest."""


# --- User agent ---------------------------------------------------------------

# SEC asks for a descriptive agent identifying the requester with a contact
# address. Requiring an address is what makes the value descriptive rather than
# a random token.
_CONTACT_RE = re.compile(r"[^\s@]+@[^\s@.]+\.[^\s@]{2,}")
MIN_USER_AGENT_CHARS = 16
MAX_USER_AGENT_CHARS = 200

# Placeholder fragments that appear in copy-pasted templates. Case-insensitive
# substring match: shipping any of these would tell SEC nothing about who is
# making the request, which is exactly what the requirement exists to prevent.
_PLACEHOLDER_FRAGMENTS = (
    "example.com",
    "example.org",
    "example.net",
    "your-email",
    "your_email",
    "youremail",
    "yourname",
    "your-name",
    "changeme",
    "change-me",
    "replace-me",
    "replaceme",
    "<",
    ">",
    "todo",
    "xxx@",
    "foo@bar",
    "test@test",
    "user@user",
    "sec_user_agent",
    "sample@",
    "someone@somewhere",
    "email@domain",
)


def validate_user_agent(value: Any) -> str:
    """Return a validated descriptive SEC user agent, or raise.

    Deliberately strict. A polite-but-anonymous agent string is a way of not
    identifying yourself to a public data source that asks you to.
    """
    if not isinstance(value, str) or not value.strip():
        raise UserAgentRejected(
            FAILURE_INVALID_USER_AGENT,
            "SEC_USER_AGENT is not set. Supply a descriptive value that "
            "identifies you, for example: "
            "'Jane Doe Research jane.doe@university.edu'",
        )
    agent = " ".join(value.split())
    if len(agent) < MIN_USER_AGENT_CHARS or len(agent) > MAX_USER_AGENT_CHARS:
        raise UserAgentRejected(
            FAILURE_INVALID_USER_AGENT,
            f"SEC_USER_AGENT must be between {MIN_USER_AGENT_CHARS} and "
            f"{MAX_USER_AGENT_CHARS} characters",
        )
    lowered = agent.lower()
    for fragment in _PLACEHOLDER_FRAGMENTS:
        if fragment in lowered:
            raise UserAgentRejected(
                FAILURE_INVALID_USER_AGENT,
                "SEC_USER_AGENT looks like an unfilled placeholder "
                f"(contains {fragment!r}). Supply a real contact address.",
            )
    if not _CONTACT_RE.search(agent):
        raise UserAgentRejected(
            FAILURE_INVALID_USER_AGENT,
            "SEC_USER_AGENT must include a contact email address so requests "
            "identify the requester",
        )
    # A bare address identifies a mailbox, not a requester.
    if len(agent.split()) < 2:
        raise UserAgentRejected(
            FAILURE_INVALID_USER_AGENT,
            "SEC_USER_AGENT must name the requester as well as a contact "
            "address, for example 'Jane Doe Research jane.doe@university.edu'",
        )
    return agent


def resolve_user_agent(explicit: str | None = None) -> str:
    """Explicit value, else SEC_USER_AGENT from the environment."""
    import config

    candidate = explicit if explicit is not None else (
        os.environ.get("SEC_USER_AGENT") or config.SEC_USER_AGENT
    )
    return validate_user_agent(candidate)


# --- Official source enforcement ----------------------------------------------


def require_official_url(url: Any) -> str:
    """Accept only https URLs whose hostname EQUALS an official SEC host."""
    if not isinstance(url, str) or not url.strip():
        raise NonOfficialSource(
            FAILURE_NON_OFFICIAL_SOURCE, "source URL is missing"
        )
    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        raise NonOfficialSource(
            FAILURE_NON_OFFICIAL_SOURCE,
            f"source URL must use https, got scheme {parsed.scheme!r}",
        )
    hostname = (parsed.hostname or "").lower()
    if hostname not in rfb.OFFICIAL_SEC_HOSTS:
        raise NonOfficialSource(
            FAILURE_NON_OFFICIAL_SOURCE,
            f"host {hostname!r} is not an official SEC endpoint "
            f"(allowed: {list(rfb.OFFICIAL_SEC_HOSTS)}). Mirrors, caches, and "
            "third-party copies are not official sources.",
        )
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise NonOfficialSource(
            FAILURE_NON_OFFICIAL_SOURCE,
            "source URL must not carry credentials or a non-default port",
        )
    return url.strip()


# --- Transport ----------------------------------------------------------------


@dataclass
class Response:
    """A minimal, injectable HTTP response: status, headers, body.

    ``body`` is always the DECODED entity body. ``Content-Encoding`` is left in
    ``headers`` verbatim as a record of how the bytes arrived, but it has
    already been applied — see ``decode_content_encoding``.
    """

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


# The request advertises `Accept-Encoding: gzip, deflate`, and SEC honors it.
# urllib does NOT decode transfer encodings, so a transport that returned
# `raw.read()` verbatim would hand back a compressed stream. That is not a
# cosmetic bug: `acquire_side` hashes exactly these bytes, so every frozen
# digest would be the sha256 of a gzip container rather than of the filing —
# and gzip headers carry an mtime and OS byte, so the digest would not even be
# stable across two downloads of identical content. Decoding here keeps
# "expected_sha256 is the digest of the filing" true.
_SUPPORTED_CONTENT_ENCODINGS = frozenset({"", "identity", "gzip", "x-gzip", "deflate"})


def _decompress_bounded(data: bytes, wbits: int, limit: int) -> bytes:
    """Inflate at most ``limit`` bytes, then stop.

    Bounded on the DECOMPRESSED size, because the caller's byte cap protects
    only against a large transfer, not against a small one that expands.
    """
    decompressor = zlib.decompressobj(wbits)
    out = decompressor.decompress(data, limit)
    # Anything still pending means the payload exceeds the bound. Return the
    # oversized prefix so the caller's size check refuses it by the normal
    # path instead of inventing a second refusal.
    if decompressor.unconsumed_tail:
        return out + b"\x00"
    return out


def decode_content_encoding(body: bytes, encoding: str | None) -> bytes:
    """Apply Content-Encoding so callers always see the real entity body."""
    token = (encoding or "").strip().lower()
    if token not in _SUPPORTED_CONTENT_ENCODINGS:
        raise AcquisitionError(
            FAILURE_UNSUPPORTED_ENCODING,
            f"official source returned an unsupported Content-Encoding "
            f"({token!r}); bytes were not decoded and nothing was hashed",
        )
    if not body or token in ("", "identity"):
        return body
    limit = MAX_DOCUMENT_BYTES + 1
    try:
        if token in ("gzip", "x-gzip"):
            return _decompress_bounded(body, 16 + zlib.MAX_WBITS, limit)
        try:
            return _decompress_bounded(body, zlib.MAX_WBITS, limit)
        except zlib.error:
            # Some servers send raw deflate under the same token.
            return _decompress_bounded(body, -zlib.MAX_WBITS, limit)
    except zlib.error as exc:
        raise AcquisitionError(
            FAILURE_MALFORMED_ENCODING,
            f"official source returned a malformed {token} stream "
            f"({type(exc).__name__})",
        ) from exc


def urllib_transport(
    url: str, *, headers: dict[str, str], timeout: float
) -> Response:
    """The real transport. Replaced wholesale in tests — no monkeypatching of
    urllib internals, so a test can never accidentally reach the network."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as raw:
            response_headers = {key: value for key, value in raw.headers.items()}
            body = raw.read(MAX_DOCUMENT_BYTES + 1)
            return Response(
                status=getattr(raw, "status", 200) or 200,
                headers=response_headers,
                body=decode_content_encoding(
                    body, response_headers.get("Content-Encoding")
                ),
            )
    except urllib.error.HTTPError as exc:  # a response, not a transport fault
        error_headers = {key: value for key, value in (exc.headers or {}).items()}
        try:
            body = decode_content_encoding(
                exc.read(MAX_DOCUMENT_BYTES + 1),
                error_headers.get("Content-Encoding"),
            )
        except Exception:  # noqa: BLE001 - body is diagnostic only
            body = b""
        return Response(
            status=exc.code,
            headers=error_headers,
            body=body,
        )


@dataclass
class Fetcher:
    """Rate-limited, bounded-retry fetcher over an injectable transport.

    ``clock`` and ``sleep`` are injected so pacing, backoff, and Retry-After
    are testable deterministically without a test ever sleeping for real.
    """

    user_agent: str
    allow_network: bool = False
    transport: Callable[..., Response] = urllib_transport
    min_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_seconds: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _last_request_at: float | None = field(default=None, init=False, repr=False)
    request_count: int = field(default=0, init=False)
    slept_seconds: list[float] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.user_agent = validate_user_agent(self.user_agent)
        if self.max_attempts < 1:
            raise AcquisitionError(
                "invalid_retry_policy", "max_attempts must be at least 1"
            )

    # -- pacing
    def _pace(self) -> None:
        if self._last_request_at is None:
            self._last_request_at = self.clock()
            return
        elapsed = self.clock() - self._last_request_at
        remaining = self.min_interval_seconds - elapsed
        if remaining > 0:
            self._nap(remaining)
        self._last_request_at = self.clock()

    def _nap(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.slept_seconds.append(round(seconds, 6))
        self.sleep(seconds)

    def _retry_after(self, response: Response) -> float | None:
        """Honor Retry-After (delta-seconds form), capped."""
        raw = response.header("Retry-After")
        if raw is None:
            return None
        try:
            seconds = float(str(raw).strip())
        except ValueError:
            # HTTP-date form: not parsed here on purpose — falling back to the
            # configured backoff is safer than trusting a clock comparison
            # against a remote date.
            return None
        if seconds < 0:
            return None
        return min(seconds, MAX_RETRY_AFTER_SECONDS)

    def _backoff_for(self, attempt: int) -> float:
        index = min(attempt - 1, len(self.backoff_seconds) - 1)
        return self.backoff_seconds[index] if self.backoff_seconds else 0.0

    def get(self, url: str) -> Response:
        """One rate-limited GET with bounded transport retry.

        Raises AcquisitionError with a stable code; the message never contains
        response bodies, so a server error page cannot leak into output.
        """
        if not self.allow_network:
            raise NetworkDisabled(
                FAILURE_NETWORK_DISABLED,
                "network access is disabled. Benchmark acquisition reaches an "
                "external public service and must be requested explicitly "
                "(--allow-network).",
            )
        official = require_official_url(url)
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Host": urlparse(official).hostname or "",
        }

        last_error: AcquisitionError | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._pace()
            self.request_count += 1
            try:
                response = self.transport(
                    official, headers=headers, timeout=self.timeout_seconds
                )
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
                last_error = AcquisitionError(
                    FAILURE_TRANSPORT,
                    f"transport failure contacting the official source "
                    f"({type(exc).__name__})",
                )
                if attempt < self.max_attempts:
                    self._nap(self._backoff_for(attempt))
                    continue
                raise last_error from exc

            if 200 <= response.status < 300:
                return response

            if (
                response.status in TRANSIENT_HTTP_STATUSES
                and attempt < self.max_attempts
            ):
                delay = self._retry_after(response)
                self._nap(delay if delay is not None else self._backoff_for(attempt))
                last_error = AcquisitionError(
                    FAILURE_HTTP_STATUS,
                    f"official source returned HTTP {response.status}",
                )
                continue

            raise AcquisitionError(
                FAILURE_HTTP_STATUS,
                f"official source returned HTTP {response.status}",
            )

        raise last_error or AcquisitionError(
            FAILURE_TRANSPORT, "acquisition failed without a recorded cause"
        )


# --- Atomic, verified download ------------------------------------------------


def _write_atomic(target: Path, data: bytes) -> None:
    """Temp file in the target directory -> fsync -> os.replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".part"
    )
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def acquire_side(
    *,
    fetcher: Fetcher,
    layout: rfb.CorpusLayout,
    pair_id: str,
    side: str,
    side_payload: dict[str, Any],
    accept_unverified_hash: bool = False,
    now: Callable[[], str] = rfb.utc_now_iso,
) -> dict[str, Any]:
    """Acquire one filing, verify it, and record local acquisition metadata.

    Returns a structured outcome. Never raises for an expected failure — the
    caller aggregates outcomes so one bad source does not abort a run and hide
    what else was or was not verified.

    ``accept_unverified_hash`` supports the discovery run against a manifest
    that is still ``proposed``: bytes are downloaded and their digest recorded
    locally so a human can freeze it, but the outcome is explicitly NOT
    verified and the CLI exits nonzero.
    """
    target = layout.source_file(pair_id, side, side_payload["primary_document"])
    expected = side_payload["expected_sha256"]
    placeholder = expected == rfb.PLACEHOLDER_SHA256
    url = side_payload["official_source_url"]

    base = {
        "pair_id": pair_id,
        "side": side,
        "accession_number": side_payload["accession_number"],
        "expected_sha256": expected,
        "expected_sha256_is_placeholder": placeholder,
        "source_path": layout.relative(target),
    }

    if placeholder and not accept_unverified_hash:
        return {
            **base,
            "outcome": OUTCOME_FAILED,
            "verified": False,
            "failure_code": FAILURE_PLACEHOLDER_HASH,
            "detail": (
                "the manifest carries a placeholder digest for this filing, so "
                "downloaded bytes cannot be verified. Run with --record-hashes "
                "to capture observed digests for human review."
            ),
        }

    # Verified cache reuse: an already-correct local file is never re-fetched.
    if target.exists():
        observed = rfb.sha256_file(target)
        if not placeholder and observed == expected:
            return {
                **base,
                "outcome": OUTCOME_CACHED,
                "verified": True,
                "observed_sha256": observed,
                "byte_count": target.stat().st_size,
            }
        if not placeholder:
            # Refuse silent replacement. A cached file that disagrees with the
            # frozen manifest is a fact a human has to look at; overwriting it
            # would destroy the evidence that they disagreed.
            return {
                **base,
                "outcome": OUTCOME_FAILED,
                "verified": False,
                "observed_sha256": observed,
                "failure_code": FAILURE_CACHED_CONTENT_MISMATCH,
                "detail": (
                    "a cached local file exists whose digest does not match "
                    "the frozen manifest. It is NOT overwritten. Remove the "
                    "local file deliberately if the manifest is correct."
                ),
            }

    try:
        response = fetcher.get(url)
    except AcquisitionError as exc:
        return {
            **base,
            "outcome": OUTCOME_FAILED,
            "verified": False,
            "failure_code": exc.code,
            "detail": exc.message,
        }

    body = response.body or b""
    if not body:
        return {
            **base,
            "outcome": OUTCOME_FAILED,
            "verified": False,
            "failure_code": FAILURE_EMPTY_RESPONSE,
            "detail": "the official source returned an empty document",
        }
    if len(body) > MAX_DOCUMENT_BYTES:
        return {
            **base,
            "outcome": OUTCOME_FAILED,
            "verified": False,
            "failure_code": FAILURE_RESPONSE_TOO_LARGE,
            "detail": (
                f"document exceeds the {MAX_DOCUMENT_BYTES}-byte bound and was "
                "not written"
            ),
        }

    observed = hashlib.sha256(body).hexdigest()
    if not placeholder and observed != expected:
        # Verify BEFORE writing: bytes that fail the frozen digest never reach
        # the corpus directory at all.
        return {
            **base,
            "outcome": OUTCOME_FAILED,
            "verified": False,
            "observed_sha256": observed,
            "failure_code": FAILURE_CHECKSUM_MISMATCH,
            "detail": (
                "downloaded content does not match the frozen manifest digest; "
                "nothing was written"
            ),
        }

    _write_atomic(target, body)
    metadata = {
        "metadata_version": ACQUISITION_METADATA_VERSION,
        "pair_id": pair_id,
        "side": side,
        "accession_number": side_payload["accession_number"],
        "official_source_url": url,
        "primary_document": side_payload["primary_document"],
        "acquired_at": now(),
        "byte_count": len(body),
        "observed_sha256": observed,
        "expected_sha256": expected,
        "verified": not placeholder,
        "content_type": response.header("Content-Type"),
        "user_agent_supplied": True,
    }
    rfb.write_json_atomic(
        layout.acquisition_metadata_path(pair_id, side), metadata
    )

    return {
        **base,
        "outcome": OUTCOME_DOWNLOADED,
        "verified": not placeholder,
        "observed_sha256": observed,
        "byte_count": len(body),
        **(
            {
                "failure_code": FAILURE_PLACEHOLDER_HASH,
                "detail": (
                    "bytes were downloaded and their digest recorded locally, "
                    "but the manifest has no digest to verify against. This "
                    "filing is NOT source-verified."
                ),
            }
            if placeholder
            else {}
        ),
    }


def acquire_manifest(
    manifest: dict[str, Any],
    *,
    fetcher: Fetcher,
    layout: rfb.CorpusLayout,
    pair_ids: list[str] | None = None,
    accept_unverified_hash: bool = False,
) -> dict[str, Any]:
    """Acquire every requested pair; return a structured, path-safe report."""
    pairs = rfb.manifest_pairs(manifest)
    if pair_ids:
        wanted = set(pair_ids)
        unknown = sorted(wanted - {pair["pair_id"] for pair in pairs})
        if unknown:
            raise AcquisitionError(
                "unknown_pair_id",
                f"manifest has no pairs {unknown}",
            )
        pairs = [pair for pair in pairs if pair["pair_id"] in wanted]

    results: list[dict[str, Any]] = []
    for pair in pairs:
        for side, payload in rfb.pair_sides(pair):
            results.append(
                acquire_side(
                    fetcher=fetcher,
                    layout=layout,
                    pair_id=pair["pair_id"],
                    side=side,
                    side_payload=payload,
                    accept_unverified_hash=accept_unverified_hash,
                )
            )

    verified = [item for item in results if item["verified"]]
    failures = [item for item in results if item["outcome"] == OUTCOME_FAILED]
    return {
        "benchmark_id": manifest["benchmark_id"],
        "manifest_status": manifest["status"],
        "requested_filings": len(results),
        "verified_filings": len(verified),
        "downloaded": sum(
            1 for item in results if item["outcome"] == OUTCOME_DOWNLOADED
        ),
        "reused_verified_cache": sum(
            1 for item in results if item["outcome"] == OUTCOME_CACHED
        ),
        "failed": len(failures),
        "request_count": fetcher.request_count,
        "retry_policy": ACQUISITION_RETRY_POLICY,
        "all_verified": bool(results) and len(verified) == len(results),
        "filings": results,
    }
