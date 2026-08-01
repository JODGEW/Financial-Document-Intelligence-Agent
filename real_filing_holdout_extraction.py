"""Blind Item 1A extraction over the source-verified extraction holdout.

Stage 3.5, the step after source verification: run the FROZEN extraction
parser, unchanged, over the twenty holdout filing bodies whose digests the
committed manifest froze, record the outcome of every side exactly as it
falls, and advance the manifest exactly one step (``source_verified`` ->
``corpus_built``). This is the first time the parser observes these documents.

The one rule this module exists to enforce
------------------------------------------
The parser may not change in response to what this run observes. Before any
filing is read, every frozen code file is hashed; after extraction, comparison,
and packet generation finish, every hash is recomputed and must be identical.
A drifted parser source is refused BEFORE extraction; a drifted hash AFTER the
run fails the whole report. If extraction exposes a defect, the defect is
recorded and the blind result preserved — repairing the parser here would
convert the holdout into development data, exactly the failure mode the
holdout exists to rule out (a documented parser change requires freezing a
parser v3 AND selecting a NEW unseen holdout).

What this run may and may not claim
-----------------------------------
It may claim blind extraction coverage on this frozen corpus: exact extracted,
missing, ambiguous, and parse-failed counts, the buildable comparison-pair
count, and machine-proposed packet availability. It may NOT claim detector
accuracy, annotation accuracy, precision or recall of any kind, generalization
of detector quality, or Stage 3.5 completion: no human-verified label exists,
so ``extraction_holdout_evaluation`` and ``generalization_claim_supported``
remain false even at 20/20 coverage. Coverage is not correctness.

Everything downstream of the committed reports — filing bodies, extracted
section text, per-pair Chroma indexes, comparison databases, full detector
results, packets, machine-proposed annotations — stays in the gitignored
``benchmark_data/`` tree. Committed artifacts carry counts, codes, hashes, and
bounded heading labels only.

This module composes the EXISTING benchmark builder
(``scripts.build_real_filing_benchmark``) and the EXISTING packet generator;
it adds no second extraction implementation, no second detector call path, and
no second annotation writer. It imports nothing that can open a socket, which
``tests/`` asserts at the import graph.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import real_filing_benchmark as rfb
import real_filing_holdout as rfh

BLIND_EXTRACTION_REPORT_VERSION = "real-filing-holdout.blind-extraction.v1"
HOLDOUT_EXECUTION_REPORT_VERSION = "real-filing-holdout.execution-report.v1"
HOLDOUT_PACKET_INVENTORY_VERSION = "real-filing-holdout.packet-inventory.v1"
BLIND_RUNNER_VERSION = "real_filing_holdout_blind_extraction.v1"

# --- Frozen code files ----------------------------------------------------------
# The closed list of files whose bytes must be identical before and after the
# blind run: the extraction parser itself, the ingestion path that applies it,
# and the detector / validator / governance files the comparison workflow
# executes. Repo-relative on purpose — a committed report never carries a local
# absolute path.

FROZEN_CODE_FILES = (
    "loaders/sec_headings.py",
    "loaders/html.py",
    "ingest.py",
    "chroma_batching.py",
    "filing_registry.py",
    "comparison_detector.py",
    "comparison_validators.py",
    "comparison_store.py",
    "comparison_governance.py",
    "governance/comparison_schema.py",
    "policies/comparison_risk_policy.yaml",
)

# --- Stable failure codes -------------------------------------------------------

FAILURE_STATUS_NOT_EXTRACTABLE = "holdout_status_not_extractable"
FAILURE_PARSER_SOURCE_DRIFT = "holdout_parser_source_drift"
FAILURE_EXCLUSION_DRIFT = "holdout_exclusion_drift"
FAILURE_MANIFEST_HASH_DRIFT = "holdout_manifest_hash_drift"
FAILURE_SOURCE_MISSING = "holdout_source_missing"
FAILURE_SOURCE_CHECKSUM_DRIFT = "holdout_source_checksum_drift"
FAILURE_FROZEN_CODE_CHANGED = "holdout_frozen_code_changed_during_run"
FAILURE_SIDES_NOT_ALL_ATTEMPTED = "holdout_sides_not_all_attempted"

REASON_PAIR_BUILD_FAILED = "pair_build_failed"


class HoldoutExtractionError(rfb.BenchmarkError):
    """A bounded, code-carrying blind-extraction failure. Never carries filing
    content, credentials, local absolute paths, or exception text."""


def _repo_root(repo_root: str | Path | None = None) -> Path:
    return Path(repo_root or Path(__file__).resolve().parent)


def frozen_code_hashes(repo_root: str | Path | None = None) -> dict[str, str]:
    """sha256 of every frozen code file, keyed by repo-relative path."""
    root = _repo_root(repo_root)
    return {name: rfb.sha256_file(root / name) for name in FROZEN_CODE_FILES}


def require_frozen_code_unchanged(
    before: Mapping[str, str], after: Mapping[str, str]
) -> None:
    """Exact equality over the closed frozen-file list; names the drifted
    files, never their content."""
    drifted = sorted(
        name
        for name in FROZEN_CODE_FILES
        if before.get(name) != after.get(name)
    )
    if drifted:
        raise HoldoutExtractionError(
            FAILURE_FROZEN_CODE_CHANGED,
            "frozen code changed while the blind run was in progress: "
            f"{drifted}. The run's outputs cannot be attributed to the frozen "
            "parser and the report is refused.",
        )


# --- Preconditions ---------------------------------------------------------------


def verify_blind_run_preconditions(
    manifest: Mapping[str, Any],
    manifest_path: str | Path,
    *,
    repo_root: str | Path | None = None,
    development_manifest: Mapping[str, Any] | None = None,
) -> None:
    """Refuse the run unless every frozen identity still holds.

    ``validate_holdout_manifest`` pins the schema, pair ordering, strata, and
    development-corpus disjointness. This gate adds what only the working tree
    can attest: the manifest is at a status extraction may run over, the parser
    bytes on disk still hash to the frozen digest, the exclusion block still
    derives from the committed development manifest, and the manifest bytes
    still hash to the value the committed upstream report froze.
    """
    rfh.validate_holdout_manifest(manifest)

    if manifest["status"] not in (
        rfb.STATUS_SOURCE_VERIFIED,
        rfb.STATUS_CORPUS_BUILT,
    ):
        raise HoldoutExtractionError(
            FAILURE_STATUS_NOT_EXTRACTABLE,
            f"holdout manifest status {manifest['status']!r} does not permit "
            "extraction: the blind run requires verified source bytes "
            f"({rfb.STATUS_SOURCE_VERIFIED!r}), and only a rebuild may run at "
            f"{rfb.STATUS_CORPUS_BUILT!r}",
        )

    current_parser_hash = rfh.frozen_parser_source_sha256(repo_root)
    if current_parser_hash != manifest["frozen_parser_source_sha256"]:
        raise HoldoutExtractionError(
            FAILURE_PARSER_SOURCE_DRIFT,
            "the extraction parser source on disk no longer hashes to the "
            "digest frozen in the holdout manifest. The parser may not change "
            "after the freeze; a deliberate, documented parser change "
            "requires freezing a NEW holdout corpus.",
        )

    expected_exclusions = rfh.development_exclusions(development_manifest)
    frozen = manifest["development_exclusions"]
    if (
        frozen["development_benchmark_id"]
        != expected_exclusions["development_benchmark_id"]
        or sorted(frozen["excluded_ciks"])
        != sorted(expected_exclusions["excluded_ciks"])
        or sorted(frozen["excluded_accessions"])
        != sorted(expected_exclusions["excluded_accessions"])
    ):
        raise HoldoutExtractionError(
            FAILURE_EXCLUSION_DRIFT,
            "the manifest's frozen development-corpus exclusions no longer "
            "match those derived from the committed development manifest",
        )

    _verify_manifest_hash_chain(Path(manifest_path), manifest["status"])


def _verify_manifest_hash_chain(manifest_path: Path, status: str) -> None:
    """The manifest bytes must equal what the committed upstream report froze.

    At ``source_verified`` the anchor is the source-verification report's
    ``new_manifest_sha256``; at ``corpus_built`` it is the blind-extraction
    report's. A missing report beside the manifest (synthetic library/test
    manifests) skips the check — the committed tree is pinned by tests instead.
    """
    anchor_name = {
        rfb.STATUS_SOURCE_VERIFIED: "source_verification_report.json",
        rfb.STATUS_CORPUS_BUILT: "blind_extraction_report.json",
    }.get(status)
    if anchor_name is None:
        return
    report_path = manifest_path.parent / anchor_name
    if not report_path.exists():
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HoldoutExtractionError(
            "holdout_report_unreadable",
            f"{anchor_name} could not be read ({type(exc).__name__})",
        ) from None
    frozen = report.get("new_manifest_sha256")
    if frozen != rfb.sha256_file(manifest_path):
        raise HoldoutExtractionError(
            FAILURE_MANIFEST_HASH_DRIFT,
            "the holdout manifest no longer hashes to the value the committed "
            f"{anchor_name} froze. A frozen identity has drifted; the blind "
            "run is refused rather than executed over an edited freeze.",
        )


# --- Source verification ---------------------------------------------------------


def verify_holdout_sources(
    manifest: Mapping[str, Any], layout: rfb.CorpusLayout
) -> list[dict[str, Any]]:
    """Every local body must hash to the committed digest before the parser
    reads a single byte. A missing or drifted file refuses the whole run; no
    filing is ever re-downloaded, replaced, or skipped here."""
    verifications: list[dict[str, Any]] = []
    for pair in manifest["pairs"]:
        for side, payload in rfb.pair_sides(pair):
            target = layout.source_file(
                pair["pair_id"], side, payload["primary_document"]
            )
            if not target.exists():
                raise HoldoutExtractionError(
                    FAILURE_SOURCE_MISSING,
                    f"{pair['pair_id']}/{side}: the verified source body is "
                    "not present locally. Acquisition is a separate, "
                    "explicitly networked step; this run never downloads.",
                )
            observed = rfb.sha256_file(target)
            if observed != payload["expected_sha256"]:
                raise HoldoutExtractionError(
                    FAILURE_SOURCE_CHECKSUM_DRIFT,
                    f"{pair['pair_id']}/{side}: local source bytes no longer "
                    "hash to the digest the committed manifest froze. The "
                    "file is preserved and the run refused; nothing is "
                    "replaced.",
                )
            verifications.append(
                {
                    "pair_id": pair["pair_id"],
                    "side": side,
                    "source_sha256": observed,
                    "verified": True,
                }
            )
    return verifications


# --- One pair, blind ------------------------------------------------------------


def _failed_side(side: str, source_name: str, detail_code: str) -> dict[str, Any]:
    """A bounded parse_failed side record for a pair whose build raised."""
    return {
        "side": side,
        "filing_id": None,
        "source_name": source_name,
        "source_sha256": None,
        "parse_status": None,
        "extraction_outcome": rfb.EXTRACTION_PARSE_FAILED,
        "heading_detected": None,
        "section_hash": None,
        "section_char_count": 0,
        "section_paragraph_count": 0,
        "section_chunk_count": 0,
        "indexed_chunk_count": 0,
        "unit_count": 0,
        "units": [],
        "extraction_detail": detail_code,
        "extraction_parser_version": None,
        "extraction_reason": detail_code,
        "candidate_count": 0,
        "substantive_candidate_count": 0,
        "navigation_rejected_count": 0,
        "selected_element_tag": None,
        "boundary_heading": None,
    }


def blind_extract_pair(
    pair: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    layout: rfb.CorpusLayout,
    detect: bool = True,
) -> dict[str, Any]:
    """Run the existing ingestion, extraction, and (when both sides extracted)
    comparison path over one frozen pair, and persist a build record.

    Every step is the EXISTING implementation from the development-benchmark
    builder; nothing here re-implements extraction or detection. A failure is
    recorded as an outcome, never repaired: the pair stays, the parser stays,
    and the record says what happened.
    """
    # Imported here, not at module top, so importing this module never pulls
    # the ingestion/detector graph until a run actually starts.
    from scripts import build_real_filing_benchmark as builder
    import comparison_detector
    import comparison_store
    import ingest
    from loaders import html as loaders_html

    pair = dict(pair)
    pair_id = pair["pair_id"]
    durations: dict[str, int] = {}

    verified = builder.verify_sources(pair, layout)

    sides: dict[str, dict[str, Any]] = {}
    section_texts: dict[str, str] = {}
    try:
        started = time.monotonic()
        ingestion = builder._ingest_pair(pair, verified, layout)  # noqa: SLF001
        durations["ingest_ms"] = int((time.monotonic() - started) * 1000)
        entries = {
            entry["source_path"]: entry
            for entry in _registry_entries(ingestion["registry_path"])
        }
        for source_name, side in ingestion["side_by_source"].items():
            side_started = time.monotonic()
            extracted = builder._extract_side(  # noqa: SLF001
                side, source_name, ingestion, entries.get(source_name)
            )
            durations[side] = int((time.monotonic() - side_started) * 1000)
            section_texts[side] = extracted.pop("_section_text")
            sides[side] = extracted
    except Exception as exc:  # noqa: BLE001 - a blind outcome, never a repair
        detail = f"{REASON_PAIR_BUILD_FAILED}:{type(exc).__name__}"
        for side, payload in rfb.pair_sides(pair):
            if side not in sides:
                sides[side] = _failed_side(
                    side,
                    builder._workspace_source_name(pair, side, payload),  # noqa: SLF001
                    detail,
                )
                durations.setdefault(side, 0)
        ingestion = None
        section_texts = {}

    evaluable = rfb.build_is_evaluable({**sides, "pair_id": pair_id})
    if detect and evaluable and ingestion is not None:
        detect_started = time.monotonic()
        execution = builder._run_workflow(pair, ingestion, sides)  # noqa: SLF001
        durations["detect_ms"] = int((time.monotonic() - detect_started) * 1000)
    else:
        execution = {
            "executed": False,
            "skipped_reason": (
                "detection not requested"
                if not detect
                else "at least one side did not produce an extracted Item 1A "
                "section, so the comparison workflow was not run and no "
                "detection attempt exists for this pair"
            ),
        }
    result_payload = execution.pop("result", None)

    record = {
        "record_version": rfb.BUILD_RECORD_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "pair_id": pair_id,
        "issuer_name": pair["issuer_name"],
        "cik": pair["cik"],
        "sic": pair["sic"],
        "stratum_id": pair["stratum_id"],
        "stratum_label": pair["stratum_label"],
        # The shared packet generator reads ``sector_label``; the holdout's
        # stratification label IS its sector label, recorded under both names
        # rather than duplicated into a second packet implementation.
        "sector_label": pair["stratum_label"],
        "source_manifest_hash": manifest_sha256,
        "parser_versions": {
            "builder": BLIND_RUNNER_VERSION,
            "html_parser": loaders_html.HTML_PARSER_VERSION,
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

    for side, text in section_texts.items():
        if text:
            path = layout.section_text_path(pair_id, side)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if result_payload is not None:
        rfb.write_json_atomic(
            layout.build_dir(pair_id) / "detection_result.json", result_payload
        )
    rfb.write_json_atomic(layout.build_record_path(pair_id), record)
    return {"record": record, "durations": durations}


def _registry_entries(registry_path: Path) -> list[dict[str, Any]]:
    import filing_registry

    return filing_registry.list_entries(registry_path)


# --- The predeclared single execution --------------------------------------------


def run_blind_extraction(
    manifest: Mapping[str, Any],
    manifest_path: str | Path,
    layout: rfb.CorpusLayout,
    *,
    repo_root: str | Path | None = None,
    development_manifest: Mapping[str, Any] | None = None,
    now: Callable[[], str] = rfb.utc_now_iso,
) -> dict[str, Any]:
    """Verify everything, then run every pair once, in manifest order.

    One predeclared execution: no parser change, no pair reordering, no
    replacement, no rerun-under-different-code between sides. Returns the
    bounded run payload; the caller decides what to write where.
    """
    manifest_path = Path(manifest_path)
    verify_blind_run_preconditions(
        manifest,
        manifest_path,
        repo_root=repo_root,
        development_manifest=development_manifest,
    )
    hashes_before = frozen_code_hashes(repo_root)
    prior_manifest_sha256 = rfb.sha256_file(manifest_path)
    source_verifications = verify_holdout_sources(manifest, layout)

    outcomes: list[dict[str, Any]] = []
    for pair in manifest["pairs"]:
        outcomes.append(
            blind_extract_pair(
                pair,
                manifest=manifest,
                manifest_sha256=prior_manifest_sha256,
                layout=layout,
            )
        )

    hashes_after = frozen_code_hashes(repo_root)
    require_frozen_code_unchanged(hashes_before, hashes_after)

    records = [outcome["record"] for outcome in outcomes]
    attempted_sides = sum(
        1
        for record in records
        for side in ("previous", "current")
        if record[side]["extraction_outcome"] in rfb.EXTRACTION_OUTCOMES
    )
    expected_sides = rfh.TARGET_PAIR_COUNT * 2
    if attempted_sides != expected_sides:
        raise HoldoutExtractionError(
            FAILURE_SIDES_NOT_ALL_ATTEMPTED,
            f"{attempted_sides} of {expected_sides} sides carry a recorded "
            "outcome; the run is incomplete and no manifest transition or "
            "report may claim otherwise",
        )

    return {
        "records": records,
        "durations": [outcome["durations"] for outcome in outcomes],
        "source_verifications": source_verifications,
        "prior_manifest_sha256": prior_manifest_sha256,
        "prior_manifest_status": manifest["status"],
        "frozen_code_hashes_before": hashes_before,
        "frozen_code_hashes_after": hashes_after,
        "started_reported_at": now(),
    }


# --- Manifest transition ---------------------------------------------------------


def corpus_built_corpus_role_detail() -> str:
    """Role prose that is TRUE after the blind extraction run."""
    return (
        "Issuers and exact filing pairs were frozen from official SEC "
        "metadata only, after the extraction parser was frozen and before "
        "any selected filing body was observed. The twenty frozen bodies "
        "were acquired and checksum-verified, and the frozen parser has now "
        "run over them exactly once, blind and unchanged, with every "
        "extraction outcome recorded as it fell. No label has been human-"
        "verified: extraction COVERAGE on this corpus is not extraction "
        "CORRECTNESS, so no holdout evaluation exists yet and no "
        "generalization claim is supported. A selected pair is never "
        "replaced, and modifying the frozen parser in response to these "
        "results would convert this corpus into development data."
    )


CORPUS_BUILT_DESCRIPTION = (
    "Extraction holdout at corpus_built: exact issuers and filing pairs "
    "frozen from official SEC metadata AFTER sec_html_item_headings.v2 was "
    "frozen and BEFORE any selected filing body was observed; the twenty "
    "verified bodies have since been processed by the frozen parser exactly "
    "once, blind and unchanged, through the existing ingestion, extraction, "
    "and comparison path. Every outcome was recorded as observed. No human-"
    "verified label exists, so no accuracy, holdout-evaluation, or "
    "generalization result exists. A selected pair is never replaced because "
    "of later-observed filing-body structure, extraction outcome, detector "
    "output, or evaluation result."
)


def advance_holdout_manifest_to_corpus_built(
    manifest: Mapping[str, Any], records: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """The one forward step: ``source_verified`` -> ``corpus_built``.

    ``corpus_built`` asserts that the corpus build ran and produced a recorded
    outcome for every side — NOT that extraction succeeded. A missing,
    ambiguous, or parse-failed side is a preserved blind result, and the
    development ladder's own v1 report (0-of-20) is precedent that a fully
    failed build is still an honestly built corpus. Changes exactly three
    things — status, corpus_role_detail, description — and re-validates.
    """
    import copy

    rfh.validate_holdout_status_transition(
        manifest["status"], rfb.STATUS_CORPUS_BUILT
    )
    by_pair = {record["pair_id"]: record for record in records}
    for pair in manifest["pairs"]:
        record = by_pair.get(pair["pair_id"])
        if record is None:
            raise HoldoutExtractionError(
                FAILURE_SIDES_NOT_ALL_ATTEMPTED,
                f"no build record for frozen pair {pair['pair_id']}; the "
                "manifest does not advance past a pair the run skipped",
            )
        for side in ("previous", "current"):
            if record[side]["extraction_outcome"] not in rfb.EXTRACTION_OUTCOMES:
                raise HoldoutExtractionError(
                    FAILURE_SIDES_NOT_ALL_ATTEMPTED,
                    f"{pair['pair_id']}/{side} carries no recorded outcome",
                )

    advanced = copy.deepcopy(dict(manifest))
    advanced["status"] = rfb.STATUS_CORPUS_BUILT
    advanced["corpus_role_detail"] = corpus_built_corpus_role_detail()
    if "description" in advanced:
        advanced["description"] = CORPUS_BUILT_DESCRIPTION
    rfh.validate_holdout_manifest(advanced)
    return advanced


# --- Committed reports -----------------------------------------------------------

# Keys whose values legitimately differ between two runs of identical inputs.
# Everything else in a committed report must be byte-identical across runs,
# which is what makes "deterministic bounded outputs" checkable.
VOLATILE_REPORT_KEYS = frozenset(
    {
        "generated_at",
        "built_at",
        "duration_ms",
        "ingest_duration_ms",
        "detect_duration_ms",
        "total_duration_ms",
        "evaluated_at",
    }
)


def reproducible_report(value: Any) -> Any:
    """A report's stable projection: volatile keys removed recursively."""
    if isinstance(value, dict):
        return {
            key: reproducible_report(item)
            for key, item in sorted(value.items())
            if key not in VOLATILE_REPORT_KEYS
        }
    if isinstance(value, list):
        return [reproducible_report(item) for item in value]
    return value


