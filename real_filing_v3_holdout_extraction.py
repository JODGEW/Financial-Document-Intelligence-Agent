"""Blind Item 1A extraction over the source-verified v3 extraction holdout.

Stage 3.5, the step after v3 source verification: run the FROZEN pipeline —
``sec_html_item_headings.v2`` section extraction, the ``item1a_units.v3``
unit grammar, ``item1a_detector.v3``, ``comparison_workflow.v3`` — unchanged,
over the twenty holdout filing bodies whose digests the committed manifest
froze; record the outcome of every side exactly as it falls; and advance the
manifest exactly one step (``source_verified`` -> ``corpus_built``). This is
the first time any of those frozen components observes these documents.

The one rule this module exists to enforce
------------------------------------------
No semantic component may change in response to what this run observes. Before
any filing is read, every frozen code file is hashed; after extraction,
comparison, and packet generation finish, every hash is recomputed and must be
identical. A drifted parser, detector, workflow, or evaluator source is refused
BEFORE extraction; a drifted hash AFTER the run fails the whole report. If the
run exposes a defect, the defect is recorded and the blind result preserved —
repairing anything here would convert the holdout into development data,
exactly the failure mode the holdout exists to rule out (a documented semantic
change requires freezing a new parser/detector/evaluator version AND selecting
a NEW unseen holdout).

What this run may and may not claim
-----------------------------------
It may claim blind execution coverage on this frozen corpus: exact extracted,
missing, ambiguous, and parse-failed counts, the buildable comparison-pair
count, the blocked-pair count with stable reasons, and machine-proposed packet
availability. It may NOT claim detector accuracy, annotation accuracy,
precision, recall, exact-match accuracy, unchanged-FPR, generalization of any
kind, or Stage 3.5 completion: no human-verified label exists, so
``extraction_holdout_evaluation`` and ``generalization_claim_supported`` remain
false at any coverage. Coverage is not correctness, and a buildable-pair rate
is not detector quality.

Everything downstream of the committed reports — filing bodies, extracted
section text, per-pair Chroma indexes, comparison databases, full detector
results, packets, machine-proposed annotations — stays in the gitignored
``benchmark_data/`` tree. Committed artifacts carry counts, stable codes,
hashes, canonical unit identities, corpus-relative artifact paths, and bounded
heading labels only.

This module composes the EXISTING benchmark builder
(``scripts.build_real_filing_benchmark``) and the EXISTING packet generator;
it adds no second extraction implementation, no second unit grammar, no second
detector call path, and no second annotation writer. It imports nothing that
can open a socket, which ``tests/`` asserts at the import graph, and it never
imports the gold evaluator.
"""

from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import real_filing_benchmark as rfb
import real_filing_v3_holdout as rfv3

# --- Protocol and report identities ---------------------------------------------

#: The narrow run protocol this module implements. Bumping it means a different
#: run identity, not a different pipeline: nothing here may bump the parser,
#: unit grammar, detector, workflow, evaluator, or annotation versions.
V3_BLIND_RUN_PROTOCOL_VERSION = "real-filing-v3-blind-extraction.v1"

V3_BLIND_EXTRACTION_REPORT_VERSION = "real-filing-v3-holdout.blind-extraction.v1"
V3_EXECUTION_REPORT_VERSION = "real-filing-v3-holdout.execution-report.v1"
V3_PACKET_INVENTORY_VERSION = "real-filing-v3-holdout.packet-inventory.v1"
V3_BLIND_RUNNER_VERSION = "real_filing_v3_holdout_blind_extraction.v1"

#: Redeclared literal rather than imported: the acquisition module that owns it
#: can open a socket, and this module's contract is that its import graph
#: cannot. A test pins the two to the same string.
LOCAL_PATH_CONVENTION = "sources/{pair_id}/{side}/{primary_document}"

#: Frozen identities redeclared as literals for the same reason the v3 holdout
#: module redeclares its own: importing ``governance.comparison_schema``,
#: ``ingest``, or the packet generator at module scope would pull the storage,
#: extraction, and config graphs into a module whose contract is "importing it
#: reaches nothing". Tests pin every literal to the live constant it freezes,
#: and ``verify_contract_bindings`` checks them against the live modules at
#: run time, before any body is read.
FROZEN_COMPARISON_SCHEMA_VERSION = "comparison.v1"
FROZEN_SECTION_KEY = "item_1a_risk_factors"
FROZEN_PACKET_GENERATOR_VERSION = "real_filing_annotation_packets.v1"

#: Where local build artifacts land, corpus-relative. Same reason.
BUILD_PATH_CONVENTION = "build/{pair_id}/build.json"
PACKET_PATH_CONVENTION = "packets/{pair_id}/packet.json"

#: The one deterministic execution order. Not issuer name, not heading, not
#: extraction success, not unit count, not result quality, not label type.
EXECUTION_ORDER = "manifest pair order, then previous side, then current side"

#: The gitignored tree every local body and every local build artifact must sit
#: under. A corpus root without this segment is refused before a byte is read.
UNTRACKED_CORPUS_SEGMENT = "benchmark_data"

# --- Frozen code files ----------------------------------------------------------
# The closed list of files whose bytes must be identical before and after the
# blind run: the extraction parser, the ingestion path that applies it, the
# unit grammar / detector / validators / store / governance the comparison
# workflow executes, and — because the v3 manifest pins them too — the
# contract-v2 gold evaluator and the packet generator. Repo-relative on
# purpose: a committed report never carries a local absolute path.

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
    "scripts/eval_real_filing_benchmark.py",
    "scripts/create_real_filing_annotation_packets.py",
)

# --- Stable infrastructure failure codes ----------------------------------------
# These abort BEFORE any body is processed (or fail the whole run) and are
# categorically distinct from a frozen-pipeline semantic outcome such as
# ``missing`` or ``ambiguous``, which is a recorded result of a completed run.
# Messages carry pair ids, sides, bounded codes, and counts only — never filing
# text, section excerpts, packet prose, absolute paths, environment values,
# credentials, or raw exception text.

FAILURE_MANIFEST_STATUS_INVALID = "blind_manifest_status_invalid"
FAILURE_MANIFEST_BINDING_MISMATCH = "blind_manifest_binding_mismatch"
FAILURE_CONTRACT_VERSION_MISMATCH = "blind_contract_version_mismatch"
FAILURE_SOURCE_MISSING = "blind_source_missing"
FAILURE_SOURCE_SHA256_MISMATCH = "blind_source_sha256_mismatch"
FAILURE_SOURCE_PATH_INVALID = "blind_source_path_invalid"
FAILURE_DUPLICATE_SOURCE_IDENTITY = "blind_duplicate_source_identity"
FAILURE_DUPLICATE_SOURCE_PATH = "blind_duplicate_source_path"
FAILURE_TRACKED_SOURCE_PATH = "blind_tracked_source_path"
FAILURE_HUMAN_ANNOTATION_PRESENT = "blind_human_annotation_present"
FAILURE_OUTPUT_IDENTITY_CONFLICT = "blind_output_identity_conflict"
FAILURE_EXISTING_ARTIFACT_MISMATCH = "blind_existing_artifact_mismatch"
FAILURE_PACKET_INVENTORY_MISMATCH = "blind_packet_inventory_mismatch"
FAILURE_RUN_INCOMPLETE = "blind_run_incomplete"
FAILURE_FROZEN_CODE_CHANGED = "blind_frozen_code_changed_during_run"
FAILURE_UNIT_IDENTITY_INVALID = "blind_unit_identity_invalid"
FAILURE_DUPLICATE_UNIT_IDENTITY = "blind_duplicate_unit_identity"
FAILURE_REPORT_UNREADABLE = "blind_report_unreadable"

#: Codes that mean "refused before the pipeline ran", used by the CLI to pick
#: an exit code. Everything else is a run-level failure.
PREFLIGHT_FAILURE_CODES = (
    FAILURE_MANIFEST_STATUS_INVALID,
    FAILURE_MANIFEST_BINDING_MISMATCH,
    FAILURE_CONTRACT_VERSION_MISMATCH,
    FAILURE_SOURCE_MISSING,
    FAILURE_SOURCE_SHA256_MISMATCH,
    FAILURE_SOURCE_PATH_INVALID,
    FAILURE_DUPLICATE_SOURCE_IDENTITY,
    FAILURE_DUPLICATE_SOURCE_PATH,
    FAILURE_TRACKED_SOURCE_PATH,
    FAILURE_HUMAN_ANNOTATION_PRESENT,
    FAILURE_OUTPUT_IDENTITY_CONFLICT,
    FAILURE_REPORT_UNREADABLE,
)

# --- Stable blocked reasons ------------------------------------------------------
# Pair-level blocking is a SEMANTIC outcome of the frozen pipeline, never an
# infrastructure error. These names are new (no historical semantic code is
# renamed) and describe only which side failed to yield an extractable section.

BLOCKED_PREVIOUS_NOT_EXTRACTED = "previous_side_not_extracted"
BLOCKED_CURRENT_NOT_EXTRACTED = "current_side_not_extracted"
BLOCKED_BOTH_SIDES_NOT_EXTRACTED = "both_sides_not_extracted"
BLOCKED_COMPARISON_NOT_DETECTED = "comparison_not_detected"

BLOCKED_PAIR_REASONS = (
    BLOCKED_PREVIOUS_NOT_EXTRACTED,
    BLOCKED_CURRENT_NOT_EXTRACTED,
    BLOCKED_BOTH_SIDES_NOT_EXTRACTED,
    BLOCKED_COMPARISON_NOT_DETECTED,
)

