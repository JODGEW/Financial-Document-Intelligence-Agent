"""Synthetic official-metadata fixtures for the holdout-selection suites.

Everything here is SYNTHETIC and obviously so: CIKs are low sequential
sentinels, accession numbers are formulaic, issuer names are fictional, and no
payload resembles a real registrant. The fixtures model the three official
metadata endpoints the selection is allowed to contact — the registrant list,
per-issuer submissions (with optional paged history), and companyfacts — as an
in-memory URL->payload mapping, so a test can never reach a network and an
undeclared URL is a hard failure rather than a silent fetch.
"""

from __future__ import annotations

import copy
from typing import Any

import real_filing_holdout as rfh

# One synthetic issuer per declared stratum plus spares, in ascending-CIK
# order. SICs sit inside the declared ranges.
DEFAULT_SPEC = [
    # cik_int, name,                          sic,  eligible
    (101, "Fictional Chemical Works, Inc.", 2810, True),
    (102, "Fictional Instrument Corp.", 3826, True),
    (103, "Fictional Freight Lines, Inc.", 4213, True),
    (104, "Fictional Retail Group, Inc.", 5331, True),
    (105, "Fictional Savings Financial Corp.", 6022, True),
    (106, "Fictional Beverage Brands, Inc.", 2086, True),
    (107, "Fictional Turbine Manufacturing Co.", 3511, True),
    (108, "Fictional Telecom Holdings, Inc.", 4813, True),
    (109, "Fictional Wholesale Supply Corp.", 5122, True),
    (110, "Fictional Insurance Group, Inc.", 6311, True),
    # Spares beyond every quota, used by fallback and exclusion tests.
    (111, "Fictional Specialty Chemicals, Inc.", 2890, True),
    (112, "Fictional Machine Tools Corp.", 3540, True),
]

PREVIOUS_FY = rfh.TARGET_PREVIOUS_FISCAL_YEAR
CURRENT_FY = rfh.TARGET_CURRENT_FISCAL_YEAR


def cik_str(cik_int: int) -> str:
    return f"{cik_int:010d}"


def accession(cik_int: int, year: int, sequence: int = 1) -> str:
    return f"{cik_int:010d}-{year % 100:02d}-{sequence:06d}"


def ten_k_row(cik_int: int, fiscal_year: int) -> dict[str, str]:
    """One synthetic 10-K submissions row for a calendar-year filer."""
    return {
        "form": "10-K",
        "accessionNumber": accession(cik_int, fiscal_year + 1),
        "filingDate": f"{fiscal_year + 1}-02-15",
        "reportDate": f"{fiscal_year}-12-31",
        "primaryDocument": f"fict-{cik_int:04d}-{fiscal_year}1231.htm",
    }


def submissions_payload(
    cik_int: int,
    name: str,
    sic: int,
    *,
    fiscal_years: tuple[int, ...] = (PREVIOUS_FY, CURRENT_FY),
    extra_forms: tuple[str, ...] = ("8-K", "10-Q"),
    paged: bool = False,
) -> dict[str, Any]:
    """A synthetic submissions document, optionally with paged history.

    With ``paged=True`` the 10-K rows live in a separate page file (modelling
    a high-volume filer whose 10-K rows fell out of ``recent``), and the
    recent block carries only noise forms.
    """
    rows = [ten_k_row(cik_int, year) for year in fiscal_years]
    noise = [
        {
            "form": form,
            "accessionNumber": accession(cik_int, 2025, 900 + index),
            "filingDate": "2025-05-01",
            "reportDate": "2025-03-31",
            "primaryDocument": f"noise-{index}.htm",
        }
        for index, form in enumerate(extra_forms)
    ]
    recent_rows = noise if paged else rows + noise
    payload = {
        "cik": cik_int,
        "name": name,
        "sic": str(sic),
        "sicDescription": f"Synthetic SIC {sic}",
        "filings": {
            "recent": _block(recent_rows),
            "files": (
                [{"name": f"CIK{cik_str(cik_int)}-submissions-001.json"}]
                if paged
                else []
            ),
        },
    }
    if paged:
        payload["_page_rows"] = rows + noise
    return payload


def _block(rows: list[dict[str, str]]) -> dict[str, list[str]]:
    return {
        "form": [row["form"] for row in rows],
        "accessionNumber": [row["accessionNumber"] for row in rows],
        "filingDate": [row["filingDate"] for row in rows],
        "reportDate": [row["reportDate"] for row in rows],
        "primaryDocument": [row["primaryDocument"] for row in rows],
    }


