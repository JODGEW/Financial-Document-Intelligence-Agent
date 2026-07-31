"""Deterministic Item 1A risk-factor change detection (roadmap steps 4-6).

Consumes a persisted comparison in ready_for_detection state, loads the
complete Item 1A section of each filing from the index BY METADATA (filing_id
+ canonical section_key — a plain Chroma ``get``; there is no vector top-k,
no embedding call, no LLM anywhere in this module), extracts deterministic
risk-factor units, aligns them, and produces a validated comparison.v1
ComparisonResult that is persisted through comparison_store.

Determinism contract
--------------------
Everything is rule-based: section reconstruction is ordered by the ingestion
``chunk_seq``, unit boundaries come from a conservative heading regex, and
alignment is (1) exact normalized heading match then (2) exact normalized
content match. There is deliberately NO similarity stage in v1 — the
controlled fixtures do not justify one, so unmatched units stay unmatched
rather than being fuzzily paired. Two runs over the same index produce the
same changes, evidence, and summaries (only created_at differs; result_hash
is computed over the timestamp-independent form).

Change semantics (honest by construction)
-----------------------------------------
- modified: unit aligned across both filings, normalized content differs —
  evidence on both sides.
- added / removed: a unit unmatched on one side, emitted ONLY when both
  Item 1A sections were successfully parsed AND proven complete (registry
  chunk_count equals the indexed chunk count for the filing and every chunk
  carries chunk_seq). Because section loading is metadata-based, a retrieval
  miss cannot manufacture added/removed — if the data is not there, the
  completeness gate fails and the unit becomes undetermined.
- undetermined: section missing/unloadable, completeness unprovable, ambiguous
  heading alignment, unit parsing failure, or unresolvable evidence — always
  with an explicit machine-readable reason prefix (see REASON_* codes).

Failure boundary: data-shaped insufficiency produces an UNDETERMINED RESULT
(the comparison ran; its honest outcome is "cannot be determined", status
'detected'). Only unexpected internal faults transition the comparison to
status 'failed' (failure_code='detector_internal_error') — never with paths,
SQL, or document content in the persisted summary.

Durable execution attempts (Stage 3.5 reliability step 1)
---------------------------------------------------------
Every execution that actually starts is durable before any data is read:
``comparison_store.start_detection_attempt`` commits a running attempt row, a
``detection_started`` event, and the comparison's explicit ``detecting`` state
in one transaction; exactly one of ``complete_detection_attempt`` /
``fail_detection_attempt`` finalizes it in one more. No SQLite transaction is
held while Chroma is read or the detector runs, and the running-attempt
constraint means concurrent requests do not duplicate detector work — the loser
gets DetectionInProgress rather than re-running everything.

INTERRUPTION BOUNDARY, stated plainly: killing a direct/replay process between
those transactions leaves ``comparison.status='detecting'`` with a ``running``
attempt until an eligible explicit replay. Authenticated API initial detection
instead queues before reaching this module; its one-shot worker calls the same
execution seam after an atomic claim. Killing that worker after claim leaves
the job and attempt running until its finite lease expires and a later explicit
one-shot worker invocation atomically reclaims it. The old worker is fenced
from finalization. An explicitly classified transient failure may schedule
bounded retry work, but nothing runs merely because time passes; a later
one-shot invocation must claim due work. There is no scheduler, daemon, or
external queue.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import comparison_reliability
import comparison_store
import comparison_validators
import config
import detection_job_retry
import filing_registry
from governance.comparison_schema import (
    dump_comparison,
    load_comparison,
    summarize_validation,
    FilingChange,
)
from ingest import SECTION_KEY_ITEM_1A

logger = logging.getLogger("comparison_detector")

# v2: the three previously not_run checks (citation_support,
# numeric_consistency, direction_consistency) are now computed by
# comparison_validators, which changes persisted validation output. Results
# stored under v1 remain readable; re-detection of the same pair happens as a
# NEW comparison under the bumped workflow version (comparison_store), never
# by overwriting the v1 result.
DETECTOR_VERSION = "item1a_detector.v2"

# Stable reason codes. Section/data codes appear as the prefix of a change's
# undetermined_reason; lifecycle codes surface through typed errors -> API.
REASON_COMPARISON_NOT_READY = "comparison_not_ready"
REASON_PREVIOUS_SECTION_MISSING = "previous_section_missing"
REASON_CURRENT_SECTION_MISSING = "current_section_missing"
REASON_SECTION_METADATA_INCOMPLETE = "section_metadata_incomplete"
REASON_SECTION_UNIT_PARSE_FAILED = "section_unit_parse_failed"
REASON_AMBIGUOUS_UNIT_ALIGNMENT = "ambiguous_unit_alignment"
REASON_EVIDENCE_RESOLUTION_FAILED = "evidence_resolution_failed"
REASON_INPUTS_STALE = "comparison_inputs_stale"
REASON_VERSION_SUPERSEDED = "detector_version_superseded"
REASON_DETECTOR_INTERNAL_ERROR = "detector_internal_error"
REASON_DETECTION_IN_PROGRESS = "detection_in_progress"
REASON_DEPENDENCY_UNAVAILABLE = (
    detection_job_retry.FAILURE_DEPENDENCY_UNAVAILABLE
)

# Stable domain failure codes that may be persisted VERBATIM on a failed
# attempt. A known condition keeps its own code — collapsing everything into
# detector_internal_error would destroy the diagnostic the attempt record
# exists to preserve. Anything not listed here collapses to
# detector_internal_error, so an unrecognized or dynamically constructed code
# can never reach storage.
DOMAIN_FAILURE_CODES = frozenset(
    {
        REASON_COMPARISON_NOT_READY,
        REASON_PREVIOUS_SECTION_MISSING,
        REASON_CURRENT_SECTION_MISSING,
        REASON_SECTION_METADATA_INCOMPLETE,
        REASON_SECTION_UNIT_PARSE_FAILED,
        REASON_AMBIGUOUS_UNIT_ALIGNMENT,
        REASON_EVIDENCE_RESOLUTION_FAILED,
        REASON_INPUTS_STALE,
        REASON_VERSION_SUPERSEDED,
        REASON_DETECTION_IN_PROGRESS,
        REASON_DETECTOR_INTERNAL_ERROR,
        REASON_DEPENDENCY_UNAVAILABLE,
    }
)

# Allowlisted summaries, keyed by stable code. Persisted summaries are ALWAYS
# derived from the code — never from an exception message, which can carry
# absolute paths, SQL, document content, or a stack trace. Full diagnostics stay
# server-side in the logs.
_FAILURE_SUMMARIES = {
    REASON_DETECTOR_INTERNAL_ERROR: "detection failed unexpectedly; see server logs",
    REASON_COMPARISON_NOT_READY: "the comparison was not ready for detection",
    REASON_PREVIOUS_SECTION_MISSING: "the previous filing's section was unavailable",
    REASON_CURRENT_SECTION_MISSING: "the current filing's section was unavailable",
    REASON_SECTION_METADATA_INCOMPLETE: "section completeness could not be established",
    REASON_SECTION_UNIT_PARSE_FAILED: "risk-factor units could not be parsed",
    REASON_AMBIGUOUS_UNIT_ALIGNMENT: "risk-factor alignment was ambiguous",
    REASON_EVIDENCE_RESOLUTION_FAILED: "evidence could not be resolved to indexed chunks",
    REASON_INPUTS_STALE: "the filing source content changed during detection",
    REASON_VERSION_SUPERSEDED: "a result from an older detector version exists",
    REASON_DETECTION_IN_PROGRESS: "another detection attempt was already running",
    REASON_DEPENDENCY_UNAVAILABLE: (
        "a detection dependency was temporarily unavailable"
    ),
}


def _safe_failure_summary(failure_code: str) -> str:
    """A bounded, code-derived summary safe to persist and display."""
    summary = _FAILURE_SUMMARIES.get(
        failure_code, f"detection failed with code '{failure_code}'"
    )
    return summary[: comparison_store.MAX_FAILURE_SUMMARY_CHARS]

# Conservative risk-factor heading rule for v1: a standalone title-case line
# ending in "Risk"/"Risks" (as the controlled fixtures use). The SEC section
# heading itself ("ITEM 1A. RISK FACTORS") never matches (ends in FACTORS),
# and body sentences never match (must be a full line, capital start, no
# terminal period). ALL-CAPS heading styles are a documented v1 limitation.
_HEADING_RE = re.compile(r"^[A-Z][A-Za-z0-9 ,&()'’-]{2,79}Risks?$")

# Reconstruction: consecutive chunks may share splitter overlap. We dedupe by
# the largest suffix-of-previous == prefix-of-next match in this window.
_MIN_OVERLAP = 24
_MAX_OVERLAP = 300

_PREAMBLE_KEY = "item-1a-preamble"
_PREAMBLE_HEADING = "Item 1A introductory text"



class DetectionError(Exception):
    """Base for detector-surfaced conditions. ``code`` is a stable reason;
    ``message`` is safe to display (no paths, SQL, or document content)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class UnknownComparison(DetectionError):
    """No comparison with this id exists (API: 404)."""


