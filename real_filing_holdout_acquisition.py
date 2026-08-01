"""Source verification for the frozen extraction holdout — acquisition only.

Stage 3.5, the step after the metadata-only freeze: download the exact twenty
primary 10-K documents the committed holdout manifest froze, checksum them, and
advance the manifest exactly one step (``holdout_frozen_metadata_only`` ->
``source_verified``). Nothing downstream of bytes-on-disk happens here: no
extraction, no ingestion, no Chroma, no comparison, no packet, no label. A test
pins that at the import graph.

Trust-on-first-acquisition, stated plainly
------------------------------------------
The frozen manifest carries ``expected_sha256: null`` on every side, because
metadata-only meant exactly that. This module therefore cannot *verify against*
a prior digest on the first run; what it can do — and all it claims — is:

1. fetch each body from the one canonical official EDGAR URL derived from the
   frozen CIK/accession/primary-document fields (never from a search, mirror,
   or guess),
2. decode the transport's Content-Encoding so the digest is over the filing,
   not a gzip container,
3. hash the decoded bytes, write them atomically, re-read the cached file and
   re-hash it, and accept the digest only when the two agree,
4. freeze those digests into the advanced manifest so every LATER read is a
   real verification against a committed value.

What this module refuses to do
------------------------------
- Replace, drop, or reorder a frozen pair for any reason. A source that cannot
  be acquired fails the whole source-verification phase; the manifest stays at
  ``holdout_frozen_metadata_only``.
- Update any frozen identity field from observed content. Identity flows one
  way: manifest -> URL -> bytes.
- Advance the manifest while any of the twenty sides is unverified.
- Follow a redirect off the official-host allowlist. ``urllib`` follows
  redirects silently, and with no frozen digest on first acquisition a
  redirect to a look-alike host would be invisible; the transport here
  validates every redirect target before allowing it.
- Emit filing content, local absolute paths, credentials, or transport
  exception text in any report or error.

Like ``scripts/select_real_filing_holdout.py``, this module composes the
existing acquisition infrastructure (``real_filing_acquisition``) with the
holdout schema (``real_filing_holdout``); it adds no second transport policy,
no second retry policy, and no second hashing rule.
"""

from __future__ import annotations

import copy
import hashlib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import real_filing_acquisition as rfa
import real_filing_benchmark as rfb
import real_filing_holdout as rfh

HOLDOUT_SOURCE_VERIFICATION_REPORT_VERSION = (
    "real-filing-holdout.source-verification.v1"
)
HOLDOUT_ACQUISITION_METADATA_VERSION = "real-filing-holdout.acquisition.v1"

# --- Stable failure codes beyond real_filing_acquisition's ---------------------

FAILURE_PARSER_SOURCE_DRIFT = "holdout_parser_source_drift"
FAILURE_EXCLUSION_DRIFT = "holdout_exclusion_drift"
FAILURE_MANIFEST_HASH_DRIFT = "holdout_manifest_hash_drift"
FAILURE_STATUS_NOT_ACQUIRABLE = "holdout_status_not_acquirable"
FAILURE_NOT_FULLY_VERIFIED = "holdout_not_fully_verified"
FAILURE_REREAD_MISMATCH = "cached_reread_mismatch"
FAILURE_CACHE_UNVERIFIABLE = "cached_content_unverifiable"
FAILURE_INCOMPLETE_RESPONSE = "incomplete_response"

VERIFICATION_OUTCOME_VERIFIED = "source_verified"
VERIFICATION_OUTCOME_FAILED = "failed"


class HoldoutAcquisitionError(rfb.BenchmarkError):
    """A bounded, code-carrying holdout-acquisition failure. Never carries
    filing content, credentials, local paths, or transport exception text."""


# --- Immutability gate ----------------------------------------------------------


