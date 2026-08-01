"""Controlled real-public-filing benchmark: schemas, layout, and hashing.

Stage 3.5 step 9. The synthetic comparison regression suite proves that the
Item 1A workflow's deterministic contracts do not drift. It proves nothing
about real filing structure, formatting, section variation, or issuer
language. This module is the offline, credential-free spine of a controlled
benchmark that can eventually say something about the latter — and, until a
human has actually reviewed labels, is built to refuse to say it.

The one rule this module exists to enforce
------------------------------------------
A machine-proposed label NEVER enters a gold metric denominator. Machine
proposals are written with ``annotation_status='machine_proposed'`` and no
annotator identity; only a file a human explicitly moved to ``human_verified``
with an annotator id and a verification timestamp is gold. That boundary is
checked structurally (a status/identity invariant in ``validate_annotation``),
not by convention, and again at evaluation time.

What lives here
---------------
- The frozen benchmark manifest schema (``real-filing-benchmark.manifest.v1``):
  exact keys, deterministic ordering, canonical official-source URLs,
  chronology, issuer identity, form restriction, and the four-state maturity
  ladder (proposed -> source_verified -> corpus_built ->
  human_annotation_complete).
- The versioned annotation schema
  (``real-filing-benchmark.annotation.v1``) and its bindings to section
  hashes and extracted unit ids, so changed source text invalidates labels
  rather than silently re-using them.
- The gitignored local corpus layout, and the rule that everything large —
  downloaded documents, normalized text, extracted sections — stays inside it.
- Deterministic hashing for sources, sections, units, manifests, and
  annotations.

What deliberately does NOT live here
------------------------------------
No network code (see ``real_filing_acquisition``), no detector changes, no
heading-alignment changes, no validator changes, no governance thresholds. The
benchmark measures the existing workflow; it does not adjust it. This module
imports nothing that can open a socket, which ``tests/`` asserts at the import
graph rather than in prose.

Honesty boundaries stated once, enforced everywhere below
---------------------------------------------------------
- The corpus is controlled and intentionally small. It is NOT a statistically
  representative sample of SEC filings, issuers, or filing formats.
- No accuracy claim about real filings exists until a human has verified
  labels. Every unlabeled artifact says so in its own payload.
- The synthetic regression suite remains the merge-blocking deterministic
  gate. Nothing here changes it, its baseline, or its hashes.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import config

# --- Versions ----------------------------------------------------------------

MANIFEST_SCHEMA_VERSION = "real-filing-benchmark.manifest.v1"
ANNOTATION_SCHEMA_VERSION = "real-filing-benchmark.annotation.v1"
ANNOTATION_PROTOCOL_VERSION = "real-filing-annotation.v1"
SELECTION_PROTOCOL_VERSION = "real-filing-selection.v1"
#: v2 adds the bounded Item-extraction diagnostics produced by the SEC HTML
#: heading parser (candidate counts, rejection counts, reason code, boundary
#: heading, parser version). Source hashes and the manifest schema are
#: untouched by the bump; only build records gain fields.
BUILD_RECORD_VERSION = "real-filing-benchmark.build.v2"
PACKET_SCHEMA_VERSION = "real-filing-benchmark.packet.v1"
BUILDER_VERSION = "real_filing_benchmark_builder.v1"
METRIC_DEFINITIONS_VERSION = "real-filing-benchmark-metrics.v1"

BENCHMARK_ID = "real_filing_v1"

# --- Manifest maturity ladder -------------------------------------------------
# A manifest may not advance to a later status without the artifacts that
# status asserts exist. Advancing is an explicit human act (a reviewed commit),
# never a side effect of running a tool.

STATUS_PROPOSED = "proposed"
STATUS_SOURCE_VERIFIED = "source_verified"
STATUS_CORPUS_BUILT = "corpus_built"
STATUS_HUMAN_ANNOTATION_COMPLETE = "human_annotation_complete"
STATUS_ORDER = (
    STATUS_PROPOSED,
    STATUS_SOURCE_VERIFIED,
    STATUS_CORPUS_BUILT,
    STATUS_HUMAN_ANNOTATION_COMPLETE,
)

# --- Corpus role --------------------------------------------------------------
#
# Whether a corpus may support a generalization claim depends on one fact: was
# it looked at while the code being evaluated was written? The twenty
# real_filings_v1 filings were inspected to diagnose HTML structure and design
# the SEC Item heading parser (sec_html_item_headings.v2). That makes them a
# DEVELOPMENT corpus. Its extraction numbers are in-sample and describe the
# parser's fit to documents it was built against — not its behavior on unseen
# filings.
#
# This is recorded as machine-readable metadata rather than prose alone,
# because a reader scanning a JSON report for "20/20 extracted" must not be
# able to miss it.

#: Inspected while the extraction parser was developed. In-sample.
CORPUS_ROLE_EXTRACTION_DEVELOPMENT = "extraction_development_corpus"
#: Frozen and unseen until after the extraction parser was frozen. Out-of-sample.
CORPUS_ROLE_EXTRACTION_HOLDOUT = "extraction_holdout_corpus"
CORPUS_ROLES = (
    CORPUS_ROLE_EXTRACTION_DEVELOPMENT,
    CORPUS_ROLE_EXTRACTION_HOLDOUT,
)

#: The role of the corpus committed in this repository today. Changing this
#: requires a corpus that was genuinely unseen during parser development —
#: not a re-description of this one.
REAL_FILINGS_V1_CORPUS_ROLE = CORPUS_ROLE_EXTRACTION_DEVELOPMENT


# --- Annotation vocabulary ----------------------------------------------------

ANNOTATION_UNREVIEWED = "unreviewed"
ANNOTATION_MACHINE_PROPOSED = "machine_proposed"
ANNOTATION_HUMAN_IN_PROGRESS = "human_in_progress"
ANNOTATION_HUMAN_VERIFIED = "human_verified"
ANNOTATION_REJECTED = "rejected"
ANNOTATION_STATUSES = (
    ANNOTATION_UNREVIEWED,
    ANNOTATION_MACHINE_PROPOSED,
    ANNOTATION_HUMAN_IN_PROGRESS,
    ANNOTATION_HUMAN_VERIFIED,
    ANNOTATION_REJECTED,
)

# Statuses that must carry NO human annotator identity: nothing a machine
# produced may be attributed to a person.
MACHINE_ONLY_STATUSES = (ANNOTATION_UNREVIEWED, ANNOTATION_MACHINE_PROPOSED)
# Statuses that require an explicit human annotator id AND timestamp.
HUMAN_ATTRIBUTED_STATUSES = (ANNOTATION_HUMAN_VERIFIED, ANNOTATION_REJECTED)
# The ONLY status whose labels may enter a gold metric denominator.
GOLD_STATUS = ANNOTATION_HUMAN_VERIFIED

EXPECTED_CHANGE_TYPES = (
    "added",
    "removed",
    "modified",
    "unchanged",
    "undetermined",
)
EVIDENCE_SIDES = ("previous", "current", "both", "none")
DIRECTIONS = ("increased", "decreased", "unchanged")
CONFIDENCE_LEVELS = ("high", "medium", "low")

# --- Corpus-build extraction outcomes ----------------------------------------

EXTRACTION_EXTRACTED = "extracted"
EXTRACTION_MISSING = "missing"
EXTRACTION_AMBIGUOUS = "ambiguous"
EXTRACTION_PARSE_FAILED = "parse_failed"
EXTRACTION_OUTCOMES = (
    EXTRACTION_EXTRACTED,
    EXTRACTION_MISSING,
    EXTRACTION_AMBIGUOUS,
    EXTRACTION_PARSE_FAILED,
)

# --- Bounds ------------------------------------------------------------------
# Committed artifacts carry metadata, never filing text. These caps apply to
# the LOCAL packets too: a reviewer needs enough context to judge a unit, not
# the section.

MAX_REVIEWER_NOTE_CHARS = 500
MAX_EXCERPT_CHARS = 400
MAX_HEADING_CHARS = 200
MAX_ANNOTATOR_ID_CHARS = 120

# --- Selection protocol -------------------------------------------------------
# Frozen BEFORE any detector output is observed. Recorded here so the rules a
# reader can check are the rules the corpus was built under.

SELECTION_CRITERIA = {
    "protocol_version": SELECTION_PROTOCOL_VERSION,
    "form": "10-K",
    "target_pair_count": 10,
    "target_filing_count": 20,
    "minimum_sector_labels": 5,
    "inclusion": [
        "Exactly one issuer per pair, identified by CIK.",
        "Both filings of a pair are the same issuer and the same document "
        "family (consecutive annual 10-K filings).",
        "Exact accession numbers, official filing dates, and official "
        "reporting periods, all resolved from official SEC endpoints.",
        "The primary 10-K document only — never a summary, exhibit, or "
        "third-party copy.",
        "The issuer slate is frozen before any filing is fetched or any "
        "detector output is observed.",
    ],
    "exclusion": [
        "10-K/A amendments are excluded from v1. An amendment case, if ever "
        "wanted, is a separate explicitly created case, not a substitution.",
        "A filing that is missing or inaccessible at acquisition time is "
        "excluded BEFORE the manifest is frozen, never after.",
        "A pair is never replaced, reordered, or dropped after observing "
        "detector results. A difficult pair stays in the corpus.",
    ],
    "stratification": (
        "sector_label is benchmark stratification metadata recorded at "
        "selection time. It is never inferred, looked up, or recomputed "
        "during evaluation, and it is not used to weight any metric."
    ),
    "representativeness": (
        "This corpus is controlled and intentionally small. It is NOT a "
        "statistically representative sample of SEC filings, issuers, "
        "industries, or filing formats, and no metric computed over it may "
        "be presented as one."
    ),
}


# --- Errors ------------------------------------------------------------------


class BenchmarkError(Exception):
    """Base for benchmark conditions. ``code`` is stable and safe to display."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ManifestSchemaError(BenchmarkError):
    """The benchmark manifest is invalid, incomplete, or self-contradictory."""


