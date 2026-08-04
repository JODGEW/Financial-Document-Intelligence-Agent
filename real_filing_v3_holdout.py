"""Metadata-only v3 extraction holdout selection (``real_filing_v3_holdout_v1``).

Stage 3.5. Both prior real-filing corpora are spent as v3 evidence:
``real_filing_v1`` is the extraction DEVELOPMENT corpus, and
``real_filing_holdout_v1`` — the first frozen holdout — was blind-extracted,
human-annotated, and gold-evaluated under the v2 contracts, and its failure
modes were then used to design ``item1a_units.v3`` and the contract-v2 gold
evaluator. Its bodies have been observed, so it is v3 development data. A
future v3 generalization claim therefore needs a corpus whose exact filings
were frozen AFTER both the v3 unit representation (``item1a_units.v3`` /
``item1a_detector.v3`` / ``comparison_workflow.v3``) and the v2 evaluation
contract (``real-filing-benchmark.evaluation.v2`` +
``real-filing-benchmark-metrics.v2`` + ``real-filing-benchmark.report.v2``)
were merged and frozen, and BEFORE any selected filing body was downloaded or
structurally inspected. This module is that freeze.

The one rule this module exists to enforce
------------------------------------------
No filing body is contacted, downloaded, or inspected during selection. The
selection may read issuer identity, SIC codes, filing lists, and the filer's
own XBRL fiscal-year designations — never a primary document, never HTML
structure, never Item 1A content, never an extraction, detector, workflow, or
evaluator outcome. That is structural: every URL passes the same CLOSED
metadata allowlist as the first holdout (``real_filing_holdout
.require_metadata_url``), which contains no Archives, primary-document,
exhibit, or inline-XBRL pattern.

What is different from the first holdout's protocol
---------------------------------------------------
- **Both prior corpora are excluded**, by CIK and independently by accession,
  derived at run time from the two committed manifests (never retyped), with
  each source manifest's sha256 frozen into the produced manifest so a later
  edit of either exclusion source is detectable.
- **Hash-ranked candidate order** replaces ascending-CIK order. The first
  holdout's ascending-CIK scan selected only the earliest-registered issuers
  (every selected CIK below 0000010000). Candidates are now ranked by
  ``rank_key = SHA-256("real_filing_v3_holdout_v1|" + zero-padded CIK)``,
  ascending, tie-broken by CIK then registrant title. The seed prefix is the
  benchmark id, fixed before any candidate was resolved; there is no runtime
  seed input and no randomness.
- **Target fiscal years are filer-designated FY2024 -> FY2025** (XBRL
  ``fy``/``fp`` on official companyfacts rows, never a period-end-year
  heuristic).
- **The frozen-code identity block pins the whole v3 measurement chain**, not
  just the section parser: parser, unit grammar, detector, workflow, and the
  contract-v2 evaluator, each by version string and (for source files) by
  sha256, so post-freeze drift in any of them is a checkable fact.

What this module deliberately cannot say
----------------------------------------
- ``source_verified`` is false and ``expected_sha256`` is null on every side:
  no body bytes exist, so no checksum exists.
- ``extraction_holdout_evaluation`` and ``generalization_claim_supported``
  are false: nothing has been extracted, detected, annotated, or scored.
- A selected issuer or pair is NEVER replaced because of anything observed
  later. Replacing one — or modifying any pinned frozen file in response to
  future results from this corpus — converts the holdout into development
  data; the recorded hashes make that conversion detectable.

Like the first holdout module, this module imports nothing that can open a
socket; transport is injected by the caller and every fetch passes the closed
metadata allowlist first. Tests assert that at the import graph.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

import real_filing_benchmark as rfb
import real_filing_holdout as rfh

# --- Versions and identity ----------------------------------------------------

V3_HOLDOUT_MANIFEST_SCHEMA_VERSION = "real-filing-v3-holdout.manifest.v1"
V3_HOLDOUT_SELECTION_PROTOCOL_VERSION = "real-filing-v3-holdout-selection.v1"
V3_HOLDOUT_SELECTION_REPORT_VERSION = "real-filing-v3-holdout.selection-report.v1"
V3_HOLDOUT_BENCHMARK_ID = "real_filing_v3_holdout_v1"
V3_HOLDOUT_BENCHMARK_VERSION = 1

#: Fixed seed identifier for the deterministic candidate ranking. It is the
#: benchmark id on purpose: declared before any candidate was resolved,
#: committed with the protocol, and impossible to vary per run — the selection
#: functions take no seed parameter.
SELECTION_SEED_IDENTIFIER = V3_HOLDOUT_BENCHMARK_ID

TARGET_PREVIOUS_FISCAL_YEAR = 2024
TARGET_CURRENT_FISCAL_YEAR = 2025

# --- Frozen v3/v2 contract identities ------------------------------------------
# Literals on purpose, exactly like the first holdout's parser pin: importing
# ``loaders``, ``comparison_detector``, ``comparison_store``, or the evaluator
# script would pull extraction, storage, or config graphs into a module whose
# contract is "no extraction import required". Tests pin every literal to the
# live constant it freezes.

FROZEN_EXTRACTION_PARSER_VERSION = "sec_html_item_headings.v2"
FROZEN_PARSER_SOURCE_PATH = "loaders/sec_headings.py"
FROZEN_UNIT_GRAMMAR_VERSION = "item1a_units.v3"
FROZEN_DETECTOR_VERSION = "item1a_detector.v3"
FROZEN_DETECTOR_SOURCE_PATH = "comparison_detector.py"
FROZEN_WORKFLOW_VERSION = "comparison_workflow.v3"
FROZEN_WORKFLOW_SOURCE_PATH = "comparison_store.py"
FROZEN_EVALUATION_CONTRACT_VERSION = "real-filing-benchmark.evaluation.v2"
FROZEN_METRIC_DEFINITIONS_VERSION = "real-filing-benchmark-metrics.v2"
FROZEN_REPORT_CONTRACT_VERSION = "real-filing-benchmark.report.v2"
FROZEN_EVALUATOR_SOURCE_PATH = "scripts/eval_real_filing_benchmark.py"
#: The contract-v2 subject-matching mode and the canonical sequence-aware
#: unit-identity shape it matches by.
FROZEN_SUBJECT_MATCHING = "canonical_unit_identity"
FROZEN_UNIT_IDENTITY_CONTRACT = "side:sequence:unit_key"
FROZEN_ANNOTATION_SCHEMA_VERSION = "real-filing-benchmark.annotation.v1"
FROZEN_ANNOTATION_PROTOCOL_VERSION = "real-filing-annotation.v1"

#: Repository files whose bytes the manifest freezes, as (manifest hash field,
#: manifest path field, repository path). Order is the manifest's field order.
FROZEN_SOURCE_FILES = (
    ("frozen_parser_source_sha256", "frozen_parser_source_path", FROZEN_PARSER_SOURCE_PATH),
    ("frozen_detector_source_sha256", "frozen_detector_source_path", FROZEN_DETECTOR_SOURCE_PATH),
    ("frozen_workflow_source_sha256", "frozen_workflow_source_path", FROZEN_WORKFLOW_SOURCE_PATH),
    ("frozen_evaluator_source_sha256", "frozen_evaluator_source_path", FROZEN_EVALUATOR_SOURCE_PATH),
)

# --- Manifest maturity ladder ---------------------------------------------------
# Identical ladder to the first holdout: born metadata-only, each later stage a
# single explicit forward step asserting new artifacts.

STATUS_HOLDOUT_FROZEN_METADATA_ONLY = rfh.STATUS_HOLDOUT_FROZEN_METADATA_ONLY
V3_HOLDOUT_STATUS_ORDER = rfh.HOLDOUT_STATUS_ORDER

# --- The predeclared selection protocol -----------------------------------------

#: Closed SIC ranges, redeclared here (not imported from the first holdout's
#: protocol) so this protocol is self-contained and its hash cannot move if
#: another module's does. Values are intentionally identical to the first
#: holdout's strata.
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
#: Hard bound on submissions-endpoint probes — the repository's existing
#: bounded value, unchanged. Exhausting it is a FAILED selection, never a
#: quietly smaller corpus, and it may not be raised inside a canonical run.
MAX_SUBMISSIONS_PROBES = 500

# Stable per-candidate reason codes, in the exact order the checks run.
REASON_PRIOR_CORPUS_CIK = "prior_corpus_cik"
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
REASON_PRIOR_CORPUS_ACCESSION = "prior_corpus_accession"
REASON_CHRONOLOGY_INCONSISTENT = "chronology_inconsistent"
REASON_DUPLICATE_SELECTION = "duplicate_selection"

SELECTION_FAILURE_STRATUM_UNFILLED = "stratum_quota_unfilled"
SELECTION_FAILURE_PROBE_BUDGET = "submissions_probe_budget_exhausted"
SELECTION_FAILURE_INSUFFICIENT_STRATA = "insufficient_distinct_strata"


def selection_protocol() -> dict[str, Any]:
    """The full machine-readable protocol, exactly as executed."""
    return {
        "protocol_version": V3_HOLDOUT_SELECTION_PROTOCOL_VERSION,
        "benchmark_id": V3_HOLDOUT_BENCHMARK_ID,
        "form": rfb.MANIFEST_FORM,
        "target_pair_count": TARGET_PAIR_COUNT,
        "target_filing_count": TARGET_PAIR_COUNT * 2,
        "target_previous_fiscal_year": TARGET_PREVIOUS_FISCAL_YEAR,
        "target_current_fiscal_year": TARGET_CURRENT_FISCAL_YEAR,
        "fiscal_year_rule": (
            "A target row is the unique form 10-K accession whose official "
            "companyfacts fact rows designate fp='FY' and fy=<target year>. "
            "The filer's own XBRL designation decides the fiscal year; a "
            "period-end-year heuristic is never used. The target years are "
            "fixed at FY2024 -> FY2025 and may not be switched, mixed, or "
            "widened if the corpus cannot fill: that is a FAILED selection."
        ),
        "candidate_universe": (
            "Unique CIKs from the official company_tickers.json registrant "
            "list, ranked by rank_key ascending (lowercase hex), tie-broken "
            "by normalized 10-digit CIK ascending, then normalized registrant "
            "title ascending. The ranking is reproducible from the metadata "
            "universe alone, independent of any filing body, and replaces the "
            "first holdout's ascending-CIK order, which was biased toward the "
            "earliest-registered issuers."
        ),
        "selection_seed_identifier": SELECTION_SEED_IDENTIFIER,
        "rank_key_definition": (
            "SHA-256 over UTF-8 of '<selection_seed_identifier>|<cik>' where "
            "<cik> is the zero-padded 10-digit CIK; digest compared as "
            "lowercase hex. The seed identifier is this benchmark's id, fixed "
            "before candidate resolution; no runtime seed input exists and no "
            "randomness is used."
        ),
        "sic_strata": [dict(stratum) for stratum in SIC_STRATA],
        "minimum_distinct_strata": MINIMUM_DISTINCT_STRATA,
        "max_submissions_probes": MAX_SUBMISSIONS_PROBES,
        "prior_corpus_exclusion_rule": (
            "Every CIK and, independently, every accession appearing in the "
            "committed real_filing_v1 development manifest or the committed "
            "real_filing_holdout_v1 holdout manifest is excluded. Exclusions "
            "are derived from the committed manifests at run time, never "
            "retyped, and the sha256 of each source manifest is frozen into "
            "the produced manifest so later modification of either exclusion "
            "source is detectable. Excluded CIKs are skipped before any "
            "metadata probe is spent."
        ),
        "eligibility_check_order": [
            REASON_PRIOR_CORPUS_CIK,
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
            REASON_PRIOR_CORPUS_ACCESSION,
            REASON_CHRONOLOGY_INCONSISTENT,
            REASON_DUPLICATE_SELECTION,
        ],
        "fallback_rule": (
            "Deferred candidates (stratum quota already full at probe time) "
            "are consumed in rank order to absorb slots their home stratum "
            "could not fill; an absorbed slot is recorded under the absorbing "
            "candidate's own stratum. If fewer than target_pair_count pairs "
            "fill, or fewer than minimum_distinct_strata distinct strata "
            "result, the selection FAILS with per-stratum reason codes. The "
            "corpus size is never silently reduced and the protocol is never "
            "altered after partial resolution."
        ),
        "replacement_rule": (
            "A selected issuer or filing pair is never replaced, reordered, "
            "or dropped because of later-observed filing-body structure, "
            "extraction outcome, detector output, workflow output, "
            "annotation difficulty, or evaluation result."
        ),
        "body_access_rule": (
            "Selection may contact ONLY the closed metadata endpoint "
            "allowlist (registrant list, submissions incl. paged history, "
            "companyfacts). Filing primary documents, Archives paths, "
            "exhibits, inline XBRL documents, and every other filing-body "
            "resource are outside it and are never contacted."
        ),
    }


def selection_protocol_hash() -> str:
    return rfb.payload_hash(selection_protocol())


# --- Metadata endpoint allowlist ------------------------------------------------
# The SAME closed allowlist as the first holdout, reused by reference so a
# second implementation cannot drift open. Everything outside the three
# metadata patterns — most importantly every www.sec.gov/Archives/... URL — is
# rejected before a transport is consulted.

require_metadata_url = rfh.require_metadata_url
endpoint_class = rfh.endpoint_class
submissions_url = rfh.submissions_url
submissions_page_url = rfh.submissions_page_url
companyfacts_url = rfh.companyfacts_url
COMPANY_TICKERS_URL = rfh.COMPANY_TICKERS_URL
MetadataOnlyFetcher = rfh.MetadataOnlyFetcher
NonMetadataEndpoint = rfh.NonMetadataEndpoint


class V3HoldoutSelectionError(rfb.BenchmarkError):
    """A bounded, code-carrying v3-holdout-selection failure. Never carries
    filing content, credentials, or local paths."""


class V3HoldoutManifestError(rfb.BenchmarkError):
    """The v3 holdout manifest is invalid, incomplete, or self-contradictory."""


# --- Prior-corpus exclusions ----------------------------------------------------

DEVELOPMENT_MANIFEST_PATH = "benchmarks/real_filing_v1/manifest.json"
FIRST_HOLDOUT_MANIFEST_PATH = "benchmarks/real_filing_holdout_v1/manifest.json"


def _holdout_ciks_and_accessions(
    holdout_manifest: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    ciks = sorted(
        {pair["cik"] for pair in holdout_manifest["pairs"] if pair.get("cik")}
    )
    accessions: set[str] = set()
    for pair in holdout_manifest["pairs"]:
        for side in ("previous", "current"):
            accessions.add(pair[side]["accession_number"])
    return ciks, sorted(accessions)


def prior_corpus_exclusions(
    *,
    development_manifest: Mapping[str, Any] | None = None,
    holdout_manifest: Mapping[str, Any] | None = None,
    development_manifest_sha256: str | None = None,
    holdout_manifest_sha256: str | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """CIKs and accessions of BOTH prior corpora, derived from the committed
    manifests rather than retyped, with each source manifest's sha256.

    When a manifest is injected (tests) without an explicit digest, the digest
    is the canonical payload hash of the injected document — still a drift
    detector, just over the injected content instead of committed bytes.
    """
    root = Path(repo_root or Path(__file__).resolve().parent)

    if development_manifest is None:
        development_manifest = rfb.load_manifest()
        if development_manifest_sha256 is None:
            development_manifest_sha256 = rfb.sha256_file(
                root / DEVELOPMENT_MANIFEST_PATH
            )
    if development_manifest_sha256 is None:
        development_manifest_sha256 = rfb.payload_hash(dict(development_manifest))

    if holdout_manifest is None:
        holdout_manifest = rfh.load_holdout_manifest()
        if holdout_manifest_sha256 is None:
            holdout_manifest_sha256 = rfb.sha256_file(
                root / FIRST_HOLDOUT_MANIFEST_PATH
            )
    if holdout_manifest_sha256 is None:
        holdout_manifest_sha256 = rfb.payload_hash(dict(holdout_manifest))

    development = rfh.development_exclusions(development_manifest)
    holdout_ciks, holdout_accessions = _holdout_ciks_and_accessions(
        holdout_manifest
    )
    return {
        "sources": [
            {
                "benchmark_id": development["development_benchmark_id"],
                "manifest_path": DEVELOPMENT_MANIFEST_PATH,
                "manifest_sha256": development_manifest_sha256,
                "excluded_ciks": list(development["excluded_ciks"]),
                "excluded_accessions": list(development["excluded_accessions"]),
            },
            {
                "benchmark_id": holdout_manifest["benchmark_id"],
                "manifest_path": FIRST_HOLDOUT_MANIFEST_PATH,
                "manifest_sha256": holdout_manifest_sha256,
                "excluded_ciks": holdout_ciks,
                "excluded_accessions": holdout_accessions,
            },
        ]
    }


def merged_excluded_ciks(exclusions: Mapping[str, Any]) -> set[str]:
    merged: set[str] = set()
    for source in exclusions["sources"]:
        merged.update(source["excluded_ciks"])
    return merged


def merged_excluded_accessions(exclusions: Mapping[str, Any]) -> set[str]:
    merged: set[str] = set()
    for source in exclusions["sources"]:
        merged.update(source["excluded_accessions"])
    return merged


# --- Candidate universe ---------------------------------------------------------


def rank_key(cik: str) -> str:
    """Deterministic hash rank for one normalized 10-digit CIK."""
    seed = f"{SELECTION_SEED_IDENTIFIER}|{cik}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def candidate_universe(tickers_payload: Any) -> list[dict[str, str]]:
    """Hash-ranked candidate ordering from the official registrant list.

    One entry per unique CIK, ordered by ``rank_key`` ascending, then
    normalized CIK, then normalized title. The order is reproducible from the
    registrant list alone and does not depend on the payload's own key or
    entry order. The title is informational only — the legal issuer name
    recorded in the manifest always comes from the issuer's own submissions
    metadata, never from this file.
    """
    if not isinstance(tickers_payload, dict):
        raise V3HoldoutSelectionError(
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
    candidates = [
        {"cik": cik, "title": title, "rank_key": rank_key(cik)}
        for cik, title in by_cik.items()
    ]
    candidates.sort(
        key=lambda entry: (entry["rank_key"], entry["cik"], entry["title"])
    )
    return candidates


def stratum_for_sic(sic: int) -> dict[str, Any] | None:
    for stratum in SIC_STRATA:
        low, high = stratum["sic_range"]
        if low <= sic <= high:
            return stratum
    return None


# --- Per-candidate metadata evaluation ------------------------------------------
# The row and fiscal-year readers are the first holdout's, reused by reference:
# _ten_k_rows admits exact form 10-K only (amendments and foreign annual forms
# fail the equality), fiscal_year_accessions reads the filer's own fy/fp
# designations, _complete_row requires canonical accession/date/document
# shapes. Only the target years and the exclusion sets differ here.


def evaluate_candidate_submissions(
    *,
    cik: str,
    submissions: Mapping[str, Any],
    fetcher: Any,
    excluded_accessions: set[str],
    selected_accessions: set[str],
    selected_documents: set[tuple[str, str]],
) -> dict[str, Any]:
    """Eligibility over an already-fetched submissions payload."""
    filings = submissions.get("filings") or {}
    rows = rfh._ten_k_rows(filings.get("recent") or {})
    row_accessions = {
        row["accession_number"] for row in rows if row["accession_number"]
    }

    target_years = (TARGET_PREVIOUS_FISCAL_YEAR, TARGET_CURRENT_FISCAL_YEAR)

    pages = [
        (page or {}).get("name")
        for page in (filings.get("files") or [])
    ]
    pages = [name for name in pages if isinstance(name, str) and name]

    try:
        facts_payload = fetcher.get(companyfacts_url(cik))
    except rfb.BenchmarkError:
        raise
    except Exception:  # noqa: BLE001
        return {"eligible": False, "reason": REASON_FISCAL_METADATA_UNREADABLE}
    fy_map = rfh.fiscal_year_accessions(facts_payload)

    for year in target_years:
        if not fy_map.get(year):
            return {"eligible": False, "reason": REASON_TARGET_FISCAL_YEAR_MISSING}
    for year in target_years:
        if len(fy_map[year]) != 1:
            return {"eligible": False, "reason": REASON_FISCAL_METADATA_AMBIGUOUS}

    previous_accn = next(iter(fy_map[TARGET_PREVIOUS_FISCAL_YEAR]))
    current_accn = next(iter(fy_map[TARGET_CURRENT_FISCAL_YEAR]))

    # Paged history only if a designated accession is not already in `recent`.
    if not {previous_accn, current_accn} <= row_accessions:
        for name in pages:
            try:
                page_payload = fetcher.get(submissions_page_url(name))
            except rfb.BenchmarkError:
                raise
            except Exception:  # noqa: BLE001
                return {
                    "eligible": False,
                    "reason": REASON_PAGED_HISTORY_UNREADABLE,
                }
            rows.extend(rfh._ten_k_rows(page_payload))
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
        return {"eligible": False, "reason": REASON_TARGET_ROW_NOT_FOUND}

    if not (rfh._complete_row(previous_row) and rfh._complete_row(current_row)):
        return {"eligible": False, "reason": REASON_ROW_METADATA_INCOMPLETE}

    if {previous_accn, current_accn} & excluded_accessions:
        return {"eligible": False, "reason": REASON_PRIOR_CORPUS_ACCESSION}

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


def select_v3_holdout(
    *,
    fetch_json: Callable[[str], Any],
    development_manifest: Mapping[str, Any] | None = None,
    holdout_manifest: Mapping[str, Any] | None = None,
    development_manifest_sha256: str | None = None,
    holdout_manifest_sha256: str | None = None,
    now: Callable[[], str] = rfb.utc_now_iso,
) -> dict[str, Any]:
    """Execute the predeclared protocol over official metadata.

    Returns ``{"selected": bool, "manifest": ... | None, "report": ...}``.
    ``fetch_json`` is any ``url -> parsed JSON`` callable; every call is
    routed through the closed metadata allowlist, so handing this function a
    body-fetching transport cannot make it fetch a body. There is no seed
    parameter: the ranking seed is the frozen module constant.
    """
    fetcher = MetadataOnlyFetcher(fetch_json)
    exclusions = prior_corpus_exclusions(
        development_manifest=development_manifest,
        holdout_manifest=holdout_manifest,
        development_manifest_sha256=development_manifest_sha256,
        holdout_manifest_sha256=holdout_manifest_sha256,
    )
    excluded_ciks = merged_excluded_ciks(exclusions)
    excluded_accessions = merged_excluded_accessions(exclusions)

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

    # Phase A: stratified scan in hash-rank order.
    for candidate in universe:
        if all(quota == 0 for quota in quotas.values()):
            break
        if probes >= MAX_SUBMISSIONS_PROBES:
            probe_budget_exhausted = True
            break
        cik = candidate["cik"]
        if cik in excluded_ciks:
            # Skipped before any network probe is spent.
            note(REASON_PRIOR_CORPUS_CIK)
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
            # place in rank order for the predeclared fallback.
            deferred.append(
                {"cik": cik, "stratum": {**stratum, "_sic": sic}, "submissions": submissions}
            )
            note(REASON_STRATUM_FULL_DEFERRED)
            continue
        outcome = evaluate_candidate_submissions(
            cik=cik,
            submissions=submissions,
            fetcher=fetcher,
            excluded_accessions=excluded_accessions,
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

    # Phase B: predeclared fallback over deferred candidates, rank order.
    if len(selected) < TARGET_PAIR_COUNT and not probe_budget_exhausted:
        for item in deferred:
            if len(selected) >= TARGET_PAIR_COUNT:
                break
            outcome = evaluate_candidate_submissions(
                cik=item["cik"],
                submissions=item["submissions"],
                fetcher=fetcher,
                excluded_accessions=excluded_accessions,
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
        manifest=manifest,
    )
    return {"selected": succeeded, "manifest": manifest, "report": report}


# --- Manifest construction and validation ---------------------------------------

_V3_MANIFEST_REQUIRED = (
    "schema_version",
    "benchmark_id",
    "benchmark_version",
    "status",
    "form",
    "target_pair_count",
    "target_previous_fiscal_year",
    "target_current_fiscal_year",
    "corpus_role",
    "extraction_parser_developed_using_this_corpus",
    "evaluation_contract_developed_using_this_corpus",
    "extraction_holdout_evaluation",
    "generalization_claim_supported",
    "corpus_role_detail",
    "frozen_extraction_parser_version",
    "frozen_parser_source_path",
    "frozen_parser_source_sha256",
    "frozen_unit_grammar_version",
    "frozen_detector_version",
    "frozen_detector_source_path",
    "frozen_detector_source_sha256",
    "frozen_workflow_version",
    "frozen_workflow_source_path",
    "frozen_workflow_source_sha256",
    "frozen_evaluation_contract_version",
    "frozen_metric_definitions_version",
    "frozen_report_contract_version",
    "frozen_evaluator_source_path",
    "frozen_evaluator_source_sha256",
    "frozen_subject_matching",
    "frozen_unit_identity_contract",
    "frozen_annotation_schema_version",
    "frozen_annotation_protocol_version",
    "selection_protocol_version",
    "selection_protocol_hash",
    "selection_seed_identifier",
    "prior_corpus_exclusions",
    "metadata_snapshot",
    "selected_at",
    "pairs",
)
_V3_MANIFEST_OPTIONAL = ("description",)

_V3_EXCLUSION_SOURCE_REQUIRED = (
    "benchmark_id",
    "manifest_path",
    "manifest_sha256",
    "excluded_ciks",
    "excluded_accessions",
)

_V3_PAIR_REQUIRED = (
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

_V3_SIDE_REQUIRED = (
    "accession_number",
    "form",
    "filing_date",
    "reporting_period",
    "primary_document",
    "expected_sha256",
    "source_verified",
)

#: Frozen version-identity fields and the exact value each must carry.
_FROZEN_VERSION_FIELDS = {
    "frozen_extraction_parser_version": FROZEN_EXTRACTION_PARSER_VERSION,
    "frozen_parser_source_path": FROZEN_PARSER_SOURCE_PATH,
    "frozen_unit_grammar_version": FROZEN_UNIT_GRAMMAR_VERSION,
    "frozen_detector_version": FROZEN_DETECTOR_VERSION,
    "frozen_detector_source_path": FROZEN_DETECTOR_SOURCE_PATH,
    "frozen_workflow_version": FROZEN_WORKFLOW_VERSION,
    "frozen_workflow_source_path": FROZEN_WORKFLOW_SOURCE_PATH,
    "frozen_evaluation_contract_version": FROZEN_EVALUATION_CONTRACT_VERSION,
    "frozen_metric_definitions_version": FROZEN_METRIC_DEFINITIONS_VERSION,
    "frozen_report_contract_version": FROZEN_REPORT_CONTRACT_VERSION,
    "frozen_evaluator_source_path": FROZEN_EVALUATOR_SOURCE_PATH,
    "frozen_subject_matching": FROZEN_SUBJECT_MATCHING,
    "frozen_unit_identity_contract": FROZEN_UNIT_IDENTITY_CONTRACT,
    "frozen_annotation_schema_version": FROZEN_ANNOTATION_SCHEMA_VERSION,
    "frozen_annotation_protocol_version": FROZEN_ANNOTATION_PROTOCOL_VERSION,
}

_FROZEN_HASH_FIELDS = tuple(field for field, _path_field, _path in FROZEN_SOURCE_FILES)


def frozen_source_sha256(path: str, repo_root: str | Path | None = None) -> str:
    root = Path(repo_root or Path(__file__).resolve().parent)
    return rfb.sha256_file(root / path)


def corpus_role_fields() -> dict[str, Any]:
    """Corpus-role block for a frozen-but-unevaluated v3 holdout.

    Every negative stays false until it is separately earned: the parser and
    the evaluation contract were frozen BEFORE this corpus was selected, no
    body has been read, nothing has been extracted or scored, and eligibility
    to support an out-of-sample claim is not the claim.
    """
    return {
        "corpus_role": rfb.CORPUS_ROLE_EXTRACTION_HOLDOUT,
        "extraction_parser_developed_using_this_corpus": False,
        "evaluation_contract_developed_using_this_corpus": False,
        "extraction_holdout_evaluation": False,
        "generalization_claim_supported": False,
        "corpus_role_detail": (
            "Issuers and exact filing pairs were frozen from official SEC "
            "metadata only, after item1a_units.v3 / item1a_detector.v3 / "
            "comparison_workflow.v3 and the contract-v2 gold evaluator "
            "(real-filing-benchmark.evaluation.v2 + metrics.v2 + report.v2) "
            "were merged and frozen, and before any selected filing body was "
            "downloaded or inspected. Neither the parser, the unit grammar, "
            "nor the evaluation contract was developed using this corpus. No "
            "filing body has been acquired, no extraction or comparison has "
            "run, no annotation exists, no evaluation exists, and no "
            "generalization claim is supported. Modifying any pinned frozen "
            "file in response to future results from this corpus would "
            "convert it into development data; the recorded hashes make that "
            "detectable."
        ),
    }


def _pair_id_for(stratum_id: str, ordinal: int) -> str:
    return f"{stratum_id}-{ordinal:02d}"


def _manifest_side(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "accession_number": row["accession_number"],
        "form": row["form"],
        "filing_date": row["filing_date"],
        "reporting_period": row["reporting_period"],
        "primary_document": row["primary_document"],
        # No body bytes exist, so no digest exists. null is the only honest
        # value while the manifest is metadata-only.
        "expected_sha256": None,
        "source_verified": False,
    }


def build_manifest(
    selected: list[Mapping[str, Any]],
    *,
    exclusions: Mapping[str, Any],
    universe_retrieved_at: str,
    selected_at: str,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Assemble the frozen v3 holdout manifest from selection payloads."""
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
        "schema_version": V3_HOLDOUT_MANIFEST_SCHEMA_VERSION,
        "benchmark_id": V3_HOLDOUT_BENCHMARK_ID,
        "benchmark_version": V3_HOLDOUT_BENCHMARK_VERSION,
        "status": STATUS_HOLDOUT_FROZEN_METADATA_ONLY,
        "form": rfb.MANIFEST_FORM,
        "target_pair_count": TARGET_PAIR_COUNT,
        "target_previous_fiscal_year": TARGET_PREVIOUS_FISCAL_YEAR,
        "target_current_fiscal_year": TARGET_CURRENT_FISCAL_YEAR,
        **corpus_role_fields(),
        "frozen_extraction_parser_version": FROZEN_EXTRACTION_PARSER_VERSION,
        "frozen_parser_source_path": FROZEN_PARSER_SOURCE_PATH,
        "frozen_parser_source_sha256": frozen_source_sha256(
            FROZEN_PARSER_SOURCE_PATH, repo_root
        ),
        "frozen_unit_grammar_version": FROZEN_UNIT_GRAMMAR_VERSION,
        "frozen_detector_version": FROZEN_DETECTOR_VERSION,
        "frozen_detector_source_path": FROZEN_DETECTOR_SOURCE_PATH,
        "frozen_detector_source_sha256": frozen_source_sha256(
            FROZEN_DETECTOR_SOURCE_PATH, repo_root
        ),
        "frozen_workflow_version": FROZEN_WORKFLOW_VERSION,
        "frozen_workflow_source_path": FROZEN_WORKFLOW_SOURCE_PATH,
        "frozen_workflow_source_sha256": frozen_source_sha256(
            FROZEN_WORKFLOW_SOURCE_PATH, repo_root
        ),
        "frozen_evaluation_contract_version": FROZEN_EVALUATION_CONTRACT_VERSION,
        "frozen_metric_definitions_version": FROZEN_METRIC_DEFINITIONS_VERSION,
        "frozen_report_contract_version": FROZEN_REPORT_CONTRACT_VERSION,
        "frozen_evaluator_source_path": FROZEN_EVALUATOR_SOURCE_PATH,
        "frozen_evaluator_source_sha256": frozen_source_sha256(
            FROZEN_EVALUATOR_SOURCE_PATH, repo_root
        ),
        "frozen_subject_matching": FROZEN_SUBJECT_MATCHING,
        "frozen_unit_identity_contract": FROZEN_UNIT_IDENTITY_CONTRACT,
        "frozen_annotation_schema_version": FROZEN_ANNOTATION_SCHEMA_VERSION,
        "frozen_annotation_protocol_version": FROZEN_ANNOTATION_PROTOCOL_VERSION,
        "selection_protocol_version": V3_HOLDOUT_SELECTION_PROTOCOL_VERSION,
        "selection_protocol_hash": selection_protocol_hash(),
        "selection_seed_identifier": SELECTION_SEED_IDENTIFIER,
        "prior_corpus_exclusions": {
            "sources": [dict(source) for source in exclusions["sources"]],
        },
        "metadata_snapshot": {
            "company_tickers_retrieved_at": universe_retrieved_at,
            "selected_at": selected_at,
        },
        "selected_at": selected_at,
        "description": (
            "Metadata-only v3 extraction holdout: exact issuers and filing "
            "pairs frozen from official SEC metadata AFTER the v3 unit "
            "representation and the contract-v2 gold evaluator were frozen "
            "and BEFORE any selected filing body was downloaded or "
            "structurally inspected. Both prior real-filing corpora are "
            "excluded by CIK and accession. No filing body has been "
            "acquired, no checksum exists, no extraction has run, and no "
            "accuracy or generalization result exists. A selected pair is "
            "never replaced because of later-observed filing-body structure, "
            "extraction outcome, detector output, workflow output, or "
            "evaluation result."
        ),
        "pairs": pairs,
    }
    validate_v3_holdout_manifest(manifest)
    return manifest


