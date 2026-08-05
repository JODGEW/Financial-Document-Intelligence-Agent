"""Offline behavior tests for the v3 holdout blind-extraction pipeline.

Every test runs over SYNTHETIC fixtures in temporary directories: the frozen
v3 holdout identities are reused (they are public metadata), but every body is
a hand-written fictional HTML document, so no real filing content is needed and
no network can be reached. The suite pins what this Stage 3.5 commit claims:
frozen identities cannot drift, all twenty sides are attempted exactly once
under byte-identical frozen code, the v3 unit grammar is the one that ran,
canonical sequence-aware unit identities survive repeated normalized headings,
every outcome is preserved exactly as observed, only fully extracted pairs
reach the comparison workflow, packets exist only for detected pairs and stay
machine-proposed, and no path produces a human-verified label or a gold metric.

The companion suite (``tests/test_v3_holdout_blind_artifacts.py``) covers the
committed artifacts, the manifest lifecycle, CI pinning, and output hygiene.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest

import comparison_detector
import comparison_store
import real_filing_benchmark as rfb
import real_filing_v3_holdout as rfv3
import real_filing_v3_holdout_extraction as rfx
from scripts import build_real_filing_benchmark as builder
from scripts import run_real_filing_v3_holdout_blind_extraction as cli
from tests.helpers import v3_blind_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parent.parent

COMMITTED_MANIFEST = rfv3.load_v3_holdout_manifest()

#: Pair indexes given deliberately imperfect fixtures, so outcome preservation
#: is exercised by the same full run that exercises the happy path.
MISSING_CURRENT_INDEX = 1
AMBIGUOUS_PREVIOUS_INDEX = 2
REPEATED_HEADING_INDEX = 3

REPORT_NAMES = (
    "blind_extraction_report.json",
    "execution_report.json",
    "annotation_packet_inventory.json",
)


# --- Fixtures -----------------------------------------------------------------------


def synthetic(**overrides):
    return fx.synthetic_manifest(COMMITTED_MANIFEST, **overrides)


def imperfect():
    return synthetic(
        missing_current_index=MISSING_CURRENT_INDEX,
        ambiguous_previous_index=AMBIGUOUS_PREVIOUS_INDEX,
        repeated_heading_index=REPEATED_HEADING_INDEX,
    )


@pytest.fixture(scope="module")
def full_run(tmp_path_factory):
    """One full pipeline run over the synthetic corpus, shared by the run-level
    assertions. ``builder._extract_side`` is instrumented so the suite can
    prove every side was attempted exactly once, in canonical order."""
    base = tmp_path_factory.mktemp("v3_blind")
    root = fx.untracked_root(base, "v3_holdout_full")
    document, contents = imperfect()
    manifest_path = root / "manifest.json"
    fx.write_manifest(manifest_path, document)
    layout = fx.seed_corpus(root / "corpus", document, contents)
    digests_before = {
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
        for name in REPORT_NAMES
    }
    return {
        "exit_code": code,
        "original_manifest": document,
        "manifest_path": manifest_path,
        "advanced_manifest": json.loads(manifest_path.read_text(encoding="utf-8")),
        "layout": layout,
        "contents": contents,
        "digests_before": digests_before,
        "extract_calls": extract_calls,
        "blind": reports["blind_extraction_report.json"],
        "execution": reports["execution_report.json"],
        "inventory": reports["annotation_packet_inventory.json"],
    }


def _pair_id(document: dict, index: int) -> str:
    return document["pairs"][index]["pair_id"]


def _run(tmp_path, document, contents, *, name="case"):
    root = fx.untracked_root(tmp_path, name)
    manifest_path = root / "manifest.json"
    fx.write_manifest(manifest_path, document)
    layout = fx.seed_corpus(root / "corpus", document, contents)
    return root, manifest_path, layout


def _preflight(tmp_path, document, contents, *, name="case"):
    root, manifest_path, layout = _run(tmp_path, document, contents, name=name)
    return rfx.verify_blind_run_preconditions(document, manifest_path, layout)


# --- Preflight: drift is refused before the pipeline reads a byte -------------------


def test_source_verified_manifest_is_accepted(tmp_path):
    document, contents = synthetic()
    result = _preflight(tmp_path, document, contents)
    assert len(result["source_verifications"]) == 20
    assert result["run_identity"]["run_hash"]


def test_metadata_only_manifest_cannot_run_extraction(tmp_path):
    document, contents = synthetic()
    rewound = copy.deepcopy(document)
    rewound["status"] = rfv3.STATUS_HOLDOUT_FROZEN_METADATA_ONLY
    for pair in rewound["pairs"]:
        for side in ("previous", "current"):
            pair[side]["expected_sha256"] = None
            pair[side]["source_verified"] = False
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        _preflight(tmp_path, rewound, contents)
    assert excinfo.value.code == rfx.FAILURE_MANIFEST_STATUS_INVALID


def test_human_annotation_complete_status_is_rejected(tmp_path):
    document, contents = synthetic()
    ahead = copy.deepcopy(document)
    ahead["status"] = rfb.STATUS_HUMAN_ANNOTATION_COMPLETE
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        _preflight(tmp_path, ahead, contents)
    assert excinfo.value.code == rfx.FAILURE_MANIFEST_STATUS_INVALID


def test_canonical_run_requires_ten_pairs_and_twenty_sides(tmp_path):
    document, contents = synthetic()
    short = copy.deepcopy(document)
    short["pairs"] = short["pairs"][:9]
    # The schema refuses a nine-pair v3 manifest outright; the blind gate
    # refuses the same shape with its own code when validation is bypassed.
    with pytest.raises(rfb.BenchmarkError):
        rfx.verify_blind_run_preconditions(
            short, tmp_path / "manifest.json", rfb.CorpusLayout(tmp_path)
        )
    document, contents = synthetic()
    assert len(document["pairs"]) == 10
    result = _preflight(tmp_path, document, contents, name="ten")
    assert len(result["resolved_sources"]) == 20


def test_missing_expected_sha256_is_rejected(tmp_path):
    document, contents = synthetic()
    document["pairs"][0]["current"]["expected_sha256"] = None
    with pytest.raises(rfb.BenchmarkError):
        _preflight(tmp_path, document, contents)


def test_source_verified_false_is_rejected(tmp_path):
    document, contents = synthetic()
    document["pairs"][4]["previous"]["source_verified"] = False
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        _preflight(tmp_path, document, contents)
    assert excinfo.value.code == rfx.FAILURE_MANIFEST_BINDING_MISMATCH


def test_missing_local_file_is_refused_not_downloaded(tmp_path):
    document, contents = synthetic()
    root, manifest_path, layout = _run(tmp_path, document, contents)
    pair = document["pairs"][3]
    layout.source_file(
        pair["pair_id"], "current", pair["current"]["primary_document"]
    ).unlink()
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.verify_blind_run_preconditions(document, manifest_path, layout)
    assert excinfo.value.code == rfx.FAILURE_SOURCE_MISSING
    assert "never downloads" in excinfo.value.message


def test_local_hash_mismatch_is_refused(tmp_path):
    document, contents = synthetic()
    root, manifest_path, layout = _run(tmp_path, document, contents)
    pair = document["pairs"][0]
    target = layout.source_file(
        pair["pair_id"], "previous", pair["previous"]["primary_document"]
    )
    target.write_text(contents[(pair["pair_id"], "previous")] + "\n<!-- x -->\n")
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.verify_blind_run_preconditions(document, manifest_path, layout)
    assert excinfo.value.code == rfx.FAILURE_SOURCE_SHA256_MISMATCH
    # The file is preserved and no committed hash is edited.
    assert target.exists()
    assert pair["previous"]["expected_sha256"] == fx.sha256(
        contents[(pair["pair_id"], "previous")]
    )


def test_empty_local_source_is_refused_by_the_digest_gate(tmp_path):
    document, contents = synthetic()
    root, manifest_path, layout = _run(tmp_path, document, contents)
    pair = document["pairs"][2]
    layout.source_file(
        pair["pair_id"], "current", pair["current"]["primary_document"]
    ).write_text("")
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.verify_blind_run_preconditions(document, manifest_path, layout)
    assert excinfo.value.code == rfx.FAILURE_SOURCE_SHA256_MISMATCH


def test_duplicate_local_path_is_rejected(tmp_path):
    document, contents = synthetic()
    root, manifest_path, layout = _run(tmp_path, document, contents)
    # Distinct manifest sides must never resolve to the same local file. The
    # schema's own uniqueness rules make this unreachable from a valid
    # manifest, so the resolver is exercised directly on the shape it must
    # refuse: two sides sharing a pair id, a side directory, and a file name.
    document["pairs"][1]["pair_id"] = document["pairs"][0]["pair_id"]
    document["pairs"][1]["previous"]["primary_document"] = document["pairs"][0][
        "previous"
    ]["primary_document"]
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.resolve_source_paths(document, layout)
    assert excinfo.value.code == rfx.FAILURE_DUPLICATE_SOURCE_PATH


def test_duplicate_source_identity_is_rejected(tmp_path):
    document, contents = synthetic()
    root, manifest_path, layout = _run(tmp_path, document, contents)
    first = document["pairs"][0]
    second = document["pairs"][1]
    second["cik"] = first["cik"]
    second["previous"]["accession_number"] = first["previous"]["accession_number"]
    second["previous"]["primary_document"] = first["previous"]["primary_document"]
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.resolve_source_paths(document, layout)
    assert excinfo.value.code == rfx.FAILURE_DUPLICATE_SOURCE_IDENTITY


def test_source_path_traversal_is_rejected(tmp_path):
    document, contents = synthetic()
    root, manifest_path, layout = _run(tmp_path, document, contents)
    document["pairs"][0]["previous"]["primary_document"] = "../escape.htm"
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.resolve_source_paths(document, layout)
    assert excinfo.value.code == rfx.FAILURE_SOURCE_PATH_INVALID


def test_tracked_corpus_root_is_rejected(tmp_path):
    document, _contents = synthetic()
    tracked = rfb.CorpusLayout(REPO_ROOT / "benchmarks" / "real_filing_v3_holdout_v1")
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.resolve_source_paths(document, tracked)
    assert excinfo.value.code == rfx.FAILURE_TRACKED_SOURCE_PATH


def test_source_verification_report_mismatch_is_rejected(tmp_path):
    document, contents = synthetic()
    root, manifest_path, layout = _run(tmp_path, document, contents)
    report = json.loads(
        (
            REPO_ROOT
            / "benchmarks"
            / "real_filing_v3_holdout_v1"
            / "source_verification_report.json"
        ).read_text(encoding="utf-8")
    )
    report["frozen_detector_version"] = "item1a_detector.v2"
    (manifest_path.parent / "source_verification_report.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.verify_source_verification_binding(document, manifest_path)
    assert excinfo.value.code == rfx.FAILURE_MANIFEST_BINDING_MISMATCH


@pytest.mark.parametrize(
    "field",
    (
        "frozen_parser_source_sha256",
        "frozen_detector_source_sha256",
        "frozen_workflow_source_sha256",
        "frozen_evaluator_source_sha256",
    ),
)
def test_frozen_source_hash_drift_is_rejected(tmp_path, field):
    document, contents = synthetic()
    document[field] = "0" * 64
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        _preflight(tmp_path, document, contents)
    assert excinfo.value.code == rfx.FAILURE_CONTRACT_VERSION_MISMATCH


def test_unit_grammar_binding_mismatch_is_rejected():
    document = copy.deepcopy(COMMITTED_MANIFEST)
    document["frozen_unit_grammar_version"] = comparison_detector.UNIT_GRAMMAR_V2
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.verify_contract_bindings(document)
    assert excinfo.value.code == rfx.FAILURE_CONTRACT_VERSION_MISMATCH


def test_detector_and_workflow_binding_mismatches_are_rejected():
    for field, value in (
        ("frozen_detector_version", "item1a_detector.v2"),
        ("frozen_workflow_version", "comparison_workflow.v2"),
    ):
        document = copy.deepcopy(COMMITTED_MANIFEST)
        document[field] = value
        with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
            rfx.verify_contract_bindings(document)
        assert excinfo.value.code == rfx.FAILURE_CONTRACT_VERSION_MISMATCH


def test_evaluation_contract_binding_is_pinned_live():
    # The evaluation contract the manifest declares is the one the frozen
    # evaluator implements; the blind run never invokes it, but a drifted
    # declaration would mean this corpus no longer describes contract v2.
    assert (
        COMMITTED_MANIFEST["frozen_evaluation_contract_version"]
        == rfv3.FROZEN_EVALUATION_CONTRACT_VERSION
    )
    assert (
        rfx.blind_run_protocol()["evaluation_contract_version"]
        == rfv3.FROZEN_EVALUATION_CONTRACT_VERSION
    )


def test_hand_edited_manifest_is_rejected_by_the_hash_chain(tmp_path):
    document, contents = synthetic()
    root, manifest_path, layout = _run(tmp_path, document, contents)
    (manifest_path.parent / "source_verification_report.json").write_text(
        json.dumps({"new_manifest_sha256": "0" * 64}), encoding="utf-8"
    )
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.verify_manifest_hash_chain(manifest_path, document["status"])
    assert excinfo.value.code == rfx.FAILURE_MANIFEST_BINDING_MISMATCH


def test_existing_conflicting_run_identity_is_rejected(tmp_path):
    document, contents = synthetic()
    root, manifest_path, layout = _run(tmp_path, document, contents)
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "blind_extraction_report.json").write_text(
        json.dumps({"run_hash": "0" * 64}), encoding="utf-8"
    )
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.verify_blind_run_preconditions(
            document, manifest_path, layout, report_dir=reports
        )
    assert excinfo.value.code == rfx.FAILURE_OUTPUT_IDENTITY_CONFLICT


def test_a_human_annotation_refuses_the_run(tmp_path):
    document, contents = synthetic()
    root, manifest_path, layout = _run(tmp_path, document, contents)
    annotations = layout.annotations_dir()
    annotations.mkdir(parents=True, exist_ok=True)
    (annotations / "sic-2000s-01.json").write_text(
        json.dumps({"annotation_status": rfb.ANNOTATION_HUMAN_VERIFIED}),
        encoding="utf-8",
    )
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.verify_blind_run_preconditions(document, manifest_path, layout)
    assert excinfo.value.code == rfx.FAILURE_HUMAN_ANNOTATION_PRESENT


def test_preflight_failure_processes_zero_bodies(tmp_path, monkeypatch):
    document, contents = synthetic()
    root, manifest_path, layout = _run(tmp_path, document, contents)
    pair = document["pairs"][0]
    layout.source_file(
        pair["pair_id"], "previous", pair["previous"]["primary_document"]
    ).unlink()
    calls: list[str] = []
    monkeypatch.setattr(
        builder,
        "_ingest_pair",
        lambda *a, **k: calls.append("ingest"),
    )
    code = cli.main(
        [
            "--manifest", str(manifest_path),
            "--corpus-dir", str(root / "corpus"),
            "--report-dir", str(root / "reports"),
        ]
    )
    assert code == 2
    assert calls == []
    assert not (root / "reports").exists() or not list((root / "reports").glob("*.json"))
    assert json.loads(manifest_path.read_text())["status"] == (
        rfb.STATUS_SOURCE_VERIFIED
    )


def test_committed_tree_passes_the_blind_run_preconditions():
    """The committed manifest and the live working tree still agree.

    Deliberately does not touch the local corpus: the source gate is exercised
    by the synthetic cases above and by the artifact suite's digest check.
    """
    rfv3.validate_v3_holdout_manifest(COMMITTED_MANIFEST)
    rfv3.verify_frozen_code_identities(COMMITTED_MANIFEST)
    rfv3.verify_exclusion_provenance(COMMITTED_MANIFEST)
    rfx.verify_contract_bindings(COMMITTED_MANIFEST)
    rfx.verify_source_verification_binding(
        COMMITTED_MANIFEST, rfv3.default_v3_holdout_manifest_path()
    )


# --- Frozen code --------------------------------------------------------------------


def test_frozen_code_list_is_closed_and_covers_the_run():
    for name in rfx.FROZEN_CODE_FILES:
        assert (REPO_ROOT / name).exists(), name
    for pinned in (
        rfv3.FROZEN_PARSER_SOURCE_PATH,
        rfv3.FROZEN_DETECTOR_SOURCE_PATH,
        rfv3.FROZEN_WORKFLOW_SOURCE_PATH,
        rfv3.FROZEN_EVALUATOR_SOURCE_PATH,
    ):
        assert pinned in rfx.FROZEN_CODE_FILES


def test_frozen_code_change_during_the_run_fails_the_report():
    before = rfx.frozen_code_hashes()
    after = dict(before)
    after["comparison_detector.py"] = "0" * 64
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.require_frozen_code_unchanged(before, after)
    assert excinfo.value.code == rfx.FAILURE_FROZEN_CODE_CHANGED


def test_full_run_attests_byte_identical_frozen_code(full_run):
    blind = full_run["blind"]
    assert blind["frozen_code_unchanged"] is True
    assert blind["frozen_code_hashes_before"] == blind["frozen_code_hashes_after"]
    assert set(blind["frozen_code_hashes_before"]) == set(rfx.FROZEN_CODE_FILES)
    assert blind["semantic_code_modified_during_run"] is False


# --- No network, no evaluator ---------------------------------------------------------


@pytest.mark.parametrize(
    "module_path",
    [
        "real_filing_v3_holdout_extraction.py",
        "scripts/run_real_filing_v3_holdout_blind_extraction.py",
    ],
)
def test_blind_run_imports_nothing_that_can_reach_a_network(module_path):
    imported = _imports_of(module_path)
    for forbidden in (
        "urllib",
        "http",
        "socket",
        "requests",
        "httpx",
        "boto3",
        "real_filing_acquisition",
        "real_filing_holdout_acquisition",
        "real_filing_v3_holdout_acquisition",
    ):
        assert forbidden not in imported, forbidden


@pytest.mark.parametrize(
    "module_path",
    [
        "real_filing_v3_holdout_extraction.py",
        "scripts/run_real_filing_v3_holdout_blind_extraction.py",
    ],
)
def test_blind_run_never_imports_the_gold_evaluator(module_path):
    source = (REPO_ROOT / module_path).read_text(encoding="utf-8")
    assert "eval_real_filing_benchmark" not in source.replace(
        rfv3.FROZEN_EVALUATOR_SOURCE_PATH, ""
    )


def _imports_of(module_path: str) -> set[str]:
    source = (REPO_ROOT / module_path).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def test_no_sec_user_agent_or_environment_value_is_read_by_the_blind_run():
    """Naming the variable in a denial is fine; READING one is not."""
    for module_path in (
        "real_filing_v3_holdout_extraction.py",
        "scripts/run_real_filing_v3_holdout_blind_extraction.py",
    ):
        tree = ast.parse((REPO_ROOT / module_path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "SEC_USER_AGENT", module_path
                assert node.attr not in ("getenv", "environ"), module_path
            if isinstance(node, ast.Name):
                assert node.id != "SEC_USER_AGENT", module_path


# --- The run itself --------------------------------------------------------------------


def test_full_run_completes_and_advances_exactly_one_step(full_run):
    assert full_run["exit_code"] == 0
    assert full_run["original_manifest"]["status"] == rfb.STATUS_SOURCE_VERIFIED
    assert full_run["advanced_manifest"]["status"] == rfb.STATUS_CORPUS_BUILT
    rfv3.validate_v3_holdout_status_transition(
        rfb.STATUS_SOURCE_VERIFIED, full_run["advanced_manifest"]["status"]
    )
    assert full_run["blind"]["prior_manifest_status"] == rfb.STATUS_SOURCE_VERIFIED
    assert full_run["blind"]["new_manifest_status"] == rfb.STATUS_CORPUS_BUILT


def test_all_twenty_sides_are_attempted_exactly_once(full_run):
    calls = full_run["extract_calls"]
    assert len(calls) == 20
    assert len(set(calls)) == 20
    assert full_run["blind"]["side_count"] == 20
    assert full_run["blind"]["extraction_runs"] == 20


def test_execution_order_is_manifest_order_previous_then_current(full_run):
    expected = [
        (pair["pair_id"], side)
        for pair in full_run["original_manifest"]["pairs"]
        for side in ("previous", "current")
    ]
    rows = [(row["pair_id"], row["side"]) for row in full_run["blind"]["sides"]]
    assert rows == expected
    assert [row["pair_id"] for row in full_run["blind"]["pairs"]] == [
        pair["pair_id"] for pair in full_run["original_manifest"]["pairs"]
    ]
    # Not sorted by issuer, heading, outcome, unit count, or result quality.
    assert rows != sorted(rows, key=lambda row: row[1])
    assert rfx.EXECUTION_ORDER.startswith("manifest pair order")


def test_missing_outcome_is_preserved(full_run):
    pair_id = _pair_id(full_run["original_manifest"], MISSING_CURRENT_INDEX)
    row = next(
        row
        for row in full_run["blind"]["sides"]
        if row["pair_id"] == pair_id and row["side"] == "current"
    )
    assert row["extraction_status"] == rfb.EXTRACTION_MISSING
    assert row["section_sha256"] is None
    assert row["unit_count"] == 0


def test_ambiguous_outcome_is_preserved(full_run):
    pair_id = _pair_id(full_run["original_manifest"], AMBIGUOUS_PREVIOUS_INDEX)
    row = next(
        row
        for row in full_run["blind"]["sides"]
        if row["pair_id"] == pair_id and row["side"] == "previous"
    )
    assert row["extraction_status"] == rfb.EXTRACTION_AMBIGUOUS
    assert row["section_sha256"] is None


def test_parse_failure_is_recorded_never_repaired(tmp_path, monkeypatch):
    document, contents = synthetic()
    root, manifest_path, layout = _run(tmp_path, document, contents, name="failpair")

    def exploding_ingest(*_args, **_kwargs):
        raise RuntimeError("synthetic ingestion fault")

    monkeypatch.setattr(builder, "_ingest_pair", exploding_ingest)
    outcome = rfx.blind_extract_pair(
        document["pairs"][0],
        manifest=document,
        source_manifest_hash="0" * 64,
        layout=layout,
    )
    record = outcome["record"]
    for side in ("previous", "current"):
        assert record[side]["extraction_outcome"] == rfb.EXTRACTION_PARSE_FAILED
        assert record[side]["extraction_reason"].startswith(
            rfx.REASON_PAIR_BUILD_FAILED
        )
        # The bounded detail names the exception CLASS, never its message.
        assert "synthetic ingestion fault" not in record[side]["extraction_reason"]
    assert record["execution"]["executed"] is False


def test_every_side_and_pair_is_represented(full_run):
    blind = full_run["blind"]
    manifest_pairs = [
        pair["pair_id"] for pair in full_run["original_manifest"]["pairs"]
    ]
    assert sorted({row["pair_id"] for row in blind["sides"]}) == sorted(manifest_pairs)
    assert sorted(row["pair_id"] for row in blind["pairs"]) == sorted(manifest_pairs)
    assert blind["buildable_pair_count"] + blind["blocked_pair_count"] == 10


def test_no_issuer_pair_or_source_was_replaced(full_run):
    before = full_run["original_manifest"]["pairs"]
    after = full_run["advanced_manifest"]["pairs"]
    for original, advanced in zip(before, after):
        assert original["pair_id"] == advanced["pair_id"]
        assert original["cik"] == advanced["cik"]
        assert original["issuer_name"] == advanced["issuer_name"]
        for side in ("previous", "current"):
            assert original[side] == advanced[side]
    assert full_run["blind"]["pairs_replaced"] == 0
    layout = full_run["layout"]
    for (pair_id, side), digest in full_run["digests_before"].items():
        pair = next(
            pair
            for pair in full_run["original_manifest"]["pairs"]
            if pair["pair_id"] == pair_id
        )
        target = layout.source_file(pair_id, side, pair[side]["primary_document"])
        assert hashlib.sha256(target.read_bytes()).hexdigest() == digest


def test_source_digests_are_rechecked_immediately_before_extraction(full_run):
    # Twenty preflight verifications plus twenty per-pair rechecks inside the
    # build, which is the read that actually feeds the parser.
    assert full_run["blind"]["source_checksum_reverifications"] == 40
    assert full_run["execution"]["steps"]["source_checksum_reverifications"] == 40
    assert full_run["blind"]["source_downloads"] == 0
    assert full_run["blind"]["network_requests"] == 0


def test_extraction_is_deterministic_for_identical_inputs(tmp_path):
    # Two independent corpus directories seeded with byte-identical inputs:
    # determinism is a property of the inputs, not of a shared workspace (and
    # chromadb caches per-path clients within a process, so reusing one
    # workspace would test the cache, not the pipeline).
    document, contents = synthetic()
    first = rfx.blind_extract_pair(
        document["pairs"][0],
        manifest=document,
        source_manifest_hash="0" * 64,
        layout=fx.seed_corpus(
            fx.untracked_root(tmp_path, "a") / "corpus", document, contents
        ),
    )["record"]
    second = rfx.blind_extract_pair(
        document["pairs"][0],
        manifest=document,
        source_manifest_hash="0" * 64,
        layout=fx.seed_corpus(
            fx.untracked_root(tmp_path, "b") / "corpus", document, contents
        ),
    )["record"]
    assert first["build_hash"] == second["build_hash"]
    for side in ("previous", "current"):
        assert first[side]["section_hash"] == second[side]["section_hash"]
        assert first[side]["section_hash"] is not None
        assert rfb.build_unit_ids(first, side) == rfb.build_unit_ids(second, side)
    assert first["execution"]["result_hash"] == second["execution"]["result_hash"]


def test_run_identity_is_stable_across_the_manifest_transition(full_run):
    before = rfx.corpus_identity_hash(full_run["original_manifest"])
    after = rfx.corpus_identity_hash(full_run["advanced_manifest"])
    assert before == after == full_run["blind"]["corpus_identity_hash"]


def test_reproducible_payload_excludes_timestamps_and_paths(full_run):
    blind = full_run["blind"]
    projection = rfx.reproducible_report(blind)
    assert "generated_at" not in projection
    assert "commit_sha" not in projection
    assert "prior_manifest_sha256" not in projection
    assert all("duration_ms" not in row for row in projection["sides"])
    assert blind["reproducible_payload_hash"] == rfx.reproducible_payload_hash(blind)


# --- The v3 unit grammar ---------------------------------------------------------------


def test_item1a_units_v3_is_the_grammar_that_ran(full_run):
    assert (
        comparison_detector.DEFAULT_UNIT_GRAMMAR == comparison_detector.UNIT_GRAMMAR_V3
    )
    assert full_run["execution"]["unit_grammar_version"] == "item1a_units.v3"
    assert full_run["blind"]["frozen_unit_grammar_version"] == "item1a_units.v3"
    assert comparison_detector.UNIT_GRAMMAR_V2 not in json.dumps(full_run["blind"])


def test_v3_heading_classes_are_recognized_through_the_frozen_pipeline(tmp_path):
    document, contents = synthetic()
    record = rfx.blind_extract_pair(
        document["pairs"][0],
        manifest=document,
        source_manifest_hash="0" * 64,
        layout=fx.seed_corpus(
            fx.untracked_root(tmp_path, "grammar") / "corpus", document, contents
        ),
    )["record"]
    keys = {unit["unit_key"] for unit in record["previous"]["units"]}
    # prefix form, closed general literal, and the slash punctuation v3 adds
    assert "risks-related-to-our-business" in keys
    assert "general-risk-factors" in keys
    assert "compliance-legal-operations-risks" in keys


def test_repeated_normalized_headings_stay_distinct_units(tmp_path):
    document, contents = synthetic(repeated_heading_index=0)
    record = rfx.blind_extract_pair(
        document["pairs"][0],
        manifest=document,
        source_manifest_hash="0" * 64,
        layout=fx.seed_corpus(
            fx.untracked_root(tmp_path, "repeat") / "corpus", document, contents
        ),
    )["record"]
    units = record["current"]["units"]
    keys = [unit["unit_key"] for unit in units]
    assert keys.count("risks-related-to-our-operations") == 2
    ids = [unit["unit_id"] for unit in units]
    assert len(ids) == len(set(ids))
    # Sequence-aware identity, in source order, never merged by heading key.
    assert ids == [
        rfb.unit_id("current", index, unit["unit_key"])
        for index, unit in enumerate(units)
    ]


def test_canonical_unit_identity_uses_the_frozen_shared_contract():
    assert COMMITTED_MANIFEST["frozen_unit_identity_contract"] == "side:sequence:unit_key"
    unit = comparison_detector.RiskFactorUnit(
        unit_key="operational-risks",
        heading="Operational Risks",
        filing_id="f",
        text="",
        content_hash="h",
        chunks=[],
        sequence=7,
    )
    assert comparison_detector.unit_identity("previous", unit) == rfb.unit_id(
        "previous", 7, "operational-risks"
    )


def test_duplicate_canonical_unit_identity_is_rejected():
    record = {
        "pair_id": "sic-2000s-01",
        "previous": {
            "units": [
                {"unit_id": "previous:000:a", "unit_key": "a"},
                {"unit_id": "previous:000:a", "unit_key": "a"},
            ]
        },
        "current": {"units": []},
    }
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.validate_canonical_unit_identities(record)
    assert excinfo.value.code in (
        rfx.FAILURE_UNIT_IDENTITY_INVALID,
        rfx.FAILURE_DUPLICATE_UNIT_IDENTITY,
    )


def test_unit_text_never_reaches_a_tracked_artifact(full_run):
    for name in REPORT_NAMES:
        raw = json.dumps(full_run[
            {
                "blind_extraction_report.json": "blind",
                "execution_report.json": "execution",
                "annotation_packet_inventory.json": "inventory",
            }[name]
        ])
        assert "Fictional sentence" not in raw
        assert "excerpt" not in raw


# --- Comparison gating -------------------------------------------------------------------


def test_only_fully_extracted_pairs_run_comparison(full_run):
    blocked_ids = {
        _pair_id(full_run["original_manifest"], MISSING_CURRENT_INDEX),
        _pair_id(full_run["original_manifest"], AMBIGUOUS_PREVIOUS_INDEX),
    }
    for row in full_run["blind"]["pairs"]:
        if row["pair_id"] in blocked_ids:
            assert row["buildable"] is False
            assert row["comparison_executed"] is False
            assert row["comparison_result_hash"] is None
            assert row["change_count"] == 0
            assert row["blocked_reason"] in rfx.BLOCKED_PAIR_REASONS
        else:
            assert row["buildable"] is True
            assert row["comparison_executed"] is True
            assert row["comparison_result_hash"]
            assert row["detector_version"] == comparison_detector.DETECTOR_VERSION
            assert row["workflow_version"] == comparison_store.WORKFLOW_VERSION
    assert full_run["blind"]["blocked_pair_count"] == 2
    assert full_run["blind"]["comparison_result_count"] == 8


def test_blocked_reasons_name_the_side_that_blocked(full_run):
    by_pair = {row["pair_id"]: row for row in full_run["blind"]["pairs"]}
    missing = by_pair[_pair_id(full_run["original_manifest"], MISSING_CURRENT_INDEX)]
    ambiguous = by_pair[
        _pair_id(full_run["original_manifest"], AMBIGUOUS_PREVIOUS_INDEX)
    ]
    assert missing["blocked_reason"] == rfx.BLOCKED_CURRENT_NOT_EXTRACTED
    assert ambiguous["blocked_reason"] == rfx.BLOCKED_PREVIOUS_NOT_EXTRACTED


def test_blocked_pair_is_never_replaced_or_removed(full_run):
    blind = full_run["blind"]
    assert len(blind["pairs"]) == 10
    assert blind["pairs_replaced"] == 0
    assert sum(blind["blocked_reason_counts"].values()) == blind["blocked_pair_count"]
    # No fabricated empty success result for a blocked pair.
    for row in blind["pairs"]:
        if not row["buildable"]:
            assert row["comparison_result_hash"] is None
            assert row["packet_sha256"] is None


def test_both_sides_blocked_is_recorded(tmp_path):
    document, contents = synthetic()
    pair = document["pairs"][0]
    for side in ("previous", "current"):
        contents[(pair["pair_id"], side)] = fx.NO_SECTION_HTML
        pair[side]["expected_sha256"] = fx.sha256(fx.NO_SECTION_HTML)
    root, manifest_path, layout = _run(tmp_path, document, contents, name="bothblocked")
    record = rfx.blind_extract_pair(
        pair, manifest=document, source_manifest_hash="0" * 64, layout=layout
    )["record"]
    assert rfx._blocked_reason(record) == rfx.BLOCKED_BOTH_SIDES_NOT_EXTRACTED


def test_no_gold_metric_field_exists_in_any_report(full_run):
    """Metric words may appear in a denial sentence; a metric FIELD may not."""
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

    for key in ("blind", "execution", "inventory"):
        walk(full_run[key])
    assert full_run["blind"]["gold_evaluation_runs"] == 0
    assert full_run["blind"]["gold_metrics"] is None
    assert full_run["blind"]["gold_metrics_available"] is False
    assert full_run["execution"]["gold_metrics_available"] is False
    assert full_run["inventory"]["gold_evaluation_runs"] == 0


# --- Packets and machine proposals ----------------------------------------------------------


def test_packets_exist_only_for_detected_pairs(full_run):
    inventory = full_run["inventory"]
    assert inventory["packets_written"] == 8
    assert inventory["packets_blocked"] == 2
    layout = full_run["layout"]
    for row in inventory["pairs"]:
        packet_path = layout.packet_json_path(row["pair_id"])
        if row["packet_status"] == "written":
            assert packet_path.exists()
            assert row["packet_sha256"]
            assert row["review_ready"] is True
        else:
            assert not packet_path.exists()
            assert row["packet_sha256"] is None
            assert row["blocking_reason"] in (
                rfx.PACKET_BLOCKED_NOT_EXTRACTED,
                rfx.PACKET_BLOCKED_NOT_DETECTED,
            )


def test_every_packet_states_the_machine_proposed_banner(full_run):
    from scripts import create_real_filing_annotation_packets as packets

    layout = full_run["layout"]
    for row in full_run["inventory"]["pairs"]:
        if row["packet_status"] != "written":
            continue
        packet = json.loads(
            layout.packet_json_path(row["pair_id"]).read_text(encoding="utf-8")
        )
        assert packet["banner"] == packets.MACHINE_PROPOSAL_BANNER
        assert "MACHINE-PROPOSED — NOT GROUND TRUTH" in packet["banner"]
        assert packet["human_verification_required"] is True
        markdown = layout.packet_markdown_path(row["pair_id"]).read_text(
            encoding="utf-8"
        )
        assert "MACHINE-PROPOSED — NOT GROUND TRUTH" in markdown
        assert "annotator_id" in markdown
        assert "verification_timestamp" in markdown
        assert "contributes nothing to any metric" in markdown


def test_every_generated_annotation_stays_machine_proposed(full_run):
    layout = full_run["layout"]
    for row in full_run["inventory"]["pairs"]:
        if row["packet_status"] != "written":
            continue
        annotation = json.loads(
            layout.machine_proposed_path(row["pair_id"]).read_text(encoding="utf-8")
        )
        assert annotation["annotation_status"] == rfb.ANNOTATION_MACHINE_PROPOSED
        assert annotation["annotator_id"] is None
        assert annotation["verification_timestamp"] is None
        assert rfb.is_gold(annotation) is False
        assert annotation["benchmark_id"] == "real_filing_v3_holdout_v1"


def test_machine_labels_bind_canonical_unit_identities(full_run):
    layout = full_run["layout"]
    for row in full_run["inventory"]["pairs"]:
        if row["packet_status"] != "written":
            continue
        record = builder.load_build_record(row["pair_id"], layout)
        known = set(rfb.build_unit_ids(record, "previous")) | set(
            rfb.build_unit_ids(record, "current")
        )
        annotation = json.loads(
            layout.machine_proposed_path(row["pair_id"]).read_text(encoding="utf-8")
        )
        for label in annotation["labels"]:
            for field, side in (
                ("previous_unit_id", "previous"),
                ("current_unit_id", "current"),
            ):
                value = label[field]
                if value is None:
                    continue
                assert value in known
                assert value.startswith(f"{side}:")
            # A label id is never a unit identity and never a change id.
            assert label["label_id"].startswith("lbl-")
            assert label["label_id"] not in known


def test_repeated_headings_keep_one_packet_row_per_occurrence(tmp_path):
    from scripts import create_real_filing_annotation_packets as packets

    document, contents = synthetic(repeated_heading_index=0)
    root, manifest_path, layout = _run(tmp_path, document, contents, name="pktrepeat")
    pair = document["pairs"][0]
    rfx.blind_extract_pair(
        pair, manifest=document, source_manifest_hash="0" * 64, layout=layout
    )
    packet, annotation = packets.build_packet(pair["pair_id"], layout, document)
    record = builder.load_build_record(pair["pair_id"], layout)
    repeated_ids = [
        unit["unit_id"]
        for side in ("previous", "current")
        for unit in record[side]["units"]
        if unit["unit_key"] == "risks-related-to-our-operations"
    ]
    assert len(repeated_ids) == 2
    labelled = {
        label[field]
        for label in annotation["labels"]
        for field in ("previous_unit_id", "current_unit_id")
        if label[field] is not None
    }
    for unit_id in repeated_ids:
        assert unit_id in labelled, unit_id
    # Rows are never deduplicated by unit_key.
    assert len(annotation["labels"]) == len(
        {label["label_id"] for label in annotation["labels"]}
    )


def test_zero_human_verified_labels_everywhere(full_run):
    assert full_run["blind"]["human_verified_label_count"] == 0
    assert full_run["inventory"]["human_verified_label_count"] == 0
    assert full_run["execution"]["human_verified_labels"] == 0
    for row in full_run["inventory"]["pairs"]:
        assert row["annotation_status"] == rfb.ANNOTATION_MACHINE_PROPOSED
        assert row["human_verified"] is False
        assert row["annotator_id"] is None
        assert row["verification_timestamp"] is None


def test_v2_labels_are_never_imported(full_run):
    raw = json.dumps(full_run["inventory"])
    assert "real_filing_holdout_v1" not in raw
    assert "real_filing_v1" not in raw
    assert "item1a_units.v2" not in raw


def test_inventory_carries_no_packet_body(full_run):
    raw = json.dumps(full_run["inventory"])
    assert "alignments" not in raw
    assert "banner" not in raw
    assert "excerpt" not in raw
    assert "Fictional sentence" not in raw


def test_packet_hash_is_deterministic(tmp_path):
    from scripts import create_real_filing_annotation_packets as packets

    document, contents = synthetic()
    hashes = []
    for name in ("pkta", "pktb"):
        layout = fx.seed_corpus(
            fx.untracked_root(tmp_path, name) / "corpus", document, contents
        )
        rfx.blind_extract_pair(
            document["pairs"][0],
            manifest=document,
            source_manifest_hash="0" * 64,
            layout=layout,
        )
        packet, _annotation = packets.build_packet(
            document["pairs"][0]["pair_id"], layout, document
        )
        hashes.append(rfb.payload_hash(packet))
    assert hashes[0] == hashes[1]


# --- Report structure and denials -------------------------------------------------------------


def test_reports_validate_with_exact_keys(full_run):
    rfx.validate_blind_extraction_report(full_run["blind"])
    rfx.validate_execution_report(full_run["execution"])
    rfx.validate_packet_inventory(full_run["inventory"])


@pytest.mark.parametrize(
    "key,validator",
    [
        ("blind", rfx.validate_blind_extraction_report),
        ("execution", rfx.validate_execution_report),
        ("inventory", rfx.validate_packet_inventory),
    ],
)
def test_unknown_report_keys_are_rejected(full_run, key, validator):
    document = copy.deepcopy(full_run[key])
    document["surprise_field"] = 1
    with pytest.raises(rfx.V3BlindExtractionError):
        validator(document)


def test_aggregate_counts_reconcile(full_run):
    blind = full_run["blind"]
    assert sum(blind["extraction_status_counts"].values()) == 20
    per_status: dict[str, int] = {}
    for row in blind["sides"]:
        per_status[row["extraction_status"]] = (
            per_status.get(row["extraction_status"], 0) + 1
        )
    for status, count in per_status.items():
        assert blind["extraction_status_counts"][status] == count
    assert blind["packet_count"] == full_run["inventory"]["packets_written"]
    assert (
        blind["machine_proposed_label_count"]
        == full_run["inventory"]["machine_proposed_label_count"]
    )
    assert blind["comparison_runs"] == blind["comparison_result_count"]


def test_report_denials_cannot_be_flipped(full_run):
    for field in (
        "extraction_holdout_evaluation",
        "generalization_claim_supported",
        "signoff_present",
        "gold_metrics_available",
    ):
        document = copy.deepcopy(full_run["blind"])
        document[field] = True
        with pytest.raises(rfx.V3BlindExtractionError):
            rfx.validate_blind_extraction_report(document)


def test_inventory_cannot_claim_a_human_verified_row(full_run):
    document = copy.deepcopy(full_run["inventory"])
    document["pairs"][0]["annotation_status"] = rfb.ANNOTATION_HUMAN_VERIFIED
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.validate_packet_inventory(document)
    assert excinfo.value.code == rfx.FAILURE_PACKET_INVENTORY_MISMATCH


def test_corpus_role_denials_survive_a_completed_run(full_run):
    for document in (
        full_run["advanced_manifest"],
        full_run["blind"],
        full_run["execution"],
        full_run["inventory"],
    ):
        assert document["corpus_role"] == "extraction_holdout_corpus"
        assert document["extraction_holdout_evaluation"] is False
        assert document["generalization_claim_supported"] is False
        assert document["extraction_parser_developed_using_this_corpus"] is False
        assert document["evaluation_contract_developed_using_this_corpus"] is False


def test_reports_carry_no_filing_text_or_local_paths(full_run):
    for key in ("blind", "execution", "inventory"):
        raw = json.dumps(full_run[key])
        lowered = raw.lower()
        assert "<html" not in lowered
        assert "fictional sentence" not in lowered
        assert str(REPO_ROOT).lower() not in lowered
        assert "/users/" not in lowered
        assert "/private/" not in lowered
        # Naming the gitignored tree in a note or a path convention is fine;
        # leaking the LOCAL corpus layout or an absolute path is not.
        assert "benchmark_data/real_filing_v3_holdout_v1" not in lowered
        assert "@" not in raw
        assert "cookie" not in lowered


def test_advance_refuses_a_second_step_or_a_skipped_pair(full_run):
    with pytest.raises(rfb.BenchmarkError):
        rfx.advance_v3_holdout_manifest_to_corpus_built(
            full_run["advanced_manifest"], []
        )
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        rfx.advance_v3_holdout_manifest_to_corpus_built(
            full_run["original_manifest"], []
        )
    assert excinfo.value.code == rfx.FAILURE_RUN_INCOMPLETE


def test_rerun_preserves_the_recorded_artifact(full_run, tmp_path):
    """A rerun over identical bytes must not replace the recorded run with a
    no-op summary, and a disagreeing payload fails closed."""
    report_path = tmp_path / "blind_extraction_report.json"
    report_path.write_text(json.dumps(full_run["blind"]), encoding="utf-8")
    assert cli._write_report(
        report_path,
        copy.deepcopy(full_run["blind"]),
        rfx.FAILURE_EXISTING_ARTIFACT_MISMATCH,
    ) == "unchanged"
    conflicting = copy.deepcopy(full_run["blind"])
    conflicting["buildable_pair_count"] = 10
    with pytest.raises(rfx.V3BlindExtractionError) as excinfo:
        cli._write_report(
            report_path, conflicting, rfx.FAILURE_EXISTING_ARTIFACT_MISMATCH
        )
    assert excinfo.value.code == rfx.FAILURE_EXISTING_ARTIFACT_MISMATCH
