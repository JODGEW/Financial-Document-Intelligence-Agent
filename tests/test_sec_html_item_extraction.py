"""Deterministic SEC-style HTML Item heading detection and Item 1A extraction.

Every fixture here is a small, hand-written, obviously synthetic document that
models a *generic* SEC HTML structure — a styled div heading, a table-of-
contents row, a running page header. None of them is a copy of, or an excerpt
from, any real filing, and no rule under test keys on an issuer, accession
number, file name, or content hash.

The suite is offline and credential-free: no network, no AWS, no Bedrock, no
LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("bs4")

import ingest
from loaders import sec_headings as sh
from loaders.html import HTML_PARSER_VERSION, _load

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- Generic fixture construction ---------------------------------------------

_SENTENCE = (
    "The Company may be affected by conditions outside its control, and any "
    "such condition could have a material adverse effect on its business, "
    "financial condition, and results of operations. "
)


def prose(paragraphs: int = 8, marker: str = "") -> str:
    """Bounded synthetic body text, comfortably above MIN_SECTION_CHARS."""
    tag = f"{marker} " if marker else ""
    return "".join(f"<p>{tag}{_SENTENCE * 3}</p>\n" for _ in range(paragraphs))


def document(body: str) -> str:
    return f"<html><head><title>Synthetic Filing</title></head><body>\n{body}\n</body></html>"


def load(tmp_path: Path, html: str, name: str = "synthetic-filing.htm"):
    path = tmp_path / name
    path.write_text(html, encoding="utf-8")
    return _load(str(path))


def sections(docs) -> dict[str, str]:
    """section_title -> page_content for documents that carry a title."""
    return {
        doc.metadata["section_title"]: doc.page_content
        for doc in docs
        if doc.metadata.get("section_title")
    }


def item_1a_key(docs) -> list[str]:
    """Section titles that ingest.section_key_for maps to the Item 1A key."""
    return [
        title
        for title in sections(docs)
        if ingest.section_key_for(title) == ingest.SECTION_KEY_ITEM_1A
    ]


def diagnostics(docs) -> dict:
    for doc in docs:
        if "sec_item1a_outcome" in doc.metadata:
            return doc.metadata
    return {}


def select(html: str):
    """Run detection directly, returning (blocks, candidates, selection)."""
    from bs4 import BeautifulSoup

    return sh.detect_item_sections(BeautifulSoup(html, "lxml"), "1A")


# --- 1-8: heading forms the recognizer must accept ----------------------------


def test_h1_item_1a_remains_supported(tmp_path):
    """Test 1: the pre-existing heading-tag form keeps working."""
    docs = load(
        tmp_path,
        document(
            "<h1>Item 1A. Risk Factors</h1>\n" + prose()
            + "<h1>Item 1B. Unresolved Staff Comments</h1><p>None.</p>"
        ),
    )
    assert item_1a_key(docs) == ["Item 1A. Risk Factors"]


def test_styled_div_and_span_heading_is_detected(tmp_path):
    """Test 2: a div whose text is carried by inner styled spans."""
    docs = load(
        tmp_path,
        document(
            '<div style="font-weight:700"><span>Item 1A.</span>'
            "<span> Risk Factors</span></div>\n" + prose()
            + "<div><span>Item 1B. Unresolved Staff Comments</span></div><p>None.</p>"
        ),
    )
    assert item_1a_key(docs) == ["Item 1A. Risk Factors"]
    assert diagnostics(docs)["sec_item1a_element_tag"] == "div"


def test_paragraph_with_bold_heading_is_detected(tmp_path):
    """Test 3: <p><b>Item 1A. Risk Factors</b></p>."""
    docs = load(
        tmp_path,
        document(
            "<p><b>Item 1A. Risk Factors</b></p>\n" + prose()
            + "<p><b>Item 2. Properties</b></p><p>None.</p>"
        ),
    )
    assert item_1a_key(docs) == ["Item 1A. Risk Factors"]
    assert diagnostics(docs)["sec_item1a_element_tag"] == "p"


def test_table_cell_heading_is_detected(tmp_path):
    """Test 4: the heading rendered as a single table cell."""
    docs = load(
        tmp_path,
        document(
            "<table><tr><td>Item 1A. Risk Factors</td></tr></table>\n" + prose()
            + "<table><tr><td>Item 1B. Unresolved Staff Comments</td></tr></table>"
            "<p>None.</p>"
        ),
    )
    assert item_1a_key(docs) == ["Item 1A. Risk Factors"]
    assert diagnostics(docs)["sec_item1a_element_tag"] == "td"


def test_part_prefix_is_accepted(tmp_path):
    """Test 5: 'Part I, Item 1A. Risk Factors'."""
    docs = load(
        tmp_path,
        document(
            "<div>Part I, Item 1A. Risk Factors</div>\n" + prose()
            + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
        ),
    )
    assert item_1a_key(docs) == ["Part I, Item 1A. Risk Factors"]


@pytest.mark.parametrize(
    "heading",
    [
        "Item 1A. Risk Factors",
        "Item 1A: Risk Factors",
        "ITEM 1A — RISK FACTORS",
        "Item 1A - Risk Factors",
        "Item 1A.Risk Factors.",
        "Item   1A.   Risk   Factors",
        "ITEM 1A. RISK FACTORS",
    ],
)
def test_punctuation_whitespace_and_case_variants(tmp_path, heading):
    """Tests 6 and 7: separator, spacing, and case variation."""
    docs = load(
        tmp_path,
        document(
            f"<div>{heading}</div>\n" + prose()
            + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
        ),
    )
    assert len(item_1a_key(docs)) == 1, heading


def test_non_breaking_spaces_and_entities_are_normalized(tmp_path):
    """Test 8: &nbsp;, &#160;, and a word split across styled spans."""
    docs = load(
        tmp_path,
        document(
            "<div><span>ITEM&nbsp;1A.&#160;RIS</span><span>K FACTORS</span></div>\n"
            + prose()
            + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
        ),
    )
    # Inline runs join with no separator, so the split word is read as a
    # browser renders it rather than as "RIS K FACTORS".
    assert item_1a_key(docs) == ["Item 1A. RISK FACTORS"]


