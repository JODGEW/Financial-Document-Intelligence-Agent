"""Tests for the ingest-time PII redaction module."""

from __future__ import annotations

import pytest

from loaders.pii import redact, should_redact
from loaders.registry import FormatHandler


def _stub_handler(family: str) -> FormatHandler:
    return FormatHandler(
        extensions=(".__stub__",),
        loader=lambda _path: [],
        format_family=family,  # type: ignore[arg-type]
    )


def test_redacts_email():
    text = "Contact analyst alice.smith+filing@example.com for details."
    redacted, counts = redact(text)
    assert "[REDACTED:EMAIL]" in redacted
    assert "alice.smith+filing@example.com" not in redacted
    assert counts == {"email": 1}


def test_redacts_ssn_phone_card_with_counts():
    text = (
        "SSN 123-45-6789, primary 415-555-2034, "
        "card 4111 1111 1111 1111 on file."
    )
    redacted, counts = redact(text)
    assert "[REDACTED:SSN]" in redacted
    assert "[REDACTED:PHONE]" in redacted
    assert "[REDACTED:CARD]" in redacted
    assert counts.get("ssn") == 1
    assert counts.get("phone") == 1
    assert counts.get("card") == 1


def test_redact_returns_unchanged_text_when_no_pii():
    text = "Quarterly revenue rose to $284.7 million in FY2025."
    redacted, counts = redact(text)
    assert redacted == text
    assert counts == {}


def test_should_redact_global_flag_overrides_family():
    """Global flag forces redaction even on free-text formats."""
    text_handler = _stub_handler("text")
    assert should_redact(text_handler, global_flag=True, tabular_flag=False)
    assert should_redact(text_handler, global_flag=True, tabular_flag=True)


def test_should_redact_tabular_flag_targets_tabular_only():
    """Tabular flag fires for tabular handlers and is a no-op for text."""
    text_handler = _stub_handler("text")
    tabular_handler = _stub_handler("tabular")

    assert should_redact(tabular_handler, global_flag=False, tabular_flag=True)
    assert not should_redact(text_handler, global_flag=False, tabular_flag=True)


def test_should_redact_both_flags_off_returns_false():
    text_handler = _stub_handler("text")
    tabular_handler = _stub_handler("tabular")

    assert not should_redact(text_handler, global_flag=False, tabular_flag=False)
    assert not should_redact(tabular_handler, global_flag=False, tabular_flag=False)


def test_should_redact_handles_none_handler():
    """Unknown extension (handler=None) must not crash dispatch."""
    assert should_redact(None, global_flag=False, tabular_flag=True) is False
    assert should_redact(None, global_flag=True, tabular_flag=False) is True