class DetectionNotReady(DetectionError):
    """Lifecycle does not allow detection (API: 409)."""


class DetectionInProgress(DetectionError):
    """A durable detection attempt is already running (API: 409).

    Deliberately distinct from DetectionNotReady: the comparison is mid-flight,
    not terminal. Because the running attempt is persisted rather than held in
    memory, a process killed during detection leaves this state visible.
    Eligible direct/replay attempts require explicit bounded replay; a
    worker-owned attempt is recovered only through its fenced lease-reclaim
    path.
    """


class DetectionInputsStale(DetectionError):
    """A stored result exists but its SOURCE INPUTS changed (API: 409)."""


class DetectionVersionSuperseded(DetectionError):
    """A stored result exists from an older detector version (API: 409).

    Deliberately distinct from DetectionInputsStale: a validator/detector
    version change is not an input source-hash change. The old result stays
    readable via GET; re-detection of the pair requires creating the
    comparison again under the current workflow version (which mints a new
    comparison id) — the stored result is never overwritten.
    """


class DetectionInternalError(DetectionError):
    """Unexpected detector fault; the comparison was transitioned to failed
    (API: 500 with correlation id)."""


class DetectionDependencyUnavailable(DetectionError):
    """Explicit transient dependency-boundary failure for durable jobs."""


def _normalize(text: str) -> str:
    return " ".join((text or "").split())


def _slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")


def _content_hash(text: str) -> str:
    return hashlib.sha1(_normalize(text).encode("utf-8")).hexdigest()[:12]


@dataclass
class SectionChunk:
    """One indexed chunk of a filing's Item 1A section."""

    chunk_id: str
    text: str
    filing_id: str
    source_name: str
    chunk_seq: int | None
    page: int | None
    section_title: str | None


@dataclass
class SectionLoad:
    """Outcome of loading one filing's section from the index."""

    filing_id: str
    status: str  # "loaded" | "missing"
    complete: bool = False
    incomplete_reason: str | None = None
    chunks: list[SectionChunk] = field(default_factory=list)


@dataclass
class RiskFactorUnit:
    """A deterministic comparison unit: heading + its following paragraphs."""

    unit_key: str
    heading: str
    filing_id: str
    text: str
    content_hash: str
    chunks: list[SectionChunk]


class _UnitParseFailure(Exception):
    """Section text yielded no usable units (internal control flow)."""


def open_index():
    """Open the persisted Chroma collection for metadata-only reads.

    embedding_function=None: ``get`` by metadata needs no embeddings, so the
    detector cannot make a Bedrock call even by accident.
    """
    from langchain_chroma import Chroma

    return Chroma(
        collection_name=config.CHROMA_COLLECTION,
        persist_directory=config.CHROMA_PERSIST_DIR,
        embedding_function=None,
    )


def load_section(
    filing_id: str,
    section_key: str,
    chroma_client,
    registry_entry: dict[str, Any],
) -> SectionLoad:
    """Load one filing's complete section by metadata (never vector top-k).

    status="missing" when the filing has no indexed chunks at all or none
    carry the section key. complete=True only when the registry's recorded
    chunk_count equals the filing's indexed chunk count AND every section
    chunk carries chunk_seq — the proof required before added/removed may be
    emitted.
    """
    all_for_filing = chroma_client.get(
        where={"filing_id": filing_id}, include=["metadatas"]
    )
    total_indexed = len(all_for_filing.get("ids") or [])
    if total_indexed == 0:
        return SectionLoad(filing_id=filing_id, status="missing")

    section = chroma_client.get(
        where={"$and": [{"filing_id": filing_id}, {"section_key": section_key}]},
        include=["metadatas", "documents"],
    )
    ids = section.get("ids") or []
    if not ids:
        return SectionLoad(filing_id=filing_id, status="missing")

    chunks = []
    for chunk_id, text, metadata in zip(
        ids, section.get("documents") or [], section.get("metadatas") or []
    ):
        metadata = metadata or {}
        chunks.append(
            SectionChunk(
                chunk_id=chunk_id,
                text=text or "",
                filing_id=filing_id,
                source_name=metadata.get("source_name") or "",
                chunk_seq=metadata.get("chunk_seq"),
                page=metadata.get("page"),
                section_title=metadata.get("section_title"),
            )
        )

    if any(chunk.chunk_seq is None for chunk in chunks):
        return SectionLoad(
            filing_id=filing_id,
            status="loaded",
            complete=False,
            incomplete_reason=(
                "section chunks lack chunk_seq ordering metadata — the store "
                "predates filing-section ingestion; re-run python ingest.py"
            ),
            chunks=sorted(chunks, key=lambda c: (c.page or 0, c.chunk_id)),
        )

    chunks.sort(key=lambda chunk: chunk.chunk_seq)
    expected = registry_entry.get("chunk_count")
    complete = expected is not None and expected == total_indexed
    incomplete_reason = None
    if not complete:
        incomplete_reason = (
            "indexed chunk count does not match the registry's recorded "
            f"chunk_count ({total_indexed} indexed vs {expected!r} recorded)"
        )
    return SectionLoad(
        filing_id=filing_id,
        status="loaded",
        complete=complete,
        incomplete_reason=incomplete_reason,
        chunks=chunks,
    )


