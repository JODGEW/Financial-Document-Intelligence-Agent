"""Offline behavior tests for holdout body acquisition and verification.

Every test here is mocked: transports are scripted doubles, clocks are fake,
and no test can reach a network — a scripted transport raises if called more
times than declared, and the disabled-network tests assert the transport is
never consulted at all. The suite pins the safety behavior the thirteenth
Stage 3.5 commit claims: frozen identities cannot drift, only canonical
official URLs are contacted, digests are taken over decoded bytes and proven
repeatable, one failed side blocks the manifest transition, no pair is ever
replaced, and nothing downstream of bytes-on-disk can run.
"""

from __future__ import annotations

import ast
import copy
import gzip
import hashlib
import json
import urllib.request
from pathlib import Path

import pytest

import real_filing_acquisition as rfa
import real_filing_benchmark as rfb
import real_filing_holdout as rfh
import real_filing_holdout_acquisition as rfha

REPO_ROOT = Path(__file__).resolve().parent.parent
GOOD_AGENT = "Jane Doe Research jane.doe@university.edu"

COMMITTED_MANIFEST = rfh.load_holdout_manifest()


# --- Fixtures and doubles -------------------------------------------------------


def metadata_only_manifest() -> dict:
    """The committed manifest rewound to its pre-acquisition state: same
    frozen identities, null digests, metadata-only status."""
    document = copy.deepcopy(COMMITTED_MANIFEST)
    document["status"] = rfh.STATUS_HOLDOUT_FROZEN_METADATA_ONLY
    for pair in document["pairs"]:
        for side in ("previous", "current"):
            pair[side]["expected_sha256"] = None
            pair[side]["source_verified"] = False
    rfh.validate_holdout_manifest(document)
    return document


def body_for(pair_id: str, side: str) -> bytes:
    """Deterministic synthetic filing body — obviously not a real filing."""
    return (
        f"<synthetic-holdout-fixture pair={pair_id} side={side}/>\n".encode()
        * 40
    )


def scripted_bodies(manifest: dict) -> dict[str, bytes]:
    return {
        rfha.holdout_body_url(pair, pair[side]): body_for(pair["pair_id"], side)
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    }


class MappedTransport:
    """URL -> Response mapping; raises on any URL it does not declare."""

    def __init__(self, bodies: dict[str, bytes], *, headers=None, status=200):
        self.bodies = bodies
        self.headers = dict(headers or {})
        self.status = status
        self.calls: list[str] = []

    def __call__(self, url, *, headers, timeout):
        self.calls.append(url)
        if url not in self.bodies:
            raise AssertionError(f"transport asked for undeclared URL {url!r}")
        return rfa.Response(
            status=self.status, headers=dict(self.headers), body=self.bodies[url]
        )


class RefusingTransport:
    def __call__(self, url, *, headers, timeout):  # pragma: no cover - guard
        raise AssertionError("transport must not be consulted")


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make_fetcher(transport, **overrides):
    clock = _FakeClock()
    return rfa.Fetcher(
        user_agent=GOOD_AGENT,
        allow_network=True,
        transport=transport,
        min_interval_seconds=0.0,
        clock=clock,
        sleep=clock.sleep,
        **overrides,
    )


def run_full_acquisition(tmp_path, manifest=None):
    manifest = manifest or metadata_only_manifest()
    transport = MappedTransport(scripted_bodies(manifest))
    fetcher = make_fetcher(transport)
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    acquisition = rfha.acquire_holdout_manifest(
        manifest, fetcher=fetcher, layout=layout
    )
    return manifest, acquisition, transport, layout


# --- Frozen-identity gate (drift is refused before any request) ------------------


def test_committed_manifest_passes_the_identity_gate():
    rfha.verify_frozen_identity(COMMITTED_MANIFEST)


def test_parser_source_drift_is_rejected(tmp_path):
    fake_root = tmp_path / "repo"
    (fake_root / "loaders").mkdir(parents=True)
    (fake_root / "loaders" / "sec_headings.py").write_text(
        "PARSER_VERSION = 'sec_html_item_headings.v3'\n", encoding="utf-8"
    )
    with pytest.raises(rfha.HoldoutAcquisitionError) as excinfo:
        rfha.verify_frozen_identity(COMMITTED_MANIFEST, repo_root=fake_root)
    assert excinfo.value.code == rfha.FAILURE_PARSER_SOURCE_DRIFT


