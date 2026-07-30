"""Attributable comparison-review decisions (roadmap step 9).

One pending SQLite comparison review item receives exactly one terminal
decision — approved or rejected — recorded as an append-only event carrying a
required reviewer identifier, a required allowlisted reason code, a required
bounded note, both result hashes, any permitted edits, and the complete final
reviewed comparison.v1 snapshot.

Reviewer identity has two explicit trust levels. Direct library callers retain
``legacy_self_asserted`` metadata for backward compatibility. The protected
API supplies a verified Principal subject plus narrow ``local_hs256``
token/policy provenance.

Editing is deliberately narrow: only ``FilingChange.summary`` may change,
addressed by change_id. Evidence, change types, categories, section keys,
filing references, validation statuses, risk, policy identity, and comparison
identity are immutable; no change may be added or removed. Every edited
summary is re-validated through the SAME deterministic content validators the
detector used (citation_support / numeric_consistency / direction_consistency)
against the change's ORIGINAL evidence resolved to full indexed chunk text —
an edit that introduces an unsupported heading, number, or direction claim is
rejected with the failing reason codes. Chunk ids are content-addressed, so
if the index no longer holds the original chunks the resolution fails and the
edit is refused rather than validated against different text.

Rejection preserves the governed snapshot verbatim (all changes, summaries,
and the risk decision unchanged) with only review.status = rejected — it is a
human decision about the output, not a blocked comparison, and nothing is
deleted.

The detector result and the governance evaluation (with its governed
snapshot) are never modified; the final reviewed snapshot lives on the
decision event. Events are append-only application records — NOT tamper-proof
audit storage.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

import comparison_reliability
import comparison_store
import comparison_validators
import config
from governance.comparison_schema import (
    dump_comparison,
    load_comparison,
    summarize_validation,
    FilingChange,
)

ACTION_APPROVED = "approved"
ACTION_REJECTED = "rejected"

APPROVE_REASON_CODES = (
    "approved_as_is",
    "approved_with_summary_edits",
    "approved_after_evidence_review",
)
REJECT_REASON_CODES = (
    "rejected_unsupported_summary",
    "rejected_incomplete_evidence",
    "rejected_wrong_entity_or_period",
    "rejected_ambiguous_change",
    "rejected_other",
)

MAX_REVIEWER_ID_CHARS = 120
MAX_NOTE_CHARS = 500
MAX_SUMMARY_CHARS = 1000

# The three summary-dependent checks that get re-run for edited changes. The
# structural three (evidence_presence / entity / period) are pure functions of
# the untouched evidence lists and keep their stored values.
_CONTENT_CHECKS = (
    "citation_support",
    "numeric_consistency",
    "direction_consistency",
)


class ReviewDecisionError(Exception):
    """Invalid decision input (API: 422). ``code`` is stable; the message is
    safe for display — no evidence text, paths, SQL, or reviewer prose."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ReviewNotFound(Exception):
    """Unknown review item (API: 404)."""


