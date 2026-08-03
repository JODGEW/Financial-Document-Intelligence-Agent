"""Acquisition, corpus-build, and annotation-packet tests.

Offline by construction. HTTP is a fully injected transport — the real
``urllib`` path is never entered, and a test that tried to would fail on the
network-disabled gate before resolving anything. Clock and sleep are injected
too, so pacing, bounded retry, and Retry-After are exercised deterministically
without a test ever sleeping.

Corpus builds run the REAL ingestion, section-identification, and comparison
paths over tiny synthetic HTML filings in temporary directories: no AWS, no
Bedrock, no embeddings at query time, no network.
"""

from __future__ import annotations

import gzip
import json
import zlib
from pathlib import Path

import pytest

import real_filing_acquisition as rfa
import real_filing_benchmark as rfb
from scripts import build_real_filing_benchmark as builder
from scripts import create_real_filing_annotation_packets as packets
from scripts import fetch_real_filing_benchmark as fetch_cli
from tests.helpers import real_filing_fixtures as fx

GOOD_AGENT = "Jane Doe Research jane.doe@university.edu"


# --- Transport doubles --------------------------------------------------------


class _ScriptedTransport:
    """Returns queued responses (or raises queued exceptions) in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, *, headers, timeout):
        self.calls.append((url, headers))
        if not self.script:
            raise AssertionError("transport called more times than scripted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClock:
    """Monotonic clock that only advances when something sleeps."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _fetcher(script, *, allow_network=True, **overrides):
    clock = _FakeClock()
    transport = _ScriptedTransport(script)
    fetcher = rfa.Fetcher(
        user_agent=GOOD_AGENT,
        allow_network=allow_network,
        transport=transport,
        clock=clock,
        sleep=clock.sleep,
        **overrides,
    )
    return fetcher, transport, clock


def _ok(body: bytes, **headers) -> rfa.Response:
    return rfa.Response(status=200, headers=headers, body=body)


# --- Network gating -----------------------------------------------------------


def test_network_is_disabled_by_default():
    fetcher = rfa.Fetcher(user_agent=GOOD_AGENT)
    assert fetcher.allow_network is False
    with pytest.raises(rfa.NetworkDisabled) as excinfo:
        fetcher.get("https://www.sec.gov/Archives/edgar/data/1/x/y.htm")
    assert excinfo.value.code == rfa.FAILURE_NETWORK_DISABLED


def test_disabled_fetcher_never_reaches_the_transport():
    fetcher, transport, _clock = _fetcher([_ok(b"x")], allow_network=False)
    with pytest.raises(rfa.NetworkDisabled):
        fetcher.get("https://www.sec.gov/Archives/edgar/data/1/x/y.htm")
    assert transport.calls == []


def test_cli_refuses_without_the_network_flag(capsys):
    assert fetch_cli.main([]) == 2
    assert "Network access is disabled" in capsys.readouterr().err


# --- User agent ---------------------------------------------------------------


def test_missing_user_agent_is_rejected(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setattr("config.SEC_USER_AGENT", None, raising=False)
    with pytest.raises(rfa.UserAgentRejected) as excinfo:
        rfa.resolve_user_agent()
    assert excinfo.value.code == rfa.FAILURE_INVALID_USER_AGENT


@pytest.mark.parametrize(
    "value",
    [
        "your-email@example.com",
        "Company Name your.name@example.org",
        "TODO set-me@changeme.test",
        "<your name> <your@email>",
        "Research team sample@company.test",
    ],
)
def test_placeholder_user_agents_are_rejected(value):
    with pytest.raises(rfa.UserAgentRejected):
        rfa.validate_user_agent(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "short@a.io",
        "no-contact-address-at-all-here",
        "jane.doe@university.edu",  # a mailbox is not a requester
    ],
)
def test_malformed_user_agents_are_rejected(value):
    with pytest.raises(rfa.UserAgentRejected):
        rfa.validate_user_agent(value)


def test_descriptive_user_agent_is_accepted_and_sent():
    fetcher, transport, _clock = _fetcher([_ok(b"body")])
    fetcher.get("https://www.sec.gov/Archives/edgar/data/1/x/y.htm")
    _url, headers = transport.calls[0]
    assert headers["User-Agent"] == GOOD_AGENT


def test_environment_user_agent_is_used(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", GOOD_AGENT)
    assert rfa.resolve_user_agent() == GOOD_AGENT


# --- Official-source enforcement ----------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.sec.gov.attacker.test/Archives/x.htm",
        "https://sec.gov.example.test/Archives/x.htm",
        "http://www.sec.gov/Archives/x.htm",
        "https://mirror.example.test/Archives/x.htm",
        "https://user:pass@www.sec.gov/Archives/x.htm",
        "https://www.sec.gov:8443/Archives/x.htm",
        "ftp://www.sec.gov/Archives/x.htm",
    ],
)
def test_non_official_hosts_are_refused(url):
    fetcher, transport, _clock = _fetcher([])
    with pytest.raises(rfa.NonOfficialSource):
        fetcher.get(url)
    assert transport.calls == []


