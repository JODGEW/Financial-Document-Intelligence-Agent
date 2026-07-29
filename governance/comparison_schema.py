"""Versioned structured schema for two-period filing comparisons (comparison.v1).

This module is a CONTRACT, not a workflow. It defines the machine-validated
output shape the future filing-comparison workflow (README roadmap, "Next: the
financial filing comparison workflow") will produce: two filings of the same
company in explicit chronological order, section-scoped structured changes with
evidence on both sides, deterministic validation results, and placeholder risk
and review state. Nothing in the runtime imports it yet — there is no
comparison execution path, persistence, detector, API route, review
integration, or UI. The next dependency is durable filing identity metadata
(a per-filing id, filing_date / period_end, and a previous/current
relationship), which today has no producer: ingest infers only ``year``, and
ingest's ``document_id`` is a deliberately year-stripped *family* id
(ingest._document_id), so successive filings of the same form share it — the
future filing registry must mint per-filing ids before a real workflow can
populate this schema.

Modeling choices, deliberately aligned with existing conventions:

- Pydantic v2 models (the repo's existing validated-contract surface, api.py),
  with ``extra="forbid"`` so producer typos fail loudly instead of persisting.
- Vocabularies are ``Literal`` aliases plus tuple constants — the repo uses
  Literal, not enum.Enum (loaders.registry.FormatFamily, api.py statuses).
- snake_case field names, matching the ingest/Chroma metadata and audit records
  the values come from. camelCase is the UI-boundary mapping api.py applies per
  route; a future comparison route would map field-by-field the same way.
- Decision and review vocabularies reuse the existing governance words:
  ``returned`` / ``returned_with_warning`` / ``held_for_review`` / ``blocked``
  (governance_report._decision) plus ``not_evaluated``; review states are the
  queue's ``pending`` / ``approved`` / ``rejected`` plus ``not_required``.

Added/removed semantics (read before writing a detector)
--------------------------------------------------------
``added`` means the item is present in the current filing AND the previous
filing's section was successfully retrieved and parsed without it. ``removed``
is the mirror image. Neither may be emitted because retrieval or parsing failed
on the other side — a missing, unparsed, or unavailable section yields
``undetermined`` with an explicit ``undetermined_reason``. The evidence-side
invariants enforce the representable half of that boundary (added carries only
current-side evidence, removed only previous-side); the retrieval-success half
is a detector obligation this schema records but cannot itself verify.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Iterable, Literal, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

COMPARISON_SCHEMA_VERSION = "comparison.v1"

# Canonical section key for the first targeted workflow (Item 1A Risk Factors).
# Section keys are open strings, not an enum: sections are document structure,
# and the alignment step will mint keys as it learns to parse more of them.
SECTION_ITEM_1A = "item_1a_risk_factors"

# Identifiers, excerpts, and display strings must be non-empty after stripping.
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

ChangeType = Literal["added", "removed", "modified", "undetermined"]
CHANGE_TYPES: tuple[str, ...] = get_args(ChangeType)

# Deliberately small: the first workflow targets Item 1A Risk Factors. New
# categories are added here (and only here) when a workflow actually emits them.
ChangeCategory = Literal["risk_factor"]
CHANGE_CATEGORIES: tuple[str, ...] = get_args(ChangeCategory)

# The deterministic checks the comparison workflow will run per change. The
# schema represents them before the validators exist; an unimplemented check is
# recorded as status="not_run", never silently omitted or defaulted to passed.
ValidationCheckName = Literal[
    "evidence_presence",
    "citation_support",
    "numeric_consistency",
    "entity_consistency",
    "period_consistency",
    "direction_consistency",
]
VALIDATION_CHECK_NAMES: tuple[str, ...] = get_args(ValidationCheckName)

# Four-state on purpose: a boolean would conflate "failed" with "we never ran
# it" and "it does not apply to this change type".
ValidationStatus = Literal["passed", "failed", "not_run", "not_applicable"]
VALIDATION_STATUSES: tuple[str, ...] = get_args(ValidationStatus)

# governance_report._decision vocabulary plus "not_evaluated": a comparison
# document can exist before any risk pass has run over it, and must say so
# rather than carry a fabricated score.
ComparisonDecision = Literal[
    "not_evaluated",
    "returned",
    "returned_with_warning",
    "held_for_review",
    "blocked",
]
COMPARISON_DECISIONS: tuple[str, ...] = get_args(ComparisonDecision)

RiskLevel = Literal["low", "medium", "high"]

# review_queue statuses plus "not_required" (no review routing has happened).
ReviewStatus = Literal["not_required", "pending", "approved", "rejected"]
REVIEW_STATUSES: tuple[str, ...] = get_args(ReviewStatus)


class _ComparisonModel(BaseModel):
    """Base config for every comparison.v1 model: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class FilingReference(_ComparisonModel):
    """One filing in the comparison pair.

    Identity fields (``document_id``, ``company_key``, ``form_type``,
    ``period_end``) drive the cross-filing invariants; ``company_name`` and
    ``source_name`` are display/provenance metadata and are never compared.
    """

    document_id: NonEmptyStr = Field(
        description=(
            "Unique id of THIS filing. No producer exists yet: ingest's "
            "document_id is a year-stripped family id shared by successive "
            "filings of one form (ingest._document_id), so it cannot "
            "distinguish the two sides of a comparison. The future filing "
            "registry mints per-filing ids."
        )
    )
    company_key: NonEmptyStr = Field(
        description=(
            "Normalized company identity key, as produced by "
            "ingest.company_metadata_key. Both filings in a comparison must "
            "share it."
        )
    )
    company_name: NonEmptyStr = Field(
        description="Display company name (ingest 'company' metadata)."
    )
    form_type: NonEmptyStr = Field(
        description=(
            "Filing form type, e.g. '10-K' (ingest 'filing_type' metadata). "
            "v1 comparisons require the same form type on both sides."
        )
    )
    filing_date: date | None = Field(
        default=None,
        description=(
            "Date the filing was filed. No producer exists yet (ingest infers "
            "only a year); populated by the future filing registry. Must come "
            "from durable filing metadata — never derived from a filename."
        ),
    )
    period_end: date = Field(
        description=(
            "End of the reporting period the filing covers. The chronological "
            "key of the comparison: previous.period_end < current.period_end. "
            "Required even though no producer exists yet — a comparison "
            "without explicit period ordering is not representable. Must come "
            "from durable filing metadata — never derived from a filename."
        )
    )
    source_name: NonEmptyStr = Field(
        description="Corpus file name the filing was ingested from."
    )
    version_hash: str | None = Field(
        default=None,
        description=(
            "Content-version hash of the ingested document where available "
            "(ingest 'document_version', the 12-char sha256 prefix)."
        ),
    )