# --- 9-13: navigation and duplicate disambiguation ----------------------------


CONTENTS_ROWS = "".join(
    f"<tr><td>Item {item}.</td><td>{label}</td></tr>"
    for item, label in [
        ("1", "Business"),
        ("1A", "Risk Factors"),
        ("1B", "Unresolved Staff Comments"),
        ("2", "Properties"),
        ("3", "Legal Proceedings"),
        ("4", "Mine Safety Disclosures"),
    ]
)


def test_contents_duplicate_before_substantive_heading(tmp_path):
    """Test 9: a table-of-contents row must not win over the body heading."""
    docs = load(
        tmp_path,
        document(
            f"<table>{CONTENTS_ROWS}</table>\n"
            "<div>Item 1A. Risk Factors</div>\n" + prose()
            + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
        ),
    )
    assert item_1a_key(docs) == ["Item 1A. Risk Factors"]
    meta = diagnostics(docs)
    assert meta["sec_item1a_substantive_count"] == 1
    assert meta["sec_item1a_candidate_count"] > 1


def test_anchor_navigation_duplicate_is_rejected(tmp_path):
    """Test 10: a heading that is wholly a link is a navigation entry."""
    _blocks, candidates, selection = select(
        document(
            '<div><a href="#i1a">Item 1A. Risk Factors</a></div>\n'
            '<div id="i1a">Item 1A. Risk Factors</div>\n' + prose()
            + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
        )
    )
    assert selection.outcome == sh.OUTCOME_EXTRACTED
    rejected = [c for c in candidates if c.classification == sh.CLASS_NAVIGATION]
    assert [c.reason for c in rejected] == [sh.REASON_ANCHOR_BLOCK]
    assert selection.navigation_rejected_count == 1


def test_dense_item_heading_navigation_block_is_rejected(tmp_path):
    """Test 11: many titled Item headings packed together are a contents block."""
    nav = "".join(
        f"<div>Item {item}. {label}</div>"
        for item, label in [
            ("1", "Business"),
            ("1A", "Risk Factors"),
            ("1B", "Unresolved Staff Comments"),
            ("2", "Properties"),
            ("3", "Legal Proceedings"),
            ("4", "Mine Safety Disclosures"),
            ("5", "Market for Common Equity"),
        ]
    )
    _blocks, candidates, selection = select(
        document(
            nav + "\n<div>Item 1A. Risk Factors</div>\n" + prose()
            + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
        )
    )
    assert selection.outcome == sh.OUTCOME_EXTRACTED
    assert selection.substantive_count == 1
    assert selection.navigation_rejected_count == 1
    nav_reasons = {
        c.reason for c in candidates if c.classification == sh.CLASS_NAVIGATION
    }
    assert nav_reasons == {sh.REASON_NAVIGATION_RUN}


