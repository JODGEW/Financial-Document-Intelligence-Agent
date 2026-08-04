"""Offline behaviour of the v3 extraction holdout's source acquisition.

Every case runs over synthetic manifests and a mocked transport. No test here
contacts SEC EDGAR, supplies a user agent, reads a real filing, or writes into
the committed benchmarks/ tree. Fixtures are generic HTML shapes — no real
filing content exists in this repository.

The companion suite ``test_v3_holdout_source_verification.py`` covers the
committed artifacts; this one covers the mechanism that produced them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import real_filing_acquisition as rfa
import real_filing_benchmark as rfb
import real_filing_v3_holdout as rfv3
import real_filing_v3_holdout_acquisition as rfv3a

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "benchmarks" / "real_filing_v3_holdout_v1" / "manifest.json"

USER_AGENT = "Regression Suite regression.suite@holdout.test"

BODY = b"<html><body>synthetic filing document</body></html>"
OTHER_BODY = b"<html><body>synthetic filing document.</body></html>"


# --- Fixtures -------------------------------------------------------------------


def _side(accession: str, document: str, *, year: int) -> dict:
    return {
        "accession_number": accession,
        "form": "10-K",
        "filing_date": f"{year + 1}-02-20",
        "reporting_period": f"{year}-12-31",
        "primary_document": document,
        "expected_sha256": None,
        "source_verified": False,
    }


def synthetic_manifest(pair_count: int = 10) -> dict:
    """A schema-valid metadata-only manifest carrying synthetic identities.

    The committed manifest's non-pair fields are reused so the document really
    does satisfy ``validate_v3_holdout_manifest``, but every issuer, CIK,
    accession, and document name is synthetic: acquisition tests must never
    derive a real issuer's body URL, even offline with a mocked transport.
    """
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    # The committed corpus has since advanced to source_verified; acquisition
    # starts from the metadata-only shape, so rewind the status here rather
    # than depending on where the real corpus currently sits.
    document["status"] = rfv3.STATUS_HOLDOUT_FROZEN_METADATA_ONLY
    strata = ("sic-2000s", "sic-3000s", "sic-4000s", "sic-5000s", "sic-6000s")
    pairs = []
    for index in range(pair_count):
        stratum = strata[index // 2]
        ordinal = (index % 2) + 1
        cik = f"{index + 1:010d}"
        pairs.append(
            {
                "pair_id": f"{stratum}-{ordinal:02d}",
                "cik": cik,
                "issuer_name": f"SYNTHETIC ISSUER {index + 1}",
                "sic": 2000 + (index // 2) * 1000 + 11,
                "stratum_id": stratum,
                "stratum_label": strata_label(document, stratum),
                "target_previous_fiscal_year": 2024,
                "target_current_fiscal_year": 2025,
                "metadata_source_references": [
                    f"https://data.sec.gov/submissions/CIK{cik}.json"
                ],
                "previous": _side(
                    f"{index + 1:010d}-25-000001", f"syn{index}-2024.htm", year=2024
                ),
                "current": _side(
                    f"{index + 1:010d}-26-000001", f"syn{index}-2025.htm", year=2025
                ),
            }
        )
    document["pairs"] = pairs
    assert document["status"] == rfv3.STATUS_HOLDOUT_FROZEN_METADATA_ONLY
    rfv3.validate_v3_holdout_manifest(document)
    return document


def strata_label(document: dict, stratum_id: str) -> str:
    for pair in document["pairs"]:
        if pair["stratum_id"] == stratum_id:
            return pair["stratum_label"]
    raise AssertionError(stratum_id)


@pytest.fixture
def manifest():
    return synthetic_manifest()


@pytest.fixture
def layout(tmp_path):
    return rfb.CorpusLayout(tmp_path / "v3_corpus")


class RecordingTransport:
    """A mocked official transport. Records every URL it is asked for."""

    def __init__(self, *, body=BODY, status=200, headers=None, bodies=None):
        self.body = body
        self.status = status
        self.headers = headers if headers is not None else {"Content-Type": "text/html"}
        self.bodies = bodies or {}
        self.urls: list[str] = []

    def __call__(self, url, *, headers, timeout):
        self.urls.append(url)
        body = self.bodies.get(url, self.body)
        return rfa.Response(status=self.status, headers=dict(self.headers), body=body)


def make_fetcher(transport, **kwargs):
    kwargs.setdefault("min_interval_seconds", 0.0)
    return rfa.Fetcher(
        user_agent=USER_AGENT,
        allow_network=True,
        transport=transport,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        **kwargs,
    )


def acquire(manifest, layout, transport, **kwargs):
    return rfv3a.acquire_v3_manifest(
        manifest, fetcher=make_fetcher(transport, **kwargs), layout=layout
    )


def distinct_bodies(manifest) -> dict[str, bytes]:
    """One distinguishable synthetic body per frozen side.

    Twenty real filings are twenty distinct documents, so a fixture that
    served one body everywhere would exercise the duplicate-identity refusal
    rather than the success path.
    """
    return {
        rfv3a.v3_body_url(pair, pair[side]): (
            f"<html><body>synthetic filing document "
            f"{pair['pair_id']}:{side}</body></html>"
        ).encode()
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    }


# --- Manifest and URL identity --------------------------------------------------


def test_metadata_only_manifest_is_the_accepted_starting_status():
    protocol = rfv3a.source_acquisition_protocol()
    assert protocol["accepted_manifest_status"] == "holdout_frozen_metadata_only"
    assert protocol["resulting_manifest_status"] == "source_verified"


def test_committed_manifest_has_exactly_ten_pairs_and_twenty_sides():
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert len(document["pairs"]) == 10
    sides = [
        (pair["pair_id"], side)
        for pair in document["pairs"]
        for side in ("previous", "current")
    ]
    assert len(sides) == 20
    assert len(set(sides)) == 20


def test_wrong_starting_status_is_rejected():
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document["status"] = rfb.STATUS_CORPUS_BUILT
    for pair in document["pairs"]:
        for side in ("previous", "current"):
            pair[side]["expected_sha256"] = "a" * 64
            pair[side]["source_verified"] = True
    with pytest.raises(rfv3a.V3HoldoutAcquisitionError) as excinfo:
        rfv3a.verify_frozen_identity(document, repo_root=REPO_ROOT)
    assert excinfo.value.code == rfv3a.FAILURE_MANIFEST_STATUS_INVALID


def test_canonical_url_strips_cik_zeroes_and_accession_hyphens():
    pair = {"cik": "0001425205"}
    side = {
        "accession_number": "0001558370-25-001834",
        "primary_document": "iova-20241231x10k.htm",
    }
    url = rfv3a.v3_body_url(pair, side)
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/1425205/"
        "000155837025001834/iova-20241231x10k.htm"
    )


def test_every_committed_side_yields_one_canonical_official_url():
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    urls = set()
    for pair in document["pairs"]:
        for side in ("previous", "current"):
            url = rfv3a.v3_body_url(pair, pair[side])
            assert url.startswith("https://www.sec.gov/Archives/edgar/data/")
            assert url.endswith("/" + pair[side]["primary_document"])
            urls.add(url)
    assert len(urls) == 20


@pytest.mark.parametrize(
    "url",
    (
        "http://www.sec.gov/Archives/edgar/data/1/2/a.htm",  # http downgrade
        "https://sec.gov/Archives/edgar/data/1/2/a.htm",  # wrong host
        "https://www.sec.gov.evil.test/Archives/edgar/data/1/2/a.htm",  # lookalike
        "https://www.sec.gov:8443/Archives/edgar/data/1/2/a.htm",  # port override
        "https://user:pw@www.sec.gov/Archives/edgar/data/1/2/a.htm",  # credentials
        "https://data.sec.gov/Archives/edgar/data/1/2/a.htm",  # metadata host
        "https://www.sec.gov/Archives/edgar/data/1/2/a.htm?x=1",  # query
        "https://www.sec.gov/Archives/edgar/data/1/2/a.htm#frag",  # fragment
        "https://www.sec.gov/cgi-bin/browse-edgar?action=x",  # not an archive path
        "https://www.sec.gov/Archives/edgar/data/1/2/../3/a.htm",  # traversal
        "https://www.sec.gov/Archives/edgar/data/1/2/%2e%2e/a.htm",  # encoded
        "https://www.sec.gov/Archives/edgar/data/1/2/sub%2Fa.htm",  # encoded slash
        "https://www.sec.gov/Archives/edgar/data/1/2/",  # index page
        "https://www.sec.gov/Archives/edgar/data/1/2/ex/a.htm",  # nested/exhibit
    ),
)
def test_non_canonical_body_urls_are_refused(url):
    with pytest.raises(rfv3a.V3HoldoutAcquisitionError) as excinfo:
        rfv3a.require_v3_body_url(
            url, cik="0000000001", accession="0000000001-25-000002",
            primary_document="a.htm",
        )
    assert excinfo.value.code in (
        rfv3a.FAILURE_URL_INVALID,
        rfv3a.FAILURE_URL_IDENTITY_MISMATCH,
    )


@pytest.mark.parametrize(
    "url,code",
    (
        (
            "https://www.sec.gov/Archives/edgar/data/9/0000000001250000002/a.htm",
            rfv3a.FAILURE_URL_IDENTITY_MISMATCH,
        ),
        (
            "https://www.sec.gov/Archives/edgar/data/1/9999999999250000002/a.htm",
            rfv3a.FAILURE_URL_IDENTITY_MISMATCH,
        ),
        (
            "https://www.sec.gov/Archives/edgar/data/1/0000000001250000002/b.htm",
            rfv3a.FAILURE_URL_IDENTITY_MISMATCH,
        ),
        (
            "https://www.sec.gov/Archives/edgar/data/1/0000000001250000002/"
            "a-index.htm",
            rfv3a.FAILURE_URL_IDENTITY_MISMATCH,
        ),
    ),
)
def test_alternate_filing_identities_are_refused(url, code):
    with pytest.raises(rfv3a.V3HoldoutAcquisitionError) as excinfo:
        rfv3a.require_v3_body_url(
            url, cik="0000000001", accession="0000000001-25-000002",
            primary_document="a.htm",
        )
    assert excinfo.value.code == code


@pytest.mark.parametrize(
    "document",
    ("sub/a.htm", "sub\\a.htm", "../a.htm", "a%2Fb.htm", "a.exe", "a..htm"),
)
def test_unsafe_primary_document_names_never_build_a_url(document):
    with pytest.raises(rfv3a.V3HoldoutAcquisitionError) as excinfo:
        rfv3a.v3_body_url(
            {"cik": "0000000001"},
            {"accession_number": "0000000001-25-000002", "primary_document": document},
        )
    assert excinfo.value.code == rfv3a.FAILURE_URL_INVALID


@pytest.mark.parametrize(
    "cik,accession",
    (("1425205", "0001558370-25-001834"), ("0001425205", "000155837025001834")),
)
def test_non_canonical_manifest_components_never_build_a_url(cik, accession):
    with pytest.raises(rfv3a.V3HoldoutAcquisitionError) as excinfo:
        rfv3a.v3_body_url(
            {"cik": cik},
            {"accession_number": accession, "primary_document": "a.htm"},
        )
    assert excinfo.value.code == rfv3a.FAILURE_URL_INVALID


# --- Redirects ------------------------------------------------------------------


def test_redirect_to_the_exact_canonical_url_is_the_only_permitted_target():
    handler = rfv3a._IdentityBoundRedirectHandler(
        "https://www.sec.gov/Archives/edgar/data/1/2/a.htm"
    )
    for forbidden in (
        "https://www.sec.gov/Archives/edgar/data/1/2/b.htm",
        "https://www.sec.gov/Archives/edgar/data/1/3/a.htm",
        "https://www.sec.gov/Archives/edgar/data/1/2/",
        "https://data.sec.gov/submissions/CIK0000000001.json",
        "https://mirror.test/Archives/edgar/data/1/2/a.htm",
        "http://www.sec.gov/Archives/edgar/data/1/2/a.htm",
    ):
        with pytest.raises(rfv3a.V3HoldoutAcquisitionError) as excinfo:
            handler.redirect_request(None, None, 301, "moved", {}, forbidden)
        assert excinfo.value.code == rfv3a.FAILURE_REDIRECT_FORBIDDEN


def test_redirect_count_is_structurally_zero(manifest, layout):
    transport = RecordingTransport()
    result = acquire(manifest, layout, transport)
    assert result["redirect_count"] == 0


# --- Network policy -------------------------------------------------------------


def test_user_agent_is_validated_before_any_request():
    transport = RecordingTransport()
    with pytest.raises(rfa.UserAgentRejected):
        rfa.Fetcher(
            user_agent="your-email@example.com",
            allow_network=True,
            transport=transport,
        )
    assert transport.urls == []


def test_network_is_off_unless_explicitly_enabled(manifest, layout):
    fetcher = rfa.Fetcher(user_agent=USER_AGENT, transport=RecordingTransport())
    result = rfv3a.acquire_v3_manifest(manifest, fetcher=fetcher, layout=layout)
    assert result["verified_filings"] == 0
    assert {item["failure_code"] for item in result["filings"]} == {
        rfa.FAILURE_NETWORK_DISABLED
    }


def test_only_the_exact_twenty_frozen_identities_are_requested(manifest, layout):
    transport = RecordingTransport()
    acquire(manifest, layout, transport)
    expected = [
        rfv3a.v3_body_url(pair, pair[side])
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    ]
    assert transport.urls == expected
    assert len(transport.urls) == 20


def test_no_metadata_search_or_third_party_endpoint_is_contacted(manifest, layout):
    transport = RecordingTransport()
    acquire(manifest, layout, transport)
    for url in transport.urls:
        assert url.startswith("https://www.sec.gov/Archives/edgar/data/")
        for forbidden in (
            "data.sec.gov", "submissions", "companyfacts", "company_tickers",
            "cgi-bin", "browse-edgar", "full-index", "google", "bing",
            "amazonaws", "bedrock",
        ):
            assert forbidden not in url


def test_pacing_timeout_and_retry_bound_come_from_the_shared_policy():
    protocol = rfv3a.source_acquisition_protocol()
    assert protocol["request_pacing_seconds"] == (
        rfa.DEFAULT_MIN_REQUEST_INTERVAL_SECONDS
    )
    assert protocol["request_timeout_seconds"] == rfa.DEFAULT_TIMEOUT_SECONDS
    assert protocol["retry_policy"] is rfa.ACQUISITION_RETRY_POLICY
    assert protocol["max_source_bytes"] == rfa.MAX_DOCUMENT_BYTES


def test_retry_exhaustion_fails_without_advancing(manifest, layout):
    transport = RecordingTransport(status=503, headers={})
    result = acquire(manifest, layout, transport, max_attempts=3)
    assert result["all_verified"] is False
    assert result["failed"] == 20
    assert transport.urls.count(transport.urls[0]) == 3
    with pytest.raises(rfv3a.V3HoldoutAcquisitionError) as excinfo:
        rfv3a.advance_v3_holdout_manifest(manifest, result)
    assert excinfo.value.code == rfv3a.FAILURE_NOT_FULLY_VERIFIED


def test_non_transient_status_is_not_retried(manifest, layout):
    transport = RecordingTransport(status=404, headers={})
    acquire(manifest, layout, transport, max_attempts=3)
    assert transport.urls.count(transport.urls[0]) == 1


# --- Response validation and hashing --------------------------------------------


def test_successful_html_response_is_accepted_and_hashed(manifest, layout):
    transport = RecordingTransport()
    result = acquire(manifest, layout, transport)
    assert result["all_verified"] is True
    assert result["verified_filings"] == 20
    digest = hashlib.sha256(BODY).hexdigest()
    for item in result["filings"]:
        assert item["observed_sha256"] == digest
        assert item["byte_count"] == len(BODY)


@pytest.mark.parametrize(
    "transport,code",
    (
        (RecordingTransport(body=b""), rfa.FAILURE_EMPTY_RESPONSE),
        (
            RecordingTransport(headers={"Content-Type": "application/json"}),
            rfv3a.FAILURE_CONTENT_TYPE_INVALID,
        ),
        (
            RecordingTransport(
                body=b"<html>Your Request has been identified as part of a "
                b"network of automated tools</html>"
            ),
            rfv3a.FAILURE_ACCESS_DENIED_RESPONSE,
        ),
        (
            RecordingTransport(
                headers={"Content-Type": "text/html", "Content-Length": "999999"}
            ),
            rfv3a.FAILURE_INCOMPLETE_RESPONSE,
        ),
    ),
)
def test_transport_level_refusals(manifest, layout, transport, code):
    result = acquire(manifest, layout, transport)
    assert result["all_verified"] is False
    assert {item["failure_code"] for item in result["filings"]} == {code}
    assert list(layout.root.rglob("*.htm")) == []


def test_oversized_body_is_refused(manifest, layout, monkeypatch):
    monkeypatch.setattr(rfa, "MAX_DOCUMENT_BYTES", 8)
    result = acquire(manifest, layout, RecordingTransport())
    assert {item["failure_code"] for item in result["filings"]} == {
        rfa.FAILURE_RESPONSE_TOO_LARGE
    }
    assert list(layout.root.rglob("*.htm")) == []


def test_absent_content_type_is_accepted():
    assert rfv3a.content_type_is_acceptable(None) is True
    assert rfv3a.content_type_is_acceptable("") is True
    assert rfv3a.content_type_is_acceptable("text/html; charset=utf-8") is True
    assert rfv3a.content_type_is_acceptable("text/plain") is True
    assert rfv3a.content_type_is_acceptable("application/pdf") is False


def test_hash_is_over_raw_bytes_not_decoded_or_normalized_text(manifest, layout):
    crlf = b"<html>\r\n<body>synthetic\xc3\xa9 document\xef\xbb\xbf</body>\r\n</html>"
    transport = RecordingTransport(body=crlf)
    result = acquire(manifest, layout, transport)
    expected = hashlib.sha256(crlf).hexdigest()
    assert {item["observed_sha256"] for item in result["filings"]} == {expected}
    # Neither newline nor Unicode normalization happened: the persisted bytes
    # are byte-identical to what arrived.
    stored = next(layout.root.rglob("*.htm")).read_bytes()
    assert stored == crlf
    assert b"\r\n" in stored
    assert hashlib.sha256(stored).hexdigest() == expected


def test_content_encoding_is_applied_before_hashing(manifest, layout):
    import gzip

    packed = gzip.compress(BODY)
    assert packed != BODY

    class GzipTransport(RecordingTransport):
        def __call__(self, url, *, headers, timeout):
            self.urls.append(url)
            return rfa.Response(
                status=200,
                headers={"Content-Type": "text/html", "Content-Encoding": "gzip"},
                body=rfa.decode_content_encoding(packed, "gzip"),
            )

    result = acquire(manifest, layout, GzipTransport())
    assert {item["observed_sha256"] for item in result["filings"]} == {
        hashlib.sha256(BODY).hexdigest()
    }


def test_identical_bytes_hash_identically_and_one_byte_changes_it():
    assert hashlib.sha256(BODY).hexdigest() == hashlib.sha256(BODY).hexdigest()
    assert hashlib.sha256(BODY).hexdigest() != hashlib.sha256(OTHER_BODY).hexdigest()


def test_response_headers_are_not_part_of_source_identity(manifest, layout, tmp_path):
    first = acquire(manifest, layout, RecordingTransport())
    second_layout = rfb.CorpusLayout(tmp_path / "second")
    second = acquire(
        manifest,
        second_layout,
        RecordingTransport(
            headers={
                "Content-Type": "text/html; charset=iso-8859-1",
                "Server": "different",
                "Date": "Tue, 04 Aug 2026 00:00:00 GMT",
            }
        ),
    )
    assert [item["observed_sha256"] for item in first["filings"]] == [
        item["observed_sha256"] for item in second["filings"]
    ]


def test_digests_are_lowercase_64_hex(manifest, layout):
    result = acquire(manifest, layout, RecordingTransport())
    for item in result["filings"]:
        assert rfb._SHA256_RE.match(item["observed_sha256"])


def test_local_reread_mismatch_is_refused(manifest, layout, monkeypatch):
    monkeypatch.setattr(rfb, "sha256_file", lambda *_a, **_k: "f" * 64)
    result = acquire(manifest, layout, RecordingTransport())
    assert {item["failure_code"] for item in result["filings"]} == {
        rfv3a.FAILURE_LOCAL_REREAD_MISMATCH
    }
    assert result["all_verified"] is False


# --- Local storage --------------------------------------------------------------


def test_source_paths_are_deterministic_and_relative(manifest, layout):
    result = acquire(manifest, layout, RecordingTransport())
    for item in result["filings"]:
        expected = (
            f"sources/{item['pair_id']}/{item['side']}/{item['primary_document']}"
        )
        assert item["source_path"] == expected
        assert not Path(item["source_path"]).is_absolute()
    assert rfv3a.LOCAL_PATH_CONVENTION == (
        "sources/{pair_id}/{side}/{primary_document}"
    )


def test_no_temporary_file_survives_a_successful_write(manifest, layout):
    acquire(manifest, layout, RecordingTransport())
    assert [p.name for p in layout.root.rglob("*.part")] == []
    assert [p.name for p in layout.root.rglob("*.tmp")] == []


def test_existing_identical_local_file_is_reused_without_a_request(manifest, layout):
    transport = RecordingTransport()
    acquire(manifest, layout, transport)
    assert len(transport.urls) == 20

    second = RecordingTransport()
    result = acquire(manifest, layout, second)
    assert second.urls == []
    assert result["reused_verified_cache"] == 20
    assert result["all_verified"] is True


def test_existing_mismatching_local_file_is_refused_and_preserved(manifest, layout):
    acquire(manifest, layout, RecordingTransport())
    victim = next(layout.root.rglob("*.htm"))
    victim.write_bytes(OTHER_BODY)

    transport = RecordingTransport()
    result = acquire(manifest, layout, transport)
    failures = [item for item in result["filings"] if not item["verified"]]
    assert len(failures) == 1
    assert failures[0]["failure_code"] == rfa.FAILURE_CACHED_CONTENT_MISMATCH
    # Not overwritten, not deleted, and not silently re-fetched.
    assert victim.read_bytes() == OTHER_BODY
    assert result["all_verified"] is False


def test_local_file_without_any_anchor_digest_is_refused(manifest, layout):
    pair = manifest["pairs"][0]
    target = layout.source_file(
        pair["pair_id"], "previous", pair["previous"]["primary_document"]
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(OTHER_BODY)

    result = acquire(manifest, layout, RecordingTransport())
    failures = [item for item in result["filings"] if not item["verified"]]
    assert [item["failure_code"] for item in failures] == [
        rfv3a.FAILURE_CACHE_UNVERIFIABLE
    ]
    assert target.read_bytes() == OTHER_BODY


def test_remote_bytes_disagreeing_with_a_pinned_digest_never_land(manifest, layout):
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            pair[side]["expected_sha256"] = hashlib.sha256(BODY).hexdigest()
            pair[side]["source_verified"] = True
    manifest["status"] = rfb.STATUS_SOURCE_VERIFIED

    result = acquire(manifest, layout, RecordingTransport(body=OTHER_BODY))
    assert {item["failure_code"] for item in result["filings"]} == {
        rfa.FAILURE_CHECKSUM_MISMATCH
    }
    assert list(layout.root.rglob("*.htm")) == []
    # The pinned digest is untouched.
    assert manifest["pairs"][0]["previous"]["expected_sha256"] == (
        hashlib.sha256(BODY).hexdigest()
    )


def test_nothing_is_written_under_benchmarks(manifest, layout):
    acquire(manifest, layout, RecordingTransport())
    assert layout.root.exists()
    assert REPO_ROOT / "benchmarks" not in layout.root.parents


# --- All-or-nothing transition ---------------------------------------------------


def test_twenty_verified_sources_advance_the_manifest(manifest, layout):
    bodies = distinct_bodies(manifest)
    result = acquire(manifest, layout, RecordingTransport(bodies=bodies))
    advanced = rfv3a.advance_v3_holdout_manifest(manifest, result)
    assert advanced["status"] == rfb.STATUS_SOURCE_VERIFIED
    digests = set()
    for pair in advanced["pairs"]:
        for side in ("previous", "current"):
            digest = pair[side]["expected_sha256"]
            assert rfb._SHA256_RE.match(digest)
            assert pair[side]["source_verified"] is True
            digests.add(digest)
    assert len(digests) == 20


def test_one_failure_leaves_the_manifest_untouched(manifest, layout):
    only_one_fails = {
        rfv3a.v3_body_url(manifest["pairs"][3], manifest["pairs"][3]["current"]): b""
    }
    result = acquire(
        manifest, layout, RecordingTransport(bodies=only_one_fails)
    )
    assert result["failed"] == 1
    assert result["all_verified"] is False
    with pytest.raises(rfv3a.V3HoldoutAcquisitionError) as excinfo:
        rfv3a.advance_v3_holdout_manifest(manifest, result)
    assert excinfo.value.code == rfv3a.FAILURE_NOT_FULLY_VERIFIED
    # Source manifest object is unchanged: still metadata-only, still null.
    assert manifest["status"] == rfv3.STATUS_HOLDOUT_FROZEN_METADATA_ONLY
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            assert pair[side]["expected_sha256"] is None
            assert pair[side]["source_verified"] is False


def test_failure_replaces_no_pair_and_reorders_nothing(manifest, layout):
    before = [pair["pair_id"] for pair in manifest["pairs"]]
    accessions = [
        pair[side]["accession_number"]
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    ]
    acquire(manifest, layout, RecordingTransport(status=500, headers={}))
    assert [pair["pair_id"] for pair in manifest["pairs"]] == before
    assert [
        pair[side]["accession_number"]
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    ] == accessions


def test_advance_preserves_every_frozen_identity_field(manifest, layout):
    result = acquire(
        manifest, layout, RecordingTransport(bodies=distinct_bodies(manifest))
    )
    advanced = rfv3a.advance_v3_holdout_manifest(manifest, result)
    for field in (
        "benchmark_id", "benchmark_version", "form", "selection_protocol_version",
        "selection_protocol_hash", "selection_seed_identifier",
        "prior_corpus_exclusions", "frozen_parser_source_sha256",
        "frozen_detector_source_sha256", "frozen_workflow_source_sha256",
        "frozen_evaluator_source_sha256", "frozen_unit_grammar_version",
        "frozen_evaluation_contract_version", "frozen_metric_definitions_version",
        "frozen_report_contract_version", "frozen_unit_identity_contract",
        "target_previous_fiscal_year", "target_current_fiscal_year",
        "metadata_snapshot", "selected_at",
    ):
        assert advanced[field] == manifest[field], field
    assert advanced["extraction_holdout_evaluation"] is False
    assert advanced["generalization_claim_supported"] is False
    for before, after in zip(manifest["pairs"], advanced["pairs"]):
        assert before["pair_id"] == after["pair_id"]
        assert before["cik"] == after["cik"]
        assert before["issuer_name"] == after["issuer_name"]
        assert before["sic"] == after["sic"]
        assert before["stratum_id"] == after["stratum_id"]
        for side in ("previous", "current"):
            for field in (
                "accession_number", "form", "filing_date", "reporting_period",
                "primary_document",
            ):
                assert before[side][field] == after[side][field]


def test_duplicate_source_identity_is_refused(manifest, layout):
    result = acquire(manifest, layout, RecordingTransport())
    # Two sides claiming one document is a URL-construction defect.
    result["filings"][1]["official_source_url"] = result["filings"][0][
        "official_source_url"
    ]
    with pytest.raises(rfv3a.V3HoldoutAcquisitionError) as excinfo:
        rfv3a.verify_source_identity_uniqueness(result)
    assert excinfo.value.code == rfv3a.FAILURE_DUPLICATE_IDENTITY


def test_duplicate_source_digest_does_not_merge_two_sides(manifest, layout):
    # Every synthetic side returns the same body, so uniqueness must refuse it
    # rather than treating one document as two filings.
    result = acquire(manifest, layout, RecordingTransport())
    with pytest.raises(rfv3a.V3HoldoutAcquisitionError) as excinfo:
        rfv3a.verify_source_identity_uniqueness(result)
    assert excinfo.value.code == rfv3a.FAILURE_DUPLICATE_IDENTITY


def test_distinct_bodies_pass_uniqueness(manifest, layout):
    bodies = {
        rfv3a.v3_body_url(pair, pair[side]): (
            f"<html><body>synthetic {pair['pair_id']} {side}</body></html>"
        ).encode()
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    }
    result = acquire(manifest, layout, RecordingTransport(bodies=bodies))
    rfv3a.verify_source_identity_uniqueness(result)
    assert result["all_verified"] is True


def test_advanced_manifest_payload_is_deterministic(manifest, layout, tmp_path):
    bodies = {
        rfv3a.v3_body_url(pair, pair[side]): (
            f"<html><body>{pair['pair_id']}:{side}</body></html>"
        ).encode()
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    }
    first = rfv3a.advance_v3_holdout_manifest(
        manifest, acquire(manifest, layout, RecordingTransport(bodies=bodies))
    )
    second = rfv3a.advance_v3_holdout_manifest(
        manifest,
        acquire(
            manifest, rfb.CorpusLayout(tmp_path / "again"),
            RecordingTransport(bodies=bodies),
        ),
    )
    assert rfb.canonical_json(first) == rfb.canonical_json(second)


# --- Report ---------------------------------------------------------------------


def _report(manifest, acquisition, **overrides):
    payload = {
        "manifest": manifest,
        "acquisition": acquisition,
        "prior_manifest_sha256": "a" * 64,
        "new_manifest_sha256": "b" * 64,
        "new_reproducible_manifest_hash": "c" * 64,
        "generated_at": "2026-08-04T00:00:00+00:00",
    }
    payload.update(overrides)
    return rfv3a.build_source_verification_report(**payload)


def test_report_records_counts_and_structural_zeroes(manifest, layout):
    result = acquire(manifest, layout, RecordingTransport())
    report = _report(manifest, result)
    assert report["report_version"] == (
        "real-filing-v3-holdout.source-verification.v1"
    )
    assert report["verified_source_count"] == 20
    assert report["failed_source_count"] == 0
    assert report["side_count"] == 20
    assert report["pair_count"] == 10
    assert report["hash_algorithm"] == "sha256"
    for zero in (
        "extraction_runs", "comparison_runs", "annotation_packets",
        "machine_proposed_labels", "human_verified_labels",
        "gold_evaluation_runs",
    ):
        assert report[zero] == 0
    assert report["signoff_present"] is False
    assert report["extraction_holdout_evaluation"] is False
    assert report["generalization_claim_supported"] is False


def test_report_contains_no_absolute_path_credential_or_content(manifest, layout):
    result = acquire(manifest, layout, RecordingTransport())
    raw = json.dumps(_report(manifest, result))
    lowered = raw.lower()
    assert "@" not in raw
    assert str(REPO_ROOT).lower() not in lowered
    assert "/users/" not in lowered
    assert "/home/" not in lowered
    assert "c:\\" not in lowered
    assert "<html" not in lowered
    assert "synthetic filing document" not in lowered
    assert "risk factors" not in lowered
    assert "cookie" not in lowered
    for item in _report(manifest, result)["filings"]:
        assert not Path(item["local_source_path"]).is_absolute()


def test_report_ordering_follows_the_frozen_manifest(manifest, layout):
    result = acquire(manifest, layout, RecordingTransport())
    report = _report(manifest, result)
    assert report["pair_ids"] == [pair["pair_id"] for pair in manifest["pairs"]]
    assert [(item["pair_id"], item["side"]) for item in report["filings"]] == [
        (pair["pair_id"], side)
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    ]


def test_report_manifest_binding_is_validated(manifest, layout):
    result = acquire(
        manifest, layout, RecordingTransport(bodies=distinct_bodies(manifest))
    )
    advanced = rfv3a.advance_v3_holdout_manifest(manifest, result)
    report = _report(advanced, result)
    rfv3a.verify_report_manifest_binding(report, advanced)

    poisoned = json.loads(json.dumps(report))
    poisoned["filings"][0]["sha256"] = "d" * 64
    with pytest.raises(rfv3a.V3HoldoutAcquisitionError) as excinfo:
        rfv3a.verify_report_manifest_binding(poisoned, advanced)
    assert excinfo.value.code == rfv3a.FAILURE_REPORT_MANIFEST_MISMATCH


def test_failed_report_records_the_failure_and_no_success(manifest, layout):
    result = acquire(manifest, layout, RecordingTransport(status=404, headers={}))
    report = _report(manifest, result, new_manifest_sha256=None)
    assert report["verification_outcome"] == "failed"
    assert report["new_manifest_status"] == (
        rfv3.STATUS_HOLDOUT_FROZEN_METADATA_ONLY
    )
    assert report["verified_source_count"] == 0
    assert report["failure_counts_by_reason"] == {rfa.FAILURE_HTTP_STATUS: 20}
    assert all(item["source_verified"] is False for item in report["filings"])


def test_protocol_hash_is_stable_and_bounded():
    assert rfv3a.source_acquisition_protocol_hash() == (
        rfv3a.source_acquisition_protocol_hash()
    )
    raw = json.dumps(rfv3a.source_acquisition_protocol())
    assert "@" not in raw
    assert str(REPO_ROOT) not in raw


# --- Downstream prohibitions ------------------------------------------------------


def test_acquisition_import_graph_cannot_reach_extraction_or_evaluation():
    """The strongest available proof that this step ends at bytes-on-disk."""
    import sys as _sys

    seen: set[str] = set()

    def walk(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        module = _sys.modules.get(name)
        if module is None:
            return
        for value in vars(module).values():
            candidate = getattr(value, "__name__", None)
            if isinstance(value, type(_sys)) and isinstance(candidate, str):
                walk(candidate)

    walk("real_filing_v3_holdout_acquisition")
    forbidden = (
        "loaders", "ingest", "chromadb", "chroma_batching", "comparison_detector",
        "comparison_detection_worker", "comparison_governance", "langchain",
        "boto3", "agent", "tools",
    )
    for name in seen:
        root = name.split(".")[0]
        assert root not in forbidden, f"{name} is reachable from acquisition"


def test_module_source_invokes_no_parser_detector_or_evaluator():
    source = (
        REPO_ROOT / "real_filing_v3_holdout_acquisition.py"
    ).read_text(encoding="utf-8")
    # Importable names and call sites only. Bare words like "Chroma" or
    # "extraction" appear legitimately in the module's own denial prose; the
    # import-graph test above is what rules those packages out.
    for forbidden in (
        "sec_headings", "extract_item", "BeautifulSoup", "lxml", "html.parser",
        "chromadb", "boto3", "run_detection",
        "create_real_filing_annotation_packets", "eval_real_filing_benchmark",
        "real_filing_holdout_extraction",
    ):
        assert forbidden not in source, forbidden


def test_acquisition_never_creates_downstream_artifacts(manifest, layout):
    acquire(manifest, layout, RecordingTransport())
    names = {path.name for path in layout.root.rglob("*")}
    for forbidden in ("packet.json", "packet.md", "build.json", "comparisons.db"):
        assert forbidden not in names
    assert not (layout.root / "annotations").exists()
    assert not (layout.root / "results").exists()
    assert not (layout.root / "build").exists()
