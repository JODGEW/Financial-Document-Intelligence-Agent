"""Tests for the comparison.v1 contract (governance/comparison_schema.py).

Golden fixtures 1-16 from the schema commit spec are numbered in test
docstrings. All fixtures are deterministic: fixed ids, hashes, and timestamps —
no uuid4, no now(). Everything here runs offline.
"""

import json
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from governance.comparison_schema import (
    CHANGE_CATEGORIES,
    CHANGE_TYPES,
    COMPARISON_DECISIONS,
    COMPARISON_SCHEMA_VERSION,
    REVIEW_STATUSES,
    SECTION_ITEM_1A,
    VALIDATION_CHECK_NAMES,
    VALIDATION_STATUSES,
    ComparisonResult,
    FilingChange,
    ValidationCheck,
    dump_comparison,
    load_comparison,
    summarize_validation,
)

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "comparison_v1_golden.json"

_PREV_DOC = "acme-corp-10k-fy2023"
_CURR_DOC = "acme-corp-10k-fy2024"

_FILINGS = {
    "prev": {
        "document_id": _PREV_DOC,
        "company_key": "acme corp",
        "company_name": "Acme Corp",
        "form_type": "10-K",
        "filing_date": "2024-02-20",
        "period_end": "2023-12-31",
        "source_name": "acme-corp-10k-2023.pdf",
        "version_hash": "3f2a9c1d4e5b",
    },
    "curr": {
        "document_id": _CURR_DOC,
        "company_key": "acme corp",
        "company_name": "Acme Corp",
        "form_type": "10-K",
        "filing_date": "2025-02-18",
        "period_end": "2024-12-31",
        "source_name": "acme-corp-10k-2024.pdf",
        "version_hash": "9b8c7d6e5f4a",
    },
}

_EVIDENCE = {
    "prev": {
        "document_id": _PREV_DOC,
        "chunk_id": "acme-corp-10k-2023.pdf:41:1a2b3c4d5e6f",
        "source_name": "acme-corp-10k-2023.pdf",
        "page": 41,
        "section_key": SECTION_ITEM_1A,
        "section_title": "PART I > ITEM 1A. RISK FACTORS",
        "excerpt": "We rely on a limited number of suppliers for key components.",
        "content_hash": "1a2b3c4d5e6f",
    },
    "curr": {
        "document_id": _CURR_DOC,
        "chunk_id": "acme-corp-10k-2024.pdf:44:6f5e4d3c2b1a",
        "source_name": "acme-corp-10k-2024.pdf",
        "page": 44,
        "section_key": SECTION_ITEM_1A,
        "section_title": "PART I > ITEM 1A. RISK FACTORS",
        "excerpt": (
            "We rely on single-source suppliers for key components, and any "
            "disruption could materially harm our results."
        ),
        "content_hash": "6f5e4d3c2b1a",
    },
}


def _filing(side, **overrides):
    data = deepcopy(_FILINGS[side])
    data.update(overrides)
    return data


def _evidence(side, **overrides):
    data = deepcopy(_EVIDENCE[side])
    data.update(overrides)
    return data


def _change(change_type="modified", change_id="chg-0001", **overrides):
    data = {
        "change_id": change_id,
        "change_type": change_type,
        "category": "risk_factor",
        "section_key": SECTION_ITEM_1A,
        "summary": "Supply-chain risk factor expanded to cover single-source suppliers.",
        "previous_evidence": [],
        "current_evidence": [],
        "validation": [],
        "undetermined_reason": None,
    }
    if change_type in ("modified", "removed"):
        data["previous_evidence"] = [_evidence("prev")]
    if change_type in ("modified", "added"):
        data["current_evidence"] = [_evidence("curr")]
    if change_type == "undetermined":
        data["previous_evidence"] = [_evidence("prev")]
        data["undetermined_reason"] = (
            "current filing Item 1A cybersecurity subsection could not be parsed"
        )
    data.update(overrides)
    return data


