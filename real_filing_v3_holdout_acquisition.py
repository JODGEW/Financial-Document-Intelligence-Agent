"""Source verification for the v3 extraction holdout — acquisition only.

Stage 3.5, the step after the metadata-only v3 freeze: download the exact
twenty primary 10-K documents the committed v3 manifest froze, checksum them,
and advance that manifest exactly one step
(``holdout_frozen_metadata_only`` -> ``source_verified``). Nothing downstream of
bytes-on-disk happens here: no extraction, no unit segmentation, no ingestion,
no Chroma, no comparison, no packet, no label, no metric, no sign-off. A test
pins that at the import graph.

Why a second acquisition module and not a parameter
---------------------------------------------------
``real_filing_holdout_acquisition`` performed this same step for
``real_filing_holdout_v1`` and is now a frozen historical artifact, pinned
byte-for-byte by its own suite. It also gates on a *different* exclusion schema:
the first holdout froze one development corpus under ``development_exclusions``,
while the v3 holdout excludes BOTH prior corpora under
``prior_corpus_exclusions.sources``. Bending the older module to cover both
would edit a frozen artifact to serve a newer corpus — exactly the move the
holdout discipline exists to prevent.

What is NOT duplicated: transport, pacing, bounded retry, ``Retry-After``
handling, user-agent validation, the official-host allowlist, Content-Encoding
decoding, and atomic writes all come from ``real_filing_acquisition``. There is
no second transport policy, no second retry policy, and no second hashing rule
in this repository.

Trust-on-first-acquisition, stated plainly
------------------------------------------
The frozen v3 manifest carries ``expected_sha256: null`` on every side, because
metadata-only meant exactly that. This module therefore cannot *verify against*
a prior digest on the first run; what it can do — and all it claims — is:

1. fetch each body from the one canonical official EDGAR URL derived from the
   frozen CIK/accession/primary-document fields (never from a search, a mirror,
   an index page, an exhibit, or a guess),
2. decode the transport's Content-Encoding so the digest is over the filing,
   not a gzip container,
3. hash the decoded bytes, write them atomically, re-read the written file and
   re-hash it, and accept the digest only when the two agree,
4. freeze those digests into the advanced manifest so every LATER read is a
   real verification against a committed value.

Source verification is not parser validation. It establishes only that specific
official bytes were obtained and can be reproduced. Whether those filings
contain an extractable Item 1A section is unknown here and stays unknown until
a separate blind extraction run says so.

What this module refuses to do
------------------------------
- Replace, drop, reorder, or re-select a frozen pair for any reason. A source
  that cannot be acquired fails the whole phase; the manifest stays
  metadata-only. "The URL failed", "the document is odd", "Item 1A may be
  absent", "the body is huge" are all non-reasons.
- Update any frozen identity field from observed content. Identity flows one
  way: manifest -> URL -> bytes.
- Advance the manifest while any of the twenty sides is unverified.
- Follow a redirect that lands anywhere other than this exact side's canonical
  URL. ``urllib`` follows redirects silently, and with no frozen digest on the
  first acquisition a redirect to a look-alike host, a different accession, or
  an index page would be invisible.
- Emit filing content, local absolute paths, credentials, the SEC user agent,
  raw response headers, or transport exception text in any report or error.
"""

from __future__ import annotations

import copy
import hashlib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse, urlsplit

import real_filing_acquisition as rfa
import real_filing_benchmark as rfb
import real_filing_v3_holdout as rfv3

# --- Protocol identity ----------------------------------------------------------

V3_SOURCE_ACQUISITION_PROTOCOL_VERSION = "real-filing-v3-source-acquisition.v1"
V3_SOURCE_VERIFICATION_REPORT_VERSION = (
    "real-filing-v3-holdout.source-verification.v1"
)
V3_ACQUISITION_METADATA_VERSION = "real-filing-v3-holdout.acquisition.v1"

#: Local workspace convention, recorded in the report so a reader can see the
#: layout without a local absolute path ever being committed.
LOCAL_PATH_CONVENTION = "sources/{pair_id}/{side}/{primary_document}"

#: The one host that may serve a filing body. ``data.sec.gov`` is an official
#: SEC host and passes ``require_official_url``, but it serves metadata, not
#: filing bodies; naming it here would widen the body surface for no reason.
BODY_HOST = rfb.SEC_ARCHIVES_HOST
BODY_PATH_PREFIX = "/Archives/edgar/data/"

#: Entity-body semantics, frozen into the protocol so the meaning of a digest
#: is committed alongside the digest.
SOURCE_BYTE_SEMANTICS = (
    "sha256 over the exact response entity bytes after HTTP Content-Encoding "
    "is applied, and before any text decoding, Unicode normalization, newline "
    "normalization, HTML parsing, or reformatting. The persisted file contains "
    "exactly the bytes that were hashed, and its digest is confirmed by "
    "re-reading it in binary mode."
)