def _find_overlap(previous_text: str, next_text: str) -> int:
    """Largest k where the previous text's suffix equals the next's prefix."""
    limit = min(len(previous_text), len(next_text), _MAX_OVERLAP)
    for k in range(limit, _MIN_OVERLAP - 1, -1):
        if previous_text.endswith(next_text[:k]):
            return k
    return 0


def _reconstruct(
    chunks: list[SectionChunk],
) -> tuple[str, list[tuple[SectionChunk, int, int]]]:
    """Rebuild the section text in chunk_seq order, deduping splitter overlap.

    Returns (text, spans) where each span is the chunk's [start, end) region
    in the rebuilt text. A deduped overlap region is attributed to BOTH
    adjacent chunks (the text physically exists in each), so evidence mapping
    stays truthful for units that begin inside an overlap.
    """
    text = ""
    spans: list[tuple[SectionChunk, int, int]] = []
    for chunk in chunks:
        if not text:
            text = chunk.text
            spans.append((chunk, 0, len(text)))
            continue
        overlap = _find_overlap(text, chunk.text)
        if overlap:
            start = len(text) - overlap
            text = text + chunk.text[overlap:]
        else:
            start = len(text) + 1
            text = text + "\n" + chunk.text
        spans.append((chunk, start, len(text)))
    return text, spans


def extract_units(
    chunks: list[SectionChunk], filing_id: str
) -> list[RiskFactorUnit]:
    """Deterministic risk-factor units from a loaded section.

    Unit = heading line + everything until the next heading (or section end);
    text before the first heading is the explicit preamble unit. Every unit
    maps back to the chunks whose spans intersect it — no synthesized,
    untraceable evidence text. Raises _UnitParseFailure when nothing usable
    can be extracted.
    """
    text, spans = _reconstruct(chunks)
    if not _normalize(text):
        raise _UnitParseFailure("section text is empty after reconstruction")

    headings: list[tuple[int, int, str]] = []  # (start, end, heading line)
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if _HEADING_RE.match(stripped):
            headings.append((offset, offset + len(line), stripped))
        offset += len(line)

    boundaries: list[tuple[int, int, str, str]] = []  # (start, end, key, heading)
    if not headings or headings[0][0] > 0:
        first_heading_start = headings[0][0] if headings else len(text)
        boundaries.append((0, first_heading_start, _PREAMBLE_KEY, _PREAMBLE_HEADING))
    for index, (start, _line_end, heading) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        boundaries.append((start, end, _slug(heading), heading))

    units: list[RiskFactorUnit] = []
    for start, end, key, heading in boundaries:
        unit_text = text[start:end]
        if not _normalize(unit_text):
            continue
        unit_chunks = [
            chunk
            for chunk, chunk_start, chunk_end in spans
            if chunk_start < end and chunk_end > start
        ]
        if not unit_chunks:
            raise _UnitParseFailure(
                f"unit '{key}' could not be mapped back to any indexed chunk"
            )
        units.append(
            RiskFactorUnit(
                unit_key=key,
                heading=heading,
                filing_id=filing_id,
                text=unit_text,
                content_hash=_content_hash(unit_text),
                chunks=unit_chunks,
            )
        )
    if not units:
        raise _UnitParseFailure("no risk-factor units could be extracted")
    return units


def align_units(
    previous_units: list[RiskFactorUnit], current_units: list[RiskFactorUnit]
) -> tuple[
    list[tuple[RiskFactorUnit, RiskFactorUnit]],
    list[RiskFactorUnit],
    list[RiskFactorUnit],
    list[str],
]:
    """Deterministic alignment: exact heading key, then exact content.

    Returns (aligned_pairs, unmatched_previous, unmatched_current,
    ambiguous_keys). A heading key appearing more than once on either side is
    ambiguous — its units are excluded from pairing and reported as such.
    """

    def by_key(units):
        grouped: dict[str, list[RiskFactorUnit]] = {}
        for unit in units:
            grouped.setdefault(unit.unit_key, []).append(unit)
        return grouped

    previous_by_key = by_key(previous_units)
    current_by_key = by_key(current_units)
    ambiguous = sorted(
        key
        for key in set(previous_by_key) | set(current_by_key)
        if len(previous_by_key.get(key, [])) > 1
        or len(current_by_key.get(key, [])) > 1
    )

    pairs: list[tuple[RiskFactorUnit, RiskFactorUnit]] = []
    unmatched_previous: list[RiskFactorUnit] = []
    unmatched_current: list[RiskFactorUnit] = []

    for key in sorted(set(previous_by_key) | set(current_by_key)):
        if key in ambiguous:
            continue
        in_previous = previous_by_key.get(key, [])
        in_current = current_by_key.get(key, [])
        if in_previous and in_current:
            pairs.append((in_previous[0], in_current[0]))
        elif in_previous:
            unmatched_previous.extend(in_previous)
        else:
            unmatched_current.extend(in_current)

    # Second pass: exact normalized-content matches among the leftovers
    # (covers a renamed heading over identical text). Only unique matches
    # pair; anything else stays unmatched.
    by_hash_current = {}
    for unit in unmatched_current:
        by_hash_current.setdefault(unit.content_hash, []).append(unit)
    still_previous: list[RiskFactorUnit] = []
    for unit in unmatched_previous:
        candidates = by_hash_current.get(unit.content_hash, [])
        if len(candidates) == 1:
            pairs.append((unit, candidates[0]))
            unmatched_current.remove(candidates[0])
            by_hash_current[unit.content_hash] = []
        else:
            still_previous.append(unit)

    return pairs, still_previous, unmatched_current, ambiguous