class AnnotationSchemaError(BenchmarkError):
    """An annotation file is invalid, or claims an identity it cannot have."""


class CorpusDriftError(BenchmarkError):
    """Local artifacts no longer match the inputs they were derived from."""


class StatusTransitionError(BenchmarkError):
    """A manifest status change is not a legal single forward step."""


class CorpusRoleError(BenchmarkError):
    """A corpus was described with a role outside the closed set."""


def corpus_role_fields(role: str = REAL_FILINGS_V1_CORPUS_ROLE) -> dict[str, Any]:
    """Bounded corpus-validity block embedded in every benchmark report.

    One source of truth for the build report, the evaluation report, and the
    packet inventory, so the three cannot disagree about what the corpus is.
    """
    _require(
        role in CORPUS_ROLES,
        CorpusRoleError,
        "corpus_role_unknown",
        f"corpus_role must be one of {list(CORPUS_ROLES)}, got {role!r}",
    )
    development = role == CORPUS_ROLE_EXTRACTION_DEVELOPMENT
    return {
        "corpus_role": role,
        "extraction_parser_developed_using_this_corpus": development,
        "extraction_holdout_evaluation": not development,
        # A generalization claim needs an out-of-sample corpus AND verified
        # labels. Neither exists today, so this stays false until a holdout
        # corpus is frozen, annotated, and evaluated.
        "generalization_claim_supported": False,
        "corpus_role_detail": (
            "The twenty source documents in this corpus were inspected to "
            "diagnose HTML structure while the SEC Item heading parser was "
            "developed. Extraction results over them are IN-SAMPLE DEVELOPMENT "
            "results and are not evidence that extraction generalizes to "
            "unseen filings. A separately frozen holdout corpus, selected only "
            "after the extraction parser is frozen, is required for that."
            if development
            else "This corpus was frozen and unseen until after the extraction "
            "parser was frozen, so extraction results over it are "
            "out-of-sample."
        ),
    }


# --- Small validation helpers -------------------------------------------------


def _require(condition: bool, error, code: str, message: str) -> None:
    if not condition:
        raise error(code, message)


def _exact_keys(
    mapping: Any,
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
    where: str,
    error,
    code_prefix: str,
) -> None:
    """Reject unknown keys and missing required keys, naming both."""
    _require(
        isinstance(mapping, dict),
        error,
        f"{code_prefix}_not_a_mapping",
        f"{where}: expected a JSON object",
    )
    unknown = sorted(set(mapping) - set(required) - set(optional))
    _require(
        not unknown,
        error,
        f"{code_prefix}_unknown_keys",
        f"{where}: unknown keys {unknown}",
    )
    missing = sorted(key for key in required if key not in mapping)
    _require(
        not missing,
        error,
        f"{code_prefix}_missing_keys",
        f"{where}: missing required keys {missing}",
    )


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CIK_RE = re.compile(r"^\d{10}$")
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_ID_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PRIMARY_DOC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_PRIMARY_DOC_SUFFIXES = (".htm", ".html", ".txt", ".pdf")

# A hash of all zeros is the only accepted placeholder, and only while the
# manifest is 'proposed'. Anything else that is not a real digest is an error,
# so a typo can never masquerade as a pending value.
PLACEHOLDER_SHA256 = "0" * 64


def _require_iso_date(value: Any, where: str, error, code_prefix: str) -> date:
    _require(
        isinstance(value, str) and bool(_ISO_DATE_RE.match(value)),
        error,
        f"{code_prefix}_invalid_date",
        f"{where}: expected an ISO date (YYYY-MM-DD), got {value!r}",
    )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise error(
            f"{code_prefix}_invalid_date", f"{where}: {exc}"
        ) from exc


def _require_iso_timestamp(value: Any, where: str, error, code_prefix: str) -> datetime:
    _require(
        isinstance(value, str) and bool(value.strip()),
        error,
        f"{code_prefix}_invalid_timestamp",
        f"{where}: expected an ISO-8601 UTC timestamp, got {value!r}",
    )
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise error(
            f"{code_prefix}_invalid_timestamp",
            f"{where}: {value!r} is not an ISO-8601 timestamp",
        ) from exc
    _require(
        moment.tzinfo is not None,
        error,
        f"{code_prefix}_naive_timestamp",
        f"{where}: timestamp must carry an explicit UTC offset",
    )
    return moment


def _require_bounded_str(
    value: Any, where: str, *, max_chars: int, error, code_prefix: str
) -> str:
    _require(
        isinstance(value, str) and bool(value.strip()),
        error,
        f"{code_prefix}_invalid_text",
        f"{where}: expected a non-empty string",
    )
    _require(
        len(value) <= max_chars,
        error,
        f"{code_prefix}_text_too_long",
        f"{where}: exceeds {max_chars} characters",
    )
    return value.strip()


