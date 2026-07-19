"""Runtime grounding validation.

This is the eval runner's grounding logic moved to runtime, so every answer gets
a citation-coverage and grounding score as it is returned, not only during eval.

The rule-based primitives below (citation parsing, claim extraction, evidence
matching) are copied verbatim from ``eval_runner.py`` and must stay identical to
it. We copy rather than import because ``eval_runner`` imports ``agent`` at module
load, and ``agent`` imports this module; importing eval_runner here would form a
cycle. ``tests/test_grounding_validator.py`` pins the two implementations to the
same output on identical inputs, so any drift between them fails the suite.

Rule-based and deterministic by design. No LLM call. The grounding check is the
numeric-claim support rule from eval_runner, so ``unsupported_claims`` lists
numeric tokens (e.g. "50%", "21"), not full sentences.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- Copied verbatim from eval_runner.py. Keep in sync (see module docstring). ---

CITATION_RE = re.compile(
    r"(?P<source>[\w.-]+\.(?:pdf|md|txt))(?:[^.\n]{0,80}?\b(?:page\s+|p\.\s*)(?P<page>\d+))?",
    re.IGNORECASE,
)
CLAIM_RE = re.compile(
    r"\$?\b\d+(?:\.\d+)?\s*(?:%|million|billion|days?|hours?|quarters?|years?)?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Citation:
    source_name: str
    page: int | None = None


def _source_name(source: str | None) -> str:
    return Path(source or "").name


def _source_matches(expected: _Citation, actual: dict[str, Any]) -> bool:
    actual_name = actual.get("source_name") or _source_name(actual.get("source"))
    if actual_name != expected.source_name:
        return False
    if expected.page is None:
        return True
    return actual.get("page") == expected.page


def parse_citations(answer: str) -> list[_Citation]:
    """Parse simple filename and optional page citations from answer text."""
    citations: list[_Citation] = []
    seen = set()
    for match in CITATION_RE.finditer(answer):
        page = match.group("page")
        citation = _Citation(
            source_name=match.group("source"),
            page=int(page) if page is not None else None,
        )
        key = (citation.source_name, citation.page)
        if key in seen:
            continue
        seen.add(key)
        citations.append(citation)
    return citations


def citation_coverage(answer: str, retrieved_sources: list[dict[str, Any]]) -> float:
    """Return the share of parsed citations that match retrieved metadata."""
    citations = parse_citations(answer)
    if not citations:
        return 1.0 if not retrieved_sources else 0.0

    matched = 0
    for citation in citations:
        if any(_source_matches(citation, actual) for actual in retrieved_sources):
            matched += 1
    return matched / len(citations)


def extract_claim_tokens(answer: str) -> list[str]:
    """Extract simple numeric claims for rule-based support checks."""
    tokens: list[str] = []
    seen = set()
    for match in CLAIM_RE.finditer(answer):
        token = " ".join(match.group(0).lower().split())
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def evidence_text(retrieved_sources: list[dict[str, Any]]) -> str:
    """Join retrieved evidence for claim-support checks.

    Prefers the full chunk text (``content``) when a source carries one and
    falls back to the audit-capped ``excerpt`` otherwise, so a number sitting
    past the excerpt cap still counts as supported.
    """
    return "\n".join(
        str(source.get("content") or source.get("excerpt", ""))
        for source in retrieved_sources
    )


# --- Runtime-only composition on top of the shared primitives. ---


def validate(response_text: str, retrieved_chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate an answer against its retrieved evidence.

    Returns citation coverage, a grounding score, and the unsupported numeric
    claims found in the answer. All values are deterministic functions of the
    answer text and the retrieved chunks.
    """
    chunks = retrieved_chunks or []
    coverage = citation_coverage(response_text, chunks)

    claims = extract_claim_tokens(response_text)
    normalized_evidence = evidence_text(chunks).lower()
    unsupported_claims = [claim for claim in claims if claim not in normalized_evidence]
    unsupported_rate = len(unsupported_claims) / len(claims) if claims else 0.0

    # Grounding combines the two signals eval_runner already measures: how many
    # citations resolve to retrieved sources, and how many numeric claims the
    # evidence supports. Equal weight, range 0..1.
    grounding_score = (coverage + (1.0 - unsupported_rate)) / 2.0

    return {
        "citation_coverage": round(coverage, 4),
        "grounding_score": round(grounding_score, 4),
        "unsupported_claims": unsupported_claims,
        "unsupported_claim_count": len(unsupported_claims),
    }
