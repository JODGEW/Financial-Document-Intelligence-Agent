"""Deterministic comparison-result validators (roadmap step 7).

Implements the three previously not_run checks for comparison.v1 changes:

- citation_support — is the deterministic summary actually supported by the
  evidence attached to the change (heading present on the required sides,
  references resolvable to indexed chunks of the correct filing, undetermined
  reasons coherent with the evidence shape)?
- numeric_consistency — is every numeric token claimed IN THE SUMMARY
  supported by the required evidence side, under boundary-safe normalized
  matching?
- direction_consistency — does an explicit comparison direction in the
  summary (increased / decreased / unchanged, or the added/removed structural
  markers) match the change type and, where numeric, the attributed values?

All three are conservative lexical/numeric rules, not semantic entailment:
they prefer not_applicable (no claim of that class) or an explicit failure
over ever claiming passed without enough information. No LLM, no embeddings,
no network — inputs are the change dict plus a resolver over the already
loaded section chunks.

Why these primitives are NOT governance/grounding_validator's: that module's
claim check is deliberately substring-based ("21" would match "210") and its
regex ignores comma grouping — acceptable for coarse chat grounding, banned
here. These validators use full-token extraction with Decimal normalization
(commas, signs, $, %, trailing zeros) and kind-strict equality (a percent
claim never matches a plain page-like number). No million/billion unit
inference exists on purpose. The grounding_validator/eval_runner pair stays
byte-identical and untouched.

Every check emits: status, a stable reason_code, a stable validator_version,
and a safe human-readable detail (headings and numeric tokens only — never
full excerpts, document text, or paths).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

CITATION_SUPPORT_VERSION = "citation_support.v1"
NUMERIC_CONSISTENCY_VERSION = "numeric_consistency.v1"
DIRECTION_CONSISTENCY_VERSION = "direction_consistency.v1"

# citation_support reason codes
CITATION_SUMMARY_SUPPORTED = "citation_summary_supported"
CITATION_HEADING_NOT_SUPPORTED = "citation_heading_not_supported"
CITATION_WRONG_SIDE = "citation_wrong_side"
CITATION_EVIDENCE_UNRESOLVABLE = "citation_evidence_unresolvable"
CITATION_UNDETERMINED_REASON_MISMATCH = "citation_undetermined_reason_mismatch"

# numeric_consistency reason codes
NUMERIC_VALUES_SUPPORTED = "numeric_values_supported"
NUMERIC_NO_CLAIM = "no_numeric_claim"
NUMERIC_VALUE_UNSUPPORTED = "numeric_value_unsupported"
NUMERIC_ATTRIBUTION_AMBIGUOUS = "numeric_attribution_ambiguous"

# direction_consistency reason codes
DIRECTION_SUPPORTED = "direction_supported"
DIRECTION_NO_CLAIM = "no_directional_claim"
DIRECTION_INVERTED = "direction_inverted"
DIRECTION_UNVERIFIABLE = "direction_unverifiable"
DIRECTION_UNDETERMINED = "undetermined_change"

# The stable reason prefixes an undetermined_reason may carry (kept in sync
# with comparison_detector's REASON_* data codes) and the evidence shape each
# one permits: (previous_evidence_allowed, current_evidence_allowed).
_UNDETERMINED_SHAPES = {
    "previous_section_missing": (False, True),
    "current_section_missing": (True, False),
    "section_metadata_incomplete": (True, True),
    "section_unit_parse_failed": (True, True),
    "ambiguous_unit_alignment": (True, True),
    "evidence_resolution_failed": (True, True),
}

# "Risk factor 'X' ..." / "Risk-factor heading 'X' ..." in the deterministic
# summary templates. Headings containing a single quote are a documented v1
# parsing limitation.
_HEADING_CLAIM_RE = re.compile(r"[Rr]isk[- ]factor(?:\s+heading)?\s+'([^']+)'")

# Numeric token in summary or evidence text: optional $, digits with optional
# comma grouping and decimals, optional %. Lookarounds forbid word/number
# neighbours so "21" is never found inside "210", "x21", or "1.21", while a
# sentence-final "$50." still matches (a bare trailing dot is punctuation,
# only ".<digit>" marks an unconsumed decimal).
_NUMBER_RE = re.compile(
    r"(?<![\w.\d])(?P<currency>\$)?"
    r"(?P<value>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<percent>%)?(?![\w%])(?!\.\d)"
)

# "from X to Y" side attribution inside a summary (X -> previous, Y -> current).
_FROM_TO_RE = re.compile(
    r"from\s+(?P<from>\$?[\d,.]+%?)\s+to\s+(?P<to>\$?[\d,.]+%?)"
)

_DIRECTION_WORDS = {
    "increased": "increase",
    "decreased": "decrease",
    "unchanged": "unchanged",
}

_ADDED_MARKER = "appears in the current filing"
_REMOVED_MARKER = "appears in the previous filing"


def _normalize_text(text: str) -> str:
    return " ".join((text or "").split())


def _check(check, status, detail, *, reason_code, version):
    return {
        "check": check,
        "status": status,
        "reason_code": reason_code,
        "detail": detail,
        "validator_version": version,
    }


# --- Numeric primitives (boundary-safe, kind-aware) ---------------------------


def parse_numeric_token(token: str):
    """Normalize one numeric token to (kind, Decimal) or None.

    kind is 'currency' ($ prefix), 'percent' (% suffix), or 'plain'. Commas
    are grouping only; signs/decimals normalize via Decimal, so '50', '50.0',
    and '50,' + trailing-zero variants compare equal within a kind. There is
    deliberately no million/billion unit inference.
    """
    match = _NUMBER_RE.fullmatch(token.strip().rstrip(".,;:)"))
    if not match:
        return None
    kind = (
        "currency"
        if match.group("currency")
        else "percent"
        if match.group("percent")
        else "plain"
    )
    try:
        value = Decimal(match.group("value").replace(",", ""))
    except InvalidOperation:
        return None
    return kind, value.normalize()


def extract_numbers(text: str) -> list[tuple[str, Decimal]]:
    """All normalized (kind, value) numeric tokens in a text, full-token only."""
    numbers = []
    for match in _NUMBER_RE.finditer(text or ""):
        parsed = parse_numeric_token(match.group(0))
        if parsed is not None:
            numbers.append(parsed)
    return numbers


def _summary_numbers(summary: str) -> list[tuple[str, Decimal]]:
    return extract_numbers(summary)


def _display(kind: str, value: Decimal) -> str:
    if kind == "currency":
        return f"${value}"
    if kind == "percent":
        return f"{value}%"
    return str(value)


# --- Evidence resolution helpers ---------------------------------------------


def _resolve_side(
    refs: list[dict[str, Any]],
    expected_filing_id: str,
    resolve: Callable[[str], dict[str, Any] | None],
):
    """Resolve one evidence side. Returns (texts, unresolvable, wrong_side)."""
    texts: list[str] = []
    unresolvable: list[str] = []
    wrong_side: list[str] = []
    for ref in refs:
        resolved = resolve(ref.get("chunk_id") or "")
        if resolved is None:
            unresolvable.append(ref.get("chunk_id") or "<missing chunk_id>")
            continue
        if resolved.get("filing_id") != expected_filing_id:
            wrong_side.append(ref.get("chunk_id") or "<missing chunk_id>")
            continue
        texts.append(_normalize_text(resolved.get("text") or ""))
    return texts, unresolvable, wrong_side


def _heading_in(texts: list[str], heading: str) -> bool:
    needle = _normalize_text(heading)
    return any(needle in text for text in texts)


# --- citation_support ---------------------------------------------------------


def citation_support(
    change: dict[str, Any],
    *,
    resolve: Callable[[str], dict[str, Any] | None],
    previous_filing_id: str,
    current_filing_id: str,
) -> dict[str, Any]:
    """Is the deterministic summary supported by the change's own evidence?

    Not a re-skin of evidence_presence: list non-emptiness is that check's
    job; this one verifies content support — the named heading appears in the
    resolved text of every side the change type requires, every reference
    resolves to an indexed chunk of the filing its side cites, and an
    undetermined reason matches the actual evidence shape.
    """
    change_type = change.get("change_type")
    summary = change.get("summary") or ""

    previous_texts, previous_unresolvable, previous_wrong = _resolve_side(
        change.get("previous_evidence") or [], previous_filing_id, resolve
    )
    current_texts, current_unresolvable, current_wrong = _resolve_side(
        change.get("current_evidence") or [], current_filing_id, resolve
    )

    unresolvable = previous_unresolvable + current_unresolvable
    if unresolvable:
        return _check(
            "citation_support",
            "failed",
            f"{len(unresolvable)} evidence reference(s) do not resolve to an "
            "indexed chunk.",
            reason_code=CITATION_EVIDENCE_UNRESOLVABLE,
            version=CITATION_SUPPORT_VERSION,
        )
    if previous_wrong or current_wrong:
        return _check(
            "citation_support",
            "failed",
            f"{len(previous_wrong) + len(current_wrong)} evidence reference(s) "
            "resolve to a chunk of a different filing than their cited side.",
            reason_code=CITATION_WRONG_SIDE,
            version=CITATION_SUPPORT_VERSION,
        )

    if change_type == "undetermined":
        reason = change.get("undetermined_reason") or ""
        prefix = reason.split(":", 1)[0].strip()
        shape = _UNDETERMINED_SHAPES.get(prefix)
        if shape is None:
            return _check(
                "citation_support",
                "failed",
                "The undetermined reason does not carry a known stable reason "
                "code.",
                reason_code=CITATION_UNDETERMINED_REASON_MISMATCH,
                version=CITATION_SUPPORT_VERSION,
            )
        previous_allowed, current_allowed = shape
        if (previous_texts and not previous_allowed) or (
            current_texts and not current_allowed
        ):
            return _check(
                "citation_support",
                "failed",
                f"Evidence shape contradicts the '{prefix}' reason (a side "
                "the reason declares unavailable carries evidence).",
                reason_code=CITATION_UNDETERMINED_REASON_MISMATCH,
                version=CITATION_SUPPORT_VERSION,
            )
        return _check(
            "citation_support",
            "passed",
            f"Undetermined reason '{prefix}' is coherent with the evidence "
            "shape and all references resolve.",
            reason_code=CITATION_SUMMARY_SUPPORTED,
            version=CITATION_SUPPORT_VERSION,
        )

    heading_match = _HEADING_CLAIM_RE.search(summary)
    if heading_match is None:
        return _check(
            "citation_support",
            "failed",
            "No risk-factor heading claim could be parsed from the summary, "
            "so its support cannot be verified.",
            reason_code=CITATION_HEADING_NOT_SUPPORTED,
            version=CITATION_SUPPORT_VERSION,
        )
    heading = heading_match.group(1)

    required_sides = {
        "modified": (("previous", previous_texts), ("current", current_texts)),
        "added": (("current", current_texts),),
        "removed": (("previous", previous_texts),),
    }[change_type]
    missing = [
        side for side, texts in required_sides if not _heading_in(texts, heading)
    ]
    if missing:
        return _check(
            "citation_support",
            "failed",
            f"Heading '{heading}' is not present in the resolved "
            f"{' and '.join(missing)} evidence.",
            reason_code=CITATION_HEADING_NOT_SUPPORTED,
            version=CITATION_SUPPORT_VERSION,
        )
    return _check(
        "citation_support",
        "passed",
        f"Heading '{heading}' is supported by the resolved evidence on every "
        "required side.",
        reason_code=CITATION_SUMMARY_SUPPORTED,
        version=CITATION_SUPPORT_VERSION,
    )


# --- numeric_consistency ------------------------------------------------------


def numeric_consistency(
    change: dict[str, Any],
    *,
    resolve: Callable[[str], dict[str, Any] | None],
    previous_filing_id: str,
    current_filing_id: str,
) -> dict[str, Any]:
    """Every numeric token in the summary must be supported by the required
    evidence side; no numeric claim means not_applicable, never passed."""
    summary = change.get("summary") or ""
    claims = _summary_numbers(summary)
    if not claims:
        return _check(
            "numeric_consistency",
            "not_applicable",
            "The summary makes no numeric claim.",
            reason_code=NUMERIC_NO_CLAIM,
            version=NUMERIC_CONSISTENCY_VERSION,
        )

    change_type = change.get("change_type")
    previous_texts, prev_bad, prev_wrong = _resolve_side(
        change.get("previous_evidence") or [], previous_filing_id, resolve
    )
    current_texts, curr_bad, curr_wrong = _resolve_side(
        change.get("current_evidence") or [], current_filing_id, resolve
    )
    if prev_bad or curr_bad or prev_wrong or curr_wrong:
        return _check(
            "numeric_consistency",
            "failed",
            "Numeric claims cannot be verified because evidence references "
            "do not resolve cleanly.",
            reason_code=NUMERIC_VALUE_UNSUPPORTED,
            version=NUMERIC_CONSISTENCY_VERSION,
        )
    previous_numbers = set()
    for text in previous_texts:
        previous_numbers.update(extract_numbers(text))
    current_numbers = set()
    for text in current_texts:
        current_numbers.update(extract_numbers(text))

    # Side attribution: "from X to Y" pins X to the previous filing and Y to
    # the current one. Attributed claims are checked on their side; the rest
    # are checked per change type.
    attributed: dict[tuple[str, Decimal], str] = {}
    from_to = _FROM_TO_RE.search(summary)
    if from_to and change_type == "modified":
        from_parsed = parse_numeric_token(from_to.group("from"))
        to_parsed = parse_numeric_token(from_to.group("to"))
        if from_parsed:
            attributed[from_parsed] = "previous"
        if to_parsed:
            attributed[to_parsed] = "current"

    unsupported: list[str] = []
    for kind, value in claims:
        side = attributed.get((kind, value))
        if side == "previous":
            supported = (kind, value) in previous_numbers
        elif side == "current":
            supported = (kind, value) in current_numbers
        elif change_type == "added":
            supported = (kind, value) in current_numbers
        elif change_type == "removed":
            supported = (kind, value) in previous_numbers
        elif change_type == "modified":
            # An unattributed number on a two-sided change is ambiguous: it
            # must not pass merely because it appears somewhere.
            return _check(
                "numeric_consistency",
                "failed",
                f"Numeric claim {_display(kind, value)} has no deterministic "
                "side attribution on a modified change.",
                reason_code=NUMERIC_ATTRIBUTION_AMBIGUOUS,
                version=NUMERIC_CONSISTENCY_VERSION,
            )
        else:  # undetermined with a numeric claim: any available side
            supported = (kind, value) in (previous_numbers | current_numbers)
        if not supported:
            unsupported.append(_display(kind, value))

    if unsupported:
        return _check(
            "numeric_consistency",
            "failed",
            f"Numeric claim(s) {', '.join(unsupported)} are not supported by "
            "the required evidence side.",
            reason_code=NUMERIC_VALUE_UNSUPPORTED,
            version=NUMERIC_CONSISTENCY_VERSION,
        )
    return _check(
        "numeric_consistency",
        "passed",
        f"All {len(claims)} numeric claim(s) are supported by the required "
        "evidence side.",
        reason_code=NUMERIC_VALUES_SUPPORTED,
        version=NUMERIC_CONSISTENCY_VERSION,
    )


# --- direction_consistency ----------------------------------------------------


def direction_consistency(
    change: dict[str, Any],
    *,
    resolve: Callable[[str], dict[str, Any] | None],
    previous_filing_id: str,
    current_filing_id: str,
) -> dict[str, Any]:
    """Evaluate only explicit direction claims; never infer one."""
    summary = (change.get("summary") or "").lower()
    change_type = change.get("change_type")
    words = [
        direction
        for word, direction in _DIRECTION_WORDS.items()
        if re.search(rf"\b{word}\b", summary)
    ]

    if change_type == "undetermined":
        if words:
            return _check(
                "direction_consistency",
                "failed",
                f"An undetermined change must not claim a direction "
                f"('{words[0]}').",
                reason_code=DIRECTION_INVERTED,
                version=DIRECTION_CONSISTENCY_VERSION,
            )
        return _check(
            "direction_consistency",
            "not_applicable",
            "Undetermined changes make no directional claim to evaluate.",
            reason_code=DIRECTION_UNDETERMINED,
            version=DIRECTION_CONSISTENCY_VERSION,
        )

    if change_type in ("added", "removed"):
        expected_marker = _ADDED_MARKER if change_type == "added" else _REMOVED_MARKER
        inverted_marker = _REMOVED_MARKER if change_type == "added" else _ADDED_MARKER
        has_expected = expected_marker in summary
        has_inverted = inverted_marker in summary
        evidence_ok = (
            bool(change.get("current_evidence")) and not change.get("previous_evidence")
            if change_type == "added"
            else bool(change.get("previous_evidence"))
            and not change.get("current_evidence")
        )
        if has_inverted or not has_expected or not evidence_ok:
            return _check(
                "direction_consistency",
                "failed",
                f"The summary's direction does not match a '{change_type}' "
                "change and its evidence sides.",
                reason_code=DIRECTION_INVERTED,
                version=DIRECTION_CONSISTENCY_VERSION,
            )
        return _check(
            "direction_consistency",
            "passed",
            f"The '{change_type}' direction matches the summary wording and "
            "the evidence-side invariant.",
            reason_code=DIRECTION_SUPPORTED,
            version=DIRECTION_CONSISTENCY_VERSION,
        )

    # modified
    if not words:
        return _check(
            "direction_consistency",
            "not_applicable",
            "The summary makes no explicit increase/decrease/unchanged claim.",
            reason_code=DIRECTION_NO_CLAIM,
            version=DIRECTION_CONSISTENCY_VERSION,
        )
    direction = words[0]
    if direction == "unchanged":
        return _check(
            "direction_consistency",
            "failed",
            "A modified change cannot claim 'unchanged'.",
            reason_code=DIRECTION_INVERTED,
            version=DIRECTION_CONSISTENCY_VERSION,
        )

    from_to = _FROM_TO_RE.search(change.get("summary") or "")
    from_parsed = parse_numeric_token(from_to.group("from")) if from_to else None
    to_parsed = parse_numeric_token(from_to.group("to")) if from_to else None
    if not from_parsed or not to_parsed or from_parsed[0] != to_parsed[0]:
        return _check(
            "direction_consistency",
            "failed",
            f"The '{direction}' claim carries no verifiable from/to numeric "
            "pair of one kind.",
            reason_code=DIRECTION_UNVERIFIABLE,
            version=DIRECTION_CONSISTENCY_VERSION,
        )

    # The numeric validator owns side-attributed membership; the direction
    # check verifies the ordering the direction word asserts.
    from_value, to_value = from_parsed[1], to_parsed[1]
    ordered = to_value > from_value if direction == "increase" else to_value < from_value
    if not ordered:
        return _check(
            "direction_consistency",
            "failed",
            f"The summary claims '{direction}' but the values move "
            f"{_display(from_parsed[0], from_value)} -> "
            f"{_display(to_parsed[0], to_value)}.",
            reason_code=DIRECTION_INVERTED,
            version=DIRECTION_CONSISTENCY_VERSION,
        )
    return _check(
        "direction_consistency",
        "passed",
        f"The '{direction}' claim matches the attributed values "
        f"{_display(from_parsed[0], from_value)} -> "
        f"{_display(to_parsed[0], to_value)}.",
        reason_code=DIRECTION_SUPPORTED,
        version=DIRECTION_CONSISTENCY_VERSION,
    )


def validate_change(
    change: dict[str, Any],
    *,
    resolve: Callable[[str], dict[str, Any] | None],
    previous_filing_id: str,
    current_filing_id: str,
) -> list[dict[str, Any]]:
    """The three deterministic checks for one change, in canonical order."""
    kwargs = {
        "resolve": resolve,
        "previous_filing_id": previous_filing_id,
        "current_filing_id": current_filing_id,
    }
    return [
        citation_support(change, **kwargs),
        numeric_consistency(change, **kwargs),
        direction_consistency(change, **kwargs),
    ]
