"""Metadata-only holdout selection for the real-filing extraction benchmark.

Stage 3.5. The twenty ``real_filing_v1`` filings are an EXTRACTION DEVELOPMENT
corpus: their HTML structure was inspected while ``sec_html_item_headings.v2``
was designed, so every extraction number over them is in-sample. A
generalization claim needs a corpus whose exact filings were frozen AFTER the
parser was frozen and BEFORE any of their bodies was downloaded or structurally
inspected. This module is that freeze: a predeclared deterministic selection
protocol over OFFICIAL SEC METADATA ONLY, producing an exact ten-pair holdout
manifest whose filing bodies remain deliberately unfetched.

The one rule this module exists to enforce
------------------------------------------
No filing body is contacted, downloaded, or inspected during selection. The
selection may read issuer identity, SIC codes, filing lists, and the filer's
own XBRL fiscal-year designations — never a primary document, never HTML
structure, never Item 1A content, never an extraction or detector outcome.
That is enforced structurally: every URL the selection can fetch must match a
CLOSED allowlist of metadata endpoint patterns (``require_metadata_url``), so
a ``www.sec.gov/Archives/...`` primary-document URL is unreachable by
construction, not by convention.

What this module deliberately cannot say
----------------------------------------
- ``source_verified`` is false: no body bytes exist, so no checksum exists.
  ``expected_sha256`` is null on every side until a later, separate
  acquisition step.
- ``extraction_holdout_evaluation`` is false: extraction has not run over
  these filings, and no accuracy or generalization claim exists.
- A selected issuer is NEVER replaced because of anything observed later —
  HTML structure, extraction outcome, detector output, packet quality, or
  evaluation result. A pair that turns out to be difficult stays. Replacing
  it would convert the holdout back into development data.
- Modifying ``sec_html_item_headings.v2`` in response to holdout results has
  the same effect; the manifest freezes the parser version AND a hash of the
  parser source so that conversion is detectable, not just discouraged.

Like ``real_filing_benchmark``, this module imports nothing that can open a
socket; transport is injected by the caller (the CLI composes it with
``real_filing_acquisition.Fetcher``). Tests assert that at the import graph.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

import real_filing_benchmark as rfb

# --- Versions and identity ----------------------------------------------------

HOLDOUT_MANIFEST_SCHEMA_VERSION = "real-filing-holdout.manifest.v1"
HOLDOUT_SELECTION_PROTOCOL_VERSION = "real-filing-holdout-selection.v1"
HOLDOUT_SELECTION_REPORT_VERSION = "real-filing-holdout.selection-report.v1"
HOLDOUT_BENCHMARK_ID = "real_filing_holdout_v1"
HOLDOUT_BENCHMARK_VERSION = 1

#: The extraction parser this holdout exists to evaluate, frozen by name. The
#: string is a literal on purpose: importing ``loaders`` would pull the whole
#: format-handler graph (and its dependencies) into a module whose contract is
#: "no extraction import required". A test pins this literal to
#: ``loaders/sec_headings.py``'s PARSER_VERSION by reading that file's source.
FROZEN_EXTRACTION_PARSER_VERSION = "sec_html_item_headings.v2"
#: Repository path of the frozen parser, recorded (with its sha256) in the
#: manifest so a post-freeze parser edit is a detectable fact rather than a
#: process violation nobody can check.
FROZEN_PARSER_SOURCE_PATH = "loaders/sec_headings.py"

# --- Holdout manifest maturity ladder ------------------------------------------
# The holdout is born at a maturity the development ladder does not have:
# identities and exact filing pairs are frozen from official metadata, but no
# body has been acquired, so nothing is source-verified. Later stages reuse the
# development ladder's names because they assert the same artifacts.

STATUS_HOLDOUT_FROZEN_METADATA_ONLY = "holdout_frozen_metadata_only"
HOLDOUT_STATUS_ORDER = (
    STATUS_HOLDOUT_FROZEN_METADATA_ONLY,
    rfb.STATUS_SOURCE_VERIFIED,
    rfb.STATUS_CORPUS_BUILT,
    rfb.STATUS_HUMAN_ANNOTATION_COMPLETE,
)

# --- The predeclared selection protocol -----------------------------------------
# Everything a reader needs to re-derive the selection is in this one mapping,
# and ``selection_protocol_hash()`` freezes it. It is defined BEFORE any
# candidate is resolved and committed in the same change as the manifest it
# produced; changing it afterwards changes the hash the manifest records.

TARGET_PREVIOUS_FISCAL_YEAR = 2023
TARGET_CURRENT_FISCAL_YEAR = 2024

#: Closed SIC ranges, declared before candidate resolution. Ranges are
#: inclusive on both ends and non-overlapping; together they deliberately do
#: NOT cover the whole SIC space — a stratum is a sampling frame, not a census.
SIC_STRATA = (
    {
        "stratum_id": "sic-2000s",
        "sic_range": [2000, 2999],
        "label": "Manufacturing: food, textiles, chemicals, pharmaceuticals",
        "quota": 2,
    },
    {
        "stratum_id": "sic-3000s",
        "sic_range": [3000, 3999],
        "label": "Manufacturing: industrial, electronic, transportation equipment",
        "quota": 2,
    },
    {
        "stratum_id": "sic-4000s",
        "sic_range": [4000, 4999],
        "label": "Transportation, communications, and utilities",
        "quota": 2,
    },
    {
        "stratum_id": "sic-5000s",
        "sic_range": [5000, 5999],
        "label": "Wholesale and retail trade",
        "quota": 2,
    },
    {
        "stratum_id": "sic-6000s",
        "sic_range": [6000, 6999],
        "label": "Finance, insurance, and real estate",
        "quota": 2,
    },
)

TARGET_PAIR_COUNT = sum(stratum["quota"] for stratum in SIC_STRATA)
MINIMUM_DISTINCT_STRATA = 5
#: Hard bound on submissions-endpoint probes. Exhausting it is a FAILED
#: selection, never a quietly smaller corpus.
MAX_SUBMISSIONS_PROBES = 500

# Stable per-candidate reason codes, in the exact order the checks run. The
# first failing check is the recorded reason, which is what makes exclusion
# counts reproducible from the same metadata.
REASON_DEVELOPMENT_CORPUS_CIK = "development_corpus_cik"
REASON_SIC_MISSING = "sic_missing"
REASON_SIC_OUTSIDE_STRATA = "sic_outside_declared_strata"
REASON_STRATUM_FULL_DEFERRED = "stratum_full_deferred"
REASON_SUBMISSIONS_UNREADABLE = "submissions_unreadable"
REASON_PAGED_HISTORY_UNREADABLE = "paged_history_unreadable"
REASON_NO_10K_ROWS = "no_10k_rows"
REASON_FISCAL_METADATA_UNREADABLE = "fiscal_year_metadata_unreadable"
REASON_TARGET_FISCAL_YEAR_MISSING = "target_fiscal_year_missing"
REASON_FISCAL_METADATA_AMBIGUOUS = "fiscal_year_metadata_ambiguous"
REASON_TARGET_ROW_NOT_FOUND = "target_row_not_form_10k"
REASON_ROW_METADATA_INCOMPLETE = "row_metadata_incomplete"
REASON_DEVELOPMENT_CORPUS_ACCESSION = "development_corpus_accession"
REASON_CHRONOLOGY_INCONSISTENT = "chronology_inconsistent"
REASON_DUPLICATE_SELECTION = "duplicate_selection"

SELECTION_FAILURE_STRATUM_UNFILLED = "stratum_quota_unfilled"
SELECTION_FAILURE_PROBE_BUDGET = "submissions_probe_budget_exhausted"
SELECTION_FAILURE_INSUFFICIENT_STRATA = "insufficient_distinct_strata"


def selection_protocol() -> dict[str, Any]:
    """The full machine-readable protocol, exactly as executed."""
    return {
        "protocol_version": HOLDOUT_SELECTION_PROTOCOL_VERSION,
        "form": rfb.MANIFEST_FORM,
        "target_pair_count": TARGET_PAIR_COUNT,
        "target_filing_count": TARGET_PAIR_COUNT * 2,
        "target_previous_fiscal_year": TARGET_PREVIOUS_FISCAL_YEAR,
        "target_current_fiscal_year": TARGET_CURRENT_FISCAL_YEAR,
        "fiscal_year_rule": (
            "A target row is the unique form 10-K accession whose official "
            "companyfacts fact rows designate fp='FY' and fy=<target year>. "
            "The filer's own XBRL designation decides the fiscal year; a "
            "period-end-year heuristic is never used."
        ),
        "candidate_universe": (
            "Unique CIKs from the official company_tickers.json registrant "
            "list, ordered by normalized CIK ascending, then normalized "
            "legal issuer name (CIKs are unique, so the name key is a "
            "declared formality)."
        ),
        "sic_strata": [dict(stratum) for stratum in SIC_STRATA],
        "minimum_distinct_strata": MINIMUM_DISTINCT_STRATA,
        "max_submissions_probes": MAX_SUBMISSIONS_PROBES,
        "eligibility_check_order": [
            REASON_DEVELOPMENT_CORPUS_CIK,
            REASON_SUBMISSIONS_UNREADABLE,
            REASON_SIC_MISSING,
            REASON_SIC_OUTSIDE_STRATA,
            REASON_STRATUM_FULL_DEFERRED,
            REASON_FISCAL_METADATA_UNREADABLE,
            REASON_TARGET_FISCAL_YEAR_MISSING,
            REASON_FISCAL_METADATA_AMBIGUOUS,
            REASON_PAGED_HISTORY_UNREADABLE,
            REASON_NO_10K_ROWS,
            REASON_TARGET_ROW_NOT_FOUND,
            REASON_ROW_METADATA_INCOMPLETE,
            REASON_DEVELOPMENT_CORPUS_ACCESSION,
            REASON_CHRONOLOGY_INCONSISTENT,
            REASON_DUPLICATE_SELECTION,
        ],
        "fallback_rule": (
            "Deferred candidates (stratum quota already full at probe time) "
            "are consumed in original universe order to absorb slots their "
            "home stratum could not fill; an absorbed slot is recorded under "
            "the absorbing candidate's own stratum. If fewer than "
            "target_pair_count pairs fill, or fewer than "
            "minimum_distinct_strata distinct strata result, the selection "
            "FAILS with per-stratum reason codes. The corpus size is never "
            "silently reduced and the protocol is never altered after "
            "partial resolution."
        ),
        "replacement_rule": (
            "A selected issuer or filing pair is never replaced, reordered, "
            "or dropped because of later-observed filing-body structure, "
            "extraction outcome, detector output, annotation difficulty, or "
            "evaluation result."
        ),
        "body_access_rule": (
            "Selection may contact ONLY the closed metadata endpoint "
            "allowlist. Filing primary documents, exhibits, inline XBRL "
            "documents, and every other filing-body resource are outside it "
            "and are never contacted."
        ),
    }


def selection_protocol_hash() -> str:
    return rfb.payload_hash(selection_protocol())


# --- Metadata endpoint allowlist ------------------------------------------------
# CLOSED patterns. Anything else — most importantly every
# https://www.sec.gov/Archives/... primary-document URL — is rejected before a
# transport is even consulted. This is the structural body-access prohibition;
# documentation merely repeats it.

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL_RE = re.compile(
    r"^https://data\.sec\.gov/submissions/CIK\d{10}(?:-submissions-\d{3})?\.json$"
)
_COMPANYFACTS_URL_RE = re.compile(
    r"^https://data\.sec\.gov/api/xbrl/companyfacts/CIK\d{10}\.json$"
)

FAILURE_NON_METADATA_ENDPOINT = "non_metadata_endpoint"


class HoldoutSelectionError(rfb.BenchmarkError):
    """A bounded, code-carrying holdout-selection failure. Never carries
    filing content, credentials, or local paths."""


class HoldoutManifestError(rfb.BenchmarkError):
    """The holdout manifest is invalid, incomplete, or self-contradictory."""


class NonMetadataEndpoint(HoldoutSelectionError):
    """A URL outside the closed metadata allowlist — including every filing
    primary-document URL."""


def require_metadata_url(url: Any) -> str:
    """Accept only URLs matching the closed metadata endpoint allowlist."""
    if not isinstance(url, str) or not url.strip():
        raise NonMetadataEndpoint(
            FAILURE_NON_METADATA_ENDPOINT, "metadata URL is missing"
        )
    candidate = url.strip()
    if candidate == COMPANY_TICKERS_URL:
        return candidate
    if _SUBMISSIONS_URL_RE.match(candidate) or _COMPANYFACTS_URL_RE.match(candidate):
        return candidate
    raise NonMetadataEndpoint(
        FAILURE_NON_METADATA_ENDPOINT,
        "URL is not on the closed SEC metadata endpoint allowlist. Filing "
        "primary documents and every other filing-body resource are outside "
        "the allowlist and are never contacted during holdout selection.",
    )


def submissions_url(cik: str) -> str:
    return f"https://data.sec.gov/submissions/CIK{cik}.json"


def submissions_page_url(page_name: str) -> str:
    """Paged submissions-history URL, validated against the allowlist."""
    return require_metadata_url(f"https://data.sec.gov/submissions/{page_name}")


def companyfacts_url(cik: str) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


def endpoint_class(url: str) -> str:
    """Stable label for request accounting in the selection report."""
    if url == COMPANY_TICKERS_URL:
        return "company_tickers"
    if _COMPANYFACTS_URL_RE.match(url):
        return "companyfacts"
    if _SUBMISSIONS_URL_RE.match(url):
        return "submissions"
    return "non_metadata"


class MetadataOnlyFetcher:
    """Wrap any ``fetch_json(url) -> Any`` callable with the allowlist.

    Every fetch — including one a future caller adds — passes through
    ``require_metadata_url`` first, and every accepted request is counted per
    endpoint class so the selection report's accounting is produced by the
    same object that made the requests.
    """

    def __init__(self, fetch_json: Callable[[str], Any]):
        self._fetch_json = fetch_json
        self.request_counts: dict[str, int] = {}

    def get(self, url: str) -> Any:
        official = require_metadata_url(url)
        label = endpoint_class(official)
        self.request_counts[label] = self.request_counts.get(label, 0) + 1
        return self._fetch_json(official)


# --- Development-corpus exclusions ----------------------------------------------


def development_exclusions(
    development_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """CIKs and accessions of the extraction-development corpus, derived from
    the committed manifest rather than retyped."""
    manifest = (
        dict(development_manifest)
        if development_manifest is not None
        else rfb.load_manifest()
    )
    ciks = sorted(
        {
            entry["cik"]
            for entry in manifest["proposed_issuers"]
            if entry.get("cik")
        }
    )
    accessions: set[str] = set()
    for pair in rfb.manifest_pairs(manifest):
        for _side, payload in rfb.pair_sides(pair):
            accessions.add(payload["accession_number"])
    return {
        "development_benchmark_id": manifest["benchmark_id"],
        "excluded_ciks": ciks,
        "excluded_accessions": sorted(accessions),
    }


# --- Candidate universe ---------------------------------------------------------


def candidate_universe(tickers_payload: Any) -> list[dict[str, str]]:
    """Deterministic candidate ordering from the official registrant list.

    One entry per unique CIK, ordered by normalized CIK ascending, then
    normalized title. The title is informational only — the legal issuer name
    recorded in the manifest always comes from the issuer's own submissions
    metadata, never from this file.
    """
    if not isinstance(tickers_payload, dict):
        raise HoldoutSelectionError(
            "universe_not_a_mapping",
            "company_tickers payload: expected a JSON object",
        )
    by_cik: dict[str, str] = {}
    for entry in tickers_payload.values():
        if not isinstance(entry, dict):
            continue
        raw_cik = entry.get("cik_str")
        if not isinstance(raw_cik, int) or isinstance(raw_cik, bool) or raw_cik <= 0:
            continue
        cik = f"{raw_cik:010d}"
        title = str(entry.get("title") or "").strip()
        if cik not in by_cik or (title and title < by_cik[cik]):
            by_cik[cik] = title
    return [
        {"cik": cik, "title": by_cik[cik]}
        for cik in sorted(by_cik)
    ]


def stratum_for_sic(sic: int) -> dict[str, Any] | None:
    for stratum in SIC_STRATA:
        low, high = stratum["sic_range"]
        if low <= sic <= high:
            return stratum
    return None


# --- Per-candidate metadata evaluation ------------------------------------------


def _ten_k_rows(block: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Exact form-10-K rows from one submissions block. ``10-K/A`` and every
    other form fail the equality and can never be admitted."""
    forms = block.get("form") or []
    accessions = block.get("accessionNumber") or []
    filing_dates = block.get("filingDate") or []
    report_dates = block.get("reportDate") or []
    documents = block.get("primaryDocument") or []
    rows = []
    for index, form in enumerate(forms):
        if form != rfb.MANIFEST_FORM:
            continue
        rows.append(
            {
                "form": form,
                "accession_number": (
                    accessions[index] if index < len(accessions) else None
                ),
                "filing_date": (
                    filing_dates[index] if index < len(filing_dates) else None
                ),
                "reporting_period": (
                    report_dates[index] if index < len(report_dates) else None
                ),
                "primary_document": (
                    documents[index] if index < len(documents) else None
                ),
            }
        )
    return rows