def _evidence_refs(chunks: list[SectionChunk]) -> list[dict[str, Any]]:
    """Schema-shaped EvidenceReferences for a unit's chunks.

    document_id is the chunk's filing_id (comparison.v1's expectation — never
    the family document_id); excerpts come from the indexed chunk text.
    """
    refs = []
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        refs.append(
            {
                "document_id": chunk.filing_id,
                "chunk_id": chunk.chunk_id,
                "source_name": chunk.source_name,
                "page": chunk.page,
                "section_key": SECTION_KEY_ITEM_1A,
                "section_title": chunk.section_title,
                # [:700] mirrors audit.MAX_EXCERPT_CHARS; the strip matches the
                # schema's NonEmptyStr normalization so the stored excerpt is
                # byte-identical to what validation would produce anyway.
                "excerpt": _normalize(chunk.text)[:700].strip(),
                "content_hash": hashlib.sha1(
                    chunk.text.encode("utf-8")
                ).hexdigest()[:12],
            }
        )
    return refs


def _change_id(change_type: str, unit_key: str) -> str:
    return "chg-" + hashlib.sha1(
        f"{change_type}:{unit_key}".encode("utf-8")
    ).hexdigest()[:12]


def _check(check, status, detail, *, reason_code=None, version=None):
    return {
        "check": check,
        "status": status,
        "reason_code": reason_code,
        "detail": detail,
        "validator_version": version,
    }


def _validation_checks(
    change_type: str,
    previous_evidence: list[dict],
    current_evidence: list[dict],
    previous_filing_id: str,
    current_filing_id: str,
) -> list[dict[str, Any]]:
    """Honest deterministic checks: the three implemented validators compute
    their outcome from the actual evidence; the unimplemented three are
    explicitly not_run — never silently passed."""
    checks: list[dict[str, Any]] = []

    if change_type == "undetermined":
        checks.append(
            _check(
                "evidence_presence",
                "not_applicable",
                "Undetermined changes carry an explicit reason instead of a "
                "change-type evidence invariant.",
                reason_code="undetermined_change",
                version="evidence_presence.v1",
            )
        )
    else:
        expectations = {
            "modified": bool(previous_evidence) and bool(current_evidence),
            "added": bool(current_evidence) and not previous_evidence,
            "removed": bool(previous_evidence) and not current_evidence,
        }
        satisfied = expectations[change_type]
        checks.append(
            _check(
                "evidence_presence",
                "passed" if satisfied else "failed",
                f"Evidence sides match the '{change_type}' invariant."
                if satisfied
                else f"Evidence sides violate the '{change_type}' invariant.",
                reason_code=None if satisfied else "evidence_side_invariant_violated",
                version="evidence_presence.v1",
            )
        )

    all_evidence = previous_evidence + current_evidence
    if not all_evidence:
        checks.append(
            _check(
                "entity_consistency",
                "not_applicable",
                "No evidence references to check.",
                reason_code="no_evidence",
                version="entity_consistency.v1",
            )
        )
        checks.append(
            _check(
                "period_consistency",
                "not_applicable",
                "No evidence references to check.",
                reason_code="no_evidence",
                version="period_consistency.v1",
            )
        )
    else:
        pair_ids = {previous_filing_id, current_filing_id}
        entity_ok = all(ref["document_id"] in pair_ids for ref in all_evidence)
        checks.append(
            _check(
                "entity_consistency",
                "passed" if entity_ok else "failed",
                "All evidence belongs to the two compared filings."
                if entity_ok
                else "Evidence references a filing outside the compared pair.",
                reason_code=None if entity_ok else "foreign_filing_evidence",
                version="entity_consistency.v1",
            )
        )
        period_ok = all(
            ref["document_id"] == previous_filing_id for ref in previous_evidence
        ) and all(
            ref["document_id"] == current_filing_id for ref in current_evidence
        )
        checks.append(
            _check(
                "period_consistency",
                "passed" if period_ok else "failed",
                "Each evidence side belongs to its chronological filing."
                if period_ok
                else "An evidence side references the wrong-period filing.",
                reason_code=None if period_ok else "wrong_period_evidence",
                version="period_consistency.v1",
            )
        )

    # citation_support / numeric_consistency / direction_consistency are
    # appended by detect_changes via comparison_validators.validate_change —
    # they need the resolved chunk texts, which only the section loads hold.
    return checks


def _change(
    change_type: str,
    unit_key: str,
    summary: str,
    *,
    previous_evidence: list[dict] | None = None,
    current_evidence: list[dict] | None = None,
    undetermined_reason: str | None = None,
    previous_filing_id: str,
    current_filing_id: str,
) -> dict[str, Any]:
    previous_evidence = previous_evidence or []
    current_evidence = current_evidence or []
    return {
        "change_id": _change_id(change_type, unit_key),
        "change_type": change_type,
        "category": "risk_factor",
        "section_key": SECTION_KEY_ITEM_1A,
        "summary": summary,
        "previous_evidence": previous_evidence,
        "current_evidence": current_evidence,
        "validation": _validation_checks(
            change_type,
            previous_evidence,
            current_evidence,
            previous_filing_id,
            current_filing_id,
        ),
        "undetermined_reason": undetermined_reason,
    }


def _section_unavailable_changes(
    previous_load: SectionLoad, current_load: SectionLoad, prev_id: str, curr_id: str
) -> list[dict[str, Any]] | None:
    """One undetermined change when either whole section is unavailable."""
    reasons = []
    if previous_load.status == "missing":
        reasons.append(
            (
                REASON_PREVIOUS_SECTION_MISSING,
                "the previous filing's Item 1A section was unavailable",
            )
        )
    if current_load.status == "missing":
        reasons.append(
            (
                REASON_CURRENT_SECTION_MISSING,
                "the current filing's Item 1A section was unavailable",
            )
        )
    if not reasons:
        return None
    code_text = "; ".join(
        f"{code}: {detail}" for code, detail in reasons
    )
    summary = (
        "The Item 1A section could not be compared because "
        + " and ".join(detail for _, detail in reasons)
        + "."
    )
    return [
        _change(
            "undetermined",
            "item-1a-section",
            summary,
            undetermined_reason=code_text,
            previous_filing_id=prev_id,
            current_filing_id=curr_id,
        )
    ]


def _finalize_changes(
    changes: list[dict[str, Any]],
    previous_load: SectionLoad,
    current_load: SectionLoad,
    previous_filing_id: str,
    current_filing_id: str,
) -> list[dict[str, Any]]:
    """Append the three content validators to every change.

    The resolver maps chunk ids to the already loaded section chunks — the
    same indexed chunks the evidence was built from, so citation/numeric
    checks verify against full chunk text (stored excerpts are 700-char
    capped and would false-fail headings that sit past the cap).
    """
    chunk_index = {
        chunk.chunk_id: {"text": chunk.text, "filing_id": chunk.filing_id}
        for chunk in previous_load.chunks + current_load.chunks
    }
    for change in changes:
        change["validation"].extend(
            comparison_validators.validate_change(
                change,
                resolve=chunk_index.get,
                previous_filing_id=previous_filing_id,
                current_filing_id=current_filing_id,
            )
        )
    return changes