def test_substantive_duplicate_selected_through_following_content(tmp_path):
    """Test 12: neither first nor last wins — content volume decides."""
    docs = load(
        tmp_path,
        document(
            "<div>Item 1A. Risk Factors</div><p>See page 14.</p>\n"
            "<div>Item 1. Business</div>" + prose(2)
            + "<div>Item 1A. Risk Factors</div>\n" + prose()
            + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
        ),
    )
    assert item_1a_key(docs) == ["Item 1A. Risk Factors"]
    body = sections(docs)["Item 1A. Risk Factors"]
    assert "See page 14." not in body
    assert diagnostics(docs)["sec_item1a_insufficient_content"] == 1


def test_two_equally_plausible_candidates_are_ambiguous(tmp_path):
    """Test 13: no tie-break exists, so the outcome is ambiguous."""
    docs = load(
        tmp_path,
        document(
            "<div>Item 1A. Risk Factors</div>\n" + prose(marker="First.")
            + "<div>Item 1. Business</div>" + prose(2)
            + "<div>Item 1A. Risk Factors</div>\n" + prose(marker="Second.")
            + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
        ),
    )
    meta = diagnostics(docs)
    assert meta["sec_item1a_outcome"] == sh.OUTCOME_AMBIGUOUS
    assert meta["sec_item1a_reason"] == sh.REASON_MULTIPLE_SUBSTANTIVE
    assert meta["sec_item1a_substantive_count"] == 2
    # An ambiguous outcome must not stamp a section key for either candidate.
    assert item_1a_key(docs) == []


# --- 14-15: text the grammar must refuse --------------------------------------


def test_body_text_mention_does_not_match(tmp_path):
    """Test 14: a sentence referring to Item 1A is not a heading."""
    docs = load(
        tmp_path,
        document(
            "<div>Item 1. Business</div>\n" + prose()
            + "<p>For additional detail see Item 1A. Risk Factors of this "
            "annual report, which describes the risks summarized above and "
            "should be read together with the financial statements.</p>\n"
            + prose()
        ),
    )
    assert item_1a_key(docs) == []
    assert diagnostics(docs)["sec_item1a_outcome"] == sh.OUTCOME_MISSING


def test_standalone_risk_factors_does_not_match(tmp_path):
    """Test 15: 'Risk Factors' without Item identity is never Item 1A."""
    docs = load(
        tmp_path,
        document(
            "<div>Item 1. Business</div>\n" + prose()
            + "<div>Risk Factors</div>\n" + prose()
        ),
    )
    assert item_1a_key(docs) == []
    assert sections(docs).get("Risk Factors") is None


# --- 16-20: section boundaries ------------------------------------------------


@pytest.mark.parametrize(
    "boundary",
    [
        "Item 1B. Unresolved Staff Comments",
        "Item 1C. Cybersecurity",
        "Item 2. Properties",
    ],
)
def test_successor_item_closes_the_section(tmp_path, boundary):
    """Tests 16, 17, 18: Item 1B / 1C / 2 each end Item 1A."""
    docs = load(
        tmp_path,
        document(
            "<div>Item 1A. Risk Factors</div>\n" + prose()
            + f"<div>{boundary}</div>\n" + prose(marker="AFTER-BOUNDARY.")
        ),
    )
    body = sections(docs)["Item 1A. Risk Factors"]
    assert "AFTER-BOUNDARY." not in body
    assert diagnostics(docs)["sec_item1a_boundary_heading"] == boundary


def test_subsequent_part_closes_the_section(tmp_path):
    """Test 19: an explicit later Part ends the section."""
    docs = load(
        tmp_path,
        document(
            "<div>Part I</div>\n<div>Item 1A. Risk Factors</div>\n" + prose()
            + "<div>Part II</div>\n" + prose(marker="AFTER-BOUNDARY.")
        ),
    )
    body = sections(docs)["Item 1A. Risk Factors"]
    assert "AFTER-BOUNDARY." not in body
    assert diagnostics(docs)["sec_item1a_boundary_heading"] == "Part II"