class EvidenceReference(_ComparisonModel):
    """A resolvable pointer to one chunk of one filing.

    Evidence is never a bare string: every reference must resolve back to
    exactly one filing (``document_id``) and one chunk (``chunk_id``), with the
    excerpt carried for display and audit. The containing ComparisonResult
    enforces that previous-side evidence references the previous filing and
    current-side evidence the current filing.
    """

    document_id: NonEmptyStr = Field(
        description=(
            "document_id of the filing this evidence belongs to. Must equal "
            "the containing side's FilingReference.document_id (enforced on "
            "ComparisonResult)."
        )
    )
    chunk_id: NonEmptyStr = Field(
        description=(
            "Chunk identity. Either the ingest form "
            "source_name:page:sha1(content)[:12] (ingest.chunk_id) or the "
            "audit-reproducible form built from the normalized excerpt "
            "(governance.impact._chunk_id). The format is documented, not "
            "validated, because both producers are legitimate."
        )
    )
    source_name: NonEmptyStr = Field(
        description="Corpus file name the chunk came from."
    )
    page: int | None = Field(
        default=None,
        ge=0,
        description="Page number where the format has one.",
    )
    section_key: NonEmptyStr = Field(
        description=(
            "Canonical section key the chunk was aligned to, e.g. "
            "'item_1a_risk_factors' (SECTION_ITEM_1A)."
        )
    )
    section_title: str | None = Field(
        default=None,
        description="Human-readable section title from chunk metadata.",
    )
    excerpt: NonEmptyStr = Field(
        description="Verbatim excerpt supporting the change. Never empty."
    )
    content_hash: str | None = Field(
        default=None,
        description=(
            "Chunk content hash where available (12-char sha1 prefix, as in "
            "ingest.chunk_id / impact._content_hash)."
        ),
    )


class ValidationCheck(_ComparisonModel):
    """One deterministic check result attached to one change.

    Explicit ``not_run`` / ``not_applicable`` states exist so absence of a
    validator is never conflated with success or failure.
    """

    check: ValidationCheckName
    status: ValidationStatus
    reason_code: str | None = Field(
        default=None,
        description=(
            "Machine-readable reason. Required when status='failed'; optional "
            "context otherwise (e.g. why a check was not applicable)."
        ),
    )
    detail: NonEmptyStr = Field(
        description=(
            "Human-readable outcome, safe to display to a reviewer (no "
            "internal paths, no raw provider errors)."
        )
    )
    validator_version: str | None = Field(
        default=None,
        description=(
            "Version of the validator that produced this result. None while "
            "the check has no implementation (status='not_run')."
        ),
    )

    @model_validator(mode="after")
    def _failed_requires_reason_code(self) -> "ValidationCheck":
        if self.status == "failed" and not (self.reason_code or "").strip():
            raise ValueError(
                f"validation check '{self.check}': status='failed' requires a "
                "non-empty reason_code"
            )
        return self


