"""Offline behavior tests for the holdout blind-extraction pipeline.

Every test runs over SYNTHETIC fixtures in temporary directories: the frozen
holdout identities are reused (they are public metadata), but every body is a
hand-written fictional HTML document, so no real filing content is needed and
no network can be reached. The suite pins what the fourteenth Stage 3.5 commit
claims: frozen identities cannot drift, all twenty sides are attempted exactly
once under byte-identical frozen code, every outcome is preserved exactly as
observed, only fully extracted pairs reach the comparison workflow, packets
exist only for detected pairs and stay machine-proposed, and no path produces
a human-verified label or a gold metric.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest

import real_filing_benchmark as rfb
import real_filing_holdout as rfh
import real_filing_holdout_extraction as rfhe
from scripts import build_real_filing_benchmark as builder
from scripts import run_real_filing_holdout_blind_extraction as cli
from tests.helpers import real_filing_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parent.parent

COMMITTED_MANIFEST = rfh.load_holdout_manifest()

#: Pair indexes given deliberately imperfect fixtures, so outcome preservation
#: is exercised by the same full run that exercises the happy path.
MISSING_CURRENT_INDEX = 1
AMBIGUOUS_PREVIOUS_INDEX = 2


# --- Fixtures -------------------------------------------------------------------


def synthetic_manifest() -> tuple[dict, dict[tuple[str, str], str]]:
    """The committed manifest re-anchored to synthetic fictional bodies."""
    document = copy.deepcopy(COMMITTED_MANIFEST)
    document["status"] = rfb.STATUS_SOURCE_VERIFIED
    contents: dict[tuple[str, str], str] = {}
    for index, pair in enumerate(document["pairs"]):
        for side in ("previous", "current"):
            if index == MISSING_CURRENT_INDEX and side == "current":
                html = fx.NO_SECTION_HTML
            elif index == AMBIGUOUS_PREVIOUS_INDEX and side == "previous":
                html = fx.AMBIGUOUS_SECTION_HTML
            elif side == "previous":
                html = fx.SEC_STYLED_PREVIOUS_HTML
            else:
                html = fx.SEC_STYLED_CURRENT_HTML
            pair[side]["expected_sha256"] = hashlib.sha256(
                html.encode("utf-8")
            ).hexdigest()
            pair[side]["source_verified"] = True
            contents[(pair["pair_id"], side)] = html
    rfh.validate_holdout_manifest(document)
    return document, contents


def seed_corpus(root: Path, document: dict, contents: dict) -> rfb.CorpusLayout:
    layout = rfb.CorpusLayout(root)
    for pair in document["pairs"]:
        for side in ("previous", "current"):
            target = layout.source_file(
                pair["pair_id"], side, pair[side]["primary_document"]
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents[(pair["pair_id"], side)], encoding="utf-8")
    return layout


def write_manifest(path: Path, document: dict) -> None:
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
    )


@pytest.fixture(scope="module")
def full_run(tmp_path_factory):
    """One full pipeline run over the synthetic corpus, shared by the
    run-level assertions. ``builder._extract_side`` is instrumented so the
    suite can prove every side was attempted exactly once."""
    root = tmp_path_factory.mktemp("holdout_blind")
    document, contents = synthetic_manifest()
    manifest_path = root / "manifest.json"
    write_manifest(manifest_path, document)
    layout = seed_corpus(root / "corpus", document, contents)
    source_digests_before = {
        (pair["pair_id"], side): pair[side]["expected_sha256"]
        for pair in document["pairs"]
        for side in ("previous", "current")
    }

    extract_calls: list[tuple[str, str]] = []
    original_extract = builder._extract_side  # noqa: SLF001

    def counting_extract(side, source_name, ingestion, registry_entry):
        pair_id = source_name.split(f"-{side}-")[0]
        extract_calls.append((pair_id, side))
        return original_extract(side, source_name, ingestion, registry_entry)

    builder._extract_side = counting_extract  # noqa: SLF001
    try:
        code = cli.main(
            [
                "--manifest", str(manifest_path),
                "--corpus-dir", str(root / "corpus"),
                "--report-dir", str(root / "reports"),
            ]
        )
    finally:
        builder._extract_side = original_extract  # noqa: SLF001

    reports = {
        name: json.loads((root / "reports" / name).read_text(encoding="utf-8"))
        for name in (
            "blind_extraction_report.json",
            "execution_report.json",
            "annotation_packet_inventory.json",
        )
    }
    return {
        "exit_code": code,
        "original_manifest": document,
        "manifest_path": manifest_path,
        "advanced_manifest": json.loads(
            manifest_path.read_text(encoding="utf-8")
        ),
        "layout": layout,
        "contents": contents,
        "source_digests_before": source_digests_before,
        "extract_calls": extract_calls,
        "blind": reports["blind_extraction_report.json"],
        "execution": reports["execution_report.json"],
        "inventory": reports["annotation_packet_inventory.json"],
    }


def _pair_id(document: dict, index: int) -> str:
    return document["pairs"][index]["pair_id"]


# --- Preconditions: drift is refused before the parser reads a byte -------------


def test_source_checksum_drift_is_refused(tmp_path):
    document, contents = synthetic_manifest()
    layout = seed_corpus(tmp_path / "corpus", document, contents)
    pair = document["pairs"][0]
    target = layout.source_file(
        pair["pair_id"], "previous", pair["previous"]["primary_document"]
    )
    target.write_text("<html>tampered fictional body</html>", encoding="utf-8")
    with pytest.raises(rfhe.HoldoutExtractionError) as excinfo:
        rfhe.verify_holdout_sources(document, layout)
    assert excinfo.value.code == rfhe.FAILURE_SOURCE_CHECKSUM_DRIFT
    # The drifted file is preserved as evidence, never replaced.
    assert target.read_text(encoding="utf-8") == (
        "<html>tampered fictional body</html>"
    )


def test_missing_source_is_refused_not_downloaded(tmp_path):
    document, contents = synthetic_manifest()
    layout = seed_corpus(tmp_path / "corpus", document, contents)
    pair = document["pairs"][3]
    layout.source_file(
        pair["pair_id"], "current", pair["current"]["primary_document"]
    ).unlink()
    with pytest.raises(rfhe.HoldoutExtractionError) as excinfo:
        rfhe.verify_holdout_sources(document, layout)
    assert excinfo.value.code == rfhe.FAILURE_SOURCE_MISSING


def test_parser_source_drift_is_refused(tmp_path):
    document, _contents = synthetic_manifest()
    document["frozen_parser_source_sha256"] = "a" * 64
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, document)
    with pytest.raises(rfhe.HoldoutExtractionError) as excinfo:
        rfhe.verify_blind_run_preconditions(document, manifest_path)
    assert excinfo.value.code == rfhe.FAILURE_PARSER_SOURCE_DRIFT


def test_exclusion_drift_is_refused(tmp_path):
    document, _contents = synthetic_manifest()
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, document)
    development = rfb.load_manifest()
    shrunk = copy.deepcopy(development)
    shrunk["proposed_issuers"][0]["cik"] = "0009999999"
    with pytest.raises(rfhe.HoldoutExtractionError) as excinfo:
        rfhe.verify_blind_run_preconditions(
            document, manifest_path, development_manifest=shrunk
        )
    assert excinfo.value.code == rfhe.FAILURE_EXCLUSION_DRIFT


def test_metadata_only_manifest_cannot_run_extraction(tmp_path):
    document, _contents = synthetic_manifest()
    document["status"] = rfh.STATUS_HOLDOUT_FROZEN_METADATA_ONLY
    for pair in document["pairs"]:
        for side in ("previous", "current"):
            pair[side]["expected_sha256"] = None
            pair[side]["source_verified"] = False
    document["corpus_role_detail"] = rfh.holdout_corpus_role_fields()[
        "corpus_role_detail"
    ]
    rfh.validate_holdout_manifest(document)
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, document)
    with pytest.raises(rfhe.HoldoutExtractionError) as excinfo:
        rfhe.verify_blind_run_preconditions(document, manifest_path)
    assert excinfo.value.code == rfhe.FAILURE_STATUS_NOT_EXTRACTABLE


def test_hand_edited_manifest_is_rejected_by_the_hash_chain(tmp_path):
    document, _contents = synthetic_manifest()
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, document)
    rfb.write_json_atomic(
        tmp_path / "source_verification_report.json",
        {"new_manifest_sha256": rfb.sha256_file(manifest_path)},
    )
    rfhe.verify_blind_run_preconditions(document, manifest_path)  # intact: passes
    edited = dict(document)
    edited["description"] = "SYNTHETIC edited freeze"
    write_manifest(manifest_path, edited)
    with pytest.raises(rfhe.HoldoutExtractionError) as excinfo:
        rfhe.verify_blind_run_preconditions(edited, manifest_path)
    assert excinfo.value.code == rfhe.FAILURE_MANIFEST_HASH_DRIFT


def test_committed_tree_passes_the_blind_run_preconditions():
    rfhe.verify_blind_run_preconditions(
        COMMITTED_MANIFEST, rfh.default_holdout_manifest_path()
    )


# --- Frozen code -----------------------------------------------------------------


def test_frozen_code_list_is_closed_and_covers_the_run():
    assert "loaders/sec_headings.py" in rfhe.FROZEN_CODE_FILES
    assert "loaders/html.py" in rfhe.FROZEN_CODE_FILES
    assert "comparison_detector.py" in rfhe.FROZEN_CODE_FILES
    assert "comparison_validators.py" in rfhe.FROZEN_CODE_FILES
    assert "comparison_governance.py" in rfhe.FROZEN_CODE_FILES
    assert "policies/comparison_risk_policy.yaml" in rfhe.FROZEN_CODE_FILES
    hashes = rfhe.frozen_code_hashes()
    assert sorted(hashes) == sorted(rfhe.FROZEN_CODE_FILES)
    assert all(len(value) == 64 for value in hashes.values())


def test_frozen_parser_bytes_still_match_the_committed_freeze():
    assert (
        rfh.frozen_parser_source_sha256()
        == COMMITTED_MANIFEST["frozen_parser_source_sha256"]
    )


def test_frozen_code_change_during_the_run_fails_the_report():
    before = {name: "0" * 64 for name in rfhe.FROZEN_CODE_FILES}
    after = dict(before)
    after["comparison_detector.py"] = "1" * 64
    with pytest.raises(rfhe.HoldoutExtractionError) as excinfo:
        rfhe.require_frozen_code_unchanged(before, after)
    assert excinfo.value.code == rfhe.FAILURE_FROZEN_CODE_CHANGED
    rfhe.require_frozen_code_unchanged(before, dict(before))


def test_full_run_attests_byte_identical_frozen_code(full_run):
    report = full_run["blind"]
    assert report["frozen_code_unchanged"] is True
    assert (
        report["frozen_code_hashes_before"] == report["frozen_code_hashes_after"]
    )
    assert sorted(report["frozen_code_hashes_before"]) == sorted(
        rfhe.FROZEN_CODE_FILES
    )


# --- No network -------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_path",
    [
        "real_filing_holdout_extraction.py",
        "scripts/run_real_filing_holdout_blind_extraction.py",
    ],
)
def test_blind_extraction_imports_nothing_that_can_reach_a_network(module_path):
    source = (REPO_ROOT / module_path).read_text(encoding="utf-8")
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
        "real_filing_acquisition",
        "real_filing_holdout_acquisition",
    ):
        assert forbidden not in imported, forbidden


# --- The run itself ----------------------------------------------------------------


def test_full_run_completes_and_advances_exactly_one_step(full_run):
    assert full_run["exit_code"] == 0
    assert full_run["original_manifest"]["status"] == rfb.STATUS_SOURCE_VERIFIED
    assert full_run["advanced_manifest"]["status"] == rfb.STATUS_CORPUS_BUILT
    report = full_run["blind"]
    assert report["prior_manifest_status"] == rfb.STATUS_SOURCE_VERIFIED
    assert report["new_manifest_status"] == rfb.STATUS_CORPUS_BUILT
    rfh.validate_holdout_status_transition(
        report["prior_manifest_status"], report["new_manifest_status"]
    )
    assert report["new_manifest_sha256"] == rfb.sha256_file(
        full_run["manifest_path"]
    )


def test_all_twenty_sides_are_attempted_exactly_once(full_run):
    calls = full_run["extract_calls"]
    assert len(calls) == 20
    assert len(set(calls)) == 20
    expected = {
        (pair["pair_id"], side)
        for pair in full_run["original_manifest"]["pairs"]
        for side in ("previous", "current")
    }
    assert set(calls) == expected
    assert full_run["blind"]["blind_run"]["sides_attempted"] == 20


def test_side_rows_are_deterministically_ordered(full_run):
    rows = full_run["blind"]["sides"]
    expected = [
        (pair["pair_id"], side)
        for pair in full_run["original_manifest"]["pairs"]
        for side in ("previous", "current")
    ]
    assert [(row["pair_id"], row["side"]) for row in rows] == expected


def test_missing_outcome_is_preserved(full_run):
    pair_id = _pair_id(full_run["original_manifest"], MISSING_CURRENT_INDEX)
    row = next(
        row
        for row in full_run["blind"]["sides"]
        if row["pair_id"] == pair_id and row["side"] == "current"
    )
    assert row["outcome"] == rfb.EXTRACTION_MISSING
    assert row["section_sha256"] is None
    assert full_run["blind"]["extraction_totals"]["missing"] == 1


def test_ambiguous_outcome_is_preserved(full_run):
    pair_id = _pair_id(full_run["original_manifest"], AMBIGUOUS_PREVIOUS_INDEX)
    row = next(
        row
        for row in full_run["blind"]["sides"]
        if row["pair_id"] == pair_id and row["side"] == "previous"
    )
    assert row["outcome"] == rfb.EXTRACTION_AMBIGUOUS
    assert row["section_sha256"] is None
    assert full_run["blind"]["extraction_totals"]["ambiguous"] == 1


def test_parse_failure_is_recorded_never_repaired(tmp_path, monkeypatch):
    document, contents = synthetic_manifest()
    layout = seed_corpus(tmp_path / "corpus", document, contents)

    def exploding_extract(side, source_name, ingestion, registry_entry):
        raise RuntimeError("synthetic parser explosion")

    monkeypatch.setattr(builder, "_extract_side", exploding_extract)
    outcome = rfhe.blind_extract_pair(
        document["pairs"][0],
        manifest=document,
        manifest_sha256="0" * 64,
        layout=layout,
    )
    record = outcome["record"]
    for side in ("previous", "current"):
        assert (
            record[side]["extraction_outcome"] == rfb.EXTRACTION_PARSE_FAILED
        )
        assert record[side]["extraction_reason"].startswith(
            rfhe.REASON_PAIR_BUILD_FAILED
        )
        # A bounded class name, never exception text.
        assert "synthetic parser explosion" not in json.dumps(record)
    assert record["execution"]["executed"] is False


def test_no_issuer_or_pair_was_replaced(full_run):
    identity_fields = (
        "pair_id",
        "issuer_name",
        "cik",
        "sic",
        "stratum_id",
        "previous",
        "current",
    )
    for before, after in zip(
        full_run["original_manifest"]["pairs"],
        full_run["advanced_manifest"]["pairs"],
    ):
        for field in identity_fields:
            assert before[field] == after[field]


def test_no_source_was_replaced_or_rewritten(full_run):
    layout = full_run["layout"]
    for pair in full_run["original_manifest"]["pairs"]:
        for side in ("previous", "current"):
            target = layout.source_file(
                pair["pair_id"], side, pair[side]["primary_document"]
            )
            assert (
                rfb.sha256_file(target)
                == full_run["source_digests_before"][(pair["pair_id"], side)]
            )


def test_extraction_is_deterministic_for_identical_inputs(tmp_path):
    # Two independent corpus directories seeded with byte-identical inputs:
    # determinism is a property of the inputs, not of a shared workspace (and
    # chromadb caches per-path clients within a process, so reusing one
    # workspace would test the cache, not the pipeline).
    document, contents = synthetic_manifest()
    first = rfhe.blind_extract_pair(
        document["pairs"][0],
        manifest=document,
        manifest_sha256="0" * 64,
        layout=seed_corpus(tmp_path / "corpus_a", document, contents),
    )["record"]
    second = rfhe.blind_extract_pair(
        document["pairs"][0],
        manifest=document,
        manifest_sha256="0" * 64,
        layout=seed_corpus(tmp_path / "corpus_b", document, contents),
    )["record"]
    assert first["build_hash"] == second["build_hash"]
    for side in ("previous", "current"):
        assert first[side]["section_hash"] == second[side]["section_hash"]
        assert first[side]["section_hash"] is not None
    execution_first = dict(first["execution"])
    execution_second = dict(second["execution"])
    assert execution_first["result_hash"] == execution_second["result_hash"]


# --- Comparison gating -------------------------------------------------------------


def test_only_fully_extracted_pairs_run_comparison(full_run):
    layout = full_run["layout"]
    blocked = {
        _pair_id(full_run["original_manifest"], MISSING_CURRENT_INDEX),
        _pair_id(full_run["original_manifest"], AMBIGUOUS_PREVIOUS_INDEX),
    }
    for entry in full_run["execution"]["executions"]:
        record_path = layout.build_record_path(entry["pair_id"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if entry["pair_id"] in blocked:
            assert entry["buildable"] is False
            assert entry["executed"] is False
            assert entry["execution_status"] is None
            assert entry["result_hash"] is None
            assert entry["attempt_count"] == 0
            assert "skipped_reason" in entry and entry["skipped_reason"]
            # No detector attempt exists at all: the workflow database was
            # never created for this pair.
            workspace = layout.workspace_dir(entry["pair_id"])
            assert not (workspace / "comparisons.db").exists()
            assert record["execution"]["executed"] is False
        else:
            assert entry["buildable"] is True
            assert entry["executed"] is True
            assert entry["execution_status"] == "detected"
            assert entry["result_hash"]
            assert entry["retries"] == 0
            assert entry["reclaims"] == 0
            assert entry["detection_jobs"] == 0
    assert full_run["execution"]["comparisons_executed"] == 8


# --- Packets -----------------------------------------------------------------------


def test_packets_exist_only_for_detected_pairs(full_run):
    layout = full_run["layout"]
    blocked = {
        _pair_id(full_run["original_manifest"], MISSING_CURRENT_INDEX),
        _pair_id(full_run["original_manifest"], AMBIGUOUS_PREVIOUS_INDEX),
    }
    for row in full_run["inventory"]["pairs"]:
        packet_path = layout.packet_json_path(row["pair_id"])
        if row["pair_id"] in blocked:
            assert row["packet_status"] == "blocked"
            assert row["blocking_reason"] == (
                "item_1a_not_extracted_for_both_sides"
            )
            assert row["review_ready"] is False
            assert row["packet_hash"] is None
            assert not packet_path.exists()
        else:
            assert row["packet_status"] == "written"
            assert row["review_ready"] is True
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            assert rfb.payload_hash(packet) == row["packet_hash"]
    assert full_run["inventory"]["packets_written"] == 8
    assert full_run["inventory"]["packets_blocked"] == 2


def test_every_generated_annotation_stays_machine_proposed(full_run):
    layout = full_run["layout"]
    checked = 0
    for row in full_run["inventory"]["pairs"]:
        assert row["annotation_status"] == rfb.ANNOTATION_MACHINE_PROPOSED
        assert row["human_verified"] is False
        annotation_path = layout.machine_proposed_path(row["pair_id"])
        if not annotation_path.exists():
            continue
        annotation = rfb.load_annotation(annotation_path)
        assert (
            annotation["annotation_status"] == rfb.ANNOTATION_MACHINE_PROPOSED
        )
        assert annotation["annotator_id"] is None
        assert annotation["verification_timestamp"] is None
        assert not rfb.is_gold(annotation)
        # The annotation names the holdout corpus, not the development one.
        assert annotation["benchmark_id"] == (
            full_run["original_manifest"]["benchmark_id"]
        )
        checked += 1
    assert checked == 8


def test_zero_human_verified_labels_everywhere(full_run):
    assert full_run["blind"]["human_verified_labels"] == 0
    assert full_run["execution"]["human_verified_labels"] == 0
    assert full_run["inventory"]["human_verified_labels"] == 0
    assert full_run["execution"]["corpus_quality"]["pairs_human_verified"] == 0


def test_no_gold_metric_exists_in_any_report(full_run):
    for report in (full_run["blind"], full_run["execution"]):
        assert report["gold_metrics_available"] is False
        assert report["gold_metrics"] is None
    for name in ("blind", "execution", "inventory"):
        raw = json.dumps(full_run[name]).lower()
        for banned in (
            '"precision"',
            '"recall"',
            '"f1"',
            '"exact_match"',
            '"accuracy"',
        ):
            assert banned not in raw, (name, banned)


def test_reports_carry_no_filing_text_or_local_paths(full_run):
    layout_root = str(full_run["layout"].root)
    for name in ("blind", "execution", "inventory"):
        raw = json.dumps(full_run[name])
        assert "/Users/" not in raw
        assert layout_root not in raw
        # Sentences unique to the fixture bodies must never surface.
        assert "intrusion events" not in raw
        assert "Risk narrative" not in raw
        assert "excerpt" not in raw.lower()


def test_corpus_role_denials_survive_a_completed_run(full_run):
    for name in ("blind", "execution", "inventory"):
        report = full_run[name]
        assert report["corpus_role"] == rfb.CORPUS_ROLE_EXTRACTION_HOLDOUT
        assert report["extraction_parser_developed_using_this_corpus"] is False
        assert report["extraction_holdout_evaluation"] is False
        assert report["generalization_claim_supported"] is False


def test_advance_refuses_a_second_step_or_a_skipped_pair(full_run):
    advanced = full_run["advanced_manifest"]
    with pytest.raises(rfb.StatusTransitionError):
        rfh.validate_holdout_status_transition(
            advanced["status"], rfb.STATUS_CORPUS_BUILT
        )
    document = copy.deepcopy(full_run["original_manifest"])
    with pytest.raises(rfhe.HoldoutExtractionError) as excinfo:
        rfhe.advance_holdout_manifest_to_corpus_built(document, [])
    assert excinfo.value.code == rfhe.FAILURE_SIDES_NOT_ALL_ATTEMPTED
