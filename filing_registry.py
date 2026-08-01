"""Local filing registry: durable filing identity and ingestion outcomes.

Comparison roadmap step 2. This module gives the ingestion pipeline three
explicitly separated identities plus a durable record of what happened to every
attempted source. It exists so the future comparison workflow can reference two
distinct reporting periods of the same company without guessing.

Identity model (do not conflate these)
--------------------------------------
- ``filing_id`` — identity of ONE filing: ``slug(company_key):form_type:period_end``
  (e.g. ``acme-corporation:10-k:2025-12-31``). Deterministic from normalized
  identity fields, so re-ingesting the same filing yields the same id, and two
  filings of the same company/form with different periods get different ids.
  Only minted when explicit identity metadata (manifest) provides company,
  form type, and period_end — never derived from a filename or a random UUID.
- ``document_family_id`` — the pre-existing year-stripped lineage key
  (``ingest._document_id``): successive filings of one form share it. Used by
  the same-document impact workflow; it is NOT a filing identity. Chroma
  metadata keeps ``document_id`` as its legacy alias (same value).
- ``source_hash`` — sha256 of the file bytes (ingest's ``document_hash``):
  physical source identity, drives duplicate detection.
- chunk ids stay ``source_name:page:sha1(content)[:12]`` (ingest.chunk_id).

Filing identity metadata comes from the corpus manifest
(``corpus_manifest.yaml``, see load_manifest): ``period_end`` and
``filing_date`` are explicit-only values. They are never inferred from
filenames, body-text year guesses, or file mtimes; when the manifest does not
provide them they stay absent and the source ingests as a generic document
with no filing_id.

Ingestion outcomes (parse_status)
---------------------------------
Every attempted source records exactly one of:

- ``parsed``    — loaded and indexed. Same path re-ingested (same or changed
  content) refreshes this record; a content change with unchanged identity is
  a version update of the same filing.
- ``duplicate`` — identical content (same source_hash) already parsed under a
  different path. Not indexed: chunk ids embed the file name, so indexing the
  copy would double every chunk. ``duplicate_of`` names the canonical path.
- ``conflict``  — the derived filing_id is already held by a parsed entry with
  a different path AND different content. Reported explicitly, not indexed,
  and the existing entry is left untouched — same company/form/period with
  different content is a data problem a human must resolve.
- ``failed``    — the loader raised. Records a safe error code and a concise
  one-line summary (no traceback, no document content); full diagnostics stay
  in the CLI/server log.

Durability model: JSONL with full-file atomic replace (serialize in memory,
temp file in the same directory, flush+fsync, ``os.replace``) under one module
lock — the same lessons as governance/review_queue.py, with the same honest
limitation: single-process safety only. Cross-process writers (two concurrent
ingest runs) are not protected; acceptable for a local single-operator
prototype and fixed properly only by a real database.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

import config

PARSED = "parsed"
DUPLICATE = "duplicate"
FAILED = "failed"
CONFLICT = "conflict"
PARSE_STATUSES = (PARSED, DUPLICATE, FAILED, CONFLICT)

_REGISTRY_LOCK = threading.RLock()

# Manifest entries may carry exactly these keys. company_name, form_type, and
# period_end are required — a filing whose identity cannot be stated fully is
# a manifest error, caught before any indexing happens.
_MANIFEST_REQUIRED = ("company_name", "form_type", "period_end")
_MANIFEST_OPTIONAL = ("filing_date",)


class ManifestError(ValueError):
    """Invalid corpus manifest. Raised before any document is indexed."""


def normalize_form_type(value: str) -> str:
    """Canonical form-type token, matching ingest's inferred values ('10-k')."""
    return re.sub(r"\s+", "-", str(value).strip().lower())


def _company_slug(company_key: str) -> str:
    """company_key ('acme corporation') -> id-safe slug ('acme-corporation')."""
    return re.sub(r"\s+", "-", company_key.strip())


def filing_id_for(company_key: str, form_type: str, period_end: date) -> str:
    """Deterministic per-filing identity. Keeps the reporting period explicit."""
    return f"{_company_slug(company_key)}:{normalize_form_type(form_type)}:{period_end.isoformat()}"


def _require_date(value: Any, field: str, source: str) -> date:
    """Validate a manifest date (yaml parses ISO dates to datetime.date)."""
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        raise ManifestError(
            f"corpus manifest: {source}: '{field}' must be an ISO date "
            f"(YYYY-MM-DD), got {value!r}"
        )
    return value