def _duplicate_evidence(refs: Iterable[EvidenceReference]) -> str | None:
    """Return the first duplicated (document_id, chunk_id) key, or None."""
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        key = (ref.document_id, ref.chunk_id)
        if key in seen:
            return f"{key[0]}:{key[1]}"
        seen.add(key)
    return None


class FilingChange(_ComparisonModel):
    """One section-scoped structured change between the two filings.

    Evidence-side semantics by change_type (enforced below):

    - ``modified``: evidence required on BOTH sides.
    - ``added``: present in current, absent from a successfully parsed
      previous section. Current-side evidence required; previous-side evidence
      must be empty. NOT a statement that previous-side retrieval failed.
    - ``removed``: the mirror image — previous-side evidence required,
      current-side must be empty. NOT a statement that current-side retrieval
      failed.
    - ``undetermined``: the section was missing, unparsed, or the evidence was
      insufficient on at least one side. Requires an explicit
      ``undetermined_reason``; partial evidence on either side is allowed.
      Detectors must emit this instead of added/removed whenever they cannot
      establish the other side actually parsed.

    Deliberately absent fields: ``materiality``/``priority`` and
    ``confidence``. No detector or materiality rubric exists whose semantics
    they could honestly describe; they are added only alongside the workflow
    signal that produces them.
    """

    change_id: NonEmptyStr = Field(
        description="Identifier unique within one ComparisonResult."
    )
    change_type: ChangeType = Field(
        description=(
            "added = present in current, absent from a successfully parsed "
            "previous section; removed = the mirror image; modified = present "
            "in both with substantive differences; undetermined = evidence or "
            "parsing insufficient to make any of those claims. added/removed "
            "must never encode a retrieval failure — that is undetermined."
        )
    )
    category: ChangeCategory
    section_key: NonEmptyStr = Field(
        description=(
            "Canonical section key this change is scoped to. Must be listed "
            "in the ComparisonResult's section_scope."
        )
    )
    summary: NonEmptyStr = Field(
        description="One-paragraph reviewer-facing description of the change."
    )
    previous_evidence: list[EvidenceReference] = Field(
        default_factory=list,
        description="Evidence from the previous filing supporting this change.",
    )
    current_evidence: list[EvidenceReference] = Field(
        default_factory=list,
        description="Evidence from the current filing supporting this change.",
    )
    validation: list[ValidationCheck] = Field(
        default_factory=list,
        description="Deterministic check results for this change.",
    )
    undetermined_reason: str | None = Field(
        default=None,
        description=(
            "Why the change could not be determined (required when "
            "change_type='undetermined', forbidden otherwise), e.g. "
            "'previous filing Item 1A could not be parsed'."
        ),
    )

    @model_validator(mode="after")
    def _enforce_change_semantics(self) -> "FilingChange":
        label = f"change '{self.change_id}'"

        if self.change_type == "modified":
            if not self.previous_evidence or not self.current_evidence:
                raise ValueError(
                    f"{label}: change_type='modified' requires evidence on "
                    "both sides (previous_evidence and current_evidence)"
                )
        elif self.change_type == "added":
            if not self.current_evidence:
                raise ValueError(
                    f"{label}: change_type='added' requires current_evidence"
                )
            if self.previous_evidence:
                raise ValueError(
                    f"{label}: change_type='added' must not carry "
                    "previous_evidence — absence from the previous filing is "
                    "asserted by the detector, not evidenced by a chunk; if "
                    "the previous side did not parse, emit 'undetermined'"
                )
        elif self.change_type == "removed":
            if not self.previous_evidence:
                raise ValueError(
                    f"{label}: change_type='removed' requires previous_evidence"
                )
            if self.current_evidence:
                raise ValueError(
                    f"{label}: change_type='removed' must not carry "
                    "current_evidence — absence from the current filing is "
                    "asserted by the detector, not evidenced by a chunk; if "
                    "the current side did not parse, emit 'undetermined'"
                )

        if self.change_type == "undetermined":
            if not (self.undetermined_reason or "").strip():
                raise ValueError(
                    f"{label}: change_type='undetermined' requires a "
                    "non-empty undetermined_reason"
                )
        elif self.undetermined_reason is not None:
            raise ValueError(
                f"{label}: undetermined_reason is only valid when "
                "change_type='undetermined'"
            )

        for side, refs in (
            ("previous_evidence", self.previous_evidence),
            ("current_evidence", self.current_evidence),
        ):
            duplicate = _duplicate_evidence(refs)
            if duplicate is not None:
                raise ValueError(
                    f"{label}: duplicate evidence reference in {side}: "
                    f"{duplicate}"
                )

        seen_checks: set[str] = set()
        for check in self.validation:
            if check.check in seen_checks:
                raise ValueError(
                    f"{label}: duplicate validation check '{check.check}'"
                )
            seen_checks.add(check.check)

        return self