def detect_changes(
    previous_load: SectionLoad,
    current_load: SectionLoad,
    previous_filing_id: str,
    current_filing_id: str,
) -> list[dict[str, Any]]:
    """Produce the deterministic change list for two section loads."""
    unavailable = _section_unavailable_changes(
        previous_load, current_load, previous_filing_id, current_filing_id
    )
    if unavailable is not None:
        return _finalize_changes(
            unavailable, previous_load, current_load,
            previous_filing_id, current_filing_id,
        )

    try:
        previous_units = extract_units(previous_load.chunks, previous_filing_id)
        current_units = extract_units(current_load.chunks, current_filing_id)
    except _UnitParseFailure as failure:
        return _finalize_changes(
            [
                _change(
                    "undetermined",
                    "item-1a-section",
                    "The Item 1A section could not be compared because risk-factor "
                    "units could not be parsed deterministically.",
                    undetermined_reason=f"{REASON_SECTION_UNIT_PARSE_FAILED}: {failure}",
                    previous_filing_id=previous_filing_id,
                    current_filing_id=current_filing_id,
                )
            ],
            previous_load, current_load, previous_filing_id, current_filing_id,
        )

    pairs, unmatched_previous, unmatched_current, ambiguous = align_units(
        previous_units, current_units
    )
    both_complete = previous_load.complete and current_load.complete
    incompleteness = previous_load.incomplete_reason or current_load.incomplete_reason

    keyed_changes: list[tuple[str, dict[str, Any]]] = []

    for previous_unit, current_unit in pairs:
        if previous_unit.content_hash == current_unit.content_hash:
            continue  # unchanged: not emitted (comparison.v1 has no unchanged type)
        prev_refs = _evidence_refs(previous_unit.chunks)
        curr_refs = _evidence_refs(current_unit.chunks)
        if not prev_refs or not curr_refs:
            keyed_changes.append(
                (
                    previous_unit.unit_key,
                    _change(
                        "undetermined",
                        previous_unit.unit_key,
                        f"Risk factor '{previous_unit.heading}' could not be "
                        "classified because its evidence could not be resolved "
                        "to indexed chunks.",
                        previous_evidence=prev_refs,
                        current_evidence=curr_refs,
                        undetermined_reason=(
                            f"{REASON_EVIDENCE_RESOLUTION_FAILED}: a side of the "
                            "aligned unit resolved to no indexed chunks"
                        ),
                        previous_filing_id=previous_filing_id,
                        current_filing_id=current_filing_id,
                    ),
                )
            )
            continue
        keyed_changes.append(
            (
                previous_unit.unit_key,
                _change(
                    "modified",
                    previous_unit.unit_key,
                    f"Risk factor '{current_unit.heading}' changed between the "
                    "two filing periods.",
                    previous_evidence=prev_refs,
                    current_evidence=curr_refs,
                    previous_filing_id=previous_filing_id,
                    current_filing_id=current_filing_id,
                ),
            )
        )

    for key in ambiguous:
        keyed_changes.append(
            (
                key,
                _change(
                    "undetermined",
                    key,
                    f"Risk-factor heading '{key}' appears more than once in a "
                    "filing; alignment is ambiguous.",
                    undetermined_reason=(
                        f"{REASON_AMBIGUOUS_UNIT_ALIGNMENT}: heading key "
                        f"'{key}' is not unique within a filing"
                    ),
                    previous_filing_id=previous_filing_id,
                    current_filing_id=current_filing_id,
                ),
            )
        )

    for unit in unmatched_current:
        if both_complete:
            keyed_changes.append(
                (
                    unit.unit_key,
                    _change(
                        "added",
                        unit.unit_key,
                        f"Risk factor '{unit.heading}' appears in the current "
                        "filing and was not found in the complete previous "
                        "Item 1A section.",
                        current_evidence=_evidence_refs(unit.chunks),
                        previous_filing_id=previous_filing_id,
                        current_filing_id=current_filing_id,
                    ),
                )
            )
        else:
            keyed_changes.append(
                (
                    unit.unit_key,
                    _change(
                        "undetermined",
                        unit.unit_key,
                        f"Risk factor '{unit.heading}' was found only in the "
                        "current filing, but section completeness could not be "
                        "established, so 'added' cannot be claimed.",
                        current_evidence=_evidence_refs(unit.chunks),
                        undetermined_reason=(
                            f"{REASON_SECTION_METADATA_INCOMPLETE}: "
                            f"{incompleteness or 'completeness unprovable'}"
                        ),
                        previous_filing_id=previous_filing_id,
                        current_filing_id=current_filing_id,
                    ),
                )
            )

    for unit in unmatched_previous:
        if both_complete:
            keyed_changes.append(
                (
                    unit.unit_key,
                    _change(
                        "removed",
                        unit.unit_key,
                        f"Risk factor '{unit.heading}' appears in the previous "
                        "filing and was not found in the complete current "
                        "Item 1A section.",
                        previous_evidence=_evidence_refs(unit.chunks),
                        previous_filing_id=previous_filing_id,
                        current_filing_id=current_filing_id,
                    ),
                )
            )
        else:
            keyed_changes.append(
                (
                    unit.unit_key,
                    _change(
                        "undetermined",
                        unit.unit_key,
                        f"Risk factor '{unit.heading}' was found only in the "
                        "previous filing, but section completeness could not "
                        "be established, so 'removed' cannot be claimed.",
                        previous_evidence=_evidence_refs(unit.chunks),
                        undetermined_reason=(
                            f"{REASON_SECTION_METADATA_INCOMPLETE}: "
                            f"{incompleteness or 'completeness unprovable'}"
                        ),
                        previous_filing_id=previous_filing_id,
                        current_filing_id=current_filing_id,
                    ),
                )
            )

    keyed_changes.sort(key=lambda pair: (pair[0], pair[1]["change_type"]))
    return _finalize_changes(
        [change for _key, change in keyed_changes],
        previous_load, current_load, previous_filing_id, current_filing_id,
    )


