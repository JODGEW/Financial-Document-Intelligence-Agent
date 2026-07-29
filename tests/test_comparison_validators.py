"""Controlled fixtures for the three comparison validators.

Pure-function tests over comparison_validators with a fake chunk resolver —
no Chroma, no detector, fully offline. The case table at the bottom doubles
as the controlled validator evaluation (accuracy / false-pass / false-fail /
not-applicable counts with explicit denominators). Controlled fixtures, not a
real-filing benchmark.
"""

import pytest

import comparison_validators as cv

PREV = "acme-corporation:10-k:2024-12-31"
CURR = "acme-corporation:10-k:2025-12-31"


def _resolver(chunks):
    """chunks: {chunk_id: (filing_id, text)} -> resolve callable."""
    index = {
        chunk_id: {"filing_id": filing_id, "text": text}
        for chunk_id, (filing_id, text) in chunks.items()
    }
    return index.get


def _ref(chunk_id, document_id):
    return {"chunk_id": chunk_id, "document_id": document_id}


def _change(change_type, summary, prev_refs=(), curr_refs=(), reason=None):
    return {
        "change_type": change_type,
        "summary": summary,
        "previous_evidence": list(prev_refs),
        "current_evidence": list(curr_refs),
        "undetermined_reason": reason,
    }


_PREV_CYBER = (
    "Cybersecurity and Data Security Risks We carry cyber liability insurance "
    "with aggregate coverage of $35 million per incident. Our ten largest "
    "clients collectively accounted for 41% of total revenue, up 1,234 "
    "basis-point-years in aggregate."
)
_CURR_CYBER = (
    "Cybersecurity and Data Security Risks We carry cyber liability insurance "
    "with aggregate coverage of $50 million per incident. Our ten largest "
    "clients collectively accounted for 38% of total revenue across 210 "
    "accounts on page 44."
)

_BASE_CHUNKS = {
    "prev.pdf:1:aaa": (PREV, _PREV_CYBER),
    "curr.pdf:1:bbb": (CURR, _CURR_CYBER),
}
_PREV_REF = _ref("prev.pdf:1:aaa", PREV)
_CURR_REF = _ref("curr.pdf:1:bbb", CURR)


def _run(validator, change, chunks=None):
    return validator(
        change,
        resolve=_resolver(_BASE_CHUNKS if chunks is None else chunks),
        previous_filing_id=PREV,
        current_filing_id=CURR,
    )


# --- Focused semantic tests ---------------------------------------------------


def test_citation_heading_supported_both_sides():
    """Fixture 1."""
    check = _run(
        cv.citation_support,
        _change(
            "modified",
            "Risk factor 'Cybersecurity and Data Security Risks' changed "
            "between the two filing periods.",
            [_PREV_REF],
            [_CURR_REF],
        ),
    )
    assert (check["status"], check["reason_code"]) == (
        "passed",
        "citation_summary_supported",
    )
    assert check["validator_version"] == "citation_support.v1"


def test_citation_heading_absent_from_required_side():
    """Fixture 2: heading present current-side only -> modified fails."""
    chunks = dict(_BASE_CHUNKS)
    chunks["prev.pdf:1:aaa"] = (PREV, "Entirely different content, no heading.")
    check = _run(
        cv.citation_support,
        _change(
            "modified",
            "Risk factor 'Cybersecurity and Data Security Risks' changed "
            "between the two filing periods.",
            [_PREV_REF],
            [_CURR_REF],
        ),
        chunks,
    )
    assert (check["status"], check["reason_code"]) == (
        "failed",
        "citation_heading_not_supported",
    )
    assert "previous" in check["detail"]


def test_citation_wrong_side_and_unresolvable():
    """Fixtures 3-4."""
    swapped = _run(
        cv.citation_support,
        _change("removed", "Risk factor 'Cybersecurity and Data Security Risks' "
                "appears in the previous filing and was not found in the "
                "complete current Item 1A section.",
                [_ref("curr.pdf:1:bbb", PREV)], []),
    )
    assert (swapped["status"], swapped["reason_code"]) == (
        "failed",
        "citation_wrong_side",
    )

    missing = _run(
        cv.citation_support,
        _change("added", "Risk factor 'X' appears in the current filing and "
                "was not found in the complete previous Item 1A section.",
                [], [_ref("gone.pdf:9:zzz", CURR)]),
    )
    assert (missing["status"], missing["reason_code"]) == (
        "failed",
        "citation_evidence_unresolvable",
    )


