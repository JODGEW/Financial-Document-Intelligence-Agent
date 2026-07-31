"""Acquire the benchmark corpus from official SEC sources.

    # Network is OFF unless you say otherwise. This does nothing without it:
    python scripts/fetch_real_filing_benchmark.py

    export SEC_USER_AGENT="Jane Doe Research jane.doe@university.edu"
    python scripts/fetch_real_filing_benchmark.py --allow-network
    python scripts/fetch_real_filing_benchmark.py --allow-network --pair-id X
    python scripts/fetch_real_filing_benchmark.py --allow-network --resolve
    python scripts/fetch_real_filing_benchmark.py --allow-network --record-hashes

This is the only command in the repository that makes external network
requests, and it is never run by CI. It reaches a public government service on
a human's behalf, so it is explicit about it: network access requires
``--allow-network``, and a descriptive ``SEC_USER_AGENT`` identifying the
requester must be supplied externally. A placeholder is rejected rather than
sent.

Two supporting modes
--------------------
``--resolve`` fetches official submission metadata for the frozen issuer slate
and writes a LOCAL proposed-pairs block for a human to review and freeze into
the manifest. It never edits the committed manifest: an accession number that
lands in a frozen benchmark is a reviewed commit, not a tool's side effect.

``--record-hashes`` downloads bytes for a still-``proposed`` manifest and
records the observed digests locally so a human can freeze them. It ALWAYS
exits nonzero, because nothing is verified: recording a digest you just
computed proves only that you computed it.

Exit codes
----------
0  every requested filing was verified against a frozen manifest digest
1  a network, checksum, or source failure — including "downloaded but not
   verifiable", which is not success
2  invalid configuration or arguments (no network flag, bad user agent, ...)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import real_filing_acquisition as rfa  # noqa: E402
import real_filing_benchmark as rfb  # noqa: E402

RESOLUTION_FILE = "proposed_pairs.json"


def resolve_slate(
    manifest: dict[str, Any], fetcher: rfa.Fetcher, layout: rfb.CorpusLayout
) -> dict[str, Any]:
    """Fetch official submission metadata for each frozen slate entry.

    Writes a local proposal only. Every value comes from the official
    endpoint's own response — nothing is inferred from a name, a guess, or a
    remembered identifier — and anything the endpoint does not provide is
    reported as unresolved rather than filled in.
    """
    proposals: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for entry in manifest["proposed_issuers"]:
        if entry["resolution_status"] == rfb.ISSUER_PENDING and not entry["cik"]:
            unresolved.append(
                {
                    "slate_id": entry["slate_id"],
                    "issuer_name": entry["issuer_name"],
                    "code": "cik_unknown",
                    "detail": (
                        "this slate entry has no CIK. CIK lookup by company "
                        "name is not automated here on purpose: a name match "
                        "is ambiguous across subsidiaries and former "
                        "registrants, and picking one silently would fabricate "
                        "an identity. Look the CIK up at "
                        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany "
                        "and record it in the manifest slate first."
                    ),
                }
            )
            continue
        try:
            response = fetcher.get(rfb.canonical_submissions_url(entry["cik"]))
            payload = json.loads(response.body.decode("utf-8"))
        except (rfa.AcquisitionError, ValueError, UnicodeDecodeError) as exc:
            unresolved.append(
                {
                    "slate_id": entry["slate_id"],
                    "issuer_name": entry["issuer_name"],
                    "code": getattr(exc, "code", "submissions_unreadable"),
                    "detail": getattr(exc, "message", type(exc).__name__),
                }
            )
            continue
        candidates, unread = _annual_10k_candidates(payload, fetcher=fetcher)
        proposals.append(
            {
                "slate_id": entry["slate_id"],
                "issuer_name": entry["issuer_name"],
                "cik": entry["cik"],
                "official_entity_name": payload.get("name"),
                "annual_filings": candidates,
                "history_complete": not unread,
                "unread_history_files": unread,
                "note": (
                    "Candidate 10-K filings straight from the official "
                    "submissions endpoint. A human selects the two consecutive "
                    "annual filings matching this slate entry's target fiscal "
                    "years, excludes any 10-K/A, and freezes them into the "
                    "manifest in a reviewed commit."
                ),
            }
        )
        if unread:
            unresolved.append(
                {
                    "slate_id": entry["slate_id"],
                    "issuer_name": entry["issuer_name"],
                    "code": "filing_history_incomplete",
                    "detail": (
                        f"{len(unread)} paged submission file(s) could not be "
                        "read, so the candidate list is incomplete and a "
                        "target year may be missing from it. Reported rather "
                        "than presented as the issuer's full 10-K history."
                    ),
                }
            )

    report = {
        "resolution_version": "real-filing-benchmark.resolution.v1",
        "benchmark_id": manifest["benchmark_id"],
        "resolved_slate_entries": len(proposals),
        "unresolved_slate_entries": len(unresolved),
        "proposals": proposals,
        "unresolved": unresolved,
        "manifest_modified": False,
        "warning": (
            "This is a LOCAL PROPOSAL, not a frozen manifest. Nothing here is "
            "verified, and the committed manifest was not modified."
        ),
    }
    rfb.write_json_atomic(layout.root / RESOLUTION_FILE, report)
    return report


def _rows_from_block(block: dict[str, Any]) -> list[dict[str, Any]]:
    """10-K rows from one submissions block, verbatim.

    Reads only fields the endpoint provides. 10-K/A amendments are excluded per
    the frozen selection protocol — the comparison is exact, so an amendment
    can never be admitted as if it were a primary annual filing.
    """
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
                "accession_number": accessions[index] if index < len(accessions) else None,
                "filing_date": filing_dates[index] if index < len(filing_dates) else None,
                "reporting_period": report_dates[index] if index < len(report_dates) else None,
                "primary_document": documents[index] if index < len(documents) else None,
            }
        )
    return rows


def _annual_10k_candidates(
    payload: dict[str, Any], *, fetcher: rfa.Fetcher | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Every primary 10-K row across recent AND paged history.

    Returns ``(rows, unread_history_files)``.

    ``filings.recent`` holds only the most recent submissions, so a
    high-volume filer's 10-K rows fall out of it: JPMorgan Chase exposes one
    10-K row there and Verizon three, while both issuers' target filings sit
    in ``filings.files``. Reading ``recent`` alone would hand a human a
    candidate list that looks complete and silently is not — the exact failure
    this benchmark's design refuses elsewhere. A page that cannot be read is
    REPORTED, never quietly dropped.
    """
    filings = (payload.get("filings") or {})
    rows = _rows_from_block(filings.get("recent") or {})
    unread: list[str] = []
    for extra in filings.get("files") or []:
        name = (extra or {}).get("name")
        if not isinstance(name, str) or not name:
            continue
        if fetcher is None:
            unread.append(name)
            continue
        try:
            response = fetcher.get(
                f"https://{rfb.SEC_DATA_HOST}/submissions/{name}"
            )
            rows.extend(_rows_from_block(json.loads(response.body.decode("utf-8"))))
        except (rfa.AcquisitionError, ValueError, UnicodeDecodeError):
            unread.append(name)
    rows.sort(key=lambda row: (row["filing_date"] or "", row["accession_number"] or ""))
    return rows, unread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Acquire the controlled real-filing benchmark corpus from official "
            "SEC sources. Network access is disabled unless requested."
        )
    )
    parser.add_argument("--manifest", metavar="PATH", help="benchmark manifest path")
    parser.add_argument("--corpus-dir", metavar="PATH", help="local corpus directory")
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="REQUIRED to make any external request; off by default",
    )
    parser.add_argument(
        "--user-agent",
        metavar="VALUE",
        help="descriptive SEC user agent (defaults to $SEC_USER_AGENT)",
    )
    parser.add_argument(
        "--pair-id", action="append", default=[], help="fetch only these pairs"
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=rfa.DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
        metavar="SECONDS",
        help="minimum interval between requests (conservative by default)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=rfa.DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help="per-request timeout",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=rfa.DEFAULT_MAX_ATTEMPTS,
        metavar="N",
        help="bounded transport retry attempts per request",
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="fetch official submission metadata and write a local pair proposal",
    )
    parser.add_argument(
        "--record-hashes",
        action="store_true",
        help=(
            "download bytes for a still-proposed manifest and record observed "
            "digests locally; always exits nonzero because nothing is verified"
        ),
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        manifest = rfb.load_manifest(args.manifest)
    except rfb.BenchmarkError as exc:
        print(f"Invalid benchmark manifest [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2

    if not args.allow_network:
        print(
            "Network access is disabled. This command contacts an external "
            "public service (SEC EDGAR) and will not do so implicitly.\n"
            "Re-run with --allow-network once you have set a descriptive "
            "SEC_USER_AGENT that identifies you.",
            file=sys.stderr,
        )
        return 2

    try:
        user_agent = rfa.resolve_user_agent(args.user_agent)
    except rfa.UserAgentRejected as exc:
        print(f"Rejected SEC user agent [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2

    if args.max_attempts < 1:
        print("--max-attempts must be at least 1", file=sys.stderr)
        return 2

    layout = rfb.CorpusLayout(args.corpus_dir)
    fetcher = rfa.Fetcher(
        user_agent=user_agent,
        allow_network=True,
        min_interval_seconds=args.min_interval,
        timeout_seconds=args.timeout,
        max_attempts=args.max_attempts,
    )

    if args.resolve:
        report = resolve_slate(manifest, fetcher, layout)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print("Official-source slate resolution (LOCAL PROPOSAL ONLY)")
            print(f"  resolved  : {report['resolved_slate_entries']}")
            print(f"  unresolved: {report['unresolved_slate_entries']}")
            for item in report["unresolved"]:
                print(f"    {item['slate_id']}: [{item['code']}] {item['detail']}")
            print(f"\n  {report['warning']}")
            print(f"  Written to {RESOLUTION_FILE} in the local corpus directory.")
        # A proposal is never a verified acquisition.
        return 1 if report["unresolved_slate_entries"] else 0

    if not rfb.manifest_pairs(manifest):
        print(
            f"The manifest status is {manifest['status']!r} and it has no "
            "resolved pairs, so there is nothing to fetch. Run --resolve first, "
            "then freeze the reviewed pairs into the manifest.",
            file=sys.stderr,
        )
        return 2

    try:
        report = rfa.acquire_manifest(
            manifest,
            fetcher=fetcher,
            layout=layout,
            pair_ids=args.pair_id or None,
            accept_unverified_hash=args.record_hashes,
        )
    except rfa.AcquisitionError as exc:
        print(f"Acquisition failed [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2 if exc.code == "unknown_pair_id" else 1

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("Official-source acquisition (SEC EDGAR)")
        print(
            f"  requested={report['requested_filings']} "
            f"verified={report['verified_filings']} "
            f"downloaded={report['downloaded']} "
            f"cached={report['reused_verified_cache']} "
            f"failed={report['failed']}"
        )
        for item in report["filings"]:
            mark = "ok " if item["verified"] else "NOT VERIFIED"
            detail = (
                f" [{item['failure_code']}] {item.get('detail', '')}"
                if item.get("failure_code")
                else ""
            )
            print(
                f"  {mark:<12} {item['pair_id']}/{item['side']} "
                f"{item['outcome']} -> {item['source_path']}{detail}"
            )
        if args.record_hashes:
            print(
                "\n  --record-hashes: observed digests were written to the "
                "local acquisition metadata. NOTHING IS VERIFIED. A human "
                "freezes these into the manifest in a reviewed commit before "
                "the corpus can advance to 'source_verified'."
            )

    if args.record_hashes:
        return 1
    return 0 if report["all_verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
