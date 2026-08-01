"""Frozen holdout manifest, selection report, CI, and documentation tests.

The committed artifacts under ``benchmarks/real_filing_holdout_v1/`` are the
pre-registration of the extraction holdout: exact issuers and filing pairs,
frozen from official metadata AFTER ``sec_html_item_headings.v2`` was frozen
and BEFORE any selected filing body was downloaded. These tests pin what those
artifacts may and may not claim — most importantly that nothing anywhere says
a body was verified, an extraction ran, or a generalization claim exists.

Schema-mutation tests use a deep copy of the committed manifest rather than a
synthetic one, so the validator is exercised against the exact document shape
that is actually frozen.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
import yaml

import real_filing_benchmark as rfb
import real_filing_holdout as rfh

REPO_ROOT = Path(__file__).resolve().parent.parent
HOLDOUT_DIR = REPO_ROOT / "benchmarks" / "real_filing_holdout_v1"
MANIFEST_PATH = HOLDOUT_DIR / "manifest.json"
REPORT_PATH = HOLDOUT_DIR / "selection_report.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"

HOLDOUT_SUITES = (
    "tests/test_real_filing_holdout_selection.py",
    "tests/test_real_filing_holdout_manifest.py",
)


@pytest.fixture(scope="module")
def manifest():
    return rfh.load_holdout_manifest(MANIFEST_PATH)


@pytest.fixture(scope="module")
def report():
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def mutated(manifest):
    return copy.deepcopy(manifest)


# --- The committed freeze -------------------------------------------------------


def test_committed_manifest_validates_and_is_metadata_only(manifest):
    assert manifest["status"] == rfh.STATUS_HOLDOUT_FROZEN_METADATA_ONLY
    assert manifest["benchmark_id"] == "real_filing_holdout_v1"
    assert manifest["benchmark_id"] != rfb.BENCHMARK_ID
    assert len(manifest["pairs"]) == 10
    assert manifest["form"] == "10-K"


def test_committed_manifest_freezes_twenty_distinct_primary_filings(manifest):
    accessions = [
        pair[side]["accession_number"]
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    ]
    assert len(accessions) == 20
    assert len(set(accessions)) == 20
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            assert pair[side]["form"] == "10-K"
        assert pair["previous"]["filing_date"] < pair["current"]["filing_date"]
        assert (
            pair["previous"]["reporting_period"]
            < pair["current"]["reporting_period"]
        )
        assert (
            pair["target_current_fiscal_year"]
            == pair["target_previous_fiscal_year"] + 1
        )


def test_committed_manifest_spans_the_declared_strata(manifest):
    strata = {pair["stratum_id"] for pair in manifest["pairs"]}
    assert len(strata) >= 5
    declared = {stratum["stratum_id"]: stratum for stratum in rfh.SIC_STRATA}
    for pair in manifest["pairs"]:
        low, high = declared[pair["stratum_id"]]["sic_range"]
        assert low <= pair["sic"] <= high


def test_committed_manifest_is_disjoint_from_the_development_corpus(manifest):
    development = rfb.load_manifest()
    development_ciks = {
        entry["cik"] for entry in development["proposed_issuers"]
    }
    development_accessions = {
        payload["accession_number"]
        for pair in rfb.manifest_pairs(development)
        for _side, payload in rfb.pair_sides(pair)
    }
    holdout_ciks = {pair["cik"] for pair in manifest["pairs"]}
    holdout_accessions = {
        pair[side]["accession_number"]
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    }
    assert not (holdout_ciks & development_ciks)
    assert not (holdout_accessions & development_accessions)
    assert len(holdout_ciks) == 10
    # The frozen exclusion block matches the committed development manifest
    # exactly — derived, not retyped.
    assert manifest["development_exclusions"] == {
        "development_benchmark_id": development["benchmark_id"],
        **{
            key: value
            for key, value in rfh.development_exclusions(development).items()
            if key != "development_benchmark_id"
        },
    }


def test_no_source_hash_and_no_verification_exists_anywhere(manifest):
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            assert pair[side]["expected_sha256"] is None
            assert pair[side]["source_verified"] is False


def test_metadata_source_references_are_metadata_endpoints_only(manifest):
    for pair in manifest["pairs"]:
        assert pair["metadata_source_references"]
        for reference in pair["metadata_source_references"]:
            assert rfh.require_metadata_url(reference) == reference


# --- Frozen parser --------------------------------------------------------------


def test_frozen_parser_version_matches_the_shipped_parser(manifest):
    assert (
        manifest["frozen_extraction_parser_version"]
        == rfh.FROZEN_EXTRACTION_PARSER_VERSION
    )
    # Read the parser source rather than importing the loader stack: this
    # suite must not require the extraction dependency graph.
    source = (REPO_ROOT / rfh.FROZEN_PARSER_SOURCE_PATH).read_text(
        encoding="utf-8"
    )
    match = re.search(r'^PARSER_VERSION = "([^"]+)"$', source, re.MULTILINE)
    assert match is not None
    assert match.group(1) == manifest["frozen_extraction_parser_version"]


def test_frozen_parser_source_hash_pins_the_exact_parser_bytes(manifest):
    """If this fails, the parser changed after the holdout froze. That is not
    a test to appease: modifying the frozen parser in response to holdout
    results converts the holdout into development data. A deliberate,
    documented parser change requires freezing a NEW holdout corpus."""
    assert manifest["frozen_parser_source_sha256"] == (
        rfh.frozen_parser_source_sha256()
    )


# --- Protocol binding -----------------------------------------------------------


def test_manifest_records_the_protocol_that_produced_it(manifest):
    assert (
        manifest["selection_protocol_version"]
        == rfh.HOLDOUT_SELECTION_PROTOCOL_VERSION
    )
    assert manifest["selection_protocol_hash"] == rfh.selection_protocol_hash()


def test_manifest_hash_is_deterministic_and_matches_the_report(report):
    assert rfh.holdout_manifest_hash(MANIFEST_PATH) == (
        report["holdout_manifest_sha256"]
    )
    assert rfh.holdout_manifest_hash(MANIFEST_PATH) == (
        rfb.sha256_file(MANIFEST_PATH)
    )


# --- Corpus role ----------------------------------------------------------------


def test_manifest_carries_the_holdout_corpus_role_without_overclaiming(manifest):
    assert manifest["corpus_role"] == "extraction_holdout_corpus"
    assert manifest["extraction_parser_developed_using_this_corpus"] is False
    assert manifest["extraction_holdout_evaluation"] is False
    assert manifest["generalization_claim_supported"] is False


def test_holdout_role_block_never_flips_claims_on_its_own():
    fields = rfh.holdout_corpus_role_fields()
    assert fields["extraction_holdout_evaluation"] is False
    assert fields["generalization_claim_supported"] is False
    assert fields["corpus_role"] in rfb.CORPUS_ROLES


@pytest.mark.parametrize("name", ("manifest.json", "selection_report.json"))
def test_committed_artifacts_state_their_denials_structurally(name):
    raw = (HOLDOUT_DIR / name).read_text(encoding="utf-8")
    assert '"extraction_holdout_evaluation": false' in raw
    assert '"generalization_claim_supported": false' in raw
    assert '"extraction_holdout_evaluation": true' not in raw
    assert '"generalization_claim_supported": true' not in raw


# --- Schema strictness ----------------------------------------------------------


def test_unknown_manifest_key_is_rejected(manifest):
    document = mutated(manifest)
    document["surprise"] = 1
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code == "holdout_manifest_unknown_keys"


def test_unknown_pair_key_is_rejected(manifest):
    document = mutated(manifest)
    document["pairs"][0]["surprise"] = 1
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code == "holdout_pair_unknown_keys"


def test_a_digest_cannot_appear_while_metadata_only(manifest):
    document = mutated(manifest)
    document["pairs"][0]["previous"]["expected_sha256"] = "a" * 64
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code == "holdout_side_unexpected_sha256"


def test_source_verified_cannot_be_claimed_while_metadata_only(manifest):
    document = mutated(manifest)
    document["pairs"][0]["current"]["source_verified"] = True
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code == "holdout_side_claims_verification"


def test_an_amendment_can_never_be_frozen(manifest):
    document = mutated(manifest)
    document["pairs"][0]["previous"]["form"] = "10-K/A"
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code == "holdout_side_invalid_form"


def test_a_development_cik_can_never_be_frozen(manifest):
    document = mutated(manifest)
    development_cik = document["development_exclusions"]["excluded_ciks"][0]
    document["pairs"][0]["cik"] = development_cik
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code == "holdout_pair_development_cik"


def test_a_development_accession_can_never_be_frozen(manifest):
    document = mutated(manifest)
    development_accession = document["development_exclusions"][
        "excluded_accessions"
    ][0]
    document["pairs"][0]["previous"]["accession_number"] = development_accession
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code == "holdout_pair_development_accession"


def test_duplicate_issuer_cik_or_accession_is_rejected(manifest):
    document = mutated(manifest)
    document["pairs"][1]["cik"] = document["pairs"][0]["cik"]
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code == "holdout_pair_duplicate_cik"

    document = mutated(manifest)
    document["pairs"][1]["issuer_name"] = document["pairs"][0]["issuer_name"]
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code == "holdout_pair_duplicate_issuer"

    document = mutated(manifest)
    document["pairs"][1]["previous"]["accession_number"] = (
        document["pairs"][0]["previous"]["accession_number"]
    )
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code == "holdout_pair_duplicate_accession"


def test_sic_outside_the_declared_stratum_is_rejected(manifest):
    document = mutated(manifest)
    document["pairs"][0]["sic"] = 9999
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code == "holdout_pair_sic_outside_stratum"


def test_a_stale_protocol_hash_is_rejected(manifest):
    document = mutated(manifest)
    document["selection_protocol_hash"] = "b" * 64
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code == "holdout_manifest_protocol_hash_mismatch"


def test_unordered_pairs_are_rejected(manifest):
    document = mutated(manifest)
    document["pairs"].reverse()
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code == "holdout_pairs_unordered"


def test_reusing_the_development_benchmark_id_is_rejected(manifest):
    document = mutated(manifest)
    document["benchmark_id"] = rfb.BENCHMARK_ID
    with pytest.raises(rfh.HoldoutManifestError) as excinfo:
        rfh.validate_holdout_manifest(document)
    assert excinfo.value.code in (
        "holdout_manifest_invalid_benchmark_id",
        "holdout_manifest_reuses_development_id",
    )


def test_corpus_role_fields_cannot_be_edited_into_a_claim(manifest):
    for field in (
        "extraction_holdout_evaluation",
        "generalization_claim_supported",
        "extraction_parser_developed_using_this_corpus",
    ):
        document = mutated(manifest)
        document[field] = True
        with pytest.raises(rfh.HoldoutManifestError) as excinfo:
            rfh.validate_holdout_manifest(document)
        assert excinfo.value.code == "holdout_manifest_corpus_role_mismatch"


# --- Status ladder --------------------------------------------------------------


def test_status_advances_one_step_forward_only():
    rfh.validate_holdout_status_transition(
        rfh.STATUS_HOLDOUT_FROZEN_METADATA_ONLY, rfb.STATUS_SOURCE_VERIFIED
    )
    with pytest.raises(rfb.StatusTransitionError):
        rfh.validate_holdout_status_transition(
            rfh.STATUS_HOLDOUT_FROZEN_METADATA_ONLY, rfb.STATUS_CORPUS_BUILT
        )
    with pytest.raises(rfb.StatusTransitionError):
        rfh.validate_holdout_status_transition(
            rfb.STATUS_SOURCE_VERIFIED,
            rfh.STATUS_HOLDOUT_FROZEN_METADATA_ONLY,
        )
    with pytest.raises(rfb.StatusTransitionError):
        rfh.validate_holdout_status_transition(
            rfh.STATUS_HOLDOUT_FROZEN_METADATA_ONLY,
            rfh.STATUS_HOLDOUT_FROZEN_METADATA_ONLY,
        )


# --- Committed selection report -------------------------------------------------


def test_report_counters_prove_no_body_extraction_or_label_activity(report):
    assert report["selection_succeeded"] is True
    assert report["filing_body_requests"] == 0
    assert report["source_documents_downloaded"] == 0
    assert report["source_checksums_verified"] == 0
    assert report["extraction_runs"] == 0
    assert report["comparison_runs"] == 0
    assert report["annotation_packets"] == 0
    assert report["human_verified_labels"] == 0
    endpoints = report["metadata_endpoints_contacted"]
    assert set(endpoints) == {"company_tickers", "submissions", "companyfacts"}
    assert all(count >= 1 for count in endpoints.values())


def test_report_and_manifest_freeze_the_same_selection(report, manifest):
    report_pairs = {
        (pair["cik"], pair["previous_accession"], pair["current_accession"])
        for pair in report["selected_pairs"]
    }
    manifest_pairs = {
        (
            pair["cik"],
            pair["previous"]["accession_number"],
            pair["current"]["accession_number"],
        )
        for pair in manifest["pairs"]
    }
    assert report_pairs == manifest_pairs
    assert report["selection_protocol_hash"] == (
        manifest["selection_protocol_hash"]
    )
    assert report["frozen_parser_source_sha256"] == (
        manifest["frozen_parser_source_sha256"]
    )


def test_committed_artifacts_leak_no_credentials_paths_or_content():
    for path in (MANIFEST_PATH, REPORT_PATH):
        raw = path.read_text(encoding="utf-8")
        lowered = raw.lower()
        assert "@" not in raw, path.name  # no contact address, no user agent
        assert "sec_user_agent" not in lowered, path.name
        assert "/users/" not in lowered, path.name
        assert "/home/" not in lowered, path.name
        assert "c:\\" not in lowered, path.name
        assert "/archives/" not in lowered, path.name  # no body URL anywhere
        assert "<html" not in lowered, path.name
        assert "risk factors" not in lowered, path.name  # no filing prose


def test_no_holdout_corpus_directory_exists():
    """Selection acquires nothing: the gitignored corpus directory for the
    holdout must not exist until the later, separate acquisition step."""
    assert not (REPO_ROOT / "benchmark_data" / "real_filing_holdout_v1").exists()


# --- Development corpus is untouched --------------------------------------------


def test_development_manifest_and_reports_are_unchanged():
    development = rfb.load_manifest()
    assert development["benchmark_id"] == "real_filing_v1"
    assert development["status"] == rfb.STATUS_SOURCE_VERIFIED
    assert len(development["pairs"]) == 10
    development_dir = REPO_ROOT / "benchmarks" / "real_filing_v1"
    for name in (
        "corpus_build_report.v2.json",
        "execution_report.v2.json",
        "annotation_packet_inventory.v2.json",
    ):
        report = json.loads(
            (development_dir / name).read_text(encoding="utf-8")
        )
        assert report["corpus_role"] == "extraction_development_corpus"
        assert report["generalization_claim_supported"] is False


# --- CI -------------------------------------------------------------------------


def _workflow():
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_required_check_runs_the_holdout_suites():
    workflow = _workflow()
    job = workflow["jobs"]["comparison-regression"]
    runs = "\n".join(
        step["run"]
        for step in job["steps"]
        if isinstance(step.get("run"), str) and "pytest" in step["run"]
    )
    for suite in HOLDOUT_SUITES:
        assert suite in runs, suite
        assert (REPO_ROOT / suite).is_file(), suite


def test_required_check_identity_is_unchanged():
    workflow = _workflow()
    assert workflow["name"] == "comparison-regression"
    assert list(workflow["jobs"]) == ["comparison-regression"]
    job = workflow["jobs"]["comparison-regression"]
    assert job["name"] == "comparison-regression"
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]
    pull_request = triggers.get("pull_request")
    assert pull_request is None or "paths" not in pull_request


def test_required_check_remains_offline_and_credential_free():
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "select_real_filing_holdout",
        "--allow-network",
        "SEC_USER_AGENT",
        "secrets.",
        "AWS_ACCESS_KEY",
    ):
        assert forbidden not in raw, forbidden


# --- Documentation --------------------------------------------------------------


def test_documentation_states_the_holdout_boundaries():
    for doc in ("BENCHMARK.md", "README.MD"):
        lowered = (REPO_ROOT / doc).read_text(encoding="utf-8").lower()
        assert "real_filing_holdout_v1" in lowered, doc
        assert "holdout_frozen_metadata_only" in lowered, doc
        assert "metadata" in lowered, doc