def test_repeated_same_part_page_header_is_not_a_boundary(tmp_path):
    """A running 'Part I' page header must not truncate the section."""
    docs = load(
        tmp_path,
        document(
            "<div>Part I</div>\n<div>Item 1A. Risk Factors</div>\n"
            + prose(4, marker="BEFORE-HEADER.")
            + "<div>Part I</div><div>Item 1A</div>\n"
            + prose(4, marker="AFTER-HEADER.")
            + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
        ),
    )
    body = sections(docs)["Item 1A. Risk Factors"]
    assert "BEFORE-HEADER." in body and "AFTER-HEADER." in body
    assert diagnostics(docs)["sec_item1a_boundary_heading"].startswith("Item 1B")


def test_next_heading_is_excluded_from_section_content(tmp_path):
    """Test 20: the boundary heading belongs to the next section."""
    docs = load(
        tmp_path,
        document(
            "<div>Item 1A. Risk Factors</div>\n" + prose()
            + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
        ),
    )
    body = sections(docs)["Item 1A. Risk Factors"]
    assert "Item 1B." not in body
    assert "Unresolved Staff Comments" not in body


# --- 21-23: honest non-extraction ---------------------------------------------


def test_missing_heading_is_reported(tmp_path):
    """Test 21."""
    docs = load(
        tmp_path,
        document("<div>Item 1. Business</div>\n" + prose() + "<div>Item 2. Properties</div>" + prose()),
    )
    assert item_1a_key(docs) == []
    meta = diagnostics(docs)
    assert meta["sec_item1a_outcome"] == sh.OUTCOME_MISSING
    assert meta["sec_item1a_reason"] == sh.REASON_NO_CANDIDATE


def test_missing_end_boundary_is_ambiguous_not_unbounded(tmp_path):
    """Test 22: no trustworthy end boundary must never consume the rest."""
    docs = load(
        tmp_path,
        document("<div>Item 1A. Risk Factors</div>\n" + prose(20)),
    )
    meta = diagnostics(docs)
    assert meta["sec_item1a_outcome"] == sh.OUTCOME_AMBIGUOUS
    assert meta["sec_item1a_reason"] == sh.REASON_NO_END_BOUNDARY
    assert item_1a_key(docs) == []


def test_insufficient_substantive_content_is_not_extracted(tmp_path):
    """Test 23: a cross-reference line is not a section."""
    docs = load(
        tmp_path,
        document(
            "<div>Item 1. Business</div>" + prose()
            + "<div>Item 1A. Risk Factors</div><p>Not applicable.</p>\n"
            "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
        ),
    )
    meta = diagnostics(docs)
    assert meta["sec_item1a_outcome"] == sh.OUTCOME_MISSING
    assert meta["sec_item1a_reason"] == sh.REASON_NO_SUBSTANTIVE_CANDIDATE
    assert meta["sec_item1a_insufficient_content"] == 1
    assert item_1a_key(docs) == []


# --- 24-28: safety, determinism, and diagnostics ------------------------------


def test_hidden_script_and_style_text_is_ignored(tmp_path):
    """Test 24."""
    html = document(
        "<script>var heading = 'Item 1A. Risk Factors';</script>\n"
        "<style>.x::before { content: 'Item 1A. Risk Factors'; }</style>\n"
        "<noscript><div>Item 1A. Risk Factors</div></noscript>\n"
        '<div style="display:none">Item 1A. Risk Factors</div>\n'
        '<div hidden>Item 1A. Risk Factors</div>\n'
        "<!-- <div>Item 1A. Risk Factors</div> -->\n"
        "<div>Item 1. Business</div>\n" + prose()
    )
    docs = load(tmp_path, html)
    assert item_1a_key(docs) == []
    assert diagnostics(docs)["sec_item1a_candidate_count"] == 0
    combined = "\n".join(doc.page_content for doc in docs)
    assert "var heading" not in combined
    assert "::before" not in combined


def test_extraction_is_deterministic_across_repeated_runs(tmp_path):
    """Test 25."""
    html = document(
        f"<table>{CONTENTS_ROWS}</table>\n"
        "<div>Item 1A. Risk Factors</div>\n" + prose()
        + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
    )
    first = load(tmp_path, html, "run-a.htm")
    second = load(tmp_path, html, "run-b.htm")
    assert [d.page_content for d in first] == [d.page_content for d in second]
    strip = lambda docs: [  # noqa: E731 - local comparison helper
        {k: v for k, v in d.metadata.items() if k != "source"} for d in docs
    ]
    assert strip(first) == strip(second)