def test_selection_protocol_drift_is_rejected():
    document = copy.deepcopy(COMMITTED_MANIFEST)
    document["selection_protocol_hash"] = "b" * 64
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfha.verify_frozen_identity(document)
    assert excinfo.value.code == "holdout_manifest_protocol_hash_mismatch"


def test_development_exclusion_drift_is_rejected():
    development = rfb.load_manifest()
    shrunk = copy.deepcopy(development)
    shrunk["pairs"] = shrunk["pairs"][:1]
    with pytest.raises(rfha.HoldoutAcquisitionError) as excinfo:
        rfha.verify_frozen_identity(
            COMMITTED_MANIFEST, development_manifest=shrunk
        )
    assert excinfo.value.code == rfha.FAILURE_EXCLUSION_DRIFT


def test_manifest_beyond_source_verified_has_nothing_to_acquire():
    document = copy.deepcopy(COMMITTED_MANIFEST)
    document["status"] = rfb.STATUS_CORPUS_BUILT
    with pytest.raises(rfha.HoldoutAcquisitionError) as excinfo:
        rfha.verify_frozen_identity(document)
    assert excinfo.value.code == rfha.FAILURE_STATUS_NOT_ACQUIRABLE


def test_hand_edited_metadata_only_freeze_is_rejected_by_hash_chain(tmp_path):
    document = metadata_only_manifest()
    manifest_path = tmp_path / "manifest.json"
    rfb.write_json_atomic(manifest_path, document)
    rfb.write_json_atomic(
        tmp_path / "selection_report.json",
        {"holdout_manifest_sha256": rfb.sha256_file(manifest_path)},
    )
    rfha.verify_manifest_hash_chain(manifest_path)  # byte-identical: passes

    # Any edit — even one that stays schema-valid — breaks the chain.
    document["pairs"][0]["previous"]["accession_number"] = "0000009999-24-000001"
    rfb.write_json_atomic(manifest_path, document)
    with pytest.raises(rfha.HoldoutAcquisitionError) as excinfo:
        rfha.verify_manifest_hash_chain(manifest_path)
    assert excinfo.value.code == rfha.FAILURE_MANIFEST_HASH_DRIFT


