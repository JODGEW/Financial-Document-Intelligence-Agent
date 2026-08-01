"""Deterministic SEC-style Item heading detection over an HTML document.

Why this module exists
----------------------
The generic HTML handler derives ``section_title`` from ``<h1>``/``<h2>``/
``<h3>`` only. Real SEC filings do not use heading tags at all: an Item heading
is a styled ``div``, ``p``, ``span`` or table cell that *looks* like a heading
when rendered but carries no semantic heading markup. A filing whose Item 1A
heading is invisible to the loader produces no ``section_key``, and the whole
comparison path downstream of it records an honest ``missing``.

This module closes that gap **structurally**, not statistically:

- No LLM, no embeddings, no fuzzy or semantic similarity. Everything here is a
  closed grammar over normalized visible text plus bounded structural context.
- No issuer, accession, filename, or hash rules. Nothing in this file was
  derived from the identity of any particular filing, and no literal text was
  copied out of one.
- No browser automation and no JavaScript execution. Parsing uses the
  ``beautifulsoup4`` + ``lxml`` dependency the HTML handler already requires.

The recognizer is deliberately conservative. Where the structure does not
decide the question, the outcome is ``missing`` or ``ambiguous`` with a bounded
reason code — never a guess that happens to raise the extraction rate.

Rendering fidelity
------------------
Visible text is assembled the way a browser lays it out: inline elements are
concatenated with **no** separator, block-level elements introduce one. This
matters. Filings routinely split a single word across adjacent styled spans;
joining every element with a space would corrupt the word (``RIS`` + ``K
FACTORS``), and joining everything with nothing would weld adjacent blocks
together. Following the inline/block distinction is both correct HTML semantics
and the only rule that reads such a heading the way a human reader does.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

# --- Version ------------------------------------------------------------------

# Advanced when heading recognition, normalization, disambiguation, or boundary
# selection changes in a way that can move extracted content. v1 was the
# implicit header-tags-only behavior of the HTML handler; v2 is this module.
PARSER_VERSION = "sec_html_item_headings.v2"


# --- Policy constants ---------------------------------------------------------
#
# Every bound here is a documented structural policy, chosen from what an SEC
# Item heading and an Item section *are*, not fitted to any corpus.

#: A heading is a short label. Longer normalized text is prose, not a heading.
MAX_HEADING_TEXT_CHARS = 200

#: The title that follows an Item designator is itself a bounded label.
MAX_TITLE_CHARS = 120

#: Minimum substantive text for a selected section to count as a real section
#: rather than a cross-reference line or a contents entry. ~150 words: below
#: any genuine risk-factor disclosure, far above any table-of-contents row.
MIN_SECTION_CHARS = 1_000

#: Safety bound on a single extracted section. Exceeding it yields an explicit
#: ``ambiguous`` outcome; content is never silently truncated to fit.
MAX_SECTION_CHARS = 2_000_000

#: A contents/navigation region is a run of Item headings packed together with
#: almost no prose between them.
NAV_MIN_RUN = 6
NAV_MAX_GAP_CHARS = 400

#: Bounded diagnostic strings.
MAX_REASON_CHARS = 200


# --- Tag sets -----------------------------------------------------------------

#: Elements that establish a new line box when rendered. Used both to decide
#: where text breaks and to decide what can own a heading.
BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "body", "caption",
        "center", "dd", "div", "dl", "dt", "fieldset", "figcaption", "figure",
        "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
        "html", "li", "main", "nav", "ol", "p", "pre", "section", "table",
        "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
)

#: Never contribute visible text.
INERT_TAGS = ("script", "style", "noscript", "template", "head")


# --- Text normalization -------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
_WHITESPACE_RE = re.compile(r"\s+")
#: Control characters other than the whitespace we normalize away.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HIDDEN_STYLE_RE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:;|$)",
    re.IGNORECASE,
)


def normalize_visible_text(raw: str) -> str | None:
    """Normalize a raw visible-text run deterministically.

    Returns ``None`` when the text carries control characters, which a heading
    never legitimately does.
    """
    text = unicodedata.normalize("NFKC", raw)
    text = text.translate(_ZERO_WIDTH)
    # NFKC already folds NBSP to a space; the explicit replace keeps the
    # guarantee independent of that and covers the narrow NBSP.
    text = text.replace("\xa0", " ").replace(" ", " ")
    if _CONTROL_RE.search(text):
        return None
    return _WHITESPACE_RE.sub(" ", text).strip()


def is_hidden(element: Any) -> bool:
    """True when an element is explicitly non-visible.

    Only declarative, deterministic signals: a ``hidden`` attribute or an
    inline ``display:none`` / ``visibility:hidden``. No stylesheet cascade and
    no layout engine — this module does not render.
    """
    if element.has_attr("hidden"):
        return True
    style = element.get("style")
    if isinstance(style, str) and _HIDDEN_STYLE_RE.search(style):
        return True
    aria = element.get("aria-hidden")
    return isinstance(aria, str) and aria.strip().lower() == "true"


# --- Closed Item grammar ------------------------------------------------------

#: The closed set of 10-K Item designators, in filing order. Membership is a
#: hard gate: a token outside this list is not an Item designator, however
#: heading-like the surrounding text looks.
ITEM_SEQUENCE = (
    "1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8", "9",
    "9A", "9B", "9C", "10", "11", "12", "13", "14", "15", "16",
)
_ITEM_ORDER = {item: index for index, item in enumerate(ITEM_SEQUENCE)}

#: Closed set of Part designators, with their ordering.
PART_SEQUENCE = ("I", "II", "III", "IV")
_PART_ORDER = {part: index for index, part in enumerate(PART_SEQUENCE)}

_SEPARATOR = r"[\s.:;,\-‐-―⁃)\]]"

#: ``Part I, Item 1A. Risk Factors`` / ``ITEM 1A — RISK FACTORS`` / ``Item 1A:
#: Risk Factors``. Anchored at the start: a sentence that merely *mentions* an
#: Item cannot match, because the designator must open the block.
ITEM_HEADING_RE = re.compile(
    r"^(?:part\s+(?P<part>[ivx]{1,4})\s*" + _SEPARATOR + r"*\s*)?"
    r"item\s*(?P<item>\d{1,2}\s*[a-c]?)(?![\w])"
    r"(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)

#: A standalone Part marker (``PART II``), used for boundary detection only.
PART_HEADING_RE = re.compile(
    r"^part\s+(?P<part>[ivx]{1,4})(?P<rest>.*)$", re.IGNORECASE | re.DOTALL
)

_LEADING_SEPARATOR_RE = re.compile(r"^" + _SEPARATOR + r"+")


def parse_item_designator(text: str) -> tuple[str, str | None, str] | None:
    """Parse ``text`` as ``[Part X,] Item <id>[ separator <title>]``.

    Returns ``(item_id, part, title)`` or ``None``. ``item_id`` and ``part``
    are canonical members of the closed sequences; ``title`` is the bounded
    remainder with its leading separator removed (empty when the block carries
    the designator alone).
    """
    match = ITEM_HEADING_RE.match(text)
    if match is None:
        return None

    item_id = re.sub(r"\s+", "", match.group("item")).upper()
    if item_id not in _ITEM_ORDER:
        return None

    part = None
    if match.group("part"):
        part = match.group("part").upper()
        if part not in _PART_ORDER:
            return None

    rest = match.group("rest")
    # The designator must be followed by a separator or end of block: "Item 1A"
    # may not run straight into a word, which would mean it was never a
    # designator ("Item 1Alpha").
    if rest and not _LEADING_SEPARATOR_RE.match(rest):
        return None
    title = _LEADING_SEPARATOR_RE.sub("", rest).strip()
    if len(title) > MAX_TITLE_CHARS:
        return None
    return item_id, part, title


def parse_part_designator(text: str) -> str | None:
    """Parse ``text`` as a standalone Part marker, else ``None``."""
    match = PART_HEADING_RE.match(text)
    if match is None:
        return None
    part = match.group("part").upper()
    if part not in _PART_ORDER:
        return None
    rest = _LEADING_SEPARATOR_RE.sub("", match.group("rest")).strip()
    # ``Part II`` alone, or with a short label. A Part marker trailing a whole
    # paragraph is prose.
    if len(rest) > MAX_TITLE_CHARS:
        return None
    return part


def canonical_heading(item_id: str, part: str | None, title: str) -> str:
    """Canonical heading string handed to ``ingest.section_key_for``."""
    head = f"Item {item_id}."
    if part:
        head = f"Part {part}, {head}"
    return f"{head} {title}".strip() if title else head


def item_successors(item_id: str) -> tuple[str, ...]:
    """Items that legitimately close ``item_id``'s section."""
    index = _ITEM_ORDER.get(item_id)
    if index is None:
        return ()
    return ITEM_SEQUENCE[index + 1:]