def source_acquisition_protocol() -> dict[str, Any]:
    """The predeclared, bounded acquisition protocol.

    Hashed by :func:`source_acquisition_protocol_hash` and recorded in the
    committed report, so the rules a digest was obtained under are frozen
    beside the digest itself. Contains no credential, no path, and no value
    that varies between runs.
    """
    return {
        "protocol_version": V3_SOURCE_ACQUISITION_PROTOCOL_VERSION,
        "benchmark_id": rfv3.V3_HOLDOUT_BENCHMARK_ID,
        "accepted_manifest_status": rfv3.STATUS_HOLDOUT_FROZEN_METADATA_ONLY,
        "resulting_manifest_status": rfb.STATUS_SOURCE_VERIFIED,
        "url_construction": (
            "https://{host}/Archives/edgar/data/{cik_without_leading_zeroes}/"
            "{accession_without_hyphens}/{primary_document}, derived from "
            "frozen manifest fields only"
        ),
        "url_host_allowlist": [BODY_HOST],
        "url_scheme": "https",
        "url_path_prefix": BODY_PATH_PREFIX,
        "url_query_permitted": False,
        "url_fragment_permitted": False,
        "redirect_policy": (
            "a redirect is followed only when its target is byte-identical to "
            "this side's canonical official URL; any other target is refused"
        ),
        "request_pacing_seconds": rfa.DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
        "request_timeout_seconds": rfa.DEFAULT_TIMEOUT_SECONDS,
        "retry_policy": rfa.ACQUISITION_RETRY_POLICY,
        "max_source_bytes": rfa.MAX_DOCUMENT_BYTES,
        "hash_algorithm": "sha256",
        "source_byte_semantics": SOURCE_BYTE_SEMANTICS,
        "local_path_convention": LOCAL_PATH_CONVENTION,
        "atomic_persistence": (
            "temp file in the destination directory -> fsync -> os.replace, "
            "then binary re-read and re-hash before the digest is accepted"
        ),
        "manifest_transition": (
            "all-or-nothing: the committed manifest advances only when all "
            "twenty sides verify; a selected filing is never replaced"
        ),
        "runs_extraction": False,
        "runs_comparison": False,
        "creates_annotations": False,
        "creates_metrics": False,
    }


def source_acquisition_protocol_hash() -> str:
    return rfb.payload_hash(source_acquisition_protocol())


# --- Stable failure codes -------------------------------------------------------
# Codes beyond real_filing_acquisition's own set. Errors carry a pair id, side,
# accession, bounded reason code, and bounded hash identifiers — never filing
# text, response excerpts, the user agent, credentials, absolute paths, or raw
# exception text.

FAILURE_MANIFEST_STATUS_INVALID = "v3_source_manifest_status_invalid"
FAILURE_MANIFEST_HASH_DRIFT = "v3_source_manifest_hash_drift"
FAILURE_FROZEN_CODE_DRIFT = "v3_source_frozen_code_drift"
FAILURE_EXCLUSION_DRIFT = "v3_source_exclusion_drift"
FAILURE_URL_INVALID = "v3_source_url_invalid"
FAILURE_URL_IDENTITY_MISMATCH = "v3_source_url_identity_mismatch"
FAILURE_REDIRECT_FORBIDDEN = "v3_source_redirect_forbidden"
FAILURE_ACCESS_DENIED_RESPONSE = "v3_source_access_denied_response"
FAILURE_INCOMPLETE_RESPONSE = "v3_source_incomplete_response"
FAILURE_CONTENT_TYPE_INVALID = "v3_source_content_type_invalid"
FAILURE_LOCAL_REREAD_MISMATCH = "v3_source_local_reread_mismatch"
FAILURE_CACHE_UNVERIFIABLE = "v3_source_local_file_unverifiable"
FAILURE_DUPLICATE_IDENTITY = "v3_source_duplicate_identity"
FAILURE_NOT_FULLY_VERIFIED = "v3_source_verification_incomplete"
FAILURE_REPORT_MANIFEST_MISMATCH = "v3_source_report_manifest_mismatch"

VERIFICATION_OUTCOME_VERIFIED = "source_verified"
VERIFICATION_OUTCOME_FAILED = "failed"


class V3HoldoutAcquisitionError(rfb.BenchmarkError):
    """A bounded, code-carrying v3-holdout-acquisition failure."""


# --- Immutability gate ----------------------------------------------------------


def verify_frozen_identity(
    manifest: Mapping[str, Any], *, repo_root: str | Path | None = None
) -> None:
    """Refuse acquisition if any frozen identity has drifted.

    ``validate_v3_holdout_manifest`` already pins the schema, benchmark id,
    pair count, pair ordering, strata, form, fiscal years, protocol hash, and
    every contract-version field. This gate adds the two facts only the working
    tree can attest: the pinned source files on disk still hash to their frozen
    digests (read as bytes, never imported), and the frozen exclusion sets
    still equal what the committed prior manifests derive to.
    """
    rfv3.validate_v3_holdout_manifest(manifest)

    if manifest["status"] not in (
        rfv3.STATUS_HOLDOUT_FROZEN_METADATA_ONLY,
        rfb.STATUS_SOURCE_VERIFIED,
    ):
        raise V3HoldoutAcquisitionError(
            FAILURE_MANIFEST_STATUS_INVALID,
            f"v3 holdout manifest status {manifest['status']!r} is beyond "
            "source verification; acquisition has nothing to do",
        )

    try:
        rfv3.verify_frozen_code_identities(manifest, repo_root)
    except rfb.BenchmarkError as exc:
        raise V3HoldoutAcquisitionError(
            FAILURE_FROZEN_CODE_DRIFT,
            "a pinned source file no longer matches the digest the v3 holdout "
            "manifest froze. The parser, unit grammar, detector, workflow "
            "store, and evaluator may not change after the freeze; a "
            "deliberate change requires freezing a NEW holdout corpus. "
            f"({exc.code})",
        ) from None

    try:
        rfv3.verify_exclusion_provenance(manifest, repo_root)
    except rfb.BenchmarkError as exc:
        raise V3HoldoutAcquisitionError(
            FAILURE_EXCLUSION_DRIFT,
            "the manifest's frozen prior-corpus exclusions no longer match "
            "those derived from the committed prior manifests "
            f"({exc.code})",
        ) from None


