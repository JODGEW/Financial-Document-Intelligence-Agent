"""Release-gated comparison export (roadmap step 10).

Builds and persists a machine-readable comparison.export.v1 artifact for ONE
governance evaluation — but only when the actual governance workflow already
released it. Release eligibility follows the persisted records, never
client-submitted state:

- ``returned``              -> export the governed snapshot directly.
- ``returned_with_warning`` -> export the governed snapshot directly; the
  warning decision and reason codes ride in its ``risk`` block.
- ``held_for_review``       -> the single review item decides: pending and
  rejected refuse; approved exports the FINAL REVIEWED snapshot from the
  terminal review event, with an allowlisted decision block.
- ``blocked``               -> refuse (policy v1 never produces it; the gate
  exists so a future policy version cannot leak through).

This is release-gated export, not approved-only export: a clean returned
comparison has no review item and is NOT forced through a fake approval path —
policy determined review was not required, and the artifact says so via
``release_basis``.

The export never constructs a new summary and never modifies the selected
snapshot. Everything it wraps is re-validated first: the governed snapshot
must still validate as comparison.v1 and hash to the evaluation's stored
governed_result_hash; an approved path additionally requires the terminal
event's linkage and hashes to line up and its reviewed snapshot to validate.
Detector results, evaluations, review items, and events are read, never
written.

Idempotency: the logical export identity is (evaluation_id,
final_result_hash, export_schema_version) — deterministic ``exp_`` id plus a
unique index. A replay returns the ORIGINALLY persisted payload and
exported_at; the payload hash is computed over the timestamp-independent form
(the detector's result-hash convention), so fixed workflow inputs always hash
the same. A later policy evaluation (new evaluation id) or a different
approved reviewed snapshot (new final hash) is a separate export.

Export rows are deterministic application records in local SQLite — NOT
signed, tamper-proof, or immutable storage, and nothing here delivers,
uploads, or notifies. No LLM, no embeddings, no network.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import comparison_store
import config
from governance.comparison_export_schema import (
    EXPORT_SCHEMA_VERSION,
    RELEASE_APPROVED,
    RELEASE_RETURNED,
    RELEASE_WARNING,
    dump_export,
    load_export,
)
from governance.comparison_schema import load_comparison


class ComparisonExportError(Exception):
    """Export cannot proceed. ``code`` is stable; the message is safe for
    display — no paths, SQL, evidence text, or internal exception detail."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ExportNotFound(ComparisonExportError):
    """Unknown comparison or evaluation (API: 404)."""


class ExportNotEligible(ComparisonExportError):
    """The persisted workflow state does not permit release (API: 409)."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_hash(snapshot: dict[str, Any]) -> str:
    """The store-wide canonical hash: sha256 over sort_keys JSON (identical to
    comparison_governance._governed_hash and comparison_review's final hash)."""
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True).encode("utf-8")
    ).hexdigest()


def export_id_for(
    evaluation_id: str,
    final_result_hash: str,
    *,
    export_schema_version: str = EXPORT_SCHEMA_VERSION,
) -> str:
    """Deterministic id over the logical export identity (module docstring)."""
    key = f"{export_schema_version}|{evaluation_id}|{final_result_hash}"
    return "exp_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def payload_content_hash(payload: dict[str, Any]) -> str:
    """sha256 over the timestamp-independent canonical payload form.

    ``exported_at`` is excluded (and only it): the content hash must be stable
    for fixed workflow inputs, while the envelope timestamp records when the
    one persisted artifact was minted. No substantive comparison, governance,
    or review field is excluded.
    """
    stable = {key: value for key, value in payload.items() if key != "exported_at"}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _validated_snapshot(snapshot: dict[str, Any], label: str) -> None:
    try:
        load_comparison(snapshot)
    except Exception as exc:
        raise ExportNotEligible(
            "export_snapshot_invalid",
            f"the stored {label} snapshot no longer validates against "
            "comparison.v1 and cannot be exported",
        ) from exc


