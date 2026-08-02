"""Merge-blocking tests for the holdout human-annotation validator.

Every test runs over SYNTHETIC fixtures in temporary directories: the frozen
holdout identities are reused (they are public metadata), but every body is a
hand-written fictional HTML document from ``tests/helpers/real_filing_fixtures``
— no real filing excerpt, no real packet body, and no local-workspace file is
ever read or copied into a test. The one extraction-ambiguous pair is the same
pair the committed inventory blocks (``sic-6000s-01``), reproduced with an
ambiguous fixture on both sides so the synthetic inventory mirrors the frozen
review contract exactly: nine review-ready pairs, one blocked pair, zero
human-verified labels.

The suite pins what makes the validator a repository contract: frozen pair
set, every recorded identity (packet hash, source checksum, section hash,
result hash, parser/detector/workflow versions, manifest hash chain),
completed-annotation admission (explicitly human_verified, bounded annotator,
explicit-UTC timestamp, canonical label ids, exactly-once unit-id closure,
no filing excerpts, no absolute paths, no credential material), read-only
behavior in both modes, deterministic bounded output, and the structural
exclusion of the extraction-ambiguous pair from every count.

Two rules in the task are deliberately NOT invented here because the
repository defines no such convention: there is no placeholder-annotator
denylist (annotator_id is self-asserted local metadata by design, see
scripts/create_real_filing_annotation_packets.py), and there is no
implausible-future-timestamp window (the validator uses no wall clock at
all, which is what keeps it deterministic).
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

import real_filing_benchmark as rfb
import real_filing_holdout as rfh
from scripts import run_real_filing_holdout_blind_extraction as blind_cli
from scripts import validate_holdout_human_annotations as vha
from tests.helpers import real_filing_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parent.parent

COMMITTED_MANIFEST = rfh.load_holdout_manifest()

# A deterministic stand-in for the metadata-only selection-freeze manifest
# hash: the synthetic chain starts here, exactly one step before the
# source-verified hash the blind run records as its prior.
_SELECTION_FREEZE_HASH = "f" * 64

REVIEWER = "synthetic-reviewer-01"


# --- Synthetic workspace fixture ----------------------------------------------


def _synthetic_manifest() -> tuple[dict, dict[tuple[str, str], str]]:
    """The committed manifest re-anchored to synthetic fictional bodies, with
    the frozen ambiguous pair ambiguous on BOTH sides so the synthetic
    inventory reproduces the committed 9-written / 1-blocked shape."""
    document = copy.deepcopy(COMMITTED_MANIFEST)
    document["status"] = rfb.STATUS_SOURCE_VERIFIED
    contents: dict[tuple[str, str], str] = {}
    for pair in document["pairs"]:
        for side in ("previous", "current"):
            if pair["pair_id"] == vha.EXTRACTION_AMBIGUOUS_PAIR_ID:
                # Ambiguous on BOTH sides, but not byte-identical: identical
                # bytes under a second name are a registry `duplicate`, which
                # is a different outcome than the committed ambiguous pair.
                html = fx.AMBIGUOUS_SECTION_HTML + (
                    "<!-- current-side variant -->" if side == "current" else ""
                )
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


def _write_template(layout: rfb.CorpusLayout, pair_id: str) -> None:
    """The local human-completion template: bindings preserved, every decision
    field null. Mirrors the local preparation artifact exactly."""
    proposal = json.loads(
        layout.machine_proposed_path(pair_id).read_text(encoding="utf-8")
    )
    template = {
        "schema_version": proposal["schema_version"],
        "annotation_protocol_version": proposal["annotation_protocol_version"],
        "benchmark_id": proposal["benchmark_id"],
        "pair_id": pair_id,
        "annotation_status": None,
        "annotator_id": None,
        "verification_timestamp": None,
        "source_manifest_hash": proposal["source_manifest_hash"],
        "previous_section_hash": proposal["previous_section_hash"],
        "current_section_hash": proposal["current_section_hash"],
        "generated_by": "holdout_human_completion_template.v1",
        "labels": [
            {
                "label_id": label["label_id"],
                "expected_change_type": None,
                "previous_unit_id": label["previous_unit_id"],
                "current_unit_id": label["current_unit_id"],
                "expected_reason_code": None,
                "expected_evidence_side": None,
                "expected_direction": None,
                "reviewer_note": None,
                "confidence": None,
            }
            for label in proposal["labels"]
        ],
    }
    rfb.write_json_atomic(layout.annotation_path(pair_id), template)


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    """One blind-pipeline run over the synthetic corpus plus the synthesized
    chain reports and empty templates: a complete review workspace in the
    exact state the validator's --workspace mode must accept."""
    root = tmp_path_factory.mktemp("holdout_validation")
    document, contents = _synthetic_manifest()
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
    )
    layout = rfb.CorpusLayout(root / "corpus")
    for pair in document["pairs"]:
        for side in ("previous", "current"):
            target = layout.source_file(
                pair["pair_id"], side, pair[side]["primary_document"]
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents[(pair["pair_id"], side)], encoding="utf-8")

    # One advancing blind run. Build artifacts therefore bind the
    # source-verified PRIOR manifest hash (the snapshot the build ran under),
    # which the validator accepts because it is a hash in the verified chain;
    # the committed real corpus binds the corpus_built hash instead because
    # its reports were regenerated after the advance.
    assert (
        blind_cli.main(
            [
                "--manifest", str(manifest_path),
                "--corpus-dir", str(root / "corpus"),
                "--report-dir", str(root / "reports"),
            ]
        )
        == 0
    )

    report_dir = root / "reports"
    blind = json.loads(
        (report_dir / "blind_extraction_report.json").read_text(encoding="utf-8")
    )
    frozen_parser = document["frozen_parser_source_sha256"]
    rfb.write_json_atomic(
        report_dir / "source_verification_report.json",
        {
            "prior_manifest_sha256": _SELECTION_FREEZE_HASH,
            "new_manifest_sha256": blind["prior_manifest_sha256"],
            "frozen_parser_source_sha256": frozen_parser,
        },
    )
    rfb.write_json_atomic(
        report_dir / "selection_report.json",
        {
            "holdout_manifest_sha256": _SELECTION_FREEZE_HASH,
            "frozen_parser_source_sha256": frozen_parser,
        },
    )

    inventory = json.loads(
        (report_dir / "annotation_packet_inventory.json").read_text(encoding="utf-8")
    )
    written = sorted(
        row["pair_id"]
        for row in inventory["pairs"]
        if row["packet_status"] == "written"
    )
    # The synthetic corpus reproduces the frozen review contract exactly.
    assert written == sorted(vha.REVIEW_READY_PAIR_IDS)
    for pair_id in written:
        _write_template(layout, pair_id)

    return {
        "root": root,
        "inventory": inventory,
        "section_sample": next(
            layout.build_dir(written[0]).glob("previous_item_1a.txt")
        ).read_text(encoding="utf-8"),
    }


