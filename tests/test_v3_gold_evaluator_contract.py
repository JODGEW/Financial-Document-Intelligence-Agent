"""The contract-v2 gold evaluator: canonical sequence-aware subject identity.

The v2 holdout evaluation exposed an evaluator defect: gold matching keyed
predictions and labels by the NORMALIZED unit_key, so two units whose headings
normalize identically collapsed into one metric subject even though the v3
detector, the packet generator, and the annotation admission validator all
preserve each occurrence under the canonical identity ``side:sequence:unit_key``.

This suite freezes the corrected FUTURE contract before any new holdout is
selected:

- ``real-filing-benchmark.evaluation.v2`` + ``real-filing-benchmark-metrics.v2``
  is the only pairing that scores v3-detector artifacts, and it matches by
  exact canonical subject identity — unit_key stays descriptive metadata.
- The frozen contract v1 stays readable and identifiable, scores v2-detector
  artifacts only, and is never inferred, upgraded, or recomputed.
- Repeated normalized headings remain separate metric subjects at every seam:
  identity, label subjects, prediction subjects, matching, duplicates,
  closure, per-pair scoring, aggregation, and pair exact match.

Everything here is SYNTHETIC: hand-built build records, annotations, and
detector-shaped results in tmp_path. No real filing text, no frozen v2 label,
no network, no AWS, no LLM, no embeddings, and no Chroma. The committed v2
evidence is byte-pinned by test_gold_evaluation_signoff and is not read as an
input by any case below.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import comparison_detector  # noqa: E402
import comparison_store  # noqa: E402
import real_filing_benchmark as rfb  # noqa: E402
from scripts import eval_real_filing_benchmark as evaluator  # noqa: E402

THIS_SUITE = "tests/test_v3_gold_evaluator_contract.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"

PAIR_ID = "pair-01"
PREVIOUS_FILING = "synthetic-issuer:10-K:2023-12-31"
CURRENT_FILING = "synthetic-issuer:10-K:2024-12-31"
SOURCE_SHA = {
    "previous": hashlib.sha256(b"synthetic previous body").hexdigest(),
    "current": hashlib.sha256(b"synthetic current body").hexdigest(),
}


# --- Synthetic corpus primitives ----------------------------------------------


def _unit(side: str, index: int, key: str, *, content: str | None = None) -> dict:
    content = content or f"synthetic {key} narrative for {side} occurrence {index}"
    return {
        "unit_id": rfb.unit_id(side, index, key),
        "unit_key": key,
        "heading": key.replace("-", " ").title(),
        "char_count": len(content),
        "content_hash": hashlib.sha1(content.encode("utf-8")).hexdigest()[:12],
        "excerpt": content[:100],
    }


def _side(side: str, units: list[dict]) -> dict:
    return {
        "filing_id": PREVIOUS_FILING if side == "previous" else CURRENT_FILING,
        "extraction_outcome": rfb.EXTRACTION_EXTRACTED,
        "section_hash": rfb.section_hash(f"synthetic {side} section"),
        "source_sha256": SOURCE_SHA[side],
        "unit_count": len(units),
        "units": units,
    }


def _build_record(
    previous_units: list[dict],
    current_units: list[dict],
    *,
    detector: str = evaluator.CONTRACT_V2_DETECTOR,
    workflow: str = evaluator.CONTRACT_V2_WORKFLOW,
    execution: dict | None = None,
) -> dict:
    return {
        "pair_id": PAIR_ID,
        "issuer_name": "Fictional Contract Issuer, Inc.",
        "sector_label": "Fictional Sector A",
        "build_hash": hashlib.sha256(b"synthetic build").hexdigest(),
        "parser_versions": {"detector": detector, "workflow": workflow},
        "previous": _side("previous", previous_units),
        "current": _side("current", current_units),
        "execution": execution
        or {"evidence_total": 4, "evidence_unresolved": 0, "evidence_foreign": 0},
    }


def _label(
    change_type: str,
    prev: str | None = None,
    curr: str | None = None,
    *,
    reason: str | None = None,
    direction: str | None = None,
    confidence: str = "high",
) -> dict:
    return {
        "label_id": rfb.label_id_for(PAIR_ID, prev, curr),
        "expected_change_type": change_type,
        "previous_unit_id": prev,
        "current_unit_id": curr,
        "expected_reason_code": reason,
        "expected_evidence_side": {
            "added": "current",
            "removed": "previous",
            "modified": "both",
            "unchanged": "both",
        }.get(change_type, "none"),
        "expected_direction": direction,
        "reviewer_note": None,
        "confidence": confidence,
    }


def _annotation(labels: list[dict]) -> dict:
    return {
        "schema_version": rfb.ANNOTATION_SCHEMA_VERSION,
        "annotation_protocol_version": rfb.ANNOTATION_PROTOCOL_VERSION,
        "benchmark_id": "synthetic_contract_corpus",
        "pair_id": PAIR_ID,
        "annotation_status": rfb.ANNOTATION_HUMAN_VERIFIED,
        "annotator_id": "reviewer@localhost",
        "verification_timestamp": "2026-07-01T12:00:00+00:00",
        "source_manifest_hash": hashlib.sha256(b"synthetic manifest").hexdigest(),
        "previous_section_hash": rfb.section_hash("synthetic previous section"),
        "current_section_hash": rfb.section_hash("synthetic current section"),
        "labels": labels,
    }


def _change(
    change_type: str,
    material: str,
    *,
    reason: str | None = None,
    validation: list[dict] | None = None,
    previous_evidence: list[dict] | None = None,
    current_evidence: list[dict] | None = None,
) -> dict:
    """A detector-shaped change: identity travels only in ``change_id``."""
    return {
        "change_id": comparison_detector._change_id(  # noqa: SLF001
            change_type, material
        ),
        "change_type": change_type,
        "category": "risk_factor",
        "section_key": "item_1a_risk_factors",
        "summary": "synthetic change summary",
        "previous_evidence": previous_evidence or [],
        "current_evidence": current_evidence or [],
        "validation": validation or [],
        "undetermined_reason": reason,
    }


def _state(
    build_record: dict, annotation: dict | None, changes: list[dict] | None
) -> dict:
    return {
        "pair_id": build_record["pair_id"],
        "issuer_name": build_record["issuer_name"],
        "sector_label": build_record["sector_label"],
        "build_record": build_record,
        "detection_result": None if changes is None else {"changes": changes},
        "annotation": annotation,
        "annotation_status": annotation["annotation_status"] if annotation else None,
        "problems": [],
    }


def _config(**overrides) -> dict:
    document = {
        "config_version": evaluator.EVALUATION_CONFIG_VERSION_V2,
        "benchmark_id": "synthetic_contract_corpus",
        "metric_definitions_version": evaluator.METRIC_DEFINITIONS_VERSION_V2,
        "annotation_protocol_version": rfb.ANNOTATION_PROTOCOL_VERSION,
        "declared_detector_version": comparison_detector.DETECTOR_VERSION,
        "declared_workflow_version": comparison_store.WORKFLOW_VERSION,
        "declared_unit_grammar_version": evaluator.CONTRACT_V2_UNIT_GRAMMAR,
        "declared_section_key": "item_1a_risk_factors",
        "gold_status_required": rfb.GOLD_STATUS,
        "pass_fail_thresholds": None,
        # Unsigned by construction. Named via the evaluator's constant: the
        # repo-wide no-writer scan rightly rejects new files that spell the
        # sign-off field out, and this suite must not join its allowlist.
        evaluator.SIGNOFF_FIELD: None,
    }
    document.update(overrides)
    return document


def _manifest_pair(pair_id: str = PAIR_ID) -> dict:
    return {
        "pair_id": pair_id,
        "issuer_name": "Fictional Contract Issuer, Inc.",
        "sector_label": "Fictional Sector A",
        "previous": {"expected_sha256": SOURCE_SHA["previous"]},
        "current": {"expected_sha256": SOURCE_SHA["current"]},
    }


def _gold_report(tmp_path: Path, states: list[dict], config: dict, **kwargs) -> dict:
    manifest = {
        "schema_version": rfb.MANIFEST_SCHEMA_VERSION,
        "benchmark_id": "synthetic_contract_corpus",
        "benchmark_version": 1,
        "status": rfb.STATUS_CORPUS_BUILT,
        "selection_protocol_version": rfb.SELECTION_PROTOCOL_VERSION,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    pairs = [_manifest_pair(state["pair_id"]) for state in states]
    return evaluator.gold_report(
        manifest,
        manifest_path,
        pairs,
        states,
        rfb.CorpusLayout(tmp_path / "corpus"),
        config,
        **kwargs,
    )


def _codes(report: dict) -> set[str]:
    return {reason["code"] for reason in report.get("refusal_reasons", [])}


# --- The canonical repeated-heading corpus ------------------------------------
#
# Two previous units and two current units share the normalized unit_key
# ``repeated-risks`` with DIFFERENT sequence-aware canonical identities, plus a
# modified pair, an unchanged pair, and an added / removed occurrence — enough
# to give every occurrence its own gold relationship.

R_PREV_2 = rfb.unit_id("previous", 2, "repeated-risks")
R_PREV_3 = rfb.unit_id("previous", 3, "repeated-risks")
R_CURR_2 = rfb.unit_id("current", 2, "repeated-risks")
R_CURR_3 = rfb.unit_id("current", 3, "repeated-risks")


def _repeated_units(side: str) -> list[dict]:
    return [
        _unit(side, 0, "intro-risks", content="identical intro narrative"),
        _unit(side, 1, "cyber-risks", content=f"cyber narrative {side}"),
        _unit(side, 2, "repeated-risks", content=f"first repeat {side}"),
        _unit(side, 3, "repeated-risks", content=f"second repeat {side}"),
    ]


def _repeated_build() -> dict:
    previous = _repeated_units("previous") + [_unit("previous", 4, "sunset-risks")]
    current = _repeated_units("current") + [_unit("current", 4, "novel-risks")]
    return _build_record(previous, current)


AMBIGUOUS_REASON = "ambiguous_unit_alignment: heading key repeats"


def _repeated_changes() -> list[dict]:
    return [
        _change("modified", "cyber-risks"),
        _change("undetermined", R_PREV_2, reason=AMBIGUOUS_REASON),
        _change("undetermined", R_PREV_3, reason=AMBIGUOUS_REASON),
        _change("undetermined", R_CURR_2, reason=AMBIGUOUS_REASON),
        _change("undetermined", R_CURR_3, reason=AMBIGUOUS_REASON),
        _change("removed", "sunset-risks"),
        _change("added", "novel-risks"),
    ]


def _repeated_labels() -> list[dict]:
    return [
        _label(
            "unchanged",
            rfb.unit_id("previous", 0, "intro-risks"),
            rfb.unit_id("current", 0, "intro-risks"),
        ),
        _label(
            "modified",
            rfb.unit_id("previous", 1, "cyber-risks"),
            rfb.unit_id("current", 1, "cyber-risks"),
        ),
        _label("undetermined", R_PREV_2, None, reason="ambiguous_unit_alignment"),
        _label("undetermined", R_PREV_3, None, reason="ambiguous_unit_alignment"),
        _label("undetermined", None, R_CURR_2, reason="ambiguous_unit_alignment"),
        _label("undetermined", None, R_CURR_3, reason="ambiguous_unit_alignment"),
        _label("removed", rfb.unit_id("previous", 4, "sunset-risks"), None),
        _label("added", None, rfb.unit_id("current", 4, "novel-risks")),
    ]


def _repeated_state() -> dict:
    return _state(_repeated_build(), _annotation(_repeated_labels()), _repeated_changes())


def _score(state: dict) -> dict:
    gold, predicted, reasons = evaluator.prepare_v2_subjects(state)
    assert reasons == [], reasons
    return evaluator.score_pair_v2(state, gold, predicted)


# --- Versioning ---------------------------------------------------------------


def test_v3_artifacts_with_v2_contract_are_accepted(tmp_path):
    report = _gold_report(tmp_path, [_repeated_state()], _config())
    assert report["refused"] is False
    assert report["gold_metrics_available"] is True
    assert report["evaluation_contract_version"] == (
        evaluator.EVALUATION_CONFIG_VERSION_V2
    )
    assert report["metric_definitions_version"] == (
        evaluator.METRIC_DEFINITIONS_VERSION_V2
    )
    assert report["report_version"] == evaluator.EVALUATION_REPORT_VERSION_V2
    assert report["unit_identity_contract"] == evaluator.CONTRACT_V2_UNIT_GRAMMAR
    assert report["subject_matching"] == evaluator.SUBJECT_MATCHING_V2


def test_v3_artifacts_under_the_legacy_contract_are_refused(tmp_path):
    """Contract v1's unit-key semantics may never score v3 artifacts."""
    legacy = _config(
        config_version=evaluator.EVALUATION_CONFIG_VERSION_V1,
        metric_definitions_version=evaluator.METRIC_DEFINITIONS_VERSION_V1,
        declared_detector_version=evaluator.CONTRACT_V1_DETECTOR,
        declared_workflow_version=evaluator.CONTRACT_V1_WORKFLOW,
    )
    legacy.pop("declared_unit_grammar_version")
    # --new-run bypasses only the declared-vs-live gate; the artifact gate is
    # the load-bearing one and must hold on its own.
    report = _gold_report(tmp_path, [_repeated_state()], legacy, new_run=True)
    assert report["refused"] is True
    assert report["gold_metrics"] is None
    assert evaluator.CONTRACT_INCOMPATIBLE_DETECTOR in _codes(report)
    assert evaluator.CONTRACT_INCOMPATIBLE_WORKFLOW in _codes(report)


