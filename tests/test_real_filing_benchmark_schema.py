"""Manifest, annotation, and status-ladder schema tests.

Entirely offline and fixture-driven: every issuer, CIK, accession, and document
in this file is synthetic. No network, no AWS, no LLM, no real filing content.

The suite's centre of gravity is the machine/human boundary. A benchmark that
lets a machine-generated label drift into a gold denominator produces numbers
that look like evidence and are not, so the tests that pin that boundary are
the ones that matter most here.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import real_filing_benchmark as rfb
from tests.helpers import real_filing_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- Committed manifest -------------------------------------------------------


def test_committed_manifest_validates_and_is_honestly_proposed():
    document = rfb.load_manifest()
    assert document["status"] in rfb.STATUS_ORDER
    assert document["target_pair_count"] == 10
    assert len(document["proposed_issuers"]) == 10

    # The status must match what the file actually carries. Asserted both ways
    # so the manifest cannot claim a maturity its contents do not support, and
    # cannot silently carry unverified remote metadata either.
    if document["status"] == rfb.STATUS_PROPOSED:
        # Nothing resolved yet: the manifest says so rather than carrying
        # invented values.
        assert document["pairs"] == []
        for entry in document["proposed_issuers"]:
            assert entry["resolution_status"] == rfb.ISSUER_PENDING
            assert entry["cik"] is None
    else:
        # source_verified and beyond assert that every filing was resolved from
        # an official source and verified against a real digest.
        assert len(document["pairs"]) == document["target_pair_count"]
        for entry in document["proposed_issuers"]:
            assert entry["resolution_status"] == rfb.ISSUER_RESOLVED
            assert isinstance(entry["cik"], str) and entry["cik"].isdigit()
        for pair in document["pairs"]:
            for _side, payload in rfb.pair_sides(pair):
                assert payload["form"] == rfb.MANIFEST_FORM
                assert payload["expected_sha256"] != rfb.PLACEHOLDER_SHA256
                # Official source only, derived rather than trusted.
                assert payload["official_source_url"] == rfb.canonical_source_url(
                    pair["cik"],
                    payload["accession_number"],
                    payload["primary_document"],
                )


def test_committed_manifest_slate_meets_the_frozen_selection_criteria():
    document = rfb.load_manifest()
    sectors = {entry["sector_label"] for entry in document["proposed_issuers"]}
    assert len(sectors) >= rfb.SELECTION_CRITERIA["minimum_sector_labels"]
    assert len({entry["issuer_name"] for entry in document["proposed_issuers"]}) == 10
    for entry in document["proposed_issuers"]:
        assert (
            entry["target_current_fiscal_year"]
            == entry["target_previous_fiscal_year"] + 1
        )


def test_committed_manifest_carries_remote_metadata_only_once_verified():
    """Remote metadata appears only when the status asserts it was verified.

    A `proposed` manifest must not carry an accession number or digest nobody
    checked; a `source_verified` one must not carry a placeholder standing in
    for a digest nobody computed. Both directions are the same rule: the file
    never asserts more than was actually established.
    """
    raw = (
        REPO_ROOT / "benchmarks" / "real_filing_v1" / "manifest.json"
    ).read_text(encoding="utf-8")
    document = json.loads(raw)
    for entry in document["proposed_issuers"]:
        assert set(entry) == set(rfb._ISSUER_REQUIRED)

    if document["status"] == rfb.STATUS_PROPOSED:
        assert document["pairs"] == []
        assert rfb.PLACEHOLDER_SHA256 not in raw or not document["pairs"]
        return

    assert document["pairs"], "a non-proposed manifest must carry resolved pairs"
    # No placeholder digest survives past 'proposed' anywhere in the file.
    assert rfb.PLACEHOLDER_SHA256 not in raw
    seen_accessions = set()
    for pair in document["pairs"]:
        assert set(pair) == set(rfb._PAIR_REQUIRED)
        for _side, payload in rfb.pair_sides(pair):
            assert set(payload) == set(rfb._SIDE_REQUIRED)
            key = (pair["cik"], payload["accession_number"])
            assert key not in seen_accessions
            seen_accessions.add(key)
    assert len(seen_accessions) == 2 * len(document["pairs"])


# --- Manifest schema ----------------------------------------------------------


def test_valid_synthetic_manifest_round_trips(tmp_path):
    document = fx.single_pair_manifest()
    path = fx.write_manifest(tmp_path, document)
    assert rfb.load_manifest(path)["pairs"][0]["pair_id"] == "pair-01"


def test_unknown_top_level_key_is_rejected():
    document = fx.single_pair_manifest()
    document["extra_field"] = "surprise"
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_unknown_keys"


def test_unknown_pair_key_is_rejected():
    document = fx.single_pair_manifest()
    document["pairs"][0]["confidence"] = "high"
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_pair_unknown_keys"


def test_unknown_side_key_is_rejected():
    document = fx.single_pair_manifest()
    document["pairs"][0]["previous"]["file_size"] = 1234
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_side_unknown_keys"


def test_missing_required_key_is_rejected():
    document = fx.single_pair_manifest()
    del document["frozen_at"]
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_missing_keys"


def test_duplicate_accession_for_the_same_cik_is_rejected():
    document = fx.single_pair_manifest()
    pair = document["pairs"][0]
    pair["current"]["accession_number"] = pair["previous"]["accession_number"]
    pair["current"]["official_source_url"] = rfb.canonical_source_url(
        pair["cik"],
        pair["current"]["accession_number"],
        pair["current"]["primary_document"],
    )
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_duplicate_accession"


def test_duplicate_accession_across_pairs_is_rejected():
    slate = fx.issuer_slate(count=2, resolved=2)
    first = fx.pair(pair_id="pair-01", slate_id="slate-01", cik="0000000001")
    second = fx.pair(pair_id="pair-02", slate_id="slate-02", cik="0000000001")
    document = fx.manifest(
        pairs=[first, second],
        slate=slate,
        target_pair_count=2,
        status=rfb.STATUS_SOURCE_VERIFIED,
    )
    # Both pairs claim CIK 0000000001 with identical accessions; the slate
    # mismatch surfaces first, so align identity and re-check the accession.
    document["proposed_issuers"][1]["cik"] = "0000000001"
    second["issuer_name"] = document["proposed_issuers"][1]["issuer_name"]
    second["sector_label"] = document["proposed_issuers"][1]["sector_label"]
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code in (
        "manifest_duplicate_accession",
        "manifest_issuer_duplicate_cik",
    )


def test_invalid_chronological_order_is_rejected():
    document = fx.single_pair_manifest()
    pair = document["pairs"][0]
    pair["previous"], pair["current"] = pair["current"], pair["previous"]
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_pair_filing_dates_unordered"


def test_reporting_period_after_filing_date_is_rejected():
    document = fx.single_pair_manifest()
    document["pairs"][0]["previous"]["reporting_period"] = "2023-01-01"
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_side_period_after_filing"


def test_pairs_must_be_deterministically_ordered():
    slate = fx.issuer_slate(count=2, resolved=2)
    first = fx.pair(pair_id="pair-01", slate_id="slate-01", cik="0000000001")
    second = fx.pair(
        pair_id="pair-02",
        slate_id="slate-02",
        cik="0000000002",
        issuer_name=slate[1]["issuer_name"],
        sector_label=slate[1]["sector_label"],
    )
    document = fx.manifest(
        pairs=[second, first],
        slate=slate,
        target_pair_count=2,
        status=rfb.STATUS_SOURCE_VERIFIED,
    )
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_pairs_unordered"


def test_amendment_form_is_rejected():
    document = fx.single_pair_manifest()
    document["pairs"][0]["current"]["form"] = "10-K/A"
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_side_invalid_form"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.sec.gov.evil.test/Archives/edgar/data/1/x/y.htm",
        "http://www.sec.gov/Archives/edgar/data/1/x/y.htm",
        "https://edgar-mirror.example.test/filings/y.htm",
    ],
)
def test_non_official_source_url_is_rejected(url):
    document = fx.single_pair_manifest()
    document["pairs"][0]["previous"]["official_source_url"] = url
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_side_non_official_url"


def test_primary_document_cannot_traverse_paths():
    document = fx.single_pair_manifest()
    pair = document["pairs"][0]
    pair["previous"]["primary_document"] = "../../etc/passwd"
    pair["previous"]["official_source_url"] = rfb.canonical_source_url(
        pair["cik"], pair["previous"]["accession_number"], "../../etc/passwd"
    )
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_side_invalid_primary_document"


def test_placeholder_hash_allowed_only_while_proposed():
    document = fx.single_pair_manifest(status=rfb.STATUS_PROPOSED)
    document["pairs"][0]["previous"]["expected_sha256"] = rfb.PLACEHOLDER_SHA256
    rfb.validate_manifest(document)  # proposed: acceptable

    document["status"] = rfb.STATUS_SOURCE_VERIFIED
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_side_placeholder_hash"


def test_empty_pairs_allowed_only_while_proposed():
    document = fx.manifest(pairs=[], status=rfb.STATUS_PROPOSED)
    rfb.validate_manifest(document)
    document["status"] = rfb.STATUS_CORPUS_BUILT
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_status_requires_pairs"


def test_pair_cannot_use_an_unresolved_slate_entry():
    slate = fx.issuer_slate(count=1, resolved=0)
    document = fx.manifest(
        pairs=[fx.pair()],
        slate=slate,
        target_pair_count=1,
        status=rfb.STATUS_SOURCE_VERIFIED,
    )
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_pair_unresolved_issuer"


def test_pair_cannot_change_issuer_identity_after_freezing():
    document = fx.single_pair_manifest()
    document["pairs"][0]["issuer_name"] = "A Different Fictional Issuer, Inc."
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_pair_slate_mismatch"


def test_two_pairs_cannot_share_one_slate_entry():
    slate = fx.issuer_slate(count=2, resolved=2)
    first = fx.pair(pair_id="pair-01", slate_id="slate-01")
    second = copy.deepcopy(first)
    second["pair_id"] = "pair-02"
    second["previous"]["accession_number"] = "0000000001-22-000009"
    second["previous"]["official_source_url"] = rfb.canonical_source_url(
        second["cik"], "0000000001-22-000009", second["previous"]["primary_document"]
    )
    second["current"]["accession_number"] = "0000000001-23-000009"
    second["current"]["official_source_url"] = rfb.canonical_source_url(
        second["cik"], "0000000001-23-000009", second["current"]["primary_document"]
    )
    document = fx.manifest(
        pairs=[first, second],
        slate=slate,
        target_pair_count=2,
        status=rfb.STATUS_SOURCE_VERIFIED,
    )
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_pair_duplicate_slate_id"


def test_slate_must_span_the_minimum_sector_count():
    slate = fx.issuer_slate(count=5)
    for entry in slate:
        entry["sector_label"] = "Fictional Sector A"
    document = fx.manifest(pairs=[], slate=slate)
    with pytest.raises(rfb.ManifestSchemaError) as excinfo:
        rfb.validate_manifest(document)
    assert excinfo.value.code == "manifest_insufficient_sector_coverage"


# --- Status ladder ------------------------------------------------------------


def test_status_advances_one_documented_step_at_a_time():
    rfb.validate_status_transition(rfb.STATUS_PROPOSED, rfb.STATUS_SOURCE_VERIFIED)
    rfb.validate_status_transition(
        rfb.STATUS_CORPUS_BUILT, rfb.STATUS_HUMAN_ANNOTATION_COMPLETE
    )


@pytest.mark.parametrize(
    ("current", "target", "code"),
    [
        (rfb.STATUS_PROPOSED, rfb.STATUS_CORPUS_BUILT, "status_skipped"),
        (rfb.STATUS_PROPOSED, rfb.STATUS_HUMAN_ANNOTATION_COMPLETE, "status_skipped"),
        (rfb.STATUS_CORPUS_BUILT, rfb.STATUS_PROPOSED, "status_regression"),
        (rfb.STATUS_PROPOSED, rfb.STATUS_PROPOSED, "status_unchanged"),
        (rfb.STATUS_PROPOSED, "shipped", "unknown_status"),
    ],
)
def test_illegal_status_transitions_are_rejected(current, target, code):
    with pytest.raises(rfb.StatusTransitionError) as excinfo:
        rfb.validate_status_transition(current, target)
    assert excinfo.value.code == code


# --- Annotation schema --------------------------------------------------------


def _machine_annotation(**overrides):
    labels = overrides.pop(
        "labels",
        [
            {
                "label_id": rfb.label_id_for(
                    "pair-01", "previous:000:cyber-risks", "current:000:cyber-risks"
                ),
                "expected_change_type": "modified",
                "previous_unit_id": "previous:000:cyber-risks",
                "current_unit_id": "current:000:cyber-risks",
                "expected_reason_code": None,
                "expected_evidence_side": "both",
                "expected_direction": None,
                "reviewer_note": None,
                "confidence": "low",
            }
        ],
    )
    document = rfb.machine_proposed_annotation(
        pair_id="pair-01",
        source_manifest_hash="a" * 64,
        previous_section_hash="b" * 64,
        current_section_hash="c" * 64,
        labels=labels,
        generated_by="test",
    )
    document.update(overrides)
    return document


def test_machine_proposed_annotation_is_never_gold():
    document = _machine_annotation()
    assert document["annotation_status"] == rfb.ANNOTATION_MACHINE_PROPOSED
    assert document["annotator_id"] is None
    assert document["verification_timestamp"] is None
    assert rfb.is_gold(document) is False


def test_machine_proposed_status_cannot_carry_a_human_identity():
    document = _machine_annotation()
    document["annotator_id"] = "reviewer@localhost"
    with pytest.raises(rfb.AnnotationSchemaError) as excinfo:
        rfb.validate_annotation(document)
    assert excinfo.value.code == "annotation_machine_status_with_annotator"


def test_machine_proposed_status_cannot_carry_a_verification_timestamp():
    document = _machine_annotation()
    document["verification_timestamp"] = "2026-02-01T12:00:00+00:00"
    with pytest.raises(rfb.AnnotationSchemaError) as excinfo:
        rfb.validate_annotation(document)
    assert excinfo.value.code == "annotation_machine_status_with_timestamp"


def test_human_verified_requires_an_explicit_annotator_identity():
    document = _machine_annotation()
    document["annotation_status"] = rfb.ANNOTATION_HUMAN_VERIFIED
    with pytest.raises(rfb.AnnotationSchemaError) as excinfo:
        rfb.validate_annotation(document)
    assert excinfo.value.code == "annotation_annotator_invalid_text"


def test_human_verified_requires_an_explicit_verification_timestamp():
    document = _machine_annotation()
    document["annotation_status"] = rfb.ANNOTATION_HUMAN_VERIFIED
    document["annotator_id"] = "reviewer@localhost"
    with pytest.raises(rfb.AnnotationSchemaError) as excinfo:
        rfb.validate_annotation(document)
    assert excinfo.value.code == "annotation_missing_verification_timestamp"


def test_human_verified_annotation_is_gold():
    document = fx.human_verify(_machine_annotation())
    assert rfb.is_gold(document) is True
    assert document["annotator_id"] == "reviewer@localhost"


def test_rejected_status_also_requires_a_human_identity():
    document = _machine_annotation()
    document["annotation_status"] = rfb.ANNOTATION_REJECTED
    with pytest.raises(rfb.AnnotationSchemaError):
        rfb.validate_annotation(document)
    document["annotator_id"] = "reviewer@localhost"
    document["verification_timestamp"] = "2026-02-01T12:00:00+00:00"
    rfb.validate_annotation(document)
    assert rfb.is_gold(document) is False


def test_unknown_annotation_field_is_rejected():
    document = _machine_annotation()
    document["reviewed_by_model"] = True
    with pytest.raises(rfb.AnnotationSchemaError) as excinfo:
        rfb.validate_annotation(document)
    assert excinfo.value.code == "annotation_unknown_keys"


def test_unknown_label_field_is_rejected():
    document = _machine_annotation()
    document["labels"][0]["severity"] = "high"
    with pytest.raises(rfb.AnnotationSchemaError) as excinfo:
        rfb.validate_annotation(document)
    assert excinfo.value.code == "annotation_label_unknown_keys"


def test_duplicate_label_id_is_rejected():
    label = {
        "label_id": "lbl-duplicate",
        "expected_change_type": "added",
        "previous_unit_id": None,
        "current_unit_id": "current:001:new-risks",
        "expected_reason_code": None,
        "expected_evidence_side": "current",
        "expected_direction": None,
        "reviewer_note": None,
        "confidence": "high",
    }
    other = dict(label, current_unit_id="current:002:other-risks")
    with pytest.raises(rfb.AnnotationSchemaError) as excinfo:
        _machine_annotation(labels=[label, other])
    assert excinfo.value.code == "annotation_duplicate_label_id"


def test_duplicate_unit_binding_is_rejected():
    label = {
        "label_id": "lbl-one",
        "expected_change_type": "added",
        "previous_unit_id": None,
        "current_unit_id": "current:001:new-risks",
        "expected_reason_code": None,
        "expected_evidence_side": "current",
        "expected_direction": None,
        "reviewer_note": None,
        "confidence": "high",
    }
    other = dict(label, label_id="lbl-two")
    with pytest.raises(rfb.AnnotationSchemaError) as excinfo:
        _machine_annotation(labels=[label, other])
    assert excinfo.value.code == "annotation_duplicate_label_units"


@pytest.mark.parametrize(
    ("change_type", "previous", "current"),
    [
        ("added", "previous:000:x", "current:000:x"),
        ("removed", None, "current:000:x"),
        ("modified", "previous:000:x", None),
        ("unchanged", None, "current:000:x"),
    ],
)
def test_label_shape_must_match_its_change_type(change_type, previous, current):
    label = {
        "label_id": "lbl-shape",
        "expected_change_type": change_type,
        "previous_unit_id": previous,
        "current_unit_id": current,
        "expected_reason_code": None,
        "expected_evidence_side": "both",
        "expected_direction": None,
        "reviewer_note": None,
        "confidence": "medium",
    }
    with pytest.raises(rfb.AnnotationSchemaError) as excinfo:
        _machine_annotation(labels=[label])
    assert excinfo.value.code == "annotation_label_shape_mismatch"


def test_only_undetermined_labels_carry_a_reason_code():
    label = {
        "label_id": "lbl-reason",
        "expected_change_type": "modified",
        "previous_unit_id": "previous:000:x",
        "current_unit_id": "current:000:x",
        "expected_reason_code": "ambiguous_unit_alignment",
        "expected_evidence_side": "both",
        "expected_direction": None,
        "reviewer_note": None,
        "confidence": "medium",
    }
    with pytest.raises(rfb.AnnotationSchemaError) as excinfo:
        _machine_annotation(labels=[label])
    assert excinfo.value.code == "annotation_label_unexpected_reason_code"


def test_reviewer_note_is_bounded():
    label = {
        "label_id": "lbl-note",
        "expected_change_type": "modified",
        "previous_unit_id": "previous:000:x",
        "current_unit_id": "current:000:x",
        "expected_reason_code": None,
        "expected_evidence_side": "both",
        "expected_direction": None,
        "reviewer_note": "x" * (rfb.MAX_REVIEWER_NOTE_CHARS + 1),
        "confidence": "medium",
    }
    with pytest.raises(rfb.AnnotationSchemaError) as excinfo:
        _machine_annotation(labels=[label])
    assert excinfo.value.code == "annotation_label_note_too_long"


def test_reviewer_notes_do_not_affect_the_annotation_hash():
    """Notes are review context for a person, never an evaluation input."""
    plain = fx.human_verify(_machine_annotation())
    annotated = copy.deepcopy(plain)
    annotated["labels"][0]["reviewer_note"] = "Checked the FY23 insurance figure."
    rfb.validate_annotation(annotated)
    assert rfb.annotation_hash(plain) == rfb.annotation_hash(annotated)


def test_invalid_confidence_is_rejected():
    document = _machine_annotation()
    document["labels"][0]["confidence"] = "very-high"
    with pytest.raises(rfb.AnnotationSchemaError) as excinfo:
        rfb.validate_annotation(document)
    assert excinfo.value.code == "annotation_label_invalid_confidence"


# --- Deterministic hashing ----------------------------------------------------


def test_section_hash_is_whitespace_normalized_and_content_sensitive():
    assert rfb.section_hash("Risk  A\n\nRisk B") == rfb.section_hash("Risk A Risk B")
    assert rfb.section_hash("Risk A") != rfb.section_hash("Risk B")


def test_unit_ids_are_deterministic_and_side_scoped():
    assert rfb.unit_id("previous", 3, "cyber-risks") == "previous:003:cyber-risks"
    assert rfb.unit_id("current", 3, "cyber-risks") != rfb.unit_id(
        "previous", 3, "cyber-risks"
    )
    with pytest.raises(ValueError):
        rfb.unit_id("sideways", 0, "cyber-risks")


def test_label_ids_are_deterministic():
    first = rfb.label_id_for("pair-01", "previous:000:a", "current:000:a")
    second = rfb.label_id_for("pair-01", "previous:000:a", "current:000:a")
    third = rfb.label_id_for("pair-01", "previous:000:a", None)
    assert first == second
    assert first != third


def test_payload_hash_is_key_order_independent():
    assert rfb.payload_hash({"a": 1, "b": 2}) == rfb.payload_hash({"b": 2, "a": 1})


# --- Zero-denominator policy --------------------------------------------------


def test_zero_denominator_reports_null_not_zero():
    metric = rfb.rate(0, 0, "change_recall")
    assert metric["value"] is None
    assert metric["denominator"] == 0
    assert metric["zero_denominator"] is True
    assert metric["zero_denominator_policy"] == rfb.ZERO_DENOMINATOR_POLICY


def test_nonzero_denominator_reports_a_value_with_both_terms():
    metric = rfb.rate(3, 4, "change_precision")
    assert metric["value"] == 0.75
    assert (metric["numerator"], metric["denominator"]) == (3, 4)
    assert metric["zero_denominator"] is False


# --- Import-graph boundary ----------------------------------------------------


def test_benchmark_core_module_imports_nothing_that_can_reach_a_network():
    """Checked at the import graph, not in prose, so a docstring that merely
    claims "offline" cannot satisfy or break it."""
    import ast

    source = (REPO_ROOT / "real_filing_benchmark.py").read_text(encoding="utf-8")
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
        "real_filing_acquisition",
    ):
        assert forbidden not in imported, forbidden