#: Packet-level blocking reuses the frozen packet protocol's existing strings.
PACKET_BLOCKED_NOT_EXTRACTED = "item_1a_not_extracted_for_both_sides"
PACKET_BLOCKED_NOT_DETECTED = "comparison_not_detected"

REASON_PAIR_BUILD_FAILED = "pair_build_failed"


class V3BlindExtractionError(rfb.BenchmarkError):
    """A bounded, code-carrying blind-run failure. Never carries filing
    content, section excerpts, packet prose, credentials, local absolute
    paths, environment values, or exception text."""


def _repo_root(repo_root: str | Path | None = None) -> Path:
    return Path(repo_root or Path(__file__).resolve().parent)


# --- The predeclared run protocol -------------------------------------------------


def blind_run_protocol() -> dict[str, Any]:
    """The predeclared, bounded blind-run protocol.

    Hashed by :func:`blind_run_protocol_hash` and recorded in the committed
    reports, so the rules an outcome was produced under are frozen beside the
    outcome itself. Contains no credential, no local path, and no value that
    varies between runs.
    """
    return {
        "protocol_version": V3_BLIND_RUN_PROTOCOL_VERSION,
        "benchmark_id": rfv3.V3_HOLDOUT_BENCHMARK_ID,
        "benchmark_version": rfv3.V3_HOLDOUT_BENCHMARK_VERSION,
        "accepted_manifest_status": rfb.STATUS_SOURCE_VERIFIED,
        "resulting_manifest_status": rfb.STATUS_CORPUS_BUILT,
        "extraction_parser_version": rfv3.FROZEN_EXTRACTION_PARSER_VERSION,
        "parser_source_path": rfv3.FROZEN_PARSER_SOURCE_PATH,
        "unit_grammar_version": rfv3.FROZEN_UNIT_GRAMMAR_VERSION,
        "detector_version": rfv3.FROZEN_DETECTOR_VERSION,
        "detector_source_path": rfv3.FROZEN_DETECTOR_SOURCE_PATH,
        "workflow_version": rfv3.FROZEN_WORKFLOW_VERSION,
        "workflow_source_path": rfv3.FROZEN_WORKFLOW_SOURCE_PATH,
        "evaluation_contract_version": rfv3.FROZEN_EVALUATION_CONTRACT_VERSION,
        "evaluator_source_path": rfv3.FROZEN_EVALUATOR_SOURCE_PATH,
        "comparison_schema_version": FROZEN_COMPARISON_SCHEMA_VERSION,
        "section_key": FROZEN_SECTION_KEY,
        "packet_generator_version": FROZEN_PACKET_GENERATOR_VERSION,
        "annotation_schema_version": rfv3.FROZEN_ANNOTATION_SCHEMA_VERSION,
        "annotation_protocol_version": rfv3.FROZEN_ANNOTATION_PROTOCOL_VERSION,
        "packet_schema_version": rfb.PACKET_SCHEMA_VERSION,
        "unit_identity_contract": rfv3.FROZEN_UNIT_IDENTITY_CONTRACT,
        "subject_matching": rfv3.FROZEN_SUBJECT_MATCHING,
        "frozen_code_files": list(FROZEN_CODE_FILES),
        "local_source_path_convention": LOCAL_PATH_CONVENTION,
        "local_build_path_convention": BUILD_PATH_CONVENTION,
        "local_packet_path_convention": PACKET_PATH_CONVENTION,
        "untracked_corpus_segment": UNTRACKED_CORPUS_SEGMENT,
        "execution_order": EXECUTION_ORDER,
        "blocked_side_semantics": (
            "a side is blocked when the frozen extractor does not return an "
            f"{rfb.EXTRACTION_EXTRACTED!r} Item 1A section; the recorded "
            f"outcome is one of {list(rfb.EXTRACTION_OUTCOMES)} and is "
            "preserved, never repaired, replaced, or reclassified"
        ),
        "blocked_pair_semantics": (
            "a pair is blocked when either side is blocked, or when the "
            "frozen comparison workflow did not produce a stored result; the "
            f"reason is one of {list(BLOCKED_PAIR_REASONS)}; the pair stays "
            "in the corpus and in every report and is never replaced"
        ),
        "output_hash_semantics": (
            "sha256 over canonical JSON of the reproducible projection: "
            "wall-clock stamps, durations, database-generated attempt ids, "
            "and repository commit metadata are excluded, so identical source "
            "bytes under identical frozen code yield an identical hash"
        ),
        "hash_algorithm": "sha256",
        "runs_network_requests": False,
        "acquires_sources": False,
        "runs_gold_evaluation": False,
        "creates_human_labels": False,
        "creates_metrics": False,
        "signs_off_generalization": False,
    }


def blind_run_protocol_hash() -> str:
    return rfb.payload_hash(blind_run_protocol())


# --- Frozen code ------------------------------------------------------------------


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
        name for name in FROZEN_CODE_FILES if before.get(name) != after.get(name)
    )
    if drifted:
        raise V3BlindExtractionError(
            FAILURE_FROZEN_CODE_CHANGED,
            "frozen code changed while the blind run was in progress: "
            f"{drifted}. The run's outputs cannot be attributed to the frozen "
            "v3 pipeline and the report is refused.",
        )


# --- Preconditions ----------------------------------------------------------------


def verify_contract_bindings(manifest: Mapping[str, Any]) -> dict[str, str]:
    """The live modules must still be the versions the manifest froze.

    ``item1a_units.v3`` is selected EXPLICITLY here rather than inherited: the
    frozen builder calls ``extract_units`` with the module default, so this
    gate is what makes "the v3 grammar ran, not the v2 grammar" a checked fact
    instead of an assumption. A version that has moved refuses the run.
    """
    import comparison_detector
    import comparison_store
    import ingest
    from governance import comparison_schema

    observed = {
        "detector_version": comparison_detector.DETECTOR_VERSION,
        "unit_grammar_version": comparison_detector.DEFAULT_UNIT_GRAMMAR,
        "workflow_version": comparison_store.WORKFLOW_VERSION,
        "comparison_schema_version": comparison_schema.COMPARISON_SCHEMA_VERSION,
        "section_key": ingest.SECTION_KEY_ITEM_1A,
    }
    expected = {
        "detector_version": manifest["frozen_detector_version"],
        "unit_grammar_version": manifest["frozen_unit_grammar_version"],
        "workflow_version": manifest["frozen_workflow_version"],
        "comparison_schema_version": FROZEN_COMPARISON_SCHEMA_VERSION,
        "section_key": FROZEN_SECTION_KEY,
    }
    mismatched = sorted(
        field for field, value in expected.items() if observed[field] != value
    )
    if mismatched:
        raise V3BlindExtractionError(
            FAILURE_CONTRACT_VERSION_MISMATCH,
            "the live pipeline no longer matches the contract the v3 holdout "
            f"manifest froze: {mismatched}. The blind run is refused rather "
            "than executed under a version this holdout does not describe.",
        )
    if (
        comparison_detector.DEFAULT_UNIT_GRAMMAR
        != comparison_detector.UNIT_GRAMMAR_V3
    ):
        raise V3BlindExtractionError(
            FAILURE_CONTRACT_VERSION_MISMATCH,
            "the default unit grammar is not the frozen v3 grammar; the blind "
            "run refuses rather than silently unitizing under another grammar",
        )
    return observed


def verify_source_verification_binding(
    manifest: Mapping[str, Any], manifest_path: str | Path
) -> dict[str, Any] | None:
    """Cross-bind the committed source-verification report to this manifest.

    Returns the report (or None when no report sits beside the manifest, as
    for synthetic library/test manifests). The chained value is the digest of
    the SOURCE-VERIFIED manifest bytes: it is the stable anchor for this run's
    identity, unchanged by the transition this run performs.
    """
    report_path = Path(manifest_path).parent / "source_verification_report.json"
    if not report_path.exists():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise V3BlindExtractionError(
            FAILURE_REPORT_UNREADABLE,
            "the committed source-verification report could not be read "
            f"({type(exc).__name__})",
        ) from None
    mismatched: list[str] = []
    if report.get("benchmark_id") != manifest["benchmark_id"]:
        mismatched.append("benchmark_id")
    if report.get("new_manifest_status") != rfb.STATUS_SOURCE_VERIFIED:
        mismatched.append("new_manifest_status")
    if report.get("verification_outcome") != rfb.STATUS_SOURCE_VERIFIED:
        mismatched.append("verification_outcome")
    if report.get("selection_protocol_hash") != manifest["selection_protocol_hash"]:
        mismatched.append("selection_protocol_hash")
    for field in (
        "frozen_parser_source_sha256",
        "frozen_detector_source_sha256",
        "frozen_workflow_source_sha256",
        "frozen_evaluator_source_sha256",
        "frozen_extraction_parser_version",
        "frozen_unit_grammar_version",
        "frozen_detector_version",
        "frozen_workflow_version",
        "frozen_evaluation_contract_version",
    ):
        if report.get(field) != manifest[field]:
            mismatched.append(field)
    if report.get("side_count") != 2 * len(manifest["pairs"]):
        mismatched.append("side_count")
    if report.get("source_checksums_verified") != 2 * len(manifest["pairs"]):
        mismatched.append("source_checksums_verified")
    if mismatched:
        raise V3BlindExtractionError(
            FAILURE_MANIFEST_BINDING_MISMATCH,
            "the committed source-verification report does not bind this "
            f"manifest: {sorted(set(mismatched))}",
        )
    return report


