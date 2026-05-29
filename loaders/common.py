"""Shared helpers across loader handlers."""

from __future__ import annotations

import re
from pathlib import Path


# Acronyms we want surfaced in uppercase when humanizing a filename stem.
_UPPER_ACRONYMS = {
    "kyc",
    "sec",
    "esg",
    "kpi",
    "aml",
    "mnpi",
    "10k",
    "10q",
    "8k",
    "xbrl",
    "id",
    "ids",
    "us",
    "usd",
}


def humanize_stem(stem_or_path: str | Path) -> str:
    """Convert ``kyc-audit-trail-2026`` to ``KYC Audit Trail``.

    Drops pure year tokens so the resulting label stays generic across reruns.
    """
    stem = Path(stem_or_path).stem if isinstance(stem_or_path, Path) else Path(stem_or_path).stem
    parts = [p for p in re.split(r"[-_\s]+", stem) if p]
    parts = [p for p in parts if not re.fullmatch(r"20\d{2}", p)]
    if not parts:
        return Path(stem_or_path).name
    return " ".join(
        p.upper() if p.lower() in _UPPER_ACRONYMS else p.capitalize() for p in parts
    )