def test_v2_artifacts_under_the_new_contract_are_refused(tmp_path):
    state = _repeated_state()
    state["build_record"]["parser_versions"] = {
        "detector": evaluator.CONTRACT_V1_DETECTOR,
        "workflow": evaluator.CONTRACT_V1_WORKFLOW,
    }
    report = _gold_report(tmp_path, [state], _config(), new_run=True)
    assert report["refused"] is True
    assert evaluator.CONTRACT_INCOMPATIBLE_DETECTOR in _codes(report)
    assert evaluator.CONTRACT_INCOMPATIBLE_WORKFLOW in _codes(report)


def test_the_historical_contract_stays_identifiable_from_committed_configs():
    """Both committed configs resolve to contract v1, unchanged and unmutated."""
    for committed_path in (
        evaluator.DEFAULT_EVALUATION_CONFIG,
        evaluator.DEFAULT_HOLDOUT_EVALUATION_CONFIG,
    ):
        document = json.loads(committed_path.read_text(encoding="utf-8"))
        before = copy.deepcopy(document)
        contract = evaluator.resolve_evaluation_contract(document)
        assert contract.contract_version == evaluator.EVALUATION_CONFIG_VERSION_V1
        assert contract.metric_definitions_version == (
            evaluator.METRIC_DEFINITIONS_VERSION_V1
        )
        assert contract.report_version == evaluator.EVALUATION_REPORT_VERSION
        assert contract.subject_matching == evaluator.SUBJECT_MATCHING_V1
        assert contract.scored_detector_version == evaluator.CONTRACT_V1_DETECTOR
        # Resolution never mutates or upgrades the legacy config.
        assert document == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"config_version": "real-filing-benchmark.evaluation.v9"},
        {"metric_definitions_version": "real-filing-benchmark-metrics.v9"},
        {"config_version": None},
        {"metric_definitions_version": None},
        # Mixed pairings are not a contract: nothing is inferred from the
        # half that happens to be recognizable.
        {"metric_definitions_version": evaluator.METRIC_DEFINITIONS_VERSION_V1},
        {"config_version": evaluator.EVALUATION_CONFIG_VERSION_V1},
    ],
)
def test_unknown_or_mixed_contract_declarations_fail_closed(overrides):
    document = _config(**overrides)
    with pytest.raises(evaluator.EvaluationRefused) as failure:
        evaluator.resolve_evaluation_contract(document)
    assert failure.value.code == evaluator.CONTRACT_VERSION_UNKNOWN