def test_official_hosts_are_matched_exactly_not_by_suffix():
    assert rfa.require_official_url(
        "https://www.sec.gov/Archives/edgar/data/1/x/y.htm"
    )
    assert rfa.require_official_url("https://data.sec.gov/submissions/CIK1.json")


# --- Pacing, retry, Retry-After ----------------------------------------------


def test_requests_are_paced_by_the_configured_interval():
    fetcher, _transport, clock = _fetcher(
        [_ok(b"a"), _ok(b"b")], min_interval_seconds=1.5
    )
    fetcher.get("https://www.sec.gov/Archives/edgar/data/1/x/a.htm")
    fetcher.get("https://www.sec.gov/Archives/edgar/data/1/x/b.htm")
    assert clock.slept == [1.5]


def test_transport_failures_retry_up_to_the_bound_then_raise():
    import urllib.error

    failures = [urllib.error.URLError("boom") for _ in range(3)]
    fetcher, transport, clock = _fetcher(
        failures, max_attempts=3, backoff_seconds=(2.0, 5.0), min_interval_seconds=0
    )
    with pytest.raises(rfa.AcquisitionError) as excinfo:
        fetcher.get("https://www.sec.gov/Archives/edgar/data/1/x/y.htm")
    assert excinfo.value.code == rfa.FAILURE_TRANSPORT
    assert len(transport.calls) == 3  # bounded: not unbounded retry
    assert clock.slept == [2.0, 5.0]


def test_transient_status_retries_and_then_succeeds():
    fetcher, transport, clock = _fetcher(
        [rfa.Response(status=503), _ok(b"payload")],
        max_attempts=3,
        min_interval_seconds=0,
    )
    response = fetcher.get("https://www.sec.gov/Archives/edgar/data/1/x/y.htm")
    assert response.body == b"payload"
    assert len(transport.calls) == 2
    assert clock.slept == [2.0]


@pytest.mark.parametrize("status", [403, 404, 400, 410])
def test_non_transient_statuses_fail_immediately(status):
    fetcher, transport, _clock = _fetcher(
        [rfa.Response(status=status)], max_attempts=3, min_interval_seconds=0
    )
    with pytest.raises(rfa.AcquisitionError) as excinfo:
        fetcher.get("https://www.sec.gov/Archives/edgar/data/1/x/y.htm")
    assert excinfo.value.code == rfa.FAILURE_HTTP_STATUS
    assert len(transport.calls) == 1


def test_retry_after_header_is_honored_with_the_injected_clock():
    fetcher, _transport, clock = _fetcher(
        [
            rfa.Response(status=429, headers={"Retry-After": "7"}),
            _ok(b"payload"),
        ],
        max_attempts=3,
        min_interval_seconds=0,
    )
    fetcher.get("https://www.sec.gov/Archives/edgar/data/1/x/y.htm")
    assert clock.slept == [7.0]


def test_retry_after_is_capped():
    fetcher, _transport, clock = _fetcher(
        [
            rfa.Response(status=503, headers={"Retry-After": "99999"}),
            _ok(b"payload"),
        ],
        max_attempts=2,
        min_interval_seconds=0,
    )
    fetcher.get("https://www.sec.gov/Archives/edgar/data/1/x/y.htm")
    assert clock.slept == [rfa.MAX_RETRY_AFTER_SECONDS]


def test_unparseable_retry_after_falls_back_to_configured_backoff():
    fetcher, _transport, clock = _fetcher(
        [
            rfa.Response(
                status=503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
            ),
            _ok(b"payload"),
        ],
        max_attempts=2,
        backoff_seconds=(3.0,),
        min_interval_seconds=0,
    )
    fetcher.get("https://www.sec.gov/Archives/edgar/data/1/x/y.htm")
    assert clock.slept == [3.0]


def test_timeout_is_passed_to_the_transport():
    fetcher, transport, _clock = _fetcher([_ok(b"x")], timeout_seconds=12.5)
    fetcher.get("https://www.sec.gov/Archives/edgar/data/1/x/y.htm")
    assert fetcher.timeout_seconds == 12.5


def test_acquisition_retry_policy_is_independent_of_detection_job_retry():
    import detection_job_retry

    assert (
        rfa.ACQUISITION_RETRY_POLICY["policy_id"]
        != detection_job_retry.POLICY["policy_id"]
    )
    assert "detection" not in rfa.ACQUISITION_RETRY_POLICY["policy_id"]


