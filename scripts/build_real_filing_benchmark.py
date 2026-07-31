"""Build the local Item 1A corpus for the real-filing benchmark.

    python scripts/build_real_filing_benchmark.py                 # build every pair
    python scripts/build_real_filing_benchmark.py --pair-id X     # one pair
    python scripts/build_real_filing_benchmark.py --no-detect     # extraction only
    python scripts/build_real_filing_benchmark.py --json

For each manifest pair this verifies the cached source checksums, parses both
filings **through the existing ingestion path**, mints filing identity through
the existing registry contracts, extracts Item 1A through the existing
section-identification path, runs the existing comparison workflow, and records
bounded structural metadata.

Nothing in the extraction or detection algorithm is touched. That is the whole
point: a benchmark that adjusts the thing it measures measures nothing. If real
filings extract badly, this records ``missing`` / ``ambiguous`` / ``parse_failed``
and reports it — the honest result — rather than loosening a heading rule until
the numbers look better.

Offline and credential-free
---------------------------
Chroma is seeded with a deterministic local embedding function purely so it
will accept documents; detection reads sections by metadata and never embeds,
which this module asserts by raising if a query embedding is ever requested.
Acquisition is a separate, explicitly network-gated command.

Output hygiene
--------------
Extracted section text is written ONLY into the gitignored corpus directory.
Build records carry hashes, counts, headings, and outcomes — never section
text. Printed output carries corpus-relative paths only, never an absolute
local path.

Exit codes: 0 every requested pair built, 1 a build/verification failure,
2 invalid configuration or arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import comparison_detector  # noqa: E402
import comparison_store  # noqa: E402
import filing_registry  # noqa: E402
import ingest  # noqa: E402
import real_filing_benchmark as rfb  # noqa: E402

BUILD_STATUS_BUILT = "built"
BUILD_STATUS_FAILED = "failed"


class BuildError(rfb.BenchmarkError):
    """A pair could not be built to a recordable outcome."""


class _BenchmarkEmbeddings:
    """Deterministic offline embeddings used only to seed Chroma.

    Identical in intent to the regression suite's fixture embeddings: the
    detector reads sections by metadata, so a query embedding is never
    legitimate and asking for one raises instead of silently falling back to a
    vector search.
    """

    def embed_documents(self, texts):
        return [[float(len(text) % 7), 1.0] for text in texts]

    def embed_query(self, text):
        raise AssertionError(
            "the benchmark corpus build must never embed a query; the "
            "comparison detector reads sections by metadata"
        )


# --- Source verification ------------------------------------------------------


def verify_sources(
    pair: dict[str, Any], layout: rfb.CorpusLayout
) -> dict[str, dict[str, Any]]:
    """Every side's cached bytes must match the frozen manifest digest.

    Refusing here is what makes a build record mean something: it was produced
    from exactly the documents the manifest names.
    """
    verified: dict[str, dict[str, Any]] = {}
    for side, payload in rfb.pair_sides(pair):
        path = layout.source_file(pair["pair_id"], side, payload["primary_document"])
        if not path.exists():
            raise BuildError(
                "source_not_acquired",
                f"{pair['pair_id']}/{side}: source document has not been "
                "acquired locally; run the acquisition command first",
            )
        if payload["expected_sha256"] == rfb.PLACEHOLDER_SHA256:
            raise BuildError(
                "source_hash_is_placeholder",
                f"{pair['pair_id']}/{side}: the manifest carries a placeholder "
                "digest, so the local source cannot be verified. Freeze the "
                "real digest before building.",
            )
        observed = rfb.sha256_file(path)
        if observed != payload["expected_sha256"]:
            raise BuildError(
                "source_checksum_drift",
                f"{pair['pair_id']}/{side}: local source digest does not match "
                "the frozen manifest; the corpus and the manifest disagree",
            )
        verified[side] = {
            "path": path,
            "source_sha256": observed,
            "payload": payload,
        }
    return verified


# --- Ingestion through the existing path --------------------------------------


def _workspace_source_name(pair: dict[str, Any], side: str, payload: dict) -> str:
    """A deterministic, distinct file name for one side inside the workspace.

    Chunk ids embed the source file name, so the two sides must not share one.
    The name is derived only from frozen manifest values, so two builds of the
    same pair produce byte-identical chunk ids.
    """
    suffix = Path(payload["primary_document"]).suffix.lower()
    return f"{pair['pair_id']}-{side}-{payload['reporting_period']}{suffix}"


# Used only if the client will not report its own bound.
_CHROMA_FALLBACK_BATCH = 4096


def _add_in_batches(chroma, documents: list, ids: list[str]) -> None:
    """Upsert in client-sized batches.

    Chroma refuses a single upsert larger than its own max batch size, and a
    real 10-K comfortably exceeds it: Verizon's FY2022 filing alone produces
    ~5.8k chunks against a 5,461 limit. The synthetic fixtures are orders of
    magnitude too small to reach it, so this surfaces only against real
    filings.

    Batching is purely how the write is delivered — same chunks, same stable
    ids, same order, and upsert stays idempotent. Nothing about what is indexed
    or how a section is later identified changes.
    """
    limit = _CHROMA_FALLBACK_BATCH
    getter = getattr(getattr(chroma, "_client", None), "get_max_batch_size", None)
    if callable(getter):
        try:
            reported = int(getter())
            if reported > 0:
                limit = reported
        except Exception:  # noqa: BLE001 - fall back to the conservative bound
            pass
    for start in range(0, len(documents), limit):
        chroma.add_documents(
            documents=documents[start : start + limit],
            ids=ids[start : start + limit],
        )


def _ingest_pair(
    pair: dict[str, Any],
    verified: dict[str, dict[str, Any]],
    layout: rfb.CorpusLayout,
) -> dict[str, Any]:
    """Copy sources into a clean workspace and run the real ingestion path."""
    from langchain_chroma import Chroma

    workspace = layout.workspace_dir(pair["pair_id"])
    if workspace.exists():
        shutil.rmtree(workspace)
    sources_dir = workspace / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)

    manifest_entries: dict[str, dict[str, Any]] = {}
    side_by_source: dict[str, str] = {}
    for side, info in verified.items():
        payload = info["payload"]
        name = _workspace_source_name(pair, side, payload)
        shutil.copyfile(info["path"], sources_dir / name)
        # The corpus manifest contract, applied to a benchmark workspace: the
        # identity fields are explicit manifest values, never inferred from a
        # file name or an mtime.
        manifest_entries[name] = {
            "company_name": pair["issuer_name"],
            "form_type": filing_registry.normalize_form_type(payload["form"]),
            "period_end": rfb._require_iso_date(  # noqa: SLF001 - shared validator
                payload["reporting_period"],
                f"{pair['pair_id']}.{side}.reporting_period",
                rfb.ManifestSchemaError,
                "manifest_side",
            ),
            "filing_date": rfb._require_iso_date(  # noqa: SLF001
                payload["filing_date"],
                f"{pair['pair_id']}.{side}.filing_date",
                rfb.ManifestSchemaError,
                "manifest_side",
            ),
        }
        side_by_source[name] = side

    registry_path = workspace / "registry.jsonl"
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        documents = ingest.load_documents(
            str(sources_dir),
            manifest=manifest_entries,
            registry_path=registry_path,
        )
        chunks = ingest.split_documents(documents) if documents else []
        unique, ids = ingest._dedupe_by_id(chunks) if chunks else ([], [])
        counts: dict[str, int] = {}
        for chunk in unique:
            rel = chunk.metadata.get("source_path")
            if rel:
                counts[rel] = counts.get(rel, 0) + 1
        if counts:
            filing_registry.update_chunk_counts(counts, registry_path)

        chroma = Chroma(
            collection_name="real_filing_benchmark",
            persist_directory=str(workspace / "chroma"),
            embedding_function=_BenchmarkEmbeddings(),
        )
        if unique:
            _add_in_batches(chroma, unique, ids)

    return {
        "workspace": workspace,
        "registry_path": registry_path,
        "chroma": chroma,
        "chunks": unique,
        "side_by_source": side_by_source,
        "manifest_entries": manifest_entries,
        "ingest_log": buffer.getvalue(),
    }


# --- Item 1A extraction through the existing section path ---------------------


def _paragraph_count(text: str) -> int:
    """Blank-line-separated blocks: a bounded structural signal, not content."""
    return len([block for block in text.split("\n\n") if block.strip()])


def _extract_side(
    side: str,
    source_name: str,
    ingestion: dict[str, Any],
    registry_entry: dict[str, Any] | None,
) -> dict[str, Any]:
    """Record one side's Item 1A extraction outcome and structural metadata.

    Uses the existing section-identification path exactly: chunks carrying
    ``section_key == ingest.SECTION_KEY_ITEM_1A``, ordered by ``chunk_seq``,
    reconstructed and unitized by the existing detector helpers. Nothing here
    re-implements or improves extraction.
    """
    record: dict[str, Any] = {
        "side": side,
        "filing_id": (registry_entry or {}).get("filing_id"),
        "source_name": source_name,
        "source_sha256": (registry_entry or {}).get("source_hash"),
        "parse_status": (registry_entry or {}).get("parse_status"),
        "extraction_outcome": rfb.EXTRACTION_PARSE_FAILED,
        "heading_detected": None,
        "section_hash": None,
        "section_char_count": 0,
        "section_paragraph_count": 0,
        "section_chunk_count": 0,
        "indexed_chunk_count": 0,
        "unit_count": 0,
        "units": [],
        "extraction_detail": None,
        "_section_text": "",
    }

    if record["parse_status"] != filing_registry.PARSED:
        record["extraction_detail"] = (
            f"the loader did not produce a parsed filing "
            f"(registry outcome: {record['parse_status']!r})"
        )
        return record

    filing_chunks = [
        chunk
        for chunk in ingestion["chunks"]
        if chunk.metadata.get("source_path") == source_name
    ]
    record["indexed_chunk_count"] = len(filing_chunks)
    section_chunks = [
        chunk
        for chunk in filing_chunks
        if chunk.metadata.get("section_key") == ingest.SECTION_KEY_ITEM_1A
    ]
    record["section_chunk_count"] = len(section_chunks)

    if not section_chunks:
        record["extraction_outcome"] = rfb.EXTRACTION_MISSING
        record["extraction_detail"] = (
            "no chunk carries the canonical Item 1A section key. The existing "
            "section path derives it from splitter-produced headings only, so "
            "a filing whose Item 1A heading is not emitted as a heading by its "
            "loader records as missing."
        )
        return record

    ordered = sorted(section_chunks, key=lambda chunk: chunk.metadata.get("chunk_seq", 0))
    headings = sorted(
        {
            (chunk.metadata.get("section_title") or "").strip()
            for chunk in ordered
            if (chunk.metadata.get("section_title") or "").strip()
        }
    )
    record["heading_detected"] = (
        headings[0][: rfb.MAX_HEADING_CHARS] if headings else None
    )

    # Ambiguity: the section key is stamped on more than one non-contiguous run
    # of chunks (a table-of-contents entry plus the body section, say). Which
    # run is "the" Item 1A section is not decidable deterministically, so the
    # honest outcome is ambiguous rather than a guess.
    sequences = [chunk.metadata.get("chunk_seq") for chunk in ordered]
    runs = 1
    for previous, following in zip(sequences, sequences[1:]):
        if previous is None or following is None or following != previous + 1:
            runs += 1
    if runs > 1 or len(headings) > 1:
        record["extraction_outcome"] = rfb.EXTRACTION_AMBIGUOUS
        record["extraction_detail"] = (
            f"the Item 1A section key is stamped on {runs} non-contiguous chunk "
            f"run(s) under {len(headings)} distinct heading(s); which run is "
            "the section cannot be decided deterministically"
        )
        return record

    section_chunk_models = [
        comparison_detector.SectionChunk(
            chunk_id=ingest.chunk_id(chunk),
            text=chunk.page_content,
            filing_id=record["filing_id"] or "",
            source_name=chunk.metadata.get("source_name") or "",
            chunk_seq=chunk.metadata.get("chunk_seq"),
            page=chunk.metadata.get("page"),
            section_title=chunk.metadata.get("section_title"),
        )
        for chunk in ordered
    ]
    section_text, _spans = comparison_detector._reconstruct(section_chunk_models)  # noqa: SLF001
    try:
        units = comparison_detector.extract_units(
            section_chunk_models, record["filing_id"] or ""
        )
    except Exception as exc:  # noqa: BLE001 - recorded as an outcome, not raised
        record["extraction_outcome"] = rfb.EXTRACTION_PARSE_FAILED
        record["extraction_detail"] = (
            f"risk-factor units could not be extracted ({type(exc).__name__})"
        )
        return record

    record["extraction_outcome"] = rfb.EXTRACTION_EXTRACTED
    record["section_hash"] = rfb.section_hash(section_text)
    record["section_char_count"] = len(section_text)
    record["section_paragraph_count"] = _paragraph_count(section_text)
    record["unit_count"] = len(units)
    record["units"] = [
        {
            "unit_id": rfb.unit_id(side, index, unit.unit_key),
            "unit_key": unit.unit_key,
            "heading": unit.heading[: rfb.MAX_HEADING_CHARS],
            "char_count": len(unit.text),
            "content_hash": unit.content_hash,
            # Bounded review context, local-only. A reviewer needs enough to
            # judge a unit; nobody needs the section pasted into a record.
            "excerpt": rfb.normalize_text(unit.text)[: rfb.MAX_EXCERPT_CHARS],
        }
        for index, unit in enumerate(units)
    ]
    record["_section_text"] = section_text
    return record


# --- Detection through the existing comparison workflow -----------------------


def _run_workflow(
    pair: dict[str, Any], ingestion: dict[str, Any], sides: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Create the comparison record and run the existing detector.

    Uses ``comparison_store.create_comparison`` and
    ``comparison_detector.detect`` unchanged. Detection is synchronous and
    direct, so no durable job, lease, or retry is created — the operational
    metrics reported later record that honestly (zero job attempts, zero
    reclaims) rather than implying an async path was exercised.
    """
    previous_id = sides["previous"]["filing_id"]
    current_id = sides["current"]["filing_id"]
    execution: dict[str, Any] = {
        "executed": False,
        "comparison_id": None,
        "lifecycle": None,
        "failure_code": None,
        "result_hash": None,
        "detector_version": comparison_detector.DETECTOR_VERSION,
        "workflow_version": comparison_store.WORKFLOW_VERSION,
        "change_count": 0,
        "changes_by_type": {},
        "undetermined_reason_codes": {},
        "evidence_total": 0,
        "evidence_unresolved": 0,
        "evidence_foreign": 0,
        "validation_summary": None,
        "attempts": [],
    }
    if not previous_id or not current_id:
        execution["failure_code"] = "filing_identity_unavailable"
        return execution

    db_path = ingestion["workspace"] / "comparisons.db"
    registry_path = ingestion["registry_path"]
    try:
        record, _created = comparison_store.create_comparison(
            previous_id, current_id, db_path=db_path, registry_path=registry_path
        )
    except comparison_store.ComparisonPairError as exc:
        execution["failure_code"] = "invalid_pair"
        execution["pair_reason_codes"] = list(exc.reason_codes)
        return execution

    comparison_id = record["comparison_id"]
    execution["comparison_id"] = comparison_id
    try:
        result, _detect_created = comparison_detector.detect(
            comparison_id,
            db_path=db_path,
            registry_path=registry_path,
            chroma_client=ingestion["chroma"],
        )
    except comparison_detector.DetectionError as exc:
        execution["lifecycle"] = comparison_store.get_comparison(
            comparison_id, db_path=db_path
        )["status"]
        execution["failure_code"] = exc.code
        execution["attempts"] = _attempt_records(comparison_id, db_path)
        return execution

    stored = comparison_store.get_result(comparison_id, db_path=db_path)
    execution["executed"] = True
    execution["lifecycle"] = comparison_store.get_comparison(
        comparison_id, db_path=db_path
    )["status"]
    execution["result_hash"] = stored["result_hash"]
    execution["change_count"] = len(result["changes"])
    execution["validation_summary"] = result["validation_summary"]

    by_type: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for change in result["changes"]:
        by_type[change["change_type"]] = by_type.get(change["change_type"], 0) + 1
        reason = change.get("undetermined_reason")
        if reason:
            code = reason.split(":", 1)[0].strip()
            reasons[code] = reasons.get(code, 0) + 1
    execution["changes_by_type"] = dict(sorted(by_type.items()))
    execution["undetermined_reason_codes"] = dict(sorted(reasons.items()))

    # Evidence-resolution mechanics, computed exactly as the synthetic suite
    # computes them: does every reference resolve to an indexed chunk of the
    # correct filing?
    for change in result["changes"]:
        for key, owner in (
            ("previous_evidence", previous_id),
            ("current_evidence", current_id),
        ):
            for reference in change[key]:
                execution["evidence_total"] += 1
                if reference["document_id"] != owner:
                    execution["evidence_foreign"] += 1
                found = ingestion["chroma"].get(ids=[reference["chunk_id"]])
                if found.get("ids") != [reference["chunk_id"]]:
                    execution["evidence_unresolved"] += 1

    execution["attempts"] = _attempt_records(comparison_id, db_path)
    execution["result"] = result
    return execution


