"""Synthetic official-metadata fixtures for the v3-holdout selection suites.

Everything here is SYNTHETIC and obviously so: CIKs are low sequential
sentinels, accession numbers are formulaic, issuer names are fictional, and no
payload resembles a real registrant. The building blocks are the first
holdout's fixtures (``tests/helpers/holdout_fixtures.py``); this module only
re-targets them at the v3 protocol's fixed FY2024 -> FY2025 window and adds a
synthetic FIRST-HOLDOUT exclusion source, because the v3 selection excludes
BOTH prior corpora.
"""

from __future__ import annotations

from typing import Any

import real_filing_v3_holdout as rfv3
from tests.helpers import holdout_fixtures as hfx

# The v3 protocol's fixed filer-designated fiscal years.
PREVIOUS_FY = rfv3.TARGET_PREVIOUS_FISCAL_YEAR
CURRENT_FY = rfv3.TARGET_CURRENT_FISCAL_YEAR

cik_str = hfx.cik_str
accession = hfx.accession
_block = hfx._block

# Synthetic issuers across every declared stratum plus spares. 101 and 112 are
# development-corpus sentinels, 114 is a first-holdout sentinel; each stratum
# keeps at least two eligible, non-excluded issuers.
DEFAULT_SPEC = [
    # cik_int, name,                              sic,  eligible
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
    (111, "Fictional Specialty Chemicals, Inc.", 2890, True),
    (112, "Fictional Machine Tools Corp.", 3540, True),
    (114, "Fictional Realty Partners, Inc.", 6500, True),
    (115, "Fictional Apparel Stores, Inc.", 5651, True),
]


def rank_order(spec: list[tuple[int, str, int, bool]] | None = None) -> list[str]:
    """The deterministic hash-rank order the v3 universe puts a spec in."""
    spec = spec if spec is not None else DEFAULT_SPEC
    ciks = [cik_str(cik_int) for cik_int, _name, _sic, _eligible in spec]
    return sorted(ciks, key=lambda cik: (rfv3.rank_key(cik), cik))


def metadata_source(
    spec: list[tuple[int, str, int, bool]] | None = None,
    *,
    paged_ciks: frozenset[int] = frozenset(),
    ambiguous_ciks: frozenset[int] = frozenset(),
    overrides: dict[str, Any] | None = None,
) -> hfx.FakeMetadataSource:
    """Build the complete fake official-metadata surface for a spec, with
    every eligible issuer carrying filer-designated FY2024 and FY2025 rows."""
    spec = spec if spec is not None else DEFAULT_SPEC
    tickers = {
        str(index): {
            "cik_str": cik_int,
            "ticker": f"FIC{cik_int}",
            "title": name,
        }
        for index, (cik_int, name, _sic, _eligible) in enumerate(spec)
    }
    payloads: dict[str, Any] = {rfv3.COMPANY_TICKERS_URL: tickers}
    for cik_int, name, sic, eligible in spec:
        fiscal_years = (
            (PREVIOUS_FY, CURRENT_FY) if eligible else (PREVIOUS_FY - 2,)
        )
        paged = cik_int in paged_ciks
        submissions = hfx.submissions_payload(
            cik_int, name, sic, fiscal_years=fiscal_years, paged=paged
        )
        page_rows = submissions.pop("_page_rows", None)
        payloads[rfv3.submissions_url(cik_str(cik_int))] = submissions
        if page_rows is not None:
            page_url = (
                f"https://data.sec.gov/submissions/"
                f"CIK{cik_str(cik_int)}-submissions-001.json"
            )
            payloads[page_url] = _block(page_rows)
        payloads[rfv3.companyfacts_url(cik_str(cik_int))] = hfx.companyfacts_payload(
            cik_int,
            fiscal_years=fiscal_years,
            ambiguous_year=CURRENT_FY if cik_int in ambiguous_ciks else None,
        )
    if overrides:
        payloads.update(overrides)
    return hfx.FakeMetadataSource(payloads)


def synthetic_development_manifest() -> dict[str, Any]:
    """A tiny synthetic development-corpus exclusion source (CIKs 101, 112)."""
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


def synthetic_first_holdout_manifest() -> dict[str, Any]:
    """A tiny synthetic first-holdout exclusion source (CIK 114)."""
    return {
        "benchmark_id": "synthetic_first_holdout_benchmark",
        "pairs": [
            {
                "cik": cik_str(114),
                "previous": {"accession_number": accession(114, PREVIOUS_FY + 1)},
                "current": {"accession_number": accession(114, CURRENT_FY + 1)},
            }
        ],
    }


def run_selection(source, *, dev=None, holdout=None):
    return rfv3.select_v3_holdout(
        fetch_json=source,
        development_manifest=dev or synthetic_development_manifest(),
        holdout_manifest=holdout or synthetic_first_holdout_manifest(),
    )