# --- Content-Encoding ---------------------------------------------------------


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


@pytest.mark.parametrize(
    "encoding,wire",
    [
        ("gzip", gzip.compress),
        ("x-gzip", gzip.compress),
        ("deflate", zlib.compress),
        ("deflate", _raw_deflate),
        ("identity", lambda data: data),
        (None, lambda data: data),
    ],
)
def test_content_encoding_is_decoded_before_anything_is_hashed(encoding, wire):
    """The request advertises gzip and SEC honors it, but urllib does not decode
    transfer encodings. Hashing the compressed stream would freeze a digest of a
    gzip container rather than of the filing — and gzip headers carry an mtime,
    so that digest would not even be reproducible across two identical
    downloads."""
    body = fx.PREVIOUS_HTML.encode("utf-8")
    assert rfa.decode_content_encoding(wire(body), encoding) == body


def test_unsupported_content_encoding_is_refused_not_passed_through():
    with pytest.raises(rfa.AcquisitionError) as excinfo:
        rfa.decode_content_encoding(b"whatever", "br")
    assert excinfo.value.code == rfa.FAILURE_UNSUPPORTED_ENCODING


def test_malformed_content_encoding_is_refused():
    with pytest.raises(rfa.AcquisitionError) as excinfo:
        rfa.decode_content_encoding(b"\x1f\x8b\x08not-a-real-stream", "gzip")
    assert excinfo.value.code == rfa.FAILURE_MALFORMED_ENCODING


def test_decompression_is_bounded_on_the_decompressed_size(corpus):
    """A small transfer that expands past the cap is refused by the caller's
    normal size check rather than streamed into memory."""
    oversized = b"\x00" * (rfa.MAX_DOCUMENT_BYTES + 4096)
    decoded = rfa.decode_content_encoding(gzip.compress(oversized), "gzip")
    assert len(decoded) > rfa.MAX_DOCUMENT_BYTES

    document = fx.single_pair_manifest()
    fetcher, _transport, _clock = _fetcher([_ok(decoded)])
    outcome = rfa.acquire_side(
        fetcher=fetcher,
        layout=corpus,
        pair_id="pair-01",
        side="previous",
        side_payload=_side(document, "previous"),
    )
    assert outcome["failure_code"] == rfa.FAILURE_RESPONSE_TOO_LARGE
    assert not outcome["verified"]


def test_a_compressed_filing_verifies_against_the_uncompressed_digest(corpus):
    """End to end: the manifest digest is the digest of the FILING, so a
    gzip-encoded response must still verify."""
    document = fx.single_pair_manifest()
    body = fx.PREVIOUS_HTML.encode("utf-8")
    expected = _side(document, "previous")["expected_sha256"]
    assert expected == rfb.sha256_bytes(body)

    decoded = rfa.decode_content_encoding(gzip.compress(body), "gzip")
    fetcher, _transport, _clock = _fetcher([_ok(decoded)])
    outcome = rfa.acquire_side(
        fetcher=fetcher,
        layout=corpus,
        pair_id="pair-01",
        side="previous",
        side_payload=_side(document, "previous"),
    )
    assert outcome["verified"]
    assert outcome["observed_sha256"] == expected


# --- Download, checksums, caching --------------------------------------------


@pytest.fixture
def corpus(tmp_path):
    return rfb.CorpusLayout(tmp_path / "benchmark_data")


def _side(document: dict, side: str) -> dict:
    return document["pairs"][0][side]


def test_verified_download_is_written_atomically_and_recorded(corpus):
    document = fx.single_pair_manifest()
    body = fx.PREVIOUS_HTML.encode("utf-8")
    fetcher, _transport, _clock = _fetcher([_ok(body, **{"Content-Type": "text/html"})])

    outcome = rfa.acquire_side(
        fetcher=fetcher,
        layout=corpus,
        pair_id="pair-01",
        side="previous",
        side_payload=_side(document, "previous"),
    )
    assert outcome["outcome"] == rfa.OUTCOME_DOWNLOADED
    assert outcome["verified"] is True

    target = corpus.source_file("pair-01", "previous", "fictional-20220930.htm")
    assert target.read_bytes() == body
    # No partial/temp artefacts survive an atomic write.
    assert [item.name for item in target.parent.iterdir() if item.name.startswith(".")] == []

    metadata = json.loads(
        corpus.acquisition_metadata_path("pair-01", "previous").read_text()
    )
    assert metadata["observed_sha256"] == fx.sha256(fx.PREVIOUS_HTML)
    assert metadata["official_source_url"].startswith("https://www.sec.gov/")
    assert metadata["acquired_at"]
    assert metadata["verified"] is True


