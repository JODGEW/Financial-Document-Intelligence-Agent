"""Versioned structured contract for released comparison results (comparison.export.v1).

This module is a CONTRACT, not a workflow — the same split as
comparison_schema.py (contract) vs comparison_detector/governance/review
(workflows). It defines the machine-validated envelope the comparison-export
workflow (comparison_export.py) persists for a RELEASE-ELIGIBLE governance
evaluation: the immutable snapshot that governance or review already produced,
wrapped with enough identity (comparison, evaluation, policy, hashes) to trace
it back through the workflow without re-reading the database.

Release-gated, not approved-only
--------------------------------
The export exists for exactly three workflow outcomes:

- ``returned_by_policy`` — policy decided no review was required; the governed
  snapshot is released directly. There is no review item and no fake approval.
- ``returned_with_warning_by_policy`` — same, with the warning decision and
  its reason codes preserved in the snapshot's ``risk`` block.
- ``approved_after_review`` — a held evaluation whose single terminal review
  event approved it; the FINAL REVIEWED snapshot from that event is released,
  together with an allowlisted decision block.

Pending, rejected, and blocked outcomes are not representable here — the
workflow refuses them before an envelope is ever built.

Reviewer identity, stated plainly: ``reviewer_id`` is SELF-ASSERTED LOCAL
METADATA (an email-like string, a local username, a test id), never
authenticated identity — and the envelope says so itself via the fixed
``reviewer_id_basis`` field, because an export artifact travels without its
source code. Export rows are deterministic application records, NOT signed,
tamper-proof, or immutable storage.

The ``comparison_result`` is the selected snapshot VERBATIM — building the
envelope never constructs a new summary or modifies the snapshot; the model
validator re-validates it against comparison.v1 and pins the cross-field
coherence (hashes, release basis vs the snapshot's own risk/review state).
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from governance.comparison_schema import load_comparison

EXPORT_SCHEMA_VERSION = "comparison.export.v1"

# Narrow release vocabulary: one value per eligible workflow outcome.
ReleaseBasis = Literal[
    "returned_by_policy",
    "returned_with_warning_by_policy",
    "approved_after_review",
]
RELEASE_BASES: tuple[str, ...] = get_args(ReleaseBasis)

RELEASE_RETURNED = "returned_by_policy"
RELEASE_WARNING = "returned_with_warning_by_policy"
RELEASE_APPROVED = "approved_after_review"

# Which governance decision each release basis is allowed to wrap.
_BASIS_TO_DECISION = {
    RELEASE_RETURNED: "returned",
    RELEASE_WARNING: "returned_with_warning",
    RELEASE_APPROVED: "held_for_review",
}


class _ExportModel(BaseModel):
    """Base config for every export model: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class ExportReviewDecision(_ExportModel):
    """Allowlisted terminal review decision, present ONLY for
    ``approved_after_review`` exports.

    Deliberately excludes request hashes, SQLite row identifiers unrelated to
    the workflow, database paths, and internal exception text. The reviewer
    note is included: it is bounded, control-character-free reviewer prose the
    review workflow already validated, and it is part of why the result was
    released.
    """

    review_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    action: Literal["approved"]
    reviewer_id: str = Field(
        min_length=1,
        description=(
            "Self-asserted local reviewer identifier exactly as recorded on "
            "the decision event. NOT authenticated identity."
        ),
    )
    reviewer_id_basis: Literal["self_asserted_local_metadata"] = Field(
        default="self_asserted_local_metadata",
        description=(
            "Fixed label so the artifact itself states that reviewer_id is "
            "self-asserted local metadata, not authenticated identity."
        ),
    )
    reason_code: str = Field(min_length=1)
    reviewer_note: str = Field(min_length=1)
    decided_at: AwareDatetime
    original_governed_result_hash: str = Field(min_length=1)
    final_reviewed_result_hash: str = Field(min_length=1)
    edited_change_ids: list[str] = Field(
        default_factory=list,
        description="change_id values whose summaries the approval edited.",
    )