def verify_manifest_hash_chain(manifest_path: str | Path, status: str) -> None:
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
    manifest_path = Path(manifest_path)
    report_path = manifest_path.parent / anchor_name
    if not report_path.exists():
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise V3BlindExtractionError(
            FAILURE_REPORT_UNREADABLE,
            f"{anchor_name} could not be read ({type(exc).__name__})",
        ) from None
    if report.get("new_manifest_sha256") != rfb.sha256_file(manifest_path):
        raise V3BlindExtractionError(
            FAILURE_MANIFEST_BINDING_MISMATCH,
            "the v3 holdout manifest no longer hashes to the value the "
            f"committed {anchor_name} froze. A frozen identity has drifted; "
            "the blind run is refused rather than executed over an edited "
            "freeze.",
        )


def verify_no_human_annotation(layout: rfb.CorpusLayout) -> int:
    """No v3 human annotation may exist as an input to the blind run.

    A machine-proposed file from an earlier run of this same command is fine
    (it is this pipeline's own output). Anything that claims a status beyond
    ``machine_proposed`` refuses the run: a human decision is never an
    execution input, and a blind run must not be able to read one.
    """
    annotations = layout.annotations_dir()
    if not annotations.exists():
        return 0
    inspected = 0
    for path in sorted(annotations.glob("*.json")):
        inspected += 1
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise V3BlindExtractionError(
                FAILURE_HUMAN_ANNOTATION_PRESENT,
                f"a local annotation file could not be read as JSON "
                f"({type(exc).__name__}); the blind run refuses rather than "
                "running beside an annotation of unknown status",
            ) from None
        status = document.get("annotation_status")
        if status != rfb.ANNOTATION_MACHINE_PROPOSED:
            raise V3BlindExtractionError(
                FAILURE_HUMAN_ANNOTATION_PRESENT,
                f"a local annotation carries status {status!r}; a human "
                "decision is never an input to the blind run, and no label of "
                "any status may be read, copied, adapted, or used as an "
                "oracle",
            )
    return inspected


def _require_untracked_corpus(layout: rfb.CorpusLayout) -> None:
    """The corpus root must sit under the gitignored ``benchmark_data`` tree.

    A filing body under a tracked directory would be committed by the next
    ``git add``; refusing here is cheaper than discovering it in review.
    """
    root = Path(os.path.normpath(Path(layout.root).absolute()))
    if UNTRACKED_CORPUS_SEGMENT not in root.parts:
        raise V3BlindExtractionError(
            FAILURE_TRACKED_SOURCE_PATH,
            "the local corpus root is not under the gitignored "
            f"{UNTRACKED_CORPUS_SEGMENT!r} tree; filing bodies, extracted "
            "sections, packets, and databases may never sit in a tracked "
            "directory",
        )


def resolve_source_paths(
    manifest: Mapping[str, Any], layout: rfb.CorpusLayout
) -> list[dict[str, Any]]:
    """Deterministically resolve, and structurally check, all twenty paths.

    Runs in canonical execution order. Refuses traversal, a path that escapes
    its own side directory, a duplicate local path, and a duplicate source
    identity — before any file is opened.
    """
    _require_untracked_corpus(layout)
    root = Path(os.path.normpath(Path(layout.root).absolute()))
    resolved: list[dict[str, Any]] = []
    seen_paths: dict[Path, str] = {}
    seen_identities: dict[tuple[str, str, str], str] = {}
    for pair in manifest["pairs"]:
        pair_id = pair["pair_id"]
        for side, payload in rfb.pair_sides(pair):
            where = f"{pair_id}/{side}"
            document_name = payload["primary_document"]
            if (
                not document_name
                or document_name in (".", "..")
                or "/" in document_name
                or "\\" in document_name
                or document_name.startswith("~")
            ):
                raise V3BlindExtractionError(
                    FAILURE_SOURCE_PATH_INVALID,
                    f"{where}: primary_document is not a plain file name",
                )
            target = layout.source_file(pair_id, side, document_name)
            # ``os.path.normpath`` semantics without touching the filesystem:
            # a traversal component would collapse the path out of its own
            # side directory, and that is exactly what must be refused. The
            # expected location is derived from frozen fields only.
            candidate = Path(os.path.normpath(target.absolute()))
            expected = Path(
                os.path.normpath(
                    layout.source_dir(pair_id, side).absolute() / document_name
                )
            )
            if candidate != expected or candidate.parent.name != side:
                raise V3BlindExtractionError(
                    FAILURE_SOURCE_PATH_INVALID,
                    f"{where}: the resolved source path does not sit in this "
                    "side's own directory",
                )
            try:
                candidate.relative_to(root)
            except ValueError:
                raise V3BlindExtractionError(
                    FAILURE_SOURCE_PATH_INVALID,
                    f"{where}: the resolved source path escapes the corpus "
                    "root",
                ) from None
            if candidate in seen_paths:
                raise V3BlindExtractionError(
                    FAILURE_DUPLICATE_SOURCE_PATH,
                    f"{where}: resolves to the same local file as "
                    f"{seen_paths[candidate]}",
                )
            seen_paths[candidate] = where

            identity = (
                pair["cik"],
                payload["accession_number"],
                document_name,
            )
            if identity in seen_identities:
                raise V3BlindExtractionError(
                    FAILURE_DUPLICATE_SOURCE_IDENTITY,
                    f"{where}: repeats the source identity already frozen for "
                    f"{seen_identities[identity]}",
                )
            seen_identities[identity] = where
            digest = payload["expected_sha256"]

            resolved.append(
                {
                    "pair_id": pair_id,
                    "side": side,
                    "path": target,
                    "expected_sha256": digest,
                    "relative_path": LOCAL_PATH_CONVENTION.format(
                        pair_id=pair_id, side=side, primary_document=document_name
                    ),
                }
            )
    return resolved