# --- Block stream -------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """One visible block of the document, in deterministic document order."""

    index: int
    tag: str
    parent_tag: str
    text: str
    is_anchor_block: bool
    emphasized: bool


def build_block_stream(soup: Any) -> list[Block]:
    """Ordered, non-overlapping stream of visible blocks.

    One traversal in document order. Inline runs accumulate into the buffer of
    their nearest block-level ancestor and concatenate with **no** separator, so
    a word split across styled sibling spans survives; a block-level child
    flushes that buffer first, so no character is ever emitted twice and the
    concatenation of the stream is the document's visible text in reading
    order.

    ``tag`` is the owning block element, which is what makes a heading report
    as the ``td`` / ``div`` / ``p`` that renders it rather than as the inner
    ``span`` that happens to carry the styling.
    """
    from bs4 import NavigableString

    blocks: list[Block] = []

    def emit(owner: Any, runs: list[tuple[str, bool, bool]]) -> None:
        if not runs:
            return
        text = normalize_visible_text("".join(run[0] for run in runs))
        if not text:
            return
        # "Predominantly an anchor": every non-blank run came from inside a
        # link, i.e. the block IS a link rather than prose containing one.
        contentful = [run for run in runs if run[0].strip()]
        blocks.append(
            Block(
                index=len(blocks),
                tag=owner.name,
                parent_tag=owner.parent.name if owner.parent else "",
                text=text,
                is_anchor_block=bool(contentful)
                and all(run[1] for run in contentful),
                emphasized=any(run[2] for run in contentful),
            )
        )

    def walk(element: Any, in_anchor: bool, in_emphasis: bool) -> None:
        runs: list[tuple[str, bool, bool]] = []
        for child in element.children:
            if isinstance(child, NavigableString):
                # Comments and processing instructions subclass NavigableString
                # but are not visible text.
                if type(child) is NavigableString:
                    runs.append((str(child), in_anchor, in_emphasis))
                continue
            name = getattr(child, "name", None)
            if name is None or name in INERT_TAGS or is_hidden(child):
                continue
            if name == "br":
                emit(element, runs)
                runs = []
                continue
            if name in BLOCK_TAGS:
                emit(element, runs)
                runs = []
                walk(child, in_anchor, in_emphasis)
                continue
            # Inline: its text belongs to this block's buffer.
            nested = walk_inline(
                child,
                in_anchor or name == "a",
                in_emphasis or name in ("b", "strong"),
            )
            runs.extend(nested)
        emit(element, runs)

    def walk_inline(
        element: Any, in_anchor: bool, in_emphasis: bool
    ) -> list[tuple[str, bool, bool]]:
        """Inline subtree text, plus any block descendants it illegally wraps."""
        runs: list[tuple[str, bool, bool]] = []
        for child in element.children:
            if isinstance(child, NavigableString):
                if type(child) is NavigableString:
                    runs.append((str(child), in_anchor, in_emphasis))
                continue
            name = getattr(child, "name", None)
            if name is None or name in INERT_TAGS or is_hidden(child):
                continue
            if name == "br":
                runs.append((" ", in_anchor, in_emphasis))
                continue
            if name in BLOCK_TAGS:
                # An inline element wrapping a block is malformed but common.
                # Treat the block normally so its text is not lost.
                emit(element, runs)
                runs = []
                walk(child, in_anchor, in_emphasis)
                continue
            runs.extend(
                walk_inline(
                    child,
                    in_anchor or name == "a",
                    in_emphasis or name in ("b", "strong"),
                )
            )
        return runs

    root = soup.body or soup
    walk(root, False, False)
    return blocks


