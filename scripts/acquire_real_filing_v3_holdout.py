"""Acquire and source-verify the v3 extraction holdout's filing bodies.

    # Network is OFF unless you say otherwise. This does nothing without it:
    python scripts/acquire_real_filing_v3_holdout.py

    export SEC_USER_AGENT="Jane Doe Research jane.doe@university.edu"
    python scripts/acquire_real_filing_v3_holdout.py --allow-network
    python scripts/acquire_real_filing_v3_holdout.py --allow-network --json

Downloads the exact twenty primary 10-K documents frozen in the committed v3
holdout manifest from official SEC archive URLs, checksums the decoded bytes,
stores them under the gitignored ``benchmark_data/real_filing_v3_holdout_v1/``
tree, and — only when all twenty verify — advances the committed manifest
exactly one step (``holdout_frozen_metadata_only`` -> ``source_verified``) and
writes the committed source-verification report. Committing those two files is
the verification act; this tool never commits anything.

This command ends at bytes-on-disk. It cannot run extraction, unit
segmentation, ingestion, Chroma, comparison, packet generation, annotation,
evaluation, or sign-off — an import-graph test pins that. A source that cannot
be acquired or verified FAILS the phase: the manifest is not advanced, the pair
is not replaced, verified local files are preserved, and a bounded failed
report is written LOCALLY (never committed). An explicit rerun reuses verified
local sources and continues.

Exit codes
----------
0  all twenty filings verified; manifest advanced (or already advanced) and the
   committed report written
1  at least one source failed acquisition or verification; nothing advanced
2  invalid configuration or arguments (no network flag, bad user agent, invalid
   or drifted manifest, ...)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402
import real_filing_acquisition as rfa  # noqa: E402
import real_filing_benchmark as rfb  # noqa: E402
import real_filing_v3_holdout as rfv3  # noqa: E402
import real_filing_v3_holdout_acquisition as rfv3a  # noqa: E402

FAILED_REPORT_NAME = "source_verification_failed.json"
REVERIFICATION_REPORT_NAME = "source_reverification.json"


def default_report_path() -> Path:
    return rfv3.default_v3_holdout_dir() / "source_verification_report.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Acquire and checksum the frozen v3 extraction-holdout filing "
            "bodies from official SEC sources. Network access is disabled "
            "unless requested. Never runs extraction."
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
    parser.add_argument("--manifest", metavar="PATH", help="v3 holdout manifest path")
    parser.add_argument(
        "--corpus-dir", metavar="PATH", help="local v3 holdout corpus directory"
    )
    parser.add_argument(
        "--report-out",
        metavar="PATH",
        help="source-verification report output path (default: committed location)",
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
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest or rfv3.default_v3_holdout_manifest_path())
    try:
        manifest = rfv3.load_v3_holdout_manifest(manifest_path)
        rfv3a.verify_frozen_identity(manifest)
        rfv3a.verify_manifest_hash_chain(manifest_path)
    except rfb.BenchmarkError as exc:
        print(f"Refusing acquisition [{exc.code}]: {exc.message}", file=sys.stderr)
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

    layout = rfb.CorpusLayout(args.corpus_dir or config.REAL_FILING_V3_HOLDOUT_DIR)
    fetcher = rfa.Fetcher(
        user_agent=user_agent,
        allow_network=True,
        transport=rfv3a.identity_bound_transport,
        min_interval_seconds=args.min_interval,
        timeout_seconds=args.timeout,
        max_attempts=args.max_attempts,
    )

    prior_manifest_sha256 = rfv3.v3_holdout_manifest_hash(manifest_path)
    acquisition = rfv3a.acquire_v3_manifest(manifest, fetcher=fetcher, layout=layout)

    new_manifest_sha256: str | None = None
    reproducible_hash: str | None = None
    described = manifest
    transitioned = False
    if acquisition["all_verified"]:
        try:
            rfv3a.verify_source_identity_uniqueness(acquisition)
        except rfb.BenchmarkError as exc:
            print(
                f"Refusing to advance [{exc.code}]: {exc.message}", file=sys.stderr
            )
            return 2
        if manifest["status"] == rfv3.STATUS_HOLDOUT_FROZEN_METADATA_ONLY:
            advanced = rfv3a.advance_v3_holdout_manifest(manifest, acquisition)
            rfb.write_json_atomic(manifest_path, advanced)
            new_manifest_sha256 = rfv3.v3_holdout_manifest_hash(manifest_path)
            reproducible_hash = rfv3.reproducible_manifest_hash(advanced)
            described = advanced
            transitioned = True
        else:
            # Rerun after the transition: everything verified against the
            # frozen digests; the manifest is already one step ahead and is
            # not rewritten, so no timestamp or reproducible field moves.
            new_manifest_sha256 = prior_manifest_sha256
            reproducible_hash = rfv3.reproducible_manifest_hash(manifest)

    report = rfv3a.build_source_verification_report(
        manifest=manifest,
        acquisition=acquisition,
        prior_manifest_sha256=prior_manifest_sha256,
        new_manifest_sha256=new_manifest_sha256,
        new_reproducible_manifest_hash=reproducible_hash,
        generated_at=rfb.utc_now_iso(),
    )

    if acquisition["all_verified"]:
        try:
            rfv3a.verify_report_manifest_binding(report, described)
        except rfb.BenchmarkError as exc:
            print(
                f"Refusing to write report [{exc.code}]: {exc.message}",
                file=sys.stderr,
            )
            return 2
        if transitioned or args.report_out:
            report_path = Path(args.report_out or default_report_path())
        else:
            # A rerun against an already-verified manifest transitions
            # nothing. Overwriting the committed report with it would replace
            # the record of the acquisition that actually happened — its
            # download counts and its prior->new hash chain back to the
            # metadata-only freeze — with a no-op whose prior and new hashes
            # are the same value. The re-verification is real and worth
            # recording, so it goes to the gitignored tree instead.
            report_path = layout.root / REVERIFICATION_REPORT_NAME
    else:
        # A failed phase never touches the committed benchmarks/ directory:
        # the bounded failed report stays in the gitignored corpus tree.
        report_path = layout.root / FAILED_REPORT_NAME
    rfb.write_json_atomic(report_path, report)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("v3 holdout source verification (official SEC sources)")
        print(f"  outcome   : {report['verification_outcome']}")
        print(
            f"  requested={acquisition['requested_filings']} "
            f"verified={acquisition['verified_filings']} "
            f"downloaded={acquisition['downloaded']} "
            f"cached={acquisition['reused_verified_cache']} "
            f"failed={acquisition['failed']}"
        )
        print(f"  requests  : {acquisition['request_count']}")
        print(f"  bytes     : {acquisition['total_verified_bytes']}")
        for item in acquisition["filings"]:
            mark = "ok " if item["verified"] else "FAILED"
            detail = f" [{item['failure_code']}]" if item.get("failure_code") else ""
            print(
                f"  {mark:<7} {item['pair_id']}/{item['side']} "
                f"{item['outcome']} -> {item['source_path']}{detail}"
            )
        if acquisition["all_verified"]:
            if new_manifest_sha256 != prior_manifest_sha256:
                print(
                    "\n  All twenty sides verified. The manifest advanced "
                    f"{rfv3.STATUS_HOLDOUT_FROZEN_METADATA_ONLY!r} -> "
                    f"{rfb.STATUS_SOURCE_VERIFIED!r}"
                )
            else:
                print(
                    "\n  All twenty sides verified against the frozen "
                    "digests; the manifest was already source_verified and "
                    "was not rewritten. The committed source-verification "
                    "report records the transition and was left untouched; "
                    "this re-verification was written to the local corpus "
                    "tree."
                )
            print(
                "  Source verification is not parser validation: the frozen "
                "parser has NOT run, and whether these filings contain "
                "extractable Item 1A sections is unknown until the separate "
                "blind extraction run reports it."
            )
        else:
            print(
                "\n  Source verification FAILED. The committed manifest was "
                "NOT advanced, no pair was replaced, verified local files "
                "were preserved, and the failed report was written locally. "
                "Fix the cause and rerun explicitly."
            )

    return 0 if acquisition["all_verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