def _summary_for(changes):
    """Dict-level tally so invalid change dicts never need to parse first."""
    counts = {status: 0 for status in VALIDATION_STATUSES}
    for change in changes:
        for check in change.get("validation", []):
            if check["status"] in counts:  # invalid statuses fail field validation
                counts[check["status"]] += 1
    return {"total_checks": sum(counts.values()), **counts}


def _comparison(**overrides):
    changes = overrides.pop("changes", [_change()])
    data = {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "comparison_id": "cmp-acme-corp-10k-fy2023-fy2024",
        "previous_filing": _filing("prev"),
        "current_filing": _filing("curr"),
        "section_scope": [SECTION_ITEM_1A],
        "changes": changes,
        "validation_summary": _summary_for(changes),
        "risk": {
            "decision": "not_evaluated",
            "reason_codes": [],
            "risk_score": None,
            "risk_level": None,
        },
        "review": {"status": "not_required", "review_id": None},
        "created_at": "2026-07-01T12:00:00Z",
        "producer": "fixture.v1",
    }
    data.update(overrides)
    return data


# --- Valid fixtures ---------------------------------------------------------


def test_valid_modified_change():
    """Fixture 1: modified Risk Factor change with evidence on both sides."""
    result = load_comparison(_comparison())
    assert result.schema_version == COMPARISON_SCHEMA_VERSION
    assert result.previous_filing.period_end == date(2023, 12, 31)
    assert result.current_filing.period_end == date(2024, 12, 31)
    assert result.created_at == datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    change = result.changes[0]
    assert change.change_type == "modified"
    assert change.previous_evidence[0].document_id == _PREV_DOC
    assert change.current_evidence[0].document_id == _CURR_DOC


def test_valid_added_change():
    """Fixture 2: added change carries current-side evidence only."""
    result = load_comparison(_comparison(changes=[_change("added")]))
    change = result.changes[0]
    assert change.change_type == "added"
    assert change.previous_evidence == []
    assert len(change.current_evidence) == 1


def test_valid_removed_change():
    """Fixture 3: removed change carries previous-side evidence only."""
    result = load_comparison(_comparison(changes=[_change("removed")]))
    change = result.changes[0]
    assert change.change_type == "removed"
    assert change.current_evidence == []
    assert len(change.previous_evidence) == 1


def test_valid_undetermined_change():
    """Fixture 4: undetermined change with a reason and partial evidence."""
    result = load_comparison(_comparison(changes=[_change("undetermined")]))
    change = result.changes[0]
    assert change.change_type == "undetermined"
    assert "could not be parsed" in change.undetermined_reason
    # Partial evidence on one side is allowed for undetermined.
    assert len(change.previous_evidence) == 1
    assert change.current_evidence == []


def test_comparison_with_no_changes_is_valid():
    """An executed comparison that found nothing is a valid document."""
    result = load_comparison(_comparison(changes=[]))
    assert result.changes == []
    assert result.validation_summary.total_checks == 0


# --- Invalid filing pairs ---------------------------------------------------


def test_same_document_pair_rejected():
    """Fixture 5: previous and current must be two distinct filings."""
    with pytest.raises(ValidationError, match="must differ"):
        load_comparison(
            _comparison(
                current_filing=_filing("curr", document_id=_PREV_DOC),
                changes=[],
            )
        )


def test_different_company_rejected():
    """Fixture 6: v1 compares filings of one company."""
    with pytest.raises(ValidationError, match="company_key"):
        load_comparison(
            _comparison(
                current_filing=_filing("curr", company_key="globex corp"),
                changes=[],
            )
        )


def test_form_type_mismatch_rejected():
    """v1 compares like-for-like filings (10-K vs 10-K)."""
    with pytest.raises(ValidationError, match="form_type"):
        load_comparison(
            _comparison(current_filing=_filing("curr", form_type="10-Q"), changes=[])
        )