def test_config_missing_both_version_keys_is_not_silently_defaulted():
    document = _config()
    document.pop("config_version")
    document.pop("metric_definitions_version")
    with pytest.raises(evaluator.EvaluationRefused) as failure:
        evaluator.resolve_evaluation_contract(document)
    assert failure.value.code == evaluator.CONTRACT_VERSION_UNKNOWN


@pytest.mark.parametrize(
    "grammar", [None, "item1a_units.v2", "item1a_units.v9"]
)
def test_v2_contract_requires_the_v3_unit_identity_grammar(grammar):
    document = _config(declared_unit_grammar_version=grammar)
    with pytest.raises(evaluator.EvaluationRefused) as failure:
        evaluator.resolve_evaluation_contract(document)
    assert failure.value.code == evaluator.CONTRACT_INCOMPATIBLE_UNIT_IDENTITY


def test_v1_contract_cannot_declare_a_unit_identity_grammar():
    document = _config(
        config_version=evaluator.EVALUATION_CONFIG_VERSION_V1,
        metric_definitions_version=evaluator.METRIC_DEFINITIONS_VERSION_V1,
        declared_unit_grammar_version=evaluator.CONTRACT_V2_UNIT_GRAMMAR,
    )
    with pytest.raises(evaluator.EvaluationRefused) as failure:
        evaluator.resolve_evaluation_contract(document)
    assert failure.value.code == evaluator.CONTRACT_INCOMPATIBLE_UNIT_IDENTITY