def fiscal_year_accessions(companyfacts_payload: Any) -> dict[int, set[str]]:
    """fy -> {accession} for form-10-K facts with fp='FY', across every
    taxonomy and concept, so the mapping cannot depend on one concept choice."""
    mapping: dict[int, set[str]] = {}
    facts = (companyfacts_payload or {}).get("facts")
    if not isinstance(facts, dict):
        return mapping
    for taxonomy in facts.values():
        if not isinstance(taxonomy, dict):
            continue
        for concept in taxonomy.values():
            units = concept.get("units") if isinstance(concept, dict) else None
            if not isinstance(units, dict):
                continue
            for entries in units.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("form") != rfb.MANIFEST_FORM:
                        continue
                    if entry.get("fp") != "FY":
                        continue
                    fy = entry.get("fy")
                    accn = entry.get("accn")
                    if not isinstance(fy, int) or not isinstance(accn, str):
                        continue
                    mapping.setdefault(fy, set()).add(accn)
    return mapping


def _complete_row(row: Mapping[str, Any]) -> bool:
    accession = row.get("accession_number")
    filing_date = row.get("filing_date")
    period = row.get("reporting_period")
    document = row.get("primary_document")
    return bool(
        isinstance(accession, str)
        and rfb._ACCESSION_RE.match(accession)
        and isinstance(filing_date, str)
        and rfb._ISO_DATE_RE.match(filing_date)
        and isinstance(period, str)
        and rfb._ISO_DATE_RE.match(period)
        and isinstance(document, str)
        and rfb._PRIMARY_DOC_RE.match(document)
        and document.lower().endswith(rfb._PRIMARY_DOC_SUFFIXES)
    )