def companyfacts_payload(
    cik_int: int,
    *,
    fiscal_years: tuple[int, ...] = (PREVIOUS_FY, CURRENT_FY),
    ambiguous_year: int | None = None,
) -> dict[str, Any]:
    """Synthetic companyfacts: one Assets fact per 10-K, carrying the filer's
    fy/fp designation. ``ambiguous_year`` adds a second accession claiming the
    same fiscal year."""
    entries = [
        {
            "end": f"{year}-12-31",
            "val": 1000 + year,
            "accn": accession(cik_int, year + 1),
            "fy": year,
            "fp": "FY",
            "form": "10-K",
            "filed": f"{year + 1}-02-15",
        }
        for year in fiscal_years
    ]
    if ambiguous_year is not None:
        entries.append(
            {
                "end": f"{ambiguous_year}-12-31",
                "val": 999,
                "accn": accession(cik_int, ambiguous_year + 1, 2),
                "fy": ambiguous_year,
                "fp": "FY",
                "form": "10-K",
                "filed": f"{ambiguous_year + 1}-03-15",
            }
        )
    return {
        "cik": cik_int,
        "entityName": f"SYNTHETIC ENTITY {cik_int}",
        "facts": {"us-gaap": {"Assets": {"units": {"USD": entries}}}},
    }


class FakeMetadataSource:
    """URL -> payload mapping with request recording.

    Raises ``KeyError`` for any URL it does not declare, so a selection that
    contacts an unexpected endpoint fails the test loudly instead of being
    quietly served.
    """

    def __init__(self, payloads: dict[str, Any]):
        self.payloads = payloads
        self.requested: list[str] = []

    def __call__(self, url: str) -> Any:
        self.requested.append(url)
        return copy.deepcopy(self.payloads[url])


def metadata_source(
    spec: list[tuple[int, str, int, bool]] | None = None,
    *,
    paged_ciks: frozenset[int] = frozenset(),
    ambiguous_ciks: frozenset[int] = frozenset(),
    overrides: dict[str, Any] | None = None,
) -> FakeMetadataSource:
    """Build the complete fake official-metadata surface for a spec."""
    spec = spec if spec is not None else DEFAULT_SPEC
    tickers = {
        str(index): {
            "cik_str": cik_int,
            "ticker": f"FIC{cik_int}",
            "title": name,
        }
        for index, (cik_int, name, _sic, _eligible) in enumerate(spec)
    }
    payloads: dict[str, Any] = {rfh.COMPANY_TICKERS_URL: tickers}
    for cik_int, name, sic, eligible in spec:
        fiscal_years = (
            (PREVIOUS_FY, CURRENT_FY) if eligible else (PREVIOUS_FY - 2,)
        )
        paged = cik_int in paged_ciks
        submissions = submissions_payload(
            cik_int, name, sic, fiscal_years=fiscal_years, paged=paged
        )
        page_rows = submissions.pop("_page_rows", None)
        payloads[rfh.submissions_url(cik_str(cik_int))] = submissions
        if page_rows is not None:
            page_url = (
                f"https://data.sec.gov/submissions/"
                f"CIK{cik_str(cik_int)}-submissions-001.json"
            )
            payloads[page_url] = _block(page_rows)
        payloads[rfh.companyfacts_url(cik_str(cik_int))] = companyfacts_payload(
            cik_int,
            fiscal_years=fiscal_years,
            ambiguous_year=CURRENT_FY if cik_int in ambiguous_ciks else None,
        )
    if overrides:
        payloads.update(overrides)
    return FakeMetadataSource(payloads)


def synthetic_development_manifest() -> dict[str, Any]:
    """A tiny synthetic development manifest for exclusion tests.

    Uses sentinel CIKs that collide with DEFAULT_SPEC's first entries so the
    exclusion path is exercised without naming any real registrant.
    """
    return {
        "benchmark_id": "synthetic_dev_benchmark",
        "proposed_issuers": [
            {"cik": cik_str(101), "resolution_status": "resolved_from_official_source"},
            {"cik": cik_str(112), "resolution_status": "resolved_from_official_source"},
        ],
        "pairs": [
            {
                "cik": cik_str(101),
                "previous": {"accession_number": accession(101, PREVIOUS_FY + 1)},
                "current": {"accession_number": accession(101, CURRENT_FY + 1)},
            }
        ],
    }