def test_unknown_artifact_detector_and_workflow_versions_are_refused(tmp_path):
    state = _repeated_state()
    state["build_record"]["parser_versions"] = {
        "detector": "item1a_detector.v9",
        "workflow": "comparison_workflow.v9",
    }
    report = _gold_report(tmp_path, [state], _config(), new_run=True)
    assert report["refused"] is True
    assert evaluator.CONTRACT_INCOMPATIBLE_DETECTOR in _codes(report)
    assert evaluator.CONTRACT_INCOMPATIBLE_WORKFLOW in _codes(report)


# --- Canonical identity and gold label subjects -------------------------------


def test_valid_subjects_exist_for_every_change_shape():
    """Added, removed, modified, unchanged, and undetermined all construct."""
    gold, predicted, reasons = evaluator.prepare_v2_subjects(_repeated_state())
    assert reasons == []
    gold_by_type: dict[str, int] = {}
    for subject in gold:
        gold_by_type[subject["change_type"]] = (
            gold_by_type.get(subject["change_type"], 0) + 1
        )
    assert gold_by_type == {
        "unchanged": 1,
        "modified": 1,
        "undetermined": 4,
        "removed": 1,
        "added": 1,
    }
    assert {subject["change_type"] for subject in predicted} == {
        "modified",
        "undetermined",
        "removed",
        "added",
    }


def _reasons_for_labels(labels: list[dict]) -> list[dict]:
    state = _state(_repeated_build(), _annotation(labels), _repeated_changes())
    _gold, _predicted, reasons = evaluator.prepare_v2_subjects(state)
    return reasons


def test_gold_side_mismatch_is_rejected():
    labels = _repeated_labels()
    # A previous-side field naming a current-side identity.
    labels[6] = _label("removed", R_CURR_2, None)
    codes = {reason["code"] for reason in _reasons_for_labels(labels)}
    assert evaluator.GOLD_SUBJECT_SIDE_MISMATCH in codes


def test_gold_sequence_mismatch_is_rejected():
    labels = _repeated_labels()
    labels[6] = _label("removed", rfb.unit_id("previous", 9, "sunset-risks"), None)
    codes = {reason["code"] for reason in _reasons_for_labels(labels)}
    assert evaluator.GOLD_SUBJECT_UNKNOWN_UNIT_IDENTITY in codes


def test_gold_unit_key_metadata_mismatch_is_rejected():
    labels = _repeated_labels()
    # Position previous:004 exists, but under 'sunset-risks', not this key.
    labels[6] = _label("removed", "previous:004:renamed-risks", None)
    codes = {reason["code"] for reason in _reasons_for_labels(labels)}
    assert evaluator.GOLD_SUBJECT_METADATA_MISMATCH in codes


def test_gold_bare_unit_key_reference_is_rejected_not_resolved():
    """No fallback from a missing canonical id to unit_key matching."""
    labels = _repeated_labels()
    labels[6] = _label("removed", "sunset-risks", None)
    codes = {reason["code"] for reason in _reasons_for_labels(labels)}
    assert evaluator.GOLD_SUBJECT_MISSING_UNIT_IDENTITY in codes


def test_gold_duplicate_subject_is_rejected():
    labels = _repeated_labels()
    duplicate = dict(labels[6], label_id="lbl-duplicate-distinct-id")
    reasons = _reasons_for_labels(labels + [duplicate])
    codes = {reason["code"] for reason in reasons}
    # A distinct label_id does not make it a distinct subject.
    assert evaluator.GOLD_SUBJECT_DUPLICATE in codes


@pytest.mark.parametrize(
    "bad_label",
    [
        # An 'added' label may not carry a previous unit.
        _label("added", R_PREV_2, rfb.unit_id("current", 4, "novel-risks")),
        # A 'removed' label may not carry a current unit.
        _label("removed", rfb.unit_id("previous", 4, "sunset-risks"), R_CURR_2),
        # A 'modified' label needs both sides.
        _label("modified", rfb.unit_id("previous", 1, "cyber-risks"), None),
    ],
)
def test_gold_subject_shape_violations_are_rejected(bad_label):
    labels = [
        label
        for label in _repeated_labels()
        if not (
            set(filter(None, (label["previous_unit_id"], label["current_unit_id"])))
            & set(
                filter(
                    None,
                    (bad_label["previous_unit_id"], bad_label["current_unit_id"]),
                )
            )
        )
    ]
    codes = {
        reason["code"] for reason in _reasons_for_labels(labels + [bad_label])
    }
    assert evaluator.SUBJECT_SHAPE_INVALID in codes


