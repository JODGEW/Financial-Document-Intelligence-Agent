"""Validate the holdout human-review workspace and completed annotations.

    python scripts/validate_holdout_human_annotations.py --workspace
    python scripts/validate_holdout_human_annotations.py
    python scripts/validate_holdout_human_annotations.py --json

Offline and credential-free: reads only the committed holdout reports under
``benchmarks/real_filing_holdout_v1/`` and the gitignored local corpus under
``benchmark_data/real_filing_holdout_v1/``. It never contacts the network,
never opens Chroma, and never runs the detector.

Two modes over the same frozen-identity checks:

- ``--workspace``: pre-review integrity. Every review-ready packet must match
  its committed inventory hash and bind to the recorded source, section,
  parser, detector, workflow, and manifest-chain identities. Completed
  annotations are reported but not required.
- default (completed): everything ``--workspace`` checks PLUS, for every
  review-ready pair, a completed annotation at ``annotations/<pair_id>.json``
  that is explicitly ``human_verified`` with a bounded annotator id, an
  explicit-UTC verification timestamp postdating packet generation, canonical
  label ids, exactly-once closure over every previous and current unit id,
  and bounded notes carrying no filing excerpts, no absolute paths, and no
  credential material.

The review-ready pair set and the one extraction-ambiguous pair are FROZEN
constants of this validator, mirroring the committed packet inventory. The
ambiguous pair is EXCLUDED from detector-label evaluation: it must have no
packet, no template, and no annotation file at all, and it is never counted
as detector-correct, detector-incorrect, unchanged, or annotation-missing.

Unit closure is by UNIT ID, never by normalized-heading unit key: a filing
that repeats a heading (e.g. two distinct ``business-risks`` units) needs an
explicit label for EACH unit id, so key-level collapse cannot silently drop a
duplicate-heading unit from the gold corpus.

This command is strictly read-only. It never edits an annotation, never sets
a status, never selects a label, never copies a machine proposal into a human
decision field, and never computes an accuracy metric. Gold evaluation is a
separate later step (``scripts/eval_real_filing_benchmark.py``); this
validator only decides whether the human-completed files are fit to enter it.
Validator acceptance is necessary but does not itself establish that the
labels are correct.

Output is deterministic and bounded: stable check ids, stable failure codes,
fixed ordering (frozen-identity checks, then pairs in pair-id order, then
directory closure), no filing text, no absolute paths, and no raw exception
text.

Exit codes: 0 all checks pass, 1 one or more checks fail, 2 the committed
reports or manifest are unreadable, internally inconsistent, or the validator
itself failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402
import real_filing_benchmark as rfb  # noqa: E402
import real_filing_holdout as rfh  # noqa: E402

HOLDOUT_BENCHMARK_ID = "real_filing_holdout_v1"

# Frozen review contract: the committed packet inventory recorded exactly
# these nine review-ready pairs and exactly this one extraction-ambiguous
# pair. An inventory that names any other set is not the frozen holdout.
REVIEW_READY_PAIR_IDS = (
    "sic-2000s-01",
    "sic-2000s-02",
    "sic-3000s-01",
    "sic-3000s-02",
    "sic-4000s-01",
    "sic-4000s-02",
    "sic-5000s-01",
    "sic-5000s-02",
    "sic-6000s-02",
)
EXTRACTION_AMBIGUOUS_PAIR_ID = "sic-6000s-01"

# Human-decision fields. The local template carries them as null; the reviewer
# fills them. Everything else in an annotation is a frozen binding.
DOCUMENT_DECISION_FIELDS = (
    "annotation_status",
    "annotator_id",
    "verification_timestamp",
)
LABEL_DECISION_FIELDS = (
    "expected_change_type",
    "expected_evidence_side",
    "expected_direction",
    "expected_reason_code",
    "confidence",
    "reviewer_note",
)
_TEMPLATE_LABEL_BINDING_FIELDS = ("label_id", "previous_unit_id", "current_unit_id")

# A reviewer note may paraphrase, never paste. Any normalized run of this many
# characters shared with a packet excerpt is treated as a copied excerpt.
EXCERPT_COPY_WINDOW_CHARS = 40

# Bounded findings: never render more than this many ids in one detail line.
MAX_DETAIL_ITEMS = 8

# Free-text hygiene for the committed-artifact path: an annotation must not
# smuggle local filesystem locations or credential material through its only
# free-text fields. Closed, deterministic patterns — no heuristic scoring.
_ABSOLUTE_PATH_RE = re.compile(
    r"file://|[A-Za-z]:\\|(?:^|[\s\"'`(\[=])/(?:Users|home|private|var|tmp|etc|opt|srv|root)/"
)
_CREDENTIAL_RES = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._\-]{12,}"),
    re.compile(r"\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)\b\s*="),
    re.compile(r"(?i)aws_(?:access_key_id|secret_access_key|session_token)"),
)


class _Findings:
    """Ordered findings; ``ok`` is the single pass/fail signal and ``code`` is
    a stable machine-readable identifier present on every failure."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(
        self,
        *,
        check: str,
        pair_id: str | None,
        ok: bool,
        detail: str,
        code: str | None = None,
    ) -> None:
        self.rows.append(
            {
                "check": check,
                "pair_id": pair_id,
                "ok": ok,
                "code": None if ok else (code or check),
                "detail": detail,
            }
        )

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if not row["ok"]]


