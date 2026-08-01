"""Run the blind Item 1A extraction over the source-verified holdout.

    python scripts/run_real_filing_holdout_blind_extraction.py
    python scripts/run_real_filing_holdout_blind_extraction.py --json
    python scripts/run_real_filing_holdout_blind_extraction.py --report-dir DIR

Offline and credential-free: no network flag exists because no network code is
reachable — sources must already be locally verified by the separate,
explicitly networked acquisition step, and a missing or drifted source refuses
the run rather than re-downloading anything.

One predeclared execution, in this exact order:

1. validate the committed holdout manifest and refuse on ANY frozen-identity
   drift (parser bytes, exclusions, manifest hash chain);
2. hash every frozen code file;
3. verify all twenty local source bodies against the committed digests;
4. run the EXISTING ingestion + extraction + comparison path over every pair,
   in manifest order, recording each side's outcome exactly as observed;
5. recompute every frozen code hash and require exact equality;
6. advance the manifest exactly one step (source_verified -> corpus_built) —
   the transition asserts that a recorded outcome exists for every side, not
   that extraction succeeded;
7. write the committed bounded reports (blind extraction report, unlabeled
   execution report, annotation packet inventory);
8. generate LOCAL machine-proposed annotation packets for pairs whose
   comparison actually detected.

If extraction exposes a parser defect, the defect is recorded and the blind
result preserved. This command never modifies the parser, never replaces a
pair, and never produces a human_verified label or any accuracy metric.

Exit codes: 0 run completed and reports written (regardless of extraction
outcomes — a recorded failure is a completed blind result), 1 the run itself
failed (frozen-code drift mid-run, incomplete attempt coverage), 2 invalid
configuration or refused preconditions (status, drifted identity, missing or
drifted sources).
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
import real_filing_benchmark as rfb  # noqa: E402
import real_filing_holdout as rfh  # noqa: E402
import real_filing_holdout_extraction as rfhe  # noqa: E402

BLIND_REPORT_NAME = "blind_extraction_report.json"
EXECUTION_REPORT_NAME = "execution_report.json"
PACKET_INVENTORY_NAME = "annotation_packet_inventory.json"


def _generate_packets(
    manifest: dict, layout: rfb.CorpusLayout, records: list[dict]
) -> list[dict]:
    """Machine-proposed packets for pairs whose comparison DETECTED.

    Uses the existing packet generator unchanged. Returns bounded inventory
    rows only; packet bodies and annotations stay in the gitignored corpus.
    """
    from scripts import create_real_filing_annotation_packets as packets

    rows: list[dict] = []
    for record in records:
        pair_id = record["pair_id"]
        execution = record.get("execution") or {}
        base = {
            "pair_id": pair_id,
            "previous_extraction_outcome": record["previous"]["extraction_outcome"],
            "current_extraction_outcome": record["current"]["extraction_outcome"],
            "previous_section_hash": record["previous"]["section_hash"],
            "current_section_hash": record["current"]["section_hash"],
            "previous_unit_count": record["previous"]["unit_count"],
            "current_unit_count": record["current"]["unit_count"],
            "annotation_status": rfb.ANNOTATION_MACHINE_PROPOSED,
            "human_verified": False,
        }
        if not execution.get("executed"):
            rows.append(
                {
                    **base,
                    "packet_status": "blocked",
                    "blocking_reason": (
                        "comparison_not_detected"
                        if rfb.build_is_evaluable(record)
                        else "item_1a_not_extracted_for_both_sides"
                    ),
                    "packet_hash": None,
                    "label_count": 0,
                    "review_ready": False,
                }
            )
            continue
        packet, annotation = packets.build_packet(pair_id, layout, manifest)
        rfb.write_json_atomic(layout.packet_json_path(pair_id), packet)
        markdown_path = layout.packet_markdown_path(pair_id)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(
            packets.render_markdown(packet), encoding="utf-8"
        )
        rfb.write_json_atomic(layout.machine_proposed_path(pair_id), annotation)
        rows.append(
            {
                **base,
                "packet_status": "written",
                "blocking_reason": None,
                "packet_hash": rfb.payload_hash(packet),
                "label_count": len(annotation["labels"]),
                "review_ready": True,
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Blind Item 1A extraction over the frozen holdout corpus. "
            "Offline: no network, no AWS, no LLM, no parser modification."
        )
    )
    parser.add_argument("--manifest", metavar="PATH", help="holdout manifest path")
    parser.add_argument(
        "--corpus-dir", metavar="PATH", help="local holdout corpus directory"
    )
    parser.add_argument(
        "--report-dir",
        metavar="PATH",
        help="directory for the committed reports (default: beside the manifest)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest or rfh.default_holdout_manifest_path())
    layout = rfb.CorpusLayout(args.corpus_dir or config.REAL_FILING_HOLDOUT_DIR)
    report_dir = Path(args.report_dir or manifest_path.parent)

    try:
        manifest = rfh.load_holdout_manifest(manifest_path)
    except rfb.BenchmarkError as exc:
        print(f"Invalid holdout manifest [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2

    try:
        run = rfhe.run_blind_extraction(manifest, manifest_path, layout)
    except rfhe.HoldoutExtractionError as exc:
        refused_preconditions = exc.code in (
            rfhe.FAILURE_STATUS_NOT_EXTRACTABLE,
            rfhe.FAILURE_PARSER_SOURCE_DRIFT,
            rfhe.FAILURE_EXCLUSION_DRIFT,
            rfhe.FAILURE_MANIFEST_HASH_DRIFT,
            rfhe.FAILURE_SOURCE_MISSING,
            rfhe.FAILURE_SOURCE_CHECKSUM_DRIFT,
            "holdout_report_unreadable",
        )
        print(f"Blind run refused [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2 if refused_preconditions else 1
    except rfb.BenchmarkError as exc:
        print(f"Blind run failed [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1

    records = run["records"]

    # Manifest transition: exactly one step, only from source_verified, and
    # only because a recorded outcome exists for every side. A rebuild at
    # corpus_built regenerates reports without touching the manifest.
    new_manifest_sha256: str | None
    if manifest["status"] == rfb.STATUS_SOURCE_VERIFIED:
        advanced = rfhe.advance_holdout_manifest_to_corpus_built(manifest, records)
        rfb.write_json_atomic(manifest_path, advanced)
        new_manifest_sha256 = rfb.sha256_file(manifest_path)
        new_status = rfb.STATUS_CORPUS_BUILT
    else:
        new_manifest_sha256 = run["prior_manifest_sha256"]
        new_status = manifest["status"]

    generated_at = rfb.utc_now_iso()
    blind_report = rfhe.build_blind_extraction_report(
        manifest=manifest,
        run=run,
        new_manifest_status=new_status,
        new_manifest_sha256=new_manifest_sha256,
        generated_at=generated_at,
    )
    execution_report = rfhe.build_holdout_execution_report(
        manifest=manifest,
        run=run,
        manifest_sha256=new_manifest_sha256,
        generated_at=generated_at,
    )
    packet_rows = _generate_packets(manifest, layout, records)
    inventory = rfhe.build_holdout_packet_inventory(
        manifest=manifest,
        packet_results=packet_rows,
        manifest_sha256=new_manifest_sha256,
        build_source_manifest_hash=run["prior_manifest_sha256"],
        generated_at=generated_at,
    )

    rfb.write_json_atomic(report_dir / BLIND_REPORT_NAME, blind_report)
    rfb.write_json_atomic(report_dir / EXECUTION_REPORT_NAME, execution_report)
    rfb.write_json_atomic(report_dir / PACKET_INVENTORY_NAME, inventory)

    if args.json:
        print(json.dumps(blind_report, indent=2, sort_keys=True))
    else:
        totals = blind_report["extraction_totals"]
        print("Holdout blind extraction (frozen parser, offline)")
        print(
            f"  manifest  : {run['prior_manifest_status']} -> {new_status}"
        )
        print(
            f"  parser    : {manifest['frozen_extraction_parser_version']} "
            f"(source unchanged: {blind_report['frozen_code_unchanged']})"
        )
        print(
            f"  sides     : extracted={totals['extracted']} "
            f"missing={totals['missing']} ambiguous={totals['ambiguous']} "
            f"parse_failed={totals['parse_failed']}"
        )
        print(
            f"  pairs     : built={blind_report['pairs_built']} "
            f"fully_extracted={blind_report['pairs_fully_extracted']} "
            f"comparisons={blind_report['comparison_runs']}"
        )
        written = sum(1 for row in packet_rows if row["packet_status"] == "written")
        print(
            f"  packets   : written={written} "
            f"blocked={len(packet_rows) - written} "
            "(all machine_proposed; zero human_verified labels)"
        )
        for record in records:
            execution = record.get("execution") or {}
            print(
                f"  {record['pair_id']:<16} "
                f"previous={record['previous']['extraction_outcome']:<12} "
                f"current={record['current']['extraction_outcome']:<12} "
                f"changes={execution.get('change_count', 0)} "
                f"lifecycle={execution.get('lifecycle') or 'not-run'}"
            )
        print(
            "\n  Blind coverage only. No accuracy, precision, recall, or "
            "generalization number exists: zero labels are human-verified. "
            "Filing content stays in the gitignored benchmark_data/ tree."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