# --- Hashing ------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    """Streamed sha256 of a local file (filings can be large)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_text(text: str) -> str:
    """Whitespace-collapsed text — the same normalization the detector uses.

    Section and unit hashes are computed over this form so an insignificant
    whitespace difference between two renderings of the same section does not
    invalidate a human's labels, while any real text change does.
    """
    return " ".join((text or "").split())


def section_hash(text: str) -> str:
    """Deterministic hash binding annotations to exact section content."""
    return sha256_bytes(normalize_text(text).encode("utf-8"))


def canonical_json(payload: Any) -> str:
    """Stable JSON for hashing: sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def payload_hash(payload: Any) -> str:
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def unit_id(side: str, index: int, unit_key: str) -> str:
    """Deterministic, human-readable id for one extracted risk-factor unit.

    The position index is part of the id because a filing may legitimately
    repeat a normalized heading (the detector reports that as ambiguous). Two
    units are then distinguishable in a packet and in a label, instead of
    silently colliding.
    """
    if side not in ("previous", "current"):
        raise ValueError(f"unknown side: {side!r}")
    return f"{side}:{index:03d}:{unit_key}"


def label_id_for(
    pair_id: str, previous_unit_id: str | None, current_unit_id: str | None
) -> str:
    """Deterministic label id from the units a label binds together."""
    material = f"{pair_id}|{previous_unit_id or ''}|{current_unit_id or ''}"
    return "lbl-" + hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]


# --- Official source URLs -----------------------------------------------------

SEC_ARCHIVES_HOST = "www.sec.gov"
SEC_DATA_HOST = "data.sec.gov"
# Exact-match hosts only. Suffix matching would admit `www.sec.gov.evil.test`,
# so the acquisition module compares hostnames for equality against this tuple.
OFFICIAL_SEC_HOSTS = (SEC_ARCHIVES_HOST, SEC_DATA_HOST)


def canonical_source_url(cik: str, accession_number: str, primary_document: str) -> str:
    """The one official EDGAR URL for a filing's primary document.

    Deriving it rather than trusting the manifest's string is what makes
    "official source only" checkable: a manifest URL is valid iff it equals
    this value, so no mirror, cache, aggregator, or look-alike host can be
    named in a frozen manifest.
    """
    return (
        f"https://{SEC_ARCHIVES_HOST}/Archives/edgar/data/"
        f"{int(cik)}/{accession_number.replace('-', '')}/{primary_document}"
    )


def canonical_submissions_url(cik: str) -> str:
    """The official submissions-metadata endpoint for one issuer."""
    return f"https://{SEC_DATA_HOST}/submissions/CIK{cik}.json"


# --- Manifest -----------------------------------------------------------------

_MANIFEST_REQUIRED = (
    "schema_version",
    "benchmark_id",
    "benchmark_version",
    "frozen_at",
    "selection_protocol_version",
    "status",
    "form",
    "target_pair_count",
    "proposed_issuers",
    "pairs",
)
_MANIFEST_OPTIONAL = ("description",)

_ISSUER_REQUIRED = (
    "slate_id",
    "issuer_name",
    "sector_label",
    "cik",
    "target_previous_fiscal_year",
    "target_current_fiscal_year",
    "resolution_status",
)

_PAIR_REQUIRED = (
    "pair_id",
    "slate_id",
    "issuer_name",
    "cik",
    "sector_label",
    "previous",
    "current",
)

_SIDE_REQUIRED = (
    "accession_number",
    "form",
    "filing_date",
    "reporting_period",
    "primary_document",
    "official_source_url",
    "expected_sha256",
)

ISSUER_PENDING = "pending_official_lookup"
ISSUER_RESOLVED = "resolved_from_official_source"
ISSUER_RESOLUTION_STATUSES = (ISSUER_PENDING, ISSUER_RESOLVED)

MANIFEST_FORM = "10-K"


def default_manifest_path() -> Path:
    return Path(config.REAL_FILING_BENCHMARK_MANIFEST)


def load_manifest(path: str | Path | None = None) -> dict[str, Any]:
    """Read and fully validate the frozen benchmark manifest."""
    path = Path(path or default_manifest_path())
    if not path.exists():
        raise ManifestSchemaError(
            "manifest_not_found", f"benchmark manifest not found: {path.name}"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestSchemaError(
            "manifest_not_json", f"{path.name}: {exc}"
        ) from exc
    validate_manifest(document)
    return document


def manifest_hash(path: str | Path | None = None) -> str:
    """sha256 over the manifest's exact bytes: the join key for every report."""
    return sha256_file(Path(path or default_manifest_path()))


def validate_manifest(document: Any) -> None:
    """Reject every documented manifest defect.

    Deliberately strict: this file is the pre-registration of what will be
    measured, so a defect here has to be a loud failure rather than a quiet
    coercion.
    """
    _exact_keys(
        document,
        required=_MANIFEST_REQUIRED,
        optional=_MANIFEST_OPTIONAL,
        where="manifest",
        error=ManifestSchemaError,
        code_prefix="manifest",
    )
    _require(
        document["schema_version"] == MANIFEST_SCHEMA_VERSION,
        ManifestSchemaError,
        "manifest_schema_version_mismatch",
        f"manifest: schema_version must be {MANIFEST_SCHEMA_VERSION!r}, got "
        f"{document['schema_version']!r}",
    )
    _require(
        isinstance(document["benchmark_id"], str)
        and bool(_ID_RE.match(document["benchmark_id"])),
        ManifestSchemaError,
        "manifest_invalid_benchmark_id",
        "manifest: benchmark_id must be a lowercase slug",
    )
    _require(
        isinstance(document["benchmark_version"], int)
        and not isinstance(document["benchmark_version"], bool)
        and document["benchmark_version"] >= 1,
        ManifestSchemaError,
        "manifest_invalid_benchmark_version",
        "manifest: benchmark_version must be an integer >= 1",
    )
    _require_iso_timestamp(
        document["frozen_at"], "manifest.frozen_at", ManifestSchemaError, "manifest"
    )
    _require(
        document["selection_protocol_version"] == SELECTION_PROTOCOL_VERSION,
        ManifestSchemaError,
        "manifest_invalid_selection_protocol",
        f"manifest: selection_protocol_version must be "
        f"{SELECTION_PROTOCOL_VERSION!r}",
    )
    _require(
        document["status"] in STATUS_ORDER,
        ManifestSchemaError,
        "manifest_invalid_status",
        f"manifest: status must be one of {list(STATUS_ORDER)}, got "
        f"{document['status']!r}",
    )
    _require(
        document["form"] == MANIFEST_FORM,
        ManifestSchemaError,
        "manifest_invalid_form",
        f"manifest: v1 covers form {MANIFEST_FORM!r} only",
    )
    _require(
        isinstance(document["target_pair_count"], int)
        and not isinstance(document["target_pair_count"], bool)
        and document["target_pair_count"] >= 1,
        ManifestSchemaError,
        "manifest_invalid_target_pair_count",
        "manifest: target_pair_count must be an integer >= 1",
    )
    if "description" in document:
        _require_bounded_str(
            document["description"],
            "manifest.description",
            max_chars=2000,
            error=ManifestSchemaError,
            code_prefix="manifest",
        )

    slate = _validate_issuer_slate(document)
    _validate_pairs(document, slate)