def _side_report_row(
    pair_id: str,
    side: str,
    record_side: Mapping[str, Any],
    parser_source_sha256: str,
    duration_ms: int,
) -> dict[str, Any]:
    """One bounded per-side row. Counts, codes, hashes, and two bounded
    heading labels — never section text, never an excerpt, never a path."""
    heading = record_side.get("heading_detected")
    boundary = record_side.get("boundary_heading")
    return {
        "pair_id": pair_id,
        "side": side,
        "source_sha256": record_side.get("source_sha256"),
        "parser_version": record_side.get("extraction_parser_version"),
        "parser_source_sha256": parser_source_sha256,
        "outcome": record_side["extraction_outcome"],
        "reason_code": record_side.get("extraction_reason"),
        "candidate_count": record_side.get("candidate_count", 0),
        "substantive_candidate_count": record_side.get(
            "substantive_candidate_count", 0
        ),
        "selected_heading": (
            heading[: rfb.MAX_HEADING_CHARS] if heading else None
        ),
        "selected_tag": record_side.get("selected_element_tag"),
        "rejected_navigation_count": record_side.get(
            "navigation_rejected_count", 0
        ),
        "boundary_heading": (
            boundary[: rfb.MAX_HEADING_CHARS] if boundary else None
        ),
        "character_count": record_side.get("section_char_count", 0),
        "chunk_count": record_side.get("section_chunk_count", 0),
        "unit_count": record_side.get("unit_count", 0),
        "section_sha256": record_side.get("section_hash"),
        "duration_ms": duration_ms,
    }