def test_citation_undetermined_reason_consistency():
    """Fixture 17: coherent reason passes; contradicted shape fails."""
    coherent = _run(
        cv.citation_support,
        _change(
            "undetermined",
            "The Item 1A section could not be compared because the previous "
            "filing's Item 1A section was unavailable.",
            [], [],
            reason="previous_section_missing: the previous filing's Item 1A "
            "section was unavailable",
        ),
    )
    assert coherent["status"] == "passed"

    contradicted = _run(
        cv.citation_support,
        _change(
            "undetermined",
            "The Item 1A section could not be compared.",
            [_PREV_REF], [],
            reason="previous_section_missing: claimed missing yet evidenced",
        ),
    )
    assert (contradicted["status"], contradicted["reason_code"]) == (
        "failed",
        "citation_undetermined_reason_mismatch",
    )

    unknown_code = _run(
        cv.citation_support,
        _change("undetermined", "Could not compare.", [], [], reason="vibes: ?"),
    )
    assert unknown_code["status"] == "failed"


def test_numeric_not_applicable_without_claim():
    """No numeric claim -> not_applicable, never passed."""
    check = _run(
        cv.numeric_consistency,
        _change("modified", "Risk factor 'Cybersecurity and Data Security "
                "Risks' changed between the two filing periods.",
                [_PREV_REF], [_CURR_REF]),
    )
    assert (check["status"], check["reason_code"]) == (
        "not_applicable",
        "no_numeric_claim",
    )


def test_numeric_boundary_and_normalization():
    """Fixtures 6-8: 21 never matches 210; % kind-strict; commas normalize."""
    # "21" appears nowhere; "210" does. Substring matching would false-pass.
    trap = _run(
        cv.numeric_consistency,
        _change("added", "Risk factor 'X' appears in the current filing with "
                "21 new accounts.", [], [_CURR_REF]),
    )
    assert (trap["status"], trap["reason_code"]) == (
        "failed",
        "numeric_value_unsupported",
    )
    assert "21" in trap["detail"]

    # Percent normalization + kind strictness: 38% supported; but a plain 44
    # claim may match "page 44" text tokens, while 44% must NOT.
    percent = _run(
        cv.numeric_consistency,
        _change("added", "Risk factor 'X' appears in the current filing at "
                "38% of revenue.", [], [_CURR_REF]),
    )
    assert percent["status"] == "passed"
    percent_kind = _run(
        cv.numeric_consistency,
        _change("added", "Risk factor 'X' appears in the current filing at "
                "44% of revenue.", [], [_CURR_REF]),
    )
    assert percent_kind["status"] == "failed"  # only plain 44 exists in text

    comma = _run(
        cv.numeric_consistency,
        _change("removed", "Risk factor 'X' appears in the previous filing "
                "citing 1234 basis-point-years.", [_PREV_REF], []),
    )
    assert comma["status"] == "passed"  # evidence has "1,234"


def test_numeric_side_attribution():
    """Fixture 10: from/to pins previous/current; swapped values fail."""
    correct = _run(
        cv.numeric_consistency,
        _change("modified", "Risk factor 'Cybersecurity and Data Security "
                "Risks' coverage increased from $35 to $50.",
                [_PREV_REF], [_CURR_REF]),
    )
    assert correct["status"] == "passed"

    swapped = _run(
        cv.numeric_consistency,
        _change("modified", "Risk factor 'Cybersecurity and Data Security "
                "Risks' coverage increased from $50 to $35.",
                [_PREV_REF], [_CURR_REF]),
    )
    assert (swapped["status"], swapped["reason_code"]) == (
        "failed",
        "numeric_value_unsupported",
    )

    ambiguous = _run(
        cv.numeric_consistency,
        _change("modified", "Risk factor 'Cybersecurity and Data Security "
                "Risks' now cites $50.", [_PREV_REF], [_CURR_REF]),
    )
    assert (ambiguous["status"], ambiguous["reason_code"]) == (
        "failed",
        "numeric_attribution_ambiguous",
    )