# --- Candidate classification -------------------------------------------------

CLASS_SUBSTANTIVE = "substantive"
CLASS_DESIGNATOR_ONLY = "designator_only"
CLASS_NAVIGATION = "navigation"
CLASS_INSUFFICIENT_CONTENT = "insufficient_content"

REASON_DESIGNATOR_ONLY = "designator_only_block"
REASON_NAVIGATION_RUN = "dense_item_navigation_run"
REASON_ANCHOR_BLOCK = "intra_document_anchor_block"
REASON_INSUFFICIENT_CONTENT = "insufficient_following_content"


@dataclass
class Candidate:
    """An Item-heading-shaped block plus its bounded structural context."""

    block_index: int
    item_id: str
    part: str | None
    title: str
    canonical: str
    tag: str
    parent_tag: str
    is_anchor_block: bool
    emphasized: bool
    text_length: int
    classification: str = CLASS_SUBSTANTIVE
    reason: str | None = None
    following_chars: int = 0

    def diagnostics(self) -> dict[str, Any]:
        """Bounded, content-free view for a report."""
        return {
            "item_id": self.item_id,
            "part": self.part,
            "canonical_heading": self.canonical[:MAX_HEADING_TEXT_CHARS],
            "element_tag": self.tag,
            "parent_tag": self.parent_tag,
            "is_anchor_block": self.is_anchor_block,
            "classification": self.classification,
            "reason": (self.reason or "")[:MAX_REASON_CHARS] or None,
        }


