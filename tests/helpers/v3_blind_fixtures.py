"""Synthetic SEC-shaped fixtures for the v3 holdout blind-run suites.

Every document here is a hand-written fictional filing: the SHAPE of a real
EDGAR 10-K (a contents table whose Item designator and title sit in separate
cells, then styled div/span blocks with no heading tags anywhere), never the
content of one. No real filing text exists in this repository, and these
fixtures never touch the frozen v3 holdout bodies.

The unit headings exercise all three ``item1a_units.v3`` heading classes —
the ``... Risks`` suffix form, the ``Risks Related to ...`` prefix form, the
closed ``General Risk Factors`` literal, and the ``/`` punctuation the v3
grammar adds — plus a deliberately repeated normalized heading.

One constraint worth stating, because it shapes every fixture here: the frozen
reconstruction dedupes chunk overlap only in the [24, 300] character window, so
a heading SHORTER than 24 characters that happens to land on a chunk boundary
is reconstructed twice. ``General Risk Factors`` (20 characters) is therefore
always placed first in a section, where no boundary can fall on it, and every
other heading is comfortably longer. That is a property of the fixtures, not a
behavior these suites assert or change: the frozen pipeline is never modified
here.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import real_filing_benchmark as rfb
from tests.helpers import real_filing_fixtures as _shared

# --- SEC-shaped document model ----------------------------------------------------

_CONTENTS = "<table>" + "".join(
    f"<tr><td><span>Item {item}.</span></td><td><span>{label}</span></td></tr>"
    for item, label in [
        ("1", "Business"),
        ("1A", "Risk Factors"),
        ("1B", "Unresolved Staff Comments"),
        ("2", "Properties"),
    ]
) + "</table>"


def _pad(seed: int, sentences: int = 5) -> str:
    """Non-repeating filler, so a section is substantive without being
    self-similar enough to confuse overlap reconstruction."""
    return " ".join(
        f"Fictional sentence {seed}-{index} describes an invented condition "
        "that affects no real company and states no real fact."
        for index in range(sentences)
    )


def sec_filing(units: list[tuple[str, str]], year: str) -> str:
    """One fictional 10-K whose Item 1A section holds the given units."""
    body = "".join(
        f'<div style="font-weight:700"><span>{heading}</span></div>\n'
        f"<div><span>{text} {_pad(index)}</span></div>\n"
        for index, (heading, text) in enumerate(units)
    )
    return (
        f"<html><head><title>Fictional 10-K FY{year}</title></head><body>\n"
        f"{_CONTENTS}\n"
        "<div><span>Part I</span></div>\n"
        '<div style="font-weight:700"><span>Item 1A.</span>'
        "<span> Risk Factors</span></div>\n"
        "<div><span>The following risk factors should be read together with "
        "the rest of this annual report.</span></div>\n"
        + body
        + '<div style="font-weight:700"><span>Item 1B. Unresolved Staff '
        "Comments</span></div>\n<div><span>None.</span></div>\n"
        "</body></html>\n"
    )


# --- The v3 grammar, exercised ------------------------------------------------------

PREVIOUS_UNITS = [
    ("General Risk Factors", "Broad economic conditions may affect us."),
    (
        "Risks Related to Our Business",
        "We depend on a small number of fictional suppliers.",
    ),
    ("Compliance/Legal Operations Risks", "We operate in regulated markets."),
]

CURRENT_UNITS = [
    ("General Risk Factors", "Broad economic conditions may affect us."),
    (
        "Risks Related to Our Business",
        "We depend on a single fictional contract manufacturer this year.",
    ),
    (
        "Cybersecurity and Data Security Risks",
        "We face invented intrusion attempts.",
    ),
]

REPEATED_HEADING_UNITS = [
    ("General Risk Factors", "Broad economic conditions may affect us."),
    ("Risks Related to Our Operations", "Our fictional facilities may be disrupted."),
    (
        "Risks Related to Our Operations",
        "A second block repeats the same normalized heading on purpose.",
    ),
]

PREVIOUS_HTML = sec_filing(PREVIOUS_UNITS, "2024")
CURRENT_HTML = sec_filing(CURRENT_UNITS, "2025")
REPEATED_HEADING_HTML = sec_filing(REPEATED_HEADING_UNITS, "2025")

#: The two blocked-side shapes are REUSED verbatim from the shared benchmark
#: fixtures rather than rewritten here: they are the fictional documents the
#: existing suites already pin to the ``missing`` and ``ambiguous`` outcomes,
#: and a second hand-written variant would only risk drifting away from the
#: outcome it is supposed to exercise.
NO_SECTION_HTML = _shared.NO_SECTION_HTML
AMBIGUOUS_SECTION_HTML = _shared.AMBIGUOUS_SECTION_HTML


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- A synthetic corpus over the frozen v3 identities ---------------------------------


def synthetic_manifest(
    committed: dict[str, Any],
    *,
    missing_current_index: int | None = None,
    ambiguous_previous_index: int | None = None,
    repeated_heading_index: int | None = None,
    status: str | None = None,
) -> tuple[dict[str, Any], dict[tuple[str, str], str]]:
    """The committed v3 manifest re-anchored to synthetic fictional bodies.

    The frozen identities (issuers, CIKs, accessions, document names) are
    public metadata and are reused so the suites exercise the real ordering and
    the real pair count; only the BODIES are synthetic, and their digests are
    recomputed to match.
    """
    import real_filing_v3_holdout as rfv3

    document = copy.deepcopy(committed)
    document["status"] = status or rfb.STATUS_SOURCE_VERIFIED
    if document["status"] == rfb.STATUS_CORPUS_BUILT:
        import real_filing_v3_holdout_extraction as rfx

        document["corpus_role_detail"] = rfx.corpus_built_corpus_role_detail()
        document["description"] = rfx.CORPUS_BUILT_DESCRIPTION
    contents: dict[tuple[str, str], str] = {}
    for index, pair in enumerate(document["pairs"]):
        for side in ("previous", "current"):
            if index == missing_current_index and side == "current":
                html = NO_SECTION_HTML
            elif index == ambiguous_previous_index and side == "previous":
                html = AMBIGUOUS_SECTION_HTML
            elif index == repeated_heading_index and side == "current":
                # Current side only: two sides with byte-identical content are
                # a registry ``duplicate`` by design, which would block the
                # pair for a reason that has nothing to do with headings.
                html = REPEATED_HEADING_HTML
            elif side == "previous":
                html = PREVIOUS_HTML
            else:
                html = CURRENT_HTML
            pair[side]["expected_sha256"] = sha256(html)
            pair[side]["source_verified"] = True
            contents[(pair["pair_id"], side)] = html
    rfv3.validate_v3_holdout_manifest(document)
    return document, contents


def seed_corpus(
    root: Path, document: dict[str, Any], contents: dict[tuple[str, str], str]
) -> rfb.CorpusLayout:
    """Write the synthetic bodies under an untracked corpus root.

    ``benchmark_data`` is part of the path on purpose: the blind runner refuses
    a corpus root outside the gitignored tree, which is exactly the rule that
    keeps a filing body out of a tracked directory.
    """
    layout = rfb.CorpusLayout(root)
    for pair in document["pairs"]:
        for side in ("previous", "current"):
            target = layout.source_file(
                pair["pair_id"], side, pair[side]["primary_document"]
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents[(pair["pair_id"], side)], encoding="utf-8")
    return layout


def untracked_root(base: Path, name: str) -> Path:
    """A corpus root under a ``benchmark_data`` segment, as the runner requires."""
    root = base / "benchmark_data" / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_manifest(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        __import__("json").dumps(document, indent=2, sort_keys=True),
        encoding="utf-8",
    )