class ValidationSummary(_ComparisonModel):
    """Roll-up of every ValidationCheck across every change.

    The ComparisonResult validator recomputes the tally from the changes and
    rejects a summary that disagrees, so this block is derived data that cannot
    drift from the per-change results. Produce it with summarize_validation().
    """

    total_checks: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    not_run: int = Field(ge=0)
    not_applicable: int = Field(ge=0)

    @model_validator(mode="after")
    def _totals_add_up(self) -> "ValidationSummary":
        parts = self.passed + self.failed + self.not_run + self.not_applicable
        if parts != self.total_checks:
            raise ValueError(
                "validation_summary: total_checks "
                f"({self.total_checks}) != passed+failed+not_run+"
                f"not_applicable ({parts})"
            )
        return self


def summarize_validation(changes: Iterable[FilingChange]) -> ValidationSummary:
    """Compute the ValidationSummary a set of changes requires."""
    counts = {status: 0 for status in VALIDATION_STATUSES}
    for change in changes:
        for check in change.validation:
            counts[check.status] += 1
    return ValidationSummary(
        total_checks=sum(counts.values()),
        passed=counts["passed"],
        failed=counts["failed"],
        not_run=counts["not_run"],
        not_applicable=counts["not_applicable"],
    )


class RiskDecision(_ComparisonModel):
    """Placeholder for the future comparison risk output.

    Defaults to decision='not_evaluated', which asserts nothing has been
    computed: score, level, and reason codes must all be absent. Once a risk
    pass exists it emits one of the existing governance decisions (reusing the
    governance_report vocabulary) with its score, level, and reason codes.
    """

    decision: ComparisonDecision = "not_evaluated"
    reason_codes: list[NonEmptyStr] = Field(
        default_factory=list,
        description=(
            "Machine-readable reason codes (e.g. the risk_scorer codes such "
            "as 'risk_score_at_or_above_review_threshold'), kept separate "
            "from the numeric score."
        ),
    )
    risk_score: float | None = Field(default=None, ge=0.0, le=1.0)
    risk_level: RiskLevel | None = None

    @model_validator(mode="after")
    def _placeholder_coherence(self) -> "RiskDecision":
        if self.decision == "not_evaluated":
            if (
                self.risk_score is not None
                or self.risk_level is not None
                or self.reason_codes
            ):
                raise ValueError(
                    "risk: decision='not_evaluated' must not carry "
                    "risk_score, risk_level, or reason_codes — a placeholder "
                    "asserts that nothing was computed"
                )
        else:
            if self.risk_score is None or self.risk_level is None:
                raise ValueError(
                    f"risk: decision='{self.decision}' requires both "
                    "risk_score and risk_level"
                )
        return self


class ReviewState(_ComparisonModel):
    """Placeholder for the future comparison review routing.

    'not_required' means no review routing has happened (or none was needed).
    No reviewer identity, assignment, SLA, or revision history in v1 — those
    belong to the Stage-3 review workflow.
    """

    status: ReviewStatus = "not_required"
    review_id: str | None = Field(
        default=None,
        description=(
            "Review queue key once routed (the queue keys items "
            "'review_<auditId>'); None until review routing exists."
        ),
    )

    @model_validator(mode="after")
    def _not_required_has_no_id(self) -> "ReviewState":
        if self.status == "not_required" and self.review_id is not None:
            raise ValueError(
                "review: status='not_required' must not carry a review_id"
            )
        return self