def test_checksum_mismatch_refuses_and_writes_nothing(corpus):
    document = fx.single_pair_manifest()
    fetcher, _transport, _clock = _fetcher([_ok(b"<html>different bytes</html>")])
    outcome = rfa.acquire_side(
        fetcher=fetcher,
        layout=corpus,
        pair_id="pair-01",
        side="previous",
        side_payload=_side(document, "previous"),
    )
    assert outcome["outcome"] == rfa.OUTCOME_FAILED
    assert outcome["failure_code"] == rfa.FAILURE_CHECKSUM_MISMATCH
    assert not corpus.source_file(
        "pair-01", "previous", "fictional-20220930.htm"
    ).exists()


def test_verified_cache_is_reused_without_a_request(corpus):
    document = fx.single_pair_manifest()
    fx.seed_sources(corpus, document, {("pair-01", "previous"): fx.PREVIOUS_HTML,
                                       ("pair-01", "current"): fx.CURRENT_HTML})
    fetcher, transport, _clock = _fetcher([])
    outcome = rfa.acquire_side(
        fetcher=fetcher,
        layout=corpus,
        pair_id="pair-01",
        side="previous",
        side_payload=_side(document, "previous"),
    )
    assert outcome["outcome"] == rfa.OUTCOME_CACHED
    assert outcome["verified"] is True
    assert transport.calls == []


def test_cached_content_mismatch_refuses_silent_replacement(corpus):
    document = fx.single_pair_manifest()
    target = corpus.source_file("pair-01", "previous", "fictional-20220930.htm")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("<html>stale local copy</html>", encoding="utf-8")

    fetcher, transport, _clock = _fetcher([])
    outcome = rfa.acquire_side(
        fetcher=fetcher,
        layout=corpus,
        pair_id="pair-01",
        side="previous",
        side_payload=_side(document, "previous"),
    )
    assert outcome["failure_code"] == rfa.FAILURE_CACHED_CONTENT_MISMATCH
    # The disagreeing local file is preserved, not overwritten.
    assert target.read_text() == "<html>stale local copy</html>"
    assert transport.calls == []


def test_placeholder_hash_is_not_verified_without_the_explicit_flag(corpus):
    document = fx.single_pair_manifest(status=rfb.STATUS_PROPOSED)
    _side(document, "previous")["expected_sha256"] = rfb.PLACEHOLDER_SHA256
    fetcher, transport, _clock = _fetcher([])
    outcome = rfa.acquire_side(
        fetcher=fetcher,
        layout=corpus,
        pair_id="pair-01",
        side="previous",
        side_payload=_side(document, "previous"),
    )
    assert outcome["failure_code"] == rfa.FAILURE_PLACEHOLDER_HASH
    assert transport.calls == []


def test_record_hashes_downloads_but_never_claims_verification(corpus):
    document = fx.single_pair_manifest(status=rfb.STATUS_PROPOSED)
    _side(document, "previous")["expected_sha256"] = rfb.PLACEHOLDER_SHA256
    body = fx.PREVIOUS_HTML.encode("utf-8")
    fetcher, _transport, _clock = _fetcher([_ok(body)])
    outcome = rfa.acquire_side(
        fetcher=fetcher,
        layout=corpus,
        pair_id="pair-01",
        side="previous",
        side_payload=_side(document, "previous"),
        accept_unverified_hash=True,
    )
    assert outcome["outcome"] == rfa.OUTCOME_DOWNLOADED
    assert outcome["verified"] is False
    assert outcome["observed_sha256"] == fx.sha256(fx.PREVIOUS_HTML)


def test_empty_response_is_a_failure(corpus):
    document = fx.single_pair_manifest()
    fetcher, _transport, _clock = _fetcher([_ok(b"")])
    outcome = rfa.acquire_side(
        fetcher=fetcher,
        layout=corpus,
        pair_id="pair-01",
        side="previous",
        side_payload=_side(document, "previous"),
    )
    assert outcome["failure_code"] == rfa.FAILURE_EMPTY_RESPONSE


def test_acquire_manifest_reports_a_failure_without_aborting_the_run(corpus):
    """One bad source must not hide what else was or was not verified."""
    document = fx.single_pair_manifest()
    fetcher, _transport, _clock = _fetcher(
        [_ok(b"<html>wrong bytes</html>"), _ok(fx.CURRENT_HTML.encode())],
        min_interval_seconds=0,
    )
    report = rfa.acquire_manifest(document, fetcher=fetcher, layout=corpus)
    assert report["requested_filings"] == 2
    assert report["verified_filings"] == 1
    assert report["failed"] == 1
    assert report["all_verified"] is False
    codes = {item.get("failure_code") for item in report["filings"]}
    assert rfa.FAILURE_CHECKSUM_MISMATCH in codes