def _resolve_release(
    comparison_id: str, evaluation: dict[str, Any], db_path: str | Path
) -> dict[str, Any]:
    """Resolve which snapshot (if any) the persisted workflow state releases.

    Returns {release_basis, snapshot, final_result_hash, review_id,
    review_decision}. Raises ExportNotEligible with a stable code otherwise.
    """
    governed = evaluation["governed_result"]
    _validated_snapshot(governed, "governed")
    if _snapshot_hash(governed) != evaluation["governed_result_hash"]:
        raise ExportNotEligible(
            "export_snapshot_invalid",
            "the stored governed snapshot does not hash to the evaluation's "
            "recorded governed_result_hash",
        )

    decision = evaluation["decision"]
    if decision in ("returned", "returned_with_warning"):
        if (governed.get("review") or {}).get("status") != "not_required":
            raise ExportNotEligible(
                "export_snapshot_invalid",
                "a policy-returned governed snapshot must record "
                "review.status='not_required'",
            )
        return {
            "release_basis": (
                RELEASE_RETURNED if decision == "returned" else RELEASE_WARNING
            ),
            "snapshot": governed,
            "final_result_hash": evaluation["governed_result_hash"],
            "review_id": None,
            "review_decision": None,
        }

    if decision == "held_for_review":
        return _resolve_approved_release(evaluation, governed, db_path)

    # 'blocked' (unreachable under policy v1) and any future decision refuse.
    raise ExportNotEligible(
        "export_not_release_eligible",
        f"governance decision '{decision}' is not release-eligible",
    )


def _resolve_approved_release(
    evaluation: dict[str, Any], governed: dict[str, Any], db_path: str | Path
) -> dict[str, Any]:
    """The held path: only an approved terminal review event releases."""
    review_id = (governed.get("review") or {}).get("review_id")
    item = (
        comparison_store.get_review_item(review_id, db_path=db_path)
        if review_id
        else None
    )
    if item is None:
        raise ExportNotEligible(
            "review_missing",
            "the held evaluation has no comparison review item; nothing "
            "authorizes a release",
        )
    if item["evaluation_id"] != evaluation["evaluation_id"]:
        raise ExportNotEligible(
            "review_event_mismatch",
            "the review item is not linked to this governance evaluation",
        )
    if item["status"] == "pending":
        raise ExportNotEligible(
            "review_pending",
            "the held comparison is awaiting its review decision and cannot "
            "be exported",
        )
    if item["status"] == "rejected":
        raise ExportNotEligible(
            "review_rejected",
            "the review rejected this comparison; rejected results are not "
            "release-eligible",
        )

    events = comparison_store.list_review_events(item["review_id"], db_path=db_path)
    terminal = next(
        (
            event
            for event in events
            if event["event_id"] == item.get("terminal_event_id")
        ),
        None,
    )
    if (
        terminal is None
        or terminal["action"] != "approved"
        or terminal["evaluation_id"] != evaluation["evaluation_id"]
        or terminal["comparison_id"] != evaluation["comparison_id"]
        or terminal["original_governed_result_hash"]
        != evaluation["governed_result_hash"]
    ):
        raise ExportNotEligible(
            "review_event_mismatch",
            "the approved review's terminal event does not line up with this "
            "evaluation's stored hashes and linkage",
        )

    reviewed = terminal["reviewed_result"]
    _validated_snapshot(reviewed, "final reviewed")
    if (reviewed.get("review") or {}).get("status") != "approved":
        raise ExportNotEligible(
            "export_snapshot_invalid",
            "the final reviewed snapshot does not record review.status="
            "'approved'",
        )
    if _snapshot_hash(reviewed) != terminal["final_reviewed_result_hash"]:
        raise ExportNotEligible(
            "review_event_mismatch",
            "the final reviewed snapshot does not hash to the event's "
            "recorded final_reviewed_result_hash",
        )

    return {
        "release_basis": RELEASE_APPROVED,
        "snapshot": reviewed,
        "final_result_hash": terminal["final_reviewed_result_hash"],
        "review_id": item["review_id"],
        "review_decision": {
            "review_id": item["review_id"],
            "event_id": terminal["event_id"],
            "action": terminal["action"],
            "reviewer_id": terminal["reviewer_id"],
            "reason_code": terminal["reason_code"],
            "reviewer_note": terminal["reviewer_note"],
            "decided_at": terminal["created_at"],
            "original_governed_result_hash": terminal[
                "original_governed_result_hash"
            ],
            "final_reviewed_result_hash": terminal["final_reviewed_result_hash"],
            "edited_change_ids": [
                edit.get("change_id", "") for edit in terminal.get("edits") or []
            ],
        },
    }


