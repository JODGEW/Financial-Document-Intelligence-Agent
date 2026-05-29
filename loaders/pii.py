"""PII redaction at ingestion time.

Two-tier dispatch (see MULTI_FORMAT_PROGRESS.md decisions log):
- ``PII_REDACT_AT_INGEST`` (global, default false): when true, redact every
  document regardless of format.
- ``PII_REDACT_TABULAR_AT_INGEST`` (default true): when true, redact only
  documents whose handler has ``format_family == "tabular"`` (CSV / XLSX).

Patterns mirror the types declared in ``policies/guardrails-policy.md`` so
ingest-time redaction and Bedrock guardrail anonymization stay aligned.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import FormatHandler


EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
PHONE_RE = re.compile(
    r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)


# Order matters: CARD before PHONE so a 16-digit card is not partially
# consumed as a phone number. SSN is distinct (3-2-4) so order is safe.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", EMAIL_RE),
    ("SSN", SSN_RE),
    ("CARD", CARD_RE),
    ("PHONE", PHONE_RE),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Replace PII matches with ``[REDACTED:TYPE]``. Returns (text, counts)."""
    counts: dict[str, int] = {}
    for label, pattern in PATTERNS:
        text, n = pattern.subn(f"[REDACTED:{label}]", text)
        if n:
            counts[label.lower()] = n
    return text, counts


def should_redact(
    handler: "FormatHandler | None",
    *,
    global_flag: bool,
    tabular_flag: bool,
) -> bool:
    """Two-tier dispatch encoding the project's PII policy.

    - global_flag wins for any handler.
    - tabular_flag applies only when handler.format_family == "tabular".
    """
    if global_flag:
        return True
    if tabular_flag and handler is not None and handler.format_family == "tabular":
        return True
    return False