def test_acquire_manifest_rejects_an_unknown_pair_id(corpus):
    document = fx.single_pair_manifest()
    fetcher, _transport, _clock = _fetcher([])
    with pytest.raises(rfa.AcquisitionError) as excinfo:
        rfa.acquire_manifest(
            document, fetcher=fetcher, layout=corpus, pair_ids=["pair-99"]
        )
    assert excinfo.value.code == "unknown_pair_id"


def test_slate_resolution_writes_a_local_proposal_and_never_edits_the_manifest(
    corpus, tmp_path
):
    document = fx.single_pair_manifest()
    manifest_path = fx.write_manifest(tmp_path, document)
    before = manifest_path.read_bytes()
    submissions = json.dumps(
        {
            "name": "Fictional Benchmark Issuer One, Inc.",
            "filings": {
                "recent": {
                    "form": ["10-K", "10-K/A", "8-K"],
                    "accessionNumber": [
                        "0000000001-23-000001",
                        "0000000001-23-000002",
                        "0000000001-23-000003",
                    ],
                    "filingDate": ["2023-11-01", "2023-12-01", "2023-05-01"],
                    "reportDate": ["2023-09-30", "2023-09-30", "2023-04-30"],
                    "primaryDocument": ["a.htm", "b.htm", "c.htm"],
                }
            },
        }
    ).encode("utf-8")
    fetcher, transport, _clock = _fetcher([_ok(submissions)], min_interval_seconds=0)

    report = fetch_cli.resolve_slate(document, fetcher, corpus)
    assert report["manifest_modified"] is False
    assert manifest_path.read_bytes() == before
    assert report["resolved_slate_entries"] == 1
    candidates = report["proposals"][0]["annual_filings"]
    # Amendments are excluded by the frozen selection protocol.
    assert [row["form"] for row in candidates] == ["10-K"]
    assert candidates[0]["accession_number"] == "0000000001-23-000001"
    assert (corpus.root / fetch_cli.RESOLUTION_FILE).exists()
    assert transport.calls[0][0] == "https://data.sec.gov/submissions/CIK0000000001.json"


def _submissions(recent_forms, *, files=None, name="Fictional Issuer, Inc."):
    def block(forms, tag):
        return {
            "form": list(forms),
            "accessionNumber": [
                f"000000000{tag}-2{i}-00000{i}" for i in range(len(forms))
            ],
            "filingDate": [f"20{20 + i}-03-01" for i in range(len(forms))],
            "reportDate": [f"20{20 + i}-01-31" for i in range(len(forms))],
            "primaryDocument": [f"doc{tag}{i}.htm" for i in range(len(forms))],
        }

    payload = {"name": name, "filings": {"recent": block(recent_forms, 1)}}
    if files:
        payload["filings"]["files"] = [{"name": n} for n in files]
    return payload, block


def test_slate_resolution_reads_paged_filing_history_not_just_recent(corpus):
    """`filings.recent` truncates a high-volume filer's 10-K history — JPMorgan
    exposes one 10-K row there. A candidate list built from `recent` alone
    looks complete and is not."""
    payload, block = _submissions(["10-K", "8-K"], files=["CIK0000000001-submissions-001.json"])
    older = block(["10-K", "10-K/A", "10-K"], 2)
    document = fx.single_pair_manifest()
    fetcher, transport, _clock = _fetcher(
        [
            _ok(json.dumps(payload).encode("utf-8")),
            _ok(json.dumps(older).encode("utf-8")),
        ],
        min_interval_seconds=0,
    )

    report = fetch_cli.resolve_slate(document, fetcher, corpus)
    proposal = report["proposals"][0]
    # 1 primary 10-K from recent + 2 from the paged file; the 10-K/A is excluded.
    assert len(proposal["annual_filings"]) == 3
    assert {row["form"] for row in proposal["annual_filings"]} == {"10-K"}
    assert proposal["history_complete"] is True
    assert proposal["unread_history_files"] == []
    assert transport.calls[1][0] == (
        "https://data.sec.gov/submissions/CIK0000000001-submissions-001.json"
    )


def test_unreadable_history_page_is_reported_not_silently_dropped(corpus):
    payload, _block = _submissions(["10-K"], files=["CIK0000000001-submissions-001.json"])
    document = fx.single_pair_manifest()
    fetcher, _transport, _clock = _fetcher(
        [_ok(json.dumps(payload).encode("utf-8")), _ok(b"not json at all")],
        min_interval_seconds=0,
    )

    report = fetch_cli.resolve_slate(document, fetcher, corpus)
    proposal = report["proposals"][0]
    assert proposal["history_complete"] is False
    assert proposal["unread_history_files"] == ["CIK0000000001-submissions-001.json"]
    codes = [item["code"] for item in report["unresolved"]]
    assert "filing_history_incomplete" in codes