def _text_between(blocks: list[Block], start: int, end: int) -> int:
    return sum(len(blocks[i].text) for i in range(start + 1, min(end, len(blocks))))


def find_candidates(blocks: list[Block]) -> list[Candidate]:
    """Every Item-heading-shaped block, classified deterministically."""
    candidates: list[Candidate] = []
    for block in blocks:
        if len(block.text) > MAX_HEADING_TEXT_CHARS:
            continue
        parsed = parse_item_designator(block.text)
        if parsed is None:
            continue
        item_id, part, title = parsed
        candidates.append(
            Candidate(
                block_index=block.index,
                item_id=item_id,
                part=part,
                title=title,
                canonical=canonical_heading(item_id, part, title),
                tag=block.tag,
                parent_tag=block.parent_tag,
                is_anchor_block=block.is_anchor_block,
                emphasized=block.emphasized,
                text_length=len(block.text),
            )
        )

    _classify(candidates, blocks)
    return candidates


def _navigation_run_members(
    candidates: list[Candidate], blocks: list[Block]
) -> set[int]:
    """Indices of candidates inside a dense contents/navigation region.

    A contents block is a run of Item headings packed together with almost no
    prose between consecutive entries. Real section headings are separated by
    the section itself.
    """
    members: set[int] = set()
    run: list[int] = []

    def close(run: list[int]) -> None:
        if len(run) >= NAV_MIN_RUN:
            members.update(run)

    for position, candidate in enumerate(candidates):
        if not run:
            run = [position]
            continue
        previous = candidates[run[-1]]
        gap = _text_between(blocks, previous.block_index, candidate.block_index)
        if gap <= NAV_MAX_GAP_CHARS:
            run.append(position)
        else:
            close(run)
            run = [position]
    close(run)
    return members


def _classify(candidates: list[Candidate], blocks: list[Block]) -> None:
    """Assign exactly one classification to every candidate.

    Order matters and is fixed: a block with no title in it cannot be a section
    heading regardless of anything else, then navigation, then content volume.
    """
    navigation = _navigation_run_members(candidates, blocks)

    for position, candidate in enumerate(candidates):
        # A heading names its section. A block holding only the designator is
        # either a contents row whose title sits in a sibling cell, or a
        # running page header repeating the item currently in effect. Neither
        # is a section heading.
        if not candidate.title:
            candidate.classification = CLASS_DESIGNATOR_ONLY
            candidate.reason = REASON_DESIGNATOR_ONLY
            continue
        # A heading that is itself a link into the document points *at* the
        # section rather than opening it. This holds regardless of what follows
        # it, because what follows a contents link is usually the section.
        if candidate.is_anchor_block:
            candidate.classification = CLASS_NAVIGATION
            candidate.reason = REASON_ANCHOR_BLOCK
            continue

        candidate.following_chars = _following_content_chars(
            candidate, candidates, blocks
        )
        if candidate.following_chars >= MIN_SECTION_CHARS:
            # Substantive content settles it. Density cannot overrule this:
            # a contents block sitting immediately above the body would
            # otherwise sweep the real heading into the navigation run.
            candidate.classification = CLASS_SUBSTANTIVE
            continue
        candidate.classification = CLASS_NAVIGATION if position in navigation else (
            CLASS_INSUFFICIENT_CONTENT
        )
        candidate.reason = (
            REASON_NAVIGATION_RUN
            if position in navigation
            else REASON_INSUFFICIENT_CONTENT
        )