def test_reversed_and_equal_periods_rejected():
    """Fixture 7: previous.period_end must be strictly before current's."""
    with pytest.raises(ValidationError, match="strictly before"):
        load_comparison(
            _comparison(
                previous_filing=_filing("prev", period_end="2024-12-31"),
                current_filing=_filing("curr", period_end="2023-12-31"),
                changes=[],
            )
        )
    with pytest.raises(ValidationError, match="strictly before"):
        load_comparison(
            _comparison(
                previous_filing=_filing("prev", period_end="2024-12-31"),
                changes=[],
            )
        )


# --- Invalid evidence-side semantics ----------------------------------------


def test_modified_with_one_evidence_side_rejected():
    """Fixture 8: modified requires evidence on both sides."""
    with pytest.raises(ValidationError, match="both sides"):
        load_comparison(
            _comparison(changes=[_change("modified", current_evidence=[])])
        )
    with pytest.raises(ValidationError, match="both sides"):
        load_comparison(
            _comparison(changes=[_change("modified", previous_evidence=[])])
        )


def test_added_removed_evidence_side_mismatch_rejected():
    """Fixture 9: added must not carry previous evidence; removed no current."""
    with pytest.raises(ValidationError, match="must not carry\\s+previous_evidence"):
        load_comparison(
            _comparison(
                changes=[_change("added", previous_evidence=[_evidence("prev")])]
            )
        )
    with pytest.raises(ValidationError, match="must not carry\\s+current_evidence"):
        load_comparison(
            _comparison(
                changes=[_change("removed", current_evidence=[_evidence("curr")])]
            )
        )
    with pytest.raises(ValidationError, match="requires current_evidence"):
        load_comparison(
            _comparison(changes=[_change("added", current_evidence=[])])
        )
    with pytest.raises(ValidationError, match="requires previous_evidence"):
        load_comparison(
            _comparison(changes=[_change("removed", previous_evidence=[])])
        )


def test_evidence_referencing_wrong_filing_rejected():
    """Fixture 10: each evidence side must reference its own filing."""
    wrong_prev = _evidence("prev", document_id=_CURR_DOC)
    with pytest.raises(ValidationError, match="not previous_filing.document_id"):
        load_comparison(
            _comparison(
                changes=[
                    _change("modified", previous_evidence=[wrong_prev])
                ]
            )
        )

    wrong_curr = _evidence("curr", document_id=_PREV_DOC)
    with pytest.raises(ValidationError, match="not current_filing.document_id"):
        load_comparison(
            _comparison(
                changes=[_change("modified", current_evidence=[wrong_curr])]
            )
        )

    stray = _evidence("curr", document_id="acme-corp-10k-fy2022")
    with pytest.raises(ValidationError, match="not current_filing.document_id"):
        load_comparison(
            _comparison(changes=[_change("added", current_evidence=[stray])])
        )


def test_duplicate_change_ids_rejected():
    """Fixture 11: change_id values are unique within a comparison."""
    with pytest.raises(ValidationError, match="duplicate change_id"):
        load_comparison(
            _comparison(
                changes=[
                    _change("modified", change_id="chg-0001"),
                    _change("added", change_id="chg-0001"),
                ]
            )
        )


def test_duplicate_evidence_references_rejected():
    """Fixture 12: the same (document_id, chunk_id) twice in one side."""
    with pytest.raises(ValidationError, match="duplicate evidence reference"):
        load_comparison(
            _comparison(
                changes=[
                    _change(
                        "modified",
                        current_evidence=[_evidence("curr"), _evidence("curr")],
                    )
                ]
            )
        )