class ComparisonResult(_ComparisonModel):
    """The comparison.v1 document: one governed two-filing comparison."""

    schema_version: Literal["comparison.v1"] = Field(
        default=COMPARISON_SCHEMA_VERSION,
        description="Contract version. Unknown versions are rejected.",
    )
    comparison_id: NonEmptyStr
    previous_filing: FilingReference
    current_filing: FilingReference
    section_scope: list[NonEmptyStr] = Field(
        min_length=1,
        description=(
            "Canonical section keys this comparison covered. Every change's "
            "section_key must appear here. v1 targets "
            "['item_1a_risk_factors']."
        ),
    )
    changes: list[FilingChange] = Field(
        default_factory=list,
        description="May be empty: a comparison that found no changes is valid.",
    )
    validation_summary: ValidationSummary
    risk: RiskDecision = Field(default_factory=RiskDecision)
    review: ReviewState = Field(default_factory=ReviewState)
    created_at: AwareDatetime = Field(
        description="Timezone-aware creation time. Naive datetimes are rejected."
    )
    producer: NonEmptyStr = Field(
        description=(
            "Identifier and version of what produced this document, e.g. "
            "'comparison_workflow.v1'. Golden fixtures use 'fixture.v1'."
        )
    )

    @model_validator(mode="after")
    def _enforce_cross_filing_invariants(self) -> "ComparisonResult":
        prev, curr = self.previous_filing, self.current_filing

        if prev.document_id == curr.document_id:
            raise ValueError(
                "previous_filing.document_id and current_filing.document_id "
                f"must differ; both are '{prev.document_id}' — a comparison "
                "requires two distinct filings"
            )
        if prev.company_key != curr.company_key:
            raise ValueError(
                "previous_filing.company_key "
                f"('{prev.company_key}') != current_filing.company_key "
                f"('{curr.company_key}') — v1 compares filings of one company"
            )
        if prev.form_type != curr.form_type:
            raise ValueError(
                f"previous_filing.form_type ('{prev.form_type}') != "
                f"current_filing.form_type ('{curr.form_type}') — v1 compares "
                "like-for-like filings"
            )
        if prev.period_end >= curr.period_end:
            raise ValueError(
                f"previous_filing.period_end ({prev.period_end.isoformat()}) "
                "must be strictly before current_filing.period_end "
                f"({curr.period_end.isoformat()})"
            )

        scope = set(self.section_scope)
        if len(scope) != len(self.section_scope):
            raise ValueError("section_scope entries must be unique")

        seen_change_ids: set[str] = set()
        for change in self.changes:
            if change.change_id in seen_change_ids:
                raise ValueError(
                    f"duplicate change_id '{change.change_id}' — change_id "
                    "values must be unique within a comparison"
                )
            seen_change_ids.add(change.change_id)

            if change.section_key not in scope:
                raise ValueError(
                    f"change '{change.change_id}': section_key "
                    f"'{change.section_key}' is not in section_scope"
                )

            for ref in change.previous_evidence:
                if ref.document_id != prev.document_id:
                    raise ValueError(
                        f"change '{change.change_id}': previous_evidence "
                        f"chunk '{ref.chunk_id}' references document "
                        f"'{ref.document_id}', not previous_filing.document_id "
                        f"'{prev.document_id}'"
                    )
            for ref in change.current_evidence:
                if ref.document_id != curr.document_id:
                    raise ValueError(
                        f"change '{change.change_id}': current_evidence "
                        f"chunk '{ref.chunk_id}' references document "
                        f"'{ref.document_id}', not current_filing.document_id "
                        f"'{curr.document_id}'"
                    )

        expected = summarize_validation(self.changes)
        if expected != self.validation_summary:
            raise ValueError(
                "validation_summary does not match the checks on changes: "
                f"expected {expected.model_dump()}, "
                f"got {self.validation_summary.model_dump()}"
            )

        # Placeholder coherence between the risk and review blocks.
        if self.risk.decision == "not_evaluated":
            if self.review.status != "not_required":
                raise ValueError(
                    "review.status must be 'not_required' while "
                    "risk.decision='not_evaluated' — review routing cannot "
                    "precede risk evaluation"
                )
        elif self.risk.decision == "held_for_review":
            if self.review.status == "not_required":
                raise ValueError(
                    "risk.decision='held_for_review' requires a review state "
                    "(pending/approved/rejected), got 'not_required'"
                )

        return self


def dump_comparison(result: ComparisonResult) -> dict[str, Any]:
    """Serialize to JSON-compatible data (dates as ISO strings, stable order)."""
    return result.model_dump(mode="json")


def load_comparison(data: str | bytes | dict[str, Any]) -> ComparisonResult:
    """Parse and fully validate a comparison.v1 document from JSON or a dict."""
    if isinstance(data, (str, bytes)):
        return ComparisonResult.model_validate_json(data)
    return ComparisonResult.model_validate(data)