def test_direction_semantics():
    """Fixtures 11-16."""
    increase = _run(
        cv.direction_consistency,
        _change("modified", "Risk factor 'C' coverage increased from $35 to "
                "$50.", [_PREV_REF], [_CURR_REF]),
    )
    assert (increase["status"], increase["reason_code"]) == (
        "passed",
        "direction_supported",
    )

    decrease = _run(
        cv.direction_consistency,
        _change("modified", "Risk factor 'C' concentration decreased from 41% "
                "to 38%.", [_PREV_REF], [_CURR_REF]),
    )
    assert decrease["status"] == "passed"

    inverted = _run(
        cv.direction_consistency,
        _change("modified", "Risk factor 'C' coverage increased from $50 to "
                "$35.", [_PREV_REF], [_CURR_REF]),
    )
    assert (inverted["status"], inverted["reason_code"]) == (
        "failed",
        "direction_inverted",
    )

    no_direction = _run(
        cv.direction_consistency,
        _change("modified", "Risk factor 'C' changed between the two filing "
                "periods.", [_PREV_REF], [_CURR_REF]),
    )
    assert (no_direction["status"], no_direction["reason_code"]) == (
        "not_applicable",
        "no_directional_claim",
    )

    unverifiable = _run(
        cv.direction_consistency,
        _change("modified", "Risk factor 'C' increased sharply.",
                [_PREV_REF], [_CURR_REF]),
    )
    assert (unverifiable["status"], unverifiable["reason_code"]) == (
        "failed",
        "direction_unverifiable",
    )

    unchanged_claim = _run(
        cv.direction_consistency,
        _change("modified", "Risk factor 'C' is unchanged.",
                [_PREV_REF], [_CURR_REF]),
    )
    assert unchanged_claim["status"] == "failed"

    added_ok = _run(
        cv.direction_consistency,
        _change("added", "Risk factor 'X' appears in the current filing and "
                "was not found in the complete previous Item 1A section.",
                [], [_CURR_REF]),
    )
    assert (added_ok["status"], added_ok["reason_code"]) == (
        "passed",
        "direction_supported",
    )

    added_inverted = _run(
        cv.direction_consistency,
        _change("added", "Risk factor 'X' appears in the previous filing and "
                "was not found in the complete current Item 1A section.",
                [], [_CURR_REF]),
    )
    assert (added_inverted["status"], added_inverted["reason_code"]) == (
        "failed",
        "direction_inverted",
    )

    removed_ok = _run(
        cv.direction_consistency,
        _change("removed", "Risk factor 'X' appears in the previous filing "
                "and was not found in the complete current Item 1A section.",
                [_PREV_REF], []),
    )
    assert removed_ok["status"] == "passed"

    undetermined = _run(
        cv.direction_consistency,
        _change("undetermined", "Could not be compared.", [], [],
                reason="previous_section_missing: unavailable"),
    )
    assert (undetermined["status"], undetermined["reason_code"]) == (
        "not_applicable",
        "undetermined_change",
    )

    undetermined_contradiction = _run(
        cv.direction_consistency,
        _change("undetermined", "Coverage increased materially.", [], [],
                reason="previous_section_missing: unavailable"),
    )
    assert undetermined_contradiction["status"] == "failed"


def test_details_are_safe():
    """Fixture 18: no paths, no full excerpts, bounded length."""
    cases = [
        _run(cv.citation_support, _change(
            "modified",
            "Risk factor 'Cybersecurity and Data Security Risks' changed "
            "between the two filing periods.", [_PREV_REF], [_CURR_REF])),
        _run(cv.numeric_consistency, _change(
            "added", "Risk factor 'X' appears in the current filing with 21 "
            "new accounts.", [], [_CURR_REF])),
        _run(cv.direction_consistency, _change(
            "modified", "Risk factor 'C' coverage increased from $50 to $35.",
            [_PREV_REF], [_CURR_REF])),
    ]
    for check in cases:
        detail = check["detail"]
        assert "/" not in detail  # no filesystem paths
        assert len(detail) <= 200
        assert _PREV_CYBER[:60] not in detail  # never full evidence text
        assert check["reason_code"]
        assert check["validator_version"]