def default_v3_holdout_dir() -> Path:
    return (
        Path(__file__).resolve().parent
        / "benchmarks"
        / V3_HOLDOUT_BENCHMARK_ID
    )


def default_v3_holdout_manifest_path() -> Path:
    return default_v3_holdout_dir() / "manifest.json"


def default_v3_selection_report_path() -> Path:
    return default_v3_holdout_dir() / "selection_report.json"


def default_v3_evaluation_config_path() -> Path:
    return default_v3_holdout_dir() / "evaluation_config.json"


def load_v3_holdout_manifest(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path or default_v3_holdout_manifest_path())
    if not target.exists():
        raise V3HoldoutManifestError(
            "v3_holdout_manifest_not_found",
            f"v3 holdout manifest not found: {target.name}",
        )
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise V3HoldoutManifestError(
            "v3_holdout_manifest_not_json", f"{target.name}: {exc}"
        ) from exc
    validate_v3_holdout_manifest(document)
    return document


def v3_holdout_manifest_hash(path: str | Path | None = None) -> str:
    return rfb.sha256_file(Path(path or default_v3_holdout_manifest_path()))


#: Volatile timestamp fields excluded from the reproducible manifest hash.
#: The committed-file sha256 (over all bytes, timestamps included) remains the
#: cross-artifact chaining hash, exactly like the first holdout; this second
#: hash exists so identical metadata provably yields an identical selection.
_VOLATILE_MANIFEST_FIELDS = ("metadata_snapshot", "selected_at")


