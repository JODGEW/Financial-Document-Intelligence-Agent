"""Freeze the metadata-only v3 extraction holdout from official SEC metadata.

    # Network is OFF unless you say otherwise. This does nothing without it:
    python scripts/select_real_filing_v3_holdout.py

    export SEC_USER_AGENT="Jane Doe Research jane.doe@university.edu"
    python scripts/select_real_filing_v3_holdout.py --allow-network
    python scripts/select_real_filing_v3_holdout.py --allow-network --json

METADATA ONLY. This command executes the predeclared deterministic v3-holdout
selection protocol (``real-filing-v3-holdout-selection.v1``) over official SEC
metadata endpoints and writes the frozen v3 holdout manifest plus a bounded
selection audit report. It can never download, open, or inspect a filing
body: every URL it fetches must match the closed metadata allowlist
(``real_filing_holdout.require_metadata_url``), which does not contain a
single primary-document, exhibit, or Archives URL pattern. Committing the
output files is the freeze act — a reviewed commit, not a tool side effect.

Exit codes
----------
0  the protocol filled all ten pairs; manifest and report were written
1  the protocol could not fill the selection; a FAILED report was written and
   no manifest was frozen
2  invalid configuration or arguments (no network flag, bad user agent, ...)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import real_filing_acquisition as rfa  # noqa: E402
import real_filing_benchmark as rfb  # noqa: E402
import real_filing_holdout as rfh  # noqa: E402
import real_filing_v3_holdout as rfv3  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze the v3 real-filing extraction holdout from official SEC "
            "metadata. Metadata only: filing bodies are never contacted. "
            "Network access is disabled unless requested."
        )
    )
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
        "--manifest-out",
        metavar="PATH",
        help="v3 holdout manifest output path (default: committed location)",
    )
    parser.add_argument(
        "--report-out",
        metavar="PATH",
        help="selection report output path (default: committed location)",
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
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if not args.allow_network:
        print(
            "Network access is disabled. This command contacts official SEC "
            "METADATA endpoints (registrant list, submissions, companyfacts) "
            "and will not do so implicitly.\n"
            "Re-run with --allow-network once you have set a descriptive "
            "SEC_USER_AGENT that identifies you. Filing bodies are never "
            "contacted either way.",
            file=sys.stderr,
        )
        return 2

    try:
        user_agent = rfa.resolve_user_agent(args.user_agent)
    except rfa.UserAgentRejected as exc:
        print(f"Rejected SEC user agent [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2

    # Both prior corpora are exclusion sources; either being unreadable is a
    # configuration failure, never a smaller exclusion set.
    try:
        development_manifest = rfb.load_manifest()
        holdout_manifest = rfh.load_holdout_manifest()
    except rfb.BenchmarkError as exc:
        print(
            f"Invalid prior-corpus manifest [{exc.code}]: {exc.message} — the "
            "v3 holdout cannot compute its exclusions without both committed "
            "prior manifests.",
            file=sys.stderr,
        )
        return 2

    fetcher = rfa.Fetcher(
        user_agent=user_agent,
        allow_network=True,
        min_interval_seconds=args.min_interval,
        timeout_seconds=args.timeout,
    )

    def fetch_json(url: str):
        # Defense in depth: the selection module re-checks the allowlist, but
        # this transport wrapper checks FIRST, so even a module bug could not
        # turn this command into a body fetch.
        official = rfv3.require_metadata_url(url)
        response = fetcher.get(official)
        return json.loads(response.body.decode("utf-8"))

    try:
        result = rfv3.select_v3_holdout(
            fetch_json=fetch_json,
            development_manifest=development_manifest,
            holdout_manifest=holdout_manifest,
            development_manifest_sha256=rfb.sha256_file(
                _REPO_ROOT / rfv3.DEVELOPMENT_MANIFEST_PATH
            ),
            holdout_manifest_sha256=rfb.sha256_file(
                _REPO_ROOT / rfv3.FIRST_HOLDOUT_MANIFEST_PATH
            ),
        )
    except rfv3.NonMetadataEndpoint as exc:
        print(f"Refused non-metadata URL [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
    except rfa.AcquisitionError as exc:
        print(f"Metadata fetch failed [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1

    report_path = Path(args.report_out or rfv3.default_v3_selection_report_path())
    rfb.write_json_atomic(report_path, result["report"])

    if result["selected"]:
        manifest_path = Path(
            args.manifest_out or rfv3.default_v3_holdout_manifest_path()
        )
        rfb.write_json_atomic(manifest_path, result["manifest"])
        # The report records the hash of the exact bytes just written, so the
        # committed report and committed manifest can be checked against each
        # other forever after.
        result["report"]["holdout_manifest_sha256"] = rfv3.v3_holdout_manifest_hash(
            manifest_path
        )
        rfb.write_json_atomic(report_path, result["report"])

    report = result["report"]
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("Metadata-only v3 holdout selection (official SEC metadata)")
        print(f"  succeeded : {report['selection_succeeded']}")
        print(f"  pairs     : {report['selected_pair_count']}")
        print(f"  strata    : {report['stratum_distribution']}")
        print(f"  requests  : {report['metadata_endpoints_contacted']}")
        print(f"  body requests: {report['filing_body_requests']} (structurally zero)")
        for source in report["prior_corpus_exclusions_applied"]:
            print(
                f"  excluded  : {source['benchmark_id']} — "
                f"{source['excluded_cik_count']} CIKs, "
                f"{source['excluded_accession_count']} accessions"
            )
        for failure in report["failures"]:
            print(f"  FAILED [{failure['code']}] {failure.get('detail', '')}")
        for pair in report["selected_pairs"]:
            print(
                f"    {pair['stratum_id']}  CIK {pair['cik']}  "
                f"{pair['issuer_name']}  "
                f"{pair['previous_accession']} -> {pair['current_accession']}"
            )
        if result["selected"]:
            print(
                "\n  Manifest and selection report written. Committing them "
                "is the freeze act. No filing body was contacted; "
                "source_verified is false everywhere; acquisition is a later, "
                "separate step, and extraction must not run until the "
                "acquired bodies are checksum-verified."
            )
        else:
            print(
                "\n  Selection FAILED under the predeclared protocol. The "
                "failed report was written; no manifest was frozen, the "
                "protocol was not altered, and no filing body was contacted."
            )

    return 0 if result["selected"] else 1


if __name__ == "__main__":
    sys.exit(main())