def test_parser_version_is_recorded(tmp_path):
    """Test 26."""
    docs = load(
        tmp_path,
        document(
            "<div>Item 1A. Risk Factors</div>\n" + prose()
            + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
        ),
    )
    assert diagnostics(docs)["sec_parser_version"] == HTML_PARSER_VERSION
    assert HTML_PARSER_VERSION == "sec_html_item_headings.v2"


def test_source_bytes_and_hash_are_untouched(tmp_path):
    """Test 27: extraction reads; it never rewrites the source."""
    import hashlib

    html = document(
        "<div>Item 1A. Risk Factors</div>\n" + prose()
        + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
    )
    path = tmp_path / "immutable.htm"
    path.write_text(html, encoding="utf-8")
    before = path.read_bytes()
    digest = hashlib.sha256(before).hexdigest()

    _load(str(path))

    assert path.read_bytes() == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_diagnostics_carry_no_filing_prose(tmp_path):
    """Test 28: diagnostics are counts, codes, and a bounded heading label."""
    docs = load(
        tmp_path,
        document(
            "<div>Item 1A. Risk Factors</div>\n" + prose(marker="SECRET-BODY-TEXT.")
            + "<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>"
        ),
    )
    meta = diagnostics(docs)
    payload = json.dumps({k: v for k, v in meta.items() if k.startswith("sec_")})
    assert "SECRET-BODY-TEXT." not in payload
    assert _SENTENCE.strip() not in payload
    for key, value in meta.items():
        if key.startswith("sec_") and isinstance(value, str):
            assert len(value) <= sh.MAX_HEADING_TEXT_CHARS, key
        if key.startswith("sec_"):
            assert isinstance(value, (str, int, float, bool)), key


# --- 29-30: neighbouring paths that must not move -----------------------------


def test_pdf_section_extraction_behavior_is_unchanged():
    """Test 29: the PDF PART/ITEM marker path is untouched."""
    from langchain_core.documents import Document

    page = Document(
        page_content=(
            "PART I\n"
            "ITEM 1A. RISK FACTORS\n"
            "Our operations are subject to a number of risks.\n"
            "ITEM 1B. UNRESOLVED STAFF COMMENTS\n"
            "None.\n"
        ),
        metadata={"source": "synthetic.pdf", "page": 0},
    )
    chunks, trailing = ingest._split_pdf_page(page, None)
    titles = [c.metadata.get("section_title") for c in chunks]
    assert "ITEM 1A. RISK FACTORS" in titles
    assert trailing == "ITEM 1B. UNRESOLVED STAFF COMMENTS"
    assert ingest.section_key_for("ITEM 1A. RISK FACTORS") == (
        ingest.SECTION_KEY_ITEM_1A
    )


def test_synthetic_comparison_regression_inputs_do_not_use_the_html_loader():
    """Test 30: the regression corpus cannot move when HTML parsing changes.

    The pair fixtures are pre-parsed chunk JSON, and the controlled ``docs/``
    corpus contains no HTML, so neither regression input reaches this loader.
    """
    fixtures = sorted((REPO_ROOT / "tests" / "fixtures" / "comparison_regression").glob("*.json"))
    assert fixtures
    for fixture in fixtures:
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        assert payload["synthetic"] is True
        for filing in payload["filings"]:
            assert filing["chunks"], fixture.name
            assert not filing["source_name"].endswith((".htm", ".html"))

    corpus = REPO_ROOT / "docs"
    assert not list(corpus.glob("*.htm"))
    assert not list(corpus.glob("*.html"))


# --- End-to-end through the benchmark corpus builder --------------------------