def test_slate_resolution_refuses_to_guess_a_missing_cik(corpus):
    document = fx.manifest(pairs=[], slate=fx.issuer_slate(count=1, resolved=0))
    fetcher, transport, _clock = _fetcher([])
    report = fetch_cli.resolve_slate(document, fetcher, corpus)
    assert report["resolved_slate_entries"] == 0
    assert report["unresolved"][0]["code"] == "cik_unknown"
    assert "fabricate an identity" in report["unresolved"][0]["detail"]
    assert transport.calls == []


def test_acquisition_report_contains_no_filing_content_or_absolute_paths(corpus):
    document = fx.single_pair_manifest()
    fetcher, _transport, _clock = _fetcher(
        [_ok(fx.PREVIOUS_HTML.encode()), _ok(fx.CURRENT_HTML.encode())]
    )
    report = rfa.acquire_manifest(document, fetcher=fetcher, layout=corpus)
    blob = json.dumps(report)
    assert report["all_verified"] is True
    assert "Cybersecurity and Data Security Risks" not in blob
    assert "<html" not in blob
    assert str(corpus.root) not in blob
    for item in report["filings"]:
        assert not Path(item["source_path"]).is_absolute()


# --- Corpus build -------------------------------------------------------------


@pytest.fixture
def built_pair(tmp_path):
    """A fully built single-pair corpus from synthetic HTML filings."""
    document = fx.single_pair_manifest()
    manifest_path = fx.write_manifest(tmp_path, document)
    layout = rfb.CorpusLayout(tmp_path / "benchmark_data")
    fx.seed_sources(
        layout,
        document,
        {
            ("pair-01", "previous"): fx.PREVIOUS_HTML,
            ("pair-01", "current"): fx.CURRENT_HTML,
        },
    )
    record = builder.build_pair(document["pairs"][0], document, layout)
    return {
        "manifest": document,
        "manifest_path": manifest_path,
        "layout": layout,
        "record": record,
    }


def test_build_extracts_item_1a_through_the_existing_section_path(built_pair):
    record = built_pair["record"]
    for side in ("previous", "current"):
        assert record[side]["extraction_outcome"] == rfb.EXTRACTION_EXTRACTED
        assert record[side]["section_hash"]
        assert record[side]["unit_count"] >= 3
        assert "Item 1A" in (record[side]["heading_detected"] or "")
        assert record[side]["section_char_count"] > 0
        assert record[side]["parse_status"] == "parsed"


def test_build_records_bounded_structural_metadata_only(built_pair):
    record = built_pair["record"]
    blob = json.dumps(record)
    # Section text never enters the record; unit excerpts are bounded.
    assert "Item 1B" not in blob
    for side in ("previous", "current"):
        for unit in record[side]["units"]:
            assert len(unit["excerpt"]) <= rfb.MAX_EXCERPT_CHARS
            assert len(unit["heading"]) <= rfb.MAX_HEADING_CHARS
    assert record["parser_versions"]["section_key"] == "item_1a_risk_factors"


def test_extracted_section_text_stays_in_the_gitignored_corpus_directory(built_pair):
    layout = built_pair["layout"]
    for side in ("previous", "current"):
        path = layout.section_text_path("pair-01", side)
        assert path.exists()
        assert layout.root in path.parents


def test_build_is_deterministic_over_identical_inputs(tmp_path):
    document = fx.single_pair_manifest()
    hashes = []
    for index in range(2):
        layout = rfb.CorpusLayout(tmp_path / f"corpus-{index}")
        fx.seed_sources(
            layout,
            document,
            {
                ("pair-01", "previous"): fx.PREVIOUS_HTML,
                ("pair-01", "current"): fx.CURRENT_HTML,
            },
        )
        record = builder.build_pair(document["pairs"][0], document, layout)
        hashes.append(record["build_hash"])
    assert hashes[0] == hashes[1]


def test_unit_ids_and_section_hashes_are_deterministic(tmp_path):
    document = fx.single_pair_manifest()
    identities = []
    for index in range(2):
        layout = rfb.CorpusLayout(tmp_path / f"corpus-{index}")
        fx.seed_sources(
            layout,
            document,
            {
                ("pair-01", "previous"): fx.PREVIOUS_HTML,
                ("pair-01", "current"): fx.CURRENT_HTML,
            },
        )
        record = builder.build_pair(document["pairs"][0], document, layout)
        identities.append(
            (
                record["previous"]["section_hash"],
                record["current"]["section_hash"],
                rfb.build_unit_ids(record, "previous"),
                rfb.build_unit_ids(record, "current"),
            )
        )
    assert identities[0] == identities[1]