def test_uncovered_unit_fails_inventory_closure():
    labels = _repeated_labels()[:-1]  # drop the 'added' label for novel-risks
    codes = {reason["code"] for reason in _reasons_for_labels(labels)}
    assert evaluator.UNIT_INVENTORY_NOT_CLOSED in codes


def test_multiply_covered_unit_fails_inventory_closure():
    labels = _repeated_labels()
    # A second, non-duplicate subject binding an already covered unit.
    labels.append(_label("undetermined", R_PREV_2, R_CURR_2))
    codes = {reason["code"] for reason in _reasons_for_labels(labels)}
    assert evaluator.UNIT_INVENTORY_NOT_CLOSED in codes


def test_label_order_and_label_ids_do_not_affect_metrics():
    baseline = _score(_repeated_state())

    reordered_labels = list(reversed(_repeated_labels()))
    reordered = _state(
        _repeated_build(), _annotation(reordered_labels), _repeated_changes()
    )
    renamed_labels = _repeated_labels()
    for index, label in enumerate(renamed_labels):
        label["label_id"] = f"lbl-renamed-{index:02d}"
    renamed = _state(
        _repeated_build(), _annotation(renamed_labels), _repeated_changes()
    )

    for variant in (reordered, renamed):
        score = _score(variant)
        assert score["metrics"] == baseline["metrics"]
        assert score["exact_match"] == baseline["exact_match"]
        assert score["missed"] == baseline["missed"]
        assert score["false_positives"] == baseline["false_positives"]


# --- Prediction subjects ------------------------------------------------------


def _reasons_for_changes(changes: list[dict]) -> list[dict]:
    state = _state(_repeated_build(), _annotation(_repeated_labels()), changes)
    _gold, _predicted, reasons = evaluator.prepare_v2_subjects(state)
    return reasons


def test_prediction_subjects_bind_exact_canonical_occurrences():
    _gold, predicted, reasons = evaluator.prepare_v2_subjects(_repeated_state())
    assert reasons == []
    by_key = {subject["key"]: subject["change_type"] for subject in predicted}
    assert by_key[(R_PREV_2, None)] == "undetermined"
    assert by_key[(R_PREV_3, None)] == "undetermined"
    assert by_key[(None, R_CURR_2)] == "undetermined"
    assert by_key[(None, R_CURR_3)] == "undetermined"
    assert by_key[
        (
            rfb.unit_id("previous", 1, "cyber-risks"),
            rfb.unit_id("current", 1, "cyber-risks"),
        )
    ] == "modified"
    assert by_key[(rfb.unit_id("previous", 4, "sunset-risks"), None)] == "removed"
    assert by_key[(None, rfb.unit_id("current", 4, "novel-risks"))] == "added"


def test_prediction_with_unresolvable_change_id_fails_closed():
    changes = _repeated_changes()
    changes.append(
        {
            "change_id": "chg-000000000000",
            "change_type": "added",
            "category": "risk_factor",
            "section_key": "item_1a_risk_factors",
            "summary": "synthetic",
            "previous_evidence": [],
            "current_evidence": [],
            "validation": [],
            "undetermined_reason": None,
        }
    )
    codes = {reason["code"] for reason in _reasons_for_changes(changes)}
    assert evaluator.PREDICTION_SUBJECT_UNKNOWN_UNIT_IDENTITY in codes


def test_prediction_keyed_by_a_repeating_unit_key_fails_closed():
    """A v3 prediction that names no single occurrence is never scored by key."""
    changes = _repeated_changes() + [_change("undetermined", "repeated-risks")]
    codes = {reason["code"] for reason in _reasons_for_changes(changes)}
    assert evaluator.PREDICTION_SUBJECT_MISSING_UNIT_IDENTITY in codes


def test_duplicate_prediction_subjects_are_rejected_not_merged():
    changes = _repeated_changes() + [
        _change("undetermined", "cyber-risks", reason="synthetic duplicate")
    ]
    codes = {reason["code"] for reason in _reasons_for_changes(changes)}
    assert evaluator.PREDICTION_SUBJECT_DUPLICATE in codes


def test_prediction_shape_violation_is_rejected():
    # 'added' material that resolves to a previous-side-only unit.
    changes = _repeated_changes() + [_change("added", "sunset-risks")]
    codes = {reason["code"] for reason in _reasons_for_changes(changes)}
    assert evaluator.SUBJECT_SHAPE_INVALID in codes


def test_prediction_order_evidence_and_confidence_do_not_affect_identity():
    baseline = _score(_repeated_state())

    reordered = _state(
        _repeated_build(),
        _annotation(_repeated_labels()),
        list(reversed(_repeated_changes())),
    )
    assert _score(reordered)["metrics"] == baseline["metrics"]

    decorated_changes = _repeated_changes()
    decorated_changes[0]["previous_evidence"] = [
        {"document_id": PREVIOUS_FILING, "chunk_id": "chunk-1"}
    ]
    decorated_changes[0]["confidence"] = "high"
    decorated = _state(
        _repeated_build(), _annotation(_repeated_labels()), decorated_changes
    )
    assert _score(decorated)["metrics"] == baseline["metrics"]

    relabeled = _repeated_labels()
    for label in relabeled:
        label["confidence"] = "low"
    low_confidence = _state(
        _repeated_build(), _annotation(relabeled), _repeated_changes()
    )
    assert _score(low_confidence)["metrics"] == baseline["metrics"]