def _result_hash(wire: dict[str, Any]) -> str:
    """sha256 over the timestamp-independent canonical form: two runs over the
    same index produce the same hash."""
    stable = {key: value for key, value in wire.items() if key != "created_at"}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def detect(
    comparison_id: str,
    *,
    db_path: str | Path | None = None,
    registry_path: str | Path | None = None,
    chroma_client=None,
) -> tuple[dict[str, Any], bool]:
    """Run detection for a persisted comparison; returns (wire_result, created).

    Thin wrapper over ``detect_with_attempt`` that drops the attempt id, so
    every existing caller keeps its two-tuple contract.
    """
    result, created, _attempt_id = detect_with_attempt(
        comparison_id,
        db_path=db_path,
        registry_path=registry_path,
        chroma_client=chroma_client,
    )
    return result, created


def detect_with_attempt(
    comparison_id: str,
    *,
    db_path: str | Path | None = None,
    registry_path: str | Path | None = None,
    chroma_client=None,
    actor_context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], bool, str | None]:
    """Detection with the durable attempt id exposed: (result, created, attempt).

    Idempotent: an existing result under the same detector version and the
    same registry source hashes is returned as-is (created=False), together
    with the id of the attempt that produced it when one was recorded (results
    predating attempt tracking simply have none). Changed source hashes or a
    different detector version raise DetectionInputsStale /
    DetectionVersionSuperseded — the stored result is never silently presented
    as current and never overwritten.

    Every execution that actually starts is durable: ``start_detection_attempt``
    commits a running attempt, a detection_started event, and the comparison's
    ``detecting`` state BEFORE the index is read, and exactly one of
    ``complete_detection_attempt`` / ``fail_detection_attempt`` finalizes it in
    a single transaction. No SQLite transaction is held while Chroma is read or
    the detector runs.

    Requests rejected before an attempt starts (unknown comparison, invalid
    pair, stale inputs, superseded version, terminal lifecycle, another attempt
    already running) create NO attempt — a rejected request is not a failed
    execution.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    registry_path = registry_path or config.FILING_REGISTRY_PATH

    record = comparison_store.get_comparison(comparison_id, db_path=db_path)
    if record is None:
        raise UnknownComparison(
            "comparison_not_found", "comparison does not exist"
        )

    # Registry truth for the pair (re-validated at detect time) + input hashes.
    previous_ref, current_ref = comparison_store.validate_pair(
        record["previous_filing_id"], record["current_filing_id"], registry_path
    )
    previous_entry = filing_registry.get_filing(
        record["previous_filing_id"], registry_path
    )
    current_entry = filing_registry.get_filing(
        record["current_filing_id"], registry_path
    )
    previous_hash = previous_entry.get("source_hash") or ""
    current_hash = current_entry.get("source_hash") or ""

    outcome = _stored_outcome(comparison_id, previous_hash, current_hash, db_path)
    if outcome is not None:
        return outcome

    # Durably start the execution. This commits the running attempt, the
    # detection_started event, and the 'detecting' transition in ONE
    # transaction, then closes it — nothing below runs inside a SQLite
    # transaction. Concurrent callers race here and exactly one wins.
    try:
        attempt = comparison_store.start_detection_attempt(
            comparison_id,
            detector_version=DETECTOR_VERSION,
            workflow_version=comparison_store.WORKFLOW_VERSION,
            previous_source_hash=previous_hash,
            current_source_hash=current_hash,
            db_path=db_path,
        )
    except comparison_store.DetectionStateError as exc:
        if exc.code == comparison_store.REASON_DETECTION_IN_PROGRESS:
            raise DetectionInProgress(
                REASON_DETECTION_IN_PROGRESS,
                "a detection attempt is already running for this comparison; "
                "it is not started twice and is not recovered automatically",
            ) from exc
        # comparison_not_ready. A concurrent request may have COMPLETED
        # between our stored-result read above and this start transaction, so
        # re-evaluate the canonical stored result before reporting a lifecycle
        # error: the loser of that race gets the winner's result under the
        # existing idempotency contract, not a spurious 409. The message comes
        # from the exception (read inside the transaction), never from our own
        # stale read.
        outcome = _stored_outcome(
            comparison_id, previous_hash, current_hash, db_path
        )
        if outcome is not None:
            return outcome
        raise DetectionNotReady(REASON_COMPARISON_NOT_READY, exc.message) from exc

    # Logged AFTER the start transaction committed, so the record can never
    # describe a transition that rolled back (Stage 3.5 step 3).
    comparison_reliability.log_lifecycle_event(
        comparison_reliability.EVENT_ATTEMPT_STARTED,
        attempt=attempt,
        actor_context=actor_context,
    )

    return execute_attempt(
        attempt["attempt_id"],
        record=record,
        previous_ref=previous_ref,
        current_ref=current_ref,
        previous_entry=previous_entry,
        current_entry=current_entry,
        previous_hash=previous_hash,
        current_hash=current_hash,
        chroma_client=chroma_client,
        db_path=db_path,
        actor_context=actor_context,
    )


def execute_attempt(
    attempt_id: str,
    *,
    record: dict[str, Any],
    previous_ref,
    current_ref,
    previous_entry: dict[str, Any],
    current_entry: dict[str, Any],
    previous_hash: str,
    current_hash: str,
    chroma_client=None,
    db_path: str | Path | None = None,
    actor_context: Mapping[str, Any] | None = None,
    job_id: str | None = None,
    worker_id: str | None = None,
    claim_generation: int | None = None,
    claim_token: str | None = None,
    job_now: datetime | None = None,
    retry_policy: dict[str, Any] | None = None,
    max_claim_generations: int | None = None,
) -> tuple[dict[str, Any], bool, str | None]:
    """Compute and finalize ONE already-started running attempt.

    The shared execution seam: ``detect_with_attempt`` calls it for a first
    attempt and ``detection_recovery.replay_attempt`` calls it for a
    replacement, so both paths get byte-identical computation, the same
    single-transaction success finalization, and the same failure-code
    semantics. The caller is responsible for having already committed the
    running attempt — reaching this function always implies a durable attempt
    exists.

    Returns ``(wire_result, created, attempt_id)``. On failure the attempt is
    finalized durably and the original condition is re-raised.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    comparison_id = record["comparison_id"]
    job_execution = any(
        value is not None
        for value in (job_id, worker_id, claim_generation, claim_token)
    )
    if job_execution and not (
        all(
            isinstance(value, str) and value
            for value in (job_id, worker_id, claim_token)
        )
        and isinstance(claim_generation, int)
        and not isinstance(claim_generation, bool)
        and claim_generation >= 1
    ):
        raise ValueError(
            "job_id, worker_id, claim_generation, and claim_token must be "
            "supplied together"
        )
    try:
        wire = _compute_result(
            record, previous_ref, current_ref, previous_entry, current_entry,
            chroma_client,
        )

        try:
            # Result insert + attempt succeeded + detection_succeeded event +
            # detecting -> detected, all in one transaction.
            result_hash = _result_hash(wire)
            if job_execution:
                terminal = comparison_store.complete_detection_job(
                    job_id,
                    attempt_id,
                    worker_id=worker_id,
                    claim_generation=claim_generation,
                    claim_token=claim_token,
                    result_json=json.dumps(wire),
                    result_hash=result_hash,
                    detector_version=DETECTOR_VERSION,
                    workflow_version=comparison_store.WORKFLOW_VERSION,
                    previous_source_hash=previous_hash,
                    current_source_hash=current_hash,
                    now=job_now,
                    db_path=db_path,
                )
                finalized = terminal["attempt"]
            else:
                finalized = comparison_store.complete_detection_attempt(
                    attempt_id,
                    result_json=json.dumps(wire),
                    result_hash=result_hash,
                    detector_version=DETECTOR_VERSION,
                    workflow_version=comparison_store.WORKFLOW_VERSION,
                    previous_source_hash=previous_hash,
                    current_source_hash=current_hash,
                    db_path=db_path,
                )
        except comparison_store.ComparisonResultExists:
            # Defensive: the running-attempt constraint makes a concurrent
            # identical detection unreachable, but a result is never
            # overwritten, so serve the stored one.
            if job_execution:
                raise
            stored = comparison_store.get_result(comparison_id, db_path=db_path)
            return stored["result"], False, attempt_id
        comparison_reliability.log_lifecycle_event(
            comparison_reliability.EVENT_ATTEMPT_SUCCEEDED,
            attempt=finalized,
            elapsed_ms=comparison_reliability.elapsed_ms_for(finalized),
            actor_context=actor_context,
        )
        return wire, True, attempt_id
    except DetectionError as exc:
        # A KNOWN domain condition raised AFTER the attempt started: the
        # execution really ran and really failed, so finalize it durably under
        # its OWN stable code. Collapsing it into detector_internal_error would
        # destroy exactly the diagnostic the attempt record exists to keep.
        _finalize_failed_attempt(
            attempt_id,
            _persisted_failure_code(exc),
            db_path,
            actor_context=actor_context,
            job_id=job_id,
            worker_id=worker_id,
            claim_generation=claim_generation,
            claim_token=claim_token,
            job_now=job_now,
            retry_policy=retry_policy,
            max_claim_generations=max_claim_generations,
        )
        raise
    except Exception as exc:
        if (
            job_execution
            and isinstance(exc, comparison_store.DetectionStateError)
            and exc.code in comparison_store.JOB_OWNERSHIP_LOST_CODES
        ):
            current_job = comparison_store.get_detection_job(
                job_id, db_path=db_path
            )
            current_attempt = comparison_store.get_detection_attempt(
                attempt_id, db_path=db_path
            )
            comparison_reliability.log_detection_job_event(
                comparison_reliability.EVENT_JOB_FINALIZE_REJECTED,
                job=current_job or {
                    "job_id": job_id,
                    "comparison_id": comparison_id,
                    "attempt_id": attempt_id,
                    "claim_generation": claim_generation,
                },
                attempt=current_attempt,
                source_attempt_id=(
                    attempt_id
                    if current_job
                    and current_job.get("attempt_id") != attempt_id
                    else None
                ),
                replacement_attempt_id=(
                    current_job.get("attempt_id")
                    if current_job
                    and current_job.get("attempt_id") != attempt_id
                    else None
                ),
                failure_code=exc.code,
            )
            raise
        # Unexpected, non-domain fault. Full diagnostics are logged here so they
        # survive regardless of caller (the API layer logs too, but the CLI and
        # the regression runner do not).
        logger.exception(
            "Unexpected detector fault on attempt %s; recording it as %s",
            attempt_id,
            REASON_DETECTOR_INTERNAL_ERROR,
        )
        _finalize_failed_attempt(
            attempt_id,
            REASON_DETECTOR_INTERNAL_ERROR,
            db_path,
            actor_context=actor_context,
            job_id=job_id,
            worker_id=worker_id,
            claim_generation=claim_generation,
            claim_token=claim_token,
            job_now=job_now,
            retry_policy=retry_policy,
            max_claim_generations=max_claim_generations,
        )
        # Chained so the API layer's logger.exception also captures the real
        # fault; the DetectionInternalError message itself stays safe.
        raise DetectionInternalError(
            REASON_DETECTOR_INTERNAL_ERROR,
            "detection failed unexpectedly",
        ) from exc


