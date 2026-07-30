"""FastAPI backend for the React chat interface."""

import json
import logging
import mimetypes
import sqlite3
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

from agent import detect_guardrail_intervention, query, stream_query
import comparison_detector
import comparison_export
import comparison_governance
import comparison_review
import comparison_store
import config
from governance import review_queue
from loaders.registry import supported_extensions

logger = logging.getLogger("api")

# The only error contract chat clients ever see. Raw exception text must never
# leave the server: provider errors, OSError strings, and library messages can
# carry absolute paths, request contents, and configuration fragments. The
# error_id is a fresh opaque correlation id logged with the server-side
# traceback — NOT an audit_id, because when a request fails no audit record
# was written and claiming one would be false.
SAFE_ERROR_CODE = "internal_error"
SAFE_ERROR_MESSAGE = "The request could not be completed. Please try again."


def _new_error_id() -> str:
    return f"err_{uuid.uuid4().hex[:12]}"


MAX_HISTORY_TURNS = 4
DOCS_DIR = Path(__file__).resolve().parent / "docs"

# Single source of truth: whatever the ingestion registry supports is also
# what the document sidebar / file-server endpoint exposes. Adding a new
# format never requires touching this file again.
SUPPORTED_DOC_SUFFIXES = set(supported_extensions())

# Explicit MIME types where the stdlib mimetypes module is ambiguous or
# under-specified for finance/compliance use; anything else falls back to
# ``mimetypes.guess_type`` at serve time.
MEDIA_TYPES = {
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".eml": "message/rfc822",
    ".msg": "application/vnd.ms-outlook",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
}