def export_comparison(
    comparison_id: str,
    evaluation_id: str,
    *,
    db_path: str | Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create (or idempotently return) the export for one released evaluation.

    Returns (stored_export, created); ``stored_export['export']`` is the
    persisted comparison.export.v1 document. Raises ExportNotFound for an
    unknown comparison or evaluation and ExportNotEligible (stable code) when
    the persisted workflow state does not permit release.
    """
    db_path = db_path or config.COMPARISON_DB_PATH

    record = comparison_store.get_comparison(comparison_id, db_path=db_path)
    if record is None:
        raise ExportNotFound(
            "comparison_not_found", f"comparison {comparison_id!r} does not exist"
        )
    evaluation = comparison_store.get_evaluation(evaluation_id, db_path=db_path)
    if evaluation is None:
        raise ExportNotFound(
            "comparison_not_governed",
            "no governance evaluation with this id exists; govern the "
            "comparison first",
        )
    if evaluation["comparison_id"] != comparison_id:
        raise ExportNotEligible(
            "evaluation_mismatch",
            "the evaluation does not belong to this comparison",
        )

    stored = comparison_store.get_result(comparison_id, db_path=db_path)
    if stored is None or stored["result_hash"] != evaluation["comparison_result_hash"]:
        raise ExportNotEligible(
            "evaluation_result_stale",
            "the evaluation does not reference the comparison's current "
            "stored detector result",
        )

    release = _resolve_release(comparison_id, evaluation, db_path)
    export_id = export_id_for(evaluation_id, release["final_result_hash"])
    payload = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "export_id": export_id,
        "comparison_id": comparison_id,
        "evaluation_id": evaluation_id,
        "review_id": release["review_id"],
        "release_basis": release["release_basis"],
        "policy_id": evaluation["policy_id"],
        "policy_version": evaluation["policy_version"],
        "detector_result_hash": evaluation["comparison_result_hash"],
        "governed_result_hash": evaluation["governed_result_hash"],
        "final_result_hash": release["final_result_hash"],
        "exported_at": _utc_now_iso(),
        "comparison_result": release["snapshot"],
        "review_decision": release["review_decision"],
    }
    # Round-trip through the contract: an envelope that does not validate is a
    # bug, and the canonical persisted form is the model's own dump.
    payload = dump_export(load_export(payload))

    return comparison_store.record_export(
        export_id=export_id,
        export_schema_version=EXPORT_SCHEMA_VERSION,
        comparison_id=comparison_id,
        evaluation_id=evaluation_id,
        review_id=release["review_id"],
        release_basis=release["release_basis"],
        source_result_hash=evaluation["governed_result_hash"],
        final_result_hash=release["final_result_hash"],
        export_payload_json=json.dumps(payload),
        export_payload_hash=payload_content_hash(payload),
        db_path=db_path,
    )


def get_export(
    export_id: str, *, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    """Fetch one stored export, or None."""
    return comparison_store.get_export(
        export_id, db_path=db_path or config.COMPARISON_DB_PATH
    )


def list_exports(
    comparison_id: str, *, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Every stored export for a comparison, newest first."""
    return comparison_store.list_exports(
        comparison_id, db_path=db_path or config.COMPARISON_DB_PATH
    )
