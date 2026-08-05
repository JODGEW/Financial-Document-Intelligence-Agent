"""Human-review preparation and admission contract for the v3 extraction holdout.

Offline, credential-free, and strictly non-semantic. This module prepares the
local review workspace for ``real_filing_v3_holdout_v1`` and decides whether
human-completed annotations are fit to enter a future gold evaluation. It never
decides a label.

The boundary this module exists to hold
---------------------------------------

A machine proposal is an INPUT to review, never ground truth. Nothing here may
turn one into gold:

- it never writes ``annotation_status``, ``annotator_id``, or
  ``verification_timestamp`` — the three fields that constitute admission;
- it never writes any label decision field (change type, evidence side,
  direction, reason code, confidence, reviewer note);
- it never writes a reviewer decision into a review record;
- it never calls a model, an embedding service, a heuristic classifier, or the
  gold evaluator, and it computes no metric and no machine/human agreement;
- it uses no wall clock, so it cannot mint a verification timestamp even by
  accident, and its outputs are a deterministic function of the workspace;
- it reads no environment variable, no git identity, and no prior corpus's
  annotations, so reviewer identity cannot be inferred from anything.

Three artifacts per reviewable pair
-----------------------------------

1. ``annotations/<pair_id>.machine_proposed.json`` — the frozen machine
   proposal written by the blind run. Immutable. Its evaluated-content hash is
   pinned by the committed packet inventory and its raw bytes are additionally
   pinned by the local review record, so any edit is detected.
2. ``annotations/<pair_id>.json`` — the HUMAN annotation. Prepared as an EMPTY
   template: unit bindings and frozen hashes are carried over, every decision
   field is ``null``. A reviewer who agrees with a machine proposal must still
   type the value; leaving a file alone is never approval.
3. ``review/<pair_id>.review.json`` — the review record. It carries the
   provenance the annotation schema has no room for (packet hash, machine
   proposal hashes, comparison result hash, canonical unit identities) plus one
   row per canonical subject where the reviewer records an EXPLICIT decision.
   The frozen annotation schema is not modified to make room for any of this.

Canonical subjects
------------------

A subject is the exact ``(previous_unit_id, current_unit_id)`` pair of canonical
``side:sequence:unit_key`` identities a label binds — the same identity the
contract-v2 evaluator matches on. A filing that repeats a normalized heading
produces distinct occurrences that share a unit key; each occurrence is its own
subject, gets its own row, and needs its own decision. Unit keys alone are never
subjects, and occurrences are never collapsed or renumbered.

Blocked pairs
-------------

A pair whose Item 1A was not extracted on both sides has no packet, no machine
proposal, no template, and no review record. It stays in the corpus with its
stable blocking reason and is never given a fabricated label to make a count
come out even.

Admission is necessary, not sufficient
--------------------------------------

Passing this gate proves identity, closure, shape, and hygiene. It does not
establish that a reviewer's judgement is correct, and it produces no accuracy
or generalization claim.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import real_filing_benchmark as rfb
import real_filing_v3_holdout as rfv3

BENCHMARK_ID = rfv3.V3_HOLDOUT_BENCHMARK_ID

#: Versions of the artifacts this module owns. They are NOT the frozen
#: annotation schema/protocol versions, which this module never changes.
REVIEW_QUEUE_VERSION = "real-filing-v3-holdout.review-queue.v1"
REVIEW_RECORD_VERSION = "real-filing-v3-holdout.human-review-record.v1"
PREPARATION_TOOL_VERSION = "real_filing_v3_human_review.v1"
HUMAN_TEMPLATE_GENERATED_BY = "real_filing_v3_human_completion_template.v1"

#: Review-queue statuses. ``pending`` is the initial state of a prepared pair
#: and asserts nothing about the reviewer's opinion. ``admitted``/``rejected``
#: are DERIVED from the human annotation's own status — never set by a tool.
REVIEW_STATUS_BLOCKED = "blocked"
REVIEW_STATUS_PENDING = "pending"
REVIEW_STATUS_IN_REVIEW = "in_review"
REVIEW_STATUS_REVIEWER_COMPLETED = "reviewer_completed"
REVIEW_STATUS_ADMITTED = "admitted"
REVIEW_STATUS_REJECTED = "rejected"
REVIEW_STATUSES = (
    REVIEW_STATUS_BLOCKED,
    REVIEW_STATUS_PENDING,
    REVIEW_STATUS_IN_REVIEW,
    REVIEW_STATUS_REVIEWER_COMPLETED,
    REVIEW_STATUS_ADMITTED,
    REVIEW_STATUS_REJECTED,
)

#: The four explicit per-subject actions a reviewer may record. There is no
#: fifth value meaning "left alone": omission is not a decision.
DECISION_RETAINED = "retained"
DECISION_CHANGED = "changed"
DECISION_UNDETERMINED = "undetermined"
DECISION_REJECTED_MALFORMED = "rejected_malformed"
REVIEWER_DECISIONS = (
    DECISION_RETAINED,
    DECISION_CHANGED,
    DECISION_UNDETERMINED,
    DECISION_REJECTED_MALFORMED,
)

#: Fields only a human may fill. No code path in this module writes any of
#: them; ``tests`` assert that at the source level.
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
#: Carried into the template from the machine proposal so unit closure is
#: reachable. The BINDING is itself a machine proposal the reviewer confirms,
#: changes, or replaces; it is not a decision this module makes.
TEMPLATE_LABEL_BINDING_FIELDS = ("label_id", "previous_unit_id", "current_unit_id")

#: The blocking reasons the committed inventory may carry. A row naming any
#: other reason is not the frozen corpus.
KNOWN_BLOCKING_REASONS = (
    "item_1a_not_extracted_for_both_sides",
    "comparison_not_detected",
)

#: A closed, deterministic placeholder denylist for ``annotator_id``. Reviewer
#: identity is self-asserted local metadata, so this cannot verify a person —
#: it only refuses values that assert nobody. Matching is on the casefolded,
#: whitespace-collapsed value, with no fuzzy scoring.
PLACEHOLDER_ANNOTATOR_IDS = (
    "-",
    "?",
    "annotator",
    "anonymous",
    "changeme",
    "change-me",
    "example",
    "fixme",
    "human",
    "me",
    "n/a",
    "na",
    "name",
    "none",
    "null",
    "placeholder",
    "reviewer",
    "someone",
    "tbd",
    "test",
    "todo",
    "unknown",
    "user",
    "x",
    "xxx",
    "your name",
    "your-name",
    "yourname",
)

#: A reviewer note may paraphrase, never paste. Any normalized run of this many
#: characters shared with a packet excerpt is treated as a copied excerpt.
EXCERPT_COPY_WINDOW_CHARS = 40

#: Bounded findings: never render more than this many ids in one detail line.
MAX_DETAIL_ITEMS = 8

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


class HumanReviewError(rfb.BenchmarkError):
    """Refusal to prepare, read, or validate the review workspace."""


# --- Workspace layout ---------------------------------------------------------
# `rfb.CorpusLayout` is a frozen artifact of the corpus build and is not
# modified. The review tree is an additive sibling of `annotations/`.


def review_dir(layout: rfb.CorpusLayout) -> Path:
    return layout.root / "review"


def review_queue_path(layout: rfb.CorpusLayout) -> Path:
    return review_dir(layout) / "queue.json"


def review_record_path(layout: rfb.CorpusLayout, pair_id: str) -> Path:
    return review_dir(layout) / f"{pair_id}.review.json"


def human_review_path(layout: rfb.CorpusLayout, pair_id: str) -> Path:
    """The human annotation, deliberately OUTSIDE ``annotations/``.

    The blind runner's frozen precondition refuses any ``annotations/*.json``
    whose status is not ``machine_proposed`` — a rule that exists so a human
    decision can never be an input to a blind run, and one this phase does not
    modify. An empty human template carries a null status, so keeping it in
    ``annotations/`` would permanently break that gate on a prepared
    workspace. ``annotations/`` therefore holds machine proposals only, and the
    human artifact lives beside its review record with its own identity, its
    own hash, and no way to overwrite the proposal.
    """
    return review_dir(layout) / f"{pair_id}.human_review.json"


# --- Findings -----------------------------------------------------------------


class Findings:
    """Ordered findings; ``ok`` is the single pass/fail signal and ``code`` is a
    stable machine-readable identifier present on every failure."""

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
                "ok": bool(ok),
                "code": None if ok else (code or check),
                "detail": detail,
            }
        )

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if not row["ok"]]

    @property
    def failed_codes(self) -> list[str]:
        return sorted({row["code"] for row in self.failed if row["code"]})


def bounded(items: Sequence[str]) -> str:
    shown = list(items[:MAX_DETAIL_ITEMS])
    extra = len(items) - len(shown)
    rendered = ", ".join(shown)
    return f"{rendered} (+{extra} more)" if extra > 0 else rendered


# --- Loading ------------------------------------------------------------------


def load_json(path: Path, what: str) -> dict[str, Any]:
    """Load a JSON artifact. Failures report the basename and the exception
    CLASS only — never raw exception text, which can embed local paths."""
    if not path.exists():
        raise HumanReviewError(
            "v3_review_artifact_missing", f"{what} not found: {path.name}"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HumanReviewError(
            "v3_review_artifact_unreadable",
            f"{what} unreadable: {path.name} ({type(exc).__name__})",
        ) from None
    if not isinstance(document, dict):
        raise HumanReviewError(
            "v3_review_artifact_unreadable",
            f"{what} is not a JSON object: {path.name}",
        )
    return document


def load_committed_artifacts(
    manifest_path: Path, report_dir: Path
) -> dict[str, Any]:
    """The committed v3 corpus record: manifest plus the five bounded reports."""
    return {
        "manifest": rfv3.load_v3_holdout_manifest(manifest_path),
        "manifest_path": manifest_path,
        "inventory": load_json(
            report_dir / "annotation_packet_inventory.json", "packet inventory"
        ),
        "blind": load_json(
            report_dir / "blind_extraction_report.json", "blind extraction report"
        ),
        "execution": load_json(
            report_dir / "execution_report.json", "execution report"
        ),
        "source_verification": load_json(
            report_dir / "source_verification_report.json",
            "source verification report",
        ),
        "selection": load_json(report_dir / "selection_report.json", "selection report"),
        "evaluation_config": load_json(
            report_dir / "evaluation_config.json", "evaluation config"
        ),
    }


def inventory_rows(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Committed inventory pair rows, in committed order. Review ordering is
    this order and nothing else — never label count, issuer, change type, or
    perceived difficulty."""
    return [row for row in inventory.get("pairs", []) if isinstance(row, dict)]