def test_build_refuses_checksum_drift(tmp_path):
    document = fx.single_pair_manifest()
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    fx.seed_sources(
        layout,
        document,
        {
            ("pair-01", "previous"): fx.PREVIOUS_HTML.replace("41%", "42%"),
            ("pair-01", "current"): fx.CURRENT_HTML,
        },
    )
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        builder.build_pair(document["pairs"][0], document, layout)
    assert excinfo.value.code == "source_checksum_drift"


def test_build_refuses_a_missing_source(tmp_path):
    document = fx.single_pair_manifest()
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        builder.build_pair(document["pairs"][0], document, layout)
    assert excinfo.value.code == "source_not_acquired"


def test_build_refuses_a_placeholder_digest(tmp_path):
    document = fx.single_pair_manifest(status=rfb.STATUS_PROPOSED)
    document["pairs"][0]["previous"]["expected_sha256"] = rfb.PLACEHOLDER_SHA256
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    fx.seed_sources(
        layout,
        document,
        {
            ("pair-01", "previous"): fx.PREVIOUS_HTML,
            ("pair-01", "current"): fx.CURRENT_HTML,
        },
    )
    with pytest.raises(rfb.BenchmarkError) as excinfo:
        builder.build_pair(document["pairs"][0], document, layout)
    assert excinfo.value.code == "source_hash_is_placeholder"


def test_missing_section_is_recorded_not_hidden(tmp_path):
    document = fx.single_pair_manifest(current_html=fx.NO_SECTION_HTML)
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    fx.seed_sources(
        layout,
        document,
        {
            ("pair-01", "previous"): fx.PREVIOUS_HTML,
            ("pair-01", "current"): fx.NO_SECTION_HTML,
        },
    )
    record = builder.build_pair(document["pairs"][0], document, layout)
    assert record["current"]["extraction_outcome"] == rfb.EXTRACTION_MISSING
    assert record["current"]["unit_count"] == 0
    assert rfb.build_is_evaluable(record) is False
    # A pair that did not extract does not silently run the workflow.
    assert record["execution"]["executed"] is False


def test_ambiguous_section_is_recorded_not_guessed(tmp_path):
    document = fx.single_pair_manifest(current_html=fx.AMBIGUOUS_SECTION_HTML)
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    fx.seed_sources(
        layout,
        document,
        {
            ("pair-01", "previous"): fx.PREVIOUS_HTML,
            ("pair-01", "current"): fx.AMBIGUOUS_SECTION_HTML,
        },
    )
    record = builder.build_pair(document["pairs"][0], document, layout)
    assert record["current"]["extraction_outcome"] == rfb.EXTRACTION_AMBIGUOUS
    assert "non-contiguous" in record["current"]["extraction_detail"]


def test_build_runs_the_existing_comparison_workflow(built_pair):
    execution = built_pair["record"]["execution"]
    assert execution["executed"] is True
    assert execution["lifecycle"] == "detected"
    assert execution["detector_version"] == "item1a_detector.v3"
    assert execution["change_count"] >= 1
    assert execution["evidence_total"] >= 1
    assert execution["evidence_unresolved"] == 0
    assert execution["evidence_foreign"] == 0


def test_build_never_embeds_a_query(built_pair):
    """The fixture embeddings raise on embed_query; reaching detection at all
    proves the detector read sections by metadata."""
    assert built_pair["record"]["execution"]["executed"] is True