@pytest.fixture
def clone(workspace, tmp_path):
    """A disposable copy of the synthetic workspace for tamper tests."""
    root = tmp_path / "ws"
    shutil.copytree(workspace["root"], root)
    return root


def _validate(root: Path, *, completed: bool) -> vha._Findings:
    return vha.run_validation(
        layout=rfb.CorpusLayout(root / "corpus"),
        manifest_path=root / "manifest.json",
        report_dir=root / "reports",
        require_completed=completed,
    )


def _cli(root: Path, *args: str) -> int:
    return vha.main(
        [
            "--manifest", str(root / "manifest.json"),
            "--corpus-dir", str(root / "corpus"),
            "--report-dir", str(root / "reports"),
            *args,
        ]
    )


def _failed_codes(findings: vha._Findings) -> set[str]:
    return {row["code"] for row in findings.failed}


def _edit_json(path: Path, mutate) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def _later_than(timestamp: str, hours: int = 1) -> str:
    return (
        vha._parse_aware(timestamp) + timedelta(hours=hours)
    ).isoformat()


def _complete_pair(root: Path, inventory: dict, pair_id: str, **overrides) -> dict:
    """A valid human-verified completion derived from the pair's ALIGNMENT
    structure (unit bindings), with change types chosen by shape — never
    copied from the machine proposal's decisions."""
    layout = rfb.CorpusLayout(root / "corpus")
    proposal = json.loads(
        layout.machine_proposed_path(pair_id).read_text(encoding="utf-8")
    )
    labels = []
    for label in proposal["labels"]:
        previous, current = label["previous_unit_id"], label["current_unit_id"]
        if previous and current:
            change, side = "unchanged", "both"
        elif previous:
            change, side = "removed", "previous"
        else:
            change, side = "added", "current"
        labels.append(
            {
                "label_id": rfb.label_id_for(pair_id, previous, current),
                "expected_change_type": change,
                "previous_unit_id": previous,
                "current_unit_id": current,
                "expected_reason_code": None,
                "expected_evidence_side": side,
                "expected_direction": None,
                "reviewer_note": None,
                "confidence": "high",
            }
        )
    document = {
        "schema_version": rfb.ANNOTATION_SCHEMA_VERSION,
        "annotation_protocol_version": rfb.ANNOTATION_PROTOCOL_VERSION,
        "benchmark_id": vha.HOLDOUT_BENCHMARK_ID,
        "pair_id": pair_id,
        "annotation_status": rfb.ANNOTATION_HUMAN_VERIFIED,
        "annotator_id": REVIEWER,
        "verification_timestamp": _later_than(inventory["generated_at"]),
        "source_manifest_hash": proposal["source_manifest_hash"],
        "previous_section_hash": proposal["previous_section_hash"],
        "current_section_hash": proposal["current_section_hash"],
        "labels": labels,
    }
    document.update(overrides)
    rfb.write_json_atomic(layout.annotation_path(pair_id), document)
    return document