def review_ready_rows(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [row for row in inventory_rows(inventory) if row.get("packet_status") == "written"]


def blocked_rows(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [row for row in inventory_rows(inventory) if row.get("packet_status") != "written"]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stable_result_hash(result: Mapping[str, Any]) -> str:
    """Timestamp-independent hash, identical to the detector's convention."""
    stable = {key: value for key, value in result.items() if key != "created_at"}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True).encode("utf-8")
    ).hexdigest()


# --- Canonical subjects -------------------------------------------------------


def canonical_unit_inventory(
    build_record: Mapping[str, Any]
) -> tuple[dict[str, tuple[str, int, str]], list[str]]:
    """The pair's closed canonical-unit inventory, from the build record.

    Mirrors the contract-v2 evaluator's admission rule without importing it: a
    unit is admitted only when its declared ``unit_id`` equals
    ``rfb.unit_id(side, index, unit_key)`` for its own recorded position. A
    record whose id disagrees with its own metadata is invalid input, not a
    unit. Repeated normalized headings stay separate because the sequence is
    part of the identity.
    """
    inventory: dict[str, tuple[str, int, str]] = {}
    problems: list[str] = []
    for side in ("previous", "current"):
        for index, unit in enumerate(build_record.get(side, {}).get("units", [])):
            declared = unit.get("unit_id")
            canonical = rfb.unit_id(side, index, unit.get("unit_key", ""))
            if declared != canonical:
                problems.append(f"{side}[{index}]")
                continue
            inventory[declared] = (side, index, unit["unit_key"])
    return inventory, problems


