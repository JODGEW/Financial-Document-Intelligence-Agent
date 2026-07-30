"""Controlled regression suite + CI gate for the Item 1A comparison workflow.

    python scripts/eval_comparison_regression.py            # human summary, gates enforced
    python scripts/eval_comparison_regression.py --json      # machine-readable report
    python scripts/eval_comparison_regression.py --report out.json
    python scripts/eval_comparison_regression.py --write-baseline   # explicit, refused in CI

Runs every labeled fixture in ``tests/fixtures/comparison_regression_labels.json``
end to end — filing registry -> index -> comparison record -> deterministic
detection -> governance routing -> review decision -> release-gated export —
scores it against human-written labels, and exits nonzero when any gate fails.

Scope honesty: the fixtures are CONTROLLED SYNTHETIC documents (fictional
companies, hand-written or hand-assembled chunk fixtures). The suite protects
deterministic behavior from silent drift. It is NOT a real-filing benchmark:
no metric here is evidence of accuracy on real SEC filings.

Offline by construction
-----------------------
Temporary registry, index, and SQLite locations per fixture; no AWS
credentials, no Bedrock, no embeddings of fixture text at query time (the
detector reads by metadata), no network. Seeding uses a deterministic local
fake embedding function purely so Chroma will accept documents — detection
never embeds, and this module asserts that by raising if a query embedding is
ever requested.

Nothing tracked is written during a normal run: reports go to stdout or an
explicit ``--report`` path, and the baseline is rewritten only under
``--write-baseline`` (which refuses to run in CI).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import comparison_detector  # noqa: E402
import comparison_export  # noqa: E402
import comparison_governance  # noqa: E402
import comparison_review  # noqa: E402
import comparison_store  # noqa: E402
import comparison_validators  # noqa: E402
import config  # noqa: E402
import filing_registry  # noqa: E402
import ingest  # noqa: E402
from governance.comparison_export_schema import EXPORT_SCHEMA_VERSION  # noqa: E402
from governance.comparison_schema import (  # noqa: E402
    CHANGE_TYPES,
    COMPARISON_SCHEMA_VERSION,
    SECTION_ITEM_1A,
)

LABEL_SCHEMA_VERSION = "comparison-regression.v1"
LABELS_PATH = _REPO_ROOT / "tests" / "fixtures" / "comparison_regression_labels.json"
FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "comparison_regression"
DEFAULT_BASELINE = _REPO_ROOT / "eval" / "comparison_regression_baseline.json"

# --- Gates -------------------------------------------------------------------
# These fixtures are controlled and the workflow is fully deterministic, so
# every gate is exact. A gate is not a tuning knob: lowering one to admit a
# failing case requires a written rationale in the same reviewed diff.
GATES: dict[str, tuple[str, float]] = {
    "change_precision": (">=", 1.0),
    "change_recall": (">=", 1.0),
    "change_type_accuracy": (">=", 1.0),
    "evidence_resolution_rate": (">=", 1.0),
    "unchanged_false_positive_rate": ("<=", 0.0),
    "undetermined_reason_accuracy": (">=", 1.0),
    "lifecycle_accuracy": (">=", 1.0),
    "governance_decision_accuracy": (">=", 1.0),
    "release_eligibility_accuracy": (">=", 1.0),
    "deterministic_result_rate": (">=", 1.0),
}

# A rate whose denominator is zero asserts nothing, so it is reported as null
# with its denominator visible and treated as PASSING — never as 0.0 (which
# would fail a >= gate) and never silently dropped from the report.
ZERO_DENOMINATOR_POLICY = "null_value_gate_passes"

_REVIEWER_ID = "regression-suite@localhost"  # self-asserted local metadata
_APPROVE_NOTE = "Controlled regression fixture: approved to exercise release."
_REJECT_NOTE = "Controlled regression fixture: rejected to exercise refusal."


class LabelError(Exception):
    """The label file is invalid or self-contradictory."""


class FixtureRunError(Exception):
    """A fixture could not be run to a scoreable outcome."""


# --- Label validation --------------------------------------------------------


def load_labels(path: str | Path = LABELS_PATH) -> dict[str, Any]:
    """Load and fully validate the label contract."""
    labels = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_labels(labels)
    return labels


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LabelError(message)


def validate_labels(labels: dict[str, Any]) -> None:
    """Reject every documented label defect (see the module docstring's B)."""
    _require(
        labels.get("label_schema_version") == LABEL_SCHEMA_VERSION,
        f"label_schema_version must be {LABEL_SCHEMA_VERSION!r}, got "
        f"{labels.get('label_schema_version')!r}",
    )
    fixtures = labels.get("fixtures")
    _require(
        isinstance(fixtures, list) and bool(fixtures),
        "labels must carry a non-empty 'fixtures' list",
    )

    seen_ids: set[str] = set()
    for fixture in fixtures:
        fixture_id = fixture.get("fixture_id")
        _require(
            isinstance(fixture_id, str) and bool(fixture_id.strip()),
            "every fixture needs a non-empty fixture_id",
        )
        _require(fixture_id not in seen_ids, f"duplicate fixture_id {fixture_id!r}")
        seen_ids.add(fixture_id)
        _require(
            fixture.get("kind") in ("corpus", "chunks"),
            f"{fixture_id}: kind must be 'corpus' or 'chunks'",
        )
        _require(
            fixture.get("synthetic") is True,
            f"{fixture_id}: every fixture must be labeled synthetic",
        )
        _require(
            fixture.get("expected_lifecycle") in ("detected", "failed"),
            f"{fixture_id}: expected_lifecycle must be 'detected' or 'failed'",
        )

        previous = fixture.get("previous_filing") or {}
        current = fixture.get("current_filing") or {}
        for side, identity in (("previous", previous), ("current", current)):
            for field in ("filing_id", "company_key", "form_type", "period_end"):
                _require(
                    isinstance(identity.get(field), str)
                    and bool(identity[field].strip()),
                    f"{fixture_id}: {side}_filing.{field} is required",
                )
        _require(
            previous["filing_id"] != current["filing_id"],
            f"{fixture_id}: a comparison needs two distinct filings",
        )
        _require(
            previous["company_key"] == current["company_key"],
            f"{fixture_id}: both filings must share a company_key",
        )
        _require(
            previous["form_type"] == current["form_type"],
            f"{fixture_id}: both filings must share a form_type",
        )
        _require(
            previous["period_end"] < current["period_end"],
            f"{fixture_id}: previous period_end must be strictly before current "
            f"({previous['period_end']} vs {current['period_end']})",
        )

        seen_keys: set[tuple[str, str]] = set()
        for change in fixture.get("expected_changes") or []:
            unit_key = change.get("unit_key")
            change_type = change.get("change_type")
            _require(
                isinstance(unit_key, str) and bool(unit_key.strip()),
                f"{fixture_id}: every expected change needs a unit_key",
            )
            _require(
                change_type in CHANGE_TYPES,
                f"{fixture_id}: unsupported change_type {change_type!r} "
                f"(supported: {list(CHANGE_TYPES)})",
            )
            key = (unit_key, change_type)
            _require(
                key not in seen_keys,
                f"{fixture_id}: duplicate expected change key {key}",
            )
            seen_keys.add(key)
            sides = change.get("evidence_sides")
            _require(
                isinstance(sides, list)
                and all(side in ("previous", "current") for side in sides),
                f"{fixture_id}/{unit_key}: evidence_sides must list "
                "'previous' and/or 'current'",
            )
            expected_sides = {
                "modified": ["previous", "current"],
                "added": ["current"],
                "removed": ["previous"],
            }
            if change_type in expected_sides:
                _require(
                    sides == expected_sides[change_type],
                    f"{fixture_id}/{unit_key}: change_type "
                    f"'{change_type}' requires evidence_sides "
                    f"{expected_sides[change_type]}, got {sides}",
                )
                _require(
                    change.get("undetermined_reason_code") is None,
                    f"{fixture_id}/{unit_key}: only undetermined changes carry "
                    "an undetermined_reason_code",
                )
            else:
                _require(
                    isinstance(change.get("undetermined_reason_code"), str)
                    and bool(change["undetermined_reason_code"].strip()),
                    f"{fixture_id}/{unit_key}: undetermined changes require an "
                    "undetermined_reason_code",
                )

        unchanged_keys: set[str] = set()
        for unit in fixture.get("expected_unchanged_units") or []:
            unit_key = unit.get("unit_key")
            _require(
                isinstance(unit_key, str) and bool(unit_key.strip()),
                f"{fixture_id}: every unchanged unit needs a unit_key",
            )
            _require(
                unit_key not in unchanged_keys,
                f"{fixture_id}: duplicate expected unchanged unit {unit_key!r}",
            )
            _require(
                unit_key not in {key for key, _type in seen_keys},
                f"{fixture_id}: {unit_key!r} is labeled both changed and "
                "unchanged",
            )
            unchanged_keys.add(unit_key)

        decision = fixture.get("expected_governance_decision")
        _require(
            decision is None
            or decision in ("returned", "returned_with_warning", "held_for_review"),
            f"{fixture_id}: expected_governance_decision must be a policy "
            f"decision or null, got {decision!r}",
        )
        _validate_release_labels(fixture_id, fixture, decision)


def _validate_release_labels(
    fixture_id: str, fixture: dict[str, Any], decision: str | None
) -> None:
    """Release labels must not contradict the governance decision they follow."""
    release = fixture.get("expected_release")
    if release is None:
        return
    eligible_before = release.get("eligible_before_review")
    _require(
        isinstance(eligible_before, bool),
        f"{fixture_id}: expected_release.eligible_before_review must be a boolean",
    )
    review_decision = release.get("review_decision")
    _require(
        review_decision in (None, "approved", "rejected"),
        f"{fixture_id}: expected_release.review_decision must be null, "
        "'approved', or 'rejected'",
    )
    eligible_after = release.get("eligible_after_review")
    basis = release.get("release_basis")

    if decision == "held_for_review":
        _require(
            eligible_before is False,
            f"{fixture_id}: a held comparison cannot be release-eligible "
            "before its review decision",
        )
        _require(
            review_decision is not None,
            f"{fixture_id}: a held comparison needs a labeled review_decision",
        )
        _require(
            isinstance(eligible_after, bool),
            f"{fixture_id}: a held comparison needs a labeled "
            "eligible_after_review",
        )
        _require(
            eligible_after == (review_decision == "approved"),
            f"{fixture_id}: only an approved review makes a held comparison "
            "release-eligible",
        )
        if eligible_after:
            _require(
                basis == "approved_after_review",
                f"{fixture_id}: an approved held comparison releases under "
                "'approved_after_review'",
            )
        else:
            _require(
                basis is None,
                f"{fixture_id}: an ineligible comparison must not name a "
                "release_basis",
            )
    elif decision in ("returned", "returned_with_warning"):
        _require(
            eligible_before is True,
            f"{fixture_id}: policy returned this comparison, so it is "
            "release-eligible without a human approval",
        )
        _require(
            review_decision is None and eligible_after is None,
            f"{fixture_id}: a returned comparison has no review item — "
            "review_decision and eligible_after_review must be null",
        )
        expected_basis = (
            "returned_by_policy"
            if decision == "returned"
            else "returned_with_warning_by_policy"
        )
        _require(
            basis == expected_basis,
            f"{fixture_id}: decision '{decision}' releases under "
            f"'{expected_basis}', got {basis!r}",
        )


# --- Fixture environment -----------------------------------------------------


class _FixtureEmbeddings:
    """Deterministic offline embeddings used only to seed Chroma.

    Detection reads by metadata, so a query embedding is never legitimate:
    asking for one raises rather than silently falling back to a vector search.
    """

    def embed_documents(self, texts):
        return [[float(len(text) % 7), 1.0] for text in texts]

    def embed_query(self, text):
        raise AssertionError(
            "the comparison workflow must never embed a query; detection "
            "reads sections by metadata"
        )


def _seed_chunk_fixture(fixture_path: Path, workdir: Path):
    """Build a temp registry + Chroma index from a parsed-chunk fixture."""
    from langchain_chroma import Chroma
    from langchain_core.documents import Document

    spec = json.loads(fixture_path.read_text(encoding="utf-8"))
    registry = workdir / "registry.jsonl"
    documents: list[Document] = []
    ids: list[str] = []

    for filing in spec["filings"]:
        source_name = filing["source_name"]
        form_type = filing_registry.normalize_form_type(spec["form_type"])
        for chunk in filing["chunks"]:
            metadata = {
                "source": f"/fixture/{source_name}",
                "source_name": source_name,
                "source_path": source_name,
                "filing_id": filing["filing_id"],
                "document_id": spec["document_family_id"],
                "company": spec["company_name"],
                "company_key": spec["company_key"],
                "filing_type": form_type,
                "chunk_seq": chunk["chunk_seq"],
                "page": chunk["page"],
                "section_title": chunk["section_title"],
            }
            if chunk.get("section_key"):
                metadata["section_key"] = chunk["section_key"]
            documents.append(Document(page_content=chunk["text"], metadata=metadata))
            # Same shape as ingest.chunk_id: source:page:sha1(content)[:12].
            ids.append(
                f"{source_name}:{chunk['page']}:"
                + hashlib.sha1(chunk["text"].encode("utf-8")).hexdigest()[:12]
            )

        filing_registry.record_outcome(
            registry,
            source_path=source_name,
            source_name=source_name,
            source_hash=filing["source_hash"],
            parse_status=filing_registry.PARSED,
            document_family_id=spec["document_family_id"],
            filing_id=filing["filing_id"],
            company_key=spec["company_key"],
            company_name=spec["company_name"],
            form_type=form_type,
            period_end=filing["period_end"],
            identity_source="manifest",
            loader="regression_fixture",
            chunk_count=len(filing["chunks"]),
        )

    chroma = Chroma(
        collection_name="regression_fixture",
        persist_directory=str(workdir / "chroma"),
        embedding_function=_FixtureEmbeddings(),
    )
    chroma.add_documents(documents=documents, ids=ids)
    return registry, chroma


def _seed_corpus_fixture(workdir: Path):
    """Build a temp registry + Chroma index from the controlled docs/ corpus."""
    from langchain_chroma import Chroma

    registry = workdir / "registry.jsonl"
    documents = ingest.load_documents(
        config.DOCS_DIR,
        manifest=filing_registry.load_manifest(),
        registry_path=registry,
    )
    chunks = ingest.split_documents(documents)
    unique, ids = ingest._dedupe_by_id(chunks)
    counts: dict[str, int] = {}
    for chunk in unique:
        rel = chunk.metadata.get("source_path")
        counts[rel] = counts.get(rel, 0) + 1
    filing_registry.update_chunk_counts(counts, registry)

    chroma = Chroma(
        collection_name="regression_corpus",
        persist_directory=str(workdir / "chroma"),
        embedding_function=_FixtureEmbeddings(),
    )
    chroma.add_documents(documents=unique, ids=ids)
    return registry, chroma


def _seed(fixture: dict[str, Any], workdir: Path):
    """Seed one fixture's registry + index, keeping stdout clean.

    ``ingest`` prints per-file progress; swallowing it here keeps ``--json``
    output parseable without the caller stripping a preamble.
    """
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        if fixture["kind"] == "corpus":
            return _seed_corpus_fixture(workdir)
        return _seed_chunk_fixture(_fixture_file(fixture), workdir)


def _fixture_file(fixture: dict[str, Any]) -> Path:
    """The chunk-fixture file backing this label entry."""
    return FIXTURES_DIR / (fixture["fixture_id"].replace("-", "_") + ".json")


def fixture_input_hashes(labels: dict[str, Any]) -> dict[str, str]:
    """sha256 of every tracked input the metrics depend on. Names only, never
    paths — the report must carry no absolute or temporary location."""
    hashes = {"labels": _sha256_file(LABELS_PATH)}
    for fixture in labels["fixtures"]:
        if fixture["kind"] == "chunks":
            hashes[fixture["fixture_id"]] = _sha256_file(_fixture_file(fixture))
        else:
            digest = hashlib.sha256()
            for name in sorted(
                path.name for path in Path(config.DOCS_DIR).glob("acme-corp-10k-*")
            ):
                digest.update(name.encode("utf-8"))
                digest.update((Path(config.DOCS_DIR) / name).read_bytes())
            hashes[fixture["fixture_id"]] = digest.hexdigest()
    return hashes


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Fixture execution -------------------------------------------------------


def run_fixture(fixture: dict[str, Any], workdir: Path) -> dict[str, Any]:
    """Run one fixture end to end from clean state; return its raw outcome.

    Scoring happens later and separately: this function records what the
    workflow DID, never what the labels expected.
    """
    registry, chroma = _seed(fixture, workdir)
    db = workdir / "comparisons.db"
    previous_id = fixture["previous_filing"]["filing_id"]
    current_id = fixture["current_filing"]["filing_id"]

    record, created_record = comparison_store.create_comparison(
        previous_id, current_id, db_path=db, registry_path=registry
    )
    comparison_id = record["comparison_id"]

    outcome: dict[str, Any] = {
        "fixture_id": fixture["fixture_id"],
        "comparison_id": comparison_id,
        "record_created": created_record,
        "detected": False,
        "changes": [],
        "unresolved_evidence": 0,
        "total_evidence": 0,
        "foreign_evidence": 0,
        "idempotency": {},
        "notes": [],
    }

    try:
        result, detect_created = comparison_detector.detect(
            comparison_id, db_path=db, registry_path=registry, chroma_client=chroma
        )
    except comparison_detector.DetectionError as exc:
        outcome["lifecycle"] = "failed"
        outcome["failure_code"] = exc.code
        return outcome

    stored_result = comparison_store.get_result(comparison_id, db_path=db)
    outcome["detected"] = True
    outcome["lifecycle"] = comparison_store.get_comparison(
        comparison_id, db_path=db
    )["status"]
    outcome["result_hash"] = stored_result["result_hash"]
    outcome["detector_version"] = stored_result["detector_version"]
    outcome["result"] = result
    outcome["changes"] = [
        {
            "change_id": change["change_id"],
            "change_type": change["change_type"],
            "undetermined_reason": change.get("undetermined_reason"),
            "previous_evidence": len(change["previous_evidence"]),
            "current_evidence": len(change["current_evidence"]),
        }
        for change in result["changes"]
    ]
    outcome["validation_summary"] = result["validation_summary"]
    outcome["validator_versions"] = sorted(
        {
            check["validator_version"]
            for change in result["changes"]
            for check in change["validation"]
            if check.get("validator_version")
        }
    )

    # Evidence ownership + resolution against the fixture's own index.
    for change in result["changes"]:
        for side, filing_id in (
            ("previous_evidence", previous_id),
            ("current_evidence", current_id),
        ):
            for ref in change[side]:
                outcome["total_evidence"] += 1
                if ref["document_id"] != filing_id:
                    outcome["foreign_evidence"] += 1
                got = chroma.get(ids=[ref["chunk_id"]])
                if got.get("ids") != [ref["chunk_id"]]:
                    outcome["unresolved_evidence"] += 1

    # Detection idempotency: same inputs, same stored result, no new row.
    replay, replay_created = comparison_detector.detect(
        comparison_id, db_path=db, registry_path=registry, chroma_client=chroma
    )
    outcome["idempotency"]["detect_created_again"] = replay_created
    outcome["idempotency"]["detect_result_stable"] = replay == result

    _run_governance_and_release(fixture, outcome, comparison_id, db)
    with closing(sqlite3.connect(str(db))) as conn:
        outcome["sqlite_integrity"] = conn.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
    return outcome


def _run_governance_and_release(
    fixture: dict[str, Any],
    outcome: dict[str, Any],
    comparison_id: str,
    db: Path,
) -> None:
    """Govern, probe release eligibility, apply the labeled review decision,
    then probe release again. Records observed behavior only."""
    evaluation, evaluation_created = comparison_governance.govern(
        comparison_id, db_path=db
    )
    outcome["governance_decision"] = evaluation["decision"]
    outcome["governance_reason_codes"] = evaluation["reason_codes"]
    outcome["risk_score"] = evaluation["risk_score"]
    outcome["evaluation_id"] = evaluation["evaluation_id"]
    outcome["governed_result_hash"] = evaluation["governed_result_hash"]
    outcome["governed_result"] = evaluation["governed_result"]

    replay_evaluation, replay_created = comparison_governance.govern(
        comparison_id, db_path=db
    )
    outcome["idempotency"]["govern_created_again"] = replay_created
    outcome["idempotency"]["govern_evaluation_stable"] = (
        replay_evaluation["evaluation_id"] == evaluation["evaluation_id"]
        and replay_evaluation["evaluated_at"] == evaluation["evaluated_at"]
    )
    outcome["idempotency"]["govern_created_first"] = evaluation_created

    pending_reviews = comparison_store.list_comparison_reviews(
        db_path=db, comparison_id=comparison_id
    )
    outcome["pending_review_count"] = len(pending_reviews)

    # Every export attempt's created flag: exactly one may ever be True.
    outcome["export_created_flags"] = []

    # Release probe BEFORE any review decision.
    outcome["release_before_review"] = _probe_release(
        comparison_id, evaluation["evaluation_id"], db, outcome
    )

    labeled_decision = (fixture.get("expected_release") or {}).get("review_decision")
    if labeled_decision is not None and pending_reviews:
        review_id = pending_reviews[0]["review_id"]
        action = "approved" if labeled_decision == "approved" else "rejected"
        event, _created = comparison_review.decide(
            review_id,
            action=action,
            reviewer_id=_REVIEWER_ID,
            reason_code=(
                "approved_as_is" if action == "approved" else "rejected_ambiguous_change"
            ),
            reviewer_note=(
                _APPROVE_NOTE if action == "approved" else _REJECT_NOTE
            ),
            db_path=db,
        )
        outcome["review_id"] = review_id
        outcome["review_action"] = event["action"]
        outcome["final_reviewed_result_hash"] = event["final_reviewed_result_hash"]
        outcome["reviewed_result"] = event["reviewed_result"]
        outcome["release_after_review"] = _probe_release(
            comparison_id, evaluation["evaluation_id"], db, outcome
        )

    # Immutability baseline: taken after every governance and review write, so
    # the comparison below isolates what the EXPORT calls did.
    before = _workflow_rows(db)

    export_probe = outcome.get("release_after_review") or outcome[
        "release_before_review"
    ]
    if export_probe["eligible"]:
        # Export idempotency: same id, same payload, same exported_at.
        first, first_created = comparison_export.export_comparison(
            comparison_id, evaluation["evaluation_id"], db_path=db
        )
        second, second_created = comparison_export.export_comparison(
            comparison_id, evaluation["evaluation_id"], db_path=db
        )
        outcome["export_created_flags"] += [first_created, second_created]
        outcome["export_id"] = first["export_id"]
        outcome["export_payload_hash"] = first["export_payload_hash"]
        outcome["export_release_basis"] = first["release_basis"]
        outcome["export_final_result_hash"] = first["final_result_hash"]
        outcome["export_payload"] = first["export"]
        outcome["idempotency"]["export_created_first"] = first_created
        outcome["idempotency"]["export_created_again"] = second_created
        outcome["idempotency"]["export_payload_stable"] = (
            second["export"] == first["export"]
            and second["export"]["exported_at"] == first["export"]["exported_at"]
        )
        # Which snapshot did the export release?
        if "reviewed_result" in outcome:
            outcome["export_selected_reviewed_snapshot"] = (
                first["export"]["comparison_result"] == outcome["reviewed_result"]
            )
        outcome["export_selected_governed_snapshot"] = (
            first["export"]["comparison_result"] == outcome["governed_result"]
        )

    outcome["workflow_rows_unchanged_after_export"] = _workflow_rows(db) == before


def _probe_release(
    comparison_id: str, evaluation_id: str, db: Path, outcome: dict[str, Any]
) -> dict[str, Any]:
    """Ask the real export gate whether this evaluation is releasable.

    Read-only when refused; when eligible it persists the export, which is
    exactly the behavior under test. Every attempt's created flag is recorded
    so the suite can prove exactly one row was ever inserted.
    """
    try:
        record, created = comparison_export.export_comparison(
            comparison_id, evaluation_id, db_path=db
        )
    except comparison_export.ComparisonExportError as exc:
        return {"eligible": False, "code": exc.code, "release_basis": None}
    outcome["export_created_flags"].append(created)
    return {
        "eligible": True,
        "code": None,
        "release_basis": record["release_basis"],
    }


_WORKFLOW_TABLES = (
    "comparison_results",
    "comparison_governance_evaluations",
    "comparison_review_items",
    "comparison_review_events",
)


def _workflow_rows(db: Path) -> dict[str, list[tuple]]:
    """Every row of the tables export must never modify."""
    rows: dict[str, list[tuple]] = {}
    with closing(sqlite3.connect(str(db))) as conn:
        for table in _WORKFLOW_TABLES:
            rows[table] = [
                tuple(row)
                for row in conn.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"  # fixed table names
                ).fetchall()
            ]
    return rows