def reproducible_manifest_hash(manifest: Mapping[str, Any]) -> str:
    reproducible = {
        key: value
        for key, value in manifest.items()
        if key not in _VOLATILE_MANIFEST_FIELDS
    }
    return rfb.payload_hash(reproducible)


def validate_v3_holdout_manifest(document: Any) -> None:
    """Reject every documented v3-holdout-manifest defect."""
    rfb._exact_keys(
        document,
        required=_V3_MANIFEST_REQUIRED,
        optional=_V3_MANIFEST_OPTIONAL,
        where="v3 holdout manifest",
        error=V3HoldoutManifestError,
        code_prefix="v3_holdout_manifest",
    )
    _require = rfb._require
    _require(
        document["schema_version"] == V3_HOLDOUT_MANIFEST_SCHEMA_VERSION,
        V3HoldoutManifestError,
        "v3_holdout_manifest_schema_version_mismatch",
        f"v3 holdout manifest: schema_version must be "
        f"{V3_HOLDOUT_MANIFEST_SCHEMA_VERSION!r}",
    )
    _require(
        document["benchmark_id"] == V3_HOLDOUT_BENCHMARK_ID,
        V3HoldoutManifestError,
        "v3_holdout_manifest_invalid_benchmark_id",
        f"v3 holdout manifest: benchmark_id must be {V3_HOLDOUT_BENCHMARK_ID!r}",
    )
    _require(
        document["benchmark_id"]
        not in (rfb.BENCHMARK_ID, rfh.HOLDOUT_BENCHMARK_ID),
        V3HoldoutManifestError,
        "v3_holdout_manifest_reuses_prior_id",
        "v3 holdout manifest: the v3 holdout may never reuse a prior corpus's "
        "benchmark id",
    )
    _require(
        document["benchmark_version"] == V3_HOLDOUT_BENCHMARK_VERSION,
        V3HoldoutManifestError,
        "v3_holdout_manifest_invalid_benchmark_version",
        f"v3 holdout manifest: benchmark_version must be "
        f"{V3_HOLDOUT_BENCHMARK_VERSION}",
    )
    _require(
        document["status"] in V3_HOLDOUT_STATUS_ORDER,
        V3HoldoutManifestError,
        "v3_holdout_manifest_invalid_status",
        f"v3 holdout manifest: status must be one of "
        f"{list(V3_HOLDOUT_STATUS_ORDER)}",
    )
    _require(
        document["form"] == rfb.MANIFEST_FORM,
        V3HoldoutManifestError,
        "v3_holdout_manifest_invalid_form",
        f"v3 holdout manifest: v1 covers form {rfb.MANIFEST_FORM!r} only",
    )
    _require(
        document["target_pair_count"] == TARGET_PAIR_COUNT,
        V3HoldoutManifestError,
        "v3_holdout_manifest_invalid_target_pair_count",
        f"v3 holdout manifest: target_pair_count must be {TARGET_PAIR_COUNT}",
    )
    _require(
        document["target_previous_fiscal_year"] == TARGET_PREVIOUS_FISCAL_YEAR
        and document["target_current_fiscal_year"] == TARGET_CURRENT_FISCAL_YEAR,
        V3HoldoutManifestError,
        "v3_holdout_manifest_fiscal_years_mismatch",
        f"v3 holdout manifest: target fiscal years must be the protocol's "
        f"{TARGET_PREVIOUS_FISCAL_YEAR} -> {TARGET_CURRENT_FISCAL_YEAR}",
    )

    role_fields = corpus_role_fields()
    for field, expected in role_fields.items():
        if field == "corpus_role_detail":
            rfb._require_bounded_str(
                document[field],
                f"v3 holdout manifest.{field}",
                max_chars=2000,
                error=V3HoldoutManifestError,
                code_prefix="v3_holdout_manifest",
            )
            continue
        _require(
            document[field] == expected,
            V3HoldoutManifestError,
            "v3_holdout_manifest_corpus_role_mismatch",
            f"v3 holdout manifest.{field}: must be {expected!r} — a v3 "
            "holdout manifest cannot claim parser or evaluation-contract "
            "development, a completed holdout evaluation, or a supported "
            "generalization claim",
        )

    for field, expected in _FROZEN_VERSION_FIELDS.items():
        _require(
            document[field] == expected,
            V3HoldoutManifestError,
            "v3_holdout_manifest_frozen_identity_mismatch",
            f"v3 holdout manifest.{field}: must be exactly {expected!r} — "
            "the frozen v3/v2 contract identities may not vary, mix versions, "
            "or name an unknown version",
        )
    for field in _FROZEN_HASH_FIELDS:
        _require(
            isinstance(document[field], str)
            and bool(rfb._SHA256_RE.match(document[field])),
            V3HoldoutManifestError,
            "v3_holdout_manifest_invalid_frozen_hash",
            f"v3 holdout manifest.{field}: must be 64 lowercase hex characters",
        )

    _require(
        document["selection_protocol_version"]
        == V3_HOLDOUT_SELECTION_PROTOCOL_VERSION,
        V3HoldoutManifestError,
        "v3_holdout_manifest_invalid_selection_protocol",
        f"v3 holdout manifest: selection_protocol_version must be "
        f"{V3_HOLDOUT_SELECTION_PROTOCOL_VERSION!r}",
    )
    _require(
        document["selection_protocol_hash"] == selection_protocol_hash(),
        V3HoldoutManifestError,
        "v3_holdout_manifest_protocol_hash_mismatch",
        "v3 holdout manifest: selection_protocol_hash does not match the "
        "protocol in this repository — the protocol may not change after "
        "the selection it produced",
    )
    _require(
        document["selection_seed_identifier"] == SELECTION_SEED_IDENTIFIER,
        V3HoldoutManifestError,
        "v3_holdout_manifest_seed_mismatch",
        f"v3 holdout manifest: selection_seed_identifier must be "
        f"{SELECTION_SEED_IDENTIFIER!r}; the ranking seed is fixed and never "
        "user-supplied",
    )

    exclusions = document["prior_corpus_exclusions"]
    rfb._exact_keys(
        exclusions,
        required=("sources",),
        where="v3 holdout manifest.prior_corpus_exclusions",
        error=V3HoldoutManifestError,
        code_prefix="v3_holdout_exclusions",
    )
    sources = exclusions["sources"]
    _require(
        isinstance(sources, list) and len(sources) == 2,
        V3HoldoutManifestError,
        "v3_holdout_exclusions_source_count",
        "v3 holdout manifest: prior_corpus_exclusions.sources must list "
        "exactly the two prior corpora",
    )
    seen_source_ids: set[str] = set()
    excluded_ciks: set[str] = set()
    excluded_accessions: set[str] = set()
    for index, source in enumerate(sources):
        where = f"v3 holdout manifest.prior_corpus_exclusions.sources[{index}]"
        rfb._exact_keys(
            source,
            required=_V3_EXCLUSION_SOURCE_REQUIRED,
            where=where,
            error=V3HoldoutManifestError,
            code_prefix="v3_holdout_exclusions",
        )
        source_id = source["benchmark_id"]
        _require(
            isinstance(source_id, str) and bool(rfb._ID_RE.match(source_id)),
            V3HoldoutManifestError,
            "v3_holdout_exclusions_invalid_source_id",
            f"{where}: benchmark_id must be a lowercase slug",
        )
        _require(
            source_id not in seen_source_ids,
            V3HoldoutManifestError,
            "v3_holdout_exclusions_duplicate_source",
            f"{where}: duplicate exclusion source {source_id!r}",
        )
        seen_source_ids.add(source_id)
        _require(
            source_id != V3_HOLDOUT_BENCHMARK_ID,
            V3HoldoutManifestError,
            "v3_holdout_exclusions_self_reference",
            f"{where}: the v3 holdout cannot be its own exclusion source",
        )
        path_value = source["manifest_path"]
        _require(
            isinstance(path_value, str)
            and 0 < len(path_value) <= 200
            and not path_value.startswith(("/", "\\"))
            and ".." not in path_value,
            V3HoldoutManifestError,
            "v3_holdout_exclusions_invalid_path",
            f"{where}: manifest_path must be a bounded repository-relative "
            "path",
        )
        _require(
            isinstance(source["manifest_sha256"], str)
            and bool(rfb._SHA256_RE.match(source["manifest_sha256"])),
            V3HoldoutManifestError,
            "v3_holdout_exclusions_invalid_hash",
            f"{where}: manifest_sha256 must be 64 lowercase hex characters",
        )
        for list_field in ("excluded_ciks", "excluded_accessions"):
            values = source[list_field]
            _require(
                isinstance(values, list) and values,
                V3HoldoutManifestError,
                "v3_holdout_exclusions_empty",
                f"{where}.{list_field}: must be a non-empty list",
            )
        excluded_ciks.update(source["excluded_ciks"])
        excluded_accessions.update(source["excluded_accessions"])

    snapshot = document["metadata_snapshot"]
    rfb._exact_keys(
        snapshot,
        required=("company_tickers_retrieved_at", "selected_at"),
        where="v3 holdout manifest.metadata_snapshot",
        error=V3HoldoutManifestError,
        code_prefix="v3_holdout_snapshot",
    )
    for field in ("company_tickers_retrieved_at", "selected_at"):
        rfb._require_iso_timestamp(
            snapshot[field],
            f"v3 holdout manifest.metadata_snapshot.{field}",
            V3HoldoutManifestError,
            "v3_holdout_manifest",
        )
    rfb._require_iso_timestamp(
        document["selected_at"],
        "v3 holdout manifest.selected_at",
        V3HoldoutManifestError,
        "v3_holdout_manifest",
    )
    if "description" in document:
        rfb._require_bounded_str(
            document["description"],
            "v3 holdout manifest.description",
            max_chars=2000,
            error=V3HoldoutManifestError,
            code_prefix="v3_holdout_manifest",
        )

    _validate_v3_pairs(document, excluded_ciks, excluded_accessions)