def resolve_detection_inputs(
    comparison_id: str,
    *,
    db_path: str | Path | None = None,
    registry_path: str | Path | None = None,
) -> dict[str, Any]:
    """Registry truth for one comparison: record, both refs, entries, hashes.

    Shared by detection and replay so both resolve inputs identically. Raises
    UnknownComparison for an absent comparison and ComparisonPairError when the
    pair no longer validates against the registry.
    """
    db_path = db_path or config.COMPARISON_DB_PATH
    registry_path = registry_path or config.FILING_REGISTRY_PATH

    record = comparison_store.get_comparison(comparison_id, db_path=db_path)
    if record is None:
        raise UnknownComparison(
            "comparison_not_found", "comparison does not exist"
        )
    previous_ref, current_ref = comparison_store.validate_pair(
        record["previous_filing_id"], record["current_filing_id"], registry_path
    )
    previous_entry = filing_registry.get_filing(
        record["previous_filing_id"], registry_path
    )
    current_entry = filing_registry.get_filing(
        record["current_filing_id"], registry_path
    )
    return {
        "record": record,
        "previous_ref": previous_ref,
        "current_ref": current_ref,
        "previous_entry": previous_entry,
        "current_entry": current_entry,
        "previous_hash": previous_entry.get("source_hash") or "",
        "current_hash": current_entry.get("source_hash") or "",
    }


def _compute_result(
    record: dict[str, Any],
    previous_ref,
    current_ref,
    previous_entry: dict[str, Any],
    current_entry: dict[str, Any],
    chroma_client,
) -> dict[str, Any]:
    """PURE computation: load both sections, detect changes, build the wire doc.

    Deliberately performs NO lifecycle persistence — no attempt, no event, no
    result row, no status transition. Keeping the boundary explicit is what lets
    the orchestrator guarantee that every execution which reaches this function
    is already covered by a durable running attempt, and that a result can only
    be committed by the attempt that produced it.
    """
    comparison_id = record["comparison_id"]
    section_key = record["section_scope"][0]
    client = chroma_client if chroma_client is not None else open_index()
    previous_load = load_section(
        record["previous_filing_id"], section_key, client, previous_entry
    )
    current_load = load_section(
        record["current_filing_id"], section_key, client, current_entry
    )
    changes = detect_changes(
        previous_load,
        current_load,
        record["previous_filing_id"],
        record["current_filing_id"],
    )
    result_model = load_comparison(
        {
            "schema_version": record["schema_version"],
            "comparison_id": comparison_id,
            "previous_filing": previous_ref.model_dump(mode="json"),
            "current_filing": current_ref.model_dump(mode="json"),
            "section_scope": record["section_scope"],
            "changes": changes,
            "validation_summary": summarize_validation(
                [FilingChange.model_validate(change) for change in changes]
            ).model_dump(),
            "risk": {"decision": "not_evaluated"},
            "review": {"status": "not_required"},
            "created_at": _utc_now_iso(),
            "producer": DETECTOR_VERSION,
        }
    )
    return dump_comparison(result_model)