def evaluate_candidate_submissions(
    *,
    cik: str,
    submissions: Mapping[str, Any],
    fetcher: MetadataOnlyFetcher,
    exclusions: Mapping[str, Any],
    selected_accessions: set[str],
    selected_documents: set[tuple[str, str]],
) -> dict[str, Any]:
    """Eligibility over an already-fetched submissions payload."""
    filings = submissions.get("filings") or {}
    rows = _ten_k_rows(filings.get("recent") or {})
    row_accessions = {
        row["accession_number"] for row in rows if row["accession_number"]
    }

    target_years = (TARGET_PREVIOUS_FISCAL_YEAR, TARGET_CURRENT_FISCAL_YEAR)

    # Paged history: only consulted when the recent block alone cannot hold
    # both target rows — a high-volume filer's 10-K rows fall out of `recent`.
    # An unreadable page is an exclusion, never a silently shorter history.
    pages = [
        (page or {}).get("name")
        for page in (filings.get("files") or [])
    ]
    pages = [name for name in pages if isinstance(name, str) and name]

    try:
        facts_payload = fetcher.get(companyfacts_url(cik))
    except HoldoutSelectionError:
        raise
    except Exception:  # noqa: BLE001
        return {"eligible": False, "reason": REASON_FISCAL_METADATA_UNREADABLE}
    fy_map = fiscal_year_accessions(facts_payload)

    for year in target_years:
        if not fy_map.get(year):
            return {"eligible": False, "reason": REASON_TARGET_FISCAL_YEAR_MISSING}
    for year in target_years:
        if len(fy_map[year]) != 1:
            return {"eligible": False, "reason": REASON_FISCAL_METADATA_AMBIGUOUS}

    previous_accn = next(iter(fy_map[TARGET_PREVIOUS_FISCAL_YEAR]))
    current_accn = next(iter(fy_map[TARGET_CURRENT_FISCAL_YEAR]))

    # Fetch paged history only if a designated accession is not already in the
    # recent rows.
    if not {previous_accn, current_accn} <= row_accessions:
        for name in pages:
            try:
                page_payload = fetcher.get(submissions_page_url(name))
            except HoldoutSelectionError:
                raise
            except Exception:  # noqa: BLE001
                return {
                    "eligible": False,
                    "reason": REASON_PAGED_HISTORY_UNREADABLE,
                }
            rows.extend(_ten_k_rows(page_payload))
            row_accessions = {
                row["accession_number"] for row in rows if row["accession_number"]
            }
            if {previous_accn, current_accn} <= row_accessions:
                break

    if not rows:
        return {"eligible": False, "reason": REASON_NO_10K_ROWS}

    by_accession = {
        row["accession_number"]: row for row in rows if row["accession_number"]
    }
    previous_row = by_accession.get(previous_accn)
    current_row = by_accession.get(current_accn)
    if previous_row is None or current_row is None:
        # The designated accession is not a form-10-K submissions row: either
        # the row is an amendment (form mismatch) or metadata disagrees.
        return {"eligible": False, "reason": REASON_TARGET_ROW_NOT_FOUND}

    if not (_complete_row(previous_row) and _complete_row(current_row)):
        return {"eligible": False, "reason": REASON_ROW_METADATA_INCOMPLETE}

    excluded_accessions = set(exclusions["excluded_accessions"])
    if {previous_accn, current_accn} & excluded_accessions:
        return {"eligible": False, "reason": REASON_DEVELOPMENT_CORPUS_ACCESSION}

    if not (
        previous_row["filing_date"] < current_row["filing_date"]
        and previous_row["reporting_period"] < current_row["reporting_period"]
    ):
        return {"eligible": False, "reason": REASON_CHRONOLOGY_INCONSISTENT}

    document_keys = {
        (cik, previous_row["primary_document"]),
        (cik, current_row["primary_document"]),
    }
    if (
        {previous_accn, current_accn} & selected_accessions
        or previous_accn == current_accn
        or document_keys & selected_documents
        or len(document_keys) != 2
    ):
        return {"eligible": False, "reason": REASON_DUPLICATE_SELECTION}

    issuer_name = str(submissions.get("name") or "").strip()
    if not issuer_name:
        return {"eligible": False, "reason": REASON_ROW_METADATA_INCOMPLETE}

    return {
        "eligible": True,
        "issuer_name": issuer_name,
        "previous_row": previous_row,
        "current_row": current_row,
        "previous_accession": previous_accn,
        "current_accession": current_accn,
    }


