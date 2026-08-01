"""Committed-artifact tests for the holdout blind-extraction result.

These tests pin the COMMITTED record of the first blind run: the manifest
advanced exactly one step to ``corpus_built``, the hash chain from the
source-verification freeze is intact, all twenty side rows are bounded and
preserved exactly as observed (18 extracted, 2 ambiguous), the frozen parser
bytes still match the freeze, no committed artifact carries filing text, a
local path, a human-verified label, or a gold metric — and the development
corpus, the synthetic regression suite, and the CI posture are untouched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import real_filing_benchmark as rfb
import real_filing_holdout as rfh
import real_filing_holdout_extraction as rfhe

REPO_ROOT = Path(__file__).resolve().parent.parent
HOLDOUT_DIR = REPO_ROOT / "benchmarks" / "real_filing_holdout_v1"
DEV_DIR = REPO_ROOT / "benchmarks" / "real_filing_v1"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"

COMMITTED_REPORTS = (
    "blind_extraction_report.json",
    "execution_report.json",
    "annotation_packet_inventory.json",
)

#: The exact bounded per-side row schema. A new key here is a reviewed schema
#: change, never an accident.
SIDE_ROW_KEYS = {
    "pair_id",
    "side",
    "source_sha256",
    "parser_version",
    "parser_source_sha256",
    "outcome",
    "reason_code",
    "candidate_count",
    "substantive_candidate_count",
    "selected_heading",
    "selected_tag",
    "rejected_navigation_count",
    "boundary_heading",
    "character_count",
    "chunk_count",
    "unit_count",
    "section_sha256",
    "duration_ms",
}

#: Phrasings that cannot be part of an honest statement about this corpus.
OVERCLAIM_PHRASES = (
    "unbiased",
    "representative sample",
    "production ready",
    "production-ready",
    "stage 3.5 complete",
    '"generalization_claim_supported": true',
    '"extraction_holdout_evaluation": true',
)

#: Sentences invoking generalization must be negated or forward-looking.
NEGATION_MARKERS = (
    "not", "never", "no ", "cannot", "false", "requires", "is required",
    "until", "would", "remains", "unseen", "unchanged",
)


def _report(name: str) -> dict:
    return json.loads((HOLDOUT_DIR / name).read_text(encoding="utf-8"))


def _manifest() -> dict:
    return rfh.load_holdout_manifest()


# --- Manifest transition and hash chain ------------------------------------------


def test_manifest_advanced_exactly_one_step_to_corpus_built():
    manifest = _manifest()
    assert manifest["status"] == rfb.STATUS_CORPUS_BUILT
    report = _report("blind_extraction_report.json")
    assert report["prior_manifest_status"] == rfb.STATUS_SOURCE_VERIFIED
    assert report["new_manifest_status"] == rfb.STATUS_CORPUS_BUILT
    rfh.validate_holdout_status_transition(
        report["prior_manifest_status"], report["new_manifest_status"]
    )


def test_manifest_hash_chain_is_intact_from_the_source_verification_freeze():
    source_verification = _report("source_verification_report.json")
    blind = _report("blind_extraction_report.json")
    assert (
        blind["prior_manifest_sha256"]
        == source_verification["new_manifest_sha256"]
    )
    committed_bytes = rfb.sha256_file(HOLDOUT_DIR / "manifest.json")
    assert blind["new_manifest_sha256"] == committed_bytes
    assert _report("execution_report.json")["manifest_sha256"] == committed_bytes
    assert (
        _report("annotation_packet_inventory.json")["manifest_sha256"]
        == committed_bytes
    )


def test_manifest_prose_advanced_with_the_facts():
    manifest = _manifest()
    assert manifest["corpus_role_detail"] == (
        rfhe.corpus_built_corpus_role_detail()
    )
    assert manifest["description"] == rfhe.CORPUS_BUILT_DESCRIPTION


def test_frozen_pair_identities_survived_the_transition():
    manifest = _manifest()
    assert len(manifest["pairs"]) == rfh.TARGET_PAIR_COUNT
    for pair in manifest["pairs"]:
        for side in ("previous", "current"):
            payload = pair[side]
            assert payload["source_verified"] is True
            assert re.fullmatch(r"[0-9a-f]{64}", payload["expected_sha256"])


# --- Frozen parser and frozen code -----------------------------------------------


def test_frozen_parser_hash_is_identical_across_manifest_report_and_disk():
    manifest = _manifest()
    report = _report("blind_extraction_report.json")
    live = rfh.frozen_parser_source_sha256()
    assert manifest["frozen_parser_source_sha256"] == live
    assert report["frozen_parser_source_sha256"] == live
    assert (
        report["frozen_extraction_parser_version"]
        == rfh.FROZEN_EXTRACTION_PARSER_VERSION
    )
    for row in report["sides"]:
        assert row["parser_source_sha256"] == live


def test_blind_run_attests_frozen_code_was_byte_identical():
    report = _report("blind_extraction_report.json")
    assert report["frozen_code_unchanged"] is True
    assert (
        report["frozen_code_hashes_before"]
        == report["frozen_code_hashes_after"]
    )
    assert sorted(report["frozen_code_hashes_before"]) == sorted(
        rfhe.FROZEN_CODE_FILES
    )
    # The parser entry of the frozen-code attestation is the same digest the
    # manifest froze — one identity, recorded three ways, never diverging.
    assert (
        report["frozen_code_hashes_before"][rfh.FROZEN_PARSER_SOURCE_PATH]
        == _manifest()["frozen_parser_source_sha256"]
    )


# --- The blind result, exactly as observed ---------------------------------------


def test_all_twenty_sides_have_bounded_rows_in_manifest_order():
    manifest = _manifest()
    report = _report("blind_extraction_report.json")
    rows = report["sides"]
    assert len(rows) == 20
    expected_order = [
        (pair["pair_id"], side)
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    ]
    assert [(row["pair_id"], row["side"]) for row in rows] == expected_order
    digests = {
        (pair["pair_id"], side): pair[side]["expected_sha256"]
        for pair in manifest["pairs"]
        for side in ("previous", "current")
    }
    for row in rows:
        assert set(row) == SIDE_ROW_KEYS
        assert row["outcome"] in rfb.EXTRACTION_OUTCOMES
        assert row["source_sha256"] == digests[(row["pair_id"], row["side"])]
        for field in ("selected_heading", "boundary_heading"):
            value = row[field]
            assert value is None or (
                isinstance(value, str)
                and 0 < len(value) <= rfb.MAX_HEADING_CHARS
                and "\n" not in value
            )


def test_the_blind_result_is_recorded_and_not_smoothed():
    """18 extracted, 2 ambiguous, 0 missing, 0 parse_failed — the first
    out-of-sample result, preserved exactly. Regenerating this report under
    a changed parser cannot silently improve it: the run refuses on parser
    drift, so these numbers change only with a NEW holdout."""
    report = _report("blind_extraction_report.json")
    assert report["extraction_totals"] == {
        "extracted": 18,
        "missing": 0,
        "ambiguous": 2,
        "parse_failed": 0,
    }
    assert report["pairs_built"] == 10
    assert report["pairs_fully_extracted"] == 9
    assert report["comparison_runs"] == 9
    assert report["blind_run"]["sides_attempted"] == 20
    assert report["blind_run"]["pairs_replaced"] == 0
    assert report["blind_run"]["parser_modified_during_run"] is False
    ambiguous = [
        row for row in report["sides"] if row["outcome"] == "ambiguous"
    ]
    assert {row["pair_id"] for row in ambiguous} == {"sic-6000s-01"}
    for row in ambiguous:
        assert row["section_sha256"] is None
        assert row["unit_count"] == 0


def test_extracted_rows_carry_section_hashes_and_extraction_diagnostics():
    report = _report("blind_extraction_report.json")
    for row in report["sides"]:
        if row["outcome"] != "extracted":
            continue
        assert re.fullmatch(r"[0-9a-f]{64}", row["section_sha256"])
        assert row["parser_version"] == rfh.FROZEN_EXTRACTION_PARSER_VERSION
        assert row["character_count"] > 0
        assert row["chunk_count"] > 0
        assert row["unit_count"] > 0
        assert isinstance(row["reason_code"], str) and row["reason_code"]


def test_execution_report_covers_only_fully_extracted_pairs():
    execution = _report("execution_report.json")
    assert execution["comparisons_executed"] == 9
    quality = execution["corpus_quality"]
    assert quality["pairs_requested"] == 10
    assert quality["pairs_built"] == 10
    assert quality["pairs_extracted"] == 9
    assert quality["pairs_ambiguous_section"] == 1
    assert quality["pairs_missing_section"] == 0
    assert quality["pairs_parse_failed"] == 0
    assert quality["pairs_human_verified"] == 0
    assert quality["filing_extraction_outcomes"] == {
        "extracted": 18,
        "missing": 0,
        "ambiguous": 2,
        "parse_failed": 0,
    }
    for entry in execution["executions"]:
        if entry["pair_id"] == "sic-6000s-01":
            assert entry["buildable"] is False
            assert entry["executed"] is False
            assert entry["result_hash"] is None
            assert entry["attempt_count"] == 0
        else:
            assert entry["executed"] is True
            assert entry["execution_status"] == "detected"
            assert re.fullmatch(r"[0-9a-f]{64}", entry["result_hash"])
        assert entry["retries"] == 0
        assert entry["reclaims"] == 0
        assert entry["detection_jobs"] == 0


def test_packet_inventory_matches_the_detection_outcomes():
    inventory = _report("annotation_packet_inventory.json")
    assert inventory["packets_written"] == 9
    assert inventory["packets_blocked"] == 1
    assert len(inventory["pairs"]) == 10
    for row in inventory["pairs"]:
        assert row["annotation_status"] == rfb.ANNOTATION_MACHINE_PROPOSED
        assert row["human_verified"] is False
        if row["pair_id"] == "sic-6000s-01":
            assert row["packet_status"] == "blocked"
            assert row["blocking_reason"] == (
                "item_1a_not_extracted_for_both_sides"
            )
            assert row["packet_hash"] is None
            assert row["review_ready"] is False
        else:
            assert row["packet_status"] == "written"
            assert re.fullmatch(r"[0-9a-f]{64}", row["packet_hash"])
            assert row["review_ready"] is True
            assert row["label_count"] > 0


# --- Honesty boundaries ------------------------------------------------------------


@pytest.mark.parametrize("name", COMMITTED_REPORTS)
def test_reports_identify_the_holdout_without_claiming_its_evaluation(name):
    report = _report(name)
    assert report["corpus_role"] == rfb.CORPUS_ROLE_EXTRACTION_HOLDOUT
    assert report["extraction_parser_developed_using_this_corpus"] is False
    assert report["extraction_holdout_evaluation"] is False
    assert report["generalization_claim_supported"] is False
    assert report["human_verified_labels"] == 0


@pytest.mark.parametrize("name", COMMITTED_REPORTS)
def test_reports_do_not_overclaim_the_blind_result(name):
    raw = (HOLDOUT_DIR / name).read_text(encoding="utf-8").lower()
    for phrase in OVERCLAIM_PHRASES:
        assert phrase not in raw, f"{name} overclaims with {phrase!r}"
    for sentence in re.split(r"(?<=[.;])\s+|\n", raw):
        if "generaliz" not in sentence and "generalis" not in sentence:
            continue
        assert any(marker in sentence for marker in NEGATION_MARKERS), (
            f"{name} asserts generalization without denying it: "
            f"{sentence.strip()[:160]!r}"
        )
    assert '"generalization_claim_supported": false' in raw


@pytest.mark.parametrize("name", COMMITTED_REPORTS)
def test_reports_carry_no_gold_metric(name):
    raw = (HOLDOUT_DIR / name).read_text(encoding="utf-8").lower()
    for banned in (
        '"precision"',
        '"recall"',
        '"f1"',
        '"exact_match"',
        '"change_precision"',
        '"change_recall"',
    ):
        assert banned not in raw, (name, banned)
    report = _report(name)
    if "gold_metrics_available" in report:
        assert report["gold_metrics_available"] is False
        assert report["gold_metrics"] is None


@pytest.mark.parametrize("name", COMMITTED_REPORTS)
def test_reports_carry_no_filing_text_or_local_path(name):
    raw = (HOLDOUT_DIR / name).read_text(encoding="utf-8")
    assert "/Users/" not in raw
    assert "\\Users\\" not in raw
    assert "/home/" not in raw
    assert "excerpt" not in raw.lower()
    # Bounded structure: no string field longer than the report's own caps
    # (headings, codes, hashes, prose notes) — a filing section would be
    # thousands of characters.
    def _walk(value):
        if isinstance(value, dict):
            for item in value.values():
                _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)
        elif isinstance(value, str):
            assert len(value) <= 2000
    _walk(_report(name))


def test_machine_proposed_annotations_cannot_claim_the_development_corpus():
    """The annotation writer threads the holdout's own benchmark id; the
    hard-coded development default cannot leak into holdout artifacts."""
    document = rfb.machine_proposed_annotation(
        pair_id="sic-2000s-01",
        source_manifest_hash="0" * 64,
        previous_section_hash="1" * 64,
        current_section_hash="2" * 64,
        labels=[],
        generated_by="test",
        benchmark_id=rfh.HOLDOUT_BENCHMARK_ID,
    )
    assert document["benchmark_id"] == rfh.HOLDOUT_BENCHMARK_ID
    assert document["annotation_status"] == rfb.ANNOTATION_MACHINE_PROPOSED


# --- Nothing else moved ------------------------------------------------------------


def test_development_corpus_and_its_reports_are_unchanged():
    dev_manifest_hash = rfb.manifest_hash()
    for name in (
        "corpus_build_report.v2.json",
        "execution_report.v2.json",
        "annotation_packet_inventory.v2.json",
    ):
        report = json.loads((DEV_DIR / name).read_text(encoding="utf-8"))
        assert report["manifest_hash"] == dev_manifest_hash
        assert report["corpus_role"] == "extraction_development_corpus"
    build = json.loads(
        (DEV_DIR / "corpus_build_report.json").read_text(encoding="utf-8")
    )
    assert build["filing_extraction_outcomes"] == {"missing": 20}


def test_synthetic_regression_suite_is_untouched():
    from scripts import eval_comparison_regression as ecr

    assert len(ecr.GATES) == 10
    gates = json.dumps(sorted(ecr.GATES))
    assert "holdout" not in gates
    assert "real_filing" not in gates


def test_ci_runs_the_new_suites_offline_with_no_sec_access():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "tests/test_real_filing_holdout_blind_extraction.py" in text
    assert "tests/test_real_filing_holdout_build_report.py" in text
    assert "SEC_USER_AGENT" not in text
    assert "secrets." not in text
    assert "AWS_ACCESS_KEY" not in text
    assert "name: comparison-regression" in text
    assert "acquire_real_filing_holdout" not in text
    assert "run_real_filing_holdout_blind_extraction" not in text
