"""Audit helpers for retrieved evidence and query traces."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config


SOURCE_HEADER_RE = re.compile(
    r"^\[Source (?P<rank>\d+): (?P<source>.+?)"
    r"(?:, page (?P<page>\d+))?"
    r"(?:, section: (?P<section>.*?)(?=, (?:company|type|year):|\]))?"
    r"(?:, [^\]]+)*"
    r"\]\n"
    r"(?P<content>.*)$",
    re.DOTALL,
)
MAX_TRACE_CONTENT_CHARS = 4000
MAX_EXCERPT_CHARS = 700


def _as_dict(value: Any) -> dict[str, Any]:
    """Convert LangChain-ish objects or dicts into a plain dictionary."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "dict"):
        return value.dict()
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return {
        key: getattr(value, key)
        for key in ("id", "name", "args", "type")
        if hasattr(value, key)
    }


def _source_path(raw_source: str) -> str:
    """Return a corpus-relative source path when possible."""
    try:
        path = Path(raw_source).expanduser().resolve()
        docs_dir = Path(config.DOCS_DIR).expanduser().resolve()
        return path.relative_to(docs_dir).as_posix()
    except (OSError, ValueError):
        return Path(raw_source).name


def parse_local_search_sources(tool_output: str) -> list[dict[str, Any]]:
    """Parse local_search output into chunk metadata for audit/display."""
    sources = []
    for block in tool_output.split("\n\n---\n\n"):
        match = SOURCE_HEADER_RE.match(block.strip())
        if not match:
            continue

        raw_source = match.group("source").strip()
        page = match.group("page")
        section = match.group("section")
        content = " ".join(match.group("content").split())
        source_path = _source_path(raw_source)
        record = {
            "rank": int(match.group("rank")),
            "source": raw_source,
            "source_name": Path(raw_source).name,
            "source_path": source_path,
            "page": int(page) if page is not None else None,
            "excerpt": content[:MAX_EXCERPT_CHARS],
        }
        if section:
            record["section_title"] = section.strip()
        sources.append(record)
    return sources


def _message_type(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("type", "message"))
    return getattr(message, "type", message.__class__.__name__.lower())


def _message_content(message: Any) -> str:
    if isinstance(message, dict):
        content = message.get("content", "")
        return content if isinstance(content, str) else json.dumps(content, default=str)
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, default=str)


def _tool_calls(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, dict):
        return [_as_dict(call) for call in message.get("tool_calls", [])]
    calls = getattr(message, "tool_calls", None) or []
    return [_as_dict(call) for call in calls]


def _tool_name(message: Any, tool_call_names: dict[str, str]) -> str | None:
    if isinstance(message, dict):
        name = message.get("name")
        if name:
            return str(name)
        tool_call_id = message.get("tool_call_id")
        if tool_call_id:
            return tool_call_names.get(tool_call_id)
        return None
    name = getattr(message, "name", None)
    if name:
        return name
    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id:
        return tool_call_names.get(tool_call_id)
    return None


def extract_retrieved_sources(messages: list[Any]) -> list[dict[str, Any]]:
    """Extract local retrieved chunks from LangChain message traces."""
    tool_call_names = {}
    for message in messages:
        for call in _tool_calls(message):
            call_id = call.get("id")
            call_name = call.get("name")
            if call_id and call_name:
                tool_call_names[call_id] = call_name

    sources = []
    seen = set()
    for message in messages:
        if _message_type(message) != "tool":
            continue
        if _tool_name(message, tool_call_names) != "local_search":
            continue
        for source in parse_local_search_sources(_message_content(message)):
            key = (
                source["source"],
                source["page"],
                source["excerpt"],
            )
            if key in seen:
                continue
            seen.add(key)
            source["rank"] = len(sources) + 1
            sources.append(source)
    return sources


def build_response_trace(messages: list[Any]) -> list[dict[str, Any]]:
    """Build a serializable trace of model/tool messages for audit logs."""
    tool_call_names = {}
    for message in messages:
        for call in _tool_calls(message):
            call_id = call.get("id")
            call_name = call.get("name")
            if call_id and call_name:
                tool_call_names[call_id] = call_name

    trace = []
    for message in messages:
        entry = {
            "type": _message_type(message),
            "content": _message_content(message)[:MAX_TRACE_CONTENT_CHARS],
        }
        calls = _tool_calls(message)
        if calls:
            entry["tool_calls"] = calls
        tool_name = _tool_name(message, tool_call_names)
        if tool_name:
            entry["tool_name"] = tool_name
        trace.append(entry)
    return trace


def build_audit_record(
    query: str,
    answer: str,
    messages: list[Any],
    retrieved_sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create a single JSON-serializable audit record."""
    audit_id = str(uuid.uuid4())
    return {
        "audit_id": audit_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "answer": answer,
        "retrieved_sources": retrieved_sources,
        "response_trace": build_response_trace(messages),
    }


def write_audit_record(
    record: dict[str, Any],
    audit_log_path: str | Path = config.AUDIT_LOG_PATH,
) -> str:
    """Append an audit record as JSONL and return its audit id."""
    path = Path(audit_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(record, default=str) + "\n")
    return str(record["audit_id"])