def _validate_issuer_slate(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The frozen pre-registration: who will be measured, decided up front."""
    issuers = document["proposed_issuers"]
    _require(
        isinstance(issuers, list),
        ManifestSchemaError,
        "manifest_issuers_not_a_list",
        "manifest: proposed_issuers must be a list",
    )
    _require(
        len(issuers) == document["target_pair_count"],
        ManifestSchemaError,
        "manifest_issuer_count_mismatch",
        f"manifest: proposed_issuers must hold exactly target_pair_count "
        f"({document['target_pair_count']}) entries, got {len(issuers)}",
    )

    slate: dict[str, dict[str, Any]] = {}
    names: set[str] = set()
    ciks: set[str] = set()
    sectors: set[str] = set()
    previous_slate_id = ""
    for index, issuer in enumerate(issuers):
        where = f"manifest.proposed_issuers[{index}]"
        _exact_keys(
            issuer,
            required=_ISSUER_REQUIRED,
            where=where,
            error=ManifestSchemaError,
            code_prefix="manifest_issuer",
        )
        slate_id = issuer["slate_id"]
        _require(
            isinstance(slate_id, str) and bool(_ID_RE.match(slate_id)),
            ManifestSchemaError,
            "manifest_issuer_invalid_slate_id",
            f"{where}: slate_id must be a lowercase slug",
        )
        _require(
            slate_id not in slate,
            ManifestSchemaError,
            "manifest_issuer_duplicate_slate_id",
            f"{where}: duplicate slate_id {slate_id!r}",
        )
        # Deterministic ordering: the slate is frozen, so its order is part of
        # the contract and cannot drift between commits.
        _require(
            slate_id > previous_slate_id,
            ManifestSchemaError,
            "manifest_issuers_unordered",
            f"{where}: proposed_issuers must be sorted by slate_id "
            f"({slate_id!r} follows {previous_slate_id!r})",
        )
        previous_slate_id = slate_id

        name = _require_bounded_str(
            issuer["issuer_name"],
            f"{where}.issuer_name",
            max_chars=200,
            error=ManifestSchemaError,
            code_prefix="manifest_issuer",
        )
        _require(
            name not in names,
            ManifestSchemaError,
            "manifest_issuer_duplicate_name",
            f"{where}: issuer_name {name!r} appears more than once — v1 uses "
            "one issuer per pair",
        )
        names.add(name)
        sector = _require_bounded_str(
            issuer["sector_label"],
            f"{where}.sector_label",
            max_chars=100,
            error=ManifestSchemaError,
            code_prefix="manifest_issuer",
        )
        sectors.add(sector)

        for field in ("target_previous_fiscal_year", "target_current_fiscal_year"):
            value = issuer[field]
            _require(
                isinstance(value, int)
                and not isinstance(value, bool)
                and 1990 <= value <= 2100,
                ManifestSchemaError,
                "manifest_issuer_invalid_fiscal_year",
                f"{where}.{field}: expected a four-digit fiscal year",
            )
        _require(
            issuer["target_current_fiscal_year"]
            == issuer["target_previous_fiscal_year"] + 1,
            ManifestSchemaError,
            "manifest_issuer_not_consecutive",
            f"{where}: v1 pairs are CONSECUTIVE annual filings; "
            f"{issuer['target_previous_fiscal_year']} -> "
            f"{issuer['target_current_fiscal_year']} is not",
        )
        _require(
            issuer["resolution_status"] in ISSUER_RESOLUTION_STATUSES,
            ManifestSchemaError,
            "manifest_issuer_invalid_resolution_status",
            f"{where}: resolution_status must be one of "
            f"{list(ISSUER_RESOLUTION_STATUSES)}",
        )

        cik = issuer["cik"]
        if issuer["resolution_status"] == ISSUER_PENDING:
            # CIK is remote metadata. While a slate entry is pending, it MUST
            # be absent rather than recalled, guessed, or approximated — a
            # wrong identifier in a frozen manifest is a fabricated fact.
            _require(
                cik is None,
                ManifestSchemaError,
                "manifest_issuer_pending_with_cik",
                f"{where}: a pending issuer must carry cik=null until it is "
                "resolved from an official SEC source",
            )
        else:
            _require(
                isinstance(cik, str) and bool(_CIK_RE.match(cik)),
                ManifestSchemaError,
                "manifest_issuer_invalid_cik",
                f"{where}: cik must be a 10-digit zero-padded string",
            )
            _require(
                cik not in ciks,
                ManifestSchemaError,
                "manifest_issuer_duplicate_cik",
                f"{where}: duplicate cik {cik!r}",
            )
            ciks.add(cik)
        slate[slate_id] = issuer

    # Proportional on purpose: v1's ten-pair corpus must span at least five
    # sectors, but a smaller benchmark cannot span more sectors than it has
    # issuers, and pretending otherwise would make the rule unsatisfiable
    # rather than meaningful.
    required_sectors = min(
        SELECTION_CRITERIA["minimum_sector_labels"], document["target_pair_count"]
    )
    _require(
        len(sectors) >= required_sectors,
        ManifestSchemaError,
        "manifest_insufficient_sector_coverage",
        f"manifest: the slate must span at least {required_sectors} sector "
        f"labels, got {len(sectors)}",
    )
    return slate


def _validate_pairs(
    document: dict[str, Any], slate: dict[str, dict[str, Any]]
) -> None:
    pairs = document["pairs"]
    _require(
        isinstance(pairs, list),
        ManifestSchemaError,
        "manifest_pairs_not_a_list",
        "manifest: pairs must be a list",
    )
    status = document["status"]
    if not pairs:
        # An empty pair list is honest ONLY while proposed: it says the remote
        # metadata has not been resolved yet. Any later status asserts the
        # existence of artifacts derived from pairs.
        _require(
            status == STATUS_PROPOSED,
            ManifestSchemaError,
            "manifest_status_requires_pairs",
            f"manifest: status {status!r} requires resolved pairs; only "
            f"{STATUS_PROPOSED!r} may carry an empty pairs list",
        )
        return

    # While proposed, resolution may still be in progress, so a partial pair
    # list is honest. Every later status asserts the corpus was acquired,
    # built, or annotated, and a partial corpus cannot assert that.
    if status == STATUS_PROPOSED:
        _require(
            len(pairs) <= document["target_pair_count"],
            ManifestSchemaError,
            "manifest_pair_count_exceeds_target",
            f"manifest: {len(pairs)} pairs exceed the frozen target of "
            f"{document['target_pair_count']}",
        )
    else:
        _require(
            len(pairs) == document["target_pair_count"],
            ManifestSchemaError,
            "manifest_pair_count_mismatch",
            f"manifest: status {status!r} requires exactly target_pair_count "
            f"({document['target_pair_count']}) resolved pairs, got "
            f"{len(pairs)}",
        )

    seen_pair_ids: set[str] = set()
    seen_slate_ids: set[str] = set()
    seen_accessions: set[tuple[str, str]] = set()
    previous_pair_id = ""
    for index, pair in enumerate(pairs):
        where = f"manifest.pairs[{index}]"
        _exact_keys(
            pair,
            required=_PAIR_REQUIRED,
            where=where,
            error=ManifestSchemaError,
            code_prefix="manifest_pair",
        )
        pair_id = pair["pair_id"]
        _require(
            isinstance(pair_id, str) and bool(_ID_RE.match(pair_id)),
            ManifestSchemaError,
            "manifest_pair_invalid_id",
            f"{where}: pair_id must be a lowercase slug",
        )
        _require(
            pair_id not in seen_pair_ids,
            ManifestSchemaError,
            "manifest_pair_duplicate_id",
            f"{where}: duplicate pair_id {pair_id!r}",
        )
        seen_pair_ids.add(pair_id)
        _require(
            pair_id > previous_pair_id,
            ManifestSchemaError,
            "manifest_pairs_unordered",
            f"{where}: pairs must be sorted by pair_id ({pair_id!r} follows "
            f"{previous_pair_id!r})",
        )
        previous_pair_id = pair_id

        slate_id = pair["slate_id"]
        _require(
            isinstance(slate_id, str) and slate_id in slate,
            ManifestSchemaError,
            "manifest_pair_unknown_slate_id",
            f"{where}: slate_id {slate_id!r} is not in the frozen issuer slate",
        )
        _require(
            slate_id not in seen_slate_ids,
            ManifestSchemaError,
            "manifest_pair_duplicate_slate_id",
            f"{where}: slate entry {slate_id!r} is already used by another pair",
        )
        seen_slate_ids.add(slate_id)
        entry = slate[slate_id]
        _require(
            entry["resolution_status"] == ISSUER_RESOLVED,
            ManifestSchemaError,
            "manifest_pair_unresolved_issuer",
            f"{where}: slate entry {slate_id!r} is still pending official "
            "lookup and cannot back a resolved pair",
        )
        _require(
            pair["issuer_name"] == entry["issuer_name"]
            and pair["sector_label"] == entry["sector_label"]
            and pair["cik"] == entry["cik"],
            ManifestSchemaError,
            "manifest_pair_slate_mismatch",
            f"{where}: issuer identity must match its frozen slate entry — a "
            "pair may not silently change issuer, sector, or CIK",
        )

        previous = _validate_side(pair, "previous", where, status)
        current = _validate_side(pair, "current", where, status)
        _require(
            previous["filing_date"] < current["filing_date"],
            ManifestSchemaError,
            "manifest_pair_filing_dates_unordered",
            f"{where}: previous.filing_date must be strictly before "
            "current.filing_date",
        )
        _require(
            previous["reporting_period"] < current["reporting_period"],
            ManifestSchemaError,
            "manifest_pair_periods_unordered",
            f"{where}: previous.reporting_period must be strictly before "
            "current.reporting_period",
        )
        for side_name, parsed in (("previous", previous), ("current", current)):
            key = (pair["cik"], parsed["accession_number"])
            _require(
                key not in seen_accessions,
                ManifestSchemaError,
                "manifest_duplicate_accession",
                f"{where}.{side_name}: accession "
                f"{parsed['accession_number']!r} for CIK {pair['cik']} already "
                "appears in this manifest",
            )
            seen_accessions.add(key)


def _validate_side(
    pair: dict[str, Any], side: str, where: str, status: str
) -> dict[str, Any]:
    payload = pair[side]
    side_where = f"{where}.{side}"
    _exact_keys(
        payload,
        required=_SIDE_REQUIRED,
        where=side_where,
        error=ManifestSchemaError,
        code_prefix="manifest_side",
    )
    _require(
        payload["form"] == MANIFEST_FORM,
        ManifestSchemaError,
        "manifest_side_invalid_form",
        f"{side_where}: v1 accepts form {MANIFEST_FORM!r} only — amendments "
        "such as '10-K/A' are excluded and are never substitutions",
    )
    accession = payload["accession_number"]
    _require(
        isinstance(accession, str) and bool(_ACCESSION_RE.match(accession)),
        ManifestSchemaError,
        "manifest_side_invalid_accession",
        f"{side_where}: accession_number must look like NNNNNNNNNN-NN-NNNNNN",
    )
    filing_date = _require_iso_date(
        payload["filing_date"],
        f"{side_where}.filing_date",
        ManifestSchemaError,
        "manifest_side",
    )
    reporting_period = _require_iso_date(
        payload["reporting_period"],
        f"{side_where}.reporting_period",
        ManifestSchemaError,
        "manifest_side",
    )
    _require(
        reporting_period <= filing_date,
        ManifestSchemaError,
        "manifest_side_period_after_filing",
        f"{side_where}: reporting_period cannot be after filing_date",
    )

    document_name = payload["primary_document"]
    _require(
        isinstance(document_name, str)
        and bool(_PRIMARY_DOC_RE.match(document_name))
        and document_name.lower().endswith(_PRIMARY_DOC_SUFFIXES),
        ManifestSchemaError,
        "manifest_side_invalid_primary_document",
        f"{side_where}: primary_document must be a plain file name ending in "
        f"one of {list(_PRIMARY_DOC_SUFFIXES)} — no directories, no traversal",
    )

    expected_url = canonical_source_url(pair["cik"], accession, document_name)
    _require(
        payload["official_source_url"] == expected_url,
        ManifestSchemaError,
        "manifest_side_non_official_url",
        f"{side_where}: official_source_url must be the canonical EDGAR URL "
        "derived from cik/accession/primary_document; mirrors, caches, and "
        "third-party copies are not official sources",
    )

    digest = payload["expected_sha256"]
    _require(
        isinstance(digest, str) and bool(_SHA256_RE.match(digest)),
        ManifestSchemaError,
        "manifest_side_invalid_sha256",
        f"{side_where}: expected_sha256 must be 64 lowercase hex characters",
    )
    if digest == PLACEHOLDER_SHA256:
        _require(
            status == STATUS_PROPOSED,
            ManifestSchemaError,
            "manifest_side_placeholder_hash",
            f"{side_where}: a placeholder hash is only permitted while the "
            f"manifest status is {STATUS_PROPOSED!r}; a verified manifest "
            "must carry the real digest",
        )

    return {
        "accession_number": accession,
        "filing_date": filing_date,
        "reporting_period": reporting_period,
        "primary_document": document_name,
        "official_source_url": expected_url,
        "expected_sha256": digest,
    }


def manifest_pairs(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(document.get("pairs") or [])


def pair_sides(pair: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """[(side_name, side_payload)] in deterministic chronological order."""
    return [("previous", pair["previous"]), ("current", pair["current"])]


def find_pair(document: Mapping[str, Any], pair_id: str) -> dict[str, Any] | None:
    for pair in manifest_pairs(document):
        if pair["pair_id"] == pair_id:
            return pair
    return None


def validate_status_transition(current: str, target: str) -> None:
    """A manifest advances one documented step at a time, forward only."""
    for name, value in (("current", current), ("target", target)):
        _require(
            value in STATUS_ORDER,
            StatusTransitionError,
            "unknown_status",
            f"{name} status {value!r} is not one of {list(STATUS_ORDER)}",
        )
    current_index = STATUS_ORDER.index(current)
    target_index = STATUS_ORDER.index(target)
    _require(
        target_index != current_index,
        StatusTransitionError,
        "status_unchanged",
        f"status is already {current!r}",
    )
    _require(
        target_index > current_index,
        StatusTransitionError,
        "status_regression",
        f"a benchmark manifest cannot move backwards from {current!r} to "
        f"{target!r}",
    )
    _require(
        target_index == current_index + 1,
        StatusTransitionError,
        "status_skipped",
        f"{current!r} -> {target!r} skips "
        f"{list(STATUS_ORDER[current_index + 1:target_index])}; each status "
        "asserts artifacts the previous one produced",
    )


# --- Local corpus layout ------------------------------------------------------


class CorpusLayout:
    """Paths under the gitignored local benchmark corpus directory.

    Everything large lives here and nothing here is committed: downloaded
    documents, extracted section text, per-pair indexes and databases, review
    packets, completed annotations, and local run outputs. ``relative()``
    exists because CLI output must never print an absolute local path.
    """

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or config.REAL_FILING_BENCHMARK_DIR)

    # -- sources
    def source_dir(self, pair_id: str, side: str) -> Path:
        return self.root / "sources" / pair_id / side

    def source_file(self, pair_id: str, side: str, primary_document: str) -> Path:
        return self.source_dir(pair_id, side) / primary_document

    def acquisition_metadata_path(self, pair_id: str, side: str) -> Path:
        return self.source_dir(pair_id, side) / "acquisition.json"

    # -- build
    def build_dir(self, pair_id: str) -> Path:
        return self.root / "build" / pair_id

    def build_record_path(self, pair_id: str) -> Path:
        return self.build_dir(pair_id) / "build.json"

    def section_text_path(self, pair_id: str, side: str) -> Path:
        return self.build_dir(pair_id) / f"{side}_item_1a.txt"

    def workspace_dir(self, pair_id: str) -> Path:
        """Per-pair parsed sources, registry, index, and workflow database."""
        return self.build_dir(pair_id) / "workspace"

    def build_log_path(self) -> Path:
        return self.root / "build" / "build_log.jsonl"

    # -- annotation
    def packet_dir(self, pair_id: str) -> Path:
        return self.root / "packets" / pair_id

    def packet_json_path(self, pair_id: str) -> Path:
        return self.packet_dir(pair_id) / "packet.json"

    def packet_markdown_path(self, pair_id: str) -> Path:
        return self.packet_dir(pair_id) / "packet.md"

    def annotations_dir(self) -> Path:
        return self.root / "annotations"

    def annotation_path(self, pair_id: str) -> Path:
        return self.annotations_dir() / f"{pair_id}.json"

    def machine_proposed_path(self, pair_id: str) -> Path:
        return self.annotations_dir() / f"{pair_id}.machine_proposed.json"

    # -- results
    def results_dir(self) -> Path:
        return self.root / "results"

    def relative(self, path: str | Path) -> str:
        """Corpus-relative POSIX path — safe to print, never absolute."""
        candidate = Path(path)
        try:
            return candidate.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return candidate.name


def write_json_atomic(path: str | Path, payload: Any) -> None:
    """Serialize -> temp file in the same directory -> fsync -> os.replace.

    The same durability pattern the review queue and filing registry use. A
    killed process leaves either the old file or the new one, never a partial
    JSON document a later run would misread.
    """
    import os
    import tempfile

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    handle_fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --- Build records ------------------------------------------------------------

_BUILD_SIDE_KEYS = (
    "side",
    "filing_id",
    "source_name",
    "source_sha256",
    "parse_status",
    "extraction_outcome",
    "heading_detected",
    "section_hash",
    "section_char_count",
    "section_paragraph_count",
    "section_chunk_count",
    "indexed_chunk_count",
    "unit_count",
    "units",
    "extraction_detail",
    # Bounded extraction diagnostics (build record v2). Counts, a reason code,
    # an element tag, and two heading labels — never section text, never an
    # excerpt, never a path.
    "extraction_parser_version",
    "extraction_reason",
    "candidate_count",
    "substantive_candidate_count",
    "navigation_rejected_count",
    "selected_element_tag",
    "boundary_heading",
)

# ``excerpt`` is a bounded MAX_EXCERPT_CHARS slice kept so a reviewer can judge
# a unit without opening the filing. Build records live only in the gitignored
# corpus directory; no committed artifact carries it.
_BUILD_UNIT_KEYS = (
    "unit_id",
    "unit_key",
    "heading",
    "char_count",
    "content_hash",
    "excerpt",
)


# Keys whose values legitimately differ between two builds of identical inputs:
# wall-clock stamps and the execution-attempt identifiers minted from them. The
# same idea as the synthetic regression suite's timestamp projection. Everything
# else — extraction outcomes, section hashes, unit ids, unit content hashes,
# result hash, change counts, evidence counts — must be byte-identical, which is
# what makes "deterministic output from identical inputs" a checkable claim
# rather than a stated intention.
_VOLATILE_BUILD_KEYS = frozenset(
    {"built_at", "build_hash", "attempts", "started_at", "finished_at", "attempt_id"}
)


def _reproducible(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _reproducible(item)
            for key, item in sorted(value.items())
            if key not in _VOLATILE_BUILD_KEYS
        }
    if isinstance(value, list):
        return [_reproducible(item) for item in value]
    return value


def build_record_hash(record: Mapping[str, Any]) -> str:
    """Hash of a build record's reproducible content."""
    return payload_hash(_reproducible(dict(record)))


def validate_build_record(record: Any) -> None:
    """Structural check for a locally produced build record."""
    _require(
        isinstance(record, dict),
        CorpusDriftError,
        "build_record_not_a_mapping",
        "build record: expected a JSON object",
    )
    _require(
        record.get("record_version") == BUILD_RECORD_VERSION,
        CorpusDriftError,
        "build_record_version_mismatch",
        f"build record: record_version must be {BUILD_RECORD_VERSION!r}",
    )
    for side in ("previous", "current"):
        payload = record.get(side)
        _exact_keys(
            payload,
            required=_BUILD_SIDE_KEYS,
            where=f"build record.{side}",
            error=CorpusDriftError,
            code_prefix="build_record",
        )
        _require(
            payload["extraction_outcome"] in EXTRACTION_OUTCOMES,
            CorpusDriftError,
            "build_record_invalid_extraction_outcome",
            f"build record.{side}: extraction_outcome must be one of "
            f"{list(EXTRACTION_OUTCOMES)}",
        )
        for index, unit in enumerate(payload["units"]):
            _exact_keys(
                unit,
                required=_BUILD_UNIT_KEYS,
                where=f"build record.{side}.units[{index}]",
                error=CorpusDriftError,
                code_prefix="build_record_unit",
            )
            _require(
                isinstance(unit["excerpt"], str)
                and len(unit["excerpt"]) <= MAX_EXCERPT_CHARS,
                CorpusDriftError,
                "build_record_excerpt_too_long",
                f"build record.{side}.units[{index}]: excerpt exceeds the "
                f"{MAX_EXCERPT_CHARS}-character bound",
            )


def build_unit_ids(record: Mapping[str, Any], side: str) -> list[str]:
    return [unit["unit_id"] for unit in record[side]["units"]]


def build_is_evaluable(record: Mapping[str, Any]) -> bool:
    """True when both sides produced an extracted Item 1A section.

    A pair whose section is missing, ambiguous, or unparseable is still part
    of the corpus and still counted in the corpus-quality report — it simply
    cannot contribute gold change metrics, and saying so explicitly is the
    point.
    """
    return all(
        record[side]["extraction_outcome"] == EXTRACTION_EXTRACTED
        for side in ("previous", "current")
    )


# --- Annotations --------------------------------------------------------------

_ANNOTATION_REQUIRED = (
    "schema_version",
    "annotation_protocol_version",
    "benchmark_id",
    "pair_id",
    "annotation_status",
    "annotator_id",
    "verification_timestamp",
    "source_manifest_hash",
    "previous_section_hash",
    "current_section_hash",
    "labels",
)
_ANNOTATION_OPTIONAL = ("generated_by",)

_LABEL_REQUIRED = (
    "label_id",
    "expected_change_type",
    "previous_unit_id",
    "current_unit_id",
    "expected_reason_code",
    "expected_evidence_side",
    "expected_direction",
    "reviewer_note",
    "confidence",
)


def load_annotation(path: str | Path) -> dict[str, Any]:
    """Read and fully validate one completed annotation file."""
    target = Path(path)
    if not target.exists():
        raise AnnotationSchemaError(
            "annotation_not_found", f"annotation file not found: {target.name}"
        )
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AnnotationSchemaError(
            "annotation_not_json", f"{target.name}: {exc}"
        ) from exc
    validate_annotation(document)
    return document


def validate_annotation(document: Any) -> None:
    """Reject every documented annotation defect.

    The load-bearing rule is the status/identity invariant: a machine-produced
    status may carry no annotator identity, and a human status requires one.
    That is what makes "machine proposals cannot become gold automatically" a
    structural property instead of a promise.
    """
    _exact_keys(
        document,
        required=_ANNOTATION_REQUIRED,
        optional=_ANNOTATION_OPTIONAL,
        where="annotation",
        error=AnnotationSchemaError,
        code_prefix="annotation",
    )
    _require(
        document["schema_version"] == ANNOTATION_SCHEMA_VERSION,
        AnnotationSchemaError,
        "annotation_schema_version_mismatch",
        f"annotation: schema_version must be {ANNOTATION_SCHEMA_VERSION!r}",
    )
    _require(
        document["annotation_protocol_version"] == ANNOTATION_PROTOCOL_VERSION,
        AnnotationSchemaError,
        "annotation_protocol_version_mismatch",
        f"annotation: annotation_protocol_version must be "
        f"{ANNOTATION_PROTOCOL_VERSION!r}",
    )
    _require(
        isinstance(document["benchmark_id"], str)
        and bool(_ID_RE.match(document["benchmark_id"])),
        AnnotationSchemaError,
        "annotation_invalid_benchmark_id",
        "annotation: benchmark_id must be a lowercase slug",
    )
    _require(
        isinstance(document["pair_id"], str)
        and bool(_ID_RE.match(document["pair_id"])),
        AnnotationSchemaError,
        "annotation_invalid_pair_id",
        "annotation: pair_id must be a lowercase slug",
    )
    status = document["annotation_status"]
    _require(
        status in ANNOTATION_STATUSES,
        AnnotationSchemaError,
        "annotation_invalid_status",
        f"annotation: annotation_status must be one of "
        f"{list(ANNOTATION_STATUSES)}, got {status!r}",
    )
    for field in ("source_manifest_hash", "previous_section_hash", "current_section_hash"):
        _require(
            isinstance(document[field], str)
            and bool(_SHA256_RE.match(document[field])),
            AnnotationSchemaError,
            "annotation_invalid_hash",
            f"annotation.{field}: must be 64 lowercase hex characters",
        )

    _validate_annotator_identity(document, status)
    _validate_labels(document)


def _validate_annotator_identity(document: Mapping[str, Any], status: str) -> None:
    """The machine/human boundary, enforced structurally."""
    annotator = document["annotator_id"]
    timestamp = document["verification_timestamp"]

    if status in MACHINE_ONLY_STATUSES:
        _require(
            annotator is None,
            AnnotationSchemaError,
            "annotation_machine_status_with_annotator",
            f"annotation: status {status!r} is machine-produced and must not "
            "name a human annotator — a machine proposal is not a person's "
            "judgement",
        )
        _require(
            timestamp is None,
            AnnotationSchemaError,
            "annotation_machine_status_with_timestamp",
            f"annotation: status {status!r} must not carry a verification "
            "timestamp; nothing was verified",
        )
        return

    _require_bounded_str(
        annotator,
        "annotation.annotator_id",
        max_chars=MAX_ANNOTATOR_ID_CHARS,
        error=AnnotationSchemaError,
        code_prefix="annotation_annotator",
    )
    if status in HUMAN_ATTRIBUTED_STATUSES:
        _require(
            timestamp is not None,
            AnnotationSchemaError,
            "annotation_missing_verification_timestamp",
            f"annotation: status {status!r} requires an explicit "
            "verification_timestamp set by the human reviewer",
        )
    if timestamp is not None:
        _require_iso_timestamp(
            timestamp,
            "annotation.verification_timestamp",
            AnnotationSchemaError,
            "annotation",
        )


def _validate_labels(document: Mapping[str, Any]) -> None:
    labels = document["labels"]
    _require(
        isinstance(labels, list),
        AnnotationSchemaError,
        "annotation_labels_not_a_list",
        "annotation: labels must be a list",
    )
    seen_ids: set[str] = set()
    seen_units: set[tuple[str | None, str | None]] = set()
    for index, label in enumerate(labels):
        where = f"annotation.labels[{index}]"
        _exact_keys(
            label,
            required=_LABEL_REQUIRED,
            where=where,
            error=AnnotationSchemaError,
            code_prefix="annotation_label",
        )
        label_id = label["label_id"]
        _require(
            isinstance(label_id, str) and bool(label_id.strip()),
            AnnotationSchemaError,
            "annotation_label_invalid_id",
            f"{where}: label_id must be a non-empty string",
        )
        _require(
            label_id not in seen_ids,
            AnnotationSchemaError,
            "annotation_duplicate_label_id",
            f"{where}: duplicate label_id {label_id!r}",
        )
        seen_ids.add(label_id)

        change_type = label["expected_change_type"]
        _require(
            change_type in EXPECTED_CHANGE_TYPES,
            AnnotationSchemaError,
            "annotation_label_invalid_change_type",
            f"{where}: expected_change_type must be one of "
            f"{list(EXPECTED_CHANGE_TYPES)}, got {change_type!r}",
        )
        previous_unit = label["previous_unit_id"]
        current_unit = label["current_unit_id"]
        for field, value in (
            ("previous_unit_id", previous_unit),
            ("current_unit_id", current_unit),
        ):
            _require(
                value is None or (isinstance(value, str) and bool(value.strip())),
                AnnotationSchemaError,
                "annotation_label_invalid_unit_reference",
                f"{where}.{field}: must be a unit id or null",
            )
        _require(
            previous_unit is not None or current_unit is not None,
            AnnotationSchemaError,
            "annotation_label_no_unit_reference",
            f"{where}: a label must reference at least one unit",
        )
        unit_key = (previous_unit, current_unit)
        _require(
            unit_key not in seen_units,
            AnnotationSchemaError,
            "annotation_duplicate_label_units",
            f"{where}: a second label already binds {unit_key}",
        )
        seen_units.add(unit_key)

        _validate_label_shape(where, change_type, previous_unit, current_unit, label)

        side = label["expected_evidence_side"]
        _require(
            side in EVIDENCE_SIDES,
            AnnotationSchemaError,
            "annotation_label_invalid_evidence_side",
            f"{where}: expected_evidence_side must be one of "
            f"{list(EVIDENCE_SIDES)}, got {side!r}",
        )
        direction = label["expected_direction"]
        _require(
            direction is None or direction in DIRECTIONS,
            AnnotationSchemaError,
            "annotation_label_invalid_direction",
            f"{where}: expected_direction must be one of "
            f"{list(DIRECTIONS)} or null",
        )
        _require(
            label["confidence"] in CONFIDENCE_LEVELS,
            AnnotationSchemaError,
            "annotation_label_invalid_confidence",
            f"{where}: confidence must be one of {list(CONFIDENCE_LEVELS)}",
        )
        note = label["reviewer_note"]
        _require(
            note is None
            or (isinstance(note, str) and len(note) <= MAX_REVIEWER_NOTE_CHARS),
            AnnotationSchemaError,
            "annotation_label_note_too_long",
            f"{where}: reviewer_note must be null or at most "
            f"{MAX_REVIEWER_NOTE_CHARS} characters",
        )


def _validate_label_shape(
    where: str,
    change_type: str,
    previous_unit: str | None,
    current_unit: str | None,
    label: Mapping[str, Any],
) -> None:
    """Unit references and reason codes must agree with the change type."""
    if change_type == "added":
        _require(
            previous_unit is None and current_unit is not None,
            AnnotationSchemaError,
            "annotation_label_shape_mismatch",
            f"{where}: an 'added' label references a current unit only",
        )
    elif change_type == "removed":
        _require(
            previous_unit is not None and current_unit is None,
            AnnotationSchemaError,
            "annotation_label_shape_mismatch",
            f"{where}: a 'removed' label references a previous unit only",
        )
    elif change_type in ("modified", "unchanged"):
        _require(
            previous_unit is not None and current_unit is not None,
            AnnotationSchemaError,
            "annotation_label_shape_mismatch",
            f"{where}: a {change_type!r} label references both units",
        )

    reason = label["expected_reason_code"]
    if change_type == "undetermined":
        _require(
            reason is None
            or (isinstance(reason, str) and bool(reason.strip())),
            AnnotationSchemaError,
            "annotation_label_invalid_reason_code",
            f"{where}: expected_reason_code must be a stable code or null",
        )
    else:
        _require(
            reason is None,
            AnnotationSchemaError,
            "annotation_label_unexpected_reason_code",
            f"{where}: only an 'undetermined' label carries an "
            "expected_reason_code",
        )


def validate_annotation_against_build(
    annotation: Mapping[str, Any], build_record: Mapping[str, Any]
) -> None:
    """Bind an annotation to the exact corpus build it was written against.

    Labels are statements about specific section text. If that text changed,
    the labels are not merely stale — they are about a different document, and
    silently reusing them would fabricate agreement. Same for a unit id that
    does not exist in the build.
    """
    _require(
        annotation["pair_id"] == build_record["pair_id"],
        CorpusDriftError,
        "annotation_pair_mismatch",
        f"annotation is for pair {annotation['pair_id']!r} but the build "
        f"record is for {build_record['pair_id']!r}",
    )
    for side in ("previous", "current"):
        field = f"{side}_section_hash"
        _require(
            annotation[field] == build_record[side]["section_hash"],
            CorpusDriftError,
            "annotation_section_hash_drift",
            f"annotation.{field} does not match the built {side} section; the "
            "source text changed, so these labels describe different content "
            "and cannot be used",
        )

    known = set(build_unit_ids(build_record, "previous")) | set(
        build_unit_ids(build_record, "current")
    )
    for label in annotation["labels"]:
        for field in ("previous_unit_id", "current_unit_id"):
            value = label[field]
            if value is None:
                continue
            _require(
                value in known,
                CorpusDriftError,
                "annotation_unknown_unit_reference",
                f"annotation label {label['label_id']!r}: {field} {value!r} "
                "is not a unit in this corpus build",
            )
            expected_side = "previous" if field == "previous_unit_id" else "current"
            _require(
                value.startswith(f"{expected_side}:"),
                CorpusDriftError,
                "annotation_unit_side_mismatch",
                f"annotation label {label['label_id']!r}: {field} {value!r} "
                f"is not a {expected_side}-side unit",
            )


def annotation_hash(annotation: Mapping[str, Any]) -> str:
    """Hash of the evaluated content of an annotation.

    Reviewer notes are excluded: they are review context for a human, never an
    input to a metric, and they are never emitted in a report. Excluding them
    from the hash keeps the join key stable when a reviewer clarifies a note.
    """
    stable = dict(annotation)
    stable["labels"] = [
        {key: value for key, value in label.items() if key != "reviewer_note"}
        for label in annotation["labels"]
    ]
    return payload_hash(stable)


def annotations_hash(annotations: Iterable[Mapping[str, Any]]) -> str:
    """One hash over every annotation entering an evaluation, order-stable."""
    return payload_hash(
        sorted(
            [
                {"pair_id": item["pair_id"], "annotation_hash": annotation_hash(item)}
                for item in annotations
            ],
            key=lambda entry: entry["pair_id"],
        )
    )


def machine_proposed_annotation(
    *,
    pair_id: str,
    source_manifest_hash: str,
    previous_section_hash: str,
    current_section_hash: str,
    labels: list[dict[str, Any]],
    generated_by: str,
) -> dict[str, Any]:
    """Build a MACHINE-PROPOSED annotation document.

    It is deliberately impossible to produce a gold document here: the status
    is fixed to ``machine_proposed`` and identity fields are fixed to null. A
    human moves the file to ``human_verified`` by editing it and supplying
    their own identity and timestamp — which is the entire human-verification
    boundary, expressed as a data invariant.
    """
    document = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "annotation_protocol_version": ANNOTATION_PROTOCOL_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "pair_id": pair_id,
        "annotation_status": ANNOTATION_MACHINE_PROPOSED,
        "annotator_id": None,
        "verification_timestamp": None,
        "source_manifest_hash": source_manifest_hash,
        "previous_section_hash": previous_section_hash,
        "current_section_hash": current_section_hash,
        "generated_by": generated_by,
        "labels": labels,
    }
    validate_annotation(document)
    return document