# --- Scoring -----------------------------------------------------------------


def _unit_key_of(change_id: str, candidate_keys: set[str]) -> str | None:
    """Recover a change's unit key by recomputing the deterministic change_id."""
    for key in sorted(candidate_keys):
        for change_type in CHANGE_TYPES:
            if change_id == comparison_detector._change_id(change_type, key):
                return key
    return None


def score_fixture(fixture: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    """Compare one fixture's observed outcome against its labels."""
    expected = {
        (change["unit_key"], change["change_type"])
        for change in fixture.get("expected_changes") or []
    }
    expected_by_key = {
        change["unit_key"]: change
        for change in fixture.get("expected_changes") or []
    }
    unchanged_keys = {
        unit["unit_key"] for unit in fixture.get("expected_unchanged_units") or []
    }
    candidate_keys = set(expected_by_key) | unchanged_keys

    detected: set[tuple[str, str]] = set()
    detected_keys: set[str] = set()
    reason_results: list[bool] = []
    for change in outcome["changes"]:
        key = _unit_key_of(change["change_id"], candidate_keys)
        label = key if key is not None else f"unlabeled:{change['change_id']}"
        detected.add((label, change["change_type"]))
        detected_keys.add(label)
        if key is not None and key in expected_by_key:
            code = expected_by_key[key].get("undetermined_reason_code")
            if code:
                reason_results.append(
                    (change.get("undetermined_reason") or "").startswith(code)
                )

    true_positives = detected & expected
    matched_expected = [
        (key, change_type)
        for key, change_type in expected
        if key in detected_keys
    ]

    # Evidence-side requirements are asserted per matched expected change.
    side_ok = True
    for change in outcome["changes"]:
        key = _unit_key_of(change["change_id"], candidate_keys)
        if key is None or key not in expected_by_key:
            continue
        wanted = set(expected_by_key[key]["evidence_sides"])
        got = set()
        if change["previous_evidence"]:
            got.add("previous")
        if change["current_evidence"]:
            got.add("current")
        if wanted != got:
            side_ok = False

    lifecycle_ok = outcome.get("lifecycle") == fixture["expected_lifecycle"]

    expected_decision = fixture.get("expected_governance_decision")
    governance_ok = (
        None
        if expected_decision is None
        else outcome.get("governance_decision") == expected_decision
    )

    release_ok = _score_release(fixture, outcome)

    return {
        "fixture_id": fixture["fixture_id"],
        "expected_change_count": len(expected),
        "detected_change_count": len(detected),
        "true_positives": sorted(true_positives),
        "false_positives": sorted(detected - expected),
        "missed": sorted(expected - detected),
        "matched_expected_count": len(matched_expected),
        "type_correct_count": len(
            [pair for pair in matched_expected if pair in detected]
        ),
        "unchanged_units": len(unchanged_keys),
        "unchanged_false_positives": sorted(unchanged_keys & detected_keys),
        "evidence_total": outcome["total_evidence"],
        "evidence_unresolved": outcome["unresolved_evidence"],
        "evidence_foreign": outcome["foreign_evidence"],
        "evidence_sides_ok": side_ok,
        "undetermined_reason_total": len(reason_results),
        "undetermined_reason_correct": sum(1 for ok in reason_results if ok),
        "lifecycle": outcome.get("lifecycle"),
        "lifecycle_ok": lifecycle_ok,
        "governance_decision": outcome.get("governance_decision"),
        "governance_ok": governance_ok,
        "release_ok": release_ok,
        "release_before_review": outcome.get("release_before_review"),
        "release_after_review": outcome.get("release_after_review"),
        "export_release_basis": outcome.get("export_release_basis"),
        "result_hash": outcome.get("result_hash"),
        "risk_score": outcome.get("risk_score"),
        "invariants": _score_invariants(fixture, outcome),
    }


def _score_release(fixture: dict[str, Any], outcome: dict[str, Any]) -> bool | None:
    """All release assertions for one fixture, or None when unlabeled."""
    release = fixture.get("expected_release")
    if release is None:
        return None
    before = outcome.get("release_before_review") or {}
    if before.get("eligible") != release["eligible_before_review"]:
        return False

    if release["review_decision"] is None:
        return (
            before.get("release_basis") == release["release_basis"]
            and outcome.get("export_release_basis") == release["release_basis"]
            and outcome.get("export_selected_governed_snapshot") is True
        )

    after = outcome.get("release_after_review") or {}
    if after.get("eligible") != release["eligible_after_review"]:
        return False
    if not release["eligible_after_review"]:
        return after.get("code") == "review_rejected"
    return (
        after.get("release_basis") == release["release_basis"]
        and outcome.get("export_release_basis") == release["release_basis"]
        and outcome.get("export_selected_reviewed_snapshot") is True
    )


def _score_invariants(
    fixture: dict[str, Any], outcome: dict[str, Any]
) -> dict[str, bool]:
    """The end-to-end workflow invariants that are not rate metrics."""
    idempotency = outcome.get("idempotency", {})
    invariants = {
        "detect_idempotent": idempotency.get("detect_created_again") is False
        and idempotency.get("detect_result_stable") is True,
        "govern_idempotent": idempotency.get("govern_created_again") is False
        and idempotency.get("govern_evaluation_stable") is True,
        "evidence_ownership_correct": outcome.get("foreign_evidence") == 0,
        "evidence_sides_correct": True,  # replaced by the caller's side check
        "sqlite_integrity_ok": outcome.get("sqlite_integrity") == "ok",
        "workflow_rows_unchanged_after_export": outcome.get(
            "workflow_rows_unchanged_after_export"
        )
        is True,
        "no_not_run_checks": (outcome.get("validation_summary") or {}).get(
            "not_run", 0
        )
        == 0,
    }
    expected_decision = fixture.get("expected_governance_decision")
    if expected_decision == "held_for_review":
        invariants["exactly_one_pending_review"] = (
            outcome.get("pending_review_count") == 1
        )
    else:
        invariants["no_review_item_created"] = (
            outcome.get("pending_review_count") == 0
        )
    if "export_id" in outcome:
        invariants["export_idempotent"] = (
            idempotency.get("export_created_again") is False
            and idempotency.get("export_payload_stable") is True
        )
        # Across every export attempt in this fixture's run, exactly one row
        # may have been inserted.
        invariants["export_persisted_exactly_once"] = (
            sum(1 for created in outcome["export_created_flags"] if created) == 1
        )
    else:
        invariants["no_export_persisted"] = not any(
            outcome.get("export_created_flags") or []
        )
    return invariants


def _rate(numerator: int, denominator: int, name: str) -> dict[str, Any]:
    """A rate with its denominator visible and zero-denominator made explicit."""
    if denominator == 0:
        return {
            "metric": name,
            "value": None,
            "numerator": numerator,
            "denominator": 0,
            "zero_denominator": True,
            "zero_denominator_policy": ZERO_DENOMINATOR_POLICY,
        }
    return {
        "metric": name,
        "value": round(numerator / denominator, 6),
        "numerator": numerator,
        "denominator": denominator,
        "zero_denominator": False,
    }


def aggregate_metrics(
    scores: list[dict[str, Any]], determinism: dict[str, bool]
) -> dict[str, dict[str, Any]]:
    """Suite-level metrics with every denominator stated explicitly."""
    tp = sum(len(score["true_positives"]) for score in scores)
    detected = sum(score["detected_change_count"] for score in scores)
    expected = sum(score["expected_change_count"] for score in scores)
    matched = sum(score["matched_expected_count"] for score in scores)
    type_correct = sum(score["type_correct_count"] for score in scores)
    evidence_total = sum(score["evidence_total"] for score in scores)
    evidence_resolved = sum(
        score["evidence_total"] - score["evidence_unresolved"] - score["evidence_foreign"]
        for score in scores
    )
    unchanged_total = sum(score["unchanged_units"] for score in scores)
    unchanged_fp = sum(len(score["unchanged_false_positives"]) for score in scores)
    reason_total = sum(score["undetermined_reason_total"] for score in scores)
    reason_correct = sum(score["undetermined_reason_correct"] for score in scores)
    lifecycle_ok = sum(1 for score in scores if score["lifecycle_ok"])
    governance_scored = [
        score for score in scores if score["governance_ok"] is not None
    ]
    release_scored = [score for score in scores if score["release_ok"] is not None]

    return {
        "change_precision": _rate(tp, detected, "change_precision"),
        "change_recall": _rate(tp, expected, "change_recall"),
        "change_type_accuracy": _rate(type_correct, matched, "change_type_accuracy"),
        "evidence_resolution_rate": _rate(
            evidence_resolved, evidence_total, "evidence_resolution_rate"
        ),
        "unchanged_false_positive_rate": _rate(
            unchanged_fp, unchanged_total, "unchanged_false_positive_rate"
        ),
        "undetermined_reason_accuracy": _rate(
            reason_correct, reason_total, "undetermined_reason_accuracy"
        ),
        "lifecycle_accuracy": _rate(lifecycle_ok, len(scores), "lifecycle_accuracy"),
        "governance_decision_accuracy": _rate(
            sum(1 for score in governance_scored if score["governance_ok"]),
            len(governance_scored),
            "governance_decision_accuracy",
        ),
        "release_eligibility_accuracy": _rate(
            sum(1 for score in release_scored if score["release_ok"]),
            len(release_scored),
            "release_eligibility_accuracy",
        ),
        "deterministic_result_rate": _rate(
            sum(1 for stable in determinism.values() if stable),
            len(determinism),
            "deterministic_result_rate",
        ),
    }


def evaluate_gates(metrics: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply every configured gate; a null (zero-denominator) metric passes."""
    results = []
    for name, (comparison, threshold) in GATES.items():
        metric = metrics[name]
        value = metric["value"]
        if value is None:
            passed = True
            note = "zero denominator: metric asserts nothing and cannot fail"
        elif comparison == ">=":
            passed = value >= threshold
            note = None
        else:
            passed = value <= threshold
            note = None
        results.append(
            {
                "gate": name,
                "comparison": comparison,
                "threshold": threshold,
                "value": value,
                "denominator": metric["denominator"],
                "passed": passed,
                "note": note,
            }
        )
    return results


# --- Determinism -------------------------------------------------------------

# Keys whose values legitimately differ between two runs (wall-clock stamps)
# and the hashes computed over documents that contain them. Determinism is
# asserted on everything else, plus on the detector's own
# timestamp-independent result_hash, which must be byte-equal across runs.
_TIMESTAMP_KEYS = frozenset(
    {"created_at", "evaluated_at", "exported_at", "decided_at", "updated_at"}
)
_TIMESTAMP_DERIVED_HASHES = frozenset(
    {
        "governed_result_hash",
        "final_result_hash",
        "final_reviewed_result_hash",
        "original_governed_result_hash",
        "export_payload_hash",
        "export_id",
    }
)


def timestamp_independent(value: Any) -> Any:
    """Recursively drop wall-clock stamps and the hashes computed over them."""
    if isinstance(value, dict):
        return {
            key: timestamp_independent(item)
            for key, item in sorted(value.items())
            if key not in _TIMESTAMP_KEYS and key not in _TIMESTAMP_DERIVED_HASHES
        }
    if isinstance(value, list):
        return [timestamp_independent(item) for item in value]
    return value


def _determinism_projection(outcome: dict[str, Any]) -> dict[str, Any]:
    """What two fresh runs of the same fixture must produce identically."""
    return {
        "result_hash": outcome.get("result_hash"),
        "lifecycle": outcome.get("lifecycle"),
        "governance_decision": outcome.get("governance_decision"),
        "governance_reason_codes": outcome.get("governance_reason_codes"),
        "risk_score": outcome.get("risk_score"),
        "export_release_basis": outcome.get("export_release_basis"),
        "result": timestamp_independent(outcome.get("result")),
        "governed_result": timestamp_independent(outcome.get("governed_result")),
        "export_payload": timestamp_independent(outcome.get("export_payload")),
    }


# --- Report ------------------------------------------------------------------


def _versions(labels: dict[str, Any]) -> dict[str, Any]:
    return {
        "label_schema_version": labels["label_schema_version"],
        "comparison_schema_version": COMPARISON_SCHEMA_VERSION,
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "detector_version": comparison_detector.DETECTOR_VERSION,
        "workflow_version": comparison_store.WORKFLOW_VERSION,
        "section_key": SECTION_ITEM_1A,
        "governance_policy_id": comparison_governance.POLICY["policy_id"],
        "governance_policy_version": comparison_governance.POLICY["policy_version"],
        "validator_versions": {
            "citation_support": comparison_validators.CITATION_SUPPORT_VERSION,
            "numeric_consistency": comparison_validators.NUMERIC_CONSISTENCY_VERSION,
            "direction_consistency": comparison_validators.DIRECTION_CONSISTENCY_VERSION,
        },
    }


def run_suite(labels: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run every fixture twice from fresh temporary state and score the suite.

    Returns the complete machine-readable report. Contains no absolute or
    temporary paths by construction.
    """
    labels = labels or load_labels()
    scores: list[dict[str, Any]] = []
    determinism: dict[str, bool] = {}
    invariant_failures: list[str] = []

    for fixture in labels["fixtures"]:
        with tempfile.TemporaryDirectory(prefix="cmp-regress-a-") as first_dir:
            first = run_fixture(fixture, Path(first_dir))
        with tempfile.TemporaryDirectory(prefix="cmp-regress-b-") as second_dir:
            second = run_fixture(fixture, Path(second_dir))

        determinism[fixture["fixture_id"]] = _determinism_projection(
            first
        ) == _determinism_projection(second)

        score = score_fixture(fixture, first)
        score["invariants"]["evidence_sides_correct"] = score["evidence_sides_ok"]
        scores.append(score)
        for name, passed in score["invariants"].items():
            if not passed:
                invariant_failures.append(f"{fixture['fixture_id']}: {name}")

    metrics = aggregate_metrics(scores, determinism)
    gates = evaluate_gates(metrics)
    passed = all(gate["passed"] for gate in gates) and not invariant_failures

    return {
        "suite": "comparison-regression",
        "scope": (
            "CONTROLLED SYNTHETIC fixtures (fictional companies). Protects "
            "deterministic behavior from silent drift; NOT a real-filing "
            "benchmark and not evidence of accuracy on real SEC filings."
        ),
        "versions": _versions(labels),
        "fixture_hashes": fixture_input_hashes(labels),
        "fixture_count": len(scores),
        "metrics": metrics,
        "gates": gates,
        "determinism": determinism,
        "invariant_failures": sorted(invariant_failures),
        "fixtures": scores,
        "passed": passed,
    }


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def print_summary(report: dict[str, Any]) -> None:
    """Human-readable terminal summary (no paths, no evidence text)."""
    print("Comparison regression suite (controlled synthetic fixtures)")
    print(f"  {report['scope']}")
    versions = report["versions"]
    print(
        f"  labels={versions['label_schema_version']} "
        f"detector={versions['detector_version']} "
        f"workflow={versions['workflow_version']}"
    )
    print(
        f"  comparison={versions['comparison_schema_version']} "
        f"export={versions['export_schema_version']} "
        f"policy={versions['governance_policy_id']}"
        f"/{versions['governance_policy_version']}"
    )

    print(f"\nFixtures ({report['fixture_count']}):")
    for score in report["fixtures"]:
        flags = []
        if score["false_positives"]:
            flags.append(f"FP={len(score['false_positives'])}")
        if score["missed"]:
            flags.append(f"missed={len(score['missed'])}")
        if score["unchanged_false_positives"]:
            flags.append(f"unchanged-FP={len(score['unchanged_false_positives'])}")
        broken = [name for name, ok in score["invariants"].items() if not ok]
        if broken:
            flags.append("invariants:" + ",".join(sorted(broken)))
        status = "ok" if not flags else "; ".join(flags)
        print(
            f"  {score['fixture_id']:<26} "
            f"changes={score['detected_change_count']}/"
            f"{score['expected_change_count']} "
            f"lifecycle={score['lifecycle']} "
            f"decision={score['governance_decision']} "
            f"release={score['export_release_basis'] or 'none'}  [{status}]"
        )

    print("\nMetrics (numerator/denominator):")
    for name, metric in report["metrics"].items():
        suffix = " ZERO-DENOMINATOR (passes)" if metric["zero_denominator"] else ""
        print(
            f"  {name:<32} {_fmt(metric['value'])} "
            f"({metric['numerator']}/{metric['denominator']}){suffix}"
        )

    print("\nGates:")
    for gate in report["gates"]:
        mark = "PASS" if gate["passed"] else "FAIL"
        print(
            f"  [{mark}] {gate['gate']:<32} {gate['comparison']} "
            f"{gate['threshold']} (got {_fmt(gate['value'])})"
        )

    if report["invariant_failures"]:
        print("\nWorkflow invariant failures:")
        for failure in report["invariant_failures"]:
            print(f"  FAIL {failure}")

    unstable = [name for name, stable in report["determinism"].items() if not stable]
    if unstable:
        print("\nNondeterministic fixtures: " + ", ".join(sorted(unstable)))

    print("\n" + ("ALL GATES PASSED" if report["passed"] else "REGRESSION DETECTED"))


# --- Baseline ----------------------------------------------------------------


def baseline_view(report: dict[str, Any]) -> dict[str, Any]:
    """The compact, human-diffable baseline projection of a report.

    Deliberately not a full output snapshot: versions, per-metric values with
    denominators, gate configuration, and each fixture's stable result hash.
    A detector or workflow version change is VISIBLE here and does not bless
    changed metrics — the metric lines diff on their own.
    """
    return {
        "suite": report["suite"],
        "scope": report["scope"],
        "versions": report["versions"],
        "fixture_hashes": report["fixture_hashes"],
        "gates": {
            gate["gate"]: {
                "comparison": gate["comparison"],
                "threshold": gate["threshold"],
            }
            for gate in report["gates"]
        },
        "metrics": {
            name: {
                "value": metric["value"],
                "numerator": metric["numerator"],
                "denominator": metric["denominator"],
            }
            for name, metric in report["metrics"].items()
        },
        "fixtures": {
            score["fixture_id"]: {
                "lifecycle": score["lifecycle"],
                "detected_change_count": score["detected_change_count"],
                "expected_change_count": score["expected_change_count"],
                "governance_decision": score["governance_decision"],
                "risk_score": score["risk_score"],
                "release_basis": score["export_release_basis"],
                "result_hash": score["result_hash"],
            }
            for score in report["fixtures"]
        },
    }


def in_ci() -> bool:
    """True on GitHub Actions and other standard CI runners."""
    return any(
        os.environ.get(name, "").strip().lower() in ("1", "true", "yes")
        for name in ("CI", "GITHUB_ACTIONS", "CONTINUOUS_INTEGRATION")
    )


def diff_baseline(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Human-readable differences between a stored baseline and a fresh run."""
    lines: list[str] = []
    for key, value in current["versions"].items():
        old = baseline.get("versions", {}).get(key)
        if old != value:
            lines.append(f"  version {key}: {old} -> {value}")
    for name, metric in current["metrics"].items():
        old = baseline.get("metrics", {}).get(name, {})
        if old.get("value") != metric["value"] or old.get("denominator") != metric[
            "denominator"
        ]:
            lines.append(
                f"  metric {name}: {old.get('value')} "
                f"({old.get('numerator')}/{old.get('denominator')}) -> "
                f"{metric['value']} ({metric['numerator']}/{metric['denominator']})"
            )
    for fixture_id, fixture in current["fixtures"].items():
        old = baseline.get("fixtures", {}).get(fixture_id)
        if old is None:
            lines.append(f"  fixture {fixture_id}: new")
            continue
        for field in sorted(fixture):
            if old.get(field) != fixture[field]:
                lines.append(
                    f"  fixture {fixture_id}.{field}: "
                    f"{old.get(field)} -> {fixture[field]}"
                )
    for fixture_id in sorted(set(baseline.get("fixtures", {})) - set(current["fixtures"])):
        lines.append(f"  fixture {fixture_id}: removed")
    for name in sorted(set(baseline.get("gates", {})) | set(current["gates"])):
        old = baseline.get("gates", {}).get(name)
        new = current["gates"].get(name)
        if old != new:
            lines.append(f"  gate {name}: {old} -> {new}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Controlled comparison regression suite and CI gate. Offline: no "
            "AWS credentials, no network, no LLM."
        )
    )
    parser.add_argument(
        "--json", action="store_true", help="print the machine-readable report only"
    )
    parser.add_argument(
        "--report", metavar="PATH", help="also write the JSON report to PATH"
    )
    parser.add_argument(
        "--baseline",
        metavar="PATH",
        default=str(DEFAULT_BASELINE),
        help="baseline file to compare against (default: eval/comparison_regression_baseline.json)",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="rewrite the baseline from this run (refused in CI; never part of a normal run)",
    )
    args = parser.parse_args(argv)

    if args.write_baseline and in_ci():
        print(
            "Refusing to write the baseline in CI: a baseline change must be an "
            "explicit, reviewed commit made by a human.",
            file=sys.stderr,
        )
        return 2

    try:
        labels = load_labels()
    except LabelError as exc:
        print(f"Invalid labels: {exc}", file=sys.stderr)
        return 2

    report = run_suite(labels)
    view = baseline_view(report)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_summary(report)

    if args.report:
        Path(args.report).write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
        if not args.json:
            print(f"\nJSON report written to {args.report}")

    baseline_path = Path(args.baseline)
    if args.write_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(view, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if not args.json:
            print(f"\nBaseline written to {baseline_path.name}")
    elif baseline_path.exists() and not args.json:
        differences = diff_baseline(
            json.loads(baseline_path.read_text(encoding="utf-8")), view
        )
        print("\nBaseline diff:")
        if differences:
            for line in differences:
                print(line)
            print(
                "  (a deliberate change is blessed by re-running with "
                "--write-baseline and committing the diff)"
            )
        else:
            print("  no differences")

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
