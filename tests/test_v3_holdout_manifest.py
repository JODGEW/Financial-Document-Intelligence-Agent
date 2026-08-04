"""The committed v3-holdout artifacts: manifest, evaluation config, report.

This suite freezes the COMMITTED metadata-only v3 holdout
(``benchmarks/real_filing_v3_holdout_v1/``): schema and denial validation of
the manifest, live pinning of every frozen v3/v2 contract identity against
the modules that define them, frozen-code and exclusion-provenance drift
gates, the future evaluation config's declarations, and the selection
report's zero downstream-activity counters. Entirely offline: no network, no
AWS, no filing body, no Chroma.

The companion suite (``tests/test_v3_holdout_selection.py``) covers the
selection algorithm over synthetic metadata; this one covers the frozen
artifacts of record.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

import comparison_detector
import comparison_store
import real_filing_benchmark as rfb
import real_filing_holdout as rfh
import real_filing_v3_holdout as rfv3
from scripts import eval_real_filing_benchmark as evaluator

REPO_ROOT = Path(__file__).resolve().parent.parent
V3_DIR = REPO_ROOT / "benchmarks" / "real_filing_v3_holdout_v1"
MANIFEST_PATH = V3_DIR / "manifest.json"
REPORT_PATH = V3_DIR / "selection_report.json"
CONFIG_PATH = V3_DIR / "evaluation_config.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"

V3_SUITES = (
    "tests/test_v3_holdout_selection.py",
    "tests/test_v3_holdout_manifest.py",
)


@pytest.fixture(scope="module")
def manifest():
    return rfv3.load_v3_holdout_manifest(MANIFEST_PATH)


@pytest.fixture(scope="module")
def report():
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def config_document():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def mutated(manifest, **overrides):
    copy = json.loads(json.dumps(manifest))
    copy.update(overrides)
    return copy


# --- The committed manifest -----------------------------------------------------


def test_committed_manifest_is_valid_and_metadata_only(manifest):
    assert manifest["schema_version"] == "real-filing-v3-holdout.manifest.v1"
    assert manifest["benchmark_id"] == "real_filing_v3_holdout_v1"
    assert manifest["status"] == rfv3.STATUS_HOLDOUT_FROZEN_METADATA_ONLY
    assert manifest["form"] == "10-K"
    assert manifest["target_pair_count"] == 10
    assert manifest["target_previous_fiscal_year"] == 2024
    assert manifest["target_current_fiscal_year"] == 2025


def test_committed_manifest_denies_every_downstream_claim(manifest):
    assert manifest["corpus_role"] == "extraction_holdout_corpus"
    assert manifest["extraction_parser_developed_using_this_corpus"] is False
    assert manifest["evaluation_contract_developed_using_this_corpus"] is False
    assert manifest["extraction_holdout_evaluation"] is False
    assert manifest["generalization_claim_supported"] is False
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            assert pair[side]["expected_sha256"] is None
            assert pair[side]["source_verified"] is False


def test_committed_manifest_has_ten_pairs_over_five_two_issuer_strata(manifest):
    pairs = manifest["pairs"]
    assert len(pairs) == 10
    assert len({pair["cik"] for pair in pairs}) == 10
    assert len({pair["issuer_name"] for pair in pairs}) == 10
    strata: dict[str, int] = {}
    for pair in pairs:
        strata[pair["stratum_id"]] = strata.get(pair["stratum_id"], 0) + 1
    assert strata == {
        "sic-2000s": 2,
        "sic-3000s": 2,
        "sic-4000s": 2,
        "sic-5000s": 2,
        "sic-6000s": 2,
    }
    accessions = [
        pair[side]["accession_number"]
        for pair in pairs
        for side in ("previous", "current")
    ]
    assert len(accessions) == len(set(accessions)) == 20
    assert [pair["pair_id"] for pair in pairs] == sorted(
        pair["pair_id"] for pair in pairs
    )


def test_committed_manifest_is_disjoint_from_both_prior_corpora(manifest):
    dev = rfb.load_manifest()
    first_holdout = rfh.load_holdout_manifest()
    prior_ciks = {entry["cik"] for entry in dev["proposed_issuers"]}
    prior_ciks |= {pair["cik"] for pair in first_holdout["pairs"]}
    prior_accessions = {
        payload["accession_number"]
        for pair in rfb.manifest_pairs(dev)
        for _side, payload in rfb.pair_sides(pair)
    }
    prior_accessions |= {
        pair[side]["accession_number"]
        for pair in first_holdout["pairs"]
        for side in ("previous", "current")
    }
    for pair in manifest["pairs"]:
        assert pair["cik"] not in prior_ciks
        for side in ("previous", "current"):
            assert pair[side]["accession_number"] not in prior_accessions


def test_exclusion_provenance_matches_the_committed_prior_manifests(manifest):
    # Recomputes both exclusion sets and both source-manifest hashes from the
    # committed files: this is simultaneously the drift gate for the frozen
    # exclusion block AND the proof that neither prior manifest has changed
    # since the freeze.
    rfv3.verify_exclusion_provenance(manifest)
    sources = manifest["prior_corpus_exclusions"]["sources"]
    assert [source["benchmark_id"] for source in sources] == [
        "real_filing_v1",
        "real_filing_holdout_v1",
    ]
    assert [source["manifest_path"] for source in sources] == [
        "benchmarks/real_filing_v1/manifest.json",
        "benchmarks/real_filing_holdout_v1/manifest.json",
    ]
    assert sources[0]["manifest_sha256"] == rfb.sha256_file(
        REPO_ROOT / "benchmarks" / "real_filing_v1" / "manifest.json"
    )
    assert sources[1]["manifest_sha256"] == rfb.sha256_file(
        REPO_ROOT / "benchmarks" / "real_filing_holdout_v1" / "manifest.json"
    )
    for source in sources:
        assert len(source["excluded_ciks"]) == 10
        assert len(source["excluded_accessions"]) == 20


def test_exclusion_hash_drift_is_refused(manifest):
    poisoned = json.loads(json.dumps(manifest))
    poisoned["prior_corpus_exclusions"]["sources"][0]["manifest_sha256"] = "a" * 64
    with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
        rfv3.verify_exclusion_provenance(poisoned)
    assert excinfo.value.code == "v3_holdout_exclusion_source_drift"


def test_exclusion_set_drift_is_refused(manifest):
    poisoned = json.loads(json.dumps(manifest))
    poisoned["prior_corpus_exclusions"]["sources"][1]["excluded_ciks"] = [
        "0000000001"
    ]
    with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
        rfv3.verify_exclusion_provenance(poisoned)
    assert excinfo.value.code == "v3_holdout_exclusion_set_drift"


# --- Schema denials -------------------------------------------------------------


def test_unknown_manifest_key_is_rejected(manifest):
    with pytest.raises(rfv3.V3HoldoutManifestError):
        rfv3.validate_v3_holdout_manifest(mutated(manifest, surprise=True))


def test_missing_manifest_key_is_rejected(manifest):
    broken = json.loads(json.dumps(manifest))
    del broken["frozen_detector_version"]
    with pytest.raises(rfv3.V3HoldoutManifestError):
        rfv3.validate_v3_holdout_manifest(broken)


def test_prior_benchmark_ids_are_rejected(manifest):
    for stolen in (rfb.BENCHMARK_ID, rfh.HOLDOUT_BENCHMARK_ID):
        with pytest.raises(rfv3.V3HoldoutManifestError):
            rfv3.validate_v3_holdout_manifest(
                mutated(manifest, benchmark_id=stolen)
            )


def test_verification_claims_are_rejected_while_metadata_only(manifest):
    poisoned = json.loads(json.dumps(manifest))
    poisoned["pairs"][0]["previous"]["expected_sha256"] = "b" * 64
    with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
        rfv3.validate_v3_holdout_manifest(poisoned)
    assert excinfo.value.code == "v3_holdout_side_unexpected_sha256"

    poisoned = json.loads(json.dumps(manifest))
    poisoned["pairs"][0]["current"]["source_verified"] = True
    with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
        rfv3.validate_v3_holdout_manifest(poisoned)
    assert excinfo.value.code == "v3_holdout_side_claims_verification"


def test_corpus_role_overclaims_are_rejected(manifest):
    for field in (
        "extraction_parser_developed_using_this_corpus",
        "evaluation_contract_developed_using_this_corpus",
        "extraction_holdout_evaluation",
        "generalization_claim_supported",
    ):
        with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
            rfv3.validate_v3_holdout_manifest(mutated(manifest, **{field: True}))
        assert excinfo.value.code == "v3_holdout_manifest_corpus_role_mismatch"


def test_amendment_and_foreign_forms_are_rejected(manifest):
    for form in ("10-K/A", "20-F", "40-F"):
        poisoned = json.loads(json.dumps(manifest))
        poisoned["pairs"][0]["previous"]["form"] = form
        with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
            rfv3.validate_v3_holdout_manifest(poisoned)
        assert excinfo.value.code == "v3_holdout_side_invalid_form"


def test_duplicate_pair_content_is_rejected(manifest):
    poisoned = json.loads(json.dumps(manifest))
    donor = poisoned["pairs"][0]
    target = poisoned["pairs"][1]
    target["cik"] = donor["cik"]
    with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
        rfv3.validate_v3_holdout_manifest(poisoned)
    assert excinfo.value.code == "v3_holdout_pair_duplicate_cik"

    poisoned = json.loads(json.dumps(manifest))
    poisoned["pairs"][1]["previous"]["accession_number"] = (
        poisoned["pairs"][0]["previous"]["accession_number"]
    )
    with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
        rfv3.validate_v3_holdout_manifest(poisoned)
    assert excinfo.value.code == "v3_holdout_pair_duplicate_accession"


def test_unordered_pairs_are_rejected(manifest):
    poisoned = json.loads(json.dumps(manifest))
    poisoned["pairs"].reverse()
    with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
        rfv3.validate_v3_holdout_manifest(poisoned)
    assert excinfo.value.code == "v3_holdout_pairs_unordered"


def test_wrong_fiscal_years_are_rejected(manifest):
    with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
        rfv3.validate_v3_holdout_manifest(
            mutated(manifest, target_previous_fiscal_year=2023)
        )
    assert excinfo.value.code == "v3_holdout_manifest_fiscal_years_mismatch"


def test_status_transitions_are_single_forward_steps():
    order = rfv3.V3_HOLDOUT_STATUS_ORDER
    rfv3.validate_v3_holdout_status_transition(order[0], order[1])
    with pytest.raises(rfb.StatusTransitionError):
        rfv3.validate_v3_holdout_status_transition(order[0], order[2])
    with pytest.raises(rfb.StatusTransitionError):
        rfv3.validate_v3_holdout_status_transition(order[1], order[0])
    with pytest.raises(rfb.StatusTransitionError):
        rfv3.validate_v3_holdout_status_transition(order[0], order[0])


# --- Frozen contract identities pinned to the live modules ----------------------


def test_frozen_versions_match_the_live_modules(manifest):
    parser_source = (REPO_ROOT / "loaders" / "sec_headings.py").read_text(
        encoding="utf-8"
    )
    live_parser = re.search(
        r'^PARSER_VERSION = "([^"]+)"', parser_source, re.MULTILINE
    ).group(1)
    assert manifest["frozen_extraction_parser_version"] == live_parser
    assert manifest["frozen_unit_grammar_version"] == (
        comparison_detector.UNIT_GRAMMAR_V3
    )
    assert manifest["frozen_unit_grammar_version"] == (
        comparison_detector.DEFAULT_UNIT_GRAMMAR
    )
    assert manifest["frozen_detector_version"] == (
        comparison_detector.DETECTOR_VERSION
    )
    assert manifest["frozen_workflow_version"] == (
        comparison_store.WORKFLOW_VERSION
    )
    assert manifest["frozen_evaluation_contract_version"] == (
        evaluator.EVALUATION_CONFIG_VERSION_V2
    )
    assert manifest["frozen_metric_definitions_version"] == (
        evaluator.METRIC_DEFINITIONS_VERSION_V2
    )
    assert manifest["frozen_report_contract_version"] == (
        evaluator.EVALUATION_REPORT_VERSION_V2
    )
    assert manifest["frozen_subject_matching"] == evaluator.SUBJECT_MATCHING_V2
    assert manifest["frozen_annotation_schema_version"] == (
        rfb.ANNOTATION_SCHEMA_VERSION
    )
    assert manifest["frozen_annotation_protocol_version"] == (
        rfb.ANNOTATION_PROTOCOL_VERSION
    )
    # The canonical sequence-aware unit identity: the shape rfb.unit_id
    # produces (zero-padded sequence) and contract v2 matches by.
    assert manifest["frozen_unit_identity_contract"] == "side:sequence:unit_key"
    assert rfb.unit_id("previous", 3, "some_unit") == "previous:003:some_unit"


def test_frozen_code_hashes_match_the_live_files(manifest):
    # The freeze discipline: editing any pinned file after this freeze is a
    # detectable fact, and doing it in response to results from this corpus
    # converts the corpus into development data.
    rfv3.verify_frozen_code_identities(manifest)
    for hash_field, path_field, path in rfv3.FROZEN_SOURCE_FILES:
        assert manifest[path_field] == path
        assert manifest[hash_field] == rfb.sha256_file(REPO_ROOT / path)


def test_frozen_code_drift_is_refused(manifest):
    poisoned = mutated(manifest, frozen_detector_source_sha256="c" * 64)
    with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
        rfv3.verify_frozen_code_identities(poisoned)
    assert excinfo.value.code == "v3_holdout_frozen_code_drift"
    assert "comparison_detector.py" in excinfo.value.message


def test_mixed_or_unknown_contract_versions_are_rejected(manifest):
    mixes = (
        {"frozen_unit_grammar_version": "item1a_units.v2"},
        {"frozen_detector_version": "item1a_detector.v2"},
        {"frozen_workflow_version": "comparison_workflow.v2"},
        {"frozen_evaluation_contract_version": "real-filing-benchmark.evaluation.v1"},
        {"frozen_metric_definitions_version": "real-filing-benchmark-metrics.v1"},
        {"frozen_report_contract_version": "real-filing-benchmark.report.v1"},
        {"frozen_detector_version": "item1a_detector.v9"},
        {"frozen_subject_matching": "normalized_unit_key"},
        {"frozen_extraction_parser_version": "sec_html_item_headings.v3"},
    )
    for overrides in mixes:
        with pytest.raises(rfv3.V3HoldoutManifestError) as excinfo:
            rfv3.validate_v3_holdout_manifest(mutated(manifest, **overrides))
        assert excinfo.value.code == (
            "v3_holdout_manifest_frozen_identity_mismatch"
        ), overrides


def test_protocol_hash_pins_the_selection_rules(manifest):
    assert manifest["selection_protocol_version"] == (
        "real-filing-v3-holdout-selection.v1"
    )
    assert manifest["selection_protocol_hash"] == rfv3.selection_protocol_hash()
    assert manifest["selection_seed_identifier"] == "real_filing_v3_holdout_v1"
    with pytest.raises(rfv3.V3HoldoutManifestError):
        rfv3.validate_v3_holdout_manifest(
            mutated(manifest, selection_protocol_hash="d" * 64)
        )


# --- The committed selection report ---------------------------------------------


def test_report_matches_manifest_and_records_zero_downstream_activity(
    manifest, report
):
    assert report["report_version"] == "real-filing-v3-holdout.selection-report.v1"
    assert report["benchmark_id"] == manifest["benchmark_id"]
    assert report["selection_succeeded"] is True
    assert report["failures"] == []
    assert report["selection_protocol_hash"] == manifest["selection_protocol_hash"]
    assert report["manifest_status"] == manifest["status"]
    assert report["holdout_manifest_sha256"] == rfb.sha256_file(MANIFEST_PATH)
    assert report["reproducible_manifest_hash"] == (
        rfv3.reproducible_manifest_hash(manifest)
    )
    assert report["filing_body_requests"] == 0
    assert report["source_documents_downloaded"] == 0
    assert report["source_checksums_verified"] == 0
    assert report["extraction_runs"] == 0
    assert report["comparison_runs"] == 0
    assert report["annotation_packets"] == 0
    assert report["human_verified_labels"] == 0
    assert report["gold_evaluation_runs"] == 0
    assert report["signoff_present"] is False
    assert report["extraction_holdout_evaluation"] is False
    assert report["generalization_claim_supported"] is False
    assert report["official_hosts_contacted"] == ["www.sec.gov", "data.sec.gov"]
    assert set(report["metadata_endpoints_contacted"]) == {
        "company_tickers",
        "submissions",
        "companyfacts",
    }
    assert report["submissions_probes"] <= rfv3.MAX_SUBMISSIONS_PROBES


def test_report_pair_table_matches_the_manifest(manifest, report):
    manifest_pairs = {
        pair["cik"]: (
            pair["previous"]["accession_number"],
            pair["current"]["accession_number"],
            pair["stratum_id"],
        )
        for pair in manifest["pairs"]
    }
    report_pairs = {
        pair["cik"]: (
            pair["previous_accession"],
            pair["current_accession"],
            pair["stratum_id"],
        )
        for pair in report["selected_pairs"]
    }
    assert report_pairs == manifest_pairs
    assert report["selected_pair_count"] == 10
    assert report["stratum_distribution"] == {
        "sic-2000s": 2,
        "sic-3000s": 2,
        "sic-4000s": 2,
        "sic-5000s": 2,
        "sic-6000s": 2,
    }


def test_committed_artifacts_carry_no_credentials_paths_or_body_urls(
    manifest, report, config_document
):
    for payload in (manifest, report, config_document):
        text = json.dumps(payload)
        assert "SEC_USER_AGENT" not in text
        assert "/Users/" not in text
        assert "/home/" not in text
        assert "C:\\" not in text
        assert "/Archives/" not in text
        assert str(REPO_ROOT) not in text
    for pair in manifest["pairs"]:
        for reference in pair["metadata_source_references"]:
            assert rfv3.require_metadata_url(reference) == reference


def test_no_source_documents_or_downstream_artifacts_exist(manifest):
    if manifest["status"] != rfv3.STATUS_HOLDOUT_FROZEN_METADATA_ONLY:
        pytest.skip("corpus advanced beyond metadata-only in a later commit")
    assert not (REPO_ROOT / "benchmark_data" / "real_filing_v3_holdout_v1").exists()
    committed = sorted(path.name for path in V3_DIR.iterdir())
    assert committed == [
        "evaluation_config.json",
        "manifest.json",
        "selection_report.json",
    ]


# --- The future evaluation config -----------------------------------------------

# The sign-off key is named via the evaluator's constant on purpose: the
# repo-wide no-writer scan rightly rejects new files that spell it out.
_EXPECTED_CONFIG_KEYS = (
    "config_version",
    "benchmark_id",
    "metric_definitions_version",
    "annotation_protocol_version",
    "declared_detector_version",
    "declared_workflow_version",
    "declared_unit_grammar_version",
    "declared_section_key",
    "gold_status_required",
    "pass_fail_thresholds",
    evaluator.SIGNOFF_FIELD,
    "notes",
)


def test_config_has_the_exact_declared_key_set(config_document):
    assert tuple(config_document) == _EXPECTED_CONFIG_KEYS


def test_config_resolves_to_the_frozen_contract_v2(config_document):
    contract = evaluator.resolve_evaluation_contract(config_document)
    assert contract.contract_version == evaluator.EVALUATION_CONFIG_VERSION_V2
    assert contract.metric_definitions_version == (
        evaluator.METRIC_DEFINITIONS_VERSION_V2
    )
    assert contract.report_version == evaluator.EVALUATION_REPORT_VERSION_V2
    assert contract.scored_detector_version == "item1a_detector.v3"
    assert contract.scored_workflow_version == "comparison_workflow.v3"
    assert contract.subject_matching == "canonical_unit_identity"


def test_config_binds_to_this_manifest_and_pins_every_identity(
    manifest, config_document
):
    assert config_document["benchmark_id"] == manifest["benchmark_id"]
    assert config_document["declared_detector_version"] == (
        manifest["frozen_detector_version"]
    )
    assert config_document["declared_workflow_version"] == (
        manifest["frozen_workflow_version"]
    )
    assert config_document["declared_unit_grammar_version"] == (
        manifest["frozen_unit_grammar_version"]
    )
    assert config_document["config_version"] == (
        manifest["frozen_evaluation_contract_version"]
    )
    assert config_document["metric_definitions_version"] == (
        manifest["frozen_metric_definitions_version"]
    )
    assert config_document["annotation_protocol_version"] == (
        manifest["frozen_annotation_protocol_version"]
    )
    assert config_document["declared_section_key"] == "item_1a_risk_factors"
    assert config_document["gold_status_required"] == rfb.GOLD_STATUS


def test_config_declares_no_result_no_threshold_and_no_signoff(config_document):
    assert config_document["pass_fail_thresholds"] is None
    assert config_document[evaluator.SIGNOFF_FIELD] is None
    text = json.dumps(config_document)
    for banned in (
        "change_precision\":",
        "change_recall\":",
        "pairs_scored\":",
        "annotation_status",
    ):
        assert banned not in text
    assert evaluator.validate_signoff_document(config_document) is None


def test_config_cannot_downgrade_or_infer_missing_versions(config_document):
    legacy = dict(config_document)
    legacy["config_version"] = evaluator.EVALUATION_CONFIG_VERSION_V1
    legacy["metric_definitions_version"] = evaluator.METRIC_DEFINITIONS_VERSION_V1
    with pytest.raises(evaluator.EvaluationRefused):
        # A v1 pairing cannot carry a unit-grammar declaration: no silent
        # downgrade of this config to the frozen v1 contract is possible.
        evaluator.resolve_evaluation_contract(legacy)

    for missing in ("config_version", "metric_definitions_version"):
        broken = dict(config_document)
        del broken[missing]
        with pytest.raises(evaluator.EvaluationRefused) as excinfo:
            evaluator.resolve_evaluation_contract(broken)
        assert excinfo.value.code == evaluator.CONTRACT_VERSION_UNKNOWN

    broken = dict(config_document)
    del broken["declared_unit_grammar_version"]
    with pytest.raises(evaluator.EvaluationRefused) as excinfo:
        evaluator.resolve_evaluation_contract(broken)
    assert excinfo.value.code == evaluator.CONTRACT_INCOMPATIBLE_UNIT_IDENTITY


def test_metadata_only_corpus_cannot_be_scored():
    # The declaration is accepted, but no evaluation branch reads the v3
    # holdout manifest schema: gold scoring is structurally unreachable while
    # the corpus is metadata-only, in ANY mode, not refused by convention.
    with pytest.raises(evaluator.EvaluationRefused) as excinfo:
        evaluator.load_manifest_dispatch(MANIFEST_PATH)
    assert excinfo.value.code == "manifest_schema_version_unsupported"


# --- CI wiring ------------------------------------------------------------------


def _workflow():
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_both_v3_holdout_suites_run_in_the_required_check():
    job = _workflow()["jobs"]["comparison-regression"]
    runs = "\n".join(
        step["run"]
        for step in job["steps"]
        if isinstance(step.get("run"), str) and "pytest" in step["run"]
    )
    for suite in V3_SUITES:
        assert suite in runs
        assert (REPO_ROOT / suite).is_file()


def test_required_check_identity_is_unchanged():
    workflow = _workflow()
    assert workflow["name"] == "comparison-regression"
    assert list(workflow["jobs"]) == ["comparison-regression"]
    job = workflow["jobs"]["comparison-regression"]
    assert job["name"] == "comparison-regression"
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]
    for value in triggers.values():
        if isinstance(value, dict):
            assert "paths" not in value
            assert "paths-ignore" not in value


def test_required_check_gains_no_credentials_or_network():
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "--allow-network",
        "SEC_USER_AGENT",
        "sec.gov",
        "secrets.",
        "AWS_ACCESS_KEY",
        "configure-aws-credentials",
        "select_real_filing_v3_holdout",
    ):
        assert forbidden not in raw, forbidden