def is_gold(annotation: Mapping[str, Any]) -> bool:
    """The single predicate that decides whether labels count.

    Everything else — machine proposals, in-progress review, rejected pairs —
    is excluded from every gold numerator and denominator.
    """
    return annotation.get("annotation_status") == GOLD_STATUS


# --- Metric primitives --------------------------------------------------------

# Identical policy to the synthetic regression suite and the reliability
# module: a rate whose denominator is zero asserts nothing, so it reports as
# null with its denominator visible — never 0, never NaN, never omitted.
ZERO_DENOMINATOR_POLICY = "null_value_metric_asserts_nothing"


def rate(numerator: int, denominator: int, name: str) -> dict[str, Any]:
    """A rate with its denominator visible and zero-denominator explicit."""
    if denominator == 0:
        return {
            "metric": name,
            "value": None,
            "numerator": numerator,
            "denominator": 0,
            "zero_denominator": True,
            "zero_denominator_policy": ZERO_DENOMINATOR_POLICY,
        }
    return {
        "metric": name,
        "value": round(numerator / denominator, 6),
        "numerator": numerator,
        "denominator": denominator,
        "zero_denominator": False,
    }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def repo_commit_sha(repo_root: str | Path | None = None) -> str | None:
    """The current commit, read from .git without running a subprocess.

    Returns None when the directory is not a git checkout or the ref cannot
    be resolved — a report says ``null`` rather than guessing a provenance it
    does not have.
    """
    root = Path(repo_root or Path(__file__).resolve().parent)
    git_dir = root / ".git"
    if git_dir.is_file():  # worktree: .git is a file pointing elsewhere
        try:
            pointer = git_dir.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not pointer.startswith("gitdir:"):
            return None
        git_dir = Path(pointer.split(":", 1)[1].strip())
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
    if not git_dir.is_dir():
        return None
    head = git_dir / "HEAD"
    try:
        content = head.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content.startswith("ref:"):
        return content if re.fullmatch(r"[0-9a-f]{40}", content) else None
    ref = content.split(":", 1)[1].strip()
    ref_path = git_dir / ref
    if ref_path.is_file():
        try:
            value = ref_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value if re.fullmatch(r"[0-9a-f]{40}", value) else None
    packed = git_dir / "packed-refs"
    if not packed.is_file():
        return None
    try:
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0] if re.fullmatch(r"[0-9a-f]{40}", parts[0]) else None
    except OSError:
        return None
    return None