def test_empty_identifiers_and_excerpts_rejected():
    """Fixture 13: empty or whitespace-only identifiers/excerpts fail."""
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            load_comparison(
                _comparison(
                    changes=[
                        _change(
                            "modified",
                            current_evidence=[_evidence("curr", excerpt=bad)],
                        )
                    ]
                )
            )
        with pytest.raises(ValidationError):
            load_comparison(_comparison(comparison_id=bad, changes=[]))
        with pytest.raises(ValidationError):
            load_comparison(
                _comparison(
                    changes=[
                        _change(
                            "modified",
                            previous_evidence=[_evidence("prev", chunk_id=bad)],
                        )
                    ]
                )
            )
        with pytest.raises(ValidationError):
            load_comparison(
                _comparison(
                    previous_filing=_filing("prev", document_id=bad), changes=[]
                )
            )


def test_naive_timestamp_rejected():
    """Fixture 14: created_at must be timezone-aware."""
    with pytest.raises(ValidationError, match="timezone"):
        load_comparison(
            _comparison(created_at="2026-07-01T12:00:00", changes=[])
        )


def test_unknown_enum_values_rejected():
    """Fixture 15: unknown vocabulary values are rejected everywhere."""
    with pytest.raises(ValidationError, match="change_type"):
        load_comparison(_comparison(changes=[_change("renamed")]))
    with pytest.raises(ValidationError, match="category"):
        load_comparison(
            _comparison(changes=[_change("modified", category="liquidity")])
        )
    with pytest.raises(ValidationError, match="status"):
        load_comparison(
            _comparison(
                changes=[
                    _change(
                        "modified",
                        validation=[
                            {
                                "check": "evidence_presence",
                                "status": "maybe",
                                "reason_code": None,
                                "detail": "x",
                                "validator_version": None,
                            }
                        ],
                    )
                ]
            )
        )
    with pytest.raises(ValidationError, match="check"):
        load_comparison(
            _comparison(
                changes=[
                    _change(
                        "modified",
                        validation=[
                            {
                                "check": "vibes",
                                "status": "passed",
                                "reason_code": None,
                                "detail": "x",
                                "validator_version": None,
                            }
                        ],
                    )
                ]
            )
        )
    with pytest.raises(ValidationError, match="decision"):
        load_comparison(
            _comparison(
                risk={"decision": "escalated", "risk_score": 0.5, "risk_level": "low"},
                changes=[],
            )
        )
    with pytest.raises(ValidationError, match="status"):
        load_comparison(
            _comparison(review={"status": "assigned"}, changes=[])
        )
    with pytest.raises(ValidationError, match="schema_version"):
        load_comparison(_comparison(schema_version="comparison.v2", changes=[]))


def test_unknown_fields_rejected():
    """extra='forbid': producer typos fail instead of persisting silently."""
    data = _comparison(changes=[])
    data["materiality"] = "high"
    with pytest.raises(ValidationError, match="materiality"):
        load_comparison(data)

    change = _change("modified")
    change["confidence"] = 0.9
    with pytest.raises(ValidationError, match="confidence"):
        load_comparison(_comparison(changes=[change]))


# --- Validation checks, summary, risk, and review invariants ----------------


def test_undetermined_requires_reason_and_forbids_it_elsewhere():
    with pytest.raises(ValidationError, match="undetermined_reason"):
        load_comparison(
            _comparison(
                changes=[_change("undetermined", undetermined_reason=None)]
            )
        )
    with pytest.raises(ValidationError, match="undetermined_reason"):
        load_comparison(
            _comparison(
                changes=[_change("undetermined", undetermined_reason="  ")]
            )
        )
    with pytest.raises(ValidationError, match="only valid when"):
        load_comparison(
            _comparison(
                changes=[_change("modified", undetermined_reason="stray")]
            )
        )


def test_failed_check_requires_reason_code():
    check = {
        "check": "numeric_consistency",
        "status": "failed",
        "reason_code": None,
        "detail": "Totals in the two excerpts disagree.",
        "validator_version": "numeric_consistency.v1",
    }
    with pytest.raises(ValidationError, match="reason_code"):
        ValidationCheck.model_validate(check)
    check["reason_code"] = "numeric_mismatch"
    assert ValidationCheck.model_validate(check).status == "failed"