def load_manifest(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load and validate the corpus manifest; {} when the file is absent.

    Returns a mapping of corpus-relative source_path -> validated entry with
    ``company_name`` (str), ``form_type`` (normalized str),
    ``period_end`` (date), and optional ``filing_date`` (date). A present but
    invalid manifest raises ManifestError so identity problems fail before
    indexing instead of minting wrong filing ids.
    """
    path = Path(path or config.CORPUS_MANIFEST_PATH)
    if not path.exists():
        return {}

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("filings", {}), dict):
        raise ManifestError(
            "corpus manifest: top level must be a mapping with a 'filings' mapping"
        )

    manifest: dict[str, dict[str, Any]] = {}
    for source_path, entry in (raw.get("filings") or {}).items():
        if not isinstance(entry, dict):
            raise ManifestError(
                f"corpus manifest: {source_path}: entry must be a mapping"
            )
        unknown = set(entry) - set(_MANIFEST_REQUIRED) - set(_MANIFEST_OPTIONAL)
        if unknown:
            raise ManifestError(
                f"corpus manifest: {source_path}: unknown keys {sorted(unknown)}"
            )
        missing = [
            key for key in _MANIFEST_REQUIRED if not entry.get(key)
        ]
        if missing:
            raise ManifestError(
                f"corpus manifest: {source_path}: missing required identity "
                f"fields {missing} — a filing's identity must be stated "
                "explicitly, not inferred"
            )

        validated = {
            "company_name": str(entry["company_name"]).strip(),
            "form_type": normalize_form_type(entry["form_type"]),
            "period_end": _require_date(entry["period_end"], "period_end", source_path),
        }
        if not validated["company_name"]:
            raise ManifestError(
                f"corpus manifest: {source_path}: company_name is empty"
            )
        if entry.get("filing_date") is not None:
            validated["filing_date"] = _require_date(
                entry["filing_date"], "filing_date", source_path
            )
        else:
            validated["filing_date"] = None
        manifest[str(source_path)] = validated
    return manifest


# --- Registry file I/O (review_queue atomic-write pattern) -------------------


def _read_entries(path: Path) -> list[dict[str, Any]]:
    """Read JSONL entries, returning [] when the file is absent."""
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def _atomic_write_entries(path: Path, entries: list[dict[str, Any]]) -> None:
    """Atomically replace the registry file (serialize -> temp -> fsync -> replace)."""
    lines = "".join(json.dumps(entry, default=str) + "\n" for entry in entries)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def resolve_outcome(
    source_path: str,
    source_hash: str,
    filing_id: str | None,
    registry_path: str | Path,
) -> tuple[str, dict[str, Any] | None]:
    """Decide the outcome for an incoming source against the current registry.

    Returns (status, related_entry): (PARSED, None) when the source may be
    indexed; (DUPLICATE, canonical_entry) when identical content is already
    parsed under a different path; (CONFLICT, holder_entry) when the filing_id
    is already held by a parsed entry with different path and content.

    The duplicate check runs first: identical content IS the same filing, so
    a renamed copy reports as a duplicate, never a conflict. Entries for the
    same source_path never block their own re-ingestion (same or changed
    content is a refresh / version update of that source).
    """
    others = [
        entry
        for entry in _read_entries(Path(registry_path))
        if entry.get("parse_status") == PARSED
        and entry.get("source_path") != source_path
    ]
    for entry in others:
        if entry.get("source_hash") == source_hash:
            return DUPLICATE, entry
    if filing_id:
        for entry in others:
            if entry.get("filing_id") == filing_id and entry.get("source_hash") != source_hash:
                return CONFLICT, entry
    return PARSED, None


def record_outcome(
    registry_path: str | Path,
    *,
    source_path: str,
    source_name: str,
    source_hash: str | None,
    parse_status: str,
    document_family_id: str | None = None,
    filing_id: str | None = None,
    company_key: str | None = None,
    company_name: str | None = None,
    form_type: str | None = None,
    period_end: str | None = None,
    filing_date: str | None = None,
    identity_source: str | None = None,
    loader: str | None = None,
    chunk_count: int | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
    duplicate_of: str | None = None,
    conflict_with: str | None = None,
) -> dict[str, Any]:
    """Upsert the outcome record for one source_path (latest attempt wins)."""
    if parse_status not in PARSE_STATUSES:
        raise ValueError(f"unknown parse_status: {parse_status!r}")

    record = {
        "source_path": source_path,
        "source_name": source_name,
        "source_hash": source_hash,
        "document_family_id": document_family_id,
        "filing_id": filing_id,
        "company_key": company_key,
        "company_name": company_name,
        "form_type": form_type,
        "period_end": period_end,
        "filing_date": filing_date,
        "identity_source": identity_source,
        "parse_status": parse_status,
        "loader": loader,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "chunk_count": chunk_count,
        "error_code": error_code,
        "error_summary": error_summary,
        "duplicate_of": duplicate_of,
        "conflict_with": conflict_with,
    }

    path = Path(registry_path)
    with _REGISTRY_LOCK:
        entries = [
            entry
            for entry in _read_entries(path)
            if entry.get("source_path") != source_path
        ]
        entries.append(record)
        _atomic_write_entries(path, entries)
    return record


def update_chunk_counts(
    counts_by_source_path: dict[str, int], registry_path: str | Path
) -> None:
    """Fill in chunk_count on parsed entries once their chunks are indexed.

    chunk_count is the completion marker, not a splitting statistic: the
    detector treats a filing as completely indexed only when this equals the
    number of chunks actually in the vector store. Callers must therefore write
    it *after* every vector-store batch has succeeded, never before (see
    clear_chunk_counts for the other half of that ordering).
    """
    path = Path(registry_path)
    with _REGISTRY_LOCK:
        entries = _read_entries(path)
        changed = False
        for entry in entries:
            if entry.get("parse_status") != PARSED:
                continue
            count = counts_by_source_path.get(entry.get("source_path"))
            if count is not None and entry.get("chunk_count") != count:
                entry["chunk_count"] = count
                changed = True
        if changed:
            _atomic_write_entries(path, entries)


def clear_chunk_counts(
    source_paths: Iterable[str], registry_path: str | Path
) -> None:
    """Drop the completion marker for sources about to be re-indexed.

    Vector-store writes are not transactional, so a run that dies partway must
    not leave a stale chunk_count from an earlier successful run standing as a
    completion claim over a half-written index. Clearing first and restoring
    only after every batch succeeds makes the incomplete state the durable one:
    a filing with chunk_count None is reported incomplete by the detector,
    which is the honest outcome until an explicit rerun finishes the upsert.

    Only parsed entries are touched, and only their chunk_count — identity,
    hashes, and status are untouched.
    """
    wanted = set(source_paths)
    if not wanted:
        return
    path = Path(registry_path)
    with _REGISTRY_LOCK:
        entries = _read_entries(path)
        changed = False
        for entry in entries:
            if entry.get("parse_status") != PARSED:
                continue
            if entry.get("source_path") in wanted and entry.get("chunk_count") is not None:
                entry["chunk_count"] = None
                changed = True
        if changed:
            _atomic_write_entries(path, entries)


# --- Queries -----------------------------------------------------------------


def list_entries(registry_path: str | Path) -> list[dict[str, Any]]:
    """Every outcome record (all statuses), in file order."""
    return _read_entries(Path(registry_path))


def list_filings(registry_path: str | Path) -> list[dict[str, Any]]:
    """Parsed entries with a filing identity, ordered by period_end."""
    filings = [
        entry
        for entry in _read_entries(Path(registry_path))
        if entry.get("parse_status") == PARSED and entry.get("filing_id")
    ]
    filings.sort(key=lambda entry: entry.get("period_end") or "")
    return filings


def get_filing(filing_id: str, registry_path: str | Path) -> dict[str, Any] | None:
    """Fetch one parsed filing entry by filing_id, or None."""
    for entry in list_filings(registry_path):
        if entry.get("filing_id") == filing_id:
            return entry
    return None


def find_filings(
    registry_path: str | Path,
    *,
    company_key: str | None = None,
    form_type: str | None = None,
) -> list[dict[str, Any]]:
    """Parsed filings matching the given identity filters, by period_end."""
    form_type = normalize_form_type(form_type) if form_type else None
    return [
        entry
        for entry in list_filings(registry_path)
        if (company_key is None or entry.get("company_key") == company_key)
        and (form_type is None or entry.get("form_type") == form_type)
    ]


def chronological_candidates(
    company_key: str, form_type: str, registry_path: str | Path
) -> list[dict[str, Any]]:
    """Same-company same-form filings ordered by period_end (comparison pairs).

    Adjacent elements are (previous, current) candidates; the ordering key is
    the explicit period_end, never a filename year.
    """
    return find_filings(
        registry_path, company_key=company_key, form_type=form_type
    )


def to_filing_reference(entry: dict[str, Any]):
    """Build a validated comparison.v1 FilingReference from a parsed entry.

    Every identity field comes straight from the registry record — nothing is
    guessed. Raises ValueError when the entry is not a successfully parsed
    filing (no filing identity, or a non-parsed outcome).
    """
    from governance.comparison_schema import FilingReference

    if entry.get("parse_status") != PARSED or not entry.get("filing_id"):
        raise ValueError(
            "FilingReference requires a parsed registry entry with a filing "
            f"identity; got parse_status={entry.get('parse_status')!r}, "
            f"filing_id={entry.get('filing_id')!r}"
        )
    source_hash = entry.get("source_hash") or ""
    return FilingReference(
        document_id=entry["filing_id"],
        company_key=entry["company_key"],
        company_name=entry["company_name"],
        form_type=entry["form_type"],
        filing_date=entry.get("filing_date"),
        period_end=entry["period_end"],
        source_name=entry["source_name"],
        version_hash=source_hash[:12] or None,
    )