def _following_content_chars(
    candidate: Candidate, candidates: list[Candidate], blocks: list[Block]
) -> int:
    """Substantive text between a candidate and the next *different* Item.

    A repeat of the same Item designator is a running page header, not a
    boundary — measuring to it would make every real section look empty.
    """
    end = len(blocks)
    for other in candidates:
        if other.block_index <= candidate.block_index:
            continue
        if other.item_id == candidate.item_id:
            continue
        end = other.block_index
        break
    return _text_between(blocks, candidate.block_index, end)


# --- Section selection --------------------------------------------------------

OUTCOME_EXTRACTED = "extracted"
OUTCOME_MISSING = "missing"
OUTCOME_AMBIGUOUS = "ambiguous"

REASON_NO_CANDIDATE = "no_item_heading_found"
REASON_NO_SUBSTANTIVE_CANDIDATE = "no_substantive_item_heading"
REASON_MULTIPLE_SUBSTANTIVE = "multiple_substantive_item_headings"
REASON_NO_END_BOUNDARY = "no_trustworthy_end_boundary"
REASON_SECTION_TOO_LARGE = "section_exceeds_maximum_bound"
REASON_SELECTED = "single_substantive_item_heading"


@dataclass
class SectionSelection:
    """Outcome of selecting one Item section, with bounded diagnostics."""

    outcome: str
    reason: str
    item_id: str
    start_block: int | None = None
    end_block: int | None = None
    canonical_heading: str | None = None
    element_tag: str | None = None
    boundary_heading: str | None = None
    candidate_count: int = 0
    substantive_count: int = 0
    navigation_rejected_count: int = 0
    designator_only_count: int = 0
    insufficient_content_count: int = 0

    def diagnostics(self) -> dict[str, Any]:
        """Bounded, filing-text-free diagnostic payload."""
        return {
            "parser_version": PARSER_VERSION,
            "outcome": self.outcome,
            "extraction_reason": self.reason[:MAX_REASON_CHARS],
            "item_id": self.item_id,
            "candidate_count": self.candidate_count,
            "substantive_candidate_count": self.substantive_count,
            "navigation_rejected_count": self.navigation_rejected_count,
            "designator_only_count": self.designator_only_count,
            "insufficient_content_count": self.insufficient_content_count,
            "selected_canonical_heading": (
                self.canonical_heading[:MAX_HEADING_TEXT_CHARS]
                if self.canonical_heading
                else None
            ),
            "selected_element_tag": self.element_tag,
            "boundary_heading": (
                self.boundary_heading[:MAX_HEADING_TEXT_CHARS]
                if self.boundary_heading
                else None
            ),
        }


def _part_in_effect(
    candidate: Candidate, candidates: list[Candidate], blocks: list[Block]
) -> str | None:
    """The Part declared at or before a candidate, if one is declared."""
    if candidate.part:
        return candidate.part
    part: str | None = None
    for block in blocks[: candidate.block_index + 1]:
        if len(block.text) > MAX_HEADING_TEXT_CHARS:
            continue
        declared = parse_part_designator(block.text)
        if declared is not None:
            part = declared
    return part