# --- Selection ------------------------------------------------------------------


def select_holdout(
    *,
    fetch_json: Callable[[str], Any],
    development_manifest: Mapping[str, Any] | None = None,
    now: Callable[[], str] = rfb.utc_now_iso,
) -> dict[str, Any]:
    """Execute the predeclared protocol over official metadata.

    Returns ``{"selected": bool, "manifest": ... | None, "report": ...}``.
    ``fetch_json`` is any ``url -> parsed JSON`` callable; every call is
    routed through the closed metadata allowlist, so handing this function a
    body-fetching transport cannot make it fetch a body.
    """
    fetcher = MetadataOnlyFetcher(fetch_json)
    exclusions = development_exclusions(development_manifest)
    excluded_ciks = set(exclusions["excluded_ciks"])

    universe = candidate_universe(fetcher.get(COMPANY_TICKERS_URL))
    universe_retrieved_at = now()

    quotas = {s["stratum_id"]: s["quota"] for s in SIC_STRATA}
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    exclusion_counts: dict[str, int] = {}
    selected_accessions: set[str] = set()
    selected_documents: set[tuple[str, str]] = set()
    probes = 0
    probe_budget_exhausted = False

    def note(reason: str) -> None:
        exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1

    # Phase A: stratified scan in universe order.
    for candidate in universe:
        if all(quota == 0 for quota in quotas.values()):
            break
        if probes >= MAX_SUBMISSIONS_PROBES:
            probe_budget_exhausted = True
            break
        cik = candidate["cik"]
        if cik in excluded_ciks:
            note(REASON_DEVELOPMENT_CORPUS_CIK)
            continue
        probes += 1
        try:
            submissions = fetcher.get(submissions_url(cik))
        except NonMetadataEndpoint:
            raise
        except Exception:  # noqa: BLE001
            note(REASON_SUBMISSIONS_UNREADABLE)
            continue
        sic_raw = submissions.get("sic")
        try:
            sic = int(str(sic_raw).strip())
        except (TypeError, ValueError):
            note(REASON_SIC_MISSING)
            continue
        stratum = stratum_for_sic(sic)
        if stratum is None:
            note(REASON_SIC_OUTSIDE_STRATA)
            continue
        if quotas[stratum["stratum_id"]] == 0:
            # Deferral is free: no further requests. The candidate keeps its
            # place in universe order for the predeclared fallback.
            deferred.append(
                {"cik": cik, "stratum": {**stratum, "_sic": sic}, "submissions": submissions}
            )
            note(REASON_STRATUM_FULL_DEFERRED)
            continue
        outcome = evaluate_candidate_submissions(
            cik=cik,
            submissions=submissions,
            fetcher=fetcher,
            exclusions=exclusions,
            selected_accessions=selected_accessions,
            selected_documents=selected_documents,
        )
        if not outcome["eligible"]:
            note(outcome["reason"])
            continue
        quotas[stratum["stratum_id"]] -= 1
        selected.append(
            {
                "cik": cik,
                "stratum_id": stratum["stratum_id"],
                "stratum_label": stratum["label"],
                "sic": sic,
                "issuer_name": outcome["issuer_name"],
                "previous_row": outcome["previous_row"],
                "current_row": outcome["current_row"],
                "selected_at": now(),
            }
        )
        selected_accessions.update(
            {outcome["previous_accession"], outcome["current_accession"]}
        )
        selected_documents.update(
            {
                (cik, outcome["previous_row"]["primary_document"]),
                (cik, outcome["current_row"]["primary_document"]),
            }
        )

    # Phase B: predeclared fallback over deferred candidates, universe order.
    if len(selected) < TARGET_PAIR_COUNT and not probe_budget_exhausted:
        for item in deferred:
            if len(selected) >= TARGET_PAIR_COUNT:
                break
            outcome = evaluate_candidate_submissions(
                cik=item["cik"],
                submissions=item["submissions"],
                fetcher=fetcher,
                exclusions=exclusions,
                selected_accessions=selected_accessions,
                selected_documents=selected_documents,
            )
            if not outcome["eligible"]:
                note(outcome["reason"])
                continue
            stratum = item["stratum"]
            selected.append(
                {
                    "cik": item["cik"],
                    "stratum_id": stratum["stratum_id"],
                    "stratum_label": stratum["label"],
                    "sic": stratum["_sic"],
                    "issuer_name": outcome["issuer_name"],
                    "previous_row": outcome["previous_row"],
                    "current_row": outcome["current_row"],
                    "selected_at": now(),
                    "fallback_absorbed": True,
                }
            )
            selected_accessions.update(
                {outcome["previous_accession"], outcome["current_accession"]}
            )
            selected_documents.update(
                {
                    (item["cik"], outcome["previous_row"]["primary_document"]),
                    (item["cik"], outcome["current_row"]["primary_document"]),
                }
            )

    distinct_strata = {entry["stratum_id"] for entry in selected}
    failures: list[dict[str, Any]] = []
    if probe_budget_exhausted:
        failures.append(
            {
                "code": SELECTION_FAILURE_PROBE_BUDGET,
                "detail": f"probe budget of {MAX_SUBMISSIONS_PROBES} exhausted",
            }
        )
    if len(selected) < TARGET_PAIR_COUNT:
        unfilled = sorted(
            stratum_id for stratum_id, quota in quotas.items() if quota > 0
        )
        failures.append(
            {
                "code": SELECTION_FAILURE_STRATUM_UNFILLED,
                "detail": (
                    f"{len(selected)} of {TARGET_PAIR_COUNT} pairs filled"
                ),
                "unfilled_strata": unfilled,
            }
        )
    if selected and len(distinct_strata) < min(
        MINIMUM_DISTINCT_STRATA, TARGET_PAIR_COUNT
    ):
        failures.append(
            {
                "code": SELECTION_FAILURE_INSUFFICIENT_STRATA,
                "detail": (
                    f"{len(distinct_strata)} distinct strata selected; "
                    f"{MINIMUM_DISTINCT_STRATA} required"
                ),
            }
        )

    succeeded = not failures and len(selected) == TARGET_PAIR_COUNT

    manifest = (
        build_manifest(
            selected,
            exclusions=exclusions,
            universe_retrieved_at=universe_retrieved_at,
            selected_at=now(),
        )
        if succeeded
        else None
    )
    report = build_selection_report(
        selected=selected,
        exclusions=exclusions,
        exclusion_counts=exclusion_counts,
        fetcher=fetcher,
        probes=probes,
        universe_size=len(universe),
        failures=failures,
        generated_at=now(),
    )
    return {"selected": succeeded, "manifest": manifest, "report": report}