def _persisted_failure_code(exc: DetectionError) -> str:
    """The stable code to persist for a known post-start domain failure.

    Only codes on the module's own allowlist are stored; anything else — an
    unrecognized or dynamically built code — collapses to
    detector_internal_error, so storage can never receive a code this module
    does not define.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in DOMAIN_FAILURE_CODES:
        return code
    return REASON_DETECTOR_INTERNAL_ERROR


def _stored_outcome(
    comparison_id: str,
    previous_hash: str,
    current_hash: str,
    db_path: str | Path,
) -> tuple[dict[str, Any], bool, str | None] | None:
    """Evaluate an already-stored result against the current inputs.

    Returns the idempotent (result, False, attempt_id) response when a stored
    result is current, None when no result exists, and raises the existing
    409-shaped conditions otherwise. Called both before starting an attempt and
    again if the start loses a race to a concurrent execution, so the two paths
    can never disagree about what a stored result means.
    """
    stored = comparison_store.get_result(comparison_id, db_path=db_path)
    if stored is None:
        return None
    if stored["detector_version"] != DETECTOR_VERSION:
        # Not an input change: the sources are whatever they were — the
        # DETECTOR moved. The old result stays readable via GET; a re-run under
        # the current version is a new comparison (new workflow version -> new
        # comparison id), never an overwrite.
        raise DetectionVersionSuperseded(
            REASON_VERSION_SUPERSEDED,
            f"a result from {stored['detector_version']} exists; the current "
            f"detector is {DETECTOR_VERSION}. The stored result remains "
            "readable as old-version output; re-detect by creating the "
            "comparison under the current workflow version",
        )
    if (
        stored["previous_source_hash"] == previous_hash
        and stored["current_source_hash"] == current_hash
    ):
        return (
            stored["result"],
            False,
            _succeeded_attempt_id(comparison_id, stored["result_hash"], db_path),
        )
    raise DetectionInputsStale(
        REASON_INPUTS_STALE,
        "a detection result exists but its source content changed; the stored "
        "result is not silently presented as current and is never overwritten",
    )


def _finalize_failed_attempt(
    attempt_id: str,
    failure_code: str,
    db_path: str | Path,
    *,
    actor_context: Mapping[str, Any] | None = None,
    job_id: str | None = None,
    worker_id: str | None = None,
    claim_generation: int | None = None,
    claim_token: str | None = None,
    job_now: datetime | None = None,
    retry_policy: dict[str, Any] | None = None,
    max_claim_generations: int | None = None,
) -> None:
    """Mark the running attempt failed, never masking the original fault.

    The summary is derived from the stable code via the allowlist, never from an
    exception message. If this transaction itself fails, nothing is applied —
    the attempt stays running and the comparison stays detecting (the documented
    interruption boundary) — and the original detector error still propagates,
    because a storage problem here must not replace the real cause in the
    client's error path.

    The structured failure record is emitted only after the transaction
    committed, and carries the STABLE failure code — never exception text.
    """
    try:
        if job_id is not None:
            terminal = comparison_store.fail_detection_job(
                job_id,
                attempt_id,
                worker_id=worker_id,
                claim_generation=claim_generation,
                claim_token=claim_token,
                failure_code=failure_code,
                failure_summary=_safe_failure_summary(failure_code),
                retry_policy=retry_policy or detection_job_retry.POLICY,
                max_claim_generations=(
                    max_claim_generations
                    if max_claim_generations is not None
                    else 1
                ),
                now=job_now,
                db_path=db_path,
            )
            finalized = terminal["attempt"]
        else:
            finalized = comparison_store.fail_detection_attempt(
                attempt_id,
                failure_code=failure_code,
                failure_summary=_safe_failure_summary(failure_code),
                db_path=db_path,
            )
    except comparison_store.DetectionStateError as exc:
        if (
            job_id is not None
            and exc.code in comparison_store.JOB_OWNERSHIP_LOST_CODES
        ):
            current_job = comparison_store.get_detection_job(
                job_id, db_path=db_path
            )
            current_attempt = comparison_store.get_detection_attempt(
                attempt_id, db_path=db_path
            )
            comparison_reliability.log_detection_job_event(
                comparison_reliability.EVENT_JOB_FINALIZE_REJECTED,
                job=current_job or {
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "claim_generation": claim_generation,
                },
                attempt=current_attempt,
                source_attempt_id=(
                    attempt_id
                    if current_job
                    and current_job.get("attempt_id") != attempt_id
                    else None
                ),
                replacement_attempt_id=(
                    current_job.get("attempt_id")
                    if current_job
                    and current_job.get("attempt_id") != attempt_id
                    else None
                ),
                failure_code=exc.code,
            )
            raise
        logger.exception(
            "Could not finalize detection attempt %s as failed; it remains "
            "running and its comparison remains 'detecting'",
            attempt_id,
        )
        if job_id is not None:
            raise
        return
    except Exception:
        logger.exception(
            "Could not finalize detection attempt %s as failed; it remains "
            "running and its comparison remains 'detecting'",
            attempt_id,
        )
        if job_id is not None:
            raise
        return
    comparison_reliability.log_lifecycle_event(
        comparison_reliability.EVENT_ATTEMPT_FAILED,
        attempt=finalized,
        elapsed_ms=comparison_reliability.elapsed_ms_for(finalized),
        actor_context=actor_context,
    )


def _succeeded_attempt_id(
    comparison_id: str, result_hash: str, db_path: str | Path
) -> str | None:
    """The attempt that produced a stored result, when one was recorded.

    Results are never overwritten and at most one attempt can succeed per
    result, so matching on result_hash is unambiguous. Returns None for
    results stored before attempt tracking existed (or through the plain
    ``record_result`` library path).
    """
    for attempt in comparison_store.list_detection_attempts(
        comparison_id, db_path=db_path
    ):
        if (
            attempt["status"] == comparison_store.ATTEMPT_SUCCEEDED
            and attempt["result_hash"] == result_hash
        ):
            return attempt["attempt_id"]
    return None