def subject_key(label: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """The canonical subject a label binds. Label id and ordering play no part."""
    return (label.get("previous_unit_id"), label.get("current_unit_id"))


def subject_display(key: tuple[str | None, str | None]) -> str:
    return f"({key[0] or '-'} -> {key[1] or '-'})"


def check_subject_closure(
    labels: Iterable[Mapping[str, Any]],
    inventory: Mapping[str, tuple[str, int, str]],
) -> dict[str, list[str]]:
    """Exactly-once closure by canonical unit IDENTITY, never by unit key.

    Returns the defect sets; an empty value for every key is a closed set.
    """
    coverage: Counter[str] = Counter()
    seen: set[tuple[str | None, str | None]] = set()
    duplicate_subjects: list[str] = []
    unknown: list[str] = []
    key_only: list[str] = []
    side_mismatch: list[str] = []
    for label in labels:
        key = subject_key(label)
        if key in seen:
            duplicate_subjects.append(subject_display(key))
        seen.add(key)
        for field, expected_side in (
            ("previous_unit_id", "previous"),
            ("current_unit_id", "current"),
        ):
            value = label.get(field)
            if value is None:
                continue
            coverage[value] += 1
            if value in inventory:
                if inventory[value][0] != expected_side:
                    side_mismatch.append(value)
                continue
            parts = str(value).split(":", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                key_only.append(str(value))
            elif parts[0] != expected_side:
                side_mismatch.append(str(value))
            else:
                unknown.append(str(value))
    return {
        "uncovered": sorted(unit for unit in inventory if coverage[unit] == 0),
        "multiply_covered": sorted(
            unit for unit, count in coverage.items() if unit in inventory and count > 1
        ),
        "duplicate_subjects": sorted(set(duplicate_subjects)),
        "unknown_units": sorted(set(unknown)),
        "unit_key_only": sorted(set(key_only)),
        "side_mismatch": sorted(set(side_mismatch)),
    }


def canonical_subject_rows(
    proposal: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """One review row per canonical subject the machine proposal binds.

    Machine values are carried as clearly-named ``machine_proposed_*`` fields —
    context for the reviewer, never a recommendation and never a default. Rows
    keep proposal order so packet and comparison ordering survive review.
    """
    rows: list[dict[str, Any]] = []
    for index, label in enumerate(proposal.get("labels", [])):
        rows.append(
            {
                "subject_index": index,
                "label_id": label["label_id"],
                "previous_unit_id": label["previous_unit_id"],
                "current_unit_id": label["current_unit_id"],
                "machine_proposed_change_type": label["expected_change_type"],
                "machine_proposed_evidence_side": label["expected_evidence_side"],
                "machine_proposed_direction": label["expected_direction"],
                "machine_proposed_reason_code": label["expected_reason_code"],
                "machine_proposed_confidence": label["confidence"],
                "reviewer_decision": None,
                "reviewer_decision_note": None,
            }
        )
    return rows


_REVIEW_RECORD_KEYS = (
    "review_record_version",
    "prepared_by",
    "benchmark_id",
    "pair_id",
    "annotation_schema_version",
    "annotation_protocol_version",
    "unit_identity_contract",
    "packet_relative_path",
    "packet_sha256",
    "machine_proposal_relative_path",
    "machine_proposal_annotation_sha256",
    "machine_proposal_file_sha256",
    "human_review_relative_path",
    "comparison_result_hash",
    "source_manifest_hash",
    "previous_section_hash",
    "current_section_hash",
    "previous_source_sha256",
    "current_source_sha256",
    "canonical_unit_ids",
    "proposed_label_count",
    "reviewer_completed",
    "subjects",
)

_SUBJECT_KEYS = (
    "subject_index",
    "label_id",
    "previous_unit_id",
    "current_unit_id",
    "machine_proposed_change_type",
    "machine_proposed_evidence_side",
    "machine_proposed_direction",
    "machine_proposed_reason_code",
    "machine_proposed_confidence",
    "reviewer_decision",
    "reviewer_decision_note",
)

_QUEUE_ENTRY_KEYS = (
    "review_position",
    "pair_id",
    "review_status",
    "blocking_reason",
    "packet_relative_path",
    "packet_sha256",
    "machine_annotation_relative_path",
    "machine_annotation_sha256",
    "machine_annotation_file_sha256",
    "human_review_relative_path",
    "review_record_relative_path",
    "comparison_result_hash",
    "previous_section_hash",
    "current_section_hash",
    "previous_source_sha256",
    "current_source_sha256",
    "proposed_label_count",
    "canonical_unit_id_count",
    "subjects_decided",
)


# --- Text hygiene -------------------------------------------------------------


def _normalized(text: str) -> str:
    return rfb.normalize_text(text).lower()


def packet_excerpt_pool(packet: Mapping[str, Any]) -> list[str]:
    pool: list[str] = []
    for entry in packet.get("alignments", []):
        if not isinstance(entry, dict):
            continue
        for side in ("previous", "current"):
            excerpt = entry.get(f"{side}_excerpt")
            if excerpt:
                pool.append(_normalized(excerpt))
    for side in ("previous", "current"):
        for unit in packet.get(side, {}).get("units", []):
            if isinstance(unit, dict) and unit.get("excerpt"):
                pool.append(_normalized(unit["excerpt"]))
    return pool


def note_copies_excerpt(note: str, excerpt_pool: Sequence[str]) -> bool:
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


def text_sensitive_reason(text: str) -> str | None:
    if _ABSOLUTE_PATH_RE.search(text):
        return "absolute_path"
    for pattern in _CREDENTIAL_RES:
        if pattern.search(text):
            return "credential_material"
    return None


def is_placeholder_annotator(value: Any) -> bool:
    """A closed, deterministic refusal of ids that assert nobody."""
    if not isinstance(value, str):
        return True
    collapsed = " ".join(value.split()).casefold()
    if not collapsed:
        return True
    if not any(character.isalnum() for character in collapsed):
        return True
    return collapsed in PLACEHOLDER_ANNOTATOR_IDS


def parse_aware(timestamp: Any) -> datetime | None:
    """Parse an ISO timestamp, or return None. Never falls back to a clock."""
    if not isinstance(timestamp, str):
        return None
    try:
        return datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def is_explicit_utc(moment: datetime | None) -> bool:
    """Naive timestamps and non-UTC offsets are both refused: an admission
    record whose instant depends on the reader's locale is not a record."""
    return moment is not None and moment.utcoffset() == timedelta(0)


# --- Frozen-identity checks ---------------------------------------------------


def check_frozen_chain(findings: Findings, artifacts: Mapping[str, Any]) -> None:
    """Everything the review workspace inherits must still be the frozen thing."""
    manifest = artifacts["manifest"]
    inventory = artifacts["inventory"]
    blind = artifacts["blind"]
    execution = artifacts["execution"]
    source_verification = artifacts["source_verification"]
    selection = artifacts["selection"]
    evaluation_config = artifacts["evaluation_config"]

    manifest_hash = rfb.sha256_file(artifacts["manifest_path"])
    findings.add(
        check="manifest_hash_matches_inventory",
        pair_id=None,
        ok=manifest_hash == inventory.get("manifest_sha256"),
        code="v3_review_manifest_hash_mismatch",
        detail="committed manifest hashes to the inventory's manifest_sha256"
        if manifest_hash == inventory.get("manifest_sha256")
        else "committed manifest hash does not match the inventory",
    )
    findings.add(
        check="manifest_status_is_corpus_built",
        pair_id=None,
        ok=manifest.get("status") == rfb.STATUS_CORPUS_BUILT,
        code="v3_review_manifest_status_unexpected",
        detail=f"manifest status is {manifest.get('status')!r}",
    )
    for label, ok in (
        (
            "blind report new_manifest_sha256 is the committed manifest",
            blind.get("new_manifest_sha256") == manifest_hash,
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
    ):
        findings.add(
            check="manifest_hash_chain",
            pair_id=None,
            ok=bool(ok),
            code="v3_review_manifest_chain_broken",
            detail=label if ok else f"BROKEN: {label}",
        )

    corpus_identity = inventory.get("build_source_manifest_hash")
    identity_ok = (
        isinstance(corpus_identity, str)
        and corpus_identity == blind.get("corpus_identity_hash")
        and corpus_identity == execution.get("build_source_manifest_hash")
    )
    findings.add(
        check="corpus_identity_hash_consistent",
        pair_id=None,
        ok=identity_ok,
        code="v3_review_corpus_identity_mismatch",
        detail="inventory, blind report, and execution report agree on the "
        "corpus identity hash the build ran under"
        if identity_ok
        else "the corpus identity hash disagrees across the committed reports",
    )

    try:
        rfv3.verify_frozen_code_identities(manifest)
        drift = None
    except rfb.BenchmarkError as exc:
        drift = exc.message
    findings.add(
        check="frozen_code_identities_unchanged",
        pair_id=None,
        ok=drift is None,
        code="v3_review_frozen_code_drift",
        detail="parser, detector, workflow store, and contract-v2 evaluator "
        "still hash to the values the manifest froze"
        if drift is None
        else "frozen source files drifted — annotating against modified "
        "semantic code converts the holdout into development data",
    )
    try:
        rfv3.verify_exclusion_provenance(manifest)
        exclusion_drift = None
    except rfb.BenchmarkError as exc:
        exclusion_drift = exc.message
    findings.add(
        check="prior_corpus_exclusions_unchanged",
        pair_id=None,
        ok=exclusion_drift is None,
        code="v3_review_exclusion_drift",
        detail="the frozen development-corpus exclusions still derive from the "
        "committed prior manifests"
        if exclusion_drift is None
        else "the frozen prior-corpus exclusions no longer match their sources",
    )

    contract_ok = (
        inventory.get("annotation_schema_version")
        == manifest.get("frozen_annotation_schema_version")
        == rfb.ANNOTATION_SCHEMA_VERSION
        and inventory.get("annotation_protocol_version")
        == manifest.get("frozen_annotation_protocol_version")
        == rfb.ANNOTATION_PROTOCOL_VERSION
        and inventory.get("unit_identity_contract")
        == manifest.get("frozen_unit_identity_contract")
        == rfv3.FROZEN_UNIT_IDENTITY_CONTRACT
    )
    findings.add(
        check="annotation_contract_frozen",
        pair_id=None,
        ok=contract_ok,
        code="v3_review_annotation_contract_drift",
        detail="the live annotation schema/protocol and the unit identity "
        "contract are the ones the corpus froze"
        if contract_ok
        else "the annotation schema, protocol, or unit identity contract "
        "disagrees with the frozen corpus",
    )

    config_ok = (
        evaluation_config.get("benchmark_id") == BENCHMARK_ID
        and evaluation_config.get("config_version")
        == manifest.get("frozen_evaluation_contract_version")
        and evaluation_config.get("metric_definitions_version")
        == manifest.get("frozen_metric_definitions_version")
        and evaluation_config.get("gold_status_required") == rfb.GOLD_STATUS
        and evaluation_config.get("pass_fail_thresholds") is None
    )
    findings.add(
        check="evaluation_config_frozen_and_unsigned",
        pair_id=None,
        ok=config_ok,
        code="v3_review_evaluation_config_drift",
        detail="the committed evaluation config still declares contract v2, "
        "requires human_verified gold, and declares no pass/fail gate"
        if config_ok
        else "the committed evaluation config drifted from the frozen contract",
    )

    zero_gold = (
        inventory.get("human_verified_label_count") == 0
        and inventory.get("gold_evaluation_runs") == 0
        and execution.get("human_verified_labels") == 0
        and execution.get("gold_metrics_available") is False
        and execution.get("gold_metrics") is None
    )
    findings.add(
        check="committed_reports_record_zero_gold",
        pair_id=None,
        ok=zero_gold,
        code="v3_review_committed_gold_claim",
        detail="the committed reports record zero human-verified labels and "
        "zero gold evaluations, as a record of the blind run"
        if zero_gold
        else "a committed report claims gold labels or gold metrics",
    )

    denials_ok = all(
        report.get("extraction_holdout_evaluation") is False
        and report.get("generalization_claim_supported") is False
        for report in (inventory, blind, execution)
    ) and manifest.get("generalization_claim_supported") is False
    findings.add(
        check="no_generalization_claim_present",
        pair_id=None,
        ok=denials_ok,
        code="v3_review_generalization_claim_present",
        detail="extraction_holdout_evaluation and generalization_claim_supported "
        "are false everywhere; human review does not change either"
        if denials_ok
        else "a committed artifact asserts holdout evaluation or generalization",
    )


def check_inventory_pair_set(findings: Findings, artifacts: Mapping[str, Any]) -> None:
    """The reviewable set is the committed inventory's, and it must close over
    the frozen manifest exactly once."""
    inventory = artifacts["inventory"]
    manifest = artifacts["manifest"]
    rows = inventory_rows(inventory)
    ids = [str(row.get("pair_id")) for row in rows]
    duplicates = sorted({pair_id for pair_id in ids if ids.count(pair_id) > 1})
    findings.add(
        check="inventory_pair_ids_unique",
        pair_id=None,
        ok=not duplicates,
        code="v3_review_duplicate_pair",
        detail="every inventory pair id appears exactly once"
        if not duplicates
        else f"duplicate inventory pair ids: {bounded(duplicates)}",
    )

    manifest_ids = [pair["pair_id"] for pair in manifest.get("pairs", [])]
    unknown = sorted(set(ids) - set(manifest_ids))
    missing = sorted(set(manifest_ids) - set(ids))
    findings.add(
        check="inventory_matches_manifest_pairs",
        pair_id=None,
        ok=not unknown and not missing,
        code="v3_review_pair_set_drift",
        detail="the inventory names exactly the frozen manifest pairs"
        if not unknown and not missing
        else (
            f"pair set drift — unknown: {bounded(unknown) or 'none'};"
            f" missing: {bounded(missing) or 'none'}"
        ),
    )

    order_ok = ids == sorted(ids)
    findings.add(
        check="inventory_order_is_deterministic",
        pair_id=None,
        ok=order_ok,
        code="v3_review_inventory_order_drift",
        detail="committed inventory order is the deterministic pair-id order "
        "review must follow"
        if order_ok
        else "committed inventory order is not deterministic pair-id order",
    )

    written = review_ready_rows(inventory)
    blocked = blocked_rows(inventory)
    counts_ok = (
        inventory.get("packets_written") == len(written)
        and inventory.get("packets_blocked") == len(blocked)
        and inventory.get("machine_proposed_label_count")
        == sum(int(row.get("label_count") or 0) for row in rows)
    )
    findings.add(
        check="inventory_counts_self_consistent",
        pair_id=None,
        ok=counts_ok,
        code="v3_review_inventory_counts_inconsistent",
        detail=(
            f"inventory totals agree with its rows: {len(written)} written, "
            f"{len(blocked)} blocked, "
            f"{inventory.get('machine_proposed_label_count')} proposed labels"
        )
        if counts_ok
        else "inventory totals disagree with its own rows",
    )


# --- Per-pair workspace integrity ---------------------------------------------


def check_pair_workspace(
    findings: Findings,
    *,
    row: Mapping[str, Any],
    layout: rfb.CorpusLayout,
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify one review-ready pair's frozen inputs.

    Returns the loadable pieces so the completion checks can bind against
    them; a missing piece is reported and returned as ``None``.
    """
    pair_id = str(row["pair_id"])
    manifest = artifacts["manifest"]
    inventory = artifacts["inventory"]
    execution_rows = {
        entry["pair_id"]: entry
        for entry in artifacts["execution"].get("executions", [])
        if isinstance(entry, dict)
    }
    corpus_identity = inventory.get("build_source_manifest_hash")
    loaded: dict[str, Any] = {"packet": None, "record": None, "proposal": None}

    packet_path = layout.packet_json_path(pair_id)
    if not packet_path.exists():
        findings.add(
            check="packet_present",
            pair_id=pair_id,
            ok=False,
            code="v3_review_packet_missing",
            detail="local machine-proposed packet is missing",
        )
    else:
        packet = load_json(packet_path, f"{pair_id} packet")
        loaded["packet"] = packet
        packet_hash = rfb.payload_hash(packet)
        findings.add(
            check="packet_hash_matches_inventory",
            pair_id=pair_id,
            ok=packet_hash == row.get("packet_sha256"),
            code="v3_review_packet_hash_drift",
            detail="packet hashes to the committed inventory value"
            if packet_hash == row.get("packet_sha256")
            else "local packet does not hash to the committed inventory value",
        )
        bind_ok = (
            packet.get("pair_id") == pair_id
            and packet.get("benchmark_id") == BENCHMARK_ID
            and packet.get("source_manifest_hash") == corpus_identity
            and packet.get("previous", {}).get("section_hash")
            == row.get("previous_section_hash")
            and packet.get("current", {}).get("section_hash")
            == row.get("current_section_hash")
        )
        findings.add(
            check="packet_binds_corpus_identity_and_sections",
            pair_id=pair_id,
            ok=bind_ok,
            code="v3_review_packet_binding_drift",
            detail="packet binds the corpus identity hash and both section hashes"
            if bind_ok
            else "packet identity fields disagree with the committed inventory",
        )
        versions = packet.get("parser_versions", {})
        for label, actual, expected, code in (
            (
                "parser",
                versions.get("html_parser"),
                manifest.get("frozen_extraction_parser_version"),
                "v3_review_parser_identity_drift",
            ),
            (
                "unit grammar",
                versions.get("unit_grammar"),
                manifest.get("frozen_unit_grammar_version"),
                "v3_review_unit_grammar_identity_drift",
            ),
            (
                "detector",
                versions.get("detector"),
                manifest.get("frozen_detector_version"),
                "v3_review_detector_identity_drift",
            ),
            (
                "workflow",
                versions.get("workflow"),
                manifest.get("frozen_workflow_version"),
                "v3_review_workflow_identity_drift",
            ),
        ):
            findings.add(
                check="packet_binds_pipeline_identity",
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
            code="v3_review_build_record_missing",
            detail="local build record is missing",
        )
    else:
        record = load_json(build_path, f"{pair_id} build record")
        loaded["record"] = record
        record_ok = (
            record.get("source_manifest_hash") == corpus_identity
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
            code="v3_review_build_record_drift",
            detail="build record matches the inventory's hashes and unit counts"
            if record_ok
            else "build record disagrees with the committed inventory",
        )
        unit_inventory, problems = canonical_unit_inventory(record)
        count_ok = not problems and len(unit_inventory) == (
            int(row.get("previous_canonical_unit_id_count") or 0)
            + int(row.get("current_canonical_unit_id_count") or 0)
        )
        findings.add(
            check="canonical_unit_identities_wellformed",
            pair_id=pair_id,
            ok=count_ok,
            code="v3_review_unit_identity_invalid",
            detail=(
                f"{len(unit_inventory)} canonical side:sequence:unit_key "
                "identities, each agreeing with its own position metadata"
            )
            if count_ok
            else "a build unit id disagrees with its own side, sequence, or "
            f"unit_key metadata, or the count drifted: {bounded(problems)}",
        )

        for side in ("previous", "current"):
            text_path = layout.section_text_path(pair_id, side)
            if not text_path.exists():
                findings.add(
                    check="section_text_hash_matches",
                    pair_id=pair_id,
                    ok=False,
                    code="v3_review_section_text_missing",
                    detail=f"missing {side} section text",
                )
                continue
            recomputed = rfb.section_hash(text_path.read_text(encoding="utf-8"))
            ok = recomputed == record.get(side, {}).get("section_hash")
            findings.add(
                check="section_text_hash_matches",
                pair_id=pair_id,
                ok=ok,
                code="v3_review_section_hash_drift",
                detail=f"{side} section text re-hashes to the recorded hash"
                if ok
                else f"{side} section text drifted from the recorded hash",
            )

        manifest_pair = next(
            (
                pair
                for pair in manifest.get("pairs", [])
                if pair.get("pair_id") == pair_id
            ),
            None,
        )
        for side in ("previous", "current"):
            expected = (manifest_pair or {}).get(side, {}).get("expected_sha256")
            primary_document = (
                (manifest_pair or {}).get(side, {}).get("primary_document")
            )
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
                    code="v3_review_source_missing",
                    detail=f"missing {side} source body",
                )
                continue
            observed = rfb.sha256_file(source_path)
            ok = observed == expected and observed == recorded
            findings.add(
                check="source_checksum_matches_manifest",
                pair_id=pair_id,
                ok=ok,
                code="v3_review_source_checksum_drift",
                detail=f"{side} source sha256 matches the frozen manifest digest"
                if ok
                else f"{side} source sha256 disagrees with the frozen digest",
            )

    result_path = layout.build_dir(pair_id) / "detection_result.json"
    execution_row = execution_rows.get(pair_id, {})
    if not result_path.exists():
        findings.add(
            check="result_hash_matches_reports",
            pair_id=pair_id,
            ok=False,
            code="v3_review_detection_result_missing",
            detail="missing local detection result",
        )
    else:
        result = load_json(result_path, f"{pair_id} detection result")
        recomputed = stable_result_hash(result)
        ok = (
            recomputed == execution_row.get("result_hash")
            and recomputed == row.get("comparison_result_hash")
        )
        findings.add(
            check="result_hash_matches_reports",
            pair_id=pair_id,
            ok=ok,
            code="v3_review_result_hash_drift",
            detail="detection result re-hashes to the committed result hash"
            if ok
            else "local detection result does not match the committed result hash",
        )

    proposal_path = layout.machine_proposed_path(pair_id)
    if not proposal_path.exists():
        findings.add(
            check="machine_proposal_intact",
            pair_id=pair_id,
            ok=False,
            code="v3_review_machine_proposal_missing",
            detail="missing machine proposal",
        )
        return loaded
    try:
        proposal = rfb.load_annotation(proposal_path)
    except rfb.BenchmarkError as exc:
        findings.add(
            check="machine_proposal_intact",
            pair_id=pair_id,
            ok=False,
            code="v3_review_machine_proposal_invalid",
            detail=f"machine proposal invalid [{exc.code}]",
        )
        return loaded
    loaded["proposal"] = proposal
    intact = (
        proposal.get("annotation_status") == rfb.ANNOTATION_MACHINE_PROPOSED
        and proposal.get("annotator_id") is None
        and proposal.get("verification_timestamp") is None
        and proposal.get("benchmark_id") == BENCHMARK_ID
        and proposal.get("pair_id") == pair_id
        and len(proposal.get("labels", [])) == row.get("label_count")
        and rfb.annotation_hash(proposal) == row.get("annotation_sha256")
    )
    findings.add(
        check="machine_proposal_intact",
        pair_id=pair_id,
        ok=intact,
        code="v3_review_machine_proposal_drift",
        detail="machine proposal is unmodified, machine_proposed, unattributed, "
        "and hashes to the committed inventory value"
        if intact
        else "machine proposal status, attribution, label count, or hash drifted",
    )
    if loaded["record"] is not None:
        try:
            rfb.validate_annotation_against_build(proposal, loaded["record"])
            bind_ok, bind_code = True, None
        except rfb.BenchmarkError as exc:
            bind_ok, bind_code = False, exc.code
        findings.add(
            check="machine_proposal_binds_build",
            pair_id=pair_id,
            ok=bind_ok,
            code=f"v3_review_machine_proposal_build_binding:{bind_code}",
            detail="machine proposal binds this build's sections and unit ids"
            if bind_ok
            else f"machine proposal build binding failed [{bind_code}]",
        )
    return loaded


def check_blocked_pair(
    findings: Findings, *, row: Mapping[str, Any], layout: rfb.CorpusLayout
) -> None:
    """A blocked pair stays in the corpus and receives NOTHING.

    No packet, no proposal, no template, no review record, and above all no
    fabricated label. It is never replaced and never marked human_verified to
    make a corpus count come out even.
    """
    pair_id = str(row["pair_id"])
    row_ok = (
        row.get("packet_status") == "blocked"
        and row.get("review_ready") is False
        and row.get("human_verified") is False
        and row.get("packet_sha256") is None
        and row.get("annotation_sha256") is None
        and row.get("annotation_relative_path") is None
        and row.get("comparison_result_hash") is None
        and row.get("label_count") == 0
        and row.get("blocking_reason") in KNOWN_BLOCKING_REASONS
    )
    findings.add(
        check="blocked_pair_row_wellformed",
        pair_id=pair_id,
        ok=row_ok,
        code="v3_review_blocked_row_invalid",
        detail=(
            f"blocked ({row.get('blocking_reason')}); stays in the corpus and "
            "in evaluator scoring scope, and receives no fabricated label"
        )
        if row_ok
        else "inventory row is not a proper blocked row",
    )
    forbidden = [
        layout.packet_json_path(pair_id),
        layout.packet_markdown_path(pair_id),
        layout.machine_proposed_path(pair_id),
        human_review_path(layout, pair_id),
        review_record_path(layout, pair_id),
    ]
    present = sorted(path.name for path in forbidden if path.exists())
    findings.add(
        check="blocked_pair_has_no_annotation_surface",
        pair_id=pair_id,
        ok=not present,
        code="v3_review_blocked_pair_surface_forbidden",
        detail="no packet, proposal, template, or review record exists for the "
        "blocked pair"
        if not present
        else f"forbidden files exist for the blocked pair: {bounded(present)}",
    )


def check_directories_closed(
    findings: Findings, *, layout: rfb.CorpusLayout, inventory: Mapping[str, Any]
) -> None:
    ready = [str(row["pair_id"]) for row in review_ready_rows(inventory)]
    allowed_annotations = {f"{pair_id}.machine_proposed.json" for pair_id in ready}
    annotations = layout.annotations_dir()
    unexpected = (
        sorted(
            entry.name
            for entry in annotations.iterdir()
            if entry.is_file() and entry.name not in allowed_annotations
        )
        if annotations.exists()
        else []
    )
    findings.add(
        check="annotation_directory_closed",
        pair_id=None,
        ok=not unexpected,
        code="v3_review_unexpected_annotation_file",
        detail="annotations/ holds only the review-ready pairs' machine "
        "proposals; a human decision never enters that directory"
        if not unexpected
        else f"unexpected annotation files: {bounded(unexpected)}",
    )

    allowed_review = (
        {f"{pair_id}.review.json" for pair_id in ready}
        | {f"{pair_id}.human_review.json" for pair_id in ready}
        | {"queue.json"}
    )
    directory = review_dir(layout)
    unexpected_review = (
        sorted(
            entry.name
            for entry in directory.iterdir()
            if entry.is_file() and entry.name not in allowed_review
        )
        if directory.exists()
        else []
    )
    findings.add(
        check="review_directory_closed",
        pair_id=None,
        ok=not unexpected_review,
        code="v3_review_unexpected_review_file",
        detail="review/ holds only the queue, the review records, and the "
        "human annotations of the review-ready pairs"
        if not unexpected_review
        else f"unexpected review files: {bounded(unexpected_review)}",
    )


# --- Review record and queue --------------------------------------------------


def build_review_record(
    *,
    row: Mapping[str, Any],
    layout: rfb.CorpusLayout,
    proposal: Mapping[str, Any],
    record: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    """A prepared review record: provenance plus one undecided subject row.

    Every ``reviewer_decision`` is ``null``. Nothing in this function chooses a
    value, and a null decision is never treated as agreement later.
    """
    pair_id = str(row["pair_id"])
    unit_inventory, _problems = canonical_unit_inventory(record)
    return {
        "review_record_version": REVIEW_RECORD_VERSION,
        "prepared_by": PREPARATION_TOOL_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "pair_id": pair_id,
        "annotation_schema_version": rfb.ANNOTATION_SCHEMA_VERSION,
        "annotation_protocol_version": rfb.ANNOTATION_PROTOCOL_VERSION,
        "unit_identity_contract": rfv3.FROZEN_UNIT_IDENTITY_CONTRACT,
        "packet_relative_path": layout.relative(layout.packet_json_path(pair_id)),
        "packet_sha256": row.get("packet_sha256"),
        "machine_proposal_relative_path": layout.relative(
            layout.machine_proposed_path(pair_id)
        ),
        "machine_proposal_annotation_sha256": row.get("annotation_sha256"),
        "machine_proposal_file_sha256": file_sha256(
            layout.machine_proposed_path(pair_id)
        ),
        "human_review_relative_path": layout.relative(
            human_review_path(layout, pair_id)
        ),
        "comparison_result_hash": row.get("comparison_result_hash"),
        "source_manifest_hash": inventory.get("build_source_manifest_hash"),
        "previous_section_hash": row.get("previous_section_hash"),
        "current_section_hash": row.get("current_section_hash"),
        "previous_source_sha256": record.get("previous", {}).get("source_sha256"),
        "current_source_sha256": record.get("current", {}).get("source_sha256"),
        "canonical_unit_ids": {
            "previous": [
                unit for unit in sorted(unit_inventory) if unit.startswith("previous:")
            ],
            "current": [
                unit for unit in sorted(unit_inventory) if unit.startswith("current:")
            ],
        },
        "proposed_label_count": len(proposal.get("labels", [])),
        "reviewer_completed": False,
        "subjects": canonical_subject_rows(proposal),
    }


def build_human_template(
    *, pair_id: str, proposal: Mapping[str, Any]
) -> dict[str, Any]:
    """The empty human annotation: bindings carried, every decision ``null``.

    The template is deliberately NOT schema-valid — ``annotation_status`` is
    null and no label carries a change type — so an untouched template can
    never be mistaken for an annotation, and a reviewer who agrees with a
    machine proposal must type the value rather than leave a file alone.
    """
    return {
        "schema_version": proposal["schema_version"],
        "annotation_protocol_version": proposal["annotation_protocol_version"],
        "benchmark_id": proposal["benchmark_id"],
        "pair_id": pair_id,
        "annotation_status": None,
        "annotator_id": None,
        "verification_timestamp": None,
        "source_manifest_hash": proposal["source_manifest_hash"],
        "previous_section_hash": proposal["previous_section_hash"],
        "current_section_hash": proposal["current_section_hash"],
        "generated_by": HUMAN_TEMPLATE_GENERATED_BY,
        "labels": [
            {
                "label_id": label["label_id"],
                "expected_change_type": None,
                "previous_unit_id": label["previous_unit_id"],
                "current_unit_id": label["current_unit_id"],
                "expected_reason_code": None,
                "expected_evidence_side": None,
                "expected_direction": None,
                "reviewer_note": None,
                "confidence": None,
            }
            for label in proposal["labels"]
        ],
    }


def is_untouched_template(document: Mapping[str, Any]) -> bool:
    """A human file nobody has decided anything in yet."""
    if document.get("annotation_status") is not None:
        return False
    if document.get("annotator_id") is not None:
        return False
    if document.get("verification_timestamp") is not None:
        return False
    return not any(
        label.get(field) is not None
        for label in document.get("labels", [])
        if isinstance(label, dict)
        for field in LABEL_DECISION_FIELDS
    )


def derive_review_status(
    *,
    row: Mapping[str, Any],
    review_record: Mapping[str, Any] | None,
    human: Mapping[str, Any] | None,
) -> str:
    """Queue status DERIVED from workspace state — never asserted by a tool.

    ``admitted`` and ``rejected`` mirror what the reviewer wrote into the
    annotation's own ``annotation_status``; they are not a judgement this
    module makes and they do not by themselves mean the pair passed the
    admission gate.
    """
    if row.get("packet_status") != "written":
        return REVIEW_STATUS_BLOCKED
    status = (human or {}).get("annotation_status")
    if status == rfb.ANNOTATION_HUMAN_VERIFIED:
        return REVIEW_STATUS_ADMITTED
    if status == rfb.ANNOTATION_REJECTED:
        return REVIEW_STATUS_REJECTED
    if review_record is None:
        return REVIEW_STATUS_PENDING
    if review_record.get("reviewer_completed") is True:
        return REVIEW_STATUS_REVIEWER_COMPLETED
    decided = sum(
        1
        for subject in review_record.get("subjects", [])
        if isinstance(subject, dict) and subject.get("reviewer_decision") is not None
    )
    if decided == 0 and (human is None or is_untouched_template(human)):
        return REVIEW_STATUS_PENDING
    return REVIEW_STATUS_IN_REVIEW


def build_queue(
    *,
    layout: rfb.CorpusLayout,
    inventory: Mapping[str, Any],
    records: Mapping[str, Mapping[str, Any] | None],
    humans: Mapping[str, Mapping[str, Any] | None],
) -> dict[str, Any]:
    """The deterministic local review queue.

    Ordering is the committed inventory's order and nothing else: never label
    count, issuer, machine confidence, change type, or perceived difficulty.
    Entries carry identity, hashes, bindings, counts, and a derived status —
    no packet prose, no filing text, no reviewer decision, no absolute path,
    and no wall-clock value, so the payload is a pure function of the
    workspace.
    """
    entries: list[dict[str, Any]] = []
    for position, row in enumerate(inventory_rows(inventory), start=1):
        pair_id = str(row["pair_id"])
        review_record = records.get(pair_id)
        blocked = row.get("packet_status") != "written"
        entries.append(
            {
                "review_position": position,
                "pair_id": pair_id,
                "review_status": derive_review_status(
                    row=row, review_record=review_record, human=humans.get(pair_id)
                ),
                "blocking_reason": row.get("blocking_reason"),
                "packet_relative_path": row.get("packet_relative_path"),
                "packet_sha256": row.get("packet_sha256"),
                "machine_annotation_relative_path": row.get(
                    "annotation_relative_path"
                ),
                "machine_annotation_sha256": row.get("annotation_sha256"),
                "machine_annotation_file_sha256": (
                    (review_record or {}).get("machine_proposal_file_sha256")
                ),
                "human_review_relative_path": (
                    None
                    if blocked
                    else layout.relative(human_review_path(layout, pair_id))
                ),
                "review_record_relative_path": (
                    None
                    if blocked
                    else layout.relative(review_record_path(layout, pair_id))
                ),
                "comparison_result_hash": row.get("comparison_result_hash"),
                "previous_section_hash": row.get("previous_section_hash"),
                "current_section_hash": row.get("current_section_hash"),
                "previous_source_sha256": (
                    (review_record or {}).get("previous_source_sha256")
                ),
                "current_source_sha256": (
                    (review_record or {}).get("current_source_sha256")
                ),
                "proposed_label_count": row.get("label_count"),
                "canonical_unit_id_count": (
                    int(row.get("previous_canonical_unit_id_count") or 0)
                    + int(row.get("current_canonical_unit_id_count") or 0)
                ),
                "subjects_decided": sum(
                    1
                    for subject in (review_record or {}).get("subjects", [])
                    if isinstance(subject, dict)
                    and subject.get("reviewer_decision") is not None
                ),
            }
        )
    return {
        "queue_version": REVIEW_QUEUE_VERSION,
        "prepared_by": PREPARATION_TOOL_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "annotation_schema_version": rfb.ANNOTATION_SCHEMA_VERSION,
        "annotation_protocol_version": rfb.ANNOTATION_PROTOCOL_VERSION,
        "unit_identity_contract": rfv3.FROZEN_UNIT_IDENTITY_CONTRACT,
        "packet_inventory_run_hash": inventory.get("run_hash"),
        "manifest_sha256": inventory.get("manifest_sha256"),
        "build_source_manifest_hash": inventory.get("build_source_manifest_hash"),
        "ordering": "committed_annotation_packet_inventory_order",
        "human_verified_count": 0,
        "notes": [
            "Machine proposals are INPUTS TO REVIEW, not ground truth. A null "
            "or unchanged value is never approval; every label needs an "
            "explicit reviewer decision recorded in the pair's review record.",
            "No tool in this repository sets annotation_status, annotator_id, "
            "or verification_timestamp. A person edits the human annotation "
            "file to admit it; passing the admission gate proves identity, "
            "closure, and hygiene, never that a judgement is correct.",
            "Blocked pairs stay in the corpus with their stable blocking "
            "reason and receive no fabricated label.",
        ],
        "entries": entries,
    }


def check_queue(
    findings: Findings, *, queue: Mapping[str, Any], inventory: Mapping[str, Any]
) -> None:
    """The queue must be a faithful, prose-free, decision-free index."""
    entries = [entry for entry in queue.get("entries", []) if isinstance(entry, dict)]
    expected = [str(row["pair_id"]) for row in inventory_rows(inventory)]
    observed = [str(entry.get("pair_id")) for entry in entries]

    findings.add(
        check="queue_version_and_identity",
        pair_id=None,
        ok=queue.get("queue_version") == REVIEW_QUEUE_VERSION
        and queue.get("benchmark_id") == BENCHMARK_ID
        and queue.get("ordering") == "committed_annotation_packet_inventory_order",
        code="v3_review_queue_identity_drift",
        detail="queue declares its version, benchmark, and inventory ordering",
    )
    findings.add(
        check="queue_order_follows_inventory",
        pair_id=None,
        ok=observed == expected,
        code="v3_review_queue_order_drift",
        detail="queue order is the committed packet inventory order"
        if observed == expected
        else "queue order does not follow the committed packet inventory",
    )
    duplicates = sorted({pair_id for pair_id in observed if observed.count(pair_id) > 1})
    findings.add(
        check="queue_pairs_unique",
        pair_id=None,
        ok=not duplicates,
        code="v3_review_queue_duplicate_pair",
        detail="every pair appears in the queue exactly once"
        if not duplicates
        else f"duplicate queue pairs: {bounded(duplicates)}",
    )
    unknown = sorted(set(observed) - set(expected))
    findings.add(
        check="queue_pairs_known",
        pair_id=None,
        ok=not unknown,
        code="v3_review_queue_unknown_pair",
        detail="every queue pair is a committed inventory pair"
        if not unknown
        else f"queue names pairs outside the committed inventory: {bounded(unknown)}",
    )
    positions = [entry.get("review_position") for entry in entries]
    findings.add(
        check="queue_positions_contiguous",
        pair_id=None,
        ok=positions == list(range(1, len(entries) + 1)),
        code="v3_review_queue_position_drift",
        detail="review positions are 1..n in inventory order",
    )

    key_defects = sorted(
        str(entry.get("pair_id"))
        for entry in entries
        if set(entry) != set(_QUEUE_ENTRY_KEYS)
    )
    findings.add(
        check="queue_entry_keys_exact",
        pair_id=None,
        ok=not key_defects,
        code="v3_review_queue_entry_keys_invalid",
        detail="every queue entry carries exactly the bounded metadata keys"
        if not key_defects
        else f"queue entries with unexpected keys: {bounded(key_defects)}",
    )
    bad_status = sorted(
        str(entry.get("pair_id"))
        for entry in entries
        if entry.get("review_status") not in REVIEW_STATUSES
    )
    findings.add(
        check="queue_statuses_known",
        pair_id=None,
        ok=not bad_status,
        code="v3_review_queue_status_invalid",
        detail="every queue status is one of the declared review statuses"
        if not bad_status
        else f"unknown queue statuses on: {bounded(bad_status)}",
    )
    decision_fields = sorted(
        str(entry.get("pair_id"))
        for entry in entries
        if any(
            field in entry
            for field in LABEL_DECISION_FIELDS + DOCUMENT_DECISION_FIELDS
        )
    )
    findings.add(
        check="queue_carries_no_human_decision",
        pair_id=None,
        ok=not decision_fields,
        code="v3_review_queue_carries_decision",
        detail="the queue carries no annotation decision and no reviewer identity"
        if not decision_fields
        else f"queue entries carry decision fields: {bounded(decision_fields)}",
    )


def check_review_record(
    findings: Findings,
    *,
    pair_id: str,
    review_record: Mapping[str, Any],
    row: Mapping[str, Any],
    layout: rfb.CorpusLayout,
    inventory: Mapping[str, Any],
    proposal: Mapping[str, Any] | None,
) -> None:
    """The review record must bind the frozen inputs and hold only explicit
    decisions."""
    keys_ok = set(review_record) == set(_REVIEW_RECORD_KEYS)
    findings.add(
        check="review_record_keys_exact",
        pair_id=pair_id,
        ok=keys_ok,
        code="v3_review_record_keys_invalid",
        detail="review record carries exactly the declared keys"
        if keys_ok
        else "review record keys drifted",
    )
    binding_ok = (
        review_record.get("review_record_version") == REVIEW_RECORD_VERSION
        and review_record.get("benchmark_id") == BENCHMARK_ID
        and review_record.get("pair_id") == pair_id
        and review_record.get("packet_sha256") == row.get("packet_sha256")
        and review_record.get("machine_proposal_annotation_sha256")
        == row.get("annotation_sha256")
        and review_record.get("comparison_result_hash")
        == row.get("comparison_result_hash")
        and review_record.get("source_manifest_hash")
        == inventory.get("build_source_manifest_hash")
        and review_record.get("previous_section_hash")
        == row.get("previous_section_hash")
        and review_record.get("current_section_hash")
        == row.get("current_section_hash")
        and review_record.get("annotation_schema_version")
        == rfb.ANNOTATION_SCHEMA_VERSION
        and review_record.get("annotation_protocol_version")
        == rfb.ANNOTATION_PROTOCOL_VERSION
        and review_record.get("unit_identity_contract")
        == rfv3.FROZEN_UNIT_IDENTITY_CONTRACT
    )
    findings.add(
        check="review_record_binds_frozen_inputs",
        pair_id=pair_id,
        ok=binding_ok,
        code="v3_review_record_binding_drift",
        detail="review record binds the packet, machine proposal, comparison "
        "result, corpus identity, and section hashes"
        if binding_ok
        else "review record bindings disagree with the committed inventory",
    )

    proposal_path = layout.machine_proposed_path(pair_id)
    byte_ok = (
        proposal_path.exists()
        and file_sha256(proposal_path) == review_record.get("machine_proposal_file_sha256")
    )
    findings.add(
        check="machine_proposal_bytes_unchanged",
        pair_id=pair_id,
        ok=byte_ok,
        code="v3_review_machine_proposal_bytes_drift",
        detail="the machine proposal is byte-identical to the copy the review "
        "record pinned when review was prepared"
        if byte_ok
        else "the machine proposal's bytes changed after preparation — the "
        "original proposal must remain independently hash-verifiable",
    )

    subjects = [
        subject
        for subject in review_record.get("subjects", [])
        if isinstance(subject, dict)
    ]
    subject_keys_ok = all(set(subject) == set(_SUBJECT_KEYS) for subject in subjects)
    findings.add(
        check="review_record_subject_keys_exact",
        pair_id=pair_id,
        ok=subject_keys_ok and len(subjects) == len(review_record.get("subjects", [])),
        code="v3_review_record_subject_keys_invalid",
        detail="every subject row carries exactly the declared keys",
    )

    if proposal is not None:
        expected_subjects = [
            (label["previous_unit_id"], label["current_unit_id"])
            for label in proposal["labels"]
        ]
        observed_subjects = [
            (subject.get("previous_unit_id"), subject.get("current_unit_id"))
            for subject in subjects
        ]
        findings.add(
            check="review_record_covers_every_proposed_subject",
            pair_id=pair_id,
            ok=observed_subjects == expected_subjects,
            code="v3_review_record_subject_drift",
            detail=f"{len(observed_subjects)} subject rows in machine-proposal "
            "order, one per canonical occurrence"
            if observed_subjects == expected_subjects
            else "review record subjects disagree with the machine proposal's "
            "canonical subjects",
        )
    duplicates = sorted(
        {
            subject_display(key)
            for key in (
                (subject.get("previous_unit_id"), subject.get("current_unit_id"))
                for subject in subjects
            )
            if [
                (item.get("previous_unit_id"), item.get("current_unit_id"))
                for item in subjects
            ].count(key)
            > 1
        }
    )
    findings.add(
        check="review_record_subjects_unique",
        pair_id=pair_id,
        ok=not duplicates,
        code="v3_review_record_duplicate_subject",
        detail="no canonical subject appears in two review rows"
        if not duplicates
        else f"duplicate canonical subjects: {bounded(duplicates)}",
    )
    invalid = sorted(
        str(subject.get("label_id"))
        for subject in subjects
        if subject.get("reviewer_decision") is not None
        and subject.get("reviewer_decision") not in REVIEWER_DECISIONS
    )
    findings.add(
        check="reviewer_decisions_are_declared_values",
        pair_id=pair_id,
        ok=not invalid,
        code="v3_review_invalid_reviewer_decision",
        detail=f"every recorded decision is one of {list(REVIEWER_DECISIONS)}"
        if not invalid
        else f"unknown reviewer decisions on: {bounded(invalid)}",
    )


def check_reviewer_completion(
    findings: Findings,
    *,
    pair_id: str,
    review_record: Mapping[str, Any],
    human: Mapping[str, Any] | None,
) -> None:
    """Completion is an explicit act over every subject; nothing infers it."""
    subjects = [
        subject
        for subject in review_record.get("subjects", [])
        if isinstance(subject, dict)
    ]
    undecided = sorted(
        str(subject.get("label_id"))
        for subject in subjects
        if subject.get("reviewer_decision") is None
    )
    findings.add(
        check="every_subject_explicitly_decided",
        pair_id=pair_id,
        ok=not undecided,
        code="v3_review_subject_undecided",
        detail=f"all {len(subjects)} canonical subjects carry an explicit "
        "reviewer decision"
        if not undecided
        else f"subjects with no explicit reviewer decision: {bounded(undecided)}",
    )
    malformed = sorted(
        str(subject.get("label_id"))
        for subject in subjects
        if subject.get("reviewer_decision") == DECISION_REJECTED_MALFORMED
    )
    findings.add(
        check="no_subject_rejected_as_malformed",
        pair_id=pair_id,
        ok=not malformed,
        code="v3_review_subject_rejected_malformed",
        detail="no subject was rejected as malformed"
        if not malformed
        else "the reviewer rejected these subjects as malformed; admission "
        f"stops until they are resolved: {bounded(malformed)}",
    )
    findings.add(
        check="reviewer_completion_marker_present",
        pair_id=pair_id,
        ok=review_record.get("reviewer_completed") is True,
        code="v3_review_completion_marker_missing",
        detail="the reviewer recorded an explicit completion marker for this packet"
        if review_record.get("reviewer_completed") is True
        else "no explicit reviewer completion marker; file presence, an "
        "unchanged value, and an omitted field are never approval",
    )
    if human is not None and is_untouched_template(human):
        findings.add(
            check="human_annotation_is_not_an_untouched_template",
            pair_id=pair_id,
            ok=False,
            code="v3_review_untouched_template",
            detail="the human annotation is still the empty template",
        )


# --- Completed-annotation admission -------------------------------------------

_EVIDENCE_SIDE_REQUIREMENTS = {
    "previous": ("previous_unit_id",),
    "current": ("current_unit_id",),
    "both": ("previous_unit_id", "current_unit_id"),
}


def check_completed_annotation(
    findings: Findings,
    *,
    pair_id: str,
    layout: rfb.CorpusLayout,
    row: Mapping[str, Any],
    inventory: Mapping[str, Any],
    packet: Mapping[str, Any] | None,
    record: Mapping[str, Any] | None,
    require_admitted: bool,
) -> None:
    """The gold-admission gate for one human annotation."""
    path = human_review_path(layout, pair_id)
    if not path.exists():
        findings.add(
            check="human_annotation_admitted",
            pair_id=pair_id,
            ok=not require_admitted,
            code="v3_review_annotation_missing",
            detail="awaiting human review"
            if not require_admitted
            else "no human annotation file exists",
        )
        return

    raw = load_json(path, f"{pair_id} human annotation")
    if raw.get("annotation_status") is None:
        _check_template_integrity(
            findings, pair_id=pair_id, raw=raw, row=row, inventory=inventory
        )
        findings.add(
            check="human_annotation_admitted",
            pair_id=pair_id,
            ok=not require_admitted,
            code="v3_review_annotation_not_completed",
            detail="still an empty template — not human_verified"
            if require_admitted
            else "empty template awaiting human decisions",
        )
        return

    try:
        annotation = rfb.load_annotation(path)
    except rfb.BenchmarkError as exc:
        findings.add(
            check="human_annotation_schema",
            pair_id=pair_id,
            ok=False,
            code=f"v3_review_annotation_schema:{exc.code}",
            detail=f"invalid annotation [{exc.code}]",
        )
        return
    findings.add(
        check="human_annotation_schema",
        pair_id=pair_id,
        ok=True,
        detail="schema valid: exact keys, declared change types, no duplicate "
        "label ids, bounded notes",
    )

    status = annotation["annotation_status"]
    if status in rfb.MACHINE_ONLY_STATUSES:
        findings.add(
            check="human_annotation_admitted",
            pair_id=pair_id,
            ok=False,
            code="v3_review_machine_status_in_human_file",
            detail=f"the human annotation still carries the machine status "
            f"{status!r} — a machine proposal was copied instead of reviewed, "
            "and a machine status can never be admitted",
        )
        return
    verified = status == rfb.ANNOTATION_HUMAN_VERIFIED
    findings.add(
        check="human_annotation_admitted",
        pair_id=pair_id,
        ok=verified if require_admitted else True,
        code="v3_review_annotation_not_human_verified",
        detail=f"annotation_status={status!r}"
        + ("" if verified else " (not human_verified)"),
    )

    annotator = annotation.get("annotator_id")
    findings.add(
        check="annotator_id_explicit_and_not_placeholder",
        pair_id=pair_id,
        ok=not is_placeholder_annotator(annotator),
        code="v3_review_annotator_placeholder",
        detail="annotator_id is an explicit, non-placeholder, self-asserted id"
        if not is_placeholder_annotator(annotator)
        else "annotator_id is missing or a placeholder; no tool may supply one, "
        "and it is never derived from git, the environment, or a prior corpus",
    )

    timestamp = annotation.get("verification_timestamp")
    moment = parse_aware(timestamp)
    findings.add(
        check="verification_timestamp_explicit_utc",
        pair_id=pair_id,
        ok=is_explicit_utc(moment),
        code="v3_review_timestamp_not_explicit_utc",
        detail="verification timestamp is parseable and carries an explicit "
        "+00:00 offset"
        if is_explicit_utc(moment)
        else "verification timestamp is missing, unparseable, naive, or not UTC",
    )
    generated = parse_aware(inventory.get("generated_at"))
    postdates = moment is not None and generated is not None and moment > generated
    findings.add(
        check="verification_timestamp_postdates_packets",
        pair_id=pair_id,
        ok=postdates,
        code="v3_review_timestamp_precedes_packets",
        detail="verification timestamp postdates packet generation"
        if postdates
        else "verification timestamp does not postdate packet generation — "
        "nothing existed to verify at that instant",
    )

    bind_ok = (
        annotation.get("benchmark_id") == BENCHMARK_ID
        and annotation.get("pair_id") == pair_id
        and annotation.get("source_manifest_hash")
        == inventory.get("build_source_manifest_hash")
        and annotation.get("previous_section_hash") == row.get("previous_section_hash")
        and annotation.get("current_section_hash") == row.get("current_section_hash")
    )
    findings.add(
        check="human_annotation_binds_corpus",
        pair_id=pair_id,
        ok=bind_ok,
        code="v3_review_annotation_binding_drift",
        detail="annotation binds this benchmark, pair, corpus identity, and both "
        "section hashes"
        if bind_ok
        else "annotation identity or hash bindings drifted",
    )

    if record is not None:
        try:
            rfb.validate_annotation_against_build(annotation, record)
            build_ok, build_code = True, None
        except rfb.BenchmarkError as exc:
            build_ok, build_code = False, exc.code
        findings.add(
            check="human_annotation_binds_build",
            pair_id=pair_id,
            ok=build_ok,
            code=f"v3_review_annotation_build_binding:{build_code}",
            detail="section hashes match and every label references a known unit"
            if build_ok
            else f"build binding failed [{build_code}]",
        )
        unit_inventory, problems = canonical_unit_inventory(record)
        closure = check_subject_closure(annotation["labels"], unit_inventory)
        for check, code, key, message in (
            (
                "unit_inventory_closed",
                "v3_review_unit_uncovered",
                "uncovered",
                "canonical unit identities carrying no label",
            ),
            (
                "unit_covered_exactly_once",
                "v3_review_unit_multiply_covered",
                "multiply_covered",
                "canonical unit identities bound by more than one label",
            ),
            (
                "no_duplicate_canonical_subject",
                "v3_review_duplicate_canonical_subject",
                "duplicate_subjects",
                "canonical subjects bound by more than one label",
            ),
            (
                "no_unknown_unit_identity",
                "v3_review_unknown_unit_identity",
                "unknown_units",
                "references that are not unit identities in this build",
            ),
            (
                "subject_is_not_unit_key_only",
                "v3_review_unit_key_only_subject",
                "unit_key_only",
                "references that are a bare normalized unit key, not a "
                "side:sequence:unit_key occurrence",
            ),
            (
                "unit_identity_side_matches",
                "v3_review_unit_side_mismatch",
                "side_mismatch",
                "references naming the wrong side",
            ),
        ):
            defects = closure[key]
            findings.add(
                check=check,
                pair_id=pair_id,
                ok=not defects,
                code=code,
                detail=f"no {message}"
                if not defects
                else f"{len(defects)} {message}: {bounded(defects)}",
            )
        findings.add(
            check="build_unit_identities_wellformed",
            pair_id=pair_id,
            ok=not problems,
            code="v3_review_build_unit_identity_invalid",
            detail="every build unit id agrees with its own position metadata"
            if not problems
            else f"build units with inconsistent identity metadata: {bounded(problems)}",
        )

    canonical = sorted(
        label["label_id"]
        for label in annotation["labels"]
        if label["label_id"]
        != rfb.label_id_for(
            pair_id, label["previous_unit_id"], label["current_unit_id"]
        )
    )
    findings.add(
        check="label_ids_canonical",
        pair_id=pair_id,
        ok=not canonical,
        code="v3_review_label_id_not_canonical",
        detail="every label_id is the canonical id for its unit binding"
        if not canonical
        else f"label ids not derived from their bindings: {bounded(canonical)}",
    )

    _check_label_completeness(findings, pair_id=pair_id, annotation=annotation)
    _check_annotation_hygiene(
        findings, pair_id=pair_id, annotation=annotation, packet=packet
    )


def _check_template_integrity(
    findings: Findings,
    *,
    pair_id: str,
    raw: Mapping[str, Any],
    row: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> None:
    """An untouched template: exact keys, bindings intact, decisions empty."""
    expected_keys = set(rfb._ANNOTATION_REQUIRED) | set(rfb._ANNOTATION_OPTIONAL)
    unexpected = sorted(set(raw) - expected_keys)
    missing = sorted(set(rfb._ANNOTATION_REQUIRED) - set(raw))
    label_defects = [
        f"labels[{index}]"
        for index, label in enumerate(raw.get("labels", []))
        if not isinstance(label, dict) or set(label) != set(rfb._LABEL_REQUIRED)
    ]
    findings.add(
        check="template_keys_exact",
        pair_id=pair_id,
        ok=not unexpected and not missing and not label_defects,
        code="v3_review_template_keys_invalid",
        detail="template carries exactly the annotation schema keys"
        if not unexpected and not missing and not label_defects
        else (
            f"template keys drifted — unexpected: {bounded(unexpected) or 'none'};"
            f" missing: {bounded(missing) or 'none'};"
            f" labels: {bounded(label_defects) or 'ok'}"
        ),
    )
    bound_ok = (
        raw.get("benchmark_id") == BENCHMARK_ID
        and raw.get("pair_id") == pair_id
        and raw.get("annotator_id") is None
        and raw.get("verification_timestamp") is None
        and raw.get("source_manifest_hash")
        == inventory.get("build_source_manifest_hash")
        and raw.get("previous_section_hash") == row.get("previous_section_hash")
        and raw.get("current_section_hash") == row.get("current_section_hash")
    )
    findings.add(
        check="template_bindings_intact",
        pair_id=pair_id,
        ok=bound_ok,
        code="v3_review_template_corrupt",
        detail="template bindings are intact and unattributed"
        if bound_ok
        else "template bindings drifted or carry reviewer identity",
    )


def _check_label_completeness(
    findings: Findings, *, pair_id: str, annotation: Mapping[str, Any]
) -> None:
    """Admission-gate rules on top of the frozen schema.

    These are consistency rules, not semantic judgements: they say a decision
    must be complete and internally coherent, never what the decision should
    be.
    """
    missing_confidence = sorted(
        label["label_id"]
        for label in annotation["labels"]
        if label["confidence"] not in rfb.CONFIDENCE_LEVELS
    )
    findings.add(
        check="every_label_carries_confidence",
        pair_id=pair_id,
        ok=not missing_confidence,
        code="v3_review_label_confidence_missing",
        detail="every label carries a declared confidence level"
        if not missing_confidence
        else f"labels without a confidence level: {bounded(missing_confidence)}",
    )

    missing_reason = sorted(
        label["label_id"]
        for label in annotation["labels"]
        if label["expected_change_type"] == "undetermined"
        and not (
            isinstance(label["expected_reason_code"], str)
            and label["expected_reason_code"].strip()
        )
    )
    findings.add(
        check="undetermined_labels_carry_a_reason",
        pair_id=pair_id,
        ok=not missing_reason,
        code="v3_review_undetermined_reason_missing",
        detail="every undetermined label carries an explicit reason code"
        if not missing_reason
        else f"undetermined labels with no reason code: {bounded(missing_reason)}",
    )

    evidence_defects: list[str] = []
    for label in annotation["labels"]:
        side = label["expected_evidence_side"]
        if side not in rfb.EVIDENCE_SIDES:
            evidence_defects.append(label["label_id"])
            continue
        if side == "none":
            if label["expected_change_type"] != "undetermined":
                evidence_defects.append(label["label_id"])
            continue
        required = _EVIDENCE_SIDE_REQUIREMENTS[side]
        if any(label[field] is None for field in required):
            evidence_defects.append(label["label_id"])
    findings.add(
        check="evidence_references_consistent_with_bindings",
        pair_id=pair_id,
        ok=not evidence_defects,
        code="v3_review_evidence_reference_invalid",
        detail="every label's evidence side names sides the label actually binds"
        if not evidence_defects
        else "labels whose evidence side is missing, unknown, or names a side "
        f"the label does not bind: {bounded(sorted(set(evidence_defects)))}",
    )

    direction_defects = sorted(
        label["label_id"]
        for label in annotation["labels"]
        if label["expected_direction"] is not None
        and (label["previous_unit_id"] is None or label["current_unit_id"] is None)
    )
    findings.add(
        check="direction_only_where_both_sides_bind",
        pair_id=pair_id,
        ok=not direction_defects,
        code="v3_review_direction_without_both_sides",
        detail="a direction is claimed only where a label binds both a previous "
        "and a current occurrence"
        if not direction_defects
        else f"labels claiming a direction with only one side bound: "
        f"{bounded(direction_defects)}",
    )


def _check_annotation_hygiene(
    findings: Findings,
    *,
    pair_id: str,
    annotation: Mapping[str, Any],
    packet: Mapping[str, Any] | None,
) -> None:
    pool = packet_excerpt_pool(packet or {})
    copied = sorted(
        label["label_id"]
        for label in annotation["labels"]
        if label["reviewer_note"] and note_copies_excerpt(label["reviewer_note"], pool)
    )
    findings.add(
        check="no_filing_excerpts_in_annotation",
        pair_id=pair_id,
        ok=not copied,
        code="v3_review_note_copies_excerpt",
        detail="reviewer notes are bounded and copy no filing excerpt"
        if not copied
        else f"reviewer notes copy packet excerpts on: {bounded(copied)}",
    )
    sensitive: list[str] = []
    annotator = annotation.get("annotator_id")
    if isinstance(annotator, str) and text_sensitive_reason(annotator):
        sensitive.append("annotator_id")
    for label in annotation["labels"]:
        note = label["reviewer_note"]
        if isinstance(note, str) and text_sensitive_reason(note):
            sensitive.append(label["label_id"])
    findings.add(
        check="no_sensitive_material_in_annotation",
        pair_id=pair_id,
        ok=not sensitive,
        code="v3_review_annotation_sensitive_material",
        detail="no absolute paths or credential material in free-text fields"
        if not sensitive
        else "absolute paths or credential material found in: "
        + bounded(sorted(set(sensitive))),
    )


# --- Orchestration ------------------------------------------------------------

MODE_WORKSPACE = "workspace"
MODE_PAIR = "pair"
MODE_CORPUS = "corpus"


def _load_optional(path: Path, what: str) -> dict[str, Any] | None:
    return load_json(path, what) if path.exists() else None


def run_preflight(
    *, layout: rfb.CorpusLayout, artifacts: Mapping[str, Any]
) -> tuple[Findings, dict[str, dict[str, Any]]]:
    """Pre-review integrity of the FROZEN inputs only.

    Deliberately excludes the queue and the review records, because this is
    what preparation must pass before either of those exists. Returns the
    findings plus the loaded packet/build/proposal for each review-ready pair.
    """
    findings = Findings()
    check_frozen_chain(findings, artifacts)
    check_inventory_pair_set(findings, artifacts)
    loaded: dict[str, dict[str, Any]] = {}
    for row in inventory_rows(artifacts["inventory"]):
        pair_id = str(row["pair_id"])
        if row.get("packet_status") != "written":
            check_blocked_pair(findings, row=row, layout=layout)
            continue
        loaded[pair_id] = check_pair_workspace(
            findings, row=row, layout=layout, artifacts=artifacts
        )
    return findings, loaded


def run_validation(
    *,
    layout: rfb.CorpusLayout,
    manifest_path: Path,
    report_dir: Path,
    mode: str,
    pair_id: str | None = None,
) -> Findings:
    """Validate the review workspace. Strictly read-only in every mode.

    ``workspace`` checks pre-review integrity; ``pair`` additionally requires
    one named packet to be reviewer-completed and admitted; ``corpus`` requires
    every review-ready pair to be admitted. No mode edits a file, chooses a
    label, or computes a metric.
    """
    artifacts = load_committed_artifacts(manifest_path, report_dir)
    inventory = artifacts["inventory"]
    findings = Findings()
    check_frozen_chain(findings, artifacts)
    check_inventory_pair_set(findings, artifacts)

    if mode == MODE_PAIR:
        known = {str(row["pair_id"]) for row in inventory_rows(inventory)}
        if pair_id not in known:
            raise HumanReviewError(
                "v3_review_unknown_pair",
                f"{pair_id!r} is not a pair in the committed packet inventory",
            )
        if pair_id in {str(row["pair_id"]) for row in blocked_rows(inventory)}:
            raise HumanReviewError(
                "v3_review_pair_blocked",
                f"{pair_id!r} is a blocked pair: it has no packet and receives "
                "no annotation",
            )

    queue_path = review_queue_path(layout)
    queue = _load_optional(queue_path, "review queue")
    if queue is None:
        findings.add(
            check="review_queue_present",
            pair_id=None,
            ok=False,
            code="v3_review_queue_missing",
            detail="no local review queue; run the preparation command first",
        )
    else:
        check_queue(findings, queue=queue, inventory=inventory)

    for row in inventory_rows(inventory):
        current = str(row["pair_id"])
        if row.get("packet_status") != "written":
            check_blocked_pair(findings, row=row, layout=layout)
            continue
        loaded = check_pair_workspace(
            findings, row=row, layout=layout, artifacts=artifacts
        )
        review_record = _load_optional(
            review_record_path(layout, current), f"{current} review record"
        )
        if review_record is None:
            findings.add(
                check="review_record_present",
                pair_id=current,
                ok=False,
                code="v3_review_record_missing",
                detail="no review record; run the preparation command first",
            )
        else:
            check_review_record(
                findings,
                pair_id=current,
                review_record=review_record,
                row=row,
                layout=layout,
                inventory=inventory,
                proposal=loaded["proposal"],
            )

        require_admitted = mode == MODE_CORPUS or (
            mode == MODE_PAIR and current == pair_id
        )
        if require_admitted and review_record is not None:
            human_path = human_review_path(layout, current)
            check_reviewer_completion(
                findings,
                pair_id=current,
                review_record=review_record,
                human=_load_optional(human_path, f"{current} human annotation"),
            )
        check_completed_annotation(
            findings,
            pair_id=current,
            layout=layout,
            row=row,
            inventory=inventory,
            packet=loaded["packet"],
            record=loaded["record"],
            require_admitted=require_admitted,
        )

    check_directories_closed(findings, layout=layout, inventory=inventory)
    return findings