# --- Manifest construction and validation ---------------------------------------

_HOLDOUT_MANIFEST_REQUIRED = (
    "schema_version",
    "benchmark_id",
    "benchmark_version",
    "status",
    "form",
    "target_pair_count",
    "corpus_role",
    "extraction_parser_developed_using_this_corpus",
    "extraction_holdout_evaluation",
    "generalization_claim_supported",
    "corpus_role_detail",
    "frozen_extraction_parser_version",
    "frozen_parser_source_path",
    "frozen_parser_source_sha256",
    "selection_protocol_version",
    "selection_protocol_hash",
    "development_exclusions",
    "metadata_snapshot",
    "selected_at",
    "pairs",
)
_HOLDOUT_MANIFEST_OPTIONAL = ("description",)

_HOLDOUT_PAIR_REQUIRED = (
    "pair_id",
    "issuer_name",
    "cik",
    "sic",
    "stratum_id",
    "stratum_label",
    "target_previous_fiscal_year",
    "target_current_fiscal_year",
    "metadata_source_references",
    "previous",
    "current",
)

_HOLDOUT_SIDE_REQUIRED = (
    "accession_number",
    "form",
    "filing_date",
    "reporting_period",
    "primary_document",
    "expected_sha256",
    "source_verified",
)


def frozen_parser_source_sha256(repo_root: str | Path | None = None) -> str:
    root = Path(repo_root or Path(__file__).resolve().parent)
    return rfb.sha256_file(root / FROZEN_PARSER_SOURCE_PATH)


def _pair_id_for(stratum_id: str, ordinal: int) -> str:
    return f"{stratum_id}-{ordinal:02d}"


def build_manifest(
    selected: list[Mapping[str, Any]],
    *,
    exclusions: Mapping[str, Any],
    universe_retrieved_at: str,
    selected_at: str,
) -> dict[str, Any]:
    """Assemble the frozen holdout manifest from selection payloads."""
    ordinals: dict[str, int] = {}
    pairs = []
    for entry in selected:
        ordinal = ordinals.get(entry["stratum_id"], 0) + 1
        ordinals[entry["stratum_id"]] = ordinal
        pairs.append(
            {
                "pair_id": _pair_id_for(entry["stratum_id"], ordinal),
                "issuer_name": entry["issuer_name"],
                "cik": entry["cik"],
                "sic": entry["sic"],
                "stratum_id": entry["stratum_id"],
                "stratum_label": entry["stratum_label"],
                "target_previous_fiscal_year": TARGET_PREVIOUS_FISCAL_YEAR,
                "target_current_fiscal_year": TARGET_CURRENT_FISCAL_YEAR,
                "metadata_source_references": [
                    submissions_url(entry["cik"]),
                    companyfacts_url(entry["cik"]),
                ],
                "previous": _manifest_side(entry["previous_row"]),
                "current": _manifest_side(entry["current_row"]),
            }
        )
    pairs.sort(key=lambda pair: pair["pair_id"])
    manifest = {
        "schema_version": HOLDOUT_MANIFEST_SCHEMA_VERSION,
        "benchmark_id": HOLDOUT_BENCHMARK_ID,
        "benchmark_version": HOLDOUT_BENCHMARK_VERSION,
        "status": STATUS_HOLDOUT_FROZEN_METADATA_ONLY,
        "form": rfb.MANIFEST_FORM,
        "target_pair_count": TARGET_PAIR_COUNT,
        **holdout_corpus_role_fields(),
        "frozen_extraction_parser_version": FROZEN_EXTRACTION_PARSER_VERSION,
        "frozen_parser_source_path": FROZEN_PARSER_SOURCE_PATH,
        "frozen_parser_source_sha256": frozen_parser_source_sha256(),
        "selection_protocol_version": HOLDOUT_SELECTION_PROTOCOL_VERSION,
        "selection_protocol_hash": selection_protocol_hash(),
        "development_exclusions": {
            "development_benchmark_id": exclusions["development_benchmark_id"],
            "excluded_ciks": list(exclusions["excluded_ciks"]),
            "excluded_accessions": list(exclusions["excluded_accessions"]),
        },
        "metadata_snapshot": {
            "company_tickers_retrieved_at": universe_retrieved_at,
            "selected_at": selected_at,
        },
        "selected_at": selected_at,
        "description": (
            "Metadata-only extraction holdout: exact issuers and filing "
            "pairs frozen from official SEC metadata AFTER "
            "sec_html_item_headings.v2 was frozen and BEFORE any selected "
            "filing body was downloaded or structurally inspected. No filing "
            "body has been acquired, no checksum exists, no extraction has "
            "run, and no accuracy or generalization result exists. A "
            "selected pair is never replaced because of later-observed "
            "filing-body structure, extraction outcome, detector output, or "
            "evaluation result."
        ),
        "pairs": pairs,
    }
    validate_holdout_manifest(manifest)
    return manifest


def _manifest_side(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "accession_number": row["accession_number"],
        "form": row["form"],
        "filing_date": row["filing_date"],
        "reporting_period": row["reporting_period"],
        "primary_document": row["primary_document"],
        # No body bytes exist, so no digest exists. null is the only honest
        # value here — the development schema's placeholder digest is not
        # reused because "pending verification" and "never fetched" are
        # different claims.
        "expected_sha256": None,
        "source_verified": False,
    }


def holdout_corpus_role_fields() -> dict[str, Any]:
    """Corpus-role block for a frozen-but-unevaluated holdout.

    ``extraction_holdout_evaluation`` and ``generalization_claim_supported``
    stay false: the corpus being *eligible* to support an out-of-sample claim
    is not the claim. Both flip only after bodies are acquired, extraction
    runs unchanged, and a human verifies labels — none of which has happened.
    """
    return {
        "corpus_role": rfb.CORPUS_ROLE_EXTRACTION_HOLDOUT,
        "extraction_parser_developed_using_this_corpus": False,
        "extraction_holdout_evaluation": False,
        "generalization_claim_supported": False,
        "corpus_role_detail": (
            "Issuers and exact filing pairs were frozen from official SEC "
            "metadata only, after the extraction parser was frozen and "
            "before any selected filing body was downloaded or inspected. "
            "No filing body has been acquired and no extraction has run, so "
            "no holdout evaluation exists yet and no generalization claim "
            "is supported. Modifying the frozen parser in response to "
            "future holdout results would convert this corpus into "
            "development data."
        ),
    }


def default_holdout_manifest_path() -> Path:
    return (
        Path(__file__).resolve().parent
        / "benchmarks"
        / HOLDOUT_BENCHMARK_ID
        / "manifest.json"
    )


def default_selection_report_path() -> Path:
    return default_holdout_manifest_path().parent / "selection_report.json"


