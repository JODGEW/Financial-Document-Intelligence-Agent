"""Deterministic metadata-only holdout selection tests.

Entirely offline and fixture-driven: the official metadata surface is an
in-memory URL->payload mapping (``tests/helpers/holdout_fixtures.py``) whose
issuers are synthetic sentinels. No network, no AWS, no real registrant, no
filing content.

The suite's centre of gravity is the body-access prohibition and the
predeclared-protocol discipline: the selection may never contact a filing
body, never shrink silently, and never change its rules after seeing partial
results. Those are the properties that make the frozen holdout a holdout.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

import real_filing_benchmark as rfb
import real_filing_holdout as rfh
from tests.helpers import holdout_fixtures as hfx

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_selection(source, dev=None):
    return rfh.select_holdout(
        fetch_json=source,
        development_manifest=dev or hfx.synthetic_development_manifest(),
    )


# --- Development-corpus exclusion ----------------------------------------------


def test_development_cik_is_never_probed_or_selected():
    source = hfx.metadata_source()
    result = run_selection(source)
    assert result["selected"] is True
    selected_ciks = {pair["cik"] for pair in result["manifest"]["pairs"]}
    assert hfx.cik_str(101) not in selected_ciks
    # Not even a metadata probe is spent on a development issuer.
    assert rfh.submissions_url(hfx.cik_str(101)) not in source.requested
    assert result["report"]["exclusion_counts"][
        rfh.REASON_DEVELOPMENT_CORPUS_CIK
    ] >= 1


def test_development_accession_excludes_an_otherwise_eligible_candidate():
    # Give a NON-development CIK a filing whose accession collides with a
    # development-corpus accession: the issuer must be excluded even though
    # its own CIK is clean.
    dev = hfx.synthetic_development_manifest()
    dev["pairs"] = [
        {
            "cik": hfx.cik_str(112),
            "previous": {
                "accession_number": hfx.accession(106, hfx.PREVIOUS_FY + 1)
            },
            "current": {
                "accession_number": hfx.accession(106, hfx.CURRENT_FY + 1)
            },
        }
    ]
    dev["proposed_issuers"] = [
        {"cik": hfx.cik_str(112), "resolution_status": "resolved_from_official_source"}
    ]
    source = hfx.metadata_source()
    result = run_selection(source, dev=dev)
    selected_ciks = {pair["cik"] for pair in result["manifest"]["pairs"]}
    assert hfx.cik_str(106) not in selected_ciks
    assert result["report"]["exclusion_counts"][
        rfh.REASON_DEVELOPMENT_CORPUS_ACCESSION
    ] == 1


def test_exclusions_are_derived_from_the_committed_development_manifest():
    exclusions = rfh.development_exclusions()
    committed = rfb.load_manifest()
    assert exclusions["excluded_ciks"] == sorted(
        entry["cik"] for entry in committed["proposed_issuers"]
    )
    expected_accessions = {
        payload["accession_number"]
        for pair in rfb.manifest_pairs(committed)
        for _side, payload in rfb.pair_sides(pair)
    }
    assert set(exclusions["excluded_accessions"]) == expected_accessions
    assert len(exclusions["excluded_accessions"]) == 20


# --- Determinism and ordering ---------------------------------------------------


def test_selection_is_deterministic_over_identical_metadata():
    first = run_selection(hfx.metadata_source())
    second = run_selection(hfx.metadata_source())
    strip = lambda manifest: {  # noqa: E731 - timestamps legitimately differ
        key: value
        for key, value in manifest.items()
        if key not in ("metadata_snapshot", "selected_at")
    }
    assert strip(first["manifest"]) == strip(second["manifest"])
    assert (
        first["report"]["selected_pairs"] == second["report"]["selected_pairs"]
    )
    assert (
        first["report"]["exclusion_counts"]
        == second["report"]["exclusion_counts"]
    )


def test_candidate_universe_orders_by_cik_with_stable_dedup():
    payload = {
        "0": {"cik_str": 300, "ticker": "CCC", "title": "Gamma Corp"},
        "1": {"cik_str": 100, "ticker": "AAA", "title": "Alpha Corp"},
        "2": {"cik_str": 200, "ticker": "BBB", "title": "Beta Corp"},
        # Duplicate CIK under a second ticker: one candidate, smallest title.
        "3": {"cik_str": 100, "ticker": "AAB", "title": "Alpha Class B"},
        # Junk rows are ignored rather than crashing the run.
        "4": {"cik_str": True, "ticker": "BAD", "title": "Bool"},
        "5": "not-a-mapping",
    }
    universe = rfh.candidate_universe(payload)
    assert [entry["cik"] for entry in universe] == [
        "0000000100",
        "0000000200",
        "0000000300",
    ]
    assert universe[0]["title"] == "Alpha Class B"


def test_within_stratum_pair_numbering_follows_universe_order():
    result = run_selection(hfx.metadata_source())
    pairs = result["manifest"]["pairs"]
    by_stratum: dict[str, list[str]] = {}
    for pair in pairs:
        by_stratum.setdefault(pair["stratum_id"], []).append(pair["cik"])
    # 106 (2086) precedes 111 (2890) in ascending-CIK universe order because
    # 101 was excluded as a development CIK.
    assert by_stratum["sic-2000s"] == [hfx.cik_str(106), hfx.cik_str(111)]
    assert [pair["pair_id"] for pair in pairs] == sorted(
        pair["pair_id"] for pair in pairs
    )


# --- Strata ---------------------------------------------------------------------


def test_declared_strata_are_closed_ranges_covering_the_quota():
    assert len(rfh.SIC_STRATA) == 5
    assert sum(stratum["quota"] for stratum in rfh.SIC_STRATA) == 10
    ranges = [tuple(stratum["sic_range"]) for stratum in rfh.SIC_STRATA]
    for low, high in ranges:
        assert low < high
    # Non-overlapping, declared in ascending order.
    for (low_a, high_a), (low_b, _high_b) in zip(ranges, ranges[1:]):
        assert high_a < low_b
    assert rfh.stratum_for_sic(1999) is None
    assert rfh.stratum_for_sic(2000)["stratum_id"] == "sic-2000s"
    assert rfh.stratum_for_sic(6999)["stratum_id"] == "sic-6000s"
    assert rfh.stratum_for_sic(7000) is None


def test_each_stratum_receives_exactly_its_quota():
    result = run_selection(hfx.metadata_source())
    assert result["report"]["stratum_distribution"] == {
        "sic-2000s": 2,
        "sic-3000s": 2,
        "sic-4000s": 2,
        "sic-5000s": 2,
        "sic-6000s": 2,
    }


def test_out_of_strata_sic_is_excluded_with_a_stable_reason():
    spec = list(hfx.DEFAULT_SPEC) + [(113, "Fictional Services Co.", 7372, True)]
    result = run_selection(hfx.metadata_source(spec))
    counts = result["report"]["exclusion_counts"]
    # 113 sits outside every declared range; it may only be counted if the
    # scan reached it before all quotas filled — force that by removing an
    # in-range candidate.
    starved = [entry for entry in spec if entry[0] != 110]
    result = run_selection(hfx.metadata_source(starved))
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfh.REASON_SIC_OUTSIDE_STRATA, 0) >= 1


# --- Fallback and failure -------------------------------------------------------


def test_deferred_fallback_absorbs_an_unfillable_stratum_slot():
    # Remove the second finance issuer (110): sic-6000s can only fill 1 of 2.
    # The predeclared fallback consumes deferred candidates in universe order
    # — 113 (sic-3000s) was deferred when its stratum filled with 102 and 107
    # (101 and 112 are development-excluded), so it absorbs the slot and the
    # corpus still reaches ten pairs over five distinct strata.
    spec = [entry for entry in hfx.DEFAULT_SPEC if entry[0] != 110] + [
        (113, "Fictional Industrial Pumps Corp.", 3560, True)
    ]
    result = run_selection(hfx.metadata_source(spec))
    assert result["selected"] is True
    report = result["report"]
    assert report["stratum_distribution"]["sic-6000s"] == 1
    assert report["stratum_distribution"]["sic-3000s"] == 3
    absorbed = [
        pair for pair in report["selected_pairs"] if pair["fallback_absorbed"]
    ]
    assert [pair["cik"] for pair in absorbed] == [hfx.cik_str(113)]
    rfh.validate_holdout_manifest(result["manifest"])


def test_insufficient_universe_fails_without_shrinking_or_mutating():
    # Remove every finance candidate: 9 pairs maximum. The selection must
    # FAIL loudly — never freeze a smaller corpus — and the protocol object
    # must be byte-identical before and after.
    protocol_before = rfh.selection_protocol_hash()
    spec = [
        entry for entry in hfx.DEFAULT_SPEC if not (6000 <= entry[2] <= 6999)
    ]
    result = run_selection(hfx.metadata_source(spec))
    assert result["selected"] is False
    assert result["manifest"] is None
    report = result["report"]
    assert report["selection_succeeded"] is False
    codes = {failure["code"] for failure in report["failures"]}
    assert rfh.SELECTION_FAILURE_STRATUM_UNFILLED in codes
    unfilled = [
        failure["unfilled_strata"]
        for failure in report["failures"]
        if failure["code"] == rfh.SELECTION_FAILURE_STRATUM_UNFILLED
    ]
    assert unfilled == [["sic-6000s"]]
    assert rfh.selection_protocol_hash() == protocol_before
    assert report["selection_protocol_hash"] == protocol_before


def test_probe_budget_exhaustion_is_a_failure_not_a_smaller_corpus(monkeypatch):
    monkeypatch.setattr(rfh, "MAX_SUBMISSIONS_PROBES", 3)
    result = run_selection(hfx.metadata_source())
    assert result["selected"] is False
    assert result["manifest"] is None
    codes = {failure["code"] for failure in result["report"]["failures"]}
    assert rfh.SELECTION_FAILURE_PROBE_BUDGET in codes


# --- Eligibility ----------------------------------------------------------------


def test_issuer_without_both_consecutive_annual_10ks_is_excluded():
    spec = [
        (105, "Fictional Savings Financial Corp.", 6022, False),  # ineligible
        *[entry for entry in hfx.DEFAULT_SPEC if entry[0] != 105],
    ]
    result = run_selection(hfx.metadata_source(spec))
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfh.REASON_TARGET_FISCAL_YEAR_MISSING, 0) >= 1
    selected_ciks = {
        pair["cik"] for pair in result["report"]["selected_pairs"]
    }
    assert hfx.cik_str(105) not in selected_ciks


def test_amendment_designated_target_row_is_rejected():
    # The companyfacts designation points at an accession whose submissions
    # row is a 10-K/A: the equality gate on form '10-K' must refuse it.
    source = hfx.metadata_source()
    url = rfh.submissions_url(hfx.cik_str(104))
    payload = source.payloads[url]
    recent = payload["filings"]["recent"]
    index = recent["accessionNumber"].index(
        hfx.accession(104, hfx.CURRENT_FY + 1)
    )
    recent["form"][index] = "10-K/A"
    result = run_selection(source)
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfh.REASON_TARGET_ROW_NOT_FOUND, 0) >= 1
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert hfx.cik_str(104) not in selected_ciks


def test_non_10k_forms_never_enter_the_row_scan():
    rows = rfh._ten_k_rows(
        {
            "form": ["10-K", "10-K/A", "10-Q", "8-K", "20-F", "40-F"],
            "accessionNumber": ["a", "b", "c", "d", "e", "f"],
            "filingDate": ["2025-01-01"] * 6,
            "reportDate": ["2024-12-31"] * 6,
            "primaryDocument": ["x.htm"] * 6,
        }
    )
    assert [row["accession_number"] for row in rows] == ["a"]


def test_paged_submissions_history_is_read_when_recent_is_insufficient():
    source = hfx.metadata_source(paged_ciks=frozenset({103}))
    result = run_selection(source)
    assert result["selected"] is True
    page_url = (
        f"https://data.sec.gov/submissions/CIK{hfx.cik_str(103)}"
        "-submissions-001.json"
    )
    assert page_url in source.requested
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert hfx.cik_str(103) in selected_ciks


def test_unreadable_paged_history_excludes_rather_than_truncates():
    source = hfx.metadata_source(paged_ciks=frozenset({103}))
    page_url = (
        f"https://data.sec.gov/submissions/CIK{hfx.cik_str(103)}"
        "-submissions-001.json"
    )
    del source.payloads[page_url]
    result = run_selection(source)
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfh.REASON_PAGED_HISTORY_UNREADABLE, 0) == 1
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert hfx.cik_str(103) not in selected_ciks


def test_fiscal_year_comes_from_filer_designation_not_period_end_year():
    # A June-fiscal-year filer: the FY2023 10-K's period ends 2024-06-30 and
    # the FY2024 10-K's period ends 2025-06-30. A period-end-year heuristic
    # would misassign both rows; the filer's own fy designation must win.
    source = hfx.metadata_source()
    cik = hfx.cik_str(105)
    sub_url = rfh.submissions_url(cik)
    facts_url = rfh.companyfacts_url(cik)
    previous_accn = hfx.accession(105, hfx.PREVIOUS_FY + 1)
    current_accn = hfx.accession(105, hfx.CURRENT_FY + 1)
    source.payloads[sub_url]["filings"]["recent"] = hfx._block(
        [
            {
                "form": "10-K",
                "accessionNumber": previous_accn,
                "filingDate": f"{hfx.PREVIOUS_FY + 1}-09-15",
                "reportDate": f"{hfx.PREVIOUS_FY + 1}-06-30",
                "primaryDocument": "fict-0105-junefy1.htm",
            },
            {
                "form": "10-K",
                "accessionNumber": current_accn,
                "filingDate": f"{hfx.CURRENT_FY + 1}-09-15",
                "reportDate": f"{hfx.CURRENT_FY + 1}-06-30",
                "primaryDocument": "fict-0105-junefy2.htm",
            },
        ]
    )
    result = run_selection(source)
    assert result["selected"] is True
    pair = next(
        pair
        for pair in result["manifest"]["pairs"]
        if pair["cik"] == cik
    )
    # Selected BY designation: reporting periods end in later calendar years
    # than the designated fiscal years.
    assert pair["previous"]["accession_number"] == previous_accn
    assert pair["current"]["accession_number"] == current_accn
    assert pair["previous"]["reporting_period"].startswith(
        str(hfx.PREVIOUS_FY + 1)
    )
    assert pair["target_previous_fiscal_year"] == hfx.PREVIOUS_FY


def test_ambiguous_fiscal_year_designation_is_excluded():
    source = hfx.metadata_source(ambiguous_ciks=frozenset({105}))
    # The second same-fy accession must also exist as a submissions row for
    # ambiguity (rather than row absence) to be the failing check.
    result = run_selection(source)
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfh.REASON_FISCAL_METADATA_AMBIGUOUS, 0) == 1
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert hfx.cik_str(105) not in selected_ciks


def test_duplicate_accession_across_issuers_is_rejected():
    # Fictional metadata anomaly: a later candidate claims the accession an
    # earlier selection already froze.
    source = hfx.metadata_source()
    cik = hfx.cik_str(110)
    stolen = hfx.accession(105, hfx.CURRENT_FY + 1)
    facts = source.payloads[rfh.companyfacts_url(cik)]
    entries = facts["facts"]["us-gaap"]["Assets"]["units"]["USD"]
    for entry in entries:
        if entry["fy"] == hfx.CURRENT_FY:
            entry["accn"] = stolen
    recent = source.payloads[rfh.submissions_url(cik)]["filings"]["recent"]
    index = recent["accessionNumber"].index(
        hfx.accession(110, hfx.CURRENT_FY + 1)
    )
    recent["accessionNumber"][index] = stolen
    result = run_selection(source)
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfh.REASON_DUPLICATE_SELECTION, 0) == 1
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert cik not in selected_ciks


# --- Body-access prohibition ----------------------------------------------------

BODY_URLS = (
    "https://www.sec.gov/Archives/edgar/data/1/000000000125000001/fict.htm",
    "https://www.sec.gov/Archives/edgar/data/1/000000000125000001/fict-ex99.htm",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany",
    "https://data.sec.gov/api/xbrl/frames/us-gaap/Assets/USD/CY2024Q4I.json",
    "https://efts.sec.gov/LATEST/search-index?q=risk",
    "https://example.com/mirror/fict.htm",
    "http://data.sec.gov/submissions/CIK0000000001.json",
    "https://data.sec.gov/submissions/../Archives/fict.htm",
)


@pytest.mark.parametrize("url", BODY_URLS)
def test_metadata_allowlist_rejects_every_body_and_lookalike_url(url):
    with pytest.raises(rfh.NonMetadataEndpoint) as excinfo:
        rfh.require_metadata_url(url)
    assert excinfo.value.code == rfh.FAILURE_NON_METADATA_ENDPOINT


def test_selection_contacts_only_declared_metadata_endpoints():
    source = hfx.metadata_source(paged_ciks=frozenset({103}))
    run_selection(source)
    assert source.requested, "the selection must have fetched metadata"
    for url in source.requested:
        # Re-validating every requested URL proves no request bypassed the
        # allowlist — a filing-body URL would raise here.
        assert rfh.require_metadata_url(url) == url
    classes = {rfh.endpoint_class(url) for url in source.requested}
    assert classes <= {"company_tickers", "submissions", "companyfacts"}


def test_a_body_fetching_transport_cannot_be_reached():
    """Even a transport that would happily serve a filing body is never asked
    for one: the wrapper validates the URL before the transport sees it."""

    def treacherous(url: str):
        if "/Archives/" in url:
            raise AssertionError("a filing body URL reached the transport")
        return hfx.metadata_source()(url)

    result = rfh.select_holdout(
        fetch_json=treacherous,
        development_manifest=hfx.synthetic_development_manifest(),
    )
    assert result["selected"] is True
    with pytest.raises(rfh.NonMetadataEndpoint):
        rfh.MetadataOnlyFetcher(treacherous).get(BODY_URLS[0])


def test_selection_writes_no_files_and_creates_no_corpus_directory(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    result = run_selection(hfx.metadata_source())
    assert result["selected"] is True
    assert list(tmp_path.iterdir()) == []
    assert not (tmp_path / "benchmark_data").exists()


def test_holdout_module_imports_no_network_extraction_or_storage_stack():
    """Checked at the import graph, like real_filing_benchmark's own test: the
    selection cannot open a socket, load a filing, run extraction, embed, or
    touch Chroma even by accident."""
    source = (REPO_ROOT / "real_filing_holdout.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in (
        "urllib",
        "http",
        "socket",
        "requests",
        "httpx",
        "boto3",
        "langchain",
        "langchain_aws",
        "chromadb",
        "real_filing_acquisition",
        "ingest",
        "loaders",
        "tools",
        "agent",
        "comparison_detector",
        "comparison_store",
        "bs4",
    ):
        assert forbidden not in imported, forbidden


# --- Audit report ---------------------------------------------------------------


def test_selection_report_counters_prove_no_body_or_evaluation_activity():
    report = run_selection(hfx.metadata_source())["report"]
    assert report["filing_body_requests"] == 0
    assert report["source_documents_downloaded"] == 0
    assert report["source_checksums_verified"] == 0
    assert report["extraction_runs"] == 0
    assert report["comparison_runs"] == 0
    assert report["annotation_packets"] == 0
    assert report["human_verified_labels"] == 0
    assert report["generalization_claim_supported"] is False
    assert report["corpus_role"] == rfh.rfb.CORPUS_ROLE_EXTRACTION_HOLDOUT
    assert report["extraction_holdout_evaluation"] is False


def test_selection_report_records_protocol_and_exclusion_provenance():
    report = run_selection(hfx.metadata_source())["report"]
    assert report["selection_protocol_hash"] == rfh.selection_protocol_hash()
    assert (
        report["selection_protocol"]["protocol_version"]
        == rfh.HOLDOUT_SELECTION_PROTOCOL_VERSION
    )
    applied = report["development_exclusions_applied"]
    assert applied["excluded_cik_count"] == len(applied["excluded_ciks"])
    assert applied["excluded_accession_count"] == len(
        applied["excluded_accessions"]
    )
    endpoints = report["metadata_endpoints_contacted"]
    assert set(endpoints) == {"company_tickers", "submissions", "companyfacts"}
    assert endpoints["company_tickers"] == 1


def test_failed_selection_still_reports_and_freezes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = [
        entry for entry in hfx.DEFAULT_SPEC if not (6000 <= entry[2] <= 6999)
    ]
    result = run_selection(hfx.metadata_source(spec))
    assert result["selected"] is False
    assert result["manifest"] is None
    assert result["report"]["selection_succeeded"] is False
    assert result["report"]["filing_body_requests"] == 0
    assert list(tmp_path.iterdir()) == []


# --- Protocol content -----------------------------------------------------------


def test_protocol_is_predeclared_and_hash_stable():
    protocol = rfh.selection_protocol()
    assert protocol["target_pair_count"] == 10
    assert protocol["target_filing_count"] == 20
    assert protocol["target_previous_fiscal_year"] + 1 == (
        protocol["target_current_fiscal_year"]
    )
    assert len(protocol["sic_strata"]) == 5
    assert protocol["minimum_distinct_strata"] == 5
    assert rfh.selection_protocol_hash() == rfb.payload_hash(protocol)
    # The declared check order is closed and matches the reason vocabulary.
    order = protocol["eligibility_check_order"]
    assert len(order) == len(set(order))
    assert rfh.REASON_DEVELOPMENT_CORPUS_CIK == order[0]


def test_synthetic_regression_gates_remain_untouched():
    from scripts import eval_comparison_regression as ecr

    assert "holdout" not in json.dumps(sorted(ecr.GATES))
    assert len(ecr.GATES) == 10