def select_section(
    blocks: list[Block], candidates: list[Candidate], item_id: str
) -> SectionSelection:
    """Select the one substantive occurrence of ``item_id`` and bound it.

    Neither the first nor the last occurrence is privileged. Selection is by
    classification alone; anything other than exactly one substantive candidate
    is reported honestly rather than resolved by preference.
    """
    for_item = [c for c in candidates if c.item_id == item_id]
    counts = {
        "candidate_count": len(for_item),
        "navigation_rejected_count": sum(
            1 for c in for_item if c.classification == CLASS_NAVIGATION
        ),
        "designator_only_count": sum(
            1 for c in for_item if c.classification == CLASS_DESIGNATOR_ONLY
        ),
        "insufficient_content_count": sum(
            1 for c in for_item if c.classification == CLASS_INSUFFICIENT_CONTENT
        ),
    }
    substantive = [c for c in for_item if c.classification == CLASS_SUBSTANTIVE]
    counts["substantive_count"] = len(substantive)

    if not for_item:
        return SectionSelection(
            outcome=OUTCOME_MISSING, reason=REASON_NO_CANDIDATE,
            item_id=item_id, **counts,
        )
    if not substantive:
        return SectionSelection(
            outcome=OUTCOME_MISSING, reason=REASON_NO_SUBSTANTIVE_CANDIDATE,
            item_id=item_id, **counts,
        )
    if len(substantive) > 1:
        return SectionSelection(
            outcome=OUTCOME_AMBIGUOUS, reason=REASON_MULTIPLE_SUBSTANTIVE,
            item_id=item_id, **counts,
        )

    selected = substantive[0]
    boundary = _find_boundary(selected, candidates, blocks)
    if boundary is None:
        return SectionSelection(
            outcome=OUTCOME_AMBIGUOUS, reason=REASON_NO_END_BOUNDARY,
            item_id=item_id,
            canonical_heading=selected.canonical, element_tag=selected.tag,
            **counts,
        )
    end_block, boundary_heading = boundary

    section_chars = _text_between(blocks, selected.block_index, end_block)
    if section_chars > MAX_SECTION_CHARS:
        return SectionSelection(
            outcome=OUTCOME_AMBIGUOUS, reason=REASON_SECTION_TOO_LARGE,
            item_id=item_id,
            canonical_heading=selected.canonical, element_tag=selected.tag,
            boundary_heading=boundary_heading, **counts,
        )

    return SectionSelection(
        outcome=OUTCOME_EXTRACTED, reason=REASON_SELECTED, item_id=item_id,
        start_block=selected.block_index, end_block=end_block,
        canonical_heading=selected.canonical, element_tag=selected.tag,
        boundary_heading=boundary_heading, **counts,
    )


def _find_boundary(
    selected: Candidate, candidates: list[Candidate], blocks: list[Block]
) -> tuple[int, str] | None:
    """First trustworthy end boundary after ``selected``, exclusive.

    Accepts a titled, non-navigation Item heading that succeeds the selected
    item in the closed sequence, or a Part strictly later than the Part in
    effect. A repeated same-Part marker is running page furniture and is
    rejected — accepting it would truncate a section at its first page break.
    """
    successors = set(item_successors(selected.item_id))
    part_now = _part_in_effect(selected, candidates, blocks)
    part_rank = _PART_ORDER.get(part_now) if part_now else None

    boundary_candidates: dict[int, str] = {}
    for candidate in candidates:
        if candidate.block_index <= selected.block_index:
            continue
        if candidate.item_id not in successors:
            continue
        if candidate.classification in (CLASS_NAVIGATION, CLASS_DESIGNATOR_ONLY):
            continue
        boundary_candidates[candidate.block_index] = candidate.canonical
        break

    if part_rank is not None:
        for block in blocks[selected.block_index + 1:]:
            if len(block.text) > MAX_HEADING_TEXT_CHARS:
                continue
            declared = parse_part_designator(block.text)
            if declared is None:
                continue
            if _PART_ORDER[declared] > part_rank:
                boundary_candidates[block.index] = f"Part {declared}"
                break

    if not boundary_candidates:
        return None
    index = min(boundary_candidates)
    return index, boundary_candidates[index]


def blocks_text(blocks: list[Block], first: int, stop: int) -> str:
    """Text of ``blocks[first:stop]`` in document order."""
    return "\n\n".join(
        blocks[i].text
        for i in range(max(first, 0), min(stop, len(blocks)))
        if blocks[i].text
    )


def section_text(blocks: list[Block], start_block: int, end_block: int) -> str:
    """Text of a selected section: heading block and end boundary both excluded."""
    return blocks_text(blocks, start_block + 1, end_block)


def detect_item_sections(soup: Any, item_id: str) -> tuple[
    list[Block], list[Candidate], SectionSelection
]:
    """Full pipeline over a parsed document: blocks, candidates, selection."""
    blocks = build_block_stream(soup)
    candidates = find_candidates(blocks)
    return blocks, candidates, select_section(blocks, candidates, item_id)


def substantive_headings(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Every candidate that survived classification, in document order."""
    return [c for c in candidates if c.classification == CLASS_SUBSTANTIVE]