def build_blind_extraction_report(
    *,
    manifest: Mapping[str, Any],
    run: Mapping[str, Any],
    new_manifest_status: str,
    new_manifest_sha256: str | None,
    generated_at: str,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """The committed record of the blind run: per-side outcomes, totals, the
    frozen-code hash attestation, and the manifest hash chain."""
    records = run["records"]
    durations = run["durations"]
    parser_hash = manifest["frozen_parser_source_sha256"]

    sides = []
    for record, pair_durations in zip(records, durations):
        for side in ("previous", "current"):
            sides.append(
                _side_report_row(
                    record["pair_id"],
                    side,
                    record[side],
                    parser_hash,
                    pair_durations.get(side, 0),
                )
            )

    totals = {
        outcome: sum(1 for row in sides if row["outcome"] == outcome)
        for outcome in rfb.EXTRACTION_OUTCOMES
    }
    buildable = [record for record in records if rfb.build_is_evaluable(record)]
    executed = [
        record
        for record in buildable
        if (record.get("execution") or {}).get("executed")
    ]

    role_fields = rfh.holdout_corpus_role_fields()
    role_fields["corpus_role_detail"] = corpus_built_corpus_role_detail()

    return {
        "report_version": BLIND_EXTRACTION_REPORT_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "benchmark_version": manifest["benchmark_version"],
        "generated_at": generated_at,
        "blind_run": {
            "predeclared_single_execution": True,
            "parser_modified_during_run": False,
            "pairs_replaced": 0,
            "sides_attempted": len(sides),
            "runner_version": BLIND_RUNNER_VERSION,
        },
        "prior_manifest_status": run["prior_manifest_status"],
        "new_manifest_status": new_manifest_status,
        "prior_manifest_sha256": run["prior_manifest_sha256"],
        "new_manifest_sha256": new_manifest_sha256,
        "frozen_extraction_parser_version": manifest[
            "frozen_extraction_parser_version"
        ],
        "frozen_parser_source_path": manifest["frozen_parser_source_path"],
        "frozen_parser_source_sha256": parser_hash,
        "selection_protocol_version": manifest["selection_protocol_version"],
        "selection_protocol_hash": manifest["selection_protocol_hash"],
        **role_fields,
        "frozen_code_hashes_before": dict(run["frozen_code_hashes_before"]),
        "frozen_code_hashes_after": dict(run["frozen_code_hashes_after"]),
        "frozen_code_unchanged": (
            run["frozen_code_hashes_before"] == run["frozen_code_hashes_after"]
        ),
        "source_checksums_verified": len(run["source_verifications"]),
        "sides": sides,
        "extraction_totals": totals,
        "pairs_built": len(records),
        "pairs_fully_extracted": len(buildable),
        "comparison_runs": len(executed),
        "human_verified_labels": 0,
        "gold_metrics_available": False,
        "gold_metrics": None,
        "commit_sha": rfb.repo_commit_sha(repo_root),
        "notes": [
            (
                "BLIND RESULT. This is the first time sec_html_item_headings.v2 "
                "observed these twenty filings: the corpus was frozen from "
                "metadata after the parser was frozen, bodies were acquired "
                "and checksum-verified without extraction, and this run "
                "executed the parser unchanged over all twenty sides in one "
                "predeclared pass."
            ),
            (
                "This report claims blind extraction COVERAGE only: exact "
                "extracted, missing, ambiguous, and parse-failed counts, the "
                "buildable comparison-pair count, and machine-proposed packet "
                "availability. Coverage is not correctness."
            ),
            (
                "No detector-accuracy, annotation-accuracy, or "
                "filing-change-quality number exists or may be derived from "
                "this report: zero labels are human-verified, so "
                "generalization_claim_supported remains false even at full "
                "coverage. A human-verified evaluation over this corpus, with "
                "the parser unchanged, is the required next step."
            ),
            (
                "Every extraction outcome is preserved exactly as observed. A "
                "defect this run exposes is recorded, not repaired: modifying "
                "the frozen parser in response to these results would convert "
                "this holdout into development data, require freezing a "
                "parser v3, and require selecting a NEW unseen holdout."
            ),
            (
                "Filing bodies, extracted section text, per-pair indexes, "
                "comparison databases, full detector results, packets, and "
                "machine-proposed annotations stay in the gitignored "
                "benchmark_data/ tree. This report carries counts, stable "
                "codes, hashes, and bounded heading labels only."
            ),
        ],
        "regenerated_by": (
            "python scripts/run_real_filing_holdout_blind_extraction.py "
            "(offline; requires locally verified holdout sources)"
        ),
    }


def build_holdout_execution_report(
    *,
    manifest: Mapping[str, Any],
    run: Mapping[str, Any],
    manifest_sha256: str | None,
    generated_at: str,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Unlabeled execution report: what the workflow DID on the holdout pairs.

    Deliberately mirrors the development benchmark's --unlabeled mode: bounded
    execution mechanics, no accuracy metric of any kind, and its own payload
    says so.
    """
    import comparison_detector
    import comparison_store

    records = run["records"]
    durations = run["durations"]

    executions = []
    for record, pair_durations in zip(records, durations):
        execution = record.get("execution") or {}
        buildable = rfb.build_is_evaluable(record)
        attempts = execution.get("attempts") or []
        undetermined = (execution.get("changes_by_type") or {}).get(
            "undetermined", 0
        )
        executions.append(
            {
                "pair_id": record["pair_id"],
                "buildable": buildable,
                "executed": bool(execution.get("executed")),
                "execution_status": execution.get("lifecycle"),
                "skipped_reason": execution.get("skipped_reason"),
                "failure_code": execution.get("failure_code"),
                "result_hash": execution.get("result_hash"),
                "change_count": execution.get("change_count", 0),
                "changes_by_type": dict(execution.get("changes_by_type") or {}),
                "undetermined_count": undetermined,
                "undetermined_reason_codes": dict(
                    execution.get("undetermined_reason_codes") or {}
                ),
                "evidence_reference_count": execution.get("evidence_total", 0),
                "evidence_unresolved": execution.get("evidence_unresolved", 0),
                "evidence_foreign": execution.get("evidence_foreign", 0),
                "duration_ms": pair_durations.get("detect_ms", 0),
                "attempt_count": len(attempts),
                # Structural zeroes for the direct synchronous path: no durable
                # job is created, so no retry, reclaim, or lease can exist.
                "retries": max(0, len(attempts) - 1),
                "reclaims": 0,
                "detection_jobs": 0,
            }
        )

    role_fields = rfh.holdout_corpus_role_fields()
    role_fields["corpus_role_detail"] = corpus_built_corpus_role_detail()

    return {
        "report_version": HOLDOUT_EXECUTION_REPORT_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "benchmark_version": manifest["benchmark_version"],
        "mode": "unlabeled_execution_report",
        "generated_at": generated_at,
        "manifest_status": rfb.STATUS_CORPUS_BUILT,
        "manifest_sha256": manifest_sha256,
        "build_source_manifest_hash": run["prior_manifest_sha256"],
        "detector_version": comparison_detector.DETECTOR_VERSION,
        "workflow_version": comparison_store.WORKFLOW_VERSION,
        **role_fields,
        "warnings": [
            "NO GOLD ACCURACY METRICS ARE AVAILABLE IN THIS REPORT.",
            (
                "Machine-proposed annotations are NOT verified labels and are "
                "not used here."
            ),
            (
                "This report describes execution mechanics only and CANNOT "
                "SUPPORT ANY QUALITY OR ACCURACY CLAIM about the workflow on "
                "real filings."
            ),
        ],
        "corpus_quality": {
            "pairs_requested": len(manifest["pairs"]),
            "pairs_built": len(records),
            "pairs_extracted": sum(
                1 for record in records if rfb.build_is_evaluable(record)
            ),
            "pairs_missing_section": _pairs_with_outcome(
                records, rfb.EXTRACTION_MISSING
            ),
            "pairs_ambiguous_section": _pairs_with_outcome(
                records, rfb.EXTRACTION_AMBIGUOUS
            ),
            "pairs_parse_failed": _pairs_with_outcome(
                records, rfb.EXTRACTION_PARSE_FAILED
            ),
            "pairs_human_verified": 0,
            "filing_extraction_outcomes": {
                outcome: sum(
                    1
                    for record in records
                    for side in ("previous", "current")
                    if record[side]["extraction_outcome"] == outcome
                )
                for outcome in rfb.EXTRACTION_OUTCOMES
            },
        },
        "executions": executions,
        "comparisons_executed": sum(1 for entry in executions if entry["executed"]),
        "human_verified_labels": 0,
        "gold_metrics_available": False,
        "gold_metrics": None,
        "commit_sha": rfb.repo_commit_sha(repo_root),
        "regenerated_by": (
            "python scripts/run_real_filing_holdout_blind_extraction.py "
            "(offline; requires locally verified holdout sources)"
        ),
    }


def _pairs_with_outcome(records: list[Mapping[str, Any]], outcome: str) -> int:
    return sum(
        1
        for record in records
        if any(
            record[side]["extraction_outcome"] == outcome
            for side in ("previous", "current")
        )
    )


def build_holdout_packet_inventory(
    *,
    manifest: Mapping[str, Any],
    packet_results: list[Mapping[str, Any]],
    manifest_sha256: str | None,
    build_source_manifest_hash: str,
    generated_at: str,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Bounded committed inventory of the LOCAL machine-proposed packets.

    Identity, counts, hashes, and readiness only. Packet bodies, excerpts, and
    annotations never appear here; every annotation is machine_proposed and
    zero labels are human-verified, which this payload states structurally.
    """
    role_fields = rfh.holdout_corpus_role_fields()
    role_fields["corpus_role_detail"] = corpus_built_corpus_role_detail()
    pairs = sorted(
        (dict(entry) for entry in packet_results),
        key=lambda entry: entry["pair_id"],
    )
    return {
        "report_version": HOLDOUT_PACKET_INVENTORY_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "benchmark_version": manifest["benchmark_version"],
        "generated_at": generated_at,
        "manifest_sha256": manifest_sha256,
        "build_source_manifest_hash": build_source_manifest_hash,
        "annotation_schema_version": rfb.ANNOTATION_SCHEMA_VERSION,
        "annotation_protocol_version": rfb.ANNOTATION_PROTOCOL_VERSION,
        "packet_schema_version": rfb.PACKET_SCHEMA_VERSION,
        **role_fields,
        "packets_written": sum(
            1 for entry in pairs if entry["packet_status"] == "written"
        ),
        "packets_blocked": sum(
            1 for entry in pairs if entry["packet_status"] == "blocked"
        ),
        "pairs": pairs,
        "human_verified_labels": 0,
        "commit_sha": rfb.repo_commit_sha(repo_root),
        "notes": [
            (
                "Every annotation is machine_proposed. A machine-proposed "
                "label is NOT ground truth and can never enter a gold metric; "
                "human_verified requires an annotator id and verification "
                "timestamp that no tool in this repository sets."
            ),
            (
                "Packets and machine-proposed annotations stay in the "
                "gitignored benchmark_data/ tree; only this bounded inventory "
                "is committed."
            ),
            (
                "A blocked pair stays in the corpus and is reported with its "
                "blocking reason. It is never replaced, and its extraction "
                "outcome is never revisited under changed code."
            ),
            (
                "Human review of these packets is the remaining Stage 3.5 "
                "work: until labels are human-verified, no accuracy number "
                "exists and generalization_claim_supported remains false."
            ),
        ],
        "regenerated_by": (
            "python scripts/run_real_filing_holdout_blind_extraction.py "
            "(offline; requires locally verified holdout sources)"
        ),
    }