def test_numeric_token_parsing_primitives():
    """Normalization contract: commas, $, %, decimals; kind separation."""
    from decimal import Decimal

    assert cv.parse_numeric_token("1,234") == ("plain", Decimal("1234"))
    assert cv.parse_numeric_token("$50") == ("currency", Decimal("50"))
    assert cv.parse_numeric_token("38%") == ("percent", Decimal("38"))
    assert cv.parse_numeric_token("7.20%") == ("percent", Decimal("7.2"))
    assert cv.parse_numeric_token("word") is None

    numbers = cv.extract_numbers("21 accounts of 210 at 38% and $1,234.50")
    assert ("plain", Decimal("21")) in numbers
    assert ("plain", Decimal("210")) in numbers
    assert ("percent", Decimal("38")) in numbers
    assert ("currency", Decimal("1234.5")) in numbers
    # Boundary safety: "21" is extracted only as its own token, never carved
    # out of "210".
    assert numbers.count(("plain", Decimal("21"))) == 1


# --- Controlled validator evaluation (metrics with explicit denominators) -----

# (name, validator, change, chunks_override, expected_status)
_EVAL_CASES = [
    ("citation both sides", cv.citation_support,
     _change("modified", "Risk factor 'Cybersecurity and Data Security Risks' "
             "changed between the two filing periods.", [_PREV_REF], [_CURR_REF]),
     None, "passed"),
    ("citation missing side", cv.citation_support,
     _change("modified", "Risk factor 'Cybersecurity and Data Security Risks' "
             "changed between the two filing periods.", [_PREV_REF], [_CURR_REF]),
     {"prev.pdf:1:aaa": (PREV, "no heading here"),
      "curr.pdf:1:bbb": (CURR, _CURR_CYBER)}, "failed"),
    ("citation wrong side", cv.citation_support,
     _change("removed", "Risk factor 'Cybersecurity and Data Security Risks' "
             "appears in the previous filing and was not found in the complete "
             "current Item 1A section.", [_ref("curr.pdf:1:bbb", PREV)], []),
     None, "failed"),
    ("citation unresolvable", cv.citation_support,
     _change("added", "Risk factor 'X' appears in the current filing and was "
             "not found in the complete previous Item 1A section.",
             [], [_ref("gone:9:z", CURR)]),
     None, "failed"),
    ("citation undetermined coherent", cv.citation_support,
     _change("undetermined", "Could not be compared.", [], [],
             reason="current_section_missing: unavailable"),
     None, "passed"),
    ("citation undetermined mismatch", cv.citation_support,
     _change("undetermined", "Could not be compared.", [], [_CURR_REF],
             reason="current_section_missing: unavailable"),
     None, "failed"),
    ("numeric none", cv.numeric_consistency,
     _change("modified", "Risk factor 'C' changed between the two filing "
             "periods.", [_PREV_REF], [_CURR_REF]),
     None, "not_applicable"),
    ("numeric supported", cv.numeric_consistency,
     _change("added", "Risk factor 'X' appears in the current filing at 38% "
             "of revenue.", [], [_CURR_REF]),
     None, "passed"),
    ("numeric substring trap", cv.numeric_consistency,
     _change("added", "Risk factor 'X' appears in the current filing with 21 "
             "new accounts.", [], [_CURR_REF]),
     None, "failed"),
    ("numeric comma", cv.numeric_consistency,
     _change("removed", "Risk factor 'X' appears in the previous filing "
             "citing 1234 basis-point-years.", [_PREV_REF], []),
     None, "passed"),
    ("numeric unsupported", cv.numeric_consistency,
     _change("added", "Risk factor 'X' appears in the current filing at $99.",
             [], [_CURR_REF]),
     None, "failed"),
    ("numeric attribution ok", cv.numeric_consistency,
     _change("modified", "Risk factor 'C' coverage increased from $35 to $50.",
             [_PREV_REF], [_CURR_REF]),
     None, "passed"),
    ("numeric attribution swapped", cv.numeric_consistency,
     _change("modified", "Risk factor 'C' coverage increased from $50 to $35.",
             [_PREV_REF], [_CURR_REF]),
     None, "failed"),
    ("numeric ambiguous", cv.numeric_consistency,
     _change("modified", "Risk factor 'C' now cites $50.",
             [_PREV_REF], [_CURR_REF]),
     None, "failed"),
    ("direction increase", cv.direction_consistency,
     _change("modified", "Risk factor 'C' coverage increased from $35 to $50.",
             [_PREV_REF], [_CURR_REF]),
     None, "passed"),
    ("direction decrease", cv.direction_consistency,
     _change("modified", "Risk factor 'C' concentration decreased from 41% to "
             "38%.", [_PREV_REF], [_CURR_REF]),
     None, "passed"),
    ("direction inverted", cv.direction_consistency,
     _change("modified", "Risk factor 'C' coverage increased from $50 to $35.",
             [_PREV_REF], [_CURR_REF]),
     None, "failed"),
    ("direction none", cv.direction_consistency,
     _change("modified", "Risk factor 'C' changed between the two filing "
             "periods.", [_PREV_REF], [_CURR_REF]),
     None, "not_applicable"),
    ("direction added", cv.direction_consistency,
     _change("added", "Risk factor 'X' appears in the current filing and was "
             "not found in the complete previous Item 1A section.",
             [], [_CURR_REF]),
     None, "passed"),
    ("direction removed", cv.direction_consistency,
     _change("removed", "Risk factor 'X' appears in the previous filing and "
             "was not found in the complete current Item 1A section.",
             [_PREV_REF], []),
     None, "passed"),
    ("direction undetermined", cv.direction_consistency,
     _change("undetermined", "Could not be compared.", [], [],
             reason="previous_section_missing: unavailable"),
     None, "not_applicable"),
]