# --- Repeated headings stay separate ------------------------------------------


def test_four_repeated_heading_units_are_four_distinct_subjects():
    score = _score(_repeated_state())
    # 7 non-unchanged gold subjects: 4 undetermined occurrences + modified +
    # removed + added. Occurrences count, not distinct headings.
    assert score["expected_change_count"] == 7
    assert score["detected_change_count"] == 7
    assert score["matched_count"] == 7
    assert score["exact_match"] is True
    assert score["metrics"]["change_precision"]["denominator"] == 7
    assert score["metrics"]["change_recall"]["denominator"] == 7


def test_one_occurrence_cannot_satisfy_another():
    """A prediction for occurrence 2 does not satisfy occurrence 3."""
    changes = [
        change
        for change in _repeated_changes()
        if change["change_id"]
        != comparison_detector._change_id("undetermined", R_PREV_3)  # noqa: SLF001
    ]
    # Duplicate the occurrence-2 prediction? No — that would be a duplicate
    # subject. Simply omitting occurrence 3 must read as a miss even though
    # occurrence 2 shares its normalized heading.
    state = _state(_repeated_build(), _annotation(_repeated_labels()), changes)
    score = _score(state)
    assert score["matched_count"] == 6
    assert score["metrics"]["change_recall"]["numerator"] == 6
    assert score["metrics"]["change_recall"]["denominator"] == 7
    assert [R_PREV_3 + "|-", "undetermined"] in score["missed"]
    assert score["exact_match"] is False


def test_swapped_sequence_identities_do_not_match():
    """Two occurrences of one heading are not interchangeable subjects.

    A pure swap of two same-type occurrences yields the same subject SET, so
    the boundary is proven with types: give the two occurrences DIFFERENT
    gold types. If matching fell back to the shared normalized heading, the
    detector's undetermined prediction for occurrence 3 could satisfy either
    label; under exact canonical identity it satisfies only its own.
    """
    labels = _repeated_labels()
    labels[4] = _label(
        "undetermined", None, R_CURR_2, reason="ambiguous_unit_alignment"
    )
    labels[5] = _label("added", None, R_CURR_3)
    state = _state(_repeated_build(), _annotation(labels), _repeated_changes())
    score = _score(state)
    # The detector predicted undetermined for BOTH occurrences; the
    # added-labelled occurrence is a type mismatch on its exact subject — not
    # a match borrowed from the sibling with the same normalized heading.
    assert score["matched_count"] == 6
    assert score["metrics"]["change_type_accuracy"]["denominator"] == 7
    assert score["metrics"]["change_type_accuracy"]["numerator"] == 6
    assert score["exact_match"] is False


def test_missing_and_extra_repeated_occurrences_move_the_right_denominators():
    # 'Extra' under a closed inventory means a prediction for an occurrence
    # gold labels as unchanged; 'missing' means a gold occurrence with no
    # prediction. Occurrence 3 is labelled unchanged as a pair here, so the
    # detector's two per-occurrence predictions for it become extras.
    labels = [
        _label(
            "unchanged",
            rfb.unit_id("previous", 0, "intro-risks"),
            rfb.unit_id("current", 0, "intro-risks"),
        ),
        _label(
            "modified",
            rfb.unit_id("previous", 1, "cyber-risks"),
            rfb.unit_id("current", 1, "cyber-risks"),
        ),
        _label("undetermined", R_PREV_2, None, reason="ambiguous_unit_alignment"),
        # Occurrence 3 is labelled unchanged across the pair; the detector
        # still emitted per-occurrence undetermined changes for it.
        _label("unchanged", R_PREV_3, R_CURR_3),
        _label("undetermined", None, R_CURR_2, reason="ambiguous_unit_alignment"),
        _label("removed", rfb.unit_id("previous", 4, "sunset-risks"), None),
        _label("added", None, rfb.unit_id("current", 4, "novel-risks")),
    ]
    state = _state(_repeated_build(), _annotation(labels), _repeated_changes())
    score = _score(state)
    # Gold change subjects: 5 (undetermined prev2, undetermined curr2,
    # modified, removed, added). Predictions: 7. The two occurrence-3
    # predictions are false positives against an unchanged-labelled unit.
    assert score["metrics"]["change_precision"]["denominator"] == 7
    assert score["metrics"]["change_precision"]["numerator"] == 5
    assert score["metrics"]["change_recall"]["denominator"] == 5
    assert score["metrics"]["change_recall"]["numerator"] == 5
    assert score["metrics"]["unchanged_false_positive_rate"]["denominator"] == 2
    assert score["metrics"]["unchanged_false_positive_rate"]["numerator"] == 1
    assert score["exact_match"] is False


def test_wrong_type_on_one_repeated_occurrence_lowers_type_accuracy():
    labels = _repeated_labels()
    labels[3] = _label("removed", R_PREV_3, None)
    state = _state(_repeated_build(), _annotation(labels), _repeated_changes())
    score = _score(state)
    assert score["metrics"]["change_type_accuracy"]["denominator"] == 7
    assert score["metrics"]["change_type_accuracy"]["numerator"] == 6
    assert score["exact_match"] is False