def verify_frozen_identity(
    manifest: Mapping[str, Any],
    *,
    repo_root: str | Path | None = None,
    development_manifest: Mapping[str, Any] | None = None,
) -> None:
    """Refuse acquisition if any frozen identity has drifted.

    ``validate_holdout_manifest`` already pins the schema, benchmark id, pair
    count, pair ordering, strata, form, protocol hash, and development-corpus
    disjointness. This gate adds the two facts only the working tree can
    attest: the parser bytes on disk still hash to the frozen digest (read as
    bytes, never imported), and the exclusion block still equals what the
    committed development manifest derives to.
    """
    rfh.validate_holdout_manifest(manifest)

    if manifest["status"] not in (
        rfh.STATUS_HOLDOUT_FROZEN_METADATA_ONLY,
        rfb.STATUS_SOURCE_VERIFIED,
    ):
        raise HoldoutAcquisitionError(
            FAILURE_STATUS_NOT_ACQUIRABLE,
            f"holdout manifest status {manifest['status']!r} is beyond "
            "source verification; acquisition has nothing to do",
        )

    current_parser_hash = rfh.frozen_parser_source_sha256(repo_root)
    if current_parser_hash != manifest["frozen_parser_source_sha256"]:
        raise HoldoutAcquisitionError(
            FAILURE_PARSER_SOURCE_DRIFT,
            "the extraction parser source on disk no longer hashes to the "
            "digest frozen in the holdout manifest. The parser may not change "
            "after the freeze; a deliberate, documented parser change "
            "requires freezing a NEW holdout corpus.",
        )

    expected_exclusions = rfh.development_exclusions(development_manifest)
    frozen = manifest["development_exclusions"]
    if (
        frozen["development_benchmark_id"]
        != expected_exclusions["development_benchmark_id"]
        or sorted(frozen["excluded_ciks"])
        != sorted(expected_exclusions["excluded_ciks"])
        or sorted(frozen["excluded_accessions"])
        != sorted(expected_exclusions["excluded_accessions"])
    ):
        raise HoldoutAcquisitionError(
            FAILURE_EXCLUSION_DRIFT,
            "the manifest's frozen development-corpus exclusions no longer "
            "match those derived from the committed development manifest",
        )


def verify_manifest_hash_chain(manifest_path: str | Path) -> None:
    """A still-metadata-only manifest must be byte-identical to the file the
    committed selection report froze.

    The selection report records ``holdout_manifest_sha256`` over the exact
    bytes the freeze wrote. Until the transition, any difference means the
    freeze was hand-edited after the fact — a changed accession, CIK, or
    document name would still be schema-valid, so only the hash can catch it.
    Skipped when no selection report sits beside the manifest (synthetic
    library/test manifests) and after the transition (the source-verification
    report pins the new bytes instead).
    """
    import json

    manifest_path = Path(manifest_path)
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HoldoutAcquisitionError(
            "holdout_manifest_unreadable",
            f"holdout manifest could not be read ({type(exc).__name__})",
        ) from None
    if document.get("status") != rfh.STATUS_HOLDOUT_FROZEN_METADATA_ONLY:
        return
    report_path = manifest_path.parent / "selection_report.json"
    if not report_path.exists():
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HoldoutAcquisitionError(
            "holdout_selection_report_unreadable",
            f"selection report could not be read ({type(exc).__name__})",
        ) from None
    frozen = report.get("holdout_manifest_sha256")
    if frozen != rfb.sha256_file(manifest_path):
        raise HoldoutAcquisitionError(
            FAILURE_MANIFEST_HASH_DRIFT,
            "the metadata-only holdout manifest no longer hashes to the "
            "value the committed selection report froze. A frozen identity "
            "has drifted; acquisition is refused rather than verifying an "
            "edited freeze.",
        )


# --- Official body URL ----------------------------------------------------------


def holdout_body_url(pair: Mapping[str, Any], side_payload: Mapping[str, Any]) -> str:
    """The one canonical official EDGAR URL, derived from frozen fields only."""
    return rfa.require_official_url(
        rfb.canonical_source_url(
            pair["cik"],
            side_payload["accession_number"],
            side_payload["primary_document"],
        )
    )


# --- Redirect-guarded transport -------------------------------------------------


class _OfficialRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow a redirect only when its target passes the official-host rule.

    Needed here and not in the development acquisition path because the first
    holdout acquisition has no frozen digest to catch substituted content: a
    silent redirect to a non-official host would otherwise be undetectable.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        rfa.require_official_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def redirect_guarded_transport(
    url: str, *, headers: dict[str, str], timeout: float
) -> rfa.Response:
    """``real_filing_acquisition.urllib_transport`` semantics plus the
    redirect guard. Replaced wholesale in tests, like the original."""
    opener = urllib.request.build_opener(_OfficialRedirectHandler())
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
    except rfa.NonOfficialSource:
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


# --- One side -------------------------------------------------------------------