def test_duplicate_validation_checks_rejected():
    check = {
        "check": "evidence_presence",
        "status": "passed",
        "reason_code": None,
        "detail": "Evidence present on both sides.",
        "validator_version": "evidence_presence.v1",
    }
    with pytest.raises(ValidationError, match="duplicate validation check"):
        FilingChange.model_validate(_change("modified", validation=[check, check]))


def test_validation_summary_must_match_changes():
    change = _change(
        "modified",
        validation=[
            {
                "check": "evidence_presence",
                "status": "passed",
                "reason_code": None,
                "detail": "Evidence present on both sides.",
                "validator_version": "evidence_presence.v1",
            }
        ],
    )
    data = _comparison(changes=[change])
    data["validation_summary"] = {
        "total_checks": 0,
        "passed": 0,
        "failed": 0,
        "not_run": 0,
        "not_applicable": 0,
    }
    with pytest.raises(ValidationError, match="does not match the checks"):
        load_comparison(data)

    # Internally inconsistent totals fail on the summary model itself.
    data["validation_summary"] = {
        "total_checks": 5,
        "passed": 1,
        "failed": 0,
        "not_run": 0,
        "not_applicable": 0,
    }
    with pytest.raises(ValidationError, match="total_checks"):
        load_comparison(data)


def test_summarize_validation_helper_matches_model_tally():
    changes = [
        FilingChange.model_validate(
            _change(
                "modified",
                validation=[
                    {
                        "check": "evidence_presence",
                        "status": "passed",
                        "reason_code": None,
                        "detail": "ok",
                        "validator_version": "evidence_presence.v1",
                    },
                    {
                        "check": "numeric_consistency",
                        "status": "not_run",
                        "reason_code": None,
                        "detail": "validator not implemented",
                        "validator_version": None,
                    },
                ],
            )
        )
    ]
    summary = summarize_validation(changes)
    assert summary.total_checks == 2
    assert summary.passed == 1
    assert summary.not_run == 1
    assert summary.failed == 0
    assert summary.not_applicable == 0


def test_section_key_outside_scope_rejected():
    with pytest.raises(ValidationError, match="not in section_scope"):
        load_comparison(
            _comparison(
                changes=[_change("modified", section_key="item_7_mdna")]
            )
        )


def test_risk_placeholder_coherence():
    """not_evaluated carries nothing; evaluated decisions carry score+level."""
    with pytest.raises(ValidationError, match="not_evaluated"):
        load_comparison(
            _comparison(
                risk={"decision": "not_evaluated", "risk_score": 0.2},
                changes=[],
            )
        )
    with pytest.raises(ValidationError, match="requires both"):
        load_comparison(
            _comparison(risk={"decision": "returned"}, changes=[])
        )
    result = load_comparison(
        _comparison(
            risk={
                "decision": "returned",
                "reason_codes": [],
                "risk_score": 0.12,
                "risk_level": "low",
            },
            changes=[],
        )
    )
    assert result.risk.risk_level == "low"


def test_risk_review_cross_coherence():
    """Review routing cannot precede risk; a hold requires a review state."""
    with pytest.raises(ValidationError, match="cannot\\s+precede risk evaluation"):
        load_comparison(
            _comparison(
                review={"status": "pending", "review_id": "review_x"},
                changes=[],
            )
        )
    with pytest.raises(ValidationError, match="requires a review state"):
        load_comparison(
            _comparison(
                risk={
                    "decision": "held_for_review",
                    "reason_codes": ["removed_risk_factor"],
                    "risk_score": 0.8,
                    "risk_level": "high",
                },
                changes=[],
            )
        )
    with pytest.raises(ValidationError, match="not_required"):
        load_comparison(
            _comparison(
                review={"status": "not_required", "review_id": "review_x"},
                changes=[],
            )
        )