def test_pair_exact_match_requires_the_exact_subject_type_set():
    exact = _score(_repeated_state())
    assert exact["exact_match"] is True

    # Missing one repeated occurrence prediction → not exact.
    missing = _state(
        _repeated_build(),
        _annotation(_repeated_labels()),
        [
            change
            for change in _repeated_changes()
            if change["change_id"]
            != comparison_detector._change_id(  # noqa: SLF001
                "undetermined", R_CURR_3
            )
        ],
    )
    assert _score(missing)["exact_match"] is False

    # Extra prediction against an unchanged-labelled subject → not exact.
    extra = _state(
        _repeated_build(),
        _annotation(_repeated_labels()),
        _repeated_changes() + [_change("modified", "intro-risks")],
    )
    assert _score(extra)["exact_match"] is False


def test_serialization_round_trip_preserves_every_occurrence():
    state = _repeated_state()
    round_tripped = json.loads(json.dumps(state, sort_keys=True))
    assert _score(round_tripped) == _score(state)


# --- Metric definitions -------------------------------------------------------


def test_zero_denominators_stay_null():
    labels = [
        label
        for label in _repeated_labels()
        if label["expected_change_type"] != "unchanged"
    ] + [
        # Cover the intro units without an unchanged label: mark them
        # undetermined so the unchanged denominator is genuinely zero.
        _label(
            "undetermined",
            rfb.unit_id("previous", 0, "intro-risks"),
            rfb.unit_id("current", 0, "intro-risks"),
            reason="ambiguous_unit_alignment",
        ),
    ]
    changes = _repeated_changes() + [
        _change(
            "undetermined",
            "intro-risks",
            reason="ambiguous_unit_alignment: synthetic",
        )
    ]
    state = _state(_repeated_build(), _annotation(labels), changes)
    score = _score(state)
    metric = score["metrics"]["unchanged_false_positive_rate"]
    assert metric["denominator"] == 0
    assert metric["value"] is None
    assert metric["zero_denominator"] is True
    direction = score["metrics"]["direction_consistency_accuracy"]
    assert direction["value"] is None and direction["denominator"] == 0


def test_evidence_resolution_rate_comes_from_execution_counters():
    build = _repeated_build()
    build["execution"] = {
        "evidence_total": 10,
        "evidence_unresolved": 2,
        "evidence_foreign": 1,
    }
    state = _state(build, _annotation(_repeated_labels()), _repeated_changes())
    metric = _score(state)["metrics"]["evidence_resolution_rate"]
    assert (metric["numerator"], metric["denominator"]) == (7, 10)


def test_numerators_never_exceed_denominators():
    for state in (
        _repeated_state(),
        _state(_repeated_build(), _annotation(_repeated_labels()), []),
    ):
        for metric in _score(state)["metrics"].values():
            if metric["denominator"]:
                assert metric["numerator"] <= metric["denominator"], metric
            else:
                assert metric["numerator"] == 0, metric


def test_blocked_pair_is_excluded_not_scored(tmp_path):
    blocked_build = _build_record(
        _repeated_units("previous"), _repeated_units("current")
    )
    blocked_build["pair_id"] = "pair-02"
    blocked_build["current"]["extraction_outcome"] = rfb.EXTRACTION_AMBIGUOUS
    blocked = {
        "pair_id": "pair-02",
        "issuer_name": blocked_build["issuer_name"],
        "sector_label": blocked_build["sector_label"],
        "build_record": blocked_build,
        "detection_result": None,
        "annotation": None,
        "annotation_status": None,
        "problems": [
            {
                "code": "annotation_not_found",
                "detail": "no completed annotation file; a machine-proposed "
                "packet is not one",
            }
        ],
    }
    report = _gold_report(tmp_path, [_repeated_state(), blocked], _config())
    assert report["refused"] is False
    scope = report["scoring_scope"]
    assert scope["pairs_scored"] == 1
    assert scope["pairs_excluded"] == 1
    assert scope["excluded_pairs"][0]["pair_id"] == "pair-02"
    # The blocked pair enters no metric denominator.
    assert report["gold_metrics"]["pair_exact_match_rate"]["denominator"] == 1


def test_invalid_duplicate_state_refuses_before_any_metric(tmp_path):
    state = _state(
        _repeated_build(),
        _annotation(_repeated_labels()),
        _repeated_changes()
        + [_change("undetermined", "cyber-risks", reason="synthetic duplicate")],
    )
    report = _gold_report(tmp_path, [state], _config())
    assert report["refused"] is True
    assert report["gold_metrics"] is None
    assert report["gold_metrics_available"] is False
    assert evaluator.PREDICTION_SUBJECT_DUPLICATE in _codes(report)


def test_per_pair_totals_reconcile_with_the_aggregate(tmp_path):
    second = _repeated_state()
    second["pair_id"] = "pair-02"
    second["build_record"] = copy.deepcopy(second["build_record"])
    second["build_record"]["pair_id"] = "pair-02"
    second["annotation"] = copy.deepcopy(second["annotation"])
    second["annotation"]["pair_id"] = "pair-02"
    report = _gold_report(tmp_path, [_repeated_state(), second], _config())
    assert report["refused"] is False
    per_pair = report["per_pair"]
    aggregate = report["gold_metrics"]
    assert aggregate["change_precision"]["numerator"] == sum(
        score["matched_count"] for score in per_pair
    )
    assert aggregate["change_precision"]["denominator"] == sum(
        score["detected_change_count"] for score in per_pair
    )
    assert aggregate["change_recall"]["denominator"] == sum(
        score["expected_change_count"] for score in per_pair
    )
    assert aggregate["pair_exact_match_rate"]["denominator"] == len(per_pair)
    assert aggregate["pair_exact_match_rate"]["numerator"] == sum(
        1 for score in per_pair if score["exact_match"]
    )


