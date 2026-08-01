"""Controlled local fixtures for the real-filing benchmark suites.

Everything here is SYNTHETIC and obviously so: CIKs are 0000000001-style
sentinels, accession numbers are sequential, and issuer names are fictional.
Nothing in this module names, resembles, or stands in for a real SEC registrant
or a real filing — the benchmark suites must be able to run offline in CI
without a single byte of real filing content existing in the repository.

The HTML documents are small hand-written risk-factor sections. They exercise
the EXISTING ingestion and section-identification path, so the extraction
outcomes the tests assert are the outcomes the real path produces — not a
re-implementation. Two heading shapes are covered, matching the loader's two
strategies: ``<h1>`` headings (generic header-tag sectioning) and the styled
``div``/``span`` form real filings use (SEC Item structure).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import real_filing_benchmark as rfb

SYNTHETIC_CIK = "0000000001"
SYNTHETIC_ISSUER = "Fictional Benchmark Issuer One, Inc."


def _risk_unit(heading: str, body: str) -> str:
    return f"<p><b>{heading}</b></p>\n<p>{body}</p>\n"


PREVIOUS_HTML = """<html><head><title>Fictional 10-K FY2022</title></head>
<body>
<h1>Item 1A. Risk Factors</h1>
<p>The following risk factors should be read together with the rest of this
annual report.</p>
{units}
<h1>Item 1B. Unresolved Staff Comments</h1>
<p>None.</p>
</body></html>
""".format(
    units="".join(
        [
            _risk_unit(
                "Cybersecurity and Data Security Risks",
                "We experienced 3 attempted intrusion events during fiscal 2022 "
                "and maintained cyber insurance coverage of $35 million.",
            ),
            _risk_unit(
                "Concentration and Customer Risks",
                "Our top ten clients accounted for 41% of revenue in fiscal 2022.",
            ),
            _risk_unit(
                "Reference Rate Transition Risks",
                "The transition away from legacy reference rates may affect our "
                "floating-rate obligations.",
            ),
        ]
    )
)

CURRENT_HTML = """<html><head><title>Fictional 10-K FY2023</title></head>
<body>
<h1>Item 1A. Risk Factors</h1>
<p>The following risk factors should be read together with the rest of this
annual report.</p>
{units}
<h1>Item 1B. Unresolved Staff Comments</h1>
<p>None.</p>
</body></html>
""".format(
    units="".join(
        [
            _risk_unit(
                "Cybersecurity and Data Security Risks",
                "We experienced 5 attempted intrusion events during fiscal 2023 "
                "and increased cyber insurance coverage to $50 million.",
            ),
            _risk_unit(
                "Concentration and Customer Risks",
                "Our top ten clients accounted for 41% of revenue in fiscal 2022.",
            ),
            _risk_unit(
                "Artificial Intelligence and Model Risks",
                "We began deploying machine-learning models into client-facing "
                "workflows during fiscal 2023.",
            ),
        ]
    )
)

# No Item 1A heading at all: the existing section path stamps no section key,
# so the honest extraction outcome is 'missing'.
NO_SECTION_HTML = """<html><head><title>Fictional 10-K FY2023</title></head>
<body>
<h1>Item 1. Business</h1>
<p>We provide fictional services to fictional clients.</p>
<p><b>Cybersecurity and Data Security Risks</b></p>
<p>Risk narrative that is never marked as an Item 1A section heading.</p>
</body></html>
"""

# Two separate Item 1A headings (a contents entry plus the body section): which
# run is "the" section is not deterministically decidable, so the outcome is
# 'ambiguous'.
AMBIGUOUS_SECTION_HTML = """<html><head><title>Fictional 10-K FY2023</title></head>
<body>
<h1>Item 1A. Risk Factors</h1>
<p>See page 14.</p>
<h1>Item 1. Business</h1>
<p>We provide fictional services to fictional clients.</p>
<h1>Item 1A. Risk Factors</h1>
<p><b>Cybersecurity and Data Security Risks</b></p>
<p>We experienced 5 attempted intrusion events during fiscal 2023.</p>
</body></html>
"""


# --- SEC-shaped HTML (no heading tags anywhere) -------------------------------
#
# A generic model of how a real filing renders structure: a contents table whose
# Item designator and title sit in separate cells, then styled div/span blocks
# for the body headings. Nothing here is copied from any filing; it is the
# shape, not the content.

_SEC_CONTENTS = "<table>" + "".join(
    f"<tr><td><span>Item {item}.</span></td><td><span>{label}</span></td></tr>"
    for item, label in [
        ("1", "Business"),
        ("1A", "Risk Factors"),
        ("1B", "Unresolved Staff Comments"),
        ("2", "Properties"),
    ]
) + "</table>"

_SEC_FILLER = (
    "This fictional disclosure exists so the section carries enough substantive "
    "text to be treated as a section rather than a cross-reference line. It "
    "describes no real company and states no real fact. "
)


def _sec_unit(heading: str, body: str) -> str:
    return (
        f'<div style="font-weight:700"><span>{heading}</span></div>\n'
        f"<div><span>{body} {_SEC_FILLER * 3}</span></div>\n"
    )


def _sec_filing(units: list[tuple[str, str]], year: str) -> str:
    return (
        f"<html><head><title>Fictional 10-K FY{year}</title></head><body>\n"
        f"{_SEC_CONTENTS}\n"
        "<div><span>Part I</span></div>\n"
        '<div style="font-weight:700"><span>Item 1A.</span>'
        "<span> Risk Factors</span></div>\n"
        "<div><span>The following risk factors should be read together with "
        "the rest of this annual report.</span></div>\n"
        + "".join(_sec_unit(heading, body) for heading, body in units)
        + '<div style="font-weight:700"><span>Item 1B. Unresolved Staff '
        "Comments</span></div>\n<div><span>None.</span></div>\n"
        "</body></html>\n"
    )


SEC_STYLED_PREVIOUS_HTML = _sec_filing(
    [
        (
            "Cybersecurity and Data Security Risks",
            "We experienced 3 attempted intrusion events during fiscal 2022 and "
            "maintained cyber insurance coverage of $35 million.",
        ),
        (
            "Concentration and Customer Risks",
            "Our top ten clients accounted for 41% of revenue in fiscal 2022.",
        ),
        (
            "Reference Rate Transition Risks",
            "The transition away from legacy reference rates may affect our "
            "floating-rate obligations.",
        ),
    ],
    "2022",
)

SEC_STYLED_CURRENT_HTML = _sec_filing(
    [
        (
            "Cybersecurity and Data Security Risks",
            "We experienced 5 attempted intrusion events during fiscal 2023 and "
            "increased cyber insurance coverage to $50 million.",
        ),
        (
            "Concentration and Customer Risks",
            "Our top ten clients accounted for 41% of revenue in fiscal 2022.",
        ),
        (
            "Artificial Intelligence and Model Risks",
            "We began deploying machine-learning models into client-facing "
            "workflows during fiscal 2023.",
        ),
    ],
    "2023",
)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def side(
    *,
    accession_suffix: str,
    filing_date: str,
    reporting_period: str,
    primary_document: str,
    expected_sha256: str,
    cik: str = SYNTHETIC_CIK,
) -> dict[str, Any]:
    accession = f"{cik}-{accession_suffix}"
    return {
        "accession_number": accession,
        "form": "10-K",
        "filing_date": filing_date,
        "reporting_period": reporting_period,
        "primary_document": primary_document,
        "official_source_url": rfb.canonical_source_url(
            cik, accession, primary_document
        ),
        "expected_sha256": expected_sha256,
    }


def issuer_slate(count: int = 1, *, resolved: int = 1) -> list[dict[str, Any]]:
    """A frozen slate of ``count`` synthetic issuers across five sectors."""
    sectors = [
        "Fictional Sector A",
        "Fictional Sector B",
        "Fictional Sector C",
        "Fictional Sector D",
        "Fictional Sector E",
    ]
    slate = []
    for index in range(count):
        is_resolved = index < resolved
        slate.append(
            {
                "slate_id": f"slate-{index + 1:02d}",
                "issuer_name": (
                    SYNTHETIC_ISSUER
                    if index == 0
                    else f"Fictional Benchmark Issuer {index + 1}, Inc."
                ),
                "sector_label": sectors[index % len(sectors)],
                "cik": f"{index + 1:010d}" if is_resolved else None,
                "target_previous_fiscal_year": 2022,
                "target_current_fiscal_year": 2023,
                "resolution_status": (
                    rfb.ISSUER_RESOLVED if is_resolved else rfb.ISSUER_PENDING
                ),
            }
        )
    return slate


def manifest(
    pairs: list[dict[str, Any]] | None = None,
    *,
    status: str = rfb.STATUS_PROPOSED,
    slate: list[dict[str, Any]] | None = None,
    target_pair_count: int | None = None,
) -> dict[str, Any]:
    slate = slate if slate is not None else issuer_slate()
    pairs = pairs or []
    return {
        "schema_version": rfb.MANIFEST_SCHEMA_VERSION,
        "benchmark_id": "real_filing_test",
        "benchmark_version": 1,
        "frozen_at": "2026-01-01T00:00:00+00:00",
        "selection_protocol_version": rfb.SELECTION_PROTOCOL_VERSION,
        "status": status,
        "form": "10-K",
        "target_pair_count": (
            target_pair_count if target_pair_count is not None else len(slate)
        ),
        "description": "SYNTHETIC test fixture. No real issuer or filing.",
        "proposed_issuers": slate,
        "pairs": pairs,
    }


def pair(
    *,
    pair_id: str = "pair-01",
    slate_id: str = "slate-01",
    cik: str = SYNTHETIC_CIK,
    previous_html: str = PREVIOUS_HTML,
    current_html: str = CURRENT_HTML,
    issuer_name: str = SYNTHETIC_ISSUER,
    sector_label: str = "Fictional Sector A",
) -> dict[str, Any]:
    return {
        "pair_id": pair_id,
        "slate_id": slate_id,
        "issuer_name": issuer_name,
        "cik": cik,
        "sector_label": sector_label,
        "previous": side(
            accession_suffix="22-000001",
            filing_date="2022-11-01",
            reporting_period="2022-09-30",
            primary_document="fictional-20220930.htm",
            expected_sha256=sha256(previous_html),
            cik=cik,
        ),
        "current": side(
            accession_suffix="23-000001",
            filing_date="2023-11-01",
            reporting_period="2023-09-30",
            primary_document="fictional-20230930.htm",
            expected_sha256=sha256(current_html),
            cik=cik,
        ),
    }


def single_pair_manifest(
    *,
    previous_html: str = PREVIOUS_HTML,
    current_html: str = CURRENT_HTML,
    status: str = rfb.STATUS_SOURCE_VERIFIED,
) -> dict[str, Any]:
    """One resolved pair backed by a one-entry frozen slate."""
    slate = issuer_slate(count=1, resolved=1)
    return manifest(
        pairs=[pair(previous_html=previous_html, current_html=current_html)],
        status=status,
        slate=slate,
        target_pair_count=1,
    )


def write_manifest(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    return path


def seed_sources(
    layout: rfb.CorpusLayout,
    manifest_document: dict[str, Any],
    contents: dict[tuple[str, str], str],
) -> None:
    """Place source bytes in the corpus as if acquisition had verified them."""
    for pair_document in rfb.manifest_pairs(manifest_document):
        for side_name, payload in rfb.pair_sides(pair_document):
            text = contents[(pair_document["pair_id"], side_name)]
            target = layout.source_file(
                pair_document["pair_id"], side_name, payload["primary_document"]
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")


def human_verify(
    annotation: dict[str, Any],
    *,
    annotator_id: str = "reviewer@localhost",
    timestamp: str = "2026-02-01T12:00:00+00:00",
    labels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Simulate the ONE step no tool performs: a person verifying labels."""
    verified = dict(annotation)
    verified["annotation_status"] = rfb.ANNOTATION_HUMAN_VERIFIED
    verified["annotator_id"] = annotator_id
    verified["verification_timestamp"] = timestamp
    if labels is not None:
        verified["labels"] = labels
    rfb.validate_annotation(verified)
    return verified