def test_controlled_validator_evaluation_metrics(capsys):
    """Accuracy / false-pass / false-fail / not-applicable with explicit
    denominators over every fixture case. Nothing is excluded from the
    denominators. Controlled fixtures — not a real-filing benchmark."""
    per_validator = {}
    for name, validator, change, chunks, expected in _EVAL_CASES:
        actual = _run(validator, dict(change), chunks)
        stats = per_validator.setdefault(
            validator.__name__,
            {"total": 0, "correct": 0, "false_pass": 0, "false_fail": 0,
             "not_applicable": 0, "wrong": []},
        )
        stats["total"] += 1
        if actual["status"] == expected:
            stats["correct"] += 1
        else:
            stats["wrong"].append((name, expected, actual["status"]))
        if actual["status"] == "passed" and expected != "passed":
            stats["false_pass"] += 1
        if actual["status"] == "failed" and expected == "passed":
            stats["false_fail"] += 1
        if actual["status"] == "not_applicable":
            stats["not_applicable"] += 1

    print("\nControlled validator evaluation (fixtures, not a benchmark):")
    for validator_name, stats in per_validator.items():
        accuracy = stats["correct"] / stats["total"]
        print(
            f"  {validator_name}: accuracy {stats['correct']}/{stats['total']} "
            f"({accuracy:.2f}) | false-pass {stats['false_pass']} | "
            f"false-fail {stats['false_fail']} | "
            f"not_applicable {stats['not_applicable']}"
        )
        assert stats["false_pass"] == 0, stats["wrong"]
        assert stats["false_fail"] == 0, stats["wrong"]
        assert accuracy == 1.0, stats["wrong"]

    assert set(per_validator) == {
        "citation_support", "numeric_consistency", "direction_consistency"
    }


def test_validator_determinism():
    """Same inputs -> byte-identical checks across repeated runs."""
    for name, validator, change, chunks, _expected in _EVAL_CASES:
        first = _run(validator, dict(change), chunks)
        second = _run(validator, dict(change), chunks)
        assert first == second, name