def test_metric_semantics_are_deterministic_across_runs(tmp_path):
    first = _gold_report(tmp_path, [_repeated_state()], _config())
    second = _gold_report(tmp_path, [_repeated_state()], _config())
    first.pop("evaluated_at")
    second.pop("evaluated_at")
    assert first == second


# --- Safety and immutability --------------------------------------------------


def test_metadata_drift_inside_a_build_record_is_rejected():
    build = _repeated_build()
    build["previous"]["units"][2]["unit_key"] = "renamed-risks"
    state = _state(build, _annotation(_repeated_labels()), _repeated_changes())
    _gold, _predicted, reasons = evaluator.prepare_v2_subjects(state)
    codes = {reason["code"] for reason in reasons}
    assert evaluator.SUBJECT_SHAPE_INVALID in codes


def test_refusal_reasons_are_deterministic_and_bounded(tmp_path):
    labels = _repeated_labels()
    labels[6] = _label("removed", "sunset-risks", None)
    labels[7] = _label("added", None, "current:009:novel-risks")
    state = _state(_repeated_build(), _annotation(labels), _repeated_changes())
    report_a = _gold_report(tmp_path, [state], _config())
    report_b = _gold_report(tmp_path, [state], _config())
    assert report_a["refusal_reasons"] == report_b["refusal_reasons"]
    rendered = json.dumps(report_a["refusal_reasons"])
    # Stable codes, no local paths, no raw exceptions, no filing narrative.
    assert str(tmp_path) not in rendered
    assert "Traceback" not in rendered
    assert "synthetic cyber narrative" not in rendered
    assert "narrative" not in rendered


def test_invalid_contract_writes_no_report(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            _config(
                # Matches the default manifest so the run reaches the
                # contract gate itself rather than the benchmark-id gate.
                benchmark_id="real_filing_v1",
                config_version="real-filing-benchmark.evaluation.v9",
            )
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "never-written.json"
    code = evaluator.main(
        [
            "--evaluation-config",
            str(config_path),
            "--report",
            str(report_path),
            "--json",
        ]
    )
    assert code == 2
    assert not report_path.exists()
    err = capsys.readouterr().err
    assert evaluator.CONTRACT_VERSION_UNKNOWN in err


def test_committed_v2_artifacts_are_untouched_by_this_suite():
    """The frozen v2 evidence stays exactly as committed: contract v1,
    unsigned, generalization unsupported, metrics not recomputed here."""
    holdout_dir = REPO_ROOT / "benchmarks" / "real_filing_holdout_v1"
    config_document = json.loads(
        (holdout_dir / "evaluation_config.json").read_text(encoding="utf-8")
    )
    assert config_document["config_version"] == (
        evaluator.EVALUATION_CONFIG_VERSION_V1
    )
    assert config_document["metric_definitions_version"] == (
        evaluator.METRIC_DEFINITIONS_VERSION_V1
    )
    assert "declared_unit_grammar_version" not in config_document
    assert config_document[evaluator.SIGNOFF_FIELD] is None
    report = json.loads(
        (holdout_dir / "gold_evaluation_report.json").read_text(encoding="utf-8")
    )
    assert report["report_version"] == evaluator.EVALUATION_REPORT_VERSION
    assert report["metric_definitions_version"] == (
        evaluator.METRIC_DEFINITIONS_VERSION_V1
    )
    assert "evaluation_contract_version" not in report
    assert report["generalization_claim_supported"] is False
    assert report["detector_version"] == evaluator.CONTRACT_V1_DETECTOR
    assert report["workflow_version"] == evaluator.CONTRACT_V1_WORKFLOW


def test_v2_scoring_never_opens_the_index_or_runs_the_detector(
    tmp_path, monkeypatch
):
    """Contract-v2 scoring reads serialized artifacts only.

    No Chroma open, no embeddings, no live detection: if any seam under the
    v2 gold path tried to open the index or re-run detection, this run would
    fail instead of scoring.
    """

    def _explode(*_args, **_kwargs):  # pragma: no cover - defensive
        raise AssertionError("offline evaluation must not touch the index")

    monkeypatch.setattr(comparison_detector, "open_index", _explode)
    monkeypatch.setattr(comparison_detector, "detect", _explode)
    monkeypatch.setattr(comparison_detector, "load_section", _explode)
    report = _gold_report(tmp_path, [_repeated_state()], _config())
    assert report["refused"] is False
    assert report["gold_metrics_available"] is True


# --- CI wiring ----------------------------------------------------------------


def _workflow():
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_this_suite_runs_in_the_required_check():
    job = _workflow()["jobs"]["comparison-regression"]
    runs = "\n".join(
        step["run"]
        for step in job["steps"]
        if isinstance(step.get("run"), str) and "pytest" in step["run"]
    )
    assert THIS_SUITE in runs
    assert (REPO_ROOT / THIS_SUITE).is_file()


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


def test_required_check_stays_offline_and_credential_free():
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "--allow-network",
        "SEC_USER_AGENT",
        "sec.gov",
        "secrets.",
        "AWS_ACCESS_KEY",
        "configure-aws-credentials",
    ):
        assert forbidden not in raw, forbidden