def load_holdout_manifest(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path or default_holdout_manifest_path())
    if not target.exists():
        raise HoldoutManifestError(
            "holdout_manifest_not_found",
            f"holdout manifest not found: {target.name}",
        )
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HoldoutManifestError(
            "holdout_manifest_not_json", f"{target.name}: {exc}"
        ) from exc
    validate_holdout_manifest(document)
    return document


def holdout_manifest_hash(path: str | Path | None = None) -> str:
    return rfb.sha256_file(Path(path or default_holdout_manifest_path()))


def validate_holdout_manifest(document: Any) -> None:
    """Reject every documented holdout-manifest defect."""
    rfb._exact_keys(
        document,
        required=_HOLDOUT_MANIFEST_REQUIRED,
        optional=_HOLDOUT_MANIFEST_OPTIONAL,
        where="holdout manifest",
        error=HoldoutManifestError,
        code_prefix="holdout_manifest",
    )
    _require = rfb._require
    _require(
        document["schema_version"] == HOLDOUT_MANIFEST_SCHEMA_VERSION,
        HoldoutManifestError,
        "holdout_manifest_schema_version_mismatch",
        f"holdout manifest: schema_version must be "
        f"{HOLDOUT_MANIFEST_SCHEMA_VERSION!r}",
    )
    _require(
        document["benchmark_id"] == HOLDOUT_BENCHMARK_ID,
        HoldoutManifestError,
        "holdout_manifest_invalid_benchmark_id",
        f"holdout manifest: benchmark_id must be {HOLDOUT_BENCHMARK_ID!r}",
    )
    _require(
        document["benchmark_id"] != rfb.BENCHMARK_ID,
        HoldoutManifestError,
        "holdout_manifest_reuses_development_id",
        "holdout manifest: the holdout may never reuse the development "
        "benchmark id",
    )
    _require(
        document["benchmark_version"] == HOLDOUT_BENCHMARK_VERSION,
        HoldoutManifestError,
        "holdout_manifest_invalid_benchmark_version",
        f"holdout manifest: benchmark_version must be "
        f"{HOLDOUT_BENCHMARK_VERSION}",
    )
    _require(
        document["status"] in HOLDOUT_STATUS_ORDER,
        HoldoutManifestError,
        "holdout_manifest_invalid_status",
        f"holdout manifest: status must be one of {list(HOLDOUT_STATUS_ORDER)}",
    )
    _require(
        document["form"] == rfb.MANIFEST_FORM,
        HoldoutManifestError,
        "holdout_manifest_invalid_form",
        f"holdout manifest: v1 covers form {rfb.MANIFEST_FORM!r} only",
    )
    _require(
        document["target_pair_count"] == TARGET_PAIR_COUNT,
        HoldoutManifestError,
        "holdout_manifest_invalid_target_pair_count",
        f"holdout manifest: target_pair_count must be {TARGET_PAIR_COUNT}",
    )

    role_fields = holdout_corpus_role_fields()
    for field, expected in role_fields.items():
        if field == "corpus_role_detail":
            rfb._require_bounded_str(
                document[field],
                f"holdout manifest.{field}",
                max_chars=2000,
                error=HoldoutManifestError,
                code_prefix="holdout_manifest",
            )
            continue
        _require(
            document[field] == expected,
            HoldoutManifestError,
            "holdout_manifest_corpus_role_mismatch",
            f"holdout manifest.{field}: must be {expected!r} — a holdout "
            "manifest cannot claim parser development, a completed holdout "
            "evaluation, or a supported generalization claim",
        )

    _require(
        document["frozen_extraction_parser_version"]
        == FROZEN_EXTRACTION_PARSER_VERSION,
        HoldoutManifestError,
        "holdout_manifest_parser_version_mismatch",
        f"holdout manifest: frozen_extraction_parser_version must be "
        f"{FROZEN_EXTRACTION_PARSER_VERSION!r}",
    )
    _require(
        document["frozen_parser_source_path"] == FROZEN_PARSER_SOURCE_PATH,
        HoldoutManifestError,
        "holdout_manifest_parser_path_mismatch",
        f"holdout manifest: frozen_parser_source_path must be "
        f"{FROZEN_PARSER_SOURCE_PATH!r}",
    )
    _require(
        isinstance(document["frozen_parser_source_sha256"], str)
        and bool(rfb._SHA256_RE.match(document["frozen_parser_source_sha256"])),
        HoldoutManifestError,
        "holdout_manifest_invalid_parser_hash",
        "holdout manifest: frozen_parser_source_sha256 must be 64 lowercase "
        "hex characters",
    )
    _require(
        document["selection_protocol_version"]
        == HOLDOUT_SELECTION_PROTOCOL_VERSION,
        HoldoutManifestError,
        "holdout_manifest_invalid_selection_protocol",
        f"holdout manifest: selection_protocol_version must be "
        f"{HOLDOUT_SELECTION_PROTOCOL_VERSION!r}",
    )
    _require(
        document["selection_protocol_hash"] == selection_protocol_hash(),
        HoldoutManifestError,
        "holdout_manifest_protocol_hash_mismatch",
        "holdout manifest: selection_protocol_hash does not match the "
        "protocol in this repository — the protocol may not change after "
        "the selection it produced",
    )

    exclusions = document["development_exclusions"]
    rfb._exact_keys(
        exclusions,
        required=(
            "development_benchmark_id",
            "excluded_ciks",
            "excluded_accessions",
        ),
        where="holdout manifest.development_exclusions",
        error=HoldoutManifestError,
        code_prefix="holdout_manifest_exclusions",
    )
    excluded_ciks = exclusions["excluded_ciks"]
    excluded_accessions = exclusions["excluded_accessions"]
    _require(
        isinstance(excluded_ciks, list) and excluded_ciks,
        HoldoutManifestError,
        "holdout_manifest_exclusions_empty",
        "holdout manifest: excluded_ciks must be a non-empty list",
    )
    _require(
        isinstance(excluded_accessions, list) and excluded_accessions,
        HoldoutManifestError,
        "holdout_manifest_exclusions_empty",
        "holdout manifest: excluded_accessions must be a non-empty list",
    )

    snapshot = document["metadata_snapshot"]
    rfb._exact_keys(
        snapshot,
        required=("company_tickers_retrieved_at", "selected_at"),
        where="holdout manifest.metadata_snapshot",
        error=HoldoutManifestError,
        code_prefix="holdout_manifest_snapshot",
    )
    for field in ("company_tickers_retrieved_at", "selected_at"):
        rfb._require_iso_timestamp(
            snapshot[field],
            f"holdout manifest.metadata_snapshot.{field}",
            HoldoutManifestError,
            "holdout_manifest",
        )
    rfb._require_iso_timestamp(
        document["selected_at"],
        "holdout manifest.selected_at",
        HoldoutManifestError,
        "holdout_manifest",
    )
    if "description" in document:
        rfb._require_bounded_str(
            document["description"],
            "holdout manifest.description",
            max_chars=2000,
            error=HoldoutManifestError,
            code_prefix="holdout_manifest",
        )

    _validate_holdout_pairs(document, set(excluded_ciks), set(excluded_accessions))