def _attempt_records(comparison_id: str, db_path: Path) -> list[dict[str, Any]]:
    """Bounded attempt metadata for the operational report (no result JSON)."""
    records = []
    for attempt in comparison_store.list_detection_attempts(
        comparison_id, db_path=db_path
    ):
        records.append(
            {
                "attempt_id": attempt["attempt_id"],
                "attempt_number": attempt["attempt_number"],
                "status": attempt["status"],
                "failure_code": attempt.get("failure_code"),
                "started_at": attempt.get("started_at"),
                "finished_at": attempt.get("finished_at"),
            }
        )
    return records


# --- One pair -----------------------------------------------------------------


def build_pair(
    pair: dict[str, Any],
    manifest: dict[str, Any],
    layout: rfb.CorpusLayout,
    *,
    detect: bool = True,
) -> dict[str, Any]:
    """Verify, parse, extract, execute, and persist one pair's build record."""
    verified = verify_sources(pair, layout)
    ingestion = _ingest_pair(pair, verified, layout)

    entries = {
        entry["source_path"]: entry
        for entry in filing_registry.list_entries(ingestion["registry_path"])
    }
    sides: dict[str, dict[str, Any]] = {}
    section_texts: dict[str, str] = {}
    for source_name, side in ingestion["side_by_source"].items():
        extracted = _extract_side(
            side, source_name, ingestion, entries.get(source_name)
        )
        section_texts[side] = extracted.pop("_section_text")
        sides[side] = extracted

    execution = (
        _run_workflow(pair, ingestion, sides)
        if detect and rfb.build_is_evaluable({**sides, "pair_id": pair["pair_id"]})
        else {
            "executed": False,
            "skipped_reason": (
                "detection not requested"
                if not detect
                else "at least one side did not produce an extracted Item 1A "
                "section, so the comparison workflow was not run"
            ),
        }
    )
    result_payload = execution.pop("result", None)

    record = {
        "record_version": rfb.BUILD_RECORD_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "pair_id": pair["pair_id"],
        "slate_id": pair["slate_id"],
        "issuer_name": pair["issuer_name"],
        "cik": pair["cik"],
        "sector_label": pair["sector_label"],
        "source_manifest_hash": rfb.manifest_hash(),
        "parser_versions": {
            "builder": rfb.BUILDER_VERSION,
            "section_key": ingest.SECTION_KEY_ITEM_1A,
            "detector": comparison_detector.DETECTOR_VERSION,
            "workflow": comparison_store.WORKFLOW_VERSION,
        },
        "previous": sides["previous"],
        "current": sides["current"],
        "execution": execution,
        "built_at": rfb.utc_now_iso(),
    }
    record["build_hash"] = rfb.build_record_hash(record)
    rfb.validate_build_record(record)

    # Extracted text stays in the gitignored corpus directory, never in a
    # committed artifact and never in a printed line.
    for side, text in section_texts.items():
        if text:
            path = layout.section_text_path(pair["pair_id"], side)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if result_payload is not None:
        rfb.write_json_atomic(
            layout.build_dir(pair["pair_id"]) / "detection_result.json",
            result_payload,
        )
    rfb.write_json_atomic(layout.build_record_path(pair["pair_id"]), record)
    return record


