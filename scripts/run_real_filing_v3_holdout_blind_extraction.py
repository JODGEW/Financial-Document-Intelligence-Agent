"""Run the blind Item 1A extraction over the source-verified v3 holdout.

    python scripts/run_real_filing_v3_holdout_blind_extraction.py
    python scripts/run_real_filing_v3_holdout_blind_extraction.py --json
    python scripts/run_real_filing_v3_holdout_blind_extraction.py --report-dir DIR

Offline and credential-free: no network flag exists because no network code is
reachable — sources must already be locally verified by the separate,
explicitly networked acquisition step, and a missing or drifted source refuses
the run rather than re-downloading anything. No SEC_USER_AGENT is read or
required, no AWS or Bedrock call is made, and no embedding service is
contacted (the corpus build seeds Chroma with a deterministic local embedding
function whose query path raises).

One predeclared execution, in this exact order:

1. validate the committed v3 holdout manifest and refuse on ANY frozen-identity
   drift (parser/detector/workflow/evaluator bytes, prior-corpus exclusions,
   live pipeline versions, the source-verification report binding, the manifest
   hash chain);
2. refuse if any local annotation claims a status beyond machine_proposed — a
   human decision is never an input to a blind run;
3. resolve all twenty local source paths deterministically and refuse
   traversal, duplicates, and any tracked location;
4. verify all twenty local source bodies against the committed digests, and
   compute this run's deterministic identity;
5. hash every frozen code file;
6. run the EXISTING ingestion + extraction + unitization + comparison path over
   every pair, in manifest order, previous side before current side, recording
   each side's outcome exactly as observed;
7. recompute every frozen code hash and require exact equality;
8. advance the manifest exactly one step (source_verified -> corpus_built) —
   the transition asserts that a recorded outcome exists for every side, not
   that extraction succeeded;
9. generate LOCAL machine-proposed annotation packets for pairs whose
   comparison actually detected, through the existing packet generator;
10. write the committed bounded reports (blind extraction report, unlabeled
    execution report, annotation packet inventory), refusing to overwrite an
    existing report that disagrees.

If the run exposes a defect in the frozen pipeline, the defect is recorded and
the blind result preserved. This command never modifies a semantic component,
never replaces a pair, never omits a blocked side or pair, never runs the gold
evaluator, and never produces a human_verified label or any accuracy metric.

Exit codes: 0 run completed and reports written (regardless of extraction or
comparison outcomes — a recorded failure is a completed blind result), 1 the
run itself failed (frozen-code drift mid-run, incomplete attempt coverage,
disagreeing existing artifact), 2 invalid configuration or refused
preconditions (status, drifted identity, missing or drifted sources, a human
annotation present).
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
import real_filing_v3_holdout as rfv3  # noqa: E402
import real_filing_v3_holdout_extraction as rfx  # noqa: E402

BLIND_REPORT_NAME = "blind_extraction_report.json"
EXECUTION_REPORT_NAME = "execution_report.json"
PACKET_INVENTORY_NAME = "annotation_packet_inventory.json"


def _packet_row(
    record: dict,
    *,
    layout: rfb.CorpusLayout,
    packet: dict | None,
    annotation: dict | None,
    blocking_reason: str | None,
) -> dict:
    """One bounded inventory row. Identity, counts, hashes, and bindings only —
    never packet prose, unit text, or an absolute path.

    Every row is machine_proposed with a null annotator and a null verification
    timestamp; nothing here can be edited into gold, because gold requires a
    person to edit the local annotation file itself.
    """
    pair_id = record["pair_id"]
    execution = record.get("execution") or {}
    labels = (annotation or {}).get("labels", [])
    labelled_unit_ids = {
        label[field]
        for label in labels
        for field in ("previous_unit_id", "current_unit_id")
        if label[field] is not None
    }
    return {
        "pair_id": pair_id,
        "packet_id": f"pkt-{pair_id}",
        "packet_status": "written" if packet is not None else "blocked",
        "packet_relative_path": (
            layout.relative(layout.packet_json_path(pair_id))
            if packet is not None
            else None
        ),
        "packet_sha256": rfb.payload_hash(packet) if packet is not None else None,
        "blocking_reason": blocking_reason,
        "annotation_status": rfb.ANNOTATION_MACHINE_PROPOSED,
        "human_verified": False,
        "annotator_id": None,
        "verification_timestamp": None,
        "label_count": len(labels),
        "annotation_relative_path": (
            layout.relative(layout.machine_proposed_path(pair_id))
            if annotation is not None
            else None
        ),
        "annotation_sha256": (
            rfb.annotation_hash(annotation) if annotation is not None else None
        ),
        "previous_extraction_outcome": record["previous"]["extraction_outcome"],
        "current_extraction_outcome": record["current"]["extraction_outcome"],
        "previous_section_hash": record["previous"]["section_hash"],
        "current_section_hash": record["current"]["section_hash"],
        "previous_unit_count": record["previous"]["unit_count"],
        "current_unit_count": record["current"]["unit_count"],
        "previous_canonical_unit_id_count": len(record["previous"]["units"]),
        "current_canonical_unit_id_count": len(record["current"]["units"]),
        "labelled_unit_id_count": len(labelled_unit_ids),
        "comparison_result_hash": execution.get("result_hash"),
        "review_ready": packet is not None,
    }


def generate_packets(
    manifest: dict, layout: rfb.CorpusLayout, records: list[dict]
) -> list[dict]:
    """Machine-proposed packets for pairs whose comparison DETECTED.

    Uses the existing packet generator unchanged, so packet rows and machine
    proposals bind the same canonical sequence-aware unit identities the build
    records carry, and a repeated normalized heading keeps one row per
    occurrence. Returns bounded inventory rows only; packet bodies and
    annotations stay in the gitignored corpus.
    """
    from scripts import create_real_filing_annotation_packets as packets

    rows: list[dict] = []
    for record in records:
        pair_id = record["pair_id"]
        execution = record.get("execution") or {}
        if not execution.get("executed"):
            rows.append(
                _packet_row(
                    record,
                    layout=layout,
                    packet=None,
                    annotation=None,
                    blocking_reason=(
                        rfx.PACKET_BLOCKED_NOT_DETECTED
                        if rfb.build_is_evaluable(record)
                        else rfx.PACKET_BLOCKED_NOT_EXTRACTED
                    ),
                )
            )
            continue
        packet, annotation = packets.build_packet(pair_id, layout, manifest)
        rfb.write_json_atomic(layout.packet_json_path(pair_id), packet)
        markdown_path = layout.packet_markdown_path(pair_id)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(packets.render_markdown(packet), encoding="utf-8")
        rfb.write_json_atomic(layout.machine_proposed_path(pair_id), annotation)
        rows.append(
            _packet_row(
                record,
                layout=layout,
                packet=packet,
                annotation=annotation,
                blocking_reason=None,
            )
        )
    return rows


def _write_report(path: Path, payload: dict, conflict_code: str) -> str:
    """Write a committed report, or preserve an identical existing one.

    A rerun over identical sources under identical frozen code produces an
    identical reproducible payload; rewriting it would only churn the
    timestamp, so the committed bytes are left exactly as they are. A payload
    that DISAGREES fails closed: the recorded canonical artifact is never
    silently replaced.
    """
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise rfx.V3BlindExtractionError(
                rfx.FAILURE_REPORT_UNREADABLE,
                f"an existing {path.name} could not be read "
                f"({type(exc).__name__})",
            ) from None
        if rfx.payloads_agree(existing, payload):
            return "unchanged"
        raise rfx.V3BlindExtractionError(
            conflict_code,
            f"an existing {path.name} records a different outcome for this "
            "run identity. The recorded execution attempt is preserved; the "
            "rerun is refused rather than overwriting it.",
        )
    rfb.write_json_atomic(path, payload)
    return "written"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Blind Item 1A extraction over the frozen v3 holdout corpus. "
            "Offline: no network, no AWS, no LLM, no semantic modification."
        )
    )
    parser.add_argument("--manifest", metavar="PATH", help="v3 holdout manifest path")
    parser.add_argument(
        "--corpus-dir", metavar="PATH", help="local v3 holdout corpus directory"
    )
    parser.add_argument(
        "--report-dir",
        metavar="PATH",
        help="directory for the committed reports (default: beside the manifest)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest or rfv3.default_v3_holdout_manifest_path())
    layout = rfb.CorpusLayout(args.corpus_dir or config.REAL_FILING_V3_HOLDOUT_DIR)
    report_dir = Path(args.report_dir or manifest_path.parent)

    try:
        manifest = rfv3.load_v3_holdout_manifest(manifest_path)
    except rfb.BenchmarkError as exc:
        print(
            f"Invalid v3 holdout manifest [{exc.code}]: {exc.message}",
            file=sys.stderr,
        )
        return 2

    try:
        run = rfx.run_blind_extraction(
            manifest, manifest_path, layout, report_dir=report_dir
        )
    except rfx.V3BlindExtractionError as exc:
        print(f"Blind run refused [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2 if exc.code in rfx.PREFLIGHT_FAILURE_CODES else 1
    except rfb.BenchmarkError as exc:
        print(f"Blind run failed [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1

    records = run["records"]

    # Manifest transition: exactly one step, only from source_verified, and
    # only because a recorded outcome exists for every side. A rerun at
    # corpus_built regenerates reports without touching the manifest.
    try:
        if manifest["status"] == rfb.STATUS_SOURCE_VERIFIED:
            advanced = rfx.advance_v3_holdout_manifest_to_corpus_built(
                manifest, records
            )
            rfb.write_json_atomic(manifest_path, advanced)
            new_manifest_sha256 = rfb.sha256_file(manifest_path)
            new_status = rfb.STATUS_CORPUS_BUILT
        else:
            new_manifest_sha256 = run["prior_manifest_sha256"]
            new_status = manifest["status"]
    except rfb.BenchmarkError as exc:
        print(
            f"Manifest transition refused [{exc.code}]: {exc.message}",
            file=sys.stderr,
        )
        return 1

    generated_at = rfb.utc_now_iso()
    try:
        packet_rows = generate_packets(manifest, layout, records)
        blind_report = rfx.build_blind_extraction_report(
            manifest=manifest,
            run=run,
            packet_rows=packet_rows,
            new_manifest_status=new_status,
            new_manifest_sha256=new_manifest_sha256,
            generated_at=generated_at,
            layout=layout,
        )
        execution_report = rfx.build_execution_report(
            manifest=manifest,
            run=run,
            manifest_sha256=new_manifest_sha256,
            generated_at=generated_at,
        )
        rfx.validate_execution_report(execution_report)
        inventory = rfx.build_packet_inventory(
            manifest=manifest,
            run=run,
            packet_rows=packet_rows,
            manifest_sha256=new_manifest_sha256,
            generated_at=generated_at,
        )
        rfx.validate_packet_inventory(inventory)

        outcomes = {
            BLIND_REPORT_NAME: _write_report(
                report_dir / BLIND_REPORT_NAME,
                blind_report,
                rfx.FAILURE_EXISTING_ARTIFACT_MISMATCH,
            ),
            EXECUTION_REPORT_NAME: _write_report(
                report_dir / EXECUTION_REPORT_NAME,
                execution_report,
                rfx.FAILURE_EXISTING_ARTIFACT_MISMATCH,
            ),
            PACKET_INVENTORY_NAME: _write_report(
                report_dir / PACKET_INVENTORY_NAME,
                inventory,
                rfx.FAILURE_PACKET_INVENTORY_MISMATCH,
            ),
        }
    except rfb.BenchmarkError as exc:
        print(f"Blind run failed [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(blind_report, indent=2, sort_keys=True))
    else:
        totals = blind_report["extraction_status_counts"]
        print("v3 holdout blind extraction (frozen v3 pipeline, offline)")
        print(f"  run       : {blind_report['run_id']}")
        print(f"  manifest  : {run['prior_manifest_status']} -> {new_status}")
        print(
            f"  pipeline  : {manifest['frozen_extraction_parser_version']} / "
            f"{manifest['frozen_unit_grammar_version']} / "
            f"{manifest['frozen_detector_version']} / "
            f"{manifest['frozen_workflow_version']} "
            f"(code unchanged: {blind_report['frozen_code_unchanged']})"
        )
        print(
            f"  sides     : extracted={totals['extracted']} "
            f"missing={totals['missing']} ambiguous={totals['ambiguous']} "
            f"parse_failed={totals['parse_failed']}"
        )
        print(
            f"  pairs     : buildable={blind_report['buildable_pair_count']} "
            f"blocked={blind_report['blocked_pair_count']} "
            f"comparisons={blind_report['comparison_result_count']}"
        )
        print(
            f"  packets   : written={blind_report['packet_count']} "
            f"blocked={len(packet_rows) - blind_report['packet_count']} "
            f"labels={blind_report['machine_proposed_label_count']} "
            "(all machine_proposed; zero human_verified labels)"
        )
        for row in blind_report["pairs"]:
            print(
                f"  {row['pair_id']:<16} "
                f"previous={row['previous_extraction_status']:<12} "
                f"current={row['current_extraction_status']:<12} "
                f"changes={row['change_count']} "
                f"blocked={row['blocked_reason'] or '-'}"
            )
        for name, outcome in outcomes.items():
            print(f"  report    : {name} {outcome}")
        print(
            "\n  Blind coverage only. No accuracy, precision, recall, "
            "exact-match, or generalization number exists: zero labels are "
            "human-verified, and machine proposals can never enter a metric. "
            "Filing content stays in the gitignored benchmark_data/ tree."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