# --- Serialization, round-trip, JSON schema, golden file --------------------


def test_json_round_trip_preserves_everything():
    """Fixture 16: dump → JSON text → parse loses nothing."""
    changes = [
        _change("modified", change_id="chg-0001"),
        _change("added", change_id="chg-0002"),
        _change("removed", change_id="chg-0003"),
        _change("undetermined", change_id="chg-0004"),
    ]
    original = load_comparison(_comparison(changes=changes))

    dumped = dump_comparison(original)
    text = json.dumps(dumped)
    reparsed = load_comparison(text)

    assert reparsed == original
    assert dump_comparison(reparsed) == dumped  # deterministic re-dump
    # Enum, date, and evidence data survive as the correct types/values.
    assert {c.change_type for c in reparsed.changes} == set(CHANGE_TYPES)
    assert reparsed.previous_filing.filing_date == date(2024, 2, 20)
    assert reparsed.created_at.tzinfo is not None
    assert (
        reparsed.changes[0].previous_evidence[0].chunk_id
        == _EVIDENCE["prev"]["chunk_id"]
    )
    # JSON-compatible: only plain types after mode="json" dumping.
    assert json.loads(json.dumps(dumped, sort_keys=True)) == json.loads(
        json.dumps(dumped, sort_keys=True)
    )


def test_model_json_schema_pins_required_fields_and_enums():
    """No schemas/ artifact convention exists; pin the generated schema here."""
    schema = ComparisonResult.model_json_schema()

    assert schema["required"] == [
        "comparison_id",
        "previous_filing",
        "current_filing",
        "section_scope",
        "validation_summary",
        "created_at",
        "producer",
    ]
    assert schema["properties"]["schema_version"]["const"] == COMPARISON_SCHEMA_VERSION

    defs = schema["$defs"]
    assert defs["FilingChange"]["properties"]["change_type"]["enum"] == list(
        CHANGE_TYPES
    )
    assert defs["FilingChange"]["properties"]["category"]["const"] == "risk_factor"
    assert CHANGE_CATEGORIES == ("risk_factor",)
    assert defs["ValidationCheck"]["properties"]["status"]["enum"] == list(
        VALIDATION_STATUSES
    )
    assert defs["ValidationCheck"]["properties"]["check"]["enum"] == list(
        VALIDATION_CHECK_NAMES
    )
    assert defs["RiskDecision"]["properties"]["decision"]["enum"] == list(
        COMPARISON_DECISIONS
    )
    assert defs["ReviewState"]["properties"]["status"]["enum"] == list(
        REVIEW_STATUSES
    )
    # Identity vs registry-deferred fields on the filing reference.
    assert defs["FilingReference"]["required"] == [
        "document_id",
        "company_key",
        "company_name",
        "form_type",
        "period_end",
        "source_name",
    ]
    assert "filing_date" not in defs["FilingReference"]["required"]
    assert defs["EvidenceReference"]["required"] == [
        "document_id",
        "chunk_id",
        "source_name",
        "section_key",
        "excerpt",
    ]


def test_golden_fixture_parses_and_redumps_identically():
    """The checked-in golden file is the wire-format pin for comparison.v1."""
    text = GOLDEN_PATH.read_text(encoding="utf-8")
    stored = json.loads(text)

    result = load_comparison(text)
    assert dump_comparison(result) == stored

    # The golden document exercises every change type and both placeholder-vs-
    # evaluated shapes for validation checks.
    assert {c.change_type for c in result.changes} == set(CHANGE_TYPES)
    statuses = {
        check.status for change in result.changes for check in change.validation
    }
    assert statuses == set(VALIDATION_STATUSES)
    assert result.risk.decision == "held_for_review"
    assert result.review.status == "pending"
    assert summarize_validation(result.changes) == result.validation_summary
