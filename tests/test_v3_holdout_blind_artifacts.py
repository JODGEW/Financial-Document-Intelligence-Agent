"""The committed v3 holdout blind-run artifacts.

This suite freezes what the blind run actually produced and committed under
``benchmarks/real_filing_v3_holdout_v1/``: the manifest transition and its hash
chain, the twenty bounded per-side rows, the ten per-pair rows, the unlabeled
execution report, the machine-proposed packet inventory, and the boundaries
none of them may cross. Entirely offline: no network, no AWS, no filing body,
no Chroma, no evaluator.

The companion suite (``tests/test_v3_holdout_blind_extraction.py``) covers the
pipeline's behavior over synthetic fixtures; this one covers the artifacts of
record and the frozen material that must NOT have moved.

The blind result is pinned exactly as observed — 16 of 20 sides extracted, 2
missing, 2 ambiguous, 8 of 10 pairs buildable — so that "the outcome was
preserved, not repaired" is a checked fact. Changing any of these numbers means
either the frozen pipeline changed (which converts this corpus into development
data) or a source changed (which the digest gate refuses).
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
import real_filing_v3_holdout as rfv3
import real_filing_v3_holdout_extraction as rfx
# Imported for its SIGNOFF_FIELD constant only. The sign-off key is never
# spelled out in a new file: a repo-wide scan rejects any file that does,
# which is how "no tool writes a sign-off" stays checkable. Importing the
# evaluator module is not running an evaluation, and the blind-run modules
# themselves never import it.
from scripts import eval_real_filing_benchmark as evaluator

REPO_ROOT = Path(__file__).resolve().parent.parent
V3_DIR = REPO_ROOT / "benchmarks" / "real_filing_v3_holdout_v1"
MANIFEST_PATH = V3_DIR / "manifest.json"
BLIND_PATH = V3_DIR / "blind_extraction_report.json"
EXECUTION_PATH = V3_DIR / "execution_report.json"
INVENTORY_PATH = V3_DIR / "annotation_packet_inventory.json"
CONFIG_PATH = V3_DIR / "evaluation_config.json"
SELECTION_PATH = V3_DIR / "selection_report.json"
SOURCE_REPORT_PATH = V3_DIR / "source_verification_report.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"

BLIND_SUITES = (
    "tests/test_v3_holdout_blind_extraction.py",
    "tests/test_v3_holdout_blind_artifacts.py",
)

#: The blind result, exactly as the frozen v3 pipeline produced it.
EXPECTED_EXTRACTION_COUNTS = {
    "extracted": 16,
    "missing": 2,
    "ambiguous": 2,
    "parse_failed": 0,
}
EXPECTED_BLOCKED_PAIRS = {
    "sic-3000s-01": rfx.BLOCKED_BOTH_SIDES_NOT_EXTRACTED,
    "sic-6000s-02": rfx.BLOCKED_BOTH_SIDES_NOT_EXTRACTED,
}

NEGATION_MARKERS = (
    "no ",
    "not ",
    "never",
    "cannot",
    "false",
    "zero",
    "without",
    "remains",
    "would",
    "until",
)


@pytest.fixture(scope="module")
def manifest():
    return rfv3.load_v3_holdout_manifest(MANIFEST_PATH)


@pytest.fixture(scope="module")
def blind():
    return json.loads(BLIND_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def execution():
    return json.loads(EXECUTION_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def inventory():
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def source_report():
    return json.loads(SOURCE_REPORT_PATH.read_text(encoding="utf-8"))


# --- Manifest transition and hash chain ---------------------------------------------


def test_manifest_advanced_exactly_one_step_to_corpus_built(manifest, blind):
    assert manifest["status"] == rfb.STATUS_CORPUS_BUILT
    assert blind["prior_manifest_status"] == rfb.STATUS_SOURCE_VERIFIED
    assert blind["new_manifest_status"] == rfb.STATUS_CORPUS_BUILT
    rfv3.validate_v3_holdout_status_transition(
        blind["prior_manifest_status"], blind["new_manifest_status"]
    )
    # And nothing beyond it is claimed.
    assert manifest["status"] != rfb.STATUS_HUMAN_ANNOTATION_COMPLETE


def test_manifest_hash_chain_is_intact_from_the_source_verification_freeze(
    blind, source_report
):
    assert blind["prior_manifest_sha256"] == source_report["new_manifest_sha256"]
    assert blind["source_verified_manifest_sha256"] == (
        source_report["new_manifest_sha256"]
    )
    assert blind["new_manifest_sha256"] == rfb.sha256_file(MANIFEST_PATH)
    assert blind["prior_manifest_sha256"] != blind["new_manifest_sha256"]
    assert blind["source_verification_report_version"] == (
        source_report["report_version"]
    )
    assert blind["source_acquisition_protocol_hash"] == (
        source_report["source_acquisition_protocol_hash"]
    )
    rfx.verify_manifest_hash_chain(MANIFEST_PATH, manifest_status(blind))


def manifest_status(blind: dict) -> str:
    return blind["new_manifest_status"]


def test_frozen_pair_identities_survived_the_transition(manifest, source_report):
    reported = {
        (item["pair_id"], item["side"]): item for item in source_report["filings"]
    }
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            item = reported[(pair["pair_id"], side)]
            assert pair[side]["expected_sha256"] == item["sha256"]
            assert pair[side]["source_verified"] is True
            assert pair[side]["accession_number"] == item["accession_number"]
            assert pair[side]["primary_document"] == item["primary_document"]
            assert pair["cik"] == item["cik"]
    assert [pair["pair_id"] for pair in manifest["pairs"]] == (
        source_report["pair_ids"]
    )


def test_manifest_prose_advanced_with_the_facts(manifest):
    detail = manifest["corpus_role_detail"]
    assert detail == rfx.corpus_built_corpus_role_detail()
    assert manifest["description"] == rfx.CORPUS_BUILT_DESCRIPTION
    assert "run over them exactly once, blind and unchanged" in detail
    assert "is not extraction" in detail or "not extraction" in detail
    assert "no generalization claim is supported" in detail


def test_frozen_bindings_survived_the_transition(manifest, blind):
    for field in (
        "frozen_extraction_parser_version",
        "frozen_parser_source_path",
        "frozen_parser_source_sha256",
        "frozen_unit_grammar_version",
        "frozen_detector_version",
        "frozen_detector_source_sha256",
        "frozen_workflow_version",
        "frozen_workflow_source_sha256",
        "frozen_evaluation_contract_version",
        "frozen_evaluator_source_sha256",
        "selection_protocol_version",
        "selection_protocol_hash",
    ):
        assert blind[field] == manifest[field], field
    rfv3.verify_frozen_code_identities(manifest, REPO_ROOT)
    rfv3.verify_exclusion_provenance(manifest, REPO_ROOT)


def test_live_pipeline_still_matches_the_versions_the_run_recorded(blind, execution):
    assert blind["frozen_detector_version"] == comparison_detector.DETECTOR_VERSION
    assert blind["frozen_workflow_version"] == comparison_store.WORKFLOW_VERSION
    assert blind["frozen_unit_grammar_version"] == (
        comparison_detector.DEFAULT_UNIT_GRAMMAR
    )
    assert execution["detector_version"] == comparison_detector.DETECTOR_VERSION
    assert execution["workflow_version"] == comparison_store.WORKFLOW_VERSION
    assert execution["unit_grammar_version"] == comparison_detector.UNIT_GRAMMAR_V3


def test_run_protocol_is_recorded_and_reproducible(blind):
    assert blind["blind_run_protocol_version"] == (
        rfx.V3_BLIND_RUN_PROTOCOL_VERSION
    )
    assert blind["blind_run_protocol_hash"] == rfx.blind_run_protocol_hash()
    assert blind["blind_run_protocol"] == rfx.blind_run_protocol()
    assert blind["runner_version"] == rfx.V3_BLIND_RUNNER_VERSION
    assert blind["run_id"].startswith("v3blind-")
    assert rfb._SHA256_RE.match(blind["run_hash"])
    assert blind["reproducible_payload_hash"] == rfx.reproducible_payload_hash(blind)
    assert blind["execution_order"] == rfx.EXECUTION_ORDER


def test_every_committed_report_shares_the_run_identity(blind, execution, inventory):
    for document in (execution, inventory):
        assert document["run_id"] == blind["run_id"]
        assert document["run_hash"] == blind["run_hash"]
        assert document["blind_run_protocol_hash"] == blind["blind_run_protocol_hash"]


# --- Frozen code attestation -----------------------------------------------------------


def test_blind_run_attests_frozen_code_was_byte_identical(blind):
    assert blind["frozen_code_unchanged"] is True
    assert blind["frozen_code_hashes_before"] == blind["frozen_code_hashes_after"]
    assert set(blind["frozen_code_hashes_before"]) == set(rfx.FROZEN_CODE_FILES)
    assert blind["semantic_code_modified_during_run"] is False


def test_frozen_code_is_still_byte_identical_to_the_attestation(blind):
    """The semantic components were not touched AFTER the run either.

    This is the whole holdout claim: a change made in response to these results
    would convert the corpus into development data, and this comparison is what
    makes that detectable.
    """
    assert rfx.frozen_code_hashes(REPO_ROOT) == blind["frozen_code_hashes_after"]


# --- The blind result, exactly as observed ------------------------------------------------


def test_all_twenty_sides_have_bounded_rows_in_manifest_order(manifest, blind):
    expected = [
        (pair["pair_id"], side)
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    ]
    assert [(row["pair_id"], row["side"]) for row in blind["sides"]] == expected
    assert blind["side_count"] == 20
    assert blind["pair_count"] == 10
    assert [row["pair_id"] for row in blind["pairs"]] == [
        pair["pair_id"] for pair in manifest["pairs"]
    ]


def test_every_side_row_binds_its_frozen_source_digest(manifest, blind):
    digests = {
        (pair["pair_id"], side): pair[side]["expected_sha256"]
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    }
    for row in blind["sides"]:
        key = (row["pair_id"], row["side"])
        # A parse_failed side records no digest; every side that the pipeline
        # actually read binds the exact frozen one.
        if row["source_sha256"] is not None:
            assert row["source_sha256"] == digests[key], key


def test_the_blind_result_is_recorded_and_not_smoothed(blind):
    assert blind["extraction_status_counts"] == EXPECTED_EXTRACTION_COUNTS
    assert sum(blind["extraction_status_counts"].values()) == 20
    assert blind["buildable_pair_count"] == 8
    assert blind["blocked_pair_count"] == 2
    assert blind["buildable_pair_count"] + blind["blocked_pair_count"] == 10
    blocked = {
        row["pair_id"]: row["blocked_reason"]
        for row in blind["pairs"]
        if not row["buildable"]
    }
    assert blocked == EXPECTED_BLOCKED_PAIRS


def test_blocked_pairs_stay_in_the_corpus_and_are_never_replaced(manifest, blind):
    assert blind["pairs_replaced"] == 0
    for pair_id in EXPECTED_BLOCKED_PAIRS:
        assert any(pair["pair_id"] == pair_id for pair in manifest["pairs"])
        row = next(row for row in blind["pairs"] if row["pair_id"] == pair_id)
        # No fabricated empty success result and no packet for a blocked pair.
        assert row["comparison_executed"] is False
        assert row["comparison_result_hash"] is None
        assert row["change_count"] == 0
        assert row["packet_sha256"] is None
        assert row["machine_proposed_label_count"] == 0


def test_extracted_rows_carry_section_hashes_and_bounded_diagnostics(blind):
    for row in blind["sides"]:
        if row["extraction_status"] != rfb.EXTRACTION_EXTRACTED:
            assert row["section_sha256"] is None
            assert row["unit_count"] == 0
            continue
        assert rfb._SHA256_RE.match(row["section_sha256"])
        assert row["section_character_count"] > 0
        assert row["section_chunk_count"] > 0
        assert row["unit_count"] >= 1
        assert row["canonical_unit_id_count"] == row["unit_count"]
        assert row["parser_version"] == "sec_html_item_headings.v2"
        if row["selected_heading"]:
            assert len(row["selected_heading"]) <= rfb.MAX_HEADING_CHARS
        if row["boundary_heading"]:
            assert len(row["boundary_heading"]) <= rfb.MAX_HEADING_CHARS


def test_repeated_normalized_headings_are_reported_not_collapsed(blind):
    """At least one real filing repeats a normalized heading, and the report
    says so per side rather than merging the occurrences away."""
    repeated = [
        row for row in blind["sides"] if row["repeated_unit_key_occurrences"] > 0
    ]
    assert repeated, "expected at least one side with a repeated normalized heading"
    for row in repeated:
        assert row["canonical_unit_id_count"] == row["unit_count"]
        # Distinct keys are strictly fewer than units precisely because the
        # occurrences were preserved as separate canonical identities.
        assert row["distinct_unit_key_count"] < row["unit_count"]


def test_single_unit_sections_are_recorded_as_observed(blind):
    """A section that yields one whole-section unit is a valid blind outcome
    and is preserved, not treated as a failure."""
    singles = [
        row
        for row in blind["sides"]
        if row["extraction_status"] == rfb.EXTRACTION_EXTRACTED
        and row["unit_count"] == 1
    ]
    assert singles
    for row in singles:
        assert row["section_sha256"]


def test_ambiguous_pair_kept_its_parser_diagnostics(blind):
    """The ambiguous pair is the interesting blind outcome: the frozen parser
    DID establish one substantive Item 1A heading, and the section key still
    landed on more than one non-contiguous chunk run. Recorded, not repaired."""
    rows = [row for row in blind["sides"] if row["pair_id"] == "sic-6000s-02"]
    assert len(rows) == 2
    for row in rows:
        assert row["extraction_status"] == rfb.EXTRACTION_AMBIGUOUS
        assert row["substantive_candidate_count"] == 1
        assert row["selected_heading"]
        assert row["section_chunk_count"] > 0
        assert row["unit_count"] == 0
        assert row["section_sha256"] is None


def test_missing_pair_is_recorded_without_inventing_a_reason(blind):
    rows = [row for row in blind["sides"] if row["pair_id"] == "sic-3000s-01"]
    assert len(rows) == 2
    for row in rows:
        assert row["extraction_status"] == rfb.EXTRACTION_MISSING
        assert row["section_chunk_count"] == 0
        assert row["indexed_chunk_count"] > 0
        assert row["unit_count"] == 0


def test_execution_report_covers_only_fully_extracted_pairs(blind, execution):
    assert execution["mode"] == "unlabeled_execution_report"
    assert len(execution["executions"]) == 10
    executed = [entry for entry in execution["executions"] if entry["executed"]]
    assert len(executed) == blind["comparison_result_count"] == 8
    for entry in execution["executions"]:
        if entry["executed"]:
            assert entry["execution_status"] == "detected"
            assert rfb._SHA256_RE.match(entry["result_hash"])
            assert entry["attempt_count"] >= 1
        else:
            assert entry["result_hash"] is None
            assert entry["blocked_reason"] in rfx.BLOCKED_PAIR_REASONS
        # The direct synchronous path creates no durable job.
        assert entry["retries"] == 0
        assert entry["reclaims"] == 0
        assert entry["detection_jobs"] == 0
    assert execution["steps"]["comparisons_executed"] == 8
    assert execution["steps"]["comparisons_blocked"] == 2


def test_result_hashes_are_recorded_once_per_built_pair(blind, execution):
    from_blind = {
        row["pair_id"]: row["comparison_result_hash"]
        for row in blind["pairs"]
        if row["comparison_result_hash"]
    }
    from_execution = {
        entry["pair_id"]: entry["result_hash"]
        for entry in execution["executions"]
        if entry["result_hash"]
    }
    assert from_blind == from_execution
    assert len(from_blind) == 8
    assert len(set(from_blind.values())) == 8


def test_packet_inventory_matches_the_detection_outcomes(blind, inventory):
    assert len(inventory["pairs"]) == 10
    assert inventory["packets_written"] == blind["packet_count"] == 8
    assert inventory["packets_blocked"] == 2
    assert inventory["machine_proposed_label_count"] == (
        blind["machine_proposed_label_count"]
    )
    written = {row["pair_id"] for row in inventory["pairs"] if row["packet_status"] == "written"}
    detected = {
        row["pair_id"] for row in blind["pairs"] if row["comparison_result_hash"]
    }
    assert written == detected
    for row in inventory["pairs"]:
        if row["packet_status"] == "written":
            assert rfb._SHA256_RE.match(row["packet_sha256"])
            assert row["packet_relative_path"].startswith("packets/")
            assert row["annotation_relative_path"].startswith("annotations/")
            assert row["label_count"] > 0
            assert row["labelled_unit_id_count"] > 0
            assert row["review_ready"] is True
        else:
            assert row["packet_sha256"] is None
            assert row["packet_relative_path"] is None
            assert row["label_count"] == 0
            assert row["review_ready"] is False
            assert row["blocking_reason"] in (
                rfx.PACKET_BLOCKED_NOT_EXTRACTED,
                rfx.PACKET_BLOCKED_NOT_DETECTED,
            )


def test_inventory_rows_bind_the_canonical_unit_identity_contract(blind, inventory):
    assert inventory["unit_identity_contract"] == "side:sequence:unit_key"
    assert blind["unit_identity_contract"] == "side:sequence:unit_key"
    by_pair = {row["pair_id"]: row for row in blind["pairs"]}
    sides = {(row["pair_id"], row["side"]): row for row in blind["sides"]}
    for row in inventory["pairs"]:
        assert row["previous_canonical_unit_id_count"] == (
            sides[(row["pair_id"], "previous")]["canonical_unit_id_count"]
        )
        assert row["current_canonical_unit_id_count"] == (
            sides[(row["pair_id"], "current")]["canonical_unit_id_count"]
        )
        assert row["comparison_result_hash"] == (
            by_pair[row["pair_id"]]["comparison_result_hash"]
        )


def test_reports_validate_under_their_own_exact_key_contracts(
    blind, execution, inventory
):
    rfx.validate_blind_extraction_report(blind)
    rfx.validate_execution_report(execution)
    rfx.validate_packet_inventory(inventory)


def test_aggregate_counts_reconcile_with_the_rows(blind, inventory):
    per_status: dict[str, int] = {}
    for row in blind["sides"]:
        per_status[row["extraction_status"]] = (
            per_status.get(row["extraction_status"], 0) + 1
        )
    for status, count in blind["extraction_status_counts"].items():
        assert per_status.get(status, 0) == count
    assert sum(blind["blocked_reason_counts"].values()) == blind["blocked_pair_count"]
    assert blind["extraction_runs"] == 20
    assert blind["comparison_runs"] == blind["comparison_result_count"]
    assert blind["source_checksum_reverifications"] == 40
    assert blind["machine_proposed_label_count"] == sum(
        row["label_count"] for row in inventory["pairs"]
    )


# --- Honesty boundaries -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    (BLIND_PATH, EXECUTION_PATH, INVENTORY_PATH, MANIFEST_PATH),
)
def test_committed_artifacts_state_their_denials_structurally(path):
    raw = path.read_text(encoding="utf-8")
    assert '"extraction_holdout_evaluation": false' in raw
    assert '"generalization_claim_supported": false' in raw
    assert '"extraction_holdout_evaluation": true' not in raw
    assert '"generalization_claim_supported": true' not in raw


def test_no_committed_artifact_carries_a_gold_metric(blind, execution, inventory):
    banned = {
        "precision",
        "recall",
        "f1",
        "accuracy",
        "exact_match",
        "exact_match_accuracy",
        "unchanged_fpr",
        "false_positive_rate",
        "change_type_accuracy",
    }

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in banned, f"{path}/{key}"
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    for document in (blind, execution, inventory):
        walk(document)
    assert blind["gold_metrics"] is None
    assert blind["gold_metrics_available"] is False
    assert execution["gold_metrics"] is None
    assert execution["gold_metrics_available"] is False


def test_zero_human_verified_labels_and_zero_evaluations(blind, execution, inventory):
    assert blind["human_verified_label_count"] == 0
    assert blind["gold_evaluation_runs"] == 0
    assert blind["signoff_present"] is False
    assert execution["human_verified_labels"] == 0
    assert execution["steps"]["gold_evaluation_runs"] == 0
    assert execution["steps"]["human_verified_labels"] == 0
    assert inventory["human_verified_label_count"] == 0
    assert inventory["gold_evaluation_runs"] == 0
    for row in inventory["pairs"]:
        assert row["annotation_status"] == rfb.ANNOTATION_MACHINE_PROPOSED
        assert row["human_verified"] is False
        assert row["annotator_id"] is None
        assert row["verification_timestamp"] is None


def test_zero_network_and_zero_source_acquisition(blind):
    assert blind["network_requests"] == 0
    assert blind["source_downloads"] == 0
    assert blind["blind_run_protocol"]["runs_network_requests"] is False
    assert blind["blind_run_protocol"]["acquires_sources"] is False
    assert blind["blind_run_protocol"]["runs_gold_evaluation"] is False
    assert blind["blind_run_protocol"]["creates_human_labels"] is False
    assert blind["blind_run_protocol"]["signs_off_generalization"] is False


def test_the_development_evidence_boundary_is_stated(blind):
    boundary = blind["development_evidence_boundary"]
    assert "OBSERVED" in boundary
    assert "development data" in boundary
    assert "separately frozen" in boundary


@pytest.mark.parametrize("path", (BLIND_PATH, EXECUTION_PATH, INVENTORY_PATH))
def test_generalization_language_appears_only_in_denials(path):
    raw = path.read_text(encoding="utf-8").lower()
    for sentence in re.split(r"(?<=[.;])\s+|\n", raw):
        if not any(root in sentence for root in ("generaliz", "out-of-sample")):
            continue
        assert any(marker in sentence for marker in NEGATION_MARKERS), (
            f"{path.name} asserts a generalization concept without denying it: "
            f"{sentence.strip()[:160]!r}"
        )


@pytest.mark.parametrize("path", (BLIND_PATH, EXECUTION_PATH, INVENTORY_PATH))
def test_reports_do_not_overclaim_the_blind_result(path):
    """Every quality concept may appear only inside a sentence that denies it.

    The reports deliberately SAY "a buildable-pair rate is not detector
    quality" and "MACHINE-PROPOSED — NOT GROUND TRUTH"; what they may never do
    is assert one.
    """
    lowered = path.read_text(encoding="utf-8").lower()
    sentences = re.split(r"(?<=[.;])\s+|\n", lowered)
    for phrase in (
        "extraction accuracy",
        "detector accuracy",
        "detector quality",
        "annotation accuracy",
        "validated the parser",
        "proves the parser",
        "confirms the detector",
        "representative performance",
        "stage 3.5 complete",
        "ground truth",
    ):
        for sentence in sentences:
            if phrase not in sentence:
                continue
            assert any(marker in sentence for marker in NEGATION_MARKERS), (
                f"{path.name} asserts {phrase!r} without denying it: "
                f"{sentence.strip()[:180]!r}"
            )


@pytest.mark.parametrize("path", (BLIND_PATH, EXECUTION_PATH, INVENTORY_PATH))
def test_reports_carry_no_filing_text_credentials_or_local_paths(path):
    raw = path.read_text(encoding="utf-8")
    lowered = raw.lower()
    assert "@" not in raw, path.name
    assert "sec_user_agent" not in lowered, path.name
    assert "/users/" not in lowered, path.name
    assert "/home/" not in lowered, path.name
    assert "c:\\" not in lowered, path.name
    assert str(REPO_ROOT).lower() not in lowered, path.name
    assert "<html" not in lowered, path.name
    assert "cookie" not in lowered, path.name
    assert "benchmark_data/real_filing_v3_holdout_v1" not in lowered, path.name
    assert "/archives/" not in lowered, path.name
    # Bounded headings are permitted; section prose is not.
    assert "risk factors should be read" not in lowered, path.name


def test_no_committed_artifact_carries_a_section_excerpt_or_unit_body(
    blind, execution, inventory
):
    for document in (blind, execution, inventory):
        raw = json.dumps(document)
        assert '"excerpt"' not in raw
        assert '"units"' not in raw
        assert '"alignments"' not in raw
        assert '"text"' not in raw


# --- Nothing else moved ---------------------------------------------------------------------


def test_selection_and_source_verification_reports_were_not_rewritten():
    """Both upstream reports are historical records of the steps they
    performed. A later step never edits them."""
    import subprocess

    for path in (SELECTION_PATH, SOURCE_REPORT_PATH, CONFIG_PATH):
        changed = subprocess.run(
            ["git", "diff", "--name-only", "origin/main", "--", str(path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if changed.returncode != 0:
            pytest.skip("origin/main is not available in this checkout")
        assert changed.stdout.strip() == "", path.name


def test_evaluation_config_still_declares_an_incomplete_evaluation():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["config_version"] == "real-filing-benchmark.evaluation.v2"
    assert config["metric_definitions_version"] == "real-filing-benchmark-metrics.v2"
    assert config["declared_detector_version"] == "item1a_detector.v3"
    assert config["declared_workflow_version"] == "comparison_workflow.v3"
    assert config["declared_unit_grammar_version"] == "item1a_units.v3"
    assert config["gold_status_required"] == "human_verified"
    assert config["pass_fail_thresholds"] is None
    assert config[evaluator.SIGNOFF_FIELD] is None


def test_prior_corpora_and_their_reports_are_unchanged():
    import subprocess

    for relative in (
        "benchmarks/real_filing_v1",
        "benchmarks/real_filing_holdout_v1",
        "eval/comparison_regression_baseline.json",
        "HOLDOUT_EVALUATION.md",
    ):
        changed = subprocess.run(
            ["git", "diff", "--name-only", "origin/main", "--", relative],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if changed.returncode != 0:
            pytest.skip("origin/main is not available in this checkout")
        assert changed.stdout.strip() == "", relative


def test_semantic_components_are_unchanged_since_the_freeze():
    import subprocess

    for relative in (
        rfv3.FROZEN_PARSER_SOURCE_PATH,
        rfv3.FROZEN_DETECTOR_SOURCE_PATH,
        rfv3.FROZEN_WORKFLOW_SOURCE_PATH,
        rfv3.FROZEN_EVALUATOR_SOURCE_PATH,
        "loaders/html.py",
        "scripts/create_real_filing_annotation_packets.py",
    ):
        changed = subprocess.run(
            ["git", "diff", "--name-only", "origin/main", "--", relative],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if changed.returncode != 0:
            pytest.skip("origin/main is not available in this checkout")
        assert changed.stdout.strip() == "", relative


def test_no_filing_body_or_local_artifact_is_tracked():
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "benchmark_data"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert tracked.stdout.strip() == ""
    committed = {path.name for path in V3_DIR.iterdir()}
    assert committed == {
        "manifest.json",
        "selection_report.json",
        "source_verification_report.json",
        "evaluation_config.json",
        "blind_extraction_report.json",
        "execution_report.json",
        "annotation_packet_inventory.json",
    }
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "benchmark_data/" in gitignore.splitlines()
    for name in ("sources", "build", "packets", "annotations"):
        assert not (V3_DIR / name).exists()


# --- CI ----------------------------------------------------------------------------------------


def test_required_check_runs_the_v3_blind_suites():
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    job = workflow["jobs"]["comparison-regression"]
    runs = "\n".join(
        step["run"]
        for step in job["steps"]
        if isinstance(step.get("run"), str) and "pytest" in step["run"]
    )
    for suite in BLIND_SUITES:
        assert suite in runs, suite
        assert (REPO_ROOT / suite).is_file(), suite


def test_required_check_identity_and_triggers_are_unchanged():
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert workflow["name"] == "comparison-regression"
    assert set(workflow["jobs"]) == {"comparison-regression"}
    assert workflow["jobs"]["comparison-regression"]["name"] == "comparison-regression"
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["pull_request"] in (None, {})


def test_required_check_never_runs_the_real_blind_run_or_reaches_sec():
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "run_real_filing_v3_holdout_blind_extraction",
        "acquire_real_filing_v3_holdout",
        "select_real_filing_v3_holdout",
        "eval_real_filing_benchmark",
        "--allow-network",
        "SEC_USER_AGENT",
        "secrets.",
        "aws-actions",
    ):
        assert forbidden not in raw, forbidden


def test_required_check_retains_the_regression_evaluator_and_artifact_upload():
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["comparison-regression"]["steps"]
    runs = "\n".join(step["run"] for step in steps if isinstance(step.get("run"), str))
    assert "scripts/eval_comparison_regression.py" in runs
    assert any(
        str(step.get("uses", "")).startswith("actions/upload-artifact")
        for step in steps
    )


def test_required_check_retains_every_previously_declared_suite():
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    for suite in (
        "tests/test_comparison_regression.py",
        "tests/test_comparison_detector.py",
        "tests/test_item1a_unit_parser_v3.py",
        "tests/test_chroma_batching.py",
        "tests/test_runtime_fault_injection.py",
        "tests/test_real_filing_benchmark_schema.py",
        "tests/test_real_filing_benchmark_tools.py",
        "tests/test_real_filing_benchmark_evaluator.py",
        "tests/test_sec_html_item_extraction.py",
        "tests/test_real_filing_holdout_blind_extraction.py",
        "tests/test_real_filing_holdout_build_report.py",
        "tests/test_holdout_human_annotation_validation.py",
        "tests/test_real_filing_holdout_gold_evaluation.py",
        "tests/test_gold_evaluation_signoff.py",
        "tests/test_v3_gold_evaluator_contract.py",
        "tests/test_v3_holdout_selection.py",
        "tests/test_v3_holdout_manifest.py",
        "tests/test_v3_holdout_source_acquisition.py",
        "tests/test_v3_holdout_source_verification.py",
    ):
        assert suite in raw, suite


# --- Documentation ------------------------------------------------------------------------------


def test_documentation_states_the_v3_blind_run_boundaries():
    for name in ("README.MD", "BENCHMARK.md"):
        raw = (REPO_ROOT / name).read_text(encoding="utf-8")
        lowered = raw.lower()
        assert "corpus_built" in lowered, name
        assert "machine-proposed" in lowered or "machine_proposed" in lowered, name


def test_documentation_makes_no_v3_accuracy_or_generalization_claim():
    for name in ("README.MD", "BENCHMARK.md"):
        raw = (REPO_ROOT / name).read_text(encoding="utf-8").lower()
        # Collapse markdown line wrapping first: a wrapped line is not a
        # sentence boundary, and splitting on it would strip the negation off
        # the front of a properly hedged sentence.
        lowered = re.sub(r"\s+", " ", raw)
        for sentence in re.split(r"(?<=[.;])\s+", lowered):
            # Scoped to sentences that actually invoke the v3 holdout: the
            # docs legitimately report the completed v2 holdout evaluation and
            # its real metrics.
            if not any(
                marker in sentence
                for marker in ("v3 holdout", "real_filing_v3_holdout_v1")
            ):
                continue
            if not any(
                root in sentence
                for root in (
                    "generaliz",
                    "out-of-sample",
                    "accuracy",
                    "precision",
                    "recall",
                    "represent",
                    "detector quality",
                )
            ):
                continue
            assert any(marker in sentence for marker in NEGATION_MARKERS), (
                f"{name} makes an unhedged v3 claim: {sentence.strip()[:180]!r}"
            )


def test_documentation_states_the_blind_result_without_overclaiming_it():
    benchmark = (REPO_ROOT / "BENCHMARK.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.MD").read_text(encoding="utf-8")
    for raw in (benchmark, readme):
        collapsed = re.sub(r"\s+", " ", raw)
        assert "16" in collapsed and "20" in collapsed
        assert "Coverage is not correctness" in collapsed
        assert "MACHINE-PROPOSED — NOT GROUND TRUTH" in collapsed
        assert "human_verified" in collapsed
    assert "corpus_built" in benchmark
    # Stage claims are unchanged by this commit.
    assert "Stage 3 remains current" in benchmark
    assert "Stage 3.5 remains in progress" in benchmark


def test_holdout_evaluation_doc_is_untouched():
    import subprocess

    changed = subprocess.run(
        ["git", "diff", "--name-only", "origin/main", "--", "HOLDOUT_EVALUATION.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if changed.returncode != 0:
        pytest.skip("origin/main is not available in this checkout")
    assert changed.stdout.strip() == ""