def _complete_all(root: Path, inventory: dict) -> None:
    for pair_id in vha.REVIEW_READY_PAIR_IDS:
        _complete_pair(root, inventory, pair_id)


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): rfb.sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- Workspace and identity (tests 1-13) --------------------------------------


def test_synthetic_workspace_passes_workspace_mode(workspace):
    findings = _validate(workspace["root"], completed=False)
    assert findings.failed == []


def test_all_nine_review_ready_pairs_required(clone):
    inventory_path = clone / "reports" / "annotation_packet_inventory.json"

    def drop_one(document):
        document["pairs"] = [
            row
            for row in document["pairs"]
            if row["pair_id"] != "sic-3000s-01"
        ]

    _edit_json(inventory_path, drop_one)
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_review_ready_set_drifted" in codes


def test_blocked_pair_must_have_no_annotation_file(clone, workspace):
    layout = rfb.CorpusLayout(clone / "corpus")
    forbidden = layout.annotation_path(vha.EXTRACTION_AMBIGUOUS_PAIR_ID)
    forbidden.write_text("{}", encoding="utf-8")
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_ambiguous_pair_annotation_forbidden" in codes
    assert "holdout_unexpected_annotation_file" in codes


def test_unknown_pair_rejected(clone):
    inventory_path = clone / "reports" / "annotation_packet_inventory.json"

    def add_unknown(document):
        impostor = copy.deepcopy(document["pairs"][0])
        impostor["pair_id"] = "sic-9000s-99"
        document["pairs"].append(impostor)

    _edit_json(inventory_path, add_unknown)
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_inventory_unknown_pair" in codes
    assert "holdout_review_ready_set_drifted" in codes


def test_missing_pair_rejected(clone):
    inventory_path = clone / "reports" / "annotation_packet_inventory.json"

    def drop_blocked(document):
        document["pairs"] = [
            row
            for row in document["pairs"]
            if row["pair_id"] != vha.EXTRACTION_AMBIGUOUS_PAIR_ID
        ]

    _edit_json(inventory_path, drop_blocked)
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_ambiguous_set_drifted" in codes


def test_duplicate_pair_rejected(clone):
    inventory_path = clone / "reports" / "annotation_packet_inventory.json"
    _edit_json(
        inventory_path,
        lambda document: document["pairs"].append(
            copy.deepcopy(document["pairs"][0])
        ),
    )
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_inventory_duplicate_pair" in codes


def test_packet_hash_drift_rejected(clone):
    packet_path = (
        rfb.CorpusLayout(clone / "corpus").packet_json_path("sic-2000s-01")
    )
    _edit_json(
        packet_path,
        lambda document: document.update(issuer_name="Tampered Issuer"),
    )
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_packet_hash_drift" in codes


def test_source_hash_drift_rejected(clone):
    manifest = json.loads((clone / "manifest.json").read_text(encoding="utf-8"))
    pair = next(
        item for item in manifest["pairs"] if item["pair_id"] == "sic-2000s-01"
    )
    source = rfb.CorpusLayout(clone / "corpus").source_file(
        "sic-2000s-01", "previous", pair["previous"]["primary_document"]
    )
    source.write_text(
        source.read_text(encoding="utf-8") + "<!-- tampered -->", encoding="utf-8"
    )
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_source_checksum_drift" in codes


def test_section_hash_drift_rejected(clone):
    text_path = rfb.CorpusLayout(clone / "corpus").section_text_path(
        "sic-2000s-01", "previous"
    )
    text_path.write_text(
        text_path.read_text(encoding="utf-8") + " tampered", encoding="utf-8"
    )
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_section_hash_drift" in codes


def test_result_hash_drift_rejected(clone):
    result_path = (
        rfb.CorpusLayout(clone / "corpus").build_dir("sic-2000s-01")
        / "detection_result.json"
    )
    _edit_json(
        result_path,
        lambda document: document.update(section_scope="tampered_scope"),
    )
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_result_hash_drift" in codes


def test_parser_source_drift_rejected(clone, monkeypatch):
    monkeypatch.setattr(
        vha.rfh, "frozen_parser_source_sha256", lambda repo_root=None: "0" * 64
    )
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_parser_source_drift" in codes


def test_parser_identity_drift_rejected(clone):
    packet_path = (
        rfb.CorpusLayout(clone / "corpus").packet_json_path("sic-2000s-01")
    )
    _edit_json(
        packet_path,
        lambda document: document["parser_versions"].update(
            html_parser="sec_html_item_headings.v99"
        ),
    )
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_parser_identity_drift" in codes


