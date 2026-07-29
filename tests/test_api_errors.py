"""Error-sanitization tests: raw exception details must never reach a chat
client, while the full exception stays available server-side via logging."""

import json
import logging
import re

from fastapi.testclient import TestClient

import api

client = TestClient(api.app)

# Values that must never appear in any client-visible byte.
_SECRET = "TOPSECRET-EXCEPTION-PAYLOAD-77"
_ABS_PATH = "/Users/wenhaohe/Desktop/ReAct-RAG/private/creds.txt"
_CRED_FRAGMENT = "AKIAFAKECREDFRAG99"
_BOOM = f"provider exploded: {_SECRET} while reading {_ABS_PATH} token={_CRED_FRAGMENT}"

_ERROR_ID_RE = re.compile(r"^err_[0-9a-f]{12}$")


def _stream_events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_stream_error_after_status_is_sanitized(monkeypatch, caplog):
    """A mid-stream dependency failure yields only the safe error contract."""

    def exploding_stream(message, history):
        yield {"type": "status", "message": "Searching local documents..."}
        raise RuntimeError(_BOOM)

    monkeypatch.setattr(api, "stream_query", exploding_stream)

    with caplog.at_level(logging.ERROR, logger="api"):
        response = client.post(
            "/api/chat/stream", json={"message": "hi", "history": []}
        )

    # Nothing secret in any client-visible byte.
    assert _SECRET not in response.text
    assert _ABS_PATH not in response.text
    assert _CRED_FRAGMENT not in response.text
    assert "RuntimeError" not in response.text

    events = _stream_events(response)
    # The status event sent before the failure still arrives.
    assert events[0] == {"type": "status", "message": "Searching local documents..."}

    error = events[-1]
    # Allowlisted fields only, stable code and message, opaque correlation id.
    assert set(error) == {"type", "code", "message", "error_id"}
    assert error["type"] == "error"
    assert error["code"] == api.SAFE_ERROR_CODE
    assert error["message"] == api.SAFE_ERROR_MESSAGE
    assert _ERROR_ID_RE.match(error["error_id"])

    # The full exception and the correlation id are captured server-side.
    assert _SECRET in caplog.text
    assert "RuntimeError" in caplog.text
    assert error["error_id"] in caplog.text
    assert "/api/chat/stream" in caplog.text


def test_stream_error_before_first_event_is_sanitized(monkeypatch, caplog):
    """A failure before the first agent event still produces the safe contract."""

    def exploding_stream(message, history):
        raise RuntimeError(_BOOM)
        yield  # pragma: no cover - makes this a generator function

    monkeypatch.setattr(api, "stream_query", exploding_stream)

    with caplog.at_level(logging.ERROR, logger="api"):
        response = client.post(
            "/api/chat/stream", json={"message": "hi", "history": []}
        )

    assert _SECRET not in response.text
    events = _stream_events(response)
    assert len(events) == 1
    assert events[0]["code"] == api.SAFE_ERROR_CODE
    assert _ERROR_ID_RE.match(events[0]["error_id"])
    assert _SECRET in caplog.text


def test_sync_chat_error_is_sanitized(monkeypatch, caplog):
    """The synchronous endpoint returns the stable 500 shape, never str(exc)."""

    def boom(*args, **kwargs):
        raise RuntimeError(_BOOM)

    monkeypatch.setattr(api, "query", boom)

    with caplog.at_level(logging.ERROR, logger="api"):
        response = client.post("/api/chat", json={"message": "hi", "history": []})

    assert response.status_code == 500
    assert _SECRET not in response.text
    assert _ABS_PATH not in response.text
    assert _CRED_FRAGMENT not in response.text

    body = response.json()
    assert set(body) == {"code", "message", "error_id"}
    assert body["code"] == api.SAFE_ERROR_CODE
    assert body["message"] == api.SAFE_ERROR_MESSAGE
    assert _ERROR_ID_RE.match(body["error_id"])

    assert _SECRET in caplog.text
    assert body["error_id"] in caplog.text
    assert "/api/chat" in caplog.text


def test_empty_message_still_returns_the_explicit_400(monkeypatch):
    """The pre-existing 400 for empty messages is not swallowed by the catch."""
    response = client.post("/api/chat", json={"message": "   ", "history": []})
    assert response.status_code == 400
    assert response.json()["detail"] == "Message cannot be empty."