def _read_local_metadata(layout: rfb.CorpusLayout, pair_id: str, side: str) -> dict | None:
    import json

    path = layout.acquisition_metadata_path(pair_id, side)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def acquire_holdout_side(
    *,
    fetcher: rfa.Fetcher,
    layout: rfb.CorpusLayout,
    pair: Mapping[str, Any],
    side: str,
    now: Callable[[], str] = rfb.utc_now_iso,
) -> dict[str, Any]:
    """Acquire and checksum one frozen filing. Returns a structured outcome;
    never raises for an expected failure, so one bad source cannot hide the
    state of the other nineteen."""
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
        url = holdout_body_url(pair, side_payload)
    except rfa.AcquisitionError as exc:
        return {**base, "outcome": rfa.OUTCOME_FAILED, "verified": False,
                "failure_code": exc.code, "detail": exc.message}
    base["official_source_url"] = url

    # Verified-cache reuse: a local file whose bytes still hash to a digest we
    # can anchor (the frozen manifest's, or the recorded acquisition
    # metadata's) is never re-fetched. A disagreeing file is preserved as
    # evidence and refused — never silently overwritten.
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
                    "a cached local file exists but neither the manifest nor "
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
                    f"a cached local file disagrees with the {anchor_kind} "
                    "digest. It is NOT overwritten; a human removes it "
                    "deliberately if re-acquisition is intended."
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
    except rfa.AcquisitionError as exc:
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
        # Verify BEFORE writing: bytes that fail the frozen digest never reach
        # the corpus directory at all.
        return {**base, "outcome": rfa.OUTCOME_FAILED, "verified": False,
                "observed_sha256": observed,
                "failure_code": rfa.FAILURE_CHECKSUM_MISMATCH,
                "detail": (
                    "downloaded content does not match the frozen manifest "
                    "digest; nothing was written"
                )}

    rfa._write_atomic(target, body)

    # Repeatability through cached re-read: the digest that gets frozen is the
    # digest of the bytes a later reader will actually see.
    reread = rfb.sha256_file(target)
    if reread != observed:
        return {**base, "outcome": rfa.OUTCOME_FAILED, "verified": False,
                "observed_sha256": observed,
                "failure_code": FAILURE_REREAD_MISMATCH,
                "detail": (
                    "re-reading the just-written cache file produced a "
                    "different digest; the digest was not accepted"
                )}

    acquired_at = now()
    rfb.write_json_atomic(
        layout.acquisition_metadata_path(pair_id, side),
        {
            "metadata_version": HOLDOUT_ACQUISITION_METADATA_VERSION,
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
            "content_type": response.header("Content-Type"),
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
        "content_type": response.header("Content-Type"),
    }


# --- All twenty -----------------------------------------------------------------


def acquire_holdout_manifest(
    manifest: Mapping[str, Any],
    *,
    fetcher: rfa.Fetcher,
    layout: rfb.CorpusLayout,
    now: Callable[[], str] = rfb.utc_now_iso,
) -> dict[str, Any]:
    """Acquire every frozen side, in manifest order. Aggregates outcomes; the
    frozen pair list is read, never edited."""
    results: list[dict[str, Any]] = []
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            results.append(
                acquire_holdout_side(
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
        "failed": sum(
            1 for item in results if item["outcome"] == rfa.OUTCOME_FAILED
        ),
        "request_count": fetcher.request_count,
        "total_verified_bytes": sum(
            item.get("byte_count", 0) for item in verified
        ),
        "all_verified": bool(results) and len(verified) == len(results),
    }


# --- Manifest transition --------------------------------------------------------


def source_verified_corpus_role_detail() -> str:
    """Role prose that is TRUE after acquisition.

    The freeze-time detail says "No filing body has been acquired"; leaving
    that sentence beside twenty digests would be an honest field next to a
    dishonest sentence. The role itself and every denial stay frozen — only
    the descriptive prose advances with the facts.
    """
    return (
        "Issuers and exact filing pairs were frozen from official SEC "
        "metadata only, after the extraction parser was frozen and before "
        "any selected filing body was downloaded or inspected. The twenty "
        "frozen filing bodies have since been acquired from official SEC "
        "sources and checksum-verified, but the frozen parser has NOT run "
        "over them: no extraction, no annotation, and no holdout evaluation "
        "exists, and no generalization claim is supported. A selected pair "
        "is never replaced, and modifying the frozen parser in response to "
        "future holdout results would convert this corpus into development "
        "data."
    )


def source_verified_corpus_role_fields() -> dict[str, Any]:
    fields = rfh.holdout_corpus_role_fields()
    fields["corpus_role_detail"] = source_verified_corpus_role_detail()
    return fields


SOURCE_VERIFIED_DESCRIPTION = (
    "Extraction holdout at source_verified: exact issuers and filing pairs "
    "frozen from official SEC metadata AFTER sec_html_item_headings.v2 was "
    "frozen and BEFORE any selected filing body was observed; the twenty "
    "frozen bodies were then acquired from official SEC sources and "
    "checksum-verified over decoded canonical bytes. The frozen parser has "
    "NOT run over them: no extraction, annotation, accuracy, or "
    "generalization result exists. A selected pair is never replaced because "
    "of later-observed filing-body structure, extraction outcome, detector "
    "output, or evaluation result."
)


def advance_holdout_manifest(
    manifest: Mapping[str, Any], acquisition: Mapping[str, Any]
) -> dict[str, Any]:
    """The one forward step: freeze verified digests and set source_verified.

    Refuses unless every side is verified. Changes exactly three things —
    per-side ``expected_sha256``, per-side ``source_verified``, and the
    top-level ``status`` — and re-validates the result, so a defect here is a
    loud failure rather than a committed lie.
    """
    if not acquisition.get("all_verified"):
        raise HoldoutAcquisitionError(
            FAILURE_NOT_FULLY_VERIFIED,
            f"{acquisition.get('verified_filings', 0)} of "
            f"{acquisition.get('requested_filings', 0)} filings are verified; "
            "the manifest advances only when all twenty are. No pair is ever "
            "replaced to make acquisition easier.",
        )
    rfh.validate_holdout_status_transition(
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
                raise HoldoutAcquisitionError(
                    FAILURE_NOT_FULLY_VERIFIED,
                    f"no verified digest for {pair['pair_id']}/{side}",
                )
            pair[side]["expected_sha256"] = digests[key]
            pair[side]["source_verified"] = True
    advanced["status"] = rfb.STATUS_SOURCE_VERIFIED
    advanced["corpus_role_detail"] = source_verified_corpus_role_detail()
    if "description" in advanced:
        advanced["description"] = SOURCE_VERIFIED_DESCRIPTION
    rfh.validate_holdout_manifest(advanced)
    return advanced


# --- Source-verification report -------------------------------------------------


def build_source_verification_report(
    *,
    manifest: Mapping[str, Any],
    acquisition: Mapping[str, Any],
    prior_manifest_sha256: str,
    new_manifest_sha256: str | None,
    generated_at: str,
) -> dict[str, Any]:
    """Bounded committed report: identities, digests, counts, and hashes —
    never filing content, never a local absolute path, never a credential."""
    outcome = (
        VERIFICATION_OUTCOME_VERIFIED
        if acquisition["all_verified"]
        else VERIFICATION_OUTCOME_FAILED
    )
    hosts = sorted(
        {
            urlparse(item["official_source_url"]).hostname
            for item in acquisition["filings"]
            if item.get("official_source_url")
        }
    )
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
                "sha256": item["observed_sha256"],
                "byte_count": item["byte_count"],
                "acquired_at": item.get("acquired_at"),
            }
            if item["verified"]
            else {"failure_code": item.get("failure_code")}
        )
        for item in acquisition["filings"]
    ]
    return {
        "report_version": HOLDOUT_SOURCE_VERIFICATION_REPORT_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "benchmark_version": manifest["benchmark_version"],
        "generated_at": generated_at,
        "verification_outcome": outcome,
        "prior_manifest_status": manifest["status"],
        "new_manifest_status": (
            rfb.STATUS_SOURCE_VERIFIED if acquisition["all_verified"] else
            manifest["status"]
        ),
        "prior_manifest_sha256": prior_manifest_sha256,
        "new_manifest_sha256": new_manifest_sha256,
        "frozen_extraction_parser_version": manifest[
            "frozen_extraction_parser_version"
        ],
        "frozen_parser_source_sha256": manifest["frozen_parser_source_sha256"],
        "selection_protocol_version": manifest["selection_protocol_version"],
        "selection_protocol_hash": manifest["selection_protocol_hash"],
        **source_verified_corpus_role_fields(),
        "pair_ids": [pair["pair_id"] for pair in manifest["pairs"]],
        "official_hosts_contacted": hosts,
        "filing_body_requests": acquisition["request_count"],
        "source_documents_downloaded": acquisition["downloaded"],
        "reused_verified_cache": acquisition["reused_verified_cache"],
        "source_checksums_verified": acquisition["verified_filings"],
        "failed_filings": acquisition["failed"],
        "total_verified_bytes": acquisition["total_verified_bytes"],
        "retry_policy": rfa.ACQUISITION_RETRY_POLICY,
        "filings": filings,
        # Structural zeroes: this step ends at bytes-on-disk. The import-graph
        # test proves nothing below could have run; these fields exist so a
        # reader can check the claim without reading the test.
        "extraction_runs": 0,
        "comparison_runs": 0,
        "annotation_packets": 0,
        "human_verified_labels": 0,
        "notes": [
            (
                "Digests are taken over the DECODED filing bytes and confirmed "
                "by re-reading the written cache file. SEC honors gzip, so a "
                "digest over the wire bytes would not be reproducible across "
                "downloads."
            ),
            (
                "This was the first acquisition of these bodies: the frozen "
                "manifest carried null digests, so these digests were observed "
                "here and frozen into the advanced manifest, where every later "
                "read verifies against them."
            ),
            (
                "The extraction parser has NOT run over these filings. No "
                "extraction, comparison, packet, or label exists for this "
                "corpus, and no generalization claim is supported until it "
                "does — with the parser unchanged."
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
            "python scripts/acquire_real_filing_holdout.py --allow-network "
            "(requires SEC_USER_AGENT; never run in CI)"
        ),
    }