def _validate_holdout_pairs(
    document: Mapping[str, Any],
    excluded_ciks: set[str],
    excluded_accessions: set[str],
) -> None:
    _require = rfb._require
    pairs = document["pairs"]
    _require(
        isinstance(pairs, list) and len(pairs) == TARGET_PAIR_COUNT,
        HoldoutManifestError,
        "holdout_manifest_pair_count_mismatch",
        f"holdout manifest: exactly {TARGET_PAIR_COUNT} frozen pairs are "
        f"required, got {len(pairs) if isinstance(pairs, list) else 'non-list'}",
    )
    strata_by_id = {s["stratum_id"]: s for s in SIC_STRATA}
    seen_pair_ids: set[str] = set()
    seen_ciks: set[str] = set()
    seen_names: set[str] = set()
    seen_accessions: set[str] = set()
    seen_documents: set[tuple[str, str]] = set()
    distinct_strata: set[str] = set()
    previous_pair_id = ""
    for index, pair in enumerate(pairs):
        where = f"holdout manifest.pairs[{index}]"
        rfb._exact_keys(
            pair,
            required=_HOLDOUT_PAIR_REQUIRED,
            where=where,
            error=HoldoutManifestError,
            code_prefix="holdout_pair",
        )
        pair_id = pair["pair_id"]
        _require(
            isinstance(pair_id, str) and bool(rfb._ID_RE.match(pair_id)),
            HoldoutManifestError,
            "holdout_pair_invalid_id",
            f"{where}: pair_id must be a lowercase slug",
        )
        _require(
            pair_id not in seen_pair_ids,
            HoldoutManifestError,
            "holdout_pair_duplicate_id",
            f"{where}: duplicate pair_id {pair_id!r}",
        )
        seen_pair_ids.add(pair_id)
        _require(
            pair_id > previous_pair_id,
            HoldoutManifestError,
            "holdout_pairs_unordered",
            f"{where}: pairs must be sorted by pair_id",
        )
        previous_pair_id = pair_id

        cik = pair["cik"]
        _require(
            isinstance(cik, str) and bool(rfb._CIK_RE.match(cik)),
            HoldoutManifestError,
            "holdout_pair_invalid_cik",
            f"{where}: cik must be a 10-digit zero-padded string",
        )
        _require(
            cik not in seen_ciks,
            HoldoutManifestError,
            "holdout_pair_duplicate_cik",
            f"{where}: duplicate cik {cik!r}",
        )
        seen_ciks.add(cik)
        _require(
            cik not in excluded_ciks,
            HoldoutManifestError,
            "holdout_pair_development_cik",
            f"{where}: cik {cik!r} belongs to the extraction-development "
            "corpus and can never enter the holdout",
        )

        name = rfb._require_bounded_str(
            pair["issuer_name"],
            f"{where}.issuer_name",
            max_chars=200,
            error=HoldoutManifestError,
            code_prefix="holdout_pair",
        )
        _require(
            name not in seen_names,
            HoldoutManifestError,
            "holdout_pair_duplicate_issuer",
            f"{where}: issuer_name {name!r} appears more than once",
        )
        seen_names.add(name)

        sic = pair["sic"]
        _require(
            isinstance(sic, int) and not isinstance(sic, bool),
            HoldoutManifestError,
            "holdout_pair_invalid_sic",
            f"{where}: sic must be an integer SIC code",
        )
        stratum = strata_by_id.get(pair["stratum_id"])
        _require(
            stratum is not None,
            HoldoutManifestError,
            "holdout_pair_unknown_stratum",
            f"{where}: stratum_id {pair['stratum_id']!r} is not a declared "
            "stratum",
        )
        low, high = stratum["sic_range"]
        _require(
            low <= sic <= high,
            HoldoutManifestError,
            "holdout_pair_sic_outside_stratum",
            f"{where}: sic {sic} is outside the declared range of "
            f"{pair['stratum_id']!r} [{low}, {high}]",
        )
        _require(
            pair["stratum_label"] == stratum["label"],
            HoldoutManifestError,
            "holdout_pair_stratum_label_mismatch",
            f"{where}: stratum_label must match the declared stratum",
        )
        distinct_strata.add(pair["stratum_id"])

        _require(
            pair["target_previous_fiscal_year"] == TARGET_PREVIOUS_FISCAL_YEAR
            and pair["target_current_fiscal_year"] == TARGET_CURRENT_FISCAL_YEAR,
            HoldoutManifestError,
            "holdout_pair_fiscal_years_mismatch",
            f"{where}: target fiscal years must be the protocol's "
            f"{TARGET_PREVIOUS_FISCAL_YEAR} -> {TARGET_CURRENT_FISCAL_YEAR}",
        )

        references = pair["metadata_source_references"]
        _require(
            isinstance(references, list) and references,
            HoldoutManifestError,
            "holdout_pair_missing_metadata_references",
            f"{where}: metadata_source_references must be a non-empty list",
        )
        for reference in references:
            # Every recorded reference must itself be a metadata endpoint —
            # a body URL in the provenance trail would mean a body was read.
            try:
                require_metadata_url(reference)
            except NonMetadataEndpoint:
                raise HoldoutManifestError(
                    "holdout_pair_non_metadata_reference",
                    f"{where}: {reference!r} is not a metadata endpoint",
                ) from None

        previous = _validate_holdout_side(pair, "previous", where, document["status"])
        current = _validate_holdout_side(pair, "current", where, document["status"])
        _require(
            previous["filing_date"] < current["filing_date"],
            HoldoutManifestError,
            "holdout_pair_filing_dates_unordered",
            f"{where}: previous.filing_date must be strictly before "
            "current.filing_date",
        )
        _require(
            previous["reporting_period"] < current["reporting_period"],
            HoldoutManifestError,
            "holdout_pair_periods_unordered",
            f"{where}: previous.reporting_period must be strictly before "
            "current.reporting_period",
        )
        for side_name, parsed in (("previous", previous), ("current", current)):
            accession = parsed["accession_number"]
            _require(
                accession not in excluded_accessions,
                HoldoutManifestError,
                "holdout_pair_development_accession",
                f"{where}.{side_name}: accession {accession!r} belongs to the "
                "extraction-development corpus and can never enter the holdout",
            )
            _require(
                accession not in seen_accessions,
                HoldoutManifestError,
                "holdout_pair_duplicate_accession",
                f"{where}.{side_name}: accession {accession!r} already "
                "appears in this manifest",
            )
            seen_accessions.add(accession)
            document_key = (cik, parsed["primary_document"])
            _require(
                document_key not in seen_documents,
                HoldoutManifestError,
                "holdout_pair_duplicate_primary_document",
                f"{where}.{side_name}: primary document "
                f"{parsed['primary_document']!r} already appears for this cik",
            )
            seen_documents.add(document_key)

    _require(
        len(distinct_strata) >= min(MINIMUM_DISTINCT_STRATA, TARGET_PAIR_COUNT),
        HoldoutManifestError,
        "holdout_manifest_insufficient_strata",
        f"holdout manifest: pairs must span at least "
        f"{MINIMUM_DISTINCT_STRATA} distinct SIC strata, got "
        f"{len(distinct_strata)}",
    )