def verify_manifest_hash_chain(manifest_path: str | Path) -> None:
    """A still-metadata-only manifest must be byte-identical to the file the
    committed selection report froze.

    The selection report records ``holdout_manifest_sha256`` over the exact
    bytes the freeze wrote. Until the transition, any difference means the
    freeze was hand-edited afterwards — a changed accession, CIK, or document
    name would still be schema-valid, so only the hash can catch it. Skipped
    when no selection report sits beside the manifest (synthetic library/test
    manifests) and after the transition, where the source-verification report
    pins the new bytes instead.
    """
    import json

    manifest_path = Path(manifest_path)
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise V3HoldoutAcquisitionError(
            "v3_source_manifest_unreadable",
            f"v3 holdout manifest could not be read ({type(exc).__name__})",
        ) from None
    if document.get("status") != rfv3.STATUS_HOLDOUT_FROZEN_METADATA_ONLY:
        return
    report_path = manifest_path.parent / "selection_report.json"
    if not report_path.exists():
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise V3HoldoutAcquisitionError(
            "v3_source_selection_report_unreadable",
            f"v3 selection report could not be read ({type(exc).__name__})",
        ) from None
    frozen = report.get("holdout_manifest_sha256")
    if frozen != rfb.sha256_file(manifest_path):
        raise V3HoldoutAcquisitionError(
            FAILURE_MANIFEST_HASH_DRIFT,
            "the metadata-only v3 holdout manifest no longer hashes to the "
            "value the committed selection report froze. A frozen identity "
            "has drifted; acquisition is refused rather than verifying an "
            "edited freeze.",
        )


# --- Official body URL ----------------------------------------------------------


def _reject(code: str, message: str) -> None:
    raise V3HoldoutAcquisitionError(code, message)


def v3_body_url(pair: Mapping[str, Any], side_payload: Mapping[str, Any]) -> str:
    """The one canonical official EDGAR URL for this exact frozen side.

    Every component is validated *before* the URL is built, and the built URL
    is then decomposed and required to round-trip back to those same frozen
    components. A URL that cannot be re-read as this side's identity is not
    this side's URL, whatever it looks like.
    """
    cik = pair.get("cik")
    accession = side_payload.get("accession_number")
    document = side_payload.get("primary_document")

    if not isinstance(cik, str) or not rfb._CIK_RE.match(cik):
        _reject(
            FAILURE_URL_INVALID,
            "frozen cik is not a canonical ten-digit CIK; no URL was built",
        )
    if not isinstance(accession, str) or not rfb._ACCESSION_RE.match(accession):
        _reject(
            FAILURE_URL_INVALID,
            "frozen accession_number is not in canonical "
            "NNNNNNNNNN-NN-NNNNNN form; no URL was built",
        )
    if (
        not isinstance(document, str)
        or not rfb._PRIMARY_DOC_RE.match(document)
        or not document.lower().endswith(rfb._PRIMARY_DOC_SUFFIXES)
    ):
        # The regex already excludes "/", "\\", "%", and control characters,
        # and anchors the first character, so ".." can never be the whole name.
        _reject(
            FAILURE_URL_INVALID,
            "frozen primary_document is not a safe basename with an allowed "
            "filing-document extension; no URL was built",
        )
    if ".." in document:
        _reject(
            FAILURE_URL_INVALID,
            "frozen primary_document contains a traversal sequence; no URL "
            "was built",
        )

    url = rfb.canonical_source_url(cik, accession, document)
    require_v3_body_url(url, cik=cik, accession=accession, primary_document=document)
    return url


def require_v3_body_url(
    url: Any, *, cik: str, accession: str, primary_document: str
) -> str:
    """Closed validation of one body URL against one frozen filing identity.

    ``rfa.require_official_url`` enforces https, exact-hostname equality
    against the official SEC hosts, no credentials, and no port override. This
    adds everything specific to a *body*: the archives host only, the exact
    ``/Archives/edgar/data/`` prefix, no query, no fragment, and a path that
    decomposes to exactly this side's CIK, accession, and filename.
    """
    try:
        official = rfa.require_official_url(url)
    except rfa.AcquisitionError as exc:
        raise V3HoldoutAcquisitionError(
            FAILURE_URL_INVALID,
            f"source URL is not an official SEC endpoint ({exc.code})",
        ) from None

    parts = urlsplit(official)
    if (parts.hostname or "").lower() != BODY_HOST:
        _reject(
            FAILURE_URL_INVALID,
            f"filing bodies are served only by {BODY_HOST}; metadata hosts "
            "are not a substitute for a body",
        )
    if parts.query:
        _reject(FAILURE_URL_INVALID, "source URL must carry no query string")
    if parts.fragment:
        _reject(FAILURE_URL_INVALID, "source URL must carry no fragment")

    path = parts.path
    if not path.startswith(BODY_PATH_PREFIX):
        _reject(
            FAILURE_URL_INVALID,
            f"source URL path must be exactly under {BODY_PATH_PREFIX}",
        )
    lowered = path.lower()
    if "%2f" in lowered or "%5c" in lowered or "%2e" in lowered:
        _reject(
            FAILURE_URL_INVALID,
            "source URL path contains a percent-encoded separator or "
            "traversal sequence",
        )
    if "\\" in path or ".." in path:
        _reject(FAILURE_URL_INVALID, "source URL path contains a traversal sequence")

    segments = path[len(BODY_PATH_PREFIX) :].split("/")
    if len(segments) != 3 or not all(segments):
        _reject(
            FAILURE_URL_INVALID,
            "source URL path must be exactly "
            "<cik>/<accession>/<primary_document>; index pages, exhibit "
            "directories, and nested paths are not the primary document",
        )
    cik_segment, accession_segment, document_segment = segments

    if cik_segment != str(int(cik)):
        _reject(
            FAILURE_URL_IDENTITY_MISMATCH,
            "source URL CIK segment does not match this side's frozen CIK",
        )
    if accession_segment != accession.replace("-", ""):
        _reject(
            FAILURE_URL_IDENTITY_MISMATCH,
            "source URL accession segment does not match this side's frozen "
            "accession; an alternate accession is never a substitution",
        )
    if document_segment != primary_document:
        _reject(
            FAILURE_URL_IDENTITY_MISMATCH,
            "source URL filename does not match this side's frozen "
            "primary_document; an alternate document, index page, exhibit, or "
            "XBRL instance is never a substitution",
        )
    return official