def test_hash_chain_is_skipped_after_the_transition(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    rfb.write_json_atomic(manifest_path, COMMITTED_MANIFEST)
    rfb.write_json_atomic(
        tmp_path / "selection_report.json", {"holdout_manifest_sha256": "x"}
    )
    rfha.verify_manifest_hash_chain(manifest_path)  # source_verified: pinned elsewhere


# --- Official URL construction ---------------------------------------------------


def test_body_urls_are_canonical_official_and_exact():
    for pair in COMMITTED_MANIFEST["pairs"]:
        for side in ("previous", "current"):
            payload = pair[side]
            url = rfha.holdout_body_url(pair, payload)
            assert url == rfb.canonical_source_url(
                pair["cik"], payload["accession_number"], payload["primary_document"]
            )
            assert url.startswith("https://www.sec.gov/Archives/edgar/data/")
            assert payload["accession_number"].replace("-", "") in url
            assert url.endswith("/" + payload["primary_document"])


def test_acquisition_requests_exactly_the_twenty_frozen_urls(tmp_path):
    manifest, acquisition, transport, _layout = run_full_acquisition(tmp_path)
    expected = set(scripted_bodies(manifest))
    assert set(transport.calls) == expected
    assert len(transport.calls) == 20
    assert acquisition["request_count"] == 20


def test_non_official_host_is_rejected():
    with pytest.raises(rfa.NonOfficialSource):
        rfa.require_official_url(
            "https://www.sec.gov.example.test/Archives/edgar/data/1/x/y.htm"
        )
    fetcher = make_fetcher(RefusingTransport())
    with pytest.raises(rfa.NonOfficialSource):
        fetcher.get("https://sec-mirror.example.net/Archives/edgar/data/1/x/y.htm")


def test_redirect_to_non_official_host_is_rejected():
    handler = rfha._OfficialRedirectHandler()
    request = urllib.request.Request(
        "https://www.sec.gov/Archives/edgar/data/1/x/y.htm"
    )
    with pytest.raises(rfa.NonOfficialSource):
        handler.redirect_request(
            request, None, 301, "Moved", {}, "https://mirror.example.net/y.htm"
        )
    # An official-host redirect target is allowed through.
    allowed = handler.redirect_request(
        request, None, 301, "Moved", {},
        "https://www.sec.gov/Archives/edgar/data/1/x/z.htm",
    )
    assert allowed is not None


# --- Content-Encoding and digests ------------------------------------------------


def gzip_transport(bodies: dict[str, bytes], *, mtime: float) -> MappedTransport:
    """Wire bytes are gzip-compressed (with a container timestamp); the
    transport decodes them exactly as the real one does."""
    wire = {
        url: rfa.decode_content_encoding(
            gzip.compress(body, mtime=int(mtime)), "gzip"
        )
        for url, body in bodies.items()
    }
    return MappedTransport(wire, headers={"Content-Encoding": "gzip"})


def test_digest_is_over_decoded_bytes_not_the_gzip_container(tmp_path):
    manifest = metadata_only_manifest()
    pair = manifest["pairs"][0]
    body = body_for(pair["pair_id"], "previous")
    url = rfha.holdout_body_url(pair, pair["previous"])
    transport = gzip_transport({url: body}, mtime=1111)
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    outcome = rfha.acquire_holdout_side(
        fetcher=make_fetcher(transport), layout=layout, pair=pair, side="previous"
    )
    assert outcome["verified"] is True
    assert outcome["observed_sha256"] == hashlib.sha256(body).hexdigest()
    assert outcome["observed_sha256"] != hashlib.sha256(
        gzip.compress(body, mtime=1111)
    ).hexdigest()


def test_gzip_container_timestamps_cannot_change_the_digest(tmp_path):
    """Two downloads whose wire bytes differ (gzip mtime) must freeze the
    same digest — the digest is of the filing, not the transfer."""
    manifest = metadata_only_manifest()
    pair = manifest["pairs"][0]
    body = body_for(pair["pair_id"], "current")
    url = rfha.holdout_body_url(pair, pair["current"])
    digests = []
    for mtime in (1111, 2222):
        layout = rfb.CorpusLayout(tmp_path / f"corpus-{mtime}")
        outcome = rfha.acquire_holdout_side(
            fetcher=make_fetcher(gzip_transport({url: body}, mtime=mtime)),
            layout=layout,
            pair=pair,
            side="current",
        )
        assert outcome["verified"] is True
        digests.append(outcome["observed_sha256"])
    assert digests[0] == digests[1]


def test_written_file_is_atomic_and_matches_the_digest(tmp_path):
    manifest, acquisition, _transport, layout = run_full_acquisition(tmp_path)
    assert acquisition["all_verified"] is True
    for item in acquisition["filings"]:
        target = layout.root / item["source_path"]
        assert target.is_file()
        assert rfb.sha256_file(target) == item["observed_sha256"]
        leftovers = [
            name
            for name in target.parent.iterdir()
            if name.name.endswith((".part", ".tmp"))
        ]
        assert leftovers == []


def test_partial_identity_response_is_not_written_or_verified(tmp_path):
    manifest = metadata_only_manifest()
    pair = manifest["pairs"][0]
    body = body_for(pair["pair_id"], "previous")
    url = rfha.holdout_body_url(pair, pair["previous"])
    transport = MappedTransport(
        {url: body[: len(body) // 2]},
        headers={"Content-Length": str(len(body))},
    )
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    outcome = rfha.acquire_holdout_side(
        fetcher=make_fetcher(transport), layout=layout, pair=pair, side="previous"
    )
    assert outcome["outcome"] == rfa.OUTCOME_FAILED
    assert outcome["failure_code"] == rfha.FAILURE_INCOMPLETE_RESPONSE
    assert not (layout.root / outcome["source_path"]).exists()


def test_empty_response_is_refused(tmp_path):
    manifest = metadata_only_manifest()
    pair = manifest["pairs"][0]
    url = rfha.holdout_body_url(pair, pair["previous"])
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    outcome = rfha.acquire_holdout_side(
        fetcher=make_fetcher(MappedTransport({url: b""})),
        layout=layout,
        pair=pair,
        side="previous",
    )
    assert outcome["failure_code"] == rfa.FAILURE_EMPTY_RESPONSE


# --- Cache behavior --------------------------------------------------------------


def test_verified_cache_is_reused_without_touching_the_network(tmp_path):
    manifest, first, _transport, layout = run_full_acquisition(tmp_path)
    advanced = rfha.advance_holdout_manifest(manifest, first)
    rerun = rfha.acquire_holdout_manifest(
        advanced, fetcher=make_fetcher(RefusingTransport()), layout=layout
    )
    assert rerun["all_verified"] is True
    assert rerun["reused_verified_cache"] == 20
    assert rerun["downloaded"] == 0
    assert rerun["request_count"] == 0


def test_disagreeing_cached_file_is_rejected_and_preserved(tmp_path):
    manifest, first, _transport, layout = run_full_acquisition(tmp_path)
    advanced = rfha.advance_holdout_manifest(manifest, first)
    pair = advanced["pairs"][0]
    target = layout.source_file(
        pair["pair_id"], "previous", pair["previous"]["primary_document"]
    )
    tampered = b"<synthetic-tampered-cache/>"
    target.write_bytes(tampered)
    outcome = rfha.acquire_holdout_side(
        fetcher=make_fetcher(RefusingTransport()),
        layout=layout,
        pair=pair,
        side="previous",
    )
    assert outcome["outcome"] == rfa.OUTCOME_FAILED
    assert outcome["failure_code"] == rfa.FAILURE_CACHED_CONTENT_MISMATCH
    # Preserved as evidence, never silently replaced.
    assert target.read_bytes() == tampered


def test_unanchorable_cached_file_is_refused_not_trusted(tmp_path):
    manifest = metadata_only_manifest()
    pair = manifest["pairs"][0]
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    target = layout.source_file(
        pair["pair_id"], "previous", pair["previous"]["primary_document"]
    )
    target.parent.mkdir(parents=True)
    target.write_bytes(b"<synthetic-unanchored-bytes/>")
    outcome = rfha.acquire_holdout_side(
        fetcher=make_fetcher(RefusingTransport()),
        layout=layout,
        pair=pair,
        side="previous",
    )
    assert outcome["outcome"] == rfa.OUTCOME_FAILED
    assert outcome["failure_code"] == rfha.FAILURE_CACHE_UNVERIFIABLE


def test_download_that_disagrees_with_a_frozen_digest_writes_nothing(tmp_path):
    manifest, first, _transport, _layout = run_full_acquisition(tmp_path)
    advanced = rfha.advance_holdout_manifest(manifest, first)
    pair = advanced["pairs"][0]
    url = rfha.holdout_body_url(pair, pair["previous"])
    layout = rfb.CorpusLayout(tmp_path / "fresh-corpus")
    outcome = rfha.acquire_holdout_side(
        fetcher=make_fetcher(MappedTransport({url: b"<synthetic-wrong-body/>"})),
        layout=layout,
        pair=pair,
        side="previous",
    )
    assert outcome["failure_code"] == rfa.FAILURE_CHECKSUM_MISMATCH
    assert not (layout.root / outcome["source_path"]).exists()


# --- User agent and network gating ----------------------------------------------


def test_missing_user_agent_is_rejected(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setattr("config.SEC_USER_AGENT", None, raising=False)
    with pytest.raises(rfa.UserAgentRejected):
        rfa.resolve_user_agent()


def test_placeholder_user_agent_is_rejected():
    with pytest.raises(rfa.UserAgentRejected):
        rfa.validate_user_agent("Your Name your-email@example.com")


def test_network_disabled_without_explicit_authorization(tmp_path):
    manifest = metadata_only_manifest()
    fetcher = rfa.Fetcher(
        user_agent=GOOD_AGENT,
        allow_network=False,
        transport=RefusingTransport(),
    )
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    outcome = rfha.acquire_holdout_side(
        fetcher=fetcher, layout=layout, pair=manifest["pairs"][0], side="previous"
    )
    assert outcome["outcome"] == rfa.OUTCOME_FAILED
    assert outcome["failure_code"] == rfa.FAILURE_NETWORK_DISABLED


def test_cli_refuses_without_the_network_flag(capsys):
    from scripts import acquire_real_filing_holdout as cli

    assert cli.main([]) == 2
    assert "Network access is disabled" in capsys.readouterr().err


def test_cli_rejects_a_placeholder_user_agent(capsys):
    from scripts import acquire_real_filing_holdout as cli

    code = cli.main(
        ["--allow-network", "--user-agent", "Your Name your-email@example.com"]
    )
    assert code == 2
    assert "Rejected SEC user agent" in capsys.readouterr().err


# --- Transition ------------------------------------------------------------------


def test_one_failed_side_blocks_the_manifest_transition(tmp_path):
    manifest = metadata_only_manifest()
    bodies = scripted_bodies(manifest)
    failing_url = rfha.holdout_body_url(
        manifest["pairs"][0], manifest["pairs"][0]["previous"]
    )
    bodies.pop(failing_url)
    transport = MappedTransport(bodies)
    transport.bodies[failing_url] = b""  # declared, but empty -> refused
    acquisition = rfha.acquire_holdout_manifest(
        manifest, fetcher=make_fetcher(transport), layout=rfb.CorpusLayout(tmp_path)
    )
    assert acquisition["verified_filings"] == 19
    assert acquisition["all_verified"] is False
    with pytest.raises(rfha.HoldoutAcquisitionError) as excinfo:
        rfha.advance_holdout_manifest(manifest, acquisition)
    assert excinfo.value.code == rfha.FAILURE_NOT_FULLY_VERIFIED


def test_acquisition_never_mutates_the_frozen_manifest(tmp_path):
    manifest = metadata_only_manifest()
    before = copy.deepcopy(manifest)
    rfha.acquire_holdout_manifest(
        manifest,
        fetcher=make_fetcher(MappedTransport(scripted_bodies(manifest))),
        layout=rfb.CorpusLayout(tmp_path),
    )
    # No pair replaced, reordered, or edited in place — ever.
    assert manifest == before


def test_all_twenty_verified_permits_exactly_one_forward_step(tmp_path):
    manifest, acquisition, _transport, _layout = run_full_acquisition(tmp_path)
    advanced = rfha.advance_holdout_manifest(manifest, acquisition)
    assert advanced["status"] == rfb.STATUS_SOURCE_VERIFIED
    assert advanced["status"] != rfb.STATUS_CORPUS_BUILT
    rfh.validate_holdout_manifest(advanced)
    digests = {
        (item["pair_id"], item["side"]): item["observed_sha256"]
        for item in acquisition["filings"]
    }
    for pair in advanced["pairs"]:
        for side in ("previous", "current"):
            assert pair[side]["source_verified"] is True
            assert pair[side]["expected_sha256"] == digests[(pair["pair_id"], side)]
    # Identity fields are byte-identical to the freeze.
    for frozen_pair, advanced_pair in zip(manifest["pairs"], advanced["pairs"]):
        for field in ("pair_id", "cik", "issuer_name", "sic", "stratum_id"):
            assert advanced_pair[field] == frozen_pair[field]
        for side in ("previous", "current"):
            for field in (
                "accession_number", "form", "filing_date",
                "reporting_period", "primary_document",
            ):
                assert advanced_pair[side][field] == frozen_pair[side][field]


def test_an_already_advanced_manifest_cannot_transition_again(tmp_path):
    manifest, acquisition, _transport, _layout = run_full_acquisition(tmp_path)
    advanced = rfha.advance_holdout_manifest(manifest, acquisition)
    with pytest.raises(rfb.StatusTransitionError):
        rfha.advance_holdout_manifest(advanced, acquisition)


def test_advanced_manifest_keeps_every_denial_false(tmp_path):
    manifest, acquisition, _transport, _layout = run_full_acquisition(tmp_path)
    advanced = rfha.advance_holdout_manifest(manifest, acquisition)
    assert advanced["corpus_role"] == rfb.CORPUS_ROLE_EXTRACTION_HOLDOUT
    assert advanced["extraction_parser_developed_using_this_corpus"] is False
    assert advanced["extraction_holdout_evaluation"] is False
    assert advanced["generalization_claim_supported"] is False
    assert advanced["frozen_extraction_parser_version"] == (
        manifest["frozen_extraction_parser_version"]
    )
    assert advanced["frozen_parser_source_sha256"] == (
        manifest["frozen_parser_source_sha256"]
    )
    assert advanced["selection_protocol_hash"] == (
        manifest["selection_protocol_hash"]
    )


def test_advanced_manifest_serializes_deterministically(tmp_path):
    manifest, acquisition, _transport, _layout = run_full_acquisition(tmp_path)
    advanced = rfha.advance_holdout_manifest(manifest, acquisition)
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    rfb.write_json_atomic(first, advanced)
    rfb.write_json_atomic(second, advanced)
    assert rfb.sha256_file(first) == rfb.sha256_file(second)


# --- Report ---------------------------------------------------------------------


def full_report(tmp_path):
    manifest, acquisition, _transport, _layout = run_full_acquisition(tmp_path)
    return rfha.build_source_verification_report(
        manifest=manifest,
        acquisition=acquisition,
        prior_manifest_sha256="a" * 64,
        new_manifest_sha256="b" * 64,
        generated_at=rfb.utc_now_iso(),
    )


def test_report_counters_prove_zero_downstream_activity(tmp_path):
    report = full_report(tmp_path)
    assert report["verification_outcome"] == "source_verified"
    assert report["source_checksums_verified"] == 20
    assert report["source_documents_downloaded"] == 20
    assert report["filing_body_requests"] == 20
    assert report["extraction_runs"] == 0
    assert report["comparison_runs"] == 0
    assert report["annotation_packets"] == 0
    assert report["human_verified_labels"] == 0
    assert report["extraction_holdout_evaluation"] is False
    assert report["generalization_claim_supported"] is False
    assert report["official_hosts_contacted"] == ["www.sec.gov"]


def test_failed_run_reports_failure_and_advances_nothing(tmp_path):
    manifest = metadata_only_manifest()
    bodies = scripted_bodies(manifest)
    bodies[
        rfha.holdout_body_url(manifest["pairs"][0], manifest["pairs"][0]["previous"])
    ] = b""
    acquisition = rfha.acquire_holdout_manifest(
        manifest,
        fetcher=make_fetcher(MappedTransport(bodies)),
        layout=rfb.CorpusLayout(tmp_path),
    )
    report = rfha.build_source_verification_report(
        manifest=manifest,
        acquisition=acquisition,
        prior_manifest_sha256="a" * 64,
        new_manifest_sha256=None,
        generated_at=rfb.utc_now_iso(),
    )
    assert report["verification_outcome"] == "failed"
    assert report["new_manifest_status"] == rfh.STATUS_HOLDOUT_FROZEN_METADATA_ONLY
    assert report["new_manifest_sha256"] is None
    assert report["source_checksums_verified"] == 19
    failed = [item for item in report["filings"] if not item["verified"]]
    assert len(failed) == 1
    assert failed[0]["failure_code"] == rfa.FAILURE_EMPTY_RESPONSE


def test_report_and_outcomes_carry_no_content_paths_or_credentials(tmp_path):
    report = full_report(tmp_path)
    raw = json.dumps(report).lower()
    assert "<synthetic" not in raw  # no body bytes, even fixture ones
    assert "/users/" not in raw
    assert "/home/" not in raw
    assert "c:\\" not in raw
    assert str(tmp_path).lower() not in raw
    # The env var NAME may appear in regenerated_by; the VALUE never does.
    assert "jane.doe" not in raw
    assert "@university" not in raw
    for item in report["filings"]:
        assert item["official_source_url"].startswith("https://www.sec.gov/")


def test_no_packet_annotation_or_workspace_artifacts_are_created(tmp_path):
    _manifest, acquisition, _transport, layout = run_full_acquisition(tmp_path)
    assert acquisition["all_verified"] is True
    assert not (layout.root / "packets").exists()
    assert not (layout.root / "annotations").exists()
    assert not (layout.root / "build").exists()
    assert not (layout.root / "results").exists()
    assert {path.name for path in layout.root.iterdir()} == {"sources"}


# --- Import graph: this commit cannot extract, ingest, or compare ----------------


def _imports_of(path: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


@pytest.mark.parametrize(
    "path",
    (
        REPO_ROOT / "real_filing_holdout_acquisition.py",
        REPO_ROOT / "scripts" / "acquire_real_filing_holdout.py",
    ),
    ids=("module", "cli"),
)
def test_acquisition_imports_no_extraction_ingestion_or_storage_stack(path):
    imported = _imports_of(path)
    for forbidden in (
        "loaders",
        "ingest",
        "chromadb",
        "bs4",
        "lxml",
        "langchain",
        "langchain_aws",
        "langchain_chroma",
        "boto3",
        "tools",
        "agent",
        "comparison_detector",
        "comparison_store",
        "comparison_governance",
        "comparison_detection_worker",
    ):
        assert forbidden not in imported, forbidden


def test_parser_source_is_read_as_bytes_never_imported():
    source = (REPO_ROOT / "real_filing_holdout_acquisition.py").read_text(
        encoding="utf-8"
    )
    assert "import loaders" not in source
    assert "from loaders" not in source
    # The hash gate goes through the file-reading helper, not an import.
    assert "frozen_parser_source_sha256" in source


# --- The wider system is untouched ----------------------------------------------


def test_development_corpus_remains_unchanged():
    development = rfb.load_manifest()
    assert development["benchmark_id"] == "real_filing_v1"
    assert development["status"] == rfb.STATUS_SOURCE_VERIFIED
    assert len(development["pairs"]) == 10


def test_synthetic_regression_gates_remain_untouched():
    from scripts import eval_comparison_regression as ecr

    assert "holdout" not in json.dumps(sorted(ecr.GATES))
    assert len(ecr.GATES) == 10
