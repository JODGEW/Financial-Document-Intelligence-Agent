"""Deterministic metadata-only v3-holdout selection tests.

Entirely offline and fixture-driven: the official metadata surface is an
in-memory URL->payload mapping (``tests/helpers/v3_holdout_fixtures.py``)
whose issuers are synthetic sentinels. No network, no AWS, no real
registrant, no filing content.

The suite's centre of gravity is unchanged from the first holdout — the
body-access prohibition and the predeclared-protocol discipline — plus what
is new in the v3 protocol: hash-ranked candidate order under a fixed seed,
exclusion of BOTH prior corpora by CIK and accession with recorded source
hashes, the fixed FY2024 -> FY2025 target, and the frozen v3/v2 contract
identity block.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

import real_filing_benchmark as rfb
import real_filing_holdout as rfh
import real_filing_v3_holdout as rfv3
from tests.helpers import v3_holdout_fixtures as vfx

REPO_ROOT = Path(__file__).resolve().parent.parent

run_selection = vfx.run_selection


# --- Selection identity: hash-ranked order, fixed seed --------------------------


def test_benchmark_id_is_distinct_from_both_prior_corpora():
    assert rfv3.V3_HOLDOUT_BENCHMARK_ID == "real_filing_v3_holdout_v1"
    assert rfv3.V3_HOLDOUT_BENCHMARK_ID != rfb.BENCHMARK_ID
    assert rfv3.V3_HOLDOUT_BENCHMARK_ID != rfh.HOLDOUT_BENCHMARK_ID
    assert (
        rfv3.V3_HOLDOUT_MANIFEST_SCHEMA_VERSION
        != rfh.HOLDOUT_MANIFEST_SCHEMA_VERSION
    )
    assert (
        rfv3.V3_HOLDOUT_SELECTION_PROTOCOL_VERSION
        != rfh.HOLDOUT_SELECTION_PROTOCOL_VERSION
    )


def test_rank_key_is_sha256_over_the_fixed_seed_and_cik():
    import hashlib

    cik = "0000000102"
    expected = hashlib.sha256(
        f"{rfv3.V3_HOLDOUT_BENCHMARK_ID}|{cik}".encode("utf-8")
    ).hexdigest()
    assert rfv3.rank_key(cik) == expected
    assert rfv3.SELECTION_SEED_IDENTIFIER == rfv3.V3_HOLDOUT_BENCHMARK_ID


def test_candidate_universe_is_hash_ranked_not_cik_ordered():
    payload = {
        str(index): {
            "cik_str": cik_int,
            "ticker": f"T{cik_int}",
            "title": f"Sentinel {cik_int}",
        }
        for index, cik_int in enumerate([101, 102, 103, 104, 105, 106])
    }
    universe = rfv3.candidate_universe(payload)
    ciks = [entry["cik"] for entry in universe]
    assert sorted(ciks) == [f"{n:010d}" for n in [101, 102, 103, 104, 105, 106]]
    assert ciks == sorted(ciks, key=rfv3.rank_key)
    # The hash ranking genuinely permutes: ascending-CIK order and rank order
    # disagree for these sentinels (a fixed property of the fixed seed).
    assert ciks != sorted(ciks)


def test_metadata_payload_ordering_does_not_change_the_universe():
    entries = [
        {"cik_str": cik_int, "ticker": f"T{cik_int}", "title": f"Sentinel {cik_int}"}
        for cik_int in [104, 101, 106, 102, 105, 103]
    ]
    forward = {str(i): entry for i, entry in enumerate(entries)}
    backward = {str(i): entry for i, entry in enumerate(reversed(entries))}
    assert rfv3.candidate_universe(forward) == rfv3.candidate_universe(backward)


def test_duplicate_cik_dedups_with_stable_smallest_title():
    payload = {
        "0": {"cik_str": 100, "ticker": "AAA", "title": "Alpha Corp"},
        "1": {"cik_str": 100, "ticker": "AAB", "title": "Alpha Class B"},
        "2": {"cik_str": True, "ticker": "BAD", "title": "Bool"},
        "3": "not-a-mapping",
    }
    universe = rfv3.candidate_universe(payload)
    assert [entry["cik"] for entry in universe] == ["0000000100"]
    assert universe[0]["title"] == "Alpha Class B"


def test_selection_is_deterministic_over_identical_metadata():
    first = run_selection(vfx.metadata_source())
    second = run_selection(vfx.metadata_source())
    strip = lambda manifest: {  # noqa: E731 - timestamps legitimately differ
        key: value
        for key, value in manifest.items()
        if key not in ("metadata_snapshot", "selected_at")
    }
    assert strip(first["manifest"]) == strip(second["manifest"])
    assert first["report"]["selected_pairs"] == second["report"]["selected_pairs"]
    assert (
        first["report"]["exclusion_counts"]
        == second["report"]["exclusion_counts"]
    )
    assert rfv3.reproducible_manifest_hash(
        first["manifest"]
    ) == rfv3.reproducible_manifest_hash(second["manifest"])


def test_no_random_source_and_no_seed_parameter_exists():
    # The module never imports a randomness source ...
    source = (REPO_ROOT / "real_filing_v3_holdout.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "random" not in imported
    assert "secrets" not in imported
    # ... and no selection entry point accepts a caller-supplied seed.
    for func in (rfv3.select_v3_holdout, rfv3.candidate_universe, rfv3.rank_key):
        parameters = inspect.signature(func).parameters
        assert not any("seed" in name for name in parameters), func.__name__


def test_within_stratum_pair_numbering_follows_rank_order():
    result = run_selection(vfx.metadata_source())
    assert result["selected"] is True
    pairs = result["manifest"]["pairs"]
    order = vfx.rank_order()
    by_stratum: dict[str, list[str]] = {}
    for pair in pairs:
        by_stratum.setdefault(pair["stratum_id"], []).append(pair["cik"])
    for stratum_ciks in by_stratum.values():
        assert stratum_ciks == sorted(stratum_ciks, key=order.index)
    assert [pair["pair_id"] for pair in pairs] == sorted(
        pair["pair_id"] for pair in pairs
    )


# --- Prior-corpus exclusions ----------------------------------------------------


def test_development_cik_is_never_probed_or_selected():
    source = vfx.metadata_source()
    result = run_selection(source)
    assert result["selected"] is True
    selected_ciks = {pair["cik"] for pair in result["manifest"]["pairs"]}
    assert vfx.cik_str(101) not in selected_ciks
    assert vfx.cik_str(112) not in selected_ciks
    # Not even a metadata probe is spent on an excluded issuer.
    assert rfv3.submissions_url(vfx.cik_str(101)) not in source.requested
    assert rfv3.submissions_url(vfx.cik_str(112)) not in source.requested
    assert result["report"]["exclusion_counts"][
        rfv3.REASON_PRIOR_CORPUS_CIK
    ] >= 2


def test_first_holdout_cik_is_never_probed_or_selected():
    source = vfx.metadata_source()
    result = run_selection(source)
    assert result["selected"] is True
    selected_ciks = {pair["cik"] for pair in result["manifest"]["pairs"]}
    assert vfx.cik_str(114) not in selected_ciks
    assert rfv3.submissions_url(vfx.cik_str(114)) not in source.requested


def test_development_accession_excludes_an_otherwise_eligible_candidate():
    dev = vfx.synthetic_development_manifest()
    dev["pairs"] = [
        {
            "cik": vfx.cik_str(112),
            "previous": {
                "accession_number": vfx.accession(106, vfx.PREVIOUS_FY + 1)
            },
            "current": {
                "accession_number": vfx.accession(106, vfx.CURRENT_FY + 1)
            },
        }
    ]
    result = run_selection(vfx.metadata_source(), dev=dev)
    selected_ciks = {pair["cik"] for pair in result["manifest"]["pairs"]}
    assert vfx.cik_str(106) not in selected_ciks
    assert result["report"]["exclusion_counts"][
        rfv3.REASON_PRIOR_CORPUS_ACCESSION
    ] == 1


def test_first_holdout_accession_excludes_an_otherwise_eligible_candidate():
    holdout = vfx.synthetic_first_holdout_manifest()
    holdout["pairs"] = [
        {
            "cik": vfx.cik_str(114),
            "previous": {
                "accession_number": vfx.accession(109, vfx.PREVIOUS_FY + 1)
            },
            "current": {
                "accession_number": vfx.accession(109, vfx.CURRENT_FY + 1)
            },
        }
    ]
    result = run_selection(vfx.metadata_source(), holdout=holdout)
    selected_ciks = {pair["cik"] for pair in result["manifest"]["pairs"]}
    assert vfx.cik_str(109) not in selected_ciks
    assert result["report"]["exclusion_counts"][
        rfv3.REASON_PRIOR_CORPUS_ACCESSION
    ] == 1


def test_exclusions_are_derived_from_both_committed_manifests():
    exclusions = rfv3.prior_corpus_exclusions()
    dev_source, holdout_source = exclusions["sources"]

    committed_dev = rfb.load_manifest()
    assert dev_source["benchmark_id"] == committed_dev["benchmark_id"]
    assert dev_source["excluded_ciks"] == sorted(
        entry["cik"] for entry in committed_dev["proposed_issuers"]
    )
    assert len(dev_source["excluded_accessions"]) == 20
    assert dev_source["manifest_sha256"] == rfb.sha256_file(
        REPO_ROOT / rfv3.DEVELOPMENT_MANIFEST_PATH
    )

    committed_holdout = rfh.load_holdout_manifest()
    assert holdout_source["benchmark_id"] == committed_holdout["benchmark_id"]
    assert holdout_source["excluded_ciks"] == sorted(
        pair["cik"] for pair in committed_holdout["pairs"]
    )
    assert len(holdout_source["excluded_accessions"]) == 20
    assert holdout_source["manifest_sha256"] == rfb.sha256_file(
        REPO_ROOT / rfv3.FIRST_HOLDOUT_MANIFEST_PATH
    )

    # The merged sets cover both corpora and stay independently non-empty.
    assert len(rfv3.merged_excluded_ciks(exclusions)) == 20
    assert len(rfv3.merged_excluded_accessions(exclusions)) == 40


def test_exclusion_source_hashes_are_frozen_into_the_manifest():
    result = run_selection(vfx.metadata_source())
    frozen = result["manifest"]["prior_corpus_exclusions"]["sources"]
    assert [source["benchmark_id"] for source in frozen] == [
        "synthetic_dev_benchmark",
        "synthetic_first_holdout_benchmark",
    ]
    for source in frozen:
        assert rfb._SHA256_RE.match(source["manifest_sha256"])
        assert source["excluded_ciks"]
        assert source["excluded_accessions"]


def test_exclusion_manifest_hash_drift_is_rejected(tmp_path):
    result = run_selection(vfx.metadata_source())
    manifest = result["manifest"]
    # The provenance check recomputes from the COMMITTED manifests; a manifest
    # frozen against synthetic sources cannot match them.
    with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
        rfv3.verify_exclusion_provenance(manifest)
    assert excinfo.value.code in (
        "v3_holdout_exclusion_source_drift",
        "v3_holdout_exclusion_set_drift",
    )


def test_manifest_validation_rejects_prior_corpus_cik_and_accession():
    result = run_selection(vfx.metadata_source())
    manifest = result["manifest"]

    poisoned = json.loads(json.dumps(manifest))
    poisoned["prior_corpus_exclusions"]["sources"][0]["excluded_ciks"].append(
        poisoned["pairs"][0]["cik"]
    )
    with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
        rfv3.validate_v3_holdout_manifest(poisoned)
    assert excinfo.value.code == "v3_holdout_pair_prior_corpus_cik"

    poisoned = json.loads(json.dumps(manifest))
    poisoned["prior_corpus_exclusions"]["sources"][1][
        "excluded_accessions"
    ].append(poisoned["pairs"][0]["previous"]["accession_number"])
    with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
        rfv3.validate_v3_holdout_manifest(poisoned)
    assert excinfo.value.code == "v3_holdout_pair_prior_corpus_accession"


# --- Strata and eligibility -----------------------------------------------------


def test_declared_strata_are_closed_ranges_covering_the_quota():
    assert len(rfv3.SIC_STRATA) == 5
    assert sum(stratum["quota"] for stratum in rfv3.SIC_STRATA) == 10
    ranges = [tuple(stratum["sic_range"]) for stratum in rfv3.SIC_STRATA]
    for low, high in ranges:
        assert low < high
    for (low_a, high_a), (low_b, _high_b) in zip(ranges, ranges[1:]):
        assert high_a < low_b
    assert rfv3.stratum_for_sic(1999) is None
    assert rfv3.stratum_for_sic(2000)["stratum_id"] == "sic-2000s"
    assert rfv3.stratum_for_sic(6999)["stratum_id"] == "sic-6000s"
    assert rfv3.stratum_for_sic(7000) is None


def test_each_stratum_receives_exactly_its_quota():
    result = run_selection(vfx.metadata_source())
    assert result["report"]["stratum_distribution"] == {
        "sic-2000s": 2,
        "sic-3000s": 2,
        "sic-4000s": 2,
        "sic-5000s": 2,
        "sic-6000s": 2,
    }
    manifest = result["manifest"]
    assert len(manifest["pairs"]) == 10
    assert len({pair["cik"] for pair in manifest["pairs"]}) == 10
    assert len({pair["issuer_name"] for pair in manifest["pairs"]}) == 10


def test_target_fiscal_years_are_fixed_at_2024_to_2025():
    assert rfv3.TARGET_PREVIOUS_FISCAL_YEAR == 2024
    assert rfv3.TARGET_CURRENT_FISCAL_YEAR == 2025
    protocol = rfv3.selection_protocol()
    assert protocol["target_previous_fiscal_year"] == 2024
    assert protocol["target_current_fiscal_year"] == 2025
    result = run_selection(vfx.metadata_source())
    for pair in result["manifest"]["pairs"]:
        assert pair["target_previous_fiscal_year"] == 2024
        assert pair["target_current_fiscal_year"] == 2025


def test_non_10k_forms_never_enter_the_row_scan():
    rows = rfh._ten_k_rows(
        {
            "form": ["10-K", "10-K/A", "10-Q", "8-K", "20-F", "40-F"],
            "accessionNumber": ["a", "b", "c", "d", "e", "f"],
            "filingDate": ["2026-01-01"] * 6,
            "reportDate": ["2025-12-31"] * 6,
            "primaryDocument": ["x.htm"] * 6,
        }
    )
    assert [row["accession_number"] for row in rows] == ["a"]


def test_amendment_designated_target_row_is_rejected():
    source = vfx.metadata_source()
    url = rfv3.submissions_url(vfx.cik_str(104))
    payload = source.payloads[url]
    recent = payload["filings"]["recent"]
    index = recent["accessionNumber"].index(
        vfx.accession(104, vfx.CURRENT_FY + 1)
    )
    recent["form"][index] = "10-K/A"
    result = run_selection(source)
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfv3.REASON_TARGET_ROW_NOT_FOUND, 0) >= 1
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert vfx.cik_str(104) not in selected_ciks


def test_missing_target_fiscal_year_is_rejected():
    # 105 keeps only FY2024: FY2025 designation missing -> excluded.
    source = vfx.metadata_source()
    cik = vfx.cik_str(105)
    facts = source.payloads[rfv3.companyfacts_url(cik)]
    entries = facts["facts"]["us-gaap"]["Assets"]["units"]["USD"]
    facts["facts"]["us-gaap"]["Assets"]["units"]["USD"] = [
        entry for entry in entries if entry["fy"] != vfx.CURRENT_FY
    ]
    result = run_selection(source)
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfv3.REASON_TARGET_FISCAL_YEAR_MISSING, 0) >= 1
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert cik not in selected_ciks


def test_missing_previous_fiscal_year_is_rejected():
    source = vfx.metadata_source()
    cik = vfx.cik_str(110)
    facts = source.payloads[rfv3.companyfacts_url(cik)]
    entries = facts["facts"]["us-gaap"]["Assets"]["units"]["USD"]
    facts["facts"]["us-gaap"]["Assets"]["units"]["USD"] = [
        entry for entry in entries if entry["fy"] != vfx.PREVIOUS_FY
    ]
    result = run_selection(source)
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfv3.REASON_TARGET_FISCAL_YEAR_MISSING, 0) >= 1
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert cik not in selected_ciks


def test_fiscal_year_comes_from_filer_designation_not_period_end_year():
    # A June-fiscal-year filer: the FY2024 10-K's period ends 2025-06-30 and
    # the FY2025 10-K's period ends 2026-06-30. A period-end-year heuristic
    # would misassign both rows; the filer's own fy designation must win.
    source = vfx.metadata_source()
    cik = vfx.cik_str(105)
    sub_url = rfv3.submissions_url(cik)
    previous_accn = vfx.accession(105, vfx.PREVIOUS_FY + 1)
    current_accn = vfx.accession(105, vfx.CURRENT_FY + 1)
    source.payloads[sub_url]["filings"]["recent"] = vfx._block(
        [
            {
                "form": "10-K",
                "accessionNumber": previous_accn,
                "filingDate": f"{vfx.PREVIOUS_FY + 1}-09-15",
                "reportDate": f"{vfx.PREVIOUS_FY + 1}-06-30",
                "primaryDocument": "fict-0105-junefy1.htm",
            },
            {
                "form": "10-K",
                "accessionNumber": current_accn,
                "filingDate": f"{vfx.CURRENT_FY + 1}-09-15",
                "reportDate": f"{vfx.CURRENT_FY + 1}-06-30",
                "primaryDocument": "fict-0105-junefy2.htm",
            },
        ]
    )
    result = run_selection(source)
    assert result["selected"] is True
    pair = next(
        pair for pair in result["manifest"]["pairs"] if pair["cik"] == cik
    )
    assert pair["previous"]["accession_number"] == previous_accn
    assert pair["current"]["accession_number"] == current_accn
    assert pair["previous"]["reporting_period"].startswith(
        str(vfx.PREVIOUS_FY + 1)
    )
    assert pair["target_previous_fiscal_year"] == vfx.PREVIOUS_FY


def test_ambiguous_fiscal_year_designation_is_excluded():
    source = vfx.metadata_source(ambiguous_ciks=frozenset({105}))
    result = run_selection(source)
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfv3.REASON_FISCAL_METADATA_AMBIGUOUS, 0) == 1
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert vfx.cik_str(105) not in selected_ciks


def test_paged_submissions_history_is_read_when_recent_is_insufficient():
    source = vfx.metadata_source(paged_ciks=frozenset({103}))
    result = run_selection(source)
    assert result["selected"] is True
    page_url = (
        f"https://data.sec.gov/submissions/CIK{vfx.cik_str(103)}"
        "-submissions-001.json"
    )
    assert page_url in source.requested
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert vfx.cik_str(103) in selected_ciks


def test_unreadable_paged_history_excludes_rather_than_truncates():
    source = vfx.metadata_source(paged_ciks=frozenset({103}))
    page_url = (
        f"https://data.sec.gov/submissions/CIK{vfx.cik_str(103)}"
        "-submissions-001.json"
    )
    del source.payloads[page_url]
    result = run_selection(source)
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfv3.REASON_PAGED_HISTORY_UNREADABLE, 0) == 1
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert vfx.cik_str(103) not in selected_ciks


def test_duplicate_accession_across_issuers_is_rejected():
    source = vfx.metadata_source()
    # The universe is rank-ordered, so pick the LATER of the two finance
    # issuers to steal the earlier one's accession.
    order = vfx.rank_order()
    finance = sorted(
        [vfx.cik_str(105), vfx.cik_str(110)], key=order.index
    )
    victim_cik, thief_cik = finance
    victim_int = int(victim_cik)
    stolen = vfx.accession(victim_int, vfx.CURRENT_FY + 1)
    facts = source.payloads[rfv3.companyfacts_url(thief_cik)]
    entries = facts["facts"]["us-gaap"]["Assets"]["units"]["USD"]
    for entry in entries:
        if entry["fy"] == vfx.CURRENT_FY:
            entry["accn"] = stolen
    recent = source.payloads[rfv3.submissions_url(thief_cik)]["filings"]["recent"]
    index = recent["accessionNumber"].index(
        vfx.accession(int(thief_cik), vfx.CURRENT_FY + 1)
    )
    recent["accessionNumber"][index] = stolen
    result = run_selection(source)
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfv3.REASON_DUPLICATE_SELECTION, 0) == 1
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert thief_cik not in selected_ciks


def test_incomplete_row_metadata_is_rejected():
    source = vfx.metadata_source()
    cik = vfx.cik_str(108)
    recent = source.payloads[rfv3.submissions_url(cik)]["filings"]["recent"]
    index = recent["accessionNumber"].index(
        vfx.accession(108, vfx.CURRENT_FY + 1)
    )
    recent["primaryDocument"][index] = ""
    result = run_selection(source)
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfv3.REASON_ROW_METADATA_INCOMPLETE, 0) >= 1
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert cik not in selected_ciks


def test_inconsistent_chronology_is_rejected():
    source = vfx.metadata_source()
    cik = vfx.cik_str(102)
    recent = source.payloads[rfv3.submissions_url(cik)]["filings"]["recent"]
    index = recent["accessionNumber"].index(
        vfx.accession(102, vfx.CURRENT_FY + 1)
    )
    # The FY2025 filing pretends to precede the FY2024 one.
    recent["filingDate"][index] = f"{vfx.PREVIOUS_FY}-01-01"
    recent["reportDate"][index] = f"{vfx.PREVIOUS_FY - 1}-12-31"
    result = run_selection(source)
    counts = result["report"]["exclusion_counts"]
    assert counts.get(rfv3.REASON_CHRONOLOGY_INCONSISTENT, 0) >= 1
    selected_ciks = {pair["cik"] for pair in result["report"]["selected_pairs"]}
    assert cik not in selected_ciks


# --- Fallback and failure -------------------------------------------------------


def test_deferred_fallback_absorbs_an_unfillable_stratum_slot():
    # Remove one finance issuer: sic-6000s can only fill 1 of 2 (114 is a
    # first-holdout exclusion). A spare candidate deferred when its own
    # stratum filled absorbs the slot in rank order, and the corpus still
    # reaches ten pairs over five distinct strata.
    removed = 110
    spec = [entry for entry in vfx.DEFAULT_SPEC if entry[0] != removed]
    result = run_selection(vfx.metadata_source(spec))
    assert result["selected"] is True
    report = result["report"]
    assert report["stratum_distribution"]["sic-6000s"] == 1
    absorbed = [
        pair for pair in report["selected_pairs"] if pair["fallback_absorbed"]
    ]
    assert len(absorbed) == 1
    rfv3.validate_v3_holdout_manifest(result["manifest"])


def test_insufficient_universe_fails_without_shrinking_or_mutating():
    protocol_before = rfv3.selection_protocol_hash()
    spec = [
        entry for entry in vfx.DEFAULT_SPEC if not (6000 <= entry[2] <= 6999)
    ]
    result = run_selection(vfx.metadata_source(spec))
    assert result["selected"] is False
    assert result["manifest"] is None
    report = result["report"]
    assert report["selection_succeeded"] is False
    codes = {failure["code"] for failure in report["failures"]}
    assert rfv3.SELECTION_FAILURE_STRATUM_UNFILLED in codes
    unfilled = [
        failure["unfilled_strata"]
        for failure in report["failures"]
        if failure["code"] == rfv3.SELECTION_FAILURE_STRATUM_UNFILLED
    ]
    assert unfilled == [["sic-6000s"]]
    assert rfv3.selection_protocol_hash() == protocol_before
    assert report["selection_protocol_hash"] == protocol_before
    assert report["manifest_status"] is None
    assert report["reproducible_manifest_hash"] is None


def test_probe_budget_exhaustion_is_a_failure_not_a_smaller_corpus(monkeypatch):
    monkeypatch.setattr(rfv3, "MAX_SUBMISSIONS_PROBES", 3)
    result = run_selection(vfx.metadata_source())
    assert result["selected"] is False
    assert result["manifest"] is None
    codes = {failure["code"] for failure in result["report"]["failures"]}
    assert rfv3.SELECTION_FAILURE_PROBE_BUDGET in codes


def test_probe_budget_is_the_repository_bound():
    assert rfv3.MAX_SUBMISSIONS_PROBES == 500
    assert (
        rfv3.selection_protocol()["max_submissions_probes"]
        == rfv3.MAX_SUBMISSIONS_PROBES
    )


# --- Body-access prohibition ----------------------------------------------------

BODY_URLS = (
    "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/fict.htm",
    "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/fict-ex99.htm",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany",
    "https://data.sec.gov/api/xbrl/frames/us-gaap/Assets/USD/CY2025Q4I.json",
    "https://efts.sec.gov/LATEST/search-index?q=risk",
    "https://example.com/mirror/fict.htm",
    "https://data.sec.gov.evil.example/submissions/CIK0000000001.json",
    "http://data.sec.gov/submissions/CIK0000000001.json",
    "https://data.sec.gov/submissions/../Archives/fict.htm",
)


@pytest.mark.parametrize("url", BODY_URLS)
def test_metadata_allowlist_rejects_every_body_and_lookalike_url(url):
    with pytest.raises(rfv3.NonMetadataEndpoint):
        rfv3.require_metadata_url(url)


def test_the_allowlist_is_the_first_holdouts_by_reference():
    # One implementation, no drift: the v3 module reuses the exact allowlist
    # object rather than re-declaring a second one that could diverge open.
    assert rfv3.require_metadata_url is rfh.require_metadata_url
    assert rfv3.MetadataOnlyFetcher is rfh.MetadataOnlyFetcher


def test_selection_contacts_only_declared_metadata_endpoints():
    source = vfx.metadata_source(paged_ciks=frozenset({103}))
    run_selection(source)
    assert source.requested, "the selection must have fetched metadata"
    for url in source.requested:
        assert rfv3.require_metadata_url(url) == url
    classes = {rfv3.endpoint_class(url) for url in source.requested}
    assert classes <= {"company_tickers", "submissions", "companyfacts"}


def test_a_body_fetching_transport_cannot_be_reached():
    def treacherous(url: str):
        if "/Archives/" in url:
            raise AssertionError("a filing body URL reached the transport")
        return vfx.metadata_source()(url)

    result = rfv3.select_v3_holdout(
        fetch_json=treacherous,
        development_manifest=vfx.synthetic_development_manifest(),
        holdout_manifest=vfx.synthetic_first_holdout_manifest(),
    )
    assert result["selected"] is True
    with pytest.raises(rfv3.NonMetadataEndpoint):
        rfv3.MetadataOnlyFetcher(treacherous).get(BODY_URLS[0])


def test_selection_writes_no_files_and_creates_no_corpus_directory(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    result = run_selection(vfx.metadata_source())
    assert result["selected"] is True
    assert list(tmp_path.iterdir()) == []
    assert not (tmp_path / "benchmark_data").exists()


def test_v3_module_imports_no_network_extraction_or_storage_stack():
    """Checked at the import graph: the selection cannot open a socket, load
    a filing, run extraction or detection, embed, or touch Chroma."""
    source = (REPO_ROOT / "real_filing_v3_holdout.py").read_text(encoding="utf-8")
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
        "real_filing_holdout_acquisition",
        "real_filing_holdout_extraction",
        "ingest",
        "loaders",
        "tools",
        "agent",
        "comparison_detector",
        "comparison_store",
        "comparison_governance",
        "bs4",
    ):
        assert forbidden not in imported, forbidden
    assert imported <= {
        "__future__",
        "hashlib",
        "json",
        "pathlib",
        "typing",
        "real_filing_benchmark",
        "real_filing_holdout",
    }


# --- Audit report ---------------------------------------------------------------


def test_selection_report_counters_prove_no_downstream_activity():
    report = run_selection(vfx.metadata_source())["report"]
    assert report["filing_body_requests"] == 0
    assert report["source_documents_downloaded"] == 0
    assert report["source_checksums_verified"] == 0
    assert report["extraction_runs"] == 0
    assert report["comparison_runs"] == 0
    assert report["annotation_packets"] == 0
    assert report["human_verified_labels"] == 0
    assert report["gold_evaluation_runs"] == 0
    assert report["signoff_present"] is False
    assert report["corpus_role"] == rfb.CORPUS_ROLE_EXTRACTION_HOLDOUT
    assert report["extraction_holdout_evaluation"] is False
    assert report["generalization_claim_supported"] is False
    assert report["extraction_parser_developed_using_this_corpus"] is False
    assert report["evaluation_contract_developed_using_this_corpus"] is False


def test_selection_report_records_protocol_and_exclusion_provenance():
    report = run_selection(vfx.metadata_source())["report"]
    assert report["selection_protocol_hash"] == rfv3.selection_protocol_hash()
    assert (
        report["selection_protocol"]["protocol_version"]
        == rfv3.V3_HOLDOUT_SELECTION_PROTOCOL_VERSION
    )
    assert (
        report["selection_seed_identifier"] == rfv3.SELECTION_SEED_IDENTIFIER
    )
    applied = report["prior_corpus_exclusions_applied"]
    assert len(applied) == 2
    for source in applied:
        assert source["excluded_cik_count"] == len(source["excluded_ciks"])
        assert source["excluded_accession_count"] == len(
            source["excluded_accessions"]
        )
    endpoints = report["metadata_endpoints_contacted"]
    assert set(endpoints) == {"company_tickers", "submissions", "companyfacts"}
    assert endpoints["company_tickers"] == 1
    assert report["official_hosts_contacted"] == ["www.sec.gov", "data.sec.gov"]


def test_selection_report_pins_every_frozen_contract_identity():
    report = run_selection(vfx.metadata_source())["report"]
    assert report["frozen_extraction_parser_version"] == "sec_html_item_headings.v2"
    assert report["frozen_unit_grammar_version"] == "item1a_units.v3"
    assert report["frozen_detector_version"] == "item1a_detector.v3"
    assert report["frozen_workflow_version"] == "comparison_workflow.v3"
    assert report["frozen_evaluation_contract_version"] == (
        "real-filing-benchmark.evaluation.v2"
    )
    assert report["frozen_metric_definitions_version"] == (
        "real-filing-benchmark-metrics.v2"
    )
    assert report["frozen_report_contract_version"] == (
        "real-filing-benchmark.report.v2"
    )
    assert report["frozen_subject_matching"] == "canonical_unit_identity"
    assert report["frozen_unit_identity_contract"] == "side:sequence:unit_key"


def test_report_carries_no_credentials_environment_values_or_local_paths():
    result = run_selection(vfx.metadata_source())
    for payload in (result["report"], result["manifest"]):
        text = json.dumps(payload)
        assert "SEC_USER_AGENT" not in text
        assert "/Users/" not in text
        assert "/home/" not in text
        assert "C:\\" not in text
        assert str(REPO_ROOT) not in text


def test_failed_selection_still_reports_and_freezes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = [
        entry for entry in vfx.DEFAULT_SPEC if not (6000 <= entry[2] <= 6999)
    ]
    result = run_selection(vfx.metadata_source(spec))
    assert result["selected"] is False
    assert result["manifest"] is None
    assert result["report"]["selection_succeeded"] is False
    assert result["report"]["filing_body_requests"] == 0
    assert list(tmp_path.iterdir()) == []


def test_failure_codes_are_stable_and_deterministically_ordered():
    spec = [
        entry for entry in vfx.DEFAULT_SPEC if not (6000 <= entry[2] <= 6999)
    ]
    first = run_selection(vfx.metadata_source(spec))["report"]["failures"]
    second = run_selection(vfx.metadata_source(spec))["report"]["failures"]
    assert first == second
    assert [failure["code"] for failure in first] == [
        rfv3.SELECTION_FAILURE_STRATUM_UNFILLED,
        rfv3.SELECTION_FAILURE_INSUFFICIENT_STRATA,
    ]


# --- Protocol content -----------------------------------------------------------


def test_protocol_is_predeclared_and_hash_stable():
    protocol = rfv3.selection_protocol()
    assert protocol["protocol_version"] == "real-filing-v3-holdout-selection.v1"
    assert protocol["benchmark_id"] == rfv3.V3_HOLDOUT_BENCHMARK_ID
    assert protocol["target_pair_count"] == 10
    assert protocol["target_filing_count"] == 20
    assert protocol["target_previous_fiscal_year"] + 1 == (
        protocol["target_current_fiscal_year"]
    )
    assert len(protocol["sic_strata"]) == 5
    assert protocol["minimum_distinct_strata"] == 5
    assert protocol["selection_seed_identifier"] == rfv3.V3_HOLDOUT_BENCHMARK_ID
    assert rfv3.selection_protocol_hash() == rfb.payload_hash(protocol)
    order = protocol["eligibility_check_order"]
    assert len(order) == len(set(order))
    assert order[0] == rfv3.REASON_PRIOR_CORPUS_CIK


def test_synthetic_regression_gates_remain_untouched():
    from scripts import eval_comparison_regression as ecr

    assert "holdout" not in json.dumps(sorted(ecr.GATES))
    assert len(ecr.GATES) == 10