def test_detector_identity_drift_rejected(clone):
    _edit_json(
        clone / "reports" / "execution_report.json",
        lambda document: document.update(detector_version="item1a_detector.v99"),
    )
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_detector_identity_drift" in codes


def test_workflow_identity_drift_rejected(clone):
    _edit_json(
        clone / "reports" / "execution_report.json",
        lambda document: document.update(
            workflow_version="comparison_workflow.v99"
        ),
    )
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_workflow_identity_drift" in codes


def test_manifest_chain_drift_rejected(clone):
    _edit_json(
        clone / "reports" / "source_verification_report.json",
        lambda document: document.update(new_manifest_sha256="1" * 64),
    )
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_manifest_chain_broken" in codes


def test_manifest_rewrite_rejected_by_inventory_hash(clone):
    # Byte-identical content is the contract: even a reformat is drift.
    manifest_path = clone / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(
        json.dumps(document, indent=4, sort_keys=True), encoding="utf-8"
    )
    codes = _failed_codes(_validate(clone, completed=False))
    assert "holdout_manifest_hash_mismatch" in codes


# --- Human completion (tests 14-23) -------------------------------------------


def test_completed_mode_passes_with_all_nine_verified(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    findings = _validate(clone, completed=True)
    assert findings.failed == []
    assert _cli(clone) == 0


def test_machine_proposed_status_rejected_in_completed_mode(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    layout = rfb.CorpusLayout(clone / "corpus")
    proposal = json.loads(
        layout.machine_proposed_path("sic-2000s-01").read_text(encoding="utf-8")
    )
    rfb.write_json_atomic(layout.annotation_path("sic-2000s-01"), proposal)
    codes = _failed_codes(_validate(clone, completed=True))
    assert codes == {"holdout_machine_proposed_status_remains"}


def test_null_status_rejected_in_completed_mode(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _write_template(rfb.CorpusLayout(clone / "corpus"), "sic-2000s-01")
    codes = _failed_codes(_validate(clone, completed=True))
    assert codes == {"holdout_annotation_not_completed"}


def test_empty_annotator_id_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _complete_pair(clone, workspace["inventory"], "sic-2000s-01", annotator_id="  ")
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_annotation_schema:annotation_annotator_invalid_text" in codes


def test_overlong_annotator_id_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _complete_pair(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        annotator_id="x" * (rfb.MAX_ANNOTATOR_ID_CHARS + 1),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_annotation_schema:annotation_annotator_text_too_long" in codes


def test_naive_timestamp_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _complete_pair(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        verification_timestamp="2026-08-02T12:00:00",
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_annotation_schema:annotation_naive_timestamp" in codes


def test_explicit_non_utc_offset_string_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _complete_pair(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        verification_timestamp="2099-01-01T12:00:00+05:30",
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_timestamp_not_utc" in codes


def test_timestamp_before_packet_generation_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _complete_pair(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        verification_timestamp="2001-01-01T00:00:00+00:00",
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_timestamp_precedes_packets" in codes


# --- Label schema (tests 24-38) -----------------------------------------------


def _tamper_first_label(root, inventory, pair_id, mutate):
    document = _complete_pair(root, inventory, pair_id)
    mutate(document["labels"][0], document)
    rfb.write_json_atomic(
        rfb.CorpusLayout(root / "corpus").annotation_path(pair_id), document
    )


def test_unknown_label_keys_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _tamper_first_label(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        lambda label, _doc: label.update(surprise_field="x"),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert any(code.startswith("holdout_annotation_schema:") for code in codes)


def test_unknown_unit_ids_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])

    def swap_unit(label, _doc):
        side = "previous_unit_id" if label["previous_unit_id"] else "current_unit_id"
        label[side] = f"{side.split('_')[0]}:099:phantom-risk"
        label["label_id"] = rfb.label_id_for(
            "sic-2000s-01", label["previous_unit_id"], label["current_unit_id"]
        )

    _tamper_first_label(clone, workspace["inventory"], "sic-2000s-01", swap_unit)
    codes = _failed_codes(_validate(clone, completed=True))
    assert (
        "holdout_annotation_build_binding:annotation_unknown_unit_reference"
        in codes
    )


def test_duplicate_unit_tuple_rejected_by_schema(clone, workspace):
    _complete_all(clone, workspace["inventory"])

    def duplicate_tuple(label, document):
        twin = copy.deepcopy(label)
        document["labels"].append(twin)

    _tamper_first_label(
        clone, workspace["inventory"], "sic-2000s-01", duplicate_tuple
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert any(
        code
        in (
            "holdout_annotation_schema:annotation_duplicate_label_id",
            "holdout_annotation_schema:annotation_duplicate_label_units",
        )
        for code in codes
    )


def test_duplicate_label_ids_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    document = _complete_pair(clone, workspace["inventory"], "sic-2000s-01")
    if len(document["labels"]) < 2:
        pytest.skip("pair has a single label; duplicate-id case needs two")
    document["labels"][1]["label_id"] = document["labels"][0]["label_id"]
    rfb.write_json_atomic(
        rfb.CorpusLayout(clone / "corpus").annotation_path("sic-2000s-01"),
        document,
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_annotation_schema:annotation_duplicate_label_id" in codes


def test_missing_label_id_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _tamper_first_label(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        lambda label, _doc: label.update(label_id=""),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_annotation_schema:annotation_label_invalid_id" in codes


def test_non_canonical_label_id_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _tamper_first_label(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        lambda label, _doc: label.update(label_id="lbl-000000000000"),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_label_id_not_canonical" in codes


def test_invalid_expected_change_type_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _tamper_first_label(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        lambda label, _doc: label.update(expected_change_type="rewritten"),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert (
        "holdout_annotation_schema:annotation_label_invalid_change_type" in codes
    )


def test_invalid_expected_evidence_side_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _tamper_first_label(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        lambda label, _doc: label.update(expected_evidence_side="sideways"),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert (
        "holdout_annotation_schema:annotation_label_invalid_evidence_side"
        in codes
    )


def test_expected_direction_nullable_and_closed(clone, workspace):
    # Null direction is the existing contract (accepted in the all-valid
    # corpus); a value outside the closed set is rejected.
    _complete_all(clone, workspace["inventory"])
    assert _validate(clone, completed=True).failed == []
    _tamper_first_label(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        lambda label, _doc: label.update(expected_direction="sideways"),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_annotation_schema:annotation_label_invalid_direction" in codes


def test_reason_code_only_for_undetermined(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _tamper_first_label(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        lambda label, _doc: label.update(expected_reason_code="stale"),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert (
        "holdout_annotation_schema:annotation_label_unexpected_reason_code"
        in codes
    )


def test_invalid_confidence_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _tamper_first_label(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        lambda label, _doc: label.update(confidence="certain"),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_annotation_schema:annotation_label_invalid_confidence" in codes


def test_reviewer_note_optional_and_bounded(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _tamper_first_label(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        lambda label, _doc: label.update(
            reviewer_note="n" * (rfb.MAX_REVIEWER_NOTE_CHARS + 1)
        ),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_annotation_schema:annotation_label_note_too_long" in codes


def test_copied_filing_excerpt_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    packet = json.loads(
        rfb.CorpusLayout(clone / "corpus")
        .packet_json_path("sic-2000s-01")
        .read_text(encoding="utf-8")
    )
    excerpt = next(
        entry[f"{side}_excerpt"]
        for entry in packet["alignments"]
        for side in ("previous", "current")
        if entry.get(f"{side}_excerpt")
        and len(entry[f"{side}_excerpt"]) >= vha.EXCERPT_COPY_WINDOW_CHARS
    )
    pasted = "I checked this: " + excerpt[: vha.EXCERPT_COPY_WINDOW_CHARS + 15]
    _tamper_first_label(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        lambda label, _doc: label.update(reviewer_note=pasted),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_note_copies_excerpt" in codes


def test_absolute_path_in_note_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _tamper_first_label(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        lambda label, _doc: label.update(
            reviewer_note="see /Users/someone/Desktop/notes.txt"
        ),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_annotation_sensitive_material" in codes


def test_credential_material_in_note_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _tamper_first_label(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        lambda label, _doc: label.update(
            reviewer_note="key AKIAABCDEFGHIJKLMNOP was used"
        ),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_annotation_sensitive_material" in codes


def test_environment_assignment_in_note_rejected(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    _tamper_first_label(
        clone,
        workspace["inventory"],
        "sic-2000s-01",
        lambda label, _doc: label.update(
            reviewer_note="ran with TAVILY_API_KEY=abc123"
        ),
    )
    codes = _failed_codes(_validate(clone, completed=True))
    assert "holdout_annotation_sensitive_material" in codes


# --- Inventory closure over synthetic units (tests 39-51) ---------------------

_PREV_HASH = "a" * 64
_CUR_HASH = "b" * 64
_MANIFEST_HASH = "c" * 64
_GENERATED_AT = "2026-08-01T00:00:00+00:00"
_CLOSURE_PAIR = "sic-5000s-01"


def _closure_label(previous, current, change, *, side=None, reason=None):
    if side is None:
        side = "both" if previous and current else (
            "previous" if change == "removed" else
            "current" if change == "added" else "none"
        )
    return {
        "label_id": rfb.label_id_for(_CLOSURE_PAIR, previous, current),
        "expected_change_type": change,
        "previous_unit_id": previous,
        "current_unit_id": current,
        "expected_reason_code": reason,
        "expected_evidence_side": side,
        "expected_direction": None,
        "reviewer_note": None,
        "confidence": "high",
    }


def _closure_findings(tmp_path, labels, units_previous, units_current):
    """Run the completed-annotation checks over a hand-built synthetic build
    record — no pipeline, no real packet, no real units."""
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    record = {
        "pair_id": _CLOSURE_PAIR,
        "previous": {
            "section_hash": _PREV_HASH,
            "units": [{"unit_id": unit} for unit in units_previous],
        },
        "current": {
            "section_hash": _CUR_HASH,
            "units": [{"unit_id": unit} for unit in units_current],
        },
    }
    row = {
        "pair_id": _CLOSURE_PAIR,
        "previous_section_hash": _PREV_HASH,
        "current_section_hash": _CUR_HASH,
    }
    inventory = {"manifest_sha256": _MANIFEST_HASH, "generated_at": _GENERATED_AT}
    document = {
        "schema_version": rfb.ANNOTATION_SCHEMA_VERSION,
        "annotation_protocol_version": rfb.ANNOTATION_PROTOCOL_VERSION,
        "benchmark_id": vha.HOLDOUT_BENCHMARK_ID,
        "pair_id": _CLOSURE_PAIR,
        "annotation_status": rfb.ANNOTATION_HUMAN_VERIFIED,
        "annotator_id": REVIEWER,
        "verification_timestamp": "2026-08-01T01:00:00+00:00",
        "source_manifest_hash": _MANIFEST_HASH,
        "previous_section_hash": _PREV_HASH,
        "current_section_hash": _CUR_HASH,
        "labels": labels,
    }
    rfb.write_json_atomic(layout.annotation_path(_CLOSURE_PAIR), document)
    findings = vha._Findings()
    vha._check_completed_annotation(
        findings,
        row=row,
        layout=layout,
        inventory=inventory,
        packet={},
        record=record,
        require_completed=True,
    )
    return findings


def test_every_unit_covered_passes(tmp_path):
    findings = _closure_findings(
        tmp_path,
        labels=[
            _closure_label(
                "previous:000:alpha-risk", "current:000:alpha-risk", "unchanged"
            ),
            _closure_label(
                "previous:001:beta-risk", "current:001:beta-risk", "modified"
            ),
        ],
        units_previous=["previous:000:alpha-risk", "previous:001:beta-risk"],
        units_current=["current:000:alpha-risk", "current:001:beta-risk"],
    )
    assert findings.failed == []


def test_dropped_previous_unit_fails_closure(tmp_path):
    findings = _closure_findings(
        tmp_path,
        labels=[
            _closure_label(
                "previous:000:alpha-risk", "current:000:alpha-risk", "unchanged"
            ),
        ],
        units_previous=["previous:000:alpha-risk", "previous:001:beta-risk"],
        units_current=["current:000:alpha-risk"],
    )
    failed = {row["code"]: row for row in findings.failed}
    assert "holdout_unit_uncovered" in failed
    assert "previous:001:beta-risk" in failed["holdout_unit_uncovered"]["detail"]


def test_dropped_current_unit_fails_closure(tmp_path):
    findings = _closure_findings(
        tmp_path,
        labels=[
            _closure_label(
                "previous:000:alpha-risk", "current:000:alpha-risk", "unchanged"
            ),
        ],
        units_previous=["previous:000:alpha-risk"],
        units_current=["current:000:alpha-risk", "current:001:beta-risk"],
    )
    failed = {row["code"]: row for row in findings.failed}
    assert "holdout_unit_uncovered" in failed
    assert "current:001:beta-risk" in failed["holdout_unit_uncovered"]["detail"]


def test_added_unit_coverage_valid(tmp_path):
    findings = _closure_findings(
        tmp_path,
        labels=[
            _closure_label(
                "previous:000:alpha-risk", "current:000:alpha-risk", "unchanged"
            ),
            _closure_label(None, "current:001:new-risk", "added"),
        ],
        units_previous=["previous:000:alpha-risk"],
        units_current=["current:000:alpha-risk", "current:001:new-risk"],
    )
    assert findings.failed == []


def test_removed_unit_coverage_valid(tmp_path):
    findings = _closure_findings(
        tmp_path,
        labels=[
            _closure_label(
                "previous:000:alpha-risk", "current:000:alpha-risk", "unchanged"
            ),
            _closure_label("previous:001:old-risk", None, "removed"),
        ],
        units_previous=["previous:000:alpha-risk", "previous:001:old-risk"],
        units_current=["current:000:alpha-risk"],
    )
    assert findings.failed == []


def test_modified_pair_coverage_valid(tmp_path):
    findings = _closure_findings(
        tmp_path,
        labels=[
            _closure_label(
                "previous:000:alpha-risk", "current:000:alpha-risk", "modified"
            ),
        ],
        units_previous=["previous:000:alpha-risk"],
        units_current=["current:000:alpha-risk"],
    )
    assert findings.failed == []


def test_undetermined_coverage_valid(tmp_path):
    findings = _closure_findings(
        tmp_path,
        labels=[
            _closure_label(
                "previous:000:alpha-risk",
                "current:000:alpha-risk",
                "unchanged",
            ),
            _closure_label(
                "previous:001:strange-risk",
                None,
                "undetermined",
                side="none",
                reason="alignment_ambiguous",
            ),
        ],
        units_previous=["previous:000:alpha-risk", "previous:001:strange-risk"],
        units_current=["current:000:alpha-risk"],
    )
    assert findings.failed == []


def test_multiply_covered_unit_rejected(tmp_path):
    findings = _closure_findings(
        tmp_path,
        labels=[
            _closure_label(
                "previous:000:alpha-risk", "current:000:alpha-risk", "modified"
            ),
            _closure_label("previous:000:alpha-risk", None, "removed"),
            _closure_label(None, "current:001:beta-risk", "added"),
        ],
        units_previous=["previous:000:alpha-risk"],
        units_current=["current:000:alpha-risk", "current:001:beta-risk"],
    )
    codes = _failed_codes(findings)
    assert "holdout_unit_multiply_covered" in codes


_REPEATED_PREVIOUS = [
    "previous:001:business-risks",
    "previous:002:business-risks",
    "previous:007:general-risks",
    "previous:008:general-risks",
]
_REPEATED_CURRENT = [
    "current:001:business-risks",
    "current:002:business-risks",
    "current:007:general-risks",
    "current:008:general-risks",
]


def test_repeated_headings_reject_key_only_collapse(tmp_path):
    """Covering one unit per normalized heading key is NOT closure: the
    duplicate business-risks and general-risks unit ids each need their own
    label, exactly the sic-5000s-01 shape."""
    findings = _closure_findings(
        tmp_path,
        labels=[
            _closure_label(
                "previous:001:business-risks",
                "current:001:business-risks",
                "unchanged",
            ),
            _closure_label(
                "previous:007:general-risks",
                "current:007:general-risks",
                "unchanged",
            ),
        ],
        units_previous=_REPEATED_PREVIOUS,
        units_current=_REPEATED_CURRENT,
    )
    failed = {row["code"]: row for row in findings.failed}
    assert "holdout_unit_uncovered" in failed
    detail = failed["holdout_unit_uncovered"]["detail"]
    assert "previous:002:business-risks" in detail
    assert "previous:008:general-risks" in detail


def test_repeated_business_risks_units_need_explicit_ids(tmp_path):
    findings = _closure_findings(
        tmp_path,
        labels=[
            _closure_label(
                "previous:001:business-risks",
                "current:001:business-risks",
                "unchanged",
            ),
            _closure_label(
                "previous:007:general-risks",
                "current:007:general-risks",
                "unchanged",
            ),
            _closure_label(
                "previous:008:general-risks",
                "current:008:general-risks",
                "unchanged",
            ),
        ],
        units_previous=_REPEATED_PREVIOUS,
        units_current=_REPEATED_CURRENT,
    )
    failed = {row["code"]: row for row in findings.failed}
    detail = failed["holdout_unit_uncovered"]["detail"]
    assert "previous:002:business-risks" in detail
    assert "current:002:business-risks" in detail


def test_repeated_general_risks_units_need_explicit_ids(tmp_path):
    findings = _closure_findings(
        tmp_path,
        labels=[
            _closure_label(
                "previous:001:business-risks",
                "current:001:business-risks",
                "unchanged",
            ),
            _closure_label(
                "previous:002:business-risks",
                "current:002:business-risks",
                "unchanged",
            ),
            _closure_label(
                "previous:007:general-risks",
                "current:007:general-risks",
                "unchanged",
            ),
        ],
        units_previous=_REPEATED_PREVIOUS,
        units_current=_REPEATED_CURRENT,
    )
    failed = {row["code"]: row for row in findings.failed}
    detail = failed["holdout_unit_uncovered"]["detail"]
    assert "previous:008:general-risks" in detail
    assert "current:008:general-risks" in detail


def test_complete_repeated_heading_annotation_passes(tmp_path):
    findings = _closure_findings(
        tmp_path,
        labels=[
            _closure_label(
                "previous:001:business-risks",
                "current:001:business-risks",
                "unchanged",
            ),
            _closure_label(
                "previous:002:business-risks",
                "current:002:business-risks",
                "modified",
            ),
            _closure_label(
                "previous:007:general-risks",
                "current:007:general-risks",
                "unchanged",
            ),
            _closure_label(
                "previous:008:general-risks",
                "current:008:general-risks",
                "modified",
            ),
        ],
        units_previous=_REPEATED_PREVIOUS,
        units_current=_REPEATED_CURRENT,
    )
    assert findings.failed == []


# --- Safety and behavior (tests 52-61) ----------------------------------------


def test_both_modes_are_read_only(clone):
    before = _tree_digest(clone)
    _validate(clone, completed=False)
    _validate(clone, completed=True)
    assert _tree_digest(clone) == before


def test_cli_modes_are_read_only_and_exit_codes_hold(clone):
    before = _tree_digest(clone)
    assert _cli(clone, "--workspace") == 0
    assert _cli(clone) == 1  # templates present, nothing verified
    assert _tree_digest(clone) == before


def test_no_annotation_generated_automatically(clone):
    layout = rfb.CorpusLayout(clone / "corpus")
    annotations = sorted(
        path.name for path in layout.annotations_dir().iterdir() if path.is_file()
    )
    _validate(clone, completed=True)
    after = sorted(
        path.name for path in layout.annotations_dir().iterdir() if path.is_file()
    )
    assert after == annotations
    # Exactly nine proposals and nine templates; nothing else appears.
    assert len(after) == 18


def test_templates_carry_no_machine_decisions(workspace):
    layout = rfb.CorpusLayout(workspace["root"] / "corpus")
    for pair_id in vha.REVIEW_READY_PAIR_IDS:
        template = json.loads(
            layout.annotation_path(pair_id).read_text(encoding="utf-8")
        )
        assert template["annotation_status"] is None
        assert template["annotator_id"] is None
        assert template["verification_timestamp"] is None
        for label in template["labels"]:
            for field in vha.LABEL_DECISION_FIELDS:
                assert label[field] is None, (pair_id, field)


def test_findings_are_deterministic(clone):
    first = _validate(clone, completed=True).rows
    second = _validate(clone, completed=True).rows
    assert first == second


def test_incomplete_corpus_reports_exactly_nine_findings(clone):
    findings = _validate(clone, completed=True)
    failed = findings.failed
    assert len(failed) == 9
    assert {row["code"] for row in failed} == {"holdout_annotation_not_completed"}
    assert sorted(row["pair_id"] for row in failed) == sorted(
        vha.REVIEW_READY_PAIR_IDS
    )


def test_output_is_bounded_and_leaks_no_content_or_paths(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    rows = _validate(clone, completed=True).rows + _validate(
        clone, completed=False
    ).rows
    section_fragment = vha._normalized(workspace["section_sample"])[:60]
    for row in rows:
        assert len(row["detail"]) < 500
        assert str(clone) not in row["detail"]
        assert section_fragment not in vha._normalized(row["detail"])


def test_blocked_pair_contributes_to_no_completion_count(clone, workspace):
    _complete_all(clone, workspace["inventory"])
    findings = _validate(clone, completed=True)
    ambiguous_rows = [
        row
        for row in findings.rows
        if row["pair_id"] == vha.EXTRACTION_AMBIGUOUS_PAIR_ID
    ]
    assert {row["check"] for row in ambiguous_rows} == {
        "ambiguous_pair_blocked_in_inventory",
        "ambiguous_pair_has_no_annotation_surface",
    }
    assert all(row["ok"] for row in ambiguous_rows)
    # Never counted as annotation-missing, detector-correct, or unchanged:
    # no completion, closure, or label check ever names the blocked pair.
    completion_checks = [
        row
        for row in findings.rows
        if row["pair_id"] == vha.EXTRACTION_AMBIGUOUS_PAIR_ID
        and row["check"]
        not in (
            "ambiguous_pair_blocked_in_inventory",
            "ambiguous_pair_has_no_annotation_surface",
        )
    ]
    assert completion_checks == []


def test_internal_errors_never_leak_raw_exception_text(clone, monkeypatch, capsys):
    def explode(**_kwargs):
        raise RuntimeError("secret local detail /Users/someone/private")

    monkeypatch.setattr(vha, "run_validation", explode)
    assert _cli(clone) == 2
    err = capsys.readouterr().err
    assert "holdout_validator_internal_error" in err
    assert "RuntimeError" in err
    assert "secret local detail" not in err
    assert "/Users/" not in err


# --- CI wiring ----------------------------------------------------------------


def test_required_check_runs_this_suite():
    """Self-pin: the merge-blocking workflow must run this suite, or the
    validator contract cannot block a regression."""
    import yaml

    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml")
        .read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["comparison-regression"]
    combined = "\n".join(
        step["run"]
        for step in job["steps"]
        if isinstance(step.get("run"), str) and "pytest" in step["run"]
    )
    assert "tests/test_holdout_human_annotation_validation.py" in combined


def test_validator_module_imports_no_network_client():
    import ast

    source = (
        REPO_ROOT / "scripts" / "validate_holdout_human_annotations.py"
    ).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"urllib", "http", "socket", "requests", "boto3", "chromadb"}
    assert imported & forbidden == set()