def _clean_bounded(
    value: Any, *, field: str, code: str, max_chars: int
) -> str:
    """Strip surrounding whitespace, require non-empty, bound length, and
    reject control characters (they are unsafe for plain display)."""
    if not isinstance(value, str) or not value.strip():
        raise ReviewDecisionError(code, f"{field} is required and must be a non-empty string")
    cleaned = value.strip()
    if len(cleaned) > max_chars:
        raise ReviewDecisionError(
            code, f"{field} must be at most {max_chars} characters"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
        raise ReviewDecisionError(
            code, f"{field} must not contain control characters"
        )
    return cleaned


def _validate_reason(action: str, reason_code: str, has_edits: bool) -> None:
    if action == ACTION_APPROVED:
        if reason_code not in APPROVE_REASON_CODES:
            raise ReviewDecisionError(
                "invalid_reason_code",
                f"approve requires one of {list(APPROVE_REASON_CODES)}",
            )
        # One clean invariant: edits present <=> approved_with_summary_edits.
        if has_edits and reason_code != "approved_with_summary_edits":
            raise ReviewDecisionError(
                "edits_require_summary_edit_reason",
                "decisions with edits must use reason "
                "'approved_with_summary_edits'",
            )
        if not has_edits and reason_code == "approved_with_summary_edits":
            raise ReviewDecisionError(
                "summary_edit_reason_requires_edits",
                "'approved_with_summary_edits' requires at least one edit",
            )
    elif action == ACTION_REJECTED:
        if reason_code not in REJECT_REASON_CODES:
            raise ReviewDecisionError(
                "invalid_reason_code",
                f"reject requires one of {list(REJECT_REASON_CODES)}",
            )
        if has_edits:
            raise ReviewDecisionError(
                "reject_does_not_accept_edits",
                "rejected decisions must not carry edits",
            )
    else:
        raise ReviewDecisionError(
            "invalid_action", "action must be 'approved' or 'rejected'"
        )


def _normalize_edits(
    edits: list[dict[str, Any]] | None, governed: dict[str, Any]
) -> list[dict[str, str]]:
    """Validate the edit list against the governed result's changes.

    Every change_id must address exactly one existing change; duplicates,
    unknown ids, empty/unchanged/oversized summaries are rejected. Returns
    [{change_id, original_summary, new_summary}] in change order.
    """
    if not edits:
        return []
    changes_by_id = {
        change["change_id"]: change for change in governed.get("changes") or []
    }
    seen: set[str] = set()
    normalized: dict[str, dict[str, str]] = {}
    for entry in edits:
        if not isinstance(entry, dict):
            raise ReviewDecisionError("invalid_edit", "each edit must be an object")
        change_id = entry.get("change_id")
        if not isinstance(change_id, str) or change_id not in changes_by_id:
            raise ReviewDecisionError(
                "unknown_change_id",
                "an edit addresses a change_id that does not exist in the "
                "governed result",
            )
        if change_id in seen:
            raise ReviewDecisionError(
                "duplicate_edit_change_id",
                "each change_id may be edited at most once",
            )
        seen.add(change_id)
        summary = _clean_bounded(
            entry.get("summary"),
            field="edit summary",
            code="invalid_edit_summary",
            max_chars=MAX_SUMMARY_CHARS,
        )
        if summary == changes_by_id[change_id]["summary"]:
            raise ReviewDecisionError(
                "unchanged_edit_summary",
                "an edit must actually change the summary",
            )
        normalized[change_id] = {
            "change_id": change_id,
            "original_summary": changes_by_id[change_id]["summary"],
            "new_summary": summary,
        }
    # Deterministic order: follow the governed result's change order.
    return [
        normalized[change["change_id"]]
        for change in governed.get("changes") or []
        if change["change_id"] in normalized
    ]


def _default_resolver(chunk_ids: list[str]) -> Callable[[str], dict | None]:
    """Resolve chunk ids to full text + filing via the embedding-free index."""
    import comparison_detector

    client = comparison_detector.open_index()
    got = client.get(ids=chunk_ids, include=["documents", "metadatas"])
    index = {
        chunk_id: {
            "text": text or "",
            "filing_id": (metadata or {}).get("filing_id"),
        }
        for chunk_id, text, metadata in zip(
            got.get("ids") or [],
            got.get("documents") or [],
            got.get("metadatas") or [],
        )
    }
    return index.get


def _apply_edits(
    governed: dict[str, Any],
    normalized_edits: list[dict[str, str]],
    resolve: Callable[[str], dict | None] | None,
) -> dict[str, Any]:
    """Apply summary edits to a deep copy and re-validate the edited changes.

    Only the addressed summaries and their three content checks change;
    every other field of every change is preserved byte-for-byte. Any edited
    check that fails rejects the whole decision with its reason codes.
    """
    reviewed = json.loads(json.dumps(governed))  # deep copy
    edits_by_id = {edit["change_id"]: edit for edit in normalized_edits}
    previous_filing_id = governed["previous_filing"]["document_id"]
    current_filing_id = governed["current_filing"]["document_id"]

    if resolve is None:
        chunk_ids = sorted(
            {
                ref["chunk_id"]
                for change in reviewed["changes"]
                if change["change_id"] in edits_by_id
                for ref in change["previous_evidence"] + change["current_evidence"]
            }
        )
        resolve = _default_resolver(chunk_ids)

    for change in reviewed["changes"]:
        edit = edits_by_id.get(change["change_id"])
        if edit is None:
            continue
        change["summary"] = edit["new_summary"]
        rerun = comparison_validators.validate_change(
            change,
            resolve=resolve,
            previous_filing_id=previous_filing_id,
            current_filing_id=current_filing_id,
        )
        rerun_by_name = {check["check"]: check for check in rerun}
        failed = [
            check for check in rerun if check["status"] == "failed"
        ]
        if failed:
            codes = sorted({check["reason_code"] for check in failed if check["reason_code"]})
            raise ReviewDecisionError(
                "edit_validation_failed",
                "the edited summary is not supported by the change's original "
                f"evidence ({', '.join(codes)})",
            )
        change["validation"] = [
            rerun_by_name.get(check["check"], check)
            if check["check"] in _CONTENT_CHECKS
            else check
            for check in change["validation"]
        ]

    reviewed["validation_summary"] = summarize_validation(
        [FilingChange.model_validate(change) for change in reviewed["changes"]]
    ).model_dump()
    return reviewed


def _request_hash(
    action: str,
    reviewer_id: str,
    reason_code: str,
    reviewer_note: str,
    normalized_edits: list[dict[str, str]],
) -> str:
    canonical = json.dumps(
        {
            "action": action,
            "reviewer_id": reviewer_id,
            "reason_code": reason_code,
            "reviewer_note": reviewer_note,
            "edits": [
                {"change_id": e["change_id"], "summary": e["new_summary"]}
                for e in normalized_edits
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def decide(
    review_id: str,
    *,
    action: str,
    reviewer_id: str,
    reason_code: str,
    reviewer_note: str,
    edits: list[dict[str, Any]] | None = None,
    db_path: str | Path | None = None,
    resolve: Callable[[str], dict | None] | None = None,
    actor_context: Mapping[str, Any] | None = None,
    actor_policy_id: str | None = None,
    actor_policy_version: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Apply one terminal decision to a pending comparison review.

    Returns (event_record, created). Byte-equivalent replays return the
    stored event with created=False; a different request against a decided
    review raises comparison_store.ReviewAlreadyDecided (API: 409). Input
    problems raise ReviewDecisionError (API: 422).
    """
    db_path = db_path or config.COMPARISON_DB_PATH

    reviewer_id = _clean_bounded(
        reviewer_id, field="reviewerId", code="invalid_reviewer_id",
        max_chars=MAX_REVIEWER_ID_CHARS,
    )
    reviewer_note = _clean_bounded(
        reviewer_note, field="reviewerNote", code="invalid_reviewer_note",
        max_chars=MAX_NOTE_CHARS,
    )
    attribution = comparison_store.actor_attribution_from_context(
        reviewer_id,
        actor_context,
        actor_policy_id=actor_policy_id,
        actor_policy_version=actor_policy_version,
    )

    item = comparison_store.get_review_item(review_id, db_path=db_path)
    if item is None:
        raise ReviewNotFound(review_id)
    evaluation = comparison_store.get_evaluation(
        item["evaluation_id"], db_path=db_path
    )
    if evaluation is None:
        raise ReviewNotFound(review_id)
    governed = evaluation["governed_result"]

    normalized_edits = _normalize_edits(edits, governed)
    _validate_reason(action, reason_code, bool(normalized_edits))

    if normalized_edits:
        reviewed = _apply_edits(governed, normalized_edits, resolve)
    else:
        reviewed = json.loads(json.dumps(governed))
    reviewed["review"] = {"status": action, "review_id": review_id}
    # Risk decision, score, level, and reason codes are preserved verbatim.

    final_model = load_comparison(reviewed)  # comparison.v1 revalidation
    reviewed_wire = dump_comparison(final_model)
    final_hash = hashlib.sha256(
        json.dumps(reviewed_wire, sort_keys=True).encode("utf-8")
    ).hexdigest()

    request_hash = _request_hash(
        action, reviewer_id, reason_code, reviewer_note, normalized_edits
    )
    event_id = "rev_evt_" + hashlib.sha256(
        f"{review_id}|{request_hash}".encode("utf-8")
    ).hexdigest()[:16]

    event, created = comparison_store.decide_review(
        review_id,
        event_id=event_id,
        action=action,
        reviewer_id=reviewer_id,
        reason_code=reason_code,
        reviewer_note=reviewer_note,
        request_hash=request_hash,
        original_governed_result_hash=evaluation["governed_result_hash"],
        final_reviewed_result_hash=final_hash,
        reviewed_result_json=json.dumps(reviewed_wire),
        edit_summary_json=(
            json.dumps(normalized_edits) if normalized_edits else None
        ),
        **attribution,
        db_path=db_path,
    )
    if created:
        comparison_reliability.log_lifecycle_event(
            comparison_reliability.EVENT_REVIEW_DECIDED,
            comparison_id=event["comparison_id"],
            review_id=review_id,
            review_event_id=event["event_id"],
            review_action=event["action"],
            status=event["action"],
            result_hash=event["final_reviewed_result_hash"],
            actor_context=actor_context,
        )
    return event, created
