"""FastAPI backend for the React chat interface."""

import json
import mimetypes
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, StreamingResponse

from agent import detect_guardrail_intervention, query, stream_query
import config
from governance import review_queue
from loaders.registry import supported_extensions


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


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Answer a chat message using the existing RAG agent."""
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

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
        except Exception as exc:
            yield json.dumps(
                {
                    "type": "error",
                    "message": str(exc) or "The streaming request failed.",
                }
            ) + "\n"

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
    )