class ChatMessage(BaseModel):
    """A single chat message exchanged with the frontend."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    """Request body for a chat turn."""

    message: str = Field(min_length=1)
    history: list[ChatMessage] = Field(default_factory=list)


class RetrievedSource(BaseModel):
    """Retrieved chunk metadata exposed for auditability in the UI.

    Mirrors the review API's SafeReviewSource: corpus-relative fields only.
    The agent's source dicts also carry `source` (a local absolute path);
    _to_chat_source drops it before anything reaches the response.
    """

    rank: int
    source_name: str
    source_path: str
    section_title: str | None = None
    page: int | None = None
    excerpt: str


class ChatResponse(BaseModel):
    """Response body for a chat turn."""

    answer: str
    sources: list[RetrievedSource] = Field(default_factory=list)
    audit_id: str | None = None
    governance_report: dict | None = None


class CorpusDocument(BaseModel):
    """A source document available for review in the frontend."""

    name: str
    path: str
    file_type: str
    url: str


class SafeReviewSource(BaseModel):
    """A retrieved source from a review item, restricted to corpus-relative fields.

    The stored queue source also carries `source` (a local absolute path); that
    field is dropped by the mapper and has no counterpart here.
    """

    rank: int | None = None
    sourceName: str | None = None
    sourcePath: str | None = None
    sectionTitle: str | None = None
    page: int | None = None
    excerpt: str | None = None
    documentUrl: str | None = None


class ReviewSummary(BaseModel):
    """Queue listing entry for the reviewer UI. The field set is an allowlist."""

    reviewId: str
    question: str
    riskScore: float
    riskLevel: str
    riskReasons: list[str] = Field(default_factory=list)
    reviewStatus: Literal["pending", "approved", "rejected"]
    createdAt: str
    reviewedAt: str | None = None
    wasWithheld: bool | None = None


class ReviewDetail(ReviewSummary):
    """Full review item: summary fields plus the draft, evidence, and audit join."""

    auditId: str | None = None
    draftAnswer: str
    retrievedSources: list[SafeReviewSource] = Field(default_factory=list)
    decision: str | None = None
    reviewerNote: str | None = None
    governanceReport: dict | None = None


class ReviewActionRequest(BaseModel):
    """Body for the approve/reject review actions."""

    note: str | None = None


class ComparisonCreateRequest(BaseModel):
    """Body for creating a comparison. Identity fields only.

    The client may name the two filings and the section scope — nothing else.
    Company, form type, dates, source names, and hashes are resolved
    server-side from the filing registry, never accepted from the client.
    """

    previousFilingId: str = Field(min_length=1)
    currentFilingId: str = Field(min_length=1)
    sectionScope: list[str] | None = None


class ComparisonRecordDTO(BaseModel):
    """A persisted comparison entity. The field set is an allowlist.

    Pre-detection on purpose: there are no change/evidence/validation/risk/
    review fields because no detector has run — this is a ComparisonRecord,
    not a comparison.v1 ComparisonResult.
    """

    comparisonId: str
    schemaVersion: str
    workflowVersion: str
    previousFilingId: str
    currentFilingId: str
    sectionScope: list[str]
    status: Literal["ready_for_detection", "detecting", "detected", "failed"]
    createdAt: str
    updatedAt: str
    failureCode: str | None = None
    failureSummary: str | None = None


class ComparisonCreateResponse(BaseModel):
    """POST /api/comparisons response: the entity plus idempotency outcome."""

    created: bool
    comparison: ComparisonRecordDTO


class GovernanceEvaluationDTO(BaseModel):
    """An immutable comparison governance evaluation. Allowlisted fields.

    ``governedResult`` is the validated comparison.v1 snapshot (risk/review
    updated, everything else preserved from the detector result) — the same
    wire contract the result endpoint uses. No storage paths, no SQL detail.
    """

    evaluationId: str
    comparisonId: str
    policyId: str
    policyVersion: str
    riskScore: float
    riskLevel: str
    decision: str
    reasonCodes: list[str]
    evaluatedAt: str
    comparisonResultHash: str
    governedResultHash: str
    governedResult: dict


class GovernanceEvaluateResponse(BaseModel):
    """POST governance response: the evaluation plus idempotency outcome."""

    created: bool
    evaluation: GovernanceEvaluationDTO


class ComparisonReviewSummaryDTO(BaseModel):
    """Pending comparison-review summary. Deliberately excerpt-free: the
    governed result (with evidence) is referenced by hash and served by the
    governance endpoint, never copied into list rows."""

    reviewId: str
    comparisonId: str
    evaluationId: str
    status: Literal["pending"]
    riskScore: float
    riskLevel: str
    reasonCodes: list[str]
    createdAt: str


class ComparisonReviewEditRequest(BaseModel):
    """One requested summary edit, addressed by change_id. Nothing else about
    a change is editable."""

    changeId: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class ComparisonReviewDecisionRequest(BaseModel):
    """Body for the terminal comparison-review decision.

    reviewerId is a SELF-ASSERTED local identifier (email-like string, local
    username, test id) — it is recorded as attribution metadata and is NOT
    authenticated identity. reasonCode must come from the action's allowlist;
    reviewerNote is required, bounded prose.
    """

    action: Literal["approved", "rejected"]
    reviewerId: str = Field(min_length=1)
    reasonCode: str = Field(min_length=1)
    reviewerNote: str = Field(min_length=1)
    edits: list[ComparisonReviewEditRequest] | None = None


class ComparisonReviewEventDTO(BaseModel):
    """One append-only review decision event. Allowlisted fields."""

    eventId: str
    reviewId: str
    comparisonId: str
    evaluationId: str
    action: Literal["approved", "rejected"]
    reviewerId: str
    reasonCode: str
    reviewerNote: str
    originalGovernedResultHash: str
    finalReviewedResultHash: str
    editedChangeIds: list[str]
    createdAt: str


class ComparisonReviewDecisionDTO(ComparisonReviewEventDTO):
    """The terminal decision with the complete final reviewed comparison.v1
    snapshot (the sanctioned wire contract, as with governed results)."""

    reviewedResult: dict


class ComparisonReviewDecisionResponse(BaseModel):
    created: bool
    decision: ComparisonReviewDecisionDTO


class ComparisonReviewDetailDTO(BaseModel):
    """Full review item: linkage, status, governance metadata, the governed
    result, and the terminal decision when one exists."""

    reviewId: str
    comparisonId: str
    evaluationId: str
    status: Literal["pending", "approved", "rejected"]
    riskScore: float
    riskLevel: str
    reasonCodes: list[str]
    comparisonResultHash: str
    governedResultHash: str
    createdAt: str
    decidedAt: str | None = None
    governedResult: dict
    decision: ComparisonReviewDecisionDTO | None = None


class ComparisonExportCreateRequest(BaseModel):
    """Body for creating an export. The evaluation id is the ONLY input.

    Eligibility is resolved server-side from persisted records: the client
    cannot submit a decision, review status, reviewer identity, result JSON,
    result hashes, policy identity, or release basis — unknown fields are
    rejected (422), not ignored.
    """

    model_config = ConfigDict(extra="forbid")

    evaluationId: str = Field(min_length=1)


class ComparisonExportSummaryDTO(BaseModel):
    """One export row for list views. The field set is an allowlist:
    identity, linkage, basis, and hashes only — no comparison evidence, no
    exported payload, no reviewer note."""

    exportId: str
    exportSchemaVersion: str
    comparisonId: str
    evaluationId: str
    reviewId: str | None = None
    releaseBasis: Literal[
        "returned_by_policy",
        "returned_with_warning_by_policy",
        "approved_after_review",
    ]
    sourceResultHash: str
    finalResultHash: str
    exportPayloadHash: str
    createdAt: str


class ComparisonExportCreateResponse(BaseModel):
    """POST exports response: the persisted comparison.export.v1 document
    (verbatim, as with detection results) plus the idempotency outcome."""

    created: bool
    export: dict


class DetectionAttemptDTO(BaseModel):
    """One durable detection execution. The field set is an allowlist.

    Deliberately payload-free: no comparison result, no evidence excerpts, no
    filing content. ``failureSummary`` is the store's bounded, code-derived
    summary — never an exception message, path, or SQL fragment.
    """

    attemptId: str
    comparisonId: str
    attemptNumber: int
    status: Literal["running", "succeeded", "failed"]
    detectorVersion: str
    workflowVersion: str
    previousSourceHash: str
    currentSourceHash: str
    startedAt: str
    finishedAt: str | None = None
    resultHash: str | None = None
    failureCode: str | None = None
    failureSummary: str | None = None


class DetectionEventDTO(BaseModel):
    """One append-only detection transition event. Allowlisted fields.

    Application records, NOT tamper-proof storage. Carries a stable event type
    and at most a result hash or failure code — never result JSON, evidence,
    reviewer notes, paths, SQL, or raw error text.
    """

    eventId: str
    attemptId: str
    comparisonId: str
    eventType: Literal[
        "detection_started", "detection_succeeded", "detection_failed"
    ]
    eventSeq: int
    createdAt: str
    resultHash: str | None = None
    failureCode: str | None = None


class ComparisonDetectResponse(BaseModel):
    """POST /api/comparisons/{id}/detect response.

    ``result`` is the validated comparison.v1 wire document exactly as
    persisted (the schema contract IS the API shape for detection results —
    it contains filing ids, section keys, chunk ids, and excerpts, never
    absolute paths or storage detail).

    ``attemptId`` is additive and optional, so the existing wire contract is
    unchanged for current clients: it names the durable execution that produced
    the result. It is deliberately NOT inserted into the comparison.v1 document
    — attempt metadata is workflow bookkeeping, not part of the result schema.
    It is null only for a result stored before attempt tracking existed.
    """

    created: bool
    result: dict
    attemptId: str | None = None


app = FastAPI(title="Financial Document Intelligence API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _to_agent_history(history: list[ChatMessage]) -> list[tuple[str, str]]:
    """Convert UI message history into LangChain chat roles.

    Drops blocked turns (the refused question and its block message). Bedrock's
    guardrail scans the whole input including history, so replaying a denied
    question would block every later turn in the conversation. The blocked turn
    stays visible in the UI; it just is not fed back to the model.
    """
    recent = history[-(MAX_HISTORY_TURNS * 2):]
    role_map = {
        "user": "human",
        "assistant": "ai",
    }

    cleaned: list[tuple[str, str]] = []
    for msg in recent:
        role = role_map[msg.role]
        if role == "ai" and detect_guardrail_intervention(msg.content, []) == "blocked":
            # Also drop the user question that triggered the block.
            if cleaned and cleaned[-1][0] == "human":
                cleaned.pop()
            continue
        cleaned.append((role, msg.content))
    return cleaned


def _to_chat_source(raw: dict) -> RetrievedSource:
    """Map an agent source dict onto the chat allowlist, field by field.

    The `source` value (a local absolute path) is discarded here, matching the
    review API's SafeReviewSource mapping.
    """
    return RetrievedSource(
        rank=raw.get("rank", 0),
        source_name=raw.get("source_name", ""),
        source_path=raw.get("source_path", ""),
        section_title=raw.get("section_title"),
        page=raw.get("page"),
        excerpt=raw.get("excerpt", ""),
    )


def _resolve_doc_path(document_path: str) -> Path:
    """Resolve a requested document path inside the docs directory."""
    root = DOCS_DIR.resolve()
    resolved = (root / document_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc

    if not resolved.is_file() or resolved.suffix.lower() not in SUPPORTED_DOC_SUFFIXES:
        raise HTTPException(status_code=404, detail="Document not found.")
    return resolved


@app.get("/api/health")
def health() -> dict[str, str]:
    """Health check for local development."""
    return {"status": "ok"}


@app.get("/api/documents", response_model=list[CorpusDocument])
def list_documents() -> list[CorpusDocument]:
    """List reviewable source documents from the local corpus."""
    documents = []
    for path in sorted(DOCS_DIR.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_DOC_SUFFIXES:
            continue

        relative_path = path.relative_to(DOCS_DIR).as_posix()
        encoded_path = quote(relative_path)
        documents.append(
            CorpusDocument(
                name=path.name,
                path=relative_path,
                file_type=path.suffix.lower().lstrip("."),
                url=f"/api/documents/{encoded_path}",
            )
        )
    return documents


@app.get("/api/documents/{document_path:path}")
def get_document(document_path: str) -> FileResponse:
    """Open a source document from the local corpus."""
    path = _resolve_doc_path(document_path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=path.name,
            content_disposition_type="inline",
        )

    media_type = MEDIA_TYPES.get(suffix)
    if media_type is None:
        guessed, _ = mimetypes.guess_type(path.name)
        media_type = guessed or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


def _document_url_for(source_path: str | None) -> str | None:
    """Return the served URL for a corpus-relative source path, or None.

    Reuses the docs-directory resolver so a URL is only produced for files the
    document endpoint would actually serve. Anything unresolvable (missing file,
    escape attempt, unsupported suffix) maps to None instead of an error.
    """
    if not source_path or not isinstance(source_path, str):
        return None
    try:
        resolved = _resolve_doc_path(source_path)
    except HTTPException:
        return None
    relative = resolved.relative_to(DOCS_DIR.resolve()).as_posix()
    return f"/api/documents/{quote(relative)}"


def _to_safe_source(raw: dict) -> SafeReviewSource:
    """Map one stored queue source onto the response allowlist, field by field.

    The stored `source` value (a local absolute path) is discarded here.
    """
    source_path = raw.get("source_path")
    return SafeReviewSource(
        rank=raw.get("rank"),
        sourceName=raw.get("source_name"),
        sourcePath=source_path,
        sectionTitle=raw.get("section_title"),
        page=raw.get("page"),
        excerpt=raw.get("excerpt"),
        documentUrl=_document_url_for(source_path),
    )


def _to_review_summary(item: dict, status: str) -> ReviewSummary:
    """Map a stored queue item onto the summary allowlist. No dict spread."""
    return ReviewSummary(
        reviewId=item.get("reviewId", ""),
        question=item.get("question", ""),
        riskScore=item.get("riskScore", 0.0),
        riskLevel=item.get("riskLevel", ""),
        riskReasons=list(item.get("riskReasons") or []),
        reviewStatus=status,
        createdAt=item.get("createdAt") or "",
        reviewedAt=item.get("reviewedAt"),
        wasWithheld=item.get("wasWithheld"),
    )


def _to_review_detail(item: dict, status: str) -> ReviewDetail:
    """Map a stored queue item onto the detail allowlist. No dict spread."""
    sources = item.get("retrievedSources") or []
    return ReviewDetail(
        reviewId=item.get("reviewId", ""),
        question=item.get("question", ""),
        riskScore=item.get("riskScore", 0.0),
        riskLevel=item.get("riskLevel", ""),
        riskReasons=list(item.get("riskReasons") or []),
        reviewStatus=status,
        createdAt=item.get("createdAt") or "",
        reviewedAt=item.get("reviewedAt"),
        wasWithheld=item.get("wasWithheld"),
        auditId=item.get("auditId"),
        draftAnswer=item.get("draftAnswer", ""),
        retrievedSources=[
            _to_safe_source(source) for source in sources if isinstance(source, dict)
        ],
        decision=item.get("decision"),
        reviewerNote=item.get("reviewerNote"),
        governanceReport=_governance_report_for(item.get("auditId")),
    )


def _governance_report_for(audit_id: str | None) -> dict | None:
    """Scan the audit log for audit_id and return only its governance_report.

    The rest of the audit record stays server-side on purpose: retrieved_sources
    carries local absolute paths and response_trace carries full tool content.
    A missing record, absent log, or unreadable line yields None.
    """
    if not audit_id:
        return None
    try:
        with Path(config.AUDIT_LOG_PATH).open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("audit_id") == audit_id:
                    report = record.get("governance_report")
                    return report if isinstance(report, dict) else None
    except OSError:
        return None
    return None


@app.get("/api/reviews", response_model=list[ReviewSummary])
def list_reviews(
    status: Literal["pending", "approved", "rejected", "all"] = "pending",
) -> list[ReviewSummary]:
    """List human review queue items.

    Ordering: pending items come oldest first by createdAt. Approved and
    rejected items come most recently reviewed first (reviewedAt, falling back
    to createdAt). For status=all the response is the pending block first in
    its own order, then the terminal block in its own order.
    """
    pairs = review_queue.list_items(config.REVIEW_QUEUE_DIR, status)
    pending = [pair for pair in pairs if pair[1] == "pending"]
    terminal = [pair for pair in pairs if pair[1] != "pending"]
    pending.sort(key=lambda pair: pair[0].get("createdAt") or "")
    terminal.sort(
        key=lambda pair: pair[0].get("reviewedAt") or pair[0].get("createdAt") or "",
        reverse=True,
    )
    return [_to_review_summary(item, found) for item, found in pending + terminal]


@app.get("/api/reviews/{review_id}", response_model=ReviewDetail)
def get_review(review_id: str) -> ReviewDetail:
    """Fetch one review item by id, searching pending and terminal files."""
    found = review_queue.get_any(review_id, config.REVIEW_QUEUE_DIR)
    if found is None:
        raise HTTPException(status_code=404, detail="Review item not found.")
    item, status = found
    return _to_review_detail(item, status)


def _resolve_review_action(review_id: str, action, target_status: str, note: str | None) -> ReviewDetail:
    """Shared approve/reject flow: 404 absent, 409 terminal, 200 on success."""
    found = review_queue.get_any(review_id, config.REVIEW_QUEUE_DIR)
    if found is None:
        raise HTTPException(status_code=404, detail="Review item not found.")
    if found[1] != "pending":
        raise HTTPException(
            status_code=409, detail=f"Review item is already {found[1]}."
        )

    try:
        resolved = action(review_id, config.REVIEW_QUEUE_DIR, note=note)
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail="Failed to write the review queue."
        ) from exc

    if resolved is None:
        # The item left pending between the check and the write. Re-classify
        # into 404/409 rather than surfacing a 500.
        recheck = review_queue.get_any(review_id, config.REVIEW_QUEUE_DIR)
        if recheck is None:
            raise HTTPException(status_code=404, detail="Review item not found.")
        raise HTTPException(
            status_code=409, detail=f"Review item is already {recheck[1]}."
        )
    return _to_review_detail(resolved, target_status)


@app.post("/api/reviews/{review_id}/approve", response_model=ReviewDetail)
def approve_review(
    review_id: str, payload: ReviewActionRequest | None = None
) -> ReviewDetail:
    """Approve a pending review item, stamping reviewedAt server-side."""
    note = payload.note if payload else None
    return _resolve_review_action(review_id, review_queue.approve, "approved", note)


@app.post("/api/reviews/{review_id}/reject", response_model=ReviewDetail)
def reject_review(
    review_id: str, payload: ReviewActionRequest | None = None
) -> ReviewDetail:
    """Reject a pending review item, stamping reviewedAt server-side."""
    note = payload.note if payload else None
    return _resolve_review_action(review_id, review_queue.reject, "rejected", note)


def _to_comparison_dto(record: dict) -> ComparisonRecordDTO:
    """Map a stored comparison record onto the allowlist, field by field.

    Filing ids, scope keys, timestamps, and lifecycle fields only — no
    storage paths, no registry entry contents.
    """
    return ComparisonRecordDTO(
        comparisonId=record.get("comparison_id", ""),
        schemaVersion=record.get("schema_version", ""),
        workflowVersion=record.get("workflow_version", ""),
        previousFilingId=record.get("previous_filing_id", ""),
        currentFilingId=record.get("current_filing_id", ""),
        sectionScope=list(record.get("section_scope") or []),
        status=record.get("status", "ready_for_detection"),
        createdAt=record.get("created_at", ""),
        updatedAt=record.get("updated_at", ""),
        failureCode=record.get("failure_code"),
        failureSummary=record.get("failure_summary"),
    )


def _comparison_storage_error(exc: Exception) -> HTTPException:
    """Log the real storage failure; return a safe 500 with a correlation id.

    SQL text, SQLite error strings, and filesystem paths stay server-side.
    """
    error_id = _new_error_id()
    logger.exception("Comparison storage failure (error_id=%s)", error_id)
    return HTTPException(
        status_code=500,
        detail={
            "code": "comparison_storage_error",
            "message": "Failed to access comparison storage.",
            "error_id": error_id,
        },
    )


@app.post("/api/comparisons", response_model=ComparisonCreateResponse)
def create_comparison(
    request: ComparisonCreateRequest, response: Response
) -> ComparisonCreateResponse:
    """Create (or idempotently return) a comparison for a validated filing pair.

    201 with created=true for a new record; 200 with created=false when the
    identical logical comparison already exists. Ineligible pairs are 422
    with stable machine-readable reason codes — nothing is persisted.
    """
    try:
        record, created = comparison_store.create_comparison(
            request.previousFilingId.strip(),
            request.currentFilingId.strip(),
            request.sectionScope,
        )
    except comparison_store.ComparisonPairError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_comparison_pair",
                "reasons": exc.reasons,
                "message": exc.detail,
            },
        ) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc

    response.status_code = 201 if created else 200
    return ComparisonCreateResponse(
        created=created, comparison=_to_comparison_dto(record)
    )


@app.get("/api/comparisons", response_model=list[ComparisonRecordDTO])
def list_comparisons(
    filing_id: str | None = None,
    status: Literal["ready_for_detection", "detecting", "detected", "failed"] | None = None,
) -> list[ComparisonRecordDTO]:
    """List comparisons, newest first. Minimal stable filters only:
    filing_id matches either side of the pair; status matches exactly."""
    try:
        records = comparison_store.list_comparisons(
            filing_id=filing_id, status=status
        )
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [_to_comparison_dto(record) for record in records]


@app.get("/api/comparisons/{comparison_id}", response_model=ComparisonRecordDTO)
def get_comparison(comparison_id: str) -> ComparisonRecordDTO:
    """Fetch one comparison by id; 404 when it does not exist."""
    try:
        record = comparison_store.get_comparison(comparison_id)
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Comparison not found.")
    return _to_comparison_dto(record)


@app.post(
    "/api/comparisons/{comparison_id}/detect",
    response_model=ComparisonDetectResponse,
)
def detect_comparison(
    comparison_id: str, response: Response
) -> ComparisonDetectResponse:
    """Run deterministic Item 1A change detection for a persisted comparison.

    201 + created=true for a newly persisted result; 200 + created=false when
    the same detector version already produced a result for the same source
    hashes; 404 unknown comparison; 409 for lifecycle violations, an already
    running attempt (`detection_in_progress`), or stale inputs; 422 when the
    filing pair no longer validates against the registry; safe 500 with a
    correlation id for unexpected faults.

    The response additionally carries `attemptId`, naming the durable execution
    behind the result.
    """
    try:
        result, created, attempt_id = comparison_detector.detect_with_attempt(
            comparison_id
        )
    except comparison_detector.UnknownComparison:
        raise HTTPException(status_code=404, detail="Comparison not found.")
    except (
        comparison_detector.DetectionNotReady,
        comparison_detector.DetectionInProgress,
        comparison_detector.DetectionInputsStale,
        comparison_detector.DetectionVersionSuperseded,
    ) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except comparison_store.ComparisonPairError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_comparison_pair",
                "reasons": exc.reasons,
                "message": exc.detail,
            },
        ) from exc
    except comparison_detector.DetectionInternalError as exc:
        error_id = _new_error_id()
        logger.exception("Comparison detection failed (error_id=%s)", error_id)
        raise HTTPException(
            status_code=500,
            detail={
                "code": exc.code,
                "message": "Detection failed unexpectedly. Please try again.",
                "error_id": error_id,
            },
        ) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc

    response.status_code = 201 if created else 200
    return ComparisonDetectResponse(
        created=created, result=result, attemptId=attempt_id
    )


def _to_detection_attempt_dto(record: dict) -> DetectionAttemptDTO:
    """Map a stored attempt onto the allowlist, field by field."""
    return DetectionAttemptDTO(
        attemptId=record.get("attempt_id", ""),
        comparisonId=record.get("comparison_id", ""),
        attemptNumber=record.get("attempt_number", 0),
        status=record.get("status", "running"),
        detectorVersion=record.get("detector_version", ""),
        workflowVersion=record.get("workflow_version", ""),
        previousSourceHash=record.get("previous_source_hash", ""),
        currentSourceHash=record.get("current_source_hash", ""),
        startedAt=record.get("started_at", ""),
        finishedAt=record.get("finished_at"),
        resultHash=record.get("result_hash"),
        failureCode=record.get("failure_code"),
        failureSummary=record.get("failure_summary"),
    )


def _to_detection_event_dto(record: dict) -> DetectionEventDTO:
    """Map a stored transition event onto the allowlist, field by field."""
    return DetectionEventDTO(
        eventId=record.get("event_id", ""),
        attemptId=record.get("attempt_id", ""),
        comparisonId=record.get("comparison_id", ""),
        eventType=record.get("event_type", "detection_started"),
        eventSeq=record.get("event_seq", 0),
        createdAt=record.get("created_at", ""),
        resultHash=record.get("result_hash"),
        failureCode=record.get("failure_code"),
    )


@app.get(
    "/api/comparisons/{comparison_id}/detection-attempts",
    response_model=list[DetectionAttemptDTO],
)
def list_detection_attempts(comparison_id: str) -> list[DetectionAttemptDTO]:
    """Every detection attempt for one comparison, in execution order
    (attempt_number ascending); 404 when the comparison does not exist.

    Summaries only — no comparison result payload and no evidence.
    """
    try:
        if comparison_store.get_comparison(comparison_id) is None:
            raise HTTPException(status_code=404, detail="Comparison not found.")
        records = comparison_store.list_detection_attempts(comparison_id)
    except HTTPException:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [_to_detection_attempt_dto(record) for record in records]


@app.get("/api/detection-attempts/{attempt_id}", response_model=DetectionAttemptDTO)
def get_detection_attempt(attempt_id: str) -> DetectionAttemptDTO:
    """One detection attempt by id; 404 when it does not exist."""
    try:
        record = comparison_store.get_detection_attempt(attempt_id)
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Detection attempt not found.")
    return _to_detection_attempt_dto(record)


@app.get(
    "/api/detection-attempts/{attempt_id}/events",
    response_model=list[DetectionEventDTO],
)
def list_detection_events(attempt_id: str) -> list[DetectionEventDTO]:
    """Append-only transition events for one attempt, oldest first.

    Deterministically ordered by the stored sequence key, so a started event
    always precedes its terminal event regardless of timestamp resolution.
    404 when the attempt does not exist.
    """
    try:
        if comparison_store.get_detection_attempt(attempt_id) is None:
            raise HTTPException(
                status_code=404, detail="Detection attempt not found."
            )
        records = comparison_store.list_detection_events(attempt_id)
    except HTTPException:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [_to_detection_event_dto(record) for record in records]


def _to_governance_dto(record: dict) -> GovernanceEvaluationDTO:
    """Map a stored evaluation onto the allowlist, field by field."""
    return GovernanceEvaluationDTO(
        evaluationId=record.get("evaluation_id", ""),
        comparisonId=record.get("comparison_id", ""),
        policyId=record.get("policy_id", ""),
        policyVersion=record.get("policy_version", ""),
        riskScore=record.get("risk_score", 0.0),
        riskLevel=record.get("risk_level", ""),
        decision=record.get("decision", ""),
        reasonCodes=list(record.get("reason_codes") or []),
        evaluatedAt=record.get("evaluated_at", ""),
        comparisonResultHash=record.get("comparison_result_hash", ""),
        governedResultHash=record.get("governed_result_hash", ""),
        governedResult=record.get("governed_result") or {},
    )


@app.post(
    "/api/comparisons/{comparison_id}/governance",
    response_model=GovernanceEvaluateResponse,
)
def evaluate_comparison_governance(
    comparison_id: str, response: Response
) -> GovernanceEvaluateResponse:
    """Evaluate a detected comparison under the comparison risk policy.

    201 + created=true for a new immutable evaluation (held decisions also
    create their single pending review item in the same transaction); 200 +
    created=false for the idempotent replay; 404 unknown comparison or no
    detector result; 409 when the stored result is invalid or the hash is
    stale; safe 500 with correlation id otherwise.
    """
    try:
        record, created = comparison_governance.govern(comparison_id)
    except comparison_governance.GovernanceNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except comparison_governance.GovernanceResultInvalid as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": exc.message}
        ) from exc
    except comparison_store.ComparisonLifecycleError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.status, "message": str(exc)},
        ) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc

    response.status_code = 201 if created else 200
    return GovernanceEvaluateResponse(
        created=created, evaluation=_to_governance_dto(record)
    )


@app.get(
    "/api/comparisons/{comparison_id}/governance",
    response_model=GovernanceEvaluationDTO,
)
def get_comparison_governance(comparison_id: str) -> GovernanceEvaluationDTO:
    """The evaluation for the current policy id/version and current result
    hash; 404 when none exists yet."""
    try:
        record = comparison_governance.get_current_evaluation(comparison_id)
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No governance evaluation exists for this comparison "
            "under the current policy.",
        )
    return _to_governance_dto(record)


@app.get("/api/comparison-reviews", response_model=list[ComparisonReviewSummaryDTO])
def list_comparison_reviews(
    comparison_id: str | None = None,
) -> list[ComparisonReviewSummaryDTO]:
    """Pending comparison review summaries (newest first). Read-only: no
    approve/reject exists yet, and rows carry no evidence excerpts."""
    try:
        items = comparison_store.list_comparison_reviews(
            comparison_id=comparison_id
        )
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [
        ComparisonReviewSummaryDTO(
            reviewId=item.get("review_id", ""),
            comparisonId=item.get("comparison_id", ""),
            evaluationId=item.get("evaluation_id", ""),
            status=item.get("status", "pending"),
            riskScore=item.get("risk_score", 0.0),
            riskLevel=item.get("risk_level", ""),
            reasonCodes=list(item.get("reason_codes") or []),
            createdAt=item.get("created_at", ""),
        )
        for item in items
    ]


def _to_review_event_dto(event: dict, with_result: bool) -> ComparisonReviewDecisionDTO | ComparisonReviewEventDTO:
    """Map a stored decision event onto the allowlist, field by field."""
    fields = dict(
        eventId=event.get("event_id", ""),
        reviewId=event.get("review_id", ""),
        comparisonId=event.get("comparison_id", ""),
        evaluationId=event.get("evaluation_id", ""),
        action=event.get("action", "approved"),
        reviewerId=event.get("reviewer_id", ""),
        reasonCode=event.get("reason_code", ""),
        reviewerNote=event.get("reviewer_note", ""),
        originalGovernedResultHash=event.get("original_governed_result_hash", ""),
        finalReviewedResultHash=event.get("final_reviewed_result_hash", ""),
        editedChangeIds=[
            edit.get("change_id", "") for edit in event.get("edits") or []
        ],
        createdAt=event.get("created_at", ""),
    )
    if with_result:
        return ComparisonReviewDecisionDTO(
            **fields, reviewedResult=event.get("reviewed_result") or {}
        )
    return ComparisonReviewEventDTO(**fields)


@app.get(
    "/api/comparison-reviews/{review_id}",
    response_model=ComparisonReviewDetailDTO,
)
def get_comparison_review(review_id: str) -> ComparisonReviewDetailDTO:
    """One review item with governance metadata, the governed result, and the
    terminal decision (with final reviewed snapshot) when present."""
    try:
        item = comparison_store.get_review_item(review_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Review item not found.")
        evaluation = comparison_store.get_evaluation(item["evaluation_id"])
        events = comparison_store.list_review_events(review_id)
    except HTTPException:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc

    decision = None
    if item.get("terminal_event_id") and events:
        decision = _to_review_event_dto(events[-1], with_result=True)
    return ComparisonReviewDetailDTO(
        reviewId=item.get("review_id", ""),
        comparisonId=item.get("comparison_id", ""),
        evaluationId=item.get("evaluation_id", ""),
        status=item.get("status", "pending"),
        riskScore=(evaluation or {}).get("risk_score", 0.0),
        riskLevel=(evaluation or {}).get("risk_level", ""),
        reasonCodes=list((evaluation or {}).get("reason_codes") or []),
        comparisonResultHash=item.get("comparison_result_hash", ""),
        governedResultHash=item.get("governed_result_hash", ""),
        createdAt=item.get("created_at", ""),
        decidedAt=item.get("decided_at"),
        governedResult=(evaluation or {}).get("governed_result") or {},
        decision=decision,
    )


@app.post(
    "/api/comparison-reviews/{review_id}/decision",
    response_model=ComparisonReviewDecisionResponse,
)
def decide_comparison_review(
    review_id: str, request: ComparisonReviewDecisionRequest, response: Response
) -> ComparisonReviewDecisionResponse:
    """Apply the single terminal decision to a pending comparison review.

    201 new decision; 200 byte-equivalent idempotent replay; 404 unknown
    review; 409 already decided by a different request; 422 invalid reviewer /
    reason / action-edit combination / unsupported edit; safe 500 otherwise.
    """
    try:
        event, created = comparison_review.decide(
            review_id,
            action=request.action,
            reviewer_id=request.reviewerId,
            reason_code=request.reasonCode,
            reviewer_note=request.reviewerNote,
            edits=[
                {"change_id": edit.changeId, "summary": edit.summary}
                for edit in (request.edits or [])
            ]
            or None,
        )
    except comparison_review.ReviewNotFound:
        raise HTTPException(status_code=404, detail="Review item not found.")
    except comparison_store.ReviewAlreadyDecided as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "review_already_decided",
                "message": f"review is already {exc.status} by a different "
                "request; decisions are never overwritten",
            },
        ) from exc
    except comparison_review.ReviewDecisionError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc

    response.status_code = 201 if created else 200
    return ComparisonReviewDecisionResponse(
        created=created,
        decision=_to_review_event_dto(event, with_result=True),
    )


@app.get(
    "/api/comparison-reviews/{review_id}/events",
    response_model=list[ComparisonReviewEventDTO],
)
def list_comparison_review_events(review_id: str) -> list[ComparisonReviewEventDTO]:
    """Append-only decision history, oldest first (currently 0 or 1 terminal
    event; the list shape leaves room for future non-terminal events)."""
    try:
        if comparison_store.get_review_item(review_id) is None:
            raise HTTPException(status_code=404, detail="Review item not found.")
        events = comparison_store.list_review_events(review_id)
    except HTTPException:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [_to_review_event_dto(event, with_result=False) for event in events]


@app.get("/api/comparisons/{comparison_id}/result")
def get_comparison_result(comparison_id: str) -> dict:
    """Return the persisted, validated comparison.v1 wire document.

    404 when the comparison does not exist or has no detection result yet.
    """
    try:
        stored = comparison_store.get_result(comparison_id)
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail="No detection result exists for this comparison.",
        )
    return stored["result"]


def _to_export_summary_dto(record: dict) -> ComparisonExportSummaryDTO:
    """Map a stored export row onto the summary allowlist, field by field.

    Deliberately payload-free: no comparison evidence and no reviewer note
    ever appear in list responses.
    """
    return ComparisonExportSummaryDTO(
        exportId=record.get("export_id", ""),
        exportSchemaVersion=record.get("export_schema_version", ""),
        comparisonId=record.get("comparison_id", ""),
        evaluationId=record.get("evaluation_id", ""),
        reviewId=record.get("review_id"),
        releaseBasis=record.get("release_basis", "returned_by_policy"),
        sourceResultHash=record.get("source_result_hash", ""),
        finalResultHash=record.get("final_result_hash", ""),
        exportPayloadHash=record.get("export_payload_hash", ""),
        createdAt=record.get("created_at", ""),
    )


@app.post(
    "/api/comparisons/{comparison_id}/exports",
    response_model=ComparisonExportCreateResponse,
)
def create_comparison_export(
    comparison_id: str, request: ComparisonExportCreateRequest, response: Response
) -> ComparisonExportCreateResponse:
    """Create (or idempotently return) the release-gated export for one
    governance evaluation.

    201 + created=true for a newly persisted export; 200 + created=false for
    the idempotent replay (the ORIGINAL payload and exported_at are returned);
    404 unknown comparison or evaluation; 409 with a stable reason code when
    the persisted workflow state does not permit release (pending or rejected
    review, stale hashes, mismatched linkage, blocked); safe 500 with a
    correlation id for unexpected storage failures.
    """
    try:
        record, created = comparison_export.export_comparison(
            comparison_id, request.evaluationId.strip()
        )
    except comparison_export.ExportNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except comparison_export.ExportNotEligible as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc

    response.status_code = 201 if created else 200
    return ComparisonExportCreateResponse(created=created, export=record["export"])


@app.get(
    "/api/comparisons/{comparison_id}/exports",
    response_model=list[ComparisonExportSummaryDTO],
)
def list_comparison_exports(comparison_id: str) -> list[ComparisonExportSummaryDTO]:
    """Export summaries for one comparison, newest first; 404 when the
    comparison does not exist. Summaries only — the full artifact is served
    by GET /api/comparison-exports/{export_id}."""
    try:
        if comparison_store.get_comparison(comparison_id) is None:
            raise HTTPException(status_code=404, detail="Comparison not found.")
        records = comparison_export.list_exports(comparison_id)
    except HTTPException:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [_to_export_summary_dto(record) for record in records]


@app.get("/api/comparison-exports/{export_id}")
def get_comparison_export(export_id: str) -> dict:
    """Return the persisted comparison.export.v1 document verbatim
    (application/json) — that schema IS the API contract for exports. 404
    when no export with this id exists. No file is generated and no
    filesystem path is exposed."""
    try:
        record = comparison_export.get_export(export_id)
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Export not found.")
    return record["export"]


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Answer a chat message using the existing RAG agent."""
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    try:
        result = await run_in_threadpool(
            query,
            message,
            _to_agent_history(request.history),
        )
        return ChatResponse(
            answer=result["output"],
            sources=[
                _to_chat_source(source)
                for source in result.get("sources", [])
                if isinstance(source, dict)
            ],
            audit_id=result.get("audit_id"),
            governance_report=result.get("governance_report"),
        )
    except Exception:
        error_id = _new_error_id()
        logger.exception("POST /api/chat failed (error_id=%s)", error_id)
        return JSONResponse(
            status_code=500,
            content={
                "code": SAFE_ERROR_CODE,
                "message": SAFE_ERROR_MESSAGE,
                "error_id": error_id,
            },
        )


@app.post("/api/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Stream a chat response as newline-delimited JSON events."""
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    def event_stream():
        try:
            for event in stream_query(message, _to_agent_history(request.history)):
                # The agent yields raw source dicts (absolute `source` path
                # included); re-shape them onto the chat allowlist before they
                # leave the API.
                if event.get("type") == "sources":
                    event = {
                        "type": "sources",
                        "sources": [
                            _to_chat_source(source).model_dump()
                            for source in (event.get("sources") or [])
                            if isinstance(source, dict)
                        ],
                    }
                yield json.dumps(event, default=str) + "\n"
        except Exception:
            error_id = _new_error_id()
            logger.exception(
                "POST /api/chat/stream failed (error_id=%s)", error_id
            )
            yield json.dumps(
                {
                    "type": "error",
                    "code": SAFE_ERROR_CODE,
                    "message": SAFE_ERROR_MESSAGE,
                    "error_id": error_id,
                }
            ) + "\n"

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
    )