def verify_holdout_sources(
    resolved: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Every local body must hash to the committed digest before the frozen
    pipeline reads a single byte. A missing or drifted file refuses the whole
    run; no filing is ever re-downloaded, replaced, or skipped here."""
    verifications: list[dict[str, Any]] = []
    for entry in resolved:
        where = f"{entry['pair_id']}/{entry['side']}"
        target = Path(entry["path"])
        if not target.exists():
            raise V3BlindExtractionError(
                FAILURE_SOURCE_MISSING,
                f"{where}: the verified source body is not present locally. "
                "Acquisition is a separate, explicitly networked step; this "
                "run never downloads and never reacquires.",
            )
        observed = rfb.sha256_file(target)
        if observed != entry["expected_sha256"]:
            raise V3BlindExtractionError(
                FAILURE_SOURCE_SHA256_MISMATCH,
                f"{where}: local source bytes no longer hash to the digest the "
                "committed manifest froze. The file is preserved and the run "
                "refused; nothing is replaced and no committed hash is edited.",
            )
        verifications.append(
            {
                "pair_id": entry["pair_id"],
                "side": entry["side"],
                "source_sha256": observed,
                "verified": True,
            }
        )
    return verifications


#: Manifest fields that move with the LIFECYCLE rather than with the corpus's
#: identity. Excluded from the corpus identity hash so this run's identity is
#: stable across the very transition the run performs — the alternative would
#: make a rerun look like a different run and refuse itself.
_LIFECYCLE_MANIFEST_FIELDS = (
    "status",
    "corpus_role_detail",
    "description",
    "metadata_snapshot",
    "selected_at",
)


def corpus_identity_hash(manifest: Mapping[str, Any]) -> str:
    """Hash of everything that makes this corpus THIS corpus.

    Covers the benchmark id/version, every pair and side identity, all twenty
    ``expected_sha256`` values and ``source_verified`` flags, every frozen
    parser/grammar/detector/workflow/evaluator/annotation binding, the
    selection protocol version and hash, the seed, and the prior-corpus
    exclusions. It excludes only the lifecycle fields above, so it is the same
    value before and after ``source_verified -> corpus_built``.
    """
    return rfb.payload_hash(
        {
            key: value
            for key, value in manifest.items()
            if key not in _LIFECYCLE_MANIFEST_FIELDS
        }
    )


def compute_run_identity(
    *,
    manifest: Mapping[str, Any],
    source_verification_report: Mapping[str, Any] | None,
    source_verifications: list[Mapping[str, Any]],
    frozen_code: Mapping[str, str],
) -> dict[str, Any]:
    """The deterministic identity of this canonical run.

    Binds the corpus identity (selection identities, all twenty source digests,
    every frozen contract binding), the twenty digests observed on disk, and
    the frozen code that will process them. No wall clock, no local path, no
    database id, no random value, and nothing that moves when the manifest
    advances — so a reverification pass reproduces it exactly.
    """
    payload = {
        "protocol_version": V3_BLIND_RUN_PROTOCOL_VERSION,
        "protocol_hash": blind_run_protocol_hash(),
        "runner_version": V3_BLIND_RUNNER_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "benchmark_version": manifest["benchmark_version"],
        "corpus_identity_hash": corpus_identity_hash(manifest),
        "source_sha256_by_side": {
            f"{entry['pair_id']}:{entry['side']}": entry["source_sha256"]
            for entry in source_verifications
        },
        "frozen_code_hashes": dict(frozen_code),
    }
    run_hash = rfb.payload_hash(payload)
    return {
        "run_id": f"v3blind-{run_hash[:16]}",
        "run_hash": run_hash,
        "corpus_identity_hash": payload["corpus_identity_hash"],
        # The chained upstream digest: the exact source-verified manifest bytes
        # the committed acquisition report froze. Recorded, not part of the
        # identity, because those bytes are what this run advances.
        "source_verified_manifest_sha256": (
            (source_verification_report or {}).get("new_manifest_sha256")
        ),
        "protocol_version": V3_BLIND_RUN_PROTOCOL_VERSION,
        "protocol_hash": payload["protocol_hash"],
    }


def verify_output_identity(report_dir: str | Path, run_identity: Mapping[str, Any]) -> None:
    """An existing blind report in this directory must describe THIS run.

    A different run identity means the directory already holds a canonical
    result produced from different sources or different frozen code. That
    fails closed: the earlier attempt is never silently erased or overwritten.
    """
    report_path = Path(report_dir) / "blind_extraction_report.json"
    if not report_path.exists():
        return
    try:
        existing = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise V3BlindExtractionError(
            FAILURE_REPORT_UNREADABLE,
            f"an existing blind-extraction report could not be read "
            f"({type(exc).__name__})",
        ) from None
    if existing.get("run_hash") != run_identity["run_hash"]:
        raise V3BlindExtractionError(
            FAILURE_OUTPUT_IDENTITY_CONFLICT,
            "a blind-extraction report already exists in this directory with a "
            "different run identity. The recorded execution attempt is "
            "preserved; this run is refused rather than overwriting it.",
        )


def verify_blind_run_preconditions(
    manifest: Mapping[str, Any],
    manifest_path: str | Path,
    layout: rfb.CorpusLayout,
    *,
    repo_root: str | Path | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Refuse the run unless every frozen identity still holds.

    ``validate_v3_holdout_manifest`` pins the schema, pair ordering, strata,
    fiscal years, prior-corpus disjointness, and every frozen contract string.
    This gate adds what only the working tree can attest, in order: an
    extractable status, exactly ten pairs and twenty sides, the pinned source
    files still hashing to their frozen digests, the frozen exclusion sets
    still deriving from the committed prior manifests, the live pipeline still
    at the frozen versions, the committed source-verification report still
    binding this manifest, the manifest hash chain, no human annotation, twenty
    structurally valid non-duplicate local paths, and twenty matching digests.

    Every failure here processes zero bodies.
    """
    rfv3.validate_v3_holdout_manifest(manifest)

    if manifest["status"] not in (
        rfb.STATUS_SOURCE_VERIFIED,
        rfb.STATUS_CORPUS_BUILT,
    ):
        raise V3BlindExtractionError(
            FAILURE_MANIFEST_STATUS_INVALID,
            f"v3 holdout manifest status {manifest['status']!r} does not "
            "permit extraction: the blind run requires verified source bytes "
            f"({rfb.STATUS_SOURCE_VERIFIED!r}), and only a rerun may run at "
            f"{rfb.STATUS_CORPUS_BUILT!r}",
        )

    pair_count = len(manifest["pairs"])
    if pair_count != rfv3.TARGET_PAIR_COUNT:
        raise V3BlindExtractionError(
            FAILURE_MANIFEST_BINDING_MISMATCH,
            f"the canonical run requires exactly {rfv3.TARGET_PAIR_COUNT} "
            f"frozen pairs and {2 * rfv3.TARGET_PAIR_COUNT} sides, got "
            f"{pair_count} pairs",
        )
    for pair in manifest["pairs"]:
        for side, payload in rfb.pair_sides(pair):
            if not payload.get("source_verified"):
                raise V3BlindExtractionError(
                    FAILURE_MANIFEST_BINDING_MISMATCH,
                    f"{pair['pair_id']}/{side}: source_verified is not true; "
                    "the blind run never executes over a partial corpus",
                )

    try:
        rfv3.verify_frozen_code_identities(manifest, repo_root)
    except rfb.BenchmarkError as exc:
        raise V3BlindExtractionError(
            FAILURE_CONTRACT_VERSION_MISMATCH,
            "a pinned source file no longer matches the digest the v3 holdout "
            "manifest froze. The parser, unit grammar, detector, workflow "
            "store, and evaluator may not change after the freeze, and "
            "changing one in response to results from this corpus would "
            f"convert it into development data. ({exc.code})",
        ) from None

    try:
        rfv3.verify_exclusion_provenance(manifest, repo_root)
    except rfb.BenchmarkError as exc:
        raise V3BlindExtractionError(
            FAILURE_MANIFEST_BINDING_MISMATCH,
            "the manifest's frozen prior-corpus exclusions no longer match "
            f"those derived from the committed prior manifests ({exc.code})",
        ) from None

    contract = verify_contract_bindings(manifest)
    source_report = verify_source_verification_binding(manifest, manifest_path)
    verify_manifest_hash_chain(manifest_path, manifest["status"])
    annotations_inspected = verify_no_human_annotation(layout)

    resolved = resolve_source_paths(manifest, layout)
    verifications = verify_holdout_sources(resolved)
    frozen_code = frozen_code_hashes(repo_root)
    identity = compute_run_identity(
        manifest=manifest,
        source_verification_report=source_report,
        source_verifications=verifications,
        frozen_code=frozen_code,
    )
    if report_dir is not None:
        verify_output_identity(report_dir, identity)

    return {
        "contract": contract,
        "source_verification_report": source_report,
        "resolved_sources": resolved,
        "source_verifications": verifications,
        "frozen_code_hashes": frozen_code,
        "run_identity": identity,
        "local_annotations_inspected": annotations_inspected,
    }


# --- Canonical unit identity ------------------------------------------------------


def validate_canonical_unit_identities(record: Mapping[str, Any]) -> None:
    """Every unit carries a well-formed, unique ``side:sequence:unit_key``.

    The identity is produced by the frozen builder through
    ``real_filing_benchmark.unit_id``; this re-derives it and refuses any
    drift. Repeated normalized headings stay distinct by construction, because
    the sequence is a list position, not a heading key.
    """
    for side in ("previous", "current"):
        seen: set[str] = set()
        for index, unit in enumerate(record[side]["units"]):
            expected = rfb.unit_id(side, index, unit["unit_key"])
            if unit["unit_id"] != expected:
                raise V3BlindExtractionError(
                    FAILURE_UNIT_IDENTITY_INVALID,
                    f"{record['pair_id']}/{side}: unit at position {index} "
                    "does not carry its canonical sequence-aware identity",
                )
            if unit["unit_id"] in seen:
                raise V3BlindExtractionError(
                    FAILURE_DUPLICATE_UNIT_IDENTITY,
                    f"{record['pair_id']}/{side}: duplicate canonical unit "
                    "identity; units are never merged or deduplicated by "
                    "normalized heading",
                )
            seen.add(unit["unit_id"])


def _unit_key_repetitions(record_side: Mapping[str, Any]) -> int:
    """How many units share a normalized heading key with another unit.

    Nonzero means the filing repeats a heading. Those occurrences stay
    separate everywhere downstream — units, comparison seams, packet rows,
    machine labels, and this report.
    """
    counts: dict[str, int] = {}
    for unit in record_side.get("units", []):
        counts[unit["unit_key"]] = counts.get(unit["unit_key"], 0) + 1
    return sum(count for count in counts.values() if count > 1)


# --- One pair, blind ---------------------------------------------------------------


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
    source_manifest_hash: str,
    layout: rfb.CorpusLayout,
    detect: bool = True,
) -> dict[str, Any]:
    """Run the existing ingestion, extraction, unitization, and (when both
    sides extracted) comparison path over one frozen pair, and persist a build
    record.

    Every step is the EXISTING implementation from the development-benchmark
    builder; nothing here re-implements extraction, the unit grammar, or
    detection. A failure is recorded as an outcome, never repaired: the pair
    stays, the frozen code stays, and the record says what happened.
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

    # The digest is rechecked immediately before the parser reads the bytes,
    # not only in preflight: this is the read that feeds extraction.
    verified = builder.verify_sources(pair, layout)
    rechecks = len(verified)

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
        # The corpus IDENTITY hash, not the live manifest file digest: the
        # build came from these exact selection identities and these exact
        # twenty source digests, and that binding must not drift merely
        # because a lifecycle field advanced. It keeps build, packet, and
        # annotation hashes reproducible across the transition this run
        # performs.
        "source_manifest_hash": source_manifest_hash,
        "parser_versions": {
            "builder": V3_BLIND_RUNNER_VERSION,
            "html_parser": loaders_html.HTML_PARSER_VERSION,
            "section_key": ingest.SECTION_KEY_ITEM_1A,
            "unit_grammar": comparison_detector.DEFAULT_UNIT_GRAMMAR,
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
    validate_canonical_unit_identities(record)

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
    return {"record": record, "durations": durations, "source_rechecks": rechecks}


def _registry_entries(registry_path: Path) -> list[dict[str, Any]]:
    import filing_registry

    return filing_registry.list_entries(registry_path)


# --- The predeclared single execution ----------------------------------------------


def run_blind_extraction(
    manifest: Mapping[str, Any],
    manifest_path: str | Path,
    layout: rfb.CorpusLayout,
    *,
    repo_root: str | Path | None = None,
    report_dir: str | Path | None = None,
    now: Callable[[], str] = rfb.utc_now_iso,
) -> dict[str, Any]:
    """Verify everything, then run every pair once, in manifest order.

    One predeclared execution: no semantic change, no pair reordering, no
    replacement, no rerun-under-different-code between sides. Returns the
    bounded run payload; the caller decides what to write where.
    """
    manifest_path = Path(manifest_path)
    preflight = verify_blind_run_preconditions(
        manifest,
        manifest_path,
        layout,
        repo_root=repo_root,
        report_dir=report_dir,
    )
    hashes_before = preflight["frozen_code_hashes"]
    prior_manifest_sha256 = rfb.sha256_file(manifest_path)
    identity_hash = preflight["run_identity"]["corpus_identity_hash"]

    outcomes: list[dict[str, Any]] = []
    for pair in manifest["pairs"]:
        outcomes.append(
            blind_extract_pair(
                pair,
                manifest=manifest,
                source_manifest_hash=identity_hash,
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
    expected_sides = 2 * rfv3.TARGET_PAIR_COUNT
    if attempted_sides != expected_sides:
        raise V3BlindExtractionError(
            FAILURE_RUN_INCOMPLETE,
            f"{attempted_sides} of {expected_sides} sides carry a recorded "
            "outcome; the run is incomplete and no manifest transition or "
            "report may claim otherwise",
        )

    return {
        "records": records,
        "durations": [outcome["durations"] for outcome in outcomes],
        "source_verifications": preflight["source_verifications"],
        "resolved_sources": preflight["resolved_sources"],
        "source_rechecks": sum(outcome["source_rechecks"] for outcome in outcomes),
        "run_identity": preflight["run_identity"],
        "source_verification_report": preflight["source_verification_report"],
        "prior_manifest_sha256": prior_manifest_sha256,
        "prior_manifest_status": manifest["status"],
        "frozen_code_hashes_before": hashes_before,
        "frozen_code_hashes_after": hashes_after,
        "contract": preflight["contract"],
        "started_reported_at": now(),
    }


# --- Manifest transition ------------------------------------------------------------


def corpus_built_corpus_role_detail() -> str:
    """Role prose that is TRUE after the blind extraction run."""
    return (
        "Issuers and exact filing pairs were frozen from official SEC metadata "
        "only, after item1a_units.v3 / item1a_detector.v3 / "
        "comparison_workflow.v3 and the contract-v2 gold evaluator "
        "(real-filing-benchmark.evaluation.v2 + metrics.v2 + report.v2) were "
        "merged and frozen, and before any selected filing body was downloaded "
        "or inspected. Neither the parser, the unit grammar, nor the "
        "evaluation contract was developed using this corpus. The twenty "
        "frozen bodies were acquired from official SEC sources and "
        "checksum-verified, and the frozen pipeline has now run over them "
        "exactly once, blind and unchanged, with every extraction and "
        "comparison outcome recorded as it fell. No label has been "
        "human-verified: execution COVERAGE on this corpus is not extraction "
        "or detector CORRECTNESS, so no holdout evaluation exists yet and no "
        "generalization claim is supported. A selected pair is never replaced, "
        "and modifying any pinned frozen file in response to these results "
        "would convert this corpus into development data; the recorded hashes "
        "make that detectable."
    )


CORPUS_BUILT_DESCRIPTION = (
    "v3 extraction holdout at corpus_built: exact issuers and filing pairs "
    "frozen from official SEC metadata AFTER the v3 unit representation and "
    "the contract-v2 gold evaluator were frozen and BEFORE any selected filing "
    "body was observed; the twenty checksum-verified bodies have since been "
    "processed by the frozen parser, unit grammar, detector, and comparison "
    "workflow exactly once, blind and unchanged. Every outcome was recorded as "
    "observed, including blocked sides and blocked pairs. No human-verified "
    "label exists, so no accuracy, holdout-evaluation, or generalization "
    "result exists. A selected pair is never replaced because of "
    "later-observed filing-body structure, extraction outcome, detector "
    "output, workflow output, or evaluation result."
)


def advance_v3_holdout_manifest_to_corpus_built(
    manifest: Mapping[str, Any], records: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """The one forward step: ``source_verified`` -> ``corpus_built``.

    ``corpus_built`` asserts that the corpus build ran and produced a recorded
    outcome for every side — NOT that extraction succeeded. A missing,
    ambiguous, or parse-failed side is a preserved blind result. Changes
    exactly three things — status, corpus_role_detail, description — and
    re-validates, so every selection identity, every source digest, every
    ``source_verified`` flag, every frozen binding, and every denial survives
    unchanged.
    """
    rfv3.validate_v3_holdout_status_transition(
        manifest["status"], rfb.STATUS_CORPUS_BUILT
    )
    by_pair = {record["pair_id"]: record for record in records}
    for pair in manifest["pairs"]:
        record = by_pair.get(pair["pair_id"])
        if record is None:
            raise V3BlindExtractionError(
                FAILURE_RUN_INCOMPLETE,
                f"no build record for frozen pair {pair['pair_id']}; the "
                "manifest does not advance past a pair the run skipped",
            )
        for side in ("previous", "current"):
            if record[side]["extraction_outcome"] not in rfb.EXTRACTION_OUTCOMES:
                raise V3BlindExtractionError(
                    FAILURE_RUN_INCOMPLETE,
                    f"{pair['pair_id']}/{side} carries no recorded outcome",
                )

    advanced = copy.deepcopy(dict(manifest))
    advanced["status"] = rfb.STATUS_CORPUS_BUILT
    advanced["corpus_role_detail"] = corpus_built_corpus_role_detail()
    if "description" in advanced:
        advanced["description"] = CORPUS_BUILT_DESCRIPTION
    rfv3.validate_v3_holdout_manifest(advanced)
    return advanced


# --- Reproducible projection ---------------------------------------------------------

#: Keys whose values legitimately differ between two runs of identical inputs:
#: wall-clock stamps, measured durations, and repository checkout metadata.
#: Everything else in a committed report must be byte-identical across runs,
#: which is what makes "deterministic bounded outputs" a checkable claim.
VOLATILE_REPORT_KEYS = frozenset(
    {
        "generated_at",
        "built_at",
        "duration_ms",
        "ingest_duration_ms",
        "detect_duration_ms",
        "total_duration_ms",
        "evaluated_at",
        "commit_sha",
        "reproducible_payload_hash",
    }
)


#: Keys that record WHICH lifecycle step a run performed. The canonical run
#: advances source_verified -> corpus_built; a later reverification pass over
#: the same bytes performs no step and can never reproduce them. They stay in
#: the committed report (they are the hash chain) but are excluded from the
#: reproducible output identity, which is about the OUTPUT, not the step.
TRANSITION_REPORT_KEYS = frozenset(
    {
        "prior_manifest_status",
        "prior_manifest_sha256",
        "new_manifest_status",
    }
)

NON_REPRODUCIBLE_REPORT_KEYS = VOLATILE_REPORT_KEYS | TRANSITION_REPORT_KEYS


def reproducible_report(value: Any) -> Any:
    """A report's stable projection: volatile keys removed recursively."""
    if isinstance(value, dict):
        return {
            key: reproducible_report(item)
            for key, item in sorted(value.items())
            if key not in NON_REPRODUCIBLE_REPORT_KEYS
        }
    if isinstance(value, list):
        return [reproducible_report(item) for item in value]
    return value