def _bounded(items: list[str]) -> str:
    shown = list(items[:MAX_DETAIL_ITEMS])
    extra = len(items) - len(shown)
    rendered = ", ".join(shown)
    return f"{rendered} (+{extra} more)" if extra > 0 else rendered


def _load_json(path: Path, what: str) -> dict[str, Any]:
    """Load a JSON artifact; failures report the basename and the exception
    CLASS only — never raw exception text, which can embed local paths."""
    if not path.exists():
        raise rfb.BenchmarkError(
            "holdout_report_missing", f"{what} not found: {path.name}"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise rfb.BenchmarkError(
            "holdout_report_unreadable",
            f"{what} unreadable: {path.name} ({type(exc).__name__})",
        ) from None


def _stable_result_hash(result: dict[str, Any]) -> str:
    """Timestamp-independent hash, identical to the detector's convention."""
    stable = {key: value for key, value in result.items() if key != "created_at"}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _normalized(text: str) -> str:
    return rfb.normalize_text(text).lower()


def _packet_excerpt_pool(packet: dict[str, Any]) -> list[str]:
    pool: list[str] = []
    for entry in packet.get("alignments", []):
        if not isinstance(entry, dict):
            continue
        for side in ("previous", "current"):
            excerpt = entry.get(f"{side}_excerpt")
            if excerpt:
                pool.append(_normalized(excerpt))
    return pool


def _note_copies_excerpt(note: str, excerpt_pool: list[str]) -> bool:
    normalized = _normalized(note)
    if len(normalized) < EXCERPT_COPY_WINDOW_CHARS:
        return False
    for excerpt in excerpt_pool:
        last_start = len(excerpt) - EXCERPT_COPY_WINDOW_CHARS
        for start in range(0, max(1, last_start + 1)):
            window = excerpt[start : start + EXCERPT_COPY_WINDOW_CHARS]
            if len(window) == EXCERPT_COPY_WINDOW_CHARS and window in normalized:
                return True
    return False


def _text_sensitive_reason(text: str) -> str | None:
    if _ABSOLUTE_PATH_RE.search(text):
        return "absolute_path"
    for pattern in _CREDENTIAL_RES:
        if pattern.search(text):
            return "credential_material"
    return None


def _build_unit_ids(record: dict[str, Any]) -> set[str]:
    return set(rfb.build_unit_ids(record, "previous")) | set(
        rfb.build_unit_ids(record, "current")
    )


def _parse_aware(timestamp: str) -> datetime:
    return datetime.fromisoformat(str(timestamp).strip().replace("Z", "+00:00"))


def _build_anchor(inventory: dict[str, Any]) -> str | None:
    """The manifest hash every build artifact must bind: the snapshot the
    corpus build actually ran under, as recorded by the committed inventory."""
    return inventory.get("build_source_manifest_hash") or inventory.get(
        "manifest_sha256"
    )


# --- Frozen-identity checks ---------------------------------------------------


def _check_frozen_chain(
    findings: _Findings,
    *,
    manifest: dict[str, Any],
    manifest_path: Path,
    inventory: dict[str, Any],
    blind: dict[str, Any],
    source_verification: dict[str, Any],
    selection: dict[str, Any],
) -> None:
    current_manifest_hash = rfb.sha256_file(manifest_path)
    findings.add(
        check="manifest_hash_matches_inventory",
        pair_id=None,
        ok=current_manifest_hash == inventory.get("manifest_sha256"),
        code="holdout_manifest_hash_mismatch",
        detail=(
            "committed manifest hashes to the inventory's manifest_sha256"
            if current_manifest_hash == inventory.get("manifest_sha256")
            else "committed manifest hash does not match the inventory"
        ),
    )
    chain = (
        (
            "blind report new_manifest_sha256 is the committed manifest",
            blind.get("new_manifest_sha256") == current_manifest_hash,
        ),
        (
            "blind report chains from the source-verified manifest",
            blind.get("prior_manifest_sha256")
            == source_verification.get("new_manifest_sha256"),
        ),
        (
            "source verification chains from the selection freeze",
            source_verification.get("prior_manifest_sha256")
            == selection.get("holdout_manifest_sha256"),
        ),
    )
    for label, ok in chain:
        findings.add(
            check="manifest_hash_chain",
            pair_id=None,
            ok=bool(ok),
            code="holdout_manifest_chain_broken",
            detail=label if ok else f"BROKEN: {label}",
        )

    frozen = manifest.get("frozen_parser_source_sha256")
    for name, report in (
        ("selection_report", selection),
        ("source_verification_report", source_verification),
        ("blind_extraction_report", blind),
    ):
        ok = report.get("frozen_parser_source_sha256") == frozen
        findings.add(
            check="frozen_parser_hash_consistent",
            pair_id=None,
            ok=ok,
            code="holdout_parser_hash_inconsistent",
            detail=f"{name} records the frozen parser hash"
            if ok
            else f"{name} disagrees with the manifest's frozen parser hash",
        )
    current_parser = rfh.frozen_parser_source_sha256(_REPO_ROOT)
    findings.add(
        check="frozen_parser_source_unchanged",
        pair_id=None,
        ok=current_parser == frozen,
        code="holdout_parser_source_drift",
        detail=(
            "loaders/sec_headings.py matches the frozen hash"
            if current_parser == frozen
            else "loaders/sec_headings.py has drifted from the frozen hash — "
            "annotating against a modified parser converts the holdout into "
            "development data"
        ),
    )

    findings.add(
        check="inventory_frozen_counts",
        pair_id=None,
        ok=(
            inventory.get("benchmark_id") == HOLDOUT_BENCHMARK_ID
            and inventory.get("packets_written") == len(REVIEW_READY_PAIR_IDS)
            and inventory.get("packets_blocked") == 1
            and inventory.get("human_verified_labels") == 0
        ),
        code="holdout_inventory_counts_drifted",
        detail="inventory records 9 written packets, 1 blocked, 0 human_verified",
    )

    # Build artifacts bind the manifest snapshot the corpus build ran under.
    # That snapshot must itself be chained: either the committed manifest
    # (a corpus_built regeneration, the committed case) or the blind run's
    # source-verified prior. Any other value is an unanchored identity.
    anchor = _build_anchor(inventory)
    anchor_ok = anchor in (
        inventory.get("manifest_sha256"),
        blind.get("prior_manifest_sha256"),
    )
    findings.add(
        check="build_source_hash_in_chain",
        pair_id=None,
        ok=anchor_ok,
        code="holdout_build_anchor_unchained",
        detail="build_source_manifest_hash is a verified chain hash"
        if anchor_ok
        else "build_source_manifest_hash is not a hash in the verified "
        "manifest chain",
    )


def _check_inventory_pair_set(
    findings: _Findings, *, inventory: dict[str, Any], manifest: dict[str, Any]
) -> None:
    """The pair set is frozen: exactly the nine review-ready ids, exactly the
    one ambiguous id, no duplicates, nothing outside the manifest."""
    rows = [row for row in inventory.get("pairs", []) if isinstance(row, dict)]
    ids = [str(row.get("pair_id")) for row in rows]
    duplicates = sorted({pair_id for pair_id in ids if ids.count(pair_id) > 1})
    findings.add(
        check="inventory_pair_ids_unique",
        pair_id=None,
        ok=not duplicates,
        code="holdout_inventory_duplicate_pair",
        detail="every inventory pair id appears exactly once"
        if not duplicates
        else f"duplicate inventory pair ids: {_bounded(duplicates)}",
    )

    manifest_ids = {pair["pair_id"] for pair in rfb.manifest_pairs(manifest)}
    unknown = sorted(set(ids) - manifest_ids)
    findings.add(
        check="inventory_pairs_exist_in_manifest",
        pair_id=None,
        ok=not unknown,
        code="holdout_inventory_unknown_pair",
        detail="every inventory pair id is a frozen manifest pair"
        if not unknown
        else f"inventory names pairs outside the frozen manifest: {_bounded(unknown)}",
    )

    written = sorted(
        row["pair_id"] for row in rows if row.get("packet_status") == "written"
    )
    expected = sorted(REVIEW_READY_PAIR_IDS)
    missing = sorted(set(expected) - set(written))
    unexpected = sorted(set(written) - set(expected))
    findings.add(
        check="review_ready_pair_set_frozen",
        pair_id=None,
        ok=written == expected,
        code="holdout_review_ready_set_drifted",
        detail="the nine frozen review-ready pairs are exactly the written packets"
        if written == expected
        else (
            f"review-ready set drifted — missing: {_bounded(missing) or 'none'};"
            f" unexpected: {_bounded(unexpected) or 'none'}"
        ),
    )

    blocked = sorted(
        row["pair_id"] for row in rows if row.get("packet_status") != "written"
    )
    findings.add(
        check="ambiguous_pair_set_frozen",
        pair_id=None,
        ok=blocked == [EXTRACTION_AMBIGUOUS_PAIR_ID],
        code="holdout_ambiguous_set_drifted",
        detail=f"exactly {EXTRACTION_AMBIGUOUS_PAIR_ID} is blocked "
        "(extraction_ambiguous)"
        if blocked == [EXTRACTION_AMBIGUOUS_PAIR_ID]
        else f"blocked pair set drifted: {_bounded(blocked) or 'empty'}",
    )


# --- Per-pair workspace checks ------------------------------------------------


def _check_pair_workspace(
    findings: _Findings,
    *,
    row: dict[str, Any],
    layout: rfb.CorpusLayout,
    manifest: dict[str, Any],
    inventory: dict[str, Any],
    execution_rows: dict[str, dict[str, Any]],
    execution_report: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Verify one review-ready pair. Returns (packet, build_record) when
    loadable so the completion checks can bind against them."""
    pair_id = row["pair_id"]
    inventory_manifest_hash = _build_anchor(inventory)

    packet_path = layout.packet_json_path(pair_id)
    if not packet_path.exists():
        findings.add(
            check="packet_present",
            pair_id=pair_id,
            ok=False,
            code="holdout_packet_missing",
            detail="local machine-proposed packet is missing",
        )
        return None, None
    packet = _load_json(packet_path, f"{pair_id} packet")
    packet_hash = rfb.payload_hash(packet)
    findings.add(
        check="packet_hash_matches_inventory",
        pair_id=pair_id,
        ok=packet_hash == row.get("packet_hash"),
        code="holdout_packet_hash_drift",
        detail="packet hash matches the committed inventory"
        if packet_hash == row.get("packet_hash")
        else "local packet does not hash to the committed inventory value",
    )

    bind_ok = (
        packet.get("pair_id") == pair_id
        and packet.get("benchmark_id") == HOLDOUT_BENCHMARK_ID
        and packet.get("source_manifest_hash") == inventory_manifest_hash
        and packet.get("previous", {}).get("section_hash")
        == row.get("previous_section_hash")
        and packet.get("current", {}).get("section_hash")
        == row.get("current_section_hash")
    )
    findings.add(
        check="packet_binds_manifest_and_sections",
        pair_id=pair_id,
        ok=bind_ok,
        code="holdout_packet_binding_drift",
        detail="packet binds the committed manifest hash and section hashes"
        if bind_ok
        else "packet identity fields disagree with the committed inventory",
    )

    versions = packet.get("parser_versions", {})
    for label, actual, expected, code in (
        (
            "parser",
            versions.get("html_parser"),
            manifest.get("frozen_extraction_parser_version"),
            "holdout_parser_identity_drift",
        ),
        (
            "detector",
            versions.get("detector"),
            execution_report.get("detector_version"),
            "holdout_detector_identity_drift",
        ),
        (
            "workflow",
            versions.get("workflow"),
            execution_report.get("workflow_version"),
            "holdout_workflow_identity_drift",
        ),
    ):
        findings.add(
            check=f"packet_binds_{label}_identity",
            pair_id=pair_id,
            ok=actual == expected,
            code=code,
            detail=f"packet records the frozen {label} identity"
            if actual == expected
            else f"packet {label} identity disagrees with the frozen identity",
        )

    build_path = layout.build_record_path(pair_id)
    if not build_path.exists():
        findings.add(
            check="build_record_present",
            pair_id=pair_id,
            ok=False,
            code="holdout_build_record_missing",
            detail="local build record is missing",
        )
        return packet, None
    record = _load_json(build_path, f"{pair_id} build record")
    record_ok = (
        record.get("source_manifest_hash") == inventory_manifest_hash
        and record.get("previous", {}).get("section_hash")
        == row.get("previous_section_hash")
        and record.get("current", {}).get("section_hash")
        == row.get("current_section_hash")
        and record.get("previous", {}).get("unit_count")
        == row.get("previous_unit_count")
        and record.get("current", {}).get("unit_count")
        == row.get("current_unit_count")
    )
    findings.add(
        check="build_record_binds_inventory",
        pair_id=pair_id,
        ok=record_ok,
        code="holdout_build_record_drift",
        detail="build record matches the inventory's hashes and unit counts"
        if record_ok
        else "build record disagrees with the committed inventory",
    )

    for side in ("previous", "current"):
        side_payload = record.get(side, {})
        text_path = layout.section_text_path(pair_id, side)
        if text_path.exists():
            recomputed = rfb.section_hash(text_path.read_text(encoding="utf-8"))
            ok = recomputed == side_payload.get("section_hash")
            findings.add(
                check="section_text_hash_matches",
                pair_id=pair_id,
                ok=ok,
                code="holdout_section_hash_drift",
                detail=f"{side} section text re-hashes to the recorded section hash"
                if ok
                else f"{side} section text does NOT re-hash to the recorded "
                "section hash — the local section text drifted",
            )
        else:
            findings.add(
                check="section_text_hash_matches",
                pair_id=pair_id,
                ok=False,
                code="holdout_section_text_missing",
                detail=f"missing {side} section text",
            )

    manifest_pair = rfb.find_pair(manifest, pair_id)
    for side in ("previous", "current"):
        expected = (manifest_pair or {}).get(side, {}).get("expected_sha256")
        # On disk the body keeps its official primary_document name; the build
        # record's source_name is the normalized ingestion-time alias.
        primary_document = (manifest_pair or {}).get(side, {}).get("primary_document")
        recorded = record.get(side, {}).get("source_sha256")
        source_path = (
            layout.source_file(pair_id, side, primary_document)
            if primary_document
            else None
        )
        if source_path is None or not source_path.exists():
            findings.add(
                check="source_checksum_matches_manifest",
                pair_id=pair_id,
                ok=False,
                code="holdout_source_missing",
                detail=f"missing {side} source body",
            )
            continue
        observed = rfb.sha256_file(source_path)
        ok = observed == expected and observed == recorded
        findings.add(
            check="source_checksum_matches_manifest",
            pair_id=pair_id,
            ok=ok,
            code="holdout_source_checksum_drift",
            detail=f"{side} source sha256 matches the frozen manifest digest"
            if ok
            else f"{side} source sha256 disagrees with the frozen manifest digest",
        )

    result_path = layout.build_dir(pair_id) / "detection_result.json"
    execution_row = execution_rows.get(pair_id, {})
    if result_path.exists():
        result = _load_json(result_path, f"{pair_id} detection result")
        recomputed = _stable_result_hash(result)
        ok = recomputed == execution_row.get("result_hash")
        findings.add(
            check="result_hash_matches_execution_report",
            pair_id=pair_id,
            ok=ok,
            code="holdout_result_hash_drift",
            detail="detection result re-hashes to the committed result_hash"
            if ok
            else "local detection result does not match the committed result_hash",
        )
    else:
        findings.add(
            check="result_hash_matches_execution_report",
            pair_id=pair_id,
            ok=False,
            code="holdout_detection_result_missing",
            detail="missing local detection result",
        )

    proposal_path = layout.machine_proposed_path(pair_id)
    if not proposal_path.exists():
        findings.add(
            check="machine_proposal_intact",
            pair_id=pair_id,
            ok=False,
            code="holdout_machine_proposal_missing",
            detail="missing machine proposal",
        )
    else:
        try:
            proposal = rfb.load_annotation(proposal_path)
            rfb.validate_annotation_against_build(proposal, record)
            ok = (
                proposal["annotation_status"] == rfb.ANNOTATION_MACHINE_PROPOSED
                and len(proposal["labels"]) == row.get("label_count")
            )
            findings.add(
                check="machine_proposal_intact",
                pair_id=pair_id,
                ok=ok,
                code="holdout_machine_proposal_drift",
                detail="machine proposal is intact, machine_proposed, and bound "
                "to this build"
                if ok
                else "machine proposal status or label count drifted",
            )
        except rfb.BenchmarkError as exc:
            findings.add(
                check="machine_proposal_intact",
                pair_id=pair_id,
                ok=False,
                code="holdout_machine_proposal_invalid",
                detail=f"machine proposal invalid [{exc.code}]",
            )

    return packet, record


def _check_blocked_pair(
    findings: _Findings, *, row: dict[str, Any], layout: rfb.CorpusLayout
) -> None:
    """The ambiguous pair is excluded, and nothing may quietly re-admit it.

    It contributes NOTHING to detector-label evaluation: it is not counted as
    detector-correct, detector-incorrect, unchanged, or annotation-missing.
    """
    pair_id = row["pair_id"]
    ok = (
        row.get("packet_status") == "blocked"
        and row.get("review_ready") is False
        and row.get("packet_hash") is None
        and row.get("label_count") == 0
    )
    findings.add(
        check="ambiguous_pair_blocked_in_inventory",
        pair_id=pair_id,
        ok=ok,
        code="holdout_ambiguous_row_invalid",
        detail=(
            f"{pair_id} is blocked ({row.get('blocking_reason')}); it is "
            "reported as extraction_ambiguous and EXCLUDED from detector-label "
            "evaluation — it is never counted as detector-correct, "
            "detector-incorrect, unchanged, or annotation-missing"
        )
        if ok
        else f"{pair_id} inventory row is not a proper blocked row",
    )
    forbidden = [
        layout.packet_json_path(pair_id),
        layout.packet_markdown_path(pair_id),
        layout.machine_proposed_path(pair_id),
        layout.annotation_path(pair_id),
    ]
    present = sorted(path.name for path in forbidden if path.exists())
    findings.add(
        check="ambiguous_pair_has_no_annotation_surface",
        pair_id=pair_id,
        ok=not present,
        code="holdout_ambiguous_pair_annotation_forbidden",
        detail="no packet, template, or annotation file exists for the "
        "ambiguous pair"
        if not present
        else f"forbidden files exist for the ambiguous pair: {_bounded(present)}",
    )


def _check_annotation_dir_closed(
    findings: _Findings, *, layout: rfb.CorpusLayout
) -> None:
    allowed = {
        f"{pair_id}.machine_proposed.json" for pair_id in REVIEW_READY_PAIR_IDS
    } | {f"{pair_id}.json" for pair_id in REVIEW_READY_PAIR_IDS}
    annotations_dir = layout.annotations_dir()
    unexpected = (
        sorted(
            entry.name
            for entry in annotations_dir.iterdir()
            if entry.is_file() and entry.name not in allowed
        )
        if annotations_dir.exists()
        else []
    )
    findings.add(
        check="annotation_directory_closed",
        pair_id=None,
        ok=not unexpected,
        code="holdout_unexpected_annotation_file",
        detail="annotations/ holds only the nine proposals and nine "
        "completion files"
        if not unexpected
        else f"unexpected annotation files: {_bounded(unexpected)}",
    )


# --- Completed-annotation checks ----------------------------------------------


def _check_template(
    findings: _Findings,
    *,
    raw: dict[str, Any],
    row: dict[str, Any],
    inventory: dict[str, Any],
    record: dict[str, Any] | None,
    require_completed: bool,
) -> None:
    """An untouched human-completion template: exact annotation keys, bindings
    intact, every decision field empty. Never an error in --workspace mode;
    always incomplete in completed mode."""
    pair_id = row["pair_id"]
    expected_keys = set(rfb._ANNOTATION_REQUIRED) | set(rfb._ANNOTATION_OPTIONAL)
    unexpected_keys = sorted(set(raw) - expected_keys)
    missing_keys = sorted(set(rfb._ANNOTATION_REQUIRED) - set(raw))
    label_key_defects: list[str] = []
    for index, label in enumerate(raw.get("labels", [])):
        if not isinstance(label, dict):
            label_key_defects.append(f"labels[{index}]")
            continue
        if set(label) != set(rfb._LABEL_REQUIRED):
            label_key_defects.append(f"labels[{index}]")
    findings.add(
        check="template_keys_exact",
        pair_id=pair_id,
        ok=not unexpected_keys and not missing_keys and not label_key_defects,
        code="holdout_template_keys_invalid",
        detail="template carries exactly the annotation schema keys"
        if not unexpected_keys and not missing_keys and not label_key_defects
        else (
            f"template keys drifted — unexpected: {_bounded(unexpected_keys) or 'none'};"
            f" missing: {_bounded(missing_keys) or 'none'};"
            f" labels: {_bounded(label_key_defects) or 'ok'}"
        ),
    )

    decisions_filled = sorted(
        {
            field
            for label in raw.get("labels", [])
            if isinstance(label, dict)
            for field in LABEL_DECISION_FIELDS
            if label.get(field) is not None
        }
    )
    template_ok = (
        raw.get("benchmark_id") == HOLDOUT_BENCHMARK_ID
        and raw.get("pair_id") == pair_id
        and raw.get("annotator_id") is None
        and raw.get("verification_timestamp") is None
        and raw.get("source_manifest_hash") == _build_anchor(inventory)
        and raw.get("previous_section_hash") == row.get("previous_section_hash")
        and raw.get("current_section_hash") == row.get("current_section_hash")
        and not decisions_filled
    )
    findings.add(
        check="template_empty_and_bound",
        pair_id=pair_id,
        ok=template_ok,
        code="holdout_template_corrupt",
        detail="empty human-completion template: bindings intact, no "
        "decision fields filled"
        if template_ok
        else "template is corrupt: bindings drifted or decision fields "
        f"pre-filled ({_bounded(decisions_filled) or 'bindings'})",
    )

    if record is not None:
        referenced = {
            label.get(field)
            for label in raw.get("labels", [])
            if isinstance(label, dict)
            for field in ("previous_unit_id", "current_unit_id")
            if label.get(field) is not None
        }
        build_units = _build_unit_ids(record)
        extra = sorted(referenced - build_units)
        uncovered = sorted(build_units - referenced)
        findings.add(
            check="template_units_known",
            pair_id=pair_id,
            ok=not extra,
            code="holdout_template_unknown_unit",
            detail="every template unit reference exists in the build"
            if not extra
            else f"template references units outside the build: {_bounded(extra)}",
        )
        if uncovered:
            # The frozen machine proposal aligned by unit_key, so a filing
            # that repeats a normalized heading leaves the duplicate units
            # unreferenced. Choosing how they bind is a HUMAN decision: the
            # reviewer must ADD labels until unit closure passes on the
            # completed file. Not a workspace defect.
            findings.add(
                check="template_uncovered_units_pending_human_labels",
                pair_id=pair_id,
                ok=True,
                detail="reviewer must add labels covering: " + _bounded(uncovered),
            )

    if require_completed:
        findings.add(
            check="completed_annotation_human_verified",
            pair_id=pair_id,
            ok=False,
            code="holdout_annotation_not_completed",
            detail="still an empty template — not human_verified",
        )


def _check_completed_annotation(
    findings: _Findings,
    *,
    row: dict[str, Any],
    layout: rfb.CorpusLayout,
    inventory: dict[str, Any],
    packet: dict[str, Any] | None,
    record: dict[str, Any] | None,
    require_completed: bool,
) -> None:
    pair_id = row["pair_id"]
    path = layout.annotation_path(pair_id)
    if not path.exists():
        findings.add(
            check="completed_annotation_human_verified",
            pair_id=pair_id,
            ok=not require_completed,
            code="holdout_annotation_not_completed",
            detail="awaiting human completion"
            if not require_completed
            else "no completed annotation file exists",
        )
        return

    raw = _load_json(path, f"{pair_id} completed annotation")
    if raw.get("annotation_status") is None:
        _check_template(
            findings,
            raw=raw,
            row=row,
            inventory=inventory,
            record=record,
            require_completed=require_completed,
        )
        return

    # A status was chosen: the file must now pass the full annotation schema
    # (exact keys, bounded annotator id, ISO timestamp, duplicate-label and
    # note bounds all enforced by the shared schema validator).
    try:
        annotation = rfb.load_annotation(path)
    except rfb.BenchmarkError as exc:
        findings.add(
            check="completed_annotation_schema",
            pair_id=pair_id,
            ok=False,
            code=f"holdout_annotation_schema:{exc.code}",
            detail=f"invalid annotation [{exc.code}]",
        )
        return
    findings.add(
        check="completed_annotation_schema",
        pair_id=pair_id,
        ok=True,
        detail="schema valid: exact keys, bounded annotator id, no duplicate "
        "labels, bounded notes",
    )

    status = annotation["annotation_status"]
    if status == rfb.ANNOTATION_MACHINE_PROPOSED:
        findings.add(
            check="no_machine_proposed_status_remains",
            pair_id=pair_id,
            ok=False,
            code="holdout_machine_proposed_status_remains",
            detail="completion file still carries machine_proposed status — a "
            "machine proposal was copied instead of reviewed",
        )
        return

    verified = status == rfb.ANNOTATION_HUMAN_VERIFIED
    findings.add(
        check="completed_annotation_human_verified",
        pair_id=pair_id,
        ok=verified if require_completed else True,
        code="holdout_annotation_not_human_verified",
        detail=f"annotation_status={status!r}"
        + ("" if verified else " (not human_verified)"),
    )

    timestamp = annotation.get("verification_timestamp")
    if timestamp is not None:
        moment = _parse_aware(timestamp)
        findings.add(
            check="verification_timestamp_explicit_utc",
            pair_id=pair_id,
            ok=moment.utcoffset() == timedelta(0),
            code="holdout_timestamp_not_utc",
            detail="verification timestamp carries an explicit UTC offset"
            if moment.utcoffset() == timedelta(0)
            else "verification timestamp must carry a +00:00 offset",
        )
        after_packets = moment > _parse_aware(inventory["generated_at"])
        findings.add(
            check="verification_timestamp_postdates_packets",
            pair_id=pair_id,
            ok=after_packets,
            code="holdout_timestamp_precedes_packets",
            detail="verification timestamp postdates packet generation"
            if after_packets
            else "verification timestamp predates packet generation — nothing "
            "existed to verify at that instant",
        )

    ok = annotation.get("benchmark_id") == HOLDOUT_BENCHMARK_ID and annotation.get(
        "source_manifest_hash"
    ) == _build_anchor(inventory)
    findings.add(
        check="completed_annotation_binds_manifest",
        pair_id=pair_id,
        ok=ok,
        code="holdout_annotation_manifest_drift",
        detail="annotation binds the committed manifest hash"
        if ok
        else "annotation manifest binding drifted",
    )

    if record is None:
        return
    try:
        rfb.validate_annotation_against_build(annotation, record)
        findings.add(
            check="section_hashes_and_units_match_build",
            pair_id=pair_id,
            ok=True,
            detail="section hashes match and every label references a known unit",
        )
    except rfb.BenchmarkError as exc:
        findings.add(
            check="section_hashes_and_units_match_build",
            pair_id=pair_id,
            ok=False,
            code=f"holdout_annotation_build_binding:{exc.code}",
            detail=f"build binding failed [{exc.code}]",
        )

    # Exactly-once closure by UNIT ID. Key-level coverage is not enough: a
    # filing that repeats a normalized heading has distinct unit ids sharing a
    # unit key, and each id needs its own explicit label.
    reference_counts: Counter[str] = Counter(
        label[field]
        for label in annotation["labels"]
        for field in ("previous_unit_id", "current_unit_id")
        if label[field] is not None
    )
    build_units = _build_unit_ids(record)
    uncovered = sorted(unit for unit in build_units if reference_counts[unit] == 0)
    findings.add(
        check="unit_inventory_closed",
        pair_id=pair_id,
        ok=not uncovered,
        code="holdout_unit_uncovered",
        detail="every previous and current unit id is covered by a label"
        if not uncovered
        else f"units with no label: {_bounded(uncovered)}",
    )
    multiply = sorted(
        unit
        for unit, count in reference_counts.items()
        if unit in build_units and count > 1
    )
    findings.add(
        check="unit_covered_exactly_once",
        pair_id=pair_id,
        ok=not multiply,
        code="holdout_unit_multiply_covered",
        detail="no unit id is claimed by more than one label"
        if not multiply
        else f"units claimed by multiple labels: {_bounded(multiply)}",
    )

    canonical = all(
        label["label_id"]
        == rfb.label_id_for(pair_id, label["previous_unit_id"], label["current_unit_id"])
        for label in annotation["labels"]
    )
    findings.add(
        check="label_ids_canonical",
        pair_id=pair_id,
        ok=canonical,
        code="holdout_label_id_not_canonical",
        detail="every label_id is the canonical id for its unit binding"
        if canonical
        else "a label_id does not derive from its unit binding",
    )

    excerpt_pool = _packet_excerpt_pool(packet or {})
    copied = sorted(
        label["label_id"]
        for label in annotation["labels"]
        if label["reviewer_note"]
        and _note_copies_excerpt(label["reviewer_note"], excerpt_pool)
    )
    findings.add(
        check="no_filing_excerpts_in_annotation",
        pair_id=pair_id,
        ok=not copied,
        code="holdout_note_copies_excerpt",
        detail="reviewer notes are bounded and copy no filing excerpts"
        if not copied
        else f"reviewer notes copy packet excerpts on labels: {_bounded(copied)}",
    )

    sensitive: list[str] = []
    annotator = annotation.get("annotator_id")
    if isinstance(annotator, str) and _text_sensitive_reason(annotator):
        sensitive.append("annotator_id")
    for label in annotation["labels"]:
        note = label["reviewer_note"]
        if isinstance(note, str) and _text_sensitive_reason(note):
            sensitive.append(label["label_id"])
    findings.add(
        check="no_sensitive_material_in_annotation",
        pair_id=pair_id,
        ok=not sensitive,
        code="holdout_annotation_sensitive_material",
        detail="no absolute paths or credential material in free-text fields"
        if not sensitive
        else "absolute paths or credential material found in: "
        + _bounded(sorted(sensitive)),
    )


# --- Entry points -------------------------------------------------------------


def run_validation(
    *,
    layout: rfb.CorpusLayout,
    manifest_path: Path,
    report_dir: Path,
    require_completed: bool,
) -> _Findings:
    manifest = rfh.load_holdout_manifest(manifest_path)
    inventory = _load_json(
        report_dir / "annotation_packet_inventory.json", "packet inventory"
    )
    blind = _load_json(
        report_dir / "blind_extraction_report.json", "blind extraction report"
    )
    source_verification = _load_json(
        report_dir / "source_verification_report.json", "source verification report"
    )
    selection = _load_json(report_dir / "selection_report.json", "selection report")
    execution_report = _load_json(
        report_dir / "execution_report.json", "execution report"
    )
    execution_rows = {
        row["pair_id"]: row
        for row in execution_report.get("executions", [])
        if isinstance(row, dict)
    }

    findings = _Findings()
    _check_frozen_chain(
        findings,
        manifest=manifest,
        manifest_path=manifest_path,
        inventory=inventory,
        blind=blind,
        source_verification=source_verification,
        selection=selection,
    )
    _check_inventory_pair_set(findings, inventory=inventory, manifest=manifest)

    for row in sorted(
        (row for row in inventory.get("pairs", []) if isinstance(row, dict)),
        key=lambda item: str(item.get("pair_id")),
    ):
        if row.get("packet_status") == "written":
            packet, record = _check_pair_workspace(
                findings,
                row=row,
                layout=layout,
                manifest=manifest,
                inventory=inventory,
                execution_rows=execution_rows,
                execution_report=execution_report,
            )
            _check_completed_annotation(
                findings,
                row=row,
                layout=layout,
                inventory=inventory,
                packet=packet,
                record=record,
                require_completed=require_completed,
            )
        else:
            _check_blocked_pair(findings, row=row, layout=layout)

    _check_annotation_dir_closed(findings, layout=layout)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the holdout human-review workspace (--workspace) or the "
            "human-completed annotations (default). Offline; never edits, "
            "never scores."
        )
    )
    parser.add_argument(
        "--workspace",
        action="store_true",
        help="pre-review integrity only; completed annotations not required",
    )
    parser.add_argument("--manifest", metavar="PATH", help="holdout manifest path")
    parser.add_argument(
        "--corpus-dir", metavar="PATH", help="local holdout corpus directory"
    )
    parser.add_argument(
        "--report-dir",
        metavar="PATH",
        help="directory of the committed holdout reports (default: beside the "
        "manifest)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest or rfh.default_holdout_manifest_path())
    layout = rfb.CorpusLayout(args.corpus_dir or config.REAL_FILING_HOLDOUT_DIR)
    report_dir = Path(args.report_dir or manifest_path.parent)

    try:
        findings = run_validation(
            layout=layout,
            manifest_path=manifest_path,
            report_dir=report_dir,
            require_completed=not args.workspace,
        )
    except rfb.BenchmarkError as exc:
        print(f"Validation refused [{exc.code}]: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — never leak raw exception text
        print(
            "Validation failed [holdout_validator_internal_error]: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return 2

    failed = findings.failed
    if args.json:
        print(
            json.dumps(
                {
                    "mode": "workspace" if args.workspace else "completed",
                    "checks": findings.rows,
                    "failed_count": len(failed),
                    "failed_codes": sorted(
                        {row["code"] for row in failed if row["code"]}
                    ),
                    "passed": not failed,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1 if failed else 0

    mode = "workspace integrity" if args.workspace else "completed annotations"
    print(f"Holdout human-annotation validation — {mode}")
    for row in findings.rows:
        marker = "ok  " if row["ok"] else "FAIL"
        scope = row["pair_id"] or "-"
        print(f"  [{marker}] {scope:<16} {row['check']}: {row['detail']}")
    print(
        f"\n  {len(findings.rows) - len(failed)}/{len(findings.rows)} checks "
        f"passed; {len(failed)} failed."
    )
    if failed:
        print(
            "  This command changes nothing. Fix the files above and rerun; "
            "only 'human_verified' annotations can ever enter a gold metric."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