# --- Identity-bound redirect guard ----------------------------------------------


class _IdentityBoundRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow a redirect only when it lands on this side's exact canonical URL.

    Stricter than the first holdout's host-level guard, and deliberately so:
    on a first acquisition there is no frozen digest, so a redirect to a
    different accession or to a filing index page on the very same official
    host would produce bytes that verify against nothing.
    """

    def __init__(self, expected_url: str):
        super().__init__()
        self._expected_url = expected_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        if newurl != self._expected_url:
            raise V3HoldoutAcquisitionError(
                FAILURE_REDIRECT_FORBIDDEN,
                "the official source redirected away from this side's exact "
                "canonical URL; the redirect was refused and nothing was read",
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def identity_bound_transport(
    url: str, *, headers: dict[str, str], timeout: float
) -> rfa.Response:
    """``real_filing_acquisition.urllib_transport`` semantics plus the
    identity-bound redirect guard. Replaced wholesale in tests, like the
    original — no monkeypatching of urllib internals."""
    opener = urllib.request.build_opener(_IdentityBoundRedirectHandler(url))
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(request, timeout=timeout) as raw:
            response_headers = {key: value for key, value in raw.headers.items()}
            body = raw.read(rfa.MAX_DOCUMENT_BYTES + 1)
            return rfa.Response(
                status=getattr(raw, "status", 200) or 200,
                headers=response_headers,
                body=rfa.decode_content_encoding(
                    body, response_headers.get("Content-Encoding")
                ),
            )
    except V3HoldoutAcquisitionError:
        raise
    except urllib.error.HTTPError as exc:  # a response, not a transport fault
        error_headers = {key: value for key, value in (exc.headers or {}).items()}
        try:
            body = rfa.decode_content_encoding(
                exc.read(rfa.MAX_DOCUMENT_BYTES + 1),
                error_headers.get("Content-Encoding"),
            )
        except Exception:  # noqa: BLE001 - body is diagnostic only
            body = b""
        return rfa.Response(status=exc.code, headers=error_headers, body=body)


# --- Transport-level response checks --------------------------------------------
# These distinguish a real document response from an SEC error/rate-limit page
# or a truncated transfer. They never inspect Item 1A, headings, or any filing
# semantics, and they never decide whether to KEEP a selected filing.

#: SEC serves primary documents as HTML or plain text. A JSON or PDF body under
#: a .htm identity means something other than the primary document answered.
_ACCEPTED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

#: Byte markers that appear in SEC's automated access-denied / rate-limit
#: interstitials. Matched case-insensitively over a bounded prefix only — this
#: is a transport check, not a content read.
_DENIAL_MARKERS = (
    b"your request has been identified as part of a network of automated tools",
    b"request rate threshold",
    b"undeclared automated tool",
    b"declare your traffic by updating your user agent",
    b"access denied",
    b"you have been blocked",
)
_DENIAL_SCAN_BYTES = 4096


def content_type_is_acceptable(content_type: str | None) -> bool:
    """True when the declared media type is consistent with a primary document.

    Absent is accepted: the digest, not the header, is the identity, and some
    responses legitimately omit it. A *declared* type that is not document-like
    is refused, because it means something else answered.
    """
    if content_type is None or not str(content_type).strip():
        return True
    token = str(content_type).split(";", 1)[0].strip().lower()
    return token in _ACCEPTED_CONTENT_TYPES


def looks_like_access_denial(body: bytes) -> bool:
    """True when a 200 response is actually SEC's automated-access notice.

    Only a bounded prefix is examined, only for fixed transport markers. A
    200-with-a-denial-page would otherwise be hashed and frozen as if it were
    the filing.
    """
    prefix = bytes(body[:_DENIAL_SCAN_BYTES]).lower()
    return any(marker in prefix for marker in _DENIAL_MARKERS)


# --- One side -------------------------------------------------------------------


def _read_local_metadata(
    layout: rfb.CorpusLayout, pair_id: str, side: str
) -> dict | None:
    import json

    path = layout.acquisition_metadata_path(pair_id, side)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def acquire_v3_side(
    *,
    fetcher: rfa.Fetcher,
    layout: rfb.CorpusLayout,
    pair: Mapping[str, Any],
    side: str,
    now: Callable[[], str] = rfb.utc_now_iso,
) -> dict[str, Any]:
    """Acquire and checksum one frozen filing.

    Returns a structured outcome and never raises for an expected failure, so
    one bad source cannot hide the state of the other nineteen.
    """
    side_payload = pair[side]
    pair_id = pair["pair_id"]
    target = layout.source_file(pair_id, side, side_payload["primary_document"])
    expected = side_payload["expected_sha256"]

    base = {
        "pair_id": pair_id,
        "side": side,
        "cik": pair["cik"],
        "accession_number": side_payload["accession_number"],
        "form": side_payload["form"],
        "filing_date": side_payload["filing_date"],
        "reporting_period": side_payload["reporting_period"],
        "primary_document": side_payload["primary_document"],
        "source_path": layout.relative(target),
    }

    try:
        url = v3_body_url(pair, side_payload)
    except (V3HoldoutAcquisitionError, rfa.AcquisitionError) as exc:
        return {**base, "outcome": rfa.OUTCOME_FAILED, "verified": False,
                "failure_code": exc.code, "detail": exc.message}
    base["official_source_url"] = url

    # Verified-cache reuse: a local file whose bytes still hash to a digest we
    # can anchor (the frozen manifest's, or the recorded acquisition
    # metadata's) is never re-fetched. A disagreeing file is preserved as
    # evidence and refused — never silently overwritten or deleted.
    if target.exists():
        observed = rfb.sha256_file(target)
        anchor = expected
        anchor_kind = "frozen_manifest"
        if anchor is None:
            metadata = _read_local_metadata(layout, pair_id, side)
            recorded = (metadata or {}).get("observed_sha256")
            if isinstance(recorded, str) and rfb._SHA256_RE.match(recorded):
                anchor = recorded
                anchor_kind = "recorded_acquisition_metadata"
        if anchor is None:
            return {
                **base,
                "outcome": rfa.OUTCOME_FAILED,
                "verified": False,
                "observed_sha256": observed,
                "failure_code": FAILURE_CACHE_UNVERIFIABLE,
                "detail": (
                    "a local source file exists but neither the manifest nor "
                    "recorded acquisition metadata carries a digest to verify "
                    "it against. Remove the local file deliberately to "
                    "re-acquire from the official source."
                ),
            }
        if observed != anchor:
            return {
                **base,
                "outcome": rfa.OUTCOME_FAILED,
                "verified": False,
                "observed_sha256": observed,
                "failure_code": rfa.FAILURE_CACHED_CONTENT_MISMATCH,
                "detail": (
                    f"a local source file disagrees with the {anchor_kind} "
                    "digest. It is NOT overwritten and NOT deleted; a human "
                    "removes it deliberately if re-acquisition is intended."
                ),
            }
        return {
            **base,
            "outcome": rfa.OUTCOME_CACHED,
            "verified": True,
            "observed_sha256": observed,
            "byte_count": target.stat().st_size,
            "acquired_at": (
                (_read_local_metadata(layout, pair_id, side) or {}).get("acquired_at")
                or now()
            ),
        }

    try:
        response = fetcher.get(url)
    except (V3HoldoutAcquisitionError, rfa.AcquisitionError) as exc:
        return {**base, "outcome": rfa.OUTCOME_FAILED, "verified": False,
                "failure_code": exc.code, "detail": exc.message}

    body = response.body or b""
    if not body:
        return {**base, "outcome": rfa.OUTCOME_FAILED, "verified": False,
                "failure_code": rfa.FAILURE_EMPTY_RESPONSE,
                "detail": "the official source returned an empty document"}
    if len(body) > rfa.MAX_DOCUMENT_BYTES:
        return {**base, "outcome": rfa.OUTCOME_FAILED, "verified": False,
                "failure_code": rfa.FAILURE_RESPONSE_TOO_LARGE,
                "detail": (
                    f"document exceeds the {rfa.MAX_DOCUMENT_BYTES}-byte "
                    "bound and was not written"
                )}

    content_type = response.header("Content-Type")
    if not content_type_is_acceptable(content_type):
        return {**base, "outcome": rfa.OUTCOME_FAILED, "verified": False,
                "failure_code": FAILURE_CONTENT_TYPE_INVALID,
                "detail": (
                    "the official source declared a media type inconsistent "
                    "with a primary filing document; nothing was written"
                )}
    if looks_like_access_denial(body):
        return {**base, "outcome": rfa.OUTCOME_FAILED, "verified": False,
                "failure_code": FAILURE_ACCESS_DENIED_RESPONSE,
                "detail": (
                    "the official source returned an automated-access or "
                    "rate-limit notice rather than the filing; nothing was "
                    "written. Slow the request pacing and rerun explicitly."
                )}

    # A truncated identity-encoded response is not detectable by decoding, so
    # check the server's own declared length. Partial bytes are never written,
    # let alone verified.
    declared = response.header("Content-Length")
    encoding = (response.header("Content-Encoding") or "").strip().lower()
    if declared is not None and encoding in ("", "identity"):
        try:
            declared_length = int(str(declared).strip())
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length != len(body):
            return {**base, "outcome": rfa.OUTCOME_FAILED, "verified": False,
                    "failure_code": FAILURE_INCOMPLETE_RESPONSE,
                    "detail": (
                        "response body length disagrees with the declared "
                        "Content-Length; partial bytes were not written"
                    )}

    observed = hashlib.sha256(body).hexdigest()
    if expected is not None and observed != expected:
        # Verify BEFORE writing: bytes that fail an already-frozen digest never
        # reach the corpus directory at all, and the pinned digest is never
        # re-pinned to whatever arrived.
        return {**base, "outcome": rfa.OUTCOME_FAILED, "verified": False,
                "observed_sha256": observed,
                "failure_code": rfa.FAILURE_CHECKSUM_MISMATCH,
                "detail": (
                    "downloaded content does not match the frozen manifest "
                    "digest; nothing was written and the pinned digest was "
                    "not changed"
                )}

    rfa._write_atomic(target, body)

    # Repeatability through re-read: the digest that gets frozen is the digest
    # of the bytes a later reader will actually see on disk.
    reread = rfb.sha256_file(target)
    if reread != observed:
        return {**base, "outcome": rfa.OUTCOME_FAILED, "verified": False,
                "observed_sha256": observed,
                "failure_code": FAILURE_LOCAL_REREAD_MISMATCH,
                "detail": (
                    "re-reading the just-written local file produced a "
                    "different digest; the digest was not accepted"
                )}

    acquired_at = now()
    rfb.write_json_atomic(
        layout.acquisition_metadata_path(pair_id, side),
        {
            "metadata_version": V3_ACQUISITION_METADATA_VERSION,
            "protocol_version": V3_SOURCE_ACQUISITION_PROTOCOL_VERSION,
            "pair_id": pair_id,
            "side": side,
            "cik": pair["cik"],
            "accession_number": side_payload["accession_number"],
            "official_source_url": url,
            "primary_document": side_payload["primary_document"],
            "acquired_at": acquired_at,
            "byte_count": len(body),
            "observed_sha256": observed,
            "reread_sha256": reread,
            "content_type": content_type,
            "user_agent_supplied": True,
        },
    )

    return {
        **base,
        "outcome": rfa.OUTCOME_DOWNLOADED,
        "verified": True,
        "observed_sha256": observed,
        "byte_count": len(body),
        "acquired_at": acquired_at,
        "content_type": content_type,
    }


# --- All twenty -----------------------------------------------------------------


def acquire_v3_manifest(
    manifest: Mapping[str, Any],
    *,
    fetcher: rfa.Fetcher,
    layout: rfb.CorpusLayout,
    now: Callable[[], str] = rfb.utc_now_iso,
) -> dict[str, Any]:
    """Acquire every frozen side, in frozen manifest order.

    Aggregates outcomes; the frozen pair list is read, never edited, filtered,
    reordered, or extended.
    """
    results: list[dict[str, Any]] = []
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            results.append(
                acquire_v3_side(
                    fetcher=fetcher, layout=layout, pair=pair, side=side, now=now
                )
            )

    verified = [item for item in results if item["verified"]]
    return {
        "filings": results,
        "requested_filings": len(results),
        "verified_filings": len(verified),
        "downloaded": sum(
            1 for item in results if item["outcome"] == rfa.OUTCOME_DOWNLOADED
        ),
        "reused_verified_cache": sum(
            1 for item in results if item["outcome"] == rfa.OUTCOME_CACHED
        ),
        "failed": sum(1 for item in results if item["outcome"] == rfa.OUTCOME_FAILED),
        "request_count": fetcher.request_count,
        # Structurally zero, not merely observed to be: the identity-bound
        # handler refuses every redirect target that differs from this side's
        # canonical URL, and a redirect to the identical URL is a loop urllib
        # rejects. No followed redirect can therefore change what was fetched.
        "redirect_count": 0,
        "total_verified_bytes": sum(item.get("byte_count", 0) for item in verified),
        "all_verified": bool(results) and len(verified) == len(results),
    }


def verify_source_identity_uniqueness(acquisition: Mapping[str, Any]) -> None:
    """Two frozen sides may never resolve to the same document or the same
    bytes.

    The v3 selection guarantees ten distinct issuers and twenty distinct
    accessions, so a repeated URL means a URL-construction defect and a
    repeated digest means the same body was served twice — either would make
    the "previous vs current" comparison compare a filing with itself.
    """
    seen_urls: dict[str, tuple[str, str]] = {}
    seen_digests: dict[str, tuple[str, str]] = {}
    for item in acquisition["filings"]:
        identity = (item["pair_id"], item["side"])
        url = item.get("official_source_url")
        if url is not None:
            if url in seen_urls:
                owner = seen_urls[url]
                raise V3HoldoutAcquisitionError(
                    FAILURE_DUPLICATE_IDENTITY,
                    f"{identity[0]}/{identity[1]} resolves to the same official "
                    f"document as {owner[0]}/{owner[1]}; two frozen sides may "
                    "never share a primary document",
                )
            seen_urls[url] = identity
        digest = item.get("observed_sha256")
        if item.get("verified") and isinstance(digest, str):
            if digest in seen_digests:
                owner = seen_digests[digest]
                raise V3HoldoutAcquisitionError(
                    FAILURE_DUPLICATE_IDENTITY,
                    f"{identity[0]}/{identity[1]} has the same source digest as "
                    f"{owner[0]}/{owner[1]}; identical bytes are not two "
                    "distinct filings and are never merged into one side",
                )
            seen_digests[digest] = identity


# --- Manifest transition --------------------------------------------------------


def source_verified_corpus_role_detail() -> str:
    """Role prose that is TRUE after acquisition.

    The freeze-time detail says "No filing body has been acquired"; leaving
    that sentence beside twenty digests would be an honest field next to a
    dishonest sentence. The role itself and every denial stay frozen — only the
    descriptive prose advances with the facts.
    """
    return (
        "Issuers and exact filing pairs were frozen from official SEC metadata "
        "only, after item1a_units.v3 / item1a_detector.v3 / "
        "comparison_workflow.v3 and the contract-v2 gold evaluator "
        "(real-filing-benchmark.evaluation.v2 + metrics.v2 + report.v2) were "
        "merged and frozen, and before any selected filing body was downloaded "
        "or inspected. Neither the parser, the unit grammar, nor the "
        "evaluation contract was developed using this corpus. The twenty "
        "frozen bodies have since been acquired from official SEC sources and "
        "checksum-verified over decoded entity bytes, but the frozen parser "
        "has NOT run over them: no extraction, no unit segmentation, no "
        "comparison, no annotation, and no evaluation exists, and no "
        "generalization claim is supported. Source verification establishes "
        "only which bytes were obtained; whether a filing contains an "
        "extractable Item 1A section is unknown until a separate blind "
        "extraction run reports it. Modifying any pinned frozen file in "
        "response to future results from this corpus would convert it into "
        "development data; the recorded hashes make that detectable."
    )


SOURCE_VERIFIED_DESCRIPTION = (
    "v3 extraction holdout at source_verified: exact issuers and filing pairs "
    "frozen from official SEC metadata AFTER the v3 unit representation and "
    "the contract-v2 gold evaluator were frozen and BEFORE any selected filing "
    "body was observed; the twenty frozen bodies were then acquired from "
    "official SEC sources and checksum-verified over decoded entity bytes. The "
    "frozen parser has NOT run over them: no extraction, comparison, "
    "annotation, accuracy, or generalization result exists. A selected pair is "
    "never replaced because of later-observed filing-body structure, "
    "extraction outcome, detector output, workflow output, or evaluation "
    "result."
)


def advance_v3_holdout_manifest(
    manifest: Mapping[str, Any], acquisition: Mapping[str, Any]
) -> dict[str, Any]:
    """The one forward step: freeze verified digests and set source_verified.

    Refuses unless every side is verified. Changes exactly four things —
    per-side ``expected_sha256``, per-side ``source_verified``, the top-level
    ``status``, and the two descriptive prose fields the schema permits to move
    (``corpus_role_detail`` and the optional ``description``) — then
    re-validates the result, so a defect here is a loud failure rather than a
    committed lie. No frozen identity field is touched, and no new key is
    introduced: the manifest schema is exact-key, and the acquisition protocol
    identity is bound through the committed report instead.
    """
    if not acquisition.get("all_verified"):
        raise V3HoldoutAcquisitionError(
            FAILURE_NOT_FULLY_VERIFIED,
            f"{acquisition.get('verified_filings', 0)} of "
            f"{acquisition.get('requested_filings', 0)} filings are verified; "
            "the manifest advances only when all twenty are. No pair is ever "
            "replaced to make acquisition easier.",
        )
    verify_source_identity_uniqueness(acquisition)
    rfv3.validate_v3_holdout_status_transition(
        manifest["status"], rfb.STATUS_SOURCE_VERIFIED
    )

    digests = {
        (item["pair_id"], item["side"]): item["observed_sha256"]
        for item in acquisition["filings"]
        if item["verified"]
    }
    advanced = copy.deepcopy(dict(manifest))
    for pair in advanced["pairs"]:
        for side in ("previous", "current"):
            key = (pair["pair_id"], side)
            if key not in digests:
                raise V3HoldoutAcquisitionError(
                    FAILURE_NOT_FULLY_VERIFIED,
                    f"no verified digest for {pair['pair_id']}/{side}",
                )
            pair[side]["expected_sha256"] = digests[key]
            pair[side]["source_verified"] = True
    advanced["status"] = rfb.STATUS_SOURCE_VERIFIED
    advanced["corpus_role_detail"] = source_verified_corpus_role_detail()
    if "description" in advanced:
        advanced["description"] = SOURCE_VERIFIED_DESCRIPTION
    rfv3.validate_v3_holdout_manifest(advanced)
    return advanced


# --- Source-verification report -------------------------------------------------


def build_source_verification_report(
    *,
    manifest: Mapping[str, Any],
    acquisition: Mapping[str, Any],
    prior_manifest_sha256: str,
    new_manifest_sha256: str | None,
    new_reproducible_manifest_hash: str | None,
    generated_at: str,
) -> dict[str, Any]:
    """Bounded committed report: identities, digests, counts, and hashes —
    never filing content, never a local absolute path, never a credential."""
    all_verified = bool(acquisition["all_verified"])
    outcome = (
        VERIFICATION_OUTCOME_VERIFIED if all_verified else VERIFICATION_OUTCOME_FAILED
    )
    hosts = sorted(
        {
            urlparse(item["official_source_url"]).hostname
            for item in acquisition["filings"]
            if item.get("official_source_url")
        }
    )
    retry_reasons: dict[str, int] = {}
    for item in acquisition["filings"]:
        code = item.get("failure_code")
        if code:
            retry_reasons[code] = retry_reasons.get(code, 0) + 1

    filings = [
        {
            key: item[key]
            for key in (
                "pair_id", "side", "cik", "accession_number", "form",
                "filing_date", "reporting_period", "primary_document",
                "official_source_url", "outcome", "verified",
            )
            if key in item
        }
        | (
            {
                "final_source_url_equals_canonical": True,
                "response_status": 200,
                "content_type": item.get("content_type"),
                "content_encoding_applied_before_hash": True,
                "byte_count": item["byte_count"],
                "sha256": item["observed_sha256"],
                "local_source_path": item["source_path"],
                "source_verified": True,
                "acquired_at": item.get("acquired_at"),
            }
            if item["verified"]
            else {
                "source_verified": False,
                "failure_code": item.get("failure_code"),
            }
        )
        for item in acquisition["filings"]
    ]

    return {
        "report_version": V3_SOURCE_VERIFICATION_REPORT_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "benchmark_version": manifest["benchmark_version"],
        "generated_at": generated_at,
        "verification_outcome": outcome,
        # -- protocol identity
        "source_acquisition_protocol_version": (
            V3_SOURCE_ACQUISITION_PROTOCOL_VERSION
        ),
        "source_acquisition_protocol_hash": source_acquisition_protocol_hash(),
        "source_acquisition_protocol": source_acquisition_protocol(),
        # -- manifest chain
        "prior_manifest_status": manifest["status"],
        "new_manifest_status": (
            rfb.STATUS_SOURCE_VERIFIED if all_verified else manifest["status"]
        ),
        "prior_manifest_sha256": prior_manifest_sha256,
        "new_manifest_sha256": new_manifest_sha256,
        "new_reproducible_manifest_hash": new_reproducible_manifest_hash,
        # -- frozen contract identities, unchanged
        "selection_protocol_version": manifest["selection_protocol_version"],
        "selection_protocol_hash": manifest["selection_protocol_hash"],
        "frozen_extraction_parser_version": manifest[
            "frozen_extraction_parser_version"
        ],
        "frozen_parser_source_sha256": manifest["frozen_parser_source_sha256"],
        "frozen_unit_grammar_version": manifest["frozen_unit_grammar_version"],
        "frozen_detector_version": manifest["frozen_detector_version"],
        "frozen_detector_source_sha256": manifest["frozen_detector_source_sha256"],
        "frozen_workflow_version": manifest["frozen_workflow_version"],
        "frozen_workflow_source_sha256": manifest["frozen_workflow_source_sha256"],
        "frozen_evaluation_contract_version": manifest[
            "frozen_evaluation_contract_version"
        ],
        "frozen_metric_definitions_version": manifest[
            "frozen_metric_definitions_version"
        ],
        "frozen_report_contract_version": manifest["frozen_report_contract_version"],
        "frozen_evaluator_source_sha256": manifest["frozen_evaluator_source_sha256"],
        # -- corpus role, with only the prose advanced
        "corpus_role": manifest["corpus_role"],
        "extraction_parser_developed_using_this_corpus": False,
        "evaluation_contract_developed_using_this_corpus": False,
        "extraction_holdout_evaluation": False,
        "generalization_claim_supported": False,
        "corpus_role_detail": source_verified_corpus_role_detail(),
        # -- counts
        "pair_ids": [pair["pair_id"] for pair in manifest["pairs"]],
        "pair_count": len(manifest["pairs"]),
        "side_count": len(acquisition["filings"]),
        "official_hosts_contacted": hosts,
        "request_attempts": acquisition["request_count"],
        "filing_body_requests": acquisition["request_count"],
        "successful_body_requests": acquisition["downloaded"],
        "source_documents_downloaded": acquisition["downloaded"],
        "reused_verified_cache": acquisition["reused_verified_cache"],
        "source_checksums_verified": acquisition["verified_filings"],
        "verified_source_count": acquisition["verified_filings"],
        "failed_source_count": acquisition["failed"],
        "failure_counts_by_reason": dict(sorted(retry_reasons.items())),
        "redirect_count": acquisition["redirect_count"],
        "total_verified_bytes": acquisition["total_verified_bytes"],
        "hash_algorithm": "sha256",
        "source_byte_semantics": SOURCE_BYTE_SEMANTICS,
        "local_path_convention": LOCAL_PATH_CONVENTION,
        "retry_policy": rfa.ACQUISITION_RETRY_POLICY,
        "filings": filings,
        # Structural zeroes: this step ends at bytes-on-disk. The import-graph
        # test proves nothing below could have run; these fields exist so a
        # reader can check the claim without reading the test.
        "extraction_runs": 0,
        "comparison_runs": 0,
        "annotation_packets": 0,
        "machine_proposed_labels": 0,
        "human_verified_labels": 0,
        "gold_evaluation_runs": 0,
        "signoff_present": False,
        "notes": [
            (
                "Digests are taken over the DECODED filing bytes and confirmed "
                "by re-reading the written local file. SEC honors gzip, so a "
                "digest over the wire bytes would not be reproducible across "
                "downloads."
            ),
            (
                "This was the first acquisition of these bodies: the frozen "
                "manifest carried null digests, so these digests were observed "
                "here and frozen into the advanced manifest, where every later "
                "read verifies against them. A later mismatch fails; it never "
                "silently re-pins."
            ),
            (
                "Source verification is not parser validation. It establishes "
                "which official bytes were obtained, and nothing about whether "
                "any filing contains an extractable Item 1A section."
            ),
            (
                "The extraction parser has NOT run over these filings. No "
                "extraction, unit segmentation, comparison, packet, label, or "
                "metric exists for this corpus, and no generalization claim is "
                "supported until it does — with every pinned file unchanged."
            ),
            (
                "Complete filings live only under the gitignored "
                "benchmark_data/ tree and are never committed."
            ),
            (
                "The SEC user agent is supplied externally and appears in no "
                "artifact."
            ),
        ],
        "regenerated_by": (
            "python scripts/acquire_real_filing_v3_holdout.py --allow-network "
            "(requires SEC_USER_AGENT; never run in CI)"
        ),
    }


def verify_report_manifest_binding(
    report: Mapping[str, Any], manifest: Mapping[str, Any]
) -> None:
    """Cross-check the report against the manifest it claims to describe.

    Written before either artifact is committed. A report that disagrees with
    its manifest is worse than no report: it would look like corroboration.
    """
    if report["benchmark_id"] != manifest["benchmark_id"]:
        raise V3HoldoutAcquisitionError(
            FAILURE_REPORT_MANIFEST_MISMATCH,
            "source-verification report and manifest name different benchmarks",
        )
    if report["new_manifest_status"] != manifest["status"]:
        raise V3HoldoutAcquisitionError(
            FAILURE_REPORT_MANIFEST_MISMATCH,
            "source-verification report's new_manifest_status does not match "
            "the manifest it describes",
        )
    if report["pair_ids"] != [pair["pair_id"] for pair in manifest["pairs"]]:
        raise V3HoldoutAcquisitionError(
            FAILURE_REPORT_MANIFEST_MISMATCH,
            "source-verification report's pair ids do not match the frozen "
            "manifest pair order",
        )
    reported = {
        (item["pair_id"], item["side"]): item.get("sha256")
        for item in report["filings"]
    }
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            key = (pair["pair_id"], side)
            if key not in reported:
                raise V3HoldoutAcquisitionError(
                    FAILURE_REPORT_MANIFEST_MISMATCH,
                    f"source-verification report omits {key[0]}/{key[1]}",
                )
            if reported[key] != pair[side]["expected_sha256"]:
                raise V3HoldoutAcquisitionError(
                    FAILURE_REPORT_MANIFEST_MISMATCH,
                    f"source-verification report digest for {key[0]}/{key[1]} "
                    "does not match the manifest digest",
                )