class ComparisonExport(_ExportModel):
    """The comparison.export.v1 document: one released comparison result."""

    export_schema_version: Literal["comparison.export.v1"] = Field(
        default=EXPORT_SCHEMA_VERSION,
        description="Contract version. Unknown versions are rejected.",
    )
    export_id: str = Field(min_length=1)
    comparison_id: str = Field(min_length=1)
    evaluation_id: str = Field(min_length=1)
    review_id: str | None = Field(
        default=None,
        description=(
            "The approved review item for approved_after_review exports; "
            "None for policy-returned exports (no review item exists)."
        ),
    )
    release_basis: ReleaseBasis
    policy_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    detector_result_hash: str = Field(
        min_length=1,
        description="Hash of the stored detector result the evaluation governed.",
    )
    governed_result_hash: str = Field(
        min_length=1,
        description="Hash of the evaluation's governed comparison.v1 snapshot.",
    )
    final_result_hash: str = Field(
        min_length=1,
        description=(
            "Hash of the snapshot actually exported: the governed hash for "
            "policy-returned exports, the final reviewed hash for "
            "approved_after_review."
        ),
    )
    exported_at: AwareDatetime
    comparison_result: dict[str, Any] = Field(
        description=(
            "The released snapshot VERBATIM — a validated comparison.v1 wire "
            "document, never rewritten or summarized by the export."
        )
    )
    review_decision: ExportReviewDecision | None = None

    @model_validator(mode="after")
    def _enforce_release_coherence(self) -> "ComparisonExport":
        # The snapshot must itself be a valid comparison.v1 document that
        # belongs to this comparison.
        try:
            snapshot = load_comparison(self.comparison_result)
        except Exception as exc:  # re-raise as a field-shaped error
            raise ValueError(
                f"comparison_result is not a valid comparison.v1 document: {exc}"
            ) from exc
        if snapshot.comparison_id != self.comparison_id:
            raise ValueError(
                "comparison_result.comparison_id "
                f"('{snapshot.comparison_id}') != export comparison_id "
                f"('{self.comparison_id}')"
            )

        expected_decision = _BASIS_TO_DECISION[self.release_basis]
        if snapshot.risk.decision != expected_decision:
            raise ValueError(
                f"release_basis='{self.release_basis}' requires the snapshot's "
                f"risk.decision to be '{expected_decision}', got "
                f"'{snapshot.risk.decision}'"
            )

        if self.release_basis == RELEASE_APPROVED:
            if self.review_decision is None or self.review_id is None:
                raise ValueError(
                    "release_basis='approved_after_review' requires both "
                    "review_id and the review_decision block"
                )
            decision = self.review_decision
            if decision.review_id != self.review_id:
                raise ValueError(
                    "review_decision.review_id must equal the export's review_id"
                )
            if decision.original_governed_result_hash != self.governed_result_hash:
                raise ValueError(
                    "review_decision.original_governed_result_hash must equal "
                    "the export's governed_result_hash"
                )
            if decision.final_reviewed_result_hash != self.final_result_hash:
                raise ValueError(
                    "review_decision.final_reviewed_result_hash must equal "
                    "the export's final_result_hash"
                )
            if snapshot.review.status != "approved":
                raise ValueError(
                    "approved_after_review requires the snapshot's "
                    f"review.status to be 'approved', got '{snapshot.review.status}'"
                )
        else:
            if self.review_decision is not None or self.review_id is not None:
                raise ValueError(
                    f"release_basis='{self.release_basis}' must not carry a "
                    "review_id or review_decision — policy decided no review "
                    "was required"
                )
            if self.final_result_hash != self.governed_result_hash:
                raise ValueError(
                    "policy-returned exports release the governed snapshot: "
                    "final_result_hash must equal governed_result_hash"
                )
            if snapshot.review.status != "not_required":
                raise ValueError(
                    "policy-returned exports require the snapshot's "
                    "review.status to be 'not_required', got "
                    f"'{snapshot.review.status}'"
                )
        return self


def dump_export(export: ComparisonExport) -> dict[str, Any]:
    """Serialize to JSON-compatible data (dates as ISO strings, stable order)."""
    return export.model_dump(mode="json")


def load_export(data: str | bytes | dict[str, Any]) -> ComparisonExport:
    """Parse and fully validate a comparison.export.v1 document."""
    if isinstance(data, (str, bytes)):
        return ComparisonExport.model_validate_json(data)
    return ComparisonExport.model_validate(data)