def test_build_cli_reports_relative_paths_only(tmp_path, capsys):
    document = fx.single_pair_manifest()
    manifest_path = fx.write_manifest(tmp_path, document)
    corpus_dir = tmp_path / "corpus"
    layout = rfb.CorpusLayout(corpus_dir)
    fx.seed_sources(
        layout,
        document,
        {
            ("pair-01", "previous"): fx.PREVIOUS_HTML,
            ("pair-01", "current"): fx.CURRENT_HTML,
        },
    )
    code = builder.main(
        ["--manifest", str(manifest_path), "--corpus-dir", str(corpus_dir), "--json"]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert str(corpus_dir) not in output
    assert "Cybersecurity" not in output
    payload = json.loads(output)
    assert payload["pairs_built"] == 1


# --- Annotation packets -------------------------------------------------------


def test_packets_are_machine_proposed_and_never_gold(built_pair):
    packet, annotation = packets.build_packet(
        "pair-01", built_pair["layout"], built_pair["manifest"]
    )
    assert annotation["annotation_status"] == rfb.ANNOTATION_MACHINE_PROPOSED
    assert annotation["annotator_id"] is None
    assert annotation["verification_timestamp"] is None
    assert rfb.is_gold(annotation) is False
    assert packet["human_verification_required"] is True
    assert "NOT GROUND TRUTH" in packet["banner"]
    for entry in packet["alignments"]:
        assert entry["proposal_source"] == rfb.ANNOTATION_MACHINE_PROPOSED


def test_packet_labels_bind_to_deterministic_unit_ids_and_section_hashes(built_pair):
    record = built_pair["record"]
    _packet, annotation = packets.build_packet(
        "pair-01", built_pair["layout"], built_pair["manifest"]
    )
    assert annotation["previous_section_hash"] == record["previous"]["section_hash"]
    assert annotation["current_section_hash"] == record["current"]["section_hash"]
    known = set(rfb.build_unit_ids(record, "previous")) | set(
        rfb.build_unit_ids(record, "current")
    )
    for label in annotation["labels"]:
        for field in ("previous_unit_id", "current_unit_id"):
            if label[field] is not None:
                assert label[field] in known


def test_packet_excerpts_are_bounded(built_pair):
    packet, _annotation = packets.build_packet(
        "pair-01", built_pair["layout"], built_pair["manifest"]
    )
    for entry in packet["alignments"]:
        for side in ("previous", "current"):
            excerpt = entry[f"{side}_excerpt"]
            if excerpt is not None:
                assert len(excerpt) <= rfb.MAX_EXCERPT_CHARS


def test_packet_cli_writes_json_and_markdown_and_claims_zero_gold(
    built_pair, capsys
):
    code = packets.main(
        [
            "--manifest",
            str(built_pair["manifest_path"]),
            "--corpus-dir",
            str(built_pair["layout"].root),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["gold_labels_produced"] == 0
    assert payload["packets_written"] == 1
    layout = built_pair["layout"]
    assert layout.packet_json_path("pair-01").exists()
    markdown = layout.packet_markdown_path("pair-01").read_text(encoding="utf-8")
    assert "MACHINE-PROPOSED" in markdown
    assert "human_verified" in markdown


def test_import_validator_accepts_a_human_verified_file(built_pair, tmp_path):
    layout = built_pair["layout"]
    _packet, annotation = packets.build_packet(
        "pair-01", layout, built_pair["manifest"]
    )
    verified = fx.human_verify(annotation)
    rfb.write_json_atomic(layout.annotation_path("pair-01"), verified)
    verdict = packets.validate_completed_annotation(
        layout.annotation_path("pair-01"), layout
    )
    assert verdict["is_gold"] is True
    assert verdict["annotator_id_basis"] == "self_asserted_local_metadata"


def test_import_validator_rejects_section_hash_drift(built_pair):
    layout = built_pair["layout"]
    _packet, annotation = packets.build_packet(
        "pair-01", layout, built_pair["manifest"]
    )
    verified = fx.human_verify(annotation)
    verified["previous_section_hash"] = "f" * 64
    rfb.write_json_atomic(layout.annotation_path("pair-01"), verified)
    with pytest.raises(rfb.CorpusDriftError) as excinfo:
        packets.validate_completed_annotation(layout.annotation_path("pair-01"), layout)
    assert excinfo.value.code == "annotation_section_hash_drift"


def test_import_validator_rejects_unknown_unit_references(built_pair):
    layout = built_pair["layout"]
    _packet, annotation = packets.build_packet(
        "pair-01", layout, built_pair["manifest"]
    )
    verified = fx.human_verify(annotation)
    verified["labels"][0]["current_unit_id"] = "current:099:invented-unit"
    verified["labels"][0]["previous_unit_id"] = None
    verified["labels"][0]["expected_change_type"] = "added"
    verified["labels"][0]["expected_evidence_side"] = "current"
    rfb.write_json_atomic(layout.annotation_path("pair-01"), verified)
    with pytest.raises(rfb.CorpusDriftError) as excinfo:
        packets.validate_completed_annotation(layout.annotation_path("pair-01"), layout)
    assert excinfo.value.code == "annotation_unknown_unit_reference"


def test_import_validator_rejects_a_unit_on_the_wrong_side(built_pair):
    layout = built_pair["layout"]
    record = built_pair["record"]
    _packet, annotation = packets.build_packet(
        "pair-01", layout, built_pair["manifest"]
    )
    verified = fx.human_verify(annotation)
    current_unit = rfb.build_unit_ids(record, "current")[0]
    verified["labels"][0]["previous_unit_id"] = current_unit
    verified["labels"][0]["current_unit_id"] = current_unit
    verified["labels"][0]["expected_change_type"] = "modified"
    verified["labels"][0]["expected_evidence_side"] = "both"
    rfb.write_json_atomic(layout.annotation_path("pair-01"), verified)
    with pytest.raises(rfb.CorpusDriftError) as excinfo:
        packets.validate_completed_annotation(layout.annotation_path("pair-01"), layout)
    assert excinfo.value.code == "annotation_unit_side_mismatch"
