"""Item 1A unit-heading grammar v3 (``item1a_units.v3``) and its versioning.

Scope of this suite, stated once:

- The GRAMMAR under test segments an already-extracted Item 1A section into
  risk-factor units. It is owned by ``comparison_detector`` and is NOT the SEC
  HTML section extractor (``loaders/sec_headings.py``, sec_html_item_headings.v2),
  which this change does not touch.
- Every fixture is hand-written and generic. No issuer name, CIK, accession,
  filename, source hash, pair id, or filing excerpt from any real corpus
  appears here, and a test below asserts the parser module itself carries no
  such rule.
- The frozen v2 grammar must stay reproducible and byte-identical in behavior;
  v3 is a distinct identity (item1a_detector.v3 / comparison_workflow.v3).
  The committed v2 holdout artifacts keep their recorded v2 identity and are
  never recomputed by anything in this suite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import comparison_detector as cd
import comparison_store
import real_filing_benchmark as rfb
from comparison_detector import (
    DEFAULT_UNIT_GRAMMAR,
    UNIT_GRAMMAR_V2,
    UNIT_GRAMMAR_V3,
    DetectionError,
    REASON_UNIT_GRAMMAR_UNKNOWN,
    SectionChunk,
    SectionLoad,
    align_units,
    detect_changes,
    extract_units,
    unit_identity,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOLDOUT_DIR = REPO_ROOT / "benchmarks" / "real_filing_holdout_v1"
DEV_DIR = REPO_ROOT / "benchmarks" / "real_filing_v1"

FILING_ID = "fictional-co:10-k:2025-12-31"
BODY = (
    "We depend on a concentrated set of suppliers and counterparties, and an "
    "interruption at any of them could delay deliveries and reduce revenue."
)


def _chunks(text: str, filing_id: str = FILING_ID) -> list[SectionChunk]:
    return [
        SectionChunk(
            chunk_id="fixture.html:1:aaaaaaaaaaaa",
            text=text,
            filing_id=filing_id,
            source_name="fixture.html",
            chunk_seq=0,
            page=1,
            section_title="ITEM 1A. RISK FACTORS",
        )
    ]


def _units(text: str, **kwargs):
    return extract_units(_chunks(text), FILING_ID, **kwargs)


def _section(*headings: str, preamble: str | None = "Introductory fixture text.") -> str:
    lines = []
    if preamble is not None:
        lines.append(preamble)
    for heading in headings:
        lines.append(heading)
        lines.append(BODY)
    return "\n".join(lines)


# --- Grammar classes: positives ------------------------------------------------

POSITIVE_HEADINGS = [
    # 1-2: existing suffix forms (v2-compatible)
    "Cybersecurity Risk",
    "Market and Liquidity Risks",
    # 3-4: prefix forms
    "Risks Related to Technology",
    "Risk Related to Regulation",
    "Risks Relating to Our Industry",
    # 5-6: general category forms
    "General Risk Factors",
    "General Risk Factor",
    # 7: slash
    "Legal/Regulatory Risks",
    # 8: justified punctuation from the closed set
    "Credit, Liquidity & Funding (Wholesale) Risks",
    "Customers' Data Risks",
    "Customers’ Data Risks",
    "Risks Related to Mergers, Acquisitions & Divestitures",
]


@pytest.mark.parametrize("heading", POSITIVE_HEADINGS)
def test_v3_recognizes_generic_heading_class(heading):
    units = _units(_section(heading))
    assert [u.heading for u in units] == ["Item 1A introductory text", heading]


# --- Grammar classes: negatives ------------------------------------------------

NEGATIVE_CANDIDATES = [
    # 15-16: prose containing the trigger phrases
    "The company faces risks related to market conditions.",
    "Risks related to supply are discussed in this section.",
    "These risk factors could adversely affect results.",
    "General risk factors are described below.",
    "We describe general risk factors below",
    # 17: terminal sentence punctuation
    "Our suppliers may fail to deliver, which would reduce revenue.",
    # 18: overlong candidates (bounds tested exactly further down)
    "X" + "a" * 90 + " Risks",
    "Risks Related to " + "A" + "b" * 90,
    # 19-20: bare designators
    "Risk",
    "Risks",
    # 22: unsupported punctuation
    "Market — Structure Risks",
    "Regulatory: Compliance Risks",
    "Litigation; Settlement Risks",
    # 23: heading-like phrase embedded in prose on one line
    "As described below, Risks Related to Technology",
    # 24: body bullet with risk language
    "- risks related to weather may increase costs",
    # 25: URL / path-like text containing slash
    "https://example.com/risks",
    "docs/build/risks",
    # 26: numeric table row containing "risk"
    "2024 14 12 risk",
    # 27: false prefix
    "Risking Related to Payments",
    # 28: false suffix
    "Risk Management Discussion",
    # lowercase connector is prose, not a title-form heading
    "Risks related to our vendors",
]


@pytest.mark.parametrize("candidate", NEGATIVE_CANDIDATES)
def test_v3_rejects_generic_prose_and_malformed_candidates(candidate):
    units = _units(_section(candidate))
    # The candidate line never becomes a heading: everything stays preamble.
    assert [u.unit_key for u in units] == ["item-1a-preamble"]


def test_empty_and_whitespace_candidates_are_rejected():
    # 21 / 30: an empty candidate, and one whose normalization becomes empty.
    text = "Introductory fixture text.\n\n   \n" + BODY
    units = _units(text)
    assert [u.unit_key for u in units] == ["item-1a-preamble"]


def test_heading_length_bounds_are_exact():
    # Suffix budget is v2's, unchanged: [A-Z] + 2..79 chars + "Risks".
    longest_suffix = "X" + "a" * 79 + "Risks"
    too_long_suffix = "X" + "a" * 80 + "Risks"
    assert [u.heading for u in _units(_section(longest_suffix))][1] == longest_suffix
    assert [u.unit_key for u in _units(_section(too_long_suffix))] == [
        "item-1a-preamble"
    ]
    # Prefix tail budget: capital start + up to 79 further chars.
    longest_prefix = "Risks Related to " + "A" + "b" * 79
    too_long_prefix = "Risks Related to " + "A" + "b" * 80
    assert [u.heading for u in _units(_section(longest_prefix))][1] == longest_prefix
    assert [u.unit_key for u in _units(_section(too_long_prefix))] == [
        "item-1a-preamble"
    ]


# --- Unit boundaries, ordering, determinism ------------------------------------


def test_single_and_multiple_headings_close_units_exactly():
    text = _section("Risks Related to Technology", "General Risk Factors")
    units = _units(text)
    assert [u.unit_key for u in units] == [
        "item-1a-preamble",
        "risks-related-to-technology",
        "general-risk-factors",
    ]
    # Exact closure: each unit starts at its heading line and ends where the
    # next begins; the final unit runs to the section end.
    assert units[1].text.startswith("Risks Related to Technology")
    assert units[1].text.rstrip().endswith(BODY)
    assert units[2].text.startswith("General Risk Factors")
    assert units[2].text.rstrip().endswith(BODY)
    assert "".join(u.text for u in units) == text


def test_preamble_is_explicit_and_absent_when_section_starts_with_a_heading():
    with_preamble = _units(_section("Operational Risks"))
    assert with_preamble[0].unit_key == "item-1a-preamble"
    without_preamble = _units(_section("Operational Risks", preamble=None))
    assert [u.unit_key for u in without_preamble] == ["operational-risks"]


def test_source_order_is_preserved_never_sorted_by_key():
    units = _units(_section("Zulu Risks", "Alpha Risks", "Risks Related to Mango"))
    assert [u.unit_key for u in units] == [
        "item-1a-preamble",
        "zulu-risks",
        "alpha-risks",
        "risks-related-to-mango",
    ]
    assert [u.sequence for u in units] == [0, 1, 2, 3]


def test_extraction_is_deterministic():
    text = _section("Risks Related to Technology", "General Risk Factors")
    first = _units(text)
    second = _units(text)
    assert [(u.unit_key, u.sequence, u.content_hash) for u in first] == [
        (u.unit_key, u.sequence, u.content_hash) for u in second
    ]
    assert [unit_identity("current", u) for u in first] == [
        unit_identity("current", u) for u in second
    ]


def test_mixed_v2_and_v3_heading_styles_in_one_section():
    units = _units(
        _section(
            "Cybersecurity Risk",
            "Risks Related to Technology",
            "General Risk Factors",
            "Legal/Regulatory Risks",
        )
    )
    assert [u.unit_key for u in units] == [
        "item-1a-preamble",
        "cybersecurity-risk",
        "risks-related-to-technology",
        "general-risk-factors",
        "legal-regulatory-risks",
    ]


def test_final_heading_keeps_its_substantive_body():
    units = _units(_section("Risks Related to Technology"))
    assert units[-1].text.rstrip().endswith(BODY)
    assert units[-1].content_hash == cd._content_hash(units[-1].text)


# --- Repeated normalized headings ----------------------------------------------


def test_repeated_headings_stay_distinct_units_with_unique_identities():
    units = _units(
        _section("Business Risks", "Business Risks", "General Risk Factors")
    )
    keys = [u.unit_key for u in units]
    assert keys == [
        "item-1a-preamble",
        "business-risks",
        "business-risks",
        "general-risk-factors",
    ]
    identities = [unit_identity("previous", u) for u in units]
    assert len(set(identities)) == len(units)
    assert identities[1] == "previous:001:business-risks"
    assert identities[2] == "previous:002:business-risks"
    # Distinct occurrences keep their own content.
    assert units[1].content_hash == units[2].content_hash  # same fixture body
    assert units[1].text != ""


def test_alignment_reports_duplicates_ambiguous_and_drops_no_unit():
    previous = _units(_section("Business Risks", "Business Risks"))
    current = _units(_section("Business Risks"))
    pairs, unmatched_previous, unmatched_current, ambiguous = align_units(
        previous, current
    )
    assert ambiguous == ["business-risks"]
    assert pairs == [] or all(
        p.unit_key != "business-risks" and c.unit_key != "business-risks"
        for p, c in pairs
    )
    # The input unit lists still hold every occurrence (nothing collapsed).
    assert [u.unit_key for u in previous].count("business-risks") == 2


def test_serialization_emits_one_change_per_ambiguous_unit():
    prev_load = SectionLoad(
        filing_id="prev-f",
        status="loaded",
        complete=True,
        chunks=_chunks(
            _section("Business Risks", "Business Risks", preamble=None), "prev-f"
        ),
    )
    curr_load = SectionLoad(
        filing_id="curr-f",
        status="loaded",
        complete=True,
        chunks=_chunks(_section("Business Risks", preamble=None), "curr-f"),
    )
    changes = detect_changes(prev_load, curr_load, "prev-f", "curr-f")
    assert [c["change_type"] for c in changes] == ["undetermined"] * 3
    assert len({c["change_id"] for c in changes}) == 3
    reasons = [c["undetermined_reason"] for c in changes]
    assert all(r.startswith(cd.REASON_AMBIGUOUS_UNIT_ALIGNMENT) for r in reasons)
    # Sequence-aware identities, in deterministic side-then-sequence order.
    occurrences = [r.rsplit("occurrence ", 1)[1].rstrip(")") for r in reasons]
    assert occurrences == [
        "previous:000:business-risks",
        "previous:001:business-risks",
        "current:000:business-risks",
    ]
    # And the ids are exactly the deterministic hash of those identities.
    assert [c["change_id"] for c in changes] == [
        cd._change_id("undetermined", occurrence) for occurrence in occurrences
    ]


def test_detection_is_stable_across_runs_for_ambiguous_units():
    def run():
        prev_load = SectionLoad(
            filing_id="prev-f",
            status="loaded",
            complete=True,
            chunks=_chunks(_section("Business Risks", "Business Risks"), "prev-f"),
        )
        curr_load = SectionLoad(
            filing_id="curr-f",
            status="loaded",
            complete=True,
            chunks=_chunks(_section("Business Risks"), "curr-f"),
        )
        return detect_changes(prev_load, curr_load, "prev-f", "curr-f")

    assert [c["change_id"] for c in run()] == [c["change_id"] for c in run()]


# --- Version identities ---------------------------------------------------------


def test_v3_identities_are_explicit_and_distinct():
    assert cd.DETECTOR_VERSION == "item1a_detector.v3"
    assert comparison_store.WORKFLOW_VERSION == "comparison_workflow.v3"
    assert UNIT_GRAMMAR_V2 == "item1a_units.v2"
    assert UNIT_GRAMMAR_V3 == "item1a_units.v3"
    assert DEFAULT_UNIT_GRAMMAR == UNIT_GRAMMAR_V3


def test_unknown_grammar_version_is_refused_with_a_stable_code():
    with pytest.raises(DetectionError) as excinfo:
        _units(_section("Operational Risks"), grammar_version="item1a_units.v99")
    assert excinfo.value.code == REASON_UNIT_GRAMMAR_UNKNOWN
    # The safe message names the version string only — no text, no paths.
    assert "item1a_units.v99" in excinfo.value.message
    assert "Operational" not in excinfo.value.message


# --- Frozen v2 grammar reproducibility ------------------------------------------


def test_v2_regex_is_byte_identical_to_the_frozen_grammar():
    assert cd._HEADING_RE.pattern == r"^[A-Z][A-Za-z0-9 ,&()'’-]{2,79}Risks?$"


@pytest.mark.parametrize(
    "heading",
    [
        "Risks Related to Technology",
        "Risk Related to Regulation",
        "General Risk Factors",
        "General Risk Factor",
        "Legal/Regulatory Risks",
    ],
)
def test_v2_grammar_still_rejects_the_v3_only_classes(heading):
    units = _units(_section(heading), grammar_version=UNIT_GRAMMAR_V2)
    assert [u.unit_key for u in units] == ["item-1a-preamble"]


def test_v2_and_v3_agree_byte_for_byte_on_suffix_only_sections():
    text = _section("Cybersecurity Risk", "Market and Liquidity Risks")
    v2 = _units(text, grammar_version=UNIT_GRAMMAR_V2)
    v3 = _units(text, grammar_version=UNIT_GRAMMAR_V3)
    assert [
        (u.unit_key, u.heading, u.text, u.content_hash, u.sequence) for u in v2
    ] == [(u.unit_key, u.heading, u.text, u.content_hash, u.sequence) for u in v3]


def test_committed_v2_artifacts_keep_their_v2_identity():
    """The frozen evaluation stays bound to v2 and is never reinterpreted."""
    report = json.loads(
        (HOLDOUT_DIR / "gold_evaluation_report.json").read_text(encoding="utf-8")
    )
    assert report["detector_version"] == "item1a_detector.v2"
    assert report["workflow_version"] == "comparison_workflow.v2"
    assert report["generalization_claim_supported"] is False
    config_document = json.loads(
        (HOLDOUT_DIR / "evaluation_config.json").read_text(encoding="utf-8")
    )
    assert config_document["declared_detector_version"] == "item1a_detector.v2"
    assert config_document["declared_workflow_version"] == "comparison_workflow.v2"


# --- No issuer-specific or corpus-specific logic --------------------------------


def test_parser_module_contains_no_issuer_or_corpus_identity_rule():
    source = (REPO_ROOT / "comparison_detector.py").read_text(encoding="utf-8")
    assert "sic-" not in source
    for manifest_name in (
        HOLDOUT_DIR / "manifest.json",
        DEV_DIR / "manifest.json",
    ):
        manifest = json.loads(manifest_name.read_text(encoding="utf-8"))
        for pair in rfb.manifest_pairs(manifest):
            assert pair["pair_id"] not in source
            for _side, payload in rfb.pair_sides(pair):
                for field in ("cik", "accession_number", "sha256", "primary_document"):
                    value = payload.get(field)
                    if isinstance(value, str) and value.strip():
                        assert value not in source


def test_grammar_is_pure_regex_over_plain_text():
    """Unit parsing needs no Chroma, network, embeddings, or LLM: the whole
    suite runs it over plain dataclasses, and the grammar predicates are
    compiled regex objects with no callable dependencies."""
    for predicate in cd._UNIT_GRAMMARS.values():
        assert callable(predicate)
    for pattern in (
        cd._HEADING_RE,
        cd._V3_SUFFIX_HEADING_RE,
        cd._V3_PREFIX_HEADING_RE,
        cd._V3_GENERAL_HEADING_RE,
    ):
        assert isinstance(pattern, re.Pattern)


# --- Packet seam: repeated headings survive by unit id --------------------------


def _fixture_filing_html(year: str, units: list[tuple[str, str]]) -> str:
    body = "".join(
        f"<p><b>{heading}</b></p>\n<p>{text}</p>\n" for heading, text in units
    )
    return (
        f"<html><head><title>Fictional 10-K FY{year}</title></head>\n<body>\n"
        "<h1>Item 1A. Risk Factors</h1>\n"
        "<p>The following risk factors should be read together with the rest "
        "of this annual report.</p>\n"
        f"{body}"
        "<h1>Item 1B. Unresolved Staff Comments</h1>\n<p>None.</p>\n"
        "</body></html>\n"
    )


def test_repeated_headings_survive_the_packet_seam_by_unit_id(tmp_path):
    """A built pair whose current filing repeats a heading yields one packet
    row and one machine label PER occurrence, each bound to its own
    sequence-aware unit id — never one collapsed row per heading key."""
    from tests.helpers import real_filing_fixtures as fx
    from scripts import build_real_filing_benchmark as builder
    from scripts import create_real_filing_annotation_packets as packets

    shared_general = (
        "Broad economic conditions could reduce demand across every segment "
        "in which we operate."
    )
    previous_html = _fixture_filing_html(
        "2022",
        [
            ("Supply Chain Risks", "We rely on a single fabrication partner."),
            ("General Risk Factors", shared_general),
        ],
    )
    current_html = _fixture_filing_html(
        "2023",
        [
            ("Supply Chain Risks", "We rely on a single fabrication partner."),
            (
                "Supply Chain Risks",
                "Separately, our logistics providers operate near capacity.",
            ),
            ("General Risk Factors", shared_general),
        ],
    )
    document = fx.single_pair_manifest(
        previous_html=previous_html, current_html=current_html
    )
    layout = rfb.CorpusLayout(tmp_path / "benchmark_data")
    fx.seed_sources(
        layout,
        document,
        {
            ("pair-01", "previous"): previous_html,
            ("pair-01", "current"): current_html,
        },
    )
    record = builder.build_pair(document["pairs"][0], document, layout)
    assert record["previous"]["extraction_outcome"] == rfb.EXTRACTION_EXTRACTED
    assert record["current"]["extraction_outcome"] == rfb.EXTRACTION_EXTRACTED

    packet, annotation = packets.build_packet("pair-01", layout, document)

    ambiguous_rows = [
        row
        for row in packet["alignments"]
        if row["alignment_basis"] == "ambiguous_heading"
    ]
    # One row per occurrence: one previous + two current supply-chain units.
    assert len(ambiguous_rows) == 3
    bound_ids = [
        row["previous_unit_id"] or row["current_unit_id"] for row in ambiguous_rows
    ]
    assert len(set(bound_ids)) == 3
    assert all(
        row["machine_proposed_change_type"] == "undetermined"
        and (row["machine_proposed_undetermined_reason"] or "").startswith(
            cd.REASON_AMBIGUOUS_UNIT_ALIGNMENT
        )
        for row in ambiguous_rows
    )

    # Every machine label binds a distinct label id and unit-id pair, so the
    # duplicate-heading units reach annotation (and any later evaluator) as
    # separate labels keyed by unit id.
    label_bindings = [
        (label["previous_unit_id"], label["current_unit_id"])
        for label in annotation["labels"]
    ]
    assert len(set(label_bindings)) == len(label_bindings)
    label_ids = [label["label_id"] for label in annotation["labels"]]
    assert len(set(label_ids)) == len(label_ids)
    supply_labels = [
        binding
        for binding in label_bindings
        if "supply-chain-risks" in ((binding[0] or "") + (binding[1] or ""))
    ]
    assert len(supply_labels) == 3


# --- CI pinning -----------------------------------------------------------------


def test_this_suite_is_pinned_into_the_required_ci_check():
    """The merge-blocking check must run this suite: a grammar regression that
    CI never executes is a regression CI cannot block. The branch-protection
    contract (workflow/job/name) stays untouched by adding a suite."""
    import yaml

    workflow_path = REPO_ROOT / ".github" / "workflows" / "comparison-regression.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    assert workflow["name"] == "comparison-regression"
    assert list(workflow["jobs"]) == ["comparison-regression"]
    job = workflow["jobs"]["comparison-regression"]
    assert job["name"] == "comparison-regression"
    runs = "\n".join(
        step["run"]
        for step in job["steps"]
        if isinstance(step.get("run"), str) and "pytest" in step["run"]
    )
    assert "tests/test_item1a_unit_parser_v3.py" in runs