def _validate_v3_pairs(
    document: Mapping[str, Any],
    excluded_ciks: set[str],
    excluded_accessions: set[str],
) -> None:
    _require = rfb._require
    pairs = document["pairs"]
    _require(
        isinstance(pairs, list) and len(pairs) == TARGET_PAIR_COUNT,
        V3HoldoutManifestError,
        "v3_holdout_manifest_pair_count_mismatch",
        f"v3 holdout manifest: exactly {TARGET_PAIR_COUNT} frozen pairs are "
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
        where = f"v3 holdout manifest.pairs[{index}]"
        rfb._exact_keys(
            pair,
            required=_V3_PAIR_REQUIRED,
            where=where,
            error=V3HoldoutManifestError,
            code_prefix="v3_holdout_pair",
        )
        pair_id = pair["pair_id"]
        _require(
            isinstance(pair_id, str) and bool(rfb._ID_RE.match(pair_id)),
            V3HoldoutManifestError,
            "v3_holdout_pair_invalid_id",
            f"{where}: pair_id must be a lowercase slug",
        )
        _require(
            pair_id not in seen_pair_ids,
            V3HoldoutManifestError,
            "v3_holdout_pair_duplicate_id",
            f"{where}: duplicate pair_id {pair_id!r}",
        )
        seen_pair_ids.add(pair_id)
        _require(
            pair_id > previous_pair_id,
            V3HoldoutManifestError,
            "v3_holdout_pairs_unordered",
            f"{where}: pairs must be sorted by pair_id",
        )
        previous_pair_id = pair_id

        cik = pair["cik"]
        _require(
            isinstance(cik, str) and bool(rfb._CIK_RE.match(cik)),
            V3HoldoutManifestError,
            "v3_holdout_pair_invalid_cik",
            f"{where}: cik must be a 10-digit zero-padded string",
        )
        _require(
            cik not in seen_ciks,
            V3HoldoutManifestError,
            "v3_holdout_pair_duplicate_cik",
            f"{where}: duplicate cik {cik!r}",
        )
        seen_ciks.add(cik)
        _require(
            cik not in excluded_ciks,
            V3HoldoutManifestError,
            "v3_holdout_pair_prior_corpus_cik",
            f"{where}: cik {cik!r} belongs to a prior real-filing corpus and "
            "can never enter the v3 holdout",
        )

        name = rfb._require_bounded_str(
            pair["issuer_name"],
            f"{where}.issuer_name",
            max_chars=200,
            error=V3HoldoutManifestError,
            code_prefix="v3_holdout_pair",
        )
        _require(
            name not in seen_names,
            V3HoldoutManifestError,
            "v3_holdout_pair_duplicate_issuer",
            f"{where}: issuer_name {name!r} appears more than once",
        )
        seen_names.add(name)

        sic = pair["sic"]
        _require(
            isinstance(sic, int) and not isinstance(sic, bool),
            V3HoldoutManifestError,
            "v3_holdout_pair_invalid_sic",
            f"{where}: sic must be an integer SIC code",
        )
        stratum = strata_by_id.get(pair["stratum_id"])
        _require(
            stratum is not None,
            V3HoldoutManifestError,
            "v3_holdout_pair_unknown_stratum",
            f"{where}: stratum_id {pair['stratum_id']!r} is not a declared "
            "stratum",
        )
        low, high = stratum["sic_range"]
        _require(
            low <= sic <= high,
            V3HoldoutManifestError,
            "v3_holdout_pair_sic_outside_stratum",
            f"{where}: sic {sic} is outside the declared range of "
            f"{pair['stratum_id']!r} [{low}, {high}]",
        )
        _require(
            pair["stratum_label"] == stratum["label"],
            V3HoldoutManifestError,
            "v3_holdout_pair_stratum_label_mismatch",
            f"{where}: stratum_label must match the declared stratum",
        )
        distinct_strata.add(pair["stratum_id"])

        _require(
            pair["target_previous_fiscal_year"] == TARGET_PREVIOUS_FISCAL_YEAR
            and pair["target_current_fiscal_year"] == TARGET_CURRENT_FISCAL_YEAR,
            V3HoldoutManifestError,
            "v3_holdout_pair_fiscal_years_mismatch",
            f"{where}: target fiscal years must be the protocol's "
            f"{TARGET_PREVIOUS_FISCAL_YEAR} -> {TARGET_CURRENT_FISCAL_YEAR}",
        )

        references = pair["metadata_source_references"]
        _require(
            isinstance(references, list) and references,
            V3HoldoutManifestError,
            "v3_holdout_pair_missing_metadata_references",
            f"{where}: metadata_source_references must be a non-empty list",
        )
        for reference in references:
            # A body URL in the provenance trail would mean a body was read.
            try:
                require_metadata_url(reference)
            except NonMetadataEndpoint:
                raise V3HoldoutManifestError(
                    "v3_holdout_pair_non_metadata_reference",
                    f"{where}: {reference!r} is not a metadata endpoint",
                ) from None

        previous = _validate_v3_side(pair, "previous", where, document["status"])
        current = _validate_v3_side(pair, "current", where, document["status"])
        _require(
            previous["filing_date"] < current["filing_date"],
            V3HoldoutManifestError,
            "v3_holdout_pair_filing_dates_unordered",
            f"{where}: previous.filing_date must be strictly before "
            "current.filing_date",
        )
        _require(
            previous["reporting_period"] < current["reporting_period"],
            V3HoldoutManifestError,
            "v3_holdout_pair_periods_unordered",
            f"{where}: previous.reporting_period must be strictly before "
            "current.reporting_period",
        )
        for side_name, parsed in (("previous", previous), ("current", current)):
            accession = parsed["accession_number"]
            _require(
                accession not in excluded_accessions,
                V3HoldoutManifestError,
                "v3_holdout_pair_prior_corpus_accession",
                f"{where}.{side_name}: accession {accession!r} belongs to a "
                "prior real-filing corpus and can never enter the v3 holdout",
            )
            _require(
                accession not in seen_accessions,
                V3HoldoutManifestError,
                "v3_holdout_pair_duplicate_accession",
                f"{where}.{side_name}: accession {accession!r} already "
                "appears in this manifest",
            )
            seen_accessions.add(accession)
            document_key = (cik, parsed["primary_document"])
            _require(
                document_key not in seen_documents,
                V3HoldoutManifestError,
                "v3_holdout_pair_duplicate_primary_document",
                f"{where}.{side_name}: primary document "
                f"{parsed['primary_document']!r} already appears for this cik",
            )
            seen_documents.add(document_key)

    _require(
        len(distinct_strata) >= min(MINIMUM_DISTINCT_STRATA, TARGET_PAIR_COUNT),
        V3HoldoutManifestError,
        "v3_holdout_manifest_insufficient_strata",
        f"v3 holdout manifest: pairs must span at least "
        f"{MINIMUM_DISTINCT_STRATA} distinct SIC strata, got "
        f"{len(distinct_strata)}",
    )


def _validate_v3_side(
    pair: Mapping[str, Any], side: str, where: str, status: str
) -> dict[str, Any]:
    _require = rfb._require
    payload = pair[side]
    side_where = f"{where}.{side}"
    rfb._exact_keys(
        payload,
        required=_V3_SIDE_REQUIRED,
        where=side_where,
        error=V3HoldoutManifestError,
        code_prefix="v3_holdout_side",
    )
    _require(
        payload["form"] == rfb.MANIFEST_FORM,
        V3HoldoutManifestError,
        "v3_holdout_side_invalid_form",
        f"{side_where}: form must be exactly {rfb.MANIFEST_FORM!r} — "
        "amendments such as '10-K/A' and foreign annual forms such as '20-F' "
        "or '40-F' are excluded and are never substitutions",
    )
    accession = payload["accession_number"]
    _require(
        isinstance(accession, str) and bool(rfb._ACCESSION_RE.match(accession)),
        V3HoldoutManifestError,
        "v3_holdout_side_invalid_accession",
        f"{side_where}: accession_number must look like NNNNNNNNNN-NN-NNNNNN",
    )
    filing_date = rfb._require_iso_date(
        payload["filing_date"],
        f"{side_where}.filing_date",
        V3HoldoutManifestError,
        "v3_holdout_side",
    )
    reporting_period = rfb._require_iso_date(
        payload["reporting_period"],
        f"{side_where}.reporting_period",
        V3HoldoutManifestError,
        "v3_holdout_side",
    )
    _require(
        reporting_period <= filing_date,
        V3HoldoutManifestError,
        "v3_holdout_side_period_after_filing",
        f"{side_where}: reporting_period cannot be after filing_date",
    )
    document_name = payload["primary_document"]
    _require(
        isinstance(document_name, str)
        and bool(rfb._PRIMARY_DOC_RE.match(document_name))
        and document_name.lower().endswith(rfb._PRIMARY_DOC_SUFFIXES),
        V3HoldoutManifestError,
        "v3_holdout_side_invalid_primary_document",
        f"{side_where}: primary_document must be a plain file name ending in "
        f"one of {list(rfb._PRIMARY_DOC_SUFFIXES)}",
    )
    if status == STATUS_HOLDOUT_FROZEN_METADATA_ONLY:
        # Metadata-only means exactly that: no bytes, no digest, nothing
        # verified. A digest appearing here would claim an acquisition that
        # never happened.
        _require(
            payload["expected_sha256"] is None,
            V3HoldoutManifestError,
            "v3_holdout_side_unexpected_sha256",
            f"{side_where}: expected_sha256 must be null while the manifest "
            f"is {STATUS_HOLDOUT_FROZEN_METADATA_ONLY!r}; no filing body has "
            "been acquired, so no digest can exist",
        )
        _require(
            payload["source_verified"] is False,
            V3HoldoutManifestError,
            "v3_holdout_side_claims_verification",
            f"{side_where}: source_verified must be false while the manifest "
            f"is {STATUS_HOLDOUT_FROZEN_METADATA_ONLY!r}",
        )
    else:
        _require(
            isinstance(payload["expected_sha256"], str)
            and bool(rfb._SHA256_RE.match(payload["expected_sha256"])),
            V3HoldoutManifestError,
            "v3_holdout_side_invalid_sha256",
            f"{side_where}: a manifest beyond metadata-only must carry a "
            "real digest",
        )
        _require(
            payload["expected_sha256"] != rfb.PLACEHOLDER_SHA256,
            V3HoldoutManifestError,
            "v3_holdout_side_placeholder_sha256",
            f"{side_where}: the development schema's placeholder digest is "
            "not a digest",
        )
    return {
        "accession_number": accession,
        "filing_date": filing_date,
        "reporting_period": reporting_period,
        "primary_document": document_name,
    }


#: One documented forward step at a time — the first holdout's rule verbatim.
validate_v3_holdout_status_transition = rfh.validate_holdout_status_transition


# --- Frozen-identity and exclusion-provenance verification ----------------------


def verify_frozen_code_identities(
    manifest: Mapping[str, Any], repo_root: str | Path | None = None
) -> None:
    """Refuse when any pinned source file no longer matches its frozen hash.

    This is the drift gate every later step (acquisition, blind extraction,
    evaluation) must pass first: a post-freeze edit to the parser, detector,
    workflow store, or evaluator invalidates the holdout for v3 evidence, and
    doing it in response to results from this corpus converts the corpus into
    development data.
    """
    drifted: list[str] = []
    for hash_field, _path_field, path in FROZEN_SOURCE_FILES:
        if frozen_source_sha256(path, repo_root) != manifest[hash_field]:
            drifted.append(path)
    if drifted:
        raise V3HoldoutManifestError(
            "v3_holdout_frozen_code_drift",
            "frozen source files no longer match the hashes the v3 holdout "
            f"manifest froze: {sorted(drifted)}",
        )


def verify_exclusion_provenance(
    manifest: Mapping[str, Any], repo_root: str | Path | None = None
) -> None:
    """Refuse when either committed exclusion-source manifest has changed or
    the frozen exclusion sets no longer match what those manifests derive."""
    root = Path(repo_root or Path(__file__).resolve().parent)
    recomputed = prior_corpus_exclusions(repo_root=root)
    frozen_sources = manifest["prior_corpus_exclusions"]["sources"]
    for frozen, current in zip(frozen_sources, recomputed["sources"]):
        if frozen["manifest_sha256"] != current["manifest_sha256"]:
            raise V3HoldoutManifestError(
                "v3_holdout_exclusion_source_drift",
                f"exclusion source {frozen['benchmark_id']!r}: the committed "
                "manifest no longer matches the sha256 the v3 holdout froze",
            )
        if (
            frozen["benchmark_id"] != current["benchmark_id"]
            or frozen["excluded_ciks"] != current["excluded_ciks"]
            or frozen["excluded_accessions"] != current["excluded_accessions"]
        ):
            raise V3HoldoutManifestError(
                "v3_holdout_exclusion_set_drift",
                f"exclusion source {frozen['benchmark_id']!r}: the frozen "
                "CIK/accession exclusion sets no longer match the committed "
                "source manifest",
            )


# --- Selection audit report ------------------------------------------------------


def build_selection_report(
    *,
    selected: list[Mapping[str, Any]],
    exclusions: Mapping[str, Any],
    exclusion_counts: Mapping[str, int],
    fetcher: Any,
    probes: int,
    universe_size: int,
    failures: list[Mapping[str, Any]],
    generated_at: str,
    manifest: Mapping[str, Any] | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Bounded audit report: identities, counts, and hashes — never filing
    content, never a local path, never a credential or user agent."""
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
        "report_version": V3_HOLDOUT_SELECTION_REPORT_VERSION,
        "benchmark_id": V3_HOLDOUT_BENCHMARK_ID,
        "benchmark_version": V3_HOLDOUT_BENCHMARK_VERSION,
        "generated_at": generated_at,
        "selection_succeeded": not failures and len(selected) == TARGET_PAIR_COUNT,
        "failures": [dict(item) for item in failures],
        "selection_protocol_version": V3_HOLDOUT_SELECTION_PROTOCOL_VERSION,
        "selection_protocol_hash": selection_protocol_hash(),
        "selection_protocol": selection_protocol(),
        "selection_seed_identifier": SELECTION_SEED_IDENTIFIER,
        "target_previous_fiscal_year": TARGET_PREVIOUS_FISCAL_YEAR,
        "target_current_fiscal_year": TARGET_CURRENT_FISCAL_YEAR,
        "frozen_extraction_parser_version": FROZEN_EXTRACTION_PARSER_VERSION,
        "frozen_parser_source_sha256": frozen_source_sha256(
            FROZEN_PARSER_SOURCE_PATH, repo_root
        ),
        "frozen_unit_grammar_version": FROZEN_UNIT_GRAMMAR_VERSION,
        "frozen_detector_version": FROZEN_DETECTOR_VERSION,
        "frozen_detector_source_sha256": frozen_source_sha256(
            FROZEN_DETECTOR_SOURCE_PATH, repo_root
        ),
        "frozen_workflow_version": FROZEN_WORKFLOW_VERSION,
        "frozen_workflow_source_sha256": frozen_source_sha256(
            FROZEN_WORKFLOW_SOURCE_PATH, repo_root
        ),
        "frozen_evaluation_contract_version": FROZEN_EVALUATION_CONTRACT_VERSION,
        "frozen_metric_definitions_version": FROZEN_METRIC_DEFINITIONS_VERSION,
        "frozen_report_contract_version": FROZEN_REPORT_CONTRACT_VERSION,
        "frozen_evaluator_source_sha256": frozen_source_sha256(
            FROZEN_EVALUATOR_SOURCE_PATH, repo_root
        ),
        "frozen_subject_matching": FROZEN_SUBJECT_MATCHING,
        "frozen_unit_identity_contract": FROZEN_UNIT_IDENTITY_CONTRACT,
        **corpus_role_fields(),
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
        "prior_corpus_exclusions_applied": [
            {
                "benchmark_id": source["benchmark_id"],
                "manifest_path": source["manifest_path"],
                "manifest_sha256": source["manifest_sha256"],
                "excluded_cik_count": len(source["excluded_ciks"]),
                "excluded_accession_count": len(source["excluded_accessions"]),
                "excluded_ciks": list(source["excluded_ciks"]),
                "excluded_accessions": list(source["excluded_accessions"]),
            }
            for source in exclusions["sources"]
        ],
        "metadata_endpoints_contacted": {
            "company_tickers": fetcher.request_counts.get("company_tickers", 0),
            "submissions": fetcher.request_counts.get("submissions", 0),
            "companyfacts": fetcher.request_counts.get("companyfacts", 0),
        },
        "official_hosts_contacted": ["www.sec.gov", "data.sec.gov"],
        "manifest_status": (
            manifest["status"] if manifest is not None else None
        ),
        "reproducible_manifest_hash": (
            reproducible_manifest_hash(manifest) if manifest is not None else None
        ),
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
        "gold_evaluation_runs": 0,
        "signoff_present": False,
    }