def reproducible_payload_hash(report: Mapping[str, Any]) -> str:
    """sha256 over canonical JSON of a report's reproducible projection."""
    return rfb.payload_hash(reproducible_report(dict(report)))


def payloads_agree(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Two reports describe the same run outcome, ignoring volatile fields."""
    return reproducible_report(dict(left)) == reproducible_report(dict(right))


# --- Committed reports ----------------------------------------------------------------


def _blocked_reason(record: Mapping[str, Any]) -> str | None:
    """Stable pair-level blocking reason, or None when the pair is complete."""
    previous_ok = (
        record["previous"]["extraction_outcome"] == rfb.EXTRACTION_EXTRACTED
    )
    current_ok = record["current"]["extraction_outcome"] == rfb.EXTRACTION_EXTRACTED
    if not previous_ok and not current_ok:
        return BLOCKED_BOTH_SIDES_NOT_EXTRACTED
    if not previous_ok:
        return BLOCKED_PREVIOUS_NOT_EXTRACTED
    if not current_ok:
        return BLOCKED_CURRENT_NOT_EXTRACTED
    if not (record.get("execution") or {}).get("executed"):
        return BLOCKED_COMPARISON_NOT_DETECTED
    return None


def _side_report_row(
    pair_id: str,
    side: str,
    record_side: Mapping[str, Any],
    layout: rfb.CorpusLayout,
    duration_ms: int,
) -> dict[str, Any]:
    """One bounded per-side row. Counts, stable codes, hashes, canonical unit
    identities, a corpus-relative artifact path, and two bounded heading labels
    — never section text, never a unit body, never an absolute path."""
    heading = record_side.get("heading_detected")
    boundary = record_side.get("boundary_heading")
    unit_ids = [unit["unit_id"] for unit in record_side.get("units", [])]
    return {
        "pair_id": pair_id,
        "side": side,
        "source_sha256": record_side.get("source_sha256"),
        "parser_version": record_side.get("extraction_parser_version"),
        "extraction_status": record_side["extraction_outcome"],
        "reason_code": record_side.get("extraction_reason"),
        "candidate_count": record_side.get("candidate_count", 0),
        "substantive_candidate_count": record_side.get(
            "substantive_candidate_count", 0
        ),
        "rejected_navigation_count": record_side.get("navigation_rejected_count", 0),
        "selected_heading": heading[: rfb.MAX_HEADING_CHARS] if heading else None,
        "selected_tag": record_side.get("selected_element_tag"),
        "boundary_heading": boundary[: rfb.MAX_HEADING_CHARS] if boundary else None,
        "section_sha256": record_side.get("section_hash"),
        "section_character_count": record_side.get("section_char_count", 0),
        "section_paragraph_count": record_side.get("section_paragraph_count", 0),
        "section_chunk_count": record_side.get("section_chunk_count", 0),
        "indexed_chunk_count": record_side.get("indexed_chunk_count", 0),
        "unit_count": record_side.get("unit_count", 0),
        "canonical_unit_id_count": len(unit_ids),
        "distinct_unit_key_count": len({unit_id.split(":", 2)[2] for unit_id in unit_ids}),
        "repeated_unit_key_occurrences": _unit_key_repetitions(record_side),
        "local_artifact_path": layout.relative(layout.build_record_path(pair_id)),
        "duration_ms": duration_ms,
    }


def _pair_report_row(
    record: Mapping[str, Any],
    packet_row: Mapping[str, Any] | None,
    duration_ms: int,
) -> dict[str, Any]:
    """One bounded per-pair row: buildable or blocked, with a stable reason."""
    execution = record.get("execution") or {}
    blocked_reason = _blocked_reason(record)
    return {
        "pair_id": record["pair_id"],
        "buildable": blocked_reason is None,
        "blocked_reason": blocked_reason,
        "previous_extraction_status": record["previous"]["extraction_outcome"],
        "current_extraction_status": record["current"]["extraction_outcome"],
        "comparison_executed": bool(execution.get("executed")),
        "comparison_result_hash": execution.get("result_hash"),
        "detector_version": execution.get("detector_version"),
        "workflow_version": execution.get("workflow_version"),
        "change_count": execution.get("change_count", 0),
        "changes_by_type": dict(execution.get("changes_by_type") or {}),
        "packet_id": (packet_row or {}).get("packet_id"),
        "packet_sha256": (packet_row or {}).get("packet_sha256"),
        "machine_proposed_label_count": (packet_row or {}).get("label_count", 0),
        "duration_ms": duration_ms,
    }


def _corpus_role_fields() -> dict[str, Any]:
    fields = rfv3.corpus_role_fields()
    fields["corpus_role_detail"] = corpus_built_corpus_role_detail()
    return fields


def build_blind_extraction_report(
    *,
    manifest: Mapping[str, Any],
    run: Mapping[str, Any],
    packet_rows: list[Mapping[str, Any]],
    new_manifest_status: str,
    new_manifest_sha256: str | None,
    generated_at: str,
    layout: rfb.CorpusLayout,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """The committed record of the blind run: per-side and per-pair outcomes,
    totals, the frozen-code attestation, and the manifest hash chain."""
    records = run["records"]
    durations = run["durations"]
    identity = run["run_identity"]
    packets_by_pair = {row["pair_id"]: row for row in packet_rows}

    sides: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    for record, pair_durations in zip(records, durations):
        for side in ("previous", "current"):
            sides.append(
                _side_report_row(
                    record["pair_id"],
                    side,
                    record[side],
                    layout,
                    pair_durations.get(side, 0),
                )
            )
        pairs.append(
            _pair_report_row(
                record,
                packets_by_pair.get(record["pair_id"]),
                pair_durations.get("detect_ms", 0),
            )
        )

    extraction_totals = {
        outcome: sum(1 for row in sides if row["extraction_status"] == outcome)
        for outcome in rfb.EXTRACTION_OUTCOMES
    }
    blocked_totals = {
        reason: sum(1 for row in pairs if row["blocked_reason"] == reason)
        for reason in BLOCKED_PAIR_REASONS
    }
    buildable = [row for row in pairs if row["buildable"]]
    comparison_results = [row for row in pairs if row["comparison_result_hash"]]
    written_packets = [
        row for row in packet_rows if row["packet_status"] == "written"
    ]

    report = {
        "report_version": V3_BLIND_EXTRACTION_REPORT_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "benchmark_version": manifest["benchmark_version"],
        "generated_at": generated_at,
        "blind_run_protocol_version": identity["protocol_version"],
        "blind_run_protocol_hash": identity["protocol_hash"],
        "blind_run_protocol": blind_run_protocol(),
        "runner_version": V3_BLIND_RUNNER_VERSION,
        "run_id": identity["run_id"],
        "run_hash": identity["run_hash"],
        "corpus_identity_hash": identity["corpus_identity_hash"],
        "predeclared_single_execution": True,
        "semantic_code_modified_during_run": False,
        "pairs_replaced": 0,
        "execution_order": EXECUTION_ORDER,
        "prior_manifest_status": run["prior_manifest_status"],
        "new_manifest_status": new_manifest_status,
        "prior_manifest_sha256": run["prior_manifest_sha256"],
        "new_manifest_sha256": new_manifest_sha256,
        "source_verified_manifest_sha256": identity[
            "source_verified_manifest_sha256"
        ],
        "source_verification_report_version": (
            (run["source_verification_report"] or {}).get("report_version")
        ),
        "source_acquisition_protocol_hash": (
            (run["source_verification_report"] or {}).get(
                "source_acquisition_protocol_hash"
            )
        ),
        "frozen_extraction_parser_version": manifest[
            "frozen_extraction_parser_version"
        ],
        "frozen_parser_source_path": manifest["frozen_parser_source_path"],
        "frozen_parser_source_sha256": manifest["frozen_parser_source_sha256"],
        "frozen_unit_grammar_version": manifest["frozen_unit_grammar_version"],
        "frozen_detector_version": manifest["frozen_detector_version"],
        "frozen_detector_source_sha256": manifest["frozen_detector_source_sha256"],
        "frozen_workflow_version": manifest["frozen_workflow_version"],
        "frozen_workflow_source_sha256": manifest["frozen_workflow_source_sha256"],
        "frozen_evaluation_contract_version": manifest[
            "frozen_evaluation_contract_version"
        ],
        "frozen_evaluator_source_sha256": manifest["frozen_evaluator_source_sha256"],
        "comparison_schema_version": FROZEN_COMPARISON_SCHEMA_VERSION,
        "annotation_schema_version": manifest["frozen_annotation_schema_version"],
        "annotation_protocol_version": manifest["frozen_annotation_protocol_version"],
        "packet_schema_version": rfb.PACKET_SCHEMA_VERSION,
        "unit_identity_contract": manifest["frozen_unit_identity_contract"],
        "selection_protocol_version": manifest["selection_protocol_version"],
        "selection_protocol_hash": manifest["selection_protocol_hash"],
        **_corpus_role_fields(),
        "frozen_code_hashes_before": dict(run["frozen_code_hashes_before"]),
        "frozen_code_hashes_after": dict(run["frozen_code_hashes_after"]),
        "frozen_code_unchanged": (
            run["frozen_code_hashes_before"] == run["frozen_code_hashes_after"]
        ),
        "pair_count": len(records),
        "side_count": len(sides),
        "sides": sides,
        "pairs": pairs,
        "extraction_status_counts": extraction_totals,
        "blocked_reason_counts": blocked_totals,
        "buildable_pair_count": len(buildable),
        "blocked_pair_count": len(pairs) - len(buildable),
        "comparison_result_count": len(comparison_results),
        "packet_count": len(written_packets),
        "machine_proposed_label_count": sum(
            row["label_count"] for row in packet_rows
        ),
        "human_verified_label_count": 0,
        "gold_evaluation_runs": 0,
        "network_requests": 0,
        "source_downloads": 0,
        "source_checksum_reverifications": (
            len(run["source_verifications"]) + run["source_rechecks"]
        ),
        "extraction_runs": len(sides),
        "comparison_runs": len(comparison_results),
        "extraction_holdout_evaluation": False,
        "generalization_claim_supported": False,
        "signoff_present": False,
        "development_evidence_boundary": (
            "This corpus is now OBSERVED by the frozen v3 pipeline. It may be "
            "used as unseen-evaluation evidence only for the exact frozen "
            "parser, unit grammar, detector, workflow, and evaluation contract "
            "recorded above. Any semantic correction made in response to these "
            "results — to heading recognition, section boundaries, unit "
            "boundaries, detector semantics, workflow semantics, the evaluator "
            "contract, the annotation schema, or metric definitions — converts "
            "this corpus into development data and requires a new version plus "
            "a separately frozen, unseen holdout."
        ),
        "gold_metrics_available": False,
        "gold_metrics": None,
        "commit_sha": rfb.repo_commit_sha(repo_root),
        "notes": [
            (
                "BLIND RESULT. This is the first time the frozen v3 pipeline "
                "(sec_html_item_headings.v2 section extraction, "
                "item1a_units.v3, item1a_detector.v3, comparison_workflow.v3) "
                "observed these twenty filings: the corpus was frozen from "
                "metadata after those components were frozen, bodies were "
                "acquired and checksum-verified without extraction, and this "
                "run executed them unchanged over all twenty sides in one "
                "predeclared pass."
            ),
            (
                "This report claims blind execution COVERAGE only: exact "
                "extracted, missing, ambiguous, and parse-failed counts, the "
                "buildable and blocked pair counts with stable reasons, and "
                "machine-proposed packet availability. Coverage is not "
                "correctness, and a buildable-pair rate is not detector "
                "quality."
            ),
            (
                "No detector-accuracy, annotation-accuracy, precision, recall, "
                "exact-match, unchanged-FPR, or filing-change-quality number "
                "exists or may be derived from this report: zero labels are "
                "human-verified, so generalization_claim_supported remains "
                "false at any coverage. Independent human review and "
                "annotation admission is the required next step."
            ),
            (
                "Every extraction and comparison outcome is preserved exactly "
                "as observed. A defect this run exposes is recorded, not "
                "repaired: no filing was replaced, no blocked side or pair was "
                "removed, and no semantic failure was reclassified as an "
                "infrastructure failure."
            ),
            (
                "Filing bodies, extracted section text, unit text, per-pair "
                "indexes, comparison databases, full detector results, packet "
                "bodies, and machine-proposed annotations stay in the "
                "gitignored benchmark_data/ tree. This report carries counts, "
                "stable codes, hashes, canonical unit identity counts, "
                "corpus-relative artifact paths, and bounded heading labels "
                "only."
            ),
        ],
        "regenerated_by": (
            "python scripts/run_real_filing_v3_holdout_blind_extraction.py "
            "(offline; requires locally verified v3 holdout sources)"
        ),
    }
    report["reproducible_payload_hash"] = reproducible_payload_hash(report)
    validate_blind_extraction_report(report)
    return report


def build_execution_report(
    *,
    manifest: Mapping[str, Any],
    run: Mapping[str, Any],
    manifest_sha256: str | None,
    generated_at: str,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Unlabeled execution report: what the frozen workflow DID on these pairs.

    Deliberately mirrors the development benchmark's --unlabeled mode and the
    first holdout's execution report: bounded execution mechanics and local
    artifact identity, no accuracy metric of any kind, and its own payload says
    so. It does not duplicate the blind extraction report's per-side rows.
    """
    records = run["records"]
    durations = run["durations"]
    identity = run["run_identity"]

    executions = []
    for record, pair_durations in zip(records, durations):
        execution = record.get("execution") or {}
        attempts = execution.get("attempts") or []
        undetermined = (execution.get("changes_by_type") or {}).get("undetermined", 0)
        executions.append(
            {
                "pair_id": record["pair_id"],
                "buildable": _blocked_reason(record) is None,
                "blocked_reason": _blocked_reason(record),
                "executed": bool(execution.get("executed")),
                "execution_status": execution.get("lifecycle"),
                "skipped_reason": execution.get("skipped_reason"),
                "failure_code": execution.get("failure_code"),
                "build_hash": record["build_hash"],
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
                "previous_canonical_unit_id_count": len(record["previous"]["units"]),
                "current_canonical_unit_id_count": len(record["current"]["units"]),
                "duration_ms": pair_durations.get("detect_ms", 0),
                "attempt_count": len(attempts),
                # Structural zeroes for the direct synchronous path: no durable
                # job is created, so no retry, reclaim, or lease can exist.
                "retries": max(0, len(attempts) - 1),
                "reclaims": 0,
                "detection_jobs": 0,
            }
        )

    return {
        "report_version": V3_EXECUTION_REPORT_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "benchmark_version": manifest["benchmark_version"],
        "mode": "unlabeled_execution_report",
        "generated_at": generated_at,
        "run_id": identity["run_id"],
        "run_hash": identity["run_hash"],
        "blind_run_protocol_version": identity["protocol_version"],
        "blind_run_protocol_hash": identity["protocol_hash"],
        "manifest_status": rfb.STATUS_CORPUS_BUILT,
        "manifest_sha256": manifest_sha256,
        "build_source_manifest_hash": run["run_identity"]["corpus_identity_hash"],
        "detector_version": run["contract"]["detector_version"],
        "workflow_version": run["contract"]["workflow_version"],
        "unit_grammar_version": run["contract"]["unit_grammar_version"],
        "comparison_schema_version": run["contract"]["comparison_schema_version"],
        **_corpus_role_fields(),
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
        "executions": executions,
        "steps": {
            "source_checksum_reverifications": (
                len(run["source_verifications"]) + run["source_rechecks"]
            ),
            "sides_extracted_attempted": 2 * len(records),
            "pairs_attempted": len(records),
            "comparisons_executed": sum(
                1 for entry in executions if entry["executed"]
            ),
            "comparisons_blocked": sum(
                1 for entry in executions if not entry["executed"]
            ),
            "network_requests": 0,
            "source_downloads": 0,
            "gold_evaluation_runs": 0,
            "human_verified_labels": 0,
        },
        "local_artifact_inventory_hash": rfb.payload_hash(
            [
                {
                    "pair_id": record["pair_id"],
                    "build_hash": record["build_hash"],
                    "result_hash": (record.get("execution") or {}).get("result_hash"),
                }
                for record in records
            ]
        ),
        "human_verified_labels": 0,
        "gold_metrics_available": False,
        "gold_metrics": None,
        "commit_sha": rfb.repo_commit_sha(repo_root),
        "regenerated_by": (
            "python scripts/run_real_filing_v3_holdout_blind_extraction.py "
            "(offline; requires locally verified v3 holdout sources)"
        ),
    }


def build_packet_inventory(
    *,
    manifest: Mapping[str, Any],
    run: Mapping[str, Any],
    packet_rows: list[Mapping[str, Any]],
    manifest_sha256: str | None,
    generated_at: str,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Bounded committed inventory of the LOCAL machine-proposed packets.

    Identity, counts, hashes, bindings, and readiness only. Packet bodies,
    excerpts, unit text, and annotations never appear here; every annotation is
    machine_proposed and zero labels are human-verified, which this payload
    states structurally rather than only in prose.
    """
    identity = run["run_identity"]
    pairs = sorted((dict(row) for row in packet_rows), key=lambda row: row["pair_id"])
    return {
        "report_version": V3_PACKET_INVENTORY_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "benchmark_version": manifest["benchmark_version"],
        "generated_at": generated_at,
        "run_id": identity["run_id"],
        "run_hash": identity["run_hash"],
        "blind_run_protocol_version": identity["protocol_version"],
        "blind_run_protocol_hash": identity["protocol_hash"],
        "manifest_sha256": manifest_sha256,
        "build_source_manifest_hash": run["run_identity"]["corpus_identity_hash"],
        "annotation_schema_version": manifest["frozen_annotation_schema_version"],
        "annotation_protocol_version": manifest["frozen_annotation_protocol_version"],
        "packet_schema_version": rfb.PACKET_SCHEMA_VERSION,
        "packet_generator_version": FROZEN_PACKET_GENERATOR_VERSION,
        "unit_identity_contract": manifest["frozen_unit_identity_contract"],
        **_corpus_role_fields(),
        "packets_written": sum(
            1 for row in pairs if row["packet_status"] == "written"
        ),
        "packets_blocked": sum(
            1 for row in pairs if row["packet_status"] == "blocked"
        ),
        "machine_proposed_label_count": sum(row["label_count"] for row in pairs),
        "human_verified_label_count": 0,
        "gold_evaluation_runs": 0,
        "pairs": pairs,
        "notes": [
            (
                "MACHINE-PROPOSED — NOT GROUND TRUTH. Every label in every "
                "packet was produced by deterministic detector output. No "
                "human reviewer has admitted any of them, they cannot "
                "contribute to any metric, and a machine proposal never "
                "defaults to human_verified."
            ),
            (
                "human_verified requires a reviewer to separately edit or "
                "create the human annotation artifact through the human "
                "admission workflow, supplying an annotator_id and a "
                "verification timestamp that no tool in this repository sets "
                "or infers."
            ),
            (
                "Packets, packet prose, and machine-proposed annotations stay "
                "in the gitignored benchmark_data/ tree; only this bounded "
                "inventory is committed, and it carries no packet body text."
            ),
            (
                "A blocked pair stays in the corpus and is reported with its "
                "stable blocking reason. It is never replaced, and its "
                "extraction outcome is never revisited under changed semantic "
                "code."
            ),
            (
                "These rows are an inventory of proposals, NOT annotations "
                "admitted for evaluation. Human review of these packets is the "
                "remaining Stage 3.5 work: until labels are human-verified, no "
                "accuracy number exists and generalization_claim_supported "
                "remains false."
            ),
        ],
        "commit_sha": rfb.repo_commit_sha(repo_root),
        "regenerated_by": (
            "python scripts/run_real_filing_v3_holdout_blind_extraction.py "
            "(offline; requires locally verified v3 holdout sources)"
        ),
    }


# --- Committed-report validation ------------------------------------------------------

_BLIND_REPORT_REQUIRED = (
    "report_version",
    "benchmark_id",
    "benchmark_version",
    "generated_at",
    "blind_run_protocol_version",
    "blind_run_protocol_hash",
    "blind_run_protocol",
    "runner_version",
    "run_id",
    "run_hash",
    "corpus_identity_hash",
    "predeclared_single_execution",
    "semantic_code_modified_during_run",
    "pairs_replaced",
    "execution_order",
    "prior_manifest_status",
    "new_manifest_status",
    "prior_manifest_sha256",
    "new_manifest_sha256",
    "source_verified_manifest_sha256",
    "source_verification_report_version",
    "source_acquisition_protocol_hash",
    "frozen_extraction_parser_version",
    "frozen_parser_source_path",
    "frozen_parser_source_sha256",
    "frozen_unit_grammar_version",
    "frozen_detector_version",
    "frozen_detector_source_sha256",
    "frozen_workflow_version",
    "frozen_workflow_source_sha256",
    "frozen_evaluation_contract_version",
    "frozen_evaluator_source_sha256",
    "comparison_schema_version",
    "annotation_schema_version",
    "annotation_protocol_version",
    "packet_schema_version",
    "unit_identity_contract",
    "selection_protocol_version",
    "selection_protocol_hash",
    "corpus_role",
    "corpus_role_detail",
    "extraction_parser_developed_using_this_corpus",
    "evaluation_contract_developed_using_this_corpus",
    "extraction_holdout_evaluation",
    "generalization_claim_supported",
    "frozen_code_hashes_before",
    "frozen_code_hashes_after",
    "frozen_code_unchanged",
    "pair_count",
    "side_count",
    "sides",
    "pairs",
    "extraction_status_counts",
    "blocked_reason_counts",
    "buildable_pair_count",
    "blocked_pair_count",
    "comparison_result_count",
    "packet_count",
    "machine_proposed_label_count",
    "human_verified_label_count",
    "gold_evaluation_runs",
    "network_requests",
    "source_downloads",
    "source_checksum_reverifications",
    "extraction_runs",
    "comparison_runs",
    "signoff_present",
    "development_evidence_boundary",
    "gold_metrics_available",
    "gold_metrics",
    "commit_sha",
    "notes",
    "regenerated_by",
    "reproducible_payload_hash",
)

_BLIND_SIDE_ROW_REQUIRED = (
    "pair_id",
    "side",
    "source_sha256",
    "parser_version",
    "extraction_status",
    "reason_code",
    "candidate_count",
    "substantive_candidate_count",
    "rejected_navigation_count",
    "selected_heading",
    "selected_tag",
    "boundary_heading",
    "section_sha256",
    "section_character_count",
    "section_paragraph_count",
    "section_chunk_count",
    "indexed_chunk_count",
    "unit_count",
    "canonical_unit_id_count",
    "distinct_unit_key_count",
    "repeated_unit_key_occurrences",
    "local_artifact_path",
    "duration_ms",
)

_BLIND_PAIR_ROW_REQUIRED = (
    "pair_id",
    "buildable",
    "blocked_reason",
    "previous_extraction_status",
    "current_extraction_status",
    "comparison_executed",
    "comparison_result_hash",
    "detector_version",
    "workflow_version",
    "change_count",
    "changes_by_type",
    "packet_id",
    "packet_sha256",
    "machine_proposed_label_count",
    "duration_ms",
)

_PACKET_ROW_REQUIRED = (
    "pair_id",
    "packet_id",
    "packet_status",
    "packet_relative_path",
    "packet_sha256",
    "blocking_reason",
    "annotation_status",
    "human_verified",
    "annotator_id",
    "verification_timestamp",
    "label_count",
    "annotation_relative_path",
    "annotation_sha256",
    "previous_extraction_outcome",
    "current_extraction_outcome",
    "previous_section_hash",
    "current_section_hash",
    "previous_unit_count",
    "current_unit_count",
    "previous_canonical_unit_id_count",
    "current_canonical_unit_id_count",
    "labelled_unit_id_count",
    "comparison_result_hash",
    "review_ready",
)


def validate_blind_extraction_report(document: Any) -> None:
    """Exact-key structural validation, plus the reconciliations that make the
    aggregate counts meaningful and the denials structural."""
    rfb._exact_keys(  # noqa: SLF001 - shared validator
        document,
        required=_BLIND_REPORT_REQUIRED,
        where="v3 blind extraction report",
        error=V3BlindExtractionError,
        code_prefix="blind_report",
    )
    _require = rfb._require  # noqa: SLF001
    _require(
        document["report_version"] == V3_BLIND_EXTRACTION_REPORT_VERSION,
        V3BlindExtractionError,
        "blind_report_version_mismatch",
        f"report_version must be {V3_BLIND_EXTRACTION_REPORT_VERSION!r}",
    )
    for index, row in enumerate(document["sides"]):
        rfb._exact_keys(  # noqa: SLF001
            row,
            required=_BLIND_SIDE_ROW_REQUIRED,
            where=f"v3 blind extraction report.sides[{index}]",
            error=V3BlindExtractionError,
            code_prefix="blind_report_side",
        )
        _require(
            row["extraction_status"] in rfb.EXTRACTION_OUTCOMES,
            V3BlindExtractionError,
            "blind_report_invalid_extraction_status",
            f"sides[{index}]: extraction_status must be one of "
            f"{list(rfb.EXTRACTION_OUTCOMES)}",
        )
    for index, row in enumerate(document["pairs"]):
        rfb._exact_keys(  # noqa: SLF001
            row,
            required=_BLIND_PAIR_ROW_REQUIRED,
            where=f"v3 blind extraction report.pairs[{index}]",
            error=V3BlindExtractionError,
            code_prefix="blind_report_pair",
        )
        _require(
            row["blocked_reason"] is None
            or row["blocked_reason"] in BLOCKED_PAIR_REASONS,
            V3BlindExtractionError,
            "blind_report_invalid_blocked_reason",
            f"pairs[{index}]: blocked_reason must be null or one of "
            f"{list(BLOCKED_PAIR_REASONS)}",
        )

    _require(
        len(document["sides"]) == document["side_count"] == 2 * document["pair_count"],
        V3BlindExtractionError,
        "blind_report_side_count_mismatch",
        "every selected side must appear exactly once",
    )
    _require(
        len(document["pairs"]) == document["pair_count"],
        V3BlindExtractionError,
        "blind_report_pair_count_mismatch",
        "every selected pair must appear exactly once",
    )
    _require(
        sum(document["extraction_status_counts"].values()) == document["side_count"],
        V3BlindExtractionError,
        "blind_report_extraction_totals_mismatch",
        "extraction status counts must reconcile with the per-side rows",
    )
    _require(
        document["buildable_pair_count"] + document["blocked_pair_count"]
        == document["pair_count"],
        V3BlindExtractionError,
        "blind_report_pair_totals_mismatch",
        "buildable and blocked pairs must sum to the pair count",
    )
    _require(
        sum(document["blocked_reason_counts"].values())
        == document["blocked_pair_count"],
        V3BlindExtractionError,
        "blind_report_blocked_totals_mismatch",
        "blocked reason counts must reconcile with the blocked pair count",
    )
    _require(
        document["human_verified_label_count"] == 0
        and document["gold_evaluation_runs"] == 0
        and document["network_requests"] == 0
        and document["source_downloads"] == 0
        and document["extraction_holdout_evaluation"] is False
        and document["generalization_claim_supported"] is False
        and document["signoff_present"] is False
        and document["gold_metrics_available"] is False
        and document["gold_metrics"] is None,
        V3BlindExtractionError,
        "blind_report_denial_violated",
        "a blind-run report can never claim a human label, a gold evaluation, "
        "a network request, a source download, a holdout evaluation, a "
        "generalization claim, or a sign-off",
    )


def validate_packet_inventory(document: Any) -> None:
    """Exact-key structural validation for the committed packet inventory."""
    required = (
        "report_version",
        "benchmark_id",
        "benchmark_version",
        "generated_at",
        "run_id",
        "run_hash",
        "blind_run_protocol_version",
        "blind_run_protocol_hash",
        "manifest_sha256",
        "build_source_manifest_hash",
        "annotation_schema_version",
        "annotation_protocol_version",
        "packet_schema_version",
        "packet_generator_version",
        "unit_identity_contract",
        "corpus_role",
        "corpus_role_detail",
        "extraction_parser_developed_using_this_corpus",
        "evaluation_contract_developed_using_this_corpus",
        "extraction_holdout_evaluation",
        "generalization_claim_supported",
        "packets_written",
        "packets_blocked",
        "machine_proposed_label_count",
        "human_verified_label_count",
        "gold_evaluation_runs",
        "pairs",
        "notes",
        "commit_sha",
        "regenerated_by",
    )
    rfb._exact_keys(  # noqa: SLF001
        document,
        required=required,
        where="v3 packet inventory",
        error=V3BlindExtractionError,
        code_prefix="blind_packet_inventory",
    )
    _require = rfb._require  # noqa: SLF001
    for index, row in enumerate(document["pairs"]):
        rfb._exact_keys(  # noqa: SLF001
            row,
            required=_PACKET_ROW_REQUIRED,
            where=f"v3 packet inventory.pairs[{index}]",
            error=V3BlindExtractionError,
            code_prefix="blind_packet_inventory_row",
        )
        _require(
            row["annotation_status"] == rfb.ANNOTATION_MACHINE_PROPOSED
            and row["human_verified"] is False
            and row["annotator_id"] is None
            and row["verification_timestamp"] is None,
            V3BlindExtractionError,
            FAILURE_PACKET_INVENTORY_MISMATCH,
            f"pairs[{index}]: every inventory row must remain "
            "machine_proposed with no invented annotator identity or "
            "verification timestamp",
        )
    _require(
        document["packets_written"] + document["packets_blocked"]
        == len(document["pairs"]),
        V3BlindExtractionError,
        FAILURE_PACKET_INVENTORY_MISMATCH,
        "written and blocked packet counts must reconcile with the rows",
    )
    _require(
        document["machine_proposed_label_count"]
        == sum(row["label_count"] for row in document["pairs"]),
        V3BlindExtractionError,
        FAILURE_PACKET_INVENTORY_MISMATCH,
        "the machine-proposed label count must reconcile with the rows",
    )
    _require(
        document["human_verified_label_count"] == 0
        and document["gold_evaluation_runs"] == 0
        and document["extraction_holdout_evaluation"] is False
        and document["generalization_claim_supported"] is False,
        V3BlindExtractionError,
        FAILURE_PACKET_INVENTORY_MISMATCH,
        "a packet inventory can never claim a human-verified label, a gold "
        "evaluation, a holdout evaluation, or a generalization claim",
    )


def validate_execution_report(document: Any) -> None:
    """Exact-key structural validation for the committed execution report."""
    required = (
        "report_version",
        "benchmark_id",
        "benchmark_version",
        "mode",
        "generated_at",
        "run_id",
        "run_hash",
        "blind_run_protocol_version",
        "blind_run_protocol_hash",
        "manifest_status",
        "manifest_sha256",
        "build_source_manifest_hash",
        "detector_version",
        "workflow_version",
        "unit_grammar_version",
        "comparison_schema_version",
        "corpus_role",
        "corpus_role_detail",
        "extraction_parser_developed_using_this_corpus",
        "evaluation_contract_developed_using_this_corpus",
        "extraction_holdout_evaluation",
        "generalization_claim_supported",
        "warnings",
        "executions",
        "steps",
        "local_artifact_inventory_hash",
        "human_verified_labels",
        "gold_metrics_available",
        "gold_metrics",
        "commit_sha",
        "regenerated_by",
    )
    rfb._exact_keys(  # noqa: SLF001
        document,
        required=required,
        where="v3 execution report",
        error=V3BlindExtractionError,
        code_prefix="blind_execution_report",
    )
    rfb._require(  # noqa: SLF001
        document["human_verified_labels"] == 0
        and document["gold_metrics_available"] is False
        and document["gold_metrics"] is None
        and document["steps"]["network_requests"] == 0
        and document["steps"]["source_downloads"] == 0
        and document["steps"]["gold_evaluation_runs"] == 0,
        V3BlindExtractionError,
        "blind_execution_report_denial_violated",
        "an unlabeled execution report can never claim a human label, a gold "
        "metric, a network request, a source download, or a gold evaluation",
    )