def _validate_holdout_side(
    pair: Mapping[str, Any], side: str, where: str, status: str
) -> dict[str, Any]:
    _require = rfb._require
    payload = pair[side]
    side_where = f"{where}.{side}"
    rfb._exact_keys(
        payload,
        required=_HOLDOUT_SIDE_REQUIRED,
        where=side_where,
        error=HoldoutManifestError,
        code_prefix="holdout_side",
    )
    _require(
        payload["form"] == rfb.MANIFEST_FORM,
        HoldoutManifestError,
        "holdout_side_invalid_form",
        f"{side_where}: form must be exactly {rfb.MANIFEST_FORM!r} — "
        "amendments such as '10-K/A' are excluded and are never substitutions",
    )
    accession = payload["accession_number"]
    _require(
        isinstance(accession, str) and bool(rfb._ACCESSION_RE.match(accession)),
        HoldoutManifestError,
        "holdout_side_invalid_accession",
        f"{side_where}: accession_number must look like NNNNNNNNNN-NN-NNNNNN",
    )
    filing_date = rfb._require_iso_date(
        payload["filing_date"],
        f"{side_where}.filing_date",
        HoldoutManifestError,
        "holdout_side",
    )
    reporting_period = rfb._require_iso_date(
        payload["reporting_period"],
        f"{side_where}.reporting_period",
        HoldoutManifestError,
        "holdout_side",
    )
    _require(
        reporting_period <= filing_date,
        HoldoutManifestError,
        "holdout_side_period_after_filing",
        f"{side_where}: reporting_period cannot be after filing_date",
    )
    document_name = payload["primary_document"]
    _require(
        isinstance(document_name, str)
        and bool(rfb._PRIMARY_DOC_RE.match(document_name))
        and document_name.lower().endswith(rfb._PRIMARY_DOC_SUFFIXES),
        HoldoutManifestError,
        "holdout_side_invalid_primary_document",
        f"{side_where}: primary_document must be a plain file name ending in "
        f"one of {list(rfb._PRIMARY_DOC_SUFFIXES)}",
    )
    if status == STATUS_HOLDOUT_FROZEN_METADATA_ONLY:
        # Metadata-only means exactly that: no bytes, no digest, nothing
        # verified. A digest appearing here would claim an acquisition that
        # never happened.
        _require(
            payload["expected_sha256"] is None,
            HoldoutManifestError,
            "holdout_side_unexpected_sha256",
            f"{side_where}: expected_sha256 must be null while the manifest "
            f"is {STATUS_HOLDOUT_FROZEN_METADATA_ONLY!r}; no filing body has "
            "been acquired, so no digest can exist",
        )
        _require(
            payload["source_verified"] is False,
            HoldoutManifestError,
            "holdout_side_claims_verification",
            f"{side_where}: source_verified must be false while the manifest "
            f"is {STATUS_HOLDOUT_FROZEN_METADATA_ONLY!r}",
        )
    else:
        _require(
            isinstance(payload["expected_sha256"], str)
            and bool(rfb._SHA256_RE.match(payload["expected_sha256"])),
            HoldoutManifestError,
            "holdout_side_invalid_sha256",
            f"{side_where}: a manifest beyond metadata-only must carry a "
            "real digest",
        )
    return {
        "accession_number": accession,
        "filing_date": filing_date,
        "reporting_period": reporting_period,
        "primary_document": document_name,
    }


def validate_holdout_status_transition(current: str, target: str) -> None:
    """One documented forward step at a time, mirroring the development ladder."""
    _require = rfb._require
    for name, value in (("current", current), ("target", target)):
        _require(
            value in HOLDOUT_STATUS_ORDER,
            rfb.StatusTransitionError,
            "unknown_status",
            f"{name} status {value!r} is not one of {list(HOLDOUT_STATUS_ORDER)}",
        )
    current_index = HOLDOUT_STATUS_ORDER.index(current)
    target_index = HOLDOUT_STATUS_ORDER.index(target)
    _require(
        target_index != current_index,
        rfb.StatusTransitionError,
        "status_unchanged",
        f"status is already {current!r}",
    )
    _require(
        target_index > current_index,
        rfb.StatusTransitionError,
        "status_regression",
        f"a holdout manifest cannot move backwards from {current!r} to "
        f"{target!r}",
    )
    _require(
        target_index == current_index + 1,
        rfb.StatusTransitionError,
        "status_skipped",
        f"{current!r} -> {target!r} skips "
        f"{list(HOLDOUT_STATUS_ORDER[current_index + 1:target_index])}",
    )


# --- Selection audit report ------------------------------------------------------


def build_selection_report(
    *,
    selected: list[Mapping[str, Any]],
    exclusions: Mapping[str, Any],
    exclusion_counts: Mapping[str, int],
    fetcher: MetadataOnlyFetcher,
    probes: int,
    universe_size: int,
    failures: list[Mapping[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    """Bounded audit report: identities, counts, and hashes — never filing
    content, never a local path, never a credential."""
    stratum_counts: dict[str, int] = {}
    for entry in selected:
        stratum_counts[entry["stratum_id"]] = (
            stratum_counts.get(entry["stratum_id"], 0) + 1
        )
    non_metadata = {
        label: count
        for label, count in fetcher.request_counts.items()
        if label not in ("company_tickers", "submissions", "companyfacts")
    }
    return {
        "report_version": HOLDOUT_SELECTION_REPORT_VERSION,
        "benchmark_id": HOLDOUT_BENCHMARK_ID,
        "benchmark_version": HOLDOUT_BENCHMARK_VERSION,
        "generated_at": generated_at,
        "selection_succeeded": not failures and len(selected) == TARGET_PAIR_COUNT,
        "failures": [dict(item) for item in failures],
        "selection_protocol_version": HOLDOUT_SELECTION_PROTOCOL_VERSION,
        "selection_protocol_hash": selection_protocol_hash(),
        "selection_protocol": selection_protocol(),
        "frozen_extraction_parser_version": FROZEN_EXTRACTION_PARSER_VERSION,
        "frozen_parser_source_sha256": frozen_parser_source_sha256(),
        **holdout_corpus_role_fields(),
        "candidate_universe_size": universe_size,
        "submissions_probes": probes,
        "selected_pair_count": len(selected),
        "selected_pairs": [
            {
                "cik": entry["cik"],
                "issuer_name": entry["issuer_name"],
                "sic": entry["sic"],
                "stratum_id": entry["stratum_id"],
                "previous_accession": entry["previous_row"]["accession_number"],
                "current_accession": entry["current_row"]["accession_number"],
                "fallback_absorbed": bool(entry.get("fallback_absorbed")),
            }
            for entry in selected
        ],
        "stratum_distribution": dict(sorted(stratum_counts.items())),
        "exclusion_counts": dict(sorted(exclusion_counts.items())),
        "development_exclusions_applied": {
            "development_benchmark_id": exclusions["development_benchmark_id"],
            "excluded_cik_count": len(exclusions["excluded_ciks"]),
            "excluded_accession_count": len(exclusions["excluded_accessions"]),
            "excluded_ciks": list(exclusions["excluded_ciks"]),
            "excluded_accessions": list(exclusions["excluded_accessions"]),
        },
        "metadata_endpoints_contacted": {
            "company_tickers": fetcher.request_counts.get("company_tickers", 0),
            "submissions": fetcher.request_counts.get("submissions", 0),
            "companyfacts": fetcher.request_counts.get("companyfacts", 0),
        },
        # Structurally zero: the allowlist rejects everything else before a
        # transport is consulted, so a nonzero value here cannot occur in a
        # completed run — the field exists so a reader can check the claim.
        "filing_body_requests": sum(non_metadata.values()),
        "source_documents_downloaded": 0,
        "source_checksums_verified": 0,
        "extraction_runs": 0,
        "comparison_runs": 0,
        "annotation_packets": 0,
        "human_verified_labels": 0,
    }
