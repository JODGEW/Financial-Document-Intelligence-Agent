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
    """Retrieved chunk metadata exposed for auditability in the UI."""

    rank: int
    source: str
    source_name: str
    source_path: str
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
        sources=result.get("sources", []),
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