def test_sec_shaped_filing_builds_and_detects_end_to_end(tmp_path):
    """A filing with no heading tags at all now reaches the comparison workflow.

    This is the whole point of the change: the same offline builder that
    recorded a null extraction over heading-tag-free filings now extracts,
    unitizes, and runs the existing detector — with no detector, alignment,
    validator, or threshold change.
    """
    import real_filing_benchmark as rfb
    from scripts import build_real_filing_benchmark as builder
    from tests.helpers import real_filing_fixtures as fx

    manifest = fx.single_pair_manifest(
        previous_html=fx.SEC_STYLED_PREVIOUS_HTML,
        current_html=fx.SEC_STYLED_CURRENT_HTML,
    )
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    fx.seed_sources(
        layout,
        manifest,
        {
            ("pair-01", "previous"): fx.SEC_STYLED_PREVIOUS_HTML,
            ("pair-01", "current"): fx.SEC_STYLED_CURRENT_HTML,
        },
    )
    record = builder.build_pair(manifest["pairs"][0], manifest, layout)

    for side in ("previous", "current"):
        payload = record[side]
        assert payload["extraction_outcome"] == rfb.EXTRACTION_EXTRACTED, side
        assert payload["heading_detected"] == "Item 1A. Risk Factors"
        assert payload["selected_element_tag"] == "div"
        assert payload["boundary_heading"].startswith("Item 1B")
        assert payload["extraction_parser_version"] == HTML_PARSER_VERSION
        assert payload["extraction_reason"] == sh.REASON_SELECTED
        # The contents row is seen and rejected, not silently absent.
        assert payload["candidate_count"] >= 2
        assert payload["substantive_candidate_count"] == 1
        assert payload["unit_count"] >= 3

    assert rfb.build_is_evaluable(record) is True
    assert record["execution"]["executed"] is True
    assert record["execution"]["lifecycle"] == "detected"
    assert record["parser_versions"]["html_parser"] == HTML_PARSER_VERSION


def test_build_record_diagnostics_carry_no_section_text(tmp_path):
    """Build-record diagnostics stay bounded: counts, codes, and labels."""
    import real_filing_benchmark as rfb
    from scripts import build_real_filing_benchmark as builder
    from tests.helpers import real_filing_fixtures as fx

    manifest = fx.single_pair_manifest(
        previous_html=fx.SEC_STYLED_PREVIOUS_HTML,
        current_html=fx.SEC_STYLED_CURRENT_HTML,
    )
    layout = rfb.CorpusLayout(tmp_path / "corpus")
    fx.seed_sources(
        layout,
        manifest,
        {
            ("pair-01", "previous"): fx.SEC_STYLED_PREVIOUS_HTML,
            ("pair-01", "current"): fx.SEC_STYLED_CURRENT_HTML,
        },
    )
    record = builder.build_pair(manifest["pairs"][0], manifest, layout)

    diagnostic_keys = (
        "extraction_parser_version",
        "extraction_reason",
        "candidate_count",
        "substantive_candidate_count",
        "navigation_rejected_count",
        "selected_element_tag",
        "boundary_heading",
    )
    for side in ("previous", "current"):
        payload = json.dumps({k: record[side][k] for k in diagnostic_keys})
        assert fx._SEC_FILLER.strip() not in payload
        assert "intrusion events" not in payload
        assert str(tmp_path) not in payload


# --- Grammar unit coverage ----------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Item 1A. Risk Factors", ("1A", None, "Risk Factors")),
        ("PART I, ITEM 1A: RISK FACTORS", ("1A", "I", "RISK FACTORS")),
        ("Item 1A", ("1A", None, "")),
        ("Item 15. Exhibits", ("15", None, "Exhibits")),
        ("Item 7A. Quantitative Disclosures", ("7A", None, "Quantitative Disclosures")),
    ],
)
def test_designator_grammar_accepts_closed_forms(text, expected):
    assert sh.parse_item_designator(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Risk Factors",
        "Items 1A and 1B",
        "Item 1Alpha is not a designator",
        "Item 99. Not A Real Item",
        "Item 1D. Outside The Closed Set",
        "See Item 1A. Risk Factors for detail",
        "Part IX, Item 1A. Risk Factors",
    ],
)
def test_designator_grammar_refuses_everything_else(text):
    assert sh.parse_item_designator(text) is None


def test_item_successors_are_closed_and_ordered():
    successors = sh.item_successors("1A")
    assert successors[:3] == ("1B", "1C", "2")
    assert "1" not in successors
    assert sh.item_successors("16") == ()


def test_control_characters_are_rejected():
    assert sh.normalize_visible_text("Item 1A.\x07 Risk Factors") is None
    assert sh.normalize_visible_text("Item 1A. Risk Factors") == "Item 1A. Risk Factors"