def load_build_record(pair_id: str, layout: rfb.CorpusLayout) -> dict[str, Any]:
    path = layout.build_record_path(pair_id)
    if not path.exists():
        raise BuildError(
            "build_record_not_found",
            f"{pair_id}: no corpus build record; run the build command first",
        )
    record = json.loads(path.read_text(encoding="utf-8"))
    rfb.validate_build_record(record)
    return record


def load_detection_result(
    pair_id: str, layout: rfb.CorpusLayout
) -> dict[str, Any] | None:
    path = layout.build_dir(pair_id) / "detection_result.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# --- CLI ----------------------------------------------------------------------


def _append_build_log(layout: rfb.CorpusLayout, entry: dict[str, Any]) -> None:
    path = layout.build_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _summary_line(record: dict[str, Any]) -> str:
    previous = record["previous"]
    current = record["current"]
    execution = record.get("execution") or {}
    return (
        f"  {record['pair_id']:<28} "
        f"previous={previous['extraction_outcome']:<12} "
        f"current={current['extraction_outcome']:<12} "
        f"units={previous['unit_count']}/{current['unit_count']} "
        f"changes={execution.get('change_count', 0)} "
        f"lifecycle={execution.get('lifecycle') or 'not-run'}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build the local Item 1A benchmark corpus from verified sources. "
            "Offline and credential-free: no network, no AWS, no LLM."
        )
    )
    parser.add_argument("--manifest", metavar="PATH", help="benchmark manifest path")
    parser.add_argument(
        "--corpus-dir", metavar="PATH", help="local gitignored corpus directory"
    )
    parser.add_argument(
        "--pair-id", action="append", default=[], help="build only these pairs"
    )
    parser.add_argument(
        "--no-detect",
        action="store_true",
        help="extract Item 1A but do not run the comparison workflow",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        manifest = rfb.load_manifest(args.manifest)
    except rfb.BenchmarkError as exc:
        print(f"Invalid benchmark manifest [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2

    layout = rfb.CorpusLayout(args.corpus_dir)
    pairs = rfb.manifest_pairs(manifest)
    if args.pair_id:
        wanted = set(args.pair_id)
        unknown = sorted(wanted - {pair["pair_id"] for pair in pairs})
        if unknown:
            print(f"Unknown pair ids: {unknown}", file=sys.stderr)
            return 2
        pairs = [pair for pair in pairs if pair["pair_id"] in wanted]

    if not pairs:
        print(
            "The benchmark manifest has no resolved pairs. Its status is "
            f"{manifest['status']!r}: remote metadata (CIK, accession numbers, "
            "dates, primary documents, digests) has not been resolved from "
            "official SEC sources yet, so there is nothing to build.",
            file=sys.stderr,
        )
        return 2

    built: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for pair in pairs:
        try:
            record = build_pair(pair, manifest, layout, detect=not args.no_detect)
        except rfb.BenchmarkError as exc:
            failure = {
                "pair_id": pair["pair_id"],
                "status": BUILD_STATUS_FAILED,
                "failure_code": exc.code,
                "detail": exc.message,
            }
            failures.append(failure)
            _append_build_log(layout, {**failure, "at": rfb.utc_now_iso()})
            continue
        built.append(record)
        _append_build_log(
            layout,
            {
                "pair_id": record["pair_id"],
                "status": BUILD_STATUS_BUILT,
                "build_hash": record["build_hash"],
                "previous_outcome": record["previous"]["extraction_outcome"],
                "current_outcome": record["current"]["extraction_outcome"],
                "at": record["built_at"],
            },
        )

    report = {
        "benchmark_id": manifest["benchmark_id"],
        "manifest_status": manifest["status"],
        "pairs_requested": len(pairs),
        "pairs_built": len(built),
        "pairs_failed": len(failures),
        "extraction_outcomes": {
            outcome: sum(
                1
                for record in built
                for side in ("previous", "current")
                if record[side]["extraction_outcome"] == outcome
            )
            for outcome in rfb.EXTRACTION_OUTCOMES
        },
        "failures": failures,
        "pairs": [
            {
                "pair_id": record["pair_id"],
                "build_hash": record["build_hash"],
                "previous_extraction_outcome": record["previous"]["extraction_outcome"],
                "current_extraction_outcome": record["current"]["extraction_outcome"],
                "change_count": (record.get("execution") or {}).get("change_count", 0),
            }
            for record in built
        ],
    }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("Real-filing benchmark corpus build (controlled, offline)")
        print(
            f"  pairs requested={report['pairs_requested']} "
            f"built={report['pairs_built']} failed={report['pairs_failed']}"
        )
        for record in built:
            print(_summary_line(record))
        for failure in failures:
            print(f"  FAIL {failure['pair_id']}: [{failure['failure_code']}] "
                  f"{failure['detail']}")
        print(
            "\nExtracted section text stays in the local gitignored corpus "
            "directory. No filing content is written to a committed artifact."
        )

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
